"""KF-04：调色手势自动录制路由回归。

验收（计划 KF-04 行）：手势级自动录制与 base 参数一个历史条目；
播放回填/程序化更新/seek/undo 不产生关键帧（origin 隔离）；
Corrector 级 auto 开关作用后代、写入只落改动参数；会话切换丢弃状态。
"""
from __future__ import annotations

from unittest.mock import Mock

import pytest

from src.app.controllers.color_keyframe_recorder import KeyframeAutoRecorder
from src.features.color.core.color_graph_migration import migrate_legacy_stack
from src.features.edit.core.project import EditProject
from tests.color.fixtures.color_graph import f03_layer_stack


def _graph_project():
    project, sid = EditProject.create(name="kf04").create_color_stack(
        asset_id="a1", name="s")
    stack = project.color_stacks[sid].with_layers(
        [layer.to_dict(None) for layer in f03_layer_stack()])
    graph = migrate_legacy_stack(stack)
    project = project.update_color_stack(sid, color_graph=graph)
    return project, sid, graph


def _node_id(graph):
    return next(n.node_id for n in graph.nodes if n.node_type == "corrector")


# ---------------------------------------------------------------------------
# 录制器单元
# ---------------------------------------------------------------------------


def test_recorder_collects_last_value_and_binds_first_frame():
    recorder = KeyframeAutoRecorder()
    recorder.set_auto("n1", True)
    recorder.observe("n1", "exposure", 0.4, frame=30)
    recorder.observe("n1", "exposure", 0.7, frame=31)  # 同手势帧不漂移
    recorder.observe("n1", "exposure", 0.9, frame=32)
    edits = recorder.take_pending_edits()
    assert edits == [{"action": "set", "node_id": "n1", "param": "exposure",
                      "frame": 30, "value": 0.9,
                      "interpolation": "linear"}]
    assert recorder.take_pending_edits() == []  # 手势已复位


def test_recorder_ignores_unregistered_and_auto_off():
    recorder = KeyframeAutoRecorder()
    recorder.set_auto("n1", True)
    recorder.observe("n1", "blend_mode", "add", frame=10)  # 未登记字段
    recorder.observe("n2", "exposure", 0.5, frame=10)      # auto 未开
    assert recorder.take_pending_edits() == []


def test_recorder_drops_gesture_without_effective_frame():
    recorder = KeyframeAutoRecorder()
    recorder.set_auto("n1", True)
    recorder.observe("n1", "exposure", 0.5, frame=None)  # 无播放上下文
    assert recorder.take_pending_edits() == []


def test_recorder_reset_on_session_switch():
    recorder = KeyframeAutoRecorder()
    recorder.set_auto("n1", True)
    recorder.observe("n1", "exposure", 0.5, frame=10)
    recorder.reset()
    assert recorder.auto_nodes() == frozenset()
    assert recorder.take_pending_edits() == []


@pytest.mark.parametrize("frame,anchor", [(0, 10), (10, 10), (19, 10), (20, 20), (40, 20)])
def test_auto_off_updates_existing_anchor_preserving_dynamics(frame, anchor):
    recorder = KeyframeAutoRecorder()
    recorder.track_provider = lambda *_: {"keyframes": [
        {"frame": 10, "value": 0.0, "kind": "static", "start": 0.2, "end": 0.8},
        {"frame": 20, "value": 1.0, "kind": "dynamic", "start": 2.0, "end": 0.3},
    ]}
    recorder.observe("n1", "exposure", 0.4, frame)
    recorder.observe("n1", "exposure", 0.7, 80)  # 手势中的播放头变化不换锚点
    edit, = recorder.take_pending_edits()
    assert edit["frame"] == anchor
    assert edit["value"] == 0.7
    assert edit["kind"] == ("static" if anchor == 10 else "dynamic")
    assert edit["start"] == (0.2 if anchor == 10 else 2.0)


