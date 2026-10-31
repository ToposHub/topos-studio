"""Standard dataset/performance/fault/security release matrix (P10)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from src.features.edit.ai_editing.privacy_guard import redact_text


DEFAULT_COMMERCIAL_MATRIX_DIMENSIONS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("task_type", ("product_ad", "interview", "subtitle_multilingual")),
    ("platform", ("web", "mobile")),
    ("codec", ("h264", "h265")),
)


@dataclass(frozen=True)
class MatrixCase:
    name: str
    category: str
    passed: bool
    error_code: str = ""
    error_message: str = ""
    execution_mode: str = "synthetic"  # synthetic / real
    evidence_ref: str = ""              # 脱敏报告/工件引用
    provider_id: str = ""
    task_type: str = ""
    platform: str = ""
    codec: str = ""

    @property
    def has_real_evidence(self) -> bool:
        """Whether this case proves a real provider execution."""
        return (
            self.execution_mode == "real"
            and bool(self.evidence_ref.strip())
            and bool(self.provider_id.strip())
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "category": self.category,
            "passed": self.passed,
            "error_code": self.error_code,
            "error_message": redact_text(self.error_message)[:300],
            "execution_mode": self.execution_mode,
            "evidence_ref": redact_text(self.evidence_ref)[:200],
            "provider_id": redact_text(self.provider_id)[:100],
            "task_type": redact_text(self.task_type)[:100],
            "platform": redact_text(self.platform)[:100],
            "codec": redact_text(self.codec)[:100],
        }


@dataclass(frozen=True)
class MatrixResult:
    passed: bool
    case_pass_rate: float
    missing_categories: tuple[str, ...] = ()
    failed_cases: tuple[str, ...] = ()
    missing_real_evidence_cases: tuple[str, ...] = ()
    real_evidence_complete: bool = False
    missing_dimensions: tuple[str, ...] = ()
    cases: tuple[MatrixCase, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "passed": self.passed,
            "case_pass_rate": self.case_pass_rate,
            "missing_categories": list(self.missing_categories),
            "failed_cases": list(self.failed_cases),
            "missing_real_evidence_cases": list(self.missing_real_evidence_cases),
            "real_evidence_complete": self.real_evidence_complete,
            "missing_dimensions": list(self.missing_dimensions),
            "cases": [case.to_dict() for case in self.cases],
        }


def find_missing_dimensions(
    cases: Iterable[MatrixCase],
    required_dimensions: tuple[tuple[str, tuple[str, ...]], ...] = (),
) -> tuple[str, ...]:
    """Return required dimension values not represented by the matrix cases."""
    normalized = tuple(cases)
    missing: list[str] = []
    for field_name, required_values in required_dimensions:
        observed = {str(getattr(case, field_name, "")).strip() for case in normalized}
        missing.extend(
            f"{field_name}={value}"
            for value in required_values
            if value not in observed
        )
    return tuple(missing)


def evaluate_test_matrix(
    cases: Iterable[MatrixCase],
    *,
    required_categories: tuple[str, ...] = (
        "dataset", "performance", "fault", "security",
    ),
    min_pass_rate: float = 1.0,
    require_real_evidence: bool = False,
    required_dimensions: tuple[tuple[str, tuple[str, ...]], ...] = (),
) -> MatrixResult:
    """Require coverage, pass rate and optionally real execution evidence.

    Synthetic cases remain valid for local diagnostics, but their absence of
    real evidence is always reported so a release gate can fail closed later.

    AUD-14212：``min_pass_rate`` 是生效的通过率阈值——1.0 即零容忍；
    <1.0 允许容忍个别失败 case（失败明细仍完整列在 ``failed_cases``，
    便于区分"按阈值拒绝"与"按零容忍拒绝"），不再被 ``not failed``
    条件蕴含成死参数。
    """
    if not 0.0 <= min_pass_rate <= 1.0:
        raise ValueError("min_pass_rate must be in [0, 1]")
    normalized = tuple(cases)
    passed = sum(1 for case in normalized if case.passed)
    rate = passed / len(normalized) if normalized else 0.0
    present = {case.category for case in normalized}
    missing = tuple(category for category in required_categories if category not in present)
    failed = tuple(case.name for case in normalized if not case.passed)
    missing_real = tuple(case.name for case in normalized if not case.has_real_evidence)
    real_complete = bool(normalized) and not missing_real
    missing_dimensions = find_missing_dimensions(normalized, required_dimensions)
    matrix_passed = bool(normalized) and rate >= min_pass_rate and not missing
    if require_real_evidence and not real_complete:
        matrix_passed = False
    if missing_dimensions:
        matrix_passed = False
    return MatrixResult(
        passed=matrix_passed,
        case_pass_rate=rate,
        missing_categories=missing,
        failed_cases=failed,
        missing_real_evidence_cases=missing_real,
        real_evidence_complete=real_complete,
        missing_dimensions=missing_dimensions,
        cases=normalized,
    )


__all__ = [
    "DEFAULT_COMMERCIAL_MATRIX_DIMENSIONS",
    "MatrixCase",
    "MatrixResult",
    "evaluate_test_matrix",
    "find_missing_dimensions",
]
