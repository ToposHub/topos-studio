"""
P2-09 解码会话池测试。

覆盖：
- 有上限 + LRU 逐出（最旧优先、单次逐出上限、逐出在管理器锁外 close）
- 引用租约：在用会话不被逐出，归还后可被回收
- 逐出清理完整性：types/cache/refs/last_used/stream_locks/池克隆/busy 身份
- 交互预约身份语义（id 复用不再误伤健康会话）
- 负缓存：close_source 按素材清除、invalidate_all 全清、规模护栏
- 真实素材（4K H265）：小上限下多流轮转解码正确、被逐出的流可重建
"""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor

import pytest

import src.features.edit.core.edit_gpu_decode_manager as decode_manager_module
from src.features.edit.core.edit_gpu_decode_manager import EditGpuDecodeManager
from tests.edit.test_edit_gpu_decode_manager import (
    _CloseTrackingSource,
    _FakeSequentialPlanarSource,
)


def _use_cap(monkeypatch, cap: int) -> None:
    monkeypatch.setattr(decode_manager_module, "_MAX_LIVE_SOURCES", cap)


def _make_manager(monkeypatch, cap: int, created: list) -> EditGpuDecodeManager:
    """伪造 planar source 工厂的 manager（decode 路径与真实一致）。"""
    _use_cap(monkeypatch, cap)
    manager = EditGpuDecodeManager()
    manager._get_disk_proxy_info = lambda asset_path: None  # type: ignore[method-assign]

    def _create_source(asset_path: str, target_size, **kwargs):
        source = _CloseTrackingSource(current_frame=0)
        created.append(source)
        return source, "planar"

    manager._create_source = _create_source  # type: ignore[method-assign]
    return manager


def _source_key(manager: EditGpuDecodeManager, asset: str, stream_key: str):
    spec = manager._resolve_preview_decode_source(asset, (640, 360))
    return manager._make_source_key(manager._normalize_stream_key(asset, stream_key), spec)


class TestLruEviction:
    """有上限 + LRU 逐出。"""

    def test_evicts_oldest_over_cap(self, monkeypatch) -> None:
        created: list = []
        manager = _make_manager(monkeypatch, cap=3, created=created)

        for i in range(5):
            frame, frame_type = manager.get_frame(
                f"/a{i}.mov", 0, target_size=(640, 360), stream_key=f"clip-{i}"
            )
            assert frame is not None and frame_type == "planar"

        stats = manager.session_pool_stats()
        assert stats["primary_sources"] == 3
        assert stats["evictions"] == 2
        # 最旧的两个（clip-0/clip-1）被逐出关闭
        assert created[0].closed and created[1].closed
        assert not created[2].closed and not created[3].closed and not created[4].closed
        # 存活的是最新三个
        live_paths = {key[0] for key in manager._sources}
        assert live_paths == {"clip-2", "clip-3", "clip-4"}

    def test_evicted_key_recreates_on_next_request(self, monkeypatch) -> None:
        created: list = []
        manager = _make_manager(monkeypatch, cap=1, created=created)

        manager.get_frame("/a.mov", 0, target_size=(640, 360), stream_key="clip-a")
        manager.get_frame("/b.mov", 0, target_size=(640, 360), stream_key="clip-b")
        assert created[0].closed

        frame, frame_type = manager.get_frame(
            "/a.mov", 1, target_size=(640, 360), stream_key="clip-a"
        )
        assert frame is not None and frame.frame_index == 1
        assert frame_type == "planar"
        assert len(created) == 3  # a 被逐出后重建

    def test_eviction_closes_outside_manager_lock(self, monkeypatch) -> None:
        created: list = []
        manager = _make_manager(monkeypatch, cap=1, created=created)
        lock_checks: list[bool] = []

        manager.get_frame("/a.mov", 0, target_size=(640, 360), stream_key="clip-a")
        source_a = created[0]

        def _spy_close() -> None:
            source_a.closed = True
            # close VT 会话可能阻塞：绝不能拿着管理器锁做（P2-09 契约）
            acquired = manager._lock.acquire(blocking=False)
            lock_checks.append(acquired)
            if acquired:
                manager._lock.release()

        source_a.close = _spy_close  # type: ignore[method-assign]

        manager.get_frame("/b.mov", 0, target_size=(640, 360), stream_key="clip-b")

        assert source_a.closed
        assert lock_checks == [True]

    def test_eviction_cleans_containers_and_pool_clones(self, monkeypatch) -> None:
        created: list = []
        manager = _make_manager(monkeypatch, cap=2, created=created)

        manager.get_frame("/a.mov", 0, target_size=(640, 360), stream_key="clip-a")
        key_a = _source_key(manager, "/a.mov", "clip-a")
        # 手工放一个池克隆，验证逐出 key 时一并关闭清理
        clone = _CloseTrackingSource(current_frame=0)
        manager._interactive_source_pool[key_a] = [(clone, "planar")]

        manager.get_frame("/b.mov", 0, target_size=(640, 360), stream_key="clip-b")
        manager.get_frame("/c.mov", 0, target_size=(640, 360), stream_key="clip-c")

        assert key_a not in manager._sources
        assert key_a not in manager._source_cache
        assert key_a not in manager._source_types
        assert key_a not in manager._source_refs
        assert key_a not in manager._source_last_used
        assert key_a not in manager._stream_locks
        assert key_a not in manager._interactive_source_pool
        assert clone.closed


