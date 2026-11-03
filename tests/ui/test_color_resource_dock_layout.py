"""Upgrade default resource tabs once, preserving subsequent user layouts."""
from __future__ import annotations
from typing import Any
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QDockWidget, QMainWindow, QWidget
from src.app.controllers.dock_layout_controller import DockLayoutMixin


class _Config:
    def __init__(self) -> None:
        self.values: dict[str, Any] = {}

    def get(self, key: str) -> Any:
        return self.values.get(key)

    def set(self, key: str, value: Any, auto_save: bool = True) -> None:
        self.values[key] = value

    def save_config(self) -> None:
        pass


class _Window(QMainWindow):
    _migrate_color_resource_dock_layout = DockLayoutMixin._migrate_color_resource_dock_layout

    def __init__(self) -> None:
        super().__init__()
        self.current_page = 'Color'
        self.config_manager = _Config()
        self.page_layouts: dict[str, Any] = {}
        self.saved: list[str] = []
        for name in ('dock_presets', 'dock_layers', 'dock_lut', 'dock_plugin'):
            dock = QDockWidget(name, self)
            dock.setObjectName(name)
            dock.setWidget(QWidget())
            setattr(self, name, dock)
            self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, dock)
        self.tabifyDockWidget(self.dock_layers, self.dock_plugin)

    def _save_current_layout_to_store(self, page: str) -> None:
        self.saved.append(page)


def test_upgrade_separates_nodes_and_keeps_later_custom_layout(qtbot: Any) -> None:
    host = _Window()
    qtbot.addWidget(host)
    host._migrate_color_resource_dock_layout()
    assert host.dockWidgetArea(host.dock_plugin) == Qt.DockWidgetArea.LeftDockWidgetArea
    assert host.dock_plugin in host.tabifiedDockWidgets(host.dock_presets)
    assert host.dock_lut in host.tabifiedDockWidgets(host.dock_presets)
    assert host.dock_plugin not in host.tabifiedDockWidgets(host.dock_layers)
    host.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, host.dock_plugin)
    host._migrate_color_resource_dock_layout()
    assert host.dockWidgetArea(host.dock_plugin) == Qt.DockWidgetArea.RightDockWidgetArea
    assert host.saved == ['Color']


def test_upgrade_preserves_floating_but_reset_restores_default(qtbot: Any) -> None:
    host = _Window()
    qtbot.addWidget(host)
    host.dock_plugin.setFloating(True)
    host._migrate_color_resource_dock_layout()
    assert host.dock_plugin.isFloating()
    host._migrate_color_resource_dock_layout(force=True)
    assert not host.dock_plugin.isFloating()
    assert host.dock_plugin in host.tabifiedDockWidgets(host.dock_presets)
