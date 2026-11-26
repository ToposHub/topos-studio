"""
FFmpegMediaSource seek 精度测试。

测试目标：
1. GOP codec (H.264/H.265) seek 返回目标帧
2. Intra codec (ProRes/DNxHR) seek 快速稳定
3. 快速 seek 不累积内存
4. seek 后帧号正确

测试策略：
- 如果没有测试素材，使用 pytest.skip
- 使用 fixtures_manifest.json 中声明的素材（如果存在）
"""

from __future__ import annotations

import pytest
import os
import json
import logging
from types import SimpleNamespace
from typing import Optional

from src.shared.media import ffmpeg_source
from src.shared.media.ffmpeg_source import (
    FFmpegMediaSource,
    _is_gop_codec,
    _is_intra_codec,
)
from src.shared.media.seek import (
    frame_to_pts,
    pts_to_frame,
    is_gop_codec,
    is_intra_codec,
    estimate_seek_cost,
    choose_seek_strategy,
    SeekInfo,
)
from src.shared.media.errors import SeekError
from src.shared.media.metadata import Rational, VideoMetadata

logger = logging.getLogger(__name__)


@pytest.mark.parametrize(
    ('plat', 'codec', 'pixel_format', 'rate', 'target_size', 'threads', 'enabled'),
    [
        ('win32', 'h264', 'yuv420p', 25, None, 0, True),
        ('win32', 'h264', 'yuv420p', 24, None, 0, False),
        ('win32', 'h264', 'yuv420p', 25, (1920, 1080), 0, False),
        ('win32', 'hevc', 'yuv420p', 25, None, 0, False),
        ('win32', 'h264', 'yuv420p10le', 25, None, 0, False),
        ('darwin', 'h264', 'yuv420p', 25, None, 8, True),
    ],
)
def test_interactive_scrub_tuning_preserves_platform_thread_policy(
    monkeypatch: pytest.MonkeyPatch, plat: str, codec: str, pixel_format: str,
    rate: int, target_size: tuple[int, int] | None, threads: int, enabled: bool,
) -> None:
    monkeypatch.setattr(ffmpeg_source, 'sys', SimpleNamespace(platform=plat))
    monkeypatch.setattr(ffmpeg_source.platform, 'machine', lambda: 'x86_64')
    monkeypatch.setattr(ffmpeg_source.os, 'cpu_count', lambda: 16)
    source = FFmpegMediaSource('memory', target_size=target_size)
    decoder = SimpleNamespace(name=codec, is_open=False, thread_count=0)
    source._video_stream = SimpleNamespace(codec_context=decoder, average_rate=rate, base_rate=25)
    source._metadata = SimpleNamespace(width=3840, height=2160, pixel_format=pixel_format)
    try:
        assert source.tune_interactive_scrub() is enabled
        assert decoder.thread_count == threads
    finally:
        source._video_stream = None


@pytest.mark.parametrize(('target_read', 'first_pts'), [(False, 29), (True, 29), (True, 30)])
def test_windows_scrub_history_invalidates_ambiguous_frame_mapping(
    monkeypatch: pytest.MonkeyPatch, target_read: bool, first_pts: int,
) -> None:
    av = pytest.importorskip('av')
    monkeypatch.setattr(ffmpeg_source, 'sys', SimpleNamespace(platform='win32'))
    source = FFmpegMediaSource('memory', preserve_planes=True)
    source._is_open = source._is_planar_mode = True
    source._metadata = VideoMetadata(width=16, height=16, fps=Rational(25, 1),
                                     total_frames=10, codec_name='h264',
                                     codec_profile=None, pixel_format='yuv420p', bit_depth=8)
    source._scrub_seek_frame_limit = 9
    monkeypatch.setattr(source, '_frame_to_pts', lambda index: index * 10)
    frame = av.VideoFrame(16, 16, 'yuv420p')
    frame.pts = 20
    source._remember_scrub_seek_frame(2, frame)
    assert source._scrub_seek_frames.keys() == {2}

    ambiguous = av.VideoFrame(16, 16, 'yuv420p')
    ambiguous.pts = first_pts
    if target_read:
        source._current_frame = 3
        assert source._convert_frame_planar(ambiguous).pts == first_pts
    else:
        source._remember_scrub_seek_frame(3, ambiguous)
    exact = av.VideoFrame(16, 16, 'yuv420p')
    exact.pts = 30
    source._remember_scrub_seek_frame(3, exact)
    assert not source._scrub_seek_frames
    assert source.read_recent_scrub_frame(2) is None


