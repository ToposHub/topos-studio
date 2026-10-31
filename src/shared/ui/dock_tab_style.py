"""Platform-neutral styling for QMainWindow's tabified dock tabs.

Qt creates the tab bar used by ``QMainWindow.tabifyDockWidget`` internally.
That bar is a ``QMainWindowTabBar``.  Its tab painting is owned explicitly by
the application QSS below; the implementation deliberately does not attach a
Python ``QProxyStyle`` to this private Qt widget because Qt may recreate it
during layout restoration, which is unsafe for native application startup.
"""

from __future__ import annotations

from PyQt6.QtWidgets import QTabBar

__all__ = [
    "DOCK_TAB_BAR_STYLE",
    "apply_dock_tab_bar_style",
]


DOCK_TAB_BAR_STYLE = """
QMainWindowTabBar#ToposDockTabBar, QTabBar#ToposDockTabBar {
    background: transparent;
    border: none;
}

QMainWindowTabBar#ToposDockTabBar::tab,
QTabBar#ToposDockTabBar::tab {
    background: rgb(70, 70, 77);
    color: #bbbbbb;
    padding: 3px 14px;
    border: none;
    margin: 0;
    min-height: 18px;
}

QMainWindowTabBar#ToposDockTabBar::tab:hover,
QTabBar#ToposDockTabBar::tab:hover {
    background: rgb(85, 85, 91);
    color: #bbbbbb;
}

QMainWindowTabBar#ToposDockTabBar::tab:selected,
QTabBar#ToposDockTabBar::tab:selected {
    background: rgb(204, 102, 0);
    color: #ffffff;
}

QMainWindowTabBar#ToposDockTabBar::tab:first,
QTabBar#ToposDockTabBar::tab:first {
    border-top-left-radius: 8px;
    border-bottom-left-radius: 8px;
}

QMainWindowTabBar#ToposDockTabBar::tab:last,
QTabBar#ToposDockTabBar::tab:last {
    border-top-right-radius: 8px;
    border-bottom-right-radius: 8px;
}

QMainWindowTabBar#ToposDockTabBar::tab:only-one,
QTabBar#ToposDockTabBar::tab:only-one {
    border-radius: 8px;
}
"""


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
