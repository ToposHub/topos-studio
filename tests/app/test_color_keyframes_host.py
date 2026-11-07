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
    # 注入窗口形状 + 轨道（Layer 域：mask.<shape_id>.<field>，随 legacy
    # 载荷合并；形状与轨道同在——悬空轨道不投影不可写行）
    shape_id = "11111111-2222-3333-4444-555555555555"
    from src.features.color.core.layer import MaskShape
    shape = MaskShape(MaskShape.CIRCLE)
    shape.id = shape_id
    shape.name = "跟踪窗口"
    layers[0].mask_shapes = [shape]
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
    assert len([track for track in snapshot.tracks if track.scope == "color"]) == 3
    assert not snapshot.tracks[-1].available  # Sizing placeholder is explicitly read-only.
    first = snapshot.tracks[0]
    assert first.id == f"node:{node_id}"
    leaves = [leaf for group in first.children for leaf in group.children]
    leaf_ids = {leaf.id for leaf in leaves}
    # grade 叶轨（图命令域）与窗口叶轨（legacy 合并域）同树投影
    assert f"node:{node_id}|exposure" in leaf_ids
    assert any(leaf_id.endswith("|mask.11111111-2222-3333-4444-555555555555.center")
               for leaf_id in leaf_ids)
    exposure = next(child for child in leaves
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
        corrector="", graph=graph)
    assert snapshot.context == "ctx"
    assert snapshot.timecode_fps == 25
    # 初始没有节点选择时回退首行。
    assert snapshot.corrector == snapshot.tracks[0].id


def test_clip_trim_projects_local_frames_and_edits_source_frames(qtbot):
    graph, _sid, node_id = _graph_with_keyframes()
    playback = _FakePlayback(total=20)
    playback.clip.source_start = 20
    panel, host, _ = _host(qtbot, graph, playback=playback)
    submissions = []
    host._submit_batch = lambda edits: submissions.append(edits) or True
    assert host.refresh()
    leaf = f"node:{node_id}|exposure"
    assert (leaf, 10) in panel._markers  # Source 30.
    assert (leaf, 0) not in panel._markers  # Source 10 is outside the trim.
    panel.request("add", ((leaf, 12),))
    panel.request("move", ((leaf, 10),), delta=2)
    assert submissions[0][0]["frame"] == 32
    assert (submissions[1][0]["frame"], submissions[1][0]["to_frame"]) == (30, 32)
    playback.clip.speed = 0.5
    assert host.refresh()
    assert panel.snapshot.edit_notice
    panel.request("add", ((leaf, 13),))
    assert len(submissions) == 2


def test_active_corrector_refresh_and_expired_binding_cannot_submit(qtbot):
    graph, _sid, _node = _graph_with_keyframes()
    panel, host, _ = _host(qtbot, graph)
    assert host.refresh()
    second = panel.snapshot.tracks[1].id
    host._active_node_provider = lambda: second.removeprefix("node:")
    assert host.refresh()
    assert panel.snapshot.corrector == second
    submissions = []
    host._submit_batch = lambda edits: submissions.append(edits) or True
    host._session_provider = lambda: None
    panel.request("add", ((f"{second}|exposure", 16),))
    assert not submissions
    assert not panel.snapshot.context


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
                     "scrub_finished", "seek"]
    assert playback.calls[-1] == ("seek", 30)


def test_cancelling_scrub_does_not_seek(qtbot):
    graph, _sid, _node = _graph_with_keyframes()
    panel, host, playback = _host(qtbot, graph)
    assert host.refresh()
    panel.scrub_started.emit()
    panel.seek(10)
    panel.scrub_cancelled.emit()
    assert playback.calls == [("scrub_started", 10), ("scrub_finished", 10)]


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
    assert panel.snapshot.context.split("|")[2:4] == ["c2", "s1"]


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


# ---------------------------------------------------------------------------
# KF-03：真实写入口（真实 AppHost + ProjectCommandStack 全链路）
# ---------------------------------------------------------------------------


