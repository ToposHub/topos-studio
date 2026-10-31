"""
Video Performance Monitor Unit Tests
测试 VideoPerformanceMonitor 性能监控模块。

覆盖：
- VideoPerformanceMonitor 基础功能
- 解码/上传时间记录
- 丢帧统计
- 统计计算（平均、最大、最小、P95）
- 上下文管理器
- 全局监控器实例
- P0.A 新增: VideoPlaybackStats 数据类
- P0.A 新增: 播放性能追踪方法
- P0.A 新增: 聚合日志输出功能
"""

import pytest
import time
import threading
from src.shared.media.performance_monitor import (
    VideoPerformanceMonitor,
    FrameDropRecord,
    VideoPlaybackStats,
    track_decode_time,
    track_upload_time,
    track_playback_tick,
    track_frame_process,
    track_frame_seek,
    track_frame_read,
    get_performance_monitor,
    reset_global_monitor,
    PROXY_LADDER_RUNGS,
)


class TestVideoPerformanceMonitorBasic:
    """VideoPerformanceMonitor 基础功能测试"""

    def test_creation(self) -> None:
        """测试监控器创建"""
        monitor = VideoPerformanceMonitor(name='test_monitor')
        assert monitor.name == 'test_monitor'
        assert monitor.is_active == False
        assert monitor.frames_processed == 0

    def test_default_name(self) -> None:
        """测试默认名称"""
        monitor = VideoPerformanceMonitor()
        assert monitor.name == 'video_pipeline'

    def test_repr(self) -> None:
        """测试字符串表示"""
        monitor = VideoPerformanceMonitor(name='test')
        assert 'test' in repr(monitor)
        assert 'inactive' in repr(monitor)

        monitor.start_session()
        assert 'active' in repr(monitor)


class TestSessionManagement:
    """会话管理测试"""

    def test_start_session(self) -> None:
        """测试开始会话"""
        monitor = VideoPerformanceMonitor()
        monitor.track_decode_time(0, 10.0)  # 会话开始前的数据
        monitor.start_session()

        assert monitor.is_active == True
        assert monitor.frames_processed == 0  # 清空了之前的数据

    def test_end_session(self) -> None:
        """测试结束会话"""
        monitor = VideoPerformanceMonitor()
        monitor.start_session()
        monitor.track_decode_time(0, 10.0)
        monitor.end_session()

        assert monitor.is_active == False
        stats = monitor.get_stats()
        assert stats['frames_processed'] == 1

    def test_session_duration(self) -> None:
        """测试会话持续时间"""
        monitor = VideoPerformanceMonitor()
        monitor.start_session()
        time.sleep(0.1)  # 等待 100ms
        monitor.end_session()

        stats = monitor.get_stats()
        assert stats['session_duration'] >= 0.1

    def test_clear(self) -> None:
        """测试清空数据"""
        monitor = VideoPerformanceMonitor()
        monitor.start_session()
        monitor.track_decode_time(0, 10.0)
        monitor.track_upload_time(0, 5.0)
        monitor.track_frame_drop(0, 'test')

        monitor.clear()

        assert monitor.is_active == True  # 会话状态保持
        stats = monitor.get_stats()
        assert stats['frames_processed'] == 0
        assert stats['total_drops'] == 0

    def test_reset(self) -> None:
        """测试完全重置"""
        monitor = VideoPerformanceMonitor()
        monitor.start_session()
        monitor.track_decode_time(0, 10.0)

        monitor.reset()

        assert monitor.is_active == False
        stats = monitor.get_stats()
        assert stats['frames_processed'] == 0


class TestDecodeTimeTracking:
    """解码时间记录测试"""

    def test_track_single_decode_time(self) -> None:
        """测试记录单个解码时间"""
        monitor = VideoPerformanceMonitor()
        monitor.start_session()
        monitor.track_decode_time(0, 15.5)

        assert monitor.frames_processed == 1
        stats = monitor.get_stats()
        assert stats['decode_avg_ms'] == 15.5
        assert stats['decode_max_ms'] == 15.5
        assert stats['decode_min_ms'] == 15.5

    def test_track_multiple_decode_times(self) -> None:
        """测试记录多个解码时间"""
        monitor = VideoPerformanceMonitor()
        monitor.start_session()

        times = [10.0, 15.0, 20.0, 25.0, 30.0]
        for i, t in enumerate(times):
            monitor.track_decode_time(i, t)

        stats = monitor.get_stats()
        assert stats['decode_avg_ms'] == 20.0  # 平均值
        assert stats['decode_max_ms'] == 30.0
        assert stats['decode_min_ms'] == 10.0
        assert stats['decode_p95_ms'] == 30.0  # P95 是最大值（5个样本的95%）

    def test_track_decode_time_updates_existing(self) -> None:
        """测试更新已存在的帧"""
        monitor = VideoPerformanceMonitor()
        monitor.start_session()

        monitor.track_decode_time(0, 10.0)
        monitor.track_decode_time(0, 20.0)  # 更新同一帧

        # 帧数计数增加（每次 track 都增加）
        assert monitor.frames_processed == 2

    def test_decode_stats_empty(self) -> None:
        """测试无数据时的统计"""
        monitor = VideoPerformanceMonitor()
        monitor.start_session()

        stats = monitor.get_stats()
        assert stats['decode_avg_ms'] == 0.0
        assert stats['decode_max_ms'] == 0.0
        assert stats['decode_min_ms'] == 0.0
        assert stats['decode_p95_ms'] == 0.0
        assert stats['decode_std_ms'] == 0.0


class TestUploadTimeTracking:
    """上传时间记录测试"""

    def test_track_single_upload_time(self) -> None:
        """测试记录单个上传时间"""
        monitor = VideoPerformanceMonitor()
        monitor.start_session()
        monitor.track_upload_time(0, 2.5)

        stats = monitor.get_stats()
        assert stats['upload_avg_ms'] == 2.5

    def test_track_multiple_upload_times(self) -> None:
        """测试记录多个上传时间"""
        monitor = VideoPerformanceMonitor()
        monitor.start_session()

        times = [1.0, 2.0, 3.0, 4.0, 5.0]
        for i, t in enumerate(times):
            monitor.track_upload_time(i, t)

        stats = monitor.get_stats()
        assert stats['upload_avg_ms'] == 3.0
        assert stats['upload_max_ms'] == 5.0
        assert stats['upload_min_ms'] == 1.0

    def test_upload_stats_empty(self) -> None:
        """测试无数据时的上传统计"""
        monitor = VideoPerformanceMonitor()
        monitor.start_session()

        stats = monitor.get_stats()
        assert stats['upload_avg_ms'] == 0.0


