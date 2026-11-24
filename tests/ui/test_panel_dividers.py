"""Regression tests for the cross-platform dock panel divider style."""

import re
import sys

import pytest

from PyQt6.QtCore import QEvent, QPoint, QPointF, QRect, Qt
from PyQt6.QtGui import QColor, QImage, QMouseEvent, QPainter, QPaintEvent
from PyQt6.QtWidgets import QApplication, QDockWidget, QMainWindow, QStyle, QStyleFactory, QTabBar, QTabWidget, QWidget

from src.shared.ui.style_sheet import C, DARK_THEME
from src.shared.ui.dock_tab_style import apply_dock_tab_bar_style
from src.shared.ui.panel_divider import PanelDockWidget, _divider_segments, _dock_rectangles, paint_shared_dock_dividers


def _style_block(selector: str, *, state: str = "") -> str:
    match = re.search(
        rf"{re.escape(selector)}{re.escape(state)}\s*\{{(?P<body>[^}}]*)\}}",
        DARK_THEME,
    )
    assert match is not None, f"Missing stylesheet selector: {selector}{state}"
    return match.group("body")


def test_dock_panels_disable_platform_borders_for_custom_painting() -> None:
    dock_style = _style_block("QDockWidget")

    assert "border: none" in dock_style


def test_dock_divider_reserves_only_the_visible_boundary() -> None:
    divider_style = _style_block("QMainWindow::separator")

    assert "background-color: transparent" in divider_style
    assert "width: 1px" in divider_style
    assert "height: 1px" in divider_style
    assert "border: none" in divider_style
    assert C.ORANGE_PRESS not in divider_style


def test_nested_splitter_keeps_the_resize_target_transparent() -> None:
    splitter_style = _style_block("QSplitter::handle")

    assert "background-color: transparent" in splitter_style
    assert "border: none" in splitter_style


def test_panel_dock_paints_the_custom_divider_overlay(qapp) -> None:
    host = QMainWindow()
    host.resize(240, 160)
    dock = PanelDockWidget("Panel", host)
    dock.setWidget(QWidget())
    host.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, dock)
    host.show()
    qapp.processEvents()

    image = dock.grab().toImage().convertToFormat(QImage.Format.Format_RGB32)
    divider = (13, 13, 15)
    divider_pixels = sum(
        1
        for y in range(image.height())
        for x in range(image.width())
        if image.pixelColor(x, y).getRgb()[:3] == divider
    )

    assert dock._divider_overlay.isVisible()
    assert divider_pixels >= max(2, (image.width() + image.height()) // 2)
    host.close()


@pytest.mark.parametrize("dpr", [1, 2])
@pytest.mark.parametrize("orientation", [Qt.Orientation.Horizontal, Qt.Orientation.Vertical])
def test_dock_surfaces_reach_the_rule_and_keep_native_edge_drag(
    qapp, qtbot, dpr: int, orientation: Qt.Orientation,
) -> None:
    class Window(QMainWindow):
        def paintEvent(self, event: QPaintEvent) -> None:
            super().paintEvent(event)
            painter = QPainter(self)
            paint_shared_dock_dividers(self, painter)
            painter.end()

    host = Window()
    qtbot.addWidget(host)
    host.resize(600, 400)
    host.setStyleSheet(DARK_THEME)
    colors = ("#433145", "#244c48")
    docks = []
    for color in colors:
        dock = PanelDockWidget("Panel", host)
        content = QWidget()
        content.setMinimumSize(80, 80)
        content.setStyleSheet(f"background: {color};")
        dock.setWidget(content)
        title = QWidget()
        title.setFixedHeight(0)
        dock.setTitleBarWidget(title)
        host.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, dock)
        docks.append(dock)
    host.splitDockWidget(*docks, orientation)
    host.show()
    qapp.processEvents()
    assert host.style().pixelMetric(QStyle.PixelMetric.PM_DockWidgetSeparatorExtent, None, host) == 1

    def check_surfaces() -> QPoint:
        first, second = [dock.geometry() for dock in docks]
        image = QImage(host.width() * dpr, host.height() * dpr, QImage.Format.Format_RGB32)
        image.setDevicePixelRatio(dpr)
        image.fill(QColor("#ee00ee"))
        painter = QPainter(image)
        host.render(painter)
        painter.end()
        if orientation == Qt.Orientation.Horizontal:
            assert second.left() - first.right() == 2
            seam = QPoint(first.right() + 1, first.center().y())
            offsets = (QPoint(-1, 0), QPoint(), QPoint(1, 0))
        else:
            assert second.top() - first.bottom() == 2
            seam = QPoint(first.center().x(), first.bottom() + 1)
            offsets = (QPoint(0, -1), QPoint(), QPoint(0, 1))
        for offset, color in zip(offsets, (colors[0], C.PANEL_DIVIDER, colors[1])):
            pixel = (seam + offset) * dpr
            assert image.pixelColor(pixel).name() == color
        return seam

    seam = check_surfaces()
    # Native Qt dock separators extend two pixels into each adjacent pane.
    point = seam + (QPoint(2, 0) if orientation == Qt.Orientation.Horizontal else QPoint(0, 2))
    target = host.childAt(point)
    assert target is host.childAt(seam)
    assert target not in (docks[0], docks[1], docks[0].widget(), docks[1].widget())
    before = docks[0].size()
    local = target.mapFrom(host, point)
    delta = QPoint(35, 0) if orientation == Qt.Orientation.Horizontal else QPoint(0, 35)
    qtbot.mousePress(target, Qt.MouseButton.LeftButton, pos=local)
    move = QMouseEvent(
        QEvent.Type.MouseMove, QPointF(local + delta),
        QPointF(target.mapToGlobal(local + delta)), Qt.MouseButton.NoButton,
        Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
    )
    QApplication.sendEvent(target, move)
    qapp.processEvents()
    qtbot.mouseRelease(target, Qt.MouseButton.LeftButton, pos=local + delta)
    qapp.processEvents()
    assert docks[0].size() != before
    check_surfaces()
    host.resize(640, 440)
    qapp.processEvents()
    check_surfaces()


