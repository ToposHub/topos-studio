"""Shared actions for QDockWidget context menus.

Keeping these actions in one place prevents panel-specific context menus from
drifting away from the standard dock menu used by the main window.
"""
from __future__ import annotations

from collections.abc import Callable
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


def add_dock_management_actions(
    menu: QMenu, dock: QDockWidget, *, toggle_floating: Callable[[], None] | None = None,
) -> None:
    """Append workspace actions; only explicitly floatable hosts can opt in to Float."""
    if dock.isFloating():
        dock_action = menu.addAction(I18N.tr("dock.dock", "Dock"))
        dock_action.triggered.connect(toggle_floating or (lambda: dock.setFloating(False)))
    elif toggle_floating is not None and dock.features() & QDockWidget.DockWidgetFeature.DockWidgetFloatable:
        menu.addAction(I18N.tr("dock.float", "Float"), toggle_floating)

    if dock.features() & QDockWidget.DockWidgetFeature.DockWidgetClosable:
        close_action = menu.addAction(I18N.tr("dock.close", "Close"))
        close_action.triggered.connect(lambda: dock.setVisible(False))
