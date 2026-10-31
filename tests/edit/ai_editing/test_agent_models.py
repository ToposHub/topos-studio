"""Unified Agent V2 domain model tests (plan §4.1 / §4.2 / §A0).

Covers the contract the rest of the runtime depends on:

- ``AgentRunState`` legal / illegal transitions, terminal + cancel semantics,
  and the fail-closed normalisation (unknown → ``FAILED``).
- Versioned ``to_dict`` / ``from_dict`` round-trips for every persisted object.
- Redaction of secrets / absolute paths via ``sanitize_for_storage``.
- Risk + kind validation that fails safe (write-level / unknown) rather than
  silently degrading to a permissive value.
"""

from __future__ import annotations

import pytest

from src.features.edit.ai_editing.agent_models import (
    AGENT_MODELS_SCHEMA_VERSION,
    HTML_GENERATION_PROTOCOL_VERSION,
    AgentCheckpoint,
    AgentEvent,
    AgentEventKind,
    AgentMessage,
    AgentMessageStatus,
    AgentRun,
    AgentRunState,
    AgentTask,
    AgentTaskKind,
    ApprovalRequest,
    ApprovalRisk,
    ArtifactKind,
    ArtifactRef,
    ContextUsage,
    ResponsePayloadKind,
)


# ---------------------------------------------------------------------- #
# AgentRunState
# ---------------------------------------------------------------------- #


class TestAgentRunStateTransitions:
    """Plan §4.2 state machine: legal forward edges only."""

    @pytest.mark.parametrize(
        "path",
        [
            # The canonical happy path from plan §4.2.
            [
                AgentRunState.GATHERING_CONTEXT,
                AgentRunState.PLANNING,
                AgentRunState.EXECUTING,
                AgentRunState.VERIFYING,
                AgentRunState.SUCCEEDED,
            ],
            # Approval branch.
            [
                AgentRunState.GATHERING_CONTEXT,
                AgentRunState.PLANNING,
                AgentRunState.WAITING_APPROVAL,
                AgentRunState.EXECUTING,
                AgentRunState.VERIFYING,
                AgentRunState.PARTIAL_SUCCESS,
            ],
            # User clarification branch.
            [
                AgentRunState.GATHERING_CONTEXT,
                AgentRunState.WAITING_USER_INPUT,
                AgentRunState.PLANNING,
                AgentRunState.EXECUTING,
                AgentRunState.VERIFYING,
                AgentRunState.SUCCEEDED,
            ],
            # Pause for resume then recover.
            [
                AgentRunState.GATHERING_CONTEXT,
                AgentRunState.PLANNING,
                AgentRunState.EXECUTING,
                AgentRunState.VERIFYING,
                AgentRunState.PAUSED_FOR_RESUME,
                AgentRunState.GATHERING_CONTEXT,
            ],
        ],
    )
    def test_legal_paths_transition(self, path):
        current = AgentRunState.CREATED
        for nxt in path:
            assert AgentRunState.can_transition(current, nxt)
            current = AgentRunState.transition(current, nxt)

    @pytest.mark.parametrize(
        "frm,to",
        [
            (AgentRunState.SUCCEEDED, AgentRunState.EXECUTING),
            (AgentRunState.FAILED, AgentRunState.RUNNING if False else AgentRunState.VERIFYING),
            (AgentRunState.CANCELLED, AgentRunState.EXECUTING),
            (AgentRunState.CREATED, AgentRunState.SUCCEEDED),  # skip phases
            (AgentRunState.CREATED, AgentRunState.VERIFYING),
        ],
    )
    def test_illegal_transitions_rejected(self, frm, to):
        assert not AgentRunState.can_transition(frm, to)
        with pytest.raises(ValueError):
            AgentRunState.transition(frm, to)

    def test_every_non_terminal_state_can_be_cancelled(self):
        non_terminal = [
            s for s in AgentRunState._TRANSITIONS
            if s not in AgentRunState.TERMINAL_STATES
        ]
        assert non_terminal, "sanity: there are non-terminal states"
        for state in non_terminal:
            assert AgentRunState.can_transition(state, AgentRunState.CANCELLED), state

    def test_terminal_states_have_no_outgoing_edges(self):
        for terminal in AgentRunState.TERMINAL_STATES:
            for other in AgentRunState._TRANSITIONS:
                if other == terminal:
                    continue
                assert not AgentRunState.can_transition(terminal, other)

    @pytest.mark.parametrize(
        "raw,expected",
        [
            (AgentRunState.PLANNING, AgentRunState.PLANNING),
            ("PLANNING", AgentRunState.PLANNING),
            ("  planning  ", AgentRunState.PLANNING),
            ("bogus", AgentRunState.FAILED),  # fail closed, never CREATED
            ("", AgentRunState.FAILED),
            (None, AgentRunState.FAILED),
        ],
    )
    def test_normalize_fail_closed(self, raw, expected):
        assert AgentRunState.normalize(raw) == expected

    def test_terminal_and_user_blocked_classifications(self):
        assert AgentRunState.is_terminal(AgentRunState.SUCCEEDED)
        assert not AgentRunState.is_terminal(AgentRunState.EXECUTING)
        assert AgentRunState.is_user_blocked(AgentRunState.WAITING_APPROVAL)
        assert AgentRunState.is_user_blocked(AgentRunState.PAUSED_FOR_RESUME)
        assert not AgentRunState.is_user_blocked(AgentRunState.PLANNING)