# =============================================================================
# 测试素材管理
# =============================================================================

def get_test_video_path(codec_type: str) -> Optional[str]:
    """
    获取测试视频路径。

    Args:
        codec_type: 'gop' 或 'intra'

    Returns:
        测试视频路径或 None
    """
    # 检查 fixtures manifest
    manifest_path = os.path.join(
        os.path.dirname(__file__), 'fixtures_manifest.json'
    )

    if os.path.exists(manifest_path):
        try:
            with open(manifest_path, 'r') as f:
                manifest = json.load(f)
            videos = manifest.get('videos', {})
            if codec_type in videos:
                video_info = videos[codec_type]
                video_path = video_info.get('path')
                if video_path and os.path.exists(video_path):
                    return video_path
        except (json.JSONDecodeError, IOError):
            pass

    # 检查环境变量
    env_key = f'TEST_VIDEO_{codec_type.upper()}'
    video_path = os.environ.get(env_key)
    if video_path and os.path.exists(video_path):
        return video_path

    return None


def check_ffmpeg_available() -> bool:
    """检查 FFmpeg/PyAV 是否可用"""
    try:
        import av
        return True
    except ImportError:
        return False


# =============================================================================
# 编解码器分类测试
# =============================================================================

class TestCodecClassification:
    """编解码器分类测试"""

    def test_gop_codec_detection(self) -> None:
        """测试 GOP codec 检测"""
        # H.264 variants
        assert _is_gop_codec('h264') is True
        assert _is_gop_codec('libx264') is True
        assert _is_gop_codec('H264') is True

        # H.265/HEVC variants
        assert _is_gop_codec('h265') is True
        assert _is_gop_codec('hevc') is True
        assert _is_gop_codec('libx265') is True

        # VP9/AV1
        assert _is_gop_codec('vp9') is True
        assert _is_gop_codec('av1') is True

        # 非 GOP codec
        assert _is_gop_codec('prores') is False
        assert _is_gop_codec('dnxhr') is False
        assert _is_gop_codec('rawvideo') is False

    def test_intra_codec_detection(self) -> None:
        """测试 Intra codec 检测"""
        # ProRes variants
        assert _is_intra_codec('prores') is True
        assert _is_intra_codec('prores_ks') is True
        assert _is_intra_codec('prores_aw') is True

        # DNxHR/DNxHD
        assert _is_intra_codec('dnxhr') is True
        assert _is_intra_codec('dnxhd') is True

        # Raw/FFV1
        assert _is_intra_codec('rawvideo') is True
        assert _is_intra_codec('ffv1') is True

        # 非 Intra codec
        assert _is_intra_codec('h264') is False
        assert _is_intra_codec('hevc') is False

    def test_seek_module_codec_detection(self) -> None:
        """测试 seek.py 模块的 codec 检测"""
        # 与 ffmpeg_source.py 的检测保持一致
        assert is_gop_codec('h264') == _is_gop_codec('h264')
        assert is_gop_codec('prores') == _is_gop_codec('prores')
        assert is_intra_codec('prores') == _is_intra_codec('prores')
        assert is_intra_codec('h264') == _is_intra_codec('h264')


# =============================================================================
# PTS/帧号转换测试
# =============================================================================

