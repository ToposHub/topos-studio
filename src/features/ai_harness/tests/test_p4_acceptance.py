"""P4.6 P4 红线集成验收（plan §10.6 / §10.9）。

本测试把 P4.1–P4.5 串成一条端到端流水线，验证 plan §10.6 的全部完成定义：

> 从官方模板或现有 WorkflowDefinition 创建版本化草稿；声明输入/输出 schema、
> 风险、工具 allowlist、模型/资源要求、默认 policy、UI card 和许可证。
> 运行 dry-run：用 fake provider、固定 evidence fixture、固定 timeline snapshot
> 生成 RunRecord，检查端口、DAG、预算、权限、缓存 key、plan diff。
> 为每个 Skill 编写 eval：成功、模型缺失、证据不足、拒绝上传、取消、
> revision stale、无 approval 六类最低用例；发布前必须通过。
> 发布产物是 SkillDefinition + graph template + manifest + eval version +
> content hash。本地开发 Skill 醒目标"未验证"；第三方不得执行任意 Python、
> shell、文件系统或 UI 自动化。

同时验证 plan §10.9 验收矩阵的"开发者（Skill Studio）"列：发布的 auto_caption
Skill 与官方 auto_caption 共享同样的 manifest schema / executor_kind 家族 /
PREVIEW-only 产物结构。

红线汇总（本测试一网打尽）：

1. **不存 secret / 路径**（plan §1.4）：发布产物 to_dict 不含绝对路径 / openai key；
2. **local_dev 永远 unverified**（plan §10.6）：发布前后 source 不变；
3. **第三方不执行任意代码**（plan §10.6）：executor_kind 受控，registry 拒绝
   FORBIDDEN_CAPABILITIES；
4. **dry-run 不写时间线**（plan §1.4）：终态 RunRecord 无 approval/commit；
5. **eval 必须全 PASS 才能发布**（plan §10.6）。
"""
from __future__ import annotations


from src.features.ai_harness.editorial_skills import p2_official_manifests
from src.features.ai_harness.skill_dry_run import (
    DryRunCheckCode,
    SkillDryRunner,
    make_auto_caption_fixture,
)
from src.features.ai_harness.skill_eval_suite import run_default_suite
from src.features.ai_harness.skill_release import (
    PublishDenyCode,
    SkillReleaseGate,
)
from src.features.ai_harness.skill_studio_drafts import (
    ExecutorKind,
    SkillStudioStore,
    new_draft_from_template,
    new_draft_from_workflow,
)
from src.features.edit.ai_editing.skill_manifest import (
    OFFICIAL_VOICEOVER_POLISH,
    SkillManifest,
    SkillSource,
)
from src.features.edit.ai_editing.skill_registry import SkillRegistry


# ---------------------------------------------------------------------- #
# 端到端流水线：template → draft → dry-run → eval → publish → register
# ---------------------------------------------------------------------- #


