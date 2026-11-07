"""B06 剩余接线：ColorGraphAppHost（真实节点面板接权威图 + E09 图编辑
上下文 + scope owner 真实 ID）。

验收（计划 §6.4/§6.5 + 台账 B06 债务项）：
- clip 水合 → bind_stack + 投影水合（无图栈=迁移视图，首次命令建图）；
- 图命令提交（节点面板/grade 手势）→ 工程 command → 引擎重投影
  （apply_stack_to_engine + S04 图登记）+ 重渲染；
- grade 手势：图编辑上下文（选中 Corrector）经 grade_patch 事务合并，
  一次手势一个 command；未选中节点 = legacy 路径零变更；
- group scope owner：真实组 ID/阶段（§6.4），组栈重绑；
- 对齐：图节点 → 引擎活动层（source_layer_id 唯一许可路径）。
"""

from __future__ import annotations

from unittest.mock import Mock

import pytest

pytest.importorskip("PyQt6")

from src.app.controllers.color_graph_controller import (
    ColorGraphAppHost,
    ColorGraphPanelHost,
    GraphSessionState,
)
from src.features.edit.core.commands import ProjectCommandStack
from src.features.edit.core.color_group import (
    ColorGroupData,
    ColorGroupMember,
)
from src.features.edit.core.project import EditProject
from src.features.color.core.color_graph_migration import migrate_legacy_stack
from tests.color.fixtures.color_graph import f03_layer_stack


@pytest.fixture(scope="module")
def qapp():
    from PyQt6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


def _project_with_graph_stack():
    project, stack_id = EditProject.create(name="host").create_color_stack(
        asset_id="a1", name="s")
    stack_data = project.color_stacks[stack_id].with_layers(
        [l.to_dict(None) for l in f03_layer_stack()])
    graph = migrate_legacy_stack(stack_data)
    project = project.update_color_stack(stack_id, color_graph=graph)
    return project, stack_id, graph


class _Harness:
    def __init__(self, qapp, project, with_panel=True):
        self.holder = {"project": project}
        self.cmd_stack = ProjectCommandStack()
        self.engine = Mock()
        self.engine.layers = []
        self.engine.active_layer_index = 0
        # B16.5 flag 默认关（Mock 属性自动为真值会误开 flag 路由）
        self.engine.color_graph_render_enabled = False
        self.renders = []
        self.host = ColorGraphAppHost(
            get_project=lambda: self.holder["project"],
            set_project=self._set_project,
            project_stack=self.cmd_stack,
            engine_provider=lambda: self.engine,
            request_render=lambda: self.renders.append(1),
            panels_provider=lambda: {},
            set_hydration_guard=self._guard,
        )
        self.panel = None
        if with_panel:
            from src.features.color.ui.node_editor_panel import ColorNodePanel
            self.panel = ColorNodePanel()
            self.host.attach_panel(self.panel)
        self.guards = []

    def _set_project(self, project):
        # main_window set_project 语义：只替换工程引用（引擎同步由
        # AppHost 的 set_project 包装回调驱动，不在此重入）
        self.holder["project"] = project

    def _guard(self, on):
        self.guards.append(bool(on))


@pytest.fixture
def harness(qapp):
    project, stack_id, graph = _project_with_graph_stack()
    h = _Harness(qapp, project)
    return h, stack_id, graph


# ═══ clip 水合 ═══════════════════════════════════════════════════════════

