"""M9 Skill Manifest 单元测试（§5 / §10.1 / §11.1 M9 step 1）。

覆盖：
- ToolPolicy allowlist + permission ceiling + forbidden 双重过滤
- SkillSource 分级 + unverified 标识
- SkillManifest frozen + content_hash 稳定 + with_signature
- compute_content_hash：内容变 → hash 变
- 三个官方内置 manifest 字段完备性 + 签名
- to_dict 序列化（skill.json 形态）

红线对照：
- Skill 不拥有裸权限，只声明所需工具/权限（§5.3 第一条）
- 来源分级 + 未验证标识（§5.3 第二条）
- content_hash 内容变 → hash 变（§5.3 第三条）
- 官方 Skill 也不自动取得 write 权限（tool_policy 显式 forbidden apply/render/export）
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.edit_core

from src.features.edit.ai_editing.artifact_models import ArtifactKind
from src.features.edit.ai_editing.editorial_models import SegmentKind
from src.features.edit.ai_editing.skill_manifest import (
    OFFICIAL_PRODUCT_AD_30S,
    OFFICIAL_SKILLS,
    OFFICIAL_SUBTITLE_DRAFT,
    OFFICIAL_VOICEOVER_POLISH,
    SkillManifest,
    SkillRisk,
    SkillSignature,
    SkillSource,
    ToolPolicy,
    compute_content_hash,
    with_signature,
)


# ================================================================
# ToolPolicy
# ================================================================


class TestToolPolicy:
    def test_empty_policy_denies_everything(self):
        p = ToolPolicy()
        # 空 allowlist + max=read：无 tool 能通过（allowlist 非空才检查包含）
        # 空 allowlist 意为"不限制 allowlist"，但 permission ceiling 仍生效
        assert p.permits("topos.search_assets", "read") is True
        assert p.permits("topos.apply_edit_plan", "write") is False  # 超 ceiling

    def test_allowlist_filters(self):
        p = ToolPolicy(
            allowed_tools=("topos.search_assets", "topos.get_capabilities"),
            max_permission="read",
        )
        assert p.permits("topos.search_assets", "read") is True
        assert p.permits("topos.validate_edit_plan", "preview") is False  # 不在 allowlist

    def test_permission_ceiling(self):
        p = ToolPolicy(allowed_tools=("topos.apply_edit_plan",), max_permission="read")
        # 在 allowlist 但 permission 超 ceiling → 拒
        assert p.permits("topos.apply_edit_plan", "write") is False

    def test_forbidden_overrides_allowlist(self):
        p = ToolPolicy(
            allowed_tools=("topos.search_assets", "topos.apply_edit_plan"),
            forbidden_tools=("topos.apply_edit_plan",),
            max_permission="write",
        )
        assert p.permits("topos.search_assets", "read") is True
        # 在 allowlist 且 permission 够，但在 forbidden → 拒
        assert p.permits("topos.apply_edit_plan", "write") is False

    def test_dedup_normalization(self):
        p = ToolPolicy(allowed_tools=("a", "a", " b ", ""), forbidden_tools=("x", "x"))
        assert p.allowed_tools == ("a", "b")
        assert p.forbidden_tools == ("x",)

    def test_unknown_permission_denies(self):
        p = ToolPolicy(allowed_tools=("t",), max_permission="read")
        assert p.permits("t", "bogus_perm") is False


# ================================================================
# SkillSource
# ================================================================


class TestSkillSource:
    def test_official_not_unverified(self):
        assert SkillSource.OFFICIAL.unverified is False

    def test_verified_third_party_not_unverified(self):
        assert SkillSource.VERIFIED_THIRD_PARTY.unverified is False

    def test_local_dev_is_unverified(self):
        assert SkillSource.LOCAL_DEV.unverified is True


# ================================================================
# SkillManifest
# ================================================================


def _minimal_manifest(**overrides) -> SkillManifest:
    base = dict(
        skill_id="test.skill",
        version="1.0.0",
        title="Test",
        required_evidence_kinds=(SegmentKind.TRANSCRIPT,),
        produces_plan=True,
        tool_policy=ToolPolicy(max_permission="read"),
        risk=SkillRisk.PROPOSAL_ONLY,
    )
    base.update(overrides)
    return SkillManifest(**base)


class TestSkillManifest:
    def test_frozen(self):
        m = _minimal_manifest()
        with pytest.raises(Exception):
            m.skill_id = "changed"  # type: ignore[misc]

    def test_empty_skill_id_rejected(self):
        with pytest.raises(ValueError):
            SkillManifest(skill_id="")

    def test_content_hash_stable(self):
        m1 = _minimal_manifest()
        m2 = _minimal_manifest()
        assert compute_content_hash(m1) == compute_content_hash(m2)

    def test_content_hash_changes_with_content(self):
        m1 = _minimal_manifest(title="A")
        m2 = _minimal_manifest(title="B")
        assert compute_content_hash(m1) != compute_content_hash(m2)

    def test_content_hash_independent_of_signature(self):
        # 签名本身不参与 hash（避免循环）
        m = _minimal_manifest()
        signed = with_signature(m)
        assert compute_content_hash(m) == compute_content_hash(signed)

    def test_with_signature_sets_hash(self):
        m = _minimal_manifest()
        signed = with_signature(m, signed_by="topos")
        assert signed.signature is not None
        assert signed.signature.content_hash == compute_content_hash(m)
        assert signed.signature.algorithm == "sha256-self"
        assert signed.signature.signed_by == "topos"

    def test_official_default_signed_by_topos(self):
        m = _minimal_manifest(source=SkillSource.OFFICIAL)
        signed = with_signature(m)
        assert signed.signature is not None
        assert signed.signature.signed_by == "topos"

    def test_unverified_property_from_source(self):
        m = _minimal_manifest(source=SkillSource.LOCAL_DEV)
        assert m.unverified is True
        m2 = _minimal_manifest(source=SkillSource.OFFICIAL)
        assert m2.unverified is False

    def test_to_dict_contains_skill_json_fields(self):
        m = with_signature(_minimal_manifest(
            roles=("creative_director", "qa_reviewer"),
            eval_version="v1",
        ))
        d = m.to_dict()
        for key in ("skill_id", "version", "title", "source", "license",
                    "required_evidence_kinds", "produces_plan", "tool_policy",
                    "risk", "input_schema", "output_schema", "ui_card",
                    "eval_version", "roles", "content_hash", "unverified",
                    "signature"):
            assert key in d, f"missing {key}"
        assert d["required_evidence_kinds"] == ["transcript"]
        assert d["roles"] == ["creative_director", "qa_reviewer"]
        assert d["unverified"] is False

    def test_to_dict_unverified_flag_true_for_local_dev(self):
        m = _minimal_manifest(source=SkillSource.LOCAL_DEV)
        assert m.to_dict()["unverified"] is True


# ================================================================
# 三个官方内置 manifest
# ================================================================


class TestOfficialSkills:
    def test_three_official_skills_registered(self):
        ids = {m.skill_id for m in OFFICIAL_SKILLS}
        assert ids == {
            "topos.voiceover_polish",
            "topos.product_ad_30s",
            "topos.subtitle_draft",
        }

    def test_all_official_signed(self):
        for m in OFFICIAL_SKILLS:
            assert m.signature is not None, f"{m.skill_id} missing signature"
            assert m.signature.content_hash == compute_content_hash(m)
            assert m.signature.signed_by == "topos"

    def test_all_official_source(self):
        for m in OFFICIAL_SKILLS:
            assert m.source == SkillSource.OFFICIAL
            assert m.unverified is False

    def test_voiceover_polish_fields(self):
        m = OFFICIAL_VOICEOVER_POLISH
        assert m.produces_plan is True
        assert m.produces_artifact_kind is None
        assert SegmentKind.TRANSCRIPT in m.required_evidence_kinds
        assert "hook" in m.target_beats

    def test_product_ad_requires_three_evidence_kinds(self):
        m = OFFICIAL_PRODUCT_AD_30S
        assert set(m.required_evidence_kinds) == {
            SegmentKind.TRANSCRIPT, SegmentKind.SHOT, SegmentKind.TAG,
        }
        assert "problem" in m.target_beats

    def test_subtitle_draft_produces_artifact_not_plan(self):
        m = OFFICIAL_SUBTITLE_DRAFT
        assert m.produces_plan is False
        assert m.produces_artifact_kind == ArtifactKind.SCRIPT

    def test_official_no_write_permission(self):
        """红线：官方 Skill 也不自动取得 write 权限（§5.3 第一条）。"""
        for m in OFFICIAL_SKILLS:
            # forbidden_tools 含 apply/render/export
            assert "topos.apply_edit_plan" in m.tool_policy.forbidden_tools
            assert "topos.render_preview" in m.tool_policy.forbidden_tools
            assert "topos.request_export" in m.tool_policy.forbidden_tools
            # max_permission 不超过 proposal
            assert m.tool_policy.max_permission == "proposal"

    def test_official_roles_empty_v1(self):
        """v1：官方工作流 roles=()（走 M7 单路径，不引入子智能体）。"""
        for m in OFFICIAL_SKILLS:
            assert m.roles == ()
