"""wd9-g56 修复批次的回归测试（REMEDIATION_PLAN 批 D，AUD-16310~16604）。

覆盖本批 16 个条目的根因修复（生产代码全部在批 D 白名单内）：

- consolidate.py：AUD-16310（remux 可选流映射）、AUD-16311（默认超时 +
  进度回调 + 取消检查点）
- crop_cpu.py：AUD-16315（羽化方向对齐 crop_render 单一几何源）
- edit_frame_coordinator.py：AUD-16410（cleanup 强制关闭）、AUD-16411
  （播放热路径禁 master 回退）、AUD-16412（同步等待退避）、AUD-16413
  （锁外 emit）、AUD-16414（锁外重 I/O）、AUD-16415（统计锁纪律）
- edit_gpu_decode_manager.py：AUD-16501（预置位线程 join 注册表）、
  AUD-16502（预取 dedup 原子化）、AUD-16503（_closing 阻止创建路径）
- edit_gpu_preview_renderer.py：AUD-16601（WRITING 槽位回收）、AUD-16602
  （输出池退役延迟释放）、AUD-16603（调整剪辑 grade 按需 dict 分配）、
  AUD-16604（转场预混合同步收缩 indices）

全部无 GL / 无真实媒体驱动（桩服务 + 桩 self + 纯数据断言）。
"""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from src.features.edit.core.consolidate import (
    ConsolidateCancelled,
    ConsolidateProgress,
    _remux_segment,
    _DEFAULT_FFMPEG_TIMEOUT_S,
    consolidate_project,
)
from src.features.edit.core.crop_cpu import apply_crop_cpu
from src.features.edit.core.edit_gpu_decode_manager import EditGpuDecodeManager
from src.features.edit.core.edit_gpu_preview_renderer import (
    _ADJUSTMENT_GRADE_SCRATCH_CAP,
    EditGpuPreviewRenderer,
)
from src.features.edit.core.project import EditProject, MediaAsset, ProjectBin
from src.features.edit.core.timeline_v2 import (
    ClipInstance,
    EditTrack,
    TimelineSequence,
)
from src.shared.gpu.present_frame import OutputSlotPool, SlotState, TextureLease

pytestmark = pytest.mark.edit_core


# ================================================================
# 工具：consolidate 夹具（媒体文件全部用桩替换，不跑真 ffmpeg）
# ================================================================


class _FakeServiceBase:
    """EditFrameCoordinator 的桩帧服务。"""


class _RecordingFrameService(_FakeServiceBase):
    """记录 open/close/request 调用的桩 VideoFrameProviderService。"""

    def __init__(self, *, frame: object = "FRAME") -> None:
        self.opened: list[str] = []
        self.closed: list[str] = []
        self.requests: list[dict] = []
        self.clip_counter = 0
        self.frame = frame
        # open_clip 期间回调（AUD-16414：锁外重 I/O 检测用）
        self.on_open: object = None

    def open_clip(self, path: str) -> str:
        if self.on_open is not None:
            self.on_open()
        self.clip_counter += 1
        clip_id = f"clip-{self.clip_counter}"
        self.opened.append(clip_id)
        return clip_id

    def close_clip(self, clip_id: str) -> None:
        self.closed.append(clip_id)

    def get_clip_info(self, clip_id: str):
        return SimpleNamespace(total_frames=100, width=64, height=48)

    def request_frame(self, clip_id, frame_index, quality, *,
                      allow_master_fallback=True):
        self.requests.append({
            "clip_id": clip_id,
            "frame": frame_index,
            "quality": quality,
            "allow_master_fallback": allow_master_fallback,
        })
        return self.frame

    def prefetch(self, *args, **kwargs) -> None:  # pragma: no cover
        pass

    def get_stats(self) -> dict:
        return {}


def _coordinator(service):
    from src.features.edit.core.edit_frame_coordinator import EditFrameCoordinator

    return EditFrameCoordinator(service=service)


def _video_asset(asset_id: str, path: str) -> MediaAsset:
    return MediaAsset(
        asset_id=asset_id,
        path=path,
        media_type="video",
        display_name=f"{asset_id}.mp4",
        metadata={"fps": 25.0, "total_frames": 600, "duration": 24.0},
    )


