#!/usr/bin/env python3
"""
Test Fixtures Generator for P5-2 Standard Material Matrix

使用 FFmpeg 生成合成测试素材，覆盖 P5-2 标准素材矩阵的所有类型。

## 功能
- H.264 8-bit 4:2:0 (1080p/4K)
- H.265 10-bit 4:2:0 (4K HDR)
- ProRes 422 HQ (1080p/4K)
- ProRes 4444 with Alpha (1080p/4K)
- DNxHR HQX (4K)
- NV12/P010 原始样本（YUV shader 正确性测试）

## 使用方法
    python tests/media/generate_test_fixtures.py --all
    python tests/media/generate_test_fixtures.py --fixtures h264_8bit_420_1080p,prores_422_hq_1080p
    python tests/media/generate_test_fixtures.py --group p52_standard_matrix
    python tests/media/generate_test_fixtures.py --list

## 性能目标验证
生成后自动验证素材是否符合 manifest 中定义的性能目标。
"""

from __future__ import annotations

import os
import sys
import json
import subprocess
import argparse
import logging
import time
import hashlib
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class FixtureSpec:
    """素材生成规格"""
    fixture_id: str
    name: str
    description: str
    file_path: str
    width: int
    height: int
    fps: float
    fps_num: int
    fps_den: int
    duration_s: float
    codec: str
    pix_fmt: str
    bit_depth: int
    chroma_subsampling: str
    color_space: str
    color_primaries: str
    color_transfer: str
    color_range: str = 'tv'
    alpha_mode: Optional[str] = None
    gop_size: int = 30
    b_frames: int = 2
    ref_frames: int = 4
    extra_args: List[str] = field(default_factory=list)
    test_patterns: List[str] = field(default_factory=list)
    performance_targets: Dict[str, Any] = field(default_factory=dict)
    tags: List[str] = field(default_factory=list)
    # W0-01: Windows 硬解期望（None=不适用/未标注；
    #   'required'=目标硬解必须可用；'vendor_dependent'=按 vendor 矩阵；
    #   'unsupported_diag'=NVDEC/D3D11VA 均不支持，专用于验证显式诊断回退）
    hw_decode_expectation: Optional[str] = None


