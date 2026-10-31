"""
P3.1 空闲主动预缓冲（idle prebuffer）测试。

暂停/静默时后台预解码播放头 ±N 帧进帧缓存：
1. 调度器：距离序窗口构建、缓存命中跳过、epoch 失效
2. worker：enqueue_background_if_idle 永不挤掉交互任务、后台标志透传
3. 结果回填：background_frame_ready 只写缓存不上屏、替换低分辨率快照
4. GPU submit：队列满时后台任务拒绝而非淘汰交互任务
"""

from __future__ import annotations

import queue
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PyQt6.QtGui import QImage

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.features.edit.core.timeline_preview_renderer import (
    PreviewRenderWorker,
    TimelinePreviewRenderer,
)
from src.features.edit.core.edit_gpu_preview_renderer import (
    EditGpuPreviewRenderer,
    EditRenderTask,
)
from src.features.edit.core.timeline_v2 import ClipInstance, EditTrack, TimelineSequence
from src.features.edit.core.project import EditProject, MediaAsset
from src.features.edit.core.clip_params import TransformParams, CropParams
from src.shared.media.metadata import Rational


# ── Fixtures（与 test_timeline_preview_renderer.py 保持一致）──


@pytest.fixture
def mock_frame_provider():
    provider = Mock()
    provider.open_clip.return_value = "clip-test-001"
    mock_frame = Mock()
    mock_frame.data = Mock()
    mock_frame.data.shape = (1080, 1920, 3)
    mock_frame.data.dtype = "uint8"
    mock_frame.pixel_format = "bgr24"
    provider.request_frame.return_value = mock_frame
    provider.close_clip.return_value = True
    return provider


@pytest.fixture
def mock_asset():
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
    project = EditProject.create("Test Project")
    return project.import_asset(
        path=mock_asset.path,
        media_type=mock_asset.media_type,
        metadata=mock_asset.metadata,
    )


