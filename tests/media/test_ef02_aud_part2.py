"""批 EF-02 part 2 —— src/shared/media 22 条 Suggestion 级审计修复的回归测试。

覆盖 AUD-35309..36115（清单见 docs/plans/2026-09-28-multi-machine-program/
REMEDIATION_PLAN.md 行 2103-2125）。每条修复配正例（修复后行为）与
反例（修复回退时必须失败）：

- 行为级测试：直接执行被改代码路径，断言新契约；
- 源码级测试：对纯性能/文档/锁位置类修复，用 inspect/AST 钉死修复
  形态——回退旧实现即失败。

约束：不真实解码大媒体文件、不创建真实 GPU/COM 资源、不弹模态对话框。
"""

from __future__ import annotations

import ast
import importlib
import inspect
import os
import sys
import threading
import time
from pathlib import Path
from typing import List

import numpy as np
import pytest

from src.shared.media.performance_monitor import VideoPerformanceMonitor
from src.shared.media.pixel_format import PixelFormatRegistry, Subsampling
from src.shared.media.proxy import DiskProxyConfig
from src.shared.media.proxy_generator import ProxyGenerator
from src.shared.media.proxy_manager import ProxyManager, ProxyTask
from src.shared.media.proxy_manifest import (
    ProxyManifest,
    ProxyManifestEntry,
    ProxyManifestKey,
)
from src.shared.media.proxy import ProxyStatus
from src.shared.media.seek import Rational, frame_to_pts
from src.shared.media.topos_image_sequence import PlaneCache

REPO_ROOT = Path(__file__).resolve().parents[2]
MEDIA_DIR = REPO_ROOT / "src" / "shared" / "media"


def _read(rel: str) -> str:
    return (MEDIA_DIR / rel).read_text(encoding="utf-8")


# =============================================================================
# AUD-35309：VtDecodeOutputQueue 计数器必须持 _lock 读写
# =============================================================================

class TestAUD35309:
    def test_counters_updated_under_lock(self) -> None:
        """正例+反例：回退（锁外 += / 裸属性读）时本测试失败。"""
        from src.shared.media.macos_vt.vt_session import VtDecodeOutputQueue

        src = inspect.getsource(VtDecodeOutputQueue)
        # put 路径：两个计数器增量都必须在 with self._lock 块内
        put_src = inspect.getsource(VtDecodeOutputQueue.put_pixel_buffer)
        assert "with self._lock:" in put_src, "计数器增量未持锁"
        assert "_total_count += 1" in put_src
        assert "_dropped_count += 1" in put_src
        # 属性读也持锁
        for prop in ("dropped_count", "total_count"):
            prop_src = inspect.getsource(
                getattr(VtDecodeOutputQueue, prop).fget)
            assert "with self._lock:" in prop_src, f"{prop} 读未持锁"
        # 锁对象确实存在且非死代码（旧实现创建后从未使用）
        assert "self._lock = threading.Lock()" in src

    def test_counter_behavior(self) -> None:
        """正例：诊断计数器初始为 0，属性可安全并发读取。"""
        from src.shared.media.macos_vt.vt_session import VtDecodeOutputQueue

        q = VtDecodeOutputQueue(max_size=2)
        assert q.total_count == 0
        assert q.dropped_count == 0
        results: List[int] = []

        def reader() -> None:
            for _ in range(200):
                results.append(q.total_count + q.dropped_count)

        threads = [threading.Thread(target=reader) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5.0)
        assert len(results) == 800


# =============================================================================
# AUD-35319：verify_color reference 仅首帧计算
# =============================================================================

class TestAUD35319:
    def test_reference_read_only_before_first_render(self) -> None:
        """正例+反例：回退（每帧无条件读 reference）时本测试失败。"""
        src = _read("macos_vt/vt_zero_copy_probe.py")
        # reference 计算点必须带 frames_rendered_count == 0 守卫
        assert (
            "if verify_color and frames_rendered_count == 0 and pb.pixel_buffer is not None:"
            in src
        ), "reference 读取缺少首帧守卫（AUD-35319 修复被回退）"
        # 消费点仍是首帧渲染后一次
        assert "if verify_color and frames_rendered_count == 1:" in src


# =============================================================================
# AUD-35407：check_proxy_ladder 死分支/死变量/docstring
# =============================================================================

