"""M05（S08）· 调色链顺序回归：pre → main → trim → post。

验收（升级计划 M §M05.1）：以 contrast35（pre）+ exposure20（main）
非交换向量消除既有顺序误差（旧实现 main→pre 单像素最大误差
0.0439084）；链段带 ``_chain_segment`` 标签，pre 段缺失时 trim 不
错位到主栈之前。四路消费（CPU 导出 / CPU 预览 / GPU 预览 /
GPU 导出 uniform 序列）同一顺序。
"""

from __future__ import annotations

from types import SimpleNamespace as NS

import numpy as np
import pytest

pytestmark = pytest.mark.edit_core

from src.features.color.core.the_decompiler import GradingMath
from src.features.edit.core.color_chain import chain_extra_params
from src.features.edit.core.color_group import ColorGroupData, ColorGroupMember
from src.shared.project.color_stack import ColorStackData


def _stack(stack_id: str, **params) -> ColorStackData:
    return ColorStackData(
        stack_id=stack_id, asset_id="",
        name=stack_id, grading_params=dict(params))


def _project_with_chain(pre=None, trim=None, post=None):
    """带组（pre/post 栈）+ clip（trim 栈）的工程。"""
    stacks = {}
    group_kwargs = {}
    if pre is not None:
        stacks["s-pre"] = pre
        group_kwargs["pre_group_stack_id"] = "s-pre"
    if post is not None:
        stacks["s-post"] = post
        group_kwargs["post_group_stack_id"] = "s-post"
    if trim is not None:
        stacks["s-trim"] = trim
    group = ColorGroupData(
        group_id="g1", name="G",
        members=(ColorGroupMember(timeline_id="tl1", clip_id="c1"),),
        **group_kwargs)
    project = NS(
        color_stacks=stacks,
        color_groups={"g1": group},
    )
    clip = NS(clip_id="c1", color_trim_stack_id=(
        "s-trim" if trim is not None else None))
    return project, clip


# ================================================================
# 链段标签与缺段不错位
# ================================================================

class TestChainSegmentLabels:
    def test_all_segments_labeled_in_order(self):
        project, clip = _project_with_chain(
            pre=_stack("s-pre", contrast=35.0),
            trim=_stack("s-trim", shadow=-10.0),
            post=_stack("s-post", saturation=15.0))
        extras = chain_extra_params(project, "tl1", clip)
        assert [e["_chain_segment"] for e in extras] == ["pre", "trim", "post"]

    def test_missing_pre_does_not_displace_trim(self):
        # pre 缺失时 trim 仍标记为 trim（消费方不会把它放到主栈之前）
        project, clip = _project_with_chain(
            trim=_stack("s-trim", shadow=-10.0))
        extras = chain_extra_params(project, "tl1", clip)
        assert len(extras) == 1
        assert extras[0]["_chain_segment"] == "trim"


# ================================================================
# CPU 导出顺序（生产 _apply_color_grade 路径）
# ================================================================

class TestExportChainOrder:
    def _exporter(self, grade):
        from src.features.edit.core.timeline_export import TimelineExporter
        exporter = TimelineExporter.__new__(TimelineExporter)
        exporter._get_clip_color_grade = lambda _: grade
        return exporter

    def test_pre_before_main_exact_match(self):
        # S08 验收向量：contrast35(pre) + exposure20(main) 非交换
        main_params = {"exposure": 20.0}
        pre = {"contrast": 35.0, "_chain_segment": "pre"}
        exporter = self._exporter({
            "grading_params": main_params,
            "chain_extra_params": [pre]})
        frame = np.array([[[25, 90, 200]]], dtype=np.uint8)
        actual = exporter._apply_color_grade(frame, NS(clip_id="x", source_frame=0))
        rgb = frame[..., ::-1].astype(np.float32).reshape(-1, 3) / 255.0
        expected = GradingMath.apply_grading(
            GradingMath.apply_grading(rgb, pre), main_params)
        expected_u8 = np.rint(
            np.clip(expected.reshape(frame.shape)[..., ::-1], 0, 1) * 255
        ).astype(np.uint8)
        assert actual.dtype == np.uint8
        # 顺序正确 = 与 pre→main 参考一致（旧 main→pre 单像素差可达 11/255）
        assert np.array_equal(actual, expected_u8)

    def test_full_chain_order_through_real_group(self):
        # 真实组链：pre→main→trim→post 全四段经生产 grade 字典
        project, clip = _project_with_chain(
            pre=_stack("s-pre", contrast=35.0),
            trim=_stack("s-trim", shadow=-10.0),
            post=_stack("s-post", saturation=15.0))
        extras = chain_extra_params(project, "tl1", clip)
        main_params = {"exposure": 20.0}
        exporter = self._exporter({
            "grading_params": main_params,
            "chain_extra_params": extras})
        frame = np.array([[[25, 90, 200]]], dtype=np.uint8)
        actual = exporter._apply_color_grade(frame, NS(clip_id="c1", source_frame=0))
        rgb = frame[..., ::-1].astype(np.float32).reshape(-1, 3) / 255.0
        buf = GradingMath.apply_grading(
            rgb, extras[0])                       # pre
        buf = GradingMath.apply_grading(buf, main_params)  # main
        buf = GradingMath.apply_grading(buf, extras[1])    # trim
        buf = GradingMath.apply_grading(buf, extras[2])    # post
        expected_u8 = np.rint(
            np.clip(buf.reshape(frame.shape)[..., ::-1], 0, 1) * 255
        ).astype(np.uint8)
        assert np.array_equal(actual, expected_u8)

    def test_trim_only_stays_after_main(self):
        # pre 缺失：trim 不前移（旧位置语义会把唯一段当"主栈后"——
        # 现在按标签固定 trim 段在主栈之后）
        project, clip = _project_with_chain(
            trim=_stack("s-trim", contrast=35.0))
        extras = chain_extra_params(project, "tl1", clip)
        main_params = {"exposure": 20.0}
        exporter = self._exporter({
            "grading_params": main_params,
            "chain_extra_params": extras})
        frame = np.array([[[25, 90, 200]]], dtype=np.uint8)
        actual = exporter._apply_color_grade(frame, NS(clip_id="c1", source_frame=0))
        rgb = frame[..., ::-1].astype(np.float32).reshape(-1, 3) / 255.0
        expected = GradingMath.apply_grading(
            GradingMath.apply_grading(rgb, main_params), extras[0])
        expected_u8 = np.rint(
            np.clip(expected.reshape(frame.shape)[..., ::-1], 0, 1) * 255
        ).astype(np.uint8)
        assert np.array_equal(actual, expected_u8)


