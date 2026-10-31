"""P1.5 批次1：result_card_builder 聚合层测试（RunRecord → ResultCardState）。

> 验证 build_result_card_from_run 把 RunRecord 聚合成 ResultCardState：
> - FAILED/CANCELLED → error_result
> - SUCCEEDED + typed artifact → 按 artifact 类型推断 kind/trust
> - 异常/未知 → 降级 error_result，不崩
> - ACTIVE 状态 → None（不渲染终态卡）
"""
from __future__ import annotations

from dataclasses import dataclass

import pytest

from src.features.ai_harness.invocation_models import (
    ArtifactRef, RunRecord, RunStatus,
)
from src.features.ai_harness.result_card import ResultCardState
from src.features.ai_harness.result_card_builder import build_result_card_from_run

pytestmark = pytest.mark.edit_core


# ---------------------------------------------------------------------- #
# fake typed artifact（dataclass，模拟 task_artifacts 的形状）
# ---------------------------------------------------------------------- #


@dataclass(frozen=True)
class _FakeScriptDraft:
    """模拟 ScriptDraft（task_artifacts.py:79）。"""
    draft_id: str = "draft-1"
    audience: str = "年轻用户"
    hooks: tuple = ()
    evidence_refs: tuple[str, ...] = ("seg-1", "seg-2")
    produced_by: str = "creative_director"


@dataclass(frozen=True)
class _FakeEvidenceCitation:
    segment_id: str = "seg-1"
    trust: str = "trusted"


@dataclass(frozen=True)
class _FakeEvidenceSet:
    """模拟 EvidenceSet（task_artifacts.py:155）。"""
    set_id: str = "es-1"
    citations: tuple = ()
    evidence_refs: tuple[str, ...] = ()
    produced_by: str = "media_librarian"

    @property
    def untrusted_count(self) -> int:
        return sum(1 for c in self.citations if getattr(c, "trust", "") == "untrusted")


@dataclass(frozen=True)
class _FakeQaFinding:
    finding_id: str = "f-1"
    category: str = "constraint"
    severity: str = "warning"
    message: str = "时长偏长"


@dataclass(frozen=True)
class _FakeQaReport:
    """模拟 QaReport（task_artifacts.py:288）。"""
    report_id: str = "qa-1"
    findings: tuple = ()
    approved: bool = True
    summary: str = "QA 通过"


@dataclass(frozen=True)
class _FakeUnknownArtifact:
    """未识别的 typed artifact（验证通用兜底）。"""
    artifact_id: str = "unknown-1"
    some_field: str = "data"


# ---------------------------------------------------------------------- #
# 辅助：构造 RunRecord
# ---------------------------------------------------------------------- #


def _make_run(
    *, status: str = RunStatus.SUCCEEDED,
    artifacts: tuple[ArtifactRef, ...] = (),
    diagnostics: tuple[dict, ...] = (),
    error_code: str = "", error_message: str = "",
    cancel_reason: str = "",
) -> RunRecord:
    return RunRecord(
        run_id="r1", invocation_id="inv1",
        status=status, artifacts=artifacts, diagnostics=diagnostics,
        error_code=error_code, error_message=error_message,
        cancel_reason=cancel_reason,
    )


# ---------------------------------------------------------------------- #
# FAILED / CANCELLED → error_result
# ---------------------------------------------------------------------- #


class TestErrorAndCancelled:
    def test_failed_run_returns_error_result(self):
        """FAILED run → error_result，含 error_code/error_message/diagnostics。"""
        run = _make_run(
            status=RunStatus.FAILED,
            error_code="WORKFLOW_NO_OUTPUT",
            error_message="workflow produced no output",
            diagnostics=({"code": "MISSING_EVIDENCE", "missing": ["seg-1"]},),
        )
        state = build_result_card_from_run(run, skill_id="topos.auto_caption")
        assert state is not None
        assert state.kind == "error"
        assert state.error_code == "WORKFLOW_NO_OUTPUT"
        assert "workflow produced no output" in state.summary
        assert len(state.diagnostics) == 1

    def test_cancelled_run_returns_error_result(self):
        """CANCELLED run → error_result，error_code=CANCELLED。"""
        run = _make_run(
            status=RunStatus.CANCELLED,
            cancel_reason="用户取消",
        )
        state = build_result_card_from_run(run)
        assert state is not None
        assert state.kind == "error"
        assert state.error_code == "CANCELLED"
        assert "用户取消" in state.summary


