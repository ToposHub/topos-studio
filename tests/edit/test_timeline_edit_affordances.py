"""Raster geometry checks for small timeline pointers and edge brackets."""
from __future__ import annotations

from typing import Any

import pytest
from PyQt6.QtCore import QPoint, QRectF, Qt
from PyQt6.QtGui import QBrush, QColor, QImage, QPainter, QPen

from src.features.edit.ui.timeline_edit_affordances import edit_cursor, paint_trim_bracket

pytestmark = pytest.mark.edit_ui

KINDS = ("trim_left", "trim_right", "ripple_left", "ripple_right", "roll", "slip", "slide", "fade")


def _ink_pixels(image: QImage) -> list[tuple[int, int]]:
    return [(x, y) for y in range(image.height()) for x in range(image.width())
            if image.pixelColor(x, y).alpha() > 0]


@pytest.mark.parametrize("kind", KINDS)
def test_cursor_has_accurate_logical_hotspot_and_unclipped_2x_vector_ink(qapp: Any, kind: Any) -> None:
    cursor = edit_cursor(kind)
    assert cursor is edit_cursor(kind)
    assert cursor.hotSpot() == QPoint(16, 16)
    pixmap = cursor.pixmap()
    assert not pixmap.isNull()
    assert pixmap.devicePixelRatio() == 2.0
    assert (pixmap.width(), pixmap.height()) == (64, 64)
    assert pixmap.deviceIndependentSize().width() == 32.0
    image = pixmap.toImage()
    pixels = _ink_pixels(image)
    assert len(pixels) >= 50
    assert min(x for x, _y in pixels) >= 1
    assert max(x for x, _y in pixels) < image.width() - 1
    assert min(y for _x, y in pixels) >= 1
    assert max(y for _x, y in pixels) < image.height() - 1
    colors = [image.pixelColor(x, y) for x, y in pixels]
    assert any(color.alpha() > 200 and min(color.red(), color.green(), color.blue()) > 220 for color in colors)
    assert any(color.alpha() > 200 and max(color.red(), color.green(), color.blue()) < 40 for color in colors)


def test_edit_operations_have_distinct_cursor_images(qapp: Any) -> None:
    images = [edit_cursor(kind).pixmap().toImage() for kind in KINDS]
    rasters = {tuple(image.pixel(x, y) for y in range(image.height()) for x in range(image.width()))
               for image in images}
    assert len(rasters) == len(KINDS)


@pytest.mark.parametrize("edge", ["left", "right"])
@pytest.mark.parametrize("active", [False, True])
def test_bracket_uses_a_narrow_full_height_edge_and_short_caps(qapp: Any, edge: Any, active: bool) -> None:
    image = QImage(220, 100, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    try:
        paint_trim_bracket(painter, QRectF(10, 10, 200, 80), edge, QColor("#E29227"), active)
    finally:
        painter.end()
    pixels = _ink_pixels(image)
    assert pixels
    assert all(10 <= x < 210 and 10 <= y < 90 for x, y in pixels)
    assert all(x < 20 if edge == "left" else x >= 200 for x, _y in pixels)
    # Caps occupy only the top/bottom few rows. The middle cannot become a
    # 10–15px filled trim handle, including during an active edit.
    middle = [(x, y) for x, y in pixels if 20 <= y < 80]
    assert all(x < 15 if edge == "left" else x >= 205 for x, _y in middle)
    assert all(any(py == y for _x, py in pixels) for y in range(10, 90))
    assert len(pixels) < 700


def test_bracket_restores_callers_painter_state(qapp: Any) -> None:
    image = QImage(100, 100, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    try:
        painter.setPen(QPen(QColor("red"), 3.0))
        painter.setBrush(QBrush(QColor("blue")))
        painter.setClipRect(QRectF(0, 0, 70, 70))
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        original = (painter.pen(), painter.brush(), painter.clipBoundingRect(), painter.renderHints())
        paint_trim_bracket(painter, QRectF(10, 10, 60, 50), "left", "#E29227", True)
        assert (painter.pen(), painter.brush(), painter.clipBoundingRect(), painter.renderHints()) == original
    finally:
        painter.end()


@pytest.mark.parametrize("width,height", [(1, 1), (2, 2), (4, 60)])
def test_small_brackets_do_not_paint_outside_the_clip(qapp: Any, width: int, height: int) -> None:
    image = QImage(30, 90, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    try:
        for edge in ("left", "right"):
            paint_trim_bracket(painter, QRectF(10, 10, width, height), edge, "#E29227", True)
    finally:
        painter.end()
    assert all(10 <= x < 10 + width and 10 <= y < 10 + height for x, y in _ink_pixels(image))


def test_invalid_affordance_names_are_rejected(qapp: Any) -> None:
    with pytest.raises(ValueError, match="Unknown timeline edit cursor"):
        edit_cursor("invalid")  # type: ignore[arg-type]
    image = QImage(10, 10, QImage.Format.Format_ARGB32_Premultiplied)
    painter = QPainter(image)
    try:
        with pytest.raises(ValueError, match="Unknown trim edge"):
            paint_trim_bracket(painter, QRectF(0, 0, 10, 10), "invalid", "white")  # type: ignore[arg-type]
    finally:
        painter.end()
