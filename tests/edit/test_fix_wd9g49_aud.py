"""wd9-g49 回归测试：批 D Major 16 条（AUD-13903..14106，ai_editing m-o 段）。

对应修复报告 docs/plans/2026-09-28-multi-machine-program/fixes/reports/wd9-g49.md。
逐条锁定各修复的根因行为，防止回归：

- AUD-13903/13904：GenerationService 宿主侧加锁 + close() 清扫非终态 Job
- AUD-13908：``_as_float`` 支持有理数帧率字符串（"30000/1001"）
- AUD-13915：MessageRecord.from_dict 的 timestamp 防御性转换
- AUD-13917：workspace_from_run_result 兼容真实 AgentRun/AgentCheckpoint 形状
- AUD-13919/13920：MissionStore.set_status 状态机校验 + 查询返回快照
- AUD-13923：model_router 移除幻影 reasoning 过滤维度
- AUD-14000：PlanStatus.INVALID 存在且 from_dict 容错回退不再自爆
- AUD-14003/14004：写入门 fail-closed + 恢复 artifact 不复活 writable/evidence
- AUD-14007：license "cleared" → CLEARED
- AUD-14009/14010：OCR _model_version 区分 stub/real + 双包名探测
"""

from __future__ import annotations

import dataclasses
import threading
from concurrent.futures import CancelledError
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.edit_core

from src.features.edit.ai_editing.agent_models import (
    AgentCheckpoint,
    AgentRun,
    AgentRunState,
)
from src.features.edit.ai_editing.generation_bin import GenerationBin
from src.features.edit.ai_editing.generation_models import (
    GeneratedAsset,
    GenerationConsent,
    GenerationModality,
    GenerationProvenance,
    GenerationRequest,
    GenerationResult,
)
from src.features.edit.ai_editing.generation_provider import ProviderRegistry
from src.features.edit.ai_editing.jobs import JobKind, JobRegistry, JobStatus
from src.features.edit.ai_editing.media_generation_service import GenerationService
from src.features.edit.ai_editing.media_materialization_pipeline import (
    GeneratedMediaProbeService,
    _as_float,
)
from src.features.edit.ai_editing.message_record import MessageRecord
from src.features.edit.ai_editing.mission_workspace import workspace_from_run_result
from src.features.edit.ai_editing.missions import (
    MISSION_ACTIVE,
    MISSION_CANCELLED,
    MISSION_FAILED,
    MISSION_PAUSED,
    MISSION_SUCCEEDED,
    MissionStore,
    SuccessCriterion,
)
from src.features.edit.ai_editing.model_router import (
    ModelRouteRequest,
    ModelRouter,
)
from src.features.edit.ai_editing.models import PlanStatus, ValidationReport
from src.features.edit.ai_editing.motion_graphic_artifacts import (
    RESTORED_EVIDENCE_FLAG,
    HtmlArtifact,
    MotionGraphicBuildRequest,
    can_write_artifact,
    scene_is_stale,
)
from src.features.edit.ai_editing.music_policy import LicenseStatus, MusicPolicy
from src.features.edit.ai_editing.ocr_provider import (
    ModelPackNotInstalledError,
    OcrProvider,
)
from src.features.edit.ai_editing.provider_profiles import AiProviderProfile


# ---------------------------------------------------------------------- #
# helpers（生成服务）
# ---------------------------------------------------------------------- #


def _full_consent(**kw) -> GenerationConsent:
    base = dict(
        confirmed=True, provider_id="blocking-stub",
        cost_acknowledged=True, upload_acknowledged=True,
        copyright_acknowledged=True,
    )
    base.update(kw)
    return GenerationConsent(**base)


def _request(request_id: str = "req1") -> GenerationRequest:
    return GenerationRequest(
        request_id=request_id,
        modality=GenerationModality.VIDEO,
        prompt="产品特写",
        consent=_full_consent(),
    )


def _candidate(request_id: str, provider_id: str = "blocking-stub") -> GeneratedAsset:
    return GeneratedAsset(
        asset_id=f"gen-{request_id}-c1",
        request_id=request_id,
        modality=GenerationModality.VIDEO,
        provenance=GenerationProvenance(
            provider_id=provider_id,
            model_id="test-model",
            model_version="1.0",
            input_prompt_hash="deadbeef",
        ),
    )


