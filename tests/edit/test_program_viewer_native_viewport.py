"""Program Viewer routing for the native Metal viewport (plan tasks 2.2 / 2.3).

The viewer widget decides where each frame goes: a native descriptor is
handed to the Metal viewport and the layer is raised over the GL viewer;
anything the GL viewer can show (CPU snapshots, GL descriptors) takes the
screen back.  Overlays are re-rendered into the layer's overlay image from
the same painters paintGL uses, a frame's lease goes back on the GUI thread
like the GL ack, and a viewport that cannot be on screen never starves the
viewer.  The renderer is a stand-in with the same signals; the real layer is
covered on the Cocoa platform by ``test_metal_viewport_host.py``.
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("PyQt6.QtWidgets")

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtGui import QImage
from PyQt6.QtWidgets import QWidget

from src.features.edit.ui.program_viewer import ProgramViewerWidget
from src.shared.gpu.metal.present_port import normalized_rect
from src.shared.gpu.present_frame import (
    GpuPresentFrame,
    NativePresentDescriptor,
    PresentLeaseKey,
)

FRAME_W, FRAME_H = 1920, 1080


class _FakeRenderer(QObject):
    """The renderer's native-viewport API, recording every call."""

    native_viewport_failed = pyqtSignal(str)
    native_frame_presented = pyqtSignal(int)

    def __init__(self, *, opted_in: bool = True) -> None:
        super().__init__()
        self.opted_in = opted_in
        self.active = False
        self.accept = True
        self.attached: list = []
        self.detached = 0
        self.submits: list = []
        self.represented: list = []
        self.wanted: list = []
        self.released: list = []          # frames sent back for the render thread

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
        return self.accept

    def represent_native_viewport(self, rect) -> bool:
        self.represented.append(tuple(rect))
        return True

    def set_native_frames_wanted(self, wanted: bool) -> None:
        self.wanted.append(bool(wanted))

    def release_native_frame(self, present, reason: str = "unclaimed") -> None:
        self.released.append((present, reason))


class _FakeHost(QWidget):
    """Stands in for the Metal layer host: a plain widget that records overlays."""

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


def _native_present(frame: int = 10, slot: int = 3, generation: int = 4) -> GpuPresentFrame:
    return GpuPresentFrame(
        texture_id=0, width=FRAME_W, height=FRAME_H, frame=frame,
        generation_id=generation, slot_id=slot,
        lease_key=PresentLeaseKey(pool_generation=7, slot_id=slot, submission_id=2),
        extra={"render_backend": "metal"},
        native=NativePresentDescriptor(
            api="metal", device_identity="metal:Test GPU:1", resource=object(),
            resource_format="bgra8unorm", width=FRAME_W, height=FRAME_H),
    )


def _result(present: GpuPresentFrame, ack, trace_id: int = 0) -> dict:
    return {"present_frame": present, "ack_cb": ack, "trace_id": trace_id}


@pytest.fixture()
def widget(qapp):
    made = ProgramViewerWidget()
    made.resize(800, 520)
    made.show()
    qapp.processEvents()
    yield made
    made._native_renderer = None      # nothing to tear down on the fake
    made.close()
    qapp.processEvents()


def _arm(widget, renderer: _FakeRenderer) -> _FakeHost:
    """Put the widget into native mode with a fake layer host."""
    host = _FakeHost(widget._viewer_container)
    widget._metal_host = host
    widget._native_renderer = renderer
    renderer.active = True
    widget._viewer._overlay_dirty_hook = widget._schedule_native_refresh
    renderer.native_viewport_failed.connect(widget._on_native_viewport_failed)
    renderer.native_frame_presented.connect(widget._on_native_frame_presented)
    widget._layout_native_host()
    return host


class _Ack:
    def __init__(self) -> None:
        self.slots: list = []

    def __call__(self, slot_id: int) -> None:
        self.slots.append(slot_id)


class TestEnabling:
    def test_without_opt_in_nothing_is_set_up(self, widget) -> None:
        assert widget.enable_native_viewport(_FakeRenderer(opted_in=False)) is False
        assert widget.native_viewport_enabled() is False
        assert widget._metal_host is None

    def test_a_renderer_without_the_api_is_ignored(self, widget) -> None:
        assert widget.enable_native_viewport(None) is False
        assert widget.enable_native_viewport(object()) is False
        assert widget.native_viewport_enabled() is False

    def test_a_refused_layer_keeps_the_gl_viewer(self, widget, qapp) -> None:
        if qapp.platformName() == "cocoa":
            pytest.skip("the real layer attaches under Cocoa; see the host tests")
        renderer = _FakeRenderer()
        assert widget.enable_native_viewport(renderer) is False
        assert widget.native_viewport_enabled() is False
        assert renderer.attached == []
        assert widget._viewer.isVisible()


