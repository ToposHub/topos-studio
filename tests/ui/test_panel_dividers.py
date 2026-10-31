"""Regression tests for the cross-platform dock panel divider style."""

import re
import sys

import pytest

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QImage
from PyQt6.QtWidgets import QDockWidget, QMainWindow, QStyleFactory, QTabBar, QWidget

from src.shared.ui.style_sheet import C, DARK_THEME
from src.shared.ui.dock_tab_style import apply_dock_tab_bar_style
from src.shared.ui.panel_divider import PanelDockWidget


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


def test_dock_divider_is_a_fixed_custom_one_pixel_line() -> None:
    divider_style = _style_block("QMainWindow::separator")
    hover_style = _style_block("QMainWindow::separator", state=":hover")

    assert f"background-color: {C.PANEL_DIVIDER}" in divider_style
    assert "width: 1px" in divider_style
    assert "height: 1px" in divider_style
    assert "border: none" in divider_style
    assert f"background-color: {C.PANEL_DIVIDER}" in hover_style
    assert C.ORANGE_PRESS not in hover_style


def test_nested_splitter_uses_the_same_custom_divider() -> None:
    splitter_style = _style_block("QSplitter::handle")

    assert f"background-color: {C.PANEL_DIVIDER}" in splitter_style
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
