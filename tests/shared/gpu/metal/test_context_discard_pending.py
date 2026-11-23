"""``MetalComputeContext.discard_pending`` (Metal migration plan task 1.2).

Commands encoded but not submitted belong to the frame that encoded them.
When the submission ring turns out to be full, they must be dropped as one
group instead of being appended to the next frame's command buffer.
"""

from __future__ import annotations

from typing import Any, Tuple

import pytest

from src.shared.gpu.metal.context import MetalComputeError, MetalComputeContext
from src.shared.gpu.metal.submission_scheduler import MetalSubmissionScheduler


class _FakeMetal:
    MTLPixelFormatR8Unorm = 10
    MTLResourceStorageModeShared = 0
    MTLTextureUsageShaderRead = 1
    MTLTextureUsageShaderWrite = 2

    @staticmethod
    def MTLSizeMake(w: int, h: int, d: int) -> Tuple[int, int, int]:
        return (w, h, d)

    @staticmethod
    def MTLRegionMake2D(x: int, y: int, w: int, h: int) -> Tuple[int, int, int, int]:
        return (x, y, w, h)


class _Texture:
    def __init__(self, width: int = 4, height: int = 4) -> None:
        self.width = width
        self.height = height


class _Encoder:
    def __init__(self, command_buffer: "_CommandBuffer") -> None:
        self.command_buffer = command_buffer

    def setComputePipelineState_(self, state: Any) -> None:
        self.command_buffer.pipelines.append(state)

    def setBuffer_offset_atIndex_(self, buffer: Any, offset: int, index: int) -> None:
        self.command_buffer.buffers.append((index, buffer))

    def setTexture_atIndex_(self, texture: Any, index: int) -> None:
        self.command_buffer.textures.append((index, texture))

    def dispatchThreads_threadsPerThreadgroup_(self, size: Any, group: Any) -> None:
        self.command_buffer.dispatches += 1

    def endEncoding(self) -> None:
        self.command_buffer.ended = True


class _CommandBuffer:
    def __init__(self, queue: "_Queue") -> None:
        self.queue = queue
        self.committed = False
        self.ended = False
        self.pipelines: list = []
        self.buffers: list = []
        self.textures: list = []
        self.dispatches = 0
        self.completion = None
        self.error_value = None

    def computeCommandEncoder(self) -> _Encoder:
        return _Encoder(self)

    def addCompletedHandler_(self, handler: Any) -> None:
        self.completion = handler

    def commit(self) -> None:
        self.committed = True

    def waitUntilCompleted(self) -> None:
        pass

    def error(self) -> Any:
        return self.error_value


class _Queue:
    def __init__(self) -> None:
        self.buffers: list[_CommandBuffer] = []

    def commandBuffer(self) -> _CommandBuffer:
        command_buffer = _CommandBuffer(self)
        self.buffers.append(command_buffer)
        return command_buffer


class _Pipeline:
    """Stands in for a compiled kernel (``MetalKernel``)."""

    max_threads_per_group = 1024
    pipeline_state = "pipeline"


class _Device:
    def __init__(self) -> None:
        self.queue = _Queue()

    def newCommandQueue(self) -> _Queue:
        return self.queue

    def newBufferWithBytes_length_options_(self, data: bytes, length: int, options: int) -> Any:
        return object()

    def newBufferWithLength_options_(self, length: int, options: int) -> Any:
        return object()

    def hasUnifiedMemory(self) -> bool:
        return True


def _context(monkeypatch: pytest.MonkeyPatch) -> MetalComputeContext:
    import sys

    fake = type(sys)("Metal")
    for name in dir(_FakeMetal):
        if not name.startswith("__"):
            setattr(fake, name, getattr(_FakeMetal, name))
    monkeypatch.setitem(sys.modules, "Metal", fake)
    context = MetalComputeContext(device=_Device())
    context._kernels["k"] = _Pipeline()  # type: ignore[assignment]
    return context


def _dispatch(context: MetalComputeContext) -> None:
    context.dispatch(
        context._kernels["k"],  # type: ignore[arg-type]
        buffers={0: context.create_buffer(16)},
        threads=(4, 1, 1),
    )


def test_nothing_pending_before_encoding() -> None:
    import sys

    real_metal = sys.modules.get("Metal")
    if real_metal is None:
        pytest.skip("pyobjc-framework-Metal not installed")
    try:
        context = MetalComputeContext()
    except MetalComputeError as exc:
        pytest.skip(str(exc))
    assert context.has_pending_work() is False
    assert context.discard_pending() is False


def test_discard_drops_the_whole_encoded_group(monkeypatch: pytest.MonkeyPatch) -> None:
    context = _context(monkeypatch)
    _dispatch(context)
    _dispatch(context)
    assert context.has_pending_work() is True

    assert context.discard_pending() is True
    assert context.has_pending_work() is False
    # Nothing was committed.
    assert all(not buffer.committed for buffer in context._queue.buffers)

    # The next frame starts from an empty command buffer.
    _dispatch(context)
    context.submit(wait=False)
    committed = context._queue.buffers[-1]
    assert committed.committed is True
    assert committed.dispatches == 1


def test_submit_after_discard_does_not_report_dropped_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _context(monkeypatch)
    _dispatch(context)
    context.discard_pending()
    # An empty submit after a deliberate discard is "no work", not an error.
    assert context.submit(wait=False) is None


def test_full_ring_after_encoding_is_discarded_not_carried_over(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _context(monkeypatch)
    scheduler = MetalSubmissionScheduler(context, max_in_flight=1)

    _dispatch(context)
    assert scheduler.submit() is not None

    # Frame 2 encoded before the ring was known to be full.
    _dispatch(context)
    assert scheduler.submit() is None
    assert context.has_pending_work() is True
    assert context.discard_pending() is True

    # Frame 3 must not contain frame 2's kernel.
    context._queue.buffers[0].completion(context._queue.buffers[0])
    assert scheduler.poll() == (0,)
    _dispatch(context)
    assert scheduler.submit() is not None
    first = context._queue.buffers[0]
    third = context._queue.buffers[-1]
    assert third is not first
    assert third.dispatches == 1


class _ExplodingKernel:
    """A kernel whose pipeline access fails inside the encoder."""

    max_threads_per_group = 64

    @property
    def pipeline_state(self) -> Any:
        raise RuntimeError("pipeline lost")


def test_dispatch_exception_still_leaves_discard_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _context(monkeypatch)
    _dispatch(context)
    buffer = context.create_buffer(16)
    with pytest.raises(RuntimeError):
        context.dispatch(
            _ExplodingKernel(),  # type: ignore[arg-type]
            buffers={0: buffer}, threads=(4, 1, 1), group=(1, 1, 1),
        )
    assert context.has_dropped_work is True
    assert context.discard_pending() is True
    assert context.has_dropped_work is False
    # The next frame encodes into a fresh command buffer.
    _dispatch(context)
    assert len(context._queue.buffers[-1].dispatches and [1]) == 1
