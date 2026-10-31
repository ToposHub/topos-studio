"""P4.1 测试：开发者 Skill Studio —— 版本化草稿（plan §10.6）。

验证 SkillDraft/SkillVersion/SkillStudioStore 的核心契约：

- 从官方模板 / 现有 WorkflowDefinition 创建版本化草稿；
- 草稿携带：输入/输出 schema、风险、工具 allowlist、模型/资源要求、
  默认 policy、UI card、许可证；
- 草稿版本号单调；不同内容 → 不同 content hash；
- local_dev 草稿必须 unverified=True；
- 草稿被显式 publish 之前不得进入 SkillRegistry（不污染生产表）。

红线（plan §10.6 / §1.4）：
- 第三方 / local_dev 不得执行任意 Python/shell/filesystem/UI 自动化
  （SkillDraft.executor_kind 必须是受控枚举，禁止 ``code_exec``）。
"""
from __future__ import annotations

import pytest

from src.features.ai_harness.skill_studio_drafts import (
    DRAFT_MODEL_REQUIREMENTS,
    DraftTemplateOrigin,
    ExecutorKind,
    ModelRequirement,
    SkillDraft,
    SkillDraftVersion,
    SkillStudioStore,
    new_draft_from_template,
    new_draft_from_workflow,
)
from src.features.edit.ai_editing.skill_manifest import (
    OFFICIAL_VOICEOVER_POLISH,
    SkillRisk,
    SkillSource,
)


# ---------------------------------------------------------------------- #
# ExecutorKind 安全红线
# ---------------------------------------------------------------------- #


class TestExecutorKindSafety:
    def test_forbidden_kinds_not_in_enum(self) -> None:
        """红线：exec / shell / fs / ui_automation 不得出现在受控枚举里。"""
        all_kinds = {k.value for k in ExecutorKind}
        forbidden = {"python_exec", "shell_exec", "code_exec", "fs_write", "ui_automation"}
        assert not (forbidden & all_kinds), f"forbidden exec kinds leaked: {forbidden & all_kinds}"

    def test_is_arbitrary_code_returns_false_for_all_kinds(self) -> None:
        for kind in ExecutorKind:
            assert not kind.is_arbitrary_code, f"{kind} must not be arbitrary-code-executable"

    def test_kind_for_workflow_returns_declared(self) -> None:
        assert ExecutorKind.kind_for_workflow({}).value in {
            "workflow_runner", "taskgraph_roles", "editorial_builder",
        }


# ---------------------------------------------------------------------- #
# DraftTemplateOrigin
# ---------------------------------------------------------------------- #


class TestTemplateOrigin:
    def test_official_template_origin(self) -> None:
        origin = DraftTemplateOrigin.official_template("topos.auto_caption")
        assert origin.kind == "official_template"
        assert origin.template_id == "topos.auto_caption"

    def test_workflow_definition_origin(self) -> None:
        origin = DraftTemplateOrigin.workflow_definition("wf-custom-001")
        assert origin.kind == "workflow_definition"
        assert origin.workflow_id == "wf-custom-001"

    def test_origin_serialization_roundtrip(self) -> None:
        origin = DraftTemplateOrigin.official_template("topos.voiceover_polish")
        d = origin.to_dict()
        assert d["kind"] == "official_template"
        assert d["template_id"] == "topos.voiceover_polish"


# ---------------------------------------------------------------------- #
# SkillDraftVersion
# ---------------------------------------------------------------------- #


class TestSkillDraftVersion:
    def test_first_version_is_v0_1_0(self) -> None:
        v = SkillDraftVersion.first()
        assert str(v) == "0.1.0"
        assert v.is_dev  # 开发阶段

    def test_bump_patch_increments(self) -> None:
        v = SkillDraftVersion.first()
        v2 = v.bump_patch()
        assert str(v2) == "0.1.1"

    def test_bump_minor_resets_patch(self) -> None:
        v = SkillDraftVersion.from_string("0.1.3")
        v2 = v.bump_minor()
        assert str(v2) == "0.2.0"

    def test_bump_major_resets_minor_and_patch(self) -> None:
        v = SkillDraftVersion.from_string("0.3.2")
        v2 = v.bump_major()
        assert str(v2) == "1.0.0"
        assert not v2.is_dev  # 1.x 进入候选发布

    def test_invalid_version_string_rejected(self) -> None:
        with pytest.raises(ValueError):
            SkillDraftVersion.from_string("not-a-version")


