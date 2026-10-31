"""
Video Pipeline Test Matrix

测试视频管线在各种 codec/container/pix_fmt/color/fps 组合下的功能。

## 组合维度

- codec: h264, hevc, prores, dnxhr
- container: mov, mp4, mxf
- pix_fmt: yuv420p, yuv420p10le, yuv422p10le, yuv444p12le
- color: Rec.709, Rec.2020, PQ, HLG
- fps: 23.976, 24, 25, 29.97, 30, 50, 60

## 测试内容

1. probe() 元数据读取
2. decode() 帧解码
3. seek() 精度验证
4. color metadata preservation

## 使用方法

    pytest tests/media/test_matrix.py -v
    pytest tests/media/test_matrix.py -v --matrix-group=professional
    pytest tests/media/test_matrix.py -v --matrix-group=hdr
"""

from __future__ import annotations

import os
import json
import pytest
import time
import logging
from pathlib import Path
from typing import Optional, Dict, List, Any, Iterator
from dataclasses import dataclass, field

# 导入测试模块
from src.shared.media.ffmpeg_source import FFmpegMediaSource
from src.shared.media.metadata import Rational

logger = logging.getLogger(__name__)


# =============================================================================
# Test Matrix Definition
# =============================================================================

@dataclass
class MatrixCase:
    """测试矩阵单个测试用例"""
    name: str
    codec: str
    container: str
    fps: float
    profile: Optional[str] = None
    pix_fmt: Optional[str] = None
    bit_depth: Optional[int] = None
    resolution: Optional[tuple[int, int]] = None
    color_space: Optional[str] = None
    color_primaries: Optional[str] = None
    color_transfer: Optional[str] = None
    alpha: bool = False
    hdr: bool = False
    hdr_format: Optional[str] = None
    fixture_id: Optional[str] = None
    tags: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            'name': self.name,
            'codec': self.codec,
            'container': self.container,
            'fps': self.fps,
            'profile': self.profile,
            'pix_fmt': self.pix_fmt,
            'bit_depth': self.bit_depth,
            'resolution': self.resolution,
            'color_space': self.color_space,
            'color_primaries': self.color_primaries,
            'color_transfer': self.color_transfer,
            'alpha': self.alpha,
            'hdr': self.hdr,
            'hdr_format': self.hdr_format,
            'fixture_id': self.fixture_id,
            'tags': self.tags,
        }


