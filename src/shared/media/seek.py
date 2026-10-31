"""
Seek 辅助模块。

提供帧定位相关的辅助函数：
- keyframe 查找
- PTS/帧号转换
- seek 策略判断

## 职责

- find_keyframe_before: 找到目标 PTS 之前的最近 keyframe PTS
- frame_to_pts: 帧号转换为 PTS
- pts_to_frame: PTS 转换为帧号
- estimate_seek_cost: 估算 seek 到目标帧需要的解码帧数

## 设计原则

- 线程安全：纯函数，无状态
- 完整类型注解
- 与 FFmpegMediaSource 配合使用
"""

from __future__ import annotations

import logging
from typing import Optional, Any

from .metadata import Rational

logger = logging.getLogger(__name__)


# =============================================================================
# 编解码器分类
# =============================================================================

# GOP-based codecs
GOP_CODECS: frozenset[str] = frozenset({
    'h264', 'h265', 'hevc', 'mpeg4', 'mpeg2video', 'mpeg1video',
    'vp8', 'vp9', 'av1', 'libx264', 'libx265', 'libvpx', 'libvpx-vp9'
})

# Intra-frame codecs
INTRA_CODECS: frozenset[str] = frozenset({
    'prores', 'prores_ks', 'prores_aw', 'dnxhr', 'dnxhd',
    'dvvideo', 'dv', 'ffv1', 'utvideo', 'huffyuv', 'lagarith',
    'magicyuv', 'rawvideo', 'yuv4'
})


def is_gop_codec(codec_name: str) -> bool:
    """
    判断是否为 GOP 编解码器。

    GOP codec 需要先 seek 到 keyframe，然后 decode forward 到目标帧。

    Args:
        codec_name: 编解码器名称

    Returns:
        GOP codec 返回 True
    """
    name_lower = codec_name.lower()
    return name_lower in GOP_CODECS or any(
        c in name_lower for c in ('h264', 'h265', 'hevc', 'vp9', 'av1')
    )


def is_intra_codec(codec_name: str) -> bool:
    """
    判断是否为 Intra-frame 编解码器。

    Intra codec 每帧都是关键帧，可以直接 seek 到任意帧。

    Args:
        codec_name: 编解码器名称

    Returns:
        Intra codec 返回 True
    """
    name_lower = codec_name.lower()
    return name_lower in INTRA_CODECS or any(
        c in name_lower for c in ('prores', 'dnx', 'raw', 'ffv1')
    )


# =============================================================================
# PTS/帧号转换
# =============================================================================

def frame_to_pts(
    frame_index: int,
    fps: Rational,
    time_base: Optional[Any] = None
) -> int:
    """
    将帧号转换为 PTS。

    PTS (Presentation Time Stamp) 是视频帧的时间戳单位，
    与 time_base 相关。time_base 通常是一个分数（如 1/1000）。

    Args:
        frame_index: 帧号（从 0 开始）
        fps: 帧率（Rational）
        time_base: PyAV 时间基（Fraction），为 None 时使用帧率倒数

    Returns:
        PTS 值（整数）

    Examples:
        >>> fps = Rational(24000, 1001)  # 23.976fps
        >>> frame_to_pts(100, fps)  # 第 100 帧 PTS（time_base 为 None 时
        ...                          # PTS = frame_index * fps.den）
        100100
    """
    if time_base is None:
        # 使用帧率倒数作为时间基
        # PTS = frame_index * fps.den
        return frame_index * fps.den

    # time_base 是 Fraction，有 numerator 和 denominator
    # PTS = frame_index / fps / time_base
    # = frame_index * fps.den / fps.num * time_base.den / time_base.num
    try:
        tb_num = time_base.numerator if hasattr(time_base, 'numerator') else 1
        tb_den = time_base.denominator if hasattr(time_base, 'denominator') else 1

        pts_per_frame = fps.den * tb_den / (fps.num * tb_num)
        return int(frame_index * pts_per_frame)
    except (AttributeError, TypeError):
        # Fallback
        return frame_index * fps.den


def pts_to_frame(
    pts: int,
    fps: Rational,
    time_base: Optional[Any] = None
) -> int:
    """
    将 PTS 转换为帧号。

    Args:
        pts: PTS 值
        fps: 帧率（Rational）
        time_base: PyAV 时间基（Fraction），为 None 时使用帧率倒数

    Returns:
        帧号（整数，向下取整）

    Examples:
        >>> fps = Rational(24000, 1001)  # 23.976fps
        >>> pts_to_frame(100 * fps.den, fps)
        100
    """
    if time_base is None:
        # 使用帧率倒数作为时间基
        return pts // fps.den

    try:
        tb_num = time_base.numerator if hasattr(time_base, 'numerator') else 1
        tb_den = time_base.denominator if hasattr(time_base, 'denominator') else 1

        pts_per_frame = fps.den * tb_den / (fps.num * tb_num)
        frame = int(pts / pts_per_frame)
        return max(0, frame)
    except (AttributeError, TypeError):
        # Fallback
        return pts // fps.den


