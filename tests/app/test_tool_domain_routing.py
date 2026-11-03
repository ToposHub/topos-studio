"""N03：Qualifier/Window 工具面板的图模式写路由（typed handle）。

验收（计划 N03）：图编辑上下文中工具参数**实际落正确 node**（命令进
graph command 栈、一次手势一次 undo）；legacy 引擎只作同值预览；水合
回填回流不产生命令；legacy 上下文行为零变更。窗口/结构路由见
tests/app/test_tool_domain_routing_windows.py（N03-c）。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

pytest.importorskip("PyQt6")

from src.app.controllers.color_controller import ColorControllerMixin
from tests.app.test_color_resource_controller import _project_with_graph, qapp
from tests.app.test_color_graph_app_host import _Harness


class _QualifierEngine:
    """记录型引擎：qualifier 写入留痕（预览通道）。"""

    def __init__(self) -> None:
        self.qualifier_writes: list[dict] = []
        self.active_layer_index = 0
        self.layers: list = []

    def set_qualifier_params(self, params: dict) -> None:
        self.qualifier_writes.append(dict(params))


class _ToolHost(ColorControllerMixin):
    """Mixin 宿主（self.* 解析到本实例属性）。"""

    def __init__(self, graph_host, engine) -> None:
        self.color_graph_host = graph_host
        self.engine = engine

    def _invalidate_video_render_params_if_playing(self) -> None:
        pass


@pytest.fixture()
def rig(qapp):
    project, stack_id, graph = _project_with_graph()
    harness = _Harness(qapp, project)
    harness.host.on_clip_stack_loaded(stack_id, scope_owner_id=stack_id)
    engine = _QualifierEngine()
    host = _ToolHost(harness.host, engine)
    corrector = next(n.id for n in harness.panel.snapshot().nodes.values()
                     if n.kind.value == "corrector")
    harness.panel.view.state.graph.active_id = corrector
    harness.panel.view._selection_changed()
    return SimpleNamespace(harness=harness, host=host, engine=engine,
                           stack_id=stack_id, corrector=corrector)


def _node_params(rig):
    return next(
        n.params for n in
        rig.harness.holder["project"].color_stacks[rig.stack_id].color_graph.nodes
        if n.node_id == rig.corrector)


def _depth(rig) -> int:
    return len(rig.harness.cmd_stack._undo_stack)


def test_qualifier_routes_to_graph_command(rig) -> None:
    base = _depth(rig)
    rig.host.on_qualifier_params_changed(
        {"enabled": True, "hue_center": 0.3, "hue_width": 0.2})
    # 手势在途：权威图未变，引擎同值预览已写
    assert "qualifier" not in _node_params(rig)
    assert rig.engine.qualifier_writes == [
        {"enabled": True, "hue_center": 0.3, "hue_width": 0.2}]
    assert rig.harness.host.grade_gesture_open()
    # 收束（interaction_ended / 自冲刷同语义）→ 一次 command
    assert rig.host._flush_qualifier_gesture() is None
    assert _depth(rig) == base + 1
    qualifier = dict(_node_params(rig).get("qualifier") or {})
    assert qualifier["hue_center"] == pytest.approx(0.3)
    assert qualifier["enabled"] is True


def test_qualifier_diff_suppresses_noop_emits(rig) -> None:
    rig.host.on_qualifier_params_changed({"enabled": True, "hue_center": 0.3})
    rig.host._flush_qualifier_gesture()
    base = _depth(rig)
    # 水合回填重发射（同值）：diff 为空 → 不产生命令
    rig.host.on_qualifier_params_changed(
        {"enabled": True, "hue_center": 0.3})
    rig.host._flush_qualifier_gesture()
    assert _depth(rig) == base


def test_qualifier_hydration_guard_skips_entirely(rig) -> None:
    rig.host._color_stack_hydration_in_progress = True
    rig.host.on_qualifier_params_changed({"enabled": True})
    assert rig.engine.qualifier_writes == []
    assert not rig.harness.host.grade_gesture_open()
    del rig.host._color_stack_hydration_in_progress


def test_qualifier_legacy_mode_writes_engine_directly() -> None:
    engine = _QualifierEngine()
    host = _ToolHost(None, engine)  # 无图宿主 → legacy
    host.on_qualifier_params_changed({"enabled": True, "hue_center": 0.1})
    assert engine.qualifier_writes == [
        {"enabled": True, "hue_center": 0.1}]


def test_panel_qualifier_projection_overlays_params_domain(rig) -> None:
    """AppHost.panel_qualifier_params：legacy 基底 + params.qualifier 覆盖。"""
    rig.host.on_qualifier_params_changed({"enabled": True, "hue_center": 0.3})
    rig.host._flush_qualifier_gesture()
    # 重选触发 handle 刷新后读投影
    projection = rig.harness.host.panel_qualifier_params()
    assert projection["hue_center"] == pytest.approx(0.3)
    assert projection["enabled"] is True