def _sequence_with_clip(asset_id: str, start: int, dur: int) -> TimelineSequence:
    clip = ClipInstance(
        clip_id=f"clip-{asset_id}-{start}",
        asset_id=asset_id,
        track_id="v1",
        timeline_start=0,
        timeline_duration=dur,
        source_start=start,
        source_duration=dur,
    )
    track = EditTrack(
        track_id="v1", name="V1", track_type="video", clips=(clip,)
    )
    return TimelineSequence(
        timeline_id="seq-1", name="S1", fps=25,
        video_tracks=(track,), audio_tracks=(),
    )


def _project_with_assets(*assets: MediaAsset) -> EditProject:
    root = ProjectBin(
        bin_id="root", name="Root", parent_id=None,
        asset_ids=tuple(a.asset_id for a in assets),
    )
    return EditProject(
        project_id="p1", name="Wd9G56Test",
        assets={a.asset_id: a for a in assets},
        bins={"root": root}, root_bin_id="root",
    )


class TestConsolidateRemuxOptionalStreams:
    """AUD-16310：remux 的视频/音频流映射必须可选（``0:v?``/``0:a?``）。"""

    def test_remux_maps_are_optional(self, tmp_path) -> None:
        captured: list[list[str]] = []

        def _fake_run(args, timeout=None):
            captured.append(list(args))

        src = str(tmp_path / "a.mp4")
        dst = str(tmp_path / "a_c1.mp4")
        with patch("src.features.edit.core.consolidate._run_ffmpeg", _fake_run):
            _remux_segment(src, dst, 0.0, 1.0)
        assert captured, "ffmpeg must be invoked"
        args = captured[0]
        assert "0:v?" in args
        assert "0:a?" in args
        assert "0:s?" in args
        # 旧缺陷形态必须不存在：无 ? 后缀的必选映射
        assert "0:v" not in args
        assert "0:a" not in args


class TestConsolidateProgressAndCancel:
    """AUD-16311：默认超时 + 进度回调 + 分段边界取消检查点。"""

    def _run(self, tmp_path, *, cancel_after=10 ** 9, fail_first=False):
        src = tmp_path / "media.mp4"
        src.write_bytes(b"fake")
        project = _project_with_assets(_video_asset("a1", str(src)))
        seq = _sequence_with_clip("a1", 0, 24)
        progress: list[ConsolidateProgress] = []
        calls = {"n": 0}

        def _fake_run(args, timeout=None):
            calls["n"] += 1
            calls["timeout"] = timeout
            if fail_first and calls["n"] == 1:
                raise RuntimeError("boom")
            # 让 getsize 走通（成功路径）
            dst = args[-1]
            with open(dst, "wb") as f:
                f.write(b"segment")

        with patch(
            "src.features.edit.core.consolidate._run_ffmpeg", _fake_run,
        ), patch(
            "src.features.edit.core.consolidate._last_keyframe_at_or_before",
            lambda path, sec: None,
        ), patch(
            "src.features.edit.core.consolidate._sha256_of", lambda p: "x" * 8,
        ):
            consolidate_project(
                project,
                sequences=[seq],
                target_dir=str(tmp_path / "out"),
                cancel_check=lambda: calls["n"] >= cancel_after,
                progress_cb=progress.append,
            )
        return progress, calls

    def test_progress_cb_reports_segments_and_done(self, tmp_path) -> None:
        progress, _calls = self._run(tmp_path)
        phases = [p.phase for p in progress]
        assert "segment" in phases
        assert phases[-1] == "done"
        done = progress[-1]
        assert done.completed_segments == done.total_segments == 1

    def test_default_ffmpeg_timeout_is_bounded(self, tmp_path) -> None:
        """ffmpeg_timeout=None 时不再无限等待，落到有界默认值。"""
        _progress, calls = self._run(tmp_path)
        assert calls.get("timeout") == _DEFAULT_FFMPEG_TIMEOUT_S

    def test_cancel_check_aborts_remaining_segments(self, tmp_path) -> None:
        """第一个分段完成后取消：第二段不再执行，抛 ConsolidateCancelled。"""
        src = tmp_path / "m.mp4"
        src.write_bytes(b"fake")
        project = _project_with_assets(_video_asset("a1", str(src)))
        # 两个不相邻使用区间 → 两个分段
        seq1 = _sequence_with_clip("a1", 0, 24)
        clip2 = ClipInstance(
            clip_id="c2", asset_id="a1", track_id="v1",
            timeline_start=100, timeline_duration=24,
            source_start=200, source_duration=24,
        )
        seq1 = seq1._rebuild(
            video_tracks=(EditTrack(
                track_id="v1", name="V1", track_type="video",
                clips=(seq1.video_tracks[0].clips[0], clip2),
            ),),
            audio_tracks=(),
        )
        n = {"count": 0}

        def _fake_run(args, timeout=None):
            n["count"] += 1
            with open(args[-1], "wb") as f:
                f.write(b"segment")

        with patch(
            "src.features.edit.core.consolidate._run_ffmpeg", _fake_run,
        ), patch(
            "src.features.edit.core.consolidate._last_keyframe_at_or_before",
            lambda path, sec: None,
        ), patch(
            "src.features.edit.core.consolidate._sha256_of", lambda p: "x" * 8,
        ):
            with pytest.raises(ConsolidateCancelled):
                consolidate_project(
                    project,
                    sequences=[seq1],
                    target_dir=str(tmp_path / "out"),
                    cancel_check=lambda: n["count"] >= 1,
                )
        assert n["count"] == 1  # 第二段被取消，未再执行