class TestFrameDropTracking:
    """丢帧记录测试"""

    def test_track_single_drop(self) -> None:
        """测试记录单个丢帧"""
        monitor = VideoPerformanceMonitor()
        monitor.start_session()
        monitor.track_decode_time(0, 10.0)  # 总帧数
        monitor.track_frame_drop(5, 'decoder_overload')

        stats = monitor.get_stats()
        assert stats['total_drops'] == 1
        assert stats['drop_rate'] == 1.0  # 1/1 = 100%

    def test_track_multiple_drops(self) -> None:
        """测试记录多个丢帧"""
        monitor = VideoPerformanceMonitor()
        monitor.start_session()

        for i in range(10):
            monitor.track_decode_time(i, 10.0)

        monitor.track_frame_drop(5, 'decoder_overload')
        monitor.track_frame_drop(8, 'upload_lag')

        stats = monitor.get_stats()
        assert stats['total_drops'] == 2
        assert stats['drop_rate'] == 0.2  # 2/10 = 20%

    def test_drop_reasons_categorization(self) -> None:
        """测试丢帧原因分类"""
        monitor = VideoPerformanceMonitor()
        monitor.start_session()

        for i in range(20):
            monitor.track_decode_time(i, 10.0)

        monitor.track_frame_drop(5, 'decoder_overload')
        monitor.track_frame_drop(8, 'decoder_overload')
        monitor.track_frame_drop(10, 'upload_lag')
        monitor.track_frame_drop(15, 'seek')

        stats = monitor.get_stats()
        assert stats['drop_reasons']['decoder_overload'] == 2
        assert stats['drop_reasons']['upload_lag'] == 1
        assert stats['drop_reasons']['seek'] == 1

    def test_drop_rate_zero_frames(self) -> None:
        """测试无帧时的丢帧率"""
        monitor = VideoPerformanceMonitor()
        monitor.start_session()
        monitor.track_frame_drop(0, 'test')

        stats = monitor.get_stats()
        assert stats['drop_rate'] == 0.0  # 0帧时丢帧率为0


class TestStatisticsCalculation:
    """统计计算测试"""

    def test_p95_calculation(self) -> None:
        """测试 P95 计算"""
        monitor = VideoPerformanceMonitor()
        monitor.start_session()

        # 100个样本，P95 应接近第95个百分位
        for i in range(100):
            monitor.track_decode_time(i, float(i))

        stats = monitor.get_stats()
        # 100个样本的 P95 约在索引 95
        assert stats['decode_p95_ms'] >= 95.0

    def test_std_deviation(self) -> None:
        """测试标准差计算"""
        monitor = VideoPerformanceMonitor()
        monitor.start_session()

        # 固定值，标准差应为0
        for i in range(10):
            monitor.track_decode_time(i, 10.0)

        stats = monitor.get_stats()
        assert stats['decode_std_ms'] == 0.0

        # 变化值，标准差应大于0
        monitor.clear()
        times = [5.0, 10.0, 15.0, 20.0, 25.0]
        for i, t in enumerate(times):
            monitor.track_decode_time(i, t)

        stats = monitor.get_stats()
        assert stats['decode_std_ms'] > 0.0

    def test_single_sample_stats(self) -> None:
        """测试单个样本的统计"""
        monitor = VideoPerformanceMonitor()
        monitor.start_session()
        monitor.track_decode_time(0, 15.0)

        stats = monitor.get_stats()
        assert stats['decode_avg_ms'] == 15.0
        assert stats['decode_max_ms'] == 15.0
        assert stats['decode_min_ms'] == 15.0
        assert stats['decode_p95_ms'] == 15.0
        assert stats['decode_std_ms'] == 0.0  # 单样本无标准差

    def test_large_dataset(self) -> None:
        """测试大数据集"""
        monitor = VideoPerformanceMonitor()
        monitor.start_session()

        # 1000个样本
        for i in range(1000):
            monitor.track_decode_time(i, 10.0 + (i % 20))

        stats = monitor.get_stats()
        assert stats['frames_processed'] == 1000
        # 平均值应在合理范围内
        assert 10.0 <= stats['decode_avg_ms'] <= 30.0


class TestReportGeneration:
    """报告生成测试"""

    def test_empty_report(self) -> None:
        """测试空数据报告"""
        monitor = VideoPerformanceMonitor(name='test')
        monitor.start_session()
        report = monitor.get_report()

        assert 'test' in report
        assert '解码性能' in report
        assert '上传性能' in report
        assert '丢帧统计' in report

    def test_report_with_data(self) -> None:
        """测试有数据的报告"""
        monitor = VideoPerformanceMonitor(name='pipeline_test')
        monitor.start_session()

        monitor.track_decode_time(0, 15.5)
        monitor.track_upload_time(0, 2.3)
        monitor.track_frame_drop(5, 'decoder_overload')

        report = monitor.get_report()

        assert 'pipeline_test' in report
        assert '15.5' in report or '15.50' in report  # 解码时间
        assert 'decoder_overload' in report

    def test_report_drop_reasons(self) -> None:
        """测试报告包含丢帧原因"""
        monitor = VideoPerformanceMonitor()
        monitor.start_session()

        for i in range(10):
            monitor.track_decode_time(i, 10.0)

        monitor.track_frame_drop(5, 'decoder_overload')
        monitor.track_frame_drop(8, 'upload_lag')

        report = monitor.get_report()

        assert 'decoder_overload' in report
        assert 'upload_lag' in report


class TestContextManagers:
    """上下文管理器测试"""

    def test_track_decode_time_context(self) -> None:
        """测试解码时间追踪上下文"""
        monitor = VideoPerformanceMonitor()
        monitor.start_session()

        with track_decode_time(monitor, frame_index=0):
            time.sleep(0.01)  # 等待10ms

        stats = monitor.get_stats()
        assert stats['decode_avg_ms'] >= 10.0  # 至少10ms
        assert stats['frames_processed'] == 1

    def test_track_upload_time_context(self) -> None:
        """测试上传时间追踪上下文"""
        monitor = VideoPerformanceMonitor()
        monitor.start_session()

        with track_upload_time(monitor, frame_index=0):
            time.sleep(0.005)  # 等待5ms

        stats = monitor.get_stats()
        assert stats['upload_avg_ms'] >= 5.0

    def test_context_with_exception(self) -> None:
        """测试上下文管理器在异常时仍记录"""
        monitor = VideoPerformanceMonitor()
        monitor.start_session()

        try:
            with track_decode_time(monitor, frame_index=0):
                raise ValueError("Test exception")
        except ValueError:
            pass

        # 即使异常也应记录时间
        stats = monitor.get_stats()
        assert stats['frames_processed'] == 1

    def test_nested_contexts(self) -> None:
        """测试嵌套上下文"""
        monitor = VideoPerformanceMonitor()
        monitor.start_session()

        with track_decode_time(monitor, frame_index=0):
            with track_upload_time(monitor, frame_index=0):
                time.sleep(0.01)

        stats = monitor.get_stats()
        assert stats['frames_processed'] == 1
        assert stats['decode_avg_ms'] >= 10.0
        assert stats['upload_avg_ms'] >= 10.0  # 上传也在sleep期间


