#!/usr/bin/env python3
"""
Synthetic Video Fixture Generator

使用 FFmpeg 生成合成测试视频素材，用于性能基准测试。

## 功能
- 1080p H.264 SDR (5-10 秒)
- 4K H.264 SDR (3-5 秒，可选)
- ProRes 422 HQ 1080p (用于 proxy 测试)

## 使用方法
    python tests/media/synthetic_fixture_generator.py
    python tests/media/synthetic_fixture_generator.py --output-dir /tmp/test_fixtures
    python tests/media/synthetic_fixture_generator.py --fixtures 1080p,4k

## 与 P10.A 的关系
当外部素材缺失时，可以使用 synthetic fixture 进行基础性能验证（smoke test）。
这确保 CI 环境也能获得基本性能数据。
"""

from __future__ import annotations

import os
import sys
import subprocess
import argparse
import logging
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class FixtureSpec:
    """合成素材规格"""
    fixture_id: str
    name: str
    width: int
    height: int
    fps: float
    duration_s: float
    codec: str
    pix_fmt: str
    color_space: str
    color_primaries: str
    color_transfer: str
    extra_args: List[str] = None

    def __post_init__(self):
        if self.extra_args is None:
            self.extra_args = []


# P10.A 要求的合成素材规格
SYNTHETIC_FIXTURES: Dict[str, FixtureSpec] = {
    'synthetic_1080p_h264_sdr': FixtureSpec(
        fixture_id='synthetic_1080p_h264_sdr',
        name='Synthetic 1080p H.264 SDR (10s)',
        width=1920,
        height=1080,
        fps=29.97,
        duration_s=10.0,
        codec='libx264',
        pix_fmt='yuv420p',
        color_space='bt709',
        color_primaries='bt709',
        color_transfer='bt709',
        extra_args=[
            '-preset', 'medium',
            '-crf', '23',
            '-g', '30',  # GOP size
            '-bf', '3',  # B frames
        ],
    ),
    'synthetic_1080p_h264_short': FixtureSpec(
        fixture_id='synthetic_1080p_h264_short',
        name='Synthetic 1080p H.264 SDR (5s)',
        width=1920,
        height=1080,
        fps=29.97,
        duration_s=5.0,
        codec='libx264',
        pix_fmt='yuv420p',
        color_space='bt709',
        color_primaries='bt709',
        color_transfer='bt709',
        extra_args=[
            '-preset', 'medium',
            '-crf', '23',
            '-g', '30',
            '-bf', '3',
        ],
    ),
    'synthetic_4k_h264_sdr': FixtureSpec(
        fixture_id='synthetic_4k_h264_sdr',
        name='Synthetic 4K H.264 SDR (3s)',
        width=3840,
        height=2160,
        fps=30.0,
        duration_s=3.0,
        codec='libx264',
        pix_fmt='yuv420p',
        color_space='bt709',
        color_primaries='bt709',
        color_transfer='bt709',
        extra_args=[
            '-preset', 'medium',
            '-crf', '28',  # 4K 使用较高 CRF
            '-g', '30',
            '-bf', '2',
        ],
    ),
    'synthetic_prores_422_1080p': FixtureSpec(
        fixture_id='synthetic_prores_422_1080p',
        name='Synthetic ProRes 422 HQ 1080p (5s)',
        width=1920,
        height=1080,
        fps=24.0,
        duration_s=5.0,
        codec='prores_ks',
        pix_fmt='yuv422p10le',
        color_space='bt709',
        color_primaries='bt709',
        color_transfer='bt709',
        extra_args=[
            '-profile', '3',  # ProRes 422 HQ
        ],
    ),
    'synthetic_h265_10bit_pq': FixtureSpec(
        fixture_id='synthetic_h265_10bit_pq',
        name='Synthetic 4K H.265 10-bit PQ (3s)',
        width=3840,
        height=2160,
        fps=24.0,
        duration_s=3.0,
        codec='libx265',
        pix_fmt='yuv420p10le',
        color_space='bt2020nc',
        color_primaries='bt2020',
        color_transfer='smpte2084',
        extra_args=[
            '-preset', 'medium',
            '-crf', '28',
            '-x265-params', 'colorprim=bt2020:transfer=smpte2084:colormatrix=bt2020nc:hdr10',
        ],
    ),
}