class TestRealWriteEntry:
    """验收：一次用户动作一个历史条目；拒绝无部分写入；Undo/Redo 对称。"""

    def _wired(self, qtbot, *, with_window: bool = False):
        from unittest.mock import Mock

        from src.app.controllers.color_graph_controller import (
            ColorGraphAppHost,
        )
        from src.features.color.core.color_graph_migration import (
            migrate_legacy_stack,
        )
        from src.features.edit.core.commands import ProjectCommandStack
        from src.features.edit.core.project import EditProject
        from tests.color.fixtures.color_graph import f03_layer_stack

        project, sid = EditProject.create(name="kf03w").create_color_stack(
            asset_id="a1", name="s")
        layers = f03_layer_stack()
        window_shape_id = ""
        if with_window:
            from src.features.color.core.layer import MaskShape
            shape = MaskShape(MaskShape.CIRCLE)
            window_shape_id = shape.id
            shape.name = "圆窗"
            layers[0].mask_shapes = [shape]
        stack = project.color_stacks[sid].with_layers(
            [layer.to_dict(None) for layer in layers])
        graph = migrate_legacy_stack(stack)
        project = project.update_color_stack(sid, color_graph=graph)
        holder = {"project": project, "session": _session()}
        engine = Mock()
        engine.layers = []
        app_host = ColorGraphAppHost(
            get_project=lambda: holder["project"],
            set_project=lambda p: holder.__setitem__("project", p),
            project_stack=ProjectCommandStack(),
            engine_provider=lambda: engine,
            request_render=lambda: None, panels_provider=lambda: {})
        app_host.on_clip_stack_loaded(sid)
        panel = ColorKeyframesPanel(demo=False)
        qtbot.addWidget(panel)
        kf_host = ColorKeyframesHost(
            panel,
            graph_provider=lambda: app_host.bound_graph(),
            active_node_provider=lambda: "",
            session_provider=lambda: holder["session"],
            playback=_FakePlayback(),
            submit_batch=app_host.submit_keyframe_batch,
            submit_action=app_host.submit_panel_action,
        )
        assert kf_host.refresh() is True
        correctors = [n.node_id for n in app_host.bound_graph().nodes
                      if n.node_type == "corrector"]
        if with_window:
            return panel, kf_host, app_host, holder, correctors, window_shape_id
        return panel, kf_host, app_host, holder, correctors

    def _undo(self, app_host, holder):
        return app_host.bridge._project_stack.undo(holder["project"])

    def _depth(self, app_host) -> int:
        # undo() 会弹栈，深度探查只能读栈长（undo 栈 = 已提交命令数）
        return len(app_host.bridge._project_stack._undo_stack)

    def test_add_is_one_history_entry_with_symmetric_undo(self, qtbot):
        panel, _host, app_host, holder, nodes = self._wired(qtbot)
        revision0 = app_host.bound_graph().graph_revision
        panel.request("add", ((f"node:{nodes[0]}|exposure", 12),))
        graph = app_host.bound_graph()
        assert graph.graph_revision == revision0 + 1
        node = next(n for n in graph.nodes if n.node_id == nodes[0])
        track = node.params["grade_keyframes"]["exposure"]
        assert any(int(k["frame"]) == 12 for k in track["keyframes"])
        assert self._depth(app_host) == 1  # 恰好一条历史
        undone = self._undo(app_host, holder)
        assert undone is not None
        undone_node = next(n for n in
                           undone.color_stacks[
                               next(iter(undone.color_stacks))]
                           .color_graph.nodes if n.node_id == nodes[0])
        assert not (undone_node.params.get("grade_keyframes") or {})
        assert self._depth(app_host) == 0

    def test_two_node_move_is_single_history_entry(self, qtbot):
        panel, _host, app_host, holder, nodes = self._wired(qtbot)
        leaf_a, leaf_b = (f"node:{nodes[0]}|exposure",
                          f"node:{nodes[1]}|contrast")
        panel.request("add", ((leaf_a, 12),))
        panel.request("add", ((leaf_b, 24),))
        assert self._depth(app_host) == 2
        panel.request("move", ((leaf_a, 12), (leaf_b, 24)), delta=4)
        graph = app_host.bound_graph()

        def frames(node_id, param):
            node = next(n for n in graph.nodes if n.node_id == node_id)
            track = (node.params.get("grade_keyframes") or {}).get(param) or {}
            return sorted(int(k["frame"]) for k in track["keyframes"])

        assert frames(nodes[0], "exposure") == [16]
        assert frames(nodes[1], "contrast") == [28]
        assert self._depth(app_host) == 3  # 批量 move 只加一条
        undone = self._undo(app_host, holder)
        assert undone is not None  # 一次 undo 恢复两节点
        undone_graph = undone.color_stacks[
            next(iter(undone.color_stacks))].color_graph

        def undone_frames(node_id, param):
            node = next(n for n in undone_graph.nodes
                        if n.node_id == node_id)
            track = (node.params.get("grade_keyframes") or {}).get(param) or {}
            return sorted(int(k["frame"]) for k in track["keyframes"])

        assert undone_frames(nodes[0], "exposure") == [12]
        assert undone_frames(nodes[1], "contrast") == [24]

    def test_locked_node_rejection_leaves_no_partial_write(self, qtbot):
        panel, _host, app_host, holder, nodes = self._wired(qtbot)
        leaf_a, leaf_b = (f"node:{nodes[0]}|exposure",
                          f"node:{nodes[1]}|contrast")
        panel.request("add", ((leaf_a, 12),))
        panel.request("add", ((leaf_b, 24),))
        panel.request("lock", ((f"node:{nodes[1]}", 0),), enabled=True)
        assert self._depth(app_host) == 3
        revision = app_host.bound_graph().graph_revision
        # 两节点批量 move：目标节点已锁 → 整批拒绝
        panel.request("move", ((leaf_a, 12), (leaf_b, 24)), delta=4)
        graph = app_host.bound_graph()
        assert graph.graph_revision == revision  # 无部分写入
        assert self._depth(app_host) == 3  # 无新历史条目

    def test_auto_toggle_routes_to_recorder_and_snapshot(self, qtbot):
        from src.app.controllers.color_keyframe_recorder import (
            KeyframeAutoRecorder,
        )
        panel, host, app_host, _holder, nodes = self._wired(qtbot)
        recorder = KeyframeAutoRecorder()
        host._recorder = recorder
        row_id = f"node:{nodes[0]}"
        panel.request("auto", ((row_id, 0),), enabled=True)
        assert recorder.auto_nodes() == frozenset({nodes[0]})
        assert host.refresh(force=True) is True
        node_row = next(track for track in panel.snapshot.tracks
                        if track.id == row_id)
        assert node_row.auto is True
        assert all(child.auto for child in node_row.children)  # 开关作用后代
        panel.request("auto", ((row_id, 0),), enabled=False)
        assert recorder.auto_nodes() == frozenset()

    def test_stale_context_request_is_dropped(self, qtbot):
        panel, host, app_host, holder, nodes = self._wired(qtbot)
        revision = app_host.bound_graph().graph_revision
        from src.features.color.ui.color_keyframes_view_state import EditRequest
        host._on_edit_requested(EditRequest(
            context="stale|ctx", action="add",
            targets=((f"node:{nodes[0]}|exposure", 12),)))
        assert app_host.bound_graph().graph_revision == revision

    # ── KF-07 复检批次：qualifier canonical 名 / 正式剪贴板 / 窗口写入 ──

    def test_qualifier_add_writes_canonical_track_name(self, qtbot):
        """回归：宿主曾发裸字段名被批命令白名单拒（qualifier 写入口
        从未生效）——必须落 canonical 全名 ``qualifier.<field>``。"""
        panel, _host, app_host, _holder, nodes = self._wired(qtbot)
        revision0 = app_host.bound_graph().graph_revision
        panel.request("add", ((f"node:{nodes[0]}|qualifier.lum_low", 12),))
        graph = app_host.bound_graph()
        assert graph.graph_revision == revision0 + 1
        node = next(n for n in graph.nodes if n.node_id == nodes[0])
        track = node.params["grade_keyframes"]["qualifier.lum_low"]
        assert any(int(k["frame"]) == 12 for k in track["keyframes"])

    def test_paste_uses_captured_payload_after_source_delete(self, qtbot):
        """正式剪贴板：复制时捕获不可变载荷——源键删除后粘贴仍写入
        复制时的值（不读已失效源）。"""
        panel, host, app_host, _holder, nodes = self._wired(qtbot)
        leaf = f"node:{nodes[0]}|exposure"
        panel.request("add", ((leaf, 10),))
        assert host.refresh(force=True) is True  # 面板标记回填
        graph = app_host.bound_graph()
        node = next(n for n in graph.nodes if n.node_id == nodes[0])
        value = next(k["value"] for k in
                     node.params["grade_keyframes"]["exposure"]["keyframes"]
                     if int(k["frame"]) == 10)
        panel.selected = {(leaf, 10)}
        panel.copy()
        assert host._clipboard_entries  # 复制即捕获
        panel.request("delete", ((leaf, 10),))
        assert host.refresh(force=True) is True
        panel.paste(40, leaf)
        graph = app_host.bound_graph()
        node = next(n for n in graph.nodes if n.node_id == nodes[0])
        track = node.params["grade_keyframes"]["exposure"]
        pasted = next(k for k in track["keyframes"]
                      if int(k["frame"]) == 40)
        assert pasted["value"] == value

    def test_cross_context_paste_uses_captured_payload(self, qtbot):
        """跨片段粘贴：源片段离开（context 已切换）后按捕获载荷写入
        当前活动 Corrector 的同名叶轨。"""
        panel, host, app_host, holder, nodes = self._wired(qtbot)
        leaf = f"node:{nodes[0]}|exposure"
        panel.request("add", ((leaf, 10),))
        assert host.refresh(force=True) is True
        panel.selected = {(leaf, 10)}
        panel.copy()
        # 切片段：session clip_id 变化 → context 变化，投影重建
        holder["session"] = _session(clip_id="c2")
        assert host.refresh(force=True) is True
        assert panel.snapshot.context != panel._clipboard_context
        revision = app_host.bound_graph().graph_revision
        panel.paste(60, "")
        graph = app_host.bound_graph()
        assert graph.graph_revision == revision + 1
        node = next(n for n in graph.nodes if n.node_id == nodes[0])
        track = node.params["grade_keyframes"]["exposure"]
        assert any(int(k["frame"]) == 60 for k in track["keyframes"])

    def test_paste_arity_mismatch_rejects_whole_batch(self, qtbot):
        """能力签名：vec3 源粘到标量轨 → 整批拒绝，无部分写入。"""
        panel, host, app_host, _holder, nodes = self._wired(qtbot)
        lift_leaf = f"node:{nodes[0]}|lift"
        exposure_leaf = f"node:{nodes[0]}|exposure"
        panel.request("add", ((lift_leaf, 12),))
        assert host.refresh(force=True) is True
        panel.selected = {(lift_leaf, 12)}
        panel.copy()
        revision = app_host.bound_graph().graph_revision
        panel.paste(30, exposure_leaf)  # 单源重映射到标量轨
        assert app_host.bound_graph().graph_revision == revision
        node = next(n for n in app_host.bound_graph().nodes
                    if n.node_id == nodes[0])
        track = (node.params.get("grade_keyframes") or {}).get("exposure")
        assert not track  # 无部分写入

    def test_window_add_samples_shape_and_writes_mask_track(self, qtbot):
        """窗口轨道图域写入：面板加键采样形状现值，落 canonical
        ``mask.<shape_id>.<field>`` 轨道。"""
        panel, _host, app_host, _holder, nodes, shape_id = self._wired(
            qtbot, with_window=True)
        leaf = f"node:{nodes[0]}|mask.{shape_id}.center"
        assert any(child.id == leaf
                   for row in panel.snapshot.tracks
                   for group in row.children
                   for child in group.children)  # 形状叶轨常驻投影
        revision0 = app_host.bound_graph().graph_revision
        panel.request("add", ((leaf, 8),))
        graph = app_host.bound_graph()
        assert graph.graph_revision == revision0 + 1
        node = next(n for n in graph.nodes if n.node_id == nodes[0])
        track = node.params["grade_keyframes"][f"mask.{shape_id}.center"]
        key = next(k for k in track["keyframes"] if int(k["frame"]) == 8)
        assert len(tuple(key["value"])) == 2  # vec2（内存态 tuple/list 均可）

    def test_window_missing_shape_rejected_fail_closed(self, qtbot):
        """形状存在性 fail-closed：不存在的 shape_id 不复活轨道。"""
        _panel, _h, app_host, _holder, nodes, _sid = self._wired(
            qtbot, with_window=True)
        revision = app_host.bound_graph().graph_revision
        accepted = app_host.submit_keyframe_batch([{
            "action": "set", "node_id": nodes[0],
            "param": "mask.00000000-0000-0000-0000-000000000000.center",
            "frame": 4, "value": [0.5, 0.5],
        }])
        assert accepted is False
        assert app_host.bound_graph().graph_revision == revision

    def test_window_provider_evaluates_shapes_per_frame(self, qtbot):
        """预览同源：provider 帧模式逐帧补写形状（与导出同函数、基底
        求值不累积），帧盲模式保持 base。"""
        from src.features.color.core.color_graph_param_source import (
            graph_node_layer_provider,
        )
        panel, _host, app_host, _holder, nodes, shape_id = self._wired(
            qtbot, with_window=True)

        def _center(shapes):
            return tuple(next(s for s in shapes
                              if s.get("id") == shape_id)["center"])

        # base 形状先定格（帧盲对照用）
        base_center = _center(
            graph_node_layer_provider(app_host.bound_graph())(
                nodes[0])["mask_shapes"])
        assert app_host.submit_keyframe_batch([
            {"action": "set", "node_id": nodes[0],
             "param": f"mask.{shape_id}.center", "frame": 0,
             "value": [0.5, 0.5]},
            {"action": "set", "node_id": nodes[0],
             "param": f"mask.{shape_id}.center", "frame": 24,
             "value": [0.9, 0.1]},
        ])
        graph = app_host.bound_graph()

        def center_at(frame):
            provider = graph_node_layer_provider(graph, frame=frame)
            payload = provider(nodes[0])
            return _center(payload["mask_shapes"])

        assert center_at(0) == (0.5, 0.5)
        assert center_at(12) == (0.7, 0.3)  # 线性中点
        assert center_at(24) == (0.9, 0.1)
        # 帧盲 provider 不补写（base 形状原样）
        blind = graph_node_layer_provider(graph)(nodes[0])
        assert _center(blind["mask_shapes"]) == base_center


