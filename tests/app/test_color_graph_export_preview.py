"""B13 导出图闸门测试 —— 图不可静默丢效果（计划 B13 验收子集）。

- 可投影串行图：grade 来自图派生 layers（stale legacy layers 不覆盖）；
- 复杂图：显式 color_graph_not_exportable，禁止"成功但少效果"。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.features.color.core.color_graph_migration import migrate_legacy_stack
from src.features.color.core.color_graph_service import ColorGraphService
from src.features.edit.core.project import EditProject
from tests.color.fixtures.color_graph import f03_layer_stack
from tests.color.test_color_graph_migration import _stack_with_layers


def _graded_stack(layers):
    """带非默认平铺参数的栈（避免'无需调色'提前返回 None）。"""
    stack = _stack_with_layers(layers)
    grading = dict(stack.grading_params)
    grading["exposure"] = 0.5
    return stack.with_grading_params(grading)


def _exporter_with_stack(stack):
    project = SimpleNamespace(color_stacks={stack.stack_id: stack})
    from src.features.edit.core.timeline_export import TimelineExporter
    exporter = TimelineExporter.__new__(TimelineExporter)
    exporter._project = project
    return exporter


def _layer_info(stack_id: str):
    clip = SimpleNamespace(clip_id="clip-1", color_stack_id=stack_id)
    return SimpleNamespace(clip=clip)


class TestExportGraphGate:
    def test_projectable_graph_derives_grade_from_graph(self):
        """可投影图：grade 的 layers 来自图投影（图优先）。"""
        layers = f03_layer_stack()
        stack = _graded_stack(layers)
        graph = migrate_legacy_stack(
            stack.with_layers([l.to_dict(None) for l in layers]))
        # stale legacy layers（与图不同）——图必须赢
        stale = stack.with_layers(
            [layers[0].to_dict(None)], active_layer_index=0)
        carrying = stale.with_color_graph(graph)
        exporter = _exporter_with_stack(carrying)
        grade = exporter._get_clip_color_grade(_layer_info(carrying.stack_id))
        assert grade is not None
        layers_payload = grade.get("layers") or []
        assert len(layers_payload) == 3, "必须取图投影的 3 层而非 stale 1 层"

    def test_complex_graph_refuses_export(self):
        counter = iter(range(50))
        service = ColorGraphService(id_factory=lambda: f"e{next(counter)}")
        stack = _graded_stack(f03_layer_stack())
        graph = migrate_legacy_stack(stack)
        grown = service.apply_intent(graph, "add_parallel_branch", {})
        assert grown.ok
        carrying = stack.with_color_graph(grown.graph)
        exporter = _exporter_with_stack(carrying)
        with pytest.raises(RuntimeError) as ei:
            exporter._get_clip_color_grade(_layer_info(carrying.stack_id))
        assert "color_graph_not_exportable" in str(ei.value)

    def test_plain_legacy_stack_unchanged(self):
        stack = _graded_stack(f03_layer_stack())
        exporter = _exporter_with_stack(stack)
        grade = exporter._get_clip_color_grade(_layer_info(stack.stack_id))
        assert grade is not None
        assert len(grade.get("layers") or []) == 3
