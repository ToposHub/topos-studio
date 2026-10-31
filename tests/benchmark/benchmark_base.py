"""
Video Performance Benchmark Base Class
视频性能基准测试基类。

提供：
- Mock 视频播放环境
- 性能数据采集基础设施
- 统计计算工具
- 测试配置管理
"""

from __future__ import annotations

import time
import statistics
import threading
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional, Dict, List, Any, Tuple, Generator
from unittest.mock import MagicMock, patch, PropertyMock
from contextlib import contextmanager

import numpy as np

logger = logging.getLogger(__name__)


@dataclass
class PerformanceSample:
    """
    单次性能采样数据。

    Attributes:
        timestamp: 采样时间戳（相对于测试开始）
        value: 采样值
        category: 采样类别（tick, decode, queue, etc.）
        metadata: 附加信息
    """
    timestamp: float
    value: float
    category: str
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class PerformanceTimeSeries:
    """
    性能时间序列数据。

    Attributes:
        samples: 采样列表
        category: 数据类别
        start_time: 开始时间
        end_time: 结束时间
    """
    samples: List[PerformanceSample] = field(default_factory=list)
    category: str = ""
    start_time: float = 0.0
    end_time: float = 0.0

    def add_sample(self, value: float, metadata: Optional[Dict[str, Any]] = None) -> None:
        """添加采样。"""
        timestamp = time.monotonic() - self.start_time
        self.samples.append(PerformanceSample(
            timestamp=timestamp,
            value=value,
            category=self.category,
            metadata=metadata or {},
        ))
        self.end_time = time.monotonic()

    def get_values(self) -> List[float]:
        """获取所有值。"""
        return [s.value for s in self.samples]

    def get_timestamps(self) -> List[float]:
        """获取所有时间戳。"""
        return [s.timestamp for s in self.samples]

    def calculate_stats(self) -> Dict[str, float]:
        """
        计算统计指标。

        Returns:
            包含 avg, min, max, p50, p95, p99, std 的字典
        """
        values = self.get_values()
        if not values:
            return {
                'avg': 0.0,
                'min': 0.0,
                'max': 0.0,
                'p50': 0.0,
                'p95': 0.0,
                'p99': 0.0,
                'std': 0.0,
                'count': 0,
            }

        sorted_values = sorted(values)
        n = len(sorted_values)

        return {
            'avg': statistics.mean(values),
            'min': sorted_values[0],
            'max': sorted_values[-1],
            'p50': sorted_values[int(n * 0.50)],
            'p95': sorted_values[int(n * 0.95)],
            'p99': sorted_values[int(n * 0.99)],
            'std': statistics.stdev(values) if n > 1 else 0.0,
            'count': n,
        }

    def calculate_growth_rate(self) -> float:
        """
        计算增长趋势。

        使用线性回归计算值随时间的增长速率。

        Returns:
            每秒的增长量（ms/s 或 frames/s）
        """
        if len(self.samples) < 10:
            return 0.0

        timestamps = self.get_timestamps()
        values = self.get_values()

        # 简单线性回归
        n = len(timestamps)
        sum_x = sum(timestamps)
        sum_y = sum(values)
        sum_xy = sum(t * v for t, v in zip(timestamps, values))
        sum_x2 = sum(t * t for t in timestamps)

        # 斜率 = (n*sum_xy - sum_x*sum_y) / (n*sum_x2 - sum_x^2)
        denominator = n * sum_x2 - sum_x * sum_x
        if denominator == 0:
            return 0.0

        slope = (n * sum_xy - sum_x * sum_y) / denominator
        return slope  # 每秒的增长量


