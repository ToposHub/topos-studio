# batch-234 审查报告 — src/features/ai_workflow/runtime/

- 审查日期：2026-09-28
- 编号区间：AUD-23400–AUD-23499
- 文件数：6（1,682 行）
- 审查维度：线程安全 / GPU 资源生命周期 / GLSL·HDR 正确性 / 信号防递归 / 状态提交一致性 / 异常与取消路径 / 输入验证 / 性能模式 / 类型注解
- 交叉验证（只读）：src/features/ai_harness/runtime.py、legacy_adapters.py、invocation_models.py、src/features/edit/ai_editing/transaction_service.py、src/app/controllers/ai_edit_host_bridge_impl.py、ai_editing_mixin.py

统计：Critical 0 · Major 4 · Suggestion 4

---

## src/features/ai_workflow/runtime/__init__.py (行数: 14)

### 发现

（无）

### 通过项
- 通过（无逻辑）：纯模块 docstring，无 import、无 re-export、无可执行代码。

---

## src/features/ai_workflow/runtime/fake_workflow_service.py (行数: 173)

### 发现

- [AUD-23400] [Major] 取消后仍返回"成功"结果，run 终态被记为 SUCCEEDED 而非 CANCELLED
  - 位置: src/features/ai_workflow/runtime/fake_workflow_service.py:131-144
  - 证据:
    ```python
    if cancel():
        _LOG.debug("fake workflow cancelled during node %s", node_id)
        break
    ...
    return FakeWorkflowResult(
        artifact_id=f"fake-wf-{uuid.uuid4().hex[:12]}",
        variants=(),
        degraded=False,
    ```
  - 影响: 已交叉验证完整链路：本服务 cancel 中途 break 后仍返回带非空 `artifact_id` 的结果 → `legacy_adapters._record_workflow_result` 记录 artifact 且 `has_artifact` 为真 → `runtime.finish(run_id, success=True)`；而 `runtime.finish`（runtime.py:544-549）仅在 `success=False` 时才检查 `cancel_requested` 归入 CANCELLED。结果：用户取消的合成 run 终态显示 SUCCEEDED 并附带一个 fake artifact，违背本包"fake provider 诚实"红线，也与同路径运行前取消（legacy_adapters.py:267-268 → CANCELLED）行为不一致。现有测试（test_fake_workflow_service.py:158-189）只断言节点事件跳过，未覆盖终态。
  - 建议: cancel 打断时返回不含产出的结果（如 `artifact_id=""`），并（配合 adapter 侧）在 `_record_workflow_result` / `finish` 前检查 `runtime.is_cancelled(run_id)`，使取消路径统一收敛到 CANCELLED。
  - 置信度: 高

### 通过项
- 线程安全：纯 Python 协作式执行，无共享可变状态；cancel token / emitter 均为注入回调。
- GPU 资源生命周期：不触碰 ModernGL / 纹理 / GPU 对象。
- GLSL·HDR：不涉及着色器与像素数据。
- 信号防递归：不依赖 Qt，无信号。
- 异常路径：`run_workflow` 无裸 except；构造参数 `node_duration_s < 0` 显式校验。
- 输入验证：`_derive_node_ids` 过滤空串并保序去重；无节点信息时明确退化为单节点 `"workflow"`。
- 性能模式：sleep 为合成执行的既定设计，worker 线程内阻塞不影响 UI。
- 类型注解：完整（含 `NodeEventEmitter` / `CancelToken` 别名与 dataclass 字段）。

---

## src/features/ai_workflow/runtime/workflow_action_service.py (行数: 273)

### 发现

- [AUD-23401] [Suggestion] apply 提交不做 revision 复核，与 rollback 的可验证边界不对称
  - 位置: src/features/ai_workflow/runtime/workflow_action_service.py:152-158
  - 证据:
    ```python
    try:
        compiled = compile_plan(plan)
        new_timeline = bridge.commit_ai_command(
            plan.target.timeline_id, compiled.macro, undo_label=compiled.undo_label,
        )
    ```
  - 影响: revision 门禁完全依赖 `preview()` 内的 STALE_REVISION 检查（第 92 行），consume grant 与 commit 之间无二次校验。已交叉验证宿主侧 `commit_ai_command`（ai_edit_host_bridge_impl.py:558-595）用"当前 revision + approval_granted=True"过策略，不比对 `plan.base_revision`；而 rollback 路径（本文件 198-206 行）显式传 `expected_revision` 由宿主验证。当前 apply 全程在主线程同步执行，实际竞态窗口小，但审批 grant 绑定的 diff 是旧快照，防御深度弱于 rollback 侧。
  - 建议: 让 `commit_ai_command` 同样接受 `expected_revision`（或 `expected_undo_label`）由宿主复核；或 commit 前在锁内重读 timeline revision 再比对一次。
  - 置信度: 中

