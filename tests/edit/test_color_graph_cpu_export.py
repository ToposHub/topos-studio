"""R5：Edit/Deliver CPU 回退路径按权威图逐节点消费（不再扁平取首层）。

验收（BACKEND_REMEDIATION_PLAN R5）：
- 多 Corrector 图逐节点合成：切换 active_layer_index 输出不变；
  常量节点（legacy 参数域）与动态节点（params 轨道域）同时生效；
- 能力不足默认拒绝导出（ExportGradeDegradedError 指名原因），
  allow_degraded_grade=True 才显式降级到扁平路径；
- Edit CPU 预览回退与 Deliver CPU 导出同一入口同一输出（parity）。
"""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest

from src.features.color.core.color_graph_migration import migrate_legacy_stack
from src.features.color.core.color_graph_service import ColorGraphService
from src.features.edit.core.project import EditProject
from tests.color.fixtures.color_graph import f03_layer_stack


def _graph_with_tracks():
    """f03 串行 3 Corrector 图：C1 常量曝光（图命令域），C2 饱和度动画。"""
    project, sid = EditProject.create(name="r5").create_color_stack(
        asset_id="a1", name="s")
    stack = project.color_stacks[sid].with_layers(
        [layer.to_dict(None) for layer in f03_layer_stack()])
    graph = migrate_legacy_stack(stack)
    correctors = [n for n in graph.nodes if n.node_type == "corrector"]
    service = ColorGraphService()
    # C1：常量曝光（不动画，图命令域写 grade）
    static = service.apply_intent(graph, "set_node_option", {
        "node_id": correctors[0].node_id,
        "name": "grade_patch", "value": ("exposure", 1.0)})
    assert static.ok, static.diagnostics
    # C2：饱和度关键帧 0.0@10 → 1.0@30（linear）
    seeded = service.apply_intent(static.graph, "batch_edit_keyframes", {
        "edits": [
            {"action": "set", "node_id": correctors[1].node_id,
             "param": "saturation", "frame": 10, "value": 0.0},
            {"action": "set", "node_id": correctors[1].node_id,
             "param": "saturation", "frame": 30, "value": 1.0},
        ]})
    assert seeded.ok, seeded.diagnostics
    return project.color_stacks[sid].with_color_graph(seeded.graph), sid


def _exporter(stack, **attrs):
    from src.features.edit.core.timeline_export import TimelineExporter
    exporter = TimelineExporter.__new__(TimelineExporter)
    exporter._project = SimpleNamespace(
        color_stacks={stack.stack_id: stack})
    for key, value in attrs.items():
        setattr(exporter, key, value)
    return exporter


def _layer(stack_id: str, source_frame: int):
    clip = SimpleNamespace(clip_id="clip-1", color_stack_id=stack_id,
                           timeline_id="t1")
    return SimpleNamespace(clip=clip, clip_id="clip-1",
                           source_frame=source_frame)


def _gradient_frame(width: int = 32, height: int = 24) -> np.ndarray:
    """彩色渐变帧（饱和度/曝光变化可观测）。"""
    x = np.linspace(0, 1.0, width, dtype=np.float32)
    frame = np.zeros((height, width, 3), dtype=np.uint8)
    frame[..., 0] = (x * 255).astype(np.uint8)               # B 渐变
    frame[..., 1] = ((1.0 - x) * 255).astype(np.uint8)       # G 反向
    frame[..., 2] = (np.abs(x - 0.5) * 2 * 255).astype(np.uint8)  # R V 形
    return frame


def _mean_brightness(frame: np.ndarray) -> float:
    return float(frame[..., :3].mean())


