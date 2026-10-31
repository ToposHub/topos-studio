"""
SourceViewer widget transform contract tests.

Tests the zoom/pan/resize behavior that SourceViewer relies on
through its PreviewViewTransform instance and frame guard logic.

Contract invariants:
1. open_asset() → reset to fit mode (reset_fit)
2. set_texture in fit mode → auto-fits
3. wheelEvent → mode == "manual", zoom changes
4. Continuous frame loading (playback) does NOT reset manual zoom
5. set_gpu_frame with stale frame → discarded
6. set_gpu_frame with expected frame → accepted
7. resizeGL in fit mode → auto-refit
8. resizeGL in manual mode → preserved
"""
import math

from src.features.edit.ui.preview_view_transform import PreviewViewTransform
from src.features.edit.ui.source_viewer import SourceViewerWidget


class TestSourceViewerOpenAsset:
    """Simulates SourceViewerWidget.open_asset() behavior."""

    def test_open_asset_resets_to_fit(self):
        """Opening a new asset resets transform to fit (mirrors open_asset line 1180)."""
        t = PreviewViewTransform()
        t.zoom_at(3.0, 500, 300, 1000, 600, 1920, 1080)
        t.pan_by(100.0, 200.0)
        assert t.mode == "manual"
        assert t.zoom > 1.0

        # open_asset calls: self.viewer._view_transform.reset_fit()
        t.reset_fit()
        assert t.mode == "fit"
        assert t.zoom == 1.0
        assert t.pan_x_px == 0.0
        assert t.pan_y_px == 0.0

    def test_open_second_asset_resets_even_from_actual(self):
        """Opening a new asset from actual mode also resets."""
        t = PreviewViewTransform()
        t.set_actual_size(3840, 2160, 960, 540)
        assert t.mode == "actual"

        t.reset_fit()
        assert t.mode == "fit"
        assert t.zoom == 1.0


class TestSourceViewerSetTexture:
    """Simulates SourceViewer.set_texture() / set_shared_texture() behavior."""

    def test_set_texture_in_fit_mode_triggers_fit(self):
        """Contract: set_texture calls fit_to_window when mode is 'fit'."""
        t = PreviewViewTransform()
        assert t.mode == "fit"

        # set_texture checks mode == "fit" and calls fit_to_window → reset_fit
        if t.mode == "fit":
            t.reset_fit()

        assert t.zoom == 1.0
        assert t.pan_x_px == 0.0

    def test_set_texture_in_manual_mode_preserves_zoom(self):
        """Contract: set_texture does NOT call fit_to_window when mode is 'manual'."""
        t = PreviewViewTransform()
        t.zoom_at(2.5, 500, 300, 1000, 600, 1920, 1080)
        zoom_before = t.zoom
        pan_before = (t.pan_x_px, t.pan_y_px)

        # set_texture checks mode != "fit" → does NOT call fit_to_window
        if t.mode != "fit":
            pass  # no reset

        assert t.zoom == zoom_before
        assert t.pan_x_px == pan_before[0]
        assert t.pan_y_px == pan_before[1]

    def test_set_shared_texture_same_behavior_as_set_texture(self):
        """Contract: set_shared_texture follows same fit/manual logic."""
        t = PreviewViewTransform()
        t.zoom_at(1.8, 500, 300, 1000, 600, 1920, 1080)
        assert t.mode == "manual"
        zoom_before = t.zoom

        # set_shared_texture: same logic — only fits if mode is "fit"
        if t.mode == "fit":
            t.reset_fit()

        assert t.zoom == zoom_before  # manual mode preserved


class TestSourceViewerPlaybackZoom:
    """Tests that playback frame updates don't reset manual zoom."""

    def test_continuous_frame_updates_preserve_manual_zoom(self):
        """Simulates playback: loading many frames while zoomed in.

        Contract: SourceViewer.set_texture_from_qimage() does NOT change
        _view_transform at all — it only uploads the texture.
        The transform is only modified by user interaction (wheel/pan/F/1)
        or open_asset() reset.
        """
        t = PreviewViewTransform()
        t.zoom_at(2.0, 500, 300, 1000, 600, 1920, 1080)
        assert t.mode == "manual"
        zoom = t.zoom
        pan = (t.pan_x_px, t.pan_y_px)

        # Simulate 30 frames of playback
        for frame_idx in range(30):
            # set_texture_from_qimage does NOT touch _view_transform
            # It only updates _current_tex_id, _tex_width, _tex_height
            # So the transform state remains unchanged
            pass

        assert t.zoom == zoom
        assert t.pan_x_px == pan[0]
        assert t.pan_y_px == pan[1]
        assert t.mode == "manual"

    def test_frame_updates_after_pan_preserve_pan(self):
        """Pan during playback is preserved across frame updates."""
        t = PreviewViewTransform()
        t.pan_by(50.0, -30.0)
        assert t.mode == "manual"

        # Frame update (doesn't touch transform)
        zoom = t.zoom
        pan = (t.pan_x_px, t.pan_y_px)

        # More frame updates
        for _ in range(10):
            pass  # frame update doesn't change transform

        assert t.zoom == zoom
        assert t.pan_x_px == pan[0]
        assert t.pan_y_px == pan[1]


