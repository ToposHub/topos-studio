# HDR / 高级色显示能力探针（W5-03 前置，本机）

- 日期：2026-09-12 ｜ GPU：AMD Radeon RX 6800 ｜ OS：Win11 26200
- 链路：ctypes DXGI 走查——`CreateDXGIFactory1 → EnumAdapters → EnumOutputs
  → QueryInterface(IID_IDXGIOutput6) → GetDesc1`（vtable[27]，槽位按本机
  SDK 10.0.26100 头文件方法序钉死；GetDesc 与 GetDesc1 的 DeviceName 交叉
  验证 `ok`，脚本 `scripts/probe_hdr_display_windows.py`）。

## 结论

**本机显示器具备 HDR 能力（HDR400 级），当前处于 SDR 模式。**

| 字段 | 实测 | 含义 |
|---|---|---|
| BitsPerColor | **10** | 显示管线 10-bit |
| ColorSpace | 0（RGB_FULL_G22_NONE_P709） | 当前 SDR sRGB 模式（Windows「使用 HDR」关闭） |
| MinLuminance | 0.29 nits | HDR 静态元数据可达 |
| MaxLuminance | 400 nits | HDR400 级面板 |
| MaxFullFrameLuminance | 400 nits | 全屏峰值 |
| Primaries | R(0.670,0.314) G(0.327,0.640) B(0.136,0.069) W(0.3125,0.328) | 接近 sRGB/Rec.709 |
| 适配器 | AMD Radeon RX 6800（0x1002） | 输出 \\.\DISPLAY1，monitor 0x10001 |

## 对 W5-03 的裁决输入

1. **W5-03（scRGB vs HDR10 裁决 + HDR 呈现端到端验收）不再被"无 HDR 显示器"
   阻挡**：本机显示器 HDR 元数据完整可达，开启 Windows「使用 HDR」开关后
   ColorSpace 应变为 `RGB_FULL_G2084_NONE_P2020(12)`，可做完整 HDR 呈现验收
   （复测本探针 + HDR10 swapchain 呈现验证）。
2. 面板为 HDR400（400 nits）：够做 HDR 呈现正确性验证，但**不能**作为
   高亮度 HDR 观感结论的依据（W5-03 的画质结论需注明面板等级）。
3. 诚实性边界：ColorSpace=0 只反映当前开关状态，不证明面板无 HDR 能力；
   本次以静态元数据（Min/MaxLuminance、10bpc）为能力证据。
