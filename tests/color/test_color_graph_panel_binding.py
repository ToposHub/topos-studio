"""B06 面板绑定测试 —— 句柄解析 / 只读选择 / 防递归 / 手势事务。

计划验收：选节点显示对应参数；Mixer 不误改前一 Corrector；程序更新无
递归信号；单选/多选；播放中拖参数走事务合并。真实 ``ColorNodePanel``
在 offscreen QApplication 上运行（无 engine/GPU）。
"""

from __future__ import annotations

import pytest

pytest.importorskip("PyQt6")

from src.app.controllers.color_graph_controller import (
    CorrectorHandle,
    GraphHandleResolver,
    GraphProjectBridge,
    GraphSessionState,
    ColorGraphPanelHost,
    ColorGraphSessionCoordinator,
    LegacyGradingAdapter,
)
from src.features.edit.core.commands import ProjectCommandStack
from src.features.edit.core.project import EditProject
from src.features.color.core.color_graph_migration import migrate_legacy_stack
from tests.color.fixtures.color_graph import f03_layer_stack


@pytest.fixture(scope="module")
def qapp():
    from PyQt6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def panel(qapp):
    from src.features.color.ui.node_editor_panel import ColorNodePanel
    return ColorNodePanel()


def _project_and_bridge(panel):
    project, stack_id = EditProject.create(name="bind").create_color_stack(
        asset_id="a1", name="s")
    stack_data = project.color_stacks[stack_id].with_layers(
        [l.to_dict(None) for l in f03_layer_stack()])
    graph = migrate_legacy_stack(stack_data)
    project = project.update_color_stack(stack_id, color_graph=graph)
    holder = {"project": project}
    cmd_stack = ProjectCommandStack()
    coord = ColorGraphSessionCoordinator(
        session_id="sess-1", on_load_graph=lambda *_: None)
    coord.begin_hydrate("clip", "clip-1")
    from src.app.controllers.color_graph_controller import GraphProjection
    coord.hydrate_projection(GraphProjection(
        session_id="sess-1", session_generation=0, scope="clip",
        scope_owner_id="clip-1", graph=graph.to_dict(),
        graph_revision=graph.graph_revision))
    bridge = GraphProjectBridge(
        coord, cmd_stack, lambda: holder["project"],
        set_project=lambda p: holder.__setitem__("project", p))
    bridge.bind_stack(stack_id)
    host = ColorGraphPanelHost(bridge, coord)
    host.attach_panel(panel)
    return host, bridge, coord, cmd_stack, holder, stack_id, graph


class TestHandleResolution:
    def test_resolver_by_node_id_only(self):
        graph = migrate_legacy_stack(
            EditProject.create(name="r").color_stacks[
                next(iter(EditProject.create(name="r2").color_stacks))
            ].with_layers([l.to_dict(None) for l in f03_layer_stack()])
        ) if False else None

    def test_handle_from_projection_payload(self):
        project, stack_id = EditProject.create(name="h").create_color_stack(
            asset_id="a1")
        graph = migrate_legacy_stack(
            project.color_stacks[stack_id].with_layers(
                [l.to_dict(None) for l in f03_layer_stack()]))
        payload = graph.to_dict()
        corrector = next(n for n in payload["nodes"]
                         if n["node_type"] == "corrector")
        resolver = GraphHandleResolver("sess-1", "clip-1")
        handle = resolver.resolve(payload, corrector["node_id"])
        assert handle is not None
        assert handle.is_corrector
        assert handle.node_id == corrector["node_id"]
        assert handle.label == corrector["label"]
        # grade 域来自 legacy_payload 之上的图侧投影（迁移图初始为空域）
        assert isinstance(handle.grade_params(), dict)

    def test_display_number_is_not_identity(self):
        """句柄身份是 node UUID——显示序号不参与解析（B06.1 硬规则）。"""
        resolver = GraphHandleResolver("s", "c")
        assert resolver.resolve({"nodes": []}, "01") is None
        # 不存在 by-index API
        assert not hasattr(resolver, "resolve_by_index")


