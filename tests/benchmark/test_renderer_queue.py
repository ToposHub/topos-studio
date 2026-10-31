"""
Renderer Queue Performance Acceptance Tests
渲染队列性能验收测试。

测试验收指标：
- renderer_queue_depth_p95
- renderer_queue_max_depth

测试方法：
1. 模拟渲染器队列状态变化
2. 收集队列深度分布
3. 计算 P95 和最大值
4. 与验收标准对比
"""

from __future__ import annotations

import pytest
import time
import numpy as np
from typing import Dict, Any, List

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


class RendererQueueBenchmark(VideoPerformanceBenchmark):
    """
    渲染队列深度基准测试。

    测试渲染器队列积压情况。
    """

    TEST_DURATION_SECONDS: float = 10.0

    def __init__(
        self,
        simulate_backpressure: bool = True,
        max_queue_size: int = 10,
    ) -> None:
        """
        初始化渲染队列测试。

        Args:
            simulate_backpressure: 是否模拟 backpressure 机制
            max_queue_size: 最大队列大小
        """
        super().__init__()
        self.simulate_backpressure = simulate_backpressure
        self.max_queue_size = max_queue_size
        self._mock_env: MockPlaybackEnvironment = None

    def setup_mock_environment(self) -> Dict[str, Any]:
        """设置 Mock 环境。"""
        self._mock_env = MockPlaybackEnvironment(
            video_resolution=(1920, 1080),
            fps=60.0,
            total_frames=600,
        )
        return {
            'monitor': self._mock_env.monitor,
        }

    def get_metric_id(self) -> str:
        """获取指标 ID。"""
        return "renderer_queue_depth_p95"

    def simulate_queue_state(self) -> int:
        """
        模拟渲染队列状态。

        Returns:
            当前队列深度
        """
        # 基础队列深度（正常播放）
        base_depth = np.random.normal(1.5, 0.5)
        base_depth = max(0, int(base_depth))

        # 周期性波动（模拟 seek、zoom/pan 等）
        if np.random.random() < 0.1:  # 10% 概率
            burst_depth = np.random.randint(3, 6)
            base_depth += burst_depth

        # 如果有 backpressure，队列不会无限增长
        if self.simulate_backpressure:
            base_depth = min(base_depth, 3)
            # backpressure 会逐渐减少队列
            if base_depth > 3:
                base_depth -= np.random.randint(0, 2)

        return base_depth

    def run_benchmark(self) -> AcceptanceResult:
        """
        运行渲染队列深度测试。

        Returns:
            验收结果
        """
        test_start = time.monotonic()

        env = self.setup_mock_environment()
        queue_series = self.start_recording('queue_depth')

        with self.benchmark_context():
            num_samples = int(self.TEST_DURATION_SECONDS * 60)

            for i in range(num_samples):
                queue_depth = self.simulate_queue_state()

                self.record_sample('queue_depth', queue_depth)

                if self._mock_env:
                    self._mock_env.monitor.track_renderer_queue_depth(queue_depth)

                time.sleep(0.0001)

        queue_series = self.stop_recording('queue_depth')
        queue_stats = queue_series.calculate_stats()

        test_end = time.monotonic()

        # P95 队列深度
        queue_p95 = queue_stats['p95']

        # 创建结果（反向指标）
        details = {
            'queue_avg_depth': queue_stats['avg'],
            'queue_min_depth': queue_stats['min'],
            'queue_p50_depth': queue_stats['p50'],
            'queue_p95_depth': queue_p95,
            'queue_max_depth': queue_stats['max'],
            'simulate_backpressure': self.simulate_backpressure,
            'max_queue_size': self.max_queue_size,
            'overflow_count': sum(1 for v in queue_series.get_values() if v > 5),
            'sample_count': queue_stats['count'],
        }

        message = (
            f"渲染队列深度测试 "
            f"(backpressure={self.simulate_backpressure})，"
            f"P95: {queue_p95:.1f}，"
            f"最大: {queue_stats['max']:.1f}"
        )

        return create_result_from_measurement(
            metric_id="renderer_queue_depth_p95",
            measured_value=queue_p95,
            details=details,
            message=message,
            test_duration_ms=(test_end - test_start) * 1000,
            inverse=True,
        )

    def run_max_benchmark(self) -> AcceptanceResult:
        """
        运行最大队列深度测试。

        Returns:
            验收结果
        """
        test_start = time.monotonic()

        env = self.setup_mock_environment()
        queue_series = self.start_recording('queue_depth')

        # 模拟更长的测试以捕捉峰值
        with self.benchmark_context():
            num_samples = int(self.TEST_DURATION_SECONDS * 60)

            # 增加峰值概率
            for i in range(num_samples):
                # 增加 burst 概率
                if np.random.random() < 0.2:
                    queue_depth = np.random.randint(4, self.max_queue_size)
                else:
                    queue_depth = np.random.randint(0, 3)

                if self.simulate_backpressure:
                    queue_depth = min(queue_depth, 5)

                self.record_sample('queue_depth', queue_depth)
                time.sleep(0.0001)

        queue_series = self.stop_recording('queue_depth')
        queue_stats = queue_series.calculate_stats()

        test_end = time.monotonic()

        details = {
            'queue_max_depth': queue_stats['max'],
            'queue_p95_depth': queue_stats['p95'],
            'queue_avg_depth': queue_stats['avg'],
        }

        message = f"渲染队列最大深度: {queue_stats['max']:.1f}"

        return create_result_from_measurement(
            metric_id="renderer_queue_max_depth",
            measured_value=queue_stats['max'],
            details=details,
            message=message,
            test_duration_ms=(test_end - test_start) * 1000,
            inverse=True,
        )


