# 修复报告 wd1-g2 — 批 D（AI 编辑控制器域，Major × 18）

- 日期：2026-09-29
- 证据文档：docs/audit/2026-09-28/batches/batch-020-app-controllers.md、
  batch-021-app-controllers.md、batch-022-app-controllers.md、
  docs/testcoverage/2026-09-28/GAP_REPORT.md
- 改动文件（全部在白名单内）：
  - `src/app/controllers/ai_edit_controller.py`（AUD-2001/2002/2003/2004/2005/2006/2007/2008）
  - `src/app/controllers/ai_edit_host_bridge.py`（AUD-2100）
  - `src/app/controllers/ai_edit_host_bridge_impl.py`（AUD-2103/2104/2105）
  - `src/app/controllers/ai_editing_mixin.py`（AUD-2200）
  - `src/app/controllers/ai_workflow_composite_render.py`（AUD-2210/2211/2212/2213）
- 测试改动：
  - 新增 `tests/app/test_fix_wd1g2_aud.py`（30 用例，全绿；同时是 GAP-010 首批补测）
  - `tests/app/test_ai_command_stack_commit.py`、`tests/app/test_ai_e2e_deterministic.py`、
    `tests/edit/ai_editing/test_workflow_runner.py`：3 处 bridge 构造补
    `legacy_no_policy=True`（锁定旧行为协议，见 AUD-2105 说明）
- 本任务约束：禁止 git 分支操作/commit/push、禁止修改 REMEDIATION_PLAN.md——
  逐条结论只记于本报告；REMEDIATION_PLAN.md 未做任何改动（保持 HEAD 原状），
  由 orchestrator 在记账环节统一勾选。

## 逐条结论

| 编号 | 结论 |
|------|------|
| AUD-2001 | **FIXED** |
| AUD-2002 | **FIXED**（按证据主方案：非 chat 分支 return 前清空；MG/Teaching 分支保留——设计内文件交接 + 既有测试锁定，见说明） |
| AUD-2003 | **FIXED** |
| AUD-2004 | **FIXED** |
| AUD-2005 | **FIXED** |
| AUD-2006 | **FIXED** |
| AUD-2007 | **FIXED** |
| AUD-2008 | **FIXED** |
| GAP-010 | **FIXED（部分/首批）**——30 例定向补测，控制器覆盖 39%→42%，缺口未闭合 |
| AUD-2100 | **FIXED** |
| AUD-2103 | **FIXED** |
| AUD-2104 | **FIXED** |
| AUD-2105 | **FIXED**（证据首选方案：显式注入替代隐式 fail-open） |
| AUD-2200 | **FIXED** |
| AUD-2210 | **FIXED** |
| AUD-2211 | **FIXED**（引用快照方案，完整数据快照不可行——见说明） |
| AUD-2212 | **FIXED**（证据备选方案：写入投递回 GUI 线程——GenerationBin 不在白名单，见说明） |
| AUD-2213 | **FIXED** |

---

## AUD-2001（Major/bug）`_start_chat` 多模态校验失败早退遗留 running 活动项

**状态：FIXED**

**根因**：`_start_chat` 在 2076-2080 的附件校验失败分支直接 `return`，既不
`_finish_legacy_chat_activity` 也不 `_discard_legacy_chat_activity`；对照
`profile is None` 分支（经 `_on_chat_failed` → `_finish_legacy_chat_activity
(status="failed")` 收尾）不对称。`_legacy_chat_activity_item` 永远停留
`status="running"`、`can_cancel=True`。

**修复**（ai_edit_controller.py `_start_chat`）：早退分支补
`self._finish_legacy_chat_activity("已阻止", status="cancelled")`。附件保留在
`_pending_chat_attachments`（供隔离 artifact 请求的模块重试消费——模块复用
同一 request_id 重试，重试前用户可切换 language 路由 Profile；此为旧行为
不变量，与 AUD-2002 的清理互补）。

**测试**：`test_aud_2001_blocked_multimodal_early_exit_finishes_activity`
（断言 status=cancelled、can_cancel=False、终态发布到 panel.set_run_drawer、
附件保留重试）。

## AUD-2002（Major/risk）`_pending_chat_attachments` 跨请求残留

**状态：FIXED（按证据主方案 + 边界说明）**

