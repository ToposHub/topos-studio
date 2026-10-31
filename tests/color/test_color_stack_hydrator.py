"""Tests for ColorStackHydrator — bidirectional sync between ColorStackData and UI/engine."""

from __future__ import annotations

import pytest
from unittest.mock import MagicMock, create_autospec, patch
from types import SimpleNamespace

from src.shared.project.color_stack import ColorStackData
from src.features.color.adapters.color_stack_hydrator import ColorStackHydrator


# ================================================================
# Fixtures — mock panels
# ================================================================

def _make_mock_grading_panel() -> MagicMock:
    panel = MagicMock()
    panel.current_stack_id = None
    panel.get_current_params.return_value = {
        "lift": (10, 20, 30),
        "gamma": (0, 0, 0),
        "gain": (5, 5, 5),
        "exposure": 1.5,
        "temp": 0,
        "tint": 0,
        "contrast": 0,
        "shadow": 0,
        "saturation": 50,
        "vibrance": 0,
        "mid_detail": 0,
        "hue_shift": 0,
    }
    return panel


def _make_mock_curves_panel() -> MagicMock:
    panel = MagicMock()
    panel.get_curves_data.return_value = {
        "M": [(0, 255), (128, 100), (255, 0)],
        "R": [(0, 255), (255, 0)],
        "G": [(0, 255), (255, 0)],
        "B": [(0, 255), (255, 0)],
    }
    return panel


def _make_mock_hsl_panel() -> MagicMock:
    panel = MagicMock()
    panel.get_curves_data.return_value = {
        "lum_vs_sat": [(0, 0.5), (1, 0.5)],
    }
    return panel


def _make_mock_mixer_panel() -> MagicMock:
    panel = MagicMock()
    panel.params = {
        "red": [0, 0, 0],
        "green": [10, -5, 3],
    }
    return panel


def _make_mock_soft_clip_panel() -> MagicMock:
    panel = MagicMock()
    panel.enable_check = MagicMock()
    panel.enable_check.isChecked.return_value = True
    panel.exposure_control = {"spin": MagicMock()}
    panel.exposure_control["spin"].value.return_value = 0.5
    panel.contrast_control = {"spin": MagicMock()}
    panel.contrast_control["spin"].value.return_value = 0.0
    panel.high_control = {"spin": MagicMock()}
    panel.high_control["spin"].value.return_value = 1.2
    panel.white_point_control = {"spin": MagicMock()}
    panel.white_point_control["spin"].value.return_value = 1.1
    panel.shoulder_strength_control = {"spin": MagicMock()}
    panel.shoulder_strength_control["spin"].value.return_value = 1.0
    panel.low_control = {"spin": MagicMock()}
    panel.low_control["spin"].value.return_value = 0.0
    panel.black_point_control = {"spin": MagicMock()}
    panel.black_point_control["spin"].value.return_value = -0.1
    panel.toe_strength_control = {"spin": MagicMock()}
    panel.toe_strength_control["spin"].value.return_value = 1.0
    return panel


def _make_mock_lut_panel() -> MagicMock:
    return MagicMock()


def _make_all_panels() -> dict:
    return {
        "grading_panel": _make_mock_grading_panel(),
        "curves_panel": _make_mock_curves_panel(),
        "hsl_panel": _make_mock_hsl_panel(),
        "mixer_panel": _make_mock_mixer_panel(),
        "soft_clip_panel": _make_mock_soft_clip_panel(),
        "lut_panel": _make_mock_lut_panel(),
    }


def _make_mock_engine() -> MagicMock:
    engine = MagicMock()
    layer = MagicMock()
    layer.grading_params = {}
    layer.curves_data = {}
    layer.hsl_curve_data = {}
    layer.mixer_params = {}
    layer.soft_clip_params = {}
    layer.active_lut_path = None
    layer.lut_intensity = 1.0
    engine.layers = [layer]
    return engine


def _make_sample_stack() -> ColorStackData:
    return ColorStackData(
        stack_id="test-stack-001",
        name="Test",
        grading_params={
            "lift": (1, 2, 3),
            "gamma": (4, 5, 6),
            "gain": (7, 8, 9),
            "exposure": 0.5,
            "temp": 10,
            "tint": -5,
            "contrast": 20,
            "shadow": -10,
            "saturation": 30,
            "vibrance": 15,
            "mid_detail": 5,
            "hue_shift": 0,
        },
        curves_data={"M": [(0, 255), (64, 192), (255, 0)]},
        hsl_curve_data={"hue_vs_sat": [(0, 0.5), (1, 0.5)]},
        mixer_params={"red": [5, 0, 0], "blue": [0, -3, 2]},
        soft_clip_params={
            "enabled": True,
            "exposure": 0.3,
            "contrast": 0.0,
            "high": 1.2,
            "white_point": 1.1,
            "shoulder_strength": 1.0,
            "low": 0.0,
            "black_point": -0.1,
            "toe_strength": 1.0,
        },
        lut_path="/path/to/lut.cube",
        lut_intensity=0.8,
    )


