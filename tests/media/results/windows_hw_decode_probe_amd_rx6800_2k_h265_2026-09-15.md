# Windows 硬解能力探针（amd_rx6800）

日期：2026-09-15T15:00:10 ｜ 输入：`tests\test_videos\2K_H265.mov`

协议：真实 PyAV open + 首 5 帧 decode；输出格式与耗时记录；不等于 D3D11 zero-copy

| 设备类型 | 状态 | 帧数 | 输出格式 | 耗时 ms | 错误 |
|---|---|---:|---|---:|---|
| none | ok | 5 | p010le | 202.5 |  |
| vdpau | ok | 5 | yuv420p10le | 92.0 |  |
| cuda | failed | 0 | — | 1.6 | PermissionError: [Errno 1] Operation not permitted |
| vaapi | ok | 5 | yuv420p10le | 74.7 |  |
| dxva2 | ok | 5 | p010le | 220.2 |  |
| qsv | ok | 5 | yuv420p10le | 108.1 |  |
| videotoolbox | ok | 5 | yuv420p10le | 83.1 |  |
| d3d11va | ok | 5 | p010le | 149.8 |  |
| drm | ok | 5 | yuv420p10le | 112.8 |  |
| opencl | ok | 5 | yuv420p10le | 98.8 |  |
| mediacodec | ok | 5 | yuv420p10le | 99.3 |  |
| vulkan | ok | 5 | yuv420p10le | 85.6 |  |
| d3d12va | ok | 5 | p010le | 601.9 |  |
| amf | ok | 5 | yuv420p10le | 123.9 |  |
| ohcodec | ok | 5 | yuv420p10le | 86.1 |  |

## 边界

此探针只证明 PyAV 后端可以打开并解码样本帧；硬解输出被下载/转换到当前 Python 路径时，不能作为 D3D11 全链路 GPU 证据。