**根因**：`_on_request_submitted` 无条件暂存附件（:953），媒体生成 / 插件 /
router 非聊天分支不经 `_start_chat` 直接 return，附件既被忽略又残留；之后
feature 模块调 `start_html_generate_chat` / `start_motion_graphic_build_chat`
时 `_start_chat` 把陈旧附件当本次请求的附件消费（draft_attachments + 重新
注册 MCP 图片），违反 526-527 行声明的"避免跨请求残留"不变量。

**修复**（ai_edit_controller.py）：
1. 媒体模式分支（`get_media_assistant_modes`）return 前清空暂存（证据主方案）。
2. Assistant 插件非 `generic_chat` handler 分支清空。
3. router 分发返回后：只有 unknown → `_start_chat` 分支消费附件（消费即清空），
   其余非聊天出口统一在调用点清空。
4. 无 router 回退 `_run_legacy_brief_path` 前清空（brief/plan 路径不支持多模态）。
5. 暂存区声明处补不变量注释。

**关键设计取舍（与证据"所有非 chat 分支"的差异，需说明）**：
Motion Graphic / Teaching 分支**保留**暂存，不清空。理由：
(a) 附件→隔离 artifact 请求是**设计内的文件交接**——用户拖入文件提交 MG 请求，
模块授权后经 `start_html_generate_chat` 消费，回归测试
`tests/ui/test_p1_chat_free_conversation.py::test_isolated_html_generation_keeps_dropped_files_in_model_message`
**锁定**该行为（隔离 `_start_chat` 必须消费暂存附件），全量清空会破坏该测试
与产品功能；
(b) 残留的生命周期被结构性封顶：任何新提交在 :953 整体覆写暂存区，非隔离
`_start_chat` 调用点（1037/1080/1115/1250）全部位于提交流内（覆写之后），
隔离调用点不再能读到与本流程无关的附件（见 AUD-2001 保留的重试语义除外）。
即残留最迟在下一次提交时被覆写，无界泄漏面已消除。

**测试**：`test_aud_2002_media_mode_submission_clears_pending_attachments`、
`test_aud_2002_routed_non_chat_submission_clears_pending_attachments`、
`test_aud_2002_legacy_brief_path_clears_pending_attachments`；
`tests/ui/test_p1_chat_free_conversation.py` 全绿（设计内交接未破坏）。

## AUD-2003（Major/bug）cancel 路径不终结 V2 Motion Graphic run

**状态：FIXED**

**根因**：`_on_chat_cancelled` 的 `teaching_html_generate` 分支只调 handler
兜底，不调 `complete_motion_graphic_run`；对照 `_on_chat_failed`（:3974 调用）
不对称，`_v2_mg_runs[request_id]` 的 AgentRun 永久 running（幽灵行）。

**修复**：cancel 分支在 handler 通知前补
`self.complete_motion_graphic_run(request_id, success=False)`（与 fail 路径
顺序一致）。

**测试**：`test_aud_2003_chat_cancelled_completes_motion_graphic_run`（调用
契约）、`test_aud_2003_chat_cancelled_pops_v2_mg_run_mapping`（端到端映射清除）。

## AUD-2004（Major/risk）dispose 不清 worker 引用/不断信号/不移除订阅

**状态：FIXED**

**根因**：dispose 的 wait(2000) 超时后，`_chat_worker`/`_plan_worker`/
`_subtitle_plan_worker` 引用未清、lambda 信号未断、runtime 订阅未移除；
排队信号在 dispose 后投递，身份检查 `worker is not self._chat_worker` 依旧
通过，`_on_chat_cancelled`/`_on_chat_finished` 继续向**共享 panel** 发布
旧会话状态。

**修复**（ai_edit_controller.py）：
1. 新增 `_retire_worker(worker, *, wait=True)`：协作取消 → 自定义信号全量
   断连（`finished_plan/finished_reply/finished_payload/stream_delta/failed/
   cancelled`；保留 `finished → deleteLater` 释放链）→ 超时仍在运行则
   `_detach_running_worker`。
2. dispose：三个 worker 走 `_retire_worker`，随后显式置 `self._plan_worker/
   _chat_worker/_subtitle_plan_worker = None`（身份检查丢弃迟到信号）。
3. `__init__` 保存 `runtime.add_subscriber` 返回的退订句柄
   `self._runtime_unsubscribe`；dispose 调用移除订阅（迟到 run 事件不再经
   `_run_state_changed` 驱动旧 controller）。

