"""Bounded asynchronous Metal submission scheduler.

The timeline renderer used to call ``finish()`` for every frame.  This module
provides the small ownership layer required by M6: a fixed number of in-flight
submissions, completion-driven release, and explicit back-pressure.  It does
not hide a wait or silently grow a queue.

Threading contract (Metal migration plan task 1.2):

- Metal's completion handler runs on the driver's own thread, never on the
  submitter's.  It only moves the finished command buffer onto an internal
  queue.  It must not release resources or run user code: ``release_callbacks``
  and ``on_complete`` run later on the thread that owns the resources, inside
  :meth:`MetalSubmissionScheduler.poll`.  Every frame loop therefore has to
  call ``poll()``; a loop that stops polling stops reclaiming (bounded, never
  unbounded growth) — see the warning emitted by ``_bound_completed_locked``.
- Capacity can be reserved *before* encoding (:meth:`try_reserve` /
  :meth:`submit_reserved`).  Encoding without a reservation and finding the
  ring full leaves encoded-but-unsubmitted commands in the context; those
  belong to the current frame only and must be discarded as a group with
  ``MetalComputeContext.discard_pending()`` — never carried into another
  frame's submission.
- A completion that does not arrive within ``completion_timeout_s`` is
  reported by :meth:`pending_timeouts`.  The owner pauses new submissions for
  that device (:meth:`pause`), keeps the last presented frame and destroys
  resources only after :meth:`drain` confirms the GPU is idle.
- ``drain`` is a shutdown-only lifecycle boundary — production frame loops
  never wait on it.
"""

from __future__ import annotations

import inspect
import logging
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable, Deque, Dict, Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MetalSubmission:
    """A submitted command buffer and its monotonic scheduler sequence."""

    sequence: int
    command_buffer: Any


@dataclass(frozen=True)
class _Completion:
    """One finished submission waiting for the owner thread."""

    sequence: int
    submission: MetalSubmission
    error: Optional[str]
    release_callbacks: Tuple[Callable[[], None], ...]
    on_complete: Optional[Callable[[MetalSubmission, Any], None]]