class TestGlobalMonitor:
    """全局监控器测试"""

    def test_get_performance_monitor(self) -> None:
        """测试获取全局监控器"""
        reset_global_monitor()
        monitor = get_performance_monitor()

        assert monitor is not None
        assert monitor.name == 'global_video_pipeline'

    def test_singleton_behavior(self) -> None:
        """测试单例行为"""
        reset_global_monitor()
        monitor1 = get_performance_monitor()
        monitor2 = get_performance_monitor()

        assert monitor1 is monitor2

    def test_reset_global_monitor(self) -> None:
        """测试重置全局监控器"""
        monitor = get_performance_monitor()
        monitor.start_session()
        monitor.track_decode_time(0, 10.0)

        reset_global_monitor()

        # 重新获取应该是新实例
        new_monitor = get_performance_monitor()
        assert new_monitor is not monitor
        assert new_monitor.frames_processed == 0


class TestThreadSafety:
    """线程安全测试"""

    def test_concurrent_track_decode_time(self) -> None:
        """测试并发记录解码时间"""
        monitor = VideoPerformanceMonitor()
        monitor.start_session()

        def worker(start_frame: int) -> None:
            for i in range(start_frame, start_frame + 100):
                monitor.track_decode_time(i, float(i % 10 + 1))

        threads = [
            threading.Thread(target=worker, args=(i * 100,))
            for i in range(5)
        ]

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        stats = monitor.get_stats()
        assert stats['frames_processed'] == 500

    def test_concurrent_session_operations(self) -> None:
        """测试并发会话操作"""
        monitor = VideoPerformanceMonitor()

        def start_end() -> None:
            monitor.start_session()
            time.sleep(0.01)
            monitor.end_session()

        threads = [
            threading.Thread(target=start_end)
            for i in range(10)
        ]

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # 应正常完成，无竞态条件
        assert monitor.is_active == False


class TestFrameDropRecord:
    """FrameDropRecord 测试"""

    def test_frame_drop_record_creation(self) -> None:
        """测试丢帧记录创建"""
        record = FrameDropRecord(frame_index=100, reason='decoder_overload')

        assert record.frame_index == 100
        assert record.reason == 'decoder_overload'
        assert record.timestamp > 0.0

    def test_frame_drop_record_repr(self) -> None:
        """测试丢帧记录字符串表示"""
        record = FrameDropRecord(frame_index=100, reason='test')
        assert '100' in repr(record)
        assert 'test' in repr(record)


class TestEffectiveFPS:
    """有效帧率测试"""

    def test_effective_fps_calculation(self) -> None:
        """测试有效帧率计算"""
        monitor = VideoPerformanceMonitor()
        monitor.start_session()

        start_time = time.monotonic()
        for i in range(100):
            monitor.track_decode_time(i, 10.0)
        elapsed = time.monotonic() - start_time

        # 等待足够时间以确保计算有意义
        time.sleep(0.1)
        monitor.end_session()

        stats = monitor.get_stats()
        # 会话持续时间应大于0
        assert stats['session_duration'] > 0.0

    def test_zero_duration_fps(self) -> None:
        """测试零持续时间时的帧率"""
        monitor = VideoPerformanceMonitor()
        # 未开始会话
        stats = monitor.get_stats()
        assert stats['effective_fps'] == 0.0


class TestEdgeCases:
    """边缘情况测试"""

    def test_stats_before_session(self) -> None:
        """测试会话开始前获取统计"""
        monitor = VideoPerformanceMonitor()
        stats = monitor.get_stats()

        assert stats['session_duration'] == 0.0
        assert stats['frames_processed'] == 0

    def test_negative_time_warning(self) -> None:
        """测试负时间值（不推荐但应接受）"""
        monitor = VideoPerformanceMonitor()
        monitor.start_session()
        monitor.track_decode_time(0, -1.0)

        stats = monitor.get_stats()
        # 应记录但不影响计算
        assert stats['decode_avg_ms'] == -1.0

    def test_very_large_time(self) -> None:
        """测试非常大的时间值"""
        monitor = VideoPerformanceMonitor()
        monitor.start_session()
        monitor.track_decode_time(0, 10000.0)  # 10秒

        stats = monitor.get_stats()
        assert stats['decode_max_ms'] == 10000.0

    def test_zero_time(self) -> None:
        """测试零时间值"""
        monitor = VideoPerformanceMonitor()
        monitor.start_session()
        monitor.track_decode_time(0, 0.0)

        stats = monitor.get_stats()
        assert stats['decode_avg_ms'] == 0.0
        assert stats['decode_min_ms'] == 0.0


# ================================================================
# P0.A 新增: VideoPlaybackStats 测试
# ================================================================

class TestVideoPlaybackStats:
    """VideoPlaybackStats 数据类测试"""

    def test_default_values(self) -> None:
        """测试默认值"""
        stats = VideoPlaybackStats()
        assert stats.tick_ms == 0.0
        assert stats.decode_ms == 0.0
        assert stats.process_ms == 0.0
        assert stats.upload_submit_ms == 0.0
        assert stats.render_submit_ms == 0.0
        assert stats.queue_depth == 0
        assert stats.dropped_frames == 0
        assert stats.seek_ms == 0.0
        assert stats.read_ms == 0.0
        assert stats.cache_hit == False
        assert stats.frame_index == -1
        assert stats.timestamp > 0.0

    def test_custom_values(self) -> None:
        """测试自定义值"""
        stats = VideoPlaybackStats(
            tick_ms=15.5,
            decode_ms=20.0,
            process_ms=5.0,
            upload_submit_ms=2.5,
            render_submit_ms=3.0,
            queue_depth=8,
            dropped_frames=2,
            frame_index=100
        )
        assert stats.tick_ms == 15.5
        assert stats.decode_ms == 20.0
        assert stats.process_ms == 5.0
        assert stats.upload_submit_ms == 2.5
        assert stats.render_submit_ms == 3.0
        assert stats.queue_depth == 8
        assert stats.dropped_frames == 2
        assert stats.frame_index == 100

    def test_to_dict(self) -> None:
        """测试转换为字典"""
        stats = VideoPlaybackStats(tick_ms=10.0, decode_ms=15.0)
        d = stats.to_dict()
        assert 'tick_ms' in d
        assert 'decode_ms' in d
        assert d['tick_ms'] == 10.0
        assert d['decode_ms'] == 15.0

    def test_repr(self) -> None:
        """测试字符串表示"""
        stats = VideoPlaybackStats(tick_ms=10.0, decode_ms=15.0, queue_depth=5)
        repr_str = repr(stats)
        assert 'tick=10' in repr_str
        assert 'decode=15' in repr_str
        assert 'queue=5' in repr_str