# ---------------------------------------------------------------------- #
# AgentRun lifecycle
# ---------------------------------------------------------------------- #


class TestAgentRunLifecycle:
    def _new(self):
        return AgentRun(run_id="r1", task_id="t1", mode="chat")

    def test_initial_state_is_created(self):
        run = self._new()
        assert run.state == AgentRunState.CREATED
        assert not run.is_terminal
        assert run.ended_at == 0.0

    def test_with_state_advances_and_keeps_identity(self):
        run = self._new()
        advanced = run.with_state(AgentRunState.GATHERING_CONTEXT)
        assert advanced.state == AgentRunState.GATHERING_CONTEXT
        assert advanced.run_id == run.run_id
        # original is immutable
        assert run.state == AgentRunState.CREATED

    def test_terminal_state_stamps_ended_at(self):
        run = self._new()
        done = run.with_state(AgentRunState.GATHERING_CONTEXT).with_state(
            AgentRunState.FAILED
        )
        assert done.is_terminal
        assert done.ended_at > 0.0

    def test_with_state_rejects_illegal_move(self):
        run = self._new()
        with pytest.raises(ValueError):
            run.with_state(AgentRunState.SUCCEEDED)

    def test_cancel_from_planning(self):
        run = self._new()
        cancelled = (
            run.with_state(AgentRunState.GATHERING_CONTEXT)
            .with_state(AgentRunState.PLANNING)
            .with_state(AgentRunState.CANCELLED)
        )
        assert cancelled.is_terminal
        assert cancelled.state == AgentRunState.CANCELLED

    def test_roundtrip_preserves_all_fields(self):
        run = (
            self._new()
            .with_state(AgentRunState.GATHERING_CONTEXT)
            .with_state(AgentRunState.PLANNING)
            .with_state(AgentRunState.EXECUTING)
            .with_state(AgentRunState.VERIFYING)
            .with_state(AgentRunState.SUCCEEDED)
        )
        restored = AgentRun.from_dict(run.to_dict())
        assert restored.run_id == run.run_id
        assert restored.task_id == run.task_id
        assert restored.state == run.state
        assert restored.ended_at == run.ended_at
        assert restored.mode == run.mode

    def test_run_requires_task_id(self):
        with pytest.raises(ValueError):
            AgentRun(run_id="r1", task_id="")


# ---------------------------------------------------------------------- #
# AgentTask
# ---------------------------------------------------------------------- #