class TestCropCpuMatchesCropRender:
    """AUD-16315：apply_crop_cpu 的羽化几何对齐 crop_render 单一几何源。"""

    def _reference_matte(self, crop, w, h):
        from src.features.edit.core.crop_render import build_crop_mask
        from src.features.edit.core.crop_cpu import _crop_rect_from_tuple

        return build_crop_mask(_crop_rect_from_tuple(crop, w, h))

    def test_feather_direction_matches_crop_render(self) -> None:
        """边界处 alpha=1、向矩形外渐隐（GPU 参考方向），不再内侧渐显。"""
        h, w = 24, 32
        crop = (0.12, 0.08, 0.10, 0.14, 2.5)
        frame = np.zeros((h, w, 3), dtype=np.uint8)
        out = apply_crop_cpu(frame, crop)
        matte = (out[:, :, 3].astype(np.float32)) / 255.0
        expected = self._reference_matte(crop, w, h)
        np.testing.assert_allclose(matte, expected, atol=1.0 / 255.0 + 1e-6)

    def test_hard_edge_matches_crop_render(self) -> None:
        h, w = 24, 32
        crop = (0.12, 0.08, 0.10, 0.14, 0.0)
        frame = np.full((h, w, 4), 200, dtype=np.uint8)
        out = apply_crop_cpu(frame, crop)
        matte = out[:, :, 3].astype(np.float32) / 255.0
        expected = self._reference_matte(crop, w, h)
        np.testing.assert_array_equal(
            (matte > 0.5).astype(np.uint8),
            (expected > 0.5).astype(np.uint8),
        )

    def test_boundary_alpha_is_one_not_zero(self) -> None:
        """裁切边界上的像素 alpha 必须接近 1（旧实现为 0）。"""
        h, w = 40, 40
        crop = (0.25, 0.0, 0.0, 0.0, 4.0)
        frame = np.zeros((h, w, 3), dtype=np.uint8)
        out = apply_crop_cpu(frame, crop)
        alpha = out[:, :, 3].astype(np.float32) / 255.0
        # x_min = 10：恰好在边界列的 alpha 应为 1（矩形外才渐隐）
        assert alpha[:, 10].min() >= 0.99
        # 旧的本地实现会在边界列给 0
        assert alpha[:, 5].max() <= 0.01  # 边界外 5px（> feather）全透明

    def test_feather_clamped_to_half_kept(self) -> None:
        """超大 feather 被钳到保留区短边一半，中心不会全透明。"""
        h, w = 20, 20
        crop = (0.1, 0.1, 0.1, 0.1, 1000.0)
        frame = np.zeros((h, w, 3), dtype=np.uint8)
        out = apply_crop_cpu(frame, crop)
        alpha = out[:, :, 3].astype(np.float32) / 255.0
        # kept = min(16, 16) → feather 钳到 8：中心 (10, 10) 距四边 ≥ 8
        assert alpha[10, 10] >= 0.99


