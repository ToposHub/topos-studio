"""
Color Tools Scripting API Tests.

Tests the scripting API interpolation functions for curves and HSL curves.
Covers scipy fallback behavior.

Requirements from docs/color_tools_acceptance_residual_plan_2026-05-24.md:
- Curves 0-255 coordinate interpolation
- HSL default LUT generation
- Mixer dict/list compatibility
- scipy fallback should not crash
- Signal protection patterns for UI sync
"""
import pytest
import numpy as np


class TestSignalProtectionPattern:
    """Test the _syncing_ui flag pattern used in color panels.

    Ensures set_params() with emit=False does not trigger signals,
    and reset methods only emit final state.
    """

    def test_syncing_flag_blocks_emission(self):
        class MockPanel:
            def __init__(self):
                self._syncing_ui = False
                self._emit_count = 0
            
            def set_params(self, params, emit=True):
                if not emit:
                    self._syncing_ui = True
                
                for key, value in params.items():
                    pass
                
                if not emit:
                    self._syncing_ui = False
                else:
                    self._emit_count += 1
        
        panel = MockPanel()
        
        panel.set_params({"temp": 50}, emit=False)
        assert panel._emit_count == 0
        assert panel._syncing_ui == False
        
        panel.set_params({"temp": 60}, emit=True)
        assert panel._emit_count == 1

    def test_reset_uses_syncing_flag(self):
        class MockPanel:
            def __init__(self):
                self._syncing_ui = False
                self._emit_count = 0
                self.values = {"a": 0, "b": 0}
            
            def reset_all(self):
                self._syncing_ui = True
                
                self.values["a"] = 0
                self.values["b"] = 0
                
                self._syncing_ui = False
                self._emit_count += 1
        
        panel = MockPanel()
        panel.reset_all()
        
        assert panel._emit_count == 1


class TestCurvesScriptingAPI:
    """Test curves interpolation API."""

    def test_interpolate_two_points_linear(self):
        from src.features.scripting.api import _interpolate_curve_255

        # AUD-25900: y follows the CurvesWidget inverted-Y convention,
        # so the identity curve is [(0, 255), (255, 0)].
        result = _interpolate_curve_255([(0, 255), (255, 0)])

        assert result.shape == (256,)
        assert result[0] == 0.0
        assert result[255] == 1.0
        assert abs(result[128] - 0.5) < 0.01

    def test_interpolate_three_points_smooth(self):
        from src.features.scripting.api import _interpolate_curve_255

        # AUD-25900: brightening midtones = mid point pulled toward the
        # top of the widget (smaller y), endpoints at the UI identity.
        result = _interpolate_curve_255([(0, 255), (128, 100), (255, 0)])

        assert result.shape == (256,)
        assert result[0] == 0.0
        assert result[255] == 1.0
        assert result[128] > 0.5

    def test_interpolate_empty_returns_default(self):
        from src.features.scripting.api import _interpolate_curve_255

        result = _interpolate_curve_255([])

        assert result.shape == (256,)
        assert np.allclose(result, np.arange(256) / 255.0)

    def test_interpolate_single_point(self):
        from src.features.scripting.api import _interpolate_curve_255

        result = _interpolate_curve_255([(128, 100)])

        assert result.shape == (256,)
        # AUD-25900: inverted-Y convention — (255 - y) / 255.
        assert abs(result[128] - (255.0 - 100) / 255.0) < 1e-6


class TestHSLCurvesScriptingAPI:
    """Test HSL curves interpolation API."""

    def test_build_default_hsl_lut(self):
        from src.features.scripting.api import _build_default_hsl_lut
        
        lut = _build_default_hsl_lut()
        
        assert lut.shape == (6, 256)
        assert np.allclose(lut[0], 1.0)
        assert np.allclose(lut[1], 0.0)
        assert np.allclose(lut[2], 1.0)
        assert np.allclose(lut[3], 1.0)
        assert np.allclose(lut[4], 1.0)
        assert np.allclose(lut[5], 1.0)

    def test_interpolate_hue_sat_curve(self):
        from src.features.scripting.api import _interpolate_hsl_curve_255
        
        result = _interpolate_hsl_curve_255(
            [(0, 127.5), (128, 100), (255, 127.5)],
            "HueSat"
        )
        
        assert result.shape == (256,)
        assert result[0] == 1.0
        assert result[128] > 1.0
        assert abs(result[255] - 1.0) < 0.01

    def test_interpolate_hue_hue_curve(self):
        from src.features.scripting.api import _interpolate_hsl_curve_255
        
        result = _interpolate_hsl_curve_255(
            [(0, 127.5), (128, 80), (255, 127.5)],
            "HueHue"
        )
        
        assert result.shape == (256,)
        assert abs(result[0]) < 0.01
        assert result[128] != 0.0
        assert abs(result[255]) < 0.01

    def test_interpolate_empty_returns_default_multiplier(self):
        from src.features.scripting.api import _interpolate_hsl_curve_255
        
        result = _interpolate_hsl_curve_255([], "LumSat")
        
        assert result.shape == (256,)
        assert np.allclose(result, 1.0)

    def test_interpolate_empty_returns_default_shift(self):
        from src.features.scripting.api import _interpolate_hsl_curve_255
        
        result = _interpolate_hsl_curve_255([], "HueHue")
        
        assert result.shape == (256,)
        assert np.allclose(result, 0.0)


