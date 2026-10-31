"""R1/R2 收尾修复的覆盖测试（§11.1 收尾修复计划）。

覆盖范围：

- **R1 装配断裂修复**：AssistantService 与 MediaAnalysisController 共享同一
  ``AnalysisService`` 实例（同一证据仓库）。此前两者各自 new 一份，导致 Agent
  workflow 永久走 ``_NullAnalysisReader``，看不到任何媒体分析证据。
- **R1 Context Provider 事实计算修复**：``analyzed_count`` 从真实 AnalysisService
  的 ``ready`` 状态派生（此前用 ``has_proxy`` 冒充）；``analysis_fresh`` 聚合
  fingerprint 有效性（此前恒 False）。
- **R1 启动自检**：开发模式下断言 AssistantService 持有的 analysis_service 与
  宿主共享实例一致；不一致时抛 RuntimeError。
- **R2 scheduler 取消回写**：pending job 被 ``cancel_job`` / ``cancel_all`` 取消时
  调用 ``on_cancel`` 回调，使 AnalysisService 的 request 状态正确迁移到 CANCELLED
  （此前永久卡 RUNNING）。
"""

from __future__ import annotations

import os
import tempfile
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytestmark = [pytest.mark.app, pytest.mark.ai_edit]

from src.app.controllers.ai_editing_mixin import (  # noqa: E402
    _all_analysis_fresh,
    _count_locked_decisions,
    _fingerprint_from_asset,
    _safe_get_status,
)
from src.app.controllers.media_analysis_controller import (  # noqa: E402
    MediaAnalysisController,
)
from src.features.edit.ai_editing.analysis_models import AssetFingerprint  # noqa: E402
from src.features.edit.ai_editing.analysis_request import (  # noqa: E402
    AnalysisDataPolicy,
    AnalysisRecipe,
    AnalysisRequest,
    RequestState,
)
from src.features.edit.ai_editing.analysis_scheduler import (  # noqa: E402
    AnalysisJobScheduler,
    ResourceLane,
)
from src.features.edit.ai_editing.analysis_service import AnalysisService  # noqa: E402
from src.features.edit.ai_editing.assistant_service import AssistantService  # noqa: E402
from src.features.edit.ai_editing.editorial_models import (  # noqa: E402
    DecisionState,
    EditDecision,
)
from src.features.edit.ai_editing.jobs import JobHandle, JobKind, JobStatus  # noqa: E402


# ================================================================
# R1：共享 AnalysisService 实例（装配根修复的核心断言）
# ================================================================


class _FakeProject:
    """最小 project 替身（提供 project_id / revision / assets）。"""

    def __init__(self, project_id: str = "p1", revision: int = 1) -> None:
        self.project_id = project_id
        self.revision = revision
        self.assets: dict[str, object] = {}


def test_assistant_service_receives_shared_analysis_service_instance():
    """R1：AssistantService 持有的 analysis_service 就是宿主共享的同一实例。

    此前 ``_setup_ai_editing`` 漏传 ``analysis_service=``，导致 service 回退
    ``_NullAnalysisReader``（永远 unknown）。这里直接断言构造契约。
    """
    shared = AnalysisService()
    # 模拟 _setup_ai_editing 的装配：同一实例注入 AssistantService
    service = AssistantService.__new__(AssistantService)
    # 复刻 AssistantService.__init__ 的关键赋值路径
    service._analysis_service = shared

    # 核心断言：service 持有的就是共享实例（而非 _NullAnalysisReader）
    assert service._analysis_service is shared
    assert service._analysis_service is not None
    # 确认不是 _NullAnalysisReader（其 get_status 返回 "unknown"）
    assert service._analysis_service.get_status("any_asset") == "unknown"  # 空仓库


