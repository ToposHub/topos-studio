"""
Test PlaybackCoordinator Tick (S0.B)

测试 PlaybackCoordinator.tick() 的单次提交行为：
- tick() 不触发 _on_frame_ready 回调
- _on_play_tick() 只提交一次渲染

验收命令：
.venv/bin/python -m pytest tests/media/test_playback_coordinator_tick.py -q
"""

from __future__ import annotations

import sys
import os
from pathlib import Path

import pytest
import numpy as np
from unittest.mock import MagicMock, patch, PropertyMock

# Ensure project root is on path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))


# =============================================================================
# Mock Fixtures - V-P1-2: 使用新的配置键 video_pipeline_mode
# =============================================================================

@pytest.fixture(scope="session")
def qapp():
    """Create a headless QApplication for the entire test session."""
    from PyQt6.QtWidgets import QApplication
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    yield app


@pytest.fixture
def mock_engine(qapp):
    """
    Create a mock ImageEngine for testing.

    S5.B: Mock renderer.get_renderer_stats() 返回正确的整数值。
    V-P1-2: 使用 video_pipeline_mode='v3' 替代废弃的键。
    """
    engine = MagicMock()
    engine.renderer = MagicMock()
    # S5.B: Mock get_renderer_stats() 返回正确的字典
    engine.renderer.get_renderer_stats = MagicMock(return_value={
        'queue_depth': 0,
        'dropped_preview_frames': 0,
        'coalesced_video_commands': 0,
    })
    engine.viewport_size = (1920, 1080)
    engine.window = MagicMock()
    engine.window.config_manager = MagicMock()
    # V-P1-2: 使用 video_pipeline_mode='v3' 替代废弃的键
    engine.window.config_manager.config = {
        'video_pipeline_mode': 'v3',  # V-P1-2: 新键
        'video_pipeline_allow_v2_fallback': False,
        'video_playback_resolution': 1920,
        'video_preview_max_queue': 3,  # S5.B: 添加配置
    }
    # V-P1-2: Mock get() 方法以正确返回配置值
    engine.window.config_manager.get = lambda key, default=None: {
        'video_pipeline_mode': 'v3',
        'video_pipeline_allow_v2_fallback': False,
        'video_playback_resolution': 1920,
        'video_preview_max_queue': 3,
    }.get(key, default)
    yield engine


def create_mock_frame(frame_index: int, width: int = 1920, height: int = 1080) -> MagicMock:
    """创建 mock DecodedFrame 用于测试"""
    frame = MagicMock()
    frame.frame_index = frame_index
    frame.data = np.zeros((height, width, 3), dtype=np.uint8)
    frame.pixel_format = 'bgr24'
    frame.width = width
    frame.height = height
    frame.is_proxy = True
    return frame


# =============================================================================
# Test: tick() 不触发回调
# =============================================================================

class TestTickNoCallback:
    """测试 tick() 不触发 _on_frame_ready 回调"""

    def test_tick_returns_frame_without_callback(self, mock_engine) -> None:
        """tick() 返回帧但不触发 _on_frame_ready 回调"""
        from src.shared.video.playback_state import PlaybackCoordinator
        
        coordinator = PlaybackCoordinator()
        
        # Mock session
        mock_session = MagicMock()
        mock_session.tick.return_value = create_mock_frame(100)
        mock_session.current_frame = 100
        coordinator._session = mock_session
        
        # 设置回调
        callback_called = []
        def mock_callback(frame, frame_index):
            callback_called.append((frame, frame_index))
        
        coordinator.set_on_frame_ready(mock_callback)
        
        # 调用 tick
        frame = coordinator.tick()
        
        # S0.B: tick() 不应该触发回调
        assert frame is not None
        assert len(callback_called) == 0  # 回调未被调用

    def test_tick_with_none_frame_no_callback(self, mock_engine) -> None:
        """tick() 返回 None 时也不触发回调"""
        from src.shared.video.playback_state import PlaybackCoordinator
        
        coordinator = PlaybackCoordinator()
        
        # Mock session 返回 None
        mock_session = MagicMock()
        mock_session.tick.return_value = None
        mock_session.current_frame = 100
        coordinator._session = mock_session
        
        # 设置回调
        callback_called = []
        def mock_callback(frame, frame_index):
            callback_called.append((frame, frame_index))
        
        coordinator.set_on_frame_ready(mock_callback)
        
        # 调用 tick
        frame = coordinator.tick()
        
        # None 帧，回调也不应被调用
        assert frame is None
        assert len(callback_called) == 0


