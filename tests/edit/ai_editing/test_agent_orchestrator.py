"""AgentOrchestrator tests (plan §A1).

Validates the unified run lifecycle that turns ordinary chat into an
observable, cancellable, recoverable AgentRun:

- Every request gets a unique run_id and a CREATED → GATHERING_CONTEXT start.
- Event order respects the state machine (no ``completed → running`` flips).
- Cancel / fail / pause all reach a terminal (or parked) state with the right
  terminal event.
- Stale callbacks can be dropped: ``active_run_id`` clears once a run ends.
- Two concurrent runs keep independent event streams (no cross-contamination).
"""

from __future__ import annotations

import threading

import pytest

from src.features.edit.ai_editing.agent_models import (
    AgentEventKind,
    AgentRunState,
    AgentTask,
    ApprovalRequest,
    ApprovalRisk,
    ArtifactKind,
    ArtifactRef,
    ContextUsage,
)
from src.features.edit.ai_editing.agent_orchestrator import (
    AgentOrchestrator,
    RunNotFoundError,
    RunNotTransitionableError,
)


@pytest.fixture
def orch() -> AgentOrchestrator:
    return AgentOrchestrator()


@pytest.fixture
def captured(orch):
    events: list = []
    orch.subscribe(events.append)
    return events


def _task(session_id: str = "s1", title: str = "t") -> AgentTask:
    return AgentTask(session_id=session_id, title=title, user_goal="goal")


# ---------------------------------------------------------------------- #
# Creation + initial events
# ---------------------------------------------------------------------- #


class TestStartTask:
    def test_unique_run_id_per_request(self, orch):
        rid_a = orch.start_task(_task("s1"))
        rid_b = orch.start_task(_task("s1"))
        assert rid_a != rid_b

    def test_starts_in_gathering_context_with_events(self, orch, captured):
        run_id = orch.start_task(_task())
        run = orch.get_run(run_id)
        assert run.state == AgentRunState.GATHERING_CONTEXT
        assert run.mode == "chat"
        kinds = [e.kind for e in captured]
        assert kinds == [AgentEventKind.RUN_CREATED, AgentEventKind.RUN_CONTEXT_STARTED]
        # every event carries the run_id (plan §A1 §7)
        assert all(e.run_id == run_id for e in captured)

    def test_active_run_tracks_session(self, orch):
        run_id = orch.start_task(_task("s1"))
        assert orch.active_run_id("s1") == run_id


# ---------------------------------------------------------------------- #
# Happy path + event ordering
# ---------------------------------------------------------------------- #


class TestHappyPath:
    def test_full_lifecycle_to_succeeded(self, orch, captured):
        run_id = orch.start_task(_task())
        orch.mark_context_ready(run_id)
        orch.mark_plan_ready(run_id)
        orch.begin_verification(run_id)
        done = orch.finish_verification(run_id, success=True)
        assert done.state == AgentRunState.SUCCEEDED
        assert done.is_terminal
        assert done.ended_at > 0.0
        assert AgentEventKind.RUN_COMPLETED in [e.kind for e in captured]

    def test_event_order_respects_state_machine(self, orch, captured):
        run_id = orch.start_task(_task())
        orch.mark_context_ready(run_id)
        orch.mark_plan_ready(run_id)
        orch.begin_verification(run_id)
        orch.finish_verification(run_id, success=True)
        kinds = [e.kind for e in captured]
        # No illegal reordering: created before context, completed last.
        assert kinds.index(AgentEventKind.RUN_CREATED) == 0
        assert kinds.index(AgentEventKind.RUN_CONTEXT_STARTED) == 1
        assert kinds[-1] == AgentEventKind.RUN_COMPLETED

    def test_partial_success_emits_completed(self, orch, captured):
        run_id = orch.start_task(_task())
        orch.mark_context_ready(run_id)
        orch.mark_plan_ready(run_id)
        orch.begin_verification(run_id)
        done = orch.finish_verification(run_id, partial=True)
        assert done.state == AgentRunState.PARTIAL_SUCCESS
        assert AgentEventKind.RUN_COMPLETED in [e.kind for e in captured]


# ---------------------------------------------------------------------- #
# Cancel / fail / pause
# ---------------------------------------------------------------------- #


