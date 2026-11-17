"""Files 事件总线（C 阶段，总计划 C 表第 5 行）。

- 订阅返回**可注销句柄**，事件携带库作用域与 revision；
- progress 类事件按订阅者**合并（last-wins）**，普通事件有界队列
  drop-oldest 并计数——慢监听者不反压发布方、不无界积累；
- ``close_all()`` 撤销全部订阅（关闭面板/切换库），此后 publish 不投递。

线程口径：监听者在总线分发线程执行；Qt 调用方自行 marshal。
"""
from __future__ import annotations

import itertools
import threading
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable

TOPIC_ASSETS_CHANGED = "assets.changed"
TOPIC_SCAN_PROGRESS = "scan.progress"
TOPIC_TASK_PROGRESS = "task.progress"
TOPIC_TRASH_CHANGED = "trash.changed"
TOPIC_LIBRARY_CHANGED = "library.changed"

COALESCABLE = frozenset({TOPIC_SCAN_PROGRESS, TOPIC_TASK_PROGRESS})
QUEUE_LIMIT = 50


@dataclass(frozen=True)
class Event:
    topic: str
    library_id: str
    revision: int
    payload: dict[str, Any]


Listener = Callable[[Event], None]
Unsubscribe = Callable[[], None]


class _Subscriber:
    def __init__(self, sid: int, topic: str, library_id: str,
                 listener: Listener) -> None:
        self.sid = sid
        self.topic = topic
        self.library_id = library_id
        self.listener = listener
        self.queue: deque[Event] = deque(maxlen=QUEUE_LIMIT)
        self.progress_slot: Event | None = None
        self.dropped = 0
        self.coalesced = 0
        self.alive = True


class FilesEventBus:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._condition = threading.Condition(self._lock)
        self._subscribers: dict[int, _Subscriber] = {}
        self._ids = itertools.count(1)
        self._closed = False
        self._thread = threading.Thread(
            target=self._dispatch_loop, name="files-event-bus", daemon=True)
        self._thread.start()

    # ------------------------------------------------------------ 订阅

    def subscribe(self, topic: str, listener: Listener,
                  *, library_id: str = "") -> Unsubscribe:
        with self._condition:
            if self._closed:
                raise RuntimeError("事件总线已关闭")
            subscriber = _Subscriber(next(self._ids), topic, library_id, listener)
            self._subscribers[subscriber.sid] = subscriber
        return self._make_unsubscribe(subscriber.sid)

    def _make_unsubscribe(self, sid: int) -> Unsubscribe:
        def _unsubscribe() -> None:
            with self._condition:
                subscriber = self._subscribers.pop(sid, None)
                if subscriber is not None:
                    subscriber.alive = False
                self._condition.notify_all()

        return _unsubscribe

    def close_all(self) -> None:
        """撤销全部订阅；此后 publish 不再投递（关闭/切库）。"""
        with self._condition:
            self._closed = True
            for subscriber in self._subscribers.values():
                subscriber.alive = False
            self._subscribers.clear()
            self._condition.notify_all()

    # ------------------------------------------------------------ 发布

    def publish(self, topic: str, payload: dict[str, Any] | None = None, *,
                library_id: str = "", revision: int = 0) -> None:
        event = Event(topic=topic, library_id=library_id,
                      revision=revision, payload=dict(payload or {}))
        with self._condition:
            if self._closed:
                return
            for subscriber in self._subscribers.values():
                if not subscriber.alive or subscriber.topic != topic:
                    continue
                if subscriber.library_id and subscriber.library_id != library_id:
                    continue  # 库作用域过滤
                if topic in COALESCABLE:
                    if subscriber.progress_slot is not None:
                        subscriber.coalesced += 1
                    subscriber.progress_slot = event
                else:
                    if len(subscriber.queue) == subscriber.queue.maxlen:
                        subscriber.dropped += 1  # drop-oldest（deque 自动）
                    subscriber.queue.append(event)
            self._condition.notify_all()

    # ------------------------------------------------------------ 分发

    def _dispatch_loop(self) -> None:
        while True:
            deliverable: list[tuple[_Subscriber, Event]] = []
            with self._condition:
                while not self._has_pending():
                    if self._closed and not self._subscribers:
                        return
                    self._condition.wait(0.5)
                for subscriber in list(self._subscribers.values()):
                    if not subscriber.alive:
                        continue
                    if subscriber.progress_slot is not None:
                        deliverable.append((subscriber, subscriber.progress_slot))
                        subscriber.progress_slot = None
                    while subscriber.queue:
                        deliverable.append((subscriber, subscriber.queue.popleft()))
            for subscriber, event in deliverable:
                if not subscriber.alive:
                    continue
                try:
                    subscriber.listener(event)
                except Exception:  # 监听者异常不拖垮总线
                    pass

    def _has_pending(self) -> bool:
        return any(subscriber.alive and (subscriber.progress_slot is not None
                                         or subscriber.queue)
                   for subscriber in self._subscribers.values())

    # ------------------------------------------------------------ 统计（测试/诊断）

    def stats(self) -> dict[str, dict[str, int]]:
        with self._condition:
            return {
                f"sub-{subscriber.sid}": {
                    "dropped": subscriber.dropped,
                    "coalesced": subscriber.coalesced,
                    "pending": len(subscriber.queue)
                    + (1 if subscriber.progress_slot is not None else 0),
                }
                for subscriber in self._subscribers.values()
            }
