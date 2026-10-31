"""
Unified Cache Manager Module

实现多级预览缓存策略，协调各级缓存的生命周期。

## 职责

- CacheManager: 统一缓存管理器，协调所有缓存层级
- CacheStats: 缓存统计信息（命中率、内存使用、淘汰策略）
- CacheTier: 缓存层级枚举
- CacheWarmer: 缓存预热策略
- MemoryPressureMonitor: 内存压力监控与自动清理

## 缓存层级

```text
┌─────────────────────────────────────────────────────────────────┐
│ CacheManager (统一管理器)                                         │
│ - 协调所有缓存层级                                                 │
│ - 监控内存压力                                                     │
│ - 提供统一统计接口                                                  │
└────────────────────────┬────────────────────────────────────────┘
                         │
          ┌──────────────┼──────────────┐
          │              │              │
          ▼              ▼              ▼
┌─────────────────┐ ┌─────────────┐ ┌──────────────────┐
│ DecodeFrameCache│ │RenderPlan   │ │GPUTextureCache   │
│ (解码帧)         │ │Cache        │ │(GPU 纹理)        │
│                 │ │(渲染计划)    │ │                  │
│ - Proxy 帧      │ │- FramePlan  │ │- Texture Pool    │
│ - Master 帧     │ │- ClipPlan   │ │- FBO Cache       │
│ - 帧队列        │ │- Transition │ │- Shader Cache    │
└─────────────────┘ └─────────────┘ └──────────────────┘
          │              │              │
          └──────────────┼──────────────┘
                         │
          ┌──────────────┼──────────────┐
          │              │              │
          ▼              ▼              ▼
┌─────────────────┐ ┌─────────────┐ ┌──────────────────┐
│TimelinePreview  │ │AudioWaveform│ │ThumbnailCache    │
│Cache            │ │Cache        │ │(缩略图)           │
│(时间线预览)      │ │(音频波形)    │ │                  │
│- Rendered Frame │ │- Peak Data  │ │- Image Thumb     │
│- Composite      │ │- Samples    │ │- Video Thumb     │
└─────────────────┘ └─────────────┘ └──────────────────┘
```

## 设计原则

- 线程安全：所有缓存操作使用 threading.Lock
- LRU 淘汰：自动淘汰最久未使用的条目
- 可配置大小：每个缓存层级独立配置
- 缓存键设计：帧号 + 质量级别 + 分辨率 + 色彩空间
- 内存压力响应：自动清理以响应系统内存压力
- 完整类型注解

## 使用示例

```python
from src.features.color.core.cache_manager import CacheManager, CacheConfig

# 创建缓存管理器
config = CacheConfig(
    max_decode_frames=32,
    max_render_plans=16,
    max_gpu_textures=8,
)
cache_mgr = CacheManager(config)

# 解码帧缓存
cache_mgr.decode_cache.put(clip_id, frame_index, quality, frame_data)
frame = cache_mgr.decode_cache.get(clip_id, frame_index, quality)

# 渲染计划缓存
cache_mgr.render_plan_cache.put(frame_index, render_plan)
plan = cache_mgr.render_plan_cache.get(frame_index)

# 获取统计信息
stats = cache_mgr.get_stats()
print(f"Hit rate: {stats['total_hit_rate']:.2%}")

# 内存压力清理
cache_mgr.on_memory_pressure(level="high")
```
"""

from __future__ import annotations

import logging
import threading
from collections import OrderedDict
from dataclasses import dataclass
from enum import Enum, auto
from typing import (
    Any,
    Callable,
    Dict,
    Generic,
    List,
    Optional,
    TypeVar,
)

import numpy as np

logger = logging.getLogger(__name__)


# ================================================================
# 类型定义
# ================================================================

T = TypeVar('T')


# ================================================================
# 缓存层级枚举
# ================================================================

class CacheTier(Enum):
    """
    缓存层级枚举。

    定义缓存的优先级和内存压力下的清理顺序。
    """
    DECODE_FRAME = auto()      # 解码帧缓存（最高优先级，最难重新生成）
    RENDER_PLAN = auto()       # 渲染计划缓存
    GPU_TEXTURE = auto()       # GPU 纹理缓存
    TIMELINE_PREVIEW = auto()  # 时间线预览缓存
    AUDIO_WAVEFORM = auto()    # 音频波形缓存
    THUMBNAIL = auto()         # 缩略图缓存（最低优先级，最容易重新生成）


# ================================================================
# 缓存键
# ================================================================

@dataclass(frozen=True)
class FrameCacheKey:
    """
    帧缓存键。

    考虑：帧号、质量级别、分辨率、色彩空间

    Attributes:
        clip_id: 剪辑 ID
        frame_index: 帧号
        quality: 质量级别 ('proxy', 'master')
        width: 宽度（用于 proxy 区分不同分辨率）
        height: 高度
        color_space: 色彩空间 ('RGB', 'BGR', 'YUV')
    """
    clip_id: str
    frame_index: int
    quality: str
    width: int
    height: int
    color_space: str = "RGB"

    def __repr__(self) -> str:
        return (
            f"FrameKey(clip={self.clip_id[:8]}..., "
            f"frame={self.frame_index}, "
            f"q={self.quality}, "
            f"{self.width}x{self.height}, "
            f"cs={self.color_space})"
        )


@dataclass(frozen=True)
class RenderPlanCacheKey:
    """
    渲染计划缓存键。

    Attributes:
        frame_index: 帧号
        viewport_width: 视口宽度
        viewport_height: 视口高度
        quality: 质量级别
    """
    frame_index: int
    viewport_width: int
    viewport_height: int
    quality: str = "preview"


@dataclass(frozen=True)
class TextureCacheKey:
    """
    GPU 纹理缓存键。

    Attributes:
        width: 纹理宽度
        height: 纹理高度
        components: 通道数
        dtype: 数据类型
        usage: 用途标识
    """
    width: int
    height: int
    components: int
    dtype: str
    usage: str = "frame"


@dataclass(frozen=True)
class PreviewCacheKey:
    """
    时间线预览缓存键。

    Attributes:
        frame_index: 帧号
        viewport_width: 视口宽度
        viewport_height: 视口高度
        composite_mode: 合成模式
    """
    frame_index: int
    viewport_width: int
    viewport_height: int
    composite_mode: str = "default"


@dataclass(frozen=True)
class WaveformCacheKey:
    """
    音频波形缓存键。

    Attributes:
        asset_id: 素材 ID
        sample_rate: 采样率
        num_channels: 通道数
    """
    asset_id: str
    sample_rate: int = 44100
    num_channels: int = 1


@dataclass(frozen=True)
class ThumbnailCacheKey:
    """
    缩略图缓存键。

    Attributes:
        asset_path: 素材路径
        size: 缩略图尺寸
        format: 格式 ('png', 'webp')
    """
    asset_path: str
    size: int = 128
    format: str = "webp"


