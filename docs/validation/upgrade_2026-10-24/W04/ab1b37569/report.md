# W04 · 设备匹配与平面格式能力门 —— 验证报告

- 基线提交：ab1b37569（W03 收口）
- 机器：B（Windows 11 Pro 26200 / i9-13900K / RTX 4090 621.01 + Intel UHD
  770 / av 18.1.0（libx264 可用）/ cuda-bindings 13.4.2 / driver 13050）
- 对应计划：W04（W-I04），前置 W02 ✅ W03 ✅

## 交付内容

### 1. 设备对齐：拒绝固定 CUdevice(0) 隐含假设

`CudaGlInteropSession.initialize()`（`cuda_resident.py`）：

- 新增 `_select_aligned_device`：先 `cuGLGetDevices(CU_GL_DEVICE_LIST_ALL)`
  查与**当前 GL 上下文**关联的 CUDA 设备 → 用该设备建上下文
  （`device_source='gl_context'`）；
- GL 在非 CUDA 适配器（返回 CUDA_ERROR_NO_DEVICE=100，如 GL 落核显）→
  **fail-closed**（`no_cuda_device_for_gl_context`），上下文不创建——旧行
  为会在 bind 期以难归因的注册错误失败或错配设备；
- 查询失败（典型：无 current GL 上下文）且全机恰一台 CUDA 设备 → device 0
  与之确定重合（`device_source='single_cuda_device'`，如实记录）；
- 多台 CUDA 设备且无法对齐 → fail-closed（`cannot_align_gl_adapter`），
  不盲选 0。
- 会话 stats 新增能力身份：`device_ordinal / device_name /
  device_source / driver_version`（能力缓存失效条件 = 身份变化）。

### 2. 平面格式能力门（fail-closed，不错读 UV/越界）

- 新增 `validate_resident_plane_geometry()`：4:2:0 半平面拷贝不变量——
  平面 itemsize 与契约一致且两平面相同、Y ndim=2 shape≥(h,w)、UV ndim∈{2,3}
  且交错深度=2、pitch ≥ row_bytes（防越界读）、设备指针非空。
  **真实 ABI 修正**：4090 实测 PyAV cuda UV 平面为 ndim=3 `[h/2, w/2, 2]`
  （交错分量对）——初版 ndim==2 假设被真机 L2 立即证伪并修正（替身
  测试无法发现的实证例）。
- `_verify_hw_output`（open 期）：平面数恰为 2（4:2:2/4:4:4 交错格式
  显式拒绝，fail-closed 进下载/CPU 回退）；首帧几何先过拷贝不变量门。
- `_convert_frame_planar`（逐帧）：平面数 + 几何逐帧校验（解码中途几何
  漂移 → MediaDecodeError，不越界拷贝）。

### 3. bind 前设备指针身份门（GL 线程，私有上下文内）

`_verify_plane_pointers`：逐平面 `cuPointerGetAttribute` 核验
MEMORY_TYPE==DEVICE 且 DEVICE_ORDINAL==会话设备序号——解码器契约漂移/
跨设备指针在拷贝前显式拒绝（`plane_not_device_memory` /
`plane_device_mismatch`，喂给 W03 按素材降级链）。

## 测试证据

### L1 替身（tests/media/test_cuda_resident_capability_w04.py，17 绿）

- 设备对齐四路径：GL 关联设备 7 被采用（cuCtxCreate 收到 7，非 0）/
  GL 在非 CUDA 适配器 fail-closed（上下文不创建）/ 无 GL+单设备回退并
  记录 / 多设备无法对齐 fail-closed；
- 几何门：真实 ABI 形态（UV ndim=3）通过；pitch 覆盖对齐 padding；
  pitch<row_bytes / itemsize 不匹配 / Y 或 UV 过小 / Y 3-D / UV 交错
  深度≠2 / 空指针逐项拒绝；
- 指针门：host 内存 / 设备序号不匹配 / 属性查询失败拒绝，匹配通过；
- 平面数门：真 DLPack 胶囊（numpy 构造）3 平面 4:4:4 拒绝、双平面转换
  正常。
- W02 替身扩展：fake driver 增加 cuGLGetDevices/cuDeviceGetCount/
  cuDeviceGetName/cuDriverGetVersion/cuPointerGetAttribute 及旋钮
  （gl_devices/gl_err/device_count/ptr_*），原 20 用例保持全绿。

