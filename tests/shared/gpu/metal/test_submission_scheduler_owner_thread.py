"""Owner-thread reclamation for the Metal submission scheduler (plan task 1.2).

Metal's completion handler runs on the driver's thread.  It may only queue the
finished command buffer; releasing resources and running ``on_complete``
belong to the thread that owns them, inside ``poll()``.  Capacity is reserved
before encoding so encoded commands never wait in the context for another
frame, and a completion that never arrives pauses the device instead of
letting work pile up.
"""

from __future__ import annotations

import threading
from typing import Any, Callable, List, Optional

import pytest

from src.shared.gpu.metal.submission_scheduler import MetalSubmissionScheduler


class _CommandBuffer:
    def __init__(self) -> None:
        self.error_value: Any = None
        self.wait_calls = 0
        self.released = False

    def error(self) -> Any:
        return self.error_value

    def waitUntilCompleted(self) -> None:
        self.wait_calls += 1


class _Clock:
    """Controllable monotonic clock."""

    def __init__(self, start: float = 100.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _Context:
    """MetalComputeContext double: records the completion handler per submit."""

    def __init__(self) -> None:
        self.callbacks: List[Callable[[Any], None]] = []
        self.command_buffers: List[_CommandBuffer] = []
        self.submit_error: Optional[Exception] = None
        self.submit_returns_none = False

    def submit(self, *, completion: Callable[[Any], None], wait: bool) -> Any:
        assert wait is False
        if self.submit_error is not None:
            raise self.submit_error
        if self.submit_returns_none:
            return None
        command_buffer = _CommandBuffer()
        self.callbacks.append(completion)
        self.command_buffers.append(command_buffer)
        return command_buffer

    def complete(self, index: int, error: Any = None) -> _CommandBuffer:
        command_buffer = self.command_buffers[index]
        command_buffer.error_value = error
        self.callbacks[index](command_buffer)
        return command_buffer


class TestCompletionQueue:
    def test_completion_does_not_release_until_owner_polls(self) -> None:
        context = _Context()
        scheduler = MetalSubmissionScheduler(context, max_in_flight=2)
        threads: List[int] = []
        released: List[str] = []

        scheduler.submit(release_callbacks=(lambda: (released.append("input"),
                                                      threads.append(threading.get_ident())),))
        context.complete(0)
        # The driver thread only queued the result.
        assert released == []
        assert scheduler.in_flight == 0

        owner = threading.get_ident()
        assert scheduler.poll() == (0,)
        assert released == ["input"]
        assert threads == [owner]
        assert scheduler.poll() == ()

    def test_on_complete_runs_on_owner_thread_with_error(self) -> None:
        context = _Context()
        scheduler = MetalSubmissionScheduler(context, max_in_flight=2)
        seen: List[tuple] = []

        scheduler.submit(on_complete=lambda submission, error: seen.append(
            (threading.get_ident(), submission.sequence, error)))
        context.complete(0, error="GPU fault")
        assert seen == []
        scheduler.poll()
        assert seen == [(threading.get_ident(), 0, "GPU fault")]
        assert scheduler.error(0) == "GPU fault"

    def test_gpu_failure_still_releases_each_lease_once(self) -> None:
        context = _Context()
        scheduler = MetalSubmissionScheduler(context, max_in_flight=4)
        counts = {"input": 0, "output": 0}

        def bump(key: str) -> None:
            counts[key] += 1

        scheduler.submit(release_callbacks=(
            lambda: bump("input"), lambda: bump("output"),
        ))
        command_buffer = context.complete(0, error="GPU fault")
        # A driver may deliver the same completion twice; nothing runs twice.
        context.callbacks[0](command_buffer)
        scheduler.poll()
        scheduler.poll()
        assert counts == {"input": 1, "output": 1}

    def test_broken_release_callback_does_not_block_the_rest(self) -> None:
        context = _Context()
        scheduler = MetalSubmissionScheduler(context, max_in_flight=4)
        order: List[str] = []

        def explode() -> None:
            raise RuntimeError("release failed")

        scheduler.submit(release_callbacks=(explode, lambda: order.append("after")))
        scheduler.submit(release_callbacks=(lambda: order.append("next"),))
        context.complete(0)
        context.complete(1)
        assert scheduler.poll() == (0, 1)
        assert order == ["after", "next"]

    def test_on_complete_failure_is_recorded_not_raised(self) -> None:
        context = _Context()
        scheduler = MetalSubmissionScheduler(context, max_in_flight=2)

        def explode(_submission: Any, _error: Any) -> None:
            raise ValueError("callback bug")

        scheduler.submit(on_complete=explode)
        context.complete(0)
        assert scheduler.poll() == (0,)
        assert "on_complete failed" in (scheduler.error(0) or "")

    def test_synchronous_completion_is_handled_once(self) -> None:
        context = _Context()
        scheduler = MetalSubmissionScheduler(context, max_in_flight=1)
        released: List[str] = []
        real_submit = context.submit

        def synchronous_submit(*, completion: Callable[[Any], None], wait: bool) -> Any:
            command_buffer = real_submit(completion=completion, wait=wait)
            completion(command_buffer)
            return command_buffer

        context.submit = synchronous_submit  # type: ignore[method-assign]
        assert scheduler.submit(release_callbacks=(lambda: released.append("once"),)) is not None
        assert scheduler.in_flight == 0
        assert released == []
        assert scheduler.poll() == (0,)
        assert released == ["once"]


class TestReservationBeforeEncoding:
    def test_full_ring_refuses_reservation_without_touching_context(self) -> None:
        context = _Context()
        scheduler = MetalSubmissionScheduler(context, max_in_flight=1)
        assert scheduler.try_reserve() == 0
        assert scheduler.try_reserve() is None
        # Nothing was committed, so there is nothing to discard.
        assert context.callbacks == []
        assert scheduler.in_flight == 1

    def test_submit_reserved_commits_under_reservation(self) -> None:
        context = _Context()
        scheduler = MetalSubmissionScheduler(context, max_in_flight=1)
        sequence = scheduler.try_reserve()
        released: List[str] = []
        submission = scheduler.submit_reserved(
            sequence, release_callbacks=(lambda: released.append("frame"),))
        assert submission is not None and submission.sequence == sequence
        assert scheduler.try_reserve() is None
        context.complete(0)
        scheduler.poll()
        assert released == ["frame"]
        assert scheduler.try_reserve() is not None

    def test_cancel_reservation_returns_capacity(self) -> None:
        context = _Context()
        scheduler = MetalSubmissionScheduler(context, max_in_flight=1)
        sequence = scheduler.try_reserve()
        assert scheduler.cancel_reservation(sequence) is True
        assert scheduler.in_flight == 0
        assert scheduler.cancel_reservation(sequence) is False
        assert scheduler.submit_reserved(sequence) is None

    def test_submit_after_encoding_on_full_ring_leaves_context_uncommitted(self) -> None:
        context = _Context()
        scheduler = MetalSubmissionScheduler(context, max_in_flight=1)
        assert scheduler.submit() is not None
        # Encoded work meets a full ring: nothing is committed.
        assert scheduler.submit() is None
        assert len(context.callbacks) == 1

    def test_context_submit_failure_releases_reservation(self) -> None:
        context = _Context()
        scheduler = MetalSubmissionScheduler(context, max_in_flight=1)
        context.submit_error = RuntimeError("encode failed")
        with pytest.raises(RuntimeError):
            scheduler.submit()
        assert scheduler.in_flight == 0
        assert scheduler.try_reserve() is not None

    def test_context_without_work_releases_reservation(self) -> None:
        context = _Context()
        scheduler = MetalSubmissionScheduler(context, max_in_flight=1)
        context.submit_returns_none = True
        assert scheduler.submit() is None
        assert scheduler.in_flight == 0


class TestCompletionTimeout:
    def test_slow_completion_is_reported_then_pauses_new_submissions(self) -> None:
        context = _Context()
        clock = _Clock()
        scheduler = MetalSubmissionScheduler(
            context, max_in_flight=4, completion_timeout_s=1.0, clock=clock)
        assert scheduler.submit() is not None
        clock.advance(0.5)
        assert scheduler.pending_timeouts() == ()

        # Only the clock moves: the completion never arrives.
        clock.advance(5.5)
        assert scheduler.pending_timeouts() == (0,)
        scheduler.pause("completion_timeout:sequence=0")
        assert scheduler.paused is True
        assert scheduler.try_reserve() is None

        # Still in flight: the pause holds so nothing stacks on the stuck GPU.
        assert scheduler.resume_if_idle() is False
        context.complete(0)
        assert scheduler.poll() == (0,)
        assert scheduler.resume_if_idle() is True
        assert scheduler.try_reserve() is not None

    def test_without_timeout_nothing_is_reported(self) -> None:
        clock = _Clock()
        scheduler = MetalSubmissionScheduler(_Context(), max_in_flight=1, clock=clock)
        scheduler.submit()
        clock.advance(1e9)
        assert scheduler.pending_timeouts() == ()

    def test_invalid_timeout_is_rejected(self) -> None:
        with pytest.raises(ValueError):
            MetalSubmissionScheduler(_Context(), max_in_flight=1, completion_timeout_s=0.0)


class _CommitErrorContext(_Context):
    """A context whose command buffer fails synchronously after commit."""

    def submit(
        self, *, completion: Callable[[Any], None], wait: bool,
        raise_after_commit: bool = True,
    ) -> Any:
        command_buffer = super().submit(completion=completion, wait=wait)
        command_buffer.error_value = "fault at commit"
        if raise_after_commit:
            raise RuntimeError("Metal command buffer 执行失败: fault at commit")
        return command_buffer


class TestCommittedBufferFailingAtCommit:
    def test_error_after_commit_flows_through_the_completion(self) -> None:
        context = _CommitErrorContext()
        scheduler = MetalSubmissionScheduler(context, max_in_flight=2)
        released: List[str] = []
        errors: List[Any] = []

        # No exception: the buffer is committed, so its leases must travel
        # through the completion path rather than being forgotten.
        submission = scheduler.submit(
            release_callbacks=(lambda: released.append("lease"),),
            on_complete=lambda _submission, error: errors.append(error),
        )
        assert submission is not None
        assert scheduler.in_flight == 1
        assert released == []

        # The driver delivers the completion with the buffer's own error.
        context.callbacks[0](context.command_buffers[0])
        assert scheduler.poll() == (0,)
        assert released == ["lease"]
        assert errors == ["fault at commit"]
        assert scheduler.error(0) == "fault at commit"

    def test_plain_exception_before_commit_still_raises(self) -> None:
        context = _Context()
        scheduler = MetalSubmissionScheduler(context, max_in_flight=1)
        context.submit_error = RuntimeError("no completion support")
        with pytest.raises(RuntimeError):
            scheduler.submit()
        assert scheduler.in_flight == 0


class TestDrainLifecycle:
    def test_wait_idle_leaves_completions_for_poll(self) -> None:
        context = _Context()
        scheduler = MetalSubmissionScheduler(context, max_in_flight=2)
        released: List[str] = []
        scheduler.submit(release_callbacks=(lambda: released.append("later"),))

        def wait_then_complete(command_buffer: Any) -> None:
            context.complete(0)

        assert scheduler.wait_idle(wait_fn=wait_then_complete, timeout_s=1.0) is True
        assert scheduler.in_flight == 0
        # Nothing ran: the owner decides when to reclaim.
        assert released == []
        assert scheduler.ready_unpolled == 1
        assert scheduler.poll() == (0,)
        assert released == ["later"]


    def test_drain_runs_pending_releases_on_the_owner_thread(self) -> None:
        context = _Context()
        scheduler = MetalSubmissionScheduler(context, max_in_flight=2)
        threads: List[int] = []
        scheduler.submit(release_callbacks=(
            lambda: threads.append(threading.get_ident()),))
        context.complete(0)
        assert threads == []
        assert scheduler.drain(timeout_s=0.1) is True
        assert threads == [threading.get_ident()]

    def test_drain_false_means_gpu_may_still_reference_resources(self) -> None:
        context = _Context()
        scheduler = MetalSubmissionScheduler(context, max_in_flight=1)
        scheduler.submit()

        def wait_fails(_command_buffer: Any) -> None:
            raise RuntimeError("device lost while waiting")

        # The wait could not confirm the buffer: the GPU may still be busy.
        assert scheduler.drain(wait_fn=wait_fails, timeout_s=0.05) is False
        assert scheduler.in_flight == 1

    def test_wait_confirms_a_buffer_whose_handler_is_late(self) -> None:
        context = _Context()
        scheduler = MetalSubmissionScheduler(context, max_in_flight=1)
        released: List[str] = []
        scheduler.submit(release_callbacks=(lambda: released.append("lease"),))
        # waitUntilCompleted returns; the driver has not delivered the handler.
        assert scheduler.wait_idle(timeout_s=0.1) is True
        assert scheduler.in_flight == 0
        assert released == []
        assert scheduler.poll() == (0,)
        assert released == ["lease"]
        # A late driver callback for the same buffer changes nothing.
        context.complete(0)
        assert scheduler.poll() == ()
        assert released == ["lease"]

    def test_drain_true_after_completion_arrives(self) -> None:
        context = _Context()
        scheduler = MetalSubmissionScheduler(context, max_in_flight=1)

        def wait_then_complete(command_buffer: Any) -> None:
            command_buffer.wait_calls += 1
            context.complete(0)

        scheduler.submit()
        assert scheduler.drain(wait_fn=wait_then_complete, timeout_s=1.0) is True
        assert scheduler.in_flight == 0