class TestEndToEndPipeline:
    def test_full_pipeline_official_template_source(self) -> None:
        """红线：从官方模板创建 → dry-run → eval → 发布 → 注册，全链路一致。"""
        # 1. 从官方模板创建草稿
        store = SkillStudioStore()
        draft = new_draft_from_template(
            author="dev-alice",
            template=OFFICIAL_VOICEOVER_POLISH,
            title="我的口播精剪",
            description="基于官方模板的副本",
        )
        store.save(draft)
        assert draft.manifest.source == SkillSource.LOCAL_DEV
        assert draft.unverified is True

        # 2. dry-run（fake provider + 固定 fixture）
        from src.features.ai_harness.skill_dry_run import make_voiceover_polish_fixture
        runner = SkillDryRunner()
        outcome = runner.run(draft, make_voiceover_polish_fixture())
        assert outcome.success
        assert outcome.edit_plan is not None

        # 3. eval 六类用例
        report = run_default_suite(draft)
        assert report.passed, f"eval failures: {[r.name for r in report.failures()]}"

        # 4. 发布（bump 到 1.0.0）
        gate = SkillReleaseGate()
        ready = draft.update_field("title", "口播精剪 v1", kind="major")
        result = gate.publish(ready, released_by="dev-alice")
        assert result.success
        release = result.release
        assert release is not None
        assert release.unverified is True  # 红线：仍 unverified

        # 5. 注册进 SkillRegistry
        registry = SkillRegistry(register_official=False)
        diags = gate.register_into(registry, release)
        assert not any(_is_error(d) for d in diags)
        got = registry.get(release.skill_id)
        assert got is not None
        assert got.source == SkillSource.LOCAL_DEV
        assert got.unverified is True

    def test_full_pipeline_workflow_definition_source(self) -> None:
        """从现有 WorkflowDefinition 创建 → 同一发布流水线。"""
        from src.features.ai_harness.skill_studio_drafts import ModelRequirement
        from src.features.edit.ai_editing.editorial_models import SegmentKind
        wf = {
            "workflow_id": "wf-e2e-001",
            "name": "端到端 workflow",
            "nodes": [
                {"node_id": "input", "spec_id": "topos.media.selected_assets",
                 "params": {"media_kind": "video"}},
                {"node_id": "asr", "spec_id": "topos.analysis.transcribe_auto"},
            ],
            "edges": [
                {"from": {"node_id": "input", "port": "assets"},
                 "to": {"node_id": "asr", "port": "assets"}},
            ],
            "policy": {"network": "ask", "timeline_write": "approval_required"},
        }
        draft = new_draft_from_workflow(
            author="dev-bob", workflow_definition=wf, title="E2E workflow Skill",
            # 显式声明模型需求 + 证据类型，使 eval 的 model_missing /
            # evidence_insufficient 用例可触发（与官方 auto_caption 同族）
            model_requirements=(
                ModelRequirement(capability="asr", required=True),
                ModelRequirement(capability="llm", required=True),
            ),
        )
        # 补 required_evidence_kinds（new_draft_from_workflow 不自动推断）
        draft = draft.update_field(
            "required_evidence_kinds", (SegmentKind.TRANSCRIPT,), kind="patch",
        )
        assert draft.origin.kind == "workflow_definition"
        # dry-run DAG 检查
        runner = SkillDryRunner()
        outcome = runner.run(draft, make_auto_caption_fixture())
        assert any(c.code == DryRunCheckCode.DAG_ACYCLIC for c in outcome.checks)
        # 发布
        ready = draft.update_field("title", "E2E v1", kind="major")
        result = SkillReleaseGate().publish(ready, released_by="dev-bob")
        assert result.success
        assert result.release.graph_template == wf


# ---------------------------------------------------------------------- #
# 红线 1：发布产物不含 secret / 路径
# ---------------------------------------------------------------------- #


class TestNoSecretsOrPaths:
    def test_published_release_no_secrets(self) -> None:
        draft = new_draft_from_template(
            author="dev-alice", template=OFFICIAL_VOICEOVER_POLISH, title="T",
        )
        ready = draft.update_field("title", "T v1", kind="major")
        result = SkillReleaseGate().publish(ready, released_by="dev-alice")
        assert result.success
        text = repr(result.release.to_dict())
        assert "/Users/" not in text
        assert "sk-" not in text
        assert "password" not in text.lower()
        assert "api_key" not in text.lower()

    def test_published_release_no_real_asset_paths(self) -> None:
        """发布产物 to_dict 不含真实素材路径。"""
        draft = new_draft_from_template(
            author="dev-alice", template=OFFICIAL_VOICEOVER_POLISH, title="T",
        )
        ready = draft.update_field("title", "T v1", kind="major")
        result = SkillReleaseGate().publish(ready, released_by="dev-alice")
        d = result.release.to_dict()
        # manifest.brief_template / input_schema / 各字段都不含绝对路径
        for key in ("manifest", "graph_template"):
            sub = repr(d.get(key, {}))
            assert "/Users/" not in sub
            assert "C:\\\\Users" not in sub