class VideoPerformanceBenchmark(ABC):
    """
    视频性能基准测试基类。

    提供测试基础设施：
    - Mock 播放环境
    - 性能数据采集
    - 统计计算
    - 结果报告
    """

    # 测试配置
    TEST_DURATION_SECONDS: float = 30.0
    SAMPLE_INTERVAL_MS: float = 16.67  # 约 60fps

    def __init__(self) -> None:
        """初始化基准测试。"""
        self._time_series: Dict[str, PerformanceTimeSeries] = {}
        self._test_start_time: float = 0.0
        self._test_end_time: float = 0.0
        self._is_running: bool = False
        self._lock = threading.Lock()

    @abstractmethod
    def setup_mock_environment(self) -> Dict[str, Any]:
        """
        设置 Mock 测试环境。

        Returns:
            包含 mock 对象的字典
        """
        pass

    @abstractmethod
    def run_benchmark(self) -> None:
        """
        运行基准测试。

        子类实现具体的测试逻辑。
        """
        pass

    @abstractmethod
    def get_metric_id(self) -> str:
        """
        获取当前测试对应的验收指标 ID。

        Returns:
            指标 ID
        """
        pass

    def start_recording(self, category: str) -> PerformanceTimeSeries:
        """
        开始记录性能数据。

        Args:
            category: 数据类别

        Returns:
            PerformanceTimeSeries 实例
        """
        series = PerformanceTimeSeries(category=category)
        series.start_time = time.monotonic()
        with self._lock:
            self._time_series[category] = series
        return series

    def stop_recording(self, category: str) -> PerformanceTimeSeries:
        """
        停止记录性能数据。

        Args:
            category: 数据类别

        Returns:
            PerformanceTimeSeries 实例
        """
        with self._lock:
            series = self._time_series.get(category)
            if series:
                series.end_time = time.monotonic()
        return series

    def record_sample(self, category: str, value: float, metadata: Optional[Dict[str, Any]] = None) -> None:
        """
        记录单次采样。

        Args:
            category: 数据类别
            value: 采样值
            metadata: 附加信息
        """
        with self._lock:
            series = self._time_series.get(category)
            if series:
                series.add_sample(value, metadata)

    def get_time_series(self, category: str) -> Optional[PerformanceTimeSeries]:
        """获取时间序列。"""
        with self._lock:
            return self._time_series.get(category)

    def get_all_time_series(self) -> Dict[str, PerformanceTimeSeries]:
        """获取所有时间序列。"""
        with self._lock:
            return dict(self._time_series)

    def clear_recording(self) -> None:
        """清空所有记录。"""
        with self._lock:
            self._time_series.clear()

    @contextmanager
    def benchmark_context(self) -> Generator[None, None, None]:
        """
        基准测试上下文管理器。

        自动管理测试生命周期。
        """
        self._test_start_time = time.monotonic()
        self._is_running = True
        try:
            yield
        finally:
            self._test_end_time = time.monotonic()
            self._is_running = False

    def get_test_duration_ms(self) -> float:
        """获取测试耗时（毫秒）。"""
        return (self._test_end_time - self._test_start_time) * 1000


