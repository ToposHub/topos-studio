"""
Playback HUD Tests
播放 HUD 系统的单元测试。

测试覆盖：
- PlaybackHUDModel 数据聚合
- PlaybackHUDPanel UI 组件
- PlaybackHUDController 集成
- PercentileStats 计算
- 导出日志功能
"""

import pytest
import time
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.shared.ui.playback_hud import (
    PlaybackHUDPanel,
    PlaybackHUDModel,
    PercentileStats,
    VideoPipelineInfo,
    PlaybackPerformanceStats,
)


# =============================================================================
# PercentileStats 测试
# =============================================================================

class TestPercentileStats:
    """PercentileStats 单元测试"""

    def test_basic_creation(self):
        """测试基本创建"""
        stats = PercentileStats(p50=10.0, p95=20.0, max=30.0, avg=15.0, count=100)
        assert stats.p50 == 10.0
        assert stats.p95 == 20.0
        assert stats.max == 30.0
        assert stats.avg == 15.0
        assert stats.count == 100

    def test_to_dict(self):
        """测试字典转换"""
        stats = PercentileStats(p50=10.0, p95=20.0, max=30.0)
        d = stats.to_dict()
        assert d['p50'] == 10.0
        assert d['p95'] == 20.0
        assert d['max'] == 30.0

    def test_format_display(self):
        """测试显示格式化"""
        stats = PercentileStats(p50=10.0, p95=20.0, max=30.0)
        display = stats.format_display()
        assert "p50=10.0" in display
        assert "p95=20.0" in display
        assert "max=30.0" in display
        assert "ms" in display

    def test_custom_unit(self):
        """测试自定义单位"""
        stats = PercentileStats(p50=10, p95=20, max=30)
        display = stats.format_display(unit='frames')
        assert "frames" in display


# =============================================================================
# VideoPipelineInfo 测试
# =============================================================================

class TestVideoPipelineInfo:
    """VideoPipelineInfo 单元测试"""

    def test_default_values(self):
        """测试默认值"""
        info = VideoPipelineInfo()
        assert info.pipeline == "unknown"
        assert info.backend == "unknown"
        assert info.codec == "unknown"

    def test_custom_values(self):
        """测试自定义值"""
        info = VideoPipelineInfo(
            pipeline="v3",
            backend="ffmpeg",
            codec="prores",
            pixel_format="yuv422p10le",
            source_type="disk_proxy",
            hwaccel="videotoolbox",
        )
        assert info.pipeline == "v3"
        assert info.backend == "ffmpeg"
        assert info.codec == "prores"
        assert info.hwaccel == "videotoolbox"

    def test_to_dict(self):
        """测试字典转换"""
        info = VideoPipelineInfo(pipeline="v3", backend="ffmpeg")
        d = info.to_dict()
        assert d['pipeline'] == "v3"
        assert d['backend'] == "ffmpeg"


# =============================================================================
# PlaybackHUDModel 测试
# =============================================================================

