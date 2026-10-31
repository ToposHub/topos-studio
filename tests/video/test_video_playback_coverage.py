"""Coverage tests for ``src/shared/video/video_playback.py`` (Plan 02 / Agent C2).

补充覆盖播放引擎中可 headless 测试的单元逻辑（全部使用 mock 帧源，不真实
打开媒体文件 / 不依赖 PyAV 解码）：

- 播放状态机：start/stop/toggle 的状态与信号、全局播放注册表
- 帧号与时间换算：seek_to_time / get_current_time / get_time_string（含
  NTSC 分数帧率 30000/1001）
- 速度处理：set_playback_speed 的定时器间隔与时钟速度联动
- 缓冲与丢帧策略：ProxyPrefetchWorker 的就绪队列（精确/最近未来帧匹配、
  generation 失效、满队列丢最旧帧、落后帧清理、run 循环、错误路径）
- seek 请求合并：worker 层 _seek_request 覆盖式合并、引擎层静态预览
  request-key 去重（轮询不重复 seek）
- 异常路径：coordinator load 失败/抛异常、首帧三级获取（master 超时 →
  proxy → placeholder）、帧转换失败
- 循环播放边界：loop_playback / In-Out 范围回绕 / 未知时长不提前停止 /
  跨片段残留帧丢弃
- 基类桩行为、代理分辨率计算、显示几何、渲染参数与提交路径

已知疑似 bug 以 characterization test 固定当前行为并引用 BUG-C2-xx 编号，
详见 docs/testcoverage/2026-09-28/BUGS-agent-C2.md。

验收命令：
.venv/bin/python -m pytest tests/video/test_video_playback_coverage.py -q
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, List, Optional, Tuple
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.shared.media.frame import DecodedFrame
from src.shared.media.gpu_frame import GpuDecodedFrame, GpuFramePlane
from src.shared.video.render_commands import RenderCommand
from src.shared.video.video_playback import (  # noqa: F401
    EnhancedVideoPlaybackEngine,
    FirstFrameResult,
    FirstFrameStatus,
    ProxyPrefetchWorker,
    VideoPipelineFallbackReason,
    VideoPlaybackEngine,
    _assert_no_sync_decode_in_playback,
    _global_playback_engines,
    _global_playback_state_lock,
    _is_any_engine_playing,
    _register_playback_engine,
    _safe_stat_int,
    _unregister_playback_engine,
)


# =============================================================================
# Fixtures / helpers
# =============================================================================


@pytest.fixture(scope="session")
def qapp():
    """Create a headless QApplication for the entire test session."""
    from PyQt6.QtWidgets import QApplication

    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    yield app


@pytest.fixture(autouse=True)
def _clean_global_playback_registry():
    """Isolate the module-level playback registry between tests."""
    with _global_playback_state_lock:
        _global_playback_engines.clear()
    yield
    with _global_playback_state_lock:
        _global_playback_engines.clear()


class _SignalSpy:
    """Record pyqtSignal emissions without an event loop (direct connections)."""

    def __init__(self, signal: Any) -> None:
        self.calls: List[Tuple[Any, ...]] = []
        signal.connect(self._on_signal)

    def _on_signal(self, *args: Any) -> None:
        self.calls.append(args)

    @property
    def values(self) -> List[Any]:
        """First argument of each emission."""
        return [call[0] for call in self.calls if call]


def _default_config() -> dict:
    return {
        "video_pipeline_mode": "v3",
        "video_pipeline_allow_v2_fallback": False,
        "video_playback_resolution": 1920,
        "video_preview_drop_frames": True,
        "video_preview_max_queue": 3,
        "video_use_yuv_gpu_upload": True,
    }


def make_engine(**config_overrides: Any) -> MagicMock:
    """Build a mock ImageEngine with a real dict-backed config manager."""
    config = _default_config()
    config.update(config_overrides)

    engine = MagicMock()
    engine.renderer = None
    engine.viewport_size = (960, 540)
    engine.visible_rect = (0.0, 0.0, 960.0, 540.0)
    engine.scene_size = None
    engine.layers = []
    engine.current_raw_params = {"enabled": False}
    engine.film_grain_params = {}
    engine.show_mask_mode = False
    engine.active_layer_index = 0
    engine.img_full_original = None
    engine.img_source_full = None
    engine.canvas_ref = None

    engine.window = MagicMock()
    engine.window.config_manager = MagicMock()
    engine.window.config_manager.config = dict(config)
    engine.window.config_manager.get = lambda key, default=None: config.get(key, default)
    return engine


def make_coordinator(total_frames: int = 100, fps: float = 25.0) -> MagicMock:
    """Build a mock PlaybackCoordinator with sane scalar attributes."""
    coordinator = MagicMock()
    coordinator.is_loaded = True
    coordinator.total_frames = total_frames
    coordinator.fps = fps
    coordinator.current_frame = 0
    coordinator.session = None
    coordinator.get_video_info.return_value = {}
    coordinator.load.return_value = True
    coordinator.start_playback.return_value = True
    return coordinator


@pytest.fixture
def make_controller(qapp):
    """Factory for controllers; stops playback and clears the registry on teardown."""
    created: List[Any] = []

    def _factory(
        engine: MagicMock,
        coordinator: Optional[MagicMock] = None,
        cls: type = EnhancedVideoPlaybackEngine,
    ) -> Any:
        controller = cls(engine)
        created.append(controller)
        if coordinator is not None:
            controller._use_dual_path = True
            controller._coordinator = coordinator
            controller.current_path = "/tmp/coverage-video.mp4"
        return controller

    yield _factory

    for controller in created:
        try:
            controller.stop_playback()
        except Exception:
            pass
    with _global_playback_state_lock:
        _global_playback_engines.clear()


def make_packed_frame(
    frame_index: int,
    color: Tuple[int, int, int] = (255, 0, 0),
    pixel_format: str = "rgb24",
    width: int = 4,
    height: int = 4,
) -> DecodedFrame:
    """Build a packed DecodedFrame with one marked pixel."""
    data = np.zeros((height, width, 3), dtype=np.uint8)
    data[0, 0] = color
    return DecodedFrame(
        frame_index=frame_index,
        pts=0,
        time_seconds=0.0,
        data=data,
        pixel_format=pixel_format,
        width=width,
        height=height,
    )


def make_planar_frame(frame_index: int, pixel_format: str = "yuv420p") -> DecodedFrame:
    """Build a planar yuv420p DecodedFrame (4x4 luma, 2x2 chroma)."""
    y = np.full((4, 4), 16, dtype=np.uint8)
    u = np.full((2, 2), 128, dtype=np.uint8)
    v = np.full((2, 2), 128, dtype=np.uint8)
    return DecodedFrame(
        frame_index=frame_index,
        pts=0,
        time_seconds=0.0,
        planes=(y, u, v),
        pixel_format=pixel_format,
    )


# =============================================================================
# P2-1: 全局播放状态追踪与同步解码断言
# =============================================================================


class TestGlobalPlaybackStateTracking:
    """播放状态全局注册表（用于检测 UI tick 中的同步解码违规）。"""

    def test_register_and_query_engine_state(self) -> None:
        _register_playback_engine(1001, True)
        assert _is_any_engine_playing() is True

        _register_playback_engine(1002, False)
        assert _is_any_engine_playing() is True  # 1001 仍在播放

        _register_playback_engine(1001, False)
        assert _is_any_engine_playing() is False

    def test_unregister_removes_engine(self) -> None:
        _register_playback_engine(2001, True)
        _unregister_playback_engine(2001)
        assert _is_any_engine_playing() is False
        # 重复注销不抛错
        _unregister_playback_engine(2001)

    def test_sync_decode_violation_logged_on_main_thread(self, caplog) -> None:
        _register_playback_engine(3001, True)
        with caplog.at_level(logging.ERROR, logger="src.shared.video.video_playback"):
            _assert_no_sync_decode_in_playback("seek_frame", frame_index=5)
        assert any("P2-1 VIOLATION" in rec.message for rec in caplog.records)

        # 注销后（非播放态）同样的调用不应记录违规
        _register_playback_engine(3001, False)
        caplog.clear()
        with caplog.at_level(logging.ERROR, logger="src.shared.video.video_playback"):
            _assert_no_sync_decode_in_playback("seek_frame", frame_index=5)
        assert not any("P2-1 VIOLATION" in rec.message for rec in caplog.records)

    def test_sync_decode_check_scoped_to_specific_engine(self, caplog) -> None:
        _register_playback_engine(4001, True)
        with caplog.at_level(logging.ERROR, logger="src.shared.video.video_playback"):
            # 只检查 engine 4002（未注册，视为未播放）→ 不违规
            _assert_no_sync_decode_in_playback("get_master_frame", engine_id=4002)
        assert not any("P2-1 VIOLATION" in rec.message for rec in caplog.records)

        caplog.clear()
        with caplog.at_level(logging.ERROR, logger="src.shared.video.video_playback"):
            _assert_no_sync_decode_in_playback("get_master_frame", engine_id=4001)
        assert any("P2-1 VIOLATION" in rec.message for rec in caplog.records)


class TestSafeStatInt:
    """P1-1: stats 字典的安全整数读取（防 MagicMock TypeError）。"""

    def test_int_passthrough(self) -> None:
        assert _safe_stat_int({"queue_depth": 7}, "queue_depth") == 7

    def test_missing_key_returns_default(self) -> None:
        assert _safe_stat_int({}, "queue_depth") == 0
        assert _safe_stat_int({}, "queue_depth", default=5) == 5

    def test_non_int_value_returns_default(self) -> None:
        assert _safe_stat_int({"queue_depth": MagicMock()}, "queue_depth") == 0
        assert _safe_stat_int({"queue_depth": "12"}, "queue_depth", default=2) == 2


# =============================================================================
# ProxyPrefetchWorker: 缓冲与丢帧策略（mock 帧源）
# =============================================================================


def _make_worker(
    qapp: Any,
    max_queue_size: int = 8,
    lookahead: int = 4,
    current_frame: int = 0,
    total_frames: int = 100,
) -> Tuple[ProxyPrefetchWorker, MagicMock, SimpleNamespace]:
    source_manager = MagicMock()
    source_manager.total_frames = total_frames
    clock = SimpleNamespace(current_frame=current_frame)
    worker = ProxyPrefetchWorker(
        source_manager=source_manager,
        playback_clock=clock,
        max_queue_size=max_queue_size,
        prefetch_lookahead=lookahead,
    )
    # BUG-C2-02（workaround）：生产代码 get_ready_frame/_add_frame 在持有
    # 非递归 QMutex 时嵌套调用 _remove_frame 再加锁，会死锁。这里替换为
    # QRecursiveMutex 以便测试队列逻辑本身；死锁证据见 BUGS-agent-C2.md。
    from PyQt6.QtCore import QRecursiveMutex

    worker._ready_frames_mutex = QRecursiveMutex()  # type: ignore[assignment]
    return worker, source_manager, clock


class TestProxyPrefetchQueue:
    """就绪帧队列策略：匹配、消费、generation 失效、满队丢旧。"""

    def test_exact_match_is_returned_and_removed(self, qapp) -> None:
        worker, _, _ = _make_worker(qapp)
        frame = np.zeros((2, 2, 3), dtype=np.uint8)
        worker._add_frame(10, frame, worker._get_generation())

        result = worker.get_ready_frame(10)
        assert result is not None
        array, index = result
        assert index == 10
        assert array is frame
        # 已消费：队列为空，再次取返回 None
        assert worker.get_queue_depth() == 0
        assert worker.get_ready_frame(10) is None

    def test_nearest_future_frame_is_matched(self, qapp) -> None:
        worker, _, _ = _make_worker(qapp)
        gen = worker._get_generation()
        far = np.zeros((2, 2, 3), dtype=np.uint8)
        near = np.ones((2, 2, 3), dtype=np.uint8)
        worker._add_frame(16, far, gen)
        worker._add_frame(15, near, gen)

        result = worker.get_ready_frame(14)
        assert result is not None
        array, index = result
        assert index == 15, "应返回最近的未来帧"
        assert array is near

    def test_past_frames_are_skipped(self, qapp) -> None:
        worker, _, _ = _make_worker(qapp)
        gen = worker._get_generation()
        worker._add_frame(12, np.zeros((2, 2, 3), dtype=np.uint8), gen)

        # 12 < 目标 14 → 过去帧被跳过，无未来帧 → None
        assert worker.get_ready_frame(14) is None
        assert worker.get_queue_depth() == 1, "过去帧不应被消费"

    def test_stale_generation_frames_are_invalidated_by_seek(self, qapp) -> None:
        worker, _, _ = _make_worker(qapp)
        worker._add_frame(20, np.zeros((2, 2, 3), dtype=np.uint8), 0)
        worker.request_seek(20)  # generation 0 → 1，队列清空

        assert worker.get_queue_depth() == 0
        fresh = np.ones((2, 2, 3), dtype=np.uint8)
        # 旧 generation 帧与新 generation 帧并存
        worker._add_frame(20, np.zeros((2, 2, 3), dtype=np.uint8), 0)
        worker._add_frame(21, fresh, 1)

        result = worker.get_ready_frame(20)
        assert result is not None
        array, index = result
        assert index == 21, "旧 generation 的精确帧必须被跳过"
        assert array is fresh

    def test_full_queue_drops_oldest_frame(self, qapp) -> None:
        worker, _, _ = _make_worker(qapp, max_queue_size=2)
        gen = worker._get_generation()
        oldest = np.zeros((2, 2, 3), dtype=np.uint8)
        middle = np.ones((2, 2, 3), dtype=np.uint8)
        newest = np.full((2, 2, 3), 7, dtype=np.uint8)
        worker._add_frame(1, oldest, gen)
        worker._add_frame(2, middle, gen)
        worker._add_frame(3, newest, gen)

        assert worker.get_queue_depth() == 2, "满队后应保持容量上限"
        assert worker._has_frame(1) is False, "最旧帧应已被丢弃"
        result = worker.get_ready_frame(1)
        assert result is not None
        assert result[1] == 2, "被丢弃的帧之后应返回下一帧"
        assert result[0] is middle

    def test_maybe_drop_old_frames_keeps_recent_and_current_gen(self, qapp) -> None:
        worker, _, _ = _make_worker(qapp)
        gen = worker._get_generation()
        recent = np.ones((2, 2, 3), dtype=np.uint8)
        worker._add_frame(5, np.zeros((2, 2, 3), dtype=np.uint8), gen)
        worker._add_frame(9, recent, gen)
        worker._add_frame(9, np.zeros((2, 2, 3), dtype=np.uint8), gen + 5)  # stale gen

        worker._maybe_drop_old_frames(10)  # keep_threshold = 8

        assert worker.get_queue_depth() == 1
        result = worker.get_ready_frame(9)
        assert result is not None and result[0] is recent

    def test_stop_clears_queue(self, qapp) -> None:
        worker, _, _ = _make_worker(qapp)
        worker._add_frame(3, np.zeros((2, 2, 3), dtype=np.uint8), worker._get_generation())
        worker.stop()
        assert worker.get_queue_depth() == 0
        assert worker._stop_flag is True


class TestProxyPrefetchSeekMerging:
    """seek 请求合并：连续 request_seek 只保留最后一个目标。"""

    def test_rapid_seek_requests_keep_only_latest(self, qapp) -> None:
        worker, _, _ = _make_worker(qapp)
        gen_before = worker._get_generation()

        worker.request_seek(30)
        worker.request_seek(40)  # 立即覆盖，未处理的 30 被合并掉

        assert worker._get_generation() == gen_before + 2, "每次 seek 递增 generation"
        assert worker._seek_request == 40, "pending 请求只保留最新目标"
        assert worker._prefetch_frame == 40

    def test_check_seek_request_consumes_pending_once(self, qapp) -> None:
        worker, _, _ = _make_worker(qapp)
        worker.request_seek(40)
        worker._prefetch_frame = 0  # 模拟 run 循环尚未处理

        worker._check_seek_request()
        assert worker._prefetch_frame == 40
        assert worker._seek_request is None

        # 第二次调用（无新请求）不应重置预读位置
        worker._prefetch_frame = 41
        worker._check_seek_request()
        assert worker._prefetch_frame == 41


class TestProxyPrefetchFrameProcessing:
    """worker 内的帧格式转换（packed 路径；planar 交还给主线程处理）。"""

    def test_bgra_uint8_strips_alpha(self, qapp) -> None:
        worker, _, _ = _make_worker(qapp)
        data = np.array([[[10, 20, 30, 255]]], dtype=np.uint8)
        frame = SimpleNamespace(data=data, pixel_format="bgra")

        result = worker._process_frame(frame)
        assert result is not None
        assert result.shape == (1, 1, 3)
        assert result[0, 0].tolist() == [10, 20, 30]

    def test_rgb24_uint16_converts_and_flips_to_bgr(self, qapp) -> None:
        worker, _, _ = _make_worker(qapp)
        data = np.array([[[65535, 0, 0]]], dtype=np.uint16)
        frame = SimpleNamespace(data=data, pixel_format="rgb48le")

        result = worker._process_frame(frame)
        assert result is not None
        assert result.dtype == np.uint8
        assert int(result[0, 0, 0]) == 0, "R 通道翻转到 BGR 后应在末位"
        assert int(result[0, 0, 2]) == 255

    def test_planar_frame_returns_none(self, qapp) -> None:
        worker, _, _ = _make_worker(qapp)
        frame = SimpleNamespace(
            data=None,
            planes=(np.zeros((4, 4), np.uint8),),
            pixel_format="yuv420p",
        )
        assert worker._process_frame(frame) is None

    def test_conversion_exception_returns_none(self, qapp) -> None:
        worker, _, _ = _make_worker(qapp)

        class _ExplodingFrame:
            @property
            def data(self) -> np.ndarray:
                raise ValueError("boom")

        assert worker._process_frame(_ExplodingFrame()) is None


class TestProxyPrefetchRunLoop:
    """run() 主循环（在当前线程同步执行，用受控 stop_flag 终止）。"""

    def test_run_prefetches_lookahead_window_from_source(self, qapp) -> None:
        worker, source_manager, _ = _make_worker(
            qapp, max_queue_size=8, lookahead=4, current_frame=0, total_frames=8
        )
        fetched: List[int] = []

        def fake_get(frame_index: int) -> SimpleNamespace:
            fetched.append(frame_index)
            if len(fetched) >= 4:
                worker._stop_flag = True
            return SimpleNamespace(
                data=np.zeros((2, 2, 3), dtype=np.uint8), pixel_format="bgr24"
            )

        source_manager.get_proxy_frame.side_effect = fake_get

        spy = _SignalSpy(worker.frame_ready)
        worker.run()  # 同步执行到 stop_flag 置位

        assert fetched == [0, 1, 2, 3], "应按顺序预读 lookahead 窗口内的帧"
        assert worker.get_queue_depth() == 4
        assert spy.values == [0, 1, 2, 3]

    def test_run_swallows_source_errors_and_emits_signal(self, qapp) -> None:
        worker, source_manager, _ = _make_worker(qapp)

        def raising_get(frame_index: int) -> None:
            worker._stop_flag = True
            raise RuntimeError("decode boom")

        source_manager.get_proxy_frame.side_effect = raising_get
        spy = _SignalSpy(worker.error_occurred)

        worker.run()

        assert spy.values == ["decode boom"]
        assert worker._stop_flag is True

    def test_run_waits_when_window_is_already_prefetched(self, qapp) -> None:
        worker, source_manager, clock = _make_worker(
            qapp, max_queue_size=8, lookahead=4, current_frame=0, total_frames=8
        )
        gen = worker._get_generation()
        for idx in range(4):
            worker._add_frame(idx, np.zeros((2, 2, 3), dtype=np.uint8), gen)
        worker._prefetch_frame = 0
        calls: List[int] = []

        def counting_get(frame_index: int) -> Optional[np.ndarray]:
            calls.append(frame_index)
            return None

        source_manager.get_proxy_frame.side_effect = counting_get

        # _has_frame 命中 → 预读指针前移，不重复解码已有帧；窗口耗尽后
        # 对下一帧（4）反复等待，由 stop_flag 终止。
        import threading

        stopper = threading.Timer(0.05, worker.stop)
        stopper.start()
        worker.run()
        stopper.join(timeout=1.0)

        assert calls, "窗口耗尽后应持续探测下一帧"
        assert set(calls) == {4}, "已预读的 0-3 帧不应被重复请求"


# =============================================================================
# 播放状态机（v3 coordinator 路径）
# =============================================================================


class TestPlaybackStateMachine:
    """start/stop/toggle 的状态、信号与全局注册。"""

    def test_start_playback_requires_loaded_coordinator(self, make_controller) -> None:
        engine = make_engine()
        coordinator = make_coordinator()
        coordinator.is_loaded = False
        controller = make_controller(engine, coordinator)
        spy = _SignalSpy(controller.play_state_changed)

        controller.start_playback()

        assert controller.is_playing is False
        assert spy.calls == []
        coordinator.start_playback.assert_not_called()

    def test_start_playback_failure_keeps_stopped_state(self, make_controller) -> None:
        engine = make_engine()
        coordinator = make_coordinator()
        coordinator.start_playback.return_value = False
        controller = make_controller(engine, coordinator)
        spy = _SignalSpy(controller.play_state_changed)

        controller.start_playback()

        assert controller.is_playing is False
        assert spy.calls == []
        assert _is_any_engine_playing() is False

    def test_start_playback_success_state_and_signals(self, make_controller) -> None:
        engine = make_engine()
        coordinator = make_coordinator(fps=25.0)
        controller = make_controller(engine, coordinator)
        state_spy = _SignalSpy(controller.play_state_changed)

        controller.start_playback()

        assert controller.is_playing is True
        assert state_spy.values == [True]
        assert _is_any_engine_playing() is True, "V-P0-04: 成功启动后才注册"
        coordinator.start_playback.assert_called_once()
        assert controller._play_timer.isActive() is True

        controller.stop_playback()

        assert controller.is_playing is False
        assert state_spy.values == [True, False]
        assert _is_any_engine_playing() is False
        coordinator.stop_playback.assert_called_once()
        assert controller._play_timer.isActive() is False
        assert controller._last_displayed_frame_index is None, "停止后重置去重索引"

    def test_stop_playback_emits_even_when_not_playing(self, make_controller) -> None:
        engine = make_engine()
        coordinator = make_coordinator()
        controller = make_controller(engine, coordinator)
        spy = _SignalSpy(controller.play_state_changed)

        controller.stop_playback()

        coordinator.stop_playback.assert_called_once()
        assert controller.is_playing is False
        assert spy.values == [False]

    def test_start_playback_marks_renderer_lane_and_withdraws_texture(
        self, make_controller
    ) -> None:
        engine = make_engine()
        renderer = MagicMock()
        renderer.current_front_tex_id = 99
        engine.renderer = renderer
        coordinator = make_coordinator()
        controller = make_controller(engine, coordinator)

        controller.start_playback()

        assert renderer.video_playback_active is True
        assert renderer.current_front_tex_id is None
        assert renderer.current_front_size == (0, 0)
        assert controller._last_submitted_video_reason == "playback_pending"
        controller.stop_playback()
        assert renderer.video_playback_active is False

    def test_start_playback_seeks_back_into_in_out_range(self, make_controller) -> None:
        engine = make_engine()
        coordinator = make_coordinator()
        controller = make_controller(engine, coordinator)
        controller.set_in_out_range(10, 50)
        controller.current_frame = 70  # 出点之外
        frame_spy = _SignalSpy(controller.frame_changed)

        controller.start_playback()

        coordinator.seek_to_frame.assert_called_once_with(10, blocking=False)
        assert controller.current_frame == 10
        assert frame_spy.values == [10]
        controller.stop_playback()

    def test_start_playback_without_any_path_stays_stopped(self, make_controller) -> None:
        engine = make_engine()
        controller = make_controller(engine)
        controller._use_dual_path = False
        controller._playback_clock = None
        spy = _SignalSpy(controller.play_state_changed)

        controller.start_playback()

        assert controller.is_playing is False
        assert spy.calls == []

    def test_toggle_play_stops_when_playing(self, make_controller) -> None:
        engine = make_engine()
        coordinator = make_coordinator()
        controller = make_controller(engine, coordinator)
        controller.start_playback()
        spy = _SignalSpy(controller.play_state_changed)

        controller.toggle_play()
        assert controller.is_playing is False
        assert spy.values == [False]

    def test_toggle_play_starts_when_paused(self, make_controller) -> None:
        engine = make_engine()
        coordinator = make_coordinator()
        controller = make_controller(engine, coordinator)
        spy = _SignalSpy(controller.play_state_changed)

        controller.toggle_play()
        assert controller.is_playing is True
        assert spy.values == [True]
        controller.stop_playback()

    def test_base_engine_start_playback_is_stub(self, make_controller) -> None:
        engine = make_engine()
        controller = make_controller(engine, cls=VideoPlaybackEngine)
        spy = _SignalSpy(controller.play_state_changed)

        controller.start_playback()

        assert controller.is_playing is False
        assert spy.calls == []

    def test_base_engine_tick_stops_playback(self, make_controller) -> None:
        engine = make_engine()
        controller = make_controller(engine, cls=VideoPlaybackEngine)
        controller.is_playing = True
        spy = _SignalSpy(controller.play_state_changed)

        controller._on_play_tick()

        assert controller.is_playing is False
        assert spy.values == [False]


class TestEnhancedPlaybackClockLegacyStart:
    """旧架构启动分支：仅有 _playback_clock（无 coordinator）时也能进入播放态。"""

    def test_start_with_playback_clock_only(self, make_controller) -> None:
        engine = make_engine()
        controller = make_controller(engine)
        controller._use_dual_path = True
        controller._coordinator = None
        controller._source_manager = None
        clock = MagicMock()
        clock.fps = 25.0
        controller._playback_clock = clock
        spy = _SignalSpy(controller.play_state_changed)

        controller.start_playback()

        assert controller.is_playing is True
        clock.start.assert_called_once()
        assert spy.values == [True]
        # 无 source_manager → 不启动预读 worker
        assert controller._proxy_prefetch_worker is None
        controller.stop_playback()
        clock.stop.assert_called_once()


# =============================================================================
# 帧号/时间换算（含 NTSC 分数帧率）
# =============================================================================


NTSC_FPS = 30000.0 / 1001.0  # 29.97002997...


class TestFrameTimeConversion:
    """帧号 <-> 秒换算；分数帧率使用 30000/1001。"""

    def test_get_current_time_with_fractional_fps(self, make_controller) -> None:
        engine = make_engine()
        controller = make_controller(engine)
        controller._playback_clock = SimpleNamespace(fps=NTSC_FPS)
        controller.current_frame = 90

        assert controller.get_current_time() == pytest.approx(90 / NTSC_FPS)

    def test_get_current_time_without_clock(self, make_controller) -> None:
        engine = make_engine()
        controller = make_controller(engine)
        controller._playback_clock = None
        controller.current_frame = 90

        assert controller.get_current_time() == 0.0

    def test_get_time_string_formats_current_and_total(self, make_controller) -> None:
        engine = make_engine()
        controller = make_controller(engine)
        controller._playback_clock = SimpleNamespace(fps=25.0, total_frames=3750)
        controller.current_frame = 125  # 5s

        assert controller.get_time_string() == "00:00:05 / 00:02:30"

    def test_get_time_string_without_clock(self, make_controller) -> None:
        engine = make_engine()
        controller = make_controller(engine)
        controller._playback_clock = None

        assert controller.get_time_string() == "00:00:00 / 00:00:00"

    def test_seek_to_time_converts_using_clock_fps(self, make_controller) -> None:
        engine = make_engine()
        controller = make_controller(engine, make_coordinator())
        controller._playback_clock = SimpleNamespace(fps=NTSC_FPS)
        controller.seek_to_frame = MagicMock()

        controller.seek_to_time(10.0)

        # int(10.0 * 29.97002997...) == 299（截断而非四舍五入）
        controller.seek_to_frame.assert_called_once_with(299)

    def test_seek_to_time_defaults_to_30fps_without_clock(self, make_controller) -> None:
        engine = make_engine()
        controller = make_controller(engine, make_coordinator())
        controller._playback_clock = None
        controller.seek_to_frame = MagicMock()

        controller.seek_to_time(10.0)

        controller.seek_to_frame.assert_called_once_with(300)

    def test_seek_to_time_without_loaded_video_is_noop(self, make_controller) -> None:
        engine = make_engine()
        controller = make_controller(engine, make_coordinator())
        controller.current_path = None
        controller._playback_clock = SimpleNamespace(fps=25.0)
        controller.seek_to_frame = MagicMock()

        controller.seek_to_time(4.0)

        controller.seek_to_frame.assert_not_called()


# =============================================================================
# 速度处理
# =============================================================================


class TestPlaybackSpeed:
    """set_playback_speed：定时器间隔 + 时钟速度联动。"""

    def test_double_speed_updates_timer_interval_and_clock(self, make_controller) -> None:
        engine = make_engine()
        controller = make_controller(engine, make_coordinator())
        controller._play_timer = MagicMock()
        controller.is_playing = True
        clock_set_speed = MagicMock()
        controller._playback_clock = SimpleNamespace(fps=25.0, set_speed=clock_set_speed)

        controller.set_playback_speed(2.0)

        # int(1000 / 50) = 20 → max(8, int(20*0.8)) = 16
        controller._play_timer.setInterval.assert_called_once_with(16)
        clock_set_speed.assert_called_once_with(2.0)

    def test_four_x_speed_clamps_interval_floor(self, make_controller) -> None:
        engine = make_engine()
        controller = make_controller(engine, make_coordinator())
        controller._play_timer = MagicMock()
        controller.is_playing = True
        controller._playback_clock = SimpleNamespace(fps=25.0, set_speed=MagicMock())

        controller.set_playback_speed(4.0)

        # int(1000/100)=10 → int(10*0.8)=8 → floor 8
        controller._play_timer.setInterval.assert_called_once_with(8)

    def test_zero_fps_skips_timer_update(self, make_controller) -> None:
        engine = make_engine()
        controller = make_controller(engine, make_coordinator())
        controller._play_timer = MagicMock()
        controller.is_playing = True
        clock_set_speed = MagicMock()
        controller._playback_clock = SimpleNamespace(fps=0.0, set_speed=clock_set_speed)

        controller.set_playback_speed(2.0)

        controller._play_timer.setInterval.assert_not_called()
        clock_set_speed.assert_called_once_with(2.0)

    def test_paused_engine_does_not_touch_timer(self, make_controller) -> None:
        engine = make_engine()
        controller = make_controller(engine, make_coordinator())
        controller._play_timer = MagicMock()
        controller.is_playing = False
        controller._playback_clock = SimpleNamespace(fps=25.0, set_speed=MagicMock())

        controller.set_playback_speed(2.0)

        controller._play_timer.setInterval.assert_not_called()


# =============================================================================
# In/Out 范围与循环播放边界
# =============================================================================


class TestInOutRange:
    """In/Out 范围设置与生效出点计算。"""

    def test_set_range_clamps_to_total(self, make_controller) -> None:
        engine = make_engine()
        coordinator = make_coordinator(total_frames=100)
        controller = make_controller(engine, coordinator)

        controller.set_in_out_range(10, 500)
        assert (controller.in_frame, controller.out_frame) == (10, 99)

        controller.set_in_out_range(10, None)
        assert (controller.in_frame, controller.out_frame) == (10, 99)

    def test_invalid_range_is_ignored(self, make_controller) -> None:
        engine = make_engine()
        coordinator = make_coordinator(total_frames=100)
        controller = make_controller(engine, coordinator)
        controller.set_in_out_range(2, 8)

        controller.set_in_out_range(60, 50)
        assert (controller.in_frame, controller.out_frame) == (2, 8)

    def test_clear_range(self, make_controller) -> None:
        engine = make_engine()
        controller = make_controller(engine, make_coordinator())
        controller.set_in_out_range(10, 50)

        controller.clear_in_out_range()

        assert controller.in_frame == 0
        assert controller.out_frame is None
        assert controller._in_out_range_active() is False

    def test_unknown_duration_open_end_is_none(self, make_controller) -> None:
        engine = make_engine()
        controller = make_controller(engine, make_coordinator(total_frames=0))
        controller.current_path = "/tmp/x.mp4"

        assert controller._effective_out_frame() is None, "V-P0-09: 未知时长无终点"

    def test_unknown_duration_with_user_out_point(self, make_controller) -> None:
        engine = make_engine()
        controller = make_controller(engine, make_coordinator(total_frames=0))
        controller.set_in_out_range(10, 50)

        assert controller._effective_out_frame() == 50

    def test_known_duration_with_user_out_point(self, make_controller) -> None:
        engine = make_engine()
        controller = make_controller(engine, make_coordinator(total_frames=100))
        controller.set_in_out_range(10, 150)

        assert controller._effective_out_frame() == 99

    def test_range_total_falls_back_to_source_manager(self, make_controller) -> None:
        engine = make_engine()
        controller = make_controller(engine)
        controller._use_dual_path = True
        controller._coordinator = None
        controller._source_manager = SimpleNamespace(total_frames=60)

        assert controller._range_total_frames() == 60
        assert controller._effective_out_frame() == 59

    def test_range_total_handles_bad_values(self, make_controller) -> None:
        engine = make_engine()
        coordinator = make_coordinator()
        coordinator.total_frames = "not-an-int"
        controller = make_controller(engine, coordinator)
        controller._source_manager = SimpleNamespace(total_frames=60)

        assert controller._range_total_frames() == 60

    def test_base_class_range_with_unknown_total(self, make_controller) -> None:
        engine = make_engine()
        controller = make_controller(engine, cls=VideoPlaybackEngine)

        # 基类恒为未知时长：out=None 时出点收敛为 in_frame 本身（单帧范围）
        controller.set_in_out_range(5, None)
        assert (controller.in_frame, controller.out_frame) == (5, 5)
        assert controller._effective_out_frame() == 5


class TestLoopBoundaries:
    """_on_play_tick（v3）的循环与边界判定（mock coordinator 帧）。"""

    def _make_ticking_controller(
        self, make_controller: Callable[..., Any], total_frames: int = 100
    ) -> Tuple[Any, MagicMock, List[Tuple[Any, int, dict]]]:
        engine = make_engine()
        coordinator = make_coordinator(total_frames=total_frames)
        controller = make_controller(engine, coordinator)
        controller.is_playing = True
        controller._submit_decoded_frame_playback = MagicMock()
        return controller, coordinator, []

    def test_loop_playback_wraps_to_frame_zero(self, make_controller) -> None:
        controller, coordinator, _ = self._make_ticking_controller(make_controller)
        controller.loop_playback = True
        coordinator.tick.return_value = SimpleNamespace(frame_index=100)  # > out(99)
        frame_spy = _SignalSpy(controller.frame_changed)

        controller._on_play_tick()

        coordinator.seek_to_frame.assert_called_once_with(0, blocking=False)
        assert controller.current_frame == 0
        assert frame_spy.values == [0]
        controller._submit_decoded_frame_playback.assert_not_called()

    def test_no_loop_stops_at_end(self, make_controller) -> None:
        controller, coordinator, _ = self._make_ticking_controller(make_controller)
        coordinator.tick.return_value = SimpleNamespace(frame_index=100)

        controller._on_play_tick()

        assert controller.is_playing is False
        coordinator.stop_playback.assert_called_once()
        controller._submit_decoded_frame_playback.assert_not_called()

    def test_in_out_range_wraps_to_in_frame(self, make_controller) -> None:
        controller, coordinator, _ = self._make_ticking_controller(make_controller)
        controller.set_in_out_range(10, 50)
        coordinator.tick.return_value = SimpleNamespace(frame_index=51)
        frame_spy = _SignalSpy(controller.frame_changed)

        controller._on_play_tick()

        coordinator.seek_to_frame.assert_called_once_with(10, blocking=False)
        assert controller.current_frame == 10
        assert frame_spy.values == [10]

    def test_stale_pre_in_frame_from_departing_clip_is_dropped(
        self, make_controller
    ) -> None:
        controller, coordinator, _ = self._make_ticking_controller(make_controller)
        controller.set_in_out_range(10, 50)
        coordinator.tick.return_value = SimpleNamespace(frame_index=5)

        controller._on_play_tick()

        controller._submit_decoded_frame_playback.assert_not_called()
        coordinator.seek_to_frame.assert_not_called()
        assert controller.current_frame == 0, "残留帧不应推进播放位置"

    def test_stale_post_out_frame_below_clock_position_is_dropped(
        self, make_controller
    ) -> None:
        controller, coordinator, _ = self._make_ticking_controller(make_controller)
        controller.set_in_out_range(10, 50)
        coordinator.tick.return_value = SimpleNamespace(frame_index=60)
        coordinator.session = SimpleNamespace(
            playback_clock=SimpleNamespace(current_frame=40), fps=25.0
        )

        controller._on_play_tick()

        controller._submit_decoded_frame_playback.assert_not_called()

    def test_genuine_range_crossing_wraps_when_clock_agrees(self, make_controller) -> None:
        controller, coordinator, _ = self._make_ticking_controller(make_controller)
        controller.set_in_out_range(10, 50)
        coordinator.tick.return_value = SimpleNamespace(frame_index=60)
        coordinator.session = SimpleNamespace(
            playback_clock=SimpleNamespace(current_frame=60), fps=25.0
        )

        controller._on_play_tick()

        coordinator.seek_to_frame.assert_called_once_with(10, blocking=False)
        assert controller.current_frame == 10

    def test_unknown_duration_never_stops_early(self, make_controller) -> None:
        controller, coordinator, _ = self._make_ticking_controller(
            make_controller, total_frames=0
        )
        coordinator.tick.return_value = SimpleNamespace(frame_index=10**6)

        controller._on_play_tick()

        assert controller.is_playing is True
        controller._submit_decoded_frame_playback.assert_called_once()

    def test_same_frame_is_held_without_resubmit(self, make_controller) -> None:
        controller, coordinator, _ = self._make_ticking_controller(make_controller)
        frame = SimpleNamespace(frame_index=42)
        coordinator.tick.return_value = frame
        controller._last_displayed_frame_index = 42
        controller._last_submitted_render_params_generation = (
            controller._render_params_generation
        )

        controller._on_play_tick()

        controller._submit_decoded_frame_playback.assert_not_called()

    def test_grade_param_change_forces_resubmit_of_held_frame(
        self, make_controller
    ) -> None:
        controller, coordinator, _ = self._make_ticking_controller(make_controller)
        frame = SimpleNamespace(frame_index=42)
        coordinator.tick.return_value = frame
        controller._last_displayed_frame_index = 42
        controller._last_submitted_render_params_generation = (
            controller._render_params_generation
        )

        controller.invalidate_cached_render_params()
        controller._on_play_tick()

        controller._submit_decoded_frame_playback.assert_called_once_with(frame, 42)

    def test_renderer_congestion_submits_once_then_skips(self, make_controller) -> None:
        engine = make_engine()
        renderer = MagicMock()
        renderer.get_renderer_stats.return_value = {
            "queue_depth": 5,  # >= max_queue(3)
            "dropped_preview_frames": 2,
            "coalesced_video_commands": 1,
        }
        engine.renderer = renderer
        coordinator = make_coordinator()
        controller = make_controller(engine, coordinator)
        controller.is_playing = True
        controller._submit_decoded_frame_playback = MagicMock()
        coordinator.tick.side_effect = [
            SimpleNamespace(frame_index=10),
            SimpleNamespace(frame_index=11),
        ]

        controller._on_play_tick()  # 拥塞探测：本 tick 仍提交
        assert controller._submit_decoded_frame_playback.call_count == 1

        controller._on_play_tick()  # 200ms 探测窗口内：跳过提交但推进时钟
        assert controller._submit_decoded_frame_playback.call_count == 1
        assert controller._last_displayed_frame_index == 11
        assert controller._last_displayed_decoded_frame is not None

    def test_tick_without_frame_restarts_stopped_clock(self, make_controller) -> None:
        engine = make_engine()
        coordinator = make_coordinator()
        coordinator.tick.return_value = None
        clock = MagicMock()
        clock.is_playing = False
        clock.current_frame = 5
        coordinator.session = SimpleNamespace(
            playback_clock=clock,
            worker_manager=SimpleNamespace(is_running=lambda: True),
            state=SimpleNamespace(name="READY"),
            fps=25.0,
        )
        controller = make_controller(engine, coordinator)
        controller.is_playing = True
        controller._submit_decoded_frame_playback = MagicMock()

        controller._on_play_tick()

        clock.start.assert_called_once()
        controller._submit_decoded_frame_playback.assert_not_called()

    def test_tick_without_any_path_stops_playback(self, make_controller) -> None:
        engine = make_engine()
        controller = make_controller(engine)
        controller._use_dual_path = False
        controller.is_playing = True

        controller._on_play_tick()

        assert controller.is_playing is False


class TestLegacyTickLoop:
    """旧架构 _on_play_tick 分支（clock + source_manager，无 coordinator）。"""

    def _make_legacy_controller(
        self, make_controller: Callable[..., Any]
    ) -> Tuple[Any, MagicMock, MagicMock, MagicMock]:
        engine = make_engine()
        controller = make_controller(engine)
        controller._use_dual_path = True
        controller._coordinator = None
        clock = MagicMock()
        clock.tick.return_value = 20
        source_manager = SimpleNamespace(total_frames=100)
        prefetch = MagicMock()
        prefetch.get_ready_frame.return_value = None
        controller._playback_clock = clock
        controller._source_manager = source_manager
        controller._proxy_prefetch_worker = prefetch
        controller._submit_frame_playback = MagicMock()
        controller.is_playing = True
        return controller, clock, source_manager, prefetch

    def test_ready_frame_is_submitted(self, make_controller) -> None:
        controller, clock, _, prefetch = self._make_legacy_controller(make_controller)
        frame = np.zeros((2, 2, 3), dtype=np.uint8)
        prefetch.get_ready_frame.return_value = (frame, 20)

        controller._on_play_tick()

        prefetch.get_ready_frame.assert_called_once_with(20, timeout=0.0)
        controller._submit_frame_playback.assert_called_once_with(frame, 20)
        assert controller._last_displayed_frame_index == 20

    def test_no_ready_frame_holds_last_frame(self, make_controller) -> None:
        controller, clock, _, prefetch = self._make_legacy_controller(make_controller)
        controller._last_displayed_frame_index = 7

        controller._on_play_tick()

        controller._submit_frame_playback.assert_not_called()

    def test_no_ready_frame_and_no_history_drops_tick(self, make_controller) -> None:
        controller, clock, _, prefetch = self._make_legacy_controller(make_controller)
        controller._last_displayed_frame_index = None

        controller._on_play_tick()

        controller._submit_frame_playback.assert_not_called()

    def test_in_out_wrap_on_legacy_path(self, make_controller) -> None:
        controller, clock, _, prefetch = self._make_legacy_controller(make_controller)
        controller.set_in_out_range(10, 50)
        clock.tick.return_value = 51

        controller._on_play_tick()

        clock.seek_to_frame.assert_called_once_with(10)
        prefetch.request_seek.assert_called_once_with(10)

    def test_loop_wrap_on_legacy_path(self, make_controller) -> None:
        controller, clock, _, prefetch = self._make_legacy_controller(make_controller)
        controller.loop_playback = True
        clock.tick.return_value = 100  # > out(99)

        controller._on_play_tick()

        clock.seek_to_frame.assert_called_once_with(0)
        prefetch.request_seek.assert_called_once_with(0)

    def test_stop_at_end_on_legacy_path(self, make_controller) -> None:
        controller, clock, _, prefetch = self._make_legacy_controller(make_controller)
        clock.tick.return_value = 100

        controller._on_play_tick()

        assert controller.is_playing is False
        clock.stop.assert_called_once()


# =============================================================================
# 首帧三级处理与 load_video 异常路径
# =============================================================================


def _session_for_first_frame(
    master_frame: Optional[Any],
    proxy_entry_frame: Optional[Any],
) -> MagicMock:
    session = MagicMock()
    session.get_master_frame_blocking.return_value = master_frame
    session._frame_queue = (
        MagicMock() if proxy_entry_frame is not None else None
    )
    if proxy_entry_frame is not None:
        session._frame_queue.dequeue.return_value = SimpleNamespace(
            frame=proxy_entry_frame
        )
    session._source_manager = None
    return session


class TestFirstFrameThreeLevels:
    """_show_first_frame_via_coordinator：master → proxy → placeholder。"""

    def test_master_ok_path(self, make_controller) -> None:
        engine = make_engine()
        coordinator = make_coordinator()
        packed = make_packed_frame(0)
        coordinator.session = _session_for_first_frame(packed, None)
        controller = make_controller(engine, coordinator)

        result = controller._show_first_frame_via_coordinator()

        assert result.status is FirstFrameStatus.MASTER_OK
        assert result.is_displayable is True
        assert result.used_proxy is False
        assert result.should_fallback_v2 is False
        engine.set_image.assert_called_once()

    def test_master_timeout_falls_back_to_proxy(self, make_controller) -> None:
        engine = make_engine()
        coordinator = make_coordinator()
        proxy = make_packed_frame(0, color=(1, 2, 3))
        coordinator.session = _session_for_first_frame(None, proxy)
        controller = make_controller(engine, coordinator)

        result = controller._show_first_frame_via_coordinator()

        assert result.status is FirstFrameStatus.PROXY_OK
        assert result.used_proxy is True
        assert result.frame is proxy
        engine.set_image.assert_called_once()

    def test_all_levels_fail_keeps_v3_placeholder(self, make_controller) -> None:
        engine = make_engine()
        coordinator = make_coordinator()
        coordinator.session = _session_for_first_frame(None, None)
        controller = make_controller(engine, coordinator)

        result = controller._show_first_frame_via_coordinator()

        assert result.status is FirstFrameStatus.PLACEHOLDER
        assert result.needs_placeholder is True
        assert result.fallback_reason is None, "v2 链路已移除，永不 fallback"
        engine.set_image.assert_not_called()

    def test_unloaded_coordinator_reports_fallback_reason(self, make_controller) -> None:
        engine = make_engine()
        coordinator = make_coordinator()
        coordinator.is_loaded = False
        controller = make_controller(engine, coordinator)

        result = controller._show_first_frame_via_coordinator()

        assert result.status is FirstFrameStatus.NOT_READY
        assert result.fallback_reason is VideoPipelineFallbackReason.COORDINATOR_LOAD_FAILED
        assert result.should_fallback_v2 is True

    def test_render_failure_falls_through_to_proxy(self, make_controller) -> None:
        engine = make_engine()
        engine.set_image.side_effect = RuntimeError("GL context lost")
        coordinator = make_coordinator()
        coordinator.session = _session_for_first_frame(
            make_packed_frame(0), make_packed_frame(0)
        )
        controller = make_controller(engine, coordinator)

        result = controller._show_first_frame_via_coordinator()

        # master 渲染失败 → 尝试 proxy；proxy 渲染同样失败 → placeholder
        assert result.status is FirstFrameStatus.PLACEHOLDER
        assert engine.set_image.call_count == 2


class TestFirstFrameResultProperties:
    """FirstFrameResult 属性分组。"""

    def test_displayable_statuses(self) -> None:
        for status in (
            FirstFrameStatus.MASTER_OK,
            FirstFrameStatus.PROXY_OK,
            FirstFrameStatus.FRAME_PATH_DEGRADED,
        ):
            assert FirstFrameResult(status=status).is_displayable is True

    def test_placeholder_statuses(self) -> None:
        for status in (
            FirstFrameStatus.NOT_READY,
            FirstFrameStatus.PLACEHOLDER,
            FirstFrameStatus.TIMEOUT_PROXY,
        ):
            result = FirstFrameResult(status=status)
            assert result.needs_placeholder is True
            assert result.is_displayable is False

    def test_fallback_v2_requires_reason(self) -> None:
        assert (
            FirstFrameResult(
                status=FirstFrameStatus.ERROR,
                fallback_reason=VideoPipelineFallbackReason.COORDINATOR_LOAD_FAILED,
            ).should_fallback_v2
            is True
        )
        assert (
            FirstFrameResult(status=FirstFrameStatus.ERROR).should_fallback_v2 is False
        )
        assert (
            FirstFrameResult(
                status=FirstFrameStatus.ERROR,
                fallback_reason=VideoPipelineFallbackReason.NONE,
            ).should_fallback_v2
            is False
        )


class TestLoadVideoPaths:
    """load_video 成功/失败/异常路径（PlaybackCoordinator 以 mock 注入）。"""

    def test_successful_load_emits_signals_and_shows_first_frame(
        self, make_controller, qapp
    ) -> None:
        engine = make_engine()
        coordinator = make_coordinator(total_frames=137, fps=25.0)
        coordinator.get_video_info.return_value = {
            "codec": "h264",
            "pixel_format": "yuv420p",
            "width": 1920,
            "height": 1080,
            "display_width": 1920,
            "display_height": 1080,
        }
        coordinator.session = _session_for_first_frame(make_packed_frame(0), None)
        controller = make_controller(engine, coordinator)
        loaded_spy = _SignalSpy(controller.video_loaded)
        total_spy = _SignalSpy(controller.total_frames_changed)
        frame_spy = _SignalSpy(controller.frame_changed)

        with patch(
            "src.shared.video.playback_state.PlaybackCoordinator",
            return_value=coordinator,
        ):
            assert controller.load_video("/tmp/coverage-load.mp4") is True

        assert controller.current_path == "/tmp/coverage-load.mp4"
        assert controller.current_frame == 0
        coordinator.load.assert_called_once_with("/tmp/coverage-load.mp4")
        assert loaded_spy.values == ["/tmp/coverage-load.mp4"]
        assert total_spy.values == [137]
        assert frame_spy.values == [0]
        engine.set_image.assert_called_once()
        assert controller._use_dual_path is True

    def test_load_failure_returns_false_and_cleans_up(self, make_controller, qapp) -> None:
        engine = make_engine()
        coordinator = make_coordinator()
        coordinator.load.return_value = False
        controller = make_controller(engine, coordinator)

        with patch(
            "src.shared.video.playback_state.PlaybackCoordinator",
            return_value=coordinator,
        ):
            assert controller.load_video("/tmp/missing.mp4") is False

        # 入口 unload_video() 一次 + 失败清理一次
        assert coordinator.unload.call_count == 2
        assert controller._coordinator is None

    def test_load_exception_returns_false_and_cleans_up(
        self, make_controller, qapp
    ) -> None:
        engine = make_engine()
        coordinator = make_coordinator()
        coordinator.load.side_effect = RuntimeError("probe crashed")
        controller = make_controller(engine, coordinator)

        with patch(
            "src.shared.video.playback_state.PlaybackCoordinator",
            return_value=coordinator,
        ):
            assert controller.load_video("/tmp/corrupt.mp4") is False

        assert coordinator.unload.call_count == 2
        assert controller._coordinator is None

    def test_dual_path_disabled_fails_fast(self, make_controller) -> None:
        engine = make_engine()
        coordinator = make_coordinator()
        controller = make_controller(engine, coordinator)
        controller._use_dual_path = False

        assert controller.load_video("/tmp/any.mp4") is False
        coordinator.load.assert_not_called()

    def test_first_frame_placeholder_still_counts_as_loaded(
        self, make_controller, qapp
    ) -> None:
        engine = make_engine()
        coordinator = make_coordinator()
        coordinator.session = _session_for_first_frame(None, None)
        controller = make_controller(engine, coordinator)

        with patch(
            "src.shared.video.playback_state.PlaybackCoordinator",
            return_value=coordinator,
        ):
            assert controller.load_video("/tmp/slowprores.mov") is True

        assert controller._coordinator is not None
        assert controller._use_dual_path is True

    def test_coordinator_init_failure_fails_load(self, make_controller, qapp) -> None:
        engine = make_engine()
        controller = make_controller(engine)
        controller._use_dual_path = True

        with patch(
            "src.shared.video.playback_state.PlaybackCoordinator",
            side_effect=ImportError("playback_state gone"),
        ):
            assert controller.load_video("/tmp/any.mp4") is False

        assert controller._coordinator is None


class TestDualPathComponentInit:
    """_init_dual_path_components：SourceManager 打开失败/异常的降级。"""

    def test_components_created_on_success(self, make_controller) -> None:
        engine = make_engine()
        controller = make_controller(engine)
        controller._use_dual_path = True
        controller.current_path = "/tmp/plain.mp4"

        source_manager = MagicMock()
        source_manager.open.return_value = True
        source_manager.fps = SimpleNamespace(to_float=lambda: 24.0)
        source_manager.total_frames = 240
        source_manager.proxy_size = (960, 540)

        with patch("src.shared.video.SourceManager", return_value=source_manager) as MS, \
             patch("src.shared.video.PlaybackClock") as MClock, \
             patch("src.shared.video.FrameScheduler") as MSched, \
             patch("src.shared.media.factory.MediaSourceFactory") as MFactory:
            MFactory.probe_video_metadata.return_value = None
            controller._init_dual_path_components()

        assert controller._use_dual_path is True
        assert controller._source_manager is source_manager
        assert controller._playback_clock is MClock.return_value
        assert controller._frame_scheduler is MSched.return_value
        MS.assert_called_once()
        MClock.assert_called_once_with(fps=24.0, total_frames=240)

    def test_open_failure_disables_dual_path(self, make_controller) -> None:
        engine = make_engine()
        controller = make_controller(engine)
        controller._use_dual_path = True
        controller.current_path = "/tmp/plain.mp4"

        source_manager = MagicMock()
        source_manager.open.return_value = False

        with patch("src.shared.video.SourceManager", return_value=source_manager), \
             patch("src.shared.media.factory.MediaSourceFactory") as MFactory:
            MFactory.probe_video_metadata.return_value = None
            controller._init_dual_path_components()

        assert controller._use_dual_path is False
        assert controller._source_manager is None

    def test_import_failure_disables_dual_path(self, make_controller) -> None:
        engine = make_engine()
        controller = make_controller(engine)
        controller._use_dual_path = True
        controller.current_path = "/tmp/plain.mp4"

        with patch(
            "src.shared.video.SourceManager", side_effect=ImportError("gone")
        ), patch("src.shared.media.factory.MediaSourceFactory") as MFactory:
            MFactory.probe_video_metadata.return_value = None
            controller._init_dual_path_components()

        assert controller._use_dual_path is False
        assert controller._source_manager is None
        assert controller._playback_clock is None


# =============================================================================
# 帧提交路径（packed / planar / GPU / ndarray）
# =============================================================================


class TestFrameSubmitPaths:
    """_submit_frame_playback / _submit_decoded_frame_playback 分流。"""

    def _submit_controller(self, make_controller: Callable[..., Any]):
        engine = make_engine()
        renderer = MagicMock()
        engine.renderer = renderer
        controller = make_controller(engine, make_coordinator())
        return controller, renderer

    def test_packed_decoded_frame_submits_with_reason(self, make_controller) -> None:
        controller, renderer = self._submit_controller(make_controller)
        frame = make_packed_frame(7)

        controller._submit_decoded_frame_playback(frame, 7, quality="master", reason="seek")

        cmd, payload = renderer.submit.call_args.args
        assert cmd == RenderCommand.RENDER_VIDEO_FRAME
        assert payload["decoded_frame"] is frame
        assert payload["frame_index"] == 7
        assert payload["quality"] == "master"
        assert payload["reason"] == "seek"
        assert payload["drop_if_stale"] is True
        assert controller.current_frame == 7
        assert controller._last_displayed_decoded_frame is frame
        assert controller._last_displayed_frame_index == 7

    def test_planar_frame_submits_with_yuv_metadata(self, make_controller) -> None:
        controller, renderer = self._submit_controller(make_controller)
        renderer._yuv_gpu_supported = False
        frame = make_planar_frame(3)

        controller._submit_decoded_frame_playback(frame, 3, reason="playback")

        cmd, payload = renderer.submit.call_args.args
        assert cmd == RenderCommand.RENDER_VIDEO_FRAME
        assert payload["decoded_frame"] is frame
        assert payload["yuv_matrix"] == 0  # BT709 默认
        assert payload["color_range"] == 0  # LIMITED 默认
        # macOS 上 renderer 必须通过 YUV GPU self-test 才启用 GPU 上传
        expected_gpu_upload = False if sys.platform == "darwin" else True
        assert payload["use_yuv_gpu_upload"] is expected_gpu_upload

    def test_gpu_decoded_frame_uses_gpu_lane(self, make_controller) -> None:
        controller, renderer = self._submit_controller(make_controller)
        plane = GpuFramePlane(
            component="RGBA", api="opengl", texture_id=7, target="2d", width=64, height=64
        )
        gpu_frame = GpuDecodedFrame(
            frame_index=3,
            pts=0,
            time_seconds=0.0,
            width=64,
            height=64,
            visible_rect=None,
            pixel_format="rgba",
            bit_depth=8,
            planes=(plane,),
            decode_path="hwaccel",
        )

        controller._submit_decoded_frame_playback(gpu_frame, 3)

        cmd, payload = renderer.submit.call_args.args
        assert cmd == RenderCommand.RENDER_VIDEO_GPU_FRAME
        assert payload["gpu_decoded_frame"] is gpu_frame
        assert payload["frame_index"] == 3
        assert payload["decode_path"] == "hwaccel"
        assert controller._last_zero_copy is True
        assert controller._last_displayed_frame_index == 3
        # 标记已释放，避免 __del__ 触发未释放警告
        gpu_frame.release.release()

    def test_plain_ndarray_falls_back_to_frame_submit(self, make_controller) -> None:
        controller, renderer = self._submit_controller(make_controller)
        array = np.zeros((4, 4, 3), dtype=np.uint8)

        controller._submit_decoded_frame_playback(array, 2)

        cmd, payload = renderer.submit.call_args.args
        assert cmd == RenderCommand.RENDER_VIDEO_FRAME
        assert payload["frame"] is array

    def test_unknown_frame_type_is_rejected(self, make_controller) -> None:
        controller, renderer = self._submit_controller(make_controller)

        controller._submit_decoded_frame_playback("not-a-frame", 2)  # type: ignore[arg-type]

        renderer.submit.assert_not_called()

    def test_submit_without_renderer_is_noop(self, make_controller) -> None:
        engine = make_engine()
        controller = make_controller(engine, make_coordinator())  # renderer=None
        array = np.zeros((4, 4, 3), dtype=np.uint8)

        controller._submit_frame_playback(array, 5)

        assert controller.current_frame == 0, "无 renderer 时不推进帧号"

    def test_seek_reason_sets_direct_render_flag(self, make_controller) -> None:
        controller, renderer = self._submit_controller(make_controller)
        frame = np.zeros((4, 4, 3), dtype=np.uint8)

        controller._submit_frame_playback(frame, 5, reason="seek")
        assert controller._last_frame_ready_direct_render is True

        controller._last_frame_ready_direct_render = False
        controller._submit_frame_playback(frame, 6, reason="playback")
        assert controller._last_frame_ready_direct_render is False

    def test_4k_packed_warning_emitted_once(self, make_controller, caplog) -> None:
        controller, renderer = self._submit_controller(make_controller)
        big = make_packed_frame(0, width=3840, height=8)

        with caplog.at_level(logging.WARNING, logger="src.shared.video.video_playback"):
            controller._submit_decoded_frame_playback(big, 0)
            count_first = sum(
                "4K playback using packed_rgb_cpu_upload" in r.message
                for r in caplog.records
            )
            controller._submit_decoded_frame_playback(big, 1)
            count_second = sum(
                "4K playback using packed_rgb_cpu_upload" in r.message
                for r in caplog.records
            )

        assert count_first == 1
        assert count_second == 1, "日志节流：第二次提交不再重复警告"


# =============================================================================
# 帧转换（_process_decoded_frame / _convert_planar_to_rgb）
# =============================================================================


class TestDecodedFrameProcessing:
    """CPU 帧转换：BGR 直通、RGB 翻转、16bit 缩放、异常路径。"""

    def _controller(self, make_controller: Callable[..., Any]):
        return make_controller(make_engine(), make_coordinator())

    def test_bgr24_uint8_passthrough(self, make_controller) -> None:
        controller = self._controller(make_controller)
        frame = make_packed_frame(0, pixel_format="bgr24")

        result = controller._process_decoded_frame(frame)
        assert result is frame.data

    def test_bgra_uint8_strips_alpha(self, make_controller) -> None:
        controller = self._controller(make_controller)
        data = np.zeros((2, 2, 4), dtype=np.uint8)
        data[..., 0] = 10
        frame = DecodedFrame(
            frame_index=0, pts=0, time_seconds=0.0, data=data, pixel_format="bgra"
        )

        result = controller._process_decoded_frame(frame)
        assert result is not None and result.shape == (2, 2, 3)
        assert int(result[0, 0, 0]) == 10

    def test_rgb24_flips_channels_to_bgr(self, make_controller) -> None:
        controller = self._controller(make_controller)
        frame = make_packed_frame(0, color=(255, 0, 0), pixel_format="rgb24")

        result = controller._process_decoded_frame(frame)
        assert result is not None
        assert int(result[0, 0, 0]) == 0
        assert int(result[0, 0, 2]) == 255

    def test_rgb48_uint16_scales_to_uint8(self, make_controller) -> None:
        controller = self._controller(make_controller)
        data = np.zeros((2, 2, 3), dtype=np.uint16)
        data[..., 0] = 65535
        frame = DecodedFrame(
            frame_index=0,
            pts=0,
            time_seconds=0.0,
            data=data,
            pixel_format="rgb48le",
        )

        result = controller._process_decoded_frame(frame)
        assert result is not None
        assert int(result[0, 0, 0]) == 0  # R → BGR 末位
        assert int(result[0, 0, 2]) == 255

    def test_bgr16bit_truncates_to_black_bug_c2_01(self, make_controller) -> None:
        """BUG-C2-01（已修复，AUD-39600）：16-bit BGR 路径曾除以
        (1<<bit_depth) 后直接 astype(uint8)，白色 65535 被截断为 0（黑）。
        现与 worker 的 _process_frame 一致使用 /65535*255 正确缩放。
        详见 BUGS-agent-C2.md。"""
        controller = self._controller(make_controller)
        data = np.full((2, 2, 4), 65535, dtype=np.uint16)
        frame = DecodedFrame(
            frame_index=0, pts=0, time_seconds=0.0, data=data, pixel_format="bgra64"
        )

        result = controller._process_decoded_frame(frame)
        assert result is not None
        assert result.dtype == np.uint8
        # AUD-39600 修复后：白色 → 白（不再是黑）
        assert int(result[0, 0, 0]) == 255
        assert int(result[0, 0, 1]) == 255
        assert int(result[0, 0, 2]) == 255

    def test_float_rgb_is_clipped_and_scaled(self, make_controller) -> None:
        controller = self._controller(make_controller)
        data = np.array([[[2.0, 0.5, 0.0]]], dtype=np.float32)
        frame = DecodedFrame(
            frame_index=0, pts=0, time_seconds=0.0, data=data, pixel_format="rgb24"
        )

        result = controller._process_decoded_frame(frame)
        assert result is not None
        assert result.dtype == np.uint8
        # RGB (clip→1.0, 0.5, 0.0) * 255 = (255, 127, 0) → 翻转 BGR
        assert result[0, 0].tolist() == [0, 127, 255]

    def test_frame_without_data_or_planes_returns_none(self, make_controller) -> None:
        controller = self._controller(make_controller)
        frame = DecodedFrame(frame_index=0, pts=0, time_seconds=0.0)

        assert controller._process_decoded_frame(frame) is None

    def test_conversion_exception_returns_none(self, make_controller) -> None:
        controller = self._controller(make_controller)

        class _ExplodingFrame:
            @property
            def data(self) -> np.ndarray:
                raise ValueError("boom")

        assert controller._process_decoded_frame(_ExplodingFrame()) is None


class TestPlanarConversion:
    """YUV planar → BGR CPU 转换（ColorConverter）。"""

    def test_limited_range_black_converts_to_black(self, make_controller) -> None:
        controller = make_controller(make_engine(), make_coordinator())
        frame = make_planar_frame(0)  # Y=16 U=V=128 (limited black)

        result = controller._convert_planar_to_rgb(frame)

        assert result is not None
        assert result.shape == (4, 4, 3)
        assert result.dtype == np.uint8
        # limited-range 黑（Y=16 U=V=128）应转换为近黑（逐像素断言，避免聚合）
        assert int(result[0, 0, 0]) <= 10
        assert int(result[3, 3, 2]) <= 10

    def test_metadata_color_fields_are_parsed(self, make_controller) -> None:
        controller = make_controller(make_engine(), make_coordinator())
        frame = make_planar_frame(0, pixel_format="yuv422p")
        frame.metadata = SimpleNamespace(
            color_matrix="smpte170m", color_range="full", bit_depth=8
        )

        result = controller._convert_planar_to_rgb(frame)
        assert result is not None and result.shape == (4, 4, 3)

    def test_insufficient_planes_returns_none(self, make_controller) -> None:
        controller = make_controller(make_engine(), make_coordinator())
        frame = DecodedFrame(
            frame_index=0,
            pts=0,
            time_seconds=0.0,
            planes=(np.zeros((4, 4), np.uint8),),
            pixel_format="yuv420p",
        )

        assert controller._convert_planar_to_rgb(frame) is None

    def test_converter_failure_returns_none(self, make_controller) -> None:
        from src.shared.media.color_decode import ColorConverter

        controller = make_controller(make_engine(), make_coordinator())
        bad_plane = MagicMock()
        frame = SimpleNamespace(
            planes=(bad_plane, bad_plane, bad_plane),
            metadata=None,
            pixel_format="yuv420p",
        )

        with patch.object(
            ColorConverter, "yuv_to_rgb", side_effect=RuntimeError("bad planes")
        ):
            assert controller._convert_planar_to_rgb(frame) is None


class TestFramePathHelpers:
    """_get_frame_path / _get_yuv_matrix / _get_color_range。"""

    def _controller(self, make_controller: Callable[..., Any]):
        return make_controller(make_engine(), make_coordinator())

    def test_frame_path_none_is_unknown(self, make_controller) -> None:
        assert self._controller(make_controller)._get_frame_path(None) == "unknown"

    def test_frame_path_nv12(self, make_controller) -> None:
        controller = self._controller(make_controller)
        assert controller._get_frame_path(make_planar_frame(0, "nv12")) == (
            "nv12_r8_rg8_upload"
        )

    def test_frame_path_p010(self, make_controller) -> None:
        controller = self._controller(make_controller)
        y = np.zeros((4, 4), dtype=np.uint16)
        u = np.zeros((2, 2), dtype=np.uint16)
        v = np.zeros((2, 2), dtype=np.uint16)
        frame = DecodedFrame(
            frame_index=0, pts=0, time_seconds=0.0, planes=(y, u, v), pixel_format="p010le"
        )
        assert controller._get_frame_path(frame) == "p010_r16ui_upload"

    def test_frame_path_planar_8bit(self, make_controller) -> None:
        controller = self._controller(make_controller)
        assert controller._get_frame_path(make_planar_frame(0)) == (
            "planar_yuv_r8_upload"
        )

    def test_frame_path_planar_16bit(self, make_controller) -> None:
        controller = self._controller(make_controller)
        y = np.zeros((4, 4), dtype=np.uint16)
        frame = DecodedFrame(
            frame_index=0,
            pts=0,
            time_seconds=0.0,
            planes=(y, y, y),
            pixel_format="yuv420p16le",
        )
        assert controller._get_frame_path(frame) == "planar_yuv_r16ui_upload"

    def test_frame_path_packed(self, make_controller) -> None:
        controller = self._controller(make_controller)
        assert controller._get_frame_path(make_packed_frame(0)) == (
            "packed_rgb_cpu_upload"
        )

    def test_yuv_matrix_mapping(self, make_controller) -> None:
        controller = self._controller(make_controller)
        assert controller._get_yuv_matrix(SimpleNamespace()) == 0  # 无 metadata → BT709
        for matrix_str, expected in (
            ("bt709", 0),
            ("smpte170m", 2),
            ("bt470bg", 2),
            ("bt2020nc", 1),
            ("bt2020", 1),
            ("something-else", 0),
        ):
            frame = SimpleNamespace(
                metadata=SimpleNamespace(color_matrix=matrix_str, color_range="limited")
            )
            assert controller._get_yuv_matrix(frame) == expected

    def test_color_range_mapping(self, make_controller) -> None:
        controller = self._controller(make_controller)
        assert controller._get_color_range(SimpleNamespace()) == 0
        for range_str, expected in (
            ("limited", 0),
            ("tv", 0),
            ("full", 1),
            ("pc", 1),
            ("garbage", 0),
        ):
            frame = SimpleNamespace(
                metadata=SimpleNamespace(color_matrix="bt709", color_range=range_str)
            )
            assert controller._get_color_range(frame) == expected


# =============================================================================
# 帧源尺寸 / 场景几何 / 代理分辨率
# =============================================================================


class TestProxySizeComputation:
    """代理分辨率计算（长边限制 + 64px 下限）。"""

    def test_4k_downscales_to_1920_long_edge(self, make_controller) -> None:
        controller = make_controller(make_engine(), make_coordinator())
        controller._update_proxy_size_from_info(3840, 2160)
        assert controller._proxy_size == (1920, 1080)

    def test_small_video_keeps_native_size(self, make_controller) -> None:
        controller = make_controller(make_engine(), make_coordinator())
        controller._update_proxy_size_from_info(640, 360)
        assert controller._proxy_size == (640, 360)

    def test_min_edge_clamped_to_64(self, make_controller) -> None:
        engine = make_engine(video_playback_resolution=100)
        controller = make_controller(engine, make_coordinator())
        controller._update_proxy_size_from_info(200, 50)
        # scale=0.5 → (100, 25) → 高度钳制到 64
        assert controller._proxy_size == (100, 64)

    def test_update_proxy_size_uses_media_display_size(self, make_controller) -> None:
        controller = make_controller(make_engine(), make_coordinator())
        controller._media_display_size = (3840, 2160)
        controller._update_proxy_size()
        assert controller._proxy_size == (1920, 1080)

    def test_update_proxy_size_without_media_size_is_noop(self, make_controller) -> None:
        controller = make_controller(make_engine(), make_coordinator())
        controller._update_proxy_size()
        assert controller._proxy_size is None

    def test_update_viewport_size_propagates_to_engine(self, make_controller) -> None:
        engine = make_engine()
        controller = make_controller(engine, make_coordinator())
        controller.update_viewport_size(1920, 1080)
        assert engine.viewport_size == (1920, 1080)


class TestDisplayGeometry:
    """显示场景尺寸与呈现几何。"""

    def test_set_display_scene_size_syncs_engine(self, make_controller) -> None:
        engine = make_engine()
        controller = make_controller(engine, make_coordinator())
        gen0 = controller._render_params_generation

        controller.set_display_scene_size((1920, 1080))
        assert engine.scene_size == (1920.0, 1080.0)
        assert controller._render_params_generation == gen0 + 1

        # 相同尺寸重复设置不再失效缓存
        controller.set_display_scene_size((1920, 1080))
        assert controller._render_params_generation == gen0 + 1

    def test_clear_display_scene_size_restores_engine(self, make_controller) -> None:
        engine = make_engine()
        controller = make_controller(engine, make_coordinator())
        controller.set_display_scene_size((1920, 1080))
        controller.clear_display_scene_size()

        assert controller._display_scene_size is None
        assert engine.scene_size is None

    def test_invalid_display_size_clears_override(self, make_controller) -> None:
        engine = make_engine()
        controller = make_controller(engine, make_coordinator())
        controller.set_display_scene_size((1920, 1080))

        controller.set_display_scene_size((0, 100))

        assert controller._display_scene_size is None
        assert engine.scene_size is None

    def test_set_media_display_size_valid_and_invalid(self, make_controller) -> None:
        engine = make_engine()
        controller = make_controller(engine, make_coordinator())

        controller.set_media_display_size((1280, 720))
        assert controller.get_media_display_size() == (1280, 720)
        assert controller._display_scene_size == (1280, 720)

        controller.set_media_display_size(None)
        assert controller.get_media_display_size() is None

        controller.set_media_display_size((0, -5))
        assert controller.get_media_display_size() is None

    def test_presentation_size_prefers_media_display(self, make_controller) -> None:
        controller = make_controller(make_engine(), make_coordinator())
        controller.set_media_display_size((1920, 1080))

        assert controller._presentation_size_for_frame(640, 360) == (1920, 1080)

    def test_presentation_size_falls_back_and_clamps(self, make_controller) -> None:
        controller = make_controller(make_engine(), make_coordinator())

        assert controller._presentation_size_for_frame(640, 360) == (640, 360)
        assert controller._presentation_size_for_frame(0, 0) == (1, 1)

    def test_fit_render_target_preserves_aspect(self) -> None:
        fitted = VideoPlaybackEngine._fit_render_target_to_presentation(
            (1000, 500), (16, 9)
        )
        assert fitted == (889, 500)

    def test_metadata_display_size_with_rotation_swap(self, make_controller) -> None:
        engine = make_engine()
        controller = make_controller(engine, make_coordinator())
        metadata = SimpleNamespace(display_size=(1920, 1080), rotation=90)

        controller._set_media_display_size_from_metadata(metadata)

        assert controller.get_media_display_size() == (1080, 1920)

    def test_metadata_without_rotation_keeps_size(self, make_controller) -> None:
        engine = make_engine()
        controller = make_controller(engine, make_coordinator())
        metadata = SimpleNamespace(display_size=(1920, 1080), rotation=0)

        controller._set_media_display_size_from_metadata(metadata)

        assert controller.get_media_display_size() == (1920, 1080)

    def test_bad_metadata_is_ignored(self, make_controller) -> None:
        engine = make_engine()
        controller = make_controller(engine, make_coordinator())

        controller._set_media_display_size_from_metadata(None)
        controller._set_media_display_size_from_metadata(SimpleNamespace(display_size=None))

        assert controller.get_media_display_size() is None


class TestDecodedFrameSourceSize:
    """_get_decoded_frame_source_size：metadata → extra → (0,0)。"""

    def _controller(self, make_controller: Callable[..., Any]):
        return make_controller(make_engine(), make_coordinator())

    def test_none_frame(self, make_controller) -> None:
        assert self._controller(make_controller)._get_decoded_frame_source_size(None) == (
            0,
            0,
        )

    def test_metadata_size(self, make_controller) -> None:
        controller = self._controller(make_controller)
        frame = SimpleNamespace(metadata=SimpleNamespace(width=3840, height=2160))
        assert controller._get_decoded_frame_source_size(frame) == (3840, 2160)

    def test_extra_original_size(self, make_controller) -> None:
        controller = self._controller(make_controller)
        frame = SimpleNamespace(
            metadata=None,
            extra={"original_width": 1920, "original_height": 1080},
        )
        assert controller._get_decoded_frame_source_size(frame) == (1920, 1080)

    def test_extra_source_size(self, make_controller) -> None:
        controller = self._controller(make_controller)
        frame = SimpleNamespace(
            metadata=None,
            extra={"source_width": 1280, "source_height": 720},
        )
        assert controller._get_decoded_frame_source_size(frame) == (1280, 720)

    def test_no_size_info(self, make_controller) -> None:
        controller = self._controller(make_controller)
        frame = SimpleNamespace(metadata=None, extra={})
        assert controller._get_decoded_frame_source_size(frame) == (0, 0)


class TestPlaybackSceneSize:
    """_get_playback_scene_size 的优先级链。"""

    def test_display_scene_wins(self, make_controller) -> None:
        engine = make_engine()
        engine.scene_size = (3840.0, 2160.0)
        controller = make_controller(engine, make_coordinator())
        controller.set_display_scene_size((1920, 1080))

        assert controller._get_playback_scene_size(960, 540) == (1920.0, 1080.0)

    def test_engine_scene_size_second(self, make_controller) -> None:
        engine = make_engine()
        engine.scene_size = (3840.0, 2160.0)
        controller = make_controller(engine, make_coordinator())

        assert controller._get_playback_scene_size(960, 540) == (3840.0, 2160.0)

    def test_source_size_last(self, make_controller) -> None:
        controller = make_controller(make_engine(), make_coordinator())

        assert controller._get_playback_scene_size(1280, 720) == (1280.0, 720.0)
        assert controller._get_playback_scene_size() is None


# =============================================================================
# 渲染参数与 raw 预览
# =============================================================================


class TestRenderParamsAndRawPreview:
    """raw 预览参数映射 + 视口回退。"""

    def test_raw_enabled_maps_values(self, make_controller) -> None:
        engine = make_engine()
        engine.current_raw_params = {
            "enabled": True,
            "exposure": 1.5,
            "saturation": 50.0,
            "shadows": 0.2,
            "highlights": -0.1,
            "contrast": 1.2,
            "lift": 0.01,
            "gain": 0.9,
        }
        controller = make_controller(engine, make_coordinator())

        raw = controller._build_video_raw_params()

        assert raw["exposure"] == 1.5
        assert raw["saturation"] == 0.5, "0~100 的 saturation 归一到 0~1"
        assert raw["shadows"] == 0.2
        assert raw["contrast"] == 1.2

    def test_raw_saturation_above_two_treated_as_percent(self, make_controller) -> None:
        engine = make_engine()
        engine.current_raw_params = {"enabled": True, "saturation": 100.0}
        controller = make_controller(engine, make_coordinator())

        assert controller._build_video_raw_params()["saturation"] == 1.0

    def test_raw_identity_when_disabled(self, make_controller) -> None:
        engine = make_engine()
        engine.current_raw_params = {"enabled": False, "exposure": 9.9}
        controller = make_controller(engine, make_coordinator())

        raw = controller._build_video_raw_params()
        assert raw["exposure"] == 0.0
        assert raw["saturation"] == 1.0
        assert raw["color_space"] == -1

    def test_invalid_viewport_falls_back_to_frame_size(self, make_controller) -> None:
        engine = make_engine()
        engine.viewport_size = (0, 0)
        controller = make_controller(engine, make_coordinator())

        params = controller._get_playback_render_params(640, 360)
        assert params["viewport_size"] == (640, 360)

    def test_invalid_visible_rect_uses_scene_fallback(self, make_controller) -> None:
        engine = make_engine()
        engine.viewport_size = (960, 540)
        engine.visible_rect = (0, 0, 0, 0)
        engine.scene_size = None
        controller = make_controller(engine, make_coordinator())

        params = controller._get_playback_render_params(640, 360)
        assert params["scene_visible_rect"] == (0.0, 0.0, 640.0, 360.0)

    def test_static_preview_params_use_engine_viewport(self, make_controller) -> None:
        engine = make_engine()
        controller = make_controller(engine, make_coordinator())

        params = controller._get_static_preview_render_params(320, 180)
        assert params["viewport_size"] == (960, 540)
        assert params["visible_rect"] == (0.0, 0.0, 320.0, 180.0)
        assert params["render_reason"] == "static_preview"

    def test_static_preview_params_fallback_without_viewport(self, make_controller) -> None:
        engine = make_engine()
        engine.viewport_size = None
        controller = make_controller(engine, make_coordinator())

        params = controller._get_static_preview_render_params(320, 180)
        assert params["viewport_size"] == (320, 180)

    def test_invalidate_cached_params_bumps_generation(self, make_controller) -> None:
        engine = make_engine()
        renderer = MagicMock()
        engine.renderer = renderer
        controller = make_controller(engine, make_coordinator())
        controller._get_playback_render_params(640, 360)  # 填充缓存
        assert controller._cached_render_params is not None

        controller.invalidate_cached_render_params()

        assert controller._cached_render_params is None
        assert controller._render_params_generation == 1
        renderer.discard_pending_grading_refresh.assert_called_once()

    def test_engine_reference_preserved_during_proxy_playback(self, make_controller) -> None:
        engine = make_engine()
        controller = make_controller(engine, make_coordinator())
        master = np.zeros((1080, 1920, 3), dtype=np.uint8)
        engine.img_full_original = master
        controller.set_display_scene_size((1920, 1080))

        controller._update_engine_video_frame_reference(
            np.zeros((540, 960, 3), dtype=np.uint8),
            preserve_display_reference=True,
        )
        assert engine.img_full_original is master

        controller._update_engine_video_frame_reference(
            np.zeros((540, 960, 3), dtype=np.uint8),
            preserve_display_reference=False,
        )
        assert engine.img_full_original is not master


class TestLetterboxStaticPreview:
    """deprecated letterbox 辅助的早退分支。"""

    def test_none_and_degenerate_frames_returned_as_is(self, make_controller) -> None:
        controller = make_controller(make_engine(), make_coordinator())
        assert controller._letterbox_static_preview_frame(None) is None
        flat = np.zeros(10, dtype=np.uint8)
        assert controller._letterbox_static_preview_frame(flat) is flat

    def test_missing_viewport_returns_frame(self, make_controller) -> None:
        engine = make_engine()
        engine.viewport_size = None
        controller = make_controller(engine, make_coordinator())
        frame = np.zeros((4, 4, 3), dtype=np.uint8)
        assert controller._letterbox_static_preview_frame(frame) is frame

    def test_same_size_frame_made_contiguous(self, make_controller) -> None:
        engine = make_engine()
        engine.viewport_size = (4, 4)
        controller = make_controller(engine, make_coordinator())
        frame = np.ascontiguousarray(np.zeros((4, 4, 3), dtype=np.uint8))
        result = controller._letterbox_static_preview_frame(frame)
        assert result.shape == frame.shape

    def test_letterbox_centers_frame(self, make_controller) -> None:
        pytest.importorskip("cv2")
        engine = make_engine()
        engine.viewport_size = (100, 100)
        controller = make_controller(engine, make_coordinator())
        frame = np.full((10, 20, 3), 200, dtype=np.uint8)  # h=10 w=20

        result = controller._letterbox_static_preview_frame(frame)

        assert result.shape == (100, 100, 3)
        # 背景填充 26（int dtype）
        assert int(result[0, 0, 0]) == 26
        # 内容区中心保持原像素值
        assert int(result[50, 50, 0]) == 200


# =============================================================================
# 静态预览 / scrub 呈现
# =============================================================================


class TestStaticPreviewState:
    """静态预览的 generation/pending/hold 状态管理。"""

    def test_begin_static_preview_generation_bumps_ids(self, make_controller) -> None:
        engine = make_engine()
        renderer = MagicMock()
        renderer._video_generation_id = 0
        engine.renderer = renderer
        controller = make_controller(engine, make_coordinator())

        gen1 = controller._begin_static_preview_generation()
        assert gen1 == 1
        assert renderer._video_generation_id == 1

        gen2 = controller._begin_static_preview_generation()
        assert gen2 == 2
        assert renderer._video_generation_id == 2

    def test_mark_renderer_video_texture_pending(self, make_controller) -> None:
        engine = make_engine()
        renderer = MagicMock()
        renderer.current_front_tex_id = 42
        renderer.current_front_size = (640, 360)
        engine.renderer = renderer
        controller = make_controller(engine, make_coordinator())

        controller._mark_renderer_video_texture_pending()

        assert renderer.video_texture_pending is True
        assert renderer.current_front_tex_id is None
        assert renderer.current_front_size == (0, 0)

    def test_clip_switch_guard_discards_pending_refreshes(self, make_controller) -> None:
        engine = make_engine()
        renderer = MagicMock()
        engine.renderer = renderer
        controller = make_controller(engine, make_coordinator())

        controller.begin_clip_switch_preview_guard()

        renderer.discard_pending_preview_refreshes.assert_called_once()
        assert renderer.video_texture_pending is True
        assert controller._generation_id == 1

    def test_withdraw_pending_restores_renderer(self, make_controller) -> None:
        engine = make_engine()
        renderer = MagicMock()
        engine.renderer = renderer
        controller = make_controller(engine, make_coordinator())
        controller.begin_clip_switch_preview_guard()

        controller.withdraw_static_preview_pending()

        assert renderer.video_texture_pending is False
        engine.render_pipeline.assert_called_once_with(render_reason="viewport_refresh")

    def test_static_preview_hold_flag(self, make_controller) -> None:
        engine = make_engine()
        controller = make_controller(engine, make_coordinator())

        controller._set_static_preview_frame_hold(True)
        assert engine._hold_viewport_frame is True
        controller._set_static_preview_frame_hold(False)
        assert engine._hold_viewport_frame is False


class TestRequestStaticPreviewFrame:
    """轮询式静态预览：request-key 去重（seek 请求合并）。"""

    def _make(self, make_controller: Callable[..., Any]):
        engine = make_engine()
        coordinator = make_coordinator()
        controller = make_controller(engine, coordinator)
        controller._submit_decoded_frame_playback = MagicMock()
        return controller, coordinator

    def test_polling_same_frame_does_not_reseek(self, make_controller) -> None:
        controller, coordinator = self._make(make_controller)

        assert controller.request_static_preview_frame(7) is False
        assert controller.request_static_preview_frame(7) is False

        # 去重：同一帧的重复轮询只发起一次异步 seek / master 请求
        assert coordinator.seek_to_frame.call_count == 1
        coordinator.seek_to_frame.assert_called_with(7, blocking=False)
        assert coordinator.request_master_frame.call_count == 1
        assert controller.current_frame == 7

    def test_new_frame_issued_new_seek(self, make_controller) -> None:
        controller, coordinator = self._make(make_controller)
        controller.request_static_preview_frame(7)
        controller.request_static_preview_frame(8)

        assert coordinator.seek_to_frame.call_count == 2

    def test_ready_master_frame_submitted_once(self, make_controller) -> None:
        controller, coordinator = self._make(make_controller)
        controller.request_static_preview_frame(7)

        ready = SimpleNamespace(frame_index=7)
        coordinator.take_ready_master_frame.return_value = ready

        assert controller.request_static_preview_frame(7) is True

        controller._submit_decoded_frame_playback.assert_called_once_with(
            ready, 7, quality="master", reason="seek", drop_if_stale=True
        )
        assert controller._last_static_preview_delivered is True
        assert controller._last_displayed_static_preview_is_thumbnail is False

    def test_without_dual_path_falls_back_to_seek(self, make_controller) -> None:
        engine = make_engine()
        controller = make_controller(engine, make_coordinator())
        controller._use_dual_path = False
        controller.seek_to_frame = MagicMock()

        assert controller.request_static_preview_frame(5) is True
        controller.seek_to_frame.assert_called_once_with(5)


class TestRenderStaticPreviewFrame:
    """同步静态预览路径（coordinator → source_manager → 失败）。"""

    def _make(self, make_controller: Callable[..., Any]):
        engine = make_engine()
        coordinator = make_coordinator()
        controller = make_controller(engine, coordinator)
        return controller, coordinator, engine

    def test_coordinator_ready_frame_renders(self, make_controller) -> None:
        controller, coordinator, engine = self._make(make_controller)
        array = np.full((8, 8, 3), 33, dtype=np.uint8)
        coordinator.get_current_frame_data.return_value = array

        assert controller.render_static_preview_frame(4) is True

        engine.set_image.assert_called_once()
        assert controller.current_frame == 4
        assert controller._last_displayed_frame_index == 4
        assert controller._last_static_preview_delivered is True

    def test_source_manager_proxy_fallback(self, make_controller) -> None:
        controller, coordinator, engine = self._make(make_controller)
        coordinator.get_current_frame_data.return_value = None
        packed = make_packed_frame(9)
        controller._source_manager = SimpleNamespace(
            get_proxy_frame=lambda f: packed,
            get_master_frame=lambda f: None,
        )

        assert controller.render_static_preview_frame(9) is True
        engine.set_image.assert_called_once()

    def test_no_frame_anywhere_returns_false(self, make_controller) -> None:
        controller, coordinator, _ = self._make(make_controller)
        coordinator.get_current_frame_data.return_value = None

        assert controller.render_static_preview_frame(9) is False
        assert controller.current_frame != 9

    def test_engine_render_failure_returns_false(self, make_controller) -> None:
        controller, coordinator, engine = self._make(make_controller)
        engine.set_image.side_effect = RuntimeError("no GL")
        coordinator.get_current_frame_data.return_value = np.zeros((4, 4, 3), np.uint8)

        assert controller.render_static_preview_frame(2) is False


class TestSubmitStaticPreviewArray:
    """缩略图静态预览提交。"""

    def test_invalid_inputs_return_false(self, make_controller) -> None:
        controller = make_controller(make_engine(), make_coordinator())

        assert controller.submit_static_preview_array(None, 1) is False
        assert controller.submit_static_preview_array([1, 2, 3], 1) is False  # type: ignore[arg-type]

    def test_missing_renderer_returns_false(self, make_controller) -> None:
        engine = make_engine()  # renderer None
        controller = make_controller(engine, make_coordinator())
        frame = np.zeros((4, 4, 3), dtype=np.uint8)

        assert controller.submit_static_preview_array(frame, 1) is False

    def test_valid_array_submits_as_thumbnail(self, make_controller) -> None:
        engine = make_engine()
        renderer = MagicMock()
        engine.renderer = renderer
        controller = make_controller(engine, make_coordinator())
        frame = np.full((6, 10, 3), 120, dtype=np.uint8)

        assert controller.submit_static_preview_array(frame, 12) is True

        cmd, payload = renderer.submit.call_args.args
        assert cmd == RenderCommand.RENDER_VIDEO_FRAME
        assert payload["reason"] == "seek"
        assert payload["quality"] == "proxy"
        assert controller._last_displayed_static_preview_is_thumbnail is True
        assert controller._last_static_preview_delivered is True
        assert controller.current_frame == 12

    def test_internal_error_returns_false(self, make_controller) -> None:
        engine = make_engine()
        engine.renderer = MagicMock()
        controller = make_controller(engine, make_coordinator())
        controller._get_static_preview_render_params = MagicMock(
            side_effect=RuntimeError("boom")
        )

        assert (
            controller.submit_static_preview_array(np.zeros((4, 4, 3), np.uint8), 3)
            is False
        )


class TestStaticPreviewShowsFrame:
    """static_preview_shows_frame 判定。"""

    def _make(self, make_controller: Callable[..., Any], renderer: Any):
        engine = make_engine()
        engine.renderer = renderer
        controller = make_controller(engine, make_coordinator())
        controller.current_frame = 24
        return controller

    def test_true_when_texture_present_and_paused(self, make_controller) -> None:
        renderer = MagicMock()
        renderer.current_video_texture = MagicMock()
        controller = self._make(make_controller, renderer)
        assert controller.static_preview_shows_frame(24) is True

    def test_false_while_playing(self, make_controller) -> None:
        controller = self._make(make_controller, MagicMock())
        controller.is_playing = True
        assert controller.static_preview_shows_frame(24) is False

    def test_false_without_video(self, make_controller) -> None:
        controller = self._make(make_controller, MagicMock())
        controller.current_path = ""
        assert controller.static_preview_shows_frame(24) is False

    def test_false_on_frame_mismatch(self, make_controller) -> None:
        controller = self._make(make_controller, MagicMock())
        assert controller.static_preview_shows_frame(25) is False

    def test_false_without_renderer(self, make_controller) -> None:
        controller = self._make(make_controller, None)
        assert controller.static_preview_shows_frame(24) is False


# =============================================================================
# seek 路径（coordinator / 旧架构 / 基类）
# =============================================================================


class TestEnhancedSeekLegacyPath:
    """无 coordinator 时的旧架构 seek。"""

    def _make(self, make_controller: Callable[..., Any]):
        engine = make_engine()
        controller = make_controller(engine)
        controller._use_dual_path = True
        controller._coordinator = None
        controller.current_path = "/tmp/legacy.mp4"
        clock = MagicMock()
        controller._playback_clock = clock
        prefetch = MagicMock()
        controller._proxy_prefetch_worker = prefetch
        # stop_playback 会停掉并置空 worker；屏蔽以观察 seek 自身的协调调用
        controller._stop_proxy_prefetch_worker = MagicMock()
        return controller, clock, prefetch

    def test_seek_submits_master_frame_and_updates_state(self, make_controller) -> None:
        controller, clock, prefetch = self._make(make_controller)
        packed = make_packed_frame(30)

        controller._source_manager = SimpleNamespace(
            total_frames=100,
            get_master_frame=lambda f: packed,
        )
        controller.seek_to_frame(30)

        assert controller.current_frame == 30
        assert controller._last_displayed_frame_index == 30
        clock.seek_to_frame.assert_called_once_with(30)
        # stop_playback 会停止并置空 worker，但 seek 仍通过该 worker 实例请求预读
        prefetch.request_seek.assert_called_once_with(31)

    def test_seek_without_frame_still_updates_frame_number(self, make_controller) -> None:
        controller, clock, prefetch = self._make(make_controller)
        controller._source_manager = SimpleNamespace(
            total_frames=100,
            get_master_frame=lambda f: None,
        )

        controller.seek_to_frame(33)

        assert controller.current_frame == 33
        clock.seek_to_frame.assert_called_once_with(33)


class TestBaseSeekAndStepping:
    """基类 seek / 逐帧步进 / 边界跳转。"""

    def _make(self, make_controller: Callable[..., Any]):
        controller = make_controller(make_engine(), cls=VideoPlaybackEngine)
        controller.current_path = "/tmp/base.mp4"
        controller._show_frame_hq = MagicMock()
        return controller

    def test_seek_without_video_is_noop(self, make_controller) -> None:
        controller = make_controller(make_engine(), cls=VideoPlaybackEngine)
        controller._show_frame_hq = MagicMock()

        controller.seek_to_frame(10)

        controller._show_frame_hq.assert_not_called()
        assert controller._generation_id == 0

    def test_seek_increments_generation_and_shows_hq(self, make_controller) -> None:
        controller = self._make(make_controller)

        controller.seek_to_frame(15)

        controller._show_frame_hq.assert_called_once_with(15)
        assert controller._generation_id == 1

    def test_seek_stops_playback_first(self, make_controller) -> None:
        controller = self._make(make_controller)
        controller.is_playing = True
        spy = _SignalSpy(controller.play_state_changed)

        controller.seek_to_frame(15)

        assert controller.is_playing is False
        assert spy.values == [False]

    def test_next_and_prev_frame(self, make_controller) -> None:
        controller = self._make(make_controller)
        controller.current_frame = 5

        controller.next_frame()
        controller._show_frame_hq.assert_called_with(6)

        controller.prev_frame()
        controller._show_frame_hq.assert_called_with(4)

    def test_stepping_requires_loaded_video(self, make_controller) -> None:
        controller = make_controller(make_engine(), cls=VideoPlaybackEngine)
        controller._show_frame_hq = MagicMock()

        controller.next_frame()
        controller.prev_frame()

        controller._show_frame_hq.assert_not_called()

    def test_first_frame_seeks_zero(self, make_controller) -> None:
        controller = self._make(make_controller)
        controller.first_frame()
        controller._show_frame_hq.assert_called_once_with(0)

    def test_last_frame_with_known_total(self, make_controller) -> None:
        controller = self._make(make_controller)
        controller._playback_clock = SimpleNamespace(total_frames=120, fps=25.0)

        controller.last_frame()

        controller._show_frame_hq.assert_called_once_with(119)

    def test_last_frame_with_unknown_total_holds_current(self, make_controller) -> None:
        controller = self._make(make_controller)
        controller.current_frame = 8
        controller._playback_clock = None  # BUG-C2-03：基类未初始化该属性，需显式置空

        controller.last_frame()

        controller._show_frame_hq.assert_called_once_with(8)


class TestBaseEngineStubAndInfo:
    """基类桩方法与信息查询。"""

    def test_load_video_stub_returns_false(self, make_controller) -> None:
        controller = make_controller(make_engine(), cls=VideoPlaybackEngine)
        assert controller.load_video("/tmp/any.mp4") is False

    def test_unload_resets_state(self, make_controller) -> None:
        controller = make_controller(make_engine(), cls=VideoPlaybackEngine)
        controller.current_path = "/tmp/x.mp4"
        controller.current_frame = 7
        controller._media_display_size = (1920, 1080)
        controller._float_buffer = np.zeros((4, 4, 3), np.float32)

        controller.unload_video()

        assert controller.current_path is None
        assert controller.current_frame == 0
        assert controller._media_display_size is None
        assert controller._float_buffer is None
        assert controller.is_video_loaded() is False

    def test_get_video_info(self, make_controller) -> None:
        controller = make_controller(make_engine(), cls=VideoPlaybackEngine)
        assert controller.get_video_info() is None

        controller.current_path = "/tmp/x.mp4"
        controller.current_frame = 3
        info = controller.get_video_info()
        assert info == {"path": "/tmp/x.mp4", "current_frame": 3}

    def test_get_float_buffer_reuses_and_reallocates(self, make_controller) -> None:
        controller = make_controller(make_engine(), cls=VideoPlaybackEngine)

        buf1 = controller._get_float_buffer(4, 6)
        assert controller._get_float_buffer(4, 6) is buf1

        buf2 = controller._get_float_buffer(8, 8)
        assert buf2 is not buf1
        assert buf2.shape == (8, 8, 3)

    def test_base_time_helpers_require_subclass_clock_bug_c2_03(
        self, make_controller
    ) -> None:
        """BUG-C2-03（已修复，AUD-39601）：基类 __init__ 现在初始化
        _playback_clock/_proxy_prefetch_worker 为 None，纯基类实例调用
        get_current_time / seek_to_time / last_frame 不再抛
        AttributeError（原 characterization 断言旧缺陷，修复后翻转）。"""
        controller = make_controller(make_engine(), cls=VideoPlaybackEngine)
        controller.current_path = "/tmp/x.mp4"

        # 修复后：基类实例可安全调用（无 clock → 0.0 / no-op）
        assert controller.get_current_time() == 0.0
        controller.seek_to_time(1.0)
        controller.last_frame()

    def test_base_proxy_ladder_requires_worker_attr_bug_c2_03(
        self, make_controller
    ) -> None:
        """BUG-C2-03 附注：基类 _check_proxy_ladder 引用子类的
        _proxy_prefetch_worker；在 _proxy_size 已设置且触发降级时基类实例会
        AttributeError。这里仅验证安全早退分支。"""
        controller = make_controller(make_engine(), cls=VideoPlaybackEngine)
        controller._proxy_size = None
        controller._check_proxy_ladder()  # 不应抛错（早退）


# =============================================================================
# 卸载与访问器
# =============================================================================


class TestUnloadEnhanced:
    """Enhanced 卸载路径的资源清理。"""

    def test_unload_cleans_coordinator_sources_and_renderer(self, make_controller) -> None:
        engine = make_engine()
        renderer = MagicMock()
        renderer.current_video_texture = object()
        renderer.current_video_planes = object()
        engine.renderer = renderer
        coordinator = make_coordinator()
        controller = make_controller(engine, coordinator)
        source_manager = MagicMock()
        controller._source_manager = source_manager
        controller._last_submitted_video_reason = "playback"
        controller._last_submitted_video_frame_index = 42

        controller.unload_video()

        coordinator.unload.assert_called_once()
        assert controller._coordinator is None
        source_manager.close.assert_called_once()
        assert renderer.current_video_texture is None
        assert renderer.current_video_planes is None
        assert controller._last_submitted_video_reason is None
        assert controller._last_submitted_video_frame_index == -1
        assert controller._last_displayed_frame_index is None


class TestEnhancedAccessors:
    """getter 委托链。"""

    def _controller(self, make_controller: Callable[..., Any]):
        return make_controller(make_engine(), make_coordinator())

    def test_get_source_manager_prefers_coordinator(self, make_controller) -> None:
        controller = self._controller(make_controller)
        legacy = object()
        controller._source_manager = legacy
        assert controller.get_source_manager() is controller._coordinator.source_manager

        controller._coordinator = None
        assert controller.get_source_manager() is legacy

    def test_get_playback_clock_prefers_coordinator_session(self, make_controller) -> None:
        controller = self._controller(make_controller)
        legacy_clock = object()
        controller._playback_clock = legacy_clock

        session_clock = object()
        controller._coordinator.session = SimpleNamespace(playback_clock=session_clock)
        assert controller.get_playback_clock() is session_clock

        controller._coordinator.session = None
        assert controller.get_playback_clock() is legacy_clock

    def test_get_coordinator(self, make_controller) -> None:
        controller = self._controller(make_controller)
        assert controller.get_coordinator() is controller._coordinator

    def test_get_video_info_from_coordinator(self, make_controller) -> None:
        controller = self._controller(make_controller)
        controller._coordinator.get_video_info.return_value = {"codec": "prores"}
        assert controller.get_video_info() == {"codec": "prores"}

    def test_get_video_info_fallback_to_base(self, make_controller) -> None:
        controller = self._controller(make_controller)
        controller._coordinator = None
        controller._use_dual_path = False
        controller.current_path = "/tmp/x.mp4"
        assert controller.get_video_info() == {
            "path": "/tmp/x.mp4",
            "current_frame": 0,
        }

    def test_dropped_frames_and_clock_state(self, make_controller) -> None:
        controller = self._controller(make_controller)
        assert controller.get_dropped_frames() == 0
        assert controller.get_clock_state() is None

        clock = MagicMock()
        clock.dropped_frames = 7
        clock.get_state.return_value = "PLAYING"
        controller._playback_clock = clock
        assert controller.get_dropped_frames() == 7
        assert controller.get_clock_state() == "PLAYING"

    def test_cache_stats(self, make_controller) -> None:
        controller = self._controller(make_controller)
        assert controller.get_cache_stats() is None

        source = MagicMock()
        source.get_cache_stats.return_value = {"hits": 3}
        controller._source_manager = source
        assert controller.get_cache_stats() == {"hits": 3}

    def test_is_video_loaded_across_paths(self, make_controller) -> None:
        controller = self._controller(make_controller)
        controller._coordinator.is_loaded = True
        assert controller.is_video_loaded() is True

        controller._coordinator.is_loaded = False
        assert controller.is_video_loaded() is True  # current_path 兜底

        controller.current_path = None
        controller._coordinator.is_loaded = False
        assert controller.is_video_loaded() is False

    def test_perf_stats_interfaces(self, make_controller) -> None:
        controller = self._controller(make_controller)
        stats = controller.get_perf_stats()
        assert isinstance(stats, dict)
        assert "tick_avg_ms" in stats
        assert controller.get_perf_monitor() is controller._perf_monitor
        controller.reset_perf_stats()
        assert controller.get_perf_stats()["tick_max_ms"] == 0.0


class TestCoordinatorCallbacks:
    """coordinator → 引擎的回调桥接。"""

    def test_frame_ready_callback_submits(self, make_controller) -> None:
        controller = make_controller(make_engine(), make_coordinator())
        controller._submit_decoded_frame_playback = MagicMock()
        frame = SimpleNamespace(frame_index=9)

        controller._on_coordinator_frame_ready(frame, 9)
        controller._submit_decoded_frame_playback.assert_called_once_with(frame, 9)

        controller._on_coordinator_frame_ready(None, 10)
        controller._submit_decoded_frame_playback.assert_called_once()  # 仍是 1 次

    def test_state_change_callback_maps_to_signal(self, make_controller) -> None:
        controller = make_controller(make_engine(), make_coordinator())
        spy = _SignalSpy(controller.play_state_changed)

        controller._on_coordinator_state_change("PLAYING")
        controller._on_coordinator_state_change("PAUSED")

        assert spy.values == [True, False]


# =============================================================================
# 管线配置 / 预览策略
# =============================================================================


class TestPipelinePolicyHelpers:
    """feature flag 与预览丢帧策略读取。"""

    def test_preview_policy_from_config(self, make_controller) -> None:
        controller = make_controller(
            make_engine(video_preview_drop_frames=True, video_preview_max_queue=5),
            make_coordinator(),
        )
        assert controller.should_drop_preview_frames() is True
        assert controller.get_preview_queue_depth() == 5

    def test_yuv_gpu_upload_disabled_by_config(self, make_controller) -> None:
        controller = make_controller(
            make_engine(video_use_yuv_gpu_upload=False), make_coordinator()
        )
        assert controller.should_use_yuv_gpu_upload() is False

    def test_yuv_gpu_upload_requires_self_test_on_darwin(self, make_controller) -> None:
        engine = make_engine()
        renderer = MagicMock()
        renderer._yuv_gpu_supported = False
        engine.renderer = renderer
        controller = make_controller(engine, make_coordinator())
        if sys.platform == "darwin":
            assert controller.should_use_yuv_gpu_upload() is False

        renderer._yuv_gpu_supported = True
        assert controller.should_use_yuv_gpu_upload() is True

    def test_pipeline_decision_accessors(self, make_controller) -> None:
        controller = make_controller(make_engine(), make_coordinator())
        decision = controller.get_pipeline_decision()
        assert decision is not None
        assert controller.is_v3_pipeline_enabled() is True
        assert "v3" in controller.get_pipeline_mode_string()

    def test_get_pipeline_report_aggregates_state(self, make_controller) -> None:
        engine = make_engine()
        coordinator = make_coordinator()
        source = MagicMock()
        source.backend_name = "ffmpeg"
        source.bit_depth = 10
        coordinator.source_manager = source
        coordinator.get_video_info.return_value = {
            "codec": "prores",
            "pixel_format": "yuv422p10le",
        }
        controller = make_controller(engine, coordinator)

        report = controller.get_pipeline_report()

        assert report["pipeline_mode"] == "v3"
        assert report["source_backend"] == "ffmpeg"
        assert report["codec"] == "prores"
        assert report["pixel_format"] == "yuv422p10le"
        assert report["bit_depth"] == 10
        assert report["zero_copy"] is False
        assert "fallback_reason" in report
        assert "renderer_queue_depth" in report

    def test_refresh_viewport_without_renderer(self, make_controller) -> None:
        controller = make_controller(make_engine(), make_coordinator())
        assert controller.refresh_current_frame_view() is False


class TestProxyLadder:
    """P2.2 代理阶梯调整。"""

    def _controller(self, make_controller: Callable[..., Any]):
        controller = make_controller(make_engine(), make_coordinator())
        controller._media_display_size = (1920, 1080)
        controller._proxy_size = (1920, 1080)
        controller._perf_monitor = MagicMock()
        return controller

    def test_stable_reason_keeps_proxy(self, make_controller) -> None:
        controller = self._controller(make_controller)
        controller._perf_monitor.check_proxy_ladder.return_value = (1920, "stable")

        controller._check_proxy_ladder()

        assert controller._proxy_size == (1920, 1080)

    def test_no_data_reason_keeps_proxy(self, make_controller) -> None:
        controller = self._controller(make_controller)
        controller._perf_monitor.check_proxy_ladder.return_value = (960, "no_data")

        controller._check_proxy_ladder()

        assert controller._proxy_size == (1920, 1080)

    def test_downgrade_shrinks_proxy_and_updates_worker(self, make_controller) -> None:
        controller = self._controller(make_controller)
        controller._perf_monitor.check_proxy_ladder.return_value = (
            960,
            "downgrade_slow_decode",
        )
        worker = MagicMock()
        controller._proxy_prefetch_worker = worker

        controller._check_proxy_ladder()

        assert controller._proxy_size == (960, 540)
        worker.update_target_size.assert_called_once_with((960, 540))

    def test_upgrade_restores_original_resolution(self, make_controller) -> None:
        controller = self._controller(make_controller)
        controller._proxy_size = (960, 540)
        controller._perf_monitor.check_proxy_ladder.return_value = (
            1920,
            "upgrade_fast_decode",
        )

        controller._check_proxy_ladder()

        assert controller._proxy_size == (1920, 1080)

    def test_no_media_display_size_keeps_proxy(self, make_controller) -> None:
        controller = self._controller(make_controller)
        controller._media_display_size = None
        controller._perf_monitor.check_proxy_ladder.return_value = (
            960,
            "downgrade_slow_decode",
        )

        controller._check_proxy_ladder()

        assert controller._proxy_size == (1920, 1080)


# =============================================================================
# 基类 play state / refresh（补充）
# =============================================================================


class TestBaseStopPlayback:
    """基类 stop_playback 的幂等性。"""

    def test_stop_when_not_playing_is_silent(self, make_controller) -> None:
        controller = make_controller(make_engine(), cls=VideoPlaybackEngine)
        spy = _SignalSpy(controller.play_state_changed)

        controller.stop_playback()

        assert spy.calls == []
        assert controller.is_playing is False

    def test_stop_when_playing_emits_false(self, make_controller) -> None:
        controller = make_controller(make_engine(), cls=VideoPlaybackEngine)
        controller.is_playing = True
        spy = _SignalSpy(controller.play_state_changed)

        controller.stop_playback()

        assert controller.is_playing is False
        assert spy.values == [False]
        assert controller._play_timer.isActive() is False


class TestSelectBackendForPath:
    """专业格式后端选择。"""

    def test_professional_extension_forces_ffmpeg(self, make_controller) -> None:
        controller = make_controller(make_engine(), make_coordinator())

        assert controller._select_backend_for_path("/tmp/interview.mov") == "ffmpeg"
        assert controller._select_backend_for_path("/tmp/clip.mxf") == "ffmpeg"

    def test_regular_file_uses_auto(self, make_controller) -> None:
        controller = make_controller(make_engine(), make_coordinator())
        with patch(
            "src.shared.media.factory.MediaSourceFactory"
        ) as factory:
            factory.probe_video_metadata.return_value = None
            assert controller._select_backend_for_path("/tmp/regular.mp4") == "auto"

    def test_professional_codec_forces_ffmpeg(self, make_controller) -> None:
        controller = make_controller(make_engine(), make_coordinator())
        with patch(
            "src.shared.media.factory.MediaSourceFactory"
        ) as factory:
            metadata = SimpleNamespace(codec_name="prores_ks")
            factory.probe_video_metadata.return_value = metadata
            assert controller._select_backend_for_path("/tmp/graded.mp4") == "ffmpeg"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