class TestTerminalTransitions:
    def test_cancel_emits_both_events(self, orch, captured):
        run_id = orch.start_task(_task())
        assert orch.request_cancel(run_id) is True
        assert orch.get_run(run_id).state == AgentRunState.CANCELLED
        kinds = [e.kind for e in captured]
        assert AgentEventKind.RUN_CANCEL_REQUESTED in kinds
        assert AgentEventKind.RUN_CANCELLED in kinds
        # cancel_requested precedes cancelled
        assert kinds.index(AgentEventKind.RUN_CANCEL_REQUESTED) < kinds.index(
            AgentEventKind.RUN_CANCELLED
        )

    def test_cancel_terminal_run_is_noop(self, orch):
        run_id = orch.start_task(_task())
        orch.request_cancel(run_id)
        # second cancel does nothing
        assert orch.request_cancel(run_id) is False

    def test_fail_from_any_non_terminal(self, orch, captured):
        run_id = orch.start_task(_task())
        orch.mark_context_ready(run_id)
        done = orch.fail(run_id, reason="boom")
        assert done.state == AgentRunState.FAILED
        assert done.error_code == ""
        assert AgentEventKind.RUN_FAILED in [e.kind for e in captured]

    def test_fail_terminal_is_noop(self, orch):
        run_id = orch.start_task(_task())
        orch.fail(run_id)
        first = orch.get_run(run_id)
        orch.fail(run_id)
        assert orch.get_run(run_id).state == first.state

    def test_pause_for_resume(self, orch):
        run_id = orch.start_task(_task())
        orch.mark_context_ready(run_id)
        orch.mark_plan_ready(run_id)
        orch.begin_verification(run_id)
        paused = orch.pause_for_resume(run_id)
        assert paused.state == AgentRunState.PAUSED_FOR_RESUME
        assert paused.is_user_blocked
        # can recover to gathering_context
        recovered = orch.transition(run_id, AgentRunState.GATHERING_CONTEXT)
        assert recovered.state == AgentRunState.GATHERING_CONTEXT


# ---------------------------------------------------------------------- #
# Approval flow
# ---------------------------------------------------------------------- #


class TestApprovalFlow:
    def test_request_then_grant(self, orch, captured):
        run_id = orch.start_task(_task())
        orch.mark_context_ready(run_id)
        approval = ApprovalRequest(
            run_id=run_id, tool_name="apply_edit_plan", risk=ApprovalRisk.WRITE
        )
        orch.request_approval(run_id, approval)
        assert orch.get_run(run_id).state == AgentRunState.WAITING_APPROVAL
        assert AgentEventKind.RUN_WAITING_APPROVAL in [e.kind for e in captured]
        orch.grant_approval(run_id)
        assert orch.get_run(run_id).state == AgentRunState.EXECUTING

    def test_user_input_branch(self, orch):
        run_id = orch.start_task(_task())
        orch.request_user_input(run_id, message="need clarification")
        assert orch.get_run(run_id).state == AgentRunState.WAITING_USER_INPUT
        # back to planning after clarification
        orch.transition(run_id, AgentRunState.PLANNING)


# ---------------------------------------------------------------------- #
# Stale-callback isolation (plan §A1 §7)
# ---------------------------------------------------------------------- #


class TestStaleCallbackIsolation:
    def test_active_run_clears_after_terminal(self, orch):
        run_id = orch.start_task(_task("s1"))
        assert orch.active_run_id("s1") == run_id
        orch.request_cancel(run_id)
        assert orch.active_run_id("s1") is None

    def test_second_run_supersedes_first_in_session(self, orch):
        first = orch.start_task(_task("s1"))
        second = orch.start_task(_task("s1"))
        # The newest run is the active one for the session.
        assert orch.active_run_id("s1") == second
        assert orch.active_run_id("s1") != first

    def test_unknown_session_has_no_active_run(self, orch):
        assert orch.active_run_id("nope") is None


# ---------------------------------------------------------------------- #
# Concurrent run isolation
# ---------------------------------------------------------------------- #


