"""P4.3 测试：开发者 Skill Studio —— 评测套件 eval（plan §10.6）。

> 为每个 Skill 编写 eval：成功、模型缺失、证据不足、拒绝上传、取消、
> revision stale、无 approval 六类最低用例；发布前必须通过。

本测试验证 ``SkillEvalSuite`` 能：

1. 为单个草稿装配 6 类（plan 文字列了 7 个，"六类最低"指最少 6）固定用例；
2. 每个用例跑 ``SkillDryRunner`` + 受控注入（缺模型/缺证据/拒绝上传/取消/
   revision stale/无 approval），断言期望结果；
3. 失败用例必须 "expected_failure=True"（即用例本身期望 dry-run 失败，
   才算 PASS；这是 eval 的语义，不是误报）；
4. 汇总成 :class:`SkillEvalReport`（passed/total/per-case），可被 P4.4 发布门禁消费；
5. 红线：eval 全程不联网 / 不写时间线 / 不执行任意代码。

eval 用例的期望不是"全部成功"，而是"按场景产出预期终态"：

- ``success`` → dry-run 成功 + EditPlan PREVIEW
- ``model_missing`` → dry-run 失败 + MODEL_MISSING
- ``evidence_insufficient`` → dry-run 失败 + EVIDENCE_INSUFFICIENT
- ``upload_rejected`` → policy.allow_*=False 时 cloud_upload 被拒（dry-run 不联网）
- ``cancel`` → 取消信号 → 终态 CANCELLED + partial artifact 保留
- ``revision_stale`` → base_revision 与 snapshot 不一致 → STALE_REVISION 拒绝
- ``no_approval`` → dry-run 产 PREVIEW plan，无 approval 事件（红线）
"""
from __future__ import annotations


from src.features.ai_harness.skill_dry_run import (
    DryRunFixture,
    make_auto_caption_fixture,
)
from src.features.ai_harness.skill_eval_suite import (
    EXPECTED_FAILURE_CASES,
    EvalCaseKind,
    SkillEvalCase,
    SkillEvalSuite,
    run_default_suite,
)
from src.features.ai_harness.skill_studio_drafts import (
    SkillDraft,
    new_draft_from_template,
)
from src.features.edit.ai_editing.skill_manifest import OFFICIAL_VOICEOVER_POLISH


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
        author="dev-alice", template=ac, title="自动字幕草稿",
    )


# ---------------------------------------------------------------------- #
# EvalCaseKind 覆盖
# ---------------------------------------------------------------------- #


class TestEvalCaseKinds:
    def test_six_minimum_kinds_defined(self) -> None:
        """plan §10.6 '六类最低用例'。"""
        # 至少包含这 6 个（revision_stale/no_approval 是加分项）
        required = {
            "success", "model_missing", "evidence_insufficient",
            "upload_rejected", "cancel", "no_approval",
        }
        defined = set(EvalCaseKind.ALL)
        assert required.issubset(defined), f"missing: {required - defined}"

    def test_expected_failure_kinds_correct(self) -> None:
        """这些 kind 的用例本身期望 dry-run 失败（eval 的语义）。"""
        for k in EXPECTED_FAILURE_CASES:
            assert k in set(EvalCaseKind.ALL)
        # success 不在 expected_failure 里
        assert "success" not in EXPECTED_FAILURE_CASES


# ---------------------------------------------------------------------- #
# 单用例运行
# ---------------------------------------------------------------------- #


class TestSingleEvalCase:
    def test_success_case_passes(self) -> None:
        case = SkillEvalCase(
            kind=EvalCaseKind.SUCCESS,
            name="success",
            fixture_factory=make_voiceover_fixture_factory(),
        )
        result = case.evaluate(_voiceover_draft())
        assert result.passed
        assert result.outcome.success

    def test_model_missing_case_expects_failure(self) -> None:
        case = SkillEvalCase(
            kind=EvalCaseKind.MODEL_MISSING,
            name="model_missing",
            fixture_factory=make_missing_model_fixture_factory(),
        )
        result = case.evaluate(_voiceover_draft())
        # 用例期望 dry-run 失败 + MODEL_MISSING；满足则 eval passed
        assert result.passed
        assert result.outcome.error_code == "MODEL_MISSING"

    def test_evidence_insufficient_case(self) -> None:
        case = SkillEvalCase(
            kind=EvalCaseKind.EVIDENCE_INSUFFICIENT,
            name="evidence_insufficient",
            fixture_factory=make_empty_evidence_fixture_factory(),
        )
        result = case.evaluate(_auto_caption_draft())
        assert result.passed
        assert result.outcome.error_code == "EVIDENCE_INSUFFICIENT"

    def test_upload_rejected_case(self) -> None:
        """upload_rejected: policy 禁止 upload → dry-run 不联网（拒绝）。"""
        case = SkillEvalCase(
            kind=EvalCaseKind.UPLOAD_REJECTED,
            name="upload_rejected",
            fixture_factory=make_auto_caption_fixture_factory(),
        )
        result = case.evaluate(_voiceover_draft())
        assert result.passed
        # 红线：dry-run 无 cloud_upload/network 事件
        kinds = {e.kind for e in result.outcome.run_record.events}
        assert "cloud_upload" not in kinds

    def test_cancel_case(self) -> None:
        case = SkillEvalCase(
            kind=EvalCaseKind.CANCEL,
            name="cancel",
            fixture_factory=make_voiceover_fixture_factory(),
            cancel_after_start=True,
        )
        result = case.evaluate(_voiceover_draft())
        assert result.passed
        # 取消 → 终态 cancelled（红线：取消后下游不再启动）
        assert result.outcome.run_record.status == "cancelled"
        assert result.cancelled

    def test_no_approval_case(self) -> None:
        """no_approval: dry-run 产 PREVIEW plan，无 approval 事件（红线）。"""
        case = SkillEvalCase(
            kind=EvalCaseKind.NO_APPROVAL,
            name="no_approval",
            fixture_factory=make_auto_caption_fixture_factory(),
        )
        result = case.evaluate(_auto_caption_draft())
        assert result.passed
        kinds = {e.kind for e in result.outcome.run_record.events}
        assert "approval.granted" not in kinds
        assert "approval.consumed" not in kinds

    def test_revision_stale_case(self) -> None:
        case = SkillEvalCase(
            kind=EvalCaseKind.REVISION_STALE,
            name="revision_stale",
            fixture_factory=make_stale_revision_fixture_factory(),
        )
        result = case.evaluate(_voiceover_draft())
        assert result.passed
        assert result.outcome.error_code == "STALE_REVISION"