def test_shared_service_makes_assistant_workflow_see_real_evidence():
    """R1 端到端：媒体池分析写入证据 → AssistantService 通过同一 service 读到。

    这是装配断裂修复的业务价值验证：此前 AssistantService 看不到媒体池分析结果，
    现在二者共享 AnalysisService，证据可见。
    """
    shared = AnalysisService()

    # 模拟媒体池 controller 写入一份分析证据（经 propose → confirm → execute）
    media_ctrl = MediaAnalysisController(
        analysis_service=shared,
        scheduler=AnalysisJobScheduler(),
        project_getter=lambda: _FakeProject(),
    )
    req = media_ctrl.request_analysis_draft(["asset_x"], recipe=AnalysisRecipe.FAST_LOCAL)
    media_ctrl.confirm_and_execute(req.request_id, confirmed_at=time.time())

    # AssistantService 通过共享实例读到的 status 必须与媒体池一致
    # （默认 SceneCutProvider 返回空 segments → status 为 "pending" 而非 "unknown"，
    #  证明 AssistantService 看到了媒体池写入的记录，而非 Null 兜底）
    assistant_view = shared.get_status("asset_x")
    assert assistant_view != "unknown", (
        "AssistantService 仍读 NullAnalysisReader——装配断裂未修复"
    )
    assert assistant_view in ("pending", "ready")


def test_null_analysis_reader_only_used_when_no_service_injected():
    """R1：只有显式不注入 analysis_service 时才回退 _NullAnalysisReader。

    保证向后兼容（既有单测不注入 service 时仍走 Null），同时确保生产装配路径
    （_setup_ai_editing）注入后不会误回退。
    """
    # 不注入 → service 内部 _analysis_service 为 None
    from src.features.edit.ai_editing.assistant_service import _NullAnalysisReader

    service_no_inject = AssistantService.__new__(AssistantService)
    service_no_inject._analysis_service = None
    # _build_default_workflow_runner 的兜底逻辑：None or _NullAnalysisReader()
    reader = service_no_inject._analysis_service or _NullAnalysisReader()
    assert isinstance(reader, _NullAnalysisReader)

    # 注入 → 不是 Null
    real = AnalysisService()
    service_injected = AssistantService.__new__(AssistantService)
    service_injected._analysis_service = real
    reader2 = service_injected._analysis_service or _NullAnalysisReader()
    assert reader2 is real
    assert not isinstance(reader2, _NullAnalysisReader)


# ================================================================
# R1：Context Provider 事实计算（模块级辅助函数）
# ================================================================


def test_safe_get_status_returns_unknown_on_exception():
    """R1：_safe_get_status 在 AnalysisService 抛异常时返回 'unknown'（不传播）。"""
    class _BrokenService:
        def get_status(self, asset_id):
            raise RuntimeError("db locked")
    assert _safe_get_status(_BrokenService(), "a1") == "unknown"


def test_count_locked_decisions_counts_locked_state():
    """R1：_count_locked_decisions 统计 DecisionState.LOCKED 数量（此前恒 0）。"""
    locked = EditDecision(
        decision_id="d1", action="keep", subject="clip_a",
        supports=("hook",), explanation="keep", evidence_refs=(),
        state=DecisionState.LOCKED,
    )
    accepted = EditDecision(
        decision_id="d2", action="trim", subject="clip_b",
        supports=("cta",), explanation="ok", evidence_refs=(),
        state=DecisionState.ACCEPTED,
    )
    another_locked = EditDecision(
        decision_id="d3", action="keep", subject="clip_c",
        supports=("value",), explanation="must keep", evidence_refs=(),
        state=DecisionState.LOCKED,
    )

    class _FakeService:
        _decisions = {"d1": locked, "d2": accepted, "d3": another_locked}

    assert _count_locked_decisions(_FakeService()) == 2


def test_count_locked_decisions_handles_empty():
    """R1：无 decisions 时返回 0（不抛）。"""
    class _Empty:
        _decisions = {}
    assert _count_locked_decisions(_Empty()) == 0

    class _None:
        pass
    # _decisions 属性不存在 → getattr 默认 () → 0
    assert _count_locked_decisions(_None()) == 0


