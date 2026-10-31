"""AgentMission lifecycle with parent-child run hierarchy (OS-01).

OS-01 asks for a long-lived mission object that carries the goal text, success
criteria, allowed scope, budget, deadline, parent/child run links, latest
evidence, user decisions and termination reason — so that "make a 15-second
ad" can survive across multiple chats, restarts and sub-tasks.

The existing ``autonomous_mission.AutonomousMission`` provides a 4-stage
ledger; ``goal_supervisor.GoalSpec`` provides the scope identity.  This module
fills the gaps the audit flagged:

* ``objective_text`` + ``success_criteria`` (not just an opaque ref);
* absolute ``deadline`` timestamp (not just relative wall time);
* ``parent_run_id`` / ``child_run_ids`` hierarchy;
* structured ``decision_log`` (not just a truncated reason string).

The module is designed to *compose* with the existing types rather than
replace them: a :class:`Mission` can wrap a ``GoalSpec`` for scope identity
and an ``AutonomousMission`` for stage tracking.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
import hashlib
import json
import threading
import time
import uuid
from typing import Any, Iterable, Mapping


# --------------------------------------------------------------------------- #
# Mission status
# --------------------------------------------------------------------------- #

MISSION_ACTIVE = "active"
MISSION_PAUSED = "paused"                 # user paused; no new actions
MISSION_WAITING_USER = "waiting_user"     # needs a user decision
MISSION_SUCCEEDED = "succeeded"
MISSION_FAILED = "failed"
MISSION_CANCELLED = "cancelled"
MISSION_SUPERSEDED = "superseded"         # replaced by a newer mission

ACTIVE_MISSION_STATUSES: frozenset[str] = frozenset({MISSION_ACTIVE, MISSION_PAUSED, MISSION_WAITING_USER})
TERMINAL_MISSION_STATUSES: frozenset[str] = frozenset({
    MISSION_SUCCEEDED, MISSION_FAILED, MISSION_CANCELLED, MISSION_SUPERSEDED,
})


# --------------------------------------------------------------------------- #
# Decision log entry
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class MissionDecision:
    """One structured user / system decision in the mission log."""

    decision_id: str
    timestamp: float
    kind: str                     # user_accept / user_reject / user_override / system_replan / system_rollback / clarify
    reason: str = ""
    artifact_ref: str | None = None
    actor: str = "user"           # user / system / host

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision_id": self.decision_id,
            "timestamp": self.timestamp,
            "kind": self.kind,
            "reason": self.reason,
            "artifact_ref": self.artifact_ref,
            "actor": self.actor,
        }


# --------------------------------------------------------------------------- #
# Success criteria
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class SuccessCriterion:
    """One measurable success condition for the mission.

    ``check`` is an opaque string the host interprets (e.g.
    ``"delivery_quality_gate_passed"``); ``target`` is the expected value.
    """

    check: str
    target: str = "true"
    label: str = ""
    met: bool = False
    evidence_ref: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "check": self.check,
            "target": self.target,
            "label": self.label,
            "met": self.met,
            "evidence_ref": self.evidence_ref,
        }


# --------------------------------------------------------------------------- #
# Mission
# --------------------------------------------------------------------------- #


@dataclass
class Mission:
    """Long-lived, hierarchical agent mission (OS-01).

    The mission is mutable (status / decisions / criteria evolve), so it uses
    a regular dataclass + a lock for thread safety via :class:`MissionStore`.
    """

    mission_id: str
    project_id: str
    objective_text: str
    success_criteria: tuple[SuccessCriterion, ...] = ()
    allowed_scope: Mapping[str, Any] = field(default_factory=dict)
    budget_units: float | None = None
    deadline: float | None = None
    parent_run_id: str | None = None
    child_run_ids: tuple[str, ...] = ()
    status: str = MISSION_ACTIVE
    decisions: tuple[MissionDecision, ...] = ()
    latest_evidence_fingerprint: str | None = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    termination_reason: str = ""

    def __post_init__(self) -> None:
        if not self.mission_id:
            raise ValueError("Mission.mission_id is required")
        if not self.project_id:
            raise ValueError("Mission.project_id is required")
        if not self.objective_text:
            raise ValueError("Mission.objective_text is required")
        if self.status not in ACTIVE_MISSION_STATUSES | TERMINAL_MISSION_STATUSES:
            raise ValueError(f"Unknown mission status: {self.status!r}")

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_MISSION_STATUSES

    @property
    def is_active(self) -> bool:
        return self.status in ACTIVE_MISSION_STATUSES

    @property
    def success_criteria_met(self) -> bool:
        return bool(self.success_criteria) and all(c.met for c in self.success_criteria)

    def scope_hash(self) -> str:
        payload = {
            "project_id": self.project_id,
            "objective_text": self.objective_text,
            "allowed_scope": dict(self.allowed_scope),
            "budget_units": self.budget_units,
            "deadline": self.deadline,
        }
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def describe_progress(self) -> str:
        """Human-readable one-liner: '已完成什么、下一步是什么、为何等待用户'."""
        total = len(self.success_criteria)
        met = sum(1 for c in self.success_criteria if c.met)
        parts = [f"目标：{self.objective_text[:60]}"]
        if total:
            parts.append(f"成功标准 {met}/{total} 已满足")
        if self.status == MISSION_WAITING_USER:
            parts.append("正在等待用户决策")
        elif self.status == MISSION_PAUSED:
            parts.append("已暂停")
        if self.termination_reason:
            parts.append(f"结束原因：{self.termination_reason}")
        return "；".join(parts)

    def to_dict(self) -> dict[str, Any]:
        return {
            "mission_id": self.mission_id,
            "project_id": self.project_id,
            "objective_text": self.objective_text,
            "success_criteria": [c.to_dict() for c in self.success_criteria],
            "allowed_scope": dict(self.allowed_scope),
            "budget_units": self.budget_units,
            "deadline": self.deadline,
            "parent_run_id": self.parent_run_id,
            "child_run_ids": list(self.child_run_ids),
            "status": self.status,
            "decisions": [d.to_dict() for d in self.decisions],
            "latest_evidence_fingerprint": self.latest_evidence_fingerprint,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "termination_reason": self.termination_reason,
        }


# --------------------------------------------------------------------------- #
# Store
# --------------------------------------------------------------------------- #


class MissionStore:
    """Thread-safe store of missions with parent-child hierarchy support.

    Thread-safety contract (AUD-13920): 查询方法（``get``/``require``/
    ``active_missions``/``children_of``/``all_missions``/``create``）返回的是
    **快照副本**（``dataclasses.replace`` 浅拷贝），调用方在锁外持有/修改快照
    不会与锁内的原子替换竞争；所有对店内 Mission 的变更必须走本类的变更方法
    （``add_decision`` / ``update_criterion`` / ``set_status`` /
    ``set_evidence_fingerprint``）。
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._missions: dict[str, Mission] = {}

    @staticmethod
    def _snapshot(mission: Mission) -> Mission:
        """Return a copy the caller may hold outside the store lock.

        元组字段（decisions/success_criteria/child_run_ids）本就不可变；
        ``allowed_scope`` 是 Mapping，拷一层 dict 防止快照持有者改写店内共享
        的 scope 内容。
        """
        return replace(mission, allowed_scope=dict(mission.allowed_scope))

    def create(
        self,
        *,
        project_id: str,
        objective_text: str,
        success_criteria: Iterable[SuccessCriterion] = (),
        allowed_scope: Mapping[str, Any] | None = None,
        budget_units: float | None = None,
        deadline: float | None = None,
        parent_run_id: str | None = None,
    ) -> Mission:
        mission = Mission(
            mission_id=f"mission-{uuid.uuid4().hex}",
            project_id=project_id,
            objective_text=objective_text,
            success_criteria=tuple(success_criteria),
            allowed_scope=dict(allowed_scope or {}),
            budget_units=budget_units,
            deadline=deadline,
            parent_run_id=parent_run_id,
        )
        with self._lock:
            self._missions[mission.mission_id] = mission
            if parent_run_id:
                parent = self._missions.get(parent_run_id)
                if parent and mission.mission_id not in parent.child_run_ids:
                    parent.child_run_ids = parent.child_run_ids + (mission.mission_id,)
        return self._snapshot(mission)

    def get(self, mission_id: str) -> Mission | None:
        with self._lock:
            mission = self._missions.get(mission_id)
            return self._snapshot(mission) if mission is not None else None

    def require(self, mission_id: str) -> Mission:
        mission = self.get(mission_id)
        if mission is None:
            raise KeyError(f"Unknown mission: {mission_id!r}")
        return mission

    def _require_live(self, mission_id: str) -> Mission:
        """Return the stored (mutable) mission for in-store mutations only.

        AUD-13920：对外 ``require`` 返回快照后，store 内部的变更方法必须
        改用本方法拿到店内活对象，否则会改到快照副本上静默丢失。
        """
        mission = self._missions.get(mission_id)
        if mission is None:
            raise KeyError(f"Unknown mission: {mission_id!r}")
        return mission

    def active_missions(self, project_id: str | None = None) -> tuple[Mission, ...]:
        with self._lock:
            return tuple(
                self._snapshot(m) for m in self._missions.values()
                if m.is_active and (project_id is None or m.project_id == project_id)
            )

    def children_of(self, mission_id: str) -> tuple[Mission, ...]:
        with self._lock:
            parent = self._missions.get(mission_id)
            if parent is None:
                return ()
            return tuple(
                self._snapshot(self._missions[c])
                for c in parent.child_run_ids if c in self._missions
            )

    # ---------------- mutations ---------------- #

    def add_decision(
        self,
        mission_id: str,
        *,
        kind: str,
        reason: str = "",
        artifact_ref: str | None = None,
        actor: str = "user",
    ) -> MissionDecision:
        with self._lock:
            mission = self._require_live(mission_id)
            decision = MissionDecision(
                decision_id=f"decision-{uuid.uuid4().hex}",
                timestamp=time.time(),
                kind=kind,
                reason=reason,
                artifact_ref=artifact_ref,
                actor=actor,
            )
            mission.decisions = mission.decisions + (decision,)
            mission.updated_at = time.time()
            return decision

    def update_criterion(
        self,
        mission_id: str,
        check: str,
        *,
        met: bool,
        evidence_ref: str | None = None,
    ) -> None:
        with self._lock:
            mission = self._require_live(mission_id)
            updated = tuple(
                SuccessCriterion(
                    check=c.check, target=c.target, label=c.label,
                    met=met if c.check == check else c.met,
                    evidence_ref=evidence_ref if c.check == check else c.evidence_ref,
                )
                for c in mission.success_criteria
            )
            mission.success_criteria = updated
            mission.updated_at = time.time()
            # Auto-succeed if all criteria are met.
            if mission.success_criteria_met and mission.is_active:
                mission.status = MISSION_SUCCEEDED
                mission.termination_reason = "所有成功条件已满足"

    def set_status(
        self,
        mission_id: str,
        status: str,
        *,
        reason: str = "",
    ) -> None:
        """Transition a mission's status with the same rules as construction.

        AUD-13919：``Mission.__post_init__`` 在构造期用白名单校验 status，
        但旧 ``set_status`` 直接赋值——拼错的状态（如 ``"actve"``）既不在
        ACTIVE 也不在 TERMINAL 集合，mission 变成 ``is_active``/``is_terminal``
        双 False 的僵尸；且终态可被随意改回 active，破坏 OS-01 单向状态机。
        现在与构造期同样校验（非法 → ValueError），并拒绝任何终态迁移
        （复活需显式 reopen / 新建 mission）。
        """
        with self._lock:
            mission = self._require_live(mission_id)
            if status not in ACTIVE_MISSION_STATUSES | TERMINAL_MISSION_STATUSES:
                raise ValueError(f"Unknown mission status: {status!r}")
            if mission.is_terminal:
                raise ValueError(
                    f"mission {mission_id!r} already terminal "
                    f"({mission.status!r}); reopening requires a new mission"
                )
            mission.status = status
            mission.termination_reason = reason
            mission.updated_at = time.time()

    def set_evidence_fingerprint(self, mission_id: str, fingerprint: str) -> None:
        with self._lock:
            mission = self._require_live(mission_id)
            mission.latest_evidence_fingerprint = fingerprint
            mission.updated_at = time.time()

    def all_missions(self) -> tuple[Mission, ...]:
        with self._lock:
            return tuple(self._snapshot(m) for m in self._missions.values())


__all__ = [
    "Mission",
    "MissionStore",
    "MissionDecision",
    "SuccessCriterion",
    "ACTIVE_MISSION_STATUSES",
    "TERMINAL_MISSION_STATUSES",
    "MISSION_ACTIVE",
    "MISSION_PAUSED",
    "MISSION_WAITING_USER",
    "MISSION_SUCCEEDED",
    "MISSION_FAILED",
    "MISSION_CANCELLED",
    "MISSION_SUPERSEDED",
]
