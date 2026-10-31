"""P4.2 测试：开发者 Skill Studio —— dry-run 引擎（plan §10.6）。

验证 ``SkillDryRunner`` 的核心契约（plan §10.6 第二条）：

> 运行 dry-run：用 fake provider、固定 evidence fixture、固定 timeline snapshot
> 生成 ``RunRecord``，检查端口、DAG、预算、权限、缓存 key、plan diff。

覆盖：

- 固定 evidence fixture（不含私人素材；plan §12.2 "建立不含私人素材的 fixture"）；
- fake provider / fake scheduler 全程不联网、不写盘；
- RunRecord 终态成功 + 产 EditPlan（PREVIEW 模式）；
- 检查项：端口兼容 / DAG 无环 / 预算不超 / 权限不越级 / cache-key 稳定 /
  plan-diff 非空（仅 preview）；
- 失败场景：缺模型 → 终态 FAILED + MODEL_MISSING；缺证据 → EVIDENCE_INSUFFICIENT；
  超预算 → BUDGET_EXCEEDED；权限不足（executor_kind 为任意代码）→ PERMISSION_DENIED；
  local_dev 草稿声明 write 风险 → 仍只产 PREVIEW（不审批 / 不写时间线）。

红线（plan §10.6 / §1.4）：

- dry-run **绝不**写真实时间线（终态 RunRecord 不触发 approval/commit）；
- dry-run **绝不**联网（fake provider 全程内存）；
- 第三方 / local_dev 不得执行任意代码（``SkillDryRunner`` 只调用受控 executor_kind）。
"""
from __future__ import annotations

from dataclasses import replace


from src.features.ai_harness.invocation_models import RunEventKind
from src.features.ai_harness.skill_dry_run import (
    DryRunCheckCode,
    DryRunErrorCode,
    DryRunFixture,
    SkillDryRunner,
    make_auto_caption_fixture,
    make_voiceover_polish_fixture,
)
from src.features.ai_harness.skill_studio_drafts import (
    SkillDraft,
    new_draft_from_template,
)
from src.features.edit.ai_editing.skill_manifest import (
    OFFICIAL_VOICEOVER_POLISH,
    SkillRisk,
)


def _voiceover_draft() -> SkillDraft:
    return new_draft_from_template(
        author="dev-alice",
        template=OFFICIAL_VOICEOVER_POLISH,
        title="口播精剪草稿",
    )


def _auto_caption_draft() -> SkillDraft:
    from src.features.ai_harness.editorial_skills import p2_official_manifests
    ac = next(m for m in p2_official_manifests() if m.skill_id == "topos.auto_caption")
    return new_draft_from_template(
        author="dev-alice",
        template=ac,
        title="自动字幕草稿",
    )


# ---------------------------------------------------------------------- #
# DryRunFixture：固定证据 + timeline snapshot
# ---------------------------------------------------------------------- #


class TestDryRunFixture:
    def test_fixture_has_no_real_assets(self) -> None:
        """红线：fixture 不含私人素材路径。"""
        fx = make_auto_caption_fixture()
        for ref in fx.evidence_refs:
            assert "/Users/" not in ref.payload
            assert "C:\\Users" not in ref.payload
            assert "secret" not in ref.payload.lower()

    def test_fixture_has_stable_snapshot_revision(self) -> None:
        fx = make_auto_caption_fixture()
        assert fx.timeline_snapshot.revision > 0
        assert fx.timeline_snapshot.timeline_id  # 非空

    def test_fixture_is_deterministic(self) -> None:
        """同一 fixture 工厂调用两次 → 内容一致（便于回归对比）。"""
        a = make_auto_caption_fixture()
        b = make_auto_caption_fixture()
        assert a.evidence_refs == b.evidence_refs
        assert a.timeline_snapshot == b.timeline_snapshot


# ---------------------------------------------------------------------- #
# 成功场景
# ---------------------------------------------------------------------- #


