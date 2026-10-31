from __future__ import annotations

from unittest.mock import MagicMock

from src.shared.gpu.metal.submission_scheduler import MetalSubmissionScheduler


class _Context:
    def __init__(self) -> None:
        self.callbacks = []
        self.command_buffers = []

    def submit(self, *, completion, wait):
        assert wait is False
        cb = MagicMock()
        cb.error.return_value = None
        cb.waitUntilCompleted = MagicMock()
        self.callbacks.append(completion)
        self.command_buffers.append(cb)
        return cb


class _SynchronousCompletionContext(_Context):
    def submit(self, *, completion, wait):
        command_buffer = super().submit(completion=completion, wait=wait)
        completion(command_buffer)
        return command_buffer


def test_scheduler_is_bounded_and_completion_releases() -> None:
    context = _Context()
    scheduler = MetalSubmissionScheduler(context, max_in_flight=2)
    released: list[int] = []

    first = scheduler.submit(release_callbacks=(lambda: released.append(1),))
    second = scheduler.submit(release_callbacks=(lambda: released.append(2),))
    assert first is not None and second is not None
    assert scheduler.submit() is None
    assert scheduler.in_flight == 2

    context.callbacks[0](context.command_buffers[0])
    assert released == [1]
    assert scheduler.in_flight == 1
    assert scheduler.poll() == (0,)

    third = scheduler.submit()
    assert third is not None
    assert scheduler.in_flight == 2


def test_scheduler_drains_without_hiding_queue_growth() -> None:
    context = _Context()
    scheduler = MetalSubmissionScheduler(context, max_in_flight=1)
    scheduler.submit()
    scheduler.drain()
    context.command_buffers[0].waitUntilCompleted.assert_called_once()


def test_scheduler_handles_completion_before_submit_returns() -> None:
    context = _SynchronousCompletionContext()
    scheduler = MetalSubmissionScheduler(context, max_in_flight=1)
    released: list[str] = []

    submission = scheduler.submit(
        release_callbacks=(lambda: released.append("released"),)
    )

    assert submission is not None
    assert released == ["released"]
    assert scheduler.in_flight == 0
    assert scheduler.poll() == (0,)
