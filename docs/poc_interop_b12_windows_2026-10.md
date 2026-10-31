# B1.2 互操作与路线选择报告（Windows 线 · 机器 B · 2026-10-24）

> 计划：`docs/perf_plan_B_windows_2026-10-22.md` §B1.2。
> 脚本：`scripts/poc_cuda_gl_interop.py`；原始数据：
> `scripts/poc_cuda_gl_interop_B12_20261024_run01.json`。
> 环境：RTX 4090 / 驱动 621.01（CUDA 13.5）/ PyAV 18.1.0 / GL 3.3 core /
> cuda-bindings 13.4.2（venv 内随 torch 栈存在），commit `9058ffbfe` 后树。
> 证据等级：**L2**（本机真机编译/执行/对拍）。

## 路线选择结论

**选择 CUDA→GL interop（私有上下文 + cuda-bindings）接入 B2.1 生产预览
消费**；D3D11 原生桥接路线维持不立项（见 §4）；CPU/下载路径保留为回退。

- 比较对象与终点：三路径（生产 CPU planar / 生产 HW download planar /
  零拷贝候选）统一以「GL 纹理就绪可采样」为计时终点，同素材、同帧数
  （10 warmup + 100 timed × 5 组交错轮换采样），decode 段与 transfer 段
  分开归因；零拷贝纹理格式与生产 planar 上传池**同构**
  （NV12→R8+RG8，P010→R16UI+RG16UI，usampler 语义）。

## 1. 计时矩阵（steady fps，min/max 跨 5 组）

| 素材 | cpu_planar | hw_download_planar | hw_zero_copy | 零拷贝/下载 |
|---|---|---|---|---|
| 4K H264 (NV12) | **827.5** (811–837) | 102.4 (101.5–103.5) | **241.2** (230–248) | **2.36×** |
| 4K H265 10bit (P010) | 326.6 (307–338) | 80.4 (79.9–80.8) | **306.3** (302–313) | **3.81×** |

分段（ms/帧，5 组均值）：

| 素材×路径 | decode | transfer | CPU 字节/帧 |
|---|---|---|---|
| H264 cpu | 0.085 | 1.124 | 12.44 MB |
| H264 download | 6.184 | 3.584 | 12.44 MB |
| H264 zero_copy | 3.808 | **0.340** | **0** |
| H265 cpu | 0.466 | 2.599 | 24.89 MB |
| H265 download | 5.346 | 7.092 | 24.89 MB |
| H265 zero_copy | 2.772 | **0.493** | **0** |

解读（对照 B0.3 假设）：

- **H1 成立**：4K H264 下载路径的 transfer 段（下载+行提取+上传，
  3.58ms）与 decode 内隐式下载把 102fps 压到 CPU 的 1/8；零拷贝把
  transfer 段压到 0.34ms（纯设备内 2×DtoA 拷贝 + map/unmap）。
- **H2 成立**：10bit H265 上零拷贝（306fps）与 CPU（327fps）单轨接近
  ——软解计算成本本身不再是压倒项，下载税此前掩盖了硬解优势
  （B0.2 的 133→306fps）。
- CPU 路径 decode 段 0.085/0.466ms 是帧线程流水线的摊销值（生产
  ffmpeg_source.py:1137-1147 同款 `thread_type=AUTO` + `thread_count=0`，
  本 PoC 实测 943fps decode-only 复现 B0.2 的 841fps 量级），单轨仍占优；
  零拷贝的价值在多轨（H3：CPU 解码核竞争）与 10bit 场景，B2 多轨验收
  时验证，不在此预支结论。

## 2. 正确性证据（全部 L2 本机对拍）

| 验证项 | 结果 |
|---|---|
| 纹理 readback 逐字节 md5 vs 同解码器下载参考（NV12/P010，各 4 帧双平面） | **8/8 平面全对**（4/4+4/4） |
| **FBO 真采样对拍**（texelFetch 双平面→RGBA8 vs CPU 同公式期望；计划：“可注册纹理≠可正确采样”） | NV12 max_diff=0；P010 max_diff=1 LSB（10bit/65535 归一化+u8 量化舍入） |
| **交织序正确性**（生产现实序：解码会话打开→逐帧 DLPack+map/copy/unmap 交织→首计时帧读回 md5 vs 免疫毒化的下载参考会话） | **两种素材均逐字节一致**（同步由 map/unmap 提供，无跨上下文竞争迹象） |
| UV 平面 DLPack ABI（B1.1 遗留项） | shape=[h/2, w/2, **2**]（ndim=3，交错 U/V），strides=[pitch, 2, 1] 元素单位；pitch 与 Y 同为 4096（4K） |
| P010 16bit 消费语义 | R16UI/RG16UI usampler 直传（生产 yuv_gpu_upload.py R16UI 池同构），码值不经 >>6，归一化在着色器 |

## 3. 本机 ABI/时序实录（B1.1 结论修正，B2.1 接线约束）

1. **ctypes 路线否决**：手写 ctypes 的 `cuMemcpy2D_v2` 以合法参数被拒
   （rc=1 invalid argument；同参数值经 cuda-bindings 成功），未版本化
   `cuMemcpy2D` rc=201（与 B1.1 的 `cuMemAlloc` 同病）。B1.1 的
   “cuda→GL 纯 ctypes 可行”**修正为经 cuda-bindings（cuda-python
   13.4.2）可行**。生产接线依赖该库（本 venv 已存在，随 torch 栈）。
