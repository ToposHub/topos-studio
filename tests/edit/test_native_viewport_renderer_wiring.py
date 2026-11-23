"""Native viewport wiring of the preview renderer (Metal migration plan 2.2).

The renderer is the seam between two threads: the GUI thread asks for the
native viewport and hands over frames the presenter accepted; the render
thread builds the Metal port and the frame producer, publishes native frames
when their GPU work completes, and returns slots when the viewport is done
with them.  These tests drive that seam with fakes; the real Metal path is
covered on the device by ``test_native_metal_preview_producer.py`` and the
end-to-end pixel comparison in ``test_native_viewport_end_to_end.py``.
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("PyQt6.QtWidgets")

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.features.edit.core.edit_gpu_preview_renderer import EditGpuPreviewRenderer, EditRenderTask
from src.features.edit.core.native_metal_preview import (
    DROPPED, FALLBACK, SUBMITTED, FailedFrame, PublishedFrame,
)
from src.shared.gpu.metal.present_port import PresentOutcome, SurfacePublisher
from src.shared.gpu.present_frame import PresentLeaseKey


class _FakePort:
    def __init__(self) -> None:
        self.requests: list = []
        self.submits: list = []
        self.outcomes: list = []
        self.shutdown_outcomes: list = []
        self.shutdown_calls = 0
        self.represented = None
        self.stalled = False
        self.in_flight = 0
        self.pending = 0
        self.stats = {"stalled": 0}

    def set_request(self, **changes) -> None:
        self.requests.append(changes)

    def submit(self, frame, **kwargs) -> bool:
        self.submits.append((frame, kwargs))
        return True

    def represent_displayed(self, rect) -> bool:
        self.represented = rect
        return True

    def poll(self):
        outcomes, self.outcomes = self.outcomes, []
        return outcomes

    def shutdown(self):
        self.shutdown_calls += 1
        outcomes, self.shutdown_outcomes = self.shutdown_outcomes, []
        return outcomes


class _FakeProducer:
    def __init__(self, outcome=SUBMITTED) -> None:
        self.enabled = True
        self.in_flight = 0
        self.paused_reason = None
        self.outcome = outcome
        self.rendered: list = []
        self.composites: list = []
        self.acked: list = []
        self.polled = 0
        self.shutdown_calls = 0
        self.poll_result = ([], [])

    def try_render(self, task, size, source, lut):
        self.rendered.append((task, size, source, lut))
        return self.outcome

    def try_composite(self, task, size, plan, coordinate_size):
        self.composites.append((task, size, plan, coordinate_size))
        return self.outcome

    def poll(self):
        self.polled += 1
        result, self.poll_result = self.poll_result, ([], [])
        return result

    def ack(self, present, reason=""):
        self.acked.append((present, reason))

    def shutdown(self):
        self.shutdown_calls += 1
        return True


class _VtFrame:
    pixel_buffer = object()
    pixel_format = "nv12"
    bit_depth = 8
    color_matrix = "bt709"
    color_range = "limited"
    color_primaries = None
    color_transfer = None
    width, height = 1920, 1080

    def __init__(self) -> None:
        self.releases = 0

    def release(self) -> None:
        self.releases += 1


def _task(source=None, frame: int = 25) -> EditRenderTask:
    layer = {"frame_type": "vt_pixel_buffer", "vt_pixel_buffer": source,
             "track_info": {}}
    return EditRenderTask(frame=frame, generation_id=3, layers=[layer],
                          output_fbo_size=(960, 540))


@pytest.fixture()
def renderer(qapp):
    made = EditGpuPreviewRenderer(main_context=None)
    yield made


@pytest.fixture()
def native(renderer):
    """The renderer with a fake port and producer, as the render thread would have."""
    renderer._metal_preview_mode = "all"
    renderer._native_port = _FakePort()
    renderer._native_producer = _FakeProducer()
    return renderer


class TestAttachAndDetach:
    def test_off_by_default_and_refused_without_opt_in(self, renderer) -> None:
        assert renderer.native_viewport_opted_in() is False
        failures: list = []
        renderer.native_viewport_failed.connect(failures.append)
        renderer.attach_native_viewport(object(), SurfacePublisher())
        renderer._apply_native_viewport_request(renderer._native_viewport_request)
        assert failures == ["native_viewport_disabled"]
        assert renderer.native_viewport_active() is False

    def test_refused_when_the_metal_preview_is_off(self, renderer) -> None:
        renderer._native_viewport_opt_in = True
        renderer._metal_preview_mode = "off"
        failures: list = []
        renderer.native_viewport_failed.connect(failures.append)
        renderer._apply_native_viewport_request(("attach", object(), SurfacePublisher()))
        assert failures == ["native_viewport_disabled"]
        assert renderer._native_port is None and renderer._native_producer is None

    def test_a_broken_layer_is_reported_not_raised(self, renderer) -> None:
        renderer._native_viewport_opt_in = True
        renderer._metal_preview_mode = "all"
        failures: list = []
        renderer.native_viewport_failed.connect(failures.append)
        # No GL context here: building the Metal preview raises.
        renderer._apply_native_viewport_request(("attach", object(), SurfacePublisher()))
        assert len(failures) == 1 and failures[0]
        assert renderer._native_port is None and renderer._native_producer is None

    def test_detach_returns_when_nothing_was_built(self, renderer) -> None:
        assert renderer.detach_native_viewport(timeout_s=0.1) is True
        assert renderer._native_viewport_request is None

    def test_a_stopped_render_thread_cannot_hold_the_layer(self, native) -> None:
        """No thread will answer: settle at once instead of waiting."""
        native._native_port = _FakePort()        # a port the thread left behind
        assert native.isRunning() is False
        assert native.detach_native_viewport(timeout_s=5.0) is True
        assert native._native_viewport_request is None
        assert native._native_port is None

    def test_detach_runs_on_the_render_thread_and_confirms(self, native) -> None:
        released = SimpleNamespace(frame=1)
        native._native_port.shutdown_outcomes = [PresentOutcome(released, "released", "shutdown")]
        producer = native._native_producer
        native.isRunning = lambda: True                 # a live render thread
        native.detach_native_viewport(timeout_s=0.0)   # only queues the request
        done = native._native_viewport_request[1]
        assert not done.is_set()
        native._apply_native_viewport_request(native._native_viewport_request)
        assert done.is_set()
        assert native._native_port is None and native._native_producer is None
        # The frame the viewport still held went back to the producer.
        assert producer.acked == [(released, "shutdown")]


class TestHandingFramesOver:
    def test_refused_without_a_viewport(self, renderer) -> None:
        present = SimpleNamespace(generation_id=1, width=4, height=4, extra={})
        assert renderer.submit_native_present(present, rect=(0, 0, 1, 1),
                                              display_rect_px=(0, 0, 4, 4)) is False

    def test_refused_while_a_viewport_request_is_pending(self, native) -> None:
        native._native_viewport_request = ("attach", object(), object())
        present = SimpleNamespace(generation_id=1, width=4, height=4, extra={})
        assert native.submit_native_present(present, rect=(0, 0, 1, 1),
                                            display_rect_px=(0, 0, 4, 4)) is False
        assert native._native_port.submits == []

    def test_forwarded_with_its_rectangle_and_callbacks(self, native) -> None:
        released = lambda frame, reason: None  # noqa: E731
        present = SimpleNamespace(generation_id=5, width=1920, height=1080,
                                  extra={"scrub_token": 9, "deadline_ns": 123})
        assert native.submit_native_present(
            present, rect=(0.0, 0.1, 1.0, 0.8), display_rect_px=(0, 54, 1920, 972),
            on_released=released) is True
        (frame, kwargs), = native._native_port.submits
        assert frame is present
        assert kwargs["rect"] == (0.0, 0.1, 1.0, 0.8)
        assert kwargs["display_rect_px"] == (0, 54, 1920, 972)
        assert kwargs["on_released"] is released
        assert kwargs["on_presented"] == native._note_native_frame_presented

    def test_represent_reaches_the_port(self, native) -> None:
        assert native.represent_native_viewport((0.1, 0.1, 0.8, 0.8)) is True
        assert native._native_port.represented == (0.1, 0.1, 0.8, 0.8)
        native._native_port = None
        assert native.represent_native_viewport((0, 0, 1, 1)) is False

    def test_generation_changes_reach_the_port(self, native) -> None:
        native.set_generation_id(7)
        assert native._native_port.requests == [{"generation": 7}]
        assert native._native_request_generation == 7
        # Older generations are rejected by the renderer itself.
        native.set_generation_id(6)
        assert len(native._native_port.requests) == 1


class TestTakingAFrame:
    def test_no_producer_or_disabled_producer_leaves_the_frame_alone(self, renderer, native) -> None:
        source = _VtFrame()
        task = _task(source)
        native._native_producer = None
        assert native._try_native_metal_preview(task, 960, 540) is False
        native._native_producer = _FakeProducer()
        native._native_producer.enabled = False
        assert native._try_native_metal_preview(task, 960, 540) is False
        assert task.layers[0]["vt_pixel_buffer"] is source and source.releases == 0

    @pytest.mark.parametrize("background,cached,snapshot", [
        (True, False, False), (False, True, False), (False, False, True)])
    def test_special_tasks_stay_on_the_gl_path(self, native, background, cached, snapshot) -> None:
        source = _VtFrame()
        task = _task(source)
        task.background, task.cached_composite = background, cached
        native._snapshot_armed = snapshot
        assert native._try_native_metal_preview(task, 960, 540) is False
        assert native._native_producer.rendered == []

    def test_ineligible_frame_is_left_to_the_gl_path(self, native) -> None:
        source = _VtFrame()
        task = _task(source)
        task.layers[0]["frame_type"] = "planar"
        assert native._try_native_metal_preview(task, 960, 540) is False
        assert task.layers[0]["vt_pixel_buffer"] is source

    def test_fallback_gives_the_frame_back(self, native) -> None:
        source = _VtFrame()
        task = _task(source)
        native._native_producer.outcome = FALLBACK
        assert native._try_native_metal_preview(task, 960, 540) is False
        assert task.layers[0]["vt_pixel_buffer"] is source
        assert native._task_used_io_surface_source is False

    def test_submitted_frame_is_taken_with_the_latest_render_size(self, native) -> None:
        source = _VtFrame()
        task = _task(source, frame=42)
        assert native._try_native_metal_preview(task, 960, 540) is True
        (rendered_task, size, rendered_source, lut), = native._native_producer.rendered
        assert (rendered_task, size, rendered_source, lut) == (task, (960, 540), source, None)
        assert task.layers[0]["vt_pixel_buffer"] is None   # owned by the transaction now
        assert native._task_used_io_surface_source is True
        assert native._native_port.requests[-1] == {"render_size": (960, 540)}

    def test_dropped_frame_is_reported(self, native) -> None:
        source = _VtFrame()
        errors: list = []
        native.render_error.connect(lambda frame, message: errors.append((frame, message)))
        native._native_producer.outcome = DROPPED
        assert native._try_native_metal_preview(_task(source, frame=7), 960, 540) is True
        assert errors == [(7, "native_preview_dropped")]

    def test_composite_frames_carry_every_source_into_the_transaction(self, native) -> None:
        """Plan 2.6: a cross dissolve is two layers with rewritten opacities."""
        first, second = _VtFrame(), _VtFrame()
        task = _task(first, frame=51)
        task.layers.append({"frame_type": "vt_pixel_buffer", "vt_pixel_buffer": second,
                            "track_info": {"opacity": 0.65}})
        task.layers[0]["track_info"] = {"opacity": 0.35}
        native._active_transform_coordinate_size = (1920, 1080)
        errors: list = []
        native.render_error.connect(lambda frame, message: errors.append(message))

        assert native._try_native_metal_preview(task, 960, 540) is True

        (plan_task, size, plan, coordinate), = native._native_producer.composites
        assert (plan_task, size, coordinate) == (task, (960, 540), (1920, 1080))
        assert [(layer.opacity, layer.kind) for layer in plan.layers] == [(0.35, "nv12"),
                                                                          (0.65, "nv12")]
        assert plan.layers[0].source is first and plan.layers[1].source is second
        # Both frames belong to the frame transaction now; it releases them.
        assert [layer["vt_pixel_buffer"] for layer in task.layers] == [None, None]
        assert (first.releases, second.releases) == (0, 0)
        assert native._native_producer.rendered == [] and errors == []

    def test_composite_fallback_gives_every_frame_back(self, native) -> None:
        first, second = _VtFrame(), _VtFrame()
        task = _task(first)
        task.layers.append({"frame_type": "vt_pixel_buffer", "vt_pixel_buffer": second,
                            "track_info": {"opacity": 0.5}})
        native._active_transform_coordinate_size = (960, 540)
        native._native_producer.outcome = FALLBACK

        assert native._try_native_metal_preview(task, 960, 540) is False

        assert [layer["vt_pixel_buffer"] for layer in task.layers] == [first, second]
        assert (first.releases, second.releases) == (0, 0)
        assert native._task_used_io_surface_source is False

    def test_composite_dropped_frame_is_reported_with_both_frames_taken(self, native) -> None:
        first, second = _VtFrame(), _VtFrame()
        task = _task(first, frame=9)
        task.layers.append({"frame_type": "vt_pixel_buffer", "vt_pixel_buffer": second,
                            "track_info": {"opacity": 0.5}})
        native._active_transform_coordinate_size = (960, 540)
        errors: list = []
        native.render_error.connect(lambda frame, message: errors.append((frame, message)))
        native._native_producer.outcome = DROPPED

        assert native._try_native_metal_preview(task, 960, 540) is True

        assert errors == [(9, "native_preview_dropped")]
        assert [layer["vt_pixel_buffer"] for layer in task.layers] == [None, None]

    def test_a_failing_composite_gives_every_frame_back_and_raises(self, native) -> None:
        first, second = _VtFrame(), _VtFrame()
        task = _task(first)
        task.layers.append({"frame_type": "vt_pixel_buffer", "vt_pixel_buffer": second,
                            "track_info": {"opacity": 0.5}})
        native._active_transform_coordinate_size = (960, 540)

        def explode(*_args):
            raise RuntimeError("encode failed")

        native._native_producer.try_composite = explode
        with pytest.raises(RuntimeError, match="encode failed"):
            native._try_native_metal_preview(task, 960, 540)
        assert [layer["vt_pixel_buffer"] for layer in task.layers] == [first, second]
        assert (first.releases, second.releases) == (0, 0)

    @pytest.mark.parametrize("mode", ["lut", "off"])
    def test_composites_need_the_explicit_opt_in(self, native, mode: str) -> None:
        first, second = _VtFrame(), _VtFrame()
        task = _task(first)
        task.layers.append({"frame_type": "vt_pixel_buffer", "vt_pixel_buffer": second,
                            "track_info": {"opacity": 0.5}})
        native._metal_preview_mode = mode

        assert native._try_native_metal_preview(task, 960, 540) is False

        assert native._native_producer.composites == []
        assert [layer["vt_pixel_buffer"] for layer in task.layers] == [first, second]

    def test_a_failing_producer_gives_the_frame_back_and_raises(self, native) -> None:
        source = _VtFrame()
        task = _task(source)

        def explode(*_args):
            raise RuntimeError("encode failed")

        native._native_producer.try_render = explode
        with pytest.raises(RuntimeError, match="encode failed"):
            native._try_native_metal_preview(task, 960, 540)
        assert task.layers[0]["vt_pixel_buffer"] is source
        assert source.releases == 0


class TestRenderThreadLoop:
    def test_published_frames_reach_the_presenter_and_the_state_machine(self, native) -> None:
        presents: list = []
        legacy: list = []
        native.present_frame_ready.connect(presents.append)
        native.frame_ready.connect(lambda *args: legacy.append(args))
        present = SimpleNamespace(slot_id=2, width=960, height=540)
        task = _task(frame=77)
        native._native_producer.poll_result = ([PublishedFrame(present, task, 1.5)], [])
        native._poll_native_viewport()
        assert presents == [present]
        assert legacy == [(77, 3, 0, 960, 540)]

    def test_failed_frames_are_reported_and_a_pause_is_announced(self, native) -> None:
        errors: list = []
        failures: list = []
        native.render_error.connect(lambda frame, message: errors.append((frame, message)))
        native.native_viewport_failed.connect(failures.append)
        native._native_producer.poll_result = ([], [FailedFrame(_task(frame=5), "GPU fault")])
        native._poll_native_viewport()
        assert errors == [(5, "GPU fault")]
        assert failures == []               # not paused: the next frame retries

        native._native_producer.paused_reason = "completion_timeout:sequences=[0]"
        native._native_producer.poll_result = ([], [FailedFrame(_task(frame=6), "x")])
        native._poll_native_viewport()
        assert errors[-1] == (6, "x")
        assert failures == ["completion_timeout:sequences=[0]"]

    def test_released_viewport_frames_free_their_slots(self, native) -> None:
        frame = SimpleNamespace(frame=9)
        native._native_port.outcomes = [
            PresentOutcome(frame, "released", "replaced"),
            PresentOutcome(SimpleNamespace(frame=10), "presented"),
        ]
        native._poll_native_viewport()
        assert [(item[0], item[1]) for item in native._native_producer.acked] == [(frame, "replaced")]

    def test_a_broken_port_does_not_stop_the_loop(self, native) -> None:
        def explode():
            raise RuntimeError("device lost")

        native._native_port.poll = explode
        native._poll_native_viewport()      # must not raise
        assert native._native_producer.polled == 1

    def test_unclaimed_frames_are_freed_by_the_render_thread(self, native) -> None:
        """The viewer may ask from its own thread; the slot pool is the
        render thread's, so the release happens in the next poll."""
        frame = SimpleNamespace(frame=8, slot_id=1)
        acked: list = []
        native.slot_acked.connect(acked.append)
        native.release_native_frame(frame, "viewport_queue_refused")     # any thread
        native.release_native_frame(frame, "again")                      # coalesced
        assert native._native_producer.acked == []                        # nothing yet
        native._poll_native_viewport()
        assert [(f, r) for f, r in native._native_producer.acked] == [
            (frame, "viewport_queue_refused"), (frame, "again")]
        assert acked == [1, 1]                 # the presenter sees the protocol end

    def test_a_stalled_present_moves_new_frames_to_the_gl_path(self, native) -> None:
        """The display stopped completing presents: report it once, stop
        feeding frames to the native path, resume when completions return."""
        failures: list = []
        native.native_viewport_failed.connect(failures.append)
        port = native._native_port
        source = _VtFrame()
        task = _task(source)

        port.stalled = True
        native._poll_native_viewport()
        native._poll_native_viewport()
        assert len(failures) == 1 and failures[0].startswith("present_stalled:")
        # The frame is left whole for the GL path.
        assert native._try_native_metal_preview(task, 960, 540) is False
        assert task.layers[0]["vt_pixel_buffer"] is source
        assert native._native_producer.rendered == []

        port.stalled = False
        native._poll_native_viewport()
        assert len(failures) == 1               # recovery is silent
        assert native._try_native_metal_preview(task, 960, 540) is True

    def test_presented_frames_are_counted(self, native) -> None:
        shown: list = []
        native.native_frame_presented.connect(shown.append)
        native._note_native_frame_presented(SimpleNamespace(frame=31))
        assert shown == [31]


class TestShutdown:
    def test_exit_releases_viewport_then_producer(self, native) -> None:
        frame = SimpleNamespace(frame=1)
        producer = native._native_producer
        native._native_port.shutdown_outcomes = [PresentOutcome(frame, "released", "shutdown")]
        native._shutdown_native_viewport()
        assert native._native_port is None and native._native_producer is None
        assert producer.acked == [(frame, "shutdown")]
        assert producer.shutdown_calls == 1

    def test_exit_tolerates_a_failing_port(self, native) -> None:
        def explode():
            raise RuntimeError("already gone")

        producer = native._native_producer
        native._native_port.shutdown = explode
        native._shutdown_native_viewport()
        assert producer.shutdown_calls == 1


class TestLeaseKeyOnNativeDescriptor:
    def test_key_is_exposed_for_routing(self) -> None:
        key = PresentLeaseKey(pool_generation=2, slot_id=3, submission_id=4)
        assert (key.pool_generation, key.slot_id, key.submission_id) == (2, 3, 4)
