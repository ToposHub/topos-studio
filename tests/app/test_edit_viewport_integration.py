"""Edit Viewport host entry points and legacy dock visibility migration."""

from types import SimpleNamespace
from unittest.mock import MagicMock

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QDockWidget, QMainWindow, QWidget

from src.app.controllers.dock_layout_controller import DockLayoutMixin
from src.app.controllers.file_controller import FileOpsMixin
from src.features.edit.module import EditModule
from src.shared.panels.registry import PanelRegistry


def test_edit_registers_a_single_viewport_panel() -> None:
    registry = PanelRegistry()
    EditModule.register_panels(SimpleNamespace(_available=True), registry)
    viewport = registry.get("edit.viewport")
    assert viewport.fallback_title == "Viewport"
    assert viewport.dock_attr == "dock_program_viewer"
    assert registry.get("edit.source_viewer") is None
    assert registry.get("edit.program_viewer") is None


def test_asset_double_click_opens_source_in_unified_viewport() -> None:
    asset = SimpleNamespace(asset_id="camera", display_name="Camera A.mov")
    viewport = SimpleNamespace(show_source=MagicMock())
    dock = SimpleNamespace(show=MagicMock(), raise_=MagicMock())
    source = SimpleNamespace(open_asset=MagicMock(), viewer=SimpleNamespace(setFocus=MagicMock()))
    host = SimpleNamespace(
        _current_edit_project=SimpleNamespace(assets={"camera": asset}),
        current_page="Edit", source_viewer_widget=source,
        edit_viewport=viewport, dock_program_viewer=dock,
        dock_source_viewer=None,
    )
    FileOpsMixin._on_asset_double_clicked(host, "camera")
    source.open_asset.assert_called_once_with(asset)
    viewport.show_source.assert_called_once_with(focus=True)
    dock.show.assert_called_once_with()
    dock.raise_.assert_called_once_with()


def test_quick_preview_keeps_current_monitor_and_focus() -> None:
    asset = SimpleNamespace(asset_id="camera", display_name="Camera A.mov")
    viewport = SimpleNamespace(show_source=MagicMock())
    source = SimpleNamespace(open_asset=MagicMock(), current_asset=None)
    host = SimpleNamespace(
        _current_edit_project=SimpleNamespace(assets={"camera": asset}),
        source_viewer_widget=source, edit_viewport=viewport,
    )
    FileOpsMixin._on_asset_selected(host, "camera")
    source.open_asset.assert_called_once_with(asset)
    viewport.show_source.assert_not_called()


def test_source_only_legacy_layout_reveals_viewport_once(qapp) -> None:
    legacy = QMainWindow()
    for name, hidden in (("dockProgramViewer", True), ("dockSourceViewer", False)):
        dock = QDockWidget(name, legacy)
        dock.setObjectName(name)
        dock.setWidget(QWidget(dock))
        legacy.addDockWidget(Qt.DockWidgetArea.TopDockWidgetArea, dock)
        dock.setVisible(not hidden)
    state = legacy.saveState()

    window = QMainWindow()
    viewport = QWidget()
    dock = QDockWidget("Viewport", window)
    dock.setObjectName("dockProgramViewer")
    dock.setWidget(viewport)
    window.addDockWidget(Qt.DockWidgetArea.TopDockWidgetArea, dock)
    assert window.restoreState(state)
    assert dock.isHidden()

    data: dict[str, object] = {}
    config = SimpleNamespace(
        get=data.get, set=lambda key, value, auto_save=True: data.__setitem__(key, value),
        save_config=MagicMock(),
    )
    host = SimpleNamespace(
        current_page="Edit", edit_viewport=viewport, dock_program_viewer=dock,
        config_manager=config, page_layouts={}, saveState=window.saveState,
        _current_product_mode=lambda: "studio", _current_work_mode=lambda: "video",
        _save_current_layout_to_store=MagicMock(),
    )
    DockLayoutMixin._migrate_edit_viewport_visibility(host)
    assert not dock.isHidden()
    assert dock.widget() is viewport
    assert not host.page_layouts["Edit"].isEmpty()
    dock.hide()
    DockLayoutMixin._migrate_edit_viewport_visibility(host)
    assert dock.isHidden()
    config.save_config.assert_called_once_with()
    legacy.close()
    window.close()
