"""Metal present port logic (Metal migration plan task 2.1).

The port decides what reaches the screen and when a frame's lease goes back:
stale generations, a superseded scrub token, the wrong render size and a
missed deadline retire a frame without presenting it; no drawable keeps the
last picture; a frame is released exactly once — after its own present
completed, after a newer frame replaced it, and once no repeated showing
draws its texture any more.  The Metal pass itself is replaced by a fake here;
the real shader is covered on a real device by ``test_metal_present_pass.py``.
"""

from __future__ import annotations

import sys
from collections import deque
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from src.shared.gpu.metal.present_port import (
    DEFAULT_BACKGROUND,
    MetalPresentPort,
    SurfacePublisher,
    SurfaceState,
    build_present_params,
    normalized_rect,
)


class _CommandBuffer:
    def __init__(self) -> None:
        self.error_value = None

    def error(self):
        return self.error_value


class _FakeSink:
    """Hands out a fixed number of drawables and plays the driver's part.

    A drawable goes back into the pool only once its command buffer completed
    *and* it reached the screen, in either order — as with a real layer.
    """

    pixel_format = 80

    def __init__(self, drawables: int = 3) -> None:
        self._free = [object() for _ in range(drawables)]
        self.outstanding: deque = deque()     # entries still out of the pool
        self.drawables_taken = 0

    def next_drawable(self):
        self.drawables_taken += 1
        return self._free.pop() if self._free else None

    def drawable_texture(self, drawable):
        return drawable

    def present(self, command_buffer, drawable) -> None:
        self.outstanding.append({"command_buffer": command_buffer, "drawable": drawable,
                                 "complete": False, "shown": False})

    def on_presented(self, drawable, callback) -> None:
        for entry in self.outstanding:
            if entry["drawable"] is drawable:
                entry["presented_callback"] = callback
                return
        raise AssertionError("drawable that was never handed out")

    def complete_next(self, error=None) -> None:
        for entry in self.outstanding:
            if not entry["complete"]:
                entry["complete"] = True
                entry["command_buffer"].error_value = error
                entry["command_buffer"].on_complete(entry["command_buffer"])
                self._recycle()
                return
        raise AssertionError("nothing is in flight")

    def show_next(self) -> None:
        """The oldest unshown drawable reached the screen."""
        for entry in self.outstanding:
            if not entry["shown"]:
                entry["shown"] = True
                callback = entry.pop("presented_callback", None)
                if callback is not None:
                    callback()
                self._recycle()
                return
        raise AssertionError("nothing is waiting for the screen")

    def _recycle(self) -> None:
        for entry in list(self.outstanding):
            if entry["complete"] and entry["shown"]:
                self.outstanding.remove(entry)
                self._free.append(entry["drawable"])


class _FakePresenter:
    def __init__(self) -> None:
        self.calls: list = []
        self.fail: Exception | None = None

    def draw(self, held, drawable, sink, *, on_complete, on_actual_present) -> None:
        if self.fail is not None:
            raise self.fail
        command_buffer = _CommandBuffer()
        command_buffer.on_complete = on_complete
        self.calls.append((held, drawable))
        sink.present(command_buffer, drawable)
        sink.on_presented(drawable, on_actual_present)


def _drain(sink: "_FakeSink", port: MetalPresentPort) -> None:
    """Let every outstanding drawable complete and reach the screen."""
    while sink.outstanding:
        if any(not entry["complete"] for entry in sink.outstanding):
            sink.complete_next()
        else:
            sink.show_next()
        port.poll()


def _visible(size=(1920, 1080)) -> SurfacePublisher:
    return SurfacePublisher(SurfaceState(generation=3, drawable_size=size, scale=1.0, visible=True))


def _frame(frame=1, generation=5, size=(1920, 1080), token=None):
    return SimpleNamespace(frame=frame, generation_id=generation, width=size[0],
                           height=size[1], scrub_token=token, native=object())