# ================================================================
# 缓存统计
# ================================================================

@dataclass
class CacheStatistics:
    """
    缓存统计信息。

    Attributes:
        hits: 命中次数
        misses: 未命中次数
        evictions: 淘汰次数
        inserts: 插入次数
        total_bytes: 总字节数（估算）
    """
    hits: int = 0
    misses: int = 0
    evictions: int = 0
    inserts: int = 0
    total_bytes: int = 0

    @property
    def hit_rate(self) -> float:
        """命中率"""
        total = self.hits + self.misses
        if total == 0:
            return 0.0
        return self.hits / total

    @property
    def miss_rate(self) -> float:
        """未命中率"""
        return 1.0 - self.hit_rate

    def merge(self, other: CacheStatistics) -> CacheStatistics:
        """
        合并另一个统计信息。

        Args:
            other: 要合并的统计信息

        Returns:
            新的合并统计信息
        """
        return CacheStatistics(
            hits=self.hits + other.hits,
            misses=self.misses + other.misses,
            evictions=self.evictions + other.evictions,
            inserts=self.inserts + other.inserts,
            total_bytes=self.total_bytes + other.total_bytes,
        )

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            "hits": self.hits,
            "misses": self.misses,
            "hit_rate": self.hit_rate,
            "evictions": self.evictions,
            "inserts": self.inserts,
            "total_bytes": self.total_bytes,
            "total_mb": self.total_bytes / (1024.0 * 1024.0),
        }

    def reset(self) -> None:
        """重置统计"""
        self.hits = 0
        self.misses = 0
        self.evictions = 0
        self.inserts = 0
        self.total_bytes = 0


# ================================================================
# 缓存配置
# ================================================================

@dataclass
class CacheConfig:
    """
    缓存配置。

    Attributes:
        max_decode_frames: 最大解码帧缓存数
        max_render_plans: 最大渲染计划缓存数
        max_gpu_textures: 最大 GPU 纹理缓存数
        max_timeline_previews: 最大时间线预览缓存数
        max_audio_waveforms: 最大音频波形缓存数
        max_thumbnails: 最大缩略图缓存数
        max_memory_mb: 最大内存使用（MB）
        enable_warmup: 是否启用缓存预热
        warmup lookahead_frames: 预热前瞻帧数
    """
    max_decode_frames: int = 32
    max_render_plans: int = 16
    max_gpu_textures: int = 8
    max_timeline_previews: int = 16
    max_audio_waveforms: int = 50
    max_thumbnails: int = 1000
    max_memory_mb: float = 2048.0  # 2GB
    enable_warmup: bool = True
    warmup_lookahead_frames: int = 10
    warmup_batch_size: int = 5


# ================================================================
# LRU 缓存基类
# ================================================================

