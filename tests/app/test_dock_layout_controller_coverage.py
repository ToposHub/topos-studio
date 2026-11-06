"""Coverage tests for ``src/app/controllers/dock_layout_controller.py``.

The controller is a mixin on MainWindow.  Following the conventions of
``tests/ui/test_composite_dock_floating_titlebar.py`` and
``tests/edit/test_workspace_presets.py`` the units under test are exercised
on a minimal ``QMainWindow + DockLayoutMixin`` host with real offscreen Qt
docks (no GL, no engine, no modal dialogs):

* dock registry completeness (``all_docks`` membership — source contract)
* ``FloatingDockTitleBar`` behaviour (title sync, close button)
* titlebar maintenance / debounce (``update_all_dock_titlebars``,
  ``_sync_floating_docks``)
* floating/docked geometry recording (``_on_dock_floating_changed``)
* page dock visibility enforcement per page and per work mode
* docked size persistence (``_save_all_docked_sizes`` /
  ``_restore_docked_sizes``)
* layout state serialization round-trips (``_get_all_page_layouts`` /
  ``_apply_all_page_layouts``, ``initialize_page_layouts`` variants,
  ``restore_layout_state`` variants)
* factory default layout file handling (``_load_factory_layout``,
  ``_get_factory_layout_path``, ``set_current_layout_as_default``,
  ``reset_layout_to_default``)
* LayoutStore integration (``_save_current_layout_to_store``,
  ``_restore_layout_from_store``, ``_load_layouts_from_store``,
  ``reset_product_layout``)
* dialog / combobox / menu plumbing (save/delete/apply layout,
  ``update_layout_combobox``, ``on_layout_combo_activated``,
  ``update_load_layout_menu``, ``_show_dock_context_menu``)
* one-shot migrations not already covered elsewhere
  (``_migrate_edit_info_dock_visibility``,
  ``_migrate_composite_scopes_dock_visibility``,
  ``_migrate_color_mapping_dock_layout``)

Suspected src bugs found while writing these tests are recorded in
``docs/testcoverage/2026-09-28/BUGS-agent-B4.md`` (BUG-B4-xx); nothing in
src is modified here.  Where a test pins the *current* behaviour of a
suspected bug it references the matching BUG-B4-xx number in a comment so
the assertion gets updated together with the fix.
"""

from __future__ import annotations

import inspect
import json
import os
import re
from typing import Any, Callable, Iterator, List, Optional, Sequence, Set, Tuple

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6.QtCore import QByteArray, QPoint, Qt
from PyQt6.QtWidgets import (QComboBox, QDockWidget, QMainWindow, QMenu,
                             QStatusBar, QTabBar, QToolButton, QWidget)

from src.app.controllers import dock_layout_controller as dlc
from src.app.controllers.dock_layout_controller import (
    DockLayoutMixin,
    FloatingDockTitleBar,
    _COLOR_TOOL_DOCK_MAX_HEIGHT,
    _COLOR_MAPPING_DOCK_LAYOUT_MIGRATION_KEY,
    _COLOR_MAPPING_DOCK_LAYOUT_MIGRATION_VERSION,
    _COMPOSITE_SCOPES_DOCK_LAYOUT_MIGRATION_KEY,
    _COMPOSITE_SCOPES_DOCK_LAYOUT_MIGRATION_VERSION,
    _EDIT_INFO_DOCK_LAYOUT_MIGRATION_KEY,
    _EDIT_INFO_DOCK_LAYOUT_MIGRATION_VERSION,
)
from src.features.color.ui.color_wheel import GradingPanel
from src.shared.feature_flags import get_effective_layout_mode
from src.shared.i18n.i18n import I18N
from src.shared.layout.profile import LayoutProfile
from src.shared.layout.store import LayoutStore
from src.shared.panels.registry import PanelDescriptor, PanelRegistry


# ═══════════════════════════════════════════════════════════════════════
# Fakes
# ═══════════════════════════════════════════════════════════════════════


class _MemoryConfig:
    """ConfigManager stand-in: get / set(key, value, auto_save) / save_config."""

    def __init__(self, data: Optional[dict] = None) -> None:
        self.data: dict = dict(data or {})
        self.save_config_calls = 0

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def set(self, key: str, value: Any, auto_save: bool = True) -> None:
        self.data[key] = value

    def save_config(self) -> None:
        self.save_config_calls += 1


class _FakeLayoutDb:
    """Layout-database stand-in used by save/delete/apply layout flows."""

    def __init__(self, layouts: Optional[Sequence[str]] = None,
                 settings: Optional[dict] = None) -> None:
        self.layouts: List[str] = list(layouts or [])
        self.settings: dict = dict(settings or {})
        self.saved: List[Tuple[str, str, str, dict, Any]] = []
        self.loaded: List[Tuple[str, Any]] = []
        self.deleted: List[Tuple[str, Any]] = []
        self.blob: Tuple[Optional[str], Optional[str], Optional[dict]] = (None, None, None)

    def save_layout(self, name: str, geom: str, state: str,
                    panels: dict, work_mode: Any = None) -> None:
        self.saved.append((name, geom, state, panels, work_mode))
        if name not in self.layouts:
            self.layouts.append(name)

    def list_layouts(self, work_mode: Any = None) -> List[str]:
        return list(self.layouts)

    def load_layout(self, name: str, work_mode: Any = None) -> Tuple[Optional[str], Optional[str], Optional[dict]]:
        self.loaded.append((name, work_mode))
        return self.blob

    def delete_layout(self, name: str, work_mode: Any = None) -> None:
        self.deleted.append((name, work_mode))
        if name in self.layouts:
            self.layouts.remove(name)

    def get_setting(self, key: str) -> Any:
        return self.settings.get(key)


# attr -> (objectName, initial area), mirroring DockLayoutMixin.setup_docks.
_BASE_DOCKS: dict = {
    "dock_viewport": ("dockViewport", Qt.DockWidgetArea.TopDockWidgetArea),
    "dock_library": ("dockLibrary", Qt.DockWidgetArea.LeftDockWidgetArea),
    "dock_files": ("dockFiles", Qt.DockWidgetArea.LeftDockWidgetArea),
    "dock_info": ("dockInfo", Qt.DockWidgetArea.RightDockWidgetArea),
    "dock_layers": ("dockLayers", Qt.DockWidgetArea.RightDockWidgetArea),
    "dock_tracker": ("dockTracker", Qt.DockWidgetArea.RightDockWidgetArea),
    "dock_denoise": ("dockDenoise", Qt.DockWidgetArea.BottomDockWidgetArea),
    "dock_deblur": ("dockDeblur", Qt.DockWidgetArea.BottomDockWidgetArea),
    "dock_grading": ("dockGrading", Qt.DockWidgetArea.BottomDockWidgetArea),
    "dock_curves": ("dockCurves", Qt.DockWidgetArea.BottomDockWidgetArea),
    "dock_color_mapping": ("dockColorMapping", Qt.DockWidgetArea.BottomDockWidgetArea),
    "dock_audio_mixer": ("dockAudioMixer", Qt.DockWidgetArea.BottomDockWidgetArea),
    "dock_effects": ("dockBlurSharpen", Qt.DockWidgetArea.BottomDockWidgetArea),
    "dock_grain": ("dockGrain", Qt.DockWidgetArea.BottomDockWidgetArea),
    "dock_masks": ("dockMasks", Qt.DockWidgetArea.BottomDockWidgetArea),
    "dock_qualifier": ("dockQualifier", Qt.DockWidgetArea.BottomDockWidgetArea),
    "dock_lut": ("dockLut", Qt.DockWidgetArea.BottomDockWidgetArea),
    "dock_raw": ("dockRaw", Qt.DockWidgetArea.BottomDockWidgetArea),
    "dock_analysis": ("dockAnalysis", Qt.DockWidgetArea.BottomDockWidgetArea),
    "dock_color_keyframes": ("dockColorKeyframes", Qt.DockWidgetArea.BottomDockWidgetArea),
    "dock_export": ("dockExport", Qt.DockWidgetArea.RightDockWidgetArea),
    "dock_crop": ("dockCrop", Qt.DockWidgetArea.BottomDockWidgetArea),
    "dock_plugin": ("dockEffects", Qt.DockWidgetArea.RightDockWidgetArea),
    "dock_script": ("dockScript", Qt.DockWidgetArea.BottomDockWidgetArea),
    "dock_presets": ("dockPresets", Qt.DockWidgetArea.LeftDockWidgetArea),
    "dock_color_timeline_clips": ("dockColorTimelineClips", Qt.DockWidgetArea.TopDockWidgetArea),
}