def _port(drawables=3, **kwargs):
    sink = _FakeSink(drawables)
    presenter = _FakePresenter()
    port = MetalPresentPort(sink, presenter, _visible(), **kwargs)
    return port, sink, presenter


class _Lease:
    def __init__(self) -> None:
        self.released: list[str] = []

    def __call__(self, frame, reason) -> None:
        self.released.append(reason)


class TestHappyPath:
    def test_frame_is_presented_and_kept_until_replaced(self) -> None:
        port, sink, presenter = _port()
        first, second = _frame(1), _frame(2)
        lease_first, lease_second = _Lease(), _Lease()
        shown: list = []
        assert port.submit(first, on_presented=shown.append, on_released=lease_first) is True
        assert port.submit(second, on_presented=shown.append, on_released=lease_second) is True

        assert [o.event for o in port.poll()] == ["presented", "presented"]
        assert shown == [first, second]
        assert len(presenter.calls) == 2
        # Nothing completed yet: both leases are still out.
        assert lease_first.released == [] and lease_second.released == []

        # Frame 1's own present finished and frame 2 already replaced it: the
        # GPU no longer reads frame 1's texture, so its lease goes back.
        sink.complete_next()
        assert [(e.frame, e.event, e.reason) for o in port.poll() if (e := o)] == [
            (first, "released", "replaced")]
        assert lease_first.released == ["replaced"]
        sink.complete_next()                 # frame 2 done and on screen
        assert port.poll() == []
        assert lease_second.released == []   # still the picture on screen
        assert port.displayed_frame is second
        assert port.held_leases == 1

    def test_completion_and_release_are_each_reported_once(self) -> None:
        port, sink, _presenter = _port()
        frames = [_frame(n) for n in range(6)]
        leases = [_Lease() for _ in frames]
        for index, (frame, lease) in enumerate(zip(frames, leases)):
            assert port.submit(frame, on_released=lease) is True
            if index % 2 == 1:               # the waiting queue holds two frames
                port.poll()
                _drain(sink, port)
        # Only the last frame stays on screen; every earlier one — including
        # the first of each pair shown together — was released exactly once.
        assert [lease.released for lease in leases] == [["replaced"]] * 5 + [[]]

    def test_actual_presentation_is_counted_separately(self) -> None:
        port, sink, _presenter = _port()
        port.submit(_frame(1))
        port.poll()
        assert port.stats["presented"] == 1
        assert port.stats["actually_presented"] == 0
        sink.complete_next()
        port.poll()
        assert port.stats["actually_presented"] == 0
        sink.show_next()
        port.poll()
        assert port.stats["actually_presented"] == 1


class TestRequestMatching:
    def test_stale_generation_is_retired_without_presenting(self) -> None:
        port, sink, presenter = _port()
        frame, lease = _frame(1, generation=4), _Lease()
        port.set_request(generation=5)
        port.submit(frame, on_released=lease)
        assert [(o.frame, o.event, o.reason) for o in port.poll()] == [
            (frame, "released", "stale_generation")]
        assert presenter.calls == []
        assert sink.drawables_taken == 0
        assert lease.released == ["stale_generation"]

    def test_superseded_scrub_token_is_retired(self) -> None:
        port, _sink, presenter = _port()
        frame = _frame(1, token=7)
        port.set_request(scrub_token=8)
        port.submit(frame)
        assert port.poll()[0].reason == "stale_scrub_token"
        assert presenter.calls == []
        port.set_request(scrub_token=7)
        current = _frame(2, token=7)
        port.submit(current)
        assert [o.event for o in port.poll()] == ["presented"]

    def test_wrong_render_size_is_retired(self) -> None:
        port, _sink, presenter = _port()
        port.set_request(render_size=(960, 540))
        port.submit(_frame(1, size=(1920, 1080)))
        assert port.poll()[0].reason == "stale_render_size"
        assert presenter.calls == []
        port.submit(_frame(2, size=(960, 540)))
        assert [o.event for o in port.poll()] == ["presented"]

    def test_past_deadline_is_retired(self) -> None:
        now = [1_000]
        port, _sink, presenter = _port(wall_ns=lambda: now[0])
        port.submit(_frame(1), deadline_ns=2_000)
        now[0] = 2_500
        assert port.poll()[0].reason == "past_deadline"
        assert presenter.calls == []
        assert port.stats["past_deadline"] == 1

    def test_hidden_surface_retires_everything(self) -> None:
        port, _sink, presenter = _port()
        port.publisher.publish(visible=False)
        port.submit(_frame(1))
        assert port.poll()[0].reason == "stale_surface_unavailable"
        assert presenter.calls == []