- [AUD-23402] [Suggestion] `_plans` / `_previews` / `_applied` 无淘汰，长会话内存持续增长
  - 位置: src/features/ai_workflow/runtime/workflow_action_service.py:50-52
  - 证据:
    ```python
    self._plans: dict[str, Any] = {}
    self._previews: dict[str, PreviewResult] = {}
    self._applied: dict[tuple[str, str, str], Any] = {}
    ```
  - 影响: 三个字典只增不减（写入点 61/103/165；唯一删除是 rollback 按 run_id 清理 `_applied`，218-221 行）。`_applied` 按 key 保存完整 `new_timeline` 对象、`_previews` 保存含 diff 的 PreviewResult；长时间编辑会话中每次 run 的 plan/preview/timeline 都被永久持有。
  - 建议: 按 run 终态或 LRU（如保留最近 N 个 run）淘汰旧条目，或仅保存回滚所需的最小信息（revision、undo_label）而非整个 timeline。
  - 置信度: 高

### 通过项
- 线程安全：所有共享 dict 读写在 `threading.RLock` 内；providers 为注入闭包。
- GPU 资源生命周期：纯 Python 边界，不涉及 GPU。
- GLSL·HDR：不涉及。
- 信号防递归：无 Qt 信号。
- 状态提交一致性：STALE_REVISION 门禁、replay 去重（already_applied 缓存在 stale 检查之前）、grant 绑定 plan_hash+diff_hash、rollback 由宿主 `expected_undo_label`/`expected_revision` 验证，链条整体闭环。
- 异常路径：三处 `except Exception` 均带 noqa 且转为显式 diagnostic_code 失败结果（COMMAND_COMMIT_FAILED / ROLLBACK_FAILED / APPROVAL_REQUEST_FAILED），未掩盖领域错误；ApprovalError 单独捕获。
- 输入验证：空 run_id / 空 plan_id 拒绝；grant 缺失即 APPROVAL_REQUIRED，失败均 fail-closed。
- 性能模式：`_plan_hash` 每次失败路径做一次 JSON 序列化，计划体量小，可接受。
- 类型注解：完整。

---

## src/features/ai_workflow/runtime/workflow_context.py (行数: 193)

### 发现

- [AUD-23403] [Major] 仅配置过 ComfyUI profile（未绑定/未探活）即广告 image_gen+video_gen 能力
  - 位置: src/features/ai_workflow/runtime/workflow_context.py:186-189
  - 证据:
    ```python
    comfy_executors = getattr(host_bridge, "_comfy_generation_executors", None)
    if comfy_executors:
        capabilities.add("image_gen")
        capabilities.add("video_gen")
    ```
  - 影响: 已交叉验证宿主侧：`ai_editing_mixin.py:764-786` 在装配期就为**每个已配置 profile** 填充 `_comfy_generation_executors[profile_id]`，且构造时 `server_capabilities=client._empty_capabilities()`；"诚实注册"门禁在 `bind_comfy_workflow`（mixin:1662-1694：探活 → 绑定 → 才注册进 ProviderRegistry，注释明言"未绑定不做能力广告"）。本函数只判断 dict 非空，等于把能力广告绕回"配置即可用"：含 image_gen/video_gen 节点的图能通过编译期能力检查，运行期才因 executor 无绑定 workflow 而失败——与注释"存在即代表真实可用"不符，编译期能力门禁失真。
  - 建议: 改判"已绑定且探活成功"的信号（如 executor 的 server_capabilities 非空、或从 ProviderRegistry 读取已注册的 comfy provider），而非装配期 dict 的真值。
  - 置信度: 高

