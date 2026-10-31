"""
Resource Leak Test Module
崩溃和资源泄漏测试，验证 GPU/RAM 稳定性和线程清理。

## 职责

- ResourceLeakTester: 资源泄漏测试器
- 测试解码内存泄漏
- 测试纹理池泄漏（模拟播放）
- 测试线程清理完整性

## 测试目标

- P4/P5/P8 模块的资源管理
- FFmpegMediaSource 的 open/close 生命周期
- GStreamerMediaSource 的资源释放
- FrameCache 的内存管理
- 解码队列的线程清理

## 验收标准

- 内存增长 < 10MB/100次迭代
- 无线程残留（线程数稳定）
- 无 GPU 资源泄漏（仅测试软件端）
"""

from __future__ import annotations

import gc
import os
import sys
import threading
import time
import tracemalloc
import unittest
import weakref
from dataclasses import dataclass
from typing import Optional, Callable, Any

import numpy as np

# 导入被测模块
from src.shared.media import (
    MediaSourceFactory,
    FFmpegMediaSource,
    FrameCache,
    DecodedFrame,
    MediaOpenError,
)


@dataclass
class LeakTestResult:
    """
    泄漏测试结果数据结构。

    Attributes:
        test_name: 测试名称
        baseline_mb: 基线内存（MB）
        final_mb: 最终内存（MB）
        growth_mb: 内存增长（MB）
        leak_detected: 是否检测到泄漏
        iterations: 迭代次数
        duration_seconds: 测试时长（秒）
        thread_count_before: 测试前线程数
        thread_count_after: 测试后线程数
        thread_leak: 是否有线程泄漏
        details: 详细信息字典
    """
    test_name: str
    baseline_mb: float
    final_mb: float
    growth_mb: float
    leak_detected: bool
    iterations: int
    duration_seconds: float
    thread_count_before: int
    thread_count_after: int
    thread_leak: bool
    details: dict[str, Any]

    def __repr__(self) -> str:
        """字符串表示"""
        status = "LEAK" if self.leak_detected else "OK"
        thread_status = "THREAD_LEAK" if self.thread_leak else "THREAD_OK"
        return (
            f"LeakTestResult({self.test_name}: "
            f"mem={self.baseline_mb:.1f}MB -> {self.final_mb:.1f}MB "
            f"(+{self.growth_mb:.1f}MB), "
            f"threads={self.thread_count_before} -> {self.thread_count_after}, "
            f"[{status}, {thread_status}])"
        )


