"""
Frame Cache Module
帧缓存管理，支持 LRU 驱逐策略和内存限制。

## 职责

- FrameCacheEntry: 缓存条目数据结构
- FrameCache: LRU 缓存管理器

## 设计原则

- LRU 驱逐：基于最近使用时间
- 内存限制：可配置最大内存使用
- 统计监控：缓存命中率、内存使用
- 线程安全：缓存操作使用锁保护

## 使用示例

```python
from src.shared.media import FrameCache, DecodedFrame

# 创建缓存（500MB 上限）
cache = FrameCache(max_memory_mb=500)

# 存入帧
cache.put(frame)

# 获取帧（会更新 last_used_time）
cached_frame = cache.get(frame_index=100, is_master=True)

# 获取统计
stats = cache.get_stats()
print(f"entries: {stats['entries']}, memory: {stats['memory_mb']}MB")

# 清空缓存
cache.clear()
```
"""

from __future__ import annotations

import threading
import time
import logging
from dataclasses import dataclass, field
from typing import Optional, TYPE_CHECKING
from collections import OrderedDict

if TYPE_CHECKING:
    from .frame import DecodedFrame

logger = logging.getLogger(__name__)


def calculate_frame_memory(frame: DecodedFrame) -> int:
    """
    计算帧数据占用的内存大小（字节）。

    Args:
        frame: DecodedFrame 实例

    Returns:
        内存大小（字节）
    """
    total_size = 0

    # 打包格式
    if frame.is_packed and frame.data is not None:
        total_size += frame.data.nbytes

    # 平面格式
    if frame.is_planar and frame.planes:
        for plane in frame.planes:
            if hasattr(plane, 'nbytes'):
                total_size += plane.nbytes
            else:
                # ndarray nbytes
                import numpy as np
                total_size += np.asarray(plane).nbytes

    # 添加元数据开销估算（约 100 字节）
    total_size += 100

    return total_size


@dataclass
class FrameCacheEntry:
    """
    缓存条目数据结构。

    Attributes:
        frame: 解码后的帧数据
        frame_index: 帧号（从 0 开始）
        is_master: 是否为母版帧（用于区分不同版本）
        last_used_time: 最后使用时间戳（用于 LRU）
        memory_size: 帧数据占用的内存大小（字节）
        insert_time: 插入缓存的时间戳
        hit_count: 缓存命中次数
    """
    frame: DecodedFrame
    frame_index: int
    is_master: bool
    last_used_time: float = field(default_factory=time.monotonic)
    memory_size: int = 0
    insert_time: float = field(default_factory=time.monotonic)
    hit_count: int = 0

    def __post_init__(self) -> None:
        """初始化派生属性"""
        if self.memory_size == 0:
            self.memory_size = calculate_frame_memory(self.frame)

    def touch(self) -> None:
        """更新最后使用时间（标记为最近使用）"""
        self.last_used_time = time.monotonic()
        self.hit_count += 1

    def age(self) -> float:
        """
        计算条目的年龄（从插入到现在的时间）。

        Returns:
            年龄（秒）
        """
        return time.monotonic() - self.insert_time

    def idle_time(self) -> float:
        """
        计算条目的空闲时间（从最后使用到现在的时间）。

        Returns:
            空闲时间（秒）
        """
        return time.monotonic() - self.last_used_time

    def __repr__(self) -> str:
        """字符串表示"""
        return (
            f"FrameCacheEntry("
            f"frame={self.frame_index}, "
            f"master={self.is_master}, "
            f"size={self.memory_size / 1024:.1f}KB, "
            f"hits={self.hit_count}, "
            f"idle={self.idle_time():.1f}s)"
        )


