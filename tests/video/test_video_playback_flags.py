"""
Test Video Playback Feature Flags (S0.A)

测试 feature flag 对 v3 pipeline 的控制：
- 配置 v2/false 时不创建 PlaybackCoordinator
- 配置 v3/true 时启用 coordinator
- coordinator 初始化异常时 fallback，不影响 load_video()

验收命令：
.venv/bin/python -m pytest tests/test_video_playback_flags.py -q
"""

from __future__ import annotations

import sys
import os
import pytest
import numpy as np
from types import SimpleNamespace
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
    V-P1-2: 使用 video_pipeline_mode='v2' 替代废弃的键。
    """
    engine = MagicMock()
    engine.renderer = None
    engine.viewport_size = (1920, 1080)
    engine.window = MagicMock()

    # Mock config_manager with get() method for new config keys
    engine.window.config_manager = MagicMock()
    engine.window.config_manager.config = {
        'video_pipeline_mode': 'v2',  # V-P1-2: 新键
        'video_pipeline_allow_v2_fallback': True,
        'video_playback_resolution': 1920,
    }
    # V-P1-2: Mock get() 方法以正确返回配置值
    engine.window.config_manager.get = lambda key, default=None: {
        'video_pipeline_mode': 'v2',
        'video_pipeline_allow_v2_fallback': True,
        'video_playback_resolution': 1920,
    }.get(key, default)

    yield engine


@pytest.fixture
def mock_engine_v3_enabled(qapp):
    """
    Create a mock ImageEngine with v3 enabled config.
    V-P1-2: 使用 video_pipeline_mode='v3' 替代废弃的键。
    """
    engine = MagicMock()
    engine.renderer = None
    engine.viewport_size = (1920, 1080)
    engine.window = MagicMock()

    # Mock config_manager with v3 enabled (new config keys)
    engine.window.config_manager = MagicMock()
    engine.window.config_manager.config = {
        'video_pipeline_mode': 'v3',  # V-P1-2: 新键
        'video_pipeline_allow_v2_fallback': False,
        'video_playback_resolution': 1920,
    }
    # V-P1-2: Mock get() 方法以正确返回配置值
    engine.window.config_manager.get = lambda key, default=None: {
        'video_pipeline_mode': 'v3',
        'video_pipeline_allow_v2_fallback': False,
        'video_playback_resolution': 1920,
    }.get(key, default)

    yield engine


# =============================================================================
# Test: is_v3_pipeline_enabled
# =============================================================================

class TestIsV3PipelineEnabled:
    """测试 is_v3_pipeline_enabled() 方法"""

    def test_v2_config_resolves_to_v3(self, mock_engine) -> None:
        """v2 GStreamer 链路已移除：v2 配置映射为 v3，is_v3_pipeline_enabled() 返回 True"""
        from src.shared.video.video_playback import EnhancedVideoPlaybackEngine

        controller = EnhancedVideoPlaybackEngine(mock_engine)

        # v2 配置解析为 v3（v2 已移除）
        assert controller.is_v3_pipeline_enabled() is True

    def test_v3_config_returns_true(self, mock_engine_v3_enabled) -> None:
        """配置为 v3 + v3_enabled=True 时，is_v3_pipeline_enabled() 返回 True"""
        from src.shared.video.video_playback import EnhancedVideoPlaybackEngine
        
        controller = EnhancedVideoPlaybackEngine(mock_engine_v3_enabled)
        
        # v3 配置，应该返回 True
        assert controller.is_v3_pipeline_enabled() is True

    def test_v3_version_but_disabled_returns_false(self, qapp) -> None:
        """配置为 auto 模式 + allow_fallback=True 时默认使用 v3"""
        from src.shared.video.video_playback import EnhancedVideoPlaybackEngine
        
        engine = MagicMock()
        engine.renderer = None
        engine.viewport_size = (1920, 1080)
        engine.window = MagicMock()
        engine.window.config_manager = MagicMock()
        # V-P1-2: 使用 video_pipeline_mode='auto' + allow_fallback=True
        engine.window.config_manager.config = {
            'video_pipeline_mode': 'auto',  # V-P1-2: 新键
            'video_pipeline_allow_v2_fallback': True,
            'video_playback_resolution': 1920,
        }
        engine.window.config_manager.get = lambda key, default=None: {
            'video_pipeline_mode': 'auto',
            'video_pipeline_allow_v2_fallback': True,
            'video_playback_resolution': 1920,
        }.get(key, default)
        
        controller = EnhancedVideoPlaybackEngine(engine)
        
        # auto 模式默认使用 v3，没有 fallback 原因时返回 True
        assert controller.is_v3_pipeline_enabled() is True


# =============================================================================
# Test: enable_dual_path behavior
# =============================================================================

class TestEnableDualPathBehavior:
    """测试 enable_dual_path 根据配置的行为"""

    def test_v2_config_no_coordinator_created(self, mock_engine) -> None:
        """配置为 v2/false 时，enable_dual_path(False) 不创建 coordinator"""
        from src.shared.video.video_playback import EnhancedVideoPlaybackEngine
        
        controller = EnhancedVideoPlaybackEngine(mock_engine)
        controller.enable_dual_path(False)  # 明确禁用
        
        # 检查 coordinator 未创建
        assert controller._coordinator is None
        assert controller._use_dual_path is False

    def test_v3_config_coordinator_created(self, mock_engine_v3_enabled) -> None:
        """配置为 v3/true 时，enable_dual_path(True) 创建 coordinator"""
        from src.shared.video.video_playback import EnhancedVideoPlaybackEngine
        
        # Mock PlaybackCoordinator 初始化成功 - patch 在导入位置
        with patch('src.shared.video.playback_state.PlaybackCoordinator') as MockCoordinator:
            mock_coordinator = MagicMock()
            MockCoordinator.return_value = mock_coordinator
            
            controller = EnhancedVideoPlaybackEngine(mock_engine_v3_enabled)
            
            # 模拟 MainWindow 的逻辑
            if controller.is_v3_pipeline_enabled():
                controller.enable_dual_path(True)
            
            # 检查 coordinator 创建成功
            assert controller._coordinator is mock_coordinator
            assert controller._use_dual_path is True

    def test_coordinator_init_failure_fallback(self, mock_engine_v3_enabled) -> None:
        """coordinator 初始化异常时，自动 fallback 到 v2"""
        from src.shared.video.video_playback import EnhancedVideoPlaybackEngine
        
        # Mock PlaybackCoordinator 初始化失败 - patch 在导入位置
        with patch('src.shared.video.playback_state.PlaybackCoordinator') as MockCoordinator:
            MockCoordinator.side_effect = ImportError("PlaybackCoordinator not available")
            
            controller = EnhancedVideoPlaybackEngine(mock_engine_v3_enabled)
            
            # 即使配置为 v3，但初始化失败会 fallback
            controller.enable_dual_path(True)
            
            # 检查 fallback 成功：coordinator 为 None，dual_path 为 False
            assert controller._coordinator is None
            assert controller._use_dual_path is False

    def test_coordinator_first_frame_failure_stays_v3(self, mock_engine_v3_enabled) -> None:
        """V-P0-5: v3 加载成功但首帧不可显示时，保持 v3 状态显示 placeholder，不 fallback 到 v2。

        首帧失败（FIRST_FRAME_TIMEOUT, FIRST_FRAME_UNAVAILABLE）是非致命错误，
        只显示 placeholder，不触发 v2 fallback。
        """
        from src.shared.video.video_playback import EnhancedVideoPlaybackEngine

        with patch('src.shared.video.playback_state.PlaybackCoordinator') as MockCoordinator, \
             patch('src.shared.video.video_playback.VideoPlaybackEngine.load_video', return_value=True) as mock_v2_load:
            mock_coordinator = MagicMock()
            mock_coordinator.load.return_value = True
            mock_coordinator.is_loaded = True
            mock_coordinator.fps = 25.0
            mock_coordinator.total_frames = 137
            mock_coordinator.source_manager = None
            mock_coordinator.session = None
            mock_coordinator.get_current_frame_data.return_value = None
            MockCoordinator.return_value = mock_coordinator

            controller = EnhancedVideoPlaybackEngine(mock_engine_v3_enabled)
            controller.enable_dual_path(True)

            assert controller.load_video('/tmp/test-video.mp4') is True
            # V-P0-5: 首帧失败是非致命错误，保持 v3 状态
            assert controller._use_dual_path is True
            assert controller._coordinator is not None
            # v2 load 不应被调用（首帧失败不触发 fallback）
            mock_v2_load.assert_not_called()

    def test_v3_loaded_state_does_not_require_legacy_decoder(self, mock_engine_v3_enabled) -> None:
        """v3 coordinator/source-manager playback can be loaded while legacy decoder is None."""
        from src.shared.video.video_playback import EnhancedVideoPlaybackEngine

        controller = EnhancedVideoPlaybackEngine(mock_engine_v3_enabled)
        controller._use_dual_path = True
        controller._coordinator = SimpleNamespace(is_loaded=True)
        controller.decoder = None
        controller.current_path = "/tmp/test-video.mp4"

        assert controller.is_video_loaded() is True

        controller._coordinator = SimpleNamespace(is_loaded=False)
        assert controller.is_video_loaded() is True

        controller.current_path = None
        assert controller.is_video_loaded() is False


# =============================================================================
# Test: load_video fallback
# =============================================================================

class TestLoadVideoFallback:
    """测试 load_video() 的 fallback 行为"""

    def test_load_video_v2_path_no_video(self, mock_engine) -> None:
        """配置为 v2 时，_use_dual_path 为 False，coordinator 未创建"""
        from src.shared.video.video_playback import EnhancedVideoPlaybackEngine
        
        controller = EnhancedVideoPlaybackEngine(mock_engine)
        
        # 禁用 dual path
        controller.enable_dual_path(False)
        
        # 验证状态
        assert controller._use_dual_path is False
        assert controller._coordinator is None

    def test_load_video_coordinator_failure_fallback(self, mock_engine_v3_enabled) -> None:
        """coordinator 初始化失败时，enable_dual_path 自动 fallback"""
        from src.shared.video.video_playback import EnhancedVideoPlaybackEngine
        
        # Mock PlaybackCoordinator 初始化失败 - patch 在导入位置
        with patch('src.shared.video.playback_state.PlaybackCoordinator') as MockCoordinator:
            MockCoordinator.side_effect = RuntimeError("coordinator failed")
            
            controller = EnhancedVideoPlaybackEngine(mock_engine_v3_enabled)
            
            # enable_dual_path 会触发 fallback
            controller.enable_dual_path(True)
            
            # 现在 _use_dual_path 应该是 False（已 fallback）
            assert controller._use_dual_path is False
            assert controller._coordinator is None


# =============================================================================
# Test: MainWindow integration
# =============================================================================

class TestMainWindowIntegration:
    """测试 MainWindow.load_video 的配置驱动行为"""

    def test_main_window_creates_controller_based_on_config(self, mock_engine, qapp) -> None:
        """MainWindow 根据 is_v3_pipeline_enabled() 决定启用 dual path"""
        from src.shared.video.video_playback import EnhancedVideoPlaybackEngine
        
        # 模拟 MainWindow 的逻辑
        controller = EnhancedVideoPlaybackEngine(mock_engine)
        
        # S0.A 推荐逻辑
        if controller.is_v3_pipeline_enabled():
            controller.enable_dual_path(True)
        else:
            controller.enable_dual_path(False)

        # v2 配置解析为 v3：dual path 应该启用（v2 已移除）
        assert controller._use_dual_path is True

    def test_main_window_v3_creates_coordinator(self, mock_engine_v3_enabled, qapp) -> None:
        """MainWindow v3 配置时创建 coordinator"""
        from src.shared.video.video_playback import EnhancedVideoPlaybackEngine
        
        # Mock PlaybackCoordinator 初始化成功 - patch 在导入位置
        with patch('src.shared.video.playback_state.PlaybackCoordinator') as MockCoordinator:
            mock_coordinator = MagicMock()
            MockCoordinator.return_value = mock_coordinator
            
            # 模拟 MainWindow 的逻辑
            controller = EnhancedVideoPlaybackEngine(mock_engine_v3_enabled)
            
            if controller.is_v3_pipeline_enabled():
                controller.enable_dual_path(True)
            else:
                controller.enable_dual_path(False)
            
            # v3 配置：dual path 应该启用，coordinator 创建
            assert controller._use_dual_path is True
            assert controller._coordinator is mock_coordinator


class TestPlaybackRenderParams:
    """Playback render params should keep color identity and live viewport state."""

    def test_raw_disabled_uses_identity_params(self, mock_engine_v3_enabled) -> None:
        from src.shared.video.video_playback import EnhancedVideoPlaybackEngine

        mock_engine_v3_enabled.layers = []
        mock_engine_v3_enabled.visible_rect = (0.0, 0.0, 1920.0, 1080.0)
        mock_engine_v3_enabled.current_raw_params = {
            "enabled": False,
            "saturation": 100.0,
            "shadows": 40.0,
            "highlights": -30.0,
            "contrast": 2.0,
        }
        mock_engine_v3_enabled.film_grain_params = {}
        mock_engine_v3_enabled.show_mask_mode = False
        mock_engine_v3_enabled.active_layer_index = 0

        controller = EnhancedVideoPlaybackEngine(mock_engine_v3_enabled)
        params = controller._get_playback_render_params(1920, 1080)

        raw = params["raw_preview"]
        assert raw["saturation"] == 1.0
        assert raw["shadows"] == 0.0
        assert raw["highlights"] == 0.0
        assert raw["contrast"] == 1.0
        assert raw["color_space"] == -1

    def test_viewport_params_refresh_without_rebuilding_layer_cache(self, mock_engine_v3_enabled) -> None:
        from src.shared.video.video_playback import EnhancedVideoPlaybackEngine

        mock_engine_v3_enabled.layers = []
        mock_engine_v3_enabled.current_raw_params = {"enabled": False}
        mock_engine_v3_enabled.film_grain_params = {}
        mock_engine_v3_enabled.show_mask_mode = False
        mock_engine_v3_enabled.active_layer_index = 0
        mock_engine_v3_enabled.viewport_size = (1000, 500)
        mock_engine_v3_enabled.visible_rect = (0.0, 0.0, 1920.0, 960.0)

        controller = EnhancedVideoPlaybackEngine(mock_engine_v3_enabled)
        first = controller._get_playback_render_params(1920, 1080)

        mock_engine_v3_enabled.viewport_size = (800, 400)
        mock_engine_v3_enabled.visible_rect = (100.0, 50.0, 960.0, 540.0)
        mock_engine_v3_enabled.show_mask_mode = True
        second = controller._get_playback_render_params(1920, 1080)

        assert first["viewport_size"] == (1000, 500)
        assert first["visible_rect"] == (0.0, 0.0, 1920.0, 960.0)
        assert second["viewport_size"] == (800, 400)
        assert second["visible_rect"] == (100.0, 50.0, 960.0, 540.0)
        assert second["show_mask"] is True

    def test_playback_submit_keeps_decoded_frame_resolution(self, mock_engine_v3_enabled) -> None:
        """Playback must not render a native 1080p frame into the widget-size FBO."""
        from src.shared.video.video_playback import EnhancedVideoPlaybackEngine

        mock_engine_v3_enabled.layers = []
        mock_engine_v3_enabled.current_raw_params = {"enabled": False}
        mock_engine_v3_enabled.film_grain_params = {}
        mock_engine_v3_enabled.show_mask_mode = False
        mock_engine_v3_enabled.active_layer_index = 0
        mock_engine_v3_enabled.viewport_size = (960, 540)
        mock_engine_v3_enabled.visible_rect = (0.0, 0.0, 1920.0, 1080.0)

        controller = EnhancedVideoPlaybackEngine(mock_engine_v3_enabled)
        params = controller._get_playback_render_params(
            1920,
            1080,
            render_at_source_resolution=True,
        )

        assert params["viewport_size"] == (1920, 1080)

    def test_native_render_target_uses_display_aspect(self, mock_engine_v3_enabled) -> None:
        """Native-quality playback targets must follow the timeline display aspect."""
        from src.shared.video.video_playback import EnhancedVideoPlaybackEngine

        mock_engine_v3_enabled.layers = []
        mock_engine_v3_enabled.current_raw_params = {"enabled": False}
        mock_engine_v3_enabled.film_grain_params = {}
        mock_engine_v3_enabled.show_mask_mode = False
        mock_engine_v3_enabled.active_layer_index = 0
        mock_engine_v3_enabled.viewport_size = (960, 540)
        mock_engine_v3_enabled.visible_rect = (0.0, 0.0, 1920.0, 1080.0)

        controller = EnhancedVideoPlaybackEngine(mock_engine_v3_enabled)
        controller.set_display_scene_size((1920, 1080))
        params = controller._get_playback_render_params(
            1920,
            1200,
            source_width=1920,
            source_height=1200,
            render_at_source_resolution=True,
        )

        # Preserve the native width while preventing the intermediate FBO
        # from inheriting a non-display pixel aspect.
        assert params["viewport_size"] == (1920, 1080)

    def test_refresh_current_frame_reuses_last_decoded_frame(self, mock_engine_v3_enabled) -> None:
        from src.shared.video.video_playback import EnhancedVideoPlaybackEngine

        # V-P1-2: 设置 renderer 以测试 fallback 路径
        renderer = MagicMock()
        renderer.current_video_texture = None  # 没有 GPU 纹理
        renderer.current_video_planes = None
        mock_engine_v3_enabled.renderer = renderer

        controller = EnhancedVideoPlaybackEngine(mock_engine_v3_enabled)
        frame = object()
        controller._last_displayed_decoded_frame = frame
        controller._last_displayed_frame_index = 24
        controller._submit_decoded_frame_playback = MagicMock()

        assert controller.refresh_current_frame_view() is True
        controller._submit_decoded_frame_playback.assert_called_once_with(frame, 24)

    def test_refresh_current_frame_resubmits_existing_video_texture(self, mock_engine_v3_enabled) -> None:
        from src.features.color.core.async_renderer import RenderCommand
        from src.shared.video.video_playback import EnhancedVideoPlaybackEngine

        renderer = MagicMock()
        # V-P1-2: 设置 current_video_texture 为有 size 的 mock 对象
        mock_texture = MagicMock()
        mock_texture.size = (640, 360)
        renderer.current_video_texture = mock_texture
        renderer.current_video_planes = None
        mock_engine_v3_enabled.renderer = renderer

        controller = EnhancedVideoPlaybackEngine(mock_engine_v3_enabled)
        controller._get_playback_render_params = MagicMock(return_value={"viewport_size": (640, 360)})

        assert controller.refresh_current_frame_view() is True
        renderer.submit.assert_called_once()
        # 验证提交的是正确的命令和参数
        call_args = renderer.submit.call_args.args
        assert call_args[0] == RenderCommand.RENDER_FRAME
        assert call_args[1]["viewport_size"] == (640, 360)

    def test_refresh_current_frame_does_not_compete_with_playback(self, mock_engine_v3_enabled) -> None:
        """Playback owns presentation; a viewport refresh must not publish a held frame."""
        from src.shared.video.video_playback import EnhancedVideoPlaybackEngine

        renderer = MagicMock()
        mock_engine_v3_enabled.renderer = renderer
        controller = EnhancedVideoPlaybackEngine(mock_engine_v3_enabled)
        controller.is_playing = True

        assert controller.refresh_current_frame_view() is True
        renderer.submit.assert_not_called()


class TestSeekToFrameBlocking:
    """V-P0-01: seek_to_frame 的 blocking 参数控制是否在 UI 线程同步取 master 帧。

    scrub 等高频热路径必须传 blocking=False，避免长 GOP 4K ~300ms 卡顿。
    """

    def _make_v3_controller(self, mock_engine_v3_enabled):
        from src.shared.video.video_playback import EnhancedVideoPlaybackEngine

        controller = EnhancedVideoPlaybackEngine(mock_engine_v3_enabled)
        controller._use_dual_path = True
        mock_coordinator = MagicMock()
        mock_coordinator.is_loaded = True
        mock_coordinator.total_frames = 1000
        mock_coordinator.current_frame = 50
        mock_coordinator.fps = 30.0
        mock_coordinator.get_current_frame_data.return_value = None
        controller._coordinator = mock_coordinator
        # 停止 play 时的时钟/标记依赖
        controller._playback_clock = MagicMock()
        controller.is_playing = False
        return controller, mock_coordinator

    def test_blocking_false_skips_sync_master_fetch(self, mock_engine_v3_enabled) -> None:
        """blocking=False 时绝不同步取 master 帧（不阻塞 UI），但仍尝试非阻塞取帧显示。"""
        controller, mock_coordinator = self._make_v3_controller(mock_engine_v3_enabled)

        controller.seek_to_frame(123, blocking=False)

        # 异步 seek，blocking=False
        mock_coordinator.seek_to_frame.assert_called_once_with(123, blocking=False, churn_generation=True)
        # 关键：绝不在 UI 线程同步取 master 帧（这正是 ~300ms 卡顿的来源）。
        # 只允许非阻塞的 proxy/已就绪帧获取（is_master=False）。
        for call in mock_coordinator.get_current_frame_data.call_args_list:
            assert not (call.kwargs.get('is_master', False)), (
                "非阻塞 seek 不应同步取 master 帧"
            )
        # 异步请求 master（松手后/refine 用）
        mock_coordinator.request_master_frame.assert_called_once_with(123)
        # 仍更新 current_frame，保证 UI 进度同步
        assert controller.current_frame == 123

    def test_blocking_false_displays_ready_frame_during_scrub(self, mock_engine_v3_enabled) -> None:
        """blocking=False 且有已就绪帧时，应立即提交显示（scrub 实时预览）。"""
        controller, mock_coordinator = self._make_v3_controller(mock_engine_v3_enabled)
        ready_frame = MagicMock()
        ready_frame.frame_index = 123
        # get_current_frame_data(is_master=False) 返回已就绪的 proxy 帧
        mock_coordinator.get_current_frame_data.side_effect = (
            lambda is_master=False: ready_frame if not is_master else None
        )
        submitted = []
        controller._submit_decoded_frame_playback = (
            lambda frame, num, **kw: submitted.append((frame, num, kw))
        )

        controller.seek_to_frame(123, blocking=False)

        # 应提交显示（质量 proxy），让拖动过程有视觉反馈
        assert submitted, "scrub 过程中已就绪的帧应被提交显示"
        frame, num, kw = submitted[0]
        assert num == 123
        assert kw.get('quality') == 'proxy'

    def test_blocking_none_keeps_legacy_behavior(self, mock_engine_v3_enabled) -> None:
        """blocking=None 时维持原"按播放态推断"逻辑（回归保护）。

        当前未播放（_is_any_engine_playing()=False）→ 走 blocking 同步取帧。
        """
        controller, mock_coordinator = self._make_v3_controller(mock_engine_v3_enabled)

        with patch('src.shared.video.video_playback._is_any_engine_playing',
                   return_value=False):
            controller.seek_to_frame(77)

        # 未播放 → 旧行为是 blocking seek + 同步取帧
        mock_coordinator.seek_to_frame.assert_called_once_with(77, blocking=True, churn_generation=True)
        mock_coordinator.get_current_frame_data.assert_called_once_with(is_master=True)

    def test_blocking_false_does_not_emit_blocking_static_preview(self, mock_engine_v3_enabled) -> None:
        """blocking=False 不应触发静态预览 hold 标记（那是阻塞路径专用）。"""
        controller, _ = self._make_v3_controller(mock_engine_v3_enabled)
        controller._set_static_preview_frame_hold = MagicMock()

        controller.seek_to_frame(5, blocking=False)

        controller._set_static_preview_frame_hold.assert_not_called()

    def test_ready_frame_after_nonblocking_seek_can_be_presented(self, mock_engine_v3_enabled) -> None:
        """异步解码在 seek 返回后完成时，scrub 仍能提交目标预览帧。"""
        controller, mock_coordinator = self._make_v3_controller(mock_engine_v3_enabled)
        ready_frame = MagicMock()
        ready_frame.frame_index = 123
        mock_coordinator.take_ready_master_frame.return_value = ready_frame
        submitted = []
        controller._submit_decoded_frame_playback = (
            lambda frame, num, **kw: submitted.append((frame, num, kw))
        )
        controller._last_displayed_frame_index = -1

        assert controller.present_ready_scrub_frame(123) is True
        mock_coordinator.take_ready_master_frame.assert_called_once_with(123)
        assert submitted == [
            (ready_frame, 123, {
                "quality": "proxy",
                "reason": "playback",
                "drop_if_stale": True,
            })
        ]


class TestPlaybackFrameSubmit:
    """Regression tests for the lightweight v2 playback submit path."""

    def test_submit_frame_playback_keeps_ui_tick_lightweight(self, mock_engine) -> None:
        """UI tick should submit raw frame data, not pre-convert BGR to RGBA."""
        import numpy as np
        from src.features.color.core.async_renderer import RenderCommand
        from src.shared.video.video_playback import VideoPlaybackEngine

        renderer = MagicMock()
        mock_engine.renderer = renderer
        mock_engine.layers = []
        mock_engine.current_raw_params = {"enabled": False}
        mock_engine.film_grain_params = {}
        mock_engine.show_mask_mode = False
        mock_engine.active_layer_index = 0
        mock_engine.viewport_size = (320, 180)
        mock_engine.visible_rect = (0.0, 0.0, 320.0, 180.0)

        controller = VideoPlaybackEngine(mock_engine)
        frame = np.zeros((180, 320, 3), dtype=np.uint8)

        controller._submit_frame_playback(frame, 3)

        renderer.submit.assert_called_once()
        cmd, payload = renderer.submit.call_args.args
        assert cmd == RenderCommand.RENDER_VIDEO_FRAME
        assert payload["frame"] is frame
        assert "frame_data" not in payload
        assert payload["frame_index"] == 3
        assert payload["quality"] == "proxy"
        assert payload["reason"] == "playback"

    def test_playback_render_params_scale_scene_rect_to_proxy_frame(self, mock_engine) -> None:
        """Playback render params must map scene coords into proxy frame coords."""
        from src.shared.video.video_playback import VideoPlaybackEngine

        mock_engine.layers = []
        mock_engine.current_raw_params = {"enabled": False}
        mock_engine.film_grain_params = {}
        mock_engine.show_mask_mode = False
        mock_engine.active_layer_index = 0
        mock_engine.viewport_size = (960, 540)
        mock_engine.visible_rect = (0.0, 0.0, 3840.0, 2160.0)
        mock_engine.scene_size = (3840.0, 2160.0)

        controller = VideoPlaybackEngine(mock_engine)
        params = controller._get_playback_render_params(1920, 1080)

        assert params["visible_rect"] == (0.0, 0.0, 1920.0, 1080.0)
        assert params["scene_visible_rect"] == (0.0, 0.0, 3840.0, 2160.0)

    def test_playback_render_params_scale_using_frame_metadata_when_scene_size_is_missing(self, mock_engine) -> None:
        """Proxy playback should not sample past texture edges when scene_size is stale."""
        from src.shared.video.video_playback import VideoPlaybackEngine

        mock_engine.layers = []
        mock_engine.current_raw_params = {"enabled": False}
        mock_engine.film_grain_params = {}
        mock_engine.show_mask_mode = False
        mock_engine.active_layer_index = 0
        mock_engine.viewport_size = (960, 540)
        mock_engine.visible_rect = (960.0, 540.0, 1920.0, 1080.0)
        mock_engine.scene_size = None

        controller = VideoPlaybackEngine(mock_engine)
        params = controller._get_playback_render_params(
            960,
            540,
            source_width=3840,
            source_height=2160,
        )

        assert params["visible_rect"] == (240.0, 135.0, 480.0, 270.0)
        assert params["scene_visible_rect"] == (960.0, 540.0, 1920.0, 1080.0)

    def test_playback_render_params_prefer_canvas_scene_over_proxy_metadata(self, mock_engine) -> None:
        """Shared proxy metadata can be proxy-sized; canvas scene remains authoritative."""
        from src.shared.video.video_playback import VideoPlaybackEngine

        mock_engine.layers = []
        mock_engine.current_raw_params = {"enabled": False}
        mock_engine.film_grain_params = {}
        mock_engine.show_mask_mode = False
        mock_engine.active_layer_index = 0
        mock_engine.viewport_size = (960, 540)
        mock_engine.visible_rect = (960.0, 540.0, 1920.0, 1080.0)
        mock_engine.scene_size = (3840.0, 2160.0)

        controller = VideoPlaybackEngine(mock_engine)
        params = controller._get_playback_render_params(
            960,
            540,
            source_width=960,
            source_height=540,
        )

        assert params["visible_rect"] == (240.0, 135.0, 480.0, 270.0)
        assert params["scene_visible_rect"] == (960.0, 540.0, 1920.0, 1080.0)

    def test_playback_render_params_use_timeline_display_scene_size(self, mock_engine) -> None:
        """A 4K source in a 2K timeline should display through the 2K timeline scene."""
        from src.shared.video.video_playback import VideoPlaybackEngine

        mock_engine.layers = []
        mock_engine.current_raw_params = {"enabled": False}
        mock_engine.film_grain_params = {}
        mock_engine.show_mask_mode = False
        mock_engine.active_layer_index = 0
        mock_engine.viewport_size = (960, 540)
        mock_engine.visible_rect = (0.0, 0.0, 1920.0, 1080.0)
        mock_engine.scene_size = (3840.0, 2160.0)

        controller = VideoPlaybackEngine(mock_engine)
        controller.set_display_scene_size((1920, 1080))
        params = controller._get_playback_render_params(
            960,
            540,
            source_width=3840,
            source_height=2160,
        )

        assert mock_engine.scene_size == (1920.0, 1080.0)
        assert params["visible_rect"] == (0.0, 0.0, 960.0, 540.0)
        assert params["scene_visible_rect"] == (0.0, 0.0, 1920.0, 1080.0)

    def test_clearing_display_override_restores_decoded_frame_aspect(self, mock_engine) -> None:
        """Color must not retain an ultra-wide timeline canvas during playback."""
        from src.shared.video.video_playback import VideoPlaybackEngine

        mock_engine.layers = []
        mock_engine.current_raw_params = {"enabled": False}
        mock_engine.film_grain_params = {}
        mock_engine.show_mask_mode = False
        mock_engine.active_layer_index = 0
        mock_engine.viewport_size = (960, 540)
        mock_engine.visible_rect = (0.0, 0.0, 2560.0, 1000.0)
        mock_engine.scene_size = (2560.0, 1000.0)

        controller = VideoPlaybackEngine(mock_engine)
        controller.set_display_scene_size((2560, 1000))
        controller.clear_display_scene_size()
        params = controller._get_playback_render_params(
            1920,
            1080,
            source_width=1920,
            source_height=1080,
            render_at_source_resolution=True,
        )

        assert mock_engine.scene_size is None
        assert params["viewport_size"] == (1920, 1080)

    def test_media_display_metadata_overrides_non_square_raster_aspect(self, mock_engine) -> None:
        """SAR-corrected display size, not decoded pixels, defines Color's canvas."""
        from src.shared.media.metadata import Rational, VideoMetadata
        from src.shared.video.video_playback import VideoPlaybackEngine

        metadata = VideoMetadata(
            width=1920,
            height=1000,
            fps=Rational(24, 1),
            total_frames=1,
            codec_name="test",
            codec_profile=None,
            pixel_format="yuv420p",
            bit_depth=8,
            # 1920 x (1000 / (25/27)) = 1920 x 1080 = 16:9.
            sample_aspect_ratio=Rational(25, 27),
        )
        controller = VideoPlaybackEngine(mock_engine)

        controller._set_media_display_size_from_metadata(metadata)

        assert controller.get_media_display_size() == (1920, 1080)
        assert controller._display_scene_size == (1920, 1080)
        assert mock_engine.scene_size == (1920.0, 1080.0)

    def test_proxy_submit_preserves_master_reference_with_display_scene(self, mock_engine) -> None:
        """Proxy playback must not replace the master-sized frame used for still geometry."""
        from src.shared.video.video_playback import VideoPlaybackEngine
        from src.shared.video.render_commands import RenderCommand

        renderer = MagicMock()
        mock_engine.renderer = renderer
        mock_engine.layers = []
        mock_engine.current_raw_params = {"enabled": False}
        mock_engine.film_grain_params = {}
        mock_engine.show_mask_mode = False
        mock_engine.active_layer_index = 0
        mock_engine.viewport_size = (960, 540)
        mock_engine.visible_rect = (0.0, 0.0, 1920.0, 1080.0)
        master = object()
        mock_engine.img_full_original = master
        mock_engine.img_source_full = master

        controller = VideoPlaybackEngine(mock_engine)
        controller.set_display_scene_size((1920, 1080))
        proxy_frame = np.zeros((540, 960, 3), dtype=np.uint8)

        controller._submit_frame_playback(proxy_frame, 12)

        assert mock_engine.img_full_original is master
        assert mock_engine.img_source_full is master
        cmd, payload = renderer.submit.call_args.args
        assert cmd == RenderCommand.RENDER_VIDEO_FRAME
        assert payload["render_params"]["visible_rect"] == (0.0, 0.0, 960.0, 540.0)

    def test_static_preview_array_uses_the_timeline_display_target(self, mock_engine_v3_enabled) -> None:
        """Static thumbnails share the video preview's timeline display target.

        The thumbnail keeps its native upload size, while the renderer uses
        the Canvas-provided timeline target.  It must not manufacture a
        separate thumbnail-shaped presentation frame.
        """
        from src.features.color.core.async_renderer import RenderCommand
        from src.shared.video.video_playback import EnhancedVideoPlaybackEngine

        renderer = MagicMock()
        mock_engine_v3_enabled.renderer = renderer
        mock_engine_v3_enabled.layers = []
        mock_engine_v3_enabled.current_raw_params = {"enabled": False}
        mock_engine_v3_enabled.film_grain_params = {}
        mock_engine_v3_enabled.show_mask_mode = False
        mock_engine_v3_enabled.active_layer_index = 0
        mock_engine_v3_enabled.viewport_size = (160, 90)
        mock_engine_v3_enabled.visible_rect = (0.0, 0.0, 160.0, 90.0)

        controller = EnhancedVideoPlaybackEngine(mock_engine_v3_enabled)
        frame = np.full((50, 100, 3), 180, dtype=np.uint8)

        assert controller.submit_static_preview_array(frame, 12) is True

        cmd, payload = renderer.submit.call_args.args
        assert cmd == RenderCommand.RENDER_VIDEO_FRAME
        submitted = payload["frame"]
        # Native size preserved — no letterbox/resize, no pillarbox bars.
        assert submitted.shape == (50, 100, 3)
        assert np.all(submitted == 180)
        # The target is the timeline display aspect, not the thumbnail size.
        assert payload["render_params"]["viewport_size"] == (160, 90)
        # Effect and mask coordinates remain in the thumbnail's pixel space.
        assert payload["render_params"]["visible_rect"] == (0.0, 0.0, 100.0, 50.0)
        assert payload["render_params"]["scene_visible_rect"] == (0.0, 0.0, 100.0, 50.0)
        assert controller.current_frame == 12
        assert controller._last_displayed_frame_index == 12
        assert controller._last_displayed_frame is submitted


