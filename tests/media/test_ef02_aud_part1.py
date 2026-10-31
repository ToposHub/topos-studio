"""批 EF-02 子运行 1/2 —— src/shared/media Suggestion 级修复回归测试。

覆盖 AUD-34304/34313/34314/34318/34422/34502/34505/34510/34514/
34603/34612/34709/34711/34807/34813/34814/34909/35001/35010/35011/
35103/35204/35209（23 条）。

约定：
- 不真实解码大媒体文件（合成小样本 / mock）；
- 不触发可能弹模态对话框的路径（不创建 QApplication）；
- 修复回退时对应测试必须失败。
"""

from __future__ import annotations

import inspect
import os
import re
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from src.shared.media.adapter import VideoDecoderAdapter
from src.shared.media.cache import CachePolicy, FrameCache
from src.shared.media.color_metadata import (
    normalize_color_primaries,
    normalize_color_space,
    normalize_color_transfer,
)
from src.shared.media.decode_queue import DecodedFrameEntry, DecodeQueue
from src.shared.media.frame import DecodedFrame
from src.shared.media.frame_cache import FrameCache as LruFrameCache
from src.shared.media.gpu_frame import GpuDecodedFrame, GpuFramePlane, GpuFrameRelease
from src.shared.media.hwaccel import HardwareCapability, HardwareDecodeConfig, HardwareDecoder
from src.shared.media.proxy import DiskProxyInfo, ProxyStatus

# ============================================================================
# AUD-34304: _detect_actual_bit_depth 位深来自 pixel_format/plane_infos/
# metadata，max() 仅作兜底
# ============================================================================


def _bare_adapter() -> VideoDecoderAdapter:
    """构造不带解码器的适配器（仅调用纯函数；release 路径属性齐备）。"""
    adapter = VideoDecoderAdapter.__new__(VideoDecoderAdapter)
    adapter._source = None
    adapter._metadata_cache = None
    adapter._frame_buffer = None
    return adapter


class TestAud34304BitDepth:
    def test_registry_bit_depth_wins_over_max_scan(self):
        """10-bit 格式 + 高亮 uint16 数据：注册表位深（10）优先于 max() 扫描。

        回退（旧行为只做 max 扫描）时：max=60000 > 4095 → 误判 16，本测试失败。
        """
        adapter = _bare_adapter()
        frame = DecodedFrame(
            frame_index=0,
            pts=0,
            time_seconds=0.0,
            data=np.full((4, 4, 3), 60000, dtype=np.uint16),
            pixel_format='yuv420p10le',  # 注册表 bit_depth=10
        )
        assert adapter._detect_actual_bit_depth(frame) == 10

    def test_plane_infos_bit_depth_used(self):
        """无注册表格式时 plane_infos[0].bit_depth 生效。"""
        from src.shared.media.frame import PlaneInfo

        adapter = _bare_adapter()
        frame = DecodedFrame(
            frame_index=0,
            pts=0,
            time_seconds=0.0,
            planes=(np.full((4, 4), 60000, dtype=np.uint16),),
            pixel_format='made_up_custom_12bit',  # 不在注册表
            plane_infos=(PlaneInfo(
                index=0, width=4, height=4, stride=8, component='Y',
                dtype=np.dtype(np.uint16), bit_depth=12,
            ),),
        )
        assert adapter._detect_actual_bit_depth(frame) == 12

    def test_metadata_bit_depth_used(self):
        """无注册表/plane_infos 时 metadata.bit_depth 生效。"""
        adapter = _bare_adapter()
        metadata = MagicMock()
        metadata.bit_depth = 10
        frame = DecodedFrame(
            frame_index=0,
            pts=0,
            time_seconds=0.0,
            data=np.full((4, 4, 3), 60000, dtype=np.uint16),
            pixel_format='made_up_custom_fmt',
            metadata=metadata,
        )
        assert adapter._detect_actual_bit_depth(frame) == 10

    def test_max_scan_only_as_last_fallback(self):
        """三级来源全缺时回退 max() 扫描：暗帧 → 10、亮帧 → 16。"""
        adapter = _bare_adapter()
        dark = DecodedFrame(
            frame_index=0, pts=0, time_seconds=0.0,
            data=np.full((4, 4, 3), 500, dtype=np.uint16),
            pixel_format='made_up_custom_fmt',
        )
        bright = DecodedFrame(
            frame_index=0, pts=0, time_seconds=0.0,
            data=np.full((4, 4, 3), 60000, dtype=np.uint16),
            pixel_format='made_up_custom_fmt',
        )
        assert adapter._detect_actual_bit_depth(dark) == 10
        assert adapter._detect_actual_bit_depth(bright) == 16


# ============================================================================
# AUD-34313: manifest 未配置时 ensure_proxy_async 无条件返回占位 entry
# ============================================================================


class TestAud34313UnconfiguredManifest:
    def test_existing_source_returns_placeholder_not_assert(self, tmp_path):
        """未 configure + 源文件存在：返回 MISSING 占位而非 assert 崩溃。

        回退（旧代码落入 assert）时本测试以 AssertionError 失败。
        """
        from src.shared.media.asset_proxy_manager import AssetProxyManager

        source = tmp_path / "source.mp4"
        source.write_bytes(b"\x00" * 16)

        manager = AssetProxyManager()
        entry = manager.ensure_proxy_async(
            source_path=str(source), asset_id="a1")
        assert entry.status == ProxyStatus.MISSING
        assert "not configured" in (entry.error or "")

    def test_missing_source_returns_placeholder(self, tmp_path):
        """未 configure + 源文件不存在：返回 MISSING 占位（key=None 路径）。"""
        from src.shared.media.asset_proxy_manager import AssetProxyManager

        manager = AssetProxyManager()
        entry = manager.ensure_proxy_async(
            source_path=str(tmp_path / "nope.mp4"), asset_id="a1")
        assert entry.status == ProxyStatus.MISSING


