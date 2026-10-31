"""
Frame Cache Module
帧缓存管理，用于存储解码后的帧数据。

## 职责

- FrameCache: 帧缓存容器
- CachePolicy: 缓存策略配置
- LRUEvictor: LRU 淘汰策略

## 设计原则

- 线程安全缓存访问
- 内存大小限制
- LRU 淘汰策略
- 支持 master/proxy 双缓存

## 使用示例

```python
from src.shared.media.cache import FrameCache, CachePolicy

# 创建帧缓存
cache = FrameCache(
    max_frames=16,
    max_memory_mb=512
)

# 存入帧
cache.put(frame_index=100, frame_data=decoded_frame)

# 获取帧
frame = cache.get(100)
```

## 与 SourceManager 的关系

SourceManager 维护两个独立的缓存：
- master_cache: 高质量帧缓存（暂停/逐帧使用）
- proxy_cache: 代理帧缓存（播放使用）
"""

from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Optional, Dict, Any, Tuple

import numpy as np

from .frame import DecodedFrame

logger = logging.getLogger(__name__)

# AUD-34318：get_missing_frames 超过该帧数跨度时告警（长时间线全量
# 扫描有可感知开销，提示调用方缩小区间或分批查询）
_MISSING_FRAMES_WARN_SPAN = 50_000


@dataclass(frozen=True)
class CachePolicy:
    """
    缓存策略配置。

    定义缓存的大小限制和淘汰规则。

    Attributes:
        max_frames: 最大帧数限制
        max_memory_mb: 最大内存限制（MB）
        ttl_seconds: 缓存条目存活时间（秒）
        prefetch_count: 预读帧数
        evict_policy: 淘汰策略（'lru', 'fifo'）
    """

    max_frames: int = 16
    max_memory_mb: int = 512
    ttl_seconds: float = 30.0
    prefetch_count: int = 4
    evict_policy: str = 'lru'  # 'lru' or 'fifo'


@dataclass
class CacheEntry:
    """
    缓存条目。

    存储单个帧的缓存信息。

    Attributes:
        frame_index: 帧号
        frame_data: 帧数据（DecodedFrame 或 ndarray）
        size_bytes: 数据大小（字节）
        timestamp: 存入时间戳
        access_count: 访问次数
        last_access: 最后访问时间
        is_master: 是否为高质量 master 帧
    """

    frame_index: int
    frame_data: Any  # DecodedFrame | np.ndarray
    size_bytes: int
    timestamp: float = field(default_factory=time.time)
    access_count: int = 0
    last_access: float = field(default_factory=time.time)
    is_master: bool = True

    def touch(self) -> None:
        """更新访问信息"""
        self.access_count += 1
        self.last_access = time.time()

    @property
    def age(self) -> float:
        """缓存条目年龄（秒）"""
        return time.time() - self.timestamp

    def is_expired(self, ttl: float) -> bool:
        """是否过期"""
        return self.age > ttl


