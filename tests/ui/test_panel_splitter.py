"""Pixel and native-drag regressions for workspace splitter junctions."""

from PyQt6.QtCore import QPoint, QRect, Qt
from PyQt6.QtGui import QColor, QImage, QPainter, QPaintEvent
from PyQt6.QtWidgets import QApplication, QSplitter, QWidget

import pytest
from pytestqt.qtbot import QtBot

from src.shared.ui.panel_splitter import PANEL_RULE_WIDTH, PanelSplitter
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
    assert splitter.handleWidth() == PANEL_RULE_WIDTH
    image = _render(handle, dpr)
    if orientation == Qt.Orientation.Horizontal:
        pixels = [image.pixelColor(x, image.height() // 2).name() for x in range(image.width())]
    else:
        pixels = [image.pixelColor(image.width() // 2, y).name() for y in range(image.height())]
    assert pixels.count(C.PANEL_DIVIDER) == dpr


def test_old_saved_splitter_state_cannot_restore_wide_gutter(qapp, qtbot) -> None:
    old = QSplitter(Qt.Orientation.Horizontal)
    qtbot.addWidget(old)
    old.addWidget(QWidget())
    old.addWidget(QWidget())
    old.setHandleWidth(8)
    old.setSizes([140, 260])
    splitter = PanelSplitter(Qt.Orientation.Horizontal)
    qtbot.addWidget(splitter)
    splitter.addWidget(QWidget())
    splitter.addWidget(QWidget())
    assert splitter.restoreState(old.saveState())
    assert splitter.handleWidth() == PANEL_RULE_WIDTH
    assert splitter.sizes()[0] < splitter.sizes()[1]


def test_opt_in_resize_grip_adds_visible_drag_target(qapp: QApplication, qtbot: QtBot) -> None:
    splitter = PanelSplitter(Qt.Orientation.Horizontal)
    qtbot.addWidget(splitter)
    splitter.setStyleSheet(DARK_THEME)
    splitter.addWidget(QWidget())
    splitter.addWidget(QWidget())
    splitter.resize(400, 300)
    splitter.show()
    qapp.processEvents()
    handle = splitter.handle(1)
    old_width = handle.width()
    old_state = splitter.saveState()

    splitter.set_resize_grip(True)
    assert splitter.restoreState(old_state)
    qapp.processEvents()
    assert splitter.handleWidth() > PANEL_RULE_WIDTH
    assert handle.width() > old_width
    assert handle.cursor().shape() == Qt.CursorShape.SplitHCursor

    image = _render(handle)
    center = handle.contentsRect().center()
    assert image.pixelColor(center).name() == C.TEXT_HINT
    assert image.pixelColor(center.x(), center.y() - 14).name() == C.PANEL_DIVIDER
    assert image.pixelColor(center.x() - 3, center.y()).name() == C.BG_PANEL

    before = splitter.sizes()
    press = QPoint(1, handle.height() // 2)
    assert splitter.childAt(handle.mapTo(splitter, press)) is handle
    qtbot.mousePress(handle, Qt.MouseButton.LeftButton, pos=press)
    qtbot.mouseMove(handle, pos=press + QPoint(30, 0))
    qtbot.mouseRelease(handle, Qt.MouseButton.LeftButton, pos=press + QPoint(30, 0))
    assert splitter.sizes()[0] > before[0]

    splitter.set_resize_grip(False)
    assert splitter.handleWidth() == PANEL_RULE_WIDTH


def test_composite_exposes_only_its_layers_splitter_grip(qapp: QApplication, qtbot: QtBot) -> None:
    from src.features.composite.ui.keyframes_model import KeyframesModel
    from src.features.composite.ui.keyframes_panel import KeyframesPanel

    panel = KeyframesPanel()
    qtbot.addWidget(panel)
    other = PanelSplitter()
    qtbot.addWidget(other)
    assert panel._splitter.handleWidth() == PANEL_RULE_WIDTH

    panel.set_layer_view_mode(KeyframesModel.VIEW_MODE_LAYERS)
    assert panel._splitter.handleWidth() > PANEL_RULE_WIDTH
    assert panel._right_splitter.handleWidth() == other.handleWidth() == PANEL_RULE_WIDTH

    panel.set_layer_view_mode(KeyframesModel.VIEW_MODE_NODES)
    assert panel._splitter.handleWidth() == PANEL_RULE_WIDTH


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
    outer.setSizes([200, 200])
    outer.show()
    qapp.processEvents()
    horizontal = outer.handle(1)
    vertical = inner.handle(1)

    def assert_join() -> None:
        image = _render(outer)
        x = vertical.mapTo(outer, vertical.contentsRect().topLeft()).x()
        y = horizontal.mapTo(outer, horizontal.contentsRect().topLeft()).y()
        assert all(image.pixelColor(x, row).name() == C.PANEL_DIVIDER for row in range(y + 1))
        assert all(image.pixelColor(col, y).name() == C.PANEL_DIVIDER for col in range(outer.width()))
        assert image.pixelColor(x - 2, 0).name() != C.PANEL_DIVIDER

    assert_join()
    before = inner.sizes()
    # Press near the edge of the hit area, several pixels off the visible rule.
    point = QPoint(0, vertical.height() // 2)
    assert not vertical.contentsRect().contains(point)
    assert outer.childAt(vertical.mapTo(outer, point)) is vertical
    qtbot.mousePress(vertical, Qt.MouseButton.LeftButton, pos=point)
    qtbot.mouseMove(vertical, pos=point + QPoint(40, 0))
    qtbot.mouseRelease(vertical, Qt.MouseButton.LeftButton, pos=point + QPoint(40, 0))
    qapp.processEvents()
    assert inner.sizes() != before
    assert_join()
    inner.setSizes([120, 372])
    qapp.processEvents()
    assert_join()


class _PaintedPane(QWidget):
    """Distinct real surfaces reveal any parent background around a seam."""

    HEADER_HEIGHT = 24

    def __init__(self, body: str, header: str) -> None:
        super().__init__()
        self.setMinimumSize(64, 48)
        self.body_color = QColor(body)
        self.header_color = QColor(header)

    def paintEvent(self, event: QPaintEvent) -> None:
        del event
        painter = QPainter(self)
        painter.fillRect(self.rect(), self.body_color)
        painter.fillRect(0, 0, self.width(), self.HEADER_HEIGHT - 1, self.header_color)
        painter.fillRect(0, self.HEADER_HEIGHT - 1, self.width(), 1, QColor(C.PANEL_DIVIDER))


def _assert_surfaces_meet_rules(root: QWidget, panes: list[_PaintedPane], dpr: int) -> None:
    """Inspect complete seam/header neighborhoods in the composed widget.

    Expected colors come from pane surfaces, independent of handle geometry
    or the divider implementation. Every pixel outside the panes must be a
    black boundary, so a transparent wide gutter cannot pass this check.
    """
    image = _render(root, dpr)
    cells = [(pane, QRect(pane.mapTo(root, QPoint()), pane.size())) for pane in panes]
    columns: set[int] = set()
    rows: set[int] = set()
    for pane, rect in cells:
        for edge in (rect.left(), rect.right()):
            columns.update(range(max(0, edge - 2), min(root.width(), edge + 3)))
        for edge in (rect.top(), rect.bottom(), rect.top() + pane.HEADER_HEIGHT - 1):
            rows.update(range(max(0, edge - 2), min(root.height(), edge + 3)))
    points = {(x, y) for x in columns for y in range(root.height())}
    points.update((x, y) for y in rows for x in range(root.width()))
    divider = QColor(C.PANEL_DIVIDER)
    for x, y in points:
        expected = divider
        for pane, rect in cells:
            if rect.contains(x, y):
                local_y = y - rect.top()
                expected = (pane.header_color if local_y < pane.HEADER_HEIGHT - 1
                            else divider if local_y == pane.HEADER_HEIGHT - 1
                            else pane.body_color)
                break
        for dx in range(dpr):
            for dy in range(dpr):
                actual = image.pixelColor(x * dpr + dx, y * dpr + dy)
                assert actual == expected, (
                    f"Surface/seam mismatch at ({x}, {y}) DPR {dpr}: "
                    f"{actual.name()} != {expected.name()}"
                )


@pytest.mark.parametrize("dpr", [1, 2])
@pytest.mark.parametrize("orientation", [Qt.Orientation.Horizontal, Qt.Orientation.Vertical])
def test_pane_and_header_colors_reach_single_pixel_boundary(
    qapp: QApplication, qtbot: QtBot, dpr: int, orientation: Qt.Orientation,
) -> None:
    splitter = PanelSplitter(orientation)
    qtbot.addWidget(splitter)
    splitter.setStyleSheet(DARK_THEME + "QSplitter { background: #692455; }")
    panes = [_PaintedPane("#245749", "#475726"), _PaintedPane("#294b70", "#573b29")]
    for pane in panes:
        splitter.addWidget(pane)
    splitter.resize(420, 300)
    splitter.show()
    qapp.processEvents()
    _assert_surfaces_meet_rules(splitter, panes, dpr)

    splitter.setSizes([103, 317])
    qapp.processEvents()
    _assert_surfaces_meet_rules(splitter, panes, dpr)
    splitter.resize(473, 337)
    qapp.processEvents()
    _assert_surfaces_meet_rules(splitter, panes, dpr)


@pytest.mark.parametrize("dpr", [1, 2])
@pytest.mark.parametrize("orientation", [Qt.Orientation.Horizontal, Qt.Orientation.Vertical])
@pytest.mark.parametrize("cross", [False, True], ids=["T", "cross"])
def test_nested_panel_surfaces_remain_flush_at_junctions(
    qapp: QApplication, qtbot: QtBot, dpr: int, orientation: Qt.Orientation, cross: bool,
) -> None:
    outer = PanelSplitter(orientation)
    qtbot.addWidget(outer)
    outer.setStyleSheet(DARK_THEME + "QSplitter { background: #692455; }")
    perpendicular = (Qt.Orientation.Vertical if orientation == Qt.Orientation.Horizontal
                     else Qt.Orientation.Horizontal)
    first = PanelSplitter(perpendicular)
    colors = [("#245749", "#475726"), ("#294b70", "#573b29"),
              ("#654769", "#306264"), ("#715347", "#3e436a")]
    panes = [_PaintedPane(*pair) for pair in colors[:4 if cross else 3]]
    for pane in panes[:2]:
        first.addWidget(pane)
    outer.addWidget(first)
    second = PanelSplitter(perpendicular) if cross else panes[2]
    if cross:
        for pane in panes[2:]:
            second.addWidget(pane)
    outer.addWidget(second)
    outer.resize(420, 300)
    outer.show()
    qapp.processEvents()
    _assert_surfaces_meet_rules(outer, panes, dpr)

    # Align the two branches for an X, then stagger them to exercise two T
    # junctions. Reallocation and window resize must not leave the old seam.
    first.setSizes([123, 177])
    if cross:
        second.setSizes([123, 177])
    qapp.processEvents()
    _assert_surfaces_meet_rules(outer, panes, dpr)
    if cross:
        second.setSizes([187, 113])
    outer.setSizes([137, 283])
    outer.resize(473, 337)
    qapp.processEvents()
    _assert_surfaces_meet_rules(outer, panes, dpr)
