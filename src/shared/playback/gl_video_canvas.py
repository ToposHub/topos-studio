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
[E5/FUP-07 整改] YUV 上传/转换/绘制移入专用渲染线程
（``gl_render_worker.GLVideoRenderWorker``：共享上下文 + latest-wins 命令
队列 + 双缓冲前台帧 + deferred release）。GUI 线程的 ``paintGL`` 只做
呈现：raw bind 前台纹理名 + 贴图四边形——不再在 GUI 线程创建/上传/
绘制视频内容。QOpenGLWidget 的 ``initializeGL/paintGL`` 仍发生在 GUI
线程（Qt 硬性约束，widget 表面绑定 GUI），但重活已离线程。

渲染线程初始化失败（远程桌面/共享组不可用等）或渲染中失败时，画布自动
回退**既有 GUI 直绘路径**（原 ``_paint_yuv`` 实现保留，行为不变）。

失败回退
--------
离屏测试、远程桌面、无 GL 的机器上 ``initializeGL`` 可能拿不到
context。此时 ``gpu_active`` 保持 False，画布仍可按普通 QImage 绘制；
播放器在解码前检查 ``gpu_active``，自动走原 RGB 路径。
"""
from __future__ import annotations

import logging

import numpy as np

from PyQt6.QtCore import QRectF, Qt, pyqtSlot
from PyQt6.QtGui import QImage, QPainter, QSurfaceFormat
from PyQt6.QtOpenGLWidgets import QOpenGLWidget

from .yuv_frame import YuvFrame

logger = logging.getLogger(__name__)

_BLIT_VERT = """
#version 330 core
in vec2 in_vert;
in vec2 in_uv;
out vec2 v_uv;
void main() { v_uv = in_uv; gl_Position = vec4(in_vert, 0.0, 1.0); }
"""
_BLIT_FRAG = """
#version 330 core
in vec2 v_uv;
out vec4 frag;
uniform sampler2D u_tex;
void main() { frag = texture(u_tex, v_uv); }
"""


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


_GL_TEXTURE0 = 0x84C0
_GL_TEXTURE_2D = 0x0DE1
_gl_module = None


def _raw_gl_binder():
    """跨上下文纹理 raw bind 的最小 GL 接口（按可用性择一）。

    1. ``QOpenGLFunctions_4_1_Core``（repo 既有模式，composite/gl_utils 同款）；
    2. ctypes 直载系统 GL（PyQt6 构建未暴露 QOpenGLFunctions 时的兜底）。
    返回 ``(glActiveTexture, glBindTexture)`` 或 None。
    """
    global _gl_module
    if _gl_module is not None:
        return _gl_module or None
    try:
        from PyQt6.QtOpenGL import QOpenGLFunctions_4_1_Core

        gl = QOpenGLFunctions_4_1_Core()
        gl.initializeOpenGLFunctions()
        _gl_module = (gl.glActiveTexture, gl.glBindTexture)
        return _gl_module
    except Exception:
        pass
    try:
        import ctypes
        import ctypes.util

        for name in ("OpenGL", ctypes.util.find_library("OpenGL") or ""):
            if not name:
                continue
            try:
                lib = ctypes.CDLL(
                    "/System/Library/Frameworks/OpenGL.framework/OpenGL"
                    if name == "OpenGL" and __import__("sys").platform == "darwin"
                    else name)
                lib.glActiveTexture.argtypes = [ctypes.c_uint]
                lib.glBindTexture.argtypes = [ctypes.c_uint, ctypes.c_uint]
                _gl_module = (lib.glActiveTexture, lib.glBindTexture)
                return _gl_module
            except Exception:
                continue
    except Exception:
        pass
    _gl_module = False
    return None


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
        # [E5] 渲染线程路径：worker 存活时上传/转换/绘制全在渲染线程；
        # 失败回退 GUI 直绘（_uploader 懒建）
        self._render_worker = None
        self._blit_prog = None
        self._blit_vao = None
        self._presented_array = None   # CPU 前台帧缓冲（绘制期间保活）
        self._size_cache = None   # [FR-21] 显示尺寸快照（GUI 线程维护）
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
        """[FR-21] GUI 线程更新尺寸快照：解码线程只读缓存。

        ``width()/devicePixelRatioF()`` 属 QWidget 主线程 API，解码线程
        直接调用会读到撕裂值并触屏屏幕对象查询（对齐 _VideoCanvas 的
        U10 缓存方案）。resizeGL/paintGL 都在 GUI 线程，顺手刷新。
        """
        w, h = self.width(), self.height()
        if w <= 0 or h <= 0:
            self._size_cache = None
        else:
            dpr = float(self.devicePixelRatioF())
            self._size_cache = (max(1, int(w * dpr)), max(1, int(h * dpr)))

    def closeEvent(self, event) -> None:
        self._release_gl()
        super().closeEvent(event)

    def set_image(self, image: YuvFrame | QImage | None) -> None:
        """接收 QImage（RGB 回退）或 YuvFrame（GPU 主路径）。

        [E5] worker 存活时 YuvFrame 只入队（渲染线程消费），GUI 不做
        任何 GL；QImage 路径不变（CPU 贴图）。
        """
        self._frame = image
        if isinstance(image, YuvFrame) and self._worker_alive():
            self._render_worker.submit_frame(image)
        else:
            self.update()

    @pyqtSlot(int)
    def _on_frame_ready(self, _frame_id: int) -> None:
        if isinstance(self._frame, YuvFrame):
            self.update()

    def display_size(self):
        """设备像素显示区尺寸（快照）；工作线程安全。"""
        return getattr(self, '_size_cache', None)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self._on_click:
            self._on_click()
        super().mouseReleaseEvent(event)

    # -- GL 生命周期 --

    def _worker_alive(self) -> bool:
        return self._render_worker is not None and self._render_worker.alive

    def initializeGL(self) -> None:
        try:
            import moderngl

            self._ctx = moderngl.create_context()
        except Exception as exc:
            self._gpu_active = False
            self._gpu_error = f'{type(exc).__name__}: {exc}'
            logger.warning('[GL-CANVAS] GPU path unavailable, RGB fallback: %s',
                           self._gpu_error, exc_info=True)
            return
        # [E5] 首选渲染线程路径：共享上下文的 worker 承担上传/转换/绘制
        from .gl_render_worker import GLVideoRenderWorker

        worker = GLVideoRenderWorker(self.context(), parent=self)
        if worker.start():
            self._render_worker = worker
            worker.worker_failed.connect(self._on_worker_failed)
            worker.frame_ready.connect(self._on_frame_ready, Qt.ConnectionType.QueuedConnection)
            if self._size_cache:
                worker.submit_size(self._size_cache)
            self._gpu_active = True
            logger.info('[GL-CANVAS] render worker ready (thread=%s)',
                        worker.render_thread_name)
            return
        logger.warning('[GL-CANVAS] render worker unavailable (%s); '
                       'GUI direct path', worker.init_error)
        worker.deleteLater()
        self._init_direct_uploader()

    def _init_direct_uploader(self) -> None:
        """[E5] 渲染线程不可用时的 GUI 直绘回退（原实现，行为不变）。"""
        if self._ctx is None:
            self._gpu_active = False
            return
        try:
            from src.shared.video.yuv_gpu_upload import YUVGPUUploader

            self._uploader = YUVGPUUploader(self._ctx, pool_size=4, dtype='f2')
            self._gpu_active = True
        except Exception as exc:
            self._gpu_active = False
            self._gpu_error = f'{type(exc).__name__}: {exc}'
            logger.warning('[GL-CANVAS] direct uploader init failed: %s',
                           self._gpu_error, exc_info=True)

    def _on_worker_failed(self, reason: str) -> None:
        # 渲染线程退役：下一帧起走 GUI 直绘（_uploader 懒建）
        logger.warning('[GL-CANVAS] render worker failed (%s); '
                       'falling back to GUI direct path', reason)
        self._gpu_error = reason
        self.update()

    def resizeGL(self, w: int, h: int) -> None:
        # 尺寸变化只触发重绘；纹理仍复用，避免每帧重复上传。
        self.refresh_size_cache()
        if self._worker_alive() and self._size_cache:
            self._render_worker.submit_size(self._size_cache)
            if isinstance(self._frame, YuvFrame):
                self._render_worker.submit_frame(self._frame)
        self.update()

    def paintGL(self) -> None:
        self.refresh_size_cache()  # GUI 线程内维护快照（FR-21）
        frame = self._frame
        if frame is None:
            self._paint_black()
            return
        if isinstance(frame, YuvFrame):
            if self._worker_alive():
                self._present_front()   # 只呈现已完成的前台帧
                return
            if self._gpu_active:
                try:
                    if self._uploader is None:
                        self._init_direct_uploader()
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

    def _present_front(self) -> None:
        """[E5] 呈现渲染线程已完成的前台帧。

        * ``("texture", gl_name, w, h)``：共享组可用——raw bind + 贴图
          四边形（零拷贝）；
        * ``("cpu", rgba, w, h)``：无共享组（macOS 常见）——渲染线程已
          回读，GUI 只把呈现结果按 QImage 贴出；
        没有前台帧（首帧在途）时画黑底——不阻塞等渲染线程。
        """
        info = self._render_worker.front_texture()
        if info is None:
            self._paint_black()
            return
        mode, payload, width, height, _frame_id = info
        try:
            if mode == "cpu":
                image = QImage(payload.data, width, height,
                               width * 4, QImage.Format.Format_RGBA8888)
                self._presented_array = payload   # 绘制期间保持缓冲存活
                self._paint_qimage(image)
                return
            import moderngl

            if self._blit_prog is None:
                self._blit_prog = self._ctx.program(
                    vertex_shader=_BLIT_VERT, fragment_shader=_BLIT_FRAG)
                vbo = self._ctx.buffer(np.array([
                    -1.0, -1.0, 0.0, 0.0,
                    1.0, -1.0, 1.0, 0.0,
                    -1.0, 1.0, 0.0, 1.0,
                    1.0, 1.0, 1.0, 1.0,
                ], dtype='f4'))
                self._blit_vao = self._ctx.vertex_array(
                    self._blit_prog, [(vbo, '2f 2f', 'in_vert', 'in_uv')])
            # 跨上下文纹理：同一 share group 的 GL 名，raw bind 采样
            # （与 image_engine/composite 消费跨上下文纹理的既有模式一致）
            binder = _raw_gl_binder()
            if binder is None:
                raise RuntimeError("no raw GL binder available")
            gl_active, gl_bind = binder
            gl_active(_GL_TEXTURE0)
            gl_bind(_GL_TEXTURE_2D, int(payload))
            self._blit_prog['u_tex'].value = 0
            self._ctx.viewport = (0, 0, max(1, self.width() * int(
                self.devicePixelRatioF())), max(1, self.height() * int(
                    self.devicePixelRatioF())))
            self._blit_vao.render(moderngl.TRIANGLE_STRIP)
            gl_active(_GL_TEXTURE0)
            gl_bind(_GL_TEXTURE_2D, 0)
        except Exception as exc:
            logger.warning('[GL-CANVAS] present failed: %s', exc, exc_info=True)
            self._paint_black()

    def _release_gl(self) -> None:
        # [E5] 先停渲染线程（其资源在渲染线程释放）；再收 GUI 侧呈现资源
        worker, self._render_worker = self._render_worker, None
        if worker is not None:
            try:
                worker.stop()
            except Exception:
                logger.debug('[GL-CANVAS] worker stop failed', exc_info=True)
            worker.deleteLater()
        uploader = self._uploader
        self._uploader = None
        self._gpu_active = False
        try:
            if self._blit_vao is not None or self._blit_prog is not None:
                self.makeCurrent()
                if self._blit_vao is not None:
                    self._blit_vao.release()
                if self._blit_prog is not None:
                    self._blit_prog.release()
                self._blit_vao = self._blit_prog = None
        except Exception:
            logger.debug('[GL-CANVAS] blit release failed', exc_info=True)
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