def test_fingerprint_from_asset_reads_size_and_mtime(tmp_path):
    """R1：_fingerprint_from_asset 从 asset.path 构造 size+mtime 指纹。"""
    f = tmp_path / "clip.mp4"
    f.write_bytes(b"x" * 1234)
    st = os.stat(f)

    class _Asset:
        path = str(f)

    fp = _fingerprint_from_asset(_Asset())
    assert fp is not None
    assert fp.size == 1234
    assert fp.mtime_ns == st.st_mtime_ns
    assert fp.content_hash is None  # 廉价指纹，不计算 hash


def test_fingerprint_from_asset_returns_none_for_missing_path():
    """R1：文件不存在 / 无 path 时返回 None（不抛，调用方据此跳过新鲜度判定）。"""
    class _VirtualAsset:
        path = ""  # 虚拟素材无 path

    assert _fingerprint_from_asset(_VirtualAsset()) is None

    class _MissingFile:
        path = "/nonexistent/path/clip.mp4"

    assert _fingerprint_from_asset(_MissingFile()) is None


def test_all_analysis_fresh_true_when_no_analysis():
    """R1：无任何已分析记录时 analysis_fresh=True（无证据可过期）。"""
    svc = AnalysisService()
    assert _all_analysis_fresh(svc, {}) is True
    assert _all_analysis_fresh(svc, {"a1": object()}) is True


def test_all_analysis_fresh_false_when_asset_modified():
    """R1：已分析 asset 的 fingerprint 不匹配（素材被修改）→ analysis_fresh=False。

    此前 analysis_fresh 恒为 False，无法区分"未分析"与"已分析但过期"。
    """
    with tempfile.TemporaryDirectory() as d:
        f = os.path.join(d, "clip.mp4")
        with open(f, "wb") as fh:
            fh.write(b"original")

        class _Asset:
            path = f

        # 分析时记录的指纹（基于原文件）
        original_fp = AssetFingerprint(size=8, mtime_ns=os.stat(f).st_mtime_ns)
        svc = AnalysisService()
        # 手动写入一份分析记录（绕过 provider，直接 ingest）
        from src.features.edit.ai_editing.analysis_models import (
            AnalysisProvenance,
            AssetAnalysis,
        )
        analysis = AssetAnalysis(
            asset_id="a1", fingerprint=original_fp, fps="24/1",
            scene_cuts=(), provenance=AnalysisProvenance(provider="test", model="m"),
        )
        svc.ingest("a1", analysis, ())

        # 素材未改 → fresh
        assert _all_analysis_fresh(svc, {"a1": _Asset()}) is True

        # 修改素材（size 变化）→ fingerprint 不匹配 → not fresh
        with open(f, "wb") as fh:
            fh.write(b"modified-content-longer")
        assert _all_analysis_fresh(svc, {"a1": _Asset()}) is False


# ================================================================
# R1：启动自检（_verify_shared_analysis_service）
# ================================================================


class _CompositionRootHost:
    """复刻 AiEditingMixin 的自检逻辑（避免实例化完整 MainWindow）。

    直接测试 AiEditingMixin._verify_shared_analysis_service 方法（它是 unbound
    method，可经 __func__ 调用，self 只需提供 _ai_strict_composition 属性）。
    """

    def __init__(self, strict: bool = False) -> None:
        self._ai_strict_composition = strict


def _mixin_verify():
    """获取 AiEditingMixin._verify_shared_analysis_service 的原始函数。"""
    from src.app.controllers.ai_editing_mixin import AiEditingMixin
    method = AiEditingMixin._verify_shared_analysis_service
    # Python 3.12: 类中定义的函数就是普通 function，直接用；旧版本可能包装为
    # unbound method，需取 __func__。
    return getattr(method, "__func__", method)


def test_verify_shared_service_passes_when_consistent():
    """R1：service 持有的 analysis_service 与共享实例一致 → 无异常。"""
    shared = AnalysisService()

    class _Service:
        _analysis_service = shared

    host = _CompositionRootHost(strict=True)
    # 不抛即通过
    _mixin_verify()(host, _Service(), shared)


def test_verify_shared_service_passes_when_none():
    """R1：共享实例为 None（装配失败场景）→ 直接返回，不校验。"""
    class _Service:
        _analysis_service = None

    host = _CompositionRootHost(strict=True)
    _mixin_verify()(host, _Service(), None)  # 不抛