class LRUCache(Generic[T]):
    """
    线程安全的 LRU 缓存基类。

    Attributes:
        max_size: 最大缓存条目数
        max_bytes: 最大缓存字节数（0 表示仅按条目数限制，AUD-0313）
        on_evict: 条目离开缓存时的处置回调（AUD-0315）
        stats: 缓存统计
    """

    def __init__(
        self,
        max_size: int,
        tier: CacheTier,
        max_bytes: int = 0,
        on_evict: Optional[Callable[[T], None]] = None,
    ) -> None:
        """
        初始化 LRU 缓存。

        Args:
            max_size: 最大缓存条目数
            tier: 缓存层级
            max_bytes: 最大缓存字节数（字节容量上限；0 = 不限，
                仅按条目数淘汰）。put 时同时按条目数与字节数淘汰，
                但始终保留最新插入的条目。
            on_evict: 可选处置回调。任何因覆盖、容量淘汰、remove、
                invalidate_range 或 clear 离开缓存的值都会（且只会）
                调用一次该回调；回调应把值投递给资源属主线程释放
                （如渲染线程的 deferred-release 队列），不得在回调内
                长时间阻塞。
        """
        self._max_size: int = max_size
        self._tier: CacheTier = tier
        self._cache: OrderedDict[Any, T] = OrderedDict()
        self._lock: threading.Lock = threading.Lock()
        self._stats: CacheStatistics = CacheStatistics()
        # AUD-0314: 每条目的字节数与条目同生命周期维护（平行的 key→bytes
        # 映射，值仍原样存储，兼容 DecodeFrameCache 的直接遍历）。
        self._bytes: Dict[Any, int] = {}
        # AUD-0313: 字节容量上限（0 = 不限）。
        self._max_bytes: int = max(0, int(max_bytes))
        # AUD-0315: 条目离开缓存时的处置回调。
        self._on_evict: Optional[Callable[[T], None]] = on_evict

    @property
    def tier(self) -> CacheTier:
        """缓存层级"""
        return self._tier

    @property
    def max_size(self) -> int:
        """最大缓存大小"""
        return self._max_size

    @max_size.setter
    def max_size(self, value: int) -> None:
        """设置最大缓存大小"""
        evicted: List[T] = []
        with self._lock:
            self._max_size = value
            # 如果超出新大小，淘汰旧条目
            evicted = self._evict_locked()
        self._fire_on_evict(evicted)

    @property
    def max_bytes(self) -> int:
        """字节容量上限（0 = 不限）"""
        with self._lock:
            return self._max_bytes

    @max_bytes.setter
    def max_bytes(self, value: int) -> None:
        """设置字节容量上限并立即收敛"""
        evicted: List[T] = []
        with self._lock:
            self._max_bytes = max(0, int(value))
            evicted = self._evict_locked()
        self._fire_on_evict(evicted)

    def set_evict_hook(self, on_evict: Optional[Callable[[T], None]]) -> None:
        """设置/更换条目处置回调（AUD-0315）。"""
        with self._lock:
            self._on_evict = on_evict

    @property
    def size(self) -> int:
        """当前缓存大小"""
        with self._lock:
            return len(self._cache)

    @property
    def stats(self) -> CacheStatistics:
        """缓存统计（只读副本）"""
        with self._lock:
            return CacheStatistics(
                hits=self._stats.hits,
                misses=self._stats.misses,
                evictions=self._stats.evictions,
                inserts=self._stats.inserts,
                total_bytes=self._stats.total_bytes,
            )

    def get(self, key: Any) -> Optional[T]:
        """
        获取缓存条目。

        Args:
            key: 缓存键

        Returns:
            缓存值或 None
        """
        with self._lock:
            if key in self._cache:
                self._cache.move_to_end(key)
                self._stats.hits += 1
                return self._cache[key]
            self._stats.misses += 1
            return None

    def put(self, key: Any, value: T, estimated_bytes: int = 0) -> None:
        """
        添加缓存条目。

        Args:
            key: 缓存键
            value: 缓存值
            estimated_bytes: 估算字节数
        """
        evicted: List[T] = []
        with self._lock:
            # AUD-0314: 覆盖同 key 时按旧条目的字节数扣减（此前旧值不
            # 减，total_bytes 单调虚增）。
            if key in self._cache:
                old_value = self._pop_entry_locked(key)
                if old_value is not value:
                    evicted.append(old_value)

            # 添加新的
            self._cache[key] = value
            self._bytes[key] = int(estimated_bytes)
            self._stats.inserts += 1
            self._stats.total_bytes += int(estimated_bytes)

            # AUD-0313/0314: 条目数与字节上限双维淘汰；被淘汰条目按其
            # 自身字节数扣减（此前减的是刚插入条目的字节数）。至少保留
            # 刚插入的条目——单条即超限时不做自我淘汰。
            evicted.extend(self._evict_locked())
        self._fire_on_evict(evicted)

    def contains(self, key: Any) -> bool:
        """
        检查键是否存在。

        Args:
            key: 缓存键

        Returns:
            存在返回 True
        """
        with self._lock:
            return key in self._cache

    def remove(self, key: Any) -> bool:
        """
        移除缓存条目。

        Args:
            key: 缓存键

        Returns:
            成功移除返回 True
        """
        evicted: List[T] = []
        with self._lock:
            if key in self._cache:
                # AUD-0314: remove 也必须扣减对应条目的字节（此前不减）。
                evicted.append(self._pop_entry_locked(key))
                removed = True
            else:
                removed = False
        self._fire_on_evict(evicted)
        return removed

    def clear(self) -> int:
        """
        清空缓存。

        Returns:
            清空的条目数
        """
        evicted: List[T] = []
        with self._lock:
            count = len(self._cache)
            evicted = list(self._cache.values())
            self._cache.clear()
            self._bytes.clear()
            self._stats.total_bytes = 0
        self._fire_on_evict(evicted)
        return count

    def invalidate_range(
        self,
        key_filter: Callable[[Any], bool]
    ) -> int:
        """
        批量使缓存条目失效。

        Args:
            key_filter: 键过滤器函数，返回 True 的键将被移除

        Returns:
            移除的条目数
        """
        evicted: List[T] = []
        with self._lock:
            keys_to_remove = [k for k in self._cache.keys() if key_filter(k)]
            for key in keys_to_remove:
                # AUD-0314: 批量失效同样按条目自身字节扣减（此前不减）。
                evicted.append(self._pop_entry_locked(key))
        self._fire_on_evict(evicted)
        return len(keys_to_remove)

    def get_all_keys(self) -> List[Any]:
        """
        获取所有缓存键。

        Returns:
            键列表
        """
        with self._lock:
            return list(self._cache.keys())

    def reset_stats(self) -> None:
        """重置统计"""
        with self._lock:
            self._stats.reset()

    # ================================================================
    # 内部：条目生命周期（调用方必须已持有 self._lock）
    # ================================================================

    def _pop_entry_locked(self, key: Any) -> T:
        """弹出一个条目并按其自身字节数扣减统计（AUD-0314）。"""
        value = self._cache.pop(key)
        entry_bytes = self._bytes.pop(key, 0)
        self._stats.total_bytes = max(0, self._stats.total_bytes - entry_bytes)
        return value

    def _evict_locked(self) -> List[T]:
        """按条目数与字节上限淘汰最旧条目，返回被淘汰的值列表。

        始终保留最新插入的条目（len == 1 时不再淘汰），因此单条超过
        上限的 put 仍会成功——上限约束的是缓存的稳态占用，而非单条
        可缓存性。
        """
        evicted: List[T] = []
        while len(self._cache) > 1 and (
            len(self._cache) > self._max_size
            or (
                self._max_bytes > 0
                and self._stats.total_bytes > self._max_bytes
            )
        ):
            oldest_key, oldest_value = self._cache.popitem(last=False)
            entry_bytes = self._bytes.pop(oldest_key, 0)
            self._stats.total_bytes = max(
                0, self._stats.total_bytes - entry_bytes
            )
            self._stats.evictions += 1
            evicted.append(oldest_value)
            logger.debug(
                f"Cache evicted {self._tier.name}: {oldest_key}"
            )
        return evicted

    def _fire_on_evict(self, values: List[T]) -> None:
        """在锁外把离开缓存的值交给处置回调（AUD-0315）。

        回调预期是"投递到资源属主线程释放队列"一类的轻量操作；单个
        回调失败只记日志，绝不能影响缓存自身状态。
        """
        with self._lock:
            hook = self._on_evict
        if hook is None or not values:
            return
        for value in values:
            try:
                hook(value)
            except Exception as e:
                logger.error(
                    f"on_evict callback failed for {self._tier.name}: {e}"
                )


# ================================================================
# 解码帧缓存
# ================================================================

