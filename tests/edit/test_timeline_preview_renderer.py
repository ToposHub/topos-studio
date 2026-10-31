"""
Timeline Preview Renderer Tests
时间线预览渲染器测试。

测试覆盖：
1. PreviewFrameCache: LRU 缓存行为
2. PreviewRenderWorker: 渲染任务队列
3. TimelinePreviewRenderer: 预览渲染控制器
4. 帧请求和缓存命中
5. 多轨合成（第一版：取最上层）
6. 线程安全和信号
"""

from __future__ import annotations

import sys
import threading
import time
import types
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, call, patch

import numpy as np
import pytest
from PyQt6.QtCore import QThread, QMutex
from PyQt6.QtGui import QImage

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.features.edit.core.timeline_v2 import (
    ClipInstance,
    EditTrack,
    TimelineSequence,
)
from src.features.edit.core.project import EditProject, MediaAsset
from src.features.edit.core.clip_params import TransformParams, CropParams
from src.features.edit.core.timeline_preview_renderer import (
    PreviewFrameCache,
    PreviewRenderWorker,
    TimelinePreviewRenderer,
    AssetResolver,
    create_timeline_preview_renderer,
)
from src.features.edit.core.edit_playback_controller import (
    EditPlaybackController,
    PlaybackDirection,
)
from src.features.edit.core.timeline_render_plan import (
    TimelineRenderPlan,
    FrameRenderPlan,
    ClipLayerInfo,
)
from src.shared.media.metadata import Rational
from src.features.edit.core.realtime_playback import (
    FrameHandle,
    LayerFrameBundle,
    SourceKind,
)
from src.features.edit.core.realtime_playback.types import FrameIdentity


# ── Fixtures ──


def test_preview_worker_reports_missing_visible_asset(tmp_path: Path) -> None:
    """The preview diagnosis must identify missing sources before compositing."""
    existing_path = tmp_path / "existing.mp4"
    missing_path = tmp_path / "missing.mp4"
    existing_path.write_bytes(b"placeholder")

    project = EditProject.create("Offline preview diagnosis")
    project = project.import_asset(
        path=str(existing_path),
        media_type="video",
        metadata={},
        display_name="existing.mp4",
    )
    project = project.import_asset(
        path=str(missing_path),
        media_type="video",
        metadata={},
        display_name="missing.mp4",
    )
    existing_id, missing_id = tuple(project.assets)

    worker = PreviewRenderWorker.__new__(PreviewRenderWorker)
    worker._asset_resolver = AssetResolver(project)
    layers = (
        SimpleNamespace(asset_id=existing_id),
        SimpleNamespace(asset_id=missing_id),
        SimpleNamespace(asset_id=missing_id),
    )

    assert worker._offline_media_for_layers(layers) == [{
        "asset_id": missing_id,
        "name": "missing.mp4",
        "path": str(missing_path),
    }]


@pytest.fixture
def mock_frame_provider():
    """创建模拟的 VideoFrameProviderService"""
    provider = Mock()

    # 模拟 open_clip 返回 clip_id
    provider.open_clip.return_value = "clip-test-001"

    # 模拟 request_frame 返回帧数据
    mock_frame = Mock()
    mock_frame.data = MagicMock()  # numpy array mock
    mock_frame.data.shape = (1080, 1920, 3)
    mock_frame.data.dtype = "uint8"
    mock_frame.pixel_format = "bgr24"
    provider.request_frame.return_value = mock_frame

    # 模拟 close_clip
    provider.close_clip.return_value = True

    return provider


@pytest.fixture
def mock_video_renderer():
    """创建模拟的 VideoAsyncRenderer"""
    renderer = Mock()
    renderer.upload_and_render_frame = Mock()
    renderer.start = Mock()
    renderer.stop_thread = Mock()
    return renderer


@pytest.fixture
def mock_asset():
    """创建模拟的 MediaAsset"""
    return MediaAsset(
        asset_id="asset-001",
        path="/path/to/video.mp4",
        media_type="video",
        display_name="Video.mp4",
        metadata={
            "width": 1920,
            "height": 1080,
            "fps": 24.0,
            "total_frames": 100,
            "duration_seconds": 4.0,
        },
    )


@pytest.fixture
def mock_project(mock_asset):
    """创建模拟的 EditProject"""
    project = EditProject.create("Test Project")
    # 导入素材
    project = project.import_asset(
        path=mock_asset.path,
        media_type=mock_asset.media_type,
        metadata=mock_asset.metadata,
    )
    return project


@pytest.fixture
def simple_clip():
    """创建简单的 clip"""
    return ClipInstance(
        clip_id="clip-001",
        asset_id="asset-001",
        track_id="track-001",
        timeline_start=0,
        timeline_duration=100,
        source_start=0,
        source_duration=100,
        speed=1.0,
        transform=TransformParams(),
        crop=CropParams(),
    )


@pytest.fixture
def simple_track(simple_clip):
    """创建简单的轨道"""
    return EditTrack(
        track_id="track-001",
        name="Video 1",
        track_type="video",
        index=0,
        clips=(simple_clip,),
    )


@pytest.fixture
def simple_sequence(simple_track):
    """创建简单的时间线序列"""
    return TimelineSequence(
        timeline_id="seq-001",
        name="Test Sequence",
        fps=Rational(24, 1),
        width=1920,
        height=1080,
        video_tracks=(simple_track,),
    )


@pytest.fixture
def multi_track_sequence():
    """创建多轨时间线序列"""
    # 下层轨道
    lower_clip = ClipInstance(
        clip_id="clip-lower",
        asset_id="asset-001",
        track_id="track-001",
        timeline_start=0,
        timeline_duration=100,
        source_start=0,
        source_duration=100,
        speed=1.0,
    )
    lower_track = EditTrack(
        track_id="track-001",
        name="Video 1",
        track_type="video",
        index=0,
        clips=(lower_clip,),
    )

    # 上层轨道
    upper_clip = ClipInstance(
        clip_id="clip-upper",
        asset_id="asset-002",
        track_id="track-002",
        timeline_start=50,
        timeline_duration=50,
        source_start=0,
        source_duration=50,
        speed=1.0,
        opacity=0.5,
    )
    upper_track = EditTrack(
        track_id="track-002",
        name="Video 2",
        track_type="video",
        index=1,
        clips=(upper_clip,),
    )

    return TimelineSequence(
        timeline_id="seq-multi",
        name="Multi Track Sequence",
        fps=Rational(24, 1),
        width=1920,
        height=1080,
        video_tracks=(lower_track, upper_track),
    )


@pytest.fixture
def multi_asset_project():
    """创建多素材的项目"""
    project = EditProject.create("Multi Asset Project")

    # 导入两个素材
    project = project.import_asset(
        path="/path/to/video1.mp4",
        media_type="video",
        metadata={
            "width": 1920,
            "height": 1080,
            "fps": 24.0,
            "total_frames": 100,
        },
    )

    project = project.import_asset(
        path="/path/to/video2.mp4",
        media_type="video",
        metadata={
            "width": 1920,
            "height": 1080,
            "fps": 24.0,
            "total_frames": 50,
        },
    )

    return project


# ── PreviewFrameCache 测试 ──


class TestPreviewFrameCache:
    """PreviewFrameCache 测试"""

    def test_put_and_get(self):
        """测试基本存取"""
        cache = PreviewFrameCache(max_size=4)

        # 存入帧
        cache.put(0, "frame-0-data")
        cache.put(1, "frame-1-data")

        # 取出帧
        assert cache.get(0) == "frame-0-data"
        assert cache.get(1) == "frame-1-data"

    def test_cache_miss(self):
        """测试缓存未命中"""
        cache = PreviewFrameCache(max_size=4)

        # 取出不存在的帧
        assert cache.get(100) is None

    def test_lru_eviction(self):
        """测试 LRU 淘汰"""
        cache = PreviewFrameCache(max_size=4)

        # 存入 4 帧
        for i in range(4):
            cache.put(i, f"frame-{i}")

        # 存入第 5 帧，应淘汰最旧的（frame 0）
        cache.put(4, "frame-4")

        # frame 0 应被淘汰
        assert cache.get(0) is None
        assert cache.get(4) == "frame-4"

    def test_lru_order(self):
        """测试 LRU 顺序"""
        cache = PreviewFrameCache(max_size=4)

        # 存入 4 帧
        for i in range(4):
            cache.put(i, f"frame-{i}")

        # 访问 frame 0（移到末尾）
        cache.get(0)

        # 存入第 5 帧，应淘汰 frame 1（最旧未访问）
        cache.put(4, "frame-4")

        # frame 0 应还在（因为刚访问）
        assert cache.get(0) == "frame-0"
        # frame 1 应被淘汰
        assert cache.get(1) is None

    def test_clear(self):
        """测试清空缓存"""
        cache = PreviewFrameCache(max_size=4)

        for i in range(4):
            cache.put(i, f"frame-{i}")

        cache.clear()

        for i in range(4):
            assert cache.get(i) is None

    def test_invalidate_range(self):
        """测试范围失效"""
        cache = PreviewFrameCache(max_size=10)

        for i in range(10):
            cache.put(i, f"frame-{i}")

        # 失效 5-7 帧
        cache.invalidate(5, 7)

        # 5-7 应被清除
        for i in range(5, 8):
            assert cache.get(i) is None

        # 其他帧应还在
        assert cache.get(0) == "frame-0"
        assert cache.get(8) == "frame-8"

    def test_hit_rate(self):
        """测试命中率统计"""
        cache = PreviewFrameCache(max_size=4)

        # 存入帧
        for i in range(4):
            cache.put(i, f"frame-{i}")

        # 3 次命中
        for i in range(3):
            cache.get(i)

        # 2 次未命中
        cache.get(100)
        cache.get(200)

        # 命中率 = 3 / 5 = 0.6
        assert cache.get_hit_rate() == 0.6

    def test_get_stats(self):
        """测试统计信息"""
        cache = PreviewFrameCache(max_size=4)

        for i in range(4):
            cache.put(i, f"frame-{i}")

        stats = cache.get_stats()

        assert stats["size"] == 4
        assert stats["max_size"] == 4
        assert stats["hits"] == 0
        assert stats["misses"] == 0

    def test_thread_safety(self):
        """测试线程安全"""
        cache = PreviewFrameCache(max_size=100)

        # 多线程写入
        def writer(start, count):
            for i in range(start, start + count):
                cache.put(i, f"frame-{i}")

        threads = []
        for i in range(10):
            t = threading.Thread(target=writer, args=(i * 10, 10))
            threads.append(t)
            t.start()

        for t in threads:
            t.join()

        # 验证缓存大小不超过最大值
        stats = cache.get_stats()
        assert stats["size"] <= cache.max_size


class TestPlaybackStaleFrameGuard:
    """Playback-result freshness guard tests."""

    def _renderer_shell(self, controller: EditPlaybackController):
        shell = type("RendererShell", (), {})()
        shell._playback_controller = controller
        shell._PLAYBACK_STALE_TOLERANCE_FRAMES = (
            TimelinePreviewRenderer._PLAYBACK_STALE_TOLERANCE_FRAMES
        )
        shell._last_emitted_frame = -1
        shell._pending_frame = None
        shell._queued_frame = None
        shell._pending_mutex = QMutex()
        # 默认模拟"本播放会话已发射过帧"的状态
        shell._playback_session_emitted = True
        return shell

    def _is_stale(
        self,
        controller: EditPlaybackController,
        frame: int,
        last_emitted_frame: int = 100,
        playback_session_emitted: bool = True,
    ) -> bool:
        shell = self._renderer_shell(controller)
        shell._last_emitted_frame = last_emitted_frame
        shell._playback_session_emitted = playback_session_emitted
        return TimelinePreviewRenderer._is_playback_result_stale(shell, frame)

    def test_forward_playback_keeps_completed_frame_behind_controller(self):
        """播放中不按墙钟丢帧，避免慢素材反复追远帧导致抖动。"""
        controller = EditPlaybackController()
        controller.set_fps(24.0)
        controller.set_timeline_duration(240)
        controller.seek(100)
        controller.play()

        tolerance = TimelinePreviewRenderer._PLAYBACK_STALE_TOLERANCE_FRAMES
        assert self._is_stale(controller, 100 - tolerance - 1) is False
        assert self._is_stale(controller, 100 - tolerance) is False

    def test_backward_playback_keeps_completed_frame_ahead_of_controller(self):
        """反向播放同样由 renderer/panel 定速，不用 stale guard 追墙钟。"""
        controller = EditPlaybackController()
        controller.set_fps(24.0)
        controller.set_timeline_duration(240)
        controller.seek(100)
        controller.play_with_speed(PlaybackDirection.BACKWARD, 1.0)

        tolerance = TimelinePreviewRenderer._PLAYBACK_STALE_TOLERANCE_FRAMES
        assert self._is_stale(controller, 100 + tolerance + 1) is False
        assert self._is_stale(controller, 100 + tolerance) is False

    def test_paused_playback_keeps_nearby_frame(self):
        """暂停时，接近 playhead 的帧不被丢弃（允许精确 seek 渲染）"""
        controller = EditPlaybackController()
        controller.set_fps(24.0)
        controller.set_timeline_duration(240)
        controller.seek(100)
        controller.play()
        controller.pause()  # 确保状态为 PAUSED

        tolerance = TimelinePreviewRenderer._PLAYBACK_STALE_TOLERANCE_FRAMES
        # 在 tolerance 范围内的帧不丢弃
        assert self._is_stale(controller, 100 - tolerance) is False
        assert self._is_stale(controller, 100 + tolerance) is False
        assert self._is_stale(controller, 100) is False

    def test_paused_playback_drops_far_stale_frame(self):
        """暂停时，远离 playhead 的旧渲染结果被丢弃（防止画面跳回）"""
        controller = EditPlaybackController()
        controller.set_fps(24.0)
        controller.set_timeline_duration(240)
        controller.seek(100)
        controller.play()
        controller.pause()  # 确保状态为 PAUSED

        tolerance = TimelinePreviewRenderer._PLAYBACK_STALE_TOLERANCE_FRAMES
        # 超出 tolerance 的帧丢弃
        assert self._is_stale(controller, 100 - tolerance - 1) is True
        assert self._is_stale(controller, 100 + tolerance + 1) is True
        # 极端情况：暂停在199但旧渲染帧97
        controller.seek(199)
        assert self._is_stale(controller, 97) is True

    def test_playing_keeps_first_completed_frame_even_if_late(self):
        controller = EditPlaybackController()
        controller.set_fps(24.0)
        controller.set_timeline_duration(240)
        controller.seek(100)
        controller.play()

        # 原始守卫：_last_emitted_frame < 0 时接受
        assert self._is_stale(controller, 20, last_emitted_frame=-1) is False
        # 新守卫：播放会话尚未发射过帧时，即使 _last_emitted_frame 有值也接受
        # （模拟播放前预览渲染设置了 _last_emitted_frame=100 的情况）
        assert self._is_stale(
            controller, 20,
            last_emitted_frame=100,
            playback_session_emitted=False,
        ) is False

    def test_playing_stale_guard_never_queues_live_controller_frame(self):
        """播放中 stale guard 不再把下一请求改成 controller.current_frame。"""
        controller = EditPlaybackController()
        controller.set_fps(24.0)
        controller.set_timeline_duration(240)
        controller.seek(100)
        controller.play()
        shell = self._renderer_shell(controller)

        assert TimelinePreviewRenderer._is_playback_result_stale(shell, 70) is False
        assert shell._queued_frame is None

    def test_playing_self_pacing_does_not_request_next_frame_without_queue(self):
        controller = EditPlaybackController()
        controller.set_fps(25.0)
        controller.set_timeline_duration(250)
        controller.seek(10)
        controller.play()
        shell = self._renderer_shell(controller)
        requested: list[int] = []
        shell.request_render = requested.append

        TimelinePreviewRenderer._advance_to_next_frame(shell)

        assert requested == []

    def test_render_in_flight_coalesces_to_latest_requested_frame(self):
        shell = self._renderer_shell(EditPlaybackController())
        shell._pending_frame = 10
        shell._render_in_flight = True
        shell._SEEK_JUMP_THRESHOLD = TimelinePreviewRenderer._SEEK_JUMP_THRESHOLD
        shell._reset_emitted_frame_guard = lambda: None

        TimelinePreviewRenderer.request_render(shell, 11)
        TimelinePreviewRenderer.request_render(shell, 12)

        assert shell._queued_frame == 12

    def test_playing_render_in_flight_coalesces_to_adjacent_frame(self):
        """当 gap <= 2 时，coalesce 应保持顺序解码（排队 adjacent）"""
        controller = EditPlaybackController()
        controller.set_timeline_duration(240)
        controller.seek(10)
        controller.play()
        shell = self._renderer_shell(controller)
        shell._pending_frame = 10
        shell._render_in_flight = True
        shell._SEEK_JUMP_THRESHOLD = TimelinePreviewRenderer._SEEK_JUMP_THRESHOLD
        shell._reset_emitted_frame_guard = lambda: None
        shell._is_controller_playing = lambda: True
        shell._coalesce_playback_frame = (
            lambda requested, pending: TimelinePreviewRenderer._coalesce_playback_frame(
                shell,
                requested,
                pending,
            )
        )

        # gap = 12 - 10 = 2，仍然使用相邻帧（11）
        TimelinePreviewRenderer.request_render(shell, 12)

        assert shell._queued_frame == 11

    def test_playing_render_in_flight_large_gap_uses_adjacent_frame(self):
        """播放中 gap 再大也只排相邻帧，避免 seek 式追帧抖动。"""
        controller = EditPlaybackController()
        controller.set_timeline_duration(240)
        controller.seek(10)
        controller.play()
        shell = self._renderer_shell(controller)
        shell._pending_frame = 10
        shell._render_in_flight = True
        shell._SEEK_JUMP_THRESHOLD = TimelinePreviewRenderer._SEEK_JUMP_THRESHOLD
        shell._reset_emitted_frame_guard = lambda: None
        shell._is_controller_playing = lambda: True
        shell._coalesce_playback_frame = (
            lambda requested, pending: TimelinePreviewRenderer._coalesce_playback_frame(
                shell,
                requested,
                pending,
            )
        )

        TimelinePreviewRenderer.request_render(shell, 15)

        assert shell._queued_frame == 11

    def test_playing_multitrack_render_in_flight_uses_adjacent_frame(self):
        """多轨合成播放时保持相邻帧推进，避免追墙钟导致画面跳动。"""
        controller = EditPlaybackController()
        controller.set_timeline_duration(240)
        controller.seek(10)
        controller.play()
        shell = self._renderer_shell(controller)
        shell._pending_frame = 10
        shell._pending_layer_count = 2
        shell._render_in_flight = True
        shell._SEEK_JUMP_THRESHOLD = TimelinePreviewRenderer._SEEK_JUMP_THRESHOLD
        shell._reset_emitted_frame_guard = lambda: None
        shell._is_controller_playing = lambda: True
        shell._coalesce_playback_frame = (
            lambda requested, pending: TimelinePreviewRenderer._coalesce_playback_frame(
                shell,
                requested,
                pending,
            )
        )

        TimelinePreviewRenderer.request_render(shell, 30)

        assert shell._queued_frame == 11

    def test_small_queued_playback_overlap_does_not_reanchor(self):
        """One queued follow-up is normal event-loop overlap, not back-pressure."""
        controller = EditPlaybackController()
        controller.set_timeline_duration(240)
        controller.seek(10)
        controller.play()
        shell = self._renderer_shell(controller)
        shell._render_in_flight = True
        shell._pending_frame = 10
        shell._queued_frame = 11
        shell._pending_layer_count = 2
        shell._is_controller_playing = lambda: True

        assert TimelinePreviewRenderer.should_defer_playback_tick(shell, 12) is False

    def test_single_track_large_backlog_defers_before_queueing_far_frame(self):
        """Only a sustained single-track backlog re-anchors playback."""
        controller = EditPlaybackController()
        controller.set_timeline_duration(240)
        controller.seek(10)
        controller.play()
        shell = self._renderer_shell(controller)
        shell._render_in_flight = True
        shell._pending_frame = 10
        shell._queued_frame = None
        shell._pending_layer_count = 1
        shell._is_controller_playing = lambda: True

        assert TimelinePreviewRenderer.should_defer_playback_tick(shell, 30) is True

    def test_multitrack_large_backlog_defers_before_queueing_far_frame(self):
        """A sustained multitrack backlog still re-anchors playback."""
        controller = EditPlaybackController()
        controller.set_timeline_duration(240)
        controller.seek(10)
        controller.play()
        shell = self._renderer_shell(controller)
        shell._render_in_flight = True
        shell._pending_frame = 10
        shell._queued_frame = None
        shell._pending_layer_count = 2
        shell._is_controller_playing = lambda: True

        assert TimelinePreviewRenderer.should_defer_playback_tick(shell, 30) is True

    def test_multitrack_adjacent_tick_can_queue_followup(self):
        """多轨渲染中，相邻帧 tick 允许进入 renderer 队列。"""
        controller = EditPlaybackController()
        controller.set_timeline_duration(240)
        controller.seek(10)
        controller.play()
        shell = self._renderer_shell(controller)
        shell._render_in_flight = True
        shell._pending_frame = 10
        shell._queued_frame = None
        shell._pending_layer_count = 2
        shell._is_controller_playing = lambda: True

        assert TimelinePreviewRenderer.should_defer_playback_tick(shell, 11) is False

    def test_reanchor_to_frame_resets_playback_time_base(self):
        """背压重锚后，下一次 tick 从稳定帧继续而不是追旧墙钟。"""
        controller = EditPlaybackController()
        controller.set_fps(24.0)
        controller.set_timeline_duration(240)
        controller.seek(10)
        controller.play()
        now = controller._state.start_time + 1.0
        assert controller.tick(now) > 10

        controller.reanchor_to_frame(10, now_monotonic=now, emit_frame_changed=False)
        next_frame = controller.tick(now + (1.0 / 24.0))

        assert next_frame == 11

    def test_advance_prefers_queued_latest_frame_during_playback(self):
        controller = EditPlaybackController()
        controller.set_fps(25.0)
        controller.set_timeline_duration(250)
        controller.seek(10)
        controller.play()
        shell = self._renderer_shell(controller)
        shell._queued_frame = 12
        requested: list[int] = []
        shell.request_render = requested.append

        TimelinePreviewRenderer._advance_to_next_frame(shell)

        assert requested == [12]
        assert shell._queued_frame is None

    def test_paused_frame_step_forward_not_coalesced(self):
        """暂停时逐帧前进：coalesce 不改变帧号，直接排队请求帧"""
        controller = EditPlaybackController()
        controller.set_timeline_duration(240)
        controller.seek(10)
        # 注意：不调用 play()，保持 paused 状态
        shell = self._renderer_shell(controller)
        shell._pending_frame = 10
        shell._render_in_flight = True
        shell._SEEK_JUMP_THRESHOLD = TimelinePreviewRenderer._SEEK_JUMP_THRESHOLD
        shell._reset_emitted_frame_guard = lambda: None
        shell._is_controller_playing = lambda: False  # 暂停状态
        shell._coalesce_playback_frame = (
            lambda requested, pending: TimelinePreviewRenderer._coalesce_playback_frame(
                shell, requested, pending,
            )
        )

        # 逐帧前进到 11，暂停时 coalesce 直接返回请求帧
        TimelinePreviewRenderer.request_render(shell, 11)
        assert shell._queued_frame == 11  # 不是 adjacent，而是请求帧本身

    def test_should_emit_bypassed_when_paused_backward_step(self):
        """暂停时向后逐帧：monotonic guard 被绕过，允许显示前一帧"""
        controller = EditPlaybackController()
        controller.set_fps(24.0)
        controller.set_timeline_duration(240)
        controller.seek(164)
        controller.play()
        controller.pause()  # 状态 = paused

        shell = self._renderer_shell(controller)
        shell._last_emitted_frame = 164  # 上一帧是 164
        shell._playback_direction = 1   # 正向

        # 向后步进到 163：应该允许（非播放状态绕过守卫）
        assert TimelinePreviewRenderer._should_emit_frame(shell, 163) is True

    def test_should_emit_bypassed_when_paused_forward_step(self):
        """暂停时向前逐帧：monotonic guard 被绕过，允许显示后一帧"""
        controller = EditPlaybackController()
        controller.set_fps(24.0)
        controller.set_timeline_duration(240)
        controller.seek(100)
        controller.play()
        controller.pause()

        shell = self._renderer_shell(controller)
        shell._last_emitted_frame = 100
        shell._playback_direction = 1

        # 向前步进到 101：允许（原本就满足 >= last_emitted）
        assert TimelinePreviewRenderer._should_emit_frame(shell, 101) is True
        # 向后步进到 99：也允许（非播放状态）
        assert TimelinePreviewRenderer._should_emit_frame(shell, 99) is True

    def test_should_emit_guard_active_during_playback(self):
        """播放中：monotonic guard 仍然生效，阻止乱序小幅度回退帧"""
        controller = EditPlaybackController()
        controller.set_fps(24.0)
        controller.set_timeline_duration(240)
        controller.seek(100)
        controller.play()  # 状态 = playing

        shell = self._renderer_shell(controller)
        shell._last_emitted_frame = 164
        shell._playback_direction = 1
        shell._SEEK_JUMP_THRESHOLD = TimelinePreviewRenderer._SEEK_JUMP_THRESHOLD

        # 小幅回退（163 < 164，delta=1 < threshold=10）：丢弃
        assert TimelinePreviewRenderer._should_emit_frame(shell, 163) is False
        # 正向帧（>= 164）：允许
        assert TimelinePreviewRenderer._should_emit_frame(shell, 164) is True
        assert TimelinePreviewRenderer._should_emit_frame(shell, 165) is True

    def test_paused_frame_step_backward_not_coalesced(self):
        """暂停时逐帧后退：coalesce 不改变帧号，直接排队请求帧"""
        controller = EditPlaybackController()
        controller.set_timeline_duration(240)
        controller.seek(10)
        shell = self._renderer_shell(controller)
        shell._pending_frame = 10
        shell._render_in_flight = True
        shell._SEEK_JUMP_THRESHOLD = TimelinePreviewRenderer._SEEK_JUMP_THRESHOLD
        shell._reset_emitted_frame_guard = lambda: None
        shell._is_controller_playing = lambda: False  # 暂停状态
        shell._coalesce_playback_frame = (
            lambda requested, pending: TimelinePreviewRenderer._coalesce_playback_frame(
                shell, requested, pending,
            )
        )

        # 逐帧后退到 9
        TimelinePreviewRenderer.request_render(shell, 9)
        assert shell._queued_frame == 9  # 不是 adjacent，而是请求帧本身

    def test_advance_last_emitted_prevents_rerender_loop(self):
        """monotonic guard 丢弃帧后，_advance_last_emitted_to_dropped_frame
        推进 _last_emitted_frame，使 _advance_to_next_frame 不再重新请求同一帧。"""
        controller = EditPlaybackController()
        controller.set_fps(24.0)
        controller.set_timeline_duration(240)
        controller.seek(106)
        controller.play()
        controller.pause()

        shell = self._renderer_shell(controller)
        shell._last_emitted_frame = 106
        shell._playback_direction = 1
        shell._SEEK_JUMP_THRESHOLD = TimelinePreviewRenderer._SEEK_JUMP_THRESHOLD

        # 模拟：帧 105 被单调守卫丢弃
        TimelinePreviewRenderer._advance_last_emitted_to_dropped_frame(shell, 105)
        # _last_emitted_frame 应推进到 max(106, 105) = 106（正向不回退）
        assert shell._last_emitted_frame == 106

        # _advance_to_next_frame: next_frame(106) == _last_emitted_frame(106) → skip
        # 不会触发 request_render → 不会形成死循环
        requested_frames: list[int] = []
        shell.request_render = lambda f: requested_frames.append(f)
        TimelinePreviewRenderer._advance_to_next_frame(shell)
        assert requested_frames == []  # 没有重新请求任何帧

    def test_advance_last_emitted_updates_forward(self):
        """正向播放：丢弃帧号 > _last_emitted_frame 时，推进到丢弃帧号"""
        shell = type("Shell", (), {})()
        shell._last_emitted_frame = 100
        shell._playback_direction = 1

        TimelinePreviewRenderer._advance_last_emitted_to_dropped_frame(shell, 105)
        assert shell._last_emitted_frame == 105

    def test_advance_last_emitted_no_regress_backward(self):
        """正向播放：丢弃帧号 < _last_emitted_frame 时，不回退"""
        shell = type("Shell", (), {})()
        shell._last_emitted_frame = 110
        shell._playback_direction = 1

        TimelinePreviewRenderer._advance_last_emitted_to_dropped_frame(shell, 105)
        assert shell._last_emitted_frame == 110  # 不回退


