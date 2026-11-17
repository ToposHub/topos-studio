"""E5/FUP-07：播放器 GPU 渲染线程——共享上下文、命令队列、前台帧呈现。

所有权模型与 AsyncRenderer 一致（复用协议，不共用实例——播放器画布与
Color 页渲染器宿主/生命周期不同，强行共用只会得到参数泥球）：

* **渲染线程独占 GL**：YUV 平面上传、YUV→RGB 转换、letterbox 绘制全部
  发生在专用 ``QThread`` 的共享上下文里（``QOpenGLContext.setShareContext``
  挂进画布的 share group）；GUI 线程只提交 ``YuvFrame``/尺寸快照并呈现
  已完成的前台帧（raw bind + 贴图四边形）。
* **latest-wins 命令队列**：渲染线程按队列驱动，排队的旧帧直接丢弃——
  UI 提交永不积压（对齐 AsyncRenderer 的 lane 合并语义）。
* **双缓冲前台帧**：渲染线程写后备 FBO，``ctx.finish()`` 后锁内换前；
  GUI 只读前台纹理（同一 share group，raw GL 名绑定采样）。
* **deferred release**：资源创建时登记 ``GLResourceRegistry``（带
  ``release_fn``）；正常关停在**拥有 context 的渲染线程**释放并销账，
  释放失败保留登记可重试（AUD-1300 语义）。
* **失败回退**：渲染线程初始化/渲染失败不致命——worker 退役，画布回
  退既有 GUI 直绘路径（``gl_video_canvas`` 的原实现，行为不变）。

真机验证：本模块的测试在带 GL 的机器（cocoa/真显卡）上执行完整渲染
链路与像素断言；无 GL 环境（CI offscreen）自动跳过——与
``tools/verify_native_gpu_preview.py`` 的口径一致，不冒充已验证。
"""
from __future__ import annotations

import logging
import queue
import threading
from typing import Any, Callable, Optional

import numpy as np

from PyQt6.QtCore import QObject, QThread, pyqtSignal

from .gl_video_canvas import _flip_rows, _h273_matrix_to_uploader
from .yuv_frame import YuvFrame
from src.shared.gpu.gl_resource_registry import (
    GLResourceRegistry, GROUP_FRAMEBUFFERS, GROUP_UPLOAD,
)

logger = logging.getLogger(__name__)