### L2 真机格式矩阵（tests/media/test_cuda_resident_format_matrix_w04.py，4 绿）

- **非对齐尺寸 322×202**（非 2^n 对齐、半高 101 为奇数行）：驻留导入
  纹理 md5 与同解码器下载参考**逐字节相等**（pitch padding 几何正确）；
- **4:2:2 High 4:2:2 H.264**：open fail-closed（MediaOpenError，不冒充
  驻留）；经真实管理器路由回退 planar/cpu（绝不 cuda_resident）；
- 会话能力身份：stats 携带设备名/驱动版本/来源（gl_context 或
  single_cuda_device）/设备序号。

### L2/L3 既有真机回归（改动后全绿）

- test_windows_cuda_resident.py 37 绿（含 4K H264/P010 md5 对拍——指针
  身份门+几何门在真路径上每帧运行，md5 仍逐字节一致）；
- L3 播放 3 绿 + W03 恢复 1 绿 ×2 轮；tests/media 全组 **3412 绿 0 败**
  （较 W02 后 +21 = W04 新增用例）。

### 素材锚点（tests/test_videos 为 gitignore 本地生成，同 B 线惯例）

- `unaligned_322x202_h264.mov`
  sha256=c2151b473bdb5f3e09d267782825368818e442bf4f60dd77e158746ba7b1eb19
- `yuv422_h264.mov`
  sha256=ef9d2cf1cfa4349619c36668fb89eac405d0422d9dd67e762e323864bf0fd098
- 生成：av libx264 crf18 24fps 24帧，逐帧确定性梯度图案（Y/U/V 各异
  斜率）；422 需 `codec_context.profile = 4`（FF_PROFILE_H264_HIGH_422），
  x264 私有 options 的 profile 名不生效（实证）。

## 验收矩阵对照（Windows 侧执行态）

| 矩阵项 | 状态 | 证据 |
|---|---|---|
| 4090+核显组合，GL 在 4090 | ✅ 真机 | L2/L3 全部（stats gl_context/single_cuda_device） |
| GL 在非 CUDA 适配器（核显） | ✅ L1 替身 fail-closed | no_cuda_device_for_gl_context 路径 |
| 强制错设备 | ✅ L1 替身（GL 设备 7≠0） | created_device==7 用例 |
| 多 CUDA 设备无法对齐 | ✅ L1 替身 fail-closed | cannot_align_gl_adapter 用例 |
| 420 8bit | ✅ 真机 md5 | 既有 4K/非对齐对拍 |
| 420 10bit（P010/P016） | ✅ 真机 md5 | test_p010_resident_import |
| **422** | ✅ 真机 fail-closed 回退 | 本报告 L2（open 拒绝 + 管理器路由回退） |
| 444 | ✅ L1（DLPack 3 平面拒绝） | 平面数门用例 |
| 奇数尺寸 | ⚠️ 如实登记 | H.264 420 编码尺寸必为偶数（331×197 无法存在）；以 322×202 非对齐（含奇数半高 101）覆盖几何压力 |
| NV12/P010 full/limited、601/709/2020、PQ/HLG 色彩数值（W04.2） | 📋 后续子项 | 本包交付设备/格式几何门；色彩数值对拍属 W04.2/M19 共享 shader 帧 ABI 集成 |
| VT→GL 行 | ➖ 不适用 | M19/Mac 产出；Windows 记"不适用" |
| AMD/Intel 另机 | ➖ 未验 | 缺机器，如实登记 |

## 已知限制与如实登记

- W04.2 色彩数值子项（bit_depth range 系数/matrix/primaries/transfer/
  chroma siting/crop/alpha、工作域 vs 显示域限幅）未在本包展开——按计划
  由 M19 共享 shader/帧 ABI 集成后以同源夹具对拍，Windows 提供驻留生产
  路径证据（当前管线 YUV→RGB 转换沿用既有 shader，md5 对拍已锁"同解码
  器下载参考"等价性）。
- 多 CUDA 设备真机交错（4090+另一台 NVIDIA）无法在本机构造；多设备
  fail-closed 与 GL 关联选择由 L1 替身覆盖。
- 裁切（crop）可见区域：驻留路径当前以整帧几何传递（`visible_rect
  0,0,w,h`），素材级 crop 由渲染层 crop pass 处理（FX-M06），不改变平面
  几何门职责。
