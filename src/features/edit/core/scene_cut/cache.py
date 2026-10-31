"""
Scene Cut Detection Cache System.

Implements caching for scene cut analysis results and thumbnails,
with proper cache key generation and invalidation.

## Responsibilities

- Cache key generation including: path, size, mtime_ns, asset_id,
  source range, settings hash, detector_version, proxy fingerprint
- JSON serialization of SceneCutAnalysis (no pickle)
- Analysis cache directory: {project cache}/scene_cut/analyses/
- Thumbnail cache directory: {project cache}/scene_cut/thumbs/
- LRU management for thumbnails
- Cache invalidation on file/settings/algorithm changes

## Design Principles

- JSON serialization only (no pickle for security/portability)
- Complete type annotations
- Thread-safe operations
- Cache keys are stable and collision-resistant
- LRU eviction for memory management

## Reference

- docs/resolve_like_scene_cut_detection_implementation_plan_2026-06-04.md Section 7 (P3)
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .models import (
    DETECTOR_VERSION,
    SceneCutAnalysis,
    SceneCutSettings,
)

logger = logging.getLogger(__name__)


# =============================================================================
# Cache Key Generation
# =============================================================================

@dataclass(frozen=True)
class MediaFingerprint:
    """
    Fingerprint for media file identification.

    Used for cache invalidation when media changes.

    Attributes:
        path: Source file path.
        size: File size in bytes.
        mtime_ns: Modification time in nanoseconds.
        asset_id: Asset identifier in project.
    """

    path: str
    size: int
    mtime_ns: int
    asset_id: str

    def to_hash(self) -> str:
        """Generate stable hash for this fingerprint."""
        data = f"{self.path}|{self.size}|{self.mtime_ns}|{self.asset_id}"
        return hashlib.sha256(data.encode()).hexdigest()[:16]

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "path": self.path,
            "size": self.size,
            "mtime_ns": self.mtime_ns,
            "asset_id": self.asset_id,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> MediaFingerprint:
        """Create instance from dictionary."""
        return MediaFingerprint(
            path=data["path"],
            size=data["size"],
            mtime_ns=data["mtime_ns"],
            asset_id=data["asset_id"],
        )

    @staticmethod
    def from_path(path: str, asset_id: str) -> MediaFingerprint | None:
        """
        Create fingerprint from file path.

        Reads file metadata to generate fingerprint.

        Args:
            path: Source file path.
            asset_id: Asset identifier.

        Returns:
            MediaFingerprint or None if file not found.
        """
        try:
            stat = os.stat(path)
            return MediaFingerprint(
                path=path,
                size=stat.st_size,
                mtime_ns=stat.st_mtime_ns,
                asset_id=asset_id,
            )
        except FileNotFoundError:
            logger.warning(f"File not found for fingerprint: {path}")
            return None
        except OSError as e:
            logger.warning(f"Failed to stat file {path}: {e}")
            return None


@dataclass(frozen=True)
class CacheKeyComponents:
    """
    Components for generating a cache key.

    All components must be included to ensure cache validity.

    Attributes:
        media_fingerprint: Media file fingerprint.
        source_range_start: First source frame to analyze.
        source_range_end: Last source frame (None = end of media).
        settings_hash: Hash of detection settings.
        detector_version: Version string of detector algorithm.
        proxy_fingerprint: Fingerprint of proxy file (optional).
        clip_id: Clip ID for clip-specific analysis (optional).
    """

    media_fingerprint: str
    source_range_start: int
    source_range_end: int | None
    settings_hash: str
    detector_version: str
    proxy_fingerprint: str | None = None
    clip_id: str | None = None

    def generate_key(self) -> str:
        """
        Generate cache key from components.

        The key is a SHA256 hash truncated to 24 characters.

        Returns:
            Cache key string.
        """
        range_str = f"{self.source_range_start}-{self.source_range_end or 'end'}"
        proxy_str = self.proxy_fingerprint or "no-proxy"
        clip_str = self.clip_id or "full-media"

        data = (
            f"{self.media_fingerprint}"
            f"|{range_str}"
            f"|{self.settings_hash}"
            f"|{self.detector_version}"
            f"|{proxy_str}"
            f"|{clip_str}"
        )
        return hashlib.sha256(data.encode()).hexdigest()[:24]


def generate_cache_key(
    path: str,
    size: int,
    mtime_ns: int,
    asset_id: str,
    settings: SceneCutSettings,
    clip_id: str | None = None,
    proxy_path: str | None = None,
    proxy_fingerprint: str | None = None,
) -> str:
    """
    Generate cache key for scene cut analysis.

    Args:
        path: Source file path.
        size: File size in bytes.
        mtime_ns: Modification time in nanoseconds.
        asset_id: Asset identifier.
        settings: Detection settings.
        clip_id: Clip ID for clip-specific analysis.
        proxy_path: Proxy file path (optional).
        proxy_fingerprint: Proxy file fingerprint (optional).

    Returns:
        Cache key string.
    """
    media_fp = MediaFingerprint(
        path=path,
        size=size,
        mtime_ns=mtime_ns,
        asset_id=asset_id,
    )

    # Generate proxy fingerprint if proxy path provided but no fingerprint
    actual_proxy_fp = proxy_fingerprint
    if proxy_path and not proxy_fingerprint:
        try:
            proxy_stat = os.stat(proxy_path)
            proxy_fp_data = f"{proxy_path}|{proxy_stat.st_size}|{proxy_stat.st_mtime_ns}"
            actual_proxy_fp = hashlib.sha256(proxy_fp_data.encode()).hexdigest()[:16]
        except OSError:
            actual_proxy_fp = "proxy-missing"

    components = CacheKeyComponents(
        media_fingerprint=media_fp.to_hash(),
        source_range_start=settings.start_source_frame,
        source_range_end=settings.end_source_frame,
        settings_hash=settings.to_hash(),
        detector_version=DETECTOR_VERSION,
        proxy_fingerprint=actual_proxy_fp,
        clip_id=clip_id,
    )

    return components.generate_key()


# =============================================================================
# Analysis Cache
# =============================================================================

class AnalysisCache:
    """
    Cache for scene cut analysis results.

    Stores analysis results as JSON files in a dedicated directory.

    Attributes:
        cache_dir: Base directory for scene cut cache.
        analyses_dir: Directory for analysis JSON files.

    Directory structure:
        {cache_dir}/scene_cut/
            analyses/
                <cache_key>.json
            thumbs/
                <cache_key>_<frame>_before.jpg
                <cache_key>_<frame>_after.jpg
    """

    def __init__(self, project_cache_dir: str | None = None) -> None:
        """
        Initialize analysis cache.

        Args:
            project_cache_dir: Project cache directory path.
                If None, uses default project cache location.
        """
        if project_cache_dir is None:
            from src.shared.paths.paths import get_cache_dir
            project_cache_dir = get_cache_dir()

        self._cache_dir = Path(project_cache_dir) / "scene_cut"
        self._analyses_dir = self._cache_dir / "analyses"
        self._thumbs_dir = self._cache_dir / "thumbs"

        # Create directories if needed
        self._ensure_directories()

    def _ensure_directories(self) -> None:
        """Create cache directories if they don't exist."""
        try:
            self._cache_dir.mkdir(parents=True, exist_ok=True)
            self._analyses_dir.mkdir(exist_ok=True)
            self._thumbs_dir.mkdir(exist_ok=True)
        except OSError as e:
            logger.warning(f"Failed to create cache directories: {e}")

    @property
    def cache_dir(self) -> Path:
        """Base cache directory."""
        return self._cache_dir

    @property
    def analyses_dir(self) -> Path:
        """Analysis JSON files directory."""
        return self._analyses_dir

    @property
    def thumbs_dir(self) -> Path:
        """Thumbnails directory."""
        return self._thumbs_dir

    # ==================== Analysis Operations ====================

    def get_analysis_path(self, cache_key: str) -> Path:
        """
        Get path to analysis JSON file.

        Args:
            cache_key: Cache key string.

        Returns:
            Path to analysis file.
        """
        return self._analyses_dir / f"{cache_key}.json"

    def has_analysis(self, cache_key: str) -> bool:
        """
        Check if cached analysis exists.

        Args:
            cache_key: Cache key string.

        Returns:
            True if analysis exists and is valid.
        """
        path = self.get_analysis_path(cache_key)
        if not path.exists():
            return False

        # Validate JSON structure
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            # Check required fields
            required = ["analysis_id", "asset_id", "source_path", "detector_version"]
            return all(k in data for k in required)
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"Invalid cache file {path}: {e}")
            return False

    def load_analysis(self, cache_key: str) -> SceneCutAnalysis | None:
        """
        Load cached analysis.

        Args:
            cache_key: Cache key string.

        Returns:
            SceneCutAnalysis or None if not found/invalid.
        """
        path = self.get_analysis_path(cache_key)
        if not path.exists():
            return None

        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return SceneCutAnalysis.from_dict(data)
        except json.JSONDecodeError as e:
            logger.error(f"JSON decode error in {path}: {e}")
            return None
        except ValueError as e:
            # Cache files are external data (hand-edited, older schema,
            # third-party writers): unknown enum values in from_dict and
            # out-of-range values in __post_init__ raise ValueError, which
            # must surface as "invalid → None" like the other malformed-
            # data cases instead of escaping to callers (AUD-17810).
            logger.error(f"Invalid analysis values in {path}: {e}")
            return None
        except (KeyError, TypeError) as e:
            logger.error(f"Invalid analysis data in {path}: {e}")
            return None
        except OSError as e:
            logger.error(f"Failed to read {path}: {e}")
            return None

    def save_analysis(self, analysis: SceneCutAnalysis) -> str:
        """
        Save analysis to cache.

        Args:
            analysis: SceneCutAnalysis to save.

        Returns:
            Cache key used for storage.
        """
        # Generate cache key from analysis
        cache_key = analysis.to_cache_key()
        path = self.get_analysis_path(cache_key)

        try:
            # Serialize to JSON
            data = analysis.to_dict()

            # Write with atomic replace
            temp_path = path.with_suffix(".tmp")
            with open(temp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)

            # Atomic rename
            temp_path.replace(path)

            logger.debug(f"Saved analysis {analysis.analysis_id} to {path}")
            return cache_key

        except OSError as e:
            logger.error(f"Failed to save analysis to {path}: {e}")
            raise RuntimeError(f"Failed to save analysis: {e}") from e

    def delete_analysis(self, cache_key: str) -> bool:
        """
        Delete cached analysis.

        Args:
            cache_key: Cache key string.

        Returns:
            True if deleted, False if not found.
        """
        path = self.get_analysis_path(cache_key)
        if not path.exists():
            return False

        try:
            path.unlink()
            logger.debug(f"Deleted analysis cache {cache_key}")
            return True
        except OSError as e:
            logger.warning(f"Failed to delete {path}: {e}")
            return False

    def invalidate_for_asset(self, asset_id: str) -> int:
        """
        Invalidate all cached analyses for an asset.

        Useful when asset is relinked or proxy changed.

        Args:
            asset_id: Asset identifier.

        Returns:
            Number of deleted cache files.
        """
        deleted = 0
        for path in self._analyses_dir.glob("*.json"):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if data.get("asset_id") == asset_id:
                    path.unlink()
                    deleted += 1
            except (json.JSONDecodeError, OSError) as e:
                logger.warning(f"Failed to invalidate {path}: {e}")

        if deleted > 0:
            logger.info(f"Invalidated {deleted} cache files for asset {asset_id}")
        return deleted

    def clear_all(self) -> int:
        """
        Clear all cached analyses.

        Returns:
            Number of deleted cache files.
        """
        deleted = 0
        for path in self._analyses_dir.glob("*.json"):
            try:
                path.unlink()
                deleted += 1
            except OSError as e:
                logger.warning(f"Failed to delete {path}: {e}")

        logger.info(f"Cleared {deleted} analysis cache files")
        return deleted

    # ==================== Validation ====================

    def validate_analysis(self, analysis: SceneCutAnalysis, path: str) -> bool:
        """
        Validate cached analysis against current media state.

        Args:
            analysis: Cached analysis.
            path: Current media file path.

        Returns:
            True if analysis is still valid, False if invalid.
        """
        # Check detector version
        if analysis.detector_version != DETECTOR_VERSION:
            logger.debug(f"Detector version mismatch: {analysis.detector_version} != {DETECTOR_VERSION}")
            return False

        # Check media fingerprint
        try:
            stat = os.stat(path)
            media_fp = MediaFingerprint(
                path=path,
                size=stat.st_size,
                mtime_ns=stat.st_mtime_ns,
                asset_id=analysis.asset_id,
            )
            if analysis.media_fingerprint != media_fp.to_hash():
                logger.debug(f"Media fingerprint mismatch for {path}")
                return False
        except OSError:
            logger.debug(f"Cannot validate: file not found {path}")
            return False

        return True

    def is_cache_valid(
        self,
        cache_key: str,
        path: str,
        settings: SceneCutSettings,
    ) -> bool:
        """
        Check if cached result is valid for current settings.

        Args:
            cache_key: Cache key to check.
            path: Media file path.
            settings: Current detection settings.

        Returns:
            True if cache is valid, False if needs refresh.
        """
        analysis = self.load_analysis(cache_key)
        if analysis is None:
            return False

        return self.validate_analysis(analysis, path)