class TestLatestSubmissionWins:
    def test_a_newer_scrub_token_retires_waiting_frames_of_the_old_one(self) -> None:
        port, _sink, presenter = _port(drawables=0)
        port.submit(_frame(1, token=5))
        port.submit(_frame(2, token=6))         # queue full afterwards
        assert port.represent_displayed((0, 0, 1, 1)) is False
        # Free a drawable: frame 1 is stale now, frame 6's frame is shown.
        port._sink._free.append(object())
        outcomes = port.poll()
        assert [(o.frame.frame, o.event, o.reason) for o in outcomes] == [
            (1, "released", "stale_scrub_token"), (2, "presented", "")]

    def test_playback_frame_retires_a_waiting_scrub_frame(self) -> None:
        port, sink, presenter = _port()
        sink._free = []                         # no drawable for the moment
        port.submit(_frame(1, token=9))
        assert port.poll() == []
        sink._free.append(object())
        port.submit(_frame(2, token=None))      # playback resumed
        outcomes = port.poll()
        assert [(o.frame.frame, o.event, o.reason) for o in outcomes] == [
            (1, "released", "stale_scrub_token"), (2, "presented", "")]

    def test_a_later_frame_cannot_extend_an_earlier_deadline(self) -> None:
        now = [0]
        port, sink, _presenter = _port(wall_ns=lambda: now[0])
        sink._free = []
        port.submit(_frame(1), deadline_ns=100)
        port.poll()
        port.submit(_frame(2), deadline_ns=10_000)   # a much later deadline
        sink._free.append(object())
        now[0] = 500
        assert [(o.frame.frame, o.event, o.reason) for o in port.poll()] == [
            (1, "released", "past_deadline"), (2, "presented", "")]

    def test_tokens_are_read_from_the_descriptor_extra(self) -> None:
        port, _sink, _presenter = _port()
        frame = SimpleNamespace(frame=1, generation_id=5, width=1920, height=1080,
                                extra={"scrub_token": 4, "deadline_ns": 7})
        assert port.submit(frame) is True
        held = port._pending[0]
        assert held.scrub_token == 4 and held.deadline_ns == 7