# P5-2 标准素材矩阵生成规格
P52_FIXTURES: Dict[str, FixtureSpec] = {
    'h264_8bit_420_1080p': FixtureSpec(
        fixture_id='h264_8bit_420_1080p',
        name='H.264 8-bit 4:2:0 1080p',
        description='P5-2: H.264/AVC 8-bit 4:2:0 1080p',
        file_path='p52/h264_8bit_420_1080p.mp4',
        width=1920,
        height=1080,
        fps=29.97,
        fps_num=30000,
        fps_den=1001,
        duration_s=10.0,
        codec='libx264',
        pix_fmt='yuv420p',
        bit_depth=8,
        chroma_subsampling='4:2:0',
        color_space='bt709',
        color_primaries='bt709',
        color_transfer='bt709',
        color_range='tv',
        gop_size=30,
        b_frames=3,
        ref_frames=4,
        extra_args=[
            '-preset', 'medium',
            '-crf', '23',
            '-level', '4.1',
        ],
        test_patterns=['color_bars', 'gradient', 'checkerboard'],
        performance_targets={
            'hw_decode_fps': {'min': 60.0},
            'proxy_decode_fps': {'min': 120.0, 'scale': 0.5},
            'seek_ms': {'max': 50.0},
            'open_ms': {'max': 200.0},
        },
        tags=['p52', 'h264', '8bit', '420', '1080p', 'hw-decode', 'proxy'],
    ),
    'h264_8bit_420_4k': FixtureSpec(
        fixture_id='h264_8bit_420_4k',
        name='H.264 8-bit 4:2:0 4K',
        description='P5-2: H.264/AVC 8-bit 4:2:0 4K',
        file_path='p52/h264_8bit_420_4k.mp4',
        width=3840,
        height=2160,
        fps=30.0,
        fps_num=30,
        fps_den=1,
        duration_s=10.0,
        codec='libx264',
        pix_fmt='yuv420p',
        bit_depth=8,
        chroma_subsampling='4:2:0',
        color_space='bt709',
        color_primaries='bt709',
        color_transfer='bt709',
        color_range='tv',
        gop_size=30,
        b_frames=2,
        ref_frames=4,
        extra_args=[
            '-preset', 'medium',
            '-crf', '28',
            '-level', '5.1',
        ],
        test_patterns=['color_bars', 'gradient', 'checkerboard'],
        performance_targets={
            'hw_decode_fps': {'min': 30.0},
            'proxy_decode_fps': {'min': 60.0, 'scale': 0.5},
            'cpu_decode_fps': {'min': 12.0},
            'seek_ms': {'max': 100.0},
            'open_ms': {'max': 300.0},
        },
        tags=['p52', 'h264', '8bit', '420', '4k', 'hw-decode', 'proxy'],
    ),
    'h265_10bit_420_4k': FixtureSpec(
        fixture_id='h265_10bit_420_4k',
        name='H.265 10-bit 4:2:0 4K HDR',
        description='P5-2: H.265/HEVC 10-bit 4:2:0 4K HDR',
        file_path='p52/h265_10bit_420_4k.mp4',
        width=3840,
        height=2160,
        fps=24.0,
        fps_num=24,
        fps_den=1,
        duration_s=10.0,
        codec='libx265',
        pix_fmt='yuv420p10le',
        bit_depth=10,
        chroma_subsampling='4:2:0',
        color_space='bt2020nc',
        color_primaries='bt2020',
        color_transfer='smpte2084',
        color_range='tv',
        gop_size=48,
        b_frames=4,
        ref_frames=5,
        extra_args=[
            '-preset', 'medium',
            '-crf', '28',
            '-x265-params',
            'colorprim=bt2020:transfer=smpte2084:colormatrix=bt2020nc:hdr10:level-idc=51',
        ],
        test_patterns=['color_bars', 'gradient', 'hdr_test_patterns'],
        performance_targets={
            'hw_decode_fps': {'min': 24.0},
            'cpu_decode_fps': {'min': 6.0},
            'proxy_decode_fps': {'min': 30.0, 'scale': 0.25},
            'seek_ms': {'max': 150.0},
            'open_ms': {'max': 400.0},
        },
        tags=['p52', 'h265', '10bit', '420', '4k', 'hdr', 'hw-decode', 'fallback'],
    ),
    'prores_422_hq_1080p': FixtureSpec(
        fixture_id='prores_422_hq_1080p',
        name='ProRes 422 HQ 1080p',
        description='P5-2: ProRes 422 HQ 1080p',
        file_path='p52/prores_422_hq_1080p.mov',
        width=1920,
        height=1080,
        fps=24.0,
        fps_num=24,
        fps_den=1,
        duration_s=10.0,
        codec='prores_ks',
        pix_fmt='yuv422p10le',
        bit_depth=10,
        chroma_subsampling='4:2:2',
        color_space='bt709',
        color_primaries='bt709',
        color_transfer='bt709',
        color_range='tv',
        extra_args=[
            '-profile', '3',  # ProRes 422 HQ
            '-qscale', '11',  # Quality scale
        ],
        test_patterns=['color_checker', 'gray_ramp', 'skin_tones'],
        performance_targets={
            'cpu_decode_fps': {'min': 60.0},
            'proxy_decode_fps': {'min': 120.0, 'scale': 0.5},
            'v3_full_decode_fps': {'min': 24.0},
            'seek_ms': {'max': 30.0},
            'open_ms': {'max': 150.0},
        },
        tags=['p52', 'prores', '422hq', '10bit', '422', '1080p', 'cpu-decode', 'v3'],
    ),
    'prores_422_hq_4k': FixtureSpec(
        fixture_id='prores_422_hq_4k',
        name='ProRes 422 HQ 4K',
        description='P5-2: ProRes 422 HQ 4K',
        file_path='p52/prores_422_hq_4k.mov',
        width=3840,
        height=2160,
        fps=24.0,
        fps_num=24,
        fps_den=1,
        duration_s=10.0,
        codec='prores_ks',
        pix_fmt='yuv422p10le',
        bit_depth=10,
        chroma_subsampling='4:2:2',
        color_space='bt709',
        color_primaries='bt709',
        color_transfer='bt709',
        color_range='tv',
        extra_args=[
            '-profile', '3',
            '-qscale', '11',
        ],
        test_patterns=['color_checker', 'gray_ramp', 'skin_tones'],
        performance_targets={
            'cpu_decode_fps': {'min': 24.0},
            'proxy_decode_fps': {'min': 48.0, 'scale': 0.5},
            'v3_full_decode_fps': {'min': 12.0},
            'seek_ms': {'max': 50.0},
            'open_ms': {'max': 200.0},
        },
        tags=['p52', 'prores', '422hq', '10bit', '422', '4k', 'cpu-decode'],
    ),
    'prores_4444_1080p': FixtureSpec(
        fixture_id='prores_4444_1080p',
        name='ProRes 4444 with Alpha 1080p',
        description='P5-2: ProRes 4444 1080p 含 Alpha',
        file_path='p52/prores_4444_1080p.mov',
        width=1920,
        height=1080,
        fps=24.0,
        fps_num=24,
        fps_den=1,
        duration_s=5.0,
        codec='prores_ks',
        pix_fmt='yuva444p12le',
        bit_depth=12,
        chroma_subsampling='4:4:4',
        color_space='bt709',
        color_primaries='bt709',
        color_transfer='bt709',
        color_range='tv',
        alpha_mode='straight',
        extra_args=[
            '-profile', '4',  # ProRes 4444
            '-qscale', '10',
        ],
        test_patterns=['alpha_checkerboard', 'gradient_alpha', 'color_checker'],
        performance_targets={
            'cpu_decode_fps': {'min': 24.0},
            'proxy_decode_fps': {'min': 48.0, 'scale': 0.5},
            'alpha_channel_test': {'required': True},
            'seek_ms': {'max': 40.0},
            'open_ms': {'max': 180.0},
        },
        tags=['p52', 'prores', '4444', '12bit', '444', 'alpha', '1080p', 'fallback'],
    ),
    'prores_4444_4k': FixtureSpec(
        fixture_id='prores_4444_4k',
        name='ProRes 4444 with Alpha 4K',
        description='P5-2: ProRes 4444 4K 含 Alpha',
        file_path='p52/prores_4444_4k.mov',
        width=3840,
        height=2160,
        fps=24.0,
        fps_num=24,
        fps_den=1,
        duration_s=5.0,
        codec='prores_ks',
        pix_fmt='yuva444p12le',
        bit_depth=12,
        chroma_subsampling='4:4:4',
        color_space='bt709',
        color_primaries='bt709',
        color_transfer='bt709',
        color_range='tv',
        alpha_mode='straight',
        extra_args=[
            '-profile', '4',
            '-qscale', '10',
        ],
        test_patterns=['alpha_checkerboard', 'gradient_alpha', 'color_checker'],
        performance_targets={
            'cpu_decode_fps': {'min': 12.0},
            'proxy_decode_fps': {'min': 24.0, 'scale': 0.25},
            'alpha_channel_test': {'required': True},
            'seek_ms': {'max': 80.0},
            'open_ms': {'max': 300.0},
        },
        tags=['p52', 'prores', '4444', '12bit', '444', 'alpha', '4k', 'fallback'],
    ),
    'dnxhr_hqx_4k': FixtureSpec(
        fixture_id='dnxhr_hqx_4k',
        name='DNxHR HQX 4K',
        description='P5-2: DNxHR HQX 4K',
        file_path='p52/dnxhr_hqx_4k.mxf',
        width=3840,
        height=2160,
        fps=25.0,
        fps_num=25,
        fps_den=1,
        duration_s=8.0,
        codec='dnxhd',
        pix_fmt='yuv422p10le',
        bit_depth=10,
        chroma_subsampling='4:2:2',
        color_space='bt709',
        color_primaries='bt709',
        color_transfer='bt709',
        color_range='tv',
        extra_args=[
            '-vb', '180M',  # ~180 Mbps for HQX at 4K
            '-profile', 'vbr_4k_hqx',
        ],
        test_patterns=['color_checker', 'gray_ramp', 'skin_tones'],
        performance_targets={
            'cpu_decode_fps': {'min': 30.0},
            'proxy_decode_fps': {'min': 60.0, 'scale': 0.5},
            'seek_ms': {'max': 40.0},
            'open_ms': {'max': 180.0},
        },
        tags=['p52', 'dnxhr', 'hqx', '10bit', '422', '4k', 'intra', 'cpu-decode'],
    ),
    # ------------------------------------------------------------------
    # W0-01: Windows 解码素材矩阵（AV1 + 4:2:2 诊断用例）
    # ------------------------------------------------------------------
    'av1_8bit_420_1080p': FixtureSpec(
        fixture_id='av1_8bit_420_1080p',
        name='AV1 8-bit 4:2:0 1080p',
        description='W0-01: AV1 8-bit 4:2:0 1080p（AV1 硬解：RDNA2+/Ampere+/DG+，按 vendor 矩阵）',
        file_path='p52/av1_8bit_420_1080p.mp4',
        width=1920,
        height=1080,
        fps=24.0,
        fps_num=24,
        fps_den=1,
        duration_s=10.0,
        codec='libaom-av1',
        pix_fmt='yuv420p',
        bit_depth=8,
        chroma_subsampling='4:2:0',
        color_space='bt709',
        color_primaries='bt709',
        color_transfer='bt709',
        color_range='tv',
        extra_args=[
            '-crf', '30',
            '-cpu-used', '8',
            '-row-mt', '1',
        ],
        test_patterns=['color_bars', 'gradient'],
        hw_decode_expectation='vendor_dependent',
        tags=['w0', 'av1', '8bit', '420', '1080p', 'hw-decode'],
    ),
    'av1_10bit_420_4k': FixtureSpec(
        fixture_id='av1_10bit_420_4k',
        name='AV1 10-bit 4:2:0 4K',
        description='W0-01: AV1 10-bit 4:2:0 4K（硬解 vendor 依赖，时长压到 5s 控制编码成本）',
        file_path='p52/av1_10bit_420_4k.mp4',
        width=3840,
        height=2160,
        fps=24.0,
        fps_num=24,
        fps_den=1,
        duration_s=5.0,
        codec='libaom-av1',
        pix_fmt='yuv420p10le',
        bit_depth=10,
        chroma_subsampling='4:2:0',
        color_space='bt709',
        color_primaries='bt709',
        color_transfer='bt709',
        color_range='tv',
        extra_args=[
            '-crf', '32',
            '-cpu-used', '8',
            '-row-mt', '1',
        ],
        test_patterns=['color_bars', 'gradient'],
        hw_decode_expectation='vendor_dependent',
        tags=['w0', 'av1', '10bit', '420', '4k', 'hw-decode'],
    ),
    'h264_422_8bit_1080p': FixtureSpec(
        fixture_id='h264_422_8bit_1080p',
        name='H.264 4:2:2 8-bit 1080p',
        description='W0-01 诊断用例：NVDEC/D3D11VA 均不支持 4:2:2，验证显式诊断回退（禁静默软解伪装）',
        file_path='p52/h264_422_8bit_1080p.mp4',
        width=1920,
        height=1080,
        fps=24.0,
        fps_num=24,
        fps_den=1,
        duration_s=10.0,
        codec='libx264',
        pix_fmt='yuv422p',
        bit_depth=8,
        chroma_subsampling='4:2:2',
        color_space='bt709',
        color_primaries='bt709',
        color_transfer='bt709',
        color_range='tv',
        extra_args=[
            '-preset', 'medium',
            '-crf', '20',
            '-profile:v', 'high422',
        ],
        test_patterns=['color_bars', 'checkerboard'],
        hw_decode_expectation='unsupported_diag',
        tags=['w0', 'h264', '8bit', '422', '1080p', 'diag', 'cpu-decode'],
    ),
    'h264_422_10bit_1080p': FixtureSpec(
        fixture_id='h264_422_10bit_1080p',
        name='H.264 4:2:2 10-bit 1080p',
        description='W0-01 必含诊断用例（计划验收标准）：硬解 session 可能成功打开但输出 yuv422p10le，'
                    'open() 首帧验证必须拒绝并显式回退 CPU planar',
        file_path='p52/h264_422_10bit_1080p.mp4',
        width=1920,
        height=1080,
        fps=24.0,
        fps_num=24,
        fps_den=1,
        duration_s=10.0,
        codec='libx264',
        pix_fmt='yuv422p10le',
        bit_depth=10,
        chroma_subsampling='4:2:2',
        color_space='bt709',
        color_primaries='bt709',
        color_transfer='bt709',
        color_range='tv',
        extra_args=[
            '-preset', 'medium',
            '-crf', '20',
            '-profile:v', 'high422',
        ],
        test_patterns=['color_bars', 'checkerboard'],
        hw_decode_expectation='unsupported_diag',
        tags=['w0', 'h264', '10bit', '422', '1080p', 'diag', 'cpu-decode'],
    ),
    'h265_422_10bit_1080p': FixtureSpec(
        fixture_id='h265_422_10bit_1080p',
        name='H.265 4:2:2 10-bit 1080p',
        description='W0-01 诊断用例：HEVC Main 4:2:2 10 硬解不可用，验证显式诊断回退',
        file_path='p52/h265_422_10bit_1080p.mp4',
        width=1920,
        height=1080,
        fps=24.0,
        fps_num=24,
        fps_den=1,
        duration_s=10.0,
        codec='libx265',
        pix_fmt='yuv422p10le',
        bit_depth=10,
        chroma_subsampling='4:2:2',
        color_space='bt709',
        color_primaries='bt709',
        color_transfer='bt709',
        color_range='tv',
        extra_args=[
            '-preset', 'medium',
            '-crf', '24',
        ],
        test_patterns=['color_bars', 'checkerboard'],
        hw_decode_expectation='unsupported_diag',
        tags=['w0', 'h265', '10bit', '422', '1080p', 'diag', 'cpu-decode'],
    ),
}