# =============================================================================
# Thumbnail Cache with LRU
# =============================================================================

@dataclass
class ThumbnailEntry:
    """
    Entry in thumbnail cache.

    Attributes:
        cache_key: Analysis cache key.
        source_frame: Source frame number.
        kind: "before" or "after" cut.
        path: Path to thumbnail file.
        size_bytes: File size.
        last_access: Last access timestamp.
    """

    cache_key: str
    source_frame: int
    kind: str  # "before" or "after"
    path: Path
    size_bytes: int
    last_access: float = field(default_factory=time.time)

    def access(self) -> None:
        """Update last access time."""
        self.last_access = time.time()


class ThumbnailCache:
    """
    LRU cache for scene cut thumbnails.

    Manages before/after thumbnails for each candidate cut.

    Attributes:
        max_total_size: Maximum total cache size in bytes.
        max_entries: Maximum number of thumbnail entries.

    Default limits:
        - 500 MB total size
        - 10000 max entries
    """

    DEFAULT_MAX_SIZE = 500 * 1024 * 1024  # 500 MB
    DEFAULT_MAX_ENTRIES = 10000

    def __init__(
        self,
        thumbs_dir: Path | None = None,
        max_total_size: int = DEFAULT_MAX_SIZE,
        max_entries: int = DEFAULT_MAX_ENTRIES,
    ) -> None:
        """
        Initialize thumbnail cache.

        Args:
            thumbs_dir: Directory for thumbnail files.
            max_total_size: Maximum total size in bytes.
            max_entries: Maximum number of entries.
        """
        if thumbs_dir is None:
            from src.shared.paths.paths import get_cache_dir
            cache_dir = Path(get_cache_dir()) / "scene_cut"
            thumbs_dir = cache_dir / "thumbs"

        self._thumbs_dir = thumbs_dir
        self._max_total_size = max_total_size
        self._max_entries = max_entries

        # LRU tracking: OrderedDict maintains insertion order
        self._entries: OrderedDict[str, ThumbnailEntry] = OrderedDict()
        self._current_size: int = 0

        # AUD-17812：落实模块 docstring "Thread-safe operations" 的声明——
        # _entries/_current_size 的所有读写（含 LRU move_to_end 与淘汰）
        # 统一经这把 RLock 串行化（检测 worker 与 UI 线程并发访问）。
        self._lock = threading.RLock()

        # Create directory
        self._ensure_directory()

        # Scan existing thumbnails
        self._scan_existing()

    def _ensure_directory(self) -> None:
        """Create thumbnail directory if needed."""
        try:
            self._thumbs_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            logger.warning(f"Failed to create thumbs directory: {e}")

    def _scan_existing(self) -> None:
        """Scan existing thumbnail files and add to tracking."""
        try:
            for path in self._thumbs_dir.glob("*.jpg"):
                # Parse filename: <cache_key>_<frame>_<kind>.jpg
                name = path.stem
                parts = name.split("_")
                if len(parts) >= 3:
                    cache_key = parts[0]
                    try:
                        source_frame = int(parts[1])
                    except ValueError:
                        continue
                    kind = parts[2]  # "before" or "after"

                    size = path.stat().st_size
                    entry_key = f"{cache_key}_{source_frame}_{kind}"
                    entry = ThumbnailEntry(
                        cache_key=cache_key,
                        source_frame=source_frame,
                        kind=kind,
                        path=path,
                        size_bytes=size,
                    )
                    self._entries[entry_key] = entry
                    self._current_size += size

        except OSError as e:
            logger.warning(f"Failed to scan thumbnails: {e}")

    @property
    def thumbs_dir(self) -> Path:
        """Thumbnail directory."""
        return self._thumbs_dir

    @property
    def current_size(self) -> int:
        """Current total size in bytes."""
        with self._lock:
            return self._current_size

    @property
    def entry_count(self) -> int:
        """Number of cached entries."""
        with self._lock:
            return len(self._entries)

    # ==================== Thumbnail Operations ====================

    def _make_entry_key(self, cache_key: str, source_frame: int, kind: str) -> str:
        """Generate entry key for tracking."""
        return f"{cache_key}_{source_frame}_{kind}"

    def _make_filename(self, cache_key: str, source_frame: int, kind: str) -> str:
        """Generate filename for thumbnail."""
        return f"{cache_key}_{source_frame}_{kind}.jpg"

    def get_thumb_path(self, cache_key: str, source_frame: int, kind: str) -> Path:
        """
        Get path to thumbnail file.

        Args:
            cache_key: Analysis cache key.
            source_frame: Source frame number.
            kind: "before" or "after".

        Returns:
            Path to thumbnail file.
        """
        filename = self._make_filename(cache_key, source_frame, kind)
        return self._thumbs_dir / filename

    def has_thumb(self, cache_key: str, source_frame: int, kind: str) -> bool:
        """
        Check if thumbnail exists.

        Args:
            cache_key: Analysis cache key.
            source_frame: Source frame number.
            kind: "before" or "after".

        Returns:
            True if thumbnail exists.
        """
        entry_key = self._make_entry_key(cache_key, source_frame, kind)
        with self._lock:
            if entry_key in self._entries:
                # Move to end for LRU (most recently accessed)
                self._entries.move_to_end(entry_key)
                self._entries[entry_key].access()
                return True

            # Check file directly
            path = self.get_thumb_path(cache_key, source_frame, kind)
            if path.exists():
                size = path.stat().st_size
                entry = ThumbnailEntry(
                    cache_key=cache_key,
                    source_frame=source_frame,
                    kind=kind,
                    path=path,
                    size_bytes=size,
                )
                self._entries[entry_key] = entry
                self._current_size += size
                return True

            return False

    def save_thumb(
        self,
        cache_key: str,
        source_frame: int,
        kind: str,
        image_data: bytes,
    ) -> Path:
        """
        Save thumbnail to cache.

        Args:
            cache_key: Analysis cache key.
            source_frame: Source frame number.
            kind: "before" or "after".
            image_data: JPEG image bytes.

        Returns:
            Path to saved thumbnail.
        """
        path = self.get_thumb_path(cache_key, source_frame, kind)
        entry_key = self._make_entry_key(cache_key, source_frame, kind)

        with self._lock:
            # Check if exists and remove old entry
            if entry_key in self._entries:
                old_entry = self._entries[entry_key]
                self._current_size -= old_entry.size_bytes
                del self._entries[entry_key]

            # Write file
            try:
                with open(path, "wb") as f:
                    f.write(image_data)
            except OSError as e:
                logger.error(f"Failed to save thumbnail {path}: {e}")
                raise RuntimeError(f"Failed to save thumbnail: {e}") from e

            # Add entry
            size = len(image_data)
            entry = ThumbnailEntry(
                cache_key=cache_key,
                source_frame=source_frame,
                kind=kind,
                path=path,
                size_bytes=size,
            )
            self._entries[entry_key] = entry
            self._current_size += size

            # Move to end (most recently accessed)
            self._entries.move_to_end(entry_key)

            # Evict if needed
            self._evict_if_needed()

        return path

    def load_thumb(
        self,
        cache_key: str,
        source_frame: int,
        kind: str,
    ) -> bytes | None:
        """
        Load thumbnail from cache.

        Args:
            cache_key: Analysis cache key.
            source_frame: Source frame number.
            kind: "before" or "after".

        Returns:
            JPEG image bytes or None if not found.
        """
        path = self.get_thumb_path(cache_key, source_frame, kind)
        if not path.exists():
            return None

        try:
            with open(path, "rb") as f:
                data = f.read()

            # Update LRU tracking
            entry_key = self._make_entry_key(cache_key, source_frame, kind)
            with self._lock:
                if entry_key in self._entries:
                    self._entries.move_to_end(entry_key)
                    self._entries[entry_key].access()

            return data

        except OSError as e:
            logger.warning(f"Failed to load thumbnail {path}: {e}")
            return None

    def delete_thumb(self, cache_key: str, source_frame: int, kind: str) -> bool:
        """
        Delete single thumbnail.

        Args:
            cache_key: Analysis cache key.
            source_frame: Source frame number.
            kind: "before" or "after".

        Returns:
            True if deleted.
        """
        path = self.get_thumb_path(cache_key, source_frame, kind)
        entry_key = self._make_entry_key(cache_key, source_frame, kind)

        with self._lock:
            # Remove from tracking
            if entry_key in self._entries:
                self._current_size -= self._entries[entry_key].size_bytes
                del self._entries[entry_key]

            # Delete file
            if path.exists():
                try:
                    path.unlink()
                    return True
                except OSError as e:
                    logger.warning(f"Failed to delete {path}: {e}")
                    return False

            return False

    def delete_thumbs_for_analysis(self, cache_key: str) -> int:
        """
        Delete all thumbnails for an analysis.

        Args:
            cache_key: Analysis cache key.

        Returns:
            Number of deleted thumbnails.
        """
        deleted = 0

        with self._lock:
            # Find matching entries
            keys_to_delete = [
                k for k in self._entries
                if self._entries[k].cache_key == cache_key
            ]

            for entry_key in keys_to_delete:
                entry = self._entries[entry_key]
                if entry.path.exists():
                    try:
                        entry.path.unlink()
                        deleted += 1
                    except OSError:
                        pass
                self._current_size -= entry.size_bytes
                del self._entries[entry_key]

        return deleted

    # ==================== LRU Eviction ====================

    def _evict_if_needed(self) -> None:
        """Evict oldest entries if cache exceeds limits."""
        # Evict by count
        while len(self._entries) > self._max_entries:
            self._evict_one()

        # Evict by size
        while self._current_size > self._max_total_size:
            self._evict_one()

    def _evict_one(self) -> None:
        """Evict single oldest entry."""
        if not self._entries:
            return

        # Pop oldest (first in OrderedDict)
        entry_key, entry = self._entries.popitem(last=False)

        # Delete file
        if entry.path.exists():
            try:
                entry.path.unlink()
                logger.debug(f"Evicted thumbnail {entry.path}")
            except OSError as e:
                logger.warning(f"Failed to evict {entry.path}: {e}")

        self._current_size -= entry.size_bytes

    def clear_all(self) -> int:
        """
        Clear all thumbnails.

        Returns:
            Number of deleted files.
        """
        deleted = 0

        with self._lock:
            for path in self._thumbs_dir.glob("*.jpg"):
                try:
                    path.unlink()
                    deleted += 1
                except OSError:
                    pass

            self._entries.clear()
            self._current_size = 0

        return deleted


