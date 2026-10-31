"""M9 Skill Registry 单元测试（§3.1 / §5.3 / §11.1 M9 step 2）。

覆盖：
- 默认注册官方 Skill
- register / disable / enable / remove
- 升级检查（仅更高版本号）
- content_hash 完整性自检（篡改 → 拒绝）
- forbidden capabilities 声明 → 拒绝
- tools_for 工具过滤（allowlist + permission ceiling）
- validate_tool_call 不符 → 诊断
- 版本比较 semver

红线对照：
- Skill 不拥有裸权限（§5.3 第一条，FORBIDDEN_CAPABILITIES 拒绝）
- 来源分级 + 未验证标识（§5.3 第二条，local_dev unverified）
- content_hash 完整性（§5.3 第三条，篡改拒绝）
- 最小权限且可见（§1.2 不变量 #4，未注册/禁用 → 空工具）
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.edit_core

from src.features.edit.ai_editing.editorial_models import SegmentKind
from src.features.edit.ai_editing.skill_manifest import (
    SkillManifest,
    SkillSource,
    ToolPolicy,
    compute_content_hash,
    with_signature,
)
from src.features.edit.ai_editing.skill_registry import (
    SkillRegistry,
    _version_is_higher,
)


# ================================================================
# 辅助
# ================================================================


def _make_manifest(
    skill_id: str = "test.skill", version: str = "1.0.0",
    *, source: SkillSource = SkillSource.VERIFIED_THIRD_PARTY,
    **overrides,
) -> SkillManifest:
    base = dict(
        skill_id=skill_id, version=version, title="Test",
        source=source,
        required_evidence_kinds=(SegmentKind.TRANSCRIPT,),
        produces_plan=True,
        tool_policy=ToolPolicy(
            allowed_tools=("topos.search_assets",), max_permission="read",
        ),
    )
    base.update(overrides)
    return with_signature(SkillManifest(**base), signed_by="tester")


class _FakeTool:
    """duck-typed ToolDefinition（有 .name / .permission）。"""

    def __init__(self, name: str, permission: str) -> None:
        self.name = name
        self.permission = permission


# ================================================================
# 默认注册 + 查询
# ================================================================


class TestRegistryBasics:
    def test_default_registers_official(self):
        reg = SkillRegistry()
        assert reg.count == 3
        assert reg.get("topos.voiceover_polish") is not None
        assert reg.get("topos.product_ad_30s") is not None
        assert reg.get("topos.subtitle_draft") is not None

    def test_get_unknown_returns_none(self):
        reg = SkillRegistry()
        assert reg.get("bogus") is None

    def test_require_unknown_raises(self):
        reg = SkillRegistry()
        with pytest.raises(KeyError):
            reg.require("bogus")

    def test_list_skills_sorted(self):
        reg = SkillRegistry()
        skills = reg.list_skills()
        ids = [m.skill_id for m in skills]
        assert ids == sorted(ids)

    def test_list_skills_excludes_disabled(self):
        reg = SkillRegistry()
        reg.disable("topos.voiceover_polish")
        skills = reg.list_skills()
        ids = {m.skill_id for m in skills}
        assert "topos.voiceover_polish" not in ids
        # include_disabled 含禁用
        all_ids = {m.skill_id for m in reg.list_skills(include_disabled=True)}
        assert "topos.voiceover_polish" in all_ids

    def test_is_enabled(self):
        reg = SkillRegistry()
        assert reg.is_enabled("topos.voiceover_polish") is True
        reg.disable("topos.voiceover_polish")
        assert reg.is_enabled("topos.voiceover_polish") is False
        assert reg.is_enabled("bogus") is False

    def test_no_official_register_option(self):
        reg = SkillRegistry(register_official=False)
        assert reg.count == 0


# ================================================================
# register / disable / enable / remove
# ================================================================


class TestRegisterDisable:
    def test_register_third_party_succeeds(self):
        reg = SkillRegistry(register_official=False)
        m = _make_manifest()
        diags = reg.register(m)
        assert diags == []
        assert reg.count == 1

    def test_register_local_dev_unverified_flag(self):
        reg = SkillRegistry(register_official=False)
        m = _make_manifest(source=SkillSource.LOCAL_DEV)
        reg.register(m)
        assert reg.get("test.skill").unverified is True

    def test_disable_then_enable(self):
        reg = SkillRegistry(register_official=False)
        reg.register(_make_manifest())
        assert reg.disable("test.skill") is True
        assert reg.is_enabled("test.skill") is False
        assert reg.disable("test.skill") is False  # 已禁用，幂等
        assert reg.enable("test.skill") is True
        assert reg.is_enabled("test.skill") is True

    def test_remove_third_party(self):
        reg = SkillRegistry(register_official=False)
        reg.register(_make_manifest())
        assert reg.remove("test.skill") is True
        assert reg.count == 0

    def test_cannot_remove_official(self):
        reg = SkillRegistry()
        assert reg.remove("topos.voiceover_polish") is False
        assert reg.count == 3


# ================================================================
# 升级检查
# ================================================================


class TestUpgrade:
    def test_higher_version_upgrades(self):
        reg = SkillRegistry(register_official=False)
        reg.register(_make_manifest(version="1.0.0"))
        diags = reg.register(_make_manifest(version="1.1.0"))
        assert diags == []
        assert reg.get("test.skill").version == "1.1.0"

    def test_lower_version_rejected(self):
        reg = SkillRegistry(register_official=False)
        reg.register(_make_manifest(version="1.2.0"))
        diags = reg.register(_make_manifest(version="1.1.0"))
        assert len(diags) == 1
        assert diags[0].is_error
        assert reg.get("test.skill").version == "1.2.0"  # 未变

    def test_same_version_rejected(self):
        reg = SkillRegistry(register_official=False)
        reg.register(_make_manifest(version="1.0.0"))
        diags = reg.register(_make_manifest(version="1.0.0"))
        assert len(diags) == 1
        assert diags[0].is_error


# ================================================================
# content_hash 完整性自检
# ================================================================


class TestContentHashIntegrity:
    def test_tampered_hash_rejected(self):
        """manifest 自带 signature 的 content_hash 与重算不一致 → 拒绝。"""
        from dataclasses import replace
        from src.features.edit.ai_editing.skill_manifest import SkillSignature
        reg = SkillRegistry(register_official=False)
        m = _make_manifest()
        # 篡改签名里的 hash
        tampered_sig = SkillSignature(
            content_hash="0" * 64,  # 假 hash
            algorithm=m.signature.algorithm, signed_by=m.signature.signed_by,
        )
        tampered = replace(m, signature=tampered_sig)
        diags = reg.register(tampered)
        assert len(diags) == 1
        assert diags[0].is_error
        assert reg.count == 0

    def test_unsigned_manifest_accepted(self):
        """无 signature 的 manifest 不做完整性自检（信任调用方）。"""
        reg = SkillRegistry(register_official=False)
        m = SkillManifest(
            skill_id="unsigned.skill", version="1.0.0",
            required_evidence_kinds=(SegmentKind.TRANSCRIPT,),
            tool_policy=ToolPolicy(max_permission="read"),
        )
        diags = reg.register(m)
        assert diags == []
        assert reg.count == 1


# ================================================================
# forbidden capabilities
# ================================================================


class TestForbiddenCapabilities:
    def test_register_skill_with_forbidden_tool_rejected(self):
        """红线：Skill 声明 exec/shell/fs 能力 → 拒绝（§5.3 第一条）。"""
        reg = SkillRegistry(register_official=False)
        m = _make_manifest(
            tool_policy=ToolPolicy(
                allowed_tools=("topos.search_assets", "exec_python"),
                max_permission="read",
            ),
        )
        diags = reg.register(m)
        assert len(diags) == 1
        assert diags[0].code.value == "TAEP_PERMISSION_DENIED"
        assert reg.count == 0


# ================================================================
# tools_for 工具过滤
# ================================================================


class TestToolsFor:
    def test_filters_by_allowlist_and_ceiling(self):
        reg = SkillRegistry(register_official=False)
        reg.register(_make_manifest(
            tool_policy=ToolPolicy(
                allowed_tools=("topos.search_assets", "topos.get_capabilities"),
                max_permission="read",
            ),
        ))
        all_tools = [
            _FakeTool("topos.search_assets", "read"),
            _FakeTool("topos.get_capabilities", "read"),
            _FakeTool("topos.validate_edit_plan", "preview"),  # 超 ceiling
            _FakeTool("topos.apply_edit_plan", "write"),  # 不在 allowlist
        ]
        visible = reg.tools_for("test.skill", all_tools)
        names = {t.name for t in visible}
        assert names == {"topos.search_assets", "topos.get_capabilities"}

    def test_disabled_skill_returns_empty(self):
        """最小权限：禁用的 Skill 不可见任何工具（§1.2 不变量 #4）。"""
        reg = SkillRegistry(register_official=False)
        reg.register(_make_manifest())
        reg.disable("test.skill")
        visible = reg.tools_for("test.skill", [_FakeTool("topos.search_assets", "read")])
        assert visible == []

    def test_unregistered_skill_returns_empty(self):
        reg = SkillRegistry(register_official=False)
        visible = reg.tools_for("bogus", [_FakeTool("topos.search_assets", "read")])
        assert visible == []


