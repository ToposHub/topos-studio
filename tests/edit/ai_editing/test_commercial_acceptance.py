"""Commercial panel-only acceptance evidence tests."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.edit_core

from src.features.edit.ai_editing.commercial_acceptance import (
    PANEL_WORKFLOW_STAGES,
    CommercialAcceptanceObservation,
    CommercialAcceptanceReport,
    CommercialAcceptanceSpec,
    run_commercial_acceptance,
)
from src.features.edit.ai_editing.eval_runner import (
    EvalSuiteRunner,
    ReleaseGatePolicy,
    evaluate_release_gate,
)
from src.features.edit.ai_editing.eval_trace import EvalCase
from src.features.edit.ai_editing.quality_gate import QualityGateInput


def _spec(case_id: str, task: str, platform: str, codec: str) -> CommercialAcceptanceSpec:
    return CommercialAcceptanceSpec(case_id, task, platform, codec)


def _real_observation(spec: CommercialAcceptanceSpec) -> CommercialAcceptanceObservation:
    return CommercialAcceptanceObservation(
        spec=spec,
        execution_mode="real",
        provider_id="provider-live",
        credential_verified=True,
        credential_evidence_ref="credential-check:sha256:abc123",
        entrypoint="workflow_panel",
        completed_stages=PANEL_WORKFLOW_STAGES,
        stage_evidence_refs=tuple((stage, f"stage:{stage}:sha256:abc") for stage in PANEL_WORKFLOW_STAGES),
        safety_verified=True,
        license_verified=True,
        region_verified=True,
        quality_gate_passed=True,
        restart_recovery_passed=True,
        cancel_latency_ms=120.0,
        long_task_duration_ms=12000.0,
        pressure_samples=3,
        concurrency_peak=2,
        long_task_passed=True,
    )


def _commercial_specs() -> tuple[CommercialAcceptanceSpec, ...]:
    return (
        _spec("ad-web-h264", "product_ad", "web", "h264"),
        _spec("interview-mobile-h265", "interview", "mobile", "h265"),
        _spec("subtitle-web-h264", "subtitle_multilingual", "web", "h264"),
        _spec("ad-mobile-h265", "product_ad", "mobile", "h265"),
    )


def test_real_panel_report_requires_every_stage_and_commercial_check() -> None:
    report = CommercialAcceptanceReport(tuple(
        _real_observation(spec) for spec in _commercial_specs()
    ))

    assert report.passed
    assert report.real_evidence_complete
    assert report.panel_only_complete
    assert report.missing_dimensions == ()
    assert report.report_id.startswith("commercial-acceptance-")


def test_synthetic_or_non_panel_observation_cannot_pass() -> None:
    spec = _commercial_specs()[0]
    observation = _real_observation(spec)
    blocked = CommercialAcceptanceObservation(
        spec=spec,
        execution_mode="synthetic",
        provider_id=observation.provider_id,
        credential_verified=True,
        credential_evidence_ref=observation.credential_evidence_ref,
        entrypoint="developer_api",
        completed_stages=PANEL_WORKFLOW_STAGES,
        stage_evidence_refs=observation.stage_evidence_refs,
        safety_verified=True,
        license_verified=True,
        region_verified=True,
        quality_gate_passed=True,
        restart_recovery_passed=True,
        cancel_latency_ms=120.0,
        long_task_passed=True,
    )
    report = CommercialAcceptanceReport((blocked,))

    assert not report.passed
    assert "task_type=interview" in report.missing_dimensions
    assert any(
        "panel_only_workflow_evidence_missing" in reason
        for reason in report.failure_reasons()
    )


def test_missing_pressure_or_duplicate_write_evidence_blocks_report() -> None:
    spec = _commercial_specs()[0]
    observation = _real_observation(spec)
    blocked = CommercialAcceptanceObservation(
        **{
            **observation.__dict__,
            "pressure_samples": 0,
            "duplicate_write_count": 1,
        }
    )

    assert not blocked.passed
    assert not blocked.rollout_safe
    assert "duplicate_write_charge_crash_or_copyright_incident" in blocked.failure_reasons()


def test_report_exports_rollout_metrics_without_raw_content() -> None:
    observations = tuple(
        CommercialAcceptanceObservation(
            **{
                **_real_observation(spec).__dict__,
                "user_edits": 1,
                "satisfied_users": 1,
                "total_cost_units": 2.5,
            }
        )
        for spec in _commercial_specs()
    )
    report = CommercialAcceptanceReport(observations)

    metrics = report.to_rollout_metrics()
    assert metrics.tasks == 4
    assert metrics.successful_tasks == 4
    assert metrics.total_cost_units == 10.0
    assert report.to_dict()["rollout_metrics"]["satisfaction_rate"] == 1.0


def test_runner_captures_probe_exception_as_failed_evidence() -> None:
    specs = _commercial_specs()[:1]
    report = run_commercial_acceptance(
        specs,
        lambda _spec: (_ for _ in ()).throw(RuntimeError("Bearer abcdefghijklmnop")),
        clock=lambda: 123.0,
    )

    assert not report.passed
    assert report.created_at == 123.0
    data = report.to_dict()
    assert "secret" not in str(data).lower()
    assert "ACCEPTANCE_PROBE_EXCEPTION" in str(data)


def test_release_gate_can_require_panel_acceptance_report() -> None:
    report = CommercialAcceptanceReport(tuple(
        _real_observation(spec) for spec in _commercial_specs()
    ))
    trace = EvalSuiteRunner().run(
        "commercial-panel-e2e",
        (("ok", "workflow", lambda: EvalCase("ok", True)),),
        quality_input=QualityGateInput(),
        commercial_acceptance=report,
    )
    blocked = evaluate_release_gate(
        trace,
        policy=ReleaseGatePolicy(require_commercial_acceptance=True),
    )
    assert blocked.allowed is False  # matrix and audit are still independently required
    assert blocked.commercial_acceptance_passed
    assert blocked.commercial_acceptance_report_id == report.report_id

    missing = evaluate_release_gate(
        trace,
        policy=ReleaseGatePolicy(require_commercial_acceptance=True, require_matrix=False,
                                 require_commercial_audit=False),
    )
    assert missing.allowed