# =============================================================================
# Combined Scene Cut Cache
# =============================================================================

class SceneCutCache:
    """
    Combined cache for scene cut detection.

    Manages both analysis results and thumbnails.

    Attributes:
        analysis_cache: Analysis cache instance.
        thumbnail_cache: Thumbnail cache instance.

    Usage:
        cache = SceneCutCache(project_cache_dir)
        cache_key = cache.generate_key(asset, settings)
        analysis = cache.load_analysis(cache_key)
        if analysis is None:
            # Run detection
            analysis = run_detection(...)
            cache.save_analysis(analysis)
    """

    def __init__(
        self,
        project_cache_dir: str | None = None,
        max_thumb_size: int = ThumbnailCache.DEFAULT_MAX_SIZE,
        max_thumb_entries: int = ThumbnailCache.DEFAULT_MAX_ENTRIES,
    ) -> None:
        """
        Initialize combined cache.

        Args:
            project_cache_dir: Project cache directory.
            max_thumb_size: Maximum thumbnail cache size.
            max_thumb_entries: Maximum thumbnail entries.
        """
        self._analysis_cache = AnalysisCache(project_cache_dir)
        self._thumbnail_cache = ThumbnailCache(
            thumbs_dir=self._analysis_cache.thumbs_dir,
            max_total_size=max_thumb_size,
            max_entries=max_thumb_entries,
        )

    @property
    def analysis_cache(self) -> AnalysisCache:
        """Analysis cache instance."""
        return self._analysis_cache

    @property
    def thumbnail_cache(self) -> ThumbnailCache:
        """Thumbnail cache instance."""
        return self._thumbnail_cache

    @property
    def cache_dir(self) -> Path:
        """Base cache directory."""
        return self._analysis_cache.cache_dir

    # ==================== Convenience Methods ====================

    def generate_key(
        self,
        path: str,
        size: int,
        mtime_ns: int,
        asset_id: str,
        settings: SceneCutSettings,
        clip_id: str | None = None,
        proxy_path: str | None = None,
    ) -> str:
        """Generate cache key for analysis."""
        return generate_cache_key(
            path=path,
            size=size,
            mtime_ns=mtime_ns,
            asset_id=asset_id,
            settings=settings,
            clip_id=clip_id,
            proxy_path=proxy_path,
        )

    def generate_key_from_asset(
        self,
        asset: Any,  # MediaAsset
        settings: SceneCutSettings,
        clip_id: str | None = None,
    ) -> str | None:
        """
        Generate cache key from MediaAsset.

        Args:
            asset: MediaAsset instance.
            settings: Detection settings.
            clip_id: Clip ID for clip-specific analysis.

        Returns:
            Cache key or None if asset invalid.
        """
        try:
            stat = os.stat(asset.path)
            return generate_cache_key(
                path=asset.path,
                size=stat.st_size,
                mtime_ns=stat.st_mtime_ns,
                asset_id=asset.asset_id,
                settings=settings,
                clip_id=clip_id,
                proxy_path=asset.proxy_path if asset.has_proxy else None,
            )
        except OSError:
            return None

    def has_analysis(self, cache_key: str) -> bool:
        """Check if cached analysis exists."""
        return self._analysis_cache.has_analysis(cache_key)

    def load_analysis(self, cache_key: str) -> SceneCutAnalysis | None:
        """Load cached analysis."""
        return self._analysis_cache.load_analysis(cache_key)

    def save_analysis(self, analysis: SceneCutAnalysis) -> str:
        """Save analysis to cache."""
        cache_key = self._analysis_cache.save_analysis(analysis)


        return cache_key

    def invalidate_asset(self, asset_id: str) -> tuple[int, int]:
        """
        Invalidate all cache for an asset.

        Args:
            asset_id: Asset identifier.

        Returns:
            Tuple of (analyses_deleted, thumbs_deleted).
        """
        # Find analysis cache keys for this asset
        analyses_deleted = self._analysis_cache.invalidate_for_asset(asset_id)

        # Find thumbnails to delete
        thumbs_deleted = 0
        for path in self._analysis_cache.thumbs_dir.glob("*.jpg"):
            # Check if analysis exists
            parts = path.stem.split("_")
            if parts:
                cache_key = parts[0]
                analysis_path = self._analysis_cache.get_analysis_path(cache_key)
                if not analysis_path.exists():
                    try:
                        path.unlink()
                        thumbs_deleted += 1
                    except OSError:
                        pass

        return (analyses_deleted, thumbs_deleted)

    def clear_all(self) -> tuple[int, int]:
        """
        Clear all cache.

        Returns:
            Tuple of (analyses_deleted, thumbs_deleted).
        """
        analyses_deleted = self._analysis_cache.clear_all()
        thumbs_deleted = self._thumbnail_cache.clear_all()
        return (analyses_deleted, thumbs_deleted)


