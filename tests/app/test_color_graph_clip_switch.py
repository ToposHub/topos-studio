"""B06.4 clip/asset/project 切换协议测试 —— E13 观察者 + generation 语义。

计划验收：A/B clip 快切；切 clip 时当前事务完成或取消有明确结果；
晚到 selection/ack/thumbnail/AI 事件全部丢弃；scope 不得跨 clip 复用。
"""

from __future__ import annotations

import pytest

from src.app.controllers.color_graph_controller import (
    GraphProjectBridge,
    GraphProjection,
    GraphSessionState,
    ColorGraphPanelHost,
    ColorGraphSessionCoordinator,
    IntentRejection,
    LegacyGradingAdapter,
)
from src.features.color.core.color_graph_migration import migrate_legacy_stack
from src.features.color.core.color_session import (
    ColorSession,
    ColorSessionManager,
)
from src.features.edit.core.commands import ProjectCommandStack
from src.features.edit.core.project import EditProject
from tests.color.fixtures.color_graph import f03_layer_stack


def _stack_graph(stack):
    return migrate_legacy_stack(
        stack.with_layers([l.to_dict(None) for l in f03_layer_stack()]))


@pytest.fixture
def two_clip_setup():
    """工程：clip A / clip B 两个栈（各带图）+ 桥 + host。"""
    project, stack_a = EditProject.create(name="switch").create_color_stack(
        asset_id="a1", name="A")
    project, stack_b = project.create_color_stack(asset_id="a2", name="B")
    project = project.update_color_stack(
        stack_a, color_graph=_stack_graph(project.color_stacks[stack_a]))
    project = project.update_color_stack(
        stack_b, color_graph=_stack_graph(project.color_stacks[stack_b]))
    holder = {"project": project}
    cmd_stack = ProjectCommandStack()
    coord = ColorGraphSessionCoordinator(
        session_id="sess-A", on_load_graph=lambda *_: None)
    coord.begin_hydrate("clip", "clip-A")
    coord.hydrate_projection(GraphProjection(
        session_id="sess-A", session_generation=0, scope="clip",
        scope_owner_id="clip-A",
        graph=project.color_stacks[stack_a].color_graph.to_dict(),
        graph_revision=project.color_stacks[stack_a].color_graph.graph_revision))
    bridge = GraphProjectBridge(
        coord, cmd_stack, lambda: holder["project"],
        set_project=lambda p: holder.__setitem__("project", p))
    bridge.bind_stack(stack_a)
    host = ColorGraphPanelHost(bridge, coord)
    return host, bridge, coord, cmd_stack, holder, stack_a, stack_b


def _make_session(manager: ColorSessionManager, stack_id: str) -> ColorSession:
    """构造最小合法 ColorSession（timeline_clip 来源）。"""
    from src.features.color.core.color_session import SourceKind
    return ColorSession(
        session_id=f"sess-{stack_id[:8]}",
        source_kind=SourceKind.TIMELINE_CLIP
        if hasattr(SourceKind, "TIMELINE_CLIP") else SourceKind.VIDEO_ASSET,
        project_id="proj-1",
        color_stack_id=stack_id,
        clip_id="clip-x",
        timeline_id="tl-1",
        asset_id="asset-1")


class TestSessionManagerObservers:
    def test_observer_notified_on_set_and_clear(self, two_clip_setup):
        host, bridge, coord, cmd_stack, holder, stack_a, stack_b = \
            two_clip_setup
        manager = ColorSessionManager()
        events = []
        manager.add_session_observer(events.append)
        session = _make_session(manager, stack_a)
        manager.set_current_session(session)
        assert events[-1] is session
        manager.clear_current_session()
        assert events[-1] is None

    def test_observer_failure_does_not_block_switch(self, two_clip_setup):
        host, bridge, coord, cmd_stack, holder, stack_a, stack_b = \
            two_clip_setup
        manager = ColorSessionManager()
        def boom(_session):
            raise RuntimeError("observer boom")
        manager.add_session_observer(boom)
        session = _make_session(manager, stack_b)
        manager.set_current_session(session)
        assert manager.get_current_session() is session


