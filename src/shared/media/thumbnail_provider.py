from __future__ import annotations

import os
import unicodedata
import cv2
import numpy as np
import hashlib
import logging
import math
import threading
from dataclasses import dataclass, field
from typing import Callable, Optional, List, Tuple

# Per-file lock to allow parallel extraction of different video files
# (OpenCV VideoCapture is not thread-safe, but different files can be read concurrently)
_cv_locks: dict[str, threading.Lock] = {}
_cv_locks_guard = threading.Lock()


def _get_file_lock(path: str) -> threading.Lock:
    """Get or create a per-file lock for VideoCapture access."""
    with _cv_locks_guard:
        if path not in _cv_locks:
            _cv_locks[path] = threading.Lock()
        return _cv_locks[path]


def _cleanup_file_lock(path: str) -> None:
    """Remove per-file lock after use to prevent unbounded growth."""
    with _cv_locks_guard:
        _cv_locks.pop(path, None)


# VideoCapture pool: reuse open capture handles to avoid repeated open/close overhead
_cap_pool_lock = threading.Lock()
_cap_pool: dict[str, list] = {}  # path -> [cv2.VideoCapture, ...]
_CAP_POOL_MAX_PER_FILE = 2
_CAP_POOL_MAX_TOTAL = 20


def _get_capture(path: str):
    """Get a reused VideoCapture from pool, or create a new one."""
    with _cap_pool_lock:
        if path in _cap_pool and _cap_pool[path]:
            cap = _cap_pool[path].pop()
            if cap.isOpened():
                return cap
            cap.release()  # Release stale handle
    cap = cv2.VideoCapture(path)
    return cap


def _return_capture(path: str, cap) -> None:
    """Return a VideoCapture to the pool for reuse."""
    if not cap.isOpened():
        cap.release()
        return
    with _cap_pool_lock:
        total = sum(len(v) for v in _cap_pool.values())
        if total >= _CAP_POOL_MAX_TOTAL:
            cap.release()
            # Clean up empty path entries to prevent unbounded dict growth
            if path in _cap_pool and not _cap_pool[path]:
                del _cap_pool[path]
            return
        if path not in _cap_pool:
            _cap_pool[path] = []
        if len(_cap_pool[path]) < _CAP_POOL_MAX_PER_FILE:
            _cap_pool[path].append(cap)
        else:
            cap.release()
            # Clean up empty path entries
            if not _cap_pool[path]:
                del _cap_pool[path]

logger = logging.getLogger(__name__)
from collections import OrderedDict
from PyQt6.QtCore import QObject, pyqtSignal, QRunnable, QThreadPool, QTimer, pyqtSlot, Qt, QThread
from PyQt6.QtGui import QPixmap, QImage
from PyQt6.QtWidgets import QApplication
from src.shared.io.file_io import FileIO


# ================================================================
# Thumbnail on-disk cache lookup helpers (AUD-35712/35713)
# ================================================================

def _global_thumbnail_cache_dir() -> str:
    """全局缩略图缓存目录（源码树内 .assetdeck/thumbnails，历史兼容）。"""
    return os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "..", ".assetdeck", "thumbnails"
    )


# {目录: ((mtime_ns, size), {NFC 名: 实际条目名})}——按目录 mtime 失效，
# 避免同一目录的重复缩略图请求每次都全列扫描 + 逐文件 NFC 规范化。
_nfc_index_cache: "OrderedDict[str, tuple[tuple[int, int], dict[str, str]]]" = OrderedDict()
_nfc_index_lock = threading.Lock()
_NFC_INDEX_MAX_DIRS = 64


def _nfc_index_for_dir(cache_dir: str) -> dict[str, str]:
    """目录条目的 NFC 规范化名索引（macOS NFD 文件名兼容查找）。

    返回 ``{nfc_name: 实际条目名}``；目录不可读返回空 dict。单次 listdir、
    每条目只规范化一次，结果按目录 ``(mtime_ns, size)`` 指纹缓存——
    目录内容增删会改变 mtime，缓存随之失效，正确性与全量扫描等价。
    """
    try:
        st = os.stat(cache_dir)
        fingerprint = (st.st_mtime_ns, st.st_size)
    except OSError:
        return {}
    with _nfc_index_lock:
        cached = _nfc_index_cache.get(cache_dir)
        if cached is not None and cached[0] == fingerprint:
            return cached[1]
    try:
        entries = os.listdir(cache_dir)
    except OSError:
        index: dict[str, str] = {}
    else:
        index = {}
        for entry in entries:
            index.setdefault(unicodedata.normalize('NFC', entry), entry)
    with _nfc_index_lock:
        if len(_nfc_index_cache) >= _NFC_INDEX_MAX_DIRS:
            _nfc_index_cache.popitem(last=False)
        _nfc_index_cache[cache_dir] = (fingerprint, index)
    return index


# AUD-35719: 已删除全仓零引用的死 DTO（worker 经 _ThumbnailSignalBridge
# 以 object 信号传递 QImage，不再需要独立结果类型）。

try:
    import rawpy
except ImportError:
    rawpy = None

try:
    from psd_tools import PSDImage
except ImportError:
    PSDImage = None


# ================================================================
# Signal bridge for main-thread dispatch
# ================================================================

class _ThumbnailSignalBridge(QObject):
    """Dispatches worker results to the main thread via queued signal connections.

    Workers call ``emit_*`` methods from any thread. Because the signal-slot
    connection crosses thread boundaries, PyQt6 automatically uses
    ``Qt.QueuedConnection``, ensuring the connected slots run on the main
    thread.  This guarantees that ``QPixmap.fromImage()`` is only called on
    the GUI thread.
    """

    thumbnail_done = pyqtSignal(str, object)       # path, QImage|None
    frame_thumbnail_done = pyqtSignal(str, int, object)  # path, frame_idx, QImage|None
    clip_thumbnails_done = pyqtSignal(str, object)       # request_key, ClipThumbnailSet


_bridge = _ThumbnailSignalBridge()


def _qobject_is_alive(obj: QObject | None) -> bool:
    """Return False when PyQt's Python wrapper points at a deleted QObject."""
    if obj is None:
        return False
    try:
        obj.objectName()
        return True
    except RuntimeError:
        return False


# AUD-35717：_bridge 重建互斥锁。_get_bridge 会被多个 worker 线程并发
# 调用，无锁 check-then-act 可能各建一个 bridge——晚到 worker 在新 bridge
# 上 emit 的结果不会投递到 provider 已连接的旧 bridge（缩略图静默丢失）。
_bridge_rebuild_lock = threading.Lock()


def _get_bridge() -> _ThumbnailSignalBridge:
    """Get a live thumbnail signal bridge, recreating it after Qt teardown.

    Tests and hot-reload paths can leave the module-level bridge wrapper
    pointing at a deleted C++ object.  Recreate it lazily so constructing a new
    provider or emitting a late worker result does not crash with
    "wrapped C/C++ object ... has been deleted".

    AUD-35717：重建全程持锁，保证全模块任意时刻至多存在一个活跃 bridge，
    provider 连接的 bridge 不会被并发重建脱离。
    """
    global _bridge
    with _bridge_rebuild_lock:
        if not _qobject_is_alive(_bridge):
            _bridge = _ThumbnailSignalBridge()
        return _bridge

class LRUCache:
    def __init__(self, capacity: int) -> None:
        self.cache = OrderedDict()
        self.capacity = capacity

    def get(self, key):
        if key not in self.cache:
            return None
        self.cache.move_to_end(key)
        return self.cache[key]

    def put(self, key, value) -> None:
        self.cache[key] = value
        self.cache.move_to_end(key)
        if len(self.cache) > self.capacity:
            self.cache.popitem(last=False)

    def remove(self, key) -> None:
        if key in self.cache:
            del self.cache[key]

    def clear(self) -> None:
        """Clear all cached items."""
        self.cache.clear()


# ================================================================
# Frame Thumbnail Cache Key
# ================================================================

@dataclass(frozen=True)
class FrameThumbnailKey:
    """Immutable cache key for video frame thumbnails.

    Includes ``mtime_ns`` so that a file replacement at the same path
    invalidates stale entries automatically.  ``asset_id`` distinguishes
    different assets that may share a path (e.g. re-imported media).

    Attributes:
        asset_id: Unique asset identifier (or ``""`` if unavailable).
        path: Absolute file path of the video.
        mtime_ns: File modification time in nanoseconds (``os.stat``).
        frame_index: Zero-based frame index within the video.
        target_height: Target thumbnail height in pixels.
    """

    asset_id: str
    path: str
    mtime_ns: int
    frame_index: int
    target_height: int


def _make_frame_key(
    asset_id: str | None,
    path: str,
    frame_index: int,
    target_height: int,
) -> tuple | FrameThumbnailKey:
    """Build a cache key, falling back to a plain tuple when ``asset_id``
    is not available (backward-compatible path).

    When ``asset_id`` is ``None`` the caller opted out of the new key
    scheme — we return ``(path, frame_index, target_height)`` so that
    existing callers continue to hit the cache unchanged.
    """
    if asset_id is None:
        return (path, frame_index, target_height)
    try:
        mtime_ns: int = os.stat(path).st_mtime_ns
    except OSError:
        mtime_ns = 0
    return FrameThumbnailKey(asset_id, path, mtime_ns, frame_index, target_height)