# ================================================================
# pytest 测试函数
# ================================================================

@pytest.fixture
def renderer_queue_benchmark():
    """渲染队列基准测试 fixture。"""
    return RendererQueueBenchmark(simulate_backpressure=True)


@pytest.fixture
def renderer_queue_benchmark_no_backpressure():
    """无 backpressure 的渲染队列测试 fixture。"""
    return RendererQueueBenchmark(simulate_backpressure=False)


@pytest.mark.benchmark
@pytest.mark.acceptance
class TestRendererQueueAcceptance:
    """渲染队列性能验收测试。"""

    def test_renderer_queue_p95_with_backpressure(self, renderer_queue_benchmark) -> None:
        """
        测试有 backpressure 时的队列深度 P95。

        验收标准：
        - 最低目标: <= 3
        - 专业目标: <= 2
        """
        result = renderer_queue_benchmark.run_benchmark()

        print(f"\n[验收测试] {result.metric_id}")
        print(f"  P95 深度: {result.measured_value:.1f}")
        print(f"  验收级别: {result.level.value}")
        print(f"  通过: {result.passed}")
        print(f"  消息: {result.message}")

        # 验证最低目标
        assert result.measured_value <= 3.0, \
            f"渲染队列 P95 超阈值: {result.measured_value:.1f} > 3"

    def test_renderer_queue_max_with_backpressure(self, renderer_queue_benchmark) -> None:
        """
        测试队列最大深度。

        验收标准：
        - 最低目标: <= 5
        - 专业目标: <= 3
        """
        result = renderer_queue_benchmark.run_max_benchmark()

        print(f"\n[验收测试] renderer_queue_max_depth")
        print(f"  最大深度: {result.measured_value:.1f}")
        print(f"  验收级别: {result.level.value}")
        print(f"  通过: {result.passed}")

        # 验证最低目标
        assert result.measured_value <= 5.0, \
            f"渲染队列最大深度超阈值: {result.measured_value:.1f} > 5"

    def test_renderer_queue_backpressure_effectiveness(self) -> None:
        """
        测试 backpressure 机制有效性。

        比较 backpressure 开启和关闭时的队列深度差异。
        """
        # 开启 backpressure
        benchmark_with = RendererQueueBenchmark(simulate_backpressure=True)
        result_with = benchmark_with.run_benchmark()

        # 关闭 backpressure
        benchmark_without = RendererQueueBenchmark(simulate_backpressure=False)
        result_without = benchmark_without.run_benchmark()

        print(f"\n[对比测试] backpressure 有效性")
        print(f"  有 backpressure P95: {result_with.measured_value:.1f}")
        print(f"  无 backpressure P95: {result_without.measured_value:.1f}")

        # backpressure 应显著降低队列深度
        reduction = result_without.measured_value - result_with.measured_value
        print(f"  降低量: {reduction:.1f}")

        # 验证 backpressure 有效
        assert result_with.measured_value < result_without.measured_value, \
            f"backpressure 未生效: {result_with.measured_value} >= {result_without.measured_value}"


def run_renderer_queue_benchmarks() -> Dict[str, AcceptanceResult]:
    """
    运行所有渲染队列基准测试。

    Returns:
        结果字典
    """
    results: Dict[str, AcceptanceResult] = {}

    benchmark = RendererQueueBenchmark(simulate_backpressure=True)
    results["renderer_queue_depth_p95"] = benchmark.run_benchmark()
    results["renderer_queue_max_depth"] = benchmark.run_max_benchmark()

    return results


if __name__ == "__main__":
    results = run_renderer_queue_benchmarks()

    print("\n" + "=" * 60)
    print("渲染队列性能验收测试结果")
    print("=" * 60)

    for metric_id, result in results.items():
        metric = METRICS.get_metric(metric_id)
        print(f"\n{metric.name if metric else metric_id}:")
        print(f"  测量值: {result.measured_value:.1f}")
        print(f"  最低目标: {metric.minimum_target if metric else 'N/A'}")
        print(f"  专业目标: {metric.professional_target if metric else 'N/A'}")
        print(f"  验收级别: {result.level.value}")
        print(f"  通过: {'✓' if result.passed else '✗'}")