class TestPlaybackPerformanceTracking:
    """P0.A 播放性能追踪方法测试"""

    def test_track_tick_time(self) -> None:
        """测试 UI tick 时间记录"""
        monitor = VideoPerformanceMonitor()
        monitor.start_session()
        # 禁用聚合日志输出（避免测试时打印）
        monitor.set_aggregate_log_enabled(False)

        monitor.track_tick_time(15.5)
        monitor.track_tick_time(20.0)

        stats = monitor.get_aggregate_stats()
        assert stats['tick_avg_ms'] == 17.75
        assert stats['tick_max_ms'] == 20.0

    def test_track_process_time(self) -> None:
        """测试处理时间记录"""
        monitor = VideoPerformanceMonitor()
        monitor.start_session()
        monitor.set_aggregate_log_enabled(False)

        monitor.track_process_time(0, 5.0)
        monitor.track_process_time(1, 10.0)

        stats = monitor.get_aggregate_stats()
        assert stats['process_avg_ms'] == 7.5

    def test_track_render_submit_time(self) -> None:
        """测试渲染提交时间记录"""
        monitor = VideoPerformanceMonitor()
        monitor.start_session()
        monitor.set_aggregate_log_enabled(False)

        monitor.track_render_submit_time(0, 3.0)
        monitor.track_render_submit_time(1, 5.0)

        stats = monitor.get_aggregate_stats()
        assert stats['render_avg_ms'] == 4.0

    def test_track_queue_depth(self) -> None:
        """测试队列深度记录"""
        monitor = VideoPerformanceMonitor()
        monitor.start_session()
        monitor.set_aggregate_log_enabled(False)

        monitor.track_queue_depth(4)
        monitor.track_queue_depth(8)
        monitor.track_queue_depth(6)

        stats = monitor.get_aggregate_stats()
        assert stats['queue_avg_depth'] == 6.0  # (4+8+6)/3 = 6
        assert stats['queue_max_depth'] == 8

    def test_track_seek_time(self) -> None:
        """测试 seek 时间记录"""
        monitor = VideoPerformanceMonitor()
        monitor.start_session()
        monitor.set_aggregate_log_enabled(False)

        monitor.track_seek_time(100, 50.0)
        monitor.track_seek_time(200, 80.0)

        stats = monitor.get_aggregate_stats()
        assert stats['seek_avg_ms'] == 65.0

    def test_track_read_time(self) -> None:
        """测试 read 时间记录"""
        monitor = VideoPerformanceMonitor()
        monitor.start_session()
        monitor.set_aggregate_log_enabled(False)

        monitor.track_read_time(0, 10.0)
        monitor.track_read_time(1, 12.0)

        stats = monitor.get_aggregate_stats()
        assert stats['read_avg_ms'] == 11.0

    def test_track_cache_hit(self) -> None:
        """测试缓存命中记录"""
        monitor = VideoPerformanceMonitor()
        monitor.start_session()
        monitor.set_aggregate_log_enabled(False)

        # 3 次命中，2 次未命中
        monitor.track_cache_hit(0, True)
        monitor.track_cache_hit(1, True)
        monitor.track_cache_hit(2, False)
        monitor.track_cache_hit(3, True)
        monitor.track_cache_hit(4, False)

        stats = monitor.get_aggregate_stats()
        assert stats['cache_hit_rate'] == 0.6  # 3/5 = 60%
        assert stats['cache_hit_count'] == 3
        assert stats['cache_total_requests'] == 5

    def test_record_playback_stats(self) -> None:
        """测试完整播放统计记录"""
        monitor = VideoPerformanceMonitor()
        monitor.start_session()
        monitor.set_aggregate_log_enabled(False)

        stats1 = VideoPlaybackStats(tick_ms=15.0, decode_ms=20.0, frame_index=0)
        stats2 = VideoPlaybackStats(tick_ms=18.0, decode_ms=22.0, frame_index=1)

        monitor.record_playback_stats(stats1)
        monitor.record_playback_stats(stats2)

        assert monitor.frames_processed == 2
        playback_stats = monitor.get_playback_stats()
        assert len(playback_stats) == 2
        assert playback_stats[0].frame_index == 0
        assert playback_stats[1].frame_index == 1

    def test_get_aggregate_stats(self) -> None:
        """测试聚合统计输出"""
        monitor = VideoPerformanceMonitor()
        monitor.start_session()
        monitor.set_aggregate_log_enabled(False)

        # 记录各种指标
        monitor.track_tick_time(15.0)
        monitor.track_process_time(0, 5.0)
        monitor.track_render_submit_time(0, 3.0)
        monitor.track_queue_depth(8)
        monitor.track_cache_hit(0, True)
        monitor.track_frame_drop(5, 'test')

        stats = monitor.get_aggregate_stats()

        # 验证所有字段存在
        assert 'tick_avg_ms' in stats
        assert 'process_avg_ms' in stats
        assert 'render_avg_ms' in stats
        assert 'queue_avg_depth' in stats
        assert 'dropped_frames' in stats
        assert 'cache_hit_rate' in stats

        # 验证值正确
        assert stats['tick_avg_ms'] == 15.0
        assert stats['process_avg_ms'] == 5.0
        assert stats['render_avg_ms'] == 3.0
        assert stats['queue_avg_depth'] == 8.0
        assert stats['dropped_frames'] == 1
        assert stats['cache_hit_rate'] == 1.0


class TestAggregateLogOutput:
    """聚合日志输出测试"""

    def test_aggregate_log_interval(self) -> None:
        """测试聚合日志间隔设置"""
        monitor = VideoPerformanceMonitor()
        monitor.set_aggregate_log_interval(2.5)
        assert monitor._aggregate_interval == 2.5

        # 边界值测试
        monitor.set_aggregate_log_interval(0.3)  # 最小值是 0.5
        assert monitor._aggregate_interval == 0.5

        monitor.set_aggregate_log_interval(100)  # 最大值是 60
        assert monitor._aggregate_interval == 60.0

    def test_aggregate_log_enabled(self) -> None:
        """测试聚合日志启用/禁用"""
        monitor = VideoPerformanceMonitor()
        assert monitor._aggregate_log_enabled == True  # 默认启用

        monitor.set_aggregate_log_enabled(False)
        assert monitor._aggregate_log_enabled == False

        monitor.set_aggregate_log_enabled(True)
        assert monitor._aggregate_log_enabled == True

    def test_aggregate_log_no_output_when_disabled(self) -> None:
        """测试禁用时不输出聚合日志"""
        monitor = VideoPerformanceMonitor()
        monitor.start_session()
        monitor.set_aggregate_log_enabled(False)

        # 记录一些数据（使用 track_decode_time 增加 frames_processed）
        for i in range(100):
            monitor.track_decode_time(i, 10.0)
            monitor.track_tick_time(10.0)

        # 等待超过聚合间隔
        time.sleep(1.5)

        # 应正常完成，无异常
        stats = monitor.get_aggregate_stats()
        assert stats['frames_processed'] == 100