def _estimate_pixmap_bytes(pixmap: QPixmap) -> int:
    """Return an estimated memory footprint for *pixmap* in bytes.

    Assumes 4 bytes per pixel (ARGB32 / premultiplied ARGB).
    """
    if pixmap.isNull():
        return 0
    return pixmap.width() * pixmap.height() * 4


class MemoryAwareLRUCache:
    """LRU cache that tracks estimated memory usage and evicts entries
    when the cumulative size exceeds a configurable byte threshold.

    Falls back to item-count eviction when pixmap sizes are unavailable.
    """

    def __init__(
        self,
        max_items: int = 200,
        max_bytes: int = 500 * 1024 * 1024,  # 500 MB
    ) -> None:
        self._cache: OrderedDict = OrderedDict()
        self._sizes: dict = {}  # key -> estimated bytes
        self._total_bytes: int = 0
        self.max_items: int = max_items
        self.max_bytes: int = max_bytes

    def get(self, key) -> QPixmap | None:
        if key not in self._cache:
            return None
        self._cache.move_to_end(key)
        return self._cache[key]

    def put(self, key, value: QPixmap) -> None:
        # If key already present, subtract old size first
        if key in self._cache:
            old_size = self._sizes.pop(key, 0)
            self._total_bytes -= old_size
            del self._cache[key]

        estimated = _estimate_pixmap_bytes(value) if isinstance(value, QPixmap) else 0
        self._cache[key] = value
        self._sizes[key] = estimated
        self._total_bytes += estimated

        # Evict oldest entries while over budget (byte or item count)
        while self._cache and (
            self._total_bytes > self.max_bytes
            or len(self._cache) > self.max_items
        ):
            evict_key, _ = self._cache.popitem(last=False)
            evict_size = self._sizes.pop(evict_key, 0)
            self._total_bytes -= evict_size

    def remove(self, key) -> None:
        if key in self._cache:
            del self._cache[key]
            evict_size = self._sizes.pop(key, 0)
            self._total_bytes -= evict_size

    def clear(self) -> None:
        """Clear all cached items."""
        self._cache.clear()
        self._sizes.clear()
        self._total_bytes = 0

    @property
    def total_bytes(self) -> int:
        """Current estimated memory usage in bytes."""
        return self._total_bytes


# ================================================================
# Clip Thumbnail Data Classes (Phase 1.1)
# ================================================================

@dataclass
class ClipThumbnailRequest:
    """
    Request for generating a set of thumbnails for a clip.

    参考 Kdenlive ClipThumbs.qml 的取帧策略：
    - 按像素计算需要多少缩略图
    - 每个缩略图覆盖固定的像素宽度（最小 40px）

    Attributes:
        asset_id: Asset ID for cache lookup
        asset_path: Video file path
        clip_width_px: Current clip display width in pixels
        source_start: Clip's source_in frame number
        source_duration: Clip's source frame count
        pixels_per_frame: Pixels per frame (zoom scale)
        thumbnail_height: Target height for thumbnails
    """
    asset_id: str
    asset_path: str
    clip_width_px: int
    source_start: int
    source_duration: int
    pixels_per_frame: float
    thumbnail_height: int = 48
    display_mode: str = "filmstrip"

    # A filmstrip only needs one source image per visible thumbnail cell.  The
    # bucket keeps cache churn low while zooming, and the cap puts a strict
    # upper bound on background decode and memory use.
    # Timeline zoom can make one long clip thousands of pixels wide.  Keeping
    # this bounded at 24 avoids a burst of random video seeks monopolising
    # disk/decoder resources just after a zoom gesture, while still filling a
    # typical desktop viewport with one frame per filmstrip cell.
    MAX_FILMSTRIP_FRAMES = 24
    FRAME_COUNT_BUCKET = 4

    def thumbnail_frame_count(self) -> int:
        """Return a display-density-aware number of source frames to decode."""
        if self.source_duration <= 0:
            return 1

        # Match decoding work to the timeline's LOD.  Previously a 40px clip
        # still decoded a four-frame filmstrip even though the UI can only show
        # one frame; start/end clips likewise decoded unnecessary intermediates.
        if self.display_mode == "single":
            return 1
        if self.display_mode == "start_end":
            return min(2, self.source_duration)

        # A decoded thumbnail should approximately fill one filmstrip cell.
        # This removes the former duration-only cap of 20 frames, which made
        # a long clip turn into large blurred blocks after zooming in.
        thumbnail_width = max(48, int(round(self.thumbnail_height * 16.0 / 9.0)))
        visible_cells = max(1, math.ceil(self.clip_width_px / thumbnail_width))
        bucketed_cells = math.ceil(visible_cells / self.FRAME_COUNT_BUCKET) * self.FRAME_COUNT_BUCKET
        return max(1, min(self.source_duration, self.MAX_FILMSTRIP_FRAMES, bucketed_cells))

    def compute_thumbnail_frames(self) -> List[int]:
        """
        Compute which frames to extract for thumbnails.

        Matches thumbnail count to displayed filmstrip cells, using a bounded
        four-cell density bucket to prevent a request on every tiny zoom step.

        Returns:
            List of frame indices to extract
        """
        # Match decoded density to the display width instead of the source
        # duration alone.  The same source range therefore gains detail after
        # a zoom-in and reuses an appropriate cached quality level.
        if self.source_duration <= 0:
            return [self.source_start]

        num_thumbs = self.thumbnail_frame_count()
        if num_thumbs == 1:
            return [self.source_start]

        # Calculate frame indices evenly distributed across source range
        frames = []
        for i in range(num_thumbs):
            frame_offset = i * (self.source_duration - 1) / (num_thumbs - 1)
            frame_n = self.source_start + int(frame_offset)
            frames.append(min(frame_n, self.source_start + self.source_duration - 1))

        return frames

    def get_cache_key(self) -> str:
        """
        Generate unique cache key for this request.

        The density bucket and height are included so a low-resolution set is
        never stretched across a larger clip or taller track after zooming.
        Small zoom changes within the same bucket still reuse the cache.
        """
        return (
            f"{self.asset_id}_{self.source_start}_{self.source_duration}"
            f"_h{self.thumbnail_height}_m{self.display_mode}"
            f"_n{self.thumbnail_frame_count()}"
        )


@dataclass
class ClipThumbnailSet:
    """
    A set of thumbnails for a clip.

    Contains frame-indexed QImage thumbnails.

    Workers store QImage (thread-safe).  The main-thread callback converts
    to QPixmap when emitting the public signal.

    Attributes:
        request_key: Cache key from ClipThumbnailRequest
        frames: List of (frame_index, QImage) tuples
        thumbnail_height: Height of each thumbnail
        is_complete: Whether all frames were successfully extracted
    """
    request_key: str
    frames: List[Tuple[int, QImage]] = field(default_factory=list)
    thumbnail_height: int = 48
    is_complete: bool = True
    # Generation of the background request that produced this result. A
    # stale worker must not overwrite a newer request after a timeline cut.
    generation: int = 0

    @property
    def num_frames(self) -> int:
        """Number of thumbnails in the set."""
        return len(self.frames)


@dataclass
class _PendingClipPixmapConversion:
    """Incremental main-thread conversion state for one clip thumbnail set."""
    request_key: str
    thumbnail_height: int
    is_complete: bool
    frames: List[Tuple[int, QImage]]
    generation: int = 0
    pixmap_frames: List[Tuple[int, QPixmap]] = field(default_factory=list)
    next_index: int = 0


# ================================================================
# Clip Thumbnail Cache (Phase 1.1)
# ================================================================

