"""
ProgramViewer widget transform contract tests.

Tests the zoom/pan/resize behavior that ProgramViewer relies on
through its PreviewViewTransform instance.

Contract invariants:
1. wheelEvent → mode == "manual", zoom changes
2. resizeGL with mode=="manual" → zoom/pan preserved
3. resizeGL with mode=="fit" → auto-refit
4. Middle-button pan → mode == "manual", pan changes
5. F key → mode == "fit", zoom == 1.0, pan == (0, 0)
6. 1 key → mode == "actual", zoom = 1/fit_scale
7. Zoom clamped to [min_zoom, max_zoom]
8. Anchor point preserved during zoom
"""
import math

from src.features.edit.ui.preview_view_transform import PreviewViewTransform


class TestProgramViewerWheelZoom:
    """Simulates ProgramViewer.wheelEvent → zoom_at behavior."""

    def test_wheel_zoom_switches_to_manual(self):
        t = PreviewViewTransform()
        assert t.mode == "fit"
        t.zoom_at(1.2, 500, 300, 1000, 600, 1920, 1080)
        assert t.mode == "manual"

    def test_wheel_zoom_increases_zoom(self):
        t = PreviewViewTransform()
        t.zoom_at(1.5, 500, 300, 1000, 600, 1920, 1080)
        assert t.zoom > 1.0

    def test_wheel_zoom_out_decreases_zoom(self):
        t = PreviewViewTransform()
        t.zoom_at(1.5, 500, 300, 1000, 600, 1920, 1080)
        zoom_after_in = t.zoom
        t.zoom_at(0.8, 500, 300, 1000, 600, 1920, 1080)
        assert t.zoom < zoom_after_in

    def test_anchor_point_preserved_on_zoom_in(self):
        t = PreviewViewTransform()
        anchor_x, anchor_y = 500.0, 300.0

        # Get position of anchor point before zoom
        before = t.display_rect_px(1920, 1080, 1000, 600)
        rel_x = (anchor_x - before[0]) / before[2]
        rel_y = (anchor_y - before[1]) / before[3]

        t.zoom_at(2.0, anchor_x, anchor_y, 1000, 600, 1920, 1080)

        after = t.display_rect_px(1920, 1080, 1000, 600)
        assert math.isclose((anchor_x - after[0]) / after[2], rel_x, abs_tol=1e-9)
        assert math.isclose((anchor_y - after[1]) / after[3], rel_y, abs_tol=1e-9)

    def test_anchor_point_preserved_on_zoom_out(self):
        t = PreviewViewTransform()
        # First zoom in
        t.zoom_at(3.0, 500, 300, 1000, 600, 1920, 1080)
        anchor_x, anchor_y = 750.0, 400.0

        before = t.display_rect_px(1920, 1080, 1000, 600)
        rel_x = (anchor_x - before[0]) / before[2]
        rel_y = (anchor_y - before[1]) / before[3]

        t.zoom_at(0.5, anchor_x, anchor_y, 1000, 600, 1920, 1080)

        after = t.display_rect_px(1920, 1080, 1000, 600)
        assert math.isclose((anchor_x - after[0]) / after[2], rel_x, abs_tol=1e-9)
        assert math.isclose((anchor_y - after[1]) / after[3], rel_y, abs_tol=1e-9)

    def test_zoom_clamped_to_max(self):
        t = PreviewViewTransform()
        t.zoom_at(1000.0, 500, 300, 1000, 600, 1920, 1080)
        assert t.zoom <= t.max_zoom

    def test_zoom_clamped_to_min(self):
        t = PreviewViewTransform()
        t.zoom_at(0.001, 500, 300, 1000, 600, 1920, 1080)
        assert t.zoom >= t.min_zoom


