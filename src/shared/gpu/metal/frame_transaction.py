"""One frame's Metal work as a bounded transaction (Metal migration plan 1.2).

A frame transaction ties together, for a single frame:

- a reservation in the submission ring, taken **before** anything is encoded,
- the input leases the encoded commands read (decoded frames),
- the output lease the commands write (a present texture),
- the completion, which only the thread owning the resources observes in
  :meth:`MetalFrameTransactionPool.poll`.

Release rules:

- Inputs wait for the last GPU read **of this transaction**.  A decoded frame
  read by several transactions is bound to each of them; every transaction
  releases its own reference exactly once when its command buffer completes.
- The output additionally waits for the display consumer: it is released
  once the GPU has finished writing **and** the consumer has called
  :meth:`MetalFrameTransactionPool.note_display_done`.  A failed transaction
  releases its output immediately — there is nothing to display.
- Encoded-but-unsubmitted commands belong to this frame only.  If the commit
  fails or the frame is cancelled after encoding, the whole group is discarded
  through ``MetalComputeContext.discard_pending()``; it is never appended to
  another frame's command buffer.

One transaction encodes at a time: the pool assumes the owner runs
``begin → encode → submit`` to completion before starting the next frame, so
the context's pending command buffer is never shared by two transactions.

Back-pressure follows the existing hold/drop rule: :meth:`begin` returns
``None`` when the ring is full or the device is paused, and the caller holds
the previous frame or drops the request.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from src.shared.gpu.metal.submission_scheduler import MetalSubmissionScheduler

logger = logging.getLogger(__name__)

ReleaseFn = Callable[[], None]
CompleteFn = Callable[["MetalFrameTransaction"], None]


class FrameTransactionState(Enum):
    """Where a frame transaction is in its life."""

    RESERVED = "reserved"        # capacity held, not committed yet
    SUBMITTED = "submitted"      # command buffer committed, GPU may be running
    COMPLETED = "completed"      # GPU finished without error
    FAILED = "failed"            # GPU reported an error, or the commit failed
    ABORTED = "aborted"          # dropped before commit (cancel, nothing to commit)


def _run_once(released: Dict[str, bool], key: str, callbacks: Iterable[ReleaseFn]) -> int:
    """Run ``callbacks`` once per transaction; returns how many were run."""
    if released.get(key):
        return 0
    released[key] = True
    callbacks = tuple(callbacks)
    for callback in callbacks:
        try:
            callback()
        except Exception:
            # Exactly-once at the lease level: one broken callback must not
            # keep the others from returning their resources.
            logger.exception("MetalFrameTransaction: %s release failed", key)
    return len(callbacks)


@dataclass
class MetalFrameTransaction:
    """One frame's GPU work and the leases it holds."""

    sequence: int
    inputs: Tuple[ReleaseFn, ...] = ()
    output: Optional[ReleaseFn] = None
    #: Runs once on the owner thread when the transaction is finished
    #: (:meth:`MetalFrameTransactionPool.poll`), after every lease rule was
    #: applied.  Never called for an aborted transaction.
    on_complete: Optional[CompleteFn] = None
    state: FrameTransactionState = FrameTransactionState.RESERVED
    error: Optional[str] = None
    #: Set by the owner when a completed frame was handed to the presenter.
    published: bool = False
    #: Owner-defined payload travelling with the transaction (frame index,
    #: generation, present lease …).  The pool never looks at it.
    payload: Any = None
    _released: Dict[str, bool] = field(default_factory=dict, repr=False)
    _display_done: bool = field(default=False, repr=False)
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    # -- 查询 -----------------------------------------------------------

    @property
    def in_flight(self) -> bool:
        """Whether the GPU may still be running this transaction."""
        return self.state is FrameTransactionState.SUBMITTED

    @property
    def display_done(self) -> bool:
        with self._lock:
            return self._display_done

    @property
    def inputs_released(self) -> bool:
        return bool(self._released.get("inputs"))

    @property
    def output_released(self) -> bool:
        return bool(self._released.get("output"))

    # -- 状态迁移（均由资源线程在持有 _lock 时调用） ------------------------

    def _mark_submitted(self) -> None:
        if self.state is FrameTransactionState.RESERVED:
            self.state = FrameTransactionState.SUBMITTED

    def _note_display_done_locked(self) -> bool:
        """Record that the presenter is finished; release the output if idle.

        Returns ``True`` when this call was the last outstanding condition.
        """
        if self._display_done:
            return False
        self._display_done = True
        if self.state is not FrameTransactionState.COMPLETED:
            return False
        return self._release_output_locked() > 0 or self.output is None

    def _gpu_finished_locked(self, error: Optional[str]) -> None:
        """The command buffer completed (``error`` = GPU failure)."""
        if self.state is not FrameTransactionState.SUBMITTED:
            return
        self.error = error
        self.state = (
            FrameTransactionState.FAILED if error is not None
            else FrameTransactionState.COMPLETED
        )
        self._release_inputs_locked()
        if self.output is None:
            # Nothing was written for a presenter: the frame is fully idle.
            self._released["output"] = True
        elif error is not None or self._display_done:
            # A failed frame has nothing to display; a frame the presenter
            # already reported has no remaining reader.
            self._release_output_locked()

    def _settle_uncommitted_locked(self, error: Optional[str]) -> None:
        """Settle a transaction whose commands were never committed."""
        if self.state is not FrameTransactionState.RESERVED:
            return
        self.error = error
        self.state = (
            FrameTransactionState.ABORTED if error is None
            else FrameTransactionState.FAILED
        )
        self._release_inputs_locked()
        self._release_output_locked()

    def _release_inputs_locked(self) -> int:
        return _run_once(self._released, "inputs", self.inputs)

    def _release_output_locked(self) -> int:
        if self.output is None:
            self._released["output"] = True
            return 0
        return _run_once(self._released, "output", (self.output,))


