"""F4.4 · 批量镜头匹配与人工修正——验收测试。

验收锚点（计划 §F4.4）：
- 多机位同场景匹配与肤色/中性色基准检查（已知偏移可恢复）；
- 剪影、极端光色、HDR 不被强行拉成同分布（排除守卫 + 置信压制）；
- 锁定镜头/品牌色保护（零写入 WB / 整体跳过）；
- 过时 revision 拒绝、单次 Undo、批量提交经 F4.1 微调栈（各镜头微调
  语义）+ F4.2 pre-shot-match 版本锚；
- 匹配参数映射与 GradingMath 数值锚定（可解释映射不是玄学）。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src.features.color.core.shot_match import (
    ShotStatistics,
    deltas_to_grading_params,
    match_shot,
    measure_shot,
    plan_shot_match,
)
from src.features.edit.core.shot_match_apply import (
    PRE_MATCH_VERSION_NAME,
    ApplyShotMatchCommand,
)

pytestmark = pytest.mark.color_core if hasattr(
    pytest.mark, "color_core") else pytest.mark.edit_core

RNG = np.random.default_rng(2026)


def _same_scene_frame(exposure_gain=1.0, wb=(1.0, 1.0, 1.0),
                      sat_scale=1.0, size=96) -> np.ndarray:
    """同场景合成帧：中性墙(55%) + 肤色块(20%) + 高光/暗部(25%)。"""
    h = w = size
    frame = np.zeros((h, w, 3), dtype=np.float64)
    neutral = 0.18 + 0.02 * RNG.standard_normal((h, w))
    frame[..., :] = np.clip(neutral, 0.01, None)[..., None]
    # 肤色块（Rec709 肤色线附近：R>G>B）
    y0, y1 = int(h * 0.3), int(h * 0.5)
    x0, x1 = int(w * 0.3), int(w * 0.7)
    frame[y0:y1, x0:x1] = np.array([0.45, 0.30, 0.20])
    # 高光块
    frame[int(h*0.6):int(h*0.8), int(w*0.1):int(w*0.3)] = 0.85
    # 暗部块
    frame[int(h*0.6):int(h*0.8), int(w*0.7):int(w*0.9)] = 0.02
    # 偏色/曝光/饱和
    frame *= np.array(wb, dtype=np.float64)
    frame *= exposure_gain
    mean = frame.mean(axis=-1, keepdims=True)
    frame = mean + (frame - mean) * sat_scale
    return frame.astype(np.float32)


class TestMeasureAndMatch:
    def test_skin_and_neutral_bases_measured(self):
        stats = measure_shot(_same_scene_frame(), "a")
        # 中性簇存在且均值接近 0.18
        assert stats.neutral_fraction > 0.2
        assert abs(stats.neutral_luma - 0.18) < 0.03
        # 肤色簇测得 R>G>B
        assert stats.skin_mean is not None and stats.skin_fraction > 0.01
        r, g, b = stats.skin_mean
        assert r > g > b

    def test_multicam_same_scene_recovers_known_offsets(self):
        """多机位同场景：已知 +0.5 stop / WB 暖偏 / 饱和偏 → 匹配量恢复。"""
        ref = measure_shot(_same_scene_frame(), "ref")
        darker_warm = measure_shot(
            _same_scene_frame(exposure_gain=2 ** -0.5, wb=(1.2, 1.0, 0.85)),
            "cam-b")
        result = match_shot(darker_warm, ref)
        assert abs(result.exposure_stops - 0.5) < 0.08  # 恢复曝光差
        assert result.wb_gains[0] < 1.05 and result.wb_gains[2] > 1.05 \
            or abs(result.wb_gains[0] - 1 / 1.2) < 0.1  # 抵消暖偏
        assert result.exclude_reason == ""
        assert result.confidence >= 0.5

    def test_saturation_ratio_recovers(self):
        ref = measure_shot(_same_scene_frame(), "ref")
        flat = measure_shot(_same_scene_frame(sat_scale=0.7), "flat")
        result = match_shot(flat, ref)
        assert result.saturation_ratio > 1.15  # 参考更饱和 → 提升建议

    def test_silhouette_not_forced_to_same_distribution(self):
        ref = measure_shot(_same_scene_frame(), "ref")
        silhouette = measure_shot(
            _same_scene_frame(exposure_gain=0.02), "sil")
        result = match_shot(silhouette, ref)
        assert "剪影" in result.exclude_reason
        assert result.confidence <= 0.5  # 必须走人工

    def test_extreme_cast_guard(self):
        ref = measure_shot(_same_scene_frame(), "ref")
        cast = measure_shot(
            _same_scene_frame(wb=(2.8, 1.0, 0.4)), "cast")
        result = match_shot(cast, ref)
        assert "极端光色" in result.exclude_reason

    def test_hdr_dynamic_range_not_flattened(self):
        ref = measure_shot(_same_scene_frame(), "ref")  # SDR 峰值 <1
        hdr = measure_shot(
            _same_scene_frame(exposure_gain=6.0), "hdr")  # peak >4
        result = match_shot(hdr, ref)
        assert "HDR" in result.exclude_reason

    def test_locked_shot_excluded_and_brand_skips_wb(self):
        ref = measure_shot(_same_scene_frame(), "ref")
        shot = measure_shot(
            _same_scene_frame(wb=(1.15, 1.0, 0.9)), "brand")
        locked = match_shot(shot, ref, locked=True)
        assert "锁定" in locked.exclude_reason
        brand = match_shot(shot, ref, brand_hue_protected=True)
        assert brand.exclude_reason == ""
        assert brand.wb_gains == (1.0, 1.0, 1.0)  # WB 保色相跳过


class TestParamMapping:
    def test_mapping_inverts_gradingmath(self):
        """映射数值锚定：GradingMath 用映射参数产生的 stops ≈ 目标。"""
        from src.features.color.core.the_decompiler import GradingMath
        for stops in (-1.0, -0.5, 0.5, 1.0, 1.5):
            params = deltas_to_grading_params(stops, (1, 1, 1), 1.0)
            e = params["exposure"]
            produced = (e / 100.0) ** 2 * 6.0 * (1 if e >= 0 else -1)
            assert abs(produced - stops) < 1e-3, (stops, produced)
        # WB 映射：temp/tint 参数恢复通道增益
        params = deltas_to_grading_params(0.0, (1.10, 1.0, 0.92), 1.0)
        assert abs((1 + params["temp"] / 200.0) - 1.10) < 1e-6
        assert abs((1 - params["tint"] / 200.0) - 1.0) < 1e-6

    def test_explain_string_covers_all_deltas(self):
        ref = measure_shot(_same_scene_frame(), "ref")
        shot = measure_shot(_same_scene_frame(exposure_gain=0.8), "s")
        result = match_shot(shot, ref)
        text = result.explain()
        assert "exposure" in text and "WB" in text and "confidence" in text


class TestPlanAndReport:
    def test_report_partitions_auto_vs_confirmation(self):
        ref = measure_shot(_same_scene_frame(), "ref")
        good = measure_shot(_same_scene_frame(exposure_gain=0.9), "good")
        poor = measure_shot(
            _same_scene_frame(exposure_gain=0.02), "sil")
        report = plan_shot_match(ref, [good, poor], locked_clip_ids=())
        auto = report.auto_applicable()
        confirm = report.needs_confirmation()
        assert any(s.clip_id == "good" for s in auto)
        # 剪影属硬排除（不自动、不进确认队列）——报告可见但不参与批量
        assert not any(s.clip_id == "sil" for s in auto)
        sil = next(s for s in report.suggestions if s.clip_id == "sil")
        assert sil.excluded and "剪影" in sil.exclude_reason


class TestApplyCommand:
    def _project(self):
        from src.features.edit.core.timeline_v2 import (
            ClipInstance, EditTrack, Rational, TimelineSequence)
        from src.features.edit.core.project import EditProject
        from src.shared.project.color_stack import ColorStackData
        stack = ColorStackData(stack_id="main", grading_params={
            "exposure": 0.0, "lift": (0, 0, 0), "gamma": (0, 0, 0),
            "gain": (0, 0, 0)})
        clips = tuple(
            ClipInstance(cid, "asset", "v1", i * 10, 10, 0, 10,
                         color_stack_id="main")
            for i, cid in enumerate(("cam-a", "cam-b", "cam-c")))
        timeline = TimelineSequence(
            timeline_id="tl1", name="TL", fps=24.0,
            video_tracks=(EditTrack(
                "v1", "V1", "video", 0, clips=clips),))
        return EditProject(
            project_id="p", name="P", color_stacks={"main": stack},
            timelines={"tl1": timeline}, revision=7)

    def _report(self, project):
        ref = measure_shot(_same_scene_frame(), "cam-a")
        cam_b = measure_shot(
            _same_scene_frame(exposure_gain=2 ** -0.4), "cam-b")
        cam_c = measure_shot(
            _same_scene_frame(exposure_gain=0.02), "cam-c")  # 剪影
        return plan_shot_match(ref, [cam_b, cam_c])

    def test_batch_apply_creates_trims_and_version_anchor(self):
        project = self._project()
        report = self._report(project)
        cmd = ApplyShotMatchCommand.create(project, "tl1", report)
        after, applied = cmd.apply(project)
        # 剪影镜头零写入；可应用镜头有 trim 栈
        tl = after.timelines["tl1"]
        clip_b = tl.get_clip_by_id("cam-b")
        clip_c = tl.get_clip_by_id("cam-c")
        assert clip_b.color_trim_stack_id
        assert clip_c.color_trim_stack_id is None
        trim = after.color_stacks[clip_b.color_trim_stack_id]
        assert trim.scope == "clip_trim"
        assert trim.grading_params["exposure"] > 0  # 补曝光
        # F4.2 锚：主栈有 pre-shot-match 版本
        versions = (after.color_stack_versions.get("main") or {}).get(
            "versions", {})
        assert PRE_MATCH_VERSION_NAME in versions
        assert applied.versioned_stack_ids == ("main",)
        # 主栈内容未被改动（微调语义）
        assert after.color_stacks["main"].grading_params["exposure"] == 0.0

    def test_undo_restores_everything(self):
        project = self._project()
        report = self._report(project)
        cmd = ApplyShotMatchCommand.create(project, "tl1", report)
        after, applied = cmd.apply(project)
        restored = applied.undo(after)
        assert restored.color_stacks.keys() == project.color_stacks.keys()
        assert restored.timelines["tl1"].get_clip_by_id(
            "cam-b").color_trim_stack_id is None
        assert restored.color_stack_versions == {}

    def test_stale_revision_refused_zero_write(self):
        project = self._project()
        report = self._report(project)
        cmd = ApplyShotMatchCommand.create(project, "tl1", report)
        from dataclasses import replace
        bumped = replace(project, revision=8)  # 工程已前进
        with pytest.raises(ValueError, match="过期"):
            cmd.apply(bumped)

    def test_locked_clips_never_enter_command(self):
        project = self._project()
        ref = measure_shot(_same_scene_frame(), "cam-a")
        cam_b = measure_shot(
            _same_scene_frame(exposure_gain=0.8), "cam-b")
        report = plan_shot_match(
            ref, [cam_b], locked_clip_ids=("cam-b",))
        with pytest.raises(ValueError, match="无可应用"):
            ApplyShotMatchCommand.create(project, "tl1", report)

    def test_trim_feeds_f41_chain_export_resolution(self):
        """导出一致：匹配写入的 trim 经 F4.1 链被导出解析消费。"""
        from src.features.edit.core.color_chain import (
            chain_extra_params, extras_active)
        project = self._project()
        report = self._report(project)
        cmd = ApplyShotMatchCommand.create(project, "tl1", report)
        after, _ = cmd.apply(project)
        clip_b = after.timelines["tl1"].get_clip_by_id("cam-b")
        extras = chain_extra_params(after, "tl1", clip_b)
        assert extras_active(extras)
        assert any(e.get("exposure", 0) > 0 for e in extras)
