# Windows 时间线导出基准（amd_rx6800_config_isolated_smoke）

日期 2026-09-15 ｜ 原始数据：`windows_timeline_export_amd_rx6800_config_isolated_smoke_2026-09-15.json`

协议：输出 30 帧 ｜ 预热 2 帧 ｜ 连续 1 次 ｜ 中位数报告 ｜ topos @ 63a02d05

## 输入材料

| 文件 | 状态 | SHA-256 | 探针 |
|---|---|---|---|
| 2K_H265.mov | ok | 0aee87a9fc159d2e28bfec8286269feef72499007b9bb8422b96fb89a568970b | hevc 1920x1080 yuv420p10le |
| 2K_h264.mp4 | ok | 3d420027cb4b6afcff11e5cc7b3510ddbb019de732ee403d21f6f40b3f1af04f | h264 1920x1080 yuv420p |
| 4K_H264.mov | ok | 044b677f60b6bfb5561bfabb974ebb2ab62c128da9a409d52ff0c1dc67973717 | h264 3840x2160 yuv420p |

| 用例 | 组 | 状态 | 中位 FPS | p50 帧时 | p95 帧时 | 输出 MB | 备注 |
|---|---|---|---|---|---|---|---|
| T0 单段 H.264 直通 | cpu_pipeline | ok | 35.077 | 3.715 | 5.877 | 3.127 |  |
| T0 单段 H.264 直通 | hw_download | ok | 42.898 | 5.978 | 14.228 | 3.167 |  |
| T0 单段 H.264 直通 | ffmpeg_gpu_ref | ok | 67.418 | — | — | 3.168 | ref=gpu_surface |
| T0 单段 H.264 直通 | d3d11_gpu_surface | declared_not_run | — | — | — | — | not_yet_implemented:native_bridge:No module named 'topos_hwdecode_windows' |
| T1 resize + 色彩转换 | cpu_pipeline | ok | 21.016 | 25.254 | 28.368 | 3.485 |  |
| T1 resize + 色彩转换 | hw_download | ok | 17.482 | 34.09 | 44.776 | 3.012 |  |
| T1 resize + 色彩转换 | ffmpeg_gpu_ref | ok | 73.718 | — | — | 2.928 | ref=hwaccel_download；ref_fallback_reason_recorded |
| T1 resize + 色彩转换 | d3d11_gpu_surface | declared_not_run | — | — | — | — | not_yet_implemented:native_bridge:No module named 'topos_hwdecode_windows' |

## 基线比值（现状，非 D3D11 门禁）

| 用例 | 比值 | 数值 |
|---|---|---|
| T0 | cpu_pipeline_vs_ffmpeg | 0.52 |
| T0 | hw_download_vs_ffmpeg | 0.636 |
| T1 | cpu_pipeline_vs_ffmpeg | 0.285 |
| T1 | hw_download_vs_ffmpeg | 0.237 |

## 结论

- T0：组 d3d11_gpu_surface 未运行——not_yet_implemented:native_bridge:No module named 'topos_hwdecode_windows'。
- T0：现有硬解下载路径达到 FFmpeg GPU 参照的 63.6%（计划 §4.3 的 85% 门禁适用于未来 D3D11 路径，此值是现状基线）。
- T1：组 d3d11_gpu_surface 未运行——not_yet_implemented:native_bridge:No module named 'topos_hwdecode_windows'。
- T1：现有硬解下载路径达到 FFmpeg GPU 参照的 23.7%（计划 §4.3 的 85% 门禁适用于未来 D3D11 路径，此值是现状基线）。
