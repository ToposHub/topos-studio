"""Per-action execution contract: postcondition, estimated cost, compensation (OS-10).

OS-10 asks every write action to declare what it will check *after* execution
(postcondition), what it costs *before* execution (estimated cost), and how to
undo itself (compensation).  The existing ``transaction_service.preview``
produces a diff + diff_hash but no estimated_cost and no per-action
postcondition; ``autonomous_agent`` does post-commit verify but via a host
callback, not a declared contract.

This module defines :class:`ActionContract` — the declaration that travels
with a tool / plan step — plus :class:`PostconditionResult` for the verify
phase.  The host adapter evaluates postconditions after apply; any failure
triggers the declared compensation (rollback) automatically.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping


# --------------------------------------------------------------------------- #
# Postcondition kinds
# --------------------------------------------------------------------------- #

POST_CLIP_EXISTS = "clip_exists"
POST_CLIP_DURATION = "clip_duration"
POST_ASSET_RESOLVED = "asset_resolved"
POST_REVISION_ADVANCED = "revision_advanced"
POST_SCENE_HASH = "scene_hash"
POST_NODE_CONNECTED = "node_connected"
POST_GRADE_NODE_EXISTS = "grade_node_exists"
POST_FILE_EXISTS = "file_exists"
POST_FILE_NON_EMPTY = "file_non_empty"
POST_QUALITY_GATE_PASSED = "quality_gate_passed"

SUPPORTED_POSTCONDITIONS: frozenset[str] = frozenset({
    POST_CLIP_EXISTS, POST_CLIP_DURATION, POST_ASSET_RESOLVED,
    POST_REVISION_ADVANCED, POST_SCENE_HASH, POST_NODE_CONNECTED,
    POST_GRADE_NODE_EXISTS, POST_FILE_EXISTS, POST_FILE_NON_EMPTY,
    POST_QUALITY_GATE_PASSED,
})


# --------------------------------------------------------------------------- #
# Compensation strategies
# --------------------------------------------------------------------------- #

COMPENSATION_UNDO = "undo"                # call the paired undo tool
COMPENSATION_ROLLBACK_GROUP = "rollback_group"  # roll back the whole transaction group
COMPENSATION_MANUAL = "manual"            # flag for human intervention
COMPENSATION_NONE = "none"                # read-only / no side effect

ALL_COMPENSATION_STRATEGIES: frozenset[str] = frozenset({
    COMPENSATION_UNDO, COMPENSATION_ROLLBACK_GROUP, COMPENSATION_MANUAL, COMPENSATION_NONE,
})


# --------------------------------------------------------------------------- #
# Action contract
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class PostconditionSpec:
    """One post-execution check declaration."""

    kind: str
    target_ref: str = ""              # clip_id / node_id / file path hash
    expected: Mapping[str, Any] = field(default_factory=dict)
    severity: str = "error"           # error (block) / warning (advisory)

    def __post_init__(self) -> None:
        if self.kind not in SUPPORTED_POSTCONDITIONS:
            raise ValueError(f"Unknown postcondition kind: {self.kind!r}")
        if self.severity not in {"error", "warning"}:
            raise ValueError(f"severity must be error or warning, got {self.severity!r}")


@dataclass(frozen=True)
class CompensationSpec:
    """How to undo an action if a postcondition fails."""

    strategy: str = COMPENSATION_UNDO
    undo_tool: str | None = None
    description: str = ""

    def __post_init__(self) -> None:
        if self.strategy not in ALL_COMPENSATION_STRATEGIES:
            raise ValueError(f"Unknown compensation strategy: {self.strategy!r}")
        if self.strategy == COMPENSATION_UNDO and not self.undo_tool:
            raise ValueError("undo compensation requires undo_tool")


@dataclass(frozen=True)
class ActionContract:
    """OS-10 per-action declaration: cost + postcondition + compensation.

    Travel alongside every plan step / tool call.  The preview phase shows
    ``estimated_cost``; the verify phase evaluates ``postconditions``; on
    failure, ``compensation`` is applied.
    """

    action_id: str
    estimated_cost: float = 0.0
    estimated_duration_seconds: float = 0.0
    postconditions: tuple[PostconditionSpec, ...] = ()
    compensation: CompensationSpec = field(default_factory=lambda: CompensationSpec(strategy=COMPENSATION_NONE))
    risk_flags: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.action_id:
            raise ValueError("ActionContract.action_id is required")
        if self.estimated_cost < 0:
            raise ValueError("estimated_cost cannot be negative")
        if self.estimated_duration_seconds < 0:
            raise ValueError("estimated_duration_seconds cannot be negative")

    @property
    def has_postconditions(self) -> bool:
        return bool(self.postconditions)

    @property
    def is_reversible(self) -> bool:
        return self.compensation.strategy != COMPENSATION_NONE

    def to_dict(self) -> dict[str, Any]:
        return {
            "action_id": self.action_id,
            "estimated_cost": self.estimated_cost,
            "estimated_duration_seconds": self.estimated_duration_seconds,
            "postconditions": [
                {"kind": p.kind, "target_ref": p.target_ref,
                 "expected": dict(p.expected), "severity": p.severity}
                for p in self.postconditions
            ],
            "compensation": {
                "strategy": self.compensation.strategy,
                "undo_tool": self.compensation.undo_tool,
                "description": self.compensation.description,
            },
            "risk_flags": list(self.risk_flags),
            "is_reversible": self.is_reversible,
        }


# --------------------------------------------------------------------------- #
# Postcondition evaluation result
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class PostconditionFinding:
    kind: str
    target_ref: str
    passed: bool
    severity: str
    message: str
    observed: Any = None


@dataclass(frozen=True)
class PostconditionResult:
    """Result of evaluating all postconditions for one action."""

    action_id: str
    findings: tuple[PostconditionFinding, ...] = ()
    triggered_compensation: bool = False
    compensation_result: str = ""        # succeeded / failed / skipped

    @property
    def errors(self) -> tuple[PostconditionFinding, ...]:
        return tuple(f for f in self.findings if not f.passed and f.severity == "error")

    @property
    def all_passed(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict[str, Any]:
        return {
            "action_id": self.action_id,
            "all_passed": self.all_passed,
            "triggered_compensation": self.triggered_compensation,
            "compensation_result": self.compensation_result,
            "findings": [
                {"kind": f.kind, "target_ref": f.target_ref, "passed": f.passed,
                 "severity": f.severity, "message": f.message}
                for f in self.findings
            ],
        }


def evaluate_postconditions(
    contract: ActionContract,
    *,
    check_fn: Callable[[PostconditionSpec], PostconditionFinding],
) -> PostconditionResult:
    """Evaluate every postcondition in a contract (OS-10 verify phase).

    ``check_fn`` is host-supplied — it inspects the real project state and
    returns a :class:`PostconditionFinding`.  On any error, the result flags
    ``triggered_compensation=True``.
    """

    findings = tuple(check_fn(spec) for spec in contract.postconditions)
    has_errors = any(not f.passed and f.severity == "error" for f in findings)
    return PostconditionResult(
        action_id=contract.action_id,
        findings=findings,
        triggered_compensation=has_errors and contract.is_reversible,
    )


__all__ = [
    "ActionContract",
    "CompensationSpec",
    "PostconditionSpec",
    "PostconditionFinding",
    "PostconditionResult",
    "SUPPORTED_POSTCONDITIONS",
    "ALL_COMPENSATION_STRATEGIES",
    "POST_CLIP_EXISTS",
    "POST_CLIP_DURATION",
    "POST_ASSET_RESOLVED",
    "POST_REVISION_ADVANCED",
    "POST_SCENE_HASH",
    "POST_NODE_CONNECTED",
    "POST_GRADE_NODE_EXISTS",
    "POST_FILE_EXISTS",
    "POST_FILE_NON_EMPTY",
    "POST_QUALITY_GATE_PASSED",
    "COMPENSATION_UNDO",
    "COMPENSATION_ROLLBACK_GROUP",
    "COMPENSATION_MANUAL",
    "COMPENSATION_NONE",
    "evaluate_postconditions",
]
