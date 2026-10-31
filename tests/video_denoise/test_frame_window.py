"""帧窗口与消费者侧缓存单元测试（纯 CPU）。"""

import numpy as np
import pytest

from src.shared.video.denoise.frame_types import FrameWindow, TemporalFrameWindowCache

pytestmark = pytest.mark.media_core


class _FakeFrame:
    def __init__(self, index, nbytes=1000):
        self.frame_index = index
        self.frame_data = np.zeros(nbytes, dtype=np.uint8)


class TestFrameWindow:
    def test_indices_ordered(self):
        w = FrameWindow(
            clip_id="c", center_frame=10,
            previous_frames=[_FakeFrame(9), _FakeFrame(8)],
            next_frames=[_FakeFrame(11), _FakeFrame(12)],
            center_frame_data=_FakeFrame(10),
        )
        assert w.frame_indices == [8, 9, 10, 11, 12]
        ordered = w.all_frames_ordered()
        assert [f.frame_index for f in ordered] == [8, 9, 10, 11, 12]

    def test_degenerate(self):
        assert FrameWindow(clip_id="c", center_frame=0).is_degenerate()
        assert not FrameWindow(clip_id="c", center_frame=0,
                               next_frames=[_FakeFrame(1)]).is_degenerate()

    def test_sparse_frames_use_actual_indices_and_skip_none(self):
        w = FrameWindow(
            clip_id="c", center_frame=10,
            previous_frames=[_FakeFrame(9), None, _FakeFrame(7)],
            next_frames=[_FakeFrame(11), None, _FakeFrame(13)],
            center_frame_data=_FakeFrame(10),
        )
        assert w.frame_indices == [7, 9, 10, 11, 13]
        assert [f.frame_index for f in w.all_frames_ordered()] == [7, 9, 10, 11, 13]

    def test_none_center_is_not_returned_as_a_frame(self):
        w = FrameWindow(clip_id="c", center_frame=10,
                        previous_frames=[_FakeFrame(9)])
        assert w.all_frames_ordered()[0].frame_index == 9


class TestWindowCache:
    def test_put_get_roundtrip(self):
        cache = TemporalFrameWindowCache()
        win = FrameWindow(clip_id="c", center_frame=5,
                          next_frames=[_FakeFrame(6)])
        cache.put("c", 5, "master", 1, "nv12", (1920, 1080), win)
        got = cache.get("c", 5, "master", 1, "nv12", (1920, 1080))
        assert got is win
        assert cache.get("c", 5, "master", 2, "nv12", (1920, 1080)) is None

    def test_quality_and_resolution_isolation(self):
        """proxy 与 master、不同分辨率不得混用（计划 M2.2）。"""
        cache = TemporalFrameWindowCache()
        win = FrameWindow(clip_id="c", center_frame=5)
        cache.put("c", 5, "proxy", 1, "nv12", (960, 540), win)
        assert cache.get("c", 5, "master", 1, "nv12", (960, 540)) is None
        assert cache.get("c", 5, "proxy", 1, "nv12", (1920, 1080)) is None

    def test_generation_invalidation(self):
        """seek（generation 变化）后旧缓存失效。"""
        cache = TemporalFrameWindowCache()
        cache.put("c", 5, "master", 1, "nv12", (1920, 1080),
                  FrameWindow(clip_id="c", center_frame=5))
        cache.invalidate_generation("c", 2)
        assert cache.get("c", 5, "master", 1, "nv12", (1920, 1080)) is None
        assert cache.get_stats()["memory_bytes"] == 0

    def test_replacement_does_not_double_count_memory(self):
        cache = TemporalFrameWindowCache()
        first = FrameWindow("c", 5, center_frame_data=_FakeFrame(5, 128))
        second = FrameWindow("c", 5, center_frame_data=_FakeFrame(5, 256))
        cache.put("c", 5, "master", 1, "nv12", (1, 1), first)
        assert cache.get_stats()["memory_bytes"] == 128
        cache.put("c", 5, "master", 1, "nv12", (1, 1), second)
        assert cache.get_stats()["memory_bytes"] == 256

    def test_memory_budget_eviction(self):
        cache = TemporalFrameWindowCache(memory_budget_mb=1.0)  # 1MB
        for i in range(50):
            cache.put("c", i, "master", 1, "nv12", (1920, 1080),
                      FrameWindow(clip_id="c", center_frame=i,
                                  next_frames=[_FakeFrame(i)]))  # 每项 ~1KB
        stats = cache.get_stats()
        assert stats["memory_bytes"] <= 1024 * 1024
        assert stats["entries"] <= 50

    def test_hit_rate_stats(self):
        cache = TemporalFrameWindowCache()
        cache.put("c", 1, "master", 1, "nv12", (1, 1), FrameWindow("c", 1))
        cache.get("c", 1, "master", 1, "nv12", (1, 1))
        cache.get("c", 2, "master", 1, "nv12", (1, 1))
        stats = cache.get_stats()
        assert stats["hits"] == 1 and stats["misses"] == 1
        assert stats["hit_rate"] == 0.5