class FixtureGenerator:
    """素材生成器"""

    def __init__(self, output_dir: Optional[str] = None):
        """
        初始化生成器。

        Args:
            output_dir: 输出目录，默认为 tests/media/fixtures/
        """
        if output_dir:
            self.output_dir = Path(output_dir)
        else:
            project_root = Path(__file__).parent.parent.parent
            self.output_dir = project_root / 'tests' / 'media' / 'fixtures'

        self.output_dir.mkdir(parents=True, exist_ok=True)

        # 加载 manifest
        self.manifest: Dict[str, Any] = {}
        self._load_manifest()

    def _load_manifest(self) -> None:
        """加载 fixtures_manifest.json"""
        manifest_path = Path(__file__).parent / 'fixtures_manifest.json'
        if manifest_path.exists():
            try:
                with open(manifest_path, 'r', encoding='utf-8') as f:
                    self.manifest = json.load(f)
            except (json.JSONDecodeError, IOError) as e:
                logger.warning(f"Failed to load manifest: {e}")

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

    def get_test_pattern_filter(self, patterns: List[str], spec: FixtureSpec) -> str:
        """
        根据测试图案类型生成 FFmpeg filter。

        Args:
            patterns: 测试图案列表
            spec: 素材规格

        Returns:
            FFmpeg filter 字符串
        """
        # 优先使用 smpte_bars（标准 SMPTE 彩条）
        if 'color_bars' in patterns:
            return f'smptebars=size={spec.width}x{spec.height}:rate={spec.fps}'
        elif 'color_checker' in patterns:
            # ColorChecker 需要特殊处理
            return f'testsrc2=size={spec.width}x{spec.height}:rate={spec.fps}'
        elif 'alpha_checkerboard' in patterns:
            # Alpha checkerboard 需要特殊处理
            return self._generate_alpha_pattern(spec)
        else:
            return f'testsrc2=size={spec.width}x{spec.height}:rate={spec.fps}'

    def _generate_alpha_pattern(self, spec: FixtureSpec) -> str:
        """
        生成带 Alpha 通道的测试图案。

        Args:
            spec: 素材规格

        Returns:
            FFmpeg filter 字符串
        """
        # 使用 geq filter 创建自定义 alpha pattern
        # checkerboard pattern with alpha
        return (
            f'testsrc2=size={spec.width}x{spec.height}:rate={spec.fps},'
            f'format=rgba,'
            f'geq=r=128:g=128:b=128:a=if(lt(mod(X\\,128)\\,64)\\,255\\,0)'
        )

    def generate_fixture(self, spec: FixtureSpec, force: bool = False) -> Optional[Path]:
        """
        生成合成素材。

        Args:
            spec: 素材规格
            force: 是否强制重新生成

        Returns:
            生成的文件路径或 None（失败时）
        """
        output_path = self.output_dir / spec.file_path

        # 创建子目录
        output_path.parent.mkdir(parents=True, exist_ok=True)

        # 检查是否已存在
        if output_path.exists() and not force:
            logger.info(f"素材已存在: {output_path}")
            return output_path

        logger.info(f"生成素材: {spec.name}")
        logger.info(f"  规格: {spec.width}x{spec.height} @ {spec.fps}fps, {spec.duration_s}s")
        logger.info(f"  编码: {spec.codec}, {spec.pix_fmt}")
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
                timeout=300,  # 最长 5 分钟
            )

            if result.returncode != 0:
                logger.error(f"FFmpeg 失败: {result.stderr}")
                return None

            elapsed = time.time() - start_time
            logger.info(f"  生成成功 ({elapsed:.1f}s)")
            logger.info(f"  文件大小: {output_path.stat().st_size / 1024 / 1024:.1f} MB")

            # 验证文件
            if self._verify_fixture(output_path, spec):
                return output_path
            else:
                logger.error(f"验证失败: {output_path}")
                output_path.unlink(missing_ok=True)
                return None

        except subprocess.TimeoutExpired:
            logger.error(f"生成超时: {spec.name}")
            output_path.unlink(missing_ok=True)  # 无限源失控时不能留下巨型半成品
            return None
        except Exception as e:
            logger.error(f"生成失败: {e}")
            return None

    def _build_ffmpeg_command(self, spec: FixtureSpec, output_path: Path) -> List[str]:
        """
        构建 FFmpeg 命令。

        Args:
            spec: 素材规格
            output_path: 输出路径

        Returns:
            FFmpeg 命令列表
        """
        # 基础命令
        cmd = [
            'ffmpeg',
            '-y',  # 覆盖输出
            '-f', 'lavfi',
            '-i', self.get_test_pattern_filter(spec.test_patterns, spec),
        ]

        # 添加音频轨道（可选）
        if not spec.file_path.endswith('.nv12') and not spec.file_path.endswith('.p010'):
            cmd.extend([
                '-f', 'lavfi',
                '-i', f'sine=frequency=1000:duration={spec.duration_s}',
            ])

        # 视频编码参数
        cmd.extend([
            '-c:v', spec.codec,
            '-pix_fmt', spec.pix_fmt,
            '-colorspace', spec.color_space,
            '-color_primaries', spec.color_primaries,
            '-color_trc', spec.color_transfer,
            '-color_range', spec.color_range,
        ])

        # GOP 参数（H.264/H.265）
        if spec.codec in ['libx264', 'libx265']:
            cmd.extend([
                '-g', str(spec.gop_size),
                '-bf', str(spec.b_frames),
                '-refs', str(spec.ref_frames),
            ])

        # 编码器特定参数
        if spec.extra_args:
            cmd.extend(spec.extra_args)

        # 音频编码（如果添加了音频）
        if not spec.file_path.endswith('.nv12') and not spec.file_path.endswith('.p010'):
            cmd.extend([
                '-c:a', 'aac',
                '-b:a', '128k',
            ])

        # 输出文件（-t 必须是输出选项：放输入之间会被当作第二个输入的
        # 时长限制，导致无限 lavfi 源永不停止——曾造成 300s 超时）
        cmd.extend(['-t', str(spec.duration_s)])
        cmd.append(str(output_path))

        return cmd

    def _verify_fixture(self, path: Path, spec: FixtureSpec) -> bool:
        """验证生成的素材是否符合规格"""
        try:
            result = subprocess.run(
                ['ffprobe', '-v', 'error', '-show_entries',
                 'stream=width,height,pix_fmt,r_frame_rate,codec_name,'
                 'color_space,color_primaries,color_transfer,'
                 'bits_per_raw_sample,has_alpha',
                 '-of', 'json', str(path)],
                capture_output=True,
                text=True,
                timeout=30,
            )

            if result.returncode != 0:
                logger.error(f"ffprobe 失败: {result.stderr}")
                return False

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
                ('codec_name', spec.codec.replace('libx264', 'h264').replace('libx265', 'hevc').replace('prores_ks', 'prores')),
                ('pix_fmt', spec.pix_fmt),
            ]

            all_ok = True
            for key, expected in checks:
                actual = video_stream.get(key, '')
                # 对于 codec_name，检查是否匹配（考虑别名）
                if key == 'codec_name':
                    if expected == 'prores' and actual not in ['prores', 'prores_ks']:
                        logger.warning(f"  {key}: 期望 {expected}, 实际 {actual}")
                        all_ok = False
                elif actual != expected:
                    logger.warning(f"  {key}: 期望 {expected}, 实际 {actual}")
                    all_ok = False

            # 检查 Alpha 通道（如果需要）
            if spec.alpha_mode:
                has_alpha = video_stream.get('has_alpha', '0')
                if has_alpha != '1':
                    logger.warning(f"  Alpha 通道: 期望存在, 实际 {has_alpha}")
                    all_ok = False

            return all_ok

        except Exception as e:
            logger.error(f"验证失败: {e}")
            return False

    def generate_nv12_sample(self, spec: Dict[str, Any]) -> Optional[Path]:
        """
        生成 NV12 原始样本。

        NV12 是硬件解码器常见的输出格式（YUV 4:2:0 semiplanar）。

        Args:
            spec: NV12 样本规格

        Returns:
            生成的文件路径或 None
        """
        output_path = self.output_dir / spec['file_path']
        output_path.parent.mkdir(parents=True, exist_ok=True)

        if output_path.exists():
            logger.info(f"NV12 样本已存在: {output_path}")
            return output_path

        width = spec['resolution']['width']
        height = spec['resolution']['height']

        logger.info(f"生成 NV12 样本: {width}x{height}")

        # NV12 格式: Y plane (width * height) + UV plane (width * height / 2)
        # UV plane: U 和 V interleaved

        try:
            # 使用 FFmpeg 生成测试图案并提取 NV12
            cmd = [
                'ffmpeg',
                '-y',
                '-f', 'lavfi',
                '-i', f'smptebars=size={width}x{height}:rate=1',
                '-vframes', '1',
                '-pix_fmt', 'nv12',
                '-f', 'rawvideo',
                str(output_path),
            ]

            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=30,
            )

            if result.returncode != 0:
                logger.error(f"NV12 生成失败: {result.stderr}")
                return None

            # 验证文件大小
            expected_size = width * height + width * height // 2  # Y + UV
            actual_size = output_path.stat().st_size

            if actual_size != expected_size:
                logger.error(f"NV12 文件大小错误: 期望 {expected_size}, 实际 {actual_size}")
                output_path.unlink(missing_ok=True)
                return None

            logger.info(f"  NV12 样本生成成功: {actual_size} bytes")
            return output_path

        except Exception as e:
            logger.error(f"NV12 生成失败: {e}")
            return None

    def generate_p010_sample(self, spec: Dict[str, Any]) -> Optional[Path]:
        """
        生成 P010 原始样本。

        P010 是 10-bit NV12（HDR 硬件解码输出格式）。

        Args:
            spec: P010 样本规格

        Returns:
            生成的文件路径或 None
        """
        output_path = self.output_dir / spec['file_path']
        output_path.parent.mkdir(parents=True, exist_ok=True)

        if output_path.exists():
            logger.info(f"P010 样本已存在: {output_path}")
            return output_path

        width = spec['resolution']['width']
        height = spec['resolution']['height']

        logger.info(f"生成 P010 样本: {width}x{height}")

        # P010 格式: Y plane (width * height * 2 bytes) + UV plane (width * height bytes)
        # 每个 Y 值是 16-bit，但有效数据是 10-bit (高 6 bits 为 0 或 padding)

        try:
            # 使用 FFmpeg 生成 HDR 测试图案并提取 P010
            cmd = [
                'ffmpeg',
                '-y',
                '-f', 'lavfi',
                '-i', f'testsrc2=size={width}x{height}:rate=1',
                '-vframes', '1',
                '-pix_fmt', 'p010le',
                '-colorspace', 'bt2020nc',
                '-color_primaries', 'bt2020',
                '-color_trc', 'smpte2084',
                '-f', 'rawvideo',
                str(output_path),
            ]

            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=30,
            )

            if result.returncode != 0:
                logger.error(f"P010 生成失败: {result.stderr}")
                return None

            # 验证文件大小
            expected_size = width * height * 2 + width * height  # Y (16-bit) + UV (interleaved 16-bit)
            actual_size = output_path.stat().st_size

            if actual_size != expected_size:
                logger.error(f"P010 文件大小错误: 期望 {expected_size}, 实际 {actual_size}")
                output_path.unlink(missing_ok=True)
                return None

            logger.info(f"  P010 样本生成成功: {actual_size} bytes")
            return output_path

        except Exception as e:
            logger.error(f"P010 生成失败: {e}")
            return None

    def generate_all(self, fixtures: Optional[List[str]] = None,
                      force: bool = False) -> Dict[str, Optional[Path]]:
        """
        生成所有素材或指定素材。

        Args:
            fixtures: 要生成的素材 ID 列表
            force: 是否强制重新生成

        Returns:
            fixture_id -> 文件路径 的字典
        """
        if fixtures:
            specs = [P52_FIXTURES[f] for f in fixtures if f in P52_FIXTURES]
        else:
            specs = list(P52_FIXTURES.values())

        results = {}
        for spec in specs:
            path = self.generate_fixture(spec, force)
            results[spec.fixture_id] = path

        # 生成 NV12/P010 样本
        if 'nv12_sample_1080p' in fixtures or fixtures is None:
            nv12_spec = self.manifest.get('p52_standard_material_matrix', {}).get('fixtures', {}).get('nv12_sample_1080p', {})
            if nv12_spec:
                path = self.generate_nv12_sample(nv12_spec)
                results['nv12_sample_1080p'] = path

        if 'p010_sample_1080p' in fixtures or fixtures is None:
            p010_spec = self.manifest.get('p52_standard_material_matrix', {}).get('fixtures', {}).get('p010_sample_1080p', {})
            if p010_spec:
                path = self.generate_p010_sample(p010_spec)
                results['p010_sample_1080p'] = path

        return results

    def get_fixture_path(self, fixture_id: str) -> Optional[Path]:
        """获取已存在的素材路径"""
        if fixture_id in P52_FIXTURES:
            path = self.output_dir / P52_FIXTURES[fixture_id].file_path
        else:
            # 从 manifest 查找
            spec = self.manifest.get('p52_standard_material_matrix', {}).get('fixtures', {}).get(fixture_id, {})
            if spec:
                path = self.output_dir / spec['file_path']
            else:
                return None

        if path.exists():
            return path
        return None