class TestRouting:
    def test_native_frame_without_a_viewport_is_returned_and_logged(self, widget, caplog) -> None:
        """No renderer can free the slot: the frame is dropped and that is logged."""
        import logging

        shown: list = []
        widget._viewer.set_present_frame = lambda *args, **kwargs: shown.append(args)
        ack = _Ack()
        with caplog.at_level(logging.ERROR, logger="src.features.edit.ui.program_viewer"):
            widget.set_frame(10, _result(_native_present(), ack, trace_id=0))
        assert ack.slots == [3]                  # bookkeeping only
        assert shown == []                       # the GL viewer cannot show it
        assert any("cannot be returned" in r.getMessage() for r in caplog.records)

    def test_native_frame_is_handed_over_and_the_layer_is_shown(self, widget, qapp) -> None:
        renderer = _FakeRenderer()
        host = _arm(widget, renderer)
        assert host.isHidden()
        viewer = widget._viewer
        shown: list = []
        viewer.set_present_frame = lambda *args, **kwargs: shown.append(args)
        present = _native_present(frame=42)
        ack = _Ack()

        widget.set_frame(42, _result(present, ack, trace_id=77))

        assert shown == [] and ack.slots == []
        (submitted, kwargs), = renderer.submits
        assert submitted is present
        assert callable(kwargs["on_released"])
        rect = kwargs["rect"]
        # Letterbox: the 16:9 picture keeps its aspect inside the layer.
        assert 0.0 <= rect[0] and rect[0] + rect[2] <= 1.0 + 1e-6
        assert 0.0 <= rect[1] and rect[1] + rect[3] <= 1.0 + 1e-3
        assert rect[2] > 0 and rect[3] > 0
        assert abs((rect[2] * host.drawable_size[0]) / (rect[3] * host.drawable_size[1])
                   - FRAME_W / FRAME_H) < 0.01
        assert not host.isHidden()
        assert host.geometry() == viewer.geometry()
        assert (viewer._tex_width, viewer._tex_height) == (FRAME_W, FRAME_H)
        assert viewer._current_frame_index == 42
        # The GL viewer is not repainted for it.
        assert shown == []

    def test_the_picture_rectangle_follows_the_viewer_geometry(self, widget, qapp) -> None:
        renderer = _FakeRenderer()
        host = _arm(widget, renderer)
        widget.set_frame(1, _result(_native_present(frame=1), _Ack()))
        (_present, kwargs), = renderer.submits
        viewer = widget._viewer
        rect_px = viewer._calculate_display_rect_qt()
        assert kwargs["rect"] == normalized_rect(
            (rect_px.x(), rect_px.y(), rect_px.width(), rect_px.height()), host.drawable_size)

    def test_a_refused_submission_returns_the_lease(self, widget) -> None:
        renderer = _FakeRenderer()
        _arm(widget, renderer)
        renderer.accept = False
        ack = _Ack()
        present = _native_present(frame=5, slot=2)
        widget.set_frame(5, _result(present, ack))
        assert widget._metal_host.isHidden()
        # The slot is the render thread's to free: the frame is sent back to it.
        assert [(f, r) for f, r in renderer.released] == [(present, "viewport_queue_refused")]
        assert ack.slots == [2]              # bookkeeping only, as for any native ack

    def test_a_cpu_snapshot_takes_the_screen_back(self, widget, qapp) -> None:
        renderer = _FakeRenderer()
        host = _arm(widget, renderer)
        widget.set_frame(1, _result(_native_present(frame=1), _Ack()))
        assert not host.isHidden()

        image = QImage(64, 36, QImage.Format.Format_RGB32)
        image.fill(0)
        widget.set_frame(2, image)
        qapp.processEvents()
        assert host.isHidden()
        assert widget._native_showing is False

    def test_a_gl_descriptor_takes_the_screen_back(self, widget, qapp) -> None:
        renderer = _FakeRenderer()
        host = _arm(widget, renderer)
        widget.set_frame(1, _result(_native_present(frame=1), _Ack()))
        shown: list = []
        widget._viewer.set_present_frame = lambda *args, **kwargs: shown.append(args)
        gl_frame = GpuPresentFrame(texture_id=55, width=64, height=36, frame=2,
                                   generation_id=4, slot_id=0, fence_ns=1)
        widget.set_frame(2, _result(gl_frame, _Ack()))
        assert host.isHidden()
        assert len(shown) == 1

    def test_clear_texture_hides_the_layer(self, widget) -> None:
        renderer = _FakeRenderer()
        host = _arm(widget, renderer)
        widget.set_frame(1, _result(_native_present(frame=1), _Ack()))
        widget.clear_texture()
        assert host.isHidden()


