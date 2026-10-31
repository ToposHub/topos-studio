"""阶段 2 验收：取消机制与任务代次（generation）。"""

import pytest

from src.shared.tracking.tracker_2d import Tracker2D
from src.shared.tracking.types import TrackingMode

from tests.shared.tracking import tracker_support as ts

pytestmark = pytest.mark.skipif(
    not ts.TOOLS_AVAILABLE, reason="tracking fixture 工具不可用"
)


class _Token:
    """最小 cancel token：提供 is_set()。"""

    def __init__(self) -> None:
        self._set = False

    def set(self) -> None:
        self._set = True

    def is_set(self) -> bool:
        return self._set


@pytest.fixture(scope="module")
def fixture_dir() -> str:
    return ts.fixture_dir()


class TestCancellation:
    def test_cancel_before_start_stops_immediately(self, fixture_dir):
        tracker = Tracker2D()
        req = ts.build_request(fixture_dir, "translation", TrackingMode.TRANSLATION)
        token = _Token()
        token.set()
        result = tracker.track(req, cancel_token=token)
        assert result.cancelled is True
        # 除参考帧外不应产生任何结果
        assert set(result.transforms) == {req.reference_frame}

    def test_cancel_midway_keeps_completed_frames(self, fixture_dir):
        """中途取消：已完成帧保留，结果标记 cancelled。"""
        tracker = Tracker2D()
        req = ts.build_request(fixture_dir, "translation", TrackingMode.TRANSLATION)
        token = _Token()
        cancelled_at = []

        def progress(done: int, total: int) -> None:
            if done >= 5:
                token.set()
                cancelled_at.append(done)

        result = tracker.track(req, cancel_token=token, progress_cb=progress)
        assert result.cancelled is True
        assert cancelled_at, "progress 回调未被调用"
        n = len(result.transforms)
        # 参考帧 + 少量已完成帧（远少于全部 62 帧）
        assert 1 < n < 62

    def test_no_cancel_runs_to_completion(self, fixture_dir):
        tracker = Tracker2D()
        req = ts.build_request(fixture_dir, "translation", TrackingMode.TRANSLATION)
        token = _Token()
        result = tracker.track(req, cancel_token=token)
        assert result.cancelled is False
        # 单向：参考帧 + 62 帧 = 63（双向合并才覆盖全部 125 帧）
        assert len(result.transforms) == 63


class TestGeneration:
    def test_generation_echoed_in_result(self, fixture_dir):
        tracker = Tracker2D()
        for gen in (0, 1, 42):
            req = ts.build_request(
                fixture_dir, "translation", TrackingMode.TRANSLATION,
                generation=gen,
            )
            result = tracker.track(req)
            assert result.generation == gen

    def test_generation_survives_cancellation(self, fixture_dir):
        tracker = Tracker2D()
        req = ts.build_request(
            fixture_dir, "translation", TrackingMode.TRANSLATION,
            generation=7,
        )
        token = _Token()
        token.set()
        result = tracker.track(req, cancel_token=token)
        assert result.generation == 7
        assert result.cancelled is True


class TestProgressCallback:
    def test_progress_monotonic_and_complete(self, fixture_dir):
        tracker = Tracker2D()
        req = ts.build_request(fixture_dir, "translation", TrackingMode.TRANSLATION)
        calls = []
        tracker.track(req, progress_cb=lambda d, t: calls.append((d, t)))
        assert len(calls) == req.frame_end - req.reference_frame  # 62 帧
        dones = [d for d, _ in calls]
        assert dones == sorted(dones)
        assert dones[-1] == len(calls)
        assert all(t == len(calls) for _, t in calls)

    def test_progress_callback_exception_does_not_break_tracking(self, fixture_dir):
        """回调抛异常不影响跟踪本身。"""
        tracker = Tracker2D()
        req = ts.build_request(fixture_dir, "translation", TrackingMode.TRANSLATION)

        def bad_cb(done: int, total: int) -> None:
            raise RuntimeError("boom")

        result = tracker.track(req, progress_cb=bad_cb)
        assert result.cancelled is False
        assert len(result.transforms) == 63


class TestSamplerReuse:
    def test_external_sampler_not_closed(self, fixture_dir):
        """外部传入的采样器由调用方管理，跟踪结束不得关闭它。"""
        import os
        from src.shared.tracking.frame_sampler import FrameSampler

        tracker = Tracker2D()
        req = ts.build_request(fixture_dir, "translation", TrackingMode.TRANSLATION)
        sampler = FrameSampler(os.path.join(fixture_dir, "translation.mp4"))
        try:
            result = tracker.track(req, sampler=sampler)
            assert len(result.transforms) == 63
            # 采样器仍可继续使用
            sample = sampler.read_gray(0, req.quality)
            assert sample.frame_index == 0
        finally:
            sampler.close()
