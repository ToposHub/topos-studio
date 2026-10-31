"""Tests for lazy thumbnail loading in TimelineClipCard / TimelineClipStrip."""

from __future__ import annotations

import sys
import unittest
from unittest.mock import MagicMock, patch, PropertyMock

from PyQt6.QtWidgets import QApplication
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QPixmap

from src.features.color.ui.timeline_clip_strip import (
    CardSize,
    ClipPreviewMode,
    ColorClipEntry,
    TimelineClipCard,
    TimelineClipStrip,
    _clip_has_explicit_grade,
    _color_stack_has_grade,
    build_clip_entries,
)
from src.app.controllers.edit_controller import _crop_pixmap_to_aspect

# ---------------------------------------------------------------------------
# Ensure a QApplication exists (required for QWidget tests)
# ---------------------------------------------------------------------------
_app: QApplication | None = None


def _ensure_app() -> QApplication:
    global _app
    if _app is None:
        _app = QApplication.instance()
        if _app is None:
            _app = QApplication(sys.argv)
    return _app


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_entry(index: int = 0, color_graded: bool = False) -> ColorClipEntry:
    """Create a minimal ColorClipEntry for testing."""
    return ColorClipEntry(
        timeline_id="tl-1",
        clip_id=f"clip-{index}",
        track_id="V1",
        track_name="V1",
        asset_id=f"asset-{index}",
        asset_name=f"clip_{index:04d}.mov",
        asset_path=f"/tmp/clip_{index:04d}.mov",
        media_type="video",
        timeline_start=index * 100,
        timeline_duration=100,
        source_start=0,
        source_duration=100,
        color_stack_id=None,
        color_shared=False,
        color_graded=color_graded,
    )


