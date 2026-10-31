"""
Audio Waveform Extraction Module.

Provides waveform extraction from audio files and caching for timeline display.

## Responsibilities

- WaveformData: Peak amplitude data structure
- WaveformCache: LRU cache for waveform data
- WaveformExtractor: Background extraction using QThread
- request_waveform_async: Async waveform request with callback

## Design Principles

- Thread-safe: QThread for background extraction
- LRU cache: Limited memory usage with automatic eviction
- Signal-based: Notify UI when waveform is ready
- Graceful degradation: Show loading state when waveform pending
"""

from __future__ import annotations

import hashlib
import logging
import math
import os
import tempfile
from dataclasses import dataclass
from typing import Optional, Callable, Dict
from collections import OrderedDict
import numpy as np

from PyQt6.QtCore import QCoreApplication, QObject, pyqtSignal, QThread, QMutex

logger = logging.getLogger(__name__)

# Default waveform parameters
# Keep a high-resolution envelope (roughly 2K peak columns/sec) so zooming in
# reveals real transient detail instead of enlarged coarse blocks. Rendering
# still reduces it to one min/max pair per screen column.
WAVEFORM_SAMPLES_PER_SECOND = 2048
# At worst, stereo min/max data is about 2 MiB per cached asset. This extra
# headroom postpones coarsening for long clips while keeping a 32-entry cache
# within a practical memory budget.
MAX_WAVEFORM_SAMPLES = 131072
WAVEFORM_CACHE_SIZE = 32
# Waveform extraction is deliberately serialized.  Multiple PyAV decoders
# compete with timeline playback for CPU and disk bandwidth far more than a
# waveform benefits from parallel generation.
MAX_CONCURRENT_WAVEFORM_EXTRACTIONS = 1
# Decode in short batches, then yield. Sleeping after every ~1024-sample audio
# frame added about 86ms of artificial delay per second of 44.1 kHz media.
# QThread low priority and immediate interruption on playback still protect UI
# and video decode without making waveform generation unnecessarily slow.
WAVEFORM_DECODE_YIELD_EVERY_FRAMES = 32
WAVEFORM_DECODE_YIELD_MS = 1
# AUD-15605: cancel_all 在 GUI 线程等待 worker 退出的上限（毫秒）。协作式中断
# 只在解码帧间检查；worker 阻塞在 av.open()/底层 IO（慢速网络源）时可能长时间
# 不退出——超过上限即告警并转为后台托管，不再无界挂起 GUI 线程。
WAVEFORM_CANCEL_WAIT_MS = 1500


class _WaveformExtractionDeferred(Exception):
    """Internal control flow: playback took priority over waveform decoding."""


# ================================================================
# WaveformData - Data Structure
# ================================================================

@dataclass
class WaveformData:
    """
    Waveform peak amplitude data.

    Attributes:
        asset_id: Associated asset ID
        peaks_min: Minimum peak values (normalized -1.0 to 0.0)
        peaks_max: Peak **magnitude** envelope, ``max(|samples|)`` per bucket
            (normalized 0.0 to 1.0). AUD-15606: this is NOT the signed positive
            half of the waveform — for an all-negative block (deep trough) the
            value is the magnitude of that trough, kept so the combined
            min/max pair always brackets the full signal amplitude.
        samples_per_second: Sample density
        duration_seconds: Audio duration
        channels: Number of audio channels (1=mono, 2=stereo)
        is_complete: Whether extraction is complete

    For stereo audio:
        peaks_min and peaks_max contain interleaved data: [L0, R0, L1, R1, ...]
        Use get_samples_for_range() with channel parameter to extract per-channel data.
    """
    asset_id: str
    peaks_min: np.ndarray  # Negative peaks (downward, <= 0.0)
    # AUD-15606: magnitude envelope max(|peak|), >= 0.0 — not "positive peaks".
    peaks_max: np.ndarray
    samples_per_second: float
    duration_seconds: float
    channels: int = 1  # Number of audio channels (1=mono, 2=stereo, etc.)
    is_complete: bool = True

    def __post_init__(self) -> None:
        if len(self.peaks_min) != len(self.peaks_max):
            raise ValueError("peaks_min and peaks_max must have same length")

    @property
    def num_samples(self) -> int:
        return len(self.peaks_min)

    def get_samples_for_range(
        self,
        start_time: float,
        end_time: float,
        num_pixels: int,
        channel: Optional[int] = None
    ) -> tuple[np.ndarray, np.ndarray]:
        """
        Get peak samples for a time range, scaled to pixel count.

        Args:
            start_time: Start time in seconds
            end_time: End time in seconds
            num_pixels: Number of pixels to display
            channel: Optional channel index (0=left, 1=right for stereo).
                     If None and channels > 1, returns mixed (all channels combined).
                     If specified, returns only that channel's data.

        Returns:
            Tuple of (peaks_min, peaks_max) arrays scaled to num_pixels

        For stereo audio with interleaved data:
            peaks_min/max format: [L0, R0, L1, R1, L2, R2, ...]
            To get left channel (channel=0): extract indices 0, 2, 4, ...
            To get right channel (channel=1): extract indices 1, 3, 5, ...
        """
        if self.num_samples == 0 or num_pixels <= 0:
            return np.zeros(num_pixels), np.zeros(num_pixels)

        # Calculate sample indices for time range
        # For interleaved data, actual samples = num_samples / channels
        samples_per_channel = self.num_samples // self.channels
        start_idx = int(start_time * self.samples_per_second)
        end_idx = int(end_time * self.samples_per_second)

        # Clamp indices
        start_idx = max(0, min(start_idx, samples_per_channel - 1))
        end_idx = max(0, min(end_idx, samples_per_channel))

        if start_idx >= end_idx:
            return np.zeros(num_pixels), np.zeros(num_pixels)

        # Reshape once to (time buckets, channels).  The old implementation
        # iterated every bucket in Python for stereo/mixdown, which made wide
        # timeline clips and repeated paints needlessly expensive.
        interleaved_min = self.peaks_min[:samples_per_channel * self.channels].reshape(
            samples_per_channel, self.channels)
        interleaved_max = self.peaks_max[:samples_per_channel * self.channels].reshape(
            samples_per_channel, self.channels)

        # Extract range for specific channel or mix all channels.
        if self.channels > 1 and channel is not None:
            safe_channel = max(0, min(int(channel), self.channels - 1))
            range_min = interleaved_min[start_idx:end_idx, safe_channel]
            range_max = interleaved_max[start_idx:end_idx, safe_channel]
        elif self.channels > 1:
            # Mixdown envelope: retain the extreme of every channel per bucket.
            range_min = np.min(interleaved_min[start_idx:end_idx], axis=1)
            range_max = np.max(interleaved_max[start_idx:end_idx], axis=1)
        else:
            # Mono: simple range extraction
            range_min = self.peaks_min[start_idx:end_idx]
            range_max = self.peaks_max[start_idx:end_idx]

        # Resample to pixel count
        if len(range_min) == num_pixels:
            return range_min, range_max

        source_count = len(range_min)
        if source_count >= num_pixels:
            # One min/max envelope per screen column.  reduceat avoids a
            # Python loop and, unlike averaging, preserves transient peaks.
            starts = (np.arange(num_pixels, dtype=np.int64) * source_count) // num_pixels
            result_min = np.minimum.reduceat(range_min, starts)
            result_max = np.maximum.reduceat(range_max, starts)
            return result_min, result_max

        # Zoomed-in case: repeat the nearest source envelope.  It is both
        # stable and far cheaper than building zero-width reduction buckets.
        indices = (np.arange(num_pixels, dtype=np.int64) * source_count) // num_pixels
        return range_min[indices], range_max[indices]


