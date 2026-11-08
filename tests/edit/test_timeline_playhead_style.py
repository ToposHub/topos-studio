"""Geometry regressions for the compact timeline playhead."""

from __future__ import annotations

import pytest
from PyQt6.QtCore import QPointF

from src.features.edit.ui.timeline_items import PlayheadItem


pytestmark = pytest.mark.edit_core


def test_playhead_pointer_is_compact_and_connected_to_stem() -> None:
    """The pointer must remain a compact shape with no gap before its stem."""
    playhead = PlayheadItem(frame=0, pixels_per_frame=1.0, num_tracks=1)

    assert playhead.boundingRect().width() == 14.0
    assert playhead.shape().contains(QPointF(0.0, 9.5))
    assert playhead.shape().contains(QPointF(0.0, 10.0))


def test_pointer_is_flat_and_dragging_keeps_compact_bounds(qapp) -> None:
    from PyQt6.QtCore import Qt
    from PyQt6.QtGui import QImage, QPainter
    from src.features.edit.ui.timeline_items import PLAYHEAD_COLOR
    playhead = PlayheadItem(20, 2, 1)
    bounds = playhead.boundingRect()
    image = QImage(40, 50, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.translate(20, 0)
    playhead.paint(painter, None)
    painter.end()
    assert image.pixelColor(20, 2) == PLAYHEAD_COLOR
    assert image.pixelColor(20, 5) == PLAYHEAD_COLOR
    assert image.pixelColor(10, 5).alpha() == 0
    playhead.set_dragging(True)
    assert playhead.boundingRect() == bounds
    assert playhead._head_path().boundingRect().height() == 12


@pytest.mark.parametrize('dpr', [1.0, 1.25, 1.5, 2.0, 3.0])
@pytest.mark.parametrize('fraction', [0.0, 0.25, 0.5, 0.75])
def test_head_and_stem_share_the_same_device_pixel_axis(qapp, dpr: float, fraction: float) -> None:
    from PyQt6.QtCore import Qt
    from PyQt6.QtGui import QImage, QPainter
    playhead = PlayheadItem(0, 1, 1)
    image = QImage(240, 200, QImage.Format.Format_ARGB32_Premultiplied)
    image.setDevicePixelRatio(dpr)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.translate(30 + fraction, 0)
    playhead.paint(painter, None)
    painter.end()
    def centroid(row: int) -> float:
        weights = [(x + 0.5, image.pixelColor(x, row).alpha()) for x in range(image.width())]
        return sum(x * alpha for x, alpha in weights) / sum(alpha for _x, alpha in weights)
    assert abs(centroid(round(3*dpr)) - centroid(round(25*dpr))) <= 0.12
