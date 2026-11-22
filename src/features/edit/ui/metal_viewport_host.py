"""QWidget that shows a ``CAMetalLayer`` (Metal migration plan task 2.1).

The Edit preview's Program Viewer is a ``QOpenGLWidget``.  This host is the
window side of the native path: a Qt widget whose native view carries an
``NSView`` backed by a ``CAMetalLayer``.  The GUI thread alone creates,
sizes and removes those objects and publishes the result as a
:class:`~src.shared.gpu.metal.present_port.SurfaceState`; the render thread
reads that state and takes drawables from the same layer.

Two properties keep the rest of the viewer working unchanged:

- the added views never take part in hit testing, so mouse events still
  reach the ``QOpenGLWidget`` below and its subtitle / transform-handle
  interaction and zoom / pan code keep working as they are;
- the layer never waits for a drawable (``allowsNextDrawableTimeout`` is
  off): a realtime presenter keeps the last picture instead of blocking.

An optional overlay view sits above the layer for guides, subtitles and
handles (task 2.3); it shows a ``QImage`` painted by the existing overlay
code.
"""

from __future__ import annotations

import ctypes
import logging
import sys
import threading
from typing import Any, Optional

from PyQt6.QtCore import QEvent, QObject, Qt
from PyQt6.QtGui import QGuiApplication, QImage
from PyQt6.QtWidgets import QWidget

from src.shared.gpu.metal.present_port import SurfacePublisher, SurfaceState

logger = logging.getLogger(__name__)

# BGRA8Unorm: the format the present pass is built for and an SDR display
# accepts without conversion.
METAL_VIEWPORT_PIXEL_FORMAT = 80

_passthrough_views: Optional[tuple] = None
_classes_lock = threading.Lock()


def _define_passthrough_views() -> tuple:
    """Define the two Objective-C classes.  Their names are fixed: PyObjC
    registers an Objective-C class under the literal name of its Python
    class at definition time."""
    import Quartz

    class ToposMetalViewportView(Quartz.NSView):  # type: ignore[misc]
        def hitTest_(self, _point):  # noqa: N802 - Objective-C selector
            return None

    class ToposMetalOverlayView(Quartz.NSView):  # type: ignore[misc]
        def hitTest_(self, _point):  # noqa: N802 - Objective-C selector
            return None

    return ToposMetalViewportView, ToposMetalOverlayView


def passthrough_view_classes() -> tuple:
    """``(video view, overlay view)`` Objective-C classes that decline hit testing.

    Returning ``nil`` from ``hitTest:`` makes AppKit hand the event to the view
    below — Qt's own NSView for the widget — so every existing mouse handler
    of the viewer keeps working above the Metal layer.  The classes are
    registered with the runtime once per process.
    """
    global _passthrough_views
    with _classes_lock:
        if _passthrough_views is not None:
            return _passthrough_views
        import objc

        try:
            _passthrough_views = _define_passthrough_views()
        except objc.error:
            # Already registered (module reloaded): reuse the runtime classes.
            _passthrough_views = (
                objc.lookUpClass("ToposMetalViewportView"),
                objc.lookUpClass("ToposMetalOverlayView"),
            )
        return _passthrough_views


def create_metal_layer(device: Any, *, color_space: Any = None) -> Any:
    """A configured ``CAMetalLayer`` for SDR BGRA8 presentation."""
    import Metal
    import Quartz

    layer = Quartz.CAMetalLayer.alloc().init()
    layer.setDevice_(device)
    layer.setPixelFormat_(Metal.MTLPixelFormatBGRA8Unorm)
    # The drawable is only ever drawn to and presented; that is the fastest
    # configuration and lets the compositor discard it after presentation.
    layer.setFramebufferOnly_(True)
    # Realtime presenter: never block waiting for a drawable.
    layer.setAllowsNextDrawableTimeout_(False)
    layer.setDisplaySyncEnabled_(True)
    layer.setOpaque_(True)
    space = color_space or Quartz.CGColorSpaceCreateWithName(Quartz.kCGColorSpaceSRGB)
    if space is not None:
        layer.setColorspace_(space)
    return layer


