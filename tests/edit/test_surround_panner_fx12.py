"""P2 · 环绕声像矩阵（FX-12 · 2026-11-21 计划）。

验收：5.1 的 C/L/Ls 朝向 mono 脉冲命中命名声道；LFE 静音；各方向
增益平方和误差 ≤1e-6；扇区中点双扬声器等功率；±180 回绕正确；
7.1 侧面扇区；旧立体声矩阵不受影响。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.features.edit.core.audio_dsp import (
    spatial_panner_matrix,
    surround_panner_gains,
    surround_speaker_azimuths,
)

LABELS_51 = ["L", "R", "C", "LFE", "Ls", "Rs"]


class TestSurroundGains:
    def test_layout_table(self) -> None:
        table = surround_speaker_azimuths(6)
        assert table["C"] == 0.0 and table["L"] == -30.0
        assert table["LFE"] is None  # 不参加水平投影
        assert -120.0 <= table["Ls"] <= -100.0
        assert surround_speaker_azimuths(2) == {}

    @pytest.mark.parametrize("label,azimuth", [
        ("C", 0.0), ("L", -30.0), ("R", 30.0),
        ("Ls", -110.0), ("Rs", 110.0)])
    def test_exact_speaker_hits(self, label, azimuth) -> None:
        """朝向命名扬声器 → 单声道命中；LFE 静音；平方和 1。"""
        gains = surround_panner_gains(6, azimuth)
        index = LABELS_51.index(label)
        expected = np.zeros(6)
        expected[index] = 1.0
        np.testing.assert_allclose(gains, expected, atol=1e-9)
        assert gains[LABELS_51.index("LFE")] == 0.0
        assert np.sum(gains ** 2) == pytest.approx(1.0, abs=1e-6)

    def test_sector_midpoint_equal_power(self) -> None:
        """扇区中点：双扬声器等功率（平方和 1）。"""
        gains = surround_panner_gains(6, -15.0)  # L/C 中点
        assert gains[LABELS_51.index("L")] == pytest.approx(2 ** -0.5, 1e-6)
        assert gains[LABELS_51.index("C")] == pytest.approx(2 ** -0.5, 1e-6)
        assert np.sum(gains ** 2) == pytest.approx(1.0, abs=1e-6)

    def test_rear_wraparound(self) -> None:
        """正后方（±180）：Ls/Rs 扇区回绕中点。"""
        gains = surround_panner_gains(6, 180.0)
        assert gains[LABELS_51.index("Ls")] == pytest.approx(2 ** -0.5, 1e-6)
        assert gains[LABELS_51.index("Rs")] == pytest.approx(2 ** -0.5, 1e-6)

    def test_seven_point_one_side_pair(self) -> None:
        """7.1：−70° 落在 L/Lss 侧面扇区。"""
        gains = surround_panner_gains(8, -70.0)
        labels = ["L", "R", "C", "LFE", "Ls", "Rs", "Lss", "Rss"]
        assert gains[labels.index("L")] > 0.0
        assert gains[labels.index("Lss")] > 0.0
        assert gains[labels.index("Ls")] == 0.0
        assert np.sum(gains ** 2) == pytest.approx(1.0, abs=1e-6)

    def test_outside_sector_raises(self) -> None:
        from src.features.edit.core.audio_dsp import _pair_gains
        with pytest.raises(ValueError):
            _pair_gains(-30.0, 30.0, 170.0)  # 目标在扇区外


class TestStereoBaseline:
    def test_stereo_matrix_untouched(self) -> None:
        """旧立体声矩阵：中位单位矩阵、极左交叉为零。"""
        np.testing.assert_allclose(
            spatial_panner_matrix(0.0, {"enabled": True}), np.eye(2),
            atol=1e-9)
        m = spatial_panner_matrix(1.0, {"enabled": True, "spread": 100.0,
                                        "spread_auto": False})
        assert m[0, 1] < 1e-9  # 极右时左扬声器不收右声道内容
