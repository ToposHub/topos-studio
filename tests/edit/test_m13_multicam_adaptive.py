"""M13.5/M13.6 · 多机位降档与 DPI 恢复回归。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

pytestmark = pytest.mark.edit_core

from src.features.edit.core.multicam_adaptive import (
    dpi_restore_geometry,
    multicam_quality_policy,
)


class TestMulticamQualityPolicy:
    def test_four_angles_full_baseline(self):
        p = multicam_quality_policy(4)
        assert p["scale"] == 1.0 and not p["degraded"]

    def test_six_heavy_half(self):
        p = multicam_quality_policy(6, program_load="heavy")
        assert p["scale"] == 0.5 and p["degraded"]

    def test_six_light_full(self):
        p = multicam_quality_policy(6, program_load="light")
        assert p["scale"] == 1.0 and not p["degraded"]

    def test_nine_half_light_quarter_heavy(self):
        assert multicam_quality_policy(9)["scale"] == 0.5
        assert multicam_quality_policy(9, program_load="heavy")["scale"] == 0.25

    def test_over_nine_quarter_divider2_explicit(self):
        p = multicam_quality_policy(12)
        assert p["scale"] == 0.25 and p["frame_divider"] == 2
        assert p["degraded"] and "不承诺" in p["reason"]

    def test_measured_in_budget_overrides(self):
        # 实测在预算内：即使 12 角度也不降档
        p = multicam_quality_policy(12, measured_p95_budget_ms=35.0)
        assert p["scale"] == 1.0 and not p["degraded"]

    def test_measured_over_budget_degrades(self):
        p = multicam_quality_policy(4, measured_p95_budget_ms=60.0)
        assert p["scale"] == 0.5 and p["degraded"]


class TestDpiRestoreGeometry:
    SCREENS = [(0, 0, 1920, 1080), (1920, 0, 2560, 1440)]

    def test_visible_window_untouched(self):
        geo = (200, 200, 800, 600)
        assert dpi_restore_geometry(geo, self.SCREENS) == geo

    def test_second_screen_window_kept(self):
        geo = (2200, 100, 800, 600)  # 完全在第二屏
        assert dpi_restore_geometry(geo, self.SCREENS) == geo

    def test_dead_screen_falls_back_to_first(self):
        # 只剩第一屏；窗口原在已拔的第二屏
        geo = (3000, 500, 800, 600)
        restored = dpi_restore_geometry(geo, [self.SCREENS[0]])
        assert restored == (1120, 480, 800, 600)  # 钳入屏内

    def test_clamp_when_window_larger_than_screen(self):
        restored = dpi_restore_geometry(
            (5000, 5000, 4000, 3000), [(0, 0, 1920, 1080)])
        assert restored[0] == 0 and restored[1] == 0

    def test_no_screens_passthrough(self):
        geo = (100, 100, 500, 400)
        assert dpi_restore_geometry(geo, []) == geo
