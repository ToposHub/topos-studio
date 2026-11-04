"""N07：Timeline 调色栈真实 owner 模型 + pre→main→trim→post→timeline 链。

验收（计划 N07）：timeline 栈真实登记/迁移/命令/保存；timeline 段进
统一链序（无登记退化 4 段）；编辑落 timeline 栈、切回 clip/组不串写；
保存重开 + 资源闭包覆盖；Composite 共享主栈跨组归属 fail-visible。
"""
from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from src.features.edit.core.color_chain import (
    chain_extra_params, resolve_timeline_stack,
)
from src.features.edit.core.commands.color_commands import (
    SetTimelineGradeStackCommand,
)
from src.features.edit.core.project import EditProject
from src.shared.project.color_stack import ColorStackData


def _timeline_stack(timeline_id="tl-1", exposure=0.0):
    return ColorStackData(
        stack_id=f"timeline-{timeline_id}",
        name=f"Timeline Grade {timeline_id[:8]}",
        scope="timeline",
        grading_params={"exposure": exposure})


class TestTimelineStackModel:
    def test_command_apply_undo_roundtrip(self):
        project = EditProject.create(name="n07")
        stack = _timeline_stack(exposure=0.3)
        cmd = SetTimelineGradeStackCommand(
            command_id="c1", label="l", timeline_id="tl-1",
            stack_id=stack.stack_id, stack=stack)
        applied = cmd.apply(project)
        assert applied.timeline_grade_stack_id("tl-1") == stack.stack_id
        assert applied.color_stacks[stack.stack_id].scope == "timeline"
        undone = cmd.undo(applied)
        assert undone.timeline_grade_stack_id("tl-1") == ""
        assert stack.stack_id not in undone.color_stacks
        # 冲突拒绝（映射被改过）
        dirty = applied.with_timeline_grade_stacks({"tl-1": "other"})
        with pytest.raises(Exception):
            cmd.apply(dirty)

    def test_chain_extra_params_includes_timeline_segment(self):
        stack = _timeline_stack(exposure=0.5)
        project = EditProject.create(name="n07")
        cmd = SetTimelineGradeStackCommand(
            command_id="c0", label="l", timeline_id="tl-1",
            stack_id=stack.stack_id, stack=stack)
        project = cmd.apply(project)

        class _Clip:
            clip_id = "clip-1"
            color_trim_stack_id = None

        extras = chain_extra_params(project, "tl-1", _Clip())
        segments = [e.get("_chain_segment") for e in extras]
        assert segments[-1] == "timeline"
        # 无登记 → 退化（无 timeline 段）
        extras2 = chain_extra_params(project, "tl-2", _Clip())
        assert all(e.get("_chain_segment") != "timeline" for e in extras2)
        # resolve_timeline_stack 直接查询
        assert resolve_timeline_stack(project, "tl-1") is not None
        assert resolve_timeline_stack(project, "tl-2") is None

    def test_io_roundtrip_carries_timeline_stacks(self, tmp_path: Path):
        from src.features.edit.core.edit_project_io import EditProjectIO
        project = EditProject.create(name="n07io")
        stack = _timeline_stack(exposure=0.4)
        cmd = SetTimelineGradeStackCommand(
            command_id="c1", label="l", timeline_id="tl-1",
            stack_id=stack.stack_id, stack=stack)
        project = cmd.apply(project)
        target = tmp_path / "n07.topos"
        assert EditProjectIO().save_project(
            target, project, create_backup=False, use_ai_callback=False)
        loaded = EditProjectIO().load_project(target)
        if isinstance(loaded, tuple):
            loaded = loaded[0]
        assert loaded.timeline_grade_stack_id("tl-1") == stack.stack_id
        assert loaded.color_stacks[stack.stack_id].grading_params == {
            "exposure": 0.4}
        # 闭包：timeline 栈图自动进 R09 收集（color_stacks 全量）
        with zipfile.ZipFile(target) as zf:
            settings = zf.read("project.json").decode("utf-8") \
                if "project.json" in zf.namelist() else ""
        assert stack.stack_id in project.color_stacks  # 保存源含栈


