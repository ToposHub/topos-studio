"""
Tests for signal deduplication and generation tokens in TimelineClipStrip.

Verifies that:
- TimelineClipCard does NOT connect to provider signals directly
- TimelineClipStrip manages a single connection per signal
- Pending maps correctly route thumbnail results to the right cards
- Generation tokens discard stale results after _rebuild_cards
- _rebuild_cards clears pending maps and increments generation
"""

from __future__ import annotations

import sys
import os
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

# Ensure src is importable
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt6.QtWidgets import QApplication
from PyQt6.QtGui import QPixmap
from PyQt6.QtCore import QObject, pyqtSignal

from src.features.color.ui.timeline_clip_strip import (
    TimelineClipCard,
    TimelineClipStrip,
    ColorClipEntry,
    ClipPreviewMode,
    BottomLabelMode,
    CardSize,
    _compute_preview_frame,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_entry(
    clip_id: str = "clip1",
    asset_path: str = "/tmp/test.mp4",
    media_type: str = "video",
    source_start: int = 0,
    source_duration: int = 100,
) -> ColorClipEntry:
    return ColorClipEntry(
        timeline_id="tl1",
        clip_id=clip_id,
        track_id="v1",
        track_name="V1",
        asset_id="asset1",
        asset_name="test.mp4",
        asset_path=asset_path,
        media_type=media_type,
        timeline_start=0,
        timeline_duration=100,
        source_start=source_start,
        source_duration=source_duration,
        color_stack_id=None,
        color_shared=False,
    )


# Ensure a QApplication exists for widget tests
@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    yield app


# ---------------------------------------------------------------------------
# Mock ThumbnailProvider to avoid real media decode
# ---------------------------------------------------------------------------

class MockThumbnailProvider(QObject):
    """Minimal mock that exposes the same signals as ThumbnailProvider."""

    thumbnail_ready = pyqtSignal(str, QPixmap)
    frame_thumbnail_ready = pyqtSignal(str, int, QPixmap)

    _instance = None

    @classmethod
    def instance(cls):
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def get_thumbnail(self, path: str, icon_size: int = 128):
        return None  # never cached

    def get_frame_thumbnail(self, path: str, frame_index: int,
                            total_frames: int, icon_size: int = 256,
                            **kwargs) -> None:
        pass  # no-op


@pytest.fixture(autouse=True)
def _mock_provider(monkeypatch):
    """Replace ThumbnailProvider.instance() with our mock for all tests."""
    mock = MockThumbnailProvider.instance()
    monkeypatch.setattr(
        "src.features.color.ui.timeline_clip_strip.ThumbnailProvider.instance",
        lambda: mock,
    )
    yield mock
    # Reset singleton between tests
    MockThumbnailProvider._instance = None


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestCardNoDirectConnection:
    """Verify TimelineClipCard does not connect to provider signals."""

    def test_image_card_does_not_connect(self, qapp, _mock_provider):
        """After ensure_thumbnail_requested, no direct signal connection exists."""
        entry = _make_entry(media_type="image", asset_path="/tmp/img.jpg")
        card = TimelineClipCard(entry)

        # Request thumbnail
        card.ensure_thumbnail_requested()

        # Emit a signal from the mock provider — card should NOT receive it
        # because the card no longer connects directly.
        received = []

        original = card._on_thumbnail_ready
        def spy(path, pixmap):
            received.append(path)

        card._on_thumbnail_ready = spy

        pixmap = QPixmap(10, 10)
        _mock_provider.thumbnail_ready.emit("/tmp/img.jpg", pixmap)

        # The card should NOT have been called via signal
        assert len(received) == 0, (
            "Card received thumbnail via direct signal connection — "
            "cards should NOT connect to provider signals"
        )

    def test_video_card_does_not_connect(self, qapp, _mock_provider):
        """After ensure_thumbnail_requested, no direct signal connection exists."""
        entry = _make_entry(media_type="video")
        card = TimelineClipCard(entry)

        card.ensure_thumbnail_requested()

        received = []

        def spy(path, frame_index, pixmap):
            received.append((path, frame_index))

        card._on_frame_thumbnail_ready = spy

        pixmap = QPixmap(10, 10)
        frame_num = _compute_preview_frame(entry, ClipPreviewMode.MIDDLE_FRAME)
        _mock_provider.frame_thumbnail_ready.emit("/tmp/test.mp4", frame_num, pixmap)

        assert len(received) == 0, (
            "Card received frame thumbnail via direct signal connection — "
            "cards should NOT connect to provider signals"
        )


class TestStripSingleConnection:
    """Verify TimelineClipStrip manages a single connection to each signal."""

    def test_strip_connects_to_both_signals(self, qapp, _mock_provider):
        """Strip should connect to thumbnail_ready and frame_thumbnail_ready once."""
        strip = TimelineClipStrip()
        # The strip constructor connects to both signals.
        # Verify by emitting and checking side effects.
        assert _mock_provider.receivers(
            _mock_provider.thumbnail_ready
        ) >= 1, "Strip should be connected to thumbnail_ready"
        assert _mock_provider.receivers(
            _mock_provider.frame_thumbnail_ready
        ) >= 1, "Strip should be connected to frame_thumbnail_ready"

    def test_connection_count_does_not_grow_with_cards(self, qapp, _mock_provider):
        """Adding cards should NOT increase connection count to provider signals."""
        strip = TimelineClipStrip()
        strip._entries = [
            _make_entry(clip_id=f"c{i}", asset_path=f"/tmp/f{i}.mp4")
            for i in range(5)
        ]
        strip._rebuild_cards()

        thumb_receivers_before = _mock_provider.receivers(_mock_provider.thumbnail_ready)
        frame_receivers_before = _mock_provider.receivers(_mock_provider.frame_thumbnail_ready)

        # Request thumbnails for all cards
        for card in strip._cards:
            card.ensure_thumbnail_requested()

        thumb_receivers_after = _mock_provider.receivers(_mock_provider.thumbnail_ready)
        frame_receivers_after = _mock_provider.receivers(_mock_provider.frame_thumbnail_ready)

        assert thumb_receivers_after == thumb_receivers_before, (
            "thumbnail_ready connection count grew after card requests"
        )
        assert frame_receivers_after == frame_receivers_before, (
            "frame_thumbnail_ready connection count grew after card requests"
        )


class TestPendingMapRouting:
    """Verify pending maps correctly route thumbnail results to right cards."""

    def test_image_thumbnail_routed_to_registered_card(self, qapp, _mock_provider):
        """Image thumbnail signal routes to the correct card via pending map."""
        strip = TimelineClipStrip()
        entry = _make_entry(media_type="image", asset_path="/tmp/photo.jpg")
        card = TimelineClipCard(entry)
        strip._cards = [card]

        # Register card in pending map
        strip._register_image_card(card)

        # Emit thumbnail ready from provider
        pixmap = QPixmap(10, 10)
        received = []

        original = card._on_thumbnail_ready
        def spy(path, px):
            received.append((path, px))

        card._on_thumbnail_ready = spy

        _mock_provider.thumbnail_ready.emit("/tmp/photo.jpg", pixmap)

        assert len(received) == 1
        assert received[0][0] == "/tmp/photo.jpg"
        # QPixmap may be copied when passed through Qt signal, so check size
        assert not received[0][1].isNull()

        # Pending entry should be cleaned up
        key = (strip._thumbnail_generation, "/tmp/photo.jpg")
        assert key not in strip._pending_image_cards

    def test_frame_thumbnail_routed_to_registered_card(self, qapp, _mock_provider):
        """Frame thumbnail signal routes to the correct card via pending map."""
        strip = TimelineClipStrip()
        entry = _make_entry(media_type="video")
        card = TimelineClipCard(entry)
        strip._cards = [card]

        frame_num = _compute_preview_frame(entry, ClipPreviewMode.MIDDLE_FRAME)
        strip._register_frame_card(card, frame_num)

        pixmap = QPixmap(10, 10)
        received = []

        def spy(path, frame_idx, px):
            received.append((path, frame_idx, px))

        card._on_frame_thumbnail_ready = spy

        _mock_provider.frame_thumbnail_ready.emit("/tmp/test.mp4", frame_num, pixmap)

        assert len(received) == 1
        assert received[0][0] == "/tmp/test.mp4"
        assert received[0][1] == frame_num

    def test_multiple_cards_same_path(self, qapp, _mock_provider):
        """Multiple cards for the same path all receive the thumbnail."""
        strip = TimelineClipStrip()
        entry = _make_entry(media_type="image", asset_path="/tmp/shared.jpg")
        card1 = TimelineClipCard(entry, clip_index=1)
        card2 = TimelineClipCard(entry, clip_index=2)
        strip._cards = [card1, card2]

        strip._register_image_card(card1)
        strip._register_image_card(card2)

        count = [0]

        def spy1(path, px):
            count[0] += 1

        def spy2(path, px):
            count[0] += 1

        card1._on_thumbnail_ready = spy1
        card2._on_thumbnail_ready = spy2

        pixmap = QPixmap(10, 10)
        _mock_provider.thumbnail_ready.emit("/tmp/shared.jpg", pixmap)

        assert count[0] == 2, "Both cards should have received the thumbnail"

    def test_unregistered_card_not_called(self, qapp, _mock_provider):
        """A card NOT registered in pending maps should not receive thumbnails."""
        strip = TimelineClipStrip()
        entry = _make_entry(media_type="image", asset_path="/tmp/orphan.jpg")
        card = TimelineClipCard(entry)
        # NOT registering the card

        received = []

        def spy(path, px):
            received.append(path)

        card._on_thumbnail_ready = spy

        pixmap = QPixmap(10, 10)
        _mock_provider.thumbnail_ready.emit("/tmp/orphan.jpg", pixmap)

        assert len(received) == 0, "Unregistered card should not receive thumbnails"


class TestGenerationTokens:
    """Verify generation tokens discard stale results."""

    def test_rebuild_increments_generation(self, qapp, _mock_provider):
        """_rebuild_cards should increment the generation counter."""
        strip = TimelineClipStrip()
        gen_before = strip._thumbnail_generation

        strip._entries = [_make_entry()]
        strip._rebuild_cards()

        assert strip._thumbnail_generation == gen_before + 1

    def test_rebuild_clears_pending_maps(self, qapp, _mock_provider):
        """_rebuild_cards should clear pending maps."""
        strip = TimelineClipStrip()
        entry = _make_entry(media_type="image", asset_path="/tmp/img.jpg")
        card = TimelineClipCard(entry)
        strip._cards = [card]
        strip._register_image_card(card)

        assert len(strip._pending_image_cards) > 0

        strip._entries = [_make_entry()]
        strip._rebuild_cards()

        assert len(strip._pending_image_cards) == 0
        assert len(strip._pending_frame_cards) == 0

    def test_stale_generation_discarded(self, qapp, _mock_provider):
        """Thumbnail result from a previous generation is silently discarded."""
        strip = TimelineClipStrip()
        entry = _make_entry(media_type="image", asset_path="/tmp/old.jpg")
        card = TimelineClipCard(entry)
        strip._cards = [card]

        # Register in current generation
        strip._register_image_card(card)

        # Simulate rebuild — generation incremented, pending maps cleared
        old_gen = strip._thumbnail_generation
        strip._thumbnail_generation += 1
        strip._pending_image_cards.clear()

        received = []

        def spy(path, px):
            received.append(path)

        card._on_thumbnail_ready = spy

        # Emit a signal that corresponds to the old generation
        # The strip's callback will use the CURRENT generation to build the
        # key, so the old card is not in the new pending map.
        pixmap = QPixmap(10, 10)
        _mock_provider.thumbnail_ready.emit("/tmp/old.jpg", pixmap)

        assert len(received) == 0, (
            "Stale thumbnail from old generation should be discarded"
        )

    def test_fresh_generation_delivered(self, qapp, _mock_provider):
        """Thumbnail result for current generation is delivered correctly."""
        strip = TimelineClipStrip()

        # Build fresh cards
        strip._entries = [_make_entry(media_type="image", asset_path="/tmp/new.jpg")]
        strip._rebuild_cards()  # increments generation

        card = strip._cards[0]
        strip._register_image_card(card)

        received = []

        def spy(path, px):
            received.append(path)

        card._on_thumbnail_ready = spy

        pixmap = QPixmap(10, 10)
        _mock_provider.thumbnail_ready.emit("/tmp/new.jpg", pixmap)

        assert len(received) == 1, "Fresh generation result should be delivered"


class TestSignalCleanup:
    """Verify signal cleanup on strip destruction."""

    def test_cleanup_disconnects_signals(self, qapp, _mock_provider):
        """_cleanup_signal_connections should disconnect from provider signals."""
        strip = TimelineClipStrip()

        thumb_receivers_before = _mock_provider.receivers(_mock_provider.thumbnail_ready)
        assert thumb_receivers_before >= 1

        strip._cleanup_signal_connections()

        thumb_receivers_after = _mock_provider.receivers(_mock_provider.thumbnail_ready)
        assert thumb_receivers_after == thumb_receivers_before - 1
