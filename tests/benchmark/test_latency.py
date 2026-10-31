"""
Latency and Fallback Acceptance Tests
延迟和 Fallback 验收测试。

测试验收指标：
- playback_latency_growth_rate
- seek_latency_p95_ms
- fallback_v2_count
- fallback_rate

测试方法：
1. 模拟播放期间的延迟变化
2. 测试 seek 操作延迟
3. 模拟 fallback v2 触发场景
4. 收集延迟趋势和 fallback 计数
"""

from __future__ import annotations

import pytest
import time
import numpy as np
from typing import Dict, Any, List, Optional
from dataclasses import dataclass, field
from enum import Enum

from tests.benchmark.benchmark_base import (
    VideoPerformanceBenchmark,
    MockPlaybackEnvironment,
    PerformanceTimeSeries,
)
from tests.benchmark.acceptance_criteria import (
    AcceptanceResult,
    AcceptanceLevel,
    METRICS,
    create_result_from_measurement,
)


class LatencyBenchmark(VideoPerformanceBenchmark):
    """
    延迟基准测试。

    测试播放延迟趋势和 seek 延迟。
    """

    TEST_DURATION_SECONDS: float = 30.0

    def __init__(
        self,
        simulate_latency_growth: bool = False,
        growth_rate_factor: float = 0.0,
    ) -> None:
        """
        初始化延迟测试。

        Args:
            simulate_latency_growth: 是否模拟延迟增长
            growth_rate_factor: 增长因子（ms/s）
        """
        super().__init__()
        self.simulate_latency_growth = simulate_latency_growth
        self.growth_rate_factor = growth_rate_factor
        self._mock_env: MockPlaybackEnvironment = None

    def setup_mock_environment(self) -> Dict[str, Any]:
        """设置 Mock 环境。"""
        self._mock_env = MockPlaybackEnvironment(
            video_resolution=(1920, 1080),
            fps=30.0,
            total_frames=900,
        )
        return {
            'monitor': self._mock_env.monitor,
        }

    def get_metric_id(self) -> str:
        """获取指标 ID。"""
        return "playback_latency_growth_rate"

    def simulate_latency(self, elapsed_seconds: float) -> float:
        """
        模拟播放延迟。

        Args:
            elapsed_seconds: 播放已进行的时间

        Returns:
            当前延迟（毫秒）
        """
        # 基础延迟
        base_latency = 10.0  # 10ms 基础延迟

        # 如果模拟延迟增长
        if self.simulate_latency_growth:
            growth = elapsed_seconds * self.growth_rate_factor
            base_latency += growth

        # 随机波动
        noise = np.random.normal(0, 2.0)
        base_latency += noise

        return max(5.0, base_latency)

    def run_benchmark(self) -> AcceptanceResult:
        """
        运行延迟趋势测试。

        Returns:
            验收结果
        """
        test_start = time.monotonic()

        env = self.setup_mock_environment()
        latency_series = self.start_recording('latency')

        with self.benchmark_context():
            num_samples = int(self.TEST_DURATION_SECONDS * 30)

            for i in range(num_samples):
                elapsed = i / 30.0  # 已播放秒数
                latency = self.simulate_latency(elapsed)

                self.record_sample('latency', latency)

                if self._mock_env:
                    # 记录到监控器（seek 时间模拟）
                    self._mock_env.monitor.track_seek_time(i, latency)

                time.sleep(0.001)

        latency_series = self.stop_recording('latency')

        # 计算增长速率
        growth_rate = latency_series.calculate_growth_rate()

        test_end = time.monotonic()

        latency_stats = latency_series.calculate_stats()

        details = {
            'latency_avg_ms': latency_stats['avg'],
            'latency_min_ms': latency_stats['min'],
            'latency_max_ms': latency_stats['max'],
            'latency_p95_ms': latency_stats['p95'],
            'growth_rate_ms_per_s': growth_rate,
            'simulate_growth': self.simulate_latency_growth,
            'growth_factor': self.growth_rate_factor,
            'test_duration_s': self.TEST_DURATION_SECONDS,
        }

        message = (
            f"播放延迟趋势测试 "
            f"(增长模拟={self.simulate_latency_growth})，"
            f"增长速率: {growth_rate:.2f}ms/s"
        )

        # 增长速率是反向指标（越低越好）
        return create_result_from_measurement(
            metric_id="playback_latency_growth_rate",
            measured_value=growth_rate,
            details=details,
            message=message,
            test_duration_ms=(test_end - test_start) * 1000,
            inverse=True,
        )


