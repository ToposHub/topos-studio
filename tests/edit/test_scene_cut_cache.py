"""
Tests for scene_cut.cache — cache system for scene cut detection.

Validates:
- Cache key generation with all components
- MediaFingerprint generation and hashing
- Analysis cache save/load/delete operations
- Thumbnail cache with LRU eviction
- Cache invalidation on file/settings/algorithm changes
- JSON serialization (no pickle)
"""

import hashlib
import json
import os
import tempfile
import time
from pathlib import Path
from fractions import Fraction

import pytest

from src.features.edit.core.scene_cut.cache import (
    AnalysisCache,
    ThumbnailCache,
    SceneCutCache,
    MediaFingerprint,
    CacheKeyComponents,
    generate_cache_key,
    get_scene_cut_cache,
    check_cache_validity,
)
from src.features.edit.core.scene_cut.models import (
    DETECTOR_VERSION,
    SceneCutSettings,
    SceneCutAnalysis,
    SceneCutCandidate,
    SceneCutKind,
    SceneCutDecision,
    SceneCutMetrics,
    FrameTimePoint,
    SourceFrameTimeIndex,
)


# ===================================================================
# Test: MediaFingerprint
# ===================================================================

class TestMediaFingerprint:
    """Media file fingerprint for cache invalidation."""

    def test_fingerprint_hash_is_stable(self):
        fp = MediaFingerprint(
            path="/path/to/video.mp4",
            size=1024000,
            mtime_ns=1700000000000000,
            asset_id="asset-001",
        )
        hash1 = fp.to_hash()
        hash2 = fp.to_hash()
        assert hash1 == hash2
        assert len(hash1) == 16

    def test_different_paths_produce_different_hashes(self):
        fp1 = MediaFingerprint(
            path="/path/to/video1.mp4",
            size=1024000,
            mtime_ns=1700000000000000,
            asset_id="asset-001",
        )
        fp2 = MediaFingerprint(
            path="/path/to/video2.mp4",
            size=1024000,
            mtime_ns=1700000000000000,
            asset_id="asset-001",
        )
        assert fp1.to_hash() != fp2.to_hash()

    def test_different_sizes_produce_different_hashes(self):
        fp1 = MediaFingerprint(
            path="/path/to/video.mp4",
            size=1024000,
            mtime_ns=1700000000000000,
            asset_id="asset-001",
        )
        fp2 = MediaFingerprint(
            path="/path/to/video.mp4",
            size=2048000,
            mtime_ns=1700000000000000,
            asset_id="asset-001",
        )
        assert fp1.to_hash() != fp2.to_hash()

    def test_different_mtimes_produce_different_hashes(self):
        fp1 = MediaFingerprint(
            path="/path/to/video.mp4",
            size=1024000,
            mtime_ns=1700000000000000,
            asset_id="asset-001",
        )
        fp2 = MediaFingerprint(
            path="/path/to/video.mp4",
            size=1024000,
            mtime_ns=1800000000000000,
            asset_id="asset-001",
        )
        assert fp1.to_hash() != fp2.to_hash()

    def test_to_dict_and_from_dict_roundtrip(self):
        fp = MediaFingerprint(
            path="/path/to/video.mp4",
            size=1024000,
            mtime_ns=1700000000000000,
            asset_id="asset-001",
        )
        d = fp.to_dict()
        restored = MediaFingerprint.from_dict(d)
        assert restored.path == fp.path
        assert restored.size == fp.size
        assert restored.mtime_ns == fp.mtime_ns
        assert restored.asset_id == fp.asset_id

    def test_from_path_creates_valid_fingerprint(self, tmp_path):
        # Create temp file
        video_path = tmp_path / "test.mp4"
        video_path.write_bytes(b"test content 12345")

        fp = MediaFingerprint.from_path(str(video_path), "asset-001")
        assert fp is not None
        assert fp.path == str(video_path)
        assert fp.size == len(b"test content 12345")
        assert fp.asset_id == "asset-001"

    def test_from_path_returns_none_for_missing_file(self):
        fp = MediaFingerprint.from_path("/nonexistent/path.mp4", "asset-001")
        assert fp is None