def _make_image_entry(index: int = 0) -> ColorClipEntry:
    """Create an image ColorClipEntry for testing."""
    return ColorClipEntry(
        timeline_id="tl-1",
        clip_id=f"img-clip-{index}",
        track_id="V1",
        track_name="V1",
        asset_id=f"img-asset-{index}",
        asset_name=f"img_{index:04d}.png",
        asset_path=f"/tmp/img_{index:04d}.png",
        media_type="image",
        timeline_start=index * 100,
        timeline_duration=100,
        source_start=0,
        source_duration=100,
        color_stack_id=None,
        color_shared=False,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestTimelineClipCardLazyThumbnail(unittest.TestCase):
    """Verify that TimelineClipCard does NOT request thumbnails in constructor."""

    @classmethod
    def setUpClass(cls) -> None:
        _ensure_app()

    @patch("src.features.color.ui.timeline_clip_strip.ThumbnailProvider")
    def test_constructor_does_not_request_thumbnail(self, mock_provider_cls: MagicMock) -> None:
        """Card constructor must NOT call _request_thumbnail()."""
        mock_instance = MagicMock()
        mock_provider_cls.instance.return_value = mock_instance

        entry = _make_entry()
        card = TimelineClipCard(
            entry, clip_index=1, total_clips=1,
            preview_mode=ClipPreviewMode.MIDDLE_FRAME,
            card_size=CardSize.MEDIUM,
        )

        # _thumbnail_requested should be False after construction
        self.assertFalse(card._thumbnail_requested)

        # No thumbnail request methods should have been called
        mock_instance.get_frame_thumbnail.assert_not_called()
        mock_instance.get_thumbnail.assert_not_called()

        card.deleteLater()

    def test_graded_card_uses_gradient_bottom_label(self) -> None:
        """A graded clip is visually distinguishable in the strip."""
        graded_card = TimelineClipCard(_make_entry(0, color_graded=True))
        plain_card = TimelineClipCard(_make_entry(1, color_graded=False))

        self.assertIn("qlineargradient", graded_card._bottom_label.styleSheet())
        self.assertNotIn("qlineargradient", plain_card._bottom_label.styleSheet())

        graded_card.deleteLater()
        plain_card.deleteLater()

    def test_empty_color_stack_is_not_marked_as_graded(self) -> None:
        """Creating a stack alone must not show the graded marker."""
        self.assertFalse(
            _color_stack_has_grade(
                {"grading_params": {}, "soft_clip_params": {}}
            )
        )
        self.assertTrue(
            _color_stack_has_grade(
                {"grading_params": {"saturation": 0.2}}
            )
        )

    def test_reset_payload_defaults_are_not_marked_as_graded(self) -> None:
        """Reset-generated compatibility/default fields keep the marker off."""
        self.assertFalse(
            _color_stack_has_grade(
                {
                    "grading_params": {
                        "temperature": 0,
                        "shadows": 0,
                    },
                    "mixer_params": {
                        color: [0, 0, 0]
                        for color in (
                            "red", "orange", "yellow", "green",
                            "aqua", "blue", "purple", "magenta",
                        )
                    },
                    "soft_clip_params": {
                        "enabled": False,
                        "high": 1.0,
                        "white_point": 1.1,
                        "shoulder_strength": 1.0,
                        "low": 0.0,
                        "black_point": -0.1,
                        "toe_strength": 1.0,
                        "exposure": 0.0,
                        "contrast": 0.0,
                    },
                    "lut_intensity": 1.0,
                }
            )
        )

    def test_inherited_local_stack_is_not_marked_for_new_segment(self) -> None:
        """A split segment only shows the marker for its stack owner."""
        stack = {
            "scope": "clip_local",
            "clip_id": "clip-1",
            "grading_params": {"saturation": 0.2},
        }
        self.assertTrue(_clip_has_explicit_grade(stack, "clip-1", False))
        self.assertFalse(_clip_has_explicit_grade(stack, "clip-2", False))
        self.assertTrue(_clip_has_explicit_grade(stack, "clip-2", True))

    @patch("src.features.color.ui.timeline_clip_strip.ThumbnailProvider")
    def test_ensure_thumbnail_requested_only_once(self, mock_provider_cls: MagicMock) -> None:
        """ensure_thumbnail_requested() must only issue one request."""
        mock_instance = MagicMock()
        mock_provider_cls.instance.return_value = mock_instance

        entry = _make_entry()
        card = TimelineClipCard(
            entry, clip_index=1, total_clips=1,
            preview_mode=ClipPreviewMode.MIDDLE_FRAME,
            card_size=CardSize.MEDIUM,
        )

        # Should not be requested yet
        self.assertFalse(card._thumbnail_requested)

        # First call: should request
        card.ensure_thumbnail_requested()
        self.assertTrue(card._thumbnail_requested)
        self.assertEqual(mock_instance.get_frame_thumbnail.call_count, 1)

        # Second call: should be a no-op
        card.ensure_thumbnail_requested()
        self.assertEqual(mock_instance.get_frame_thumbnail.call_count, 1)

        # Third call: still no additional request
        card.ensure_thumbnail_requested()
        self.assertEqual(mock_instance.get_frame_thumbnail.call_count, 1)

        card.deleteLater()

    @patch("src.features.color.ui.timeline_clip_strip.ThumbnailProvider")
    def test_ensure_thumbnail_no_preview_mode(self, mock_provider_cls: MagicMock) -> None:
        """With NO_PREVIEW mode, ensure_thumbnail_requested should not decode."""
        mock_instance = MagicMock()
        mock_provider_cls.instance.return_value = mock_instance

        entry = _make_entry()
        card = TimelineClipCard(
            entry, clip_index=1, total_clips=1,
            preview_mode=ClipPreviewMode.NO_PREVIEW,
            card_size=CardSize.MEDIUM,
        )

        card.ensure_thumbnail_requested()
        self.assertTrue(card._thumbnail_requested)

        # get_frame_thumbnail should NOT be called for NO_PREVIEW
        mock_instance.get_frame_thumbnail.assert_not_called()

        card.deleteLater()


class TestTimelineClipStripLazyThumbnails(unittest.TestCase):
    """Verify scroll-aware lazy thumbnail loading in TimelineClipStrip."""

    @classmethod
    def setUpClass(cls) -> None:
        _ensure_app()

    @patch("src.features.color.ui.timeline_clip_strip.ThumbnailProvider")
    def test_request_visible_thumbnails_only_visible_range(self, mock_provider_cls: MagicMock) -> None:
        """_request_visible_thumbnails should only request thumbnails in the
        visible + prefetch range, not all cards."""
        mock_instance = MagicMock()
        mock_provider_cls.instance.return_value = mock_instance

        strip = TimelineClipStrip()

        # Build 50 entries
        entries = [_make_entry(i) for i in range(50)]
        strip._entries = entries
        strip._rebuild_cards()

        # Process events so QTimer.singleShot(0) fires
        _app.processEvents()

        # After rebuild, only visible + prefetch range should have been requested.
        # With default viewport ~800px and card width 132 + spacing 4 = 136,
        # visible range is about 6 cards, prefetch range is start-8 to end+8 = ~22 cards.
        requested_count = sum(
            1 for card in strip._cards if card._thumbnail_requested
        )

        # Must be less than total cards (50)
        self.assertLess(requested_count, 50)

        # Must be within visible + 16 (8 prefetch on each side)
        # Max possible: viewport_cards + 16
        self.assertLessEqual(requested_count, 30)

        strip.deleteLater()

    @patch("src.features.color.ui.timeline_clip_strip.ThumbnailProvider")
    def test_cards_follow_panel_height(self, mock_provider_cls: MagicMock) -> None:
        """Clip cards should grow when the Timeline Clips panel grows."""
        mock_provider_cls.instance.return_value = MagicMock()

        strip = TimelineClipStrip()
        strip.resize(900, 120)
        strip._entries = [_make_entry(0), _make_entry(1)]
        strip._rebuild_cards()
        strip.show()
        _app.processEvents()
        compact_height = strip._cards[0].height()
        compact_width = strip._cards[0].width()

        strip.resize(900, 240)
        _app.processEvents()
        expanded_height = strip._cards[0].height()
        expanded_width = strip._cards[0].width()

        self.assertGreater(expanded_height, compact_height)
        self.assertGreater(expanded_width, compact_width)
        self.assertEqual(
            strip._scroll_area.horizontalScrollBarPolicy(),
            Qt.ScrollBarPolicy.ScrollBarAlwaysOn,
        )
        horizontal_bar = strip._scroll_area.horizontalScrollBar()
        self.assertGreaterEqual(horizontal_bar.minimumHeight(), 9)

        strip.deleteLater()

    @patch("src.features.color.ui.timeline_clip_strip.ThumbnailProvider")
    def test_graded_preview_replaces_raw_card_thumbnail(
        self, mock_provider_cls: MagicMock,
    ) -> None:
        """A graded snapshot should survive a strip refresh without re-decoding."""
        mock_provider_cls.instance.return_value = MagicMock()
        strip = TimelineClipStrip()
        strip._entries = [_make_entry(0)]
        strip._rebuild_cards()
        _app.processEvents()

        graded_pixmap = QPixmap(32, 18)
        strip.set_graded_preview("clip-0", graded_pixmap)

        self.assertTrue(strip._cards[0]._preview_override_active)
        self.assertTrue(strip._cards[0]._thumbnail_requested)
        self.assertFalse(strip._cards[0]._pixmap.isNull())

        strip._rebuild_cards()
        _app.processEvents()
        self.assertTrue(strip._cards[0]._preview_override_active)

        strip.deleteLater()

    @patch("src.features.color.ui.timeline_clip_strip.ThumbnailProvider")
    def test_playback_active_prevents_new_requests(self, mock_provider_cls: MagicMock) -> None:
        """When playback is active, _request_visible_thumbnails should not
        issue new thumbnail requests for video clips."""
        mock_instance = MagicMock()
        mock_provider_cls.instance.return_value = mock_instance

        strip = TimelineClipStrip()

        entries = [_make_entry(i) for i in range(20)]
        strip._entries = entries
        strip._rebuild_cards()
        _app.processEvents()

        # Reset tracking
        for card in strip._cards:
            card._thumbnail_requested = False

        # Reset mock call counts
        mock_instance.get_frame_thumbnail.reset_mock()

        # Set playback active
        strip.set_playback_active(True)
        self.assertTrue(strip._playback_active)

        # Trigger visible thumbnails during playback
        strip._request_visible_thumbnails()

        # No new frame thumbnail requests should have been made
        mock_instance.get_frame_thumbnail.assert_not_called()

        # Cards should still not have thumbnails requested
        for card in strip._cards:
            self.assertFalse(card._thumbnail_requested)

        strip.deleteLater()

    @patch("src.features.color.ui.timeline_clip_strip.ThumbnailProvider")
    def test_playback_active_consumes_image_cache(self, mock_provider_cls: MagicMock) -> None:
        """During playback, image thumbnails should be consumed from cache."""
        from PyQt6.QtGui import QPixmap

        mock_instance = MagicMock()
        mock_provider_cls.instance.return_value = mock_instance

        # Return a cached pixmap for images
        cached_pixmap = QPixmap(64, 64)
        mock_instance.get_thumbnail.return_value = cached_pixmap

        strip = TimelineClipStrip()

        entries = [_make_image_entry(i) for i in range(10)]
        strip._entries = entries
        strip._rebuild_cards()
        _app.processEvents()

        # Reset tracking
        for card in strip._cards:
            card._thumbnail_requested = False

        strip.set_playback_active(True)
        strip._request_visible_thumbnails()

        # Image entries should have thumbnails from cache
        requested = [c for c in strip._cards if c._thumbnail_requested]
        self.assertGreater(len(requested), 0)

        strip.deleteLater()

    @patch("src.features.color.ui.timeline_clip_strip.ThumbnailProvider")
    def test_500_entries_suppresses_previews(self, mock_provider_cls: MagicMock) -> None:
        """With 500+ entries, all cards should use NO_PREVIEW mode by default."""
        mock_instance = MagicMock()
        mock_provider_cls.instance.return_value = mock_instance

        strip = TimelineClipStrip()

        entries = [_make_entry(i) for i in range(500)]
        strip._entries = entries
        strip._rebuild_cards()
        _app.processEvents()

        # Previews should be suppressed
        self.assertTrue(strip._previews_suppressed)

        # All cards should have NO_PREVIEW mode
        for card in strip._cards:
            self.assertEqual(card._preview_mode, ClipPreviewMode.NO_PREVIEW)

        # Even after requesting visible thumbnails, no frame decode should happen
        strip._request_visible_thumbnails()
        mock_instance.get_frame_thumbnail.assert_not_called()

        strip.deleteLater()

    @patch("src.features.color.ui.timeline_clip_strip.ThumbnailProvider")
    def test_500_entries_request_count(self, mock_provider_cls: MagicMock) -> None:
        """With 500 entries, the number of thumbnail requests must be
        <= visible + 16 after enabling previews."""
        mock_instance = MagicMock()
        mock_provider_cls.instance.return_value = mock_instance

        strip = TimelineClipStrip()

        entries = [_make_entry(i) for i in range(500)]
        strip._entries = entries
        strip._rebuild_cards()
        _app.processEvents()

        # Enable previews via the right-click action
        strip._previews_suppressed = False
        # Reset the preview mode so cards will request
        for card in strip._cards:
            card._preview_mode = ClipPreviewMode.MIDDLE_FRAME
            card._thumbnail_requested = False

        strip._request_visible_thumbnails()

        requested_count = sum(
            1 for card in strip._cards if card._thumbnail_requested
        )

        # Should be <= visible area + 16 (8 prefetch on each side)
        # With ~800px viewport and card_w=136, visible ~6 cards
        # Total with prefetch: ~6 + 16 = 22
        self.assertLessEqual(requested_count, 30)
        self.assertLess(requested_count, 500)

        strip.deleteLater()

    @patch("src.features.color.ui.timeline_clip_strip.ThumbnailProvider")
    def test_scroll_triggers_debounce_timer(self, mock_provider_cls: MagicMock) -> None:
        """_on_scroll_changed should start the debounce timer."""
        mock_instance = MagicMock()
        mock_provider_cls.instance.return_value = mock_instance

        strip = TimelineClipStrip()
        entries = [_make_entry(i) for i in range(20)]
        strip._entries = entries
        strip._rebuild_cards()
        _app.processEvents()

        # Stop any active timer from initial load
        strip._thumbnail_debounce_timer.stop()
        self.assertFalse(strip._thumbnail_debounce_timer.isActive())

        # Call _on_scroll_changed directly (simulates scroll event)
        strip._on_scroll_changed(100)

        # Timer should now be running
        self.assertTrue(strip._thumbnail_debounce_timer.isActive())

        strip.deleteLater()

    @patch("src.features.color.ui.timeline_clip_strip.ThumbnailProvider")
    def test_enable_previews_for_large_timeline(self, mock_provider_cls: MagicMock) -> None:
        """_enable_previews_for_large_timeline should clear suppression and rebuild."""
        mock_instance = MagicMock()
        mock_provider_cls.instance.return_value = mock_instance

        strip = TimelineClipStrip()

        entries = [_make_entry(i) for i in range(500)]
        strip._entries = entries
        strip._rebuild_cards()
        _app.processEvents()

        self.assertTrue(strip._previews_suppressed)

        # User enables previews via right-click
        strip._enable_previews_for_large_timeline()

        # Suppression should be cleared
        self.assertFalse(strip._previews_suppressed)

        # Cards should have been rebuilt with user's preview mode
        # (NOT NO_PREVIEW, but the saved _preview_mode)
        for card in strip._cards:
            self.assertEqual(card._preview_mode, strip._preview_mode)

        strip.deleteLater()


class TestTimelineClipStripPlaybackToggle(unittest.TestCase):
    """Test set_playback_active behavior."""

    @classmethod
    def setUpClass(cls) -> None:
        _ensure_app()

    def test_set_playback_active_state(self) -> None:
        strip = TimelineClipStrip()
        self.assertFalse(strip._playback_active)

        strip.set_playback_active(True)
        self.assertTrue(strip._playback_active)

        strip.set_playback_active(False)
        self.assertFalse(strip._playback_active)

        strip.deleteLater()


class TestTimelinePreviewCapture(unittest.TestCase):
    """Verify viewer matte is removed before a graded preview is cached."""

    @classmethod
    def setUpClass(cls) -> None:
        _ensure_app()

    def test_crop_capture_to_source_aspect(self) -> None:
        captured = QPixmap(400, 300)
        cropped = _crop_pixmap_to_aspect(captured, 16.0 / 9.0)

        self.assertEqual((cropped.width(), cropped.height()), (400, 225))

    def test_matching_aspect_keeps_capture(self) -> None:
        captured = QPixmap(400, 225)
        cropped = _crop_pixmap_to_aspect(captured, 16.0 / 9.0)

        self.assertEqual((cropped.width(), cropped.height()), (400, 225))


class TestRebuildPrimesFromThumbnailCache(unittest.TestCase):
    """A strip rebuild must restore cached thumbnails synchronously.

    ``_rebuild_cards`` destroys every card; without priming, the recreated
    cards paint the dark placeholder until the async provider round-trip
    completes — the black flash seen on clip switches.
    """

    @classmethod
    def setUpClass(cls) -> None:
        _ensure_app()

    @staticmethod
    def _dispose(strip: TimelineClipStrip) -> None:
        """Stop the 0 ms debounce timer before discarding the strip.

        Without an event loop the armed timer never fires here; if it fires
        during a LATER test file's processEvents() the provider singleton
        may have been swapped for that file's mock, and the mismatched
        signature aborts the interpreter inside the Qt slot.
        """
        strip._thumbnail_debounce_timer.stop()
        strip.deleteLater()

    @patch("src.features.color.ui.timeline_clip_strip.ThumbnailProvider")
    def test_rebuild_restores_cached_video_thumbnails(self, mock_provider_cls: MagicMock) -> None:
        """Video cards get their cached pixmap in the same pass, no event loop."""
        stub = MagicMock()
        cached = QPixmap(24, 14)
        stub.get_cached_frame_pixmap_any.return_value = cached
        mock_provider_cls.instance.return_value = stub

        strip = TimelineClipStrip()
        strip._entries = [_make_entry(i) for i in range(3)]
        strip._rebuild_cards()

        # No processEvents(): priming must be synchronous so the strip never
        # paints a blank frame between the old and the restored thumbnails.
        for card in strip._cards:
            self.assertIsNotNone(card._pixmap)
            self.assertFalse(card._pixmap.isNull())
        self.assertGreaterEqual(stub.get_cached_frame_pixmap_any.call_count, 1)

        self._dispose(strip)

    @patch("src.features.color.ui.timeline_clip_strip.ThumbnailProvider")
    def test_rebuild_restores_cached_image_thumbnails(self, mock_provider_cls: MagicMock) -> None:
        """Image cards are primed from the basic thumbnail cache."""
        stub = MagicMock()
        cached = QPixmap(24, 14)
        stub.get_cached_thumbnail.return_value = cached
        stub.get_cached_frame_pixmap_any.return_value = None
        mock_provider_cls.instance.return_value = stub

        strip = TimelineClipStrip()
        strip._entries = [_make_image_entry(0)]
        strip._rebuild_cards()

        self.assertIsNotNone(strip._cards[0]._pixmap)
        self.assertFalse(strip._cards[0]._pixmap.isNull())
        stub.get_cached_thumbnail.assert_called_once_with(
            "/tmp/img_0000.png", icon_size=144,
        )

        self._dispose(strip)

    @patch("src.features.color.ui.timeline_clip_strip.ThumbnailProvider")
    def test_graded_override_wins_over_cached_raw_thumbnail(
        self, mock_provider_cls: MagicMock,
    ) -> None:
        """The graded snapshot is applied after priming and takes priority."""
        stub = MagicMock()
        raw = QPixmap(24, 14)
        stub.get_cached_frame_pixmap_any.return_value = raw
        mock_provider_cls.instance.return_value = stub

        strip = TimelineClipStrip()
        graded = QPixmap(32, 18)
        strip._graded_preview_cache["clip-0"] = graded
        strip._entries = [_make_entry(0)]
        strip._rebuild_cards()

        card = strip._cards[0]
        self.assertTrue(card._preview_override_active)
        self.assertIs(card._pixmap, graded)

        self._dispose(strip)

    @patch("src.features.color.ui.timeline_clip_strip.ThumbnailProvider")
    def test_uncached_cards_stay_pending_without_crash(self, mock_provider_cls: MagicMock) -> None:
        """A cache miss leaves the card empty for async backfill, no error."""
        stub = MagicMock()
        stub.get_cached_frame_pixmap_any.return_value = None
        stub.get_cached_thumbnail.return_value = None
        mock_provider_cls.instance.return_value = stub

        strip = TimelineClipStrip()
        strip._entries = [_make_entry(0)]
        strip._rebuild_cards()

        self.assertIsNone(strip._cards[0]._pixmap)

        self._dispose(strip)

    def test_provider_probe_covers_all_key_forms(self) -> None:
        """get_cached_frame_pixmap_any hits the legacy tuple and the oldest
        (path, frame) stores — the strip relies on this to restore a
        thumbnail decoded by another component under a different key."""
        from src.shared.media.thumbnail_provider import ThumbnailProvider

        provider = ThumbnailProvider.__new__(ThumbnailProvider)
        pixmap = QPixmap(8, 6)

        class _DictCache(dict):
            put = dict.__setitem__

        entry = _make_entry(0)
        frame = 0

        # legacy (path, frame, height) tuple store
        provider._frame_pixmap_cache = _DictCache()
        provider.frame_cache = _DictCache()
        provider._frame_pixmap_cache[(entry.asset_path, frame, 144)] = pixmap
        hit = provider.get_cached_frame_pixmap_any(
            entry.asset_path, frame, target_height=144, asset_id=None,
        )
        self.assertIs(hit, pixmap)

        # oldest (path, frame) store only
        provider._frame_pixmap_cache = _DictCache()
        provider.frame_cache = _DictCache()
        provider.frame_cache[(entry.asset_path, frame)] = pixmap
        hit = provider.get_cached_frame_pixmap_any(
            entry.asset_path, frame, target_height=144, asset_id=entry.asset_id,
        )
        self.assertIs(hit, pixmap)


if __name__ == "__main__":
    unittest.main()
