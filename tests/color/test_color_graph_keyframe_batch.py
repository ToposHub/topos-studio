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


def test_nested_and_root_keys_commit_once_and_reject_atomically():
    graph = _graph()
    a, b, c = _correctors(graph)
    service = ColorGraphService()
    grouped = service.apply_intent(graph, "group_subgraph", {"member_ids": [a.node_id, b.node_id]})
    assert grouped.ok
    graph = grouped.graph
    compound = next(n for n in graph.nodes if n.compound_children is not None)
    edits = [
        {"action": "set", "node_id": a.node_id, "compound_path": [compound.node_id],
         "param": "exposure", "frame": 12, "value": 0.4},
        {"action": "set", "node_id": c.node_id, "param": "contrast", "frame": 20, "value": 0.2},
    ]
    result = service.apply_intent(graph, "batch_edit_keyframes", {"edits": edits})
    assert result.ok, result.diagnostics
    assert result.graph.graph_revision == graph.graph_revision + 1
    child = next(n for n in result.graph.nodes if n.node_id == compound.node_id).compound_children
    assert _frames(child, a.node_id, "exposure") == [12]
    assert _frames(result.graph, c.node_id, "contrast") == [20]
    repeated = service.apply_intent(result.graph, "batch_edit_keyframes", {"edits": edits})
    assert repeated.ok and repeated.graph is result.graph
    bad = [edits[1], {**edits[0], "node_id": "missing"}]
    rejected = service.apply_intent(graph, "batch_edit_keyframes", {"edits": bad})
    assert not rejected.ok and rejected.graph is graph
    locked = service.apply_intent(graph, "set_edit_lock", {"node_ids": [compound.node_id], "locked": True})
    assert locked.ok
    rejected = service.apply_intent(locked.graph, "batch_edit_keyframes", {"edits": edits})
    assert not rejected.ok and rejected.primary_code == "node_locked"


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


# ---------------------------------------------------------------------------
# R3：无损更新（只触及目标轨；未知字段/未来格式保留；触及不可解析整批拒）
# ---------------------------------------------------------------------------

from dataclasses import replace as _dc_replace

from src.shared.project.color_graph import unfreeze_params as _unfreeze


def _graph_with_custom_kf(kf: dict):
    """携带自定义 params.grade_keyframes（含未知字段/未来格式）的图。"""
    graph = _graph()
    a, _b, _c = _correctors(graph)
    nodes = tuple(
        _dc_replace(n, params={**n.params, "grade_keyframes": kf})
        if n.node_id == a.node_id else n for n in graph.nodes)
    return _dc_replace(graph, nodes=nodes), a.node_id


_FUTURE_TRACK = {"schema": "v9", "keyframes": None,
                 "bezier_handles": [[0.1, 0.2]]}


