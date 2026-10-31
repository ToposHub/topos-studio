"""P9.2 测试：WorkflowRunBridge（FakeWorkflowService 回调 → runtime node 事件）。

> :class:`FakeWorkflowService` 用纯函数回调 ``emit_node_event(kind, node_id)`` 发
> 合成节点事件。:class:`WorkflowRunBridge` 把这个回调接进 :class:`HarnessRuntime`
> 的事件流：闭包绑定 ``run_id``，内部调 :meth:`HarnessRuntime.emit_node_event`。

验证契约：

- ``make_emit_for_run(run_id)`` 返回闭包 ``(kind, node_id, payload=None) -> None``。
- 闭包调用后，runtime 的 ``RunRecord.events`` 出现对应 ``node.*`` 事件，node_id 正确。
- 可选 ``payload`` 透传进事件（真实后端传 ``{"stage": ...}``）。
- run 不存在时闭包静默 no-op（不抛、不产生事件）。
- 订阅者能收到（验证 runtime → 订阅者 → adapter 的链路）。
- kind 透传：``"started"`` → ``node.started``。
"""
from src.features.ai_harness.invocation_models import (
    Invocation,
    InvocationScope,
    InvocationSource,
    RunEventKind,
    RunPolicy,
)
from src.features.ai_harness.runtime import HarnessRuntime


def _make_invocation() -> Invocation:
    return Invocation(
        invocation_id="inv-bridge",
        source=InvocationSource.WORKFLOW,
        skill_id="test.skill",
        scope=InvocationScope(project_id="p1", timeline_id="", selected_asset_ids=()),
        policy=RunPolicy(),
        inputs={},
        user_text="",
        created_at=0.0,
    )


def test_bridge_closure_forwards_to_runtime() -> None:
    """闭包调用 → runtime.events 出现 node.started，node_id 正确。"""
    from src.features.ai_workflow.runtime.workflow_run_bridge import (
        WorkflowRunBridge,
    )
    rt = HarnessRuntime()
    run_id = rt.start(_make_invocation())
    bridge = WorkflowRunBridge(rt)
    emit = bridge.make_emit_for_run(run_id)
    emit("started", "n1")
    emit("succeeded", "n1")
    rec = rt.get_run(run_id)
    assert rec is not None
    node_events = [e for e in rec.events if e.kind.startswith("node.")]
    assert len(node_events) == 2
    assert node_events[0].kind == RunEventKind.NODE_STARTED
    assert node_events[0].node_id == "n1"
    assert node_events[1].kind == RunEventKind.NODE_SUCCEEDED
    assert node_events[1].node_id == "n1"


def test_bridge_closure_subscriber_receives() -> None:
    """订阅者收到 bridge 转发的事件。"""
    from src.features.ai_workflow.runtime.workflow_run_bridge import (
        WorkflowRunBridge,
    )
    rt = HarnessRuntime()
    received = []
    rt.add_subscriber(lambda e: received.append(e))
    run_id = rt.start(_make_invocation())
    bridge = WorkflowRunBridge(rt)
    emit = bridge.make_emit_for_run(run_id)
    emit("failed", "n2")
    failed = [e for e in received if e.kind == RunEventKind.NODE_FAILED]
    assert len(failed) == 1
    assert failed[0].node_id == "n2"


def test_bridge_closure_is_run_scoped() -> None:
    """两个 run 的闭包互不干扰（闭包绑定各自 run_id）。"""
    from src.features.ai_workflow.runtime.workflow_run_bridge import (
        WorkflowRunBridge,
    )
    rt = HarnessRuntime()
    run_a = rt.start(_make_invocation())
    run_b = rt.start(_make_invocation())
    bridge = WorkflowRunBridge(rt)
    emit_a = bridge.make_emit_for_run(run_a)
    emit_b = bridge.make_emit_for_run(run_b)
    emit_a("started", "na")
    emit_b("succeeded", "nb")
    rec_a = rt.get_run(run_a)
    rec_b = rt.get_run(run_b)
    assert rec_a is not None and rec_b is not None
    a_nodes = [e for e in rec_a.events if e.kind.startswith("node.")]
    b_nodes = [e for e in rec_b.events if e.kind.startswith("node.")]
    assert len(a_nodes) == 1 and a_nodes[0].node_id == "na"
    assert len(b_nodes) == 1 and b_nodes[0].node_id == "nb"


def test_bridge_closure_forwards_payload() -> None:
    """AUD-23801：三参调用 ``payload`` 透传进事件（真实后端传阶段信息）。"""
    from src.features.ai_workflow.runtime.workflow_run_bridge import (
        WorkflowRunBridge,
    )
    rt = HarnessRuntime()
    run_id = rt.start(_make_invocation())
    bridge = WorkflowRunBridge(rt)
    emit = bridge.make_emit_for_run(run_id)
    emit("progress", "n1", payload={"stage": "s1"})
    rec = rt.get_run(run_id)
    assert rec is not None
    progress_events = [e for e in rec.events
                       if e.kind == RunEventKind.NODE_PROGRESS]
    assert len(progress_events) == 1
    ev = progress_events[0]
    assert ev.node_id == "n1"
    # payload 字段透传 + node_id 自动并入（旧消费方兼容契约）
    assert ev.payload.get("stage") == "s1"
    assert ev.payload.get("node_id") == "n1"


def test_bridge_closure_unknown_run_is_silent_noop() -> None:
    """AUD-23801：run 不存在时闭包静默 no-op——不抛、不产生事件。"""
    from src.features.ai_workflow.runtime.workflow_run_bridge import (
        WorkflowRunBridge,
    )
    rt = HarnessRuntime()
    received: list[object] = []
    rt.add_subscriber(lambda e: received.append(e))
    bridge = WorkflowRunBridge(rt)
    emit = bridge.make_emit_for_run("no-such-run")
    # 不抛异常
    emit("started", "n1")
    emit("failed", "n1")
    # 订阅者没有收到任何事件（no-op 分支：emit_node_event 返回 False）
    assert received == []