# ---------------------------------------------------------------------- #
# SkillEvalSuite（6 类汇总）
# ---------------------------------------------------------------------- #


class TestSkillEvalSuite:
    def test_default_suite_has_six_kinds(self) -> None:
        suite = SkillEvalSuite.default()
        kinds = {c.kind for c in suite.cases}
        assert {
            "success", "model_missing", "evidence_insufficient",
            "upload_rejected", "cancel", "no_approval",
        }.issubset(kinds)

    def test_run_default_suite_all_pass(self) -> None:
        draft = _voiceover_draft()
        report = run_default_suite(draft)
        assert report.total >= 6
        assert report.failed_count == 0, f"failures: {[c.name for c in report.results if not c.passed]}"
        assert report.passed

    def test_report_has_per_case_results(self) -> None:
        report = run_default_suite(_auto_caption_draft())
        all_kinds = set(EvalCaseKind.ALL)
        for r in report.results:
            assert r.name
            assert r.kind in all_kinds
            assert isinstance(r.passed, bool)
            assert r.duration_ms >= 0

    def test_report_serializable(self) -> None:
        report = run_default_suite(_voiceover_draft())
        d = report.to_dict()
        assert "results" in d
        assert "passed" in d
        assert d["total"] == report.total

    def test_report_for_failed_skill(self) -> None:
        """构造一个故意失败的草稿（缺模型）→ suite 应如实报告 failure。"""
        from src.features.ai_harness.skill_studio_drafts import ModelRequirement
        from dataclasses import replace
        draft = _voiceover_draft()
        # 注入一个永远缺的能力 → success 用例必失败
        draft = replace(
            draft,
            model_requirements=draft.model_requirements + (
                ModelRequirement(capability="nonexistent_cap", required=True),
            ),
        )
        report = run_default_suite(draft)
        # success 用例应该失败（缺 nonexistent_cap）
        success_result = next(r for r in report.results if r.kind == "success")
        assert not success_result.passed
        # 整体 report.passed=False
        assert not report.passed


# ---------------------------------------------------------------------- #
# 红线：eval 全程无副作用
# ---------------------------------------------------------------------- #


class TestEvalRedLines:
    def test_eval_suite_no_network_events(self) -> None:
        report = run_default_suite(_voiceover_draft())
        for r in report.results:
            kinds = {e.kind for e in r.outcome.run_record.events}
            assert "cloud_upload" not in kinds
            assert "network_request" not in kinds

    def test_eval_suite_no_arbitrary_code(self) -> None:
        report = run_default_suite(_voiceover_draft())
        for r in report.results:
            kinds = {e.kind for e in r.outcome.run_record.events}
            for forbidden in ("process_spawn", "shell_exec", "code_exec", "fs_write"):
                assert forbidden not in kinds

    def test_eval_suite_no_timeline_commit(self) -> None:
        """红线：eval 不写时间线（无 timeline_committed 事件）。"""
        report = run_default_suite(_voiceover_draft())
        for r in report.results:
            kinds = {e.kind for e in r.outcome.run_record.events}
            assert "timeline_committed" not in kinds
            assert "approval.granted" not in kinds


# ---------------------------------------------------------------------- #
# fixture 工厂（脱敏；模块级供 SkillEvalCase 引用）
# ---------------------------------------------------------------------- #


def make_voiceover_fixture_factory():
    from src.features.ai_harness.skill_dry_run import make_voiceover_polish_fixture
    return make_voiceover_polish_fixture


def make_auto_caption_fixture_factory():
    return make_auto_caption_fixture


def make_missing_model_fixture_factory():
    def _factory() -> DryRunFixture:
        base = make_auto_caption_fixture()
        return DryRunFixture(
            evidence_refs=base.evidence_refs,
            timeline_snapshot=base.timeline_snapshot,
            available_capabilities=set(),  # 空 → 缺模型
        )
    return _factory


def make_empty_evidence_fixture_factory():
    def _factory() -> DryRunFixture:
        base = make_auto_caption_fixture()
        return DryRunFixture(
            evidence_refs=(),  # 空证据
            timeline_snapshot=base.timeline_snapshot,
            available_capabilities={"asr", "demucs"},
        )
    return _factory


def make_stale_revision_fixture_factory():
    def _factory() -> DryRunFixture:
        base = make_auto_caption_fixture()
        # revision=-1 表示 stale（与 base_revision 不一致）
        from src.features.ai_harness.skill_dry_run import TimelineSnapshot
        stale = TimelineSnapshot(
            timeline_id=base.timeline_snapshot.timeline_id,
            project_id=base.timeline_snapshot.project_id,
            revision=-1,  # 标记 stale
            track_count=base.timeline_snapshot.track_count,
            fps=base.timeline_snapshot.fps,
        )
        return DryRunFixture(
            evidence_refs=base.evidence_refs,
            timeline_snapshot=stale,
            available_capabilities=base.available_capabilities,
        )
    return _factory