class DecodeFrameCache:
    """
    解码帧缓存。

    缓存解码后的视频帧，支持 proxy/master 分离。

    Attributes:
        proxy_cache: Proxy 帧缓存
        master_cache: Master 帧缓存
    """

    def __init__(self, max_proxy: int = 32, max_master: int = 8) -> None:
        """
        初始化解码帧缓存。

        Args:
            max_proxy: Proxy 帧最大缓存数
            max_master: Master 帧最大缓存数
        """
        self.proxy_cache: LRUCache[np.ndarray] = LRUCache(
            max_proxy, CacheTier.DECODE_FRAME
        )
        self.master_cache: LRUCache[np.ndarray] = LRUCache(
            max_master, CacheTier.DECODE_FRAME
        )

    def get(
        self,
        clip_id: str,
        frame_index: int,
        quality: str = "proxy",
        width: int = 0,
        height: int = 0,
        color_space: str = "RGB",
    ) -> Optional[np.ndarray]:
        """
        获取缓存帧。

        如果 width/height 为 0，则搜索所有尺寸匹配该 clip_id + frame_index + quality 的帧。

        Args:
            clip_id: 剪辑 ID
            frame_index: 帧号
            quality: 质量级别
            width: 宽度（0 表示任意）
            height: 高度（0 表示任意）
            color_space: 色彩空间

        Returns:
            帧数据或 None
        """
        cache = self.proxy_cache if quality == "proxy" else self.master_cache

        # 如果指定了完整尺寸，直接查找
        if width > 0 and height > 0:
            key = FrameCacheKey(
                clip_id=clip_id,
                frame_index=frame_index,
                quality=quality,
                width=width,
                height=height,
                color_space=color_space,
            )
            return cache.get(key)

        # 否则，搜索所有匹配的键（不指定尺寸）
        with cache._lock:
            for k, v in cache._cache.items():
                if (isinstance(k, FrameCacheKey) and
                    k.clip_id == clip_id and
                    k.frame_index == frame_index and
                    k.quality == quality):
                    cache._cache.move_to_end(k)
                    cache._stats.hits += 1
                    return v
            cache._stats.misses += 1
            return None

    def put(
        self,
        clip_id: str,
        frame_index: int,
        quality: str,
        frame_data: np.ndarray,
        width: int = 0,
        height: int = 0,
        color_space: str = "RGB",
    ) -> None:
        """
        缓存帧。

        Args:
            clip_id: 剪辑 ID
            frame_index: 帧号
            quality: 质量级别
            frame_data: 帧数据
            width: 宽度
            height: 高度
            color_space: 色彩空间
        """
        if width == 0:
            width = frame_data.shape[1] if frame_data.ndim >= 2 else 0
        if height == 0:
            height = frame_data.shape[0] if frame_data.ndim >= 2 else 0

        key = FrameCacheKey(
            clip_id=clip_id,
            frame_index=frame_index,
            quality=quality,
            width=width,
            height=height,
            color_space=color_space,
        )

        # 估算字节数
        estimated_bytes = frame_data.nbytes

        cache = self.proxy_cache if quality == "proxy" else self.master_cache
        cache.put(key, frame_data, estimated_bytes)

    def get_stats(self) -> Dict[str, Any]:
        """
        获取统计信息。

        Returns:
            统计字典
        """
        proxy_stats = self.proxy_cache.stats
        master_stats = self.master_cache.stats
        combined = proxy_stats.merge(master_stats)

        return {
            "tier": "DECODE_FRAME",
            "proxy": proxy_stats.to_dict(),
            "master": master_stats.to_dict(),
            "combined": combined.to_dict(),
            "proxy_size": self.proxy_cache.size,
            "master_size": self.master_cache.size,
        }

    def clear(self, quality: Optional[str] = None) -> int:
        """
        清空缓存。

        Args:
            quality: 质量级别，None 表示清空所有

        Returns:
            清空的帧数
        """
        if quality == "proxy":
            return self.proxy_cache.clear()
        elif quality == "master":
            return self.master_cache.clear()
        else:
            proxy_count = self.proxy_cache.clear()
            master_count = self.master_cache.clear()
            return proxy_count + master_count


# ================================================================
# 渲染计划缓存
# ================================================================

class RenderPlanCache:
    """
    渲染计划缓存。

    缓存渲染计划，避免重复计算。
    """

    def __init__(self, max_size: int = 16) -> None:
        """
        初始化渲染计划缓存。

        Args:
            max_size: 最大缓存数
        """
        self._cache: LRUCache[Any] = LRUCache(max_size, CacheTier.RENDER_PLAN)

    def get(self, frame_index: int, viewport_w: int = 1920, viewport_h: int = 1080) -> Optional[Any]:
        """
        获取缓存的渲染计划。

        Args:
            frame_index: 帧号
            viewport_w: 视口宽度
            viewport_h: 视口高度

        Returns:
            渲染计划或 None
        """
        key = RenderPlanCacheKey(
            frame_index=frame_index,
            viewport_width=viewport_w,
            viewport_height=viewport_h,
        )
        return self._cache.get(key)

    def put(self, frame_index: int, render_plan: Any, viewport_w: int = 1920, viewport_h: int = 1080) -> None:
        """
        缓存渲染计划。

        Args:
            frame_index: 帧号
            render_plan: 渲染计划
            viewport_w: 视口宽度
            viewport_h: 视口高度
        """
        key = RenderPlanCacheKey(
            frame_index=frame_index,
            viewport_width=viewport_w,
            viewport_height=viewport_h,
        )
        # 估算大小（简化）
        estimated_bytes = 1024  # 假设每个计划约 1KB
        self._cache.put(key, render_plan, estimated_bytes)

    def get_stats(self) -> Dict[str, Any]:
        """获取统计信息"""
        stats = self._cache.stats
        return {
            "tier": "RENDER_PLAN",
            "stats": stats.to_dict(),
            "size": self._cache.size,
        }

    def clear(self) -> int:
        """清空缓存"""
        return self._cache.clear()


# ================================================================
# GPU 纹理缓存
# ================================================================

class GPUTextureCache:
    """
    GPU 纹理缓存。

    缓存 GPU 纹理对象，避免重复创建。

    AUD-0315：纹理对象携带 GL 资源，被淘汰/清空时绝不能只在任意调用
    线程丢掉 Python 引用（析构线程不受控，违背项目 GPU deferred-release
    架构）。通过 ``release_hook`` 注入"投递到渲染线程释放队列"的回调；
    未注入钩子时行为与旧版一致（仅记录淘汰），接线方必须先
    ``set_release_hook`` 再启用缓存。
    """

    def __init__(
        self,
        max_size: int = 8,
        max_bytes: int = 0,
        release_hook: Optional[Callable[[Any], None]] = None,
    ) -> None:
        """
        初始化 GPU 纹理缓存。

        Args:
            max_size: 最大缓存数
            max_bytes: 字节容量上限（0 = 不限）
            release_hook: 条目离开缓存时的处置回调（应把纹理投递到
                渲染线程的 deferred-release 队列，而非就地 release）
        """
        self._cache: LRUCache[Any] = LRUCache(
            max_size,
            CacheTier.GPU_TEXTURE,
            max_bytes=max_bytes,
            on_evict=release_hook,
        )

    def set_release_hook(self, hook: Optional[Callable[[Any], None]]) -> None:
        """注入/更换 deferred-release 投递回调（AUD-0315）。"""
        self._cache.set_evict_hook(hook)

    def get(self, width: int, height: int, components: int = 4, dtype: str = "f2", usage: str = "frame") -> Optional[Any]:
        """
        获取缓存的纹理。

        Args:
            width: 宽度
            height: 高度
            components: 通道数
            dtype: 数据类型
            usage: 用途

        Returns:
            纹理对象或 None
        """
        key = TextureCacheKey(
            width=width,
            height=height,
            components=components,
            dtype=dtype,
            usage=usage,
        )
        return self._cache.get(key)

    def put(self, width: int, height: int, texture: Any, components: int = 4, dtype: str = "f2", usage: str = "frame") -> None:
        """
        缓存纹理。

        Args:
            width: 宽度
            height: 高度
            texture: 纹理对象
            components: 通道数
            dtype: 数据类型
            usage: 用途
        """
        key = TextureCacheKey(
            width=width,
            height=height,
            components=components,
            dtype=dtype,
            usage=usage,
        )
        # 估算字节数
        estimated_bytes = width * height * components * 2  # float16
        self._cache.put(key, texture, estimated_bytes)

    def get_stats(self) -> Dict[str, Any]:
        """获取统计信息"""
        stats = self._cache.stats
        return {
            "tier": "GPU_TEXTURE",
            "stats": stats.to_dict(),
            "size": self._cache.size,
        }

    def clear(self) -> int:
        """清空缓存"""
        return self._cache.clear()


