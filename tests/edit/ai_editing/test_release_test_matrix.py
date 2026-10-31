"""Release matrix provenance and real-execution evidence tests."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.edit_core

from src.features.edit.ai_editing.release_test_matrix import (
    DEFAULT_COMMERCIAL_MATRIX_DIMENSIONS,
    MatrixCase,
    evaluate_test_matrix,
)


def _real_cases() -> tuple[MatrixCase, ...]:
    return tuple(
        MatrixCase(
            name=category,
            category=category,
            passed=True,
            execution_mode="real",
            evidence_ref=f"run:{category}",
            provider_id="provider-1",
            task_type="product_ad",
            platform="web",
            codec="h264",
        )
        for category in ("dataset", "performance", "fault", "security")
    )


def test_matrix_reports_real_evidence_completion() -> None:
    result = evaluate_test_matrix(_real_cases())

    assert result.passed
    assert result.real_evidence_complete
    assert result.missing_real_evidence_cases == ()


def test_synthetic_case_is_visible_but_not_a_release_proof() -> None:
    cases = _real_cases()[:-1] + (MatrixCase(
        name="security",
        category="security",
        passed=True,
        execution_mode="synthetic",
    ),)

    result = evaluate_test_matrix(cases)

    assert result.passed  # local matrix quality is still valid
    assert not result.real_evidence_complete
    assert result.missing_real_evidence_cases == ("security",)


def test_real_case_requires_provider_and_evidence_reference() -> None:
    result = evaluate_test_matrix(
        _real_cases()[:-1] + (MatrixCase(
            name="security",
            category="security",
            passed=True,
            execution_mode="real",
        ),)
    )

    assert not result.real_evidence_complete
    assert result.missing_real_evidence_cases == ("security",)


def test_require_real_evidence_can_fail_matrix_directly() -> None:
    result = evaluate_test_matrix(
        _real_cases()[:-1] + (MatrixCase("security", "security", True),),
        require_real_evidence=True,
    )

    assert not result.passed


def test_required_dimensions_are_reported_and_block_matrix() -> None:
    result = evaluate_test_matrix(
        _real_cases(),
        required_dimensions=DEFAULT_COMMERCIAL_MATRIX_DIMENSIONS,
    )

    assert not result.passed
    assert "task_type=interview" in result.missing_dimensions
    assert "platform=mobile" in result.missing_dimensions
    assert "codec=h265" in result.missing_dimensions
