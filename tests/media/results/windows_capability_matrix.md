# Windows 能力矩阵（W1-04 初版：AMD 行）

日期：2026-09-12T04:36:06 ｜ GPU：AMD Radeon RX 6800 ｜ PyAV 18.1.0

裁决协议：输出 ∈ {nv12, p010le, p016le} 才算 supported（PyAV 会静默回退软解，
只信输出格式，不信会话成败）。

| 素材 | codec | 格式 | 分辨率 | 后端 | 裁决 | 输出格式 | fps |
|---|---|---|---|---|---|---|---|
| 2K_h264.mp4 | h264 | yuv420p | 1920×1080 | d3d11va | supported | nv12 | 159.1 |
| 4K_H264.mov | h264 | yuv420p | 3840×2160 | d3d11va | supported | nv12 | 65.4 |
| 2K_H265.mov | hevc | yuv420p10le | 1920×1080 | d3d11va | supported | p010le | 155.6 |
| 4K_H265.mov | hevc | yuv420p10le | 3840×2160 | d3d11va | supported | p010le | 49.4 |
| diag_h264_422_10bit_720p.mp4 | h264 | yuv422p10le | 1280×720 | d3d11va | unsupported_silent_sw | yuv422p10le | 1096.3 |
| diag_hevc_422_10bit_720p.mp4 | hevc | yuv422p10le | 1280×720 | d3d11va | unsupported_silent_sw | yuv422p10le | 560.0 |
| diag_av1_8bit_720p.mp4 | av1 | yuv420p | 1280×720 | d3d11va | unsupported_silent_sw | yuv420p | 722.9 |

## 说明

- 本机为 AMD RX 6800（RDNA2）：AV1 硬解预期支持、4:2:2 全系预期拒绝，
  以本表实测为准。NVIDIA/Intel 行按 K-W5 不写（无真机）。
- 真实素材来自 `tests/test_videos`；4:2:2 诊断素材为现场合成（720p，
  硬解单元按格式而非分辨率拒绝，720p 结论可代表）。