class TestBackPressure:
    def test_no_drawable_keeps_the_frame_and_the_last_picture(self) -> None:
        port, sink, presenter = _port(drawables=1)
        first = _frame(1)
        port.submit(first)
        assert [o.event for o in port.poll()] == ["presented"]
        # The only drawable is out; frame 2 has to wait.
        second = _frame(2)
        lease = _Lease()
        port.submit(second, on_released=lease)
        assert port.poll() == []
        assert port.pending == 1
        assert port.displayed_frame is first
        assert lease.released == []
        sink.show_next()                    # shown, not yet completed: still out
        assert port.poll() == []
        assert port.pending == 1
        sink.complete_next()                # now the drawable is free again
        events = port.poll()
        assert [(o.frame, o.event, o.reason) for o in events] == [
            (second, "presented", ""), (first, "released", "replaced")]
        assert port.pending == 0
        assert len(presenter.calls) == 2

    def test_waiting_frame_expires_while_no_drawable_is_free(self) -> None:
        now = [0]
        port, sink, _presenter = _port(drawables=1, wall_ns=lambda: now[0])
        port.submit(_frame(1))
        port.poll()
        port.submit(_frame(2), deadline_ns=100)
        port.poll()
        now[0] = 500
        assert [(o.frame.frame, o.reason) for o in port.poll()] == [(2, "past_deadline")]
        assert port.pending == 0

    def test_full_queue_refuses_a_frame_without_touching_its_lease(self) -> None:
        port, sink, _presenter = _port(drawables=0, max_pending=1)
        port.submit(_frame(1))
        lease = _Lease()
        assert port.submit(_frame(2), on_released=lease) is False
        assert lease.released == []
        assert port.stats["dropped_full"] == 1
        assert port.pending == 1

    def test_in_flight_drawables_are_bounded(self) -> None:
        port, sink, presenter = _port(max_in_flight=2)
        for index in range(2):
            assert port.submit(_frame(index)) is True
        port.poll()
        assert len(presenter.calls) == 2     # nothing waits, but no third drawable
        assert port.in_flight == 2
        assert port.pending == 0
        sink.show_next()
        assert port.poll() == []             # completion is what frees a frame
        sink.complete_next()
        for index in (2, 3):
            assert port.submit(_frame(index)) is True
        port.poll()
        assert len(presenter.calls) == 3     # one drawable is still out
        _drain(sink, port)
        assert len(presenter.calls) == 4
        assert port.in_flight == 0


class TestFailures:
    def test_encode_failure_releases_the_frame_once(self) -> None:
        port, _sink, presenter = _port()
        presenter.fail = RuntimeError("encode failed")
        frame, lease = _frame(1), _Lease()
        port.submit(frame, on_released=lease)
        outcomes = port.poll()
        assert [(o.frame, o.event) for o in outcomes] == [(frame, "released")]
        assert outcomes[0].reason.startswith("present_failed:")
        assert lease.released == [outcomes[0].reason]
        assert port.poll() == []
        assert port.stats["failed"] == 1

    def test_gpu_error_reported_by_completion_releases_the_frame(self) -> None:
        port, sink, _presenter = _port()
        frame, lease = _frame(1), _Lease()
        port.submit(frame, on_released=lease)
        assert [o.event for o in port.poll()] == ["presented"]
        sink.complete_next(error="GPU fault")
        assert [(o.frame, o.event) for o in port.poll()] == [(frame, "released")]
        assert lease.released == ["present_failed:GPU fault"]
        assert port.displayed_frame is None

    def test_callback_failure_does_not_stop_the_port(self) -> None:
        port, sink, _presenter = _port()

        def explode(_frame, _reason) -> None:
            raise RuntimeError("viewer gone")

        port.submit(_frame(1), on_presented=lambda _f: 1 / 0, on_released=explode)
        assert [o.event for o in port.poll()] == ["presented"]
        sink.complete_next()
        port.submit(_frame(2))
        # Frame 2 is shown; frame 1 is released and its broken callback is logged.
        assert [o.event for o in port.poll()] == ["presented", "released"]
        assert port.displayed_frame.frame == 2