class TestLease:
    """引用租约：在用会话不被逐出。"""

    def test_leased_source_survives_eviction(self, monkeypatch) -> None:
        created: list = []
        manager = _make_manager(monkeypatch, cap=2, created=created)

        manager.get_frame("/a.mov", 0, target_size=(640, 360), stream_key="clip-a")
        manager.get_frame("/b.mov", 0, target_size=(640, 360), stream_key="clip-b")
        key_a = _source_key(manager, "/a.mov", "clip-a")
        key_b = _source_key(manager, "/b.mov", "clip-b")

        # 模拟 clip-a 正在解码（持租约）
        with manager._lock:
            manager._lease_source(key_a)

        # 插入第三个会话：必须跳过持租约的 clip-a，逐出 clip-b
        manager.get_frame("/c.mov", 0, target_size=(640, 360), stream_key="clip-c")

        assert key_a in manager._sources
        assert not created[0].closed
        assert key_b not in manager._sources
        assert created[1].closed

        manager._release_source(key_a)
        assert manager.session_pool_stats()["leased"] == 0

    def test_get_frame_holds_lease_during_decode(self, monkeypatch) -> None:
        created: list = []
        manager = _make_manager(monkeypatch, cap=8, created=created)
        seen_refs: list[int] = []

        original_decode = manager._decode_frame

        def _spy_decode(source, source_type, frame_index, target_size, start):
            # 解码临界区内：本会话应处于租约保护下
            key = _source_key(manager, "/a.mov", "clip-a")
            with manager._lock:
                seen_refs.append(manager._source_refs.get(key, 0))
            return original_decode(source, source_type, frame_index, target_size, start)

        manager._decode_frame = _spy_decode  # type: ignore[method-assign]

        manager.get_frame("/a.mov", 0, target_size=(640, 360), stream_key="clip-a")
        assert seen_refs == [1]
        assert manager.session_pool_stats()["leased"] == 0


class TestBusyIdentity:
    """交互预约的身份语义（P2-09 修复 id() 复用误伤）。"""

    def test_stale_id_entry_does_not_skip_healthy_source(self, monkeypatch) -> None:
        manager = EditGpuDecodeManager()
        primary = _FakeSequentialPlanarSource(current_frame=690)
        pool_busy = _FakeSequentialPlanarSource(current_frame=70)
        pool_free = _FakeSequentialPlanarSource(current_frame=20)
        source_key = ("clip", "/asset.mov", "/asset.mov", (1280, 720), "auto")
        manager._interactive_source_pool[source_key] = [
            (pool_busy, "vt_planar"),
            (pool_free, "vt_planar"),
        ]
        # 陈旧条目：id 键属于 pool_busy，但登记的本体是 pool_free
        # （旧 set 语义下 pool_busy 会被误判 busy 而跳过）
        manager._interactive_busy_sources[id(pool_busy)] = pool_free

        selected, _type = manager._select_interactive_source(
            source_key, primary, "vt_planar", 74, reserve=True,
        )

        assert selected is pool_busy
        manager._release_interactive_source(pool_busy)
        assert manager._interactive_busy_sources.get(id(pool_busy)) is None


