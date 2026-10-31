"""Color workspace presets（P1-COLOR-19）测试。

覆盖：
- workspace_presets：内置预设完整性、sanitize 过滤/迁移、用户预设
  保存/加载/删除/重名替换、损坏数据容忍
- ColorWorkspaceMixin：Color 页外不动作、应用切换 dock 可见性 + scope 源、
  保存当前面板组合、active 匹配、隔离性（apply 不写 config、save 只写
  color_workspaces 键，不触碰 LayoutStore）
"""
from unittest.mock import MagicMock

import pytest

from src.features.color.core.workspace_presets import (
    CONFIG_KEY, ColorWorkspacePreset, builtin_color_workspaces,
    delete_user_workspace, find_workspace, load_user_workspaces,
    sanitize_preset, save_user_workspace,
)

# ---------------------------------------------------------------------------
# 纯数据模块
# ---------------------------------------------------------------------------


class StubConfig:
    def __init__(self):
        self.store = {}

    def get(self, key):
        return self.store.get(key)

    def set(self, key, value):
        self.store[key] = value


def test_builtin_presets_complete():
    presets = builtin_color_workspaces()
    ids = [p.preset_id for p in presets]
    assert len(ids) == len(set(ids))
    assert set(ids) == {
        "builtin.primary", "builtin.secondary", "builtin.hdr",
        "builtin.tracking", "builtin.scopes", "builtin.delivery",
    }
    for preset in presets:
        assert preset.builtin
        assert preset.title_key
        assert preset.name
        # 任何工作区都保留 viewer
        assert "workspace.viewport" in preset.visible_panels
        # 面板 id 只允许三个已知前缀，且无重复
        for panel_id in preset.visible_panels:
            assert panel_id.startswith(("workspace.", "color.", "advanced."))
    # 任务导向组合互不相同
    panel_sets = [frozenset(p.visible_panels) for p in presets]
    assert len(panel_sets) == len(set(panel_sets))


def test_builtin_preset_contents():
    presets = {p.preset_id: p for p in builtin_color_workspaces()}
    assert "color.qualifier" in presets["builtin.secondary"].visible_panels
    assert "color.tracker" in presets["builtin.tracking"].visible_panels
    assert "color.analysis" in presets["builtin.scopes"].visible_panels
    assert "workspace.export" in presets["builtin.delivery"].visible_panels
    assert "color.raw" in presets["builtin.hdr"].visible_panels
    assert presets["builtin.hdr"].scope_source == "graded"


def test_sanitize_filters_and_migrates():
    preset = ColorWorkspacePreset(
        preset_id="user.x", name="X",
        visible_panels=frozenset({
            "workspace.viewport", "color.grading", "color.gone_in_v2",
            "workspace.library", "workspace.export", "color.timeline_clips",
        }))
    # 未知面板被丢弃（升级迁移），viewport 永远保留
    resolved = sanitize_preset(preset, ["workspace.viewport", "color.grading"], "all")
    assert resolved == frozenset({"workspace.viewport", "color.grading"})
    # video 模式隐藏图像专属面板
    resolved_video = sanitize_preset(
        preset,
        ["workspace.viewport", "color.grading", "workspace.library",
         "workspace.export", "color.timeline_clips"],
        "video")
    assert resolved_video == frozenset({"workspace.viewport", "color.grading",
                                        "color.timeline_clips"})
    # image 模式隐藏时间线面板
    resolved_image = sanitize_preset(
        preset,
        ["workspace.viewport", "color.timeline_clips", "workspace.library"],
        "image")
    assert resolved_image == frozenset({"workspace.viewport", "workspace.library"})

    # Legacy secondary-color workspaces promote any former remapping tool to
    # the new combined Color Mapping dock.
    legacy = ColorWorkspacePreset(
        preset_id="user.legacy",
        name="Legacy",
        visible_panels=frozenset({"color.warper"}),
    )
    assert sanitize_preset(legacy, ["color.mapping"], "all") == frozenset({
        "workspace.viewport", "color.mapping",
    })