class SyntheticFixtureGenerator:
    """合成素材生成器"""

    def __init__(self, output_dir: Optional[str] = None):
        """
        初始化生成器。

        Args:
            output_dir: 输出目录，默认为 tests/media/fixtures/synthetic/
        """
        if output_dir:
            self.output_dir = Path(output_dir)
        else:
            # 默认输出到项目测试目录
            project_root = Path(__file__).parent.parent.parent
            self.output_dir = project_root / 'tests' / 'media' / 'fixtures' / 'synthetic'

        self.output_dir.mkdir(parents=True, exist_ok=True)

    def check_ffmpeg(self) -> bool:
        """检查 FFmpeg 是否可用"""
        try:
            result = subprocess.run(
                ['ffmpeg', '-version'],
                capture_output=True,
                text=True,
                timeout=5,
            )
            return result.returncode == 0
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return False

    def generate_fixture(self, spec: FixtureSpec, force: bool = False) -> Optional[Path]:
        """
        生成合成素材。

        Args:
            spec: 素材规格
            force: 是否强制重新生成

        Returns:
            生成的文件路径或 None（失败时）
        """
        output_path = self.output_dir / f"{spec.fixture_id}.mp4"

        # 检查是否已存在
        if output_path.exists() and not force:
            logger.info(f"素材已存在: {output_path}")
            return output_path

        logger.info(f"生成素材: {spec.name}")
        logger.info(f"  输出: {output_path}")

        # 构建 FFmpeg 命令
        cmd = self._build_ffmpeg_command(spec, output_path)

        # 执行命令
        start_time = time.time()
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=120,  # 最长 2 分钟
            )

            if result.returncode != 0:
                logger.error(f"FFmpeg 失败: {result.stderr}")
                return None

            elapsed = time.time() - start_time
            logger.info(f"  生成成功 ({elapsed:.1f}s)")

            # 验证文件
            if self._verify_fixture(output_path, spec):
                return output_path
            else:
                logger.error(f"验证失败: {output_path}")
                output_path.unlink(missing_ok=True)
                return None

        except subprocess.TimeoutExpired:
            logger.error(f"生成超时: {spec.name}")
            return None
        except Exception as e:
            logger.error(f"生成失败: {e}")
            return None

    def _build_ffmpeg_command(self, spec: FixtureSpec, output_path: Path) -> List[str]:
        """
        构建 FFmpeg 命令。

        使用 testsrc2 生成测试图案（包含多种颜色和动态元素）。
        注意：testsrc2 不支持 n 参数，使用 -t 参数限制时长。
        """
        # 基础命令
        cmd = [
            'ffmpeg',
            '-y',  # 覆盖输出
            '-f', 'lavfi',  # 使用 lavfi 输入
            '-i', f'testsrc2=size={spec.width}x{spec.height}:rate={spec.fps}',
            '-f', 'lavfi',
            '-i', f'sine=frequency=1000:duration={spec.duration_s}',  # 添加音频轨道
            '-t', str(spec.duration_s),  # 限制输出时长
            '-c:v', spec.codec,
            '-pix_fmt', spec.pix_fmt,
            '-colorspace', spec.color_space,
            '-color_primaries', spec.color_primaries,
            '-color_trc', spec.color_transfer,
            '-color_range', 'tv',
        ]

        # 添加编码器特定参数
        if spec.extra_args:
            cmd.extend(spec.extra_args)

        # 音频编码
        cmd.extend([
            '-c:a', 'aac',
            '-b:a', '128k',
        ])

        # 输出文件
        cmd.append(str(output_path))

        return cmd

    def _verify_fixture(self, path: Path, spec: FixtureSpec) -> bool:
        """验证生成的素材是否符合规格"""
        try:
            result = subprocess.run(
                ['ffprobe', '-v', 'error', '-show_entries',
                 'stream=width,height,pix_fmt,r_frame_rate,codec_name,'
                 'color_space,color_primaries,color_transfer',
                 '-of', 'json', str(path)],
                capture_output=True,
                text=True,
                timeout=10,
            )

            if result.returncode != 0:
                logger.error(f"ffprobe 失败: {result.stderr}")
                return False

            import json
            data = json.loads(result.stdout)
            streams = data.get('streams', [])

            if not streams:
                logger.error("无视频流")
                return False

            video_stream = streams[0]

            # 检查关键属性
            checks = [
                ('width', spec.width),
                ('height', spec.height),
            ]

            for key, expected in checks:
                actual = int(video_stream.get(key, 0))
                if actual != expected:
                    logger.warning(f"  {key}: 期望 {expected}, 实际 {actual}")

            return True

        except Exception as e:
            logger.error(f"验证失败: {e}")
            return False

    def generate_all(self, fixtures: Optional[List[str]] = None,
                      force: bool = False) -> Dict[str, Optional[Path]]:
        """
        生成所有素材或指定素材。

        Args:
            fixtures: 要生成的素材 ID 列表，None 表示生成全部
            force: 是否强制重新生成

        Returns:
            fixture_id -> 文件路径 的字典
        """
        if fixtures:
            specs = [SYNTHETIC_FIXTURES[f] for f in fixtures if f in SYNTHETIC_FIXTURES]
        else:
            specs = list(SYNTHETIC_FIXTURES.values())

        results = {}
        for spec in specs:
            path = self.generate_fixture(spec, force)
            results[spec.fixture_id] = path

        return results

    def get_fixture_path(self, fixture_id: str) -> Optional[Path]:
        """获取已存在的素材路径"""
        path = self.output_dir / f"{fixture_id}.mp4"
        if path.exists():
            return path
        return None