def test_grading_rules_join_both_dock_seams(qapp, qtbot) -> None:
    from src.features.color.ui.color_wheel import GradingPanel

    class Window(QMainWindow):
        def paintEvent(self, event: QPaintEvent) -> None:
            super().paintEvent(event)
            painter = QPainter(self)
            paint_shared_dock_dividers(self, painter)
            painter.end()

    host = Window()
    qtbot.addWidget(host)
    host.setStyleSheet(DARK_THEME)
    grading = GradingPanel()
    left, middle, right = (PanelDockWidget(name, host) for name in ("A", "Grading", "B"))
    for dock, content in ((left, QWidget()), (middle, grading), (right, QWidget())):
        content.setMinimumWidth(80)
        dock.setWidget(content)
        title = QWidget()
        title.setFixedHeight(1)
        dock.setTitleBarWidget(title)
        host.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, dock)
    host.splitDockWidget(left, middle, Qt.Orientation.Horizontal)
    host.splitDockWidget(middle, right, Qt.Orientation.Horizontal)
    host.resize(1060, 400)
    host.show()
    host.resizeDocks([left, middle, right], [180, 700, 180], Qt.Orientation.Horizontal)

    for mode in ("wheels", "bars", "log"):
        grading._mode_buttons[mode].click()
        qapp.processEvents()
        image = host.grab().toImage()
        dpr = image.devicePixelRatio()
        left_seam = middle.geometry().left() - 1
        right_seam = middle.geometry().right() + 1
        header = grading.reset_all_btn.parentWidget()
        rows = [header.mapTo(host, header.rect().bottomLeft()).y()]
        rows.extend(divider.mapTo(host, QPoint()).y()
                    for divider in (grading._h_line_mid, grading._h_line_bottom))
        for y in rows:
            for x in range(left_seam, right_seam + 1):
                assert image.pixelColor(int(x * dpr), int(y * dpr)).name() == C.PANEL_DIVIDER

        y = header.mapTo(host, header.rect().center()).y()
        for x in (left_seam + 1, right_seam - 1):
            assert image.pixelColor(int(x * dpr), int(y * dpr)).name() == "#202024"