class _BlockingProvider:
    """generate() 阻塞到 release 被 set，用于构造 QUEUED/RUNNING 僵尸场景。"""

    provider_id = "blocking-stub"

    def __init__(self, release: threading.Event) -> None:
        self._release = release
        self.entered = threading.Event()

    @property
    def supported_modalities(self) -> tuple[GenerationModality, ...]:
        return (GenerationModality.VIDEO,)

    def generate(self, request: GenerationRequest, *, progress=None) -> GenerationResult:
        self.entered.set()
        self._release.wait(timeout=10)
        return GenerationResult(
            request_id=request.request_id,
            candidates=(_candidate(request.request_id),),
        )


def _service(provider, *, jobs=None) -> GenerationService:
    registry = ProviderRegistry()
    registry.register(provider, preferred=True)
    return GenerationService(
        registry=registry,
        bin=GenerationBin(),
        jobs=jobs if jobs is not None else JobRegistry(),
    )


# ====================================================================== #
# AUD-13903：宿主侧锁（registry 事件序号 / GenerationBin 枚举 / 池单例）
# ====================================================================== #


class TestAud13903HostSideLocking:
    def test_bin_enumeration_survives_concurrent_writes(self) -> None:
        """worker 并发 add_candidates 与 UI 枚举 by_request 不再互相竞争。"""
        svc = _service(_BlockingProvider(threading.Event()))
        errors: list[Exception] = []

        def adder(tag: int) -> None:
            try:
                for i in range(50):
                    cand = dataclasses.replace(
                        _candidate(f"req-{tag}-{i}"), asset_id=f"a-{tag}-{i}",
                    )
                    svc.generation_bin.add_candidates((cand,))
            except Exception as exc:  # noqa: BLE001 — 测试线程错误需收集
                errors.append(exc)

        threads = [threading.Thread(target=adder, args=(t,)) for t in range(8)]
        for t in threads:
            t.start()
        for _ in range(200):
            svc.generation_bin.by_request("req-0-0")  # 枚举方
            svc.generation_bin.all_assets()
        for t in threads:
            t.join(timeout=5)
        assert errors == []
        assert svc.generation_bin.count == 400

    def test_registry_event_sequences_unique_under_threads(self) -> None:
        """并发 _record_event 的事件序号严格递增且不重复（游标不漏事件）。"""
        svc = GenerationService(registry=ProviderRegistry())
        jobs = svc.job_registry
        handles = [jobs.submit(JobKind.GENERATION) for _ in range(40)]

        def drive(start: int) -> None:
            for handle in handles[start::4]:
                handle.update_progress(0.5, "tick")
                handle.complete({})

        threads = [threading.Thread(target=drive, args=(i,)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)
        sequences = [event.sequence for event in jobs.events_since(0)]
        assert len(sequences) == len(set(sequences))
        assert sequences == sorted(sequences)

    def test_worker_pool_single_instance_under_concurrent_access(self) -> None:
        """并发首调不再重复构建线程池（旧 check-then-act 泄漏输家 executor）。"""
        svc = _service(_BlockingProvider(threading.Event()))
        pools: list[object] = []

        def grab() -> None:
            pools.append(svc.worker_pool)

        threads = [threading.Thread(target=grab) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)
        assert len(pools) == 8
        assert len({id(pool) for pool in pools}) == 1

    def test_foreign_registry_falls_back_to_single_worker(self) -> None:
        """外部注入的（无法加锁的）registry → 池退回单 worker（jobs.py P4 契约）。"""
        svc = _service(_BlockingProvider(threading.Event()), jobs=JobRegistry())
        assert svc.worker_pool._executor._max_workers == 1

    def test_default_registry_keeps_pool_concurrency(self) -> None:
        """service 自建 _LockedJobRegistry 时保留池默认并发（锁已就位）。"""
        registry = ProviderRegistry()
        registry.register(_BlockingProvider(threading.Event()), preferred=True)
        svc = GenerationService(registry=registry)
        assert svc.worker_pool._executor._max_workers >= 2


# ====================================================================== #
# AUD-13904：close() 清扫非终态 Job
# ====================================================================== #


class TestAud13904CloseSweepsActiveJobs:
    def test_close_fails_queued_and_running_jobs(self) -> None:
        release = threading.Event()
        provider = _BlockingProvider(release)
        svc = _service(provider)  # 外部 registry → 单 worker：第二个 job 排队
        job1, diags = svc.request_generation_async(_request("req1"))
        assert job1 is not None and diags == []
        assert provider.entered.wait(timeout=2)  # job1 占住唯一 worker
        job2, _ = svc.request_generation_async(_request("req2"))
        assert job2 is not None
        assert job2.status == JobStatus.QUEUED

        pool = svc.worker_pool
        future1 = pool.future(job1.job_id)
        future2 = pool.future(job2.job_id)

        svc.close()

        # 未启动的 job 不再永久 QUEUED；运行中的 job 也被显式终结。
        for job in (job1, job2):
            assert job.status == JobStatus.FAILED
            assert job.error_code == "JOB_SHUTDOWN"
        assert svc.job_registry.list_active() == []
        assert svc.job_registry.metrics().active == 0

        # 迟到的 provider 结果不得复活状态，也不得把候选写进素材箱。
        release.set()
        # 未启动的 future 被 cancel_futures=True 取消——CancelledError 是预期终点。
        if future2 is not None:
            with pytest.raises(CancelledError):
                future2.result(timeout=5)
        if future1 is not None:
            future1.result(timeout=5)
        assert job1.status == JobStatus.FAILED
        assert svc.generation_bin.by_request("req1") == ()
        assert svc.generation_bin.by_request("req2") == ()

    def test_close_without_workers_is_idempotent(self) -> None:
        svc = _service(_BlockingProvider(threading.Event()))
        svc.close()
        svc.close()  # 二次关闭不抛


# ====================================================================== #
# AUD-13908：有理数 fps 字符串
# ====================================================================== #


class TestAud13908RationalFps:
    def test_as_float_parses_rational_strings(self) -> None:
        assert _as_float("30000/1001") == pytest.approx(30000 / 1001)
        assert _as_float("30/1") == 30.0
        assert _as_float(29.97) == 29.97
        assert _as_float("29.97") == 29.97

    def test_as_float_rejects_garbage(self) -> None:
        assert _as_float("30/0") is None  # Fraction 除零
        assert _as_float("abc") is None
        assert _as_float(None) is None
        assert _as_float({"fps": 30}) is None

    def test_probe_reports_ffprobe_rational_fps(self, tmp_path) -> None:
        media = tmp_path / "gen.mp4"
        media.write_bytes(b"0123456789")
        service = GeneratedMediaProbeService(
            probe_fn=lambda path: {
                "mime_type": "video/mp4",
                "duration_seconds": 3.5,
                "fps": "30000/1001",
            },
        )
        probe = service.probe(str(media))
        assert probe.fps == pytest.approx(30000 / 1001)
        assert probe.duration_seconds == 3.5


# ====================================================================== #
# AUD-13915：timestamp 防御性转换
# ====================================================================== #


class TestAud13915TimestampCoercion:
    def test_corrupted_timestamp_falls_back_to_zero(self) -> None:
        assert MessageRecord.from_dict(
            {"message_id": "m1", "timestamp": "abc"},
        ).timestamp == 0.0
        assert MessageRecord.from_dict(
            {"message_id": "m2", "timestamp": None},
        ).timestamp == 0.0
        assert MessageRecord.from_dict(
            {"message_id": "m3", "timestamp": {"t": 1}},
        ).timestamp == 0.0

    def test_valid_timestamp_passthrough(self) -> None:
        assert MessageRecord.from_dict(
            {"message_id": "m4", "timestamp": 123.5},
        ).timestamp == 123.5
        assert MessageRecord.from_dict({"message_id": "m5"}).timestamp == 0.0


# ====================================================================== #
# AUD-13917：真实 AgentRun/AgentCheckpoint 形状
# ====================================================================== #


class TestAud13917RealRunShape:
    def test_real_agent_run_and_checkpoint(self) -> None:
        run = AgentRun(
            run_id="r1", task_id="t1",
            state=AgentRunState.EXECUTING, state_reason="building",
        )
        cp = AgentCheckpoint(
            run_id="r1", state=AgentRunState.EXECUTING,
            completed_steps=("s0", "s1"),
        )
        # 真实类型没有 plan/diagnostics/rollback_failures 属性——不得抛 AttributeError。
        view = workspace_from_run_result(
            SimpleNamespace(state=run.state, state_reason=run.state_reason, checkpoint=cp),
        )
        assert view["status"] == "running"
        assert view["phase"] == "executing"
        assert view["completed_steps"] == 2
        assert view["run_id"] == "r1"
        assert view["reason"] == "building"
        assert view["cost_units"] == 0.0

    def test_agent_run_state_mapped_to_view_statuses(self) -> None:
        def view_for(state: str) -> str:
            run = AgentRun(run_id="r2", task_id="t2", state=state)
            return workspace_from_run_result(
                SimpleNamespace(state=run.state, state_reason="", checkpoint=None),
            )["status"]

        assert view_for(AgentRunState.WAITING_USER_INPUT) == "waiting_for_user"
        assert view_for(AgentRunState.WAITING_APPROVAL) == "waiting_for_user"
        assert view_for(AgentRunState.VERIFYING) == "running"
        assert view_for(AgentRunState.FAILED) == "failed"
        assert view_for(AgentRunState.CANCELLED) == "cancelled"

    def test_legacy_mock_shape_still_works(self) -> None:
        """OS-06 既有 mock 契约（tests/ai/test_os06_mission_workspace.py）不破坏。"""
        step = SimpleNamespace(
            step_id="s1", tool_name="insert_clip",
            writes_timeline=True, requires_approval=True,
        )
        plan = SimpleNamespace(steps=(step,), plan_hash="ph")
        cp = SimpleNamespace(
            run_id="run-abc", phase=SimpleNamespace(value="act"),
            current_step_id="s1", completed_step_ids=("s0",),
            cost_units=1.5, reason="",
        )
        view = workspace_from_run_result(SimpleNamespace(
            status=SimpleNamespace(value="running"), checkpoint=cp, plan=plan,
            diagnostics=("step done",), rollback_failures=(),
        ))
        assert view["status"] == "running"
        assert view["phase"] == "act"
        assert view["current_tool_name"] == "insert_clip"
        assert view["completed_steps"] == 1
        assert view["total_steps"] == 1
        assert view["cost_units"] == 1.5
        assert view["risk_flags"]["writes_timeline"] == 1


# ====================================================================== #
# AUD-13919 / AUD-13920：MissionStore 状态机与快照
# ====================================================================== #


class TestAud13919SetStatusValidation:
    def _store_with_mission(self) -> tuple[MissionStore, str]:
        store = MissionStore()
        mission = store.create(project_id="p1", objective_text="goal")
        return store, mission.mission_id

    def test_misspelled_status_rejected(self) -> None:
        store, mission_id = self._store_with_mission()
        with pytest.raises(ValueError):
            store.set_status(mission_id, "actve")

    def test_terminal_mission_cannot_be_revived(self) -> None:
        store, mission_id = self._store_with_mission()
        store.set_status(mission_id, MISSION_FAILED, reason="boom")
        with pytest.raises(ValueError):
            store.set_status(mission_id, MISSION_ACTIVE)
        with pytest.raises(ValueError):
            store.set_status(mission_id, MISSION_SUCCEEDED, reason="late pass")

    def test_legal_active_transition_still_works(self) -> None:
        store, mission_id = self._store_with_mission()
        store.set_status(mission_id, MISSION_PAUSED, reason="user")
        assert store.get(mission_id).status == MISSION_PAUSED
        assert store.get(mission_id).termination_reason == "user"

    def test_auto_success_via_update_criterion_unchanged(self) -> None:
        store = MissionStore()
        mission = store.create(
            project_id="p1", objective_text="g",
            success_criteria=(SuccessCriterion(check="gate"),),
        )
        store.update_criterion(mission.mission_id, "gate", met=True)
        assert store.get(mission.mission_id).status == MISSION_SUCCEEDED


class TestAud13920SnapshotQueries:
    def test_get_returns_snapshot_not_live_reference(self) -> None:
        store = MissionStore()
        mission = store.create(project_id="p1", objective_text="g")
        leaked = store.get(mission.mission_id)
        assert leaked is not None
        leaked.status = MISSION_FAILED  # 锁外改写快照
        assert store.get(mission.mission_id).status == MISSION_ACTIVE

    def test_create_returns_snapshot(self) -> None:
        store = MissionStore()
        mission = store.create(project_id="p1", objective_text="g")
        mission.status = MISSION_FAILED
        assert store.get(mission.mission_id).status == MISSION_ACTIVE

    def test_store_mutations_visible_through_queries(self) -> None:
        store = MissionStore()
        mission = store.create(
            project_id="p1", objective_text="g",
            success_criteria=(SuccessCriterion(check="gate"),),
        )
        decision = store.add_decision(mission.mission_id, kind="user_accept")
        refreshed = store.get(mission.mission_id)
        assert refreshed is not None
        assert [d.decision_id for d in refreshed.decisions] == [decision.decision_id]
        store.update_criterion(mission.mission_id, "gate", met=True)
        refreshed = store.get(mission.mission_id)
        assert refreshed is not None
        assert refreshed.success_criteria[0].met is True

    def test_children_of_returns_snapshots(self) -> None:
        store = MissionStore()
        parent = store.create(project_id="p1", objective_text="parent")
        store.create(project_id="p1", objective_text="child", parent_run_id=parent.mission_id)
        children = store.children_of(parent.mission_id)
        assert len(children) == 1
        children[0].status = MISSION_CANCELLED
        assert store.children_of(parent.mission_id)[0].status == MISSION_ACTIVE


# ====================================================================== #
# AUD-13923：移除幻影 reasoning 过滤维度
# ====================================================================== #


class TestAud13923PhantomReasoningRemoved:
    def test_request_no_longer_carries_phantom_field(self) -> None:
        request = ModelRouteRequest(task="plan")
        assert not hasattr(request, "reasoning_effort")
        field_names = {f.name for f in dataclasses.fields(ModelRouteRequest)}
        assert "reasoning_effort" not in field_names

    def test_router_no_phantom_filter_method(self) -> None:
        assert not hasattr(ModelRouter, "_supports_reasoning")

    def test_routing_still_selects_by_modality(self) -> None:
        profile = AiProviderProfile(profile_id="p1", display_name="P1")
        router = ModelRouter((profile,))
        route = router.route(ModelRouteRequest(task="plan", allow_fallback=True))
        assert route.profile.profile_id == "p1"


# ====================================================================== #
# AUD-14000：PlanStatus.INVALID
# ====================================================================== #


class TestAud14000PlanStatusInvalid:
    def test_invalid_member_exists(self) -> None:
        assert PlanStatus("invalid") is PlanStatus.INVALID

    def test_from_dict_unknown_status_degrades_to_invalid(self) -> None:
        report = ValidationReport.from_dict(
            {"plan_id": "p1", "status": "from-the-year-3000", "base_revision": 1},
        )
        assert report.status is PlanStatus.INVALID  # 旧代码此处抛 AttributeError

    def test_from_dict_known_status_roundtrip(self) -> None:
        report = ValidationReport.from_dict(
            {"plan_id": "p1", "status": "valid", "base_revision": 2},
        )
        assert report.status is PlanStatus.VALID
        assert report.base_revision == 2


# ====================================================================== #
# AUD-14003 / AUD-14004：写入门 fail-closed + 恢复不复活
# ====================================================================== #


def _mg_request(**overrides):
    base = dict(
        task_id="t1", scene_id="s1", scene_revision=3,
        context_fingerprint="fp1", canvas_width=1920, canvas_height=1080,
    )
    base.update(overrides)
    return MotionGraphicBuildRequest(**base)


def _staged_artifact(**overrides):
    base = dict(
        html_payload_ref="store://abc", sanitized_html_hash="h1",
        page_count=2, render_evidence={"captured_frames": 8},
    )
    base.update(overrides)
    return HtmlArtifact(**base)


class TestAud14003StaleGateFailClosed:
    def test_confirmed_artifact_without_basis_is_stale(self) -> None:
        """审计场景：确认后（writable）但空 fingerprint + 未传 revision → 过期。"""
        req = _mg_request(context_fingerprint="")  # 宿主没填指纹
        art = _staged_artifact(writable=True)      # 已确认，approved_scene_hash 空
        assert scene_is_stale(req, art) is True

    def test_stamped_artifact_without_request_fingerprint_is_stale(self) -> None:
        """确认盖了 approved_scene_hash 戳但请求侧指纹为空 → 无法对比 → 过期。"""
        req = _mg_request(context_fingerprint="")
        art = _staged_artifact(approved_scene_hash="fp1")
        assert scene_is_stale(req, art) is True

    def test_unconfirmed_artifact_without_basis_not_predicated_stale(self) -> None:
        """未确认的 artifact 不做无依据的有罪推定（写入由 staged/evidence/writable 门拦截）。"""
        req = _mg_request(context_fingerprint="")
        art = _staged_artifact()  # writable=False，approved_scene_hash 空
        assert scene_is_stale(req, art) is False

    def test_fingerprint_only_matching_basis_passes(self) -> None:
        req = _mg_request(context_fingerprint="fp1")
        art = _staged_artifact(approved_scene_hash="fp1")
        assert scene_is_stale(req, art) is False
        assert scene_is_stale(_mg_request(context_fingerprint="fp2"), art) is True

    def test_revision_only_basis_passes(self) -> None:
        req = _mg_request(context_fingerprint="")  # 无指纹
        art = _staged_artifact()  # 无 approved_scene_hash
        assert scene_is_stale(req, art, current_scene_revision=3) is False
        assert scene_is_stale(req, art, current_scene_revision=4) is True

    def test_revision_match_but_fingerprint_mismatch_is_stale(self) -> None:
        req = _mg_request(context_fingerprint="fp2")
        art = _staged_artifact(approved_scene_hash="fp1")
        assert scene_is_stale(req, art, current_scene_revision=3) is True

    def test_write_gate_blocks_without_basis_with_clear_reason(self) -> None:
        art = _staged_artifact(writable=True)  # approved_scene_hash 为空
        ok, reason = can_write_artifact(_mg_request(context_fingerprint="fp1"), art)
        assert ok is False
        assert "缺少场景版本依据" in reason

    def test_write_gate_allows_with_revision_basis(self) -> None:
        art = _staged_artifact(writable=True)
        ok, reason = can_write_artifact(
            _mg_request(context_fingerprint=""), art, current_scene_revision=3,
        )
        assert ok is True
        assert reason == ""


class TestAud14004RestoredArtifactNotTrusted:
    def test_from_dict_forces_writable_false(self) -> None:
        art = _staged_artifact(writable=True, approved_scene_hash="fp1")
        restored = HtmlArtifact.from_dict(art.to_dict())
        assert restored.writable is False  # 恢复后必须重新确认

    def test_restored_evidence_flagged_and_downgraded(self) -> None:
        art = _staged_artifact(writable=True)
        restored = HtmlArtifact.from_dict(art.to_dict())
        assert restored.render_evidence.get(RESTORED_EVIDENCE_FLAG) is True
        assert restored.has_verification_evidence is False

    def test_restored_artifact_blocked_at_write_gate(self) -> None:
        art = _staged_artifact(writable=True, approved_scene_hash="fp1")
        restored = HtmlArtifact.from_dict(art.to_dict())
        for revision in (None, 3):
            writable = dataclasses.replace(restored, writable=True)  # 最坏情况：宿主硬翻 writable
            ok, reason = can_write_artifact(_mg_request(), writable, current_scene_revision=revision)
            assert ok is False
            assert "验证" in reason  # 卡在"缺少真实浏览器验证证据"

    def test_fresh_evidence_still_trusted(self) -> None:
        art = _staged_artifact(writable=True, approved_scene_hash="fp1")
        assert art.has_verification_evidence is True
        ok, _ = can_write_artifact(_mg_request(), art, current_scene_revision=3)
        assert ok is True


# ====================================================================== #
# AUD-14007：license "cleared" → CLEARED
# ====================================================================== #


class _FakeMediaAsset:
    def __init__(self, metadata: dict) -> None:
        self.asset_id = "a1"
        self.display_name = "Song"
        self.metadata = metadata


class TestAud14007ClearedLicense:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("cleared", LicenseStatus.CLEARED),
            ("licensed", LicenseStatus.LICENSED),
            ("授权", LicenseStatus.LICENSED),
            ("已授权", LicenseStatus.LICENSED),
            ("pending", LicenseStatus.PENDING),
            ("unknown", LicenseStatus.UNKNOWN),
            ("garbage", LicenseStatus.UNKNOWN),
        ],
    )
    def test_license_mapping(self, raw: str, expected: LicenseStatus) -> None:
        policy = MusicPolicy()
        music = policy.register_from_media_asset(
            _FakeMediaAsset({"music_license": raw}),
        )
        assert music.license_status is expected

    def test_cleared_music_is_applicable_but_not_licensed(self) -> None:
        policy = MusicPolicy()
        music = policy.register_from_media_asset(
            _FakeMediaAsset({"music_license": "cleared"}),
        )
        assert music.is_applicable is True  # CLEARED 仍可应用
        assert music.license_status is not LicenseStatus.LICENSED  # 但语义不被吞并

    def test_legacy_license_key_also_mapped(self) -> None:
        policy = MusicPolicy()
        music = policy.register_from_media_asset(_FakeMediaAsset({"license": "cleared"}))
        assert music.license_status is LicenseStatus.CLEARED