def test_user_workspace_roundtrip_and_replace():
    config = StubConfig()
    preset = save_user_workspace(config, "My Look", ["color.grading", "color.lut"])
    assert preset is not None and preset.preset_id.startswith("user.")
    assert "workspace.viewport" in preset.visible_panels  # 自动保留 viewer
    loaded = load_user_workspaces(config)
    assert [p.name for p in loaded] == ["My Look"]
    assert loaded[0].visible_panels == preset.visible_panels

    # 同名替换而不是追加
    replaced = save_user_workspace(config, "My Look", ["color.qualifier"], "source")
    assert replaced.preset_id != preset.preset_id
    loaded = load_user_workspaces(config)
    assert len(loaded) == 1
    assert loaded[0].name == "My Look"
    assert loaded[0].scope_source == "source"
    assert "color.qualifier" in loaded[0].visible_panels

    # 删除
    assert delete_user_workspace(config, replaced.preset_id) is True
    assert load_user_workspaces(config) == []
    assert delete_user_workspace(config, replaced.preset_id) is False
    # 内置预设不可删
    assert delete_user_workspace(config, "builtin.primary") is False


def test_user_workspace_validation():
    config = StubConfig()
    assert save_user_workspace(config, "   ", []) is None
    assert save_user_workspace(config, "BadScope", [], "nonsense") is not None
    assert load_user_workspaces(config)[0].scope_source == ""
    # 损坏数据容忍：非 dict / 缺字段 / 内置 id 伪装 / 重复 id
    config.set(CONFIG_KEY, {"items": [
        "not-a-dict",
        {"name": "no-id"},
        {"preset_id": "builtin.fake", "name": "Fake"},
        {"preset_id": "user.a", "name": ""},
        {"preset_id": "user.dup", "name": "Dup", "visible_panels": "bad"},
        {"preset_id": "user.dup", "name": "Dup", "visible_panels": [1, "color.grading"]},
    ]})
    loaded = load_user_workspaces(config)
    assert len(loaded) == 1
    assert loaded[0].name == "Dup"
    assert loaded[0].visible_panels == frozenset({"color.grading", "workspace.viewport"})
    # 完全缺失 / 类型不对
    assert load_user_workspaces(None) == []
    assert load_user_workspaces(StubConfig()) == []


def test_find_workspace_and_display_name():
    config = StubConfig()
    saved = save_user_workspace(config, "Look", ["color.grading"])
    assert find_workspace(config, "builtin.scopes").name == "Scopes"
    assert find_workspace(config, saved.preset_id).name == "Look"
    assert find_workspace(config, "missing") is None

    builtin = builtin_color_workspaces()[0]
    assert builtin.display_name(lambda k, d=None, **kw: {"workspace.primary": "一级"}.get(k, d)) == "一级"
    assert builtin.display_name(None) == builtin.name
    assert builtin.display_name(lambda k, d=None, **kw: d) == builtin.name


# ---------------------------------------------------------------------------
# ColorWorkspaceMixin（bare mixin，不启动 Qt）
# ---------------------------------------------------------------------------
from src.app.controllers.color_workspace_controller import ColorWorkspaceMixin
from src.shared.panels.registry import PanelDescriptor

_PANEL_IDS = [
    "workspace.viewport", "color.timeline_clips", "workspace.presets",
    "color.grading", "color.curves", "color.lut", "color.qualifier",
    "color.masks", "color.analysis", "workspace.export",
]


class _FakeDock:
    """带真实可见状态的假 dock：setVisible 立即生效，isVisible 可查询。"""

    def __init__(self, visible: bool):
        self._visible = visible
        self.setVisible = MagicMock(side_effect=self._set_visible)

    def _set_visible(self, value: bool) -> None:
        self._visible = bool(value)

    def isVisible(self) -> bool:
        return self._visible