@pytest.mark.skipif(
    sys.platform == "darwin",
    reason=(
        "Qt 6.10 on macOS can segfault while re-polishing a tabified dock "
        "after other WebEngine/Metal UI tests; the same geometry contract is "
        "covered on Linux CI in the PR Qt shard."
    ),
)
def test_tabified_docks_use_custom_cross_platform_tab_geometry(qapp) -> None:
    """Dock tabs must not inherit Windows/macOS alignment differences."""
    original_style = qapp.style()
    host = QMainWindow()
    host.resize(425, 120)
    host.setStyleSheet(DARK_THEME)
    first = QDockWidget("Camera Raw", host)
    second = QDockWidget("Color Wheel", host)
    first.setWidget(QWidget())
    second.setWidget(QWidget())
    host.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, first)
    host.tabifyDockWidget(first, second)
    host.show()

    try:
        for style_name in ("macOS", "Windows", "Fusion"):
            # Changing QApplication's global style re-polishes every widget
            # left by earlier UI tests and can block Qt's event loop on macOS.
            # Scope the cross-platform check to this host window instead.
            host.setStyle(QStyleFactory.create(style_name))
            qapp.processEvents()
            tab_bar = host.findChildren(QTabBar)[0]
            apply_dock_tab_bar_style(tab_bar)
            qapp.processEvents()

            assert tab_bar.objectName() == "ToposDockTabBar"
            assert tab_bar.drawBase() is False
            assert tab_bar.expanding() is False
            assert "QMainWindowTabBar#ToposDockTabBar::tab" in tab_bar.styleSheet()
            assert "background: rgb(204, 102, 0)" in tab_bar.styleSheet()
    finally:
        host.setStyle(original_style)
        host.close()


