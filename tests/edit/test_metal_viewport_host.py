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

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QImage
from PyQt6.QtWidgets import QWidget

if sys.platform != "darwin":
    pytest.skip("macOS only", allow_module_level=True)

import objc

from src.shared.ui.widgets.metal_viewport_host import MetalViewportHost
from src.shared.gpu.metal.context import MetalComputeContext

_METAL_OK, _METAL_REASON = MetalComputeContext.availability_reason()


def _platform(qapp) -> str:
    return qapp.platformName()


def _needs_cocoa(qapp) -> None:
    """The real-window tests need the Cocoa plugin; offscreen has no NSViews."""
    if not _METAL_OK:
        pytest.skip(f"needs Metal ({_METAL_REASON})")
    if _platform(qapp) != "cocoa":
        pytest.skip(f"needs the cocoa Qt platform (got {_platform(qapp)})")


@pytest.fixture()
def window(qapp):
    _needs_cocoa(qapp)
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
    def test_other_qt_platforms_are_refused_not_crashed(self, qapp) -> None:
        """``winId()`` is only an NSView pointer under the Cocoa plugin."""
        if not _METAL_OK:
            pytest.skip(f"needs Metal ({_METAL_REASON})")
        host = MetalViewportHost()
        host.resize(64, 36)
        host.show()
        qapp.processEvents()
        if _platform(qapp) == "cocoa":
            host.close()
            pytest.skip("covered by the real-window tests")
        assert host.attach() is False
        assert host.failure.startswith("platform_unsupported:")
        assert host.attached is False and host.layer is None
        assert MetalViewportHost.supported() == (False, host.failure)
        # An overlay requested before attaching is simply kept.
        host.set_overlay_image(QImage(4, 4, QImage.Format.Format_ARGB32_Premultiplied))
        host.close()

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

    def test_supported_reports_the_platform(self, window) -> None:
        ok, reason = MetalViewportHost.supported()
        assert ok is True, reason


class TestAddedViews:
    def test_creating_and_showing_the_host_logs_no_surface_error(self, qapp, caplog) -> None:
        """Qt delivers events while the widget is being built; the host must
        already hold its state by then."""
        import logging

        with caplog.at_level(logging.WARNING, logger="src.shared.ui.widgets.metal_viewport_host"):
            host = MetalViewportHost()
            host.resize(128, 72)
            host.show()
            host.setAttribute(Qt.WidgetAttribute.WA_NativeWindow, True)
            qapp.processEvents()
            host.close()
        assert not [r for r in caplog.records
                    if "surface event failed" in r.getMessage()], caplog.records


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
        _needs_cocoa(qapp)
        host = MetalViewportHost()
        image = QImage(4, 4, QImage.Format.Format_ARGB32_Premultiplied)
        host.set_overlay_image(image)
        assert host._overlay_image is image
        host.close()


def test_the_old_import_path_still_reaches_the_host() -> None:
    """The host moved to the shared widgets; the Edit path forwards to it."""
    from src.features.edit.ui import metal_viewport_host as forwarding
    from src.shared.ui.widgets import metal_viewport_host as moved

    assert forwarding.MetalViewportHost is moved.MetalViewportHost
    assert forwarding.METAL_VIEWPORT_PIXEL_FORMAT == moved.METAL_VIEWPORT_PIXEL_FORMAT