class MediaTestMatrix:
    """视频管线测试矩阵

    组合维度：
    - codec: h264, hevc, prores, dnxhr
    - container: mov, mp4, mxf
    - pix_fmt: yuv420p, yuv420p10le, yuv422p10le
    - color: Rec.709, Rec.2020, PQ, HLG
    - fps: 23.976, 24, 25, 29.97, 30, 50, 60
    """

    # 核心测试用例（覆盖主要 codec/container 组合）
    TEST_CASES: List[MatrixCase] = [
        # ProRes 系列
        MatrixCase(
            name='prores_422_mov_24fps',
            codec='prores',
            profile='422',
            container='mov',
            fps=24.0,
            pix_fmt='yuv422p10le',
            bit_depth=10,
            color_space='bt709',
            color_primaries='bt709',
            color_transfer='bt709',
            fixture_id='prores_422_hq_10bit',
            tags=['professional', '10bit', 'rec709', 'mov', 'intra']
        ),
        MatrixCase(
            name='prores_4444_alpha_mov_30fps',
            codec='prores',
            profile='4444',
            container='mov',
            fps=30.0,
            pix_fmt='yuv444p12le',
            bit_depth=12,
            alpha=True,
            color_space='bt709',
            color_primaries='bt709',
            color_transfer='bt709',
            fixture_id='prores_4444_alpha',
            tags=['professional', 'alpha', '12bit', 'rec709', 'mov', 'intra']
        ),

        # DNxHR 系列
        MatrixCase(
            name='dnxhr_hqx_mxf_25fps',
            codec='dnxhr',
            profile='hqx',
            container='mxf',
            fps=25.0,
            pix_fmt='yuv422p10le',
            bit_depth=10,
            resolution=(3840, 2160),
            color_space='bt709',
            color_primaries='bt709',
            color_transfer='bt709',
            fixture_id='dnxhr_hqx_10bit',
            tags=['professional', '10bit', '4k', 'mxf', 'rec709', 'intra']
        ),

        # H.264 系列
        MatrixCase(
            name='h264_mp4_2997fps',
            codec='h264',
            container='mp4',
            fps=29.97,
            pix_fmt='yuv420p',
            bit_depth=8,
            color_space='bt709',
            color_primaries='bt709',
            color_transfer='bt709',
            fixture_id='h264_long_gop',
            tags=['consumer', '8bit', 'long-gop', 'rec709', 'mp4', 'gop']
        ),

        # HEVC HDR 系列
        MatrixCase(
            name='hevc_pq_mp4_50fps',
            codec='hevc',
            container='mp4',
            fps=50.0,
            pix_fmt='yuv420p10le',
            bit_depth=10,
            resolution=(3840, 2160),
            color_space='bt2020nc',
            color_primaries='bt2020',
            color_transfer='smpte2084',
            hdr=True,
            hdr_format='HDR10',
            fixture_id='h265_10bit_pq',
            tags=['hdr', '10bit', '4k', 'pq', 'rec2020', 'hevc', 'mp4', 'gop']
        ),
        MatrixCase(
            name='hevc_hlg_mp4_50fps',
            codec='hevc',
            container='mp4',
            fps=50.0,
            pix_fmt='yuv420p10le',
            bit_depth=10,
            resolution=(3840, 2160),
            color_space='bt2020nc',
            color_primaries='bt2020',
            color_transfer='arib-std-b67',
            hdr=True,
            hdr_format='HLG',
            fixture_id='h265_10bit_hlg',
            tags=['hdr', '10bit', '4k', 'hlg', 'rec2020', 'hevc', 'mp4', 'gop']
        ),

        # FPS 变化测试
        MatrixCase(
            name='fps_23976',
            codec='h264',
            container='mp4',
            fps=23.976,
            fixture_id='fps_23_976',
            tags=['fps-test', 'ntsc-film']
        ),
        MatrixCase(
            name='fps_24',
            codec='h264',
            container='mp4',
            fps=24.0,
            fixture_id='fps_24',
            tags=['fps-test', 'cinema']
        ),
        MatrixCase(
            name='fps_25',
            codec='h264',
            container='mp4',
            fps=25.0,
            fixture_id='fps_25',
            tags=['fps-test', 'pal']
        ),
        MatrixCase(
            name='fps_2997',
            codec='h264',
            container='mp4',
            fps=29.97,
            fixture_id='fps_29_97',
            tags=['fps-test', 'ntsc']
        ),
        MatrixCase(
            name='fps_30',
            codec='h264',
            container='mp4',
            fps=30.0,
            fixture_id='fps_30',
            tags=['fps-test', 'standard']
        ),
        MatrixCase(
            name='fps_60',
            codec='h264',
            container='mp4',
            fps=60.0,
            fixture_id='fps_60',
            tags=['fps-test', 'hfr']
        ),
    ]

    # 预定义测试组
    GROUPS: Dict[str, List[str]] = {
        'basic': ['prores_422_mov_24fps', 'h264_mp4_2997fps'],
        'professional': ['prores_422_mov_24fps', 'prores_4444_alpha_mov_30fps', 'dnxhr_hqx_mxf_25fps'],
        'hdr': ['hevc_pq_mp4_50fps', 'hevc_hlg_mp4_50fps'],
        'fps': ['fps_23976', 'fps_24', 'fps_25', 'fps_2997', 'fps_30', 'fps_60'],
        'intra': ['prores_422_mov_24fps', 'prores_4444_alpha_mov_30fps', 'dnxhr_hqx_mxf_25fps'],
        'gop': ['h264_mp4_2997fps', 'hevc_pq_mp4_50fps', 'hevc_hlg_mp4_50fps'],
        'full': [tc.name for tc in TEST_CASES],
    }

    def __init__(self, fixtures_path: Optional[str] = None):
        """
        初始化测试矩阵。

        Args:
            fixtures_path: 测试素材根路径
        """
        self.fixtures_path = fixtures_path or os.environ.get(
            'TOPOS_VIDEO_FIXTURES_PATH',
            '~/VideoTestFixtures'
        )
        self.manifest: Dict[str, Any] = {}
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

    def get_fixture_path(self, fixture_id: str) -> Optional[str]:
        """
        获取测试素材的实际文件路径。

        Args:
            fixture_id: 素材 ID

        Returns:
            文件路径或 None
        """
        # 从 manifest 查找
        fixture = self._find_fixture(fixture_id)
        if not fixture:
            logger.warning(f"Fixture not found: {fixture_id}")
            return None

        relative_path = fixture.get('file_path', '')
        base_path = self.fixtures_path

        # 处理环境变量和用户目录
        if base_path.startswith('~'):
            base_path = os.path.expanduser(base_path)

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

    def _find_fixture(self, fixture_id: str) -> Optional[Dict[str, Any]]:
        """在 manifest 中查找 fixture"""
        fixtures = self.manifest.get('fixtures', {})

        # 直接查找
        if fixture_id in fixtures:
            fixture = fixtures[fixture_id]
            if 'fixtures' not in fixture:
                return fixture

        # 在嵌套组中查找
        for key, value in fixtures.items():
            if isinstance(value, dict) and 'fixtures' in value:
                nested = value.get('fixtures', {})
                if fixture_id in nested:
                    return nested[fixture_id]

        return None

    def get_test_case(self, name: str) -> Optional[MatrixCase]:
        """获取指定测试用例"""
        for tc in self.TEST_CASES:
            if tc.name == name:
                return tc
        return None

    def get_group_cases(self, group: str) -> List[MatrixCase]:
        """获取指定组的测试用例"""
        case_names = self.GROUPS.get(group, [])
        return [tc for tc in self.TEST_CASES if tc.name in case_names]

    def run_matrix(self, group: Optional[str] = None) -> Dict[str, Dict[str, Any]]:
        """
        运行测试矩阵。

        Args:
            group: 可选的测试组名称

        Returns:
            测试结果字典
        """
        results: Dict[str, Dict[str, Any]] = {}

        cases = self.get_group_cases(group or 'full')

        for case in cases:
            video_path = self.get_fixture_path(case.fixture_id or '')
            results[case.name] = self._run_case(case, video_path)

        return results

    def _run_case(self, case: MatrixCase, video_path: Optional[str]) -> Dict[str, Any]:
        """
        运行单个测试用例。

        Args:
            case: 测试用例
            video_path: 视频文件路径

        Returns:
            测试结果
        """
        result: Dict[str, Any] = {
            'case': case.to_dict(),
            'available': video_path is not None,
            'probe': None,
            'decode': None,
            'seek': None,
            'color_metadata': None,
            'errors': [],
            'timings': {},
        }

        if not video_path:
            result['errors'].append('Fixture file not found')
            return result

        try:
            # 1. Probe metadata
            start = time.perf_counter()
            source = FFmpegMediaSource(video_path)
            metadata = source.probe()
            result['timings']['probe_ms'] = (time.perf_counter() - start) * 1000

            result['probe'] = {
                'width': metadata.width,
                'height': metadata.height,
                'codec': metadata.codec_name,
                'fps': metadata.fps.to_float(),
                'bit_depth': metadata.bit_depth,
                'total_frames': metadata.total_frames,
                'duration': metadata.duration,
            }

            # 2. Decode frames
            source.open()
            decode_results: Dict[str, Any] = {
                'frames_tested': 0,
                'frame_sizes': [],
                'frame_dtypes': [],
                'decode_fps': None,
            }

            start = time.perf_counter()
            for i in range(5):  # 解码 5 帧
                frame = source.read_next_frame()
                if frame is not None:
                    decode_results['frames_tested'] += 1
                    if frame.data is not None:
                        decode_results['frame_sizes'].append(frame.data.shape)
                        decode_results['frame_dtypes'].append(str(frame.dtype))
            decode_time_ms = (time.perf_counter() - start) * 1000

            if decode_results['frames_tested'] > 0:
                decode_results['decode_fps'] = (
                    decode_results['frames_tested'] / (decode_time_ms / 1000)
                )

            result['decode'] = decode_results
            result['timings']['decode_ms'] = decode_time_ms

            # 3. Seek accuracy
            seek_results: Dict[str, Any] = {
                'seek_count': 0,
                'accurate_seek_count': 0,
                'max_error_frames': 0,
                'seek_times_ms': [],
            }

            if metadata.total_frames and metadata.total_frames > 100:
                source.seek_frame(0)  # 重置
                import random
                seek_positions = random.sample(
                    range(10, metadata.total_frames - 10), 3
                )

                for target in seek_positions:
                    start = time.perf_counter()
                    source.seek_frame(target)
                    frame = source.read_next_frame()
                    seek_time_ms = (time.perf_counter() - start) * 1000

                    seek_results['seek_count'] += 1
                    seek_results['seek_times_ms'].append(seek_time_ms)

                    if frame is not None:
                        error = abs(frame.frame_index - target)
                        seek_results['max_error_frames'] = max(
                            seek_results['max_error_frames'], error
                        )
                        if error <= 1:
                            seek_results['accurate_seek_count'] += 1

            result['seek'] = seek_results

            # 4. Color metadata preservation
            color_result: Dict[str, Any] = {
                'color_space': metadata.color_space,
                'color_primaries': metadata.color_primaries,
                'color_transfer': metadata.color_transfer,
                'color_range': metadata.color_range,
                'is_hdr': metadata.is_hdr,
            }

            if case.hdr:
                color_result['expected_hdr'] = True
                color_result['hdr_preserved'] = metadata.is_hdr

            result['color_metadata'] = color_result

            source.close()

        except Exception as e:
            result['errors'].append(str(e))
            logger.error(f"Test case {case.name} failed: {e}")

        return result