# ---------------------------------------------------------------------- #
# 红线 2：local_dev 永远 unverified（发布前后）
# ---------------------------------------------------------------------- #


class TestLocalDevUnverifiedInvariant:
    def test_unverified_throughout_pipeline(self) -> None:
        draft = new_draft_from_template(
            author="dev-alice", template=OFFICIAL_VOICEOVER_POLISH, title="T",
        )
        # 草稿期
        assert draft.unverified is True
        ready = draft.update_field("title", "T v1", kind="major")
        # 发布期
        result = SkillReleaseGate().publish(ready, released_by="x")
        assert result.release.unverified is True
        # 注册期
        registry = SkillRegistry(register_official=False)
        SkillReleaseGate().register_into(registry, result.release)
        got = registry.get(result.release.skill_id)
        assert got.unverified is True

    def test_cannot_become_official_via_publish(self) -> None:
        draft = new_draft_from_template(
            author="third-party", template=OFFICIAL_VOICEOVER_POLISH, title="T",
        )
        ready = draft.update_field("title", "T v1", kind="major")
        result = SkillReleaseGate().publish(ready, released_by="third-party")
        assert result.release.manifest.source == SkillSource.LOCAL_DEV
        assert result.release.manifest.source != SkillSource.OFFICIAL


# ---------------------------------------------------------------------- #
# 红线 3：第三方不执行任意代码（端到端）
# ---------------------------------------------------------------------- #


class TestNoArbitraryCodeE2E:
    def test_published_skill_executor_kind_safe(self) -> None:
        draft = new_draft_from_template(
            author="third-party", template=OFFICIAL_VOICEOVER_POLISH, title="T",
        )
        ready = draft.update_field("title", "T v1", kind="major")
        result = SkillReleaseGate().publish(ready, released_by="third-party")
        assert not result.release.executor_kind.is_arbitrary_code

    def test_registry_rejects_skill_declaring_exec(self) -> None:
        registry = SkillRegistry(register_official=False)
        malicious = SkillManifest(
            skill_id="thirdparty.evil",
            version="1.0.0",
            title="恶意第三方",
            source=SkillSource.LOCAL_DEV,
            tool_policy=__import__(
                "src.features.edit.ai_editing.skill_manifest", fromlist=["ToolPolicy"]
            ).ToolPolicy(allowed_tools=("exec_python", "call_shell")),
        )
        diags = registry.register(malicious)
        assert any(_is_error(d) for d in diags)
        assert registry.get(malicious.skill_id) is None


# ---------------------------------------------------------------------- #
# 红线 4：dry-run 不写时间线
# ---------------------------------------------------------------------- #


class TestDryRunNoTimelineWrite:
    def test_dry_run_outcome_no_commit_events(self) -> None:
        draft = new_draft_from_template(
            author="dev-alice", template=OFFICIAL_VOICEOVER_POLISH, title="T",
        )
        from src.features.ai_harness.skill_dry_run import make_voiceover_polish_fixture
        outcome = SkillDryRunner().run(draft, make_voiceover_polish_fixture())
        kinds = {e.kind for e in outcome.run_record.events}
        assert "approval.granted" not in kinds
        assert "approval.consumed" not in kinds
        assert "timeline_committed" not in kinds

    def test_published_skill_dry_run_still_preview_only(self) -> None:
        """发布后的 Skill 再次 dry-run 仍是 PREVIEW。"""
        draft = new_draft_from_template(
            author="dev-alice", template=OFFICIAL_VOICEOVER_POLISH, title="T",
        )
        ready = draft.update_field("title", "T v1", kind="major")
        SkillReleaseGate().publish(ready, released_by="x")
        # 再次 dry-run（用同草稿）
        from src.features.ai_harness.skill_dry_run import make_voiceover_polish_fixture
        outcome = SkillDryRunner().run(ready, make_voiceover_polish_fixture())
        # 无条件断言（AUD-22701）：edit_plan 变 None（dry-run 失败路径）时本
        # 红线测试必须失败，不能让 if 包裹导致空转通过。
        assert outcome.edit_plan is not None, \
            "发布后 dry-run 必须仍产出 EditPlan（失败路径不得静默通过红线）"
        assert outcome.edit_plan.mode.value == "preview"


