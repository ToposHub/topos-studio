"""Native Mixer page registration, product gates and dock persistence."""
from __future__ import annotations

import os
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QDockWidget, QMainWindow, QWidget

from src.app.controllers import dock_layout_controller as docks_module
from src.app.controllers.dock_layout_controller import DockLayoutMixin, FloatingDockTitleBar
from src.app.controllers.page_controller import PageManagementMixin
from src.app.feature_registry import FeatureRegistry
from src.features.mixer.module import MixerModule, MIXER_PANEL_SPECS
from src.shared.feature_flags import load_feature_flags
from src.shared.layout.store import LayoutStore
from src.shared.panels.registry import PanelRegistry


class MemoryConfig:
    def __init__(self, **values: object) -> None:
        self.values = values

    def get(self, key: str, default: object = None) -> object:
        return self.values.get(key, default)

    def set(self, key: str, value: object, auto_save: bool = True) -> None:
        self.values[key] = value


@pytest.mark.parametrize("mode,work_mode,enabled", [
    ("studio", "video", True), ("studio", "image", False),
    ("color", "video", False), ("edit", "video", False),
    ("composite", "video", False), ("topos-studio", "video", True),
])
def test_mixer_product_and_work_mode_gate(monkeypatch, mode, work_mode, enabled):
    monkeypatch.setenv("TOPOS_PRODUCT_MODE", mode)
    monkeypatch.delenv("TOPOS_FEATURE_MIXER", raising=False)
    monkeypatch.delenv("TOPOS_DISABLE_FEATURES", raising=False)
    assert load_feature_flags(MemoryConfig(work_mode=work_mode)).mixer is enabled


def test_mixer_appears_in_studio_packaging_only():
    from src.products.packaging import get_packaging_config
    for name in ("topos-studio", "topos-color", "topos-edit", "topos-composite"):
        config = get_packaging_config(name)
        assert ("mixer" in config["include_dirs"]) is (name == "topos-studio")


def test_module_factory_is_local_and_reuses_one_session(qtbot, monkeypatch):
    fake_ui = ModuleType("src.features.mixer.ui.mixer_page")

    class LocalPage(QWidget):
        def __init__(self):
            super().__init__()
            self.on_enter = Mock()
            self.on_leave = Mock()

    fake_ui.MixerPage = LocalPage
    monkeypatch.setitem(sys.modules, fake_ui.__name__, fake_ui)
    module = MixerModule()
    registry = FeatureRegistry()
    registry.initialize(SimpleNamespace(save_router=None))
    assert registry.register_module(module)
    descriptor = registry.get_page("mixer")
    page = descriptor.create_page(None)
    qtbot.addWidget(page)
    assert descriptor.create_page(None) is page
    registry.on_page_enter("mixer")
    registry.on_page_leave("mixer")
    page.on_enter.assert_called_once_with()
    page.on_leave.assert_called_once_with()
    # No project save handler or audio-service dependency is registered.
    assert module.register_save_handlers(SimpleNamespace()) is None


def test_four_independent_panel_entries_and_optional_editor():
    registry = PanelRegistry()
    MixerModule().register_panels(registry)
    panels = registry.get_all()
    assert [p.dock_attr for p in panels] == [spec[1] for spec in MIXER_PANEL_SPECS]
    assert all(p.feature_id == "mixer" and p.page_ids == ("Mixer",) for p in panels)
    # Media Pool / Effects 是 project-backed Mixer 的默认可见面板（P0-1）。
    assert [p.panel_id for p in panels if p.default_visible] == [
        "mixer.console", "mixer.viewer", "mixer.timeline",
        "mixer.media_pool", "mixer.effects",
    ]
    assert registry.get_panels_by_category({"mixer"}, work_mode="image") == {}


def test_app_page_factory_leaves_mixer_widgets_to_the_shared_edit_host(qtbot) -> None:
    module = MixerModule()
    host = QWidget()
    qtbot.addWidget(host)
    context = SimpleNamespace(get_main_window=lambda: host)
    page = module.register_pages(context)[0].create_page(context)
    qtbot.addWidget(page)
    assert page.project_backed
    assert not hasattr(page, "session")
    page.on_enter()
    assert page.active
    page.dispose_ui()
    assert not page.active


