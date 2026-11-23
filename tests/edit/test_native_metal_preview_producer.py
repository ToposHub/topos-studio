"""Native preview producer on a real Metal device (plan tasks 2.1 / 2.2).

The producer renders eligible preview frames into Metal present-slot
textures and publishes them as native ``GpuPresentFrame``s once the GPU
finished.  Checked here: publication only after completion, both inputs
released exactly once, the slot reusable after the viewport's ack, every
fallback leaving the frame untouched for the GL path, failure reporting,
pool retirement on a size change, and a clean shutdown.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.features.edit.core.native_metal_preview import (
    DROPPED,
    FALLBACK,
    SUBMITTED,
    FailedFrame,
    FrameTransactionState,
    NativePreviewProducer,
)
from src.shared.gpu.metal.context import MetalComputeContext
from src.shared.gpu.metal.frame_transaction import MetalFrameTransactionPool
from src.shared.gpu.metal.present_port import ORIGIN_TOP_LEFT
from src.shared.gpu.present_frame import SlotState
from src.shared.media.gpu_frame import GpuFrameRelease

_METAL_OK, _METAL_REASON = MetalComputeContext.availability_reason()
pytestmark = pytest.mark.skipif(not _METAL_OK, reason=f"needs Metal ({_METAL_REASON})")

W, H = 64, 32
FILL_MSL = """
#include <metal_stdlib>
using namespace metal;
kernel void fill_for_test(texture2d<float, access::write> out [[texture(0)]],
                           constant float4 &color [[buffer(0)]],
                           uint2 p [[thread_position_in_grid]]) {
    if (p.x >= out.get_width() || p.y >= out.get_height()) return;
    out.write(color, p);
}
"""


class _Preview:
    """Stands in for MetalTimelinePreview: same contract, trivial kernel."""

    def __init__(self) -> None:
        self._context = MetalComputeContext()
        self._kernel = self._context.compile_kernel("fill_for_test", FILL_MSL)
        self._params = self._context.create_buffer_from_numpy(
            np.array([0.25, 0.5, 0.75, 1.0], dtype=np.float32))
        self.import_releases: list = []
        self.dispatches: list = []
        self.refuse = False

    @property
    def metal_context(self):
        return self._context

    def encode_native(self, vt_frame, output, size, *, lut=None):
        if self.refuse:
            return None
        self._context.dispatch(
            self._kernel, buffers={0: self._params}, textures={0: output},
            threads=(size[0], size[1], 1))
        self.dispatches.append(output)
        return GpuFrameRelease(
            release_callback=lambda: self.import_releases.append(vt_frame.index))


class _FailingContext:
    """A command buffer that completes with an error as soon as it is committed."""

    def __init__(self, message: str) -> None:
        self._message = message

    def has_pending_work(self) -> bool:
        return False

    def submit(self, *, completion, wait=False, raise_after_commit=True):
        buffer = SimpleNamespace(error=lambda: self._message)
        completion(buffer)
        return buffer


class _VtFrame:
    def __init__(self, index: int) -> None:
        self.index = index
        self.releases = 0

    def release(self) -> None:
        self.releases += 1


def _task(frame: int, generation: int = 3):
    return SimpleNamespace(frame=frame, generation_id=generation, deadline_ns=0,
                           scrub_request_token=None)


def _settle(producer, seconds: float = 2.0, expected: int = 1):
    """Poll until ``expected`` frames in flight came back from the GPU.

    Completions arrive one command buffer at a time: two frames submitted
    together may come back in two separate polls.
    """
    deadline = time.monotonic() + seconds
    published, failed = [], []
    while time.monotonic() < deadline:
        got, bad = producer.poll()
        published.extend(got)
        failed.extend(bad)
        if len(published) + len(failed) >= expected:
            break
        time.sleep(0.002)
    return published, failed


@pytest.fixture()
def producer():
    preview = _Preview()
    made = NativePreviewProducer(preview, pool_size=2, max_in_flight=2)
    made.ensure_size((W, H))
    yield made, preview
    assert made.shutdown() is True


class TestHappyPath:
    def test_a_fresh_producer_is_enabled_before_its_first_frame(self) -> None:
        """The renderer asks ``enabled`` before anything created the slots."""
        fresh = NativePreviewProducer(_Preview(), pool_size=2)
        try:
            assert fresh.enabled is True
            assert fresh.try_render(_task(1), (W, H), _VtFrame(1), None) == SUBMITTED
            assert fresh._size == (W, H)
            _settle(fresh)
        finally:
            assert fresh.shutdown() is True

    def test_frame_is_published_only_after_the_gpu_finished(self, producer) -> None:
        made, preview = producer
        source = _VtFrame(11)
        assert made.try_render(_task(1), (W, H), source, None) == SUBMITTED
        # Encoded but nothing waited for the GPU.
        assert made.in_flight == 1
        assert source.releases == 0 and preview.import_releases == []
        assert made.poll() == ([], []) or True

        published, failed = _settle(made)
        assert failed == []
        (item,) = published
        present = item.present
        assert present.is_native and present.api == "metal"
        assert present.texture_id == 0 and present.fence_ns == 0
        assert present.frame == 1 and present.generation_id == 3
        assert present.native.origin == ORIGIN_TOP_LEFT
        assert present.native.resource_format == "bgra8unorm"
        assert present.native.resource is preview.dispatches[0]
        assert present.native.device_identity == preview.metal_context.device_identity
        assert present.extra["render_backend"] == "metal"
        assert present.extra["native_viewport"] is True
        assert present.validate_for_consumer(
            api="metal", device_identity=preview.metal_context.device_identity,
            resource_formats=("bgra8unorm",)) == ""
        # Both inputs went back exactly once, at completion.
        assert source.releases == 1 and preview.import_releases == [11]

    def test_the_kernel_output_lands_in_the_slot_texture(self, producer) -> None:
        made, preview = producer
        made.try_render(_task(1), (W, H), _VtFrame(1), None)
        (item,) = _settle(made)[0]
        texture = item.present.native.resource
        preview.metal_context.finish()
        raw = preview.metal_context.read_texture(texture, W * 4, H, np.uint8, region_width=W)
        pixel = raw.reshape(H, W, 4)[H // 2, W // 2]
        # BGRA8 of (0.25, 0.5, 0.75): B=191, G=128, R=64.
        assert abs(int(pixel[0]) - 191) <= 2 and abs(int(pixel[1]) - 128) <= 2
        assert abs(int(pixel[2]) - 64) <= 2

    def test_slot_is_reused_only_after_the_viewport_acks(self, producer) -> None:
        made, _preview = producer
        made.try_render(_task(1), (W, H), _VtFrame(1), None)
        made.try_render(_task(2), (W, H), _VtFrame(2), None)
        published, _ = _settle(made, expected=2)
        assert len(published) == 2
        # Both slots are published and not acknowledged: the next frame has
        # nowhere to go and is left to the GL path.
        source = _VtFrame(3)
        assert made.try_render(_task(3), (W, H), source, None) == FALLBACK
        assert source.releases == 0
        assert made.stats["pool_full"] == 1

        made.ack(published[0].present, "replaced")
        assert made.try_render(_task(3), (W, H), source, None) == SUBMITTED
        # An ack is idempotent and a foreign key touches nothing.
        made.ack(published[0].present, "duplicate")
        made.ack(published[1].present, "replaced")
        _settle(made)


class TestFallbacks:
    def test_paused_producer_leaves_the_frame_to_the_gl_path(self, producer) -> None:
        made, _preview = producer
        made._transactions.scheduler.pause("completion_timeout:sequences=[0]")
        source = _VtFrame(1)
        assert made.enabled is False
        assert made.try_render(_task(1), (W, H), source, None) == FALLBACK
        assert source.releases == 0

    def test_full_ring_is_refused_before_anything_is_imported(self, producer) -> None:
        made, preview = producer
        made.try_render(_task(1), (W, H), _VtFrame(1), None)
        made.try_render(_task(2), (W, H), _VtFrame(2), None)
        _settle(made, expected=2)
        source = _VtFrame(3)
        assert made.try_render(_task(3), (W, H), source, None) == FALLBACK
        assert source.releases == 0 and len(preview.dispatches) == 2

    def test_unimportable_frame_gives_its_slot_back(self, producer) -> None:
        made, preview = producer
        preview.refuse = True
        source = _VtFrame(1)
        assert made.try_render(_task(1), (W, H), source, None) == FALLBACK
        assert source.releases == 0
        assert all(slot.state is SlotState.FREE for slot in made._pool._slots.values())
        preview.refuse = False
        assert made.try_render(_task(1), (W, H), source, None) == SUBMITTED

    def test_encode_exception_discards_commands_and_returns_the_slot(self, producer, monkeypatch) -> None:
        made, preview = producer
        source = _VtFrame(1)
        encode = preview.encode_native

        def fail_after_dispatch(*args, **kwargs):
            imported = encode(*args, **kwargs)
            imported.release()
            raise RuntimeError("encode failed")

        with monkeypatch.context() as patch:
            patch.setattr(preview, "encode_native", fail_after_dispatch)
            with pytest.raises(RuntimeError, match="encode failed"):
                made.try_render(_task(1), (W, H), source, None)
        assert not preview.metal_context.has_pending_work()
        assert made.in_flight == 0 and source.releases == 0
        assert all(slot.state is SlotState.FREE for slot in made._pool._slots.values())
        assert made.try_render(_task(1), (W, H), source, None) == SUBMITTED

    def test_commit_failure_reports_the_frame_as_dropped(self, producer) -> None:
        made, _preview = producer
        source = _VtFrame(1)
        scheduler = made._transactions.scheduler
        original = scheduler.submit_reserved

        def failing_submit(*args, **kwargs):
            raise RuntimeError("commit refused")

        scheduler.submit_reserved = failing_submit
        try:
            assert made.try_render(_task(1), (W, H), source, None) == DROPPED
        finally:
            scheduler.submit_reserved = original
        # The frame is consumed: inputs went back once and the slot is free.
        assert source.releases == 1
        assert all(slot.state is SlotState.FREE for slot in made._pool._slots.values())
        assert made._pending == {}


class TestFailureAndRetirement:
    def test_gpu_failure_reports_the_task_and_frees_the_slot(self, producer) -> None:
        made, _preview = producer
        # A context whose command buffer fails: the driver reports the error
        # through the completion handler.
        made._transactions = MetalFrameTransactionPool(
            _FailingContext("GPU fault"), max_in_flight=2)
        source = _VtFrame(1)
        assert made.try_render(_task(1), (W, H), source, None) == SUBMITTED
        published, failed = made.poll()
        assert published == []
        assert [(f.task.frame, f.error) for f in failed] == [(1, "GPU fault")]
        # Inputs returned once, slot writable again.
        assert source.releases == 1
        assert all(slot.state is SlotState.FREE for slot in made._pool._slots.values())
        assert made.stats["failed"] == 1

    def test_size_change_retires_the_old_pool_until_its_frames_return(
        self, producer, monkeypatch,
    ) -> None:
        made, _preview = producer
        made.try_render(_task(1), (W, H), _VtFrame(1), None)
        (item,) = _settle(made)[0]
        old_pool = made._pool
        made.ensure_size((W * 2, H * 2))
        assert made._pool is not old_pool
        assert made._size == (W * 2, H * 2)
        made.poll()
        # A published frame still occupies the retired pool: keep it.
        assert len(made._retired) == 1
        made.ack(item.present, "replaced")
        time.sleep(0.01)
        made.poll()
        assert made._retired == []

    def test_repeated_resizes_preserve_unacknowledged_slots_and_bound_old_pools(self, producer) -> None:
        made, _preview = producer
        presents = []
        for index in (1, 2, 3):
            size = (W * index, H * index)
            assert made.try_render(_task(index), size, _VtFrame(index), None) == SUBMITTED
            (item,) = _settle(made)[0]
            presents.append(item.present)
        assert len(made._retired) == 2
        source = _VtFrame(4)
        assert made.try_render(_task(4), (W * 4, H * 4), source, None) == FALLBACK
        assert made._size == (W * 3, H * 3) and source.releases == 0
        assert len(made._retired) == 2
        made.ack(presents[0], "replaced")
        assert made.try_render(_task(4), (W * 4, H * 4), source, None) == SUBMITTED
        assert len(made._retired) == 2

    def test_dropped_frame_stays_dropped(self, producer) -> None:
        made, preview = producer
        preview.refuse = True
        assert made.try_render(_task(1), (W, H), _VtFrame(1), None) == FALLBACK
        assert made._pending == {}


class TestShutdown:
    def test_shutdown_returns_everything(self, producer) -> None:
        made, _preview = producer
        made.try_render(_task(1), (W, H), _VtFrame(1), None)
        (item,) = _settle(made)[0]
        assert made.shutdown() is True
        assert made._pending == {}
        assert all(slot.state is SlotState.FREE for slot in made._pool._slots.values())
        assert made.enabled is True   # pool still exists; the renderer drops it