class TestGraphCpuExport:
    def test_per_node_composition_ignores_active_layer(self):
        stack, sid = _graph_with_tracks()
        frame = _gradient_frame()
        outputs = []
        for active in (0, 1, 2):
            layered = stack.with_layers(stack.layers,
                                        active_layer_index=active)
            exporter = _exporter(layered)
            outputs.append(exporter._apply_color_grade_unstabilized(
                frame.copy(), _layer(sid, source_frame=20)))
        # 切换活动层：图输出逐字节一致（扁平路径会随活动层漂移）
        assert np.array_equal(outputs[0], outputs[1])
        assert np.array_equal(outputs[1], outputs[2])

    def test_constant_legacy_node_and_animated_node_both_apply(self):
        stack, sid = _graph_with_tracks()
        frame = _gradient_frame()
        exporter = _exporter(stack)
        at_start = exporter._apply_color_grade_unstabilized(
            frame.copy(), _layer(sid, source_frame=10))  # saturation=0
        at_end = exporter._apply_color_grade_unstabilized(
            frame.copy(), _layer(sid, source_frame=30))  # saturation=1
        # 动画节点生效：饱和度 0→1 改变色度分布（均值亮度不变也合法，
        # 用通道差异断言）
        start_spread = float(np.abs(
            at_start[..., 0].astype(float) - at_start[..., 2].astype(float)
        ).mean())
        end_spread = float(np.abs(
            at_end[..., 0].astype(float) - at_end[..., 2].astype(float)
        ).mean())
        assert end_spread != pytest.approx(start_spread)
        # 常量节点生效：图输出比扁平基线亮（C1 exposure=1.0 进合成；
        # 扁平路径用栈级默认参数，不含节点曝光）
        flat_stack = stack.with_color_graph(None)
        flat_exporter = _exporter(flat_stack)
        flat = flat_exporter._apply_color_grade_unstabilized(
            frame.copy(), _layer(sid, source_frame=30))
        assert _mean_brightness(at_end) != pytest.approx(
            _mean_brightness(flat))

    def test_unsupported_node_refuses_export_unless_degraded(self):
        from src.features.edit.core.timeline_export import (
            ExportGradeDegradedError,
        )
        stack, sid = _graph_with_tracks()
        graph = stack.color_graph
        correctors = [n for n in graph.nodes if n.node_type == "corrector"]
        service = ColorGraphService()
        # C3：邻域参数（mid_detail）→ CPU 生产域显式 unsupported
        patched = service.apply_intent(graph, "set_node_option", {
            "node_id": correctors[2].node_id,
            "name": "grade_patch", "value": ("mid_detail", 0.5)})
        assert patched.ok, patched.diagnostics
        carrying = stack.with_color_graph(patched.graph)
        frame = _gradient_frame()
        strict = _exporter(carrying)
        with pytest.raises(ExportGradeDegradedError, match="mid_detail"):
            strict._apply_color_grade_unstabilized(
                frame.copy(), _layer(sid, source_frame=20))
        # 显式降级：走扁平路径成片（不静默少效果——告警 + 扁平输出）
        degraded = _exporter(carrying, allow_degraded_grade=True)
        out = degraded._apply_color_grade_unstabilized(
            frame.copy(), _layer(sid, source_frame=20))
        assert out.shape == frame.shape

    def test_preview_cpu_fallback_matches_export(self):
        from unittest.mock import MagicMock
        from src.features.edit.core.timeline_preview_renderer import (
            AssetResolver, PreviewRenderWorker,
        )
        stack, sid = _graph_with_tracks()
        frame = _gradient_frame()
        exporter = _exporter(stack)
        preview = PreviewRenderWorker(
            frame_provider=MagicMock(),
            asset_resolver=AssetResolver(SimpleNamespace(
                color_stacks={sid: stack})),
            video_renderer=None)
        try:
            self._assert_parity(exporter, preview, stack, sid, frame)
        finally:
            preview._decode_executor.shutdown(wait=False)

    @staticmethod
    def _assert_parity(exporter, preview, stack, sid, frame):
        for source_frame in (10, 20, 30):
            exported = exporter._apply_color_grade_unstabilized(
                frame.copy(), _layer(sid, source_frame=source_frame))
            previewed = preview._apply_preview_color_grade_unstabilized(
                frame.copy(), _layer(sid, source_frame=source_frame))
            # Edit CPU 预览回退与 Deliver CPU 导出同一入口同一输出
            assert np.array_equal(exported, previewed)


class TestGraphCpuFailurePolicy:
    """R5：失败不得发布缺少节点/效果的预览或导出。"""

    def test_export_unexpected_failure_fails_fast_with_clip_id(
            self, monkeypatch):
        from src.features.color.core import color_graph_cpu
        stack, sid = _graph_with_tracks()
        frame = _gradient_frame()

        def _boom(*_args, **_kwargs):
            raise RuntimeError("keyer exploded")

        monkeypatch.setattr(color_graph_cpu, "run_graph_frame_cpu", _boom)
        exporter = _exporter(stack)
        with pytest.raises(ValueError, match="clip-1.*keyer exploded"):
            exporter._apply_color_grade_unstabilized(
                frame.copy(), _layer(sid, source_frame=20))

    def test_preview_unexpected_failure_does_not_publish_flattened_grade(
            self, monkeypatch):
        from unittest.mock import MagicMock
        from src.features.color.core import color_graph_cpu
        from src.features.edit.core.timeline_preview_renderer import (
            AssetResolver, PreviewRenderWorker,
        )
        stack, sid = _graph_with_tracks()
        frame = _gradient_frame()

        def _boom(*_args, **_kwargs):
            raise RuntimeError("lut resource missing")

        monkeypatch.setattr(color_graph_cpu, "run_graph_frame_cpu", _boom)
        preview = PreviewRenderWorker(
            frame_provider=MagicMock(),
            asset_resolver=AssetResolver(SimpleNamespace(
                color_stacks={sid: stack})),
            video_renderer=None)
        try:
            with pytest.raises(RuntimeError, match="lut resource missing"):
                preview._apply_preview_color_grade_unstabilized(
                    frame.copy(), _layer(sid, source_frame=20))
        finally:
            preview._decode_executor.shutdown(wait=False)
