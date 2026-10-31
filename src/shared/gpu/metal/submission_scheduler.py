"""Bounded asynchronous Metal submission scheduler.

The timeline renderer used to call ``finish()`` for every frame.  This module
provides the small ownership layer required by M6: a fixed number of in-flight
submissions, completion-driven release, and explicit back-pressure.  It does
not hide a wait or silently grow a queue.

Threading contract: every public method takes the internal RLock and may be
called from the render/export thread; the completion handler runs on Metal's
own completion thread (never the submitter's), so ``release_callbacks`` and
``on_complete`` must be thread-safe and must not call back into ``submit``.
``drain`` is a shutdown-only lifecycle boundary — production frame loops
never wait on it.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MetalSubmission:
    """A submitted command buffer and its monotonic scheduler sequence."""

    sequence: int
    command_buffer: Any


class MetalSubmissionScheduler:
    """A bounded completion-handler scheduler for one Metal context.

    ``submit`` assumes the context has already encoded the current command
    buffer. If the ring is full it returns ``None`` without committing work;
    the caller must apply back-pressure or use a documented fallback.
    """

    def __init__(self, context: Any, max_in_flight: int = 4) -> None:
        """Bind the scheduler to one Metal compute context.

        Args:
            context: a `MetalComputeContext`-like object whose
                ``submit(completion=..., wait=False)`` commits its current
                command buffer.
            max_in_flight: ring capacity (>= 1). When reached, ``submit``
                returns ``None`` instead of queuing — the caller owns
                back-pressure.
        """
        if int(max_in_flight) < 1:
            raise ValueError("max_in_flight must be >= 1")
        self._context = context
        self._max = int(max_in_flight)
        self._lock = threading.RLock()
        self._next_sequence = 0
        self._pending: dict[int, MetalSubmission] = {}
        self._completed: list[int] = []
        self._errors: dict[int, str] = {}
        self._release_callbacks: dict[int, tuple[Callable[[], None], ...]] = {}
        # AUD-1523: _completed/_errors 只靠调用方自觉 poll()/error() 消费，
        # 长期不 poll 会无界增长。这里给出有界上限（诊断数据，允许溢出时
        # 丢最旧），溢出只告警一次避免刷屏。
        self._completed_limit = max(64, self._max * 16)
        self._errors_limit = self._completed_limit
        self._warned_unpolled = False

    @property
    def max_in_flight(self) -> int:
        """Ring capacity (constructor argument, fixed for lifetime)."""
        return self._max

    @property
    def in_flight(self) -> int:
        """Currently reserved-but-not-completed submission count."""
        with self._lock:
            return len(self._pending)

    def submit(
        self,
        *,
        release_callbacks: tuple[Callable[[], None], ...] = (),
        on_complete: Optional[Callable[[MetalSubmission, Any], None]] = None,
    ) -> Optional[MetalSubmission]:
        """Commit the context's pending command buffer if capacity exists."""
        with self._lock:
            if len(self._pending) >= self._max:
                return None
            sequence = self._next_sequence
            self._next_sequence += 1
            # Reserve the slot before calling into Metal. A test double, and
            # theoretically a very fast driver, may invoke the completion
            # handler synchronously from commit().
            self._pending[sequence] = None  # type: ignore[assignment]
            self._release_callbacks[sequence] = tuple(release_callbacks)

        def completed(command_buffer: Any) -> None:
            error = None
            try:
                error = command_buffer.error()
            except Exception as exc:  # pragma: no cover - defensive PyObjC edge
                error = exc
            with self._lock:
                if sequence not in self._pending:
                    return
                pending = self._pending.pop(sequence)
                submission = pending or MetalSubmission(sequence, command_buffer)
                if error is not None:
                    self._errors[sequence] = str(error)
                    self._prune_errors_locked()
                self._completed.append(sequence)
                self._bound_completed_locked()
                callbacks = self._release_callbacks.pop(sequence, ())
            for callback in callbacks:
                try:
                    callback()
                except Exception:
                    # Resource release is exactly-once at the lease level; a
                    # broken callback must not prevent the ring from draining.
                    pass
            if on_complete is not None:
                # AUD-1521: on_complete 与 release 回调同等防护——它运行在
                # Metal 的 completion 线程上，异常逃逸会打断驱动回调分发
                # 且无人知晓；失败记入 _errors 供 poll 侧发现。
                try:
                    on_complete(submission, error)
                except Exception as exc:
                    logger.warning(
                        "MetalSubmissionScheduler: on_complete for sequence "
                        "%s failed: %s",
                        sequence,
                        exc,
                    )
                    with self._lock:
                        self._errors[sequence] = f"on_complete failed: {exc}"
                        self._prune_errors_locked()

        try:
            command_buffer = self._context.submit(completion=completed, wait=False)
        except Exception:
            with self._lock:
                self._pending.pop(sequence, None)
                self._release_callbacks.pop(sequence, None)
            raise
        if command_buffer is None:
            with self._lock:
                self._pending.pop(sequence, None)
                self._release_callbacks.pop(sequence, None)
            return None
        submission = MetalSubmission(sequence, command_buffer)
        with self._lock:
            # If completion ran synchronously, it already removed the
            # reservation and released the callbacks. Do not resurrect it.
            if sequence in self._pending:
                self._pending[sequence] = submission
        return submission

    def poll(self) -> tuple[int, ...]:
        """Return and clear completion sequence numbers in completion order."""
        with self._lock:
            completed = tuple(self._completed)
            self._completed.clear()
            return completed

    # ── AUD-1523: 有界诊断缓存 ──

    def _bound_completed_locked(self) -> None:
        """``completed`` 回调内调用（持锁）：_completed 超限丢最旧并告警。

        正常帧循环每帧 ``poll()`` 清空，永远不会触达上限；持续超限说明
        调用方停用了 poll（如渲染循环退出但 completion 线程仍在收尾），
        丢最旧保新 + 一次性告警优于无界堆积。
        """
        if len(self._completed) <= self._completed_limit:
            return
        dropped = self._completed.pop(0)
        if not self._warned_unpolled:
            self._warned_unpolled = True
            logger.warning(
                "MetalSubmissionScheduler: >%d completions without "
                "poll(); dropping oldest completed sequence %s — poll() "
                "must be called per frame to consume completions",
                self._completed_limit,
                dropped,
            )

    def _prune_errors_locked(self) -> None:
        """持锁调用：_errors 超限时丢弃最小序号（最旧）条目。"""
        while len(self._errors) > self._errors_limit:
            oldest = min(self._errors)
            del self._errors[oldest]

    def error(self, sequence: int) -> Optional[str]:
        """Completion-reported error text for one sequence (``None`` = ok)."""
        with self._lock:
            return self._errors.get(int(sequence))

    def drain(
        self,
        wait_fn: Optional[Callable[[Any], None]] = None,
        *,
        timeout_s: float = 5.0,
    ) -> None:
        """Drain the ring during exporter shutdown.

        ``submit`` reserves a slot (``None``) inside the lock and only fills
        it in after Metal returns the command buffer; a snapshot taken in
        that window used to dereference ``.command_buffer`` on ``None`` and
        crash the shutdown path (AUD-1520).  Reserved-but-unfilled slots are
        skipped and retried until they resolve (or the ``timeout_s`` budget
        is exhausted); even then their completion handlers close them.

        The default path uses the command buffer's own wait method. Production
        frame loops should never call this; it is a lifecycle boundary only.
        """
        deadline = time.monotonic() + max(0.0, float(timeout_s))
        while True:
            with self._lock:
                pending = tuple(self._pending.values())
            if not pending:
                return
            reserved = 0
            for submission in pending:
                if submission is None:
                    # submit() 预留槽：command buffer 尚未返回，不能解引用。
                    reserved += 1
                    continue
                try:
                    if wait_fn is not None:
                        wait_fn(submission.command_buffer)
                    else:
                        submission.command_buffer.waitUntilCompleted()
                except Exception as exc:
                    # 单个 buffer 等待失败不终止其余 in-flight 的排空。
                    logger.debug(
                        "MetalSubmissionScheduler.drain: waiting on "
                        "sequence %s failed: %s",
                        submission.sequence,
                        exc,
                    )
            if reserved == 0 or time.monotonic() >= deadline:
                if reserved:
                    logger.debug(
                        "MetalSubmissionScheduler.drain: %d submission(s) "
                        "still reserved mid-submit after %.1fs; leaving "
                        "them to their completion handlers",
                        reserved,
                        timeout_s,
                    )
                return
            # 预留槽留给在途的 submit() 一点时间落定后重新快照。
            time.sleep(0.01)