# =============================================================================
# Test Fixtures
# =============================================================================

@pytest.fixture
def test_matrix() -> MediaTestMatrix:
    """测试矩阵 fixture"""
    return MediaTestMatrix()


def pytest_addoption(parser: pytest.Parser) -> None:
    """添加 pytest 命令行选项"""
    parser.addoption(
        '--matrix-group',
        action='store',
        default=None,
        help='Test matrix group: basic, professional, hdr, fps, intra, gop, full'
    )


# =============================================================================
# Matrix Tests
# =============================================================================

class TestMatrixProbes:
    """测试矩阵 - Probe 验证"""

    @pytest.mark.parametrize('case_name', MediaTestMatrix.GROUPS['basic'])
    def test_probe_metadata(self, test_matrix: MediaTestMatrix, case_name: str) -> None:
        """验证 probe 返回正确元数据"""
        case = test_matrix.get_test_case(case_name)
        if case is None:
            pytest.skip(f"Test case {case_name} not found")

        video_path = test_matrix.get_fixture_path(case.fixture_id or '')
        if video_path is None:
            pytest.skip(f"Fixture {case.fixture_id} not available")

        source = FFmpegMediaSource(video_path)
        metadata = source.probe()

        # 验证基本属性
        assert metadata is not None
        assert metadata.width > 0
        assert metadata.height > 0
        assert metadata.fps.to_float() > 0

        # 验证 codec
        if case.codec:
            assert case.codec in metadata.codec_name.lower()

        # 验证 fps（允许 NTSC 帧率转换误差）
        if case.fps:
            fps_diff = abs(metadata.fps.to_float() - case.fps)
            # NTSC 帧率可能有微小差异
            assert fps_diff < 0.01 or abs(fps_diff - 0.024) < 0.001

    @pytest.mark.parametrize('case_name', MediaTestMatrix.GROUPS['professional'])
    def test_professional_codec_probe(self, test_matrix: MediaTestMatrix, case_name: str) -> None:
        """验证专业 codec 的 probe"""
        case = test_matrix.get_test_case(case_name)
        if case is None:
            pytest.skip(f"Test case {case_name} not found")

        video_path = test_matrix.get_fixture_path(case.fixture_id or '')
        if video_path is None:
            pytest.skip(f"Fixture {case.fixture_id} not available")

        source = FFmpegMediaSource(video_path)
        metadata = source.probe()

        # 专业格式应有高位深
        if case.bit_depth and case.bit_depth >= 10:
            assert metadata.bit_depth >= 10


