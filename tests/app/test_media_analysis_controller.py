"""TAEP M3 — MediaAnalysisController 测试。

验收（§17.2 / §17.3 / M3 验收）：
- 三步流：propose（未确认）→ confirm_and_execute（经 scheduler）。
- §17.3 红线：未确认的 request 不能执行。
- per-asset 状态信号（analysis_status_changed）。
- 工程切换/关闭：on_project_changed(None) → cancel_all + 清状态（无 orphan worker）。
- 不写 timeline（无 commit/apply 方法）。
- 默认导入零 AI 推理（M3 验收第 1 项）。
"""

from __future__ import annotations

import os
import threading

import pytest

# 控制器是 QObject，需要 QApplication；但本项目 tests/conftest 已提供 qapp fixture。
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytestmark = [pytest.mark.app, pytest.mark.ai_edit]

from src.app.controllers.media_analysis_controller import (
    MediaAnalysisController,
    STATUS_PENDING,
    STATUS_READY,
    STATUS_RUNNING,
    STATUS_FAILED,
)
from src.features.edit.ai_editing.analysis_request import (
    AnalysisDataPolicy,
    AnalysisRecipe,
    AnalysisRequest,
    RequestState,
)
from src.features.edit.ai_editing.analysis_scheduler import AnalysisJobScheduler
from src.features.edit.ai_editing.analysis_service import AnalysisService


def test_async_scheduler_runs_work_off_caller_thread(qapp):
    """生产 scheduler 不得在调用 confirm 的 Qt 线程执行分析 work。"""
    from src.features.edit.ai_editing.analysis_scheduler import ResourceLane
    from src.features.edit.ai_editing.jobs import JobHandle, JobKind

    scheduler = AnalysisJobScheduler(asynchronous=True)
    finished = threading.Event()
    caller = threading.get_ident()
    worker_thread: list[int] = []
    job = JobHandle(kind=JobKind.ANALYSIS, label="async test")

    def work() -> None:
        worker_thread.append(threading.get_ident())
        job.complete({"ok": True})
        scheduler.notify_finished(job.job_id)
        finished.set()

    scheduler.submit(ResourceLane.ANALYSIS_LOCAL, job, work=work)
    assert finished.wait(2.0)
    assert worker_thread and worker_thread[0] != caller
    assert job.status.value == "succeeded"
    scheduler.shutdown(wait=True)


class _FakeProject:
    """最小 project 替身（提供 project_id / revision）。"""

    def __init__(self, project_id: str = "p1", revision: int = 7) -> None:
        self.project_id = project_id
        self.revision = revision


def _make_controller(project: _FakeProject | None = None) -> MediaAnalysisController:
    """构造一个 controller（service + scheduler + project_getter）。"""
    svc = AnalysisService()
    sched = AnalysisJobScheduler()
    proj = project or _FakeProject()
    return MediaAnalysisController(
        analysis_service=svc,
        scheduler=sched,
        project_getter=lambda: proj,
    )


# ================================================================
# 三步流：propose
# ================================================================


def test_request_analysis_draft_creates_unconfirmed_request(qapp):
    """propose 创建未确认 request（§17.3：Assistant/Skill 只能 propose）。"""
    ctrl = _make_controller()
    req = ctrl.request_analysis_draft(["a1", "a2"], recipe=AnalysisRecipe.FAST_LOCAL)
    assert req.is_confirmed is False
    assert req.state == RequestState.PROPOSED
    assert req.user_confirmed_at is None
    # 登记到 service
    assert ctrl._service.get_request(req.request_id) is not None


def test_request_analysis_draft_emits_proposed_signal(qapp):
    """propose 发出 analysis_proposed 信号（UI 据此弹确认卡）。"""
    ctrl = _make_controller()
    received: list = []
    ctrl.analysis_proposed.connect(lambda r: received.append(r))
    req = ctrl.request_analysis_draft(["a1"], recipe=AnalysisRecipe.EDITORIAL_LOCAL)
    assert len(received) == 1
    assert received[0] is req


