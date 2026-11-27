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
    "DOCK_TAB_BAR_HEIGHT",
    "apply_dock_tab_bar_style",
]


DOCK_TAB_BAR_HEIGHT = 24
DOCK_TAB_BAR_STYLE = (
    orange_tab_style("QMainWindowTabBar#ToposDockTabBar")
    + orange_tab_style("QTabBar#ToposDockTabBar")
)


_COMPACT_DOCK_TAB_BAR_STYLE = DOCK_TAB_BAR_STYLE.replace("padding: 3px 14px", "padding: 3px 5px")


def apply_dock_tab_bar_style(tab_bar: QTabBar, *, compact: bool = False) -> None:
    """Apply the shared dock-tab geometry and style to one internal tab bar."""

    tab_bar.setObjectName("ToposDockTabBar")
    tab_bar.setFixedHeight(DOCK_TAB_BAR_HEIGHT)
    tab_bar.setExpanding(False)
    tab_bar.setDrawBase(False)
    tab_bar.setDocumentMode(False)
    if tab_bar.property("_topos_dock_tabs_compact") != compact:
        tab_bar.setStyleSheet(_COMPACT_DOCK_TAB_BAR_STYLE if compact else DOCK_TAB_BAR_STYLE)
        tab_bar.setProperty("_topos_dock_tabs_compact", compact)
    if compact:
        for index in range(tab_bar.count()):
            tab_bar.setTabToolTip(index, tab_bar.tabText(index))
    tab_bar.updateGeometry()
    tab_bar.update()
    install_tab_divider_overlay(tab_bar)
