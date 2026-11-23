"""Drawables are fetched off the owner's thread (plan 2.1; found on a real
machine with the display awake).

``CAMetalLayer.nextDrawable`` waits for the display to hand back a drawable
when none is free -- with ``allowsNextDrawableTimeout = False`` just as
without it.  The first version of the layer sink called it from the render
thread's poll loop, so every poll without a drawable stalled that thread for
about a second: in the real application the Edit preview managed one frame a
second while the native viewport was attached.  The sink now asks the layer
on a helper thread; the port treats a frame that waits too long for a
drawable like a stall, so the owner shows frames another way meanwhile.
"""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

from src.shared.gpu.metal.present_port import CAMetalLayerSink, MetalPresentPort, SurfacePublisher, SurfaceState


def _wait_until(condition, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.002)
    return condition()


class _BlockingLayer:
    """``nextDrawable`` holds the call until the test lets it answer."""

    def __init__(self, answer: object = None) -> None:
        self.answer = answer if answer is not None else object()
        self.gate = threading.Event()
        self.calls = 0
        self.answered = threading.Event()

    def nextDrawable(self):
        self.calls += 1
        self.gate.wait(10.0)
        drawable, self.answer = self.answer, None      # nil afterwards, like an exhausted layer
        self.answered.set()
        return drawable


class TestLayerSink:
    def test_next_drawable_does_not_wait_for_the_layer(self) -> None:
        layer = _BlockingLayer()                       # never answered while we ask
        sink = CAMetalLayerSink(layer, 80)
        started = time.monotonic()
        assert sink.next_drawable() is None
        assert time.monotonic() - started < 0.2        # a blocking call took ~1 s
        assert layer.calls == 1                        # but the layer was asked

    def test_the_drawable_is_handed_over_once_it_arrives(self) -> None:
        layer = _BlockingLayer()
        sink = CAMetalLayerSink(layer, 80)
        assert sink.next_drawable() is None
        assert sink.next_drawable() is None            # still waiting: asked once only
        assert layer.calls == 1
        layer.gate.set()
        got: list = []
        assert _wait_until(lambda: (got.append(sink.next_drawable()), got[-1] is not None)[1])
        assert layer.calls == 1                        # that was the layer's only drawable
        assert sink.next_drawable() is None

    def test_the_layer_is_asked_again_only_after_it_answered(self) -> None:
        layer = _BlockingLayer()
        sink = CAMetalLayerSink(layer, 80)
        for _ in range(50):                            # a frame every 20 ms for a second
            assert sink.next_drawable() is None
            time.sleep(0.02)
        assert layer.calls == 1                        # one request in flight
        layer.gate.set()
        assert _wait_until(lambda: sink.next_drawable() is not None)
        assert sink.next_drawable() is None            # the layer answers nil now
        assert _wait_until(lambda: layer.calls >= 2)   # and is asked again

    def test_an_untaken_drawable_is_given_back(self) -> None:
        layer = _BlockingLayer()
        sink = CAMetalLayerSink(layer, 80)
        assert sink.next_drawable() is None
        layer.gate.set()
        assert _wait_until(layer.answered.is_set)
        time.sleep(0.02)                              # let the helper thread store it
        sink.discard_ready()                          # no frame will use it
        assert sink.next_drawable() is None           # gone; a new request went out
        assert layer.calls == 2

    def test_close_stops_the_requests_and_drops_a_late_answer(self) -> None:
        layer = _BlockingLayer()
        sink = CAMetalLayerSink(layer, 80)
        assert sink.next_drawable() is None
        sink.close()
        layer.gate.set()                              # the layer answers after the close
        assert _wait_until(layer.answered.is_set)
        time.sleep(0.02)
        assert sink.next_drawable() is None           # nothing is kept or asked
        assert layer.calls == 1
        assert sink._layer is None

    def test_a_layer_that_answers_nil_never_blocks_the_caller(self) -> None:
        layer = _BlockingLayer()
        layer.answer, layer.gate = None, threading.Event()
        layer.gate.set()                              # answers nil at once
        sink = CAMetalLayerSink(layer, 80)
        started = time.monotonic()
        for _ in range(20):
            assert sink.next_drawable() is None
        assert time.monotonic() - started < 0.2
        assert _wait_until(lambda: layer.calls >= 1)  # the layer was still asked


# ---------------------------------------------------------------------------
# The port
# ---------------------------------------------------------------------------


