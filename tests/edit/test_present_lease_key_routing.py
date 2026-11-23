"""Lease-key acknowledgement routing (Metal migration plan task 1.3).

A pool is rebuilt when the output size changes and the old one is retired.
Slot ids are reused in the new pool, so an acknowledgement that only names a
slot cannot say which frame it confirms: a late ack for a retired pool's slot
could release a different frame now occupying the same slot id in the new
pool.  Descriptors therefore carry a lease key (pool generation, slot,
submission) and acknowledgements are routed by it.
"""

from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.features.edit.core.edit_gpu_preview_renderer import EditGpuPreviewRenderer
from src.features.edit.core.timeline_preview_renderer import TimelinePreviewRenderer
from src.shared.gpu.present_frame import (
    GpuPresentFrame,
    NativePresentDescriptor,
    OutputSlotPool,
    PresentLeaseKey,
    SlotState,
)


def _pool(generation: int, size: int = 2) -> OutputSlotPool:
    pool = OutputSlotPool(size=size, pool_generation=generation)
    for slot_id in range(size):
        pool.register_slot(slot_id, texture_glo=100 + 10 * generation + slot_id,
                           width=64, height=64)
    return pool


def _published(pool: OutputSlotPool, frame: int) -> GpuPresentFrame:
    lease = pool.acquire_for_writing(generation_id=1, frame_index=frame)
    assert lease is not None
    lease.mark_ready(fence_ns=1)
    return lease.make_present_frame(frame=frame, generation_id=1)


def _renderer(current: OutputSlotPool, retired: list[OutputSlotPool]):
    """Bind the renderer's ack method to a shell holding two pool generations."""
    emitted: list[int] = []
    shell = SimpleNamespace(
        _present_pool=current,
        _retired_output_pools=[(pool, [], 0.0) for pool in retired],
        slot_acked=SimpleNamespace(emit=emitted.append),
    )
    shell.ack_present_slot = EditGpuPreviewRenderer.ack_present_slot.__get__(shell)
    return shell, emitted


class TestRendererAckRouting:
    def test_late_ack_from_retired_pool_spares_the_new_pool(self) -> None:
        retired, current = _pool(1), _pool(2)
        old_frame = _published(retired, frame=100)
        new_frame = _published(current, frame=101)
        assert old_frame.slot_id == new_frame.slot_id
        shell, emitted = _renderer(current, [retired])

        # The viewer confirms a frame of the retired pool after the resize.
        shell.ack_present_slot(old_frame.slot_id, old_frame.lease_key)

        assert retired.get_slot(0).state is SlotState.RELEASABLE
        # The new pool's same-numbered slot still belongs to its own frame.
        slot = current.get_slot(0)
        assert slot.state is SlotState.READY
        assert slot.frame_index == 101
        assert emitted == [old_frame.slot_id]

    def test_keyed_ack_reaches_the_current_pool(self) -> None:
        current = _pool(7)
        frame = _published(current, frame=3)
        shell, _emitted = _renderer(current, [])
        shell.ack_present_slot(frame.slot_id, frame.lease_key)
        assert current.get_slot(0).state is SlotState.RELEASABLE

    def test_stale_submission_key_is_ignored(self) -> None:
        current = _pool(7)
        first = _published(current, frame=1)
        shell, emitted = _renderer(current, [])
        shell.ack_present_slot(first.slot_id, first.lease_key)
        second = _published(current, frame=2)
        stale = replace(first.lease_key, submission_id=first.lease_key.submission_id)

        # A duplicate of the first confirmation must not release frame 2.
        shell.ack_present_slot(first.slot_id, stale)
        assert current.get_slot(0).state is SlotState.READY
        assert current.get_slot(0).frame_index == 2
        assert second.lease_key.submission_id > first.lease_key.submission_id
        assert current.stats()["stale_acks"] == 1

    def test_unknown_pool_generation_touches_nothing(self) -> None:
        current = _pool(7)
        frame = _published(current, frame=1)
        shell, emitted = _renderer(current, [])
        shell.ack_present_slot(0, PresentLeaseKey(pool_generation=99, slot_id=0, submission_id=1))
        assert current.get_slot(0).state is SlotState.READY
        # The telemetry signal still fires: the viewer did hand something back.
        assert emitted == [0]

    def test_without_key_the_legacy_broadcast_is_kept(self) -> None:
        retired, current = _pool(1), _pool(2)
        old_frame, new_frame = _published(retired, frame=100), _published(current, frame=101)
        shell, _emitted = _renderer(current, [retired])
        shell.ack_present_slot(0)
        assert retired.get_slot(0).state is SlotState.RELEASABLE
        assert current.get_slot(0).state is SlotState.RELEASABLE
        assert (old_frame.lease_key.pool_generation, new_frame.lease_key.pool_generation) == (1, 2)

    def test_negative_slot_is_ignored(self) -> None:
        current = _pool(7)
        shell, emitted = _renderer(current, [])
        shell.ack_present_slot(-1, PresentLeaseKey(7, 0, 1))
        assert emitted == []