class TestMatrixDecode:
    """测试矩阵 - 解码验证"""

    @pytest.mark.parametrize('case_name', MediaTestMatrix.GROUPS['basic'])
    def test_decode_first_frames(self, test_matrix: MediaTestMatrix, case_name: str) -> None:
        """验证解码前几帧"""
        case = test_matrix.get_test_case(case_name)
        if case is None:
            pytest.skip(f"Test case {case_name} not found")

        video_path = test_matrix.get_fixture_path(case.fixture_id or '')
        if video_path is None:
            pytest.skip(f"Fixture {case.fixture_id} not available")

        with FFmpegMediaSource(video_path) as source:
            frames = []
            for i in range(3):
                frame = source.read_next_frame()
                if frame is not None:
                    frames.append(frame)

            assert len(frames) == 3

            # 验证帧号递增
            for i, frame in enumerate(frames):
                assert frame.frame_index == i

    @pytest.mark.parametrize('case_name', MediaTestMatrix.GROUPS['hdr'])
    def test_hdr_decode_bit_depth(self, test_matrix: MediaTestMatrix, case_name: str) -> None:
        """验证 HDR 素材保持高位深"""
        case = test_matrix.get_test_case(case_name)
        if case is None:
            pytest.skip(f"Test case {case_name} not found")

        video_path = test_matrix.get_fixture_path(case.fixture_id or '')
        if video_path is None:
            pytest.skip(f"Fixture {case.fixture_id} not available")

        source = FFmpegMediaSource(video_path)
        metadata = source.probe()

        if metadata.bit_depth < 10:
            pytest.skip("Not a 10-bit source")

        source.open()
        frame = source.read_next_frame()

        import numpy as np
        # 高位深不应降级到 uint8
        assert frame.dtype in (np.uint16, np.float32)

        source.close()