# ====================================================================== #
# AUD-14009 / AUD-14010：OCR stub/real 标识 + 双包探测
# ====================================================================== #


class TestAud14009ModelVersionDistinguishesStub:
    def test_stub_env_marks_stub_even_when_package_installed(self, monkeypatch) -> None:
        monkeypatch.delenv("TOPOS_OCR_REAL", raising=False)
        # 旧 bug：只要包可 import，stub 运行也被标成 rapidocr-<version>。
        assert OcrProvider._model_version() == "rapidocr-stub-v1"

    def test_real_env_differs_from_stub(self, monkeypatch) -> None:
        monkeypatch.setenv("TOPOS_OCR_REAL", "1")
        version = OcrProvider._model_version()
        assert version != "rapidocr-stub-v1"
        assert version.startswith("rapidocr")


class TestAud14010DualPackageProbe:
    def _patch_find_spec(self, monkeypatch, available: set[str]) -> None:
        monkeypatch.setattr(
            "src.features.edit.ai_editing.ocr_provider.importlib.util.find_spec",
            lambda name: object() if name in available else None,
        )

    def test_legacy_package_only_environment_accepted(self, monkeypatch) -> None:
        """只装旧包 rapidocr_onnxruntime 的环境不再在入口被拒。"""
        from src.features.edit.ai_editing.analysis_models import AssetFingerprint

        self._patch_find_spec(monkeypatch, {"rapidocr_onnxruntime"})
        monkeypatch.delenv("TOPOS_OCR_REAL", raising=False)
        provider = OcrProvider(
            sampler=None,
            shot_spans_provider=lambda asset_id: (),
            total_frames_provider=lambda asset_id: 0,
        )
        analysis, _segments = provider.analyze("a1", AssetFingerprint(), fps="24/1")
        assert analysis.asset_id == "a1"
        assert analysis.provenance.model == "rapidocr-stub-v1"

    def test_both_packages_missing_raises(self, monkeypatch) -> None:
        from src.features.edit.ai_editing.analysis_models import AssetFingerprint

        self._patch_find_spec(monkeypatch, set())
        provider = OcrProvider(
            sampler=None,
            shot_spans_provider=lambda asset_id: (),
            total_frames_provider=lambda asset_id: 0,
        )
        with pytest.raises(ModelPackNotInstalledError) as excinfo:
            provider.analyze("a1", AssetFingerprint(), fps="24/1")
        assert "rapidocr_onnxruntime" in str(excinfo.value)  # 错误信息提及两个包名

    def test_new_package_only_environment_accepted(self, monkeypatch) -> None:
        from src.features.edit.ai_editing.analysis_models import AssetFingerprint

        self._patch_find_spec(monkeypatch, {"rapidocr"})
        monkeypatch.delenv("TOPOS_OCR_REAL", raising=False)
        provider = OcrProvider(
            sampler=None,
            shot_spans_provider=lambda asset_id: (),
            total_frames_provider=lambda asset_id: 0,
        )
        analysis, _ = provider.analyze("a1", AssetFingerprint(), fps="24/1")
        assert analysis.provenance.model == "rapidocr-stub-v1"