class TestDryRunSuccess:
    def test_voiceover_dry_run_succeeds(self) -> None:
        draft = _voiceover_draft()
        fixture = make_voiceover_polish_fixture()
        runner = SkillDryRunner()
        outcome = runner.run(draft, fixture)
        assert outcome.success
        assert outcome.run_record.is_terminal
        assert outcome.run_record.error_code == ""

    def test_produces_preview_edit_plan(self) -> None:
        """红线：dry-run 产 EditPlan 但 mode=PREVIEW（不写时间线）。"""
        draft = _auto_caption_draft()
        fixture = make_auto_caption_fixture()
        runner = SkillDryRunner()
        outcome = runner.run(draft, fixture)
        assert outcome.edit_plan is not None
        assert outcome.edit_plan.mode.value == "preview"

    def test_run_record_has_skill_id(self) -> None:
        draft = _voiceover_draft()
        outcome = SkillDryRunner().run(draft, make_voiceover_polish_fixture())
        assert outcome.run_record.skill_id == draft.skill_id

    def test_all_checks_pass(self) -> None:
        draft = _voiceover_draft()
        outcome = SkillDryRunner().run(draft, make_voiceover_polish_fixture())
        codes = {c.code for c in outcome.checks}
        assert DryRunCheckCode.PORTS_COMPATIBLE in codes
        assert DryRunCheckCode.DAG_ACYCLIC in codes
        assert DryRunCheckCode.BUDGET_WITHIN in codes
        assert DryRunCheckCode.PERMISSION_OK in codes
        assert DryRunCheckCode.CACHE_KEY_STABLE in codes
        assert DryRunCheckCode.PLAN_DIFF_NONEMPTY in codes
        assert all(c.passed for c in outcome.checks)


# ---------------------------------------------------------------------- #
# 失败场景
# ---------------------------------------------------------------------- #


class TestDryRunModelMissing:
    def test_model_missing_fails_run(self) -> None:
        draft = _voiceover_draft()
        fixture = make_voiceover_polish_fixture()
        # 显式声明 fixture 缺模型 → 应失败
        fixture = DryRunFixture(
            evidence_refs=fixture.evidence_refs,
            timeline_snapshot=fixture.timeline_snapshot,
            available_capabilities=set(),  # 空能力 → asr/llm 都缺
        )
        outcome = SkillDryRunner().run(draft, fixture)
        assert not outcome.success
        assert outcome.run_record.error_code == "MODEL_MISSING"
        # 对应检查项失败
        assert any(
            c.code == DryRunCheckCode.MODEL_REQUIREMENTS and not c.passed
            for c in outcome.checks
        )


class TestDryRunEvidenceInsufficient:
    def test_evidence_insufficient_fails_run(self) -> None:
        draft = _auto_caption_draft()
        # 空 evidence fixture
        fixture = DryRunFixture(
            evidence_refs=(),
            timeline_snapshot=make_auto_caption_fixture().timeline_snapshot,
            available_capabilities={"asr", "demucs"},
        )
        outcome = SkillDryRunner().run(draft, fixture)
        assert not outcome.success
        assert outcome.run_record.error_code == "EVIDENCE_INSUFFICIENT"


class TestDryRunBudgetExceeded:
    def test_budget_exceeded_fails_run(self) -> None:
        draft = _voiceover_draft()
        fixture = make_voiceover_polish_fixture()
        runner = SkillDryRunner(budget_limit=0.0)  # 预算为 0 → 必超
        outcome = runner.run(draft, fixture)
        assert not outcome.success
        assert outcome.run_record.error_code == "BUDGET_EXCEEDED"


def _run_event_vocabulary() -> set[str]:
    """真实事件词汇表（``RunEventKind`` 常量类，invocation_models.py）。

    dry-run 走真实 ``HarnessRuntime``，事件 kind 只能取自该词汇表；
    断言"发出的 kind ⊆ 词汇表"可 fail-closed 地发现未登记的新事件类别
    （旧断言对词汇表外事件名恒真，见 AUD-22807）。
    """
    return {
        v for name, v in vars(RunEventKind).items()
        if isinstance(v, str) and not name.startswith("_")
    }


class TestDryRunPermissionDenied:
    def test_arbitrary_executor_permission_denied(self) -> None:
        """红线：executor_kind 为任意代码 → PERMISSION_DENIED（PERMISSION_OK 失败）。

        正常途径无法构造该草稿（ExecutorKind 构造期拒绝）；这里模拟攻击者
        经反序列化 / replace 注入伪造 kind，验证 dry-run 的独立权限防线。
        """
        class _ForgedArbitraryKind:
            value = "python_exec"
            is_arbitrary_code = True

        draft = replace(_voiceover_draft(), executor_kind=_ForgedArbitraryKind())
        outcome = SkillDryRunner().run(draft, make_voiceover_polish_fixture())
        assert not outcome.success
        assert outcome.run_record.error_code == DryRunErrorCode.PERMISSION_DENIED
        assert outcome.edit_plan is None  # 失败短路，不进 executor
        perm = next(c for c in outcome.checks if c.code == DryRunCheckCode.PERMISSION_OK)
        assert not perm.passed
        assert "forbidden executor_kind" in perm.message

    def test_write_risk_draft_stays_preview(self) -> None:
        """红线：local_dev 草稿即使声明 risk=write，dry-run 也只产 PREVIEW。"""
        draft = _voiceover_draft()
        draft = draft.update_field("risk", SkillRisk.WRITE_REQUIRES_APPROVAL, kind="patch")
        outcome = SkillDryRunner().run(draft, make_voiceover_polish_fixture())
        assert outcome.edit_plan is not None
        assert outcome.edit_plan.mode.value == "preview"
        # 不触发审批授予 / 消费（用真实 RunEventKind 常量，非虚构事件名）
        event_kinds = {e.kind for e in outcome.run_record.events}
        assert RunEventKind.APPROVAL_GRANTED not in event_kinds
        assert RunEventKind.APPROVAL_CONSUMED not in event_kinds
        # fail-closed：事件流也不得出现任何 commit / timeline 写类 kind
        assert not any("commit" in k for k in event_kinds)


