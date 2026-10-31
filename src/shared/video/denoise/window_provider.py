"""暂停态时域降噪邻帧窗口提供器（UI 线程调度 + 后台线程取帧/转换）。

对应计划 M4.2/M2.3 的 Paused 质量路径：
- UI 侧在暂停 / seek 落定 / 逐帧 / 参数变化时调度窗口获取（去抖合并）；
- 后台线程经 VideoFrameProviderService.request_frame_window 取邻帧，
  CPU 转换为归一化 RGBA float16（宽度上限 2560，控制上传与显存预算，
  与本模块 _MAX_NEIGHBOR_WIDTH 一致；AUD-38907: 此前误记为 1920）；
- 结果经 Qt 信号回到 UI 线程，由控制器提交 SET_VIDEO_DENOISE_WINDOW。

任何失败静默放弃——暂停态窗口只是增强，缺失时渲染器自动退化为因果
历史（Preview 同款行为），绝不阻塞交互。
"""

from __future__ import annotations

import logging
import queue
import threading
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
from PyQt6.QtCore import QObject, QTimer, pyqtSignal

from .params import VideoDenoiseParams

logger = logging.getLogger(__name__)

# 邻帧纹理宽度上限：窗口重放在静态预览使用，运动估计本就运行在 1/4 分辨率，
# 降采样邻帧可控制上传与显存预算（4K→2560 约 29MB/帧 f16，4 邻帧 ≈118MB）。
# 配合重放时"邻帧轮半权重/当前帧轮全权重"（G-P1），当前帧全分辨率细节保留。
_MAX_NEIGHBOR_WIDTH = 2560

# AUD-38905：后台队列里的"释放帧服务会话"任务标记（普通任务是
# (path, frame, before, after, token) 5 元组，路径不会等于该哨兵）。
_CLOSE_CLIP_JOB = "__close_clip__"