class MetalViewportHost(QWidget):
    """Native window surface for Metal presentation.  GUI thread only."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        # Plain attributes first: creating the widget and its native window
        # below already delivers events (WinIdChange, scale changes) to
        # event(), which reads them.
        self._publisher = SurfacePublisher()
        self._layer: Any = None
        self._metal_view: Any = None
        self._overlay_view: Any = None
        self._overlay_layer: Any = None
        self._device: Any = None
        self._failure: str = ""
        self._overlay_image: Optional[QImage] = None
        self._overlay_buffer: Optional[bytes] = None
        super().__init__(parent)
        self.setObjectName("MetalViewportHost")
        # A native window of our own: the NSView must exist before anything
        # can be added to it, and ancestors stay in Qt's backing store.
        self.setAttribute(Qt.WidgetAttribute.WA_NativeWindow, True)
        self.setAttribute(Qt.WidgetAttribute.WA_DontCreateNativeAncestors, True)
        self.setAttribute(Qt.WidgetAttribute.WA_AlwaysStackOnTop, True)

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    @property
    def attached(self) -> bool:
        return self._layer is not None

    @property
    def layer(self) -> Any:
        """The ``CAMetalLayer`` once attached; the render thread presents to it."""
        return self._layer

    @property
    def device(self) -> Any:
        return self._device

    @property
    def publisher(self) -> SurfacePublisher:
        """Latest size / scale / visibility state, readable from any thread."""
        return self._publisher

    @property
    def failure(self) -> str:
        """Why :meth:`attach` refused (empty when attached or not attempted)."""
        return self._failure

    @property
    def drawable_size(self) -> tuple:
        state = self._publisher.current()
        return state.drawable_size

    @staticmethod
    def supported() -> tuple:
        """``(ok, reason)``: whether this process can host a Metal viewport."""
        if sys.platform != "darwin":
            return False, "not_macos"
        # ``winId()`` is only an NSView pointer on the Cocoa platform plugin.
        # Any other plugin (offscreen, minimal …) hands out ids that must never
        # be dereferenced as AppKit objects.
        platform = QGuiApplication.platformName() if QGuiApplication.instance() is not None else ""
        if platform != "cocoa":
            return False, f"platform_unsupported:{platform or 'no_application'}"
        try:
            import Metal  # noqa: F401
            import Quartz  # noqa: F401
            import objc  # noqa: F401
        except ImportError as error:
            return False, f"framework_unavailable:{error}"
        from src.shared.gpu.metal.context import MetalComputeContext

        return MetalComputeContext.availability_reason()

    # ------------------------------------------------------------------
    # 装拆
    # ------------------------------------------------------------------

    def attach(self, device: Optional[Any] = None) -> bool:
        """Create the layer and its view inside this widget's native view.

        Call once the widget has a native window (it is created on demand by
        ``winId()``).  ``device`` is the Metal device the frames are rendered
        with; pass it so presentation needs no cross-device copy.
        """
        if self._layer is not None:
            return True
        ok, reason = self.supported()
        if not ok:
            self._failure = reason
            logger.info("MetalViewportHost: not available (%s)", reason)
            return False
        import Metal
        import objc
        import Quartz

        if device is None:
            device = Metal.MTLCreateSystemDefaultDevice()
            if device is None:
                self._failure = "no_metal_device"
                return False
        try:
            host_view = objc.objc_object(c_void_p=ctypes.c_void_p(int(self.winId())))
            bounds = host_view.bounds()
            layer = create_metal_layer(device)
            view_class = passthrough_view_classes()[0]
            view = view_class.alloc().initWithFrame_((bounds.origin, bounds.size))
            view.setLayer_(layer)
            view.setWantsLayer_(True)
            view.setAutoresizingMask_(
                Quartz.NSViewWidthSizable | Quartz.NSViewHeightSizable)
            host_view.setAutoresizesSubviews_(True)
            host_view.addSubview_(view)
        except Exception as error:  # noqa: BLE001 - any AppKit failure is a refusal
            self._failure = f"attach_failed:{error}"
            logger.warning("MetalViewportHost: attach failed: %s", error)
            self._detach_objects()
            return False
        self._device, self._layer, self._metal_view = device, layer, view
        self._failure = ""
        self._sync_surface()
        return True

    def detach(self) -> None:
        """Remove the views.  The render thread must have stopped presenting."""
        self._detach_objects()
        self._publisher.publish(visible=False)

    def _detach_objects(self) -> None:
        view, overlay = self._metal_view, self._overlay_view
        self._metal_view = self._overlay_view = None
        self._layer = self._overlay_layer = None
        for item in (overlay, view):
            if item is not None:
                try:
                    item.removeFromSuperview()
                except Exception:  # noqa: BLE001 - teardown must not raise
                    logger.debug("MetalViewportHost: remove view failed", exc_info=True)
        self._device = None

    # ------------------------------------------------------------------
    # 尺寸与屏幕状态
    # ------------------------------------------------------------------

    def _sync_surface(self) -> SurfaceState:
        """Bring the views to the widget's current size and publish the state."""
        if self.__dict__.get("_layer") is None:
            return self.__dict__.get("_publisher", SurfacePublisher()).current()
        width, height = max(1, self.width()), max(1, self.height())
        scale = float(self.devicePixelRatioF())
        drawable = (max(1, round(width * scale)), max(1, round(height * scale)))
        frame = ((0.0, 0.0), (float(width), float(height)))
        try:
            self._metal_view.setFrame_(frame)
            self._layer.setFrame_(frame)
            self._layer.setContentsScale_(scale)
            self._layer.setDrawableSize_(drawable)
            if self._overlay_view is not None:
                self._overlay_view.setFrame_(frame)
                self._overlay_layer.setFrame_(frame)
                self._overlay_layer.setContentsScale_(scale)
        except Exception:  # noqa: BLE001 - a failed resize keeps the last state
            logger.warning("MetalViewportHost: surface update failed", exc_info=True)
            return self._publisher.current()
        # The generation in the published state is what the render thread
        # compares against; the drawable size is read to apply it.
        state = self._publisher.publish(
            drawable_size=drawable, scale=scale, visible=self._is_really_visible())
        return state

    def _is_really_visible(self) -> bool:
        window = self.window()
        return bool(self.isVisible() and (window is None or not window.isMinimized()))

    def resizeEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        super().resizeEvent(event)
        self._sync_surface()

    def showEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        super().showEvent(event)
        self._publisher.publish(visible=True)

    def hideEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        self._publisher.publish(visible=False)
        super().hideEvent(event)

    # Scale or screen changes keep the widget size but change the drawable
    # size: they are a new surface generation.  Not every binding exposes the
    # internal screen-change type, so missing names are simply not watched.
    _SURFACE_EVENT_TYPES = tuple(
        kind for kind in (
            getattr(QEvent.Type, "DevicePixelRatioChange", None),
            getattr(QEvent.Type, "ScreenChangeInternal", None),
            getattr(QEvent.Type, "WinIdChange", None),
        ) if kind is not None
    )

    def event(self, event: Any) -> bool:
        # PyQt aborts the process on an exception escaping a virtual method:
        # nothing in here may raise.
        try:
            if event.type() in self._SURFACE_EVENT_TYPES:
                self._sync_surface()
        except Exception:  # noqa: BLE001 - a failed sync keeps the last surface state
            logger.debug("MetalViewportHost: surface event failed", exc_info=True)
        return super().event(event)

    # ------------------------------------------------------------------
    # 叠加层（2.3）
    # ------------------------------------------------------------------

    def set_overlay_image(self, image: Optional[QImage]) -> None:
        """Show a transparent ``QImage`` above the video, or clear it.

        The image is painted in widget pixels (logical size times scale).
        The overlay view is created on first use; like the video view it
        takes no mouse events.
        """
        if self._layer is None:
            self._overlay_image = image
            return
        if image is None:
            if self._overlay_layer is not None:
                self._overlay_layer.setContents_(None)
            self._overlay_image = None
            self._overlay_buffer = None
            return
        import Quartz

        if self._overlay_view is None and not self._ensure_overlay_view():
            self._overlay_image = image
            return
        self._overlay_image = image
        try:
            cg_image, buffer = _cg_image_from_qimage(image)
            # The CGImage shares the buffer; keep it alive with the layer.
            self._overlay_buffer = buffer
            self._overlay_layer.setContents_(cg_image)
        except Exception:  # noqa: BLE001 - an overlay failure never takes the video down
            logger.warning("MetalViewportHost: overlay update failed", exc_info=True)

    def _ensure_overlay_view(self) -> bool:
        import objc
        import Quartz

        try:
            host_view = objc.objc_object(c_void_p=ctypes.c_void_p(int(self.winId())))
            bounds = host_view.bounds()
            view_class = passthrough_view_classes()[1]
            view = view_class.alloc().initWithFrame_((bounds.origin, bounds.size))
            layer = Quartz.CALayer.alloc().init()
            layer.setOpaque_(False)
            layer.setContentsGravity_(Quartz.kCAGravityTopLeft)
            view.setLayer_(layer)
            view.setWantsLayer_(True)
            view.setAutoresizingMask_(
                Quartz.NSViewWidthSizable | Quartz.NSViewHeightSizable)
            host_view.addSubview_(view)
        except Exception:  # noqa: BLE001
            logger.warning("MetalViewportHost: overlay view failed", exc_info=True)
            return False
        self._overlay_view, self._overlay_layer = view, layer
        self._sync_surface()
        return True


def _cg_image_from_qimage(image: QImage) -> Any:
    """A ``CGImage`` sharing the pixels of a premultiplied ARGB32 ``QImage``."""
    import Quartz

    if image.format() != QImage.Format.Format_ARGB32_Premultiplied:
        image = image.convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)
    size = image.sizeInBytes()
    buffer = image.constBits().asstring(size)
    provider = Quartz.CGDataProviderCreateWithData(None, buffer, len(buffer), None)
    colour_space = Quartz.CGColorSpaceCreateWithName(Quartz.kCGColorSpaceSRGB)
    cg_image = Quartz.CGImageCreate(
        image.width(), image.height(), 8, 32, image.bytesPerLine(), colour_space,
        Quartz.kCGBitmapByteOrder32Little | Quartz.kCGImageAlphaPremultipliedFirst,
        provider, None, False, Quartz.kCGRenderingIntentDefault)
    # Keep the pixel buffer alive for as long as the image exists.
    return cg_image, buffer


def overlay_rect_pixels(widget: QWidget) -> tuple:
    """Pixel size of an overlay image for ``widget`` (logical size times scale)."""
    scale = float(widget.devicePixelRatioF())
    return max(1, round(widget.width() * scale)), max(1, round(widget.height() * scale))
