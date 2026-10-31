"""Regression tests for source-range Trim previews and edge feedback."""

from __future__ import annotations

from typing import Any

import pytest
from PyQt6.QtCore import QPointF, QRectF
from PyQt6.QtGui import QImage, QPainter, QPixmap
from PyQt6.QtWidgets import QGraphicsScene, QStyleOptionGraphicsItem

from src.features.edit.ui.timeline_items import (
    ClipItem,
    EditTool,
    FADE_HANDLE_HIT_HEIGHT,
    FADE_HANDLE_HIT_RADIUS,
    RollHandleItem,
    THUMBNAIL_MODE_FILMSTRIP,
    _trim_cursor,
)
from src.shared.media.thumbnail_provider import ClipThumbnailSet


pytestmark = pytest.mark.edit_core


def _make_video_clip() -> ClipItem:
    return ClipItem(
        clip_id="clip-1",
        track_id="v1",
        asset_name="source.mov",
        start_frame=0,
        duration_frames=100,
        pixels_per_frame=2.0,
        track_index=0,
        source_start=20,
        source_duration=100,
        track_height=60,
        media_type="video",
        asset_id="asset-1",
        asset_path="/tmp/source.mov",
    )


def test_trim_preview_changes_source_range_without_committing_thumbnail_range(
    qapp: Any,
) -> None:
    clip = _make_video_clip()

    clip.begin_trim_preview()
    clip.update_trim_preview_source_range(35, 85)

    assert (clip._source_start, clip._source_duration) == (35, 85)
    assert (clip._trim_preview_source_start, clip._trim_preview_source_duration) == (20, 100)

    clip.end_trim_preview()
    assert clip._trim_preview_active is False


def test_trim_preview_does_not_stretch_cached_tail_into_extended_range(
    qapp: Any,
) -> None:
    """Held frames remain at source-time positions when a clip is extended."""
    clip = _make_video_clip()
    clip.begin_trim_preview()
    clip.update_trim_preview_source_range(20, 180)

    frames = [
        (20, QPixmap(16, 9)),
        (70, QPixmap(16, 9)),
        (119, QPixmap(16, 9)),
    ]
    thumbnail_set = ClipThumbnailSet(
        request_key="trim-preview",
        frames=frames,
        thumbnail_height=56,
    )
    drawn_cells: list[QRectF] = []
    clip._draw_single_thumbnail_at = (  # type: ignore[method-assign]
        lambda painter, pixmap, target_cell, clip_rect: drawn_cells.append(target_cell)
    )

    canvas = QImage(400, 70, QImage.Format.Format_ARGB32_Premultiplied)
    painter = QPainter(canvas)
    try:
        clip._draw_cached_thumbnails(
            painter,
            QRectF(0.0, 0.0, 360.0, 60.0),
            thumbnail_set,
            THUMBNAIL_MODE_FILMSTRIP,
            clip._source_start,
            clip._source_duration,
        )
    finally:
        painter.end()

    assert drawn_cells
    assert max(cell.right() for cell in drawn_cells) < 358.0


def test_trim_edge_cursors_are_distinct() -> None:
    left = _trim_cursor("left")
    right = _trim_cursor("right")

    assert left.pixmap().cacheKey() != right.pixmap().cacheKey()


def test_clip_seam_uses_horizontal_cursor_in_trim_tools(qapp: Any) -> None:
    """A clip seam should advertise its horizontal drag affordance."""
    handle = RollHandleItem(
        left_clip_id="left",
        right_clip_id="right",
        cut_frame=100,
        track_id="v1",
        pixels_per_frame=2.0,
        track_index=0,
    )

    for tool in (EditTool.SELECT, EditTool.TRIM, EditTool.RIPPLE, EditTool.ROLL):
        handle.set_active_tool(tool)
        assert handle.cursor().shape().name == "SizeHorCursor"

    handle.set_active_tool(EditTool.BLADE)
    assert handle.cursor().shape().name == "ArrowCursor"


def test_fade_handle_hit_area_is_compact() -> None:
    """Fade controls should not swallow the nearby trim edge area."""
    clip = _make_video_clip()
    clip_rect = QRectF(0.0, 0.0, clip._width, clip._track_height)
    fade_x, fade_y = clip._fade_handle_position(clip_rect, "in")

    assert clip._fade_handle_at(
        QPointF(fade_x + FADE_HANDLE_HIT_RADIUS - 0.5, fade_y)
    ) == "in"
    assert clip._fade_handle_at(
        QPointF(fade_x + FADE_HANDLE_HIT_RADIUS + 0.5, fade_y)
    ) is None
    assert clip._fade_handle_at(
        QPointF(fade_x, FADE_HANDLE_HIT_HEIGHT + 0.5)
    ) is None


def test_narrow_clip_paint_restores_qpainter_state(qapp: Any) -> None:
    """Early exits for tiny clips must not leak QPainter save states."""
    clip = ClipItem(
        clip_id="tiny",
        track_id="v1",
        asset_name="tiny.mov",
        start_frame=0,
        duration_frames=1,
        pixels_per_frame=0.25,
        track_index=0,
        track_height=60,
    )
    canvas = QImage(32, 64, QImage.Format.Format_ARGB32_Premultiplied)
    painter = QPainter(canvas)
    try:
        clip.paint(painter, QStyleOptionGraphicsItem())
    finally:
        painter.end()


def test_clip_hit_shape_does_not_include_border_invalidation_margin(
    qapp: Any,
) -> None:
    """The next clip must not steal clicks just before the current cut."""
    clip = _make_video_clip()

    assert clip.boundingRect().left() < 0
    assert clip.shape().contains(clip.boundingRect().topLeft()) is False
    assert clip.shape().contains(QRectF(0, 0, clip._width, clip._track_height).center())