def frame_to_seconds(frame_index: int, fps: Rational) -> float:
    """
    将帧号转换为秒数。

    Args:
        frame_index: 帧号
        fps: 帧率

    Returns:
        秒数
    """
    return frame_index / fps.to_float()


def seconds_to_frame(seconds: float, fps: Rational) -> int:
    """
    将秒数转换为帧号。

    Args:
        seconds: 秒数
        fps: 帧率

    Returns:
        帧号（向下取整）
    """
    return int(seconds * fps.to_float())


# =============================================================================
# Keyframe 查找
# =============================================================================

def find_keyframe_before(
    target_pts: int,
    stream: Any
) -> Optional[int]:
    """
    找到目标 PTS 之前的最近 keyframe PTS。

    注意：此函数需要 PyAV stream 的 index 支持。
    对于大多数视频，keyframe index 可能不完整或不存在。

    Args:
        target_pts: 目标 PTS
        stream: PyAV VideoStream

    Returns:
        Keyframe PTS 或 None（无法确定）
    """
    # 检查 stream 是否有 keyframe 信息
    # PyAV 的 keyframe 信息通常需要通过 side_data 或 packet 获取

    # 方式 1: 检查 stream 的 side_data
    if hasattr(stream, 'side_data'):
        for sd in stream.side_data:
            if hasattr(sd, 'type') and sd.type == 'keyframe':
                # 有 keyframe side_data
                # 这里需要更复杂的解析
                pass

    # 方式 2: 从 codec context 获取
    # PyAV 10+ 支持 keyframe intervals
    if hasattr(stream, 'codec_context') and stream.codec_context:
        ctx = stream.codec_context
        if hasattr(ctx, 'gop_size'):
            gop_size = ctx.gop_size
            if gop_size and gop_size > 0:
                # 假设 GOP 大小固定，估算 keyframe PTS
                # 这是一个近似值，可能不准确
                return target_pts

    # 默认返回 None，让 seek 函数自行处理
    return None


def estimate_keyframe_interval(codec_name: str, real_interval: Optional[int] = None) -> int:
    """
    估算 GOP 编解码器的 keyframe 间隔。

    Args:
        codec_name: 编解码器名称
        real_interval: 真实 keyframe 间隔（P2-05 GOP 索引就绪时由
            ``FFmpegMediaSource.keyframe_interval_hint`` 提供）。传入时优先
            使用真实值——硬编码猜测表与真实素材（如 4K HEVC keyint=25）
            经常不符，会误导 seek-vs-顺序解码决策。

    Returns:
        预估的 GOP 大小（帧数）

    Common GOP sizes:
        - H.264/H.265: 通常 12-250 帧（取决于编码设置）
        - ProRes: 1 帧（每帧都是关键帧）
        - DNxHR: 1 帧（每帧都是关键帧）
    """
    if real_interval is not None and real_interval > 0:
        return int(real_interval)

    if is_intra_codec(codec_name):
        return 1

    # GOP codec 的典型值
    # 这是一个保守估计，避免 decode forward 太多帧
    codec_defaults: dict[str, int] = {
        'h264': 12,  # 常见 GOP: 12, 24, 30, 60
        'h265': 12,
        'hevc': 12,
        'mpeg4': 12,
        'mpeg2video': 15,  # PAL: 15, NTSC: 15
        'vp9': 120,  # VP9 默认 keyframe interval
        'av1': 120,
        'vp8': 120,
    }

    name_lower = codec_name.lower()
    for key, value in codec_defaults.items():
        if key in name_lower:
            return value

    # 默认保守值
    return 30