class SeekLatencyBenchmark(VideoPerformanceBenchmark):
    """
    Seek 延迟基准测试。
    """

    TEST_DURATION_SECONDS: float = 10.0
    # P95 needs enough samples to be a stable percentile estimate. With only
    # ~20 samples, P95 collapses to ~the 2nd-largest value (near the max), which
    # is high-variance and straddles the 100ms target ~25% of runs. 200 samples
    # yields a stable estimate of the mixture's true 95th percentile (~80ms).
    NUM_SEEK_OPERATIONS: int = 200
    # Reproducible isolated generator — any fixed seed passes because the true
    # P95 (~80ms) is below target; the seed only removes run-to-run noise.
    SEEK_RNG_SEED: int = 42

    def __init__(self) -> None:
        """初始化 Seek 测试。"""
        super().__init__()
        self._mock_env: MockPlaybackEnvironment = None

    def setup_mock_environment(self) -> Dict[str, Any]:
        """设置 Mock 环境。"""
        self._mock_env = MockPlaybackEnvironment(
            video_resolution=(1920, 1080),
            fps=30.0,
            total_frames=300,
        )
        return {
            'monitor': self._mock_env.monitor,
        }

    def get_metric_id(self) -> str:
        """获取指标 ID。"""
        return "seek_latency_p95_ms"

    def simulate_seek(self, rng: np.random.Generator) -> float:
        """
        模拟 Seek 操作延迟（合成统计模型，非真实解码测量）。

        Args:
            rng: 已播种的 numpy Generator，保证可复现且不污染全局 RNG。

        Returns:
            Seek 延迟（毫秒）

        Note:
            真实 seek 路径的性能由 tests/media/test_video_frame_provider_service.py、
            test_video_session_master_frame.py、test_ffmpeg_seek*.py 覆盖。本基准
            仅用于验收阈值烟雾测试，故需足够样本量使 P95 稳定。
        """
        # Seek 延迟通常比正常播放高
        base_seek_ms = rng.normal(30.0, 10.0)  # 平均 30ms

        # 偶尔的慢 seek
        if rng.random() < 0.1:
            slow_seek_ms = rng.normal(80.0, 20.0)
            return slow_seek_ms

        return max(10.0, base_seek_ms)

    def run_benchmark(self) -> AcceptanceResult:
        """运行 Seek 延迟测试。"""
        test_start = time.monotonic()

        env = self.setup_mock_environment()
        seek_series = self.start_recording('seek_latency')

        rng = np.random.default_rng(self.SEEK_RNG_SEED)
        with self.benchmark_context():
            for i in range(self.NUM_SEEK_OPERATIONS):
                seek_ms = self.simulate_seek(rng)

                self.record_sample('seek_latency', seek_ms)

                if self._mock_env:
                    self._mock_env.monitor.track_seek_time(i, seek_ms)

                # Pacing only — does not affect the simulated latency stats;
                # kept small so the larger sample count stays fast.
                time.sleep(0.005)

        seek_series = self.stop_recording('seek_latency')
        seek_stats = seek_series.calculate_stats()

        test_end = time.monotonic()

        details = {
            'seek_avg_ms': seek_stats['avg'],
            'seek_min_ms': seek_stats['min'],
            'seek_max_ms': seek_stats['max'],
            'seek_p95_ms': seek_stats['p95'],
            'seek_operations_count': self.NUM_SEEK_OPERATIONS,
        }

        message = f"Seek 延迟测试，P95: {seek_stats['p95']:.1f}ms"

        return create_result_from_measurement(
            metric_id="seek_latency_p95_ms",
            measured_value=seek_stats['p95'],
            details=details,
            message=message,
            test_duration_ms=(test_end - test_start) * 1000,
            inverse=True,
        )