**补充（超出证据的最小要求，为正确性必需）**：`_detach_running_worker` 把
超时 worker 摘除 Qt parent（`setParent(None)`）并放入模块级
`_DETACHED_WORKERS` 集合，`finished` 时移除。原因：dispose 断开全部信号后，
controller 可被 GC，Qt 父子关系会把仍在运行的子 QThread 一并销毁
（"QThread: Destroyed while thread is still running" 崩溃）。detached 集合
保证 worker 存活到 run() 自然结束。

**测试**：`test_aud_2004_dispose_clears_worker_refs_and_drops_late_signals`、
`test_aud_2004_dispose_removes_runtime_subscriber`。

## AUD-2005（Major/risk）worker 以 parent=self 创建且从不 deleteLater

**状态：FIXED**

**根因**：`_ChatWorker`/`_PlanWorker`/`_SubtitlePlanWorker` 的 run() 结束后
对象不销毁，`_messages`（多模态消息含 base64 图片 data URL，单附件可达数
MB）与 service/index/state 引用随 worker 存活到 controller 死亡，长会话
内存无界增长。

**修复**：四个创建点统一 `worker.finished.connect(worker.deleteLater)`
（_ChatWorker、_PlanWorker、_SubtitlePlanWorker，以及同为该模式的 V2
`AgentV2ChatWorker`——与 MG worker 既有正确做法对齐）。信号发出顺序保证
自定义信号（queued）先于 finished→deleteLater 送达。

**测试**：`test_aud_2005_plan_worker_delete_later_on_finish`、
`test_aud_2005_chat_worker_delete_later_on_finish`（行为级验证：wait 后
processEvents，QObject destroyed 触发）。

## AUD-2006（Major/bug）Run Drawer 取消按钮对 V2 chat run 无效

**状态：FIXED**

**根因**：`_on_run_cancel_requested` 只调 `HarnessRuntime.cancel`，V2 run 不
在 runtime 中（返回 False 且被吞），无 `_v2_orchestrator.request_cancel`
分支——V2 drawer 条目 `can_cancel=True` 但点击无效。

**修复**：runtime.cancel 返回 False（或抛错）时分派
`self._v2_orchestrator.request_cancel(item_id, reason="用户取消")`（按 run
归属分派；编排器对未知/终态 run 安全返回 False）。runtime 命中时直接返回，
不误触发 V2。

**测试**：`test_aud_2006_run_cancel_dispatches_to_v2_orchestrator`、
`test_aud_2006_run_cancel_runtime_hit_skips_v2`。

## AUD-2007（Major/risk）`_SubtitlePlanWorker` 后台线程读 live ProjectSequences

**状态：FIXED**

**根因**：bridge 的 `get_project_sequences` 仅浅拷贝顶层 dict，project 与
Sequence/Track 是活引用；`SubtitleEditPlanner.plan` 在 QThread 中经
`build_snapshot` 迭代 `project.assets` / `sequence.all_tracks`（LLM 调用前后
多次读取，历时数秒），与主线程时间线编辑并发可触发
"dict changed size during iteration" 或半更新脏读。

**修复**（ai_edit_controller.py）：
1. 新增模块级 `_deep_copy_state_for_worker(state, timeline_id)`：主线程深拷贝
   目标 timeline + project（工程模型均为 frozen dataclass 纯数据，无 Qt/锁
   成员），构造只含目标序列的最小 `ProjectSequences`。
2. `_start_subtitle_edit_agent` 非 TOPOS_TEST_MODE 路径传深快照给 worker；
   拷贝失败按确定性失败上报（`subtitle_snapshot` clarification），不退回
   活引用静默恢复竞态。测试模式同步路径保持 live state（单线程无竞态，
   旧测试契约不变）。

**测试**：`test_aud_2007_deep_copy_state_for_worker_is_private_snapshot`
（隔离性/最小性/抗变异）、
`test_aud_2007_subtitle_worker_receives_snapshot_not_live_state`（wiring）。

## AUD-2008（Major/perf）dispose 对 MG 后处理 worker 串行 wait(2000)

**状态：FIXED**

**根因**：`_MotionGraphicPostprocessWorker.cancel_requested` 只在 processor
返回后检查（Chromium 不可中断），`wait(2000)` 几乎必然超时；N 个并发请求
时工程切换主线程串行冻结 2s×N。