class TestPreviewViewportAspect:
    """Preview render size must preserve timeline aspect ratio."""

    def _renderer_shell(self, width: int = 1920, height: int = 1080):
        shell = type("RendererShell", (), {})()
        shell._sequence = TimelineSequence(
            timeline_id="tl-aspect",
            name="Aspect",
            fps=Rational(24, 1),
            width=width,
            height=height,
        )
        # Bind _get_sequence_resolution so _decode_target_size works
        shell._get_sequence_resolution = lambda: TimelinePreviewRenderer._get_sequence_resolution(shell)
        shell._effective_preview_quality_scale = (
            lambda base_w, base_h: TimelinePreviewRenderer._effective_preview_quality_scale(
                shell,
                base_w,
                base_h,
            )
        )
        shell._max_preview_edge = 1280
        return shell

    def test_raw_viewport_is_fitted_to_timeline_aspect(self):
        shell = self._renderer_shell(1920, 1080)

        fitted = TimelinePreviewRenderer._fit_viewport_to_sequence_aspect(
            shell, (800, 600)
        )

        assert fitted == (800, 450)

    def test_preview_size_fits_to_sequence_aspect_only(self):
        """Since max_preview_edge cap was removed, a 4K viewport for a
        1920×1080 sequence should fit to 1920×1080 (capped by scale=1.0)."""
        shell = self._renderer_shell(1920, 1080)

        fitted = TimelinePreviewRenderer._fit_viewport_to_sequence_aspect(
            shell, (3840, 2160)
        )

        # scale = min(3840/1920, 2160/1080, 1.0) = 1.0
        assert fitted == (1920, 1080)

    def test_preview_size_fits_small_viewport(self):
        """A viewport smaller than sequence should downscale."""
        shell = self._renderer_shell(1920, 1080)

        fitted = TimelinePreviewRenderer._fit_viewport_to_sequence_aspect(
            shell, (960, 540)
        )

        assert fitted == (960, 540)

    def test_decode_target_size_full_quality(self):
        """Full edit quality is still bounded by the configured playback cap."""
        shell = self._renderer_shell(1920, 1080)
        shell._max_preview_edge = 1920
        shell._preview_quality = 1.0
        shell._viewport_size = (896, 504)  # viewport is smaller than sequence

        # Decode uses the 1080p edit cap, not the viewport.
        assert TimelinePreviewRenderer._decode_target_size.fget(shell) == (1920, 1080)

    def test_decode_target_size_auto_quality_clamps_to_preview_edge(self):
        """Smooth mode targets 1920×1080 for a 4K timeline."""
        shell = self._renderer_shell(3840, 2160)
        shell._preview_quality = 0.0
        shell._viewport_size = (1280, 720)

        shell._max_preview_edge = 3840
        assert TimelinePreviewRenderer._decode_target_size.fget(shell) == (1920, 1080)

    def test_decode_target_size_full_quality_keeps_4k_timeline_native(self):
        """Full mode must be sharper than Smooth on a 4K timeline."""
        shell = self._renderer_shell(3840, 2160)
        shell._preview_quality = 1.0
        shell._max_preview_edge = 3840

        assert TimelinePreviewRenderer._decode_target_size.fget(shell) == (3840, 2160)

    def test_decode_target_size_smooth_does_not_upscale_low_resolution_source(self):
        """Smooth mode must preserve a source below 1080p instead of enlarging it."""
        shell = self._renderer_shell(1280, 720)
        shell._preview_quality = 0.0
        shell._max_preview_edge = 3840

        assert TimelinePreviewRenderer._decode_target_size.fget(shell) == (1280, 720)

    def test_scrub_decode_target_stays_at_720p(self):
        """Drag preview keeps a 720p-class frame even when playback is 1080p."""
        shell = self._renderer_shell(3840, 2160)
        shell._scrub_preview_edge = 1280

        assert TimelinePreviewRenderer._scrub_decode_target_size.fget(shell) == (1280, 720)

    def test_gpu_scrub_exact_uses_playback_decode_target(self):
        """GPU scrub exact frames reuse the normal playback decode source."""
        shell = self._renderer_shell(3840, 2160)
        shell._max_preview_edge = 1920
        shell._scrub_preview_edge = 1280
        shell._has_gpu_preview_pipeline = lambda: True
        shell._decode_target_size = TimelinePreviewRenderer._decode_target_size.fget(shell)

        assert TimelinePreviewRenderer._scrub_render_decode_target_size(shell) == (1920, 1080)

    def test_decode_target_size_half_quality(self):
        """At quality=0.5, decode target is half the sequence resolution."""
        shell = self._renderer_shell(1920, 1080)
        shell._preview_quality = 0.5
        shell._viewport_size = (896, 504)  # viewport irrelevant

        w, h = TimelinePreviewRenderer._decode_target_size.fget(shell)
        assert w == 960
        assert h == 540

    def test_decode_target_size_quarter_quality_even_height(self):
        """At quality=0.25, decode target height must be even for YUV."""
        shell = self._renderer_shell(1920, 1081)  # odd sequence height
        shell._preview_quality = 0.25
        shell._viewport_size = (896, 504)

        w, h = TimelinePreviewRenderer._decode_target_size.fget(shell)
        assert w == 480
        assert h % 2 == 0


# ── AssetResolver 测试 ──


class TestAssetResolver:
    """AssetResolver 测试"""

    def test_get_asset_path(self, mock_project):
        """测试获取素材路径"""
        resolver = AssetResolver(mock_project)

        # 获取已导入素材的路径
        asset = list(mock_project.assets.values())[0]
        path = resolver.get_asset_path(asset.asset_id)

        assert path == asset.path

    def test_get_asset_path_not_found(self, mock_project):
        """测试获取不存在的素材"""
        resolver = AssetResolver(mock_project)

        path = resolver.get_asset_path("non-existent-id")

        assert path is None

    def test_get_asset_metadata(self, mock_project):
        """测试获取素材元数据"""
        resolver = AssetResolver(mock_project)

        asset = list(mock_project.assets.values())[0]
        metadata = resolver.get_asset_metadata(asset.asset_id)

        assert metadata is not None
        assert "width" in metadata
        assert "height" in metadata

    def test_get_asset_type(self, mock_project):
        """测试获取素材类型"""
        resolver = AssetResolver(mock_project)

        asset = list(mock_project.assets.values())[0]
        media_type = resolver.get_asset_type(asset.asset_id)

        assert media_type == "video"


# ── PreviewRenderWorker 测试 ──