class FallbackBenchmark(VideoPerformanceBenchmark):
    """
    Fallback 基准测试。

    测试 v3 -> v2 fallback 发生情况。
    """

    TEST_DURATION_SECONDS: float = 30.0
    NUM_PLAYBACK_SESSIONS: int = 10

    # Fallback 原因类型
    FALLBACK_REASONS = [
        "hw_accel_failure",
        "codec_unsupported",
        "memory_pressure",
        "timeout",
        "decoder_error",
        "user_forced_v2",
    ]

    def __init__(
        self,
        simulate_fallbacks: bool = False,
        fallback_probability: float = 0.05,
    ) -> None:
        """
        初始化 Fallback 测试。

        Args:
            simulate_fallbacks: 是否模拟 fallback
            fallback_probability: fallback 发生概率
        """
        super().__init__()
        self.simulate_fallbacks = simulate_fallbacks
        self.fallback_probability = fallback_probability
        self._fallback_count: int = 0
        self._fallback_events: List[str] = []
        self._session_index: int = 0

    def setup_mock_environment(self) -> Dict[str, Any]:
        """设置 Mock 环境。"""
        return {}

    def get_metric_id(self) -> str:
        """获取指标 ID。"""
        return "fallback_v2_count"

    def simulate_playback_session(self) -> Optional[str]:
        """
        模拟播放会话。

        Returns:
            如果发生 fallback，返回原因；否则返回 None
        """
        if self.simulate_fallbacks:
            interval = max(1, int(round(1.0 / max(self.fallback_probability, 0.001))))
            session_index = self._session_index
            self._session_index += 1
            if session_index % interval == 0:
                return self.FALLBACK_REASONS[session_index % len(self.FALLBACK_REASONS)]
        return None

    def run_benchmark(self) -> AcceptanceResult:
        """运行 Fallback 计数测试。"""
        test_start = time.monotonic()

        self._fallback_count = 0
        self._fallback_events = []
        self._session_index = 0

        with self.benchmark_context():
            for i in range(self.NUM_PLAYBACK_SESSIONS):
                fallback_reason = self.simulate_playback_session()

                if fallback_reason:
                    self._fallback_count += 1
                    self._fallback_events.append(fallback_reason)

                time.sleep(0.1)

        test_end = time.monotonic()

        # 计算 fallback 率
        fallback_rate = self._fallback_count / self.NUM_PLAYBACK_SESSIONS

        details = {
            'fallback_count': self._fallback_count,
            'total_sessions': self.NUM_PLAYBACK_SESSIONS,
            'fallback_rate': fallback_rate,
            'simulate_fallbacks': self.simulate_fallbacks,
            'fallback_probability': self.fallback_probability,
            'fallback_events': self._fallback_events,
        }

        message = (
            f"Fallback 测试 "
            f"(模拟={self.simulate_fallbacks})，"
            f"计数: {self._fallback_count}，"
            f"率: {fallback_rate:.1%}"
        )

        # Fallback 计数是反向指标
        return create_result_from_measurement(
            metric_id="fallback_v2_count",
            measured_value=self._fallback_count,
            details=details,
            message=message,
            test_duration_ms=(test_end - test_start) * 1000,
            inverse=True,
        )

    def run_rate_benchmark(self) -> AcceptanceResult:
        """运行 Fallback 率测试。"""
        result = self.run_benchmark()

        # 转换为 fallback 率指标
        fallback_rate = result.details.get('fallback_rate', 0.0)

        return create_result_from_measurement(
            metric_id="fallback_rate",
            measured_value=fallback_rate,
            details=result.details,
            message=f"Fallback 率测试，率: {fallback_rate:.1%}",
            test_duration_ms=result.test_duration_ms,
            inverse=True,
        )


# ================================================================
# pytest 测试函数
# ================================================================

@pytest.fixture
def latency_benchmark():
    """延迟基准测试 fixture。"""
    return LatencyBenchmark(simulate_latency_growth=False)


@pytest.fixture
def latency_benchmark_with_growth():
    """有增长模拟的延迟测试 fixture。"""
    return LatencyBenchmark(simulate_latency_growth=True, growth_rate_factor=1.0)


@pytest.fixture
def seek_benchmark():
    """Seek 延迟测试 fixture。"""
    return SeekLatencyBenchmark()


@pytest.fixture
def fallback_benchmark():
    """Fallback 测试 fixture。"""
    return FallbackBenchmark(simulate_fallbacks=False)


@pytest.fixture
def fallback_benchmark_simulated():
    """模拟 Fallback 的测试 fixture。"""
    return FallbackBenchmark(simulate_fallbacks=True, fallback_probability=0.05)


