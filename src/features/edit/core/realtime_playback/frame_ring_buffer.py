"""
Frame Ring Buffer
单 stream 的 ready frame 环形缓冲（规格书 §11 / Stage E1）。

职责：
- 固定容量保存每个 ClipDecodeStream 的 ready frames。
- 支持精确查找（get_exact）、窗口淘汰（evict_outside_window）、seek flush。
- 统计 hit/miss/evict，供 telemetry 与 ring hit rate 验收使用。

容量建议（规格书 §11.2）：
- 单轨 25fps: 32-64 frames
- 双轨 25fps: 每 stream 24-48 frames
- 4K proxy: 16-32 frames
- scrub: 8-16 frames

线程安全：所有操作加锁。decode worker 写入，scheduler/engine 读取。
"""

from __future__ import annotations

import logging
import threading
from collections import OrderedDict
from typing import Dict, Optional

from .types import FrameHandle

logger = logging.getLogger(__name__)


class FrameRingBuffer:
    """
    固定容量的 source_frame -> FrameHandle 缓冲。

    用 OrderedDict 实现 LRU + 窗口淘汰。容量满时淘汰最旧的帧。
    """

    def __init__(self, capacity: int = 48) -> None:
        self._capacity = max(4, int(capacity))
        self._frames: "OrderedDict[int, FrameHandle]" = OrderedDict()
        self._lock = threading.RLock()
        # 统计
        self._hits = 0
        self._misses = 0
        self._puts = 0
        self._evicts = 0
        self._flushes = 0

    # ------------------------------------------------------------------
    # put / get
    # ------------------------------------------------------------------

    @staticmethod
    def _release_handle(handle: FrameHandle | None) -> None:
        """Release an external GPU payload dropped before compositor consume."""
        if handle is None:
            return
        try:
            from src.shared.media.gpu_frame import safe_release_gpu_frame

            safe_release_gpu_frame(handle.payload)
        except Exception:
            # Ring eviction is housekeeping; it must never break the decode
            # producer if an optional release implementation is unavailable.
            logger.debug("FrameRingBuffer payload release failed", exc_info=True)

    def put(self, frame: FrameHandle) -> None:
        """放入一个 ready frame。重复 frame_index 会覆盖。"""
        dropped: list[FrameHandle] = []
        with self._lock:
            idx = frame.frame_index
            if idx in self._frames:
                # 覆盖，移到末尾（最新）
                self._frames.move_to_end(idx)
                previous = self._frames[idx]
                self._frames[idx] = frame
                if previous is not frame:
                    dropped.append(previous)
            else:
                self._frames[idx] = frame
                self._puts += 1
            # 容量淘汰
            while len(self._frames) > self._capacity:
                _, evicted = self._frames.popitem(last=False)
                dropped.append(evicted)
                self._evicts += 1
        for evicted in dropped:
            self._release_handle(evicted)

    def get_exact(self, source_frame: int) -> Optional[FrameHandle]:
        """精确查找。命中计 hit，未命中计 miss。"""
        with self._lock:
            frame = self._frames.get(source_frame)
            if frame is not None:
                self._hits += 1
                # 命中后移到末尾（最近使用）
                self._frames.move_to_end(source_frame)
                return frame
            self._misses += 1
            return None

    def take_exact(self, source_frame: int) -> Optional[FrameHandle]:
        """取出并转移一帧的所有权。

        ``get_exact`` 适合 peek/复用，但 GPU 帧交给 compositor 后，ring
        不能继续持有同一个 external resource。scheduler 消费 bundle 时
        使用本方法，把句柄所有权从 ring 转交给渲染任务；命中/未命中仍
        计入同一组统计。
        """
        with self._lock:
            frame = self._frames.pop(source_frame, None)
            if frame is not None:
                self._hits += 1
                return frame
            self._misses += 1
            return None

    def contains(self, source_frame: int) -> bool:
        """是否包含某帧（不计统计）。"""
        with self._lock:
            return source_frame in self._frames

    def peek_newest(self) -> Optional[FrameHandle]:
        """返回最新的帧（用于 hold previous）。"""
        with self._lock:
            if not self._frames:
                return None
            idx = next(reversed(self._frames))
            return self._frames[idx]

    # ------------------------------------------------------------------
    # 窗口淘汰 / flush
    # ------------------------------------------------------------------

    def evict_outside_window(self, start: int, end: int) -> int:
        """
        淘汰 [start, end] 窗口外的帧。

        用于播放窗口推进时清理旧帧（规格书 §11 / §9.3）。
        返回淘汰数量。
        """
        evicted = 0
        dropped: list[FrameHandle] = []
        with self._lock:
            stale = [idx for idx in self._frames if idx < start or idx > end]
            for idx in stale:
                handle = self._frames.pop(idx, None)
                if handle is not None:
                    dropped.append(handle)
                self._evicts += 1
                evicted += 1
        for handle in dropped:
            self._release_handle(handle)
        return evicted

    def clear(self, reason: str = "") -> None:
        """清空（seek flush 时调用）。"""
        with self._lock:
            dropped = list(self._frames.values())
            n = len(dropped)
            self._frames.clear()
            self._flushes += 1
            if n > 0:
                logger.debug(f"FrameRingBuffer cleared {n} frames: {reason}")
        for handle in dropped:
            self._release_handle(handle)

    # ------------------------------------------------------------------
    # 统计
    # ------------------------------------------------------------------

    def stats(self) -> Dict[str, int | float]:
        with self._lock:
            total = self._hits + self._misses
            hit_rate = (self._hits / total) if total > 0 else 0.0
            return {
                "capacity": self._capacity,
                "size": len(self._frames),
                "hits": self._hits,
                "misses": self._misses,
                "puts": self._puts,
                "evicts": self._evicts,
                "flushes": self._flushes,
                "hit_rate": round(hit_rate, 4),
            }

    def reset_stats(self) -> None:
        with self._lock:
            self._hits = 0
            self._misses = 0
            self._puts = 0
            self._evicts = 0
            self._flushes = 0

    # ------------------------------------------------------------------
    # properties
    # ------------------------------------------------------------------

    @property
    def capacity(self) -> int:
        return self._capacity

    @property
    def size(self) -> int:
        with self._lock:
            return len(self._frames)

    @property
    def frame_indices(self) -> tuple[int, ...]:
        with self._lock:
            return tuple(self._frames.keys())
