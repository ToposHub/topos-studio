# W1-03 适配器匹配实测（amd_6800）

日期：2026-09-15T14:57:11 ｜ 原始数据：`adapter_match_probe_amd_6800.json`

| 适配器 | vendor | VID/PID | LUID |
|---|---|---|---|
| AMD Radeon RX 6800 | amd | 0x1002/0x73BF | 000111A0-00000000 |
| AMD Radeon RX 6800 | amd | 0x1002/0x73BF | 023C407C-00000000 |
| Microsoft Basic Render Driver | unknown | 0x1414/0x008C | 00013C96-00000000 |

## 规则验证

- 相同 LUID：match=True（放行）
- 不同 LUID：match=False，reason=`adapter_mismatch:00010000-00000000|000111A0-00000000`
- render 侧快照（未探测）：reason=`render_adapter_unprobed`——WGL 下无标准途径查询 render adapter LUID，运行时由 interop 层传入，不猜。

## 结论

- 本机 decode 侧主适配器：AMD Radeon RX 6800（LUID `000111A0-00000000`）；枚举中另含 iGPU（Intel UHD 770）与 Basic Render Driver。
- 同名适配器出现多个不同 LUID 条目（DXGI 枚举行为），适配器匹配必须按 LUID 而非描述字符串裁决。
- iGPU+dGPU 桌面机形态下匹配规则验证通过；真 Optimus 笔记本（动态切换 render GPU）仍留待真机复核。
