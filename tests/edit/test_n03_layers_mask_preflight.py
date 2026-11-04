"""N03 余项 · 逐层载荷/mask 消费/预检阻断测试。

审计句：timeline_export 抽首个 qualifier/曲线/LUT 扁平化，未保留各
layer 的 opacity/mask/plugin/次序；mask_shapes_cpu 只有模型 helper 无
导出消费；preflight/delivery_matrix 丢效果仍 warning。
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

# float32 帧强制走全量 float 调色路径（uint8 快路径依赖真实分解器）
FRAME = np.full((8, 8, 3), 0.5, np.float32)


# ── 逐层载荷（_get_clip_color_grade） ──────────────────────────────

def _stack(layers, active_index=None):
    return NS(
        grading_params={"exposure": 0.5},
        curves_data={}, hsl_curve_data={}, mixer_params={},
        lut_path=None, soft_clip_params={},
        layers=layers, active_layer_index=active_index)


def _grade(exporter, layer_clip):
    """直调 _get_clip_color_grade（stub 宿主）。"""
    return TimelineExporter._get_clip_color_grade(exporter, layer_clip)


def _exporter(stack):
    clip = NS(clip_id="c1", color_stack_id="s1", color_trim_stack_id=None)
    project = NS(color_stacks={"s1": stack},
                 color_groups={})
    layer = NS(clip=clip, source_frame=0)
    return NS(_project=project, _sequence=NS(timeline_id="tl1")), layer


class TestLayerPayloadPreservation:
    def test_multi_layer_payload_keeps_all_and_order(self):
        layers = [
            {"curves_data": {"m": [0, 1]}, "opacity": 0.5},
            {"active_lut_path": "/l/x.cube", "opacity": 1.0,
             "plugins": [{"id": "p1"}]},
            {"qualifier_params": {"enabled": True, "hue_range": [0, .1]}},
        ]
        exporter, clip = _exporter(_stack(layers, active_index=0))
        grade = _grade(exporter, clip)
        assert grade is not None
        payload = grade["layers"]
        assert [e["index"] for e in payload] == [0, 1, 2]  # 次序保留
        assert payload[0]["active"] is True and payload[0]["opacity"] == 0.5
        assert payload[1]["lut_path"] == "/l/x.cube"
        assert payload[1]["plugins"] == [{"id": "p1"}]  # plugin 保留
        assert payload[2]["qualifier_params"]["enabled"] is True
        # 多层高级内容 → 组合超 CPU 扁平能力，advanced 标记升位
        assert grade["has_advanced_operations"] is True

    def test_single_layer_compat_flatten_unchanged(self):
        layers = [{"curves_data": {"m": [0, 1]}}]
        exporter, clip = _exporter(_stack(layers, active_index=0))
        grade = _grade(exporter, clip)
        assert grade["curves_data"] == {"m": [0, 1]}  # 兼容键等价填充
        assert len(grade["layers"]) == 1


# ── mask 消费（_apply_color_grade） ────────────────────────────────

def _mask_grade(shapes, allow=False, qp=None):
    grade = {
        "grading_params": {"gamma": (0.5, 0.5, 0.5)},
        "grade_keyframes": {},
        "layers": ({"index": 0, "active": True, "opacity": 1.0,
                    "mask_shapes": shapes},),
    }
    if qp:
        grade["qualifier_params"] = qp
    stub = NS(
        allow_degraded_grade=allow,
        _get_clip_color_grade=lambda layer: grade,
        _get_decomposed_grade_fastpath=lambda *a, **k: None,
        _color_grade_lut_cache={})
    # bd8b15f 起 _apply_color_grade = unstabilized + 稳定化包裹（无
    # _project/clip 状态 → 空态直通）
    stub._apply_color_grade_unstabilized = (
        lambda frame, layer: TimelineExporter._apply_color_grade_unstabilized(
            stub, frame, layer))
    stub._clip_stabilization_state = (
        lambda layer: TimelineExporter._clip_stabilization_state(stub, layer))
    return stub


def _apply(stub):
    return TimelineExporter._apply_color_grade(
        stub, FRAME, NS(clip_id="c1", source_frame=0))


class TestMaskConsumption:
    def test_analytic_circle_mask_restricts_grade(self):
        # 中央圆 mask：圈外保持原值（exposure 大幅提升仅圈内生效）
        shape = {"type": "circle", "visible": True,
                 "center": [0.5, 0.5], "size": [0.25, 0.25],
                 "feather": 0.0, "operation": "add"}
        stub = _mask_grade([shape])
        out = _apply(stub)
        corner = float(out[0, 0, 0])
        center = float(out[4, 4, 0])
        assert corner == pytest.approx(0.5, abs=0.01)  # mask 外=原图
        # mask 内=已调色（gamma 0.5 → 0.5 升到 ~0.72，实测 0.721）
        assert center > 0.65

    def test_unsupported_curve_mask_refuses_by_default(self):
        shape = {"type": "curve", "visible": True, "points": []}
        stub = _mask_grade([shape])
        with pytest.raises(ExportGradeDegradedError, match="不可评估形状"):
            _apply(stub)

    def test_unsupported_mask_allowed_when_declared(self):
        shape = {"type": "curve", "visible": True, "points": []}
        stub = _mask_grade([shape], allow=True)
        out = _apply(stub)  # 跳过 mask，整层调色继续
        assert out.shape == FRAME.shape


# ── 预检阻断（_check_color_delivery_preflight） ────────────────────

class TestPreflightBlocking:
    def _run(self, monkeypatch, allow_degraded=0):
        from src.features.deliver.core.video_export_preflight import (
            PreflightContext, PreflightResult, _check_color_delivery_preflight,
        )
        from src.features.deliver.core.video_export_preflight import (
            SourceKind,
        )
        job = NS(config={"allow_degraded_grade": allow_degraded},
                 source_kind=SourceKind.TIMELINE)
        row = NS(is_lossy=True, detail_key="curves")
        report = NS(rows=[row])
        monkeypatch.setattr(
            "src.features.color.core.delivery_matrix."
            "sequence_color_stacks", lambda seq, proj: {"s1": object()})
        monkeypatch.setattr(
            "src.features.color.core.delivery_matrix."
            "report_from_color_stacks",
            lambda *a, **k: report)
        monkeypatch.setattr(
            "src.features.color.core.delivery_matrix."
            "sequence_video_clip_ids", lambda seq: [])
        monkeypatch.setattr(
            "src.features.color.core.delivery_matrix."
            "delivery_preflight_issues",
            lambda r, tr=None: [("color.delivery.dropped.curves",
                                 "curves 将被丢弃")])
        ctx = PreflightContext(sequence=NS(), project=NS())
        result = PreflightResult()
        _check_color_delivery_preflight(job, ctx, result)
        return result

    def test_lossy_blocks_by_default(self, monkeypatch):
        result = self._run(monkeypatch)
        assert result.ok is False
        assert any("blocking" in i.code for i in result.issues)

    def test_declared_degrades_to_warning(self, monkeypatch):
        result = self._run(monkeypatch, allow_degraded=1)
        assert result.ok is True
        assert all("blocking" not in i.code for i in result.issues)
