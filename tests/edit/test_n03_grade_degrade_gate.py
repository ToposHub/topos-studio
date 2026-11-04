"""N03 · 导出调色降级硬门测试。

审计句：后处理只日志丢弃，keyer 失败可无限 matte 继续输出（视觉错误
标成功）。修复契约：两类降级默认拒绝导出（ExportGradeDegradedError）；
显式 allow_degraded_grade=True 才保留旧降级路径。
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace as NS

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

pytestmark = pytest.mark.edit_core

from src.features.edit.core.timeline_export import (  # noqa: E402
    ExportGradeDegradedError, TimelineExporter)

FRAME = np.full((4, 4, 3), 128, np.uint8)


def _stub_exporter(allow_degraded: bool, qualifier_params: dict) -> NS:
    grade = {"grading_params": {"exposure": 0.0},
             "qualifier_params": qualifier_params,
             "grade_keyframes": {}}
    stub = NS(
        allow_degraded_grade=allow_degraded,
        _get_clip_color_grade=lambda layer: grade,
        # 后续主参数路径的辅助（stub 化：走最简 float 全量应用）
        _get_decomposed_grade_fastpath=(
            lambda *a, **k: None),
        _color_grade_lut_cache={},
    )
    # bd8b15f 起 _apply_color_grade = unstabilized + 稳定化包裹（无
    # _project/clip 状态 → clip_stabilization_state 空 → 直通）
    stub._apply_color_grade_unstabilized = (
        lambda frame, layer: TimelineExporter._apply_color_grade_unstabilized(
            stub, frame, layer))
    stub._clip_stabilization_state = (
        lambda layer: TimelineExporter._clip_stabilization_state(stub, layer))
    return stub


def _layer():
    return NS(clip_id="c1", source_frame=0)


def _call(stub, frame=FRAME):
    return TimelineExporter._apply_color_grade(stub, frame, _layer())


class TestKeyerDegradeHardGate:
    def test_keyer_failure_refuses_by_default(self, monkeypatch):
        def boom(rgb, params):
            raise RuntimeError("keyer explode")

        monkeypatch.setattr(
            "src.features.color.core.qualifier_cpu.compute_qualifier_matte",
            boom)
        stub = _stub_exporter(False, {"hue_range": [0.0, 0.1]})
        with pytest.raises(ExportGradeDegradedError, match="拒绝导出"):
            _call(stub)

    def test_keyer_failure_allowed_when_declared(self, monkeypatch, caplog):
        def boom(rgb, params):
            raise RuntimeError("keyer explode")

        monkeypatch.setattr(
            "src.features.color.core.qualifier_cpu.compute_qualifier_matte",
            boom)
        stub = _stub_exporter(True, {"hue_range": [0.0, 0.1]})
        out = _call(stub)  # 显式降级：不抛，帧继续
        assert out.shape == FRAME.shape

    def test_postprocess_drop_refuses_by_default(self, monkeypatch):
        monkeypatch.setattr(
            "src.features.color.core.qualifier_cpu.compute_qualifier_matte",
            lambda rgb, params: np.ones(tuple(rgb.shape[:2]) + (1,),
                                            np.float32))
        monkeypatch.setattr(
            "src.features.color.core.qualifier_cpu."
            "qualifier_postprocess_active",
            lambda params: True)
        stub = _stub_exporter(False, {"hue_range": [0.0, 0.1]})
        with pytest.raises(ExportGradeDegradedError,
                           match="后处理.*拒绝导出"):
            _call(stub)

    def test_postprocess_drop_allowed_when_declared(self, monkeypatch):
        monkeypatch.setattr(
            "src.features.color.core.qualifier_cpu.compute_qualifier_matte",
            lambda rgb, params: np.ones(tuple(rgb.shape[:2]) + (1,),
                                            np.float32))
        monkeypatch.setattr(
            "src.features.color.core.qualifier_cpu."
            "qualifier_postprocess_active",
            lambda params: True)
        stub = _stub_exporter(True, {"hue_range": [0.0, 0.1]})
        out = _call(stub)
        assert out.shape == FRAME.shape

    def test_no_qualifier_unchanged(self):
        stub = _stub_exporter(False, {})
        out = _call(stub)
        assert out.shape == FRAME.shape  # 无 qualifier 不触发门


class TestLayerInternalAdvancedMarking:
    """N03 首条余项：layer 内 LUT/曲线/qualifier/插件键补标记。"""

    def _extras_with_stack_layers(self, layers):
        from src.features.edit.core import color_chain as cc
        stack = NS(grading_params={"exposure": 0.0},
                   curves_data={}, hsl_curve_data={}, mixer_params={},
                   lut_path=None, soft_clip_params={}, layers=layers)
        project = NS(color_stacks={"s1": stack})
        clip = NS(clip_id="c1", color_trim_stack_id="s1")
        # 无组：仅 trim 段生效
        from types import SimpleNamespace as NS2
        project2 = NS2(color_stacks={"s1": stack}, color_groups={})
        return cc.chain_extra_params(project2, "tl1", clip)

    def test_layer_lut_marks_unsupported(self):
        extras = self._extras_with_stack_layers(
            [{"active_lut_path": "/luts/x.cube"}])
        assert extras and extras[0]["_unsupported_advanced"] is True

    def test_layer_qualifier_marks_unsupported(self):
        extras = self._extras_with_stack_layers(
            [{"qualifier_params": {"hue_range": [0, 0.1]}}])
        assert extras and extras[0]["_unsupported_advanced"] is True

    def test_plain_layers_not_marked(self):
        extras = self._extras_with_stack_layers(
            [{"opacity": 1.0}, {}])
        assert extras and "_unsupported_advanced" not in extras[0]
