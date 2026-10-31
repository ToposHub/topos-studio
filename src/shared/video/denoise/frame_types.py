"""视频降噪帧窗口类型与消费者侧缓存。

对应 docs/video_denoise_implementation_plan.md 第 7 节（M2 邻帧读取与缓存）。

- FrameWindow: 一次邻帧窗口请求的结果（中心帧 + 前后邻帧 + 缺帧信息）
- TemporalFrameWindowCache: 消费者侧窗口缓存，键包含
  clip_id / quality / generation_id / pixel_format / resolution，
  带内存预算与 LRU 淘汰，seek（generation 变化）自动失效。

解码侧的帧缓存由 VideoFrameProviderService 的 ClipSession 负责，
本缓存服务于时域降噪消费者（如导出/暂停预览）反复请求相邻窗口的场景。
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple


@dataclass
class FrameWindow:
    """一个以 center_frame 为中心的邻帧窗口。

    previous_frames / next_frames 按离中心由近到远排序；
    元素是解码帧对象（DecodedFrame / GpuDecodedFrame），由调用方解释。
    """

    clip_id: str
    center_frame: int
    previous_frames: List[Any] = field(default_factory=list)
    next_frames: List[Any] = field(default_factory=list)
    center_frame_data: Optional[Any] = None
    missing_frames: List[int] = field(default_factory=list)
    quality: str = "proxy"
    generation_id: int = 0

    @property
    def frame_indices(self) -> List[int]:
        """窗口内全部帧号（从最旧到最新，含中心帧）。"""
        def index_or_fallback(frame: Any, fallback: int) -> int:
            value = getattr(frame, "frame_index", fallback)
            try:
                return int(value)
            except (TypeError, ValueError):
                return fallback

        # previous_frames / next_frames are near→far.  Use the decoded frame's
        # actual index when available: a decoder may return a sparse window
        # after a dropped frame, so deriving indices from list lengths is wrong.
        before: List[int] = []
        for distance, frame in enumerate(reversed(self.previous_frames), start=1):
            if frame is not None:
                before.append(index_or_fallback(frame, self.center_frame - distance))
        after: List[int] = []
        for distance, frame in enumerate(self.next_frames, start=1):
            if frame is not None:
                after.append(index_or_fallback(frame, self.center_frame + distance))
        return before + [self.center_frame] + after

    def all_frames_ordered(self) -> List[Any]:
        """返回最旧→最新的帧数据列表（缺帧跳过，不含 None）。"""
        frames = [f for f in reversed(self.previous_frames) if f is not None]
        if self.center_frame_data is not None:
            frames.append(self.center_frame_data)
        frames.extend(f for f in self.next_frames if f is not None)
        return frames

    def is_degenerate(self) -> bool:
        """只有中心帧、无任何邻帧时，时域降噪应退化为空域/直通。"""
        return not self.previous_frames and not self.next_frames


class TemporalFrameWindowCache:
    """时域降噪消费者侧的窗口 LRU 缓存。

    AUD-38902 状态注记：本类属计划内（M2.2）预留能力，当前全库无生产
    消费者——暂停态实际路径由 window_provider 直接走
    request_frame_window，不经此缓存。锁/LRU/字节预算逻辑仅有单元测试
    （tests/video_denoise/test_frame_window.py）覆盖；接入新消费者前请
    先补齐集成验证。

    不可变契约：窗口对象放入后归缓存所有，消费者从 ``get`` 取得的
    ``FrameWindow`` 不得就地修改（否则会破坏字节记账与缓存一致性）。

    缓存键: (clip_id, frame_index, quality, generation_id, pixel_format, resolution)

    策略（计划 M2.2）：
    - 连续播放命中相邻窗口
    - generation_id 变化（seek / 换素材 / proxy↔master 切换）→ 全部失效
    - 内存预算按帧数据 ndarray 的 nbytes 估算；无 ndarray 的帧按 0 计
    """

    def __init__(self, memory_budget_mb: float = 512.0) -> None:
        self._lock = threading.RLock()
        self._entries: "OrderedDict[Tuple, Any]" = OrderedDict()
        self._memory_budget = int(memory_budget_mb * 1024 * 1024)
        self._current_bytes = 0
        self._hits = 0
        self._misses = 0

    # ------------------------------------------------------------------

    @staticmethod
    def make_key(
        clip_id: str,
        frame_index: int,
        quality: str,
        generation_id: int,
        pixel_format: str,
        resolution: Tuple[int, int],
    ) -> Tuple:
        return (
            str(clip_id),
            int(frame_index),
            str(quality),
            int(generation_id),
            str(pixel_format),
            (int(resolution[0]), int(resolution[1])),
        )

    @staticmethod
    def _estimate_bytes(frame: Any) -> int:
        # DecodedFrame: packed 在 .data，异步上传包装对象也可能使用
        # .frame_data；平面数据在 .planes。
        for attr in ("data", "frame_data"):
            data = getattr(frame, attr, None)
            if data is not None and hasattr(data, "nbytes"):
                return int(data.nbytes)
        planes = getattr(frame, "planes", None)
        if planes:
            return sum(
                int(getattr(p, "nbytes", 0) or 0) for p in planes
            )
        return 0

    @classmethod
    def _window_bytes(cls, window: FrameWindow) -> int:
        return sum(cls._estimate_bytes(frame) for frame in window.all_frames_ordered())

    # ------------------------------------------------------------------

    def put(
        self,
        clip_id: str,
        frame_index: int,
        quality: str,
        generation_id: int,
        pixel_format: str,
        resolution: Tuple[int, int],
        window: FrameWindow,
    ) -> None:
        key = self.make_key(
            clip_id, frame_index, quality, generation_id, pixel_format, resolution
        )
        total = self._window_bytes(window)
        with self._lock:
            if key in self._entries:
                old = self._entries.pop(key)
                self._current_bytes -= self._window_bytes(old)
            self._entries[key] = window
            self._current_bytes += total
            self._evict_locked()

    def get(
        self,
        clip_id: str,
        frame_index: int,
        quality: str,
        generation_id: int,
        pixel_format: str,
        resolution: Tuple[int, int],
    ) -> Optional[FrameWindow]:
        key = self.make_key(
            clip_id, frame_index, quality, generation_id, pixel_format, resolution
        )
        with self._lock:
            window = self._entries.get(key)
            if window is None:
                self._misses += 1
                return None
            self._entries.move_to_end(key)
            self._hits += 1
            return window

    # ------------------------------------------------------------------

    def invalidate_generation(self, clip_id: str, generation_id: int) -> None:
        """seek/切素材后清空该 clip 的旧 generation 缓存。"""
        with self._lock:
            stale = [
                k for k in self._entries
                if k[0] == clip_id and k[3] != generation_id
            ]
            for k in stale:
                stale_window = self._entries.pop(k, None)
                if stale_window is not None:
                    self._current_bytes -= self._window_bytes(stale_window)
            if self._current_bytes < 0:
                self._current_bytes = 0

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self._current_bytes = 0

    def get_stats(self) -> Dict[str, Any]:
        with self._lock:
            total = self._hits + self._misses
            return {
                "entries": len(self._entries),
                "memory_bytes": self._current_bytes,
                "memory_budget": self._memory_budget,
                "hits": self._hits,
                "misses": self._misses,
                "hit_rate": (self._hits / total) if total else 0.0,
            }

    def _evict_locked(self) -> None:
        while self._current_bytes > self._memory_budget and self._entries:
            _, evicted = self._entries.popitem(last=False)
            self._current_bytes -= self._window_bytes(evicted)
            if self._current_bytes < 0:
                self._current_bytes = 0
