"""M06（S11）· Qualifier 贯通导出侧求值与显式降级回归。

验收（升级计划 M §M06 首片）：qualifier.* 动画轨道在导出/预览侧按
source_frame 求值（与 Color 引擎 evaluate_qualifier_keyframes 同函数，
此前只在 Color 预览求值、导出侧被基础白名单静默忽略）；只开
qualifier 的栈 grade 不再返回 None（静默无效果）；CPU primary 导出
不渲染 matte——求值后**显式告警**丢弃（delivery 预检 WARNING 同源）。
"""

from __future__ import annotations

from types import SimpleNamespace as NS

import numpy as np
import pytest

pytestmark = pytest.mark.edit_core

from src.shared.project.color_stack import ColorStackData


def _qualifier_stack(**qp_overrides) -> ColorStackData:
    """只开 qualifier（无任何基础/高级参数）的栈——此前 grade=None。"""
    qp = {"enabled": True, "lum_low": 0.2, "lum_high": 0.8,
          "lum_soft_low": 0.0, "lum_soft_high": 0.0,
          "hue_enabled": False, "sat_enabled": False}
    qp.update(qp_overrides)
    return ColorStackData(
        stack_id="s1", asset_id="", name="q",
        grading_params={},
        layers=[{"layer_index": 0, "name": "L",
                 "qualifier_params": qp, "grade_keyframes": {}}],
    )


class TestQualifierGradePassthrough:
    def _exporter(self, project, clip):
        from src.features.edit.core.timeline_export import TimelineExporter
        exporter = TimelineExporter.__new__(TimelineExporter)
        exporter._project = project
        return exporter

    def test_qualifier_only_stack_not_silent_none(self):
        # S11：只开 qualifier 的栈此前返回 None（静默无效果）
        project = NS(
            color_stacks={"s1": _qualifier_stack()},
            color_groups={}, audio_sync_bindings={})
        clip = NS(clip_id="c1", color_stack_id="s1",
                  color_trim_stack_id=None)
        exporter = self._exporter(project, clip)
        grade = exporter._get_clip_color_grade(NS(clip=clip, clip_id="c1"))
        assert grade is not None
        assert grade["qualifier_params"]["lum_low"] == pytest.approx(0.2)
        assert grade["has_advanced_operations"] is True

    def test_disabled_qualifier_stays_none(self):
        project = NS(
            color_stacks={"s1": _qualifier_stack(enabled=False)},
            color_groups={}, audio_sync_bindings={})
        clip = NS(clip_id="c1", color_stack_id="s1",
                  color_trim_stack_id=None)
        exporter = self._exporter(project, clip)
        grade = exporter._get_clip_color_grade(NS(clip=clip, clip_id="c1"))
        # 无任何 active 项 → None（既有语义不变）
        assert grade is None


class TestQualifierFrameEvaluation:
    def test_animated_qualifier_applies_matte_per_frame(self, caplog):
        """M06.2：动画 qualifier 按帧求值并**像素级应用**（CPU keyer）。

        lum_high 轨道 0→96 帧 0.5→1.0：帧 48 求值 0.75。亮度带
        [0.2, 0.75) 内的像素被限定（grade 只作用于带内），带外保持源值。
        """
        from src.features.color.core.grade_keyframes import (
            GradeKeyframeTrack,
        )
        track = GradeKeyframeTrack("qualifier.lum_high")
        track.set_keyframe(0, 0.5, "linear")
        track.set_keyframe(96, 1.0, "linear")
        stack = _qualifier_stack(lum_high=0.5)
        stack.layers[0]["grade_keyframes"] = {
            "qualifier.lum_high": track}

        from src.features.edit.core.timeline_export import TimelineExporter
        exporter = TimelineExporter.__new__(TimelineExporter)
        grade = {
            "grading_params": {"exposure": 20.0},
            "grade_keyframes": {"qualifier.lum_high": track.to_dict()},
            "qualifier_params": dict(stack.layers[0]["qualifier_params"]),
        }
        exporter._get_clip_color_grade = lambda _: grade
        # 两像素：亮像素（l≈0.71，帧48时带内）与暗像素（l≈0.1，带外）
        frame = np.array([[[180, 180, 180], [25, 25, 25]]], dtype=np.uint8)
        out = exporter._apply_color_grade(
            frame, NS(clip_id="c1", source_frame=48))
        assert out is not None
        # 带外暗像素：matte=0 → 输出 == 源（grade 被限定掉）
        assert out[0, 1, 0] == 25

    def test_static_qualifier_limits_grade_to_band(self):
        """M06.2：静态 qualifier——grade 只作用于亮度带内像素。"""
        from src.features.edit.core.timeline_export import TimelineExporter
        exporter = TimelineExporter.__new__(TimelineExporter)
        exporter._get_clip_color_grade = lambda _: {
            "grading_params": {"exposure": 50.0},
            "grade_keyframes": None,
            "qualifier_params": {"enabled": True, "lum_low": 0.4,
                                 "lum_high": 0.9, "lum_soft_low": 0.0,
                                 "lum_soft_high": 0.0,
                                 "hue_enabled": False, "sat_enabled": False},
        }
        frame = np.array([[[180, 180, 180], [25, 25, 25]]], dtype=np.uint8)
        out = exporter._apply_color_grade(
            frame, NS(clip_id="c1", source_frame=0))
        # 带内亮像素被提亮；带外暗像素保持源值（matte 限定生效）
        assert int(out[0, 0, 0]) > 180
        assert out[0, 1, 0] == 25

    def test_postprocess_params_warn_still_applies_basic(self, caplog):
        """matte 后处理（clean/blur/denoise）CPU 不支持——显式告警，
        基础 keyer 仍应用。"""
        import logging
        from src.features.edit.core.timeline_export import TimelineExporter
        exporter = TimelineExporter.__new__(TimelineExporter)
        exporter._get_clip_color_grade = lambda _: {
            "grading_params": {"exposure": 50.0},
            "grade_keyframes": None,
            "qualifier_params": {"enabled": True, "lum_low": 0.4,
                                 "lum_high": 0.9, "blur": 3.0},
        }
        frame = np.array([[[180, 180, 180], [25, 25, 25]]], dtype=np.uint8)
        with caplog.at_level(logging.WARNING):
            out = exporter._apply_color_grade(
                frame, NS(clip_id="c1", source_frame=0))
        assert "后处理" in caplog.text
        assert out[0, 1, 0] == 25  # 基础 keyer 仍限定