class TestLosslessBatchUpdate:
    def _fixture(self):
        kf = {
            # 被编辑轨：一键带 keyframe 级未知字段（不被本批触及）
            "exposure": {"keyframes": [
                {"frame": 10, "value": 1.0, "interpolation": "linear",
                 "bezier_handles": [[0.0, 0.0], [1.0, 1.0]]},
                {"frame": 20, "value": 2.0, "interpolation": "linear"},
            ], "track_note": "keep me"},
            # 未触及轨：轨道级未知字段 + 墓碑扩展
            "temperature": {"keyframes": [
                {"frame": 5, "value": 0.5, "interpolation": "ease"}],
                "solo": True},
        }
        return _graph_with_custom_kf(kf)

    def test_edit_one_track_preserves_other_track_and_untouched_keys(self):
        graph, node_id = self._fixture()
        service = ColorGraphService()
        result = service.apply_intent(graph, "batch_edit_keyframes",
                                      {"edits": [
            {"action": "set", "node_id": node_id, "param": "exposure",
             "frame": 20, "value": 9.0},
        ]})
        assert result.ok, result.diagnostics
        params = _unfreeze(_params(result.graph, node_id))
        # 未触及轨逐字节保留（含轨道级未知字段）
        assert params["temperature"] == {"keyframes": [
            {"frame": 5, "value": 0.5, "interpolation": "ease"}],
            "solo": True}
        # 编辑轨：未触及键保留 keyframe 级未知字段；轨道级未知字段保留
        entries = {int(k["frame"]): k
                   for k in params["exposure"]["keyframes"]}
        assert entries[10]["bezier_handles"] == [[0.0, 0.0], [1.0, 1.0]]
        assert params["exposure"]["track_note"] == "keep me"
        assert entries[20]["value"] == 9.0

    def test_move_and_dynamics_only_touch_targeted_fields(self):
        graph, node_id = self._fixture()
        service = ColorGraphService()
        moved = service.apply_intent(graph, "batch_edit_keyframes",
                                     {"edits": [
            {"action": "move", "node_id": node_id, "param": "exposure",
             "frame": 10, "to_frame": 12},
        ]})
        assert moved.ok
        entries = {int(k["frame"]): k for k in _unfreeze(
            _params(moved.graph, node_id))["exposure"]["keyframes"]}
        # move 只改帧号：未知字段随键原样迁移
        assert entries[12]["bezier_handles"] == [[0.0, 0.0], [1.0, 1.0]]

        dyn = service.apply_intent(moved.graph, "batch_edit_keyframes",
                                   {"edits": [
            {"action": "dynamics", "node_id": node_id,
             "param": "exposure", "frame": 12,
             "kind": "dynamic", "start": 0.25, "end": 0.75},
        ]})
        assert dyn.ok
        entry = next(k for k in _unfreeze(_params(dyn.graph, node_id))
                     ["exposure"]["keyframes"] if int(k["frame"]) == 12)
        # dynamics 只改键型/动态属性：值与未知字段保留
        assert (entry["kind"], entry["start"], entry["end"]) == (
            "dynamic", 0.25, 0.75)
        assert entry["bezier_handles"] == [[0.0, 0.0], [1.0, 1.0]]

    def test_noop_edit_does_not_rewrite_other_tracks(self):
        graph, node_id = self._fixture()
        service = ColorGraphService()
        revision = graph.graph_revision
        result = service.apply_intent(graph, "batch_edit_keyframes",
                                      {"edits": [
            {"action": "set", "node_id": node_id, "param": "exposure",
             "frame": 20, "value": 2.0},  # 与现状一致 → 无任何写入
        ]})
        assert result.ok
        assert result.graph.graph_revision == revision
        node = next(n for n in result.graph.nodes
                    if n.node_id == node_id)
        # params 域不被规范化重写（未知字段不因 no-op 编辑丢失）
        assert node.params["grade_keyframes"]["exposure"][
            "track_note"] == "keep me"

    def test_touching_unparseable_track_rejects_whole_batch(self):
        graph, node_id = _graph_with_custom_kf({
            "exposure": dict(_FUTURE_TRACK)})
        service = ColorGraphService()
        seeded = service.apply_intent(graph, "batch_edit_keyframes",
                                      {"edits": [
            {"action": "set", "node_id": node_id, "param": "contrast",
             "frame": 8, "value": 0.1},
        ]})
        assert seeded.ok  # 未触及的未来格式不受影响
        revision = seeded.graph.graph_revision
        result = service.apply_intent(seeded.graph, "batch_edit_keyframes",
                                      {"edits": [
            {"action": "set", "node_id": node_id, "param": "exposure",
             "frame": 8, "value": 0.2},   # 触及不可解析轨道
            {"action": "set", "node_id": node_id, "param": "contrast",
             "frame": 9, "value": 0.3},
        ]})
        assert not result.ok
        assert result.diagnostics[0].code == "track_format_unsupported"
        assert "exposure" in str(result.diagnostics[0])  # 指名轨道
        assert result.graph.graph_revision == revision  # 无部分写入
        params = _params(result.graph, node_id)
        assert _unfreeze(params["exposure"]) == _FUTURE_TRACK  # 数据原样
        assert 9 not in _frames(result.graph, node_id, "contrast")

    def test_touching_non_mapping_value_rejects_batch(self):
        graph, node_id = _graph_with_custom_kf({"exposure": "future-blob"})
        service = ColorGraphService()
        result = service.apply_intent(graph, "batch_edit_keyframes",
                                      {"edits": [
            {"action": "remove", "node_id": node_id, "param": "exposure",
             "frame": 8},
        ]})
        assert not result.ok
        assert result.diagnostics[0].code == "track_format_unsupported"
        # 非 Mapping 值原样保留（旧代码会静默丢弃）
        assert _params(result.graph, node_id)["exposure"] == "future-blob"

    def test_single_key_command_reuses_lossless_core(self):
        # 兼容脚本单键命令：未触及轨与未知字段同样保留（R3 §2）
        graph, node_id = self._fixture()
        service = ColorGraphService()
        result = service.apply_intent(graph, "set_node_keyframe", {
            "node_id": node_id, "param": "exposure",
            "frame": 30, "value": 3.0})
        assert result.ok, result.diagnostics
        params = _unfreeze(_params(result.graph, node_id))
        assert params["temperature"]["solo"] is True
        assert params["exposure"]["track_note"] == "keep me"
        entries = {int(k["frame"]): k
                   for k in params["exposure"]["keyframes"]}
        assert entries[10]["bezier_handles"] == [[0.0, 0.0], [1.0, 1.0]]
        assert entries[30]["value"] == 3.0