class VideoDenoiseWindowProvider(QObject):
    """调度并交付暂停态时域降噪的邻帧窗口。

    生命周期：UI 线程创建并 schedule()；后台单工作线程取帧转换；
    window_ready 在 UI 线程发射（Qt 队列连接自动跨线程投递）。
    """

    window_ready = pyqtSignal(dict)

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._debounce_token = 0
        self._dispatch_token = 0
        self._jobs: "queue.Queue[Optional[Tuple]]" = queue.Queue()
        self._worker: Optional[threading.Thread] = None
        self._worker_lock = threading.Lock()
        # 帧服务 clip 会话（按路径缓存；换素材时关闭旧会话）
        self._clip_path: Optional[str] = None
        self._clip_id: Optional[str] = None
        # AUD-38905：会话世代号。clear()/shutdown() 排入的"关闭会话"
        # 任务携带关闭时刻的世代号；期间若又有 schedule() 重开会话
        # （世代号已变），关闭任务自动放弃，避免误关新会话。
        self._clip_epoch: int = 0

    # ------------------------------------------------------------------
    # UI 线程入口

    def schedule(
        self,
        video_path: str,
        source_frame: int,
        params: VideoDenoiseParams,
        before: int = 2,
        after: int = 2,
    ) -> None:
        """请求 (video_path, source_frame) 的邻帧窗口（立即派发后台任务）。

        temporal 未启用时静默忽略。去抖合并由调用方（控制器）完成，
        这里只负责后台取帧与最新任务取代（token）。
        """
        if not video_path or source_frame < 0:
            return
        try:
            temporal = params.temporal if params is not None else None
            if temporal is None or not temporal.is_active():
                return
        except Exception:
            return

        self._debounce_token += 1
        path = str(video_path)
        frame = int(source_frame)
        before = max(1, min(int(before), 3))
        after = max(1, min(int(after), 3))

        self._dispatch_token += 1
        token = self._dispatch_token
        QTimer.singleShot(0, lambda: self._enqueue(path, frame, before, after, token))

    def clear(self) -> None:
        """作废所有在途请求并释放帧服务独立会话（换素材/卸载视频时调用）。

        AUD-38905：此前 `shutdown()` 全库无调用点，clear() 只作废
        token 不关会话——卸载视频后独立解码会话（master/proxy/VT 源）
        在帧服务里永久泄漏。现在 clear() 同时把"关闭会话"排入后台
        worker（close 是阻塞 I/O，绝不在 UI 线程执行）；若关闭任务
        执行前又有 schedule() 重开会话（epoch 已变），任务自动放弃。
        """
        self._debounce_token += 1
        self._dispatch_token += 1
        self._release_clip_async()

    def _release_clip_async(self) -> None:
        """把"关闭帧服务会话"排入后台 worker（幂等：无会话则跳过）。"""
        clip_id = self._clip_id
        if not clip_id:
            return
        epoch = self._clip_epoch
        self._ensure_worker()
        self._jobs.put((_CLOSE_CLIP_JOB, clip_id, epoch))

    # ------------------------------------------------------------------
    # 后台取帧

    def _enqueue(self, path: str, frame: int, before: int, after: int, token: int) -> None:
        self._ensure_worker()
        self._jobs.put((path, frame, before, after, token))

    def _ensure_worker(self) -> None:
        with self._worker_lock:
            if self._worker is not None and self._worker.is_alive():
                return
            self._worker = threading.Thread(
                target=self._worker_loop, name="VideoDenoiseWindow", daemon=True
            )
            self._worker.start()

    def _worker_loop(self) -> None:
        while True:
            job = self._jobs.get()
            if job is None:
                break
            try:
                if (
                    isinstance(job, tuple)
                    and len(job) == 3
                    and job[0] == _CLOSE_CLIP_JOB
                ):
                    self._close_clip_job(job[1], job[2])
                else:
                    self._process_job(*job)
            except Exception:
                logger.exception("VideoDenoiseWindowProvider: job failed")
            finally:
                self._jobs.task_done()

    def _close_clip_job(self, clip_id: str, epoch: int) -> None:
        """后台关闭帧服务会话（AUD-38905）。

        epoch 与当前世代号不一致说明关闭任务在队列里等待期间已有新的
        schedule() 重开会话——放弃关闭，保留新会话。
        """
        if epoch != self._clip_epoch or self._clip_id != clip_id:
            logger.debug(
                "VideoDenoiseWindowProvider: skip stale clip close "
                f"(job epoch={epoch}, current={self._clip_epoch})"
            )
            return
        try:
            from ..frame_provider_service import get_frame_provider_service

            get_frame_provider_service().close_clip(clip_id)
        except Exception:
            logger.debug(
                "VideoDenoiseWindowProvider: close_clip failed",
                exc_info=True,
            )
        finally:
            if self._clip_id == clip_id:
                self._clip_id = None
                self._clip_path = None

    def _process_job(self, path: str, frame: int, before: int, after: int, token: int) -> None:
        payload = self._fetch_window(path, frame, before, after)
        if token != self._dispatch_token:
            return  # 请求已过期（seek 到了别处/换了素材）
        # 空 payload（无可用邻帧）同样投递，用于清除渲染器里的旧窗口。
        self.window_ready.emit(payload)

    def _fetch_window(self, path: str, frame: int, before: int, after: int) -> Dict[str, Any]:
        from ..frame_provider_service import get_frame_provider_service

        try:
            service = get_frame_provider_service()
            clip_id = self._ensure_clip(service, path)
            if clip_id is None:
                return {}
            window = service.request_frame_window(
                clip_id, frame, before, after, quality='master'
            )
        except Exception:
            logger.exception("VideoDenoiseWindowProvider: request_frame_window failed")
            return {}

        # previous_frames 是近→远（center-1, center-2…），取反得到远→近；
        # process_window 期望 最旧→最新（不含当前帧，当前帧由渲染器追加）。
        ordered = list(reversed(window.previous_frames)) + list(window.next_frames)
        neighbors: List[Tuple[int, np.ndarray]] = []
        for decoded in ordered:
            try:
                rgba = _frame_to_rgba_f16(decoded, _MAX_NEIGHBOR_WIDTH)
            except Exception:
                logger.exception(
                    "VideoDenoiseWindowProvider: neighbor convert failed "
                    f"(frame={getattr(decoded, 'frame_index', '?')})"
                )
                continue
            if rgba is not None:
                neighbors.append((int(getattr(decoded, 'frame_index', -1)), rgba))

        if not neighbors:
            return {}
        return {
            'path': path,
            'center_frame': int(frame),
            'neighbors': neighbors,
        }

    def _ensure_clip(self, service, path: str) -> Optional[str]:
        """打开独立的降噪取帧会话（绝不与播放会话共享解码器）。

        G-P0（黑屏修复）：share_by_path=False 强制创建独立 ClipSession。
        复用播放会话时，request_frame_window 的逐帧 seek 会与播放引擎的
        顺序帧请求竞争同一个解码器，导致播放帧饥饿黑屏（声音正常）。
        独立会话多付一次邻帧解码（暂停态后台一次性成本），彻底隔离。
        """
        if self._clip_path == path and self._clip_id:
            return self._clip_id
        if self._clip_id:
            try:
                service.close_clip(self._clip_id)
            except Exception:
                pass
            self._clip_id = None
            self._clip_path = None
        try:
            clip_id = service.open_clip(path, share_by_path=False)
        except Exception:
            logger.warning(f"VideoDenoiseWindowProvider: open_clip failed: {path}")
            return None
        # AUD-38905：新会话世代号 +1，使队列里等待的关闭任务失效
        self._clip_epoch += 1
        self._clip_path = path
        self._clip_id = clip_id
        return clip_id

    def shutdown(self) -> None:
        """释放帧服务会话并停止后台线程（应用退出时调用）。

        AUD-38905：全库曾无调用点导致卸载视频后独立会话泄漏——泄漏
        已改由 clear()（换素材/卸载/开始播放时控制器会调用）经后台
        worker 兜底释放；shutdown() 保留为同步退出路径。
        """
        self.clear()
        if self._clip_id:
            try:
                from ..frame_provider_service import get_frame_provider_service

                get_frame_provider_service().close_clip(self._clip_id)
            except Exception:
                logger.debug(
                    "VideoDenoiseWindowProvider: shutdown close_clip failed",
                    exc_info=True,
                )
            self._clip_id = None
            self._clip_path = None
        self._jobs.put(None)


