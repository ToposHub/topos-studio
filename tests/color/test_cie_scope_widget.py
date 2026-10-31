"""CieGpuScopeWidget: reference-style CIE chrome, fixed full view."""
import math

import pytest


@pytest.fixture(scope='module')
def qapp_mod():
    from PyQt6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


def _make_widget(qapp_mod):
    from src.shared.ui.widgets.cie_scope_widget import CieGpuScopeWidget
    w = CieGpuScopeWidget()
    w.resize(500, 500)
    return w


class TestColorTempLocusMath:
    def test_planckian_known_points(self):
        from src.shared.ui.widgets.cie_scope_widget import planckian_locus_xy
        # 6500K planckian ≈ (0.3135, 0.3237); 2856K (illuminant A)
        # ≈ (0.4468, 0.4072).
        x, y = planckian_locus_xy(6500)
        assert abs(x - 0.3135) < 0.003 and abs(y - 0.3237) < 0.003
        x, y = planckian_locus_xy(2856)
        assert abs(x - 0.4468) < 0.004 and abs(y - 0.4072) < 0.004
        assert planckian_locus_xy(1000) is None
        assert planckian_locus_xy(30000) is None

    def test_daylight_lands_on_d65(self):
        from src.shared.ui.widgets.cie_scope_widget import daylight_locus_xy
        x, y = daylight_locus_xy(6504)
        assert abs(x - 0.3127) < 0.0015 and abs(y - 0.3290) < 0.0015
        assert daylight_locus_xy(2000) is None  # below D-series range


class TestWavelengthColors:
    def test_spectral_gradient(self):
        from src.shared.ui.widgets.cie_scope_widget import wavelength_to_rgb
        green = wavelength_to_rgb(550)
        assert green.green() > 200 and green.blue() < 100
        blue = wavelength_to_rgb(450)
        assert blue.blue() > 200 and blue.red() < 40
        red = wavelength_to_rgb(660)
        assert red.red() > 180 and red.green() < 30
        # Edge wavelengths are dimmed (Bruton intensity factor).
        assert wavelength_to_rgb(381).value() < wavelength_to_rgb(500).value()


class TestGamutPresets:
    def test_reference_presets_complete(self):
        from src.shared.ui.widgets.cie_scope_widget import CIE_GAMUT_PRESETS
        assert set(CIE_GAMUT_PRESETS) == {
            'sRGB', 'Rec. 709', 'Adobe RGB', 'DCI P3', 'Rec. 2020'}
        bt2020 = CIE_GAMUT_PRESETS['Rec. 2020']
        assert bt2020['R'] == (0.708, 0.292)
        assert bt2020['G'] == (0.170, 0.797)
        assert bt2020['B'] == (0.131, 0.046)
        # Presets with identical primaries keep distinct presentation.
        assert (CIE_GAMUT_PRESETS['sRGB']['color']
                != CIE_GAMUT_PRESETS['Rec. 709']['color'])
        assert (CIE_GAMUT_PRESETS['DCI P3']['style']
                != CIE_GAMUT_PRESETS['Rec. 2020']['style'])


class TestViewWindow:
    def test_default_window_matches_plot_range(self, qapp_mod):
        w = _make_widget(qapp_mod)
        assert w._view_window() == (0.0, 0.8, 0.0, 0.9)
        assert w._blit_src_rect(384, 432) == (0, 0, 384, 432)

    def test_xy_px_roundtrip(self, qapp_mod):
        w = _make_widget(qapp_mod)
        w.resize(640, 480)
        for x, y in ((0.0, 0.0), (0.64, 0.33), (0.8, 0.9), (1 / 3, 1 / 3)):
            px = w._xy_to_px(x, y)
            rx, ry = w._px_to_xy(px)
            assert math.isclose(rx, x, abs_tol=1e-9)
            assert math.isclose(ry, y, abs_tol=1e-9)

    def test_y_axis_points_up(self, qapp_mod):
        w = _make_widget(qapp_mod)
        w.resize(500, 500)
        low = w._xy_to_px(0.4, 0.1)
        high = w._xy_to_px(0.4, 0.8)
        assert high.y() < low.y()  # screen y grows downward


class TestLocusSpline:
    def test_bezier_segments_pass_through_table_points(self):
        from src.shared.ui.widgets.cie_scope_widget import (
            CIE_SPECTRAL_LOCUS,
            catmull_rom_to_bezier,
            cubic_bezier_point,
        )
        xy = [(x, y) for _, x, y in CIE_SPECTRAL_LOCUS]
        segments = catmull_rom_to_bezier(xy)
        assert len(segments) == len(xy) - 1
        for i, (p0, _c1, _c2, p3) in enumerate(segments):
            assert p0 == xy[i]
            assert p3 == xy[i + 1]
        assert catmull_rom_to_bezier([(0.3, 0.3)]) == []

    def test_bezier_endpoints_and_midpoint(self):
        from src.shared.ui.widgets.cie_scope_widget import (
            catmull_rom_to_bezier, cubic_bezier_point)
        p0, c1, c2, p3 = catmull_rom_to_bezier(
            [(0.0, 0.0), (1.0, 1.0), (2.0, 0.0)])[0]
        assert cubic_bezier_point(p0, c1, c2, p3, 0.0) == p0
        assert cubic_bezier_point(p0, c1, c2, p3, 1.0) == p3
        mx, my = cubic_bezier_point(p0, c1, c2, p3, 0.5)
        # 曲线中点应偏离直弦中点 (0.5, 0.5) —— 邻点切向把弧撑弯
        assert abs(mx - 0.5) > 0.01 or abs(my - 0.5) > 0.01

    def test_spline_reduces_faceting_vs_polyline(self):
        """贝塞尔采样轨迹应偏离 10nm 折线弦(曲率被补上)。"""
        from src.shared.ui.widgets.cie_scope_widget import (
            CIE_SPECTRAL_LOCUS,
            catmull_rom_to_bezier,
            cubic_bezier_point,
        )
        xy = [(x, y) for _, x, y in CIE_SPECTRAL_LOCUS]
        segments = catmull_rom_to_bezier(xy)
        max_dev = 0.0
        for i, (p0, c1, c2, p3) in enumerate(segments):
            for t in (0.25, 0.5, 0.75):
                bx, by = cubic_bezier_point(p0, c1, c2, p3, t)
                lx = p0[0] + (p3[0] - p0[0]) * t
                ly = p0[1] + (p3[1] - p0[1]) * t
                max_dev = max(max_dev, math.hypot(bx - lx, by - ly))
        # 10nm 间隔的绿色弯折区应有明显弧线偏离(>0.005 xy)
        assert max_dev > 0.005


class TestOverlayOptions:
    def test_gamut_toggle_and_defaults(self, qapp_mod):
        w = _make_widget(qapp_mod)
        assert w.show_gamuts == ['sRGB']
        assert w.show_locus and w.show_grid and w.show_labels
        assert w.show_ee_point and not w.show_gamut_filled
        assert w.color_temp_curve == 'daylight'
        w.toggle_gamut('DCI P3', True)
        w.toggle_gamut('DCI P3', True)  # idempotent
        assert w.show_gamuts == ['sRGB', 'DCI P3']
        w.toggle_gamut('sRGB', False)
        assert w.show_gamuts == ['DCI P3']

    def test_curve_option_validation(self, qapp_mod):
        w = _make_widget(qapp_mod)
        w.set_color_temp_curve('blackbody')
        assert w.color_temp_curve == 'blackbody'
        w.set_color_temp_curve(None)
        assert w.color_temp_curve is None
        w.set_color_temp_curve('bogus')
        assert w.color_temp_curve is None