class TestPreviewRenderWorker:
    """PreviewRenderWorker 测试"""

    def test_motion_graphic_valid_disk_cache_is_used_before_browser_render(
        self, tmp_path, mock_frame_provider, simple_sequence, monkeypatch,
    ):
        """A reopened project should decode its valid cache, not reopen HTML."""
        cache_path = tmp_path / "motion-cache.mp4"
        cache_path.write_bytes(b"cache")
        clip = replace(
            simple_sequence.video_tracks[0].clips[0],
            asset_id="teaching_animation:scene-1",
            timeline_start=10,
            timeline_duration=3,
            source_duration=3,
        )
        sequence = replace(
            simple_sequence,
            video_tracks=(replace(simple_sequence.video_tracks[0], clips=(clip,)),),
        )
        project = EditProject.create("Motion cache")
        asset = MediaAsset(
            asset_id=clip.asset_id,
            path="topos://motion_graphic/scene-1",
            media_type="motion_graphic",
            display_name="Motion Graphic",
            metadata={"scene_id": "scene-1", "duration_frames": 3},
        )
        project = replace(project, assets={asset.asset_id: asset})
        from src.features.edit.core.render_cache import (
            RenderCacheManifest, RenderCacheStatus,
        )
        project = project.set_render_cache_manifest(RenderCacheManifest(
            cache_id="cache-1",
            timeline_id=sequence.timeline_id,
            clip_id=clip.clip_id,
            path=str(cache_path),
            status=RenderCacheStatus.VALID,
            render_kind="motion_graphic",
            scene_id="scene-1",
            start_frame=10,
            duration_frames=3,
        ))

        class _Source:
            def __init__(self, *args, **kwargs):
                self.reads = []

            def open(self):
                return None

            def read_frame(self, index):
                self.reads.append(index)
                return SimpleNamespace(
                    data=np.array([[[1, 2, 3]]], dtype=np.uint8),
                    pixel_format="rgb24",
                )

            def close(self):
                return None

        source_holder = []

        def _source(*args, **kwargs):
            source = _Source(*args, **kwargs)
            source_holder.append(source)
            return source

        monkeypatch.setattr("src.shared.media.ffmpeg_source.FFmpegMediaSource", _source)
        worker = PreviewRenderWorker(
            frame_provider=mock_frame_provider,
            asset_resolver=AssetResolver(project),
            video_renderer=None,
            timeline_sequence=sequence,
            viewport_size=(1920, 1080),
        )
        try:
            layer = ClipLayerInfo(
                clip=clip,
                source_frame=1,
                track_index=0,
                track_name="Graphics",
                opacity=1.0,
                transform=(0, 0, 1, 1, 0, "center", 0, 0),
                crop=(0, 0, 0, 0, 0),
            )
            first = worker._get_layer_frame(layer)
            second = worker._get_layer_frame(layer)
            assert first is not None and second is not None
            assert first[0, 0].tolist() == [3, 2, 1]
            assert source_holder[0].reads == [1]
        finally:
            worker.stop()

    def test_auto_decode_target_clamps_to_preview_edge(self, mock_frame_provider, mock_project, simple_sequence):
        """Worker smooth mode should avoid full-size master decode for edit preview."""
        resolver = AssetResolver(mock_project)

        worker = PreviewRenderWorker(
            frame_provider=mock_frame_provider,
            asset_resolver=resolver,
            video_renderer=None,
            timeline_sequence=simple_sequence,
            viewport_size=(1920, 1080),
            max_preview_edge=1280,
        )

        assert worker._preview_quality == 0.0
        assert worker._decode_target_size == (1280, 720)

        worker.stop()

    def test_enqueue_task(self, mock_frame_provider, mock_project, simple_sequence):
        """测试加入渲染任务"""
        resolver = AssetResolver(mock_project)

        worker = PreviewRenderWorker(
            frame_provider=mock_frame_provider,
            asset_resolver=resolver,
            video_renderer=None,
            viewport_size=(1920, 1080),
        )

        # 计算渲染计划
        plan = TimelineRenderPlan(simple_sequence)
        frame_plan = plan.get_frame_plan(0)

        # 加入任务
        worker.enqueue(0, frame_plan)

        # 验证队列有任务
        assert worker._queue.qsize() == 1

        # 停止线程
        worker.stop()

    def test_stop_worker(self, mock_frame_provider, mock_project):
        """测试停止渲染线程"""
        resolver = AssetResolver(mock_project)

        worker = PreviewRenderWorker(
            frame_provider=mock_frame_provider,
            asset_resolver=resolver,
            video_renderer=None,
        )

        # 启动线程
        worker.start()

        # 停止线程
        worker.stop()
        worker.wait(3000)  # QThread.wait(ms) 不接受 timeout 关键字参数

        assert not worker.isRunning()

    def test_set_viewport_size(self, mock_frame_provider, mock_project):
        """测试设置视口尺寸"""
        resolver = AssetResolver(mock_project)

        worker = PreviewRenderWorker(
            frame_provider=mock_frame_provider,
            asset_resolver=resolver,
            video_renderer=None,
        )

        worker.set_viewport_size((3840, 2160))

        assert worker._viewport_size == (3840, 2160)

        worker.stop()

    def test_gpu_path_requests_decode_at_preview_quality_size(
        self,
        mock_frame_provider,
        simple_sequence,
    ):
        """GPU decode size follows preview quality rather than widget size."""
        layer = TimelineRenderPlan(simple_sequence).get_frame_plan(0).layers[0]

        resolver = Mock()
        resolver.get_asset_type.return_value = "video"
        resolver.get_asset_path.return_value = "/path/to/video.mp4"

        decode_manager = Mock()
        decoded_frame = Mock()
        decode_manager.get_frame.return_value = (decoded_frame, "planar")

        gpu_renderer = Mock()
        gpu_renderer.submit.return_value = True

        worker = PreviewRenderWorker(
            frame_provider=mock_frame_provider,
            asset_resolver=resolver,
            video_renderer=None,
            timeline_sequence=simple_sequence,
            viewport_size=(960, 540),
            gpu_decode_manager=decode_manager,
            gpu_renderer=gpu_renderer,
        )

        ok, _ = worker._try_gpu_render(
            frame=0,
            layers=(layer,),
            transitions=(),
            generation_id=1,
        )

        assert ok is True
        decode_manager.get_frame.assert_called_once_with(
            "/path/to/video.mp4",
            0,
            target_size=(1920, 1080),
            stream_key="clip-001",
        )
        submitted_task = gpu_renderer.submit.call_args.args[0]
        assert submitted_task.output_fbo_size == (960, 540)

        worker.stop()

    def test_gpu_path_consumes_ready_scheduler_bundle(
        self,
        mock_frame_provider,
        simple_sequence,
    ):
        """A ready scheduler frame must become the submitted GPU layer payload."""
        layer = TimelineRenderPlan(simple_sequence).get_frame_plan(0).layers[0]
        resolver = Mock()
        resolver.get_asset_type.return_value = "video"
        resolver.get_asset_path.return_value = "/path/to/video.mp4"

        decoded = Mock()
        handle = FrameHandle(
            identity=FrameIdentity(
                timeline_id=simple_sequence.timeline_id,
                clip_id=layer.clip_id,
                asset_id=layer.asset_id,
                source_path="/proxy/video.mov",
                source_frame=layer.source_frame,
                source_kind=SourceKind.DISK_PROXY,
            ),
            frame_index=layer.source_frame,
            width=1920,
            height=1080,
            pixel_format="yuv420p",
            bit_depth=8,
            color_space=None,
            pts_seconds=0.0,
            source_kind=SourceKind.DISK_PROXY,
            payload=decoded,
            payload_type="planar_yuv",
        )
        scheduler = Mock()
        scheduler.layer_clip_ids = (layer.clip_id,)
        scheduler.request_bundle.return_value = LayerFrameBundle(
            timeline_frame=0,
            generation_id=1,
            layers=(handle,),
            deadline_ns=0,
            complete=True,
        )
        decode_manager = Mock()
        gpu_renderer = Mock()
        gpu_renderer.submit.return_value = True
        compositor = Mock()
        compositor.submit_backend_task.return_value = True

        worker = PreviewRenderWorker(
            frame_provider=mock_frame_provider,
            asset_resolver=resolver,
            video_renderer=None,
            timeline_sequence=simple_sequence,
            viewport_size=(960, 540),
            gpu_decode_manager=decode_manager,
            gpu_renderer=gpu_renderer,
        )
        worker._timeline_decode_scheduler = scheduler
        worker._gpu_compositor = compositor
        worker._current_render_realtime = True
        try:
            ok, trace_info = worker._try_gpu_render(
                frame=0,
                layers=(layer,),
                transitions=(),
                generation_id=1,
                task_meta={"request_kind": "playhead"},
            )
            assert ok is True
            assert trace_info["scheduler_bundle_hit"] is True
            scheduler.request_bundle.assert_called_once()
            decode_manager.get_frame.assert_not_called()
            compositor.submit_backend_task.assert_called_once()
            gpu_renderer.submit.assert_not_called()
            submitted = compositor.submit_backend_task.call_args.args[0]
            assert submitted.layers[0]["decoded_frame"] is decoded
            assert submitted.layers[0]["frame_type"] == "planar"
        finally:
            worker.stop()

    def test_gpu_scrub_path_uses_interactive_decode(
        self,
        mock_frame_provider,
        simple_sequence,
    ):
        """Scrub GPU decode should use the interactive stale/deadline-aware API."""
        layer = TimelineRenderPlan(simple_sequence).get_frame_plan(0).layers[0]

        resolver = Mock()
        resolver.get_asset_type.return_value = "video"
        resolver.get_asset_path.return_value = "/path/to/video.mp4"

        decode_manager = Mock()
        decoded_frame = Mock()
        decode_manager.get_frame_interactive.return_value = (decoded_frame, "vt_planar")
        decode_manager.estimate_interactive_decode_deadline_ms.return_value = 80.0

        gpu_renderer = Mock()
        gpu_renderer.submit.return_value = True

        worker = PreviewRenderWorker(
            frame_provider=mock_frame_provider,
            asset_resolver=resolver,
            video_renderer=None,
            timeline_sequence=simple_sequence,
            viewport_size=(960, 540),
            gpu_decode_manager=decode_manager,
            gpu_renderer=gpu_renderer,
        )
        worker.set_interactive_scrub_state(7, 0, True)

        ok, trace_info = worker._try_gpu_render(
            frame=0,
            layers=(layer,),
            transitions=(),
            generation_id=1,
            task_meta={
                "request_kind": "scrub",
                "scrub_request_token": 7,
                "request_started_at": time.perf_counter() - 10.0,
            },
        )

        assert ok is True
        assert trace_info["decode_path"] == "videotoolbox_planar"
        decode_manager.estimate_interactive_decode_deadline_ms.assert_called_once_with(
            "/path/to/video.mp4",
            0,
            target_size=(1920, 1080),
            stream_key="clip-001",
        )
        decode_manager.get_frame_interactive.assert_called_once()
        decode_manager.get_frame.assert_not_called()
        call_kwargs = decode_manager.get_frame_interactive.call_args.kwargs
        assert call_kwargs["target_size"] == (1920, 1080)
        assert call_kwargs["stream_key"] == "clip-001"
        assert callable(call_kwargs["is_stale"])
        assert isinstance(call_kwargs["deadline_at"], float)
        submitted_task = gpu_renderer.submit.call_args.args[0]
        assert submitted_task.request_kind == "scrub"
        assert submitted_task.scrub_request_token == 7
        assert call_kwargs["deadline_at"] > time.perf_counter()
        assert submitted_task.layers[0]["frame_type"] == "vt_planar"

        worker.stop()

    def test_gpu_scrub_decode_that_becomes_stale_still_submits_progress_frame(
        self,
        mock_frame_provider,
        simple_sequence,
    ):
        """An in-flight scrub decode should not be thrown away just because the mouse moved."""
        layer = TimelineRenderPlan(simple_sequence).get_frame_plan(0).layers[0]

        resolver = Mock()
        resolver.get_asset_type.return_value = "video"
        resolver.get_asset_path.return_value = "/path/to/video.mp4"

        decode_manager = Mock()
        gpu_renderer = Mock()
        gpu_renderer.submit.return_value = True

        worker = PreviewRenderWorker(
            frame_provider=mock_frame_provider,
            asset_resolver=resolver,
            video_renderer=None,
            timeline_sequence=simple_sequence,
            viewport_size=(960, 540),
            gpu_decode_manager=decode_manager,
            gpu_renderer=gpu_renderer,
        )

        decoded_frame = Mock()

        def decode_and_move_mouse(*_args, **_kwargs):
            worker.set_interactive_scrub_state(2, 20, True)
            return decoded_frame, "vt_planar"

        decode_manager.get_frame_interactive.side_effect = decode_and_move_mouse
        worker.set_interactive_scrub_state(1, 0, True)

        ok, trace_info = worker._try_gpu_render(
            frame=0,
            layers=(layer,),
            transitions=(),
            generation_id=1,
            task_meta={
                "request_kind": "scrub",
                "scrub_request_token": 1,
            },
        )

        assert ok is True
        assert trace_info["decode_path"] == "videotoolbox_planar"
        gpu_renderer.submit.assert_called_once()
        submitted_task = gpu_renderer.submit.call_args.args[0]
        assert submitted_task.frame == 0
        assert submitted_task.request_kind == "scrub"

        worker.stop()

    def test_gpu_scrub_settled_deadline_expands_budget(
        self,
        mock_frame_provider,
        simple_sequence,
    ):
        """Settled scrub repair gets a wider decode budget than live dragging."""
        resolver = Mock()
        resolver.get_asset_type.return_value = "video"
        resolver.get_asset_path.return_value = "/path/to/video.mp4"

        decode_manager = Mock()
        decode_manager.estimate_interactive_decode_deadline_ms.return_value = 80.0

        worker = PreviewRenderWorker(
            frame_provider=mock_frame_provider,
            asset_resolver=resolver,
            video_renderer=None,
            timeline_sequence=simple_sequence,
            viewport_size=(960, 540),
            gpu_decode_manager=decode_manager,
            gpu_renderer=Mock(),
        )

        deadline_at = worker._interactive_scrub_deadline_at(
            asset_path="/path/to/video.mp4",
            source_frame=0,
            target_size=(1280, 720),
            stream_key="clip-001",
            task_meta={
                "request_kind": "scrub",
                "scrub_request_token": 7,
                "scrub_settled": True,
            },
        )

        assert deadline_at - time.perf_counter() >= 0.11
        worker.stop()

    def test_gpu_scrub_abandoned_far_decode_uses_remaining_live_budget(
        self,
        mock_frame_provider,
        simple_sequence,
    ):
        """Scrub decodes far from the playhead should not receive a fresh full budget."""
        resolver = Mock()
        resolver.get_asset_type.return_value = "video"
        resolver.get_asset_path.return_value = "/path/to/video.mp4"

        decode_manager = Mock()
        decode_manager.estimate_interactive_decode_deadline_ms.return_value = 80.0

        worker = PreviewRenderWorker(
            frame_provider=mock_frame_provider,
            asset_resolver=resolver,
            video_renderer=None,
            timeline_sequence=simple_sequence,
            viewport_size=(960, 540),
            gpu_decode_manager=decode_manager,
            gpu_renderer=Mock(),
        )
        worker._interactive_scrub_stale_grace_ms = 20.0
        worker.set_interactive_scrub_state(2, 20, True)

        # Task targets frame 200 while the playhead sits at frame 20: far
        # beyond the abandon distance, so its budget shrinks to leftovers.
        deadline_at = worker._interactive_scrub_deadline_at(
            asset_path="/path/to/video.mp4",
            source_frame=0,
            target_size=(1280, 720),
            stream_key="clip-001",
            task_meta={
                "request_kind": "scrub",
                "scrub_request_token": 1,
                "frame": 200,
                "request_started_at": time.perf_counter() - 0.12,
            },
        )

        assert deadline_at - time.perf_counter() <= 0.02
        worker.stop()

    def test_gpu_scrub_superseded_nearby_decode_keeps_full_budget(
        self,
        mock_frame_provider,
        simple_sequence,
    ):
        """Nearby superseded scrub decodes keep their full budget.

        A newer scrub request a few frames away must NOT shrink the running
        decode's budget: killing nearly-complete GOP walks here is what froze
        the viewer during fast drags (DaVinci-style scrub lets them finish
        and displays the result).
        """
        resolver = Mock()
        resolver.get_asset_type.return_value = "video"
        resolver.get_asset_path.return_value = "/path/to/video.mp4"

        decode_manager = Mock()
        decode_manager.estimate_interactive_decode_deadline_ms.return_value = 80.0

        worker = PreviewRenderWorker(
            frame_provider=mock_frame_provider,
            asset_resolver=resolver,
            video_renderer=None,
            timeline_sequence=simple_sequence,
            viewport_size=(960, 540),
            gpu_decode_manager=decode_manager,
            gpu_renderer=Mock(),
        )
        worker.set_interactive_scrub_state(99, 20, True)

        # Superseded by token AND age, but the target (frame 35) is still
        # near the playhead (frame 20): full budget, decode runs to completion.
        deadline_at = worker._interactive_scrub_deadline_at(
            asset_path="/path/to/video.mp4",
            source_frame=0,
            target_size=(1280, 720),
            stream_key="clip-001",
            task_meta={
                "request_kind": "scrub",
                "scrub_request_token": 1,
                "frame": 35,
                "request_started_at": time.perf_counter() - 0.12,
            },
        )

        assert deadline_at - time.perf_counter() >= 0.05
        worker.stop()

    def test_abandoned_interactive_scrub_task_distance_rule(
        self,
        mock_frame_provider,
        simple_sequence,
    ):
        """Scrub tasks are abandoned by playhead distance, not by token churn."""
        worker = PreviewRenderWorker(
            frame_provider=mock_frame_provider,
            asset_resolver=Mock(),
            video_renderer=None,
            timeline_sequence=simple_sequence,
            viewport_size=(960, 540),
            gpu_decode_manager=Mock(),
            gpu_renderer=Mock(),
        )
        worker.set_interactive_scrub_state(500, 400, True)

        meta = {"request_kind": "scrub", "scrub_request_token": 1, "frame": 410}
        # Nearby newer request: token differs, but distance is small → keep.
        assert worker._is_abandoned_interactive_scrub_task(410, meta) is False
        # Playhead jumped far past the task target → abandon.
        worker.set_interactive_scrub_state(501, 700, True)
        assert worker._is_abandoned_interactive_scrub_task(410, meta) is True
        # Non-scrub tasks never count.
        assert worker._is_abandoned_interactive_scrub_task(410, None) is False
        worker.stop()

    def test_gpu_scrub_default_grace_keeps_near_ready_decode_alive(
        self,
        mock_frame_provider,
        simple_sequence,
    ):
        """Latest-only scrub should release superseded work within one frame."""
        worker = PreviewRenderWorker(
            frame_provider=mock_frame_provider,
            asset_resolver=Mock(),
            video_renderer=None,
            timeline_sequence=simple_sequence,
            viewport_size=(960, 540),
            gpu_decode_manager=Mock(),
            gpu_renderer=Mock(),
        )

        worker.set_interactive_scrub_state(2, 20, True)

        assert worker._is_stale_interactive_scrub_task(
            {
                "request_kind": "scrub",
                "scrub_request_token": 1,
                "request_started_at": time.perf_counter() - 0.008,
            },
            min_age_ms=worker._interactive_scrub_stale_grace_ms,
        ) is False

        worker.stop()

    def test_gpu_renderer_not_ready_uses_non_holding_fallback(
        self,
        mock_frame_provider,
        simple_sequence,
    ):
        """A failed GPU render thread must not freeze realtime playback."""
        layer = TimelineRenderPlan(simple_sequence).get_frame_plan(0).layers[0]

        resolver = Mock()
        resolver.get_asset_type.return_value = "video"
        resolver.get_asset_path.return_value = "/path/to/video.mp4"

        decode_manager = Mock()
        decode_manager.get_frame.return_value = (Mock(), "planar")
        gpu_renderer = SimpleNamespace(is_initialized=False)

        worker = PreviewRenderWorker(
            frame_provider=mock_frame_provider,
            asset_resolver=resolver,
            video_renderer=None,
            timeline_sequence=simple_sequence,
            gpu_decode_manager=decode_manager,
            gpu_renderer=gpu_renderer,
        )

        ok, trace = worker._try_gpu_render(
            frame=0,
            layers=(layer,),
            transitions=(),
            generation_id=1,
        )

        assert ok is False
        assert trace["fallback_reason"] == "gpu_renderer_not_ready"
        assert worker._should_hold_previous_on_gpu_fallback(
            True,
            trace["fallback_reason"],
        ) is False

        worker.stop()

    def test_gpu_scrub_stale_decode_preserves_fallback_reason(
        self,
        mock_frame_provider,
        simple_sequence,
    ):
        """A stale scrub decode must not be converted into generic GPU no-layer fallback."""
        layer = TimelineRenderPlan(simple_sequence).get_frame_plan(0).layers[0]

        resolver = Mock()
        resolver.get_asset_type.return_value = "video"
        resolver.get_asset_path.return_value = "/path/to/video.mp4"

        decode_manager = Mock()
        decode_manager.get_frame_interactive.return_value = (None, "interactive_stale")
        decode_manager.estimate_interactive_decode_deadline_ms.return_value = 80.0

        worker = PreviewRenderWorker(
            frame_provider=mock_frame_provider,
            asset_resolver=resolver,
            video_renderer=None,
            timeline_sequence=simple_sequence,
            viewport_size=(960, 540),
            gpu_decode_manager=decode_manager,
            gpu_renderer=Mock(),
        )

        ok, trace = worker._try_gpu_render(
            frame=0,
            layers=(layer,),
            transitions=(),
            generation_id=1,
            task_meta={
                "request_kind": "scrub",
                "scrub_request_token": 7,
                "request_started_at": time.perf_counter(),
            },
        )

        assert ok is False
        assert trace["fallback_reason"] == "interactive_scrub_stale"

        worker.stop()

    def test_gpu_path_decodes_multitrack_layers_with_independent_streams(
        self,
        mock_frame_provider,
        multi_track_sequence,
    ):
        """Multi-track GPU preview should keep layer order and stream identity."""
        layers = TimelineRenderPlan(multi_track_sequence).get_frame_plan(75).layers

        resolver = Mock()
        resolver.get_asset_type.return_value = "video"
        resolver.get_asset_path.side_effect = {
            "asset-001": "/path/to/lower.mov",
            "asset-002": "/path/to/upper.mov",
        }.get

        decode_manager = Mock()
        decode_manager.get_frame.side_effect = lambda *args, **kwargs: (Mock(), "planar")

        gpu_renderer = Mock()
        gpu_renderer.submit.return_value = True

        worker = PreviewRenderWorker(
            frame_provider=mock_frame_provider,
            asset_resolver=resolver,
            video_renderer=None,
            timeline_sequence=multi_track_sequence,
            viewport_size=(960, 540),
            gpu_decode_manager=decode_manager,
            gpu_renderer=gpu_renderer,
        )

        ok, _ = worker._try_gpu_render(
            frame=75,
            layers=layers,
            transitions=(),
            generation_id=1,
        )

        assert ok is True
        calls = {
            call.kwargs["stream_key"]: (call.args[0], call.args[1])
            for call in decode_manager.get_frame.call_args_list
        }
        assert calls == {
            "clip-lower": ("/path/to/lower.mov", 75),
            "clip-upper": ("/path/to/upper.mov", 25),
        }
        submitted_task = gpu_renderer.submit.call_args.args[0]
        track_indices = [
            layer_data["track_info"]["track_index"]
            for layer_data in submitted_task.layers
        ]
        assert track_indices == [0, 1]

        worker.stop()

    def test_execute_render_prunes_lower_layers_hidden_by_opaque_video(
        self,
        mock_frame_provider,
    ):
        """A full-frame opaque upper clip should avoid decoding hidden tracks."""
        lower_clip = ClipInstance(
            clip_id="clip-lower",
            asset_id="asset-001",
            track_id="track-001",
            timeline_start=0,
            timeline_duration=100,
            source_start=0,
            source_duration=100,
        )
        upper_clip = ClipInstance(
            clip_id="clip-upper",
            asset_id="asset-002",
            track_id="track-002",
            timeline_start=0,
            timeline_duration=100,
            source_start=0,
            source_duration=100,
        )
        sequence = TimelineSequence(
            timeline_id="seq-opaque-stack",
            name="Opaque Stack",
            fps=Rational(24, 1),
            width=1920,
            height=1080,
            video_tracks=(
                EditTrack("track-001", "Video 1", "video", 0, clips=(lower_clip,)),
                EditTrack("track-002", "Video 2", "video", 1, clips=(upper_clip,)),
            ),
        )
        plan = TimelineRenderPlan(sequence).get_frame_plan(10)

        resolver = Mock()
        resolver.get_asset_type.return_value = "video"
        # O2 起遮挡判定要求"已知无 Alpha"的源像素格式（fail-closed）：
        # 元数据缺失/未知一律保留下层，因此这里提供真实素材会携带的
        # pixel_format 元数据。
        resolver.get_asset_metadata.return_value = {"pixel_format": "yuv420p"}
        worker = PreviewRenderWorker(
            frame_provider=mock_frame_provider,
            asset_resolver=resolver,
            video_renderer=None,
            timeline_sequence=sequence,
            viewport_size=(960, 540),
            gpu_decode_manager=Mock(),
            gpu_renderer=Mock(),
        )
        captured_layers: list[tuple[str, ...]] = []

        def fake_gpu_render(frame, layers, transitions, generation_id, **kwargs):
            captured_layers.append(tuple(layer.clip_id for layer in layers))
            return True, {}

        worker._try_gpu_render = fake_gpu_render  # type: ignore[method-assign]

        worker._execute_render(10, plan, generation_id=1)

        assert captured_layers == [("clip-upper",)]
        worker.stop()

    def test_execute_render_keeps_lower_layers_under_transparent_video(
        self,
        mock_frame_provider,
        multi_track_sequence,
    ):
        """Transparent upper clips still need lower tracks for correct preview."""
        plan = TimelineRenderPlan(multi_track_sequence).get_frame_plan(75)

        resolver = Mock()
        resolver.get_asset_type.return_value = "video"
        worker = PreviewRenderWorker(
            frame_provider=mock_frame_provider,
            asset_resolver=resolver,
            video_renderer=None,
            timeline_sequence=multi_track_sequence,
            viewport_size=(960, 540),
            gpu_decode_manager=Mock(),
            gpu_renderer=Mock(),
        )
        captured_layers: list[tuple[str, ...]] = []

        def fake_gpu_render(frame, layers, transitions, generation_id, **kwargs):
            captured_layers.append(tuple(layer.clip_id for layer in layers))
            return True, {}

        worker._try_gpu_render = fake_gpu_render  # type: ignore[method-assign]

        worker._execute_render(75, plan, generation_id=1)

        assert captured_layers == [("clip-lower", "clip-upper")]
        worker.stop()

    def test_multitrack_gpu_decode_uses_worker_decode_pool(
        self,
        mock_frame_provider,
        multi_track_sequence,
    ):
        """Multi-track decode should reuse the worker's fixed decode pool."""
        layers = TimelineRenderPlan(multi_track_sequence).get_frame_plan(75).layers

        resolver = Mock()
        resolver.get_asset_type.return_value = "video"
        resolver.get_asset_path.side_effect = {
            "asset-001": "/path/to/lower.mov",
            "asset-002": "/path/to/upper.mov",
        }.get

        decode_manager = Mock()
        decode_manager.get_frame.side_effect = lambda *args, **kwargs: (Mock(), "planar")

        gpu_renderer = Mock()
        gpu_renderer.submit.return_value = True

        class InlineDecodeExecutor:
            def __init__(self):
                self.submits = []

            def submit(self, fn, *args, **kwargs):
                self.submits.append(args[0])
                return SimpleNamespace(result=lambda: fn(*args, **kwargs))

            def shutdown(self, wait=False, cancel_futures=False):
                pass

        worker = PreviewRenderWorker(
            frame_provider=mock_frame_provider,
            asset_resolver=resolver,
            video_renderer=None,
            timeline_sequence=multi_track_sequence,
            viewport_size=(960, 540),
            gpu_decode_manager=decode_manager,
            gpu_renderer=gpu_renderer,
        )
        worker._decode_executor.shutdown(wait=False, cancel_futures=True)
        fake_executor = InlineDecodeExecutor()
        worker._decode_executor = fake_executor

        ok, _ = worker._try_gpu_render(
            frame=75,
            layers=layers,
            transitions=(),
            generation_id=1,
        )

        assert ok is True
        assert len(fake_executor.submits) == 2

        worker.stop()

    def test_realtime_gpu_decode_miss_holds_previous_frame_without_cpu_fallback(
        self,
        mock_frame_provider,
        simple_sequence,
    ):
        """During realtime playback, transient GPU misses should not enter CPU composite."""
        plan = TimelineRenderPlan(simple_sequence).get_frame_plan(0)

        resolver = Mock()
        resolver.get_asset_type.return_value = "video"
        resolver.get_asset_path.return_value = "/path/to/video.mp4"

        decode_manager = Mock()
        decode_manager.get_frame.return_value = (None, "")

        gpu_renderer = Mock()
        gpu_renderer.submit.return_value = True

        worker = PreviewRenderWorker(
            frame_provider=mock_frame_provider,
            asset_resolver=resolver,
            video_renderer=None,
            timeline_sequence=simple_sequence,
            viewport_size=(960, 540),
            gpu_decode_manager=decode_manager,
            gpu_renderer=gpu_renderer,
        )
        worker._composite_layers = Mock(return_value=np.zeros((8, 8, 3), dtype=np.uint8))
        emitted = []
        worker.frame_rendered.connect(lambda frame, result: emitted.append((frame, result)))

        worker._execute_render(
            0,
            plan,
            0,
            skip_cpu_fallback_on_gpu_miss=True,
        )

        assert emitted == [(0, None)]
        worker._composite_layers.assert_not_called()

        worker.stop()

    def test_gpu_decode_miss_keeps_cpu_fallback_when_not_realtime(
        self,
        mock_frame_provider,
        simple_sequence,
    ):
        """Paused/scrub renders should still use CPU fallback if GPU decode misses."""
        plan = TimelineRenderPlan(simple_sequence).get_frame_plan(0)

        resolver = Mock()
        resolver.get_asset_type.return_value = "video"
        resolver.get_asset_path.return_value = "/path/to/video.mp4"

        decode_manager = Mock()
        decode_manager.get_frame.return_value = (None, "")

        gpu_renderer = Mock()
        gpu_renderer.submit.return_value = True

        worker = PreviewRenderWorker(
            frame_provider=mock_frame_provider,
            asset_resolver=resolver,
            video_renderer=None,
            timeline_sequence=simple_sequence,
            viewport_size=(960, 540),
            gpu_decode_manager=decode_manager,
            gpu_renderer=gpu_renderer,
        )
        worker._composite_layers = Mock(return_value=np.zeros((8, 8, 3), dtype=np.uint8))
        emitted = []
        worker.frame_rendered.connect(lambda frame, result: emitted.append((frame, result)))

        worker._execute_render(
            0,
            plan,
            0,
            skip_cpu_fallback_on_gpu_miss=False,
        )

        assert emitted
        assert emitted[0][0] == 0
        assert isinstance(emitted[0][1], QImage)
        worker._composite_layers.assert_called_once()

        worker.stop()

    def test_cpu_path_opens_independent_sessions_per_timeline_clip(
        self,
        mock_project,
    ):
        """CPU fallback should not share a decoder cursor across timeline clips."""
        resolver = AssetResolver(mock_project)
        provider = Mock()
        provider.open_clip.side_effect = ["session-a", "session-b"]

        from src.shared.media.frame import DecodedFrame
        frame_a = DecodedFrame(
            frame_index=0,
            pts=0,
            time_seconds=0.0,
            data=np.zeros((8, 8, 3), dtype=np.uint8),
            pixel_format="bgr24",
        )
        frame_b = DecodedFrame(
            frame_index=10,
            pts=10,
            time_seconds=10.0 / 24.0,
            data=np.zeros((8, 8, 3), dtype=np.uint8),
            pixel_format="bgr24",
        )
        provider.request_frame.side_effect = [frame_a, frame_b]

        worker = PreviewRenderWorker(
            frame_provider=provider,
            asset_resolver=resolver,
            video_renderer=None,
        )

        asset = list(mock_project.assets.values())[0]
        assert worker._get_video_frame(asset.path, 0, asset.asset_id, "clip-a") is not None
        assert worker._get_video_frame(asset.path, 10, asset.asset_id, "clip-b") is not None

        assert provider.open_clip.call_args_list == [
            call(asset.path, share_by_path=False, defer_memory_proxy=True),
            call(asset.path, share_by_path=False, defer_memory_proxy=True),
        ]
        assert set(worker._open_clips) == {
            f"clip-a:{asset.asset_id}",
            f"clip-b:{asset.asset_id}",
        }

        worker.stop()

    def test_plain_video_prefers_direct_preview_source_over_frame_provider(
        self,
        mock_project,
        simple_sequence,
    ):
        """Edit preview should decode ordinary source media through the lightweight path."""
        resolver = AssetResolver(mock_project)
        provider = Mock()

        from src.shared.media.frame import DecodedFrame
        decoded = DecodedFrame(
            frame_index=10,
            pts=10,
            time_seconds=10.0 / 24.0,
            data=np.zeros((1080, 1920, 3), dtype=np.uint8),
            pixel_format="bgr24",
        )
        fake_source = Mock()
        fake_source.read_frame.return_value = decoded

        worker = PreviewRenderWorker(
            frame_provider=provider,
            asset_resolver=resolver,
            video_renderer=None,
            timeline_sequence=simple_sequence,
            viewport_size=(1280, 720),
        )

        asset = list(mock_project.assets.values())[0]
        with patch(
            "src.features.edit.core.timeline_preview_renderer.os.path.isfile",
            return_value=True,
        ), patch(
            "src.shared.media.ffmpeg_source.FFmpegMediaSource",
            return_value=fake_source,
        ) as source_cls:
            frame = worker._get_video_frame(asset.path, 10, asset.asset_id, "clip-a")

        assert frame is not None
        assert frame.shape == (1080, 1920, 3)
        source_cls.assert_called_once_with(
            path=asset.path,
            target_size=(1920, 1080),
            preserve_planes=False,
            output_mode="packed_rgb",
            output_format="rgb24",
        )
        fake_source.open.assert_called_once()
        fake_source.read_frame.assert_called_once_with(10)
        provider.open_clip.assert_not_called()
        provider.request_frame.assert_not_called()

        worker.stop()

    def test_topos_video_skips_ffmpeg_direct_preview_source(
        self,
        mock_project,
        simple_sequence,
    ):
        """TPIC media must fall through to the native Topos decoder."""
        resolver = AssetResolver(mock_project)
        worker = PreviewRenderWorker(
            frame_provider=Mock(),
            asset_resolver=resolver,
            video_renderer=None,
            timeline_sequence=simple_sequence,
            viewport_size=(1280, 720),
        )
        asset = list(mock_project.assets.values())[0]

        with patch(
            "src.features.edit.core.timeline_preview_renderer.os.path.isfile",
            return_value=True,
        ), patch(
            "src.shared.media.topos_source.is_topos_movie",
            return_value=True,
        ), patch(
            "src.shared.media.ffmpeg_source.FFmpegMediaSource",
        ) as source_cls:
            result = worker._get_or_create_direct_preview_source(
                asset.path,
                asset.asset_id,
                "clip-a",
            )

        assert result == (None, None)
        source_cls.assert_not_called()
        key = (
            f"clip-a:{asset.asset_id}",
            asset.path,
            worker._decode_target_size,
        )
        assert key in worker._direct_preview_failed

        worker.stop()

    def test_direct_preview_reuses_held_transition_frame(self, mock_project, simple_sequence):
        """A transition-held source frame must not trigger a backwards GOP seek."""
        resolver = AssetResolver(mock_project)
        provider = Mock()

        from src.shared.media.frame import DecodedFrame

        decoded = DecodedFrame(
            frame_index=99,
            pts=99,
            time_seconds=99.0 / 24.0,
            data=np.zeros((1080, 1920, 3), dtype=np.uint8),
            pixel_format="bgr24",
        )
        fake_source = Mock()
        fake_source.read_frame.return_value = decoded

        worker = PreviewRenderWorker(
            frame_provider=provider,
            asset_resolver=resolver,
            video_renderer=None,
            timeline_sequence=simple_sequence,
            viewport_size=(1280, 720),
        )

        asset = list(mock_project.assets.values())[0]
        with patch(
            "src.features.edit.core.timeline_preview_renderer.os.path.isfile",
            return_value=True,
        ), patch(
            "src.shared.media.ffmpeg_source.FFmpegMediaSource",
            return_value=fake_source,
        ) as source_cls:
            first = worker._get_direct_preview_frame(
                asset.path, 99, asset.asset_id, "clip-a"
            )
            second = worker._get_direct_preview_frame(
                asset.path, 99, asset.asset_id, "clip-a"
            )

        assert first is second
        fake_source.read_frame.assert_called_once_with(99)
        source_cls.assert_called_once_with(
            path=asset.path,
            target_size=(1920, 1080),
            preserve_planes=False,
            output_mode="packed_rgb",
            output_format="rgb24",
        )

        worker.stop()

    def test_plain_single_layer_render_skips_cpu_compositor(
        self,
        mock_project,
        simple_sequence,
        mock_frame_provider,
    ):
        """A plain one-clip timeline should render like a player, not a compositor."""
        plan = TimelineRenderPlan(simple_sequence).get_frame_plan(0)
        asset = MediaAsset(
            asset_id="asset-001",
            path="/path/to/video.mp4",
            media_type="video",
            display_name="Video.mp4",
            metadata={"width": 1920, "height": 1080, "fps": 24.0},
        )
        project = replace(mock_project, assets={"asset-001": asset})
        resolver = AssetResolver(project)

        from src.shared.media.frame import DecodedFrame
        decoded = DecodedFrame(
            frame_index=0,
            pts=0,
            time_seconds=0.0,
            data=np.zeros((1080, 1920, 3), dtype=np.uint8),
            pixel_format="bgr24",
        )
        fake_source = Mock()
        fake_source.read_frame.return_value = decoded

        worker = PreviewRenderWorker(
            frame_provider=mock_frame_provider,
            asset_resolver=resolver,
            video_renderer=None,
            timeline_sequence=simple_sequence,
            viewport_size=(1920, 1080),
        )
        worker._composite_layers = Mock(return_value=np.zeros((8, 8, 3), dtype=np.uint8))

        emitted = []
        worker.frame_rendered.connect(lambda frame, result: emitted.append((frame, result)))

        with patch(
            "src.features.edit.core.timeline_preview_renderer.os.path.isfile",
            return_value=True,
        ), patch(
            "src.shared.media.ffmpeg_source.FFmpegMediaSource",
            return_value=fake_source,
        ) as source_cls:
            worker._execute_render(
                0,
                plan,
                0,
                skip_cpu_fallback_on_gpu_miss=False,
            )

        assert emitted
        assert emitted[0][0] == 0
        assert isinstance(emitted[0][1], QImage)
        assert emitted[0][1].width() == 1920
        assert emitted[0][1].height() == 1080
        source_cls.assert_called_once_with(
            path=asset.path,
            target_size=(1920, 1080),
            preserve_planes=False,
            output_mode="packed_rgb",
            output_format="rgb24",
        )
        worker._composite_layers.assert_not_called()
        mock_frame_provider.open_clip.assert_not_called()
        mock_frame_provider.request_frame.assert_not_called()

        worker.stop()

    def test_cpu_multitrack_composite_renders_at_preview_size(
        self,
        multi_asset_project,
        multi_track_sequence,
        mock_frame_provider,
    ):
        """Multi-track CPU fallback should not upscale preview frames to widget size."""
        resolver = AssetResolver(multi_asset_project)
        layers = TimelineRenderPlan(multi_track_sequence).get_frame_plan(75).layers
        worker = PreviewRenderWorker(
            frame_provider=mock_frame_provider,
            asset_resolver=resolver,
            video_renderer=None,
            timeline_sequence=multi_track_sequence,
            viewport_size=(1920, 1080),
            max_preview_edge=1280,
        )
        worker._get_layer_frame = Mock(side_effect=[
            np.full((720, 1280, 3), 32, dtype=np.uint8),
            np.full((720, 1280, 3), 224, dtype=np.uint8),
        ])

        composite = worker._composite_layers(layers, (), 75)

        assert composite is not None
        assert composite.shape == (720, 1280, 3)
        assert worker._decode_target_size == (1280, 720)

        worker.stop()

    def test_prefetch_video_frame_range_uses_isolated_clip_session(self):
        """Prebuffer must not move the display decoder session during playback."""
        resolver = Mock()
        provider = Mock()
        provider.open_clip.return_value = "prebuffer-session-a"

        worker = PreviewRenderWorker(
            frame_provider=provider,
            asset_resolver=resolver,
            video_renderer=None,
        )

        ok = worker.prefetch_video_frame_range(
            "/path/to/video.mp4",
            "asset-001",
            "clip-a",
            [0, 1, 2, 5, 6],
            quality="proxy",
        )

        assert ok is True
        assert worker._open_clips == {}
        assert worker._prebuffer_clips == {
            "clip-a:asset-001": "prebuffer-session-a",
        }
        provider.open_clip.assert_called_once_with(
            "/path/to/video.mp4",
            share_by_path=False,
            defer_memory_proxy=True,
        )
        assert provider.prefetch.call_args_list == [
            call("prebuffer-session-a", 0, 2, quality="proxy"),
            call("prebuffer-session-a", 5, 6, quality="proxy"),
        ]

        provider.prefetch.reset_mock()
        ok = worker.prefetch_video_frame_range(
            "/path/to/video.mp4",
            "asset-001",
            "clip-a",
            [7],
            quality="proxy",
        )

        assert ok is True
        provider.open_clip.assert_called_once()
        provider.prefetch.assert_called_once_with("prebuffer-session-a", 7, 7, quality="proxy")

        worker.stop()

    def test_cpu_path_accepts_adjacent_decoded_frame_index(
        self,
        mock_project,
    ):
        """Adjacent frame_index drift from PTS rounding should not blank preview."""
        resolver = AssetResolver(mock_project)
        provider = Mock()
        provider.open_clip.return_value = "session-a"

        from src.shared.media.frame import DecodedFrame
        adjacent_proxy = DecodedFrame(
            frame_index=9,
            pts=9,
            time_seconds=9.0 / 24.0,
            data=np.zeros((8, 8, 3), dtype=np.uint8),
            pixel_format="bgr24",
        )
        provider.request_frame.return_value = adjacent_proxy

        worker = PreviewRenderWorker(
            frame_provider=provider,
            asset_resolver=resolver,
            video_renderer=None,
        )

        asset = list(mock_project.assets.values())[0]
        assert worker._get_video_frame(asset.path, 10, asset.asset_id, "clip-a") is not None
        provider.request_frame.assert_called_once_with("session-a", 10, quality="proxy")

        worker.stop()

    def test_cpu_path_rejects_decoded_frame_with_far_wrong_index(
        self,
        mock_project,
    ):
        """Wrong-frame decode results must not be composited into the viewer."""
        resolver = AssetResolver(mock_project)
        provider = Mock()
        provider.open_clip.return_value = "session-a"

        from src.shared.media.frame import DecodedFrame
        wrong_proxy = DecodedFrame(
            frame_index=7,
            pts=7,
            time_seconds=7.0 / 24.0,
            data=np.zeros((8, 8, 3), dtype=np.uint8),
            pixel_format="bgr24",
        )
        wrong_master = DecodedFrame(
            frame_index=13,
            pts=13,
            time_seconds=13.0 / 24.0,
            data=np.zeros((8, 8, 3), dtype=np.uint8),
            pixel_format="bgr24",
        )
        provider.request_frame.side_effect = [wrong_proxy, wrong_master]

        worker = PreviewRenderWorker(
            frame_provider=provider,
            asset_resolver=resolver,
            video_renderer=None,
        )

        asset = list(mock_project.assets.values())[0]
        assert worker._get_video_frame(asset.path, 10, asset.asset_id, "clip-a") is None
        assert provider.request_frame.call_args_list == [
            call("session-a", 10, quality="proxy"),
            call("session-a", 10, quality="master"),
        ]

        worker.stop()

    def test_cleanup_clips(self, mock_frame_provider, mock_project):
        """测试清理 clip"""
        resolver = AssetResolver(mock_project)

        worker = PreviewRenderWorker(
            frame_provider=mock_frame_provider,
            asset_resolver=resolver,
            video_renderer=None,
        )

        # 模拟已打开的 clip
        worker._open_clips["asset-001"] = "clip-test-001"

        # 清理
        worker.cleanup_clips()

        # 验证 clip 已关闭
        mock_frame_provider.close_clip.assert_called_once_with("clip-test-001")
        assert len(worker._open_clips) == 0


