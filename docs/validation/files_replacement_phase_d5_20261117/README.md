# Files 替代 D5 批次验证记录（原生文件任务列表）

日期：2026-11-17。对应[总计划](../../plans/2026-11-17-files-final-replacement-and-upgrade-plan.md) D 表第 5 项。

## 交付物

| 层 | 内容 |
| --- | --- |
| transport | 首发 `task_started`（completed=0）附完整调用参数（targets/policy/options）——任务列表据此提供重试；进度事件只带计数 |
| 台账 | `FilesJobsRegistry`（FilesPanel 常驻，挂 transport 三信号）：进度快照、逐项成功/失败、取消/失败/完成状态、`retry_request` 重组调用参数、`clear_finished`；提交失败（无任务号）也记匿名事实 |
| UI | `JobsDialog`（注册表投影，可关/重开不丢账）：任务/进度/状态/逐项计数表、选中行失败原因摘要、取消（仅运行中）、重试失败项（仅终态有失败项）、清空已完成；入口在「更多工具 → 文件任务…」 |
| 归属 | 任务在 facade 服务执行；registry 由 FilesPanel 持有——切目录、关任务窗口、重开均不丢归属 |

## 验证

- `tests/ui/test_files_jobs_ui.py` **4 项**：台账进度/终态/重试参数/匿名失败/清空；
  关窗后台账照记；对话框渲染顺序（运行中在前）、取消/重试按钮门控与意图载荷；
  transport 首发事件带参、进度事件不带。
- 受影响面：**362 passed / 1 skipped**（D1-D4 全部 + 面板/位置/会话回归 + tests/files）。

## 诚实边界

- 「重试」= 对失败目标重发同一操作（原参数）；**「恢复」未做**——B3 journal 的
  崩溃恢复（pending_recovery）在服务启动侧，任务列表只呈现本次会话账目，
  跨进程任务历史/恢复 UI 未实现（依赖 B3 恢复入口的服务化）。
- 取消为协作式（条目/块边界生效）；同步任务在小批量下可能瞬间完成，
  取消入口自然失效（按钮门控已反映）。
- 本项记**部分**。

## 隐私

仅源码/测试/Markdown；合成素材与临时目录。

## 追加批次（同日）：B3 生产接线 + 恢复入口（第 5 项「恢复」缺口收口）

| 层 | 内容 |
| --- | --- |
| facade | 默认持有 `OperationJournal` 并注入任务/批改名服务——此前生产路径 journal=None 纯内存运行，崩溃零记录（B3 落地缺口） |
| 写路径 | `_transfer` begin 落**意图目的地**（恢复证据分类依据；策略改名后缀按存在性 best-effort） |
| 服务 | `FilesTaskService.pending_recoveries()`（job_log ACTIVE 态遗留）/ `recover(job_id)`（证据分类；无法判定保持待恢复） |
| transport | `recovery_loaded`/`recovery_applied` 信号 + `load_recoveries`/`recover_job` |
| 台账/对话框 | 待恢复态入账（含中断原因）、恢复按钮（仅待恢复可用）、判定落账（completed→done；failed→可重试；证据不足保持并展示分类原因） |
| 接线 | FilesPanel 原生分支启动即 `load_recoveries()`；恢复完成刷新面板 |

### 验证（追加）

- services：facade 默认 journal 终态落账（含意图目的地断言）；崩溃遗留→待恢复清单→磁盘证据分类（`tests/files` 20 项全绿，248 项全量两连绿）。
- UI：transport 恢复流（遗留→清单→判定）；台账/对话框待恢复行、恢复门控、证据不足保持（`test_files_jobs_ui.py` 6 项）。
- 组合回归 367 passed；早前两次全量各现 1 例不同时序偶发（单独与复跑均绿，与既往顺序脆弱记录一致，非本批归属）。

### 边界更新

- 「恢复」=B3 证据分类（completed/retriable/pending_recovery）；**跨进程任务历史浏览**未做（job_log 有界 200 条，trim 保留非终态；历史 UI 属可选后续）。
- 证据分类依赖意图目的地入账；策略改名的后缀路径不预判（分类按源/目标存在性 best-effort，证据不足如实保持待恢复）。