class TestClipSwitchProtocol:
    def test_switch_flushes_pending_and_increments_generation(
            self, two_clip_setup):
        host, bridge, coord, cmd_stack, holder, stack_a, stack_b = \
            two_clip_setup
        adapter = LegacyGradingAdapter(host, bridge)
        corrector = next(
            n["node_id"] for n in coord.last_ack.graph["nodes"]
            if n["node_type"] == "corrector")
        # 手势进行中切 clip
        adapter.begin_gesture("g-A")
        adapter.submit_param_change(corrector, "exposure", 0.7, "g-A")
        generation = host.on_clip_switch("sess-B", "clip-B",
                                         flush_pending=True)
        assert generation == 1
        assert not bridge._buffer.active
        assert len(cmd_stack._undo_stack) == 1, "未完成手势必须 flush（明确结果）"
        assert coord.state == GraphSessionState.HYDRATING

    def test_switch_with_cancel_discards_pending(self, two_clip_setup):
        host, bridge, coord, cmd_stack, holder, stack_a, stack_b = \
            two_clip_setup
        adapter = LegacyGradingAdapter(host, bridge)
        corrector = next(
            n["node_id"] for n in coord.last_ack.graph["nodes"]
            if n["node_type"] == "corrector")
        adapter.begin_gesture("g-cancel")
        adapter.submit_param_change(corrector, "exposure", 9.0, "g-cancel")
        host.on_clip_switch("sess-B", "clip-B", flush_pending=False)
        assert not bridge._buffer.active
        assert cmd_stack._undo_stack == [], "取消路径不得留下半事务"

    def test_late_ack_from_previous_clip_dropped(self, two_clip_setup):
        host, bridge, coord, cmd_stack, holder, stack_a, stack_b = \
            two_clip_setup
        host.on_clip_switch("sess-B", "clip-B")
        stale = GraphProjection(
            session_id="sess-A", session_generation=0, scope="clip",
            scope_owner_id="clip-A", graph={"stale": True},
            graph_revision=99)
        assert coord.accept_ack(stale) is False
        assert coord.last_ack is None or coord.last_ack.graph != {"stale": True}

    def test_late_reject_after_switch_ignored(self, two_clip_setup):
        host, bridge, coord, *_ = two_clip_setup
        host.on_clip_switch("sess-B", "clip-B")
        assert coord.accept_reject(
            IntentRejection(code="late", message="old clip")) is False
        assert coord.state == GraphSessionState.HYDRATING

    def test_new_clip_hydrates_its_own_graph(self, two_clip_setup):
        host, bridge, coord, cmd_stack, holder, stack_a, stack_b = \
            two_clip_setup
        graph_a_before = holder["project"].color_stacks[
            stack_a].color_graph.to_dict()
        host.on_clip_switch("sess-B", "clip-B")
        bridge.bind_stack(stack_b)
        coord.hydrate_projection(GraphProjection(
            session_id="sess-B", session_generation=coord.generation,
            scope="clip", scope_owner_id="clip-B",
            graph=holder["project"].color_stacks[stack_b].color_graph.to_dict(),
            graph_revision=holder["project"].color_stacks[
                stack_b].color_graph.graph_revision))
        assert coord.state == GraphSessionState.READY
        # 切 clip 不污染另一栈
        assert (holder["project"].color_stacks[stack_a].color_graph.to_dict()
                == graph_a_before)

    def test_generation_guard_blocks_cross_session_submit(
            self, two_clip_setup):
        from src.app.controllers.color_graph_controller import (
            ColorGraphIntentNormalizer,
        )
        host, bridge, coord, cmd_stack, holder, stack_a, stack_b = \
            two_clip_setup
        host.on_clip_switch("sess-B", "clip-B")
        normalizer = ColorGraphIntentNormalizer(
            session_id="sess-A", scope_owner_id="clip-A",
            stack_id=stack_a, expected_graph_revision=None,
            session_generation=0)  # 旧 generation
        intent = normalizer.normalize(
            "add_serial", {"scope": "clip", "compound_path": [],
                           "payload": {"arguments": {}}})
        assert bridge.submit(intent) is False
        assert cmd_stack._undo_stack == []

    def test_rapid_ab_switch_keeps_states_separate(self, two_clip_setup):
        host, bridge, coord, cmd_stack, holder, stack_a, stack_b = \
            two_clip_setup
        for new_session, owner, stack_id in (
                ("sess-B", "clip-B", stack_b),
                ("sess-A", "clip-A", stack_a),
                ("sess-B", "clip-B", stack_b)):
            host.on_clip_switch(new_session, owner)
            bridge.bind_stack(stack_id)
        assert coord.session_id == "sess-B"
        assert coord.generation == 3


class TestScopeHygiene:
    def test_scope_draft_not_saved_into_new_scope(self, two_clip_setup):
        """scope_changed 只导航：不得把当前 draft 保存进新 scope（§6.4）。"""
        host, bridge, coord, *_ = two_clip_setup
        host._on_scope_changed("timeline")
        assert getattr(host, "_pending_scope", None) == "timeline"
        # 切 clip 后 pending scope 不残留旧 owner 语义
        host.on_clip_switch("sess-B", "clip-B")
        # _pending_scope 是导航意图，不产生工程写入
        assert cmd_stack_active_writes(host) == 0


def cmd_stack_active_writes(host) -> int:
    """统计 host 生命周期内的工程写入（通过桥的 undo 栈长度）。"""
    stack = host._bridge._project_stack
    return len(stack._undo_stack)