**修复**：dispose 把 MG worker 从等待循环拆出，`_retire_worker(worker,
wait=False)`：置 cancel 标志、断开 controller 侧槽位、`_detach_running_
worker` 摘除 parent 由 `_DETACHED_WORKERS` 持有到自然结束（deleteLater +
集合回收）。plan/chat/subtitle worker 保持协作 wait(2000)（至多 3 个，且
HTTP 返回后可协作终止，非本条范围）。

**测试**：`test_aud_2008_dispose_does_not_block_on_mg_postprocess_worker`
（真实线程：dispose 耗时 <1.5s、cancel 标志置位、parent 摘除、detached 集合
在 run() 结束后回收）。

## GAP-010（Major/test-gap）ai_edit_controller.py 39% 覆盖、缺失 2199 行

**状态：FIXED（部分/首批）**

本批以 30 个定向用例（`tests/app/test_fix_wd1g2_aud.py`）覆盖批 D 修复面
（聊天生命周期 / dispose / worker 生命周期 / 取消路由 / 字幕规划快照 /
bridge 策略与导出路径 / composite render provider）。量化（pytest-cov，
与既有 controller 套件合并）：**39%（缺失 2199/3594）→ 42%（缺失
2147/3703）**，缺失行净减且新修复代码同步入测。2199 行的整体缺口无法在
单批内负责任地闭合；剩余区域（自主 agent、媒体生成、result card、
auto-caption 等）留待后续补测批次。

## AUD-2100（Major/bug）NullAiEditHostBridge 缺协议方法

**状态：FIXED**

**根因**：协议声明了 `detect_autonomous_replay` / `rollback_ai_command`，
Null 实现全类无对应方法——`isinstance(NullAiEditHostBridge(), AiEditHostBridge)`
（@runtime_checkable）为 False；静态检查报协议成员缺失；新增调用点
（autonomous_agent.py 直接调用，无 getattr 兜底）即 AttributeError。

**修复**（ai_edit_host_bridge.py）：补两方法。`detect_autonomous_replay`
返回 `{"known": False}`（未装配宿主不可能有已提交计划，与生产实现
"无法证明"分支同语义，修订门 fail-closed）；`rollback_ai_command` 返回
None（无法证明 undo 目标，调用方保守中止）。

**测试**：`test_aud_2100_null_bridge_implements_full_protocol`（isinstance
协议一致性 + 两方法返回值）。

## AUD-2103（Major/bug）AI 导出未绑定已验证的 timeline_id

**状态：FIXED**

**根因**：预检、字幕安全区测量、品牌合同、LUFS/静音门全部按
`scope.timeline_id` 计算，而宿主 `perform_video_export` 编码的是
`edit_page._current_timeline`（settings 不携带 timeline_id）；
`state.sequences` 是全工程多时间线字典，两条时间线可不同——质量合同与
实际成片脱节。

**修复**（ai_edit_host_bridge_impl.py `request_ai_export`）：在 Job 提交前、
策略检查后校验 `edit_page._current_timeline.timeline_id == timeline_id`
（与 `commit_ai_command` 的一致性校验同构），不一致返回确定失败
"目标时间线未在编辑页打开…"，宿主导出器不被调用。

**测试**：`test_aud_2103_export_rejected_when_target_timeline_not_active`。

## AUD-2104（Major/bug）AI 导出 Job 在宿主静默取消路径永久 running

**状态：FIXED**

**根因**：仅"抛异常"会 fail；宿主 `perform_video_export` 存在多个不抛异常
的提前 return（无时间线 QMessageBox+return、config.validate 失败、用户拒绝
DeliveryMatrixDialog、已有导出进行中），`_post_export` 只在编码 worker 成功
前被调用——job 已 `mark_running()` 但永不终态。

**修复**：以宿主的导出启动状态为接受信号（真实宿主接受路径写
`_video_export_started_at`（deliver_controller.py:1048）与 `_video_exporter`
（:1056））：调用前后对比两个观测点，均未变化 → 宿主未接受 →
`job.fail("EXPORT_NOT_STARTED", …)`（`job.status.is_active` 守卫，避免理论
上的同步完成路径覆盖终态）并返回确定失败。宿主抛异常路径行为不变。

**测试**：`test_aud_2104_export_job_fails_when_host_silently_declines`
（job FAILED + EXPORT_NOT_STARTED）、
`test_aud_2104_export_accepted_when_host_starts_export`（接受路径不受影响）。

## AUD-2105（Major/risk）`check_ai_action` 缺 feature_flags 时 fail-open

**状态：FIXED（证据首选方案：显式注入）**