class TestRunIsolation:
    def test_two_runs_keep_independent_events(self, orch):
        r1 = orch.start_task(_task("s1"))
        r2 = orch.start_task(_task("s2"))
        orch.mark_context_ready(r1)
        # r2 stays in gathering_context
        assert orch.get_run(r1).state == AgentRunState.PLANNING
        assert orch.get_run(r2).state == AgentRunState.GATHERING_CONTEXT
        assert all(e.run_id == r1 for e in orch.get_events(r1))
        assert all(e.run_id == r2 for e in orch.get_events(r2))
        # cancelling r1 does not affect r2
        orch.request_cancel(r1)
        assert orch.get_run(r1).state == AgentRunState.CANCELLED
        assert orch.get_run(r2).state == AgentRunState.GATHERING_CONTEXT

    def test_thread_safe_concurrent_starts(self):
        orch = AgentOrchestrator()
        run_ids: list[str] = []

        def _start():
            run_ids.append(orch.start_task(_task()))

        threads = [threading.Thread(target=_start) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(run_ids) == 8
        assert len(set(run_ids)) == 8  # all unique


# ---------------------------------------------------------------------- #
# Side channels: usage / artifact / checkpoint
# ---------------------------------------------------------------------- #


class TestSideChannels:
    def test_record_usage(self, orch, captured):
        run_id = orch.start_task(_task())
        usage = ContextUsage(actual_prompt=100, actual_completion=50)
        orch.record_usage(run_id, usage)
        run = orch.get_run(run_id)
        assert run.context_usage is not None
        assert run.context_usage.actual_prompt == 100
        assert AgentEventKind.RUN_USAGE_RECORDED in [e.kind for e in captured]

    def test_stage_artifact(self, orch, captured):
        run_id = orch.start_task(_task())
        art = ArtifactRef(artifact_id="a1", kind=ArtifactKind.HTML_DOCUMENT, payload_hash="h")
        orch.stage_artifact(run_id, art)
        assert "a1" in orch.get_run(run_id).artifact_refs
        assert AgentEventKind.ARTIFACT_STAGED in [e.kind for e in captured]

    def test_stage_artifact_idempotent(self, orch):
        run_id = orch.start_task(_task())
        art = ArtifactRef(artifact_id="a1", kind=ArtifactKind.TEXT)
        orch.stage_artifact(run_id, art)
        orch.stage_artifact(run_id, art)
        assert orch.get_run(run_id).artifact_refs == ("a1",)

    def test_save_checkpoint(self, orch, captured):
        run_id = orch.start_task(_task())
        orch.save_checkpoint(
            run_id,
            "cp1",
            completed_steps=("step1",),
            protected_context=("canvas_width", "fps"),
        )
        assert orch.get_run(run_id).checkpoint_id == "cp1"
        assert AgentEventKind.RUN_CHECKPOINT_SAVED in [e.kind for e in captured]


# ---------------------------------------------------------------------- #
# Terminal immutability: side channels must not mutate a finished run
# ---------------------------------------------------------------------- #


class TestTerminalImmutability:
    """A cancelled / failed run keeps its final snapshot frozen (plan §P0)."""

    def test_record_usage_noop_on_cancelled_run(self, orch):
        run_id = orch.start_task(_task())
        orch.request_cancel(run_id)
        run_before = orch.get_run(run_id)
        orch.record_usage(run_id, ContextUsage(actual_prompt=999))
        run_after = orch.get_run(run_id)
        # usage was NOT attached; the cancelled snapshot is unchanged
        assert run_after.context_usage == run_before.context_usage

    def test_stage_artifact_noop_on_cancelled_run(self, orch):
        run_id = orch.start_task(_task())
        orch.request_cancel(run_id)
        orch.stage_artifact(
            run_id,
            ArtifactRef(artifact_id="late", kind=ArtifactKind.TEXT, payload_hash="h"),
        )
        assert "late" not in orch.get_run(run_id).artifact_refs

    def test_save_checkpoint_noop_on_failed_run(self, orch):
        run_id = orch.start_task(_task())
        orch.fail(run_id, reason="boom")
        orch.save_checkpoint(run_id, "cp-late")
        assert orch.get_run(run_id).checkpoint_id == ""

    def test_record_usage_still_works_on_active_run(self, orch):
        # Sanity: the guard must not break the normal (non-terminal) path.
        run_id = orch.start_task(_task())
        orch.record_usage(run_id, ContextUsage(actual_prompt=42))
        assert orch.get_run(run_id).context_usage.actual_prompt == 42


# ---------------------------------------------------------------------- #
# Session-scoped cancel (host does not always know the run id)
# ---------------------------------------------------------------------- #


class TestSessionCancel:
    def test_cancel_active_run_for_session(self, orch):
        run_id = orch.start_task(_task(session_id="sess-a"))
        assert orch.request_cancel_for_session("sess-a") is True
        assert orch.get_run(run_id).state == AgentRunState.CANCELLED

    def test_cancel_returns_false_when_no_active_run(self, orch):
        # No run at all for this session.
        assert orch.request_cancel_for_session("nope") is False

    def test_cancel_returns_false_for_terminal_run(self, orch):
        run_id = orch.start_task(_task(session_id="sess-b"))
        orch.request_cancel(run_id)  # now terminal
        # A second cancel via session is a no-op.
        assert orch.request_cancel_for_session("sess-b") is False


# ---------------------------------------------------------------------- #
# Subscribers
# ---------------------------------------------------------------------- #


class TestSubscribers:
    def test_unsubscribe_stops_delivery(self, orch):
        received = []
        unsub = orch.subscribe(received.append)
        run_id = orch.start_task(_task())
        assert received  # got the created event
        received.clear()
        unsub()
        orch.mark_context_ready(run_id)
        assert not received  # no more delivery

    def test_throwing_sink_does_not_block_others(self, orch):
        good = []

        def bad(_):
            raise RuntimeError("boom")

        orch.subscribe(bad)
        orch.subscribe(good.append)
        orch.start_task(_task())
        assert good  # the good sink still received despite the bad one


# ---------------------------------------------------------------------- #
# Errors
# ---------------------------------------------------------------------- #


class TestErrors:
    def test_unknown_run_raises(self, orch):
        with pytest.raises(RunNotFoundError):
            orch.transition("nope", AgentRunState.PLANNING)

    def test_illegal_transition_raises(self, orch):
        run_id = orch.start_task(_task())
        with pytest.raises(ValueError):
            # cannot jump straight to succeeded
            orch.transition(run_id, AgentRunState.SUCCEEDED)

    def test_convenience_requires_correct_state(self, orch):
        run_id = orch.start_task(_task())
        # cannot finish verification before entering VERIFYING
        with pytest.raises(RunNotTransitionableError):
            orch.finish_verification(run_id)

    def test_mark_plan_ready_requires_planning(self, orch):
        run_id = orch.start_task(_task())
        # run is in GATHERING_CONTEXT, not PLANNING
        with pytest.raises(RunNotTransitionableError):
            orch.mark_plan_ready(run_id)


# ---------------------------------------------------------------------- #
# Persistence (plan §A5 §5.2 restart recovery)
# ---------------------------------------------------------------------- #


class TestPersistence:
    def test_export_contains_runs_and_tasks(self, orch):
        orch.start_task(_task("s1"))
        state = orch.export_state()
        assert len(state["runs"]) == 1
        assert len(state["tasks"]) == 1
        assert "s1" in state["active_run_by_session"]

    def test_restore_recovers_state_and_usage(self, orch):
        run_id = orch.start_task(_task("s1"))
        orch.mark_context_ready(run_id)
        orch.record_usage(run_id, ContextUsage(actual_prompt=100, actual_completion=30))
        state = orch.export_state()

        fresh = AgentOrchestrator()
        count = fresh.restore_state(state)
        assert count == 1
        restored = fresh.get_run(run_id)
        assert restored.state == AgentRunState.PLANNING
        assert restored.context_usage.actual_prompt == 100

    def test_terminal_run_stays_terminal_after_restore(self, orch):
        run_id = orch.start_task(_task("s1"))
        orch.request_cancel(run_id)
        state = orch.export_state()

        fresh = AgentOrchestrator()
        fresh.restore_state(state)
        assert fresh.get_run(run_id).state == AgentRunState.CANCELLED
        assert fresh.get_run(run_id).is_terminal
        # terminal run is not "active" for its session
        assert fresh.active_run_id("s1") is None

    def test_active_run_survives_roundtrip(self, orch):
        run_id = orch.start_task(_task("s1"))
        state = orch.export_state()
        fresh = AgentOrchestrator()
        fresh.restore_state(state)
        # non-terminal run remains the active run for its session
        assert fresh.active_run_id("s1") == run_id

    def test_corrupt_entries_skipped(self, orch):
        # A run dict missing task_id is invalid; it must be skipped, not crash.
        bad = {"runs": [{"run_id": "x"}], "tasks": [], "active_run_by_session": {}}
        count = orch.restore_state(bad)
        assert count == 0

    def test_paused_run_restores_paused(self, orch):
        run_id = orch.start_task(_task("s1"))
        orch.mark_context_ready(run_id)
        orch.mark_plan_ready(run_id)
        orch.begin_verification(run_id)
        orch.pause_for_resume(run_id)
        state = orch.export_state()

        fresh = AgentOrchestrator()
        fresh.restore_state(state)
        restored = fresh.get_run(run_id)
        # Plan §2.7 / §10.2 §7: a paused run stays paused; the host must
        # re-validate before resuming.
        assert restored.state == AgentRunState.PAUSED_FOR_RESUME
        assert restored.is_user_blocked