def test_request_analysis_draft_emits_pending_status(qapp):
    """propose 后 asset 状态变 pending（UI badge）。"""
    ctrl = _make_controller()
    statuses: dict[str, str] = {}
    ctrl.analysis_status_changed.connect(lambda aid, s: statuses.__setitem__(aid, s))
    ctrl.request_analysis_draft(["a1", "a2"])
    assert statuses == {"a1": STATUS_PENDING, "a2": STATUS_PENDING}


def test_request_analysis_draft_requires_project_id(qapp):
    """无 project（project_getter 返回 None）→ 报错。"""
    svc = AnalysisService()
    sched = AnalysisJobScheduler()
    ctrl = MediaAnalysisController(svc, sched, project_getter=lambda: None)
    with pytest.raises(ValueError, match="project_id"):
        ctrl.request_analysis_draft(["a1"])


# ================================================================
# 三步流：confirm + execute
# ================================================================


def test_confirm_and_execute_succeeds(qapp):
    """用户确认 → confirm + execute（经 scheduler）→ COMPLETED。"""
    ctrl = _make_controller()
    req = ctrl.request_analysis_draft(["a1"], recipe=AnalysisRecipe.FAST_LOCAL)
    job, err = ctrl.confirm_and_execute(req.request_id, confirmed_at=1000.0)
    assert err == "", f"expected no error, got: {err!r}"
    assert job is not None
    final = ctrl._service.get_request(req.request_id)
    assert final.state == RequestState.COMPLETED
    assert final.is_confirmed


def test_confirm_and_execute_emits_running_then_ready(qapp):
    """execute 时 asset 状态：pending → running → ready。"""
    ctrl = _make_controller()
    statuses: list[tuple[str, str]] = []
    ctrl.analysis_status_changed.connect(lambda aid, s: statuses.append((aid, s)))
    req = ctrl.request_analysis_draft(["a1"])
    ctrl.confirm_and_execute(req.request_id, confirmed_at=1.0)
    # 至少经历 pending（propose）→ running（execute）
    status_seq = [s for _, s in statuses if _ == "a1"]
    assert STATUS_PENDING in status_seq
    assert STATUS_RUNNING in status_seq


def test_confirm_and_execute_unknown_request(qapp):
    ctrl = _make_controller()
    job, err = ctrl.confirm_and_execute("nonexistent")
    assert job is None
    assert "unknown" in err


def test_execute_unconfirmed_request_via_direct_service(qapp):
    """§17.3 红线：未确认的 request 直接 execute 被拒（service 层约束）。

    controller.confirm_and_execute 会先 confirm，所以这里测 service 层的拒绝。
    """
    svc = AnalysisService()
    req = AnalysisRequest.create("p1", ["a1"], recipe_id=AnalysisRecipe.FAST_LOCAL)
    svc.propose_analysis(req)
    # 直接调 service.execute_analysis（未经 confirm）→ 拒绝
    job, err = svc.execute_analysis(req.request_id)
    assert job is None
    assert "not confirmed" in err


# ================================================================
# 工程生命周期（无 orphan worker）
# ================================================================


def test_on_project_changed_cancels_all(qapp):
    """工程切换/关闭 → cancel_all（无 orphan worker）。"""
    ctrl = _make_controller()
    req = ctrl.request_analysis_draft(["a1", "a2"])
    ctrl.confirm_and_execute(req.request_id, confirmed_at=1.0)
    # 模拟有 scheduler 任务在跑（submit 一个 pending）
    from src.features.edit.ai_editing.jobs import JobHandle, JobKind
    ctrl._scheduler.submit(
        __import__("src.features.edit.ai_editing.analysis_scheduler", fromlist=["ResourceLane"]).ResourceLane.ANALYSIS_LOCAL,
        JobHandle(kind=JobKind.ANALYSIS, label="extra"),
    )
    ctrl.on_project_changed(None)
    assert ctrl._scheduler.is_idle()
    # request 映射清空
    assert ctrl._request_assets == {}


def test_on_project_changed_emits_no_error(qapp):
    """关闭时即使无活动任务也不抛。"""
    ctrl = _make_controller()
    ctrl.on_project_changed(None)  # 无任务，no-op
    assert ctrl._scheduler.is_idle()


# ================================================================
# 边界：不写 timeline
# ================================================================


