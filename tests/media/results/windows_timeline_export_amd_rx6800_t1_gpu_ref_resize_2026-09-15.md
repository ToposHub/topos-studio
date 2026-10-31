# Windows 时间线导出基准（amd_rx6800_t1_gpu_ref_resize）

日期 2026-09-15 ｜ 原始数据：`windows_timeline_export_amd_rx6800_t1_gpu_ref_resize_2026-09-15.json`

协议：输出 30 帧 ｜ 预热 0 帧 ｜ 连续 1 次 ｜ 中位数报告 ｜ topos @ 98106291

## 输入材料

| 文件 | 状态 | SHA-256 | 探针 |
|---|---|---|---|
| 2K_H265.mov | ok | 0aee87a9fc159d2e28bfec8286269feef72499007b9bb8422b96fb89a568970b | hevc 1920x1080 yuv420p10le |
| 2K_h264.mp4 | ok | 3d420027cb4b6afcff11e5cc7b3510ddbb019de732ee403d21f6f40b3f1af04f | h264 1920x1080 yuv420p |

| 用例 | 组 | 状态 | 中位 FPS | p50 帧时 | p95 帧时 | 输出 MB | 备注 |
|---|---|---|---|---|---|---|---|
| T1 resize + 色彩转换 | ffmpeg_gpu_ref | error | — | — | — | — | ref=hwaccel_download |

## 基线比值（现状，非 D3D11 门禁）

| 用例 | 比值 | 数值 |
|---|---|---|

## 结论

- T1：组 ffmpeg_gpu_ref 失败——按计划 §6.1，失败组必须先归因，不得进入下一步门禁。
