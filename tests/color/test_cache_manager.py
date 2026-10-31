"""
Cache Manager Integration Tests

测试 CacheManager 集成和现有缓存的协调。
"""

import unittest
import threading
import time
import numpy as np
from typing import Optional

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from src.features.color.core.cache_manager import (
    CacheManager,
    CacheConfig,
    CacheWarmer,
    MemoryPressureMonitor,
    CacheTier,
    FrameCacheKey,
    get_cache_manager,
    reset_cache_manager,
)


class TestCacheManager(unittest.TestCase):
    """CacheManager 测试"""

    def setUp(self) -> None:
        """测试前准备"""
        reset_cache_manager()
        self.config = CacheConfig(
            max_decode_frames=8,
            max_render_plans=4,
            max_gpu_textures=4,
            max_timeline_previews=4,
            max_audio_waveforms=10,
            max_thumbnails=100,
        )
        self.cache_mgr = CacheManager(self.config)

    def tearDown(self) -> None:
        """测试后清理"""
        reset_cache_manager()

    def test_decode_frame_cache_put_get(self) -> None:
        """测试解码帧缓存的 put/get"""
        clip_id = "test_clip_001"
        frame_index = 0
        quality = "proxy"

        # 创建测试帧数据
        frame_data = np.random.randint(0, 255, (100, 100, 3), dtype=np.uint8)

        # 缓存帧
        self.cache_mgr.decode_cache.put(
            clip_id, frame_index, quality, frame_data
        )

        # 获取帧
        cached_frame = self.cache_mgr.decode_cache.get(
            clip_id, frame_index, quality
        )

        self.assertIsNotNone(cached_frame)
        np.testing.assert_array_equal(frame_data, cached_frame)

    def test_decode_frame_cache_lru_eviction(self) -> None:
        """测试 LRU 淘汰"""
        clip_id = "test_clip"
        quality = "proxy"

        # 缓存超过最大数量的帧
        for i in range(10):
            frame_data = np.zeros((10, 10, 3), dtype=np.uint8)
            self.cache_mgr.decode_cache.put(clip_id, i, quality, frame_data)

        # 最早的帧应该被淘汰
        old_frame = self.cache_mgr.decode_cache.get(clip_id, 0, quality)
        self.assertIsNone(old_frame)

        # 最新的帧应该还在
        new_frame = self.cache_mgr.decode_cache.get(clip_id, 9, quality)
        self.assertIsNotNone(new_frame)

    def test_decode_frame_cache_quality_separation(self) -> None:
        """测试 proxy/master 分离"""
        clip_id = "test_clip"
        frame_index = 0

        proxy_frame = np.ones((10, 10, 3), dtype=np.uint8) * 100
        master_frame = np.ones((10, 10, 3), dtype=np.uint8) * 200

        # 缓存不同质量的帧
        self.cache_mgr.decode_cache.put(clip_id, frame_index, "proxy", proxy_frame)
        self.cache_mgr.decode_cache.put(clip_id, frame_index, "master", master_frame)

        # 验证分离
        cached_proxy = self.cache_mgr.decode_cache.get(clip_id, frame_index, "proxy")
        cached_master = self.cache_mgr.decode_cache.get(clip_id, frame_index, "master")

        self.assertIsNotNone(cached_proxy)
        self.assertIsNotNone(cached_master)
        np.testing.assert_array_equal(proxy_frame, cached_proxy)
        np.testing.assert_array_equal(master_frame, cached_master)

    def test_render_plan_cache(self) -> None:
        """测试渲染计划缓存"""
        frame_index = 10

        # 模拟渲染计划
        render_plan = {"layers": [], "transitions": []}

        self.cache_mgr.render_plan_cache.put(frame_index, render_plan)
        cached_plan = self.cache_mgr.render_plan_cache.get(frame_index)

        self.assertIsNotNone(cached_plan)
        self.assertEqual(render_plan, cached_plan)

    def test_timeline_preview_cache(self) -> None:
        """测试时间线预览缓存"""
        frame_index = 5

        # 模拟预览数据
        preview_data = np.random.randint(0, 255, (100, 100, 3), dtype=np.uint8)

        self.cache_mgr.timeline_preview_cache.put(frame_index, preview_data)
        cached_preview = self.cache_mgr.timeline_preview_cache.get(frame_index)

        self.assertIsNotNone(cached_preview)
        np.testing.assert_array_equal(preview_data, cached_preview)

    def test_audio_waveform_cache(self) -> None:
        """测试音频波形缓存"""
        asset_id = "audio_001"

        # 模拟波形数据
        waveform_data = {
            "peaks_min": np.random.uniform(-1.0, 0.0, 100),
            "peaks_max": np.random.uniform(0.0, 1.0, 100),
        }

        self.cache_mgr.audio_waveform_cache.put(asset_id, waveform_data)
        cached_waveform = self.cache_mgr.audio_waveform_cache.get(asset_id)

        self.assertIsNotNone(cached_waveform)
        self.assertEqual(waveform_data, cached_waveform)

    def test_thumbnail_cache(self) -> None:
        """测试缩略图缓存"""
        asset_path = "/path/to/image.jpg"

        # 模拟缩略图数据
        thumbnail_data = np.random.randint(0, 255, (128, 128, 3), dtype=np.uint8)

        self.cache_mgr.thumbnail_cache.put(asset_path, thumbnail_data)
        cached_thumb = self.cache_mgr.thumbnail_cache.get(asset_path)

        self.assertIsNotNone(cached_thumb)
        np.testing.assert_array_equal(thumbnail_data, cached_thumb)

    def test_stats_collection(self) -> None:
        """测试统计信息收集"""
        clip_id = "test_clip"
        quality = "proxy"

        # 执行一些缓存操作
        for i in range(5):
            frame_data = np.zeros((10, 10, 3), dtype=np.uint8)
            self.cache_mgr.decode_cache.put(clip_id, i, quality, frame_data)

        # 命中
        self.cache_mgr.decode_cache.get(clip_id, 0, quality)
        self.cache_mgr.decode_cache.get(clip_id, 1, quality)

        # 未命中
        self.cache_mgr.decode_cache.get(clip_id, 99, quality)

        stats = self.cache_mgr.get_stats()

        self.assertIn("decode_frame", stats)
        self.assertIn("total", stats)
        self.assertGreater(stats["total"]["hits"], 0)
        self.assertGreater(stats["total"]["misses"], 0)

    def test_clear_all(self) -> None:
        """测试清空所有缓存"""
        clip_id = "test_clip"
        frame_data = np.zeros((10, 10, 3), dtype=np.uint8)

        # 缓存一些数据
        self.cache_mgr.decode_cache.put(clip_id, 0, "proxy", frame_data)
        self.cache_mgr.render_plan_cache.put(0, {"test": True})

        # 清空
        cleared = self.cache_mgr.clear_all()

        self.assertGreater(cleared["decode_frame"], 0)
        self.assertGreater(cleared["render_plan"], 0)

        # 验证已清空
        self.assertIsNone(self.cache_mgr.decode_cache.get(clip_id, 0, "proxy"))

    def test_clear_tier(self) -> None:
        """测试清空指定层级"""
        clip_id = "test_clip"
        frame_data = np.zeros((10, 10, 3), dtype=np.uint8)

        self.cache_mgr.decode_cache.put(clip_id, 0, "proxy", frame_data)
        self.cache_mgr.render_plan_cache.put(0, {"test": True})

        # 只清空解码帧缓存
        self.cache_mgr.clear_tier(CacheTier.DECODE_FRAME)

        # 解码帧已清空
        self.assertIsNone(self.cache_mgr.decode_cache.get(clip_id, 0, "proxy"))
        # 渲染计划还在
        self.assertIsNotNone(self.cache_mgr.render_plan_cache.get(0))

    def test_memory_pressure_low(self) -> None:
        """测试低内存压力"""
        # 缓存缩略图
        self.cache_mgr.thumbnail_cache.put("/path/1.jpg", np.zeros((10, 10, 3)))
        self.cache_mgr.thumbnail_cache.put("/path/2.jpg", np.zeros((10, 10, 3)))

        # 触发低压力
        self.cache_mgr.on_memory_pressure("low")

        # 缩略图应被清空
        self.assertIsNone(self.cache_mgr.thumbnail_cache.get("/path/1.jpg"))
        self.assertIsNone(self.cache_mgr.thumbnail_cache.get("/path/2.jpg"))

    def test_memory_pressure_critical(self) -> None:
        """测试严重内存压力"""
        clip_id = "test_clip"
        frame_data = np.zeros((10, 10, 3), dtype=np.uint8)

        # 缓存各种数据
        self.cache_mgr.decode_cache.put(clip_id, 0, "proxy", frame_data)
        self.cache_mgr.render_plan_cache.put(0, {"test": True})
        self.cache_mgr.timeline_preview_cache.put(0, np.zeros((10, 10, 3)))
        self.cache_mgr.thumbnail_cache.put("/path/1.jpg", np.zeros((10, 10, 3)))

        # 触发严重压力
        self.cache_mgr.on_memory_pressure("critical")

        # 非关键缓存应被清空
        self.assertIsNone(self.cache_mgr.thumbnail_cache.get("/path/1.jpg"))
        self.assertIsNone(self.cache_mgr.timeline_preview_cache.get(0))
        self.assertIsNone(self.cache_mgr.render_plan_cache.get(0))

    def test_memory_callback(self) -> None:
        """测试内存压力回调"""
        callback_called = []

        def callback(level: str) -> None:
            callback_called.append(level)

        self.cache_mgr.register_memory_callback(callback)
        self.cache_mgr.on_memory_pressure("medium")

        self.assertEqual(callback_called, ["medium"])

    def test_thread_safety(self) -> None:
        """测试线程安全"""
        clip_id = "test_clip"
        errors = []

        def writer(start: int, count: int) -> None:
            try:
                for i in range(start, start + count):
                    frame_data = np.zeros((10, 10, 3), dtype=np.uint8)
                    self.cache_mgr.decode_cache.put(clip_id, i, "proxy", frame_data)
            except Exception as e:
                errors.append(e)

        def reader(start: int, count: int) -> None:
            try:
                for i in range(start, start + count):
                    self.cache_mgr.decode_cache.get(clip_id, i, "proxy")
            except Exception as e:
                errors.append(e)

        # 创建多个线程
        threads = []
        for i in range(4):
            t1 = threading.Thread(target=writer, args=(i * 10, 10))
            t2 = threading.Thread(target=reader, args=(i * 10, 10))
            threads.extend([t1, t2])

        # 启动所有线程
        for t in threads:
            t.start()

        # 等待所有线程完成
        for t in threads:
            t.join()

        # 验证没有错误
        self.assertEqual(errors, [])

    def test_cache_warmer(self) -> None:
        """测试缓存预热"""
        clip_id = "test_clip"
        decoded_frames = []

        def mock_decode(cid: str, frame_idx: int, q: str) -> Optional[np.ndarray]:
            decoded_frames.append(frame_idx)
            return np.zeros((10, 10, 3), dtype=np.uint8)

        warmer = CacheWarmer(self.cache_mgr)
        warmer.start_warmup(
            clip_id,
            start_frame=0,
            quality="proxy",
            lookahead=5,
            decode_callback=mock_decode,
        )

        # 等待预热完成
        time.sleep(0.5)

        # 验证预热了帧
        self.assertGreater(len(decoded_frames), 0)

        # 停止预热
        warmer.stop_warmup()

    def test_update_config(self) -> None:
        """测试更新配置"""
        new_config = CacheConfig(
            max_decode_frames=16,
            max_render_plans=8,
        )

        self.cache_mgr.update_config(new_config)

        self.assertEqual(self.cache_mgr.config.max_decode_frames, 16)
        self.assertEqual(self.cache_mgr.decode_cache.proxy_cache.max_size, 16)