# ================================================================
# M06.3 · 0/48/96 帧全链像素对拍（CPU 导出 vs 逐帧闭式参考）
# ================================================================

class TestFrameChainPixelParity:
    """验收向量：动画 qualifier（lum_high 0→96 帧 0.5→1.0）在帧
    0/48/96 的导出输出 == 独立计算的逐帧参考（keyframe 求值 →
    qualifier_cpu matte → GradingMath → matte 混合），三帧全对拍。"""

    @staticmethod
    def _reference(frame_u8: np.ndarray, lum_high: float,
                   exposure: float) -> np.ndarray:
        """逐帧闭式参考（与导出链各环节同一权威实现，独立组装）。"""
        from src.features.color.core.qualifier_cpu import (
            apply_qualifier_masked_grade,
            compute_qualifier_matte,
        )
        from src.features.color.core.the_decompiler import GradingMath
        rgb = frame_u8[..., ::-1].astype(np.float32) / 255.0
        qp = {"enabled": True, "lum_low": 0.2, "lum_high": lum_high,
              "lum_soft_low": 0.0, "lum_soft_high": 0.0,
              "hue_enabled": False, "sat_enabled": False}
        matte = compute_qualifier_matte(rgb, qp)
        graded = GradingMath.apply_grading(
            rgb.copy(), {"exposure": exposure}).reshape(rgb.shape)
        mixed = apply_qualifier_masked_grade(rgb, graded, matte)
        return np.rint(np.clip(mixed[..., ::-1], 0.0, 1.0)
                       * 255.0).astype(np.uint8)

    def test_frames_0_48_96_match_reference(self):
        from src.features.color.core.grade_keyframes import (
            GradeKeyframeTrack,
        )
        from src.features.edit.core.timeline_export import TimelineExporter
        track = GradeKeyframeTrack("qualifier.lum_high")
        track.set_keyframe(0, 0.5, "linear")
        track.set_keyframe(96, 1.0, "linear")
        exporter = TimelineExporter.__new__(TimelineExporter)
        exporter._get_clip_color_grade = lambda _: {
            "grading_params": {"exposure": 50.0},
            "grade_keyframes": {"qualifier.lum_high": track.to_dict()},
            "qualifier_params": {"enabled": True, "lum_low": 0.2,
                                 "lum_high": 0.5, "lum_soft_low": 0.0,
                                 "lum_soft_high": 0.0,
                                 "hue_enabled": False,
                                 "sat_enabled": False},
        }
        # 四梯度亮度像素：255/180/100/25——随帧推进的带上沿变化
        # 应改变中亮像素的限定状态（带内/带外随 lum_high 演化）
        frame = np.array([[[255, 255, 255], [180, 180, 180],
                           [100, 100, 100], [25, 25, 25]]],
                         dtype=np.uint8)
        for f, lum_high in ((0, 0.5), (48, 0.75), (96, 1.0)):
            out = exporter._apply_color_grade(
                frame.copy(), NS(clip_id="c1", source_frame=f))
            expected = self._reference(frame, lum_high, 50.0)
            assert out is not None, f
            assert np.array_equal(out, expected), (
                f"帧 {f}（lum_high={lum_high}）全链输出与参考不符",
                out, expected)

    def test_band_edge_moves_across_frames(self):
        """对拍有效性：三帧的中间像素（亮度≈0.71）限定状态确实变化
        ——帧 0 带外（0.71>0.5）/帧 96 带内（0.71<1.0），参考非平凡。"""
        from src.features.color.core.qualifier_cpu import (
            compute_qualifier_matte,
        )
        rgb = np.full((1, 1, 3), 180 / 255.0, np.float32)
        for lum_high, expect_in in ((0.5, False), (0.75, True),
                                    (1.0, True)):
            matte = compute_qualifier_matte(rgb, {
                "enabled": True, "lum_low": 0.2, "lum_high": lum_high,
                "lum_soft_low": 0.0, "lum_soft_high": 0.0,
                "hue_enabled": False, "sat_enabled": False})
            assert bool(matte[0, 0] > 0.5) == expect_in, lum_high
