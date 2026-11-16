"""Thumbnails retain the V/A crop when video rows overflow their panel."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from PyQt6.QtCore import QPoint
from PyQt6.QtGui import QColor, QPixmap
from PyQt6.QtWidgets import QApplication
from pytestqt.qtbot import QtBot

from src.features.edit.ui.timeline_items import ClipItem, TRACK_HEADER_WIDTH
from src.shared.media.thumbnail_provider import ClipThumbnailCache, ClipThumbnailRequest, ClipThumbnailSet
from tests.edit.test_timeline_section_dividers import _render, _sequence, _view

pytestmark = pytest.mark.edit_ui


@pytest.mark.parametrize("dpr", [1, 2])
@pytest.mark.parametrize("path", ["filmstrip", "start_end", "single", "scheduler", "placeholder"])
def test_overflowing_video_thumbnails_do_not_paint_in_audio_panel(
    qapp: QApplication, qtbot: QtBot, monkeypatch: pytest.MonkeyPatch, dpr: int, path: str,
) -> None:
    from dataclasses import replace

    sequence = _sequence(5, 4, 0)
    sequence = replace(sequence, audio_tracks=tuple(replace(t, clips=()) for t in sequence.audio_tracks))
    # Avoid decoding: exercise actual cached/placeholder drawing with a solid
    # identifiable thumbnail, including the scheduler's single-frame fallback.
    cache = ClipThumbnailCache()
    cache.clear()
    monkeypatch.setattr(ClipItem, "_thumbnail_provider",
                        lambda self: SimpleNamespace(is_thumbnail_work_suspended=True))
    pixmap = QPixmap(160, 90)
    pixmap.fill(QColor("#ff00ff"))
    view = _view(qtbot, sequence, height=360)
    mode = "single" if path == "scheduler" else "filmstrip" if path == "placeholder" else path
    view._thumbnail_display_mode = mode
    for item in view._items_map.values():
        item._thumbnail_display_mode = mode
    monkeypatch.setattr(view, "get_scheduler_cached_thumbnail",
                        lambda *args: pixmap if path == "scheduler" else None)
    qapp.processEvents()
    view._playhead_item = None

    def check() -> None:
        qapp.processEvents()
        items = list(view._items_map.values())
        if path in ("filmstrip", "start_end", "single"):
            for item in items:
                request = ClipThumbnailRequest(
                    asset_id=item._asset_id, asset_path=item._asset_path,
                    clip_width_px=int(item._width), source_start=item._source_start,
                    source_duration=item._source_duration, pixels_per_frame=item._pixels_per_frame,
                    thumbnail_height=item._track_height - 4, display_mode=mode,
                )
                key = request.get_cache_key()
                cache.put(key, ClipThumbnailSet(key, [(0, pixmap), (1000, pixmap)], item._track_height - 4))
        for item in items:
            item.hide()
        baseline = _render(view, dpr)
        for item in items:
            item.show()
        actual = _render(view, dpr)
        origin = view.viewport().mapTo(view, QPoint())
        area = view._layout_engine.area_layout()
        top = view.mapFromScene(area.audio_area.topLeft()).y()
        x = origin.x() + TRACK_HEADER_WIDTH + 20
        y = origin.y() + top
        width = view.viewport().width() - TRACK_HEADER_WIDTH - 40
        height = min(view.viewport().height() - top, int(area.audio_area.height()))
        assert height > 20
        assert actual.copy(x * dpr, y * dpr, width * dpr, height * dpr) == baseline.copy(
            x * dpr, y * dpr, width * dpr, height * dpr,
        ), f"Video {path} painted below V/A divider"
        # Make sure the fixture really drew video content, rather than passing
        # because all rows were hidden or the thumbnail path was unused.
        video_y = view.mapFromScene(area.video_area.topLeft()).y() + 10
        assert actual.copy(x * dpr, (origin.y() + video_y) * dpr, width * dpr, 5 * dpr) != baseline.copy(
            x * dpr, (origin.y() + video_y) * dpr, width * dpr, 5 * dpr,
        )

    try:
        check()
        view._video_v_scroll.setValue(view._video_v_scroll.maximum() // 2)
        qtbot.waitUntil(lambda: not view._scroll_layout_timer.isActive())
        check()
        view.resize(1100, 430)
        view.set_zoom(4.0)
        state = view.layout_state()
        state["video_area_ratio"] = 0.35
        state["video_scroll_offset"] = 0
        view.set_layout_state(state)
        check()
    finally:
        cache.clear()