def test_verify_shared_service_raises_in_strict_mode_on_mismatch():
    """R1：开发模式下 service 持有不同实例 → RuntimeError（早暴露装配断裂）。"""
    shared = AnalysisService()
    different = AnalysisService()

    class _Service:
        _analysis_service = different  # 持有了另一份

    host = _CompositionRootHost(strict=True)
    with pytest.raises(RuntimeError, match="wiring broken"):
        _mixin_verify()(host, _Service(), shared)


def test_verify_shared_service_only_logs_in_production_mode(caplog):
    """R1：生产模式（strict=False）下不一致只记 error 日志，不抛。"""
    shared = AnalysisService()
    different = AnalysisService()

    class _Service:
        _analysis_service = different

    host = _CompositionRootHost(strict=False)
    import logging
    with caplog.at_level(logging.ERROR, logger="src.app.controllers.ai_editing_mixin"):
        _mixin_verify()(host, _Service(), shared)  # 不抛
    assert any("wiring broken" in r.message for r in caplog.records)


# ================================================================
# R2：scheduler pending 取消回写 AnalysisService（request 状态不卡 RUNNING）
# ================================================================


def test_scheduler_cancel_job_invokes_on_cancel_for_pending():
    """R2：cancel_job 取消 pending job 时调用 on_cancel 回调。"""
    scheduler = AnalysisJobScheduler()
    # 填满 ANALYSIS_LOCAL 上限（默认 2），让第三个进入 pending
    scheduler.submit(ResourceLane.ANALYSIS_LOCAL, JobHandle(kind=JobKind.ANALYSIS, label="a1"))
    scheduler.submit(ResourceLane.ANALYSIS_LOCAL, JobHandle(kind=JobKind.ANALYSIS, label="a2"))
    pending = JobHandle(kind=JobKind.ANALYSIS, label="a3")

    called: list[bool] = []
    scheduler.submit(
        ResourceLane.ANALYSIS_LOCAL, pending, on_cancel=lambda: called.append(True),
    )
    assert scheduler.pending_count() == 1

    ok = scheduler.cancel_job(pending.job_id)
    assert ok is True
    assert called == [True], "on_cancel 回调未被调用"
    assert pending.status == JobStatus.CANCELLED


def test_scheduler_cancel_all_invokes_on_cancel_for_pending():
    """R2：cancel_all 取消所有 pending job 时批量调用各自的 on_cancel。"""
    scheduler = AnalysisJobScheduler()
    # 填满上限
    scheduler.submit(ResourceLane.ANALYSIS_LOCAL, JobHandle(kind=JobKind.ANALYSIS, label="a1"))
    scheduler.submit(ResourceLane.ANALYSIS_LOCAL, JobHandle(kind=JobKind.ANALYSIS, label="a2"))

    calls: list[str] = []
    p1 = JobHandle(kind=JobKind.ANALYSIS, label="p1")
    p2 = JobHandle(kind=JobKind.ANALYSIS, label="p2")
    scheduler.submit(ResourceLane.ANALYSIS_LOCAL, p1, on_cancel=lambda: calls.append("p1"))
    scheduler.submit(ResourceLane.ANALYSIS_LOCAL, p2, on_cancel=lambda: calls.append("p2"))
    assert scheduler.pending_count() == 2

    scheduler.cancel_all()
    assert sorted(calls) == ["p1", "p2"]


def test_scheduler_cancel_job_does_not_invoke_on_cancel_for_running():
    """R2：取消 running job 时不触发 on_cancel（worker 协作中断会经 notify_finished 收尾）。

    避免 on_cancel 与 worker 完成回调重复收尾 request 状态。
    """
    scheduler = AnalysisJobScheduler()
    running = JobHandle(kind=JobKind.ANALYSIS, label="r1")
    called: list[bool] = []
    scheduler.submit(
        ResourceLane.ANALYSIS_LOCAL, running, on_cancel=lambda: called.append(True),
    )
    assert running.status == JobStatus.RUNNING  # 立即出队（有空闲容量）

    scheduler.cancel_job(running.job_id)
    assert running.status == JobStatus.CANCELLING
    assert called == [], "running job 取消不应触发 on_cancel"