def test_leaf_auto_overrides_and_parent_toggle_resets_children():
    recorder = KeyframeAutoRecorder()
    recorder.set_auto("n", True)
    recorder.set_track_auto("n", "exposure", False)
    recorder.observe("n", "exposure", 0.4, 10)
    recorder.observe("n", "contrast", 0.5, 10)
    assert [edit["param"] for edit in recorder.take_pending_edits()] == ["contrast"]
    recorder.set_auto("n", False)
    recorder.set_track_auto("n", "exposure", True)
    assert recorder.auto_enabled("n", "exposure")
    recorder.set_auto("n", False)
    assert not recorder.auto_enabled("n", "exposure")


def test_identical_member_ids_in_two_compounds_do_not_share_recording():
    recorder = KeyframeAutoRecorder()
    recorder.set_auto("n", True, ("a",))
    recorder.set_auto("n", True, ("b",))
    recorder.observe("n", "exposure", 0.3, 12, ("a",))
    recorder.observe("n", "exposure", 0.8, 12, ("b",))
    groups = recorder.take_pending_edit_groups()
    assert groups[("a",)][0]["value"] == 0.3
    assert groups[("b",)][0]["value"] == 0.8


# ---------------------------------------------------------------------------
# 手势事务全链路（真实 AppHost + ProjectCommandStack）
# ---------------------------------------------------------------------------