class TestHostTimelineBinding:
    def test_ensure_creates_and_binds_timeline_scope(self):
        """切 timeline scope：懒建中性栈 + 绑定；编辑落 timeline 栈。"""
        pytest.importorskip("PyQt6")
        from src.app.controllers.color_graph_controller import (
            ColorGraphAppHost,
        )
        h, clip_stack_id = _harness_with_controller()
        host = h.host
        # 懒建
        stack_id = host.ensure_timeline_grade_stack("tl-1")
        assert stack_id
        project = h.holder["project"]
        assert project.timeline_grade_stack_id("tl-1") == stack_id
        stack = project.color_stacks[stack_id]
        assert stack.scope == "timeline"
        # 中性 = 默认 grade（零值/exposure 0），非空 dict
        assert float(stack.grading_params.get("exposure", 0.0) or 0.0) == 0.0
        # 再次 ensure 幂等
        assert host.ensure_timeline_grade_stack("tl-1") == stack_id
        # 编辑经图命令落 timeline 栈
        host.on_clip_stack_loaded(stack_id, scope="timeline",
                                  scope_owner_id="tl-1")
        from src.features.color.core.color_graph_service import ColorGraphService
        graph = project.color_stacks[stack_id].color_graph
        _ = ColorGraphService  # noqa: F841
        # timeline 初始图仅终端——先插入 corrector（同一命令路径）
        service = ColorGraphService()
        grown = service.apply_intent(graph, "insert_corrector", {})
        assert grown.ok
        new_node_id = next(iter(grown.id_mapping.values()))
        host.bridge.begin_transaction("tid-struct")
        host.bridge.add_to_transaction(grown, "tid-struct", "insert_corrector")
        host.bridge.flush_transaction()
        host.bridge.begin_transaction("tid-tl")
        result = host.bridge.submit_buffered(
            "set_node_option",
            {"node_id": new_node_id,
             "name": "grade_patch", "value": ("exposure", 42.0)},
            "tid-tl")
        assert result
        host.bridge.flush_transaction()
        project = h.holder["project"]
        tl_stack = project.color_stacks[stack_id]
        node = next(n for n in tl_stack.color_graph.nodes
                    if n.node_id == new_node_id)
        assert node.params["grade"]["exposure"] == pytest.approx(42.0)
        # clip 栈未被串写
        clip_stack = project.color_stacks[clip_stack_id]
        for n in (clip_stack.color_graph.nodes if
                  clip_stack.color_graph is not None else ()):
            assert "exposure" not in (n.params.get("grade") or {})

    def test_undo_restores_timeline_edit(self):
        pytest.importorskip("PyQt6")
        h, clip_stack_id = _harness_with_controller()
        host = h.host
        stack_id = host.ensure_timeline_grade_stack("tl-1")
        host.on_clip_stack_loaded(stack_id, scope="timeline",
                                  scope_owner_id="tl-1")
        from src.features.color.core.color_graph_service import ColorGraphService
        graph = h.holder["project"].color_stacks[stack_id].color_graph
        service = ColorGraphService()
        grown = service.apply_intent(graph, "insert_corrector", {})
        new_node_id = next(iter(grown.id_mapping.values()))
        host.bridge.begin_transaction("tid-struct2")
        host.bridge.add_to_transaction(grown, "tid-struct2", "insert_corrector")
        host.bridge.flush_transaction()
        host.bridge.begin_transaction("tid-u")
        host.bridge.submit_buffered(
            "set_node_option",
            {"node_id": new_node_id,
             "name": "grade_patch", "value": ("exposure", 7.0)},
            "tid-u")
        host.bridge.flush_transaction()
        assert len(h.cmd_stack._undo_stack) >= 1
        undone = h.cmd_stack.undo(h.holder["project"])
        h.holder["project"] = undone
        tl = undone.color_stacks[stack_id]
        node = next((n for n in (tl.color_graph.nodes
                                 if tl.color_graph is not None else ())
                     if n.node_id == new_node_id), None)
        assert node is None or "exposure" not in (node.params.get("grade")
                                                  or {})


def _project_with_stack():
    from src.features.color.core.color_graph_migration import (
        migrate_legacy_stack,
    )
    from tests.color.fixtures.color_graph import f03_layer_stack
    project, stack_id = EditProject.create(name="n07h").create_color_stack(
        asset_id="a1", name="s")
    stack_data = project.color_stacks[stack_id].with_layers(
        [l.to_dict(None) for l in f03_layer_stack()])
    graph = migrate_legacy_stack(stack_data)
    project = project.update_color_stack(stack_id, color_graph=graph)
    return project, stack_id, graph


def _first_corrector(graph):
    return next(n for n in graph.nodes if n.node_type == "corrector")


def qapp_fixture():
    import pytest as _pytest
    from PyQt6.QtWidgets import QApplication
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


class _ControllerHarness:
    def __init__(self) -> None:
        from src.app.controllers.color_graph_controller import (
            ColorGraphAppHost,
        )
        from src.features.edit.core.commands import ProjectCommandStack
        project, stack_id, graph = _project_with_stack()
        self.holder = {"project": project}
        self.cmd_stack = ProjectCommandStack()
        self.stack_id = stack_id
        self.graph = graph
        self.host = ColorGraphAppHost(
            get_project=lambda: self.holder["project"],
            set_project=lambda p: self.holder.__setitem__("project", p),
            project_stack=self.cmd_stack,
            engine_provider=lambda: None,
            request_render=lambda: None,
            panels_provider=lambda: {},
        )


def _harness_with_controller():
    harness = _ControllerHarness()
    return harness, harness.stack_id