# ================================================================
# WaveformCache - LRU Cache
# ================================================================

class WaveformCache:
    """
    LRU cache for waveform data.

    Thread-safe singleton cache with limited size.
    Automatically evicts oldest entries when full.
    """

    _instance: Optional[WaveformCache] = None
    _mutex = QMutex()

    def __new__(cls) -> "WaveformCache":
        # AUD-15601: check-then-create 必须整体持类级 `_mutex`——worker 线程
        # （run()）与 GUI 线程可能并发首次构造，无锁双重检查会造出两个实例、
        # 两套 `_cache/_pending`（pending 标记互不可见 → 同一资产重复解码）。
        # _mutex 是类属性（跨实例共享），在此整体串行化创建；类内其余方法
        # 持锁期间不会再次构造 WaveformCache()，无重入死锁。
        # 注：QMutex 不支持 with 协议，用 lock/unlock。
        cls._mutex.lock()
        try:
            if cls._instance is None:
                instance = super().__new__(cls)
                instance._cache = OrderedDict()  # type: Dict[str, WaveformData]
                instance._pending = {}  # type: Dict[str, bool]  # Track pending requests
                instance._max_size = WAVEFORM_CACHE_SIZE
                # Secondary LRU cache for source-range-resampled waveform data
                instance._range_cache = OrderedDict()  # type: OrderedDict[str, tuple]
                instance._range_cache_max_size = WAVEFORM_CACHE_SIZE * 2  # 200 ranged entries
                cls._instance = instance
            return cls._instance
        finally:
            cls._mutex.unlock()

    def get_cached_waveform(self, asset_id: str) -> Optional[WaveformData]:
        """
        Get cached waveform for asset.

        Args:
            asset_id: Asset ID to look up

        Returns:
            WaveformData if cached, None if not cached or pending
        """
        self._mutex.lock()
        try:
            if asset_id in self._cache:
                # Move to end (most recently used)
                self._cache.move_to_end(asset_id)
                return self._cache[asset_id]
            return None
        finally:
            self._mutex.unlock()

    def is_pending(self, asset_id: str) -> bool:
        """Check if waveform extraction is pending for asset."""
        self._mutex.lock()
        try:
            return asset_id in self._pending
        finally:
            self._mutex.unlock()

    def set_pending(self, asset_id: str) -> None:
        """Mark asset as having pending extraction."""
        self._mutex.lock()
        try:
            self._pending[asset_id] = True
        finally:
            self._mutex.unlock()

    def clear_pending(self, asset_id: str) -> None:
        """Clear pending status for asset."""
        self._mutex.lock()
        try:
            self._pending.pop(asset_id, None)
        finally:
            self._mutex.unlock()

    def cache_waveform(self, waveform: WaveformData) -> None:
        """
        Add waveform to cache.

        Evicts oldest entry if cache is full.

        Args:
            waveform: WaveformData to cache
        """
        self._mutex.lock()
        try:
            asset_id = waveform.asset_id

            # Remove if already exists (will be re-added at end)
            if asset_id in self._cache:
                del self._cache[asset_id]

            # Evict oldest if full
            while len(self._cache) >= self._max_size:
                self._cache.popitem(last=False)

            # Add new waveform
            self._cache[asset_id] = waveform

            # Clear pending status
            self._pending.pop(asset_id, None)

            logger.debug(f"Cached waveform for asset {asset_id}, cache size: {len(self._cache)}")
        finally:
            self._mutex.unlock()

    def clear_cache(self) -> None:
        """Clear all cached waveforms."""
        self._mutex.lock()
        try:
            self._cache.clear()
            self._range_cache.clear()
            self._pending.clear()
            logger.debug("Waveform cache cleared")
        finally:
            self._mutex.unlock()

    def get_cache_size(self) -> int:
        """Get current cache size."""
        self._mutex.lock()
        try:
            return len(self._cache)
        finally:
            self._mutex.unlock()

    @staticmethod
    def build_range_cache_key(
        asset_id: str,
        source_start: float,
        source_duration: float,
        display_width: int
    ) -> str:
        """
        Build a source-range-aware cache key.

        This key ensures that trimming a clip causes the waveform
        to be recalculated for the correct range.

        Args:
            asset_id: Asset ID
            source_start: Start time in seconds (or frame-based offset)
            source_duration: Duration in seconds (or frame-based count)
            display_width: Display width in pixels

        Returns:
            Cache key string incorporating all range parameters
        """
        return f"{asset_id}_{source_start:.4f}_{source_duration:.4f}_{display_width}"

    def get_ranged_waveform(self, cache_key: str) -> Optional[tuple[np.ndarray, np.ndarray]]:
        """
        Get cached ranged waveform (pre-resampled peaks).

        Args:
            cache_key: Key from build_range_cache_key()

        Returns:
            Tuple of (peaks_min, peaks_max) arrays if cached, None otherwise
        """
        self._mutex.lock()
        try:
            if cache_key in self._range_cache:
                self._range_cache.move_to_end(cache_key)
                return self._range_cache[cache_key]
            return None
        finally:
            self._mutex.unlock()

    def cache_ranged_waveform(
        self,
        cache_key: str,
        peaks_min: np.ndarray,
        peaks_max: np.ndarray
    ) -> None:
        """
        Cache pre-resampled ranged waveform data.

        Args:
            cache_key: Key from build_range_cache_key()
            peaks_min: Minimum peaks array
            peaks_max: Maximum peaks array
        """
        self._mutex.lock()
        try:
            # Remove if already exists (will be re-added at end)
            if cache_key in self._range_cache:
                del self._range_cache[cache_key]

            # Evict oldest if full
            while len(self._range_cache) >= self._range_cache_max_size:
                self._range_cache.popitem(last=False)

            self._range_cache[cache_key] = (peaks_min, peaks_max)
        finally:
            self._mutex.unlock()


# ================================================================
# Waveform Disk Cache
# ================================================================

# Bump when the on-disk layout changes so stale files are never loaded.
WAVEFORM_DISK_CACHE_VERSION = 1
WAVEFORM_DISK_CACHE_MAX_FILES = 256


def _waveform_disk_cache_dir() -> Optional[str]:
    """Return the waveform disk-cache directory, or None when disabled."""
    if os.environ.get("TOPOS_WAVEFORM_DISK_CACHE", "1") == "0":
        return None
    base: Optional[str] = None
    try:
        from PyQt6.QtCore import QStandardPaths

        base = QStandardPaths.writableLocation(
            QStandardPaths.StandardLocation.AppCacheLocation)
    except Exception:
        base = None
    if not base:
        base = os.path.join(tempfile.gettempdir(), "topos-color")
    path = os.path.join(base, "waveforms")
    try:
        os.makedirs(path, exist_ok=True)
        return path
    except OSError:
        return None


