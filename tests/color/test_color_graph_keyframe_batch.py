"""KF-03：关键帧批处理领域命令回归。

验收（计划 KF-03 行）：两节点批量一次事务一次 revision；批量 move
先删后插不自覆盖；锁定目标/帧冲突整批拒绝无部分写入；重复置值与
拖回原位不推进历史；显式键型（kind/start/end）随 KF-01 编解码持久化。
"""
from __future__ import annotations

import pytest

from src.features.color.core.color_graph_service import ColorGraphService


def _graph():
    from src.features.color.core.color_graph_migration import (
        migrate_legacy_stack,
    )
    from src.features.edit.core.project import EditProject
    from tests.color.fixtures.color_graph import f03_layer_stack
    project, sid = EditProject.create(name="kf03").create_color_stack(
        asset_id="a1", name="s")
    stack = project.color_stacks[sid].with_layers(
        [l.to_dict(None) for l in f03_layer_stack()])
    return migrate_legacy_stack(stack)


def _correctors(graph):
    return [n for n in graph.nodes if n.node_type == "corrector"]


def _params(graph, node_id):
    node = next(n for n in graph.nodes if n.node_id == node_id)
    return node.params.get("grade_keyframes") or {}


def _frames(graph, node_id, param):
    track = _params(graph, node_id).get(param) or {}
    return [int(k["frame"]) for k in (track.get("keyframes") or ())]


def _values(graph, node_id, param):
    track = _params(graph, node_id).get(param) or {}
    return {int(k["frame"]): k["value"]
            for k in (track.get("keyframes") or ())}


def test_batch_set_across_two_nodes_single_revision():
    graph = _graph()
    a, b, _c = _correctors(graph)
    service = ColorGraphService()
    result = service.apply_intent(graph, "batch_edit_keyframes", {"edits": [
        {"action": "set", "node_id": a.node_id, "param": "exposure",
         "frame": 12, "value": 0.4},
        {"action": "set", "node_id": b.node_id, "param": "contrast",
         "frame": 24, "value": -0.1},
    ]})
    assert result.ok, result.diagnostics
    assert result.graph.graph_revision == graph.graph_revision + 1  # 一次
    assert 12 in _frames(result.graph, a.node_id, "exposure")
    assert 24 in _frames(result.graph, b.node_id, "contrast")


def test_batch_move_adjacent_swap_no_self_overwrite():
    graph = _graph()
    a, _b, _c = _correctors(graph)
    service = ColorGraphService()
    seed = service.apply_intent(graph, "batch_edit_keyframes", {"edits": [
        {"action": "set", "node_id": a.node_id, "param": "exposure",
         "frame": 10, "value": 1.0},
        {"action": "set", "node_id": a.node_id, "param": "exposure",
         "frame": 11, "value": 2.0},
    ]})
    assert seed.ok
    swapped = service.apply_intent(seed.graph, "batch_edit_keyframes",
                                   {"edits": [
        {"action": "move", "node_id": a.node_id, "param": "exposure",
         "frame": 10, "to_frame": 11},
        {"action": "move", "node_id": a.node_id, "param": "exposure",
         "frame": 11, "to_frame": 10},
    ]})
    assert swapped.ok, swapped.diagnostics
    values = _values(swapped.graph, a.node_id, "exposure")
    assert values == {10: 2.0, 11: 1.0}  # 先删全部源帧再插入，不自覆盖


def test_move_target_conflict_rejects_whole_batch():
    graph = _graph()
    a, _b, _c = _correctors(graph)
    service = ColorGraphService()
    seed = service.apply_intent(graph, "batch_edit_keyframes", {"edits": [
        {"action": "set", "node_id": a.node_id, "param": "exposure",
         "frame": 10, "value": 1.0},
        {"action": "set", "node_id": a.node_id, "param": "exposure",
         "frame": 20, "value": 2.0},
    ]})
    revision = seed.graph.graph_revision
    conflict = service.apply_intent(seed.graph, "batch_edit_keyframes",
                                    {"edits": [
        {"action": "move", "node_id": a.node_id, "param": "exposure",
         "frame": 10, "to_frame": 15},
        {"action": "move", "node_id": a.node_id, "param": "exposure",
         "frame": 20, "to_frame": 15},
    ]})
    assert not conflict.ok
    assert conflict.diagnostics[0].code == "frame_conflict"
    assert conflict.graph.graph_revision == revision  # 无部分写入
    assert _values(conflict.graph, a.node_id, "exposure") == {
        10: 1.0, 20: 2.0}


def test_locked_node_rejects_whole_batch():
    graph = _graph()
    a, b, _c = _correctors(graph)
    service = ColorGraphService()
    locked = service.apply_intent(graph, "set_edit_lock",
                                  {"node_ids": [b.node_id], "locked": True})
    assert locked.ok
    revision = locked.graph.graph_revision
    result = service.apply_intent(locked.graph, "batch_edit_keyframes",
                                  {"edits": [
        {"action": "set", "node_id": a.node_id, "param": "exposure",
         "frame": 8, "value": 0.3},
        {"action": "set", "node_id": b.node_id, "param": "exposure",
         "frame": 8, "value": 0.3},
    ]})
    assert not result.ok
    assert result.diagnostics[0].code == "node_locked"
    assert result.graph.graph_revision == revision
    assert _frames(result.graph, a.node_id, "exposure") == []  # A 未被写入