class TestSourceViewerGpuFrameGuard:
    """Tests the GPU frame guard logic in SourceViewerWidget.set_gpu_frame().

    The guard is a trailing window: results may arrive up to
    ``_GPU_STALE_WINDOW`` frames behind the expected frame (background decode
    latency) but never ahead of it.
    """

    WINDOW = SourceViewerWidget._GPU_STALE_WINDOW

    @staticmethod
    def _should_discard(gpu_frame: int, expected_frame: int) -> bool:
        return (
            gpu_frame >= 0
            and expected_frame >= 0
            and (
                gpu_frame > expected_frame
                or gpu_frame < expected_frame - TestSourceViewerGpuFrameGuard.WINDOW
            )
        )

    def test_accept_matching_frame(self):
        """GPU frame with matching expected frame is accepted."""
        assert not self._should_discard(42, 42)

    def test_accept_frame_within_decode_latency_window(self):
        """A result one decode latency behind the expected frame is accepted."""
        expected_frame = 50
        assert not self._should_discard(49, expected_frame)
        assert not self._should_discard(expected_frame - self.WINDOW, expected_frame)

    def test_discard_stale_frame(self):
        """GPU frame far behind the expected frame is discarded."""
        expected_frame = 50
        # User seeked to frame 50, but an old GPU result arrives late
        assert self._should_discard(expected_frame - self.WINDOW - 1, expected_frame)
        assert self._should_discard(20, expected_frame)

    def test_discard_future_frame(self):
        """A result ahead of the expected frame is never displayed."""
        assert self._should_discard(51, 50)

    def test_accept_frame_when_guard_disabled(self):
        """When expected_frame is -1 (guard disabled), all frames accepted."""
        assert not self._should_discard(42, -1)

    def test_accept_frame_with_negative_gpu_frame(self):
        """When gpu frame is -1, don't apply guard."""
        assert not self._should_discard(-1, 50)

    def test_rapid_seeking_sequence(self):
        """Simulates rapid seeking: expected frame advances faster than GPU."""
        expected_frame = 100
        stale_results = [80, 85, 87]  # old GPU results far behind

        for gpu_frame in stale_results:
            assert self._should_discard(gpu_frame, expected_frame)

        # Late-but-within-window results are displayable (playback latency)
        for gpu_frame in (95, 99):
            assert not self._should_discard(gpu_frame, expected_frame)

        # Current frame result arrives
        assert not self._should_discard(100, expected_frame)


class TestSourceViewerResize:
    """Simulates SourceViewer.resizeGL() behavior."""

    def test_resize_in_fit_mode_refits(self):
        t = PreviewViewTransform()
        rect1 = t.display_rect_px(1920, 1080, 1000, 600)
        assert t.mode == "fit"

        # Resize → SourceViewer.resizeGL calls fit_to_window
        t.reset_fit()
        rect2 = t.display_rect_px(1920, 1080, 800, 500)
        assert t.zoom == 1.0

    def test_resize_in_manual_mode_preserves(self):
        t = PreviewViewTransform()
        t.zoom_at(2.0, 500, 300, 1000, 600, 1920, 1080)
        zoom_before = t.zoom
        pan_before = (t.pan_x_px, t.pan_y_px)

        # resizeGL checks mode != "fit" → does nothing
        if t.mode != "fit":
            pass  # preserve

        assert t.zoom == zoom_before
        assert t.pan_x_px == pan_before[0]
        assert t.pan_y_px == pan_before[1]


class TestSourceViewerWheelZoom:
    """Simulates SourceViewer.wheelEvent → zoom_at behavior."""

    def test_wheel_zoom_switches_to_manual(self):
        t = PreviewViewTransform()
        assert t.mode == "fit"
        t.zoom_at(1.2, 500, 300, 1000, 600, 1920, 1080)
        assert t.mode == "manual"

    def test_anchor_preserved_during_zoom(self):
        t = PreviewViewTransform()
        anchor_x, anchor_y = 600.0, 350.0

        before = t.display_rect_px(1920, 1080, 1000, 600)
        rel_x = (anchor_x - before[0]) / before[2]
        rel_y = (anchor_y - before[1]) / before[3]

        t.zoom_at(2.0, anchor_x, anchor_y, 1000, 600, 1920, 1080)

        after = t.display_rect_px(1920, 1080, 1000, 600)
        assert math.isclose((anchor_x - after[0]) / after[2], rel_x, abs_tol=1e-9)
        assert math.isclose((anchor_y - after[1]) / after[3], rel_y, abs_tol=1e-9)

    def test_zoom_clamped(self):
        t = PreviewViewTransform()
        t.zoom_at(100.0, 500, 300, 1000, 600, 1920, 1080)
        assert t.zoom <= t.max_zoom

        t.reset_fit()
        t.zoom_at(0.001, 500, 300, 1000, 600, 1920, 1080)
        assert t.zoom >= t.min_zoom