_EDIT_DOCKS: dict = {
    "dock_edit_media_pool": ("dockEditMediaPool", Qt.DockWidgetArea.LeftDockWidgetArea),
    "dock_program_viewer": ("dockProgramViewer", Qt.DockWidgetArea.TopDockWidgetArea),
    "dock_source_viewer": ("dockSourceViewer", Qt.DockWidgetArea.TopDockWidgetArea),
    "dock_edit_timeline": ("dockEditTimeline", Qt.DockWidgetArea.BottomDockWidgetArea),
    "dock_properties": ("dockProperties", Qt.DockWidgetArea.RightDockWidgetArea),
    "dock_edit_info": ("dockEditInfo", Qt.DockWidgetArea.RightDockWidgetArea),
    "dock_edit_effects": ("dockEditEffects", Qt.DockWidgetArea.RightDockWidgetArea),
    "dock_caption_workspace": ("dockCaptionWorkspace", Qt.DockWidgetArea.RightDockWidgetArea),
    "dock_adr_take_sheet": ("dockAdrTakeSheet", Qt.DockWidgetArea.RightDockWidgetArea),
    "dock_multicam": ("dockMulticam", Qt.DockWidgetArea.RightDockWidgetArea),
    "dock_ai_edit_assistant": ("dockAiEditAssistant", Qt.DockWidgetArea.RightDockWidgetArea),
}

_COMPOSITE_DOCKS: dict = {
    "dock_composite": ("dockComposite", Qt.DockWidgetArea.TopDockWidgetArea),
    "dock_node_properties": ("dockNodeProperties", Qt.DockWidgetArea.RightDockWidgetArea),
    "dock_composite_scope": ("dockCompositeScope", Qt.DockWidgetArea.BottomDockWidgetArea),
    "dock_perf": ("dockPerf", Qt.DockWidgetArea.BottomDockWidgetArea),
    "dock_composite_keyframes": ("dockCompositeKeyframes", Qt.DockWidgetArea.BottomDockWidgetArea),
    "dock_composite_spline": ("dockCompositeSpline", Qt.DockWidgetArea.BottomDockWidgetArea),
    "dock_composite_import_report": ("dockCompositeImportReport", Qt.DockWidgetArea.RightDockWidgetArea),
    "dock_composite_asset_dependency": ("dockCompositeAssetDependency", Qt.DockWidgetArea.RightDockWidgetArea),
    "dock_composite_scene_outliner": ("dockCompositeSceneOutliner", Qt.DockWidgetArea.RightDockWidgetArea),
    "dock_composite_material": ("dockCompositeMaterial", Qt.DockWidgetArea.RightDockWidgetArea),
}

_DELIVER_DOCKS: dict = {
    "dock_deliver": ("dockDeliver", Qt.DockWidgetArea.RightDockWidgetArea),
    "dockVideoBatchExport": ("dockVideoBatchExport", Qt.DockWidgetArea.BottomDockWidgetArea),
}

_ALL_DOCK_SPECS: dict = {**_BASE_DOCKS, **_EDIT_DOCKS, **_COMPOSITE_DOCKS, **_DELIVER_DOCKS}


class _DockHost(QMainWindow, DockLayoutMixin):
    """Minimal MainWindow-shaped host with the full dock set as real Qt docks.

    The host wires the attributes the mixin methods read (``all_docks``,
    ``page_layouts``, ``config_manager``, ``db``, ``status_bar`` ...) and
    connects the titlebar debounce timer exactly like ``MainWindow`` does.
    """

    def __init__(self, edit_available: bool = True,
                 composite_available: bool = True,
                 deliver_available: bool = True) -> None:
        QMainWindow.__init__(self)
        self.setWindowTitle("DockHost")
        self.config_manager = _MemoryConfig()
        self.db = _FakeLayoutDb()
        self.status_bar: QStatusBar = self.statusBar()
        self.layout_combo = QComboBox()
        self.load_layout_menu = QMenu()
        self.workspace_menu = QMenu()
        self.page_layouts: dict = {}
        self.current_page = "Color"
        self._has_saved_state = False
        self._edit_available = edit_available
        self._composite_available = composite_available
        self._deliver_available = deliver_available
        self.feature_flags = None
        self.edit_properties_panel: QWidget = QWidget()
        self.deliver_page = None
        self.composite_page = None
        self.canvas = None
        self.switch_calls: List[Tuple[str, bool]] = []
        self._floating_docks: set = set()
        self._updating_titlebars = False
        # Debounce timer wiring copied from MainWindow.__init__.
        from PyQt6.QtCore import QTimer
        self._titlebar_update_timer = QTimer()
        self._titlebar_update_timer.setSingleShot(True)
        self._titlebar_update_timer.setInterval(50)
        self._titlebar_update_timer.timeout.connect(self._perform_update_all_titlebars)
        self.layout_store = LayoutStore(None)

        self.all_docks: List[QDockWidget] = []
        for attr, (obj_name, area) in _ALL_DOCK_SPECS.items():
            dock = QDockWidget(obj_name, self)
            dock.setObjectName(obj_name)
            dock.setWidget(QWidget())
            self.addDockWidget(area, dock)
            dock.setVisible(False)
            self.all_docks.append(dock)
            setattr(self, attr, dock)

    def switch_to_page(self, page: str, force: bool = False) -> None:
        """Record page switches instead of running the real activation path."""
        self.switch_calls.append((page, force))


def _hex_of(state: QByteArray) -> str:
    return state.toHex().data().decode()


def _from_hex(hex_str: str) -> QByteArray:
    return QByteArray.fromHex(hex_str.encode())


@pytest.fixture
def host(qapp) -> Iterator[_DockHost]:
    """A shown host; shown so ``isVisible()`` semantics match the real app."""
    h = _DockHost()
    h.resize(400, 300)
    h.move(10, 10)
    h.show()
    qapp.processEvents()
    yield h
    h.close()
    h.deleteLater()


@pytest.fixture
def layout_host(qapp) -> Iterator[_DockHost]:
    """An unshown host for methods the real app runs before ``show()``.

    ``apply_default_layout`` / ``initialize_page_layouts`` rebuild the dock
    layout with long ``tabifyDockWidget`` chains.  Qt 6.10 offscreen
    segfaults intermittently when that happens on an already-realized window
    (measured ~50-70% per call), while the production app always runs these
    paths before the window is shown.  ``tests/edit/test_workspace_presets.py``
    follows the same unshown convention, so these tests use this fixture.
    """
    h = _DockHost()
    h.resize(400, 300)
    yield h
    h.close()
    h.deleteLater()