# ----------------------------------------------------------------------
# DecodedFrame → 归一化 RGBA float16
# ----------------------------------------------------------------------

_MATRIX_MAP = {
    'bt709': 0,
    'smpte170m': 2,
    'bt470bg': 2,
    'bt2020nc': 1,
    'bt2020': 1,
}

_RANGE_MAP = {
    'limited': 0,
    'tv': 0,
    'full': 1,
    'pc': 1,
}


def _frame_to_rgba_f16(decoded: Any, max_width: int) -> Optional[np.ndarray]:
    """DecodedFrame → RGBA float16（0-1 归一化，等比缩到 ≤max_width 宽）。

    平面 YUV 用与渲染器 CPU 回退一致的 get_color_converter 路径
    （矩阵/范围取自帧 metadata，缺省 BT.709 limited）。
    """
    import cv2

    width = int(getattr(decoded, 'width', 0) or 0)
    height = int(getattr(decoded, 'height', 0) or 0)
    if width <= 0 or height <= 0:
        return None

    if getattr(decoded, 'is_planar', False) and getattr(decoded, 'planes', None):
        rgb = _planar_to_rgb(decoded)
        if rgb is None:
            return None
    else:
        rgb = _packed_to_rgb(decoded)
        if rgb is None:
            return None

    if rgb.ndim != 3 or rgb.shape[-1] != 3:
        return None
    if rgb.dtype == np.uint8:
        rgb = rgb.astype(np.float32) / 255.0
    elif rgb.dtype == np.uint16:
        rgb = rgb.astype(np.float32) / 65535.0

    if rgb.shape[1] > max_width:
        new_w = max_width
        new_h = max(1, int(round(height * (max_width / float(width)))))
        rgb = cv2.resize(rgb, (new_w, new_h), interpolation=cv2.INTER_AREA)

    alpha = np.ones(rgb.shape[:2] + (1,), dtype=rgb.dtype)
    return np.ascontiguousarray(
        np.concatenate([rgb, alpha], axis=2).astype(np.float16)
    )


def _planar_to_rgb(decoded: Any) -> Optional[np.ndarray]:
    planes = decoded.planes
    if planes is None or len(planes) < 3:
        return None

    from src.shared.media.color_decode import (
        ColorMatrix,
        ColorRange,
        get_color_converter,
    )

    metadata = getattr(decoded, 'metadata', None)
    matrix_str = str(getattr(metadata, 'color_matrix', 'bt709') or 'bt709').lower()
    range_str = str(getattr(metadata, 'color_range', 'limited') or 'limited').lower()
    matrix_map = {
        0: ColorMatrix.BT709,
        1: ColorMatrix.BT2020_NC,
        2: ColorMatrix.BT601,
    }
    range_map = {0: ColorRange.LIMITED, 1: ColorRange.FULL}

    converter = get_color_converter()
    return converter.yuv_to_rgb(
        y=planes[0],
        u=planes[1],
        v=planes[2],
        matrix=matrix_map.get(_MATRIX_MAP.get(matrix_str, 0), ColorMatrix.BT709),
        range_=range_map.get(_RANGE_MAP.get(range_str, 0), ColorRange.LIMITED),
        bit_depth=int(getattr(decoded, 'bit_depth', 8) or 8),
    )


def _packed_to_rgb(decoded: Any) -> Optional[np.ndarray]:
    import cv2

    data = getattr(decoded, 'data', None)
    if data is None:
        return None
    fmt = str(getattr(decoded, 'pixel_format', '') or '').lower()
    if data.ndim == 2:
        return cv2.cvtColor(data, cv2.COLOR_GRAY2RGB)
    if data.ndim != 3:
        return None
    if fmt in ('rgb24', 'rgb48', 'rgb48le'):
        return data
    if fmt in ('rgba', 'rgba64', 'rgba64le'):
        return data[..., :3]
    if fmt in ('bgr24', 'bgr48', 'bgr48le'):
        return cv2.cvtColor(data, cv2.COLOR_BGR2RGB)
    if fmt in ('bgra', 'bgra64', 'bgra64le'):
        return cv2.cvtColor(data, cv2.COLOR_BGRA2RGB)
    if data.shape[-1] == 4:
        return cv2.cvtColor(data, cv2.COLOR_BGRA2RGB)
    if data.shape[-1] == 3:
        return cv2.cvtColor(data, cv2.COLOR_BGR2RGB)
    return None
