"""
Video Pipeline Performance Validation

验证视频管线性能目标是否达成。

## 性能目标定义（基于 Phase 里程碑）

P5 目标（4K Proxy 播放）：
- 1080p ProRes 播放：实时 24fps
- 4K ProRes Proxy 播放：实时 24fps
- 4K ProRes Master seek：< 300ms

P8/P11 目标（DNxHR 支持 + Export 优化）：
- 4K DNxHR Proxy 播放：实时 24fps
- Export ProRes 1080p：>= 30fps 导出速度

## 测试方法

1. 从 benchmark_video_pipeline.py 获取基准测试结果
2. 比对目标规格
3. 输出 pass/fail 状态

## 使用方法

    pytest tests/benchmark/performance_validation.py -v
    python tests/benchmark/performance_validation.py --benchmark-results results.json
"""

from __future__ import annotations

import os
import sys
import json
import subprocess
import pytest
import time
import statistics
import logging
from pathlib import Path
from typing import Optional, Dict, List, Any, Union
from dataclasses import dataclass, field
from enum import Enum

# 确保项目根目录在 path 中
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

# 导入测试模块
from src.shared.media.ffmpeg_source import FFmpegMediaSource

logger = logging.getLogger(__name__)


# =============================================================================
# Performance Target Definitions
# =============================================================================

class TargetType(Enum):
    """目标类型"""
    REALTIME = 'realtime'       # 实时播放（>= 视频帧率）
    MAX_MS = 'max_ms'           # 最大时间（毫秒）
    MIN_FPS = 'min_fps'         # 最小帧率
    MAX_DROP_RATE = 'max_drop_rate'  # 最大掉帧率


@dataclass
class PerformanceTarget:
    """性能目标规格"""
    name: str
    description: str
    target_type: TargetType
    value: Union[float, int]
    fixture_id: Optional[str] = None
    phase: str = ''  # 来源 Phase
    category: str = ''  # playback/seek/export
    tolerance_percent: float = 5.0  # 允许误差百分比

    def check(self, actual: Optional[float]) -> Dict[str, Any]:
        """
        检查实际值是否达标。

        Args:
            actual: 实际测量值

        Returns:
            检查结果字典
        """
        result: Dict[str, Any] = {
            'target_name': self.name,
            'target_type': self.target_type.value,
            'target_value': self.value,
            'actual_value': actual,
            'passed': False,
            'margin': None,
            'notes': [],
        }

        if actual is None:
            result['notes'].append('No actual measurement available')
            return result

        tolerance = self.value * self.tolerance_percent / 100.0

        if self.target_type == TargetType.REALTIME:
            # 实时播放：实际 fps >= 目标 fps
            result['passed'] = actual >= (self.value - tolerance)
            result['margin'] = actual - self.value

        elif self.target_type == TargetType.MAX_MS:
            # 最大时间：实际 ms <= 目标 ms
            result['passed'] = actual <= (self.value + tolerance)
            result['margin'] = self.value - actual  # 正值表示达标

        elif self.target_type == TargetType.MIN_FPS:
            # 最小帧率：实际 fps >= 目标 fps
            result['passed'] = actual >= (self.value - tolerance)
            result['margin'] = actual - self.value

        elif self.target_type == TargetType.MAX_DROP_RATE:
            # 最大掉帧率：实际掉帧率 <= 目标掉帧率
            result['passed'] = actual <= (self.value + tolerance)
            result['margin'] = self.value - actual

        return result