# ================================================================
# GPU 导出 uniform pass 序列顺序
# ================================================================

class TestGpuExportPassOrder:
    def test_pre_pass_before_main_in_uniform_sequence(self):
        from src.features.edit.core.timeline_export import TimelineExporter
        exporter = TimelineExporter.__new__(TimelineExporter)
        grade = {
            "grading_params": {"exposure": 20.0},
            "chain_extra_params": [
                {"contrast": 35.0, "_chain_segment": "pre"},
                {"shadow": -10.0, "_chain_segment": "trim"},
                {"saturation": 15.0, "_chain_segment": "post"},
            ],
        }
        exporter._get_clip_color_grade = lambda _: grade
        uniforms = exporter._gpu_grade_uniforms(
            NS(clip_id="c1", source_frame=0))
        # pass 序列 = [pre, main, trim, post]；uniform dict 无序但
        # contrast 只在 pre pass、exposure 只在 main pass
        assert len(uniforms) == 4
        assert uniforms[0].get("u_contrast") == pytest.approx(35.0)
        assert uniforms[1].get("u_exposure") == pytest.approx(20.0)
        assert uniforms[2].get("u_shadow") == pytest.approx(-10.0)
        assert uniforms[3].get("u_saturation") == pytest.approx(15.0)

    def test_no_extras_returns_single_main(self):
        from src.features.edit.core.timeline_export import TimelineExporter
        exporter = TimelineExporter.__new__(TimelineExporter)
        exporter._get_clip_color_grade = lambda _: {
            "grading_params": {"exposure": 20.0}}
        uniforms = exporter._gpu_grade_uniforms(
            NS(clip_id="c1", source_frame=0))
        assert not isinstance(uniforms, list) or len(uniforms) == 1


# ================================================================
# M05.2b · 曲线 CPU 导出应用
# ================================================================

class TestCurvesExport:
    def test_master_curve_applied_after_main_grade(self):
        """GPU 合成序：主 grade → curves（M 通道提亮）。"""
        from src.features.edit.core.timeline_export import TimelineExporter
        exporter = TimelineExporter.__new__(TimelineExporter)
        # M 曲线：输入 128 → 输出 230（控制点 UI 倒置坐标）
        curves = {"M": [(0, 255), (128, 25), (255, 0)]}
        exporter._get_clip_color_grade = lambda _: {
            "grading_params": {},
            "grade_keyframes": None,
            "chain_extra_params": [],
            "qualifier_params": None,
            "curves_data": curves,
        }
        frame = np.array([[[128, 128, 128]]], dtype=np.uint8)
        out = exporter._apply_color_grade(
            frame, NS(clip_id="c1", source_frame=0))
        # 曲线抬升：输出明显高于输入
        assert int(out[0, 0, 0]) > 128

    def test_no_curves_unchanged_path(self):
        from src.features.edit.core.timeline_export import TimelineExporter
        exporter = TimelineExporter.__new__(TimelineExporter)
        exporter._get_clip_color_grade = lambda _: {
            "grading_params": {"exposure": 20.0},
            "grade_keyframes": None,
            "chain_extra_params": [],
            "qualifier_params": None,
            "curves_data": None,
        }
        frame = np.array([[[100, 100, 100]]], dtype=np.uint8)
        out = exporter._apply_color_grade(
            frame, NS(clip_id="c1", source_frame=0))
        # 仅主 grade（恒等曲线不进路径）
        assert int(out[0, 0, 0]) != 100  # exposure 生效