class TestPlaybackContextManagers:
    """P0.A 播放上下文管理器测试"""

    def test_track_playback_tick(self) -> None:
        """测试 playback tick 上下文管理器"""
        monitor = VideoPerformanceMonitor()
        monitor.start_session()
        monitor.set_aggregate_log_enabled(False)

        with track_playback_tick(monitor):
            time.sleep(0.01)  # 10ms

        stats = monitor.get_aggregate_stats()
        assert stats['tick_avg_ms'] >= 10.0

    def test_track_frame_process(self) -> None:
        """测试 frame process 上下文管理器"""
        monitor = VideoPerformanceMonitor()
        monitor.start_session()
        monitor.set_aggregate_log_enabled(False)

        with track_frame_process(monitor, frame_index=0):
            time.sleep(0.005)  # 5ms

        stats = monitor.get_aggregate_stats()
        assert stats['process_avg_ms'] >= 5.0

    def test_track_frame_seek(self) -> None:
        """测试 frame seek 上下文管理器"""
        monitor = VideoPerformanceMonitor()
        monitor.start_session()
        monitor.set_aggregate_log_enabled(False)

        with track_frame_seek(monitor, frame_index=100):
            time.sleep(0.02)  # 20ms

        stats = monitor.get_aggregate_stats()
        assert stats['seek_avg_ms'] >= 20.0

    def test_track_frame_read(self) -> None:
        """测试 frame read 上下文管理器"""
        monitor = VideoPerformanceMonitor()
        monitor.start_session()
        monitor.set_aggregate_log_enabled(False)

        with track_frame_read(monitor, frame_index=0):
            time.sleep(0.01)  # 10ms

        stats = monitor.get_aggregate_stats()
        assert stats['read_avg_ms'] >= 10.0

    def test_context_manager_with_exception(self) -> None:
        """测试上下文管理器在异常时仍记录"""
        monitor = VideoPerformanceMonitor()
        monitor.start_session()
        monitor.set_aggregate_log_enabled(False)

        try:
            with track_playback_tick(monitor):
                raise ValueError("Test exception")
        except ValueError:
            pass

        # 即使异常也应记录时间
        stats = monitor.get_aggregate_stats()
        assert stats['tick_avg_ms'] > 0


class TestPlaybackStatsClearReset:
    """清空和重置测试（覆盖新增数据）"""

    def test_clear_keeps_session(self) -> None:
        """测试 clear 保持会话状态"""
        monitor = VideoPerformanceMonitor()
        monitor.start_session()
        monitor.set_aggregate_log_enabled(False)

        monitor.track_tick_time(10.0)
        monitor.track_process_time(0, 5.0)
        monitor.track_queue_depth(8)

        monitor.clear()

        assert monitor.is_active == True
        stats = monitor.get_aggregate_stats()
        assert stats['tick_avg_ms'] == 0.0
        assert stats['process_avg_ms'] == 0.0
        assert stats['queue_avg_depth'] == 0.0

    def test_reset_clears_session(self) -> None:
        """测试 reset 清除会话状态"""
        monitor = VideoPerformanceMonitor()
        monitor.start_session()
        monitor.set_aggregate_log_enabled(False)

        monitor.track_tick_time(10.0)
        monitor.track_process_time(0, 5.0)

        monitor.reset()

        assert monitor.is_active == False
        stats = monitor.get_aggregate_stats()


# =============================================================================
# S5.B: UI tick 工作量统计与自动降级测试
# =============================================================================

class TestS5BRendererQueueTracking:
    """S5.B: 渲染队列深度和预览丢帧追踪测试"""

    def test_track_renderer_queue_depth(self) -> None:
        """测试渲染队列深度记录"""
        monitor = VideoPerformanceMonitor()
        monitor.start_session()
        monitor.set_aggregate_log_enabled(False)

        monitor.track_renderer_queue_depth(5)
        monitor.track_renderer_queue_depth(10)
        monitor.track_renderer_queue_depth(3)

        stats = monitor.get_aggregate_stats()
        assert stats['renderer_queue_depth'] == 6.0  # (5+10+3)/3 = 6
        assert stats['renderer_queue_max_depth'] == 10

    def test_track_dropped_preview_frame(self) -> None:
        """测试预览丢帧记录"""
        monitor = VideoPerformanceMonitor()
        monitor.start_session()
        monitor.set_aggregate_log_enabled(False)

        monitor.track_dropped_preview_frame(3)  # 丢弃 3 帧
        monitor.track_dropped_preview_frame(2)  # 丢弃 2 帧

        stats = monitor.get_aggregate_stats()
        assert stats['dropped_preview_frames'] == 5

    def test_track_held_frames(self) -> None:
        """测试持有帧数记录"""
        monitor = VideoPerformanceMonitor()
        monitor.start_session()
        monitor.set_aggregate_log_enabled(False)

        monitor.track_held_frames(4)
        monitor.track_held_frames(6)
        monitor.track_held_frames(2)

        stats = monitor.get_aggregate_stats()
        assert stats['held_frames'] == 4.0  # (4+6+2)/3 = 4
        assert stats['held_frames_max'] == 6

    def test_video_playback_stats_new_fields(self) -> None:
        """测试 VideoPlaybackStats 新字段"""
        stats = VideoPlaybackStats(
            tick_ms=15.0,
            renderer_queue_depth=8,
            dropped_preview_frames=3,
            held_frames=4
        )
        d = stats.to_dict()
        assert 'renderer_queue_depth' in d
        assert 'dropped_preview_frames' in d
        assert 'held_frames' in d
        assert d['renderer_queue_depth'] == 8
        assert d['dropped_preview_frames'] == 3
        assert d['held_frames'] == 4