# ---------------------------------------------------------------------- #
# SkillDraft 基本字段
# ---------------------------------------------------------------------- #


def _make_minimal_draft() -> SkillDraft:
    return new_draft_from_template(
        author="dev-alice",
        template=OFFICIAL_VOICEOVER_POLISH,
        title="我的口播精剪副本",
        description="基于官方模板调整",
    )


class TestSkillDraftBasics:
    def test_draft_id_unique(self) -> None:
        d1 = _make_minimal_draft()
        d2 = _make_minimal_draft()
        assert d1.draft_id != d2.draft_id

    def test_draft_inherits_manifest_fields(self) -> None:
        d = _make_minimal_draft()
        # 输入/输出 schema、风险、工具 allowlist 都从模板继承
        assert d.manifest.input_schema == OFFICIAL_VOICEOVER_POLISH.input_schema
        assert d.manifest.tool_policy.allowed_tools == OFFICIAL_VOICEOVER_POLISH.tool_policy.allowed_tools
        assert d.manifest.risk == OFFICIAL_VOICEOVER_POLISH.risk

    def test_local_dev_source_unverified(self) -> None:
        """红线：草稿 source=local_dev 必须标 unverified。"""
        d = _make_minimal_draft()
        assert d.manifest.source == SkillSource.LOCAL_DEV
        assert d.manifest.unverified is True

    def test_draft_has_distinct_skill_id_from_template(self) -> None:
        d = _make_minimal_draft()
        # 草稿的 skill_id 必须区别于官方模板（避免误覆盖生产 skill）
        assert d.manifest.skill_id != OFFICIAL_VOICEOVER_POLISH.skill_id
        assert d.manifest.skill_id.startswith("draft.")

    def test_first_version_is_dev(self) -> None:
        d = _make_minimal_draft()
        assert d.version.is_dev

    def test_template_origin_recorded(self) -> None:
        d = _make_minimal_draft()
        assert d.origin.kind == "official_template"
        assert d.origin.template_id == OFFICIAL_VOICEOVER_POLISH.skill_id


# ---------------------------------------------------------------------- #
# 模型 / 资源要求
# ---------------------------------------------------------------------- #


class TestModelRequirements:
    def test_default_model_requirements_template(self) -> None:
        mr = DRAFT_MODEL_REQUIREMENTS["auto_caption"]
        caps = {m.capability for m in mr}
        assert "asr" in caps
        asr = next(m for m in mr if m.capability == "asr")
        assert asr.required is True

    def test_executor_kind_workflow_runner_default(self) -> None:
        d = _make_minimal_draft()
        # 官方模板 → workflow_runner
        assert d.executor_kind == ExecutorKind.WORKFLOW_RUNNER

    def test_executor_kind_arbitrary_code_rejected(self) -> None:
        """红线：ExecutorKind 构造期就拒绝任意代码执行后端。"""
        # 直接构造非法 kind → ValueError
        for forbidden in ("python_exec", "shell_exec", "code_exec", "fs_write", "ui_automation"):
            with pytest.raises(ValueError, match="forbidden|executor|arbitrary"):
                ExecutorKind(forbidden)
        # 所有合法成员 is_arbitrary_code 恒为 False
        for kind in ExecutorKind:
            assert not kind.is_arbitrary_code


# ---------------------------------------------------------------------- #
# SkillDraft 内容变更 → 版本/hash 变化
# ---------------------------------------------------------------------- #