# =============================================================================
# Test: _on_play_tick 单次提交
# =============================================================================

class TestOnPlayTickSingleSubmit:
    """测试 _on_play_tick() 只提交一次渲染"""

    def test_on_play_tick_single_submit(self, mock_engine) -> None:
        """调用一次 _on_play_tick() 只提交一次渲染"""
        from src.shared.video.video_playback import EnhancedVideoPlaybackEngine
        
        # Mock PlaybackCoordinator
        with patch('src.shared.video.playback_state.PlaybackCoordinator') as MockCoordinator:
            mock_coordinator = MagicMock()
            mock_coordinator.is_loaded = True
            mock_coordinator.current_frame = 100
            mock_coordinator.fps = 30.0
            # 该 fixture 必须提供一个合法的 total_frames，否则 MagicMock 的
            # total_frames 会被 int() 解析成 1，使 _effective_out_frame() 塌缩成 0，
            # 第 100 帧被误判越界而提前停止（见 V-P0-09 产品修复）。
            mock_coordinator.total_frames = 1000
            mock_coordinator.tick.return_value = create_mock_frame(100)
            MockCoordinator.return_value = mock_coordinator

            controller = EnhancedVideoPlaybackEngine(mock_engine)

            # S0.A: 启用 v3（模拟）
            controller._use_dual_path = True
            controller._coordinator = mock_coordinator
            controller.is_playing = True

            # S2.A: Mock _submit_decoded_frame_playback 来计数（新逻辑）
            submit_count = []
            def mock_submit_decoded(frame, frame_num):
                submit_count.append((frame, frame_num))
            controller._submit_decoded_frame_playback = mock_submit_decoded

            # Mock _build_render_params 返回空字典避免mock错误
            controller._build_render_params = lambda: {}

            # 调用一次 _on_play_tick
            controller._on_play_tick()

            # S0.B: 应该只提交一次
            assert len(submit_count) == 1

    def test_on_play_tick_no_double_submit(self, mock_engine) -> None:
        """确保不会通过回调再次提交"""
        from src.shared.video.video_playback import EnhancedVideoPlaybackEngine
        
        # Mock PlaybackCoordinator
        with patch('src.shared.video.playback_state.PlaybackCoordinator') as MockCoordinator:
            mock_coordinator = MagicMock()
            mock_coordinator.is_loaded = True
            mock_coordinator.current_frame = 100
            mock_coordinator.fps = 30.0
            # 提供合法 total_frames，避免 MagicMock 被 int() 解析成 1 触发越界停止（V-P0-09）。
            mock_coordinator.total_frames = 1000
            mock_coordinator.tick.return_value = create_mock_frame(100)
            MockCoordinator.return_value = mock_coordinator

            controller = EnhancedVideoPlaybackEngine(mock_engine)

            # 启用 v3
            controller._use_dual_path = True
            controller._coordinator = mock_coordinator
            controller.is_playing = True
            
            # 设置 coordinator 的 on_frame_ready 回调
            # S0.B: 即使设置了回调，tick() 也不会触发它
            callback_submit_count = []
            def mock_callback(frame, frame_index):
                callback_submit_count.append((frame, frame_index))
            mock_coordinator._on_frame_ready = mock_callback
            
            # S2.A: Mock _submit_decoded_frame_playback (新逻辑)
            submit_count = []
            def mock_submit_decoded(frame, frame_num):
                submit_count.append((frame, frame_num))
            controller._submit_decoded_frame_playback = mock_submit_decoded

            # Mock _build_render_params 返回空字典避免mock错误
            controller._build_render_params = lambda: {}

            # 调用 _on_play_tick
            controller._on_play_tick()

            # S0.B: 通过回调的提交次数应该是 0
            assert len(callback_submit_count) == 0

            # 通过 _on_play_tick 直接提交次数应该是 1
            assert len(submit_count) == 1

    def test_on_play_tick_does_not_rerender_a_held_frame(self, mock_engine) -> None:
        """A queue hold must preserve the front frame without resubmitting it."""
        from src.shared.video.video_playback import EnhancedVideoPlaybackEngine

        held_frame = create_mock_frame(100)
        next_frame = create_mock_frame(101)
        coordinator = MagicMock()
        coordinator.current_frame = 100
        coordinator.fps = 25.0
        # 提供合法 total_frames，避免 MagicMock 被 int() 解析成 1 触发越界停止（V-P0-09）。
        coordinator.total_frames = 1000
        coordinator.tick.side_effect = [held_frame, next_frame]

        controller = EnhancedVideoPlaybackEngine(mock_engine)
        controller._use_dual_path = True
        controller._coordinator = coordinator
        controller.is_playing = True
        controller._last_displayed_decoded_frame = held_frame
        controller._last_displayed_frame_index = 100
        controller._last_submitted_render_params_generation = (
            controller._render_params_generation
        )

        submitted = []
        controller._submit_decoded_frame_playback = (
            lambda frame, frame_num: submitted.append((frame, frame_num))
        )

        controller._on_play_tick()
        controller._on_play_tick()

        assert submitted == [(next_frame, 101)]

    def test_on_play_tick_rerenders_a_held_frame_once_after_grade_change(self, mock_engine) -> None:
        """A grade edit refreshes the held frame once, never every timer tick."""
        from src.shared.video.video_playback import EnhancedVideoPlaybackEngine

        held_frame = create_mock_frame(100)
        coordinator = MagicMock()
        coordinator.current_frame = 100
        coordinator.fps = 25.0
        # 提供合法 total_frames，避免 MagicMock 被 int() 解析成 1 触发越界停止（V-P0-09）。
        coordinator.total_frames = 1000
        coordinator.tick.side_effect = [held_frame, held_frame]

        controller = EnhancedVideoPlaybackEngine(mock_engine)
        controller._use_dual_path = True
        controller._coordinator = coordinator
        controller.is_playing = True
        controller._last_displayed_decoded_frame = held_frame
        controller._last_displayed_frame_index = 100
        controller._last_submitted_render_params_generation = (
            controller._render_params_generation
        )
        controller.invalidate_cached_render_params()

        submitted = []
        controller._submit_decoded_frame_playback = (
            lambda frame, frame_num: submitted.append((frame, frame_num))
        )

        controller._on_play_tick()
        controller._on_play_tick()

        assert submitted == [(held_frame, 100)]

    def test_busy_renderer_keeps_a_bounded_latest_frame_probe(self, mock_engine) -> None:
        """A busy grading queue must not permanently stop Color playback."""
        import time
        from src.shared.video.video_playback import EnhancedVideoPlaybackEngine

        mock_engine.renderer.get_renderer_stats.return_value = {
            'queue_depth': 99,
            'dropped_preview_frames': 0,
            'coalesced_video_commands': 0,
        }
        coordinator = MagicMock()
        coordinator.current_frame = 100
        coordinator.fps = 30.0
        # 提供合法 total_frames，避免 MagicMock 被 int() 解析成 1 触发越界停止（V-P0-09）。
        coordinator.total_frames = 1000
        coordinator.tick.return_value = create_mock_frame(100)

        controller = EnhancedVideoPlaybackEngine(mock_engine)
        controller._use_dual_path = True
        controller._coordinator = coordinator
        controller.is_playing = True
        submitted = []
        controller._submit_decoded_frame_playback = (
            lambda frame, frame_num: submitted.append((frame, frame_num))
        )

        controller._on_play_tick()
        controller._renderer_backpressure_last_probe_at = time.monotonic()
        controller._on_play_tick()

        assert submitted == [(coordinator.tick.return_value, 100)]

    def test_busy_renderer_still_advances_clock(self, mock_engine) -> None:
        """V-P0-14: 渲染拥塞时仍必须推进 clock/decode，只抑制 render submit。

        此前拥塞时整个 tick 提前 return，连 coordinator.tick() 都不调用，
        导致时钟/playhead 停滞，恢复后追旧帧、变成 5Hz 卡顿。
        """
        import time
        from src.shared.video.video_playback import EnhancedVideoPlaybackEngine

        mock_engine.renderer.get_renderer_stats.return_value = {
            'queue_depth': 99,
            'dropped_preview_frames': 0,
            'coalesced_video_commands': 0,
        }
        coordinator = MagicMock()
        coordinator.current_frame = 100
        coordinator.fps = 30.0
        coordinator.total_frames = 1000
        coordinator.tick.return_value = create_mock_frame(100)

        controller = EnhancedVideoPlaybackEngine(mock_engine)
        controller._use_dual_path = True
        controller._coordinator = coordinator
        controller.is_playing = True
        submitted = []
        controller._submit_decoded_frame_playback = (
            lambda frame, frame_num: submitted.append((frame, frame_num))
        )

        # 第一次 tick 是 probe（提交一次），并把最近 probe 时间设为"刚刚"，
        # 使随后的 tick 落在 200ms 抑制窗口内。
        controller._on_play_tick()
        probe_tick_call_count = coordinator.tick.call_count
        controller._renderer_backpressure_last_probe_at = time.monotonic()

        # 拥塞窗口内的第二次 tick：不应提交，但仍必须调用 coordinator.tick()
        # 推进时钟与解码（V-P0-14 核心）。
        submitted.clear()
        controller._on_play_tick()

        assert submitted == [], "拥塞窗口内不应提交渲染"
        assert coordinator.tick.call_count == probe_tick_call_count + 1, (
            "拥塞时仍必须调用 coordinator.tick() 推进时钟（V-P0-14 回归）"
        )


