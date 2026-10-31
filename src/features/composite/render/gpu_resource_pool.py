"""
GPU Resource Pool - Enhanced memory-managed texture pool for the composite pipeline.

Provides:
- Texture acquire/release with usage tracking (TEMP, CACHE, OUTPUT, TILE)
- Separate pools for cache-owned vs temp-scratch textures
- LRU eviction policy with memory budget enforcement
- Pin mechanism to protect textures in active use (e.g., display output)
- Diagnostic stats and dump capabilities
- Force cleanup for page leave / project close / memory pressure

Optimization features (v2):
- Dynamic VRAM awareness: query available VRAM via GL extensions, adjust budget
- Format tiering: Preview uses RGBA16F (f2), Export uses RGBA32F (f4)
- Cost-based eviction: evict by cost = size × age (larger + older = higher cost)
- Cross-frame slot reuse: video frame textures reuse same-size slots
- Enhanced statistics: VRAM usage, format distribution, slot reuse rate

Usage:
    pool = GPUResourcePool(fb_manager, max_memory_mb=1024)

    # Temp texture (short-lived, blend intermediate)
    tex = pool.acquire_temp_texture(1920, 1080)
    pool.release_texture(tex)

    # Cache texture (long-lived, owned by CacheEntry)
    tex = pool.acquire_cache_texture(1920, 1080)
    pool.release_texture(tex)  # Returns to cache pool

    # Output texture (display, must not be evicted)
    pool.pin_texture(output_tex.glo)  # Protect from eviction
    pool.unpin_texture(output_tex.glo)  # Allow eviction again

    # Format-tiered texture (v2)
    tex = pool.acquire_tiered_texture(1920, 1080, render_mode="preview")  # RGBA16F
    tex = pool.acquire_tiered_texture(1920, 1080, render_mode="export")   # RGBA32F

    # Video slot reuse (v2)
    pool.begin_video_frame()
    slot_tex = pool.acquire_video_slot(1920, 1080, slot_id=0)
    pool.end_video_frame()  # Slots remain pooled for next frame
"""

from collections import OrderedDict
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Dict, List, Set, Optional, Tuple, Iterator, Callable
import ctypes
import logging
import threading
import time

logger = logging.getLogger(__name__)

from src.shared.gpu.gl_backend import FramebufferManager


# ═══════════════════════════════════════════════════════════════════════
# VRAM Awareness - Dynamic GPU memory monitoring
# ═══════════════════════════════════════════════════════════════════════

class VRAMMonitor:
    """Dynamic VRAM monitoring via GL extensions.

    Supported extensions:
    - GL_NVX_gpu_memory_info (NVIDIA): total/available VRAM
    - GL_ATI_meminfo (AMD): available VRAM per pool
    - GLX_NVX_gpu_memory_info (Linux NVIDIA via GLX)

    If no extension available, falls back to user-configured budget.
    """

    # GL constants for NVIDIA VRAM query
    GL_GPU_MEMORY_INFO_TOTAL_AVAILABLE_MEMORY_NVX = 0x9048
    GL_GPU_MEMORY_INFO_CURRENT_AVAILABLE_VIDMEM_NVX = 0x9049

    # GL constants for AMD VRAM query
    GL_TEXTURE_FREE_MEMORY_ATI = 0x87FC

    def __init__(self) -> None:
        self._available_extensions: Optional[Set[str]] = None
        self._vram_total_kb: int = 0
        self._vram_available_kb: int = 0
        self._last_query_time: float = 0
        self._query_interval_sec: float = 1.0  # Query every second
        self._supported: bool = False
        self._vendor: str = ""  # "nvidia", "amd", "unknown"
        # AUD-8313: thread that owns the GL context (set by initialize()).
        # Raw glGetIntegerv calls are only legal on that thread.
        self._owner_thread_id: Optional[int] = None

    def initialize(self, ctx) -> bool:
        """Initialize VRAM monitoring, detect supported extensions.

        Args:
            ctx: ModernGL context (used to get extension string)

        Returns:
            True if VRAM monitoring is supported

        Thread contract (AUD-8313): must be called on the thread that owns
        the GL context (the render thread); it records that thread so the
        query_* methods can refuse to issue raw GL calls from anywhere else.
        """
        # AUD-8313: remember the owning thread for the off-thread guard.
        self._owner_thread_id = threading.get_ident()
        try:
            # Get GL extensions string
            gl = ctypes.CDLL(None)
            extensions_ptr = gl.glGetString(0x1F03)  # GL_EXTENSIONS
            if extensions_ptr:
                extensions_str = ctypes.cast(extensions_ptr, ctypes.c_char_p).value.decode('utf-8')
                self._available_extensions = set(extensions_str.split())

                # Check vendor string
                vendor_ptr = gl.glGetString(0x1F00)  # GL_VENDOR
                if vendor_ptr:
                    vendor_str = ctypes.cast(vendor_ptr, ctypes.c_char_p).value.decode('utf-8').lower()
                    if "nvidia" in vendor_str:
                        self._vendor = "nvidia"
                    elif "ati" in vendor_str or "amd" in vendor_str or "radeon" in vendor_str:
                        self._vendor = "amd"
                    else:
                        self._vendor = "unknown"
            else:
                # Fallback: check context info
                if hasattr(ctx, 'info') and hasattr(ctx.info, 'GL_EXTENSIONS'):
                    self._available_extensions = set(ctx.info.GL_EXTENSIONS)

            # Check for supported extensions
            if self._available_extensions:
                if "GL_NVX_gpu_memory_info" in self._available_extensions:
                    self._supported = True
                    self._vendor = "nvidia"
                elif "GL_ATI_meminfo" in self._available_extensions:
                    self._supported = True
                    self._vendor = "amd"

            return self._supported
        except Exception:
            self._supported = False
            return False

    def query_available_vram_kb(self) -> int:
        """Query current available VRAM in KB.

        Thread contract (AUD-8313): issues a raw glGetIntegerv, so only the
        GL-context owner thread (the one that called initialize()) may
        sample; other threads get the last cached reading without touching
        GL.

        Returns:
            Available VRAM in KB, or 0 if unsupported/error
        """
        if not self._supported:
            return 0
        if self._vendor not in ("nvidia", "amd") and self._vram_available_kb > 0:
            return self._vram_available_kb

        # AUD-8313: never issue raw GL from a thread without the context —
        # that is undefined behaviour on macOS and violates the
        # "UI/planning threads must not touch OpenGL" rule. Return the
        # cached snapshot instead.
        if (self._owner_thread_id is not None
                and threading.get_ident() != self._owner_thread_id):
            return self._vram_available_kb

        # Throttle queries to avoid overhead
        now = time.time()
        if now - self._last_query_time < self._query_interval_sec:
            return self._vram_available_kb

        self._last_query_time = now

        try:
            gl = ctypes.CDLL(None)
            result = ctypes.c_int(4)

            if self._vendor == "nvidia":
                # Use GL_NVX_gpu_memory_info
                gl.glGetIntegerv(self.GL_GPU_MEMORY_INFO_CURRENT_AVAILABLE_VIDMEM_NVX,
                                 ctypes.byref(result))
                self._vram_available_kb = result.value
            elif self._vendor == "amd":
                # Use GL_ATI_meminfo (returns 4 ints: free, aux free, total, largest block)
                results = (ctypes.c_int * 4)(0, 0, 0, 0)
                gl.glGetIntegerv(self.GL_TEXTURE_FREE_MEMORY_ATI, ctypes.byref(results))
                self._vram_available_kb = results[0]  # First value is free texture memory
            else:
                return 0

            return self._vram_available_kb
        except Exception:
            return 0

    def query_total_vram_kb(self) -> int:
        """Query total VRAM in KB.

        Thread contract (AUD-8313): same as query_available_vram_kb — raw
        GL only from the context-owner thread; other threads get the cached
        total.

        Returns:
            Total VRAM in KB, or 0 if unsupported/error
        """
        if not self._supported:
            return 0

        if self._vram_total_kb > 0:
            return self._vram_total_kb

        # AUD-8313: off-thread calls must not touch GL (see
        # query_available_vram_kb).
        if (self._owner_thread_id is not None
                and threading.get_ident() != self._owner_thread_id):
            return self._vram_total_kb

        try:
            gl = ctypes.CDLL(None)
            result = ctypes.c_int(0)

            if self._vendor == "nvidia":
                gl.glGetIntegerv(self.GL_GPU_MEMORY_INFO_TOTAL_AVAILABLE_MEMORY_NVX,
                                 ctypes.byref(result))
                self._vram_total_kb = result.value
            elif self._vendor == "amd":
                # AMD reports total as third value from GL_TEXTURE_FREE_MEMORY_ATI
                results = (ctypes.c_int * 4)(0, 0, 0, 0)
                gl.glGetIntegerv(self.GL_TEXTURE_FREE_MEMORY_ATI, ctypes.byref(results))
                self._vram_total_kb = results[2]

            return self._vram_total_kb
        except Exception:
            return 0

    def get_vram_budget_mb(self, safety_factor: float = 0.7) -> float:
        """Get recommended VRAM budget in MB.

        Args:
            safety_factor: Fraction of available VRAM to use (default 0.7 = 70%)

        Returns:
            Recommended budget in MB, or 0 if unknown
        """
        available_kb = self.query_available_vram_kb()
        if available_kb > 0:
            return (available_kb * safety_factor) / 1024
        return 0

    def is_supported(self) -> bool:
        return self._supported

    def get_vendor(self) -> str:
        return self._vendor

    def get_stats(self) -> dict:
        """Get VRAM monitoring stats."""
        return {
            'supported': self._supported,
            'vendor': self._vendor,
            'total_kb': self._vram_total_kb,
            'available_kb': self._vram_available_kb,
            'budget_mb': self.get_vram_budget_mb(),
        }