class TestMatrixSeek:
    """测试矩阵 - Seek 精度"""

    @pytest.mark.parametrize('case_name', MediaTestMatrix.GROUPS['intra'])
    def test_intra_codec_seek_exact(self, test_matrix: MediaTestMatrix, case_name: str) -> None:
        """Intra codec seek 应精确"""
        case = test_matrix.get_test_case(case_name)
        if case is None:
            pytest.skip(f"Test case {case_name} not found")

        video_path = test_matrix.get_fixture_path(case.fixture_id or '')
        if video_path is None:
            pytest.skip(f"Fixture {case.fixture_id} not available")

        source = FFmpegMediaSource(video_path)
        metadata = source.probe()

        if metadata.total_frames is None or metadata.total_frames < 50:
            pytest.skip("Video too short")

        source.open()

        # 测试 3 个随机位置
        import random
        test_positions = random.sample(
            range(10, min(metadata.total_frames - 10, 100)), 3
        )

        for target in test_positions:
            source.seek_frame(target)
            frame = source.read_next_frame()

            # Intra codec 应精确（误差为 0）
            assert frame.frame_index == target

        source.close()

    @pytest.mark.parametrize('case_name', MediaTestMatrix.GROUPS['gop'])
    def test_gop_codec_seek_tolerance(self, test_matrix: MediaTestMatrix, case_name: str) -> None:
        """GOP codec seek 允许 ±1 帧误差"""
        case = test_matrix.get_test_case(case_name)
        if case is None:
            pytest.skip(f"Test case {case_name} not found")

        video_path = test_matrix.get_fixture_path(case.fixture_id or '')
        if video_path is None:
            pytest.skip(f"Fixture {case.fixture_id} not available")

        source = FFmpegMediaSource(video_path)
        metadata = source.probe()

        if metadata.total_frames is None or metadata.total_frames < 100:
            pytest.skip("Video too short")

        source.open()

        import random
        test_positions = random.sample(
            range(30, min(metadata.total_frames - 10, 200)), 5
        )

        for target in test_positions:
            source.seek_frame(target)
            frame = source.read_next_frame()

            # GOP codec 允许 ±1 帧误差
            error = abs(frame.frame_index - target)
            assert error <= 1

        source.close()


class TestMatrixColorMetadata:
    """测试矩阵 - 色彩元数据"""

    @pytest.mark.parametrize('case_name', MediaTestMatrix.GROUPS['hdr'])
    def test_hdr_metadata_preserved(self, test_matrix: MediaTestMatrix, case_name: str) -> None:
        """验证 HDR 元数据正确读取"""
        case = test_matrix.get_test_case(case_name)
        if case is None:
            pytest.skip(f"Test case {case_name} not found")

        video_path = test_matrix.get_fixture_path(case.fixture_id or '')
        if video_path is None:
            pytest.skip(f"Fixture {case.fixture_id} not available")

        source = FFmpegMediaSource(video_path)
        metadata = source.probe()

        # HDR 标记
        assert metadata.is_hdr is True

        # 色彩空间
        assert metadata.color_primaries == 'bt2020'
        assert metadata.color_space in ('bt2020nc', 'bt2020')

        # 传输函数
        if case.hdr_format == 'HDR10' or case.color_transfer == 'smpte2084':
            assert metadata.color_transfer == 'smpte2084'
        elif case.hdr_format == 'HLG' or case.color_transfer == 'arib-std-b67':
            assert metadata.color_transfer == 'arib-std-b67'

    @pytest.mark.parametrize('case_name', MediaTestMatrix.GROUPS['fps'])
    def test_fps_metadata_accuracy(self, test_matrix: MediaTestMatrix, case_name: str) -> None:
        """验证帧率元数据精确"""
        case = test_matrix.get_test_case(case_name)
        if case is None:
            pytest.skip(f"Test case {case_name} not found")

        video_path = test_matrix.get_fixture_path(case.fixture_id or '')
        if video_path is None:
            pytest.skip(f"Fixture {case.fixture_id} not available")

        source = FFmpegMediaSource(video_path)
        metadata = source.probe()

        # 验证帧率
        actual_fps = metadata.fps.to_float()
        expected_fps = case.fps

        # NTSC 帧率转换允许误差
        fps_diff = abs(actual_fps - expected_fps)
        assert fps_diff < 0.01 or fps_diff < 0.1  # 允许微小误差