class ClipThumbnailCache:
    """
    LRU cache for clip thumbnail sets.

    参考 Kdenlive ThumbnailCache:
    - 使用 volatile 内存缓存（LRU）
    - 容量限制防止内存爆炸
    - 按 request_key 缓存整组缩略图

    Singleton pattern for global access.
    """

    _instance: Optional[ClipThumbnailCache] = None
    # AUD-35718：单例 check-then-act 互斥（__new__ 可从任意线程进入）
    _instance_lock = threading.Lock()

    def __new__(cls) -> ClipThumbnailCache:
        if cls._instance is None:
            with cls._instance_lock:
                if cls._instance is None:
                    inst = super().__new__(cls)
                    inst._cache: OrderedDict[str, ClipThumbnailSet] = OrderedDict()
                    inst._pending: dict = {}  # Track pending requests
                    # A set may hold up to 64 QPixmaps at high zoom. Limit the number
                    # of sets so repeated zoom levels cannot consume unbounded RAM.
                    inst._max_size = 128
                    cls._instance = inst
        return cls._instance

    def get(self, request_key: str) -> Optional[ClipThumbnailSet]:
        """
        Get cached thumbnail set by key.

        Args:
            request_key: Cache key from ClipThumbnailRequest

        Returns:
            ClipThumbnailSet if cached, None otherwise
        """
        if request_key in self._cache:
            self._cache.move_to_end(request_key)
            return self._cache[request_key]
        return None

    def get_compatible(self, request: ClipThumbnailRequest) -> Optional[ClipThumbnailSet]:
        """Return the densest cached set compatible with *request*.

        A zoom-in changes the density bucket and therefore the exact cache
        key.  Keeping the previous set on screen while the denser set is
        decoded prevents the filmstrip from flashing blank after every zoom.
        The caller still requests the exact set in the background.
        """
        prefix = (
            f"{request.asset_id}_{request.source_start}_{request.source_duration}"
            f"_h{request.thumbnail_height}_m{request.display_mode}_n"
        )
        compatible_sets = [
            thumbnail_set
            for key, thumbnail_set in self._cache.items()
            if key.startswith(prefix) and thumbnail_set.num_frames > 0
        ]
        if not compatible_sets:
            return None
        return max(compatible_sets, key=lambda thumbnail_set: thumbnail_set.num_frames)

    def put(self, request_key: str, thumbnail_set: ClipThumbnailSet) -> None:
        """
        Add thumbnail set to cache.

        Evicts oldest entry if cache is full.

        Args:
            request_key: Cache key
            thumbnail_set: ClipThumbnailSet to cache
        """
        # Remove if exists (will be re-added at end)
        if request_key in self._cache:
            del self._cache[request_key]

        # Evict oldest if full
        while len(self._cache) >= self._max_size:
            self._cache.popitem(last=False)

        self._cache[request_key] = thumbnail_set
        logger.debug(f"Cached clip thumbnails for {request_key}, cache size: {len(self._cache)}")

    def is_pending(self, request_key: str) -> bool:
        """Check if request is pending."""
        return request_key in self._pending

    def set_pending(self, request_key: str) -> None:
        """Mark request as pending."""
        self._pending[request_key] = True

    def clear_pending(self, request_key: str) -> None:
        """Clear pending status."""
        self._pending.pop(request_key, None)

    def remove(self, request_key: str) -> None:
        """Remove cached thumbnail set."""
        if request_key in self._cache:
            del self._cache[request_key]
        self._pending.pop(request_key, None)

    def clear(self) -> None:
        """Clear all cached thumbnail sets."""
        self._cache.clear()
        self._pending.clear()
        logger.debug("Clip thumbnail cache cleared")

    def get_cache_size(self) -> int:
        """Get current cache size."""
        return len(self._cache)