### 通过项
- 线程安全：一次性快照解析，无共享可变状态。
- GPU 资源生命周期：不涉及 GPU。
- GLSL·HDR：不涉及。
- 信号防递归：无 Qt。
- 状态提交一致性：context_hash 覆盖全部字段，base_revision/brief_revision 进入快照，供后续 STALE 门禁使用。
- 异常路径：`_timeline_revision` 对非法 revision 显式抛带 code 的 WorkflowContextError；无裸 except。
- 输入验证：project/timeline/asset_ids 缺失即 fail-closed（空选择不被提升为全库），符合最小范围原则。
- 性能模式：分析快照 hash 需序列化所选 asset 的分析结果，仅在 compile 前执行一次，可接受。
- 类型注解：完整。
- 备注：87-89 行 `str(x() if callable(x) else "" or "")` 的运算符优先级写法（`"" or ""`）结果正确但易误读，建议显式括号（质量备注，不计发现）。

---

## src/features/ai_workflow/runtime/workflow_run_bridge.py (行数: 59)

### 发现

（无）

### 通过项
- 线程安全：闭包仅调用 `runtime.emit_node_event`（runtime 内部 RLock + detached no-op，已验证 runtime.py:665-695）；TYPE_CHECKING 避免运行时依赖。
- GPU 资源生命周期：不涉及。
- GLSL·HDR：不涉及。
- 信号防递归：无 Qt 信号。
- 状态提交一致性：只读转发，无状态。
- 异常与取消路径：run 不存在/detached 时由 runtime 返回 False 静默 no-op，符合设计。
- 输入验证：payload 可选默认 None，兼容两参与三参调用。
- 性能模式：每次事件经 `make_emit_for_run` 新建闭包，开销可忽略。
- 类型注解：完整。

---

## src/features/ai_workflow/runtime/workflow_run_controller.py (行数: 970)

### 发现

- [AUD-23404] [Major] `_current_run_id` 从不清空，第二次 run 起 pending-cancel 机制失效，窗口期取消被静默丢失
  - 位置: src/features/ai_workflow/runtime/workflow_run_controller.py:282-285
  - 证据:
    ```python
    if self._current_run_id:
        self._runtime.cancel(self._current_run_id, reason="user_requested")
    else:
        self._cancel_pending = True
    ```
    （`_current_run_id` 全文件仅两处赋值：151 行初始化 `""`、513 行 `_on_created` 回填，run 终态后从不清空；`_cancel_pending` 在 `_start_worker` 477 行先于 `worker.start()` 重置。）
  - 影响: `panel.set_running(True)`（479 行）使 Cancel 立即可点，但新 run 的 run_id 要等 worker 线程内 `runtime.start` 同步派发 run.created（已验证 `_dispatch` 为同步内联调用）后才回填。此窗口内用户点 Cancel：`_current_run_id` 仍是**上一个已终态 run** 的 id → 走 if 分支去 cancel 死 run（runtime.cancel 不检查终态，只置标志+发事件，无效），`_cancel_pending` 不会被置位 → 新 run 永远收不到取消请求，该次点击被静默吞掉。152-155 行注释声称的 pending 机制实际只对首个 run 生效。
  - 建议: 在 `_start_worker` 启动 worker 前清空 `_current_run_id`（并接受旧 run 的 final 刷新已在此前完成），或在 `cancel()` 里判断当前 run 是否 active，非 active 时同时置 `_cancel_pending`。
  - 置信度: 高

- [AUD-23405] [Major] `run()`/`_start_worker` 无在跑防护，覆盖仍运行中的旧 worker 引用可致 QThread 销毁崩溃
  - 位置: src/features/ai_workflow/runtime/workflow_run_controller.py:530-535
  - 证据:
    ```python
    self._worker = _RunWorker(
        adapter=self._legacy_adapter,
        compiled=compiled,
        invocation=invocation,
        runtime=self._runtime,
    )
    ```
    （`_RunWorker` 构造未传 parent → QObject 无父；`run()` 168-193 行直接调 `_start_worker`，未检查 `self._worker is not None and self._worker.isRunning()`。）
  - 影响: 生产 UI 靠 panel 禁用 Run 防重复，但 controller 公共 API（`run()` / 绕过终态检查的调用路径）无自身防护。若在前一 worker 仍在运行时再次进入 `_start_worker`，`self._worker = ...` 丢掉旧 worker 的最后一个 Python 引用，运行中的 QThread C++ 对象随之析构 → "QThread: Destroyed while thread is still running"，典型崩溃。
  - 建议: `_start_worker` 开头检查在跑 worker：先协作取消（`runtime.cancel`）并等待/拒绝并发 run；或给 worker 传 parent 并在覆盖前确保旧线程已结束。
  - 置信度: 中（崩溃需绕过 panel 防护的程序化调用，但代码层无任何防线）

