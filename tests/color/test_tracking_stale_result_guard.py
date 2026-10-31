"""阶段 3 验收：陈旧结果保护（generation 匹配，旧任务结果绝不污染新状态）。"""

import pytest

from src.features.color.tracking.tracker_2d import TrackDirection

from tests.color import tracker_qt_support as tq

pytestmark = pytest.mark.usefixtures("qapp")


class TestStaleResultGuard:
    def test_late_result_after_new_start_is_discarded(self, qapp):
        """旧 worker 的结果在新任务启动后到达 → 丢弃，不触发 finished。"""
        service, workers, events = tq.make_service()
        gen1 = tq.start_default(service, directions=(TrackDirection.FORWARD,))
        old_worker = workers[0]

        gen2 = tq.start_default(service, directions=(TrackDirection.FORWARD,))
        assert gen2 == gen1 + 1

        old_worker.finished_with_result.emit(gen1, tq.make_result(gen1))
        assert events["stale"] == [("clip1:shape1", gen1)]
        assert events["finished"] == []

        # 新任务的结果正常接受
        workers[1].finished_with_result.emit(gen2, tq.make_result(gen2))
        assert len(events["finished"]) == 1
        assert events["finished"][0][1] == gen2

    def test_late_result_after_invalidate_is_discarded(self, qapp):
        """换片段/换参考帧作废旧任务后，旧结果必须丢弃。"""
        service, workers, events = tq.make_service()
        gen = tq.start_default(service, directions=(TrackDirection.FORWARD,))
        service.invalidate(clip_id="clip1")

        workers[0].finished_with_result.emit(gen, tq.make_result(gen))
        assert events["stale"] == [("clip1:shape1", gen)]
        assert events["finished"] == []
        assert len(service.cache) == 0, "陈旧结果不得写入缓存"

    def test_late_error_after_invalidate_is_discarded(self, qapp):
        service, workers, events = tq.make_service()
        gen = tq.start_default(service, directions=(TrackDirection.FORWARD,))
        service.invalidate()
        workers[0].failed_with_error.emit(gen, "太迟的错误")
        assert events["failed"] == []
        assert events["stale"] == [("clip1:shape1", gen)]

    def test_late_progress_is_ignored(self, qapp):
        service, workers, events = tq.make_service()
        gen = tq.start_default(service, directions=(TrackDirection.FORWARD,))
        tq.start_default(service, directions=(TrackDirection.FORWARD,))
        workers[0].progress.emit(gen, 5, 62)
        assert events["progress"] == []

    def test_generation_propagates_end_to_end(self, qapp):
        """generation 从请求 → worker → 结果全链路一致。"""
        service, workers, events = tq.make_service()
        gen = tq.start_default(service, directions=(TrackDirection.FORWARD,))
        assert workers[0].request.generation == gen
        result = tq.make_result(gen)
        assert result.generation == gen
        workers[0].finished_with_result.emit(gen, result)
        _, g, r = events["finished"][0]
        assert g == gen and r.generation == gen

    def test_results_areolated_between_shapes(self, qapp):
        """不同形状的代次互相独立，互不作废。"""
        service, workers, events = tq.make_service()
        gen_a = tq.start_default(service, shape_id="shapeA",
                                 directions=(TrackDirection.FORWARD,))
        gen_b = tq.start_default(service, shape_id="shapeB",
                                 directions=(TrackDirection.FORWARD,))
        assert gen_a == 1 and gen_b == 1  # 各自从 1 开始

        workers[0].finished_with_result.emit(gen_a, tq.make_result(gen_a))
        workers[1].finished_with_result.emit(gen_b, tq.make_result(gen_b))
        keys = {e[0] for e in events["finished"]}
        assert keys == {"clip1:shapeA", "clip1:shapeB"}
        assert events["stale"] == []

    def test_rapid_restart_never_applies_old_result(self, qapp):
        """连续多次重启：只有最后一次的结果被接受。"""
        service, workers, events = tq.make_service()
        gens = []
        for _ in range(4):
            gens.append(tq.start_default(service, directions=(TrackDirection.FORWARD,)))
        # 所有历史结果迟到
        for i, gen in enumerate(gens[:-1]):
            workers[i].finished_with_result.emit(gen, tq.make_result(gen))
        assert len(events["stale"]) == 3
        assert events["finished"] == []
        # 最后一次有效
        workers[-1].finished_with_result.emit(gens[-1], tq.make_result(gens[-1]))
        assert len(events["finished"]) == 1
        assert events["finished"][0][1] == gens[-1]
