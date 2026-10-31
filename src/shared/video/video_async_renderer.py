"""
Video Async Renderer - 专门用于视频渲染的独立渲染器

设计原则：
- 完全独立于图片渲染管线 (AsyncRenderer)
- 只在需要时创建资源（延迟初始化）
- 提供清理方法确保状态隔离
- 轻量级设计，专注于视频帧的快速 GPU 上传和渲染

架构说明：
- 使用独立的 OpenGL 共享上下文
- 简化的渲染流程：仅处理视频帧上传和基础合成
- 支持代理分辨率渲染以加速播放
- 与 AsyncRenderer 共享底层着色器资源

使用方式：
    # 创建视频渲染器
    video_renderer = VideoAsyncRenderer(main_context)
    video_renderer.frame_ready.connect(on_frame_ready)
    video_renderer.start()
    
    # 提交视频帧
    video_renderer.submit(VideoRenderCommand.UPLOAD_FRAME, {
        'image': frame_float32,
        'frame_size': (width, height)
    })
    
    # 渲染到屏幕
    video_renderer.submit(VideoRenderCommand.RENDER_FRAME, {
        'viewport_size': (viewport_w, viewport_h)
    })
    
    # 清理
    video_renderer.stop_thread()
"""

import moderngl
import numpy as np
import queue
import threading
import time
import traceback
import logging
from typing import Optional, Tuple, Dict, Any, List
from PyQt6.QtCore import QThread, pyqtSignal, QMutex
from PyQt6.QtGui import QOpenGLContext, QOffscreenSurface
from src.shared.gpu.gl_backend import GLContextManager, FramebufferManager
from src.shared.video.frame_queue import (
    drain_pending_gpu_frame_releases,
    set_gpu_frame_release_thread,
)
from src.shared.video.video_texture_pool import VideoTexturePool
from src.shared.video.yuv_gpu_upload import YUVGPUUploader

logger = logging.getLogger(__name__)


class VideoRenderCommand:
    """视频渲染命令枚举"""
    UPLOAD_FRAME = "upload_frame"      # 上传视频帧 (RGB 格式)
    UPLOAD_YUV_FRAME = "upload_yuv_frame"  # 上传 YUV 平面帧 (减少带宽)
    RENDER_FRAME = "render_frame"      # 渲染帧到屏幕
    UPDATE_PARAMS = "update_params"    # 更新渲染参数
    CLEANUP = "cleanup"                # 清理资源