# =============================================================================
# Test: grep verification (manual check)
# =============================================================================

class TestGrepVerification:
    """通过 grep 确认代码修改正确"""

    def test_tick_no_on_frame_ready_call(self) -> None:
        """验证 tick() 方法内没有 _on_frame_ready(frame, ...) 调用"""
        import subprocess
        
        # 查找实际的调用模式：self._on_frame_ready(frame
        result = subprocess.run(
            ['grep', '-n', 'self._on_frame_ready(frame',
             str(Path(__file__).resolve().parents[2] / "src/shared/video/playback_state.py")],
            capture_output=True, text=True
        )

        # grep 输出应该为空（没有找到调用）
        lines = result.stdout.strip()

        # S0.B: tick() 内不应该有 self._on_frame_ready(frame...) 调用
        # 如果 grep 找到了调用，说明修改不正确
        assert lines == '', f"不应该找到 self._on_frame_ready(frame...) 调用，但找到了: {lines}"

    def test_set_on_frame_ready_exists(self) -> None:
        """验证 set_on_frame_ready() 方法仍然存在（保留给未来使用）"""
        import subprocess

        result = subprocess.run(
            ['grep', '-n', 'def set_on_frame_ready',
             str(Path(__file__).resolve().parents[2] / "src/shared/video/playback_state.py")],
            capture_output=True, text=True
        )
        
        # setter 方法应该存在
        lines = result.stdout.strip()
        assert 'def set_on_frame_ready' in lines, "set_on_frame_ready() 方法应该保留"


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
