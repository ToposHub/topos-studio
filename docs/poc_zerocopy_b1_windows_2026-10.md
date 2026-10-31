# B1.1 设备驻留与资源访问 PoC 报告（Windows 线 · 机器 B · 2026-10-24）

> 计划：`docs/perf_plan_B_windows_2026-10-22.md` §B1.1（候选 2：PyAV
> `HWAccel(..., is_hw_owned=True)`）。
> 脚本：`scripts/poc_nvdec_zerocopy.py`；原始数据：
> `scripts/poc_nvdec_zerocopy_B1_20261024_run0{1,2}.json`。
> 环境：RTX 4090 / 驱动 621.01 / PyAV 18.1.0（FFmpeg libavcodec 62.28.102）/
> Windows 11 26200，commit `e650fdccd` 后树。进入条件（B0.3 瓶颈假设 H1+主指标）
> 已由帧预算报告 §7 满足。

## 结论一览（证据等级 L2，本机真机）

| # | 验证项 | 结果 |
|---|---|---|
| 1 | cuda `is_hw_owned=True` 保留硬件帧 | ✅ 帧格式保持 `cuda`，24 帧 0 字节经 CPU（对照下载基线 298.6MB/24 帧） |
| 2 | 覆盖 NV12 + P010 | ✅ 4K H264（NV12 面）与 4K H265 10bit（P010 面）均驻留成功 |
| 3 | **Python 消费接口存在** | ✅ `frame.planes` 可访问；`__dlpack_device__=(2,0)`（kDLCUDA）；`__dlpack__` 导出胶囊（首 qword=设备指针，实证偏移，见下） |
| 4 | **correctness 对拍** | ✅ 6/6 帧 Y 平面 md5 与**同解码器下载参考逐字节一致**（行归一化，驻留 pitch=4096 vs 参考 pitch=3840） |
| 5 | 生命周期 | ✅ EOF→close→二次 open 全文件（743 帧）×2 轮无异常 |
| 6 | d3d11va 保留路线 | ❌ 本机 `is_hw_owned=True` 直接解码失败：H264 `InvalidDataError:avcodec_send_packet`、H265 `MemoryError`（run02 JSON 原样记录，fail-closed） |

**B1.2 路线含义**：纯 PyAV 栈里 cuda 是唯一可行驻留路线；d3d11 驻留在 4090/
621.01 上连解码都不成立（与 B0.3 判断一致：D3D11 帧需要原生桥接层）。cuda 帧
的设备指针可从 Python 取得 → CUDA→GL interop（`cuGraphicsGLRegisterImage`）
具备纯 ctypes 实现条件，无需 native 扩展即可做最小渲染消费验证（B1.2）。

## 方法与 ABI 实录（复现关键）

1. **驻留机制**（`.venv/.../av/video/codeccontext.py:169-184` 实读）：
   `is_hw_owned=True` 时 `_transfer_hwframe` 仅设 `frame._device_id` 后原样返回
   硬件帧，不调 `av_hwframe_transfer_data`；cuda 场景 PyAV 自动加
   `primary_ctx=1` 选项（`hwaccel.py __init__`）。
2. **DLPack 胶囊 ABI**：标准 DLManagedTensor 文档偏移在本机读不到合法数据
   指针；实测布局（内容回读 + md5 对拍双重验证）：`data@+0`（首 qword 即
   CUdeviceptr）、`device@+8`、`ndim@+16`、`dtype@+20`、`shape@+24`、
   `strides@+32`（**元素单位**，Y 平面 `[4096,1]`=pitch 4096）、
   `byte_offset@+40`。胶囊名 `dltensor`。
3. **驱动入口陷阱**：驱动 621.01 上未版本化符号 `cuMemAlloc`/`cuMemcpyDtoH`
   实测 rc=201（签名已变），必须用 `cuMemcpyDtoH_v2` 等带版本入口。
4. **时序约束**：进程内把主上下文设 current 后，PyAV `av.open(hwaccel=cuda)`
   会以 -129 失败——解码会话与 nvcuda 手工操作必须时间分离（脚本已按
   "先解帧+解析胶囊→close 容器→再触碰 CUDA 上下文" 组织）。
5. correctness 的回读字节只用于对拍，不计入性能口径（计划 B1.1 验收要求）。

## 与计划验收的映射

- 「decoder→渲染消费的原始帧不经过 CPU」：解码侧成立（0 CPU 字节 + 格式保持
  `cuda`）；**渲染消费（GL interop 采样）尚未接线**——这是 B1.2 的比较对象
  （cuda→GL 新绑定 vs D3D11 原生桥接），本 PoC 不提前宣称。
- 「输出与同一解码器下载参考验证」：✅ md5 逐帧一致（表 #4）。
- 「EOF/seek/错误/取消后资源可归还」：EOF/close/重入 ✅；seek/错误注入路径
  留待 B1.2 消费链成形后一并测（如实登记，不冒充已完成）。
- 「显式回退」：d3d11va 两例失败原样记录；未发现静默软解（`allow_software_fallback`
  默认 True 的行为在 raw 层已由 B0.2 的格式门控方法覆盖）。

## 后续（进入 B1.2 的输入）

1. 最小 CUDA→GL 消费：`cuGraphicsGLRegisterImage`(pitch 纹理需走 buffer 或
   CUDA array 路线，pitch 4096≠3840 必须处理) + moderngl 采样，与
   `probe_wgl_shared_texture_roundtrip.py`（D3D11↔GL，已真机可跑）对比成本。
2. UV 平面与 P010 的 16bit 采样路径在 GL 侧的上传/采样约定（现有
   `yuv_gpu_upload.py` 的 R16UI 池可复用语义）。
3. seek/取消/错误注入的生命周期补充测试。