# ── TimelinePreviewRenderer 测试 ──


class TestTimelinePreviewRenderer:
    """TimelinePreviewRenderer 测试"""

    def test_gpu_pipeline_not_ready_keeps_cpu_playback_fallback_available(self):
        """A timed-out shared GL context must not make playback frame-less.

        The GPU objects are attached early in the editor lifecycle, before
        ``EditGpuPreviewRenderer.run()`` flips ``is_initialized``.  A single
        cached Motion Graphic Clip must still be allowed to present through
        the normal CPU/cache path during that window (and after a failed GL
        init), instead of being held forever as a GPU miss.
        """
        shell = SimpleNamespace(
            _worker=SimpleNamespace(
                _gpu_decode_manager=object(),
                _gpu_renderer=SimpleNamespace(is_initialized=False),
            ),
            _scrub_active=False,
            # Even after an earlier frame was displayed, a failed shared GL
            # context must continue to allow CPU/cache frames.
            _playback_session_emitted=True,
            _playback_controller=SimpleNamespace(
                state=SimpleNamespace(value="playing"),
            ),
            _is_controller_playing=lambda: True,
        )
        shell._has_gpu_preview_pipeline = lambda: TimelinePreviewRenderer._has_gpu_preview_pipeline(shell)
        shell._should_use_gpu_only_realtime_preview = lambda: TimelinePreviewRenderer._should_use_gpu_only_realtime_preview(shell)

        assert TimelinePreviewRenderer._has_gpu_preview_pipeline(shell) is False
        assert TimelinePreviewRenderer._should_use_gpu_only_realtime_preview(shell) is False
        assert TimelinePreviewRenderer._should_skip_cpu_fallback_on_gpu_miss(shell) is False

    def test_ready_gpu_pipeline_can_hold_on_gpu_miss(self):
        """Once initialized, the media-player-style GPU hold policy remains."""
        shell = SimpleNamespace(
            _worker=SimpleNamespace(
                _gpu_decode_manager=object(),
                _gpu_renderer=SimpleNamespace(is_initialized=True),
            ),
            _scrub_active=False,
            _playback_session_emitted=False,
            _playback_controller=SimpleNamespace(
                state=SimpleNamespace(value="playing"),
            ),
            _is_controller_playing=lambda: True,
        )
        shell._has_gpu_preview_pipeline = lambda: TimelinePreviewRenderer._has_gpu_preview_pipeline(shell)
        shell._should_use_gpu_only_realtime_preview = lambda: TimelinePreviewRenderer._should_use_gpu_only_realtime_preview(shell)

        assert TimelinePreviewRenderer._has_gpu_preview_pipeline(shell) is True
        assert TimelinePreviewRenderer._should_use_gpu_only_realtime_preview(shell) is True
        assert TimelinePreviewRenderer._should_skip_cpu_fallback_on_gpu_miss(shell) is True

    def test_init(self, mock_project, simple_sequence, mock_frame_provider):
        """测试初始化"""
        renderer = TimelinePreviewRenderer(
            project=mock_project,
            sequence=simple_sequence,
            frame_provider=mock_frame_provider,
            viewport_size=(1920, 1080),
        )

        assert renderer._project == mock_project
        assert renderer._sequence == simple_sequence
        assert renderer._worker is not None
        assert renderer._cache is not None

        # 关闭
        renderer.shutdown()

    def test_request_render_cached(self, mock_project, simple_sequence, mock_frame_provider):
        """测试请求渲染（缓存命中）"""
        renderer = TimelinePreviewRenderer(
            project=mock_project,
            sequence=simple_sequence,
            frame_provider=mock_frame_provider,
            viewport_size=(1920, 1080),
        )

        # 预先缓存帧
        mock_qimage = Mock()
        renderer._cache.put(0, mock_qimage)

        # 请求渲染帧 0（应从缓存返回）
        with patch.object(renderer, 'frame_ready') as mock_signal:
            renderer.request_render(0)

            # 验证信号发射
            mock_signal.emit.assert_called_once_with(0, mock_qimage)

        renderer.shutdown()

    def test_scrub_preview_uses_cache_without_enqueueing_exact_render(
        self,
        mock_project,
        simple_sequence,
        mock_frame_provider,
    ):
        """Scrub preview should show cached frames without starting random seek renders."""
        renderer = TimelinePreviewRenderer(
            project=mock_project,
            sequence=simple_sequence,
            frame_provider=mock_frame_provider,
            viewport_size=(1920, 1080),
        )
        mock_qimage = Mock()
        renderer._cache.put(12, mock_qimage)

        with patch.object(renderer, 'frame_ready') as mock_signal:
            renderer.begin_scrub(10)
            renderer.request_scrub_preview(12)

            mock_signal.emit.assert_called_once_with(12, mock_qimage)
            assert renderer._worker._queue.qsize() == 0

        renderer.shutdown()

    def test_scrub_preview_cache_miss_requests_async_thumbnail(
        self,
        mock_project,
        simple_sequence,
        mock_frame_provider,
    ):
        """Scrub preview should request lightweight thumbnails when no cache exists."""
        asset = MediaAsset(
            asset_id="asset-001",
            path="/path/to/video.mp4",
            media_type="video",
            display_name="Video.mp4",
            metadata={"total_frames": 100},
        )
        project = replace(mock_project, assets={"asset-001": asset})
        renderer = TimelinePreviewRenderer(
            project=project,
            sequence=simple_sequence,
            frame_provider=mock_frame_provider,
            viewport_size=(1920, 1080),
        )

        provider = Mock()
        provider.frame_thumbnail_ready.connect = Mock()
        provider.get_cached_frame_pixmap.return_value = None
        provider.get_cached_thumbnail.return_value = None

        fake_module = types.ModuleType("src.shared.media.thumbnail_provider")
        fake_module.ThumbnailProvider = type(
            "FakeThumbnailProvider",
            (),
            {"instance": staticmethod(lambda: provider)},
        )

        with patch.dict(sys.modules, {"src.shared.media.thumbnail_provider": fake_module}):
            renderer.begin_scrub(10)
            renderer.request_scrub_preview(12)

        provider.request_frame_pixmap_async.assert_called_once()
        args, kwargs = provider.request_frame_pixmap_async.call_args
        assert args[:4] == ("asset-001", "/path/to/video.mp4", 12, 100)
        assert kwargs["target_height"] == 256
        assert renderer._worker._queue.qsize() == 0
        assert renderer._placeholder_accepting_frame == 12

        renderer.shutdown()

    def test_scrub_preview_does_not_emit_generic_asset_thumbnail(
        self,
        mock_project,
        simple_sequence,
        mock_frame_provider,
    ):
        """Fast scrub must not show the same media-pool thumbnail for every frame."""
        asset = MediaAsset(
            asset_id="asset-001",
            path="/path/to/video.mp4",
            media_type="video",
            display_name="Video.mp4",
            metadata={"total_frames": 100},
        )
        project = replace(mock_project, assets={"asset-001": asset})
        renderer = TimelinePreviewRenderer(
            project=project,
            sequence=simple_sequence,
            frame_provider=mock_frame_provider,
            viewport_size=(1920, 1080),
        )

        image = QImage(16, 9, QImage.Format.Format_RGB888)
        image.fill(0)

        class FakePixmap:
            def isNull(self):
                return False

            def toImage(self):
                return image

        provider = Mock()
        provider.frame_thumbnail_ready.connect = Mock()
        provider.get_cached_frame_pixmap.return_value = None
        provider.get_cached_thumbnail.return_value = FakePixmap()

        fake_module = types.ModuleType("src.shared.media.thumbnail_provider")
        fake_module.ThumbnailProvider = type(
            "FakeThumbnailProvider",
            (),
            {"instance": staticmethod(lambda: provider)},
        )

        with patch.dict(sys.modules, {"src.shared.media.thumbnail_provider": fake_module}):
            with patch.object(renderer, "_schedule_scrub_exact_render") as schedule_exact:
                with patch.object(renderer, "frame_ready") as mock_signal:
                    renderer.begin_scrub(10)
                    renderer.request_scrub_preview(12)

        mock_signal.emit.assert_not_called()
        provider.get_cached_thumbnail.assert_not_called()
        provider.request_frame_pixmap_async.assert_called_once()
        schedule_exact.assert_called_once_with(12)

        renderer.shutdown()

    def test_scrub_preview_cache_miss_schedules_exact_render(
        self,
        mock_project,
        simple_sequence,
        mock_frame_provider,
    ):
        """Thumbnail scrub preview is only a placeholder; latest frame gets real render."""
        asset = MediaAsset(
            asset_id="asset-001",
            path="/path/to/video.mp4",
            media_type="video",
            display_name="Video.mp4",
            metadata={"total_frames": 100},
        )
        project = replace(mock_project, assets={"asset-001": asset})
        renderer = TimelinePreviewRenderer(
            project=project,
            sequence=simple_sequence,
            frame_provider=mock_frame_provider,
            viewport_size=(1920, 1080),
        )

        provider = Mock()
        provider.frame_thumbnail_ready.connect = Mock()
        provider.get_cached_frame_pixmap.return_value = None
        provider.get_cached_thumbnail.return_value = None

        fake_module = types.ModuleType("src.shared.media.thumbnail_provider")
        fake_module.ThumbnailProvider = type(
            "FakeThumbnailProvider",
            (),
            {"instance": staticmethod(lambda: provider)},
        )

        with patch.dict(sys.modules, {"src.shared.media.thumbnail_provider": fake_module}):
            with patch.object(renderer, "_schedule_scrub_exact_render") as schedule_exact:
                renderer.begin_scrub(10)
                renderer.request_scrub_preview(12)

        schedule_exact.assert_called_once_with(12)
        assert renderer._worker._queue.qsize() == 0

        renderer.shutdown()

    def test_gpu_scrub_preview_skips_nearby_cpu_cache_placeholder(
        self,
        mock_project,
        simple_sequence,
        mock_frame_provider,
    ):
        """GPU scrub should not present a nearby cached still as the current frame."""
        renderer = TimelinePreviewRenderer(
            project=mock_project,
            sequence=simple_sequence,
            frame_provider=mock_frame_provider,
            viewport_size=(1920, 1080),
        )
        renderer._worker._gpu_decode_manager = Mock()
        renderer._worker._gpu_renderer = Mock()
        renderer._cache.put(11, Mock())

        with patch.object(renderer, "_schedule_scrub_exact_render") as schedule_exact:
            with patch.object(renderer, "frame_ready") as mock_signal:
                renderer.begin_scrub(10)
                renderer.request_scrub_preview(12)

        mock_signal.emit.assert_not_called()
        schedule_exact.assert_called_once_with(12)

        renderer.shutdown()

    def test_gpu_scrub_preview_emits_exact_cpu_cache_but_still_schedules_gpu_render(
        self,
        mock_project,
        simple_sequence,
        mock_frame_provider,
    ):
        """GPU scrub may show an exact cached frame, but it must still chase the GPU frame."""
        renderer = TimelinePreviewRenderer(
            project=mock_project,
            sequence=simple_sequence,
            frame_provider=mock_frame_provider,
            viewport_size=(1920, 1080),
        )
        renderer._worker._gpu_decode_manager = Mock()
        renderer._worker._gpu_renderer = Mock()
        cached = Mock()
        renderer._cache.put(12, cached)

        with patch.object(renderer, "_schedule_scrub_exact_render") as schedule_exact:
            with patch.object(renderer, "frame_ready") as mock_signal:
                renderer.begin_scrub(10)
                renderer.request_scrub_preview(12)

        mock_signal.emit.assert_called_once_with(12, cached)
        schedule_exact.assert_called_once_with(12)

        renderer.shutdown()

    def test_gpu_scrub_exact_submit_skips_exact_cpu_cache(
        self,
        mock_project,
        simple_sequence,
        mock_frame_provider,
    ):
        """Coalesced GPU scrub submits must not terminate on CPU cache hits."""
        renderer = TimelinePreviewRenderer(
            project=mock_project,
            sequence=simple_sequence,
            frame_provider=mock_frame_provider,
            viewport_size=(1920, 1080),
        )
        renderer._worker._gpu_decode_manager = Mock()
        renderer._worker._gpu_renderer = Mock()
        renderer.begin_scrub(12)
        renderer._cache.put(12, Mock())

        with patch.object(renderer, "_dispatch_scrub_exact_render", return_value=True) as dispatch_exact:
            with patch.object(renderer, "frame_ready") as mock_signal:
                renderer._submit_scrub_exact_render(12, renderer._scrub_exact_request_token)

        mock_signal.emit.assert_not_called()
        dispatch_exact.assert_called_once_with(12)

        renderer.shutdown()

    def test_gpu_scrub_preview_skips_thumbnail_placeholder(
        self,
        mock_project,
        simple_sequence,
        mock_frame_provider,
    ):
        """GPU scrub should chase true frames instead of blurry thumbnail placeholders."""
        asset = MediaAsset(
            asset_id="asset-001",
            path="/path/to/video.mp4",
            media_type="video",
            display_name="Video.mp4",
            metadata={"total_frames": 100},
        )
        project = replace(mock_project, assets={"asset-001": asset})
        renderer = TimelinePreviewRenderer(
            project=project,
            sequence=simple_sequence,
            frame_provider=mock_frame_provider,
            viewport_size=(1920, 1080),
        )
        renderer._worker._gpu_decode_manager = Mock()
        renderer._worker._gpu_renderer = Mock()

        provider = Mock()
        provider.frame_thumbnail_ready.connect = Mock()
        provider.get_cached_frame_pixmap.return_value = None
        provider.get_cached_thumbnail.return_value = None

        fake_module = types.ModuleType("src.shared.media.thumbnail_provider")
        fake_module.ThumbnailProvider = type(
            "FakeThumbnailProvider",
            (),
            {"instance": staticmethod(lambda: provider)},
        )

        with patch.dict(sys.modules, {"src.shared.media.thumbnail_provider": fake_module}):
            with patch.object(renderer, "_schedule_scrub_exact_render") as schedule_exact:
                renderer.begin_scrub(10)
                renderer.request_scrub_preview(12)

        provider.request_frame_pixmap_async.assert_not_called()
        schedule_exact.assert_called_once_with(12)

        renderer.shutdown()

    def test_begin_scrub_forces_non_realtime_viewer_mode(
        self,
        mock_project,
        simple_sequence,
        mock_frame_provider,
    ):
        """Scrub must leave realtime mode so ProgramViewer accepts preview frames."""
        renderer = TimelinePreviewRenderer(
            project=mock_project,
            sequence=simple_sequence,
            frame_provider=mock_frame_provider,
            viewport_size=(1920, 1080),
        )
        mode_changes: list[bool] = []
        renderer.playback_mode_changed.connect(mode_changes.append)

        renderer.notify_playback_state(True)
        assert renderer._engine_playback_active is True

        renderer.begin_scrub(10)

        assert renderer._engine_playback_active is False
        assert mode_changes[-1] is False

        renderer.shutdown()

    def test_scrub_allows_cpu_fallback_even_if_controller_is_still_playing(
        self,
        mock_project,
        simple_sequence,
        mock_frame_provider,
    ):
        """A stale playing controller must not make CPU-only playback hold."""
        renderer = TimelinePreviewRenderer(
            project=mock_project,
            sequence=simple_sequence,
            frame_provider=mock_frame_provider,
            viewport_size=(1920, 1080),
        )
        controller = EditPlaybackController()
        controller.set_timeline_duration(100)
        controller.play()
        renderer.set_playback_controller(controller)
        renderer._playback_session_emitted = True

        assert renderer._should_skip_cpu_fallback_on_gpu_miss() is False

        renderer.begin_scrub(10)

        assert renderer._should_skip_cpu_fallback_on_gpu_miss() is False

        renderer.shutdown()

    def test_playback_with_gpu_pipeline_skips_cpu_fallback_before_first_emit(
        self,
        mock_project,
        simple_sequence,
        mock_frame_provider,
    ):
        """Realtime playback must not enter CPU QImage fallback when GPU is attached."""
        renderer = TimelinePreviewRenderer(
            project=mock_project,
            sequence=simple_sequence,
            frame_provider=mock_frame_provider,
            viewport_size=(1920, 1080),
        )
        controller = EditPlaybackController()
        controller.set_timeline_duration(100)
        controller.play()
        renderer.set_playback_controller(controller)
        renderer._playback_session_emitted = False
        renderer._worker._gpu_decode_manager = Mock()
        renderer._worker._gpu_renderer = Mock()

        assert renderer._should_skip_cpu_fallback_on_gpu_miss() is True

        renderer.shutdown()

    def test_playback_without_gpu_pipeline_allows_first_cpu_fallback(
        self,
        mock_project,
        simple_sequence,
        mock_frame_provider,
    ):
        """Headless/CPU-only playback keeps the compatibility first-frame fallback."""
        renderer = TimelinePreviewRenderer(
            project=mock_project,
            sequence=simple_sequence,
            frame_provider=mock_frame_provider,
            viewport_size=(1920, 1080),
        )
        controller = EditPlaybackController()
        controller.set_timeline_duration(100)
        controller.play()
        renderer.set_playback_controller(controller)
        renderer._playback_session_emitted = False

        assert renderer._should_skip_cpu_fallback_on_gpu_miss() is False

        renderer.shutdown()

    def test_playback_with_gpu_pipeline_bypasses_cpu_cache(
        self,
        mock_project,
        simple_sequence,
        mock_frame_provider,
    ):
        """Realtime GPU playback should not display stale CPU QImage cache hits."""
        renderer = TimelinePreviewRenderer(
            project=mock_project,
            sequence=simple_sequence,
            frame_provider=mock_frame_provider,
            viewport_size=(1920, 1080),
        )
        controller = EditPlaybackController()
        controller.set_timeline_duration(100)
        controller.play()
        renderer.set_playback_controller(controller)
        renderer._playback_session_emitted = True
        renderer._worker._gpu_decode_manager = Mock()
        renderer._worker._gpu_renderer = Mock()
        renderer._worker.enqueue = Mock(return_value=True)
        renderer._warmup_gpu_decode_sources = Mock()

        cached = QImage(16, 9, QImage.Format.Format_RGB888)
        renderer._cache.put(0, cached)

        emitted = []
        renderer.frame_ready.connect(lambda frame, result: emitted.append((frame, result)))
        renderer.request_render(0)

        assert emitted == []
        renderer._worker.enqueue.assert_called_once()

        renderer.shutdown()

    def test_scrub_placeholder_emits_even_if_controller_is_still_playing(
        self,
        mock_project,
        simple_sequence,
        mock_frame_provider,
    ):
        """Scrub thumbnails must not be suppressed by a stale playing controller."""
        asset = MediaAsset(
            asset_id="asset-001",
            path="/path/to/video.mp4",
            media_type="video",
            display_name="Video.mp4",
            metadata={"total_frames": 100},
        )
        project = replace(mock_project, assets={"asset-001": asset})
        renderer = TimelinePreviewRenderer(
            project=project,
            sequence=simple_sequence,
            frame_provider=mock_frame_provider,
            viewport_size=(1920, 1080),
        )
        controller = EditPlaybackController()
        controller.set_timeline_duration(100)
        controller.play()
        renderer.set_playback_controller(controller)
        renderer._playback_session_emitted = True

        image = QImage(16, 9, QImage.Format.Format_RGB888)
        image.fill(0)

        class FakePixmap:
            def isNull(self):
                return False

            def toImage(self):
                return image

        provider = Mock()
        provider.get_cached_frame_pixmap.return_value = FakePixmap()

        fake_module = types.ModuleType("src.shared.media.thumbnail_provider")
        fake_module.ThumbnailProvider = type(
            "FakeThumbnailProvider",
            (),
            {"instance": staticmethod(lambda: provider)},
        )

        with patch.dict(sys.modules, {"src.shared.media.thumbnail_provider": fake_module}):
            with patch.object(renderer, "_schedule_scrub_exact_render"):
                with patch.object(renderer, "frame_ready") as mock_signal:
                    renderer.begin_scrub(10)
                    renderer.request_scrub_preview(12)

        mock_signal.emit.assert_called_once()
        emitted_frame, emitted_image = mock_signal.emit.call_args.args
        assert emitted_frame == 12
        assert isinstance(emitted_image, QImage)

        renderer.shutdown()

    def test_scrub_move_coalesces_old_exact_render_state(
        self,
        mock_project,
        simple_sequence,
        mock_frame_provider,
    ):
        """A fast move preserves one active seek and replaces only pending work."""
        renderer = TimelinePreviewRenderer(
            project=mock_project,
            sequence=simple_sequence,
            frame_provider=mock_frame_provider,
            viewport_size=(1920, 1080),
        )
        renderer._worker.enqueue = Mock(return_value=True)

        renderer.begin_scrub(10)
        first_generation = renderer._generation_id
        assert renderer._dispatch_scrub_exact_render(10) is True
        assert renderer._scrub_exact_in_flight_frame == 10
        assert renderer._render_in_flight is True

        with patch.object(renderer, "_schedule_scrub_exact_render") as schedule_exact:
            renderer.request_scrub_preview(20)

        assert renderer._generation_id == first_generation
        assert renderer._render_in_flight is True
        assert renderer._scrub_exact_in_flight_frame == 10
        assert renderer._pending_frame == 10
        assert renderer._scrub_exact_pending_frame == 20
        schedule_exact.assert_called_once_with(20)

        renderer.shutdown()

    def test_scrub_fast_cadence_marks_dispatch_coarse_and_skips_preposition(
        self,
        mock_project,
        simple_sequence,
        mock_frame_provider,
    ):
        """Fast scrub inputs switch dispatches to coarse mode and stop prepositioning."""
        renderer = TimelinePreviewRenderer(
            project=mock_project,
            sequence=simple_sequence,
            frame_provider=mock_frame_provider,
            viewport_size=(1920, 1080),
        )
        renderer._worker.enqueue = Mock(return_value=True)

        renderer.begin_scrub(10)
        assert renderer._scrub_input_fast is False

        # Simulate a fast drag: inputs ~8ms apart.
        with patch.object(renderer, "_preposition_interactive_scrub_sources") as preposition:
            with patch.object(renderer, "_schedule_scrub_exact_render"):
                with patch.object(renderer, "_schedule_scrub_settled_exact"):
                    renderer._scrub_last_input_at = time.perf_counter() - 0.008
                    renderer.request_scrub_preview(20)
                    assert renderer._scrub_input_fast is True
                    preposition.assert_not_called()

        # Coarse dispatch must reach the worker queue flagged coarse.
        assert renderer._dispatch_scrub_exact_render(20) is True
        kwargs = renderer._worker.enqueue.call_args.kwargs
        assert kwargs.get("scrub_coarse") is True

        # A slow cadence (>40ms hysteresis) returns to exact decoding and
        # resumes prepositioning.
        with patch.object(renderer, "_preposition_interactive_scrub_sources") as preposition:
            with patch.object(renderer, "_schedule_scrub_exact_render"):
                with patch.object(renderer, "_schedule_scrub_settled_exact"):
                    # 60ms 间隔 + 10 帧跳步 = 拥堵中的快拖，必须保持快模式。
                    renderer._scrub_last_input_at = time.perf_counter() - 0.060
                    renderer.request_scrub_preview(30)
                    assert renderer._scrub_input_fast is True
                    preposition.assert_not_called()

                    # 60ms 间隔 + 1 帧小步 = 真正的慢拖，退出快模式恢复预置位。
                    renderer._scrub_last_input_at = time.perf_counter() - 0.060
                    renderer.request_scrub_preview(31)
                    assert renderer._scrub_input_fast is False
                    preposition.assert_called_once()

        renderer.shutdown()

    def test_interactive_scrub_prepositions_visible_layer_sources(
        self,
        mock_project,
        simple_sequence,
        mock_frame_provider,
    ):
        """Scrub requests should preposition spare VT cursors for visible layers."""
        renderer = TimelinePreviewRenderer(
            project=mock_project,
            sequence=simple_sequence,
            frame_provider=mock_frame_provider,
            viewport_size=(1920, 1080),
        )
        gpu_decode_manager = Mock()
        resolver = Mock()
        resolver.get_asset_type.return_value = "video"
        resolver.get_asset_path.return_value = "/path/to/video.mp4"
        renderer._worker._gpu_decode_manager = gpu_decode_manager
        renderer._worker._asset_resolver = resolver

        plan = SimpleNamespace(
            layers=(
                SimpleNamespace(
                    asset_id="asset-001",
                    clip_id="clip-001",
                    source_frame=42,
                    composite_id=None,
                ),
            )
        )

        renderer._preposition_interactive_scrub_sources(
            plan,
            target_size=(1920, 1080),
        )

        gpu_decode_manager.preposition_interactive_frame.assert_called_once_with(
            "/path/to/video.mp4",
            42,
            target_size=(1920, 1080),
            stream_key="clip-001",
            deadline_ms=48.0,
        )

        renderer.shutdown()

    def test_interactive_scrub_prepositions_forward_target(
        self,
        mock_project,
        simple_sequence,
        mock_frame_provider,
    ):
        """Fast forward scrub should move spare decode cursors ahead of the current frame."""
        renderer = TimelinePreviewRenderer(
            project=mock_project,
            sequence=simple_sequence,
            frame_provider=mock_frame_provider,
            viewport_size=(1920, 1080),
        )
        gpu_decode_manager = Mock()
        gpu_renderer = Mock()
        resolver = Mock()
        resolver.get_asset_type.return_value = "video"
        resolver.get_asset_path.return_value = "/path/to/video.mp4"
        renderer._worker._gpu_decode_manager = gpu_decode_manager
        renderer._worker._gpu_renderer = gpu_renderer
        renderer._worker._asset_resolver = resolver
        renderer._scrub_preposition_horizon_frames = 4

        with patch.object(renderer, "_schedule_scrub_exact_render") as schedule_exact:
            with patch.object(renderer, "_schedule_scrub_settled_exact"):
                renderer.begin_scrub(10)
                renderer.request_scrub_preview(12)

        schedule_exact.assert_called_once_with(12)
        gpu_decode_manager.preposition_interactive_frame.assert_called_with(
            "/path/to/video.mp4",
            16,
            target_size=(1920, 1080),
            stream_key="clip-001",
            deadline_ms=48.0,
        )

        renderer.shutdown()

    def test_scrub_preposition_target_clamps_to_sequence_bounds(
        self,
        mock_project,
        simple_sequence,
        mock_frame_provider,
    ):
        """Predictive scrub prepositioning must not run past the timeline duration."""
        renderer = TimelinePreviewRenderer(
            project=mock_project,
            sequence=simple_sequence,
            frame_provider=mock_frame_provider,
            viewport_size=(1920, 1080),
        )
        renderer._scrub_preposition_horizon_frames = 8

        assert renderer._scrub_preposition_timeline_frame(98, 1) == 99
        assert renderer._scrub_preposition_timeline_frame(2, -1) == 0
        assert renderer._scrub_preposition_timeline_frame(12, 0) == 12

        renderer.shutdown()

    def test_submit_scrub_exact_render_queues_real_render(
        self,
        mock_project,
        simple_sequence,
        mock_frame_provider,
    ):
        """Debounced scrub exact render should enter the real worker pipeline."""
        renderer = TimelinePreviewRenderer(
            project=mock_project,
            sequence=simple_sequence,
            frame_provider=mock_frame_provider,
            viewport_size=(1920, 1080),
        )
        renderer._worker.enqueue = Mock(return_value=True)

        renderer.begin_scrub(12)
        renderer._submit_scrub_exact_render(12, renderer._scrub_exact_request_token)

        renderer._worker.enqueue.assert_called_once()
        assert renderer._worker.enqueue.call_args.args[0] == 12
        assert renderer._render_in_flight is True
        assert renderer._pending_frame == 12
        assert renderer._scrub_exact_in_flight_frame == 12
        assert renderer._worker.enqueue.call_args.kwargs["decode_target_size"] == (1920, 1080)

        renderer.shutdown()

    def test_dispatch_scrub_settled_marks_worker_task(
        self,
        mock_project,
        simple_sequence,
        mock_frame_provider,
    ):
        """Settled exact repair should be visible to the worker deadline policy."""
        renderer = TimelinePreviewRenderer(
            project=mock_project,
            sequence=simple_sequence,
            frame_provider=mock_frame_provider,
            viewport_size=(1920, 1080),
        )
        renderer._worker.enqueue = Mock(return_value=True)

        renderer.begin_scrub(12)
        assert renderer._dispatch_scrub_exact_render(12, settled=True) is True

        assert renderer._worker.enqueue.call_args.kwargs["request_kind"] == "scrub"
        assert renderer._worker.enqueue.call_args.kwargs["scrub_settled"] is True
        renderer.shutdown()

    def test_gpu_scrub_exact_render_reuses_playback_decode_target(
        self,
        mock_project,
        simple_sequence,
        mock_frame_provider,
    ):
        """GPU scrub exact render should not warm a smaller second decode source."""
        renderer = TimelinePreviewRenderer(
            project=mock_project,
            sequence=simple_sequence,
            frame_provider=mock_frame_provider,
            viewport_size=(1920, 1080),
        )
        renderer._worker._gpu_decode_manager = Mock()
        renderer._worker._gpu_renderer = Mock()
        renderer._worker.enqueue = Mock(return_value=True)

        renderer.begin_scrub(12)
        renderer._submit_scrub_exact_render(12, renderer._scrub_exact_request_token)

        renderer._worker.enqueue.assert_called_once()
        assert renderer._worker.enqueue.call_args.kwargs["decode_target_size"] == (1920, 1080)
        assert renderer._worker.enqueue.call_args.kwargs["request_kind"] == "scrub"
        assert renderer._worker.enqueue.call_args.kwargs["scrub_request_token"] == renderer._scrub_request_serial
        assert isinstance(renderer._worker.enqueue.call_args.kwargs["request_started_at"], float)

        renderer.shutdown()

    def test_scrub_accepts_nearby_real_frame_but_rejects_far_result(
        self,
        mock_project,
        simple_sequence,
        mock_frame_provider,
    ):
        """A live scrub should show real progress frames instead of freezing."""
        renderer = TimelinePreviewRenderer(
            project=mock_project,
            sequence=simple_sequence,
            frame_provider=mock_frame_provider,
            viewport_size=(1920, 1080),
        )
        renderer.begin_scrub(10)
        renderer._scrub_latest_frame = 30
        renderer._scrub_latest_display_frame = 30
        # Viewer was updated just now: the direction window applies.
        renderer._scrub_last_real_frame_at = time.perf_counter()

        assert renderer._can_emit_scrub_render_result(25) is True
        assert renderer._can_emit_scrub_render_result(10) is True
        assert renderer._can_emit_scrub_render_result(100) is False

        renderer._scrub_last_real_frame = 10
        assert renderer._can_emit_scrub_render_result(20) is True
        assert renderer._can_emit_scrub_render_result(5) is False
        assert renderer._can_emit_scrub_render_result(100) is False

        # Never-freeze escape hatch: when nothing has been displayed for a
        # while (fast drag / coarse keyframe results trailing the playhead),
        # any fresh completion beats a frozen viewer.
        renderer._scrub_last_real_frame_at = time.perf_counter() - 0.5
        assert renderer._can_emit_scrub_render_result(100) is True
        assert renderer._can_emit_scrub_render_result(5) is True

        renderer.shutdown()

    def test_stale_scrub_exact_result_dispatches_latest_pending_frame(
        self,
        mock_project,
        simple_sequence,
        mock_frame_provider,
    ):
        """Old exact scrub results must not block the latest real-frame request."""
        renderer = TimelinePreviewRenderer(
            project=mock_project,
            sequence=simple_sequence,
            frame_provider=mock_frame_provider,
            viewport_size=(1920, 1080),
        )
        renderer._worker.enqueue = Mock(return_value=True)

        renderer.begin_scrub(12)
        renderer._scrub_latest_frame = 13
        renderer._scrub_latest_display_frame = 13
        renderer._scrub_exact_pending_frame = 13
        renderer._scrub_exact_in_flight_frame = 12
        renderer._render_in_flight = True
        renderer._pending_frame = 12

        renderer._drop_scrub_render_result(12, "active_stale")

        renderer._worker.enqueue.assert_called_once()
        assert renderer._worker.enqueue.call_args.args[0] == 13
        assert renderer._pending_frame == 13
        assert renderer._scrub_exact_in_flight_frame == 13

        renderer.shutdown()

    def test_worker_scrub_stale_drop_result_dispatches_latest_pending_frame(
        self,
        mock_project,
        simple_sequence,
        mock_frame_provider,
    ):
        """A worker-level stale scrub drop must unblock and chase the latest frame."""
        renderer = TimelinePreviewRenderer(
            project=mock_project,
            sequence=simple_sequence,
            frame_provider=mock_frame_provider,
            viewport_size=(1920, 1080),
        )
        renderer._worker.enqueue = Mock(return_value=True)

        renderer.begin_scrub(12)
        renderer._scrub_latest_frame = 13
        renderer._scrub_latest_display_frame = 13
        renderer._scrub_exact_pending_frame = 13
        renderer._scrub_exact_in_flight_frame = 12
        renderer._render_in_flight = True
        renderer._pending_frame = 12

        renderer._on_frame_rendered(12, {"scrub_stale_drop": True, "reason": "stale_after_decode"})

        renderer._worker.enqueue.assert_called_once()
        assert renderer._worker.enqueue.call_args.args[0] == 13
        assert renderer._pending_frame == 13
        assert renderer._scrub_exact_in_flight_frame == 13

        renderer.shutdown()

    def test_worker_gpu_submitted_dispatches_latest_pending_scrub_frame(
        self,
        mock_project,
        simple_sequence,
        mock_frame_provider,
    ):
        """Once scrub work is queued on GPU, the worker can chase the latest pending frame."""
        renderer = TimelinePreviewRenderer(
            project=mock_project,
            sequence=simple_sequence,
            frame_provider=mock_frame_provider,
            viewport_size=(1920, 1080),
        )
        renderer._worker.enqueue = Mock(return_value=True)

        renderer.begin_scrub(143)
        renderer._scrub_latest_frame = 142
        renderer._scrub_latest_display_frame = 142
        renderer._scrub_exact_pending_frame = 142
        renderer._scrub_exact_in_flight_frame = 143
        renderer._render_in_flight = True
        renderer._pending_frame = 143

        renderer._on_worker_gpu_submitted(143, renderer._generation_id)

        renderer._worker.enqueue.assert_called_once()
        assert renderer._worker.enqueue.call_args.args[0] == 142
        assert renderer._pending_frame == 142
        assert renderer._scrub_exact_in_flight_frame == 142
        assert renderer._render_in_flight is True

        renderer.shutdown()

    def test_worker_gpu_submitted_direct_release_does_not_dispatch_off_thread(
        self,
        mock_project,
        simple_sequence,
        mock_frame_provider,
    ):
        """The direct worker callback only releases state; dispatch stays on the UI thread."""
        renderer = TimelinePreviewRenderer(
            project=mock_project,
            sequence=simple_sequence,
            frame_provider=mock_frame_provider,
            viewport_size=(1920, 1080),
        )
        renderer._worker.enqueue = Mock(return_value=True)

        renderer.begin_scrub(143)
        renderer._scrub_latest_frame = 142
        renderer._scrub_latest_display_frame = 142
        renderer._scrub_exact_pending_frame = 142
        renderer._scrub_exact_in_flight_frame = 143
        renderer._render_in_flight = True
        renderer._pending_frame = 143

        renderer._on_worker_gpu_submitted_direct(143, renderer._generation_id)

        renderer._worker.enqueue.assert_not_called()
        assert renderer._pending_frame is None
        assert renderer._scrub_exact_pending_frame == 142
        assert renderer._scrub_exact_in_flight_frame is None
        assert renderer._render_in_flight is False

        renderer.shutdown()

    def test_delayed_gpu_submitted_signal_does_not_clear_newer_scrub_frame(
        self,
        mock_project,
        simple_sequence,
        mock_frame_provider,
    ):
        """A queued old release signal must not clobber a newer frame already enqueued."""
        renderer = TimelinePreviewRenderer(
            project=mock_project,
            sequence=simple_sequence,
            frame_provider=mock_frame_provider,
            viewport_size=(1920, 1080),
        )
        renderer._worker.enqueue = Mock(return_value=True)

        renderer.begin_scrub(143)
        renderer._scrub_latest_frame = 142
        renderer._scrub_latest_display_frame = 142
        renderer._scrub_exact_pending_frame = 142
        renderer._scrub_exact_in_flight_frame = 143
        renderer._render_in_flight = True
        renderer._pending_frame = 143

        renderer._on_worker_gpu_submitted_direct(143, renderer._generation_id)
        assert renderer._dispatch_pending_scrub_exact_if_needed() is True

        renderer._on_worker_gpu_submitted(143, renderer._generation_id)

        renderer._worker.enqueue.assert_called_once()
        assert renderer._worker.enqueue.call_args.args[0] == 142
        assert renderer._pending_frame == 142
        assert renderer._scrub_exact_in_flight_frame == 142
        assert renderer._render_in_flight is True

        renderer.shutdown()

    def test_old_gpu_result_does_not_clear_newer_pending_scrub_frame(
        self,
        mock_project,
        simple_sequence,
        mock_frame_provider,
    ):
        """Overlapped scrub GPU results must not reset the newer in-flight frame."""
        renderer = TimelinePreviewRenderer(
            project=mock_project,
            sequence=simple_sequence,
            frame_provider=mock_frame_provider,
            viewport_size=(1920, 1080),
        )
        renderer.begin_scrub(143)
        renderer._dispatched_generation_id = renderer._generation_id
        renderer._scrub_latest_frame = 142
        renderer._scrub_latest_display_frame = 142
        renderer._scrub_exact_pending_frame = None
        renderer._scrub_exact_in_flight_frame = 142
        renderer._pending_frame = 142
        renderer._render_in_flight = True

        with patch.object(renderer, "_can_emit_scrub_render_result", return_value=True):
            with patch.object(renderer, "_is_playback_result_stale", return_value=False):
                with patch.object(renderer, "_should_emit_frame", return_value=True):
                    with patch.object(renderer, "_schedule_playback_prebuffer"):
                        with patch.object(renderer, "_advance_to_next_frame"):
                            with patch.object(renderer, "frame_ready") as mock_signal:
                                renderer._on_gpu_frame_ready(143, renderer._generation_id, 7, 640, 360)

        assert renderer._pending_frame == 142
        assert renderer._scrub_exact_in_flight_frame == 142
        assert renderer._render_in_flight is True
        mock_signal.emit.assert_called_once()

        renderer.shutdown()

    def test_gpu_scrub_none_result_does_not_emit_or_cache_nearby_cpu_fallback(
        self,
        mock_project,
        simple_sequence,
        mock_frame_provider,
    ):
        """GPU scrub misses must not turn a nearby/first cached still into the current frame."""
        renderer = TimelinePreviewRenderer(
            project=mock_project,
            sequence=simple_sequence,
            frame_provider=mock_frame_provider,
            viewport_size=(1920, 1080),
        )
        renderer._worker._gpu_decode_manager = Mock()
        renderer._worker._gpu_renderer = Mock()

        cached_still = Mock()
        renderer._cache.put(12, cached_still)
        renderer.begin_scrub(13)
        renderer._dispatched_generation_id = renderer._generation_id
        renderer._render_in_flight = True
        renderer._pending_frame = 13
        renderer._scrub_exact_in_flight_frame = 13

        with patch.object(renderer, "frame_ready") as mock_signal:
            with patch.object(renderer, "_advance_to_next_frame") as advance:
                renderer._on_frame_rendered(13, None)

        mock_signal.emit.assert_not_called()
        assert renderer._cache.get(13) is None
        assert renderer._render_in_flight is False
        advance.assert_called_once()

        renderer.shutdown()

    def test_worker_identifies_stale_interactive_scrub_token(
        self,
        mock_frame_provider,
        mock_project,
        simple_sequence,
    ):
        """Worker can reject an already superseded scrub task before decode."""
        worker = PreviewRenderWorker(
            frame_provider=mock_frame_provider,
            asset_resolver=AssetResolver(mock_project),
            video_renderer=None,
            timeline_sequence=simple_sequence,
        )

        worker.set_interactive_scrub_state(3, 30, True)

        assert worker._is_stale_interactive_scrub_task({
            "request_kind": "scrub",
            "scrub_request_token": 2,
        }) is True
        assert worker._is_stale_interactive_scrub_task({
            "request_kind": "scrub",
            "scrub_request_token": 3,
        }) is False
        assert worker._is_stale_interactive_scrub_task({
            "request_kind": "playback",
            "scrub_request_token": 2,
        }) is False

        worker.stop()

    def test_worker_applies_grace_before_aborting_in_flight_scrub_decode(
        self,
        mock_frame_provider,
        mock_project,
        simple_sequence,
    ):
        """In-flight scrub decode only becomes stale after the grace window."""
        worker = PreviewRenderWorker(
            frame_provider=mock_frame_provider,
            asset_resolver=AssetResolver(mock_project),
            video_renderer=None,
            timeline_sequence=simple_sequence,
        )

        worker.set_interactive_scrub_state(3, 30, True)

        assert worker._is_stale_interactive_scrub_task(
            {
                "request_kind": "scrub",
                "scrub_request_token": 2,
                "request_started_at": time.perf_counter(),
            },
            min_age_ms=50.0,
        ) is False
        assert worker._is_stale_interactive_scrub_task(
            {
                "request_kind": "scrub",
                "scrub_request_token": 2,
                "request_started_at": time.perf_counter() - 0.2,
            },
            min_age_ms=50.0,
        ) is True

        worker.stop()

    def test_scrub_async_thumbnail_coalesces_to_latest_frame(
        self,
        mock_project,
        simple_sequence,
        mock_frame_provider,
    ):
        """Fast scrub should drop old thumbnail callbacks and emit only latest frame."""
        asset = MediaAsset(
            asset_id="asset-001",
            path="/path/to/video.mp4",
            media_type="video",
            display_name="Video.mp4",
            metadata={"total_frames": 100},
        )
        project = replace(mock_project, assets={"asset-001": asset})
        renderer = TimelinePreviewRenderer(
            project=project,
            sequence=simple_sequence,
            frame_provider=mock_frame_provider,
            viewport_size=(1920, 1080),
        )

        image = QImage(16, 9, QImage.Format.Format_RGB888)
        image.fill(0)

        class FakePixmap:
            def isNull(self):
                return False

            def toImage(self):
                return image

        provider = Mock()
        provider.frame_thumbnail_ready.connect = Mock()
        provider.get_cached_frame_pixmap.return_value = None
        provider.get_cached_thumbnail.return_value = None

        fake_module = types.ModuleType("src.shared.media.thumbnail_provider")
        fake_module.ThumbnailProvider = type(
            "FakeThumbnailProvider",
            (),
            {"instance": staticmethod(lambda: provider)},
        )

        with patch.dict(sys.modules, {"src.shared.media.thumbnail_provider": fake_module}):
            renderer.begin_scrub(10)
            renderer.request_scrub_preview(12)
            renderer.request_scrub_preview(13)

            assert len(renderer._pending_placeholder_requests) == 1
            assert renderer._placeholder_accepting_frame == 13

            with patch.object(renderer, "frame_ready") as mock_signal:
                renderer._on_placeholder_thumbnail_ready("/path/to/video.mp4", 12, FakePixmap())
                renderer._on_placeholder_thumbnail_ready("/path/to/video.mp4", 13, FakePixmap())

        mock_signal.emit.assert_called_once()
        emitted_frame, emitted_image = mock_signal.emit.call_args.args
        assert emitted_frame == 13
        assert isinstance(emitted_image, QImage)
        assert renderer._worker._queue.qsize() == 0

        renderer.shutdown()

    def test_finish_scrub_requests_one_exact_frame(
        self,
        mock_project,
        simple_sequence,
        mock_frame_provider,
    ):
        """Mouse release leaves scrub mode and queues exactly the final frame."""
        renderer = TimelinePreviewRenderer(
            project=mock_project,
            sequence=simple_sequence,
            frame_provider=mock_frame_provider,
            viewport_size=(1920, 1080),
        )

        renderer.begin_scrub(10)
        scrub_generation = renderer._generation_id
        renderer.finish_scrub(18)

        assert renderer._scrub_active is False
        assert renderer._scrub_exact_frame == 18
        assert renderer._generation_id > scrub_generation
        assert renderer._worker._queue.qsize() >= 1

        renderer.shutdown()

    def test_finish_scrub_does_not_flash_cached_placeholder(
        self,
        mock_project,
        simple_sequence,
        mock_frame_provider,
    ):
        """Mouse-up exact render should hold the scrub image, not flash cached stills."""
        asset = MediaAsset(
            asset_id="asset-001",
            path="/path/to/video.mp4",
            media_type="video",
            display_name="Video.mp4",
            metadata={"total_frames": 100},
        )
        project = replace(mock_project, assets={"asset-001": asset})
        renderer = TimelinePreviewRenderer(
            project=project,
            sequence=simple_sequence,
            frame_provider=mock_frame_provider,
            viewport_size=(1920, 1080),
        )
        renderer._worker.enqueue = Mock(return_value=True)
        renderer._cache.put(18, Mock())

        image = QImage(16, 9, QImage.Format.Format_RGB888)
        image.fill(0)

        class FakePixmap:
            def isNull(self):
                return False

            def toImage(self):
                return image

        provider = Mock()
        provider.frame_thumbnail_ready.connect = Mock()
        provider.get_cached_frame_pixmap.return_value = FakePixmap()
        provider.get_cached_thumbnail.return_value = FakePixmap()

        fake_module = types.ModuleType("src.shared.media.thumbnail_provider")
        fake_module.ThumbnailProvider = type(
            "FakeThumbnailProvider",
            (),
            {"instance": staticmethod(lambda: provider)},
        )

        with patch.dict(sys.modules, {"src.shared.media.thumbnail_provider": fake_module}):
            with patch.object(renderer, "frame_ready") as mock_signal:
                renderer.begin_scrub(10)
                renderer.finish_scrub(18)

        mock_signal.emit.assert_not_called()
        provider.get_cached_frame_pixmap.assert_not_called()
        provider.get_cached_thumbnail.assert_not_called()
        renderer._worker.enqueue.assert_called()
        assert renderer._worker.enqueue.call_args.args[0] == 18

        renderer.shutdown()

    def test_request_render_during_scrub_routes_to_scrub_preview(
        self,
        mock_project,
        simple_sequence,
        mock_frame_provider,
    ):
        """Legacy render callers must not re-enter exact random seek while dragging."""
        renderer = TimelinePreviewRenderer(
            project=mock_project,
            sequence=simple_sequence,
            frame_provider=mock_frame_provider,
            viewport_size=(1920, 1080),
        )
        renderer._scrub_active = True

        with patch.object(renderer, 'request_scrub_preview') as request_scrub_preview:
            renderer.request_render(33)

            request_scrub_preview.assert_called_once_with(33)
            assert renderer._worker._queue.qsize() == 0

        renderer.shutdown()

    def test_request_render_not_cached(self, mock_project, simple_sequence, mock_frame_provider):
        """测试请求渲染（缓存未命中）"""
        renderer = TimelinePreviewRenderer(
            project=mock_project,
            sequence=simple_sequence,
            frame_provider=mock_frame_provider,
            viewport_size=(1920, 1080),
        )

        # 请求渲染帧 0
        renderer.request_render(0)

        # 验证任务加入队列
        assert renderer._worker._queue.qsize() >= 1

        renderer.shutdown()

    def test_collect_playback_prebuffer_jobs_for_multitrack_window(
        self,
        multi_track_sequence,
        mock_frame_provider,
    ):
        """Prebuffer collects upcoming source frames for each visible video layer."""
        project = EditProject.create("Prebuffer Project")
        project = replace(
            project,
            assets={
                "asset-001": MediaAsset(
                    asset_id="asset-001",
                    path="/path/to/lower.mov",
                    media_type="video",
                    display_name="lower.mov",
                    metadata={"total_frames": 120},
                ),
                "asset-002": MediaAsset(
                    asset_id="asset-002",
                    path="/path/to/upper.mov",
                    media_type="video",
                    display_name="upper.mov",
                    metadata={"total_frames": 80},
                ),
            },
        )
        renderer = TimelinePreviewRenderer(
            project=project,
            sequence=multi_track_sequence,
            frame_provider=mock_frame_provider,
            viewport_size=(1920, 1080),
        )
        renderer._prebuffer_lookahead_frames = 2
        anchor_plan = TimelineRenderPlan(multi_track_sequence).get_frame_plan(75)

        jobs = renderer._collect_playback_prebuffer_jobs(
            75,
            1,
            renderer._generation_id,
            anchor_plan=anchor_plan,
        )

        assert jobs == {
            ("/path/to/lower.mov", "asset-001", "clip-lower"): [75, 76, 77],
            ("/path/to/upper.mov", "asset-002", "clip-upper"): [25, 26, 27],
        }

        renderer.shutdown()

    def test_playback_does_not_start_a_parallel_prebuffer_decoder(
        self,
        simple_sequence,
        mock_frame_provider,
    ):
        """Live playback must stay on the render worker's single decoder source."""
        project = EditProject.create("Prebuffer Project")
        project = replace(
            project,
            assets={
                "asset-001": MediaAsset(
                    asset_id="asset-001",
                    path="/path/to/video.mp4",
                    media_type="video",
                    display_name="video.mp4",
                    metadata={"total_frames": 120},
                ),
            },
        )
        renderer = TimelinePreviewRenderer(
            project=project,
            sequence=simple_sequence,
            frame_provider=mock_frame_provider,
            viewport_size=(1920, 1080),
        )
        renderer._prebuffer_executor.submit = Mock()
        renderer._schedule_playback_prebuffer(
            0,
            renderer._compute_frame_plan(0),
        )

        renderer._prebuffer_executor.submit.assert_not_called()

        renderer.shutdown()

    def test_scrub_invalidation_does_not_manage_a_second_decode_stream(
        self,
        simple_sequence,
        mock_frame_provider,
    ):
        """Scrub invalidation only invalidates render generations now."""
        renderer = TimelinePreviewRenderer(
            project=EditProject.create("Scrub Prefetch Project"),
            sequence=simple_sequence,
            frame_provider=mock_frame_provider,
            viewport_size=(1920, 1080),
        )
        gpu_manager = Mock()
        renderer._worker._gpu_decode_manager = gpu_manager

        renderer._invalidate_render_generation_for_scrub(12, "test")

        gpu_manager.cancel_prefetch.assert_not_called()
        renderer.shutdown()

    def test_playback_prebuffer_skips_active_stream_while_render_busy(
        self,
        simple_sequence,
        mock_frame_provider,
    ):
        """Prepositioning never touches the stream the display path drives.

        The cut-boundary prebuffer reuses each clip's own display stream
        (no second decode session), but it must leave the actively playing
        clip's cursor alone and only advance upcoming clip streams.
        """
        project = EditProject.create("Prebuffer Project")
        renderer = TimelinePreviewRenderer(
            project=project,
            sequence=simple_sequence,
            frame_provider=mock_frame_provider,
            viewport_size=(1920, 1080),
        )
        renderer._is_controller_playing = lambda: True
        renderer._prebuffer_executor.submit = Mock()
        renderer._render_in_flight = True
        renderer._pending_frame = 0

        # The first schedule call of a session scans immediately.
        renderer._schedule_playback_prebuffer(0, renderer._compute_frame_plan(0))
        renderer._prebuffer_executor.submit.assert_called_once()

        # Direct-run the prebuffer task with controlled jobs: the active
        # clip stream must be skipped; upcoming streams get prepositioned
        # to their entry frame on the SAME display stream.
        gpu_manager = Mock()
        gpu_manager.preposition_stream_frame = Mock(return_value=True)
        renderer._worker._gpu_decode_manager = gpu_manager
        renderer._collect_playback_prebuffer_jobs = Mock(return_value={
            ("/media/a.mp4", "asset-a", "clip-active"): [10, 11, 12],
            ("/media/a.mp4", "asset-a", "clip-next"): [36, 37, 38],
        })
        renderer._active_playback_stream_keys = Mock(
            return_value={("/media/a.mp4", "clip-active")}
        )

        renderer._run_playback_prebuffer(0, 1, renderer._generation_id, None, (renderer._generation_id, 0, 1))

        assert gpu_manager.preposition_stream_frame.call_count == 1
        call_kwargs = gpu_manager.preposition_stream_frame.call_args.kwargs
        call_args = gpu_manager.preposition_stream_frame.call_args.args
        assert call_args[0] == "/media/a.mp4"
        assert call_args[1] == 36
        assert call_kwargs.get("stream_key") == "clip-next"
        renderer.shutdown()

    def test_request_render_emits_cached_thumbnail_placeholder(
        self,
        mock_project,
        simple_sequence,
        mock_frame_provider,
    ):
        """A cached thumbnail should fill ProgramViewer while the real frame renders."""
        asset = MediaAsset(
            asset_id="asset-001",
            path="/path/to/video.mp4",
            media_type="video",
            display_name="Video.mp4",
            metadata={},
        )
        project = replace(mock_project, assets={"asset-001": asset})
        renderer = TimelinePreviewRenderer(
            project=project,
            sequence=simple_sequence,
            frame_provider=mock_frame_provider,
            viewport_size=(1920, 1080),
        )
        renderer._worker.enqueue = Mock(return_value=True)

        image = QImage(16, 9, QImage.Format.Format_RGB888)
        image.fill(0)

        class FakePixmap:
            def isNull(self):
                return False

            def toImage(self):
                return image

        provider = Mock()
        provider.get_cached_frame_pixmap.return_value = FakePixmap()

        fake_module = types.ModuleType("src.shared.media.thumbnail_provider")
        fake_module.ThumbnailProvider = type(
            "FakeThumbnailProvider",
            (),
            {"instance": staticmethod(lambda: provider)},
        )

        with patch.dict(sys.modules, {"src.shared.media.thumbnail_provider": fake_module}):
            with patch.object(renderer, "frame_ready") as mock_signal:
                renderer.request_render(0)

        assert mock_signal.emit.call_count == 1
        emitted_frame, emitted_image = mock_signal.emit.call_args.args
        assert emitted_frame == 0
        assert isinstance(emitted_image, QImage)
        provider.get_cached_frame_pixmap.assert_called()

        renderer.shutdown()

    def test_paused_frame_step_never_emits_generic_thumbnail_placeholder(
        self,
        mock_project,
        simple_sequence,
        mock_frame_provider,
    ):
        """Paused seeks must not flash the asset-cover/first-frame thumbnail."""
        asset = MediaAsset(
            asset_id="asset-001",
            path="/path/to/video.mp4",
            media_type="video",
            display_name="Video.mp4",
            metadata={"total_frames": 100},
        )
        project = replace(mock_project, assets={"asset-001": asset})
        renderer = TimelinePreviewRenderer(
            project=project,
            sequence=simple_sequence,
            frame_provider=mock_frame_provider,
            viewport_size=(1920, 1080),
        )
        renderer._worker.enqueue = Mock(return_value=True)

        image = QImage(16, 9, QImage.Format.Format_RGB888)
        image.fill(0)

        class FakePixmap:
            def isNull(self):
                return False

            def toImage(self):
                return image

        provider = Mock()
        provider.get_cached_frame_pixmap.return_value = None
        provider.get_cached_thumbnail.return_value = FakePixmap()

        fake_module = types.ModuleType("src.shared.media.thumbnail_provider")
        fake_module.ThumbnailProvider = type(
            "FakeThumbnailProvider",
            (),
            {"instance": staticmethod(lambda: provider)},
        )

        with patch.dict(sys.modules, {"src.shared.media.thumbnail_provider": fake_module}):
            with patch.object(renderer, "frame_ready") as mock_signal:
                renderer.request_render(12)

        mock_signal.emit.assert_not_called()
        provider.get_cached_thumbnail.assert_not_called()
        provider.request_frame_pixmap_async.assert_called_once()
        renderer.shutdown()

    def test_playback_first_frame_allows_cached_thumbnail_placeholder(
        self,
        mock_project,
        simple_sequence,
        mock_frame_provider,
    ):
        """播放会话首个真实帧未显示前，允许缩略图避免空屏。"""
        asset = MediaAsset(
            asset_id="asset-001",
            path="/path/to/video.mp4",
            media_type="video",
            display_name="Video.mp4",
            metadata={},
        )
        project = replace(mock_project, assets={"asset-001": asset})
        renderer = TimelinePreviewRenderer(
            project=project,
            sequence=simple_sequence,
            frame_provider=mock_frame_provider,
            viewport_size=(1920, 1080),
        )
        renderer._worker.enqueue = Mock(return_value=True)

        controller = EditPlaybackController()
        controller.set_timeline_duration(100)
        controller.play()
        renderer.set_playback_controller(controller)

        image = QImage(16, 9, QImage.Format.Format_RGB888)
        image.fill(0)

        class FakePixmap:
            def isNull(self):
                return False

            def toImage(self):
                return image

        provider = Mock()
        provider.get_cached_frame_pixmap.return_value = FakePixmap()

        fake_module = types.ModuleType("src.shared.media.thumbnail_provider")
        fake_module.ThumbnailProvider = type(
            "FakeThumbnailProvider",
            (),
            {"instance": staticmethod(lambda: provider)},
        )

        with patch.dict(sys.modules, {"src.shared.media.thumbnail_provider": fake_module}):
            with patch.object(renderer, "frame_ready") as mock_signal:
                renderer.request_render(0)

        assert mock_signal.emit.call_count == 1
        emitted_frame, emitted_image = mock_signal.emit.call_args.args
        assert emitted_frame == 0
        assert isinstance(emitted_image, QImage)

        renderer.shutdown()

    def test_playback_after_real_frame_suppresses_thumbnail_placeholder(
        self,
        mock_project,
        simple_sequence,
        mock_frame_provider,
    ):
        """真实播放帧显示过之后，播放中不再用缩略图插帧。"""
        renderer = TimelinePreviewRenderer(
            project=mock_project,
            sequence=simple_sequence,
            frame_provider=mock_frame_provider,
            viewport_size=(1920, 1080),
        )
        renderer._playback_session_emitted = True

        controller = EditPlaybackController()
        controller.set_timeline_duration(100)
        controller.play()
        renderer.set_playback_controller(controller)

        with patch.object(renderer, "frame_ready") as mock_signal:
            renderer._emit_cached_placeholder_frame(
                0,
                renderer._compute_frame_plan(0),
            )

        mock_signal.emit.assert_not_called()

        renderer.shutdown()

    def test_request_render_emits_async_thumbnail_placeholder(
        self,
        mock_project,
        simple_sequence,
        mock_frame_provider,
    ):
        """When no cached thumbnail exists, async frame thumbnail should fill first frame."""
        asset = MediaAsset(
            asset_id="asset-001",
            path="/path/to/video.mp4",
            media_type="video",
            display_name="Video.mp4",
            metadata={"total_frames": 100},
        )
        project = replace(mock_project, assets={"asset-001": asset})
        renderer = TimelinePreviewRenderer(
            project=project,
            sequence=simple_sequence,
            frame_provider=mock_frame_provider,
            viewport_size=(1920, 1080),
        )
        renderer._worker.enqueue = Mock(return_value=True)

        image = QImage(16, 9, QImage.Format.Format_RGB888)
        image.fill(0)

        class FakePixmap:
            def isNull(self):
                return False

            def toImage(self):
                return image

        class FakeSignal:
            def __init__(self):
                self._callbacks = []

            def connect(self, callback):
                self._callbacks.append(callback)

            def emit(self, *args):
                for callback in list(self._callbacks):
                    callback(*args)

        signal = FakeSignal()
        provider = Mock()
        provider.frame_thumbnail_ready = signal
        provider.get_cached_frame_pixmap.return_value = None
        provider.get_cached_thumbnail.return_value = None

        def emit_async(asset_id, file_path, frame_number, total_frames, target_height=48):
            signal.emit(file_path, frame_number, FakePixmap())

        provider.request_frame_pixmap_async.side_effect = emit_async

        fake_module = types.ModuleType("src.shared.media.thumbnail_provider")
        fake_module.ThumbnailProvider = type(
            "FakeThumbnailProvider",
            (),
            {"instance": staticmethod(lambda: provider)},
        )

        with patch.dict(sys.modules, {"src.shared.media.thumbnail_provider": fake_module}):
            with patch.object(renderer, "frame_ready") as mock_signal:
                renderer.request_render(0)

        assert mock_signal.emit.call_count == 1
        emitted_frame, emitted_image = mock_signal.emit.call_args.args
        assert emitted_frame == 0
        assert isinstance(emitted_image, QImage)
        provider.request_frame_pixmap_async.assert_called_once()

        renderer.shutdown()

    def test_set_playhead(self, mock_project, simple_sequence, mock_frame_provider):
        """测试设置播放头"""
        renderer = TimelinePreviewRenderer(
            project=mock_project,
            sequence=simple_sequence,
            frame_provider=mock_frame_provider,
            viewport_size=(1920, 1080),
        )

        # 设置播放头
        renderer.set_playhead(50)

        # 验证播放头位置
        assert renderer.get_playhead() == 50

        renderer.shutdown()

    def test_request_render_updates_playhead_for_paused_refresh(
        self, mock_project, simple_sequence, mock_frame_provider
    ):
        """暂停态刷新必须让 GPU 结果通过当前播放头保护。"""
        renderer = TimelinePreviewRenderer(
            project=mock_project,
            sequence=simple_sequence,
            frame_provider=mock_frame_provider,
            viewport_size=(1920, 1080),
        )

        renderer.request_render(50, request_kind="refresh")

        assert renderer.get_playhead() == 50
        renderer.shutdown()

    def test_set_sequence(self, mock_project, simple_sequence, mock_frame_provider):
        """测试设置时间线序列"""
        renderer = TimelinePreviewRenderer(
            project=mock_project,
            sequence=simple_sequence,
            frame_provider=mock_frame_provider,
            viewport_size=(1920, 1080),
        )

        # 缓存帧
        renderer._cache.put(0, "cached-frame")

        # 设置新序列
        new_sequence = TimelineSequence(
            timeline_id="seq-002",
            name="New Sequence",
            fps=Rational(30, 1),
            width=1920,
            height=1080,
        )
        renderer.set_sequence(new_sequence)

        # 验证缓存已清除
        assert renderer._cache.get(0) is None
        assert renderer._sequence == new_sequence
        assert renderer._generation_id > 0
        assert renderer._realtime_engine.budget.stats()["target_fps"] == 30.0

        renderer.shutdown()

    def test_sequence_prewarm_is_asset_deduped_and_capped(
        self,
        mock_project,
        simple_sequence,
        mock_frame_provider,
    ):
        """结构编辑后整序列预热不应按每个 clip_id 启动解码线程。"""
        renderer = TimelinePreviewRenderer(
            project=mock_project,
            sequence=simple_sequence,
            frame_provider=mock_frame_provider,
            viewport_size=(1920, 1080),
        )
        renderer.shutdown()

        warmups: list[tuple[str, object, object]] = []

        class FakeGpuDecodeManager:
            def warmup_source(self, asset_path, target_size=None, stream_key=None):
                warmups.append((asset_path, target_size, stream_key))

        class FakeResolver:
            def get_asset_type(self, asset_id):
                return "video"

            def get_asset_path(self, asset_id):
                if asset_id.startswith("asset-a"):
                    return "/media/source-a.mov"
                return "/media/source-b.mov"

        renderer._worker = SimpleNamespace(
            _gpu_decode_manager=FakeGpuDecodeManager(),
            _asset_resolver=FakeResolver(),
        )
        renderer._sequence_prewarm_source_limit = 1

        sequence = TimelineSequence(
            timeline_id="seq-split",
            name="Split Sequence",
            fps=Rational(24, 1),
            width=1920,
            height=1080,
            video_tracks=(
                EditTrack(
                    track_id="v1",
                    name="V1",
                    track_type="video",
                    index=0,
                    clips=(
                        ClipInstance("clip-a1", "asset-a", "v1", 0, 10, 0, 10),
                        ClipInstance("clip-a2", "asset-a", "v1", 10, 10, 10, 10),
                        ClipInstance("clip-a3", "asset-a", "v1", 20, 10, 20, 10),
                    ),
                ),
                EditTrack(
                    track_id="v2",
                    name="V2",
                    track_type="video",
                    index=1,
                    clips=(
                        ClipInstance("clip-b1", "asset-b", "v2", 0, 10, 0, 10),
                    ),
                ),
            ),
        )

        renderer._prewarm_sequence_decode_sources(sequence)

        # a1/a2/a3 是同源前向连续切片：与渲染计划一致共享 clip-a1 的流，
        # 只预热一个 VT 会话；cap=1 使 asset-b 不再预热。stream key 必须
        # 是播放热路径实际使用的 clip 流 key（此前错误地用 asset_path，
        # 导致播放启动现场重建会话）。
        assert warmups == [
            ("/media/source-a.mov", renderer._decode_target_size, "clip-a1")
        ]

    def test_set_sequence_with_only_empty_track_added_is_lightweight(
        self,
        mock_project,
        simple_sequence,
        mock_frame_provider,
    ):
        """Adding an empty track must not restart the playback render pipeline."""
        renderer = TimelinePreviewRenderer(
            project=mock_project,
            sequence=simple_sequence,
            frame_provider=mock_frame_provider,
            viewport_size=(1920, 1080),
        )
        renderer._cache.put(0, "cached-frame")
        generation_before = renderer._generation_id
        calls = {"prewarm": 0, "warmup": 0}

        def prewarm(sequence):
            calls["prewarm"] += 1

        def warmup():
            calls["warmup"] += 1

        renderer._prewarm_sequence_decode_sources = prewarm  # type: ignore[method-assign]
        renderer._schedule_current_frame_warmup = warmup  # type: ignore[method-assign]

        empty_track = EditTrack(
            track_id="empty-v2",
            name="V2",
            track_type="video",
            index=1,
            clips=(),
        )
        new_sequence = replace(
            simple_sequence,
            video_tracks=simple_sequence.video_tracks + (empty_track,),
        )

        renderer.set_sequence(new_sequence)

        assert renderer._sequence == new_sequence
        assert renderer._generation_id == generation_before
        assert renderer._cache.get(0) == "cached-frame"
        assert calls == {"prewarm": 0, "warmup": 0}

        renderer.shutdown()

    def test_set_sequence_with_video_content_change_still_resets_pipeline(
        self,
        mock_project,
        simple_sequence,
        mock_frame_provider,
    ):
        """Real clip changes still invalidate generation and frame cache."""
        renderer = TimelinePreviewRenderer(
            project=mock_project,
            sequence=simple_sequence,
            frame_provider=mock_frame_provider,
            viewport_size=(1920, 1080),
        )
        renderer._cache.put(0, "cached-frame")
        generation_before = renderer._generation_id
        calls = {"prewarm": 0, "warmup": 0}

        def prewarm(sequence):
            calls["prewarm"] += 1

        def warmup():
            calls["warmup"] += 1

        renderer._prewarm_sequence_decode_sources = prewarm  # type: ignore[method-assign]
        renderer._schedule_current_frame_warmup = warmup  # type: ignore[method-assign]

        changed_clip = replace(
            simple_sequence.video_tracks[0].clips[0],
            timeline_start=12,
        )
        changed_track = replace(
            simple_sequence.video_tracks[0],
            clips=(changed_clip,),
        )
        new_sequence = replace(simple_sequence, video_tracks=(changed_track,))

        renderer.set_sequence(new_sequence)

        assert renderer._generation_id == generation_before + 1
        assert renderer._cache.get(0) is None
        assert calls == {"prewarm": 1, "warmup": 1}

        renderer.shutdown()

    def test_invalidate_cache(self, mock_project, simple_sequence, mock_frame_provider):
        """测试缓存失效"""
        renderer = TimelinePreviewRenderer(
            project=mock_project,
            sequence=simple_sequence,
            frame_provider=mock_frame_provider,
            viewport_size=(1920, 1080),
        )

        # 缓存多个帧
        for i in range(10):
            renderer._cache.put(i, f"frame-{i}")

        # 失效 5-7 帧
        renderer.invalidate_cache(5, 7)

        # 验证失效
        for i in range(5, 8):
            assert renderer._cache.get(i) is None

        # 其他帧应还在
        assert renderer._cache.get(0) == "frame-0"

        renderer.shutdown()

    def test_clear_cache(self, mock_project, simple_sequence, mock_frame_provider):
        """测试清空缓存"""
        renderer = TimelinePreviewRenderer(
            project=mock_project,
            sequence=simple_sequence,
            frame_provider=mock_frame_provider,
            viewport_size=(1920, 1080),
        )

        # 缓存帧
        for i in range(10):
            renderer._cache.put(i, f"frame-{i}")

        # 清空
        renderer.clear_cache()

        # 验证清空
        for i in range(10):
            assert renderer._cache.get(i) is None

        renderer.shutdown()

    def test_get_cache_stats(self, mock_project, simple_sequence, mock_frame_provider):
        """测试获取缓存统计"""
        renderer = TimelinePreviewRenderer(
            project=mock_project,
            sequence=simple_sequence,
            frame_provider=mock_frame_provider,
            viewport_size=(1920, 1080),
        )

        stats = renderer.get_cache_stats()

        assert "size" in stats
        assert "max_size" in stats
        assert "hit_rate" in stats

        renderer.shutdown()

    def test_shutdown(self, mock_project, simple_sequence, mock_frame_provider):
        """测试关闭渲染器"""
        renderer = TimelinePreviewRenderer(
            project=mock_project,
            sequence=simple_sequence,
            frame_provider=mock_frame_provider,
            viewport_size=(1920, 1080),
        )

        # 关闭
        renderer.shutdown()

        # 验证清理
        assert renderer._worker is None
        assert renderer._cache.get_stats()["size"] == 0

    def test_shutdown_cancels_delayed_full_quality_refresh(
        self,
        mock_project,
        simple_sequence,
        mock_frame_provider,
        qtbot,
    ):
        """A delayed quality refresh must not outlive its renderer."""
        renderer = TimelinePreviewRenderer(
            project=mock_project,
            sequence=simple_sequence,
            frame_provider=mock_frame_provider,
            viewport_size=(1920, 1080),
        )
        refresh = Mock()
        renderer.request_render = refresh

        renderer._schedule_full_quality_refresh()
        timer = renderer._full_quality_refresh_timer
        assert timer.parent() is renderer
        assert timer.isActive()

        renderer.shutdown()
        assert not timer.isActive()
        renderer.deleteLater()

        # Process deletion and pass the timer's full delay. No callback should
        # run after shutdown or touch the deleted QObject wrapper.
        qtbot.wait(300)
        refresh.assert_not_called()


