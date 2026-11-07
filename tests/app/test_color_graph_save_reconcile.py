"""B06 保存边界图一致化（reconcile_projectable_graph）回归。

背景缺陷（本轮复查发现）：legacy 写通道（wheel 手势收束提交）只写
``stack.layers``，``stack.color_graph`` 不动；重载/切回时 E07 图优先，
图链接栈上的 legacy 编辑被静默丢弃。验收：

- 可投影图 + layers 提交 → 图随层快照重导出（参数/顺序/增删进图），
  布局（ui_state）保留，revision 只在真变化时 +1；
- layers 与图投影一致（无变化）→ 图不动、revision 不变；
- 复杂（不可投影）图 → 保持权威不同步（不压平丢结构）；
- UpdateColorStackCommand.apply/undo 对称：undo 恢复一致化前图快照；
- flag 模式（TOPOS_COLOR_GRAPH_RENDER）下图链接栈的 wheel 路由全走图
  （canvas/scope 已由图渲染，两通道不分叉）。
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

pytest.importorskip("PyQt6")

from src.features.color.core.color_graph_migration import (
    migrate_legacy_stack,
    project_legacy_layers,
    reconcile_projectable_graph,
)
from src.features.edit.core.commands import ProjectCommandStack
from src.features.edit.core.commands.color_commands import (
    UpdateColorStackCommand,
)
from src.features.edit.core.project import EditProject
from tests.color.fixtures.color_graph import f03_layer_stack
from tests.color.test_color_graph_migration import _stack_with_layers


def _graph_project():
    stack = _stack_with_layers(f03_layer_stack())
    graph = migrate_legacy_stack(stack)
    project, stack_id = EditProject.create(name="r").create_color_stack(
        asset_id="a1", name="s")
    project = project.update_color_stack(stack_id, color_graph=graph)
    return project, stack_id, graph


class TestReconcileProjectableGraph:
    def test_no_change_keeps_graph_and_revision(self):
        _p, _s, graph = _graph_project()
        projection = project_legacy_layers(graph)
        new, changed = reconcile_projectable_graph(graph, projection)
        assert not changed and new is graph

    def test_layer_edit_flows_into_graph(self):
        _p, _s, graph = _graph_project()
        layers = [dict(p) for p in project_legacy_layers(graph)]
        layers[0]["grading_params"]["exposure"] = 0.75
        new, changed = reconcile_projectable_graph(graph, layers)
        assert changed
        assert new is not graph
        # 编辑进入图投影（重载后 E07 以图重建引擎 → 编辑保留）
        assert project_legacy_layers(new)[0]["grading_params"][
            "exposure"] == 0.75
        assert new.graph_revision == graph.graph_revision + 1

    def test_layout_preserved(self):
        _p, _s, graph = _graph_project()
        from dataclasses import replace as _replace
        from src.shared.project.color_graph import ColorGraphUiState
        positioned = _replace(
            graph, ui_state=ColorGraphUiState(
                positions={"some-node": (120.0, 80.0)}, zoom=1.5))
        layers = [dict(p) for p in project_legacy_layers(graph)]
        layers.append(dict(layers[0], id="extra-1", name="Extra"))
        new, changed = reconcile_projectable_graph(positioned, layers)
        assert changed
        assert new.ui_state.positions == {"some-node": (120.0, 80.0)}
        assert new.ui_state.zoom == 1.5

    def test_layer_add_and_remove_flow_into_graph(self):
        _p, _s, graph = _graph_project()
        layers = [dict(p) for p in project_legacy_layers(graph)]
        removed = layers[:-1]                      # 删最后一层
        new, changed = reconcile_projectable_graph(graph, removed)
        assert changed
        assert len(project_legacy_layers(new)) == len(removed)
        added = [dict(layers[0], id="brand-new", name="New")]
        new2, changed2 = reconcile_projectable_graph(graph, added)
        assert changed2
        assert len(project_legacy_layers(new2)) == 1

    def test_graph_created_corrector_keeps_identity_and_other_domains_on_legacy_save(self):
        from src.features.color.core.color_graph_service import ColorGraphService
        from dataclasses import replace
        _project, _stack, graph = _graph_project()
        result = ColorGraphService().apply_intent(graph, "insert_corrector", {})
        assert result.ok
        graph = result.graph
        created = next(n for n in graph.nodes if n.node_type == "corrector" and not n.legacy_payload)
        keyed = ColorGraphService().apply_intent(graph, "set_node_keyframe", {
            "node_id": created.node_id, "param": "exposure", "frame": 24, "value": 12.0})
        assert keyed.ok
        graph = keyed.graph
        created = next(n for n in graph.nodes if n.node_id == created.node_id)
        decorated = replace(created, params={**created.params, "cache_mode": "on", "future": {"keep": 1}})
        graph = replace(graph, nodes=tuple(decorated if n.node_id == created.node_id else n for n in graph.nodes))
        layers = project_legacy_layers(graph)
        target = next(layer for layer in layers if layer["id"] == created.node_id)
        target["grading_params"]["temp"] = 20.0
        updated, changed = reconcile_projectable_graph(graph, layers)
        assert changed
        assert [n.node_id for n in updated.nodes] == [n.node_id for n in graph.nodes]
        assert updated.edges == graph.edges
        node = next(n for n in updated.nodes if n.node_id == created.node_id)
        assert node.params["grade_keyframes"] == created.params["grade_keyframes"]
        assert node.params["cache_mode"] == "on" and node.params["future"]["keep"] == 1
        assert next(p for p in project_legacy_layers(updated) if p["id"] == created.node_id)["grading_params"]["temp"] == 20.0

    def test_non_projectable_graph_kept_authoritative(self):
        from src.features.color.core.color_graph_service import ColorGraphService
        _p, _s, graph = _graph_project()
        counter = [0]

        def _ids():
            counter[0] += 1
            return f"n{counter[0]}"
        branched = ColorGraphService(id_factory=_ids).op_add_layer_branch(
            graph).graph
        with pytest.raises(Exception):
            project_legacy_layers(branched)  # 复杂图不可投影（断言前提）
        new, changed = reconcile_projectable_graph(
            branched, [dict(p) for p in
                       project_legacy_layers(graph)])
        assert not changed and new is branched, "复杂图必须保持权威不同步"

    def test_bad_layers_snapshot_leaves_graph(self):
        _p, _s, graph = _graph_project()
        bad = [{"id": "x", "grading_params": {None: object()}}]
        new, changed = reconcile_projectable_graph(graph, bad)
        assert not changed and new is graph, (
            "不可迁移/校验失败的快照不得触碰权威图")


class TestCommandReconciliation:
    def _commit(self, project, stack_id, layers):
        cmd = UpdateColorStackCommand.create(
            stack_id=stack_id, grading_params=None, layers=layers,
            active_layer_index=0)
        stack = ProjectCommandStack()
        return stack, cmd, stack.execute(cmd, project)

    def test_legacy_wheel_edit_survives_reload(self):
        """缺陷回归：legacy 编辑提交后，图携带同值（重载不丢）。"""
        project, stack_id, graph = _graph_project()
        layers = [dict(p) for p in project_legacy_layers(graph)]
        layers[1]["grading_params"]["contrast"] = -0.35
        stack, cmd, new_project = self._commit(project, stack_id, layers)
        new_graph = new_project.color_stacks[stack_id].color_graph
        assert new_graph is not graph
        assert project_legacy_layers(new_graph)[1]["grading_params"][
            "contrast"] == -0.35
        # E07 语义：以图重建引擎 → 编辑在
        from src.features.color.adapters.color_stack_hydrator import (
            ColorStackHydrator,
        )
        engine = Mock()
        engine.layers = []
        ColorStackHydrator().apply_stack_to_engine(
            new_project.color_stacks[stack_id], engine)
        assert engine.layers[1].grading_params["contrast"] == -0.35

    def test_no_change_commit_leaves_graph(self):
        project, stack_id, graph = _graph_project()
        layers = [dict(p) for p in project_legacy_layers(graph)]
        _stack, _cmd, new_project = self._commit(project, stack_id, layers)
        assert new_project.color_stacks[stack_id].color_graph is graph

    def test_undo_restores_pre_reconcile_graph(self):
        project, stack_id, graph = _graph_project()
        layers = [dict(p) for p in project_legacy_layers(graph)]
        layers[0]["grading_params"]["exposure"] = 0.9
        stack, cmd, new_project = self._commit(project, stack_id, layers)
        undone = stack.undo(new_project)
        restored = undone.color_stacks[stack_id].color_graph
        assert restored.to_dict() == graph.to_dict()

    def test_graphless_stack_unaffected(self):
        project, stack_id = EditProject.create(
            name="g0").create_color_stack(asset_id="a", name="s")
        layers = [l.to_dict(None) for l in f03_layer_stack()]
        _stack, _cmd, new_project = self._commit(project, stack_id, layers)
        assert new_project.color_stacks[stack_id].color_graph is None


class TestGestureCommitDoesNotFlattenLayers:
    def test_commit_updates_active_layer_only(self, qapp_module=None):
        """缺陷回归（per-layer 数据损坏）：手势提交尾段曾把扁平参数铺到
        **所有**引擎层——非活动层独立调色被冲掉，下一次手势快照即永久
        压平。修正后只回写活动层。"""
        from src.app.controllers.edit_controller import EditControllerMixin
        from src.features.edit.core.commands import ProjectCommandStack

        ctrl = EditControllerMixin.__new__(EditControllerMixin)
        project, stack_id = EditProject.create(
            name="flat").create_color_stack(asset_id="a", name="s")
        ctrl._current_edit_project = project
        ctrl._color_stack_hydration_in_progress = False
        engine = Mock()
        layers = f03_layer_stack()
        engine.layers = layers
        engine.active_layer_index = 0
        engine.get_video_stabilization_state = lambda: None
        engine.current_raw_params = {}
        engine.tracking_store = None
        engine.video_denoise_params = None
        engine.render_pipeline = Mock()
        ctrl.engine = engine
        ctrl.media_pool_panel = Mock()
        ctrl.media_pool_panel.get_command_stack.return_value = \
            ProjectCommandStack()
        ctrl.timeline_preview_renderer = None
        ctrl._schedule_idle_color_timeline_clip_strip_refresh = Mock()
        ctrl._schedule_color_timeline_clip_preview_sync = Mock()
        ctrl._notify_composite_inputs_stale = Mock()
        ctrl.color_timeline_clip_strip = None
        ctrl._color_timeline_clip_playback_context = None

        before = [dict(l.grading_params) for l in layers]
        ctrl._on_grading_changed_for_color_stack(
            stack_id, {"exposure": 5.0})

        assert layers[0].grading_params["exposure"] == 5.0
        for i in (1, 2):
            assert layers[i].grading_params == before[i], (
                f"非活动层 {i} 的独立调色不得被手势提交冲掉")


class TestRefreshBoundGraph:
    def test_external_commit_refreshes_engine_stash_and_projection(self):
        """缺陷回归（scope/canvas 分叉）：legacy 通道提交一致化图后，
        图宿主必须刷新引擎图 stash 与面板投影。"""
        from src.app.controllers.color_graph_controller import (
            ColorGraphAppHost,
        )
        project, stack_id, graph = _graph_project()
        holder = {"project": project}
        engine = Mock()
        engine.layers = []
        host = ColorGraphAppHost(
            get_project=lambda: holder["project"],
            set_project=lambda p: holder.__setitem__("project", p),
            project_stack=ProjectCommandStack(),
            engine_provider=lambda: engine,
            request_render=lambda: None, panels_provider=lambda: {})
        host.on_clip_stack_loaded(stack_id)
        engine.set_active_color_graph.reset_mock()
        # 外部一致化（wheel 编辑进图）
        layers = [dict(p) for p in project_legacy_layers(graph)]
        layers[0]["grading_params"]["exposure"] = 0.66
        cmd = UpdateColorStackCommand.create(
            stack_id=stack_id, layers=layers, active_layer_index=0)
        holder["project"] = ProjectCommandStack().execute(
            cmd, holder["project"])
        host.refresh_bound_graph()
        engine.set_active_color_graph.assert_called_once()
        called_graph = engine.set_active_color_graph.call_args.args[0]
        assert called_graph.graph_revision == graph.graph_revision + 1
        # 面板投影同步到新 revision（节点面板不再显示旧图）
        assert host._coordinator.last_ack.graph_revision == \
            called_graph.graph_revision

    def test_refresh_without_revision_change_skips_rehydrate(self):
        from src.app.controllers.color_graph_controller import (
            ColorGraphAppHost,
        )
        project, stack_id, graph = _graph_project()
        holder = {"project": project}
        engine = Mock()
        engine.layers = []
        host = ColorGraphAppHost(
            get_project=lambda: holder["project"],
            set_project=lambda p: holder.__setitem__("project", p),
            project_stack=ProjectCommandStack(),
            engine_provider=lambda: engine,
            request_render=lambda: None, panels_provider=lambda: {})
        host.on_clip_stack_loaded(stack_id)
        ack_before = host._coordinator.last_ack
        engine.set_active_color_graph.reset_mock()
        host.refresh_bound_graph()   # 图未变
        engine.set_active_color_graph.assert_called_once()  # stash 幂等刷新
        assert host._coordinator.last_ack is ack_before, (
            "revision 未变不应重水合面板（防闪烁）")


class TestFlagModeWheelRouting:
    def _host(self, engine_graph_flag):
        from src.app.controllers.color_graph_controller import (
            ColorGraphAppHost,
        )
        project, stack_id, graph = _graph_project()
        holder = {"project": project}
        engine = Mock()
        engine.layers = []
        engine.color_graph_render_enabled = engine_graph_flag
        # 引擎活动层 → 图节点（source_layer_id 反查路径）
        first_layer = Mock()
        first_layer.id = dict(project_legacy_layers(graph)[0])["id"]
        engine.get_active_layer = lambda: first_layer
        host = ColorGraphAppHost(
            get_project=lambda: holder["project"],
            set_project=lambda p: holder.__setitem__("project", p),
            project_stack=ProjectCommandStack(),
            engine_provider=lambda: engine,
            request_render=lambda: None, panels_provider=lambda: {})
        host.on_clip_stack_loaded(stack_id)
        return host, graph, engine

    def test_flag_off_no_selection_keeps_legacy(self):
        host, graph, _engine = self._host(False)
        assert not host.graph_edit_context_active()
        assert host.active_grade_node_id() == ""

    def test_flag_on_routes_without_selection(self):
        host, graph, _engine = self._host(True)
        assert host.graph_edit_context_active()
        node_id = host.active_grade_node_id()
        assert node_id, "引擎活动层必须能反查图节点（source_layer_id）"
        payload = dict(next(
            n for n in graph.nodes if n.node_id == node_id
        ).legacy_payload)
        assert payload["id"] == dict(
            project_legacy_layers(graph)[0])["id"]

    def test_unmappable_node_falls_back_to_legacy(self):
        """缺陷回归（静默丢手势）：flag 模式但活动层无法映射图节点时，
        on_grading_changed 必须回落 legacy 路径（不允许什么都不做）。"""
        from src.app.controllers.layer_controller import LayerOpsMixin
        host, graph, engine = self._host(True)
        # 活动层换成图里不存在的层 id（复杂图 stale 层情形）
        stale = Mock()
        stale.id = "stale-layer-id"
        engine.get_active_layer = lambda: stale
        assert host.graph_edit_context_active()
        assert host.active_grade_node_id() == ""

        mixin = LayerOpsMixin.__new__(LayerOpsMixin)
        mixin.engine = engine
        mixin.color_graph_host = host
        mixin._graph_gesture_counter = 0
        grade_calls = []
        engine.get_current_image = lambda: object()
        engine.grade_image = lambda *a: grade_calls.append(a)
        engine.current_grade_frame = None
        mixin._is_video_playing = lambda: False
        mixin._invalidate_video_render_params_if_playing = lambda: None
        mixin.request_render = lambda: None
        keys = LayerOpsMixin._GRADING_SIGNAL_KEYS
        args = tuple(3.0 if k == "exposure" else (
            (0.0, 0.0, 0.0) if k in ("lift", "gamma", "gain") else 0.0)
            for k in keys)
        mixin.on_grading_changed(*args)
        assert grade_calls, "无法映射节点时必须回落 legacy（不静默丢手势）"
        assert not host.grade_gesture_open()