# ================================================================
# 时间线预览缓存
# ================================================================

class TimelinePreviewCache:
    """
    时间线预览缓存。

    缓存已渲染的预览帧。
    """

    def __init__(self, max_size: int = 16) -> None:
        """
        初始化时间线预览缓存。

        Args:
            max_size: 最大缓存数
        """
        self._cache: LRUCache[Any] = LRUCache(max_size, CacheTier.TIMELINE_PREVIEW)

    def get(self, frame_index: int, viewport_w: int = 1920, viewport_h: int = 1080, composite_mode: str = "default") -> Optional[Any]:
        """
        获取缓存的预览帧。

        Args:
            frame_index: 帧号
            viewport_w: 视口宽度
            viewport_h: 视口高度
            composite_mode: 合成模式

        Returns:
            预览帧数据或 None
        """
        key = PreviewCacheKey(
            frame_index=frame_index,
            viewport_width=viewport_w,
            viewport_height=viewport_h,
            composite_mode=composite_mode,
        )
        return self._cache.get(key)

    def put(self, frame_index: int, preview_data: Any, viewport_w: int = 1920, viewport_h: int = 1080, composite_mode: str = "default") -> None:
        """
        缓存预览帧。

        Args:
            frame_index: 帧号
            preview_data: 预览数据
            viewport_w: 视口宽度
            viewport_h: 视口高度
            composite_mode: 合成模式
        """
        key = PreviewCacheKey(
            frame_index=frame_index,
            viewport_width=viewport_w,
            viewport_height=viewport_h,
            composite_mode=composite_mode,
        )
        # 估算字节数（假设 1080p RGB）
        estimated_bytes = viewport_w * viewport_h * 3
        self._cache.put(key, preview_data, estimated_bytes)

    def get_stats(self) -> Dict[str, Any]:
        """获取统计信息"""
        stats = self._cache.stats
        return {
            "tier": "TIMELINE_PREVIEW",
            "stats": stats.to_dict(),
            "size": self._cache.size,
        }

    def clear(self) -> int:
        """清空缓存"""
        return self._cache.clear()


# ================================================================
# 音频波形缓存
# ================================================================

class AudioWaveformCache:
    """
    音频波形缓存。

    缓存音频波形数据。
    """

    def __init__(self, max_size: int = 50) -> None:
        """
        初始化音频波形缓存。

        Args:
            max_size: 最大缓存数
        """
        self._cache: LRUCache[Any] = LRUCache(max_size, CacheTier.AUDIO_WAVEFORM)

    def get(self, asset_id: str, sample_rate: int = 44100, num_channels: int = 1) -> Optional[Any]:
        """
        获取缓存的波形数据。

        Args:
            asset_id: 素材 ID
            sample_rate: 采样率
            num_channels: 通道数

        Returns:
            波形数据或 None
        """
        key = WaveformCacheKey(
            asset_id=asset_id,
            sample_rate=sample_rate,
            num_channels=num_channels,
        )
        return self._cache.get(key)

    def put(self, asset_id: str, waveform_data: Any, sample_rate: int = 44100, num_channels: int = 1) -> None:
        """
        缓存波形数据。

        Args:
            asset_id: 素材 ID
            waveform_data: 波形数据
            sample_rate: 采样率
            num_channels: 通道数
        """
        key = WaveformCacheKey(
            asset_id=asset_id,
            sample_rate=sample_rate,
            num_channels=num_channels,
        )
        # 估算字节数（假设 10000 个采样点）
        estimated_bytes = 10000 * 8  # float64
        self._cache.put(key, waveform_data, estimated_bytes)

    def get_stats(self) -> Dict[str, Any]:
        """获取统计信息"""
        stats = self._cache.stats
        return {
            "tier": "AUDIO_WAVEFORM",
            "stats": stats.to_dict(),
            "size": self._cache.size,
        }

    def clear(self) -> int:
        """清空缓存"""
        return self._cache.clear()


# ================================================================
# 缩略图缓存
# ================================================================

class ThumbnailCache:
    """
    缩略图缓存。

    缓存图片和视频缩略图。
    """

    def __init__(self, max_size: int = 1000) -> None:
        """
        初始化缩略图缓存。

        Args:
            max_size: 最大缓存数
        """
        self._cache: LRUCache[Any] = LRUCache(max_size, CacheTier.THUMBNAIL)

    def get(self, asset_path: str, size: int = 128, fmt: str = "webp") -> Optional[Any]:
        """
        获取缓存的缩略图。

        Args:
            asset_path: 素材路径
            size: 尺寸
            fmt: 格式

        Returns:
            缩略图或 None
        """
        key = ThumbnailCacheKey(
            asset_path=asset_path,
            size=size,
            format=fmt,
        )
        return self._cache.get(key)

    def put(self, asset_path: str, thumbnail_data: Any, size: int = 128, fmt: str = "webp") -> None:
        """
        缓存缩略图。

        Args:
            asset_path: 素材路径
            thumbnail_data: 缩略图数据
            size: 尺寸
            fmt: 格式
        """
        key = ThumbnailCacheKey(
            asset_path=asset_path,
            size=size,
            format=fmt,
        )
        # 估算字节数（假设 128x128 RGB）
        estimated_bytes = size * size * 3
        self._cache.put(key, thumbnail_data, estimated_bytes)

    def get_stats(self) -> Dict[str, Any]:
        """获取统计信息"""
        stats = self._cache.stats
        return {
            "tier": "THUMBNAIL",
            "stats": stats.to_dict(),
            "size": self._cache.size,
        }

    def clear(self) -> int:
        """清空缓存"""
        return self._cache.clear()


# ================================================================
# 缓存管理器
# ================================================================