@pytest.fixture
def simple_sequence():
    clip = ClipInstance(
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
    track = EditTrack(
        track_id="track-001",
        name="Video 1",
        track_type="video",
        index=0,
        clips=(clip,),
    )
    return TimelineSequence(
        timeline_id="seq-001",
        name="Test Sequence",
        fps=Rational(24, 1),
        width=1920,
        height=1080,
        video_tracks=(track,),
    )


def _make_renderer(mock_project, simple_sequence, mock_frame_provider):
    return TimelinePreviewRenderer(
        project=mock_project,
        sequence=simple_sequence,
        frame_provider=mock_frame_provider,
        viewport_size=(1920, 1080),
    )


# ── 调度器 ──


class TestIdlePrebufferScheduler:
    def test_start_enqueues_forward_neighbor_first(
        self, mock_project, simple_sequence, mock_frame_provider
    ):
        """窗口按 |offset| 升序、正向优先派发，首个任务是 anchor+1。"""
        renderer = _make_renderer(mock_project, simple_sequence, mock_frame_provider)
        try:
            enqueued = []

            def fake_enqueue(frame, plan, gen, **kwargs):
                enqueued.append((int(frame), kwargs.get("decode_target_size")))
                return True

            renderer._worker.enqueue_background_if_idle = fake_enqueue
            renderer._start_idle_prebuffer(50)
            assert enqueued and enqueued[0][0] == 51
            assert renderer._idle_prebuffer_pending == {51}
            # 一次 tick 只派发一个任务。
            assert len(enqueued) == 1
        finally:
            renderer.shutdown()

    def test_cached_frames_are_skipped(
        self, mock_project, simple_sequence, mock_frame_provider
    ):
        renderer = _make_renderer(mock_project, simple_sequence, mock_frame_provider)
        try:
            renderer._cache.put(51, QImage(16, 16, QImage.Format.Format_RGB888))
            renderer._cache.put(49, QImage(16, 16, QImage.Format.Format_RGB888))
            enqueued = []

            def fake_enqueue(frame, plan, gen, **kwargs):
                enqueued.append(int(frame))
                return True

            renderer._worker.enqueue_background_if_idle = fake_enqueue
            renderer._start_idle_prebuffer(50)
            assert 49 not in enqueued and 51 not in enqueued
            assert enqueued and enqueued[0] == 52
        finally:
            renderer.shutdown()

    def test_render_activity_aborts_dispatch(
        self, mock_project, simple_sequence, mock_frame_provider
    ):
        """派发期间出现新渲染请求（时间戳刷新）→ tick 链立即让位。"""
        renderer = _make_renderer(mock_project, simple_sequence, mock_frame_provider)
        try:
            called = []
            renderer._worker.enqueue_background_if_idle = (
                lambda frame, plan, gen, **kw: called.append(frame) or True
            )
            # 模拟一次刚刚发生的渲染请求（安静时间 < delay）。
            renderer._last_render_request_ts = time.monotonic()
            renderer._idle_prebuffer_dispatching = True
            renderer._idle_prebuffer_targets = [51]
            renderer._tick_idle_prebuffer()
            assert called == []
            assert renderer._idle_prebuffer_dispatching is False
        finally:
            renderer.shutdown()

    def test_quiet_poll_starts_dispatch(
        self, mock_project, simple_sequence, mock_frame_provider
    ):
        """静默满 delay 后轮询回调启动窗口派发。"""
        renderer = _make_renderer(mock_project, simple_sequence, mock_frame_provider)
        try:
            enqueued = []
            renderer._worker.enqueue_background_if_idle = (
                lambda frame, plan, gen, **kw: enqueued.append(frame) or True
            )
            renderer._idle_prebuffer_armed = True
            renderer._idle_prebuffer_anchor = 50
            renderer._last_render_request_ts = time.monotonic() - 1.0  # 已静默 1s
            renderer._idle_prebuffer_poll()
            assert renderer._idle_prebuffer_dispatching is True
            assert enqueued and enqueued[0] == 51
        finally:
            renderer.shutdown()

    def test_scrub_active_blocks_scheduling(
        self, mock_project, simple_sequence, mock_frame_provider
    ):
        renderer = _make_renderer(mock_project, simple_sequence, mock_frame_provider)
        try:
            renderer._scrub_active = True
            renderer._schedule_idle_prebuffer(50)
            assert renderer._idle_prebuffer_armed is False  # 未布防
        finally:
            renderer.shutdown()


# ── worker 行为 ──


class TestWorkerBackgroundEnqueue:
    def _make_worker(self, mock_project, simple_sequence, mock_frame_provider):
        return PreviewRenderWorker(
            frame_provider=mock_frame_provider,
            asset_resolver=Mock(),
            video_renderer=None,
            timeline_sequence=simple_sequence,
        )

    def test_never_displaces_queued_interactive_task(
        self, mock_project, simple_sequence, mock_frame_provider
    ):
        worker = self._make_worker(mock_project, simple_sequence, mock_frame_provider)
        interactive_task = ("frame-interactive",)
        worker._queue.put_nowait(interactive_task)

        plan = Mock()
        assert worker.enqueue_background_if_idle(60, plan, 7) is False
        assert worker._queue.qsize() == 1
        assert worker._queue.get_nowait() == interactive_task

    def test_background_task_carries_flags(
        self, mock_project, simple_sequence, mock_frame_provider
    ):
        worker = self._make_worker(mock_project, simple_sequence, mock_frame_provider)
        assert worker.enqueue_background_if_idle(60, Mock(), worker._generation_id) is True
        task = worker._queue.get_nowait()
        meta = task[6]
        assert meta["background"] is True
        assert meta["request_kind"] == "idle_prebuffer"

    def test_background_qimage_routes_to_cache_signal(
        self, mock_project, simple_sequence, mock_frame_provider
    ):
        worker = self._make_worker(mock_project, simple_sequence, mock_frame_provider)
        worker._current_task_background = True
        img = QImage(8, 8, QImage.Format.Format_RGB888)
        with (
            patch_signal(worker.background_frame_ready) as bg_emit,
            patch_signal(worker.frame_rendered) as fr_emit,
        ):
            worker._render_frame_qimage(10, img, worker._generation_id)
            bg_emit.assert_called_once()
            assert bg_emit.call_args[0][0] == 10
            fr_emit.assert_not_called()


def patch_signal(signal):
    """Context manager that swaps a bound pyqtSignal.emit for a Mock."""
    import contextlib

    @contextlib.contextmanager
    def _ctx():
        m = Mock()
        signal.connect(lambda *a: m(*a))
        yield m

    return _ctx()


# ── 结果回填 ──


class TestBackgroundCacheFill:
    def test_full_res_result_replaces_snapshot_entry(
        self, mock_project, simple_sequence, mock_frame_provider
    ):
        renderer = _make_renderer(mock_project, simple_sequence, mock_frame_provider)
        try:
            snapshot_img = QImage(4, 4, QImage.Format.Format_RGB888)
            renderer._cache.put(50, snapshot_img)
            renderer._snapshot_frame_keys.add(50)
            renderer._idle_prebuffer_pending.add(50)

            full_img = QImage(16, 16, QImage.Format.Format_RGB888)
            with patch_signal(renderer.frame_ready) as viewer_emit:
                renderer._on_worker_background_frame_ready(
                    50, renderer._generation_id, full_img
                )
                viewer_emit.assert_not_called()  # 不上屏

            assert renderer._cache.get(50) is full_img
            assert 50 not in renderer._snapshot_frame_keys
            assert 50 not in renderer._idle_prebuffer_pending
        finally:
            renderer.shutdown()

    def test_stale_generation_dropped(
        self, mock_project, simple_sequence, mock_frame_provider
    ):
        renderer = _make_renderer(mock_project, simple_sequence, mock_frame_provider)
        try:
            before = renderer._cache.get(50)
            renderer._on_worker_background_frame_ready(
                50, renderer._generation_id + 999, QImage(8, 8, QImage.Format.Format_RGB888)
            )
            assert renderer._cache.get(50) is before
        finally:
            renderer.shutdown()


# ── GPU renderer 提交 ──


class TestGpuSubmitBackground:
    def test_background_task_refused_when_queue_full(self):
        shell = SimpleNamespace(
            _initialized=True,
            _queue=queue.Queue(maxsize=1),
            # 与任务 generation 对齐（生产路径 submit 前已 set_generation_id），
            # 避免 submit 走 generation 前进的清理分支（shell 无该机制）
            _generation_id=1,
        )
        # submit 会做 QoS 合并：队列中仅一个交互任务，无可合并项
        shell._coalesce_pending_tasks = lambda drop_background: []
        keep = EditRenderTask(frame=1, generation_id=1, layers=[])
        shell._queue.put_nowait(keep)
        bg = EditRenderTask(
            frame=2, generation_id=1, layers=[], background=True,
            request_kind="idle_prebuffer",
        )
        assert EditGpuPreviewRenderer.submit(shell, bg) is False
        assert shell._queue.qsize() == 1
        assert shell._queue.get_nowait() is keep

    def test_task_default_not_background(self):
        assert EditRenderTask(frame=1, generation_id=1, layers=[]).background is False
