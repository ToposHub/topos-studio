# Windows 时间线导出基准（amd_rx6800）

日期 2026-09-15 ｜ 原始数据：`windows_timeline_export_amd_rx6800_2026-09-15.json`

协议：输出 480 帧 ｜ 预热 60 帧 ｜ 连续 3 次 ｜ 中位数报告 ｜ topos @ 8fde64ec

| 用例 | 组 | 状态 | 中位 FPS | p50 帧时 | p95 帧时 | 输出 MB | 备注 |
|---|---|---|---|---|---|---|---|
| T0 单段 H.264 直通 | cpu_pipeline | ok | 42.525 | 21.034 | 37.625 | 43.785 |  |
| T0 单段 H.264 直通 | hw_download | ok | 139.021 | 6.087 | 6.419 | 43.498 |  |
| T0 单段 H.264 直通 | ffmpeg_gpu_ref | ok | 151.679 | — | — | 42.303 | ref=gpu_surface |
| T1 resize + 色彩转换 | cpu_pipeline | ok | 21.988 | 39.276 | 74.278 | 43.477 |  |
| T1 resize + 色彩转换 | hw_download | ok | 27.086 | 34.364 | 49.256 | 43.492 |  |
| T1 resize + 色彩转换 | ffmpeg_gpu_ref | ok | 151.092 | — | — | 42.303 | ref=gpu_surface |

## 基线比值（现状，非 D3D11 门禁）

| 用例 | 比值 | 数值 |
|---|---|---|
| T0 | cpu_pipeline_vs_ffmpeg | 0.28 |
| T0 | hw_download_vs_ffmpeg | 0.917 |
| T1 | cpu_pipeline_vs_ffmpeg | 0.146 |
| T1 | hw_download_vs_ffmpeg | 0.179 |

## 结论

- T0：现有硬解下载路径达到 FFmpeg GPU 参照的 91.7%（计划 §4.3 的 85% 门禁适用于未来 D3D11 路径，此值是现状基线）。
- T1：现有硬解下载路径达到 FFmpeg GPU 参照的 17.9%（计划 §4.3 的 85% 门禁适用于未来 D3D11 路径，此值是现状基线）。