class TestClipHydration:
    def test_clip_load_binds_and_hydrates(self, harness):
        h, stack_id, graph = harness
        h.host.on_clip_stack_loaded(stack_id, scope_owner_id=stack_id)
        assert h.host.bridge._stack_id() == stack_id
        assert h.host._coordinator.state == GraphSessionState.READY
        assert h.host._coordinator.last_ack.graph_revision == \
            graph.graph_revision
        # 真实面板装载投影（非 UI-only 草图）
        assert len(h.panel.snapshot().nodes) == len(graph.nodes)

    def test_graphless_stack_hydrates_migration_view(self, harness):
        h, _stack_id, _graph = harness
        project, fresh_id = EditProject.create(
            name="fresh").create_color_stack(asset_id="a2", name="f")
        stack = project.color_stacks[fresh_id].with_layers(
            [l.to_dict(None) for l in f03_layer_stack()])
        project = project.update_color_stack(fresh_id, layers=stack.layers)
        h.holder["project"] = project
        h.host.on_clip_stack_loaded(fresh_id)
        assert h.host._coordinator.state == GraphSessionState.READY
        # 迁移视图（未建图）：工程仍无 color_graph
        assert project.color_stacks[fresh_id].color_graph is None
        assert h.host._stack_graph(fresh_id) is None

    def test_first_edit_creates_graph_via_command(self, harness):
        """无图栈首次编辑：ApplyColorGraphCommand 自动迁移建图。"""
        h, _sid, _g = harness
        project, fresh_id = EditProject.create(
            name="fresh2").create_color_stack(asset_id="a3", name="f2")
        h.holder["project"] = project
        h.host.on_clip_stack_loaded(fresh_id)
        h.host.begin_grade_gesture("g1")
        node_id = h.host.active_grade_node_id()  # 无选择 → 空
        assert node_id == ""
        # 直接经 service 链路建图（面板 edit intent 的服务端同一命令）
        ok = h.cmd_stack.execute(
            __import__(
                "src.features.edit.core.commands.color_commands",
                fromlist=["ApplyColorGraphCommand"],
            ).ApplyColorGraphCommand.create(
                stack_id=fresh_id, operation="insert_corrector",
                arguments={}),
            h.holder["project"])
        assert ok.color_stacks[fresh_id].color_graph is not None


# ═══ 提交后引擎同步 ═══════════════════════════════════════════════════════

class TestPostCommitSync:
    def test_graph_commit_reprojects_engine(self, harness):
        h, stack_id, graph = harness
        h.host.on_clip_stack_loaded(stack_id)
        corrector = next(n for n in graph.nodes
                         if n.node_type == "corrector")
        # 经面板宿主编辑链路提交一个 grade 命令
        from src.features.edit.core.commands.color_commands import (
            ApplyColorGraphCommand,
        )
        before = h.holder["project"]
        new_project = h.cmd_stack.execute(
            ApplyColorGraphCommand.create(
                stack_id=stack_id, operation="set_node_option",
                arguments={"node_id": corrector.node_id,
                           "name": "grade_patch",
                           "value": ("exposure", 21.0)}),
            before)
        # 走宿主发布链（工程替换 + 引擎同步 + 渲染）
        h.host._on_project_published(new_project)
        # 引擎重投影：layers 来自图投影（grade 合并进 grading_params）
        assert h.engine.layers and len(h.engine.layers) == 3
        layer0 = h.engine.layers[0]
        assert layer0.grading_params["exposure"] == 21.0
        # S04 图登记同步（图 stash 更新 → scope 身份递增）
        h.engine.set_active_color_graph.assert_called()
        assert h.renders, "提交后必须重渲染"
        assert h.guards[-1] is False  # 守卫成对释放

    def test_undo_redo_sync_engine(self, harness):
        h, stack_id, graph = harness
        h.host.on_clip_stack_loaded(stack_id)
        corrector = next(n for n in graph.nodes
                         if n.node_type == "corrector")
        from src.features.edit.core.commands.color_commands import (
            ApplyColorGraphCommand,
        )
        h.host._on_project_published(h.cmd_stack.execute(
            ApplyColorGraphCommand.create(
                stack_id=stack_id, operation="set_node_option",
                arguments={"node_id": corrector.node_id,
                           "name": "grade_patch",
                           "value": ("exposure", 21.0)}),
            h.holder["project"]))
        assert h.engine.layers[0].grading_params["exposure"] == 21.0
        assert h.host.undo()
        assert h.engine.layers[0].grading_params["exposure"] != 21.0
        assert h.host.redo()
        assert h.engine.layers[0].grading_params["exposure"] == 21.0


# ═══ E09 图编辑上下文 ════════════════════════════════════════════════════

