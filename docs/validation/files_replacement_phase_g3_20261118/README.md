# G 阶段第三批（G3）：shutdown_timer 接管——独立 utility window

日期：2026-11-18 · 分支：fix-ui · 范围：`shutdown_timer_service.py`（新）+
`shutdown_timer_window.py`（新）+ 控制器原生命令表（短 id 键修正）。
G 节行验收：**启动只开窗口；系统动作有明确意图，取消/退出无遗留倒计时**。

## 新入口 / 新服务 / 关闭路径

| 部件 | 路径 |
| --- | --- |
| 新入口 | `launch("shutdown_timer")` → `_launch_native`（G1 三段 + F4 边界）→ `_open_shutdown_timer`（F5 singleton）→ `ShutdownTimerWindow`——**启动只开窗口**（idle 态、定时器未启动、零系统动作） |
| 新服务 | `ShutdownTimerService` 状态机 idle→counting→awaiting_confirmation：`arm(action, minutes)` 校验（未知动作/非正时长拒绝）；`tick()` 到零**进入待确认而非执行**；`confirm()` 才触发动作（明确意图三连：选择+启动、到零不自动、确认才执行）；`cancel()` 任意时刻回 idle 清零。系统动作宿主注入（macOS `osascript` 列表参数，无 shell 拼接；测试注入假动作绝不真执行） |
| 关闭路径 | 窗口 `closeEvent` → QTimer 停止 + `service.cancel()` → F5 销账；控制器 shutdown 同款——**无遗留倒计时/定时器** |
| 键修正 | 原生命令表改**短 id 键**（legacy 投影与命名空间合并两种控制器模式都能命中——G1/G2 的接线在 legacy 模式下此前实际未触发，本批修正后真实生效） |

## 验证

- `tests/ui/test_shutdown_timer_g3.py`（8 项）：arm 校验；60 tick 到零
  **待确认且零执行**；confirm 执行一次后 idle、再点如实拒绝；任意时刻
  cancel 清零（含到零后取消不可再确认）；idle tick 空操作；窗口打开
  不启动任何东西；倒计时到零出「立即执行」且确认才执行；关闭停表清零。
- `tests/ui/test_native_files_plugins.py` +1：控制器端到端——原生打开
  （启动 idle）、singleton 同实例、**零 HTTP**、注册表关闭销账；
- 既有测试口径迁移：legacy HTTP 固定用例（auth 快照/服务失败/dispatch
  拒绝/F6 作业）从已被接管的 format_factory/image_factory 迁至
  decode_benchmark（接管证明：旧工具不再走 /launch）。

回归：G1-G3 + plugins + files + native UI —— **807 通过**（一次
plugin_batch2 顺序抖动为仓库既有类，隔离绿）。

## 诚实边界

- 系统动作本体（osascript 睡眠/关机/重启/锁屏）**测试从不真执行**
  （注入假动作）；真机动作执行归平台矩阵验收。
- 非 macOS 平台动作为显式空集（窗口显示但无可选动作——如实降级）。
- 倒计时为分钟粒度整秒推进（无秒级 arm）；单实例（无多定时器）。

## 隐私

内存状态机 + 假动作；无隐私路径。