# ================================================================
# Tests — apply_stack_to_engine
# ================================================================

class TestApplyStackToEngine:

    def test_writes_all_params_to_layers(self) -> None:
        hydrator = ColorStackHydrator()
        stack = _make_sample_stack()
        engine = _make_mock_engine()

        hydrator.apply_stack_to_engine(stack, engine)

        layer = engine.layers[0]
        assert layer.grading_params["exposure"] == 0.5
        assert layer.curves_data["M"] == [(0, 255), (64, 192), (255, 0)]
        assert layer.hsl_curve_data["hue_vs_sat"] == [(0, 0.5), (1, 0.5)]
        assert layer.mixer_params["red"] == [5, 0, 0]
        assert layer.soft_clip_params["enabled"] is True
        assert layer.active_lut_path == "/path/to/lut.cube"
        assert layer.lut_intensity == 0.8

    def test_handles_empty_curves_data(self) -> None:
        hydrator = ColorStackHydrator()
        stack = ColorStackData(stack_id="s1")
        engine = _make_mock_engine()

        hydrator.apply_stack_to_engine(stack, engine)

        layer = engine.layers[0]
        assert layer.curves_data == {}
        assert layer.hsl_curve_data == {}

    def test_handles_no_engine(self) -> None:
        hydrator = ColorStackHydrator()
        stack = _make_sample_stack()
        # Should not raise
        hydrator.apply_stack_to_engine(stack, None)

    def test_handles_no_layers(self) -> None:
        hydrator = ColorStackHydrator()
        stack = _make_sample_stack()
        engine = MagicMock()
        engine.layers = None
        # Should not raise
        hydrator.apply_stack_to_engine(stack, engine)

    def test_handles_legacy_edit_stack_without_tracking_attribute(self) -> None:
        """进入 Color 页时，旧的 edit core 栈不能因跟踪字段缺失而崩溃。"""
        hydrator = ColorStackHydrator()
        stack = SimpleNamespace(
            stack_id="legacy-stack",
            grading_params={},
            curves_data={},
            hsl_curve_data={},
            mixer_params={},
            soft_clip_params={},
            lut_path=None,
            lut_intensity=1.0,
        )
        engine = _make_mock_engine()

        # 运行时 engine 有跟踪存储，但旧对象没有 trackings 属性；应按空存储恢复。
        from src.features.color.tracking.tracking_store import TrackingStore
        engine.tracking_store = TrackingStore()
        hydrator.apply_stack_to_engine(stack, engine)
        assert engine.tracking_store.is_empty()


# ================================================================
# Tests — apply_stack_to_panels
# ================================================================

class TestApplyStackToPanels:

    def test_updates_grading_panel(self) -> None:
        hydrator = ColorStackHydrator()
        stack = _make_sample_stack()
        panels = _make_all_panels()

        hydrator.apply_stack_to_panels(stack, panels)

        gp = panels["grading_panel"]
        gp.set_params.assert_called_once()
        assert gp.current_stack_id == "test-stack-001"
        # Verify specific values in the call
        args = gp.set_params.call_args[0]
        assert args[0] == (1, 2, 3)  # lift
        assert args[3] == 0.5        # exposure

    def test_updates_curves_panel(self) -> None:
        hydrator = ColorStackHydrator()
        stack = _make_sample_stack()
        panels = _make_all_panels()

        hydrator.apply_stack_to_panels(stack, panels)

        panels["curves_panel"].set_curves_data.assert_called_once_with(
            stack.curves_data, emit_changed=False
        )

    def test_updates_hsl_panel(self) -> None:
        hydrator = ColorStackHydrator()
        stack = _make_sample_stack()
        panels = _make_all_panels()

        hydrator.apply_stack_to_panels(stack, panels)

        panels["hsl_panel"].set_curves_data.assert_called_once_with(stack.hsl_curve_data)

    def test_updates_mixer_panel(self) -> None:
        hydrator = ColorStackHydrator()
        stack = _make_sample_stack()
        panels = _make_all_panels()

        hydrator.apply_stack_to_panels(stack, panels)

        panels["mixer_panel"].set_params.assert_called_once_with(stack.mixer_params)

    def test_updates_soft_clip_panel(self) -> None:
        hydrator = ColorStackHydrator()
        stack = _make_sample_stack()
        panels = _make_all_panels()

        hydrator.apply_stack_to_panels(stack, panels)

        panels["soft_clip_panel"].set_params.assert_called_once_with(
            stack.soft_clip_params, emit=False
        )

    def test_updates_lut_panel(self) -> None:
        hydrator = ColorStackHydrator()
        stack = _make_sample_stack()
        panels = _make_all_panels()

        hydrator.apply_stack_to_panels(stack, panels)

        panels["lut_panel"].set_selected_lut.assert_called_once_with("/path/to/lut.cube")
        panels["lut_panel"].set_intensity.assert_called_once_with(0.8)

    def test_skips_missing_panels_gracefully(self) -> None:
        hydrator = ColorStackHydrator()
        stack = _make_sample_stack()
        # Empty panels dict — should not raise
        hydrator.apply_stack_to_panels(stack, {})

    def test_handles_none_panel_gracefully(self) -> None:
        hydrator = ColorStackHydrator()
        stack = _make_sample_stack()
        panels = {"grading_panel": None, "curves_panel": None}
        hydrator.apply_stack_to_panels(stack, panels)

    def test_resets_curves_when_empty(self) -> None:
        hydrator = ColorStackHydrator()
        stack = ColorStackData(stack_id="s1")
        panels = _make_all_panels()

        hydrator.apply_stack_to_panels(stack, panels)

        panels["curves_panel"].reset.assert_called_once_with(emit_changed=False)

    def test_resets_hsl_when_empty(self) -> None:
        hydrator = ColorStackHydrator()
        stack = ColorStackData(stack_id="s1")
        panels = _make_all_panels()

        hydrator.apply_stack_to_panels(stack, panels)

        panels["hsl_panel"].reset.assert_called_once_with(emit_changed=False)


