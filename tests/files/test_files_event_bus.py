"""C3：事件总线（可注销句柄 / 库作用域 / 合并与背压 / 关闭撤销）。"""
from __future__ import annotations

import threading
import time

import pytest

from src.features.files.services.events import (
    TOPIC_ASSETS_CHANGED,
    TOPIC_SCAN_PROGRESS,
    Event,
    FilesEventBus,
)


def _deliveries(events: list[Event]) -> list[str]:
    return [event.payload.get("mark", "") for event in events]


@pytest.fixture()
def bus():
    instance = FilesEventBus()
    yield instance
    instance.close_all()


class _Sink:
    def __init__(self) -> None:
        self.events: list[Event] = []
        self.lock = threading.Lock()

    def __call__(self, event: Event) -> None:
        with self.lock:
            self.events.append(event)

    def wait_for(self, count: int, timeout: float = 5.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self.lock:
                if len(self.events) >= count:
                    return
            time.sleep(0.01)
        raise AssertionError(f"事件不足：{len(self.events)}/{count}")


def test_delivery_with_scope_and_identity(bus):
    sink_a = _Sink()
    sink_all = _Sink()
    bus.subscribe(TOPIC_ASSETS_CHANGED, sink_a, library_id="lib-a")
    bus.subscribe(TOPIC_ASSETS_CHANGED, sink_all)

    bus.publish(TOPIC_ASSETS_CHANGED, {"mark": "a1"}, library_id="lib-a", revision=7)
    bus.publish(TOPIC_ASSETS_CHANGED, {"mark": "b1"}, library_id="lib-b", revision=8)

    sink_a.wait_for(1)
    sink_all.wait_for(2)
    assert _deliveries(sink_a.events) == ["a1"], "库作用域过滤生效"
    assert _deliveries(sink_all.events) == ["a1", "b1"]
    assert sink_all.events[0].revision == 7 and sink_all.events[0].library_id == "lib-a"


def test_unsubscribe_handle_stops_delivery(bus):
    sink = _Sink()
    unsubscribe = bus.subscribe(TOPIC_ASSETS_CHANGED, sink)
    bus.publish(TOPIC_ASSETS_CHANGED, {"mark": "first"})
    sink.wait_for(1)
    unsubscribe()
    unsubscribe()  # 幂等
    bus.publish(TOPIC_ASSETS_CHANGED, {"mark": "after"})
    time.sleep(0.15)
    assert _deliveries(sink.events) == ["first"]


def test_progress_coalesces_last_wins(bus):
    """监听者阻塞期间大量 progress 合并为最新值，不无界排队。"""
    release = threading.Event()
    received: list[Event] = []
    lock = threading.Lock()

    def _slow_listener(event: Event) -> None:
        release.wait(5.0)
        with lock:
            received.append(event)

    bus.subscribe(TOPIC_SCAN_PROGRESS, _slow_listener)
    for index in range(200):
        bus.publish(TOPIC_SCAN_PROGRESS, {"mark": str(index)})
    time.sleep(0.2)  # 分发线程被第一个事件阻塞，后续全在合并槽
    stats = list(bus.stats().values())[0]
    assert stats["pending"] <= 2, "200 个 progress 只留最新"
    assert stats["coalesced"] >= 100, "合并计数可观测"
    release.set()
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        with lock:
            if received and received[-1].payload.get("mark") == "199":
                break
        time.sleep(0.02)
    assert received, "阻塞解除后至少送达"
    assert received[-1].payload["mark"] == "199", "最新值存活"
    assert len(received) < 200, "没有按 200 条原样排队"


def test_bounded_queue_drops_oldest_with_counter(bus):
    """慢监听者的普通事件 drop-oldest 且计数，不反压发布方。"""
    release = threading.Event()

    def _slow(event: Event) -> None:
        release.wait(5.0)

    slow_unsub = bus.subscribe(TOPIC_ASSETS_CHANGED, _slow)
    fast = _Sink()
    bus.subscribe(TOPIC_ASSETS_CHANGED, fast)

    for index in range(80):  # 超过 QUEUE_LIMIT=50
        bus.publish(TOPIC_ASSETS_CHANGED, {"mark": str(index)})
    # 慢监听者仍被阻塞：其队列 drop-oldest 计数已可观测
    stats = bus.stats()
    slow_stat = next(s for s in stats.values() if s["dropped"] >= 30)
    release.set()
    slow_unsub()
    fast.wait_for(1)
    time.sleep(0.1)
    assert len(fast.events) <= 50, "快监听者同样有界"
    assert fast.events[-1].payload["mark"] == "79", "最新事件存活（drop-oldest）"


def test_close_all_cancels_every_subscription(bus):
    sink = _Sink()
    bus.subscribe(TOPIC_ASSETS_CHANGED, sink)
    bus.close_all()
    bus.publish(TOPIC_ASSETS_CHANGED, {"mark": "late"})
    time.sleep(0.1)
    assert sink.events == []
    with pytest.raises(RuntimeError):
        bus.subscribe(TOPIC_ASSETS_CHANGED, sink)


def test_listener_exception_does_not_kill_bus(bus):
    calls: list[str] = []

    def _bad(event: Event) -> None:
        calls.append("bad")

        raise RuntimeError("监听者崩溃")

    good = _Sink()
    bus.subscribe(TOPIC_ASSETS_CHANGED, _bad)
    bus.subscribe(TOPIC_ASSETS_CHANGED, good)
    bus.publish(TOPIC_ASSETS_CHANGED, {"mark": "1"})
    bus.publish(TOPIC_ASSETS_CHANGED, {"mark": "2"})
    good.wait_for(2)
    assert len(calls) == 2, "坏监听者被隔离，总线继续投递"
