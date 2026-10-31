"""M22 · GradeDiff 回归。"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

pytestmark = pytest.mark.color_core

from src.features.color.core.grade_diff import frame_mean_stats, grade_diff


class TestGradeDiff:
    def test_identical_frames_zero_diff(self):
        img = np.full((8, 8, 3), 0.5, np.float32)
        d = grade_diff(img, img)
        assert d["exposure_stops"] == pytest.approx(0.0, abs=1e-6)
        assert d["temp_delta"] == pytest.approx(0.0, abs=1e-6)
        assert d["sat_ratio"] == pytest.approx(1.0, abs=1e-3)

    def test_one_stop_brighter(self):
        ref = np.full((8, 8, 3), 0.25, np.float32)
        pre = np.full((8, 8, 3), 0.5, np.float32)
        d = grade_diff(ref, pre)
        assert d["exposure_stops"] == pytest.approx(1.0, abs=0.01)

    def test_warmer_preview_positive_temp(self):
        ref = np.full((8, 8, 3), 0.5, np.float32)
        pre = ref.copy()
        pre[..., 0] = 0.6  # R 升
        pre[..., 2] = 0.4  # B 降
        d = grade_diff(ref, pre)
        assert d["temp_delta"] > 0

    def test_summary_readable(self):
        img = np.full((4, 4, 3), 0.3, np.float32)
        d = grade_diff(img, img * 2.0)
        assert "档" in d["summary"] and "饱和" in d["summary"]