2. **-129 毒化机制修正版**（ctx_poison_matrix 子进程探针实录）：
   PyAV `is_hw_owned=True`（primary_ctx=1）会话在主上下文被本进程
   初始化过后**永久** -129——unbind 无效；`cuDevicePrimaryCtxReset`
   可恢复但对活跃会话破坏性（生产多源并发下不可用）。
   `is_hw_owned=False` 会话（FFmpeg 私有 ctx 路径）**完全免疫**。
   **生产规则：interop 一律私有上下文（cuCtxCreate），主上下文永不
   触碰**——解码会话随之可自由 open/close/reopen（R2/R3 实测）。
3. **驱动不校验 interop 句柄**：垃圾句柄 map 直接访问违例（子进程
   0xC0000005 捕获为证）——生产代码必须自持句柄生命周期表，禁止将
   未验证整数送入驱动 API。
4. capability：WGL_NV_DX_interop(2) 在本机 GL 3.3 上下文存在；
   `cuGraphicsD3D11RegisterResource` 驱动导出存在（D3D11 侧记录）；
   GL_TEXTURE_2D_ARRAY 注册+layer1 array 映射 ✅（array 纹理路线可用）；
   GL renderer 与 CUDA 设备名匹配（同一 4090）。
5. 驱动 621.01 导出表无 `cuGraphicsResourceGetMappedArray`（旧入口已
   移除），现行符号 `cuGraphicsSubResourceGetMappedArray`（cuda-bindings
   已按此调用）。

## 4. 路线对比与落选理由

| 路线 | 状态 | 依据 |
|---|---|---|
| **cuda 驻留 + CUDA→GL interop（私有 ctx）** | **入选（本报告 §1–§3）** | L2 全链路：正确性 + 时序 + 失效行为齐备；0 CPU 字节；无原生扩展依赖（cuda-bindings 纯 Python 轮） |
| d3d11va 驻留 + 原生桥接 | 不立项 | B1.1 已证伪解码端（本机保留即解码失败）；原生扩展 `topos_hwdecode_windows` 不存在（B0.3）；重启需 B3/B4 立项证据 |
| D3D11 合成路线（WGL_NV_DX_interop 共享） | 不立项（依赖 B3/B4） | 扩展与驱动入口存在≠有 D3D11 帧可共享（解码端同上）；纯 D3D11 生产链未就绪（B0.3 §2），按计划标注不提前宣称 |
| CPU planar / HW download planar | 保留为回退 | 生产现役路径；零拷贝任何环节失败按 B2.1 回退矩阵回落 |

## 5. 资源上限与失效注入

- **上限**：256/256 纹理注册成功（0 失败，注册批 28ms）；全量
  map+unmap ✅（per-resource 模式 9.5ms/512 次调用）；全注销后重注册
  恢复 ✅。生产 present slot=24，池裕量充分。
- **失效注入**（进程内安全项 + 子进程危险项，全部结构化记录）：

| 注入 | 驱动行为 | 我方恢复 |
|---|---|---|
| 非法 GL 名注册 | rc=400 invalid handle（拒绝） | 正常 |
| 双重 unregister | 第二次 rc=400（拒绝，无副作用） | 正常 |
| unregister NULL | rc=400 拒绝 | 正常 |
| 坏 extent + 无效设备指针拷贝 | rc=1 拒绝，**纹理内容不变** | 正常 |
| 注册期删 GL 纹理→unregister | rc=0 容忍（无崩溃） | 正常 |
| GL 上下文销毁→unregister | rc=219 invalid graphics context（干净报错） | 新上下文重建 ✅ |
| 无 current ctx map | rc=201 invalid context（干净报错） | 正常 |
| 垃圾句柄 map | **访问违例崩溃（0xC0000005）**——见 §3-3 | 生产侧句柄表纪律 |
| 恢复循环（注册不改内容/注销后再注册） | 内容 byte-stable | ✅ |

- 真机设备移除（TDR/拔卡）无法安全注入：按计划登记为后续人工项
  （需 OS 级 TDR 触发手段），不以删除设备 API 冒充。

## 6. 与计划验收映射

- 「有一致终点的同机矩阵」✅ §1（5 组交错、统一 GL-ready 终点、分段归因）。
- 「资源上限和失败行为」✅ §5。
- 「明确选择一条先接生产」✅ cuda→GL（私有 ctx + cuda-bindings），
  落选理由 §4。
- 「现有 GL 路线所需的最小缺口」：无需 `_wrap_external_gpu_planes` 之外
  的新 GL 消费入口——零拷贝帧以 GpuDecodedFrame(api=opengl,
  texture_id=已注册互操作纹理) 形态走**现有** VT 零拷贝消费通路
  （edit_gpu_preview_renderer.py:2101-2119），B2.1 只需新增驻留帧生产侧。
- 「纯 D3D11 的 shader/生产链未就绪时标依赖 B3/B4」✅ §4。

## 7. 进入 B2.1 的输入（接线设计约束）

1. 互操作会话与解码会话**线程分离或至少上下文隔离**：interop 持有
   私有 CUDA ctx + GL ctx current（GL 渲染线程）；解码线程永不触碰
   CUDA API；`-129` 毒化由“主上下文零接触”规则根除。
2. 依赖：cuda-bindings（venv 已有）；opt-in 环境开关默认关闭；
   失败回退链：零拷贝→HW download planar→CPU planar，generation/帧序
   /所有权按计划 B2.1 验收维护。
3. 每平面一次 map/copy/unmap（同步语义已验证）；纹理池复用注册
   （注册成本 0.11ms/纹理一次性，map/unmap 对 19µs/次）。
4. P010 走 R16UI usampler（生产池同构）；UV 平面 DLPack 为 ndim=3
   交错布局，几何按 §2 推导。