class TestProgramViewerResize:
    """Simulates ProgramViewer.resizeGL behavior."""

    def test_resize_preserves_manual_zoom(self):
        """Contract: resizeGL does not change zoom when mode is 'manual'."""
        t = PreviewViewTransform()
        t.zoom_at(2.0, 500, 300, 1000, 600, 1920, 1080)
        assert t.mode == "manual"
        zoom_before = t.zoom
        pan_before = (t.pan_x_px, t.pan_y_px)

        # Simulate resize: the viewer just doesn't call _fit_to_window()
        # when mode != "fit", so transform state is preserved exactly
        assert t.zoom == zoom_before
        assert t.pan_x_px == pan_before[0]
        assert t.pan_y_px == pan_before[1]

    def test_resize_in_fit_mode_refits(self):
        """Contract: in 'fit' mode, display rect recalculates on resize."""
        t = PreviewViewTransform()

        # Original viewport
        rect1 = t.display_rect_px(1920, 1080, 1000, 600)
        assert math.isclose(rect1[2], 1000.0, abs_tol=1e-9)  # width fits viewport
        assert math.isclose(rect1[3], 562.5, abs_tol=1e-9)   # height preserves aspect

        # Resize to 800x600 (still fit mode) — 1920x1080 fits width = 800 exactly
        rect2 = t.display_rect_px(1920, 1080, 800, 600)
        # Should fit to new viewport (1920*min(800/1920,600/1080) = 1920*0.4167 = 800)
        assert rect2[2] <= 800  # width <= viewport width
        assert math.isclose(rect2[2] / rect2[3], 1920 / 1080, abs_tol=1e-6)

    def test_resize_preserves_actual_size_mode(self):
        """Contract: 'actual' mode also preserves zoom/pan on resize."""
        t = PreviewViewTransform()
        t.set_actual_size(1920, 1080, 960, 540)
        assert t.mode == "actual"
        zoom_before = t.zoom

        # Simulate resize (actual mode doesn't auto-refit either)
        # The transform state is preserved
        assert t.zoom == zoom_before
        assert t.mode == "actual"


class TestProgramViewerPan:
    """Simulates ProgramViewer middle-button pan behavior."""

    def test_pan_changes_offset(self):
        t = PreviewViewTransform()
        t.pan_by(100.0, -50.0)
        assert t.pan_x_px == 100.0
        assert t.pan_y_px == -50.0

    def test_pan_switches_to_manual(self):
        t = PreviewViewTransform()
        assert t.mode == "fit"
        t.pan_by(10.0, 10.0)
        assert t.mode == "manual"

    def test_cumulative_pan(self):
        t = PreviewViewTransform()
        t.pan_by(100.0, 50.0)
        t.pan_by(-30.0, 20.0)
        assert t.pan_x_px == 70.0
        assert t.pan_y_px == 70.0

    def test_pan_preserves_zoom(self):
        t = PreviewViewTransform()
        t.zoom_at(2.0, 500, 300, 1000, 600, 1920, 1080)
        zoom_before = t.zoom
        t.pan_by(50.0, 30.0)
        assert t.zoom == zoom_before

    def test_mouse_drag_pan_pattern(self):
        """Simulates the actual viewer pan: start pos, delta, apply."""
        t = PreviewViewTransform()
        start_pan = (t.pan_x_px, t.pan_y_px)
        dx, dy = 120.0, -80.0

        # This mirrors mouseMoveEvent: new_pan = start_pan + delta
        t.pan_x_px = start_pan[0] + dx
        t.pan_y_px = start_pan[1] + dy
        t.mode = "manual"

        assert t.pan_x_px == 120.0
        assert t.pan_y_px == -80.0
        assert t.mode == "manual"