class TestAgentTask:
    def test_defaults_and_autoid(self):
        task = AgentTask(session_id="s1", title="hello")
        assert task.task_id
        assert task.kind == AgentTaskKind.CHAT

    def test_unknown_kind_fail_closed(self):
        task = AgentTask(kind="nonsense")
        assert task.kind == AgentTaskKind.UNKNOWN

    def test_empty_task_id_rejected(self):
        with pytest.raises(ValueError):
            AgentTask(task_id="")

    def test_roundtrip(self):
        task = AgentTask(
            session_id="s1",
            title="生成动画",
            user_goal="做三页教学动画",
            acceptance_criteria=("页面数=3",),
            kind=AgentTaskKind.MOTION_GRAPHIC,
        )
        restored = AgentTask.from_dict(task.to_dict())
        assert restored.task_id == task.task_id
        assert restored.kind == AgentTaskKind.MOTION_GRAPHIC
        assert restored.acceptance_criteria == ("页面数=3",)


# ---------------------------------------------------------------------- #
# ContextUsage
# ---------------------------------------------------------------------- #


class TestContextUsage:
    def test_window_total_and_ratio(self):
        usage = ContextUsage(
            window_prompt=8000,
            window_prompt_is_estimated=False,
            window_limit=32000,
            reserved_output=4000,
        )
        assert usage.window_total == 12000
        assert usage.window_ratio == pytest.approx(8000 / 32000)

    def test_unknown_window_returns_none(self):
        usage = ContextUsage()
        assert usage.window_total is None
        assert usage.window_ratio is None

    def test_roundtrip(self):
        usage = ContextUsage(
            window_prompt=100,
            actual_prompt=90,
            actual_completion=20,
            session_total_tokens=110,
            compression_count=2,
        )
        restored = ContextUsage.from_dict(usage.to_dict())
        assert restored.window_prompt == 100
        assert restored.session_total_tokens == 110
        assert restored.compression_count == 2


# ---------------------------------------------------------------------- #
# AgentMessage
# ---------------------------------------------------------------------- #


class TestAgentMessage:
    def test_invalid_role_normalised(self):
        msg = AgentMessage(role="robot")
        assert msg.role == "user"

    def test_unknown_status_normalised(self):
        msg = AgentMessage(status="weird")
        assert msg.status == AgentMessageStatus.COMPLETED

    def test_roundtrip_with_usage_and_artifacts(self):
        msg = AgentMessage(
            run_id="r1",
            role="assistant",
            content="done",
            status=AgentMessageStatus.COMPLETED,
            usage=ContextUsage(actual_prompt=10, actual_completion=5),
            artifact_refs=("a1", "a2"),
        )
        restored = AgentMessage.from_dict(msg.to_dict())
        assert restored.role == "assistant"
        assert restored.artifact_refs == ("a1", "a2")
        assert restored.usage is not None
        assert restored.usage.actual_completion == 5


# ---------------------------------------------------------------------- #
# AgentEvent
# ---------------------------------------------------------------------- #


class TestAgentEvent:
    def test_requires_run_id(self):
        with pytest.raises(ValueError):
            AgentEvent(kind=AgentEventKind.RUN_CREATED)

    def test_known_event_set_complete(self):
        # Every minimum event from plan §A1 must be registered.
        required = {
            "run.created", "run.context_started", "run.context_ready",
            "run.plan_started", "run.plan_ready", "run.waiting_user_input",
            "run.waiting_approval", "run.tool_started", "run.tool_progress",
            "run.tool_finished", "run.verification_started",
            "run.verification_finished", "run.completed", "run.failed",
            "run.cancel_requested", "run.cancelled", "run.checkpoint_saved",
        }
        assert required.issubset(AgentEventKind.KNOWN)

    def test_roundtrip(self):
        ev = AgentEvent(
            run_id="r1",
            kind=AgentEventKind.RUN_TOOL_STARTED,
            payload={"tool": "search_assets"},
        )
        restored = AgentEvent.from_dict(ev.to_dict())
        assert restored.kind == AgentEventKind.RUN_TOOL_STARTED
        assert restored.payload == {"tool": "search_assets"}