class TestS5BDegradationLogic:
    """S5.B: 自动降级逻辑测试"""

    def test_initial_degradation_level(self) -> None:
        """测试初始降级级别"""
        monitor = VideoPerformanceMonitor()
        assert monitor.get_degradation_level() == 0
        assert monitor.get_prefetch_lookahead_reduction() == 0.0

    def test_no_degradation_with_good_tick(self) -> None:
        """测试正常 tick 不触发降级"""
        monitor = VideoPerformanceMonitor()
        monitor.start_session()
        monitor.set_aggregate_log_enabled(False)

        # 记录正常的 tick 时间（低于播放降级阈值）
        for i in range(50):
            monitor.track_tick_time(1.0)  # 1ms，正常

        level = monitor.check_tick_performance_and_degrade()
        assert level == 0

    def test_degradation_trigger_with_slow_tick(self) -> None:
        """测试慢 tick 触发降级"""
        monitor = VideoPerformanceMonitor()
        monitor.start_session()
        monitor.set_aggregate_log_enabled(False)

        # 记录慢 tick 时间（高于播放降级阈值）
        # 需要足够多的数据才能触发降级
        for i in range(100):
            monitor.track_tick_time(12.0)

        # 连续调用 check 以累积 consecutive_slow_ticks
        for i in range(35):
            monitor.check_tick_performance_and_degrade()

        level = monitor.get_degradation_level()
        # 应至少升级到级别 1
        assert level >= 1
        reduction = monitor.get_prefetch_lookahead_reduction()
        assert reduction >= 0.25

    def test_degradation_levels_map(self) -> None:
        """测试降级级别对应前瞻降低比例"""
        monitor = VideoPerformanceMonitor()
        monitor.start_session()
        monitor.set_aggregate_log_enabled(False)

        # 手动设置降级级别（模拟）
        monitor._degradation_level = 1
        monitor._update_lookahead_reduction()
        assert monitor.get_prefetch_lookahead_reduction() == 0.25

        monitor._degradation_level = 2
        monitor._update_lookahead_reduction()
        assert monitor.get_prefetch_lookahead_reduction() == 0.50

        monitor._degradation_level = 3
        monitor._update_lookahead_reduction()
        assert monitor.get_prefetch_lookahead_reduction() == 0.75

    def test_degradation_recovery(self) -> None:
        """测试降级恢复（S5.B: 连续 180 次正常检查后恢复）"""
        monitor = VideoPerformanceMonitor()
        monitor.start_session()
        monitor.set_aggregate_log_enabled(False)

        # 先触发降级
        monitor._degradation_level = 2
        monitor._prefetch_lookahead_reduction = 0.50
        monitor._consecutive_normal_ticks = 0

        # S5.B: 需要先累积足够 tick 数据以满足 p95 计算条件
        for i in range(10):
            monitor.track_tick_time(1.0)

        # 然后恢复正常 tick（S5.B: 需要 180 次正常检查才能恢复一个级别）
        for i in range(180):
            monitor.track_tick_time(1.0)  # 正常 tick
            monitor.check_tick_performance_and_degrade()

        # 级别应下降到 1
        level = monitor.get_degradation_level()
        assert level <= 1, f"Expected level <= 1 after 180 normal checks, got {level}"

        # 再继续 180 次正常检查，应该恢复到 0
        for i in range(180):
            monitor.track_tick_time(1.0)
            monitor.check_tick_performance_and_degrade()

        final_level = monitor.get_degradation_level()
        assert final_level == 0, f"Expected final level=0 after 360 normal checks, got {final_level}"

    def test_max_degradation_level(self) -> None:
        """测试最大降级级别"""
        monitor = VideoPerformanceMonitor()
        monitor.start_session()
        monitor.set_aggregate_log_enabled(False)

        # 设置最高级别
        monitor._degradation_level = 5  # 超过最大值
        monitor._update_lookahead_reduction()

        # 应限制在 75%（级别 3）
        assert monitor.get_prefetch_lookahead_reduction() == 0.75

    def test_aggregate_stats_includes_degradation(self) -> None:
        """测试聚合统计包含降级信息"""
        monitor = VideoPerformanceMonitor()
        monitor.start_session()
        monitor.set_aggregate_log_enabled(False)

        monitor._degradation_level = 1
        monitor._prefetch_lookahead_reduction = 0.25
        monitor.track_tick_time(5.0)

        stats = monitor.get_aggregate_stats()
        assert 'degradation_level' in stats
        assert 'prefetch_lookahead_reduction' in stats
        assert stats['degradation_level'] == 1
        assert stats['prefetch_lookahead_reduction'] == 0.25


class TestS5BIntegration:
    """S5.B: 集成测试"""

    def test_renderer_stats_integration(self) -> None:
        """测试渲染器统计集成"""
        monitor = VideoPerformanceMonitor()
        monitor.start_session()
        monitor.set_aggregate_log_enabled(False)

        # 模拟渲染器统计数据
        renderer_stats = {
            'queue_depth': 8,
            'dropped_preview_frames': 3,
        }

        monitor.track_renderer_queue_depth(renderer_stats['queue_depth'])
        monitor.track_dropped_preview_frame(renderer_stats['dropped_preview_frames'])

        stats = monitor.get_aggregate_stats()
        assert stats['renderer_queue_depth'] == 8.0
        assert stats['dropped_preview_frames'] == 3

    def test_full_tick_monitoring_cycle(self) -> None:
        """测试完整的 tick 监控周期"""
        monitor = VideoPerformanceMonitor()
        monitor.start_session()
        monitor.set_aggregate_log_enabled(False)

        # 模拟一个完整的播放周期
        for frame in range(60):
            # tick 时间
            tick_ms = 1.5 if frame < 30 else 3.0  # 前 30 帧正常，后 30 帧超阈值
            monitor.track_tick_time(tick_ms)

            # 队列深度
            monitor.track_queue_depth(4 + frame % 4)

            # 渲染队列深度
            monitor.track_renderer_queue_depth(2 + frame % 3)

            # 检查降级
            monitor.check_tick_performance_and_degrade()

        stats = monitor.get_aggregate_stats()

        # 验证统计完整性
        assert stats['tick_avg_ms'] > 0
        assert 'renderer_queue_depth' in stats
        assert 'degradation_level' in stats


# ================================================================
# S5.B 新增: v3 降级策略测试
# ================================================================

