"""Primary highlights must share preview, keyframe and stack state paths."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np
import pytest
from PyQt6.QtCore import QPointF, Qt
from PyQt6.QtGui import QMouseEvent
from src.app.controllers.edit_controller import EditControllerMixin
from src.app.controllers.layer_controller import LayerOpsMixin
from src.features.color.adapters.color_stack_hydrator import ColorStackHydrator
from src.features.color.core.composition_uniforms import composition_uniform_values
from src.features.color.core.the_decompiler import GradingMath
from src.features.color.ui.color_wheel import GradingPanel
from src.shared.project.color_stack import ColorStackData


_NEUTRAL_ARGS = ((0.0, 0.0, 0.0),) * 3 + (0.0,) * 9


class _Host(LayerOpsMixin, EditControllerMixin):
    def __init__(self, engine: object, panel: GradingPanel) -> None:
        self.engine = engine
        self.grading_panel = panel
        self.request_render = MagicMock()
        self._invalidate_video_render_params_if_playing = MagicMock()
        self._refresh_grade_keyframe_ui = MagicMock()
        self._on_grading_changed_for_color_stack = MagicMock()

    def _is_video_playing(self) -> bool:
        return False


def test_highlights_reach_preview_and_save_once_at_gesture_end(qapp, image_engine) -> None:
    panel = GradingPanel()
    image_engine.img_full_original = np.full((2, 2, 3), 0.8, np.float32)
    host = _Host(image_engine, panel)
    panel.current_stack_id = "clip-a"
    panel.grading_changed.connect(host.on_grading_changed)
    host._setup_grading_save_callback("clip-a")
    try:
        panel.set_params(*_NEUTRAL_ARGS, highlights=-40.0, emit=True)

        layer = image_engine.get_active_layer()
        assert layer.grading_params["highlights"] == -40.0
        values = composition_uniform_values(layer, width=2, height=2)
        assert values["u_highlights"] == -40.0
        samples = image_engine.img_full_original.reshape(-1, 3)
        rendered = GradingMath.apply_grading(
            samples, layer.grading_params)
        assert np.all(rendered < samples)
        host.request_render.assert_called_once_with()
        host._on_grading_changed_for_color_stack.assert_not_called()

        panel.interaction_ended.emit()
        saved = host._on_grading_changed_for_color_stack.call_args
        assert saved.args[0] == "clip-a"
        assert saved.args[1]["highlights"] == -40.0
        assert host._on_grading_changed_for_color_stack.call_count == 1
    finally:
        panel.deleteLater()


def test_highlights_survive_serialization_and_clip_switch(qapp, image_engine) -> None:
    panel = GradingPanel()
    hydrator = ColorStackHydrator()
    panels = {"grading_panel": panel}
    try:
        image_engine.grade_image(*_NEUTRAL_ARGS, highlights=37.5)
        stack = hydrator.collect_stack_from_engine_and_panels(
            "clip-a", image_engine, panels)
        restored = ColorStackData.from_dict(stack.to_dict())

        hydrator.apply_stack_to_panels(ColorStackData(stack_id="clip-b"), panels)
        assert panel.get_current_params()["highlights"] == 0.0
        hydrator.apply_stack_to_panels(restored, panels)
        assert panel.current_stack_id == "clip-a"
        assert panel.get_current_params()["highlights"] == 37.5
        assert restored.layers[0]["grading_params"]["highlights"] == 37.5
    finally:
        panel.deleteLater()


def test_legacy_grade_call_preserves_highlights_and_explicit_zero_resets(image_engine) -> None:
    image_engine.grade_image(*_NEUTRAL_ARGS, highlights=28.0)
    image_engine.grade_image(*_NEUTRAL_ARGS)
    assert image_engine.get_active_layer().grading_params["highlights"] == 28.0
    image_engine.grade_image(*_NEUTRAL_ARGS, highlights=0.0)
    assert image_engine.get_active_layer().grading_params["highlights"] == 0.0


def test_highlights_are_recorded_as_a_grade_keyframe() -> None:
    layer = SimpleNamespace(grade_keyframes={})
    engine = SimpleNamespace(
        current_grade_frame=24,
        get_active_layer=lambda: layer,
        record_grade_keyframe=MagicMock(return_value=True),
    )
    host = _Host(engine, SimpleNamespace(keyframe_mode=True))
    host._maybe_record_grade_keyframes(_NEUTRAL_ARGS + (-17.0,))
    engine.record_grade_keyframe.assert_any_call(24, "highlights", -17.0)
    assert engine.record_grade_keyframe.call_count == 13
    host._refresh_grade_keyframe_ui.assert_called_once_with()


@pytest.mark.parametrize("mode", ["lift", "gamma", "gain"])
def test_master_dial_gesture_updates_preview_and_saves_once(qapp, image_engine, mode: str) -> None:
    panel = GradingPanel()
    image_engine.img_full_original = np.full((2, 2, 3), 0.4, np.float32)
    host = _Host(image_engine, panel)
    panel.current_stack_id = "clip-a"
    panel.grading_changed.connect(host.on_grading_changed)
    host._setup_grading_save_callback("clip-a")
    wheel = getattr(panel, mode + "_wheel")
    dial = wheel.brightness_slider

    def send(kind: QMouseEvent.Type, x: float) -> None:
        point = QPointF(x, 11.0)
        event = QMouseEvent(kind, point, point, Qt.MouseButton.LeftButton,
                            Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
        handler = {
            QMouseEvent.Type.MouseButtonPress: dial.mousePressEvent,
            QMouseEvent.Type.MouseMove: dial.mouseMoveEvent,
            QMouseEvent.Type.MouseButtonRelease: dial.mouseReleaseEvent,
        }[kind]
        handler(event)

    try:
        send(QMouseEvent.Type.MouseButtonPress, 20.0)
        send(QMouseEvent.Type.MouseMove, 40.0)
        send(QMouseEvent.Type.MouseMove, 60.0)
        host._on_grading_changed_for_color_stack.assert_not_called()
        send(QMouseEvent.Type.MouseButtonRelease, 70.0)
        assert image_engine.get_active_layer().grading_params[mode] == (0.0, 0.0, 0.05)
        host.request_render.assert_called_once_with()
        saved = host._on_grading_changed_for_color_stack.call_args
        assert saved.args[1][mode] == (0.0, 0.0, 0.05)
        assert host._on_grading_changed_for_color_stack.call_count == 1
    finally:
        panel.deleteLater()