# ═══════════════════════════════════════════════════════════════════════
# Render Mode - Format tiering for Preview vs Export
# ═══════════════════════════════════════════════════════════════════════

class RenderMode(Enum):
    """Render mode determines texture format tiering."""
    PREVIEW = "preview"  # Interactive preview: RGBA16F (half-float, 64 bits/pixel)
    EXPORT = "export"    # High-quality export: RGBA32F (full float, 128 bits/pixel)
    FULL = "full"        # Full resolution preview: RGBA16F (same as preview)


def get_dtype_for_mode(render_mode: RenderMode) -> str:
    """Get texture dtype for a render mode.

    Preview/FULL -> 'f2' (RGBA16F, half-float)
    Export -> 'f4' (RGBA32F, full float)
    """
    if render_mode == RenderMode.EXPORT:
        return 'f4'
    return 'f2'


_NUMPY_DTYPE_FOR_TEXTURE: dict = {"f2": None, "f4": None}
try:  # numpy is a hard dependency of the app; tests without it keep import safe
    import numpy as _np

    _NUMPY_DTYPE_FOR_TEXTURE = {
        "f2": _np.float16,
        "f4": _np.float32,
    }
except ImportError:  # pragma: no cover - defensive
    pass


def upload_pooled_texture(pool, array, width: int, height: int,
                          components: int = 4, dtype: str = "f4"):
    """Acquire a pooled texture and upload a CPU float array (R-01).

    统一 CPU→纹理上传 helper：按目标纹理 dtype 转换 numpy 数组后再写。
    旧路径 ``acquire_texture(w, h)`` 默认 f2，而 Paint/AI 的 CPU 结果是
    float32——写入直接 data-size mismatch，调用方把异常当成“功能失败”
    静默直通/返回 None（Paint 无效、AI 结果回不到图里）。

    获取成功但 write 失败时把新纹理归还 pool（不泄漏 active 计数），
    异常向上抛由调用方决定降级路径。

    Args:
        pool: 暴露 acquire_texture/release_texture 的池对象。
        array: H×W×C numpy 数组（CPU 语义，翻转等预处理由调用方完成）。
        dtype: 目标纹理精度；默认 f4（Paint/AI 结果继续参与合成链，
            半精度会先损失带外/渐变精度）。

    Returns:
        池化纹理包装对象（含 .glo）。
    """
    tex = pool.acquire_texture(width, height, components, dtype)
    try:
        target_dtype = getattr(tex, "dtype", dtype) or dtype
        payload = array
        np_dtype = _NUMPY_DTYPE_FOR_TEXTURE.get(target_dtype)
        if (np_dtype is not None and array.dtype != np_dtype):
            payload = array.astype(np_dtype, copy=False)
        tex.write(_np.ascontiguousarray(payload).tobytes())
        return tex
    except Exception:
        release = getattr(pool, "release_texture", None)
        if release is not None:
            try:
                release(tex)
            except Exception:
                logger.exception("upload_pooled_texture: release-on-fail 失败")
        raise


# ═══════════════════════════════════════════════════════════════════════
# Video Slot Pool - Cross-frame texture reuse for video processing
# ═══════════════════════════════════════════════════════════════════════

# AUD-8312: ceiling on concurrently held video slots and the number of
# frames a slot may stay unused before end_frame() reclaims it. Without
# both, the slot map only ever grows across resolution/format changes.
MAX_VIDEO_SLOTS = 16
VIDEO_SLOT_IDLE_FRAMES = 8

# Full slot key: (width, height, components, dtype, slot_id)
_SLOT_KEY_T = Tuple[int, int, int, str, int]
# Base slot identity without the dtype component: (width, height, components, slot_id)
_SLOT_BASE_T = Tuple[int, int, int, int]


class VideoSlotPool:
    """Pool of reusable texture slots for video frame processing.

    When processing video frames, textures with the same size/format are
    often needed repeatedly. This pool provides slots that persist across
    frames, avoiding allocation overhead.

    Usage:
        slot_pool = VideoSlotPool()
        slot_pool.begin_frame()  # Mark slots as available for this frame
        tex = slot_pool.acquire_slot(width=1920, height=1080, slot_id=0)
        # Use tex for processing
        slot_pool.end_frame()  # Unused slots are reclaimed after a while

    Slots are indexed by (width, height, slot_id). Each slot can hold
    exactly one texture. When a slot is acquired, it returns the same
    texture used in previous frames if available.
    """

    def __init__(self, fb_manager: FramebufferManager, resource_pool: 'GPUResourcePool') -> None:
        self._fb_manager = fb_manager
        self._resource_pool = resource_pool
        # Slots: (width, height, components, dtype, slot_id) -> texture
        self._slots: Dict[_SLOT_KEY_T, object] = {}
        # Slot metadata for tracking
        self._slot_info: Dict[_SLOT_KEY_T, dict] = {}
        # AUD-8311: acquire_slot() derives the dtype from render_mode
        # (EXPORT -> 'f4'), which release/clear callers cannot reconstruct
        # from (width, height, components, slot_id) alone. Remember the
        # actual key per base identity as a fallback for lookups that miss
        # the default-'f2' key, so EXPORT slots can actually be released.
        self._slot_keys_by_base: Dict[_SLOT_BASE_T, _SLOT_KEY_T] = {}
        # AUD-8312: per-slot idle-frame streak feeding end_frame() reclaim.
        self._slot_idle_streak: Dict[_SLOT_KEY_T, int] = {}
        # Frame state
        self._in_frame: bool = False
        self._acquired_this_frame: Set[_SLOT_KEY_T] = set()
        # Statistics
        self._total_acquisitions: int = 0
        self._total_reuses: int = 0
        self._total_creations: int = 0

    def begin_frame(self):
        """Mark all slots as available for the current frame."""
        self._in_frame = True
        self._acquired_this_frame.clear()

    def end_frame(self):
        """End frame processing, reclaiming slots left idle too long.

        Slots stay allocated for fast cross-frame reuse, but a slot that
        has not been acquired for ``VIDEO_SLOT_IDLE_FRAMES`` consecutive
        frames is released back to the resource pool (AUD-8312: the slot
        map previously only ever grew between resolution/format changes).
        """
        self._in_frame = False
        for key in [k for k in self._slots if k not in self._acquired_this_frame]:
            streak = self._slot_idle_streak.get(key, 0) + 1
            if streak >= VIDEO_SLOT_IDLE_FRAMES:
                self._reclaim_slot(key)
            else:
                self._slot_idle_streak[key] = streak
        # Slots used this frame restart their idle streak.
        for key in list(self._acquired_this_frame):
            self._slot_idle_streak.pop(key, None)

    def acquire_slot(self, width: int, height: int,
                     components: int = 4,
                     dtype: str = 'f2',
                     slot_id: int = 0,
                     render_mode: Optional[RenderMode] = None) -> object:
        """Acquire a texture from a slot.

        If the slot already has a texture of matching size/format, it's reused.
        Otherwise, a new texture is created and stored in the slot.

        Args:
            width: Texture width
            height: Texture height
            components: Number of components (default 4 = RGBA)
            dtype: Data type ('f2' = half-float, 'f4' = float)
            slot_id: Slot identifier (0-255, for multiple slots of same size)
            render_mode: If provided, auto-select dtype based on mode

        Returns:
            A ModernGL texture
        """
        if render_mode is not None:
            dtype = get_dtype_for_mode(render_mode)

        key = (width, height, components, dtype, slot_id)
        self._total_acquisitions += 1

        # Check if slot already has a matching texture
        if key in self._slots:
            tex = self._slots[key]
            self._total_reuses += 1
            info = self._slot_info.get(key)
            if info is not None:
                info['last_used'] = time.time()
            self._acquired_this_frame.add(key)
            self._slot_idle_streak.pop(key, None)
            return tex

        # AUD-8312: enforce the slot ceiling before creating a new slot.
        self._enforce_slot_cap()

        # Create new texture for slot
        # Use resource pool to create (for proper tracking and budget)
        tex = self._resource_pool.acquire_temp_texture(width, height, components, dtype)
        self._slots[key] = tex
        self._slot_info[key] = {
            'created_time': time.time(),
            'last_used': time.time(),
            'acquisitions': 1,
        }
        self._total_creations += 1
        self._acquired_this_frame.add(key)
        self._slot_idle_streak.pop(key, None)
        self._slot_keys_by_base[(width, height, components, slot_id)] = key
        return tex

    def _resolve_slot_key(self, width: int, height: int, components: int,
                          dtype: str, slot_id: int) -> _SLOT_KEY_T:
        """Map release/clear arguments to the actual slot key (AUD-8311).

        acquire_slot() may have derived 'f4' from render_mode=EXPORT while
        the releasing caller only knows (width, height, components,
        slot_id); fall back to the key recorded at acquire time so the
        slot is found instead of leaking in the slot map.
        """
        key = (width, height, components, dtype, slot_id)
        if key in self._slots:
            return key
        actual = self._slot_keys_by_base.get(
            (width, height, components, slot_id))
        if actual is not None and actual in self._slots:
            return actual
        return key

    def release_slot(self, width: int, height: int,
                     components: int = 4,
                     dtype: str = 'f2',
                     slot_id: int = 0):
        """Mark a slot as available (texture remains in slot for reuse).

        This doesn't actually release the texture - it just marks it as
        available for the next frame. Call clear_slot() to truly release.
        """
        key = self._resolve_slot_key(width, height, components, dtype, slot_id)
        self._acquired_this_frame.discard(key)

    def clear_slot(self, width: int, height: int,
                   components: int = 4,
                   dtype: str = 'f2',
                   slot_id: int = 0):
        """Clear a slot, releasing the texture back to resource pool."""
        key = self._resolve_slot_key(width, height, components, dtype, slot_id)
        if key in self._slots:
            self._reclaim_slot(key)

    def _reclaim_slot(self, key: _SLOT_KEY_T) -> None:
        """Drop a slot's bookkeeping and return its texture to the pool."""
        tex = self._slots.pop(key, None)
        self._slot_info.pop(key, None)
        self._slot_idle_streak.pop(key, None)
        base = (key[0], key[1], key[2], key[4])
        if self._slot_keys_by_base.get(base) == key:
            self._slot_keys_by_base.pop(base, None)
        if tex is not None:
            self._resource_pool.release_texture(tex)

    def _enforce_slot_cap(self) -> None:
        """Keep the slot count under ``MAX_VIDEO_SLOTS`` (AUD-8312).

        Evicts the least-recently-used slot that was NOT acquired in the
        current frame — evicting an in-use texture would hand the same GL
        object to two consumers. When every slot is busy this frame the
        cap is exceeded with a warning instead of corrupting ownership.
        """
        while len(self._slots) >= MAX_VIDEO_SLOTS:
            candidates = [
                (self._slot_info.get(key, {}).get('last_used', 0.0), key)
                for key in self._slots
                if key not in self._acquired_this_frame
            ]
            if not candidates:
                logger.warning(
                    "VideoSlotPool: all %d slots are in use this frame; "
                    "exceeding the cap instead of evicting a live texture",
                    len(self._slots))
                return
            _ts, victim = min(candidates)
            logger.debug(
                "VideoSlotPool: reclaiming idle slot %s to honor slot cap",
                (victim,))
            self._reclaim_slot(victim)

    def clear_all_slots(self):
        """Clear all slots, releasing textures back to resource pool."""
        for key in list(self._slots.keys()):
            tex = self._slots.pop(key)
            self._resource_pool.release_texture(tex)
        self._slot_info.clear()
        self._slot_keys_by_base.clear()
        self._slot_idle_streak.clear()

    def get_slot_count(self) -> int:
        """Number of active slots."""
        return len(self._slots)

    def get_memory_bytes(self) -> int:
        """Total memory used by slots."""
        total = 0
        for key in self._slots:
            width, height, components, dtype, _ = key
            elem_size = 2 if dtype == 'f2' else 4
            total += width * height * components * elem_size
        return total

    def get_reuse_rate(self) -> float:
        """Slot reuse rate (0.0-1.0)."""
        if self._total_acquisitions == 0:
            return 0.0
        return self._total_reuses / self._total_acquisitions

    def get_stats(self) -> dict:
        """Get slot pool statistics."""
        return {
            'slot_count': self.get_slot_count(),
            'memory_bytes': self.get_memory_bytes(),
            'memory_mb': self.get_memory_bytes() / (1024 * 1024),
            'total_acquisitions': self._total_acquisitions,
            'total_reuses': self._total_reuses,
            'total_creations': self._total_creations,
            'reuse_rate': self.get_reuse_rate(),
        }


