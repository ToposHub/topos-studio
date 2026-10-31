import logging
from PyQt6.QtOpenGLWidgets import QOpenGLWidget
from PyQt6.QtWidgets import QGraphicsScene
from PyQt6.QtCore import pyqtSignal, QTimer
from src.shared.gpu.gl_backend import GLContextManager

logger = logging.getLogger(__name__)

# Keep Color's OpenGL framebuffer identical to Edit's ProgramViewer backdrop.
# This must be set at the GL layer: QGraphicsView's background brush does not
# paint the areas left outside a letterboxed native-painting quad.
VIEWER_BACKGROUND = (0.08, 0.08, 0.08, 1.0)

class GLCanvasViewport(QOpenGLWidget):
    """
    Custom viewport for QGraphicsView that handles ModernGL initialization.
    """
    # Signal emitted when OpenGL context and async renderer are fully initialized
    gl_initialized = pyqtSignal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        # When the render thread owns the shared GL mutex, Canvas deliberately
        # keeps showing its previous completed frame instead of blocking the
        # GUI thread.  Preserve the widget framebuffer for that frame-hold.
        self.setUpdateBehavior(QOpenGLWidget.UpdateBehavior.PartialUpdate)
        self.gl_manager = GLContextManager.instance()
        self._needs_context_restore = False
        self._pending_render_after_show = False
        self._gl_initialized = False  # Track whether GL init has completed
        # AUD-25411：show 后延迟渲染的重试计数（尺寸无效时有限重试）
        self._show_render_retries = 0
        
        # P4-1: Resize 防抖 timer，避免拖拽窗口边缘时触发 full render storm
        self._resize_debounce_timer = QTimer(self)
        self._resize_debounce_timer.setSingleShot(True)
        self._resize_debounce_timer.setInterval(50)  # 50ms 防抖
        self._resize_debounce_timer.timeout.connect(self._trigger_resize_refresh)

    def initializeGL(self) -> None:
        """Called once before the first call to paintGL() or resizeGL()."""
        if self._gl_initialized:
            return
        self._gl_initialized = True

        # Ensure the PyQt context is current before initializing ModernGL
        self.makeCurrent()
        self.gl_manager.initialize(self.context())

        # Notify the engine to start the background renderer
        parent_canvas = self.parent()
        while parent_canvas and not hasattr(parent_canvas, 'engine'):
            parent_canvas = parent_canvas.parent()

        if parent_canvas and hasattr(parent_canvas, 'engine'):
            parent_canvas.engine.initialize_async_renderer(self.context())
            # Connect the frame_ready signal to trigger update
            parent_canvas.engine.renderer.frame_ready.connect(self._on_frame_ready)

        # Emit signal to notify that GL context and async renderer are ready
        self.gl_initialized.emit()

    def _on_frame_ready(self, tex_id) -> None:
        # ``frame_ready`` is emitted by the renderer thread.  Updating only the
        # QOpenGLWidget is insufficient for a QGraphicsView background: Qt may
        # repaint the widget's GL buffer without asking Canvas.drawBackground()
        # to draw the newly published texture.  This was especially visible
        # for a paused timeline-clip switch: the old frame was cleared, but
        # the exact first frame remained absent until playback invalidated the
        # scene again.  Invalidate the background layer explicitly so this
        # completed frame is presented immediately while still staying wholly
        # on the UI thread.
        parent_canvas = self.parent()
        while parent_canvas and not hasattr(parent_canvas, 'engine'):
            parent_canvas = parent_canvas.parent()
        if parent_canvas is not None:
            # Canvas stores its QGraphicsScene in ``self.scene`` (which
            # shadows QGraphicsView.scene()), whereas other views may retain
            # the Qt method.  Support both forms.
            scene = getattr(parent_canvas, "scene", None)
            if callable(scene):
                scene = scene()
            if scene is not None:
                scene.invalidate(
                    parent_canvas.mapToScene(self.rect()).boundingRect(),
                    QGraphicsScene.SceneLayer.BackgroundLayer,
                )
        # Trigger a repaint on the UI thread.
        self.update()

    def resizeGL(self, width, height) -> None:
        """Called whenever the widget has been resized."""
        if self.gl_manager.ctx:
            # Update viewport if needed
            self.gl_manager.ctx.viewport = (0, 0, width, height)
        
        # P4-1: 启动防抖 timer，延迟触发视口刷新
        # 避免拖拽窗口边缘时频繁触发渲染（render storm）
        self._resize_debounce_timer.start()

    def _trigger_resize_refresh(self) -> None:
        """P4-1: Resize 结束后触发轻量视口刷新。
        
        复用已有的 GPU 纹理（视频或图像），只更新 viewport 参数，
        不触发完整的 render_pipeline()。
        """
        parent_canvas = self.parent()
        while parent_canvas:
            if hasattr(parent_canvas, '_update_lod') and hasattr(parent_canvas, '_refresh_video_frame_view'):
                parent_canvas._update_lod()
                # 优先使用轻量视口刷新（复用 GPU 视频纹理）
                if not parent_canvas._refresh_video_frame_view():
                    if hasattr(parent_canvas, 'engine') and parent_canvas.engine:
                        parent_canvas.engine.render_pipeline(render_reason='viewport_refresh')
                parent_canvas.viewport().update()
                break
            parent_canvas = parent_canvas.parent()

    def paintGL(self) -> None:
        """
        Called whenever the widget needs to be painted.
        Note: QGraphicsView will call drawBackground, so we don't necessarily
        perform all rendering here if used as a viewport.
        """
        # The renderer may retain a previous frame during an exact seek, but
        # the viewer backdrop must still be painted every frame.  Returning
        # early here retained QOpenGLWidget's default black buffer, making
        # static previews use black bars while playback used gray bars.
        # AUD-25413: 原先这里每次重绘都做一次父级遍历（结果从未使用），
        # 属于 paintGL 每帧热路径死代码，已删除。
        # Clear the background
        if self.gl_manager.ctx:
            # A paused seek can briefly retain the widget framebuffer while
            # the renderer owns the shared context.  Clear it to the exact
            # same #141414 backdrop used by Edit's ProgramViewer.
            self.gl_manager.ctx.clear(*VIEWER_BACKGROUND)

    def showEvent(self, event) -> None:
        """Called when the widget is shown. Ensure GL context is active."""
        super().showEvent(event)

        # Mark that we may need to restore context after being hidden
        self._needs_context_restore = False
        # AUD-25411：新的 show 会话重置重试计数
        self._show_render_retries = 0

        # Make the GL context current
        if self.gl_manager.ctx:
            self.makeCurrent()

        # Trigger update to ensure content is rendered
        self.update()

        # Notify parent canvas to trigger a render
        # This fixes black screen when switching between pages
        parent_canvas = self.parent()
        while parent_canvas:
            if hasattr(parent_canvas, 'engine') and parent_canvas.engine:
                # Use QTimer to defer the render call until after the show event completes
                # and the widget is fully visible with correct size
                QTimer.singleShot(50, self._trigger_render_after_show)
                break
            parent_canvas = parent_canvas.parent()

    def _trigger_render_after_show(self) -> None:
        """Trigger a render after the widget is shown and has correct size.

        P4-1: 优先使用轻量视口刷新（复用 GPU 纹理），仅在必要时回退到完整渲染。
        AUD-25411：视口不可见（用户已切走页面/隐藏）时直接放弃本次
        渲染并终止重试链；尺寸长期无效时只做有限次重试（约 1s），
        不再无限循环。
        """
        if not self.isVisible():
            return
        parent_canvas = self.parent()
        while parent_canvas:
            if hasattr(parent_canvas, 'engine') and parent_canvas.engine:
                # Check if viewport has valid size
                view_rect = self.rect()
                if view_rect.width() > 10 and view_rect.height() > 10:
                    self._show_render_retries = 0
                    parent_canvas._update_lod()
                    # 优先使用轻量视口刷新（复用 GPU 视频纹理）
                    if hasattr(parent_canvas, '_refresh_video_frame_view'):
                        if not parent_canvas._refresh_video_frame_view():
                            parent_canvas.engine.render_pipeline(render_reason='viewport_refresh')
                    else:
                        parent_canvas.engine.render_pipeline(render_reason='viewport_refresh')
                    parent_canvas.viewport().update()
                else:
                    # If size is still invalid, retry a bounded number of times
                    # (视口可能停在被折叠的标签页上，不应无限重试)
                    if self._show_render_retries < 20:
                        self._show_render_retries += 1
                        QTimer.singleShot(50, self._trigger_render_after_show)
                    else:
                        logger.debug(
                            "GLCanvasViewport: 尺寸持续无效，放弃 show 后渲染")
                break
            parent_canvas = parent_canvas.parent()

    def hideEvent(self, event) -> None:
        """Called when the widget is hidden."""
        super().hideEvent(event)
        self._needs_context_restore = True
