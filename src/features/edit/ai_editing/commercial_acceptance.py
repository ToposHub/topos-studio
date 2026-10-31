"""Commercial, panel-only acceptance evidence for the AI editing agent.

This module deliberately does not manufacture provider results.  A real panel
adapter supplies observations after it has executed the workflow with the
configured credential.  The report then turns those observations into a
privacy-safe, reproducible release artifact and fails closed when any part of
the evidence is missing.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Callable, Iterable

from src.features.edit.ai_editing.privacy_guard import redact_text
from src.features.edit.ai_editing.release_test_matrix import (
    DEFAULT_COMMERCIAL_MATRIX_DIMENSIONS,
    find_missing_dimensions,
)
from src.features.edit.ai_editing.rollout_metrics import RolloutMetrics


PANEL_WORKFLOW_STAGES: tuple[str, ...] = (
    "analyze",
    "plan",
    "timeline_write",
    "export",
    "recovery",
)


@dataclass(frozen=True)
class CommercialAcceptanceSpec:
    """One real commercial scenario to execute through the user panel."""

    case_id: str
    task_type: str
    platform: str
    codec: str
    required_stages: tuple[str, ...] = PANEL_WORKFLOW_STAGES

    def __post_init__(self) -> None:
        if not self.case_id.strip():
            raise ValueError("case_id is required")
        if not self.task_type.strip() or not self.platform.strip() or not self.codec.strip():
            raise ValueError("task_type, platform and codec are required")
        if not self.required_stages:
            raise ValueError("required_stages must not be empty")


@dataclass(frozen=True)
class CommercialAcceptanceObservation:
    """Sanitized observation returned by a real or fixture panel adapter."""

    spec: CommercialAcceptanceSpec
    execution_mode: str = "synthetic"  # synthetic / real
    provider_id: str = ""
    credential_verified: bool = False
    credential_evidence_ref: str = ""
    entrypoint: str = "unknown"  # workflow_panel is the only accepted path
    completed_stages: tuple[str, ...] = ()
    stage_evidence_refs: tuple[tuple[str, str], ...] = ()
    safety_verified: bool = False
    license_verified: bool = False
    region_verified: bool = False
    quality_gate_passed: bool = False
    restart_recovery_passed: bool = False
    cancel_latency_ms: float | None = None
    cancel_latency_budget_ms: float = 5000.0
    long_task_passed: bool = False
    long_task_duration_ms: float | None = None
    pressure_samples: int = 0
    concurrency_peak: int = 0
    duplicate_write_count: int = 0
    repeated_charge_count: int = 0
    provider_failure_count: int = 0
    crash_count: int = 0
    copyright_incidents: int = 0
    user_edits: int = 0
    satisfied_users: int = 0
    total_cost_units: float = 0.0
    non_panel_action_count: int = 0
    error_code: str = ""
    error_message: str = ""

    @property
    def case_id(self) -> str:
        return self.spec.case_id

    @property
    def stage_evidence_complete(self) -> bool:
        refs = {stage: ref.strip() for stage, ref in self.stage_evidence_refs}
        return all(
            stage in refs and bool(refs[stage])
            for stage in self.spec.required_stages
        )

    @property
    def has_real_evidence(self) -> bool:
        """Require proof of a credential-backed provider run, not a boolean flag."""
        return (
            self.execution_mode == "real"
            and bool(self.provider_id.strip())
            and self.credential_verified
            and bool(self.credential_evidence_ref.strip())
            and self.stage_evidence_complete
        )

    @property
    def panel_only(self) -> bool:
        return (
            self.entrypoint == "workflow_panel"
            and self.non_panel_action_count == 0
            and all(stage in self.completed_stages for stage in self.spec.required_stages)
        )

    @property
    def operational_checks_passed(self) -> bool:
        return (
            self.restart_recovery_passed
            and self.long_task_passed
            and self.long_task_duration_ms is not None
            and self.long_task_duration_ms > 0.0
            and self.pressure_samples > 0
            and self.concurrency_peak > 0
            and self.cancel_latency_ms is not None
            and 0.0 <= self.cancel_latency_ms <= self.cancel_latency_budget_ms
        )

    @property
    def rollout_safe(self) -> bool:
        return not any((
            self.duplicate_write_count,
            self.repeated_charge_count,
            self.crash_count,
            self.copyright_incidents,
        ))

    @property
    def commercial_checks_passed(self) -> bool:
        return (
            self.safety_verified
            and self.license_verified
            and self.region_verified
            and self.quality_gate_passed
        )

    @property
    def passed(self) -> bool:
        return (
            self.has_real_evidence
            and self.panel_only
            and self.operational_checks_passed
            and self.commercial_checks_passed
            and self.rollout_safe
            and not self.error_code
        )

    def failure_reasons(self) -> tuple[str, ...]:
        reasons: list[str] = []
        if not self.has_real_evidence:
            reasons.append("real_provider_credential_evidence_missing")
        if not self.panel_only:
            reasons.append("panel_only_workflow_evidence_missing")
        if not self.commercial_checks_passed:
            reasons.append("safety_license_region_or_quality_evidence_missing")
        if not self.operational_checks_passed:
            reasons.append("restart_long_task_or_cancel_latency_evidence_missing")
        if not self.rollout_safe:
            reasons.append("duplicate_write_charge_crash_or_copyright_incident")
        if self.error_code:
            reasons.append(redact_text(self.error_code)[:100])
        return tuple(dict.fromkeys(reasons))

    def to_dict(self) -> dict[str, object]:
        return {
            "case_id": self.case_id,
            "task_type": self.spec.task_type,
            "platform": self.spec.platform,
            "codec": self.spec.codec,
            "execution_mode": self.execution_mode,
            "provider_id": redact_text(self.provider_id)[:100],
            "credential_verified": self.credential_verified,
            "credential_evidence_ref": redact_text(self.credential_evidence_ref)[:200],
            "entrypoint": self.entrypoint,
            "completed_stages": list(self.completed_stages),
            "stage_evidence_refs": {
                stage: redact_text(ref)[:200]
                for stage, ref in self.stage_evidence_refs
            },
            "safety_verified": self.safety_verified,
            "license_verified": self.license_verified,
            "region_verified": self.region_verified,
            "quality_gate_passed": self.quality_gate_passed,
            "restart_recovery_passed": self.restart_recovery_passed,
            "cancel_latency_ms": self.cancel_latency_ms,
            "cancel_latency_budget_ms": self.cancel_latency_budget_ms,
            "long_task_passed": self.long_task_passed,
            "long_task_duration_ms": self.long_task_duration_ms,
            "pressure_samples": self.pressure_samples,
            "concurrency_peak": self.concurrency_peak,
            "duplicate_write_count": self.duplicate_write_count,
            "repeated_charge_count": self.repeated_charge_count,
            "provider_failure_count": self.provider_failure_count,
            "crash_count": self.crash_count,
            "copyright_incidents": self.copyright_incidents,
            "user_edits": self.user_edits,
            "satisfied_users": self.satisfied_users,
            "total_cost_units": self.total_cost_units,
            "non_panel_action_count": self.non_panel_action_count,
            "error_code": redact_text(self.error_code)[:100],
            "error_message": redact_text(self.error_message)[:300],
            "passed": self.passed,
            "failure_reasons": list(self.failure_reasons()),
        }


@dataclass(frozen=True)
class CommercialAcceptanceReport:
    """Immutable, privacy-safe report suitable for a release artifact."""

    cases: tuple[CommercialAcceptanceObservation, ...] = ()
    created_at: float = field(default_factory=time.time)
    report_id: str = ""

    def __post_init__(self) -> None:
        if self.report_id:
            return
        payload = {
            "schema_version": 1,
            "cases": [case.to_dict() for case in self.cases],
        }
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()[:20]
        object.__setattr__(self, "report_id", f"commercial-acceptance-{digest}")

    @property
    def missing_dimensions(self) -> tuple[str, ...]:
        return find_missing_dimensions(
            (
                _dimension_case(case)
                for case in self.cases
            ),
            DEFAULT_COMMERCIAL_MATRIX_DIMENSIONS,
        )

    @property
    def real_evidence_complete(self) -> bool:
        return bool(self.cases) and all(case.has_real_evidence for case in self.cases)

    @property
    def panel_only_complete(self) -> bool:
        return bool(self.cases) and all(case.panel_only for case in self.cases)

    @property
    def passed(self) -> bool:
        return bool(self.cases) and not self.missing_dimensions and all(
            case.passed for case in self.cases
        )

    def to_rollout_metrics(self) -> RolloutMetrics:
        """Aggregate only privacy-safe operational counters for rollout gates."""
        return RolloutMetrics(
            tasks=len(self.cases),
            successful_tasks=sum(case.passed for case in self.cases),
            crashes=sum(case.crash_count for case in self.cases),
            provider_failures=sum(case.provider_failure_count for case in self.cases),
            repeated_charges=sum(case.repeated_charge_count for case in self.cases),
            copyright_incidents=sum(case.copyright_incidents for case in self.cases),
            user_edits=sum(case.user_edits for case in self.cases),
            satisfied_users=sum(case.satisfied_users for case in self.cases),
            total_cost_units=sum(case.total_cost_units for case in self.cases),
        )

    def failure_reasons(self) -> tuple[str, ...]:
        reasons: list[str] = []
        reasons.extend(f"missing_dimension:{item}" for item in self.missing_dimensions)
        reasons.extend(
            f"case:{case.case_id}:{reason}"
            for case in self.cases if not case.passed
            for reason in case.failure_reasons()
        )
        if not self.cases:
            reasons.append("no_acceptance_cases")
        return tuple(dict.fromkeys(reasons))

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "report_id": self.report_id,
            "created_at": self.created_at,
            "passed": self.passed,
            "real_evidence_complete": self.real_evidence_complete,
            "panel_only_complete": self.panel_only_complete,
            "rollout_metrics": self.to_rollout_metrics().to_dict(),
            "missing_dimensions": list(self.missing_dimensions),
            "failure_reasons": list(self.failure_reasons()),
            "cases": [case.to_dict() for case in self.cases],
        }


def _dimension_case(case: CommercialAcceptanceObservation) -> object:
    """Adapt an observation to the existing matrix dimension helper."""

    return SimpleNamespace(
        task_type=case.spec.task_type,
        platform=case.spec.platform,
        codec=case.spec.codec,
    )


AcceptanceProbe = Callable[[CommercialAcceptanceSpec], CommercialAcceptanceObservation]


def run_commercial_acceptance(
    specs: Iterable[CommercialAcceptanceSpec],
    probe: AcceptanceProbe,
    *,
    clock: Callable[[], float] | None = None,
) -> CommercialAcceptanceReport:
    """Run panel probes and collect failures as report data.

    ``probe`` is the integration boundary.  A real implementation must invoke
    the actual Workflow/Assistant panel and return evidence refs for every
    stage.  Exceptions are captured as a failed case; no exception is turned
    into a passing synthetic result.
    """
    now = clock or time.time
    observations: list[CommercialAcceptanceObservation] = []
    for spec in specs:
        try:
            observation = probe(spec)
            if not isinstance(observation, CommercialAcceptanceObservation):
                raise TypeError("acceptance probe must return CommercialAcceptanceObservation")
            if observation.spec != spec:
                raise ValueError("acceptance observation spec mismatch")
            observations.append(observation)
        except Exception as exc:  # noqa: BLE001 — failed acceptance is data
            observations.append(CommercialAcceptanceObservation(
                spec=spec,
                error_code="ACCEPTANCE_PROBE_EXCEPTION",
                error_message=f"{type(exc).__name__}: {exc}",
            ))
    return CommercialAcceptanceReport(tuple(observations), created_at=now())


__all__ = [
    "AcceptanceProbe",
    "CommercialAcceptanceObservation",
    "CommercialAcceptanceReport",
    "CommercialAcceptanceSpec",
    "PANEL_WORKFLOW_STAGES",
    "run_commercial_acceptance",
]
