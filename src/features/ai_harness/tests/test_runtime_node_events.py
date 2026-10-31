"""P9.2 测试：HarnessRuntime.emit_node_event（node.* 事件入口）。

> runtime 的 ``_emit``（``runtime.py:471``）永远 ``node_id=""``——run 级事件
> 不带节点信息。``RunEventKind.NODE_*``（``invocation_models.py:185-193``）词汇
> 早已定义但从未被产生。本批补齐这一缺口：新增公共方法
> :meth:`HarnessRuntime.emit_node_event`，**显式填** ``RunEvent.node_id``。

验证契约：

- ``emit_node_event(run_id, "started", "n1")`` 后，``RunRecord.events`` 末条的
  ``node_id == "n1"``（非空）。
- kind 映射到对应 ``RunEventKind.NODE_*``（``started → node.started`` 等）。
- 订阅者（``add_subscriber``）能收到带 ``node_id`` 的事件。
- 不存在的 run_id → 静默 no-op（不抛）。
- detached session → no-op（与既有 record_* 一致）。
- **不改变**既有 ``_emit`` 的行为（run 级事件仍 ``node_id=""``）。
"""
from src.features.ai_harness.invocation_models import (
    Invocation,
    InvocationScope,
    InvocationSource,
    RunEventKind,
    RunPolicy,
)
from src.features.ai_harness.runtime import HarnessRuntime


def _make_invocation(skill_id: str = "test.skill") -> Invocation:
    return Invocation(
        invocation_id="inv-test",
        source=InvocationSource.WORKFLOW,
        skill_id=skill_id,
        scope=InvocationScope(project_id="p1", timeline_id="", selected_asset_ids=()),
        policy=RunPolicy(),
        inputs={},
        user_text="",
        created_at=0.0,
    )


# ---------------------------------------------------------------------- #
# emit_node_event 契约
# ---------------------------------------------------------------------- #


def test_emit_node_event_fills_node_id() -> None:
    """emit_node_event 后，RunRecord.events 末条 node_id 非空。"""
    rt = HarnessRuntime()
    run_id = rt.start(_make_invocation())
    rt.emit_node_event(run_id, "started", "n1")
    rec = rt.get_run(run_id)
    assert rec is not None
    last = rec.events[-1]
    assert last.node_id == "n1", f"node_id 应填 'n1'；实际：{last.node_id!r}"
    assert last.kind == RunEventKind.NODE_STARTED


def test_emit_node_event_kind_mapping() -> None:
    """kind 字符串映射到 RunEventKind.NODE_*。"""
    rt = HarnessRuntime()
    run_id = rt.start(_make_invocation())
    cases = {
        "queued": RunEventKind.NODE_QUEUED,
        "started": RunEventKind.NODE_STARTED,
        "progress": RunEventKind.NODE_PROGRESS,
        "waiting_input": RunEventKind.NODE_WAITING_INPUT,
        "succeeded": RunEventKind.NODE_SUCCEEDED,
        "failed": RunEventKind.NODE_FAILED,
        "cancelled": RunEventKind.NODE_CANCELLED,
        "cached": RunEventKind.NODE_CACHED,
        "skipped": RunEventKind.NODE_SKIPPED,
    }
    for kind_in, expected_kind in cases.items():
        rt.emit_node_event(run_id, kind_in, "nx")
        rec = rt.get_run(run_id)
        assert rec is not None
        assert rec.events[-1].kind == expected_kind, (
            f"kind={kind_in!r} 应映射到 {expected_kind}；"
            f"实际：{rec.events[-1].kind}"
        )


def test_emit_node_event_unknown_run_is_noop() -> None:
    """不存在的 run_id 静默 no-op（不抛）。"""
    rt = HarnessRuntime()
    # 不应抛
    rt.emit_node_event("nonexistent", "started", "n1")


def test_emit_node_event_subscriber_receives_node_id() -> None:
    """订阅者收到的事件 node_id 非空。"""
    rt = HarnessRuntime()
    received = []
    rt.add_subscriber(lambda evt: received.append(evt))
    run_id = rt.start(_make_invocation())
    rt.emit_node_event(run_id, "succeeded", "n2")
    node_events = [e for e in received if e.kind == RunEventKind.NODE_SUCCEEDED]
    assert len(node_events) == 1
    assert node_events[0].node_id == "n2"


def test_emit_node_event_payload_carries_node_id() -> None:
    """payload 也带 node_id（便于不支持 RunEvent.node_id 的旧消费方）。"""
    rt = HarnessRuntime()
    run_id = rt.start(_make_invocation())
    rt.emit_node_event(run_id, "started", "n3", payload={"progress": 0.5})
    rec = rt.get_run(run_id)
    assert rec is not None
    last = rec.events[-1]
    assert last.node_id == "n3"
    assert last.payload.get("node_id") == "n3"
    assert last.payload.get("progress") == 0.5


def test_run_level_emit_still_empty_node_id() -> None:
    """红线：既有 _emit（run 级事件）行为不变，node_id 仍为 ''。"""
    rt = HarnessRuntime()
    run_id = rt.start(_make_invocation())  # 触发 run.created
    rec = rt.get_run(run_id)
    assert rec is not None
    created_events = [e for e in rec.events if e.kind == RunEventKind.RUN_CREATED]
    assert created_events, "start 应发 run.created"
    assert all(e.node_id == "" for e in created_events), (
        "run 级事件 node_id 必须保持空（既有行为不变）"
    )