class TestAUD35407:
    def test_no_dead_branch_no_dead_variable(self) -> None:
        """正例+反例：回退（恢复 elif current_index == -1 / frame_budget_ms）时失败。"""
        src = inspect.getsource(VideoPerformanceMonitor.check_proxy_ladder)
        assert "elif current_index == -1" not in src, "死分支未删除"
        assert "frame_budget_ms" not in src, "死变量未删除"
        # docstring 不再宣称 p95 直接驱动判定
        assert "total_p95 > frame_budget * 1.25 连续 60 帧" not in src

    def test_downgrade_from_original_index(self) -> None:
        """正例：current_index == -1（原始分辨率）在连续慢帧下降到第一级。"""
        m = VideoPerformanceMonitor()
        m._original_long_edge = 2160
        m._current_proxy_long_edge = 2160
        m._proxy_ladder_index = -1
        m._consecutive_slow_frames = 60
        m._total_frame_times = [50.0] * 60
        long_edge, reason = m.check_proxy_ladder()
        assert long_edge != -1 and long_edge > 0
        assert reason.startswith("slow_frames=")


# =============================================================================
# AUD-35409：get_aggregate_stats 每类数据只统计一次
# =============================================================================

class TestAUD35409:
    def test_decode_stats_computed_once(self) -> None:
        """正例+反例：回退（同一数据重复 _calculate_stats 4 次）时失败。"""
        m = VideoPerformanceMonitor()
        for i in range(20):
            m.track_decode_time(i, 10.0 + i)
        calls: List[tuple] = []
        original = m._calculate_stats

        def counting_stats(values):
            calls.append(tuple(values))
            return original(values)

        m._calculate_stats = counting_stats  # type: ignore[method-assign]
        stats = m.get_aggregate_stats()
        decode_snapshot = tuple(10.0 + i for i in range(20))
        decode_computations = sum(1 for c in calls if c == decode_snapshot)
        assert decode_computations == 1, (
            f"decode 数据被统计 {decode_computations} 次（AUD-35409 回退）"
        )
        # 结果仍然正确（期望值用同一实现单次计算）
        expected = original(list(decode_snapshot))
        assert stats["decode_max_ms"] == expected["max"]
        assert stats["decode_p95_ms"] == expected["p95"]
        assert stats["decode_p50_ms"] == expected["p50"]


# =============================================================================
# AUD-35502：pixel_format 注释/文档与实现一致
# =============================================================================

class TestAUD35502:
    def test_docs_match_implementation(self) -> None:
        """正例+反例：回退（恢复错误注释）时失败。"""
        src = _read("pixel_format.py")
        # 4:1:0 垂直子采样 = 1/2（与 chroma_height_divisor 返回 2 一致）
        assert "YUV_410: 水平 1/4，垂直 1/2" in src
        assert "YUV_410: 水平 1/4，垂直 1/4" not in src
        # chroma_bits 算术注释可读（旧算式不通）
        assert "6 bits/2 pixels" not in src
        # ProRes 4444 不再宣称注册表条目带 alpha
        assert "yuv444p10 (10-bit 4:4:4 with alpha)" not in src

    def test_yuv410_vertical_divisor_is_two(self) -> None:
        """正例：实现侧 4:1:0 垂直除数为 2（文档修正的依据）。"""
        assert Subsampling.YUV_410.chroma_height_divisor == 2
        assert Subsampling.YUV_410.chroma_width_divisor == 4
        PixelFormatRegistry._ensure_initialized()
        info = PixelFormatRegistry.get("yuv420p")
        assert info is not None and info.chroma_bits_per_pixel == 12


# =============================================================================
# AUD-35605：generate() 不再吞掉启动窗口内的取消
# =============================================================================

class TestAUD35605:
    def test_cancel_before_generate_survives(self) -> None:
        """正例+反例：回退（入口重置 _cancelled）时本测试失败。

        对不存在的源文件 generate() 走最早失败路径——旧实现已在入口把
        _cancelled 清零，新实现保留取消标志。
        """
        gen = ProxyGenerator(DiskProxyConfig())
        gen.cancel()
        assert gen.is_cancelled
        result = gen.generate(source_path="/nonexistent/for_aud35605.mp4")
        assert result.success is False
        # 关键断言：取消标志未被 generate() 吞掉
        assert gen.is_cancelled, "generate() 重置了已置位的取消标志（回退）"

    def test_generate_does_not_reset_flag(self) -> None:
        """源码级：generate() 不再出现无条件重置。"""
        src = inspect.getsource(ProxyGenerator.generate)
        assert "self._cancelled = False" not in src


# =============================================================================
# AUD-35611：_tasks 终态任务有界保留
# =============================================================================