# ================================================================
# validate_tool_call
# ================================================================


class TestValidateToolCall:
    def test_allowed_call_passes(self):
        reg = SkillRegistry(register_official=False)
        reg.register(_make_manifest(
            tool_policy=ToolPolicy(
                allowed_tools=("topos.search_assets",), max_permission="read",
            ),
        ))
        diags = reg.validate_tool_call("test.skill", "topos.search_assets", "read")
        assert diags == []

    def test_disallowed_call_returns_diagnostic(self):
        reg = SkillRegistry(register_official=False)
        reg.register(_make_manifest(
            tool_policy=ToolPolicy(
                allowed_tools=("topos.search_assets",), max_permission="read",
            ),
        ))
        diags = reg.validate_tool_call("test.skill", "topos.apply_edit_plan", "write")
        assert len(diags) == 1
        assert diags[0].is_error

    def test_disabled_skill_denied(self):
        reg = SkillRegistry(register_official=False)
        reg.register(_make_manifest())
        reg.disable("test.skill")
        diags = reg.validate_tool_call("test.skill", "topos.search_assets", "read")
        assert len(diags) == 1
        assert diags[0].is_error


# ================================================================
# 版本比较
# ================================================================


class TestVersionCompare:
    def test_higher_minor(self):
        assert _version_is_higher("1.1.0", "1.0.0") is True

    def test_higher_major(self):
        assert _version_is_higher("2.0.0", "1.9.9") is True

    def test_lower(self):
        assert _version_is_higher("1.0.0", "1.0.1") is False

    def test_equal(self):
        assert _version_is_higher("1.0.0", "1.0.0") is False

    def test_different_length(self):
        assert _version_is_higher("1.0", "1.0.0") is False  # 等价
        assert _version_is_higher("1.0.1", "1.0") is True