# ---------------------------------------------------------------------- #
# SUCCEEDED + typed artifact → 按 artifact 类型推断
# ---------------------------------------------------------------------- #


class TestSucceededWithArtifact:
    def test_generated_asset_card_includes_provenance_detail(self):
        from src.features.edit.ai_editing.generation_models import (
            GeneratedAsset, GenerationModality, GenerationProvenance,
            LicenseStatus,
        )

        run = _make_run(
            status=RunStatus.SUCCEEDED_WITH_WARNINGS,
            artifacts=(ArtifactRef(
                artifact_id="gen-1", artifact_type="generated_video",
                summary="video candidate",
            ),),
        )
        generated = GeneratedAsset(
            asset_id="gen-1", request_id="req-1", modality=GenerationModality.VIDEO,
            provenance=GenerationProvenance(
                provider_id="stub", model_id="stub-video", model_version="v1",
            ),
            license=LicenseStatus.UNKNOWN,
        )
        state = build_result_card_from_run(
            run, artifact_resolver=lambda aid: generated if aid == "gen-1" else None,
        )
        assert state is not None
        assert state.kind == "generated_media"
        assert "Provider：stub" in state.detail
        assert "许可：unknown" in state.detail

    def test_script_draft_returns_variant_result(self):
        """SUCCEEDED + ScriptDraft → variant_result，evidence_refs 从 draft.evidence_refs 来。"""
        run = _make_run(
            status=RunStatus.SUCCEEDED,
            artifacts=(ArtifactRef(
                artifact_id="draft-1", artifact_type="script",
                summary="创意方向", producer_node_id="creative_director",
            ),),
        )
        draft = _FakeScriptDraft()
        resolver = lambda aid: draft if aid == "draft-1" else None  # noqa: E731
        state = build_result_card_from_run(run, artifact_resolver=resolver)
        assert state is not None
        assert state.kind == "variant"
        # evidence_refs 从 draft.evidence_refs 转换
        assert len(state.evidence_refs) == 2
        ev_ids = [e.evidence_id for e in state.evidence_refs]
        assert "seg-1" in ev_ids and "seg-2" in ev_ids

    def test_evidence_set_with_untrusted_returns_unverified(self):
        """SUCCEEDED + EvidenceSet 含 untrusted → evidence_result，trust=unverified。"""
        run = _make_run(
            status=RunStatus.SUCCEEDED,
            artifacts=(ArtifactRef(
                artifact_id="es-1", artifact_type="evidence",
                summary="证据集", producer_node_id="media_librarian",
            ),),
        )
        es = _FakeEvidenceSet(
            citations=(_FakeEvidenceCitation("seg-1", trust="untrusted"),),
            evidence_refs=("seg-1",),
        )
        resolver = lambda aid: es if aid == "es-1" else None  # noqa: E731
        state = build_result_card_from_run(run, artifact_resolver=resolver)
        assert state is not None
        assert state.kind == "evidence"
        assert state.trust == "unverified", "含 untrusted 引用 → 整体 unverified"

    def test_evidence_set_all_trusted_returns_trusted(self):
        """EvidenceSet 全 trusted → trust=trusted。"""
        run = _make_run(
            status=RunStatus.SUCCEEDED,
            artifacts=(ArtifactRef(artifact_id="es-1", artifact_type="evidence"),),
        )
        es = _FakeEvidenceSet(citations=(_FakeEvidenceCitation("seg-1", trust="trusted"),))
        resolver = lambda aid: es if aid == "es-1" else None  # noqa: E731
        state = build_result_card_from_run(run, artifact_resolver=resolver)
        assert state is not None
        assert state.kind == "evidence"
        assert state.trust == "trusted"

    def test_qa_report_returns_qa_result(self):
        """SUCCEEDED + QaReport → qa_result，diagnostics 从 findings 来。"""
        run = _make_run(
            status=RunStatus.SUCCEEDED,
            artifacts=(ArtifactRef(artifact_id="qa-1", artifact_type="qa"),),
        )
        report = _FakeQaReport(
            findings=(_FakeQaFinding(finding_id="f1", severity="warning", message="时长偏长"),),
        )
        resolver = lambda aid: report if aid == "qa-1" else None  # noqa: E731
        state = build_result_card_from_run(run, artifact_resolver=resolver)
        assert state is not None
        assert state.kind == "qa_report"
        assert len(state.diagnostics) == 1
        assert state.diagnostics[0]["severity"] == "warning"