class TestLeaseAndAccounting:
    def test_release_goes_back_on_the_gui_thread_with_the_ack(self, widget, qapp) -> None:
        renderer = _FakeRenderer()
        _arm(widget, renderer)
        ack = _Ack()
        widget.set_frame(9, _result(_native_present(frame=9, slot=4), ack, trace_id=5))
        (present, kwargs), = renderer.submits
        gui_thread = threading.get_ident()
        worker = threading.Thread(
            target=kwargs["on_released"], args=(present, "replaced"), daemon=True)
        worker.start()
        worker.join(timeout=5.0)
        # The port called back on another thread; nothing ran on the GUI
        # thread yet.
        assert ack.slots == []
        qapp.processEvents()
        assert ack.slots == [4]

    def test_presented_frame_is_counted_like_paintgl(self, widget) -> None:
        renderer = _FakeRenderer()
        _arm(widget, renderer)
        presented: list = []
        widget._viewer.frame_presented.connect(presented.append)
        widget.set_frame(31, _result(_native_present(frame=31), _Ack(), trace_id=0))
        renderer.native_frame_presented.emit(31)
        assert presented == [31]


class TestLayerCannotShow:
    def test_trim_monitor_on_top_returns_the_frame_and_redirects_new_ones(self, widget, qapp) -> None:
        renderer = _FakeRenderer()
        _arm(widget, renderer)
        monitor = QWidget(widget._viewer_container)
        monitor.setGeometry(widget._viewer.geometry())
        monitor.show()
        widget._trim_monitor_widget = monitor
        ack = _Ack()
        frame = _native_present(frame=3)
        widget.set_frame(3, _result(frame, ack))
        assert ack.slots == [3]
        assert renderer.submits == []
        assert [(f, r) for f, r in renderer.released] == [(frame, "layer_not_on_screen")]
        # New frames are told to take the GL path so the viewer keeps moving.
        assert renderer.wanted[-1] is False

        monitor.hide()
        qapp.processEvents()
        widget.set_frame(4, _result(_native_present(frame=4), _Ack()))
        assert len(renderer.submits) == 1
        assert renderer.wanted[-1] is True


class TestStateEffects:
    def test_a_native_frame_clears_stale_error_feedback_like_the_gl_viewer(self, widget) -> None:
        """A fresh picture must not keep an old error overlay on screen."""
        renderer = _FakeRenderer()
        host = _arm(widget, renderer)
        viewer = widget._viewer
        viewer.set_error_state("render_error", "boom")
        viewer._offline_media = ("/gone/clip.mov",)
        widget.set_frame(1, _result(_native_present(frame=1), _Ack()))
        assert viewer._error_state == "none" and viewer._error_message == ""
        assert viewer._offline_media == ()
        assert host.overlay_images[-1] is None

class TestRefreshAndOverlays:
    def test_zoom_change_re_presents_the_kept_frame(self, widget, qapp) -> None:
        renderer = _FakeRenderer()
        host = _arm(widget, renderer)
        widget.set_frame(1, _result(_native_present(frame=1), _Ack()))
        before = renderer.submits[0][1]["rect"]

        viewer = widget._viewer
        viewer._view_transform.zoom_at(
            2.0, viewer.width() / 2, viewer.height() / 2,
            viewer.width(), viewer.height(), FRAME_W, FRAME_H)
        widget._on_zoom_changed(2.0)
        qapp.processEvents()

        assert len(renderer.represented) >= 1
        after = renderer.represented[-1]
        assert after != tuple(before)          # zoomed in: the picture is larger
        assert after[2] > before[2] and after[3] > before[3]

    def test_overlays_are_rendered_into_the_layer_image(self, widget, qapp) -> None:
        renderer = _FakeRenderer()
        host = _arm(widget, renderer)
        widget.set_frame(1, _result(_native_present(frame=1), _Ack()))
        # Nothing to draw above a clean picture.
        assert host.overlay_images[-1] is None

        widget._viewer.set_error_state("render_error", "boom")   # calls update()
        qapp.processEvents()

        image = host.overlay_images[-1]
        assert isinstance(image, QImage)
        assert not image.isNull()
        # The transparent overlay covers the whole layer.
        assert (image.width(), image.height()) == host.drawable_size

    def test_overlay_image_matches_the_paintgl_painters(self, widget) -> None:
        viewer = widget._viewer
        assert viewer.has_overlays() is False
        assert viewer.render_overlay_image() is None
        viewer._tex_width, viewer._tex_height = FRAME_W, FRAME_H
        viewer.set_error_state("no_clip")
        assert viewer.has_overlays() is True
        image = viewer.render_overlay_image()
        assert image is not None and image.format() == QImage.Format.Format_ARGB32_Premultiplied
        # Something was painted: not every pixel stayed transparent.
        assert any(image.pixel(x, y) >> 24 for x in range(0, image.width(), 16)
                   for y in range(0, image.height(), 16))

    def test_leaving_native_mode_clears_the_overlay(self, widget, qapp) -> None:
        renderer = _FakeRenderer()
        host = _arm(widget, renderer)
        widget.set_frame(1, _result(_native_present(frame=1), _Ack()))
        # An overlay that appears while the native picture is on screen.
        widget._viewer.set_error_state("render_error", "boom")
        qapp.processEvents()
        assert host.overlay_images[-1] is not None
        image = QImage(8, 8, QImage.Format.Format_RGB32)
        image.fill(0)
        widget.set_frame(2, image)
        assert host.overlay_images[-1] is None