class TestAUD35611:
    def test_terminal_tasks_pruned_with_retention(self) -> None:
        """正例+反例：回退（无 prune / _tasks 无界增长）时失败。"""
        m = ProxyManager(DiskProxyConfig())
        m._terminal_task_retention = 2
        with m._lock:
            for i, status in enumerate(
                ("complete", "complete", "complete", "failed")
            ):
                task = ProxyTask(
                    task_id=f"t{i}", asset_id=f"a{i}",
                    source_path=f"/src{i}.mp4", status=status,
                    completed_at=float(i + 1),
                )
                m._tasks[task.task_id] = task
                m._source_to_task[task.source_path] = task.task_id
            pruned = m._prune_terminal_tasks_locked()

        assert pruned == 2
        # 最旧的 t0/t1 被淘汰，t2/t3 保留
        assert "t0" not in m._tasks and "t1" not in m._tasks
        assert "t2" in m._tasks and "t3" in m._tasks
        # 被淘汰任务的 source 映射同步清理
        assert "/src0.mp4" not in m._source_to_task
        assert m._source_to_task.get("/src2.mp4") == "t2"

    def test_active_tasks_never_pruned(self) -> None:
        m = ProxyManager(DiskProxyConfig())
        m._terminal_task_retention = 0
        with m._lock:
            active = ProxyTask(
                task_id="live", asset_id="a", source_path="/live.mp4",
                status="generating", completed_at=None,
            )
            m._tasks["live"] = active
            assert m._prune_terminal_tasks_locked() == 0
            assert "live" in m._tasks


# =============================================================================
# AUD-35612：get_valid_proxy_path 校验与取值同锁
# =============================================================================

class TestAUD35612:
    def test_valid_path_under_lock(self) -> None:
        """正例+反例：回退（锁外读 _proxy_cache）时源码断言失败。"""
        src = inspect.getsource(ProxyManager.get_valid_proxy_path)
        assert "with self._lock:" in src, "校验与取值未在同一临界区"
        assert "is_proxy_valid" in src

    def test_valid_and_invalid_paths(self, tmp_path: Path) -> None:
        m = ProxyManager(DiskProxyConfig())
        proxy = tmp_path / "p.mov"
        proxy.write_bytes(b"x")
        source = tmp_path / "s.mp4"
        source.write_bytes(b"y")
        m._proxy_cache[str(source)] = str(proxy)
        try:
            # get_source_file_info 返回 (st_mtime 秒, st_size)
            st = source.stat()
            m._cache_version[str(source)] = (st.st_mtime, st.st_size)
            assert m.get_valid_proxy_path(str(source)) == str(proxy)
        finally:
            m._proxy_cache.pop(str(source), None)
            m._cache_version.pop(str(source), None)
        assert m.get_valid_proxy_path(str(source)) is None


# =============================================================================
# AUD-35613：磁盘 IO 移出管理器锁
# =============================================================================

class _InstrumentedRLock:
    """RLock 替身：depth 计数让测试可断言"此刻无人持锁"。

    threading.RLock 无 owner/locked 查询；包装后 with/acquire/release
    语义不变，depth == 0 当且仅当锁被完全释放。
    """

    def __init__(self) -> None:
        self._inner = threading.RLock()
        self.depth = 0

    def __enter__(self) -> "_InstrumentedRLock":
        self._inner.acquire()
        self.depth += 1
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.depth -= 1
        self._inner.release()

    def acquire(self, blocking: bool = True, timeout: float = -1.0) -> bool:
        acquired = self._inner.acquire(blocking, timeout)
        if acquired:
            self.depth += 1
        return acquired

    def release(self) -> None:
        self.depth -= 1
        self._inner.release()


