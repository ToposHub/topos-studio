"""Regression tests for the shared workspace dock context actions."""
from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QDockWidget, QMainWindow, QMenu

from src.app.controllers.dock_layout_controller import restore_workspace_dock_state
from src.shared.i18n.i18n import I18N
from src.shared.ui.dock_context_menu import add_dock_management_actions


def test_docked_panel_context_only_offers_close(qtbot) -> None:
    window = QMainWindow()
    qtbot.addWidget(window)
    dock = QDockWidget("Media Pool", window)
    window.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, dock)
    window.show()
    menu = QMenu(dock)

    add_dock_management_actions(menu, dock)

    assert [action.text() for action in menu.actions()] == [I18N.tr("dock.close", "Close")]
    menu.actions()[0].trigger()
    assert not dock.isVisible()


def test_legacy_workspace_state_restores_disabled_float_as_docked(qtbot) -> None:
    window = QMainWindow()
    qtbot.addWidget(window)
    dock = QDockWidget("Files", window)
    dock.setObjectName("dockFiles")
    window.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, dock)
    dock.setFloating(True)
    state = window.saveState()
    dock.setFloating(False)
    dock.setFeatures(dock.features() & ~QDockWidget.DockWidgetFeature.DockWidgetFloatable)

    assert restore_workspace_dock_state(window, state, "Edit")
    assert not dock.isFloating()
    assert window.dockWidgetArea(dock) == Qt.DockWidgetArea.LeftDockWidgetArea
