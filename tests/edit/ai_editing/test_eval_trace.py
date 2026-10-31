"""M12 评估 trace 单元测试（§3 evaluation traces / §12.2 / §15.3）。

覆盖：
- EvalCase 数据合同
- EvalMetricSnapshot.from_gate_result
- EvalTrace.passed（gate + 所有 case）
- EvalTraceStore append-only + FIFO + next_run_id
- compare_regression：指标退化检测（higher/lower_is_better）
- regressions 便捷别名
- to_dict 导出 / clear 删除（§15.3）

红线对照：
- 第三粒度审计（§3 evaluation traces）
- 不含 prompt/素材原文（§11.1 M12 / §12.3 第三条）
- 可导出/删除（§15.3）
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.edit_core

from src.features.edit.ai_editing.eval_trace import (
    EvalCase,
    EvalMetricSnapshot,
    EvalTrace,
    EvalTraceStore,
    RegressionFinding,
)
from src.features.edit.ai_editing.quality_gate import (
    QualityGateInput,
    QualityGateResult,
    GateMetric,
    evaluate_quality_gate,
)


# ================================================================
# EvalCase
# ================================================================


class TestEvalCase:
    def test_defaults(self):
        c = EvalCase(name="case1")
        assert c.passed is True
        assert c.duration_ms == 0

    def test_to_dict_json_serializable(self):
        import json
        c = EvalCase(name="c1", passed=False, error_code="TAEP_SCHEMA_INVALID",
                     category="编辑语义")
        json.dumps(c.to_dict())


# ================================================================
# EvalMetricSnapshot
# ================================================================


class TestMetricSnapshot:
    def test_from_gate_result(self):
        result = evaluate_quality_gate(QualityGateInput(unconfirmed_writes=0))
        snap = EvalMetricSnapshot.from_gate_result(result)
        assert "unconfirmed_writes" in snap.values
        assert snap.get("unconfirmed_writes") == 0.0

    def test_get_missing_returns_none(self):
        snap = EvalMetricSnapshot()
        assert snap.get("bogus") is None


# ================================================================
# EvalTrace
# ================================================================


def _make_trace(run_id="run-1", cases=(), gate=None, snapshot=None) -> EvalTrace:
    return EvalTrace(
        run_id=run_id, timestamp=1000.0,
        cases=tuple(cases), gate_result=gate,
        metric_snapshot=snapshot or EvalMetricSnapshot(),
    )


class TestEvalTrace:
    def test_passed_all_cases_and_gate(self):
        gate = evaluate_quality_gate(QualityGateInput())  # passed
        trace = _make_trace(cases=(EvalCase("c1"), EvalCase("c2")), gate=gate)
        assert trace.passed is True

    def test_failed_when_case_fails(self):
        gate = evaluate_quality_gate(QualityGateInput())
        trace = _make_trace(
            cases=(EvalCase("c1", passed=False),), gate=gate,
        )
        assert trace.passed is False
        assert trace.failure_count == 1

    def test_failed_when_gate_fails(self):
        gate = evaluate_quality_gate(QualityGateInput(unconfirmed_writes=1))
        trace = _make_trace(cases=(EvalCase("c1"),), gate=gate)
        assert trace.passed is False

    def test_case_count_and_duration(self):
        trace = _make_trace(cases=(
            EvalCase("c1", duration_ms=100),
            EvalCase("c2", duration_ms=200),
        ))
        assert trace.case_count == 2
        assert trace.total_duration_ms == 300

    def test_to_dict_json_serializable(self):
        import json
        gate = evaluate_quality_gate(QualityGateInput())
        trace = _make_trace(cases=(EvalCase("c1"),), gate=gate,
                            snapshot=EvalMetricSnapshot.from_gate_result(gate))
        json.dumps(trace.to_dict())


# ================================================================
# EvalTraceStore
# ================================================================


class TestStore:
    def test_record_and_count(self):
        store = EvalTraceStore()
        assert store.count == 0
        store.record(_make_trace("run-1"))
        store.record(_make_trace("run-2"))
        assert store.count == 2

    def test_next_run_id_monotonic(self):
        store = EvalTraceStore()
        assert store.next_run_id() == "run-000001"
        assert store.next_run_id() == "run-000002"

    def test_get_by_run_id(self):
        store = EvalTraceStore()
        store.record(_make_trace("run-1"))
        assert store.get("run-1") is not None
        assert store.get("bogus") is None

    def test_latest(self):
        store = EvalTraceStore()
        assert store.latest() is None
        store.record(_make_trace("run-1"))
        store.record(_make_trace("run-2"))
        assert store.latest().run_id == "run-2"

    def test_previous(self):
        store = EvalTraceStore()
        store.record(_make_trace("run-1"))
        store.record(_make_trace("run-2"))
        assert store.previous("run-2").run_id == "run-1"
        assert store.previous("run-1") is None  # 无更早的

    def test_fifo_max_traces(self):
        store = EvalTraceStore(max_traces=2)
        store.record(_make_trace("run-1"))
        store.record(_make_trace("run-2"))
        store.record(_make_trace("run-3"))
        assert store.count == 2
        assert store.get("run-1") is None  # 丢弃最旧
        assert store.get("run-3") is not None


# ================================================================
# compare_regression（回归对比）
# ================================================================


class TestRegression:
    def test_no_previous_returns_empty(self):
        store = EvalTraceStore()
        store.record(_make_trace("run-1", snapshot=EvalMetricSnapshot(values={"x": 1.0})))
        assert store.compare_regression("run-1") == ()

    def test_detects_regression_higher_is_better(self):
        """成功率类指标下降 = 退化。"""
        store = EvalTraceStore()
        store.record(_make_trace("run-1",
            snapshot=EvalMetricSnapshot(values={"evidence_locatable_rate": 0.9})))
        store.record(_make_trace("run-2",
            snapshot=EvalMetricSnapshot(values={"evidence_locatable_rate": 0.7})))
        findings = store.compare_regression("run-2")
        assert len(findings) == 1
        assert findings[0].regressed is True  # 0.7 < 0.9 退化
        assert findings[0].delta == pytest.approx(-0.2)

    def test_detects_regression_lower_is_better(self):
        """泄露类指标上升 = 退化。"""
        store = EvalTraceStore()
        store.record(_make_trace("run-1",
            snapshot=EvalMetricSnapshot(values={"unconfirmed_writes": 0.0})))
        store.record(_make_trace("run-2",
            snapshot=EvalMetricSnapshot(values={"unconfirmed_writes": 2.0})))
        findings = store.compare_regression("run-2")
        assert findings[0].regressed is True  # 0 → 2 退化
        assert findings[0].delta == pytest.approx(2.0)

    def test_improvement_not_regression(self):
        store = EvalTraceStore()
        store.record(_make_trace("run-1",
            snapshot=EvalMetricSnapshot(values={"evidence_locatable_rate": 0.7})))
        store.record(_make_trace("run-2",
            snapshot=EvalMetricSnapshot(values={"evidence_locatable_rate": 0.9})))
        findings = store.compare_regression("run-2")
        assert findings[0].regressed is False  # 0.7 → 0.9 改善

    def test_regressions_alias_filters(self):
        store = EvalTraceStore()
        store.record(_make_trace("run-1", snapshot=EvalMetricSnapshot(values={
            "evidence_locatable_rate": 0.9, "unconfirmed_writes": 0.0,
        })))
        store.record(_make_trace("run-2", snapshot=EvalMetricSnapshot(values={
            "evidence_locatable_rate": 0.7,  # 退化
            "unconfirmed_writes": 0.0,       # 不变
        })))
        regs = store.regressions("run-2")
        assert len(regs) == 1
        assert regs[0].metric_name == "evidence_locatable_rate"

    def test_missing_metric_in_previous_skipped(self):
        """上次没这个指标 → 不对比。"""
        store = EvalTraceStore()
        store.record(_make_trace("run-1", snapshot=EvalMetricSnapshot(values={"a": 1.0})))
        store.record(_make_trace("run-2", snapshot=EvalMetricSnapshot(values={
            "a": 1.0, "b": 2.0,  # b 上次没有
        })))
        findings = store.compare_regression("run-2")
        assert len(findings) == 1  # 只对比了 a


# ================================================================
# 导出/删除（§15.3）
# ================================================================


class TestExportDelete:
    def test_to_dict_json_serializable(self):
        import json
        store = EvalTraceStore()
        store.record(_make_trace("run-1"))
        json.dumps(store.to_dict())

    def test_clear(self):
        store = EvalTraceStore()
        store.record(_make_trace("run-1"))
        store.clear()
        assert store.count == 0
        assert store.next_run_id() == "run-000001"  # counter reset