class TestAUD35613:
    def test_is_proxy_valid_io_outside_lock(self, tmp_path: Path) -> None:
        """正例+反例：回退（stat 在锁内执行）时断言失败。"""
        import src.shared.media.proxy_manager as pm_module

        m = ProxyManager(DiskProxyConfig())
        m._lock = _InstrumentedRLock()
        proxy = tmp_path / "p.mov"
        proxy.write_bytes(b"x")
        source = tmp_path / "s.mp4"
        source.write_bytes(b"y")
        st = source.stat()
        m._proxy_cache[str(source)] = str(proxy)
        m._cache_version[str(source)] = (st.st_mtime, st.st_size)
        original = pm_module.get_source_file_info
        lock_depths: List[int] = []

        def spy(path: str):
            lock_depths.append(m._lock.depth)
            return (st.st_mtime, st.st_size)

        pm_module.get_source_file_info = spy
        try:
            assert m.is_proxy_valid(str(source)) is True
        finally:
            pm_module.get_source_file_info = original
        assert lock_depths and lock_depths == [0], (
            "get_source_file_info 在持锁状态下执行（AUD-35613 回退）"
        )

    def test_get_stats_io_outside_lock(self, tmp_path: Path) -> None:
        import src.shared.media.proxy_manager as pm_module

        m = ProxyManager(DiskProxyConfig())
        m._lock = _InstrumentedRLock()
        proxy = tmp_path / "p.mov"
        proxy.write_bytes(b"x" * 3)
        m._proxy_cache[str(tmp_path / "s.mp4")] = str(proxy)
        original_getsize = os.path.getsize
        lock_depths: List[int] = []

        def spy_getsize(path: str) -> int:
            lock_depths.append(m._lock.depth)
            return original_getsize(path)

        saved = os.path.getsize
        os.path.getsize = spy_getsize  # type: ignore[assignment]
        try:
            stats = m.get_stats()
        finally:
            os.path.getsize = saved
        assert stats.total_proxy_size_bytes == 3
        assert stats.assets_with_proxy == 1
        assert lock_depths and not any(lock_depths), (
            "getsize 在持锁状态下执行（AUD-35613 回退）"
        )

    def test_cleanup_expired_io_outside_lock(self, tmp_path: Path) -> None:
        import src.shared.media.proxy_manager as pm_module

        m = ProxyManager(DiskProxyConfig())
        m._lock = _InstrumentedRLock()
        source = tmp_path / "s.mp4"
        source.write_bytes(b"y")
        proxy = tmp_path / "p.mov"
        proxy.write_bytes(b"x")
        m._proxy_cache[str(source)] = str(proxy)
        m._cache_version[str(source)] = (0.0, 1)  # mtime 不匹配 → 过期
        original = pm_module.get_source_file_info
        lock_depths: List[int] = []

        def spy(path: str):
            lock_depths.append(m._lock.depth)
            return (999.0, 1)

        pm_module.get_source_file_info = spy
        try:
            cleaned = m.cleanup_expired_proxies()
        finally:
            pm_module.get_source_file_info = original
        assert cleaned == 1
        assert not proxy.exists()
        assert lock_depths and not any(lock_depths), (
            "cleanup 的源文件 stat 在持锁状态下执行（AUD-35613 回退）"
        )


# =============================================================================
# AUD-35617：get_entry/upsert_entry 不暴露内部可变 entry
# =============================================================================

def _make_entry(source: str, *, status: ProxyStatus = ProxyStatus.READY,
                updated_at: float = 100.0) -> ProxyManifestEntry:
    key = ProxyManifestKey(
        source_path=source, source_mtime_ns=1, source_size=2,
        codec_policy="prores_proxy", resolution_policy="half",
        app_version="1")
    return ProxyManifestEntry(
        key=key, status=status, proxy_path="/proxy/p.mov",
        width=960, height=540, codec="prores_ks",
        pixel_format="yuv422p10le", created_at=updated_at - 1,
        updated_at=updated_at)


class TestAUD35617:
    def test_get_entry_returns_copy(self, tmp_path: Path) -> None:
        """正例+反例：回退（返回内部引用）时本测试失败。"""
        manifest = ProxyManifest(tmp_path)
        manifest.load()  # 显式加载（get_entry 的懒加载会清空未加载缓存）
        manifest.upsert_entry(_make_entry("/a.mp4"))
        got = manifest.get_entry("/a.mp4")
        assert got is not None
        # 拿到的不是内部对象
        with manifest._lock:
            internal = next(iter(manifest._entries.values()))
        assert got is not internal, "get_entry 返回内部可变对象（回退）"
        # 改写副本不影响 manifest
        got.status = ProxyStatus.FAILED
        again = manifest.get_entry("/a.mp4")
        assert again is not None and again.status == ProxyStatus.READY

    def test_upsert_stores_copy(self, tmp_path: Path) -> None:
        manifest = ProxyManifest(tmp_path)
        manifest.load()
        entry = _make_entry("/b.mp4")
        manifest.upsert_entry(entry)
        entry.status = ProxyStatus.STALE  # 事后原地改写调用方对象
        got = manifest.get_entry("/b.mp4")
        assert got is not None
        assert got.status == ProxyStatus.READY, (
            "upsert 未存副本，调用方原地改写穿透到 manifest（回退）"
        )


