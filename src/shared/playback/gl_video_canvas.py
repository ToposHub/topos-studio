"""GL 视频画布：YUV 平面直传 GPU，shader 完成 YUV→RGB/10-bit 展开。

为什么是 ``QOpenGLWidget``
--------------------------
播放器原先的画布是普通 ``QWidget`` + ``QPainter.drawImage``：解码线程
先把 10/12-bit YUV 用 swscale 压成 8-bit RGB，再整帧拷进 QImage。这样
既浪费 CPU 带宽，也把 10-bit 源的高光/暗部层次提前压掉了。

本画布用 Qt 自己管理的 OpenGL surface（macOS 上是 NSOpenGL/CGL，
Windows/Linux 上由 Qt 选择 WGL/GLX/EGL），在 ``initializeGL`` 里让
ModernGL 接管当前 Qt 上下文，然后复用项目已有的 ``YUVGPUUploader``：

- Y/U/V 三平面分别上传为纹理（8-bit 用 R8，10/12/16-bit 用 R16UI）；
- shader 按 H.273 matrix / range / chroma siting 做 YUV→RGB；
- 画面等比 letterbox，黑底，不拉伸。

线程边界
--------
所有 GL 调用都在 Qt GUI 线程的 ``initializeGL/paintGL`` 内完成；解码
线程只产出独立的 :class:`YuvFrame` 数组，不触碰任何 GL 对象。这与
``.clinerules`` 的“GL 只在渲染线程”一致，也和现有 ``GLContextManager``
的线程亲和约束不冲突（本画布不注册到全局 render context）。

失败回退
--------
离屏测试、远程桌面、无 GL 的机器上 ``initializeGL`` 可能拿不到
context。此时 ``gpu_active`` 保持 False，画布仍可按普通 QImage 绘制；
播放器在解码前检查 ``gpu_active``，自动走原 RGB 路径。
"""
from __future__ import annotations

import logging

import numpy as np

from PyQt6.QtCore import QRectF, Qt
from PyQt6.QtGui import QImage, QPainter, QSurfaceFormat
from PyQt6.QtOpenGLWidgets import QOpenGLWidget

from .yuv_frame import YuvFrame

logger = logging.getLogger(__name__)


def _h273_matrix_to_uploader(matrix: int) -> int:
    """H.273 matrix_coefficients → YUVGPUUploader 矩阵常量。

    1 = BT.709；5/6 = BT.601 系；9 = BT.2020 non-constant luminance。
    未识别值按 BT.709 处理（预览不阻断，和项目现有视频路径一致）。
    """
    if int(matrix) == 9:
        from src.shared.video.yuv_gpu_upload import YUVGPUUploader
        return YUVGPUUploader.MATRIX_BT2020
    if int(matrix) in (5, 6):
        from src.shared.video.yuv_gpu_upload import YUVGPUUploader
        return YUVGPUUploader.MATRIX_BT601
    from src.shared.video.yuv_gpu_upload import YUVGPUUploader
    return YUVGPUUploader.MATRIX_BT709


def _flip_rows(plane) -> np.ndarray:
    """垂直翻转并返回紧凑数组（OpenGL 纹理行序与 QImage 相反）。"""
    return np.ascontiguousarray(plane[::-1])


