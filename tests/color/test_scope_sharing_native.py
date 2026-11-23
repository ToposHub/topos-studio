"""Real shared Qt contexts: GPU histogram cache survives producer retirement."""
from unittest.mock import MagicMock

import moderngl
import numpy as np
import pytest
from PyQt6.QtGui import QOffscreenSurface, QOpenGLContext, QSurfaceFormat

from src.features.color.core.scope_types import ScopeDemand
from src.shared.gpu.gpu_scope_engine import GpuScopeEngine
from src.shared.ui.widgets.gpu_scope_widget import GpuScopeWidget


def test_shared_histogram_survives_producer_resize_and_release(qapp):
    if qapp.platformName() == 'offscreen':
        pytest.skip('Requires native Qt shared OpenGL contexts')
    widget = GpuScopeWidget(show_value_axis=False)
    fmt = QSurfaceFormat()
    fmt.setVersion(4, 1)
    fmt.setProfile(QSurfaceFormat.OpenGLContextProfile.CoreProfile)
    fmt.setSamples(0)
    widget.setFormat(fmt)
    widget.resize(512, 300)
    widget.show()
    qapp.processEvents()
    assert widget.isValid()
    widget._mutex = MagicMock()
    widget._mutex.tryLock.return_value = True
    producer = QOpenGLContext()
    producer.setFormat(widget.context().format())
    producer.setShareContext(widget.context())
    assert producer.create()
    assert QOpenGLContext.areSharing(producer, widget.context())
    surface = QOffscreenSurface()
    surface.setFormat(producer.format())
    surface.create()
    assert producer.makeCurrent(surface)
    ctx = moderngl.create_context(require=330)
    assert ctx.error == 'GL_NO_ERROR', 'Native context setup must start clean'
    engine = GpuScopeEngine(ctx)
    source = None
    try:
        assert engine.load_shaders()
        rgba = np.ones((180, 320, 4), dtype='f2')
        rgba[:, :, :3] = np.linspace(0.02, 0.98, 320)[None, :, None]
        source = ctx.texture((320, 180), 4, rgba.tobytes(), dtype='f2')
        demand = ScopeDemand(1, 1, 'histogram', (512, 300),
                             histogram_overlay=True, standalone_enabled=False,
                             curves_display_size=(512, 300))
        outputs = engine.render_demand(demand, source)
        assert ctx.error == 'GL_NO_ERROR', 'Histogram must submit without GL errors'
        publication = engine.publish()
        producer.doneCurrent()
        texture_id, size = outputs['curves_histogram']
        widget.set_scope_frame(texture_id, *size, publication=publication)
        qapp.processEvents()
        widget.repaint()
        image = widget.grabFramebuffer()
        assert widget._cache_valid and not widget._copy_pending
        buffer = np.frombuffer(image.bits().asstring(image.sizeInBytes()), dtype='u1')
        assert buffer.reshape(image.height(), image.bytesPerLine())[:, :image.width() * 4].std() > 20

        assert producer.makeCurrent(surface)
        # New analysis quality retires the entire old pool; its IDs can be
        # recycled immediately. No UI pointer may follow them into the pool.
        engine.render_demand(ScopeDemand(2, 2, 'waveform', (800, 600),
                                        analysis_size=(512, 288)), source)
        engine.publish()
        assert not publication.valid
        engine.release()
        producer.doneCurrent()
        widget._mutex.tryLock.return_value = False
        for width, height in ((800, 500), (400, 240), (512, 300)):
            widget.resize(width, height)
            qapp.processEvents()
            widget.repaint()
        assert widget.grabFramebuffer() == image
    finally:
        assert producer.makeCurrent(surface)
        engine.release()
        if source is not None:
            source.release()
        ctx.release()
        producer.doneCurrent()
        widget.makeCurrent()
        widget.teardownGL()
        widget.doneCurrent()
        widget.close()
        surface.destroy()