**根因**：无工程 + 宿主缺 `feature_flags` 时返回
`PolicyDecision(True, "legacy host has no policy context")`——为兼容 legacy
transaction 测试的最小 fake 在生产代码里留下的隐式 fail-open 后门；任何
未设置该属性的宿主（无头宿主、装配不完整的 fake、未来 CLI 宿主）对所有
动作（含 paid_generation）直接放行。

**修复**（证据建议方案一）：构造参数 `legacy_no_policy: bool = False`；
`check_ai_action` 删除 `feature_flags is None` 隐式放行，缺工程一律
`PolicyDecision(False, "no active project")`，仅显式声明
`legacy_no_policy=True` 的测试 fake 保持旧放行。

**既有测试更新（锁定旧行为协议，共 3 处构造点）**：
`tests/app/test_ai_command_stack_commit.py`（×2）、
`tests/app/test_ai_e2e_deterministic.py`（×1）、
`tests/edit/ai_editing/test_workflow_runner.py`（×1——同文件 1 处）。这些测试
的 fake 宿主不建模 MainWindow/feature_flags 且依赖 commit 路径的策略放行；
其聚焦点是 CommandStack 提交/undo 闭环而非策略门，因此按协议显式传入
`legacy_no_policy=True` 并加注释，而非为 fake 补建模 feature_flags（后者会
把测试变成策略测试，偏离原目的）。生产 MainWindow 恒有 feature_flags，
行为不变（`tests/app/test_ai_action_policy_boundary.py` 全绿）。

**测试**：`test_aud_2105_policy_fails_closed_without_legacy_opt_in`、
`test_aud_2105_legacy_no_policy_opt_in_keeps_test_fakes_working`。

## AUD-2200（Major/bug）装配失败的外层 except 不停用 MCP host / facade / providers

**状态：FIXED**

**根因**：MCP host 已 `start()`（本地 Unix socket 对外服务）、ToolFacade /
generation providers / comfy executors 已赋值后，后续装配抛异常只置 5 个
`None`——半拆除状态下 socket 继续服务，facade 持有的 session/analysis/
approval 引用指向已置 None 的装配，外部 stdio 客户端可在该窗口调用工具。

**修复**（ai_editing_mixin.py）：外层 except 复用 `self._teardown_ai_editing()`
（stop MCP host、dispose controller、close generation service、撤 grants、
unbind EditSession——错误路径与正常 teardown 资源状态一致），随后补清
teardown 不覆盖的 `_generation_providers` / `_comfy_generation_executors` /
`_comfy_profile_registry`，再保留原有 5 个显式置 None（teardown 失败时的兜底）。

**测试**：`test_aud_2200_setup_failure_path_runs_full_teardown`（在
controller 已建、MCP host 已 start 的装配链尾部注入失败，断言 teardown 被复用
+ providers/executors 清空 + 5 个引用置 None）。

## AUD-2210（Major）`_render_single_frame` 不调 cleanup()

**状态：FIXED**

**根因**：每次渲染新建 `CompositeFrameRenderer`（自建 worker 线程 GL
context + async PBO + 纹理池），从不调 `cleanup()`，GL 资源依赖 GC 释放——
多次渲染后显存/句柄累积，违反项目 GPU 资源显式释放铁律。

**修复**：`try/finally` 包裹 `render_comp_clip_frame`，finally 中
`renderer.cleanup()`（cleanup 自身异常只记日志，不掩盖渲染结果/异常）。

**测试**：`test_aud_2210_render_single_frame_calls_cleanup_always`（成功与
异常路径均调用）。

## AUD-2211（Major/risk）worker 线程活读 `main_window._current_edit_project/_current_timeline_sequences`

**状态：FIXED（引用快照方案——完整数据快照不可行，见说明）**

**根因**：provider 在 worker 线程 `getattr(main_window, "_current_edit_project"/
"_current_timeline_sequences")`；渲染期间主线程切换/关闭工程会把 worker 的
输入换成新引用或 None（素材错配/渲染失败），且主线程并发变异 project dict
存在竞态——违反模块 docstring 的"worker 只消费 dict 快照"红线。

**修复**（ai_workflow_composite_render.py）：
1. 新增 `_CompositeRenderContext`（edit_project/timeline_sequences/
   config_manager 引用快照）；`make_exposed_input_resolver` 的 resolver
   （主线程）在填充 `composite_graph` 快照时一并捕获三引用，存于
   `inputs["composite_graph"]["render_context"]`（该 dict 已是 typed 信封，
   `to_public_dict` 不序列化 inputs，无持久化污染）。
