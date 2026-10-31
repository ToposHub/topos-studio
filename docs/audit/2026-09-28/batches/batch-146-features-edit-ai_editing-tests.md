# Batch-146 静态审查报告 — features/edit/ai_editing/tests/test_assistant_service_cancel.py

- 日期: 2026-09-28
- 审查范围: `src/features/edit/ai_editing/tests/test_assistant_service_cancel.py`（213 行，已全文通读）
- 交叉核对的真实实现：`src/features/edit/ai_editing/assistant_service.py`（1044 行，run_workflow/cancel/set_job_cards_provider/_emit_state 路径）、`src/features/edit/ai_editing/assistant_events.py`、`src/features/edit/ai_editing/workflow.py`、`src/features/ai_harness/runtime.py`、`src/features/ai_harness/invocation_models.py`
- 问题编号区间: AUD-14600 – AUD-14603
- 汇总: Critical 0 / Major 0 / Suggestion 4

---

## src/features/edit/ai_editing/tests/test_assistant_service_cancel.py (行数: 213)

### 发现

- [AUD-14600] [Suggestion] 已取消短路测试断言过弱：只验 `result is not None`，未断言 cancelled 降级契约与状态机
  - 位置: src/features/edit/ai_editing/tests/test_assistant_service_cancel.py:88-92
  - 证据:
    ```python
    result = service.run_workflow(inp, cancel_requested=cancel_requested)

    assert fake_runner.run.call_count == 0, "已取消时不应执行 WorkflowRunner"
    # 返回 cancelled 降级结果
    assert result is not None
    ```
    对照实现（assistant_service.py:685-693）：短路分支返回 `WorkflowResult(workflow_id=inp.workflow_id, degradation="cancelled")` 并 `_set_state(AssistantState.CANCELLED)`。
  - 影响: 若短路分支的返回契约（`degradation == "cancelled"`）或状态翻转（`AssistantState.CANCELLED`）回归——例如降级文案被误删或状态漏置——测试仍然通过；注释声称验证"cancelled 降级结果"但断言强度与注释不符，回归防护不完整。
  - 建议: 追加 `assert result.degradation == "cancelled"` 与 `assert service.state == AssistantState.CANCELLED`（`AssistantState` 已在文件顶部导入）。
  - 置信度: 高

- [AUD-14601] [Suggestion] `emit_node_event` 测试回调签名与真实调用不匹配，`emitted` 恒空且从未断言（死测试脚手架）
  - 位置: src/features/edit/ai_editing/tests/test_assistant_service_cancel.py:116-130
  - 证据:
    ```python
    emitted: list[tuple[str, str]] = []

    def emit(kind: str, node_id: str) -> None:
        emitted.append((kind, node_id))
    ...
    service.run_workflow(
        inp, cancel_requested=lambda: False, emit_node_event=emit,
    )
    assert fake_runner.run.call_count == 1
    ```
    两处不匹配：(1) 真实调用（assistant_service.py:695-702 `_emit_stage`）是 `emit_node_event("progress", "", payload={"stage": stage})`，带 `payload` 关键字参数，本回调签名 `(kind, node_id)` 收不了，一旦被调用必然 TypeError 走 `except TypeError: pass` 兼容分支；(2) `fake_runner` 是 `MagicMock`，`runner.run(inp, emit_stage=...)` 不会真正回调 `emit_stage`，`_emit_stage` 根本不执行。
  - 影响: 事件回调的成功路径（emitter 正常接收事件、`emitted` 非空）零覆盖；`emitted` 列表收集后从未断言，是死变量。测试实际只覆盖"传 `emit_node_event` 不抛错"，验证强度远低于标题与脚手架所暗示的层级。
  - 建议: 用会真实调用 `emit_stage` 的 stub runner（`run` 内 `emit_stage("workflow_started")`），回调签名加 `payload: dict | None = None` 参数，并断言 `emitted` 非空且含 `"progress"` 事件。
  - 置信度: 高

- [AUD-14602] [Suggestion] `_capture_snapshot` 缺少返回类型注解
  - 位置: src/features/edit/ai_editing/tests/test_assistant_service_cancel.py:207
  - 证据:
    ```python
    def _capture_snapshot(service: AssistantService):
        """触发一次 _emit_state 并捕获 snapshot。"""
    ```
  - 影响: 不符合项目强类型规范（`.clinerules`：所有函数带类型注解）；文件内其余辅助函数（`_make_service`、`_make_workflow_input`）均已注解，仅此一处遗漏。
  - 建议: 改为 `def _capture_snapshot(service: AssistantService) -> AssistantStateSnapshot:`，并从 `assistant_events` 导入 `AssistantStateSnapshot`。
  - 置信度: 高

- [AUD-14603] [Suggestion] `WorkflowResult` 在两个测试函数内重复局部导入，应上移模块顶部
  - 位置: src/features/edit/ai_editing/tests/test_assistant_service_cancel.py:104、:122
  - 证据:
    ```python
    def test_run_workflow_proceeds_when_not_cancelled():
        ...
        from src.features.edit.ai_editing.workflow import WorkflowResult
        fake_runner.run.return_value = WorkflowResult(workflow_id=inp.workflow_id)
    ```
    （:122 `test_emit_node_event_callback_accepted_but_optional` 内同样局部导入一次。）模块顶部第 27 行已从同一模块导入 `WorkflowInput, WorkflowId`。
  - 影响: 同一依赖两处函数内局部导入 + 顶部已有同模块导入，风格不一致、增加噪音；无功能风险。
  - 建议: 将 `WorkflowResult` 并入顶部 `from src.features.edit.ai_editing.workflow import WorkflowInput, WorkflowId, WorkflowResult`，删除两处局部导入。
  - 置信度: 高

### 通过项

- 线程安全：测试全程单线程；所用 `HarnessRuntime` 内部 `RLock` 保护 `_runs`，测试无并发访问私有状态。
- GPU 资源生命周期：不涉及 ModernGL/纹理/FBO（service 层为纯逻辑，无 Qt/OpenGL 依赖），测试无 GPU 资源可泄漏。
- GLSL/HDR 正确性：不涉及着色器与 HDR 数据。
- 信号防递归：service 用 callable 回调（`on_state_changed`）而非 pyQtSignal；`_capture_snapshot` 直接覆写回调并手动 `_emit_state()`，无递归触发路径。
- 状态提交一致性：不涉及 undo/redo/revision 提交（runner 为 mock，无真实 commit；`base_revision=0` 的 WorkflowInput 仅作透传载荷）。
- 异常与取消路径（主干已覆盖）：cancel 透传 runtime（真实 `HarnessRuntime.start/cancel/is_cancelled` 集成验证）与无 runtime 回退纯状态翻转两分支均覆盖；run_workflow 已取消门槛短路、未取消正常执行均覆盖（未覆盖 `runtime.cancel` 抛错被吞的分支，属轻微覆盖缺口，不足以单列）。
- 输入验证：`WorkflowInput` 构造字段（workflow_id/asset_ids/timeline_id/project_id/base_revision/brief_id）均符合 schema；`InvocationScope(project_id="p1")` 满足 `project_id` 必填校验。
- 性能模式：无性能敏感代码；测试体量小，无重复昂贵构造。
- 依赖与标记真实性：`pytestmark = pytest.mark.edit_core` 已在 `pytest.ini:23` 注册，无 strict-markers 风险；`AssistantService(fake_session)` 走公开构造函数，`JobCard` 字段（job_id/kind/status/progress/label）与 `assistant_events.py` 定义一致。