class TestGestureAutoRecord:
    def test_tonal_saturation_controller_uses_same_recording_transaction(self):
        from src.app.controllers.layer_controller import LayerOpsMixin
        app_host, _holder, recorder, node_id = self._wired()
        app_host.graph_edit_context_active = lambda: True
        app_host.active_grade_node_id = lambda: node_id
        recorder.set_auto(node_id, True)

        class Controller(LayerOpsMixin):
            pass

        controller = Controller()
        controller.engine = Mock()
        controller.color_graph_host = app_host
        controller._invalidate_video_render_params_if_playing = Mock()
        controller._is_video_playing = lambda: False
        controller.request_render = Mock()
        controller.on_tonal_saturation_changed(12.0, 0.0, -3.0)
        assert app_host.end_grade_gesture()
        assert self._keyframes(app_host, node_id, "lift_saturation") == {42: 12.0}
        assert self._keyframes(app_host, node_id, "gain_saturation") == {42: -3.0}
        assert len(app_host.bridge._project_stack._undo_stack) == 1

    def _wired(self, source_frame=42):
        from src.app.controllers.color_graph_controller import (
            ColorGraphAppHost,
        )
        from src.features.edit.core.commands import ProjectCommandStack
        project, sid, graph = _graph_project()
        holder = {"project": project}
        engine = Mock()
        engine.layers = []
        app_host = ColorGraphAppHost(
            get_project=lambda: holder["project"],
            set_project=lambda p: holder.__setitem__("project", p),
            project_stack=ProjectCommandStack(),
            engine_provider=lambda: engine,
            request_render=lambda: None, panels_provider=lambda: {})
        app_host.on_clip_stack_loaded(sid)
        recorder = KeyframeAutoRecorder()
        app_host.attach_keyframe_recorder(
            recorder, frame_provider=lambda: source_frame)
        return app_host, holder, recorder, _node_id(app_host.bound_graph())

    def _gesture(self, app_host, node_id, values, param="exposure"):
        transaction = "txn-kf04"
        app_host.begin_grade_gesture(transaction)
        for value in values:
            assert app_host.submit_grade_change(node_id, param, value,
                                                transaction)
        return app_host.end_grade_gesture()

    def _keyframes(self, app_host, node_id, param="exposure"):
        node = next(n for n in app_host.bound_graph().nodes
                    if n.node_id == node_id)
        track = (node.params.get("grade_keyframes") or {}).get(param) or {}
        return {int(k["frame"]): k["value"]
                for k in track.get("keyframes") or ()}

    def test_auto_on_gesture_records_keyframe_in_same_history_entry(self):
        app_host, holder, recorder, node_id = self._wired()
        recorder.set_auto(node_id, True)
        depth0 = len(app_host.bridge._project_stack._undo_stack)
        assert self._gesture(app_host, node_id, [0.3, 0.6, 0.9])
        # base 参数 + 关键帧 = 一个历史条目
        assert len(app_host.bridge._project_stack._undo_stack) == depth0 + 1
        assert self._keyframes(app_host, node_id) == {42: 0.9}  # 最后值+绑定帧
        undone = app_host.bridge._project_stack.undo(holder["project"])
        assert undone is not None
        undone_stack = undone.color_stacks[next(iter(undone.color_stacks))]
        undone_node = next(n for n in undone_stack.color_graph.nodes
                           if n.node_id == node_id)
        # undo 同时恢复 base 与关键帧
        assert not (undone_node.params.get("grade_keyframes") or {})

    def test_auto_off_gesture_writes_no_keyframe(self):
        app_host, _holder, _recorder, node_id = self._wired()
        depth0 = len(app_host.bridge._project_stack._undo_stack)
        assert self._gesture(app_host, node_id, [0.5])
        assert self._keyframes(app_host, node_id) == {}
        assert len(app_host.bridge._project_stack._undo_stack) == depth0 + 1

    def test_auto_off_real_gesture_updates_anchor_and_undo_restores_it(self):
        app_host, holder, recorder, node_id = self._wired(source_frame=15)
        assert app_host.submit_keyframe_batch([
            {"action": "set", "node_id": node_id, "param": "exposure", "frame": 10, "value": 0.2, "interpolation": "hold"},
            {"action": "set", "node_id": node_id, "param": "exposure", "frame": 20, "value": 0.8},
        ])
        depth = len(app_host.bridge._project_stack._undo_stack)
        assert self._gesture(app_host, node_id, [0.4, 0.5])
        assert self._keyframes(app_host, node_id) == {10: 0.5, 20: 0.8}
        assert len(app_host.bridge._project_stack._undo_stack) == depth + 1
        undone = app_host.bridge._project_stack.undo(holder["project"])
        graph = next(iter(undone.color_stacks.values())).color_graph
        node = next(n for n in graph.nodes if n.node_id == node_id)
        keys = node.params["grade_keyframes"]["exposure"]["keyframes"]
        assert keys[0]["value"] == 0.2
        assert keys[0]["interpolation"] == "hold"

    def test_no_playback_context_drops_recording(self):
        app_host, _holder, recorder, node_id = self._wired(source_frame=None)
        recorder.set_auto(node_id, True)
        assert self._gesture(app_host, node_id, [0.5])
        assert self._keyframes(app_host, node_id) == {}

    def test_value_equal_to_existing_key_is_noop_in_history(self):
        app_host, holder, recorder, node_id = self._wired(source_frame=10)
        stack = app_host.bridge._project_stack
        recorder.set_auto(node_id, True)
        assert self._gesture(app_host, node_id, [0.5])  # 建键 10@0.5
        depth1 = len(stack._undo_stack)
        assert self._gesture(app_host, node_id, [0.5])  # 同帧同值
        assert self._keyframes(app_host, node_id) == {10: 0.5}
        depth_on = len(stack._undo_stack)
        # 对照：auto 关闭的重复手势增量（仅 base patch）
        recorder.set_auto(node_id, False)
        assert self._gesture(app_host, node_id, [0.5])
        depth_off = len(stack._undo_stack)
        # 录制器无差值 → auto 开与关的重复手势历史增量一致（批处理不追加）
        assert depth_on - depth1 == depth_off - depth_on == 1