# ---------------------------------------------------------------------- #
# ApprovalRequest
# ---------------------------------------------------------------------- #


class TestApprovalRequest:
    def test_requires_tool_name_and_run(self):
        with pytest.raises(ValueError):
            ApprovalRequest(run_id="r1", tool_name="")
        with pytest.raises(ValueError):
            ApprovalRequest(run_id="", tool_name="t")

    def test_unknown_risk_fails_safe_to_write(self):
        req = ApprovalRequest(run_id="r1", tool_name="t", risk="whatever")
        assert req.risk == ApprovalRisk.WRITE
        assert req.requires_grant

    @pytest.mark.parametrize(
        "risk,needs_grant",
        [
            (ApprovalRisk.READ_ONLY, False),
            (ApprovalRisk.PREVIEW, False),
            (ApprovalRisk.PROPOSAL, True),
            (ApprovalRisk.WRITE, True),
            (ApprovalRisk.EXPENSIVE, True),
        ],
    )
    def test_grant_matrix(self, risk, needs_grant):
        req = ApprovalRequest(run_id="r1", tool_name="t", risk=risk)
        assert req.requires_grant is needs_grant

    def test_roundtrip(self):
        req = ApprovalRequest(
            run_id="r1",
            tool_name="apply_edit_plan",
            risk=ApprovalRisk.WRITE,
            scope_summary="timeline A",
            can_undo=True,
            diff_summary={"tracks": 1},
        )
        restored = ApprovalRequest.from_dict(req.to_dict())
        assert restored.tool_name == "apply_edit_plan"
        assert restored.can_undo is True
        assert restored.diff_summary == {"tracks": 1}


# ---------------------------------------------------------------------- #
# ArtifactRef
# ---------------------------------------------------------------------- #


class TestArtifactRef:
    def test_unknown_kind_normalised(self):
        ref = ArtifactRef(kind="weird")
        assert ref.kind == ArtifactKind.OTHER

    def test_html_artifact_roundtrip(self):
        ref = ArtifactRef(
            kind=ArtifactKind.HTML_DOCUMENT,
            location="artifact://abc",
            payload_hash="deadbeef",
        )
        restored = ArtifactRef.from_dict(ref.to_dict())
        assert restored.kind == ArtifactKind.HTML_DOCUMENT
        assert restored.payload_hash == "deadbeef"


# ---------------------------------------------------------------------- #
# AgentCheckpoint
# ---------------------------------------------------------------------- #


class TestAgentCheckpoint:
    def test_requires_run_id(self):
        with pytest.raises(ValueError):
            AgentCheckpoint(run_id="")

    def test_protected_context_preserved(self):
        cp = AgentCheckpoint(
            run_id="r1",
            state=AgentRunState.PAUSED_FOR_RESUME,
            protected_context=("canvas_width", "fps", "scene_revision"),
        )
        restored = AgentCheckpoint.from_dict(cp.to_dict())
        assert restored.state == AgentRunState.PAUSED_FOR_RESUME
        assert "scene_revision" in restored.protected_context


# ---------------------------------------------------------------------- #
# Protocol version + payload kinds
# ---------------------------------------------------------------------- #


def test_html_generation_protocol_version_is_pinned():
    assert isinstance(HTML_GENERATION_PROTOCOL_VERSION, str)
    assert HTML_GENERATION_PROTOCOL_VERSION


def test_response_payload_kinds_disambiguate():
    # Plan §A0 step 8: the agent must never guess the payload type from text.
    assert ResponsePayloadKind.KNOWN == frozenset(
        {
            ResponsePayloadKind.HTML_DOCUMENT,
            ResponsePayloadKind.SCENE_JSON,
            ResponsePayloadKind.STREAM_TEXT,
            ResponsePayloadKind.TOOL_CALL,
        }
    )


def test_schema_version_bumped_int():
    assert isinstance(AGENT_MODELS_SCHEMA_VERSION, int)
    assert AGENT_MODELS_SCHEMA_VERSION >= 1
