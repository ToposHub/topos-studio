"""Regression tests for timeline thumbnail requests interrupted by playback."""

from __future__ import annotations

from typing import Any

import pytest
from PyQt6.QtCore import QRectF, QObject, pyqtSignal
from PyQt6.QtGui import QImage, QPainter

from src.features.edit.ui.timeline_items import ClipItem, THUMBNAIL_MODE_FILMSTRIP
from src.shared.media.thumbnail_provider import (
    ClipThumbnailCache,
    ClipThumbnailRequest,
    ClipThumbnailSet,
)


pytestmark = pytest.mark.edit_core


class _FakeThumbnailProvider(QObject):
    """Provider double that exposes the timeline-facing thumbnail contract."""

    clip_thumbnail_set_ready = pyqtSignal(str, object)

    def __init__(self) -> None:
        super().__init__()
        self.is_thumbnail_work_suspended: bool = False
        self.requests: list[Any] = []

    def request_clip_thumbnails(self, request: Any) -> None:
        self.requests.append(request)


def _make_video_clip() -> ClipItem:
    return ClipItem(
        clip_id="clip-1",
        track_id="v1",
        asset_name="source.mov",
        start_frame=0,
        duration_frames=120,
        pixels_per_frame=3.0,
        track_index=0,
        source_start=0,
        source_duration=120,
        track_height=60,
        media_type="video",
        asset_id="asset-1",
        asset_path="/tmp/source.mov",
    )


def _draw_thumbnails(clip: ClipItem) -> None:
    canvas = QImage(400, 70, QImage.Format.Format_ARGB32_Premultiplied)
    painter = QPainter(canvas)
    try:
        clip._draw_video_thumbnails(
            painter,
            QRectF(0.0, 0.0, 360.0, 60.0),
            THUMBNAIL_MODE_FILMSTRIP,
        )
    finally:
        painter.end()


@pytest.fixture
def thumbnail_provider(monkeypatch: pytest.MonkeyPatch) -> _FakeThumbnailProvider:
    from src.shared.media.thumbnail_provider import ThumbnailProvider

    provider = _FakeThumbnailProvider()
    cache = ClipThumbnailCache()
    cache.clear()
    monkeypatch.setattr(ThumbnailProvider, "instance", classmethod(lambda cls: provider))
    yield provider
    cache.clear()


def test_suspended_thumbnail_work_does_not_leave_clip_pending(
    qapp: Any,
    thumbnail_provider: _FakeThumbnailProvider,
) -> None:
    """A paint during zoom/playback must retry after thumbnail work resumes."""
    clip = _make_video_clip()
    thumbnail_provider.is_thumbnail_work_suspended = True

    _draw_thumbnails(clip)

    assert clip._thumbnail_pending is False
    assert thumbnail_provider.requests == []

    thumbnail_provider.is_thumbnail_work_suspended = False
    _draw_thumbnails(clip)

    assert clip._thumbnail_pending is True
    assert len(thumbnail_provider.requests) == 1


def test_waiting_clip_subscribes_to_shared_request_completion(
    qapp: Any,
    thumbnail_provider: _FakeThumbnailProvider,
) -> None:
    """A later split segment clears its pending state when the shared job ends."""
    clip = _make_video_clip()
    thumbnail_provider.is_thumbnail_work_suspended = True
    _draw_thumbnails(clip)
    request_key = clip._thumbnail_request_key
    assert request_key is not None

    cache = ClipThumbnailCache()
    cache.set_pending(request_key)
    thumbnail_provider.is_thumbnail_work_suspended = False
    _draw_thumbnails(clip)
    assert clip._thumbnail_pending is True

    thumbnail_provider.clip_thumbnail_set_ready.emit(
        request_key,
        ClipThumbnailSet(request_key=request_key, frames=[], thumbnail_height=56),
    )

    assert clip._thumbnail_pending is False


def test_zoom_uses_compatible_cached_set_while_sharper_set_loads() -> None:
    """The previous density bucket remains drawable during a zoom-in."""
    cache = ClipThumbnailCache()
    cache.clear()
    compact = ClipThumbnailRequest(
        asset_id="asset-1",
        asset_path="/tmp/source.mov",
        clip_width_px=300,
        source_start=0,
        source_duration=120,
        pixels_per_frame=3.0,
        thumbnail_height=56,
        display_mode=THUMBNAIL_MODE_FILMSTRIP,
    )
    expanded = ClipThumbnailRequest(
        asset_id="asset-1",
        asset_path="/tmp/source.mov",
        clip_width_px=1_600,
        source_start=0,
        source_duration=120,
        pixels_per_frame=16.0,
        thumbnail_height=56,
        display_mode=THUMBNAIL_MODE_FILMSTRIP,
    )
    compact_set = ClipThumbnailSet(
        request_key=compact.get_cache_key(),
        frames=[(0, object())],
        thumbnail_height=56,
    )
    cache.put(compact.get_cache_key(), compact_set)

    assert cache.get(expanded.get_cache_key()) is None
    assert cache.get_compatible(expanded) is compact_set
    cache.clear()