def _make_controller(page="Color", registered=None, work_mode="all"):
    ctrl = ColorWorkspaceMixin.__new__(ColorWorkspaceMixin)
    ctrl.current_page = page
    ctrl.config_manager = StubConfig()
    ctrl.page_layouts = {}
    ctrl.canvas = None
    ctrl.scope_panel = MagicMock()
    ctrl._work_mode = work_mode
    ctrl._registered = _PANEL_IDS if registered is None else registered
    ctrl._saved_store_calls = []
    initial_visible = {"workspace.viewport", "color.timeline_clips", "color.grading"}
    ctrl._docks = {pid: _FakeDock(pid in initial_visible) for pid in ctrl._registered}
    ctrl._current_work_mode = lambda: ctrl._work_mode
    ctrl._get_registered_panel_ids = lambda: set(ctrl._registered)
    ctrl._get_enabled_features = lambda: {"color", "library", "edit"}
    ctrl._enforce_page_dock_visibility = lambda: None
    ctrl.saveState = lambda: b"state"
    ctrl._save_current_layout_to_store = (
        lambda page_id: ctrl._saved_store_calls.append(page_id))
    ctrl._color_workspace_panel_descriptors = lambda: [
        PanelDescriptor(
            panel_id=pid, title_key=f"panel.{pid}", fallback_title=pid,
            dock_attr="dock_x")
        for pid in ctrl._registered
    ]
    ctrl._color_workspace_docks = lambda: list(ctrl._docks.items())
    return ctrl


def test_apply_workspace_sets_visibility_and_scope():
    ctrl = _make_controller()
    assert ctrl.apply_color_workspace("builtin.scopes") is True
    # scopes 工作区只显示 viewer/timeline/scopes
    shown = {pid for pid, dock in ctrl._docks.items() if dock.isVisible()}
    assert shown == {"workspace.viewport", "color.timeline_clips", "color.analysis"}
    assert ctrl.scope_panel.set_scope_source.called is False  # scopes 预设无 scope 源
    assert ctrl.page_layouts["Color"] == b"state"
    assert ctrl._saved_store_calls == ["Color"]


def test_apply_workspace_with_scope_source():
    ctrl = _make_controller()
    assert ctrl.apply_color_workspace("builtin.hdr") is True
    ctrl.scope_panel.set_scope_source.assert_called_once_with("graded")


def test_apply_workspace_blocked_off_color_page():
    ctrl = _make_controller(page="Edit")
    assert ctrl.apply_color_workspace("builtin.primary") is False
    for dock in ctrl._docks.values():
        dock.setVisible.assert_not_called()
    assert ctrl.page_layouts == {}


def test_apply_unknown_workspace():
    ctrl = _make_controller()
    assert ctrl.apply_color_workspace("no.such") is False


def test_save_and_active_roundtrip():
    ctrl = _make_controller()
    # 初始可见集：viewport + timeline + grading
    assert ctrl.active_color_workspace_id() is None
    preset = ctrl.save_current_color_workspace("My Primary")
    assert preset is not None
    assert preset.visible_panels == frozenset({
        "workspace.viewport", "color.timeline_clips", "color.grading"})
    # 保存后 active 匹配到该预设
    assert ctrl.active_color_workspace_id() == preset.preset_id
    # 应用别的再切回来仍匹配
    ctrl.apply_color_workspace("builtin.scopes")
    assert ctrl.active_color_workspace_id() == "builtin.scopes"
    ctrl.apply_color_workspace(preset.preset_id)
    assert ctrl.active_color_workspace_id() == preset.preset_id
    # 删除后不再匹配
    assert ctrl.delete_color_workspace(preset.preset_id) is True
    assert ctrl.active_color_workspace_id() is None


def test_apply_does_not_touch_user_config_and_save_uses_own_key():
    ctrl = _make_controller()
    ctrl.apply_color_workspace("builtin.primary")
    # 应用内置/用户预设只改布局，不写用户工作区表
    assert ctrl.config_manager.store == {}
    preset = ctrl.save_current_color_workspace("Mine")
    assert set(ctrl.config_manager.store) == {CONFIG_KEY}
    # LayoutStore 侧只经由 _save_current_layout_to_store（页面布局通道）
    assert ctrl._saved_store_calls == ["Color"]
    assert load_user_workspaces(ctrl.config_manager)[0].name == preset.name


def test_save_blocked_off_color_page():
    ctrl = _make_controller(page="Deliver")
    assert ctrl.save_current_color_workspace("Nope") is None