class MetalSubmissionScheduler:
    """A bounded completion-handler scheduler for one Metal context.

    The context must already hold the encoded command buffer when
    :meth:`submit` or :meth:`submit_reserved` is called.  If the ring is full
    (or the scheduler is paused) reservation fails without committing work;
    the caller owns back-pressure.
    """

    def __init__(
        self,
        context: Any,
        max_in_flight: int = 4,
        *,
        completion_timeout_s: Optional[float] = None,
        clock: Optional[Callable[[], float]] = None,
    ) -> None:
        """Bind the scheduler to one Metal compute context.

        Args:
            context: a `MetalComputeContext`-like object whose
                ``submit(completion=..., wait=False)`` commits its current
                command buffer.
            max_in_flight: ring capacity (>= 1). When reached, reservation
                returns ``None`` instead of queuing — the caller owns
                back-pressure.
            completion_timeout_s: age at which an unfinished submission is
                reported by :meth:`pending_timeouts` (``None`` = never).
            clock: monotonic clock used to age submissions (default
                ``time.monotonic``); tests inject a controllable one.
        """
        if int(max_in_flight) < 1:
            raise ValueError("max_in_flight must be >= 1")
        self._context = context
        self._max = int(max_in_flight)
        self._completion_timeout_s = (
            None if completion_timeout_s is None else float(completion_timeout_s)
        )
        if (self._completion_timeout_s is not None
                and self._completion_timeout_s <= 0.0):
            raise ValueError("completion_timeout_s must be > 0 when given")
        self._clock = clock or time.monotonic
        # A committed command buffer always reports through its completion
        # handler, even when the driver already knows it failed.  Contexts
        # that accept ``raise_after_commit=False`` let that path run; an
        # exception after commit would hide whether the buffer was sent.
        self._context_defers_commit_errors = _accepts_keyword(
            getattr(context, "submit", None), "raise_after_commit")
        self._lock = threading.RLock()
        self._next_sequence = 0
        self._pending: Dict[int, Optional[MetalSubmission]] = {}
        self._submitted_at: Dict[int, float] = {}
        self._ready: Deque[_Completion] = deque()
        self._completed: list[int] = []
        self._errors: Dict[int, str] = {}
        self._release_callbacks: Dict[int, Tuple[Callable[[], None], ...]] = {}
        self._pause_reason: Optional[str] = None
        # AUD-1523: _completed/_errors 只靠调用方自觉 poll()/error() 消费，
        # 长期不 poll 会无界增长。这里给出有界上限（诊断数据，允许溢出时
        # 丢最旧），溢出只告警一次避免刷屏。
        self._completed_limit = max(64, self._max * 16)
        self._errors_limit = self._completed_limit
        self._warned_unpolled = False

    # ------------------------------------------------------------------
    # 状态
    # ------------------------------------------------------------------

    @property
    def max_in_flight(self) -> int:
        """Ring capacity (constructor argument, fixed for lifetime)."""
        return self._max

    @property
    def in_flight(self) -> int:
        """Currently reserved-but-not-completed submission count."""
        with self._lock:
            return len(self._pending)

    @property
    def ready_unpolled(self) -> int:
        """Completions queued for the owner thread that ``poll()`` has not handled."""
        with self._lock:
            return len(self._ready)

    @property
    def paused(self) -> bool:
        """Whether new submissions are refused for this device."""
        with self._lock:
            return self._pause_reason is not None

    @property
    def pause_reason(self) -> Optional[str]:
        """Why submissions are paused (``None`` = running)."""
        with self._lock:
            return self._pause_reason

    def pause(self, reason: str) -> None:
        """Refuse new submissions (a completion timed out or the device failed).

        In-flight work keeps draining through :meth:`poll`; the owner keeps
        the last presented frame and clears the pause with :meth:`resume`.
        """
        with self._lock:
            changed = self._pause_reason != str(reason)
            self._pause_reason = str(reason)
        if changed:
            logger.warning(
                "MetalSubmissionScheduler: submissions paused (%s)", reason,
            )

    def resume(self) -> None:
        """Accept new submissions again."""
        with self._lock:
            self._pause_reason = None

    def resume_if_idle(self) -> bool:
        """Lift a pause once nothing is in flight; ``True`` when resumed.

        Used after a completion timeout: new submissions stay refused until
        every command buffer sent before the pause has come back, so a slow
        or stuck GPU cannot have work re-queued on top of it.
        """
        with self._lock:
            if self._pause_reason is None:
                return False
            if self._pending:
                return False
            self._pause_reason = None
        return True

    # ------------------------------------------------------------------
    # 预留 / 提交
    # ------------------------------------------------------------------

    def try_reserve(self) -> Optional[int]:
        """Reserve ring capacity before encoding; ``None`` when full or paused."""
        with self._lock:
            if self._pause_reason is not None or len(self._pending) >= self._max:
                return None
            sequence = self._next_sequence
            self._next_sequence += 1
            # A test double, and theoretically a very fast driver, may invoke
            # the completion handler synchronously from commit().
            self._pending[sequence] = None
            self._release_callbacks[sequence] = ()
            return sequence

    def cancel_reservation(self, sequence: int) -> bool:
        """Give back a reservation whose commands were never submitted."""
        with self._lock:
            if self._pending.get(sequence, _MISSING) is not None:
                return False
            existed = self._pending.pop(sequence, _MISSING) is not _MISSING
            self._release_callbacks.pop(sequence, None)
        return existed

    def submit(
        self,
        *,
        release_callbacks: Tuple[Callable[[], None], ...] = (),
        on_complete: Optional[Callable[[MetalSubmission, Any], None]] = None,
    ) -> Optional[MetalSubmission]:
        """Commit the context's pending command buffer if capacity exists.

        The context must already hold the encoded commands.  ``None`` means
        nothing was committed: the ring is full or paused, and the encoded
        commands are still in the context — discard them as a group with
        ``MetalComputeContext.discard_pending()`` or retry within the same
        frame.
        """
        sequence = self.try_reserve()
        if sequence is None:
            return None
        return self.submit_reserved(
            sequence, release_callbacks=release_callbacks, on_complete=on_complete,
        )

    def submit_reserved(
        self,
        sequence: int,
        *,
        release_callbacks: Tuple[Callable[[], None], ...] = (),
        on_complete: Optional[Callable[[MetalSubmission, Any], None]] = None,
    ) -> Optional[MetalSubmission]:
        """Commit the context's pending command buffer under a reservation.

        ``None`` means nothing was committed (reservation unknown, or the
        context had no encoded work); the reservation is released either way.
        """
        callbacks = tuple(release_callbacks)
        with self._lock:
            if sequence not in self._pending:
                return None
            self._release_callbacks[sequence] = callbacks

        def completed(command_buffer: Any) -> None:
            # Driver thread: record the result and return.  No resource
            # release and no user code here.
            error = None
            try:
                error = command_buffer.error()
            except Exception as exc:  # pragma: no cover - defensive PyObjC edge
                error = exc
            with self._lock:
                pending = self._pending.pop(sequence, _MISSING)
                if pending is _MISSING:
                    return
                self._submitted_at.pop(sequence, None)
                submission = pending or MetalSubmission(sequence, command_buffer)
                text: Optional[str] = None
                if error is not None:
                    text = str(error)
                    self._errors[sequence] = text
                    self._prune_errors_locked()
                self._ready.append(_Completion(
                    sequence=sequence,
                    submission=submission,
                    error=text,
                    release_callbacks=self._release_callbacks.pop(sequence, ()),
                    on_complete=on_complete,
                ))
                self._bound_completed_locked()

        with self._lock:
            if sequence in self._pending:
                self._submitted_at[sequence] = self._clock()
        submit_kwargs: Dict[str, Any] = {"completion": completed, "wait": False}
        if self._context_defers_commit_errors:
            submit_kwargs["raise_after_commit"] = False
        try:
            command_buffer = self._context.submit(**submit_kwargs)
        except Exception:
            self._forget(sequence)
            raise
        if command_buffer is None:
            self._forget(sequence)
            return None
        submission = MetalSubmission(sequence, command_buffer)
        with self._lock:
            # If completion ran synchronously, it already removed the
            # reservation and queued the callbacks. Do not resurrect it.
            if sequence in self._pending:
                self._pending[sequence] = submission
        return submission

    def _forget(self, sequence: int) -> None:
        """Drop a reservation that never turned into a submission."""
        with self._lock:
            self._pending.pop(sequence, None)
            self._submitted_at.pop(sequence, None)
            self._release_callbacks.pop(sequence, None)

    # ------------------------------------------------------------------
    # 完成（资源线程）
    # ------------------------------------------------------------------

    def poll(self, *, now: Optional[float] = None) -> Tuple[int, ...]:
        """Reclaim finished submissions on the owner thread.

        Runs the release callbacks and ``on_complete`` of every submission
        completed since the last call, then returns their sequences in
        completion order.  Call once per frame loop iteration.
        """
        with self._lock:
            ready = list(self._ready)
            self._ready.clear()
            self._completed.extend(item.sequence for item in ready)
            self._bound_completed_locked()
            completed = tuple(self._completed)
            self._completed.clear()
        for item in ready:
            for callback in item.release_callbacks:
                try:
                    callback()
                except Exception:
                    # Resource release is exactly-once at the lease level; a
                    # broken callback must not prevent the ring from draining.
                    pass
            if item.on_complete is not None:
                # AUD-1521: 用户回调异常不能打断同批其余完成项的处理；
                # 失败记入 _errors 供 error() 侧发现。
                try:
                    item.on_complete(item.submission, item.error)
                except Exception as exc:
                    logger.warning(
                        "MetalSubmissionScheduler: on_complete for sequence "
                        "%s failed: %s",
                        item.sequence,
                        exc,
                    )
                    with self._lock:
                        self._errors[item.sequence] = f"on_complete failed: {exc}"
                        self._prune_errors_locked()
        return completed

    def pending_timeouts(self, *, now: Optional[float] = None) -> Tuple[int, ...]:
        """Sequences still unfinished after ``completion_timeout_s``."""
        timeout = self._completion_timeout_s
        if timeout is None:
            return ()
        current = self._clock() if now is None else float(now)
        with self._lock:
            return tuple(
                sequence for sequence, submitted in sorted(self._submitted_at.items())
                if current - submitted > timeout
            )

    # ── AUD-1523: 有界诊断缓存 ──

    def _bound_completed_locked(self) -> None:
        """``completed`` 回调 / ``poll`` 内调用（持锁）：完成序号超限丢最旧。

        正常帧循环每帧 ``poll()`` 清空，永远不会触达上限；持续超限说明
        调用方停用了 poll（如渲染循环退出但 completion 线程仍在收尾）。
        此时不仅是诊断数据：待执行的释放回调也随之积压在 ``_ready``，
        同样按最旧优先丢弃并一次性告警——宁可延迟回收已完成的资源，也
        不让队列无界增长。
        """
        if len(self._completed) > self._completed_limit:
            del self._completed[: len(self._completed) - self._completed_limit]
        while len(self._ready) > self._completed_limit:
            dropped = self._ready.popleft()
            if not self._warned_unpolled:
                self._warned_unpolled = True
                logger.warning(
                    "MetalSubmissionScheduler: >%d completions without "
                    "poll(); dropping oldest completed sequence %s — poll() "
                    "must be called per frame to release resources",
                    self._completed_limit,
                    dropped.sequence,
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

    def wait_idle(
        self,
        wait_fn: Optional[Callable[[Any], None]] = None,
        *,
        timeout_s: float = 5.0,
    ) -> bool:
        """Wait until every submitted command buffer completed; run nothing.

        ``submit`` reserves a slot (``None``) inside the lock and only fills
        it in after Metal returns the command buffer; a snapshot taken in
        that window used to dereference ``.command_buffer`` on ``None`` and
        crash the shutdown path (AUD-1520).  Reserved-but-unfilled slots are
        skipped and retried until they resolve or the budget is spent.

        Returns ``True`` when nothing is in flight.  ``False`` means work may
        still be on the GPU (a wait failed, a slot never resolved, or the
        budget ran out): the caller must not destroy resources those commands
        touch.  ``wait_fn`` has the contract of ``waitUntilCompleted`` — it
        blocks until the buffer finished.  A buffer that was waited on
        successfully counts as completed even if the driver has not delivered
        its handler yet, so a shutdown is never held up by a late callback;
        the queued completion stays available for :meth:`poll`.  Production
        frame loops should never call this; it is a lifecycle boundary only.
        """
        deadline = time.monotonic() + max(0.0, float(timeout_s))
        waited: set = set()
        while True:
            with self._lock:
                pending = tuple(
                    submission for submission in self._pending.values()
                    if submission is not None and submission.sequence not in waited
                )
                reserved = len(self._pending) - len(
                    [s for s in self._pending.values() if s is not None])
            if not pending and not reserved:
                return True
            for submission in pending:
                try:
                    if wait_fn is not None:
                        wait_fn(submission.command_buffer)
                    else:
                        submission.command_buffer.waitUntilCompleted()
                except Exception as exc:
                    # 单个 buffer 等待失败不终止其余 in-flight 的排空。
                    logger.debug(
                        "MetalSubmissionScheduler.wait_idle: waiting on "
                        "sequence %s failed: %s",
                        submission.sequence,
                        exc,
                    )
                    continue
                waited.add(submission.sequence)
                self._confirm_waited(submission)
            with self._lock:
                remaining = len(self._pending)
            if remaining == 0:
                return True
            if time.monotonic() >= deadline:
                if reserved:
                    logger.debug(
                        "MetalSubmissionScheduler.wait_idle: %d submission(s) "
                        "still reserved mid-submit after %.1fs; leaving "
                        "them to their completion handlers",
                        reserved,
                        timeout_s,
                    )
                return False
            # Let completion handlers land and reserved slots resolve.
            time.sleep(0.01)

    def _confirm_waited(self, submission: MetalSubmission) -> None:
        """Treat a waited-on buffer as completed if its handler has not run.

        ``waitUntilCompleted`` returning means the GPU is done with the
        buffer.  Its completion handler normally has run by then; when the
        driver is still about to deliver it, queue the result here so the
        owner reclaims the leases in :meth:`poll`.  Whoever comes second finds
        the reservation gone and does nothing.
        """
        sequence = submission.sequence
        with self._lock:
            if self._pending.pop(sequence, _MISSING) is _MISSING:
                return
            self._submitted_at.pop(sequence, None)
            error: Optional[str] = None
            try:
                reported = submission.command_buffer.error()
            except Exception as exc:  # pragma: no cover - defensive PyObjC edge
                reported = exc
            if reported is not None:
                error = str(reported)
                self._errors[sequence] = error
                self._prune_errors_locked()
            self._ready.append(_Completion(
                sequence=sequence,
                submission=submission,
                error=error,
                release_callbacks=self._release_callbacks.pop(sequence, ()),
                on_complete=None,
            ))
            self._bound_completed_locked()

    def drain(
        self,
        wait_fn: Optional[Callable[[Any], None]] = None,
        *,
        timeout_s: float = 5.0,
    ) -> bool:
        """Shutdown boundary: wait for in-flight work, then run pending releases.

        Returns ``True`` when nothing is in flight and every completion has
        been handed to the owner thread.  ``False`` means work may still be
        running on the GPU: the caller must not destroy the resources those
        commands touch.
        """
        idle = self.wait_idle(wait_fn=wait_fn, timeout_s=timeout_s)
        self.poll()
        if not idle:
            with self._lock:
                in_flight = len(self._pending)
            logger.warning(
                "MetalSubmissionScheduler.drain: %d submission(s) still in "
                "flight after %.1fs; GPU work may still reference caller "
                "resources",
                in_flight,
                timeout_s,
            )
        return idle


def _accepts_keyword(func: Optional[Callable[..., Any]], name: str) -> bool:
    """Whether ``func`` declares a keyword parameter ``name``."""
    if func is None:
        return False
    try:
        return name in inspect.signature(func).parameters
    except (TypeError, ValueError):
        return False


_MISSING = object()
