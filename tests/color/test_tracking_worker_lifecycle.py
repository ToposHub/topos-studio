"""阶段 3 验收：跟踪工作线程生命周期（真实素材上运行）。"""

import os
import time

# AUD-R2-005：本机 OpenBLAS 多线程 dgetrf 在 QThread 工作线程中并行崩溃
# （exit 132 / SIGILL）。必须在 numpy 首次导入（经下方
# src.features.color.tracking.* 触发）之前限制 OpenBLAS 为单线程；
# tests/conftest.py 里有同款的配置级兜底（先于其 numpy 导入）。
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")

import pytest

from src.features.color.tracking.tracker_2d import TrackRequest, Tracker2D
from src.features.color.tracking.tracking_worker import TrackingWorker
from src.features.color.tracking.types import TrackingMode

from tests.color import tracker_support as ts

pytestmark = [
    pytest.mark.usefixtures("qapp"),
    pytest.mark.skipif(not ts.TOOLS_AVAILABLE, reason="tracking fixture 工具不可用"),
]


def pump_until(predicate, qapp, timeout_s: float = 30.0) -> bool:
    """驱动事件循环直到条件成立或超时。"""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        qapp.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    return False


def ensure_worker_stopped(worker: TrackingWorker, timeout_ms: int = 5000) -> None:
    """测试退出前确保线程结束。

    断言失败/超时后局部 worker 被 GC，若线程仍在运行会触发
    ``QThread: Destroyed while thread is still running`` 并 Abort 进程。
    所有 start() 过的 worker 都必须在 finally 中走这里。
    """
    try:
        if worker.isRunning():
            worker.request_cancel()
        worker.wait(timeout_ms)
    except RuntimeError:
        pass


@pytest.fixture(scope="module")
def fixture_dir() -> str:
    return ts.fixture_dir()


class TestWorkerLifecycle:
    def test_run_to_completion_emits_result(self, qapp, fixture_dir):
        req = ts.build_request(
            fixture_dir, "translation", TrackingMode.TRANSLATION, generation=3
        )
        worker = TrackingWorker(req)
        results, progresses = [], []
        worker.finished_with_result.connect(lambda g, r: results.append((g, r)))
        worker.progress.connect(lambda g, d, t: progresses.append((g, d, t)))

        try:
            worker.start()
            assert pump_until(lambda: results, qapp), "worker 未在超时内完成"
            worker.wait(2000)

            gen, result = results[0]
            assert gen == 3
            assert result.generation == 3
            assert result.cancelled is False
            assert len(result.transforms) == 63  # 参考帧 + 前向 62 帧
            # 进度信号：代次一致、单调递增至总数
            assert progresses, "未收到进度信号"
            assert all(g == 3 for g, _, _ in progresses)
            assert progresses[-1][1] == progresses[-1][2]
        finally:
            ensure_worker_stopped(worker)

    def test_cancel_before_start_yields_cancelled_result(self, qapp, fixture_dir):
        req = ts.build_request(fixture_dir, "translation", TrackingMode.TRANSLATION)
        worker = TrackingWorker(req)
        results = []
        worker.finished_with_result.connect(lambda g, r: results.append(r))
        worker.request_cancel()
        assert worker.cancel_requested()
        try:
            worker.start()
            assert pump_until(lambda: results, qapp)
            worker.wait(2000)
            assert results[0].cancelled is True
            # 只有参考帧结果
            assert set(results[0].transforms) == {req.reference_frame}
        finally:
            ensure_worker_stopped(worker)

    def test_invalid_media_emits_error(self, qapp, tmp_path):
        req = TrackRequest(
            media_path=str(tmp_path / "missing.mp4"),
            pattern_region=(0.3, 0.3, 0.2, 0.2),
            search_region=(0.1, 0.1, 0.6, 0.6),
            reference_frame=10,
            frame_start=0,
            frame_end=30,
            mode=TrackingMode.TRANSLATION,
            generation=1,
        )
        worker = TrackingWorker(req)
        errors = []
        worker.failed_with_error.connect(lambda g, m: errors.append((g, m)))
        try:
            worker.start()
            assert pump_until(lambda: errors, qapp), "worker 未报告错误"
            worker.wait(2000)
            gen, message = errors[0]
            assert gen == 1
            assert message != ""
        finally:
            ensure_worker_stopped(worker)

    def test_worker_uses_injected_tracker(self, qapp, fixture_dir):
        """可注入自定义 Tracker2D（配置/测试替身）。"""
        req = ts.build_request(fixture_dir, "translation", TrackingMode.TRANSLATION)
        tracker = Tracker2D()
        worker = TrackingWorker(req, tracker=tracker)
        assert worker.tracker is tracker

    def test_generation_property(self, fixture_dir):
        req = ts.build_request(
            fixture_dir, "translation", TrackingMode.TRANSLATION, generation=9
        )
        worker = TrackingWorker(req)
        assert worker.generation == 9