class TestToolGestureRecord:
    """R6：Qualifier/Window 工具手势接入自动录制（真实提交路径）。"""

    def _wired(self, source_frame=42, with_shape=False):
        from src.app.controllers.color_graph_controller import (
            ColorGraphAppHost,
        )
        from src.features.color.core.layer import MaskShape
        from src.features.edit.core.commands import ProjectCommandStack
        project, sid = EditProject.create(name="kf06").create_color_stack(
            asset_id="a1", name="s")
        layers = f03_layer_stack()
        shape_id = ""
        if with_shape:
            shape = MaskShape(MaskShape.CIRCLE)
            shape_id = shape.id
            layers[0].mask_shapes = [shape]
        stack = project.color_stacks[sid].with_layers(
            [layer.to_dict(None) for layer in layers])
        graph = migrate_legacy_stack(stack)
        project = project.update_color_stack(sid, color_graph=graph)
        holder = {"project": project, "frame": source_frame}
        engine = Mock()
        engine.layers = []
        app_host = ColorGraphAppHost(
            get_project=lambda: holder["project"],
            set_project=lambda p: holder.__setitem__("project", p),
            project_stack=ProjectCommandStack(),
            engine_provider=lambda: engine,
            request_render=lambda: None, panels_provider=lambda: {})
        app_host.on_clip_stack_loaded(sid)
        recorder = KeyframeAutoRecorder()
        app_host.attach_keyframe_recorder(
            recorder, frame_provider=lambda: holder["frame"])
        return app_host, holder, recorder, _node_id(app_host.bound_graph()), \
            shape_id

    def _tracks(self, app_host, node_id):
        node = next(n for n in app_host.bound_graph().nodes
                    if n.node_id == node_id)
        return node.params.get("grade_keyframes") or {}

    def test_qualifier_tool_gesture_records_registered_fields(self):
        app_host, holder, recorder, node_id, _shape = self._wired()
        recorder.set_auto(node_id, True)
        depth0 = len(app_host.bridge._project_stack._undo_stack)
        transaction = "txn-q"
        app_host.begin_grade_gesture(transaction)
        # 登记字段 + 离散开关 + 未登记字段混合（只录登记字段）
        assert app_host.submit_qualifier_change(
            node_id, {"lum_low": 0.2, "enabled": True,
                      "not_a_field": 1.0}, transaction)
        assert app_host.end_grade_gesture()
        tracks = self._tracks(app_host, node_id)
        assert {int(k["frame"]): k["value"]
                for k in tracks["qualifier.lum_low"]["keyframes"]} == {
            42: 0.2}
        assert "qualifier.enabled" not in tracks
        assert not any(name.startswith("qualifier.not_a_field")
                       for name in tracks)
        # base patch + 关键帧 = 一个历史条目
        assert len(app_host.bridge._project_stack._undo_stack) == depth0 + 1
        undone = app_host.bridge._project_stack.undo(holder["project"])
        undone_stack = undone.color_stacks[next(iter(undone.color_stacks))]
        undone_node = next(n for n in undone_stack.color_graph.nodes
                           if n.node_id == node_id)
        assert not (undone_node.params.get("grade_keyframes") or {})

    def test_window_shape_drag_records_mask_tracks(self):
        app_host, _holder, recorder, node_id, shape_id = self._wired(
            with_shape=True)
        recorder.set_auto(node_id, True)
        transaction = "txn-w"
        app_host.begin_grade_gesture(transaction)
        assert app_host.submit_window_shape_update(
            node_id, shape_id,
            {"center": [0.6, 0.4], "invert": True}, transaction)
        assert app_host.end_grade_gesture()
        tracks = self._tracks(app_host, node_id)
        name = f"mask.{shape_id}.center"
        assert {tuple(k["value"]) for k in tracks[name]["keyframes"]} == {
            (0.6, 0.4)}
        assert not any(f"{shape_id}.invert" in str(key) for key in tracks)

    def test_two_moments_bind_first_frame_of_each_gesture(self):
        app_host, holder, recorder, node_id, _shape = self._wired()
        recorder.set_auto(node_id, True)
        app_host.begin_grade_gesture("t1")
        app_host.submit_qualifier_change(node_id, {"lum_low": 0.1}, "t1")
        app_host.end_grade_gesture()
        holder["frame"] = 43  # 播放头推进（第二个手势绑新帧）
        app_host.begin_grade_gesture("t2")
        app_host.submit_qualifier_change(node_id, {"lum_low": 0.3}, "t2")
        app_host.end_grade_gesture()
        tracks = self._tracks(app_host, node_id)
        assert {int(k["frame"]): k["value"]
                for k in tracks["qualifier.lum_low"]["keyframes"]} == {
            42: 0.1, 43: 0.3}

    def test_auto_off_tool_gesture_writes_base_only(self):
        app_host, _holder, _recorder, node_id, _shape = self._wired()
        transaction = "txn-q"
        app_host.begin_grade_gesture(transaction)
        assert app_host.submit_qualifier_change(
            node_id, {"lum_low": 0.2}, transaction)
        assert app_host.end_grade_gesture()
        assert self._tracks(app_host, node_id) == {}