class _GradingHarness:
    """LayerOpsMixin 最小宿主（on_grading_changed 路由测试）。"""

    def __init__(self, app_host, engine):
        from src.app.controllers.layer_controller import LayerOpsMixin
        host = LayerOpsMixin.__new__(LayerOpsMixin)
        host.engine = engine
        host.color_graph_host = app_host
        host._graph_gesture_counter = 0
        self.mixin = host
        self.grade_calls = []

    def run(self, *args):
        engine = self.mixin.engine
        engine.get_current_image = lambda: object()
        engine.grade_image = lambda *a: self.grade_calls.append(a)
        engine.is_video_playing = lambda: False
        engine.current_grade_frame = None  # 关键帧路径短路（Mock 不可迭代）
        self.mixin._is_video_playing = lambda: False
        self.mixin._invalidate_video_render_params_if_playing = \
            lambda: None
        self.mixin.request_render = lambda: None
        self.mixin.on_grading_changed(*args)


def _keys():
    from src.app.controllers.layer_controller import LayerOpsMixin
    return LayerOpsMixin._GRADING_SIGNAL_KEYS


def test_projection_hides_owned_uuid_labels_and_preserves_custom_names():
    from dataclasses import replace
    from uuid import uuid4
    from src.app.controllers.color_graph_controller import graph_payload_to_draft
    _project, _stack, graph = _project_with_graph_stack()
    node = next(n for n in graph.nodes if n.node_type == "corrector")
    layer_id = str(node.legacy_payload["id"])
    for label in (node.node_id, layer_id, "自定义节点", str(uuid4())):
        patched = replace(node, label=label)
        candidate = replace(graph, nodes=tuple(patched if n.node_id == node.node_id else n for n in graph.nodes))
        projected = graph_payload_to_draft(candidate.to_dict())
        expected = "" if label in (node.node_id, layer_id) else label
        assert projected.nodes[node.node_id].label == expected


def test_graph_publish_updates_both_preview_snapshots_before_requesting_current_frame():
    from types import SimpleNamespace
    from src.app.main_window import MainWindow
    from unittest.mock import Mock
    project = object()
    events = []
    main = SimpleNamespace(update_color_stacks=lambda value: events.append(("edit", value)))
    color = SimpleNamespace(update_color_stacks=lambda value: events.append(("color", value)),
                            request_render=lambda frame: events.append(("request", frame)))
    host = SimpleNamespace(timeline_preview_renderer=main, _color_timeline_preview_renderer=color,
                           current_page="Color", _invalidate_video_render_params_if_playing=Mock(),
                           _color_timeline_playback_controller=lambda: SimpleNamespace(current_frame=112))
    MainWindow._publish_color_graph_to_previews(host, project)
    assert events == [("edit", project), ("color", project), ("request", 112)]
    host._invalidate_video_render_params_if_playing.assert_called_once_with()
    events.clear()
    host.timeline_preview_renderer = color
    MainWindow._publish_color_graph_to_previews(host, project)
    assert events == [("color", project), ("request", 112)]


def _tuple_for(keys, exposure):
    values = []
    for k in keys:
        if k == "exposure":
            values.append(exposure)
        elif k in ("lift", "gamma", "gain"):
            values.append((0.0, 0.0, 0.0))
        else:
            values.append(0.0)
    return tuple(values)