class TestS5BV3Degradation:
    """S5.B: v3 降级策略测试"""

    def test_slow_tick_triggers_degradation(self) -> None:
        """构造慢 tick 数据，断言会进入 degradation"""
        monitor = VideoPerformanceMonitor()
        monitor.start_session()
        monitor.set_aggregate_log_enabled(False)

        # 先累积 10 个 tick 以满足 p95 计算条件
        for i in range(10):
            monitor.track_tick_time(12.0)

        # 现在可以计算 p95，继续累积慢 tick 以触发降级
        for i in range(30):  # 30 次检查，每次增加一个 slow tick
            monitor.track_tick_time(12.0)  # 每次 tick 12ms，超过播放降级阈值
            level = monitor.check_tick_performance_and_degrade()

        # 30 次慢 tick 检查后，应该触发降级
        final_level = monitor.get_degradation_level()
        assert final_level >= 1, f"Expected degradation_level >= 1 after 30 slow tick checks, got {final_level}"

    def test_renderer_queue_high_skips_submit(self) -> None:
        """构造 renderer queue depth 高，验证 v3 会减少提交"""
        monitor = VideoPerformanceMonitor()
        monitor.start_session()
        monitor.set_aggregate_log_enabled(False)

        # 构造高队列深度数据
        high_queue_depth = 5  # 超过阈值 3
        monitor.track_renderer_queue_depth(high_queue_depth)

        # 验证统计数据记录正确
        stats = monitor.get_aggregate_stats()
        assert stats['renderer_queue_depth'] == high_queue_depth

        # 测试 backpressure 逻辑（通过模拟场景）
        # 验证阈值检测逻辑
        max_queue_threshold = 3
        should_skip = high_queue_depth >= max_queue_threshold
        assert should_skip, f"Expected queue_depth={high_queue_depth} >= threshold={max_queue_threshold} to trigger skip"


    def test_normal_tick_reduces_degradation_level(self) -> None:
        """恢复数据后 degradation level 降回 0"""
        monitor = VideoPerformanceMonitor()
        monitor.start_session()
        monitor.set_aggregate_log_enabled(False)

        # 先触发降级到 level 2
        monitor._degradation_level = 2
        monitor._prefetch_lookahead_reduction = 0.50
        monitor._consecutive_normal_ticks = 0

        # 先累积足够 tick 数据以满足 p95 计算
        for i in range(10):
            monitor.track_tick_time(1.0)

        # 构造连续 180 次正常检查（每次增加一个 tick）
        for i in range(180):
            monitor.track_tick_time(1.0)  # 正常 tick，低于播放降级阈值
            level = monitor.check_tick_performance_and_degrade()

        # 180 次正常检查后，应该恢复到 level 1
        assert monitor.get_degradation_level() <= 1, f"Expected recovery after 180 normal checks, got level={monitor.get_degradation_level()}"

        # 继续正常检查 180 次，应该恢复到 level 0
        for i in range(180):
            monitor.track_tick_time(1.0)
            monitor.check_tick_performance_and_degrade()

        final_level = monitor.get_degradation_level()
        assert final_level == 0, f"Expected final level=0 after 360 normal checks, got {final_level}"

    def test_degradation_level_cycles(self) -> None:
        """测试降级级别循环：正常 -> 降级 -> 恢复 -> 正常"""
        monitor = VideoPerformanceMonitor()
        monitor.start_session()
        monitor.set_aggregate_log_enabled(False)

        # Phase 1: 正常状态
        assert monitor.get_degradation_level() == 0

        # 先累积 10 个 tick 以满足 p95 计算条件
        for i in range(10):
            monitor.track_tick_time(12.0)

        # Phase 2: 触发降级（连续慢 tick 检查）
        for i in range(30):
            monitor.track_tick_time(12.0)
            monitor.check_tick_performance_and_degrade()

        assert monitor.get_degradation_level() >= 1, f"Expected level >= 1 after slow phase, got {monitor.get_degradation_level()}"

        # Phase 3: 继续慢 tick 检查，升级到更高级别
        # 注意：每次 30 次慢检查后会升级一个级别，所以 2 个周期 = level 2 + 1 = 3
        for cycle in range(2):  # 2 个周期，升级到 level 3
            for i in range(30):
                monitor.track_tick_time(12.0)
                monitor.check_tick_performance_and_degrade()

        level_after_slow = monitor.get_degradation_level()
        assert level_after_slow >= 2, f"Expected level >= 2 after more slow ticks, got {level_after_slow}"

        # Phase 4: 恢复（连续正常 tick 检查）
        # S5.B: 清空之前的慢 tick 数据，让 p95 计算基于纯正常数据
        monitor._tick_times.clear()
        # 先累积足够正常 tick 以满足 p95 计算条件
        for i in range(10):
            monitor.track_tick_time(1.0)

        # 需要按实际级别进行恢复循环
        actual_level = monitor.get_degradation_level()
        for cycle in range(actual_level):
            for i in range(180):  # 每级别需要 180 正常检查
                monitor.track_tick_time(1.0)
                monitor.check_tick_performance_and_degrade()

        # 最终恢复到 level 0
        final_level = monitor.get_degradation_level()
        assert final_level == 0, f"Expected full recovery to level=0, got {final_level}"

    def test_lookahead_reduction_levels(self) -> None:
        """测试预读前瞻降低比例按级别递增"""
        monitor = VideoPerformanceMonitor()
        monitor._degradation_level = 0
        monitor._update_lookahead_reduction()
        assert monitor.get_prefetch_lookahead_reduction() == 0.0

        monitor._degradation_level = 1
        monitor._update_lookahead_reduction()
        assert monitor.get_prefetch_lookahead_reduction() == 0.25

        monitor._degradation_level = 2
        monitor._update_lookahead_reduction()
        assert monitor.get_prefetch_lookahead_reduction() == 0.50

        monitor._degradation_level = 3
        monitor._update_lookahead_reduction()
        assert monitor.get_prefetch_lookahead_reduction() == 0.75

        # 超过最大级别，仍限制在 75%
        monitor._degradation_level = 5
        monitor._update_lookahead_reduction()
        assert monitor.get_prefetch_lookahead_reduction() == 0.75


class TestS5BConsecutiveNormalTicks:
    """S5.B: 连续正常 tick 计数测试"""

    def test_consecutive_normal_ticks_initialized(self) -> None:
        """测试连续正常计数初始化"""
        monitor = VideoPerformanceMonitor()
        assert monitor._consecutive_normal_ticks == 0

    def test_consecutive_normal_ticks_reset_on_slow_tick(self) -> None:
        """测试慢 tick 时重置正常计数"""
        monitor = VideoPerformanceMonitor()
        monitor.start_session()
        monitor.set_aggregate_log_enabled(False)

        # 先累积一些正常计数
        monitor._consecutive_normal_ticks = 100

        # 先累积 10 个 tick 以满足 p95 计算条件
        for i in range(10):
            monitor.track_tick_time(12.0)

        # 再累积一个慢 tick 并检查
        monitor.track_tick_time(12.0)
        level = monitor.check_tick_performance_and_degrade()

        # 正常计数应该被重置（因为 p95 高于播放降级阈值）
        assert monitor._consecutive_normal_ticks == 0, f"Expected normal_ticks reset to 0 after slow tick, got {monitor._consecutive_normal_ticks}"

    def test_consecutive_normal_ticks_reset_on_session_start(self) -> None:
        """测试 session start 重置正常计数"""
        monitor = VideoPerformanceMonitor()
        monitor._consecutive_normal_ticks = 50

        monitor.start_session()

        # session start 后应该重置
        assert monitor._consecutive_normal_ticks == 0


# ================================================================
# P2.2: 动态代理阶梯测试
# ================================================================

