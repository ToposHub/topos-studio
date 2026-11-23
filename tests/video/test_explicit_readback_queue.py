"""Explicit GPU readback on its own bounded queue (plan 7.2 / 7.3).

A screenshot, a grab, a thumbnail or a monitor frame is a one-off GPU->CPU
copy that a user action asked for.  It must not block the UI or the render
thread, it must be bounded, and it must be counted apart from the readbacks
the playing image makes.  The queue and its policies need no GPU (the reads
below are stand-ins); the Metal reader is exercised on the device.
"""

from __future__ import annotations

import threading
import time

import numpy as np
import pytest

from src.shared.gpu.present_frame import PresentColorInfo
from src.shared.video.explicit_readback_queue import (
    EXPLICIT_READBACK_PURPOSES,
    ExplicitReadbackQueue,
    FullPolicy,
    ReadbackImage,
    ReadbackOrigin,
    ReadbackPurpose,
    ReadbackRequest,
    TicketState,
    borrow_cpu_plane,
    note_explicit_readback,
    readback_breakdown,
    reset_explicit_readbacks,
)
from src.shared.video.pixel_copy_counters import CopyKind, add_pixel_copy, reset_pixel_copies, snapshot_pixel_copies

TERMINAL = {TicketState.DONE, TicketState.FAILED, TicketState.CANCELLED, TicketState.REJECTED}


def _pixels(seed: int = 0) -> np.ndarray:
    return (np.arange(2 * 3 * 4, dtype=np.uint16).reshape(2, 3, 4) + seed).astype(np.uint8)


def _image(seed: int = 0, **fields) -> ReadbackImage:
    return ReadbackImage(pixels=_pixels(seed), width=3, height=2, format="rgba8", **fields)


class _Lease:
    """Counts how often a request's lease is given back."""

    def __init__(self) -> None:
        self.count = 0

    def release(self) -> None:
        self.count += 1


class _Gate:
    """A read the test holds open, so the queue can be inspected mid-flight."""

    def __init__(self, seed: int = 0) -> None:
        self.started = threading.Event()
        self.go = threading.Event()
        self.seed = seed
        self.thread: str = ""

    def read(self) -> ReadbackImage:
        self.thread = threading.current_thread().name
        self.started.set()
        assert self.go.wait(10.0), "the test never opened the gate"
        return _image(self.seed)


def _request(read, **fields) -> tuple[ReadbackRequest, _Lease]:
    lease = _Lease()
    return ReadbackRequest(read=read, release=lease.release, **fields), lease


@pytest.fixture()
def counted():
    """Explicit readbacks and the global copy counter start from zero."""
    reset_explicit_readbacks()
    reset_pixel_copies()
    yield
    reset_explicit_readbacks()
    reset_pixel_copies()


@pytest.fixture()
def queue():
    made = ExplicitReadbackQueue(max_pending=4, idle_timeout_s=30.0)
    yield made
    assert made.close(timeout_s=10.0) is True


def _settled(ticket, timeout: float = 10.0) -> bool:
    assert ticket.wait(timeout), f"ticket {ticket.ticket_id} never settled"
    return True


class TestOneReadback:
    def test_it_runs_on_the_worker_thread_and_delivers_the_picture(self, queue) -> None:
        gate = _Gate(seed=5)
        done: list = []
        request, lease = _request(gate.read, on_done=done.append)
        ticket = queue.submit(request)
        assert gate.started.wait(10.0)
        assert ticket.state is TicketState.RUNNING and ticket.image is None
        gate.go.set()
        _settled(ticket)
        assert ticket.state is TicketState.DONE and ticket.error is None
        assert ticket.image is not None and np.array_equal(ticket.image.pixels, _pixels(5))
        assert done == [ticket.image]
        assert gate.thread != threading.current_thread().name      # never on the caller
        assert lease.count == 1                                      # the lease came back once
        assert queue.stats()["completed"] == 1 and queue.in_flight == 0

    def test_the_callbacks_of_each_outcome(self, queue) -> None:
        calls: dict[str, list] = {name: [] for name in ("done", "cancelled", "rejected", "failed")}
        request, _lease = _request(
            lambda: _image(),
            on_done=lambda image: calls["done"].append(image),
            on_cancelled=lambda reason: calls["cancelled"].append(reason),
            on_rejected=lambda reason: calls["rejected"].append(reason),
            on_failed=lambda error: calls["failed"].append(error))
        _settled(queue.submit(request))
        assert len(calls["done"]) == 1 and not calls["cancelled"] and not calls["rejected"] and not calls["failed"]

    def test_requests_run_one_at_a_time_in_order(self, queue) -> None:
        order: list = []
        tickets = []
        for index in range(4):
            request, _lease = _request(lambda index=index: (order.append(index), _image(index))[1])
            tickets.append(queue.submit(request))
        for ticket in tickets:
            _settled(ticket)
            assert ticket.state is TicketState.DONE
        assert order == [0, 1, 2, 3]

    def test_a_purpose_outside_the_vocabulary_is_refused(self) -> None:
        with pytest.raises(ValueError, match="目的"):
            ReadbackRequest(read=lambda: _image(), purpose="whenever")
        with pytest.raises(ValueError, match="read 必须可调用"):
            ReadbackRequest(read=None)  # type: ignore[arg-type]
        assert set(EXPLICIT_READBACK_PURPOSES) == {ReadbackPurpose.EXPLICIT_SNAPSHOT, ReadbackPurpose.MONITOR}