class TestManualAddSamples:
    """R6：手工 Add 采样当前求值结果（动画轨不复制不随时间变的 base）。"""

    def test_add_samples_evaluated_value_on_animated_track(self, qtbot):
        panel, _host, app_host, _holder, nodes = (
            TestRealWriteEntry()._wired(qtbot))
        node_id = nodes[0]
        # 建动画轨 0@0=0.0 → 4@4=1.0（linear）
        assert app_host.submit_keyframe_batch([
            {"action": "set", "node_id": node_id, "param": "exposure",
             "frame": 0, "value": 0.0},
            {"action": "set", "node_id": node_id, "param": "exposure",
             "frame": 4, "value": 1.0},
        ])
        leaf = f"node:{node_id}|exposure"
        panel.request("add", ((leaf, 1),))  # local 1 = source 1 → 求值 0.25
        node = next(n for n in app_host.bound_graph().nodes
                    if n.node_id == node_id)
        track = node.params["grade_keyframes"]["exposure"]
        value_at_1 = next(k["value"] for k in track["keyframes"]
                          if int(k["frame"]) == 1)
        assert value_at_1 == pytest.approx(0.25)  # 不是 base 0.0


class TestRetimeReadOnlyMatrix:
    """R1：复杂帧映射显式只读（非 1× 正向同速率一律无写入口）。"""

    def _host(self, qtbot, **clip_attrs):
        graph, _sid, node_id = _graph_with_keyframes()
        playback = _FakePlayback(total=20)
        for key, value in clip_attrs.items():
            setattr(playback.clip, key, value)
        panel, host, _ = _host(qtbot, graph, playback=playback)
        return panel, host, node_id

    def test_speed_variants_are_read_only(self, qtbot):
        cases = [
            {"speed": 0.5},                        # 慢速
            {"speed": 2.0},                        # 快速
            {"speed": SimpleNamespace(speed=1.0, reverse=True,
                                      speed_points=())},   # 倒放
            {"speed": SimpleNamespace(speed=1.0, reverse=False,
                                      speed_points=(0.3, 0.9))},  # 速度曲线
            {"freeze_source_frame": 24},           # 冻结
        ]
        for clip_attrs in cases:
            panel, host, node_id = self._host(qtbot, **clip_attrs)
            assert host.refresh(), clip_attrs
            snapshot = panel.snapshot
            assert snapshot.edit_notice, clip_attrs  # 显式只读提示
            assert not panel.can_edit(f"node:{node_id}|exposure")
            submissions = []
            host._submit_batch = lambda edits: submissions.append(edits) or True
            panel.request("add", ((f"node:{node_id}|exposure", 12),))
            assert not submissions, clip_attrs  # 无写入口

    def test_mixed_frame_rate_ratio_is_read_only(self, qtbot):
        panel, host, node_id = self._host(
            qtbot, _source_over_timeline_fps_ratio=lambda: 1.5)
        assert host.refresh()
        assert panel.snapshot.edit_notice
        assert not panel.can_edit(f"node:{node_id}|exposure")

    def test_effective_source_start_wins_over_source_start(self, qtbot):
        # 有效源偏移（裁切后）优先于原始 source_start——写入落在真实源帧
        graph, _sid, node_id = _graph_with_keyframes()
        playback = _FakePlayback(total=20)
        playback.clip.source_start = 100
        playback._active_color_timeline_clip_playback_context = (
            lambda: {"timeline_id": "t1", "clip": playback.clip,
                     "effective_source_start": 36})
        panel, host, _ = _host(qtbot, graph, playback=playback)
        submissions = []
        host._submit_batch = lambda edits: submissions.append(edits) or True
        assert host.refresh()
        panel.request("add", ((f"node:{node_id}|exposure", 12),))
        assert submissions[0][0]["frame"] == 48  # 36 + 12，不是 100 + 12


