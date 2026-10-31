"""阶段 3 验收：跟踪结果缓存键与 LRU 行为。"""

import os

import pytest

from src.shared.tracking.frame_sampler import AnalysisQuality
from src.shared.tracking.tracker_2d import TrackRequest, TrackDirection
from src.shared.tracking.tracking_cache import (
    TrackingCache,
    compute_media_fingerprint,
)
from src.shared.tracking.types import TrackingMode, TRACKING_ALGORITHM_VERSION


BASE = dict(
    fingerprint="fp-abc",
    frame_start=0,
    frame_end=124,
    reference_frame=62,
    direction="forward",
    mode="trs",
    quality="draft",
    pattern=(0.375, 0.347, 0.25, 0.306),
    search=(0.225, 0.197, 0.55, 0.606),
    algorithm_version=TRACKING_ALGORITHM_VERSION,
)


class TestCacheKey:
    def test_identical_params_same_key(self):
        assert TrackingCache.make_key(**BASE) == TrackingCache.make_key(**BASE)

    @pytest.mark.parametrize("field,value", [
        ("fingerprint", "fp-other"),
        ("frame_start", 5),
        ("frame_end", 100),
        ("reference_frame", 30),
        ("direction", "backward"),
        ("mode", "translation"),
        ("quality", "high"),
        ("pattern", (0.4, 0.35, 0.25, 0.3)),
        ("search", (0.2, 0.2, 0.6, 0.6)),
        ("algorithm_version", TRACKING_ALGORITHM_VERSION + 1),
    ])
    def test_any_component_change_changes_key(self, field, value):
        changed = dict(BASE, **{field: value})
        assert TrackingCache.make_key(**BASE) != TrackingCache.make_key(**changed), (
            f"缓存键对 {field} 变化不敏感"
        )

    def test_float_rounding_stability(self):
        """微小浮点抖动不应改变键（区域编辑后的数值噪声）。"""
        a = dict(BASE, pattern=(0.3750000001, 0.347, 0.25, 0.306))
        assert TrackingCache.make_key(**BASE) == TrackingCache.make_key(**a)

    def test_key_for_request(self):
        req = TrackRequest(
            media_path="x.mp4",
            pattern_region=BASE["pattern"],
            search_region=BASE["search"],
            reference_frame=62,
            frame_start=0,
            frame_end=124,
            mode=TrackingMode.TRANSLATION_ROTATION_SCALE,
            direction=TrackDirection.FORWARD,
            quality=AnalysisQuality.DRAFT,
        )
        assert TrackingCache.key_for_request(req, "fp-abc") == TrackingCache.make_key(**BASE)


class TestCacheStorage:
    def test_put_get_contains(self):
        cache = TrackingCache()
        assert cache.get("k") is None
        cache.put("k", {"result": 1})
        assert cache.contains("k")
        assert cache.get("k") == {"result": 1}
        assert len(cache) == 1

    def test_remove_and_clear(self):
        cache = TrackingCache()
        cache.put("a", 1)
        cache.put("b", 2)
        cache.remove("a")
        assert not cache.contains("a")
        cache.clear()
        assert len(cache) == 0

    def test_lru_eviction(self):
        cache = TrackingCache(max_entries=3)
        for i in range(5):
            cache.put(f"k{i}", i)
        assert len(cache) == 3
        # 最早两个被淘汰
        assert cache.get("k0") is None
        assert cache.get("k1") is None
        assert cache.get("k4") == 4

    def test_lru_refresh_on_get(self):
        cache = TrackingCache(max_entries=2)
        cache.put("a", 1)
        cache.put("b", 2)
        cache.get("a")          # a 变为最近使用
        cache.put("c", 3)       # 应淘汰 b 而不是 a
        assert cache.get("a") == 1
        assert cache.get("b") is None


class TestMediaFingerprint:
    def test_stable_for_same_file(self, tmp_path):
        path = tmp_path / "clip.mp4"
        path.write_bytes(b"\x00" * 1024)
        fp1 = compute_media_fingerprint(str(path))
        fp2 = compute_media_fingerprint(str(path))
        assert fp1 != ""
        assert fp1 == fp2

    def test_different_content_different_fingerprint(self, tmp_path):
        p1 = tmp_path / "a.mp4"
        p2 = tmp_path / "b.mp4"
        p1.write_bytes(b"\x00" * 1024)
        p2.write_bytes(b"\x01" * 2048)
        assert compute_media_fingerprint(str(p1)) != compute_media_fingerprint(str(p2))

    def test_missing_file_returns_empty(self, tmp_path):
        assert compute_media_fingerprint(str(tmp_path / "nope.mp4")) == ""

    def test_fixture_file_fingerprint_nonempty(self):
        """真实素材文件可生成指纹。"""
        from tests.shared.tracking import tracker_support as ts

        if not ts.TOOLS_AVAILABLE:
            pytest.skip("tracking fixture 工具不可用")
        directory = ts.fixture_dir()
        fp = compute_media_fingerprint(os.path.join(directory, "translation.mp4"))
        assert len(fp) == 32