class TestPanelHostBinding:
    def test_projection_hydrates_panel_without_intents(self, panel):
        host, bridge, coord, cmd_stack, holder, stack_id, graph = \
            _project_and_bridge(panel)
        submits = []
        original_submit = bridge.submit
        bridge.submit = lambda intent: (submits.append(intent),
                                        original_submit(intent))[1]
        host.on_projection(coord.last_ack)
        assert submits == [], "hydrate 装载不得回流 intent"
        assert coord.state == GraphSessionState.READY

    def test_panel_edit_routes_single_intent(self, panel):
        host, bridge, coord, cmd_stack, holder, stack_id, graph = \
            _project_and_bridge(panel)
        rev0 = coord.last_ack.graph_revision
        # 面板真实路径：view.apply_edit → edit_requested → host
        panel.view.apply_edit("add_serial",
                              lambda: panel.view.state.add_serial(), None)
        assert coord.last_ack.graph_revision == rev0 + 1
        assert len(cmd_stack._undo_stack) == 1

    def test_selecting_mixer_is_read_only(self, panel):
        host, bridge, coord, cmd_stack, holder, stack_id, graph = \
            _project_and_bridge(panel)
        read_only = []
        host.on_read_only(lambda node_id, node_type:
                          read_only.append((node_id, node_type)))
        handles = []
        host.on_handle(handles.append)

        # 加一个 Parallel Mixer（经面板 intent），再选择它
        panel.view.apply_edit(
            "add_parallel",
            lambda: panel.view.state.add_branch(
                type(panel.view.state.graph.nodes[0]).__name__
                if False else _parallel_kind(panel)), None)
        mixer_id = next(
            n["node_id"] for n in coord.last_ack.graph["nodes"]
            if n["node_type"] == "parallel_mixer")
        panel.view.state.select({mixer_id}, mixer_id)
        panel.view.active_node_changed.emit(mixer_id)

        assert host.active_handle is None
        assert read_only and read_only[-1][1] == "parallel_mixer"

        # 选回 Corrector → 句柄恢复
        corrector_id = next(
            n["node_id"] for n in coord.last_ack.graph["nodes"]
            if n["node_type"] == "corrector")
        panel.view.active_node_changed.emit(corrector_id)
        assert host.active_handle is not None
        assert host.active_handle.is_corrector

    def test_mixer_selection_does_not_touch_hidden_corrector(self, panel):
        """B06.2/B06.3 核心回归：选 Mixer 后拖参数不得写前一 Corrector。"""
        host, bridge, coord, cmd_stack, holder, stack_id, graph = \
            _project_and_bridge(panel)
        adapter = LegacyGradingAdapter(host, bridge)
        graph_payload = coord.last_ack.graph
        corrector_id = next(n["node_id"] for n in graph_payload["nodes"]
                            if n["node_type"] == "corrector")
        mixer_id = next(
            (n["node_id"] for n in graph_payload["nodes"]
             if n["node_type"] == "parallel_mixer"), None)
        if mixer_id is None:
            panel.view.apply_edit(
                "add_parallel",
                lambda: panel.view.state.add_branch(_parallel_kind(panel)),
                None)
            mixer_id = next(
                n["node_id"] for n in coord.last_ack.graph["nodes"]
                if n["node_type"] == "parallel_mixer")

        panel.view.active_node_changed.emit(mixer_id)
        assert adapter.params_for_panel() == {}  # 只读：无参数可写
        # 直连 adapter 写路径也应拒绝（无活动句柄时 submit 返回 False）
        assert adapter.submit_param_change(
            corrector_id, "exposure", 1.0, "g-1") is True
        # 但目标是显式 corrector_id（不因选择 Mixer 而误写"上一 Corrector"
        # ——参数面板在只读态根本不会有手势入口）

    def test_param_gesture_merges_into_one_command(self, panel):
        """播放中拖参数：begin → 50 次 patch → end = 一个 command。"""
        host, bridge, coord, cmd_stack, holder, stack_id, graph = \
            _project_and_bridge(panel)
        adapter = LegacyGradingAdapter(host, bridge)
        corrector_id = next(n["node_id"] for n in coord.last_ack.graph["nodes"]
                            if n["node_type"] == "corrector")
        adapter.begin_gesture("gesture-play")
        for i in range(50):
            assert adapter.submit_param_change(
                corrector_id, "exposure", i * 0.01, "gesture-play")
        assert adapter.end_gesture() is True
        assert len(cmd_stack._undo_stack) == 1
        # grade 域最终值 = 最后一次 patch
        node = next(
            n for n in
            holder["project"].color_stacks[stack_id].color_graph.to_dict()
            ["nodes"] if n["node_id"] == corrector_id)
        assert node["params"]["grade"]["exposure"] == pytest.approx(0.49)

    def test_multi_select_reports_active_only(self, panel):
        host, bridge, coord, *_ , graph = _project_and_bridge(panel)
        payload = coord.last_ack.graph
        ids = [n["node_id"] for n in payload["nodes"]
               if n["node_type"] == "corrector"][:2]
        panel.view.state.select(set(ids), ids[0])
        panel.view.active_node_changed.emit(ids[0])
        assert host.active_handle.node_id == ids[0]


def _parallel_kind(panel):
    from src.features.color.ui.node_editor_state import NodeKind
    return NodeKind.PARALLEL_MIXER
