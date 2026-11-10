"""wd8-g46 批 D（AUD-12903..AUD-13109，白名单文件）修复回归测试。

每条测试对应 REMEDIATION_PLAN.md 的一个 Major 条目，锁定修复后的根因行为：

- AUD-12903 调度路径 work 终结 asset_job + notify_finished + 汇总 job 收尾
- AUD-12904 AnalysisService 共享状态加锁（并发枚举不抛 RuntimeError）
- AUD-12905 调度路径完成计数原子（并发 worker 不丢更新、不双重收尾）
- AUD-12906 AnalysisBudget 在执行路径强制（素材数 preflight + 时长/成本检查点）
- AUD-12909 ApprovalScope.matches 签发侧强约束缺省即拒绝
- AUD-13001 路由 _active_profile 注册表异常留痕（warning + 堆栈）
- AUD-13009 config 在 start_session 前可访问（默认空配置）
- AUD-13010 cancel 传播失败可见（error_message + warning）
- AUD-13011 revision 快照失败显式降级提示（不再无声 base 0）
- AUD-13017 persist() 的 mkdir 纳入异常保护（只读目录返回 False）
- AUD-13018 load()/restore_catalog 容忍损坏 payload（不使工程打开崩溃）
- AUD-13019 cache-only 会话改名落盘
- AUD-13020 本机缓存写前合并（多实例不互相覆盖）+ 唯一临时文件名
- AUD-13102 rollback 异常转结构化 rollback_failures（apply 不裸抛）
- AUD-13103 预览-提交一致性门禁比较 operation_summaries
- AUD-13109 propose_patch_from_text 关键词匹配大小写不敏感
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
from types import SimpleNamespace
from typing import Any

import pytest

pytestmark = pytest.mark.edit_core

from src.features.edit.ai_editing.analysis_models import (
    AnalysisProvenance,
    AnalysisProvider,
    AssetAnalysis,
    AssetFingerprint,
    analysis_to_segments,
)
from src.features.edit.ai_editing.analysis_request import (
    AnalysisRecipe,
    AnalysisRequest,
    AnalysisBudget,
    RequestState,
)
from src.features.edit.ai_editing.analysis_scheduler import (
    AnalysisJobScheduler,
    ResourceLane,
)
from src.features.edit.ai_editing.analysis_service import AnalysisService
from src.features.edit.ai_editing.approval_service import (
    ApprovalScope,
    ApprovalService,
    GrantScopeMismatch,
)
from src.features.edit.ai_editing.editorial_models import Platform, SegmentKind
from src.features.edit.ai_editing.jobs import JobStatus


# --------------------------------------------------------------------------- #
# shared helpers
# --------------------------------------------------------------------------- #


class _StubProvider:
    """Deterministic local provider: always produces one SHOT segment."""

    provider_id = "stub_local"
    supported_kinds = (SegmentKind.SHOT,)

    def analyze(
        self, asset_id: str, fingerprint: AssetFingerprint, *,
        fps: str, progress: Any | None = None,
    ) -> tuple[AssetAnalysis, tuple[Any, ...]]:
        analysis = AssetAnalysis(
            asset_id=asset_id, fingerprint=fingerprint, fps=fps,
            scene_cuts=({"frame": 0, "confidence": 1.0, "kind": "hard_cut"},),
            provenance=AnalysisProvenance(provider=self.provider_id, model="test"),
        )
        return analysis, tuple(analysis_to_segments(analysis))


def _confirmed_request(
    svc: AnalysisService, asset_ids: list[str], *,
    budget: AnalysisBudget | None = None,
) -> AnalysisRequest:
    req = AnalysisRequest.create(
        "p1", asset_ids, recipe_id=AnalysisRecipe.FAST_LOCAL,
        provider_ids=("stub_local",), budget=budget,
    )
    svc.propose_analysis(req)
    return svc.confirm_analysis(req.request_id, confirmed_at=1000.0)


# --------------------------------------------------------------------------- #
# AUD-12903 — scheduler work 契约：终结 asset_job + notify_finished + 汇总收尾
# --------------------------------------------------------------------------- #


def test_aud12903_scheduler_work_finalizes_jobs_and_releases_capacity() -> None:
    svc = AnalysisService()
    svc.register(_StubProvider())
    sched = AnalysisJobScheduler()  # 同步出队（测试确定性）

    req = _confirmed_request(svc, ["a1", "a2", "a3", "a4"])
    summary, err = svc.execute_analysis(req.request_id, scheduler=sched)
    assert err == ""

    # request 收尾；汇总 job 不再永久 QUEUED
    assert svc.get_request(req.request_id).state == RequestState.COMPLETED
    assert summary is not None
    assert summary.status == JobStatus.SUCCEEDED
    assert summary.result_manifest["assets"] == 4

    # 每个 asset job 都被驱动到终态（成功；带 recipe 后缀的是调度路径 asset job）
    asset_jobs = [
        job for job in svc.jobs.list_all()
        if job.label.startswith("Analyze ") and job.label.endswith("(fast_local)")
    ]
    assert len(asset_jobs) == 4
    assert all(job.status == JobStatus.SUCCEEDED for job in asset_jobs)

    # 调度容量被释放：无残留 active/pending → is_idle 为真
    assert sched.is_idle()
    assert sched.active_count(ResourceLane.ANALYSIS_LOCAL) == 0
    assert sched.pending_count(ResourceLane.ANALYSIS_LOCAL) == 0

    # 容量未泄漏的直接后果：后续请求仍可执行
    req2 = _confirmed_request(svc, ["b1", "b2", "b3"])
    summary2, err2 = svc.execute_analysis(req2.request_id, scheduler=sched)
    assert err2 == ""
    assert svc.get_request(req2.request_id).state == RequestState.COMPLETED
    assert summary2 is not None and summary2.status == JobStatus.SUCCEEDED


def test_aud12903_pending_cancel_finalizes_summary_job() -> None:
    """播放门控下 pending job 被取消 → request 与汇总 job 仍收尾。"""
    svc = AnalysisService()
    svc.register(_StubProvider())
    sched = AnalysisJobScheduler()
    sched.set_playback_active(True)

    req = _confirmed_request(svc, ["a1", "a2"])
    summary, err = svc.execute_analysis(req.request_id, scheduler=sched)
    assert err == ""
    assert svc.get_request(req.request_id).state == RequestState.RUNNING
    assert summary is not None and summary.status == JobStatus.QUEUED

    sched.cancel_all()

    final = svc.get_request(req.request_id)
    # R06（媒体池复查）：取消与失败三分——全部 asset 被取消（无失败）
    # → request/summary 收尾 CANCELLED（不再与失败混淆）
    assert final.state == RequestState.CANCELLED
    assert summary.status == JobStatus.CANCELLED


# --------------------------------------------------------------------------- #
# AUD-12904 — 共享状态加锁
# --------------------------------------------------------------------------- #


def test_aud12904_concurrent_ingest_and_enumerate_is_locked() -> None:
    svc = AnalysisService()
    svc.register(_StubProvider())
    provider = _StubProvider()
    errors: list[Exception] = []

    def _writer(tag: str) -> None:
        try:
            for index in range(40):
                fingerprint = AssetFingerprint()
                analysis, segments = provider.analyze(
                    f"{tag}{index}", fingerprint, fps="24/1",
                )
                svc.ingest(f"{tag}{index}", analysis, segments)
        except Exception as exc:  # pragma: no cover — 只在回归时触发
            errors.append(exc)

    threads = [
        threading.Thread(target=_writer, args=(tag,)) for tag in ("w1", "w2", "w3")
    ]
    for thread in threads:
        thread.start()
    # UI 线程同时枚举（旧行为会抛 "dictionary changed size during iteration"）
    for _ in range(200):
        svc.known_asset_ids()
        svc.list_requests()
        svc.export_all()
    for thread in threads:
        thread.join(timeout=5.0)
        assert not thread.is_alive()

    assert errors == []
    assert len(svc.known_asset_ids()) == 120


# --------------------------------------------------------------------------- #
# AUD-12905 — 完成计数原子
# --------------------------------------------------------------------------- #


def test_aud12905_async_workers_count_every_asset_exactly_once() -> None:
    svc = AnalysisService()
    svc.register(_StubProvider())
    sched = AnalysisJobScheduler(asynchronous=True)  # 真实线程池（并发 2+1）
    request_done: list[tuple[str, bool]] = []

    try:
        req = _confirmed_request(svc, [f"a{i}" for i in range(8)])
        summary, err = svc.execute_analysis(
            req.request_id, scheduler=sched,
            on_request_done=lambda rid, ok: request_done.append((rid, ok)),
        )
        assert err == ""
        assert summary is not None

        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            if svc.get_request(req.request_id).state.is_terminal:
                break
            time.sleep(0.02)
        # 丢失更新会使 request 永久卡 RUNNING → 这里会超时失败
        assert svc.get_request(req.request_id).state == RequestState.COMPLETED
        # 恰好一次收尾回调（双重触发会被捕获）
        assert request_done == [(req.request_id, True)]
        assert summary.status == JobStatus.SUCCEEDED
        assert summary.result_manifest["assets"] == 8
    finally:
        sched.shutdown(wait=True)


# --------------------------------------------------------------------------- #
# AUD-12906 — AnalysisBudget 强制执行
# --------------------------------------------------------------------------- #


def test_aud12906_max_assets_preflight_rejects_request() -> None:
    svc = AnalysisService()
    svc.register(_StubProvider())
    req = _confirmed_request(
        svc, ["a1", "a2", "a3"], budget=AnalysisBudget(max_assets=2),
    )
    job, err = svc.execute_analysis(req.request_id)
    assert job is None
    assert "max_assets" in err and "budget" in err.lower()
    assert svc.get_request(req.request_id).state == RequestState.FAILED


def test_aud12906_duration_deadline_stops_execution() -> None:
    svc = AnalysisService()
    svc.register(_StubProvider())
    # max_duration_seconds=0 → 第一个检查点即超限：所有 asset 被跳过并记错
    req = _confirmed_request(
        svc, ["a1", "a2"], budget=AnalysisBudget(max_duration_seconds=0.0),
    )
    job, err = svc.execute_analysis(req.request_id)
    assert job is None
    assert "max_duration_seconds" in err
    assert svc.get_request(req.request_id).state == RequestState.FAILED


def test_aud12906_budget_checkpoints_unit_semantics() -> None:
    svc = AnalysisService()
    req = AnalysisRequest.create(
        "p1", ["a1"], recipe_id=AnalysisRecipe.FAST_LOCAL,
        budget=AnalysisBudget(
            max_duration_seconds=60.0, max_cost_units=10.0,
        ),
    )
    # 未超限：无违例
    assert svc._budget_violation(req, time.monotonic(), 0.0) is None
    # 成本达到上限：不再启动新 asset
    violation = svc._budget_violation(req, time.monotonic(), 10.0)
    assert violation is not None and "max_cost_units" in violation
    # 超过时长截止：不再启动新 asset
    violation = svc._budget_violation(req, time.monotonic() - 61.0, 0.0)
    assert violation is not None and "max_duration_seconds" in violation


# --------------------------------------------------------------------------- #
# AUD-12909 — ApprovalScope.matches 签发侧强约束
# --------------------------------------------------------------------------- #


def _commit_scope(**overrides: Any) -> ApprovalScope:
    base = dict(
        action="commit_plan", project_id="p1", timeline_id="t1",
        base_revision=7, plan_hash="h1", diff_hash=None,
    )
    base.update(overrides)
    return ApprovalScope(**base)


def test_aud12909_expected_missing_bound_diff_hash_rejected() -> None:
    svc = ApprovalService()
    grant = svc.issue_after_user_confirmation(_commit_scope(diff_hash="diff-a"))
    # 消费方在 expected 里缺省 diff_hash → 必须拒绝（旧行为：跳过比较放行）
    with pytest.raises(GrantScopeMismatch):
        svc.consume(grant.grant_id, _commit_scope())


def test_aud12909_expected_missing_bound_fields_rejected() -> None:
    svc = ApprovalService()
    # 签发侧绑定 timeline/base_revision/plan_hash，expected 全部省略 → 拒绝
    grant = svc.issue_after_user_confirmation(_commit_scope(diff_hash="d"))
    with pytest.raises(GrantScopeMismatch):
        svc.consume(grant.grant_id, ApprovalScope(action="commit_plan", project_id="p1"))


def test_aud12909_matching_bound_scope_still_consumes() -> None:
    svc = ApprovalService()
    grant = svc.issue_after_user_confirmation(_commit_scope(diff_hash="diff-a"))
    consumed = svc.consume(grant.grant_id, _commit_scope(diff_hash="diff-a"))
    assert consumed.grant_id == grant.grant_id


def test_aud12909_unbound_grant_field_still_backward_compatible() -> None:
    """签发侧未绑定的字段不约束（向后兼容旧 grant，既有锁定行为）。"""
    svc = ApprovalService()
    grant = svc.issue_after_user_confirmation(_commit_scope())  # diff_hash=None
    consumed = svc.consume(grant.grant_id, _commit_scope(diff_hash="anything"))
    assert consumed.grant_id == grant.grant_id


# --------------------------------------------------------------------------- #
# AUD-13001 — 路由注册表异常留痕
# --------------------------------------------------------------------------- #


class _BrokenProfileRegistry:
    def active_profile_for_capability(self, capability: str) -> Any:
        raise RuntimeError(f"registry corrupted for {capability}")


def test_aud13001_registry_exception_fails_closed_with_warning(caplog) -> None:
    from src.features.edit.ai_editing.assistant_capability_router import (
        AssistantCapabilityRouter,
    )

    router = AssistantCapabilityRouter(_BrokenProfileRegistry())  # type: ignore[arg-type]
    with caplog.at_level(
        logging.WARNING,
        logger="src.features.edit.ai_editing.assistant_capability_router",
    ):
        resolution = router.resolve("chat")
    # fail-closed：路由不崩溃，语言能力按未配置处理
    assert str(resolution.availability) != "available"
    messages = [record.getMessage() for record in caplog.records]
    assert any("ProfileRegistry" in message for message in messages)
    # 留有堆栈（真实故障可诊断，而不是伪装成"未配置"）
    assert any(record.exc_info for record in caplog.records)


# --------------------------------------------------------------------------- #
# AUD-13009 / 13010 / 13011 — AssistantService
# --------------------------------------------------------------------------- #


class _FakeSession:
    """最小 EditSession 替身：快照可编程失败。"""

    def __init__(self, *, snapshot_error: Exception | None = None) -> None:
        self._snapshot_error = snapshot_error

    def get_snapshot(self) -> Any:
        if self._snapshot_error is not None:
            raise self._snapshot_error
        return SimpleNamespace(
            active_timeline={"timeline_id": "t1", "revision": 9},
        )


class _BrokenRuntime:
    def cancel(self, run_id: str) -> None:
        raise RuntimeError(f"cancel channel broken for {run_id}")


def _service(session: Any = None) -> Any:
    from src.features.edit.ai_editing.assistant_service import AssistantService

    return AssistantService(session if session is not None else _FakeSession())


def test_aud13009_config_accessible_before_start_session() -> None:
    svc = _service()
    # 旧行为：AttributeError；修复后返回默认空配置
    assert svc.config.project_id == ""
    assert svc.config.timeline_id == ""
    from src.features.edit.ai_editing.assistant_service import AssistantConfig

    svc.start_session(
        AssistantConfig(asset_id="a1", timeline_id="t1", project_id="p1"),
        active_profile_id="prof", profile_connected=True,
    )
    assert svc.config.project_id == "p1"
    assert svc.config.timeline_id == "t1"


def test_aud13010_cancel_propagation_failure_is_visible(caplog) -> None:
    svc = _service()
    snapshots: list[Any] = []
    svc.on_state_changed = snapshots.append
    svc.set_active_run_id("run-1", runtime=_BrokenRuntime())

    with caplog.at_level(
        logging.WARNING,
        logger="src.features.edit.ai_editing.assistant_service",
    ):
        svc.cancel()

    assert svc.state.value == "cancelled"
    # snapshot 明示"取消可能未传达"（不再被 except: pass 吞掉）
    assert snapshots and snapshots[-1].error_message is not None
    assert "取消请求未能传达" in snapshots[-1].error_message
    assert any(record.exc_info for record in caplog.records)


def test_aud13010_cancel_success_keeps_clean_error() -> None:
    svc = _service()
    snapshots: list[Any] = []
    svc.on_state_changed = snapshots.append

    cancelled: list[str] = []

    class _OkRuntime:
        def cancel(self, run_id: str) -> None:
            cancelled.append(run_id)

    svc.set_active_run_id("run-2", runtime=_OkRuntime())
    svc.cancel()
    assert cancelled == ["run-2"]
    assert svc.state.value == "cancelled"
    assert snapshots[-1].error_message is None


def test_aud13011_snapshot_failure_surfaces_degraded_baseline(caplog) -> None:
    from src.features.edit.ai_editing.assistant_service import AssistantConfig
    from src.features.edit.ai_editing.brief_service import BriefService
    from src.features.edit.ai_editing.semantic_index import SemanticIndex

    svc = _service(_FakeSession(snapshot_error=RuntimeError("snapshot boom")))
    svc._brief = BriefService()
    svc._brief.create_default("default")
    snapshots: list[Any] = []
    svc.on_state_changed = snapshots.append
    svc.start_session(
        AssistantConfig(asset_id="a1", timeline_id="t1", project_id="p1"),
        active_profile_id="prof", profile_connected=True,
    )
    svc.submit_request("30 秒竖版短视频", brief_id="default")

    with caplog.at_level(
        logging.WARNING,
        logger="src.features.edit.ai_editing.assistant_service",
    ):
        variants = svc.plan_variants(SemanticIndex(), svc.config)

    # 修复点 1：异常留痕（旧行为 except: pass 无声）
    assert any(
        record.getMessage().startswith("timeline revision snapshot failed")
        for record in caplog.records
    )
    # 修复点 2：降级显式告知，而不是静默按 revision 0 编译
    texts = [message.text for message in svc.recent_chat_messages(20)]
    assert any("无法确认当前时间线 revision" in text for text in texts)
    # planner 行为不受影响（可能产出 0..n 个 variant，不崩溃、状态非 FAILED）
    assert svc.state.value in ("variants_ready", "idle")
    assert isinstance(variants, list)


# --------------------------------------------------------------------------- #
# AUD-13017 / 13018 / 13019 / 13020 — AssistantSessionStore
# --------------------------------------------------------------------------- #


def test_aud13017_persist_on_readonly_dir_returns_false(tmp_path) -> None:
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("root 不受目录权限约束，无法构造只读场景")
    from src.features.edit.ai_editing.assistant_session_store import (
        AssistantSessionStore,
    )

    readonly_root = tmp_path / "readonly"
    readonly_root.mkdir()
    store = AssistantSessionStore(cache_path=tmp_path / "history.json")
    if sys.platform == 'win32':
        # W07：chmod 写位在 Windows 不约束属主（目录仍可建子项）。
        # 契约本身是「写入失败 → persist 返回 False」——注入式模拟
        # 同一失败模式（mkdir PermissionError），与 POSIX chmod 场景
        # 走同一 except 分支。
        import unittest.mock as _mock
        from pathlib import Path as _Path

        real_mkdir = _Path.mkdir

        def _denying_mkdir(self, *a, **k):
            if str(readonly_root) in str(self):
                raise PermissionError(5, 'Access is denied (injected)')
            return real_mkdir(self, *a, **k)

        with _mock.patch.object(_Path, 'mkdir', _denying_mkdir):
            assert store.persist(readonly_root / "project") is False
    else:
        readonly_root.chmod(0o500)
        try:
            # 目标 ai/ 目录不存在且父目录只读 → mkdir 抛 PermissionError → 契约 False
            assert store.persist(readonly_root / "project") is False
        finally:
            readonly_root.chmod(0o755)
    # 对照：可写目录正常返回 True
    assert store.persist(tmp_path / "ok-project") is True


def test_aud13018_load_tolerates_malformed_v2_catalog(tmp_path) -> None:
    from src.features.edit.ai_editing.assistant_session_store import (
        AssistantSessionStore,
    )

    ai_dir = tmp_path / "ai"
    ai_dir.mkdir(parents=True)
    # messages 含非 dict 元素 → SessionCatalogEntry.from_dict 抛 AttributeError
    (ai_dir / "session_catalog.json").write_text(json.dumps({
        "version": 2, "active_session_id": "s1",
        "sessions": {"s1": {"session_id": "s1", "messages": ["not-a-dict"]}},
    }), encoding="utf-8")
    store = AssistantSessionStore(cache_path=tmp_path / "history.json")
    # 旧行为：AttributeError 逃逸 → 工程打不开；修复后按损坏处理，不抛
    assert store.load(tmp_path) is False
    assert store.catalog.sessions == {}


def test_aud13018_load_tolerates_scalar_v1_snapshot(tmp_path) -> None:
    from src.features.edit.ai_editing.assistant_session_store import (
        AssistantSessionStore,
    )

    ai_dir = tmp_path / "ai"
    ai_dir.mkdir(parents=True)
    # briefs 为标量 → SessionSnapshot.from_dict 的 dict(42) 抛 TypeError
    (ai_dir / "session.json").write_text(json.dumps({
        "briefs": 42, "variants": {}, "decisions": {}, "overrides": {},
        "last_active_brief_id": "", "updated_at": 1.0,
    }), encoding="utf-8")
    store = AssistantSessionStore(cache_path=tmp_path / "history.json")
    assert store.load(tmp_path) is False  # 不抛，快照按损坏处理


def test_aud13018_restore_catalog_tolerates_malformed_payload(tmp_path) -> None:
    from src.features.edit.ai_editing.assistant_session_store import (
        AssistantSessionStore,
    )

    store = AssistantSessionStore(cache_path=tmp_path / "history.json")
    # 旧行为：restore_catalog 无保护 → AttributeError 逃逸；修复后回到空目录
    store.restore_catalog({
        "version": 2, "sessions": {"s1": {"session_id": "s1", "messages": [1, 2]}},
    })
    assert store.catalog.sessions == {}


def test_aud13019_cache_only_session_rename_persists(tmp_path) -> None:
    from src.features.edit.ai_editing.assistant_session_store import (
        AssistantSessionStore,
    )
    from src.features.edit.ai_editing.session_models import MessageSummary

    cache = tmp_path / "history.json"
    writer = AssistantSessionStore(cache_path=cache)
    entry = writer.create_session("原始标题")
    writer.add_message_summary(MessageSummary(role="user", text="hi"))

    # 另一实例（跨工程恢复场景）：会话只存在于本机缓存
    reader = AssistantSessionStore(cache_path=cache)
    assert reader.get_session(entry.session_id) is not None
    assert reader.set_session_title(entry.session_id, "改名后的标题") is True

    # 重启（第三实例）：改名必须已落盘（旧行为：返回 True 但标题回退）
    reopened = AssistantSessionStore(cache_path=cache)
    restored = reopened.get_session(entry.session_id)
    assert restored is not None
    assert restored.title == "改名后的标题"


def test_aud13020_concurrent_instances_do_not_lose_sessions(tmp_path) -> None:
    from src.features.edit.ai_editing.assistant_session_store import (
        AssistantSessionStore,
    )
    from src.features.edit.ai_editing.session_models import MessageSummary

    cache = tmp_path / "history.json"
    store_a = AssistantSessionStore(cache_path=cache)
    entry_a = store_a.create_session("工程 A 会话")
    store_a.add_message_summary(MessageSummary(role="user", text="a"))

    # store_b 在 store_a 写盘后构造（独立内存快照），随后写自己的会话
    store_b = AssistantSessionStore(cache_path=cache)
    entry_b = store_b.create_session("工程 B 会话")
    store_b.add_message_summary(MessageSummary(role="user", text="b"))

    # 旧行为：整写 last-writer-wins → A 的会话丢失；修复后写前合并
    reopened = AssistantSessionStore(cache_path=cache)
    assert reopened.get_session(entry_a.session_id) is not None
    assert reopened.get_session(entry_b.session_id) is not None


def test_aud13020_clear_history_does_not_resurrect_disk_sessions(tmp_path) -> None:
    from src.features.edit.ai_editing.assistant_session_store import (
        AssistantSessionStore,
    )
    from src.features.edit.ai_editing.session_models import MessageSummary

    cache = tmp_path / "history.json"
    store = AssistantSessionStore(cache_path=cache)
    store.create_session("会话")
    store.add_message_summary(MessageSummary(role="user", text="x"))

    other = AssistantSessionStore(cache_path=cache)
    other.create_session("另一会话")
    other.add_message_summary(MessageSummary(role="user", text="y"))

    store.clear_history()
    data = json.loads(cache.read_text(encoding="utf-8"))
    # 清空语义必须胜出：merge 不把磁盘会话合并回来
    assert data == {"version": 1, "sessions": {}}


def test_aud13020_cache_write_uses_unique_temp_files(tmp_path) -> None:
    """两次连续写缓存不得复用同一固定 .tmp 名（并发写者交叉损坏）。"""
    from src.features.edit.ai_editing.assistant_session_store import (
        AssistantSessionStore,
    )
    from src.features.edit.ai_editing.session_models import MessageSummary

    cache = tmp_path / "history.json"
    store = AssistantSessionStore(cache_path=cache)
    for index in range(3):
        store.create_session(f"s{index}")
        store.add_message_summary(MessageSummary(role="user", text=f"m{index}"))
    # 写入期间不残留任何 .tmp 文件
    leftovers = [
        path.name for path in tmp_path.iterdir()
        if path.name.endswith(".tmp") or ".tmp" in path.name
    ]
    assert leftovers == []
    payload = json.loads(cache.read_text(encoding="utf-8"))
    assert len(payload["sessions"]) == 3


# --------------------------------------------------------------------------- #
# AUD-13102 / 13103 — AutonomousAgent
# --------------------------------------------------------------------------- #


class _AgentHost:
    """AutonomousAgentHost 最小实现（回滚行为可编程）。"""

    def __init__(
        self, *,
        revision: int = 4, commit_revision: int = 5,
        rollback_error: Exception | None = None,
        post_verify_passed: bool = True,
    ) -> None:
        self.revision = revision
        self.commit_revision = commit_revision
        self.rollback_error = rollback_error
        self.post_verify_passed = post_verify_passed
        self.approvals = ApprovalService()
        self.rolled_back = False

    def get_project_id(self) -> str:
        return "p1"

    def get_project_sequences(self) -> Any:
        return SimpleNamespace(
            project=SimpleNamespace(project_id="p1"),
            sequences={"t1": SimpleNamespace(revision=self.revision)},
        )

    def check_ai_action(self, action: str, **kwargs: Any) -> Any:
        return SimpleNamespace(allowed=True, reason="")

    def issue_automatic_plan_approval(
        self, plan: Any, preview: Any, *, asset_ids: tuple[str, ...] = (),
    ) -> Any:
        from src.features.edit.ai_editing.transaction_service import (
            _expected_commit_scope,
        )

        return self.approvals.issue_after_user_confirmation(
            _expected_commit_scope(
                plan, diff_hash=getattr(preview, "diff_hash", None),
            ),
            issued_by="test-automatic",
        )

    def approval_service(self) -> ApprovalService:
        return self.approvals

    def get_selected_asset_ids(self) -> tuple[str, ...]:
        return ("a1",)

    def commit_ai_command(
        self, timeline_id: str, macro: Any, *, undo_label: str,
    ) -> Any:
        return SimpleNamespace(revision=self.commit_revision, undo_label=undo_label)

    def rollback_ai_command(
        self, timeline_id: str, *, expected_undo_label: str,
        expected_revision: int | None,
    ) -> Any:
        self.rolled_back = True
        if self.rollback_error is not None:
            raise self.rollback_error
        return SimpleNamespace(revision=self.revision)

    def verify_autonomous_commit(
        self, plan: Any, state: Any, new_timeline: Any, preview: Any,
    ) -> Any:
        return SimpleNamespace(passed=self.post_verify_passed, reason="quality")

    def refresh_after_ai_commit(self, result: Any) -> None:
        return None


def _agent_plan() -> Any:
    from src.features.edit.ai_editing.models import (
        EditOperation, EditPlan, OperationKind, PlanTarget,
    )

    return EditPlan.create(
        plan_id="plan-aud13102",
        target=PlanTarget(project_id="p1", timeline_id="t1"),
        base_revision=4, title="测试",
        operations=(EditOperation(
            op_id="op-1", kind=OperationKind.ADD_CLIP, params={"asset_id": "a1"},
        ),),
    )


def _make_agent(host: _AgentHost, *, preview_compiled: Any = None,
                commit_compiled: Any = None) -> Any:
    from src.features.edit.ai_editing.autonomous_agent import (
        AutonomousAgent, AutonomousAgentConfig,
    )

    preview = preview_compiled or SimpleNamespace(
        macro=object(), undo_label="AI 自动剪辑",
        operation_summaries={"op-1": {"kind": "add_clip"}},
    )
    commit = commit_compiled or SimpleNamespace(
        macro=object(), undo_label="AI 自动剪辑",
        operation_summaries={"op-1": {"kind": "add_clip"}},
    )
    return AutonomousAgent(
        host,
        preview_plan=lambda _plan, _state: SimpleNamespace(
            has_errors=False, compiled=preview, diff_hash="diff-1",
        ),
        compile_plan=lambda _plan: commit,
        config=AutonomousAgentConfig(enabled=True),
    )


def test_aud13102_verify_failed_with_raising_rollback_returns_structured_result() -> None:
    host = _AgentHost(
        commit_revision=4,  # revision 未前进 → AUTO_VERIFY_FAILED 分支
        rollback_error=RuntimeError("undo stack corrupted"),
    )
    result = _make_agent(host).apply(_agent_plan())
    # apply 不抛；rollback 异常转为 rollback_failures（旧行为：异常逃出 apply）
    assert result.applied is False
    assert result.code == "AUTO_VERIFY_FAILED"
    assert result.rolled_back is False
    assert any("undo stack corrupted" in failure for failure in result.rollback_failures)


def test_aud13102_post_verify_failed_with_raising_rollback_returns_structured() -> None:
    host = _AgentHost(
        post_verify_passed=False,
        rollback_error=ValueError("label mismatch"),
    )
    result = _make_agent(host).apply(_agent_plan())
    assert result.applied is False
    assert result.code == "AUTO_POST_VERIFY_FAILED"
    assert result.rolled_back is False
    assert any("label mismatch" in failure for failure in result.rollback_failures)


def test_aud13103_operation_summary_divergence_blocked() -> None:
    summaries_a = {"op-1": {"kind": "add_clip", "params": {"asset_id": "a1"}}}
    summaries_b = {"op-1": {"kind": "add_clip", "params": {"asset_id": "OTHER"}}}
    host = _AgentHost()
    result = _make_agent(
        host,
        preview_compiled=SimpleNamespace(
            macro=object(), undo_label="l", plan_id="p1", operation_count=1,
            operation_summaries=summaries_a,
        ),
        commit_compiled=SimpleNamespace(
            macro=object(), undo_label="l", plan_id="p1", operation_count=1,
            operation_summaries=summaries_b,
        ),
    ).apply(_agent_plan())
    # 同 plan_id/operation_count 但编译摘要不同 → 拦截（旧行为：放行）
    assert result.applied is False
    assert result.code == "AUTO_PREVIEW_COMPILE_MISMATCH"


def test_aud13103_matching_summaries_still_apply() -> None:
    summaries = {"op-1": {"kind": "add_clip", "params": {"asset_id": "a1"}}}
    host = _AgentHost()
    result = _make_agent(
        host,
        preview_compiled=SimpleNamespace(
            macro=object(), undo_label="l", plan_id="p1", operation_count=1,
            operation_summaries=summaries,
        ),
        commit_compiled=SimpleNamespace(
            macro=object(), undo_label="l", plan_id="p1", operation_count=1,
            operation_summaries=dict(summaries),
        ),
    ).apply(_agent_plan())
    assert result.applied is True
    assert result.code == "AUTO_APPLIED"


def test_aud13103_missing_summaries_on_one_side_backward_compatible() -> None:
    """旧编译结果缺 operation_summaries 元数据时不强行比较（双侧可用才比）。"""
    host = _AgentHost()
    result = _make_agent(
        host,
        preview_compiled=SimpleNamespace(macro=object(), undo_label="l", plan_id="p1"),
        commit_compiled=SimpleNamespace(
            macro=object(), undo_label="l", plan_id="p1", operation_count=1,
        ),
    ).apply(_agent_plan())
    assert result.applied is True


# --------------------------------------------------------------------------- #
# AUD-13109 — propose_patch_from_text 大小写不敏感
# --------------------------------------------------------------------------- #


def test_aud13109_english_keywords_case_insensitive() -> None:
    from src.features.edit.ai_editing.brief_service import BriefService

    svc = BriefService()
    svc.create_default("b1")

    patch = svc.propose_patch_from_text("b1", "Make a Vertical TikTok, fast and warm")
    assert patch.changes.get("platform") == Platform.VERTICAL_SOCIAL
    tones = patch.changes.get("tone", ())
    assert "fast_paced" in tones
    assert "warm" in tones

    patch2 = svc.propose_patch_from_text("b1", "Horizontal YouTube edit, keep it Confident")
    assert patch2.changes.get("platform") == Platform.HORIZONTAL
    assert "confident" in patch2.changes.get("tone", ())


def test_aud13109_chinese_keywords_unaffected() -> None:
    from src.features.edit.ai_editing.brief_service import BriefService

    svc = BriefService()
    svc.create_default("b1")
    patch = svc.propose_patch_from_text("b1", "做一个 30 秒竖版抖音宣传片，快节奏温暖")
    assert patch.changes.get("platform") == Platform.VERTICAL_SOCIAL
    assert "fast_paced" in patch.changes.get("tone", ())
    assert "warm" in patch.changes.get("tone", ())
    duration = patch.changes.get("target_duration")
    assert duration is not None and duration.ideal == 30 * 24