class TestGlobalCacheManager(unittest.TestCase):
    """全局缓存管理器测试"""

    def setUp(self) -> None:
        """测试前准备"""
        reset_cache_manager()

    def tearDown(self) -> None:
        """测试后清理"""
        reset_cache_manager()

    def test_get_cache_manager_singleton(self) -> None:
        """测试单例模式"""
        mgr1 = get_cache_manager()
        mgr2 = get_cache_manager()

        self.assertIs(mgr1, mgr2)

    def test_reset_cache_manager(self) -> None:
        """测试重置"""
        mgr1 = get_cache_manager()
        reset_cache_manager()
        mgr2 = get_cache_manager()

        self.assertIsNot(mgr1, mgr2)


class TestCacheKey(unittest.TestCase):
    """缓存键测试"""

    def test_frame_cache_key_hashable(self) -> None:
        """测试帧缓存键可哈希"""
        key1 = FrameCacheKey(
            clip_id="clip1",
            frame_index=0,
            quality="proxy",
            width=1920,
            height=1080,
        )
        key2 = FrameCacheKey(
            clip_id="clip1",
            frame_index=0,
            quality="proxy",
            width=1920,
            height=1080,
        )

        # 相同键应相等
        self.assertEqual(key1, key2)

        # 可用作字典键
        d = {key1: "value"}
        self.assertEqual(d[key2], "value")

    def test_frame_cache_key_different_quality(self) -> None:
        """测试不同质量的键不同"""
        key_proxy = FrameCacheKey(
            clip_id="clip1",
            frame_index=0,
            quality="proxy",
            width=960,
            height=540,
        )
        key_master = FrameCacheKey(
            clip_id="clip1",
            frame_index=0,
            quality="master",
            width=1920,
            height=1080,
        )

        self.assertNotEqual(key_proxy, key_master)


if __name__ == '__main__':
    unittest.main()
