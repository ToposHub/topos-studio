#!/usr/bin/env python3
"""
Video Pipeline Performance Benchmark Script

用于测量 Topos Color 视频解码管线的性能指标。

测量指标：
- 打开时间（文件打开到第一帧可用）
- seek 时间（随机帧定位）
- 顺序解码 fps
- GPU 上传时间（可选）
- 播放掉帧率（模拟播放场景）

输出格式：JSON / CSV

使用方法：
    python tests/benchmark/benchmark_video_pipeline.py --fixture prores_422_hq_10bit
    python tests/benchmark/benchmark_video_pipeline.py --all --output json
    python tests/benchmark/benchmark_video_pipeline.py --custom /path/to/video.mp4
"""

import os
import sys
import json
import csv
import time
import argparse
import statistics
import logging
from pathlib import Path
from typing import Optional, Dict, List, Any, Tuple
from datetime import datetime

# 确保项目根目录在 path 中
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from src.shared.media.adapter import VideoDecoderAdapter as VideoDecoder  # v2 GStreamer 已移除，改用 PyAV 适配器

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


class BenchmarkResult:
    """单个测试素材的基准测试结果

    P10.A 要求的指标：
    - open_ms: 打开时间
    - seek_ms: Seek 时间
    - sequential_decode_fps: 顺序解码帧率
    - proxy_decode_fps: Proxy 解码帧率
    - upload_ms: GPU 上传时间
    - render_ms: 渲染时间
    - displayed_fps: 实际显示帧率
    - dropped_frames: 丢帧数
    - memory_mb: 内存使用（MB）
    - gpu_texture_count: GPU 纹理计数
    """

    def __init__(self, fixture_id: str):
        self.fixture_id = fixture_id
        self.timestamp = datetime.now().isoformat()

        # 打开时间测试
        self.open_times: List[float] = []  # 毫秒

        # seek 时间测试
        self.seek_times: List[float] = []  # 毫秒
        self.seek_positions: List[int] = []  # 测试的帧位置

        # 顺序解码测试
        self.sequential_decode_fps: List[float] = []
        self.sequential_frames_decoded: int = 0
        self.sequential_total_time_ms: float = 0

        # P10.A: Proxy 解码测试
        self.proxy_decode_fps: List[float] = []
        self.proxy_frames_decoded: int = 0
        self.proxy_total_time_ms: float = 0
        self.proxy_enabled: bool = False
        self.proxy_scale_factor: float = 0.5  # 默认 proxy 缩放因子

        # GPU 上传测试（可选）
        self.gpu_upload_times: List[float] = []  # 毫秒

        # P10.A: 渲染时间测试
        self.render_times: List[float] = []  # 毫秒
        self.render_frames_count: int = 0

        # 播放掉帧测试
        self.playback_target_fps: float = 0
        self.playback_actual_fps: float = 0
        self.playback_dropped_frames: int = 0
        self.playback_total_frames: int = 0
        self.playback_drop_rate: float = 0

        # P10.A: displayed_fps（实际显示帧率，考虑丢帧）
        self.displayed_fps: float = 0.0

        # 视频属性
        self.video_width: int = 0
        self.video_height: int = 0
        self.video_fps: float = 0
        self.video_total_frames: int = 0
        self.video_duration_s: float = 0

        # P10.A: 内存和 GPU 纹理统计
        self.memory_mb: float = 0.0
        self.memory_peak_mb: float = 0.0
        self.gpu_texture_count: int = 0
        self.gpu_texture_pool_size: int = 0

        # 错误信息
        self.errors: List[str] = []
    
    def to_dict(self) -> Dict[str, Any]:
        """转换为字典格式，包含 P10.A 要求的所有指标"""
        return {
            'fixture_id': self.fixture_id,
            'timestamp': self.timestamp,
            'video_properties': {
                'width': self.video_width,
                'height': self.video_height,
                'fps': self.video_fps,
                'total_frames': self.video_total_frames,
                'duration_s': self.video_duration_s,
            },
            # P10.A 必需指标
            'open_time': {
                'samples': len(self.open_times),
                'min_ms': min(self.open_times) if self.open_times else None,
                'max_ms': max(self.open_times) if self.open_times else None,
                'mean_ms': statistics.mean(self.open_times) if self.open_times else None,
                'median_ms': statistics.median(self.open_times) if self.open_times else None,
                'stdev_ms': statistics.stdev(self.open_times) if len(self.open_times) > 1 else None,
            },
            'seek_time': {
                'samples': len(self.seek_times),
                'min_ms': min(self.seek_times) if self.seek_times else None,
                'max_ms': max(self.seek_times) if self.seek_times else None,
                'mean_ms': statistics.mean(self.seek_times) if self.seek_times else None,
                'median_ms': statistics.median(self.seek_times) if self.seek_times else None,
                'stdev_ms': statistics.stdev(self.seek_times) if len(self.seek_times) > 1 else None,
                'positions': self.seek_positions,
            },
            'sequential_decode': {
                'frames_decoded': self.sequential_frames_decoded,
                'total_time_ms': self.sequential_total_time_ms,
                'fps_samples': len(self.sequential_decode_fps),
                'fps_mean': statistics.mean(self.sequential_decode_fps) if self.sequential_decode_fps else None,
                'fps_min': min(self.sequential_decode_fps) if self.sequential_decode_fps else None,
                'fps_max': max(self.sequential_decode_fps) if self.sequential_decode_fps else None,
            },
            # P10.A: Proxy 解码指标
            'proxy_decode': {
                'enabled': self.proxy_enabled,
                'scale_factor': self.proxy_scale_factor,
                'frames_decoded': self.proxy_frames_decoded,
                'total_time_ms': self.proxy_total_time_ms,
                'fps_mean': statistics.mean(self.proxy_decode_fps) if self.proxy_decode_fps else None,
                'fps_min': min(self.proxy_decode_fps) if self.proxy_decode_fps else None,
                'fps_max': max(self.proxy_decode_fps) if self.proxy_decode_fps else None,
            },
            'gpu_upload': {
                'samples': len(self.gpu_upload_times),
                'min_ms': min(self.gpu_upload_times) if self.gpu_upload_times else None,
                'max_ms': max(self.gpu_upload_times) if self.gpu_upload_times else None,
                'mean_ms': statistics.mean(self.gpu_upload_times) if self.gpu_upload_times else None,
            },
            # P10.A: 渲染时间指标
            'render_time': {
                'samples': len(self.render_times),
                'min_ms': min(self.render_times) if self.render_times else None,
                'max_ms': max(self.render_times) if self.render_times else None,
                'mean_ms': statistics.mean(self.render_times) if self.render_times else None,
                'median_ms': statistics.median(self.render_times) if self.render_times else None,
                'frames_count': self.render_frames_count,
            },
            'playback': {
                'target_fps': self.playback_target_fps,
                'actual_fps': self.playback_actual_fps,
                'total_frames': self.playback_total_frames,
                'dropped_frames': self.playback_dropped_frames,
                'drop_rate_percent': self.playback_drop_rate * 100,
            },
            # P10.A: displayed_fps（实际显示帧率）
            'displayed_fps': self.displayed_fps,
            # P10.A: 内存和 GPU 纹理统计
            'memory': {
                'current_mb': self.memory_mb,
                'peak_mb': self.memory_peak_mb,
            },
            'gpu_texture': {
                'count': self.gpu_texture_count,
                'pool_size': self.gpu_texture_pool_size,
            },
            'errors': self.errors,
        }
    
    def get_p10a_summary(self) -> Dict[str, Any]:
        """
        获取 P10.A 要求的摘要指标。

        Returns:
            包含所有 P10.A 必需指标的字典
        """
        return {
            'fixture_id': self.fixture_id,
            'open_ms': statistics.median(self.open_times) if self.open_times else None,
            'seek_ms': statistics.median(self.seek_times) if self.seek_times else None,
            'sequential_decode_fps': statistics.mean(self.sequential_decode_fps) if self.sequential_decode_fps else None,
            'proxy_decode_fps': statistics.mean(self.proxy_decode_fps) if self.proxy_decode_fps else None,
            'upload_ms': statistics.mean(self.gpu_upload_times) if self.gpu_upload_times else None,
            'render_ms': statistics.median(self.render_times) if self.render_times else None,
            'displayed_fps': self.displayed_fps,
            'dropped_frames': self.playback_dropped_frames,
            'memory_mb': self.memory_mb,
            'gpu_texture_count': self.gpu_texture_count,
        }