# 性能目标定义表
PERFORMANCE_TARGETS: List[PerformanceTarget] = [
    # P5 目标 - 4K Proxy 播放
    PerformanceTarget(
        name='1080p_prores_playback',
        description='1080p ProRes 播放实时性',
        target_type=TargetType.REALTIME,
        value=24.0,
        fixture_id='prores_422_hq_10bit',
        phase='P5',
        category='playback',
    ),
    PerformanceTarget(
        name='4k_prores_proxy_playback',
        description='4K ProRes Proxy 播放实时性',
        target_type=TargetType.REALTIME,
        value=24.0,
        fixture_id='dnxhr_hqx_10bit',  # DNxHR HQX 作为 4K proxy 测试
        phase='P5',
        category='playback',
    ),
    PerformanceTarget(
        name='4k_prores_master_seek',
        description='4K ProRes Master seek 时间',
        target_type=TargetType.MAX_MS,
        value=300.0,  # < 300ms
        fixture_id='dnxhr_hqx_10bit',
        phase='P5',
        category='seek',
    ),

    # P8/P11 目标 - DNxHR + Export
    PerformanceTarget(
        name='4k_dnxhr_proxy_playback',
        description='4K DNxHR Proxy 播放实时性',
        target_type=TargetType.REALTIME,
        value=24.0,
        fixture_id='dnxhr_hqx_10bit',
        phase='P8',
        category='playback',
    ),
    PerformanceTarget(
        name='4k_dnxhr_seek',
        description='4K DNxHR seek 时间',
        target_type=TargetType.MAX_MS,
        value=300.0,
        fixture_id='dnxhr_hqx_10bit',
        phase='P8',
        category='seek',
    ),
    PerformanceTarget(
        name='export_prores_1080p',
        description='ProRes 1080p 导出速度',
        target_type=TargetType.MIN_FPS,
        value=30.0,  # >= 30fps 导出速度
        fixture_id='prores_422_hq_10bit',
        phase='P11',
        category='export',
    ),

    # 通用性能目标
    PerformanceTarget(
        name='h264_1080p_playback',
        description='H.264 1080p 播放实时性',
        target_type=TargetType.REALTIME,
        value=29.97,
        fixture_id='h264_long_gop',
        phase='General',
        category='playback',
    ),
    PerformanceTarget(
        name='hevc_4k_hdr_playback',
        description='HEVC 4K HDR 播放实时性',
        target_type=TargetType.REALTIME,
        value=50.0,
        fixture_id='h265_10bit_pq',
        phase='General',
        category='playback',
        tolerance_percent=10.0,  # HDR 允许更大误差
    ),
    PerformanceTarget(
        name='h264_seek_accuracy',
        description='H.264 GOP seek 精度',
        target_type=TargetType.MAX_DROP_RATE,
        value=0.05,  # <= 5% 掉帧率（seek 误差）
        fixture_id='h264_long_gop',
        phase='General',
        category='seek',
    ),

    # 打开时间目标
    PerformanceTarget(
        name='video_open_time',
        description='视频打开时间',
        target_type=TargetType.MAX_MS,
        value=500.0,  # < 500ms
        fixture_id=None,  # 适用所有素材
        phase='General',
        category='open',
    ),

    # ================================================================
    # P10.A 验收性能目标
    # ================================================================

    # 1080p H.264 SDR
    PerformanceTarget(
        name='p10a_h264_1080p_open',
        description='P10.A: 1080p H.264 打开时间',
        target_type=TargetType.MAX_MS,
        value=300.0,  # < 300ms
        fixture_id='h264_long_gop',
        phase='P10',
        category='open',
    ),
    PerformanceTarget(
        name='p10a_h264_1080p_seek',
        description='P10.A: 1080p H.264 seek 时间',
        target_type=TargetType.MAX_MS,
        value=100.0,  # < 100ms
        fixture_id='h264_long_gop',
        phase='P10',
        category='seek',
    ),
    PerformanceTarget(
        name='p10a_h264_1080p_decode',
        description='P10.A: 1080p H.264 解码帧率',
        target_type=TargetType.MIN_FPS,
        value=30.0,  # >= 30fps
        fixture_id='h264_long_gop',
        phase='P10',
        category='decode',
    ),

    # 4K H.264
    PerformanceTarget(
        name='p10a_h264_4k_decode',
        description='P10.A: 4K H.264 解码帧率',
        target_type=TargetType.MIN_FPS,
        value=24.0,  # >= 24fps
        fixture_id='h264_4k',
        phase='P10',
        category='decode',
    ),

    # 4K H.265 10-bit
    PerformanceTarget(
        name='p10a_h265_4k_open',
        description='P10.A: 4K H.265 10-bit 打开时间',
        target_type=TargetType.MAX_MS,
        value=500.0,  # < 500ms
        fixture_id='h265_10bit_pq',
        phase='P10',
        category='open',
    ),
    PerformanceTarget(
        name='p10a_h265_4k_seek',
        description='P10.A: 4K H.265 10-bit seek 时间',
        target_type=TargetType.MAX_MS,
        value=300.0,  # < 300ms
        fixture_id='h265_10bit_pq',
        phase='P10',
        category='seek',
    ),
    PerformanceTarget(
        name='p10a_h265_4k_proxy_decode',
        description='P10.A: 4K H.265 Proxy 解码帧率',
        target_type=TargetType.MIN_FPS,
        value=24.0,  # >= 24fps (proxy)
        fixture_id='h265_10bit_pq',
        phase='P10',
        category='proxy_decode',
    ),

    # 4K ProRes 422
    PerformanceTarget(
        name='p10a_prores_422_4k_open',
        description='P10.A: 4K ProRes 422 打开时间',
        target_type=TargetType.MAX_MS,
        value=200.0,  # < 200ms
        fixture_id='prores_422_4k',
        phase='P10',
        category='open',
    ),
    PerformanceTarget(
        name='p10a_prores_422_4k_decode',
        description='P10.A: 4K ProRes 422 解码帧率',
        target_type=TargetType.MIN_FPS,
        value=30.0,  # >= 30fps
        fixture_id='prores_422_4k',
        phase='P10',
        category='decode',
    ),

    # ProRes 4444 alpha
    PerformanceTarget(
        name='p10a_prores_4444_open',
        description='P10.A: ProRes 4444 alpha 打开时间',
        target_type=TargetType.MAX_MS,
        value=200.0,  # < 200ms
        fixture_id='prores_4444_alpha',
        phase='P10',
        category='open',
    ),
    PerformanceTarget(
        name='p10a_prores_4444_decode',
        description='P10.A: ProRes 4444 alpha 解码帧率',
        target_type=TargetType.MIN_FPS,
        value=24.0,  # >= 24fps
        fixture_id='prores_4444_alpha',
        phase='P10',
        category='decode',
    ),

    # DNxHR
    PerformanceTarget(
        name='p10a_dnxhr_4k_open',
        description='P10.A: 4K DNxHR 打开时间',
        target_type=TargetType.MAX_MS,
        value=200.0,  # < 200ms
        fixture_id='dnxhr_hqx_10bit',
        phase='P10',
        category='open',
    ),
    PerformanceTarget(
        name='p10a_dnxhr_4k_decode',
        description='P10.A: 4K DNxHR 解码帧率',
        target_type=TargetType.MIN_FPS,
        value=25.0,  # >= 25fps
        fixture_id='dnxhr_hqx_10bit',
        phase='P10',
        category='decode',
    ),

    # GPU 上传和渲染目标
    PerformanceTarget(
        name='p10a_upload_1080p',
        description='P10.A: 1080p GPU 上传时间',
        target_type=TargetType.MAX_MS,
        value=5.0,  # < 5ms
        fixture_id=None,
        phase='P10',
        category='upload',
    ),
    PerformanceTarget(
        name='p10a_render_1080p',
        description='P10.A: 1080p 渲染时间',
        target_type=TargetType.MAX_MS,
        value=10.0,  # < 10ms
        fixture_id=None,
        phase='P10',
        category='render',
    ),

    # 内存和纹理目标
    PerformanceTarget(
        name='p10a_memory_limit',
        description='P10.A: 内存使用限制',
        target_type=TargetType.MAX_MS,  # 实际是 MB
        value=500.0,  # < 500MB
        fixture_id=None,
        phase='P10',
        category='memory',
    ),
    PerformanceTarget(
        name='p10a_texture_pool_size',
        description='P10.A: GPU 纹理池大小',
        target_type=TargetType.MAX_MS,  # 实际是计数
        value=10.0,  # <= 10 textures
        fixture_id=None,
        phase='P10',
        category='gpu_texture',
    ),
]