class TestNegativeCache:
    """负缓存策略。"""

    def test_close_source_clears_negative_entries(self, monkeypatch) -> None:
        manager = EditGpuDecodeManager()
        manager._get_disk_proxy_info = lambda asset_path: None  # type: ignore[method-assign]
        calls: list[str] = []

        def _create_source(asset_path: str, target_size, **kwargs):
            calls.append(asset_path)
            return None, "cpu"

        manager._create_source = _create_source  # type: ignore[method-assign]

        frame, frame_type = manager.get_frame(
            "/broken.mov", 0, target_size=(640, 360), stream_key="clip-x"
        )
        assert frame is None and frame_type == ""
        assert manager.session_pool_stats()["negative_keys"] == 1

        # 负缓存命中：不重试
        manager.get_frame("/broken.mov", 1, target_size=(640, 360), stream_key="clip-x")
        assert len(calls) == 1

        manager.close_source("/broken.mov")
        assert manager.session_pool_stats()["negative_keys"] == 0

        # 清除后重新探测
        manager.get_frame("/broken.mov", 2, target_size=(640, 360), stream_key="clip-x")
        assert len(calls) == 2

    def test_invalidate_all_clears_negative_entries(self, monkeypatch) -> None:
        manager = EditGpuDecodeManager()
        manager._create_source = lambda asset_path, target_size, **kwargs: (None, "cpu")  # type: ignore[method-assign]
        manager.get_frame("/broken.mov", 0, target_size=(640, 360), stream_key="clip-x")
        assert manager.session_pool_stats()["negative_keys"] == 1

        manager.invalidate_all_sources()
        assert manager.session_pool_stats()["negative_keys"] == 0

    def test_negative_cache_is_bounded(self, monkeypatch) -> None:
        manager = EditGpuDecodeManager()
        manager._create_source = lambda asset_path, target_size, **kwargs: (None, "cpu")  # type: ignore[method-assign]
        for i in range(300):
            manager.get_frame(f"/b{i}.mov", 0, target_size=(640, 360), stream_key=f"c{i}")
        assert len(manager._failed_source_keys) <= 256


class TestStatsAndConcurrency:
    """诊断统计与并发冒烟。"""

    def test_session_pool_stats_shape(self, monkeypatch) -> None:
        created: list = []
        manager = _make_manager(monkeypatch, cap=4, created=created)
        stats = manager.session_pool_stats()
        assert stats == {
            "primary_sources": 0,
            "pool_clones": 0,
            "leased": 0,
            "negative_keys": 0,
            "evictions": 0,
            "max_live_sources": 4,
        }

    def test_concurrent_get_frame_keeps_cap_and_correctness(self, monkeypatch) -> None:
        created: list = []
        manager = _make_manager(monkeypatch, cap=4, created=created)

        def _decode(i: int):
            frame, frame_type = manager.get_frame(
                f"/a{i % 16}.mov", i % 7, target_size=(640, 360), stream_key=f"clip-{i % 16}"
            )
            return frame, frame_type

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(_decode, range(64)))

        assert all(frame is not None and ftype == "planar" for frame, ftype in results)
        stats = manager.session_pool_stats()
        assert stats["primary_sources"] <= 4
        assert stats["leased"] == 0


class TestRealVideoLifecycle:
    """真实素材（4K H265）小上限轮转：解码正确 + 逐出后可重建。"""

    def test_real_hevc_rotation_under_small_cap(self, monkeypatch) -> None:
        test_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        path = os.path.join(test_root, "tests", "test_videos", "4K_H265.mov")
        if not os.path.exists(path):
            pytest.skip(f"real test video not available: {path}")

        _use_cap(monkeypatch, 2)
        manager = EditGpuDecodeManager()
        manager._get_disk_proxy_info = lambda asset_path: None  # type: ignore[method-assign]

        types_seen: set[str] = set()
        for round_index in range(2):
            for stream in ("clip-1", "clip-2", "clip-3"):
                frame, frame_type = manager.get_frame(
                    path, 0, target_size=(640, 360), stream_key=stream
                )
                assert frame is not None, f"round={round_index} stream={stream}"
                types_seen.add(frame_type)

        assert types_seen, "至少一条解码路径出帧"
        stats = manager.session_pool_stats()
        assert stats["primary_sources"] <= 2
        assert stats["evictions"] >= 3