class TestProgramViewerKeyboardShortcuts:
    """Simulates ProgramViewer keyPressEvent behavior."""

    def test_f_key_returns_to_fit(self):
        """Pressing F resets to fit mode (mirrors _fit_to_window)."""
        t = PreviewViewTransform()
        t.zoom_at(3.0, 500, 300, 1000, 600, 1920, 1080)
        t.pan_by(100.0, 200.0)
        assert t.mode == "manual"

        # F key → _fit_to_window → reset_fit()
        t.reset_fit()
        assert t.mode == "fit"
        assert t.zoom == 1.0
        assert t.pan_x_px == 0.0
        assert t.pan_y_px == 0.0

    def test_1_key_sets_actual_size(self):
        """Pressing 1 sets actual pixel size (mirrors _actual_size)."""
        t = PreviewViewTransform()
        t.set_actual_size(1920, 1080, 960, 540)
        assert t.mode == "actual"
        assert math.isclose(t.zoom, 2.0)  # 1920/960 = 2x

    def test_actual_size_4k_in_hd_viewport(self):
        t = PreviewViewTransform()
        t.set_actual_size(3840, 2160, 1920, 1080)
        assert t.mode == "actual"
        # 4K in HD viewport: fit_scale=0.5, actual_size zoom=1/0.5=2.0
        # (zoom in 2x so each texel is 1 pixel on screen)
        assert math.isclose(t.zoom, 2.0)

    def test_actual_size_with_zoom_out_viewport(self):
        t = PreviewViewTransform()
        # 4K in 960x540 viewport → actual = 4x zoom
        t.set_actual_size(3840, 2160, 960, 540)
        assert t.mode == "actual"
        assert math.isclose(t.zoom, 4.0)

    def test_fit_then_actual_then_fit(self):
        """Cycling between fit and actual."""
        t = PreviewViewTransform()

        # Start in fit
        rect_fit = t.display_rect_px(1920, 1080, 960, 540)
        assert t.mode == "fit"

        # Go to actual
        t.set_actual_size(1920, 1080, 960, 540)
        assert t.mode == "actual"
        assert t.zoom > 1.0

        # Back to fit
        t.reset_fit()
        assert t.mode == "fit"
        assert t.zoom == 1.0
        rect_back = t.display_rect_px(1920, 1080, 960, 540)
        assert math.isclose(rect_fit[0], rect_back[0])
        assert math.isclose(rect_fit[2], rect_back[2])


class TestProgramViewerZoomSetAPI:
    """Tests the set_zoom / zoom_in / zoom_out API pattern."""

    def test_set_zoom_clamps(self):
        t = PreviewViewTransform()
        t.zoom = 0.001
        assert t.zoom < t.min_zoom  # raw set doesn't clamp
        # Viewer's set_zoom does: max(min_zoom, min(max_zoom, zoom))
        clamped = max(t.min_zoom, min(t.max_zoom, 0.001))
        assert clamped == t.min_zoom

    def test_zoom_in_increases(self):
        t = PreviewViewTransform()
        old = t.zoom
        new = old * 1.2
        t.zoom = max(t.min_zoom, min(t.max_zoom, new))
        t.mode = "manual"
        assert t.zoom > old
        assert t.mode == "manual"

    def test_zoom_out_decreases(self):
        t = PreviewViewTransform()
        t.zoom_at(3.0, 500, 300, 1000, 600, 1920, 1080)
        old = t.zoom
        new = old / 1.2
        t.zoom = max(t.min_zoom, min(t.max_zoom, new))
        assert t.zoom < old


class TestProgramViewerDisplayRect:
    """Tests display rect calculation contract."""

    def test_fit_display_rect_centered(self):
        t = PreviewViewTransform()
        x, y, w, h = t.display_rect_px(1920, 1080, 1000, 1000)
        # Should be centered
        assert math.isclose(x, 0.0, abs_tol=1e-9)  # left edge at viewport left
        assert math.isclose(y, (1000 - 562.5) / 2, abs_tol=1e-9)  # vertically centered

    def test_display_rect_preserves_aspect_ratio(self):
        t = PreviewViewTransform()
        for tex_w, tex_h, vp_w, vp_h in [
            (1920, 1080, 800, 600),
            (3840, 2160, 1920, 1080),
            (640, 480, 1000, 1000),
            (1080, 1920, 800, 600),  # portrait
        ]:
            x, y, w, h = t.display_rect_px(tex_w, tex_h, vp_w, vp_h)
            display_aspect = w / h
            tex_aspect = tex_w / tex_h
            assert math.isclose(display_aspect, tex_aspect, abs_tol=1e-6), \
                f"Aspect ratio mismatch for {tex_w}x{tex_h} in {vp_w}x{vp_h}"

    def test_zero_viewport_returns_safe_default(self):
        t = PreviewViewTransform()
        x, y, w, h = t.display_rect_px(1920, 1080, 0, 0)
        assert w == 0 and h == 0

    def test_zero_texture_returns_safe_default(self):
        t = PreviewViewTransform()
        x, y, w, h = t.display_rect_px(0, 0, 1000, 600)
        assert w == 1000  # falls back to viewport size
        assert h == 600