class GLVideoCanvas(QOpenGLWidget):
    """播放器 GPU 画布；接口与 ``base_player._VideoCanvas`` 对齐。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        fmt = QSurfaceFormat()
        fmt.setRenderableType(QSurfaceFormat.RenderableType.OpenGL)
        fmt.setVersion(3, 3)
        fmt.setProfile(QSurfaceFormat.OpenGLContextProfile.CoreProfile)
        fmt.setDepthBufferSize(0)
        fmt.setStencilBufferSize(0)
        fmt.setSamples(0)
        self.setFormat(fmt)

        self._frame: YuvFrame | QImage | None = None
        self._ctx = None
        self._uploader = None
        self._gpu_active = False
        self._gpu_error = ''
        self._frame_index = 0
        self._uploaded_frame = None
        self._textures = None
        self._chroma_scale = (1.0, 1.0)
        self._chroma_offset = (0.0, 0.0)
        self._on_click = None
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)

    # -- 兼容普通画布的接口 --

    @property
    def gpu_active(self) -> bool:
        """True 表示 GL context 与 uploader 都就绪。"""
        return self._gpu_active

    @property
    def gpu_error(self) -> str:
        return self._gpu_error

    def refresh_size_cache(self) -> None:
        """与 ``_VideoCanvas`` 同名接口；本画布直接读实时尺寸。"""

    def closeEvent(self, event) -> None:
        self._release_gl()
        super().closeEvent(event)

    def set_image(self, image) -> None:
        """接收 QImage（RGB 回退）或 YuvFrame（GPU 主路径）。"""
        self._frame = image
        self.update()

    def display_size(self):
        """返回设备像素下的显示区尺寸（对齐 base_player 的约定）。"""
        dpr = float(self.devicePixelRatioF())
        return (max(1, int(self.width() * dpr)),
                max(1, int(self.height() * dpr)))

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self._on_click:
            self._on_click()
        super().mouseReleaseEvent(event)

    # -- GL 生命周期 --

    def initializeGL(self) -> None:
        try:
            import moderngl
            from src.shared.video.yuv_gpu_upload import YUVGPUUploader

            self._ctx = moderngl.create_context()
            # 3 个槽位足够环形缓冲消费；R16UI 纹理在 uploader 内部按位深选择。
            self._uploader = YUVGPUUploader(self._ctx, pool_size=4, dtype='f2')
            self._gpu_active = True
            logger.info('[GL-CANVAS] ModernGL context ready: %s',
                        getattr(self._ctx, 'version_code', '?'))
        except Exception as exc:
            self._gpu_active = False
            self._gpu_error = f'{type(exc).__name__}: {exc}'
            logger.warning('[GL-CANVAS] GPU path unavailable, RGB fallback: %s',
                           self._gpu_error, exc_info=True)

    def resizeGL(self, w: int, h: int) -> None:
        # 尺寸变化只触发重绘；纹理仍复用，避免每帧重复上传。
        self.update()

    def paintGL(self) -> None:
        frame = self._frame
        if frame is None:
            self._paint_black()
            return
        if isinstance(frame, YuvFrame) and self._gpu_active:
            try:
                self._paint_yuv(frame)
                return
            except Exception as exc:
                # 单帧 GL 失败不能让播放器黑屏：降级为 RGB 由上层下一帧接回。
                self._gpu_active = False
                self._gpu_error = f'{type(exc).__name__}: {exc}'
                logger.warning('[GL-CANVAS] frame render failed: %s',
                               self._gpu_error, exc_info=True)
        if isinstance(frame, QImage):
            self._paint_qimage(frame)
        else:
            self._paint_black()

    def _release_gl(self) -> None:
        uploader = self._uploader
        self._uploader = None
        self._gpu_active = False
        if uploader is None:
            return
        try:
            self.makeCurrent()
            uploader.release()
        except Exception:
            logger.debug('[GL-CANVAS] uploader release failed', exc_info=True)
        finally:
            try:
                self.doneCurrent()
            except Exception:
                pass
        self._ctx = None

    # -- 绘制 --

    def _current_fbo(self):
        """取 QOpenGLWidget 当前绑定的默认 FBO。

        ``QOpenGLWidget`` 在 paintGL 前会把内部 FBO 绑定为当前 framebuffer；
        ``ctx.detect_framebuffer()`` 读取当前绑定。极少数 Qt 后端在调用
        paintGL 前解绑，此时回退到 ``ctx.screen``（窗口默认 FBO）。
        """
        try:
            return self._ctx.detect_framebuffer()
        except Exception:
            return self._ctx.screen

    def _paint_black(self) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), Qt.GlobalColor.black)
        painter.end()

    def _paint_qimage(self, image: QImage) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), Qt.GlobalColor.black)
        if not image.isNull():
            src = QRectF(0, 0, image.width(), image.height())
            dst = self._fit_rect(image.width(), image.height())
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
            painter.drawImage(dst, image, src)
        painter.end()

    def _fit_rect(self, width: int, height: int) -> QRectF:
        if width <= 0 or height <= 0:
            return QRectF(self.rect())
        ww, wh = self.width(), self.height()
        scale = min(ww / width, wh / height)
        w, h = width * scale, height * scale
        return QRectF((ww - w) / 2.0, (wh - h) / 2.0, w, h)

    def _paint_yuv(self, frame: YuvFrame) -> None:
        if not frame.is_valid():
            raise ValueError('YUV plane geometry does not match subsampling')

        ctx = self._ctx
        fbo = self._current_fbo()
        fbo.use()

        # 先整体黑底，再只在 letterbox 视口内画视频，避免上下黑边被画面填满。
        dpr = float(self.devicePixelRatioF())
        fw = max(1, int(self.width() * dpr))
        fh = max(1, int(self.height() * dpr))
        ctx.viewport = (0, 0, fw, fh)
        fbo.clear(0.0, 0.0, 0.0, 1.0)

        scale = min(fw / frame.width, fh / frame.height)
        vw = max(1, int(frame.width * scale))
        vh = max(1, int(frame.height * scale))
        vx = (fw - vw) // 2
        vy = (fh - vh) // 2
        ctx.viewport = (vx, vy, vw, vh)

        if frame is not self._uploaded_frame:
            self._upload_planes(frame)
            self._uploaded_frame = frame

        from src.shared.video.yuv_gpu_upload import (
            topos_chroma_sampling_params,
            YUVGPUUploader,
        )

        if frame.subsampling == '422':
            self._chroma_scale, self._chroma_offset = topos_chroma_sampling_params(
                '422', frame.source_width or frame.width,
                frame.source_height or frame.height, frame.chroma_siting)
        else:
            self._chroma_scale = (1.0, 1.0)
            self._chroma_offset = (0.0, 0.0)

        self._uploader.set_yuv_matrix(_h273_matrix_to_uploader(frame.matrix))
        self._uploader.set_color_range(
            YUVGPUUploader.RANGE_FULL if frame.color_range else
            YUVGPUUploader.RANGE_LIMITED)
        y_tex, u_tex, v_tex = self._textures[:3]
        self._uploader.render_yuv_to_rgb(
            y_tex, u_tex, v_tex, fbo,
            chroma_uv_scale=self._chroma_scale,
            chroma_uv_offset=self._chroma_offset,
            bit_depth=int(frame.bit_depth),
            gbr_mode=('gbr' in frame.pixel_format),
        )

    def _upload_planes(self, frame: YuvFrame) -> None:
        self._frame_index += 1
        # OpenGL 纹理首行对应 v=0（quad 底部），而 numpy 行 0 是画面顶部。
        # 上传前做一次垂直翻转，避免画面上下颠倒；翻转同时获得紧凑数组，
        # 这次拷贝与 uploader 内部的 ascontiguousarray 合并成一次。
        textures = self._uploader.upload_yuv_planar(
            _flip_rows(frame.y), _flip_rows(frame.u), _flip_rows(frame.v),
            frame_index=self._frame_index,
            bit_depth=int(frame.bit_depth),
            subsampling=frame.subsampling,
        )
        if not textures or len(textures) < 3:
            raise RuntimeError('YUV upload returned no textures')
        self._textures = textures