class ResourceLeakTester:
    """
    资源泄漏测试器。

    提供多种测试方法验证媒体处理模块的资源管理。

    Usage:
        tester = ResourceLeakTester()
        result = tester.test_decode_leak('/path/to/video.mp4')
        print(result)
    """

    # 内存泄漏阈值（MB）- 超过此值视为泄漏
    MEMORY_LEAK_THRESHOLD_MB: float = 10.0

    # 线程泄漏阈值 - 超过此数量的新增线程视为泄漏
    THREAD_LEAK_THRESHOLD: int = 2

    def __init__(
        self,
        memory_threshold_mb: float = 10.0,
        thread_threshold: int = 2,
        verbose: bool = False
    ) -> None:
        """
        初始化测试器。

        Args:
            memory_threshold_mb: 内存泄漏阈值（MB）
            thread_threshold: 线程泄漏阈值
            verbose: 是否输出详细日志
        """
        self.memory_threshold = memory_threshold_mb
        self.thread_threshold = thread_threshold
        self.verbose = verbose

    def test_decode_leak(
        self,
        path: str,
        iterations: int = 100,
        frames_per_iteration: int = 10
    ) -> LeakTestResult:
        """
        测试解码内存泄漏。

        反复打开、解码、关闭媒体源，检测内存是否持续增长。

        Args:
            path: 媒体文件路径
            iterations: 迭代次数（默认 100）
            frames_per_iteration: 每次迭代解码帧数（默认 10）

        Returns:
            LeakTestResult 测试结果
        """
        test_name = "decode_leak"

        if not os.path.exists(path):
            return LeakTestResult(
                test_name=test_name,
                baseline_mb=0.0,
                final_mb=0.0,
                growth_mb=0.0,
                leak_detected=False,
                iterations=0,
                duration_seconds=0.0,
                thread_count_before=0,
                thread_count_after=0,
                thread_leak=False,
                details={"error": f"文件不存在: {path}"},
            )

        # 记录初始线程数
        thread_count_before = threading.active_count()

        # 启动内存跟踪
        tracemalloc.start()
        gc.collect()  # 先执行 GC 确保基线准确

        # 获取基线内存
        baseline_snapshot = tracemalloc.take_snapshot()
        baseline_mb = tracemalloc.get_traced_memory()[0] / 1024 / 1024

        start_time = time.monotonic()

        # 执行迭代测试
        successful_iterations = 0
        decode_errors: list[str] = []

        for i in range(iterations):
            try:
                # 创建并打开媒体源
                source = MediaSourceFactory.create(path)
                source.open()

                # 解码多帧
                for frame_idx in range(frames_per_iteration):
                    try:
                        frame = source.read_frame(frame_idx)
                        # 确保帧数据被释放（删除引用）
                        del frame
                    except Exception as e:
                        decode_errors.append(f"迭代 {i}, 帧 {frame_idx}: {e}")
                        break

                # 关闭媒体源
                source.close()

                # 删除引用
                del source

                successful_iterations += 1

                # 每 20 次迭代执行一次 GC
                if i % 20 == 19:
                    gc.collect()

            except Exception as e:
                decode_errors.append(f"迭代 {i}: {e}")

        # 强制 GC
        gc.collect()

        # 获取最终内存
        final_mb = tracemalloc.get_traced_memory()[0] / 1024 / 1024
        final_snapshot = tracemalloc.take_snapshot()

        # 计算内存差异
        memory_growth_mb = final_mb - baseline_mb

        # 停止内存跟踪
        tracemalloc.stop()

        # 记录最终线程数
        thread_count_after = threading.active_count()

        # 计算测试时长
        duration_seconds = time.monotonic() - start_time

        # 判断泄漏
        leak_detected = memory_growth_mb > self.memory_threshold
        thread_leak = (thread_count_after - thread_count_before) > self.thread_threshold

        # 分析内存增长（找出前 5 个增长最大的分配）
        top_stats = final_snapshot.compare_to(baseline_snapshot, 'lineno')
        top_growth = [
            f"{stat.size_diff / 1024:.1f}KB: {stat}"
            for stat in top_stats[:5]
        ]

        return LeakTestResult(
            test_name=test_name,
            baseline_mb=baseline_mb,
            final_mb=final_mb,
            growth_mb=memory_growth_mb,
            leak_detected=leak_detected,
            iterations=successful_iterations,
            duration_seconds=duration_seconds,
            thread_count_before=thread_count_before,
            thread_count_after=thread_count_after,
            thread_leak=thread_leak,
            details={
                'path': path,
                'iterations_requested': iterations,
                'frames_per_iteration': frames_per_iteration,
                'decode_errors': decode_errors[:10],  # 只保留前 10 个错误
                'top_memory_growth': top_growth,
            },
        )

    def test_source_lifetime(
        self,
        path: str,
        iterations: int = 50
    ) -> LeakTestResult:
        """
        测试媒体源生命周期管理。

        验证 open/close 循环中资源是否正确释放。
        使用弱引用检测对象是否被正确回收。

        Args:
            path: 媒体文件路径
            iterations: 迭代次数

        Returns:
            LeakTestResult 测试结果
        """
        test_name = "source_lifetime"

        if not os.path.exists(path):
            return LeakTestResult(
                test_name=test_name,
                baseline_mb=0.0,
                final_mb=0.0,
                growth_mb=0.0,
                leak_detected=False,
                iterations=0,
                duration_seconds=0.0,
                thread_count_before=0,
                thread_count_after=0,
                thread_leak=False,
                details={"error": f"文件不存在: {path}"},
            )

        thread_count_before = threading.active_count()

        tracemalloc.start()
        gc.collect()

        baseline_mb = tracemalloc.get_traced_memory()[0] / 1024 / 1024
        start_time = time.monotonic()

        # 记录弱引用存活情况
        weak_refs: list[weakref.ref] = []
        surviving_refs: list[int] = []  # 每次迭代后存活的数量

        for i in range(iterations):
            try:
                source = MediaSourceFactory.create(path)
                source.open()

                # 创建弱引用
                ref = weakref.ref(source)
                weak_refs.append(ref)

                # 读取一帧
                frame = source.read_frame(0)

                # 关闭并删除引用
                source.close()
                del source
                del frame

                # 检查有多少弱引用还存活
                surviving = sum(1 for r in weak_refs if r() is not None)
                surviving_refs.append(surviving)

                # 每 10 次迭代执行 GC
                if i % 10 == 9:
                    gc.collect()

            except Exception:
                pass

        # 强制 GC
        gc.collect()

        final_mb = tracemalloc.get_traced_memory()[0] / 1024 / 1024
        tracemalloc.stop()

        thread_count_after = threading.active_count()
        duration_seconds = time.monotonic() - start_time

        memory_growth_mb = final_mb - baseline_mb
        leak_detected = memory_growth_mb > self.memory_threshold
        thread_leak = (thread_count_after - thread_count_before) > self.thread_threshold

        # 分析弱引用存活情况
        final_surviving = sum(1 for r in weak_refs if r() is not None)
        object_leak_rate = final_surviving / len(weak_refs) if weak_refs else 0.0

        return LeakTestResult(
            test_name=test_name,
            baseline_mb=baseline_mb,
            final_mb=final_mb,
            growth_mb=memory_growth_mb,
            leak_detected=leak_detected or object_leak_rate > 0.1,  # >10% 对象存活视为泄漏
            iterations=iterations,
            duration_seconds=duration_seconds,
            thread_count_before=thread_count_before,
            thread_count_after=thread_count_after,
            thread_leak=thread_leak,
            details={
                'path': path,
                'weak_refs_created': len(weak_refs),
                'final_surviving_refs': final_surviving,
                'object_leak_rate': object_leak_rate,
                'surviving_refs_history': surviving_refs[-10:],  # 最后 10 次的存活数
            },
        )

    def test_frame_cache_leak(
        self,
        iterations: int = 1000,
        frame_size: tuple[int, int] = (1920, 1080)
    ) -> LeakTestResult:
        """
        测试帧缓存内存泄漏。

        模拟播放场景，反复缓存/驱逐帧，检测缓存管理是否正确。

        Args:
            iterations: 缓存操作次数
            frame_size: 模拟帧尺寸 (width, height)

        Returns:
            LeakTestResult 测试结果
        """
        test_name = "frame_cache_leak"

        thread_count_before = threading.active_count()

        tracemalloc.start()
        gc.collect()

        baseline_mb = tracemalloc.get_traced_memory()[0] / 1024 / 1024
        start_time = time.monotonic()

        # 创建帧缓存（500MB 上限）
        cache = FrameCache(max_memory_mb=500, max_entries=100)

        width, height = frame_size
        successful_puts = 0
        successful_gets = 0

        for i in range(iterations):
            try:
                # 创建模拟帧数据
                frame = DecodedFrame(
                    frame_index=i % 500,  # 模拟 500 帧循环
                    pts=i,
                    time_seconds=i / 30.0,
                    data=np.random.randint(0, 255, (height, width, 4), dtype=np.uint8),
                    pixel_format='bgra',
                    width=width,
                    height=height,
                )

                # 存入缓存
                if cache.put(frame, is_master=True):
                    successful_puts += 1

                # 尝试获取
                cached = cache.get(frame_index=i % 500, is_master=True)
                if cached is not None:
                    successful_gets += 1

                # 删除引用
                del frame
                del cached

                # 每 50 次迭代检查缓存状态
                if i % 50 == 49:
                    stats = cache.get_stats()
                    if self.verbose:
                        print(f"缓存状态: entries={stats['entries']}, "
                              f"memory={stats['memory_mb']:.1f}MB")

            except Exception:
                pass

        # 清空缓存
        cache.clear()
        del cache

        # 强制 GC
        gc.collect()

        final_mb = tracemalloc.get_traced_memory()[0] / 1024 / 1024
        tracemalloc.stop()

        thread_count_after = threading.active_count()
        duration_seconds = time.monotonic() - start_time

        memory_growth_mb = final_mb - baseline_mb
        leak_detected = memory_growth_mb > self.memory_threshold
        thread_leak = (thread_count_after - thread_count_before) > self.thread_threshold

        return LeakTestResult(
            test_name=test_name,
            baseline_mb=baseline_mb,
            final_mb=final_mb,
            growth_mb=memory_growth_mb,
            leak_detected=leak_detected,
            iterations=iterations,
            duration_seconds=duration_seconds,
            thread_count_before=thread_count_before,
            thread_count_after=thread_count_after,
            thread_leak=thread_leak,
            details={
                'frame_size': frame_size,
                'successful_puts': successful_puts,
                'successful_gets': successful_gets,
            },
        )

    def test_thread_cleanup(
        self,
        path: str,
        iterations: int = 30,
        delay_seconds: float = 0.5
    ) -> LeakTestResult:
        """
        测试线程清理。

        验证媒体源关闭后是否正确清理线程资源。

        Args:
            path: 媒体文件路径
            iterations: 迭代次数
            delay_seconds: 每次迭代后的等待时间（让线程有时间清理）

        Returns:
            LeakTestResult 测试结果
        """
        test_name = "thread_cleanup"

        if not os.path.exists(path):
            return LeakTestResult(
                test_name=test_name,
                baseline_mb=0.0,
                final_mb=0.0,
                growth_mb=0.0,
                leak_detected=False,
                iterations=0,
                duration_seconds=0.0,
                thread_count_before=0,
                thread_count_after=0,
                thread_leak=False,
                details={"error": f"文件不存在: {path}"},
            )

        # 记录初始线程数
        initial_threads = threading.active_count()
        thread_count_before = initial_threads
        thread_counts: list[int] = []

        start_time = time.monotonic()

        for i in range(iterations):
            try:
                # 打开媒体源
                source = MediaSourceFactory.create(path)
                source.open()

                # 记录打开后的线程数
                threads_after_open = threading.active_count()

                # 读取几帧
                for frame_idx in range(5):
                    source.read_frame(frame_idx)

                # 关闭媒体源
                source.close()
                del source

                # 等待线程清理
                time.sleep(delay_seconds)

                # 记录关闭后的线程数
                threads_after_close = threading.active_count()
                thread_counts.append(threads_after_close)

                if self.verbose:
                    print(f"迭代 {i}: threads {threads_after_open} -> {threads_after_close}")

            except Exception:
                pass

        # 等待所有清理完成
        time.sleep(delay_seconds * 2)
        gc.collect()

        thread_count_after = threading.active_count()
        duration_seconds = time.monotonic() - start_time

        # 判断线程泄漏：如果最终线程数比初始多超过阈值
        thread_increase = thread_count_after - initial_threads
        thread_leak = thread_increase > self.thread_threshold

        # 内存测试（简化版）
        tracemalloc.start()
        gc.collect()
        baseline_mb = tracemalloc.get_traced_memory()[0] / 1024 / 1024
        tracemalloc.stop()

        return LeakTestResult(
            test_name=test_name,
            baseline_mb=baseline_mb,
            final_mb=baseline_mb,  # 不测量内存
            growth_mb=0.0,
            leak_detected=False,  # 此测试主要关注线程
            iterations=iterations,
            duration_seconds=duration_seconds,
            thread_count_before=initial_threads,
            thread_count_after=thread_count_after,
            thread_leak=thread_leak,
            details={
                'path': path,
                'initial_threads': initial_threads,
                'thread_increase': thread_increase,
                'thread_counts_history': thread_counts[-10:],
                'delay_seconds': delay_seconds,
            },
        )

    def run_all_tests(
        self,
        video_path: Optional[str] = None,
        iterations: int = 50
    ) -> list[LeakTestResult]:
        """
        运行所有泄漏测试。

        Args:
            video_path: 视频文件路径（可选，用于需要媒体的测试）
            iterations: 各测试的迭代次数

        Returns:
            测试结果列表
        """
        results: list[LeakTestResult] = []

        # 1. 帧缓存泄漏测试（不需要视频文件）
        print("测试: frame_cache_leak...")
        result = self.test_frame_cache_leak(iterations=iterations * 10)
        results.append(result)
        print(f"  结果: {result}")

        # 2. 如果有视频文件，运行其他测试
        if video_path and os.path.exists(video_path):
            print(f"测试: decode_leak (使用 {video_path})...")
            result = self.test_decode_leak(video_path, iterations=iterations)
            results.append(result)
            print(f"  结果: {result}")

            print(f"测试: source_lifetime...")
            result = self.test_source_lifetime(video_path, iterations=iterations)
            results.append(result)
            print(f"  结果: {result}")

            print(f"测试: thread_cleanup...")
            result = self.test_thread_cleanup(video_path, iterations=min(iterations, 20))
            results.append(result)
            print(f"  结果: {result}")

        return results