2. provider（worker）只读 `render_context` 引用；快照缺上下文（resolver
   未运行的旧 invocation 形态）→ 诚实降级 None，**不回退活读**（回退会把
   红线违规重新引入生产路径）。
3. 主窗口状态在 worker 启动后变化不再影响进行中的渲染：引用快照随
   invocation 存活（Python 引用计数保证旧工程对象不被中途回收）。

**方案说明（与证据"不可变数据一并快照"的差异）**：edit_project/
timeline_sequences 是大而深的活对象图（资产、轨道、clip、字幕 cue），
完整深拷贝在主线程的开销与对象图可达性（渲染器按需遍历）使"全量不可变
快照"不可行；引用快照消除的是本条的核心危害——**worker 对主窗口可变
属性的活读**（错配/中途 None/跨请求换图）。主线程对旧工程 dict 的并发
变异风险与修复前相同（未被本条证据认定），如需彻底消除须在工程模型层
引入不可变化构建（超出白名单与本条范围）。

**测试**：`test_aud_2211_resolver_captures_render_context_references`、
`test_aud_2211_provider_reads_snapshot_not_live_window`（主窗口切换工程后
worker 仍用旧引用）、`test_aud_2211_provider_degrades_without_render_context`。

## AUD-2212（Major/risk）`gen_bin.add_candidates` worker 线程无锁写入

**状态：FIXED（证据备选方案：投递回 GUI 线程——GenerationBin 不在白名单，见说明）**

**根因**：`GenerationBin` 为无锁 dict/list；worker 线程 `add_candidates`
与 GUI 线程 `all_assets/by_state` 的生成器遍历并发时，dict resize 抛
"dictionary changed size during iteration"。

**方案说明**：证据首选方案（GenerationBin 内加 threading.Lock）需要修改
`src/features/edit/ai_editing/generation_bin.py`——不在本批白名单内，故采用
证据给出的备选方案："把 worker 产物经信号/队列投递回 GUI 线程再入箱"。

**修复**（ai_workflow_composite_render.py）：
1. `_make_bin_insert_marshaler(main_window)`（**GUI 线程调用**，wiring 阶段
   即 `make_composite_render_provider` 构造时）：非 Qt 宿主返回 None。
   内部 QObject 持 `pyqtSignal(object)`，AutoConnection 连接自身槽
   `_insert`（执行 `gen_bin.add_candidates((asset,))`）。
2. provider 在 worker 线程 `emit((gen_bin, asset))`——Qt 自动以 queued
   connection 切回 marshaler 所属（GUI）线程执行，写入与 GUI 读侧天然
   串行。时序安全：入箱信号早于 run 终态事件与任何用户交互投递。
3. headless（无 QCoreApplication / 非 QObject 宿主）回退同步写入（无并发
   读侧）。
   注：最初尝试 `QMetaObject.invokeMethod` functor 重载——PyQt6 6.10 不支持
   （PySide 专属），已改用信号方案（仓库内零依赖、语义相同）。

**测试**：`test_aud_2212_candidate_insertion_is_queued_to_gui_thread`
（真实 worker 线程 emit：处理事件前 bin 为空、processEvents 后写入生效）、
`test_aud_2212_headless_host_falls_back_to_synchronous_insertion`。

## AUD-2213（Major/bug）provider 无异常防护，int() 与渲染异常逃出契约

**状态：FIXED**

**根因**：provider 函数体无 try/except；`inputs["composite_graph"]` 为
工程/AI 可控数据，`_resolve_output_resolution` 的 `int("hd")` 直接
ValueError、渲染异常逃逸到 harness 调用点——违反"渲染失败/被取消 →
返回 None，editorial 层诚实降级"契约。

**修复**：provider 主体抽为 `_run_composite_render_provider` 并整体包
try/except（log + return None）；`_resolve_output_resolution` 对
`(TypeError, ValueError)` 回退 1920x1080 并告警。

**测试**：`test_aud_2213_provider_returns_none_when_render_raises`、
`test_aud_2213_invalid_resolution_falls_back_to_default`。

---

## 验证记录

### py_compile（全部通过）

