"""KF-02：关键帧面板只读宿主回归（投影回填 + 播放头双向同步 + 写入口关闭）。

验收基线（计划 KF-02 行）：同素材投影回填、切片段/会话切换重建、
播放头回填无回声、面板 seek 只走一次既有协议、拖拽走 scrub 车道、
edit_requested 只读丢弃、无播放上下文保留演示数据。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.app.controllers.color_keyframes_host import (
    ColorKeyframesHost,
    build_keyframes_snapshot,
)
from src.features.color.ui.color_keyframes_panel import ColorKeyframesPanel


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------


def _graph_with_keyframes():
    from src.features.color.core.color_graph_migration import (
        migrate_legacy_stack,
    )
    from src.features.color.core.color_graph_service import ColorGraphService
    from src.features.color.core.grade_keyframes import (
        GradeKeyframe,
        GradeKeyframeTrack,
        mask_track_name,
    )
    from src.features.edit.core.project import EditProject
    from tests.color.fixtures.color_graph import f03_layer_stack

    project, sid = EditProject.create(name="kf02").create_color_stack(
        asset_id="a1", name="s")
    layers = f03_layer_stack()
    # 注入窗口轨道（Layer 域：mask.<shape_id>.<field>，随 legacy 载荷合并）
    shape_id = "11111111-2222-3333-4444-555555555555"
    layers[0].grade_keyframes[mask_track_name(shape_id, "center")] = (
        GradeKeyframeTrack(mask_track_name(shape_id, "center"), [
            GradeKeyframe(0, (0.5, 0.5)),
            GradeKeyframe(24, (0.6, 0.4)),
        ]))
    stack = project.color_stacks[sid].with_layers(
        [layer.to_dict(None) for layer in layers])
    graph = migrate_legacy_stack(stack)
    service = ColorGraphService()
    node = next(n for n in graph.nodes if n.node_type == "corrector")
    set1 = service.apply_intent(graph, "set_node_keyframe", {
        "node_id": node.node_id, "param": "exposure",
        "frame": 10, "value": 0.2})
    assert set1.ok, set1.diagnostics
    set2 = service.apply_intent(set1.graph, "set_node_keyframe", {
        "node_id": node.node_id, "param": "exposure",
        "frame": 30, "value": 0.8})
    assert set2.ok, set2.diagnostics
    return set2.graph, sid, node.node_id


class _FakePlayback:
    """FileController 播放面的鸭子替身（记录上行调用，不下真 seek）。"""

    def __init__(self, total: int = 120) -> None:
        self.total = total
        self.calls: list[tuple[str, int]] = []
        self.clip = SimpleNamespace(fps=24, timeline_duration=total)

    def _active_color_timeline_clip_playback_context(self):
        return {"timeline_id": "t1", "clip": self.clip,
                "effective_source_start": None}

    def _color_timeline_clip_total_frames(self):
        return self.total

    def on_video_seek(self, frame: int) -> None:
        self.calls.append(("seek", int(frame)))

    def on_video_scrub_started(self, frame: int) -> None:
        self.calls.append(("scrub_started", int(frame)))

    def on_video_scrub_moved(self, frame: int) -> None:
        self.calls.append(("scrub_moved", int(frame)))

    def on_video_scrub_finished(self, frame: int) -> None:
        self.calls.append(("scrub_finished", int(frame)))


def _session(clip_id: str = "c1") -> SimpleNamespace:
    return SimpleNamespace(project_id="p1", timeline_id="t1", clip_id=clip_id,
                           color_stack_id="s1", session_id="sess-1")


_UNSET = object()


def _host(qtbot, graph, *, session=_UNSET, playback=None):
    panel = ColorKeyframesPanel(demo=False)
    qtbot.addWidget(panel)
    playback = playback or _FakePlayback()
    host = ColorKeyframesHost(
        panel,
        graph_provider=lambda: graph,
        active_node_provider=lambda: "",
        session_provider=lambda: (_session() if session is _UNSET
                                  else session),
        playback=playback,
    )
    return panel, host, playback


# ---------------------------------------------------------------------------
# 投影回填
# ---------------------------------------------------------------------------


def test_refresh_pushes_readonly_projection(qtbot):
    graph, _sid, node_id = _graph_with_keyframes()
    panel, host, _playback = _host(qtbot, graph)
    assert host.refresh() is True
    snapshot = panel.snapshot
    assert snapshot.context.startswith("p1|t1|c1|s1")
    assert snapshot.frame_count == 120
    assert snapshot.timecode_fps == 24
    assert len(snapshot.tracks) == 3  # 三个 Corrector 节点行
    first = snapshot.tracks[0]
    assert first.id == f"node:{node_id}"
    leaf_ids = {child.id for child in first.children}
    # grade 叶轨（图命令域）与窗口叶轨（legacy 合并域）同树投影
    assert f"node:{node_id}|exposure" in leaf_ids
    assert any(leaf_id.endswith("|mask.11111111-2222-3333-4444-555555555555.center")
               for leaf_id in leaf_ids)
    exposure = next(child for child in first.children
                    if child.id == f"node:{node_id}|exposure")
    assert [marker.frame for marker in exposure.markers] == [10, 30]
    assert all(marker.kind == "dynamic" for marker in exposure.markers)


def test_refresh_idempotent_until_revision_changes(qtbot):
    from src.features.color.core.color_graph_service import ColorGraphService

    graph, _sid, node_id = _graph_with_keyframes()
    panel, host, _playback = _host(qtbot, graph)
    assert host.refresh() is True
    assert host.refresh() is False  # 同 context + 同 revision：跳过
    service = ColorGraphService()
    bumped = service.apply_intent(graph, "set_node_keyframe", {
        "node_id": node_id, "param": "exposure",
        "frame": 30, "value": 0.9})  # 同帧覆盖，值变更推 revision
    assert bumped.ok
    graph = bumped.graph
    # graph_provider 闭包捕获的是旧对象——真实装配读 bound_graph；
    # 这里直接换 provider 验证 revision 变化触发重建
    host._graph_provider = lambda: graph
    assert host.refresh() is True


def test_no_playback_context_keeps_existing_snapshot(qtbot):
    graph, _sid, _node = _graph_with_keyframes()
    panel, host, _playback = _host(qtbot, graph, session=None)
    assert host.refresh() is False
    assert panel.snapshot.context == ""
    playback = _FakePlayback()
    playback._active_color_timeline_clip_playback_context = lambda: None
    panel2, host2, _ = _host(qtbot, graph, playback=playback)
    assert host2.refresh() is False
    assert panel2.snapshot.context == ""


def test_snapshot_builder_aggregates_metadata(qtbot):
    graph, _sid, _node = _graph_with_keyframes()
    snapshot = build_keyframes_snapshot(
        context="ctx", frame_count=48, timecode_fps=25, playhead=7,
        corrector="node:missing", graph=graph)
    assert snapshot.context == "ctx"
    assert snapshot.timecode_fps == 25
    # corrector 不在树内时回退首行（面板 scope_row 的稳定目标）
    assert snapshot.corrector == snapshot.tracks[0].id


# ---------------------------------------------------------------------------
# 播放头双向同步
# ---------------------------------------------------------------------------


def test_playhead_backfill_does_not_echo_seek(qtbot):
    graph, _sid, _node = _graph_with_keyframes()
    panel, host, playback = _host(qtbot, graph)
    assert host.refresh() is True
    host.on_local_frame_changed(50)
    assert panel.playhead == 50
    assert playback.calls == []  # 程序性回填零回声
    host.on_local_frame_changed(10_000)  # 越界钳制
    assert panel.playhead == 119


def test_playhead_backfill_skipped_for_demo_snapshot(qtbot):
    graph, _sid, _node = _graph_with_keyframes()
    panel, host, playback = _host(qtbot, graph, session=None)
    host.on_local_frame_changed(50)
    assert panel.playhead == 0  # 未推真实投影前不回填
    assert playback.calls == []


def test_seek_request_routes_once_through_existing_protocol(qtbot):
    graph, _sid, _node = _graph_with_keyframes()
    panel, host, playback = _host(qtbot, graph)
    assert host.refresh() is True
    panel.seek(10)
    assert playback.calls == [("seek", 10)]
    panel.seek(10)  # 同帧不再发（面板防回环约定）
    assert len(playback.calls) == 1


def test_ruler_scrub_uses_viewport_scrub_lane(qtbot):
    graph, _sid, _node = _graph_with_keyframes()
    panel, host, playback = _host(qtbot, graph)
    assert host.refresh() is True
    panel.scrub_started.emit()
    panel.seek(10)
    panel.seek(20)
    panel.seek(30)
    panel.scrub_finished.emit()
    kinds = [kind for kind, _frame in playback.calls]
    # 首个 seek 初始化车道（scrub_started），其余走 moved，release 结算
    assert kinds == ["scrub_started", "scrub_moved", "scrub_moved",
                     "scrub_finished"]
    assert playback.calls[-1] == ("scrub_finished", 30)  # release 最后一帧


def test_scrub_finished_without_lane_start_is_noop(qtbot):
    graph, _sid, _node = _graph_with_keyframes()
    panel, host, playback = _host(qtbot, graph)
    assert host.refresh() is True
    panel.scrub_started.emit()
    panel.scrub_finished.emit()  # 手势内无帧变化：不碰车道
    assert playback.calls == []


# ---------------------------------------------------------------------------
# 只读边界与会话切换
# ---------------------------------------------------------------------------


def test_edit_request_is_dropped_readonly(qtbot):
    graph, _sid, node_id = _graph_with_keyframes()
    panel, host, playback = _host(qtbot, graph)
    assert host.refresh() is True
    leaf_id = f"node:{node_id}|exposure"
    panel.request("add", ((leaf_id, 12),))  # context 匹配 → 只读丢弃
    assert playback.calls == []  # 无 seek、无工程副作用
    stale = panel.request.__self__  # noqa: F841 — request 内部已用当前快照
    # 过期 context 也不产生副作用（换会话后旧面板请求到达）
    from src.features.color.ui.color_keyframes_view_state import EditRequest
    host._on_edit_requested(EditRequest(
        context="p0|t0|c0|s0", action="add", targets=((leaf_id, 1),)))
    assert playback.calls == []


def test_context_switch_rebuilds_projection(qtbot):
    graph, _sid, _node = _graph_with_keyframes()
    session = _session()
    panel, host, _playback = _host(qtbot, graph, session=session)
    assert host.refresh() is True
    first_context = panel.snapshot.context
    session.clip_id = "c2"  # 切片段：身份键变化 → 面板换投影
    assert host.refresh(force=True) is True
    assert panel.snapshot.context != first_context
    assert panel.snapshot.context.endswith("|c2|s1")


def test_bound_graph_accessor_reads_bound_stack():
    from src.app.controllers.color_graph_controller import ColorGraphAppHost
    from src.features.edit.core.project import EditProject

    graph, _sid, _node = _graph_with_keyframes()
    project, sid2 = EditProject.create(name="kf02b").create_color_stack(
        asset_id="a1", name="s")
    project.color_stacks[sid2] = (
        project.color_stacks[sid2].with_color_graph(graph))
    host = ColorGraphAppHost(
        get_project=lambda: project,
        set_project=lambda _project: None,
        project_stack=None,
        engine_provider=lambda: None,
        request_render=lambda: None,
    )
    assert host.bound_graph() is None  # 未绑定栈
    host._bound_stack_id = sid2
    assert host.bound_graph() is graph


def test_panel_scrub_signals_exist(qtbot):
    panel = ColorKeyframesPanel(demo=False)
    qtbot.addWidget(panel)
    assert hasattr(panel, "scrub_started")
    assert hasattr(panel, "scrub_finished")