# ===================================================================
# Test: CacheKeyComponents
# ===================================================================

class TestCacheKeyComponents:
    """Cache key generation from components."""

    def test_generate_key_produces_24_chars(self):
        components = CacheKeyComponents(
            media_fingerprint="fp-hash-123",
            source_range_start=0,
            source_range_end=1000,
            settings_hash="settings-hash",
            detector_version=DETECTOR_VERSION,
        )
        key = components.generate_key()
        assert len(key) == 24

    def test_keys_differ_for_different_ranges(self):
        c1 = CacheKeyComponents(
            media_fingerprint="fp-hash",
            source_range_start=0,
            source_range_end=1000,
            settings_hash="settings-hash",
            detector_version=DETECTOR_VERSION,
        )
        c2 = CacheKeyComponents(
            media_fingerprint="fp-hash",
            source_range_start=0,
            source_range_end=2000,
            settings_hash="settings-hash",
            detector_version=DETECTOR_VERSION,
        )
        assert c1.generate_key() != c2.generate_key()

    def test_keys_differ_for_different_settings(self):
        c1 = CacheKeyComponents(
            media_fingerprint="fp-hash",
            source_range_start=0,
            source_range_end=1000,
            settings_hash="settings-1",
            detector_version=DETECTOR_VERSION,
        )
        c2 = CacheKeyComponents(
            media_fingerprint="fp-hash",
            source_range_start=0,
            source_range_end=1000,
            settings_hash="settings-2",
            detector_version=DETECTOR_VERSION,
        )
        assert c1.generate_key() != c2.generate_key()

    def test_keys_differ_for_different_detector_versions(self):
        c1 = CacheKeyComponents(
            media_fingerprint="fp-hash",
            source_range_start=0,
            source_range_end=1000,
            settings_hash="settings-hash",
            detector_version="1.0.0",
        )
        c2 = CacheKeyComponents(
            media_fingerprint="fp-hash",
            source_range_start=0,
            source_range_end=1000,
            settings_hash="settings-hash",
            detector_version="2.0.0",
        )
        assert c1.generate_key() != c2.generate_key()

    def test_keys_differ_with_proxy_fingerprint(self):
        c1 = CacheKeyComponents(
            media_fingerprint="fp-hash",
            source_range_start=0,
            source_range_end=1000,
            settings_hash="settings-hash",
            detector_version=DETECTOR_VERSION,
            proxy_fingerprint=None,
        )
        c2 = CacheKeyComponents(
            media_fingerprint="fp-hash",
            source_range_start=0,
            source_range_end=1000,
            settings_hash="settings-hash",
            detector_version=DETECTOR_VERSION,
            proxy_fingerprint="proxy-hash",
        )
        assert c1.generate_key() != c2.generate_key()

    def test_keys_differ_for_clip_specific(self):
        c1 = CacheKeyComponents(
            media_fingerprint="fp-hash",
            source_range_start=0,
            source_range_end=1000,
            settings_hash="settings-hash",
            detector_version=DETECTOR_VERSION,
            clip_id=None,  # Full media analysis
        )
        c2 = CacheKeyComponents(
            media_fingerprint="fp-hash",
            source_range_start=0,
            source_range_end=1000,
            settings_hash="settings-hash",
            detector_version=DETECTOR_VERSION,
            clip_id="clip-001",  # Clip-specific analysis
        )
        assert c1.generate_key() != c2.generate_key()


# ===================================================================
# Test: generate_cache_key
# ===================================================================

