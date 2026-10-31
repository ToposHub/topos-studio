# W5-03 前置 probe：HDR 显示能力（NVIDIA RTX 4090 行）

日期：2026-09-12T11:50:47 ｜ 原始数据：`hdr_display_probe_nvidia_rtx4090.json`

| 输出 | BitsPerColor | ColorSpace | Min nits | Max nits | MaxFF nits | HDR 生效 |
|---|---|---|---|---|---|---|
| \\.\DISPLAY1 | 8 | RGB_FULL_G22_NONE_P709 (SDR sRGB) | 0.0 | 0.0 | 0.0 | False |

## 结论

- 本机显示器 **8bpc SDR**（RGB_FULL_G22_NONE_P709，无 HDR 元数据）：W5-03 的 HDR 呈现裁决在本机仍被显示器能力阻挡（诚实记录，K-W5）。
- vtable/IID 走查层在 NVIDIA 真机通过（GetDesc1 DeviceName 交叉验证 ok）。
- 与 AMD RX 6800 分册（HDR400 面板）互补：两台机器覆盖「有 HDR 显示器」与「无 HDR 显示器」两种验收前置。
