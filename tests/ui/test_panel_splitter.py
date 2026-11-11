"""Pixel and native-drag regressions for workspace splitter junctions."""

from PyQt6.QtCore import QPoint, Qt
from PyQt6.QtGui import QColor, QImage, QPainter
from PyQt6.QtWidgets import QSplitter, QWidget

import pytest

from src.shared.ui.panel_splitter import PANEL_RESIZE_EXTENT, PanelSplitter
from src.shared.ui.style_sheet import C, DARK_THEME


def _render(widget: QWidget, dpr: int = 1) -> QImage:
    image = QImage(widget.width() * dpr, widget.height() * dpr, QImage.Format.Format_ARGB32)
    image.setDevicePixelRatio(dpr)
    image.fill(QColor(C.BG_PANEL))
    painter = QPainter(image)
    widget.render(painter)
    painter.end()
    return image


@pytest.mark.parametrize("dpr", [1, 2])
@pytest.mark.parametrize("orientation", [Qt.Orientation.Horizontal, Qt.Orientation.Vertical])
def test_wide_hit_target_renders_one_logical_pixel(qapp, qtbot, dpr: int, orientation: Qt.Orientation) -> None:
    splitter = PanelSplitter(orientation)
    qtbot.addWidget(splitter)
    splitter.setStyleSheet(DARK_THEME)
    splitter.resize(400, 300)
    splitter.addWidget(QWidget())
    splitter.addWidget(QWidget())
    splitter.show()
    qapp.processEvents()
    handle = splitter.handle(1)
    assert splitter.handleWidth() == PANEL_RESIZE_EXTENT
    image = _render(handle, dpr)
    if orientation == Qt.Orientation.Horizontal:
        pixels = [image.pixelColor(x, image.height() // 2).name() for x in range(image.width())]
    else:
        pixels = [image.pixelColor(image.width() // 2, y).name() for y in range(image.height())]
    assert pixels.count(C.PANEL_DIVIDER) == dpr


def test_old_saved_splitter_state_keeps_new_hit_area(qapp, qtbot) -> None:
    old = QSplitter(Qt.Orientation.Horizontal)
    qtbot.addWidget(old)
    old.addWidget(QWidget())
    old.addWidget(QWidget())
    old.setHandleWidth(1)
    old.setSizes([140, 260])
    splitter = PanelSplitter(Qt.Orientation.Horizontal)
    qtbot.addWidget(splitter)
    splitter.addWidget(QWidget())
    splitter.addWidget(QWidget())
    assert splitter.restoreState(old.saveState())
    assert splitter.handleWidth() == PANEL_RESIZE_EXTENT
    assert splitter.sizes()[0] < splitter.sizes()[1]


def test_nested_rules_join_and_follow_native_drag(qapp, qtbot) -> None:
    outer = PanelSplitter(Qt.Orientation.Vertical)
    qtbot.addWidget(outer)
    outer.setStyleSheet(DARK_THEME)
    outer.resize(500, 400)
    inner = PanelSplitter(Qt.Orientation.Horizontal)
    inner.addWidget(QWidget())
    inner.addWidget(QWidget())
    outer.addWidget(inner)
    outer.addWidget(QWidget())
    outer.show()
    qapp.processEvents()
    horizontal = outer.handle(1)
    vertical = inner.handle(1)

    def assert_join() -> None:
        image = _render(horizontal)
        x = (vertical.mapTo(outer, QPoint((vertical.width() - 1) // 2, 0)).x()
             - horizontal.mapTo(outer, QPoint()).x())
        center = (horizontal.height() - 1) // 2
        assert all(image.pixelColor(x, y).name() == C.PANEL_DIVIDER for y in range(center + 1))
        assert image.pixelColor(x - 2, 0).name() != C.PANEL_DIVIDER

    assert_join()
    before = inner.sizes()
    # Press near the edge of the hit area, several pixels off the visible rule.
    point = QPoint(1, vertical.height() // 2)
    qtbot.mousePress(vertical, Qt.MouseButton.LeftButton, pos=point)
    qtbot.mouseMove(vertical, pos=point + QPoint(40, 0))
    qtbot.mouseRelease(vertical, Qt.MouseButton.LeftButton, pos=point + QPoint(40, 0))
    qapp.processEvents()
    assert inner.sizes() != before
    assert_join()
    inner.setSizes([120, 372])
    qapp.processEvents()
    assert_join()
