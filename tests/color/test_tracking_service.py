"""阶段 3 验收：跟踪任务服务门面（假 worker 驱动的确定性测试）。"""

import numpy as np
import pytest

from src.features.color.tracking.tracker_2d import TrackDirection
from src.features.color.tracking.types import (
    FailureReason,
    TrackStatus,
    TrackTransform,
    TrackingMode,
)

from tests.color import tracker_qt_support as tq

pytestmark = pytest.mark.usefixtures("qapp")


class TestServiceStart:
    def test_start_creates_worker_per_direction(self, qapp):
        service, workers, events = tq.make_service()
        gen = tq.start_default(service)
        assert gen == 1
        assert len(workers) == 2  # forward + backward
        directions = {w.request.direction for w in workers}
        assert directions == {TrackDirection.FORWARD, TrackDirection.BACKWARD}
        assert all(w.started for w in workers)
        assert all(w.request.generation == 1 for w in workers)
        assert events["started"] == [("clip1:shape1", 1)]
        assert service.is_running("clip1", "shape1")

    def test_single_direction(self, qapp):
        service, workers, _ = tq.make_service()
        tq.start_default(service, directions=(TrackDirection.FORWARD,))
        assert len(workers) == 1

    def test_result_finished_and_cached(self, qapp):
        service, workers, events = tq.make_service()
        gen = tq.start_default(service, directions=(TrackDirection.FORWARD,))
        workers[0].finished_with_result.emit(gen, tq.make_result(gen))
        assert len(events["finished"]) == 1
        key, g, result = events["finished"][0]
        assert (key, g) == ("clip1:shape1", gen)
        assert result.generation == gen
        # 已写入缓存：再次启动同参数不创建新 worker
        before = len(workers)
        gen2 = tq.start_default(service, directions=(TrackDirection.FORWARD,))
        assert gen2 == 2
        assert len(workers) == before, "缓存命中不应创建新 worker"
        key2, g2, result2 = events["finished"][1]
        assert g2 == 2
        assert result2.generation == 2, "缓存结果必须携带当前代次"
        assert set(result2.transforms) == {63, 64, 65}

    def test_pattern_change_bypasses_cache(self, qapp):
        service, workers, _ = tq.make_service()
        gen = tq.start_default(service, directions=(TrackDirection.FORWARD,))
        workers[0].finished_with_result.emit(gen, tq.make_result(gen))
        before = len(workers)
        tq.start_default(
            service, directions=(TrackDirection.FORWARD,),
            pattern_region=(0.40, 0.35, 0.25, 0.30),
        )
        assert len(workers) == before + 1, "参数变化必须重新计算"

    def test_mutual_exclusion_cancels_previous(self, qapp):
        service, workers, _ = tq.make_service()
        tq.start_default(service)
        old_workers = list(workers)
        assert service.is_running("clip1", "shape1")
        gen2 = tq.start_default(service)
        assert gen2 == 2
        assert all(w.cancel_called for w in old_workers), "旧任务必须被取消"
        assert service.generation_of("clip1", "shape1") == 2

    def test_progress_forwarded_only_for_current_generation(self, qapp):
        service, workers, events = tq.make_service()
        gen = tq.start_default(service, directions=(TrackDirection.FORWARD,))
        workers[0].progress.emit(gen, 10, 62)
        workers[0].progress.emit(gen + 5, 20, 62)  # 陈旧代次
        assert events["progress"] == [("clip1:shape1", gen, 10, 62)]