def estimate_seek_cost(
    current_frame: int,
    target_frame: int,
    codec_name: str,
    keyframe_interval: Optional[int] = None
) -> int:
    """
    估算 seek 到目标帧需要的解码帧数。

    用于判断 seek 成本，帮助播放策略决策。

    Args:
        current_frame: 当前帧号
        target_frame: 目标帧号
        codec_name: 编解码器名称
        keyframe_interval: 真实 keyframe 间隔（P2-05 GOP 索引就绪时提供，
            优先于 codec 猜测表）

    Returns:
        预估需要解码的帧数

    Examples:
        >>> # 当前帧 100，目标帧 150，H.264
        >>> estimate_seek_cost(100, 150, 'h264')
        # 可能返回 50（从 keyframe 解码到目标）
    """
    if is_intra_codec(codec_name):
        # Intra codec 可以直接 seek，成本为 1
        return 1

    # GOP codec: 需要从 keyframe decode forward
    gop_size = estimate_keyframe_interval(codec_name, keyframe_interval)

    # 计算帧距离
    frame_distance = abs(target_frame - current_frame)

    if frame_distance == 0:
        return 0

    # 估算：最多需要解码 gop_size + 距离内的偏移
    # seek 到目标帧之前的 keyframe，然后 decode forward
    # keyframe 位置 = target_frame - (target_frame % gop_size)
    keyframe_before_target = target_frame - (target_frame % gop_size)

    # 需要解码的帧数 = target_frame - keyframe_before_target
    decode_forward = target_frame - keyframe_before_target

    # AUD-35701：GOP 编解码器无法反向顺序解码。三条路径的成本都
    # 是"seek 到目标帧之前的 keyframe，再 forward 解码 decode_forward
    # 帧"——只有正向小距离且当前帧未越过该 keyframe 时，顺序解码
    # 才真正可行（成本 = frame_distance）。
    if current_frame <= keyframe_before_target:
        # 当前帧在 keyframe 之前（keyframe 位于前方）：seek 到 keyframe
        # 再 decode forward 到目标帧。
        return max(1, decode_forward)

    if target_frame < current_frame:
        # 反向跳转：当前帧已越过目标帧前的 keyframe，必须回到该
        # keyframe 重新 forward 解码。此前走"顺序解码更高效"分支
        # 返回 frame_distance，对反向小距离跳转系统性低估最多
        # gop_size-1 帧（例：current=100, target=95, gop=12 返回 5，
        # 实际需回到 keyframe 84 解码 11 帧），交互解码 deadline 被
        # 低估导致超时/掉帧。
        return max(1, decode_forward)

    if frame_distance > gop_size:
        # 正向大距离：seek 到 keyframe 更划算
        return max(1, decode_forward)

    # 正向小距离跳转：当前帧在目标帧之前的同一 GOP 邻域内，
    # 继续顺序解码更高效
    return frame_distance


# =============================================================================
# Seek 策略
# =============================================================================

def choose_seek_strategy(
    current_frame: int,
    target_frame: int,
    codec_name: str,
    threshold: int = 10
) -> str:
    """
    选择最佳的 seek 策略。

    根据帧距离和编解码器类型，判断是否需要 seek。

    Args:
        current_frame: 当前帧号
        target_frame: 目标帧号
        codec_name: 编解码器名称
        threshold: 小于此距离时，选择顺序解码而非 seek

    Returns:
        'sequential': 继续顺序解码
        'seek': 执行 seek 操作
        'seek_forward': seek + decode forward (GOP codec)

    Examples:
        >>> choose_seek_strategy(100, 105, 'h264', threshold=10)
        'sequential'  # 小距离，顺序解码更高效

        >>> choose_seek_strategy(100, 500, 'h264', threshold=10)
        'seek_forward'  # 大距离，需要 seek
    """
    distance = abs(target_frame - current_frame)

    # 小距离：顺序解码
    if distance <= threshold:
        return 'sequential'

    # 大距离：需要 seek
    if is_intra_codec(codec_name):
        return 'seek'  # Intra codec 可以直接 seek
    else:
        return 'seek_forward'  # GOP codec 需要 seek + decode forward


# =============================================================================
# 辅助数据结构
# =============================================================================

class SeekInfo:
    """
    Seek 信息数据类。

    用于记录 seek 操作的详细信息。
    """

    def __init__(
        self,
        target_frame: int,
        actual_frame: int,
        codec_name: str,
        keyframe_frame: Optional[int] = None,
        frames_decoded: int = 0,
        tolerance: int = 1
    ) -> None:
        """
        初始化 SeekInfo。

        Args:
            target_frame: 目标帧号
            actual_frame: 实际到达的帧号
            codec_name: 编解码器名称
            keyframe_frame: seek 定位到的 keyframe 帧号
            frames_decoded: decode forward 的帧数
            tolerance: 允许的帧号误差（±tolerance）
        """
        self.target_frame = target_frame
        self.actual_frame = actual_frame
        self.codec_name = codec_name
        self.keyframe_frame = keyframe_frame
        self.frames_decoded = frames_decoded
        self.tolerance = tolerance

    @property
    def is_accurate(self) -> bool:
        """
        判断 seek 是否精确。

        Returns:
            实际帧号在目标帧号的 tolerance 范围内返回 True
        """
        return abs(self.actual_frame - self.target_frame) <= self.tolerance

    @property
    def overshoot(self) -> int:
        """
        获取 overshoot（超调）帧数。

        Returns:
            超调帧数（正数表示 overshoot，负数表示 undershoot）
        """
        return self.actual_frame - self.target_frame

    def __repr__(self) -> str:
        """字符串表示"""
        status = "accurate" if self.is_accurate else "inaccurate"
        return (
            f"SeekInfo(target={self.target_frame}, actual={self.actual_frame}, "
            f"codec={self.codec_name}, decoded={self.frames_decoded}, status={status})"
        )