# ---------------------------------------------------------------------- #
# 红线 5：eval 必须全 PASS 才能发布
# ---------------------------------------------------------------------- #


class TestEvalGateEnforced:
    def test_eval_failure_blocks_publish(self) -> None:
        from dataclasses import replace
        from src.features.ai_harness.skill_studio_drafts import ModelRequirement
        draft = new_draft_from_template(
            author="dev-alice", template=OFFICIAL_VOICEOVER_POLISH, title="T",
        )
        ready = draft.update_field("title", "T v1", kind="major")
        # 注入永远缺的能力
        broken = replace(
            ready,
            model_requirements=ready.model_requirements + (
                ModelRequirement(capability="nonexistent_cap", required=True),
            ),
        )
        result = SkillReleaseGate().publish(broken, released_by="dev-alice")
        assert not result.success
        assert result.deny_code == PublishDenyCode.EVAL_FAILED
        assert result.eval_report is not None
        assert not result.eval_report.passed

    def test_dev_version_blocks_publish(self) -> None:
        draft = new_draft_from_template(
            author="dev-alice", template=OFFICIAL_VOICEOVER_POLISH, title="T",
        )
        # 不 bump，仍是 0.1.0 dev
        result = SkillReleaseGate().publish(draft, released_by="dev-alice")
        assert not result.success
        assert result.deny_code == PublishDenyCode.DEV_VERSION_NOT_PUBLISHABLE


# ---------------------------------------------------------------------- #
# §10.9 验收矩阵：开发者发布的 auto_caption 与官方同结构
# ---------------------------------------------------------------------- #


class TestAcceptanceMatrixDeveloperColumn:
    def test_published_auto_caption_shares_official_manifest_schema(self) -> None:
        """plan §10.9：开发者发布 auto_caption Skill，与官方共享 manifest schema
        家族 / executor_kind 家族 / PREVIEW-only 产物结构。"""
        ac_template = next(
            m for m in p2_official_manifests() if m.skill_id == "topos.auto_caption"
        )
        draft = new_draft_from_template(
            author="dev-carol", template=ac_template, title="我的自动字幕",
        )
        # 草稿 executor_kind 是 editorial_builder（与官方 P2 一致）
        assert draft.executor_kind == ExecutorKind.EDITORIAL_BUILDER
        # dry-run 产 PREVIEW EditPlan（与官方 P2 一致）
        outcome = SkillDryRunner().run(draft, make_auto_caption_fixture())
        assert outcome.edit_plan is not None
        assert outcome.edit_plan.mode.value == "preview"
        # 发布
        ready = draft.update_field("title", "我的自动字幕 v1", kind="major")
        result = SkillReleaseGate().publish(ready, released_by="dev-carol")
        assert result.success
        # 发布产物 schema 字段与官方 manifest 同族（input_schema/output_schema/risk）
        rel = result.release
        assert rel.manifest.input_schema == ac_template.input_schema
        assert rel.manifest.risk == ac_template.risk


# ---------------------------------------------------------------------- #
# 辅助
# ---------------------------------------------------------------------- #


def _is_error(d: object) -> bool:
    sev = getattr(d, "severity", None)
    if sev is None:
        return False
    if hasattr(sev, "value"):
        return str(sev.value).lower() == "error"
    return str(sev).lower() == "error"
