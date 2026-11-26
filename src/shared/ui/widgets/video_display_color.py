"""Window-specific ColorSync state and the existing viewer draw-pass bindings."""

from __future__ import annotations

import ctypes
import logging
import sys
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any

import numpy as np
from PyQt6 import sip
from PyQt6.QtCore import QObject, QTimer
from PyQt6.QtGui import QGuiApplication, QImage, QOpenGLContext, QWindow
from PyQt6.QtOpenGLWidgets import QOpenGLWidget

from src.shared.color.desktop_video_color import (
    DISPLAY_COLOR_TEXTURE_UNIT, LUT_SIZE, build_calibrated_display_lut, build_display_lut,
    convert_calibrated_display_image,
)
from src.shared.color.monitor_calibration import CalibrationRevision, monitor_calibration_revision

_LOG = logging.getLogger(__name__)
_POOL = ThreadPoolExecutor(max_workers=1, thread_name_prefix="viewer-colorsync")


def _window_icc(window: QWindow) -> bytes:
    """Read the active Cocoa backing profile, including profiles Qt cannot parse."""
    try:
        import objc

        native_view = objc.objc_object(c_void_p=int(window.winId()))
        space = native_view.window().colorSpace()
        profile = space.ICCProfileData() if space is not None else None
    except (ImportError, AttributeError, RuntimeError, ValueError):
        profile = None
    return bytes(profile) if profile else bytes(window.format().colorSpace().iccProfile())


