"""Rollout, feature flags, kill switch and feedback for MG autonomy (MG-27).

MG-27 makes the autonomy *operable*: feature flags gate who gets the
autonomous path, a kill switch turns it off instantly, canary cohorts phase
the rollout, feedback + cost + copyright-incident metrics prove it is safe,
and a written rollback runbook means on-call knows what to do.

This module defines the flag/decision data model + a small
:class:`RolloutController`.  The actual flag store (feature flags service /
prefs) plugs in via a duck-typed :class:`FlagSource`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import threading
import time
from typing import Any, Callable, Mapping


# --------------------------------------------------------------------------- #
# Flag identifiers
# --------------------------------------------------------------------------- #


FLAG_MOTION_GRAPHIC_AUTO = "mg_autonomous_mode"
FLAG_MOTION_GRAPHIC_CANARY = "mg_canary"
FLAG_MOTION_GRAPHIC_PAID_GENERATION = "mg_paid_generation"
FLAG_MOTION_GRAPHIC_CLOUD_UPLOAD = "mg_cloud_upload"
FLAG_MOTION_GRAPHIC_AUTO_EXPORT = "mg_auto_export"

ALL_ROLLOUT_FLAGS: tuple[str, ...] = (
    FLAG_MOTION_GRAPHIC_AUTO,
    FLAG_MOTION_GRAPHIC_CANARY,
    FLAG_MOTION_GRAPHIC_PAID_GENERATION,
    FLAG_MOTION_GRAPHIC_CLOUD_UPLOAD,
    FLAG_MOTION_GRAPHIC_AUTO_EXPORT,
)


# --------------------------------------------------------------------------- #
# Cohorts + incidents
# --------------------------------------------------------------------------- #


COHORT_INTERNAL = "internal"
COHORT_ALPHA = "alpha"
COHORT_BETA = "beta"
COHORT_GA = "ga"

COHORT_ORDER: tuple[str, ...] = (COHORT_INTERNAL, COHORT_ALPHA, COHORT_BETA, COHORT_GA)


@dataclass(frozen=True)
class IncidentReport:
    """One recorded copyright / cost / safety incident."""

    incident_id: str
    kind: str                       # copyright / cost_overrun / unsafe_write / data_leak
    severity: str                   # info / warning / error
    timestamp: float
    details: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "incident_id": self.incident_id,
            "kind": self.kind,
            "severity": self.severity,
            "timestamp": self.timestamp,
            "details": dict(self.details),
        }


@dataclass(frozen=True)
class UserFeedback:
    """One piece of user feedback tied to a run / proposal."""

    feedback_id: str
    run_id: str | None
    outcome: str                    # accepted / rejected / modified / cancelled
    satisfaction: float             # 0..1
    reason_category: str = ""
    note: str = ""
    timestamp: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "feedback_id": self.feedback_id,
            "run_id": self.run_id,
            "outcome": self.outcome,
            "satisfaction": self.satisfaction,
            "reason_category": self.reason_category,
            "note": self.note,
            "timestamp": self.timestamp,
        }


# --------------------------------------------------------------------------- #
# Flag source protocol
# --------------------------------------------------------------------------- #


class FlagSource:
    """Minimal duck-typed protocol for a flag source.

    Concrete sources (feature flag service, Preferences, test stub) implement
    ``is_enabled(flag_id, *, user_id) -> bool`` and ``cohort_for(user_id) -> str``.
    """

    def is_enabled(self, flag_id: str, *, user_id: str | None = None) -> bool:
        raise NotImplementedError

    def cohort_for(self, user_id: str | None) -> str:
        raise NotImplementedError


class DictFlagSource(FlagSource):
    """In-memory flag source for tests and local dev."""

    def __init__(
        self,
        flags: Mapping[str, bool] | None = None,
        *,
        cohorts: Mapping[str, str] | None = None,
        default_cohort: str = COHORT_INTERNAL,
    ) -> None:
        self._flags = dict(flags or {})
        self._cohorts = dict(cohorts or {})
        self._default_cohort = default_cohort

    def set(self, flag_id: str, enabled: bool) -> None:
        self._flags[flag_id] = enabled

    def is_enabled(self, flag_id: str, *, user_id: str | None = None) -> bool:
        return bool(self._flags.get(flag_id, False))

    def cohort_for(self, user_id: str | None) -> str:
        if user_id and user_id in self._cohorts:
            return self._cohorts[user_id]
        return self._default_cohort


# --------------------------------------------------------------------------- #
# Rollout policy
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class RolloutPolicy:
    """Which cohorts may use autonomous mode at the current phase."""

    min_cohort_for_auto: str = COHORT_INTERNAL
    kill_switch_active: bool = False
    paid_generation_min_cohort: str = COHORT_BETA
    cloud_upload_min_cohort: str = COHORT_GA
    auto_export_min_cohort: str = COHORT_BETA
    data_retention_days: int = 90
    notes: str = ""

    def cohort_allows(self, capability: str, cohort: str) -> bool:
        min_cohort = {
            FLAG_MOTION_GRAPHIC_AUTO: self.min_cohort_for_auto,
            FLAG_MOTION_GRAPHIC_PAID_GENERATION: self.paid_generation_min_cohort,
            FLAG_MOTION_GRAPHIC_CLOUD_UPLOAD: self.cloud_upload_min_cohort,
            FLAG_MOTION_GRAPHIC_AUTO_EXPORT: self.auto_export_min_cohort,
        }.get(capability, COHORT_GA)
        if self.kill_switch_active:
            return False
        return COHORT_ORDER.index(cohort) >= COHORT_ORDER.index(min_cohort)


# --------------------------------------------------------------------------- #
# Controller
# --------------------------------------------------------------------------- #


class RolloutController:
    """Evaluate the rollout policy for a given user + collect telemetry.

    The controller is the single place the Assistant asks "may this user use
    autonomous Motion Graphic?".  It combines the flag source, the policy and
    the kill switch; it also records incidents + feedback so the release
    runbook has data to act on.
    """

    def __init__(
        self,
        flag_source: FlagSource,
        *,
        policy: RolloutPolicy | None = None,
        time_fn: Callable[[], float] | None = None,
    ) -> None:
        self._flags = flag_source
        self._policy = policy or RolloutPolicy()
        self._time_fn = time_fn or time.time
        self._lock = threading.RLock()
        self._incidents: list[IncidentReport] = []
        self._feedback: list[UserFeedback] = []
        # AUD-27014: the kill switch used to flip the frozen RolloutPolicy in
        # place via ``object.__setattr__``, which (a) broke the value object's
        # immutability contract and (b) leaked the switch to every controller
        # sharing the same policy instance.  The switch is now controller-local
        # mutable state; ``policy`` stays frozen and read-only.
        self._kill_switch_active: bool = False

    # ---------------- policy evaluation ---------------- #

    @property
    def kill_switch_active(self) -> bool:
        with self._lock:
            return self._kill_switch_active

    def _blocked_by_kill_switch(self) -> bool:
        return self._policy.kill_switch_active or self.kill_switch_active

    def may_use_autonomous_mode(self, *, user_id: str | None) -> bool:
        if self._blocked_by_kill_switch():
            return False
        if not self._flags.is_enabled(FLAG_MOTION_GRAPHIC_AUTO, user_id=user_id):
            return False
        return self._policy.cohort_allows(FLAG_MOTION_GRAPHIC_AUTO, self._flags.cohort_for(user_id))

    def may_use_capability(self, capability: str, *, user_id: str | None) -> bool:
        if self._blocked_by_kill_switch():
            return False
        if not self._flags.is_enabled(capability, user_id=user_id):
            return False
        return self._policy.cohort_allows(capability, self._flags.cohort_for(user_id))

    def activate_kill_switch(self, *, reason: str = "") -> None:
        with self._lock:
            self._kill_switch_active = True
        # Record as an incident so the runbook has a trail.
        self.record_incident("kill_switch", "error", details={"reason": reason})

    def deactivate_kill_switch(self, *, reason: str = "") -> None:
        with self._lock:
            self._kill_switch_active = False
        # AUD-27014: deactivation also leaves an incident trail — the runbook
        # requires the whole kill-switch window to be auditable.
        self.record_incident("kill_switch", "info", details={"reason": reason, "deactivated": True})

    @property
    def policy(self) -> RolloutPolicy:
        return self._policy

    # ---------------- telemetry ---------------- #

    def record_incident(
        self,
        kind: str,
        severity: str,
        *,
        details: Mapping[str, Any] | None = None,
    ) -> IncidentReport:
        import uuid
        incident = IncidentReport(
            incident_id=f"mg-inc-{uuid.uuid4().hex}",
            kind=kind,
            severity=severity,
            timestamp=self._time_fn(),
            details=dict(details or {}),
        )
        with self._lock:
            self._incidents.append(incident)
        return incident

    def record_feedback(
        self,
        *,
        run_id: str | None,
        outcome: str,
        satisfaction: float,
        reason_category: str = "",
        note: str = "",
    ) -> UserFeedback:
        import uuid
        feedback = UserFeedback(
            feedback_id=f"mg-fb-{uuid.uuid4().hex}",
            run_id=run_id,
            outcome=outcome,
            satisfaction=max(0.0, min(1.0, float(satisfaction))),
            reason_category=reason_category,
            note=note,
            timestamp=self._time_fn(),
        )
        with self._lock:
            self._feedback.append(feedback)
        return feedback

    def incidents(self) -> tuple[IncidentReport, ...]:
        with self._lock:
            return tuple(self._incidents)

    def feedback(self) -> tuple[UserFeedback, ...]:
        with self._lock:
            return tuple(self._feedback)

    def acceptance_rate(self) -> float:
        with self._lock:
            if not self._feedback:
                return 0.0
            accepted = sum(1 for item in self._feedback if item.outcome == "accepted")
            return accepted / len(self._feedback)

    def satisfaction_average(self) -> float:
        with self._lock:
            if not self._feedback:
                return 0.0
            return sum(item.satisfaction for item in self._feedback) / len(self._feedback)


# --------------------------------------------------------------------------- #
# Rollback runbook (text-only reference; MG-27 asks for a written runbook)
# --------------------------------------------------------------------------- #


ROLLBACK_RUNBOOK: str = """\
Motion Graphic autonomy rollback runbook
========================================