class TestBindingIdentity:
    """R4：绑定描述 + A→B→A 迟到请求失效。"""

    def test_binding_description_reports_authoritative_view(self, qtbot):
        _panel, _host, app_host, holder, _nodes = (
            TestRealWriteEntry()._wired(qtbot))
        binding = app_host.binding_description()
        assert binding["view"] == "graph"
        assert binding["writable"] is True
        assert binding["stack_id"]
        graph = app_host.bound_graph()
        assert binding["graph_revision"] == graph.graph_revision

    def test_binding_description_marks_legacy_migration_view(self):
        # 无图旧栈：迁移只读视图（首次写入走建图命令事务）
        from unittest.mock import Mock

        from src.app.controllers.color_graph_controller import (
            ColorGraphAppHost,
        )
        from src.features.edit.core.commands import ProjectCommandStack
        from src.features.edit.core.project import EditProject
        from tests.color.fixtures.color_graph import f03_layer_stack
        project, sid = EditProject.create(name="r4").create_color_stack(
            asset_id="a1", name="s")
        stack = project.color_stacks[sid].with_layers(
            [layer.to_dict(None) for layer in f03_layer_stack()])
        project = project.update_color_stack(sid, **{})  # 无 color_graph
        holder = {"project": project}
        app_host = ColorGraphAppHost(
            get_project=lambda: holder["project"],
            set_project=lambda p: holder.__setitem__("project", p),
            project_stack=ProjectCommandStack(),
            engine_provider=lambda: Mock(),
            request_render=lambda: None, panels_provider=lambda: {})
        app_host.on_clip_stack_loaded(sid)
        binding = app_host.binding_description()
        assert binding["view"] == "migration"
        assert binding["stack_id"] == sid
        # 迁移视图仍可读（bound_graph 返回投影），写入经命令事务
        assert app_host.bound_graph() is not None

    def test_a_to_b_to_a_invalidates_stale_requests(self, qtbot):
        graph, _sid, node_id = _graph_with_keyframes()
        panel, host, playback = _host(qtbot, graph)
        session_a1 = _session()                    # A 首次进入
        host._session_provider = lambda: session_a1
        assert host.refresh()
        context_a1 = panel.snapshot.context
        submissions = []
        host._submit_batch = lambda edits: submissions.append(edits) or True
        panel.request("add", ((f"node:{node_id}|exposure", 8),))
        assert submissions  # A 首次上下文可写

        session_b = _session(clip_id="c2")         # 切到 B
        host._session_provider = lambda: session_b
        assert host.refresh(force=True)
        assert panel.snapshot.context != context_a1

        session_a2 = _session()                    # 切回 A：新 session_id
        session_a2.session_id = "sess-2"
        host._session_provider = lambda: session_a2
        assert host.refresh(force=True)
        context_a2 = panel.snapshot.context
        assert context_a2 != context_a1  # 同 ID 重进 ≠ 旧 context
        # A 旧上下文的迟到请求被整批拒（session_id 已换）
        from src.features.color.ui.color_keyframes_view_state import (
            EditRequest,
        )
        stale = EditRequest(
            action="add", context=context_a1,
            targets=((f"node:{node_id}|exposure", 9),))
        host._on_edit_requested(stale)
        assert len(submissions) == 1