class MockPlaybackEnvironment:
    """
    Mock 播放环境。

    用于在没有真实视频播放器的情况下测试性能指标。
    模拟：
    - VideoPerformanceMonitor
    - PlaybackCoordinator
    - AsyncRenderer
    """

    def __init__(
        self,
        video_resolution: Tuple[int, int] = (1920, 1080),
        fps: float = 30.0,
        total_frames: int = 900,  # 30秒 * 30fps
    ) -> None:
        """
        初始化 Mock 环境。

        Args:
            video_resolution: 视频分辨率
            fps: 帧率
            total_frames: 总帧数
        """
        self.resolution = video_resolution
        self.fps = fps
        self.total_frames = total_frames

        # 创建真实的性能监控器（用于测试）
        from src.shared.media.performance_monitor import VideoPerformanceMonitor
        self.monitor = VideoPerformanceMonitor(name='mock_benchmark')
        self.monitor.set_aggregate_log_enabled(False)

        # Mock 解码器
        self.mock_decoder = MagicMock()
        self.mock_decoder.fps = fps
        self.mock_decoder.total_frames = total_frames
        self.mock_decoder.width = video_resolution[0]
        self.mock_decoder.height = video_resolution[1]

        # Mock 渲染器
        self.mock_renderer = MagicMock()
        self.mock_renderer.queue_depth = 0
        self.mock_renderer.current_front_tex_id = 0

        # 模拟帧数据
        self._frame_cache: Dict[int, np.ndarray] = {}

    def generate_mock_frame(self, frame_index: int) -> np.ndarray:
        """
        生成 Mock 帧。

        Args:
            frame_index: 帧号

        Returns:
            Mock 帧数组
        """
        if frame_index not in self._frame_cache:
            # 生成 RGBA 帧
            self._frame_cache[frame_index] = np.random.randint(
                0, 255,
                (self.resolution[1], self.resolution[0], 4),
                dtype=np.uint8
            )
        return self._frame_cache[frame_index]

    def simulate_playback_tick(self) -> Dict[str, float]:
        """
        模拟一次播放 tick。

        Returns:
            本次 tick 的性能数据
        """
        # 模拟解码耗时（随机波动）
        decode_ms = np.random.normal(10.0, 3.0)  # 平均 10ms，标准差 3ms
        decode_ms = max(1.0, decode_ms)  # 至少 1ms

        # 模拟处理耗时
        process_ms = np.random.normal(5.0, 2.0)
        process_ms = max(0.5, process_ms)

        # 模拟渲染提交耗时
        render_ms = np.random.normal(3.0, 1.0)
        render_ms = max(0.5, render_ms)

        # 模拟 UI tick 总耗时
        tick_ms = decode_ms + process_ms + render_ms + np.random.normal(2.0, 1.0)

        # 模拟队列深度（随机波动）
        queue_depth = int(np.random.normal(2, 1))
        queue_depth = max(0, queue_depth)

        # 记录到监控器
        self.monitor.track_tick_time(tick_ms)
        self.monitor.track_decode_time(0, decode_ms)
        self.monitor.track_process_time(0, process_ms)
        self.monitor.track_render_submit_time(0, render_ms)
        self.monitor.track_queue_depth(queue_depth)
        self.monitor.track_renderer_queue_depth(queue_depth + 1)

        return {
            'tick_ms': tick_ms,
            'decode_ms': decode_ms,
            'process_ms': process_ms,
            'render_ms': render_ms,
            'queue_depth': queue_depth,
        }

    def simulate_playback_session(self, duration_seconds: float) -> Dict[str, Any]:
        """
        模拟完整播放会话。

        Args:
            duration_seconds: 模拟时长（秒）

        Returns:
            会话统计数据
        """
        self.monitor.start_session()

        frame_interval = 1.0 / self.fps
        frames_to_play = int(duration_seconds * self.fps)

        for i in range(frames_to_play):
            self.simulate_playback_tick()
            time.sleep(frame_interval * 0.001)  # 微小延迟模拟真实环境

        self.monitor.end_session()
        return self.monitor.get_aggregate_stats()

    def simulate_stress_playback(self, stress_factor: float = 2.0) -> Dict[str, Any]:
        """
        模拟压力播放（解码延迟增大）。

        Args:
            stress_factor: 压力因子（解码时间乘数）

        Returns:
            会话统计数据
        """
        self.monitor.start_session()

        tick_times: list[float] = []

        for i in range(100):  # 播放 100 帧
            # 增大解码延迟
            decode_ms = np.random.normal(20.0 * stress_factor, 5.0 * stress_factor)
            decode_ms = max(10.0, decode_ms)

            tick_ms = decode_ms + np.random.normal(10.0, 5.0)
            tick_ms = max(1.0, tick_ms)
            tick_times.append(tick_ms)

            self.monitor.track_tick_time(tick_ms)
            self.monitor.track_decode_time(i, decode_ms)
            self.monitor.track_queue_depth(int(tick_ms / 10))  # 队列深度与延迟正相关

        self.monitor.end_session()
        stats = self.monitor.get_aggregate_stats()
        stats["tick_times"] = tick_times
        return stats


def calculate_percentile(values: List[float], percentile: float) -> float:
    """
    计算百分位数。

    Args:
        values: 值列表
        percentile: 百分位（0-100）

    Returns:
        百分位值
    """
    if not values:
        return 0.0

    sorted_values = sorted(values)
    index = int(len(sorted_values) * percentile / 100)
    index = min(index, len(sorted_values) - 1)
    return sorted_values[index]


def calculate_fps(frame_times: List[float]) -> float:
    """
    计算有效帧率。

    Args:
        frame_times: 帧间隔列表（毫秒）

    Returns:
        有效帧率
    """
    if not frame_times:
        return 0.0

    avg_frame_time_ms = statistics.mean(frame_times)
    if avg_frame_time_ms <= 0:
        return 0.0

    return 1000.0 / avg_frame_time_ms  # FPS = 1000ms / avg_frame_time_ms


def format_duration_ms(ms: float) -> str:
    """格式化毫秒时长。"""
    if ms < 1000:
        return f"{ms:.2f}ms"
    elif ms < 60000:
        return f"{ms / 1000:.2f}s"
    else:
        return f"{ms / 60000:.2f}m"