class TestGradingGestureRouting:
    def _select_first_corrector(self, h, graph):
        # 面板选中第一个 Corrector → 宿主句柄激活
        corrector = next(n for n in graph.nodes
                         if n.node_type == "corrector")
        h.host.panel_host._on_active_node_changed(corrector.node_id)
        return corrector

    def test_graph_context_routes_to_transaction(self, harness):
        h, stack_id, graph = harness
        h.host.on_clip_stack_loaded(stack_id)
        corrector = self._select_first_corrector(h, graph)
        assert h.host.graph_edit_context_active()

        grading = _GradingHarness(h.host, h.engine)
        grading.run(*_tuple_for(_keys(), 15.0))
        # 图事务打开且 grade_patch 已并入
        assert h.host.grade_gesture_open()
        # 预览通道同值（活动层先对齐）
        assert h.engine.active_layer_index == 0
        assert grading.grade_calls, "预览通道必须同值直写"
        # 一次手势一个 command
        depth_before = len(h.cmd_stack._undo_stack)
        assert h.host.end_grade_gesture()
        assert len(h.cmd_stack._undo_stack) == depth_before + 1
        new_graph = h.holder["project"].color_stacks[stack_id].color_graph
        node = next(n for n in new_graph.nodes
                    if n.node_id == corrector.node_id)
        assert node.params["grade"]["exposure"] == 15.0

    def test_no_selection_keeps_legacy_path(self, harness):
        h, stack_id, graph = harness
        h.host.on_clip_stack_loaded(stack_id)
        h.host.panel_host._on_active_node_changed("")
        assert not h.host.graph_edit_context_active()
        grading = _GradingHarness(h.host, h.engine)
        grading.run(*_tuple_for(_keys(), 12.0))
        assert grading.grade_calls
        assert not h.host.grade_gesture_open()
        assert h.cmd_stack._undo_stack == []

    def test_mixer_selection_is_readonly(self, harness):
        h, stack_id, graph = harness
        h.host.on_clip_stack_loaded(stack_id)
        # 无 mixer 节点——用 Source 选择模拟只读
        h.host.panel_host._on_active_node_changed(graph.source_node_id)
        from src.app.controllers.color_keyframe_recorder import KeyframeAutoRecorder
        h.host.attach_keyframe_recorder(KeyframeAutoRecorder(), frame_provider=lambda: 24)
        assert not h.host.graph_edit_context_active()
        assert h.host.panel_grade_params() is None

    def test_central_recorder_routes_unselected_active_layer_without_render_flag(self, harness):
        from src.app.controllers.color_keyframe_recorder import KeyframeAutoRecorder
        h, stack_id, graph = harness
        h.host.on_clip_stack_loaded(stack_id)
        h.host.panel_host._on_active_node_changed("")
        h.engine.color_graph_render_enabled = False
        h.engine.active_layer_index = 0
        from src.features.color.core.layer import Layer
        from src.features.color.core.color_graph_param_source import node_layer_payload
        h.engine.layers = [Layer.from_dict(node_layer_payload(node)) for node in graph.nodes
                           if node.node_type == "corrector"]
        h.engine.get_active_layer.return_value = h.engine.layers[0]
        recorder = KeyframeAutoRecorder()
        h.host.attach_keyframe_recorder(recorder, frame_provider=lambda: 24)
        node_id = next(n.node_id for n in graph.nodes if n.node_type == "corrector")
        recorder.set_auto(node_id, True)
        assert h.host.graph_edit_context_active()
        assert h.host.active_grade_node_id() == node_id
        grading = _GradingHarness(h.host, h.engine)
        current = h.host.panel_grade_params()
        grading.run(*(12.0 if key == "exposure" else current.get(key, 0.0) for key in _keys()))
        assert h.host.end_grade_gesture()
        node = next(n for n in h.host.bound_graph().nodes if n.node_id == node_id)
        assert set(node.params["grade_keyframes"]) == {"exposure"}
        keys = node.params["grade_keyframes"]["exposure"]["keyframes"]
        assert len(keys) == 1 and keys[0]["frame"] == 24 and keys[0]["value"] == 12.0
        assert len(h.cmd_stack._undo_stack) == 1


def _keys():
    from src.app.controllers.layer_controller import LayerOpsMixin
    return LayerOpsMixin._GRADING_SIGNAL_KEYS


# ═══ scope owner 真实 ID（§6.4）══════════════════════════════════════════

