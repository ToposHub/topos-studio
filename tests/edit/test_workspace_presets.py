"""P1-24 内建工作区预设（Editing/Audio/Captions/Review）测试。

两层验证：
- 纯数据层（workspace_presets.py）：预设规格自洽、与出厂 Edit 布局同构。
- Qt 宿主层：真 QMainWindow + DockLayoutMixin，验证 apply 后 dock 可见性/
  tab 化/快照持久化，以及 layout_combo 分流。
"""

from __future__ import annotations

import pytest
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QDockWidget, QMainWindow, QWidget

from src.app.controllers.dock_layout_controller import DockLayoutMixin
from src.features.edit.core.workspace_presets import (
    EDIT_WORKSPACE_MANAGED_DOCKS,
    WorkspacePreset,
    get_workspace_preset,
    list_workspace_presets,
)

pytestmark = pytest.mark.edit_ui


# ─────────────────────────────────────────────────────────────────────────────
# 纯数据层
# ─────────────────────────────────────────────────────────────────────────────

def test_preset_ids_unique_and_stable():
    presets = list_workspace_presets()
    ids = [p.preset_id for p in presets]
    assert sorted(ids) == ["audio", "captions", "editing", "review"]
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
        "dockEditInfo",
    }
    # Source lives inside Viewport; only the right inspector stack is tabified.
    assert "dockSourceViewer" not in EDIT_WORKSPACE_MANAGED_DOCKS
    assert ("dockProperties", "dockEditEffects", "dockEditInfo") in preset.tabify_groups


def test_task_presets_swap_panels_meaningfully():
    audio = get_workspace_preset("audio")
    assert "dockAudioMixer" in audio.visible_docks
    assert "dockProperties" not in audio.visible_docks
    assert audio.focus_dock == "dockAudioMixer"

    captions = get_workspace_preset("captions")
    assert "dockCaptionWorkspace" in captions.visible_docks
    assert captions.focus_dock == "dockCaptionWorkspace"

    review = get_workspace_preset("review")
    # 审阅隐藏一切编辑工具，只保留监视器/时间线/信息。
    assert set(review.visible_docks) == {
        "dockProgramViewer",
        "dockEditTimeline",
        "dockEditInfo",
    }


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
    def list_layouts(self, work_mode=None):
        return ["My Layout"]


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


def test_apply_audio_then_editing_flips_visibility(qapp):
    host = _WorkspaceHost()
    assert host.apply_edit_workspace_preset("audio") is True
    assert _visible(host) == set(get_workspace_preset("audio").visible_docks)
    assert host.apply_edit_workspace_preset("editing") is True
    assert _visible(host) == set(get_workspace_preset("editing").visible_docks)
    # Audio Mixer 在切回剪辑预设后被收起。
    assert not host.docks_by_name["dockAudioMixer"].isVisible()


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
    assert host.apply_edit_workspace_preset("review") is True
    assert switched == ["Edit"]
    assert _visible(host) == set(get_workspace_preset("review").visible_docks)


def test_layout_combobox_lists_presets_before_custom_layouts(qapp):
    host = _WorkspaceHost()
    from PyQt6.QtWidgets import QComboBox

    host.layout_combo = QComboBox()
    host.db = _FakeLayoutDb()
    host._current_work_mode = lambda: "all"  # type: ignore[assignment]
    host.update_layout_combobox()
    # 占位 + 4 内建预设 + 1 自定义布局。
    assert host.layout_combo.count() == 6
    assert host.layout_combo.itemText(1) == "剪辑"
    assert host.layout_combo.itemData(1) == "editing"
    assert host.layout_combo.itemText(5) == "My Layout"
    assert host.layout_combo.itemData(5) is None


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

    host.on_layout_combo_activated(3)  # 字幕预设（1=剪辑 2=音频 3=字幕）
    assert applied_presets == ["captions"]
    assert applied_layouts == []
    assert host.layout_combo.currentIndex() == 0  # 回到占位项

    host.on_layout_combo_activated(5)  # 自定义布局
    assert applied_layouts == ["My Layout"]
    assert applied_presets == ["captions"]

    host.on_layout_combo_activated(0)  # 占位项不动作
    assert applied_presets == ["captions"]
    assert applied_layouts == ["My Layout"]


def test_workspace_menu_lists_presets_when_edit_available(qapp):
    from PyQt6.QtWidgets import QMenu

    host = _WorkspaceHost()
    host.workspace_menu = QMenu()
    host.update_workspace_menu()
    labels = [a.text() for a in host.workspace_menu.actions()]
    assert labels == ["剪辑", "音频", "字幕", "审阅"]
    # 点击"音频"菜单项 → 应用 audio 预设。
    host.workspace_menu.actions()[1].trigger()
    assert "dockAudioMixer" in _visible(host)

    no_edit = _WorkspaceHost(edit_available=False)
    no_edit.workspace_menu = QMenu()
    no_edit.update_workspace_menu()
    actions = no_edit.workspace_menu.actions()
    assert len(actions) == 1
    assert not actions[0].isEnabled()
