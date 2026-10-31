"""Agent phase presenter tests (plan §A3).

Validates the labels that make the agent's progress legible without exposing
the model's internal chain of thought:

- Active phases show progress and auto-continue.
- User-blocked states (approval / input / pause) stop the spinner.
- Terminal states are flagged; FAILED is an error colour.
- Tool events surface the tool name; bookkeeping events do not change the
  visible phase.
"""

from __future__ import annotations

import pytest

from src.features.edit.ai_editing.agent_models import AgentEventKind, AgentRunState
from src.features.edit.ai_editing.agent_phase_presenter import (
    is_user_blocked_state,
    present_event,
    present_state,
    terminal_label,
)


# ---------------------------------------------------------------------- #
# present_state
# ---------------------------------------------------------------------- #


class TestPresentState:
    @pytest.mark.parametrize(
        "state",
        [
            AgentRunState.CREATED,
            AgentRunState.GATHERING_CONTEXT,
            AgentRunState.PLANNING,
            AgentRunState.EXECUTING,
            AgentRunState.VERIFYING,
        ],
    )
    def test_active_states_auto_continue(self, state):
        pres = present_state(state)
        assert pres.auto_continue is True
        assert pres.is_terminal is False
        assert pres.kind == "active"
        assert pres.label  # non-empty

    @pytest.mark.parametrize(
        "state",
        [
            AgentRunState.GATHERING_CONTEXT,
            AgentRunState.PLANNING,
            AgentRunState.EXECUTING,
            AgentRunState.VERIFYING,
        ],
    )
    def test_active_states_show_progress(self, state):
        assert present_state(state).shows_progress is True

    @pytest.mark.parametrize(
        "state",
        [
            AgentRunState.WAITING_APPROVAL,
            AgentRunState.WAITING_USER_INPUT,
            AgentRunState.PAUSED_FOR_RESUME,
        ],
    )
    def test_user_blocked_states_stop_spinner(self, state):
        pres = present_state(state)
        assert pres.auto_continue is False
        assert pres.kind == "waiting"
        assert is_user_blocked_state(state)

    @pytest.mark.parametrize(
        "state,label",
        [
            (AgentRunState.WAITING_APPROVAL, "等待你的确认"),
            (AgentRunState.WAITING_USER_INPUT, "需要补充信息"),
            (AgentRunState.PAUSED_FOR_RESUME, "任务已暂停，可继续"),
        ],
    )
    def test_user_blocked_labels(self, state, label):
        assert present_state(state).label == label

    @pytest.mark.parametrize(
        "state",
        [
            AgentRunState.SUCCEEDED,
            AgentRunState.PARTIAL_SUCCESS,
            AgentRunState.FAILED,
            AgentRunState.CANCELLED,
        ],
    )
    def test_terminal_states_flagged(self, state):
        assert present_state(state).is_terminal is True

    def test_failed_is_error_kind(self):
        assert present_state(AgentRunState.FAILED).kind == "error"

    def test_succeeded_is_terminal_kind(self):
        assert present_state(AgentRunState.SUCCEEDED).kind == "terminal"

    def test_unknown_state_fails_closed(self):
        # Unknown / stale state must not become a permissive active label.
        pres = present_state("totally-bogus")
        assert pres.is_terminal is True
        assert pres.kind == "error"

    def test_progress_detail_passed_through(self):
        pres = present_state(AgentRunState.EXECUTING, progress_detail="2/3 步骤")
        assert pres.detail == "2/3 步骤"


# ---------------------------------------------------------------------- #
# present_event
# ---------------------------------------------------------------------- #


class TestPresentEvent:
    def test_tool_event_surfaces_tool_name(self):
        pres = present_event(
            AgentEventKind.RUN_TOOL_STARTED, payload={"tool_name": "search_assets"}
        )
        assert pres is not None
        assert "search_assets" in pres.label
        assert pres.shows_progress is True

    def test_tool_event_without_name_generic(self):
        pres = present_event(AgentEventKind.RUN_TOOL_PROGRESS)
        assert pres is not None
        assert "工具" in pres.label

    def test_approval_event_blocks(self):
        pres = present_event(AgentEventKind.RUN_WAITING_APPROVAL)
        assert pres is not None
        assert pres.auto_continue is False

    def test_completed_event_terminal(self):
        pres = present_event(AgentEventKind.RUN_COMPLETED)
        assert pres is not None
        assert pres.is_terminal is True

    def test_failed_event_error(self):
        pres = present_event(AgentEventKind.RUN_FAILED)
        assert pres is not None
        assert pres.is_terminal is True
        assert pres.kind == "error"

    @pytest.mark.parametrize(
        "kind",
        [AgentEventKind.RUN_USAGE_RECORDED, AgentEventKind.RUN_CHECKPOINT_SAVED],
    )
    def test_bookkeeping_events_do_not_change_phase(self, kind):
        # Plan: usage / checkpoint are bookkeeping; they must not flicker the
        # visible phase label.
        assert present_event(kind) is None

    def test_artifact_staged_shows_verification_hint(self):
        pres = present_event(AgentEventKind.ARTIFACT_STAGED)
        assert pres is not None
        assert "校验" in pres.label or "校验" in pres.detail

    def test_cancel_requested_in_progress(self):
        pres = present_event(AgentEventKind.RUN_CANCEL_REQUESTED)
        assert pres is not None
        assert not pres.is_terminal  # cancellation in flight, not yet terminal

    def test_context_events_show_gathering(self):
        for kind in (AgentEventKind.RUN_CREATED, AgentEventKind.RUN_CONTEXT_STARTED):
            pres = present_event(kind)
            assert pres is not None
            assert "上下文" in pres.label


# ---------------------------------------------------------------------- #
# helpers
# ---------------------------------------------------------------------- #


class TestHelpers:
    @pytest.mark.parametrize(
        "state,label",
        [
            (AgentRunState.SUCCEEDED, "已完成"),
            (AgentRunState.FAILED, "失败"),
            (AgentRunState.CANCELLED, "已取消"),
            (AgentRunState.PARTIAL_SUCCESS, "部分完成"),
        ],
    )
    def test_terminal_label(self, state, label):
        assert terminal_label(state) == label

    def test_active_state_not_user_blocked(self):
        assert not is_user_blocked_state(AgentRunState.PLANNING)
        assert not is_user_blocked_state(AgentRunState.EXECUTING)