class CacheManager:
    """
    统一缓存管理器。

    协调所有缓存层级，提供统一接口和统计。

    Attributes:
        decode_cache: 解码帧缓存
        render_plan_cache: 渲染计划缓存
        gpu_texture_cache: GPU 纹理缓存
        timeline_preview_cache: 时间线预览缓存
        audio_waveform_cache: 音频波形缓存
        thumbnail_cache: 缩略图缓存
    """

    def __init__(self, config: Optional[CacheConfig] = None) -> None:
        """
        初始化缓存管理器。

        Args:
            config: 缓存配置
        """
        self._config: CacheConfig = config or CacheConfig()

        # 初始化各级缓存
        self.decode_cache = DecodeFrameCache(
            max_proxy=self._config.max_decode_frames,
            max_master=self._config.max_decode_frames // 4,
        )
        self.render_plan_cache = RenderPlanCache(
            max_size=self._config.max_render_plans
        )
        self.gpu_texture_cache = GPUTextureCache(
            max_size=self._config.max_gpu_textures
        )
        self.timeline_preview_cache = TimelinePreviewCache(
            max_size=self._config.max_timeline_previews
        )
        self.audio_waveform_cache = AudioWaveformCache(
            max_size=self._config.max_audio_waveforms
        )
        self.thumbnail_cache = ThumbnailCache(
            max_size=self._config.max_thumbnails
        )

        # AUD-0313: 把 CacheConfig.max_memory_mb 落地为各字节承载层的
        # 字节容量上限（此前该配置只有定义、从未被读取，缓存内存上限
        # 实际只由"条目数 × 单帧大小"决定）。权重按各层重建成本与典型
        # 单条体积分配；render_plan / waveform 条目小，仍按条目数约束。
        self._apply_byte_budgets()

        # 线程锁
        self._lock: threading.Lock = threading.Lock()

        # 内存压力回调
        self._memory_callbacks: List[Callable[[str], None]] = []

        logger.info(
            f"CacheManager initialized: "
            f"decode={self._config.max_decode_frames}, "
            f"render_plan={self._config.max_render_plans}, "
            f"gpu_texture={self._config.max_gpu_textures}, "
            f"preview={self._config.max_timeline_previews}, "
            f"waveform={self._config.max_audio_waveforms}, "
            f"thumbnail={self._config.max_thumbnails}"
        )

    @property
    def config(self) -> CacheConfig:
        """缓存配置"""
        return self._config

    # 字节预算权重（AUD-0313）：decode 帧最贵也最难重建，给大头；
    # render_plan / waveform 条目小，仅按条目数约束（权重 0）。
    _BYTE_BUDGET_WEIGHTS: Dict[str, float] = {
        "proxy": 0.45,
        "master": 0.15,
        "gpu_texture": 0.20,
        "timeline_preview": 0.15,
        "thumbnail": 0.05,
    }

    def _apply_byte_budgets(self) -> None:
        """按 ``max_memory_mb`` 收敛各字节承载层的容量上限（AUD-0313）。"""
        total_bytes = int(
            max(0.0, float(self._config.max_memory_mb)) * 1024 * 1024
        )
        w = self._BYTE_BUDGET_WEIGHTS
        self.decode_cache.proxy_cache.max_bytes = int(total_bytes * w["proxy"])
        self.decode_cache.master_cache.max_bytes = int(total_bytes * w["master"])
        self.gpu_texture_cache._cache.max_bytes = int(
            total_bytes * w["gpu_texture"]
        )
        self.timeline_preview_cache._cache.max_bytes = int(
            total_bytes * w["timeline_preview"]
        )
        self.thumbnail_cache._cache.max_bytes = int(
            total_bytes * w["thumbnail"]
        )

    # ================================================================
    # 统计接口
    # ================================================================

    def get_stats(self) -> Dict[str, Any]:
        """
        获取所有缓存的统计信息。

        Returns:
            统计字典
        """
        decode_stats = self.decode_cache.get_stats()
        render_stats = self.render_plan_cache.get_stats()
        gpu_stats = self.gpu_texture_cache.get_stats()
        preview_stats = self.timeline_preview_cache.get_stats()
        audio_stats = self.audio_waveform_cache.get_stats()
        thumb_stats = self.thumbnail_cache.get_stats()

        # 计算总计
        total_hits = (
            decode_stats["combined"]["hits"] +
            render_stats["stats"]["hits"] +
            gpu_stats["stats"]["hits"] +
            preview_stats["stats"]["hits"] +
            audio_stats["stats"]["hits"] +
            thumb_stats["stats"]["hits"]
        )
        total_misses = (
            decode_stats["combined"]["misses"] +
            render_stats["stats"]["misses"] +
            gpu_stats["stats"]["misses"] +
            preview_stats["stats"]["misses"] +
            audio_stats["stats"]["misses"] +
            thumb_stats["stats"]["misses"]
        )
        total_evictions = (
            decode_stats["combined"]["evictions"] +
            render_stats["stats"]["evictions"] +
            gpu_stats["stats"]["evictions"] +
            preview_stats["stats"]["evictions"] +
            audio_stats["stats"]["evictions"] +
            thumb_stats["stats"]["evictions"]
        )
        total_bytes = (
            decode_stats["combined"]["total_bytes"] +
            render_stats["stats"]["total_bytes"] +
            gpu_stats["stats"]["total_bytes"] +
            preview_stats["stats"]["total_bytes"] +
            audio_stats["stats"]["total_bytes"] +
            thumb_stats["stats"]["total_bytes"]
        )

        total_requests = total_hits + total_misses

        return {
            "decode_frame": decode_stats,
            "render_plan": render_stats,
            "gpu_texture": gpu_stats,
            "timeline_preview": preview_stats,
            "audio_waveform": audio_stats,
            "thumbnail": thumb_stats,
            "total": {
                "hits": total_hits,
                "misses": total_misses,
                "hit_rate": total_hits / total_requests if total_requests > 0 else 0.0,
                "evictions": total_evictions,
                "total_bytes": total_bytes,
                "total_mb": total_bytes / (1024.0 * 1024.0),
            },
        }

    def reset_stats(self) -> None:
        """重置所有统计"""
        self.decode_cache.proxy_cache.reset_stats()
        self.decode_cache.master_cache.reset_stats()
        self.render_plan_cache._cache.reset_stats()
        self.gpu_texture_cache._cache.reset_stats()
        self.timeline_preview_cache._cache.reset_stats()
        self.audio_waveform_cache._cache.reset_stats()
        self.thumbnail_cache._cache.reset_stats()

    # ================================================================
    # 清理接口
    # ================================================================

    def clear_all(self) -> Dict[str, int]:
        """
        清空所有缓存。

        Returns:
            各层级清空的条目数
        """
        return {
            "decode_frame": self.decode_cache.clear(),
            "render_plan": self.render_plan_cache.clear(),
            "gpu_texture": self.gpu_texture_cache.clear(),
            "timeline_preview": self.timeline_preview_cache.clear(),
            "audio_waveform": self.audio_waveform_cache.clear(),
            "thumbnail": self.thumbnail_cache.clear(),
        }

    def clear_tier(self, tier: CacheTier) -> int:
        """
        清空指定层级的缓存。

        Args:
            tier: 缓存层级

        Returns:
            清空的条目数
        """
        if tier == CacheTier.DECODE_FRAME:
            return self.decode_cache.clear()
        elif tier == CacheTier.RENDER_PLAN:
            return self.render_plan_cache.clear()
        elif tier == CacheTier.GPU_TEXTURE:
            return self.gpu_texture_cache.clear()
        elif tier == CacheTier.TIMELINE_PREVIEW:
            return self.timeline_preview_cache.clear()
        elif tier == CacheTier.AUDIO_WAVEFORM:
            return self.audio_waveform_cache.clear()
        elif tier == CacheTier.THUMBNAIL:
            return self.thumbnail_cache.clear()
        else:
            return 0

    # ================================================================
    # 内存压力响应
    # ================================================================

    def on_memory_pressure(self, level: str = "medium") -> None:
        """
        响应内存压力事件。

        根据压力级别清理不同层级的缓存。

        Args:
            level: 压力级别 ('low', 'medium', 'high', 'critical')
        """
        logger.warning(f"Memory pressure event: {level}")

        if level == "low":
            # 低压力：清理缩略图
            self.clear_tier(CacheTier.THUMBNAIL)
            logger.info("Cleared thumbnail cache (low pressure)")

        elif level == "medium":
            # 中压力：清理缩略图 + 音频波形
            self.clear_tier(CacheTier.THUMBNAIL)
            self.clear_tier(CacheTier.AUDIO_WAVEFORM)
            logger.info("Cleared thumbnail + waveform cache (medium pressure)")

        elif level == "high":
            # 高压力：清理缩略图 + 音频波形 + 预览
            self.clear_tier(CacheTier.THUMBNAIL)
            self.clear_tier(CacheTier.AUDIO_WAVEFORM)
            self.clear_tier(CacheTier.TIMELINE_PREVIEW)
            logger.info("Cleared thumbnail + waveform + preview cache (high pressure)")

        elif level == "critical":
            # 严重压力：清理所有非关键缓存
            self.clear_tier(CacheTier.THUMBNAIL)
            self.clear_tier(CacheTier.AUDIO_WAVEFORM)
            self.clear_tier(CacheTier.TIMELINE_PREVIEW)
            self.clear_tier(CacheTier.RENDER_PLAN)
            logger.info("Cleared non-critical caches (critical pressure)")

        # 触发回调
        for callback in self._memory_callbacks:
            try:
                callback(level)
            except Exception as e:
                logger.error(f"Memory callback error: {e}")

    def register_memory_callback(self, callback: Callable[[str], None]) -> None:
        """
        注册内存压力回调。

        Args:
            callback: 回调函数，接收压力级别参数
        """
        self._memory_callbacks.append(callback)

    # ================================================================
    # 配置更新
    # ================================================================

    def update_config(self, config: CacheConfig) -> None:
        """
        更新缓存配置。

        Args:
            config: 新配置
        """
        self._config = config

        # 更新各级缓存大小
        self.decode_cache.proxy_cache.max_size = config.max_decode_frames
        self.decode_cache.master_cache.max_size = config.max_decode_frames // 4
        self.render_plan_cache._cache.max_size = config.max_render_plans
        self.gpu_texture_cache._cache.max_size = config.max_gpu_textures
        self.timeline_preview_cache._cache.max_size = config.max_timeline_previews
        self.audio_waveform_cache._cache.max_size = config.max_audio_waveforms
        self.thumbnail_cache._cache.max_size = config.max_thumbnails

        # AUD-0313: max_memory_mb 变更同样生效。
        self._apply_byte_budgets()

        logger.info(f"CacheManager config updated: {config}")