def _waveform_disk_cache_path(cache_dir: str, file_path: str, samples_per_second: float) -> str:
    """Build a cache file path keyed by source identity (path+mtime+size)."""
    try:
        st = os.stat(file_path)
        identity = f"{os.path.abspath(file_path)}|{st.st_mtime_ns}|{st.st_size}"
    except OSError:
        identity = f"{os.path.abspath(file_path)}||"
    raw = f"v{WAVEFORM_DISK_CACHE_VERSION}|{identity}|{int(samples_per_second)}"
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()
    return os.path.join(cache_dir, digest[:24] + ".npz")


def load_waveform_from_disk(
    asset_id: str,
    file_path: str,
    samples_per_second: float = WAVEFORM_SAMPLES_PER_SECOND,
) -> Optional[WaveformData]:
    """Load a previously extracted waveform from the on-disk cache.

    Returns None on any miss (missing file, corrupted payload, version
    mismatch). Failures are intentionally silent: the caller falls back to
    a fresh extraction.
    """
    cache_dir = _waveform_disk_cache_dir()
    if not cache_dir:
        return None
    path = _waveform_disk_cache_path(cache_dir, file_path, samples_per_second)
    try:
        with np.load(path, allow_pickle=False) as payload:
            if int(payload["v"]) != WAVEFORM_DISK_CACHE_VERSION:
                return None
            return WaveformData(
                asset_id=asset_id,
                peaks_min=payload["min"].astype(np.float32, copy=False),
                peaks_max=payload["max"].astype(np.float32, copy=False),
                samples_per_second=float(payload["sps"]),
                duration_seconds=float(payload["dur"]),
                channels=int(payload["ch"]),
                is_complete=True,
            )
    except Exception:
        return None


def save_waveform_to_disk(
    file_path: str,
    waveform: WaveformData,
    samples_per_second: float = WAVEFORM_SAMPLES_PER_SECOND,
) -> None:
    """Persist one waveform to the on-disk cache and trim old entries.

    The uncompressed .npz write costs a few milliseconds per asset while
    making every later project open skip the whole decode pass.
    """
    cache_dir = _waveform_disk_cache_dir()
    if not cache_dir:
        return
    path = _waveform_disk_cache_path(cache_dir, file_path, samples_per_second)
    try:
        # np.savez appends ".npz" only when missing, so end the temp name with
        # it explicitly and keep os.replace() atomic.
        tmp_path = path + ".tmp.npz"
        np.savez(
            tmp_path,
            v=np.int64(WAVEFORM_DISK_CACHE_VERSION),
            min=waveform.peaks_min,
            max=waveform.peaks_max,
            sps=np.float64(waveform.samples_per_second),
            dur=np.float64(waveform.duration_seconds),
            ch=np.int64(waveform.channels),
        )
        os.replace(tmp_path, path)
        _trim_waveform_disk_cache(cache_dir)
    except Exception as e:
        logger.debug(f"Waveform disk-cache write failed for {file_path}: {e}")


def _trim_waveform_disk_cache(cache_dir: str) -> None:
    """Keep the cache directory bounded by deleting the oldest files."""
    try:
        entries = []
        for name in os.listdir(cache_dir):
            if not name.endswith(".npz"):
                continue
            full = os.path.join(cache_dir, name)
            try:
                entries.append((os.path.getmtime(full), full))
            except OSError:
                continue
        excess = len(entries) - WAVEFORM_DISK_CACHE_MAX_FILES
        for _, full in sorted(entries)[:max(0, excess)]:
            try:
                os.remove(full)
            except OSError:
                pass
    except OSError:
        pass


# ================================================================
# Peak Aggregator - shared vectorized min/max bucketing
# ================================================================