class MetalFrameTransactionPool:
    """Bounded pool of frame transactions for one Metal context.

    Every method runs on the thread that owns the context and its resources.
    The driver's completion path only queues; nothing of this pool executes
    on the driver thread.
    """

    def __init__(
        self,
        context: Any,
        *,
        max_in_flight: int = 4,
        completion_timeout_s: Optional[float] = None,
        clock: Optional[Callable[[], float]] = None,
    ) -> None:
        """``clock`` is the monotonic clock used to age submissions."""
        self._context = context
        self._clock = clock or time.monotonic
        self.scheduler = MetalSubmissionScheduler(
            context, max_in_flight=max_in_flight,
            completion_timeout_s=completion_timeout_s, clock=self._clock,
        )
        self._lock = threading.RLock()
        self._transactions: Dict[int, MetalFrameTransaction] = {}
        # Completed outputs the presenter is still reading.  Bounded by the
        # ring size plus whatever the presenter holds, never by time.
        self._awaiting_display: Dict[int, MetalFrameTransaction] = {}
        self.last_pause_reason: Optional[str] = None

    # -- 状态 -----------------------------------------------------------

    @property
    def max_in_flight(self) -> int:
        return self.scheduler.max_in_flight

    @property
    def in_flight(self) -> int:
        """Transactions reserved or submitted and not yet completed."""
        with self._lock:
            return len(self._transactions)

    @property
    def paused(self) -> bool:
        """New frames are refused (a completion timed out or the device failed)."""
        return self.scheduler.paused

    @property
    def pause_reason(self) -> Optional[str]:
        return self.scheduler.pause_reason

    @property
    def awaiting_display(self) -> int:
        """Completed outputs still read by the presenter."""
        with self._lock:
            return len(self._awaiting_display)

    # -- 事务 -----------------------------------------------------------

    def begin(
        self,
        *,
        inputs: Iterable[ReleaseFn] = (),
        output: Optional[ReleaseFn] = None,
        on_complete: Optional[CompleteFn] = None,
        payload: Any = None,
    ) -> Optional[MetalFrameTransaction]:
        """Reserve ring capacity for one frame before encoding.

        ``None`` means the ring is full or the device is paused: the caller
        holds the previous frame or drops the request.  Nothing has been
        encoded or committed, so there is nothing to undo.
        """
        sequence = self.scheduler.try_reserve()
        if sequence is None:
            return None
        transaction = MetalFrameTransaction(
            sequence=sequence, inputs=tuple(inputs), output=output,
            on_complete=on_complete, payload=payload,
        )
        with self._lock:
            self._transactions[sequence] = transaction
        return transaction

    def submit(self, transaction: MetalFrameTransaction) -> bool:
        """Commit the context's encoded command buffer for ``transaction``.

        Call after encoding.  Returns ``False`` when nothing was committed
        and every lease is released exactly once.  A commit failure discards
        the encoded group as a whole; "nothing encoded" has nothing to
        discard.  A driver callback is never involved for an uncommitted
        buffer.  A buffer that *was* committed is never reported here: its
        result, including a driver-detected error, arrives through
        :meth:`poll`.
        """
        with self._lock:
            if self._transactions.get(transaction.sequence) is not transaction:
                return False
        try:
            submission = self.scheduler.submit_reserved(transaction.sequence)
        except Exception as exc:
            # Raised before commit: nothing is on the GPU.  Drop the encoded
            # group so it cannot ride along with the next frame.
            self._settle_uncommitted(
                transaction, error=f"submit_failed:{exc}", discard=True)
            return False
        if submission is None:
            # The context had no encoded work for this frame.
            self._settle_uncommitted(transaction, error=None, discard=False)
            return False
        with transaction._lock:
            transaction._mark_submitted()
        return True

    def abort(self, transaction: MetalFrameTransaction, reason: str = "cancelled") -> bool:
        """Drop a reserved transaction whose commands were never committed.

        The encoded group is discarded as a whole.  Returns ``False`` for a
        transaction that is unknown or already submitted.
        """
        with self._lock:
            if self._transactions.get(transaction.sequence) is not transaction:
                return False
            if transaction.state is not FrameTransactionState.RESERVED:
                return False
            del self._transactions[transaction.sequence]
        self._discard_encoded_group()
        self.scheduler.cancel_reservation(transaction.sequence)
        self._release_quietly(transaction, error=None)
        logger.debug(
            "MetalFrameTransactionPool: sequence %s aborted (%s)",
            transaction.sequence, reason,
        )
        return True

    def _settle_uncommitted(
        self,
        transaction: MetalFrameTransaction,
        *,
        error: Optional[str],
        discard: bool,
    ) -> None:
        """Owner thread: a frame that never reached the GPU gives everything back."""
        if discard:
            self._discard_encoded_group()
        self.scheduler.cancel_reservation(transaction.sequence)
        with self._lock:
            self._transactions.pop(transaction.sequence, None)
        self._release_quietly(transaction, error=error)

    @staticmethod
    def _release_quietly(transaction: MetalFrameTransaction, *, error: Optional[str]) -> None:
        with transaction._lock:
            transaction._settle_uncommitted_locked(error)

    # -- 完成（资源线程） -------------------------------------------------

    def poll(self, *, now: Optional[float] = None) -> List[MetalFrameTransaction]:
        """Finish the transactions whose command buffers completed.

        Releases each one's inputs; an output is released as well when the
        display consumer has already reported, or when the GPU failed.  The
        transaction's ``on_complete`` runs here, after its final state is set.
        A completion that did not arrive in time pauses new frames for this
        device — the last presented frame stays on screen — and the pause is
        lifted automatically once everything in flight has come back.
        """
        sequences = self.scheduler.poll(now=now)
        finished: List[MetalFrameTransaction] = []
        for sequence in sequences:
            with self._lock:
                transaction = self._transactions.pop(sequence, None)
            if transaction is None:
                continue
            with transaction._lock:
                transaction._gpu_finished_locked(self.scheduler.error(sequence))
            if (transaction.state is FrameTransactionState.COMPLETED
                    and transaction.output is not None
                    and not transaction.output_released):
                with self._lock:
                    self._awaiting_display[sequence] = transaction
            finished.append(transaction)
            if transaction.on_complete is not None:
                try:
                    transaction.on_complete(transaction)
                except Exception:
                    logger.exception(
                        "MetalFrameTransactionPool: on_complete failed for sequence %s",
                        sequence,
                    )
        self._check_completion_timeout(now=now)
        return finished

    def note_display_done(self, transaction: MetalFrameTransaction) -> bool:
        """The presenter finished reading ``transaction``'s output.

        Returns ``True`` when the output was released by this call.  May be
        called before the GPU finished (the output is then released at
        completion) and more than once (extra calls do nothing).
        """
        with transaction._lock:
            released_now = transaction._note_display_done_locked()
        if released_now:
            with self._lock:
                self._awaiting_display.pop(transaction.sequence, None)
        return released_now

    def _check_completion_timeout(self, *, now: Optional[float]) -> None:
        timed_out = self.scheduler.pending_timeouts(now=now)
        if timed_out:
            reason = f"completion_timeout:sequences={list(timed_out)}"
            if self.scheduler.pause_reason != reason:
                self.scheduler.pause(reason)
                self.last_pause_reason = reason
                logger.warning(
                    "MetalFrameTransactionPool: %s; new frames are refused until "
                    "the GPU drains (the last frame is kept on screen)",
                    reason,
                )
            return
        if self.scheduler.paused and self.scheduler.resume_if_idle():
            logger.info(
                "MetalFrameTransactionPool: GPU drained after %s; resuming",
                self.last_pause_reason,
            )

    # -- 生命周期 ---------------------------------------------------------

    def drain(self, *, timeout_s: float = 5.0) -> bool:
        """Shutdown boundary: wait for in-flight work and run pending releases.

        ``True`` means nothing is running on the GPU and every input has been
        released, so the owner may destroy the context's resources.  ``False``
        means a command buffer may still reference them: keep the resources,
        report the reason and retry.  Outputs still held by the presenter are
        reported through :attr:`awaiting_display` and are **not** released
        here — the presenter owns that decision.
        """
        deadline = self._clock() + max(0.0, float(timeout_s))
        while True:
            remaining_s = max(0.0, deadline - self._clock())
            # Wait for the GPU without consuming completions: this pool must
            # be the one that finishes each transaction and releases leases.
            idle = self.scheduler.wait_idle(timeout_s=remaining_s)
            self.poll()
            with self._lock:
                remaining = len(self._transactions)
            if idle and remaining == 0:
                return True
            if self._clock() >= deadline:
                logger.warning(
                    "MetalFrameTransactionPool: %d transaction(s) still in flight after "
                    "%.1fs; resources must be kept",
                    remaining, timeout_s,
                )
                return False

    def _discard_encoded_group(self) -> bool:
        discard = getattr(self._context, "discard_pending", None)
        if not callable(discard):
            return False
        try:
            return bool(discard())
        except Exception:
            logger.exception("MetalFrameTransactionPool: discarding encoded group failed")
            return False

    # -- 诊断 -------------------------------------------------------------

    def stats(self) -> dict:
        """Snapshot for telemetry and acceptance checks."""
        with self._lock:
            states: Dict[str, int] = {}
            for transaction in self._transactions.values():
                states[transaction.state.value] = states.get(transaction.state.value, 0) + 1
            return {
                "max_in_flight": self.scheduler.max_in_flight,
                "in_flight": len(self._transactions),
                "states": states,
                "awaiting_display": len(self._awaiting_display),
                "paused": self.scheduler.paused,
                "pause_reason": self.scheduler.pause_reason,
                "ready_unpolled": self.scheduler.ready_unpolled,
            }


