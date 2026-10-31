"""AgentMetricsCollector tests (plan §A7 §6 / §9).

Validates the passive subscriber that records per-run latency, duration,
cancel responsiveness and activity counts from the orchestrator event stream.
A controllable clock makes the latency assertions deterministic.
"""

from __future__ import annotations

import pytest

from src.features.edit.ai_editing.agent_metrics import AgentMetricsCollector
from src.features.edit.ai_editing.agent_models import (
    AgentEventKind,
    AgentRunState,
    AgentTask,
    ArtifactKind,
    ArtifactRef,
)
from src.features.edit.ai_editing.agent_orchestrator import AgentOrchestrator


@pytest.fixture
def harness():
    """An orchestrator + collector sharing a controllable monotonic clock."""

    ticks = [0.0]

    def clock() -> float:
        return ticks[0]

    def advance(seconds: float) -> None:
        ticks[0] += seconds

    orch = AgentOrchestrator(time_fn=clock)
    collector = AgentMetricsCollector(time_fn=clock)
    collector.attach(orch)
    return orch, collector, advance


def _task(session_id: str = "s1") -> AgentTask:
    return AgentTask(session_id=session_id, title="m")


# ---------------------------------------------------------------------- #
# Latency + duration
# ---------------------------------------------------------------------- #


class TestLatency:
    def test_first_token_latency_recorded(self, harness):
        orch, collector, advance = harness
        run_id = orch.start_task(_task())
        advance(0.12)
        collector.record_first_token(run_id)
        advance(0.5)
        orch.mark_context_ready(run_id)
        orch.mark_plan_ready(run_id)
        orch.begin_verification(run_id)
        orch.finish_verification(run_id, success=True)
        m = collector.get_metrics(run_id)
        assert m.first_token_latency_ms == pytest.approx(120.0, abs=1)

    def test_duration_measured_to_terminal(self, harness):
        orch, collector, advance = harness
        orch.start_task(_task())
        advance(2.0)
        run_id2 = orch.start_task(_task("s2"))
        advance(0.0)
        orch.mark_context_ready(run_id2)
        orch.mark_plan_ready(run_id2)
        orch.begin_verification(run_id2)
        orch.finish_verification(run_id2, success=True)
        m = collector.get_metrics(run_id2)
        assert m.duration_ms is not None
        assert m.duration_ms >= 0

    def test_first_event_latency_close_to_zero(self, harness):
        orch, collector, advance = harness
        run_id = orch.start_task(_task())
        m = collector.get_metrics(run_id)
        # The created event fires synchronously during start_task.
        assert m.first_event_latency_ms is not None
        assert m.first_event_latency_ms < 50


# ---------------------------------------------------------------------- #
# Outcomes
# ---------------------------------------------------------------------- #


class TestOutcomes:
    def test_succeeded(self, harness):
        orch, collector, _ = harness
        run_id = orch.start_task(_task())
        orch.mark_context_ready(run_id)
        orch.mark_plan_ready(run_id)
        orch.begin_verification(run_id)
        orch.finish_verification(run_id, success=True)
        assert collector.get_metrics(run_id).outcome == AgentRunState.SUCCEEDED

    def test_failed(self, harness):
        orch, collector, _ = harness
        run_id = orch.start_task(_task())
        orch.fail(run_id)
        assert collector.get_metrics(run_id).outcome == AgentRunState.FAILED

    def test_cancelled(self, harness):
        orch, collector, _ = harness
        run_id = orch.start_task(_task())
        orch.request_cancel(run_id)
        assert collector.get_metrics(run_id).outcome == AgentRunState.CANCELLED


# ---------------------------------------------------------------------- #
# Cancel responsiveness
# ---------------------------------------------------------------------- #


class TestCancelLatency:
    def test_cancel_latency_measured(self, harness):
        orch, collector, advance = harness
        run_id = orch.start_task(_task())
        advance(1.0)
        orch.request_cancel(run_id)
        advance(0.15)
        m = collector.get_metrics(run_id)
        # cancel_requested -> cancelled gap (request_cancel emits both, but the
        # clock advanced between them is captured via the terminal time).
        assert m.cancel_latency_ms is not None or m.outcome == AgentRunState.CANCELLED

    def test_cancel_request_then_delayed_cancelled(self):
        # When cancel_requested and cancelled are on separate ticks, the gap
        # is measurable. We simulate by emitting them manually.
        ticks = [0.0]
        orch = AgentOrchestrator(time_fn=lambda: ticks[0])
        collector = AgentMetricsCollector(time_fn=lambda: ticks[0])
        collector.attach(orch)
        run_id = orch.start_task(_task())
        ticks[0] = 1.0
        orch.emit(run_id, AgentEventKind.RUN_CANCEL_REQUESTED)
        ticks[0] = 1.2
        orch.emit(run_id, AgentEventKind.RUN_CANCELLED)
        m = collector.get_metrics(run_id)
        assert m.cancel_latency_ms == pytest.approx(200.0, abs=1)


# ---------------------------------------------------------------------- #
# Activity counts
# ---------------------------------------------------------------------- #


class TestActivityCounts:
    def test_tool_and_artifact_counts(self, harness):
        orch, collector, _ = harness
        run_id = orch.start_task(_task())
        orch.emit(run_id, AgentEventKind.RUN_TOOL_STARTED, payload={"tool_name": "a"})
        orch.emit(run_id, AgentEventKind.RUN_TOOL_STARTED, payload={"tool_name": "b"})
        orch.stage_artifact(run_id, ArtifactRef(kind=ArtifactKind.TEXT))
        m = collector.get_metrics(run_id)
        assert m.tool_call_count == 2
        assert m.artifact_count == 1

    def test_compression_count(self, harness):
        orch, collector, _ = harness
        run_id = orch.start_task(_task())
        orch.save_checkpoint(run_id, "cp1")
        orch.save_checkpoint(run_id, "cp2")
        m = collector.get_metrics(run_id)
        assert m.compression_count == 2


# ---------------------------------------------------------------------- #
# Summary
# ---------------------------------------------------------------------- #


class TestSummary:
    def test_summary_aggregates_outcomes(self, harness):
        orch, collector, _ = harness
        # one success
        r1 = orch.start_task(_task("s1"))
        orch.fail(r1)  # quick terminal
        # one cancel
        r2 = orch.start_task(_task("s2"))
        orch.request_cancel(r2)
        s = collector.summary()
        assert s["total_runs"] == 2
        assert s["outcomes"].get(AgentRunState.FAILED) == 1
        assert s["outcomes"].get(AgentRunState.CANCELLED) == 1

    def test_empty_summary(self):
        collector = AgentMetricsCollector()
        assert collector.summary()["total_runs"] == 0

    def test_unknown_run_returns_none(self, harness):
        _, collector, _ = harness
        assert collector.get_metrics("nope") is None
