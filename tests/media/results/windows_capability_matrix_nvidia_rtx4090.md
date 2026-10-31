# Windows 能力矩阵（W1-04：nvidia_rtx4090 分册）

日期：2026-09-12T11:50:51 ｜ GPU：NVIDIA GeForce RTX 4090 ｜ PyAV 18.1.0 ｜ 后端：d3d11va, nvdec

裁决协议：输出 ∈ {nv12, p010le, p016le} 才算 supported（PyAV 会静默回退软解，
只信输出格式，不信会话成败）。

| 素材 | codec | 格式 | 分辨率 | 后端 | 裁决 | 输出格式 | fps |
|---|---|---|---|---|---|---|---|
| 2K_h264.mp4 | h264 | yuv420p | 1920×1080 | d3d11va | supported | nv12 | 297.8 |
| 2K_h264.mp4 | h264 | yuv420p | 1920×1080 | nvdec | supported | nv12 | 367.3 |
| 4K_H264.mov | h264 | yuv420p | 3840×2160 | d3d11va | supported | nv12 | 119.6 |
| 4K_H264.mov | h264 | yuv420p | 3840×2160 | nvdec | supported | nv12 | 144.1 |
| 2K_H265.mov | hevc | yuv420p10le | 1920×1080 | d3d11va | supported | p010le | 305.2 |
| 2K_H265.mov | hevc | yuv420p10le | 1920×1080 | nvdec | supported | p010le | 464.3 |
| 4K_H265.mov | hevc | yuv420p10le | 3840×2160 | d3d11va | supported | p010le | 109.5 |
| 4K_H265.mov | hevc | yuv420p10le | 3840×2160 | nvdec | supported | p010le | 160.0 |
| diag_h264_422_10bit_720p.mp4 | h264 | yuv422p10le | 1280×720 | d3d11va | unsupported_silent_sw | yuv422p10le | 2955.8 |
| diag_h264_422_10bit_720p.mp4 | h264 | yuv422p10le | 1280×720 | nvdec | unsupported_silent_sw | yuv422p10le | 2292.9 |
| diag_hevc_422_10bit_720p.mp4 | hevc | yuv422p10le | 1280×720 | d3d11va | unsupported_silent_sw | yuv422p10le | 1408.2 |
| diag_hevc_422_10bit_720p.mp4 | hevc | yuv422p10le | 1280×720 | nvdec | unsupported_silent_sw | yuv422p10le | 1353.4 |
| diag_av1_8bit_720p.mp4 | av1 | yuv420p | 1280×720 | d3d11va | unsupported_silent_sw | yuv420p | 3489.2 |
| diag_av1_8bit_720p.mp4 | av1 | yuv420p | 1280×720 | nvdec | unsupported_silent_sw | yuv420p | 3485.1 |

## 说明

- 本分册来自 nvidia_4090 真机；其它 vendor 行按 K-W5 不写（无真机）。
- 真实素材来自 `tests/test_videos`；4:2:2 诊断素材为现场合成（720p，
  硬解单元按格式而非分辨率拒绝，720p 结论可代表）。
