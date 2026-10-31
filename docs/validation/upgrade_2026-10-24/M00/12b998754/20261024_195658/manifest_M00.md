# M00 交付 manifest · 冻结真实完成状态与回归门禁

> HEAD：`12b998754`；执行宿主：Darwin x86_64 / Python 3.12.13（项目 .venv）；日期：2026-10-24。
> 对应：升级计划 M §M00（P0）；对应旧账 O01/O02、F0/F1。

## 1. 逐任务差量表（冻结完成状态）

交付物：[f_plan_gap_table.json](f_plan_gap_table.json)（53 项全量，机器可读）。

- F 计划 45/53 勾选、8 项开，与台账一致，未重写任何历史状态。
- 45 个勾选按完成层级分桶：**核验收口 8**（F0.x/F1.x，按其自身审计定义收口）、**生产已接线 30**、**仅模型/API 层闭环 7**（F4.3/F4.4/F8.1/F8.2/F8.3/F9.1/F9.3——勾选不代表用户功能验收，其 UI/正式 IO/成片消费者为后续片）。
- 8 项开：F5.2（设备）、F6.3（aaf2+接收端两门未开）、F10.1–F10.6（显式立项）——维持延期，未改成通过。
- S01–S12 十二项共享正确性缺口叠加在勾选项之上（如 F4.1 已勾但 S08 顺序错误、F2.10 已勾但 S05/S06 成片音源不一致），逐项标注了新归属（M02–M12），与 M 计划 §1/§3 一致。
- 旧 A/B/F manifest 与证据原样保留，未删除、未重写为全绿；本表只做分桶登记。

## 2. 复现基线（当前 HEAD）

| 套件 | 结果 | 证据 |
|---|---|---|
| 12 个 focused 文件单进程 | **142 passed / 6.50s，exit 0** | [focused_baseline.log](focused_baseline.log) |
| 分域子进程门禁（5 域） | **全绿（edit_core/edit_ai/deliver/color/shared 均 exit 0）** | [gate_report.json](gate_report.json) |

与 2026-10-24 审查轮（`9e05556c3`，142 passed / 7.48s）一致：零漂移。

## 3. 回归门禁（真实阻断）

- 新增 [scripts/m00_focused_gate.py](../../../../../scripts/m00_focused_gate.py)：12 个 focused 文件按 pytest 目录分 5 域，**每域独立子进程**（域内共享 Qt/解码器状态、域间完全隔离，单域挂起只超时该域）；**任何域非零失败/超时 → 门禁 exit 1**，明确不做"与旧基线失败集合相等"放行；不支持把新增失败改 skip（门禁只认零失败）。
- 新增 CI：[.github/workflows/focused-logic-gate.yml](../../../../../.github/workflows/focused-logic-gate.yml)（ubuntu+macos 矩阵、干净安装 requirements.txt、跑门禁并上传报告），覆盖关键 src 路径触发。
- 本机验证：`python scripts/m00_focused_gate.py --report ...` → `GATE PASS`（5 域全绿，总耗时 ~14.5s）。

## 4. 现有失败/缺口登记

交付物：[known_failures.json](known_failures.json)。登记：S01–S12（宿主=Mac 审查复现，owner=M02–M12）、M-I01 依赖冲突（owner=M01）、M-I02/M-I03 历史性能记录（owner=M16/M17，复测优先级不冒充实测）、机器 B 全量基线失败集合引用（tests/media 1135 ID / tests/edit 69F / composite 4 pre-existing，Mac 全量复跑归 M16.3/M18，不在 M00 重推导）、F 计划 8 项开（条件延期）。

## 5. 文档头与旧状态冲突校正

- 本 manifest 冻结 v2 计划编制时点的"全部工作包未开始"为开工期历史事实；自本轮起各包完成状态以 M 计划文档勾选 + 各包 manifest 为准（M00 本包已勾选并附本 manifest）。
- 未修改任何旧 A/B/F manifest；`docs/upgrade_plan_M_mac_2026-10-24.md` 仅更新 M00 勾选与修订记录。

## 6. 完成定义核对

- [x] 核对 A/B/F 总表 45 勾选及台账，四桶分列（audit_closed/production_wired/api_only/conditional_deferred + not_started）
- [x] 输出逐任务差量表（机器可读 JSON）与复现基线（单进程 + 分域门禁双记录）
- [x] focused suites 设真实阻断（门禁脚本 + CI），非零失败不能靠与旧基线相等放行
- [x] CI 分域/子进程，避免 Qt/C 解码器累积挂起
- [x] 登记现有失败 ID、宿主、依赖与责任人
- [x] 保留原提交和证据，未删历史、未重写为全绿

## 7. 未验收项/边界

- 本机未跑全量 tests/media、tests/edit、tests/edit UI/GPU/真实窗口套件（归 M16.3 基线与 M18 门禁按需执行）；本 manifest 引用机器 B 既有基线仅为登记，不代表 Mac 实测。
- CI 门禁在本仓库远端触发，本轮未在远端观察到运行结果（本地同命令已验证）。

## 回滚

门禁脚本与 CI 为新增文件，删除即回滚；无产品代码修改，无 schema 变更。