def test_dynamics_persists_explicit_attributes():
    graph = _graph()
    a, _b, _c = _correctors(graph)
    service = ColorGraphService()
    seeded = service.apply_intent(graph, "batch_edit_keyframes", {"edits": [
        {"action": "set", "node_id": a.node_id, "param": "exposure",
         "frame": 10, "value": 1.0},
    ]})
    weighted = service.apply_intent(seeded.graph, "batch_edit_keyframes",
                                    {"edits": [
        {"action": "dynamics", "node_id": a.node_id, "param": "exposure",
         "frame": 10, "kind": "dynamic", "start": 0.25, "end": 0.75},
    ]})
    assert weighted.ok, weighted.diagnostics
    entry = next(k for k in (_params(weighted.graph, a.node_id)
                             ["exposure"]["keyframes"])
                 if int(k["frame"]) == 10)
    assert entry["kind"] == "dynamic"
    assert entry["start"] == 0.25 and entry["end"] == 0.75


def test_convert_to_static_normalizes_to_hold():
    graph = _graph()
    a, _b, _c = _correctors(graph)
    service = ColorGraphService()
    seeded = service.apply_intent(graph, "batch_edit_keyframes", {"edits": [
        {"action": "set", "node_id": a.node_id, "param": "exposure",
         "frame": 10, "value": 1.0, "kind": "dynamic", "start": 0.5,
         "end": 0.5},
    ]})
    converted = service.apply_intent(seeded.graph, "batch_edit_keyframes",
                                     {"edits": [
        {"action": "dynamics", "node_id": a.node_id, "param": "exposure",
         "frame": 10, "kind": "static"},
    ]})
    assert converted.ok, converted.diagnostics
    entry = next(k for k in (_params(converted.graph, a.node_id)
                             ["exposure"]["keyframes"]))
    assert entry["interpolation"] == "hold"
    assert "kind" not in entry  # (static,1,1) 与 hold 等价：canonical-minimal


def test_noop_edits_do_not_advance_revision():
    graph = _graph()
    a, _b, _c = _correctors(graph)
    service = ColorGraphService()
    seeded = service.apply_intent(graph, "batch_edit_keyframes", {"edits": [
        {"action": "set", "node_id": a.node_id, "param": "exposure",
         "frame": 10, "value": 1.0},
    ]})
    revision = seeded.graph.graph_revision
    repeat = service.apply_intent(seeded.graph, "batch_edit_keyframes",
                                  {"edits": [
        {"action": "set", "node_id": a.node_id, "param": "exposure",
         "frame": 10, "value": 1.0},
    ]})
    assert repeat.ok
    assert repeat.graph.graph_revision == revision  # 重复置值不入史
    in_place = service.apply_intent(seeded.graph, "batch_edit_keyframes",
                                    {"edits": [
        {"action": "move", "node_id": a.node_id, "param": "exposure",
         "frame": 10, "to_frame": 10},
    ]})
    assert in_place.ok
    assert in_place.graph.graph_revision == revision  # 拖回原位不入史


def test_remove_batch_clears_empty_track():
    graph = _graph()
    a, _b, _c = _correctors(graph)
    service = ColorGraphService()
    seeded = service.apply_intent(graph, "batch_edit_keyframes", {"edits": [
        {"action": "set", "node_id": a.node_id, "param": "exposure",
         "frame": 10, "value": 1.0},
        {"action": "set", "node_id": a.node_id, "param": "exposure",
         "frame": 20, "value": 2.0},
    ]})
    removed = service.apply_intent(seeded.graph, "batch_edit_keyframes",
                                   {"edits": [
        {"action": "remove", "node_id": a.node_id, "param": "exposure",
         "frame": 10},
        {"action": "remove", "node_id": a.node_id, "param": "exposure",
         "frame": 20},
    ]})
    assert removed.ok
    assert "exposure" not in _params(removed.graph, a.node_id)


def test_illegal_value_shape_rejects_batch():
    graph = _graph()
    a, _b, _c = _correctors(graph)
    service = ColorGraphService()
    result = service.apply_intent(graph, "batch_edit_keyframes", {"edits": [
        {"action": "set", "node_id": a.node_id, "param": "gain",
         "frame": 5, "value": (0.1, 0.2)},  # vec3 需要三分量
    ]})
    assert not result.ok
    assert result.diagnostics[0].code == "bad_value_arity"


def test_empty_batch_is_noop():
    graph = _graph()
    service = ColorGraphService()
    result = service.apply_intent(graph, "batch_edit_keyframes",
                                  {"edits": []})
    assert result.ok
    assert result.graph.graph_revision == graph.graph_revision