# ============================================================================
# AUD-34314: emit 快照拷贝 + _mark_failed 不原地改 entry
# ============================================================================


class TestAud34314EntrySnapshot:
    def _make_manager(self, tmp_path):
        from src.shared.media.asset_proxy_manager import AssetProxyManager

        manager = AssetProxyManager()
        manager.configure(cache_dir=tmp_path / "cache")
        return manager

    def _inject_entry(self, manager, source: str):
        from src.shared.media.proxy_manifest import ProxyManifestEntry, ProxyManifestKey

        entry = ProxyManifestEntry(
            key=ProxyManifestKey(source, 1, 2, "prores_ks", "half", "1"),
            status=ProxyStatus.GENERATING,
            proxy_path=None, width=0, height=0, codec="prores_ks",
            pixel_format="", created_at=time.time(), updated_at=time.time(),
        )
        manager._manifest.upsert_entry(entry)
        return entry

    def test_mark_failed_replaces_entry_not_in_place(self, tmp_path):
        """_mark_failed 后：旧引用保持 GENERATING，manifest 中为新 FAILED 对象。

        回退（原地改 entry）时旧引用也变 FAILED，第一个断言失败。
        """
        manager = self._make_manager(tmp_path)
        source = str(tmp_path / "clip.mov")
        entry = self._inject_entry(manager, source)

        received: list[object] = []
        manager.proxy_state_changed.connect(lambda sp, e: received.append(e))

        manager._mark_failed(source, "boom")

        # 旧引用未被原地改写（AUD-34314 核心）
        assert entry.status == ProxyStatus.GENERATING
        # manifest 中是替换后的新对象
        stored = manager._manifest.get_entry(source)
        assert stored is not None and stored.status == ProxyStatus.FAILED
        assert stored.error == "boom"
        assert stored is not entry
        # emit 的是快照（不是 manifest 存储对象本身）
        assert len(received) == 1
        assert received[0] is not stored
        assert received[0].status == ProxyStatus.FAILED

    def test_sync_emit_passes_snapshot(self, tmp_path):
        """_sync_from_disk_proxy_info emit 的是拷贝而非 manifest 内对象。"""
        manager = self._make_manager(tmp_path)
        source_file = tmp_path / "clip2.mov"
        source_file.write_bytes(b"\x00" * 16)
        source = str(source_file)
        received: list[object] = []
        manager.proxy_state_changed.connect(lambda sp, e: received.append(e))

        info = DiskProxyInfo(status=ProxyStatus.READY, proxy_path="/tmp/p.mov")
        manager._sync_from_disk_proxy_info(source, info)

        assert len(received) == 1
        stored = manager._manifest.get_entry(source)
        assert stored is not None
        assert received[0] is not stored


# ============================================================================
# AUD-34318: get_missing_frames 区间校验 + 锁外扫描；prefetch_hint 无负帧号
# ============================================================================


class TestAud34318CacheRange:
    def _cache(self, policy: CachePolicy | None = None) -> FrameCache:
        cache = FrameCache(policy=policy or CachePolicy(max_frames=16, max_memory_mb=8))
        return cache

    @staticmethod
    def _frame() -> np.ndarray:
        return np.zeros((4, 4, 3), dtype=np.uint8)

    def test_prefetch_hint_backward_never_negative(self):
        cache = self._cache(CachePolicy(max_frames=16, max_memory_mb=8, prefetch_count=8))
        hints = cache.prefetch_hint(2, direction='backward')
        assert hints, "应有预读提示"
        assert all(f >= 0 for f in hints)

    def test_prefetch_hint_bidirectional_never_negative(self):
        cache = self._cache(CachePolicy(max_frames=16, max_memory_mb=8, prefetch_count=4))
        hints = cache.prefetch_hint(0, direction='bidirectional')
        assert all(f >= 0 for f in hints)

    def test_get_missing_frames_invalid_range_warns(self, caplog):
        cache = self._cache()
        with caplog.at_level("WARNING"):
            missing = cache.get_missing_frames(10, 5)
        assert missing == []
        assert any("非法区间" in r.message for r in caplog.records)

    def test_get_missing_frames_respects_cached(self):
        cache = self._cache()
        cache.put(3, self._frame())
        missing = cache.get_missing_frames(2, 5)
        assert 3 not in missing
        assert set(missing) == {2, 4, 5}

    def test_get_missing_frames_negative_start_clamped(self):
        cache = self._cache()
        missing = cache.get_missing_frames(-5, 2)
        assert missing == [0, 1, 2]


# ============================================================================
# AUD-34422: normalize_color_* 未知字符串按契约返回 'unknown'
# ============================================================================


class TestAud34422AliasContract:
    def test_unknown_transfer_returns_unknown(self):
        # 回退（透传原值）时返回 'foobar'，断言失败
        assert normalize_color_transfer('foobar') == 'unknown'

    def test_unknown_primaries_returns_unknown(self):
        assert normalize_color_primaries('not_a_primary') == 'unknown'

    def test_unknown_space_returns_unknown(self):
        assert normalize_color_space('zzz_garbage') == 'unknown'

    def test_valid_canonical_names_still_pass_through(self):
        """回归守卫：映射表内的合法标准名不被误判为 unknown。"""
        assert normalize_color_transfer('smpte2084') == 'smpte2084'
        assert normalize_color_transfer('smpte428') == 'smpte428'
        assert normalize_color_transfer('pq') == 'smpte2084'
        assert normalize_color_primaries('bt709') == 'bt709'
        assert normalize_color_space('bt709') == 'bt709'
        assert normalize_color_space('bt2020nc') == 'bt2020nc'
        assert normalize_color_transfer(None) is None