# =============================================================================
# AUD-35618：ensure_loaded double-checked locking
# =============================================================================

class TestAUD35618:
    def test_concurrent_first_access_loads_once(self, tmp_path: Path) -> None:
        """正例+反例：回退（_loaded 锁外读取）时 load 被执行两次。"""
        manifest = ProxyManifest(tmp_path)
        calls: List[int] = []
        first_entered = threading.Event()
        release = threading.Event()

        def fake_load() -> None:
            calls.append(1)
            if len(calls) == 1:
                first_entered.set()
                release.wait(5.0)
            manifest._loaded = True

        manifest.load = fake_load  # type: ignore[method-assign]
        t1 = threading.Thread(target=manifest.ensure_loaded)
        t1.start()
        assert first_entered.wait(5.0)
        t2 = threading.Thread(target=manifest.ensure_loaded)
        t2.start()
        time.sleep(0.2)  # 让 t2 走到检查点
        release.set()
        t1.join(5.0)
        t2.join(5.0)
        assert len(calls) == 1, (
            f"并发首访触发 {len(calls)} 次 load（AUD-35618 回退）"
        )


# =============================================================================
# AUD-35704：frame_to_pts docstring 示例正确
# =============================================================================

class TestAUD35704:
    def test_docstring_example_matches_implementation(self) -> None:
        """正例+反例：回退（错误示例 100）时失败。"""
        doc = inspect.getdoc(frame_to_pts) or ""
        assert "100100" in doc, "docstring 未修正为真实输出"
        # 实现校验：100 * 1001 = 100100
        assert frame_to_pts(100, Rational(24000, 1001)) == 100100


# =============================================================================
# AUD-35714：_cleanup_file_lock 在提取路径 finally 中被调用
# =============================================================================

class TestAUD35714:
    def test_cleanup_called_in_extraction_paths(self) -> None:
        """正例+反例：回退（移除调用点）时失败。"""
        import src.shared.media.thumbnail_provider as tp

        src = _read("thumbnail_provider.py")
        # 定义 1 处 + 三个提取路径调用点
        assert src.count("_cleanup_file_lock(") >= 4, (
            "_cleanup_file_lock 调用点缺失（AUD-35714 回退）"
        )
        # 行为正例：清理函数本身生效
        lock = tp._get_file_lock("/fake/video_for_aud35714.mp4")
        assert "/fake/video_for_aud35714.mp4" in tp._cv_locks
        tp._cleanup_file_lock("/fake/video_for_aud35714.mp4")
        assert "/fake/video_for_aud35714.mp4" not in tp._cv_locks
        del lock


# =============================================================================
# AUD-35717：_get_bridge 重建持锁
# =============================================================================

class TestAUD35717:
    def test_bridge_rebuild_is_locked_and_unique(self) -> None:
        """正例+反例：回退（无锁 check-then-act）时源码断言失败。"""
        import src.shared.media.thumbnail_provider as tp

        src = inspect.getsource(tp._get_bridge)
        assert "_bridge_rebuild_lock" in src, "bridge 重建未持锁（回退）"
        # 行为：并发调用拿到同一个活跃 bridge
        results: List[object] = []
        barrier = threading.Barrier(4)

        def worker() -> None:
            barrier.wait()
            for _ in range(50):
                results.append(tp._get_bridge())

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10.0)
        assert results and all(r is results[0] for r in results)


# =============================================================================
# AUD-35718：单例加锁 + 主线程守卫
# =============================================================================

