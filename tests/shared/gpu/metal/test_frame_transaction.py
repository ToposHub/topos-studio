"""Frame transaction pool (Metal migration plan task 1.2).

Injected failures: ring full, commit failure, GPU failure, duplicate driver
callbacks, completion timeout and out-of-order completion.  Every input and
every output is released exactly once, a full ring keeps the hold/drop rule,
and encoded-but-unsubmitted commands are discarded as one group.
"""

from __future__ import annotations

import random
import threading
from typing import Any, Callable, Dict, List, Optional, Tuple

import pytest

from src.shared.gpu.metal.frame_transaction import (
    FrameTransactionState,
    MetalFrameTransaction,
    MetalFrameTransactionPool,
)


class _Lease:
    """A lease double that counts how often it was released."""

    def __init__(self, name: str, log: Optional[List[str]] = None) -> None:
        self.name = name
        self.count = 0
        self.log = log

    def release(self) -> None:
        self.count += 1
        if self.log is not None:
            self.log.append(f"{self.name}:{threading.get_ident()}")

    def __call__(self) -> None:
        self.release()


class _Clock:
    """Controllable monotonic clock."""

    def __init__(self, start: float = 100.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _CommandBuffer:
    def __init__(self) -> None:
        self.error_value: Any = None
        self.waited = 0

    def error(self) -> Any:
        return self.error_value

    def waitUntilCompleted(self) -> None:
        self.waited += 1


class _Context:
    """MetalComputeContext double with an encoded-work flag."""

    def __init__(self) -> None:
        self.handlers: List[Callable[[_CommandBuffer], None]] = []
        self.buffers: List[_CommandBuffer] = []
        self.discarded = 0
        self.commits = 0
        self.fail_submit: Optional[Exception] = None
        self.encoded = False

    def mark_encoded(self) -> None:
        self.encoded = True

    def discard_pending(self) -> bool:
        had = self.encoded
        self.encoded = False
        self.discarded += 1
        return had

    def submit(self, *, completion: Callable[[_CommandBuffer], None], wait: bool) -> Any:
        assert wait is False
        if self.fail_submit is not None:
            raise self.fail_submit
        if not self.encoded:
            return None
        self.encoded = False
        self.commits += 1
        buffer = _CommandBuffer()
        self.handlers.append(completion)
        self.buffers.append(buffer)
        return buffer

    def complete(self, index: int, error: Any = None) -> None:
        self.buffers[index].error_value = error
        self.handlers[index](self.buffers[index])


def _frame(
    pool: MetalFrameTransactionPool, log: Optional[List[str]] = None,
    **kwargs: Any,
) -> Tuple[MetalFrameTransaction, _Lease, _Lease]:
    source = _Lease("input", log)
    present = _Lease("output", log)
    transaction = pool.begin(inputs=(source,), output=present, **kwargs)
    assert transaction is not None
    return transaction, source, present


class TestHappyPath:
    def test_output_waits_for_gpu_and_display(self) -> None:
        context = _Context()
        pool = MetalFrameTransactionPool(context, max_in_flight=2)
        transaction, source, present = _frame(pool)
        context.mark_encoded()
        assert pool.submit(transaction) is True
        assert transaction.in_flight

        context.complete(0)
        assert pool.poll() == [transaction]
        assert transaction.state is FrameTransactionState.COMPLETED
        # The GPU is done reading the decoded frame …
        assert source.count == 1
        # … but the presenter may still be reading the output texture.
        assert present.count == 0
        assert pool.awaiting_display == 1

        assert pool.note_display_done(transaction) is True
        assert present.count == 1
        assert pool.awaiting_display == 0

    def test_display_done_before_gpu_completion(self) -> None:
        context = _Context()
        pool = MetalFrameTransactionPool(context, max_in_flight=2)
        transaction, source, present = _frame(pool)
        context.mark_encoded()
        pool.submit(transaction)
        assert pool.note_display_done(transaction) is False
        assert present.count == 0
        context.complete(0)
        pool.poll()
        assert source.count == 1 and present.count == 1
        assert pool.awaiting_display == 0

    def test_on_complete_sees_the_final_state(self) -> None:
        context = _Context()
        pool = MetalFrameTransactionPool(context, max_in_flight=2)
        seen: List[Tuple[FrameTransactionState, bool, bool]] = []
        transaction, source, present = _frame(pool)
        transaction.on_complete = lambda tx: seen.append(
            (tx.state, tx.inputs_released, tx.output_released))
        context.mark_encoded()
        pool.submit(transaction)
        context.complete(0)
        assert seen == []
        pool.poll()
        assert seen == [(FrameTransactionState.COMPLETED, True, False)]

    def test_repeated_reports_change_nothing(self) -> None:
        context = _Context()
        pool = MetalFrameTransactionPool(context, max_in_flight=2)
        transaction, source, present = _frame(pool)
        context.mark_encoded()
        pool.submit(transaction)
        context.complete(0)
        # A driver may deliver a completion more than once.
        context.complete(0)
        pool.poll()
        pool.poll()
        assert pool.note_display_done(transaction) is True
        assert pool.note_display_done(transaction) is False
        assert (source.count, present.count) == (1, 1)

    def test_payload_travels_with_the_transaction(self) -> None:
        context = _Context()
        pool = MetalFrameTransactionPool(context, max_in_flight=1)
        transaction, _source, _present = _frame(pool, payload={"frame": 42})
        assert transaction.payload == {"frame": 42}


class TestFullRing:
    def test_begin_refuses_without_encoding_anything(self) -> None:
        context = _Context()
        pool = MetalFrameTransactionPool(context, max_in_flight=1)
        assert pool.begin() is not None
        assert pool.in_flight == 1
        # Hold the previous frame / drop the request: nothing was encoded.
        assert pool.begin() is None
        assert context.encoded is False
        assert context.commits == 0

    def test_capacity_returns_after_completion(self) -> None:
        context = _Context()
        pool = MetalFrameTransactionPool(context, max_in_flight=1)
        transaction, _source, present = _frame(pool)
        context.mark_encoded()
        pool.submit(transaction)
        context.complete(0)
        pool.poll()
        # The slot is free for the next frame even while the presenter
        # still reads this frame's output: that is the ring's bound.
        assert pool.awaiting_display == 1
        assert pool.begin() is not None
        assert present.count == 0


class TestInjectedFailures:
    def test_gpu_failure_releases_input_and_output_once(self) -> None:
        context = _Context()
        pool = MetalFrameTransactionPool(context, max_in_flight=2)
        log: List[str] = []
        transaction, source, present = _frame(pool, log)
        context.mark_encoded()
        pool.submit(transaction)
        context.complete(0, error="GPU hang")

        (owner,) = [threading.get_ident()]
        assert pool.poll() == [transaction]
        assert transaction.state is FrameTransactionState.FAILED
        assert transaction.error == "GPU hang"
        assert (source.count, present.count) == (1, 1)
        assert pool.awaiting_display == 0  # nothing was handed to the presenter
        assert {entry.split(":")[1] for entry in log} == {str(owner)}

    def test_commit_failure_discards_the_group_and_releases_once(self) -> None:
        context = _Context()
        pool = MetalFrameTransactionPool(context, max_in_flight=2)
        transaction, source, present = _frame(pool)
        context.mark_encoded()
        context.fail_submit = RuntimeError("commit refused")

        assert pool.submit(transaction) is False
        assert transaction.state is FrameTransactionState.FAILED
        assert "submit_failed" in (transaction.error or "")
        # Encoded commands were dropped as one group, never left behind.
        assert context.encoded is False
        assert context.discarded == 1
        assert (source.count, present.count) == (1, 1)
        assert pool.in_flight == 0

    def test_committed_frame_failing_at_commit_is_not_settled_early(self) -> None:
        class _CommitErrorContext(_Context):
            def submit(self, *, completion, wait, raise_after_commit=True):
                buffer = super().submit(completion=completion, wait=wait)
                buffer.error_value = "fault at commit"
                if raise_after_commit:
                    raise RuntimeError("failed after commit")
                return buffer

        context = _CommitErrorContext()
        pool = MetalFrameTransactionPool(context, max_in_flight=2)
        transaction, source, present = _frame(pool)
        context.mark_encoded()
        # Committed: the GPU may be running it, so nothing is released yet.
        assert pool.submit(transaction) is True
        assert transaction.in_flight
        assert (source.count, present.count) == (0, 0)
        context.handlers[0](context.buffers[0])  # the driver reports the error
        assert pool.poll() == [transaction]
        assert transaction.state is FrameTransactionState.FAILED
        assert transaction.error == "fault at commit"
        assert (source.count, present.count) == (1, 1)

    def test_nothing_encoded_settles_the_frame(self) -> None:
        context = _Context()
        pool = MetalFrameTransactionPool(context, max_in_flight=2)
        transaction, source, present = _frame(pool)
        assert pool.submit(transaction) is False
        assert transaction.state is FrameTransactionState.ABORTED
        assert (source.count, present.count) == (1, 1)
        assert context.discarded == 0
        assert pool.in_flight == 0

    def test_abort_after_encoding_drops_only_this_frame(self) -> None:
        context = _Context()
        pool = MetalFrameTransactionPool(context, max_in_flight=2)
        transaction, source, present = _frame(pool)
        context.mark_encoded()
        assert pool.abort(transaction, reason="seek") is True
        assert transaction.state is FrameTransactionState.ABORTED
        assert context.encoded is False
        assert (source.count, present.count) == (1, 1)
        assert pool.in_flight == 0
        # Aborting twice, or after submission, is refused.
        assert pool.abort(transaction) is False

    def test_broken_input_release_does_not_keep_the_output(self) -> None:
        context = _Context()
        pool = MetalFrameTransactionPool(context, max_in_flight=2)
        present = _Lease("output")

        def explode() -> None:
            raise RuntimeError("input release failed")

        transaction = pool.begin(inputs=(explode,), output=present)
        assert transaction is not None
        context.mark_encoded()
        pool.submit(transaction)
        context.complete(0, error="fault")
        pool.poll()
        assert present.count == 1

    def test_transaction_without_output_needs_no_display_report(self) -> None:
        context = _Context()
        pool = MetalFrameTransactionPool(context, max_in_flight=2)
        source = _Lease("input")
        transaction = pool.begin(inputs=(source,))
        assert transaction is not None
        context.mark_encoded()
        pool.submit(transaction)
        context.complete(0)
        pool.poll()
        assert source.count == 1
        assert pool.awaiting_display == 0
        assert pool.note_display_done(transaction) is True


class TestCompletionTimeout:
    def test_timeout_pauses_new_frames_and_keeps_the_last_frame(self) -> None:
        context = _Context()
        clock = _Clock()
        pool = MetalFrameTransactionPool(
            context, max_in_flight=2, completion_timeout_s=1.0, clock=clock)
        transaction, source, present = _frame(pool)
        context.mark_encoded()
        pool.submit(transaction)

        clock.advance(0.5)
        assert pool.poll() == []
        assert pool.paused is False
        clock.advance(5.5)
        assert pool.poll() == []
        assert pool.paused is True
        assert "completion_timeout" in (pool.pause_reason or "")
        # New frames are refused while the GPU is behind; the frame already
        # on screen is untouched.
        assert pool.begin() is None
        assert (source.count, present.count) == (0, 0)

        # Still in flight: the pause holds.
        clock.advance(1.0)
        assert pool.poll() == []
        assert pool.paused is True

        context.complete(0)
        assert pool.poll() == [transaction]
        assert pool.paused is False
        assert pool.begin() is not None

    def test_reason_changes_with_the_timed_out_set(self) -> None:
        context = _Context()
        clock = _Clock()
        pool = MetalFrameTransactionPool(
            context, max_in_flight=4, completion_timeout_s=1.0, clock=clock)
        frames = [_frame(pool)[0] for _ in range(2)]
        for transaction in frames:
            context.mark_encoded()
            pool.submit(transaction)
        clock.advance(2.0)
        assert pool.poll() == []
        assert pool.pause_reason == "completion_timeout:sequences=[0, 1]"
        context.complete(0)
        assert pool.poll() == [frames[0]]
        assert pool.pause_reason == "completion_timeout:sequences=[1]"
        context.complete(1)
        pool.poll()
        assert pool.paused is False


class TestDrain:
    def test_drain_false_while_the_gpu_may_still_run(self) -> None:
        context = _Context()
        pool = MetalFrameTransactionPool(context, max_in_flight=2)
        transaction, source, _present = _frame(pool)
        context.mark_encoded()
        pool.submit(transaction)
        def wait_fails() -> None:
            raise RuntimeError("device lost while waiting")

        context.buffers[0].waitUntilCompleted = wait_fails  # type: ignore[method-assign]
        assert pool.drain(timeout_s=0.05) is False
        assert source.count == 0  # nothing pretended the GPU was done

    def test_drain_true_releases_inputs_on_the_owner_thread(self) -> None:
        context = _Context()
        pool = MetalFrameTransactionPool(context, max_in_flight=2)
        log: List[str] = []
        transaction, source, present = _frame(pool, log)
        context.mark_encoded()
        pool.submit(transaction)

        def wait_then_complete() -> None:
            context.complete(0)

        context.buffers[0].waitUntilCompleted = wait_then_complete  # type: ignore[method-assign]
        assert pool.drain(timeout_s=1.0) is True
        assert source.count == 1
        # The presenter still owns the output.
        assert present.count == 0
        assert pool.awaiting_display == 1
        assert {entry.split(":")[1] for entry in log} == {str(threading.get_ident())}


class TestStress:
    def test_out_of_order_completion_releases_each_lease_once(self) -> None:
        rng = random.Random(20261123)
        context = _Context()
        pool = MetalFrameTransactionPool(context, max_in_flight=4)
        leases: List[_Lease] = []
        outstanding: List[Tuple[MetalFrameTransaction, _CommandBuffer]] = []
        presented = 0

        for frame_index in range(300):
            source, present = _Lease(f"in{frame_index}"), _Lease(f"out{frame_index}")
            leases += [source, present]
            transaction = pool.begin(inputs=(source,), output=present)
            if transaction is None:
                continue  # hold/drop: the frame is skipped, nothing leaked
            context.mark_encoded()
            assert pool.submit(transaction) is True
            outstanding.append((transaction, context.buffers[-1]))

            # Complete a random subset in arbitrary order, then let the
            # presenter report for some of the finished frames.
            for transaction, buffer in rng.sample(outstanding, k=min(len(outstanding), 2)):
                outstanding.remove((transaction, buffer))
                context.complete(context.buffers.index(buffer))
            for transaction in pool.poll():
                if transaction.state is FrameTransactionState.COMPLETED:
                    presented += 1
                    if rng.random() < 0.5:
                        pool.note_display_done(transaction)
            for transaction in list(pool._awaiting_display.values()):
                if rng.random() < 0.3:
                    pool.note_display_done(transaction)

        for _transaction, buffer in list(outstanding):
            context.complete(context.buffers.index(buffer))
        for transaction in pool.poll():
            if transaction.state is FrameTransactionState.COMPLETED:
                pool.note_display_done(transaction)
        for transaction in list(pool._awaiting_display.values()):
            pool.note_display_done(transaction)

        assert pool.in_flight == 0
        assert pool.awaiting_display == 0
        assert presented > 0
        assert all(lease.count == 1 for lease in leases)
        assert pool.stats() == {
            "max_in_flight": 4, "in_flight": 0, "states": {},
            "awaiting_display": 0, "paused": False, "pause_reason": None,
            "ready_unpolled": 0,
        }