# ============================================================================
# AUD-34502: 期望帧已在队列时原地摘除，不打乱其余条目次序
# ============================================================================


class TestAud34502InPlaceScan:
    @staticmethod
    def _entry(index: int) -> DecodedFrameEntry:
        frame = DecodedFrame(
            frame_index=index, pts=index, time_seconds=float(index),
            data=np.zeros((2, 2, 3), dtype=np.uint8),
        )
        return DecodedFrameEntry(frame=frame, frame_index=index, is_master=True)

    def _queue(self) -> DecodeQueue:
        return DecodeQueue(MagicMock(), window_size=2, max_queue_size=8)

    def test_scan_extracts_target_preserving_order(self):
        """队列 [1,2] 取帧 2：原地摘除 → 返回帧 2 且帧 1 仍队首。

        回退（旧行为：弹出 1 放回队尾返回 None）时两个断言均失败。
        """
        q = self._queue()
        q._decoded_queue.put(self._entry(1))
        q._decoded_queue.put(self._entry(2))

        entry = q.get_frame_entry(frame_index=2, timeout=0.1)
        assert entry is not None and entry.frame_index == 2

        nxt = q.get_frame_entry(timeout=0.1)
        assert nxt is not None and nxt.frame_index == 1

    def test_mismatch_still_requeues_and_times_out(self):
        """无目标帧时：弹出条目放回并在超时后返回 None，条目不丢。"""
        q = self._queue()
        q._decoded_queue.put(self._entry(7))

        assert q.get_frame_entry(frame_index=99, timeout=0.05) is None
        kept = q.get_frame_entry(timeout=0.1)
        assert kept is not None and kept.frame_index == 7

    def test_scan_drops_stale_epoch_entries(self):
        """原地扫描顺手丢弃过期代条目（AUD-34500 语义保持）。"""
        q = self._queue()
        stale = self._entry(5)
        stale.decode_epoch = q._decode_epoch + 1  # 过期代
        q._decoded_queue.put(stale)

        found = q._scan_decoded_queue_for(5)
        assert found is None
        assert q._decoded_queue.qsize() == 0
        assert q.get_stats()['frames_dropped'] == 1


# ============================================================================
# AUD-34505: 预读线程 _stats 写入在 _request_lock 内
# ============================================================================


class TestAud34505StatsLock:
    def test_prefetch_loop_stats_writes_are_locked(self):
        """源码哨兵：_prefetch_loop 内的计数器自增必须位于
        `with self._request_lock` 块中。

        回退（移除锁）时哨兵失配，测试失败。
        """
        src = inspect.getsource(DecodeQueue._prefetch_loop)
        lines = src.split("\n")
        for counter in ("frames_decoded", "frames_dropped"):
            locked = False
            found = False
            for i, line in enumerate(lines):
                if f"_stats['{counter}']" in line:
                    found = True
                    # 向上找最近 3 行内是否有加锁语句
                    window = "\n".join(lines[max(0, i - 3):i + 1])
                    assert "with self._request_lock" in window, (
                        f"_prefetch_loop 中 {counter} 写入未置于 _request_lock 内"
                    )
                    locked = True
                    break
            assert found and locked

    def test_take_entry_stats_writes_are_locked(self):
        """get_frame 公共路径的 frames_dropped 写入同样持锁。"""
        for func in (DecodeQueue._take_entry, DecodeQueue._scan_decoded_queue_for):
            src = inspect.getsource(func)
            if "_stats['frames_dropped']" in src:
                assert "with self._request_lock" in src


# ============================================================================
# AUD-34510: generate_proxy 锁内复验，TOCTOU 落败方不入队
# ============================================================================


class TestAud34510GenerateProxyRace:
    @staticmethod
    def _service():
        from src.shared.media.disk_proxy_service import DiskProxyService
        from src.shared.media.proxy import DEFAULT_DISK_PROXY_CONFIG

        service = DiskProxyService(config=DEFAULT_DISK_PROXY_CONFIG)
        service._manager = MagicMock()
        service._manager.queue_proxy_generation.return_value = "task-1"
        return service

    def test_raced_generating_is_not_overwritten(self):
        """检查后被并发置 GENERATING：不覆盖状态、不重复入队、挂接回调。

        回退（旧代码直接覆盖置位并入队）时 queue_proxy_generation 会被
        调用、回调不进注册表，断言失败。
        """
        service = self._service()
        path = "/tmp/whatever.mov"
        # 检查路径返回 MISSING（模拟检查时刻的旧状态）
        info = DiskProxyInfo(status=ProxyStatus.MISSING)
        with patch.object(service, "get_disk_proxy_info", return_value=info):
            # 但锁内缓存已被并发线程置 GENERATING（竞态对手）
            service._info_cache[path] = DiskProxyInfo(
                status=ProxyStatus.GENERATING)

            on_complete = MagicMock()
            on_progress = MagicMock()
            task_id = service.generate_proxy(
                path, "asset", on_complete=on_complete, on_progress=on_progress)

        assert task_id is None  # 未入队
        service._manager.queue_proxy_generation.assert_not_called()
        # 回调挂到注册表，等待在途生成的终态消费
        assert path in service._pending_caller_callbacks
        assert path in service._pending_progress_callbacks
        # 缓存状态保持 GENERATING，未被覆盖为新的占位对象
        assert service._info_cache[path].status == ProxyStatus.GENERATING

    def test_no_race_queues_task_and_sets_generating(self):
        """无竞态正例：MISSING → 置 GENERATING → 入队返回 task_id。"""
        service = self._service()
        path = "/tmp/fresh.mov"
        info = DiskProxyInfo(status=ProxyStatus.MISSING)
        with patch.object(service, "get_disk_proxy_info", return_value=info):
            task_id = service.generate_proxy(path, "asset")
        assert task_id == "task-1"
        service._manager.queue_proxy_generation.assert_called_once()
        assert service._info_cache[path].status == ProxyStatus.GENERATING


