"""OS-06: Mission / Plan workspace view-model.

Converts agent run state (``AgentRun`` + optional ``AgentCheckpoint`` /
plan-step iterable, or the legacy mock result shape used by the OS-06 tests)
into a Qt-free dict that the assistant panel can render as a compact
workspace card.  The user can see at a glance:

* mission status (running / waiting / succeeded / failed / cancelled)
* current phase (observe / plan / gate / act / verify / recover)
* current step and its tool
* progress (completed / total steps)
* accumulated cost
* diagnostics (errors and warnings)
* available actions (cancel / pause / resume / retry)

This module is intentionally free of Qt imports so it can be unit-tested
without a display server.
"""
from __future__ import annotations

from typing import Any, Mapping


#: AgentRunState values (agent_models) mapped onto the view's status badge set.
_RUN_STATE_TO_VIEW_STATUS: Mapping[str, str] = {
    "created": "running",
    "gathering_context": "running",
    "planning": "running",
    "executing": "running",
    "verifying": "running",
    "waiting_user_input": "waiting_for_user",
    "waiting_approval": "waiting_for_user",
    "paused_for_resume": "waiting_for_user",
    "succeeded": "succeeded",
    "partial_success": "succeeded",
    "failed": "failed",
    "cancelled": "cancelled",
}


def build_mission_workspace_view(
    *,
    status: str,
    phase: str = "",
    current_step_id: str = "",
    current_tool_name: str = "",
    completed_step_ids: tuple[str, ...] = (),
    total_steps: int = 0,
    cost_units: float = 0.0,
    plan_hash: str = "",
    run_id: str = "",
    diagnostics: tuple[str, ...] = (),
    rollback_failures: tuple[str, ...] = (),
    reason: str = "",
    steps_writing_timeline: int = 0,
    steps_requiring_approval: int = 0,
) -> dict[str, Any]:
    """Build a workspace view-model dict from agent run state.

    Parameters accept plain strings (not enum members) so callers can pass
    ``status.value`` or the string directly.  All parameters are keyword-only.
    """
    completed = len(completed_step_ids)
    progress_pct = round(completed / total_steps * 100, 1) if total_steps > 0 else 0.0

    # Status badge metadata for UI styling.
    status_meta = {
        "running": {"color": "#3b82f6", "icon": "sync", "label": "Running"},
        "succeeded": {"color": "#22c55e", "icon": "check", "label": "Succeeded"},
        "waiting_for_user": {"color": "#f59e0b", "icon": "hourglass", "label": "Waiting for user"},
        "failed": {"color": "#ef4444", "icon": "error", "label": "Failed"},
        "cancelled": {"color": "#6b7280", "icon": "cancel", "label": "Cancelled"},
    }
    badge = status_meta.get(str(status), {"color": "#6b7280", "icon": "help", "label": str(status)})

    # Available actions depend on status.
    if status == "running":
        actions = ["cancel", "pause"]
    elif status == "waiting_for_user":
        actions = ["resume", "cancel", "retry"]
    elif status == "failed":
        actions = ["retry", "cancel"]
    elif status == "succeeded":
        actions = ["close"]
    else:
        actions = ["close"]

    # Severity classification of diagnostics.
    error_diagnostics = [d for d in diagnostics if any(
        kw in d.lower() for kw in ("error", "fail", "exception", "traceback")
    )]
    warning_diagnostics = [d for d in diagnostics if d not in error_diagnostics and any(
        kw in d.lower() for kw in ("warn", "stale", "retry", "degrad")
    )]
    info_diagnostics = [d for d in diagnostics if d not in error_diagnostics and d not in warning_diagnostics]

    return {
        "run_id": str(run_id),
        "status": str(status),
        "status_badge": badge,
        "phase": str(phase),
        "current_step_id": str(current_step_id),
        "current_tool_name": str(current_tool_name),
        "completed_steps": completed,
        "total_steps": int(total_steps),
        "progress_pct": progress_pct,
        "cost_units": round(float(cost_units), 4),
        "plan_hash": str(plan_hash),
        "reason": str(reason),
        "diagnostics": {
            "errors": list(error_diagnostics),
            "warnings": list(warning_diagnostics),
            "info": list(info_diagnostics),
        },
        "rollback_failures": list(rollback_failures),
        "risk_flags": {
            "writes_timeline": int(steps_writing_timeline),
            "requires_approval": int(steps_requiring_approval),
        },
        "actions": actions,
    }


