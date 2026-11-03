"""R04: Effects 实例与参数编辑事务（手势 start/change/commit/cancel）。

验收（计划 R04）：100 次 valueChange = 1 undo；取消 = 0 undo；下拉/bool/
确认颜色各 1 command；切目标自动落定策略；异步晚到结果守卫（删节点/删
实例/换会话后丢弃）。
"""
from __future__ import annotations

import pytest

pytest.importorskip("PyQt6")

from tests.app.test_color_resource_controller import (  # noqa: F401
    SCHEMA,
    _lut_valid,
    _project_with_graph,
    _schema,
    patched_service,
    qapp,  # noqa: F401
)
from tests.app.test_color_resource_controller import _ResourceHarness


@pytest.fixture()
def rig(qapp, patched_service):  # noqa: F811
    project, stack_id, graph = _project_with_graph()
    harness = _ResourceHarness(qapp, project)
    harness.graph.host.on_clip_stack_loaded(stack_id, scope_owner_id=stack_id)
    harness.stack_id = stack_id
    harness.corrector = next(n.id for n in harness.graph.panel.snapshot().nodes.values()
                             if n.kind.value == "corrector")
    harness.controller.activate_effect(
        {"id": "grain", "kind": "effect", "available": True})
    node = harness.graph_node(harness.corrector)
    harness.instance = node.legacy_payload["plugins"][-1]["instance_id"]
    return harness


def _graph_obj(rig):
    return rig.graph.holder["project"].color_stacks[rig.stack_id].color_graph


def _add_helpers():
    def graph_node(self, node_id):
        return next(n for n in _graph_obj(self).nodes if n.node_id == node_id)

    _ResourceHarness.graph_node = graph_node


_add_helpers()


def _param(rig, name="amount"):
    plugins = rig.graph_node(rig.corrector).legacy_payload["plugins"]
    return next(p["params"][name] for p in plugins
                if p["instance_id"] == rig.instance)


def _depth(rig) -> int:
    return len(rig.graph.cmd_stack._undo_stack)


def test_hundred_change_frames_are_one_undo(rig) -> None:
    base = _depth(rig)
    rig.controller.edit('set_parameter', {'instance_id': rig.instance,
                                          'name': 'amount', 'value': 0.1,
                                          'phase': 'start'})
    for i in range(100):
        rig.controller.edit('set_parameter', {'instance_id': rig.instance,
                                              'name': 'amount',
                                              'value': i / 100.0,
                                              'phase': 'change'})
    assert _depth(rig) == base  # 手势期间零 command（缓冲）
    rig.controller.edit('set_parameter', {'instance_id': rig.instance,
                                          'name': 'amount', 'value': 0.99,
                                          'phase': 'commit'})
    assert _depth(rig) == base + 1  # 一次手势一个 command
    assert abs(_param(rig) - 0.99) < 1e-9
    assert rig.graph.host.undo()
    assert abs(_param(rig) - 0.2) < 1e-9  # 一次 undo 回到默认


def test_cancel_gesture_is_zero_undo(rig) -> None:
    base = _depth(rig)
    before = _graph_obj(rig).to_dict()
    rig.controller.edit('set_parameter', {'instance_id': rig.instance,
                                          'name': 'amount', 'value': 0.1,
                                          'phase': 'start'})
    for value in (0.3, 0.5, 0.8):
        rig.controller.edit('set_parameter', {'instance_id': rig.instance,
                                              'name': 'amount', 'value': value,
                                              'phase': 'change'})
    rig.controller.edit('set_parameter', {'instance_id': rig.instance,
                                          'name': 'amount', 'phase': 'cancel'})
    assert _depth(rig) == base  # 取消零 command
    assert _graph_obj(rig).to_dict() == before  # 图字节不变
    assert abs(_param(rig) - 0.2) < 1e-9  # 值回到权威


def test_discrete_edits_are_single_commands(rig) -> None:
    base = _depth(rig)
    # select（离散）、bool（离散）、color（确认）各一次 command
    rig.controller.edit('set_parameter', {'instance_id': rig.instance,
                                          'name': 'mode', 'value': 1})
    rig.controller.edit('set_parameter', {'instance_id': rig.instance,
                                          'name': 'toggle', 'value': 1})
    rig.controller.edit('set_parameter', {'instance_id': rig.instance,
                                          'name': 'tint', 'value': [0.1, 0.2, 0.3]})
    assert _depth(rig) == base + 3
    node = rig.graph_node(rig.corrector)
    params = next(p["params"] for p in node.legacy_payload["plugins"]
                  if p["instance_id"] == rig.instance)
    assert params["mode"] == 1 and params["toggle"] == 1
    assert list(params["tint"]) == [0.1, 0.2, 0.3]


