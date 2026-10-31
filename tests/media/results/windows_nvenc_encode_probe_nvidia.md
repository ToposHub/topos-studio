# NVENC 硬编导出实测（W6-01，NVIDIA 真机）

日期：2026-09-15T14:57:57 ｜ GPU：AMD Radeon RX 6800; AMD Radeon RX 6800; Microsoft Basic Render Driver ｜ PyAV 18.1.0

| 编码器 | 格式 | 注册 | 裁决 | 编码耗时 | 回解帧 | 实测码率 | PSNR(dB) | 基线 | 基线 PSNR(dB) |
|---|---|---|---|---|---|---|---|---|---|
| h264_nvenc | nv12 | True | open_or_encode_failed | — | 0 | — Mbps | — | libx264 | — |
| hevc_nvenc | nv12 | True | open_or_encode_failed | — | 0 | — Mbps | — | libx265 | — |
| hevc_nvenc | p010le | True | open_or_encode_failed | — | 0 | — Mbps | — | libx265 | — |
| av1_nvenc | nv12 | True | open_or_encode_failed | — | 0 | — Mbps | — | libsvtav1 | — |
| av1_nvenc | p010le | True | open_or_encode_failed | — | 0 | — Mbps | — | libsvtav1 | — |

## detail

- h264_nvenc/nv12: PermissionError: [Errno 1] Operation not permitted: 'avcodec_open2("h264_nvenc", {})'
- hevc_nvenc/nv12: PermissionError: [Errno 1] Operation not permitted: 'avcodec_open2("hevc_nvenc", {})'
- hevc_nvenc/p010le: PermissionError: [Errno 1] Operation not permitted: 'avcodec_open2("hevc_nvenc", {})'
- av1_nvenc/nv12: PermissionError: [Errno 1] Operation not permitted: 'avcodec_open2("av1_nvenc", {})'
- av1_nvenc/p010le: PermissionError: [Errno 1] Operation not permitted: 'avcodec_open2("av1_nvenc", {})'

## 诚实边界

- 可用性裁决 = 真实 open + 编码 + 回解（P0-06）；同码率画质 A/B 与参数域（rate control/B 帧/lookahead）调优需真实素材，合成渐变素材码率不跑满，PSNR 只证码流 sane。