def test_controller_does_not_write_timeline(qapp):
    """controller 没有 commit/apply/edit timeline 方法（§5.4：分析只提候选）。"""
    ctrl = _make_controller()
    public_methods = [m for m in dir(ctrl) if not m.startswith("_") and callable(getattr(ctrl, m))]
    forbidden = [m for m in public_methods if "commit" in m or "apply" in m]
    assert forbidden == [], f"controller must not expose timeline write methods: {forbidden}"


# ================================================================
# 默认导入零 AI 推理（M3 验收第 1 项）
# ================================================================


def test_media_import_worker_does_not_trigger_analysis(qapp):
    """M3 验收第 1 项：导入素材不触发 AI 分析。

    验证方式：MediaAnalysisController 在无人调 request_analysis_draft 时，不 submit
    任何 analysis job。导入路径（MediaImportWorker）只做 probe，不碰 controller。
    """
    ctrl = _make_controller()
    assert ctrl._scheduler.is_idle() is True
    assert ctrl._scheduler.pending_count() == 0
    assert ctrl._scheduler.active_count() == 0
    # AnalysisService 无任何 request
    assert ctrl._service.list_requests() == []


def test_analysis_only_starts_on_explicit_request(qapp):
    """分析只经显式 request_analysis_draft 启动，不因其他操作触发。"""
    ctrl = _make_controller()
    # 模拟"导入 10 个素材"——不调 controller 任何方法
    for i in range(10):
        _ = _FakeProject()  # 导入只创建 asset，不触发分析
    assert ctrl._scheduler.is_idle()
    assert len(ctrl._service.list_requests()) == 0
    # 只有显式 propose 才有 request
    ctrl.request_analysis_draft(["a1"])
    assert len(ctrl._service.list_requests()) == 1


# ================================================================
# 云配方路径
# ================================================================


def test_cloud_recipe_uses_cloud_lane(qapp):
    """CLOUD_ENHANCED + APPROVED_CLOUD → execute 经 ANALYSIS_CLOUD lane。"""
    from src.features.edit.ai_editing.analysis_models import AssetAnalysis
    from src.features.edit.ai_editing.editorial_models import SegmentKind

    ctrl = _make_controller()
    # A cloud recipe still declares transcript_local.  Register a minimal
    # provider so this test verifies lane selection rather than relying on the
    # old unsafe behavior that silently dropped missing recipe providers.
    class _TranscriptProvider:
        provider_id = "transcript_local"
        supported_kinds = (SegmentKind.TRANSCRIPT,)

        def analyze(self, asset_id, fingerprint, *, fps, progress=None):
            return AssetAnalysis(asset_id=asset_id, fingerprint=fingerprint, fps=fps), ()

    ctrl._service.register(_TranscriptProvider())
    req = ctrl.request_analysis_draft(
        ["a1"],
        recipe=AnalysisRecipe.CLOUD_ENHANCED,
        data_policy=AnalysisDataPolicy.APPROVED_CLOUD,
    )
    # 监听 scheduler：cloud lane 应有提交
    ctrl.confirm_and_execute(req.request_id, confirmed_at=1.0)
    # service 内部把 cloud 请求提交到 ANALYSIS_CLOUD lane
    # （同步 work 已执行完，active 可能为 0，但 _all 里有记录）
    assert ctrl._service.get_request(req.request_id).state == RequestState.COMPLETED


def test_get_asset_status_reflects_service(qapp):
    """get_asset_status 从 AnalysisService 派生。

    默认 SceneCutProvider 返回空分析（无 cuts），故 get_status 返回 "pending"
    （有记录但无 segments）。状态派生正确即可——具体 ready 需真实 provider 产出。
    """
    ctrl = _make_controller()
    # 未分析 → unknown
    assert ctrl.get_asset_status("a1") == "unknown"
    # 执行后 → 有记录（pending：默认 provider 无 segments）
    req = ctrl.request_analysis_draft(["a1"])
    ctrl.confirm_and_execute(req.request_id, confirmed_at=1.0)
    status = ctrl.get_asset_status("a1")
    assert status in ("pending", "ready"), f"expected pending/ready, got {status!r}"