```
$ .venv/bin/python -m py_compile src/app/controllers/ai_edit_controller.py \
    src/app/controllers/ai_edit_host_bridge.py \
    src/app/controllers/ai_edit_host_bridge_impl.py \
    src/app/controllers/ai_editing_mixin.py \
    src/app/controllers/ai_workflow_composite_render.py \
    tests/app/test_fix_wd1g2_aud.py
ALL_COMPILE_OK
```

### 新建测试

```
$ .venv/bin/python -m pytest tests/app/test_fix_wd1g2_aud.py -q
30 passed in 2.89s
```

### 邻接既有测试（grep tests/ 中 ai_edit/ai_workflow/bridge/mixin 相关 + 修改文件的全部导入方）

```
tests/app/test_ai_edit_controller.py + test_ai_controller_agent_runtime_v2.py
  + test_ai_action_policy_boundary.py + test_ai_command_stack_commit.py
  + test_ai_e2e_deterministic.py + test_ai_edit_host_bridge_semantic_index.py
  + test_ai_edit_host_bridge_subtitle_geometry.py + test_path_a_evidence_flow.py
  + test_ai_editing_composition_root_r1.py + test_fix_controllers_aud.py
  → 127 passed

tests/ui/test_p1_chat_free_conversation.py + tests/edit/ai_editing/test_workflow_runner.py
  + test_project_save_routing.py + test_dock_layout_controller_coverage.py
  + test_subtitle_placement.py
  → 112 passed

tests/edit/ai_editing（全目录）
  → 2000 passed, 2 skipped, 10 deselected

tests/app/test_deliver_controller_coverage.py + tests/composite/test_export_provider_thread_route.py
  + test_fix_wd1g4_aud.py + test_media_analysis_controller.py
  → 202 passed, 3 failed（预存在，见下）

tests/ai（全目录）+ tests/ui/test_p0_wiring_*.py + test_p1_acceptance/controller_deps/
  last_mile/media_pool_selection/plan_cancel/router_dispatch/result_action_acceptance
  → 670 passed, 2 failed（预存在，见下）

tests/app/test_fix_wd1g2_aud.py + tests/architecture/test_boundary_guardrails.py
  + tests/ui/test_ai_workflow_dialog_trigger.py + tests/teaching_animation/test_fix_export_module_aud4.py
  + tests/edit/ai_editing/test_p0_session_wiring.py + test_context_compress.py
  + tests/vault/test_model_pack_integration.py
  → 125 passed, 1 failed（预存在，见下）
```

### 预存在失败甄别（均与本批无关）

| 失败 | 归属 |
|------|------|
| `tests/app/test_fix_wd1g4_aud.py` ×3（`_Host` 作为 QObject parent TypeError，composite_render_worker.py:177） | 该测试不导入本批任何修改文件；故障在 `composite_controller.py`/`composite_render_worker.py`（前序 wave 修改，+206 行，非本批白名单） |
| `tests/ui/test_p1_media_pool_selection.py` ×2（QModelIndex 选择比较） | **已验证**：将本批 5 个修改文件临时还原为 HEAD 版本后仍同样失败 → 预存在 |
| `tests/architecture/test_boundary_guardrails.py`（app 层 feature 导入：ai_assistant_composer / captions.translation） | **已验证**：HEAD 版本同样失败；被标记的导入均为 HEAD 已有代码（:4970/:5979/:6119），本批未新增任何 feature 导入 |

### 覆盖率量化（GAP-010）

```
$ .venv/bin/python -m pytest tests/app/test_fix_wd1g2_aud.py \
    tests/app/test_ai_edit_controller.py tests/app/test_ai_controller_agent_runtime_v2.py \
    --cov=src.app.controllers.ai_edit_controller --cov-report=term
ai_edit_controller.py    3703    2147    42%   （基线：39%，缺失 2199/3594）
```

## 附带发现（未处理，供后续批次）

1. `export_service.run_export_preflight`（:274 起）以 `fn(config, sequence)`
   调用 `preflight_video_effects_export` / `preflight_fonts_export`，而两者
   签名为 `(sequence)`（timeline_export.py:758/943）——三套 preflight 中两套
   恒以 "preflight raised" 计入 blocking。真实导出路径疑似被此破坏（本批
   AUD-2103/2104 测试中以 monkeypatch 隔离，未修改非白名单文件）。
2. `tests/ui/test_p1_media_pool_selection.py` 2 例与 boundary guardrail
   1 例为当前工作树的预存在失败（前序 wave 遗留），建议在对应批次修复。