def test_mid_frame_schema_rejection_keeps_gesture(rig) -> None:
    rig.controller.edit('set_parameter', {'instance_id': rig.instance,
                                          'name': 'amount', 'value': 0.1,
                                          'phase': 'start'})
    rig.controller.edit('set_parameter', {'instance_id': rig.instance,
                                          'name': 'amount', 'value': 0.4,
                                          'phase': 'change'})
    # 越界中间帧被丢弃，手势仍可继续
    rig.controller.edit('set_parameter', {'instance_id': rig.instance,
                                          'name': 'amount', 'value': 5.0,
                                          'phase': 'change'})
    assert rig.rejections and rig.rejections[-1]["code"] == "param_out_of_range"
    rig.controller.edit('set_parameter', {'instance_id': rig.instance,
                                          'name': 'amount', 'value': 0.6,
                                          'phase': 'change'})
    rig.controller.edit('set_parameter', {'instance_id': rig.instance,
                                          'name': 'amount', 'value': 0.6,
                                          'phase': 'commit'})
    assert abs(_param(rig) - 0.6) < 1e-9


def test_target_switch_auto_flushes_gesture(rig) -> None:
    base = _depth(rig)
    rig.controller.edit('set_parameter', {'instance_id': rig.instance,
                                          'name': 'amount', 'value': 0.1,
                                          'phase': 'start'})
    rig.controller.edit('set_parameter', {'instance_id': rig.instance,
                                          'name': 'amount', 'value': 0.55,
                                          'phase': 'change'})
    # 切换活动节点 → 水合路径先落定在途手势（不丢已拖数据）
    view = rig.graph.panel.view
    others = [n.id for n in view.state.snapshot().nodes.values()
              if n.kind.value == "corrector" and n.id != rig.corrector]
    assert others, "fixture needs a second corrector"
    view.state.graph.active_id = others[0]
    rig.controller.refresh_target()
    assert _depth(rig) == base + 1  # 自动落定 = 一个 command
    assert abs(_param(rig) - 0.55) < 1e-9


def test_async_token_guard_discards_stale_results(rig) -> None:
    token = rig.controller.issue_async_token(rig.instance)
    assert token is not None
    assert rig.controller.verify_async_token(token)
    # 实例被删 → 晚到结果丢弃
    rig.controller.edit('remove_instance', {'instance_id': rig.instance})
    assert not rig.controller.verify_async_token(token)
    # 重新添加后：新实例新令牌有效
    rig.controller.activate_effect(
        {"id": "grain", "kind": "effect", "available": True})
    node = rig.graph_node(rig.corrector)
    new_instance = node.legacy_payload["plugins"][-1]["instance_id"]
    token2 = rig.controller.issue_async_token(new_instance)
    assert token2 is not None and rig.controller.verify_async_token(token2)
    # 会话代际切换 → 丢弃
    coordinator = rig.graph.host._coordinator
    coordinator.generation += 1
    assert not rig.controller.verify_async_token(token2)
    coordinator.generation -= 1


def test_lut_strength_gesture_coalesces(rig) -> None:
    lut_entry = {"id": "topos-res://lut_good", "kind": "lut",
                 "available": True, "resource_uri": "topos-res://lut_good"}
    rig.controller.activate_resource(lut_entry)
    base = _depth(rig)
    rig.controller.edit('set_lut_strength', {'strength': 0.9, 'phase': 'start'})
    for value in (0.2, 0.3, 0.42):
        rig.controller.edit('set_lut_strength', {'strength': value,
                                                 'phase': 'change'})
    assert _depth(rig) == base
    rig.controller.edit('set_lut_strength', {'strength': 0.42, 'phase': 'commit'})
    assert _depth(rig) == base + 1
    node = rig.graph_node(rig.corrector)
    assert abs(node.params["resources"]["lut_strength"] - 0.42) < 1e-9
    assert rig.graph.host.undo()
    node = rig.graph_node(rig.corrector)
    assert abs(node.params["resources"]["lut_strength"] - 1.0) < 1e-9
