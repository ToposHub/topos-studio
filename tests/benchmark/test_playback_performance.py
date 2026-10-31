"""
Video Playback Performance Acceptance Tests
视频播放性能验收测试。

测试验收指标：
- playback_1080p_h264_fps
- playback_4k_h264_fps
- playback_4k_prores_fps

测试方法：
1. 使用 MockPlaybackEnvironment 模拟播放环境
2. 收集播放期间的性能数据
3. 统计有效帧率和稳定性指标
4. 与验收标准对比生成结果
"""

from __future__ import annotations

import pytest
import time
import threading
import numpy as np
from typing import Dict, Any, List, Optional
from dataclasses import dataclass

from tests.benchmark.benchmark_base import (
    VideoPerformanceBenchmark,
    MockPlaybackEnvironment,
    PerformanceTimeSeries,
    calculate_fps,
    calculate_percentile,
)
from tests.benchmark.acceptance_criteria import (
    AcceptanceResult,
    AcceptanceLevel,
    METRICS,
    create_result_from_measurement,
)


class PlaybackFPSBenchmark(VideoPerformanceBenchmark):
    """
    播放帧率基准测试。

    测试播放流畅度指标。
    """

    # 测试配置
    TEST_DURATION_SECONDS: float = 30.0

    def __init__(
        self,
        resolution: tuple = (1920, 1080),
        codec: str = "h264",
        target_fps: float = 30.0,
    ) -> None:
        """
        初始化播放帧率测试。

        Args:
            resolution: 视频分辨率
            codec: 编码格式
            target_fps: 目标帧率
        """
        super().__init__()
        self.resolution = resolution
        self.codec = codec
        self.target_fps = target_fps
        self._mock_env: Optional[MockPlaybackEnvironment] = None

    def setup_mock_environment(self) -> Dict[str, Any]:
        """设置 Mock 环境。"""
        self._mock_env = MockPlaybackEnvironment(
            video_resolution=self.resolution,
            fps=self.target_fps,
            total_frames=int(self.TEST_DURATION_SECONDS * self.target_fps),
        )
        return {
            'monitor': self._mock_env.monitor,
            'decoder': self._mock_env.mock_decoder,
            'renderer': self._mock_env.mock_renderer,
        }

    def get_metric_id(self) -> str:
        """获取指标 ID。"""
        if self.resolution == (1920, 1080):
            return "playback_1080p_h264_fps"
        elif self.resolution == (3840, 2160):
            if self.codec == "prores":
                return "playback_4k_prores_fps"
            else:
                return "playback_4k_h264_fps"
        return "playback_1080p_h264_fps"

    def run_benchmark(self) -> AcceptanceResult:
        """
        运行播放帧率基准测试。

        Returns:
            验收结果
        """
        test_start = time.monotonic()

        # 设置环境
        env = self.setup_mock_environment()

        # 开始记录
        tick_series = self.start_recording('tick')
        fps_series = self.start_recording('fps')

        # 模拟播放
        with self.benchmark_context():
            frame_times: List[float] = []

            for i in range(int(self.TEST_DURATION_SECONDS * self.target_fps)):
                # 模拟播放 tick
                tick_data = env['monitor'].simulated_tick if hasattr(env['monitor'], 'simulated_tick') else {}

                # 使用 Mock 环境模拟
                if self._mock_env:
                    tick_data = self._mock_env.simulate_playback_tick()

                # 记录 tick 时间
                self.record_sample('tick', tick_data.get('tick_ms', 0.0))

                # 记录帧时间（用于计算 FPS）
                frame_time = tick_data.get('tick_ms', 0.0)
                if frame_time > 0:
                    frame_times.append(frame_time)

                # 计算当前 FPS
                if len(frame_times) >= 10:
                    recent_fps = calculate_fps(frame_times[-10:])
                    self.record_sample('fps', recent_fps)

                # 模拟帧间隔
                time.sleep(1.0 / self.target_fps * 0.001)

        # 停止记录
        tick_series = self.stop_recording('tick')
        fps_series = self.stop_recording('fps')

        test_end = time.monotonic()
        test_duration_ms = (test_end - test_start) * 1000

        # 计算统计
        tick_stats = tick_series.calculate_stats()
        fps_stats = fps_series.calculate_stats()

        # 有效帧率（平均 FPS）
        effective_fps = fps_stats['avg']

        # 稳定性指标
        fps_stability = fps_stats['std'] / fps_stats['avg'] if fps_stats['avg'] > 0 else 0.0

        # 创建结果
        details = {
            'resolution': f"{self.resolution[0]}x{self.resolution[1]}",
            'codec': self.codec,
            'target_fps': self.target_fps,
            'effective_fps': effective_fps,
            'fps_avg': fps_stats['avg'],
            'fps_min': fps_stats['min'],
            'fps_max': fps_stats['max'],
            'fps_std': fps_stats['std'],
            'fps_stability': fps_stability,
            'tick_avg_ms': tick_stats['avg'],
            'tick_p95_ms': tick_stats['p95'],
            'frame_count': tick_stats['count'],
            'test_duration_s': self.TEST_DURATION_SECONDS,
        }

        message = (
            f"播放 {self.resolution[0]}x{self.resolution[1]} {self.codec} 视频 "
            f"{self.TEST_DURATION_SECONDS}s，"
            f"有效帧率: {effective_fps:.1f}fps，"
            f"稳定性: {fps_stability:.2%}"
        )

        return create_result_from_measurement(
            metric_id=self.get_metric_id(),
            measured_value=effective_fps,
            details=details,
            message=message,
            test_duration_ms=test_duration_ms,
        )