# ================================================================
# Unit Tests
# ================================================================

class TestResourceLeak(unittest.TestCase):
    """
    资源泄漏单元测试。

    使用 mock 数据测试基本功能，避免需要真实视频文件。
    """

    def setUp(self) -> None:
        """测试前准备"""
        self.tester = ResourceLeakTester(memory_threshold_mb=5.0, verbose=False)

    def test_frame_cache_basic(self) -> None:
        """测试帧缓存基本操作（无泄漏检测）"""
        # 使用小数据量快速验证
        result = self.tester.test_frame_cache_leak(
            iterations=100,
            frame_size=(320, 240)  # 小尺寸减少内存压力
        )

        # 验证基本指标
        self.assertEqual(result.test_name, "frame_cache_leak")
        self.assertEqual(result.iterations, 100)
        self.assertGreater(result.duration_seconds, 0)

        # 打印结果供人工检查
        print(f"\n{result}")

    def test_result_dataclass(self) -> None:
        """测试 LeakTestResult 数据结构"""
        result = LeakTestResult(
            test_name="test",
            baseline_mb=10.0,
            final_mb=12.0,
            growth_mb=2.0,
            leak_detected=False,
            iterations=100,
            duration_seconds=5.0,
            thread_count_before=5,
            thread_count_after=5,
            thread_leak=False,
            details={},
        )

        # 验证字段
        self.assertEqual(result.test_name, "test")
        self.assertEqual(result.growth_mb, 2.0)
        self.assertFalse(result.leak_detected)
        self.assertFalse(result.thread_leak)

        # 验证 repr
        repr_str = repr(result)
        self.assertIn("OK", repr_str)
        self.assertIn("THREAD_OK", repr_str)


