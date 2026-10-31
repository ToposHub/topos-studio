"""P0.6 接线层测试：cancel 贯通 runtime + jobs 填进 snapshot。

> 验证 §10.2 P0.6：
> - ``AssistantService.run_workflow`` 接受 ``cancel_requested`` / ``emit_node_event``
>   关键字参数（让 ``LegacyHarnessAdapter._service_accepts_workflow_kwargs`` 探测成功）
> - 已取消时 ``run_workflow`` 在调用前门槛短路（不执行 WorkflowRunner）
> - ``AssistantService.cancel()`` 在 runtime 已注入时透传到 ``runtime.cancel``
> - ``set_job_cards_provider`` 让 ``AssistantStateSnapshot.jobs`` 非空（不再是恒 ()）
"""
from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from src.features.ai_harness.runtime import HarnessRuntime
from src.features.ai_harness.invocation_models import (
    Invocation,
    InvocationScope,
    InvocationSource,
)
from src.features.edit.ai_editing.assistant_events import (
    AssistantState,
    AssistantStateSnapshot,
    JobCard,
)
from src.features.edit.ai_editing.assistant_service import AssistantService
from src.features.edit.ai_editing.workflow import (
    WorkflowInput,
    WorkflowResult,
    WorkflowId,
)

pytestmark = pytest.mark.edit_core


# ---------------------------------------------------------------------- #
# 辅助：构造最小可用的 AssistantService（不接 EditSession）
# ---------------------------------------------------------------------- #


def _make_service() -> AssistantService:
    """构造 AssistantService（用 fake session host，不接真实工程）。"""
    fake_session = MagicMock()
    fake_session.get_snapshot.return_value = MagicMock(
        active_timeline=None, project_id="proj-1", assets={},
    )
    return AssistantService(fake_session)


def _make_workflow_input() -> WorkflowInput:
    """构造最小 WorkflowInput（口播精剪）。"""
    return WorkflowInput(
        workflow_id=WorkflowId.VOICEOVER_POLISH,
        asset_ids=("a1",),
        timeline_id="tl-1",
        project_id="proj-1",
        base_revision=0,
        brief_id="default",
    )


# ---------------------------------------------------------------------- #
# run_workflow 接受 cancel / emit_node_event
# ---------------------------------------------------------------------- #


def test_run_workflow_accepts_cancel_requested_kwarg():
    """run_workflow 签名应接受 cancel_requested（让 adapter 探测成功）。"""
    import inspect

    sig = inspect.signature(AssistantService.run_workflow)
    assert "cancel_requested" in sig.parameters, (
        "run_workflow 必须接受 cancel_requested 关键字参数"
    )
    assert "emit_node_event" in sig.parameters, (
        "run_workflow 必须接受 emit_node_event 关键字参数（adapter 探测需要二者齐全）"
    )


def test_run_workflow_short_circuits_when_already_cancelled():
    """已 cancel 时 run_workflow 在门槛处短路，不执行 WorkflowRunner。"""
    service = _make_service()
    inp = _make_workflow_input()

    def cancel_requested() -> bool:
        return True

    # 用 MagicMock 替换 _workflow_runner 验证它不被调
    fake_runner = MagicMock()
    service._workflow_runner = fake_runner

    result = service.run_workflow(inp, cancel_requested=cancel_requested)

    assert fake_runner.run.call_count == 0, "已取消时不应执行 WorkflowRunner"
    # AUD-14600：不能只断言 result is not None——必须钉死 cancelled 降级契约
    # 与状态机：degradation == "cancelled"、状态翻转成 CANCELLED、并留下取消消息。
    assert getattr(result, "degradation", None) == "cancelled", (
        "已取消短路必须返回 degradation='cancelled' 的降级 WorkflowResult"
    )
    snap = _capture_snapshot(service)
    assert snap.state == AssistantState.CANCELLED, (
        "已取消短路必须把状态机翻到 CANCELLED"
    )
    messages = [str(getattr(m, "text", "")) for m in service._messages]
    assert any("已取消" in text for text in messages), (
        "已取消短路应留下取消提示消息"
    )


def test_run_workflow_proceeds_when_not_cancelled():
    """未取消时正常执行 WorkflowRunner（cancel_requested 返回 False）。"""
    service = _make_service()
    inp = _make_workflow_input()

    def cancel_requested() -> bool:
        return False

    fake_runner = MagicMock()
    fake_runner.run.return_value = WorkflowResult(workflow_id=inp.workflow_id)
    service._workflow_runner = fake_runner

    service.run_workflow(inp, cancel_requested=cancel_requested)
    assert fake_runner.run.call_count == 1