# ============================================================================
# AUD-34514: get_proxy_stats 锁内读取；磁盘发现不再累计 bytes_saved
# ============================================================================


class TestAud34514StatsAccounting:
    @staticmethod
    def _service(tmp_path):
        from src.shared.media.disk_proxy_service import DiskProxyService
        from src.shared.media.proxy import DEFAULT_DISK_PROXY_CONFIG

        service = DiskProxyService(config=DEFAULT_DISK_PROXY_CONFIG)
        service._manager = MagicMock()
        service._manager.get_stats.return_value = MagicMock(
            assets_with_proxy=0, pending_tasks=0, generating_tasks=0,
            failed_tasks=0, total_proxy_size_bytes=0,
        )
        return service

    def test_discovery_does_not_count_bytes_saved(self, tmp_path):
        """磁盘发现已有代理不再累计 _total_bytes_saved（口径只含实际生成）。

        回退（发现路径 += proxy_size）时计数非 0，断言失败。
        """
        service = self._service(tmp_path)
        source = tmp_path / "src.mov"
        source.write_bytes(b"\x00" * 64)
        proxy = tmp_path / "proxy.mov"
        proxy.write_bytes(b"\x00" * 1024)

        service._manager.get_valid_proxy_path.return_value = str(proxy)
        with patch.object(service, "_discover_proxy_resolution",
                          return_value=None):
            info = service.get_disk_proxy_info(str(source))

        assert info.is_ready
        stats = service.get_proxy_stats()
        assert stats['service_stats']['total_bytes_saved'] == 0
        assert stats['service_stats']['total_generated'] == 0

    def test_get_proxy_stats_reflects_locked_counters(self, tmp_path):
        """完成路径计数（锁内写）能被 get_proxy_stats（锁内读）一致读到。"""
        service = self._service(tmp_path)
        service._total_generated = 3
        service._total_failed = 1
        service._total_bytes_saved = 4096
        stats = service.get_proxy_stats()
        assert stats['service_stats'] == {
            'total_generated': 3,
            'total_failed': 1,
            'total_bytes_saved': 4096,
        }

    def test_stats_reads_happen_inside_lock(self):
        """源码哨兵：结果 return 必须位于 `with self._lock` 块内（缩进>=8）。

        回退（锁外读取计数）时 return 缩进回到 4 空格，断言失败。
        """
        from src.shared.media.disk_proxy_service import DiskProxyService

        src = inspect.getsource(DiskProxyService.get_proxy_stats)
        m = re.search(r"^(\s+)return \{", src, re.M)
        assert m, "未找到结果 return"
        assert len(m.group(1)) >= 8, (
            "get_proxy_stats 的计数读取应在 with self._lock 临界区内"
        )


# ============================================================================
# AUD-34603: _select_backend 未识别名称抛 ValueError
# ============================================================================


class TestAud34603BackendValidation:
    def test_invalid_backend_raises(self):
        from src.shared.media.factory import MediaSourceFactory

        with pytest.raises(ValueError, match="无效的后端名称"):
            MediaSourceFactory._select_backend("/tmp/x.mp4", "ffmgeg")

    def test_valid_backends_do_not_raise(self):
        from src.shared.media.factory import MediaSourceFactory

        assert MediaSourceFactory._select_backend("/tmp/x.mp4", "ffmpeg") == "ffmpeg"
        assert MediaSourceFactory._select_backend("/tmp/x.mp4", "topos") == "topos"
        # gstreamer 已移除：回退 auto 并告警（不抛）
        assert MediaSourceFactory._select_backend("/tmp/x.mp4", "gstreamer") == "auto"

    def test_auto_returns_auto_without_preference(self):
        from src.shared.media.factory import MediaSourceFactory

        old = MediaSourceFactory._USER_PREFERRED_BACKEND
        try:
            MediaSourceFactory._USER_PREFERRED_BACKEND = None
            assert MediaSourceFactory._select_backend("/tmp/x.mp4", "auto") == "auto"
        finally:
            MediaSourceFactory._USER_PREFERRED_BACKEND = old


# ============================================================================
# AUD-34612: 后端注册表延迟注册加锁
# ============================================================================