class TestGroupScopeOwner:
    def _harness_with_group(self, qapp):
        project, clip_stack, _graph = _project_with_graph_stack()
        project, pre_stack = project.create_color_stack(
            asset_id="g", name="pre")
        project, post_stack = project.create_color_stack(
            asset_id="g", name="post")
        group = ColorGroupData(
            group_id="grp-1", name="Scene 1",
            members=(ColorGroupMember(timeline_id="tl-1",
                                      clip_id="clip-1"),),
            pre_group_stack_id=pre_stack,
            post_group_stack_id=post_stack)
        project = project.with_color_groups({group.group_id: group})
        h = _Harness(qapp, project)
        h.host._color_context_provider = lambda: ("tl-1", "clip-1")
        return h, clip_stack, pre_stack, post_stack, group

    def test_group_availability_follows_membership(self, qapp):
        h, clip_stack, _pre, _post, _group = self._harness_with_group(qapp)
        calls = []
        h.panel.set_group_available = lambda v: calls.append(v)
        h.host.on_clip_stack_loaded(clip_stack)
        assert calls == [True]

    def test_group_scope_rebinds_real_stack_and_owner(self, qapp):
        h, clip_stack, pre_stack, _post, group = self._harness_with_group(
            qapp)
        h.host.on_clip_stack_loaded(clip_stack)
        h.host._on_scope_menu("group_pre_clip")
        assert h.host.bridge._stack_id() == pre_stack
        assert h.host._coordinator.scope == "group_pre_clip"
        assert h.host._coordinator.scope_owner_id == "grp-1:pre"
        assert h.host._coordinator.state == GraphSessionState.READY
        # 回 clip scope（菜单路径）：重绑 clip 栈
        h.host._on_scope_menu("clip")
        assert h.host.bridge._stack_id() == clip_stack
        assert h.host._coordinator.scope == "clip"

    def test_no_group_keeps_menu_disabled(self, harness):
        h, stack_id, _graph = harness
        calls = []
        h.panel.set_group_available = lambda v: calls.append(v)
        h.host.on_clip_stack_loaded(stack_id)
        assert calls == [False]
        # scope 菜单动作无效：绑定不变
        h.host._on_scope_menu("group_pre_clip")
        assert h.host.bridge._stack_id() == stack_id

    def test_timeline_scope_binds_registered_or_readonly(self, qapp):
        h, clip_stack, _p, _po, _g = self._harness_with_group(qapp)
        h.host.on_clip_stack_loaded(clip_stack)
        # N07：timeline scope 懒建中性栈（真实 owner 栈模型）——绑定后
        # 编辑落 timeline 栈；切回 clip 重绑不串写
        h.host._on_scope_menu("timeline")
        assert h.host._coordinator.scope_owner_id == "tl-1"
        tl_stack_id = h.host._bound_stack_id
        assert tl_stack_id
        project = h.holder["project"]
        assert project.timeline_grade_stack_id("tl-1") == tl_stack_id
        assert project.color_stacks[tl_stack_id].scope == "timeline"
        assert h.host.bridge._stack_id() == tl_stack_id
        # 幂等：再次切换复用同栈
        h.host._on_scope_menu("clip")
        assert h.host.bridge._stack_id() == clip_stack
        h.host._on_scope_menu("timeline")
        assert h.host._bound_stack_id == tl_stack_id
        # 无 timeline id 上下文 → 保持只读解绑（fail-visible）


# ═══ 活动层对齐（§6.5 source_layer_id 唯一许可路径）══════════════════════

class TestActiveLayerAlignment:
    def test_align_by_source_layer_id(self, harness):
        h, stack_id, graph = harness
        h.host.on_clip_stack_loaded(stack_id)
        from src.features.color.core.layer import Layer
        from src.features.color.core.color_graph_migration import (
            project_legacy_layers,
        )
        h.engine.layers = [Layer.from_dict(dict(p))
                           for p in project_legacy_layers(graph)]
        correctors = [n for n in graph.nodes if n.node_type == "corrector"]
        second = correctors[1]
        h.host.panel_host._on_active_node_changed(second.node_id)
        assert h.host.align_engine_active_layer(second.node_id)
        assert h.engine.active_layer_index == 1

    def test_align_unknown_node_returns_false(self, harness):
        h, stack_id, _graph = harness
        h.host.on_clip_stack_loaded(stack_id)
        assert not h.host.align_engine_active_layer("no-such-node")

    def test_window_combo_new_node_aligns_and_notifies_after_commit(self, harness):
        h, stack_id, graph = harness
        h.host.on_clip_stack_loaded(stack_id)
        events = []
        h.host.panel_host.on_window_combo(
            lambda combo, node_id: events.append((combo, node_id)))
        view = h.panel.view
        view.apply_edit("add_window_combo",
                        lambda: view.state.add_window_combo("pcw"),
                        payload={"combo": "pcw"})
        assert len(events) == 1
        assert events[0][0] == "pcw"
        new_id = events[0][1]
        assert new_id not in {n.node_id for n in graph.nodes}
        assert h.host.align_engine_active_layer(new_id)
        assert h.engine.layers[h.engine.active_layer_index].id == new_id
        assert len(h.engine.layers[h.engine.active_layer_index].mask_shapes) == 1