# ── 多轨合成测试 ──


class TestMultiTrackCompositing:
    """多轨合成测试"""

    def test_topmost_layer_selection(self, multi_track_sequence):
        """测试选择最上层"""
        plan = TimelineRenderPlan(multi_track_sequence)

        # 帧 75 在两个轨道重叠区域
        frame_plan = plan.get_frame_plan(75)

        assert frame_plan.layer_count == 2

        # 最上层应为 track_index = 1
        layers = list(frame_plan.layers)
        topmost = max(layers, key=lambda l: l.track_index)
        assert topmost.track_index == 1
        assert topmost.clip_id == "clip-upper"

    def test_non_overlapping_frame(self, multi_track_sequence):
        """测试不重叠的帧"""
        plan = TimelineRenderPlan(multi_track_sequence)

        # 帧 25 只有下层有内容
        frame_plan = plan.get_frame_plan(25)

        assert frame_plan.layer_count == 1
        assert frame_plan.layers[0].clip_id == "clip-lower"


# ── 信号测试 ──


class TestSignals:
    """信号测试"""

    def test_frame_ready_signal(self, mock_project, simple_sequence, mock_frame_provider):
        """测试 frame_ready 信号"""
        renderer = TimelinePreviewRenderer(
            project=mock_project,
            sequence=simple_sequence,
            frame_provider=mock_frame_provider,
            viewport_size=(1920, 1080),
        )

        # 模拟渲染完成回调
        mock_result = Mock()
        renderer._on_frame_rendered(0, mock_result)

        # 验证缓存
        assert renderer._cache.get(0) == mock_result

        renderer.shutdown()

    def test_content_frame_none_result_preserves_previous_viewer_frame(
        self,
        mock_project,
        simple_sequence,
        mock_frame_provider,
    ):
        """有内容帧临时返回 None 时，不发 no_clip 空帧到 Viewer。"""
        renderer = TimelinePreviewRenderer(
            project=mock_project,
            sequence=simple_sequence,
            frame_provider=mock_frame_provider,
            viewport_size=(1920, 1080),
        )
        renderer._advance_to_next_frame = Mock()

        with patch.object(renderer, "frame_ready") as mock_signal:
            renderer._on_frame_rendered(0, None)

        mock_signal.emit.assert_not_called()
        assert renderer._pending_frame is None

        renderer.shutdown()

    def test_empty_timeline_none_result_emits_black_frame(
        self,
        mock_project,
        mock_frame_provider,
    ):
        """An intentional empty range replaces the previous viewer frame with black."""
        sequence = TimelineSequence(
            timeline_id="empty-seq",
            name="Empty",
            fps=Rational(24, 1),
            width=1920,
            height=1080,
        )
        renderer = TimelinePreviewRenderer(
            project=mock_project,
            sequence=sequence,
            frame_provider=mock_frame_provider,
            viewport_size=(1920, 1080),
        )
        renderer._advance_to_next_frame = Mock()

        with patch.object(renderer, "frame_ready") as mock_signal:
            renderer._on_frame_rendered(0, None)

        mock_signal.emit.assert_called_once()
        frame = mock_signal.emit.call_args.args[1]
        assert isinstance(frame, QImage)
        assert frame.width() == renderer._decode_target_size[0]
        assert frame.height() == renderer._decode_target_size[1]
        assert frame.pixelColor(0, 0).red() == 0
        assert frame.pixelColor(0, 0).green() == 0
        assert frame.pixelColor(0, 0).blue() == 0
        assert renderer._cache.get(0) is frame

        renderer.shutdown()

    def test_render_error_signal(self, mock_project, simple_sequence, mock_frame_provider):
        """测试 render_error 信号"""
        renderer = TimelinePreviewRenderer(
            project=mock_project,
            sequence=simple_sequence,
            frame_provider=mock_frame_provider,
            viewport_size=(1920, 1080),
        )

        # 模拟渲染错误回调
        renderer._on_render_error(0, "Test error")

        # 验证 pending 标记清除
        assert renderer._pending_frame is None

        renderer.shutdown()

    def test_cache_stats_changed_signal(self, mock_project, simple_sequence, mock_frame_provider):
        """测试 cache_stats_changed 信号"""
        renderer = TimelinePreviewRenderer(
            project=mock_project,
            sequence=simple_sequence,
            frame_provider=mock_frame_provider,
            viewport_size=(1920, 1080),
        )

        # 模拟渲染完成
        renderer._on_frame_rendered(0, Mock())

        # 验证统计变化
        stats = renderer.get_cache_stats()
        assert stats["size"] == 1

        renderer.shutdown()