class TestCompoundChildRecord:
    """R6 审计：compound 成员的手势录制（批命令须带 _compound_path）。"""

    def _wired_compound(self, source_frame=42):
        from src.app.controllers.color_graph_controller import (
            ColorGraphAppHost,
        )
        from src.features.color.core.color_graph_service import ColorGraphService
        from src.features.edit.core.commands import ProjectCommandStack
        project, sid = EditProject.create(name="kf06c").create_color_stack(
            asset_id="a1", name="s")
        stack = project.color_stacks[sid].with_layers(
            [layer.to_dict(None) for layer in f03_layer_stack()])
        graph = migrate_legacy_stack(stack)
        # 前两个 Corrector 组成 compound——工具手势以成员为 target
        counter = iter(range(500))
        service = ColorGraphService(id_factory=lambda: f"g{next(counter)}")
        correctors = [n.node_id for n in graph.nodes
                      if n.node_type == "corrector"]
        grouped = service.apply_intent(graph, "group_subgraph", {
            "member_ids": correctors[:2]})
        assert grouped.ok, grouped.diagnostics
        graph = grouped.graph
        compound = next(n for n in graph.nodes
                        if n.compound_children is not None)
        child = next(n for n in compound.compound_children.nodes
                     if n.node_type == "corrector")
        project = project.update_color_stack(sid, color_graph=graph)
        holder = {"project": project}
        engine = Mock()
        engine.layers = []
        app_host = ColorGraphAppHost(
            get_project=lambda: holder["project"],
            set_project=lambda p: holder.__setitem__("project", p),
            project_stack=ProjectCommandStack(),
            engine_provider=lambda: engine,
            request_render=lambda: None, panels_provider=lambda: {})
        app_host.on_clip_stack_loaded(sid)
        recorder = KeyframeAutoRecorder()
        app_host.attach_keyframe_recorder(
            recorder, frame_provider=lambda: source_frame)
        # 工具路径：面板父链指向 compound
        from types import SimpleNamespace as _NS
        app_host._panel = _NS(_parents=(("compound", compound.node_id),))
        return app_host, holder, recorder, child.node_id

    def test_compound_child_tool_gesture_records(self):
        app_host, _holder, recorder, child_id = self._wired_compound()
        recorder.set_auto(child_id, True, app_host._tool_compound_path())
        transaction = "txn-cc"
        app_host.begin_grade_gesture(transaction)
        assert app_host.submit_qualifier_change(
            child_id, {"lum_low": 0.25}, transaction)
        assert app_host.end_grade_gesture()
        # 成员的关键帧写入成功（不带路径会被 node_not_found 整批拒）
        compound = next(n for n in app_host.bound_graph().nodes
                        if n.compound_children is not None)
        node = next(n for n in compound.compound_children.nodes
                    if n.node_id == child_id)
        track = (node.params.get("grade_keyframes") or {}).get(
            "qualifier.lum_low") or {}
        assert {int(k["frame"]): k["value"]
                for k in track.get("keyframes") or ()} == {42: 0.25}

    def test_recorder_groups_by_compound_path(self):
        recorder = KeyframeAutoRecorder()
        recorder.set_auto("n1", True)
        recorder.set_auto("c2", True, ("c1",))
        recorder.observe("n1", "exposure", 0.5, frame=7)
        recorder.observe("c2", "exposure", 0.8, frame=7,
                         compound_path=("c1",))
        groups = recorder.take_pending_edit_groups()
        assert set(groups) == {(), ("c1",)}
        assert groups[()][0]["node_id"] == "n1"
        assert groups[("c1",)][0]["node_id"] == "c2"
        # 兼容面：take_pending_edits 仍返回平铺条目
        recorder.observe("n1", "exposure", 0.5, frame=7)
        flat = recorder.take_pending_edits()
        assert flat == [{"action": "set", "node_id": "n1",
                         "param": "exposure", "frame": 7, "value": 0.5,
                         "interpolation": "linear"}]