@pytest.mark.benchmark
@pytest.mark.acceptance
class TestLatencyAcceptance:
    """延迟验收测试。"""

    def test_playback_latency_no_growth(self, latency_benchmark) -> None:
        """
        测试播放延迟不增长。

        验收标准：
        - 最低目标: 增长率 <= 1.0 ms/s
        - 专业目标: 增长率 <= 0.0 ms/s（不增长）
        """
        result = latency_benchmark.run_benchmark()

        print(f"\n[验收测试] {result.metric_id}")
        print(f"  增长速率: {result.measured_value:.2f}ms/s")
        print(f"  验收级别: {result.level.value}")
        print(f"  通过: {result.passed}")
        print(f"  消息: {result.message}")

        # 验证最低目标
        assert result.measured_value <= 1.0, \
            f"延迟增长速率超阈值: {result.measured_value:.2f}ms/s > 1.0ms/s"

    def test_playback_latency_growth_detection(self, latency_benchmark_with_growth) -> None:
        """
        测试延迟增长检测能力。

        在故意模拟增长的情况下，应能检测到异常。
        """
        result = latency_benchmark_with_growth.run_benchmark()

        print(f"\n[检测测试] 增长检测")
        print(f"  模拟增长因子: {latency_benchmark_with_growth.growth_rate_factor}ms/s")
        print(f"  测量增长速率: {result.measured_value:.2f}ms/s")

        # 应检测到增长趋势
        assert result.measured_value > 0.5, \
            f"未能检测到明显的增长趋势: {result.measured_value:.2f}ms/s"

    def test_seek_latency_p95(self, seek_benchmark) -> None:
        """
        测试 Seek 延迟 P95。

        验收标准：
        - 最低目标: <= 100ms
        - 专业目标: <= 50ms
        """
        result = seek_benchmark.run_benchmark()

        print(f"\n[验收测试] {result.metric_id}")
        print(f"  P95 延迟: {result.measured_value:.1f}ms")
        print(f"  验收级别: {result.level.value}")
        print(f"  通过: {result.passed}")

        # 验证最低目标
        assert result.measured_value <= 100.0, \
            f"Seek P95 延迟超阈值: {result.measured_value:.1f}ms > 100ms"


@pytest.mark.benchmark
@pytest.mark.acceptance
class TestFallbackAcceptance:
    """Fallback 验收测试。"""

    def test_fallback_count_normal(self, fallback_benchmark) -> None:
        """
        测试正常情况下的 Fallback 计数。

        验收标准：
        - 最低目标: <= 1（只在明确失败时）
        - 专业目标: <= 0（几乎不发生）
        """
        result = fallback_benchmark.run_benchmark()

        print(f"\n[验收测试] {result.metric_id}")
        print(f"  Fallback 计数: {result.measured_value}")
        print(f"  验收级别: {result.level.value}")
        print(f"  通过: {result.passed}")

        # 验证最低目标
        assert result.measured_value <= 1, \
            f"Fallback 计数超阈值: {result.measured_value} > 1"

    def test_fallback_rate_normal(self, fallback_benchmark) -> None:
        """
        测试正常情况下的 Fallback 率。

        验收标准：
        - 最低目标: <= 5%
        - 专业目标: <= 0%
        """
        result = fallback_benchmark.run_rate_benchmark()

        print(f"\n[验收测试] fallback_rate")
        print(f"  Fallback 率: {result.measured_value:.1%}")
        print(f"  验收级别: {result.level.value}")

        # 验证最低目标
        assert result.measured_value <= 0.05, \
            f"Fallback 率超阈值: {result.measured_value:.1%} > 5%"

    def test_fallback_detection(self, fallback_benchmark_simulated) -> None:
        """
        测试 Fallback 检测能力。

        在模拟 fallback 的情况下，应能正确统计。
        """
        result = fallback_benchmark_simulated.run_benchmark()

        print(f"\n[检测测试] Fallback 统计")
        print(f"  模拟概率: {fallback_benchmark_simulated.fallback_probability}")
        print(f"  实际计数: {result.measured_value}")
        print(f"  实际率: {result.details.get('fallback_rate', 0):.1%}")

        # 应检测到 fallback
        assert result.measured_value > 0, \
            f"未能检测到 fallback 事件"


def run_latency_benchmarks() -> Dict[str, AcceptanceResult]:
    """
    运行所有延迟基准测试。

    Returns:
        结果字典
    """
    results: Dict[str, AcceptanceResult] = {}

    # 延迟趋势
    latency = LatencyBenchmark(simulate_latency_growth=False)
    results["playback_latency_growth_rate"] = latency.run_benchmark()

    # Seek 延迟
    seek = SeekLatencyBenchmark()
    results["seek_latency_p95_ms"] = seek.run_benchmark()

    # Fallback
    fallback = FallbackBenchmark(simulate_fallbacks=False)
    results["fallback_v2_count"] = fallback.run_benchmark()
    results["fallback_rate"] = fallback.run_rate_benchmark()

    return results


if __name__ == "__main__":
    results = run_latency_benchmarks()

    print("\n" + "=" * 60)
    print("延迟和 Fallback 验收测试结果")
    print("=" * 60)

    for metric_id, result in results.items():
        metric = METRICS.get_metric(metric_id)
        print(f"\n{metric.name if metric else metric_id}:")
        print(f"  测量值: {result.measured_value}")
        print(f"  最低目标: {metric.minimum_target if metric else 'N/A'}")
        print(f"  专业目标: {metric.professional_target if metric else 'N/A'}")
        print(f"  验收级别: {result.level.value}")
        print(f"  通过: {'✓' if result.passed else '✗'}")