class VideoPipelineBenchmark:
    """视频管线基准测试器

    P10.A 要求测试以下素材：
    - 1080p H.264 SDR
    - 4K H.264
    - 4K H.265 10-bit
    - 4K ProRes 422
    - ProRes 4444 alpha
    - DNxHR
    """

    # P10.A 验收素材 ID 列表
    P10A_FIXTURE_IDS = [
        'h264_long_gop',          # 1080p H.264 SDR
        'h264_4k',                # 4K H.264（需添加到 manifest）
        'h265_10bit_pq',          # 4K H.265 10-bit
        'prores_422_4k',          # 4K ProRes 422（需添加到 manifest）
        'prores_4444_alpha',      # ProRes 4444 alpha
        'dnxhr_hqx_10bit',        # DNxHR (4K)
    ]

    def __init__(self, fixtures_path: Optional[str] = None):
        """
        初始化基准测试器。

        Args:
            fixtures_path: 测试素材路径（目录或环境变量）
        """
        self.fixtures_path = fixtures_path or os.environ.get(
            'TOPOS_VIDEO_FIXTURES_PATH',
            '~/VideoTestFixtures'
        )
        self.fixtures_manifest: Dict[str, Any] = {}
        self.results: List[BenchmarkResult] = []

        # 测试配置
        self.config = {
            'open_test_iterations': 5,  # 打开测试迭代次数
            'seek_test_iterations': 10,  # seek 测试迭代次数
            'seek_positions': None,  # 自动生成 seek 位置
            'sequential_test_frames': 100,  # 顺序解码测试帧数
            'sequential_batch_size': 10,  # 批量统计 fps
            'playback_duration_s': 5.0,  # 播放测试时长（秒）
            'playback_tolerance_ms': 50,  # 播放掉帧判定阈值
            'gpu_upload_test': False,  # 是否测试 GPU 上传
            # P10.A 新增配置
            'proxy_test_frames': 100,  # Proxy 解码测试帧数
            'proxy_scale_factor': 0.5,  # Proxy 缩放因子
            'render_test_frames': 30,  # 渲染时间测试帧数
            'memory_tracking': True,  # 是否追踪内存
            'gpu_texture_tracking': True,  # 是否追踪 GPU 纹理
        }
    
    def load_manifest(self, manifest_path: str) -> bool:
        """
        加载测试素材清单。
        
        Args:
            manifest_path: manifest JSON 文件路径
            
        Returns:
            是否加载成功
        """
        try:
            with open(manifest_path, 'r', encoding='utf-8') as f:
                self.fixtures_manifest = json.load(f)
            logger.info(f"加载 manifest: {manifest_path}")
            return True
        except Exception as e:
            logger.error(f"加载 manifest 失败: {e}")
            return False
    
    def get_fixture_path(self, fixture_id: str) -> Optional[str]:
        """
        获取测试素材的实际文件路径。
        
        Args:
            fixture_id: 素材 ID
            
        Returns:
            文件路径或 None
        """
        # 从 manifest 中查找 fixture
        fixture = self._find_fixture(fixture_id)
        if not fixture:
            logger.error(f"未找到素材: {fixture_id}")
            return None
        
        # 构建完整路径
        relative_path = fixture.get('file_path', '')
        base_path = self.fixtures_path
        
        # 处理环境变量和用户目录
        if base_path.startswith('~'):
            base_path = os.path.expanduser(base_path)
        elif base_path.startswith('$'):
            base_path = os.environ.get(base_path[1:], base_path)
        
        full_path = os.path.join(base_path, relative_path)
        
        if os.path.exists(full_path):
            return full_path
        
        # 尝试 fallback 路径
        config = self.fixtures_manifest.get('config', {})
        fallbacks = config.get('fallback_paths', [])
        
        for fallback in fallbacks:
            if fallback.startswith('~'):
                fallback = os.path.expanduser(fallback)
            fallback_path = os.path.join(fallback, relative_path)
            if os.path.exists(fallback_path):
                return fallback_path
        
        logger.warning(f"素材文件不存在: {full_path}")
        return None
    
    def _find_fixture(self, fixture_id: str) -> Optional[Dict[str, Any]]:
        """在 manifest 中查找 fixture"""
        fixtures = self.fixtures_manifest.get('fixtures', {})
        
        # 直接查找
        if fixture_id in fixtures:
            fixture = fixtures[fixture_id]
            if 'fixtures' not in fixture:  # 不是 fixture_group
                return fixture
        
        # 在嵌套的 fixtures 组中查找
        for key, value in fixtures.items():
            if isinstance(value, dict) and 'fixtures' in value:
                nested = value.get('fixtures', {})
                if fixture_id in nested:
                    return nested[fixture_id]
        
        return None
    
    def benchmark_open_time(self, video_path: str, result: BenchmarkResult, iterations: int = 5) -> None:
        """
        测量视频打开时间。
        
        从创建 VideoDecoder 到第一帧可用的时间。
        
        Args:
            video_path: 视频文件路径
            result: 结果对象
            iterations: 测试迭代次数
        """
        logger.info(f"  测试打开时间 ({iterations} 次)...")
        
        for i in range(iterations):
            try:
                start = time.perf_counter()
                decoder = VideoDecoder(video_path)
                # 等待 PAUSED 状态，确保解码器就绪
                _ = decoder.get_frame(0)  # 获取第一帧
                end = time.perf_counter()
                
                open_time_ms = (end - start) * 1000
                result.open_times.append(open_time_ms)
                
                # 记录视频属性（第一次）
                if i == 0:
                    result.video_width = decoder.width
                    result.video_height = decoder.height
                    result.video_fps = decoder.fps
                    result.video_total_frames = decoder.total_frames
                    result.video_duration_s = decoder.duration
                
                decoder.release()
                logger.debug(f"    第 {i+1} 次: {open_time_ms:.2f} ms")
                
            except Exception as e:
                result.errors.append(f"打开测试第 {i+1} 次失败: {str(e)}")
                logger.error(f"    第 {i+1} 次失败: {e}")
    
    def benchmark_seek_time(self, video_path: str, result: BenchmarkResult, iterations: int = 10) -> None:
        """
        测量随机 seek 时间。
        
        随机选择帧位置，测量精准定位时间。
        
        Args:
            video_path: 视频文件路径
            result: 结果对象
            iterations: 测试迭代次数
        """
        logger.info(f"  测试 seek 时间 ({iterations} 次)...")
        
        try:
            decoder = VideoDecoder(video_path)
            total_frames = decoder.total_frames
            
            if total_frames <= 0:
                result.errors.append("无法获取总帧数，跳过 seek 测试")
                decoder.release()
                return
            
            # 生成随机 seek 位置（均匀分布）
            import random
            seek_positions = [random.randint(0, total_frames - 1) for _ in range(iterations)]
            result.seek_positions = seek_positions
            
            for pos in seek_positions:
                start = time.perf_counter()
                frame = decoder.get_frame(pos)
                end = time.perf_counter()
                
                if frame is not None:
                    seek_time_ms = (end - start) * 1000
                    result.seek_times.append(seek_time_ms)
                    logger.debug(f"    seek 到帧 {pos}: {seek_time_ms:.2f} ms")
                else:
                    result.errors.append(f"seek 帧 {pos} 失败")
                    logger.warning(f"    seek 帧 {pos} 失败")
            
            decoder.release()
            
        except Exception as e:
            result.errors.append(f"seek 测试失败: {str(e)}")
            logger.error(f"  seek 测试失败: {e}")
    
    def benchmark_sequential_decode(self, video_path: str, result: BenchmarkResult, 
                                     frames: int = 100, batch_size: int = 10) -> None:
        """
        测量顺序解码 fps。
        
        从帧 0 开始连续解码指定帧数，统计解码速度。
        
        Args:
            video_path: 视频文件路径
            result: 结果对象
            frames: 测试帧数
            batch_size: 批量统计 fps 的帧数
        """
        logger.info(f"  测试顺序解码 ({frames} 帧)...")
        
        try:
            decoder = VideoDecoder(video_path)
            
            start_total = time.perf_counter()
            batch_start = start_total
            decoded = 0
            
            for frame_num in range(min(frames, decoder.total_frames)):
                frame = decoder.get_frame(frame_num)
                
                if frame is not None:
                    decoded += 1
                    result.sequential_frames_decoded += 1
                    
                    # 每 batch_size 帧统计一次 fps
                    if decoded % batch_size == 0:
                        batch_end = time.perf_counter()
                        batch_time_s = batch_end - batch_start
                        batch_fps = batch_size / batch_time_s
                        result.sequential_decode_fps.append(batch_fps)
                        logger.debug(f"    帧 {decoded}: {batch_fps:.1f} fps")
                        batch_start = batch_end
                else:
                    result.errors.append(f"帧 {frame_num} 解码失败")
            
            end_total = time.perf_counter()
            result.sequential_total_time_ms = (end_total - start_total) * 1000
            
            decoder.release()
            logger.info(f"    解码完成: {decoded} 帧, {result.sequential_total_time_ms:.2f} ms")
            
        except Exception as e:
            result.errors.append(f"顺序解码测试失败: {str(e)}")
            logger.error(f"  顺序解码测试失败: {e}")
    
    def benchmark_gpu_upload(self, video_path: str, result: BenchmarkResult, frames: int = 30) -> None:
        """
        测量 GPU 上传时间（需要 ModernGL 环境）。
        
        注意：此测试需要真实的 OpenGL 上下文，在无 GUI 环境下可能失败。
        
        Args:
            video_path: 视频文件路径
            result: 结果对象
            frames: 测试帧数
        """
        if not self.config.get('gpu_upload_test', False):
            return
        
        logger.info(f"  测试 GPU 上传 ({frames} 帧)...")
        
        try:
            import moderngl
            import numpy as np
            
            # 创建 standalone context
            ctx = moderngl.create_standalone_context()
            
            decoder = VideoDecoder(video_path)
            
            for frame_num in range(min(frames, decoder.total_frames)):
                frame = decoder.get_frame(frame_num)
                
                if frame is not None:
                    start = time.perf_counter()
                    
                    # 创建纹理并上传
                    texture = ctx.texture(
                        (frame.shape[1], frame.shape[0]),
                        3,
                        frame.tobytes()
                    )
                    
                    end = time.perf_counter()
                    upload_time_ms = (end - start) * 1000
                    result.gpu_upload_times.append(upload_time_ms)
                    
                    texture.release()
                    logger.debug(f"    帧 {frame_num}: {upload_time_ms:.2f} ms")
            
            decoder.release()
            ctx.release()
            
        except ImportError:
            result.errors.append("GPU 上传测试需要 moderngl")
            logger.warning("  GPU 上传测试需要 moderngl，跳过")
        except Exception as e:
            result.errors.append(f"GPU 上传测试失败: {str(e)}")
            logger.error(f"  GPU 上传测试失败: {e}")
    
    def benchmark_playback(self, video_path: str, result: BenchmarkResult, 
                           duration_s: float = 5.0, tolerance_ms: float = 50) -> None:
        """
        测量播放掉帧率。
        
        模拟播放场景，按视频帧率请求帧，统计掉帧情况。
        
        Args:
            video_path: 视频文件路径
            result: 结果对象
            duration_s: 播放测试时长（秒）
            tolerance_ms: 掉帧判定阈值（毫秒）
        """
        logger.info(f"  测试播放 ({duration_s} 秒)...")
        
        try:
            decoder = VideoDecoder(video_path)
            target_fps = decoder.fps
            result.playback_target_fps = target_fps
            
            if target_fps <= 0:
                result.errors.append("无法获取帧率，跳过播放测试")
                decoder.release()
                return
            
            frame_interval_ms = 1000.0 / target_fps
            total_test_frames = int(duration_s * target_fps)
            
            dropped = 0
            decoded = 0
            frame_num = 0
            
            start_time = time.perf_counter()
            
            while frame_num < min(total_test_frames, decoder.total_frames):
                # 计算期望的时间点
                expected_time = frame_num * frame_interval_ms / 1000.0
                
                frame = decoder.get_frame(frame_num)
                actual_time = time.perf_counter() - start_time
                
                if frame is not None:
                    decoded += 1
                    result.playback_total_frames += 1
                    
                    # 检查是否掉帧（实际时间超过期望时间 + tolerance）
                    delay_ms = (actual_time - expected_time) * 1000
                    if delay_ms > tolerance_ms:
                        dropped += 1
                        logger.debug(f"    帧 {frame_num}: 延迟 {delay_ms:.1f} ms (掉帧)")
                else:
                    dropped += 1
                    result.errors.append(f"播放帧 {frame_num} 解码失败")
                
                frame_num += 1
            
            total_time = time.perf_counter() - start_time
            
            result.playback_actual_fps = decoded / total_time if total_time > 0 else 0
            result.playback_dropped_frames = dropped
            result.playback_drop_rate = dropped / decoded if decoded > 0 else 0
            # P10.A: displayed_fps = 实际显示帧数 / 总时间
            result.displayed_fps = (decoded - dropped) / total_time if total_time > 0 else 0
            
            decoder.release()
            
            logger.info(f"    播放完成: {decoded} 帧, {dropped} 掉帧, "
                        f"实际 {result.playback_actual_fps:.1f} fps")
            
        except Exception as e:
            result.errors.append(f"播放测试失败: {str(e)}")
            logger.error(f"  播放测试失败: {e}")

    # ================================================================
    # P10.A 新增测试方法
    # ================================================================

    def benchmark_proxy_decode(self, video_path: str, result: BenchmarkResult,
                                frames: int = 100, scale_factor: float = 0.5,
                                batch_size: int = 10) -> None:
        """
        测量 Proxy 解码 fps（P10.A 指标）。

        使用 FFmpegMediaSource 的 target_size 功能进行降分辨率解码。

        Args:
            video_path: 视频文件路径
            result: 结果对象
            frames: 测试帧数
            scale_factor: 缩放因子（如 0.5 表示宽度/高度减半）
            batch_size: 批量统计 fps 的帧数
        """
        logger.info(f"  测试 Proxy 解码 ({frames} 帧, scale={scale_factor})...")

        try:
            from src.shared.media.ffmpeg_source import FFmpegMediaSource

            source = FFmpegMediaSource(video_path)
            metadata = source.probe()

            if not metadata:
                result.errors.append("无法获取视频 metadata，跳过 Proxy 测试")
                return

            # 计算 proxy 尺寸
            proxy_width = int(metadata.width * scale_factor)
            proxy_height = int(metadata.height * scale_factor)

            result.proxy_enabled = True
            result.proxy_scale_factor = scale_factor

            # 设置 target_size
            source.target_size = (proxy_width, proxy_height)
            source.open()

            start_total = time.perf_counter()
            batch_start = start_total
            decoded = 0

            for frame_num in range(min(frames, metadata.total_frames or frames)):
                try:
                    frame = source.read_next_frame()
                    if frame is not None:
                        decoded += 1
                        result.proxy_frames_decoded += 1

                        # 每 batch_size 帧统计一次 fps
                        if decoded % batch_size == 0:
                            batch_end = time.perf_counter()
                            batch_time_s = batch_end - batch_start
                            batch_fps = batch_size / batch_time_s
                            result.proxy_decode_fps.append(batch_fps)
                            logger.debug(f"    Proxy 帧 {decoded}: {batch_fps:.1f} fps")
                            batch_start = batch_end
                except (StopIteration, EOFError):
                    break

            end_total = time.perf_counter()
            result.proxy_total_time_ms = (end_total - start_total) * 1000

            source.close()
            logger.info(f"    Proxy 解码完成: {decoded} 帧, {result.proxy_total_time_ms:.2f} ms")

        except ImportError:
            result.errors.append("Proxy 测试需要 FFmpegMediaSource")
            logger.warning("  Proxy 测试需要 FFmpegMediaSource，跳过")
        except Exception as e:
            result.errors.append(f"Proxy 解码测试失败: {str(e)}")
            logger.error(f"  Proxy 解码测试失败: {e}")

    def benchmark_render_time(self, video_path: str, result: BenchmarkResult,
                               frames: int = 30) -> None:
        """
        测量渲染时间（P10.A 指标）。

        模拟 GPU 上传 + 简单渲染流程的耗时。

        Args:
            video_path: 视频文件路径
            result: 结果对象
            frames: 测试帧数
        """
        logger.info(f"  测试渲染时间 ({frames} 帧)...")

        try:
            import moderngl

            # 创建 standalone context
            ctx = moderngl.create_standalone_context()

            decoder = VideoDecoder(video_path)

            for frame_num in range(min(frames, decoder.total_frames)):
                frame = decoder.get_frame(frame_num)

                if frame is not None:
                    start = time.perf_counter()

                    # 创建纹理（模拟上传）
                    texture = ctx.texture(
                        (frame.shape[1], frame.shape[0]),
                        3,
                        frame.tobytes()
                    )
                    texture.filter = (moderngl.LINEAR, moderngl.LINEAR)

                    # 简单渲染操作：创建 FBO（模拟渲染）
                    fbo = ctx.framebuffer(color_attachments=[texture])

                    # 记录渲染时间（包括 upload + basic render）
                    end = time.perf_counter()
                    render_time_ms = (end - start) * 1000
                    result.render_times.append(render_time_ms)
                    result.render_frames_count += 1

                    texture.release()
                    logger.debug(f"    帧 {frame_num}: {render_time_ms:.2f} ms")

            decoder.release()
            ctx.release()

            logger.info(f"    渲染测试完成: {result.render_frames_count} 帧")

        except ImportError:
            result.errors.append("渲染测试需要 moderngl")
            logger.warning("  渲染测试需要 moderngl，跳过")
        except Exception as e:
            result.errors.append(f"渲染测试失败: {str(e)}")
            logger.error(f"  渲染测试失败: {e}")

    def track_memory_usage(self, result: BenchmarkResult) -> None:
        """
        追踪内存使用（P10.A 指标）。

        Args:
            result: 结果对象
        """
        if not self.config.get('memory_tracking', True):
            return

        try:
            import psutil

            # 使用 psutil 获取当前进程内存
            process = psutil.Process()
            mem_info = process.memory_info()
            result.memory_mb = mem_info.rss / (1024 * 1024)  # RSS in MB

            logger.debug(f"    内存使用: {result.memory_mb:.1f} MB")

        except ImportError:
            # 如果 psutil 不可用，尝试 tracemalloc
            try:
                import tracemalloc
                tracemalloc.start()
                current, peak = tracemalloc.get_traced_memory()
                result.memory_mb = current / (1024 * 1024)
                result.memory_peak_mb = peak / (1024 * 1024)
                tracemalloc.stop()
            except ImportError:
                result.errors.append("内存追踪需要 psutil 或 tracemalloc")
                logger.warning("  内存追踪需要 psutil/tracemalloc，跳过")

    def track_gpu_texture_count(self, result: BenchmarkResult) -> None:
        """
        追踪 GPU 纹理计数（P10.A 指标）。

        Args:
            result: 结果对象
        """
        if not self.config.get('gpu_texture_tracking', True):
            return

        try:
            from src.shared.video.video_texture_pool import VideoTexturePool
            import moderngl

            # 创建模拟的纹理池来获取统计
            ctx = moderngl.create_standalone_context()
            pool = VideoTexturePool(ctx, pool_size=5)

            stats = pool.get_stats()
            result.gpu_texture_count = stats['pool_size']
            result.gpu_texture_pool_size = stats['max_size']

            pool.release_all()
            ctx.release()

            logger.debug(f"    GPU 纹理池: {result.gpu_texture_count}/{result.gpu_texture_pool_size}")

        except ImportError:
            result.errors.append("GPU 纹理追踪需要 moderngl 和 VideoTexturePool")
            logger.warning("  GPU 纹理追踪需要 moderngl，跳过")
        except Exception as e:
            result.errors.append(f"GPU 纹理追踪失败: {str(e)}")
            logger.error(f"  GPU 纹理追踪失败: {e}")

    def run_p10a_benchmark(self) -> List[BenchmarkResult]:
        """
        运行 P10.A 验收基准测试。

        测试以下素材：
        - 1080p H.264 SDR
        - 4K H.264
        - 4K H.265 10-bit
        - 4K ProRes 422
        - ProRes 4444 alpha
        - DNxHR

        Returns:
            所有测试结果
        """
        logger.info("运行 P10.A 验收基准测试...")

        results = []

        for fixture_id in self.P10A_FIXTURE_IDS:
            video_path = self.get_fixture_path(fixture_id)

            if video_path:
                result = self.run_benchmark(fixture_id, video_path)
                # 追踪内存和 GPU 纹理
                self.track_memory_usage(result)
                self.track_gpu_texture_count(result)
                results.append(result)
            else:
                # 素材不存在时创建 skip 结果
                result = BenchmarkResult(fixture_id)
                result.errors.append(f"P10.A 素材不存在: {fixture_id}")
                logger.warning(f"  P10.A 素材不存在: {fixture_id}")
                results.append(result)

        self.results = results
        return results
    
    def run_benchmark(self, fixture_id: str, video_path: str) -> BenchmarkResult:
        """
        对单个素材运行完整基准测试（包含 P10.A 指标）。
        
        Args:
            fixture_id: 素材 ID
            video_path: 视频文件路径
            
        Returns:
            测试结果
        """
        logger.info(f"基准测试: {fixture_id}")
        logger.info(f"  文件: {video_path}")
        
        result = BenchmarkResult(fixture_id)
        
        # 打开时间测试
        self.benchmark_open_time(video_path, result, 
                                  self.config['open_test_iterations'])
        
        # 如果打开失败，跳过后续测试
        if not result.open_times:
            logger.error(f"  打开测试全部失败，跳过后续测试")
            return result
        
        # seek 时间测试
        self.benchmark_seek_time(video_path, result, 
                                  self.config['seek_test_iterations'])
        
        # 顺序解码测试
        self.benchmark_sequential_decode(video_path, result,
                                             self.config['sequential_test_frames'],
                                             self.config['sequential_batch_size'])
        
        # P10.A: Proxy 解码测试
        self.benchmark_proxy_decode(video_path, result,
                                     self.config['proxy_test_frames'],
                                     self.config['proxy_scale_factor'])
        
        # GPU 上传测试（可选）
        self.benchmark_gpu_upload(video_path, result)
        
        # P10.A: 渲染时间测试（需要 GPU 环境）
        if self.config.get('gpu_upload_test', False):
            self.benchmark_render_time(video_path, result,
                                        self.config['render_test_frames'])
        
        # 播放掉帧测试
        self.benchmark_playback(video_path, result,
                                 self.config['playback_duration_s'],
                                 self.config['playback_tolerance_ms'])
        
        # P10.A: 内存和 GPU 纹理统计
        self.track_memory_usage(result)
        self.track_gpu_texture_count(result)
        
        return result
    def run_all_fixtures(self, group: Optional[str] = None) -> List[BenchmarkResult]:
        """
        对 manifest 中所有素材运行基准测试。
        
        Args:
            group: 可选的素材组名称
            
        Returns:
            所有测试结果
        """
        results = []
        
        if group:
            # 获取指定组的素材 ID
            groups = self.fixtures_manifest.get('fixture_groups', {})
            fixture_ids = groups.get(group, [])
            if not fixture_ids:
                logger.error(f"未找到素材组: {group}")
                return results
        else:
            # 获取所有素材 ID
            fixture_ids = self._get_all_fixture_ids()
        
        for fixture_id in fixture_ids:
            video_path = self.get_fixture_path(fixture_id)
            
            if video_path:
                result = self.run_benchmark(fixture_id, video_path)
                results.append(result)
            else:
                # 创建空结果，记录缺失素材
                result = BenchmarkResult(fixture_id)
                result.errors.append(f"素材文件不存在")
                results.append(result)
        
        self.results = results
        return results
    
    def run_custom_video(self, video_path: str) -> BenchmarkResult:
        """
        对自定义视频文件运行基准测试。
        
        Args:
            video_path: 视频文件路径
            
        Returns:
            测试结果
        """
        fixture_id = os.path.basename(video_path)
        return self.run_benchmark(fixture_id, video_path)
    
    def _get_all_fixture_ids(self) -> List[str]:
        """获取所有素材 ID"""
        fixture_ids = []
        fixtures = self.fixtures_manifest.get('fixtures', {})
        
        for key, value in fixtures.items():
            if isinstance(value, dict):
                if 'fixtures' in value:  # fixture_group
                    nested_ids = list(value.get('fixtures', {}).keys())
                    fixture_ids.extend(nested_ids)
                else:  # single fixture
                    fixture_ids.append(key)
        
        return fixture_ids
    
    def save_results_json(self, output_path: str) -> bool:
        """
        将结果保存为 JSON 文件。
        
        Args:
            output_path: 输出文件路径
            
        Returns:
            是否保存成功
        """
        try:
            data = {
                'benchmark_info': {
                    'timestamp': datetime.now().isoformat(),
                    'config': self.config,
                    'fixtures_path': self.fixtures_path,
                },
                'results': [r.to_dict() for r in self.results]
            }
            
            with open(output_path, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            
            logger.info(f"结果已保存: {output_path}")
            return True
            
        except Exception as e:
            logger.error(f"保存 JSON 失败: {e}")
            return False
    
    def save_results_csv(self, output_path: str) -> bool:
        """
        将结果保存为 CSV 文件（汇总格式）。
        
        Args:
            output_path: 输出文件路径
            
        Returns:
            是否保存成功
        """
        try:
            with open(output_path, 'w', newline='', encoding='utf-8') as f:
                writer = csv.writer(f)
                
                # 写入表头
                writer.writerow([
                    'fixture_id',
                    'video_width',
                    'video_height',
                    'video_fps',
                    'video_total_frames',
                    'video_duration_s',
                    'open_time_mean_ms',
                    'open_time_median_ms',
                    'seek_time_mean_ms',
                    'seek_time_median_ms',
                    'sequential_decode_fps',
                    'sequential_total_time_ms',
                    'gpu_upload_mean_ms',
                    'playback_actual_fps',
                    'playback_drop_rate_percent',
                    'errors'
                ])
                
                # 写入数据
                for r in self.results:
                    writer.writerow([
                        r.fixture_id,
                        r.video_width,
                        r.video_height,
                        r.video_fps,
                        r.video_total_frames,
                        r.video_duration_s,
                        statistics.mean(r.open_times) if r.open_times else '',
                        statistics.median(r.open_times) if r.open_times else '',
                        statistics.mean(r.seek_times) if r.seek_times else '',
                        statistics.median(r.seek_times) if r.seek_times else '',
                        statistics.mean(r.sequential_decode_fps) if r.sequential_decode_fps else '',
                        r.sequential_total_time_ms,
                        statistics.mean(r.gpu_upload_times) if r.gpu_upload_times else '',
                        r.playback_actual_fps,
                        r.playback_drop_rate * 100,
                        '; '.join(r.errors) if r.errors else ''
                    ])
            
            logger.info(f"结果已保存: {output_path}")
            return True
            
        except Exception as e:
            logger.error(f"保存 CSV 失败: {e}")
            return False
    
    def print_summary(self) -> None:
        """打印测试结果摘要（包含 P10.A 指标）"""
        print("\n" + "=" * 80)
        print("Video Pipeline Benchmark Results (P10.A 指标)")
        print("=" * 80)

        # 打印 P10.A 汇总表头
        print("\n--- P10.A 指标汇总 ---")
        print(f"{'Fixture':<25} {'open_ms':>10} {'seek_ms':>10} "
              f"{'seq_fps':>10} {'proxy_fps':>10} {'upload_ms':>10} "
              f"{'render_ms':>10} {'disp_fps':>10} {'drops':>8} "
              f"{'mem_mb':>10} {'gpu_tex':>8}")
        print("-" * 120)

        for r in self.results:
            # 打印 P10.A 汇要行
            summary = r.get_p10a_summary()
            open_ms = summary['open_ms'] if summary['open_ms'] is not None else 'N/A'
            seek_ms = summary['seek_ms'] if summary['seek_ms'] is not None else 'N/A'
            seq_fps = summary['sequential_decode_fps'] if summary['sequential_decode_fps'] is not None else 'N/A'
            proxy_fps = summary['proxy_decode_fps'] if summary['proxy_decode_fps'] is not None else 'N/A'
            upload_ms = summary['upload_ms'] if summary['upload_ms'] is not None else 'N/A'
            render_ms = summary['render_ms'] if summary['render_ms'] is not None else 'N/A'
            disp_fps = summary['displayed_fps'] if summary['displayed_fps'] is not None else 'N/A'
            drops = summary['dropped_frames'] if summary['dropped_frames'] is not None else 'N/A'
            mem_mb = summary['memory_mb'] if summary['memory_mb'] is not None else 'N/A'
            gpu_tex = summary['gpu_texture_count'] if summary['gpu_texture_count'] is not None else 'N/A'

            # 格式化数值
            if isinstance(open_ms, float):
                open_ms = f"{open_ms:.2f}"
            if isinstance(seek_ms, float):
                seek_ms = f"{seek_ms:.2f}"
            if isinstance(seq_fps, float):
                seq_fps = f"{seq_fps:.1f}"
            if isinstance(proxy_fps, float):
                proxy_fps = f"{proxy_fps:.1f}"
            if isinstance(upload_ms, float):
                upload_ms = f"{upload_ms:.2f}"
            if isinstance(render_ms, float):
                render_ms = f"{render_ms:.2f}"
            if isinstance(disp_fps, float):
                disp_fps = f"{disp_fps:.1f}"
            if isinstance(mem_mb, float):
                mem_mb = f"{mem_mb:.1f}"

            print(f"{r.fixture_id:<25} {open_ms:>10} {seek_ms:>10} "
                  f"{seq_fps:>10} {proxy_fps:>10} {upload_ms:>10} "
                  f"{render_ms:>10} {disp_fps:>10} {drops:>8} "
                  f"{mem_mb:>10} {gpu_tex:>8}")

        print("\n--- 详细结果 ---")

        for r in self.results:
            print(f"\n[{r.fixture_id}]")

            if r.errors and not r.open_times:
                print(f"  ERROR: 素材不可用")
                for err in r.errors:
                    print(f"    - {err}")
                continue

            print(f"  视频: {r.video_width}x{r.video_height} @ {r.video_fps:.2f} fps, "
                  f"{r.video_total_frames} 帧, {r.video_duration_s:.1f}s")

            if r.open_times:
                print(f"  打开时间: {statistics.median(r.open_times):.2f} ms "
                      f"(min={min(r.open_times):.2f}, max={max(r.open_times):.2f})")

            if r.seek_times:
                print(f"  seek 时间: {statistics.median(r.seek_times):.2f} ms "
                      f"(min={min(r.seek_times):.2f}, max={max(r.seek_times):.2f})")

            if r.sequential_decode_fps:
                print(f"  顺序解码: {statistics.mean(r.sequential_decode_fps):.1f} fps "
                      f"({r.sequential_frames_decoded} 帧, {r.sequential_total_time_ms:.0f} ms)")

            # P10.A: Proxy 解码结果
            if r.proxy_decode_fps:
                print(f"  Proxy 解码: {statistics.mean(r.proxy_decode_fps):.1f} fps "
                      f"(scale={r.proxy_scale_factor}, {r.proxy_frames_decoded} 帧)")

            if r.gpu_upload_times:
                print(f"  GPU 上传: {statistics.mean(r.gpu_upload_times):.2f} ms")

            # P10.A: 渲染时间结果
            if r.render_times:
                print(f"  渲染时间: {statistics.median(r.render_times):.2f} ms "
                      f"(frames={r.render_frames_count})")

            if r.playback_total_frames > 0:
                print(f"  播放: 实际 {r.playback_actual_fps:.1f} fps, "
                      f"显示 {r.displayed_fps:.1f} fps, "
                      f"掉帧 {r.playback_drop_rate*100:.1f}% "
                      f"({r.playback_dropped_frames}/{r.playback_total_frames})")

            # P10.A: 内存和 GPU 纹理统计
            if r.memory_mb > 0:
                print(f"  内存: {r.memory_mb:.1f} MB (peak={r.memory_peak_mb:.1f})")
            if r.gpu_texture_count > 0:
                print(f"  GPU 纹理池: {r.gpu_texture_count}/{r.gpu_texture_pool_size}")

            if r.errors:
                print(f"  警告:")
                for err in r.errors:
                    print(f"    - {err}")

        print("\n" + "=" * 80)


def main():
    """主函数"""
    parser = argparse.ArgumentParser(
        description='Topos Color Video Pipeline Performance Benchmark'
    )
    
    parser.add_argument(
        '--fixture', '-f',
        type=str,
        help='测试单个素材（fixture ID）'
    )
    
    parser.add_argument(
        '--all', '-a',
        action='store_true',
        help='测试所有素材'
    )
    
    parser.add_argument(
        '--group', '-g',
        type=str,
        choices=['basic', 'professional', 'hdr', 'fps_test', 'color_test', 'stress'],
        help='测试指定素材组'
    )
    
    parser.add_argument(
        '--custom', '-c',
        type=str,
        help='测试自定义视频文件路径'
    )
    
    parser.add_argument(
        '--manifest', '-m',
        type=str,
        default='tests/media/fixtures_manifest.json',
        help='manifest 文件路径'
    )
    
    parser.add_argument(
        '--fixtures-path',
        type=str,
        help='测试素材路径（覆盖默认）'
    )
    
    parser.add_argument(
        '--output', '-o',
        type=str,
        choices=['json', 'csv', 'both', 'none'],
        default='json',
        help='输出格式'
    )
    
    parser.add_argument(
        '--output-path',
        type=str,
        default=None,
        help='输出文件路径（自动生成则不指定）'
    )
    
    parser.add_argument(
        '--iterations-open',
        type=int,
        default=5,
        help='打开测试迭代次数'
    )
    
    parser.add_argument(
        '--iterations-seek',
        type=int,
        default=10,
        help='seek 测试迭代次数'
    )
    
    parser.add_argument(
        '--sequential-frames',
        type=int,
        default=100,
        help='顺序解码测试帧数'
    )
    
    parser.add_argument(
        '--playback-duration',
        type=float,
        default=5.0,
        help='播放测试时长（秒）'
    )
    
    parser.add_argument(
        '--gpu-upload',
        action='store_true',
        help='启用 GPU 上传测试'
    )

    parser.add_argument(
        '--p10a',
        action='store_true',
        help='运行 P10.A 验收基准测试'
    )

    parser.add_argument(
        '--quiet', '-q',
        action='store_true',
        help='安静模式，只输出结果'
    )
    
    args = parser.parse_args()
    
    if args.quiet:
        logging.getLogger().setLevel(logging.WARNING)
    
    # 创建基准测试器
    benchmark = VideoPipelineBenchmark(args.fixtures_path)
    
    # 加载 manifest
    manifest_path = args.manifest
    if not os.path.isabs(manifest_path):
        # 相对路径转换为项目根目录下的绝对路径
        project_root = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
        manifest_path = os.path.join(project_root, manifest_path)
    
    if not benchmark.load_manifest(manifest_path):
        print(f"错误: 无法加载 manifest: {manifest_path}")
        sys.exit(1)
    
    # 配置测试参数
    benchmark.config['open_test_iterations'] = args.iterations_open
    benchmark.config['seek_test_iterations'] = args.iterations_seek
    benchmark.config['sequential_test_frames'] = args.sequential_frames
    benchmark.config['playback_duration_s'] = args.playback_duration
    benchmark.config['gpu_upload_test'] = args.gpu_upload
    
    # 运行测试
    if args.custom:
        result = benchmark.run_custom_video(args.custom)
        benchmark.results = [result]
    elif args.fixture:
        video_path = benchmark.get_fixture_path(args.fixture)
        if video_path:
            result = benchmark.run_benchmark(args.fixture, video_path)
            benchmark.results = [result]
        else:
            print(f"错误: 素材 {args.fixture} 文件不存在")
            sys.exit(1)
    elif args.p10a:
        benchmark.run_p10a_benchmark()
    elif args.all or args.group:
        benchmark.run_all_fixtures(args.group)
    else:
        parser.print_help()
        sys.exit(0)
    
    # 打印摘要
    benchmark.print_summary()
    
    # 保存结果
    if args.output != 'none':
        # 确定输出路径
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        
        if args.output_path:
            base_path = args.output_path
        else:
            # 默认输出到 tests/media/results/
            project_root = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
            results_dir = os.path.join(project_root, 'tests', 'media', 'results')
            os.makedirs(results_dir, exist_ok=True)
            base_path = os.path.join(results_dir, f'benchmark_{timestamp}')
        
        if args.output in ['json', 'both']:
            benchmark.save_results_json(f'{base_path}.json')
        
        if args.output in ['csv', 'both']:
            benchmark.save_results_csv(f'{base_path}.csv')


if __name__ == '__main__':
    main()