class TestFileControllerHooks:
    """The two helpers the file controller calls around the GPU pipeline."""

    def test_opted_in_session_enables_the_viewport(self) -> None:
        from src.app.controllers.file_controller import _enable_native_program_viewport

        renderer = _FakeRenderer()
        calls: list = []
        # Any host holding the widget will do — no controller methods needed.
        host = SimpleNamespace(program_viewer_widget=SimpleNamespace(
            enable_native_viewport=lambda r: calls.append(r) or True))
        _enable_native_program_viewport(host, renderer)
        assert calls == [renderer]

    def test_nothing_happens_without_opt_in_or_api(self) -> None:
        from src.app.controllers import file_controller

        widget = SimpleNamespace(enable_native_viewport=lambda r: pytest.fail("must not be called"))
        host = SimpleNamespace(program_viewer_widget=widget)
        file_controller._enable_native_program_viewport(host, _FakeRenderer(opted_in=False))
        file_controller._enable_native_program_viewport(host, object())
        file_controller._enable_native_program_viewport(host, None)
        file_controller._enable_native_program_viewport(object(), _FakeRenderer())
        file_controller._disable_native_program_viewport(host)   # widget lacks the query
        file_controller._disable_native_program_viewport(object())

    def test_a_raising_viewer_never_breaks_pipeline_setup(self) -> None:
        from src.app.controllers.file_controller import _enable_native_program_viewport

        def explode(_renderer):
            raise RuntimeError("attach failed")

        host = SimpleNamespace(program_viewer_widget=SimpleNamespace(
            enable_native_viewport=explode))
        _enable_native_program_viewport(host, _FakeRenderer())   # must not raise

    def test_close_takes_the_layer_back_once(self) -> None:
        from src.app.controllers.file_controller import _disable_native_program_viewport

        calls: list = []
        host = SimpleNamespace(program_viewer_widget=SimpleNamespace(
            native_viewport_enabled=lambda: True,
            disable_native_viewport=lambda: calls.append(1)))
        _disable_native_program_viewport(host)
        assert calls == [1]


class TestFailureAndTeardown:
    def test_a_failed_viewport_falls_back_and_takes_the_layer_back(self, widget, qapp) -> None:
        renderer = _FakeRenderer()
        host = _arm(widget, renderer)
        widget.set_frame(1, _result(_native_present(frame=1), _Ack()))
        renderer.active = False                  # the port is gone for good
        renderer.native_viewport_failed.emit("completion_timeout:sequences=[0]")
        qapp.processEvents()

        assert widget._native_showing is False
        assert host.isHidden()
        assert renderer.detached == 1
        assert widget.native_viewport_enabled() is False

    def test_a_transient_failure_only_hides_the_layer(self, widget) -> None:
        renderer = _FakeRenderer()
        host = _arm(widget, renderer)
        widget.set_frame(1, _result(_native_present(frame=1), _Ack()))
        renderer.native_viewport_failed.emit("completion_timeout:sequences=[0]")
        assert widget._native_showing is False and host.isHidden()
        assert renderer.detached == 0
        assert widget.native_viewport_enabled() is True

    def test_a_dead_render_thread_cannot_hold_the_layer(self, widget, qapp) -> None:
        """No confirmation will ever come: stop retrying and take it back."""
        renderer = _FakeRenderer()
        renderer.isRunning = lambda: False
        renderer.detach_native_viewport = lambda timeout_s=2.0: False
        host = _arm(widget, renderer)
        widget.disable_native_viewport()
        assert widget.native_viewport_enabled() is True     # waiting for the thread
        widget._retry_native_detach()
        qapp.processEvents()
        assert host.detached is True
        assert widget.native_viewport_enabled() is False

    def test_disable_takes_the_layer_back_and_clears_the_hook(self, widget) -> None:
        renderer = _FakeRenderer()
        host = _arm(widget, renderer)
        widget.disable_native_viewport()
        assert renderer.detached == 1
        assert host.detached is True
        assert widget._viewer._overlay_dirty_hook is None
        assert widget.native_viewport_enabled() is False