class TestRepeatedShowing:
    def test_resize_redraws_the_displayed_frame_and_keeps_its_lease(self) -> None:
        port, sink, presenter = _port()
        frame, lease = _frame(1), _Lease()
        port.submit(frame, on_released=lease)
        port.poll()
        sink.show_next()
        sink.complete_next()
        assert port.poll() == []

        assert port.represent_displayed((0.1, 0.1, 0.8, 0.8)) is True
        outcomes = port.poll()
        assert [(o.frame, o.event) for o in outcomes] == [(frame, "presented")]
        # The repeat draws the same texture while its command buffer is in
        # flight, so the frame may not be released yet.
        assert presenter.calls[-1][0].is_repeat
        assert lease.released == []
        assert port.held_leases == 1

        sink.complete_next()
        assert port.poll() == []
        newer = _frame(2)
        port.submit(newer)
        # Showing the newer frame releases the previous picture right away.
        assert [(o.frame, o.event, o.reason) for o in port.poll()] == [
            (newer, "presented", ""), (frame, "released", "replaced")]
        assert lease.released == ["replaced"]
        assert port.held_leases == 1

    def test_nothing_to_redraw_before_the_first_frame(self) -> None:
        port, _sink, _presenter = _port()
        assert port.represent_displayed((0, 0, 1, 1)) is False
        assert port.pending == 0

    def test_waiting_repeat_never_displaces_a_real_frame(self) -> None:
        port, sink, _presenter = _port(max_pending=1, drawables=0)
        port.submit(_frame(1))
        port.poll()                      # waits: no drawable
        assert port.represent_displayed((0, 0, 1, 1)) is False
        assert port.pending == 1
        assert not sink.outstanding

    def test_a_waiting_repeat_is_replaced_by_a_newer_one(self) -> None:
        port, sink, _presenter = _port()
        port.submit(_frame(1))
        port.poll()
        sink.complete_next()
        sink.show_next()
        port.poll()
        port.represent_displayed((0.1, 0.1, 0.8, 0.8))
        port.represent_displayed((0.2, 0.2, 0.6, 0.6))
        assert port.pending == 1


class TestShutdown:
    def test_shutdown_releases_everything_once(self) -> None:
        port, sink, _presenter = _port(max_in_flight=1)
        frames = [_frame(n) for n in range(3)]
        leases = [_Lease() for _ in frames]
        for frame, lease in zip(frames[:2], leases[:2]):
            assert port.submit(frame, on_released=lease) is True
        port.poll()                      # frame 0 in flight, frame 1 waiting
        assert port.submit(frames[2], on_released=leases[2]) is True
        assert [o.event for o in port.shutdown()] == ["released"] * 3
        assert all(lease.released == ["shutdown"] for lease in leases)
        assert port.shutdown() == []
        assert port.held_leases == 0 and port.pending == 0

    def test_surface_generation_advances_on_change_only(self) -> None:
        publisher = SurfacePublisher()
        assert publisher.publish(visible=True, drawable_size=(10, 10)).generation == 1
        assert publisher.publish(drawable_size=(10, 10)).generation == 1
        assert publisher.publish(drawable_size=(20, 10)).generation == 2
        assert publisher.publish(scale=2.0).generation == 3
        assert publisher.publish(visible=False, generation=40).generation == 40
        assert publisher.current().is_usable() is False


class TestParams:
    def test_rect_conversion_uses_bottom_left_origin(self) -> None:
        # A 16:9 picture letterboxed in a 200x100 surface: 32 px bars top/bottom.
        assert normalized_rect((0, 32, 200, 36), (200, 100)) == pytest.approx((0.0, 0.32, 1.0, 0.36))
        assert normalized_rect((50, 0, 100, 100), (200, 100)) == pytest.approx((0.25, 0.0, 0.5, 1.0))

    def test_params_pack_the_struct_layout(self) -> None:
        values = build_present_params((0.25, 0.5, 0.5, 0.25), flip_y=True)
        assert values.dtype.itemsize == 48
        assert tuple(values["rect"][0]) == (0.25, 0.5, 0.5, 0.25)
        assert tuple(values["background"][0]) == DEFAULT_BACKGROUND
        assert int(values["flip_y"][0]) == 1
        assert int(build_present_params((0, 0, 1, 1), flip_y=False)["flip_y"][0]) == 0

    @pytest.mark.parametrize("limits", [dict(max_in_flight=0), dict(max_pending=0)])
    def test_limits_must_be_positive(self, limits) -> None:
        with pytest.raises(ValueError):
            MetalPresentPort(_FakeSink(), publisher=_visible(), **limits)

    def test_unknown_request_field_is_rejected(self) -> None:
        port, _sink, _presenter = _port()
        with pytest.raises(TypeError):
            port.set_request(nonsense=1)