class TestGenerateCacheKey:
    """Full cache key generation function."""

    def test_generate_key_with_all_params(self):
        key = generate_cache_key(
            path="/path/to/video.mp4",
            size=1024000,
            mtime_ns=1700000000000000,
            asset_id="asset-001",
            settings=SceneCutSettings(sensitivity=0.55),
        )
        assert len(key) == 24
        assert key.isalnum()  # Only alphanumeric chars from hex

    def test_generate_key_with_clip_id(self):
        key1 = generate_cache_key(
            path="/path/to/video.mp4",
            size=1024000,
            mtime_ns=1700000000000000,
            asset_id="asset-001",
            settings=SceneCutSettings(sensitivity=0.55),
            clip_id=None,
        )
        key2 = generate_cache_key(
            path="/path/to/video.mp4",
            size=1024000,
            mtime_ns=1700000000000000,
            asset_id="asset-001",
            settings=SceneCutSettings(sensitivity=0.55),
            clip_id="clip-001",
        )
        assert key1 != key2

    def test_generate_key_with_proxy(self, tmp_path):
        # Create proxy file
        proxy_path = tmp_path / "proxy.mp4"
        proxy_path.write_bytes(b"proxy content")

        key = generate_cache_key(
            path="/path/to/video.mp4",
            size=1024000,
            mtime_ns=1700000000000000,
            asset_id="asset-001",
            settings=SceneCutSettings(sensitivity=0.55),
            proxy_path=str(proxy_path),
        )
        assert len(key) == 24

    def test_generate_key_changes_with_settings(self):
        key1 = generate_cache_key(
            path="/path/to/video.mp4",
            size=1024000,
            mtime_ns=1700000000000000,
            asset_id="asset-001",
            settings=SceneCutSettings(sensitivity=0.55),
        )
        key2 = generate_cache_key(
            path="/path/to/video.mp4",
            size=1024000,
            mtime_ns=1700000000000000,
            asset_id="asset-001",
            settings=SceneCutSettings(sensitivity=0.65),
        )
        assert key1 != key2


# ===================================================================
# Test: AnalysisCache
# ===================================================================