class VideoAsyncRenderer(QThread):
    """
    视频专用异步渲染器。

    与 AsyncRenderer 的区别：
    - 更简化的渲染管线（无图层合成、无遮罩生成）
    - 针对视频播放优化的帧上传路径
    - 独立的 OpenGL 上下文管理
    - 支持代理分辨率快速渲染

    使用场景：
    - 视频播放时的实时帧渲染
    - 需要快速响应而不影响图片渲染管线

    状态（计划 P3-05，2026-09）：v2 遗留入口，生产路径 v3 pipeline 已由
    AsyncRenderer 承担（video_playback 启用 v3 时显式跳过本类）。按
    「保留旧入口一段时间」的兼容策略暂不删除：离线工厂
    （timeline_preview_renderer）与既有调用方继续可用，新代码不得再
    依赖本类。
    """

    frame_ready = pyqtSignal(int)  # 渲染完成的纹理 ID
    error_occurred = pyqtSignal(str)  # 错误信号
    
    def __init__(self, main_context) -> None:
        """
        初始化视频渲染器。
        
        Args:
            main_context: 主 OpenGL 上下文，用于创建共享上下文
        """
        super().__init__()
        self.main_context = main_context
        self.command_queue = queue.Queue()
        
        # OpenGL 上下文相关
        self.ctx = None
        self.surface = None
        self.shared_context = None
        
        # 双缓冲（Ping-Pong）
        self.fbo_pair = [None, None]
        self.tex_pair = [None, None]
        self.write_idx = 0
        
        # 运行状态
        self.is_running = True
        self.setObjectName("VideoRenderThread")
        
        # 视频渲染状态
        self.current_frame_tex = None  # 当前视频帧纹理
        self.current_frame_index = -1  # 当前帧号（渲染线程写）
        self.frame_size = (0, 0)  # 当前帧尺寸
        self.render_params = {}  # 渲染参数

        # AUD-39515: 调用方线程侧的自动帧号计数。upload_yuv_and_render_frame
        # 在调用线程读渲染线程写的 current_frame_index 属数据竞争（帧号可能
        # 重复/跳号，干扰纹理池 LRU 新旧判断）；改为调用方自维护计数，
        # 仅受本锁保护，不再读渲染线程状态。
        self._submit_frame_index_lock = threading.Lock()
        self._submitted_frame_index = -1
        
        # P4.1: VideoTexturePool（在渲染线程初始化）
        self._video_texture_pool = None

        # P4.4: YUVGPUUploader（YUV 平面直接上传，减少带宽）
        self._yuv_gpu_uploader: Optional[YUVGPUUploader] = None

        # 当前 YUV 纹理（用于 YUV 渲染）
        self._yuv_y_tex = None
        self._yuv_u_tex = None
        self._yuv_v_tex = None
        self._yuv_uv_tex = None  # 用于 NV12 interleaved
        self._is_yuv_frame = False
        self._yuv_format = None  # '420', '422', '444', 'nv12', 'p010'

        # AUD-39501: YUV→RGB 合成目标（帧尺寸 FBO + 纹理）。
        # YUV 上传后必须把合成出的 RGB 帧写入 current_frame_tex，
        # RENDER_FRAME 只渲染 current_frame_tex。
        self._yuv_composite_fbo = None
        self._yuv_composite_tex = None

        # 资源管理
        self.fb_manager = None
        self.utility_shaders = {}
        self._initialized = False

        # AUD-39504: 池 acquire 失败时复用的 fallback 纹理（渲染线程）
        self._fallback_frame_tex = None
        self._fallback_frame_tex_desc: Optional[Tuple[int, int, int, str]] = None
        
        # 同步锁
        self._mutex = QMutex()
        
        # 帧转换缓冲区（预分配以避免每帧分配）
        self._convert_buffer = None
        self._buffer_size = (0, 0)
        
        # 在主线程创建离屏表面（Windows 兼容性）
        self.surface = QOffscreenSurface()
        self.surface.setFormat(main_context.format())
        self.surface.create()

        # 创建共享上下文。
        # AUD-39505: 此前这里对主上下文做 doneCurrent() → create() →
        # makeCurrent() 的干预：构造时机恰逢其他页面上下文活跃时会窃取
        # current 状态并导致崩溃（file_controller 曾专门规避此构造期行为，
        # 见其"VideoAsyncRenderer 在 Color 页面 GL 上下文仍活跃时做
        # doneCurrent/makeCurrent 导致崩溃"注释）。QOpenGLContext.create()
        # 与 share context 建立共享不要求对方处于非 current 状态，该干预
        # 没有必要——已移除，构造函数不再触碰任何上下文的 current 状态。
        self.shared_context = QOpenGLContext()
        self.shared_context.setFormat(self.main_context.format())
        self.shared_context.setShareContext(self.main_context)

        if self.shared_context.create():
            pass  # Context created successfully
        else:
            logger.error("ERROR: Failed to create shared context!")

        # 移交上下文所有权到渲染线程
        self.shared_context.moveToThread(self)

        # AUD-39507: weakref.finalize "GC 后备释放" 注册已移除。旧实现的
        # _gc_resources 列表从未被填充（finalizer 遍历空列表，注释承诺的
        # 兜底释放完全不生效）；而真正把 ModernGL 对象塞进该列表同样不可
        # 行——GC 在触发回收的任意线程运行，那里没有 current 的 GL 上下文，
        # 直接 release() 违反「GL 仅渲染线程」铁律（同型结论见
        # AUD-7605/video_frame_node）。资源销毁由显式路径负责：run() 退出
        # 时在本线程释放全部 GL 对象并 release/doneCurrent 上下文
        # （AUD-39506），或调用方 stop_thread()。

        # AUD-39508: 被视口重建替换的 FBO/纹理延迟释放队列。
        # frame_ready 发出的 glo 仍可能被 UI 共享上下文采样，立即释放
        # 会删除使用中的纹理名；超过宽限期后在渲染线程释放。
        self._retired_fbo_resources: List[Tuple[float, Any, Any]] = []

        # AUD-39511: 全屏四边形几何缓存（启动时创建一次，退出时释放）
        self._quad_vbo = None
        self._quad_ibo = None
        self._quad_vao = None
    
    def run(self):
        """渲染线程主循环"""
        mutex = GLContextManager.instance().mutex

        # 激活共享上下文
        if not self.shared_context.makeCurrent(self.surface):
            logger.error("Failed to make context current on RenderThread")
            self.error_occurred.emit("Failed to make OpenGL context current")
            return

        # 初始化 ModernGL
        try:
            self.ctx = moderngl.create_context()
            self.fb_manager = FramebufferManager(self.ctx)
            self._load_shaders()
            
            # P4.1: 初始化 VideoTexturePool
            self._video_texture_pool = VideoTexturePool(self.ctx, pool_size=5)

            # P4.4: 初始化 YUVGPUUploader
            self._yuv_gpu_uploader = YUVGPUUploader(self.ctx, pool_size=5)

            self._initialized = True
            logger.info(f"ModernGL initialized on {self.objectName()}")
        except Exception as e:
            logger.error(f"Failed to initialize ModernGL: {e}")
            self.error_occurred.emit(f"Failed to initialize ModernGL: {e}")
            return

        # AUD-39201: 本线程即视频渲染线程。注册为 GPU 帧释放所属线程：
        # ReadyFrameQueue 在其他线程（UI seek/unload、解码 worker）淘汰
        # GPU 帧时不再同步触发 release 回调，而是挂入 pending 由本线程
        # 在命令循环中排水释放（gpu_frame.py: release 只在渲染线程调）。
        set_gpu_frame_release_thread(threading.get_ident())
        try:
            # 命令循环
            while self.is_running:
                # AUD-39201: 每轮排水其他线程挂起的 GPU 帧释放
                drain_pending_gpu_frame_releases()
                # AUD-39508: 释放宽限期已过的退役 FBO/纹理
                self._drain_retired_fbo_resources()
                cmd_type: Optional[str] = None
                try:
                    # 使用短超时以支持退出检查
                    item = self.command_queue.get(timeout=0.01)
                    cmd_type, data = item

                    # 命令合并：跳过连续的 RENDER_FRAME 命令
                    if cmd_type == VideoRenderCommand.RENDER_FRAME:
                        while True:
                            # AUD-39514: 窥视队首必须经 Queue 自身的
                            # mutex（threading.Lock，不可重入），因此锁内
                            # 只做无副作用的读取，确认目标后再用公共 API
                            # get_nowait() 消费。本线程是该队列唯一消费者，
                            # 窥视与消费之间队首不会变化（生产者只从尾部
                            # 追加）。
                            with self.command_queue.mutex:
                                pending = (
                                    self.command_queue.queue[0]
                                    if self.command_queue.queue else None
                                )
                            if (
                                pending is None
                                or pending[0] != VideoRenderCommand.RENDER_FRAME
                            ):
                                break
                            try:
                                next_item = self.command_queue.get_nowait()
                            except queue.Empty:
                                break
                            item = next_item
                            cmd_type, data = item
                            self.command_queue.task_done()

                    # 执行命令
                    mutex.lock()
                    try:
                        self._handle_command(cmd_type, data)
                    finally:
                        mutex.unlock()

                    self.command_queue.task_done()

                except queue.Empty:
                    continue
                except Exception as e:
                    # AUD-39510: 命令级异常不得静默丢弃。此前仅打日志，
                    # 既不 emit error_occurred 也不重发 frame_ready，消费方
                    # 等待的帧永远不来且无从感知。命令循环边界保留宽捕获
                    # 以维持线程存活，但必须携带命令类型上报错误信号。
                    logger.error(f"Error in render loop: {e}")
                    traceback.print_exc()
                    cmd_label = cmd_type if isinstance(cmd_type, str) else 'unknown'
                    self.error_occurred.emit(
                        f"Render command failed [{cmd_label}]: {e}")
        finally:
            # AUD-39201: 线程退出前排空 pending 并注销注册（注销后未排空
            # 的帧会被立即释放，回到默认行为）。
            drain_pending_gpu_frame_releases()
            set_gpu_frame_release_thread(None)

        # AUD-39506: 先捕获 moderngl 上下文引用（_cleanup_gl_resources 会
        # 将 self.ctx 置 None），确保线程退出路径能显式 release。
        moderngl_ctx = self.ctx
        # 清理资源（GL 对象在上下文仍 current 时于本线程释放）
        self._cleanup_gl_resources()
        # AUD-39506: 释放 moderngl 上下文并 doneCurrent，保证 GL 上下文在
        # 渲染线程销毁而不是留给任意 GC 线程析构。
        self._release_gl_context_on_exit(moderngl_ctx)
        logger.info("Thread exited cleanly")
    
    def submit(self, cmd_type, data=None):
        """线程安全地提交渲染命令"""
        if not self.isRunning():
            self.start()
        self.command_queue.put((cmd_type, data))
    
    def upload_and_render_frame(self, frame: np.ndarray, viewport_size: Tuple[int, int],
                                  input_format: Optional[Dict[str, Any]] = None) -> None:
        """
        便捷方法：上传并渲染视频帧（合并两个命令）。

        支持多种输入格式：
        - uint8: 标准 8-bit 视频
        - uint16: 10/12-bit 高位深视频
        - float32: HDR 数据
        - float16: 内存优化的 HDR 数据

        Args:
            frame: 输入帧（BGR 或 RGB 格式）
            viewport_size: (width, height) 视口尺寸
            input_format: 可选的输入格式信息（自动检测如果未提供）
        """
        # 检测输入格式
        if input_format is None:
            input_format = self._detect_input_format(frame)

        # 转换为 float32（归一化到 [0, 1]）
        # AUD-39500: _convert_frame_to_float_generic 返回的是本线程复用的
        # 暂存缓冲（self._convert_buffer）。若把该引用直接排入队列，渲染
        # 线程消费上一条 UPLOAD_FRAME 前生产者处理下一帧会复写同一缓冲，
        # 造成帧撕裂/串帧。入队前必须拷贝出独立缓冲；暂存池仅在调用方
        # 线程内复用，绝不跨线程别名。
        frame_float = self._convert_frame_to_float_generic(frame, input_format).copy()
        h, w = frame.shape[:2]

        # 上传帧（带格式信息）
        self.submit(VideoRenderCommand.UPLOAD_FRAME, {
            'image': frame_float,
            'frame_size': (w, h),
            'input_format': input_format
        })

        # 渲染到屏幕
        self.submit(VideoRenderCommand.RENDER_FRAME, {
            'viewport_size': viewport_size
        })

    def upload_yuv_and_render_frame(
        self,
        y: np.ndarray,
        viewport_size: Tuple[int, int],
        u: Optional[np.ndarray] = None,
        v: Optional[np.ndarray] = None,
        uv: Optional[np.ndarray] = None,
        format: str = '420',
        bit_depth: int = 8,
        frame_index: int = -1,
        yuv_matrix: int = 0,
        color_range: int = 0
    ) -> None:
        """
        便捷方法：上传 YUV 平面并渲染（减少带宽消耗）。

        支持 YUV420P, YUV422P, YUV444P, NV12, P010 格式。
        直接上传 YUV 平面到 GPU，避免 CPU 端 YUV->RGB 转换。

        Args:
            y: Y 平面数据 (numpy.ndarray)
            u: U 平面数据（用于 planar 格式）
            v: V 平面数据（用于 planar 格式）
            uv: UV interleaved 数据（用于 NV12/P010）
            viewport_size: (width, height) 视口尺寸
            format: YUV 格式 ('420', '422', '444', 'nv12', 'p010')
            bit_depth: 位深度 (8, 10, 12, 16)
            frame_index: 帧号（默认自动递增）
            yuv_matrix: 转换矩阵 (0=BT.709, 1=BT.2020, 2=BT.601)
            color_range: 颜色范围 (0=limited, 1=full)

        带宽优化：
            - 4K RGB: 99.5 MB/frame
            - 4K YUV420: 12.4 MB/frame (减少 ~87.5%)
        """
        # AUD-39515: 自动帧号由调用方线程自维护（持锁递增），不读渲染
        # 线程写的 current_frame_index。显式 frame_index >= 0 时原样使用
        # 并同步推进计数，保持混合调用下单调。
        if frame_index >= 0:
            effective_frame_index = int(frame_index)
        else:
            with self._submit_frame_index_lock:
                self._submitted_frame_index += 1
                effective_frame_index = self._submitted_frame_index
        with self._submit_frame_index_lock:
            if effective_frame_index > self._submitted_frame_index:
                self._submitted_frame_index = effective_frame_index

        data = {
            'y': y,
            'format': format,
            'bit_depth': bit_depth,
            'frame_index': effective_frame_index,
            'yuv_matrix': yuv_matrix,
            'color_range': color_range,
        }

        # 根据格式添加平面数据
        if format in ('420', '422', '444'):
            if u is not None:
                data['u'] = u
            if v is not None:
                data['v'] = v
        elif format in ('nv12', 'p010'):
            if uv is not None:
                data['uv'] = uv

        # 上传 YUV 帧
        self.submit(VideoRenderCommand.UPLOAD_YUV_FRAME, data)

        # 渲染到屏幕
        self.submit(VideoRenderCommand.RENDER_FRAME, {
            'viewport_size': viewport_size
        })

    def _convert_frame_to_float(self, frame_bgr: np.ndarray) -> np.ndarray:
        """
        将 BGR uint8 帧转换为 RGB float32。
        使用预分配缓冲区以减少内存分配。

        注意：返回值是调用方线程私有的复用暂存缓冲（self._convert_buffer），
        下一帧转换时会就地复写。跨线程传递（如排入命令队列）前必须 .copy()
        出独立缓冲。

        Args:
            frame_bgr: BGR uint8 帧

        Returns:
            RGB float32 帧（归一化到 [0, 1]）
        """
        h, w = frame_bgr.shape[:2]

        # 按需分配或重用缓冲区
        if (self._convert_buffer is None or
            self._buffer_size[0] != h or
            self._buffer_size[1] != w):
            self._convert_buffer = np.empty((h, w, 3), dtype=np.float32)
            self._buffer_size = (h, w)

        # 优化：直接在预分配缓冲中转换，避免中间数组
        # BGR -> RGB 并归一化（逐通道操作，零分配）
        np.divide(frame_bgr[:, :, 0].astype(np.float32), 255.0, out=self._convert_buffer[:, :, 2])  # B -> R
        np.divide(frame_bgr[:, :, 1].astype(np.float32), 255.0, out=self._convert_buffer[:, :, 1])  # G -> G
        np.divide(frame_bgr[:, :, 2].astype(np.float32), 255.0, out=self._convert_buffer[:, :, 0])  # R -> B

        return self._convert_buffer
    

    def _detect_input_format(self, frame: np.ndarray) -> Dict[str, Any]:
        """
        检测输入帧的格式信息。

        Args:
            frame: 输入帧数组

        Returns:
            包含格式信息的字典：
            - 'dtype': numpy dtype 名称
            - 'components': 通道数 (3 或 4)
            - 'normalized': 是否已归一化到 [0, 1]
            - 'max_value': 最大值（用于归一化）
            - 'is_bgr': 是否为 BGR 格式
        """
        dtype_name = frame.dtype.name
        shape = frame.shape

        # 确定通道数
        components = 3
        if len(shape) == 3 and shape[2] == 4:
            components = 4

        # 假设 3 通道为 BGR 格式（OpenCV 默认）
        is_bgr = len(shape) == 3 and shape[2] == 3

        if dtype_name == 'uint8':
            return {
                'dtype': 'uint8',
                'components': components,
                'normalized': False,
                'max_value': 255.0,
                'is_bgr': is_bgr
            }
        elif dtype_name == 'uint16':
            # 检测位深度（10-bit = 1023, 12-bit = 4095, 16-bit = 65535）
            max_val = frame.max() if frame.size > 0 else 65535
            if max_val <= 1023:
                bit_depth = 10
                max_value = 1023.0
            elif max_val <= 4095:
                bit_depth = 12
                max_value = 4095.0
            else:
                bit_depth = 16
                max_value = 65535.0

            return {
                'dtype': 'uint16',
                'components': components,
                'normalized': False,
                'max_value': max_value,
                'bit_depth': bit_depth,
                'is_bgr': is_bgr
            }
        elif dtype_name in ('float32', 'float16'):
            # AUD-39503: HDR float 帧禁止按逐帧最大值归一化。此前对值域
            # 超出 [0, 1] 的 float 帧计算 max_value=frame.max()，任何按
            # 该值缩放的消费方都会把亮度逐帧重缩放（暗帧变亮、亮帧变暗），
            # 违反"HDR 不得无理由归一化"的铁律。float 输入一律按原值
            # 透传（线性 HDR），tone-map 由下游显示链路决定。
            return {
                'dtype': dtype_name,
                'components': components,
                'normalized': True,
                'max_value': 1.0,
                'is_bgr': is_bgr
            }
        else:
            # 未知类型，默认作为 uint8 处理
            logger.warning(f"Unknown dtype: {dtype_name}, treating as uint8")
            return {
                'dtype': dtype_name,
                'components': components,
                'normalized': False,
                'max_value': 255.0,
                'is_bgr': is_bgr
            }

    def _convert_frame_to_float_generic(self, frame: np.ndarray,
                                         input_format: Dict[str, Any]) -> np.ndarray:
        """
        通用帧转换方法：将任意 dtype 转换为 float32。

        注意：返回值通常是调用方线程私有的复用暂存缓冲
        （self._convert_buffer），下一帧转换时会就地复写。跨线程传递
        （如排入命令队列给渲染线程消费）前必须 .copy() 出独立缓冲。

        Args:
            frame: 输入帧
            input_format: 输入格式信息（由 _detect_input_format 返回）

        Returns:
            float32 帧（归一化到 [0, 1]，RGB 格式）
        """
        dtype = input_format['dtype']
        max_value = input_format['max_value']
        is_bgr = input_format['is_bgr']
        components = input_format['components']

        h, w = frame.shape[:2]

        # 对于 uint8，使用原有的优化方法
        if dtype == 'uint8':
            return self._convert_frame_to_float(frame)

        # 确保缓冲区大小正确
        if (self._convert_buffer is None or
            self._buffer_size[0] != h or
            self._buffer_size[1] != w or
            self._convert_buffer.shape[2] != components):
            self._convert_buffer = np.empty((h, w, components), dtype=np.float32)
            self._buffer_size = (h, w)

        # 根据输入类型处理
        if dtype == 'uint16':
            # uint16 -> float32，归一化
            if is_bgr:
                # BGR -> RGB + 归一化
                self._convert_buffer[:, :, 2] = frame[:, :, 0].astype(np.float32) / max_value  # B -> R
                self._convert_buffer[:, :, 1] = frame[:, :, 1].astype(np.float32) / max_value  # G -> G
                self._convert_buffer[:, :, 0] = frame[:, :, 2].astype(np.float32) / max_value  # R -> B
            else:
                # RGB，直接归一化
                self._convert_buffer = frame.astype(np.float32) / max_value
        elif dtype in ('float32', 'float16'):
            if is_bgr and components == 3:
                # BGR -> RGB
                if dtype == 'float16':
                    frame_f32 = frame.astype(np.float32)
                else:
                    frame_f32 = frame
                self._convert_buffer[:, :, 2] = frame_f32[:, :, 0]  # B -> R
                self._convert_buffer[:, :, 1] = frame_f32[:, :, 1]  # G -> G
                self._convert_buffer[:, :, 0] = frame_f32[:, :, 2]  # R -> B
            else:
                # 已经是 RGB 或 RGBA
                if dtype == 'float16':
                    self._convert_buffer[:] = frame.astype(np.float32)
                else:
                    self._convert_buffer[:] = frame

        return self._convert_buffer

    def _get_texture_format(self, input_format: Dict[str, Any]) -> str:
        """
        根据输入格式选择合适的纹理 dtype。

        AUD-39513 纹理精度策略（有意为之，非遗漏）：
        所有输入（uint8 / uint16 / float32 / float16）统一映射到 'f2'
        （float16 纹理）：
        - uint8/uint16（含 10/12-bit）: float16 的 11 位尾数足够无损承载
          量化后的 [0,1] 归一化值；
        - float32 HDR: 有意降为 float16 以节省一半显存与上传带宽（4K
          RGBA f4→f2 每帧省约 33MB）。代价是尾数 10 位，对极端平滑的
          >11 档亮度渐变可能引入轻微条带。内部管线是 float32（见
          _convert_frame_to_float_generic），只有落纹理这一步降精度；
          若未来需要全精度 HDR 纹理，应经显式参数开关选择 'f4'，而不是
          按输入 dtype 隐式切换。

        Args:
            input_format: 输入格式信息（_detect_input_format 返回）

        Returns:
            ModernGL 纹理 dtype 字符串（恒为 'f2'）
        """
        return 'f2'

    @staticmethod
    def _default_max_for_dtype(dtype: np.dtype) -> Optional[float]:
        """整数 dtype 的满量程值（用于归一化到 [0, 1]）。"""
        if dtype == np.uint8:
            return 255.0
        if dtype == np.uint16:
            return 65535.0
        if dtype == np.int8:
            return 127.0
        if dtype == np.int16:
            return 32767.0
        if dtype == np.uint32:
            return float(np.iinfo(np.uint32).max)
        return None

    def _convert_upload_image(
        self,
        image: np.ndarray,
        input_format: Dict[str, Any],
        texture_dtype: str,
    ) -> Optional[np.ndarray]:
        """把任意 dtype 的帧转换为与纹理 dtype 匹配的数组（AUD-39502）。

        f2 纹理按 float16 解释字节：uint8/uint16 等整数帧如果原样写入，
        字节会被当作 float16 位模式解读（画面错乱）。这里统一做两件事：
        1. 整数输入按满量程归一化到 [0, 1]（有 input_format 时用其
           max_value，如 10-bit 的 1023）；
        2. 转换为目标 numpy dtype（f2 → float16，f4 → float32）。
        float32/float16 输入原值透传（AUD-39503：HDR 不做逐帧归一化）。

        Args:
            image: 输入帧数组
            input_format: _detect_input_format 产出的格式信息
            texture_dtype: ModernGL 纹理 dtype（'f2' / 'f4'）

        Returns:
            与纹理 dtype 匹配的数组；无法确定缩放比例时返回 None
        """
        arr = np.asarray(image)
        target = np.float16 if texture_dtype == 'f2' else np.float32
        if arr.dtype == target:
            return arr

        fmt = input_format or {}
        if np.issubdtype(arr.dtype, np.floating):
            # float 输入（float32/float64→f2，或 float32→f4）：原值透传
            return arr.astype(target)

        normalized = bool(fmt.get('normalized', False))
        max_value = fmt.get('max_value')
        if max_value is None:
            max_value = self._default_max_for_dtype(arr.dtype)
        if max_value is None or float(max_value) <= 0.0:
            logger.error(
                "Cannot normalize input dtype %s for %s texture upload",
                arr.dtype, texture_dtype)
            return None
        if normalized:
            # 调用方声明已归一化但 dtype 是整数：仅做类型转换
            return arr.astype(target)
        return (arr.astype(np.float32) / float(max_value)).astype(target)

    def _handle_command(self, cmd_type, data) -> None:
        """处理渲染命令"""
        if cmd_type == VideoRenderCommand.UPLOAD_FRAME:
            self._do_upload_frame(data)
        elif cmd_type == VideoRenderCommand.UPLOAD_YUV_FRAME:
            self._do_upload_yuv_frame(data)
        elif cmd_type == VideoRenderCommand.RENDER_FRAME:
            self._do_render_frame(data)
        elif cmd_type == VideoRenderCommand.UPDATE_PARAMS:
            self._do_update_params(data)
        elif cmd_type == VideoRenderCommand.CLEANUP:
            self._do_cleanup(data)
    
    def _load_shaders(self) -> None:
        """加载视频渲染所需的着色器"""
        # 简单的纹理显示着色器
        passthrough_vs = """
        #version 330
        in vec2 in_vert;
        in vec2 in_texcoord;
        out vec2 v_texcoord;
        void main() {
            gl_Position = vec4(in_vert, 0.0, 1.0);
            v_texcoord = in_texcoord;
        }
        """
        
        passthrough_fs = """
        #version 330
        uniform sampler2D u_texture;
        in vec2 v_texcoord;
        out vec4 f_color;
        void main() {
            f_color = texture(u_texture, v_texcoord);
        }
        """
        
        try:
            self.utility_shaders['passthrough'] = self.ctx.program(
                vertex_shader=passthrough_vs,
                fragment_shader=passthrough_fs
            )
        except moderngl.Error as e:
            # AUD-39509: 着色器编译失败不得吞掉后进入"僵尸正常"状态
            # （清屏空白帧仍按成功 emit frame_ready）。向上抛出，由 run()
            # 的初始化 except 统一 emit error_occurred 并终止渲染线程。
            logger.error(f"Failed to load shaders: {e}")
            raise RuntimeError(
                f"Video passthrough shader compile failed: {e}") from e

        # AUD-39511: 四边形几何是常量，随 shader 一次性创建并缓存，
        # 消除 _render_quad 每帧创建/销毁 VBO/IBO/VAO 的 GPU 对象 churn。
        self._build_quad_geometry(self.utility_shaders['passthrough'])
    
    def _do_upload_frame(self, data: Dict[str, Any]) -> None:
        """
        上传视频帧到 GPU。

        支持多种输入格式的纹理上传。

        Args:
            data: 包含以下键的字典：
                - 'image': numpy 数组 (float32, 归一化到 [0,1])
                - 'frame_size': (width, height) 帧尺寸
                - 'input_format': 输入格式信息（可选）
        """
        if not self.ctx or not data:
            return

        image = data.get('image')
        input_format = data.get('input_format', {})

        if image is None:
            return

        h, w = image.shape[:2]
        components = 3 if image.ndim == 3 and image.shape[2] == 3 else 4
        frame_index = data.get('frame_index', self.current_frame_index + 1)

        # 根据输入格式确定纹理 dtype
        texture_dtype = self._get_texture_format(input_format) if input_format else 'f2'

        # 获取输入 dtype 用于日志
        input_dtype = input_format.get('dtype', 'unknown')

        # P4.1: 使用纹理池获取纹理槽位
        if self._video_texture_pool:
            texture = self._video_texture_pool.acquire(
                width=w,
                height=h,
                frame_index=frame_index,
                components=components,
                dtype=texture_dtype
            )

            if texture:
                try:
                    # AUD-39502: 非 float32 输入此前原样写入 f2 纹理，
                    # 字节被当作 float16 位模式解读（画面错乱）。
                    # 统一经 _convert_upload_image 转换为与纹理 dtype
                    # 匹配的数组（整数归一化到 [0,1]，float 原值透传）。
                    upload_image = self._convert_upload_image(
                        image, input_format, texture_dtype)
                    if upload_image is None:
                        logger.error(
                            "Skipping frame upload: cannot convert dtype %s "
                            "for %s texture", image.dtype, texture_dtype)
                        return
                    texture.write(upload_image.tobytes())

                    self.current_frame_tex = texture
                    self.current_frame_index = frame_index
                    self.frame_size = (w, h)

                    logger.debug(f"Video frame uploaded via pool: frame={frame_index}, "
                                f"size={w}x{h}, input_dtype={input_dtype}, "
                                f"stats={self._video_texture_pool.get_stats()}")
                except Exception as e:
                    logger.error(f"Failed to write frame data: {e}")
            else:
                logger.error("Pool acquire failed, fallback to cached texture")
                # AUD-39504: 此前 fallback 每帧新建纹理且不释放上一张
                # fallback 纹理（GPU 内存泄漏）。改为维护一张按描述符
                # 复用的缓存 fallback 纹理，尺寸/格式变化时才重建。
                texture = self._acquire_fallback_texture(
                    w, h, components, texture_dtype)
                if texture is None:
                    return
                try:
                    upload_image = self._convert_upload_image(
                        image, input_format, texture_dtype)
                    if upload_image is None:
                        return
                    texture.write(upload_image.tobytes())

                    self.current_frame_tex = texture
                    self.current_frame_index = frame_index
                    self.frame_size = (w, h)
                except Exception as e:
                    logger.error(f"Fallback upload failed: {e}")
        else:
            # 纹理池未初始化
            logger.warning("VideoTexturePool not initialized")
            try:
                if self.current_frame_tex:
                    try:
                        self.current_frame_tex.release()
                    except Exception:
                        pass
                self.current_frame_tex = self.ctx.texture(
                    size=(w, h), components=components, dtype='f4'
                )
                # AUD-39502: f4 纹理同样需要 dtype 匹配的字节
                upload_image = self._convert_upload_image(
                    image, input_format, 'f4')
                if upload_image is None:
                    return
                self.current_frame_tex.write(upload_image.tobytes())
                self.current_frame_index = frame_index
                self.frame_size = (w, h)
            except Exception as e:
                logger.error(f"Failed to upload frame: {e}")

    def _acquire_fallback_texture(
        self,
        width: int,
        height: int,
        components: int,
        dtype: str,
    ) -> Optional[Any]:
        """获取复用的 fallback 纹理（池 acquire 失败时的兜底，渲染线程）。

        AUD-39504：fallback 纹理按 (width, height, components, dtype)
        描述符缓存复用；描述符变化时先释放旧纹理再重建，避免每帧新建
        纹理且不释放上一张导致的 GPU 内存泄漏。

        Args:
            width: 纹理宽度
            height: 纹理高度
            components: 通道数
            dtype: 纹理 dtype（'f2' 等）

        Returns:
            可用的 moderngl.Texture，创建失败返回 None
        """
        if not self.ctx:
            return None
        descriptor = (int(width), int(height), int(components), str(dtype))
        if (self._fallback_frame_tex is not None
                and self._fallback_frame_tex_desc == descriptor):
            return self._fallback_frame_tex

        if self._fallback_frame_tex is not None:
            try:
                self._fallback_frame_tex.release()
            except Exception:
                pass
            self._fallback_frame_tex = None
            self._fallback_frame_tex_desc = None

        try:
            texture = self.ctx.texture(
                size=(int(width), int(height)),
                components=int(components),
                dtype=str(dtype),
            )
            texture.filter = (moderngl.LINEAR, moderngl.LINEAR)
            self._fallback_frame_tex = texture
            self._fallback_frame_tex_desc = descriptor
            return texture
        except Exception as e:
            logger.error(f"Failed to create fallback frame texture: {e}")
            self._fallback_frame_tex = None
            self._fallback_frame_tex_desc = None
            return None

    def _do_upload_yuv_frame(self, data: Dict[str, Any]) -> None:
        """
        上传 YUV 平面帧到 GPU（减少带宽消耗）。

        支持 YUV420P, YUV422P, YUV444P, NV12, P010 格式。

        Args:
            data: 包含以下键的字典：
                - 'y': Y 平面数据 (numpy.ndarray)
                - 'u': U 平面数据 (可选，用于 planar 格式)
                - 'v': V 平面数据 (可选，用于 planar 格式)
                - 'uv': UV interleaved 数据 (可选，用于 NV12/P010)
                - 'format': YUV 格式 ('420', '422', '444', 'nv12', 'p010')
                - 'bit_depth': 位深度 (默认 8)
                - 'frame_index': 帧号
                - 'yuv_matrix': YUV 矩阵类型 (0=BT.709, 1=BT.2020, 2=BT.601)
                - 'color_range': 颜色范围 (0=limited, 1=full)

        带宽节省：
            - 4K RGB: 3840×2160×3 = 99.5 MB/frame
            - 4K YUV420: 12.4 MB/frame (减少 ~87.5%)
        """
        if not self.ctx or not self._yuv_gpu_uploader:
            logger.error("YUVGPUUploader not initialized")
            return

        y = data.get('y')
        if y is None:
            logger.error("Y plane data is required")
            return

        yuv_format = data.get('format', '420')
        bit_depth = data.get('bit_depth', 8)
        frame_index = data.get('frame_index', self.current_frame_index + 1)
        yuv_matrix = data.get('yuv_matrix', YUVGPUUploader.MATRIX_BT709)
        color_range = data.get('color_range', YUVGPUUploader.RANGE_LIMITED)

        # 设置转换参数
        self._yuv_gpu_uploader.set_yuv_matrix(yuv_matrix)
        self._yuv_gpu_uploader.set_color_range(color_range)

        try:
            if yuv_format in ('420', '422', '444'):
                # Planar YUV 格式
                u = data.get('u')
                v = data.get('v')
                if u is None or v is None:
                    logger.error(f"U/V planes required for {yuv_format} format")
                    return

                # 根据格式调用不同的上传方法
                if yuv_format == '420':
                    y_tex, u_tex, v_tex = self._yuv_gpu_uploader.upload_yuv420(
                        y, u, v, frame_index, bit_depth
                    )
                elif yuv_format == '422':
                    y_tex, u_tex, v_tex = self._yuv_gpu_uploader.upload_yuv422(
                        y, u, v, frame_index, bit_depth
                    )
                elif yuv_format == '444':
                    y_tex, u_tex, v_tex = self._yuv_gpu_uploader.upload_yuv444(
                        y, u, v, frame_index, bit_depth
                    )

                # 保存纹理引用
                self._yuv_y_tex = y_tex
                self._yuv_u_tex = u_tex
                self._yuv_v_tex = v_tex
                self._yuv_uv_tex = None

            elif yuv_format in ('nv12', 'p010'):
                # NV12/P010 interleaved UV 格式
                uv = data.get('uv')
                if uv is None:
                    logger.error(f"UV interleaved data required for {yuv_format} format")
                    return

                if yuv_format == 'nv12':
                    y_tex, uv_tex = self._yuv_gpu_uploader.upload_nv12(
                        y, uv, frame_index, bit_depth
                    )
                else:  # p010
                    y_tex, uv_tex = self._yuv_gpu_uploader.upload_p010(
                        y, uv, frame_index
                    )

                # 保存纹理引用
                self._yuv_y_tex = y_tex
                self._yuv_uv_tex = uv_tex
                self._yuv_u_tex = None
                self._yuv_v_tex = None

            else:
                logger.error(f"Unknown YUV format: {yuv_format}")
                return

            # 更新状态
            self._is_yuv_frame = True
            self._yuv_format = yuv_format
            self.current_frame_index = frame_index
            self.frame_size = (y.shape[1], y.shape[0])  # (width, height)

            # AUD-39501: YUV 上传完成后合成 RGB 帧并写入 current_frame_tex，
            # 与 RGB 路径（_do_upload_frame）对齐——_do_render_frame 只渲染
            # current_frame_tex，否则 RENDER_FRAME 渲染的是过期 RGB 帧。
            composite_fbo = self._ensure_yuv_composite_fbo(
                self.frame_size[0], self.frame_size[1]
            )
            if composite_fbo is not None:
                if yuv_format in ('420', '422', '444'):
                    self._yuv_gpu_uploader.render_yuv_to_rgb(
                        self._yuv_y_tex, self._yuv_u_tex, self._yuv_v_tex,
                        composite_fbo, bit_depth=bit_depth
                    )
                else:  # 'nv12' / 'p010'
                    self._yuv_gpu_uploader.render_nv12_to_rgb(
                        self._yuv_y_tex, self._yuv_uv_tex,
                        composite_fbo, bit_depth=bit_depth
                    )
                # 消费端（_do_render_frame / get_current_frame_texture）
                # 通过 current_frame_tex 建立当前帧纹理句柄
                self.current_frame_tex = self._yuv_composite_tex

            # 获取上传统计
            stats = self._yuv_gpu_uploader.get_upload_stats()

            # 计算带宽节省
            bandwidth_saved = self._yuv_gpu_uploader.calculate_bandwidth_saved(
                self.frame_size[0],
                self.frame_size[1],
                yuv_format
            )

            logger.debug(f"YUV frame uploaded: frame={frame_index}, "
                        f"format={yuv_format}, size={self.frame_size}, "
                        f"upload_time={stats.avg_upload_time_ms:.2f}ms, "
                        f"bandwidth_saved={bandwidth_saved:.1f}MB")

        except Exception as e:
            logger.error(f"Failed to upload YUV frame: {e}")
            self._is_yuv_frame = False

    def _ensure_yuv_composite_fbo(self, width: int, height: int) -> Optional[Any]:
        """
        确保 YUV→RGB 合成目标 FBO 存在且与帧尺寸一致。

        仅在渲染线程调用（命令处理路径）。尺寸变化时释放旧 FBO 与
        颜色附件纹理后重建。

        Args:
            width: 帧宽度
            height: 帧高度

        Returns:
            合成目标 moderngl.Framebuffer，上下文不可用时返回 None
        """
        if not self.ctx:
            return None

        if (self._yuv_composite_fbo is not None
                and self._yuv_composite_fbo.size == (width, height)):
            return self._yuv_composite_fbo

        # 尺寸变化：释放旧资源（FBO 与颜色附件纹理分别释放）
        if self._yuv_composite_fbo is not None:
            try:
                self._yuv_composite_fbo.release()
            except Exception:
                pass
            self._yuv_composite_fbo = None
        if self._yuv_composite_tex is not None:
            try:
                self._yuv_composite_tex.release()
            except Exception:
                pass
            self._yuv_composite_tex = None

        try:
            composite_tex = self.ctx.texture((width, height), 4, dtype='f2')
            composite_tex.filter = (moderngl.LINEAR, moderngl.LINEAR)
            composite_tex.repeat_x = composite_tex.repeat_y = False
            composite_fbo = self.ctx.framebuffer(composite_tex)

            self._yuv_composite_tex = composite_tex
            self._yuv_composite_fbo = composite_fbo
            return composite_fbo
        except Exception as e:
            logger.error(f"Failed to create YUV composite FBO: {e}")
            self._yuv_composite_fbo = None
            self._yuv_composite_tex = None
            return None

    def _do_render_frame(self, data) -> None:
        """
        渲染当前帧到屏幕。
        
        Args:
            data: 包含以下键的字典：
                - 'viewport_size': (width, height) 视口尺寸
                - 'params': 渲染参数（可选）
        """
        if not self.ctx or not self.current_frame_tex:
            return
        
        viewport_size = data.get('viewport_size', (1, 1))
        w, h = viewport_size
        
        if w <= 0 or h <= 0:
            return
        
        # 确保 FBO 存在
        self._ensure_fbos(w, h)
        
        # 设置视口
        self.ctx.viewport = (0, 0, w, h)
        
        # 渲染到当前写入缓冲区
        fbo_screen = self.fbo_pair[self.write_idx]
        fbo_screen.use()
        self.ctx.clear(0.1, 0.1, 0.1, 1.0)  # 背景色
        
        # 绑定纹理并渲染
        prog = self.utility_shaders.get('passthrough')
        if prog is None:
            # AUD-39509: 无可用着色器时不得清屏翻转后仍按成功 emit
            # frame_ready（僵尸正常状态）。上报错误并丢弃本帧。
            logger.error("passthrough shader unavailable; refusing to emit frame_ready")
            self.error_occurred.emit("Video passthrough shader unavailable")
            return
        self.current_frame_tex.use(0)
        prog['u_texture'].value = 0
        self._render_quad(prog)
        
        # 切换缓冲区
        front_idx = self.write_idx
        self.write_idx = 1 - self.write_idx
        
        # 完成渲染并发射信号
        self.ctx.finish()
        tex_id = self.tex_pair[front_idx].glo
        self.frame_ready.emit(tex_id)
    
    def _do_update_params(self, data) -> None:
        """更新渲染参数"""
        if data:
            self.render_params.update(data)
    
    def _do_cleanup(self, data) -> None:
        """清理资源"""
        self._cleanup_gl_resources()
    
    def _ensure_fbos(self, width, height) -> None:
        """确保双缓冲 FBO 存在"""
        if self.fbo_pair[0] is None or self.fbo_pair[0].size != (width, height):
            # AUD-39508: 旧 FBO 纹理的 glo 可能刚经 frame_ready 发给 UI
            # 共享上下文消费。立即 release 会在消费方采样期间删除纹理名
            # （驱动可能复用该 ID），产生跨线程纹理 ID 生命周期竞态。
            # 改为挂入延迟释放队列，宽限期后在渲染线程释放。
            for fbo, tex in zip(self.fbo_pair, self.tex_pair):
                if fbo is not None:
                    self._retire_fbo_pair(fbo, tex)

            # 创建新的 FBO
            self.fbo_pair[0] = self.ctx.framebuffer(
                self.ctx.texture((width, height), 4, dtype='f2')
            )
            self.fbo_pair[1] = self.ctx.framebuffer(
                self.ctx.texture((width, height), 4, dtype='f2')
            )
            self.tex_pair[0] = self.fbo_pair[0].color_attachments[0]
            self.tex_pair[1] = self.fbo_pair[1].color_attachments[0]

    # AUD-39508: 退役 FBO/纹理的宽限期与容量上限。宽限期覆盖 UI 消费方
    # 持有最近一帧 glo 的窗口；上限防止长时间无渲染命令时队列无限增长。
    _FBO_RELEASE_GRACE_S: float = 2.0
    _RETIRED_FBO_CAP: int = 8

    def _retire_fbo_pair(self, fbo: Any, tex: Any) -> None:
        """把被替换的 FBO/纹理对挂入延迟释放队列（仅渲染线程）。"""
        # __dict__ 直读：未跑 __init__ 的 __new__ 测试桩上 PyQt6 对缺失
        # 属性的访问会抛 RuntimeError（代码库既有惯例）
        retired = self.__dict__.setdefault('_retired_fbo_resources', [])
        retired.append(
            (time.monotonic() + self._FBO_RELEASE_GRACE_S, fbo, tex))
        while len(retired) > self._RETIRED_FBO_CAP:
            _, old_fbo, old_tex = retired.pop(0)
            self._release_retired_pair(old_fbo, old_tex)

    @staticmethod
    def _release_retired_pair(fbo: Any, tex: Any) -> None:
        """释放一对退役 FBO/纹理（仅渲染线程，容忍重复释放）。"""
        for res in (fbo, tex):
            if res is None:
                continue
            try:
                res.release()
            except moderngl.Error:
                logger.debug("Failed to release retired FBO resource",
                             exc_info=True)

    def _drain_retired_fbo_resources(self, force: bool = False) -> None:
        """释放宽限期已过（或 force 时全部）的退役 FBO/纹理。

        仅渲染线程调用：命令循环每轮排水一次，_cleanup_gl_resources
        以 force=True 收尾。
        """
        retired = self.__dict__.get('_retired_fbo_resources')
        if not retired:
            return
        now = time.monotonic()
        remaining: List[Tuple[float, Any, Any]] = []
        for deadline, fbo, tex in retired:
            if force or now >= deadline:
                self._release_retired_pair(fbo, tex)
            else:
                remaining.append((deadline, fbo, tex))
        self._retired_fbo_resources = remaining

    # AUD-39511: 全屏四边形常量几何（位置 + 纹理坐标 + 索引）
    _QUAD_VERTICES = np.array([
        # x, y, u, v
        -1.0, -1.0, 0.0, 0.0,
         1.0, -1.0, 1.0, 0.0,
         1.0,  1.0, 1.0, 1.0,
        -1.0,  1.0, 0.0, 1.0,
    ], dtype='f4')
    _QUAD_INDICES = np.array([0, 1, 2, 0, 2, 3], dtype='i4')

    def _build_quad_geometry(self, program: Any) -> None:
        """一次性创建全屏四边形 VBO/IBO/VAO（仅渲染线程）。

        AUD-39511: 顶点/索引数据是常量，旧实现在每个 RENDER_FRAME 里
        创建并销毁 VBO/IBO/VAO（60fps 下每秒 180 次 GL 对象创建+释放），
        与纹理池化（P4.1）理念相悖。几何随 shader 加载创建一次，
        _cleanup_gl_resources 统一释放。
        """
        vbo = self.ctx.buffer(self._QUAD_VERTICES.tobytes())
        ibo = self.ctx.buffer(self._QUAD_INDICES.tobytes())
        vao = self.ctx.vertex_array(
            program, [(vbo, '2f 2f', 'in_vert', 'in_texcoord')], ibo)
        self._quad_vbo = vbo
        self._quad_ibo = ibo
        self._quad_vao = vao

    def _render_quad(self, program) -> None:
        """渲染全屏四边形（AUD-39511: 复用缓存的 VBO/IBO/VAO）"""
        vao = self.__dict__.get('_quad_vao')
        if vao is None:
            # 兜底：缓存缺失时懒构建（正常路径在 _load_shaders 已创建）
            self._build_quad_geometry(program)
            vao = self.__dict__.get('_quad_vao')
            if vao is None:
                return
        vao.render(moderngl.TRIANGLES, vertices=6)
    
    def _cleanup_gl_resources(self) -> None:
        """清理 OpenGL 资源"""
        self._initialized = False

        # AUD-39508: 强制释放全部退役 FBO/纹理（上下文仍 current）
        self._drain_retired_fbo_resources(force=True)

        # AUD-39511: 释放缓存的四边形几何
        for attr in ('_quad_vao', '_quad_vbo', '_quad_ibo'):
            res = self.__dict__.get(attr)
            if res is not None:
                try:
                    res.release()
                except moderngl.Error:
                    logger.debug("Failed to release quad geometry", exc_info=True)
                self.__dict__[attr] = None

        # P4.1: 释放纹理池
        if self._video_texture_pool:
            try:
                self._video_texture_pool.release_all()
            except Exception as e:
                logger.debug(f"VideoTexturePool release failed: {e}")
            self._video_texture_pool = None

        # P4.4: 释放 YUVGPUUploader
        if self._yuv_gpu_uploader:
            try:
                self._yuv_gpu_uploader.release()
            except Exception as e:
                logger.debug(f"YUVGPUUploader release failed: {e}")
            self._yuv_gpu_uploader = None

        # 清理 YUV 纹理引用
        self._yuv_y_tex = None
        self._yuv_u_tex = None
        self._yuv_v_tex = None
        self._yuv_uv_tex = None
        self._is_yuv_frame = False

        # AUD-39504: 释放复用的 fallback 纹理
        if self._fallback_frame_tex is not None:
            try:
                self._fallback_frame_tex.release()
            except Exception:
                pass
            self._fallback_frame_tex = None
            self._fallback_frame_tex_desc = None

        # AUD-39501: 释放 YUV→RGB 合成目标（FBO 与颜色附件纹理分别释放）
        if self._yuv_composite_fbo is not None:
            try:
                self._yuv_composite_fbo.release()
            except Exception:
                pass
            self._yuv_composite_fbo = None
        if self._yuv_composite_tex is not None:
            try:
                self._yuv_composite_tex.release()
            except Exception:
                pass
            self._yuv_composite_tex = None

        self.current_frame_tex = None
        self.current_frame_index = -1
        
        for fbo in self.fbo_pair:
            if fbo:
                try:
                    fbo.release()
                except Exception:
                    pass
        self.fbo_pair = [None, None]
        
        for tex in self.tex_pair:
            if tex:
                try:
                    tex.release()
                except Exception:
                    pass
        self.tex_pair = [None, None]
        
        self._convert_buffer = None
        self._buffer_size = (0, 0)
        
        self.ctx = None
    
    def stop_thread(self):
        """安全停止渲染线程"""
        self.is_running = False
        if self.isRunning():
            self.quit()
            self.wait(3000)  # 等待最多 3 秒

    def _release_gl_context_on_exit(self, moderngl_ctx: Optional[Any]) -> None:
        """线程退出时释放 GL 上下文（仅 run() 尾部调用，渲染线程）。

        AUD-39506: 此前 run() 返回时 shared_context 仍 current 在即将
        结束的渲染线程上，moderngl 上下文仅置 None 不 release——GL 对象
        的析构线程退化为「Python GC 在哪个线程跑就在哪销毁」，跨线程
        销毁 GL 资源是未定义行为。现在：
        1. release moderngl 上下文（GL 对象已在 _cleanup_gl_resources
           于本线程、上下文 current 时释放完毕）；
        2. shared_context.doneCurrent() 归还上下文；
        3. QOffscreenSurface 归属主线程，deleteLater() 把销毁投递回
           主线程事件循环。
        """
        if moderngl_ctx is not None:
            try:
                moderngl_ctx.release()
            except moderngl.Error as e:
                logger.warning(f"Failed to release ModernGL context on exit: {e}")
        if self.shared_context is not None:
            try:
                self.shared_context.doneCurrent()
            except RuntimeError as e:
                logger.warning(f"Failed to doneCurrent shared context on exit: {e}")
        surface = self.surface
        self.surface = None
        if surface is not None:
            try:
                surface.deleteLater()
            except RuntimeError as e:
                logger.warning(f"Failed to schedule surface deletion on exit: {e}")
    
    def is_initialized(self):
        """检查渲染器是否已初始化"""
        return self._initialized
    
    def get_current_frame_texture(self):
        """获取当前帧纹理（用于调试）"""
        return self.current_frame_tex
    
    def get_texture_pool_stats(self) -> Dict[str, Any]:
        """
        P4.1: 获取纹理池统计信息。
        
        用于验证 GPU 内存稳定性。
        
        Returns:
            包含池统计信息的字典，或空字典（池未初始化）
        """
        if self._video_texture_pool:
            return self._video_texture_pool.get_stats()
        return {'pool_size': 0, 'max_size': 0, 'active_frames': [], 'sizes': []}
    
    # ================================================================
    # 上下文管理器协议
    # ================================================================
    
    def __enter__(self) -> 'VideoAsyncRenderer':
        """
        进入上下文管理器。
        
        Returns:
            self，支持 with 语句使用
        """
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        """
        退出上下文管理器，确保资源释放。
        
        Args:
            exc_type: 异常类型（如果有）
            exc_val: 异常值
            exc_tb: 异常 traceback
        
        注意：
            无论是否发生异常，都会调用 stop_thread() 清理资源
        """
        self.stop_thread()
    
    # ================================================================
    # GC 后备释放（AUD-39507：finalizer 注册已移除）
    # ================================================================

    @staticmethod
    def _cleanup_on_gc(gc_resources: List[Any]) -> None:
        """Release resources passed in ``gc_resources``.

        AUD-39507: the weakref.finalize registration that (never) fed this
        helper was removed — ``_gc_resources`` was permanently empty, so the
        "GC 时确保基本资源释放" guarantee documented in ``__init__`` never
        actually held. Wiring it up for real is NOT the fix either: GL
        objects must never be released from the GC thread (no context is
        current there — same conclusion as AUD-7605 / video_frame_node).
        Teardown is owned by the explicit paths: run() releases every GL
        object and the context itself on the render thread at exit
        (AUD-39506), or callers invoke stop_thread(). The helper itself is
        retained as a small utility for non-GL cleanup-on-exception paths.

        Args:
            gc_resources: 需要释放的资源列表
        """
        for res in gc_resources:
            try:
                if res is not None:
                    # 尝试释放资源
                    if hasattr(res, 'release'):
                        res.release()
                    elif hasattr(res, 'close'):
                        res.close()
            except Exception:
                pass  # GC 清理时忽略所有异常
