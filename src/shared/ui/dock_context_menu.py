"""Shared actions for QDockWidget context menus.

Keeping these actions in one place prevents panel-specific context menus from
drifting away from the standard dock menu used by the main window.
"""
from __future__ import annotations

from typing import Optional

from PyQt6.QtWidgets import QDockWidget, QMenu, QWidget

from src.shared.i18n.i18n import I18N


def find_parent_dock(widget: Optional[QWidget]) -> Optional[QDockWidget]:
    """Return the QDockWidget containing ``widget``, if there is one."""
    parent = widget.parentWidget() if widget is not None else None
    while parent is not None:
        if isinstance(parent, QDockWidget):
            return parent
        parent = parent.parentWidget()
    return None


def add_dock_management_actions(menu: QMenu, dock: QDockWidget) -> None:
    """Append the standard Float/Dock and Close actions to ``menu``."""
    float_action = menu.addAction(
        I18N.tr("dock.float", "Float")
        if not dock.isFloating()
        else I18N.tr("dock.dock", "Dock")
    )
    float_action.triggered.connect(
        lambda: dock.setFloating(not dock.isFloating())
    )

    close_action = menu.addAction(I18N.tr("dock.close", "Close"))
    close_action.triggered.connect(lambda: dock.setVisible(False))