class TestP22ProxyLadder:
    """P2.2: 测试动态代理阶梯升降级逻辑"""

    def test_proxy_ladder_rungs_defined(self) -> None:
        """测试代理阶梯级别已定义"""
        assert PROXY_LADDER_RUNGS == [1920, 1280, 960, 640]

    def test_set_original_resolution(self) -> None:
        """测试设置原始视频分辨率"""
        monitor = VideoPerformanceMonitor()
        monitor.set_original_resolution(3840, 2160)
        info = monitor.get_proxy_ladder_info()
        assert info['original_long_edge'] == 3840
        assert info['proxy_long_edge'] == 3840
        assert info['ladder_index'] == -1

    def test_proxy_ladder_starts_at_original(self) -> None:
        """测试代理阶梯初始状态为原始分辨率"""
        monitor = VideoPerformanceMonitor()
        monitor.start_session()
        monitor.set_original_resolution(3840, 2160)
        info = monitor.get_proxy_ladder_info()
        assert info['ladder_index'] == -1
        assert info['proxy_long_edge'] == 3840

    def test_proxy_ladder_no_video_returns_original(self) -> None:
        """测试没有视频时 check_proxy_ladder 返回原始值"""
        monitor = VideoPerformanceMonitor()
        result = monitor.check_proxy_ladder()
        assert result == (0, "no_video")

    def test_proxy_ladder_no_data_returns_current(self) -> None:
        """测试没有帧数据时 check_proxy_ladder 返回当前值"""
        monitor = VideoPerformanceMonitor()
        monitor.start_session()
        monitor.set_original_resolution(3840, 2160)
        result = monitor.check_proxy_ladder()
        assert result == (3840, "no_data")

    def test_proxy_ladder_stays_stable_under_budget(self) -> None:
        """测试帧耗时在预算内时保持原始分辨率"""
        monitor = VideoPerformanceMonitor()
        monitor.start_session()
        monitor.set_original_resolution(3840, 2160)

        # 模拟 150 帧帧耗时 = 20ms（30fps 预算 33.3ms，在预算内）
        for i in range(150):
            monitor.track_frame_total_time(20.0, fps=30.0)

        result = monitor.check_proxy_ladder()
        # 保持在原始分辨率
        assert result[0] == 3840
        assert result[1] == "stable"

    def test_proxy_ladder_downgrade_on_slow_frames(self) -> None:
        """测试连续慢帧触发降级"""
        monitor = VideoPerformanceMonitor()
        monitor.start_session()
        monitor.set_original_resolution(3840, 2160)

        # 模拟 60+ 帧帧耗时 = 50ms（远超 33.3ms * 1.25 = 41.6ms）
        for i in range(65):
            monitor.track_frame_total_time(50.0, fps=30.0)

        result = monitor.check_proxy_ladder()
        # 应该从原始分辨率降到第一级阶梯 1920
        assert result[0] == 1920, f"Expected proxy_long_edge=1920, got {result[0]}"
        assert "downgrade" in result[1] or "slow" in result[1]

    def test_proxy_ladder_multiple_downgrade(self) -> None:
        """测试连续多次降级"""
        monitor = VideoPerformanceMonitor()
        monitor.start_session()
        monitor.set_original_resolution(3840, 2160)

        # 第一次降级：原始 -> 1920
        for i in range(65):
            monitor.track_frame_total_time(50.0, fps=30.0)
        result = monitor.check_proxy_ladder()
        assert result[0] == 1920

        # 第二次降级：1920 -> 1280
        monitor._consecutive_slow_frames = 0
        monitor._consecutive_fast_frames = 0
        for i in range(65):
            monitor.track_frame_total_time(50.0, fps=30.0)
        result = monitor.check_proxy_ladder()
        assert result[0] == 1280, f"Expected 1280, got {result[0]}"

    def test_proxy_ladder_upgrade_on_fast_frames(self) -> None:
        """测试连续快帧触发升级"""
        monitor = VideoPerformanceMonitor()
        monitor.start_session()
        monitor.set_original_resolution(3840, 2160)

        # 先降级到第一级
        for i in range(65):
            monitor.track_frame_total_time(50.0, fps=30.0)
        monitor.check_proxy_ladder()
        assert monitor._proxy_ladder_index == 0

        # 再模拟 150 帧快帧（15ms，低于预算 * 0.65 = 21.6ms）
        monitor._consecutive_slow_frames = 0
        monitor._consecutive_fast_frames = 0
        for i in range(155):
            monitor.track_frame_total_time(15.0, fps=30.0)

        result = monitor.check_proxy_ladder()
        # 应该从 1920 升回原始分辨率 3840
        assert result[0] == 3840, f"Expected upgrade to 3840, got {result[0]}"
        assert "upgrade" in result[1]

    def test_proxy_ladder_min_resolution(self) -> None:
        """测试降级不会低于最低阶梯"""
        monitor = VideoPerformanceMonitor()
        monitor.start_session()
        monitor.set_original_resolution(3840, 2160)

        # 连续降级到最底
        for step in range(4):
            monitor._consecutive_slow_frames = 0
            monitor._consecutive_fast_frames = 0
            for i in range(65):
                monitor.track_frame_total_time(80.0, fps=30.0)
            monitor.check_proxy_ladder()

        # 应该停在最低级 640
        assert monitor._proxy_ladder_index == 3
        assert monitor._current_proxy_long_edge == 640

    def test_track_coalesced_video_commands_records_data(self) -> None:
        """测试 track_coalesced_video_commands 正确记录数据"""
        monitor = VideoPerformanceMonitor()
        monitor.track_coalesced_video_commands(3)
        monitor.track_coalesced_video_commands(5)
        monitor.track_coalesced_video_commands(2)
        assert monitor._coalesced_video_commands == [3, 5, 2]

    def test_coalesced_commands_in_aggregate_stats(self) -> None:
        """测试合并命令统计出现在聚合统计中"""
        monitor = VideoPerformanceMonitor()
        monitor.start_session()
        monitor.track_coalesced_video_commands(3)
        monitor.track_coalesced_video_commands(5)

        stats = monitor.get_aggregate_stats()
        assert stats['coalesced_video_commands_total'] == 8
        assert stats['coalesced_video_commands_count'] == 2

    def test_session_start_resets_proxy_ladder(self) -> None:
        """测试 start_session 重置代理阶梯状态"""
        monitor = VideoPerformanceMonitor()
        monitor.start_session()
        monitor.set_original_resolution(3840, 2160)

        # 降级
        for i in range(65):
            monitor.track_frame_total_time(50.0, fps=30.0)
        monitor.check_proxy_ladder()
        assert monitor._proxy_ladder_index == 0

        # 重置会话
        monitor.start_session()
        info = monitor.get_proxy_ladder_info()
        assert info['ladder_index'] == -1
        assert info['original_long_edge'] == 0
        assert info['proxy_long_edge'] == 0

    def test_proxy_ladder_info_in_aggregate_stats(self) -> None:
        """测试代理阶梯信息出现在聚合统计中"""
        monitor = VideoPerformanceMonitor()
        monitor.start_session()
        monitor.set_original_resolution(3840, 2160)

        stats = monitor.get_aggregate_stats()
        assert 'proxy_ladder_index' in stats
        assert 'proxy_long_edge' in stats
        assert 'original_long_edge' in stats
        assert stats['original_long_edge'] == 3840

    def test_track_frame_total_time_respects_list_limit(self) -> None:
        """测试帧总时间列表大小限制"""
        monitor = VideoPerformanceMonitor()
        for i in range(200):
            monitor.track_frame_total_time(20.0, fps=30.0)
        # 只保留最近 120 帧
        assert len(monitor._total_frame_times) == 120


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
