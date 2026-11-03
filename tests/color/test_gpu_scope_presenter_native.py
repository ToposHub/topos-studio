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
        assert before == QColor(80, 110, 140)
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