class TestAUD35718:
    def test_instance_guarded_by_lock_and_main_thread_assert(self) -> None:
        """正例+反例：回退（无锁无守卫）时失败。"""
        import src.shared.media.thumbnail_provider as tp

        src = inspect.getsource(tp.ThumbnailProvider.instance)
        assert "cls._instance_lock" in src, "instance() 未加锁（回退）"
        assert "QApplication" in src and "QThread" in src, (
            "instance() 缺主线程守卫（回退）"
        )

    def test_instance_raises_off_gui_thread(self, monkeypatch) -> None:
        """正例：有 QApplication 且当前非 GUI 线程时拒绝构造。"""
        import src.shared.media.thumbnail_provider as tp

        saved_instance = tp.ThumbnailProvider._instance
        tp.ThumbnailProvider._instance = None
        try:
            class FakeApp:
                @staticmethod
                def instance():
                    return FakeApp()

                def thread(self):
                    return object()  # 与 currentThread() 不同的对象

            monkeypatch.setattr(tp, "QApplication", FakeApp)
            with pytest.raises(RuntimeError):
                tp.ThumbnailProvider.instance()
            assert tp.ThumbnailProvider._instance is None
        finally:
            tp.ThumbnailProvider._instance = saved_instance

    def test_instance_allowed_without_app(self, monkeypatch) -> None:
        """正例：无 QApplication（纯逻辑环境）不设限。"""
        import src.shared.media.thumbnail_provider as tp

        real_cls = tp.ThumbnailProvider
        saved_instance = real_cls._instance
        real_cls._instance = None

        class StubProvider:
            def __init__(self) -> None:
                self.marker = "stub"

        monkeypatch.setattr(tp, "ThumbnailProvider", StubProvider)
        try:
            inst = real_cls.instance()
            assert isinstance(inst, StubProvider)
        finally:
            real_cls._instance = saved_instance

    def test_clip_cache_singleton_threadsafe(self) -> None:
        """正例+反例：回退（__new__ 无锁）时源码断言失败。"""
        import src.shared.media.thumbnail_provider as tp

        src = inspect.getsource(tp.ClipThumbnailCache.__new__)
        assert "_instance_lock" in src, "ClipThumbnailCache 单例未加锁（回退）"
        saved = tp.ClipThumbnailCache._instance
        tp.ClipThumbnailCache._instance = None
        try:
            results: List[object] = []
            barrier = threading.Barrier(4)

            def worker() -> None:
                barrier.wait()
                results.append(tp.ClipThumbnailCache())

            threads = [threading.Thread(target=worker) for _ in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(10.0)
            assert results and all(r is results[0] for r in results)
        finally:
            tp.ClipThumbnailCache._instance = saved


# =============================================================================
# AUD-35810：_read_slot 槽位刷新回写持 _prefetch_lock
# =============================================================================

class TestAUD35810:
    def test_slot_refresh_write_under_prefetch_lock(self) -> None:
        """正例+反例：回退（无锁回写 _by_frame）时失败。"""
        import src.shared.media.topos_image_sequence as tis

        src = inspect.getsource(tis.ToposImageSequenceSource._read_slot)
        assert "with self._prefetch_lock:" in src, "回写未持锁（回退）"
        assert "self._by_frame[new_slot.frame] = new_slot" in src, (
            "槽位刷新回写形态变化，请更新本断言"
        )


# =============================================================================
# AUD-35811：PlaneCache.put 字节记账与冻结统一 None 契约
# =============================================================================

class TestAUD35811:
    def test_put_with_none_plane(self) -> None:
        """正例+反例：回退（记账未防护 None → TypeError）时失败。"""
        cache = PlaneCache(max_bytes=1 << 20)
        a = np.zeros((4, 4), dtype=np.uint16)
        b = np.zeros((4, 4), dtype=np.uint16)
        cache.put(("k",), [a, None, b])
        got = cache.get(("k",))
        assert got is not None
        assert len(got) == 2
        assert not a.flags.writeable and not b.flags.writeable
        # 记账只计非 None 平面
        assert cache._bytes == a.nbytes + b.nbytes

    def test_put_over_budget_not_cached(self) -> None:
        cache = PlaneCache(max_bytes=8)
        a = np.zeros((64, 64), dtype=np.uint16)
        cache.put(("big",), [a])
        assert cache.get(("big",)) is None
        assert cache._bytes == 0


# =============================================================================
# AUD-35904：V9 读路径只读交付 + 平面复用
# =============================================================================

class TestAUD35904:
    def test_v9_planes_readonly_and_pooled(self) -> None:
        """正例+反例：回退（裸 np.zeros 可写交付 / 无复用）时失败。"""
        from src.shared.media.topos_source import ToposMediaSource

        decode_src = inspect.getsource(ToposMediaSource._v9_decode_gop)
        assert "flags.writeable = False" in decode_src, (
            "V9 平面交付前未冻结（read-only 契约回退）"
        )
        assert "_v9_spare_planes" in decode_src, "V9 平面复用槽缺失（回退）"
        assert "getrefcount" in decode_src, "复用缺少消费方释放判定"
        read_src = inspect.getsource(ToposMediaSource._v9_read_next)
        assert "_v9_spare_planes" in read_src, "消费后未归还复用槽（回退）"


# =============================================================================
# AUD-36006：TOPOS_VIDEO_ZERO_COPY 取值校验
# =============================================================================

class TestAUD36006:
    def _reload_with_env(self, value: str, monkeypatch):
        import src.shared.media.windows_hw.capability as cap

        monkeypatch.setenv("TOPOS_VIDEO_ZERO_COPY", value)
        return importlib.reload(cap)

    def test_invalid_value_rejected_with_warning(self, monkeypatch) -> None:
        """正例+反例：回退（未知值静默按任意值透传）时失败。"""
        import src.shared.media.windows_hw.capability as cap

        mod = self._reload_with_env("of", monkeypatch)  # 拼错值
        try:
            assert mod._zero_copy_env_var == "auto"
        finally:
            monkeypatch.setenv("TOPOS_VIDEO_ZERO_COPY", "auto")
            importlib.reload(cap)

    def test_known_values_accepted(self, monkeypatch) -> None:
        import src.shared.media.windows_hw.capability as cap

        try:
            mod = self._reload_with_env("off", monkeypatch)
            assert mod._zero_copy_env_var == "off"
            mod = self._reload_with_env("force", monkeypatch)
            assert mod._zero_copy_env_var == "force"
            mod = self._reload_with_env("auto", monkeypatch)
            assert mod._zero_copy_env_var == "auto"
        finally:
            monkeypatch.setenv("TOPOS_VIDEO_ZERO_COPY", "auto")
            importlib.reload(cap)

    def test_module_doc_declares_force_semantics(self) -> None:
        """文档与实现一致：force 在 Windows 按 auto 处理有明确说明。"""
        src = _read("windows_hw/capability.py")
        assert "按 ``auto`` 处理" in src or "按 auto 处理" in src


# =============================================================================
# AUD-36103：submit() 与 close() 的 TOCTOU 收尾
# =============================================================================

class _FakeLease:
    """最小 lease 替身：恰一次 release 语义与 TextureLease 一致。"""

    def __init__(self) -> None:
        self.released = False

    def release(self) -> None:
        if self.released:
            raise RuntimeError("lease 已释放")
        self.released = True

    def release_after(self, fence: object) -> None:
        self.release()


class TestAUD36103:
    def _make_frame(self) -> "object":
        from src.shared.media.windows_hw.d3d11_device import DXGI_FORMAT_NV12
        from src.shared.media.windows_hw.d3d11_frame import D3D11GpuFrame

        return D3D11GpuFrame(
            texture_ptr=0xC0FFEE01,
            dxgi_format=DXGI_FORMAT_NV12,
            width=1920,
            height=1080,
            lease=_FakeLease(),
            adapter_luid="LUID-TEST",
            frame_index=0,
        )

    def test_submit_after_executor_shutdown_returns_failed_future(self) -> None:
        """正例+反例：回退（RuntimeError 直接抛给调用方）时失败。"""
        from concurrent.futures import ThreadPoolExecutor

        from src.shared.media.windows_hw.d3d11_pipeline import (
            D3D11PipelineScheduler,
        )

        def render(frame):  # pragma: no cover - 不会被调用
            raise AssertionError

        def encode(target):  # pragma: no cover - 不会被调用
            raise AssertionError

        scheduler = D3D11PipelineScheduler(render, encode, surface_depth=4)
        dead_executor = ThreadPoolExecutor(max_workers=1)
        dead_executor.shutdown(wait=True)
        scheduler._render_executor = dead_executor  # 模拟 close() 已发生

        frame = self._make_frame()
        outcome = scheduler.submit(frame)  # 不得抛 RuntimeError
        assert outcome.done()
        assert outcome.result() is False
        assert frame.lease.released, "frame lease 未归还（槽位/lease 泄漏）"
        # 信号量槽位已归还：无需等待即可再次获取
        assert scheduler._slots.acquire(blocking=False)
        scheduler._slots.release()
        telemetry = scheduler.telemetry()
        assert telemetry.failed >= 1

    def test_submit_raises_when_explicitly_closed(self) -> None:
        """正例：入口 closed 检查仍生效（显式 close 后拒绝提交）。"""
        from src.shared.media.windows_hw.d3d11_pipeline import (
            D3D11PipelineScheduler,
        )

        scheduler = D3D11PipelineScheduler(
            lambda f: None, lambda t: None, surface_depth=4)
        scheduler.close()
        with pytest.raises(RuntimeError):
            scheduler.submit(self._make_frame())


# =============================================================================
# AUD-36111：_return_texture 锁外调用 releaser
# =============================================================================

class TestAUD36111:
    def _no_releaser_call_inside_condition_lock(self) -> None:
        """AST 检查：_return_texture 的 with self._condition 块内不得调用
        self._releaser。回退（持锁释放）时本断言失败。"""
        import textwrap

        from src.shared.media.windows_hw.d3d11_texture_pool import (
            D3D11TexturePool,
        )

        tree = ast.parse(textwrap.dedent(
            inspect.getsource(D3D11TexturePool._return_texture)))
        for node in ast.walk(tree):
            if isinstance(node, ast.With):
                # 只检查以 self._condition 为上下文管理器的 with 块
                ctx_ok = any(
                    (isinstance(item.context_expr, ast.Attribute)
                     and item.context_expr.attr == "_condition")
                    for item in node.items
                )
                if not ctx_ok:
                    continue
                for sub in ast.walk(node):
                    if (isinstance(sub, ast.Call)
                            and isinstance(sub.func, ast.Attribute)
                            and sub.func.attr == "_releaser"):
                        raise AssertionError(
                            "持锁调用 _releaser（AUD-36111 回退）"
                        )

    def test_releaser_called_outside_lock(self) -> None:
        self._no_releaser_call_inside_condition_lock()

    def test_closed_pool_return_releases_texture(self) -> None:
        """行为正例：关闭后归还在途纹理仍会释放且计数一致。"""
        from src.shared.media.windows_hw.d3d11_device import DXGI_FORMAT_NV12
        from src.shared.media.windows_hw.d3d11_texture_pool import (
            D3D11TexturePool,
        )

        released: List[int] = []
        pool = D3D11TexturePool(
            depth=2, width=1920, height=1080,
            dxgi_format=DXGI_FORMAT_NV12,
            texture_factory=lambda: 0xC0FFEE00 + len(released) + 1,
            texture_releaser=released.append,
        )
        lease = pool.try_acquire()
        assert lease is not None
        # 绕过 close() 的在途拒绝（模拟"先关闭、后归还"竞态窗口）
        pool._state.closed = True
        pool._state.free.clear()
        lease.release()
        assert len(released) == 1
        assert pool._state.in_use == 0


# =============================================================================
# AUD-36115：诊断状态锁纪律 + frame_index 校验
# =============================================================================

class TestAUD36115:
    def _make_bridge(self):
        from src.shared.media.windows_hw.d3d11_gl_bridge import D3D11GLBridge

        return D3D11GLBridge(ring_size=2, interop_factory=None)

    def test_negative_frame_index_rejected(self) -> None:
        """正例+反例：回退（无校验，静默进入环）时失败。"""
        from src.shared.media.windows_hw.d3d11_gl_bridge import D3D11GLBridge

        bridge = D3D11GLBridge(
            ring_size=2,
            interop_factory=lambda tex, idx, pf, bd: (object(), object()),
        )
        assert bridge.initialize() is True
        assert bridge.bind_texture(0xBEEF, -3) is None
        assert bridge.fail_count == 1
        reason = bridge.last_fallback_reason or ""
        assert "negative_frame_index" in reason

    def test_reason_write_under_lock(self) -> None:
        """正例+反例：回退（_last_fallback_reason 锁外写）时 AST 失败。"""
        import textwrap

        from src.shared.media.windows_hw.d3d11_gl_bridge import D3D11GLBridge

        tree = ast.parse(textwrap.dedent(
            inspect.getsource(D3D11GLBridge.bind_texture)))
        func = next(
            n for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef) and n.name == "_count_failure"
        )
        # 所有 _last_fallback_reason 赋值必须位于 with self._lock 块内
        with_blocks = [n for n in ast.walk(func) if isinstance(n, ast.With)]
        for sub in ast.walk(func):
            if (isinstance(sub, ast.Assign)
                    and any(
                        isinstance(t, ast.Attribute)
                        and t.attr == "_last_fallback_reason"
                        for t in sub.targets
                    )):
                inside = any(sub in ast.walk(w) for w in with_blocks)
                assert inside, (
                    "_last_fallback_reason 在锁外写（AUD-36115 回退）"
                )

    def test_properties_read_under_lock(self) -> None:
        """正例+反例：回退（诊断属性无锁读）时失败。"""
        from src.shared.media.windows_hw.d3d11_gl_bridge import D3D11GLBridge

        for prop in ("is_initialized", "last_fallback_reason", "fail_count"):
            src = inspect.getsource(
                getattr(D3D11GLBridge, prop).fget)
            assert "with self._lock:" in src, f"{prop} 读未持锁（回退）"

    def test_diagnostic_state_reads(self) -> None:
        bridge = self._make_bridge()
        assert bridge.is_initialized is False
        assert bridge.fail_count == 0
        assert bridge.last_fallback_reason is None
