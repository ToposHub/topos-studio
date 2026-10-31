# M18 · 机器 B（Windows）复验接入包（用户已确认接入）

用户于 2026-10-25 会话确认**接入 Windows 机器 B** 执行 M18 Windows
复验与 Windows 侧安装包 ABI 验证。本目录是 B 机的一键接入包。

## 接入步骤（机器 B 上）

1. **检出同源代码**：将本仓库检出到与 Mac 侧同一提交（`git log -1`
   的哈希需与 `report/machine_b_report.json` 生成时 Mac 侧 HEAD 一致；
   或按 deferred_gates.md 的复开动作登记 B 机实际提交）。
2. **环境**：Python 3.12+，`python -m venv .venv &&
   .venv\Scripts\pip install -r requirements.txt`（无 requirements 时
   按 `pip install -e .` / 既有引导脚本）。
3. **一键复验**（含环境指纹、聚焦门禁、黄金链 3、审计探针、
   Windows 侧 PE ABI）：

   ```
   python scripts/m18_machine_b_recheck.py --dist <Windows打包目录>
   ```

   `--dist` 指向 Windows 侧 PyInstaller 产物目录（含主 `.exe`）；
   未打包时可省略 `--dist`（PE ABI 段跳过，其余照跑）。

## 复验清单（与 deferred_gates.md 对齐）

| 门 | 复开动作 | 脚本覆盖 |
|----|----------|----------|
| Windows 复验（核心域回归） | B 机跑聚焦门禁+探针 | `focused_gate`/`probe_s`/`probe_r` |
| Windows 侧安装包 ABI | B 机对 dist 产物 PE 审计 | `pe_abi_audit`（架构一致性+sha256 manifest） |
| 黄金链 3（多画幅多语言交付+工程恢复） | B 机重跑端到端 | `golden_chain3` |
| 实机往返（FCPXML 外部软件生成/接收） | 需外部软件——本接入包不覆盖，仍按 deferred_gates | — |
| AAF/DeckLink/RIFE/长压 | 需 SDK/设备/样片/交互 session——仍按 deferred_gates | — |

## 结果口径

- `report/machine_b_report.json`：`overall=PASS` 即本接入包覆盖的
  Windows 复验门通过；任一 FAIL 如实保留输出尾部供排查。
- `report/windows_bundle_abi_manifest.json`：逐文件架构+sha256
  （与 Mac 侧 `bundle_abi/macos_bundle_abi_manifest.json` 同 schema）。
- 实机往返/AAF/DeckLink/RIFE/长压完成后，由 B 机或 Mac 侧将证据
  追加登记至 deferred_gates.md 对应条目后闭环 M18。
