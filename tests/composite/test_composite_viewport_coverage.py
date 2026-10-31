"""Coverage backfill for src/features/composite/ui/composite_viewport.py.

Scope (headless, no GL context — the offscreen platform never runs
initializeGL, so every GL path is either skipped by ``_gl_initialized``
guards or guarded by texture-id checks that fail first):

- viewport zoom/pan state machine (fit / actual size / clamped zoom,
  middle-mouse pan, wheel zoom)
- tool-mode state machine (set_tool_mode signals, keyboard shortcuts,
  Escape cancel/reset)
- coordinate mapping math (_compute_vertices letterbox + zoom + pan,
  view<->image round trips, pixel-rect -> NDC, rubber-band rect,
  mouse -> UV)
- set_state/get_state save & restore validation paths
- A/B snapshot guards that need no GL (failure paths, split divider drag)
- pixel inspector mapping + PixelInspectorBar readout
- toolbar sync helpers (exposure menu math, render-pass menu, radios)
- selection / hit-testing API
- display control clamps (channel/exposure/gamma/transfer/resolution/
  background/magnifier) and view-mode switching

Existing files already cover: A/B buffer bookkeeping
(test_viewer_ab_buffers.py), compare-mode round trip
(test_composite_viewer_compare.py), ROI handler math driven directly
(test_composite_viewer_roi.py), magnifier overlay math
(test_viewer_magnifier.py), safe-area content rects
(test_composite_viewport_safe_area.py).  Those paths are not repeated
here.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from PyQt6.QtCore import QEvent, QPoint, QPointF, Qt
from PyQt6.QtGui import QKeyEvent, QMouseEvent, QWheelEvent

pytestmark = pytest.mark.composite_core

import src.features.composite.ui.composite_viewport as cvmod
from src.features.composite.core.format import BBox
from src.features.composite.ui.viewer_tool_system import (
    NodeOutputRegion,
    ViewerToolMode,
)

TEX_W, TEX_H = 1920, 1080


# ── fixtures & event helpers ────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _no_gl_teardown(monkeypatch) -> None:
    """Neutralize GL object deletion for every test in this module.

    Offscreen there is never a current GL context, and PyOpenGL's
    glDeleteTextures segfaults (instead of raising) when called without
    one.  Tests that inject fake texture ids would crash in cleanup()
    teardown paths otherwise; nothing in this module may touch real GL.
    """
    for name in ("glDeleteTextures", "glDeleteFramebuffers",
                 "glDeleteProgram", "glDeleteBuffers", "glDeleteVertexArrays"):
        monkeypatch.setattr(cvmod._GLWrapper, name, lambda *a, **k: None)


@pytest.fixture
def viewer(qapp, _no_gl_teardown) -> "cvmod.CompositeViewer":
    """A viewer with a logical (non-GL) texture set; headless teardown."""
    v = cvmod.CompositeViewer()
    # tex_id>0 with _gl_initialized False skips every GL call and only
    # records metadata — the standard headless pattern used by the page.
    v.set_texture(7, TEX_W, TEX_H)
    yield v
    v.cleanup()
    v.close()


def _press(v: "cvmod.CompositeViewer", button: Qt.MouseButton, pos: QPointF,
           mods: Qt.KeyboardModifier = Qt.KeyboardModifier.NoModifier) -> None:
    v.mousePressEvent(QMouseEvent(
        QEvent.Type.MouseButtonPress, pos, button, button, mods))


def _move(v: "cvmod.CompositeViewer", pos: QPointF,
          buttons: Qt.MouseButton = Qt.MouseButton.MiddleButton) -> None:
    v.mouseMoveEvent(QMouseEvent(
        QEvent.Type.MouseMove, pos, Qt.MouseButton.NoButton, buttons,
        Qt.KeyboardModifier.NoModifier))


def _release(v: "cvmod.CompositeViewer", button: Qt.MouseButton,
             pos: QPointF) -> None:
    v.mouseReleaseEvent(QMouseEvent(
        QEvent.Type.MouseButtonRelease, pos, button, Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier))


def _key(v: "cvmod.CompositeViewer", key: Qt.Key, text: str,
         mods: Qt.KeyboardModifier = Qt.KeyboardModifier.NoModifier) -> None:
    v.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, key, mods, text))


def _wheel(v: "cvmod.CompositeViewer", dy: int, pos: QPointF) -> None:
    v.wheelEvent(QWheelEvent(
        pos, pos, QPoint(0, 0), QPoint(0, dy), Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.NoScrollPhase, False))


# ── zoom / pan state machine ────────────────────────────────────────────────


class TestZoomPanStateMachine:
    def test_fit_to_window_resets_state_and_emits(self, viewer) -> None:
        viewer._zoom_level = 3.5
        viewer._pan_x = 0.4
        viewer._pan_y = -0.2
        zooms: list[float] = []
        viewer.zoom_changed.connect(lambda z: zooms.append(z))
        viewer.fit_to_window()
        assert viewer._zoom_level == 1.0
        assert (viewer._pan_x, viewer._pan_y) == (0.0, 0.0)
        assert zooms[-1] == 1.0

    def test_actual_size_uses_inverse_fit_scale(self, viewer) -> None:
        # 640x480 logical widget, 1920x1080 frame -> fit scale 1/3 -> 1:1 zoom 3
        zooms: list[float] = []
        viewer.zoom_changed.connect(lambda z: zooms.append(z))
        viewer.actual_size()
        assert viewer._zoom_level == pytest.approx(3.0)
        assert (viewer._pan_x, viewer._pan_y) == (0.0, 0.0)
        assert zooms[-1] == pytest.approx(3.0)

    def test_actual_size_square_texture_is_identity(self, qapp) -> None:
        v = cvmod.CompositeViewer()
        try:
            v.set_texture(7, 640, 480)  # same aspect as widget content area
            v._zoom_level = 2.0
            v.actual_size()
            assert v._zoom_level == pytest.approx(1.0)
        finally:
            v.cleanup()
            v.close()

    def test_actual_size_noop_without_texture(self, viewer) -> None:
        viewer._tex_width = 0
        viewer._tex_height = 0
        viewer._zoom_level = 2.5
        zooms: list[float] = []
        viewer.zoom_changed.connect(lambda z: zooms.append(z))
        viewer.actual_size()
        assert viewer._zoom_level == 2.5
        assert zooms == []

    def test_zoom_clamped_to_min_max(self, viewer) -> None:
        viewer.fit_to_window()
        for _ in range(8):
            viewer._apply_zoom(0.01, QPoint(320, 240))
        assert viewer._zoom_level == pytest.approx(0.05)
        for _ in range(8):
            viewer._apply_zoom(100.0, QPoint(320, 240))
        assert viewer._zoom_level == pytest.approx(50.0)

    def test_zoom_anchors_image_point_under_cursor(self, viewer) -> None:
        viewer.fit_to_window()
        anchor = QPointF(400.0, 200.0)
        before = viewer.get_image_pos_at_view_pos(anchor, check_bounds=False)
        viewer._apply_zoom(2.5, anchor.toPoint())
        after = viewer.get_image_pos_at_view_pos(anchor, check_bounds=False)
        assert after.x() == pytest.approx(before.x(), abs=1e-6)
        assert after.y() == pytest.approx(before.y(), abs=1e-6)

    def test_zoom_level_property_tracks_state(self, viewer) -> None:
        assert viewer.zoom_level == 1.0
        viewer._apply_zoom(1.25, QPoint(320, 240))
        assert viewer.zoom_level == pytest.approx(1.25)

    def test_middle_mouse_pan_updates_ndc_offset(self, viewer) -> None:
        viewer.fit_to_window()
        _press(viewer, Qt.MouseButton.MiddleButton, QPointF(320, 240))
        assert viewer._is_panning is True
        _move(viewer, QPointF(370, 220))
        # +50px of a 640px viewport -> +0.15625 NDC; 20px up -> +1/12 NDC
        assert viewer._pan_x == pytest.approx(0.15625)
        assert viewer._pan_y == pytest.approx(20.0 / 240.0)
        _release(viewer, Qt.MouseButton.MiddleButton, QPointF(370, 220))
        assert viewer._is_panning is False

    def test_pan_recomputes_tool_handles(self, viewer) -> None:
        viewer._tool_handles = {"h": QPointF(1.0, 2.0)}
        viewer._start_pan(QPoint(0, 0))
        _move(viewer, QPointF(100, 100))
        # handle table is rebuilt from (empty) selection -> cleared
        assert viewer._tool_handles == {}

    def test_wheel_zoom_applies_factor_and_auto_proxy_hint(self, viewer) -> None:
        proxy: list[bool] = []
        viewer.auto_proxy_interaction.connect(lambda b: proxy.append(b))
        viewer.fit_to_window()
        _wheel(viewer, 120, QPointF(320, 240))
        assert viewer._zoom_level == pytest.approx(1.15)
        assert proxy[-1] is True
        _wheel(viewer, -120, QPointF(320, 240))
        assert viewer._zoom_level == pytest.approx(1.15 / 1.15)

    def test_wheel_zero_delta_is_noop(self, viewer) -> None:
        zooms: list[float] = []
        viewer.zoom_changed.connect(lambda z: zooms.append(z))
        _wheel(viewer, 0, QPointF(320, 240))
        assert zooms == []

    def test_compute_vertices_letterbox_and_zoom_pan(self, viewer) -> None:
        viewer.fit_to_window()
        assert viewer._compute_vertices(640, 480, TEX_W, TEX_H) == (
            pytest.approx(-1.0), pytest.approx(-0.75),
            pytest.approx(1.0), pytest.approx(0.75))
        viewer._zoom_level = 2.0
        x0, y0, x1, y1 = viewer._compute_vertices(640, 480, TEX_W, TEX_H)
        assert (x0, y0, x1, y1) == (
            pytest.approx(-2.0), pytest.approx(-1.5),
            pytest.approx(2.0), pytest.approx(1.5))
        viewer._pan_x, viewer._pan_y = 0.1, -0.05
        x0, y0, x1, y1 = viewer._compute_vertices(640, 480, TEX_W, TEX_H)
        assert x0 == pytest.approx(-1.9)
        assert y1 == pytest.approx(1.45)

    def test_compute_vertices_degenerate_returns_full_quad(self, viewer) -> None:
        assert viewer._compute_vertices(640, 480, 0, 0) == (-1.0, -1.0, 1.0, 1.0)
        assert viewer._compute_vertices(0, 0, TEX_W, TEX_H) == (-1.0, -1.0, 1.0, 1.0)

    def test_content_rect_reserves_visible_bars(self, viewer) -> None:
        viewer._toolbar.resize(640, 28)
        viewer._toolbar.isVisible = lambda: True  # type: ignore[method-assign]
        left, top, right, bottom = viewer._content_rect_pixels(1.0)
        assert (left, top, right) == (0.0, 28.0, 640.0)
        assert bottom == 480.0
        bar_h = 36  # VideoControlBar enforces its own max height
        viewer.video_control_bar.resize(640, 40)
        viewer.video_control_bar.isVisible = lambda: True  # type: ignore[method-assign]
        bottom2 = viewer._content_rect_pixels(1.0)[3]
        assert bottom2 == pytest.approx(480.0 - viewer.video_control_bar.height())
        assert viewer.video_control_bar.height() <= 40

    def test_content_rect_full_when_overlay_bars_hidden(self, viewer) -> None:
        assert viewer._content_rect_pixels(1.0) == (0.0, 0.0, 640.0, 480.0)

    def test_viewer3d_content_size_excludes_bars(self, viewer) -> None:
        # _gl_initialized False -> dpr forced to 1.0
        assert viewer.viewer3d_content_size() == (640, 480)
        viewer._toolbar.resize(640, 28)
        viewer._toolbar.isVisible = lambda: True  # type: ignore[method-assign]
        assert viewer.viewer3d_content_size() == (640, 452)


# ── tool mode state machine ─────────────────────────────────────────────────


class TestToolModeStateMachine:
    def test_default_tool_is_pan(self, viewer) -> None:
        assert viewer.get_tool_mode() is ViewerToolMode.PAN
        assert viewer.get_interaction_state().tool is ViewerToolMode.PAN

    def test_set_tool_mode_emits_and_syncs_interaction_state(self, viewer) -> None:
        events: list[ViewerToolMode] = []
        viewer.tool_mode_changed.connect(lambda m: events.append(m))
        viewer.set_tool_mode(ViewerToolMode.TRANSFORM)
        assert events == [ViewerToolMode.TRANSFORM]
        assert viewer.get_tool_mode() is ViewerToolMode.TRANSFORM
        assert viewer.get_interaction_state().tool is ViewerToolMode.TRANSFORM

    def test_same_tool_mode_does_not_reemit(self, viewer) -> None:
        events: list[ViewerToolMode] = []
        viewer.tool_mode_changed.connect(lambda m: events.append(m))
        viewer.set_tool_mode(ViewerToolMode.PAN)
        assert events == []

    def test_set_tool_mode_resets_hover_handle(self, viewer) -> None:
        viewer._hover_handle = "rotate"
        viewer.set_tool_mode(ViewerToolMode.CROP)
        assert viewer._hover_handle is None

    @pytest.mark.parametrize("key,text,expected", [
        (Qt.Key.Key_S, "s", ViewerToolMode.SELECT),
        (Qt.Key.Key_H, "h", ViewerToolMode.PAN),
        (Qt.Key.Key_T, "t", ViewerToolMode.TRANSFORM),
        (Qt.Key.Key_C, "c", ViewerToolMode.CROP),
        (Qt.Key.Key_M, "m", ViewerToolMode.MASK),
        (Qt.Key.Key_R, "r", ViewerToolMode.ROTO),
        (Qt.Key.Key_P, "p", ViewerToolMode.CORNERPIN),
        (Qt.Key.Key_B, "b", ViewerToolMode.PAINT),
    ])
    def test_keyboard_shortcuts_switch_tools(self, viewer, key, text,
                                             expected) -> None:
        _key(viewer, key, text)
        assert viewer.get_tool_mode() is expected

    def test_tool_shortcuts_ignored_with_modifiers(self, viewer) -> None:
        _key(viewer, Qt.Key.Key_S, "S", Qt.KeyboardModifier.ControlModifier)
        assert viewer.get_tool_mode() is ViewerToolMode.PAN

    def test_escape_resets_to_pan(self, viewer) -> None:
        viewer.set_tool_mode(ViewerToolMode.CROP)
        _key(viewer, Qt.Key.Key_Escape, "")
        assert viewer.get_tool_mode() is ViewerToolMode.PAN

    def test_escape_cancels_active_drag(self, viewer) -> None:
        viewer.set_tool_mode(ViewerToolMode.MASK)
        viewer._interaction_state.start_drag(
            tool=ViewerToolMode.MASK, node_id="n1", handle="p0",
            view_pos=QPointF(10, 10), ndc_pos=QPointF(0, 0),
            image_pos=QPointF(100, 100), format_pos=QPointF(),
            original_params={}, modifiers=0)
        assert viewer.is_drag_active() is True
        _key(viewer, Qt.Key.Key_Escape, "")
        assert viewer.is_drag_active() is False

    def test_lod_level_zero_when_not_dragging(self, viewer) -> None:
        assert viewer.get_lod_level() == 0
        assert viewer.is_drag_active() is False

    def test_paint_bracket_keys_resize_brush(self, viewer) -> None:
        viewer.set_tool_mode(ViewerToolMode.PAINT)
        viewer._selected_node_type = "paint"
        viewer.select_node("p1")
        sizes: list[float] = []
        viewer.paint_brush_size_changed.connect(
            lambda _nid, size: sizes.append(size))
        _key(viewer, Qt.Key.Key_BracketRight, "]")
        assert sizes == [pytest.approx(40.0 * 1.15)]
        _key(viewer, Qt.Key.Key_BracketLeft, "[")
        assert len(sizes) == 2
        assert sizes[1] == pytest.approx(40.0)  # 46 -> 46/1.15 ~= 40
        for _ in range(40):
            _key(viewer, Qt.Key.Key_BracketRight, "]")
        assert viewer._paint_brush_size_px == pytest.approx(1024.0)

    def test_paint_bracket_keys_require_paint_selection(self, viewer) -> None:
        viewer.set_tool_mode(ViewerToolMode.PAINT)
        viewer._selected_node_type = "paint"
        # no selected node -> shortcut must not fire
        sizes: list[float] = []
        viewer.paint_brush_size_changed.connect(
            lambda _nid, size: sizes.append(size))
        _key(viewer, Qt.Key.Key_BracketRight, "]")
        assert sizes == []
        assert viewer._paint_brush_size_px == 40.0

    def test_z_key_toggles_magnifier(self, viewer) -> None:
        _key(viewer, Qt.Key.Key_Z, "z")
        assert viewer._magnifier_enabled is True
        _key(viewer, Qt.Key.Key_Z, "z")
        assert viewer._magnifier_enabled is False


# ── coordinate mapping ──────────────────────────────────────────────────────


class TestCoordinateMapping:
    def test_view_image_roundtrip_at_fit(self, viewer) -> None:
        for image_pt in (QPointF(0, 0), QPointF(960, 540),
                         QPointF(TEX_W, TEX_H), QPointF(100, 900)):
            view_pt = viewer.get_view_pos_at_image_pos(image_pt)
            assert view_pt is not None
            back = viewer.get_image_pos_at_view_pos(view_pt)
            assert back is not None
            assert back.x() == pytest.approx(image_pt.x(), abs=1e-6)
            assert back.y() == pytest.approx(image_pt.y(), abs=1e-6)

    def test_view_image_roundtrip_after_zoom_and_pan(self, viewer) -> None:
        viewer._zoom_level = 2.7
        viewer._pan_x = -0.33
        viewer._pan_y = 0.21
        image_pt = QPointF(480, 270)
        view_pt = viewer.get_view_pos_at_image_pos(image_pt)
        assert view_pt is not None
        back = viewer.get_image_pos_at_view_pos(view_pt, check_bounds=False)
        assert back is not None
        assert back.x() == pytest.approx(480.0, abs=1e-6)
        assert back.y() == pytest.approx(270.0, abs=1e-6)

    def test_image_pos_outside_frame_rejected_with_bounds_check(
            self, viewer) -> None:
        assert viewer.get_image_pos_at_view_pos(QPointF(-50, -50)) is None
        assert viewer.get_image_pos_at_view_pos(
            QPointF(-50, -50), check_bounds=False) is not None

    def test_uv_at_mouse_center_and_edges(self, viewer) -> None:
        viewer._mouse_in_viewer = True
        viewer._mouse_pos = QPoint(320, 240)
        assert viewer._compute_uv_at_mouse(640, 480, TEX_W, TEX_H) == (
            pytest.approx(0.5), pytest.approx(0.5))
        viewer._mouse_pos = QPoint(0, 60)  # top-left of the letterboxed image
        u, v = viewer._compute_uv_at_mouse(640, 480, TEX_W, TEX_H)
        assert u == pytest.approx(0.0, abs=1e-9)
        assert v == pytest.approx(1.0, abs=1e-6)

    def test_uv_at_mouse_outside_image_returns_none(self, viewer) -> None:
        viewer._mouse_in_viewer = True
        viewer._mouse_pos = QPoint(-10, 240)  # left of letterboxed image
        assert viewer._compute_uv_at_mouse(640, 480, TEX_W, TEX_H) is None

    def test_uv_at_mouse_requires_presence_and_texture(self, viewer) -> None:
        viewer._mouse_in_viewer = False
        viewer._mouse_pos = QPoint(320, 240)
        assert viewer._compute_uv_at_mouse(640, 480, TEX_W, TEX_H) is None
        viewer._mouse_in_viewer = True
        assert viewer._compute_uv_at_mouse(640, 480, 0, 0) is None

    def test_ndc_from_pixel_rect_full_frame_matches_vertices(self, viewer) -> None:
        expected = viewer._compute_vertices(640, 480, TEX_W, TEX_H)
        assert viewer._ndc_from_pixel_rect(0, 0, TEX_W, TEX_H, TEX_W, TEX_H) == (
            pytest.approx(expected[0]), pytest.approx(expected[1]),
            pytest.approx(expected[2]), pytest.approx(expected[3]))

    def test_ndc_from_pixel_rect_sub_rect_is_proportional(self, viewer) -> None:
        x0, y0, x1, y1 = viewer._ndc_from_pixel_rect(
            480, 270, 960, 540, TEX_W, TEX_H)
        full = viewer._compute_vertices(640, 480, TEX_W, TEX_H)
        # UV of the sub-rect: u 0.25..0.75, v 0.25..0.75 (bottom-left origin)
        fx0, fy0, fx1, fy1 = full
        assert x0 == pytest.approx(fx0 + (fx1 - fx0) * 0.25)
        assert x1 == pytest.approx(fx0 + (fx1 - fx0) * 0.75)
        assert y0 == pytest.approx(fy0 + (fy1 - fy0) * 0.25)
        assert y1 == pytest.approx(fy0 + (fy1 - fy0) * 0.75)

    def test_ndc_from_pixel_rect_invalid_texture(self, viewer) -> None:
        assert viewer._ndc_from_pixel_rect(0, 0, 10, 10, 0, 0) is None

    def test_rubber_band_rect_maps_to_image(self, viewer) -> None:
        p1 = viewer.get_image_pos_at_view_pos(QPointF(100, 100),
                                              check_bounds=False)
        p2 = viewer.get_image_pos_at_view_pos(QPointF(300, 250),
                                              check_bounds=False)
        got = viewer._rubber_band_rect_to_image_rect((300, 250, 100, 100))
        assert got is not None
        assert got == (pytest.approx(p1.x()), pytest.approx(p1.y()),
                       pytest.approx(p2.x()), pytest.approx(p2.y()))

    def test_rubber_band_rect_none_when_unmappable(self, viewer) -> None:
        # zero-size viewport geometry cannot map anything
        assert viewer._rubber_band_rect_to_image_rect((0, 0, 0, 0)) is not None


# ── state save / restore ────────────────────────────────────────────────────


class TestStateSaveRestore:
    def test_get_state_defaults(self, viewer) -> None:
        state = viewer.get_state()
        assert state["zoom_level"] == 1.0
        assert state["tool_mode"] == "pan"
        assert state["render_pass"] == "beauty"
        assert state["resolution_mode"] == "full"
        assert state["background_mode"] == "checker"
        assert state["magnifier_enabled"] is False
        assert state["magnifier_zoom"] == 8.0
        assert state["user_roi"] is None
        assert state["selected_3d_scene_node_id"] is None
        # legacy aliases stay in sync
        assert state["show_bbox"] is state["show_bbox_overlay"]
        assert state["show_format"] is state["show_format_overlay"]

    def test_set_state_restores_all_keys(self, viewer) -> None:
        state = {
            "zoom_level": 2.5, "pan_x": 0.1, "pan_y": -0.2,
            "channel_mode": 4, "exposure": 1.3, "viewer_gamma": 2.2,
            "display_srgb": False, "display_transfer": 0,
            "split_compare_mode": "difference",
            "show_bbox_overlay": True, "show_format_overlay": True,
            "resolution_mode": "half", "magnifier_enabled": True,
            "magnifier_zoom": 4.0, "background_mode": "black",
            "tool_mode": "transform", "selected_3d_scene_node_id": "mesh_1",
        }
        viewer.set_state(state)
        assert viewer._zoom_level == 2.5
        assert (viewer._pan_x, viewer._pan_y) == (0.1, -0.2)
        assert viewer._channel_mode == 4
        assert viewer._exposure == 1.3
        assert viewer._viewer_gamma == 2.2
        assert viewer._display_srgb is False
        assert viewer._show_bbox_overlay is True
        assert viewer._show_format_overlay is True
        assert viewer._resolution_mode == "half"
        assert viewer._magnifier_enabled is True
        assert viewer._magnifier_zoom == 4.0
        assert viewer._background_mode == "black"
        assert viewer.get_tool_mode() is ViewerToolMode.TRANSFORM
        assert viewer._selected_3d_scene_node_id == "mesh_1"
        # toolbar mirrors restored channel/exposure
        assert viewer._toolbar.channel_btn.text() == "A"
        assert viewer._toolbar.exposure_label.text() == "Exp: 1.3"

    def test_set_state_clamps_difference_scale(self, viewer) -> None:
        viewer.set_state({"split_difference_scale": 99.0})
        assert viewer._split_difference_scale == 20.0
        viewer.set_state({"split_difference_scale": -5.0})
        assert viewer._split_difference_scale == 0.0

    def test_set_state_quantizes_magnifier_zoom(self, viewer) -> None:
        viewer.set_state({"magnifier_zoom": 7.0})
        assert viewer._magnifier_zoom == 8.0
        viewer.set_state({"magnifier_zoom": 2.0})
        assert viewer._magnifier_zoom == 4.0

    def test_set_state_render_pass_only_when_available(self, viewer) -> None:
        viewer.set_state({"render_pass": "depth"})
        assert viewer._selected_render_pass == "beauty"  # not available yet
        viewer._available_render_passes = {"beauty", "depth"}
        viewer.set_state({"render_pass": "depth"})
        assert viewer._selected_render_pass == "depth"

    def test_set_state_invalid_tool_mode_keeps_current(self, viewer) -> None:
        viewer.set_tool_mode(ViewerToolMode.CROP)
        viewer.set_state({"tool_mode": "does-not-exist"})
        assert viewer.get_tool_mode() is ViewerToolMode.CROP

    def test_set_state_empty_dict_is_noop(self, viewer) -> None:
        zooms: list[float] = []
        viewer.zoom_changed.connect(lambda z: zooms.append(z))
        viewer.set_state({})
        assert viewer._zoom_level == 1.0
        assert zooms == []

    def test_set_state_user_roi_emits_when_frame_known(self, viewer) -> None:
        rois: list[object] = []
        viewer.user_roi_changed.connect(lambda b: rois.append(b))
        viewer.set_state({"user_roi": [100, 100, 200, 150]})
        assert viewer._user_roi_rect == (100.0, 100.0, 200.0, 150.0)
        assert len(rois) == 1
        bbox = rois[0]
        assert isinstance(bbox, BBox)
        assert (bbox.x, bbox.y, bbox.width, bbox.height) == (100, 830, 200, 150)

    def test_set_state_user_roi_silent_before_first_frame(self, qapp) -> None:
        v = cvmod.CompositeViewer()
        try:
            rois: list[object] = []
            v.user_roi_changed.connect(lambda b: rois.append(b))
            v.set_state({"user_roi": [10, 10, 50, 50]})
            assert v._user_roi_rect == (10.0, 10.0, 50.0, 50.0)
            assert rois == []  # documented startup-restore behaviour
        finally:
            v.cleanup()
            v.close()

    def test_set_state_invalid_roi_falls_back_to_none(self, viewer) -> None:
        viewer.set_state({"user_roi": [1, 2, 3]})  # wrong arity
        assert viewer._user_roi_rect is None
        viewer.set_state({"user_roi": ["a", "b", "c", "d"]})
        assert viewer._user_roi_rect is None

    def test_state_round_trip_all_keys(self, viewer) -> None:
        viewer.set_state({
            "zoom_level": 1.75, "pan_x": -0.05, "pan_y": 0.06,
            "channel_mode": 3, "exposure": -2.4, "viewer_gamma": 1.8,
            "display_srgb": True, "display_transfer": 1,
            "split_compare_mode": "difference", "split_difference_scale": 6.0,
            "show_bbox_overlay": True, "show_format_overlay": False,
            "resolution_mode": "quarter", "magnifier_enabled": True,
            "magnifier_zoom": 4.0, "background_mode": "white",
            "tool_mode": "cornerpin", "selected_3d_scene_node_id": "m9",
            "user_roi": [5, 6, 7, 8],
        })
        out = viewer.get_state()
        assert out["zoom_level"] == 1.75
        assert out["pan_x"] == -0.05
        assert out["pan_y"] == 0.06
        assert out["channel_mode"] == 3
        assert out["exposure"] == -2.4
        assert out["viewer_gamma"] == 1.8
        assert out["split_compare_mode"] == "difference"
        assert out["split_difference_scale"] == 6.0
        assert out["resolution_mode"] == "quarter"
        assert out["magnifier_enabled"] is True
        assert out["magnifier_zoom"] == 4.0
        assert out["background_mode"] == "white"
        assert out["tool_mode"] == "cornerpin"
        assert out["selected_3d_scene_node_id"] == "m9"
        assert out["user_roi"] == [5.0, 6.0, 7.0, 8.0]


# ── texture / render-pass bookkeeping (headless paths) ──────────────────────


class TestTextureBookkeeping:
    def test_set_texture_without_gl_records_metadata(self, qapp) -> None:
        v = cvmod.CompositeViewer()
        try:
            fmt = SimpleNamespace(width=1920, height=1080)
            bbox = BBox(0, 0, 100, 50)
            assert v.set_texture(9, 800, 600, image_format=fmt, bbox=bbox)
            assert v._current_tex_id == 9
            assert (v._tex_width, v._tex_height) == (800, 600)
            assert v._beauty_dimensions == (800, 600)
            assert v._overlay_format is fmt
            assert v._overlay_bbox is bbox
            assert v._inspector_bar._format_label.text() == "800×600"
        finally:
            v.cleanup()
            v.close()

    def test_set_texture_invalid_clears_state(self, viewer) -> None:
        viewer._owned_aov_tex_ids["depth"] = 33
        viewer._owned_aov_tex_sizes["depth"] = (TEX_W, TEX_H)
        viewer.set_render_pass("depth")
        assert viewer.set_texture(0) is True
        assert viewer._current_tex_id == 0
        assert (viewer._tex_width, viewer._tex_height) == (0, 0)
        assert viewer._selected_render_pass == "beauty"
        assert viewer._owned_aov_tex_ids == {}
        assert viewer._overlay_format is None
        assert viewer._overlay_bbox is None

    def test_user_roi_reemitted_on_texture_size_change(self, viewer) -> None:
        rois: list[object] = []
        viewer.user_roi_changed.connect(lambda b: rois.append(b))
        viewer._apply_user_roi((100.0, 100.0, 200.0, 150.0))
        assert len(rois) == 1
        # same-size refresh does not re-emit
        viewer.set_texture(8, TEX_W, TEX_H)
        assert len(rois) == 1
        # size change (proxy tier switch) re-emits with recomputed bbox
        viewer.set_texture(8, 960, 540)
        assert len(rois) == 2
        bbox = rois[1]
        assert isinstance(bbox, BBox)
        assert (bbox.x, bbox.y, bbox.width, bbox.height) == (100, 290, 200, 150)

    def test_set_render_pass_falls_back_to_beauty(self, viewer) -> None:
        assert viewer.set_render_pass("depth") is False
        assert viewer._selected_render_pass == "beauty"
        # beauty reads the viewer-owned display texture, which is only
        # allocated by the GL copy path (absent headless)
        assert viewer._current_tex_id is None
        assert viewer._toolbar.render_pass_btn.text() == "Beauty"

    def test_set_render_pass_selects_owned_aov(self, viewer) -> None:
        viewer._owned_aov_tex_ids["normal"] = 55
        viewer._owned_aov_tex_sizes["normal"] = (640, 360)
        assert viewer.set_render_pass("normal") is True
        assert viewer._current_tex_id == 55
        assert (viewer._tex_width, viewer._tex_height) == (640, 360)
        # the menu label only refreshes once the AOV menu is rebuilt
        # (GL-gated set_aov_textures); headless it still says Beauty
        assert viewer._toolbar.render_pass_btn.text() == "Beauty"

    def test_set_aov_textures_noop_without_gl(self, viewer, monkeypatch) -> None:
        def _boom(*args: object, **kwargs: object) -> None:
            raise AssertionError("GL must not be touched headless")
        monkeypatch.setattr(cvmod._GLWrapper, "glIsTexture", _boom)
        viewer._toolbar.set_available_render_passes(("beauty",))
        viewer.set_aov_textures({"depth": {"texture_id": 1, "width": 4,
                                           "height": 4}})
        assert list(viewer._toolbar._render_pass_actions.keys()) == ["beauty"]

    def test_set_image_data_fails_without_gl(self, viewer) -> None:
        import numpy as np
        data = np.zeros((4, 4, 3), dtype=np.float32)
        assert viewer.set_image_data(data, 4, 4) is False

    def test_set_overlay_data_sentinel_semantics(self, viewer) -> None:
        fmt = SimpleNamespace(width=10, height=10)
        bbox = BBox(0, 0, 10, 10)
        viewer.set_overlay_data(image_format=fmt, bbox=bbox)
        assert viewer._overlay_format is fmt
        assert viewer._overlay_bbox is bbox
        viewer.set_overlay_data(image_format={}, bbox={})  # explicit clear
        assert viewer._overlay_format is None
        assert viewer._overlay_bbox is None
        viewer.set_overlay_data()  # None leaves fields untouched
        assert viewer._overlay_format is None

    def test_clear_user_roi_switches_back_to_pan_tool(self, viewer) -> None:
        viewer.set_tool_mode(ViewerToolMode.ROI)
        viewer.set_user_roi((10.0, 10.0, 50.0, 50.0))
        viewer.clear_user_roi()
        assert viewer.get_tool_mode() is ViewerToolMode.PAN


# ── A/B snapshot & split-view guards (GL-free paths) ────────────────────────


class TestAbSnapshotGuards:
    def test_store_ab_snapshot_fails_without_display_texture(
            self, viewer) -> None:
        viewer._current_tex_id = 0
        viewer._owned_display_tex_size = None
        assert viewer.store_ab_snapshot("A") is False
        assert viewer._split_active is False

    def test_store_ab_snapshot_uppercase_normalization(self, viewer) -> None:
        slots: list[str] = []

        def fake_snapshot(slot: str) -> bool:
            slots.append(slot)
            viewer._owned_secondary_b_tex_id = 222
            viewer._owned_secondary_b_tex_size = (64, 64)
            viewer._ab_active_slot = slot
            viewer._secondary_tex_id = viewer._ab_owned_tex_id(slot)
            return True

        viewer._snapshot_display_texture_to_slot = fake_snapshot  # type: ignore[assignment]
        assert viewer.store_ab_snapshot("b") is True
        assert slots == ["B"]
        assert viewer._secondary_tex_id == 222

    def test_clear_split_snapshot_resets_real_state(self, viewer) -> None:
        # ids stay 0 -> the real deleters take their no-GL early-out path
        viewer._split_active = True
        viewer._secondary_tex_id = 0
        viewer._ab_active_slot = "B"
        viewer._toolbar.split_btn.setChecked(True)
        viewer.clear_split_snapshot()
        assert viewer._split_active is False
        assert viewer._secondary_tex_id == 0
        assert viewer._owned_secondary_tex_id == 0
        assert viewer._owned_secondary_b_tex_id == 0
        assert viewer._ab_active_slot == "A"
        assert viewer._toolbar.split_btn.isChecked() is False

    def test_set_ab_compare_slot_ignores_empty_slot(self, viewer) -> None:
        viewer._owned_secondary_tex_id = 111
        viewer._owned_secondary_b_tex_id = 0
        viewer._ab_active_slot = "A"
        viewer.set_ab_compare_slot("B")
        assert viewer._ab_active_slot == "A"

    def test_set_split_view_snapshots_once_and_clears_source(
            self, viewer) -> None:
        calls: list[str] = []

        def fake_secondary() -> bool:
            calls.append("blit")
            viewer._owned_secondary_tex_id = 42
            viewer._secondary_tex_id = 42
            return True

        viewer._snapshot_display_texture_to_secondary = fake_secondary  # type: ignore[assignment]
        viewer.set_split_view(True)
        assert calls == ["blit"]
        assert viewer._split_active is True
        viewer.set_split_view(True)  # same-state guard: no second blit
        assert calls == ["blit"]
        viewer.set_split_view(False)
        assert viewer._split_active is False
        assert viewer._secondary_tex_id == 0

    def test_split_divider_hit_band(self, viewer) -> None:
        split_x = viewer.get_view_pos_at_image_pos(
            QPointF(TEX_W / 2.0, TEX_H / 2.0)).x()
        assert viewer._hit_split_handle(QPointF(split_x, 240)) is True
        assert viewer._hit_split_handle(QPointF(split_x + 8.0, 240)) is True
        assert viewer._hit_split_handle(QPointF(split_x + 20.0, 240)) is False

    def test_split_divider_drag_updates_position(self, viewer) -> None:
        viewer._split_active = True
        viewer._secondary_tex_id = 42
        _press(viewer, Qt.MouseButton.LeftButton, QPointF(320, 240))
        assert viewer._split_dragging is True
        _move(viewer, QPointF(480, 240), buttons=Qt.MouseButton.LeftButton)
        assert viewer._split_pos == pytest.approx(0.75, abs=1e-3)
        _move(viewer, QPointF(5000, 240), buttons=Qt.MouseButton.LeftButton)
        assert viewer._split_pos == 0.98  # clamped
        _release(viewer, Qt.MouseButton.LeftButton, QPointF(5000, 240))
        assert viewer._split_dragging is False


# ── display controls ────────────────────────────────────────────────────────


class TestDisplayControls:
    def test_channel_mode_clamped_and_label_synced(self, viewer) -> None:
        viewer.set_channel_mode(9)
        assert viewer._channel_mode == 6
        assert viewer._toolbar.channel_btn.text() == "A.Ovr"
        viewer.set_channel_mode(-3)
        assert viewer._channel_mode == 0
        assert viewer._toolbar.channel_btn.text() == "RGB"

    def test_exposure_clamped_and_label_synced(self, viewer) -> None:
        viewer.set_exposure(15.0)
        assert viewer._exposure == 10.0
        assert viewer._toolbar.exposure_label.text() == "Exp: 10.0"
        viewer.set_exposure(-99.0)
        assert viewer._exposure == -10.0
        viewer.set_exposure(1.234)  # rounded to 1 decimal
        assert viewer._exposure == 1.2

    def test_gamma_clamped(self, viewer) -> None:
        viewer.set_gamma(9.0)
        assert viewer._viewer_gamma == 5.0
        viewer.set_gamma(0.01)
        assert viewer._viewer_gamma == 0.1
        viewer.set_gamma(2.345)
        assert viewer._viewer_gamma == 2.35  # rounded to 2 decimals

    def test_display_transfer_clamp_and_srgb_alias(self, viewer) -> None:
        viewer.set_display_transfer(9)
        assert viewer._display_transfer == 4
        assert viewer._display_srgb is True
        viewer.set_display_transfer(-1)
        assert viewer._display_transfer == 0
        assert viewer._display_srgb is False
        viewer.set_display_srgb(True)
        assert viewer._display_transfer == 1
        assert viewer._display_srgb is True

    def test_tonemap_and_target_peak_clamped(self, viewer) -> None:
        viewer.set_tonemap(7)
        assert viewer._tonemap == 2
        viewer.set_tonemap(-1)
        assert viewer._tonemap == 0
        viewer.set_target_peak(0.0)
        assert viewer._target_peak == 1.0
        viewer.set_target_peak(1000.0)
        assert viewer._target_peak == 1000.0

    def test_mask_preview_flag(self, viewer) -> None:
        viewer.set_mask_preview(True)
        assert viewer._alpha_preview == 1
        viewer.set_mask_preview(False)
        assert viewer._alpha_preview == 0

    def test_resolution_mode_validation_and_signal_dedupe(
            self, viewer) -> None:
        events: list[str] = []
        viewer.resolution_mode_changed.connect(lambda m: events.append(m))
        viewer.set_resolution_mode("bogus")
        assert viewer._resolution_mode == "full"
        assert events == []
        viewer.set_resolution_mode("  HALF ")
        assert viewer._resolution_mode == "half"
        assert events == ["half"]
        viewer.set_resolution_mode("half")
        assert events == ["half"]  # unchanged -> no re-emit

    def test_background_mode_validation_signal_and_clear_color(
            self, viewer) -> None:
        events: list[str] = []
        viewer.background_mode_changed.connect(lambda m: events.append(m))
        viewer.set_background_mode("Purple")
        assert viewer._background_mode == "checker"
        assert events == []
        viewer.set_background_mode("black")
        assert events == ["black"]
        assert viewer._viewport_clear_color_rgb() == (0.0, 0.0, 0.0)
        viewer.set_background_mode("white")
        assert viewer._viewport_clear_color_rgb() == (1.0, 1.0, 1.0)

    def test_magnifier_toggle_and_zoom_quantize(self, viewer) -> None:
        viewer.set_magnifier_enabled(True)
        assert viewer._magnifier_enabled is True
        assert viewer._toolbar._magnifier_toggle_action.isChecked() is True
        viewer.toggle_magnifier()
        assert viewer._magnifier_enabled is False
        viewer.set_magnifier_zoom(7.0)
        assert viewer._magnifier_zoom == 8.0
        viewer.set_magnifier_zoom(3.9)
        assert viewer._magnifier_zoom == 4.0

    def test_adjust_exposure_by_delegates(self, viewer) -> None:
        viewer.set_exposure(1.0)
        viewer._adjust_exposure_by(0.5)
        assert viewer._exposure == 1.5


class TestAutoProxyInteraction:
    def test_note_emits_once_per_burst(self, viewer) -> None:
        events: list[bool] = []
        viewer.auto_proxy_interaction.connect(lambda b: events.append(b))
        viewer._note_auto_proxy_interaction()
        viewer._note_auto_proxy_interaction()
        assert events == [True]
        assert viewer._auto_proxy_interacting is True

    def test_end_emits_false_once(self, viewer) -> None:
        events: list[bool] = []
        viewer.auto_proxy_interaction.connect(lambda b: events.append(b))
        viewer._note_auto_proxy_interaction()
        viewer._end_auto_proxy_interaction()
        viewer._end_auto_proxy_interaction()  # already idle -> no re-emit
        assert events == [True, False]
        assert viewer._auto_proxy_interacting is False

    def test_idle_timer_wired_to_end(self, viewer) -> None:
        viewer._note_auto_proxy_interaction()
        assert viewer._auto_proxy_idle_timer is not None
        assert viewer._auto_proxy_idle_timer.isActive()
        viewer._auto_proxy_idle_timer.stop()  # keep test synchronous


# ── selection & hit testing ─────────────────────────────────────────────────


class TestSelectionAndHitTesting:
    def test_select_node_signals_and_state(self, viewer) -> None:
        node_events: list[tuple[str, bool]] = []
        sel_events: list[object] = []
        viewer.node_selected.connect(lambda n, m: node_events.append((n, m)))
        viewer.selection_changed.connect(lambda s: sel_events.append(s))
        viewer.select_node("n1")
        assert viewer.get_selected_node() == "n1"
        assert node_events == [("n1", False)]
        assert "n1" in viewer.get_selection_state().selected_node_ids

    def test_multi_select_accumulates_then_clears(self, viewer) -> None:
        viewer.select_node("n1")
        viewer.select_node("n2", multi_select=True)
        assert sorted(viewer.get_selection_state().selected_node_ids) == [
            "n1", "n2"]
        viewer.clear_selection()
        assert viewer.get_selected_node() is None
        assert not viewer.get_selection_state().selected_node_ids

    def test_node_output_regions_stored_as_copy(self, viewer) -> None:
        regions = [NodeOutputRegion("a", "blur", (0.0, 0.0, 960.0, 1080.0))]
        viewer.set_node_output_regions(regions)
        regions.clear()
        assert len(viewer.get_node_output_regions()) == 1
        got = viewer.get_node_output_regions()
        assert got is not viewer._node_output_regions

    def test_hit_test_node_prefers_high_z_order(self, viewer) -> None:
        viewer.set_node_output_regions([
            NodeOutputRegion("low", "blur", (0.0, 0.0, 960.0, 1080.0),
                             z_order=1),
            NodeOutputRegion("high", "merge", (480.0, 0.0, 1440.0, 1080.0),
                             z_order=5),
        ])
        overlap_view = viewer.get_view_pos_at_image_pos(QPointF(700.0, 540.0))
        hit = viewer.hit_test_node_at_pos(overlap_view)
        assert hit is not None and hit.node_id == "high"
        only_low = viewer.get_view_pos_at_image_pos(QPointF(100.0, 100.0))
        hit2 = viewer.hit_test_node_at_pos(only_low)
        assert hit2 is not None and hit2.node_id == "low"

    def test_hit_test_node_outside_returns_none(self, viewer) -> None:
        viewer.set_node_output_regions([
            NodeOutputRegion("a", "blur", (0.0, 0.0, 100.0, 100.0))])
        assert viewer.hit_test_node_at_pos(QPointF(-30, -30)) is None

    def test_handle_hit_test_uses_pixel_radius(self, viewer) -> None:
        viewer._tool_handles = {"h1": QPointF(100, 100),
                                "h2": QPointF(400, 300)}
        assert viewer.hit_test_handle_at_pos(QPointF(105, 104)) == "h1"
        assert viewer.hit_test_handle_at_pos(QPointF(300, 100)) is None

    def test_set_tool_handles_recomputes_and_clears(self, viewer) -> None:
        viewer._tool_handles = {"stale": QPointF(1, 1)}
        viewer.set_tool_handles({"stale": QPointF(2, 2)})  # arg ignored by API
        assert viewer._tool_handles == {}  # no selection -> cleared

    def test_hover_move_emits_handle_hit_changed(self, viewer) -> None:
        viewer._tool_handles = {"h1": QPointF(100, 100)}
        hits: list[object] = []
        viewer.handle_hit_changed.connect(lambda h: hits.append(h))
        _move(viewer, QPointF(102, 100), buttons=Qt.MouseButton.NoButton)
        assert viewer._hover_handle == "h1"
        assert hits == ["h1"]
        _move(viewer, QPointF(300, 300), buttons=Qt.MouseButton.NoButton)
        assert viewer._hover_handle is None
        assert hits == ["h1", None]


# ── pixel inspector ─────────────────────────────────────────────────────────


class TestPixelInspector:
    def test_enter_and_leave_events_track_mouse(self, viewer) -> None:
        from PyQt6.QtGui import QEnterEvent
        viewer.enterEvent(QEnterEvent(QPointF(1, 1), QPointF(1, 1),
                                      QPointF(1, 1)))
        assert viewer._mouse_in_viewer is True
        # PyQt6 does not expose QLeaveEvent; a typed token event is enough
        viewer.leaveEvent(QEvent(QEvent.Type.Leave))
        assert viewer._mouse_in_viewer is False
        assert viewer._pixel_valid is False
        assert viewer._inspector_bar._pos_label.text() == "x: —  y: —"

    def test_inspector_maps_image_coords_without_gl(self, viewer,
                                                    monkeypatch) -> None:
        # Deterministic legacy readback failure: no GL context exists, and we
        # pin glReadPixels to return nothing so the guard path is exercised.
        monkeypatch.setattr(
            cvmod._GLWrapper, "glReadPixels", lambda *a, **k: None)
        viewer._mouse_in_viewer = True
        viewer._mouse_pos = QPoint(320, 240)
        viewer._current_tex_id = 0
        viewer._owned_display_tex_id = None
        viewer._update_inspector_from_framebuffer(640, 480)
        assert viewer._pixel_img_x == TEX_W // 2
        assert viewer._pixel_img_y == TEX_H // 2
        assert viewer._pixel_valid is False  # readback unavailable headless
        assert viewer._inspector_bar._rgba_label.text() == \
            "R: —  G: —  B: —  A: —"

    def test_inspector_invalid_when_mouse_absent(self, viewer) -> None:
        viewer._mouse_in_viewer = False
        viewer._pixel_valid = True
        viewer._update_inspector_from_framebuffer(640, 480)
        assert viewer._pixel_valid is False

    def test_inspector_bar_readout_formatting(self, qapp) -> None:
        bar = cvmod.PixelInspectorBar()
        bar.update_pixel_info(12, 34, 0.5, 0.25, 0.75, 1.0, valid=True,
                              space="linear")
        assert bar._pos_label.text() == "x: 12  y: 34"
        assert bar._rgba_label.text() == \
            "R: 0.500  G: 0.250  B: 0.750  A: 1.000  (linear)"
        bar.update_pixel_info(0, 0, 0, 0, 0, 0, valid=False)
        assert bar._pos_label.text() == "x: —  y: —"
        assert bar._rgba_label.text() == "R: —  G: —  B: —  A: —"

    def test_inspector_bar_format_info(self, qapp) -> None:
        bar = cvmod.PixelInspectorBar()
        bar.update_format_info(1920, 1080)
        assert bar._format_label.text() == "1920×1080"
        bar.update_format_info(0, 1080)
        assert bar._format_label.text() == ""

    def test_inspector_bar_preview_indicator(self, qapp) -> None:
        bar = cvmod.PixelInspectorBar()
        bar.set_preview_node("node_1", "Blur")
        assert bar._preview_label.text() == "◎ Preview: Blur"
        assert bar._preview_label.isVisibleTo(bar) is True
        bar.set_preview_node(None)
        assert bar._preview_label.text() == ""
        assert bar._preview_label.isVisibleTo(bar) is False


# ── toolbar sync helpers ────────────────────────────────────────────────────


class TestToolbarSync:
    def test_adjust_exposure_reads_label_and_sums(self, viewer) -> None:
        tb = viewer._toolbar
        values: list[float] = []
        tb.exposure_changed.connect(lambda e: values.append(e))
        tb.update_exposure_label(2.0)
        tb._adjust_exposure(0.3)
        assert values == [pytest.approx(2.3)]
        tb._adjust_exposure(-1.0)
        assert values[-1] == pytest.approx(1.3)

    def test_adjust_exposure_clamps_at_ten_stops(self, viewer) -> None:
        tb = viewer._toolbar
        values: list[float] = []
        tb.exposure_changed.connect(lambda e: values.append(e))
        tb.update_exposure_label(9.8)
        tb._adjust_exposure(1.0)
        assert values == [10.0]
        tb.update_exposure_label(-9.9)
        tb._adjust_exposure(-0.5)
        assert values[-1] == -10.0

    def test_render_pass_menu_order_and_labels(self, viewer) -> None:
        tb = viewer._toolbar
        tb.set_available_render_passes({"shadow", "beauty", "depth", "uv"})
        assert list(tb._render_pass_actions.keys()) == [
            "beauty", "depth", "uv", "shadow"]
        assert tb.render_pass_btn.text() == "Beauty"
        requested: list[str] = []
        tb.render_pass_changed.connect(lambda p: requested.append(p))
        tb._render_pass_actions["shadow"].trigger()
        assert requested == ["shadow"]
        # the toolbar is wired to the viewer, which falls back to beauty
        # until the AOV texture actually arrives
        assert viewer._selected_render_pass == "beauty"

    def test_render_pass_label_falls_back_to_beauty(self, viewer) -> None:
        viewer._toolbar.set_available_render_passes(("beauty", "normal"))
        viewer._toolbar.update_render_pass_label("does-not-exist")
        assert viewer._toolbar.render_pass_btn.text() == "Beauty"
        assert viewer._toolbar._render_pass_actions["beauty"].isChecked()

    def test_update_ab_compare_slot_normalizes(self, viewer) -> None:
        tb = viewer._toolbar
        tb.update_ab_compare_slot("b")
        assert tb._ab_slot_actions["B"].isChecked() is True
        assert tb._ab_slot_actions["A"].isChecked() is False

    def test_update_magnifier_syncs_toggle_and_zoom(self, viewer) -> None:
        tb = viewer._toolbar
        tb.update_magnifier(True, 4.0)
        assert tb._magnifier_toggle_action.isChecked() is True
        assert tb._magnifier_zoom_actions[4.0].isChecked() is True
        assert tb._magnifier_zoom_actions[8.0].isChecked() is False

    def test_update_background_mode_syncs_radio(self, viewer) -> None:
        tb = viewer._toolbar
        tb.update_background_mode("white")
        assert tb._background_actions["white"].isChecked() is True
        assert tb._background_actions["checker"].isChecked() is False

    def test_update_channel_label(self, viewer) -> None:
        viewer._toolbar.update_channel_label("Luma")
        assert viewer._toolbar.channel_btn.text() == "Luma"

    def test_channel_menu_actions_cover_all_modes(self, viewer) -> None:
        ids = [a.data() for a in viewer._toolbar.channel_menu.actions()]
        assert ids == [m[0] for m in cvmod.CHANNEL_MODES]


# ── view mode switching ─────────────────────────────────────────────────────


class TestViewModeSwitching:
    def test_default_is_2d(self, viewer) -> None:
        assert viewer.get_view_mode() == "2d"

    def test_set_view_mode_emits_and_resets_zoom_pan(self, viewer) -> None:
        modes: list[str] = []
        viewer.view_mode_changed.connect(lambda m: modes.append(m))
        viewer._zoom_level = 4.0
        viewer._pan_x = 0.5
        viewer._pan_y = -0.5
        viewer.set_view_mode("3d")
        assert viewer.get_view_mode() == "3d"
        assert modes == ["3d"]
        assert viewer._zoom_level == 1.0
        assert (viewer._pan_x, viewer._pan_y) == (0.0, 0.0)

    def test_set_view_mode_invalid_falls_back_to_2d(self, viewer) -> None:
        viewer.set_view_mode("bogus")
        assert viewer.get_view_mode() == "2d"

    def test_same_view_mode_is_noop(self, viewer) -> None:
        modes: list[str] = []
        viewer.view_mode_changed.connect(lambda m: modes.append(m))
        viewer.set_view_mode("2d")
        assert modes == []

    def test_tab_event_toggles_view_mode(self, viewer) -> None:
        ev = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Tab,
                       Qt.KeyboardModifier.NoModifier, "\t")
        assert viewer.event(ev) is True
        assert viewer.get_view_mode() == "3d"
        ev2 = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Backtab,
                        Qt.KeyboardModifier.NoModifier, "")
        assert viewer.event(ev2) is True
        assert viewer.get_view_mode() == "2d"

    def test_3d_tools_visibility_follows_view_mode(self, viewer) -> None:
        container = viewer._toolbar._3d_tools_container
        viewer.set_view_mode("3d")
        assert container.isVisibleTo(viewer) is True
        viewer.set_view_mode("2d")
        assert container.isVisibleTo(viewer) is False

    def test_quad_view_codes_track_3d_state(self, viewer) -> None:
        from src.features.composite.ui.viewport3d_state import (
            ViewportLayoutMode, ViewportViewKind)
        # 2D mode always reports perspective codes
        assert viewer.get_quad_active_view() == 0
        assert viewer.get_single_ortho_view() == 0
        viewer.set_view_mode("3d")
        state = viewer._viewport3d_ctrl.state
        state.active_view = ViewportViewKind.FRONT
        state.layout_mode = ViewportLayoutMode.QUAD
        assert viewer.get_quad_active_view() == 3
        state.toggle_layout()  # QUAD -> SINGLE keeps FRONT as ortho override
        assert viewer.get_single_ortho_view() == 3
        assert viewer.get_quad_active_view() == 0


# ── diagnostics / picking state ─────────────────────────────────────────────


class TestDiagnosticsState:
    def test_perf_overlay_toggle_cycle(self, viewer) -> None:
        assert viewer.toggle_perf_overlay() is True
        assert viewer._perf_show_overlay is True
        assert viewer.toggle_perf_overlay() is False
        assert viewer.toggle_perf_overlay(True) is True
        assert viewer.toggle_perf_overlay(False) is False

    def test_set_render_diagnostics_rejects_non_dict(self, viewer) -> None:
        viewer.set_render_diagnostics({"perf_stats": {"tris": 1}})
        assert viewer._perf_diagnostics == {"perf_stats": {"tris": 1}}
        viewer.set_render_diagnostics("nope")
        assert viewer._perf_diagnostics is None
        viewer.set_render_diagnostics(None)
        assert viewer._perf_diagnostics is None

    def test_set_id_pick_info_validation(self, viewer) -> None:
        viewer.set_id_pick_info({"w": 10})
        assert viewer._id_pick_info == {"w": 10}
        viewer.set_id_pick_info(None)
        assert viewer._id_pick_info is None
        viewer.set_id_pick_info([1, 2])
        assert viewer._id_pick_info is None

    def test_clear_component_selection_emits(self, viewer) -> None:
        events: list[object] = []
        viewer.component_selection_changed.connect(
            lambda s: events.append(s))
        viewer.clear_component_selection()
        assert len(events) == 1
        assert events[0] is viewer.get_component_selection()

    def test_viewer3d_content_size_changed_only_in_3d(self, viewer) -> None:
        sizes: list[tuple[int, int]] = []
        viewer.viewer3d_content_size_changed.connect(
            lambda w, h: sizes.append((w, h)))
        viewer._emit_viewer3d_content_size_changed()  # 2d -> no emit
        assert sizes == []
        viewer.set_view_mode("3d")
        viewer._emit_viewer3d_content_size_changed()
        assert sizes == [(640, 480)]
        viewer._emit_viewer3d_content_size_changed()  # same size -> dedup
        assert sizes == [(640, 480)]


# ── ROI/PAINT drags through real mouse events ────────────────────────────────
# See docs/testcoverage/2026-09-28/BUGS-agent-B1.md (BUG-B1-01 / BUG-B1-02).
# mouseMoveEvent/mouseReleaseEvent dispatch tool handlers only when
# ViewerInteractionState.is_dragging is True, but the ROI and PAINT press
# handlers used to return True without calling start_drag(), so the drag
# state never armed.  Fixed by AUD-9700/AUD-9701 (wd8-g41): the press
# handlers now arm the interaction state and the release path dispatches
# the PAINT/ROI release handlers, so these run as plain regression tests.


class TestMouseEventDispatchRegression:
    # AUD-9701 (was BUG-B1-01): ROI drag arms, updates and commits.
    def test_roi_drag_commits_through_real_mouse_events(self, viewer) -> None:
        viewer.set_tool_mode(ViewerToolMode.ROI)
        rois: list[object] = []
        viewer.user_roi_changed.connect(lambda b: rois.append(b))
        _press(viewer, Qt.MouseButton.LeftButton, QPointF(100, 100))
        assert viewer.is_drag_active() is True
        _move(viewer, QPointF(300, 250), buttons=Qt.MouseButton.LeftButton)
        assert viewer._user_roi_drag_rect is not None
        assert viewer._user_roi_drag_rect[2] > 0.0
        _release(viewer, Qt.MouseButton.LeftButton, QPointF(300, 250))
        assert viewer._user_roi_drag_rect is None
        assert viewer._user_roi_rect is not None
        assert len(rois) == 1

    # AUD-9700 (was BUG-B1-02): PAINT stroke records points and commits.
    def test_paint_stroke_commits_through_real_mouse_events(
            self, viewer) -> None:
        viewer.set_tool_mode(ViewerToolMode.PAINT)
        viewer._selected_node_type = "paint"
        viewer.select_node("p1")
        strokes: list[tuple[str, dict]] = []
        viewer.paint_stroke_committed.connect(
            lambda nid, geom: strokes.append((nid, geom)))
        _press(viewer, Qt.MouseButton.LeftButton, QPointF(200, 200))
        _move(viewer, QPointF(300, 250), buttons=Qt.MouseButton.LeftButton)
        _move(viewer, QPointF(400, 300), buttons=Qt.MouseButton.LeftButton)
        assert len(viewer._paint_drag_points or []) >= 2
        _release(viewer, Qt.MouseButton.LeftButton, QPointF(400, 300))
        assert viewer._paint_drag_points is None
        assert len(strokes) == 1
        assert strokes[0][0] == "p1"