class TestPlaybackHUDModel:
    """PlaybackHUDModel 单元测试"""

    def test_init(self):
        """测试初始化"""
        model = PlaybackHUDModel()
        assert model.pipeline_info.pipeline == "unknown"
        assert model.perf_stats.frames_displayed == 0

    def test_reset(self):
        """测试重置"""
        model = PlaybackHUDModel()
        model._perf_stats.frames_displayed = 100
        model._perf_stats.frames_dropped = 5
        model.reset()
        assert model.perf_stats.frames_displayed == 0
        assert model.perf_stats.frames_dropped == 0

    def test_set_source_type(self):
        """测试源类型设置"""
        model = PlaybackHUDModel()
        model.set_source_type("disk_proxy", "planar_yuv_gpu")
        assert model.pipeline_info.source_type == "disk_proxy"
        assert model.pipeline_info.frame_path == "planar_yuv_gpu"

    def test_update_fps(self):
        """测试帧率更新"""
        model = PlaybackHUDModel()
        model.update_fps(24.0)
        assert model.perf_stats.fps == 24.0

    def test_update_frame_stats(self):
        """测试帧统计更新"""
        model = PlaybackHUDModel()
        model.update_frame_stats(displayed=100, dropped=5, held=3)
        assert model.perf_stats.frames_displayed == 100
        assert model.perf_stats.frames_dropped == 5
        assert model.perf_stats.frames_held == 3

    def test_record_sample(self):
        """测试样本记录"""
        model = PlaybackHUDModel()
        model.record_sample(decode_ms=15.0)
        model.record_sample(decode_ms=20.0)
        model.record_sample(decode_ms=25.0)

        # 验证 decode 统计
        assert model.perf_stats.decode_ms.count == 3
        assert model.perf_stats.decode_ms.avg == 20.0

    def test_record_multiple_samples(self):
        """测试多种样本记录"""
        model = PlaybackHUDModel()
        model.record_sample(
            decode_ms=15.0,
            upload_ms=2.0,
            render_ms=1.0,
            tick_ms=16.0,
            queue_depth=8,
        )
        model.record_sample(
            decode_ms=20.0,
            upload_ms=3.0,
            render_ms=2.0,
            tick_ms=20.0,
            queue_depth=10,
        )

        assert model.perf_stats.decode_ms.count == 2
        assert model.perf_stats.upload_ms.count == 2
        assert model.perf_stats.queue_depth.p95 >= 8

    def test_percentile_calculation(self):
        """测试百分位计算"""
        model = PlaybackHUDModel()

        # 添加 100 个样本（1-100）
        for i in range(1, 101):
            model.record_sample(decode_ms=float(i))

        # P50 应接近 50
        assert model.perf_stats.decode_ms.p50 == pytest.approx(50.0, abs=1)
        # P95 应接近 95
        assert model.perf_stats.decode_ms.p95 == pytest.approx(95.0, abs=1)
        # Max 应为 100
        assert model.perf_stats.decode_ms.max == 100.0

    def test_update_from_monitor_mock(self):
        """测试从监控器更新（Mock）"""
        model = PlaybackHUDModel()

        # 创建 Mock 监控器
        mock_monitor = MagicMock()
        mock_monitor._decode_times = {0: 10.0, 1: 20.0, 2: 30.0}
        mock_monitor._upload_times = {0: 2.0, 1: 3.0}
        mock_monitor._tick_times = [16.0, 17.0, 18.0]
        mock_monitor._render_submit_times = [1.0, 2.0]
        mock_monitor._queue_depths = [5, 8, 10]

        mock_stats = {
            'decode_p50_ms': 20.0,
            'decode_p95_ms': 30.0,
            'decode_max_ms': 30.0,
            'decode_avg_ms': 20.0,
            'frames_processed': 100,
            'dropped_frames': 5,
            'held_frames': 3.0,
            'cache_hit_rate': 0.8,
            'degradation_level': 1,
            'tick_p50_ms': 17.0,
            'tick_p95_ms': 18.0,
            'tick_max_ms': 18.0,
            'tick_avg_ms': 17.0,
            'tick_count': 3,
            'upload_p50_ms': 2.5,
            'upload_p95_ms': 3.0,
            'upload_max_ms': 3.0,
            'upload_avg_ms': 2.5,
            'render_p50_ms': 1.5,
            'render_p95_ms': 2.0,
            'render_max_ms': 2.0,
            'render_avg_ms': 1.5,
            'queue_p50_depth': 8.0,
            'queue_p95_depth': 10.0,
            'queue_max_depth': 10.0,
            'queue_avg_depth': 7.67,
        }
        mock_monitor.get_aggregate_stats.return_value = mock_stats

        model.update_from_monitor(mock_monitor)

        assert model.perf_stats.frames_displayed == 100
        assert model.perf_stats.frames_dropped == 5
        assert model.perf_stats.degradation_level == 1

    def test_get_full_report(self):
        """测试完整报告"""
        model = PlaybackHUDModel()
        model._video_path = "/test/video.mp4"
        model.update_frame_stats(displayed=100, dropped=5)

        report = model.get_full_report()
        assert 'timestamp' in report
        assert report['video_path'] == "/test/video.mp4"
        assert report['performance_stats']['frames_displayed'] == 100

    def test_export_log(self, tmp_path):
        """测试日志导出"""
        model = PlaybackHUDModel()
        model._video_path = "/test/video.mp4"
        model.update_frame_stats(displayed=100, dropped=5)

        output_path = str(tmp_path / "test_log.json")
        result_path = model.export_log(output_path)

        assert Path(result_path).exists()

        # 读取验证
        with open(result_path, 'r') as f:
            data = json.load(f)
        assert data['video_path'] == "/test/video.mp4"


# =============================================================================
# PlaybackHUDPanel 测试（需要 QApplication）
# =============================================================================

