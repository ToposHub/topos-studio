"""M12 质量门 单元测试（§11.1 M12 / §11.2 M12 / §12.2 硬门槛）。

覆盖：
- QualityGateInput 数据合同（含/不含软 KPI）
- GateMetric.met 判定（higher_is_better / lower_is_better）
- evaluate_quality_gate：硬门槛 enforce（unconfirmed_writes/secret_leaks/path_leaks = 0）
- 硬门槛不达标 → passed=False + TAEP_QUALITY_GATE_FAILED 诊断
- 软 KPI 不达标 → 记 met=False 但 passed 不受影响
- KpiTargets 可配置
- QualityGateResult.hard_gate_failures / soft_kpi_misses

红线对照：
- 安全指标必须是硬门槛（§12.2）
- 不靠 demo 判定完成（§11.1 M12 绝对不要做）
- 不含 prompt/素材/路径原文（§11.1 M12 / §12.3 第三条）
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.edit_core

from src.features.edit.ai_editing.diagnostics import TaepDiagnosticCode
from src.features.edit.ai_editing.quality_gate import (
    GateMetric,
    KpiTargets,
    QualityGateInput,
    QualityGateResult,
    evaluate_quality_gate,
)


# ================================================================
# GateMetric
# ================================================================


class TestGateMetric:
    def test_met_higher_is_better(self):
        m = GateMetric(name="success_rate", value=0.9, target=0.8, higher_is_better=True)
        assert m.met is True
        m2 = GateMetric(name="success_rate", value=0.7, target=0.8, higher_is_better=True)
        assert m2.met is False

    def test_met_lower_is_better(self):
        m = GateMetric(name="leaks", value=0, target=0, higher_is_better=False)
        assert m.met is True
        m2 = GateMetric(name="leaks", value=1, target=0, higher_is_better=False)
        assert m2.met is False


# ================================================================
# evaluate_quality_gate —— 硬门槛
# ================================================================


class TestHardGates:
    def test_all_zeros_passes(self):
        """红线（§12.2）：unconfirmed_writes/secret_leaks/path_leaks 都 = 0 → pass。"""
        result = evaluate_quality_gate(QualityGateInput())
        assert result.passed is True
        assert result.diagnostics == ()
        assert len(result.hard_gate_failures) == 0

    def test_unconfirmed_writes_fails(self):
        """红线（§0 第二条 / §12.2）：未确认写入 ≠ 0 → fail。"""
        result = evaluate_quality_gate(QualityGateInput(unconfirmed_writes=1))
        assert result.passed is False
        assert any(d.code == TaepDiagnosticCode.QUALITY_GATE_FAILED for d in result.diagnostics)
        assert len(result.hard_gate_failures) == 1
        assert result.hard_gate_failures[0].name == "unconfirmed_writes"

    def test_secret_leaks_fails(self):
        """红线（§0 第四条 / §15.5）：secret 泄露 ≠ 0 → fail。"""
        result = evaluate_quality_gate(QualityGateInput(secret_leaks=2))
        assert result.passed is False
        assert len(result.hard_gate_failures) == 1

    def test_path_leaks_fails(self):
        result = evaluate_quality_gate(QualityGateInput(path_leaks=1))
        assert result.passed is False
        assert result.hard_gate_failures[0].name == "path_leaks"

    def test_multiple_hard_failures(self):
        result = evaluate_quality_gate(QualityGateInput(
            unconfirmed_writes=3, secret_leaks=1, path_leaks=2,
        ))
        assert result.passed is False
        assert len(result.hard_gate_failures) == 3
        assert len(result.diagnostics) == 3


# ================================================================
# 软 KPI（记录不硬拒）
# ================================================================


class TestSoftKpi:
    def test_soft_kpi_miss_does_not_fail_gate(self):
        """软 KPI 不达标 → met=False 但 passed=True（不硬拒）。"""
        result = evaluate_quality_gate(QualityGateInput(
            evidence_locatable_rate=0.3,  # 低于 target 0.8
        ))
        assert result.passed is True  # 硬门槛全过
        assert len(result.soft_kpi_misses) == 1
        assert result.soft_kpi_misses[0].name == "evidence_locatable_rate"

    def test_soft_kpi_met_not_in_misses(self):
        result = evaluate_quality_gate(QualityGateInput(
            evidence_locatable_rate=0.9,  # 高于 target 0.8
        ))
        assert len(result.soft_kpi_misses) == 0

    def test_cancel_residual_lower_is_better(self):
        """cancel_residual_rate 越低越好（<= target）。"""
        result = evaluate_quality_gate(QualityGateInput(cancel_residual_rate=0.01))
        # target 默认 0.05；0.01 <= 0.05 → met
        metric = [m for m in result.metrics if m.name == "cancel_residual_rate"][0]
        assert metric.met is True

    def test_none_kpi_skipped(self):
        """未提供的软 KPI（None）不出现在 metrics。"""
        result = evaluate_quality_gate(QualityGateInput())  # 全 None
        soft = [m for m in result.metrics if not m.hard_gate]
        assert len(soft) == 0  # 全跳过

    def test_custom_targets(self):
        """KpiTargets 可配置。"""
        targets = KpiTargets(evidence_locatable_rate=0.95)
        result = evaluate_quality_gate(
            QualityGateInput(evidence_locatable_rate=0.9), targets=targets,
        )
        metric = [m for m in result.metrics if m.name == "evidence_locatable_rate"][0]
        assert metric.target == 0.95
        assert metric.met is False  # 0.9 < 0.95


# ================================================================
# QualityGateResult
# ================================================================


class TestResult:
    def test_to_dict_json_serializable(self):
        import json
        result = evaluate_quality_gate(QualityGateInput(unconfirmed_writes=1))
        json.dumps(result.to_dict())

    def test_frozen(self):
        result = evaluate_quality_gate(QualityGateInput())
        with pytest.raises(Exception):
            result.passed = False  # type: ignore[misc]

    def test_quality_gate_input_no_sensitive_fields(self):
        """红线（§11.1 M12 / §12.3）：input 不含 prompt/素材/路径原文。"""
        inp = QualityGateInput()
        d = inp.to_dict()
        for forbidden in ("prompt", "asset", "path", "secret", "api_key", "transcript"):
            assert forbidden not in d or forbidden in ("evidence_locatable_rate",)  # rate 名含字面量但非原文
