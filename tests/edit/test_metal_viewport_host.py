"""The native Metal viewport host in a real window (Metal migration plan 2.1).

The GUI thread owns the window side: it creates the ``NSView`` +
``CAMetalLayer`` inside the widget, keeps them at the widget's size and
publishes the surface state the render thread checks.  The added views must
not take mouse events, a hidden widget must publish "not usable", and every
size / scale change must be a new surface generation.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytest.importorskip("PyQt6.QtWidgets")

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from PyQt6.QtGui import QImage
from PyQt6.QtWidgets import QWidget

if sys.platform != "darwin":
    pytest.skip("macOS only", allow_module_level=True)

import objc

from src.features.edit.ui.metal_viewport_host import MetalViewportHost
from src.shared.gpu.metal.context import MetalComputeContext

_METAL_OK, _METAL_REASON = MetalComputeContext.availability_reason()
pytestmark = pytest.mark.skipif(not _METAL_OK, reason=f"needs Metal ({_METAL_REASON})")


@pytest.fixture()
def window(qapp):
    host = MetalViewportHost()
    host.resize(320, 180)
    host.show()
    qapp.processEvents()
    yield host
    host.detach()
    host.close()
    qapp.processEvents()


def _frame(view) -> tuple:
    rect = view.frame()
    return tuple(rect.origin), tuple(rect.size)


class TestAttach:
    def test_attach_creates_a_layer_inside_the_widget(self, window) -> None:
        assert window.attach() is True
        assert window.failure == ""
        assert window.layer is not None
        assert window.layer.device() is window.device
        assert window.layer.allowsNextDrawableTimeout() is False
        assert window.layer.framebufferOnly() is True
        scale = window.devicePixelRatioF()
        assert tuple(window.layer.drawableSize()) == (
            round(320 * scale), round(180 * scale))

    def test_surface_state_is_published_for_the_render_thread(self, window) -> None:
        assert window.attach() is True
        state = window.publisher.current()
        assert state.visible is True
        assert state.is_usable()
        assert state.scale == window.devicePixelRatioF()
        assert state.generation >= 1

    def test_attaching_twice_keeps_the_first_layer(self, window) -> None:
        assert window.attach() is True
        layer = window.layer
        assert window.attach() is True
        assert window.layer is layer

    def test_supported_reports_the_platform(self) -> None:
        ok, reason = MetalViewportHost.supported()
        assert ok is True, reason


class TestAddedViews:
    def test_views_do_not_take_mouse_events(self, window) -> None:
        assert window.attach() is True
        view = window._metal_view
        size = view.frame().size
        # nil from hitTest: hands the event to Qt's view below.
        assert view.hitTest_((0.0, 0.0)) is None
        assert view.hitTest_((size.width / 2, size.height / 2)) is None

    def test_view_tracks_the_widget_size(self, window, qapp) -> None:
        assert window.attach() is True
        before = window.publisher.current()
        window.resize(640, 360)
        qapp.processEvents()
        after = window.publisher.current()
        assert after.drawable_size != before.drawable_size
        assert after.generation > before.generation
        assert _frame(window._metal_view)[1] == (640.0, 360.0)

    def test_unchanged_size_does_not_bump_the_generation(self, window) -> None:
        assert window.attach() is True
        generation = window.publisher.current().generation
        assert window._sync_surface().generation == generation

    def test_hidden_widget_is_not_usable(self, window, qapp) -> None:
        assert window.attach() is True
        window.hide()
        qapp.processEvents()
        assert window.publisher.current().is_usable() is False
        window.show()
        qapp.processEvents()
        assert window.publisher.current().is_usable() is True

    def test_detach_removes_the_view(self, window, qapp) -> None:
        assert window.attach() is True
        view = window._metal_view
        window.detach()
        qapp.processEvents()
        assert window.attached is False
        assert view.superview() is None
        assert window.publisher.current().visible is False
        # Detaching again is harmless.
        window.detach()


class TestOverlay:
    def test_overlay_shows_a_transparent_image_above_the_video(self, window, qapp) -> None:
        assert window.attach() is True
        width, height = window._overlay_view_size() if hasattr(window, "_overlay_view_size") else (
            window.width(), window.height())
        image = QImage(width, height, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(0)
        window.set_overlay_image(image)
        qapp.processEvents()
        assert window._overlay_layer is not None
        assert window._overlay_layer.contents() is not None
        assert not window._overlay_layer.opaque()
        assert window._overlay_view.hitTest_((0.0, 0.0)) is None
        # The overlay sits above the video view.
        siblings = list(window._metal_view.superview().subviews())
        assert siblings.index(window._overlay_view) > siblings.index(window._metal_view)

        window.set_overlay_image(None)
        # PyObjC returns its NULL sentinel (or None) for a cleared property.
        assert window._overlay_layer.contents() in (None, objc.NULL)

    def test_overlay_before_attach_is_kept_until_attached(self, qapp) -> None:
        host = MetalViewportHost()
        image = QImage(4, 4, QImage.Format.Format_ARGB32_Premultiplied)
        host.set_overlay_image(image)
        assert host._overlay_image is image
        host.close()
