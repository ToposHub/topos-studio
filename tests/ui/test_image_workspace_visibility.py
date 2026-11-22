"""Image navigation and all panel entry points share the workflow filter."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import numpy as np
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QDockWidget, QMainWindow, QMenu, QWidget

from src.app.controllers.dock_layout_controller import DockLayoutMixin
from src.app.controllers.menu_toolbar_controller import MenuToolbarMixin
from src.app.controllers.deliver_controller import DeliverControllerMixin
from src.app.main_window import MainWindow
from src.app.ui.workspace_panel_bar import WorkspacePanelBar
from src.features.color.module import ColorModule
from src.features.deliver.module import DeliverModule
from src.shared.feature_flags import get_workspace_work_mode, load_feature_flags
from src.shared.panels import PanelRegistry


class Config(dict):
    def set(self, key: str, value: object, *, auto_save: bool = True) -> None:
        self[key] = value


VIDEO_DOCKS = ("dock_tracker", "dock_color_keyframes", "dock_color_timeline_clips",
               "dock_audio_mixer", "dock_deliver", "dockVideoBatchExport")
IMAGE_DOCKS = ("dock_export", "dock_grading", "dock_curves", "dock_masks", "dock_qualifier",
               "dock_analysis", "dock_mixer", "dock_grain", "dock_denoise", "dock_deblur",
               "dock_crop", "dock_viewport", "dock_layers", "dock_lut", "dock_plugin")


class Window(QMainWindow, DockLayoutMixin, MenuToolbarMixin):
    def __init__(self, mode: str) -> None:
        super().__init__()
        self.config_manager = Config(work_mode=mode)
        self._active_work_mode = get_workspace_work_mode(self.config_manager)
        self.feature_flags = load_feature_flags(self.config_manager)
        self.current_page = "Color"
        self.deliver_page = None
        self.all_docks = []
        self.setCentralWidget(QWidget())
        for attr in (*VIDEO_DOCKS, *IMAGE_DOCKS):
            dock = QDockWidget(attr, self)
            dock.setObjectName(attr)
            dock.setWidget(QWidget())
            self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, dock)
            setattr(self, attr, dock)
            self.all_docks.append(dock)


@pytest.fixture
def registry(monkeypatch):
    registry = PanelRegistry()
    ColorModule().register_panels(registry)
    DeliverModule().register_panels(registry)
    monkeypatch.setattr("src.shared.panels.get_panel_registry", lambda: registry)
    monkeypatch.setattr("src.app.controllers.dock_layout_controller.get_panel_registry", lambda: registry)
    monkeypatch.setattr("src.app.ui.workspace_panel_bar.get_panel_registry", lambda: registry)
    return registry


def menu_actions(menu: QMenu) -> list:
    return [item for action in menu.actions()
            for item in (menu_actions(action.menu()) if action.menu() else [action])]


@pytest.mark.parametrize("product", ["studio", "color"])
def test_image_menu_toolbar_and_saved_layout_cannot_reveal_video_tools(qtbot, monkeypatch, registry, product):
    monkeypatch.setenv("TOPOS_PRODUCT_MODE", product)
    window = Window("image")
    qtbot.addWidget(window)
    # Simulate a legacy Qt capture in which all video tools were open.
    state = window.saveState()
    window._enforce_work_mode_dock_visibility()
    window.restoreState(state)
    window._enforce_work_mode_dock_visibility()
    for name in VIDEO_DOCKS:
        dock = getattr(window, name)
        assert dock.isHidden()
        assert not dock.toggleViewAction().isEnabled()
        assert not dock.toggleViewAction().isVisible()

    menu = QMenu(window)
    window._build_panels_menu(menu)
    listed = menu_actions(menu)
    assert not any(getattr(window, name).toggleViewAction() in listed for name in VIDEO_DOCKS)
    assert listed.count(window.dock_export.toggleViewAction()) == 1
    assert window.dock_denoise.toggleViewAction() in listed
    bar = WorkspacePanelBar(window)
    bar.set_page("Color")
    assert window.dock_tracker not in bar._color_tools
    assert window.dock_denoise in bar._color_tools
    assert window.dock_mixer in bar._color_tools
    assert window.dock_grain in bar._color_tools


def test_image_deliver_only_shows_batch_even_after_delayed_migration(qtbot, monkeypatch, registry):
    monkeypatch.setenv("TOPOS_PRODUCT_MODE", "studio")
    window = Window("image")
    qtbot.addWidget(window)
    window.current_page = "Deliver"
    window._enforce_page_dock_visibility()
    window._migrate_deliver_layout()
    assert not window.dock_export.isHidden()
    assert all(d.isHidden() for d in window.all_docks if d is not window.dock_export)
    assert not window.feature_flags.composite
    window._deliver_available = True
    window.switch_to_page = Mock()
    assert DeliverControllerMixin._goto_deliver_page(window)
    window.switch_to_page.assert_called_once_with("Deliver")


def test_video_keeps_tracker_and_deliver_workspace_entry(qtbot, monkeypatch, registry):
    monkeypatch.setenv("TOPOS_PRODUCT_MODE", "studio")
    window = Window("video")
    qtbot.addWidget(window)
    window._enforce_work_mode_dock_visibility()
    assert window.feature_flags.composite
    menu = QMenu(window)
    window._build_panels_menu(menu)
    listed = menu_actions(menu)
    assert window.dock_tracker.toggleViewAction() in listed
    assert window.dock_deliver.toggleViewAction() in listed
    assert window.dock_export.toggleViewAction() not in listed
    assert window.dock_tracker in WorkspacePanelBar(window)._color_tools


def test_image_registry_does_not_construct_video_deliver_workspace(monkeypatch):
    monkeypatch.setenv("TOPOS_PRODUCT_MODE", "studio")
    factory = Mock(side_effect=AssertionError("Video workspace must not be constructed"))
    host = SimpleNamespace(config_manager=Config(work_mode="image"), deliver_page=None,
                           feature_registry=SimpleNamespace(get_all_pages=lambda: [
                               SimpleNamespace(page_id="deliver", create_page=factory)]))
    assert MainWindow._create_pages_from_registry(host)
    factory.assert_not_called()


def test_image_mask_tools_do_not_offer_tracking(qtbot):
    from src.features.color.ui.mask_panel import MaskPanel

    panel = MaskPanel()
    qtbot.addWidget(panel)
    panel.set_image_only_tools_visible(True)
    assert panel.btn_send_tracker.isHidden()
    assert panel.video_panel.isHidden()
    assert not panel.image_panel.isHidden()
    panel.set_image_only_tools_visible(False)
    assert not panel.video_panel.isHidden()


def test_image_deliver_hydrates_batch_resize_without_video_workspace(qtbot, tmp_path):
    from src.app.controllers.page_controller import PageManagementMixin
    from src.shared.export.export_panel import ExportPanel
    from src.shared.i18n.i18n import I18N

    panel = ExportPanel(image_mode=True)
    qtbot.addWidget(panel)
    source = tmp_path / "photo.png"
    host = SimpleNamespace(feature_registry=None, _deliver_available=True,
                           deliver_page=None, export_panel=panel,
                           engine=SimpleNamespace(img_full_original=np.zeros((100, 200, 3))),
                           current_file_path=str(source))
    PageManagementMixin.enter_page_hook(host, "Deliver")
    assert (panel.orig_w, panel.orig_h) == (200, 100)
    panel.resize_check.setChecked(True)
    panel.resize_mode.setCurrentText(I18N.tr("export.resize_width"))
    panel.resize_value.setValue(100)
    assert panel.get_settings()["options"]["resize"] == (100, 50)
    assert panel.composite_options.isHidden()
    panel._reset_output_section()
    assert not panel.get_settings()["apply_composite"]


def test_image_quick_export_hides_video_tab_and_composite_source(qtbot):
    from src.shared.export.quick_export_dialog import QuickExportDialog

    dialog = QuickExportDialog(work_mode="image", has_composite_output=True,
                               timeline_info={"has_timeline": True})
    qtbot.addWidget(dialog)
    assert not dialog.tab_bar.isTabVisible(2)
    assert dialog.source_combo.count() == 1
    assert dialog.source_combo.currentIndex() == 0
