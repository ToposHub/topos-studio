"""Regression tests for Edit-page subtitle transform handles."""

from __future__ import annotations

from dataclasses import replace

import pytest
from PyQt6.QtCore import QPointF

from src.features.composite.ui.viewer_handles import ndc_to_screen_pixels
from src.features.edit.core.timeline_v2 import SubtitleClip
from src.features.edit.ui.subtitle_handle_controller import SubtitleHandleController


pytestmark = pytest.mark.edit_core


class _Viewer:
    """Minimal ProgramViewer surface with a full-frame 16:9 canvas."""

    def __init__(self) -> None:
        self.update_calls: int = 0

    def width(self) -> int:
        return 960

    def height(self) -> int:
        return 540

    def _calculate_display_rect(self) -> tuple[float, float, float, float]:
        return (-1.0, -1.0, 2.0, 2.0)

    def update(self) -> None:
        self.update_calls += 1


def _screen_for_canvas(x: float, y: float) -> QPointF:
    return QPointF(x * 960.0 / 1920.0, y * 540.0 / 1080.0)


def _controller() -> tuple[SubtitleHandleController, _Viewer, SubtitleClip]:
    viewer = _Viewer()
    clip = SubtitleClip(clip_id="subtitle-1", track_id="s1", text="Caption")
    controller = SubtitleHandleController(viewer)
    controller.set_canvas_size(1920, 1080)
    controller.set_clip(clip)
    controller._get_subtitle_bbox = lambda: (400.0, 300.0, 200, 100)  # type: ignore[method-assign]
    return controller, viewer, clip


def test_screen_coordinates_map_to_bbox_local_pixels() -> None:
    controller, _viewer, _clip = _controller()

    assert controller._screen_to_image_px(_screen_for_canvas(400.0, 300.0), 400.0, 300.0) \
        == pytest.approx((0.0, 0.0))
    assert controller._screen_to_image_px(_screen_for_canvas(600.0, 400.0), 400.0, 300.0) \
        == pytest.approx((200.0, 100.0))


def test_move_uses_canvas_pixel_delta_not_text_bbox_scale() -> None:
    controller, viewer, clip = _controller()
    updates: list[dict] = []
    controller.on_drag_updated = lambda _clip_id, delta: updates.append(delta)
    controller._active_handle = "move"
    controller._drag_start_screen = _screen_for_canvas(430.0, 330.0)
    controller._original_params = {
        "translate_x": 0.0, "translate_y": 0.0,
        "scale_x": 1.0, "scale_y": 1.0, "rotation": 0.0,
        "pivot_mode": "center", "pivot_x": 0.0, "pivot_y": 0.0,
    }

    controller.update_drag(_screen_for_canvas(630.0, 430.0), 0)

    assert updates == [{"translate_x": pytest.approx(200.0),
                        "translate_y": pytest.approx(100.0)}]
    assert controller._selected_clip is not clip
    assert controller._selected_clip.translate_x == pytest.approx(200.0)
    assert viewer.update_calls == 1


def test_pivot_drag_converts_new_custom_pivot_back_to_canvas_space() -> None:
    controller, _viewer, _clip = _controller()
    updates: list[dict] = []
    controller.on_drag_updated = lambda _clip_id, delta: updates.append(delta)
    controller._active_handle = "pivot"
    controller._drag_start_screen = _screen_for_canvas(500.0, 350.0)
    controller._original_params = {
        "translate_x": 0.0, "translate_y": 0.0,
        "scale_x": 1.0, "scale_y": 1.0, "rotation": 0.0,
        "pivot_mode": "center", "pivot_x": 0.0, "pivot_y": 0.0,
    }

    controller.update_drag(_screen_for_canvas(520.0, 370.0), 0)

    assert updates == [{"pivot_mode": "custom",
                        "pivot_x": pytest.approx(520.0),
                        "pivot_y": pytest.approx(370.0)}]


def test_center_pivot_hit_prefers_pivot_over_legacy_center_handle() -> None:
    controller, _viewer, _clip = _controller()
    handles = controller._compute_handles()
    assert handles is not None
    x, y = ndc_to_screen_pixels(handles.pivot, 960.0, 540.0)

    assert controller.hit_test(QPointF(x, y)) == "pivot"


def test_custom_pivot_handles_use_resolved_canvas_coordinates() -> None:
    controller, _viewer, clip = _controller()
    controller.set_clip(
        clip,
        style={
            "pivot_mode": "custom", "pivot_x": 550.0, "pivot_y": 340.0,
            "translate_x": 0.0, "translate_y": 0.0,
            "scale_x": 1.0, "scale_y": 1.0, "rotation": 0.0,
        },
    )

    handles = controller._compute_handles()

    assert handles is not None


def test_bbox_measurement_is_cached_across_transform_updates(monkeypatch) -> None:
    controller, _viewer, clip = _controller()
    controller._get_subtitle_bbox = SubtitleHandleController._get_subtitle_bbox.__get__(controller)
    calls: list[SubtitleClip] = []

    def _measure(value: SubtitleClip, *_args, **_kwargs) -> tuple[float, float, float, float]:
        calls.append(value)
        return (400.0, 300.0, 200.0, 100.0)

    import src.features.edit.core.subtitle_renderer as subtitle_renderer
    monkeypatch.setattr(subtitle_renderer, "measure_subtitle_bbox", _measure)

    assert controller._get_subtitle_bbox() == (400.0, 300.0, 200, 100)
    controller.set_clip(replace(clip, translate_x=25.0))
    assert controller._get_subtitle_bbox() == (400.0, 300.0, 200, 100)
    controller.set_clip(replace(clip, text="Changed"))
    assert controller._get_subtitle_bbox() == (400.0, 300.0, 200, 100)

    assert len(calls) == 2