# ================================================================
# 缓存预热
# ================================================================

class CacheWarmer:
    """
    缓存预热策略。

    在后台预解码和预渲染帧，提升播放流畅度。

    Attributes:
        cache_manager: 缓存管理器
        is_running: 是否正在运行
    """

    def __init__(self, cache_manager: CacheManager) -> None:
        """
        初始化缓存预热。

        Args:
            cache_manager: 缓存管理器
        """
        self._cache_manager: CacheManager = cache_manager
        self._is_running: bool = False
        # AUD-0310: 可重入锁。start_warmup 在锁内做"停止旧预热"的记账
        # 翻转（旧版直接调 stop_warmup 对同一把不可重入锁二次加锁，必然
        # 死锁）。
        self._lock: threading.RLock = threading.RLock()
        self._warmup_thread: Optional[threading.Thread] = None
        self._stop_event: threading.Event = threading.Event()

    @property
    def is_running(self) -> bool:
        """是否正在运行"""
        with self._lock:
            return self._is_running

    def start_warmup(
        self,
        clip_id: str,
        start_frame: int,
        quality: str = "proxy",
        lookahead: Optional[int] = None,
        decode_callback: Optional[Callable[[str, int, str], Any]] = None,
    ) -> None:
        """
        启动缓存预热。

        Args:
            clip_id: 剪辑 ID
            start_frame: 起始帧号
            quality: 质量级别
            lookahead: 前瞻帧数（默认使用配置值）
            decode_callback: 解码回调函数，返回帧数据
        """
        if not self._cache_manager.config.enable_warmup:
            logger.debug("Cache warmup disabled")
            return

        lookahead = lookahead or self._cache_manager.config.warmup_lookahead_frames
        batch_size = self._cache_manager.config.warmup_batch_size

        with self._lock:
            old_thread: Optional[threading.Thread] = None
            if self._is_running:
                logger.debug("Warmup already running, stopping old warmup")
                # AUD-0310: 锁内只做记账翻转，绝不调用 stop_warmup()——
                # 它会再次加锁（旧版对不可重入锁二次 acquire，调用线程
                # 永久挂起，UI/播放引擎随之冻结）。旧线程的 join 放到锁外。
                self._stop_event.set()
                self._is_running = False
                old_thread = self._warmup_thread

            self._is_running = True
            # 每次运行独立的 stop event：旧循环持有已置位的事件，即使
            # 本次运行已清除（新）事件，旧循环也能及时退出，不会复活。
            stop_event = threading.Event()
            self._stop_event = stop_event
            # AUD-0310: 线程引用在锁内发布/读取，并发 start 不再观察到
            # 半发布的 _warmup_thread。
            self._warmup_thread = threading.Thread(
                target=self._warmup_loop,
                args=(clip_id, start_frame, quality, lookahead, batch_size,
                      decode_callback, stop_event),
                daemon=True,
                name="CacheWarmer",
            )
            new_thread = self._warmup_thread

        # 锁外有界 join 旧线程（至多 2 秒），不阻塞其他 stop/start。
        if old_thread is not None and old_thread.is_alive():
            old_thread.join(timeout=2.0)
        new_thread.start()

        logger.info(
            f"Cache warmup started: clip={clip_id[:8]}..., "
            f"start={start_frame}, lookahead={lookahead}"
        )

    def stop_warmup(self) -> None:
        """停止缓存预热"""
        with self._lock:
            self._stop_event.set()
            self._is_running = False
            thread = self._warmup_thread
            self._warmup_thread = None

        # 锁外 join：预热循环收尾需要拿 _lock 清 _is_running，锁内 join
        # 会把停等时间拉满 join 超时。
        if thread and thread.is_alive():
            thread.join(timeout=2.0)

        logger.info("Cache warmup stopped")

    def _warmup_loop(
        self,
        clip_id: str,
        start_frame: int,
        quality: str,
        lookahead: int,
        batch_size: int,
        decode_callback: Optional[Callable[[str, int, str], Any]],
        stop_event: Optional[threading.Event] = None,
    ) -> None:
        """
        预热循环（后台线程）。

        Args:
            clip_id: 剪辑 ID
            start_frame: 起始帧号
            quality: 质量级别
            lookahead: 前瞻帧数
            batch_size: 批次大小
            decode_callback: 解码回调
            stop_event: 本次运行的停止事件（缺省用 self._stop_event）
        """
        if stop_event is None:
            stop_event = self._stop_event
        current_frame = start_frame + 1
        end_frame = start_frame + lookahead

        while not stop_event.is_set() and current_frame <= end_frame:
            try:
                # 检查是否已缓存
                if self._cache_manager.decode_cache.get(clip_id, current_frame, quality):
                    current_frame += 1
                    continue

                # 解码帧
                if decode_callback:
                    frame_data = decode_callback(clip_id, current_frame, quality)
                    if frame_data is not None:
                        # 缓存帧
                        self._cache_manager.decode_cache.put(
                            clip_id, current_frame, quality, frame_data
                        )

                current_frame += 1

                # 批次间短暂休眠，避免占用过多 CPU
                if current_frame % batch_size == 0:
                    stop_event.wait(0.01)

            except Exception as e:
                logger.error(f"Warmup error at frame {current_frame}: {e}")
                current_frame += 1

        with self._lock:
            # AUD-0310: 只有当前注册的预热线程才能翻转 _is_running——
            # 旧循环迟到收尾时不得把新预热标记为已停止。
            if self._warmup_thread is threading.current_thread():
                self._is_running = False

        logger.debug(f"Cache warmup completed: clip={clip_id[:8]}...")


