"""Regression tests for the shared dock context-menu actions."""
from __future__ import annotations

from PyQt6.QtWidgets import QDockWidget, QMenu

from src.shared.i18n.i18n import I18N
from src.shared.ui.dock_context_menu import add_dock_management_actions


def test_shared_dock_actions_have_standard_labels_and_close_behavior(qapp):
    dock = QDockWidget("Media Pool")
    dock.setVisible(True)
    menu = QMenu()

    add_dock_management_actions(menu, dock)

    expected_float_label = (
        I18N.tr("dock.dock", "Dock")
        if dock.isFloating()
        else I18N.tr("dock.float", "Float")
    )
    assert [action.text() for action in menu.actions()] == [
        expected_float_label,
        I18N.tr("dock.close", "Close"),
    ]

    menu.actions()[1].trigger()
    assert not dock.isVisible()

    dock.deleteLater()
    menu.deleteLater()
