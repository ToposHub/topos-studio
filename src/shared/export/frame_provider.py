"""
Export Frame Provider Module
提供导出用的确定性帧迭代器。

## 职责

- ExportFrameProvider: 导出帧提供器，与播放分离
- 按帧顺序请求 master frame
- 不 drop frame，确定性解码
- 支持帧范围选择

## 设计原则

- 与播放引擎完全解耦
- 确定性帧序列（无跳帧）
- 完整类型注解
- 支持高位深视频
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Iterator, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from ..media.source import MediaSource
    from ..media.frame import DecodedFrame
    from ..media.metadata import VideoMetadata


logger = logging.getLogger(__name__)


@dataclass
class ExportRange:
    """
    导出帧范围配置。

    Attributes:
        start_frame: 起始帧号（从 0 开始）
        end_frame: 结束帧号（exclusive，不包含）
    """

    start_frame: int
    end_frame: int

    def __post_init__(self) -> None:
        """验证帧范围"""
        if self.start_frame < 0:
            raise ValueError(f"start_frame cannot be negative: {self.start_frame}")
        if self.end_frame < self.start_frame:
            raise ValueError(
                f"end_frame ({self.end_frame}) must be >= start_frame ({self.start_frame})"
            )

    @property
    def frame_count(self) -> int:
        """帧数量"""
        return self.end_frame - self.start_frame

    @property
    def duration_seconds(self) -> float:
        """
        时长（秒），需要配合帧率计算。

        注意：此属性仅返回帧数，实际时长需要帧率。
        """
        return float(self.frame_count)


class ExportFrameProvider:
    """
    导出帧提供器。

    与播放引擎完全分离，提供确定性的逐帧解码：
    - 按帧顺序请求 master frame
    - 不 drop frame
    - 确定性解码（每次导出相同帧序列）
    - 支持帧范围选择

    与播放引擎的差异：
    - 播放引擎：实时、可能跳帧、基于时间
    - 导出提供器：确定性、不跳帧、基于帧号

    Attributes:
        source: 媒体源（MediaSource）
        metadata: 视频元数据
        range: 导出帧范围
        current_frame: 当前帧号

    Examples:
        >>> from src.shared.media import FFmpegMediaSource
        >>> source = FFmpegMediaSource('/path/to/video.mp4')
        >>> provider = ExportFrameProvider(source, start_frame=0, end_frame=100)
        >>> for frame in provider:
        ...     # 处理每一帧
        ...     print(f"Frame {frame.frame_index}")
        >>> # 获取帧范围信息
        >>> start, end = provider.get_frame_range()
        >>> # 估算输出大小
        >>> size_estimate = provider.estimate_output_size(codec='prores')
    """

    def __init__(
        self,
        source: "MediaSource",
        start_frame: int = 0,
        end_frame: Optional[int] = None,
    ) -> None:
        """
        初始化导出帧提供器。

        Args:
            source: 媒体源（必须实现 MediaSource Protocol）
            start_frame: 起始帧号（从 0 开始）
            end_frame: 结束帧号（exclusive，None 表示到视频末尾）

        Raises:
            ValueError: 帧范围无效
        """
        self._source: "MediaSource" = source
        self._metadata: Optional["VideoMetadata"] = None
        self._start_frame: int = start_frame
        self._end_frame: Optional[int] = end_frame
        self._current_frame: int = start_frame

        # 探测元数据
        self._probe_metadata()

        # 确定结束帧
        if self._end_frame is None:
            if self._metadata and self._metadata.total_frames is not None:
                self._end_frame = self._metadata.total_frames
            else:
                raise ValueError(
                    "Cannot determine end_frame: video has no total_frames info. "
                    "Please specify end_frame explicitly."
                )

        # 验证帧范围
        self._validate_range()

        logger.debug(
            f"ExportFrameProvider initialized: "
            f"range=[{self._start_frame}, {self._end_frame}), "
            f"total_frames={self._metadata.total_frames if self._metadata else 'unknown'}"
        )

    def _probe_metadata(self) -> None:
        """探测视频元数据"""
        try:
            self._metadata = self._source.probe()
            logger.debug(
                f"Probed metadata: {self._metadata.width}x{self._metadata.height}, "
                f"fps={self._metadata.fps.to_fps():.3f}, "
                f"codec={self._metadata.codec_name}"
            )
        except Exception as e:
            logger.warning(f"Failed to probe metadata: {e}")
            # 继续执行，某些情况下可能不需要完整元数据

    def _validate_range(self) -> None:
        """验证帧范围有效性"""
        if self._start_frame < 0:
            raise ValueError(f"start_frame cannot be negative: {self._start_frame}")

        if self._end_frame is not None:
            if self._end_frame < self._start_frame:
                raise ValueError(
                    f"end_frame ({self._end_frame}) must be >= start_frame ({self._start_frame})"
                )

            # 检查是否超出视频范围
            if self._metadata and self._metadata.total_frames is not None:
                if self._end_frame > self._metadata.total_frames:
                    logger.warning(
                        f"end_frame ({self._end_frame}) exceeds total_frames "
                        f"({self._metadata.total_frames}), clamping to total_frames"
                    )
                    self._end_frame = self._metadata.total_frames

    def __iter__(self) -> Iterator["DecodedFrame"]:
        """
        迭代导出帧。

        按帧顺序从 start_frame 到 end_frame 解码每一帧。
        不跳帧，确定性输出。

        使用 source.read_frame(frame_idx) 确保返回的帧号与请求一致，
        不做偏帧。read_frame 方法内部会自动处理 seek 和连续帧优化。

        Yields:
            DecodedFrame 实例

        Raises:
            RuntimeError: 媒体源未打开
        """
        # 确保媒体源已打开
        if not self._source.is_open:
            self._source.open()

        for frame_idx in range(self._start_frame, self._end_frame):
            self._current_frame = frame_idx

            # 使用 read_frame 确保帧号准确
            # read_frame 内部会自动处理 seek 和连续帧优化
            try:
                frame = self._source.read_frame(frame_idx)

                # 验证返回帧号与请求一致（防止偏帧）
                if frame.frame_index != frame_idx:
                    logger.warning(
                        f"Frame index mismatch: requested {frame_idx}, "
                        f"got {frame.frame_index}. This may indicate seek precision issues."
                    )
                    # 继续处理，但记录警告

                yield frame

            except Exception as e:
                logger.error(f"Failed to decode frame {frame_idx}: {e}")
                raise

        logger.debug(
            f"Export iteration complete: "
            f"decoded {self._end_frame - self._start_frame} frames"
        )

    def get_frame_range(self) -> tuple[int, int]:
        """
        返回帧范围。

        Returns:
            (start_frame, end_frame) tuple
        """
        return (self._start_frame, self._end_frame)

    def get_frame_count(self) -> int:
        """
        返回导出帧数。

        Returns:
            帧数量
        """
        return self._end_frame - self._start_frame

    def get_duration_seconds(self) -> float:
        """
        返回导出时长（秒）。

        Returns:
            时长（秒）
        """
        if self._metadata is None:
            # 无法计算，返回帧数作为 fallback
            return float(self.get_frame_count())

        fps = self._metadata.fps.to_fps()
        return self.get_frame_count() / fps

    def estimate_output_size(
        self,
        codec: str = "h264",
        bitrate: Optional[int] = None,
    ) -> int:
        """
        估算输出文件大小。

        Args:
            codec: 编码器名称（'h264', 'hevc', 'prores', 'dnxhr'）
            bitrate: 目标比特率（bps），仅用于 GOP codec

        Returns:
            估算文件大小（字节）

        Note:
            估算公式基于：
            - ProRes/DNxHR: 固定数据率公式
            - H.264/HEVC: 使用指定 bitrate 或默认值
        """
        if self._metadata is None:
            # 无元数据时无法估算
            return 0

        frame_count = self.get_frame_count()
        width = self._metadata.width
        height = self._metadata.height
        fps = self._metadata.fps.to_fps()
        bit_depth = self._metadata.bit_depth

        # 根据编码器类型估算
        codec_lower = codec.lower()

        if codec_lower in ("prores", "prores_ks", "prores_aw"):
            # ProRes 数据率估算
            # ProRes Proxy: ~45 Mbps
            # ProRes LT: ~102 Mbps
            # ProRes 422: ~147 Mbps
            # ProRes HQ: ~220 Mbps
            # ProRes 4444: ~330 Mbps (含 alpha)
            # ProRes 4444 XQ: ~440 Mbps (含 alpha)

            # 默认使用 ProRes 422 的数据率
            # 简化公式：数据率 ≈ (width * height * fps * factor) / 8
            # factor 取值：
            # - 10-bit: 0.5 (每像素约 0.5 byte)
            # - 12-bit: 0.6

            if bit_depth <= 8:
                factor = 0.35  # 8-bit ProRes 通常不存在
            elif bit_depth <= 10:
                factor = 0.45  # ProRes 422
            else:
                factor = 0.55  # ProRes 4444 XQ

            # 数据率（字节/秒）
            data_rate_bps = (width * height * fps * factor)

            # 总大小（字节）
            total_size = int(data_rate_bps * frame_count / fps)

            # 加上容器开销（约 1%）
            total_size = int(total_size * 1.01)

            return total_size

        elif codec_lower in ("dnxhr", "dnxhd"):
            # DNxHR/DNxHD 数据率估算
            # DNxHR LB: ~78 Mbps @ 4K
            # DNxHR SQ: ~156 Mbps @ 4K
            # DNxHR HQ: ~234 Mbps @ 4K
            # DNxHR HQX: ~468 Mbps @ 4K (10-bit)
            # DNxHR 444: ~702 Mbps @ 4K (10-bit + alpha)

            # 简化估算：与 ProRes 类似
            factor = 0.4 if bit_depth <= 8 else 0.5
            data_rate_bps = (width * height * fps * factor)
            total_size = int(data_rate_bps * frame_count / fps)

            return int(total_size * 1.01)

        else:
            # GOP codec (H.264, HEVC, VP9, AV1)
            # 使用 bitrate 或默认值

            if bitrate is None:
                # 默认 bitrate 估算
                # H.264: ~10-20 Mbps for 1080p, ~40-80 Mbps for 4K
                # HEVC: ~5-10 Mbps for 1080p, ~20-40 Mbps for 4K

                if width <= 1920 and height <= 1080:
                    # HD
                    if codec_lower in ("hevc", "h265"):
                        bitrate = 8_000_000  # 8 Mbps
                    else:
                        bitrate = 12_000_000  # 12 Mbps
                elif width <= 3840 and height <= 2160:
                    # 4K
                    if codec_lower in ("hevc", "h265"):
                        bitrate = 25_000_000  # 25 Mbps
                    else:
                        bitrate = 40_000_000  # 40 Mbps
                else:
                    # 更高分辨率
                    bitrate = int(width * height * fps * 0.5)  # 简化估算

            # 总大小（字节）
            total_size = int(bitrate * frame_count / fps / 8)

            # 加上容器开销
            return int(total_size * 1.02)

    @property
    def current_frame(self) -> int:
        """当前帧号"""
        return self._current_frame

    @property
    def metadata(self) -> Optional["VideoMetadata"]:
        """视频元数据"""
        return self._metadata

    @property
    def source(self) -> "MediaSource":
        """媒体源"""
        return self._source

    def get_progress(self) -> float:
        """
        获取当前进度。

        Returns:
            进度百分比（0.0 - 1.0）
        """
        if self._end_frame == self._start_frame:
            return 1.0

        total_frames = self._end_frame - self._start_frame
        current_progress = self._current_frame - self._start_frame

        return current_progress / total_frames

    def seek_to_frame(self, frame_index: int) -> None:
        """
        定位到指定帧（用于导出中的跳转）。

        Args:
            frame_index: 目标帧号

        Raises:
            ValueError: 帧号超出导出范围
        """
        if frame_index < self._start_frame or frame_index >= self._end_frame:
            raise ValueError(
                f"Frame {frame_index} out of export range "
                f"[{self._start_frame}, {self._end_frame})"
            )

        self._current_frame = frame_index
        self._source.seek_frame(frame_index)

    def read_frame_at(self, frame_index: int) -> "DecodedFrame":
        """
        读取指定帧（不改变迭代位置）。

        Args:
            frame_index: 目标帧号

        Returns:
            DecodedFrame 实例

        Raises:
            ValueError: 帧号超出导出范围
        """
        if frame_index < self._start_frame or frame_index >= self._end_frame:
            raise ValueError(
                f"Frame {frame_index} out of export range "
                f"[{self._start_frame}, {self._end_frame})"
            )

        # 保存当前位置
        saved_frame = self._current_frame

        # AUD-33203：解码改用 MediaSource Protocol 内的 read_frame——
        # read_next_frame 不在 Protocol（src/shared/media/source.py）上，
        # 严格按 Protocol 实现的源在此处直接 AttributeError；read_frame
        # 内部已处理 seek 与连续帧优化。
        try:
            frame = self._source.read_frame(frame_index)

            # 校验返回帧号与请求一致（与 __iter__ 路径的防偏帧告警对齐；
            # seek 不精确的源会静默返回错误帧）
            if frame.frame_index != frame_index:
                logger.warning(
                    f"Frame index mismatch: requested {frame_index}, "
                    f"got {frame.frame_index}. This may indicate seek precision issues."
                )
        finally:
            # 恢复位置（如果需要）——无论解码成败都执行，异常路径不遗留
            # 污染的源位置。
            # 注意：某些 codec 可能无法精确恢复，建议只在必要时使用
            if saved_frame != frame_index:
                self._source.seek_frame(saved_frame)

        return frame


# =============================================================================
# 辅助函数
# =============================================================================


def create_export_provider(
    source: "MediaSource",
    start_frame: int = 0,
    end_frame: Optional[int] = None,
) -> ExportFrameProvider:
    """
    创建导出帧提供器的便捷函数。

    Args:
        source: 媒体源
        start_frame: 起始帧号
        end_frame: 结束帧号

    Returns:
        ExportFrameProvider 实例
    """
    return ExportFrameProvider(source, start_frame, end_frame)


def estimate_export_duration(
    frame_count: int,
    fps: float,
    codec: str = "h264",
) -> float:
    """
    估算导出时长（秒）。

    Args:
        frame_count: 帧数量
        fps: 帧率
        codec: 编码器名称（用于进度估算）

    Returns:
        估算时长（秒）
    """
    # 简化估算：帧数 / 帧率
    return frame_count / fps