"""wd10-blk 批（前波白名单阻塞项 + 登记项）修复回归测试。

每条测试对应 REMEDIATION_PLAN.md 的一个条目，锁定修复后的根因行为：

- AUD-13401 宿主 approval_service 访问器异常上抛（不再伪装授权失败）
- AUD-13402 request_export 强制"输出不允许静默覆盖"（PathGrant.allow_overwrite 生效）
- AUD-13406 commit_command 的 revision 冲突折叠为 CommitResult(applied=False)
- AUD-13900 月度成本告警以 WARNING 诊断送达调用方（job 仍创建）
- AUD-13901 CostLedger 并发累计无丢失（锁内读-改-写）
- AUD-13902 取消与候选入箱原子（TOCTOU 关闭：取消后候选不入素材箱）
- AUD-14101 _compile_operation 把 ValueError/TypeError 收敛为 CompileError
- AUD-14106 SET_CLIP_FADE / ADD_TRANSITION 值约束进 validator
- AUD-14402 SessionCatalog.from_dict 拒绝未来版本号
- AUD-14410 REVISION_CONFLICT 诊断模板变量匹配（消息可渲染、原因入 details）
- AUD-R2-013 图引擎环依赖检测真实工作（死 DFS 在引擎侧被替换）
- AUD-18806 遗留纯函数保留 ClipInstance 扩展字段 / 转场 params
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

pytestmark = pytest.mark.edit_core

from src.features.edit.ai_editing.generation_bin import GenerationBin
from src.features.edit.ai_editing.generation_models import (
    GenerationConsent,
    GenerationModality,
    GenerationRequest,
)
from src.features.edit.ai_editing.generation_provider import (
    DeterministicGenerationProvider,
    ProviderRegistry,
)
from src.features.edit.ai_editing.jobs import JobRegistry, JobStatus
from src.features.edit.ai_editing.media_generation_service import (
    CostLedger,
    GenerationService,
)
from src.shared.media.metadata import Rational

# --------------------------------------------------------------------------- #
# 共享测试数据
# --------------------------------------------------------------------------- #


def _project_state():
    from src.features.edit.ai_editing.edit_project_service import ProjectSequences
    from src.features.edit.core.project import EditProject, MediaAsset, TimelineSequenceRef
    from src.features.edit.core.timeline_v2 import EditTrack, TimelineSequence

    seq = TimelineSequence(
        timeline_id="t1", name="Main", fps=Rational(24, 1),
        width=1920, height=1080, sample_rate=48000,
        video_tracks=(EditTrack(track_id="v1", name="V1", track_type="video", index=0),),
    )
    asset = MediaAsset(
        asset_id="a1", path="/home/secret/clip.mp4", media_type="video",
        display_name="Clip A", metadata={"duration_frames": 5000},
    )
    ref = TimelineSequenceRef(
        timeline_id="t1", name="Main", fps=24.0,
        width=1920, height=1080, sample_rate=48000,
    )
    project = EditProject(
        project_id="p1", name="AI Project", assets={"a1": asset},
        timelines={"t1": ref}, root_bin_id="root", active_timeline_id="t1",
    )
    return ProjectSequences(project=project, sequences={"t1": seq})


class _ApprovalHost:
    """实现 EditSessionHost 的最小 host；approval_service 行为可注入。"""

    def __init__(self, state, approval_service_factory=None) -> None:
        self._state = state
        self._approval_service_factory = approval_service_factory
        self.committed = []

    def get_state(self):
        return self._state

    def get_manifest(self):
        from src.features.edit.ai_editing.capability_manifest import build_manifest
        return build_manifest()

    def on_committed(self, result) -> None:
        self.committed.append(result)

    def request_user_approval(self, plan, diff):
        return None

    def approval_service(self):
        if self._approval_service_factory is None:
            return None
        return self._approval_service_factory()

    # Composite 协议成员（运行时可选）
    def get_composite_snapshot(self):
        return None


def _full_consent(**kw) -> GenerationConsent:
    base = dict(
        confirmed=True, provider_id="deterministic-stub",
        cost_acknowledged=True, upload_acknowledged=True,
        copyright_acknowledged=True,
    )
    base.update(kw)
    return GenerationConsent(**base)


def _gen_request(**kw) -> GenerationRequest:
    base = dict(
        request_id="req-wd10", modality=GenerationModality.VIDEO, prompt="产品特写",
        consent=_full_consent(),
    )
    base.update(kw)
    return GenerationRequest(**base)


def _gen_service(*, ledger=None) -> GenerationService:
    reg = ProviderRegistry()
    reg.register(DeterministicGenerationProvider(), preferred=True)
    return GenerationService(
        registry=reg, bin=GenerationBin(), jobs=JobRegistry(), cost_ledger=ledger,
    )


# --------------------------------------------------------------------------- #
# AUD-13401 — 宿主授权访问器异常不再被吞
# --------------------------------------------------------------------------- #


class TestAud13401:
    def test_host_accessor_exception_propagates(self):
        from src.features.edit.ai_editing.edit_api import EditSession

        def broken_accessor():
            raise RuntimeError("host approval service corrupted")

        session = EditSession(_ApprovalHost(_project_state(), broken_accessor))
        with pytest.raises(RuntimeError, match="host approval service corrupted"):
            session._host_approval_service()

    def test_apply_surfaces_host_defect_instead_of_permission_denied(self):
        """apply() 不再把宿主缺陷伪装成"授权失败 applied=False"。"""
        from src.features.edit.ai_editing.edit_api import EditSession
        from src.features.edit.ai_editing.models import (
            EditOperation, EditPlan, OperationKind, PlanTarget,
        )

        def broken_accessor():
            raise RuntimeError("boom in host")

        session = EditSession(_ApprovalHost(_project_state(), broken_accessor))
        plan = EditPlan.create(
            PlanTarget("p1", "t1"), 0,
            operations=(EditOperation("op1", OperationKind.ADD_CLIP, params={
                "track_id": "v1", "asset_id": "a1",
                "source": {"start": 0, "duration": 10},
                "timeline": {"start": 0, "duration": 10},
            }),),
        )
        with pytest.raises(RuntimeError, match="boom in host"):
            session.apply(plan)

    def test_missing_protocol_method_returns_none(self):
        from src.features.edit.ai_editing.edit_api import EditSession

        class BareHost:
            def get_state(self):
                return _project_state()

            def get_manifest(self):
                from src.features.edit.ai_editing.capability_manifest import (
                    build_manifest,
                )
                return build_manifest()

            def on_committed(self, result) -> None:
                pass

        session = EditSession(BareHost())
        assert session._host_approval_service() is None


# --------------------------------------------------------------------------- #
# AUD-13402 — 输出不允许静默覆盖（PathGrant.allow_overwrite 生效）
# --------------------------------------------------------------------------- #


class TestAud13402:
    def _export_request(self, output_path: str):
        from src.features.edit.ai_editing.export_service import ExportRequest

        return ExportRequest(
            timeline_id="t1", codec="h264", output_path=output_path,
        )

    def test_existing_file_without_grant_is_refused(self, tmp_path):
        from src.features.edit.ai_editing.edit_api import EditSession

        target = tmp_path / "out.mp4"
        target.write_text("existing render")
        session = EditSession(_ApprovalHost(_project_state()))
        result = session.request_export(self._export_request(str(target)))
        assert result["job"] is None
        assert result["config"] is None
        assert result["preflight"].has_blocking
        assert any(
            d.code.value == "TAEP_PERMISSION_DENIED"
            for d in result["preflight"].diagnostics
        )
        # 稳定诊断文本：指出已存在 + 需要覆盖授权。
        assert any("覆盖" in b for b in result["preflight"].blocking)

    def test_existing_file_with_non_overwrite_grant_is_refused(self, tmp_path):
        from src.features.edit.ai_editing.edit_api import EditSession, PathGrant

        target = tmp_path / "out.mp4"
        target.write_text("existing render")
        session = EditSession(_ApprovalHost(_project_state()))
        grant = PathGrant(path=str(target), allow_overwrite=False, granted_by="ui-pick")
        result = session.request_export(self._export_request(str(target)), path_grant=grant)
        assert result["job"] is None
        assert result["preflight"].has_blocking

    def test_existing_file_with_overwrite_grant_passes_gate(self, tmp_path):
        from src.features.edit.ai_editing.edit_api import EditSession, PathGrant

        target = tmp_path / "out.mp4"
        target.write_text("existing render")
        session = EditSession(_ApprovalHost(_project_state()))
        grant = PathGrant(path=str(target), allow_overwrite=True, granted_by="ui-pick")
        result = session.request_export(self._export_request(str(target)), path_grant=grant)
        # 覆盖门不再拦截（其余 preflight 结论以 ExportService 为准）。
        assert not any("覆盖" in b for b in result["preflight"].blocking)
        assert all(
            d.code.value != "TAEP_PERMISSION_DENIED"
            for d in result["preflight"].diagnostics
        )

    def test_new_file_without_grant_passes_gate(self, tmp_path):
        from src.features.edit.ai_editing.edit_api import EditSession

        target = tmp_path / "fresh-out.mp4"
        session = EditSession(_ApprovalHost(_project_state()))
        result = session.request_export(self._export_request(str(target)))
        assert all(
            d.code.value != "TAEP_PERMISSION_DENIED"
            for d in result["preflight"].diagnostics
        )


# --------------------------------------------------------------------------- #
# AUD-13406 — revision 冲突统一走 CommitResult(applied=False)
# --------------------------------------------------------------------------- #


class TestAud13406:
    def test_conflict_returns_not_applied_with_base_and_current(self):
        from src.features.edit.ai_editing.edit_project_service import (
            EditProjectService,
        )
        from src.features.edit.core.commands import AddClipCommand
        from src.features.edit.core.timeline_v2 import ClipInstance

        state = _project_state()
        clip = ClipInstance(
            clip_id="c1", asset_id="a1", track_id="v1",
            timeline_start=0, timeline_duration=100,
            source_start=0, source_duration=100,
        )
        cmd = AddClipCommand.create("v1", clip)
        result = EditProjectService.commit_command(state, "t1", cmd, base_revision=9)
        assert result.applied is False
        assert result.error is not None
        assert "9" in result.error and "0" in result.error
        assert any("revision" in e.lower() for e in result.errors)
        # 两层状态不变。
        assert state.sequences["t1"].revision == 0
        assert state.project.revision == 0


# --------------------------------------------------------------------------- #
# AUD-13900 — 月度成本告警链路
# --------------------------------------------------------------------------- #


class TestAud13900:
    def test_monthly_alert_flows_as_warning_diagnostic(self):
        from src.features.edit.ai_editing.diagnostics import DiagnosticSeverity

        ledger = CostLedger(monthly_cost_alert=1.0)
        ledger.add(
            provider_id="deterministic-stub",
            modality=GenerationModality.VIDEO, cost_units=2.0,
        )
        svc = _gen_service(ledger=ledger)
        job, diags = svc.request_generation(_gen_request())
        # 告警不阻断：job 已创建并成功完成，候选入箱。
        assert job is not None
        assert job.status == JobStatus.SUCCEEDED
        warnings = [d for d in diags if d.severity == DiagnosticSeverity.WARNING]
        assert warnings, diags
        assert any("月度告警" in d.message for d in warnings)
        assert not any(d.is_error for d in diags)

    def test_below_threshold_no_warning(self):
        from src.features.edit.ai_editing.diagnostics import DiagnosticSeverity

        ledger = CostLedger(monthly_cost_alert=100.0)
        svc = _gen_service(ledger=ledger)
        job, diags = svc.request_generation(_gen_request())
        assert job is not None
        assert not any(
            d.severity == DiagnosticSeverity.WARNING for d in diags
        )

    def test_hard_cost_gate_still_blocks(self):
        from src.features.edit.ai_editing.generation_models import GenerationBudget

        svc = _gen_service()
        req = _gen_request(budget=GenerationBudget(max_cost_units=0))
        job, diags = svc.request_generation(req)
        assert job is None
        assert any(d.is_error for d in diags)

    def test_async_path_returns_warning(self):
        from src.features.edit.ai_editing.diagnostics import DiagnosticSeverity

        ledger = CostLedger(monthly_cost_alert=1.0)
        ledger.add(
            provider_id="deterministic-stub",
            modality=GenerationModality.VIDEO, cost_units=5.0,
        )
        svc = _gen_service(ledger=ledger)
        try:
            job, diags = svc.request_generation_async(_gen_request())
            assert job is not None
            assert any(
                d.severity == DiagnosticSeverity.WARNING
                and "月度告警" in d.message
                for d in diags
            )
        finally:
            svc.close()


# --------------------------------------------------------------------------- #
# AUD-13901 — CostLedger 并发累计
# --------------------------------------------------------------------------- #


class TestAud13901:
    def test_concurrent_adds_do_not_lose_units(self):
        ledger = CostLedger()
        threads: list[threading.Thread] = []
        per_thread, repeats = 8, 250
        barrier = threading.Barrier(per_thread)

        def worker() -> None:
            barrier.wait()
            for _ in range(repeats):
                ledger.add(
                    provider_id="p",
                    modality=GenerationModality.VIDEO, cost_units=0.5,
                )

        for _ in range(per_thread):
            threads.append(threading.Thread(target=worker))
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        expected = per_thread * repeats * 0.5
        assert ledger.total_for("p") == expected
        assert ledger.grand_total() == expected

    def test_ledger_state_not_shared_across_instances(self):
        # dataclass 新增锁字段不得把状态串到实例之间。
        a, b = CostLedger(), CostLedger()
        a.add(provider_id="p", modality=GenerationModality.VIDEO, cost_units=1.0)
        assert a.total_for("p") == 1.0
        assert b.total_for("p") == 0.0


# --------------------------------------------------------------------------- #
# AUD-13902 — 取消与入箱原子（TOCTOU 关闭）
# --------------------------------------------------------------------------- #


class TestAud13902:
    def test_cancel_during_finalize_window_keeps_bin_clean(self, monkeypatch):
        """取消恰在 provider 返回后、入箱前到达 → 候选不入素材箱。"""
        from src.features.edit.ai_editing.media_generation_service import (
            GenerationService,
        )

        svc = _gen_service()
        request = _gen_request()

        def cancel_then_ok(req, provider, result):
            # _validate_provider_result 位于早退 CANCELLING 检查之后、入箱
            # 之前——正是旧实现的 TOCTOU 窗口。此时请求取消。
            active = svc.job_registry.list_active()
            assert active, "job must be active while executing"
            assert svc.cancel(active[0].job_id)
            return []

        monkeypatch.setattr(
            GenerationService, "_validate_provider_result",
            staticmethod(cancel_then_ok),
        )
        job, diags = svc.request_generation(request)
        assert job is not None
        # Job 收尾为 CANCELLED，而不是 SUCCEEDED。
        assert job.status == JobStatus.CANCELLED
        # 红线（§11.1）：取消的候选不进入素材箱。
        assert svc.generation_bin.by_request(request.request_id) == ()
        assert svc.generation_bin.count == 0
        assert diags == []

    def test_cancel_after_complete_keeps_results(self):
        svc = _gen_service()
        job, _ = svc.request_generation(_gen_request())
        assert job is not None
        assert job.status == JobStatus.SUCCEEDED
        # complete 之后的迟到取消不产生副作用：Job 保持 SUCCEEDED、
        # 候选仍在素材箱（request_cancel 对终态 Job 是 no-op）。
        svc.cancel(job.job_id)
        assert job.status == JobStatus.SUCCEEDED
        assert svc.generation_bin.count > 0


# --------------------------------------------------------------------------- #
# AUD-14101 — 编译错误协议（ValueError/TypeError → CompileError）
# --------------------------------------------------------------------------- #


class TestAud14101:
    def _plan(self, kind, params):
        from src.features.edit.ai_editing.models import (
            EditOperation, EditPlan, OperationKind, PlanTarget,
        )

        return EditPlan.create(
            PlanTarget("p1", "t1"), 0,
            operations=(EditOperation("op1", kind, params=params),),
        )

    def test_add_transition_non_numeric_duration(self):
        from src.features.edit.ai_editing.plan_compiler import CompileError, compile_plan
        from src.features.edit.ai_editing.models import OperationKind

        plan = self._plan(OperationKind.ADD_TRANSITION, {
            "track_id": "v1", "left_clip_id": "c1", "right_clip_id": "c2",
            "duration": "abc",
        })
        with pytest.raises(CompileError) as exc_info:
            compile_plan(plan)
        assert "invalid param value/type" in str(exc_info.value)
        assert "ValueError" in str(exc_info.value)

    def test_split_clip_none_frame(self):
        from src.features.edit.ai_editing.plan_compiler import CompileError, compile_plan
        from src.features.edit.ai_editing.models import OperationKind

        plan = self._plan(OperationKind.SPLIT_CLIP, {
            "track_id": "v1", "clip_id": "c1", "split_frame": None,
        })
        with pytest.raises(CompileError) as exc_info:
            compile_plan(plan)
        assert "invalid param value/type" in str(exc_info.value)
        assert "TypeError" in str(exc_info.value)

    def test_set_track_state_string_state(self):
        from src.features.edit.ai_editing.plan_compiler import CompileError, compile_plan
        from src.features.edit.ai_editing.models import OperationKind

        plan = self._plan(OperationKind.SET_TRACK_STATE, {
            "track_id": "v1", "state": "mute",
        })
        with pytest.raises(CompileError):
            compile_plan(plan)

    def test_missing_param_still_reported_as_missing(self):
        from src.features.edit.ai_editing.plan_compiler import CompileError, compile_plan
        from src.features.edit.ai_editing.models import OperationKind

        plan = self._plan(OperationKind.DELETE_CLIP, {"track_id": "v1"})
        with pytest.raises(CompileError) as exc_info:
            compile_plan(plan)
        assert "missing required param" in str(exc_info.value)


# --------------------------------------------------------------------------- #
# AUD-14106 — SET_CLIP_FADE / ADD_TRANSITION 值约束
# --------------------------------------------------------------------------- #


def _cliped_state():
    """带一个真实 clip（c1, [0,100)）+ 邻接 clip（c2, [100,200)）的状态。"""
    from src.features.edit.ai_editing.edit_project_service import ProjectSequences
    from src.features.edit.core.project import EditProject, MediaAsset, TimelineSequenceRef
    from src.features.edit.core.timeline_v2 import ClipInstance, EditTrack, TimelineSequence

    c1 = ClipInstance(
        clip_id="c1", asset_id="a1", track_id="v1",
        timeline_start=0, timeline_duration=100,
        source_start=0, source_duration=100,
    )
    c2 = ClipInstance(
        clip_id="c2", asset_id="a1", track_id="v1",
        timeline_start=100, timeline_duration=100,
        source_start=100, source_duration=100,
    )
    seq = TimelineSequence(
        timeline_id="t1", name="Main", fps=Rational(24, 1),
        width=1920, height=1080, sample_rate=48000,
        video_tracks=(EditTrack(
            track_id="v1", name="V1", track_type="video", index=0,
            clips=(c1, c2),
        ),),
    )
    asset = MediaAsset(
        asset_id="a1", path="/home/secret/clip.mp4", media_type="video",
        display_name="Clip A", metadata={"duration_frames": 5000},
    )
    ref = TimelineSequenceRef(
        timeline_id="t1", name="Main", fps=24.0,
        width=1920, height=1080, sample_rate=48000,
    )
    project = EditProject(
        project_id="p1", name="AI Project", assets={"a1": asset},
        timelines={"t1": ref}, root_bin_id="root", active_timeline_id="t1",
    )
    return ProjectSequences(project=project, sequences={"t1": seq})


class TestAud14106:
    def _validate(self, kind, params):
        from src.features.edit.ai_editing.models import (
            EditOperation, EditPlan, OperationKind, PlanTarget,
        )
        from src.features.edit.ai_editing.plan_validator import validate_plan

        plan = EditPlan.create(
            PlanTarget("p1", "t1"), 0,
            operations=(EditOperation("op1", kind, params=params),),
        )
        report = validate_plan(plan, state=_cliped_state())
        return report

    def test_set_clip_fade_missing_both_fields(self):
        from src.features.edit.ai_editing.models import OperationKind

        report = self._validate(OperationKind.SET_CLIP_FADE, {"clip_id": "c1"})
        assert report.has_errors
        assert any("fade" in d.message for d in report.errors)

    def test_set_clip_fade_negative_value(self):
        from src.features.edit.ai_editing.models import OperationKind

        report = self._validate(OperationKind.SET_CLIP_FADE, {
            "clip_id": "c1", "fade_in_duration": -5,
        })
        assert report.has_errors
        assert any("non-negative" in d.message for d in report.errors)

    def test_set_clip_fade_string_value(self):
        from src.features.edit.ai_editing.models import OperationKind

        report = self._validate(OperationKind.SET_CLIP_FADE, {
            "clip_id": "c1", "fade_out_duration": "8",
        })
        assert report.has_errors
        assert any("non-negative" in d.message for d in report.errors)

    def test_set_clip_fade_valid_values_pass(self):
        from src.features.edit.ai_editing.models import OperationKind

        report = self._validate(OperationKind.SET_CLIP_FADE, {
            "clip_id": "c1", "fade_in_duration": 24, "fade_out_duration": 0,
        })
        assert not any("fade" in d.message for d in report.errors)

    def test_add_transition_bad_duration_values(self):
        from src.features.edit.ai_editing.models import OperationKind

        for bad in ("abc", 0, -3, 12.0, True):
            report = self._validate(OperationKind.ADD_TRANSITION, {
                "track_id": "v1", "left_clip_id": "c1", "right_clip_id": "c2",
                "duration": bad,
            })
            assert report.has_errors, bad
            assert any(
                "positive integer" in d.message for d in report.errors
            ), bad

    def test_add_transition_missing_duration_uses_default(self):
        from src.features.edit.ai_editing.models import OperationKind

        report = self._validate(OperationKind.ADD_TRANSITION, {
            "track_id": "v1", "left_clip_id": "c1", "right_clip_id": "c2",
        })
        assert not any("duration" in d.message for d in report.errors)


# --------------------------------------------------------------------------- #
# AUD-14402 — 未来版本目录拒绝
# --------------------------------------------------------------------------- #


class TestAud14402:
    def test_future_version_rejected(self):
        from src.features.edit.ai_editing.session_models import (
            SESSION_CATALOG_VERSION,
            SessionCatalog,
            UnsupportedCatalogVersionError,
        )

        catalog = SessionCatalog()
        catalog.create_session("s1")
        data = catalog.to_dict()
        data["version"] = SESSION_CATALOG_VERSION + 1
        with pytest.raises(UnsupportedCatalogVersionError) as exc_info:
            SessionCatalog.from_dict(data)
        assert exc_info.value.found_version == SESSION_CATALOG_VERSION + 1
        assert exc_info.value.supported_version == SESSION_CATALOG_VERSION
        # ValueError 族：AssistantSessionStore.load 的畸形 payload 边界可捕获。
        assert isinstance(exc_info.value, ValueError)

    def test_v2_roundtrip_and_v1_migration_unchanged(self):
        from src.features.edit.ai_editing.session_models import (
            LEGACY_SNAPSHOT_VERSION,
            SESSION_CATALOG_VERSION,
            SessionCatalog,
        )

        catalog = SessionCatalog()
        catalog.create_session("s1")
        restored = SessionCatalog.from_dict(catalog.to_dict())
        assert restored.version == SESSION_CATALOG_VERSION
        assert set(restored.sessions) == set(catalog.sessions)

        v1 = {"briefs": {}, "variants": {}, "decisions": {},
              "updated_at": 123.0}
        migrated = SessionCatalog.from_dict(
            {"version": LEGACY_SNAPSHOT_VERSION, **v1}
        )
        assert migrated.version == SESSION_CATALOG_VERSION


# --------------------------------------------------------------------------- #
# AUD-14410 — REVISION_CONFLICT 模板变量匹配
# --------------------------------------------------------------------------- #


def _skill_manifest(skill_id="test.skill", version="1.0.0"):
    from src.features.edit.ai_editing.editorial_models import SegmentKind
    from src.features.edit.ai_editing.skill_manifest import (
        SkillManifest, SkillSource, ToolPolicy, with_signature,
    )

    return with_signature(
        SkillManifest(
            skill_id=skill_id, version=version, title="Test",
            source=SkillSource.VERIFIED_THIRD_PARTY,
            required_evidence_kinds=(SegmentKind.TRANSCRIPT,),
            produces_plan=True,
            tool_policy=ToolPolicy(
                allowed_tools=("topos.search_assets",), max_permission="read",
            ),
        ),
        signed_by="tester",
    )


class TestAud14410:
    def test_conflict_message_renders_without_placeholders(self):
        from src.features.edit.ai_editing.diagnostics import TaepDiagnosticCode
        from src.features.edit.ai_editing.skill_registry import SkillRegistry

        reg = SkillRegistry(register_official=False)
        assert reg.register(_skill_manifest(version="1.2.0")) == []
        diags = reg.register(_skill_manifest(version="1.0.0"))
        assert len(diags) == 1
        diag = diags[0]
        assert diag.code == TaepDiagnosticCode.REVISION_CONFLICT
        # 模板变量齐备：不再回退成带花括号的原始占位符。
        assert "{" not in diag.message and "}" not in diag.message
        assert "1.2.0" in diag.message and "1.0.0" in diag.message
        # 完整说明（含 skill_id 与双版本号）进入 details，可审计。
        assert diag.details is not None
        assert diag.details["skill_id"] == "test.skill"
        assert diag.details["registered_version"] == "1.2.0"
        assert diag.details["new_version"] == "1.0.0"
        assert "not higher" in diag.details["reason"]

    def test_upgrade_with_higher_version_still_allowed(self):
        from src.features.edit.ai_editing.skill_registry import SkillRegistry

        reg = SkillRegistry(register_official=False)
        assert reg.register(_skill_manifest(version="1.0.0")) == []
        assert reg.register(_skill_manifest(version="1.1.0")) == []
        assert reg.get("test.skill").version == "1.1.0"


# --------------------------------------------------------------------------- #
# AUD-R2-013 — 图引擎环依赖检测真实工作
# --------------------------------------------------------------------------- #


class TestAudR2013:
    def _graph(self, *node_ids: str):
        from src.features.composite.core.node_engine import NodeGraph
        from src.features.composite.nodes.output_node import OutputNode

        graph = NodeGraph()
        for node_id in node_ids:
            graph.add_node(OutputNode(node_id=node_id))
        return graph

    def test_two_node_cycle_rejected(self):
        graph = self._graph("a", "b")
        ok, err = graph.set_node_expression("a", "width", "b.width")
        assert ok, err
        ok, err = graph.set_node_expression("b", "width", "a.width")
        assert not ok
        assert "Circular" in (err or "")

    def test_self_reference_rejected(self):
        graph = self._graph("solo")
        ok, err = graph.set_node_expression("solo", "width", "solo.width")
        assert not ok
        assert "Circular" in (err or "")

    def test_long_cycle_rejected(self):
        graph = self._graph("a", "b", "c")
        assert graph.set_node_expression("a", "width", "b.width")[0]
        assert graph.set_node_expression("b", "width", "c.width")[0]
        ok, err = graph.set_node_expression("c", "width", "a.width")
        assert not ok
        assert "Circular" in (err or "")

    def test_acyclic_chain_accepted_and_rejected_edge_rolls_back(self):
        graph = self._graph("a", "b")
        assert graph.set_node_expression("a", "width", "b.width")[0]
        ok, _ = graph.set_node_expression("b", "width", "this.width * 2.0")
        assert ok  # 无依赖表达式，不构成环
        # 被拒绝的表达式不残留在 manager（回滚干净）。
        graph2 = self._graph("x", "y")
        assert graph2.set_node_expression("x", "width", "y.width")[0]
        graph2.set_node_expression("y", "width", "x.width")
        assert not graph2._expression_manager.has_expression("y", "width")
        assert graph2.get_node("y").get_expression("width") is None

    def test_plan_path_cyclic_plan_rejected(self):
        from src.features.composite.edit_service import CompositeEditService

        graph = self._graph("a", "b")
        result = CompositeEditService(graph).apply({
            "plan_id": "cyclic", "operations": [
                {"op": "set_expression", "node_id": "a",
                 "param_name": "width", "expression": "b.width"},
                {"op": "set_expression", "node_id": "b",
                 "param_name": "width", "expression": "a.width"},
            ],
        })
        assert not result.applied


# --------------------------------------------------------------------------- #
# AUD-18806 — 遗留纯函数保留扩展字段
# --------------------------------------------------------------------------- #


def _rich_clip(clip_id="c1", start=0, duration=100):
    from src.features.edit.core.clip_params import ClipBlendMode
    from src.features.edit.core.timeline_v2 import ClipInstance

    clip = ClipInstance(
        clip_id=clip_id, asset_id="a1", track_id="v1",
        timeline_start=start, timeline_duration=duration,
        source_start=start, source_duration=duration,
        label_color="#ff5500",
        waveform_show_channels=True,
        fade_in_duration=10, fade_out_duration=10,
        color_stack_id="cs-1", color_shared=True,
        composite_id="comp-1", composite_bypassed=True,
        render_cache_id="rc-1",
        freeze_source_frame=7,
        blend_mode=ClipBlendMode.SCREEN,
        audio_gain_db=-3.5,
        _source_fps_num=24000, _source_fps_den=1001,
        _timeline_fps_num=24, _timeline_fps_den=1,
        _source_pts_index=(0.0, 1.0, 2.0),
    )
    object.__setattr__(clip, "_custom_probe", "keep-me")
    return clip


def _rich_sequence():
    from src.features.edit.core.timeline_v2 import EditTrack, TimelineSequence

    track = EditTrack(
        track_id="v1", name="V1", track_type="video", index=0,
        clips=(_rich_clip("c1", 0, 100), _rich_clip("c2", 100, 100)),
    )
    return TimelineSequence(
        timeline_id="s1", name="Rich", fps=Rational(24, 1),
        width=1920, height=1080, sample_rate=48000,
        video_tracks=(track,),
    )


def _assert_extended_fields_preserved(clip) -> None:
    from src.features.edit.core.clip_params import ClipBlendMode

    assert clip.label_color == "#ff5500"
    assert clip.waveform_show_channels is True
    assert clip.color_stack_id == "cs-1"
    assert clip.color_shared is True
    assert clip.composite_id == "comp-1"
    assert clip.composite_bypassed is True
    assert clip.render_cache_id == "rc-1"
    assert clip.freeze_source_frame == 7
    assert clip.blend_mode == ClipBlendMode.SCREEN
    assert clip.audio_gain_db == -3.5
    assert clip._source_fps_num == 24000
    assert clip._source_fps_den == 1001
    assert clip._timeline_fps_num == 24
    assert clip._timeline_fps_den == 1
    assert clip._source_pts_index == (0.0, 1.0, 2.0)
    # 动态属性不丢（"未知新字段不丢"）。
    assert getattr(clip, "_custom_probe", None) == "keep-me"


class TestAud18806:
    def test_split_clip_preserves_extended_fields(self):
        from src.features.edit.core.timeline_ops import split_clip

        seq = _rich_sequence()
        new_seq = split_clip(seq, "c1", 50)
        left = new_seq.get_clip_by_id("c1")
        right = next(
            c for c in new_seq.get_track_by_id("v1").clips
            if c.timeline_start == 50
        )
        assert left.timeline_duration == 50
        _assert_extended_fields_preserved(left)
        assert right.fade_in_duration == 0
        assert right.linked_clip_id is None
        _assert_extended_fields_preserved(right)

    def test_trim_clip_left_preserves_extended_fields(self):
        from src.features.edit.core.timeline_ops import trim_clip_left

        seq = _rich_sequence()
        new_seq = trim_clip_left(seq, "c1", 20)
        clip = new_seq.get_clip_by_id("c1")
        assert clip.timeline_start == 20
        assert clip.timeline_duration == 80
        _assert_extended_fields_preserved(clip)

    def test_trim_clip_right_preserves_extended_fields(self):
        from src.features.edit.core.timeline_ops import trim_clip_right

        seq = _rich_sequence()
        new_seq = trim_clip_right(seq, "c1", 80)
        clip = new_seq.get_clip_by_id("c1")
        assert clip.timeline_duration == 80
        _assert_extended_fields_preserved(clip)

    def test_move_clip_preserves_extended_fields(self):
        from src.features.edit.core.timeline_ops import move_clip

        seq = _rich_sequence()
        # c2 占 [100,200)，c1 移到 [200,300) 同轨不重叠。
        new_seq = move_clip(seq, "c1", "v1", 200)
        clip = new_seq.get_clip_by_id("c1")
        assert clip.timeline_start == 200
        _assert_extended_fields_preserved(clip)

    def test_add_transition_readd_preserves_params(self):
        from src.features.edit.core.timeline_ops import add_transition
        from src.features.edit.core.timeline_v2 import TransitionInstance

        seq = _rich_sequence()
        existing = TransitionInstance(
            transition_id="tr-1", transition_type="cross_dissolve",
            track_id="v1", left_clip_id="c1", right_clip_id="c2",
            center_frame=100, duration=12, alignment="centered",
            params={"amount": 0.7},
        )
        track = seq.get_track_by_id("v1")
        seq = seq.with_track_updated(track._rebuild(
            transitions=(existing,)
        ))
        new_seq = add_transition(seq, "c1", "c2", 12)
        transitions = new_seq.get_track_by_id("v1").transitions
        assert len(transitions) == 1
        assert transitions[0].transition_id == "tr-1"
        # AUD-18806：重加转场不再把已配置 params 清空。
        assert transitions[0].params == {"amount": 0.7}