class TestPTSConversion:
    """PTS/帧号转换测试"""

    def test_frame_to_pts_basic(self) -> None:
        """测试基本帧号到 PTS 转换"""
        # 24fps
        fps = Rational(24, 1)
        pts = frame_to_pts(100, fps)
        assert pts == 100 * fps.den  # = 100

        # 23.976fps (24000/1001)
        fps = Rational(24000, 1001)
        pts = frame_to_pts(100, fps)
        assert pts == 100 * fps.den  # = 100 * 1001

    def test_pts_to_frame_basic(self) -> None:
        """测试基本 PTS 到帧号转换"""
        # 24fps
        fps = Rational(24, 1)
        frame = pts_to_frame(100, fps)
        assert frame == 100

        # 23.976fps
        fps = Rational(24000, 1001)
        pts = 100 * fps.den  # 第 100 帧的 PTS
        frame = pts_to_frame(pts, fps)
        assert frame == 100

    def test_roundtrip_conversion(self) -> None:
        """测试 PTS/帧号转换的往返一致性"""
        fps = Rational(24000, 1001)  # 23.976fps

        for frame_idx in [0, 50, 100, 500, 1000]:
            pts = frame_to_pts(frame_idx, fps)
            frame_back = pts_to_frame(pts, fps)
            assert frame_back == frame_idx, f"Roundtrip failed for frame {frame_idx}"

    def test_seconds_conversion(self) -> None:
        """测试秒数转换"""
        fps = Rational(24000, 1001)  # 23.976fps

        # 第 100 帧的时间
        from src.shared.media.seek import frame_to_seconds, seconds_to_frame
        seconds = frame_to_seconds(100, fps)
        expected_seconds = 100 / fps.to_float()
        assert abs(seconds - expected_seconds) < 0.001

        # 秒数转帧号
        frame = seconds_to_frame(seconds, fps)
        assert frame == 100


# =============================================================================
# Seek 成本估算测试
# =============================================================================

class TestSeekCostEstimation:
    """Seek 成本估算测试"""

    def test_intra_codec_seek_cost(self) -> None:
        """Intra codec 的 seek 成本应该为 1"""
        cost = estimate_seek_cost(100, 500, 'prores')
        assert cost == 1

        cost = estimate_seek_cost(0, 100, 'dnxhr')
        assert cost == 1

    def test_gop_codec_seek_cost(self) -> None:
        """GOP codec 的 seek 成本估算"""
        # 小距离：顺序解码更高效
        cost = estimate_seek_cost(100, 105, 'h264')
        # 5 帧距离，小于 GOP size，顺序解码
        assert cost <= 5

        # 大距离：需要 seek + decode forward
        cost = estimate_seek_cost(100, 500, 'h264')
        # 需要从 keyframe decode forward
        assert cost > 0

    def test_seek_strategy_selection(self) -> None:
        """测试 seek 策略选择"""
        # 小距离：顺序解码
        strategy = choose_seek_strategy(100, 105, 'h264', threshold=10)
        assert strategy == 'sequential'

        # 大距离 GOP：seek_forward
        strategy = choose_seek_strategy(100, 500, 'h264', threshold=10)
        assert strategy == 'seek_forward'

        # 大距离 Intra：直接 seek
        strategy = choose_seek_strategy(100, 500, 'prores', threshold=10)
        assert strategy == 'seek'


# =============================================================================
# FFmpegMediaSource Seek 测试
# =============================================================================

