"""editorial_skills 单元测试（plan §10.4 P2.2）。

覆盖四个端到端 Skill 产物构造器：

- build_auto_caption：转录 evidence → 字幕草稿 artifact + EditPlan（PREVIEW，
  ENSURE_SUBTITLE_TRACK + ADD_SUBTITLE）；无证据降级；reading_speed 诊断。
- build_subtitle_localization：翻译 text_document + TTS media_generation_ref +
  音轨放置 EditPlan；无 TTS 仅产翻译。
- build_rough_cut_plan：复用 variant plan，强制 PREVIEW；无 plan 降级。
- p2_official_manifests：auto_caption / subtitle_localization 两个新 manifest。

红线（plan §10.4 P2.2 / §1.4）：

- 字幕/音频放置/粗剪都只产 EditPlan（mode=PREVIEW），不直接写时间线。
- 生成音频先入生成素材箱（media_generation_ref）。
- 字幕草稿/翻译 trust=unverified（待核）。
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.edit_core

from src.features.ai_harness.editorial_skills import (
    P2_SKILL_META,
    SkillArtifact,
    TranslationResult,
    build_auto_caption,
    build_rough_cut_plan,
    build_subtitle_localization,
    p2_official_manifests,
)
from src.features.edit.ai_editing.editorial_models import (
    FrameRange, SegmentKind, SemanticSegment,
)
from src.features.edit.ai_editing.models import (
    EditPlan, OperationKind, PlanMode, PlanTarget,
)
from src.features.edit.ai_editing.plan_compiler import compile_plan


def _transcript_segs():
    return tuple(
        SemanticSegment(
            segment_id=f"t{i}", asset_id="a1", kind=SegmentKind.TRANSCRIPT,
            source_range=FrameRange(s, d), payload={"text": txt}, confidence=0.9,
        )
        for i, (s, d, txt) in enumerate([(0, 100, "hello"), (100, 100, "world")])
    )


# ================================================================
# build_auto_caption
# ================================================================


class TestAutoCaption:
    def test_produces_preview_plan_with_subtitle_ops(self):
        out = build_auto_caption(
            project_id="p1", timeline_id="t1", base_revision=0,
            transcript_segments=_transcript_segs(),
        )
        assert out.plan is not None
        assert out.plan.mode == PlanMode.PREVIEW  # 红线：不直接写
        # 1 ensure_track + 2 subtitle lines
        kinds = [op.kind for op in out.plan.operations]
        assert OperationKind.ENSURE_SUBTITLE_TRACK in kinds
        assert kinds.count(OperationKind.ADD_SUBTITLE) == 2

    def test_subtitle_draft_artifact(self):
        out = build_auto_caption(
            project_id="p1", timeline_id="t1", base_revision=0,
            transcript_segments=_transcript_segs(),
        )
        assert len(out.artifacts) == 1
        assert out.artifacts[0].artifact_type == "subtitle_draft"
        assert out.artifacts[0].trust == "unverified"  # 待核

    def test_subtitle_op_carries_evidence_ref(self):
        out = build_auto_caption(
            project_id="p1", timeline_id="t1", base_revision=0,
            transcript_segments=_transcript_segs(),
        )
        sub_ops = [op for op in out.plan.operations if op.kind == OperationKind.ADD_SUBTITLE]
        # 空转防护（AUD-22506）：没有 ADD_SUBTITLE 时必须失败，而不是循环体
        # 不执行导致本测试静默通过（可追溯性红线失去守护）。
        assert sub_ops, "build_auto_caption must emit ADD_SUBTITLE operations"
        # 每条字幕都带 evidence_ref（可追溯）
        for op in sub_ops:
            assert op.params.get("evidence_ref")
            assert op.intent.get("evidence_ref")

    def test_reading_speed_fast_diagnostic(self):
        """超速行产出 READING_SPEED_FAST 诊断（模块文档承诺的覆盖，AUD-22505）。"""
        # 24 帧（≈1 秒）内塞 27 个字符 → cps ≈ 27 > DEFAULT_MAX_CPS (15)。
        long_text = "这一行字幕远远超过每秒十五个字符阅读速度上限的长度要求"
        fast_seg = SemanticSegment(
            segment_id="t-fast", asset_id="a1", kind=SegmentKind.TRANSCRIPT,
            source_range=FrameRange(0, 24), payload={"text": long_text},
            confidence=0.9,
        )
        out = build_auto_caption(
            project_id="p1", timeline_id="t1", base_revision=0,
            transcript_segments=(fast_seg,),
        )
        fast = [d for d in out.diagnostics if d.get("code") == "READING_SPEED_FAST"]
        assert fast, "超速字幕行必须产出 READING_SPEED_FAST 诊断"
        assert fast[0]["severity"] == "info"
        assert fast[0]["line_ids"], "诊断必须携带超速行 id（供 UI 定位）"

    def test_no_transcript_degrades(self):
        out = build_auto_caption(
            project_id="p1", timeline_id="t1", base_revision=0,
            transcript_segments=(),
        )
        assert out.degraded is True
        assert out.plan is None
        assert any(d.get("code") == "NO_TRANSCRIPT" for d in out.diagnostics)

    def test_track_name_in_params(self):
        out = build_auto_caption(
            project_id="p1", timeline_id="t1", base_revision=0,
            transcript_segments=_transcript_segs(), track_name="中文字幕",
        )
        ensure_op = next(op for op in out.plan.operations
                         if op.kind == OperationKind.ENSURE_SUBTITLE_TRACK)
        assert ensure_op.params["track_name"] == "中文字幕"

    def test_brief_id_carried(self):
        out = build_auto_caption(
            project_id="p1", timeline_id="t1", base_revision=0,
            transcript_segments=_transcript_segs(), brief_id="b1",
        )
        assert out.plan.brief_id == "b1"


# ================================================================
# build_subtitle_localization
# ================================================================


class TestSubtitleLocalization:
    def test_translation_and_tts_artifacts(self):
        tts = SkillArtifact(
            artifact_id="gen:tts1", artifact_type="media_generation_ref",
            media_asset_id="asset-tts1", duration_frames=120,
        )
        tr = TranslationResult(
            text_document_id="td:1", target_language="英文", line_count=5,
        )
        out = build_subtitle_localization(
            project_id="p1", timeline_id="t1", base_revision=0,
            subtitle_lines=_transcript_segs(), translation=tr, tts_media_ref=tts,
        )
        types = [a.artifact_type for a in out.artifacts]
        assert "text_document" in types
        assert "media_generation_ref" in types

    def test_plan_is_preview(self):
        tts = SkillArtifact(artifact_id="gen:tts1", artifact_type="media_generation_ref")
        tr = TranslationResult(text_document_id="td:1", target_language="英文", line_count=3)
        out = build_subtitle_localization(
            project_id="p1", timeline_id="t1", base_revision=0,
            subtitle_lines=_transcript_segs(), translation=tr,
            tts_media_ref=SkillArtifact(
                artifact_id=tts.artifact_id,
                artifact_type=tts.artifact_type,
                media_asset_id="asset-tts1", duration_frames=120,
            ),
        )
        assert out.plan is not None
        assert out.plan.mode == PlanMode.PREVIEW
        assert [op.kind.value for op in out.plan.operations] == [
            "ensure_track", "add_clip",
        ]
        assert all(op.kind.value != "add_review_note" for op in out.plan.operations)

        compiled = compile_plan(out.plan)
        assert compiled.operation_count == 2
        assert [item["kind"] for item in compiled.operation_summaries.values()] == [
            "ensure_track", "add_clip",
        ]

    def test_term_warnings_diagnostic(self):
        tts = SkillArtifact(artifact_id="gen:tts1", artifact_type="media_generation_ref")
        tr = TranslationResult(
            text_document_id="td:1", target_language="英文", line_count=3,
            term_warnings=("术语A",),
        )
        out = build_subtitle_localization(
            project_id="p1", timeline_id="t1", base_revision=0,
            subtitle_lines=_transcript_segs(), translation=tr, tts_media_ref=tts,
        )
        assert any(d.get("code") == "TRANSLATION_TERMS" for d in out.diagnostics)

    def test_no_tts_only_translation(self):
        """无 TTS 时仅产翻译 artifact + 诊断（红线：不伪造音轨）。"""
        tr = TranslationResult(text_document_id="td:1", target_language="英文", line_count=3)
        out = build_subtitle_localization(
            project_id="p1", timeline_id="t1", base_revision=0,
            subtitle_lines=_transcript_segs(), translation=tr, tts_media_ref=None,
        )
        assert any(a.artifact_type == "text_document" for a in out.artifacts)
        assert any(d.get("code") == "NO_TTS_MEDIA" for d in out.diagnostics)


# ================================================================
# build_rough_cut_plan
# ================================================================


class TestRoughCutPlan:
    def test_forces_preview_mode(self):
        """红线：skill 永不直接 commit（COMMIT → 强制 PREVIEW）。"""
        commit_plan = EditPlan.create(
            target=PlanTarget(project_id="p1", timeline_id="t1"),
            base_revision=0, mode=PlanMode.COMMIT,
        )
        out = build_rough_cut_plan(
            project_id="p1", timeline_id="t1", base_revision=0,
            variant_plan=commit_plan,
        )
        assert out.plan.mode == PlanMode.PREVIEW

    def test_edit_plan_ref_artifact(self):
        plan = EditPlan.create(
            target=PlanTarget(project_id="p1", timeline_id="t1"),
            base_revision=0,
        )
        out = build_rough_cut_plan(
            project_id="p1", timeline_id="t1", base_revision=0, variant_plan=plan,
        )
        assert len(out.artifacts) == 1
        assert out.artifacts[0].artifact_type == "edit_plan_ref"
        assert out.artifacts[0].payload_ref == plan.plan_id

    def test_no_variant_degrades(self):
        out = build_rough_cut_plan(
            project_id="p1", timeline_id="t1", base_revision=0, variant_plan=None,
        )
        assert out.degraded is True
        assert out.plan is None


# ================================================================
# p2_official_manifests
# ================================================================


class TestP2Manifests:
    def test_official_manifest_ids(self):
        mans = p2_official_manifests()
        ids = {m.skill_id for m in mans}
        assert ids == {
            "topos.auto_caption",
            "topos.subtitle_localization",
            "topos.composite_render_import",
        }

    def test_composite_render_manifest_fields(self):
        m = next(
            m for m in p2_official_manifests()
            if m.skill_id == "topos.composite_render_import"
        )
        assert m.produces_plan is False
        assert m.required_evidence_kinds == ()
        assert m.roles == ()
        assert "composite_graph_id" in m.input_schema.get("properties", {})

    def test_auto_caption_manifest_fields(self):
        m = next(m for m in p2_official_manifests() if m.skill_id == "topos.auto_caption")
        assert m.title == "自动字幕"
        assert m.produces_plan is True
        assert m.roles == ()  # 官方 skill，走 workflow backend
        # input_schema 含 track_name
        assert "track_name" in m.input_schema.get("properties", {})

    def test_subtitle_localization_has_target_language(self):
        m = next(m for m in p2_official_manifests()
                 if m.skill_id == "topos.subtitle_localization")
        assert "target_language" in m.input_schema.get("properties", {})

    def test_p2_skill_meta_populated(self):
        assert "topos.auto_caption" in P2_SKILL_META
        assert "topos.subtitle_localization" in P2_SKILL_META
        assert P2_SKILL_META["topos.auto_caption"]["category"] == "字幕"


# ================================================================
# 红线综合：所有产物都是 PREVIEW
# ================================================================


class TestAllPlansArePreview:
    """plan §10.4 P2.2 红线：字幕/音频放置/粗剪都只产 EditPlan（PREVIEW）。"""

    def test_auto_caption_preview(self):
        out = build_auto_caption(
            project_id="p1", timeline_id="t1", base_revision=0,
            transcript_segments=_transcript_segs(),
        )
        assert out.plan.mode == PlanMode.PREVIEW

    def test_localization_preview(self):
        out = build_subtitle_localization(
            project_id="p1", timeline_id="t1", base_revision=0,
            subtitle_lines=_transcript_segs(),
            translation=TranslationResult("td:1", "英文", 3),
            tts_media_ref=SkillArtifact(
                artifact_id="g:1", artifact_type="media_generation_ref",
                media_asset_id="asset-tts1", duration_frames=120,
            ),
        )
        assert out.plan.mode == PlanMode.PREVIEW

    def test_rough_cut_preview(self):
        plan = EditPlan.create(
            target=PlanTarget(project_id="p1", timeline_id="t1"), base_revision=0,
        )
        out = build_rough_cut_plan(
            project_id="p1", timeline_id="t1", base_revision=0, variant_plan=plan,
        )
        assert out.plan.mode == PlanMode.PREVIEW
