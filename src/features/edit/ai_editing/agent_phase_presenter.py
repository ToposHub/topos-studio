"""Map internal run state/events to safe, user-readable phase labels (plan §A3).

The thinking indicator must show *what the agent is doing right now* without
ever exposing the model's internal chain-of-thought (plan §3 note).  This
module is the single source of those labels so the Panel, the Run Drawer and
future surfaces all agree.

Two entry points:

- :func:`present_state` — render the current ``AgentRunState``.
- :func:`present_event` — render a just-emitted ``AgentEventKind`` (used for
  real-time phase updates, e.g. "正在调用：search_assets").

Labels follow the plan §A3 recommended copy table.  States that block
automatic continuation (``WAITING_APPROVAL`` / ``WAITING_USER_INPUT`` /
``PAUSED_FOR_RESUME``) set ``auto_continue=False`` so the UI knows to stop the
spinner and wait for the user.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from src.features.edit.ai_editing.agent_models import AgentEventKind, AgentRunState


# ---------------------------------------------------------------------- #
# Presentation object
# ---------------------------------------------------------------------- #


@dataclass(frozen=True)
class PhasePresentation:
    """A render-ready phase description for the UI."""

    label: str
    detail: str = ""
    shows_progress: bool = False
    #: Whether the run will continue on its own.  False for the three
    #: user-blocked states so the UI stops the spinner and waits.
    auto_continue: bool = True
    is_terminal: bool = False
    #: Coarse bucket the UI can colour-code: active / waiting / terminal / error.
    kind: str = "active"


# ---------------------------------------------------------------------- #
# State -> presentation
# ---------------------------------------------------------------------- #


def present_state(
    state: str,
    *,
    tool_name: str = "",
    progress_detail: str = "",
) -> PhasePresentation:
    """Render *state* into a user-facing phase (plan §A3 copy table)."""

    normalized = AgentRunState.normalize(state)
    if normalized == AgentRunState.CREATED:
        return PhasePresentation(label="正在准备任务", kind="active")
    if normalized == AgentRunState.GATHERING_CONTEXT:
        return PhasePresentation(
            label="正在收集上下文",
            detail=progress_detail,
            shows_progress=True,
            kind="active",
        )
    if normalized == AgentRunState.PLANNING:
        return PhasePresentation(
            label="正在制定方案",
            detail=progress_detail,
            shows_progress=True,
            kind="active",
        )
    if normalized == AgentRunState.EXECUTING:
        return PhasePresentation(
            label="正在执行变更",
            detail=progress_detail,
            shows_progress=True,
            kind="active",
        )
    if normalized == AgentRunState.VERIFYING:
        return PhasePresentation(
            label="正在校验结果",
            detail=progress_detail,
            shows_progress=True,
            kind="active",
        )
    if normalized == AgentRunState.WAITING_USER_INPUT:
        return PhasePresentation(
            label="需要补充信息",
            detail="请回答上方问题后继续",
            auto_continue=False,
            kind="waiting",
        )
    if normalized == AgentRunState.WAITING_APPROVAL:
        return PhasePresentation(
            label="等待你的确认",
            detail="请在确认卡上选择应用或取消",
            auto_continue=False,
            kind="waiting",
        )
    if normalized == AgentRunState.PAUSED_FOR_RESUME:
        return PhasePresentation(
            label="任务已暂停，可继续",
            detail=progress_detail or "已保存进度，重启后可恢复",
            auto_continue=False,
            kind="waiting",
        )
    if normalized == AgentRunState.SUCCEEDED:
        return PhasePresentation(label="已完成", is_terminal=True, kind="terminal")
    if normalized == AgentRunState.PARTIAL_SUCCESS:
        return PhasePresentation(
            label="部分完成", detail="部分步骤未通过校验", is_terminal=True, kind="terminal"
        )
    if normalized == AgentRunState.FAILED:
        return PhasePresentation(
            label="失败", detail=progress_detail, is_terminal=True, kind="error"
        )
    if normalized == AgentRunState.CANCELLED:
        return PhasePresentation(label="已取消", is_terminal=True, kind="terminal")
    # Unknown state normalises to FAILED, but guard defensively.
    return PhasePresentation(label="未知状态", is_terminal=True, kind="error")


# ---------------------------------------------------------------------- #
# Event -> presentation
# ---------------------------------------------------------------------- #


#: Event kinds that signal a tool boundary, rendered with the tool name.
_TOOL_EVENT_KINDS: frozenset[str] = frozenset(
    {AgentEventKind.RUN_TOOL_STARTED, AgentEventKind.RUN_TOOL_PROGRESS}
)


def present_event(
    event_kind: str,
    *,
    payload: Mapping[str, Any] | None = None,
) -> PhasePresentation | None:
    """Render a just-emitted event into a phase, or ``None`` if not phase-worthy.

    Some events (usage_recorded, checkpoint_saved) are bookkeeping and should
    not change the visible phase, so they return ``None``.
    """

    data = payload or {}
    if event_kind in _TOOL_EVENT_KINDS:
        tool = str(data.get("tool_name") or data.get("tool") or "")
        label = f"正在调用：{tool}" if tool else "正在调用工具"
        return PhasePresentation(label=label, shows_progress=True, kind="active")
    if event_kind in (AgentEventKind.RUN_CREATED, AgentEventKind.RUN_CONTEXT_STARTED):
        return PhasePresentation(label="正在收集上下文", shows_progress=True, kind="active")
    if event_kind == AgentEventKind.RUN_CONTEXT_READY:
        return PhasePresentation(label="正在制定方案", shows_progress=True, kind="active")
    if event_kind in (AgentEventKind.RUN_PLAN_STARTED, AgentEventKind.RUN_PLAN_READY):
        return PhasePresentation(label="正在制定方案", shows_progress=True, kind="active")
    if event_kind == AgentEventKind.RUN_WAITING_USER_INPUT:
        return PhasePresentation(
            label="需要补充信息", auto_continue=False, kind="waiting"
        )
    if event_kind == AgentEventKind.RUN_WAITING_APPROVAL:
        return PhasePresentation(
            label="等待你的确认", auto_continue=False, kind="waiting"
        )
    if event_kind in (
        AgentEventKind.RUN_VERIFICATION_STARTED,
        AgentEventKind.RUN_VERIFICATION_FINISHED,
    ):
        return PhasePresentation(label="正在校验结果", shows_progress=True, kind="active")
    if event_kind == AgentEventKind.RUN_COMPLETED:
        return PhasePresentation(label="已完成", is_terminal=True, kind="terminal")
    if event_kind == AgentEventKind.RUN_FAILED:
        return PhasePresentation(label="失败", is_terminal=True, kind="error")
    if event_kind in (
        AgentEventKind.RUN_CANCEL_REQUESTED,
        AgentEventKind.RUN_CANCELLED,
    ):
        return PhasePresentation(label="正在取消…", kind="active")
    if event_kind == AgentEventKind.ARTIFACT_STAGED:
        # Plan §A1 §11: artifact staged is a bookkeeping milestone; surface it
        # as a verification-prep hint without changing the active phase label.
        return PhasePresentation(
            label="正在校验结果", detail="已收到产物，开始校验", kind="active"
        )
    # Bookkeeping events (usage_recorded, checkpoint_saved) do not change the
    # visible phase.
    return None


# ---------------------------------------------------------------------- #
# Helpers
# ---------------------------------------------------------------------- #


def terminal_label(state: str) -> str:
    """Short label for a terminal state (used by the Run Drawer history)."""

    return present_state(state).label


def is_user_blocked_state(state: str) -> bool:
    """Whether *state* blocks automatic continuation."""

    return not present_state(state).auto_continue


__all__ = [
    "PhasePresentation",
    "is_user_blocked_state",
    "present_event",
    "present_state",
    "terminal_label",
]