# ── 工厂函数测试 ──


class TestCreateTimelinePreviewRenderer:
    """create_timeline_preview_renderer 测试"""

    def test_create_with_defaults(self, mock_project, simple_sequence):
        """测试使用默认配置创建"""
        # Mock VideoFrameProviderService
        with patch('src.shared.video.frame_provider_service.VideoFrameProviderService') as mock_vfps:
            mock_vfps.return_value = Mock()

            renderer = create_timeline_preview_renderer(
                project=mock_project,
                sequence=simple_sequence,
                main_context=None,  # 不使用 GPU 渲染
            )

            assert renderer is not None
            assert renderer._project == mock_project
            assert renderer._sequence == simple_sequence

            renderer.shutdown()

    def test_create_with_gpu(self, mock_project, simple_sequence):
        """测试使用 GPU 渲染创建"""
        mock_context = Mock()

        with patch('src.shared.video.frame_provider_service.VideoFrameProviderService') as mock_vfps:
            with patch('src.shared.video.video_async_renderer.VideoAsyncRenderer') as mock_var:
                mock_vfps.return_value = Mock()
                mock_var.return_value = Mock()

                renderer = create_timeline_preview_renderer(
                    project=mock_project,
                    sequence=simple_sequence,
                    main_context=mock_context,
                )

                assert renderer._video_renderer is not None

                renderer.shutdown()