class TestAud34612RegistryLock:
    def test_registry_lock_exists_and_used(self):
        """源码哨兵：注册/填充路径持有 _REGISTRY_LOCK。"""
        from src.shared.media.factory import MediaSourceFactory

        assert isinstance(MediaSourceFactory._REGISTRY_LOCK, type(threading.RLock()))
        src = inspect.getsource(MediaSourceFactory._ensure_backends_registered)
        assert "with cls._REGISTRY_LOCK" in src
        assert "_REGISTRY_INITIALIZED" in src
        reg_src = inspect.getsource(MediaSourceFactory.register_backend)
        assert "with cls._REGISTRY_LOCK" in reg_src

    def test_concurrent_first_registration_single_flight(self):
        """并发首查只执行一轮注册，注册表完整。"""
        from src.shared.media.factory import MediaSourceFactory as F

        saved = (dict(F._BACKEND_REGISTRY), dict(F._BACKEND_AVAILABLE),
                 F._REGISTRY_INITIALIZED)
        try:
            F._BACKEND_REGISTRY.clear()
            F._BACKEND_AVAILABLE.clear()
            F._REGISTRY_INITIALIZED = False
            errors: list[Exception] = []

            def _call() -> None:
                try:
                    F._ensure_backends_registered()
                except Exception as e:  # noqa: BLE001
                    errors.append(e)

            threads = [threading.Thread(target=_call) for _ in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=30)
            assert not errors
            assert F._REGISTRY_INITIALIZED is True
            for name in ("ffmpeg", "topos", "toos_image", "toos_sequence"):
                assert name in F._BACKEND_REGISTRY
        finally:
            F._BACKEND_REGISTRY.clear()
            F._BACKEND_REGISTRY.update(saved[0])
            F._BACKEND_AVAILABLE.clear()
            F._BACKEND_AVAILABLE.update(saved[1])
            F._REGISTRY_INITIALIZED = saved[2]


# ============================================================================
# AUD-34709: GOP 后台线程 close join + 关闭后不写回
# ============================================================================


class TestAud34709GopThreadLifecycle:
    @staticmethod
    def _source_with_thread() -> object:
        from src.shared.media.ffmpeg_source import FFmpegMediaSource

        src = FFmpegMediaSource.__new__(FFmpegMediaSource)
        src._frame_iterator = None
        src._decoder_buffer = []
        src._pending_av_frame = None
        src._gop_seek_iterator = None
        src._main_resync_pts = None
        src._container = None
        src._video_stream = None
        src._is_open = True
        src._current_frame = 0
        src._path = "/tmp/fake.mov"
        src._gop_index_thread = None
        return src

    def test_close_joins_background_thread(self):
        """close() 等待 GOP 索引线程退出并清引用。

        回退（close 不 join）时 _gop_index_thread 仍存活/非 None，断言失败。
        """
        src = self._source_with_thread()
        release = threading.Event()
        started = threading.Event()

        def _worker() -> None:
            started.set()
            release.wait(timeout=5)

        src._gop_index_thread = threading.Thread(
            target=_worker, name="gop-test", daemon=True)
        src._gop_index_thread.start()
        assert started.wait(timeout=5)

        closer = threading.Thread(target=src.close)
        closer.start()
        # close 应阻塞在 join 上（线程未放行）
        closer.join(timeout=0.3)
        assert closer.is_alive(), "close() 未等待后台线程"
        release.set()
        closer.join(timeout=5)
        assert not closer.is_alive()
        assert getattr(src, "_gop_index_thread", None) is None

    def test_build_write_back_guard_present(self):
        """源码哨兵：后台 _build 写回前检查 _is_open。"""
        from src.shared.media.ffmpeg_source import FFmpegMediaSource

        src = inspect.getsource(FFmpegMediaSource._ensure_gop_index)
        # _build 闭包内：写回语句出现在 _is_open 检查之后
        build_idx = src.find("def _build")
        assert build_idx != -1
        build_src = src[build_idx:]
        write_idx = build_src.find("self._gop_index = built")
        guard_idx = build_src.find("self._is_open")
        assert write_idx != -1 and guard_idx != -1
        assert guard_idx < write_idx


# ============================================================================
# AUD-34711: warm_gop_index 失败返回 None（异常不上抛）
# ============================================================================


class TestAud34711WarmGopIndex:
    def test_exception_returns_none(self):
        """get_or_build_gop_index 抛异常 → 返回 None（回退时异常上抛）。"""
        import src.shared.media.ffmpeg_source as fs_mod

        src = fs_mod.FFmpegMediaSource.__new__(fs_mod.FFmpegMediaSource)
        src._path = "/tmp/fake.mov"
        src._gop_index = None
        src._gop_index_attempted = False
        src._gop_index_thread = None
        src._is_open = False
        src._container = None
        src._video_stream = None
        src._frame_iterator = None
        src._decoder_buffer = []
        src._pending_av_frame = None
        src._gop_seek_iterator = None
        src._main_resync_pts = None
        src._current_frame = 0

        def _boom(path, force=False):
            raise OSError("io error")

        with patch.object(fs_mod, "get_or_build_gop_index", _boom):
            assert src.warm_gop_index() is None
        assert src._gop_index is None

    def test_success_stores_index(self):
        import src.shared.media.ffmpeg_source as fs_mod

        src = fs_mod.FFmpegMediaSource.__new__(fs_mod.FFmpegMediaSource)
        src._path = "/tmp/fake.mov"
        src._gop_index = None
        src._gop_index_attempted = False
        src._gop_index_thread = None
        src._is_open = False
        src._container = None
        src._video_stream = None
        src._frame_iterator = None
        src._decoder_buffer = []
        src._pending_av_frame = None
        src._gop_seek_iterator = None
        src._main_resync_pts = None
        src._current_frame = 0
        sentinel = object()

        with patch.object(fs_mod, "get_or_build_gop_index",
                          lambda path, force=False: sentinel):
            assert src.warm_gop_index() is sentinel
        assert src._gop_index is sentinel
        assert src._gop_index_attempted is True