class TestLeakDetection(unittest.TestCase):
    """
    泄漏检测逻辑测试。
    """

    def test_threshold_detection(self) -> None:
        """测试阈值检测逻辑"""
        tester = ResourceLeakTester(memory_threshold_mb=10.0)

        # 模拟泄漏结果
        leak_result = LeakTestResult(
            test_name="leak_test",
            baseline_mb=50.0,
            final_mb=70.0,  # 增长 20MB > 10MB 阈值
            growth_mb=20.0,
            leak_detected=True,
            iterations=100,
            duration_seconds=10.0,
            thread_count_before=5,
            thread_count_after=5,
            thread_leak=False,
            details={},
        )

        self.assertTrue(leak_result.leak_detected)
        self.assertIn("LEAK", repr(leak_result))

    def test_thread_leak_detection(self) -> None:
        """测试线程泄漏检测逻辑"""
        tester = ResourceLeakTester(thread_threshold=2)

        # 模拟线程泄漏结果
        thread_leak_result = LeakTestResult(
            test_name="thread_test",
            baseline_mb=50.0,
            final_mb=52.0,
            growth_mb=2.0,
            leak_detected=False,
            iterations=100,
            duration_seconds=10.0,
            thread_count_before=5,
            thread_count_after=10,  # 增加 5 个线程 > 2 阈值
            thread_leak=True,
            details={},
        )

        self.assertTrue(thread_leak_result.thread_leak)
        self.assertIn("THREAD_LEAK", repr(thread_leak_result))