def ensure_synthetic_fixtures(output_dir: Optional[str] = None,
                                fixture_ids: Optional[List[str]] = None) -> Dict[str, Path]:
    """
    确保合成素材存在，如果不存在则生成。

    Args:
        output_dir: 输出目录
        fixture_ids: 要确保的素材 ID 列表

    Returns:
        fixture_id -> 文件路径 的字典
    """
    generator = SyntheticFixtureGenerator(output_dir)

    # 默认生成基础测试素材
    if fixture_ids is None:
        fixture_ids = ['synthetic_1080p_h264_short', 'synthetic_prores_422_1080p']

    results = {}
    for fid in fixture_ids:
        path = generator.get_fixture_path(fid)
        if path is None and fid in SYNTHETIC_FIXTURES:
            path = generator.generate_fixture(SYNTHETIC_FIXTURES[fid])
        if path:
            results[fid] = path

    return results


def get_available_synthetic_fixtures() -> Dict[str, Path]:
    """获取所有可用的合成素材"""
    generator = SyntheticFixtureGenerator()
    results = {}

    for fid in SYNTHETIC_FIXTURES:
        path = generator.get_fixture_path(fid)
        if path:
            results[fid] = path

    return results


def main():
    """主函数"""
    parser = argparse.ArgumentParser(
        description='Generate synthetic video test fixtures'
    )

    parser.add_argument(
        '--output-dir', '-o',
        type=str,
        help='Output directory for fixtures'
    )

    parser.add_argument(
        '--fixtures', '-f',
        type=str,
        help='Comma-separated list of fixture IDs to generate'
    )

    parser.add_argument(
        '--force',
        action='store_true',
        help='Force regeneration of existing fixtures'
    )

    parser.add_argument(
        '--list',
        action='store_true',
        help='List available fixture specs'
    )

    parser.add_argument(
        '--quiet', '-q',
        action='store_true',
        help='Quiet mode'
    )

    args = parser.parse_args()

    if args.quiet:
        logging.basicConfig(level=logging.WARNING)
    else:
        logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')

    if args.list:
        print("Available synthetic fixtures:")
        for fid, spec in SYNTHETIC_FIXTURES.items():
            print(f"  {fid}: {spec.name} ({spec.width}x{spec.height} @ {spec.fps}fps, {spec.duration_s}s)")
        return

    generator = SyntheticFixtureGenerator(args.output_dir)

    if not generator.check_ffmpeg():
        print("Error: FFmpeg not available")
        sys.exit(1)

    fixture_ids = None
    if args.fixtures:
        fixture_ids = [f.strip() for f in args.fixtures.split(',')]

    results = generator.generate_all(fixture_ids, args.force)

    print("\nGenerated fixtures:")
    for fid, path in results.items():
        if path:
            print(f"  {fid}: {path}")
        else:
            print(f"  {fid}: FAILED")


if __name__ == '__main__':
    main()