class TestAnalysisCache:
    """Analysis JSON file cache."""

    def _make_analysis(self, analysis_id: str = "test-analysis"):
        settings = SceneCutSettings(sensitivity=0.55)
        time_points = (
            FrameTimePoint(source_frame=0, pts=0, seconds=0.0),
            FrameTimePoint(source_frame=100, pts=1000, seconds=4.166),
        )
        time_index = SourceFrameTimeIndex(
            asset_id="asset-001",
            media_fingerprint="fp-001",
            points=time_points,
            average_fps=24.0,
        )
        candidates = (
            SceneCutCandidate(
                source_frame=100,
                pts=1000,
                seconds=4.166,
                score=0.85,
                confidence=0.90,
                kind=SceneCutKind.HARD_CUT,
                decision=SceneCutDecision.PENDING,
            ),
        )
        return SceneCutAnalysis(
            analysis_id=analysis_id,
            asset_id="asset-001",
            source_path="/path/to/media.mp4",
            clip_id="clip-001",
            settings=settings,
            media_fingerprint="fp-001",
            source_time_index=time_index,
            candidates=candidates,
            created_at=time.time(),
            detector_version=DETECTOR_VERSION,
        )

    def test_cache_creates_directories(self, tmp_path):
        cache_dir = tmp_path / "project_cache"
        cache = AnalysisCache(str(cache_dir))
        assert cache.cache_dir.exists()
        assert cache.analyses_dir.exists()
        assert cache.thumbs_dir.exists()

    def test_save_and_load_analysis(self, tmp_path):
        cache_dir = tmp_path / "project_cache"
        cache = AnalysisCache(str(cache_dir))

        analysis = self._make_analysis()
        cache_key = cache.save_analysis(analysis)

        # Check file exists
        path = cache.get_analysis_path(cache_key)
        assert path.exists()
        assert path.suffix == ".json"

        # Load and verify
        loaded = cache.load_analysis(cache_key)
        assert loaded is not None
        assert loaded.analysis_id == analysis.analysis_id
        assert loaded.asset_id == analysis.asset_id
        assert len(loaded.candidates) == 1

    def test_has_analysis_returns_true_for_existing(self, tmp_path):
        cache_dir = tmp_path / "project_cache"
        cache = AnalysisCache(str(cache_dir))

        analysis = self._make_analysis()
        cache_key = cache.save_analysis(analysis)

        assert cache.has_analysis(cache_key) is True

    def test_has_analysis_returns_false_for_missing(self, tmp_path):
        cache_dir = tmp_path / "project_cache"
        cache = AnalysisCache(str(cache_dir))

        assert cache.has_analysis("nonexistent-key") is False

    def test_delete_analysis(self, tmp_path):
        cache_dir = tmp_path / "project_cache"
        cache = AnalysisCache(str(cache_dir))

        analysis = self._make_analysis()
        cache_key = cache.save_analysis(analysis)

        assert cache.has_analysis(cache_key) is True
        assert cache.delete_analysis(cache_key) is True
        assert cache.has_analysis(cache_key) is False

    def test_load_analysis_returns_none_for_invalid_json(self, tmp_path):
        cache_dir = tmp_path / "project_cache"
        cache = AnalysisCache(str(cache_dir))

        # Write invalid JSON
        path = cache.get_analysis_path("invalid-key")
        path.write_text("not valid json {{{")

        loaded = cache.load_analysis("invalid-key")
        assert loaded is None

    def test_load_analysis_returns_none_for_missing_required_fields(self, tmp_path):
        cache_dir = tmp_path / "project_cache"
        cache = AnalysisCache(str(cache_dir))

        # Write JSON without required fields
        path = cache.get_analysis_path("partial-key")
        path.write_text(json.dumps({"some_field": "some_value"}))

        assert cache.has_analysis("partial-key") is False

    def test_invalidate_for_asset_deletes_matching(self, tmp_path):
        cache_dir = tmp_path / "project_cache"
        cache = AnalysisCache(str(cache_dir))

        # Save two analyses with different fingerprints (as detector would produce)
        analysis1 = self._make_analysis("analysis-1")
        analysis1 = SceneCutAnalysis(
            analysis_id="analysis-1",
            asset_id="asset-to-delete",
            source_path="/path/to/media.mp4",
            settings=SceneCutSettings(),
            media_fingerprint="fp-asset-a",
            candidates=(),
            created_at=time.time(),
            detector_version=DETECTOR_VERSION,
        )
        cache.save_analysis(analysis1)

        analysis2 = self._make_analysis("analysis-2")
        analysis2 = SceneCutAnalysis(
            analysis_id="analysis-2",
            asset_id="other-asset",
            source_path="/path/to/other.mp4",
            settings=SceneCutSettings(),
            media_fingerprint="fp-asset-b",
            candidates=(),
            created_at=time.time(),
            detector_version=DETECTOR_VERSION,
        )
        cache.save_analysis(analysis2)

        # Invalidate first asset
        deleted = cache.invalidate_for_asset("asset-to-delete")
        assert deleted == 1

        # First should be deleted, second should remain
        assert cache.load_analysis(analysis1.to_cache_key()) is None
        # Note: second key is different, check by asset_id

    def test_clear_all_deletes_everything(self, tmp_path):
        cache_dir = tmp_path / "project_cache"
        cache = AnalysisCache(str(cache_dir))

        # Save multiple analyses with different asset IDs to ensure different cache keys
        for i in range(3):
            analysis = SceneCutAnalysis(
                analysis_id=f"analysis-{i}",
                asset_id=f"asset-{i}",  # Different asset_id for different cache key
                source_path=f"/path/to/media{i}.mp4",
                settings=SceneCutSettings(sensitivity=0.55 + i * 0.05),  # Different settings
                candidates=(),
                media_fingerprint=f"fp-{i}",
                created_at=time.time(),
                detector_version=DETECTOR_VERSION,
            )
            cache.save_analysis(analysis)

        deleted = cache.clear_all()
        assert deleted >= 3

        # Check files gone
        for f in cache.analyses_dir.glob("*.json"):
            pytest.fail(f"Found remaining file: {f}")

    def test_validate_analysis_checks_detector_version(self, tmp_path):
        cache_dir = tmp_path / "project_cache"
        cache = AnalysisCache(str(cache_dir))

        analysis = SceneCutAnalysis(
            analysis_id="old-analysis",
            asset_id="asset-001",
            source_path="/path/to/media.mp4",
            settings=SceneCutSettings(),
            candidates=(),
            media_fingerprint="fp",
            created_at=time.time(),
            detector_version="0.5.0",  # Old version
        )

        # Create temp file for validation
        video_path = tmp_path / "video.mp4"
        video_path.write_bytes(b"test")

        # Should be invalid due to version mismatch
        is_valid = cache.validate_analysis(analysis, str(video_path))
        assert is_valid is False