def test_registry_session_is_parented_to_main_window_without_second_editor(qtbot):
    from src.app.main_window import MainWindow
    from src.app.page_contract import PageDescriptor
    host = QMainWindow()
    qtbot.addWidget(host)
    page = QWidget()
    descriptor = PageDescriptor("mixer", "Mixer", lambda _context: page)
    host.feature_registry = SimpleNamespace(get_all_pages=lambda: [descriptor])
    assert MainWindow._create_pages_from_registry(host)
    assert host.mixer_page is page
    assert page.parent() is host and page.isHidden()
    assert not hasattr(host, "edit_page")


class DockHost(QMainWindow, DockLayoutMixin):
    def __init__(self) -> None:
        super().__init__()
        self.resize(1400, 900)
        self.current_page = "Color"
        self._mixer_available = True
        self._updating_titlebars = False
        self._floating_docks = set()
        self.page_layouts = {}
        self.config_manager = MemoryConfig()
        self.layout_store = LayoutStore(self.config_manager)
        self.mixer_page = SimpleNamespace(**{
            spec[2]: QWidget(self) for spec in MIXER_PANEL_SPECS
        })
        self.legacy = self._factory("Color", QWidget(), "legacyColor")
        self.addDockWidget(Qt.DockWidgetArea.TopDockWidgetArea, self.legacy)
        self._create_audio_workspace_docks(self._factory)
        self.all_docks = [self.legacy, *self._audio_workspace_docks()]

    def _factory(self, title, widget, name):
        dock = QDockWidget(title, self)
        dock.setObjectName(name)
        dock.setWidget(widget)
        dock.setFeatures(QDockWidget.DockWidgetFeature.DockWidgetClosable
                         | QDockWidget.DockWidgetFeature.DockWidgetMovable
                         | QDockWidget.DockWidgetFeature.DockWidgetFloatable)
        return dock

    def _current_work_mode(self):
        return "video"

    def _current_product_mode(self):
        return "studio"

    def _get_enabled_features(self):
        return {"mixer"}

    def activate_mixer(self):
        assert self._seed_audio_workspace_layout()
        assert self.restoreState(self.page_layouts["Mixer"])
        self.current_page = "Mixer"
        self._enforce_page_dock_visibility()


@pytest.fixture
def dock_host(qtbot, monkeypatch):
    registry = PanelRegistry()
    MixerModule().register_panels(registry)
    monkeypatch.setattr(docks_module, "get_panel_registry", lambda: registry)
    host = DockHost()
    qtbot.addWidget(host)
    host.show()
    qtbot.waitUntil(host.isVisible)
    host.activate_mixer()
    return host


def test_native_factory_layout_top_console_video_bottom_timeline(dock_host, qtbot):
    host = dock_host
    qtbot.waitUntil(lambda: host.dock_audio_console.isVisible())
    console, viewer, timeline = host._audio_workspace_docks()[:3]
    assert host.legacy.isHidden()
    assert host.dock_audio_clip_editor.isHidden()
    assert host.dockWidgetArea(console) == Qt.DockWidgetArea.TopDockWidgetArea
    assert host.dockWidgetArea(viewer) == Qt.DockWidgetArea.TopDockWidgetArea
    assert host.dockWidgetArea(timeline) == Qt.DockWidgetArea.BottomDockWidgetArea
    assert console.geometry().right() <= viewer.geometry().left()
    assert console.geometry().bottom() <= timeline.geometry().top()
    assert timeline.width() >= console.width() + viewer.width()
    assert len({d.objectName() for d in host._audio_workspace_docks()}) == 4
    host._perform_update_all_titlebars()
    assert all(isinstance(d.titleBarWidget(), FloatingDockTitleBar) for d in host._audio_workspace_docks())