class StressPlaybackBenchmark(VideoPerformanceBenchmark):
    """
    压力播放测试。

    测试在高负载下的播放稳定性。
    """

    def __init__(
        self,
        stress_factor: float = 2.0,
        resolution: tuple = (3840, 2160),
    ) -> None:
        """
        初始化压力测试。

        Args:
            stress_factor: 压力因子
            resolution: 视频分辨率
        """
        super().__init__()
        self.stress_factor = stress_factor
        self.resolution = resolution

    def setup_mock_environment(self) -> Dict[str, Any]:
        """设置 Mock 环境。"""
        self._mock_env = MockPlaybackEnvironment(
            video_resolution=self.resolution,
            fps=30.0,
            total_frames=100,
        )
        return {
            'monitor': self._mock_env.monitor,
        }

    def get_metric_id(self) -> str:
        """获取指标 ID。"""
        return "playback_4k_h264_fps"  # 使用 4K 指标

    def run_benchmark(self) -> AcceptanceResult:
        """运行压力测试。"""
        test_start = time.monotonic()

        env = self.setup_mock_environment()

        with self.benchmark_context():
            tick_series = self.start_recording('tick')

            # 模拟压力播放
            stats = env['monitor'].simulated_stress if hasattr(env['monitor'], 'simulated_stress') else {}
            if self._mock_env:
                stats = self._mock_env.simulate_stress_playback(self.stress_factor)

            # 记录压力下的性能
            for tick_ms in stats.get('tick_times', []):
                self.record_sample('tick', tick_ms)

        tick_series = self.stop_recording('tick')
        tick_stats = tick_series.calculate_stats()

        test_end = time.monotonic()

        # 压力下的帧率估计
        avg_tick_ms = tick_stats['avg']
        stress_fps = 1000.0 / avg_tick_ms if avg_tick_ms > 0 else 0.0

        details = {
            'stress_factor': self.stress_factor,
            'resolution': f"{self.resolution[0]}x{self.resolution[1]}",
            'stress_fps': stress_fps,
            'tick_avg_ms': tick_stats['avg'],
            'tick_p95_ms': tick_stats['p95'],
            'tick_max_ms': tick_stats['max'],
        }

        message = (
            f"压力播放测试 (factor={self.stress_factor})，"
            f"压力帧率: {stress_fps:.1f}fps，"
            f"tick p95: {tick_stats['p95']:.1f}ms"
        )

        return create_result_from_measurement(
            metric_id=self.get_metric_id(),
            measured_value=stress_fps,
            details=details,
            message=message,
            test_duration_ms=(test_end - test_start) * 1000,
        )


# ================================================================
# pytest 测试函数
# ================================================================

@pytest.fixture
def playback_benchmark_1080p():
    """1080p 播放基准测试 fixture。"""
    return PlaybackFPSBenchmark(
        resolution=(1920, 1080),
        codec="h264",
        target_fps=30.0,
    )


@pytest.fixture
def playback_benchmark_4k_h264():
    """4K H.264 播放基准测试 fixture。"""
    return PlaybackFPSBenchmark(
        resolution=(3840, 2160),
        codec="h264",
        target_fps=30.0,
    )


@pytest.fixture
def playback_benchmark_4k_prores():
    """4K ProRes 播放基准测试 fixture。"""
    return PlaybackFPSBenchmark(
        resolution=(3840, 2160),
        codec="prores",
        target_fps=30.0,
    )


