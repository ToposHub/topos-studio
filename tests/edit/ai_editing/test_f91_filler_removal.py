"""F9.1 · 删除口头语——词级检测与可审批删除计划。"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src.features.edit.ai_editing.filler_removal import (
    detect_filler_words,
    timings_fingerprint,
    build_filler_removal_plan,
)
from src.features.edit.ai_editing.models import (
    OperationKind,
    PlanMode,
    PlanTarget,
)
from src.features.edit.core.captions.model import CaptionWordTiming

pytestmark = pytest.mark.edit_core


def _timings():
    return [
        CaptionWordTiming(text="So", start_frame=0, end_frame=4),
        CaptionWordTiming(text="um", start_frame=4, end_frame=7,
                          confidence=0.95),
        CaptionWordTiming(text="we", start_frame=7, end_frame=9),
        CaptionWordTiming(text="呃", start_frame=12, end_frame=15,
                          confidence=0.9),      # zh 填充词
        CaptionWordTiming(text="launch", start_frame=15, end_frame=19),
        CaptionWordTiming(text="uh", start_frame=20, end_frame=23,
                          confidence=0.4),       # 低置信 → review
        CaptionWordTiming(text="today", start_frame=23, end_frame=27),
    ]




def _timeline():
    """词帧域的宿主时间线：v1 轨单个 clip 覆盖 [0, 40)。"""
    from src.features.edit.core.timeline_v2 import (
        ClipInstance, EditTrack, TimelineSequence,
    )
    clip = ClipInstance(
        clip_id="c-main", asset_id="a1", track_id="v1",
        timeline_start=0, timeline_duration=40,
        source_start=0, source_duration=40, speed=1.0)
    track = EditTrack(track_id="v1", track_type="video", name="V1",
                      clips=(clip,))
    return TimelineSequence(
        timeline_id="tl1", name="TL", fps=25,
        video_tracks=(track,), audio_tracks=(), subtitle_tracks=())

class TestDetectFillerWords:
    def test_filler_hits_with_frame_spans(self):
        result = detect_filler_words(_timings())
        assert [h.text for h in result.deletable] == ["um", "呃"]
        assert result.deletable[0].start_frame == 4
        assert result.deletable[0].end_frame == 7
        assert result.deletable[0].review_only is False

    def test_low_confidence_not_deleted_review_only(self):
        """低置信词不删——人工排除（review_hits）。"""
        result = detect_filler_words(_timings())
        assert [h.text for h in result.review_hits] == ["uh"]
        assert result.review_hits[0].confidence == 0.4

    def test_mixed_language_union(self):
        assert len(detect_filler_words(_timings()).deletable) == 2

    def test_custom_lexicon_extension(self):
        timings = [CaptionWordTiming(text="blorp", start_frame=0,
                                     end_frame=3, confidence=1.0)]
        result = detect_filler_words(
            timings, extra_lexicon={"en": ("blorp",)})
        assert [h.text for h in result.deletable] == ["blorp"]

    def test_punctuation_and_case_insensitive(self):
        timings = [
            CaptionWordTiming(text="Um,", start_frame=0, end_frame=3,
                              confidence=1.0)]
        assert len(detect_filler_words(timings).deletable) == 1


class TestPlanBuilding:
    def test_plan_is_preview_mode_with_whitelist_ops(self):
        """删除计划 = PREVIEW + 白名单 op（走既有审批链不新增旁路）。"""
        result = detect_filler_words(_timings())
        plan = build_filler_removal_plan(
            result, target=PlanTarget("p1", "tl1"),
            base_revision=7, plan_id="plan-1", timeline=_timeline())
        assert plan.mode == PlanMode.PREVIEW
        assert len(plan.operations) == 2  # review 项不进计划
        assert all(op.kind == OperationKind.DELETE_CLIP
                   for op in plan.operations)
        assert plan.base_revision == 7
        # M11（S02）：真实 track/clip + 词区间（可被真实 compiler 编译）
        assert all(op.params["track_id"] == "v1"
                   and op.params["clip_id"] == "c-main"
                   and "start_frame" in op.params
                   and "end_frame" in op.params
                   for op in plan.operations)

    def test_review_items_excluded_from_plan(self):
        result = detect_filler_words(_timings())
        plan = build_filler_removal_plan(
            result, target=PlanTarget("p1", "tl1"),
            base_revision=1, plan_id="plan-2", timeline=_timeline())
        op_words = [op.params.get("reason") for op in plan.operations]
        assert all("uh" not in w for w in op_words)

    def test_fingerprint_stable_and_sensitive(self):
        """幂等锚：同输入同哈希；内容变化哈希变（重试不重复删）。"""
        t = _timings()
        assert timings_fingerprint(t) == timings_fingerprint(t)
        changed = list(t)
        changed[0] = CaptionWordTiming(text="Different", start_frame=0,
                                       end_frame=4)
        assert timings_fingerprint(changed) != timings_fingerprint(t)

    def test_empty_detection_empty_plan(self):
        result = detect_filler_words([])
        plan = build_filler_removal_plan(
            result, target=PlanTarget("p", "t"), base_revision=0,
            plan_id="p3")
        assert plan.operations == ()


# ================================================================
# M11（S02）· 真实命令编译与事务
# ================================================================

class TestRealCompileAndApply:
    def test_plan_compiles_and_applies_through_real_compiler(self):
        """detect→build(带时间线)→compile_plan→apply→undo 全链。

        S02 验收：不用伪 clip_id；词区间经 split×2+ripple 真实删除；
        undo 精确恢复整轨。
        """
        from src.features.edit.ai_editing.plan_compiler import compile_plan
        result = detect_filler_words(_timings())
        plan = build_filler_removal_plan(
            result, target=PlanTarget("p1", "tl1"),
            base_revision=1, plan_id="plan-e2e", timeline=_timeline())
        compiled = compile_plan(plan)          # 此前直接 CompileError
        assert compiled.operation_count == 2

        timeline = _timeline()
        new_timeline, _updated_macro = compiled.macro.apply(timeline)
        # 两个词区间共 3+3=6 帧被 ripple 删除：单 clip 40→34 帧
        clips = new_timeline.video_tracks[0].clips
        assert len(clips) == 3  # 两次 split+delete 各留前后段并 ripple
        total = sum(c.timeline_duration for c in clips)
        assert total == 40 - 6

        undone = _updated_macro.undo(new_timeline)
        assert sum(c.timeline_duration
                   for c in undone.video_tracks[0].clips) == 40
        assert len(undone.video_tracks[0].clips) == 1

    def test_no_timeline_produces_no_illegal_ops(self):
        """未传时间线：不产出非法伪 id op（rationale 说明跳过）。"""
        result = detect_filler_words(_timings())
        plan = build_filler_removal_plan(
            result, target=PlanTarget("p1", "tl1"),
            base_revision=1, plan_id="plan-nt")
        assert plan.operations == ()
        assert "未提供时间线" in plan.rationale


# ================================================================
# M11.4 · 跨容器映射：容器内命中显式跳过（不映射波及容器的顶层删除）
# ================================================================

from src.features.edit.ai_editing.filler_removal import (
    FillerDetectionResult,
    FillerHit,
)


class TestContainerHitSkipped:
    def _timeline_with_container(self):
        from src.features.edit.core.timeline_v2 import (
            ClipInstance, EditTrack, Rational, TimelineSequence,
        )
        real = ClipInstance(
            clip_id="c-real", asset_id="a-real", track_id="v1",
            timeline_start=0, timeline_duration=50,
            source_start=0, source_duration=50)
        container = ClipInstance(
            clip_id="c-nested", asset_id="a-nested", track_id="v1",
            timeline_start=50, timeline_duration=50,
            source_start=0, source_duration=50)
        return TimelineSequence(
            timeline_id="t1", name="T", fps=Rational(25, 1),
            video_tracks=(EditTrack(
                track_id="v1", track_type="video", index=0,
                clips=(real, container),),),)

    def _plan(self, hit_range, project=None):
        from types import SimpleNamespace as NS
        from src.features.edit.ai_editing.filler_removal import (
            build_filler_removal_plan,
        )
        from src.features.edit.ai_editing.models import PlanTarget
        detection = FillerDetectionResult(hits=(
            FillerHit("um", *hit_range, 0.95),))
        return build_filler_removal_plan(
            detection, target=PlanTarget(project_id="p",
                                         timeline_id="t1"),
            base_revision=0, plan_id="t",
            timeline=self._timeline_with_container(),
            project=project)

    def test_nested_container_hit_skipped_with_reason(self):
        from types import SimpleNamespace as NS
        project = NS(assets={
            "a-real": NS(is_virtual=False),
            "a-nested": NS(is_virtual=True)})  # 嵌套序列素材
        plan = self._plan((60, 64), project=project)
        # 容器内命中：不产 op（顶层 ripple 会波及整个容器）
        assert plan.operations == ()
        assert any("容器" in s for s in plan.rationale.split("；"))

    def test_real_clip_hit_still_maps(self):
        from types import SimpleNamespace as NS
        project = NS(assets={
            "a-real": NS(is_virtual=False),
            "a-nested": NS(is_virtual=True)})
        plan = self._plan((10, 14), project=project)
        assert len(plan.operations) == 1
        assert plan.operations[0].params["clip_id"] == "c-real"

    def test_composite_clip_skipped_without_project(self):
        """composite_id 容器：无工程也可判（保守跳过面不扩大）。"""
        from src.features.edit.core.timeline_v2 import (
            ClipInstance, EditTrack, Rational, TimelineSequence,
        )
        from types import SimpleNamespace as NS
        from src.features.edit.ai_editing.filler_removal import (
            build_filler_removal_plan,
        )
        from src.features.edit.ai_editing.models import PlanTarget
        comp = ClipInstance(
            clip_id="c-comp", asset_id="a", track_id="v1",
            timeline_start=0, timeline_duration=50,
            source_start=0, source_duration=50, composite_id="g1")
        seq = TimelineSequence(
            timeline_id="t1", name="T", fps=Rational(25, 1),
            video_tracks=(EditTrack(
                track_id="v1", track_type="video", index=0,
                clips=(comp,),),),)
        plan = build_filler_removal_plan(
            FillerDetectionResult(hits=(FillerHit("um", 10, 14, 0.95),)),
            target=PlanTarget(project_id="p", timeline_id="t1"),
            base_revision=0, plan_id="t", timeline=seq)
        assert plan.operations == ()
        assert "容器" in plan.rationale
