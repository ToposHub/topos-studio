"""Native Qt framebuffer retention check (run without QT_QPA_PLATFORM=offscreen)."""
from unittest.mock import MagicMock

import pytest
from PyQt6.QtGui import QColor, QPainter

from src.shared.ui.widgets.gpu_scope_widget import GpuScopeWidget
from src.shared.ui.widgets.curves_widget import CurvesWidget


class _SolidPresenter(GpuScopeWidget):
    def _blit_shared_texture(self) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(80, 110, 140))
        painter.end()


class _CountingCurve(CurvesWidget):
    paints = 0

    def paintEvent(self, event) -> None:
        self.paints += 1
        super().paintEvent(event)


def test_curve_overlay_does_not_schedule_endless_parent_repaints(qapp):
    from PyQt6.QtTest import QTest
    canvas = _CountingCurve()
    canvas.resize(720, 360)
    canvas.btn_container.hide()
    canvas.show()
    try:
        QTest.qWait(150)
        before = canvas.paints
        QTest.qWait(150)
        assert canvas.paints - before <= 1
        canvas.update()
        QTest.qWait(30)
        assert canvas.paints > before
    finally:
        canvas.close()


def test_native_busy_renderer_keeps_last_frame(qapp):
    if qapp.platformName() == 'offscreen':
        pytest.skip('Requires a native QOpenGLWidget framebuffer')
    widget = _SolidPresenter(show_value_axis=False)
    widget.resize(320, 180)
    widget.set_scope_frame(123, 320, 180)
    widget.show()
    try:
        qapp.processEvents()
        assert widget.isValid()
        widget._mutex = MagicMock()
        widget._mutex.tryLock.return_value = True
        widget.repaint()
        before = widget.grabFramebuffer().pixelColor(160, 90)
        assert before == QColor(80, 110, 140), (before.getRgb(), widget._cache_valid, widget._copy_pending)
        widget._mutex.tryLock.return_value = False
        for _ in range(5):
            widget.update()
            qapp.processEvents()
            assert widget.grabFramebuffer().pixelColor(160, 90) == before
        widget.clear()
        qapp.processEvents()
        assert widget.grabFramebuffer().pixelColor(160, 90) == QColor(5, 5, 5)
    finally:
        widget.close()


def test_presenter_uses_default_format_installed_after_module_import(qapp):
    from PyQt6.QtGui import QSurfaceFormat
    original = QSurfaceFormat.defaultFormat()
    configured = QSurfaceFormat(original)
    configured.setVersion(4, 1)
    configured.setProfile(QSurfaceFormat.OpenGLContextProfile.CoreProfile)
    QSurfaceFormat.setDefaultFormat(configured)
    try:
        widget = GpuScopeWidget()
        assert widget.format().version() == (4, 1)
        assert widget.format().profile() == configured.profile()
        assert widget.format().samples() == 0
        widget.close()
    finally:
        QSurfaceFormat.setDefaultFormat(original)


@pytest.mark.parametrize('kind', ['rgb', 'hsl'])
def test_native_curve_snapshot_survives_resize_busy_renderer_and_recycled_id(qapp, monkeypatch, kind):
    """Qt recreates its FBO on resize; a retired bare ID must never be sampled."""
    if qapp.platformName() == 'offscreen':
        pytest.skip('Requires native Qt/OpenGL framebuffer recreation')
    from OpenGL.GL import (
        GL_TEXTURE_2D, GL_RGBA8, GL_RGBA, GL_UNSIGNED_BYTE,
        glGenTextures, glBindTexture, glTexImage2D, glDeleteTextures, glFinish,
    )
    from src.shared.gpu.scope_publication import ScopePublication
    from src.shared.ui.widgets.hsl_curves_widget import HSLCurvesWidget
    from src.shared.ui.widgets import gpu_scope_widget as presenter_module
    canvas = CurvesWidget() if kind == 'rgb' else HSLCurvesWidget()
    canvas.resize(600, 330)
    canvas.show()
    widget = canvas._scope
    widget.show()
    qapp.processEvents()
    publication = None
    source = None
    try:
        assert widget.isValid()
        widget._mutex = MagicMock()
        widget._mutex.tryLock.return_value = True
        widget.makeCurrent()
        source = glGenTextures(1)
        glBindTexture(GL_TEXTURE_2D, source)
        rgba = bytes((80, 110, 140, 255)) * (64 * 32)
        glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA8, 64, 32, 0, GL_RGBA, GL_UNSIGNED_BYTE, rgba)
        glBindTexture(GL_TEXTURE_2D, 0)
        publication = ScopePublication.publish(glFinish)
        widget.doneCurrent()
        canvas.set_scope_frame(source, 64, 32, publication=publication)
        qapp.processEvents()
        widget.repaint()
        before = widget.grabFramebuffer().pixelColor(20, 20)
        assert before == QColor(80, 110, 140)
        assert widget._cache_valid

        # An allocation/copy failure while changing output size must leave
        # the old GPU snapshot alive and visible in the same paint.
        old_cache = widget._cache_tex
        blit = presenter_module._blit_scope_pixels
        def fail_new_copy(source_rect, target_rect, filtering):
            if filtering == presenter_module.GL_NEAREST:
                raise RuntimeError('injected GPU copy failure')
            return blit(source_rect, target_rect, filtering)
        with monkeypatch.context() as fault:
            fault.setattr(presenter_module, '_blit_scope_pixels', fail_new_copy)
            canvas.set_scope_frame(source, 128, 64, publication=publication)
            widget.repaint()
            assert widget._cache_tex == old_cache
            assert widget._cache_size == (64, 32)
            assert widget.grabFramebuffer().pixelColor(20, 20) == before

        # Producer frees the old allocation. A numeric ID might now refer to
        # a scene texture: the lifetime guard must reject it before glIsTexture.
        widget.makeCurrent()
        publication.retire()
        glDeleteTextures([source])
        source = None
        widget.doneCurrent()
        live_check = MagicMock(return_value=True)
        monkeypatch.setattr(presenter_module, 'glIsTexture', live_check)
        canvas.set_scope_frame(123, 64, 32, publication=publication)
        widget.repaint()
        live_check.assert_not_called()
        assert widget.grabFramebuffer().pixelColor(20, 20) == before

        widget._mutex.tryLock.return_value = False
        for width, height in ((740, 440), (420, 260), (820, 520), (600, 330)):
            canvas.resize(width, height)
            qapp.processEvents()
            widget.repaint()
            assert widget.grabFramebuffer().pixelColor(20, 20) == before
        assert widget._cache_size == (64, 32)
        assert not widget._copy_pending
    finally:
        widget.makeCurrent()
        if publication is not None:
            publication.retire()
        if source is not None:
            glDeleteTextures([source])
        widget.teardownGL()
        widget.doneCurrent()
        canvas.close()
