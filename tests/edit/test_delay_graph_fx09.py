"""P2 · 延迟回声图数据与首跳合同（FX-09 · 2026-11-21 计划）。

验收：collect_graph_data 发布读头位置（样本+ms）/实际反馈/交叉比/
状态版本；整数读头首跳落在预期样本 ±1（44.1/48/96 kHz）；串联两条
80 ms 无反馈 → 第 160 ms 回声进入导出。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.features.edit.core.audio_dsp import (
    ALGORITHM_VERSION,
    DelayEffect,
    EffectChain,
)

WET_ONLY = {"left_delay": 80.0, "right_delay": 80.0,
            "left_feedback": 0.0, "right_feedback": 0.0,
            "dry_wet": 100.0, "low_cut": 20.0, "high_cut": 18000.0,
            "enabled": True}


class TestGraphData:
    def test_publishes_read_heads_and_feedback(self) -> None:
        """图数据：读头位置（样本+ms）、实际反馈、状态版本、尾音。"""
        fx = DelayEffect(48000)
        fx.set_params({"left_delay": 80.0, "right_delay": 121.0,
                       "left_feedback": 50.0, "right_feedback": -25.0,
                       "stereo_blend": 30.0, "dry_wet": 40.0,
                       "enabled": True})
        data = fx.collect_graph_data()
        assert data["read_head_left_ms"] == 80.0
        assert data["read_head_left_samples"] == pytest.approx(3840.0)
        assert data["read_head_right_samples"] == pytest.approx(
            121.0 * 48.0, abs=0.1)
        assert data["feedback_left"] == 50.0
        assert data["feedback_right"] == -25.0
        assert data["blend"] == 30.0
        assert data["dry_wet"] == 40.0
        assert data["state"] == "active"
        assert data["tail_estimate_samples"] > 0

    def test_bypass_state_reported(self) -> None:
        fx = DelayEffect(48000)
        fx.set_params(dict(WET_ONLY, enabled=False))
        assert fx.collect_graph_data()["state"] == "bypassed"


class TestFirstHop:
    @pytest.mark.parametrize("sample_rate", [44100, 48000, 96000])
    def test_integer_read_head_first_hop(self, sample_rate) -> None:
        """整数读头首跳落在预期样本 ±1（单边脉冲、无反馈、全湿）。"""
        fx = DelayEffect(float(sample_rate))
        fx.set_params(dict(WET_ONLY))
        n = int(sample_rate * 0.3)
        pulse = np.zeros((2, n), dtype=np.float32)
        pulse[0, 0] = 1.0
        out = fx.process(pulse)
        expected = round(80.0 * sample_rate / 1000.0)
        nonzero = np.flatnonzero(np.abs(out[0]) > 1e-4)
        assert nonzero.size > 0
        assert nonzero[0] == pytest.approx(expected, abs=1)


class TestSeriesTail:
    def test_series_80ms_echo_reaches_export(self) -> None:
        """串联两条 80 ms 无反馈：160 ms 回声进入导出（尾音累计）。"""
        sample_rate = 48000
        chain = EffectChain(float(sample_rate))
        chain.set_params([
            ("delay", dict(WET_ONLY), True, "d1", ALGORITHM_VERSION),
            ("delay", dict(WET_ONLY), True, "d2", ALGORITHM_VERSION),
        ], sample_rate=sample_rate)
        n = int(sample_rate * 0.3)
        pulse = np.zeros((2, n), dtype=np.float32)
        pulse[0, 0] = 1.0
        out = chain.process(pulse)
        # 全湿串联：唯一回声 = 80+80 ms（直接脉冲被第二级湿声吞掉）。
        assert np.abs(out[0, 7680]) > 1e-4
        before = np.abs(out[0, :3840])
        assert float(np.max(before)) < 1e-4  # 早于 80 ms 无内容
