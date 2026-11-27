"""P1-24 内建工作区预设测试。

两层验证：
- 纯数据层（workspace_presets.py）：预设规格自洽、与出厂 Edit 布局同构。
- Qt 宿主层：真 QMainWindow + DockLayoutMixin，验证 apply 后 dock 可见性/
  tab 化/快照持久化，以及 layout_combo 分流。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from PyQt6.QtCore import QPoint, QRect, Qt
from PyQt6.QtWidgets import QDockWidget, QMainWindow, QWidget
from pytestqt.qtbot import QtBot

from src.app.controllers.dock_layout_controller import DockLayoutMixin
from src.features.edit.core.workspace_presets import (
    EDIT_WORKSPACE_MANAGED_DOCKS,
    WorkspacePreset,
    get_workspace_preset,
    list_workspace_presets,
)
from src.shared.ui.dock_resize_handle import DockResizeHandle
from src.shared.ui.style_sheet import DARK_THEME

pytestmark = pytest.mark.edit_ui


# ─────────────────────────────────────────────────────────────────────────────
# 纯数据层
# ─────────────────────────────────────────────────────────────────────────────

def test_preset_ids_unique_and_stable():
    presets = list_workspace_presets()
    ids = [p.preset_id for p in presets]
    assert sorted(ids) == ["editing"]
    assert len(set(ids)) == len(ids)


def test_preset_metadata_complete():
    for preset in list_workspace_presets():
        assert preset.label
        assert preset.label_en
        assert preset.description
        assert preset.focus_dock in preset.visible_docks


def test_editing_preset_matches_factory_edit_layout():
    """Editing 预设 = 出厂 Edit 布局的 dock 集合（用户无学习成本）。"""
    preset = get_workspace_preset("editing")
    assert set(preset.visible_docks) == {
        "dockEditMediaPool",
        "dockProgramViewer",
        "dockEditTimeline",
        "dockProperties",
        "dockEditEffects",
        "dockAudioMixer",
        "dock_ai_edit_assistant",
        "dockEditInfo",
    }
    assert "dockSourceViewer" not in EDIT_WORKSPACE_MANAGED_DOCKS
    assert preset.tabify_groups == (
        ("dockEditMediaPool", "dockEditEffects", "dockAudioMixer"),
        ("dockProperties", "dock_ai_edit_assistant", "dockEditInfo"),
    )


@pytest.mark.parametrize("preset_id", ["audio", "captions", "review"])
def test_retired_builtin_presets_cannot_be_loaded(preset_id: str) -> None:
    assert get_workspace_preset(preset_id) is None


def test_invalid_preset_spec_rejected():
    with pytest.raises(ValueError):
        WorkspacePreset(
            preset_id="bad",
            label="坏",
            label_en="Bad",
            description="",
            # 不在受管名单里的 dock
            visible_docks=("dockLibrary",),
            dock_areas=(("dockLibrary", "left"),),
        )
    with pytest.raises(ValueError):
        WorkspacePreset(
            preset_id="bad-area",
            label="坏区域",
            label_en="BadArea",
            description="",
            visible_docks=("dockEditTimeline",),
            dock_areas=(("dockEditTimeline", "middle"),),
        )


def test_get_workspace_preset_unknown_returns_none():
    assert get_workspace_preset("nonexistent") is None


# ─────────────────────────────────────────────────────────────────────────────
# Qt 宿主层
# ─────────────────────────────────────────────────────────────────────────────

_ATTR_BY_OBJNAME = {
    "dockEditMediaPool": "dock_edit_media_pool",
    "dockProgramViewer": "dock_program_viewer",
    "dockSourceViewer": "dock_source_viewer",
    "dockEditTimeline": "dock_edit_timeline",
    "dockEditInfo": "dock_edit_info",
    "dockEditEffects": "dock_edit_effects",
    "dockProperties": "dock_properties",
    "dockCaptionWorkspace": "dock_caption_workspace",
    "dock_ai_edit_assistant": "dock_ai_edit_assistant",
    "dockAudioMixer": "dock_audio_mixer",
    "dockAdrTakeSheet": "dock_adr_take_sheet",
    "dockMulticam": "dock_multicam",
}


class _WorkspaceHost(DockLayoutMixin, QMainWindow):
    """带全部受管 dock 的最小宿主：真 Qt 停靠路径 + mixin 布局方法。"""

    def __init__(self, edit_available: bool = True):
        QMainWindow.__init__(self)
        self._edit_available = edit_available
        self.current_page = "Edit"
        self.page_layouts: dict = {}
        self.status_bar = self.statusBar()
        self.config_manager = None
        # _enforce_page_dock_visibility 直接访问的页面 dock 属性。
        for attr in (
            "dock_program_viewer", "dock_source_viewer", "dock_edit_timeline",
            "dock_edit_info", "dock_edit_effects", "dock_ai_edit_assistant",
            "dock_edit_media_pool", "dock_properties", "dock_caption_workspace",
            "dock_library", "dock_export", "dock_composite", "dock_perf",
            "dock_composite_keyframes", "dock_composite_spline",
            "dock_composite_animation", "dock_composite_import_report",
            "dock_composite_scene_outliner", "dock_composite_material",
            "dock_composite_scope", "dock_composite_asset_dependency",
            "dock_node_properties", "dock_color_timeline_clips",
        ):
            setattr(self, attr, None)
        self.all_docks: list = []
        self.docks_by_name: dict = {}
        for name in EDIT_WORKSPACE_MANAGED_DOCKS:
            dock = QDockWidget(name, self)
            dock.setObjectName(name)
            dock.setWidget(QWidget(self))
            self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, dock)
            dock.setVisible(False)
            self.all_docks.append(dock)
            self.docks_by_name[name] = dock
            setattr(self, _ATTR_BY_OBJNAME[name], dock)


class _FakeLayoutDb:
    def list_page_layouts(self, work_mode: str, page_id: str) -> dict[str, str]:
        assert (work_mode, page_id) == ("all", "Edit")
        return {"My Layout": "edit-state"}


def _visible(host: _WorkspaceHost) -> set:
    # 无头环境下主窗口未 show，isVisible() 受祖先可见性影响；
    # isHidden() 只反映 dock 自身的显式隐藏标志，才是预设控制的量。
    return {
        name for name, dock in host.docks_by_name.items() if not dock.isHidden()
    }


def test_apply_editing_preset_visibility_and_tabs(qapp):
    host = _WorkspaceHost()
    assert host.apply_edit_workspace_preset("editing") is True
    preset = get_workspace_preset("editing")
    assert _visible(host) == set(preset.visible_docks)
    # The viewport is a single dock; inspector tabs remain grouped.
    program = host.docks_by_name["dockProgramViewer"]
    assert host.tabifiedDockWidgets(program) == []
    assert host.dock_source_viewer is None
    # 快照写进 page_layouts["Edit"]。
    assert "Edit" in host.page_layouts
    assert not host.page_layouts["Edit"].isEmpty()


def test_default_edit_timeline_divider_resizes_both_rows(qtbot: QtBot) -> None:
    host = _WorkspaceHost()
    qtbot.addWidget(host)
    host.setStyleSheet(DARK_THEME)
    host.resize(1360, 800)
    host.apply_edit_workspace_preset("editing")
    host.show()
    viewer, timeline = host.dock_program_viewer, host.dock_edit_timeline
    seam = lambda: QPoint(viewer.geometry().center().x(), timeline.y())
    qtbot.waitUntil(lambda: isinstance(host.childAt(seam()), DockResizeHandle))

    handle = host.childAt(seam())
    start = handle.mapFrom(host, seam())
    end = start + QPoint(0, 36)
    old_viewer, old_timeline = viewer.height(), timeline.height()
    qtbot.mousePress(handle, Qt.MouseButton.LeftButton, pos=start)
    qtbot.mouseMove(handle, pos=end)
    qtbot.mouseRelease(handle, Qt.MouseButton.LeftButton, pos=end)
    qtbot.waitUntil(lambda: timeline.height() < old_timeline - 20)
    assert viewer.height() > old_viewer + 20

    host.current_page = "Color"
    host._configure_edit_resize_handles()
    assert handle.isHidden()


def test_default_edit_columns_fit_after_restore_without_feedback(
    qtbot: QtBot, monkeypatch: pytest.MonkeyPatch,
) -> None:
    host = _WorkspaceHost()
    qtbot.addWidget(host)
    host.setStyleSheet(DARK_THEME)
    host.resize(1360, 800)
    host.apply_edit_workspace_preset("editing")
    host.show()
    columns = (host.dock_edit_media_pool, host.dock_program_viewer, host.dock_properties)
    resize_docks = host.resizeDocks
    writes: list[list[int]] = []

    def record(docks: list[QDockWidget], widths: list[int], orientation: Qt.Orientation) -> None:
        writes.append(widths)
        resize_docks(docks, widths, orientation)

    monkeypatch.setattr(host, "resizeDocks", record)
    # Qt can apply an old allocation just after the page becomes visible.
    resize_docks(columns, [510, 338, 510], Qt.Orientation.Horizontal)
    host.layout().activate()

    def matches_default() -> bool:
        total = sum(dock.width() for dock in columns)
        return all(abs(dock.width() - total * share / 10) <= 3
                   for dock, share in zip(columns, (3, 4, 3)))

    assert not matches_default()
    qtbot.waitUntil(matches_default)
    assert len(writes) <= 1

    # A size notification from the docks must not initiate another correction.
    resize_docks(columns, [460, 438, 460], Qt.Orientation.Horizontal)
    host.layout().activate()
    qtbot.wait(180)
    assert len(writes) <= 1

    for width in (1560, 1160, 1560, 1360):
        host.resize(width, 800)
        qtbot.waitUntil(matches_default)
        assert columns[1].width() > max(columns[0].width(), columns[2].width())


def test_edit_divider_drag_sets_ratio_used_by_window_resize(qtbot: QtBot) -> None:
    host = _WorkspaceHost()
    qtbot.addWidget(host)
    host.setStyleSheet(DARK_THEME)
    host.resize(1360, 800)
    host.apply_edit_workspace_preset("editing")
    host.show()
    media, viewer, properties = host.dock_edit_media_pool, host.dock_program_viewer, host.dock_properties
    columns = (media, viewer, properties)

    def target() -> QPoint:
        return QPoint(properties.x(), viewer.geometry().center().y())

    qtbot.waitUntil(lambda: isinstance(host.childAt(target()), DockResizeHandle))
    assert all(host.dockWidgetArea(dock) == Qt.DockWidgetArea.TopDockWidgetArea for dock in columns)
    handle = host.childAt(target())
    start = host.mapToGlobal(target())
    end = start - QPoint(50, 0)
    original = properties.width()
    qtbot.mousePress(handle, Qt.MouseButton.LeftButton, pos=handle.mapFromGlobal(start))
    qtbot.mouseMove(handle, pos=handle.mapFromGlobal(end))
    qtbot.mouseRelease(handle, Qt.MouseButton.LeftButton, pos=handle.mapFromGlobal(end))
    qtbot.waitUntil(lambda: properties.width() > original + 30)
    shares = [dock.width() for dock in columns]

    for width in (1510, 1260, 1430, 1360):
        host.resize(width, 800)
        expected = [(width - 2) * share / sum(shares) for share in shares]
        qtbot.wait(30)
        assert all(abs(dock.width() - target) < 3 for dock, target in zip(columns, expected))
        qtbot.wait(100)
        assert all(abs(dock.width() - target) < 3 for dock, target in zip(columns, expected))

    host.apply_edit_workspace_preset("editing")
    qtbot.waitUntil(lambda: abs(viewer.width() - (host.width() - 2) * 0.4) < 3)


def test_default_edit_combines_side_groups_below_minimum_width(
    qtbot: QtBot, monkeypatch: pytest.MonkeyPatch,
) -> None:
    host = _WorkspaceHost()
    qtbot.addWidget(host)
    host.setStyleSheet(DARK_THEME)
    media, viewer, properties = host.dock_edit_media_pool, host.dock_program_viewer, host.dock_properties
    media.widget().setMinimumWidth(400)
    viewer.widget().setMinimumWidth(330)
    properties.widget().setMinimumWidth(320)
    host.resize(1360, 800)
    host.apply_edit_workspace_preset("editing")
    screen_width = 1920
    monkeypatch.setattr(host, "screen", lambda: SimpleNamespace(
        availableGeometry=lambda: QRect(0, 0, screen_width, 1080),
    ))
    host.show()
    qtbot.waitUntil(lambda: host.dockWidgetArea(properties) == Qt.DockWidgetArea.TopDockWidgetArea)

    host.resize(1060, 800)
    qtbot.waitUntil(lambda: properties in host.tabifiedDockWidgets(media))
    assert host.tabifiedDockWidgets(viewer) == []
    assert not properties.isHidden() and not media.isHidden()
    properties.raise_()
    qtbot.waitUntil(lambda: not properties.visibleRegion().isEmpty())

    host.resize(1360, 800)
    qtbot.waitUntil(lambda: properties not in host.tabifiedDockWidgets(media))
    assert host.dock_edit_info in host.tabifiedDockWidgets(properties)
    assert host.dock_edit_effects in host.tabifiedDockWidgets(media)
    assert not media.visibleRegion().isEmpty() and not properties.visibleRegion().isEmpty()

    screen_width = 1000
    host.resize(1290, 800)
    qtbot.waitUntil(lambda: properties in host.tabifiedDockWidgets(media))
    qtbot.waitUntil(lambda: host.width() <= screen_width)
    assert not viewer.visibleRegion().isEmpty()


def test_apply_preset_unknown_or_unavailable(qapp):
    host = _WorkspaceHost()
    assert host.apply_edit_workspace_preset("nonexistent") is False
    no_edit = _WorkspaceHost(edit_available=False)
    assert no_edit.apply_edit_workspace_preset("editing") is False
    # 未应用时不动任何 dock。
    assert _visible(no_edit) == set()


def test_apply_preset_switches_to_edit_page(qapp):
    host = _WorkspaceHost()
    host.current_page = "Color"

    switched: list = []

    def _fake_switch(page_name, force: bool = False):
        switched.append(page_name)
        host.current_page = page_name

    host.switch_to_page = _fake_switch  # type: ignore[assignment]
    assert host.apply_edit_workspace_preset("editing") is True
    assert switched == ["Edit"]
    assert _visible(host) == set(get_workspace_preset("editing").visible_docks)


def test_layout_combobox_lists_presets_before_custom_layouts(qapp):
    host = _WorkspaceHost()
    from PyQt6.QtWidgets import QComboBox

    host.layout_combo = QComboBox()
    host.db = _FakeLayoutDb()
    host._current_work_mode = lambda: "all"  # type: ignore[assignment]
    host.update_layout_combobox()
    assert host.layout_combo.count() == 3
    assert host.layout_combo.view().isRowHidden(0)
    assert host.layout_combo.itemText(1) == "默认布局"
    assert host.layout_combo.itemData(1) == "editing"
    assert host.layout_combo.itemText(2) == "My Layout"
    assert host.layout_combo.itemData(2) is None


def test_layout_combo_activated_routes_preset_and_custom(qapp):
    host = _WorkspaceHost()
    from PyQt6.QtWidgets import QComboBox

    host.layout_combo = QComboBox()
    host.db = _FakeLayoutDb()
    host._current_work_mode = lambda: "all"  # type: ignore[assignment]
    host.update_layout_combobox()

    applied_presets: list = []
    applied_layouts: list = []
    host.apply_edit_workspace_preset = lambda pid: applied_presets.append(pid) or True  # type: ignore[assignment]
    host.apply_layout = lambda name: applied_layouts.append(name)  # type: ignore[assignment]

    host.on_layout_combo_activated(1)
    assert applied_presets == ["editing"]
    assert applied_layouts == []
    assert host.layout_combo.currentIndex() == 1

    host.on_layout_combo_activated(2)  # 自定义布局
    assert applied_layouts == ["My Layout"]
    assert applied_presets == ["editing"]

    host.on_layout_combo_activated(0)  # 占位项不动作
    assert applied_presets == ["editing"]
    assert applied_layouts == ["My Layout"]


def test_workspace_menu_lists_presets_when_edit_available(qapp):
    from PyQt6.QtWidgets import QMenu

    host = _WorkspaceHost()
    host.workspace_menu = QMenu()
    host.update_workspace_menu()
    labels = [a.text() for a in host.workspace_menu.actions()]
    assert labels == ["默认布局"]
    host.workspace_menu.actions()[0].trigger()
    assert "dockAudioMixer" in _visible(host)
