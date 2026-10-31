# Windows 硬解能力探针（amd_rx6800）

日期：2026-09-15T14:59:56 ｜ 输入：`tests\test_videos\2K_h264.mp4`

协议：真实 PyAV open + 首 5 帧 decode；输出格式与耗时记录；不等于 D3D11 zero-copy

| 设备类型 | 状态 | 帧数 | 输出格式 | 耗时 ms | 错误 |
|---|---|---:|---|---:|---|
| none | ok | 5 | nv12 | 233.8 |  |
| vdpau | ok | 5 | yuv420p | 98.3 |  |
| cuda | failed | 0 | — | 39.0 | PermissionError: [Errno 1] Operation not permitted |
| vaapi | ok | 5 | yuv420p | 68.9 |  |
| dxva2 | ok | 5 | nv12 | 220.1 |  |
| qsv | ok | 5 | yuv420p | 93.4 |  |
| videotoolbox | ok | 5 | yuv420p | 67.1 |  |
| d3d11va | ok | 5 | nv12 | 184.6 |  |
| drm | ok | 5 | yuv420p | 95.8 |  |
| opencl | ok | 5 | yuv420p | 67.5 |  |
| mediacodec | ok | 5 | yuv420p | 69.6 |  |
| vulkan | ok | 5 | yuv420p | 71.2 |  |
| d3d12va | ok | 5 | nv12 | 576.8 |  |
| amf | ok | 5 | yuv420p | 92.1 |  |
| ohcodec | ok | 5 | yuv420p | 71.5 |  |

## 边界

此探针只证明 PyAV 后端可以打开并解码样本帧；硬解输出被下载/转换到当前 Python 路径时，不能作为 D3D11 全链路 GPU 证据。