# ===================================================================
# Test: ThumbnailCache
# ===================================================================

class TestThumbnailCache:
    """Thumbnail cache with LRU management."""

    def test_cache_creates_directory(self, tmp_path):
        thumbs_dir = tmp_path / "thumbs"
        cache = ThumbnailCache(thumbs_dir=thumbs_dir)
        assert thumbs_dir.exists()

    def test_save_and_has_thumb(self, tmp_path):
        thumbs_dir = tmp_path / "thumbs"
        cache = ThumbnailCache(thumbs_dir=thumbs_dir)

        image_data = b"fake jpeg data"
        path = cache.save_thumb("cache-key-1", 100, "before", image_data)

        assert path.exists()
        assert cache.has_thumb("cache-key-1", 100, "before")

    def test_load_thumb_returns_data(self, tmp_path):
        thumbs_dir = tmp_path / "thumbs"
        cache = ThumbnailCache(thumbs_dir=thumbs_dir)

        image_data = b"fake jpeg data xyz"
        cache.save_thumb("cache-key-1", 100, "before", image_data)

        loaded = cache.load_thumb("cache-key-1", 100, "before")
        assert loaded == image_data

    def test_load_thumb_returns_none_for_missing(self, tmp_path):
        thumbs_dir = tmp_path / "thumbs"
        cache = ThumbnailCache(thumbs_dir=thumbs_dir)

        loaded = cache.load_thumb("missing-key", 0, "before")
        assert loaded is None

    def test_delete_thumb(self, tmp_path):
        thumbs_dir = tmp_path / "thumbs"
        cache = ThumbnailCache(thumbs_dir=thumbs_dir)

        cache.save_thumb("cache-key-1", 100, "before", b"data")
        assert cache.has_thumb("cache-key-1", 100, "before")

        deleted = cache.delete_thumb("cache-key-1", 100, "before")
        assert deleted is True
        assert not cache.has_thumb("cache-key-1", 100, "before")

    def test_delete_thumbs_for_analysis(self, tmp_path):
        thumbs_dir = tmp_path / "thumbs"
        cache = ThumbnailCache(thumbs_dir=thumbs_dir)

        # Save multiple thumbs for same analysis
        cache.save_thumb("cache-key-1", 100, "before", b"data1")
        cache.save_thumb("cache-key-1", 100, "after", b"data2")
        cache.save_thumb("cache-key-1", 200, "before", b"data3")
        cache.save_thumb("other-key", 50, "before", b"data4")

        deleted = cache.delete_thumbs_for_analysis("cache-key-1")
        assert deleted == 3

        assert not cache.has_thumb("cache-key-1", 100, "before")
        assert cache.has_thumb("other-key", 50, "before")  # Unaffected

    def test_current_size_tracking(self, tmp_path):
        thumbs_dir = tmp_path / "thumbs"
        cache = ThumbnailCache(thumbs_dir=thumbs_dir)

        data1 = b"x" * 100
        data2 = b"y" * 200

        cache.save_thumb("key-1", 100, "before", data1)
        assert cache.current_size == 100

        cache.save_thumb("key-2", 200, "before", data2)
        assert cache.current_size == 300

    def test_lru_eviction_by_size(self, tmp_path):
        thumbs_dir = tmp_path / "thumbs"
        # Very small max size to force eviction
        cache = ThumbnailCache(
            thumbs_dir=thumbs_dir,
            max_total_size=50,  # 50 bytes max
            max_entries=1000,
        )

        # Save first thumb (should be evicted when second is added)
        data1 = b"x" * 30
        cache.save_thumb("key-1", 100, "before", data1)

        # Save second thumb (triggers eviction)
        data2 = b"y" * 40
        cache.save_thumb("key-2", 200, "before", data2)

        # First should be evicted
        assert not cache.has_thumb("key-1", 100, "before")
        assert cache.has_thumb("key-2", 200, "before")

    def test_lru_eviction_by_count(self, tmp_path):
        thumbs_dir = tmp_path / "thumbs"
        cache = ThumbnailCache(
            thumbs_dir=thumbs_dir,
            max_total_size=1000000,  # Large size
            max_entries=3,  # Only 3 entries
        )

        # Save 4 thumbs, first should be evicted
        cache.save_thumb("key-1", 100, "before", b"data1")
        cache.save_thumb("key-2", 200, "before", b"data2")
        cache.save_thumb("key-3", 300, "before", b"data3")
        cache.save_thumb("key-4", 400, "before", b"data4")

        # First should be evicted
        assert not cache.has_thumb("key-1", 100, "before")
        assert cache.entry_count == 3

    def test_clear_all_thumbs(self, tmp_path):
        thumbs_dir = tmp_path / "thumbs"
        cache = ThumbnailCache(thumbs_dir=thumbs_dir)

        cache.save_thumb("key-1", 100, "before", b"data1")
        cache.save_thumb("key-2", 200, "before", b"data2")

        deleted = cache.clear_all()
        assert deleted == 2
        assert cache.current_size == 0
        assert cache.entry_count == 0

    def test_scan_existing_on_init(self, tmp_path):
        thumbs_dir = tmp_path / "thumbs"
        # Create directory first
        thumbs_dir.mkdir(parents=True, exist_ok=True)
        
        # Pre-create thumbnail files
        (thumbs_dir / "existing-key_100_before.jpg").write_bytes(b"existing data")
        (thumbs_dir / "existing-key_100_after.jpg").write_bytes(b"existing data 2")

        # Initialize cache
        cache = ThumbnailCache(thumbs_dir=thumbs_dir)

        # Should find existing thumbs
        assert cache.has_thumb("existing-key", 100, "before")
        assert cache.has_thumb("existing-key", 100, "after")