@pytest.mark.skipif(
    not pytest.importorskip("PyQt6", reason="PyQt6 not available"),
    reason="PyQt6 tests skipped"
)
class TestPlaybackHUDPanel:
    """PlaybackHUDPanel 单元测试"""

    def test_panel_creation(self, qapp):
        """测试面板创建"""
        panel = PlaybackHUDPanel()
        assert panel is not None
        assert panel.model is not None

    def test_toggle_visibility(self, qapp):
        """测试可见性切换"""
        panel = PlaybackHUDPanel()
        assert not panel.is_visible()

        panel.toggle_visibility()
        assert panel.is_visible()

        panel.toggle_visibility()
        assert not panel.is_visible()

    def test_show_hide(self, qapp):
        """测试显示和隐藏"""
        panel = PlaybackHUDPanel()
        assert not panel.is_visible()

        panel.show_hud()
        assert panel.is_visible()

        panel.hide_hud()
        assert not panel.is_visible()

    def test_set_update_interval(self, qapp):
        """测试更新间隔设置"""
        panel = PlaybackHUDPanel()
        panel.set_update_interval(100)
        assert panel._update_interval == 100

        # 最小值限制
        panel.set_update_interval(50)
        assert panel._update_interval == 100  # 应被限制为 100

    def test_update_from_session_mock(self, qapp):
        """测试从会话更新（Mock）"""
        panel = PlaybackHUDPanel()

        mock_session = MagicMock()
        mock_stats = MagicMock()
        mock_stats.total_frames_played = 100
        mock_stats.total_frames_dropped = 5
        mock_stats.total_play_time = 10.0
        mock_stats.current_fps = 24.0
        mock_session.stats = mock_stats

        panel.update_from_session(mock_session)

        assert panel.model.perf_stats.frames_displayed == 100
        assert panel.model.perf_stats.frames_dropped == 5


# =============================================================================
# PlaybackHUDController 测试
# =============================================================================

@pytest.mark.skipif(
    not pytest.importorskip("PyQt6", reason="PyQt6 not available"),
    reason="PyQt6 tests skipped"
)
class TestPlaybackHUDController:
    """PlaybackHUDController 单元测试"""

    def test_controller_creation(self, qapp):
        """测试控制器创建"""
        from src.shared.ui.playback_hud_integration import PlaybackHUDController

        controller = PlaybackHUDController()
        assert controller is not None
        assert controller.panel is not None
        assert controller.model is not None

    def test_on_playback_started(self, qapp):
        """测试播放开始事件"""
        from src.shared.ui.playback_hud_integration import PlaybackHUDController

        controller = PlaybackHUDController()
        controller.on_playback_started(
            video_path="/test/video.mp4",
            metadata=None,
            pipeline_decision=None,
        )

        assert controller.model.video_path == "/test/video.mp4"

    def test_on_play_tick(self, qapp):
        """测试播放 tick"""
        from src.shared.ui.playback_hud_integration import PlaybackHUDController

        controller = PlaybackHUDController()

        # 模拟会话
        mock_session = MagicMock()
        mock_stats = MagicMock()
        mock_stats.total_frames_played = 100
        mock_stats.total_frames_dropped = 5
        mock_stats.total_play_time = 10.0
        mock_stats.current_fps = 24.0
        mock_session.stats = mock_stats
        controller.connect_session(mock_session)

        # Mock the performance monitor to avoid overwrite from global monitor
        mock_monitor = MagicMock()
        mock_monitor.get_aggregate_stats.return_value = {
            'frames_processed': 100,
            'dropped_frames': 5,
            'held_frames': 0,
            'cache_hit_rate': 0.0,
            'degradation_level': 0,
        }
        controller._monitor = mock_monitor

        controller.on_play_tick()

        assert controller.model.perf_stats.frames_displayed == 100


# =============================================================================
# 集成测试
# =============================================================================

class TestPlaybackHUDIntegration:
    """PlaybackHUD 与 PerformanceMonitor 集成测试"""

    def test_performance_monitor_percentile_integration(self):
        """测试 PerformanceMonitor 百分位集成"""
        from src.shared.media.performance_monitor import VideoPerformanceMonitor

        monitor = VideoPerformanceMonitor()
        monitor.start_session()

        # 添加样本
        for i in range(1, 101):
            monitor.track_decode_time(i, float(i))
            monitor.track_upload_time(i, float(i) / 10)
            monitor.track_tick_time(float(i) + 16.0)

        # 获取统计
        stats = monitor.get_aggregate_stats()

        # 验证百分位
        assert stats['decode_p50_ms'] == pytest.approx(50.0, abs=1)
        assert stats['decode_p95_ms'] == pytest.approx(95.0, abs=1)
        assert stats['decode_max_ms'] == 100.0

        monitor.end_session()

    def test_model_monitor_integration(self):
        """测试 Model 与 Monitor 集成"""
        from src.shared.media.performance_monitor import VideoPerformanceMonitor

        model = PlaybackHUDModel()
        monitor = VideoPerformanceMonitor()
        monitor.start_session()

        # 添加样本
        for i in range(1, 51):
            monitor.track_decode_time(i, float(i))
            monitor.track_upload_time(i, float(i) / 10)
            monitor.track_tick_time(16.0 + float(i) / 10)

        # 更新 Model
        model.update_from_monitor(monitor)

        # 验证数据传递
        assert model.perf_stats.decode_ms.count >= 50

        monitor.end_session()


# =============================================================================
# 运行测试
# =============================================================================

if __name__ == '__main__':
    pytest.main([__file__, '-v'])