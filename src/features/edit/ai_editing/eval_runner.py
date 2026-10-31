"""Offline evaluation and release gate helpers (P10)."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Iterable

from src.features.edit.ai_editing.eval_trace import (
    EvalCase,
    EvalMetricSnapshot,
    EvalTrace,
)
from src.features.edit.ai_editing.commercial_acceptance import CommercialAcceptanceReport
from src.features.edit.ai_editing.quality_gate import (
    QualityGateInput,
    evaluate_quality_gate,
)
from src.features.edit.ai_editing.privacy_guard import redact_text
from src.features.edit.ai_editing.release_test_matrix import (
    DEFAULT_COMMERCIAL_MATRIX_DIMENSIONS,
    MatrixCase,
    evaluate_test_matrix,
    find_missing_dimensions,
)


EvalCaseFactory = Callable[[], EvalCase]


@dataclass(frozen=True)
class ReleaseGatePolicy:
    min_case_pass_rate: float = 1.0
    require_quality_gate: bool = True
    max_hard_gate_failures: int = 0
    require_matrix: bool = True
    min_matrix_pass_rate: float = 1.0
    require_commercial_audit: bool = True
    require_commercial_acceptance: bool = False
    require_real_matrix_evidence: bool = True
    required_matrix_dimensions: tuple[tuple[str, tuple[str, ...]], ...] = (
        DEFAULT_COMMERCIAL_MATRIX_DIMENSIONS
    )


@dataclass(frozen=True)
class ReleaseGateResult:
    allowed: bool
    reasons: tuple[str, ...] = ()
    case_pass_rate: float = 0.0
    quality_gate_passed: bool = True
    matrix_pass_rate: float | None = None
    commercial_audit_passed: bool = False
    commercial_audit_report_id: str = ""
    commercial_acceptance_passed: bool = False
    commercial_acceptance_report_id: str = ""
    missing_matrix_dimensions: tuple[str, ...] = ()


class EvalSuiteRunner:
    """Run fixture-only cases and produce a privacy-safe EvalTrace."""

    def __init__(self, *, clock: Callable[[], float] | None = None) -> None:
        self._clock = clock or time.time

    def run(
        self,
        suite_name: str,
        cases: Iterable[tuple[str, str, EvalCaseFactory]],
        *,
        quality_input: QualityGateInput | None = None,
        matrix_cases: Iterable[MatrixCase] | None = None,
        run_id: str = "",
        commercial_audit_passed: bool = False,
        commercial_audit_report_id: str = "",
        commercial_acceptance: CommercialAcceptanceReport | None = None,
        required_matrix_dimensions: tuple[tuple[str, tuple[str, ...]], ...] = (
            DEFAULT_COMMERCIAL_MATRIX_DIMENSIONS
        ),
    ) -> EvalTrace:
        results: list[EvalCase] = []
        for name, category, factory in cases:
            started = self._clock()
            try:
                case = factory()
                if not isinstance(case, EvalCase):
                    raise TypeError("eval case factory must return EvalCase")
                results.append(EvalCase(
                    name=name,
                    passed=case.passed,
                    duration_ms=max(case.duration_ms, int((self._clock() - started) * 1000)),
                    error_code=case.error_code,
                    error_message=redact_text(case.error_message)[:300],
                    category=category or case.category,
                ))
            except Exception as exc:  # noqa: BLE001 — failed case is data, not a crash
                results.append(EvalCase(
                    name=name,
                    passed=False,
                    duration_ms=max(0, int((self._clock() - started) * 1000)),
                    error_code="EVAL_CASE_EXCEPTION",
                    error_message=redact_text(f"{type(exc).__name__}: {exc}")[:300],
                    category=category,
                ))
        gate = evaluate_quality_gate(quality_input or QualityGateInput())
        matrix_result = (
            evaluate_test_matrix(
                matrix_cases,
                min_pass_rate=1.0,
                required_dimensions=required_matrix_dimensions,
            )
            if matrix_cases is not None else None
        )
        return EvalTrace(
            run_id=run_id or f"eval-{int(self._clock() * 1000)}",
            timestamp=self._clock(),
            suite_name=suite_name,
            cases=tuple(results),
            gate_result=gate,
            metric_snapshot=EvalMetricSnapshot.from_gate_result(gate),
            matrix_result=matrix_result,
            commercial_audit_passed=commercial_audit_passed,
            commercial_audit_report_id=redact_text(str(commercial_audit_report_id))[:200],
            commercial_acceptance=commercial_acceptance,
        )


def evaluate_release_gate(
    trace: EvalTrace,
    *,
    policy: ReleaseGatePolicy | None = None,
) -> ReleaseGateResult:
    """Decide whether an eval trace is eligible for release/Beta promotion."""
    policy = policy or ReleaseGatePolicy()
    count = trace.case_count
    passed = sum(1 for case in trace.cases if case.passed)
    rate = passed / count if count else 0.0
    gate = trace.gate_result
    gate_passed = gate.passed if gate is not None else not policy.require_quality_gate
    matrix = trace.matrix_result
    matrix_pass_rate = matrix.case_pass_rate if matrix is not None else None
    missing_dimensions = (
        find_missing_dimensions(matrix.cases, policy.required_matrix_dimensions)
        if matrix is not None else ()
    )
    reasons: list[str] = []
    if rate < policy.min_case_pass_rate:
        reasons.append(f"case pass rate {rate:.3f} below {policy.min_case_pass_rate:.3f}")
    if policy.require_quality_gate and gate is None:
        reasons.append("quality gate result is missing")
    elif gate is not None and len(gate.hard_gate_failures) > policy.max_hard_gate_failures:
        reasons.append(
            f"quality hard-gate failures {len(gate.hard_gate_failures)} "
            f"exceed {policy.max_hard_gate_failures}"
        )
    if policy.require_matrix and matrix is None:
        reasons.append("release test matrix is missing")
    elif matrix is not None:
        if matrix.case_pass_rate < policy.min_matrix_pass_rate:
            reasons.append(
                f"matrix pass rate {matrix.case_pass_rate:.3f} below "
                f"{policy.min_matrix_pass_rate:.3f}"
            )
        if matrix.missing_categories:
            reasons.append("matrix categories missing: " + ", ".join(matrix.missing_categories))
        if matrix.failed_cases:
            reasons.append("matrix cases failed: " + ", ".join(matrix.failed_cases[:5]))
        if policy.require_real_matrix_evidence and not matrix.real_evidence_complete:
            missing_real = ", ".join(matrix.missing_real_evidence_cases[:5])
            reasons.append("matrix real evidence missing: " + (missing_real or "unknown"))
        if missing_dimensions:
            reasons.append("matrix dimensions missing: " + ", ".join(missing_dimensions[:8]))
    if policy.require_commercial_audit:
        if not trace.commercial_audit_passed:
            reasons.append("commercial audit is missing or failed")
        if not trace.commercial_audit_report_id:
            reasons.append("commercial audit report is missing")
    acceptance = trace.commercial_acceptance
    if policy.require_commercial_acceptance:
        if acceptance is None:
            reasons.append("commercial panel acceptance report is missing")
        elif not acceptance.passed:
            reasons.append(
                "commercial panel acceptance failed: "
                + ", ".join(acceptance.failure_reasons()[:5])
            )
    return ReleaseGateResult(
        allowed=not reasons and gate_passed,
        reasons=tuple(reasons),
        case_pass_rate=rate,
        quality_gate_passed=gate_passed,
        matrix_pass_rate=matrix_pass_rate,
        commercial_audit_passed=trace.commercial_audit_passed,
        commercial_audit_report_id=trace.commercial_audit_report_id,
        commercial_acceptance_passed=bool(acceptance and acceptance.passed),
        commercial_acceptance_report_id=acceptance.report_id if acceptance else "",
        missing_matrix_dimensions=missing_dimensions,
    )


__all__ = [
    "EvalSuiteRunner",
    "ReleaseGatePolicy",
    "ReleaseGateResult",
    "evaluate_release_gate",
]