class ThumbnailRequest(QRunnable):
    def __init__(self, path, callback, icon_size) -> None:
        super().__init__()
        self.path = path
        self.callback = callback
        self.icon_size = icon_size

    def _get_cache_path(self, path):
        """Returns the cache path for a given file.

        支持两种缓存格式（确保与 file_explorer 兼容）：
        策略1 - MD5 哈希格式：{hash}.webp
        策略2 - 原始文件名格式：filename.ext.webp

        查找顺序：先尝试原始文件名格式（精确名直接 stat 命中；未命中才
        走按目录 mtime 缓存的 NFC 索引单次查找，兼容 macOS NFD 文件名），
        再回退哈希格式
        写入时：优先使用哈希格式
        """
        # 统一 NFC 规范化并统一斜杠，解决 macOS NFD 编码问题
        norm_path = unicodedata.normalize('NFC', os.path.abspath(path).replace('\\', '/'))

        # 获取源文件目录
        source_dir = os.path.dirname(os.path.abspath(path))
        filename = os.path.basename(path)

        # 确定缓存目录
        local_cache = os.path.join(source_dir, '.cache')

        # 检查缓存目录是否存在且可写（AUD-35713：目录已存在时改用
        # os.access 判定可写性——旧实现每次请求都创建/删除 .write_test
        # 探针文件，既加 I/O 又弄脏目录 mtime，使 NFC 索引缓存永远失效；
        # 目录缺失时仍做一次真实写探针再复用）
        cache_dir = local_cache
        writable = False
        try:
            if os.path.isdir(local_cache):
                writable = os.access(local_cache, os.W_OK)
            else:
                os.makedirs(local_cache, exist_ok=True)
                test_file = os.path.join(local_cache, '.write_test')
                with open(test_file, 'w') as f: f.write('ok')
                os.remove(test_file)
                writable = True
        except OSError:
            writable = False
        if not writable:
            # 源目录不可写，使用平台规范缓存目录
            # [R2-5] 此前写源码树内 .assetdeck/——打包后落只读安装目录静默失败
            from src.shared.config.config_manager import ConfigManager
            cache_dir = os.path.join(ConfigManager.get_platform_cache_dir(), "thumbnails")
            os.makedirs(cache_dir, exist_ok=True)

        # ========== 读取时：先尝试原始文件名格式（与 file_explorer 兼容）==========
        # file_explorer 使用: filename + suffix 格式，如 photo.jpg.webp
        # 尝试多种尺寸后缀，优先查找 Large 尺寸以获取最佳质量
        filename_variants = [
            filename + '_lg.webp',        # large: photo.jpg_lg.webp (优先)
            filename + '.webp',           # small: photo.jpg.webp
            filename + '_preview.webp',   # preview: photo.jpg_preview.webp
        ]

        # AUD-35713：旧实现每次请求对缓存目录做 O(变体数×文件数) 全列扫描
        # 并逐文件 NFC 规范化。快路径直接按精确名 stat（多数请求零目录
        # 扫描）；仅当全部未命中才走 NFC 索引（单次 listdir、每条目只
        # 规范化一次，且按目录 mtime 缓存复用）。
        for cache_root in (local_cache, _global_thumbnail_cache_dir()):
            for variant in filename_variants:
                candidate = os.path.join(cache_root, variant)
                if os.path.isfile(candidate):
                    return candidate
            variant_index = _nfc_index_for_dir(cache_root)
            for variant in filename_variants:
                hit = variant_index.get(unicodedata.normalize('NFC', variant))
                if hit is not None:
                    return os.path.join(cache_root, hit)

        # ========== 写入时：使用哈希格式 ==========
        hash_name = hashlib.md5(f"{norm_path}_v3".encode('utf-8')).hexdigest()
        return os.path.join(cache_dir, f"{hash_name}.webp")

    @pyqtSlot()
    def run(self):
        try:
            # 1. Try to load from .cache first (generated when saving .topos)
            cache_path = self._get_cache_path(self.path)
            img_bgr = None
            if os.path.exists(cache_path):
                # Use np.fromfile + imdecode to handle Unicode paths on Windows
                img_data = np.fromfile(cache_path, dtype=np.uint8)
                img_bgr = cv2.imdecode(img_data, cv2.IMREAD_COLOR)
            
            # 2. If no cache, load original and resize
            if img_bgr is None:
                # Fast load for thumbnail
                img_bgr = self._load_fast_thumbnail(self.path)
            
            if img_bgr is not None:
                # Orientation is already handled in _load_fast_thumbnail for relevant formats
                # or not needed for cached images (which were saved with correct orientation)

                # Convert BGR to RGB
                img_rgb = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB)
                h, w, ch = img_rgb.shape
                bytes_per_line = ch * w
                # .copy() decouples the QImage from the numpy buffer so it
                # survives after the numpy array goes out of scope.
                q_img = QImage(
                    img_rgb.data.tobytes(), w, h, bytes_per_line,
                    QImage.Format.Format_RGB888,
                ).copy()

                # High-quality scaling: keep a reasonable size so the image
                # stays sharp even when zooming in UI.  QImage.scaled() is
                # safe to call off the GUI thread (unlike QPixmap).
                render_size = max(self.icon_size, 512)
                scaled_img = q_img.scaled(
                    render_size, render_size,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
                _get_bridge().thumbnail_done.emit(self.path, scaled_img)
            else:
                _get_bridge().thumbnail_done.emit(self.path, None)
        except Exception as e:
            logger.error(f"loading thumbnail for {self.path}: {e}")
            _get_bridge().thumbnail_done.emit(self.path, None)

    def _load_fast_thumbnail(self, path):
        """Loads a small version of the image as quickly as possible."""
        ext = os.path.splitext(path)[1].lower()
        try:
            # Video files: extract a representative frame via VideoCapture.
            # cv2.imdecode (used by the generic branch below) cannot parse
            # video containers (mp4/mov/mkv/...), so videos previously fell
            # through to a None result and rendered as a "…" placeholder in
            # the media pool. Jump to ~10% of the duration to avoid black
            # leader frames typical of the very first frame.
            from src.shared.media.format_registry import VIDEO_EXTENSIONS
            if ext in VIDEO_EXTENSIONS:
                return self._load_video_poster_frame(path)

            # M2-G2：.toos（Topos Image）——cv2.imdecode 无法解析 TPIM 容器，
            # 此前恒 None（媒体池显示占位符）。走原生解码（含浮点/RAW 域），
            # 借用文件浏览器插件的 RGB 转换（含 linear→sRGB 显示编码）。
            if ext == '.toos':
                return self._load_topos_image_thumbnail(path)

            # Professional formats support
            if ext in ['.arw', '.cr2', '.nef', '.dng', '.orf', '.raf'] and rawpy:
                with rawpy.imread(path) as raw:
                    # try to extract thumbnail first (much faster)
                    try:
                        thumb = raw.extract_thumb()
                        if thumb.format == rawpy.ThumbFormat.JPEG:
                            img = cv2.imdecode(np.frombuffer(thumb.data, np.uint8), cv2.IMREAD_COLOR)
                        else:
                            # AUD-35711：BITMAP 缩略图是 RGB ndarray
                            # （DNG/部分 NEF 常见），必须转 BGR——与
                            # JPEG 分支（imdecode 产出 BGR）和 postprocess
                            # 分支（显式转 BGR）统一出口色彩空间；漏转
                            # 会让下游 BGR2RGB 再交换一次，红蓝互换。
                            img = cv2.cvtColor(thumb.data, cv2.COLOR_RGB2BGR)
                    except (rawpy.LibRawError, ValueError, TypeError, OSError) as exc:
                        # AUD-35712：裸 except 会吞掉任意异常——只捕获缩略图
                        # 提取可能的失败类型，其余异常照常暴露给外层处理器。
                        logger.debug("RAW embedded thumbnail unavailable (%s), "
                                     "falling back to postprocess: %s", ext, exc)
                        # Fallback to half-size postprocess
                        img = raw.postprocess(use_camera_wb=True, half_size=True, no_auto_bright=True, bright=1.0)
                        img = cv2.cvtColor(img, cv2.COLOR_RGB2BGR)
            elif ext == '.psd' and PSDImage:
                psd = PSDImage.open(path)
                img = np.array(psd.composite())
                img = cv2.cvtColor(img, cv2.COLOR_RGBA2BGR)
            elif ext == '.exr' or ext == '.hdr':
                # Use np.fromfile + imdecode to handle Unicode paths on Windows
                img_data = np.fromfile(path, dtype=np.uint8)
                img = cv2.imdecode(img_data, cv2.IMREAD_ANYCOLOR | cv2.IMREAD_ANYDEPTH)
                if img is not None and img.dtype == np.float32:
                    # Simple tone mapping for preview
                    img = np.clip(img * 255, 0, 255).astype(np.uint8)
            elif ext in ['.jpg', '.jpeg']:
                # Use np.fromfile + imdecode to handle Unicode paths on Windows
                img_data = np.fromfile(path, dtype=np.uint8)
                img = cv2.imdecode(img_data, cv2.IMREAD_COLOR)
            else:
                # Use np.fromfile + imdecode to handle Unicode paths on Windows
                img_data = np.fromfile(path, dtype=np.uint8)
                img = cv2.imdecode(img_data, cv2.IMREAD_COLOR)
            
            # Standard OpenCV imread does NOT handle EXIF orientation
            if ext in ['.jpg', '.jpeg', '.png', '.tif', '.tiff', '.webp', '.heic', '.heif']:
                if img is not None:
                    img = FileIO._apply_exif_orientation(img, path)

            # Resize if still too large
            if img is not None:
                h, w = img.shape[:2]
                target_size = 1024 # Standard high-res thumbnail source
                scale = target_size / max(h, w)
                if scale < 1:
                    return cv2.resize(img, (int(w*scale), int(h*scale)), interpolation=cv2.INTER_AREA)
                return img
            return None
        except Exception as e:
            logger.error(f"load failed for {path}: {e}")
            return None

    def _load_topos_image_thumbnail(self, path):
        """M2-G2：解码 .toos 静图为 BGR 缩略图（媒体池面板）。

        复用文件浏览器插件的 _decode_rgb（整数/浮点/HALF 域全覆盖，浮点
        走 linear→sRGB 显示编码）；失败返回 None（面板显示占位符）。
        """
        try:
            from external.file_explorer.plugins.topos_image.topos_image_media import (
                _decode_rgb,
            )

            rgb, _info = _decode_rgb(path, target_size=(1024, 1024))
        except Exception as e:
            logger.warning("load .toos thumbnail failed for %s: %s", path, e)
            return None
        if rgb is None:
            return None
        if rgb.ndim == 2:
            rgb = np.stack((rgb,) * 3, axis=-1)
        if rgb.shape[-1] == 4:
            bgr = cv2.cvtColor(rgb, cv2.COLOR_RGBA2BGR)
        else:
            bgr = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
        h, w = bgr.shape[:2]
        scale = 1024 / max(h, w)
        if scale < 1:
            bgr = cv2.resize(bgr, (int(w * scale), int(h * scale)),
                             interpolation=cv2.INTER_AREA)
        return np.ascontiguousarray(bgr)

    def _load_video_poster_frame(self, path):
        """Extract a representative BGR frame from a video for thumbnailing.

        Seeks to ~10% of the total frame count (avoids pure-black leader
        frames common at the very start of clips) and decodes one frame.
        Uses the shared VideoCapture pool for efficiency. Returns a BGR
        numpy array or None on failure.
        """
        try:
            # Topos 私有编码（TPIC）没有 OpenCV/FFmpeg 解码器 —— 经 Topos
            # 后端抽帧（RGB → BGR 对齐本函数的返回约定）。
            from src.shared.media.topos_source import is_topos_movie

            if is_topos_movie(path):
                from src.shared.media.topos_source import read_topos_frame_rgb

                probe_src = None
                decode_target = None
                try:
                    from src.shared.media.topos_source import ToposMediaSource

                    probe_src = ToposMediaSource(path)
                    probe_src.open()
                    meta = probe_src.probe()
                    total = int(meta.total_frames or 0)
                    target = int(total * 0.1) if total > 0 else 0
                    # G8：解码期缩放（native reduced/AUTO_2K）——海报帧
                    # 只需 ≤1024px，4K/8K 源不再全帧解码后 CPU resize
                    mw, mh = int(meta.width or 0), int(meta.height or 0)
                    if mw > 0 and mh > 0 and max(mw, mh) > 1024:
                        scale = 1024 / max(mw, mh)
                        decode_target = (max(1, int(mw * scale)),
                                         max(1, int(mh * scale)))
                except Exception:
                    return None
                finally:
                    if probe_src is not None:
                        probe_src.close()
                rgb = read_topos_frame_rgb(path, target,
                                           target_size=decode_target)
                if rgb is None:
                    return None
                frame = rgb[..., ::-1]  # RGB → BGR
                h, w = frame.shape[:2]
                scale = 1024 / max(h, w)
                if scale < 1:
                    frame = cv2.resize(
                        frame, (int(w * scale), int(h * scale)),
                        interpolation=cv2.INTER_AREA,
                    )
                return np.ascontiguousarray(frame)

            file_lock = _get_file_lock(path)
            with file_lock:
                cap = _get_capture(path)
                try:
                    if not cap.isOpened():
                        return None
                    # Determine a representative frame index (10% in,
                    # clamped).
                    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
                    target = int(total * 0.1) if total > 0 else 0
                    if target > 0:
                        cap.set(cv2.CAP_PROP_POS_FRAMES, target)
                    ret, frame = cap.read()
                finally:
                    _return_capture(path, cap)
                    # AUD-35714：用毕清理 per-file 锁（原函数定义后从未被
                    # 调用，_cv_locks 每路径一个 Lock 只增不减）。在 finally
                    # 中调用覆盖所有退出路径；本线程仍持有锁直至 with 退出，
                    # 字典摘除后并发者会新建等价锁，互斥语义不受影响。
                    _cleanup_file_lock(path)
            if not ret or frame is None:
                return None
            # Downscale large frames to keep the thumbnail cache light.
            h, w = frame.shape[:2]
            target_size = 1024
            scale = target_size / max(h, w)
            if scale < 1:
                frame = cv2.resize(
                    frame, (int(w * scale), int(h * scale)),
                    interpolation=cv2.INTER_AREA,
                )
            return frame
        except Exception as e:
            logger.error(f"video poster frame failed for {path}: {e}")
            return None

class ThumbnailProvider(QObject):
    thumbnail_ready = pyqtSignal(str, QPixmap)
    frame_thumbnail_ready = pyqtSignal(str, int, QPixmap)  # path, frame_index, pixmap
    # Phase 1.1: Clip thumbnail set signal
    clip_thumbnail_set_ready = pyqtSignal(str, object)  # request_key, ClipThumbnailSet

    _instance = None
    _instance_lock = threading.Lock()
    PIXMAP_CONVERSIONS_PER_GUI_TICK = 4

    @classmethod
    def instance(cls) -> "ThumbnailProvider":
        """全局单例（AUD-35718：加锁 + 主线程断言）。

        __init__ 创建 QTimer/QThreadPool，要求对象创建于 GUI 线程；
        模块级便捷函数 request_clip_thumbnails_async 可从任意线程进入，
        若首次调用发生在非 GUI 线程会产生 Qt 告警/异常。无 QApplication
        时（纯逻辑测试/极早启动）不设限，保持既有可用性。
        """
        if cls._instance is None:
            with cls._instance_lock:
                if cls._instance is None:
                    app = QApplication.instance()
                    if app is not None and QThread.currentThread() is not app.thread():
                        raise RuntimeError(
                            "ThumbnailProvider.instance() must first be called "
                            "on the GUI thread (QTimer/QThreadPool affinity); "
                            f"called from {QThread.currentThread()}"
                        )
                    cls._instance = ThumbnailProvider()
        return cls._instance

    def __init__(self) -> None:
        super().__init__()
        # Thumbnail extraction must never occupy the shared worker pool used
        # by playback, import, and rendering. One dedicated, low-concurrency
        # worker keeps UI and video decoding responsive even for 4K media.
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(1)
        self.cache = LRUCache(2000)  # Cache 2000 basic thumbnails
        self.pending_requests = set()
        self.pending_frame_requests: set = set()  # (path, frame_index) tuples
        self.frame_cache = MemoryAwareLRUCache(
            max_items=200, max_bytes=500 * 1024 * 1024
        )  # Memory-aware frame thumbnail cache
        # Per-frame pixmap LRU cache keyed by FrameThumbnailKey
        self._frame_pixmap_cache: MemoryAwareLRUCache = MemoryAwareLRUCache(
            max_items=200, max_bytes=500 * 1024 * 1024
        )  # Memory-aware per-frame pixmap cache
        self._pending_frame_pixmap_requests: set = set()  # Track pending keys
        self._playback_active: bool = False
        self._thumbnail_generation: int = 0
        self._pending_clip_request_heights: dict[str, int] = {}
        self._clip_conversion_queue: OrderedDict[str, _PendingClipPixmapConversion] = OrderedDict()
        self._clip_conversion_timer = QTimer(self)
        self._clip_conversion_timer.setSingleShot(True)
        self._clip_conversion_timer.timeout.connect(self._process_clip_thumbnail_conversions)
        # Minimum clip width for thumbnail generation; narrower clips skip thumbnails
        self.MIN_CLIP_WIDTH_FOR_THUMBNAIL = 30
        # Phase 1.1: Clip thumbnail cache and callbacks
        self._clip_thumbnail_callbacks: dict = {}  # request_key -> [callbacks]

        # Connect signal bridge to main-thread callbacks.
        # Because _bridge lives on the main thread and workers emit from
        # worker threads, PyQt6 uses QueuedConnection automatically, ensuring
        # QPixmap.fromImage() is only called on the GUI thread.
        bridge = _get_bridge()
        bridge.thumbnail_done.connect(self._on_thumbnail_loaded)
        bridge.frame_thumbnail_done.connect(self._on_frame_thumbnail_loaded)
        bridge.clip_thumbnails_done.connect(self._on_clip_thumbnails_loaded)

    def _request_cancelled(self, generation: int) -> bool:
        """Return whether a background request became stale or playback won."""
        return self._playback_active or generation != self._thumbnail_generation

    def set_playback_active(self, active: bool) -> None:
        """Suspend, cancel, and later resume non-essential thumbnail work."""
        if self._playback_active == active:
            return

        self._playback_active = active
        if not active:
            return

        self._thumbnail_generation += 1
        # Remove queued work immediately. Running workers see the generation
        # change between decoded frames and stop cooperatively.
        self.pool.clear()
        self.pending_requests.clear()
        self.pending_frame_requests.clear()
        self._pending_frame_pixmap_requests.clear()

        cache = ClipThumbnailCache()
        cancelled_requests = dict(self._pending_clip_request_heights)
        for request_key, pending in self._clip_conversion_queue.items():
            cancelled_requests.setdefault(request_key, pending.thumbnail_height)
        self._pending_clip_request_heights.clear()
        self._clip_conversion_queue.clear()
        self._clip_conversion_timer.stop()
        for request_key, height in cancelled_requests.items():
            self._finalize_clip_thumbnail_set(
                request_key,
                ClipThumbnailSet(
                    request_key=request_key,
                    frames=[],
                    thumbnail_height=height,
                    is_complete=False,
                    generation=self._thumbnail_generation,
                ),
                cache=cache,
            )

    def invalidate_pending_work(self) -> None:
        """Invalidate thumbnail jobs after a timeline model replacement."""
        self._thumbnail_generation += 1
        # This provider is shared by the timeline and the media pool.  A
        # timeline replacement must not clear the pool-wide ordinary-image
        # queues: those requests have different owners and would otherwise
        # remain permanently marked as pending.  Clip workers already running
        # observe the generation change and stop cooperatively; queued clip
        # workers will do the same when they start.

        cache = ClipThumbnailCache()
        cancelled_requests = dict(self._pending_clip_request_heights)
        for request_key, pending in self._clip_conversion_queue.items():
            cancelled_requests.setdefault(request_key, pending.thumbnail_height)
        self._pending_clip_request_heights.clear()
        self._clip_conversion_queue.clear()
        self._clip_conversion_timer.stop()
        for request_key, height in cancelled_requests.items():
            self._finalize_clip_thumbnail_set(
                request_key,
                ClipThumbnailSet(
                    request_key=request_key,
                    frames=[],
                    thumbnail_height=height,
                    is_complete=False,
                    generation=self._thumbnail_generation,
                ),
                cache=cache,
            )

    @property
    def is_thumbnail_work_suspended(self) -> bool:
        """Whether timeline thumbnail extraction is temporarily suspended.

        Timeline paints can occur while playback, scrubbing, or a zoom
        transition has paused background decoding.  Callers must not retain a
        local "pending" flag in that interval: no provider request will have
        been queued to clear it later.
        """
        return self._playback_active

    def get_thumbnail(self, path, icon_size=128):
        if self._playback_active:
            return None
        # 虚拟素材（topos:// scheme）没有磁盘文件，无法提取缩略图 —— 提前
        # 返回，避免 worker 抛 FileNotFoundError 并刷日志。这些素材的占位
        # 图由调用方（media_pool_panel）按 media_type 自行绘制。
        if isinstance(path, str) and path.startswith("topos://"):
            return None
        # Check cache
        cached = self.cache.get(path)
        if cached:
            return cached

        if path in self.pending_requests:
            return None

        # Request background load
        self.pending_requests.add(path)
        request = ThumbnailRequest(path, self._on_thumbnail_loaded, icon_size)
        self.pool.start(request)
        return None

    def get_frame_thumbnail(
        self,
        path: str,
        frame_index: int,
        total_frames: int,
        icon_size: int = 256,
        *,
        asset_id: str | None = None,
    ) -> None:
        """
        Request a specific frame thumbnail from a video file.

        When ``asset_id`` is provided the cache key is a
        :class:`FrameThumbnailKey` that includes the file's ``mtime_ns``,
        so a file replacement at the same path automatically invalidates
        stale entries.  When ``asset_id`` is ``None`` (the default) a
        plain tuple key ``(path, frame_index, icon_size)`` is used for
        backward compatibility.

        Args:
            path: Video file path
            frame_index: Zero-based frame index to extract
            total_frames: Total number of frames in the video
            icon_size: Desired thumbnail size
            asset_id: Optional asset identifier for improved cache key
        """
        if self._playback_active:
            return

        frame_pixmap_key = _make_frame_key(asset_id, path, frame_index, icon_size)

        # Check the per-frame pixmap cache first
        cached = self._frame_pixmap_cache.get(frame_pixmap_key)
        if cached:
            self.frame_thumbnail_ready.emit(path, frame_index, cached)
            return

        # Also check legacy frame cache for backwards compatibility
        legacy_key = (path, frame_index)
        legacy_cached = self.frame_cache.get(legacy_key)
        if legacy_cached:
            self.frame_thumbnail_ready.emit(path, frame_index, legacy_cached)
            return

        # Check if request is already pending
        if frame_pixmap_key in self._pending_frame_pixmap_requests:
            return
        if legacy_key in self.pending_frame_requests:
            return

        self.pending_frame_requests.add(legacy_key)
        self._pending_frame_pixmap_requests.add(frame_pixmap_key)
        request = FrameThumbnailRequest(
            path, frame_index, total_frames, icon_size,
            self._on_frame_thumbnail_loaded,
            cancelled=lambda generation=self._thumbnail_generation: self._request_cancelled(generation),
        )
        self.pool.start(request, -10)

    def get_cached_thumbnail(self, path: str, icon_size: int = 128) -> QPixmap | None:
        """
        Get cached thumbnail without triggering async load.

        This is useful for drag operations where we need immediate preview
        but don't want to trigger background loading.

        Args:
            path: File path to get thumbnail for
            icon_size: Desired size (for future scaling, currently returns cached size)

        Returns:
            QPixmap if cached, None otherwise
        """
        return self.cache.get(path)

    def _on_thumbnail_loaded(self, path: str, image: QImage | None) -> None:
        """Main-thread callback: converts QImage to QPixmap and caches it."""
        if path in self.pending_requests:
            self.pending_requests.remove(path)
        if self._playback_active:
            return
        if image:
            pixmap = QPixmap.fromImage(image)
            self.cache.put(path, pixmap)
            self.thumbnail_ready.emit(path, pixmap)
        else:
            # Handle failure - maybe a default icon?
            pass

    def _on_frame_thumbnail_loaded(self, path: str, frame_index: int, image: QImage | None) -> None:
        """Main-thread callback: converts QImage to QPixmap and caches it."""
        cache_key = (path, frame_index)
        if cache_key in self.pending_frame_requests:
            self.pending_frame_requests.discard(cache_key)
        if self._playback_active:
            return

        if image:
            pixmap = QPixmap.fromImage(image)
            # Store in legacy cache
            self.frame_cache.put(cache_key, pixmap)
            # Store in per-frame pixmap cache for each pending key that
            # matches (path, frame_index).  This covers both FrameThumbnailKey
            # entries (new) and plain tuple entries (legacy).
            matching = [
                k for k in self._pending_frame_pixmap_requests
                if self._key_matches_path_frame(k, path, frame_index)
            ]
            for k in matching:
                self._frame_pixmap_cache.put(k, pixmap)
                self._pending_frame_pixmap_requests.discard(k)

            self.frame_thumbnail_ready.emit(path, frame_index, pixmap)
        else:
            # Clean up pending entries on failure
            to_remove = [
                k for k in self._pending_frame_pixmap_requests
                if self._key_matches_path_frame(k, path, frame_index)
            ]
            for k in to_remove:
                self._pending_frame_pixmap_requests.discard(k)

    @staticmethod
    def _key_matches_path_frame(key: object, path: str, frame_index: int) -> bool:
        """Return True if *key* refers to the given ``path`` and ``frame_index``.

        Handles both :class:`FrameThumbnailKey` instances and legacy tuples.
        """
        if isinstance(key, FrameThumbnailKey):
            return key.path == path and key.frame_index == frame_index
        # Legacy tuple: (path, frame_index, ...) or (path, frame_index)
        if isinstance(key, tuple) and len(key) >= 2:
            return key[0] == path and key[1] == frame_index
        return False

    # ================================================================
    # Phase 1.1: Clip Thumbnail Methods
    # ================================================================

    def request_clip_thumbnails(
        self,
        request: ClipThumbnailRequest,
        callback: Optional[Callable] = None
    ) -> None:
        """
        Request a set of thumbnails for a clip asynchronously.

        参考 Kdenlive ClipThumbs.qml:
        - 按需生成 + 惰性加载模式
        - 使用 QThreadPool 异步执行
        - 结果缓存到 ClipThumbnailCache

        Args:
            request: ClipThumbnailRequest with frame parameters
            callback: Optional callback when thumbnails are ready
                Signature: callback(request_key: str, thumbnail_set: ClipThumbnailSet)
        """
        cache = ClipThumbnailCache()
        request_key = request.get_cache_key()

        if self._playback_active:
            return

        # Skip thumbnail request if clip is too narrow
        if request.clip_width_px < self.MIN_CLIP_WIDTH_FOR_THUMBNAIL:
            logger.debug(f"Skipping thumbnails for narrow clip ({request.clip_width_px}px < {self.MIN_CLIP_WIDTH_FOR_THUMBNAIL}px)")
            return

        # Check if already cached
        cached = cache.get(request_key)
        if cached:
            if callback:
                callback(request_key, cached)
            # Also emit signal for general listeners
            self.clip_thumbnail_set_ready.emit(request_key, cached)
            return

        # Check if pending
        if cache.is_pending(request_key):
            # Add callback to wait list
            if callback:
                if request_key not in self._clip_thumbnail_callbacks:
                    self._clip_thumbnail_callbacks[request_key] = []
                self._clip_thumbnail_callbacks[request_key].append(callback)
            return

        # Start new extraction
        cache.set_pending(request_key)
        self._pending_clip_request_heights[request_key] = request.thumbnail_height

        if callback:
            self._clip_thumbnail_callbacks[request_key] = [callback]

        worker = ClipThumbnailWorker(
            request,
            self._on_clip_thumbnails_loaded,
            target_height=request.thumbnail_height,
            cancelled=lambda generation=self._thumbnail_generation: self._request_cancelled(generation),
            generation=self._thumbnail_generation,
        )
        self.pool.start(worker, -10)
        logger.debug(f"Started clip thumbnail extraction for {request_key}")

    def _on_clip_thumbnails_loaded(
        self,
        request_key: str,
        thumbnail_set: ClipThumbnailSet
    ) -> None:
        """Callback for clip thumbnail set loading.

        Runs on the main thread.  Converts the internally-stored QImage
        frames to QPixmap before caching and emitting the public signal.
        """
        if thumbnail_set.generation != self._thumbnail_generation:
            logger.debug("Discarding stale clip thumbnail result for %s", request_key)
            return
        cache = ClipThumbnailCache()
        self._pending_clip_request_heights.pop(request_key, None)

        # A worker can finish just before playback or scrubbing begins. Do not
        # let its queued GUI callback convert dozens of images into QPixmaps
        # while the user is trying to preview or drag the playhead.
        if self._playback_active:
            self._finalize_clip_thumbnail_set(
                request_key,
                ClipThumbnailSet(
                    request_key=request_key,
                    frames=[],
                    thumbnail_height=thumbnail_set.thumbnail_height,
                    is_complete=False,
                    generation=self._thumbnail_generation,
                ),
                cache=cache,
            )
            return

        if thumbnail_set.num_frames == 0:
            self._finalize_clip_thumbnail_set(
                request_key,
                ClipThumbnailSet(
                    request_key=thumbnail_set.request_key,
                    frames=[],
                    thumbnail_height=thumbnail_set.thumbnail_height,
                    is_complete=False,
                    generation=self._thumbnail_generation,
                ),
                cache=cache,
            )
            return

        self._clip_conversion_queue[request_key] = _PendingClipPixmapConversion(
            request_key=request_key,
            thumbnail_height=thumbnail_set.thumbnail_height,
            is_complete=thumbnail_set.is_complete,
            frames=thumbnail_set.frames,
            generation=thumbnail_set.generation,
        )
        self._process_clip_thumbnail_conversions()

    def _process_clip_thumbnail_conversions(self) -> None:
        """Convert only a few QImages per GUI event-loop turn."""
        if self._playback_active:
            cache = ClipThumbnailCache()
            pending_items = list(self._clip_conversion_queue.items())
            self._clip_conversion_queue.clear()
            self._clip_conversion_timer.stop()
            for request_key, pending in pending_items:
                self._finalize_clip_thumbnail_set(
                    request_key,
                    ClipThumbnailSet(
                        request_key=request_key,
                        frames=[],
                        thumbnail_height=pending.thumbnail_height,
                        is_complete=False,
                        generation=pending.generation,
                    ),
                    cache=cache,
                )
            return
        if not self._clip_conversion_queue:
            return

        request_key, pending = next(iter(self._clip_conversion_queue.items()))
        end_index = min(
            len(pending.frames),
            pending.next_index + self.PIXMAP_CONVERSIONS_PER_GUI_TICK,
        )
        for index in range(pending.next_index, end_index):
            frame_idx, qimg = pending.frames[index]
            pending.pixmap_frames.append((frame_idx, QPixmap.fromImage(qimg)))
        pending.next_index = end_index

        if pending.next_index >= len(pending.frames):
            del self._clip_conversion_queue[request_key]
            self._finalize_clip_thumbnail_set(
                request_key,
                ClipThumbnailSet(
                    request_key=request_key,
                    frames=pending.pixmap_frames,
                    thumbnail_height=pending.thumbnail_height,
                    is_complete=pending.is_complete,
                    generation=pending.generation,
                ),
            )

        if self._clip_conversion_queue:
            self._clip_conversion_timer.start(0)

    def _finalize_clip_thumbnail_set(
        self,
        request_key: str,
        pixmap_set: ClipThumbnailSet,
        *,
        cache: Optional[ClipThumbnailCache] = None,
    ) -> None:
        """Cache and publish a completed or cancelled thumbnail set."""
        cache = cache or ClipThumbnailCache()
        cache.clear_pending(request_key)
        if pixmap_set.num_frames > 0:
            cache.put(request_key, pixmap_set)

        # Call registered callbacks with QPixmap-based set
        callbacks = self._clip_thumbnail_callbacks.pop(request_key, [])
        for callback in callbacks:
            try:
                callback(request_key, pixmap_set)
            except Exception as e:
                logger.warning(f"Clip thumbnail callback error: {e}")

        # Emit signal for UI updates (pixmap-based)
        self.clip_thumbnail_set_ready.emit(request_key, pixmap_set)

    def get_cached_clip_thumbnails(self, request_key: str) -> Optional[ClipThumbnailSet]:
        """
        Get cached clip thumbnail set.

        Args:
            request_key: Cache key from ClipThumbnailRequest

        Returns:
            ClipThumbnailSet if cached, None otherwise
        """
        return ClipThumbnailCache().get(request_key)

    def is_clip_thumbnails_pending(self, request_key: str) -> bool:
        """
        Check if clip thumbnail request is pending.

        Args:
            request_key: Cache key from ClipThumbnailRequest

        Returns:
            True if extraction is pending
        """
        return ClipThumbnailCache().is_pending(request_key)

    def get_cached_frame_pixmap(
        self,
        asset_id: str,
        frame_number: int,
        target_height: int,
        *,
        path: str | None = None,
    ) -> Optional[QPixmap]:
        """
        Get cached frame pixmap by asset_id, frame_number, and target_height.

        When ``path`` is provided a :class:`FrameThumbnailKey` (with
        ``mtime_ns``) is used; otherwise a plain tuple
        ``(asset_id, frame_number, target_height)`` is used for
        backward compatibility.

        Args:
            asset_id: Asset identifier or file path
            frame_number: Frame index
            target_height: Target height in pixels
            path: Optional file path for mtime-aware cache key

        Returns:
            QPixmap if cached, None otherwise
        """
        if path is not None:
            key: tuple | FrameThumbnailKey = _make_frame_key(
                asset_id, path, frame_number, target_height,
            )
        else:
            key = (asset_id, frame_number, target_height)
        return self._frame_pixmap_cache.get(key)

    def get_cached_frame_pixmap_any(
        self,
        path: str,
        frame_number: int,
        target_height: int = 144,
        asset_id: str | None = None,
    ) -> Optional[QPixmap]:
        """Probe every frame-pixmap cache form for a cached thumbnail.

        Depending on which component decoded a frame first, the pixmap may
        live under the mtime-aware :class:`FrameThumbnailKey` (requested
        with an ``asset_id``), a legacy ``(path, frame, height)`` tuple
        (requested without), or the oldest ``(path, frame)`` store.
        Callers that must restore a known thumbnail synchronously — e.g.
        the Color clip strip rebuilding its cards without flashing the
        placeholder — use this instead of betting on one key form.
        """
        candidates: list = []
        if asset_id:
            candidates.append(
                _make_frame_key(asset_id, path, frame_number, target_height)
            )
        candidates.append((path, frame_number, target_height))
        for key in candidates:
            cached = self._frame_pixmap_cache.get(key)
            if cached is not None:
                return cached
        return self.frame_cache.get((path, frame_number))

    def request_frame_pixmap_async(
        self,
        asset_id: str,
        file_path: str,
        frame_number: int,
        total_frames: int,
        target_height: int = 48,
    ) -> None:
        """
        Request a single frame pixmap asynchronously with a
        :class:`FrameThumbnailKey` (mtime-aware).

        Emits ``frame_thumbnail_ready`` signal when the pixmap is ready.

        Args:
            asset_id: Asset identifier
            file_path: Video file path
            frame_number: Zero-based frame index
            total_frames: Total frames in the video
            target_height: Target thumbnail height in pixels
        """
        if self._playback_active:
            return

        cache_key = _make_frame_key(asset_id, file_path, frame_number, target_height)

        # Check cache first
        cached = self._frame_pixmap_cache.get(cache_key)
        if cached:
            self.frame_thumbnail_ready.emit(file_path, frame_number, cached)
            return

        # Skip if already pending
        if cache_key in self._pending_frame_pixmap_requests:
            return

        self._pending_frame_pixmap_requests.add(cache_key)
        request = FrameThumbnailRequest(
            file_path, frame_number, total_frames, target_height,
            self._on_frame_thumbnail_loaded,
            cancelled=lambda generation=self._thumbnail_generation: self._request_cancelled(generation),
        )
        self.pool.start(request, -10)


class FrameThumbnailRequest(QRunnable):
    """Async request to extract a specific frame from a video file."""

    def __init__(self, path: str, frame_index: int, total_frames: int,
                 icon_size: int, callback,
                 cancelled: Optional[Callable[[], bool]] = None) -> None:
        super().__init__()
        self.path = path
        self.frame_index = frame_index
        self.total_frames = total_frames
        self.icon_size = icon_size
        self.callback = callback
        self._cancelled = cancelled

    @pyqtSlot()
    def run(self) -> None:
        """Extract the specified frame from the video."""
        try:
            if self._cancelled is not None and self._cancelled():
                _get_bridge().frame_thumbnail_done.emit(self.path, self.frame_index, None)
                return
            ext = os.path.splitext(self.path)[1].lower()

            # Only support video files for frame extraction
            from src.shared.media.format_registry import VIDEO_EXTENSIONS
            if ext not in VIDEO_EXTENSIONS:
                _get_bridge().frame_thumbnail_done.emit(self.path, self.frame_index, None)
                return

            # Topos 私有编码（TPIC）没有 OpenCV 解码器 —— 经 Topos 后端抽帧。
            from src.shared.media.topos_source import is_topos_movie

            if is_topos_movie(self.path):
                from src.shared.media.topos_source import read_topos_frame_rgb

                img_rgb = read_topos_frame_rgb(self.path, self.frame_index)
                if img_rgb is None:
                    _get_bridge().frame_thumbnail_done.emit(
                        self.path, self.frame_index, None
                    )
                    return
                h, w = img_rgb.shape[:2]
                q_img = QImage(
                    np.ascontiguousarray(img_rgb).data.tobytes(), w, h, 3 * w,
                    QImage.Format.Format_RGB888,
                ).copy()
                render_size = max(self.icon_size, 256)
                scaled_img = q_img.scaled(
                    render_size, render_size,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
                _get_bridge().frame_thumbnail_done.emit(
                    self.path, self.frame_index, scaled_img
                )
                return

            file_lock = _get_file_lock(self.path)
            with file_lock:
                cap = _get_capture(self.path)
                try:
                    if not cap.isOpened():
                        _get_bridge().frame_thumbnail_done.emit(
                            self.path, self.frame_index, None
                        )
                        return

                    # Seek to the target frame
                    actual_frame = min(self.frame_index, self.total_frames - 1)
                    actual_frame = max(0, actual_frame)
                    cap.set(cv2.CAP_PROP_POS_FRAMES, actual_frame)

                    ret, frame = cap.read()
                finally:
                    _return_capture(self.path, cap)
                    # AUD-35714：同上，防止 _cv_locks 无界增长
                    _cleanup_file_lock(self.path)

            if not ret or frame is None:
                _get_bridge().frame_thumbnail_done.emit(self.path, self.frame_index, None)
                return

            if self._cancelled is not None and self._cancelled():
                _get_bridge().frame_thumbnail_done.emit(self.path, self.frame_index, None)
                return

            # Convert BGR to RGB
            img_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            h, w, ch = img_rgb.shape
            bytes_per_line = ch * w
            # .copy() decouples the QImage from the numpy buffer.
            q_img = QImage(
                img_rgb.data.tobytes(), w, h, bytes_per_line,
                QImage.Format.Format_RGB888,
            ).copy()

            # Scale to desired size — QImage.scaled() is thread-safe.
            render_size = max(self.icon_size, 256)
            scaled_img = q_img.scaled(
                render_size, render_size,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            _get_bridge().frame_thumbnail_done.emit(self.path, self.frame_index, scaled_img)

        except Exception as e:
            logger.error(f"Frame extraction failed for {self.path} frame {self.frame_index}: {e}")
            _get_bridge().frame_thumbnail_done.emit(self.path, self.frame_index, None)


# ================================================================
# Clip Thumbnail Worker (Phase 1.1)
# ================================================================

class ClipThumbnailWorker(QRunnable):
    """
    Async worker for extracting multiple frame thumbnails for a clip.

    参考 Kdenlive thumbnailprovider.cpp:
    - 使用 QThreadPool 异步执行
    - seek 到帧位置并解码
    - 缩放并返回 QImage（线程安全，QPixmap 只能在主线程创建）

    取帧策略（参考 Kdenlive ClipThumbs.qml）：
    1. 计算需要多少缩略图
    2. 均匀分布在 source range
    3. 逐帧 seek + decode
    """

    def __init__(
        self,
        request: ClipThumbnailRequest,
        callback: Callable,
        target_height: int = 48,
        cancelled: Optional[Callable[[], bool]] = None,
        generation: int = 0,
    ) -> None:
        """
        Initialize clip thumbnail worker.

        Args:
            request: ClipThumbnailRequest with frame parameters
            callback: Callback function(request_key, ClipThumbnailSet)
            target_height: Target thumbnail height in pixels
        """
        super().__init__()
        self._request = request
        self._callback = callback
        self._target_height = target_height
        self._cancelled = cancelled
        self._generation = generation

    @pyqtSlot()
    def run(self) -> None:
        """Extract thumbnails in background thread."""
        try:
            # Compute which frames to extract
            frame_indices = self._request.compute_thumbnail_frames()
            request_key = self._request.get_cache_key()

            if self._is_cancelled():
                self._emit_cancelled_result(request_key)
                return

            if len(frame_indices) == 0:
                # No frames needed
                thumbnail_set = ClipThumbnailSet(
                    request_key=request_key,
                    frames=[],
                    thumbnail_height=self._target_height,
                    is_complete=True,
                    generation=self._generation,
                )
                _get_bridge().clip_thumbnails_done.emit(request_key, thumbnail_set)
                return

            # 虚拟素材（topos:// scheme：adjustment / solid / comp_clip /
            # nested_sequence）没有磁盘文件，无法提取胶片缩略图 —— 提前返回
            # 空集（is_complete=True，标记为已完成而非失败），避免走到下面
            # 的 "Unknown format" 分支刷屏。这些片段的画面由渲染层程序化
            # 生成，时间线 ClipItem 自行绘制占位（ADJ/SOLID 徽标）。
            asset_path = self._request.asset_path
            if isinstance(asset_path, str) and asset_path.startswith("topos://"):
                thumbnail_set = ClipThumbnailSet(
                    request_key=request_key,
                    frames=[],
                    thumbnail_height=self._target_height,
                    is_complete=True,
                    generation=self._generation,
                )
                _get_bridge().clip_thumbnails_done.emit(request_key, thumbnail_set)
                return

            # Dispatch via signal bridge to ensure main-thread delivery
            ext = os.path.splitext(asset_path)[1].lower()
            from src.shared.media.format_registry import VIDEO_EXTENSIONS, IMAGE_EXTENSIONS
            video_extensions = VIDEO_EXTENSIONS
            image_extensions = IMAGE_EXTENSIONS

            frames: List[Tuple[int, QImage]] = []

            if ext in image_extensions:
                # For images, use the same thumbnail for all frames
                thumbnail = self._extract_image_thumbnail()
                if thumbnail:
                    for frame_idx in frame_indices:
                        frames.append((frame_idx, thumbnail))
            elif ext in video_extensions:
                # For videos, extract each frame
                frames = self._extract_video_frames(frame_indices)
            else:
                # Unknown format, skip
                logger.warning(f"Unknown format for thumbnail extraction: {ext}")
                thumbnail_set = ClipThumbnailSet(
                    request_key=request_key,
                    frames=[],
                    thumbnail_height=self._target_height,
                    is_complete=False,
                    generation=self._generation,
                )
                _get_bridge().clip_thumbnails_done.emit(request_key, thumbnail_set)
                return

            if self._is_cancelled():
                self._emit_cancelled_result(request_key)
                return

            # Create result set
            thumbnail_set = ClipThumbnailSet(
                request_key=request_key,
                frames=frames,
                thumbnail_height=self._target_height,
                is_complete=not self._is_cancelled() and len(frames) == len(frame_indices),
                generation=self._generation,
            )

            _get_bridge().clip_thumbnails_done.emit(request_key, thumbnail_set)

        except Exception as e:
            logger.error(f"Clip thumbnail extraction failed for {self._request.asset_path}: {e}")
            request_key = self._request.get_cache_key()
            thumbnail_set = ClipThumbnailSet(
                request_key=request_key,
                frames=[],
                thumbnail_height=self._target_height,
                is_complete=False,
                generation=self._generation,
            )
            _get_bridge().clip_thumbnails_done.emit(request_key, thumbnail_set)

    def _is_cancelled(self) -> bool:
        """Check cooperative cancellation from the thumbnail provider."""
        return self._cancelled is not None and self._cancelled()

    def _emit_cancelled_result(self, request_key: str) -> None:
        """Release pending UI state without caching stale thumbnail data."""
        _get_bridge().clip_thumbnails_done.emit(
            request_key,
            ClipThumbnailSet(
                request_key=request_key,
                frames=[],
                thumbnail_height=self._target_height,
                is_complete=False,
                generation=self._generation,
            ),
        )

    def _extract_image_thumbnail(self) -> Optional[QImage]:
        """Extract single thumbnail from image file (returns QImage, thread-safe)."""
        try:
            # Use np.fromfile to handle Unicode paths on Windows
            img_data = np.fromfile(self._request.asset_path, dtype=np.uint8)
            img_bgr = cv2.imdecode(img_data, cv2.IMREAD_COLOR)

            if img_bgr is None:
                return None

            # Handle EXIF orientation
            ext = os.path.splitext(self._request.asset_path)[1].lower()
            if ext in ['.jpg', '.jpeg', '.png', '.tif', '.tiff', '.webp', '.heic', '.heif']:
                img_bgr = FileIO._apply_exif_orientation(img_bgr, self._request.asset_path)

            # Convert BGR to RGB
            img_rgb = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB)
            h, w, ch = img_rgb.shape

            # Scale to target height
            scale = self._target_height / h
            new_w = int(w * scale)
            new_h = self._target_height

            img_rgb = cv2.resize(img_rgb, (new_w, new_h), interpolation=cv2.INTER_AREA)

            bytes_per_line = ch * new_w
            # .copy() decouples the QImage from the numpy buffer.
            q_img = QImage(
                img_rgb.data.tobytes(), new_w, new_h, bytes_per_line,
                QImage.Format.Format_RGB888,
            ).copy()

            return q_img

        except Exception as e:
            logger.error(f"Image thumbnail extraction failed: {e}")
            return None

    def _extract_video_frames(self, frame_indices: List[int]) -> List[Tuple[int, QImage]]:
        """
        Extract thumbnails from video file at specified frames.

        Uses per-file lock so different files can be extracted in parallel.
        Reuses VideoCapture instances from pool to avoid repeated open/close.

        Args:
            frame_indices: List of frame indices to extract

        Returns:
            List of (frame_index, QImage) tuples
        """
        frames: List[Tuple[int, QImage]] = []
        if self._is_cancelled():
            return frames
        path = self._request.asset_path

        # Topos 私有编码（TPIC）没有 OpenCV 解码器 —— 经 Topos 后端批量抽帧
        # （一次 open 多帧读取，等比缩放到目标胶片高度）。
        from src.shared.media.topos_source import is_topos_movie

        if is_topos_movie(path):
            from src.shared.media.topos_source import ToposMediaSource, topos_frame_to_rgb

            try:
                source = ToposMediaSource(path)
                source.open()
            except Exception as e:
                logger.error(f"Failed to open Topos video: {path}: {e}")
                return frames
            try:
                md = source.probe()
                total_frames = int(md.total_frames or 0)
                video_height = int(md.height or 0)
                video_width = int(md.width or 0)
                if total_frames <= 0 or video_height <= 0:
                    return frames
                target_height = self._target_height
                scale = target_height / video_height
                target_width = max(1, int(video_width * scale))
                for frame_idx in frame_indices:
                    if self._is_cancelled():
                        return []
                    actual_frame = max(0, min(frame_idx, total_frames - 1))
                    decoded = source.read_frame(actual_frame)
                    img_rgb = topos_frame_to_rgb(decoded) if decoded is not None else None
                    if img_rgb is None:
                        continue
                    if self._is_cancelled():
                        return []
                    img_rgb = cv2.resize(
                        img_rgb, (target_width, target_height),
                        interpolation=cv2.INTER_AREA,
                    )
                    h, w = img_rgb.shape[:2]
                    q_img = QImage(
                        np.ascontiguousarray(img_rgb).data.tobytes(),
                        w, h, 3 * w, QImage.Format.Format_RGB888,
                    ).copy()
                    frames.append((frame_idx, q_img))
                return frames
            finally:
                try:
                    source.close()
                except Exception:
                    pass

        file_lock = _get_file_lock(path)

        with file_lock:
            cap = _get_capture(path)
            try:
                if not cap.isOpened():
                    logger.error(f"Failed to open video: {path}")
                    return frames

                # Get video properties
                total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
                video_height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
                video_width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))

                if total_frames <= 0 or video_height <= 0:
                    return frames

                # Calculate target dimensions (preserve aspect ratio)
                target_height = self._target_height
                scale = target_height / video_height
                target_width = max(1, int(video_width * scale))

                # Extract frames.  The finally block below must run for every
                # cancellation/decoder-error return so a native VideoCapture
                # is never left in the shared pool half-owned.
                for frame_idx in frame_indices:
                    if self._is_cancelled():
                        return []
                    actual_frame = max(0, min(frame_idx, total_frames - 1))
                    cap.set(cv2.CAP_PROP_POS_FRAMES, actual_frame)

                    ret, frame = cap.read()
                    if not ret or frame is None:
                        continue

                    if self._is_cancelled():
                        return []

                    img_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                    img_rgb = cv2.resize(
                        img_rgb,
                        (target_width, target_height),
                        interpolation=cv2.INTER_AREA,
                    )

                    h, w, ch = img_rgb.shape
                    bytes_per_line = ch * w
                    q_img = QImage(
                        img_rgb.data.tobytes(), w, h, bytes_per_line,
                        QImage.Format.Format_RGB888,
                    ).copy()
                    frames.append((frame_idx, q_img))
            finally:
                _return_capture(path, cap)
                # AUD-35714：同上，防止 _cv_locks 无界增长
                _cleanup_file_lock(path)

        return frames


# ================================================================
# Convenience Functions for Clip Thumbnails (Phase 1.1)
# ================================================================

def request_clip_thumbnails_async(
    request: ClipThumbnailRequest,
    callback: Optional[Callable] = None
) -> None:
    """
    Request clip thumbnail set asynchronously.

    Args:
        request: ClipThumbnailRequest with frame parameters
        callback: Optional callback when thumbnails are ready
    """
    provider = ThumbnailProvider.instance()
    provider.request_clip_thumbnails(request, callback)


def get_cached_clip_thumbnails(request_key: str) -> Optional[ClipThumbnailSet]:
    """
    Get cached clip thumbnail set.

    Args:
        request_key: Cache key from ClipThumbnailRequest

    Returns:
        ClipThumbnailSet if cached, None otherwise
    """
    return ClipThumbnailCache().get(request_key)


def is_clip_thumbnails_pending(request_key: str) -> bool:
    """
    Check if clip thumbnail request is pending.

    Args:
        request_key: Cache key from ClipThumbnailRequest

    Returns:
        True if extraction is pending
    """
    return ClipThumbnailCache().is_pending(request_key)