class _RenderThread(QThread):
    """渲染线程：GL 初始化、命令循环、所属线程释放都在 run() 内。"""

    def __init__(self, worker: "GLVideoRenderWorker", base_context) -> None:
        super().__init__()
        self._worker = worker
        self._base_context = base_context
        self.state: dict[str, Any] = {}   # 渲染线程私有；GUI 只经 probe 读

    # -- 线程体 ----------------------------------------------------------

    def run(self) -> None:  # noqa: C901 - 线程主干，分支即阶段
        worker = self._worker
        try:
            self._init_gl(self.state)
        except Exception as exc:
            worker._init_error = f"{type(exc).__name__}: {exc}"
            worker._ready.set()
            logger.warning("[GL-WORKER] init failed: %s", worker._init_error)
            return
        worker._ready.set()
        try:
            self._loop(self.state)
        finally:
            self._teardown_gl(self.state)
            worker._alive = False

    def _init_gl(self, state: dict[str, Any]) -> None:
        from PyQt6.QtGui import QOpenGLContext

        worker = self._worker
        worker._render_thread_name = threading.current_thread().name
        surface = worker._surface   # 已在 start() 中归本线程
        surface.create()
        ctx = QOpenGLContext()
        ctx.setShareContext(worker._base_context)   # 挂进画布 share group
        ctx.setFormat(worker._base_context.format())
        if not ctx.create():
            raise RuntimeError("shared QOpenGLContext create failed")
        if not ctx.makeCurrent(surface):
            raise RuntimeError("worker makeCurrent(offscreen) failed")
        import moderngl

        state["qt_ctx"] = ctx
        state["mctx"] = moderngl.create_context()
        # 共享组探测：macOS NSOpenGL 常见"falling back to unshared context"
        # （Qt 警告）——无共享组时 GUI 采样不到 worker 纹理，前台帧改走
        # CPU 回读发布（转换仍在渲染线程；呈现层收 QImage 结果）
        state["shared"] = ctx.shareGroup() is not None
        if not state["shared"]:
            logger.info("[GL-WORKER] no share group; front frames "
                        "published via CPU readback (GPU work still "
                        "off the GUI thread)")
        from src.shared.video.yuv_gpu_upload import YUVGPUUploader

        state["uploader"] = YUVGPUUploader(state["mctx"], pool_size=4, dtype="f2")
        worker._registry.register(
            GROUP_UPLOAD, "yuv-uploader",
            release_fn=lambda: state["uploader"].release())
        state["fbos"] = {}          # slot -> (texture, fbo, w, h)
        worker._registry.register(
            GROUP_FRAMEBUFFERS, "front-back-fbos",
            release_fn=lambda: self._release_fbos(state))
        state["textures"] = None    # 最近一次上传的 Y/U/V 纹理
        state["last_frame"] = None
        state["size"] = worker._pending_size
        worker._alive = True

    def _loop(self, state: dict[str, Any]) -> None:
        q = self._worker._queue
        while True:
            try:
                cmd = q.get(timeout=0.1)
            except queue.Empty:
                continue
            stop = False
            frame: Optional[YuvFrame] = None
            probe: Optional[tuple[queue.Queue, Callable]] = None
            while True:  # 排空：latest-wins，只留最后一个 frame/size/probe
                kind = cmd[0]
                if kind == "frame":
                    frame = cmd[1]
                elif kind == "size":
                    state["size"] = cmd[1]
                elif kind == "probe":
                    probe = (cmd[1], cmd[2])
                elif kind == "stop":
                    stop = True
                try:
                    cmd = q.get_nowait()
                except queue.Empty:
                    break
            if stop:
                return
            if probe is not None:
                result_q, fn = probe
                try:
                    result_q.put(("ok", fn(state)))
                except Exception as exc:  # noqa: PERF203 - 单项失败可继续
                    result_q.put(("error", exc))
            if frame is not None:
                try:
                    self._render_frame(state, frame)
                except Exception as exc:
                    # 渲染失败：worker 退役（画布回退 GUI 直绘），不带着
                    # 坏状态继续
                    self._worker._fail(f"render: {type(exc).__name__}: {exc}")
                    logger.warning("[GL-WORKER] render failed", exc_info=True)
                    return

    # -- 渲染 ----------------------------------------------------------

    def _ensure_fbo(self, state: dict[str, Any], slot: str,
                    width: int, height: int):
        import moderngl

        entry = state["fbos"].get(slot)
        if entry is not None and entry[2] == width and entry[3] == height:
            return entry
        if entry is not None:
            entry[0].release()
            entry[1].release()
        mctx = state["mctx"]
        texture = mctx.texture((width, height), 4, dtype="f1")
        texture.filter = (moderngl.LINEAR, moderngl.LINEAR)
        fbo = mctx.framebuffer(color_attachments=[texture])
        state["fbos"][slot] = (texture, fbo, width, height)
        return state["fbos"][slot]

    def _render_frame(self, state: dict[str, Any], frame: YuvFrame) -> None:
        if not frame.is_valid():
            raise ValueError("YUV plane geometry does not match subsampling")
        worker = self._worker
        fw, fh = state["size"] or (frame.width, frame.height)
        fw, fh = max(2, int(fw)), max(2, int(fh))
        back = self._ensure_fbo(state, "back", fw, fh)
        # 双缓冲：换前时前台按需重建到当前尺寸
        front = self._ensure_fbo(state, "front", fw, fh)
        texture, fbo, _, _ = back

        mctx = state["mctx"]
        fbo.use()
        mctx.viewport = (0, 0, fw, fh)
        fbo.clear(0.0, 0.0, 0.0, 1.0)
        # letterbox：与 gl_video_canvas._paint_yuv 同一几何（等比、黑边）
        scale = min(fw / frame.width, fh / frame.height)
        vw = max(1, int(frame.width * scale))
        vh = max(1, int(frame.height * scale))
        mctx.viewport = ((fw - vw) // 2, (fh - vh) // 2, vw, vh)

        if frame is not state["last_frame"]:
            worker._frame_index += 1
            state["textures"] = state["uploader"].upload_yuv_planar(
                _flip_rows(frame.y), _flip_rows(frame.u), _flip_rows(frame.v),
                frame_index=worker._frame_index,
                bit_depth=int(frame.bit_depth),
                subsampling=frame.subsampling,
            )
            state["last_frame"] = frame
        if not state["textures"] or len(state["textures"]) < 3:
            raise RuntimeError("YUV upload returned no textures")

        from src.shared.video.yuv_gpu_upload import (
            topos_chroma_sampling_params, YUVGPUUploader,
        )
        if frame.subsampling == "422":
            chroma_scale, chroma_offset = topos_chroma_sampling_params(
                "422", frame.source_width or frame.width,
                frame.source_height or frame.height, frame.chroma_siting)
        else:
            chroma_scale, chroma_offset = (1.0, 1.0), (0.0, 0.0)
        uploader = state["uploader"]
        uploader.set_yuv_matrix(_h273_matrix_to_uploader(frame.matrix))
        uploader.set_color_range(
            YUVGPUUploader.RANGE_FULL if frame.color_range
            else YUVGPUUploader.RANGE_LIMITED)
        y_tex, u_tex, v_tex = state["textures"][:3]
        uploader.render_yuv_to_rgb(
            y_tex, u_tex, v_tex, fbo,
            chroma_uv_scale=chroma_scale,
            chroma_uv_offset=chroma_offset,
            bit_depth=int(frame.bit_depth),
            gbr_mode=("gbr" in frame.pixel_format),
        )
        # 发布前显式同步：share group 无跨上下文 fence 的诚实版本——
        # 完成即提交（finish 开销换正确性；帧率由 latest-wins 吸收）
        mctx.finish()
        if state["shared"]:
            published_payload = ("texture", texture.glo, fw, fh)
        else:
            # 无共享组：渲染线程回读 RGBA（GUI 无法采样 worker 纹理；
            # 回读阻塞的是渲染线程，不是 GUI）
            rgba = np.frombuffer(
                fbo.read(components=4, dtype="f1", alignment=1),
                dtype=np.uint8).reshape(fh, fw, 4)
            published_payload = ("cpu", rgba, fw, fh)
        with worker._front_lock:
            # 前后台互换：GUI 只读 front，worker 只写 back；同时发布
            # 线程安全的前台快照——GUI 呈现不跨线程触碰 ModernGL 对象，
            # 也不用阻塞等待渲染线程
            state["fbos"]["front"] = back
            state["fbos"]["back"] = front
            worker._published_id += 1
            published = worker._published_id
            worker._front_info = (*published_payload, published)
        worker.rendered_frames += 1
        worker.frame_ready.emit(published)

    # -- 释放 ----------------------------------------------------------

    def _release_fbos(self, state: dict[str, Any]) -> None:
        for texture, fbo, _, _ in list(state.get("fbos", {}).values()):
            try:
                fbo.release()
            finally:
                texture.release()
        state["fbos"] = {}

    def _teardown_gl(self, state: dict[str, Any]) -> None:
        worker = self._worker
        registry = worker._registry
        # 正常关停：在拥有 context 的本线程释放并销账；失败保留登记
        # （deferred-release 重试语义，AUD-1300）
        for group, name in ((GROUP_FRAMEBUFFERS, "front-back-fbos"),
                            (GROUP_UPLOAD, "yuv-uploader")):
            record = registry.get(group, name)
            if record is None or record.release_fn is None:
                continue
            try:
                record.release_fn()
                registry.unregister(group, name)
            except Exception:
                logger.exception("[GL-WORKER] release failed; kept for retry: "
                                 "%s/%s", group, name)
        qt_ctx = state.pop("qt_ctx", None)
        state.pop("mctx", None)
        state.pop("uploader", None)
        state.pop("textures", None)
        state.pop("last_frame", None)
        try:
            if qt_ctx is not None:
                qt_ctx.doneCurrent()
        except Exception:
            pass


class GLVideoRenderWorker(QObject):
    """GUI 侧句柄：提交/查询/关停；GL 一律不发生在 GUI 线程。"""

    frame_ready = pyqtSignal(int)       # 已发布前台帧编号（worker 线程 emit）
    worker_failed = pyqtSignal(str)

    def __init__(self, base_context, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._base_context = base_context
        self._registry = GLResourceRegistry(owner="playback-gl-worker")
        self._queue: queue.Queue = queue.Queue()
        self._ready = threading.Event()
        self._front_lock = threading.Lock()
        self._alive = False
        self._init_error = ""
        self._render_thread_name = ""
        self._published_id = 0
        self._frame_index = 0
        self._front_info = None    # (gl_name, w, h, frame_id)：发布时快照
        self.rendered_frames = 0
        self._pending_size: Optional[tuple[int, int]] = None
        from PyQt6.QtGui import QOffscreenSurface

        # GUI 线程构造、渲染线程 create（Qt 线程契约见 _RenderThread）
        self._surface = QOffscreenSurface()
        self._thread = _RenderThread(self, base_context)

    # -- 生命周期（GUI 线程） -------------------------------------------

    def start(self, timeout: float = 5.0) -> bool:
        # Qt 契约：moveToThread 必须在对象当前线程（GUI）调用，且要先于
        # 线程启动——渲染线程内只负责 create()
        self._surface.moveToThread(self._thread)
        self._thread.start()
        if not self._ready.wait(timeout):
            self.stop()
            self._init_error = "worker init timeout"
            return False
        if self._init_error:
            self._thread.wait(2000)
            return False
        return True

    def stop(self) -> bool:
        """入队停止、有界 join。线程卡死时**不**强制释放（有界泄漏纪律，
        与 base_player.stop_decoder 同款）——返回是否确认退出。"""
        self._queue.put(("stop",))
        self._thread.wait(3000)
        return self._thread.isFinished()

    # -- 提交（GUI 线程，latest-wins 由渲染线程排空实现） ----------------

    def submit_frame(self, frame: YuvFrame) -> None:
        if self._alive:
            self._queue.put(("frame", frame))

    def submit_size(self, size: tuple[int, int]) -> None:
        self._pending_size = size
        if self._alive:
            self._queue.put(("size", size))

    # -- 查询（GUI 线程） -------------------------------------------------

    @property
    def alive(self) -> bool:
        return self._alive

    @property
    def init_error(self) -> str:
        return self._init_error

    @property
    def render_thread_name(self) -> str:
        return self._render_thread_name

    @property
    def registry(self) -> GLResourceRegistry:
        return self._registry

    def front_texture(self):
        """已完成前台帧 → ``(mode, payload, width, height, frame_id)`` | None。

        发布时由渲染线程写入的线程安全快照：共享组可用时
        ``("texture", gl_name, w, h, id)``（GUI raw bind 采样，零拷贝）；
        否则 ``("cpu", rgba_ndarray, w, h, id)``（渲染线程已回读，GUI
        只收呈现结果）。GUI 不跨线程触碰 ModernGL 对象、不阻塞等渲染。
        """
        with self._front_lock:
            return self._front_info

    def probe(self, fn: Callable[[dict], Any], timeout: float = 5.0):
        """在渲染线程执行 ``fn(state)`` 并返回结果（呈现/测试/诊断用）。"""
        result: queue.Queue = queue.Queue()
        if not self._alive:
            raise RuntimeError("worker not alive")
        self._queue.put(("probe", result, fn))
        status, value = result.get(timeout=timeout)
        if status == "error":
            raise value
        return value

    # -- 内部（渲染线程调用） ---------------------------------------------

    def _fail(self, reason: str) -> None:
        self._alive = False
        self._init_error = reason
        self.worker_failed.emit(reason)
