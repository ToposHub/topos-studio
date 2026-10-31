"""skill_catalog 单元测试（plan §10.3 P1.2）。

覆盖：

- ``build_form_fields``：从 manifest.input_schema + ui_card.form_fields 推导有序
  字段；排除由 invocation 自动填的 asset_ids/timeline_id/project_id/brief_id；
  target_duration 按 brief_template 默认补。
- ``evaluate_prerequisites``：风险 / 来源 / profile / 证据 / 模型 全部检查；
  local_dev 标未验证；缺证据 / 缺模型 -> ready=False。
- ``SkillCatalogView``：cards / card / recommend（ready 优先）。

红线（plan §10.3 P1.2 / §1.3）：

- local_dev 必须标未验证。
- 未满足前置条件不静默启动（ready=False）。
- 不修改 registry（只读 manifest）。
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.edit_core

from src.features.ai_harness.skill_catalog import (
    SkillCatalogView,
    build_form_fields,
    evaluate_prerequisites,
    risk_label_of,
)
from src.features.edit.ai_editing.editorial_models import SegmentKind
from src.features.edit.ai_editing.skill_manifest import (
    OFFICIAL_PRODUCT_AD_30S,
    OFFICIAL_SUBTITLE_DRAFT,
    OFFICIAL_VOICEOVER_POLISH,
    SkillManifest,
    SkillRisk,
    SkillSource,
    ToolPolicy,
)


# ================================================================
# build_form_fields
# ================================================================


class TestBuildFormFields:
    def test_subtitle_draft_has_target_duration(self):
        fields = build_form_fields(OFFICIAL_SUBTITLE_DRAFT)
        names = [f.name for f in fields]
        # subtitle_draft ui_card.form_fields=()，但 brief_template 带
        # target_duration={ideal:720, min:648, max:792} → 自动补该字段
        # （schema required 全为自动填字段，不额外产表单项）
        assert "target_duration" in names
        td = next(f for f in fields if f.name == "target_duration")
        assert td.field_type == "integer"
        assert td.default == 720
        assert td.min_value == 648
        assert td.max_value == 792
        assert "asset_ids" not in names  # 自动填的不进表单
        assert "timeline_id" not in names
        assert "project_id" not in names
        assert "brief_id" not in names
        assert names == ["target_duration"]

    def test_voiceover_polish_has_target_duration(self):
        fields = build_form_fields(OFFICIAL_VOICEOVER_POLISH)
        names = [f.name for f in fields]
        # brief_template 有 ideal=720 → 自动补 target_duration
        assert "target_duration" in names
        td = next(f for f in fields if f.name == "target_duration")
        assert td.field_type == "integer"
        assert td.default == 720
        assert td.min_value == 648
        assert td.max_value == 792

    def test_auto_filled_excluded(self):
        for m in (OFFICIAL_VOICEOVER_POLISH, OFFICIAL_PRODUCT_AD_30S, OFFICIAL_SUBTITLE_DRAFT):
            fields = build_form_fields(m)
            names = [f.name for f in fields]
            for auto in ("asset_ids", "timeline_id", "project_id", "brief_id"):
                assert auto not in names, f"{m.skill_id}: {auto} should be auto-filled"

    def test_enum_field(self):
        m = SkillManifest(
            skill_id="test.enum",
            input_schema={
                "type": "object",
                "required": ["platform"],
                "properties": {
                    "platform": {"type": "string", "enum": ["vertical", "horizontal"]},
                },
            },
            ui_card={"form_fields": ("platform",)},
        )
        fields = build_form_fields(m)
        plat = next(f for f in fields if f.name == "platform")
        assert plat.field_type == "enum"
        assert plat.options == ("vertical", "horizontal")
        assert plat.required is True

    def test_custom_manifest_empty_schema(self):
        m = SkillManifest(skill_id="test.empty")
        fields = build_form_fields(m)
        assert fields == []


# ================================================================
# evaluate_prerequisites
# ================================================================


class TestEvaluatePrerequisites:
    def test_subtitle_draft_ready_with_transcript_and_asr(self):
        rep = evaluate_prerequisites(
            OFFICIAL_SUBTITLE_DRAFT,
            available_evidence_kinds=(SegmentKind.TRANSCRIPT.value,),
            installed_models=("qwen3_asr",),
            has_profile=True,
        )
        assert rep.ready is True
        assert rep.missing_evidence_kinds == ()
        assert rep.missing_models == ()
        assert rep.unverified is False

    def test_missing_evidence_blocks(self):
        rep = evaluate_prerequisites(
            OFFICIAL_SUBTITLE_DRAFT,
            available_evidence_kinds=(),  # 无转录证据
            installed_models=("qwen3_asr",),
            has_profile=True,
        )
        assert rep.ready is False
        assert "transcript" in rep.missing_evidence_kinds
        assert any("证据" in c.label and not c.ok for c in rep.checks)

    def test_missing_model_blocks(self):
        rep = evaluate_prerequisites(
            OFFICIAL_SUBTITLE_DRAFT,
            available_evidence_kinds=(SegmentKind.TRANSCRIPT.value,),
            installed_models=(),  # 无 ASR
            has_profile=True,
        )
        assert rep.ready is False
        assert "qwen3_asr" in rep.missing_models

    def test_no_profile_blocks(self):
        rep = evaluate_prerequisites(
            OFFICIAL_SUBTITLE_DRAFT,
            available_evidence_kinds=(SegmentKind.TRANSCRIPT.value,),
            installed_models=("qwen3_asr",),
            has_profile=False,
        )
        assert rep.ready is False
        assert any(c.category == "permission" and not c.ok for c in rep.checks)

    def test_local_dev_unverified(self):
        m = SkillManifest(
            skill_id="test.dev",
            source=SkillSource.LOCAL_DEV,
            required_evidence_kinds=(SegmentKind.TRANSCRIPT,),
            risk=SkillRisk.PROPOSAL_ONLY,
        )
        rep = evaluate_prerequisites(
            m, available_evidence_kinds=(SegmentKind.TRANSCRIPT.value,),
            installed_models=("qwen3_asr",), has_profile=True,
        )
        assert rep.unverified is True
        assert rep.ready is False  # unverified -> not ready
        assert any(c.category == "source" and not c.ok for c in rep.checks)

    def test_product_ad_needs_shot_and_tag(self):
        rep = evaluate_prerequisites(
            OFFICIAL_PRODUCT_AD_30S,
            available_evidence_kinds=(SegmentKind.TRANSCRIPT.value,),
            installed_models=("qwen3_asr", "scene_cut"),
            has_profile=True,
        )
        # 缺 shot / tag
        assert rep.ready is False
        assert "shot" in rep.missing_evidence_kinds or "tag" in rep.missing_evidence_kinds

    def test_blocking_messages_readable(self):
        rep = evaluate_prerequisites(
            OFFICIAL_SUBTITLE_DRAFT,
            available_evidence_kinds=(), installed_models=(), has_profile=False,
        )
        msgs = rep.blocking_messages
        assert msgs
        # 每条都有"："分隔的 label:detail
        assert all("：" in m or ":" in m for m in msgs)

    def test_risk_label(self):
        assert risk_label_of(OFFICIAL_SUBTITLE_DRAFT) == "仅提议"


# ================================================================
# SkillCatalogView
# ================================================================


def _registry():
    from src.features.edit.ai_editing.skill_registry import SkillRegistry
    return SkillRegistry()


class TestSkillCatalogView:
    def test_cards_returns_all_official(self):
        view = SkillCatalogView(
            _registry(),
            available_evidence_kinds=(SegmentKind.TRANSCRIPT.value,),
            installed_models=("qwen3_asr",),
            has_profile=True,
        )
        cards = view.cards()
        ids = {c.skill_id for c in cards}
        assert ids == {"topos.subtitle_draft", "topos.voiceover_polish", "topos.product_ad_30s"}

    def test_card_unknown_returns_none(self):
        view = SkillCatalogView(_registry())
        assert view.card("nope") is None

    def test_card_has_form_and_prereq(self):
        view = SkillCatalogView(
            _registry(),
            available_evidence_kinds=(SegmentKind.TRANSCRIPT.value,),
            installed_models=("qwen3_asr",),
            has_profile=True,
        )
        c = view.card("topos.subtitle_draft")
        assert c is not None
        assert c.prereq is not None
        assert c.prereq.ready is True
        assert isinstance(c.form_fields, tuple)

    def test_recommend_ready_first(self):
        view = SkillCatalogView(
            _registry(),
            available_evidence_kinds=(SegmentKind.TRANSCRIPT.value,),
            installed_models=("qwen3_asr",),
            has_profile=True,
        )
        rec = view.recommend(limit=5)
        # product_ad 缺 shot/tag -> not ready；它应在 ready 之后
        ids = [c.skill_id for c in rec]
        ready_ids = [c.skill_id for c in rec if c.prereq and c.prereq.ready]
        not_ready_ids = [c.skill_id for c in rec if not (c.prereq and c.prereq.ready)]
        # ready 的都在 not_ready 之前
        for r in ready_ids:
            for nr in not_ready_ids:
                assert ids.index(r) < ids.index(nr)

    def test_recommend_limit(self):
        view = SkillCatalogView(_registry())
        assert len(view.recommend(limit=2)) == 2

    def test_card_unverified_flag(self):
        from src.features.edit.ai_editing.skill_manifest import SkillManifest
        from src.features.edit.ai_editing.skill_registry import SkillRegistry
        reg = SkillRegistry()
        # 注册一个 local_dev skill
        reg.register(SkillManifest(
            skill_id="local.test", source=SkillSource.LOCAL_DEV,
            risk=SkillRisk.PROPOSAL_ONLY, tool_policy=ToolPolicy(),
        ))
        view = SkillCatalogView(reg)
        c = view.card("local.test")
        assert c is not None
        assert c.unverified is True

    def test_card_meta_populated(self):
        view = SkillCatalogView(_registry())
        c = view.card("topos.subtitle_draft")
        assert c.category == "字幕"
        assert c.produces
        assert c.trigger_examples  # 有触发示例