def get_available_fixtures() -> List[str]:
    """获取所有可生成的素材 ID"""
    return list(P52_FIXTURES.keys()) + ['nv12_sample_1080p', 'p010_sample_1080p']


def get_fixture_groups() -> Dict[str, List[str]]:
    """获取素材组定义"""
    return {
        'p52_standard_matrix': list(P52_FIXTURES.keys()) + ['nv12_sample_1080p', 'p010_sample_1080p'],
        'p52_hw_accel': ['h264_8bit_420_1080p', 'h264_8bit_420_4k', 'h265_10bit_420_4k'],
        # W0-01: Windows 解码矩阵（含诊断用例；真实素材另见 tests/test_videos）
        'win_hw_decode_matrix': [
            'h264_8bit_420_1080p', 'h264_8bit_420_4k', 'h265_10bit_420_4k',
            'av1_8bit_420_1080p', 'av1_10bit_420_4k',
            'h264_422_10bit_1080p', 'h265_422_10bit_1080p',
        ],
        'win_hw_decode_diag': [
            'h264_422_8bit_1080p', 'h264_422_10bit_1080p', 'h265_422_10bit_1080p',
        ],
        'p52_proxy': ['h264_8bit_420_1080p', 'h264_8bit_420_4k', 'prores_422_hq_1080p', 'prores_422_hq_4k'],
        'p52_alpha': ['prores_4444_1080p', 'prores_4444_4k'],
        'p52_intra': ['prores_422_hq_1080p', 'prores_422_hq_4k', 'dnxhr_hqx_4k'],
        'p52_shader_test': ['nv12_sample_1080p', 'p010_sample_1080p'],
    }