# ================================================================
# 内存压力监控
# ================================================================

class MemoryPressureMonitor:
    """
    内存压力监控器。

    监控系统内存使用，在压力时触发缓存清理。

    Attributes:
        cache_manager: 缓存管理器
        is_running: 是否正在运行
    """

    def __init__(
        self,
        cache_manager: CacheManager,
        check_interval: float = 5.0,
        warning_threshold: float = 0.8,
        critical_threshold: float = 0.95,
    ) -> None:
        """
        初始化内存压力监控。

        Args:
            cache_manager: 缓存管理器
            check_interval: 检查间隔（秒）
            warning_threshold: 警告阈值（内存使用率）
            critical_threshold: 严重阈值（内存使用率）
        """
        self._cache_manager: CacheManager = cache_manager
        self._check_interval: float = check_interval
        self._warning_threshold: float = warning_threshold
        self._critical_threshold: float = critical_threshold
        self._is_running: bool = False
        # AUD-0319: start/stop 的 check-then-act 用锁保护，防止两个线程
        # 并发 start 各自通过 ``if self._is_running`` 检查、启动两条监控
        # 线程（双倍频率触发 on_memory_pressure）。stop 的记账翻转也在
        # 同一把锁内，保证 start/stop 交错时状态一致。
        self._state_lock: threading.Lock = threading.Lock()
        self._monitor_thread: Optional[threading.Thread] = None
        self._stop_event: threading.Event = threading.Event()

    @property
    def is_running(self) -> bool:
        """是否正在运行"""
        with self._state_lock:
            return self._is_running

    def start(self) -> None:
        """启动监控"""
        # AUD-0319: 锁内完成"检查 + 翻转 + 发布线程引用"，并发 start 只有
        # 一个能通过检查。线程创建/启动放在锁外，避免持锁做耗时操作。
        with self._state_lock:
            if self._is_running:
                return
            self._is_running = True
            self._stop_event.clear()
            self._monitor_thread = threading.Thread(
                target=self._monitor_loop,
                daemon=True,
                name="MemoryPressureMonitor",
            )
            monitor_thread = self._monitor_thread

        monitor_thread.start()

        logger.info(
            f"MemoryPressureMonitor started: "
            f"interval={self._check_interval}s, "
            f"warning={self._warning_threshold:.0%}, "
            f"critical={self._critical_threshold:.0%}"
        )

    def stop(self) -> None:
        """停止监控"""
        with self._state_lock:
            self._is_running = False
            self._stop_event.set()
            monitor_thread = self._monitor_thread

        if monitor_thread and monitor_thread.is_alive():
            monitor_thread.join(timeout=2.0)

        logger.info("MemoryPressureMonitor stopped")

    def _monitor_loop(self) -> None:
        """监控循环（后台线程）"""
        # AUD-0312: import 必须在受保护的路径上。旧版在 try 块之外、
        # 循环之前裸 import，psutil 缺失时监控线程直接崩溃，try 内的
        # ``except ImportError`` 优雅降级是永不可达的死分支。
        try:
            import psutil
        except ImportError:
            logger.warning("psutil not available, memory monitoring disabled")
            return

        while not self._stop_event.is_set():
            try:
                memory = psutil.virtual_memory()
                usage_ratio = memory.percent / 100.0

                if usage_ratio >= self._critical_threshold:
                    self._cache_manager.on_memory_pressure("critical")
                elif usage_ratio >= self._warning_threshold:
                    self._cache_manager.on_memory_pressure("high")
                elif usage_ratio >= self._warning_threshold * 0.8:
                    self._cache_manager.on_memory_pressure("medium")
                # AUD-0311: 内存使用正常时不触发任何压力事件。旧版在
                # "正常"分支里每 5 秒发一次 "low" 事件清空缩略图缓存，
                # 缩略图永远无法积累，媒体浏览器周期性重复生成缩略图。

            except Exception as e:
                logger.error(f"Memory monitor error: {e}")

            self._stop_event.wait(self._check_interval)


# ================================================================
# 全局缓存管理器实例
# ================================================================

_global_cache_manager: Optional[CacheManager] = None
_global_lock: threading.Lock = threading.Lock()


def get_cache_manager(config: Optional[CacheConfig] = None) -> CacheManager:
    """
    获取全局缓存管理器实例。

    Args:
        config: 缓存配置（首次调用时有效）

    Returns:
        CacheManager 实例
    """
    global _global_cache_manager

    with _global_lock:
        if _global_cache_manager is None:
            _global_cache_manager = CacheManager(config)
        return _global_cache_manager


def reset_cache_manager() -> None:
    """重置全局缓存管理器"""
    global _global_cache_manager

    with _global_lock:
        if _global_cache_manager is not None:
            _global_cache_manager.clear_all()
            _global_cache_manager = None