# ===================================================================
# Test: SceneCutCache (Combined)
# ===================================================================

class TestSceneCutCache:
    """Combined analysis and thumbnail cache."""

    def _make_analysis(self):
        return SceneCutAnalysis(
            analysis_id="combined-test",
            asset_id="asset-001",
            source_path="/path/to/media.mp4",
            settings=SceneCutSettings(sensitivity=0.55),
            candidates=(
                SceneCutCandidate(
                    source_frame=100,
                    pts=1000,
                    seconds=4.166,
                    score=0.85,
                    confidence=0.90,
                    kind=SceneCutKind.HARD_CUT,
                ),
            ),
            created_at=time.time(),
            detector_version=DETECTOR_VERSION,
        )

    def test_combined_cache_init(self, tmp_path):
        cache_dir = tmp_path / "project_cache"
        cache = SceneCutCache(str(cache_dir))
        assert cache.cache_dir.exists()

    def test_save_and_load_analysis(self, tmp_path):
        cache_dir = tmp_path / "project_cache"
        cache = SceneCutCache(str(cache_dir))

        analysis = self._make_analysis()
        cache_key = cache.save_analysis(analysis)

        loaded = cache.load_analysis(cache_key)
        assert loaded is not None
        assert loaded.analysis_id == analysis.analysis_id

    def test_generate_key_from_params(self, tmp_path):
        cache_dir = tmp_path / "project_cache"
        cache = SceneCutCache(str(cache_dir))

        key = cache.generate_key(
            path="/path/to/video.mp4",
            size=1024000,
            mtime_ns=1700000000000000,
            asset_id="asset-001",
            settings=SceneCutSettings(sensitivity=0.55),
        )
        assert len(key) == 24

    def test_invalidate_asset_clears_both(self, tmp_path):
        cache_dir = tmp_path / "project_cache"
        cache = SceneCutCache(str(cache_dir))

        # Save analysis
        analysis = self._make_analysis()
        cache_key = cache.save_analysis(analysis)

        # Save thumbs
        cache.thumbnail_cache.save_thumb(cache_key, 100, "before", b"data")

        # Invalidate
        analyses_deleted, thumbs_deleted = cache.invalidate_asset("asset-001")
        assert analyses_deleted >= 1
        assert thumbs_deleted >= 1

    def test_clear_all_clears_both(self, tmp_path):
        cache_dir = tmp_path / "project_cache"
        cache = SceneCutCache(str(cache_dir))

        # Save some data
        analysis = self._make_analysis()
        cache_key = cache.save_analysis(analysis)
        cache.thumbnail_cache.save_thumb(cache_key, 100, "before", b"data")

        # Clear
        analyses_deleted, thumbs_deleted = cache.clear_all()
        assert analyses_deleted >= 1
        assert thumbs_deleted >= 1