class _RecordingRenderer:
    def __init__(self, accepts_key: bool = True) -> None:
        self.accepts_key = accepts_key
        self.calls: list[tuple] = []

    def ack_present_slot(self, slot_id: int, lease_key=None) -> None:
        if lease_key is not None and not self.accepts_key:
            raise TypeError("ack_present_slot() takes 2 positional arguments but 3 were given")
        self.calls.append((slot_id, lease_key))


def _tpr(renderer, **attrs) -> SimpleNamespace:
    shell = SimpleNamespace(_gpu_renderer=renderer, _annotated=[], **attrs)
    for name in ("_ack_present_slot_safe", "_make_present_ack_callback",
                 "_make_native_present_ack_callback", "_annotate_present_status_for_slot"):
        setattr(shell, name, getattr(TimelinePreviewRenderer, name).__get__(shell))
    return shell


class TestPreviewRendererCarriesTheKey:
    def test_viewer_callback_is_bound_to_the_descriptor_key(self) -> None:
        renderer = _RecordingRenderer()
        shell = _tpr(renderer)
        frame = _published(_pool(3, size=1), frame=9)
        ack = shell._make_present_ack_callback(frame)
        assert callable(ack)
        ack(frame.slot_id)
        assert renderer.calls == [(frame.slot_id, frame.lease_key)]

    def test_descriptor_without_key_keeps_the_plain_callback(self) -> None:
        renderer = _RecordingRenderer()
        shell = _tpr(renderer)
        legacy = GpuPresentFrame(texture_id=5, width=2, height=2, frame=1,
                                 generation_id=1, slot_id=0)
        assert shell._make_present_ack_callback(legacy) == renderer.ack_present_slot

    def test_native_frame_callback_never_touches_the_gl_pool(self) -> None:
        """A native slot is freed by the render thread, not by a GL ack."""
        renderer = _RecordingRenderer()
        shell = _tpr(renderer)
        native = GpuPresentFrame(
            texture_id=0, width=4, height=4, frame=1, generation_id=1, slot_id=0,
            extra={"render_backend": "metal"},
            native=NativePresentDescriptor(
                api="metal", device_identity="metal:GPU:1", resource=object()),
        )
        assert native.is_native is True
        ack = shell._make_present_ack_callback(native)
        shell._annotate_present_status_for_slot = (
            lambda slot_id, status: shell._annotated.append(slot_id))
        ack(0)
        assert renderer.calls == []
        # TPR's own protocol end is still noted once per slot.
        assert shell._annotated == [0]

    def test_no_renderer_means_no_callback(self) -> None:
        shell = _tpr(None)
        assert shell._make_present_ack_callback(object()) is None

    def test_renderer_predating_keys_falls_back_to_slot_id(self) -> None:
        renderer = _RecordingRenderer(accepts_key=False)
        shell = _tpr(renderer)
        shell._ack_present_slot_safe(4, PresentLeaseKey(1, 0, 1))
        assert renderer.calls == [(4, None)]

    def test_broken_renderer_never_raises(self) -> None:
        class _Broken:
            def ack_present_slot(self, slot_id: int, lease_key=None) -> None:
                raise RuntimeError("renderer gone")

        shell = _tpr(_Broken())
        shell._ack_present_slot_safe(4, PresentLeaseKey(1, 0, 1))
        shell._ack_present_slot_safe(4)
        shell._ack_present_slot_safe(-1, PresentLeaseKey(1, 0, 1))

    @pytest.mark.parametrize("generation", [1, 2, 3])
    def test_pools_never_share_a_generation(self, generation: int) -> None:
        assert _pool(generation).generation == generation
        assert _pool(generation).stats()["pool_generation"] == generation