def _string_value(value: Any) -> str:
    """Render a raw/enum status or phase value as its plain string."""
    return str(value.value) if hasattr(value, "value") else str(value)


def _to_str_tuple(value: Any) -> tuple[str, ...]:
    """Coerce an iterable-of-str field into a tuple, tolerating None/scalars."""
    if not value:
        return ()
    try:
        return tuple(str(item) for item in value)
    except TypeError:
        return (str(value),)


def workspace_from_run_result(result: Any) -> dict[str, Any]:
    """Build a workspace view from agent run state (AUD-13917 修复后契约).

    库中**不存在** ``AgentRunResult`` 类型；真实可用的对象是
    ``AgentRun``（``state`` / ``state_reason`` / ``run_id``）与
    ``AgentCheckpoint``（``state`` / ``completed_steps``，注意字段名是
    completed_steps 而非 completed_step_ids，且没有 phase/current_step_id/
    cost_units/reason）。本函数对两种形状都工作：

    - 真实形状：``result.state``（AgentRunState 字符串）+
      ``result.checkpoint.completed_steps`` + ``result.state_reason``；
    - 旧 mock 形状（tests/ai/test_os06_mission_workspace.py）：
      ``result.status`` / ``checkpoint.current_step_id`` / ``cp.cost_units``。

    所有字段访问都是防御性的：真实类型不携带的属性（cost、逐步工具名、
    diagnostics）退化为中性默认值而不是抛 AttributeError。
    """
    checkpoint = getattr(result, "checkpoint", None)
    plan = getattr(result, "plan", None)
    steps: tuple[Any, ...] = ()
    if plan is not None:
        steps = tuple(getattr(plan, "steps", ()) or ())

    # Count risk-relevant steps.
    steps_writing = 0
    steps_needing_approval = 0
    for step in steps:
        if getattr(step, "writes_timeline", False):
            steps_writing += 1
        if getattr(step, "requires_approval", False):
            steps_needing_approval += 1

    # Find current step's tool name from the plan (mock shape only; the real
    # AgentCheckpoint carries no current_step_id).
    current_step_id = str(getattr(checkpoint, "current_step_id", "") or "")
    current_tool = ""
    if current_step_id:
        for step in steps:
            if str(getattr(step, "step_id", "")) == current_step_id:
                current_tool = str(getattr(step, "tool_name", ""))
                break

    # Status: mock uses ``status``; AgentRun uses ``state`` (an AgentRunState
    # value like "waiting_user_input"), mapped onto the view's badge set.
    status_raw = getattr(result, "status", None)
    if status_raw is None:
        status_raw = getattr(result, "state", "")
    status = _string_value(status_raw)
    status = _RUN_STATE_TO_VIEW_STATUS.get(status, status)

    # Phase: mock checkpoints carry ``phase``; AgentCheckpoint carries
    # ``state``; fall back to the run status.
    phase_raw = getattr(checkpoint, "phase", None)
    if phase_raw is None:
        phase_raw = getattr(checkpoint, "state", None)
    if phase_raw is None:
        phase_raw = status_raw
    phase = _string_value(phase_raw)

    # Completed steps: real field is ``completed_steps``; mock uses
    # ``completed_step_ids``.
    completed_step_ids = getattr(checkpoint, "completed_steps", None)
    if completed_step_ids is None:
        completed_step_ids = getattr(checkpoint, "completed_step_ids", ())
    completed = _to_str_tuple(completed_step_ids)

    # Reason: real runs carry ``state_reason`` on the run itself.
    reason = str(
        getattr(checkpoint, "reason", "") or getattr(result, "state_reason", "") or ""
    )

    def _as_float(value: Any) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0

    return build_mission_workspace_view(
        status=status,
        phase=phase,
        current_step_id=current_step_id,
        current_tool_name=current_tool,
        completed_step_ids=completed,
        total_steps=len(steps) if plan is not None else 0,
        cost_units=_as_float(getattr(checkpoint, "cost_units", 0.0)),
        plan_hash=str(getattr(plan, "plan_hash", "") or ""),
        run_id=str(
            getattr(checkpoint, "run_id", "") or getattr(result, "run_id", "") or ""
        ),
        diagnostics=_to_str_tuple(getattr(result, "diagnostics", ())),
        rollback_failures=_to_str_tuple(getattr(result, "rollback_failures", ())),
        reason=reason,
        steps_writing_timeline=steps_writing,
        steps_requiring_approval=steps_needing_approval,
    )


__all__ = [
    "build_mission_workspace_view",
    "workspace_from_run_result",
]