# ===================================================================
# Test: JSON Serialization (No Pickle)
# ===================================================================

class TestJsonSerialization:
    """Verify JSON serialization is used, not pickle."""

    def test_analysis_file_is_valid_json(self, tmp_path):
        cache_dir = tmp_path / "project_cache"
        cache = AnalysisCache(str(cache_dir))

        analysis = SceneCutAnalysis(
            analysis_id="json-test",
            asset_id="asset-001",
            source_path="/path/to/media.mp4",
            settings=SceneCutSettings(sensitivity=0.55),
            candidates=(
                SceneCutCandidate(
                    source_frame=100,
                    pts=1000,
                    seconds=4.166,
                    score=0.85,
                    confidence=0.90,
                    kind=SceneCutKind.HARD_CUT,
                    metrics=SceneCutMetrics(
                        luma_delta=0.80,
                        hist_delta=0.70,
                        edge_delta=0.60,
                        color_delta=0.50,
                        black_score=0.10,
                        flash_score=0.05,
                        motion_score=0.30,
                        combined_score=0.65,
                    ),
                ),
            ),
            created_at=time.time(),
            detector_version=DETECTOR_VERSION,
        )

        cache_key = cache.save_analysis(analysis)
        path = cache.get_analysis_path(cache_key)

        # Read and parse as JSON
        content = path.read_text()
        data = json.loads(content)

        # Should be valid JSON dict
        assert isinstance(data, dict)
        assert "analysis_id" in data
        assert "candidates" in data

        # Verify nested structures
        candidate = data["candidates"][0]
        assert "metrics" in candidate
        assert candidate["metrics"]["luma_delta"] == 0.80

    def test_complex_analysis_roundtrip(self, tmp_path):
        cache_dir = tmp_path / "project_cache"
        cache = AnalysisCache(str(cache_dir))

        # Create complex analysis with all fields
        settings = SceneCutSettings(
            sensitivity=0.55,
            min_scene_frames=12,
            sample_width=320,
            max_sample_height=180,
            start_source_frame=100,
            end_source_frame=1000,
        )
        time_points = tuple(
            FrameTimePoint(
                source_frame=i,
                pts=i * 42,
                seconds=i / 24.0,
                duration_seconds=1 / 24.0,
                is_keyframe=(i % 12 == 0),
            )
            for i in range(100, 110)
        )
        time_index = SourceFrameTimeIndex(
            asset_id="asset-001",
            media_fingerprint="fp-complex",
            points=time_points,
            average_fps=24.0,
        )
        candidates = tuple(
            SceneCutCandidate(
                source_frame=100 + i * 10,
                pts=(100 + i * 10) * 42,
                seconds=(100 + i * 10) / 24.0,
                timeline_frame=50 + i * 5,
                score=0.75 + i * 0.05,
                confidence=0.80 + i * 0.03,
                kind=SceneCutKind.HARD_CUT,
                decision=SceneCutDecision.PENDING,
                metrics=SceneCutMetrics(
                    luma_delta=0.5 + i * 0.1,
                    hist_delta=0.4 + i * 0.1,
                    edge_delta=0.3 + i * 0.1,
                    color_delta=0.2 + i * 0.1,
                    black_score=0.05,
                    flash_score=0.02,
                    motion_score=0.1 + i * 0.05,
                    combined_score=0.4 + i * 0.1,
                ),
                reasons=("high_score", f"frame_{i}"),
                before_thumb_cache_key=f"thumb_before_{i}",
                after_thumb_cache_key=f"thumb_after_{i}",
            )
            for i in range(5)
        )

        original = SceneCutAnalysis(
            analysis_id="complex-analysis",
            asset_id="asset-001",
            source_path="/path/to/complex.mp4",
            clip_id="clip-complex",
            settings=settings,
            media_fingerprint="fp-complex",
            source_time_index=time_index,
            candidates=candidates,
            created_at=time.time(),
            detector_version=DETECTOR_VERSION,
        )

        # Save and load
        cache_key = cache.save_analysis(original)
        loaded = cache.load_analysis(cache_key)

        assert loaded is not None
        assert loaded.analysis_id == original.analysis_id
        assert loaded.settings.sensitivity == original.settings.sensitivity
        assert loaded.settings.start_source_frame == 100
        assert len(loaded.source_time_index.points) == 10
        assert len(loaded.candidates) == 5

        # Check candidate details
        for i, (orig, load) in enumerate(zip(original.candidates, loaded.candidates)):
            assert load.source_frame == orig.source_frame
            assert load.timeline_frame == orig.timeline_frame
            assert load.score == orig.score
            assert load.metrics is not None
            assert load.metrics.luma_delta == orig.metrics.luma_delta
            assert load.reasons == orig.reasons


