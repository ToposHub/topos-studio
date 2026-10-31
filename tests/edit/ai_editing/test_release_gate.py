"""Release gate tests (plan §9 / §A7 §8).

Validates that the metrics summary is checked against the plan §9 performance
thresholds, with hard failures for latency and a soft warning for duration,
and that missing data fails closed rather than passing vacuously.
"""

from __future__ import annotations

import pytest

from src.features.edit.ai_editing.release_gate import (
    DEFAULT_MAX_CANCEL_MS,
    DEFAULT_MAX_FIRST_TOKEN_MS,
    evaluate_release_gate,
)


class TestPassingGate:
    def test_all_metrics_within_thresholds(self):
        r = evaluate_release_gate(
            {
                "total_runs": 10,
                "avg_first_token_latency_ms": 120.0,
                "avg_cancel_latency_ms": 80.0,
                "avg_duration_ms": 5000.0,
            }
        )
        assert r.passed
        assert not r.failures
        assert not r.warnings

    def test_passes_with_partial_metrics(self):
        # Cancel latency absent (no cancellations observed) -> skipped, not failed.
        r = evaluate_release_gate(
            {"total_runs": 5, "avg_first_token_latency_ms": 100.0}
        )
        assert r.passed


class TestHardFailures:
    def test_first_token_latency_exceeds_gate(self):
        r = evaluate_release_gate(
            {"total_runs": 3, "avg_first_token_latency_ms": 350.0}
        )
        assert not r.passed
        assert any("首 token" in f for f in r.failures)

    def test_cancel_latency_exceeds_gate(self):
        r = evaluate_release_gate(
            {"total_runs": 3, "avg_cancel_latency_ms": 500.0}
        )
        assert not r.passed
        assert any("取消" in f for f in r.failures)

    def test_thresholds_match_plan(self):
        assert DEFAULT_MAX_FIRST_TOKEN_MS == 200.0
        assert DEFAULT_MAX_CANCEL_MS == 200.0


class TestNoDataFailsClosed:
    def test_zero_runs_fails(self):
        r = evaluate_release_gate({"total_runs": 0})
        assert not r.passed
        assert any("无运行数据" in f for f in r.failures)

    def test_missing_total_runs_fails(self):
        r = evaluate_release_gate({})
        assert not r.passed


class TestSoftWarnings:
    def test_slow_duration_is_warning_not_failure(self):
        # AUD-14208：首 token 是硬指标，缺数据即失败——本用例的主题是
        # duration 软警告，因此 summary 提供首 token 数据。
        r = evaluate_release_gate(
            {"total_runs": 3, "avg_first_token_latency_ms": 100.0,
             "avg_duration_ms": 60_000.0}
        )
        # A slow average is a warning; the gate still passes.
        assert r.passed
        assert any("运行时长" in w for w in r.warnings)

    def test_fast_duration_no_warning(self):
        r = evaluate_release_gate(
            {"total_runs": 3, "avg_first_token_latency_ms": 100.0,
             "avg_duration_ms": 2_000.0}
        )
        assert r.passed
        assert not r.warnings


class TestSerialization:
    def test_result_to_dict(self):
        r = evaluate_release_gate(
            {"total_runs": 2, "avg_first_token_latency_ms": 300.0}
        )
        d = r.to_dict()
        assert d["passed"] is False
        assert len(d["checks"]) >= 2
        assert any(c["name"] == "first_token_latency" for c in d["checks"])
