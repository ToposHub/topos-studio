"""R10: 旧写入入口的图模式路由（一次命令、引擎无双写）+ 库偏好持久化。

验收（计划 R10）：R00 审计的 A1–A5 入口在图会话激活时经正式命令适配器
（command 计数=1），legacy engine 不被触碰；偏好（收藏/视图）经用户配置
存取，缺失收藏可诊断；tools_panel 死代码（D1）清除。
"""
from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("PyQt6")

from PyQt6.QtWidgets import QApplication, QWidget
from types import SimpleNamespace

from src.features.color.ui.effects_browser_panel import ColorEffectsBrowser
from src.features.color.ui.lut_browser_panel import ColorLutBrowser
from src.features.color.ui.node_editor_panel import ColorNodePanel
from src.app.controllers.color_controller import ColorControllerMixin
from tests.app.test_color_resource_controller import (
    _ResourceHarness,
    _project_with_graph,
    qapp,
)


@pytest.fixture()
def patched_service(monkeypatch: pytest.MonkeyPatch) -> None:
    """路由测试用宽松资源权威（任意 topos-res 注册 LUT 均可）。"""
    from src.features.color.core.color_graph_service import ColorGraphService
    from src.features.edit.core.commands import color_commands
    monkeypatch.setattr(
        color_commands, "_graph_service",
        lambda: ColorGraphService(
            effect_schema_provider=lambda rid: [],
            lut_reference_validator=lambda ref: ref.startswith("topos-res://")))


class _RoutingHost(ColorControllerMixin):
    """Mixin 宿主（self.* 解析到本实例属性）。"""

    def __init__(self, controller, graph_host) -> None:
        self.color_resource_controller = controller
        self.color_graph_host = graph_host
        self.engine = _NoLegacyEngine()
        self.plugin_panel = SimpleNamespace(set_layer=lambda _l: None)
        self.layer_panel = SimpleNamespace(update_layers=lambda *_a: None)
        self.dock_plugin = SimpleNamespace(setVisible=lambda _v: None,
                                           raise_=lambda: None)
        self.status_bar = SimpleNamespace(showMessage=lambda *_a: None)

    def request_render(self) -> None:
        pass

    def _trigger_autosave(self) -> None:
        pass


class _NoLegacyEngine:
    """旧引擎双写哨兵：结构属性可用，写方法被路由后永不被调用。"""

    active_layer_index = 0
    layers: list = []

    @staticmethod
    def _forbidden(name):
        def boom(*_a, **_k):
            raise AssertionError(f"legacy engine {name} 双写")
        return boom

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        return self._forbidden(name)


class _RoutingHarness(_ResourceHarness):
    def __init__(self, qapp, project):
        super().__init__(qapp, project)
        self.mixin_host = _RoutingHost(self.controller, self.graph.host)


@pytest.fixture()
def rig(qapp, patched_service):
    project, stack_id, graph = _project_with_graph()
    harness = _RoutingHarness(qapp, project)
    harness.graph.host.on_clip_stack_loaded(stack_id, scope_owner_id=stack_id)
    harness.stack_id = stack_id
    harness.corrector = next(n.id for n in harness.graph.panel.snapshot().nodes.values()
                             if n.kind.value == "corrector")
    # 活动节点设为 corrector（路由目标）
    harness.graph.panel.view.state.graph.active_id = harness.corrector
    harness.controller.refresh_target()
    return harness


def _depth(rig) -> int:
    return len(rig.graph.cmd_stack._undo_stack)


def test_add_plugin_routes_to_graph_command(rig) -> None:
    base = _depth(rig)
    rig.mixin_host.add_plugin_to_current_layer("grain", "Grain")
    assert _depth(rig) == base + 1  # 一次命令
    node = next(n for n in rig.graph.holder["project"]
                .color_stacks[rig.stack_id].color_graph.nodes
                if n.node_id == rig.corrector)
    assert any(p.get("plugin_id") == "grain"
               for p in node.legacy_payload["plugins"])


def test_lut_entries_route_to_graph_commands(rig, tmp_path) -> None:
    cube = tmp_path / "route.cube"
    cube.write_text("LUT_3D_SIZE 2\n" + "0 0 0\n" * 7 + "1 1 1\n")
    base = _depth(rig)
    rig.mixin_host.apply_lut_from_path(str(cube))
    assert _depth(rig) == base + 1
    rig.mixin_host.on_lut_intensity_changed(0.42)
    assert _depth(rig) == base + 2
    rig.mixin_host.on_lut_reset()
    assert _depth(rig) == base + 3
    node = next(n for n in rig.graph.holder["project"]
                .color_stacks[rig.stack_id].color_graph.nodes
                if n.node_id == rig.corrector)
    assert "lut_ref" not in node.params.get("resources", {})


def test_legacy_mode_falls_back_when_no_session(rig) -> None:
    """图会话未激活（preview/无 controller）→ 路由返回 None（旧行为域）。"""
    host = _RoutingHost(None, None)
    assert host._graph_resource_route() is None


def test_browser_preferences_roundtrip(qtbot: Any) -> None:
    effects = ColorEffectsBrowser()
    lut = ColorLutBrowser()
    state = {"favorites": ["fx-a", "lut-b"], "collapsed": ["Texture"],
             "grid": True}
    effects.set_browser_state(state)
    lut.set_browser_state({"favorites": ["lut-b"], "zoom": 200, "grid": False})
    eff_out = effects.browser_state()
    assert eff_out["favorites"] == ["fx-a", "lut-b"]
    assert eff_out["collapsed"] == ["Texture"]
    assert eff_out["grid"] is True
    assert lut.browser_state()["favorites"] == ["lut-b"]
    # 缺失收藏可诊断（重命名/卸载条目）
    assert effects.missing_favorite_ids({"fx-z"}) == ["fx-a", "lut-b"]
    assert lut.missing_favorite_ids(set()) == ["lut-b"]
    # 非法载荷容错
    effects.set_browser_state("junk")
    assert effects.browser_state()["favorites"] == ["fx-a", "lut-b"]


def test_tools_panel_dead_code_removed() -> None:
    source = open("src/features/color/ui/tools_panel.py").read()
    assert "self.lut_panel.set_selected_lut" not in source
    assert "self.lut_panel.intensity_changed.connect" not in source