# ===================================================================
# Test: Utility Functions
# ===================================================================

class TestUtilityFunctions:
    """Helper functions for cache operations."""

    def test_get_scene_cut_cache_returns_instance(self):
        cache = get_scene_cut_cache()
        assert isinstance(cache, SceneCutCache)

    def test_check_cache_validity_true_for_matching(self, tmp_path):
        # Create temp file
        video_path = tmp_path / "video.mp4"
        video_path.write_bytes(b"test content")

        stat = os.stat(video_path)
        fp = MediaFingerprint(
            path=str(video_path),
            size=stat.st_size,
            mtime_ns=stat.st_mtime_ns,
            asset_id="asset-001",
        )

        settings = SceneCutSettings(sensitivity=0.55)
        analysis = SceneCutAnalysis(
            analysis_id="valid-test",
            asset_id="asset-001",
            source_path=str(video_path),
            settings=settings,
            media_fingerprint=fp.to_hash(),
            candidates=(),
            created_at=time.time(),
            detector_version=DETECTOR_VERSION,
        )

        is_valid = check_cache_validity(analysis, str(video_path), settings)
        assert is_valid is True

    def test_check_cache_validity_false_for_different_settings(self, tmp_path):
        video_path = tmp_path / "video.mp4"
        video_path.write_bytes(b"test content")

        stat = os.stat(video_path)
        fp = MediaFingerprint(
            path=str(video_path),
            size=stat.st_size,
            mtime_ns=stat.st_mtime_ns,
            asset_id="asset-001",
        )

        # Analysis with different settings
        original_settings = SceneCutSettings(sensitivity=0.55)
        current_settings = SceneCutSettings(sensitivity=0.65)

        analysis = SceneCutAnalysis(
            analysis_id="settings-test",
            asset_id="asset-001",
            source_path=str(video_path),
            settings=original_settings,
            media_fingerprint=fp.to_hash(),
            candidates=(),
            created_at=time.time(),
            detector_version=DETECTOR_VERSION,
        )

        is_valid = check_cache_validity(analysis, str(video_path), current_settings)
        assert is_valid is False

    def test_check_cache_validity_false_for_changed_file(self, tmp_path):
        video_path = tmp_path / "video.mp4"
        video_path.write_bytes(b"test content")

        stat1 = os.stat(video_path)
        fp = MediaFingerprint(
            path=str(video_path),
            size=stat1.st_size,
            mtime_ns=stat1.st_mtime_ns,
            asset_id="asset-001",
        )

        # Modify file
        video_path.write_bytes(b"modified content larger")

        settings = SceneCutSettings(sensitivity=0.55)
        analysis = SceneCutAnalysis(
            analysis_id="file-change-test",
            asset_id="asset-001",
            source_path=str(video_path),
            settings=settings,
            media_fingerprint=fp.to_hash(),
            candidates=(),
            created_at=time.time(),
            detector_version=DETECTOR_VERSION,
        )

        is_valid = check_cache_validity(analysis, str(video_path), settings)
        assert is_valid is False  # File changed, fingerprint mismatch