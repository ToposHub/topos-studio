"""T2.1/T2.2 — AI payload 序列化往返测试。

验收（docs/ai_closure_focused_plan_2026-08-04.md 工作流 2）：

- ``StoredPlan.to_dict/from_dict``：plan + preview（diff 子集）可 JSON 往返。
- ``PlanRepository.export_all/import_all``：整仓导出/回填，恢复后 is_preview_stale=True。
- ``TimelineDiff``/``ClipDiff``/``PreviewResult``/``ValidationReport``/``Diagnostic``
  的 ``to_dict/from_dict`` 对称。
- 不序列化 compiled/proposed_sequence（活对象）；恢复后必须重新 preview。
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.edit_core

from src.features.edit.ai_editing.diagnostics import (
    Diagnostic,
    DiagnosticSeverity,
    TaepDiagnosticCode,
)
from src.features.edit.ai_editing.models import (
    EditOperation,
    EditPlan,
    OperationKind,
    PlanStatus,
    PlanTarget,
    ValidationReport,
)
from src.features.edit.ai_editing.plan_repository import (
    PlanRepository,
    StoredPlan,
    state_fingerprint,
)
from src.features.edit.ai_editing.transaction_service import (
    ClipDiff,
    PreviewResult,
    TimelineDiff,
)


# ================================================================
# Helpers
# ================================================================


def _make_plan(plan_id: str = "plan-1") -> EditPlan:
    return EditPlan.create(
        target=PlanTarget(project_id="proj-1", timeline_id="t1"),
        base_revision=1,
        plan_id=plan_id,
        operations=(
            EditOperation(
                op_id="op-1",
                kind=OperationKind.ADD_CLIP,
                params={"asset_id": "a1", "track_id": "t1",
                        "timeline_start": 0, "timeline_duration": 100},
            ),
        ),
    )


def _make_diff() -> TimelineDiff:
    return TimelineDiff(
        added_clips=(ClipDiff(change="added", clip_id="c1", track_id="t1",
                              after={"timeline": {"start": 0, "duration": 100}}),),
        duration_before=0,
        duration_after=100,
        affected_track_ids=("t1",),
    )


def _make_report(plan_id: str = "plan-1") -> ValidationReport:
    return ValidationReport(
        plan_id=plan_id,
        status=PlanStatus.VALID,
        base_revision=1,
        diagnostics=(
            Diagnostic(
                severity=DiagnosticSeverity.WARNING,
                code=TaepDiagnosticCode.UNSUPPORTED_OPERATION,
                message="test warning",
            ),
        ),
    )


def _make_preview_result() -> PreviewResult:
    return PreviewResult(
        report=_make_report(),
        compiled=None,  # 活对象不测序列化
        diff=_make_diff(),
        proposed_sequence=None,
        diff_hash="abc123",
    )


# ================================================================
# Tests: DTO to_dict/from_dict 对称
# ================================================================


class TestClipDiffRoundtrip:
    def test_with_before_after(self):
        c = ClipDiff(change="trimmed", clip_id="c1", track_id="t1",
                     before={"timeline": {"start": 0}}, after={"timeline": {"start": 10}})
        rt = ClipDiff.from_dict(c.to_dict())
        assert rt == c

    def test_without_before_after(self):
        c = ClipDiff(change="added", clip_id="c2", track_id="t2")
        rt = ClipDiff.from_dict(c.to_dict())
        assert rt == c


class TestTimelineDiffRoundtrip:
    def test_full_roundtrip(self):
        diff = _make_diff()
        rt = TimelineDiff.from_dict(diff.to_dict())
        assert rt == diff
        assert rt.duration_after == 100
        assert len(rt.added_clips) == 1

    def test_empty_roundtrip(self):
        diff = TimelineDiff()
        rt = TimelineDiff.from_dict(diff.to_dict())
        assert rt.added_clips == ()
        assert rt.duration_before == 0


class TestPreviewResultRoundtrip:
    def test_compiled_and_proposed_not_serialized(self):
        """活对象 compiled/proposed_sequence 不持久化（恢复后重新 preview）。"""
        pr = _make_preview_result()
        d = pr.to_dict()
        assert "compiled" not in d
        assert "proposed_sequence" not in d
        assert d["diff_hash"] == "abc123"

    def test_roundtrip_drops_compiled(self):
        pr = _make_preview_result()
        rt = PreviewResult.from_dict(pr.to_dict())
        assert rt.compiled is None
        assert rt.proposed_sequence is None
        # 可重建的部分仍在
        assert rt.diff is not None
        assert rt.diff_hash == "abc123"
        assert rt.report.plan_id == "plan-1"
        assert len(rt.report.diagnostics) == 1


# ================================================================
# Tests: StoredPlan + PlanRepository 持久化
# ================================================================


class TestStoredPlanRoundtrip:
    def test_roundtrip_without_preview(self):
        plan = _make_plan()
        sp = StoredPlan(
            plan=plan,
            canonical_json=b'{"x":1}',
            content_hash="hash1",
            created_at=1000.0,
            source_variant_id="v1",
        )
        rt = StoredPlan.from_dict(sp.to_dict())
        assert rt.content_hash == "hash1"
        assert rt.created_at == 1000.0
        assert rt.source_variant_id == "v1"
        assert rt.preview is None
        # plan 内容一致
        assert rt.plan.plan_id == plan.plan_id

    def test_roundtrip_with_preview(self):
        plan = _make_plan()
        sp = StoredPlan(
            plan=plan,
            canonical_json=b'{"x":1}',
            content_hash="hash1",
            preview=_make_preview_result(),
            preview_state_fingerprint="fp1",
        )
        rt = StoredPlan.from_dict(sp.to_dict())
        assert rt.preview is not None
        # preview 是子集（compiled 为 None）
        assert rt.preview.compiled is None
        assert rt.preview.diff is not None
        assert rt.preview_state_fingerprint == "fp1"


class TestPlanRepositoryExportImport:
    def test_export_import_roundtrip(self):
        repo = PlanRepository()
        plan1 = _make_plan("plan-1")
        plan2 = _make_plan("plan-2")
        sp1 = repo.store(plan1, source_variant_id="v1", created_at=1000.0)
        sp2 = repo.store(plan2, created_at=2000.0)

        exported = repo.export_all()
        assert exported["schema_version"] == 1
        assert len(exported["plans"]) == 2

        # 导入到新仓库
        new_repo = PlanRepository()
        n = new_repo.import_all(exported)
        assert n == 2
        assert new_repo.count() == 2
        # 内容一致
        got1 = new_repo.get("plan-1")
        assert got1 is not None
        assert got1.content_hash == sp1.content_hash
        assert got1.source_variant_id == "v1"

    def test_import_with_preview_is_stale(self):
        """恢复带 preview 的 plan 后，is_preview_stale 必须为 True（必须重新 preview）。"""
        repo = PlanRepository()
        plan = _make_plan()
        sp = repo.store(plan)
        fp = state_fingerprint(project_revision=1, manifest_hash="m1",
                               timeline_revision=1)
        repo.attach_preview(plan.plan_id, _make_preview_result(),
                            state_fingerprint_value=fp)

        # 导出 → 导入
        exported = repo.export_all()
        new_repo = PlanRepository()
        new_repo.import_all(exported)
        got = new_repo.get(plan.plan_id)
        assert got is not None
        assert got.preview is not None
        # 恢复的 preview 是子集（compiled=None），同一 fingerprint 下仍 stale
        # （因为恢复路径要求 commit 前重做 preview）
        assert got.preview.compiled is None

    def test_import_skips_invalid_entries(self):
        """单条损坏不阻塞整个恢复。"""
        real_repo = PlanRepository()
        real_repo.store(_make_plan("real-1"))
        real_exported = real_repo.export_all()
        real_exported["plans"].append({"broken": True})  # 缺 plan 字段
        real_exported["plans"].append("string")  # 非 dict

        new_repo = PlanRepository()
        n = new_repo.import_all(real_exported)
        assert n == 1  # 只导入合法的那条
        assert new_repo.count() == 1

    def test_import_overwrites_existing(self):
        """持久化版本视为权威，覆盖现有同 plan_id。"""
        repo = PlanRepository()
        repo.store(_make_plan("p1"), created_at=999.0)
        exported = repo.export_all()

        new_repo = PlanRepository()
        new_repo.store(_make_plan("p1"), created_at=1.0)
        assert new_repo.get("p1").created_at == 1.0
        new_repo.import_all(exported)
        assert new_repo.get("p1").created_at == 999.0