def test_dividers_scan_only_docks_once_per_paint(qapp, qtbot) -> None:
    """Playback repaints must not recursively visit inspector content trees."""
    class RecordingWindow(QMainWindow):
        def __init__(self) -> None:
            super().__init__()
            self.search_options: list[Qt.FindChildOption] = []

        def findChildren(self, *args, **kwargs):
            self.search_options.append(kwargs.get("options"))
            return super().findChildren(*args, **kwargs)

    host = RecordingWindow()
    qtbot.addWidget(host)
    host.resize(500, 240)
    center = QWidget()
    host.setCentralWidget(center)
    docks = []
    for area in (Qt.DockWidgetArea.LeftDockWidgetArea, Qt.DockWidgetArea.RightDockWidgetArea):
        dock = PanelDockWidget("Panel", host)
        content = QWidget()
        dock.setWidget(content)
        host.addDockWidget(area, dock)
        # Descendant docks are not members of the main window's dock layout.
        QDockWidget("Nested inspector", content)
        docks.append(dock)
    host.show()
    qapp.processEvents()

    expected = [QRect(widget.mapTo(host, QPoint()), widget.size()) for widget in [*docks, center]]
    assert _dock_rectangles(host) == expected
    host.search_options.clear()
    docks[0]._divider_overlay.grab()
    assert host.search_options == [Qt.FindChildOption.FindDirectChildrenOnly]
    host.search_options.clear()
    image = QImage(host.size(), QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    paint_shared_dock_dividers(host, painter)
    painter.end()
    assert host.search_options == [Qt.FindChildOption.FindDirectChildrenOnly]

    docks[1].hide()
    qapp.processEvents()
    assert len(_dock_rectangles(host)) == 2
    assert len(_dock_rectangles(host, exclude=docks[0])) == 1


@pytest.mark.parametrize("dpr", [1, 2])
@pytest.mark.parametrize("cross", [False, True])
def test_junctions_are_connected_single_rules(dpr: int, cross: bool) -> None:
    cells = [QRect(0, 0, 100, 100), QRect(108, 0, 100, 100)]
    cells += ([QRect(0, 108, 100, 100), QRect(108, 108, 100, 100)]
              if cross else [QRect(0, 108, 208, 100)])
    image = QImage(208 * dpr, 208 * dpr, QImage.Format.Format_RGB32)
    image.setDevicePixelRatio(dpr)
    image.fill(QColor(C.BG_PANEL))
    painter = QPainter(image)
    segments = _divider_segments(cells, 8)
    assert len(segments) == len(set(segments))
    for orientation, position, start, end in segments:
        rect = (QRect(position, start, 1, end - start + 1)
                if orientation == "vertical"
                else QRect(start, position, end - start + 1, 1))
        painter.fillRect(rect, QColor(C.PANEL_DIVIDER))
    painter.end()
    # No missing pixels along either arm or at the center, at either DPR.
    for x in range(208 * dpr):
        assert image.pixelColor(x, 103 * dpr).name() == C.PANEL_DIVIDER
    for y in range((208 if cross else 104) * dpr):
        assert image.pixelColor(103 * dpr, y).name() == C.PANEL_DIVIDER
    assert sum(image.pixelColor(x, 50 * dpr).name() == C.PANEL_DIVIDER
               for x in range(image.width())) == dpr


def test_staggered_junctions_connect_without_cutting_through_panels() -> None:
    cells = [QRect(0, 0, 100, 100), QRect(108, 0, 140, 100),
             QRect(0, 108, 140, 100), QRect(148, 108, 100, 100)]
    segments = _divider_segments(cells, 8)
    assert ("horizontal", 103, 0, 247) in segments
    assert ("vertical", 103, 0, 103) in segments
    assert ("vertical", 143, 103, 207) in segments
    for orientation, position, start, end in segments:
        strip = (QRect(position, start, 1, end - start + 1)
                 if orientation == "vertical"
                 else QRect(start, position, end - start + 1, 1))
        assert not any(strip.intersects(cell) for cell in cells)


def test_tab_group_includes_tabs_and_ignores_inactive_offscreen_dock(qapp, qtbot) -> None:
    host = QMainWindow()
    qtbot.addWidget(host)
    host.resize(600, 400)
    host.setStyleSheet(DARK_THEME)
    host.setTabPosition(Qt.DockWidgetArea.AllDockWidgetAreas, QTabWidget.TabPosition.North)
    first, active, right = [PanelDockWidget(title, host) for title in ("First", "Active", "Right")]
    for dock in (first, active, right):
        dock.setWidget(QWidget())
        title = QWidget()
        title.setFixedHeight(1)
        dock.setTitleBarWidget(title)
    host.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, first)
    host.tabifyDockWidget(first, active)
    host.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, right)
    host.show()
    active.raise_()
    qapp.processEvents()
    bar = next(bar for bar in host.findChildren(QTabBar) if bar.isVisible())
    apply_dock_tab_bar_style(bar)
    qapp.processEvents()
    rectangles = _dock_rectangles(host)
    assert len(rectangles) == 2
    group = next(rect for rect in rectangles if rect.contains(active.geometry()))
    assert group.contains(bar.geometry())
    assert group.top() == right.geometry().top()
    assert all(rect.intersects(host.rect()) for rect in rectangles)
    # The old content-top border beneath the tab strip must disappear.
    image = active._divider_overlay.grab().toImage()
    assert image.pixelColor(image.width() // 2, 0).alpha() == 0
    # Switching tabs retains the exact same group geometry.
    first.raise_()
    qapp.processEvents()
    assert _dock_rectangles(host) == rectangles


@pytest.mark.parametrize("orientation", [Qt.Orientation.Horizontal, Qt.Orientation.Vertical])
def test_shared_rule_connects_to_outer_outline_and_survives_redocking(qapp, qtbot, orientation: Qt.Orientation) -> None:
    class Window(QMainWindow):
        def paintEvent(self, event: QPaintEvent) -> None:
            super().paintEvent(event)
            painter = QPainter(self)
            paint_shared_dock_dividers(self, painter)
            painter.end()

    host = Window()
    qtbot.addWidget(host)
    host.resize(400, 240)
    host.setStyleSheet(DARK_THEME)
    first, second = [PanelDockWidget(title, host) for title in ("A", "B")]
    for dock in (first, second):
        dock.setWidget(QWidget())
        title = QWidget()
        title.setFixedHeight(1)
        dock.setTitleBarWidget(title)
        host.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, dock)
    host.splitDockWidget(first, second, orientation)
    host.show()
    qapp.processEvents()

    def check_outline() -> None:
        image = host.grab().toImage()
        dpr = image.devicePixelRatio()
        cells = _dock_rectangles(host)
        bounds = cells[0].united(cells[1]).intersected(host.rect())
        # Each outer edge must pass continuously across the shared gutter.
        for x in range(bounds.left(), bounds.right() + 1):
            for y in (bounds.top(), bounds.bottom()):
                assert image.pixelColor(int(x * dpr), int(y * dpr)).name() == C.PANEL_DIVIDER
        for y in range(bounds.top(), bounds.bottom() + 1):
            for x in (bounds.left(), bounds.right()):
                assert image.pixelColor(int(x * dpr), int(y * dpr)).name() == C.PANEL_DIVIDER

    check_outline()
    second.setFloating(True)
    qtbot.wait(30)  # Let Qt finish changing the native window frame.
    assert len(_dock_rectangles(host)) == 1
    second.setFloating(False)
    qtbot.waitUntil(lambda: host.rect().contains(second.geometry()))
    check_outline()