# P10.A 验收素材 Fixture ID 列表
P10A_FIXTURE_IDS = [
    'h264_long_gop',       # 1080p H.264 SDR
    'h264_4k',             # 4K H.264（需添加到 manifest）
    'h265_10bit_pq',       # 4K H.265 10-bit
    'prores_422_4k',       # 4K ProRes 422（需添加到 manifest）
    'prores_4444_alpha',   # ProRes 4444 alpha
    'dnxhr_hqx_10bit',     # DNxHR (4K)
]


# =============================================================================
# Performance Validation Framework
# =============================================================================

class PerformanceValidator:
    """性能验证器"""

    def __init__(self, fixtures_path: Optional[str] = None):
        """
        初始化验证器。

        Args:
            fixtures_path: 测试素材路径
        """
        self.fixtures_path = fixtures_path or os.environ.get(
            'TOPOS_VIDEO_FIXTURES_PATH',
            '~/VideoTestFixtures'
        )
        self.manifest: Dict[str, Any] = {}
        self.benchmark_results: Dict[str, Any] = {}
        self._load_manifest()

    def _load_manifest(self) -> None:
        """加载 fixtures_manifest.json"""
        manifest_path = os.path.join(os.path.dirname(__file__), 'fixtures_manifest.json')
        if os.path.exists(manifest_path):
            try:
                with open(manifest_path, 'r', encoding='utf-8') as f:
                    self.manifest = json.load(f)
            except (json.JSONDecodeError, IOError) as e:
                logger.warning(f"Failed to load manifest: {e}")

    def load_benchmark_results(self, json_path: str) -> bool:
        """
        加载 benchmark_video_pipeline.py 的结果。

        Args:
            json_path: JSON 结果文件路径

        Returns:
            是否加载成功
        """
        try:
            with open(json_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            self.benchmark_results = data.get('results', {})
            return True
        except Exception as e:
            logger.error(f"Failed to load benchmark results: {e}")
            return False

    def get_fixture_path(self, fixture_id: str) -> Optional[str]:
        """获取测试素材路径"""
        # 处理用户目录
        base_path = self.fixtures_path
        if base_path.startswith('~'):
            base_path = os.path.expanduser(base_path)

        # 从 manifest 查找
        fixtures = self.manifest.get('fixtures', {})
        fixture = None

        if fixture_id in fixtures:
            fixture = fixtures[fixture_id]
        else:
            # 在嵌套组中查找
            for key, value in fixtures.items():
                if isinstance(value, dict) and 'fixtures' in value:
                    nested = value.get('fixtures', {})
                    if fixture_id in nested:
                        fixture = nested[fixture_id]
                        break

        if fixture:
            relative_path = fixture.get('file_path', '')
            full_path = os.path.join(base_path, relative_path)
            if os.path.exists(full_path):
                return full_path

            # 尝试 fallback 路径
            config = self.manifest.get('config', {})
            fallbacks = config.get('fallback_paths', [])
            for fallback in fallbacks:
                if fallback.startswith('~'):
                    fallback = os.path.expanduser(fallback)
                fallback_path = os.path.join(fallback, relative_path)
                if os.path.exists(fallback_path):
                    return fallback_path

        return None

    def validate_all_targets(self) -> Dict[str, Any]:
        """
        验证所有性能目标。

        Returns:
            验证结果字典
        """
        validation: Dict[str, Any] = {
            'timestamp': time.strftime('%Y-%m-%d %H:%M:%S'),
            'targets': [],
            'summary': {
                'total': len(PERFORMANCE_TARGETS),
                'passed': 0,
                'failed': 0,
                'skipped': 0,
            },
            'by_phase': {},
            'by_category': {},
        }

        for target in PERFORMANCE_TARGETS:
            actual = self._get_actual_value(target)
            result = target.check(actual)
            validation['targets'].append(result)

            # 统计
            if result['passed']:
                validation['summary']['passed'] += 1
            elif actual is None:
                validation['summary']['skipped'] += 1
            else:
                validation['summary']['failed'] += 1

            # 按 Phase 分类
            phase = target.phase
            if phase not in validation['by_phase']:
                validation['by_phase'][phase] = {'passed': 0, 'failed': 0, 'skipped': 0}
            if result['passed']:
                validation['by_phase'][phase]['passed'] += 1
            elif actual is None:
                validation['by_phase'][phase]['skipped'] += 1
            else:
                validation['by_phase'][phase]['failed'] += 1

            # 按类别分类
            category = target.category
            if category not in validation['by_category']:
                validation['by_category'][category] = {'passed': 0, 'failed': 0, 'skipped': 0}
            if result['passed']:
                validation['by_category'][category]['passed'] += 1
            elif actual is None:
                validation['by_category'][category]['skipped'] += 1
            else:
                validation['by_category'][category]['failed'] += 1

        return validation

    def _get_actual_value(self, target: PerformanceTarget) -> Optional[float]:
        """
        获取目标对应的实际测量值。

        Args:
            target: 性能目标

        Returns:
            实际值或 None
        """
        # 从 benchmark 结果获取
        if self.benchmark_results:
            fixture_result = self.benchmark_results.get(target.fixture_id or '', {})
            if fixture_result:
                return self._extract_from_benchmark(target, fixture_result)

        # 运行即时测试
        return self._run_instant_test(target)

    def _extract_from_benchmark(self, target: PerformanceTarget,
                                 result: Dict[str, Any]) -> Optional[float]:
        """从 benchmark 结果提取实际值"""
        if target.category == 'playback':
            playback = result.get('playback', {})
            return playback.get('actual_fps')

        elif target.category == 'seek':
            seek = result.get('seek_time', {})
            return seek.get('median_ms')

        elif target.category == 'open':
            open_time = result.get('open_time', {})
            return open_time.get('median_ms')

        elif target.category == 'export':
            # Export 性能需要单独测试
            return None

        return None

    def _run_instant_test(self, target: PerformanceTarget) -> Optional[float]:
        """运行即时性能测试"""
        if target.fixture_id:
            video_path = self.get_fixture_path(target.fixture_id)
            if not video_path:
                logger.warning(f"Fixture not available: {target.fixture_id}")
                return None

            try:
                if target.category == 'open':
                    return self._measure_open_time(video_path)
                elif target.category == 'playback':
                    return self._measure_playback_fps(video_path, target.value)
                elif target.category == 'seek':
                    return self._measure_seek_time(video_path)
            except Exception as e:
                logger.error(f"Instant test failed: {e}")
                return None

        return None

    def _measure_open_time(self, video_path: str, iterations: int = 3) -> Optional[float]:
        """测量视频打开时间"""
        times: List[float] = []

        for _ in range(iterations):
            start = time.perf_counter()
            source = FFmpegMediaSource(video_path)
            _ = source.read_next_frame()  # 预取第一帧
            end = time.perf_counter()
            times.append((end - start) * 1000)
            source.close()

        return statistics.median(times) if times else None

    def _measure_playback_fps(self, video_path: str,
                                target_fps: float,
                                duration_s: float = 5.0) -> Optional[float]:
        """测量播放帧率"""
        source = FFmpegMediaSource(video_path)
        metadata = source.probe()

        if not metadata or metadata.fps.to_float() <= 0:
            return None

        source.open()

        frames = 0
        start = time.perf_counter()
        max_frames = int(duration_s * metadata.fps.to_float())

        try:
            for _ in range(max_frames):
                frame = source.read_next_frame()
                if frame is not None:
                    frames += 1
                else:
                    break
        except (StopIteration, EOFError):
            pass

        elapsed = time.perf_counter() - start
        source.close()

        return frames / elapsed if elapsed > 0 else None

    def _measure_seek_time(self, video_path: str, iterations: int = 5) -> Optional[float]:
        """测量 seek 时间"""
        source = FFmpegMediaSource(video_path)
        metadata = source.probe()

        if not metadata or metadata.total_frames is None:
            return None

        if metadata.total_frames < 100:
            source.close()
            return None

        source.open()

        times: List[float] = []
        import random
        positions = random.sample(range(20, metadata.total_frames - 20), iterations)

        for pos in positions:
            start = time.perf_counter()
            source.seek_frame(pos)
            _ = source.read_next_frame()
            end = time.perf_counter()
            times.append((end - start) * 1000)

        source.close()

        return statistics.median(times) if times else None

    def print_report(self, validation: Dict[str, Any]) -> None:
        """打印验证报告"""
        print("\n" + "=" * 70)
        print("Video Pipeline Performance Validation Report")
        print("=" * 70)
        print(f"Timestamp: {validation['timestamp']}")

        summary = validation['summary']
        print(f"\nSummary: {summary['passed']}/{summary['total']} passed, "
              f"{summary['failed']} failed, {summary['skipped']} skipped")

        # 按 Phase 分组输出
        print("\n--- By Phase ---")
        for phase, stats in validation['by_phase'].items():
            total = stats['passed'] + stats['failed'] + stats['skipped']
            print(f"  {phase}: {stats['passed']}/{total} passed")

        # 按类别分组输出
        print("\n--- By Category ---")
        for category, stats in validation['by_category'].items():
            total = stats['passed'] + stats['failed'] + stats['skipped']
            print(f"  {category}: {stats['passed']}/{total} passed")

        # 详细结果
        print("\n--- Detailed Results ---")
        for result in validation['targets']:
            status = "PASS" if result['passed'] else ("SKIP" if result['actual_value'] is None else "FAIL")
            actual = result['actual_value']
            target = result['target_value']

            print(f"\n[{status}] {result['target_name']}")
            print(f"  Target: {target} ({result['target_type']})")
            if actual is not None:
                print(f"  Actual: {actual:.2f}")
                margin = result.get('margin')
                if margin is not None:
                    if result['passed']:
                        print(f"  Margin: +{margin:.2f} (达标)")
                    else:
                        print(f"  Margin: {margin:.2f} (未达标)")
            else:
                print(f"  Actual: N/A")

        print("=" * 70)


def validate_performance_targets(benchmark_results: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    验证性能目标达成。

    Args:
        benchmark_results: 可选的 benchmark 结果字典

    Returns:
        验证结果字典
    """
    validator = PerformanceValidator()

    if benchmark_results:
        validator.benchmark_results = benchmark_results

    return validator.validate_all_targets()


# =============================================================================
# Pytest Tests
# =============================================================================

@pytest.fixture
def validator() -> PerformanceValidator:
    """性能验证器 fixture"""
    return PerformanceValidator()


class TestPerformanceTargets:
    """性能目标验证测试"""

    def test_target_definitions_valid(self) -> None:
        """验证目标定义有效"""
        for target in PERFORMANCE_TARGETS:
            assert target.name, "Target must have a name"
            assert target.target_type in TargetType, "Invalid target type"
            assert target.value > 0, "Target value must be positive"
            assert target.phase, "Target must have a phase reference"
            assert target.category, "Target must have a category"

    def test_p5_playback_targets_defined(self) -> None:
        """P5 播放目标已定义"""
        p5_playback = [t for t in PERFORMANCE_TARGETS
                       if t.phase == 'P5' and t.category == 'playback']
        assert len(p5_playback) >= 2, "P5 should have at least 2 playback targets"

    def test_p5_seek_targets_defined(self) -> None:
        """P5 seek 目标已定义"""
        p5_seek = [t for t in PERFORMANCE_TARGETS
                   if t.phase == 'P5' and t.category == 'seek']
        assert len(p5_seek) >= 1, "P5 should have at least 1 seek target"

    def test_p8_targets_defined(self) -> None:
        """P8 目标已定义"""
        p8_targets = [t for t in PERFORMANCE_TARGETS if t.phase == 'P8']
        assert len(p8_targets) >= 2, "P8 should have at least 2 targets"

    def test_p11_export_target_defined(self) -> None:
        """P11 导出目标已定义"""
        p11_export = [t for t in PERFORMANCE_TARGETS
                      if t.phase == 'P11' and t.category == 'export']
        assert len(p11_export) >= 1, "P11 should have at least 1 export target"

    def test_general_targets_defined(self) -> None:
        """通用目标已定义"""
        general_targets = [t for t in PERFORMANCE_TARGETS if t.phase == 'General']
        assert len(general_targets) >= 3, "Should have at least 3 general targets"


class TestTargetCheckLogic:
    """目标检查逻辑测试"""

    def test_realtime_target_pass(self) -> None:
        """实时目标达标检查"""
        target = PerformanceTarget(
            name='test_realtime',
            description='Test',
            target_type=TargetType.REALTIME,
            value=24.0,
        )
        result = target.check(25.0)  # 25 > 24
        assert result['passed'] is True
        assert result['margin'] == 1.0

    def test_realtime_target_fail(self) -> None:
        """实时目标未达标检查"""
        target = PerformanceTarget(
            name='test_realtime',
            description='Test',
            target_type=TargetType.REALTIME,
            value=24.0,
        )
        result = target.check(20.0)  # 20 < 24
        assert result['passed'] is False
        assert result['margin'] == -4.0

    def test_max_ms_target_pass(self) -> None:
        """最大时间目标达标检查"""
        target = PerformanceTarget(
            name='test_seek',
            description='Test',
            target_type=TargetType.MAX_MS,
            value=300.0,
        )
        result = target.check(200.0)  # 200 < 300
        assert result['passed'] is True
        assert result['margin'] == 100.0  # 正值表示达标

    def test_max_ms_target_fail(self) -> None:
        """最大时间目标未达标检查"""
        target = PerformanceTarget(
            name='test_seek',
            description='Test',
            target_type=TargetType.MAX_MS,
            value=300.0,
        )
        result = target.check(500.0)  # 500 > 300
        assert result['passed'] is False
        assert result['margin'] == -200.0

    def test_min_fps_target_pass(self) -> None:
        """最小帧率目标达标检查"""
        target = PerformanceTarget(
            name='test_export',
            description='Test',
            target_type=TargetType.MIN_FPS,
            value=30.0,
        )
        result = target.check(35.0)  # 35 > 30
        assert result['passed'] is True

    def test_tolerance_applied(self) -> None:
        """容忍度应用检查"""
        target = PerformanceTarget(
            name='test_tolerance',
            description='Test',
            target_type=TargetType.REALTIME,
            value=24.0,
            tolerance_percent=10.0,  # 10% tolerance = 2.4 fps
        )
        # 21.6 fps = 24 - 2.4, should still pass with tolerance
        result = target.check(21.6)
        assert result['passed'] is True

        # 21.0 fps = 24 - 3.0, should fail even with tolerance
        result = target.check(21.0)
        assert result['passed'] is False

    def test_null_actual_value(self) -> None:
        """无实际值检查"""
        target = PerformanceTarget(
            name='test_null',
            description='Test',
            target_type=TargetType.REALTIME,
            value=24.0,
        )
        result = target.check(None)
        assert result['passed'] is False
        # 检查 notes 中包含 "No actual measurement" 字样
        assert any('No actual measurement' in note for note in result['notes'])


class TestPerformanceValidationIntegration:
    """性能验证集成测试"""

    @pytest.mark.skipif(
        not os.environ.get('TOPOS_VIDEO_FIXTURES_PATH'),
        reason="Video fixtures path not configured"
    )
    def test_validate_with_real_fixtures(self, validator: PerformanceValidator) -> None:
        """使用真实素材验证"""
        validation = validator.validate_all_targets()
        assert validation is not None
        assert 'targets' in validation
        assert 'summary' in validation

    def test_validate_with_mock_results(self) -> None:
        """使用模拟结果验证"""
        mock_results: Dict[str, Any] = {
            'prores_422_hq_10bit': {
                'playback': {'actual_fps': 25.5},
                'seek_time': {'median_ms': 180.0},
                'open_time': {'median_ms': 320.0},
            },
            'dnxhr_hqx_10bit': {
                'playback': {'actual_fps': 24.2},
                'seek_time': {'median_ms': 150.0},
            },
            'h264_long_gop': {
                'playback': {'actual_fps': 30.5},
                'seek_time': {'median_ms': 350.0},
            },
        }

        validator = PerformanceValidator()
        validator.benchmark_results = mock_results

        validation = validator.validate_all_targets()

        # 检查结果结构
        assert len(validation['targets']) == len(PERFORMANCE_TARGETS)

        # 检查有模拟值的 target
        playback_1080p = next(
            (t for t in validation['targets'] if t['target_name'] == '1080p_prores_playback'),
            None
        )
        if playback_1080p:
            assert playback_1080p['actual_value'] == 25.5
            assert playback_1080p['passed'] is True


# =============================================================================
# CLI Entry Point
# =============================================================================

def run_validation_cli(benchmark_json: Optional[str] = None, output_json: Optional[str] = None) -> None:
    """
    CLI 运行性能验证。

    Args:
        benchmark_json: benchmark_video_pipeline.py 的输出 JSON
        output_json: 输出 JSON 文件路径
    """
    validator = PerformanceValidator()

    if benchmark_json and os.path.exists(benchmark_json):
        validator.load_benchmark_results(benchmark_json)

    validation = validator.validate_all_targets()
    validator.print_report(validation)

    if output_json:
        with open(output_json, 'w', encoding='utf-8') as f:
            json.dump(validation, f, indent=2, ensure_ascii=False)
        print(f"\nResults saved to: {output_json}")


def print_targets_definition() -> None:
    """打印性能目标定义"""
    print("\n" + "=" * 70)
    print("Performance Targets Definition")
    print("=" * 70)

    # 按 Phase 分组
    phases: Dict[str, List[PerformanceTarget]] = {}
    for target in PERFORMANCE_TARGETS:
        if target.phase not in phases:
            phases[target.phase] = []
        phases[target.phase].append(target)

    for phase, targets in phases.items():
        print(f"\n--- {phase} ---")
        for target in targets:
            unit = ''
            if target.target_type == TargetType.REALTIME:
                unit = 'fps'
            elif target.target_type == TargetType.MAX_MS:
                unit = 'ms'
            elif target.target_type == TargetType.MIN_FPS:
                unit = 'fps'
            elif target.target_type == TargetType.MAX_DROP_RATE:
                unit = '%'

            print(f"  {target.name}: {target.value}{unit} ({target.target_type.value})")
            print(f"    Description: {target.description}")
            print(f"    Category: {target.category}")
            if target.fixture_id:
                print(f"    Fixture: {target.fixture_id}")
            print(f"    Tolerance: {target.tolerance_percent}%")

    print("=" * 70)


# =============================================================================
# P2-1: Synthetic Smoke Tests
# =============================================================================

class SyntheticSmokeTests:
    """
    使用合成素材进行基础性能验证（Smoke Test）。

    当外部素材缺失时，使用 FFmpeg 生成的合成素材进行基本性能验证。
    这确保 CI 环境也能获得基本性能数据。
    """

    # Synthetic smoke 性能门槛（比正式门槛宽松）
    SYNTHETIC_THRESHOLDS = {
        'open_ms': 1000.0,  # < 1s（宽松）
        'seek_ms': 500.0,   # < 500ms
        'decode_fps': 15.0,  # >= 15fps（宽松）
    }

    def __init__(self):
        self._fixtures: Dict[str, Path] = {}

    def ensure_fixtures(self) -> bool:
        """
        确保合成素材存在。

        Returns:
            是否成功获取素材
        """
        try:
            from tests.media.synthetic_fixture_generator import ensure_synthetic_fixtures

            # 确保基础测试素材存在
            self._fixtures = ensure_synthetic_fixtures(
                fixture_ids=['synthetic_1080p_h264_short']
            )

            return len(self._fixtures) > 0

        except ImportError:
            logger.warning("synthetic_fixture_generator 不可用")
            return False
        except Exception as e:
            logger.error(f"确保合成素材失败: {e}")
            return False

    def run_open_test(self, fixture_id: str = 'synthetic_1080p_h264_short') -> Optional[float]:
        """
        测试视频打开时间。

        Args:
            fixture_id: 素材 ID

        Returns:
            打开时间（ms）或 None
        """
        if fixture_id not in self._fixtures:
            return None

        video_path = str(self._fixtures[fixture_id])

        try:
            from src.shared.media.ffmpeg_source import FFmpegMediaSource
            import time
            import statistics

            times: List[float] = []

            for _ in range(3):
                start = time.perf_counter()
                source = FFmpegMediaSource(video_path)
                metadata = source.probe()
                if metadata:
                    source.open()
                    source.read_next_frame()  # 预取第一帧
                end = time.perf_counter()
                times.append((end - start) * 1000)
                source.close()

            return statistics.median(times) if times else None

        except Exception as e:
            logger.error(f"打开测试失败: {e}")
            return None

    def run_seek_test(self, fixture_id: str = 'synthetic_1080p_h264_short') -> Optional[float]:
        """
        测试 seek 时间。

        Args:
            fixture_id: 素材 ID

        Returns:
            seek 时间（ms）或 None
        """
        if fixture_id not in self._fixtures:
            return None

        video_path = str(self._fixtures[fixture_id])

        try:
            from src.shared.media.adapter import VideoDecoderAdapter as VideoDecoder  # v2 GStreamer 已移除，改用 PyAV 适配器
            import time
            import statistics
            import random

            decoder = VideoDecoder(video_path)
            total_frames = decoder.total_frames

            if total_frames <= 20:
                decoder.release()
                return None

            times: List[float] = []
            positions = random.sample(range(10, total_frames - 10), 5)

            for pos in positions:
                start = time.perf_counter()
                frame = decoder.get_frame(pos)
                end = time.perf_counter()
                if frame is not None:
                    times.append((end - start) * 1000)

            decoder.release()

            return statistics.median(times) if times else None

        except Exception as e:
            logger.error(f"Seek 测试失败: {e}")
            return None

    def run_decode_fps_test(self, fixture_id: str = 'synthetic_1080p_h264_short',
                              frames: int = 50) -> Optional[float]:
        """
        测试解码帧率。

        Args:
            fixture_id: 素材 ID
            frames: 测试帧数

        Returns:
            解码帧率或 None
        """
        if fixture_id not in self._fixtures:
            return None

        video_path = str(self._fixtures[fixture_id])

        try:
            from src.shared.media.adapter import VideoDecoderAdapter as VideoDecoder  # v2 GStreamer 已移除，改用 PyAV 适配器
            import time

            decoder = VideoDecoder(video_path)

            start = time.perf_counter()
            decoded = 0

            for frame_num in range(min(frames, decoder.total_frames)):
                frame = decoder.get_frame(frame_num)
                if frame is not None:
                    decoded += 1

            elapsed = time.perf_counter() - start
            decoder.release()

            return decoded / elapsed if elapsed > 0 else None

        except Exception as e:
            logger.error(f"解码测试失败: {e}")
            return None

    def run_all_tests(self) -> Dict[str, Any]:
        """
        运行所有 synthetic smoke 测试。

        Returns:
            测试结果字典
        """
        results: Dict[str, Any] = {
            'timestamp': time.strftime('%Y-%m-%d %H:%M:%S'),
            'fixture_type': 'synthetic',
            'tests': {},
            'summary': {
                'total': 3,
                'passed': 0,
                'failed': 0,
                'skipped': 0,
            },
        }

        if not self.ensure_fixtures():
            results['summary']['skipped'] = 3
            results['errors'] = ['无法生成合成素材']
            return results

        fixture_id = 'synthetic_1080p_h264_short'

        # 打开时间测试
        open_ms = self.run_open_test(fixture_id)
        results['tests']['open_ms'] = {
            'actual': open_ms,
            'threshold': self.SYNTHETIC_THRESHOLDS['open_ms'],
            'passed': open_ms is not None and open_ms < self.SYNTHETIC_THRESHOLDS['open_ms'],
        }
        if open_ms is None:
            results['summary']['skipped'] += 1
        elif results['tests']['open_ms']['passed']:
            results['summary']['passed'] += 1
        else:
            results['summary']['failed'] += 1

        # Seek 时间测试
        seek_ms = self.run_seek_test(fixture_id)
        results['tests']['seek_ms'] = {
            'actual': seek_ms,
            'threshold': self.SYNTHETIC_THRESHOLDS['seek_ms'],
            'passed': seek_ms is not None and seek_ms < self.SYNTHETIC_THRESHOLDS['seek_ms'],
        }
        if seek_ms is None:
            results['summary']['skipped'] += 1
        elif results['tests']['seek_ms']['passed']:
            results['summary']['passed'] += 1
        else:
            results['summary']['failed'] += 1

        # 解码帧率测试
        decode_fps = self.run_decode_fps_test(fixture_id)
        results['tests']['decode_fps'] = {
            'actual': decode_fps,
            'threshold': self.SYNTHETIC_THRESHOLDS['decode_fps'],
            'passed': decode_fps is not None and decode_fps >= self.SYNTHETIC_THRESHOLDS['decode_fps'],
        }
        if decode_fps is None:
            results['summary']['skipped'] += 1
        elif results['tests']['decode_fps']['passed']:
            results['summary']['passed'] += 1
        else:
            results['summary']['failed'] += 1

        return results


@pytest.mark.skipif(
    not os.environ.get('TOPOS_RUN_SYNTHETIC_TESTS'),
    reason="Set TOPOS_RUN_SYNTHETIC_TESTS=1 to run synthetic smoke tests"
)
class TestSyntheticSmoke:
    """Synthetic Smoke 测试类（pytest 入口）"""

    def test_synthetic_fixture_generation(self) -> None:
        """测试合成素材生成"""
        smoke = SyntheticSmokeTests()

        # 检查 FFmpeg 可用
        try:
            subprocess.run(['ffmpeg', '-version'], capture_output=True, timeout=5)
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pytest.skip("FFmpeg not available")

        assert smoke.ensure_fixtures(), "应该能生成合成素材"

    def test_synthetic_open_time(self) -> None:
        """测试合成素材打开时间"""
        smoke = SyntheticSmokeTests()

        if not smoke.ensure_fixtures():
            pytest.skip("无法生成合成素材")

        open_ms = smoke.run_open_test()

        assert open_ms is not None, "应该能测量打开时间"
        assert open_ms < 1000.0, f"打开时间应 < 1000ms，实际 {open_ms:.2f}ms"

    def test_synthetic_seek_time(self) -> None:
        """测试合成素材 seek 时间"""
        smoke = SyntheticSmokeTests()

        if not smoke.ensure_fixtures():
            pytest.skip("无法生成合成素材")

        seek_ms = smoke.run_seek_test()

        assert seek_ms is not None, "应该能测量 seek 时间"
        assert seek_ms < 500.0, f"Seek 时间应 < 500ms，实际 {seek_ms:.2f}ms"

    def test_synthetic_decode_fps(self) -> None:
        """测试合成素材解码帧率"""
        smoke = SyntheticSmokeTests()

        if not smoke.ensure_fixtures():
            pytest.skip("无法生成合成素材")

        fps = smoke.run_decode_fps_test()

        assert fps is not None, "应该能测量解码帧率"
        assert fps >= 15.0, f"解码帧率应 >= 15fps，实际 {fps:.2f}fps"


# =============================================================================
# GUI Benchmark Tests (pytest.mark.gui)
# =============================================================================

@pytest.mark.gui
@pytest.mark.skipif(
    not os.environ.get('TOPOS_RUN_GUI_TESTS'),
    reason="Set TOPOS_RUN_GUI_TESTS=1 to run GUI benchmark tests"
)
class TestGUIBenchmark:
    """GUI 基准测试类（pytest 入口）"""

    def test_gui_available(self) -> None:
        """测试 GUI 环境可用"""
        try:
            from tests.media.gui_benchmark import GUIVideoBenchmark
        except ImportError:
            pytest.skip("gui_benchmark 模块不可用")

        # 使用 dry 模式，不需要真实 GL
        benchmark = GUIVideoBenchmark(mode="dry", pipeline_version="v2")

        # 设置 offscreen 环境
        if os.environ.get('QT_QPA_PLATFORM') is None:
            os.environ['QT_QPA_PLATFORM'] = 'offscreen'

        assert benchmark.check_gui_available(), "GUI 环境应该可用"

    def test_gui_synthetic_playback_dry(self) -> None:
        """测试合成素材 dry 模式播放（不需要真实 GL）"""
        try:
            from tests.media.gui_benchmark import GUIVideoBenchmark
            from tests.media.synthetic_fixture_generator import ensure_synthetic_fixtures
        except ImportError:
            pytest.skip("必要模块不可用")

        # 确保 synthetic fixture 存在
        fixtures = ensure_synthetic_fixtures(
            fixture_ids=['synthetic_1080p_h264_short']
        )
        if not fixtures:
            pytest.skip("无法生成合成素材")

        video_path = str(fixtures['synthetic_1080p_h264_short'])

        # 设置 offscreen 环境
        if os.environ.get('QT_QPA_PLATFORM') is None:
            os.environ['QT_QPA_PLATFORM'] = 'offscreen'

        # 使用 dry 模式测试
        benchmark = GUIVideoBenchmark(mode="dry", pipeline_version="v2")

        result = benchmark.run_benchmark(video_path, fixture_id='synthetic_1080p_h264_short', duration_s=3.0)

        assert result is not None, "应该返回测试结果"
        assert result.success, f"测试失败: {result.errors}"
        assert result.mode == "dry"
        assert result.pipeline_version == "v2"

    def test_gui_synthetic_playback_v3(self) -> None:
        """测试合成素材 v3 pipeline 播放"""
        try:
            from tests.media.gui_benchmark import GUIVideoBenchmark
            from tests.media.synthetic_fixture_generator import ensure_synthetic_fixtures
        except ImportError:
            pytest.skip("必要模块不可用")

        # 确保 synthetic fixture 存在
        fixtures = ensure_synthetic_fixtures(
            fixture_ids=['synthetic_1080p_h264_short']
        )
        if not fixtures:
            pytest.skip("无法生成合成素材")

        video_path = str(fixtures['synthetic_1080p_h264_short'])

        # 设置 offscreen 环境
        if os.environ.get('QT_QPA_PLATFORM') is None:
            os.environ['QT_QPA_PLATFORM'] = 'offscreen'

        # 使用 dry 模式 + v3 pipeline 测试
        benchmark = GUIVideoBenchmark(mode="dry", pipeline_version="v3")

        result = benchmark.run_benchmark(video_path, fixture_id='synthetic_1080p_h264_short', duration_s=3.0)

        assert result is not None, "应该返回测试结果"
        assert result.success, f"测试失败: {result.errors}"
        assert result.pipeline_version == "v3"

    @pytest.mark.skipif(
        True,  # 在 headless 环境中 QOpenGLWidget 创建会导致 crash，完全跳过
        reason="Real GUI tests require display environment. QOpenGLWidget crashes in offscreen mode."
    )
    def test_gui_synthetic_playback_real_gui(self) -> None:
        """测试合成素材真实 GUI 播放（需要真实 GL）"""
        try:
            from tests.media.gui_benchmark import GUIVideoBenchmark
            from tests.media.synthetic_fixture_generator import ensure_synthetic_fixtures
        except ImportError:
            pytest.skip("必要模块不可用")

        # 确保 synthetic fixture 存在
        fixtures = ensure_synthetic_fixtures(
            fixture_ids=['synthetic_1080p_h264_short']
        )
        if not fixtures:
            pytest.skip("无法生成合成素材")

        video_path = str(fixtures['synthetic_1080p_h264_short'])

        # 设置 offscreen 环境
        if os.environ.get('QT_QPA_PLATFORM') is None:
            os.environ['QT_QPA_PLATFORM'] = 'offscreen'

        # 创建 benchmark（使用 gui 模式）
        benchmark = GUIVideoBenchmark(mode="gui", pipeline_version="v2")

        if not benchmark.check_gui_available():
            pytest.skip("GUI 环境不可用")

        # 运行 benchmark（如果 GL context 失败，内部会自动切换到 dry 模式）
        try:
            result = benchmark.run_benchmark(video_path, fixture_id='synthetic_1080p_h264_short', duration_s=3.0)
        except Exception as e:
            # 如果 GL context 创建失败，跳过测试而不是崩溃
            pytest.skip(f"GL context 创建失败: {e}")

        assert result is not None, "应该返回测试结果"
        # 由于 headless 环境可能不稳定，只检查基本成功或 dry 模式回退
        assert result.success or result.mode == "dry", f"测试失败: {result.errors}"


# =============================================================================
# CLI Entry Point
# =============================================================================

def run_synthetic_smoke_cli() -> None:
    """运行 synthetic smoke 测试 CLI"""
    smoke = SyntheticSmokeTests()
    results = smoke.run_all_tests()

    print("\n" + "=" * 70)
    print("Synthetic Smoke Tests Results")
    print("=" * 70)
    print(f"Timestamp: {results['timestamp']}")

    summary = results['summary']
    print(f"\nSummary: {summary['passed']}/{summary['total']} passed, "
          f"{summary['failed']} failed, {summary['skipped']} skipped")

    print("\n--- Test Results ---")
    for name, test in results.get('tests', {}).items():
        status = "PASS" if test['passed'] else ("SKIP" if test['actual'] is None else "FAIL")
        actual = test['actual']
        threshold = test['threshold']

        print(f"\n[{status}] {name}")
        if actual is not None:
            print(f"  Actual: {actual:.2f}")
            print(f"  Threshold: {threshold}")
        else:
            print(f"  Actual: N/A")

    print("=" * 70)


if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser(description='Video Pipeline Performance Validation')
    parser.add_argument('--benchmark-results', help='Benchmark JSON results file')
    parser.add_argument('--output', help='Output JSON file path')
    parser.add_argument('--list-targets', action='store_true',
                        help='List all performance targets')
    parser.add_argument('--synthetic-smoke', action='store_true',
                        help='Run synthetic smoke tests')
    parser.add_argument('--gui-benchmark', action='store_true',
                        help='Run GUI benchmark (requires GUI environment)')

    args = parser.parse_args()

    if args.list_targets:
        print_targets_definition()
    elif args.synthetic_smoke:
        run_synthetic_smoke_cli()
    elif args.gui_benchmark:
        try:
            from tests.media.gui_benchmark import main as gui_main
            sys.argv = ['gui_benchmark', '--synthetic']
            gui_main()
        except ImportError:
            print("Error: gui_benchmark module not available")
            sys.exit(1)
    else:
        run_validation_cli(args.benchmark_results, args.output)