"""Source Viewer routing for the native Metal viewport (plan task 2.5).

The Source Viewer shows one asset, has no overlays above the picture and
hands frames over by a tuple protocol: the controller pairs each present
descriptor through ``SourcePresentGate`` and passes only the slot id to the
widget.  A native frame lives in the render thread's Metal slot pool, so it
takes a different route — a single latest-wins slot in the controller and
``set_native_gpu_frame`` on the widget — and is returned to the render
thread whenever it is not shown.  The gate's key-less ack must never see it.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("PyQt6.QtWidgets")

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtGui import QImage
from PyQt6.QtWidgets import QWidget

from src.features.edit.ui.preview_view_transform import PreviewViewTransform
from src.features.edit.ui.source_viewer import SourceViewer, SourceViewerWidget
from src.shared.gpu.metal.present_port import normalized_rect
from src.shared.gpu.present_frame import (
    GpuPresentFrame,
    NativePresentDescriptor,
    PresentLeaseKey,
)

FRAME_W, FRAME_H = 1920, 1080


class _FakeRenderer(QObject):
    native_viewport_failed = pyqtSignal(str)
    native_frame_presented = pyqtSignal(int)

    def __init__(self, *, opted_in: bool = True) -> None:
        super().__init__()
        self.opted_in = opted_in
        self.active = False
        self.attached: list = []
        self.detached = 0
        self.submits: list = []
        self.released: list = []
        self.wanted: list = []

    def native_viewport_opted_in(self) -> bool:
        return self.opted_in

    def native_viewport_active(self) -> bool:
        return self.active

    def attach_native_viewport(self, layer, publisher) -> None:
        self.attached.append((layer, publisher))
        self.active = True

    def detach_native_viewport(self, timeout_s: float = 2.0) -> bool:
        self.detached += 1
        self.active = False
        return True

    def submit_native_present(self, present, **kwargs) -> bool:
        self.submits.append((present, kwargs))
        return True

    def release_native_frame(self, present, reason: str = "unclaimed") -> None:
        self.released.append((present, reason))

    def set_native_frames_wanted(self, wanted: bool) -> None:
        self.wanted.append(bool(wanted))


class _FakeHost(QWidget):
    def __init__(self, parent) -> None:
        super().__init__(parent)
        self.overlay_images: list = []
        self.detached = False
        self.hide()

    @property
    def drawable_size(self) -> tuple:
        scale = self.devicePixelRatioF()
        return max(1, round(self.width() * scale)), max(1, round(self.height() * scale))

    def set_overlay_image(self, image) -> None:
        self.overlay_images.append(image)

    def detach(self) -> None:
        self.detached = True


def _present(frame: int = 40, slot: int = 2, generation: int = 6) -> GpuPresentFrame:
    return GpuPresentFrame(
        texture_id=0, width=FRAME_W, height=FRAME_H, frame=frame,
        generation_id=generation, slot_id=slot,
        lease_key=PresentLeaseKey(pool_generation=9, slot_id=slot, submission_id=1),
        extra={"render_backend": "metal"},
        native=NativePresentDescriptor(
            api="metal", device_identity="metal:Test GPU:1", resource=object(),
            resource_format="bgra8unorm", width=FRAME_W, height=FRAME_H),
    )


@pytest.fixture()
def widget(qapp):
    made = SourceViewerWidget()
    made.resize(800, 520)
    made.show()
    qapp.processEvents()
    made._gpu_generation = 6
    made._gpu_expected_frame = 40
    yield made
    made._native_renderer = None
    made.close()
    qapp.processEvents()


def _arm(widget, renderer: _FakeRenderer) -> _FakeHost:
    host = _FakeHost(widget)
    widget._metal_host = host
    widget._native_renderer = renderer
    renderer.active = True
    widget.viewer.installEventFilter(widget)
    renderer.native_viewport_failed.connect(widget._on_native_viewport_failed)
    renderer.native_frame_presented.connect(widget._on_native_frame_presented)
    widget._layout_native_host()
    return host


class TestEnabling:
    def test_off_without_opt_in(self, widget) -> None:
        assert widget.enable_native_viewport(_FakeRenderer(opted_in=False)) is False
        assert widget.native_viewport_enabled() is False
        assert widget.enable_native_viewport(None) is False

    def test_a_refused_layer_keeps_the_gl_canvas(self, widget, qapp) -> None:
        if qapp.platformName() == "cocoa":
            pytest.skip("the real layer attaches under Cocoa")
        assert widget.enable_native_viewport(_FakeRenderer()) is False
        assert widget.native_viewport_enabled() is False


class TestRouting:
    def test_native_frame_without_a_viewport_is_not_shown(self, widget) -> None:
        assert widget.set_native_gpu_frame(_present(), frame=40, generation=6) is False

    def test_accepted_frame_is_handed_over_and_the_layer_is_shown(self, widget) -> None:
        renderer = _FakeRenderer()
        host = _arm(widget, renderer)
        assert host.isHidden()
        widget._gpu_displayed_slot_id = 5          # a GL frame was on display
        acked: list = []
        widget._gpu_slot_ack_callback = acked.append
        widget._gpu_pending_frame = 40
        widget._gpu_watchdog_timer.start()

        assert widget.set_native_gpu_frame(_present(frame=40), frame=40, generation=6) is True

        (submitted, kwargs), = renderer.submits
        assert submitted.is_native
        rect = kwargs["rect"]
        # 16:9 picture letterboxed in the canvas; aspect kept.
        assert 0.0 <= rect[0] and rect[0] + rect[2] <= 1.0 + 1e-6
        assert abs((rect[2] * host.drawable_size[0]) / (rect[3] * host.drawable_size[1])
                   - FRAME_W / FRAME_H) < 0.01
        assert not host.isHidden()
        assert host.geometry() == widget.viewer.geometry()
        # The GL slot it replaced is freed; the native one is the render thread's.
        assert acked == [5]
        assert widget._gpu_displayed_slot_id == -1
        assert widget._last_good_frame == 40
        assert not widget._gpu_watchdog_timer.isActive()
        assert widget._gpu_pending_frame == -1
        assert renderer.released == []

    def test_the_rectangle_is_the_canvas_fit_in_pixels(self, widget) -> None:
        renderer = _FakeRenderer()
        host = _arm(widget, renderer)
        viewer = widget.viewer
        viewer._video_aspect_ratio = 4 / 3           # from media metadata
        viewer._view_transform.reset_fit()
        widget.set_native_gpu_frame(_present(frame=40), frame=40, generation=6)
        (_present_, kwargs), = renderer.submits
        assert kwargs["rect"] == normalized_rect(
            viewer._calculate_display_rect_px(), host.drawable_size)
        rect_px = viewer._calculate_display_rect_px()
        # The rect is whole pixels: its ratio may be a pixel's width off 4:3
        # (a real Cocoa window does not always fit an exact multiple).
        assert abs(rect_px[2] / rect_px[3] - 4 / 3) <= 1.0 / rect_px[3]

    def test_old_generation_is_returned_not_shown(self, widget) -> None:
        renderer = _FakeRenderer()
        _arm(widget, renderer)
        frame = _present(frame=40, generation=5)
        assert widget.set_native_gpu_frame(frame, frame=40, generation=5) is False
        assert [(f, r) for f, r in renderer.released] == [(frame, "source_stale")]
        assert renderer.submits == []

    def test_stale_or_ahead_frame_is_returned(self, widget) -> None:
        renderer = _FakeRenderer()
        _arm(widget, renderer)
        for frame_index in (41, 40 - widget._GPU_STALE_WINDOW - 1):
            frame = _present(frame=frame_index)
            assert widget.set_native_gpu_frame(frame, frame=frame_index, generation=6) is False
            assert renderer.released[-1] == (frame, "source_stale")
        assert renderer.submits == []

    def test_hidden_view_returns_the_frame_and_redirects_new_ones(self, widget) -> None:
        renderer = _FakeRenderer()
        host = _arm(widget, renderer)
        frame = _present(frame=40)
        widget._view_hidden = True
        assert widget.set_native_gpu_frame(frame, frame=40, generation=6) is False
        assert renderer.released == [(frame, "source_layer_not_on_screen")]
        assert renderer.wanted[-1] is False
        del widget._view_hidden
        widget._view_hidden = False
        widget._layout_native_host()
        assert renderer.wanted[-1] is True

    def test_a_gl_frame_takes_the_screen_back(self, widget) -> None:
        renderer = _FakeRenderer()
        host = _arm(widget, renderer)
        widget.set_native_gpu_frame(_present(frame=40), frame=40, generation=6)
        assert not host.isHidden()
        widget._gpu_expected_frame = 41
        assert widget.set_gpu_frame(77, 640, 360, frame=41, generation=6, slot_id=3) is True
        assert host.isHidden()
        assert widget._gpu_displayed_slot_id == 3


class TestFpsReadout:
    def test_the_fps_readout_is_drawn_above_the_layer(self, widget, qapp) -> None:
        renderer = _FakeRenderer()
        host = _arm(widget, renderer)
        indicator = widget.viewer._fps_indicator
        widget.set_native_gpu_frame(_present(frame=40), frame=40, generation=6)
        qapp.processEvents()
        image = host.overlay_images[-1]
        assert isinstance(image, QImage) and not image.isNull()
        assert (image.width(), image.height()) == host.drawable_size

    def test_a_presented_native_frame_feeds_the_fps_counter(self, widget) -> None:
        renderer = _FakeRenderer()
        _arm(widget, renderer)
        indicator = widget.viewer._fps_indicator
        indicator.set_playback_state(True, 24.0)      # frames count only while playing
        before = len(indicator._samples)
        renderer.native_frame_presented.emit(40)
        assert len(indicator._samples) == before + 1
        renderer.native_frame_presented.emit(40)      # same frame is not counted twice
        assert len(indicator._samples) == before + 1


class TestFailureAndTeardown:
    def test_a_failed_viewport_hides_the_layer_and_is_taken_back(self, widget) -> None:
        renderer = _FakeRenderer()
        host = _arm(widget, renderer)
        widget.set_native_gpu_frame(_present(frame=40), frame=40, generation=6)
        renderer.active = False
        renderer.native_viewport_failed.emit("present_stalled:1")
        assert host.isHidden() and host.detached is True
        assert renderer.detached == 1
        assert widget.native_viewport_enabled() is False

    def test_a_transient_failure_only_hides_the_layer(self, widget) -> None:
        renderer = _FakeRenderer()
        host = _arm(widget, renderer)
        widget.set_native_gpu_frame(_present(frame=40), frame=40, generation=6)
        renderer.native_viewport_failed.emit("x")
        assert host.isHidden() and renderer.detached == 0

    def test_disable_takes_the_layer_back(self, widget) -> None:
        renderer = _FakeRenderer()
        host = _arm(widget, renderer)
        widget.disable_native_viewport()
        assert host.detached is True and renderer.detached == 1
        assert widget.native_viewport_enabled() is False


class _Gate:
    """SourcePresentGate double: records stashes; never pairs anything here."""

    def __init__(self) -> None:
        self.stashed: list = []

    def stash(self, present) -> None:
        self.stashed.append(present)

    def take(self, frame: int, generation: int):
        return None


def _controller(widget, renderer: _FakeRenderer):
    from src.app.controllers.file_controller import FileOpsMixin

    shell = SimpleNamespace(
        source_viewer_widget=widget, _source_gpu_renderer=renderer,
        _source_present_gate=_Gate())
    for name in ("_on_source_present_frame_ready", "_on_source_gpu_frame_ready",
                 "_stash_source_native_present", "_release_source_native_present"):
        setattr(shell, name, getattr(FileOpsMixin, name).__get__(shell))
    return shell


class TestControllerPairing:
    def test_native_descriptors_never_enter_the_gate(self, widget) -> None:
        renderer = _FakeRenderer()
        host = _arm(widget, renderer)
        shell = _controller(widget, renderer)
        present = _present(frame=40)
        shell._on_source_present_frame_ready(present)
        assert shell._source_present_gate.stashed == []
        assert shell._source_native_present is present

        shell._on_source_gpu_frame_ready(40, 6, 0, FRAME_W, FRAME_H)
        assert shell._source_native_present is None
        (submitted, _kwargs), = renderer.submits
        assert submitted is present and not host.isHidden()

    def test_a_superseded_native_descriptor_is_returned(self, widget) -> None:
        renderer = _FakeRenderer()
        _arm(widget, renderer)
        shell = _controller(widget, renderer)
        older, newer = _present(frame=39), _present(frame=40)
        shell._on_source_present_frame_ready(older)
        shell._on_source_present_frame_ready(newer)
        assert renderer.released == [(older, "source_superseded")]

    def test_an_unpaired_native_descriptor_is_returned(self, widget) -> None:
        renderer = _FakeRenderer()
        _arm(widget, renderer)
        shell = _controller(widget, renderer)
        orphan = _present(frame=40)
        shell._on_source_present_frame_ready(orphan)
        # frame_ready of another task arrived: the descriptor is stale.
        shell._on_source_gpu_frame_ready(41, 6, 0, FRAME_W, FRAME_H)
        assert renderer.released == [(orphan, "source_unpaired")]
        assert renderer.submits == []

    def test_gl_descriptors_still_use_the_gate(self, widget) -> None:
        renderer = _FakeRenderer()
        _arm(widget, renderer)
        shell = _controller(widget, renderer)
        gl_frame = GpuPresentFrame(texture_id=55, width=64, height=36, frame=7,
                                   generation_id=6, slot_id=1, fence_ns=1)
        shell._on_source_present_frame_ready(gl_frame)
        assert shell._source_present_gate.stashed == [gl_frame]
        assert getattr(shell, "_source_native_present", None) is None