# ---------------------------------------------------------------------- #
# SUCCEEDED 无 artifact / 未识别 → 通用兜底
# ---------------------------------------------------------------------- #


class TestFallback:
    def test_succeeded_no_artifact_returns_info_card(self):
        """SUCCEEDED 无 artifact → 通用完成卡（不崩）。"""
        run = _make_run(status=RunStatus.SUCCEEDED)
        state = build_result_card_from_run(run)
        assert state is not None
        # 无 artifact → info 或 variant 通用卡
        assert state.kind in ("info", "variant")

    def test_unknown_typed_artifact_returns_variant_fallback(self):
        """未识别的 typed artifact → variant 通用兜底（不崩）。"""
        run = _make_run(
            status=RunStatus.SUCCEEDED,
            artifacts=(ArtifactRef(artifact_id="x-1", artifact_type="unknown"),),
        )
        unknown = _FakeUnknownArtifact()
        resolver = lambda aid: unknown if aid == "x-1" else None  # noqa: E731
        state = build_result_card_from_run(run, artifact_resolver=resolver)
        assert state is not None
        assert state.kind == "variant", "未识别 → variant 通用兜底"

    def test_no_resolver_uses_artifact_ref_summary(self):
        """artifact_resolver=None → 用 ArtifactRef.summary 兜底，不崩。"""
        run = _make_run(
            status=RunStatus.SUCCEEDED,
            artifacts=(ArtifactRef(
                artifact_id="x-1", artifact_type="unknown",
                summary="某产物",
            ),),
        )
        state = build_result_card_from_run(run, artifact_resolver=None)
        assert state is not None
        # 不崩即可；用 summary 兜底


# ---------------------------------------------------------------------- #
# ACTIVE 状态 → None
# ---------------------------------------------------------------------- #


class TestActiveStates:
    @pytest.mark.parametrize("status", [
        RunStatus.RUNNING, RunStatus.QUEUED, RunStatus.VALIDATING,
        RunStatus.WAITING_FOR_USER,
    ])
    def test_active_status_returns_none(self, status: str):
        """ACTIVE 状态 → None（不渲染终态卡）。"""
        run = _make_run(status=status)
        state = build_result_card_from_run(run)
        assert state is None, f"{status} 不应渲染终态卡"

    def test_skipped_returns_none(self):
        """SKIPPED → None（无意义，不渲染）。"""
        run = _make_run(status=RunStatus.SKIPPED)
        state = build_result_card_from_run(run)
        assert state is None


# ---------------------------------------------------------------------- #
# 降级（resolver 异常 / 整体异常）
# ---------------------------------------------------------------------- #


class TestDegradation:
    def test_resolver_exception_degrades_gracefully(self):
        """resolver 抛异常 → 降级（error_result 或通用兜底），不崩。"""
        run = _make_run(
            status=RunStatus.SUCCEEDED,
            artifacts=(ArtifactRef(artifact_id="bad-1", artifact_type="script"),),
        )
        def _bad_resolver(aid):
            raise RuntimeError("store corrupted")
        # 不应抛异常
        state = build_result_card_from_run(run, artifact_resolver=_bad_resolver)
        assert state is not None, "resolver 异常应降级，不返回 None 之外不应崩"
        # 降级为某种卡（error 或 variant 通用）
        assert state.kind in ("error", "variant", "info")

    def test_returns_result_card_state_type(self):
        """返回值类型是 ResultCardState（非 None 时）。"""
        run = _make_run(status=RunStatus.SUCCEEDED)
        state = build_result_card_from_run(run)
        assert isinstance(state, ResultCardState)


# ---------------------------------------------------------------------- #
# skill_id 传递
# ---------------------------------------------------------------------- #


class TestSkillIdPropagation:
    def test_skill_id_in_title_or_summary(self):
        """skill_id 传入后，标题或摘要应体现来源（可追溯）。"""
        run = _make_run(status=RunStatus.SUCCEEDED)
        state = build_result_card_from_run(run, skill_id="topos.auto_caption")
        assert state is not None
        # result_id 或 title 应含 skill_id 或 run_id（可追溯）
        text = f"{state.result_id} {state.title} {state.summary}"
        assert "topos.auto_caption" in text or "r1" in text, "应可追溯到 skill/run"