def _set_work_mode(h: _DockHost, mode: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(h, "_current_work_mode", lambda: mode)


def _set_product_mode(h: _DockHost, mode: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(h, "_current_product_mode", lambda: mode)


def _show_all_page_docks(h: _DockHost) -> None:
    """Show every page-specific dock so enforcement has something to hide."""
    for attr in (
        "dock_program_viewer", "dock_source_viewer", "dock_edit_timeline",
        "dock_edit_info", "dock_edit_effects", "dock_ai_edit_assistant",
        "dock_adr_take_sheet", "dock_multicam", "dock_caption_workspace",
        "dock_edit_media_pool", "dock_properties", "dock_composite",
        "dock_node_properties", "dock_perf", "dock_composite_keyframes",
        "dock_composite_spline", "dock_composite_import_report",
        "dock_composite_asset_dependency", "dock_composite_scene_outliner",
        "dock_composite_material", "dock_composite_scope",
        "dock_color_timeline_clips", "dock_deliver", "dockVideoBatchExport",
        "dock_library", "dock_export", "dock_deblur", "dock_crop", "dock_info",
    ):
        getattr(h, attr).setVisible(True)


def _hidden(h: _DockHost, attr: str) -> bool:
    return getattr(h, attr).isHidden()


# ═══════════════════════════════════════════════════════════════════════
# A. Dock registry completeness (all_docks coverage)
# ═══════════════════════════════════════════════════════════════════════


def _registry_sets() -> Tuple[Set[str], Set[str], Set[str], Set[str]]:
    """(created, base-list, appended, adapter-assigned) dock attr sets."""
    src = inspect.getsource(DockLayoutMixin.setup_docks)
    created = set(re.findall(r"self\.(dock_\w+)\s*=\s*create_custom_dock", src))
    literal = re.search(r"self\.all_docks\s*=\s*\[(.*?)\]", src, re.DOTALL)
    assert literal is not None, "setup_docks must construct the all_docks list"
    base = set(re.findall(r"self\.(dock_\w+)", literal.group(1)))
    appended = set(re.findall(r"self\.all_docks\.append\(self\.(dock_\w+)\)", src))
    adapter = set(re.findall(r"self\.(dock_\w+)\s*=\s*docks\[", src))
    return created, base, appended, adapter


def test_color_tool_height_cap_preserves_floating_resize(host: _DockHost, qapp) -> None:
    host.resize(1200, 900)
    host.dock_viewport.show()
    dock = host.dock_grading
    dock.show()
    host._sync_color_tool_dock_max_height(dock, is_floating=False)
    qapp.processEvents()
    host.resizeDocks([dock], [700], Qt.Orientation.Vertical)
    qapp.processEvents()
    assert dock.maximumHeight() == _COLOR_TOOL_DOCK_MAX_HEIGHT
    assert dock.height() <= _COLOR_TOOL_DOCK_MAX_HEIGHT

    host._sync_color_tool_dock_max_height(dock, is_floating=True)
    assert dock.maximumHeight() > _COLOR_TOOL_DOCK_MAX_HEIGHT
    host._sync_color_tool_dock_max_height(dock, is_floating=False)
    assert dock.maximumHeight() == _COLOR_TOOL_DOCK_MAX_HEIGHT


def test_color_tool_height_cap_reaches_wheel_radius_limit(qapp) -> None:
    panel = GradingPanel()
    panel.resize(1260, _COLOR_TOOL_DOCK_MAX_HEIGHT - 7)
    panel.show()
    qapp.processEvents()
    radius_at_cap = panel.lift_wheel._wheel_canvas._geometry()[1]
    panel.resize(1260, _COLOR_TOOL_DOCK_MAX_HEIGHT + 100)
    qapp.processEvents()
    radius_above_cap = panel.lift_wheel._wheel_canvas._geometry()[1]
    panel.close()
    panel.deleteLater()
    assert radius_at_cap == radius_above_cap == 100.0


def test_created_docks_missing_from_all_docks_are_exactly_the_known_five() -> None:
    """BUG-B4-01 fixed: every created dock must be registered in all_docks.

    ``dock_properties`` / ``dock_edit_effects`` / ``dock_caption_workspace`` /
    ``dock_adr_take_sheet`` / ``dock_multicam`` used to be created via
    ``create_custom_dock`` yet never appended to ``all_docks``.  They were
    therefore skipped by ``_perform_update_all_titlebars`` (no draggable
    floating titlebar), ``_sync_floating_docks`` and docked-size persistence.
    Since the fix every created dock is registered, so nothing may be missing.
    """
    created, _base, appended, adapter = _registry_sets()
    missing = created - appended - adapter - set(_BASE_DOCKS)
    assert missing == set()


def test_all_docks_base_list_members_are_created_docks() -> None:
    """Every attr in the base all_docks literal must be a created dock."""
    created, base, _appended, _adapter = _registry_sets()
    assert base, "the base all_docks list must not be empty"
    assert base <= created


def test_every_appended_dock_is_backed_by_a_real_dock() -> None:
    """Every append() target is created, adapter-provided or the AI panel."""
    created, _base, appended, adapter = _registry_sets()
    unbacked = appended - created - adapter
    assert unbacked == {"dock_ai_edit_assistant"}, (
        "append() targets must correspond to real docks; "
        f"unexplained entries: {sorted(unbacked)}"
    )


def test_registered_docks_get_titlebar_maintenance(host: _DockHost) -> None:
    """Behavioural counterpart: all_docks members receive titlebar upkeep."""
    grading = host.dock_grading
    grading.setFloating(True)
    host._perform_update_all_titlebars()
    assert isinstance(grading.titleBarWidget(), FloatingDockTitleBar)
    grading.setFloating(False)
    host._perform_update_all_titlebars()
    tw = grading.titleBarWidget()
    assert tw is not None and not isinstance(tw, FloatingDockTitleBar)
    assert tw.maximumHeight() == 1


# ═══════════════════════════════════════════════════════════════════════
# B. FloatingDockTitleBar
# ═══════════════════════════════════════════════════════════════════════


def test_floating_titlebar_label_syncs_with_dock_title(host: _DockHost) -> None:
    dock = host.dock_grading
    bar = FloatingDockTitleBar(dock)
    dock.setTitleBarWidget(bar)
    assert bar._title_label.text() == dock.windowTitle()
    dock.setWindowTitle("Renamed Panel")
    assert bar._title_label.text() == "Renamed Panel"


def test_floating_titlebar_close_button_hides_dock(host: _DockHost) -> None:
    dock = host.dock_grading
    dock.setVisible(True)
    dock.setFloating(True)
    bar = FloatingDockTitleBar(dock)
    dock.setTitleBarWidget(bar)
    buttons = bar.findChildren(QToolButton)
    assert len(buttons) == 1
    assert not dock.isHidden()
    buttons[0].click()
    assert dock.isHidden()


# ═══════════════════════════════════════════════════════════════════════
# C. Titlebar debounce / floating sync bookkeeping
# ═══════════════════════════════════════════════════════════════════════


def test_update_all_dock_titlebars_debounce_respects_reentrancy_flag(host: _DockHost) -> None:
    host._updating_titlebars = True
    host.update_all_dock_titlebars()
    assert not host._titlebar_update_timer.isActive()

    host._updating_titlebars = False
    host.update_all_dock_titlebars()
    assert host._titlebar_update_timer.isActive()


def test_perform_update_all_titlebars_resets_reentrancy_flag(host: _DockHost) -> None:
    host._perform_update_all_titlebars()
    assert host._updating_titlebars is False


def test_sync_floating_docks_tracks_floating_membership(host: _DockHost) -> None:
    host.dock_grading.setFloating(True)
    host.dock_curves.setFloating(True)
    host._sync_floating_docks()
    assert host.dock_grading in host._floating_docks
    assert host.dock_curves in host._floating_docks
    assert isinstance(host.dock_grading.titleBarWidget(), FloatingDockTitleBar)
    assert isinstance(host.dock_curves.titleBarWidget(), FloatingDockTitleBar)

    host.dock_curves.setFloating(False)
    host._sync_floating_docks()
    assert host.dock_curves not in host._floating_docks
    assert host.dock_curves.titleBarWidget().maximumHeight() == 1
    assert host.dock_grading in host._floating_docks


def test_on_dock_floating_changed_schedules_titlebar_debounce(
        host: _DockHost, qapp) -> None:
    host._titlebar_update_timer.stop()
    host._on_dock_floating_changed(False, host.dock_grading)
    # The handler defers the debounce kick to the next event-loop turn.
    qapp.processEvents()
    assert host._titlebar_update_timer.isActive()


# ═══════════════════════════════════════════════════════════════════════
# D. Floating / docked geometry recording
# ═══════════════════════════════════════════════════════════════════════


def test_docking_transition_records_floating_geometry(host: _DockHost) -> None:
    """AUD-2600 fixed: docking must NOT overwrite the floating_* keys.

    Pin flipped (locked-old-behaviour protocol): at topLevelChanged(False)
    the dock is already reparented into the main window, so x()/y() are
    main-window child coordinates (not screen coordinates) and the size is
    no longer the floating window size. The old handler wrote those wrong
    values into ``dock_*_floating_size``/``_floating_pos``, clobbering the
    correct geometry file_controller.closeEvent saves for docks that are
    still floating. Floating geometry persistence now happens only on the
    closeEvent path for floating docks.
    """
    dock = host.dock_grading
    dock.setFloating(True)
    dock.resize(500, 400)
    dock.move(120, 90)

    host._on_dock_floating_changed(False, dock)  # docking: no floating write-back

    assert host.config_manager.get("dock_dockGrading_floating_size") is None
    assert host.config_manager.get("dock_dockGrading_floating_pos") is None


def test_float_transition_restores_recorded_geometry(host: _DockHost) -> None:
    dock = host.dock_grading
    dock.setFloating(True)
    host.config_manager.set("dock_dockGrading_floating_size", "480x360")
    host.config_manager.set("dock_dockGrading_floating_pos", "70,80")
    dock.resize(200, 150)
    dock.move(5, 5)

    host._on_dock_floating_changed(True, dock)  # floating: restore size + pos

    assert (dock.width(), dock.height()) == (480, 360)
    assert (dock.x(), dock.y()) == (70, 80)


def test_float_transition_with_malformed_saved_geometry_is_ignored(host: _DockHost) -> None:
    dock = host.dock_lut
    dock.setFloating(True)
    dock.resize(320, 240)
    host.config_manager.set("dock_dockLut_floating_size", "not-a-size")
    host.config_manager.set("dock_dockLut_floating_pos", "left,top")

    host._on_dock_floating_changed(True, dock)  # must not raise

    assert (dock.width(), dock.height()) == (320, 240)


# ═══════════════════════════════════════════════════════════════════════
# E. Page dock visibility enforcement
# ═══════════════════════════════════════════════════════════════════════


def test_enforce_on_color_page_hides_edit_and_composite_docks(
        host: _DockHost, monkeypatch: pytest.MonkeyPatch) -> None:
    _set_work_mode(host, "video", monkeypatch)
    host.current_page = "Color"
    _show_all_page_docks(host)

    host._enforce_page_dock_visibility()

    for attr in ("dock_program_viewer", "dock_source_viewer", "dock_edit_timeline",
                 "dock_edit_info", "dock_edit_effects", "dock_ai_edit_assistant",
                 "dock_adr_take_sheet", "dock_multicam", "dock_edit_media_pool",
                 "dock_properties", "dock_composite", "dock_node_properties",
                 "dock_perf", "dock_composite_keyframes", "dock_composite_spline",
                 "dock_composite_import_report", "dock_composite_asset_dependency",
                 "dock_composite_scene_outliner", "dock_composite_material",
                 "dock_composite_scope"):
        assert _hidden(host, attr), f"{attr} must be hidden on the Color page"
    # Color's own dock stays visible (video mode).
    assert not _hidden(host, "dock_color_timeline_clips")


def test_enforce_on_edit_page_keeps_edit_docks(
        host: _DockHost, monkeypatch: pytest.MonkeyPatch) -> None:
    _set_work_mode(host, "video", monkeypatch)
    host.current_page = "Edit"
    _show_all_page_docks(host)

    host._enforce_page_dock_visibility()

    for attr in ("dock_program_viewer", "dock_edit_timeline", "dock_properties",
                 "dock_edit_info", "dock_edit_effects", "dock_edit_media_pool"):
        assert not _hidden(host, attr), f"{attr} must stay visible on the Edit page"
    for attr in ("dock_composite", "dock_node_properties", "dock_composite_scope",
                 "dock_color_timeline_clips"):
        assert _hidden(host, attr), f"{attr} must be hidden on the Edit page"


def test_enforce_on_composite_page_keeps_composite_and_shared_docks(
        host: _DockHost, monkeypatch: pytest.MonkeyPatch) -> None:
    _set_work_mode(host, "video", monkeypatch)
    host.current_page = "Composite"
    _show_all_page_docks(host)

    host._enforce_page_dock_visibility()

    for attr in ("dock_composite", "dock_node_properties", "dock_composite_scope",
                 "dock_composite_keyframes"):
        assert not _hidden(host, attr), f"{attr} must stay visible on Composite"
    # Media Pool is shared between Edit and Composite.
    assert not _hidden(host, "dock_edit_media_pool")
    for attr in ("dock_program_viewer", "dock_source_viewer", "dock_edit_timeline",
                 "dock_edit_info", "dock_edit_effects", "dock_properties",
                 "dock_color_timeline_clips"):
        assert _hidden(host, attr), f"{attr} must be hidden on Composite"


def test_enforce_video_mode_hides_image_only_docks(
        host: _DockHost, monkeypatch: pytest.MonkeyPatch) -> None:
    _set_work_mode(host, "video", monkeypatch)
    host.current_page = "Edit"
    _show_all_page_docks(host)

    host._enforce_page_dock_visibility()

    assert _hidden(host, "dock_library"), "video mode must hide the image Library"
    assert _hidden(host, "dock_export"), "video mode must hide image Batch Export"
    # Deblur/Crop/Info 是图片编辑专属工具，剪辑（视频）模式强制隐藏。
    assert _hidden(host, "dock_deblur"), "video mode must hide the image-only Deblur"
    assert _hidden(host, "dock_crop"), "video mode must hide the image-only Crop"
    assert _hidden(host, "dock_info"), "video mode must hide the image-only Info"


def test_enforce_image_mode_hides_video_only_docks(
        host: _DockHost, monkeypatch: pytest.MonkeyPatch) -> None:
    _set_work_mode(host, "image", monkeypatch)
    host.current_page = "Color"
    _show_all_page_docks(host)

    host._enforce_page_dock_visibility()

    assert _hidden(host, "dock_color_timeline_clips"), (
        "image mode must hide the Color Timeline Clips strip")
    assert _hidden(host, "dockVideoBatchExport"), (
        "image mode must hide the video Batch Export dock")
    # Image-only docks stay usable in image mode.
    assert not _hidden(host, "dock_library")
    assert not _hidden(host, "dock_export")
    assert not _hidden(host, "dock_deblur")
    assert not _hidden(host, "dock_crop")
    assert not _hidden(host, "dock_info")


def test_enforce_caption_workspace_is_hidden_on_non_edit_pages(
        host: _DockHost, monkeypatch: pytest.MonkeyPatch) -> None:
    """BUG-B4-02 fixed: dock_caption_workspace is in the edit_only list.

    ``_enforce_page_dock_visibility`` used to never reference the Captions
    dock, so a layout that leaked it onto another page kept it visible while
    ADR / Multicam were enforced.  It is now part of the edit_only list and
    gets force-hidden on non-Edit pages like its siblings.
    """
    _set_work_mode(host, "video", monkeypatch)
    host.current_page = "Color"
    _show_all_page_docks(host)

    host._enforce_page_dock_visibility()

    assert _hidden(host, "dock_adr_take_sheet")
    assert _hidden(host, "dock_multicam")
    assert _hidden(host, "dock_caption_workspace"), (
        "Captions must be force-hidden on non-Edit pages (BUG-B4-02 fixed)")


def test_enforce_deliver_dock_is_hidden_on_non_deliver_pages(
        host: _DockHost, monkeypatch: pytest.MonkeyPatch) -> None:
    """BUG-B4-03 fixed: dock_deliver is page-scoped by enforcement too."""
    _set_work_mode(host, "video", monkeypatch)
    host.current_page = "Color"
    _show_all_page_docks(host)

    host._enforce_page_dock_visibility()

    assert _hidden(host, "dock_deliver"), (
        "the Deliver dock must be force-hidden on non-Deliver pages "
        "(BUG-B4-03 fixed)")


# ═══════════════════════════════════════════════════════════════════════
# F. Docked size persistence
# ═══════════════════════════════════════════════════════════════════════


def test_save_all_docked_sizes_skips_floating_and_persists_config(host: _DockHost) -> None:
    host.dock_curves.setFloating(True)
    before = host.config_manager.save_config_calls

    host._save_all_docked_sizes()

    key = "dock_dockGrading_docked_size"
    val = host.config_manager.get(key)
    assert val == f"{host.dock_grading.width()}x{host.dock_grading.height()}"
    assert "dock_dockCurves_docked_size" not in host.config_manager.data, (
        "floating docks must not be recorded as docked sizes")
    assert host.config_manager.save_config_calls == before + 1


def test_restore_docked_sizes_groups_by_area_and_orientation(
        host: _DockHost, monkeypatch: pytest.MonkeyPatch) -> None:
    host.config_manager.set("dock_dockLibrary_docked_size", "640x480")
    host.config_manager.set("dock_dockGrading_docked_size", "900x300")
    host.config_manager.set("dock_dockCurves_docked_size", "garbage")  # ignored
    host.dock_masks.setFloating(True)
    host.config_manager.set("dock_dockMasks_docked_size", "77x77")  # floating → ignored

    calls: List[Tuple[Tuple[str, ...], Tuple[int, ...], Qt.Orientation]] = []
    original_resize: Callable = host.resizeDocks

    def _recording_resize(docks: List[QDockWidget], sizes: List[int],
                          orientation: Qt.Orientation) -> None:
        calls.append((tuple(d.objectName() for d in docks), tuple(sizes), orientation))
        original_resize(docks, sizes, orientation)

    monkeypatch.setattr(host, "resizeDocks", _recording_resize)

    host._restore_docked_sizes()

    horizontal = [c for c in calls if c[2] == Qt.Orientation.Horizontal]
    vertical = [c for c in calls if c[2] == Qt.Orientation.Vertical]
    assert any("dockLibrary" in names and 640 in sizes for names, sizes, _ in horizontal), (
        f"left-area docks restore width horizontally: {calls}")
    assert any("dockGrading" in names and 300 in sizes for names, sizes, _ in vertical), (
        f"bottom-area docks restore height vertically: {calls}")
    # Invalid / floating entries are dropped, never crash the loop.
    for names, _sizes, _o in calls:
        assert "dockCurves" not in names
        assert "dockMasks" not in names


# ═══════════════════════════════════════════════════════════════════════
# G. Layout state serialization round-trips
# ═══════════════════════════════════════════════════════════════════════


def test_get_all_page_layouts_serializes_current_page(host: _DockHost) -> None:
    host.current_page = "Color"
    host.dock_grading.setVisible(True)
    host.page_layouts["Library"] = host.saveState()  # pre-existing entry

    states = host._get_all_page_layouts()

    assert set(states) == {"Color", "Library"}
    for page, hex_str in states.items():
        assert isinstance(hex_str, str) and hex_str
        bytes.fromhex(hex_str)  # must be valid hex
    assert _from_hex(states["Library"]) == host.page_layouts["Library"]
    # The current page was snapshotted into page_layouts before collecting.
    assert host.page_layouts["Color"] == _from_hex(states["Color"])


def test_apply_all_page_layouts_roundtrips_arrangement(host: _DockHost) -> None:
    host.current_page = "Color"
    host.dock_library.setVisible(False)
    host.dock_masks.setVisible(True)
    states = host._get_all_page_layouts()

    # Mutate the arrangement, then restore through the serialized form.
    host.dock_library.setVisible(True)
    host.dock_masks.setVisible(False)
    assert not host.dock_library.isHidden()
    assert host.dock_masks.isHidden()

    host._apply_all_page_layouts(states)

    assert isinstance(host.page_layouts["Color"], QByteArray)
    assert host.dock_library.isHidden(), "saved layout must hide the Library again"
    assert not host.dock_masks.isHidden(), "saved layout must re-show the Masks dock"


def test_apply_all_page_layouts_empty_is_noop(host: _DockHost) -> None:
    host.dock_library.setVisible(True)
    before = host.saveState()

    host._apply_all_page_layouts({})

    assert host.page_layouts == {}
    assert not host.dock_library.isHidden()
    assert host.saveState() == before


# ═══════════════════════════════════════════════════════════════════════
# H. apply_default_layout  (unshown host: tabify chains crash on realized
#    windows under Qt 6.10 offscreen — see the layout_host fixture)
# ═══════════════════════════════════════════════════════════════════════


def test_apply_default_layout_arrangement_and_titlebars(
        layout_host: _DockHost, monkeypatch: pytest.MonkeyPatch) -> None:
    _set_work_mode(layout_host, "all", monkeypatch)  # neither video- nor image-only hiding
    layout_host.dock_curves.setFloating(True)

    layout_host.apply_default_layout()

    # First run: every base dock visible（350a037 起 Effects 为左侧
    # Gallery/LUT 标签组一等面板——不再默认隐藏）。
    for attr in _BASE_DOCKS:
        assert _hidden(layout_host, attr) == (attr == "dock_color_keyframes"), attr
    assert not layout_host.dock_curves.isFloating(), "default layout re-docks floating docks"

    # Areas.
    assert layout_host.dockWidgetArea(layout_host.dock_viewport) == Qt.DockWidgetArea.TopDockWidgetArea
    assert layout_host.dockWidgetArea(layout_host.dock_library) == Qt.DockWidgetArea.LeftDockWidgetArea
    assert layout_host.dockWidgetArea(layout_host.dock_files) == Qt.DockWidgetArea.LeftDockWidgetArea
    assert layout_host.dockWidgetArea(layout_host.dock_info) == Qt.DockWidgetArea.RightDockWidgetArea

    # Tab groups.
    assert layout_host.dock_files in layout_host.tabifiedDockWidgets(layout_host.dock_library)
    assert layout_host.dock_layers in layout_host.tabifiedDockWidgets(layout_host.dock_info)
    # The bottom tools share one tab group...
    assert layout_host.dock_deblur in layout_host.tabifiedDockWidgets(layout_host.dock_grading)
    # ...except Denoise, which splitDockWidget pulls out next to Scopes.
    assert layout_host.dock_denoise not in layout_host.tabifiedDockWidgets(
        layout_host.dock_grading)

    # Docked docks get the 1px drag titlebar.
    assert layout_host.dock_grading.titleBarWidget().maximumHeight() == 1

    # Default docked sizes persisted.
    assert "dock_dockGrading_docked_size" in layout_host.config_manager.data


def test_apply_default_layout_video_mode_hides_image_panels(
        layout_host: _DockHost, monkeypatch: pytest.MonkeyPatch) -> None:
    _set_work_mode(layout_host, "video", monkeypatch)
    layout_host.apply_default_layout()
    assert _hidden(layout_host, "dock_library")
    assert _hidden(layout_host, "dock_export")
    # 图片专属工具面板在视频模式的硬编码默认布局中同样不出现（首启无闪烁）。
    assert _hidden(layout_host, "dock_deblur")
    assert _hidden(layout_host, "dock_crop")
    assert _hidden(layout_host, "dock_info")


def test_color_keyframes_menu_opens_in_old_layout_and_stays_on_color_page(
        layout_host: _DockHost, monkeypatch: pytest.MonkeyPatch) -> None:
    _set_work_mode(layout_host, "video", monkeypatch)
    dock = layout_host.dock_color_keyframes
    _, _, panels = layout_host._load_factory_layout("video")
    assert layout_host.restoreState(_from_hex(panels["Color"]))
    # Startup can immediately save a new Qt blob before the user opens the panel.
    layout_host.page_layouts["Color"] = layout_host.saveState()
    dock.toggleViewAction().triggered.connect(layout_host._raise_color_keyframes)
    dock.toggleViewAction().trigger()
    assert not dock.isHidden()
    assert dock in layout_host.tabifiedDockWidgets(layout_host.dock_analysis)
    layout_host.current_page = "Edit"
    layout_host._enforce_page_dock_visibility()
    assert dock.isHidden()
    assert not dock.toggleViewAction().isEnabled()


def test_apply_default_layout_image_mode_hides_video_panels(
        layout_host: _DockHost, monkeypatch: pytest.MonkeyPatch) -> None:
    _set_work_mode(layout_host, "image", monkeypatch)
    layout_host.apply_default_layout()
    assert _hidden(layout_host, "dock_color_timeline_clips")
    assert _hidden(layout_host, "dockVideoBatchExport")
    assert not _hidden(layout_host, "dock_library")
    # 图片专属工具面板在图片模式保持可用。
    assert not _hidden(layout_host, "dock_deblur")
    assert not _hidden(layout_host, "dock_crop")
    assert not _hidden(layout_host, "dock_info")


def test_apply_default_layout_keeps_plugin_when_state_saved(
        layout_host: _DockHost, monkeypatch: pytest.MonkeyPatch) -> None:
    _set_work_mode(layout_host, "all", monkeypatch)
    layout_host._has_saved_state = True
    layout_host.dock_plugin.setVisible(True)
    layout_host.apply_default_layout()
    assert not _hidden(layout_host, "dock_plugin"), (
        "with saved state the user's Effects visibility preference is kept")


# ═══════════════════════════════════════════════════════════════════════
# I. initialize_page_layouts  (unshown host, same reason as section H)
# ═══════════════════════════════════════════════════════════════════════


def test_initialize_restores_legacy_flat_saved_layouts(layout_host: _DockHost) -> None:
    layout_host.current_page = "Color"
    layout_host.dock_library.setVisible(False)
    color_hex = _hex_of(layout_host.saveState())
    layout_host.dock_masks.setVisible(False)
    edit_hex = _hex_of(layout_host.saveState())
    layout_host.config_manager.set("page_layouts", {"Color": color_hex, "Edit": edit_hex})
    layout_host.page_layouts = {}

    layout_host.initialize_page_layouts()

    assert layout_host.page_layouts["Color"] == _from_hex(color_hex)
    assert layout_host.page_layouts["Edit"] == _from_hex(edit_hex)
    # Page-specific docks were hidden before the global state is applied.
    assert _hidden(layout_host, "dock_program_viewer")
    assert _hidden(layout_host, "dock_composite")
    # No page switch happens on the saved path (restore_layout_state applies it).
    assert layout_host.switch_calls == []


def test_initialize_prefers_effective_mode_bucket_over_all(layout_host: _DockHost) -> None:
    elm = get_effective_layout_mode(layout_host.config_manager)
    layout_host.current_page = "Color"
    layout_host.dock_library.setVisible(False)
    mode_hex = _hex_of(layout_host.saveState())
    layout_host.dock_library.setVisible(True)
    layout_host.dock_masks.setVisible(False)
    all_hex = _hex_of(layout_host.saveState())
    assert mode_hex != all_hex, "test needs two distinguishable states"

    layout_host.config_manager.set(
        "page_layouts", {elm: {"Color": mode_hex}, "all": {"Color": all_hex}})
    layout_host.page_layouts = {}
    layout_host.initialize_page_layouts()
    assert layout_host.page_layouts["Color"] == _from_hex(mode_hex), (
        f"bucket {elm!r} must win over the shared 'all' bucket")

    # Without a mode-specific bucket the shared one applies.
    host2 = _DockHost()
    host2.current_page = "Color"
    host2.config_manager.set("page_layouts", {"all": {"Color": all_hex}})
    host2.page_layouts = {}
    host2.initialize_page_layouts()
    assert host2.page_layouts["Color"] == _from_hex(all_hex)
    host2.close()
    host2.deleteLater()


def test_initialize_first_run_uses_factory_file(
        layout_host: _DockHost, monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    layout_host.current_page = "Color"
    layout_host.dock_program_viewer.setVisible(False)
    state_hex = _hex_of(layout_host.saveState())
    geom_hex = _hex_of(layout_host.saveGeometry())
    layout_host.dock_program_viewer.setVisible(True)  # pre-hide + state must re-hide
    factory = tmp_path / "factory.json"
    factory.write_text(json.dumps({
        "schema": 1,
        "modes": {"video": {
            "geometry": geom_hex,
            "state": state_hex,
            "panel_states": {"Color": state_hex},
        }},
    }), encoding="utf-8")
    monkeypatch.setenv("TOPOS_DEFAULT_LAYOUT_PATH", str(factory))
    _set_work_mode(layout_host, "video", monkeypatch)

    layout_host.initialize_page_layouts()

    assert layout_host.page_layouts["Color"] == _from_hex(state_hex)
    assert _hidden(layout_host, "dock_program_viewer")
    assert layout_host.switch_calls == [("Color", True)], (
        "first run must run the full page activation path")


def test_initialize_falls_back_to_hardcoded_defaults(
        layout_host: _DockHost, monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("TOPOS_DEFAULT_LAYOUT_PATH",
                       str(tmp_path / "missing.json"))
    layout_host.current_page = "Color"

    layout_host.initialize_page_layouts()

    assert set(layout_host.page_layouts) >= {"Color", "Library", "Edit",
                                            "Composite", "Deliver"}
    for page, state in layout_host.page_layouts.items():
        assert isinstance(state, QByteArray) and not state.isEmpty(), page
    # Page-specific docks hidden again before switching back to Color.
    for attr in ("dock_program_viewer", "dock_edit_timeline", "dock_properties",
                 "dock_composite", "dock_composite_scope"):
        assert _hidden(layout_host, attr), f"{attr} must be hidden before the page switch"
    # BUG-B4-03 fixed: Composite's Node Properties dock is now in the
    # post-branch hide lists of initialize_page_layouts, so it no longer
    # leaks onto the current (non-Composite) page after the Composite capture.
    assert _hidden(layout_host, "dock_node_properties"), (
        "dock_node_properties must be hidden by initialize_page_layouts "
        "before the page switch (BUG-B4-03 fixed)")
    assert layout_host.switch_calls == [("Color", True)]

    # BUG-B4-03 fixed (variant without the Composite branch): the Deliver dock
    # is likewise in those hide lists now — it no longer relies on the later
    # Composite branch's hide-all loop to end up hidden on a non-Deliver page.
    host2 = _DockHost(composite_available=False)
    host2.current_page = "Color"
    host2.initialize_page_layouts()
    assert _hidden(host2, "dock_deliver"), (
        "dock_deliver must be hidden by initialize_page_layouts even when no "
        "later branch hides it (BUG-B4-03 fixed)")
    host2.close()
    host2.deleteLater()


# ═══════════════════════════════════════════════════════════════════════
# J. LayoutStore integration
# ═══════════════════════════════════════════════════════════════════════


def test_save_current_layout_to_store_records_visible_panels(
        host: _DockHost, monkeypatch: pytest.MonkeyPatch) -> None:
    _set_product_mode(host, "studio", monkeypatch)
    registry = PanelRegistry()
    registry.register(PanelDescriptor(
        panel_id="color.wheel_test",
        title_key="panel.color_wheel",
        fallback_title="Color Wheel",
        feature_id="color",
        category="Color",
        dock_attr="dock_grading",
        order=1,
    ))
    monkeypatch.setattr(dlc, "get_panel_registry", lambda: registry)

    host.dock_grading.setVisible(True)
    host.dock_lut.setVisible(False)
    host._save_current_layout_to_store("Color")

    profile = host.layout_store.get_profile("studio", "Color")
    assert profile is not None
    assert profile.visible_panels == ("color.wheel_test",)
    assert profile.qt_state_hex == _hex_of(host.saveState())


def test_restore_layout_from_store_true_and_false_paths(
        host: _DockHost, monkeypatch: pytest.MonkeyPatch) -> None:
    _set_product_mode(host, "studio", monkeypatch)
    # No profile stored: the default profile has no qt_state_hex → False.
    assert host._restore_layout_from_store("Color") is False

    state_hex = _hex_of(host.saveState())
    host.layout_store.save_profile(LayoutProfile(
        product_mode="studio", page_id="Color", qt_state_hex=state_hex))
    assert host._restore_layout_from_store("Color") is True


def test_load_layouts_from_store_populates_page_layouts(
        host: _DockHost, monkeypatch: pytest.MonkeyPatch) -> None:
    _set_product_mode(host, "studio", monkeypatch)
    state_hex = _hex_of(host.saveState())
    host.layout_store.save_profile(LayoutProfile(
        product_mode="studio", page_id="Edit", qt_state_hex=state_hex))
    host.layout_store.save_profile(LayoutProfile(
        product_mode="other_mode", page_id="Color", qt_state_hex=state_hex))

    host._load_layouts_from_store()

    assert host.page_layouts.get("Edit") == _from_hex(state_hex)
    assert "Color" not in host.page_layouts, (
        "profiles of other product modes must not leak into this mode")


def test_reset_product_layout_clears_current_mode_only(
        host: _DockHost, monkeypatch: pytest.MonkeyPatch) -> None:
    _set_product_mode(host, "studio", monkeypatch)
    host.layout_store.save_profile(LayoutProfile(
        product_mode="studio", page_id="Color", qt_state_hex="ab"))
    host.layout_store.save_profile(LayoutProfile(
        product_mode="other_mode", page_id="Edit", qt_state_hex="cd"))
    host.page_layouts = {"Color": QByteArray(b"x"), "Edit": QByteArray(b"y")}

    host.reset_product_layout()

    assert host.layout_store.get_profiles_for_mode("studio") == {}
    assert host.page_layouts == {}
    kept = host.layout_store.get_profiles_for_mode("other_mode")
    assert set(kept) == {"Edit"}, "other product modes must be preserved"


# ═══════════════════════════════════════════════════════════════════════
# K. Factory default layout file
# ═══════════════════════════════════════════════════════════════════════


def test_get_factory_layout_path_env_override(
        host: _DockHost, monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    override = tmp_path / "my_default.json"
    monkeypatch.setenv("TOPOS_DEFAULT_LAYOUT_PATH", str(override))
    assert host._get_factory_layout_path() == str(override)

    monkeypatch.setenv("TOPOS_DEFAULT_LAYOUT_PATH", "~/rel.json")
    assert host._get_factory_layout_path() == os.path.abspath(
        os.path.expanduser("~/rel.json"))

    monkeypatch.delenv("TOPOS_DEFAULT_LAYOUT_PATH")
    default = host._get_factory_layout_path()
    assert os.path.basename(default) == "default_layout.json"
    assert os.path.isfile(default), "the committed factory default must exist"


def _write_factory(path, payload: dict) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_load_factory_layout_mode_bucket_and_fallbacks(
        host: _DockHost, monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    path = tmp_path / "factory.json"
    _write_factory(path, {
        "schema": 1,
        "modes": {
            "video": {"geometry": "aa", "state": "bb",
                      "panel_states": {"Color": "cc"}},
            "all": {"geometry": "dd", "state": "ee"},
        },
    })
    monkeypatch.setenv("TOPOS_DEFAULT_LAYOUT_PATH", str(path))

    geom, state, panels = host._load_factory_layout(work_mode="video")
    assert (geom, state, panels) == ("aa", "bb", {"Color": "cc"})

    # Mode without its own capture falls back to the shared "all" bucket.
    geom, state, panels = host._load_factory_layout(work_mode="image")
    assert (geom, state, panels) == ("dd", "ee", None)

    # Unknown mode and no "all" bucket → nothing usable.
    _write_factory(path, {"schema": 1, "modes": {"video": {"geometry": "aa", "state": "bb"}}})
    assert host._load_factory_layout(work_mode="image") == (None, None, None)

    # Legacy flat capture applies to every mode.
    _write_factory(path, {"geometry": "ff", "state": "11"})
    assert host._load_factory_layout(work_mode="video") == ("ff", "11", None)

    # work_mode=None resolves through the current work mode.
    _set_work_mode(host, "video", monkeypatch)
    _write_factory(path, {"schema": 1, "modes": {"video": {"geometry": "aa", "state": "bb"}}})
    assert host._load_factory_layout() == ("aa", "bb", None)


def test_load_factory_layout_rejects_missing_or_corrupt_files(
        host: _DockHost, monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    missing = tmp_path / "missing.json"
    monkeypatch.setenv("TOPOS_DEFAULT_LAYOUT_PATH", str(missing))
    assert host._load_factory_layout() == (None, None, None)

    corrupt = tmp_path / "corrupt.json"
    corrupt.write_text("{not json", encoding="utf-8")
    monkeypatch.setenv("TOPOS_DEFAULT_LAYOUT_PATH", str(corrupt))
    assert host._load_factory_layout() == (None, None, None)

    unusable = tmp_path / "unusable.json"
    _write_factory(unusable, {"schema": 1, "modes": {
        "video": {"geometry": "", "state": 42}}})
    monkeypatch.setenv("TOPOS_DEFAULT_LAYOUT_PATH", str(unusable))
    assert host._load_factory_layout(work_mode="video") == (None, None, None)


def test_set_current_layout_as_default_merges_legacy_capture(
        host: _DockHost, monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    path = tmp_path / "default_layout.json"
    _write_factory(path, {
        "geometry": "legacygeom",
        "state": "legacystate",
        "panel_states": {"Color": "legacyhex"},
    })
    monkeypatch.setenv("TOPOS_DEFAULT_LAYOUT_PATH", str(path))
    _set_work_mode(host, "video", monkeypatch)
    host.current_page = "Color"
    host.page_layouts["Color"] = host.saveState()

    host.set_current_layout_as_default()

    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["schema"] == 1
    assert set(data["modes"]) == {"video", "all"}, (
        "the legacy flat capture must survive as the 'all' bucket")
    assert data["modes"]["all"] == {
        "geometry": "legacygeom", "state": "legacystate",
        "panel_states": {"Color": "legacyhex"},
    }
    assert data["modes"]["video"]["geometry"] == _hex_of(host.saveGeometry())
    assert data["modes"]["video"]["state"] == _hex_of(host.saveState())
    assert data["modes"]["video"]["panel_states"] == host._get_all_page_layouts()
    assert host.status_bar.currentMessage() == I18N.tr(
        "status.factory_layout_exported",
        "Default layout updated — commit default_layout.json to ship it.")


def test_set_current_layout_as_default_reports_write_failure(
        host: _DockHost, monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    blocker = tmp_path / "blocker.txt"
    blocker.write_text("file", encoding="utf-8")
    target = blocker / "default_layout.json"  # parent is a file → OSError
    monkeypatch.setenv("TOPOS_DEFAULT_LAYOUT_PATH", str(target))
    _set_work_mode(host, "video", monkeypatch)

    host.set_current_layout_as_default()

    assert host.status_bar.currentMessage() == I18N.tr(
        "status.factory_layout_export_failed",
        "Failed to export factory layout.")


# ═══════════════════════════════════════════════════════════════════════
# L. reset_layout_to_default  (unshown host: fallback path runs
#    apply_default_layout; factory path runs the Color migration tabify)
# ═══════════════════════════════════════════════════════════════════════


def test_reset_layout_to_default_restores_factory_capture(
        layout_host: _DockHost, monkeypatch: pytest.MonkeyPatch) -> None:
    layout_host.current_page = "Color"
    layout_host.dock_composite.setVisible(False)
    state_hex = _hex_of(layout_host.saveState())  # captured with composite hidden
    layout_host.dock_composite.setVisible(True)  # enforcement must re-hide it
    monkeypatch.setattr(
        layout_host, "_load_factory_layout",
        lambda work_mode=None: (_hex_of(layout_host.saveGeometry()), state_hex,
                                {"Color": state_hex}))

    layout_host.reset_layout_to_default()

    assert layout_host.page_layouts.get("Color") is not None
    assert _hidden(layout_host, "dock_composite"), (
        "reset must enforce page dock visibility against the factory capture")
    assert layout_host.status_bar.currentMessage() == I18N.tr(
        "status.layout_reset", "Layout reset to default.")


def test_reset_layout_to_default_falls_back_on_rejected_state(
        layout_host: _DockHost, monkeypatch: pytest.MonkeyPatch) -> None:
    # Valid geometry hex but a state this build's restoreState rejects.
    monkeypatch.setattr(
        layout_host, "_load_factory_layout",
        lambda work_mode=None: (_hex_of(layout_host.saveGeometry()), "00ff", None))
    _set_work_mode(layout_host, "all", monkeypatch)
    layout_host.current_page = "Color"

    layout_host.reset_layout_to_default()

    # 350a037 起 Effects 面板在硬编码默认布局中可见（左侧标签组）
    assert not _hidden(layout_host, "dock_plugin"), "hardcoded default shows Effects panel"
    assert not _hidden(layout_host, "dock_grading")
    assert layout_host.status_bar.currentMessage() == I18N.tr(
        "status.layout_reset_hardcoded", "Layout reset to hardcoded default.")


def test_reset_layout_to_default_falls_back_when_factory_missing(
        layout_host: _DockHost, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        layout_host, "_load_factory_layout", lambda work_mode=None: (None, None, None))
    _set_work_mode(layout_host, "all", monkeypatch)
    layout_host.current_page = "Color"
    layout_host.dock_curves.setFloating(True)

    layout_host.reset_layout_to_default()

    assert not layout_host.dock_curves.isFloating()
    assert layout_host.status_bar.currentMessage() == I18N.tr(
        "status.layout_reset_hardcoded", "Layout reset to hardcoded default.")


# ═══════════════════════════════════════════════════════════════════════
# M. restore_layout_state
# ═══════════════════════════════════════════════════════════════════════


def _restored_geometry(geom_hex: str):
    """Recompute what restoreGeometry yields for the captured hex."""
    probe = QMainWindow()
    probe.restoreGeometry(_from_hex(geom_hex))
    geom = probe.geometry()
    probe.deleteLater()
    return geom


def test_restore_layout_state_geometry_and_page_state(
        host: _DockHost, monkeypatch: pytest.MonkeyPatch) -> None:
    _set_product_mode(host, "studio", monkeypatch)
    _set_work_mode(host, "video", monkeypatch)
    host.current_page = "Color"
    # Keep the deferred Color migrations out of the picture.
    host.config_manager.set(_COLOR_MAPPING_DOCK_LAYOUT_MIGRATION_KEY, 1)

    host.dock_library.setVisible(False)
    state_hex = _hex_of(host.saveState())
    geom_hex = _hex_of(host.saveGeometry())
    host.page_layouts = {"Color": _from_hex(state_hex)}
    host.config_manager.set("window_geometry", geom_hex)

    # Mutate both the geometry and the dock arrangement.
    host.move(300, 250)
    host.resize(700, 500)
    host.dock_library.setVisible(True)
    assert not host.dock_library.isHidden()

    host.restore_layout_state()

    assert host.geometry() == _restored_geometry(geom_hex)
    assert host.dock_library.isHidden(), "per-page state must be re-applied"


def test_restore_layout_state_window_state_fallback(
        host: _DockHost, monkeypatch: pytest.MonkeyPatch) -> None:
    _set_product_mode(host, "studio", monkeypatch)
    _set_work_mode(host, "video", monkeypatch)
    host.current_page = "Deliver"  # not in page_layouts
    host.dock_masks.setVisible(False)
    state_hex = _hex_of(host.saveState())
    host.page_layouts = {}
    host.db.settings["last_state"] = state_hex

    host.dock_masks.setVisible(True)
    host.restore_layout_state()

    assert host.dock_masks.isHidden(), (
        "global window_state is the final fallback when no per-page layout exists")


# ═══════════════════════════════════════════════════════════════════════
# N. Dialogs / combobox / menus
# ═══════════════════════════════════════════════════════════════════════


def test_save_layout_dialog_persists_everything(
        host: _DockHost, monkeypatch: pytest.MonkeyPatch) -> None:
    _set_work_mode(host, "video", monkeypatch)
    host._edit_available = False
    monkeypatch.setattr(
        dlc.QInputDialog, "getText", lambda *a, **k: ("Profile A", True))

    host.save_layout_dialog()

    assert len(host.db.saved) == 1
    name, geom, state, panels, work_mode = host.db.saved[0]
    assert name == "Profile A"
    assert geom == _hex_of(host.saveGeometry())
    assert state == _hex_of(host.saveState())
    assert "Color" in panels
    assert work_mode == "video"
    assert "dock_dockGrading_docked_size" in host.config_manager.data, (
        "saving a layout must also persist docked sizes")
    assert "Profile A" in host.status_bar.currentMessage()
    assert [host.layout_combo.itemText(i) for i in range(host.layout_combo.count())] == [
        "Layout Presets", "Profile A"]


def test_save_layout_dialog_cancelled_records_nothing(
        host: _DockHost, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        dlc.QInputDialog, "getText", lambda *a, **k: ("ignored", False))
    host.save_layout_dialog()
    assert host.db.saved == []


def test_delete_layout_dialog_confirmed(
        host: _DockHost, monkeypatch: pytest.MonkeyPatch) -> None:
    _set_work_mode(host, "video", monkeypatch)
    host.db.layouts = ["Old One"]
    monkeypatch.setattr(
        dlc.QInputDialog, "getItem", lambda *a, **k: ("Old One", True))
    monkeypatch.setattr(
        dlc.QMessageBox, "question",
        lambda *a, **k: dlc.QMessageBox.StandardButton.Yes)

    host.delete_layout_dialog()

    assert host.db.deleted == [("Old One", "video")]
    assert "Old One" in host.status_bar.currentMessage()


def test_delete_layout_dialog_no_layouts_shows_information(
        host: _DockHost, monkeypatch: pytest.MonkeyPatch) -> None:
    from unittest.mock import MagicMock
    info = MagicMock()
    monkeypatch.setattr(dlc.QMessageBox, "information", info)
    host.delete_layout_dialog()
    assert host.db.deleted == []
    assert info.called


def test_update_layout_combobox_lists_presets_and_custom(
        host: _DockHost, monkeypatch: pytest.MonkeyPatch) -> None:
    _set_work_mode(host, "video", monkeypatch)
    host._edit_available = True
    host.db.layouts = ["Mine"]

    host.update_layout_combobox()

    items = [(host.layout_combo.itemText(i), host.layout_combo.itemData(i))
             for i in range(host.layout_combo.count())]
    texts = [t for t, _d in items]
    assert texts[0] == "Layout Presets"
    assert "Mine" in texts
    preset_ids = {d for _t, d in items if isinstance(d, str)}
    assert preset_ids == {"audio", "captions", "editing", "review"}

    host._edit_available = False
    host.update_layout_combobox()
    texts = [host.layout_combo.itemText(i) for i in range(host.layout_combo.count())]
    assert texts == ["Layout Presets", "Mine"]


def test_on_layout_combo_activated_dispatches_and_resets(
        host: _DockHost, monkeypatch: pytest.MonkeyPatch) -> None:
    _set_work_mode(host, "video", monkeypatch)
    host._edit_available = True
    host.db.layouts = ["Mine"]
    applied: List[str] = []
    presets: List[str] = []
    monkeypatch.setattr(host, "apply_layout", lambda name: applied.append(name))
    monkeypatch.setattr(host, "apply_edit_workspace_preset",
                        lambda pid: presets.append(pid) or True)
    host.update_layout_combobox()

    # Index 0 is the inert header.
    host.layout_combo.setCurrentIndex(0)
    host.on_layout_combo_activated(0)
    assert applied == [] and presets == []

    preset_index = next(i for i in range(host.layout_combo.count())
                        if host.layout_combo.itemData(i) == "audio")
    host.on_layout_combo_activated(preset_index)
    assert presets == ["audio"]
    assert host.layout_combo.currentIndex() == 0

    custom_index = host.layout_combo.findText("Mine")
    host.on_layout_combo_activated(custom_index)
    assert applied == ["Mine"]
    assert host.layout_combo.currentIndex() == 0


def test_update_load_layout_menu_populated_and_empty(
        host: _DockHost, monkeypatch: pytest.MonkeyPatch) -> None:
    _set_work_mode(host, "video", monkeypatch)
    host.db.layouts = ["A", "B"]
    host.update_load_layout_menu()
    actions = host.load_layout_menu.actions()
    assert [a.text() for a in actions] == ["A", "B"]
    assert all(a.isEnabled() for a in actions)

    host.db.layouts = []
    host.update_load_layout_menu()
    actions = host.load_layout_menu.actions()
    assert len(actions) == 1
    assert not actions[0].isEnabled(), "the empty placeholder must be disabled"


def test_apply_layout_restores_geometry_state_and_pages(
        host: _DockHost, monkeypatch: pytest.MonkeyPatch) -> None:
    _set_work_mode(host, "video", monkeypatch)
    host.current_page = "Color"
    host.dock_library.setVisible(False)
    state_hex = _hex_of(host.saveState())
    geom_hex = _hex_of(host.saveGeometry())
    host.dock_library.setVisible(True)

    host.db.blob = (geom_hex, state_hex, {"Color": state_hex})
    host.move(300, 250)
    host.resize(700, 500)

    host.apply_layout("My Layout")

    assert host.db.loaded == [("My Layout", "video")]
    assert host.geometry() == _restored_geometry(geom_hex)
    assert host.dock_library.isHidden()
    assert host.page_layouts["Color"] == _from_hex(state_hex)
    assert "My Layout" in host.status_bar.currentMessage()


def test_apply_layout_without_blob_is_noop(host: _DockHost) -> None:
    host.db.blob = (None, None, None)
    before = host.geometry()
    host.apply_layout("Empty")
    assert host.geometry() == before
    assert host.status_bar.currentMessage() == ""


# ═══════════════════════════════════════════════════════════════════════
# O. Dock context menu
# ═══════════════════════════════════════════════════════════════════════


class _MockMenu:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.exec_calls: List[object] = []
        self.parent = args[0] if args else None

    def setStyleSheet(self, sheet: str) -> None:
        self.sheet = sheet

    def exec(self, pos: object) -> None:
        self.exec_calls.append(pos)


class _MockFactory:
    def __init__(self, name: str) -> None:
        self.name = name
        self.instances: List[_MockMenu] = []

    def __call__(self, *args: Any, **kwargs: Any) -> _MockMenu:
        menu = _MockMenu(*args, **kwargs)
        self.instances.append(menu)
        return menu


def test_context_menu_skipped_for_docks_with_internal_menus(
        host: _DockHost, monkeypatch: pytest.MonkeyPatch) -> None:
    factory = _MockFactory("menu-skip")
    monkeypatch.setattr(dlc, "QMenu", factory)
    dock = host.dock_curves  # objectName in the skip list
    host._show_dock_context_menu(QPoint(3, 4), dock)
    assert factory.instances == [], "no generic menu may be built for dockCurves"


def test_context_menu_delegates_for_edit_effects(
        host: _DockHost, monkeypatch: pytest.MonkeyPatch) -> None:
    from unittest.mock import MagicMock
    factory = _MockFactory("menu-effects")
    monkeypatch.setattr(dlc, "QMenu", factory)
    panel = MagicMock()
    host.edit_effects_panel = panel

    host._show_dock_context_menu(QPoint(3, 4), host.dock_edit_effects)

    panel.show_unified_context_menu.assert_called_once()
    assert factory.instances == [], "the unified panel menu replaces the generic one"
    # The delegation receives a global position.
    args = panel.show_unified_context_menu.call_args[0]
    assert args[0] == host.dock_edit_effects.mapToGlobal(QPoint(3, 4))


def test_context_menu_generic_flow(
        host: _DockHost, monkeypatch: pytest.MonkeyPatch) -> None:
    from unittest.mock import MagicMock
    factory = _MockFactory("menu-generic")
    monkeypatch.setattr(dlc, "QMenu", factory)
    mgmt = MagicMock()
    monkeypatch.setattr(dlc, "add_dock_management_actions", mgmt)

    host._show_dock_context_menu(QPoint(5, 6), host.dock_grading)

    assert len(factory.instances) == 1
    menu = factory.instances[0]
    mgmt.assert_called_once_with(menu, host.dock_grading)
    assert menu.exec_calls == [host.dock_grading.mapToGlobal(QPoint(5, 6))]


# ═══════════════════════════════════════════════════════════════════════
# P. Misc helpers and remaining one-shot migrations
# ═══════════════════════════════════════════════════════════════════════


def test_get_enabled_features_with_and_without_flags(host: _DockHost) -> None:
    from unittest.mock import MagicMock
    flags = MagicMock()
    flags.get_enabled_features.return_value = ["color", "edit"]
    host.feature_flags = flags
    assert host._get_enabled_features() == {"color", "edit"}

    host.feature_flags = None
    assert host._get_enabled_features() == {
        "color", "library", "edit", "composite", "deliver"}


def test_style_dock_tab_bars_only_direct_children(
        layout_host: _DockHost, monkeypatch: pytest.MonkeyPatch) -> None:
    _set_work_mode(layout_host, "all", monkeypatch)
    layout_host.apply_default_layout()
    inner = QTabBar(layout_host.dock_script)
    inner.setObjectName("InnerTabBar")

    layout_host._style_dock_tab_bars()

    named = layout_host.findChildren(QTabBar, "ToposDockTabBar")
    assert named, "QMainWindow-owned dock tab bars must be styled"
    assert all(bar.parent() is layout_host for bar in named)
    assert inner.objectName() == "InnerTabBar", (
        "panel-internal tab bars must not be restyled")


def test_migrate_edit_info_dock_visibility_one_shot(
        host: _DockHost, monkeypatch: pytest.MonkeyPatch) -> None:
    _set_product_mode(host, "studio", monkeypatch)
    host.current_page = "Edit"
    host.dock_edit_info.setVisible(False)
    before = host.config_manager.save_config_calls

    host._migrate_edit_info_dock_visibility()

    assert not _hidden(host, "dock_edit_info"), "the Info tab is added once"
    assert host.config_manager.get(_EDIT_INFO_DOCK_LAYOUT_MIGRATION_KEY) == \
        _EDIT_INFO_DOCK_LAYOUT_MIGRATION_VERSION
    assert host.page_layouts.get("Edit") is not None
    assert host.config_manager.save_config_calls > before

    # Second run is a no-op: a user's later choice to close it survives.
    host.dock_edit_info.setVisible(False)
    host._migrate_edit_info_dock_visibility()
    assert _hidden(host, "dock_edit_info")


def test_migrate_edit_info_skipped_off_edit_page(
        host: _DockHost, monkeypatch: pytest.MonkeyPatch) -> None:
    host.current_page = "Color"
    host.dock_edit_info.setVisible(False)
    host._migrate_edit_info_dock_visibility()
    assert _hidden(host, "dock_edit_info")
    assert host.config_manager.get(_EDIT_INFO_DOCK_LAYOUT_MIGRATION_KEY) is None


def test_migrate_composite_scopes_dock_visibility_one_shot(
        host: _DockHost, monkeypatch: pytest.MonkeyPatch) -> None:
    _set_product_mode(host, "studio", monkeypatch)
    host.current_page = "Composite"
    host.dock_composite_scope.setVisible(False)

    host._migrate_composite_scopes_dock_visibility()

    assert not _hidden(host, "dock_composite_scope")
    assert host.config_manager.get(_COMPOSITE_SCOPES_DOCK_LAYOUT_MIGRATION_KEY) == \
        _COMPOSITE_SCOPES_DOCK_LAYOUT_MIGRATION_VERSION
    assert host.page_layouts.get("Composite") is not None

    host.dock_composite_scope.setVisible(False)
    host._migrate_composite_scopes_dock_visibility()
    assert _hidden(host, "dock_composite_scope"), "one-shot: user choice survives"


def test_migrate_color_mapping_dock_layout_force(
        layout_host: _DockHost, monkeypatch: pytest.MonkeyPatch) -> None:
    """Color-mapping migration tabifies docks, so it runs on an unshown host."""
    _set_product_mode(layout_host, "studio", monkeypatch)
    layout_host.current_page = "Color"
    # Visible tab anchors (Qt only registers tab groups on shown docks in
    # the unshown-window regime this test runs in).
    layout_host.dock_curves.setVisible(True)
    layout_host.dock_effects.setVisible(True)
    layout_host.dock_color_mapping.setVisible(False)
    layout_host.config_manager.set("page_layouts", {"Color": "stale"})

    layout_host._migrate_color_mapping_dock_layout(force=True)

    assert not _hidden(layout_host, "dock_color_mapping")
    assert (layout_host.dock_color_mapping
            in layout_host.tabifiedDockWidgets(layout_host.dock_curves))
    assert layout_host.config_manager.get(_COLOR_MAPPING_DOCK_LAYOUT_MIGRATION_KEY) == \
        _COLOR_MAPPING_DOCK_LAYOUT_MIGRATION_VERSION
    assert layout_host.page_layouts.get("Color") is not None
    assert layout_host.config_manager.get("page_layouts", {}).get("Color") != "stale"

    # Already migrated (no force): user arrangement untouched.
    layout_host.dock_color_mapping.setVisible(False)
    layout_host._migrate_color_mapping_dock_layout()
    assert _hidden(layout_host, "dock_color_mapping")
