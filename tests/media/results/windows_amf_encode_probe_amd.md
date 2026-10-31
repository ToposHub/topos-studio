# AMF 硬编导出实测（W6-02 AMD 半边，本机 RX 6800）

日期：2026-09-15T14:57:36 ｜ GPU：AMD Radeon RX 6800; AMD Radeon RX 6800; Microsoft Basic Render Driver ｜ PyAV 18.1.0

| 编码器 | 格式 | 注册 | 裁决 | 编码耗时 | 回解帧 | 实测码率 | PSNR(dB) | 基线 PSNR(dB) |
|---|---|---|---|---|---|---|---|---|
| h264_amf | nv12 | True | encoded | 199.7 | 30 | 0.4 Mbps | 57.35 | 92.12 |
| hevc_amf | nv12 | True | encoded | 207.4 | 30 | 0.4 Mbps | 66.84 | 93.74 |
| hevc_amf | p010le | True | encoded | 195.2 | 30 | 0.05 Mbps | 43.59 | 114.32 |
| av1_amf | nv12 | True | open_or_encode_failed | — | 0 | — Mbps | — | — |

## detail

- av1_amf/nv12: EncoderNotFoundError: [Errno 1129203192] Encoder not found: 'avcodec_open2("av1_amf", {})'