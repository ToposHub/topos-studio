"""Replan / critique decision policy (OS-02).

OS-02 expands the agent loop's 3-value ``RecoveryAction`` (RETRY /
WAIT_FOR_USER / FAIL) into a richer :class:`ReplanDecision` vocabulary and
adds the *triggers* that produce one.  The plan asks for six decision kinds:

    CONTINUE  / RETRY_LOCALLY / SWITCH_TOOL / REQUEST_CLARIFICATION /
    ROLLBACK  / FINISH

and for critique-driven triggering (not just "action failed" but also
"preview quality gate warned", "user feedback patch arrived", "revision
conflict").  This module provides both the decision type and a
:class:`ReplanPolicy` that maps trigger signals to a decision while enforcing
hard cycle / cost / tool-call limits.

The module is deliberately decoupled from ``agent_loop.py`` — the loop calls
:meth:`ReplanPolicy.decide` after each observe / act / verify step and acts
on the returned decision.  This keeps the loop's existing 3-value
``RecoveryAction`` path working while the richer decisions layer on top.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping


# --------------------------------------------------------------------------- #
# Decision vocabulary (OS-02: six kinds)
# --------------------------------------------------------------------------- #


class ReplanDecisionKind(str):
    """What the agent should do next after a critique."""


DECISION_CONTINUE = "continue"                     # everything on track, proceed
DECISION_RETRY_LOCALLY = "retry_locally"           # redo the current step with adjustments
DECISION_SWITCH_TOOL = "switch_tool"               # current tool is wrong, try an alternative
DECISION_REQUEST_CLARIFICATION = "request_clarification"  # ask the user
DECISION_ROLLBACK = "rollback"                      # undo and replan from a safe point
DECISION_FINISH = "finish"                          # goal met or hard limit hit

ALL_DECISIONS: frozenset[str] = frozenset({
    DECISION_CONTINUE, DECISION_RETRY_LOCALLY, DECISION_SWITCH_TOOL,
    DECISION_REQUEST_CLARIFICATION, DECISION_ROLLBACK, DECISION_FINISH,
})


# --------------------------------------------------------------------------- #
# Triggers
# --------------------------------------------------------------------------- #


TRIGGER_ACTION_SUCCEEDED = "action_succeeded"
TRIGGER_ACTION_FAILED = "action_failed"
TRIGGER_VERIFY_PASSED = "verify_passed"
TRIGGER_VERIFY_WARNINGS = "verify_warnings"
TRIGGER_VERIFY_FAILED = "verify_failed"
TRIGGER_QUALITY_GATE_WARNING = "quality_gate_warning"
TRIGGER_USER_FEEDBACK = "user_feedback"
TRIGGER_REVISION_CONFLICT = "revision_conflict"
TRIGGER_BUDGET_EXHAUSTED = "budget_exhausted"
TRIGGER_CYCLE_LIMIT = "cycle_limit"
TRIGGER_TOOL_LIMIT = "tool_limit"
TRIGGER_CANCEL_REQUESTED = "cancel_requested"

ALL_TRIGGERS: frozenset[str] = frozenset({
    TRIGGER_ACTION_SUCCEEDED, TRIGGER_ACTION_FAILED,
    TRIGGER_VERIFY_PASSED, TRIGGER_VERIFY_WARNINGS, TRIGGER_VERIFY_FAILED,
    TRIGGER_QUALITY_GATE_WARNING, TRIGGER_USER_FEEDBACK,
    TRIGGER_REVISION_CONFLICT, TRIGGER_BUDGET_EXHAUSTED,
    TRIGGER_CYCLE_LIMIT, TRIGGER_TOOL_LIMIT, TRIGGER_CANCEL_REQUESTED,
})


# --------------------------------------------------------------------------- #
# Decision
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ReplanDecision:
    """One critique outcome.

    Attributes:
        kind: One of :data:`ALL_DECISIONS`.
        trigger: What produced this decision.
        reason: Short human-readable explanation (sanitized by caller).
        alternative_tool: Suggested tool id when ``kind == SWITCH_TOOL``.
        clarification_question: Question for the user when
            ``kind == REQUEST_CLARIFICATION``.
        rollback_to_step: Step id to return to when ``kind == ROLLBACK``.
        adjustments: Mapping of parameter tweaks to apply on retry.
    """

    kind: str
    trigger: str
    reason: str = ""
    alternative_tool: str | None = None
    clarification_question: str | None = None
    rollback_to_step: str | None = None
    adjustments: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.kind not in ALL_DECISIONS:
            raise ValueError(f"Unknown ReplanDecision kind: {self.kind!r}")
        if self.trigger not in ALL_TRIGGERS:
            raise ValueError(f"Unknown trigger: {self.trigger!r}")

    @property
    def is_terminal(self) -> bool:
        return self.kind == DECISION_FINISH

    @property
    def needs_user_input(self) -> bool:
        return self.kind in {DECISION_REQUEST_CLARIFICATION, DECISION_ROLLBACK}

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "trigger": self.trigger,
            "reason": self.reason,
            "alternative_tool": self.alternative_tool,
            "clarification_question": self.clarification_question,
            "rollback_to_step": self.rollback_to_step,
            "adjustments": dict(self.adjustments),
        }


# --------------------------------------------------------------------------- #
# Policy
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ReplanLimits:
    """Hard limits that prevent unbounded replan loops."""

    max_cycles: int = 3
    max_retries_per_step: int = 2
    max_tool_switches: int = 2
    max_clarifications: int = 3


class ReplanPolicy:
    """Map trigger signals to :class:`ReplanDecision` under bounded limits.

    The policy is stateful: it tracks cycles, retries, tool switches and
    clarifications so that repeated failures escalate (retry → switch →
    rollback → finish) and repeated clarification requests hit the
    ``max_clarifications`` cap instead of looping forever.
    """

    def __init__(self, limits: ReplanLimits | None = None) -> None:
        self._limits = limits or ReplanLimits()
        self._cycle = 0
        self._retries: dict[str, int] = {}        # step_id → retry count
        self._tool_switches = 0
        self._clarifications = 0

    # ---------------- state ---------------- #

    @property
    def cycle(self) -> int:
        return self._cycle

    def reset(self) -> None:
        self._cycle = 0
        self._retries.clear()
        self._tool_switches = 0
        self._clarifications = 0

    def begin_cycle(self) -> None:
        self._cycle += 1

    # ---------------- decision ---------------- #

    def decide(
        self,
        trigger: str,
        *,
        step_id: str = "",
        reason: str = "",
        alternative_tool: str | None = None,
        clarification_question: str | None = None,
        rollback_to_step: str | None = None,
        adjustments: Mapping[str, Any] | None = None,
    ) -> ReplanDecision:
        """Evaluate a trigger and return the decision the loop should follow.

        Hard limits always win: once ``max_cycles`` / tool / clarification
        limits are hit, the decision is FINISH regardless of the trigger.
        """

        # Hard-limit escalations first.
        if self._cycle >= self._limits.max_cycles:
            return ReplanDecision(
                DECISION_FINISH, TRIGGER_CYCLE_LIMIT,
                reason=f"已达到最大循环次数 {self._limits.max_cycles}。",
            )
        if trigger == TRIGGER_CANCEL_REQUESTED:
            return ReplanDecision(DECISION_FINISH, trigger, reason="用户取消了任务。")
        if trigger == TRIGGER_BUDGET_EXHAUSTED:
            return ReplanDecision(DECISION_FINISH, trigger, reason="预算已用尽。")
        # AUD-14306：``max_clarifications`` 此前声明但从未执行——修订冲突/
        # 失败兜底路径可无限次产出 REQUEST_CLARIFICATION（"反复问用户"死循环）。
        # 现在达到上限后无论 trigger 是什么都强制 FINISH（docstring 承诺的
        # "Hard limits always win"）。
        if self._clarifications >= self._limits.max_clarifications:
            return ReplanDecision(
                DECISION_FINISH, trigger,
                reason=f"已达到最大澄清次数 {self._limits.max_clarifications}。",
            )

        # Critique-driven triggers.
        if trigger in {TRIGGER_VERIFY_PASSED, TRIGGER_ACTION_SUCCEEDED}:
            return ReplanDecision(DECISION_CONTINUE, trigger, reason=reason)

        if trigger == TRIGGER_VERIFY_WARNINGS or trigger == TRIGGER_QUALITY_GATE_WARNING:
            # Warrants a local retry if we haven't exhausted retries for this step.
            if self._retries.get(step_id, 0) < self._limits.max_retries_per_step:
                self._retries[step_id] = self._retries.get(step_id, 0) + 1
                return ReplanDecision(
                    DECISION_RETRY_LOCALLY, trigger, reason=reason,
                    adjustments=dict(adjustments or {}),
                )
            # Otherwise escalate to rollback.
            return ReplanDecision(
                DECISION_ROLLBACK, trigger, reason=reason,
                rollback_to_step=rollback_to_step,
            )

        if trigger == TRIGGER_REVISION_CONFLICT:
            self._clarifications += 1
            return ReplanDecision(
                DECISION_REQUEST_CLARIFICATION, trigger,
                reason=reason or "上游 revision 已变化，需要重新确认。",
                clarification_question="工程已变更，是否基于当前状态重新规划？",
            )

        if trigger == TRIGGER_USER_FEEDBACK:
            return ReplanDecision(
                DECISION_CONTINUE, trigger, reason=reason,
                adjustments=dict(adjustments or {}),
            )

        if trigger == TRIGGER_ACTION_FAILED or trigger == TRIGGER_VERIFY_FAILED:
            # Try local retry first, then switch tool, then rollback, then finish.
            retries = self._retries.get(step_id, 0)
            if retries < self._limits.max_retries_per_step:
                self._retries[step_id] = retries + 1
                return ReplanDecision(
                    DECISION_RETRY_LOCALLY, trigger, reason=reason,
                    adjustments=dict(adjustments or {}),
                )
            if alternative_tool and self._tool_switches < self._limits.max_tool_switches:
                self._tool_switches += 1
                return ReplanDecision(
                    DECISION_SWITCH_TOOL, trigger, reason=reason,
                    alternative_tool=alternative_tool,
                )
            if rollback_to_step:
                return ReplanDecision(
                    DECISION_ROLLBACK, trigger, reason=reason,
                    rollback_to_step=rollback_to_step,
                )
            self._clarifications += 1
            return ReplanDecision(
                DECISION_REQUEST_CLARIFICATION, trigger,
                reason=reason or "多次重试失败，需要用户介入。",
                clarification_question=clarification_question,
            )

        if trigger == TRIGGER_TOOL_LIMIT:
            return ReplanDecision(
                DECISION_FINISH, trigger,
                reason="已达到最大工具调用次数。",
            )

        # Default: continue (the trigger is informational).
        return ReplanDecision(DECISION_CONTINUE, trigger, reason=reason)


__all__ = [
    "ReplanDecision",
    "ReplanDecisionKind",
    "ReplanPolicy",
    "ReplanLimits",
    "ALL_DECISIONS",
    "ALL_TRIGGERS",
    "DECISION_CONTINUE",
    "DECISION_RETRY_LOCALLY",
    "DECISION_SWITCH_TOOL",
    "DECISION_REQUEST_CLARIFICATION",
    "DECISION_ROLLBACK",
    "DECISION_FINISH",
    "TRIGGER_ACTION_SUCCEEDED",
    "TRIGGER_ACTION_FAILED",
    "TRIGGER_VERIFY_PASSED",
    "TRIGGER_VERIFY_WARNINGS",
    "TRIGGER_VERIFY_FAILED",
    "TRIGGER_QUALITY_GATE_WARNING",
    "TRIGGER_USER_FEEDBACK",
    "TRIGGER_REVISION_CONFLICT",
    "TRIGGER_BUDGET_EXHAUSTED",
    "TRIGGER_CYCLE_LIMIT",
    "TRIGGER_TOOL_LIMIT",
    "TRIGGER_CANCEL_REQUESTED",
]