# ============================================================================
# AUD-34807: get_oldest_entries 语义统一；_evict_one 免全表扫描
# ============================================================================


class TestAud34807LruSemantics:
    @staticmethod
    def _frame(idx: int) -> DecodedFrame:
        return DecodedFrame(
            frame_index=idx, pts=idx, time_seconds=float(idx),
            data=np.zeros((8, 8, 3), dtype=np.uint8),
        )

    def _cache(self) -> LruFrameCache:
        return LruFrameCache(max_entries=16, max_memory_mb=4, min_idle_time_for_eviction=0.0)

    def test_get_oldest_entries_is_lru_order(self):
        """get 刷新 LRU 后，队首应是最久未访问条目。

        回退（若实现/文档回到"插入顺序"口径）时本断言失败。
        """
        cache = self._cache()
        for i in (1, 2, 3):
            assert cache.put(self._frame(i))
        assert cache.get(1) is not None  # 触摸帧 1 → 移到队尾
        oldest = cache.get_oldest_entries(3)
        assert [e.frame_index for e in oldest] == [2, 3, 1]

    def test_evict_one_picks_least_recently_used(self):
        cache = self._cache()
        for i in (1, 2, 3):
            assert cache.put(self._frame(i))
        assert cache.get(1) is not None
        evicted = cache._evict_one(force=True)
        assert evicted is not None and evicted.frame_index == 2

    def test_evict_one_source_has_no_full_scan_for_min(self):
        """源码哨兵：_evict_one 不再做最小 last_used_time 全表查找。"""
        src = inspect.getsource(LruFrameCache._evict_one)
        assert "oldest_time" not in src
        assert "break" in src


# ============================================================================
# AUD-34813: GpuDecodedFrame planes 构造期归一为 tuple
# ============================================================================


class TestAud34813PlanesTuple:
    @staticmethod
    def _plane() -> GpuFramePlane:
        from src.shared.media.gpu_frame import GpuPlaneResource

        return GpuFramePlane(
            component="Y", api="metal", texture_id=0, target="2d",
            width=4, height=4,
            resource=GpuPlaneResource(
                api="metal", native_handle=object(),
                resource_format="r8unorm", width=4, height=4,
            ),
        )

    def test_planes_list_normalized_to_tuple(self):
        """传入 list → 存储为 tuple（发布后不可原地修改）。

        回退（移除归一化）时 planes 保持 list，断言失败。
        """
        frame = GpuDecodedFrame(
            frame_index=0, pts=0, time_seconds=0.0, width=4, height=4,
            visible_rect=None, pixel_format="nv12", bit_depth=8,
            planes=[self._plane()],
        )
        assert isinstance(frame.planes, tuple)

    def test_design_principle_doc_updated(self):
        """模块设计原则不再声称 frozen=True。"""
        import src.shared.media.gpu_frame as mod

        doc = mod.__doc__ or ""
        assert "frozen=True 保证线程安全" not in doc
        assert "AUD-34813" in doc


# ============================================================================
# AUD-34814: destructor_policy="enqueue" 构造期拒绝
# ============================================================================


class TestAud34814EnqueueRejected:
    def test_enqueue_policy_raises(self):
        """未实现的策略值在构造期抛 ValueError（回退时静默接受）。"""
        with pytest.raises(ValueError, match="enqueue"):
            GpuFrameRelease(destructor_policy="enqueue")

    def test_valid_policies_accepted(self):
        GpuFrameRelease(destructor_policy="warn_only")
        GpuFrameRelease(destructor_policy="thread_safe")

    def test_docstring_no_longer_promises_enqueue(self):
        release_src = inspect.getsource(GpuFrameRelease)
        assert '"warn_only" | "thread_safe"' in release_src


# ============================================================================
# AUD-34909: 探测缓存加锁双检；HardwareDecodeConfig 探测惰性化
# ============================================================================


class TestAud34909DetectLockAndLaziness:
    def test_concurrent_first_detect_single_flight(self):
        """并发首查只探测一次（回退为无锁 check-then-set 时可能多次）。"""
        saved = HardwareCapability._detected_decoders
        call_count = {"n": 0}
        lock = threading.Lock()

        def _slow_probe() -> list:
            with lock:
                call_count["n"] += 1
            time.sleep(0.05)
            return []

        try:
            HardwareCapability._detected_decoders = None
            with patch.object(HardwareCapability,
                              "_detect_available_decoders_uncached",
                              staticmethod(_slow_probe)):
                results: list = []
                errors: list[Exception] = []

                def _call() -> None:
                    try:
                        results.append(
                            HardwareCapability.detect_available_decoders())
                    except Exception as e:  # noqa: BLE001
                        errors.append(e)

                threads = [threading.Thread(target=_call) for _ in range(8)]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join(timeout=30)
                assert not errors
                assert call_count["n"] == 1
                assert len(results) == 8
                assert all(r is results[0] for r in results)
        finally:
            HardwareCapability._detected_decoders = saved

    def test_config_construction_does_not_probe(self):
        """构造 HardwareDecodeConfig 不触发探测（回退时构造即探测）。

        __post_init__ 的探测是惰性告警：首次 get_decoder_for_codec 才执行。
        """
        with patch.object(HardwareCapability,
                          "detect_available_decoders") as mock_detect:
            config = HardwareDecodeConfig(decoder=HardwareDecoder.VIDEOTOOLBOX)
            mock_detect.assert_not_called()
            # 首次真正选择解码器时才惰性探测
            config.get_decoder_for_codec("h264")
            assert mock_detect.called

    def test_lazy_warning_fires_once(self):
        with patch.object(HardwareCapability,
                          "detect_available_decoders",
                          return_value=[]) as mock_detect:
            config = HardwareDecodeConfig(decoder=HardwareDecoder.NVDEC)
            config.get_decoder_for_codec("h264")
            config.get_decoder_for_codec("h264")
            assert mock_detect.call_count == 1


