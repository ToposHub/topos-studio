"""P1-2 自动音画同步与多机位——后端核验验收（复用并核验）。

对应《媒体池后端交接计划》P1 第 2 项。既有专项套件（127 用例）：
- tests/edit/test_multicam_sync.py：波形互相关/搜索窗/置信度/组偏移语义
- tests/edit/test_multicam_sync_guards.py：低置信度/反相/歧义/超窗拒绝
  （alignment_rejection）、TC 缺失/畸形 fail-loud、漂移诊断为拒写
  （combine_segment_alignments）、多段中位数
- tests/edit/test_multicam_production.py：组定义 stamp/read 往返、角度
  切换、组成员校验
- edit_controller：request_id + MulticamSyncIdentity 双守卫消费迟到结果
本套件补齐最后空白：worker 取消零写入、无音频参考角度明确诊断。
"""
from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.edit_core

from src.features.edit.core.multicam_sync import (
    MulticamSyncJob,
    MulticamSyncWorker,
)

TEST_VIDEOS = Path(__file__).resolve().parents[2] / "tests" / "test_videos"


def _wait_signal(qapp, received: list, timeout_ms: int = 8000) -> bool:
    """等待信号收集列表非空（Qt 事件循环驱动 worker 的 queued 信号）。"""
    from PyQt6.QtCore import QDeadlineTimer, QEventLoop
    deadline = QDeadlineTimer(timeout_ms)
    while not received and not deadline.hasExpired():
        qapp.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 50)
    return bool(received)


def _clip(name: str) -> Path:
    path = TEST_VIDEOS / name
    if not path.exists():
        pytest.skip(f"测试素材缺失: {path}")
    return path


def test_worker_cancel_emits_cancelled_with_zero_results(qapp, tmp_path) -> None:
    """中断请求在位：sync_failed("cancelled")，sync_finished 零发射。

    用子类钉死 isInterruptionRequested（QThread 的预置中断标志在线程
    启动时序上不可靠），确定性地覆盖 run() 的协作取消分支。
    """
    clip = _clip("2K_h264.mp4")

    class _CancelledWorker(MulticamSyncWorker):
        def isInterruptionRequested(self) -> bool:  # noqa: N802
            return True

    jobs = [
        MulticamSyncJob(asset_id="a", path=str(clip), fps=25.0),
        MulticamSyncJob(asset_id="b", path=str(clip), fps=25.0),
    ]
    worker = _CancelledWorker(jobs, "a")
    failed: list = []
    finished: list = []
    worker.sync_failed.connect(lambda msg: failed.append(msg))
    worker.sync_finished.connect(lambda payload: finished.append(payload))
    worker.start()
    try:
        assert _wait_signal(qapp, failed), "取消必须上报 sync_failed"
        assert failed[0] == "cancelled"
        assert not finished  # 零结果：控制器不写任何偏移
    finally:
        worker.wait(5000)


def test_worker_reference_without_audio_fails_loud(qapp, tmp_path) -> None:
    """参考角度无音频流（或不可解码）：明确失败诊断，不伪称已同步。"""
    fake = tmp_path / "not-a-media.mov"
    fake.write_bytes(b"this is not a media file")
    jobs = [
        MulticamSyncJob(asset_id="ref", path=str(fake), fps=25.0),
        MulticamSyncJob(asset_id="other", path=str(_clip("2K_h264.mp4")),
                        fps=25.0),
    ]
    worker = MulticamSyncWorker(jobs, "ref")
    failed: list = []
    finished: list = []
    worker.sync_failed.connect(lambda msg: failed.append(msg))
    worker.sync_finished.connect(lambda payload: finished.append(payload))
    worker.start()
    try:
        assert _wait_signal(qapp, failed), "不可解码参考必须失败上报"
        message = failed[0]
        assert message != "cancelled"
        assert not finished
    finally:
        worker.wait(10000)