# ================================================================
# Tests — collect_stack_from_engine_and_panels
# ================================================================

class TestCollectStack:

    def test_collects_all_params(self) -> None:
        hydrator = ColorStackHydrator()
        panels = _make_all_panels()
        engine = _make_mock_engine()
        engine.layers[0].active_lut_path = "/saved/lut.cube"
        engine.layers[0].lut_intensity = 0.7

        result = hydrator.collect_stack_from_engine_and_panels("stack-42", engine, panels)

        assert result.stack_id == "stack-42"
        assert result.grading_params["exposure"] == 1.5
        assert "M" in result.curves_data
        assert "lum_vs_sat" in result.hsl_curve_data
        assert result.mixer_params["green"] == [10, -5, 3]
        assert result.soft_clip_params["enabled"] is True
        assert result.soft_clip_params["exposure"] == 0.5
        assert result.lut_path == "/saved/lut.cube"
        assert result.lut_intensity == 0.7

    def test_handles_empty_panels(self) -> None:
        hydrator = ColorStackHydrator()
        engine = _make_mock_engine()

        result = hydrator.collect_stack_from_engine_and_panels("s1", engine, {})

        assert result.stack_id == "s1"
        assert result.grading_params == {}

    def test_handles_missing_engine(self) -> None:
        hydrator = ColorStackHydrator()
        panels = _make_all_panels()

        result = hydrator.collect_stack_from_engine_and_panels("s1", None, panels)

        assert result.lut_path is None
        assert result.lut_intensity == 1.0


# ================================================================
# Tests — hydrate (combined)
# ================================================================

class TestHydrate:

    def test_hydrate_applies_to_both(self) -> None:
        hydrator = ColorStackHydrator()
        stack = _make_sample_stack()
        engine = _make_mock_engine()
        panels = _make_all_panels()

        hydrator.hydrate(stack, engine, panels)

        # Engine should have all params
        layer = engine.layers[0]
        assert layer.grading_params["exposure"] == 0.5
        assert layer.curves_data["M"] is not None

        # GradingPanel should have been updated
        gp = panels["grading_panel"]
        gp.set_params.assert_called_once()
        assert gp.current_stack_id == "test-stack-001"

        # Curves panel should have been updated
        panels["curves_panel"].set_curves_data.assert_called_once()

        # LUT panel should have been updated
        panels["lut_panel"].set_selected_lut.assert_called_once()

        # Render should have been triggered
        engine.render_pipeline.assert_called_once()

    def test_hydrate_with_partial_panels(self) -> None:
        hydrator = ColorStackHydrator()
        stack = _make_sample_stack()
        engine = _make_mock_engine()
        # Only grading panel — should not crash
        panels = {"grading_panel": _make_mock_grading_panel()}

        hydrator.hydrate(stack, engine, panels)

        engine.render_pipeline.assert_called_once()


# ================================================================
# Tests — roundtrip: collect -> apply -> collect
# ================================================================

class TestRoundtrip:

    def test_roundtrip_preserves_grading_params(self) -> None:
        hydrator = ColorStackHydrator()
        original = _make_sample_stack()
        engine = _make_mock_engine()
        panels = _make_all_panels()

        # Apply original to engine + panels
        hydrator.hydrate(original, engine, panels)

        # Now configure panels to return the applied data when collected
        panels["grading_panel"].get_current_params.return_value = dict(original.grading_params)
        panels["curves_panel"].get_curves_data.return_value = dict(original.curves_data)
        panels["hsl_panel"].get_curves_data.return_value = dict(original.hsl_curve_data)

        # Collect
        collected = hydrator.collect_stack_from_engine_and_panels(
            original.stack_id, engine, panels
        )

        assert collected.grading_params["lift"] == (1, 2, 3)
        assert collected.grading_params["exposure"] == 0.5
        assert collected.stack_id == original.stack_id