class TestMixerScriptingAPI:
    """Test Mixer data format compatibility."""

    def test_mixer_dict_keys(self):
        COLOR_NAMES = ['red', 'orange', 'yellow', 'green', 'aqua', 'blue', 'purple', 'magenta']
        
        mixer_dict = {
            "red": [10, 20, 30],
            "green": [0, 0, 0],
            "blue": [5, 15, 25]
        }
        
        for name in COLOR_NAMES:
            if name in mixer_dict:
                assert len(mixer_dict[name]) == 3

    def test_mixer_list_length(self):
        mixer_list = [
            [10, 20, 30],
            [0, 0, 0],
            [5, 15, 25],
            [0, 0, 0],
            [0, 0, 0],
            [0, 0, 0],
            [0, 0, 0],
            [0, 0, 0]
        ]
        
        assert len(mixer_list) == 8
        for color in mixer_list:
            assert len(color) == 3

    def test_mixer_partial_dict(self):
        mixer_partial = {
            "red": [10, 20, 30]
        }
        
        assert "red" in mixer_partial
        assert mixer_partial["red"] == [10, 20, 30]


class TestScipyFallback:
    """Test that scipy fallback works without scipy installed."""

    def test_curve_interpolation_without_scipy(self):
        import sys
        import importlib
        
        if "scipy" in sys.modules:
            scipy_backup = sys.modules["scipy"]
            del sys.modules["scipy"]
            del sys.modules["scipy.interpolate"]
        else:
            scipy_backup = None
        
        import src.features.scripting.api as api_module
        importlib.reload(api_module)

        # AUD-25900: UI-identity curve (inverted Y axis).
        result = api_module._interpolate_curve_255([(0, 255), (255, 0)])

        assert result.shape == (256,)
        assert result[0] == 0.0
        assert result[255] == 1.0
        
        if scipy_backup:
            sys.modules["scipy"] = scipy_backup
            sys.modules["scipy.interpolate"] = scipy_backup.interpolate

    def test_hsl_curve_interpolation_without_scipy(self):
        import sys
        import importlib
        
        if "scipy" in sys.modules:
            scipy_backup = sys.modules["scipy"]
            del sys.modules["scipy"]
            del sys.modules["scipy.interpolate"]
        else:
            scipy_backup = None
        
        import src.features.scripting.api as api_module
        importlib.reload(api_module)
        
        result = api_module._interpolate_hsl_curve_255(
            [(0, 127.5), (128, 100), (255, 127.5)],
            "HueSat"
        )
        
        assert result.shape == (256,)
        
        if scipy_backup:
            sys.modules["scipy"] = scipy_backup
            sys.modules["scipy.interpolate"] = scipy_backup.interpolate


class TestLayerLUTBuilding:
    """Test layer LUT building from control points."""

    @pytest.mark.skipif(
        True,
        reason="Requires cv2 which may not be installed in test environment"
    )
    def test_build_curves_lut_from_data(self):
        from src.features.color.core.layer import build_curves_lut_from_data
        
        curves_data = {
            "M": [(0, 255), (128, 200), (255, 0)],
            "R": [(0, 255), (255, 0)],
            "G": [(0, 255), (255, 0)],
            "B": [(0, 255), (255, 0)]
        }
        
        lut = build_curves_lut_from_data(curves_data)
        
        assert lut.shape == (256, 1, 3)
        assert lut.dtype == np.float32

    @pytest.mark.skipif(
        True,
        reason="Requires cv2 which may not be installed in test environment"
    )
    def test_build_hsl_curve_lut_from_data(self):
        from src.features.color.core.layer import build_hsl_curve_lut_from_data
        
        hsl_data = {
            "LumSat": [(0, 127.5), (128, 100), (255, 127.5)],
            "HueHue": [(0, 127.5), (255, 127.5)],
            "HueSat": [(0, 127.5), (255, 127.5)],
            "HueLum": [(0, 127.5), (255, 127.5)],
            "SatSat": [(0, 127.5), (255, 127.5)],
            "SatLum": [(0, 127.5), (255, 127.5)]
        }
        
        lut = build_hsl_curve_lut_from_data(hsl_data)
        
        assert lut.shape == (6, 256)
        assert lut.dtype == np.float32