# ============================================================================
# AUD-35001: 探测移出 _capability_lock（快路径不被 30s probe 阻塞）
# ============================================================================


class TestAud35001CapabilityFastPath:
    @pytest.fixture(autouse=True)
    def _restore_capability_cache(self):
        from src.shared.media.macos_vt import capability as cap_mod

        saved = cap_mod._cached_capability
        yield
        cap_mod._cached_capability = saved

    def test_cached_read_does_not_block_on_probe_lock(self):
        """探测锁被长时间持有时，缓存命中路径仍即时返回。

        回退（探测持 _capability_lock 全程）时：探测锁持有者同样持
        _capability_lock，缓存命中查询会被阻塞 → 本测试超时失败。
        """
        from src.shared.media.macos_vt import capability as cap_mod

        cap_mod._cached_capability = MagicMock(available=True)

        probe_lock_held = threading.Event()
        release_probe = threading.Event()

        def _hold_probe_lock() -> None:
            with cap_mod._capability_probe_lock:
                probe_lock_held.set()
                release_probe.wait(timeout=5)

        holder = threading.Thread(target=_hold_probe_lock, daemon=True)
        holder.start()
        assert probe_lock_held.wait(timeout=5)
        try:
            start = time.monotonic()
            cap_obj = cap_mod.get_zero_copy_capability()
            elapsed = time.monotonic() - start
            assert cap_obj is cap_mod._cached_capability
            assert elapsed < 1.0, f"缓存命中被探测阻塞 {elapsed:.2f}s"
        finally:
            release_probe.set()
            holder.join(timeout=5)

    def test_compute_runs_outside_capability_lock(self):
        """源码哨兵：_compute_zero_copy_capability 持探测锁而非共享锁。"""
        from src.shared.media.macos_vt import capability as cap_mod

        src = inspect.getsource(cap_mod._compute_zero_copy_capability)
        assert "with _capability_probe_lock" in src
        # probe 子进程调用点之后、同函数内不得新 acquire _capability_lock
        #（除缓存双检）
        probe_idx = src.find("_run_auto_zero_copy_probe")
        assert probe_idx != -1
        tail = src[probe_idx:]
        assert "_capability_lock" not in tail


# ============================================================================
# AUD-35010: close 防重入加锁；samples 类型收紧
# ============================================================================