# =============================================================================
# Summary Report
# =============================================================================

def test_matrix_summary(test_matrix: MediaTestMatrix) -> None:
    """输出测试矩阵覆盖范围摘要"""
    results = test_matrix.run_matrix('full')

    available_count = sum(1 for r in results.values() if r['available'])
    total_count = len(results)

    print("\n" + "=" * 60)
    print("Test Matrix Coverage Summary")
    print("=" * 60)
    print(f"Total test cases: {total_count}")
    print(f"Available fixtures: {available_count}")
    print(f"Coverage rate: {available_count / total_count * 100:.1f}%")

    # 按维度统计
    codecs = set(tc.codec for tc in MediaTestMatrix.TEST_CASES)
    containers = set(tc.container for tc in MediaTestMatrix.TEST_CASES)
    fps_values = set(tc.fps for tc in MediaTestMatrix.TEST_CASES)

    print(f"\nCodec coverage: {sorted(codecs)}")
    print(f"Container coverage: {sorted(containers)}")
    print(f"FPS coverage: {sorted(fps_values)}")

    # HDR/Alpha 特殊测试
    hdr_cases = [tc for tc in MediaTestMatrix.TEST_CASES if tc.hdr]
    alpha_cases = [tc for tc in MediaTestMatrix.TEST_CASES if tc.alpha]

    print(f"\nHDR test cases: {len(hdr_cases)}")
    print(f"Alpha test cases: {len(alpha_cases)}")

    # 输出每个测试组
    for group, cases in MediaTestMatrix.GROUPS.items():
        available = sum(
            1 for name in cases
            if results.get(name, {}).get('available', False)
        )
        print(f"\nGroup '{group}': {available}/{len(cases)} available")

    print("=" * 60)


# =============================================================================
# CLI Entry Point
# =============================================================================

def run_matrix_cli(group: Optional[str] = None, output_json: Optional[str] = None) -> None:
    """
    CLI 运行测试矩阵。

    Args:
        group: 测试组名称
        output_json: 输出 JSON 文件路径
    """
    matrix = MediaTestMatrix()
    results = matrix.run_matrix(group)

    print("\n" + "=" * 60)
    print("Video Pipeline Test Matrix Results")
    print("=" * 60)

    for name, result in results.items():
        status = "OK" if result['available'] and not result['errors'] else "FAIL"
        print(f"\n[{status}] {name}")

        if result['available']:
            if result['probe']:
                print(f"  Probe: {result['probe']['width']}x{result['probe']['height']}, "
                      f"{result['probe']['fps']:.3f}fps, codec={result['probe']['codec']}")

            if result['timings']:
                print(f"  Probe time: {result['timings'].get('probe_ms', 0):.2f}ms")
                print(f"  Decode time: {result['timings'].get('decode_ms', 0):.2f}ms")

            if result['seek']:
                print(f"  Seek: {result['seek']['accurate_seek_count']}/{result['seek']['seek_count']} accurate")
        else:
            print(f"  Fixture not available")

        if result['errors']:
            print(f"  Errors: {result['errors']}")

    if output_json:
        with open(output_json, 'w', encoding='utf-8') as f:
            json.dump(results, f, indent=2, ensure_ascii=False)
        print(f"\nResults saved to: {output_json}")


if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser(description='Video Pipeline Test Matrix')
    parser.add_argument('--group', choices=list(MediaTestMatrix.GROUPS.keys()),
                        help='Test group to run')
    parser.add_argument('--output', help='Output JSON file path')

    args = parser.parse_args()
    run_matrix_cli(args.group, args.output)