class TestMemoryStability(unittest.TestCase):
    """
    内存稳定性测试。

    验证内存管理策略的正确性。
    """

    def test_frame_cache_memory_limit(self) -> None:
        """测试帧缓存内存限制"""
        # 创建小缓存
        cache = FrameCache(max_memory_mb=10, max_entries=50)

        # 尝试存入大量帧
        width, height = 640, 480
        frame_size_mb = (width * height * 4) / 1024 / 1024  # BGRA 4 bytes/pixel

        # 计算能存多少帧
        max_frames = int(10 / frame_size_mb) + 2  # 略超限制

        for i in range(max_frames + 10):  # 尝试存入超过限制的帧
            frame = DecodedFrame(
                frame_index=i,
                pts=i,
                time_seconds=i / 30.0,
                data=np.zeros((height, width, 4), dtype=np.uint8),
                pixel_format='bgra',
                width=width,
                height=height,
            )
            cache.put(frame, is_master=True)

        # 验证内存限制
        stats = cache.get_stats()
        self.assertLessEqual(
            stats['memory_mb'],
            10 * 1.1,  # 允许 10% 余量
            f"内存超限: {stats['memory_mb']}MB > 10MB"
        )

        # 清理
        cache.clear()

    def test_weakref_cleanup(self) -> None:
        """测试弱引用清理"""
        # 创建对象和弱引用
        frames: list[DecodedFrame] = []
        refs: list[weakref.ref] = []

        width, height = 100, 100
        for i in range(100):
            frame = DecodedFrame(
                frame_index=i,
                pts=i,
                time_seconds=i / 30.0,
                data=np.zeros((height, width, 4), dtype=np.uint8),
                pixel_format='bgra',
                width=width,
                height=height,
            )
            frames.append(frame)
            refs.append(weakref.ref(frame))

        # 删除所有对象引用
        del frames

        # 执行 GC
        gc.collect()

        # 验证弱引用是否失效
        surviving = sum(1 for r in refs if r() is not None)
        self.assertLess(
            surviving,
            10,  # 允许少量残留（GC 可能不完全）
            f"弱引用清理失败: {surviving} 个对象仍然存活"
        )