def test_paused_static_frame_updates_engine_source_with_timeline_scene() -> None:
    """Timeline scene size must not retain the previous clip while paused."""
    from src.shared.video.video_playback import EnhancedVideoPlaybackEngine

    controller = EnhancedVideoPlaybackEngine.__new__(EnhancedVideoPlaybackEngine)
    previous = np.zeros((1080, 1920, 3), dtype=np.uint8)
    current = np.full((2160, 3840, 3), 42, dtype=np.uint8)
    controller._display_scene_size = (1920, 1080)
    controller.is_playing = False
    controller.engine = SimpleNamespace(
        img_full_original=previous,
        img_source_full=previous,
    )

    EnhancedVideoPlaybackEngine._update_engine_video_frame_reference(controller, current)

    assert controller.engine.img_full_original is current
    assert controller.engine.img_source_full is current


class TestVideoTextureUploadState:
    """Video texture uploads must keep renderer source geometry in sync."""

    def test_packed_decoded_upload_rebuilds_source_tiles(self) -> None:
        import numpy as np
        from src.features.color.core.async_renderer import AsyncRenderer
        from src.shared.media.frame import DecodedFrame

        renderer = AsyncRenderer.__new__(AsyncRenderer)
        renderer.ctx = object()
        renderer._video_texture_pool = MagicMock()
        texture = MagicMock()
        renderer._video_texture_pool.acquire.return_value = texture
        renderer._ensure_source_tiles = MagicMock()
        renderer.current_video_texture = None
        renderer.current_video_planes = None
        renderer.current_video_pixel_format = None
        renderer.current_video_frame_index = -1
        renderer.video_source_size = (0, 0)
        renderer.source_image_size = (0, 0)
        renderer._video_needs_swizzle = False

        frame_data = np.zeros((360, 640, 3), dtype=np.uint8)
        decoded = DecodedFrame(
            frame_index=7,
            pts=None,
            time_seconds=0.0,
            data=frame_data,
            pixel_format='rgb24',
            width=640,
            height=360,
        )

        result = AsyncRenderer._do_upload_decoded_video_frame(
            renderer,
            {'decoded_frame': decoded},
        )

        assert result is texture
        renderer._ensure_source_tiles.assert_called_once_with(640, 360)
        assert renderer.video_source_size == (640, 360)
        assert renderer.source_image_size == (640, 360)

if __name__ == '__main__':
    pytest.main([__file__, '-v'])
