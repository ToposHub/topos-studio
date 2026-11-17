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