class VideoDisplayColor(QObject):
    """Build display LUTs off the GUI thread and upload within the draw pass."""

    def __init__(self, viewport: QOpenGLWidget, changed: Callable[[], None]) -> None:
        super().__init__(viewport)
        self._viewport = viewport
        self._changed = changed
        self._preset: str | None = None
        self._calibration: tuple[str, CalibrationRevision] = ("", (0, 0, 0, 0))
        self._key: tuple[str | None, bytes, str, CalibrationRevision] | None = None
        self._pending: Future[np.ndarray] | None = None
        self._data: np.ndarray | None = None
        self._calibration_only_requested = False
        self._calibration_only_future: Future[np.ndarray] | None = None
        self._calibration_only_data: np.ndarray | None = None
        self._texture: int | None = None
        self._texture_data: np.ndarray | None = None
        self._context: QOpenGLContext | None = None
        self._timer = QTimer(self)
        self._timer.setInterval(300)
        self._timer.timeout.connect(self._refresh_profile)
        self._image_request: tuple[QImage, Callable[[QImage], None]] | None = None
        self._image_future: Future[QImage] | None = None
        self._in_flight_image: tuple[QImage, Callable[[QImage], None]] | None = None
        self._image_revision = 0
        self._image_timer = QTimer(self)
        self._image_timer.setInterval(8)
        self._image_timer.timeout.connect(self._poll_image)

    def cancel_images(self) -> None:
        self._image_request = None
        self._image_revision += 1

    def display_image(self, image: QImage, present: Callable[[QImage], None]) -> None:
        """Adapt Color's timeline QImages using a bounded latest-frame queue."""
        if self._key is None:
            present(image)
            return
        self._image_request = (image, present)
        self._poll_image()

    def _poll_image(self) -> None:
        future = self._image_future
        if future is not None:
            if not future.done():
                return
            self._image_future = None
            original, present = self._in_flight_image
            self._in_flight_image = None
            if self._in_flight_key == self._key and self._in_flight_revision == self._image_revision:
                try:
                    image = future.result()
                except (OSError, ImportError, ValueError, RuntimeError, TypeError):
                    _LOG.warning("Viewer display image transform failed", exc_info=True)
                    image = original
                present(image)
        request, self._image_request = self._image_request, None
        if request is None or self._key is None:
            self._image_timer.stop()
            return
        self._in_flight_image = request
        self._in_flight_key = self._key
        self._in_flight_revision = self._image_revision
        self._image_future = _POOL.submit(convert_calibrated_display_image, request[0], *self._key[:3])
        self._image_timer.start()

    @property
    def has_calibration(self) -> bool:
        return bool(self._calibration[0])

    def set_calibration_lut(self, path: str) -> None:
        self.configure(self._preset, self._preset is not None, path)

    def configure(self, preset: str | None, managed: bool, calibration_lut: str | None = None) -> None:
        cocoa = sys.platform == "darwin" and QGuiApplication.platformName() == "cocoa"
        requested = preset if managed and cocoa else None
        calibration = self._calibration
        if calibration_lut is not None:
            revision = (0, 0, 0, 0)
            if calibration_lut:
                try:
                    revision = monitor_calibration_revision(calibration_lut)
                except OSError:
                    _LOG.warning("Viewer calibration LUT is unavailable: %s", calibration_lut)
                    calibration_lut = ""
            calibration = (calibration_lut, revision)
        if requested == self._preset and calibration == self._calibration:
            return
        self._preset = requested
        self._calibration = calibration
        self._refresh_profile()

    def _refresh_profile(self) -> None:
        # Cocoa's QWindow format reports the actual backing-store ICC. It
        # follows the NSWindow on profile changes and after a fullscreen reparent.
        window = self._viewport.window().windowHandle() if self._preset else None
        profile = _window_icc(window) if window is not None else b""
        path, revision = self._calibration
        key = (self._preset, profile, path, revision) if profile or path else None
        if key != self._key:
            if self._pending is not None:
                self._pending.cancel()
            if self._calibration_only_future is not None:
                self._calibration_only_future.cancel()
            self._calibration_only_requested = False
            self._calibration_only_future = self._calibration_only_data = None
            self._key = key
            if path:
                self._pending = _POOL.submit(build_calibrated_display_lut, self._preset, profile, path, revision)
            elif profile:
                self._pending = _POOL.submit(build_display_lut, self._preset, profile)
            else:
                self._pending = None
            self._data = None
            self._changed()
        pending = self._pending
        if pending is not None and pending.done():
            self._pending = None
            try:
                self._data = pending.result()
            except (OSError, ImportError, ValueError, RuntimeError, TypeError):
                _LOG.warning("Viewer display transform could not be built", exc_info=True)
                if path and profile:
                    self._pending = _POOL.submit(build_display_lut, self._preset, profile)
            else:
                self._changed()
        calibration_only = self._calibration_only_future
        if calibration_only is not None and calibration_only.done():
            self._calibration_only_future = None
            try:
                self._calibration_only_data = calibration_only.result()
            except (OSError, ImportError, ValueError, RuntimeError, TypeError):
                _LOG.warning("Viewer calibration transform could not be built", exc_info=True)
            else:
                self._changed()
        if self._preset or self._pending is not None:
            if not self._timer.isActive():
                self._timer.start()
        else:
            self._timer.stop()

    def _release_texture(self) -> None:
        texture = self._texture
        context = self._context
        self._texture = self._context = self._texture_data = None
        if texture is None or context is None:
            return
        try:
            context.aboutToBeDestroyed.disconnect(self._release_texture)
        except (RuntimeError, TypeError):
            pass
        from OpenGL.GL import glDeleteTextures

        if QOpenGLContext.currentContext() == context:
            glDeleteTextures([texture])
        elif not sip.isdeleted(self._viewport) and self._viewport.isValid():
            self._viewport.makeCurrent()
            try:
                if QOpenGLContext.currentContext() == context:
                    glDeleteTextures([texture])
            finally:
                self._viewport.doneCurrent()

    def _bind_texture(self, data: np.ndarray | None) -> bool:
        if self._texture is not None and self._texture_data is not data:
            self._release_texture()
        if data is None:
            return False
        context = QOpenGLContext.currentContext()
        if context is None:
            return False

        from OpenGL.error import GLError
        from OpenGL.GL import (
            GL_ACTIVE_TEXTURE, GL_CLAMP_TO_EDGE, GL_HALF_FLOAT, GL_LINEAR, GL_RGB, GL_RGB16F,
            GL_TEXTURE0, GL_TEXTURE_3D, GL_TEXTURE_MAG_FILTER, GL_TEXTURE_MIN_FILTER,
            GL_TEXTURE_WRAP_R, GL_TEXTURE_WRAP_S, GL_TEXTURE_WRAP_T, GL_UNPACK_ALIGNMENT,
            glActiveTexture, glBindTexture, glDeleteTextures, glGenTextures,
            glGetIntegerv, glPixelStorei, glTexImage3D, glTexParameteri,
        )

        active = int(glGetIntegerv(GL_ACTIVE_TEXTURE))
        glActiveTexture(GL_TEXTURE0 + DISPLAY_COLOR_TEXTURE_UNIT)
        try:
            if self._texture is not None:
                glBindTexture(GL_TEXTURE_3D, self._texture)
                return True
            texture = int(glGenTextures(1))
            try:
                glBindTexture(GL_TEXTURE_3D, texture)
                for axis in (GL_TEXTURE_WRAP_S, GL_TEXTURE_WRAP_T, GL_TEXTURE_WRAP_R):
                    glTexParameteri(GL_TEXTURE_3D, axis, GL_CLAMP_TO_EDGE)
                glTexParameteri(GL_TEXTURE_3D, GL_TEXTURE_MIN_FILTER, GL_LINEAR)
                glTexParameteri(GL_TEXTURE_3D, GL_TEXTURE_MAG_FILTER, GL_LINEAR)
                alignment = int(glGetIntegerv(GL_UNPACK_ALIGNMENT))
                try:
                    glPixelStorei(GL_UNPACK_ALIGNMENT, 1)
                    glTexImage3D(
                        GL_TEXTURE_3D, 0, GL_RGB16F, LUT_SIZE, LUT_SIZE, LUT_SIZE, 0,
                        GL_RGB, GL_HALF_FLOAT, ctypes.c_void_p(data.ctypes.data),
                    )
                finally:
                    glPixelStorei(GL_UNPACK_ALIGNMENT, alignment)
            except GLError:
                glDeleteTextures([texture])
                if data is self._calibration_only_data:
                    self._calibration_only_data = None
                else:
                    self._data = None
                _LOG.warning("Could not upload the viewer display LUT", exc_info=True)
                return False
            self._texture = texture
            self._texture_data = data
            self._context = context
            context.aboutToBeDestroyed.connect(self._release_texture)
            return True
        finally:
            glActiveTexture(active)

    def bind_moderngl(self, program: Any, enabled: bool) -> None:
        """Configure the currently executing Color canvas draw pass."""
        program["topos_monitor_icc_lut"].value = DISPLAY_COLOR_TEXTURE_UNIT
        program["topos_monitor_icc_enabled"].value = self._bind_texture(self._display_data(enabled))

    def _display_data(self, icc_enabled: bool) -> np.ndarray | None:
        if icc_enabled:
            return self._data
        path, revision = self._calibration
        if not path:
            return None
        if self._key is None or not self._key[1]:
            return self._data
        if not self._calibration_only_requested:
            self._calibration_only_requested = True
            self._calibration_only_future = _POOL.submit(build_calibrated_display_lut, None, b"", path, revision)
        return self._calibration_only_data

    def bind_gl(
        self, gl: Any, program: int, enabled: bool = True, *, uniform_locations: tuple[int, int] | None = None,
    ) -> bool:
        """Configure the currently executing desktop viewer draw pass."""
        if uniform_locations is None:
            uniform_locations = (
                gl.glGetUniformLocation(program, b"topos_monitor_icc_lut"),
                gl.glGetUniformLocation(program, b"topos_monitor_icc_enabled"),
            )
        lut_location, enabled_location = uniform_locations
        gl.glUniform1i(lut_location, DISPLAY_COLOR_TEXTURE_UNIT)
        active = self._bind_texture(self._display_data(enabled))
        gl.glUniform1i(enabled_location, int(active))
        return active