class TestFullQueue:
    def test_a_readback_that_does_not_fit_is_rejected_and_its_lease_returned(self) -> None:
        made = ExplicitReadbackQueue(max_pending=2, full_policy=FullPolicy.REJECT)
        gate = _Gate()
        held, held_lease = _request(gate.read)
        queued, queued_lease = _request(lambda: _image())
        rejected, rejected_lease = _request(lambda: _image(), on_rejected=lambda reason: None)
        try:
            running = made.submit(held)
            waiting = made.submit(queued)
            assert gate.started.wait(10.0) and made.in_flight == 2
            reasons: list = []
            refused = made.submit(
                ReadbackRequest(read=lambda: _image(), release=rejected_lease.release,
                                on_rejected=reasons.append))
            assert refused.state is TicketState.REJECTED and reasons == ["queue_full"]
            assert refused.wait(0) is True                      # already final
            gate.go.set()
            _settled(running)
            _settled(waiting)
            assert running.state is waiting.state is TicketState.DONE
            assert (held_lease.count, queued_lease.count, rejected_lease.count) == (1, 1, 1)
            stats = made.stats()
            assert (stats["submitted"], stats["completed"], stats["rejected"]) == (2, 2, 1)
            assert stats["max_pending"] == 2
        finally:
            gate.go.set()
            assert made.close(timeout_s=10.0) is True

    def test_other_requests_are_not_pushed_out_by_a_full_queue(self) -> None:
        made = ExplicitReadbackQueue(max_pending=1, full_policy=FullPolicy.REJECT)
        gate = _Gate()
        try:
            first = made.submit(_request(gate.read)[0])
            assert gate.started.wait(10.0)
            second = made.submit(_request(lambda: _image())[0])
            assert second.state is TicketState.REJECTED
            gate.go.set()
            _settled(first)
            assert first.state is TicketState.DONE                   # untouched by the refusal
        finally:
            gate.go.set()
            assert made.close(timeout_s=10.0) is True


class TestLatestWins:
    def test_a_newer_readback_of_the_same_key_supersedes_the_older_ones(self) -> None:
        made = ExplicitReadbackQueue(max_pending=4, full_policy=FullPolicy.LATEST_WINS)
        gate = _Gate(seed=1)                                   # the read already running
        cancelled: list = []
        running, running_lease = _request(gate.read, key="grab")
        waiting, waiting_lease = _request(lambda: _image(2), key="grab", on_cancelled=cancelled.append)
        try:
            first = made.submit(running)
            assert gate.started.wait(10.0)                      # it is being read before the next ask
            second = made.submit(waiting)
            newest = made.submit(_request(lambda: _image(3), key="grab")[0])
            # The queued one is withdrawn at once, on the calling thread.
            assert second.state is TicketState.CANCELLED and second.wait(0) is True
            assert cancelled and second.image is None
            assert waiting_lease.count == 1
            gate.go.set()                                      # the running one finishes late
            _settled(first)
            _settled(newest)
            assert first.state is TicketState.CANCELLED and first.image is None   # its result is dropped
            assert newest.state is TicketState.DONE and np.array_equal(newest.image.pixels, _pixels(3))
            assert running_lease.count == 1
            assert made.stats()["coalesced"] == 2              # the waiting one and the running one
        finally:
            gate.go.set()
            assert made.close(timeout_s=10.0) is True

    def test_a_different_key_is_left_alone(self) -> None:
        made = ExplicitReadbackQueue(max_pending=4, full_policy=FullPolicy.LATEST_WINS)
        gate = _Gate(seed=1)
        try:
            screenshot = made.submit(_request(gate.read, key="screenshot")[0])
            assert gate.started.wait(10.0)
            grab = made.submit(_request(lambda: _image(2), key="grab")[0])
            gate.go.set()
            _settled(screenshot)
            _settled(grab)
            assert screenshot.state is grab.state is TicketState.DONE
        finally:
            gate.go.set()
            assert made.close(timeout_s=10.0) is True