class TestDraftMutation:
    def test_update_creates_new_version(self) -> None:
        d = _make_minimal_draft()
        old_v = str(d.version)
        d2 = d.update_field("title", "改名后的精剪", kind="patch")
        assert str(d2.version) != old_v
        assert d2.manifest.title == "改名后的精剪"
        # 旧草稿版本未变（不可变）
        assert d.manifest.title == "我的口播精剪副本"

    def test_content_hash_changes_on_update(self) -> None:
        d = _make_minimal_draft()
        d2 = d.update_field("description", "全新的描述", kind="patch")
        assert d.manifest.content_hash != d2.manifest.content_hash

    def test_update_invalid_kind_rejected(self) -> None:
        d = _make_minimal_draft()
        with pytest.raises(ValueError):
            d.update_field("title", "x", kind="bogus")

    def test_update_field_must_be_known(self) -> None:
        d = _make_minimal_draft()
        with pytest.raises(KeyError):
            d.update_field("nonexistent_field", "x", kind="patch")


# ---------------------------------------------------------------------- #
# from WorkflowDefinition
# ---------------------------------------------------------------------- #


class TestDraftFromWorkflow:
    def test_new_draft_from_workflow_records_origin(self) -> None:
        wf_def = {
            "workflow_id": "wf-my-voiceover",
            "name": "我的配音流程",
            "nodes": [{"node_id": "n1", "spec_id": "topos.tts.qwen3"}],
            "policy": {"network": "ask", "timeline_write": "approval_required"},
        }
        d = new_draft_from_workflow(
            author="dev-bob",
            workflow_definition=wf_def,
            title="自定义配音 Skill",
        )
        assert d.origin.kind == "workflow_definition"
        assert d.origin.workflow_id == "wf-my-voiceover"
        assert d.manifest.skill_id.startswith("draft.")
        assert d.manifest.source == SkillSource.LOCAL_DEV
        assert d.manifest.unverified is True

    def test_workflow_with_declared_model_requirements(self) -> None:
        wf_def = {
            "workflow_id": "wf-tts-only",
            "name": "TTS 流程",
            "nodes": [{"node_id": "n1", "spec_id": "topos.tts.qwen3"}],
        }
        d = new_draft_from_workflow(
            author="dev-bob",
            workflow_definition=wf_def,
            title="TTS Skill",
            model_requirements=(ModelRequirement(capability="tts", required=True),),
        )
        assert any(m.capability == "tts" for m in d.model_requirements)


# ---------------------------------------------------------------------- #
# SkillStudioStore
# ---------------------------------------------------------------------- #


class TestSkillStudioStore:
    def test_save_and_get_draft(self) -> None:
        store = SkillStudioStore()
        d = _make_minimal_draft()
        store.save(d)
        got = store.get(d.draft_id)
        assert got is d  # 同一对象引用（in-memory）

    def test_list_drafts_only_local_author(self) -> None:
        store = SkillStudioStore()
        d1 = _make_minimal_draft()
        d2 = new_draft_from_template(
            author="dev-bob",
            template=OFFICIAL_VOICEOVER_POLISH,
            title="bob 的副本",
        )
        store.save(d1)
        store.save(d2)
        mine = store.list_drafts(author="dev-alice")
        assert d1 in mine
        assert d2 not in mine

    def test_delete_draft(self) -> None:
        store = SkillStudioStore()
        d = _make_minimal_draft()
        store.save(d)
        assert store.delete(d.draft_id) is True
        assert store.get(d.draft_id) is None

    def test_delete_nonexistent_returns_false(self) -> None:
        store = SkillStudioStore()
        assert store.delete("nonexistent-id") is False

    def test_drafts_isolated_from_registry(self) -> None:
        """红线：草稿不得自动进入生产 SkillRegistry。"""
        store = SkillStudioStore()
        d = _make_minimal_draft()
        store.save(d)
        # store 不应内置对 SkillRegistry 的引用 / 注册副作用
        assert not hasattr(store, "_registry") or store._registry is None


# ---------------------------------------------------------------------- #
# 风险升级红线
# ---------------------------------------------------------------------- #


class TestRiskEscalation:
    def test_local_dev_cannot_escalate_to_full_write(self) -> None:
        """红线：local_dev 不得声明 write 权限。"""
        d = _make_minimal_draft()
        # 写权限 skill 风险升级必须保持每次审批
        assert d.manifest.risk in (SkillRisk.PROPOSAL_ONLY, SkillRisk.READ_ONLY)