class TextureUsage(Enum):
    """Texture usage category for pool management."""
    TEMP = auto()      # Short-lived scratch textures (blend intermediates)
    CACHE = auto()     # Long-lived cache-owned textures (CacheEntry)
    OUTPUT = auto()    # Output textures (display, must not be evicted during display)
    TILE = auto()      # Tile textures (large image tiling)
    EXTERNAL = auto()  # External textures we track but don't own (ColorOutput)


@dataclass(frozen=True)
class TextureKey:
    """Immutable key for texture pooling - determines texture format."""
    width: int
    height: int
    components: int = 4
    dtype: str = 'f2'  # 'f2' = half-float (16-bit), 'f4' = float (32-bit)
    usage: TextureUsage = TextureUsage.TEMP

    def __str__(self) -> str:
        return f"{self.width}x{self.height}x{self.components}@{self.dtype}[{self.usage.name}]"

    @property
    def bytes_per_pixel(self) -> int:
        """Bytes per pixel based on dtype and components."""
        elem_size = 2 if self.dtype == 'f2' else 4
        return self.components * elem_size

    @property
    def total_bytes(self) -> int:
        """Total GPU memory for this texture size."""
        return self.width * self.height * self.bytes_per_pixel


@dataclass
class TextureInfo:
    """Tracked metadata for a single GPU texture."""
    glo: int                          # OpenGL texture name
    key: TextureKey                   # Texture specification
    created_time: float = field(default_factory=time.time)
    last_used_time: float = field(default_factory=time.time)
    external: bool = False           # True = we don't own/release this texture
    fence: object = field(default=None, repr=False)  # GLsync fence for reuse safety

    @property
    def width(self) -> int:
        return self.key.width

    @property
    def height(self) -> int:
        return self.key.height

    @property
    def components(self) -> int:
        return self.key.components

    @property
    def dtype(self) -> str:
        return self.key.dtype

    @property
    def usage(self) -> TextureUsage:
        return self.key.usage

    @property
    def bytes(self) -> int:
        """Estimated GPU memory in bytes."""
        return self.key.total_bytes

    def touch(self):
        """Update last used time for LRU."""
        self.last_used_time = time.time()