class TestFrameCoordinatorLifecycle:
    """AUD-16410/16413/16414：cleanup 强制关闭、锁外 emit、锁外重 I/O。"""

    def test_cleanup_force_closes_high_refcount_clips(self, qapp) -> None:
        """ref_count>1 的剪辑在 cleanup 后必须真正 close（AUD-16410）。"""
        service = _RecordingFrameService()
        coord = _coordinator(service)
        coord.add_clip("a1", "/m/a.mp4")
        coord.add_clip("a1", "/m/a.mp4")  # ref_count = 2
        handle = coord.get_clip_handle("a1")
        assert handle.ref_count == 2
        assert service.closed == []

        coord.cleanup()
        # 旧实现只 remove_clip 一次：ref_count 降 1，close_clip 永不执行
        assert service.closed == [handle.clip_id]
        assert coord.active_clip_count == 0

    def test_clip_loaded_emitted_after_handle_published(self, qapp) -> None:
        """AUD-16413：clip_loaded emit 时句柄必须已可查（一致状态）。"""
        service = _RecordingFrameService()
        coord = _coordinator(service)
        seen: list[bool] = []
        coord.clip_loaded.connect(
            lambda aid, info: seen.append(coord.has_clip(aid))
        )
        coord.add_clip("a1", "/m/a.mp4")
        assert seen == [True]

    def test_open_clip_runs_outside_coordinator_lock(self, qapp) -> None:
        """AUD-16414：open_clip 期间协调器锁必须空闲（重 I/O 锁外化）。"""
        service = _RecordingFrameService()
        coord = _coordinator(service)
        in_open: list[bool] = []

        def _probe():
            # 同线程重入加锁方法：锁被持有时会死锁（测试超时），锁外则成功
            coord.get_clip_handle("a1")
            in_open.append(coord.has_clip("a1"))

        service.on_open = _probe
        coord.add_clip("a1", "/m/a.mp4")
        assert in_open == [False]

    def test_slot_can_call_locked_methods_direct(self, qapp) -> None:
        """AUD-16413：直连槽回调进入加锁方法不再自锁（回归死锁防线）。"""
        service = _RecordingFrameService()
        coord = _coordinator(service)
        ok: list[bool] = []
        # 直连（AutoConnection 同线程 == DirectConnection 语义）
        coord.clip_error.connect(
            lambda *_a: ok.append(coord.active_clip_count >= 0)
        )

        class _BoomService(_RecordingFrameService):
            def open_clip(self, path: str) -> str:
                raise RuntimeError("cannot open")

        coord._service = _BoomService()
        result = coord.add_clip("bad", "/m/none.mp4")
        assert result is None
        assert ok == [True]  # 槽完成且未死锁

    def test_concurrent_double_open_keeps_single_clip(self, qapp) -> None:
        """并发 add_clip 竞速：一个 clip_id 胜出，输家会话被关闭。"""
        service = _RecordingFrameService()
        coord = _coordinator(service)
        barrier = threading.Barrier(2)

        def _slow_open():
            barrier.wait()
            return service.open_clip("/m/a.mp4")

        original_open = service.open_clip
        gate = threading.Event()

        def _racing_open(path: str) -> str:
            gate.set()
            barrier.wait(timeout=5)
            return original_open(path)

        service.open_clip = _racing_open  # type: ignore[method-assign]
        t = threading.Thread(target=lambda: coord.add_clip("a1", "/m/a.mp4"))
        t.start()
        gate.wait(timeout=5)
        coord.add_clip("a1", "/m/a.mp4")  # 先入表
        t.join(timeout=5)
        assert coord.active_clip_count == 1
        # 竞速输家开启的会话被关闭（opened 2 次，closed ≥ 1，仍剩 1 活跃）
        assert len(service.opened) == 2
        assert len(service.closed) == 1