class FrameCache:
    """
    帧缓存管理器。

    使用 LRU（Least Recently Used）策略管理帧缓存，
    支持内存上限配置和自动驱逐。

    Features:
    - LRU 驱逐：基于 last_used_time
    - 内存限制：可配置 max_memory_mb
    - 统计监控：命中率、内存使用
    - 线程安全：锁保护

    Usage:
        cache = FrameCache(max_memory_mb=500)
        cache.put(frame)
        cached = cache.get(frame_index=100, is_master=True)
        cache.clear()
    """

    def __init__(
        self,
        max_memory_mb: int = 500,
        max_entries: int = 1000,
        min_idle_time_for_eviction: float = 2.0
    ) -> None:
        """
        初始化帧缓存。

        Args:
            max_memory_mb: 最大内存使用（MB）
            max_entries: 最大条目数量
            min_idle_time_for_eviction: 最小空闲时间（秒），超过此时间才可被驱逐
        """
        self._max_memory = max_memory_mb * 1024 * 1024
        self._max_entries = max_entries
        self._min_idle_time = min_idle_time_for_eviction

        # 缓存存储（使用 OrderedDict 实现 LRU）
        # key: (frame_index, is_master) -> entry
        self._entries: OrderedDict[tuple[int, bool], FrameCacheEntry] = OrderedDict()
        self._current_memory: int = 0
        self._lock = threading.RLock()

        # 统计信息
        self._stats = {
            'inserts': 0,
            'hits': 0,
            'misses': 0,
            'evictions': 0,
            'evictions_memory': 0,
            'evictions_for_memory_limit': 0,
            'evictions_for_entry_limit': 0,
        }

        logger.info(
            f"FrameCache: 初始化完成, "
            f"max_memory={max_memory_mb}MB, "
            f"max_entries={max_entries}, "
            f"min_idle={min_idle_time_for_eviction}s"
        )

    def get(
        self,
        frame_index: int,
        is_master: bool = False
    ) -> Optional[DecodedFrame]:
        """
        从缓存获取帧。

        如果找到帧，会更新其 last_used_time（LRU）。

        Args:
            frame_index: 帧号
            is_master: 是否获取母版帧

        Returns:
            DecodedFrame 或 None（未命中）
        """
        key = (frame_index, is_master)

        with self._lock:
            entry = self._entries.get(key)

            if entry is None:
                self._stats['misses'] += 1
                logger.debug(f"FrameCache: 未命中 frame={frame_index}, master={is_master}")
                return None

            # 命中，更新 LRU
            entry.touch()

            # 移动到末尾（最近使用）
            self._entries.move_to_end(key)

            self._stats['hits'] += 1
            logger.debug(
                f"FrameCache: 命中 frame={frame_index}, "
                f"master={is_master}, "
                f"hits={entry.hit_count}"
            )

            return entry.frame

    def get_entry(
        self,
        frame_index: int,
        is_master: bool = False
    ) -> Optional[FrameCacheEntry]:
        """
        从缓存获取条目（包含元数据）。

        Args:
            frame_index: 帧号
            is_master: 是否获取母版帧

        Returns:
            FrameCacheEntry 或 None
        """
        key = (frame_index, is_master)

        with self._lock:
            entry = self._entries.get(key)

            if entry is None:
                self._stats['misses'] += 1
                return None

            # 命中，更新 LRU
            entry.touch()
            self._entries.move_to_end(key)
            self._stats['hits'] += 1

            return entry

    def put(
        self,
        frame: DecodedFrame,
        is_master: bool = False,
        force: bool = False
    ) -> bool:
        """
        将帧存入缓存。

        如果内存超限，会触发 LRU 驱逐。

        Args:
            frame: 要缓存的帧
            is_master: 是否为母版帧
            force: 是否强制插入（跳过内存检查）

        Returns:
            True 表示成功插入，False 表示插入失败（内存超限）
        """
        key = (frame.frame_index, is_master)
        memory_size = calculate_frame_memory(frame)

        with self._lock:
            # AUD-34805：更新已存在键同样必须经过内存上限与驱逐检查。
            # 旧实现在此提前 return True，完全绕过上限——反复 put 同一
            # 帧号（如更大的重建帧）可无限突破 _max_memory。统一做法：
            # 先按"移除旧条目"记账，再走与新插入相同的限额/驱逐路径。
            old_entry = self._entries.pop(key, None)
            if old_entry is not None:
                self._current_memory -= old_entry.memory_size

            # 检查内存限制（除非 force=True）
            if not force:
                # 尝试腾出空间
                while (self._current_memory + memory_size > self._max_memory or
                       len(self._entries) >= self._max_entries):

                    # 尝试驱逐最旧的条目
                    evicted = self._evict_one()

                    if not evicted:
                        # 无法腾出空间：回滚旧条目（保持更新前状态），插入失败
                        if old_entry is not None:
                            self._entries[key] = old_entry
                            self._current_memory += old_entry.memory_size
                        logger.warning(
                            f"FrameCache: 无法腾出空间，拒绝插入 "
                            f"frame={frame.frame_index}, "
                            f"size={memory_size / 1024:.1f}KB"
                        )
                        return False

            # 创建新条目
            entry = FrameCacheEntry(
                frame=frame,
                frame_index=frame.frame_index,
                is_master=is_master,
                memory_size=memory_size
            )

            self._entries[key] = entry
            self._current_memory += memory_size
            self._stats['inserts'] += 1

            logger.debug(
                f"FrameCache: 插入条目 frame={frame.frame_index}, "
                f"master={is_master}, "
                f"size={memory_size / 1024:.1f}KB, "
                f"total_memory={self._current_memory / 1024 / 1024:.1f}MB"
            )

            return True

    def evict_oldest(self, force: bool = False) -> Optional[FrameCacheEntry]:
        """
        驱逐最旧的条目。

        Args:
            force: 是否强制驱逐（忽略 min_idle_time）

        Returns:
            被驱逐的条目，或 None（无符合条件的条目）
        """
        with self._lock:
            return self._evict_one(force=force)

    def evict_range(
        self,
        start_frame: int,
        end_frame: int,
        is_master: Optional[bool] = None
    ) -> list[FrameCacheEntry]:
        """
        驱逐指定帧范围内的条目。

        Args:
            start_frame: 起始帧号
            end_frame: 结束帧号
            is_master: 只驱逐指定类型的帧（None 表示所有）

        Returns:
            被驱逐的条目列表
        """
        evicted_list: list[FrameCacheEntry] = []

        with self._lock:
            keys_to_evict: list[tuple[int, bool]] = []

            for key, entry in self._entries.items():
                frame_idx, master = key
                if start_frame <= frame_idx <= end_frame:
                    if is_master is None or master == is_master:
                        keys_to_evict.append(key)

            # 执行驱逐
            for key in keys_to_evict:
                entry = self._entries.pop(key)
                self._current_memory -= entry.memory_size
                self._stats['evictions'] += 1
                self._stats['evictions_memory'] += entry.memory_size
                evicted_list.append(entry)

        if evicted_list:
            logger.debug(
                f"FrameCache: 驱逐范围 "
                f"frames={start_frame}-{end_frame}, "
                f"count={len(evicted_list)}"
            )

        return evicted_list

    def clear(self) -> int:
        """
        清空缓存。

        Returns:
            清除的条目数量
        """
        with self._lock:
            count = len(self._entries)
            memory_freed = self._current_memory

            self._entries.clear()
            self._current_memory = 0

            logger.info(
                f"FrameCache: 清空缓存, "
                f"count={count}, "
                f"freed={memory_freed / 1024 / 1024:.1f}MB"
            )

            return count

    def get_stats(self) -> dict:
        """
        获取缓存统计信息。

        Returns:
            包含统计数据的字典
        """
        with self._lock:
            total_requests = self._stats['hits'] + self._stats['misses']
            hit_rate = (
                self._stats['hits'] / total_requests
                if total_requests > 0
                else 0.0
            )

            return {
                'entries': len(self._entries),
                'memory_mb': self._current_memory / 1024 / 1024,
                'max_memory_mb': self._max_memory / 1024 / 1024,
                'memory_usage_percent': (
                    self._current_memory / self._max_memory * 100
                    if self._max_memory > 0
                    else 0.0
                ),
                'hits': self._stats['hits'],
                'misses': self._stats['misses'],
                'hit_rate': hit_rate,
                'inserts': self._stats['inserts'],
                'evictions': self._stats['evictions'],
                'evictions_memory_mb': self._stats['evictions_memory'] / 1024 / 1024,
            }

    def get_memory_info(self) -> dict:
        """
        获取内存使用详情。

        Returns:
            内存使用信息字典
        """
        with self._lock:
            master_frames = len([e for k, e in self._entries.items() if k[1]])
            proxy_frames = len(self._entries) - master_frames

            return {
                'total_memory_bytes': self._current_memory,
                'total_memory_mb': self._current_memory / 1024 / 1024,
                'max_memory_bytes': self._max_memory,
                'max_memory_mb': self._max_memory / 1024 / 1024,
                'available_memory_bytes': self._max_memory - self._current_memory,
                'available_memory_mb': (
                    (self._max_memory - self._current_memory) / 1024 / 1024
                ),
                'entry_count': len(self._entries),
                'master_frames': master_frames,
                'proxy_frames': proxy_frames,
            }

    def has_frame(self, frame_index: int, is_master: bool = False) -> bool:
        """
        检查帧是否在缓存中。

        Args:
            frame_index: 帧号
            is_master: 是否检查母版帧

        Returns:
            是否存在
        """
        key = (frame_index, is_master)
        with self._lock:
            return key in self._entries

    def get_oldest_entries(self, count: int = 10) -> list[FrameCacheEntry]:
        """
        获取最旧的条目列表。

        Args:
            count: 返回数量

        Returns:
            最旧的条目列表（LRU 访问序，即 last_used_time 升序——
            get/put 均刷新时间戳并 move_to_end，队首为最久未访问条目）
        """
        with self._lock:
            # AUD-34807：OrderedDict 维护的是 LRU 访问序（命中时
            # move_to_end），非插入顺序；前面的最久未访问
            entries = list(self._entries.values())
            return entries[:count]

    # ================================================================
    # 内部方法
    # ================================================================

    def _evict_one(self, force: bool = False) -> Optional[FrameCacheEntry]:
        """
        驱逐一个条目（内部方法）。

        遵循 LRU 策略，优先驱逐空闲时间最长的条目。
        如果 force=False，只驱逐超过 min_idle_time 的条目。

        Args:
            force: 是否强制驱逐（忽略空闲时间限制）

        Returns:
            被驱逐的条目，或 None
        """
        # AUD-34807：条目序即 LRU 访问序（get/put 均刷新 last_used_time
        # 并 move_to_end），首个满足可驱逐条件的条目即空闲最久者——
        # 无需全表查找最小 last_used_time（此前每次驱逐 O(n) 全表扫描）
        evict_key: Optional[tuple[int, bool]] = None

        for key, entry in self._entries.items():
            idle = entry.idle_time()

            # 检查是否可驱逐
            if force or idle >= self._min_idle_time:
                evict_key = key
                break

        if evict_key is None:
            # 没有可驱逐的条目
            return None

        # 执行驱逐
        entry = self._entries.pop(evict_key)
        self._current_memory -= entry.memory_size
        self._stats['evictions'] += 1
        self._stats['evictions_memory'] += entry.memory_size

        # 记录驱逐原因
        if self._current_memory > self._max_memory:
            self._stats['evictions_for_memory_limit'] += 1
        elif len(self._entries) >= self._max_entries:
            self._stats['evictions_for_entry_limit'] += 1

        logger.debug(
            f"FrameCache: 驱逐 frame={entry.frame_index}, "
            f"master={entry.is_master}, "
            f"size={entry.memory_size / 1024:.1f}KB, "
            f"idle={entry.idle_time():.1f}s"
        )

        return entry

    def __repr__(self) -> str:
        """字符串表示"""
        stats = self.get_stats()
        return (
            f"FrameCache("
            f"entries={stats['entries']}, "
            f"memory={stats['memory_mb']:.1f}MB, "
            f"hit_rate={stats['hit_rate']:.1%})"
        )

    def __len__(self) -> int:
        """返回缓存条目数量"""
        with self._lock:
            return len(self._entries)

    def __contains__(self, key: tuple[int, bool]) -> bool:
        """检查键是否在缓存中"""
        with self._lock:
            return key in self._entries