class TestCancelAndFailure:
    def test_cancel_withdraws_queued_reads_at_once_and_the_running_one_when_it_ends(self, queue) -> None:
        gate = _Gate()
        running, running_lease = _request(gate.read)
        waiting, waiting_lease = _request(lambda: _image(), on_cancelled=lambda reason: None)
        first = queue.submit(running)
        second = queue.submit(waiting)
        assert gate.started.wait(10.0)
        assert queue.cancel_all("page switched") == 2
        assert second.state is TicketState.CANCELLED and second.cancel_reason == "page switched"
        assert waiting_lease.count == 1
        assert first.state is not TicketState.CANCELLED          # still copying
        gate.go.set()
        _settled(first)
        assert first.state is TicketState.CANCELLED and first.image is None
        assert running_lease.count == 1
        assert queue.stats()["cancelled"] == 2

    def test_a_read_that_raises_fails_its_ticket_and_returns_the_lease(self, queue) -> None:
        failures: list = []

        def broken() -> ReadbackImage:
            raise OSError("texture gone")

        request, lease = _request(broken, on_failed=failures.append)
        ticket = queue.submit(request)
        _settled(ticket)
        assert ticket.state is TicketState.FAILED
        assert isinstance(ticket.error, OSError) and failures == [ticket.error]
        assert lease.count == 1
        assert queue.stats()["failed"] == 1

    def test_a_cancelled_read_that_fails_is_cancelled_not_failed(self, queue) -> None:
        gate = _Gate()

        def broken() -> ReadbackImage:
            gate.started.set()
            gate.go.wait(10.0)
            raise OSError("too late")

        request, lease = _request(broken)
        ticket = queue.submit(request)
        assert gate.started.wait(10.0)
        assert queue.cancel_all("gone") == 1
        gate.go.set()
        _settled(ticket)
        assert ticket.state is TicketState.CANCELLED and lease.count == 1


class TestLifecycle:
    def test_a_queue_with_nothing_to_do_goes_idle_and_wakes_up_again(self) -> None:
        made = ExplicitReadbackQueue(max_pending=2, idle_timeout_s=0.05)
        try:
            _settled(made.submit(_request(lambda: _image(1))[0]))
            deadline = time.monotonic() + 5.0
            while made.running and time.monotonic() < deadline:    # the worker exits after the idle time
                time.sleep(0.01)
            assert made.running == 0
            later = made.submit(_request(lambda: _image(2))[0])     # and is started by the next request
            _settled(later)
            assert later.state is TicketState.DONE and np.array_equal(later.image.pixels, _pixels(2))
        finally:
            assert made.close(timeout_s=10.0) is True

    def test_close_cancels_waits_for_the_worker_and_drains_every_reader_once(self) -> None:
        made = ExplicitReadbackQueue(max_pending=4, idle_timeout_s=30.0)

        class Reader:
            drains = 0

            def drain(self, *, timeout_s: float) -> bool:
                Reader.drains += 1
                return True

        reader = Reader()
        request, first_lease = _request(lambda: _image(), reader=reader)
        first = made.submit(request)
        _settled(first)
        second_request, second_lease = _request(lambda: _image(), reader=reader)
        second = made.submit(second_request)
        assert made.close(timeout_s=10.0) is True
        assert first.state is TicketState.DONE
        assert second.state is TicketState.CANCELLED and second.cancel_reason == "queue_closed"
        assert first_lease.count == 1 and second_lease.count == 1
        assert Reader.drains == 1                               # each reader once, however many reads
        assert made.in_flight == 0 and made.running == 0 and made.closed is True
        after = made.submit(_request(lambda: _image())[0])
        assert after.state is TicketState.REJECTED and str(after.error) == "queue_closed"

    def test_bad_settings_are_refused(self) -> None:
        with pytest.raises(ValueError, match="max_pending"):
            ExplicitReadbackQueue(max_pending=0)
        with pytest.raises(ValueError, match="idle_timeout_s"):
            ExplicitReadbackQueue(idle_timeout_s=0.0)