def test_floating_redocking_and_layoutstore_round_trip(dock_host, qtbot):
    host = dock_host
    viewer = host.dock_audio_viewer
    viewer.setFloating(True)
    qtbot.waitUntil(viewer.isFloating)
    host._save_current_layout_to_store("Mixer")
    profile = host.layout_store.get_profile("studio", "Mixer")
    assert "mixer.viewer" in profile.visible_panels
    viewer.setFloating(False)
    host.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, viewer)
    assert host._restore_layout_from_store("Mixer")
    qtbot.waitUntil(viewer.isFloating)
    host._sync_floating_docks()
    assert viewer in host._floating_docks
    viewer.setFloating(False)
    host.addDockWidget(Qt.DockWidgetArea.TopDockWidgetArea, viewer)
    host._sync_floating_docks()
    assert isinstance(viewer.titleBarWidget(), FloatingDockTitleBar)


def test_clip_editor_tab_or_float_is_restorable_and_opt_in(dock_host, qtbot):
    host = dock_host
    editor = host.dock_audio_clip_editor
    assert editor.isHidden()
    host.tabifyDockWidget(host.dock_audio_timeline, editor)
    editor.show()
    editor.raise_()
    assert editor in host.tabifiedDockWidgets(host.dock_audio_timeline)
    host._save_current_layout_to_store("Mixer")
    editor.setFloating(True)
    editor.hide()
    assert host._restore_layout_from_store("Mixer")
    assert editor in host.tabifiedDockWidgets(host.dock_audio_timeline)
    assert not editor.isHidden()
    host._enforce_page_dock_visibility()
    assert not editor.isHidden()  # entering Mixer does not force its visibility


def test_native_reset_restores_three_panels_and_hides_optional_editor(dock_host, qtbot):
    host = dock_host
    host.status_bar = host.statusBar()
    host.dock_audio_viewer.setFloating(True)
    host.dock_audio_console.hide()
    host.dock_audio_clip_editor.show()
    host.reset_layout_to_default()
    qtbot.waitUntil(lambda: not host.dock_audio_viewer.isFloating())
    assert all(d.isVisible() for d in host._audio_workspace_docks()[:3])
    assert host.dock_audio_clip_editor.isHidden()
    assert host.dockWidgetArea(host.dock_audio_viewer) == Qt.DockWidgetArea.TopDockWidgetArea
    profile = host.layout_store.get_profile("studio", "Mixer")
    assert set(profile.visible_panels) == {"mixer.console", "mixer.viewer", "mixer.timeline"}


def test_toggle_action_and_other_pages_keep_mixer_panels_isolated(dock_host):
    host = dock_host
    host.dock_audio_viewer.toggleViewAction().trigger()
    assert host.dock_audio_viewer.isHidden()
    host._enforce_page_dock_visibility()
    assert host.dock_audio_viewer.isHidden()
    # Outside Mixer the early part of the production guard hides its panels.
    host.current_page = "Color"
    # Provide the legacy optional attributes used later in that same guard.
    import inspect
    import re
    for name in re.findall(r"self\.(dock_[a-z_]+)", inspect.getsource(DockLayoutMixin._enforce_page_dock_visibility)):
        if not hasattr(host, name):
            setattr(host, name, None)
    host._enforce_page_dock_visibility()
    assert all(d.isHidden() for d in host._audio_workspace_docks())
    assert all(not d.toggleViewAction().isEnabled() for d in host._audio_workspace_docks())


def test_mixer_availability_and_viewport_restore_do_not_touch_backend():
    host = SimpleNamespace(_mixer_available=True)
    assert PageManagementMixin._is_page_available(host, "Mixer")
    host._mixer_available = False
    assert not PageManagementMixin._is_page_available(host, "mixer")
    # This deliberately has no Color engine/canvas: UI Mixer must return first.
    host.current_page = "Mixer"
    PageManagementMixin._restore_viewport_after_page_switch(host)
    PageManagementMixin._finalize_viewport_restore(host)


def test_disabled_mixer_dock_setup_does_not_import_optional_feature(monkeypatch):
    monkeypatch.setitem(sys.modules, "src.features.mixer.module", None)
    host = SimpleNamespace(_mixer_available=False, mixer_page=None)
    factory = Mock()
    DockLayoutMixin._create_audio_workspace_docks(host, factory)
    factory.assert_not_called()
    assert host.dock_audio_console is None
    assert host.dock_audio_clip_editor is None