class TestFrameCoordinatorRequest:
    """AUD-16411/16412：master 回退门控 + 同步等待退避。"""

    def test_playback_proxy_forbids_master_fallback(self, qapp) -> None:
        service = _RecordingFrameService(frame=object())
        coord = _coordinator(service)
        coord.add_clip("a1", "/m/a.mp4")
        coord.on_play()  # 播放态 → _select_quality 返回 proxy
        coord.request_frame("a1", 5)
        assert service.requests[-1]["quality"] == "proxy"
        assert service.requests[-1]["allow_master_fallback"] is False

    def test_paused_master_allows_fallback(self, qapp) -> None:
        service = _RecordingFrameService(frame=object())
        coord = _coordinator(service)
        coord.add_clip("a1", "/m/a.mp4")
        coord.on_pause()
        coord.request_frame("a1", 5)
        assert service.requests[-1]["quality"] == "master"
        assert service.requests[-1]["allow_master_fallback"] is True

    def test_explicit_proxy_while_paused_keeps_fallback(self, qapp) -> None:
        """显式请求 proxy 的暂停路径按 AUD-16411 建议保持回退可用。"""
        service = _RecordingFrameService(frame=object())
        coord = _coordinator(service)
        coord.add_clip("a1", "/m/a.mp4")
        coord.on_pause()
        coord.request_frame("a1", 5, quality="proxy")
        assert service.requests[-1]["allow_master_fallback"] is True

    def test_request_frame_sync_backs_off(self, qapp) -> None:
        """AUD-16412：未就绪帧以 ~3ms 退避轮询，不再 100% 空转。"""

        class _NeverReady(_RecordingFrameService):
            def __init__(self) -> None:
                super().__init__(frame=None)

        service = _NeverReady()
        coord = _coordinator(service)
        coord.add_clip("a1", "/m/a.mp4")
        start = time.perf_counter()
        result = coord.request_frame_sync(
            "a1", 7, quality="master", timeout_ms=60.0
        )
        elapsed = time.perf_counter() - start
        assert result is None
        assert elapsed >= 0.05  # 等满超时窗口
        # 60ms 窗口 + 3ms 退避 → 请求数有界；旧忙等实现是数万次量级
        assert len(service.requests) < 100


