"""KF-05：关键帧持久化边界回归（计划 V05 · 旧工程与版本）。

验收：迁移前后同帧参数一致；删除迁移来的最后一键后保存/重开/换版本/
undo/redo 不复活；含未知字段的工程往返保留语义；播放中保存不把插值
临时值固化成 base。
"""
from __future__ import annotations

import pytest

from src.features.color.core.color_graph_param_source import (
    node_layer_payload,
)
from src.features.color.core.color_graph_service import ColorGraphService
from src.features.color.core.grade_keyframes import (
    GradeKeyframe,
    GradeKeyframeTrack,
    evaluate_grade_keyframes,
    mask_track_name,
)


def _migrated(layers_overrides=None):
    """Layer 载荷（含 legacy 关键帧）→ 迁移图。"""
    from src.features.color.core.color_graph_migration import (
        migrate_legacy_stack,
    )
    from src.features.edit.core.project import EditProject
    from tests.color.fixtures.color_graph import f03_layer_stack
    layers = f03_layer_stack()
    if layers_overrides is not None:
        layers_overrides(layers)
    project, sid = EditProject.create(name="kf05").create_color_stack(
        asset_id="a1", name="s")
    stack = project.color_stacks[sid].with_layers(
        [layer.to_dict(None) for layer in layers])
    graph = migrate_legacy_stack(stack)
    return graph, sid


def _corrector(graph, index=0):
    correctors = [n for n in graph.nodes if n.node_type == "corrector"]
    return correctors[index]


# ---------------------------------------------------------------------------
# V05 · 删除不复活（墓碑语义跨保存/重开/undo/redo）
# ---------------------------------------------------------------------------


def _seed_legacy_exposure(layers):
    layers[0].grade_keyframes["exposure"] = GradeKeyframeTrack(
        "exposure", [GradeKeyframe(10, 0.5), GradeKeyframe(20, 0.8)])


def test_delete_last_legacy_origin_key_does_not_resurrect():
    service = ColorGraphService()
    graph, _sid = _migrated(_seed_legacy_exposure)
    node = _corrector(graph)
    # legacy 轨道经合并视图可见（首开同帧参数一致）
    payload = node_layer_payload(node, include_lut_cache=False)
    assert {int(k["frame"]): k["value"]
            for k in payload["grade_keyframes"]["exposure"]["keyframes"]} == {
        10: 0.5, 20: 0.8}
    # 删除最后一个键（图域命令）
    removed = service.apply_intent(graph, "batch_edit_keyframes", {"edits": [
        {"action": "remove", "node_id": node.node_id, "param": "exposure",
         "frame": 20}]})
    assert removed.ok
    removed2 = service.apply_intent(removed.graph, "batch_edit_keyframes",
                                    {"edits": [
        {"action": "remove", "node_id": node.node_id, "param": "exposure",
         "frame": 10}]})
    assert removed2.ok
    node = _corrector(removed2.graph)
    params_kf = node.params.get("grade_keyframes") or {}
    # 冻结后 keyframes 为 tuple——按内容断言墓碑
    assert params_kf["exposure"].get("tombstone") is True
    assert not params_kf["exposure"].get("keyframes")
    # 合并视图：legacy 同名轨道被墓碑压制
    payload = node_layer_payload(node, include_lut_cache=False)
    assert payload["grade_keyframes"]["exposure"] == {"keyframes": []}


def test_tombstone_survives_save_reload_and_edit():
    from src.features.edit.core.project import EditProject
    from src.shared.project.color_stack import ColorStackData
    service = ColorGraphService()
    graph, _sid = _migrated(_seed_legacy_exposure)
    node = _corrector(graph)
    for frame in (10, 20):
        result = service.apply_intent(graph, "batch_edit_keyframes",
                                      {"edits": [
            {"action": "remove", "node_id": node.node_id,
             "param": "exposure", "frame": frame}]})
        graph = result.graph
    # 保存边界：图 → 工程栈 → to_dict → from_dict（重开）
    project, sid2 = EditProject.create(name="kf05b").create_color_stack(
        asset_id="a1", name="s")
    project = project.update_color_stack(sid2, color_graph=graph)
    restored_stack = ColorStackData.from_dict(
        project.color_stacks[sid2].to_dict())
    restored_graph = restored_stack.color_graph
    node = _corrector(restored_graph)
    payload = node_layer_payload(node, include_lut_cache=False)
    # 保存/重开后删除仍生效（不复活）
    assert payload["grade_keyframes"]["exposure"] == {"keyframes": []}
    # 再编辑同轨道：墓碑让位于新写入
    service2 = ColorGraphService()
    reseeded = service2.apply_intent(restored_graph, "batch_edit_keyframes",
                                     {"edits": [
        {"action": "set", "node_id": node.node_id, "param": "exposure",
         "frame": 5, "value": 0.1}]})
    assert reseeded.ok
    node = _corrector(reseeded.graph)
    payload = node_layer_payload(node, include_lut_cache=False)
    assert {int(k["frame"]): k["value"]
            for k in payload["grade_keyframes"]["exposure"]["keyframes"]} == {
        5: 0.1}