- [AUD-23406] [Suggestion] worker 失败仅记日志，失败信息不进 Run Drawer
  - 位置: src/features/ai_workflow/runtime/workflow_run_controller.py:702-705
  - 证据:
    ```python
    @pyqtSlot(str, str)
    def _on_worker_failed(self, run_id: str, error: str) -> None:
        _LOG.error("workflow run failed: %s", error)
        self._panel.set_running(False)
    ```
  - 影响: `invoke_compiled` 在 `runtime.start` 之前抛错时（run 从未创建、无 diagnostics 事件），用户只看到 Run 按钮恢复，Run Drawer 停留在旧状态，错误原因只存在于日志——与 `_push_diagnostics_to_drawer` 覆盖的编译失败路径不对称。
  - 建议: 失败时经 `_push_diagnostics_to_drawer` 推 `{"code": "WORKER_FAILED", "message": error}` 并 `_refresh_run_drawer(final=True)`。
  - 置信度: 高

- [AUD-23407] [Suggestion] `dispose` 等待超时后既不取消 run 也不处理仍在运行的线程
  - 位置: src/features/ai_workflow/runtime/workflow_run_controller.py:457-458
  - 证据:
    ```python
    if self._worker is not None and self._worker.isRunning():
        self._worker.wait(2000)
    ```
  - 影响: 超过 2s 仍在运行（多节点 fake run 0.3s/节点、或真实后端）时直接返回：未先请求协作取消拉短收敛时间，也未对存活线程做任何标记/告警；此后 controller 若被回收，与 AUD-23405 同类的 QThread 析构风险仍在。
  - 建议: dispose 先 `runtime.cancel(current_run_id)` 再限时 wait，超时时记录 warning 并考虑保留 worker 引用直至 finished。
  - 置信度: 中

### 通过项
- 线程安全（总体）：worker 线程只触碰 runtime（RLock 保护）；subscriber `_on_run_event` 不动 Qt 控件，仅 emit 跨线程 signal（AutoConnection 队列到主线程）；`run_id_box`/`_on_created` 的回填顺序依赖已对照 legacy_adapters（mark_running 先于 run_workflow）与 runtime（RUN_CREATED 同步派发）验证成立；`_dispatch` 对 subscriber 异常有兜底。
- GPU 资源生命周期：本包不 import moderngl/composite.core，不触碰 GPU 对象（红线达成）。
- GLSL·HDR：不涉及。
- 信号防递归：`_node_event_arrived` 等私有 signal 只连主线程槽；`_apply_node_status` → `adapter.set_node_status` → `_refresh_run_drawer` 链路无回环信号（panel 更新为直接方法调用）。
- 状态提交一致性：preview/approve/apply/rollback 全部经 `WorkflowActionService` 宿主边界，controller 不直接写 timeline（红线达成）；`already_applied` 幂等。
- 异常与取消路径（总体）：`_RunWorker.run` 捕获全部异常并 emit failed（不静默）；`runtime.cancel` 幂等、detached 拒绝；`_on_run_event` 对 RUN_STATE_CHANGED 的 payload 访问安全（已验证 runtime 三个发射点均传 dict）。
- 输入验证：graph 为空拒跑；`show_artifact` 对失效引用给出明确 unavailable 状态并兜底 host locator 异常。
- 性能模式：`_refresh_run_drawer` 每节点事件全量重建 drawer 状态，规模为 O(nodes+artifacts)，节点级事件频率下可接受；`wait_for_run` 仅泵 self 的 Core 事件，避免触碰 deferred-delete 控件。
- 类型注解：完整（含 slot、staticmethod、注入 callable 签名）。
- 备注：`_apply_node_status` 忽略信号携带的 run_id 参数（594-601 行）；同一 worker 线程内事件队列有序，当前无实际错配，仅属多 run 扩展时的隐患（不计发现）。