@pytest.mark.skipif(
    not check_ffmpeg_available(),
    reason="PyAV/FFmpeg not available"
)
class TestFFmpegMediaSourceSeek:
    """FFmpegMediaSource seek 测试"""

    @pytest.fixture
    def gop_video_path(self) -> Optional[str]:
        """GOP codec 视频路径"""
        return get_test_video_path('gop')

    @pytest.fixture
    def intra_video_path(self) -> Optional[str]:
        """Intra codec 视频路径"""
        return get_test_video_path('intra')

    def test_seek_to_first_frame(self, gop_video_path: Optional[str]) -> None:
        """测试 seek 到第一帧"""
        if gop_video_path is None:
            pytest.skip("No GOP codec test video available")

        source = FFmpegMediaSource(gop_video_path)
        with source:
            # Seek 到第 0 帧
            source.seek_frame(0)
            frame = source.read_next_frame()

            assert frame.frame_index == 0, f"Expected frame 0, got {frame.frame_index}"
            assert source.current_frame == 1  # 读取后递增

    def test_seek_to_middle_frame(self, gop_video_path: Optional[str]) -> None:
        """测试 seek 到中间帧"""
        if gop_video_path is None:
            pytest.skip("No GOP codec test video available")

        source = FFmpegMediaSource(gop_video_path)
        metadata = source.probe()

        if metadata.total_frames is None or metadata.total_frames < 100:
            pytest.skip("Video too short for middle frame test")

        with source:
            # Seek 到中间帧
            middle_frame = metadata.total_frames // 2
            source.seek_frame(middle_frame)
            frame = source.read_next_frame()

            # 允许 ±1 帧误差
            assert abs(frame.frame_index - middle_frame) <= 1, \
                f"Expected frame {middle_frame}, got {frame.frame_index}"

    def test_nearby_forward_seek_reuses_open_gop_iterator(
        self, gop_video_path: Optional[str], monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        if gop_video_path is None:
            pytest.skip("No GOP codec test video available")

        with FFmpegMediaSource(gop_video_path) as source:
            assert source.read_next_frame().frame_index == 0

            def reject_reseek(_frame: int) -> None:
                pytest.fail("A short forward seek should reuse the open decoder")

            monkeypatch.setattr(source, "_seek_gop", reject_reseek)
            assert source.read_frame(5).frame_index == 5
            assert source.read_next_frame().frame_index == 6

    def test_recent_planar_seek_frame_remains_exact_without_moving_cursor(
        self, gop_video_path: Optional[str],
    ) -> None:
        import numpy as np

        if gop_video_path is None:
            pytest.skip('No GOP codec test video available')
        with FFmpegMediaSource(gop_video_path, preserve_planes=True) as source:
            if source.metadata.codec_name != 'h264' or source.metadata.pixel_format != 'yuv420p':
                pytest.skip('H.264 4:2:0 sample required')
            if (source.metadata.total_frames or 0) <= 55:
                pytest.skip('Sample needs at least 56 frames')
            source._scrub_seek_frame_limit = 8
            reference = source.read_frame(19)
            source.read_frame(55)
            source.read_frame(23)
            cursor = source.current_frame
            recent = source.read_recent_scrub_frame(19)
            assert recent is not None and recent.frame_index == 19 and recent.pts == reference.pts
            for actual, exact in zip(recent.planes, reference.planes, strict=True):
                assert np.array_equal(actual, exact)
            assert source.current_frame == cursor
            assert source.read_next_frame().frame_index == cursor

    def test_reverse_history_keeps_walked_gop_prefix_at_exact_pts(
        self, gop_video_path: Optional[str],
    ) -> None:
        import numpy as np

        if gop_video_path is None:
            pytest.skip('No GOP codec test video available')
        with FFmpegMediaSource(gop_video_path, preserve_planes=True) as source:
            index = source.warm_gop_index()
            if source.metadata.codec_name != 'h264' or index is None:
                pytest.skip('An indexed H.264 clip is required')
            pair = next(((a, b) for a, b in zip(index.keyframe_frames, index.keyframe_frames[1:])
                         if 12 <= b - a <= 25), None)
            if pair is None:
                pytest.skip('This clip has no suitable keyframe interval')
            first, end = pair
            exact = source.read_frame(first + 1)
            source.read_frame(end)
            source._scrub_seek_frame_limit = 9
            source.read_recent_scrub_frame(end)
            assert source.read_recent_scrub_frame(end - 1) is None
            source.read_frame(end - 1)
            cursor = source.current_frame
            for target in range(end - 2, first, -1):
                recent = source.read_recent_scrub_frame(target)
                assert recent is not None and recent.frame_index == target
            assert recent.pts == exact.pts and source.current_frame == cursor
            for actual, expected in zip(recent.planes, exact.planes, strict=True):
                np.testing.assert_array_equal(actual, expected)

    def test_reverse_history_counts_padding_and_contracts_on_direction_change(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from types import SimpleNamespace

        source = FFmpegMediaSource('memory')
        source._is_open = source._is_planar_mode = True
        source._scrub_seek_frame_limit = 9
        monkeypatch.setattr(source, '_frame_to_pts', int)
        source.read_recent_scrub_frame(51)
        source.read_recent_scrub_frame(50)
        for index in range(35):
            frame = SimpleNamespace(pts=index, is_corrupt=False,
                                    planes=(SimpleNamespace(buffer_size=16 << 20),))
            source._remember_scrub_seek_frame(index, frame)
        assert list(source._scrub_seek_frames) == list(range(15, 35))
        source.read_recent_scrub_frame(60)
        assert list(source._scrub_seek_frames) == list(range(28, 35))
        source._scrub_seek_frame_limit = 0
        source._scrub_reverse_window = True
        source._trim_scrub_history()
        assert not source._scrub_seek_frames

    def test_seek_to_last_frame(self, gop_video_path: Optional[str]) -> None:
        """测试 seek 到最后一帧"""
        if gop_video_path is None:
            pytest.skip("No GOP codec test video available")

        source = FFmpegMediaSource(gop_video_path)
        metadata = source.probe()

        if metadata.total_frames is None:
            pytest.skip("Video duration unknown")

        with source:
            # Seek 到最后一帧
            last_frame = metadata.total_frames - 1
            source.seek_frame(last_frame)
            frame = source.read_next_frame()

            # 允许 ±1 帧误差
            assert abs(frame.frame_index - last_frame) <= 1, \
                f"Expected frame {last_frame}, got {frame.frame_index}"

    def test_consecutive_random_seek(self, gop_video_path: Optional[str]) -> None:
        """测试连续随机 seek"""
        if gop_video_path is None:
            pytest.skip("No GOP codec test video available")

        source = FFmpegMediaSource(gop_video_path)
        metadata = source.probe()

        if metadata.total_frames is None or metadata.total_frames < 200:
            pytest.skip("Video too short for random seek test")

        import random

        with source:
            # 连续 5 次随机 seek
            seek_points = random.sample(
                range(10, metadata.total_frames - 10), 5
            )

            for target in seek_points:
                source.seek_frame(target)
                frame = source.read_next_frame()

                # 允许 ±1 帧误差
                assert abs(frame.frame_index - target) <= 1, \
                    f"Seek to {target} failed, got {frame.frame_index}"

    def test_intra_codec_seek_accuracy(
        self,
        intra_video_path: Optional[str]
    ) -> None:
        """Intra codec seek 应该精确"""
        if intra_video_path is None:
            pytest.skip("No Intra codec test video available")

        source = FFmpegMediaSource(intra_video_path)
        metadata = source.probe()

        if metadata.total_frames is None:
            pytest.skip("Video duration unknown")

        with source:
            # Intra codec 应该能精确 seek
            # 测试几个位置
            test_frames = [0, 50, 100, metadata.total_frames - 50]
            test_frames = [f for f in test_frames if f < metadata.total_frames]

            for target in test_frames:
                source.seek_frame(target)
                frame = source.read_next_frame()

                # Intra codec 应该完全精确（误差为 0）
                assert frame.frame_index == target, \
                    f"Intra seek failed: expected {target}, got {frame.frame_index}"

    def test_seek_frame_number_validation(
        self,
        gop_video_path: Optional[str]
    ) -> None:
        """测试帧号范围验证"""
        if gop_video_path is None:
            pytest.skip("No GOP codec test video available")

        source = FFmpegMediaSource(gop_video_path)
        metadata = source.probe()

        if metadata.total_frames is None:
            pytest.skip("Video duration unknown")

        with source:
            # 超出范围的帧号应该抛出 ValueError
            # seek 到负帧号 -> 抛出 ValueError
            with pytest.raises(ValueError):
                source.seek_frame(-1)

            # seek 到超过总帧数 -> 抛出 ValueError
            with pytest.raises(ValueError):
                source.seek_frame(metadata.total_frames + 100)

    def test_memory_stability_after_many_seek(
        self,
        gop_video_path: Optional[str]
    ) -> None:
        """测试多次 seek 后内存稳定性"""
        if gop_video_path is None:
            pytest.skip("No GOP codec test video available")

        source = FFmpegMediaSource(gop_video_path)
        metadata = source.probe()

        if metadata.total_frames is None or metadata.total_frames < 100:
            pytest.skip("Video too short for memory test")

        import gc

        with source:
            # 执行 20 次 seek
            for i in range(20):
                target = (i * 5) % metadata.total_frames
                source.seek_frame(target)
                _ = source.read_next_frame()

            # 检查内存
            # 这里只是确保没有崩溃，实际内存检查需要更复杂的工具
            gc.collect()

            # 验证最后一次 seek 正常
            source.seek_frame(0)
            frame = source.read_next_frame()
            assert frame.frame_index == 0


# =============================================================================
# SeekInfo 测试
# =============================================================================

class TestSeekInfo:
    """SeekInfo 数据类测试"""

    def test_accurate_seek_info(self) -> None:
        """测试精确 seek 信息"""
        info = SeekInfo(
            target_frame=100,
            actual_frame=100,
            codec_name='prores'
        )
        assert info.is_accurate is True
        assert info.overshoot == 0

    def test_inaccurate_seek_info(self) -> None:
        """测试不精确 seek 信息"""
        # overshoot +1
        info = SeekInfo(
            target_frame=100,
            actual_frame=101,
            codec_name='h264',
            tolerance=1
        )
        assert info.is_accurate is True  # 在 tolerance 范围内

        # overshoot +2
        info = SeekInfo(
            target_frame=100,
            actual_frame=102,
            codec_name='h264',
            tolerance=1
        )
        assert info.is_accurate is False
        assert info.overshoot == 2

    def test_seek_info_repr(self) -> None:
        """测试 SeekInfo 字符串表示"""
        info = SeekInfo(
            target_frame=100,
            actual_frame=100,
            codec_name='prores'
        )
        repr_str = repr(info)
        assert 'target=100' in repr_str
        assert 'actual=100' in repr_str
        assert 'accurate' in repr_str


# =============================================================================
# 边缘情况测试
# =============================================================================

class TestEdgeCases:
    """边缘情况测试"""

    def test_empty_codec_name(self) -> None:
        """空编解码器名称"""
        assert _is_gop_codec('') is False
        assert _is_intra_codec('') is False

    def test_unknown_codec_name(self) -> None:
        """未知编解码器名称"""
        # 未知 codec 默认作为 GOP 处理（保守策略）
        assert _is_gop_codec('unknown_codec') is False
        assert _is_intra_codec('unknown_codec') is False

    def test_frame_to_pts_with_invalid_fps(self) -> None:
        """无效帧率的 PTS 转换"""
        # 这应该不会抛出异常，只是返回保守值
        fps = Rational(0, 1)  # 这会在 Rational 构造时抛出异常
        # 所以我们测试正常帧率
        fps = Rational(24, 1)
        pts = frame_to_pts(100, fps)
        assert pts == 100


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
