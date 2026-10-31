"""P10 offline evaluation and release gate tests."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.edit_core

from src.features.edit.ai_editing.eval_runner import (
    EvalSuiteRunner,
    ReleaseGatePolicy,
    evaluate_release_gate,
)
from src.features.edit.ai_editing.eval_trace import EvalCase
from src.features.edit.ai_editing.quality_gate import QualityGateInput
from src.features.edit.ai_editing.release_test_matrix import MatrixCase


def test_eval_suite_runner_records_safe_cases_and_release_decision() -> None:
    trace = EvalSuiteRunner(clock=iter([1.0] * 10).__next__).run(
        "p10-fixture",
        (
            ("schema", "contract", lambda: EvalCase("ignored", True)),
            ("redline", "security", lambda: EvalCase("ignored", True, error_message="Bearer abcdefghijklmnop")),
        ),
        quality_input=QualityGateInput(),
        matrix_cases=tuple(MatrixCase(
            name=category,
            category=category,
            passed=True,
            execution_mode="real",
            evidence_ref=f"matrix:{category}",
            provider_id="provider-1",
            task_type=task_type,
            platform=platform,
            codec=codec,
        ) for category, task_type, platform, codec in (
            ("dataset", "product_ad", "web", "h264"),
            ("performance", "interview", "mobile", "h265"),
            ("fault", "subtitle_multilingual", "web", "h264"),
            ("security", "product_ad", "mobile", "h265"),
        )),
        run_id="eval-1",
        commercial_audit_passed=True,
        commercial_audit_report_id="audit-1",
    )
    assert trace.passed
    decision = evaluate_release_gate(trace)
    assert decision.allowed
    assert decision.case_pass_rate == 1.0
    assert "abcdefghijklmnop" not in trace.cases[1].error_message


def test_release_gate_blocks_failed_cases_or_hard_gates() -> None:
    trace = EvalSuiteRunner().run(
        "p10-failing",
        (("broken", "integration", lambda: EvalCase("broken", False, error_code="MOCK_FAILED")),),
        quality_input=QualityGateInput(secret_leaks=1),
        run_id="eval-2",
    )
    decision = evaluate_release_gate(trace, policy=ReleaseGatePolicy(min_case_pass_rate=1.0))
    assert not decision.allowed
    assert decision.case_pass_rate == 0.0
    assert decision.quality_gate_passed is False
    assert decision.reasons


def test_release_gate_can_require_full_risk_matrix() -> None:
    cases = tuple(MatrixCase(
        name=category,
        category=category,
        passed=True,
        execution_mode="real",
        evidence_ref=f"matrix:{category}",
        provider_id="provider-1",
        task_type=task_type,
        platform=platform,
        codec=codec,
    ) for category, task_type, platform, codec in (
        ("dataset", "product_ad", "web", "h264"),
        ("performance", "interview", "mobile", "h265"),
        ("fault", "subtitle_multilingual", "web", "h264"),
        ("security", "product_ad", "mobile", "h265"),
    ))
    trace = EvalSuiteRunner().run(
        "p10-matrix",
        (("ok", "contract", lambda: EvalCase("ok", True)),),
        matrix_cases=cases,
        commercial_audit_passed=True,
        commercial_audit_report_id="audit-3",
    )
    decision = evaluate_release_gate(
        trace,
        policy=ReleaseGatePolicy(require_matrix=True),
    )
    assert decision.allowed
    assert decision.matrix_pass_rate == 1.0


def test_release_gate_reports_missing_matrix_categories() -> None:
    trace = EvalSuiteRunner().run(
        "p10-matrix-missing",
        (("ok", "contract", lambda: EvalCase("ok", True)),),
        matrix_cases=(MatrixCase(
            "security", "security", True,
            execution_mode="real", evidence_ref="matrix:security",
            provider_id="provider-1",
        ),),
        commercial_audit_passed=True,
        commercial_audit_report_id="audit-4",
    )
    decision = evaluate_release_gate(
        trace,
        policy=ReleaseGatePolicy(require_matrix=True),
    )
    assert not decision.allowed
    assert any("matrix categories missing" in reason for reason in decision.reasons)


def test_release_gate_defaults_fail_closed_without_matrix_or_audit() -> None:
    trace = EvalSuiteRunner().run(
        "p10-incomplete",
        (("ok", "contract", lambda: EvalCase("ok", True)),),
    )

    decision = evaluate_release_gate(trace)

    assert not decision.allowed
    assert "release test matrix is missing" in decision.reasons
    assert "commercial audit is missing or failed" in decision.reasons
    assert "commercial audit report is missing" in decision.reasons


def test_release_gate_rejects_synthetic_matrix_cases() -> None:
    cases = tuple(
        MatrixCase(name=category, category=category, passed=True)
        for category in ("dataset", "performance", "fault", "security")
    )
    trace = EvalSuiteRunner().run(
        "p10-synthetic-matrix",
        (("ok", "contract", lambda: EvalCase("ok", True)),),
        matrix_cases=cases,
        commercial_audit_passed=True,
        commercial_audit_report_id="audit-synthetic",
    )

    decision = evaluate_release_gate(trace)

    assert not decision.allowed
    assert any("matrix real evidence missing" in reason for reason in decision.reasons)


def test_release_gate_requires_commercial_task_platform_and_codec_coverage() -> None:
    cases = tuple(MatrixCase(
        name=category,
        category=category,
        passed=True,
        execution_mode="real",
        evidence_ref=f"matrix:{category}",
        provider_id="provider-1",
        task_type="product_ad",
        platform="web",
        codec="h264",
    ) for category in ("dataset", "performance", "fault", "security"))
    trace = EvalSuiteRunner().run(
        "p10-incomplete-dimensions",
        (("ok", "contract", lambda: EvalCase("ok", True)),),
        matrix_cases=cases,
        commercial_audit_passed=True,
        commercial_audit_report_id="audit-dimensions",
    )

    decision = evaluate_release_gate(trace)

    assert not decision.allowed
    assert "task_type=interview" in decision.missing_matrix_dimensions
    assert "platform=mobile" in decision.missing_matrix_dimensions
    assert "codec=h265" in decision.missing_matrix_dimensions