def test_undo_restores_deleted_legacy_track():
    """删除经真实命令栈提交：undo 两步恢复双键（含 legacy 轨道重可见）。"""
    from src.features.color.core.color_graph_migration import (
        migrate_legacy_stack,
    )
    from src.features.edit.core.commands import ProjectCommandStack
    from src.features.edit.core.commands.color_commands import (
        ApplyColorGraphCommand,
    )
    from src.features.edit.core.project import EditProject
    service = ColorGraphService()
    graph, _sid = _migrated(_seed_legacy_exposure)
    node = _corrector(graph)
    project, sid = EditProject.create(name="kf05d").create_color_stack(
        asset_id="a1", name="s")
    project = project.update_color_stack(sid, color_graph=graph)
    stack = ProjectCommandStack()
    for frame in (20, 10):
        command = ApplyColorGraphCommand.create(
            stack_id=sid, operation="batch_edit_keyframes",
            arguments={"edits": [
                {"action": "remove", "node_id": node.node_id,
                 "param": "exposure", "frame": frame}]})
        project = stack.execute(command, project)
    removed_graph = project.color_stacks[sid].color_graph
    assert _corrector(removed_graph).params[
        "grade_keyframes"]["exposure"].get("tombstone")
    # undo 两步：双键恢复（legacy 轨道重新可见）
    undone = stack.undo(project)
    assert undone is not None
    undone = stack.undo(undone)
    assert undone is not None
    node = _corrector(undone.color_stacks[sid].color_graph)
    payload = node_layer_payload(node, include_lut_cache=False)
    assert {int(k["frame"]): k["value"]
            for k in payload["grade_keyframes"]["exposure"]["keyframes"]} == {
        10: 0.5, 20: 0.8}


# ---------------------------------------------------------------------------
# 未知字段保留（往返语义）
# ---------------------------------------------------------------------------


def test_unknown_track_fields_survive_round_trip():
    """未触碰的轨道携带未知键穿越保存/重开（编辑过的轨道规范化为已知键）。"""
    from src.shared.project.color_stack import ColorStackData
    from src.features.edit.core.project import EditProject
    graph, _sid = _migrated(_seed_legacy_exposure)
    node = _corrector(graph)
    # params 域轨道带未知键（未来版本字段）
    params = dict(node.params)
    params["grade_keyframes"] = {"contrast": {"keyframes": [
        {"frame": 4, "value": 0.2, "interpolation": "linear",
         "future_field": {"x": 1}}],
        "vendor_note": "hello"}}
    from dataclasses import replace
    graph = replace(graph, nodes=tuple(
        replace(n, params=params) if n.node_id == node.node_id else n
        for n in graph.nodes))
    # 工程往返
    project, sid = EditProject.create(name="kf05c").create_color_stack(
        asset_id="a1", name="s")
    project = project.update_color_stack(sid, color_graph=graph)
    restored = ColorStackData.from_dict(project.color_stacks[sid].to_dict())
    node = _corrector(restored.color_graph)
    track = node.params["grade_keyframes"]["contrast"]
    assert track["vendor_note"] == "hello"
    assert track["keyframes"][0]["future_field"] == {"x": 1}


# ---------------------------------------------------------------------------
# 播放中保存不固化插值临时值（base 保护）
# ---------------------------------------------------------------------------


def _engine_with(layers):
    """Bare engine：绑定 ImageEngine 关键帧方法（无 GL 初始化）。"""
    from types import MethodType, SimpleNamespace
    from src.features.color.core.image_engine import ImageEngine
    eng = SimpleNamespace(layers=layers, active_layer_index=0,
                          current_grade_frame=None, render_calls=[])
    eng.render_pipeline = lambda **kw: eng.render_calls.append(kw)
    eng._update_mask_textures = lambda: None
    eng.get_active_layer = lambda: layers[0]
    eng.apply_grade_keyframes = MethodType(
        ImageEngine.apply_grade_keyframes, eng)
    eng.remove_grade_keyframe = MethodType(
        ImageEngine.remove_grade_keyframe, eng)
    return eng


def test_save_during_playback_serializes_base_not_interpolated():
    from tests.color.fixtures.color_graph import f03_layer_stack
    layers = f03_layer_stack()
    _seed_legacy_exposure(layers)
    engine = _engine_with(layers)
    # 播放到帧 15（插值中点：exposure = 0.65）再"保存"
    engine.apply_grade_keyframes(15, render=False)
    layer = layers[0]
    assert layer.grading_params["exposure"] == pytest.approx(0.65)
    payload = layer.to_dict(None)
    # 序列化的是动画前基线（0.5），不是帧 15 的插值临时值
    base_params = payload["grading_params"]
    assert base_params["exposure"] == 0.5
    # 重载后同帧求值仍得 0.65（轨道重求值同帧同值）
    restored_track = GradeKeyframeTrack.from_dict(
        payload["grade_keyframes"]["exposure"])
    assert evaluate_grade_keyframes(
        {"exposure": restored_track}, 15,
        dict(base_params))["exposure"] == pytest.approx(0.65)


def test_remove_last_keyframe_restores_base_value():
    from tests.color.fixtures.color_graph import f03_layer_stack
    layers = f03_layer_stack()
    _seed_legacy_exposure(layers)
    engine = _engine_with(layers)
    engine.apply_grade_keyframes(15, render=False)
    engine.current_grade_frame = 10
    assert engine.remove_grade_keyframe(10)
    assert engine.remove_grade_keyframe(20)
    layer = layers[0]
    # 轨道全删：参数回到动画前基线（而非最后插值帧的 0.65）
    assert not layer.grade_keyframes
    assert layer.grading_params["exposure"] == 0.5
    assert layer._grade_params_base is None