class _Clock:
    def __init__(self, start: float = 100.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _Sink:
    """Hands out what it was given; records what the port gives back."""

    pixel_format = 80

    def __init__(self, *drawables) -> None:
        self.drawables = list(drawables)
        self.discarded = 0
        self.closed = False
        self.presented: list = []

    def next_drawable(self):
        return self.drawables.pop(0) if self.drawables else None

    def discard_ready(self) -> None:
        self.discarded += 1
        self.drawables.clear()

    def close(self) -> None:
        self.closed = True
        self.drawables.clear()

    @staticmethod
    def drawable_texture(drawable):
        return drawable

    def present(self, command_buffer, drawable) -> None:
        pass

    def on_presented(self, drawable, callback) -> None:
        pass


class _Presenter:
    def __init__(self) -> None:
        self.committed: list = []

    def draw(self, held, drawable, sink, *, on_complete, on_actual_present) -> None:
        command_buffer = SimpleNamespace(error=lambda: None, on_complete=on_complete)
        self.committed.append((held.frame, drawable))


def _frame(frame=1, generation=5):
    return SimpleNamespace(frame=frame, generation_id=generation, width=1920,
                           height=1080, scrub_token=None, native=object())


def _port(*drawables, **kwargs):
    sink = _Sink(*drawables)
    presenter = _Presenter()
    publisher = SurfacePublisher(SurfaceState(generation=3, drawable_size=(1920, 1080), scale=1.0, visible=True))
    port = MetalPresentPort(sink, presenter, publisher, **kwargs)
    return port, sink, presenter


class TestStarvation:
    def test_a_frame_that_waits_too_long_for_a_drawable_stalls_the_port(self) -> None:
        clock = _Clock()
        port, sink, presenter = _port(stall_timeout_s=1.0, clock=clock)   # a dry sink
        lease = []
        port.submit(_frame(1), on_released=lambda frame, reason: lease.append(reason))
        port.poll()
        assert port.pending == 1 and port.stalled is False
        assert presenter.committed == []               # the frame is kept, nothing shown
        clock.advance(0.6)
        port.poll()
        assert port.stalled is False
        clock.advance(0.6)                            # beyond the timeout
        port.poll()
        assert port.stalled is True                   # the owner shows frames another way
        assert port.stats["starved"] == 1
        port.poll()
        assert port.stats["starved"] == 1             # one episode
        assert lease == []                            # the frame itself is still the port's

    def test_the_port_recovers_when_a_drawable_turns_up(self) -> None:
        clock = _Clock()
        port, sink, presenter = _port(stall_timeout_s=1.0, clock=clock)
        port.submit(_frame(1))
        port.poll()
        clock.advance(1.5)
        port.poll()
        assert port.stalled is True
        sink.drawables.append(object())
        outcomes = port.poll()
        assert [outcome.event for outcome in outcomes] == ["presented"]
        assert port.stalled is False and port.pending == 0
        assert port.stats["starved"] == 1              # did not count a second episode

    def test_nothing_waiting_is_not_a_stall(self) -> None:
        clock = _Clock()
        port, sink, _presenter = _port(stall_timeout_s=1.0, clock=clock)
        clock.advance(50.0)
        assert port.poll() == [] and port.stalled is False
        assert port.stats["starved"] == 0

    def test_a_frame_that_got_its_drawable_resets_the_wait(self) -> None:
        clock = _Clock()
        port, sink, presenter = _port(stall_timeout_s=1.0, clock=clock)
        port.submit(_frame(1))
        port.poll()                                    # waits: no drawable
        clock.advance(0.8)
        sink.drawables.append(object())
        port.poll()                                    # presented at 0.8 s
        assert port.stalled is False and port.pending == 0
        port.submit(_frame(2))
        clock.advance(0.8)                             # a fresh wait, not a long one
        port.poll()
        assert port.pending == 1 and port.stalled is False

    def test_an_untaken_drawable_is_given_back_when_no_frame_waits(self) -> None:
        port, sink, presenter = _port()
        assert port.poll() == []
        assert sink.discarded >= 1                     # nothing waits: nothing is kept
        sink.drawables.append(object())
        assert port.submit(_frame(1)) is True
        assert [outcome.event for outcome in port.poll()] == ["presented"]

    def test_shutdown_closes_the_sink(self) -> None:
        port, sink, _presenter = _port()
        port.shutdown()
        assert sink.closed is True

    def test_a_sink_without_the_new_methods_still_works(self) -> None:
        """Sinks that fetch synchronously need neither of the two methods."""
        class _Old:
            pixel_format = 80

            def __init__(self) -> None:
                self.free = [object()]

            def next_drawable(self):
                return self.free.pop() if self.free else None

            drawable_texture = staticmethod(lambda drawable: drawable)

            def present(self, command_buffer, drawable) -> None:
                pass

            def on_presented(self, drawable, callback) -> None:
                pass

        sink = _Old()
        publisher = SurfacePublisher(SurfaceState(generation=3, drawable_size=(1920, 1080), scale=1.0, visible=True))
        port = MetalPresentPort(sink, _Presenter(), publisher)
        port.submit(_frame(1))
        assert [outcome.event for outcome in port.poll()] == ["presented"]
        port.submit(_frame(2))                        # waits for the only drawable
        port.poll()
        assert port.pending == 1
        port.shutdown()
        assert port.pending == 0