# ---------------------------------------------------------------------- #
# Cache key 稳定性
# ---------------------------------------------------------------------- #


class TestDryRunCacheKey:
    def test_same_draft_same_fixture_same_cache_key(self) -> None:
        draft = _voiceover_draft()
        fixture = make_voiceover_polish_fixture()
        runner = SkillDryRunner()
        o1 = runner.run(draft, fixture)
        o2 = runner.run(draft, fixture)
        assert o1.cache_key == o2.cache_key

    def test_different_draft_different_cache_key(self) -> None:
        fixture = make_voiceover_polish_fixture()
        runner = SkillDryRunner()
        o1 = runner.run(_voiceover_draft(), fixture)
        o2 = runner.run(_voiceover_draft(), fixture)
        assert o1.cache_key != o2.cache_key  # 不同 draft_id → 不同 key


# ---------------------------------------------------------------------- #
# 红线：dry-run 不联网 / 不写盘
# ---------------------------------------------------------------------- #


class TestDryRunNoSideEffects:
    def test_dry_run_does_not_touch_real_providers(self) -> None:
        """fake provider 全程内存；事件流无网络 / 上传类事件。"""
        draft = _voiceover_draft()
        fixture = make_voiceover_polish_fixture()
        runner = SkillDryRunner()  # 默认 fake provider
        outcome = runner.run(draft, fixture)
        event_kinds = {e.kind for e in outcome.run_record.events}
        # dry-run 走真实 HarnessRuntime：发出的 kind 必须都在真实词汇表内
        # （旧断言用词汇表外事件名 "network_request"/"cloud_upload"，恒真）
        assert event_kinds <= _run_event_vocabulary()
        # fail-closed：若未来词汇表新增 network/cloud/upload 类 kind 且被
        # dry-run 发出，此断言失败——而非靠虚构事件名静默通过
        assert not any(
            ("network" in k) or ("cloud" in k) or ("upload" in k)
            for k in event_kinds
        )

    def test_no_arbitrary_code_executed(self) -> None:
        """红线：草稿 executor_kind 受控；dry-run 不执行任意 Python/shell。"""
        draft = _voiceover_draft()
        assert not draft.executor_kind.is_arbitrary_code
        outcome = SkillDryRunner().run(draft, make_voiceover_polish_fixture())
        event_kinds = {e.kind for e in outcome.run_record.events}
        # 同上：词汇表内 fail-closed 检查（旧断言的 process_spawn/shell_exec/
        # code_exec/fs_write 均不在词汇表内，对任何 RunRecord 恒真）
        assert event_kinds <= _run_event_vocabulary()
        assert not any(
            ("exec" in k) or ("spawn" in k) or ("shell" in k)
            or ("fs_write" in k) or ("process" in k)
            for k in event_kinds
        )


# ---------------------------------------------------------------------- #
# WorkflowDefinition 来源的草稿也走 dry-run
# ---------------------------------------------------------------------- #


class TestDryRunWorkflowDraft:
    def test_workflow_draft_dry_run_checks_dag(self) -> None:
        from src.features.ai_harness.skill_studio_drafts import new_draft_from_workflow
        wf_def = {
            "workflow_id": "wf-test-001",
            "name": "测试 workflow",
            "nodes": [
                {"node_id": "input", "spec_id": "topos.media.selected_assets"},
                {"node_id": "asr", "spec_id": "topos.analysis.transcribe_auto"},
                {"node_id": "caption", "spec_id": "topos.caption.propose_track"},
            ],
            "edges": [
                {"from": {"node_id": "input", "port": "assets"},
                 "to": {"node_id": "asr", "port": "assets"}},
                {"from": {"node_id": "asr", "port": "transcript"},
                 "to": {"node_id": "caption", "port": "transcript"}},
            ],
            "policy": {"network": "ask", "timeline_write": "approval_required"},
        }
        draft = new_draft_from_workflow(
            author="dev-alice",
            workflow_definition=wf_def,
            title="测试 workflow 草稿",
        )
        fixture = make_auto_caption_fixture()
        outcome = SkillDryRunner().run(draft, fixture)
        # 至少做了 DAG 检查
        assert any(c.code == DryRunCheckCode.DAG_ACYCLIC for c in outcome.checks)