# =============================================================================
# Utility Functions
# =============================================================================

# AUD-17813：get_ 前缀的共享语义落实为按 cache 目录键控的惰性单例。
# 此前每次调用都新建 SceneCutCache，构造内的 _scan_existing 会全量
# stat 扫描 thumbs 目录（上限 10000 项），UI 线程每次加载/保存重复
# 付出 O(n) 开销，且多实例各自维护 LRU、驱逐互不知情。
_SCENE_CUT_CACHE_LOCK = threading.Lock()
_SCENE_CUT_CACHE_INSTANCES: dict[str | None, SceneCutCache] = {}


def get_scene_cut_cache(project_cache_dir: str | None = None) -> SceneCutCache:
    """
    Get the shared scene cut cache instance (one per cache directory).

    AUD-17813: instances are cached by normalized ``project_cache_dir``
    so repeated calls (scene_cut_dialog load/save on the UI thread) reuse
    one construction — including its one-time thumbs-directory scan —
    and share a single LRU bookkeeping state.

    Args:
        project_cache_dir: Project cache directory (``None`` uses the
            global user cache directory).

    Returns:
        Shared SceneCutCache instance for this cache directory.
    """
    key: str | None = None
    if project_cache_dir:
        key = os.path.abspath(project_cache_dir)
    instance = _SCENE_CUT_CACHE_INSTANCES.get(key)
    if instance is not None:
        return instance
    with _SCENE_CUT_CACHE_LOCK:
        instance = _SCENE_CUT_CACHE_INSTANCES.get(key)
        if instance is None:
            instance = SceneCutCache(project_cache_dir)
            _SCENE_CUT_CACHE_INSTANCES[key] = instance
        return instance


def check_cache_validity(
    cached_analysis: SceneCutAnalysis,
    current_path: str,
    current_settings: SceneCutSettings,
) -> bool:
    """
    Check if cached analysis is valid for current state.

    Args:
        cached_analysis: Cached analysis result.
        current_path: Current media path.
        current_settings: Current detection settings.

    Returns:
        True if cache is valid.
    """
    # Check detector version
    if cached_analysis.detector_version != DETECTOR_VERSION:
        return False

    # Check settings
    if cached_analysis.settings.to_hash() != current_settings.to_hash():
        return False

    # Check media fingerprint
    try:
        stat = os.stat(current_path)
        fp = MediaFingerprint(
            path=current_path,
            size=stat.st_size,
            mtime_ns=stat.st_mtime_ns,
            asset_id=cached_analysis.asset_id,
        )
        if cached_analysis.media_fingerprint != fp.to_hash():
            return False
    except OSError:
        return False

    return True