class _PeakAggregator:
    """Vectorized min/max peak bucketing shared by all waveform renderers.

    Samples arrive as (channels, N) float32 blocks and are aggregated with
    ``np.minimum.reduceat`` every ~64K samples. The previous per-frame
    ``np.minimum.at`` scatter was an order of magnitude slower and dominated
    extraction time for long clips.

    Buckets stay aligned to global sample positions, so blocks may have any
    size and the bucket span may even grow mid-stream (long media with
    unreliable container metadata) without losing or double-counting samples.
    """

    _BLOCK_SAMPLES = 65536

    def __init__(
        self,
        channels: int,
        max_buckets: int = MAX_WAVEFORM_SAMPLES,
        samples_per_second: float = WAVEFORM_SAMPLES_PER_SECOND,
        sample_rate: float = 44100.0,
        estimated_duration_seconds: float = 0.0,
    ) -> None:
        self._channels = max(1, channels)
        self._capacity = int(max_buckets)
        self._base_spb = max(1, int(round(sample_rate / max(1.0, samples_per_second))))
        self._sample_rate = float(sample_rate)
        estimated_samples = int(max(0.0, estimated_duration_seconds) * sample_rate)
        # Start at the final bucket span when the duration is trustworthy so
        # the envelope never coarsens mid-decode for normal-length clips.
        self._spb = max(
            self._base_spb,
            int(math.ceil(estimated_samples / self._capacity)) if estimated_samples else self._base_spb,
        )
        self.peaks_min = np.zeros((self._channels, self._capacity), dtype=np.float32)
        self.peaks_max = np.zeros((self._channels, self._capacity), dtype=np.float32)
        # Global per-channel sample count covered by peaks + buffer together.
        self._total_seen = 0
        # Global sample count whose full buckets are already merged into peaks.
        self._agg_pos = 0
        self._buf_parts: list[np.ndarray] = []
        self._buf_len = 0

    @property
    def total_samples(self) -> int:
        return self._total_seen

    def add(self, block: np.ndarray) -> None:
        """Accumulate one (channels, N) float32 block in [-1, 1]."""
        if block.ndim != 2 or block.shape[1] <= 0:
            return
        self._buf_parts.append(block)
        self._buf_len += int(block.shape[1])
        self._total_seen += int(block.shape[1])
        if self._buf_len >= self._BLOCK_SAMPLES:
            self._flush()

    def _grow(self) -> None:
        """Double covered duration by pairwise-merging existing buckets."""
        self._spb *= 2
        even_min = self.peaks_min[:, 0::2]
        even_max = self.peaks_max[:, 0::2]
        odd_min = self.peaks_min[:, 1::2]
        odd_max = self.peaks_max[:, 1::2]
        new_min = np.zeros((self._channels, self._capacity), dtype=np.float32)
        new_max = np.zeros((self._channels, self._capacity), dtype=np.float32)
        new_min[:, :even_min.shape[1]] = even_min
        new_max[:, :even_max.shape[1]] = even_max
        if odd_min.shape[1]:
            new_min[:, :odd_min.shape[1]] = np.minimum(
                new_min[:, :odd_min.shape[1]], odd_min)
            new_max[:, :odd_max.shape[1]] = np.maximum(
                new_max[:, :odd_max.shape[1]], odd_max)
        self.peaks_min = new_min
        self.peaks_max = new_max

    def _flush(self) -> None:
        """Merge buffered samples into buckets; keep the partial tail buffered."""
        if not self._buf_parts:
            return
        block = (np.concatenate(self._buf_parts, axis=1)
                 if len(self._buf_parts) > 1 else self._buf_parts[0])
        self._buf_parts = []
        block_start = self._agg_pos
        total_end = block_start + int(block.shape[1])

        # Capacity guard: grow the bucket span until everything fits.
        while total_end > self._spb * self._capacity:
            self._grow()

        spb = self._spb
        last_full = (total_end // spb) * spb
        if last_full > block_start:
            # The buffer may start mid-bucket (after a span grow); clipping the
            # first offset to 0 merges the tail into the already-partial
            # bucket, which is exactly the semantics peak aggregation needs.
            first_bucket_start = (block_start // spb) * spb
            usable = block[:, :last_full - block_start]
            bucket_starts = np.arange(first_bucket_start, last_full, spb, dtype=np.int64)
            offsets = np.maximum(bucket_starts - block_start, 0)
            bmin = np.minimum.reduceat(usable, offsets, axis=1)
            bmax = np.maximum.reduceat(usable, offsets, axis=1)
            b0 = int(bucket_starts[0] // spb)
            nb = int(bmin.shape[1])
            seg = slice(b0, b0 + nb)
            self.peaks_min[:, seg] = np.minimum(self.peaks_min[:, seg], bmin)
            # Peak magnitude is max(|samples|): combining both envelope
            # halves keeps all-negative blocks (e.g. deep troughs) correct.
            self.peaks_max[:, seg] = np.maximum(
                self.peaks_max[:, seg], np.maximum(np.abs(bmin), np.abs(bmax)))
            self._agg_pos = last_full

        keep_from = self._agg_pos - block_start
        if keep_from < block.shape[1]:
            self._buf_parts = [np.ascontiguousarray(block[:, keep_from:])]
            self._buf_len = int(block.shape[1] - keep_from)
        else:
            self._buf_len = 0

    def finish(self) -> Optional[tuple[np.ndarray, np.ndarray, float, int]]:
        """Flush everything and return (peaks_min, peaks_max interleaved, buckets/sec, bucket_count).

        Returns None when no samples were added.
        """
        self._flush()
        # Fold the remaining partial-bucket tail into its bucket.
        if self._buf_parts:
            tail = (np.concatenate(self._buf_parts, axis=1)
                    if len(self._buf_parts) > 1 else self._buf_parts[0])
            self._buf_parts = []
            bucket = self._agg_pos // self._spb
            if bucket < self._capacity:
                tail_min = tail.min(axis=1)
                tail_max = np.abs(tail).max(axis=1)
                self.peaks_min[:, bucket] = np.minimum(self.peaks_min[:, bucket], tail_min)
                self.peaks_max[:, bucket] = np.maximum(self.peaks_max[:, bucket], tail_max)
            self._agg_pos += int(tail.shape[1])
            self._buf_len = 0
        if self._total_seen <= 0:
            return None
        bucket_count = int(min(
            self._capacity, math.ceil(self._total_seen / self._spb)))
        if bucket_count <= 0:
            return None
        peaks_min = self.peaks_min[:, :bucket_count].T.reshape(-1)
        peaks_max = self.peaks_max[:, :bucket_count].T.reshape(-1)
        samples_per_second = self._sample_rate / self._spb
        return peaks_min, peaks_max, samples_per_second, bucket_count


# ================================================================
# WaveformExtractor - Background Extraction
# ================================================================

class WaveformExtractorWorker(QThread):
    """
    Background worker for waveform extraction.

    Runs in separate thread to avoid blocking UI.
    Uses PyAV to decode audio and compute peaks.
    """

    waveform_ready = pyqtSignal(str, object)  # asset_id, WaveformData
    extraction_failed = pyqtSignal(str, str)  # asset_id, error_message
    extraction_deferred = pyqtSignal(str, str)  # asset_id, file_path

    def __init__(
        self,
        asset_id: str,
        file_path: str,
        samples_per_second: float = WAVEFORM_SAMPLES_PER_SECOND,
        parent: Optional[QObject] = None
    ) -> None:
        super().__init__(parent)
        self._asset_id = asset_id
        self._file_path = file_path
        self._samples_per_second = samples_per_second

    def run(self) -> None:
        """Extract waveform from audio file."""
        try:
            # Disk cache first: a previously extracted asset reloads in
            # milliseconds without touching PyAV at all.
            waveform = load_waveform_from_disk(
                self._asset_id, self._file_path, self._samples_per_second)
            if waveform is not None:
                WaveformCache().cache_waveform(waveform)
                self.waveform_ready.emit(self._asset_id, waveform)
                return
            waveform = self._extract_waveform()
            if self.isInterruptionRequested():
                raise _WaveformExtractionDeferred()
            if waveform:
                # Cache the result
                WaveformCache().cache_waveform(waveform)
                save_waveform_to_disk(self._file_path, waveform, self._samples_per_second)
                # Emit signal
                self.waveform_ready.emit(self._asset_id, waveform)
            else:
                WaveformCache().clear_pending(self._asset_id)
                self.extraction_failed.emit(self._asset_id, "No audio data found")
        except _WaveformExtractionDeferred:
            # Do not mark this as a media failure. The manager preserves the
            # pending request and restarts it after playback has stopped.
            self.extraction_deferred.emit(self._asset_id, self._file_path)
        except Exception as e:
            logger.error(f"Waveform extraction failed for {self._asset_id}: {e}")
            self.extraction_failed.emit(self._asset_id, str(e))
            # Clear pending status on failure
            WaveformCache().clear_pending(self._asset_id)

    def _extract_waveform(self) -> Optional[WaveformData]:
        """
        Extract waveform peaks from audio file.

        For stereo audio, stores interleaved channel data:
        peaks_min/max format: [L0, R0, L1, R1, L2, R2, ...]

        Returns:
            WaveformData with peak values and channel count, or None if no audio
        """
        import av

        try:
            container = av.open(self._file_path)
        except Exception as e:
            logger.error(f"Failed to open audio file: {self._file_path}: {e}")
            return None

        # Find audio stream
        audio_stream = None
        for stream in container.streams:
            if stream.type == 'audio':
                audio_stream = stream
                break

        if audio_stream is None:
            logger.warning(f"No audio stream in file: {self._file_path}")
            container.close()
            return None

        # Get audio properties
        duration_seconds = float(container.duration) / 1000000 if container.duration else 0.0
        if duration_seconds <= 0:
            # Estimate from stream
            if audio_stream.duration:
                duration_seconds = float(audio_stream.duration) * audio_stream.time_base
            else:
                duration_seconds = 0.0

        if duration_seconds <= 0:
            logger.warning(f"Cannot determine duration for: {self._file_path}")
            container.close()
            return None

        # Get number of channels from audio stream
        # PyAV audio_stream.channels gives the channel count
        num_channels = audio_stream.channels if hasattr(audio_stream, 'channels') else 1
        num_channels = max(1, min(num_channels, 2))  # Support mono and stereo (limit to 2 for display)

        # Aggregator keeps the full high-resolution envelope (see
        # MAX_WAVEFORM_SAMPLES note above) irrespective of container duration
        # metadata; some MOV/MP4 files report a tiny duration first and
        # deriving capacity from that repeatedly coarsened later audio.
        aggregator = _PeakAggregator(
            channels=num_channels,
            max_buckets=MAX_WAVEFORM_SAMPLES,
            samples_per_second=self._samples_per_second,
            sample_rate=44100.0,
            estimated_duration_seconds=duration_seconds,
        )

        # Resampler - preserve original channel layout for stereo
        layout = 'stereo' if num_channels == 2 else 'mono'
        resampler = av.AudioResampler(
            # Planar output guarantees to_ndarray() has one row per channel.
            # Packed s16 can return (1, N * channels), which the old code
            # interpreted as mono and left stereo peak buckets empty.
            format='s16p',
            layout=layout,
            rate=44100  # Standard rate
        )

        decoded_frame_count = 0
        try:
            for frame in container.decode(audio=0):
                if self.isInterruptionRequested():
                    raise _WaveformExtractionDeferred()
                # Resample frame
                resampled_frames = resampler.resample(frame)
                if resampled_frames is None:
                    continue
                if not isinstance(resampled_frames, (list, tuple)):
                    resampled_frames = [resampled_frames]
                for resampled in resampled_frames:
                    if self.isInterruptionRequested():
                        raise _WaveformExtractionDeferred()
                    samples = resampled.to_ndarray()

                    # Handle multi-channel audio
                    if samples.ndim > 1 and samples.shape[0] >= num_channels:
                        # samples shape: (channels, samples_per_frame)
                        # For stereo: shape (2, N)
                        num_frame_channels = samples.shape[0]
                    elif num_channels > 1 and samples.ndim == 2 and samples.shape[0] == 1:
                        # Defensive compatibility for decoders that still
                        # return packed interleaved data despite s16p.
                        packed = samples.reshape(-1)
                        usable = (packed.size // num_channels) * num_channels
                        samples = packed[:usable].reshape(-1, num_channels).T
                        num_frame_channels = num_channels
                    else:
                        # Mono: shape (N,)
                        num_frame_channels = 1
                        samples = samples.reshape(1, -1)  # Make 2D for uniform processing

                    # Normalize to -1.0 to 1.0
                    samples = samples.astype(np.float32) / 32768.0
                    # Vectorized bucketing happens inside the aggregator; the
                    # former per-frame np.minimum.at scatter dominated the
                    # extraction cost for long clips.
                    aggregator.add(samples[:min(num_frame_channels, num_channels)])
                decoded_frame_count += 1
                # Cooperative CPU budget: decoding remains asynchronous and
                # low priority, but amortising this sleep over a packet batch
                # removes the former per-packet throughput penalty.
                if decoded_frame_count % WAVEFORM_DECODE_YIELD_EVERY_FRAMES == 0:
                    self.msleep(WAVEFORM_DECODE_YIELD_MS)

        except _WaveformExtractionDeferred:
            container.close()
            raise
        except Exception as e:
            logger.warning(f"Audio decode error: {e}")

        container.close()

        result = aggregator.finish()
        if result is None:
            return None
        peaks_min, peaks_max, samples_per_second, final_bucket_count = result

        decoded_duration_seconds = float(aggregator.total_samples) / 44100.0
        duration_seconds = max(duration_seconds, decoded_duration_seconds)

        # Create WaveformData with channel count
        waveform = WaveformData(
            asset_id=self._asset_id,
            peaks_min=peaks_min,
            peaks_max=peaks_max,
            samples_per_second=samples_per_second,
            duration_seconds=duration_seconds,
            channels=num_channels,
            is_complete=True
        )

        logger.debug(f"Extracted waveform for {self._asset_id}: {final_bucket_count} samples per channel, {num_channels} channels")
        return waveform


# ================================================================
# WaveformExtractor - Manager Class
# ================================================================

class WaveformExtractor(QObject):
    """
    Manager for waveform extraction workers.

    Handles multiple concurrent extractions and notifies callbacks.
    Singleton pattern for global access.
    """

    _instance: Optional[WaveformExtractor] = None

    waveform_ready = pyqtSignal(str)  # asset_id - emit when waveform is ready
    extraction_failed = pyqtSignal(str, str)  # asset_id, error_message
    status_changed = pyqtSignal(str, int)  # message, timeout_ms (0=persistent)

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._application = QCoreApplication.instance()
        self._workers: Dict[str, WaveformExtractorWorker] = {}
        self._callbacks: Dict[str, list[Callable[[WaveformData], None]]] = {}
        self._queued_requests: OrderedDict[str, str] = OrderedDict()
        self._deferred_ready: OrderedDict[str, WaveformData] = OrderedDict()
        self._playback_active: bool = False
        # AUD-15605: cancel_all 超时后仍在运行的 worker 移入托管表，保住
        # Python 引用直到 finished 触发（防 QThread wrapper 被 GC 时线程仍在跑）。
        self._detached_workers: Dict[str, WaveformExtractorWorker] = {}
        # AUD-15607: 每个 asset 的请求代际。worker 失败信号（排队投递）送达时，
        # 只有"当前代际 == 失败代际"才清回调——否则说明失败后同一 asset 已有
        # 新请求注册了新回调，绝不能把新回调一并清掉（丢通知竞态）。
        self._request_generations: Dict[str, int] = {}

    def set_playback_active(self, active: bool) -> None:
        """Defer new waveform decoding while timeline playback has priority."""
        self._playback_active = bool(active)
        if self._playback_active:
            # Cooperatively stop the current low-priority decode at the next
            # decoded frame. This is intentionally not terminate(): PyAV must
            # unwind and close its container cleanly.
            from PyQt6 import sip

            for asset_id, worker in tuple(self._workers.items()):
                if not isinstance(worker, WaveformExtractorWorker):
                    # Defensive against stale/injected entries: production
                    # workers always have this concrete QThread type.
                    self._workers.pop(asset_id, None)
                    continue
                # A previous QApplication/test session can process a queued
                # deleteLater() after the Python wrapper was retained. Calling
                # a QThread method through that dead wrapper may crash inside
                # Qt before Python can raise RuntimeError.
                if isinstance(worker, QObject) and sip.isdeleted(worker):
                    self._workers.pop(asset_id, None)
                    continue
                worker.requestInterruption()
            if self._workers or self._queued_requests:
                self.status_changed.emit("波形生成已暂停，正在优先保证播放流畅度", 0)
            return
        if not self._playback_active:
            self._publish_deferred_waveforms()
            self._start_next_queued_worker()

    @classmethod
    def instance(cls, parent: Optional[QObject] = None) -> WaveformExtractor:
        """Get singleton instance."""
        from PyQt6 import sip

        current_application = QCoreApplication.instance()
        if (
            cls._instance is None
            or sip.isdeleted(cls._instance)
            or cls._instance._application is not current_application
        ):
            cls._instance = cls(parent)
        return cls._instance

    def request_waveform(
        self,
        asset_id: str,
        file_path: str,
        callback: Optional[Callable[[WaveformData], None]] = None
    ) -> None:
        """
        Request waveform extraction for asset.

        If already cached, calls callback immediately.
        If pending, adds callback to wait list.
        Otherwise, starts background extraction.

        Args:
            asset_id: Asset ID
            file_path: Audio file path
            callback: Optional callback when waveform is ready
        """
        cache = WaveformCache()

        # Check if already cached
        cached = cache.get_cached_waveform(asset_id)
        if cached:
            if callback:
                callback(cached)
            return

        # Check if pending
        if cache.is_pending(asset_id):
            if callback:
                # Add to callback list
                if asset_id not in self._callbacks:
                    self._callbacks[asset_id] = []
                self._callbacks[asset_id].append(callback)
            return

        # Start new extraction
        cache.set_pending(asset_id)

        if callback:
            self._callbacks[asset_id] = [callback]

        # AUD-15607: 新请求 = 新代际；_start_worker 把该代际绑定到失败处理器，
        # 旧的排队失败信号不会误清新请求刚注册的回调。
        self._request_generations[asset_id] = (
            self._request_generations.get(asset_id, 0) + 1
        )

        if (self._playback_active
                or len(self._workers) >= MAX_CONCURRENT_WAVEFORM_EXTRACTIONS):
            # Keep only one queued request per asset; callers share the same
            # pending flag/callback list and do not create duplicate decoders.
            self._queued_requests[asset_id] = file_path
            if self._playback_active:
                self.status_changed.emit("波形正在排队，将在停止播放后生成", 0)
            logger.debug("Queued waveform extraction for %s", asset_id)
            return

        self._start_worker(asset_id, file_path)

    def _start_worker(self, asset_id: str, file_path: str) -> None:
        """Start one low-priority waveform decoder on the GUI thread."""
        worker = WaveformExtractorWorker(asset_id, file_path)
        worker.waveform_ready.connect(self._on_waveform_ready)
        # AUD-15607: 失败处理器携带发起时的请求代际；信号排队送达时据此判断
        # 是否仍有权清除该 asset 的回调表。
        generation = self._request_generations.get(asset_id, 0)
        worker.extraction_failed.connect(
            lambda asset_id_, error, generation_=generation:
                self._on_extraction_failed(asset_id_, error, generation_)
        )
        worker.extraction_deferred.connect(self._on_extraction_deferred)
        # The result/failure signals are emitted from ``run()`` before the
        # QThread has necessarily returned from ``run``.  Keep the worker
        # alive until Qt emits ``finished``; deleting it from one of the
        # result handlers can destroy a still-running QThread.
        worker.finished.connect(
            lambda asset_id=asset_id: self._on_worker_finished(asset_id)
        )

        self._workers[asset_id] = worker
        worker.start(QThread.Priority.LowPriority)
        self.status_changed.emit("波形正在生成中…", 0)

        logger.debug(f"Started waveform extraction for {asset_id}")

    def _start_next_queued_worker(self) -> None:
        """Start the next request only after an active decoder has finished."""
        if self._playback_active:
            return
        while (len(self._workers) < MAX_CONCURRENT_WAVEFORM_EXTRACTIONS
               and self._queued_requests):
            asset_id, file_path = self._queued_requests.popitem(last=False)
            # A project/cache change may have satisfied or cancelled this work
            # while it was queued.
            if WaveformCache().get_cached_waveform(asset_id) is not None:
                WaveformCache().clear_pending(asset_id)
                continue
            if not WaveformCache().is_pending(asset_id):
                continue
            self._start_worker(asset_id, file_path)
            break

    def _on_waveform_ready(self, asset_id: str, waveform: WaveformData) -> None:
        """Handle waveform ready from worker without interrupting interaction."""
        # ``waveform_ready`` is emitted from ``run()`` before the QThread has
        # emitted ``finished``.  Do not pop/delete the worker here.  The
        # finished handler owns the cleanup and queue advancement.
        if self._playback_active:
            self._deferred_ready[asset_id] = waveform
            self.status_changed.emit("波形已缓存，将在预览结束后显示", 0)
            return

        self._publish_waveform_ready(asset_id, waveform)

    def _publish_waveform_ready(self, asset_id: str, waveform: WaveformData) -> None:
        """Deliver one cached waveform result to UI listeners."""
        # Call all registered callbacks
        callbacks = self._callbacks.pop(asset_id, [])
        for callback in callbacks:
            try:
                callback(waveform)
            except Exception as e:
                logger.warning(f"Waveform callback error: {e}")

        # Emit signal for UI updates
        self.waveform_ready.emit(asset_id)
        self.status_changed.emit("波形生成完成", 2000)

    def _publish_deferred_waveforms(self) -> None:
        """Publish completion notifications only after playback/scrubbing ends."""
        while self._deferred_ready and not self._playback_active:
            asset_id, waveform = self._deferred_ready.popitem(last=False)
            self._publish_waveform_ready(asset_id, waveform)

    def _on_extraction_deferred(self, asset_id: str, file_path: str) -> None:
        """Requeue interrupted work without treating it as a failed asset."""
        if WaveformCache().is_pending(asset_id):
            self._queued_requests[asset_id] = file_path

    def _on_extraction_failed(
        self,
        asset_id: str,
        error: str,
        generation: Optional[int] = None,
    ) -> None:
        """Handle extraction failure.

        AUD-15607: worker 在 run() 里先清 pending 再 emit（信号排队投递）。
        信号送达前同一 asset 的新 request_waveform 可能已注册新回调——
        此刻其代际已递增，本处理器（旧代际）必须保留新回调，只上报失败。
        ``generation=None``（直接调用/旧接线）保持旧行为：无条件清回调。
        """
        if error == "No audio data found":
            logger.info(f"No waveform available for {asset_id}: {error}")
        else:
            logger.error(f"Waveform extraction failed for {asset_id}: {error}")

        self.extraction_failed.emit(asset_id, error)

        if generation is not None:
            current_generation = self._request_generations.get(asset_id, 0)
            if current_generation != generation:
                logger.debug(
                    "Waveform failure for %s (generation %s) superseded by "
                    "newer request (generation %s); keeping its callbacks",
                    asset_id, generation, current_generation,
                )
                return

        # Clear callbacks
        self._callbacks.pop(asset_id, None)

        # The worker is cleaned up by ``_on_worker_finished``.  This handler
        # can run while ``run()`` is still unwinding, so deleting it here is a
        # QThread lifetime violation.

    def _on_worker_finished(self, asset_id: str) -> None:
        """Release a waveform worker only after its native thread stopped."""
        worker = self._workers.pop(asset_id, None)
        if worker is None:
            # AUD-15605: cancel_all 超时托管的 worker 也由 finished 统一回收。
            worker = self._detached_workers.pop(asset_id, None)
        if worker is not None:
            # At this point QThread::finished has fired, so deleteLater() is
            # safe and cannot hit the "QThread destroyed while running"
            # fatal path.
            worker.deleteLater()
        if asset_id not in self._queued_requests:
            # AUD-15607: 请求终结且未再排队 → 代际记录一并释放。
            self._request_generations.pop(asset_id, None)
        self._start_next_queued_worker()

    def cancel_all(self) -> None:
        """Cancel all pending extractions."""
        # Never use terminate() on a worker which owns a PyAV container.  It
        # can bypass the container's close path and leave native decoder state
        # half-destroyed.  The extraction loop checks interruption requests
        # cooperatively and closes its container before returning.
        workers = tuple(self._workers.values())
        for worker in workers:
            if worker.isRunning():
                worker.requestInterruption()
        for worker in workers:
            if not worker.isRunning():
                continue
            # AUD-15605: 有界等待。worker 可能阻塞在 av.open()/底层 IO（慢速
            # 网络源），无界 wait() 会把 GUI 线程（关闭项目/退出路径）冻结到
            # FFmpeg 自身 IO 超时。超时则转为后台托管：保住引用、交给已连接
            # 的 finished 处理器回收，不 terminate。
            if not worker.wait(WAVEFORM_CANCEL_WAIT_MS):
                logger.warning(
                    "Waveform worker did not stop within %d ms; leaving it "
                    "to finish in the background",
                    WAVEFORM_CANCEL_WAIT_MS,
                )
                self._detached_workers[worker._asset_id] = worker

        self._workers.clear()
        self._queued_requests.clear()
        self._deferred_ready.clear()
        self._callbacks.clear()

        # Clear all pending flags
        cache = WaveformCache()
        cache._mutex.lock()
        try:
            cache._pending.clear()
        finally:
            cache._mutex.unlock()


# ================================================================
# Clip-Effect Waveform - offline per-clip rendering
# ================================================================

# Distinct from real failures: playback preempted the render and the request
# must be re-queued instead of reported to the UI as an error.
EFFECT_WAVEFORM_DEFERRED_ERROR = "__deferred__"


class ClipEffectWaveformWorker(QThread):
    """Render the effect-processed envelope of one clip's source range.

    Decodes only [start_seconds, start_seconds + duration_seconds], streams
    the samples through the same ``EffectChain`` DSP the realtime engine
    uses, and aggregates min/max peaks with the shared vectorized
    aggregator. The clip FX chain is stateful, so processing always starts
    at the clip in-point (skipped samples never enter the chain), matching
    realtime playback semantics.
    """

    effect_waveform_ready = pyqtSignal(str, object)  # clip_id, WaveformData
    effect_waveform_failed = pyqtSignal(str, str)  # clip_id, error

    _SAMPLE_RATE = 48000.0  # Must match AudioPlaybackManager.DEFAULT_SAMPLE_RATE

    def __init__(
        self,
        clip_id: str,
        file_path: str,
        start_seconds: float,
        duration_seconds: float,
        entries: Optional[list],
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self._clip_id = clip_id
        self._file_path = file_path
        self._start_seconds = max(0.0, float(start_seconds))
        self._duration_seconds = max(0.04, float(duration_seconds))
        self._entries = list(entries) if entries else []

    def run(self) -> None:
        try:
            waveform = self._render_effect_waveform()
            if self.isInterruptionRequested():
                raise _WaveformExtractionDeferred()
            if waveform is not None:
                self.effect_waveform_ready.emit(self._clip_id, waveform)
            else:
                self.effect_waveform_failed.emit(self._clip_id, "No audio data found")
        except _WaveformExtractionDeferred:
            self.effect_waveform_failed.emit(
                self._clip_id, EFFECT_WAVEFORM_DEFERRED_ERROR)
        except Exception as e:
            logger.error(f"Clip effect waveform failed for {self._clip_id}: {e}")
            self.effect_waveform_failed.emit(self._clip_id, str(e))

    def _render_effect_waveform(self) -> Optional[WaveformData]:
        import av

        from src.features.edit.core.audio_dsp import EffectChain

        try:
            container = av.open(self._file_path)
        except Exception as e:
            logger.error(f"Failed to open audio file: {self._file_path}: {e}")
            return None

        audio_stream = None
        for stream in container.streams:
            if stream.type == 'audio':
                audio_stream = stream
                break
        if audio_stream is None:
            container.close()
            return None

        num_channels = audio_stream.channels if hasattr(audio_stream, 'channels') else 1
        num_channels = max(1, min(num_channels, 2))

        layout = 'stereo' if num_channels == 2 else 'mono'
        resampler = av.AudioResampler(
            format='s16p', layout=layout, rate=int(self._SAMPLE_RATE))

        aggregator = _PeakAggregator(
            channels=num_channels,
            max_buckets=MAX_WAVEFORM_SAMPLES,
            samples_per_second=WAVEFORM_SAMPLES_PER_SECOND,
            sample_rate=self._SAMPLE_RATE,
            estimated_duration_seconds=self._duration_seconds,
        )
        chain = EffectChain(self._SAMPLE_RATE)
        chain.set_params(self._entries, sample_rate=self._SAMPLE_RATE)

        start_s = self._start_seconds
        end_s = self._start_seconds + self._duration_seconds

        try:
            # Seek to the clip in-point; audio packets are all keyframes so the
            # landing point is within one frame of the request.
            try:
                seek_ts = int(start_s / float(audio_stream.time_base))
                container.seek(max(0, seek_ts), stream=audio_stream)
            except Exception:
                pass  # Unseekable source: decode from the beginning instead

            decoded_frame_count = 0
            for frame in container.decode(audio=0):
                if self.isInterruptionRequested():
                    raise _WaveformExtractionDeferred()
                try:
                    t0 = float(frame.pts) * float(frame.time_base)
                except (TypeError, ValueError):
                    t0 = None
                if t0 is not None and t0 >= end_s:
                    break
                resampled_frames = resampler.resample(frame)
                if resampled_frames is None:
                    continue
                if not isinstance(resampled_frames, (list, tuple)):
                    resampled_frames = [resampled_frames]
                for resampled in resampled_frames:
                    samples = resampled.to_ndarray()
                    if samples.ndim != 2 or samples.shape[1] <= 0:
                        continue
                    samples = samples[:num_channels].astype(np.float32) / 32768.0
                    frame_n = int(samples.shape[1])
                    if t0 is not None:
                        # Slice to the exact clip range in the 48 kHz domain;
                        # frames crossing the in/out points contribute only
                        # their visible part.
                        lo = max(0, int(round((start_s - t0) * self._SAMPLE_RATE)))
                        hi = min(frame_n, int(math.ceil((end_s - t0) * self._SAMPLE_RATE)))
                        if hi <= lo:
                            continue
                        if lo > 0 or hi < frame_n:
                            samples = samples[:, lo:hi]
                    # Stateful DSP: every visible sample passes the chain in
                    # order so compressor/reverb tails match realtime playback.
                    samples = chain.process(samples)
                    samples = np.asarray(samples, dtype=np.float32)
                    if samples.ndim == 1:
                        samples = samples.reshape(1, -1)
                    aggregator.add(samples[:num_channels])
                decoded_frame_count += 1
                if decoded_frame_count % WAVEFORM_DECODE_YIELD_EVERY_FRAMES == 0:
                    self.msleep(WAVEFORM_DECODE_YIELD_MS)
        except _WaveformExtractionDeferred:
            container.close()
            raise
        except Exception as e:
            logger.warning(f"Audio decode error (clip FX waveform): {e}")

        container.close()

        result = aggregator.finish()
        if result is None:
            return None
        peaks_min, peaks_max, samples_per_second, _ = result
        return WaveformData(
            asset_id=self._clip_id,
            peaks_min=peaks_min,
            peaks_max=peaks_max,
            samples_per_second=samples_per_second,
            duration_seconds=self._duration_seconds,
            channels=num_channels,
            is_complete=True,
        )


class ClipEffectWaveformRenderer(QObject):
    """Serialized background manager for clip-effect waveform renders.

    One worker at a time at low priority, deferred while timeline playback
    has CPU priority (same policy as WaveformExtractor). The latest request
    per clip wins: parameter tweaks simply replace the queued entry.
    """

    _instance: Optional["ClipEffectWaveformRenderer"] = None

    effect_waveform_ready = pyqtSignal(str, object)  # clip_id, WaveformData
    effect_waveform_failed = pyqtSignal(str, str)  # clip_id, error

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._application = QCoreApplication.instance()
        self._worker: Optional[ClipEffectWaveformWorker] = None
        # clip_id -> (file_path, start_seconds, duration_seconds, entries)
        self._queued: OrderedDict[str, tuple] = OrderedDict()
        self._playback_active: bool = False

    @classmethod
    def instance(cls, parent: Optional[QObject] = None) -> "ClipEffectWaveformRenderer":
        from PyQt6 import sip

        current_application = QCoreApplication.instance()
        if (
            cls._instance is None
            or sip.isdeleted(cls._instance)
            or cls._instance._application is not current_application
        ):
            cls._instance = cls(parent)
        return cls._instance

    def set_playback_active(self, active: bool) -> None:
        """Defer clip-FX waveform rendering while playback has priority."""
        self._playback_active = bool(active)
        if self._playback_active:
            worker = self._worker
            if worker is not None:
                worker.requestInterruption()
            return
        self._start_next()

    def request_effect_waveform(
        self,
        clip_id: str,
        file_path: str,
        start_seconds: float,
        duration_seconds: float,
        entries: Optional[list],
    ) -> None:
        self._queued[clip_id] = (
            file_path, float(start_seconds), float(duration_seconds), entries)
        if not self._playback_active:
            self._start_next()

    def _start_next(self) -> None:
        if self._worker is not None or self._playback_active or not self._queued:
            return
        clip_id, (file_path, start_s, dur_s, entries) = self._queued.popitem(last=False)
        worker = ClipEffectWaveformWorker(
            clip_id, file_path, start_s, dur_s, entries)
        worker.effect_waveform_ready.connect(self._on_ready)
        worker.effect_waveform_failed.connect(self._on_failed)
        # Keep the worker alive until Qt emits ``finished`` (same lifetime
        # rule as the waveform extraction workers).
        worker.finished.connect(
            lambda cid=clip_id: self._on_worker_finished(cid))
        self._worker = worker
        worker.start(QThread.Priority.LowPriority)

    def _on_ready(self, clip_id: str, waveform: WaveformData) -> None:
        self.effect_waveform_ready.emit(clip_id, waveform)

    def _on_failed(self, clip_id: str, error: str) -> None:
        if error == EFFECT_WAVEFORM_DEFERRED_ERROR:
            # Playback preempted the render; drop it (the next paint after
            # playback re-requests if the effect is still applied).
            logger.debug(f"Clip effect waveform deferred for {clip_id}")
            return
        self.effect_waveform_failed.emit(clip_id, error)

    def _on_worker_finished(self, clip_id: str) -> None:
        worker = self._worker
        self._worker = None
        if worker is not None:
            worker.deleteLater()
        self._start_next()


def request_effect_waveform(
    clip_id: str,
    file_path: str,
    start_seconds: float,
    duration_seconds: float,
    entries: Optional[list],
) -> None:
    """Convenience wrapper: queue one clip-effect waveform render."""
    ClipEffectWaveformRenderer.instance().request_effect_waveform(
        clip_id, file_path, start_seconds, duration_seconds, entries)


# ================================================================
# Convenience Functions
# ================================================================

def request_waveform_async(
    asset_id: str,
    file_path: str,
    callback: Optional[Callable[[WaveformData], None]] = None,
    parent: Optional[QObject] = None
) -> None:
    """
    Request waveform extraction asynchronously.

    Convenience function using singleton WaveformExtractor.

    Args:
        asset_id: Asset ID
        file_path: Audio file path
        callback: Optional callback when waveform is ready
        parent: Optional parent QObject for extractor
    """
    extractor = WaveformExtractor.instance(parent)
    extractor.request_waveform(asset_id, file_path, callback)


def get_cached_waveform(asset_id: str) -> Optional[WaveformData]:
    """
    Get cached waveform for asset.

    Convenience function using singleton WaveformCache.

    Args:
        asset_id: Asset ID

    Returns:
        WaveformData if cached, None otherwise
    """
    return WaveformCache().get_cached_waveform(asset_id)


def build_range_cache_key(
    asset_id: str,
    source_start: float,
    source_duration: float,
    display_width: int
) -> str:
    """
    Build a source-range-aware cache key for waveform data.

    Convenience function using WaveformCache.

    Args:
        asset_id: Asset ID
        source_start: Start time in seconds or frame-based offset
        source_duration: Duration in seconds or frame-based count
        display_width: Display width in pixels

    Returns:
        Cache key string incorporating all range parameters
    """
    return WaveformCache.build_range_cache_key(asset_id, source_start, source_duration, display_width)


def get_ranged_waveform(cache_key: str) -> Optional[tuple[np.ndarray, np.ndarray]]:
    """
    Get cached ranged waveform (pre-resampled peaks).

    Convenience function using singleton WaveformCache.

    Args:
        cache_key: Key from build_range_cache_key()

    Returns:
        Tuple of (peaks_min, peaks_max) arrays if cached, None otherwise
    """
    return WaveformCache().get_ranged_waveform(cache_key)


def cache_ranged_waveform(
    cache_key: str,
    peaks_min: np.ndarray,
    peaks_max: np.ndarray
) -> None:
    """
    Cache pre-resampled ranged waveform data.

    Convenience function using singleton WaveformCache.

    Args:
        cache_key: Key from build_range_cache_key()
        peaks_min: Minimum peaks array
        peaks_max: Maximum peaks array
    """
    WaveformCache().cache_ranged_waveform(cache_key, peaks_min, peaks_max)


def is_waveform_pending(asset_id: str) -> bool:
    """
    Check if waveform extraction is pending.

    Args:
        asset_id: Asset ID

    Returns:
        True if extraction is pending
    """
    return WaveformCache().is_pending(asset_id)


# ================================================================
# Placeholder Waveform Generator
# ================================================================

def generate_placeholder_waveform(
    asset_id: str,
    duration_seconds: float,
    num_samples: int = 100,
    channels: int = 1
) -> WaveformData:
    """
    Generate placeholder waveform for testing or fallback.

    Creates pseudo-random peaks based on asset_id hash.

    Args:
        asset_id: Asset ID (used for deterministic generation)
        duration_seconds: Duration of audio
        num_samples: Number of samples to generate (per channel)
        channels: Number of audio channels (1=mono, 2=stereo)

    Returns:
        WaveformData with generated peaks
        For stereo, returns interleaved data: [L0, R0, L1, R1, ...]
    """
    seed = hash(asset_id) % 10000

    np.random.seed(seed)

    # Generate interleaved samples for stereo
    total_samples = num_samples * channels
    peaks_max = np.zeros(total_samples)
    peaks_min = np.zeros(total_samples)

    for ch in range(channels):
        # Generate slightly different data for each channel
        channel_seed = seed + ch * 1000
        np.random.seed(channel_seed)
        ch_peaks_max = np.random.uniform(0.2, 0.8, num_samples)
        ch_peaks_min = -ch_peaks_max * np.random.uniform(0.6, 1.0, num_samples)

        # Place in interleaved array
        for i in range(num_samples):
            interleaved_idx = i * channels + ch
            peaks_max[interleaved_idx] = ch_peaks_max[i]
            peaks_min[interleaved_idx] = ch_peaks_min[i]

    return WaveformData(
        asset_id=asset_id,
        peaks_min=peaks_min,
        peaks_max=peaks_max,
        samples_per_second=num_samples / duration_seconds if duration_seconds > 0 else 100,
        duration_seconds=duration_seconds,
        channels=channels,
        is_complete=False  # Mark as placeholder
    )