class _StageEmittingRunner:
    """真实把 ``emit_stage`` 回调执行一次的 fake runner（AUD-14601）。

    MagicMock runner 的 ``run`` 什么也不做，导致 ``emit_node_event`` 从未被
    调用（死脚手架）。本 runner 复刻 ``WorkflowRunner.run`` 的回调契约：
    ``run(inp, *, emit_stage)``，并逐阶段调用 ``emit_stage(stage)``。
    """

    def __init__(self, stages: tuple[str, ...] = ("analyze", "plan")) -> None:
        self._stages = stages
        self.received_emit_stage: Any = None

    def run(self, inp: WorkflowInput, *, emit_stage: Any = None) -> WorkflowResult:
        self.received_emit_stage = emit_stage
        if emit_stage is not None:
            for stage in self._stages:
                emit_stage(stage)
        return WorkflowResult(workflow_id=inp.workflow_id)


def test_emit_node_event_callback_accepted_but_optional():
    """emit_node_event 回调收到 WorkflowRunner 的真实阶段事件契约。"""
    service = _make_service()
    inp = _make_workflow_input()
    emitted: list[tuple[str, str, dict]] = []

    # AUD-14601：真实调用签名是 emit_node_event("progress", "", payload={"stage": ...})
    # （assistant_service.run_workflow._emit_stage）。旧测试用 (kind, node_id) 签名
    # 且 runner 是不执行回调的 MagicMock——emitted 恒空且从未断言（死脚手架）。
    def emit(kind: str, node_id: str, payload: dict | None = None) -> None:
        emitted.append((kind, node_id, dict(payload or {})))

    service._workflow_runner = _StageEmittingRunner(stages=("analyze",))

    service.run_workflow(
        inp, cancel_requested=lambda: False, emit_node_event=emit,
    )
    assert emitted == [("progress", "", {"stage": "analyze"})], (
        "emit_node_event 必须收到真实阶段事件 (kind='progress', node_id='', "
        "payload={'stage': ...})"
    )


def test_emit_node_event_optional_without_callback():
    """不传 emit_node_event 时同样正常执行（回调是可选的）。"""
    service = _make_service()
    inp = _make_workflow_input()
    runner = _StageEmittingRunner(stages=())
    service._workflow_runner = runner

    result = service.run_workflow(inp, cancel_requested=lambda: False)
    assert runner.received_emit_stage is not None
    # 未传回调时 _emit_stage 是 no-op：调用不抛错即契约成立
    runner.received_emit_stage("analyze")
    assert result.workflow_id == inp.workflow_id


# ---------------------------------------------------------------------- #
# cancel() 透传 runtime
# ---------------------------------------------------------------------- #


def test_cancel_delegates_to_runtime_when_set():
    """runtime 已注入时 cancel() 透传到 runtime.cancel(active_run_id)。"""
    service = _make_service()
    runtime = HarnessRuntime()
    inv = Invocation(
        source=InvocationSource.CHAT, skill_id="topos.test",
        scope=InvocationScope(project_id="p1"),
    )
    run_id = runtime.start(inv)
    service.set_active_run_id(run_id, runtime=runtime)

    service.cancel()

    assert runtime.is_cancelled(run_id), "cancel 应透传到 runtime.cancel"


def test_cancel_falls_back_to_state_flip_without_runtime():
    """runtime 未注入时 cancel() 保持旧行为（纯状态翻转，向后兼容）。"""
    service = _make_service()
    service.cancel()
    # 不抛错；状态变 CANCELLED（_set_state 已触发）
    snap = _capture_snapshot(service)
    assert snap.state == AssistantState.CANCELLED


# ---------------------------------------------------------------------- #
# set_job_cards_provider 填 snapshot.jobs
# ---------------------------------------------------------------------- #


def test_set_job_cards_provider_fills_snapshot_jobs():
    """注入 provider 后 snapshot.jobs 非空（不再恒 ()）。"""
    service = _make_service()
    service.set_job_cards_provider(lambda: (
        JobCard(job_id="j1", kind="analysis", status="running", progress=0.5,
                label="ASR 转录"),
    ))
    snap = _capture_snapshot(service)
    assert len(snap.jobs) == 1
    assert snap.jobs[0].job_id == "j1"
    assert snap.jobs[0].status == "running"


def test_snapshot_jobs_empty_without_provider():
    """未注入 provider 时 snapshot.jobs 仍为 ()（向后兼容）。"""
    service = _make_service()
    snap = _capture_snapshot(service)
    assert snap.jobs == ()


def test_job_cards_provider_dynamic_refresh():
    """provider 返回的 jobs 随 registry 状态变化（每次 emit 重读）。"""
    service = _make_service()
    current_jobs: list[JobCard] = []
    service.set_job_cards_provider(lambda: tuple(current_jobs))

    snap1 = _capture_snapshot(service)
    assert len(snap1.jobs) == 0

    current_jobs.append(JobCard(job_id="j1", kind="analysis", status="queued"))
    snap2 = _capture_snapshot(service)
    assert len(snap2.jobs) == 1


# ---------------------------------------------------------------------- #
# 辅助
# ---------------------------------------------------------------------- #


def _capture_snapshot(service: AssistantService) -> AssistantStateSnapshot:
    """触发一次 _emit_state 并捕获 snapshot。"""
    captured = []
    service.on_state_changed = lambda snap: captured.append(snap)
    service._emit_state()
    assert captured, "_emit_state 应回调 on_state_changed"
    return captured[-1]
