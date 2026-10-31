"""P1-02: 帧任务优先级协议单元测试。"""

import queue


import pytest

from src.features.edit.core.realtime_playback.task_priority import (
    FrameTaskPriority,
    eviction_decision,
    priority_name,
    resolve_request_priority,
)


class TestResolvePriority:
    def test_scrub_is_highest(self):
        assert (resolve_request_priority("scrub")
                == FrameTaskPriority.SCRUB_LATEST)

    def test_playhead_default(self):
        assert (resolve_request_priority("")
                == FrameTaskPriority.PLAYHEAD)
        assert (resolve_request_priority("unknown_kind")
                == FrameTaskPriority.PLAYHEAD)

    def test_background_maps_cache_fill(self):
        assert (resolve_request_priority("idle_prebuffer")
                == FrameTaskPriority.CACHE_FILL)
        assert (resolve_request_priority("anything", background=True)
                == FrameTaskPriority.CACHE_FILL)

    def test_priority_ordering_matches_protocol(self):
        # scrub latest > playhead > prefetch > cache fill > thumbnail > export
        order = [
            FrameTaskPriority.SCRUB_LATEST,
            FrameTaskPriority.PLAYHEAD,
            FrameTaskPriority.PREFETCH,
            FrameTaskPriority.CACHE_FILL,
            FrameTaskPriority.THUMBNAIL,
            FrameTaskPriority.EXPORT,
        ]
        assert order == sorted(order)


class TestEvictionDecision:
    def test_high_priority_evicts_pending_low(self):
        verdict = eviction_decision(
            FrameTaskPriority.SCRUB_LATEST, FrameTaskPriority.CACHE_FILL)
        assert verdict.action == "evict_pending"
        assert verdict.evicted_priority == int(FrameTaskPriority.CACHE_FILL)

    def test_same_priority_latest_wins(self):
        verdict = eviction_decision(
            FrameTaskPriority.PLAYHEAD, FrameTaskPriority.PLAYHEAD)
        assert verdict.action == "evict_pending"

    def test_low_priority_never_evicts_high(self):
        for low in (FrameTaskPriority.CACHE_FILL, FrameTaskPriority.THUMBNAIL,
                    FrameTaskPriority.EXPORT):
            verdict = eviction_decision(low, FrameTaskPriority.PLAYHEAD)
            assert verdict.action == "drop_incoming", low
            assert verdict.evicted_priority is None

    def test_scrub_beats_everything(self):
        for pending in FrameTaskPriority:
            if pending is FrameTaskPriority.SCRUB_LATEST:
                continue
            verdict = eviction_decision(FrameTaskPriority.SCRUB_LATEST, pending)
            assert verdict.action == "evict_pending"


class TestPriorityName:
    def test_names_lowercase(self):
        assert priority_name(0) == "scrub_latest"
        assert priority_name(30) == "cache_fill"

    def test_unknown_value(self):
        assert priority_name(999) == "unknown"


def _task(frame, request_kind, background=False):
    """构造 worker 队列的 7 元组任务（task_meta 在索引 6）。"""
    meta = {"request_kind": request_kind, "frame": frame}
    if background:
        meta["background"] = True
    return (frame, None, 1, False, {}, None, meta)


def _make_worker_stub(queue_obj):
    """借用 PreviewRenderWorker 的纯 Python 方法构造轻量 stub。

    enqueue/_pending_head_priority 只访问 self._queue / self._generation_id
    （无 Qt 调用），无需 QThread 重量级构造。
    """
    from src.features.edit.core.timeline_preview_renderer import (
        PreviewRenderWorker,
    )

    class _WorkerStub:
        _pending_head_priority = PreviewRenderWorker._pending_head_priority
        enqueue = PreviewRenderWorker.enqueue

        def __init__(self, q):
            self._queue = q
            self._generation_id = 1

    return _WorkerStub(queue_obj)


class TestWorkerQueuePriorityIntegration:
    """enqueue 满队列裁决 + 队头窥视的 worker 级集成测试。"""

    @staticmethod
    def _make_worker():
        return _make_worker_stub(queue.Queue(maxsize=2))

    def test_head_priority_empty_queue_is_none(self):
        worker = self._make_worker()
        assert worker._pending_head_priority() is None

    def test_head_priority_resolves_task_meta(self):
        worker = self._make_worker()
        worker._queue.put_nowait(_task(5, "playhead"))
        assert worker._pending_head_priority() == FrameTaskPriority.PLAYHEAD
        worker._queue.get_nowait()
        worker._queue.put_nowait(_task(6, "idle_prebuffer"))
        assert worker._pending_head_priority() == FrameTaskPriority.CACHE_FILL
        worker._queue.get_nowait()
        worker._queue.put_nowait(_task(7, "scrub"))
        assert worker._pending_head_priority() == FrameTaskPriority.SCRUB_LATEST

    def test_head_priority_does_not_mutate_queue_accounting(self):
        # 回归：窥视必须是纯读——qsize 与 unfinished_tasks 都不得变化。
        worker = self._make_worker()
        worker._queue.put_nowait(_task(5, "playhead"))
        before = (worker._queue.qsize(), worker._queue.unfinished_tasks)
        assert worker._pending_head_priority() == FrameTaskPriority.PLAYHEAD
        after = (worker._queue.qsize(), worker._queue.unfinished_tasks)
        assert before == after
        # 窥视后再正常 get/task_done，join 必须能正常返回（会计未损坏）。
        worker._queue.get_nowait()
        worker._queue.task_done()
        worker._queue.join()

    def test_head_priority_tolerates_malformed_tuple(self):
        worker = self._make_worker()
        worker._queue.put_nowait((1, 2, 3))  # 旧形状/坏 tuple 不得抛异常
        assert worker._pending_head_priority() == FrameTaskPriority.PLAYHEAD


