"""TAEP M3 — AnalysisJobScheduler 测试。

验收（§17.7 / §18.3 / M3 验收第 3 项）：
- 资源池分级：ANALYSIS 受并发上限约束；EXPORT/PLAYBACK 不受限。
- playback_active 暂停 ANALYSIS 出队（播放不被后台分析卡顿）。
- 统一 cancel：cancel_job / cancel_all（工程关闭无 orphan worker）。
- 并发上限：超限入队等待。
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.edit_core

from src.features.edit.ai_editing.analysis_scheduler import (
    AnalysisJobScheduler,
    LaneLimits,
    ResourceLane,
)
from src.features.edit.ai_editing.jobs import JobHandle, JobKind, JobStatus


def _job(label: str = "") -> JobHandle:
    return JobHandle(kind=JobKind.ANALYSIS, label=label)


# ================================================================
# 资源池分级
# ================================================================


def test_lane_is_analysis_flag():
    assert ResourceLane.ANALYSIS_LOCAL.is_analysis is True
    assert ResourceLane.ANALYSIS_CLOUD.is_analysis is True
    assert ResourceLane.EXPORT_RENDER.is_analysis is False
    assert ResourceLane.PLAYBACK.is_analysis is False


def test_lane_limits_default():
    limits = LaneLimits()
    assert limits.limit_for(ResourceLane.ANALYSIS_LOCAL) == 2
    assert limits.limit_for(ResourceLane.ANALYSIS_CLOUD) == 1
    assert limits.limit_for(ResourceLane.EXPORT_RENDER) is None
    assert limits.limit_for(ResourceLane.PLAYBACK) is None


def test_lane_limits_custom():
    limits = LaneLimits(analysis_local_concurrency=4, analysis_cloud_concurrency=2)
    assert limits.limit_for(ResourceLane.ANALYSIS_LOCAL) == 4
    assert limits.limit_for(ResourceLane.ANALYSIS_CLOUD) == 2


# ================================================================
# submit + 并发上限
# ================================================================


def test_submit_within_limit_runs_immediately():
    """并发上限内 → 立即出队（active）。"""
    s = AnalysisJobScheduler()
    j1 = s.submit(ResourceLane.ANALYSIS_LOCAL, _job("a1"))
    j2 = s.submit(ResourceLane.ANALYSIS_LOCAL, _job("a2"))
    assert s.active_count() == 2
    assert s.pending_count() == 0
    assert j1.status == JobStatus.RUNNING


def test_submit_over_limit_queues():
    """超过并发上限（默认 2）→ 第三个入队等待。"""
    s = AnalysisJobScheduler()
    for i in range(4):
        s.submit(ResourceLane.ANALYSIS_LOCAL, _job(f"a{i}"))
    assert s.active_count() == 2  # 上限
    assert s.pending_count() == 2  # 排队


def test_cloud_lane_independent_limit():
    """ANALYSIS_CLOUD 独立计数（默认 1）。"""
    s = AnalysisJobScheduler()
    s.submit(ResourceLane.ANALYSIS_CLOUD, _job("c1"))
    s.submit(ResourceLane.ANALYSIS_CLOUD, _job("c2"))
    assert s.active_count(ResourceLane.ANALYSIS_CLOUD) == 1
    assert s.pending_count(ResourceLane.ANALYSIS_CLOUD) == 1


def test_export_lane_unlimited():
    """EXPORT_RENDER 不受并发上限约束。"""
    s = AnalysisJobScheduler()
    for i in range(5):
        s.submit(ResourceLane.EXPORT_RENDER, _job(f"e{i}"))
    assert s.active_count(ResourceLane.EXPORT_RENDER) == 5
    assert s.pending_count() == 0


# ================================================================
# playback_active 优先级（M3 验收第 3 项）
# ================================================================


def test_playback_active_blocks_analysis_dispatch():
    """播放期间，新 ANALYSIS 任务不出队（已运行的不中断）。"""
    s = AnalysisJobScheduler()
    s.submit(ResourceLane.ANALYSIS_LOCAL, _job("a1"))
    s.submit(ResourceLane.ANALYSIS_LOCAL, _job("a2"))  # 上限满
    s.set_playback_active(True)
    # 释放一个容量后，pending 仍不出队（playback 暂停）
    s.notify_finished("nonexistent")  # 无影响
    j3 = s.submit(ResourceLane.ANALYSIS_LOCAL, _job("a3"))
    assert j3.status == JobStatus.QUEUED
    assert s.pending_count() == 1


def test_playback_inactive_dispatches_pending():
    """播放结束 → 积压的分析任务出队。"""
    s = AnalysisJobScheduler()
    j1 = s.submit(ResourceLane.ANALYSIS_LOCAL, _job("a1"))
    s.submit(ResourceLane.ANALYSIS_LOCAL, _job("a2"))
    s.set_playback_active(True)
    # 这时 capacity 满且 playback 暂停；提交第三个应入队
    j3 = s.submit(ResourceLane.ANALYSIS_LOCAL, _job("a3"))
    assert j3.status == JobStatus.QUEUED
    assert s.pending_count() == 1
    # 完成第一个释放容量，但 playback 仍 active → j3 不出队
    s.notify_finished(j1.job_id)
    assert s.pending_count() == 1, "playback still active, j3 stays pending"
    # 播放结束 → j3 出队
    s.set_playback_active(False)
    assert j3.status == JobStatus.RUNNING
    assert s.pending_count() == 0


def test_playback_does_not_block_export():
    """播放期间 EXPORT lane 不受影响。"""
    s = AnalysisJobScheduler()
    s.set_playback_active(True)
    j = s.submit(ResourceLane.EXPORT_RENDER, _job("exp"))
    assert j.status == JobStatus.RUNNING
    assert s.active_count(ResourceLane.EXPORT_RENDER) == 1


# ================================================================
# cancel（无 orphan worker）
# ================================================================


def test_cancel_pending_job_removes_from_queue():
    """pending job 被 cancel → 从队列移除 + CANCELLED。"""
    s = AnalysisJobScheduler()
    s.submit(ResourceLane.ANALYSIS_LOCAL, _job("a1"))
    s.submit(ResourceLane.ANALYSIS_LOCAL, _job("a2"))  # 上限满（2）
    j3 = s.submit(ResourceLane.ANALYSIS_LOCAL, _job("a3"))  # pending
    assert s.pending_count() == 1
    ok = s.cancel_job(j3.job_id)
    assert ok is True
    assert s.pending_count() == 0
    assert j3.status == JobStatus.CANCELLED


def test_cancel_running_job_marks_cancelling():
    s = AnalysisJobScheduler()
    j1 = s.submit(ResourceLane.ANALYSIS_LOCAL, _job("a1"))
    assert j1.status == JobStatus.RUNNING
    ok = s.cancel_job(j1.job_id)
    assert ok is True
    assert j1.status == JobStatus.CANCELLING


def test_cancel_unknown_job_returns_false():
    s = AnalysisJobScheduler()
    assert s.cancel_job("nonexistent") is False


def test_cancel_all_clears_pending_and_active():
    """cancel_all 取消所有 pending + active（工程关闭用）。"""
    s = AnalysisJobScheduler()
    for i in range(4):
        s.submit(ResourceLane.ANALYSIS_LOCAL, _job(f"a{i}"))
    assert s.active_count() == 2 and s.pending_count() == 2
    n = s.cancel_all()
    assert n == 4  # 2 active + 2 pending
    assert s.is_idle()


def test_cancel_all_makes_scheduler_idle():
    """cancel_all 后 is_idle=True（工程关闭清理判定）。"""
    s = AnalysisJobScheduler()
    s.submit(ResourceLane.ANALYSIS_LOCAL, _job("a1"))
    s.cancel_all()
    assert s.is_idle() is True


def test_is_idle_when_empty():
    s = AnalysisJobScheduler()
    assert s.is_idle() is True
    s.submit(ResourceLane.ANALYSIS_LOCAL, _job("a1"))
    assert s.is_idle() is False


# ================================================================
# work 回调执行
# ================================================================


def test_work_callback_executed_on_dispatch():
    """work 回调在出队时执行。"""
    s = AnalysisJobScheduler()
    results: list[str] = []
    j = s.submit(ResourceLane.ANALYSIS_LOCAL, _job("a1"), work=lambda: results.append("done"))
    assert results == ["done"]
    assert j.status == JobStatus.RUNNING


def test_work_callback_exception_marks_job_failed():
    """work 抛异常 → job 标 FAILED + 释放容量。"""

    def bad_work() -> None:
        raise RuntimeError("boom")

    s = AnalysisJobScheduler()
    j = s.submit(ResourceLane.ANALYSIS_LOCAL, _job("a1"), work=bad_work)
    assert j.status == JobStatus.FAILED
    assert j.error_code == "SCHEDULER_WORK_ERROR"
    # 容量已释放
    assert s.active_count() == 0


def test_notify_finished_releases_capacity_and_dispatches():
    """notify_finished 释放容量 → 出队下一个 pending。"""
    s = AnalysisJobScheduler()
    j1 = s.submit(ResourceLane.ANALYSIS_LOCAL, _job("a1"))
    j2 = s.submit(ResourceLane.ANALYSIS_LOCAL, _job("a2"))
    j3 = s.submit(ResourceLane.ANALYSIS_LOCAL, _job("a3"))  # pending
    assert s.pending_count() == 1 and j3.status == JobStatus.QUEUED
    # j1 完成 → j3 出队
    s.notify_finished(j1.job_id)
    assert j3.status == JobStatus.RUNNING
    assert s.pending_count() == 0


# ================================================================
# 查询
# ================================================================


def test_get_job():
    s = AnalysisJobScheduler()
    j = s.submit(ResourceLane.ANALYSIS_LOCAL, _job("a1"))
    assert s.get_job(j.job_id) is j
    assert s.get_job("nonexistent") is None