class FrameCache:
    """
    帧缓存容器。

    线程安全的帧缓存，支持大小限制和 LRU 淘汰。

    Attributes:
        policy: 缓存策略
        _cache: 缓存数据（OrderedDict，支持 LRU）
        _lock: 线程锁
        _current_memory: 当前内存使用量（字节）
    """

    def __init__(
        self,
        policy: Optional[CachePolicy] = None,
        name: str = "default"
    ) -> None:
        """
        初始化帧缓存。

        Args:
            policy: 缓存策略配置
            name: 缓存名称（用于日志）
        """
        self.policy = policy or CachePolicy()
        self.name = name
        self._cache: OrderedDict[int, CacheEntry] = OrderedDict()
        self._lock = threading.RLock()
        self._current_memory: int = 0

        # 统计信息
        self._hit_count: int = 0
        self._miss_count: int = 0
        self._evict_count: int = 0

    def put(
        self,
        frame_index: int,
        frame_data: Any,
        is_master: bool = True
    ) -> bool:
        """
        存入帧数据。

        Args:
            frame_index: 帧号
            frame_data: 帧数据（DecodedFrame 或 ndarray）
            is_master: 是否为 master 帧

        Returns:
            True 表示成功存入

        Note:
            如果缓存已满，会自动淘汰最旧的条目
        """
        with self._lock:
            # 计算数据大小
            size_bytes = self._estimate_size(frame_data)

            # 检查是否超过内存限制
            while (
                self._current_memory + size_bytes > self.policy.max_memory_mb * 1024 * 1024
                or len(self._cache) >= self.policy.max_frames
            ):
                if not self._evict_one():
                    # 无法淘汰（缓存空），拒绝存入
                    logger.warning(
                        f"[{self.name}] Cache full, cannot store frame {frame_index}"
                    )
                    return False

            # 创建缓存条目
            entry = CacheEntry(
                frame_index=frame_index,
                frame_data=frame_data,
                size_bytes=size_bytes,
                is_master=is_master
            )

            # 如果已存在，先删除旧的
            if frame_index in self._cache:
                old_entry = self._cache[frame_index]
                self._current_memory -= old_entry.size_bytes
                del self._cache[frame_index]

            # 存入新条目
            self._cache[frame_index] = entry
            self._current_memory += size_bytes

            logger.debug(
                f"[{self.name}] Cached frame {frame_index}, "
                f"size={size_bytes}, total={len(self._cache)} frames, "
                f"memory={self._current_memory / 1024 / 1024:.1f} MB"
            )
            return True

    def get(
        self,
        frame_index: int,
        update_access: bool = True
    ) -> Optional[Any]:
        """
        获取帧数据。

        Args:
            frame_index: 帧号
            update_access: 是否更新访问信息（LRU）

        Returns:
            帧数据或 None（未找到）

        Note:
            LRU 模式下，访问的条目会移到末尾（最不容易被淘汰）
        """
        with self._lock:
            if frame_index not in self._cache:
                self._miss_count += 1
                return None

            entry = self._cache[frame_index]

            # 检查是否过期
            if entry.is_expired(self.policy.ttl_seconds):
                self._remove_entry(frame_index)
                self._miss_count += 1
                return None

            # 更新访问信息
            if update_access:
                entry.touch()
                # LRU: 移到末尾
                if self.policy.evict_policy == 'lru':
                    self._cache.move_to_end(frame_index)

            self._hit_count += 1
            return entry.frame_data

    def contains(self, frame_index: int) -> bool:
        """
        检查帧是否存在缓存中。

        Args:
            frame_index: 帧号

        Returns:
            True 表示存在
        """
        with self._lock:
            return frame_index in self._cache

    def remove(self, frame_index: int) -> bool:
        """
        手动删除指定帧。

        Args:
            frame_index: 帧号

        Returns:
            True 表示成功删除
        """
        with self._lock:
            return self._remove_entry(frame_index)

    def clear(self) -> None:
        """清空缓存"""
        with self._lock:
            self._cache.clear()
            self._current_memory = 0
            logger.info(f"[{self.name}] Cache cleared")

    def _remove_entry(self, frame_index: int) -> bool:
        """删除缓存条目（内部方法）"""
        if frame_index not in self._cache:
            return False

        entry = self._cache[frame_index]
        self._current_memory -= entry.size_bytes
        del self._cache[frame_index]
        return True

    def _evict_one(self) -> bool:
        """淘汰一个条目（内部方法）"""
        if not self._cache:
            return False

        # FIFO/LRU: 淘汰最旧的（OrderedDict 的第一个）
        oldest_index = next(iter(self._cache))
        self._remove_entry(oldest_index)
        self._evict_count += 1

        logger.debug(
            f"[{self.name}] Evicted frame {oldest_index}, "
            f"remaining={len(self._cache)}"
        )
        return True

    def _estimate_size(self, frame_data: Any) -> int:
        """
        估算帧数据大小。

        Args:
            frame_data: 帧数据

        Returns:
            大小（字节）
        """
        if isinstance(frame_data, DecodedFrame):
            if frame_data.data is not None:
                return frame_data.data.nbytes
            elif frame_data.planes:
                return sum(p.nbytes for p in frame_data.planes)
            return 0
        elif isinstance(frame_data, np.ndarray):
            return frame_data.nbytes
        else:
            # 未知类型，估算为 0
            return 0

    def get_stats(self) -> Dict[str, Any]:
        """
        获取缓存统计信息。

        Returns:
            统计数据字典
        """
        with self._lock:
            hit_rate = (
                self._hit_count / (self._hit_count + self._miss_count)
                if (self._hit_count + self._miss_count) > 0
                else 0.0
            )

            return {
                'name': self.name,
                'frames': len(self._cache),
                'memory_mb': self._current_memory / 1024 / 1024,
                'max_frames': self.policy.max_frames,
                'max_memory_mb': self.policy.max_memory_mb,
                'hit_count': self._hit_count,
                'miss_count': self._miss_count,
                'evict_count': self._evict_count,
                'hit_rate': hit_rate,
            }

    def get_cached_range(self) -> Optional[Tuple[int, int]]:
        """
        获取缓存的帧范围。

        Returns:
            (min_frame, max_frame) 或 None（缓存空）
        """
        with self._lock:
            if not self._cache:
                return None
            return (min(self._cache.keys()), max(self._cache.keys()))

    def get_missing_frames(
        self,
        start_frame: int,
        end_frame: int
    ) -> list[int]:
        """
        获取指定范围内缺失的帧号列表。

        Args:
            start_frame: 起始帧号（负值按 0 截断）
            end_frame: 结束帧号（小于 start_frame 时告警并返回空列表）

        Returns:
            缺失的帧号列表
        """
        # AUD-34318：区间参数校验——非法区间告警，不再静默返回空列表
        # 掩盖调用错误
        if start_frame > end_frame:
            logger.warning(
                f"get_missing_frames: 非法区间 start={start_frame} > "
                f"end={end_frame}，返回空列表"
            )
            return []
        if start_frame < 0:
            start_frame = 0
        span = end_frame - start_frame + 1
        if span > _MISSING_FRAMES_WARN_SPAN:
            logger.warning(
                f"get_missing_frames: 区间过大（{span} 帧 > "
                f"{_MISSING_FRAMES_WARN_SPAN}），全量扫描可能造成可感知开销"
            )
        # AUD-34318：锁内仅做缓存键快照（条目数受 max_frames 约束），
        # 大区间成员判断在锁外进行，避免长时间顶住解码线程的 put/get
        with self._lock:
            cached_keys = frozenset(self._cache.keys())
        return [
            i for i in range(start_frame, end_frame + 1)
            if i not in cached_keys
        ]

    def prefetch_hint(
        self,
        current_frame: int,
        direction: str = 'forward'
    ) -> list[int]:
        """
        生成预读提示。

        根据当前帧和播放方向，建议预读哪些帧。
        负帧号（片头 backward/bidirectional 越界）已被过滤（AUD-34318）。

        Args:
            current_frame: 当前帧号
            direction: 播放方向（'forward', 'backward', 'bidirectional'）

        Returns:
            建议预读的帧号列表（全部 >= 0）
        """
        prefetch_count = self.policy.prefetch_count

        if direction == 'forward':
            return [
                frame
                for frame in (current_frame + i for i in range(1, prefetch_count + 1))
                if frame >= 0
            ]
        elif direction == 'backward':
            return [
                frame
                for frame in (current_frame - i for i in range(1, prefetch_count + 1))
                if frame >= 0
            ]
        else:  # bidirectional
            frames = []
            for i in range(1, prefetch_count // 2 + 1):
                frames.append(current_frame + i)
                frames.append(current_frame - i)
            return [frame for frame in frames if frame >= 0]


class DualFrameCache:
    """
    双缓存容器。

    维护 master 和 proxy 两个独立的缓存，
    用于 SourceManager 的双路径架构。

    Attributes:
        master_cache: 高质量帧缓存
        proxy_cache: 代理帧缓存
    """

    def __init__(
        self,
        master_policy: Optional[CachePolicy] = None,
        proxy_policy: Optional[CachePolicy] = None
    ) -> None:
        """
        初始化双缓存。

        Args:
            master_policy: master 缓存策略（默认较小，高质量帧）
            proxy_policy: proxy 缓存策略（默认较大，播放用）
        """
        # master 缓存：高质量帧，通常用于暂停/逐帧
        # 较小的缓存，保留最近访问的帧
        default_master = CachePolicy(
            max_frames=4,
            max_memory_mb=256,
            ttl_seconds=60.0,
            prefetch_count=2
        )
        self.master_cache = FrameCache(
            policy=master_policy or default_master,
            name="master"
        )

        # proxy 缓存：代理帧，用于播放
        # 较大的缓存，支持流畅播放
        default_proxy = CachePolicy(
            max_frames=16,
            max_memory_mb=512,
            ttl_seconds=30.0,
            prefetch_count=4
        )
        self.proxy_cache = FrameCache(
            policy=proxy_policy or default_proxy,
            name="proxy"
        )

    def put_master(self, frame_index: int, frame_data: Any) -> bool:
        """存入 master 帧"""
        return self.master_cache.put(frame_index, frame_data, is_master=True)

    def put_proxy(self, frame_index: int, frame_data: Any) -> bool:
        """存入 proxy 帧"""
        return self.proxy_cache.put(frame_index, frame_data, is_master=False)

    def get_master(self, frame_index: int) -> Optional[Any]:
        """获取 master 帧"""
        return self.master_cache.get(frame_index)

    def get_proxy(self, frame_index: int) -> Optional[Any]:
        """获取 proxy 帧"""
        return self.proxy_cache.get(frame_index)

    def has_master(self, frame_index: int) -> bool:
        """检查是否有 master 帧"""
        return self.master_cache.contains(frame_index)

    def has_proxy(self, frame_index: int) -> bool:
        """检查是否有 proxy 帧"""
        return self.proxy_cache.contains(frame_index)

    def clear_all(self) -> None:
        """清空所有缓存"""
        self.master_cache.clear()
        self.proxy_cache.clear()

    def get_combined_stats(self) -> Dict[str, Any]:
        """获取合并的统计信息"""
        return {
            'master': self.master_cache.get_stats(),
            'proxy': self.proxy_cache.get_stats(),
        }