class TestAccounting:
    """Explicit, monitor and playback readbacks are told apart in the counters."""

    def test_a_purpose_is_counted_with_its_bytes_and_backend(self, counted) -> None:
        add_pixel_copy(CopyKind.CPU_DOWNLOAD, 1000)             # what the readback moved
        note_explicit_readback(purpose=ReadbackPurpose.EXPLICIT_SNAPSHOT, backend="metal", nbytes=1000)
        add_pixel_copy(CopyKind.CPU_DOWNLOAD, 500)
        note_explicit_readback(purpose=ReadbackPurpose.MONITOR, backend="opengl", nbytes=500)
        add_pixel_copy(CopyKind.CPU_DOWNLOAD, 300)              # a seek snapshot of the playing image
        breakdown = readback_breakdown()
        assert breakdown["explicit"] == {
            "events": 1, "bytes": 1000, "backends": {"metal": 1}}
        assert breakdown["monitor"] == {
            "events": 1, "bytes": 500, "backends": {"opengl": 1}}
        assert breakdown["playback"] == {"events": 1, "bytes": 300}
        assert breakdown["explicit"]["bytes"] + breakdown["monitor"]["bytes"] + breakdown["playback"]["bytes"] == 1800

    def test_the_global_counter_is_not_charged_twice(self, counted) -> None:
        add_pixel_copy(CopyKind.CPU_DOWNLOAD, 400)
        note_explicit_readback(purpose=ReadbackPurpose.EXPLICIT_SNAPSHOT, backend="metal", nbytes=400)
        assert snapshot_pixel_copies()[CopyKind.CPU_DOWNLOAD]["bytes"] == 400

    def test_notes_are_checked(self, counted) -> None:
        with pytest.raises(ValueError, match="目的"):
            note_explicit_readback(purpose="other", backend="metal", nbytes=1)
        with pytest.raises(ValueError, match="负"):
            note_explicit_readback(purpose=ReadbackPurpose.MONITOR, backend="metal", nbytes=-1)
        note_explicit_readback(purpose=ReadbackPurpose.MONITOR, backend="metal", nbytes=0)   # nothing moved
        assert readback_breakdown()["monitor"]["events"] == 0