class TestAud35010BridgeClose:
    @staticmethod
    def _bridge():
        from src.shared.media.macos_vt.gpu_export_bridge import (
            VideoToolboxGpuExportBridge,
        )

        bridge = VideoToolboxGpuExportBridge.__new__(VideoToolboxGpuExportBridge)
        bridge._closed = False
        bridge._close_lock = threading.Lock()
        bridge._session = MagicMock()
        bridge._provider = MagicMock()
        bridge._muxer = MagicMock()
        return bridge

    def test_concurrent_close_runs_chain_once(self):
        """两线程并发 close：关闭链只执行一次（回退时可能各执行一次）。"""
        bridge = self._bridge()
        barrier = threading.Barrier(2)
        errors: list[Exception] = []

        def _close() -> None:
            try:
                barrier.wait(timeout=5)
                bridge.close()
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        threads = [threading.Thread(target=_close) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        assert not errors
        bridge._session.close.assert_called_once()
        bridge._provider.close.assert_called_once()
        bridge._muxer.close.assert_called_once()

    def test_samples_annotation_is_ndarray(self):
        """write_audio_samples 注解为 np.ndarray（回退为 Any 时失败）。"""
        from src.shared.media.macos_vt.gpu_export_bridge import (
            VideoToolboxGpuExportBridge,
        )

        sig = inspect.signature(VideoToolboxGpuExportBridge.write_audio_samples)
        # from __future__ import annotations 下是字符串注解
        assert sig.parameters["samples"].annotation in ("np.ndarray", "ndarray")


# ============================================================================
# AUD-35011: 持久化 verdict 读改写纳入 _hw_lock；指纹懒初始化加锁
# ============================================================================


class TestAud35011HwCacheLocking:
    @pytest.fixture(autouse=True)
    def _isolated_hw_cache(self, tmp_path, monkeypatch):
        from src.shared.media.macos_vt import hardware_capability as hc

        monkeypatch.setenv("TOPOS_VT_HW_CACHE", "1")
        monkeypatch.delenv("TOPOS_VT_HW_PROBE", raising=False)
        monkeypatch.setenv("TOPOS_VT_HW_CACHE_FILE",
                           str(tmp_path / "hw_cache.json"))
        saved_cache = dict(hc._hw_cache)
        saved_fp = hc._hw_machine_fingerprint
        hc._hw_cache.clear()
        hc._hw_machine_fingerprint = None
        yield
        hc._hw_cache.clear()
        hc._hw_cache.update(saved_cache)
        hc._hw_machine_fingerprint = saved_fp

    def test_verdict_persisted_and_cached(self, monkeypatch):
        """探测 verdict 落盘 + 内存缓存；第二次查询不再探测。"""
        from src.shared.media.macos_vt import hardware_capability as hc

        probe_calls = {"n": 0}

        def _fake_probe(path: str):
            probe_calls["n"] += 1
            return True

        monkeypatch.setattr(hc, "probe_vt_hardware_decode_for_file", _fake_probe)

        v1 = hc.vt_hardware_decode_available("/tmp/a.mov", "h264", 8,
                                             width=1920, height=1080)
        assert v1 is True
        assert probe_calls["n"] == 1

        # 清内存缓存后从磁盘缓存恢复（不重新探测）
        hc._hw_cache.clear()
        v2 = hc.vt_hardware_decode_available("/tmp/a.mov", "h264", 8,
                                             width=1920, height=1080)
        assert v2 is True
        assert probe_calls["n"] == 1

    def test_store_inside_hw_lock(self):
        """源码哨兵：_store_hw_cache 调用位于 _hw_lock 临界区内。

        回退（锁外整体覆写）时 _store_hw_cache 缩进回到函数顶层，断言失败。
        """
        from src.shared.media.macos_vt import hardware_capability as hc

        src = inspect.getsource(hc.vt_hardware_decode_available)
        m = re.search(r"^\s+_store_hw_cache\(entries\)$", src, re.M)
        assert m, "_store_hw_cache 应在 with _hw_lock 块内（8 空格缩进）"

    def test_fingerprint_lazy_and_locked(self):
        """_machine_fingerprint 懒初始化持锁，重复调用返回同一对象。"""
        from src.shared.media.macos_vt import hardware_capability as hc

        assert "_FINGERPRINT_LOCK" in inspect.getsource(hc._machine_fingerprint)
        fp1 = hc._machine_fingerprint()
        fp2 = hc._machine_fingerprint()
        assert fp1 is fp2
        hc.clear_hardware_capability_cache()
        assert hc._hw_machine_fingerprint is None


# ============================================================================
# AUD-35103: bt2020c → bt2020nc 归一在 docstring 中注明理由
# ============================================================================


class TestAud35103ColorspaceDoc:
    def test_mapping_behavior_unchanged(self):
        from src.shared.media.macos_vt.packet_demux import PacketDemuxer

        assert PacketDemuxer._ffmpeg_colorspace_name(10) == "bt2020nc"
        assert PacketDemuxer._ffmpeg_colorspace_name(9) == "bt2020nc"
        assert PacketDemuxer._ffmpeg_colorspace_name(0) == ""
        assert PacketDemuxer._ffmpeg_colorspace_name(1) == "bt709"

    def test_docstring_documents_cl_normalization(self):
        """docstring 说明 10=bt2020c 刻意归一为 NCL 及理由。

        回退（docstring 仍写 10=bt2020c 且无说明）时断言失败。
        """
        from src.shared.media.macos_vt.packet_demux import PacketDemuxer

        doc = PacketDemuxer._ffmpeg_colorspace_name.__doc__ or ""
        assert "AUD-35103" in doc
        assert "bt2020c" in doc
        # 不再出现旧的矛盾表述
        assert not re.search(r"10=bt2020c。", doc)


# ============================================================================
# AUD-35204: unmap_surface 仅在解锁成功时清 mapped_buffers
# ============================================================================


class TestAud35204UnmapStateConsistency:
    def test_mapped_buffers_cleared_only_after_success_check(self):
        """源码哨兵：mapped_buffers 置 None 位于 status 校验（raise）之后。

        回退（先清再检查）时置 None 语句出现在 raise 之前，断言失败。
        """
        from src.shared.media.macos_vt import topos_surface as ts

        src = inspect.getsource(ts.ToposIOSurfacePool.acquire)
        m = re.search(
            r"def unmap_surface\(\).*?(?=\n        def |\n        return )",
            src, re.S)
        assert m, "未找到 unmap_surface 闭包"
        body = m.group(0)
        unlock_idx = body.find("IOSurfaceUnlock")
        raise_idx = body.find("raise ToposSurfaceUnavailable")
        clear_idx = body.find("slot.mapped_buffers = None")
        assert -1 not in (unlock_idx, raise_idx, clear_idx)
        assert unlock_idx < raise_idx < clear_idx, (
            "unmap_surface 必须先校验解锁状态，成功才清 mapped_buffers"
        )


# ============================================================================
# AUD-35209: _set_profile_level 的 CFString 在 finally 中 CFRelease
# ============================================================================


class TestAud35209CfRelease:
    def test_cfrelease_in_finally(self):
        """源码哨兵：VTSessionSetProperty 后 finally 内 CFRelease 两个引用。

        回退（无 CFRelease）时断言失败。
        """
        from src.shared.media.macos_vt.vt_compression import (
            VideoToolboxCompressionSession,
        )

        src = inspect.getsource(
            VideoToolboxCompressionSession._set_profile_level)
        assert "cf.CFRelease" in src
        assert "finally" in src
        set_idx = src.find("return int(_VT_LIB.VTSessionSetProperty(")
        rel_idx = src.find("if key_ref.value")
        assert set_idx != -1 and rel_idx != -1
        assert rel_idx > set_idx
        # key 与 value 两个引用都释放
        assert src.count("cf.CFRelease(") >= 2
        finally_src = src[src.find("finally"):]
        assert "key_ref" in finally_src
        assert "value_ref" in finally_src

    def test_cfrelease_signature_configured(self):
        """CFRelease 配置了 argtypes/restype（避免 ctypes 默认转换错误）。"""
        from src.shared.media.macos_vt.vt_compression import (
            VideoToolboxCompressionSession,
        )

        src = inspect.getsource(
            VideoToolboxCompressionSession._set_profile_level)
        assert "cf.CFRelease.restype" in src
        assert "cf.CFRelease.argtypes" in src