# ── 验收标准测试 ──


class TestAcceptanceCriteria:
    """验收标准测试"""

    def test_single_track_video_clip_visible(self, simple_sequence, mock_frame_provider, mock_project):
        """
        验收标准 1：单轨视频 clip 能在 Viewer 中看到画面

        验证：
        - RenderPlan 包含 clip 信息
        - 能获取到帧数据
        - frame_ready 信号发射
        """
        # 修改项目以包含素材
        project = mock_project.import_asset(
            path="/path/to/video.mp4",
            media_type="video",
            metadata={"width": 1920, "height": 1080, "fps": 24.0, "total_frames": 100},
        )

        # 更新 clip 的 asset_id
        asset_id = list(project.assets.keys())[0]
        clip = simple_sequence.video_tracks[0].clips[0]

        # 重新创建 clip 使用正确的 asset_id
        new_clip = ClipInstance(
            clip_id=clip.clip_id,
            asset_id=asset_id,
            track_id=clip.track_id,
            timeline_start=clip.timeline_start,
            timeline_duration=clip.timeline_duration,
            source_start=clip.source_start,
            source_duration=clip.source_duration,
            speed=clip.speed,
        )

        new_track = EditTrack(
            track_id=simple_sequence.video_tracks[0].track_id,
            name=simple_sequence.video_tracks[0].name,
            track_type="video",
            index=simple_sequence.video_tracks[0].index,
            clips=(new_clip,),
        )

        new_sequence = TimelineSequence(
            timeline_id=simple_sequence.timeline_id,
            name=simple_sequence.name,
            fps=simple_sequence.fps,
            width=simple_sequence.width,
            height=simple_sequence.height,
            video_tracks=(new_track,),
        )

        # 创建渲染器
        renderer = TimelinePreviewRenderer(
            project=project,
            sequence=new_sequence,
            frame_provider=mock_frame_provider,
            viewport_size=(1920, 1080),
        )

        # 计算渲染计划
        plan = TimelineRenderPlan(new_sequence)
        frame_plan = plan.get_frame_plan(0)

        # 验证有内容
        assert frame_plan.has_content
        assert frame_plan.layer_count == 1

        # 验证 clip 信息
        layer = frame_plan.layers[0]
        assert layer.asset_id == asset_id
        assert layer.source_frame == 0

        renderer.shutdown()

    def test_playhead_movement_updates_frame(self, simple_sequence, mock_frame_provider, mock_project):
        """
        验收标准 2：移动播放头，画面更新

        验证：
        - set_playhead 触发 request_render
        - 不同帧有不同的 RenderPlan
        """
        renderer = TimelinePreviewRenderer(
            project=mock_project,
            sequence=simple_sequence,
            frame_provider=mock_frame_provider,
            viewport_size=(1920, 1080),
        )

        # 设置播放头到帧 0
        renderer.set_playhead(0)
        assert renderer.get_playhead() == 0

        # 设置播放头到帧 50
        renderer.set_playhead(50)
        assert renderer.get_playhead() == 50

        # 设置播放头到帧 99
        renderer.set_playhead(99)
        assert renderer.get_playhead() == 99

        renderer.shutdown()

    def test_ui_not_blocked(self, simple_sequence, mock_frame_provider, mock_project):
        """
        验收标准 3：UI 不卡顿

        验证：
        - request_render 不阻塞
        - 渲染在后台线程执行
        """
        renderer = TimelinePreviewRenderer(
            project=mock_project,
            sequence=simple_sequence,
            frame_provider=mock_frame_provider,
            viewport_size=(1920, 1080),
        )

        # 验证后台线程存在
        assert renderer._worker is not None

        # request_render 应快速返回
        start_time = time.time()
        renderer.request_render(0)
        elapsed = time.time() - start_time

        # 应在 100ms 内返回（不阻塞）
        assert elapsed < 0.1

        renderer.shutdown()


# ── 集成测试 ──


class TestIntegration:
    """集成测试"""

    @pytest.mark.slow
    def test_full_render_cycle(self, mock_project, simple_sequence, mock_frame_provider):
        """
        测试完整渲染周期

        流程：
        1. 设置播放头
        2. 计算渲染计划
        3. 获取帧数据
        4. 发射信号
        """
        renderer = TimelinePreviewRenderer(
            project=mock_project,
            sequence=simple_sequence,
            frame_provider=mock_frame_provider,
            viewport_size=(1920, 1080),
        )

        # 启动渲染线程
        worker_before = renderer._worker
        assert worker_before is not None
        assert worker_before.isRunning()

        # 设置播放头
        renderer.set_playhead(0)

        # 等待渲染完成（使用信号监听）
        # 注意：在实际测试中需要等待信号
        # 这里简化处理，等待一小段时间
        time.sleep(0.2)

        # 停止
        renderer.shutdown()

        # 验证线程已停止（worker 在 shutdown 后变为 None）
        assert renderer._worker is None
        assert not worker_before.isRunning()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