Trigger any of:
- copyright / license incident recorded via RolloutController.record_incident
- cost overrun beyond budget_units on a single run
- unsafe timeline write detected by the quality gate
- satisfaction_average() < 0.6 over the last 20 runs

Steps:
1. Activate the kill switch:
   controller.activate_kill_switch(reason="…")
   This immediately blocks autonomous mode for ALL cohorts.
2. Confirm the flag service reflects the kill switch for live users.
3. File an incident with the run_id, brief_hash and plan_hash for traceability.
4. Disable the specific capability flag (paid_generation / cloud_upload /
   auto_export) rather than the whole autonomy flag when the incident is
   scoped to one capability.
5. Collect user feedback recorded during the incident window; keep for the
   data_retention_days configured on the policy.
6. Roll back the code or model version that caused the incident.
7. Re-enable per cohort (internal → alpha → beta → ga), watching
   acceptance_rate and satisfaction_average at each step.
"""


__all__ = [
    "RolloutController",
    "RolloutPolicy",
    "FlagSource",
    "DictFlagSource",
    "IncidentReport",
    "UserFeedback",
    "ALL_ROLLOUT_FLAGS",
    "FLAG_MOTION_GRAPHIC_AUTO",
    "FLAG_MOTION_GRAPHIC_CANARY",
    "FLAG_MOTION_GRAPHIC_PAID_GENERATION",
    "FLAG_MOTION_GRAPHIC_CLOUD_UPLOAD",
    "FLAG_MOTION_GRAPHIC_AUTO_EXPORT",
    "COHORT_INTERNAL",
    "COHORT_ALPHA",
    "COHORT_BETA",
    "COHORT_GA",
    "COHORT_ORDER",
    "ROLLBACK_RUNBOOK",
]