class TestReadbackImage:
    def test_a_gpu_result_borrows_or_a_cache_record(self) -> None:
        color = PresentColorInfo(color_range="full")
        gpu = _image(1, color=color, frame_index=7, generation_id=3, source="program", backend="metal")
        assert gpu.from_disk_cache is False and gpu.nbytes == 2 * 3 * 4
        record = gpu.as_cache_record("thumb:7")
        assert record.from_disk_cache is True and record.pixels is None and record.cache_key == "thumb:7"
        # The record keeps what a cache key must agree on: size, format, colour, row order.
        assert (record.width, record.height, record.format) == (gpu.width, gpu.height, gpu.format)
        assert record.color == gpu.color and record.row_order == gpu.row_order
        assert (record.frame_index, record.generation_id, record.produced_by) == (7, 3, "metal")
        assert record.backend == "cache"

    def test_validation(self) -> None:
        with pytest.raises(ValueError, match="来源"):
            ReadbackImage(pixels=None, width=1, height=1, origin="somewhere")
        with pytest.raises(ValueError, match="行序"):
            ReadbackImage(pixels=_pixels(), width=3, height=2, row_order="sideways")
        with pytest.raises(ValueError, match="不可为负"):
            ReadbackImage(pixels=_pixels(), width=-1, height=2)
        with pytest.raises(ValueError, match="必须携带像素"):
            ReadbackImage(pixels=None, width=1, height=1)
        with pytest.raises(ValueError, match="不携带像素"):
            ReadbackImage(pixels=_pixels(), width=3, height=2, origin=ReadbackOrigin.DISK_CACHE, cache_key="k")
        with pytest.raises(ValueError, match="cache_key"):
            ReadbackImage(pixels=None, width=1, height=1, origin=ReadbackOrigin.DISK_CACHE)

    def test_to_rgb8(self) -> None:
        assert _image(1).to_rgb8().shape == (2, 3, 3)
        assert np.array_equal(_image(1).to_rgb8(), _pixels(1)[..., :3])
        bgra = ReadbackImage(pixels=np.array([[[1, 2, 3, 255]]], np.uint8), width=1, height=1, format="bgra8")
        assert bgra.to_rgb8().tolist() == [[[3, 2, 1]]]            # memory B, G, R -> picture R, G, B
        half = ReadbackImage(pixels=np.array([[[0.0, 0.5, 1.0, 1.0], [2.0, -0.25, 0.25, 1.0]]], np.float16),
                             width=2, height=1, format="rgba16f")
        assert half.to_rgb8().tolist() == [[[0, 128, 255], [255, 0, 64]]]
        with pytest.raises(ValueError, match="没有像素"):
            _image().as_cache_record("k").to_rgb8()

    def test_a_cpu_plane_is_borrowed_without_a_copy(self) -> None:
        plane = np.zeros((4, 5, 3), np.uint8)
        image = borrow_cpu_plane(plane, format="rgb24", frame_index=3)
        assert image.pixels is plane and image.origin == ReadbackOrigin.CPU_PLANE and image.backend == "cpu"
        assert (image.width, image.height, image.frame_index) == (5, 4, 3)
        with pytest.raises(ValueError, match="二维"):
            borrow_cpu_plane(np.zeros(5, np.uint8), format="rgb24")


class TestMetalReader:
    def test_it_reads_a_texture_back_whole(self) -> None:
        Metal = pytest.importorskip("Metal")
        from src.shared.gpu.metal.context import MetalComputeContext
        from src.shared.video.explicit_readback_queue import METAL_READBACK_FORMATS, MetalTextureReader

        if not MetalComputeContext.is_supported():
            pytest.skip(f"no Metal device: {MetalComputeContext.availability_reason()[1]}")
        device = MetalComputeContext().device
        reader = MetalTextureReader(device)
        width, height = 37, 23
        rgba = np.arange(width * height * 4, dtype=np.float32).reshape(height, width, 4) / 50.0
        try:
            texture = MetalComputeContext(device).create_texture_from_numpy(
                rgba.astype(np.float16), Metal.MTLPixelFormatRGBA16Float, width, height)
            color = PresentColorInfo(color_range="full")
            image = reader.read(texture, width=width, height=height, pixel_format="rgba16f", color=color,
                                frame_index=4, generation_id=2, source="program", purpose=ReadbackPurpose.MONITOR)
            assert image.origin == ReadbackOrigin.GPU and image.backend == "metal"
            assert image.pixels.dtype == np.float16 and image.pixels.shape == (height, width, 4)
            assert np.array_equal(image.pixels, rgba.astype(np.float16))
            assert (image.frame_index, image.generation_id, image.source, image.color) == (4, 2, "program", color)
            assert readback_breakdown()["monitor"]["backends"] == {"metal": 1}
            assert snapshot_pixel_copies()[CopyKind.CPU_DOWNLOAD]["bytes"] == width * height * 8
            # Display values come back as bytes.
            bytes8 = np.arange(width * height * 4, dtype=np.uint8).reshape(height, width, 4)
            texture8 = MetalComputeContext(device).create_texture_from_numpy(
                bytes8, Metal.MTLPixelFormatRGBA8Unorm, width, height)
            plain = reader.read(texture8, width=width, height=height, pixel_format="rgba8")
            assert plain.pixels.dtype == np.uint8 and np.array_equal(plain.pixels, bytes8)
            with pytest.raises(ValueError, match="格式"):
                reader.read(texture8, width=width, height=height, pixel_format="rgb565")
            with pytest.raises(ValueError, match="尺寸"):
                reader.read(texture8, width=0, height=height, pixel_format="rgba8")
            assert set(METAL_READBACK_FORMATS) == {"rgba8", "bgra8", "rgba16f", "rgba32f"}
        finally:
            reset_explicit_readbacks()
            reset_pixel_copies()
            assert reader.drain(timeout_s=10.0) is True
