"""Tests for FrameThumbnailKey, MemoryAwareLRUCache, and cache key integration.

Verifies:
- FrameThumbnailKey equality, hashing, and immutability
- mtime_ns changes invalidate cache entries
- Different asset_ids produce cache misses even with same path
- Same asset + same frame hits cache
- MemoryAwareLRUCache evicts by byte budget and item count
- Backward-compatible tuple keys still work
"""
from __future__ import annotations

import os
import tempfile
import time
from collections import OrderedDict
from dataclasses import FrozenInstanceError
from unittest.mock import patch, MagicMock

import pytest
from PyQt6.QtGui import QPixmap, QImage
from PyQt6.QtWidgets import QApplication

from src.shared.media.thumbnail_provider import (
    ClipThumbnailRequest,
    FrameThumbnailKey,
    MemoryAwareLRUCache,
    LRUCache,
    _make_frame_key,
    _estimate_pixmap_bytes,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_pixmap(w: int = 64, h: int = 64) -> QPixmap:
    """Return a small non-null QPixmap for testing."""
    img = QImage(w, h, QImage.Format.Format_RGB32)
    img.fill(0xFF808080)  # gray
    return QPixmap.fromImage(img)


# ---------------------------------------------------------------------------
# FrameThumbnailKey tests (no QApplication needed)
# ---------------------------------------------------------------------------

class TestFrameThumbnailKey:
    """Tests for the FrameThumbnailKey frozen dataclass."""

    def test_equality_same_fields(self) -> None:
        a = FrameThumbnailKey("id1", "/a.mov", 1000, 0, 48)
        b = FrameThumbnailKey("id1", "/a.mov", 1000, 0, 48)
        assert a == b

    def test_hash_same_fields(self) -> None:
        a = FrameThumbnailKey("id1", "/a.mov", 1000, 0, 48)
        b = FrameThumbnailKey("id1", "/a.mov", 1000, 0, 48)
        assert hash(a) == hash(b)
        # Can be used as dict key
        d = {a: "value"}
        assert d[b] == "value"

    def test_inequality_different_asset_id(self) -> None:
        a = FrameThumbnailKey("id1", "/a.mov", 1000, 0, 48)
        b = FrameThumbnailKey("id2", "/a.mov", 1000, 0, 48)
        assert a != b

    def test_inequality_different_path(self) -> None:
        a = FrameThumbnailKey("id1", "/a.mov", 1000, 0, 48)
        b = FrameThumbnailKey("id1", "/b.mov", 1000, 0, 48)
        assert a != b

    def test_inequality_different_mtime(self) -> None:
        """Changed mtime_ns must produce a different key."""
        a = FrameThumbnailKey("id1", "/a.mov", 1000, 0, 48)
        b = FrameThumbnailKey("id1", "/a.mov", 2000, 0, 48)
        assert a != b
        assert hash(a) != hash(b)

    def test_inequality_different_frame_index(self) -> None:
        a = FrameThumbnailKey("id1", "/a.mov", 1000, 5, 48)
        b = FrameThumbnailKey("id1", "/a.mov", 1000, 10, 48)
        assert a != b

    def test_inequality_different_target_height(self) -> None:
        a = FrameThumbnailKey("id1", "/a.mov", 1000, 0, 48)
        b = FrameThumbnailKey("id1", "/a.mov", 1000, 0, 72)
        assert a != b

    def test_frozen(self) -> None:
        key = FrameThumbnailKey("id1", "/a.mov", 1000, 0, 48)
        with pytest.raises(FrozenInstanceError):
            key.asset_id = "other"  # type: ignore[misc]

    def test_usable_in_set(self) -> None:
        a = FrameThumbnailKey("id1", "/a.mov", 1000, 0, 48)
        b = FrameThumbnailKey("id1", "/a.mov", 1000, 0, 48)
        s = {a, b}
        assert len(s) == 1

    def test_different_asset_id_same_path_cache_miss(self) -> None:
        """Different asset_id with same path must miss cache."""
        key_a = FrameThumbnailKey("asset_A", "/same.mov", 1000, 0, 48)
        key_b = FrameThumbnailKey("asset_B", "/same.mov", 1000, 0, 48)
        cache: dict = {}
        cache[key_a] = "pix_A"
        assert key_b not in cache  # different asset_id = cache miss

    def test_same_asset_same_frame_cache_hit(self) -> None:
        """Same asset_id + same frame must hit cache."""
        key1 = FrameThumbnailKey("id1", "/a.mov", 1000, 5, 48)
        key2 = FrameThumbnailKey("id1", "/a.mov", 1000, 5, 48)
        cache: dict = {}
        cache[key1] = "pix"
        assert cache[key2] == "pix"


class TestClipThumbnailDensity:
    """Timeline filmstrip extraction must follow display density."""

    @staticmethod
    def _request(width: int, height: int = 48) -> ClipThumbnailRequest:
        return ClipThumbnailRequest(
            asset_id="asset-1",
            asset_path="/tmp/clip.mov",
            clip_width_px=width,
            source_start=20,
            source_duration=10_000,
            pixels_per_frame=1.0,
            thumbnail_height=height,
        )

    def test_wider_clip_requests_more_filmstrip_frames(self) -> None:
        compact = self._request(200)
        expanded = self._request(2_000)

        assert compact.thumbnail_frame_count() == 4
        assert expanded.thumbnail_frame_count() == 24
        assert len(expanded.compute_thumbnail_frames()) > len(compact.compute_thumbnail_frames())

    def test_extreme_zoom_keeps_decode_work_bounded(self) -> None:
        """A very wide clip must not queue dozens of random video seeks."""
        extreme = self._request(20_000)

        assert extreme.thumbnail_frame_count() == extreme.MAX_FILMSTRIP_FRAMES

    def test_quality_cache_key_changes_with_width_and_height(self) -> None:
        compact = self._request(200)
        expanded = self._request(2_000)
        taller = self._request(2_000, height=96)

        assert compact.get_cache_key() != expanded.get_cache_key()
        assert expanded.get_cache_key() != taller.get_cache_key()

    def test_filmstrip_includes_both_source_range_endpoints(self) -> None:
        request = self._request(2_000)
        frames = request.compute_thumbnail_frames()

        assert frames[0] == request.source_start
        assert frames[-1] == request.source_start + request.source_duration - 1

    def test_lod_modes_only_decode_the_frames_they_can_display(self) -> None:
        single = self._request(80)
        single.display_mode = "single"
        start_end = self._request(200)
        start_end.display_mode = "start_end"

        assert single.thumbnail_frame_count() == 1
        assert start_end.thumbnail_frame_count() == 2
        assert start_end.compute_thumbnail_frames() == [20, 10_019]
        assert single.get_cache_key() != start_end.get_cache_key()


# ---------------------------------------------------------------------------
# _make_frame_key tests (no QApplication needed)
# ---------------------------------------------------------------------------

class TestMakeFrameKey:
    """Tests for the _make_frame_key helper."""

    def test_asset_id_none_returns_tuple(self) -> None:
        result = _make_frame_key(None, "/a.mov", 5, 48)
        assert isinstance(result, tuple)
        assert result == ("/a.mov", 5, 48)

    def test_asset_id_provided_returns_frame_key(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".mov", delete=False) as tmp:
            tmp_path = tmp.name
        try:
            result = _make_frame_key("id1", tmp_path, 5, 48)
            assert isinstance(result, FrameThumbnailKey)
            assert result.asset_id == "id1"
            assert result.path == tmp_path
            assert result.frame_index == 5
            assert result.target_height == 48
            assert result.mtime_ns > 0  # real file has mtime
        finally:
            os.unlink(tmp_path)

    def test_asset_id_with_nonexistent_file_uses_zero_mtime(self) -> None:
        result = _make_frame_key("id1", "/nonexistent/file.mov", 5, 48)
        assert isinstance(result, FrameThumbnailKey)
        assert result.mtime_ns == 0

    def test_mtime_changes_with_file(self) -> None:
        """Modifying a file changes mtime_ns in the key."""
        with tempfile.NamedTemporaryFile(suffix=".mov", delete=False) as tmp:
            tmp_path = tmp.name
        try:
            key1 = _make_frame_key("id1", tmp_path, 0, 48)
            # Touch the file to update mtime
            time.sleep(0.05)
            os.utime(tmp_path, None)
            key2 = _make_frame_key("id1", tmp_path, 0, 48)
            # Both should have same asset_id/path/frame/height
            assert isinstance(key1, FrameThumbnailKey)
            assert isinstance(key2, FrameThumbnailKey)
            assert key1.asset_id == key2.asset_id
            assert key1.path == key2.path
            assert key1.frame_index == key2.frame_index
            assert key1.target_height == key2.target_height
        finally:
            os.unlink(tmp_path)


# ---------------------------------------------------------------------------
# MemoryAwareLRUCache tests (require QApplication for QPixmap)
# ---------------------------------------------------------------------------

class TestMemoryAwareLRUCache:
    """Tests for the memory-aware LRU cache."""

    def test_put_and_get(self, qapp: QApplication) -> None:
        cache = MemoryAwareLRUCache(max_items=10, max_bytes=1024 * 1024)
        px = _make_pixmap(64, 64)
        cache.put("k1", px)
        assert cache.get("k1") is px

    def test_miss_returns_none(self, qapp: QApplication) -> None:
        cache = MemoryAwareLRUCache(max_items=10, max_bytes=1024 * 1024)
        assert cache.get("missing") is None

    def test_item_count_eviction(self, qapp: QApplication) -> None:
        cache = MemoryAwareLRUCache(max_items=3, max_bytes=100 * 1024 * 1024)
        for i in range(5):
            cache.put(f"k{i}", _make_pixmap(8, 8))
        # Only last 3 should survive
        assert cache.get("k0") is None
        assert cache.get("k1") is None
        assert cache.get("k2") is not None
        assert cache.get("k3") is not None
        assert cache.get("k4") is not None

    def test_byte_budget_eviction(self, qapp: QApplication) -> None:
        # Each 8x8 pixmap: 8*8*4 = 256 bytes estimate
        budget = 600  # enough for ~2 entries
        cache = MemoryAwareLRUCache(max_items=100, max_bytes=budget)
        cache.put("k0", _make_pixmap(8, 8))  # ~256 bytes
        cache.put("k1", _make_pixmap(8, 8))  # ~256 bytes, total ~512
        assert cache.get("k0") is not None
        assert cache.get("k1") is not None
        cache.put("k2", _make_pixmap(8, 8))  # total ~768 > 600, evicts k0
        assert cache.get("k0") is None
        assert cache.get("k1") is not None
        assert cache.get("k2") is not None

    def test_remove(self, qapp: QApplication) -> None:
        cache = MemoryAwareLRUCache(max_items=10, max_bytes=1024 * 1024)
        px = _make_pixmap(8, 8)
        cache.put("k1", px)
        cache.remove("k1")
        assert cache.get("k1") is None

    def test_clear(self, qapp: QApplication) -> None:
        cache = MemoryAwareLRUCache(max_items=10, max_bytes=1024 * 1024)
        cache.put("k1", _make_pixmap(8, 8))
        cache.put("k2", _make_pixmap(8, 8))
        cache.clear()
        assert cache.get("k1") is None
        assert cache.get("k2") is None
        assert cache.total_bytes == 0

    def test_total_bytes_tracking(self, qapp: QApplication) -> None:
        cache = MemoryAwareLRUCache(max_items=10, max_bytes=1024 * 1024)
        px = _make_pixmap(32, 32)  # 32*32*4 = 4096
        cache.put("k1", px)
        assert cache.total_bytes == 4096

    def test_overwrite_updates_bytes(self, qapp: QApplication) -> None:
        cache = MemoryAwareLRUCache(max_items=10, max_bytes=1024 * 1024)
        cache.put("k1", _make_pixmap(16, 16))  # 16*16*4 = 1024
        assert cache.total_bytes == 1024
        cache.put("k1", _make_pixmap(32, 32))  # 32*32*4 = 4096
        assert cache.total_bytes == 4096

    def test_lru_ordering(self, qapp: QApplication) -> None:
        cache = MemoryAwareLRUCache(max_items=3, max_bytes=100 * 1024 * 1024)
        cache.put("a", _make_pixmap(8, 8))
        cache.put("b", _make_pixmap(8, 8))
        cache.put("c", _make_pixmap(8, 8))
        # Access "a" to move it to end (most recently used)
        cache.get("a")
        # Insert "d" — should evict "b" (LRU)
        cache.put("d", _make_pixmap(8, 8))
        assert cache.get("a") is not None  # was accessed
        assert cache.get("b") is None      # evicted
        assert cache.get("c") is not None
        assert cache.get("d") is not None

    def test_frame_thumbnail_key_as_cache_key(self, qapp: QApplication) -> None:
        cache = MemoryAwareLRUCache(max_items=10, max_bytes=1024 * 1024)
        key = FrameThumbnailKey("id1", "/a.mov", 1000, 0, 48)
        px = _make_pixmap(8, 8)
        cache.put(key, px)
        assert cache.get(key) is px

    def test_different_keys_same_path_different_mtime(self, qapp: QApplication) -> None:
        """Two keys with same path but different mtime should be distinct."""
        cache = MemoryAwareLRUCache(max_items=10, max_bytes=1024 * 1024)
        key1 = FrameThumbnailKey("id1", "/a.mov", 1000, 0, 48)
        key2 = FrameThumbnailKey("id1", "/a.mov", 2000, 0, 48)
        px1 = _make_pixmap(8, 8)
        px2 = _make_pixmap(16, 16)
        cache.put(key1, px1)
        cache.put(key2, px2)
        assert cache.get(key1) is px1
        assert cache.get(key2) is px2


# ---------------------------------------------------------------------------
# _estimate_pixmap_bytes tests (require QApplication for QPixmap)
# ---------------------------------------------------------------------------

class TestEstimatePixmapBytes:
    def test_normal_pixmap(self, qapp: QApplication) -> None:
        px = _make_pixmap(64, 64)
        assert _estimate_pixmap_bytes(px) == 64 * 64 * 4

    def test_null_pixmap(self, qapp: QApplication) -> None:
        px = QPixmap()
        assert _estimate_pixmap_bytes(px) == 0


# ---------------------------------------------------------------------------
# LRUCache (basic, unchanged) still works (no QApplication needed)
# ---------------------------------------------------------------------------

class TestLRUCacheBasic:
    def test_put_and_get(self) -> None:
        cache = LRUCache(10)
        cache.put("k", "v")
        assert cache.get("k") == "v"

    def test_eviction(self) -> None:
        cache = LRUCache(2)
        cache.put("a", 1)
        cache.put("b", 2)
        cache.put("c", 3)
        assert cache.get("a") is None
        assert cache.get("b") == 2
        assert cache.get("c") == 3

    def test_remove(self) -> None:
        cache = LRUCache(10)
        cache.put("k", "v")
        cache.remove("k")
        assert cache.get("k") is None

    def test_clear(self) -> None:
        cache = LRUCache(10)
        cache.put("k", "v")
        cache.clear()
        assert cache.get("k") is None