@pytest.mark.benchmark
@pytest.mark.acceptance
class TestPlaybackFPSAcceptance:
    """播放帧率验收测试。"""

    def test_1080p_h264_playback_fps(self, playback_benchmark_1080p) -> None:
        """
        测试 1080p H.264 播放帧率验收。

        验收标准：
        - 最低目标: 稳定 24/30fps
        - 专业目标: 稳定 60fps
        """
        result = playback_benchmark_1080p.run_benchmark()

        # 输出结果摘要
        print(f"\n[验收测试] {result.metric_id}")
        print(f"  测量值: {result.measured_value:.1f}fps")
        print(f"  验收级别: {result.level.value}")
        print(f"  通过: {result.passed}")
        print(f"  消息: {result.message}")

        # 验证最低目标
        assert result.level in (AcceptanceLevel.PROFESSIONAL, AcceptanceLevel.MINIMUM), \
            f"播放帧率未达标: {result.measured_value:.1f}fps < 24fps"

    def test_4k_h264_playback_fps(self, playback_benchmark_4k_h264) -> None:
        """
        测试 4K H.264/H.265 播放帧率验收。

        验收标准：
        - 最低目标: 稳定 24/30fps
        - 专业目标: 低延迟 60fps 视机器
        """
        result = playback_benchmark_4k_h264.run_benchmark()

        print(f"\n[验收测试] {result.metric_id}")
        print(f"  测量值: {result.measured_value:.1f}fps")
        print(f"  验收级别: {result.level.value}")
        print(f"  通过: {result.passed}")
        print(f"  消息: {result.message}")

        # 验证最低目标
        assert result.level in (AcceptanceLevel.PROFESSIONAL, AcceptanceLevel.MINIMUM), \
            f"播放帧率未达标: {result.measured_value:.1f}fps < 24fps"

    def test_4k_prores_playback_fps(self, playback_benchmark_4k_prores) -> None:
        """
        测试 4K ProRes 播放帧率验收。

        验收标准：
        - 最低目标: 稳定 24/30fps
        - 专业目标: 30/60fps 视磁盘/CPU
        """
        result = playback_benchmark_4k_prores.run_benchmark()

        print(f"\n[验收测试] {result.metric_id}")
        print(f"  测量值: {result.measured_value:.1f}fps")
        print(f"  验收级别: {result.level.value}")
        print(f"  通过: {result.passed}")
        print(f"  消息: {result.message}")

        # 验证最低目标
        assert result.level in (AcceptanceLevel.PROFESSIONAL, AcceptanceLevel.MINIMUM), \
            f"播放帧率未达标: {result.measured_value:.1f}fps < 24fps"

    def test_stress_playback_stability(self) -> None:
        """
        测试压力播放稳定性。

        在解码延迟增大的情况下，播放应保持稳定。
        """
        benchmark = StressPlaybackBenchmark(
            stress_factor=2.0,
            resolution=(3840, 2160),
        )

        result = benchmark.run_benchmark()

        print(f"\n[压力测试] {result.metric_id}")
        print(f"  压力帧率: {result.measured_value:.1f}fps")
        print(f"  详情: {result.details}")

        # 压力测试不要求达到正常帧率，但应保持稳定
        # 至少应大于 10fps（约为最低目标的 40%）
        assert result.measured_value > 10.0, \
            f"压力播放不稳定: {result.measured_value:.1f}fps < 10fps"


def run_playback_benchmarks() -> Dict[str, AcceptanceResult]:
    """
    运行所有播放基准测试。

    Returns:
        结果字典
    """
    results: Dict[str, AcceptanceResult] = {}

    # 1080p H.264
    benchmark_1080p = PlaybackFPSBenchmark(
        resolution=(1920, 1080),
        codec="h264",
        target_fps=30.0,
    )
    results["playback_1080p_h264_fps"] = benchmark_1080p.run_benchmark()

    # 4K H.264
    benchmark_4k = PlaybackFPSBenchmark(
        resolution=(3840, 2160),
        codec="h264",
        target_fps=30.0,
    )
    results["playback_4k_h264_fps"] = benchmark_4k.run_benchmark()

    # 4K ProRes
    benchmark_prores = PlaybackFPSBenchmark(
        resolution=(3840, 2160),
        codec="prores",
        target_fps=30.0,
    )
    results["playback_4k_prores_fps"] = benchmark_prores.run_benchmark()

    return results


if __name__ == "__main__":
    # 直接运行基准测试
    results = run_playback_benchmarks()

    print("\n" + "=" * 60)
    print("播放帧率验收测试结果")
    print("=" * 60)

    for metric_id, result in results.items():
        metric = METRICS.get_metric(metric_id)
        print(f"\n{metric.name if metric else metric_id}:")
        print(f"  测量值: {result.measured_value:.1f}fps")
        print(f"  最低目标: {metric.minimum_target if metric else 'N/A'}fps")
        print(f"  专业目标: {metric.professional_target if metric else 'N/A'}fps")
        print(f"  验收级别: {result.level.value}")
        print(f"  通过: {'✓' if result.passed else '✗'}")