class GPUResourcePool:
    """Enhanced memory-managed texture pool for the composite rendering pipeline.

    Wraps FramebufferManager to add:
    - Usage-aware texture pooling (TEMP, CACHE, OUTPUT, TILE)
    - Separate pools for different usage categories
    - LRU eviction with memory budget enforcement
    - Pin mechanism to protect active textures
    - External texture tracking (e.g., ColorOutput textures)
    - Detailed diagnostic stats and dump

    Optimization features (v2):
    - Dynamic VRAM awareness with automatic budget adjustment
    - Format tiering (Preview RGBA16F, Export RGBA32F)
    - Cost-based eviction (size × age factor)
    - Video slot pool for cross-frame reuse
    - Enhanced statistics and monitoring

    Thread safety: Used only from the render thread, no locking needed.
    """

    def __init__(self, fb_manager: FramebufferManager, max_memory_mb: int = 1024,
                 enable_vram_monitoring: bool = True,
                 enable_video_slots: bool = True) -> None:
        self.fb_manager = fb_manager
        self._initial_max_memory_mb = max_memory_mb
        self.max_memory_bytes = max_memory_mb * 1024 * 1024

        # ── VRAM Monitoring (v2) ──
        self._vram_monitor: VRAMMonitor = VRAMMonitor()
        self._enable_vram_monitoring = enable_vram_monitoring
        self._vram_budget_update_interval_sec: float = 5.0
        self._last_budget_update_time: float = 0

        # ── Video Slot Pool (v2) ──
        self._video_slot_pool: Optional[VideoSlotPool] = None
        self._enable_video_slots = enable_video_slots
        if enable_video_slots:
            self._video_slot_pool = VideoSlotPool(fb_manager, self)

        # ── Pooled textures (not in active use) ──
        # Keyed by TextureKey, ordered for LRU eviction
        self._pooled_textures: Dict[TextureKey, List] = {}
        self._pool_lru: OrderedDict[int, TextureInfo] = OrderedDict()  # glo -> info

        # ── Active textures (currently in use) ──
        self._active_textures: Dict[int, TextureInfo] = {}  # glo -> TextureInfo

        # ── Pinned textures (protected from eviction) ──
        self._pinned_texture_ids: Set[int] = set()

        # ── Frame fence for safe texture reuse ──
        self._current_fence: object = None  # GLsync — set after each frame
        self._pending_fences: List[object] = []  # Fences not yet signaled
        # Destruction queue for pooled textures whose producer fence has not
        # completed yet.  Eviction is allowed to detach a texture from the
        # reuse pool immediately, but the GL object must stay alive until its
        # last GPU reader has finished.
        self._deferred_releases: List[Tuple[object, Callable[[], None], int]] = []
        self._deferred_release_bytes: int = 0

        # ── Statistics ──
        self._total_created: int = 0
        self._total_reused: int = 0
        self._total_evicted: int = 0

        # ── Enhanced Statistics (v2) ──
        self._stats_format_distribution: Dict[str, int] = {'f2': 0, 'f4': 0}  # dtype -> count
        self._stats_by_usage: Dict[TextureUsage, int] = {}  # usage -> active count
        self._stats_cost_evictions: int = 0  # Number of cost-based evictions
        self._stats_vram_adjustments: int = 0  # Number of budget adjustments
        self._current_render_mode: RenderMode = RenderMode.PREVIEW

    # ═══════════════════════════════════════════════════════════════════════
    # Initialization & VRAM Monitoring (v2)
    # ═══════════════════════════════════════════════════════════════════════

    def initialize_vram_monitoring(self, ctx) -> bool:
        """Initialize VRAM monitoring with ModernGL context.

        Call this after GL context is created (e.g., in initializeGL).

        Args:
            ctx: ModernGL context

        Returns:
            True if VRAM monitoring is supported and initialized
        """
        if not self._enable_vram_monitoring:
            return False

        success = self._vram_monitor.initialize(ctx)
        if success:
            # Update budget based on actual VRAM
            self._update_vram_budget()
        return success

    def _update_vram_budget(self) -> bool:
        """Update memory budget based on current VRAM availability.

        Returns:
            True if budget was adjusted
        """
        if not self._vram_monitor.is_supported():
            return False

        # Throttle updates
        now = time.time()
        if now - self._last_budget_update_time < self._vram_budget_update_interval_sec:
            return False

        self._last_budget_update_time = now

        # Get recommended budget from VRAM monitor
        vram_budget_mb = self._vram_monitor.get_vram_budget_mb(safety_factor=0.6)
        if vram_budget_mb > 0:
            # Use minimum of: VRAM budget and initial configured budget
            # This prevents exceeding user's configured limit even if VRAM is larger
            new_budget_mb = min(vram_budget_mb, self._initial_max_memory_mb)
            new_budget_bytes = int(new_budget_mb * 1024 * 1024)

            if new_budget_bytes != self.max_memory_bytes:
                self.max_memory_bytes = new_budget_bytes
                self._stats_vram_adjustments += 1
                return True

        return False

    def set_render_mode(self, mode: RenderMode):
        """Set current render mode for format tiering.

        This affects the default dtype for acquire_tiered_texture().
        """
        self._current_render_mode = mode

    def get_vram_stats(self) -> dict:
        """Get VRAM monitoring statistics."""
        return self._vram_monitor.get_stats()

    # ═══════════════════════════════════════════════════════════════════════
    # Texture Acquisition
    # ═══════════════════════════════════════════════════════════════════════

    def acquire_temp_texture(self, width: int, height: int,
                             components: int = 4, dtype: str = 'f2'):
        """Acquire a TEMP texture for short-lived scratch use.

        Use for blend intermediates, temporary render targets.
        Call release_texture() when done.
        """
        return self._acquire_texture(TextureKey(
            width=width, height=height,
            components=components, dtype=dtype,
            usage=TextureUsage.TEMP
        ))

    def acquire_cache_texture(self, width: int, height: int,
                               components: int = 4, dtype: str = 'f2'):
        """Acquire a CACHE texture for long-lived cache storage.

        Use for CacheEntry-owned textures.
        Call release_texture() when evicting from cache.
        """
        return self._acquire_texture(TextureKey(
            width=width, height=height,
            components=components, dtype=dtype,
            usage=TextureUsage.CACHE
        ))

    def acquire_output_texture(self, width: int, height: int,
                                components: int = 4, dtype: str = 'f2'):
        """Acquire an OUTPUT texture for display.

        OUTPUT textures are automatically pinned to prevent eviction
        during display. Call unpin_texture() when no longer displaying.
        """
        tex = self._acquire_texture(TextureKey(
            width=width, height=height,
            components=components, dtype=dtype,
            usage=TextureUsage.OUTPUT
        ))
        # Auto-pin output textures
        self._pinned_texture_ids.add(tex.glo)
        return tex

    def acquire_tile_texture(self, width: int, height: int,
                              components: int = 4, dtype: str = 'f2'):
        """Acquire a TILE texture for large image tiling.

        Use for processing large images in chunks.
        Call release_texture() when done with the tile.
        """
        return self._acquire_texture(TextureKey(
            width=width, height=height,
            components=components, dtype=dtype,
            usage=TextureUsage.TILE
        ))

    # ═══════════════════════════════════════════════════════════════════════
    # Format Tiered Texture Acquisition (v2)
    # ═══════════════════════════════════════════════════════════════════════

    def acquire_tiered_texture(self, width: int, height: int,
                                components: int = 4,
                                render_mode: Optional[RenderMode] = None,
                                usage: TextureUsage = TextureUsage.TEMP):
        """Acquire a texture with format automatically selected by render mode.

        Preview/FULL: RGBA16F (dtype='f2', 64 bits/pixel)
        Export: RGBA32F (dtype='f4', 128 bits/pixel)

        Args:
            width: Texture width
            height: Texture height
            components: Number of components (default 4 = RGBA)
            render_mode: Render mode to determine dtype. If None, uses current mode.
            usage: Texture usage category (default TEMP)

        Returns:
            A ModernGL texture with appropriate format
        """
        if render_mode is None:
            render_mode = self._current_render_mode
        dtype = get_dtype_for_mode(render_mode)
        return self._acquire_texture(TextureKey(
            width=width, height=height,
            components=components, dtype=dtype,
            usage=usage
        ))

    def acquire_preview_texture(self, width: int, height: int,
                                components: int = 4,
                                usage: TextureUsage = TextureUsage.TEMP):
        """Acquire a preview-quality texture (RGBA16F).

        Convenience method for preview rendering.
        """
        return self.acquire_tiered_texture(width, height, components,
                                            RenderMode.PREVIEW, usage)

    def acquire_export_texture(self, width: int, height: int,
                               components: int = 4,
                               usage: TextureUsage = TextureUsage.TEMP):
        """Acquire an export-quality texture (RGBA32F).

        Convenience method for high-quality export rendering.
        """
        return self.acquire_tiered_texture(width, height, components,
                                            RenderMode.EXPORT, usage)

    # ═══════════════════════════════════════════════════════════════════════
    # Deep Compositing Texture Acquisition
    # ═══════════════════════════════════════════════════════════════════════

    def acquire_depth_texture(self, width: int, height: int,
                               usage: TextureUsage = TextureUsage.TEMP):
        """Acquire a depth texture (single channel float32).

        Deep compositing uses depth textures to store per-pixel Z values.
        This method creates a single-channel R32F texture suitable for
        depth storage.

        Args:
            width: Texture width
            height: Texture height
            usage: Texture usage category (default TEMP)

        Returns:
            A ModernGL single-channel float32 texture
        """
        return self._acquire_texture(TextureKey(
            width=width, height=height,
            components=1, dtype='f4',  # R32F for precision
            usage=usage
        ))

    # ═══════════════════════════════════════════════════════════════════════
    # Video Slot Pool Management (v2)
    # ═══════════════════════════════════════════════════════════════════════

    def begin_video_frame(self):
        """Begin video frame processing, marking slots available.

        Call before processing each video frame to reset slot state.
        """
        if self._video_slot_pool:
            self._video_slot_pool.begin_frame()

    def end_video_frame(self):
        """End video frame processing.

        Call after processing each video frame. Slots remain allocated
        for reuse in next frame.
        """
        if self._video_slot_pool:
            self._video_slot_pool.end_frame()

    def acquire_video_slot(self, width: int, height: int,
                           components: int = 4,
                           slot_id: int = 0,
                           render_mode: Optional[RenderMode] = None):
        """Acquire a texture from the video slot pool.

        Slots persist across frames for efficient video processing.
        Same-size slots are reused without reallocation.

        Args:
            width: Texture width
            height: Texture height
            components: Number of components (default 4 = RGBA)
            slot_id: Slot identifier (0-255, for multiple slots of same size)
            render_mode: If provided, auto-select dtype based on mode

        Returns:
            A ModernGL texture (reused if slot has matching texture)
        """
        if not self._video_slot_pool:
            # Fall back to regular texture acquisition
            return self.acquire_tiered_texture(width, height, components, render_mode)
        return self._video_slot_pool.acquire_slot(width, height, components,
                                                   'f2', slot_id, render_mode)

    def release_video_slot(self, width: int, height: int,
                           components: int = 4,
                           slot_id: int = 0):
        """Mark a video slot as available for reuse."""
        if self._video_slot_pool:
            self._video_slot_pool.release_slot(width, height, components, 'f2', slot_id)

    def clear_video_slot(self, width: int, height: int,
                         components: int = 4,
                         slot_id: int = 0):
        """Clear a video slot, releasing the texture."""
        if self._video_slot_pool:
            self._video_slot_pool.clear_slot(width, height, components, 'f2', slot_id)

    def clear_all_video_slots(self):
        """Clear all video slots."""
        if self._video_slot_pool:
            self._video_slot_pool.clear_all_slots()

    def get_video_slot_stats(self) -> dict:
        """Get video slot pool statistics."""
        if self._video_slot_pool:
            return self._video_slot_pool.get_stats()
        return {'slot_count': 0, 'memory_mb': 0, 'reuse_rate': 0}

    def acquire_texture(self, width: int, height: int,
                        components: int = 4, dtype: str = 'f2'):
        """Legacy API: Acquire a texture with TEMP usage.

        Deprecated: Use acquire_temp_texture() for clarity.
        """
        return self.acquire_temp_texture(width, height, components, dtype)

    def _acquire_texture(self, key: TextureKey):
        """Internal: Acquire or create a texture for the given key.

        When reusing from pool, skips textures whose fence has not yet been
        signaled by the GPU (prevents writing to a texture still in flight).
        TEMP scratch textures skip the fence check: they are only ever
        written/read inside the single render-thread GL context, whose
        command stream is already ordered — a fence adds nothing and
        (historically, via a broken ctypes signature that never signaled)
        was able to disable pooling entirely.
        """
        # Retire completed evictions before any budget work.  This keeps the
        # queue bounded during repeated resize/preview cycles while retaining
        # all GL destruction on the render thread.
        self.drain_deferred_releases()

        # Try to reuse from pool — skip fenced (in-flight) textures.
        # Reuse MUST be attempted before the budget check: a pooled texture
        # is already resident, so activating it does not grow the footprint.
        # Checking first would evict exactly the reusable textures (cost
        # eviction prefers the largest, i.e. same-size) and force a
        # reallocation of the same size every frame in steady state
        # (~10-16ms per 1080p f2 allocation).
        pooled_list = self._pooled_textures.get(key, [])
        skipped = []
        result_tex = None
        fence_relevant = key.usage != TextureUsage.TEMP

        while pooled_list:
            tex = pooled_list.pop()
            info = self._pool_lru.pop(tex.glo, None)

            # Check if texture's fence has been signaled
            if fence_relevant and info and info.fence is not None:
                if not self._is_fence_signaled(info.fence):
                    # Texture still in flight — keep looking
                    skipped.append((tex, info))
                    continue
                # Fence signaled — clean it up
                self._delete_fence(info.fence)
                info.fence = None

            # Safe to reuse
            if info:
                info.touch()
            else:
                info = TextureInfo(glo=tex.glo, key=key)
            self._active_textures[tex.glo] = info
            self._total_reused += 1
            result_tex = tex
            break

        # Put skipped textures back
        for tex, info in skipped:
            pooled_list.append(tex)
            self._pool_lru[tex.glo] = info

        if result_tex is not None:
            return result_tex

        # Only a brand-new allocation grows resident memory — budget-check
        # (and eviction) belongs here, on the create path.
        self._check_budget(key.total_bytes)

        # Create new texture via fb_manager
        tex = self.fb_manager.get_temp_texture(
            key.width, key.height, key.components, key.dtype
        )
        info = TextureInfo(glo=tex.glo, key=key)
        self._active_textures[tex.glo] = info
        self._total_created += 1
        return tex

    # ═══════════════════════════════════════════════════════════════════════
    # Texture Destruction (true GL release)
    # ═══════════════════════════════════════════════════════════════════════

    def _destroy_texture(self, tex):
        """Truly destroy a GL texture: release it and clear associated FBO.

        Unlike release_temp_texture() which returns the texture to the pool,
        this permanently destroys the OpenGL resource.
        """
        # Clear any associated FBO from fb_manager cache
        fbo = self.fb_manager._fbo_cache.pop(tex, None)
        if fbo is not None:
            try:
                fbo.release()
            except Exception as e:
                logger.debug(f"FBO release error during texture destruction: {e}")
        # Remove from fb_manager's active set if present
        self.fb_manager._active_textures.discard(tex)
        # Destroy the GL texture
        try:
            tex.release()
        except Exception as e:
            logger.debug(f"Texture release error during destruction: {e}")

    # ═══════════════════════════════════════════════════════════════════════
    # Texture Release
    # ═══════════════════════════════════════════════════════════════════════

    def release_texture(self, tex, force_evict: bool = False):
        """Release a texture back to the pool.

        Args:
            tex: The texture to release
            force_evict: If True, don't pool the texture, release immediately
        """
        if tex.glo not in self._active_textures:
            # Not tracked by us, delegate to fb_manager
            self.fb_manager.release_temp_texture(tex)
            return

        info = self._active_textures.pop(tex.glo)
        self._pinned_texture_ids.discard(tex.glo)

        if force_evict:
            # True destroy — don't return to pool.  A force eviction still
            # cannot delete a texture while a previous frame may be reading
            # it, so defer the actual GL release behind its fence.
            if info.fence is not None and not self._is_fence_signaled(info.fence):
                self._defer_texture_release(tex, info.fence, info.bytes)
            else:
                self._delete_fence(info.fence)
                self._destroy_texture(tex)
            self._total_evicted += 1
            return

        # Return to pool for reuse — tag with current frame fence
        key = info.key
        if key not in self._pooled_textures:
            self._pooled_textures[key] = []
        self._pooled_textures[key].append(tex)

        # Tag with current fence so we don't reuse while GPU still reads it
        info.fence = self._current_fence
        info.touch()
        self._pool_lru[tex.glo] = info

    def release_texture_by_id(self, tex_id: int, force_evict: bool = False):
        """Release a texture by its OpenGL ID.

        Used by CacheManager when texture was created via acquire_*_texture().
        """
        info = self._active_textures.get(tex_id)
        if info is None:
            # Check if it's in fb_manager's active set (legacy path)
            for tex in list(self.fb_manager._active_textures):
                if tex.glo == tex_id:
                    self.fb_manager.release_temp_texture(tex)
                    break
            return

        if info.external:
            # Don't release external textures
            return

        # Find the texture object
        for tex in list(self.fb_manager._active_textures):
            if tex.glo == tex_id:
                self.release_texture(tex, force_evict=force_evict)
                return

    # ═══════════════════════════════════════════════════════════════════════
    # Pin / Unpin (Eviction Protection)
    # ═══════════════════════════════════════════════════════════════════════

    def pin_texture(self, tex_id: int):
        """Pin a texture to prevent it from being evicted.

        Use for textures currently displayed or in active use.
        """
        self._pinned_texture_ids.add(tex_id)

    def unpin_texture(self, tex_id: int):
        """Unpin a texture to allow eviction."""
        self._pinned_texture_ids.discard(tex_id)

    def is_pinned(self, tex_id: int) -> bool:
        """Check if a texture is pinned."""
        return tex_id in self._pinned_texture_ids

    # ═══════════════════════════════════════════════════════════════════════
    # External Texture Tracking
    # ═══════════════════════════════════════════════════════════════════════

    def track_external(self, texture_id: int, width: int, height: int,
                       components: int = 4, dtype: str = 'f2'):
        """Track a texture we don't own (e.g., ColorOutput from shared context).

        External textures are not released by this pool - only tracked for
        memory accounting.
        """
        if texture_id > 0:
            key = TextureKey(
                width=width, height=height,
                components=components, dtype=dtype,
                usage=TextureUsage.EXTERNAL
            )
            info = TextureInfo(glo=texture_id, key=key, external=True)
            self._active_textures[texture_id] = info

    def untrack_external(self, texture_id: int):
        """Stop tracking an external texture."""
        info = self._active_textures.pop(texture_id, None)
        if info and info.external:
            self._pinned_texture_ids.discard(texture_id)

    # ═══════════════════════════════════════════════════════════════════════
    # Frame Fence — GPU/CPU synchronization without finish()
    # ═══════════════════════════════════════════════════════════════════════

    @staticmethod
    def _gl_sync_funcs():
        """Resolve glFenceSync/glClientWaitSync/glDeleteSync once, with correct
        signatures.

        历史缺陷：此前每次调用 ``ctypes.CDLL(None)`` 且未声明
        restype/argtypes——glFenceSync 返回的 64 位 sync 指针被按默认
        c_int 截断，glClientWaitSync 拿到坏指针返回 GL_WAIT_FAILED，
        永不命中 ALREADY_SIGNALED/CONDITION_SATISFIED。结果池内纹理
        被永久判定 in-flight，池复用完全失效（实测 created=556
        reused=2），每次 acquire 都重新分配全尺寸纹理（1080p f2 单次
        ~10-16ms），把交互/播放拖到个位数 fps。
        """
        funcs = getattr(GPUResourcePool, "_gl_sync_funcs_cached", None)
        if funcs is not None:
            return funcs
        try:
            gl = ctypes.CDLL(None)
            gl.glFenceSync.restype = ctypes.c_void_p
            gl.glFenceSync.argtypes = [ctypes.c_uint, ctypes.c_uint]
            gl.glClientWaitSync.restype = ctypes.c_uint
            gl.glClientWaitSync.argtypes = [
                ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint64]
            gl.glDeleteSync.restype = None
            gl.glDeleteSync.argtypes = [ctypes.c_void_p]
            funcs = (gl.glFenceSync, gl.glClientWaitSync, gl.glDeleteSync)
        except Exception as e:
            logger.warning(f"GL sync function resolution failed: {e}")
            funcs = (None, None, None)
        GPUResourcePool._gl_sync_funcs_cached = funcs
        return funcs

    def insert_fence(self):
        """Insert a GL fence sync at the current point in the GPU command stream.

        Call this after completing a frame's rendering, before emitting
        frame_ready. Textures released after this point will be tagged with
        this fence; they won't be reused until the GPU has signaled completion.

        Uses raw ctypes to call glFenceSync — avoids heavy OpenGL bindings.
        """
        gl_fence_sync, _, _ = self._gl_sync_funcs()
        if gl_fence_sync is None:
            return
        try:
            # glFenceSync(GL_SYNC_GPU_COMMANDS_COMPLETE, 0)
            # GL_SYNC_GPU_COMMANDS_COMPLETE = 0x9117
            sync = gl_fence_sync(0x9117, 0)
            if sync:
                # Retire old current fence to pending list
                if self._current_fence is not None:
                    self._pending_fences.append(self._current_fence)
                self._current_fence = sync
                # Prune signaled pending fences to prevent unbounded growth
                self._prune_signaled_fences()
                self.drain_deferred_releases()
        except Exception as e:
            logger.warning(f"Fence creation failed: {e}")

    def _defer_texture_release(self, tex, fence: object,
                               bytes_size: int) -> None:
        """Queue a texture for render-thread destruction after ``fence``.

        The texture has already been removed from the pool's active/LRU
        tracking by the caller.  The callback captures only the render-owned
        ModernGL object and is never executed from a UI or worker thread.
        """
        self._deferred_releases.append(
            (fence, lambda tex=tex: self._destroy_texture(tex), bytes_size)
        )
        self._deferred_release_bytes += max(0, int(bytes_size))

    def drain_deferred_releases(self, block: bool = False,
                                max_to_drain: Optional[int] = None) -> int:
        """Release textures whose GPU-read fence has completed.

        Args:
            block: During teardown, wait for the current GL command stream by
                calling the render-context ``finish`` fallback before release.
            max_to_drain: Optional limit useful for keeping interactive work
                bounded.

        Returns:
            Number of deferred texture releases completed.
        """
        if not self._deferred_releases:
            return 0

        remaining: List[Tuple[object, Callable[[], None], int]] = []
        drained = 0
        for fence, callback, bytes_size in self._deferred_releases:
            if max_to_drain is not None and drained >= max_to_drain:
                remaining.append((fence, callback, bytes_size))
                continue

            ready = self._is_fence_signaled(fence)
            if not ready and block:
                # ``force_cleanup`` is called only while this pool's render
                # context is current.  A bounded GL finish is the correctness
                # fallback when a fence is still pending or unavailable.
                ctx = getattr(self.fb_manager, "ctx", None)
                finish = getattr(ctx, "finish", None)
                if callable(finish):
                    try:
                        finish()
                    except Exception as exc:
                        logger.debug("Deferred texture finish failed: %s", exc)
                ready = True

            if not ready:
                remaining.append((fence, callback, bytes_size))
                continue

            self._delete_fence(fence)
            try:
                callback()
            finally:
                self._deferred_release_bytes = max(
                    0, self._deferred_release_bytes - max(0, int(bytes_size)))
                drained += 1

        self._deferred_releases = remaining
        return drained

    def _is_fence_signaled(self, fence) -> bool:
        """Check if a GL fence has been signaled (non-blocking).

        Returns True if fence is None, already signaled, or on error.
        Uses glClientWaitSync with timeout=0 for non-blocking check.
        """
        if fence is None:
            return True
        _, gl_client_wait_sync, _ = self._gl_sync_funcs()
        if gl_client_wait_sync is None:
            return True
        try:
            # glClientWaitSync(sync, GL_SYNC_FLUSH_COMMANDS_BIT, 0)
            # GL_SYNC_FLUSH_COMMANDS_BIT = 0x00000001
            # GL_ALREADY_SIGNALED = 0x911A
            # GL_CONDITION_SATISFIED = 0x911C
            result = gl_client_wait_sync(fence, 0x00000001, 0)
            return result in (0x911A, 0x911C)  # ALREADY_SIGNALED or CONDITION_SATISFIED
        except Exception as e:
            logger.warning(f"Fence status check failed: {e}")
            return True  # On error, assume signaled (safe fallback)

    def _delete_fence(self, fence):
        """Delete a GL sync object."""
        if fence is None:
            return
        _, _, gl_delete_sync = self._gl_sync_funcs()
        if gl_delete_sync is None:
            return
        try:
            gl_delete_sync(fence)
        except Exception as e:
            logger.warning(f"Fence deletion failed: {e}")

    def _prune_signaled_fences(self):
        """Remove signaled fences from the pending list to prevent leaks."""
        still_pending = []
        for fence in self._pending_fences:
            if self._is_fence_signaled(fence):
                self._delete_fence(fence)
            else:
                still_pending.append(fence)
        self._pending_fences = still_pending

    # ═══════════════════════════════════════════════════════════════════════
    # Memory Budget & Eviction
    # ═══════════════════════════════════════════════════════════════════════

    def _check_budget(self, additional_bytes: int):
        """Check if adding texture would exceed budget, evict if needed.

        Also updates VRAM budget dynamically if monitoring is enabled.
        """
        # Try to update budget based on current VRAM
        self._update_vram_budget()

        # ``memory_usage_bytes()`` intentionally reports active textures for
        # backwards-compatible diagnostics.  It is not sufficient for budget
        # enforcement: released textures remain resident in the reuse pool
        # and deferred releases remain real GL allocations until their fence
        # signals.  Count all resident bytes here or repeated preview frames
        # can silently grow VRAM while the pool believes it is below budget.
        current = self.resident_memory_bytes()
        projected = current + additional_bytes

        if projected > self.max_memory_bytes:
            self._evict_to_free(projected - self.max_memory_bytes)

    def _evict_to_free(self, bytes_needed: int):
        """Evict textures to free at least bytes_needed bytes.

        Uses cost-based eviction: cost = size × age_factor
        - Larger textures have higher cost (more memory to free)
        - Older textures (longer in pool) have higher age_factor

        This balances LRU with memory efficiency: prefer evicting
        large, old textures rather than small, recently-used ones.
        """
        bytes_freed = 0
        iterations = 0
        max_iterations = len(self._pool_lru) + 1  # Safety limit

        # If pool is small, use simple LRU
        if len(self._pool_lru) <= 3:
            while bytes_freed < bytes_needed and self._pool_lru and iterations < max_iterations:
                iterations += 1
                glo, info = self._pool_lru.popitem(last=False)
                if self._try_evict_texture(glo, info):
                    bytes_freed += info.bytes
                else:
                    # AUD-8310: eviction refused (pinned texture) — restore
                    # the entry to _pool_lru (as most-recently-used) instead
                    # of leaving it an invisible orphan: a popped-but-kept
                    # texture stays in _pooled_textures forever, invisible to
                    # cost eviction (_rank_cost_candidates only scans
                    # _pool_lru) and to pooled_memory_bytes accounting.
                    self._pool_lru[glo] = info
        else:
            # AUD-8316: cost-based eviction — rank candidates ONCE (O(n log
            # n)) and walk the ranking, instead of rescanning the whole LRU
            # after every single eviction (O(n²) on the render thread's
            # allocation path under pool pressure).
            for glo, info, _cost in self._rank_cost_candidates():
                if bytes_freed >= bytes_needed:
                    break
                if glo not in self._pool_lru:
                    continue  # already gone (e.g. deferred-release path)
                if self._try_evict_texture(glo, info):
                    bytes_freed += info.bytes
                    self._stats_cost_evictions += 1

        if bytes_freed < bytes_needed:
            # Log warning - couldn't free enough
            logger.warning(f"Could only free {bytes_freed} bytes, "
                           f"needed {bytes_needed}")

    def _rank_cost_candidates(self) -> List[Tuple[int, "TextureInfo", float]]:
        """Rank pooled textures by eviction cost, highest first (AUD-8316).

        Cost formula (unchanged): cost = size × (1 + LRU position factor),
        oldest entries carry the highest factor (up to 2.0).

        Returns:
            List of (glo, info, cost) sorted by descending cost, pinned
            textures excluded.
        """
        ranked: List[Tuple[int, "TextureInfo", float]] = []
        pool_size = len(self._pool_lru)
        if pool_size == 0:
            return ranked

        for idx, (glo, info) in enumerate(self._pool_lru.items()):
            if glo in self._pinned_texture_ids:
                continue
            age_factor = 1.0 + (idx / pool_size)  # 1.0 to 2.0
            ranked.append((glo, info, info.bytes * age_factor))

        ranked.sort(key=lambda item: item[2], reverse=True)
        return ranked

    def _try_evict_texture(self, glo: int, info: TextureInfo) -> bool:
        """Try to evict a texture. Returns True if evicted."""
        if glo in self._pinned_texture_ids:
            return False

        # Find and remove from pool list
        key = info.key
        pooled_list = self._pooled_textures.get(key, [])
        for tex in pooled_list:
            if tex.glo == glo:
                # Remove from LRU tracking only after the texture has been
                # found.  More importantly, never destroy an in-flight GL
                # object: move it to the deferred queue instead.
                self._pool_lru.pop(glo, None)
                pooled_list.remove(tex)
                if info.fence is not None and not self._is_fence_signaled(info.fence):
                    self._defer_texture_release(tex, info.fence, info.bytes)
                else:
                    self._delete_fence(info.fence)
                    self._destroy_texture(tex)
                self._total_evicted += 1
                return True

        return False

    def evict_all_pooled(self):
        """Evict all pooled textures (not active)."""
        # Keep pooled textures alive until their producer fence completes.
        # This path is used both by normal memory pressure and by teardown;
        # force_cleanup() drains it synchronously first.
        for key, pooled_list in list(self._pooled_textures.items()):
            for tex in list(pooled_list):
                info = self._pool_lru.pop(tex.glo, None)
                if info is not None and info.fence is not None \
                        and not self._is_fence_signaled(info.fence):
                    self._defer_texture_release(tex, info.fence, info.bytes)
                else:
                    self._delete_fence(info.fence if info is not None else None)
                    self._destroy_texture(tex)
                self._total_evicted += 1
            pooled_list.clear()
        self._pooled_textures.clear()
        self._pool_lru.clear()

    # ═══════════════════════════════════════════════════════════════════════
    # Force Cleanup
    # ═══════════════════════════════════════════════════════════════════════

    def force_cleanup(self, reason: str = ""):
        """Force cleanup all resources.

        Args:
            reason: Why cleanup is needed (for logging)
        """
        reason_str = f" ({reason})" if reason else ""
        logger.info(f"Force cleanup{reason_str}: "
                    f"active={self.active_count()}, pooled={self.pooled_count()}, "
                    f"slots={self.get_video_slot_stats()['slot_count']}")

        # Teardown owns the render context, so finish outstanding GPU work
        # before destructing active/pooled resources.  This also makes the
        # subsequent deferred-release drain deterministic on context close.
        ctx = getattr(self.fb_manager, "ctx", None)
        finish = getattr(ctx, "finish", None)
        if callable(finish):
            try:
                finish()
            except Exception as exc:
                logger.debug("GPU finish before resource cleanup failed: %s", exc)
        self.drain_deferred_releases(block=True)

        # Clear video slot pool first (v2)
        if self._video_slot_pool:
            self._video_slot_pool.clear_all_slots()

        # Evict all pooled textures
        self.evict_all_pooled()

        # Release all active non-external textures
        for tex_id, info in list(self._active_textures.items()):
            if not info.external:
                for tex in list(self.fb_manager._active_textures):
                    if tex.glo == tex_id:
                        self._destroy_texture(tex)
                        break

        # Clear tracking (keep external)
        self._active_textures = {
            k: v for k, v in self._active_textures.items() if v.external
        }
        self._pinned_texture_ids.clear()

        # Reset enhanced stats (v2)
        self._stats_format_distribution = {'f2': 0, 'f4': 0}
        self._stats_by_usage = {}
        self._stats_cost_evictions = 0
        self._stats_vram_adjustments = 0

        # Truly destroy all GL resources (textures, FBOs) in the FramebufferManager
        self.fb_manager.cleanup()

        # Clean up all fences
        self._cleanup_all_fences()

    def cleanup(self):
        """Legacy API: Same as force_cleanup()."""
        self.force_cleanup("cleanup")

    def _cleanup_all_fences(self):
        """Delete all outstanding GL sync fences."""
        for fence in self._pending_fences:
            self._delete_fence(fence)
        self._pending_fences.clear()
        self._delete_fence(self._current_fence)
        self._current_fence = None

    # ═══════════════════════════════════════════════════════════════════════
    # FBO Access
    # ═══════════════════════════════════════════════════════════════════════

    def get_fbo(self, texture):
        """Get or create an FBO for a texture (delegates to FramebufferManager)."""
        return self.fb_manager.get_fbo(texture)

    # ═══════════════════════════════════════════════════════════════════════
    # Statistics
    # ═══════════════════════════════════════════════════════════════════════

    def memory_usage_bytes(self) -> int:
        """Total estimated GPU memory used by active textures."""
        return sum(info.bytes for info in self._active_textures.values())

    def resident_memory_bytes(self) -> int:
        """Total estimated bytes still resident in the GPU resource pool.

        This includes active textures, pooled textures available for reuse,
        and textures waiting for a producer fence before destruction.  The
        latter two categories are easy to miss because they are not active,
        but they still consume VRAM and must participate in admission and
        eviction decisions.
        """
        return (self.memory_usage_bytes()
                + self.pooled_memory_bytes()
                + int(self._deferred_release_bytes))

    def resident_memory_mb(self) -> float:
        """Total resident GPU memory in MiB."""
        return self.resident_memory_bytes() / (1024 * 1024)

    def memory_usage_mb(self) -> float:
        return self.memory_usage_bytes() / (1024 * 1024)

    def pooled_memory_bytes(self) -> int:
        """Total estimated GPU memory in pooled (inactive) textures."""
        return sum(info.bytes for info in self._pool_lru.values())

    def pooled_memory_mb(self) -> float:
        return self.pooled_memory_bytes() / (1024 * 1024)

    def active_count(self) -> int:
        """Number of currently active textures (owned + external)."""
        return len(self._active_textures)

    def pooled_count(self) -> int:
        """Number of textures in the pool (inactive, ready for reuse)."""
        return len(self._pool_lru)

    def pinned_count(self) -> int:
        """Number of pinned textures."""
        return len(self._pinned_texture_ids)

    def owned_count(self) -> int:
        """Number of textures we own (not external)."""
        return sum(1 for info in self._active_textures.values() if not info.external)

    def external_count(self) -> int:
        """Number of externally-owned textures being tracked."""
        return sum(1 for info in self._active_textures.values() if info.external)

    def total_created(self) -> int:
        """Total number of textures created since start."""
        return self._total_created

    def total_reused(self) -> int:
        """Total number of textures reused from pool."""
        return self._total_reused

    def total_evicted(self) -> int:
        """Total number of textures evicted."""
        return self._total_evicted

    # ── Detailed Statistics ──

    def get_stats(self) -> dict:
        """Get detailed statistics as a dictionary.

        Includes v2 enhancements:
        - VRAM monitoring stats
        - Video slot pool stats
        - Format distribution (f2/f4)
        - Cost eviction stats
        """
        # Calculate format distribution
        f2_count = sum(1 for info in self._active_textures.values() if info.dtype == 'f2')
        f4_count = sum(1 for info in self._active_textures.values() if info.dtype == 'f4')

        # Calculate usage distribution
        usage_counts = {}
        for info in self._active_textures.values():
            usage_counts[info.usage] = usage_counts.get(info.usage, 0) + 1

        # Video slot stats
        slot_stats = self.get_video_slot_stats()

        # VRAM stats
        vram_stats = self.get_vram_stats()

        return {
            'active_count': self.active_count(),
            'pooled_count': self.pooled_count(),
            'pinned_count': self.pinned_count(),
            'owned_count': self.owned_count(),
            'external_count': self.external_count(),
            'memory_mb': self.memory_usage_mb(),
            'pooled_memory_mb': self.pooled_memory_mb(),
            'deferred_memory_mb': self._deferred_release_bytes / (1024 * 1024),
            'resident_memory_mb': self.resident_memory_mb(),
            'budget_mb': self.max_memory_bytes / (1024 * 1024),
            'initial_budget_mb': self._initial_max_memory_mb,
            'total_created': self._total_created,
            'total_reused': self._total_reused,
            'total_evicted': self._total_evicted,
            'cost_evictions': self._stats_cost_evictions,
            'vram_adjustments': self._stats_vram_adjustments,
            'format_f2_count': f2_count,
            'format_f4_count': f4_count,
            'usage_distribution': {u.name: c for u, c in usage_counts.items()},
            'video_slots': slot_stats,
            'vram_monitor': vram_stats,
            'render_mode': self._current_render_mode.name,
        }

    def dump_stats(self) -> str:
        """Return a formatted string with current resource statistics.

        Enhanced with v2 stats: format distribution, video slots, VRAM monitoring.
        """
        stats = self.get_stats()
        pct = (stats['resident_memory_mb'] / stats['budget_mb'] * 100) \
            if stats['budget_mb'] > 0 else 0
        reuse_rate = (stats['total_reused'] / (stats['total_created'] + stats['total_reused']) * 100
                      if (stats['total_created'] + stats['total_reused']) > 0 else 0)

        # Format distribution
        format_str = f"f2={stats['format_f2_count']}/f4={stats['format_f4_count']}"

        # Video slots
        slot_str = f"slots={stats['video_slots']['slot_count']}/{stats['video_slots']['reuse_rate']*100:.0f}%"

        # VRAM info
        vram_str = ""
        if stats['vram_monitor']['supported']:
            vram_str = f"VRAM={stats['vram_monitor']['available_kb']}KB free"

        return (
            f"[ResourcePool] active={stats['active_count']} "
            f"(owned={stats['owned_count']}, ext={stats['external_count']}, pinned={stats['pinned_count']}) "
            f"pooled={stats['pooled_count']} "
            f"vram={stats['memory_mb']:.1f}+{stats['pooled_memory_mb']:.1f}"
            f"+{stats['deferred_memory_mb']:.1f}/{stats['budget_mb']:.0f}MB"
            f" ({pct:.0f}%) "
            f"reuse={reuse_rate:.0f}% "
            f"format={format_str} "
            f"{slot_str} {vram_str}"
        )

    def dump_enhanced_stats(self) -> str:
        """Return a detailed multi-line stats report."""
        stats = self.get_stats()
        lines = [
            "=" * 60,
            "GPU Resource Pool Enhanced Statistics",
            "=" * 60,
            "",
            "Memory:",
            f"  Active: {stats['memory_mb']:.2f} MB ({stats['active_count']} textures)",
            f"  Pooled: {stats['pooled_memory_mb']:.2f} MB ({stats['pooled_count']} textures)",
            f"  Budget: {stats['budget_mb']:.0f} MB (initial: {stats['initial_budget_mb']:.0f} MB)",
            f"  Adjustments: {stats['vram_adjustments']}",
            "",
            "Formats:",
            f"  RGBA16F (f2): {stats['format_f2_count']} active",
            f"  RGBA32F (f4): {stats['format_f4_count']} active",
            f"  Render Mode: {stats['render_mode']}",
            "",
            "Operations:",
            f"  Created: {stats['total_created']}",
            f"  Reused: {stats['total_reused']}",
            f"  Evicted: {stats['total_evicted']} (cost-based: {stats['cost_evictions']})",
            "",
            "Video Slots:",
            f"  Active Slots: {stats['video_slots']['slot_count']}",
            f"  Slot Memory: {stats['video_slots']['memory_mb']:.2f} MB",
            f"  Reuse Rate: {stats['video_slots']['reuse_rate']*100:.0f}%",
            "",
            "VRAM Monitor:",
            f"  Supported: {stats['vram_monitor']['supported']}",
            f"  Vendor: {stats['vram_monitor']['vendor']}",
            f"  Available: {stats['vram_monitor']['available_kb']} KB",
            "",
            "Usage Distribution:",
        ]
        for usage, count in stats['usage_distribution'].items():
            lines.append(f"  {usage}: {count}")
        lines.append("=" * 60)
        return "\n".join(lines)

    def dump_active_textures(self) -> str:
        """Return a detailed list of active textures."""
        lines = ["[ResourcePool] Active textures:"]
        for glo, info in sorted(self._active_textures.items(), key=lambda x: -x[1].bytes):
            pinned = " [PINNED]" if glo in self._pinned_texture_ids else ""
            external = " [EXT]" if info.external else ""
            lines.append(f"  {glo}: {info.key} {info.bytes / 1024:.0f}KB{pinned}{external}")
        return "\n".join(lines)

    def dump_pooled_textures(self) -> str:
        """Return a detailed list of pooled textures."""
        lines = ["[ResourcePool] Pooled textures:"]
        for key, pooled_list in sorted(self._pooled_textures.items(), key=lambda x: -x[0].total_bytes):
            count = len(pooled_list)
            if count > 0:
                lines.append(f"  {key}: {count}x ({key.total_bytes / 1024:.0f}KB each)")
        return "\n".join(lines)

    def dump_full_report(self) -> str:
        """Return a comprehensive resource report."""
        lines = [
            "=" * 60,
            "GPU Resource Pool Report",
            "=" * 60,
            self.dump_stats(),
            "",
            self.dump_active_textures(),
            "",
            self.dump_pooled_textures(),
            "=" * 60,
        ]
        return "\n".join(lines)

    # ── Iteration for Debugging ──

    def iter_active_textures(self) -> Iterator[Tuple[int, TextureInfo]]:
        """Iterate over active textures (glo, info)."""
        return iter(self._active_textures.items())

    def iter_pooled_textures(self) -> Iterator[Tuple[int, TextureInfo]]:
        """Iterate over pooled textures (glo, info)."""
        return iter(self._pool_lru.items())

    def has_texture(self, tex_id: int) -> bool:
        """Check if a texture is being tracked (active or pooled)."""
        return tex_id in self._active_textures or tex_id in self._pool_lru

    # ═══════════════════════════════════════════════════════════════════════
    # Memory Estimation for Tiling Decision (v3)
    # ═══════════════════════════════════════════════════════════════════════

    @staticmethod
    def estimate_texture_memory(width: int, height: int,
                                components: int = 4,
                                dtype: str = 'f2') -> int:
        """Estimate GPU memory for a single texture.

        Args:
            width: Texture width in pixels.
            height: Texture height in pixels.
            components: Number of channels (default 4 = RGBA).
            dtype: Data type ('f2' = half-float 16-bit, 'f4' = float 32-bit).

        Returns:
            Estimated memory in bytes.
        """
        elem_size = 2 if dtype == 'f2' else 4
        return width * height * components * elem_size

    @staticmethod
    def estimate_render_memory(
        output_width: int,
        output_height: int,
        node_count: int,
        effect_count: int = 0,
        source_count: int = 1,
        render_mode: str = "preview",
        safety_factor: float = 1.4
    ) -> int:
        """Estimate GPU memory needed for rendering a graph.

        This estimation accounts for:
        - Output texture (display/output)
        - Source textures (color_input, constant, image_input)
        - Effect intermediate textures (blur pass, blend, etc.)
        - Cache textures for reusable results
        - FBO overhead (~5%)

        Args:
            output_width: Output image width in pixels.
            output_height: Output image height in pixels.
            node_count: Total number of nodes in the graph.
            effect_count: Number of effect nodes (blur, grade, etc.).
            source_count: Number of source nodes (color_input, constant, image_input).
            render_mode: "preview" uses f2 (64bpp), "export" uses f4 (128bpp).
            safety_factor: Multiplier for safety margin (default 1.5).

        Returns:
            Estimated GPU memory needed in bytes.
        """
        # Determine dtype based on render mode
        dtype = 'f2' if render_mode in ("preview", "full") else 'f4'

        # Base memory per texture
        tex_size = GPUResourcePool.estimate_texture_memory(
            output_width, output_height, 4, dtype
        )

        # Memory breakdown:
        # 1. Output texture: 1x
        # 2. Source textures: source_count x (may be smaller if external)
        # 3. Effect intermediates: effect_count x 2 (most effects need 2 passes)
        # 4. Cache entries: ~node_count * 0.3 (estimated cache hit rate)
        # 5. FBO overhead: ~5%

        output_mem = tex_size
        source_mem = tex_size * source_count  # Conservative: full-size source textures
        effect_mem = tex_size * effect_count * 2  # 2 passes per effect
        cache_mem = tex_size * max(0, node_count - source_count - effect_count) * 0.3

        total = output_mem + source_mem + effect_mem + cache_mem
        total_with_overhead = total * 1.05  # FBO overhead
        total_safe = int(total_with_overhead * safety_factor)

        return total_safe

    def can_render_full_frame(
        self,
        output_width: int,
        output_height: int,
        node_count: int,
        effect_count: int = 0,
        source_count: int = 1,
        render_mode: str = "preview"
    ) -> Tuple[bool, int, int]:
        """Check if full-frame rendering is possible within memory budget.

        Thread contract (AUD-8313): render-thread only. This refreshes the
        VRAM budget via the monitor's raw GL queries; calls from other
        threads silently fall back to the cached budget (see
        VRAMMonitor.query_available_vram_kb).

        Args:
            output_width: Output image width in pixels.
            output_height: Output image height in pixels.
            node_count: Total number of nodes in the graph.
            effect_count: Number of effect nodes.
            source_count: Number of source nodes.
            render_mode: "preview" or "export".

        Returns:
            Tuple of (can_render, estimated_memory_bytes, budget_bytes).
        """
        estimated = self.estimate_render_memory(
            output_width, output_height,
            node_count, effect_count, source_count,
            render_mode
        )

        # Update budget from VRAM if available
        self._update_vram_budget()
        budget = self.max_memory_bytes

        can_render = estimated <= budget
        return (can_render, estimated, budget)

    def suggest_tile_size(
        self,
        output_width: int,
        output_height: int,
        node_count: int,
        effect_count: int = 0,
        source_count: int = 1,
        render_mode: str = "preview",
        max_tile_size: int = 2048,
        min_tile_size: int = 512
    ) -> int:
        """Suggest optimal tile size for tiled rendering.

        Finds the largest tile size that fits within the memory budget.

        Thread contract (AUD-8313): render-thread only — refreshes the VRAM
        budget via raw GL queries; other threads receive the cached budget.

        Args:
            output_width: Output image width (used for aspect ratio).
            output_height: Output image height.
            node_count: Total number of nodes.
            effect_count: Number of effect nodes.
            source_count: Number of source nodes.
            render_mode: "preview" or "export".
            max_tile_size: Maximum tile dimension (default 2048).
            min_tile_size: Minimum tile dimension (default 512).

        Returns:
            Recommended tile size in pixels.
        """
        # Update budget from VRAM if available
        self._update_vram_budget()
        budget = self.max_memory_bytes

        # Try tile sizes from largest to smallest
        tile_sizes = [2048, 1024, 512, 256]
        for tile_size in tile_sizes:
            if tile_size > max_tile_size:
                continue
            if tile_size < min_tile_size:
                continue

            estimated = self.estimate_render_memory(
                tile_size, tile_size,
                node_count, effect_count, source_count,
                render_mode
            )

            if estimated <= budget:
                return tile_size

        # Fallback to minimum
        return min_tile_size

    def get_memory_budget_mb(self) -> float:
        """Get current memory budget in MB.

        This is the maximum amount of GPU memory the pool is allowed to use.

        Thread contract (AUD-8313): render-thread only — refreshes the VRAM
        budget via raw GL queries; other threads receive the cached budget.
        """
        self._update_vram_budget()
        return self.max_memory_bytes / (1024 * 1024)

    def get_available_budget_mb(self) -> float:
        """Get available memory budget (budget - current usage) in MB.

        This indicates how much memory is still available for new textures.
        Resident bytes (active + pooled + deferred) is the enforcement
        caliber used by _check_budget; reporting anything smaller here
        would overstate headroom for callers making admission decisions.

        Thread contract (AUD-8313): render-thread only — refreshes the VRAM
        budget via raw GL queries; other threads receive the cached budget.
        """
        self._update_vram_budget()
        available = self.max_memory_bytes - self.resident_memory_bytes()
        return max(0, available) / (1024 * 1024)
