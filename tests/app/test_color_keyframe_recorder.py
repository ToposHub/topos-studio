"""KF-04：调色手势自动录制路由回归。

验收（计划 KF-04 行）：手势级自动录制与 base 参数一个历史条目；
播放回填/程序化更新/seek/undo 不产生关键帧（origin 隔离）；
Corrector 级 auto 开关作用后代、写入只落改动参数；会话切换丢弃状态。
"""
from __future__ import annotations

from unittest.mock import Mock

import pytest

from src.app.controllers.color_keyframe_recorder import KeyframeAutoRecorder


def _graph_project():
    from src.features.color.core.color_graph_migration import (
        migrate_legacy_stack,
    )
    from src.features.edit.core.project import EditProject
    from tests.color.fixtures.color_graph import f03_layer_stack
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


# ---------------------------------------------------------------------------
# 手势事务全链路（真实 AppHost + ProjectCommandStack）
# ---------------------------------------------------------------------------


class TestGestureAutoRecord:
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
