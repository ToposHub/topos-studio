"""P2-2 Media Management 完整工作流——空间预检与后台化验收。

对应《媒体池后端交接计划》P2 第 2 项（复用并核验 + 补齐）：
- 裁切/复制/转码、handles、路径重映射：既有 consolidate/collect 核心
  （remux/transcode 模式、GOP 对齐、分段重映射、handles 默认 12）复用；
- 本轮补齐：收集/整理移出 UI 线程（后台 worker + 进度 + 协作取消）、
  启动前磁盘空间预检（保守估算，不足即拒）；
- 验收：不破坏源文件；取消与磁盘满安全退出（收集取消=部分结果+警告、
  整理取消=分段边界零半成品——既有核心语义，经 worker 透传）。
"""
from __future__ import annotations

import shutil
import threading
from pathlib import Path

import pytest

pytestmark = pytest.mark.edit_core

from src.features.edit.core.media_management_worker import (
    KIND_COLLECT,
    KIND_CONSOLIDATE,
    MediaManagementWorker,
    estimate_collect_bytes,
    estimate_consolidate_bytes,
    preflight_disk_space,
)
from src.features.edit.core.consolidate import ConsolidateCancelled
from src.features.edit.core.project import EditProject
from src.features.edit.core.timeline_v2 import (
    ClipInstance,
    EditTrack,
    Rational,
    TimelineSequence,
)

TEST_VIDEOS = Path(__file__).resolve().parents[2] / "tests" / "test_videos"
H264 = TEST_VIDEOS / "2K_h264.mp4"


def _project_with_clip(tmp_path: Path) -> tuple[EditProject, TimelineSequence]:
    if not H264.exists():
        pytest.skip(f"测试素材缺失: {H264}")
    media = tmp_path / "source.mp4"
    shutil.copy2(H264, media)
    project = EditProject.create("P2-2")
    project = project.import_asset(
        str(media), "video",
        {"fps": 25.0, "fps_num": 25, "fps_den": 1, "total_frames": 1572,
         "duration_seconds": 62.88, "width": 1920, "height": 1080,
         "codec": "h264"})
    asset_id = project.get_asset_by_path(str(media)).asset_id
    clip = ClipInstance(
        "c1", asset_id, "v1", 0, 50, 0, 50,
        _source_fps_num=25, _source_fps_den=1,
        _timeline_fps_num=25, _timeline_fps_den=1)
    sequence = TimelineSequence(
        timeline_id="t1", name="T", fps=Rational(25, 1),
        video_tracks=(EditTrack("v1", "V1", "video", 0, clips=(clip,)),))
    return project, sequence


# ================================================================
# 空间预检
# ================================================================

def test_estimates_are_conservative(tmp_path) -> None:
    project, sequence = _project_with_clip(tmp_path)
    source_size = (tmp_path / "source.mp4").stat().st_size
    collect_estimate = estimate_collect_bytes(project, sequences=[sequence])
    assert collect_estimate >= source_size  # 整文件复制口径
    consolidate_estimate = estimate_consolidate_bytes(project, [sequence])
    assert consolidate_estimate >= source_size  # 上界（实际只写使用区间）


def test_preflight_blocks_when_insufficient(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        "src.features.edit.core.media_management_worker.check_free_space",
        lambda directory, required: (False, 1024))
    ok, free = preflight_disk_space(str(tmp_path), 2**30)
    assert ok is False and free == 1024


# ================================================================
# 后台 worker
# ================================================================

def _wait(qapp, received: list, timeout_ms: int = 15000) -> bool:
    from PyQt6.QtCore import QDeadlineTimer, QEventLoop
    deadline = QDeadlineTimer(timeout_ms)
    while not received and not deadline.hasExpired():
        qapp.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 50)
    return bool(received)


def test_worker_runs_off_ui_thread_and_reports(tmp_path, qapp) -> None:
    """重活不在调用线程执行；进度与完成经信号回主线程。"""
    project, sequence = _project_with_clip(tmp_path)
    target = tmp_path / "archive"
    target.mkdir()

    def _work(progress_cb, cancel_check):
        worker_thread = threading.get_ident()
        from src.features.edit.core.project_archive import collect_project
        archived, report = collect_project(
            project, target_dir=str(target), sequences=[sequence],
            progress_callback=progress_cb, cancelled_fn=cancel_check)
        return (worker_thread, archived), report

    worker = MediaManagementWorker(KIND_COLLECT, _work)
    finished: list = []
    failed: list = []
    progress_events: list = []
    worker.signals.finished.connect(
        lambda k, payload, report: finished.append((k, payload, report)))
    worker.signals.failed.connect(lambda k, m: failed.append((k, m)))
    worker.signals.progress.connect(
        lambda k, done, total: progress_events.append((k, done, total)))
    QThreadPoolRunner = None
    from PyQt6.QtCore import QThreadPool
    QThreadPool.globalInstance().start(worker)

    assert _wait(qapp, finished), f"collect 未完成: {failed}"
    kind, payload, report = finished[0]
    assert kind == KIND_COLLECT and not failed
    worker_thread, archived = payload
    assert worker_thread != threading.get_ident()  # 不在 UI 线程
    assert report.passed
    # 源文件不被收集破坏
    assert (tmp_path / "source.mp4").stat().st_size > 0


def test_worker_cancel_emits_cancelled_not_failed(qapp) -> None:
    """取消：cancelled 信号（不是 failed）；核心异常类型透传语义。"""

    def _work(progress_cb, cancel_check):
        raise ConsolidateCancelled()

    worker = MediaManagementWorker(KIND_CONSOLIDATE, _work)
    cancelled: list = []
    failed: list = []
    worker.signals.cancelled.connect(lambda k: cancelled.append(k))
    worker.signals.failed.connect(lambda k, m: failed.append((k, m)))
    from PyQt6.QtCore import QThreadPool
    QThreadPool.globalInstance().start(worker)
    assert _wait(qapp, cancelled)
    assert cancelled == [KIND_CONSOLIDATE]
    assert not failed


def test_worker_failure_goes_to_failed_signal(qapp) -> None:
    def _work(progress_cb, cancel_check):
        raise RuntimeError("boom")

    worker = MediaManagementWorker(KIND_COLLECT, _work)
    failed: list = []
    cancelled: list = []
    worker.signals.failed.connect(lambda k, m: failed.append((k, m)))
    worker.signals.cancelled.connect(lambda k: cancelled.append(k))
    from PyQt6.QtCore import QThreadPool
    QThreadPool.globalInstance().start(worker)
    assert _wait(qapp, failed)
    assert failed[0][0] == KIND_COLLECT
    assert "boom" in failed[0][1]
    assert not cancelled