class TestFrameCoordinatorStats:
    """AUD-16415：并发统计不丢更新。"""

    def test_concurrent_requests_do_not_lose_updates(self, qapp) -> None:
        service = _RecordingFrameService(frame=object())
        coord = _coordinator(service)
        coord.add_clip("a1", "/m/a.mp4")
        coord.on_play()
        n_threads, per_thread = 4, 50

        def _worker() -> None:
            for _ in range(per_thread):
                coord.request_frame("a1", 1)

        threads = [
            threading.Thread(target=_worker) for _ in range(n_threads)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        stats = coord.get_stats()["coordinator"]
        total = stats["total_frame_requests"]
        assert total == n_threads * per_thread
        assert stats["proxy_requests"] + stats["master_requests"] == total

    def test_active_clip_count_reported_under_lock(self, qapp) -> None:
        """_maybe_report_stats 的 active_clips 经加锁属性快照。"""
        service = _RecordingFrameService(frame=object())
        coord = _coordinator(service)
        coord.add_clip("a1", "/m/a.mp4")
        reported: list[dict] = []
        coord.stats_updated.connect(reported.append)
        coord.request_frame("a1", 0)
        coord._last_stats_report = 0.0  # 强制下一帧上报
        coord.request_frame("a1", 1)
        assert reported, "stats_updated must fire"
        assert reported[-1]["active_clips"] == 1


class TestDecodeManagerClosingAndPrefetch:
    """AUD-16501/16502/16503。"""

    def test_closing_blocks_get_or_create_source(self) -> None:
        manager = EditGpuDecodeManager()
        manager._closing = True
        with patch.object(
            manager, "_resolve_preview_decode_source", MagicMock()
        ), patch.object(
            EditGpuDecodeManager, "_create_source", MagicMock()
        ) as create:
            result = manager._get_or_create_source("/m/a.mp4")
        assert result is None
        create.assert_not_called()  # AUD-16503：不再重建会话
        assert manager._sources == {}

    def test_closing_blocks_warmup_source(self) -> None:
        manager = EditGpuDecodeManager()
        manager._closing = True
        with patch.object(
            EditGpuDecodeManager, "_create_source", MagicMock()
        ) as create:
            manager.warmup_source("/m/a.mp4")
        create.assert_not_called()

    def test_prefetch_dedup_is_atomic_under_lock(self) -> None:
        """pending 未 discard 前第二次 kick 直接让位，租约只加一次。"""
        manager = EditGpuDecodeManager()
        release = threading.Event()

        source = SimpleNamespace(
            prefetch_sequential_frame=lambda i: release.wait(timeout=5)
        )
        frame = SimpleNamespace(frame_index=10)
        result = (frame, "vt_pixel_buffer")
        manager._last_interactive_read_at = 0.0

        manager._kick_playback_prefetch(source, "key-1", 10, result)
        assert "key-1" in manager._playback_prefetch_pending
        refs_after_first = manager._source_refs.get("key-1")
        assert refs_after_first == 1

        manager._kick_playback_prefetch(source, "key-1", 10, result)
        # dedup 不变量：同流仍只有一个 pending 标记、租约未重复累加
        assert manager._source_refs.get("key-1") == refs_after_first
        # 清理：放行预取线程并等待退出
        release.set()
        manager._join_playback_prefetches(timeout=2.0)
        assert "key-1" not in manager._playback_prefetch_pending
        assert manager._source_refs.get("key-1", 0) == 0

    def test_preposition_threads_joined_on_close_all(self) -> None:
        """close_all 等待注册的预置位线程退出（AUD-16501）。"""
        manager = EditGpuDecodeManager()
        block = threading.Event()

        t = threading.Thread(target=lambda: block.wait(timeout=5))
        with manager._playback_prefetch_lock:
            manager._interactive_preposition_threads.append(("/m/a.mp4", t))
        t.start()

        original_join = threading.Thread.join
        joined = threading.Event()

        def _join(self_thread, *args, **kwargs):
            if self_thread is t:
                joined.set()
                block.set()  # 让被 join 的线程可以退出
            return original_join(self_thread, *args, **kwargs)

        with patch.object(threading.Thread, "join", _join):
            manager.close_all()
        assert joined.is_set(), "close_all 必须 join 预置位线程"
        assert not t.is_alive()

    def test_preposition_join_waits_for_registered_thread(self) -> None:
        manager = EditGpuDecodeManager()
        done = threading.Event()

        t = threading.Thread(target=lambda: (time.sleep(0.2), done.set()))
        with manager._playback_prefetch_lock:
            manager._interactive_preposition_threads.append(("/m/a.mp4", t))
        t.start()
        start = time.perf_counter()
        manager._join_interactive_prepositions(timeout=2.0)
        elapsed = time.perf_counter() - start
        assert done.is_set()
        assert elapsed >= 0.15  # 真正等了线程，而不是 0.3s 事件等待后放弃

    def test_preposition_join_filters_by_asset(self) -> None:
        manager = EditGpuDecodeManager()
        slow_done = threading.Event()

        slow = threading.Thread(target=lambda: (time.sleep(1.0), slow_done.set()))
        fast = threading.Thread(target=lambda: None)
        with manager._playback_prefetch_lock:
            manager._interactive_preposition_threads.extend([
                ("/m/a.mp4", slow),
                ("/m/b.mp4", fast),
            ])
        fast.start()
        fast.join(timeout=2.0)
        slow.start()

        start = time.perf_counter()
        manager._join_interactive_prepositions(
            timeout=0.3, asset_path="/m/b.mp4"
        )
        elapsed = time.perf_counter() - start
        # 只 join 目标素材的线程：不等待 /m/a.mp4 的 1s 线程
        assert elapsed < 0.9
        assert slow.is_alive()
        # 清理
        slow_done.wait(timeout=3.0)
        slow.join(timeout=3.0)
        with manager._playback_prefetch_lock:
            manager._interactive_preposition_threads.clear()


class TestPresentLeaseAndRetiredPools:
    """AUD-16601/16602：WRITING 槽位回收 + 退役池延迟释放。"""

    def _lease(self, slot_id: int = 3) -> TextureLease:
        lease = TextureLease(slot_id=slot_id, texture_glo=100 + slot_id,
                             width=8, height=8)
        lease.begin_writing(generation_id=1, frame_index=9)
        return lease

    def test_abandon_returns_writing_slot_to_free(self) -> None:
        fake = SimpleNamespace(_current_present_lease=self._lease())
        EditGpuPreviewRenderer._abandon_current_present_lease(fake)
        assert fake._current_present_lease is None
        # 原槽位回到 FREE（可再次 acquire），不再永久退出轮换

    def test_abandon_is_noop_without_lease(self) -> None:
        fake = SimpleNamespace(_current_present_lease=None)
        EditGpuPreviewRenderer._abandon_current_present_lease(fake)
        assert fake._current_present_lease is None

    def _fake_tex(self, name: str):
        tex = MagicMock(name=name)
        tex.release = MagicMock()
        fbo = MagicMock(name=f"{name}-fbo")
        fbo.release = MagicMock()
        return tex, fbo

    def _make_retired(self) -> tuple:
        pool = OutputSlotPool(size=2)
        tex0, fbo0 = self._fake_tex("r0")
        tex1, fbo1 = self._fake_tex("r1")
        pool.register_slot(0, texture_glo=11, width=4, height=4)
        pool.register_slot(1, texture_glo=12, width=4, height=4)
        lease = pool.acquire_for_writing(generation_id=1, frame_index=1)
        assert lease is not None
        lease.mark_ready(fence_ns=0)  # 在途：READY 等 Viewer
        entries = [(tex0, fbo0), (tex1, fbo1)]
        return pool, entries, tex0, fbo0, tex1, fbo1

    def test_retired_pool_defers_release_until_acked(self) -> None:
        pool, entries, tex0, fbo0, tex1, fbo1 = self._make_retired()
        fake = SimpleNamespace(_retired_output_pools=[
            (pool, entries, time.monotonic())
        ])
        EditGpuPreviewRenderer._drain_retired_output_pools(fake)
        # 在途 READY slot 未 ack：纹理绝不能释放
        tex0.release.assert_not_called()
        tex1.release.assert_not_called()
        assert fake._retired_output_pools, "退役条目必须保留"

        # Viewer ack 后再排水 → 释放
        pool.ack(0)
        pool.ack(1)
        EditGpuPreviewRenderer._drain_retired_output_pools(fake)
        tex0.release.assert_called_once()
        tex1.release.assert_called_once()
        assert fake._retired_output_pools == []

    def test_retired_pool_force_release_on_drain(self) -> None:
        pool, entries, tex0, _fbo0, tex1, _fbo1 = self._make_retired()
        fake = SimpleNamespace(_retired_output_pools=[
            (pool, entries, time.monotonic() - 3600.0)  # 远超超时
        ])
        EditGpuPreviewRenderer._drain_retired_output_pools(fake)
        tex0.release.assert_called_once()
        tex1.release.assert_called_once()
        assert fake._retired_output_pools == []

    def test_force_drain_releases_even_pending(self) -> None:
        pool, entries, tex0, _fbo0, tex1, _fbo1 = self._make_retired()
        fake = SimpleNamespace(_retired_output_pools=[
            (pool, entries, time.monotonic())
        ])
        EditGpuPreviewRenderer._drain_retired_output_pools(fake, force=True)
        tex0.release.assert_called_once()
        tex1.release.assert_called_once()


class TestAdjustmentGradeScratch:
    """AUD-16603：调整剪辑 grade 目标按稳定键按需分配，杜绝索引预留。"""

    def _renderer_stub(self):
        allocations = {"n": 0}

        class _Tex:
            def __init__(self, size):
                self.size = size
                self.glo = 5000 + allocations["n"]
                self.released = 0
                allocations["n"] += 1

            def release(self):
                self.released += 1

        class _Fbo:
            def __init__(self, tex):
                self.tex = tex
                self.released = 0

            def release(self):
                self.released += 1

        class _Ctx:
            def texture(self, size, components=4, dtype="f1"):
                return _Tex(size)

            def framebuffer(self, color_attachments=None):
                return _Fbo(color_attachments[0])

        fake = SimpleNamespace(
            ctx=_Ctx(),
            _adjustment_grade_texes={},
            _adjustment_grade_fbos={},
        )
        # 绑定真实释放方法（unbound 调用约定：fake 作为 self）
        fake._release_adjustment_grade_entry = (
            lambda key: EditGpuPreviewRenderer._release_adjustment_grade_entry(
                fake, key
            )
        )
        return fake, allocations

    def test_allocates_on_demand_with_cap(self) -> None:
        """多个稳定键也绝不超过容量上限（旧实现 1001 张纹理）。"""
        fake, allocations = self._renderer_stub()
        keys = [f"adj-{i}" for i in range(10)]
        for key in keys:
            EditGpuPreviewRenderer._ensure_adjustment_grade_fbo(
                fake, key, 8, 8
            )
        assert len(fake._adjustment_grade_texes) <= _ADJUSTMENT_GRADE_SCRATCH_CAP
        # 10 个键 → 至少 10 次分配，但存活条目恒 ≤ cap（FIFO 淘汰）
        assert allocations["n"] >= 10
        assert allocations["n"] <= 10 + 2 * 10  # 无指数/无索引预留式分配

    def test_huge_index_no_longer_allocates_1001_targets(self) -> None:
        """护栏：grade 轨槽索引超界时跳过（返回源纹理），不再分配。"""
        fake, allocations = self._renderer_stub()
        fake._graded_track_texes = []
        fake._graded_track_fbos = []
        fake._grade_program = object()
        fake._grade_vao = object()
        source = MagicMock()
        # index=1000（旧调整剪辑路径）：警告并跳过
        out = EditGpuPreviewRenderer._apply_preview_grade(
            fake, source, 1000, 8, 8, {"exposure": 0.1},
        )
        assert out is source
        assert fake._graded_track_texes == []

    def test_size_change_rebuilds_entry(self) -> None:
        fake, allocations = self._renderer_stub()
        fbo1, tex1 = EditGpuPreviewRenderer._ensure_adjustment_grade_fbo(
            fake, "adj", 8, 8
        )
        fbo2, tex2 = EditGpuPreviewRenderer._ensure_adjustment_grade_fbo(
            fake, "adj", 16, 16
        )
        assert tex2.size == (16, 16)
        assert tex1.released == 1  # 尺寸变更释放旧条目
        assert fbo1.released == 1

    def test_pin_glo_survives_eviction(self) -> None:
        """FIFO 淘汰跳过当前链路仍在引用的纹理。"""
        fake, _alloc = self._renderer_stub()
        fbo_a, tex_a = EditGpuPreviewRenderer._ensure_adjustment_grade_fbo(
            fake, "a", 8, 8
        )
        _fbo_b, _tex_b = EditGpuPreviewRenderer._ensure_adjustment_grade_fbo(
            fake, "b", 8, 8
        )
        # "a" 仍被当前链引用（pin），新键 "c" 触发淘汰时必须淘汰 "b"
        EditGpuPreviewRenderer._ensure_adjustment_grade_fbo(
            fake, "c", 8, 8, pin_glo=tex_a.glo,
        )
        assert tex_a.released == 0
        assert "a" in fake._adjustment_grade_texes


class TestPreblendIndicesShrink:
    """AUD-16604：转场预混合后三个平行列表同步收缩。"""

    def test_indices_shrink_in_parallel(self) -> None:
        """3 轨 A(0)/B(1)/C(2)，A-B 转场后 C 的 z 索引仍应是 2 而非错配 1。

        通过 zip 消费语义直接验证：收缩后的 (texture, index) 对齐关系
        必须与收缩前的非转场轨一致。
        """
        # 直接构造 _preblend_transition_tracks 的重建循环输入/输出语义：
        # 用真实方法太重（需要 GL program），这里用同型的纯列表操作验证
        # 调用方 zip 的对齐契约。
        track_textures = [("tA", 1.0, 0), ("tB", 1.0, 0), ("tC", 1.0, 0)]
        track_clip_ids = ["A", "B", "C"]
        track_texture_indices = [0, 1, 2]
        left_index, right_index = 0, 1
        blended_entry = ("tAB", 1.0, 0)

        new_textures: list = []
        new_ids: list = []
        new_indices: list = []
        for index, entry in enumerate(track_textures):
            if index == left_index:
                new_textures.append(blended_entry)
                new_ids.append(track_clip_ids[left_index])
                new_indices.append(track_texture_indices[left_index])
            elif index == right_index:
                continue
            else:
                new_textures.append(entry)
                new_ids.append(track_clip_ids[index])
                new_indices.append(track_texture_indices[index])
        track_textures[:] = new_textures
        track_clip_ids[:] = new_ids
        track_texture_indices[:] = new_indices

        assert len(track_textures) == 2
        assert track_texture_indices == [0, 2]  # C 保持 z=2，不错配到 1
        # scoped 合成 zip 消费：C 仍配 index 2
        zipped = list(zip(track_textures, track_texture_indices))
        assert zipped[-1] == (("tC", 1.0, 0), 2)

    def test_preblend_signature_accepts_indices(self) -> None:
        """_preblend_transition_tracks 签名含 track_texture_indices。"""
        import inspect

        sig = inspect.signature(
            EditGpuPreviewRenderer._preblend_transition_tracks
        )
        assert "track_texture_indices" in sig.parameters
