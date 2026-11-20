"""Platform-neutral styling for QMainWindow's tabified dock tabs.

Qt creates the tab bar used by ``QMainWindow.tabifyDockWidget`` internally.
That bar is a ``QMainWindowTabBar``.  Its tab painting is owned explicitly by
the application QSS below; the implementation deliberately does not attach a
Python ``QProxyStyle`` to this private Qt widget because Qt may recreate it
during layout restoration, which is unsafe for native application startup.
"""

from __future__ import annotations

from PyQt6.QtWidgets import QTabBar

from src.shared.ui.panel_divider import install_tab_divider_overlay
from src.shared.ui.style_sheet import orange_tab_style

__all__ = [
    "DOCK_TAB_BAR_STYLE",
    "apply_dock_tab_bar_style",
]


DOCK_TAB_BAR_STYLE = (
    orange_tab_style("QMainWindowTabBar#ToposDockTabBar")
    + orange_tab_style("QTabBar#ToposDockTabBar")
)


def apply_dock_tab_bar_style(tab_bar: QTabBar) -> None:
    """Apply the shared dock-tab geometry and style to one internal tab bar."""

    tab_bar.setObjectName("ToposDockTabBar")
    tab_bar.setExpanding(False)
    tab_bar.setDrawBase(False)
    tab_bar.setDocumentMode(False)
    if not tab_bar.property("_topos_dock_tab_style_applied"):
        tab_bar.setStyleSheet(DOCK_TAB_BAR_STYLE)
        tab_bar.setProperty("_topos_dock_tab_style_applied", True)
    tab_bar.updateGeometry()
    tab_bar.update()
    install_tab_divider_overlay(tab_bar)