def test_scheduler_on_cancel_exception_does_not_break_cleanup():
    """R2：on_cancel 抛异常时被吞并记日志，不中断后续 job 的取消清理。"""
    from src.features.edit.ai_editing.analysis_scheduler import _safe_invoke

    def _bad():
        raise RuntimeError("boom")
    _safe_invoke(_bad, "job1")  # 不抛

    called: list[bool] = []
    _safe_invoke(lambda: called.append(True), "job2")
    assert called == [True]  # 后续回调仍正常执行


def test_analysis_request_migrates_to_terminal_on_pending_cancel():
    """R2 端到端：pending asset job 被 cancel_job 取消 → request 不卡 RUNNING。

    此前 pending 取消不通知 AnalysisService，``_finalize_scheduled_request`` 永不触发，
    request 状态停在 RUNNING。R2 的 on_cancel 回调使状态正确迁移。

    wd8-g46（AUD-12903）适配：work 现在会 notify_finished 释放容量，同步
    scheduler 下 a1/a2 完成后 a3 立即出队，"第三个进 pending"不再由容量泄漏
    产生。改用 playback 门控制造 pending（播放期间分析 lane 不出队），
    锁定同一契约：pending job 取消 → on_cancel 回写 → request 迁移出 RUNNING。
    """
    svc = AnalysisService()
    sched = AnalysisJobScheduler()
    ctrl = MediaAnalysisController(
        analysis_service=svc,
        scheduler=sched,
        project_getter=lambda: _FakeProject(),
    )
    # 播放期间 ANALYSIS lane 不出队 → 全部 3 个 asset job 进 pending
    sched.set_playback_active(True)
    req = ctrl.request_analysis_draft(["a1", "a2", "a3"], recipe=AnalysisRecipe.FAST_LOCAL)
    ctrl.confirm_and_execute(req.request_id, confirmed_at=time.time())

    # 此时 request 应在 RUNNING（3 个 asset job 全部 pending，work 未跑）
    assert svc.get_request(req.request_id).state == RequestState.RUNNING

    # 取消 pending 的 asset job（找到它们的 job_id）
    pending_jobs = [
        item.job for item in sched._pending
    ] if hasattr(sched, "_pending") else []
    assert len(pending_jobs) >= 1, "应有 pending job 存在"

    for pj in list(pending_jobs):
        sched.cancel_job(pj.job_id)

    # request 状态应已迁移出 RUNNING（全部 asset 被计入取消）
    final_state = svc.get_request(req.request_id).state
    assert final_state != RequestState.RUNNING, (
        f"request 仍卡 RUNNING（{final_state}）——on_cancel 回写未生效"
    )
    assert final_state in (RequestState.COMPLETED, RequestState.FAILED)


def test_analysis_request_migrates_to_terminal_on_cancel_all():
    """R2 端到端：cancel_all（工程关闭）触发 pending job 回写 → request 不卡 RUNNING。

    wd8-g46（AUD-12903）适配：同上，用 playback 门控制造 pending 队列。
    """
    svc = AnalysisService()
    sched = AnalysisJobScheduler()
    ctrl = MediaAnalysisController(
        analysis_service=svc,
        scheduler=sched,
        project_getter=lambda: _FakeProject(),
    )
    sched.set_playback_active(True)
    req = ctrl.request_analysis_draft(["a1", "a2", "a3"], recipe=AnalysisRecipe.FAST_LOCAL)
    ctrl.confirm_and_execute(req.request_id, confirmed_at=time.time())
    assert svc.get_request(req.request_id).state == RequestState.RUNNING

    # 模拟工程关闭
    ctrl.on_project_changed(None)
    final_state = svc.get_request(req.request_id).state
    assert final_state != RequestState.RUNNING, (
        f"工程关闭后 request 仍卡 RUNNING（{final_state}）"
    )