class _RaceFullQueue(queue.Queue):
    """模拟并发生产者抢占最后空位导致 put_nowait 抛 Full 的竞态。"""

    def __init__(self):
        super().__init__(maxsize=2)
        self._race_pending = None

    def arm_race(self, pending_task):
        self._race_pending = pending_task

    def put_nowait(self, item):
        if self._race_pending is not None:
            pending, self._race_pending = self._race_pending, None
            super().put_nowait(pending)
            raise queue.Full
        return super().put_nowait(item)


class TestWorkerEnqueueFullQueueVerdict:
    """满队列分支的协议裁决（低优先级丢弃 / 高优先级替换）。"""

    @staticmethod
    def _make_worker():
        return _make_worker_stub(_RaceFullQueue())

    def test_low_priority_incoming_dropped_keeps_pending(self):
        worker = self._make_worker()
        worker._queue.arm_race(_task(50, "playhead"))
        queued = worker.enqueue(
            60, None, 1, request_kind="idle_prebuffer",
        )
        assert queued is False
        assert worker._queue.qsize() == 1
        head = worker._queue.queue[0]
        assert head[6]["request_kind"] == "playhead"

    def test_high_priority_incoming_replaces_pending(self):
        worker = self._make_worker()
        worker._queue.arm_race(_task(50, "playhead"))
        queued = worker.enqueue(70, None, 1, request_kind="scrub")
        assert queued is True
        assert worker._queue.qsize() == 1
        head = worker._queue.queue[0]
        assert head[6]["request_kind"] == "scrub"
        assert head[6]["priority"] == "scrub_latest"


class TestDispatchChannel:
    """P1-01: 路由通道正字段随 task_meta 贯通。"""

    def _make_worker(self):
        return _make_worker_stub(queue.Queue(maxsize=8))

    def test_enqueue_records_dispatch_channel(self):
        worker = self._make_worker()
        assert worker.enqueue(
            10, None, 1, request_kind="scrub", dispatch_channel="scrub",
            trace_id=7,
        ) is True
        meta = worker._queue.queue[0][6]
        assert meta["dispatch_channel"] == "scrub"
        assert meta["trace_id"] == 7
        assert meta["priority"] == "scrub_latest"

    def test_enqueue_default_channel_direct(self):
        worker = self._make_worker()
        worker.enqueue(11, None, 1)
        meta = worker._queue.queue[0][6]
        assert "dispatch_channel" not in meta  # core 路径在调用方决定通道


class TestEngineScrubPresent:
    """P1-01: engine.request_scrub_present 外壳语义。"""

    def _engine(self, **kw):
        from src.features.edit.core.realtime_playback.engine import (
            RealtimePlaybackEngine,
        )

        engine = RealtimePlaybackEngine()
        engine.configure(**kw)
        return engine

    def test_scrub_delegate_receives_trace_id_and_payload(self):
        calls = []

        def delegate(frame, deadline_ns, trace_id, payload):
            calls.append((frame, deadline_ns, trace_id, payload))
            return True

        engine = self._engine(delegate_scrub_render=delegate)
        ok = engine.request_scrub_present(
            42, {"token": 5}, deadline_ns=33_300_000,
        )
        assert ok is True
        frame, deadline, trace_id, payload = calls[0]
        assert frame == 42 and deadline == 33_300_000
        assert trace_id >= 1  # engine 分配的统一 id（scrub 帧不再为 0）
        assert payload == {"token": 5}

    def test_scrub_telemetry_priority_and_mode(self):
        engine = self._engine(
            delegate_scrub_render=lambda f, d, t, p: True)
        engine.request_scrub_present(7, {})
        records = engine.telemetry._records
        assert records[-1].priority == "scrub_latest"
        assert records[-1].mode == "scrub"
        assert records[-1].miss_reason is None
        assert records[-1].trace_id >= 1

    def test_scrub_rejection_records_miss(self):
        engine = self._engine(
            delegate_scrub_render=lambda f, d, t, p: False)
        ok = engine.request_scrub_present(9, {})
        assert ok is False
        record = engine.telemetry._records[-1]
        assert record.miss_reason == "scrub_dispatch_rejected"

    def test_scrub_delegate_exception_is_contained(self):
        def bad(frame, deadline_ns, trace_id, payload):
            raise RuntimeError("boom")

        engine = self._engine(delegate_scrub_render=bad)
        assert engine.request_scrub_present(3, {}) is False

    def test_no_delegate_returns_false(self):
        engine = self._engine()
        assert engine.request_scrub_present(3, {}) is False