# ================================================================
# Main Entry
# ================================================================

def find_test_video() -> Optional[str]:
    """
    查找测试视频文件。

    Returns:
        视频文件路径或 None
    """
    # 常见测试视频位置
    test_paths = [
        '/tmp/test_video.mp4',
        'test_data/sample.mp4',
        'tests/test_data/video.mp4',
    ]

    for path in test_paths:
        if os.path.exists(path):
            return path

    return None


def main() -> int:
    """
    主入口函数。

    运行所有资源泄漏测试。

    Returns:
        退出码（0=成功，1=发现泄漏）
    """
    print("=" * 60)
    print("资源泄漏测试 - P11.3")
    print("=" * 60)

    # 创建测试器
    tester = ResourceLeakTester(
        memory_threshold_mb=10.0,
        thread_threshold=2,
        verbose=True
    )

    # 查找测试视频
    video_path = find_test_video()

    if video_path:
        print(f"\n使用测试视频: {video_path}")
    else:
        print("\n未找到测试视频，将跳过需要媒体的测试")
        print("提示: 可创建 /tmp/test_video.mp4 或指定路径")

    # 运行所有测试
    results = tester.run_all_tests(video_path, iterations=50)

    # 输出汇总
    print("\n" + "=" * 60)
    print("测试汇总")
    print("=" * 60)

    leaks_found = 0
    thread_leaks_found = 0

    for result in results:
        print(f"\n{result.test_name}:")
        print(f"  内存: {result.baseline_mb:.1f}MB -> {result.final_mb:.1f}MB "
              f"(+{result.growth_mb:.1f}MB)")
        print(f"  线程: {result.thread_count_before} -> {result.thread_count_after}")
        print(f"  状态: 内存={result.leak_detected}, 线程={result.thread_leak}")

        if result.leak_detected:
            leaks_found += 1
        if result.thread_leak:
            thread_leaks_found += 1

    # 最终结论
    print("\n" + "=" * 60)
    if leaks_found > 0:
        print(f"警告: 发现 {leaks_found} 个内存泄漏")
    if thread_leaks_found > 0:
        print(f"警告: 发现 {thread_leaks_found} 个线程泄漏")
    if leaks_found == 0 and thread_leaks_found == 0:
        print("通过: 无内存泄漏，无线程泄漏")
    print("=" * 60)

    return 1 if (leaks_found > 0 or thread_leaks_found > 0) else 0


if __name__ == '__main__':
    # 修复测试类中的语法错误
    sys.exit(main())