class TestServiceCancelInvalidate:
    def test_cancel_requests_worker_cancel(self, qapp):
        service, workers, _ = tq.make_service()
        tq.start_default(service)
        service.cancel("clip1", "shape1")
        assert all(w.cancel_called for w in workers)
        assert not service.is_running("clip1", "shape1")

    def test_cancelled_result_emits_cancelled(self, qapp):
        service, workers, events = tq.make_service()
        gen = tq.start_default(service, directions=(TrackDirection.FORWARD,))
        workers[0].finished_with_result.emit(gen, tq.make_result(gen, cancelled=True))
        assert events["cancelled"] == [("clip1:shape1", gen)]
        assert events["finished"] == []

    def test_invalidate_bumps_generation_and_cancels(self, qapp):
        service, workers, events = tq.make_service()
        gen = tq.start_default(service)
        service.invalidate(clip_id="clip1")
        assert all(w.cancel_called for w in workers)
        assert service.generation_of("clip1", "shape1") == gen + 1
        # 之后到达的旧结果被丢弃
        workers[0].finished_with_result.emit(gen, tq.make_result(gen))
        assert events["stale"] == [("clip1:shape1", gen)]
        assert events["finished"] == []

    def test_invalidate_only_targets_given_clip(self, qapp):
        service, workers, _ = tq.make_service()
        tq.start_default(service, clip_id="clipA")
        tq.start_default(service, clip_id="clipB")
        assert service.is_running("clipA", "shape1")
        assert service.is_running("clipB", "shape1")
        service.invalidate(clip_id="clipA")
        assert service.generation_of("clipA", "shape1") == 2
        assert service.generation_of("clipB", "shape1") == 1

    def test_shutdown_cancels_all(self, qapp):
        service, workers, _ = tq.make_service()
        tq.start_default(service, clip_id="clipA")
        tq.start_default(service, clip_id="clipB")
        service.shutdown()
        assert all(w.cancel_called for w in workers)
        assert service.running_keys() == []


class TestServiceRetrackFailed:
    def _binding_transforms(self):
        """63..70 有效，71..75 失败。"""
        transforms = {}
        for f in range(63, 71):
            transforms[f] = TrackTransform.from_matrix(
                f, np.eye(3), 0.9, TrackStatus.VALID
            )
        for f in range(71, 76):
            transforms[f] = TrackTransform.failed(f, FailureReason.OCCLUSION)
        return transforms

    def test_retrack_no_failed_is_noop(self, qapp):
        service, workers, _ = tq.make_service()
        transforms = {
            63: TrackTransform.from_matrix(63, np.eye(3), 0.9, TrackStatus.VALID)
        }
        gen = service.retrack_failed(
            "clip1", "shape1", transforms, tq.MEDIA, tq.PATTERN, tq.SEARCH, 62, 0, 124,
        )
        assert gen == 0
        assert workers == []

    def test_retrack_filters_merged_frames(self, qapp):
        """重跟踪结果只保留失败段及其上下文的帧。"""
        service, workers, events = tq.make_service()
        transforms = self._binding_transforms()
        gen = service.retrack_failed(
            "clip1", "shape1", transforms, tq.MEDIA, tq.PATTERN, tq.SEARCH, 62, 0, 124,
            directions=(TrackDirection.FORWARD,),
        )
        assert len(workers) == 1
        # worker 返回全量结果（63..80）
        full = tq.make_result(gen, frames=tuple(range(63, 81)))
        workers[0].finished_with_result.emit(gen, full)
        assert len(events["finished"]) == 1
        _, _, result = events["finished"][0]
        kept = set(result.transforms)
        # 失败段 71..75 + 上下文外扩 3 帧（68..78），且不含参考帧 62
        assert {71, 72, 73, 74, 75} <= kept
        assert kept <= set(range(68, 79))
        assert 63 not in kept, "远离失败段的有效帧不得被覆盖"

    def test_retrack_cached_path_also_filters(self, qapp):
        """缓存命中路径同样应用合并过滤。"""
        service, workers, events = tq.make_service()
        # 先跑一次全量并缓存
        gen = tq.start_default(service, directions=(TrackDirection.FORWARD,))
        full = tq.make_result(gen, frames=tuple(range(63, 81)))
        workers[0].finished_with_result.emit(gen, full)
        assert len(workers) == 1

        transforms = self._binding_transforms()
        gen2 = service.retrack_failed(
            "clip1", "shape1", transforms, tq.MEDIA, tq.PATTERN, tq.SEARCH, 62, 0, 124,
            directions=(TrackDirection.FORWARD,),
        )
        assert len(workers) == 1, "缓存命中不应创建新 worker"
        finished = [e for e in events["finished"] if e[1] == gen2]
        assert len(finished) == 1
        _, _, result = finished[0]
        kept = set(result.transforms)
        assert {71, 72, 73, 74, 75} <= kept <= set(range(68, 79))


class TestServiceErrors:
    def test_worker_error_forwarded(self, qapp):
        service, workers, events = tq.make_service()
        gen = tq.start_default(service, directions=(TrackDirection.FORWARD,))
        workers[0].failed_with_error.emit(gen, "解码失败")
        assert events["failed"] == [("clip1:shape1", gen, "解码失败")]