def main():
    """主函数"""
    parser = argparse.ArgumentParser(
        description='Generate test fixtures for P5-2 standard material matrix'
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
        '--group', '-g',
        type=str,
        help='Generate fixtures from a named group'
    )

    parser.add_argument(
        '--all', '-a',
        action='store_true',
        help='Generate all fixtures'
    )

    parser.add_argument(
        '--force',
        action='store_true',
        help='Force regeneration of existing fixtures'
    )

    parser.add_argument(
        '--list', '-l',
        action='store_true',
        help='List available fixture specs and groups'
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
        print("Available fixtures:")
        for fid in get_available_fixtures():
            if fid in P52_FIXTURES:
                spec = P52_FIXTURES[fid]
                print(f"  {fid}: {spec.name} ({spec.width}x{spec.height} @ {spec.fps}fps)")
            else:
                print(f"  {fid}: Raw sample")

        print("\nFixture groups:")
        for group, fixtures in get_fixture_groups().items():
            print(f"  {group}: {', '.join(fixtures)}")

        return

    generator = FixtureGenerator(args.output_dir)

    if not generator.check_ffmpeg():
        print("Error: FFmpeg not available")
        sys.exit(1)

    # 确定要生成的素材
    fixtures = None
    if args.fixtures:
        fixtures = [f.strip() for f in args.fixtures.split(',')]
    elif args.group:
        groups = get_fixture_groups()
        fixtures = groups.get(args.group)
        if not fixtures:
            print(f"Error: Unknown group '{args.group}'")
            sys.exit(1)
    elif args.all:
        fixtures = None  # Generate all

    results = generator.generate_all(fixtures, args.force)

    print("\nGenerated fixtures:")
    success_count = 0
    for fid, path in results.items():
        if path:
            print(f"  {fid}: {path}")
            success_count += 1
        else:
            print(f"  {fid}: FAILED")

    print(f"\nSummary: {success_count}/{len(results)} fixtures generated")


if __name__ == '__main__':
    main()