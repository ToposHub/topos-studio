"""
macOS VideoToolbox Packet Demux
只 demux 压缩 packet，不调用 decode/to_ndarray。

## 职责

- 使用 PyAV 打开容器
- 只读取压缩 packet
- 不调用 container.decode()
- 不调用 packet.decode()
- 输出 codec extradata、time_base、pts/dts、packet bytes

## 验收

- packet.decode 不被调用
- frame.to_ndarray 不被调用
- 能读到 extradata / codec / width / height / fps
"""

from __future__ import annotations

import ctypes
import logging
from dataclasses import dataclass
from typing import Iterator

logger = logging.getLogger(__name__)


@dataclass
class CompressedPacket:
    """
    压缩视频 packet。

    不包含解码后的帧数据，只包含压缩码流和元信息。

    Attributes:
        data: 压缩码流 bytes
        pts: 展示时间戳（stream time_base 单位）
        dts: 解码时间戳（stream time_base 单位）
        duration: 持续时间（stream time_base 单位）
        is_keyframe: 是否为关键帧
        frame_index: 估算的帧号
    """
    data: bytes
    pts: int | None
    dts: int | None
    duration: int | None
    is_keyframe: bool
    frame_index: int = -1


@dataclass
class VideoStreamInfo:
    """
    视频流元信息。

    从容器头中读取，不触发解码。

    Attributes:
        codec_name: 编码器名称（h264, hevc 等）
        codec_tag: 容器 fourcc（如 ProRes 的 'apch'/'ap4h'，可为空）
        width: 视频宽度
        height: 视频高度
        fps: 帧率
        time_base: 时间基（分子/分母）
        total_frames: 总帧数（可能为 0 表示未知）
        pixel_format: 像素格式名称
        bit_depth: 位深度
        extradata: 编码器额外数据（用于 VT session 创建）
        start_time: 起始时间戳
        color_space: 流色彩矩阵标记（如 'bt709'/'smpte170m'/'bt2020nc'，空串=未标记）
        color_range: 流颜色范围标记（'tv'/'pc'，空串=未标记）
    """
    codec_name: str = ""
    codec_tag: str = ""
    width: int = 0
    height: int = 0
    fps: float = 30.0
    time_base_num: int = 1
    time_base_den: int = 30
    total_frames: int = 0
    pixel_format: str = ""
    bit_depth: int = 8
    extradata: bytes | None = None
    start_time: int | None = None
    color_space: str = ""
    color_range: str = ""


class PacketDemuxer:
    """
    压缩 packet demuxer。

    只读取压缩 packet，不调用 decode/to_ndarray。
    用于将压缩码流传递给 VTDecompressionSession。

    Usage:
        demuxer = PacketDemuxer()
        info = demuxer.open('/path/to/4k_h264.mp4')
        for pkt in demuxer.packets():
            # 使用 pkt.data 创建 VT 压缩 packet
            pass
        demuxer.close()
    """

    def __init__(self) -> None:
        self._container: object | None = None
        self._stream: object | None = None
        self._info: VideoStreamInfo | None = None
        self._frame_index: int = 0

    def open(self, path: str) -> VideoStreamInfo:
        """
        打开视频文件，只读取容器头信息。

        Args:
            path: 视频文件路径

        Returns:
            VideoStreamInfo 元信息

        Raises:
            MediaOpenError: 文件打开失败
        """
        import av

        self._container = av.open(path)
        self._stream = None

        for s in self._container.streams:
            if s.type == 'video':
                self._stream = s
                break

        if self._stream is None:
            self._container.close()
            self._container = None
            raise ValueError(f"No video stream found in {path}")

        stream = self._stream
        codec_tag = stream.codec_tag if hasattr(stream, 'codec_tag') else ''
        if isinstance(codec_tag, int):
            # 整数 fourcc（如 0x61706368）转成 'apch' 形式
            try:
                codec_tag = codec_tag.to_bytes(4, 'big').decode('latin-1')
            except Exception:
                codec_tag = ''
        elif not isinstance(codec_tag, str):
            try:
                codec_tag = bytes(codec_tag).decode('latin-1')
            except Exception:
                codec_tag = ''
        self._info = VideoStreamInfo(
            codec_name=stream.codec_context.name if hasattr(stream, 'codec_context') else str(stream.codec_context),
            # MOV/MXF 容器 fourcc（如 ProRes 的 'apch'/'ap4h'）。VT 会话创建
            # CMVideoFormatDescription 需要 codec type 整数 fourcc，ProRes 家族
            # 无 extradata，fourcc 是唯一的格式描述来源。
            codec_tag=codec_tag,
            width=stream.codec_context.width,
            height=stream.codec_context.height,
            fps=float(stream.average_rate) if stream.average_rate else 30.0,
            time_base_num=stream.time_base.numerator if hasattr(stream.time_base, 'numerator') else 1,
            time_base_den=stream.time_base.denominator if hasattr(stream.time_base, 'denominator') else 30,
            total_frames=stream.frames if stream.frames else 0,
            # 禁止直读 codec_context.pix_fmt：PyAV 17 的 getter 对
            # AV_PIX_FMT_NONE（损坏/未解析出帧的流）无守卫——
            # PixelFormat(-1) 取 NULL 描述符直接段崩溃，hasattr 拦不住。
            # stream.format 是库自身的安全通道（NONE → None）。
            pixel_format=(
                stream.format.name
                if getattr(stream, "format", None) is not None else ''),
            bit_depth=8,
            extradata=bytes(stream.codec_context.extradata) if stream.codec_context.extradata else None,
            # 流级色彩标记。VT 解码输出帧本身不携带这些信息，必须由
            # demux 侧传入，否则预览会用错 YUV 矩阵/范围（与调色页
            # 读流元数据的行为对齐）。PyAV 17 返回 FFmpeg 枚举 int，
            # 这里直接翻译成规范字符串，避免下游别名表歧义。
            color_space=self._ffmpeg_colorspace_name(
                getattr(stream.codec_context, 'colorspace', 0) or 0
            ),
            color_range=self._ffmpeg_color_range_name(
                getattr(stream.codec_context, 'color_range', 0) or 0
            ),
        )

        if '10' in self._info.pixel_format or 'p010' in self._info.pixel_format.lower():
            self._info.bit_depth = 10
        elif '12' in self._info.pixel_format or '422' in self._info.pixel_format.lower():
            self._info.bit_depth = 10

        self._frame_index = 0
        logger.info(
            f"PacketDemuxer: opened {path}, "
            f"codec={self._info.codec_name}, "
            f"{self._info.width}x{self._info.height}, "
            f"fps={self._info.fps}, "
            f"has_extradata={self._info.extradata is not None}"
        )
        return self._info

    @staticmethod
    def _ffmpeg_colorspace_name(value: int) -> str:
        """FFmpeg AVCOL_SPC_* 枚举 → 规范矩阵名（空串=未标记）。

        0=RGB, 1=bt709, 5=bt470bg, 6=smpte170m, 7=smpte240m,
        9=bt2020nc。10=bt2020c 刻意归一为 bt2020nc（AUD-35103）：
        VT 解码输出为 NV12/P010 平面，属 NCL 型语义；且下游
        gpu_frame.VALID_COLOR_MATRICES 不含 'bt2020c'，保留 CL
        标记会破坏下游校验——故 10 与 9 同映射。未标记
        (0/2=unspecified)时返回空串，由下游按 bt709 默认处理。
        """
        return {
            1: 'bt709',
            5: 'bt470bg',
            6: 'smpte170m',
            7: 'smpte240m',
            9: 'bt2020nc',
            10: 'bt2020nc',
        }.get(int(value or 0), '')

    @staticmethod
    def _ffmpeg_color_range_name(value: int) -> str:
        """FFmpeg AVCOL_RANGE_* 枚举 → 规范范围名（空串=未标记）。

        0=unspecified, 1=mpeg(limited/tv), 2=jpeg(full/pc)。
        注意与下游别名表的 '1'→full 字符串语义不同，这里按
        FFmpeg 原始枚举语义翻译。
        """
        v = int(value or 0)
        if v == 1:
            return 'limited'
        if v == 2:
            return 'full'
        return ''

    def packets(self, max_packets: int | None = None) -> Iterator[CompressedPacket]:
        """
        迭代压缩 packet。

        不调用 decode/to_ndarray，只读取压缩码流。

        Args:
            max_packets: 最大读取 packet 数（None = 全部）

        Yields:
            CompressedPacket 压缩 packet
        """
        if self._container is None or self._stream is None:
            raise RuntimeError("PacketDemuxer not opened, call open() first")

        # AUD-35101：限额判断必须在从 demuxer 取下一个包**之前**。
        # 旧写法 `for packet in demux: if count >= max: break` 会先取出
        # 第 max+1 个包再丢弃——该压缩包被永久消费，消费者（VT 会话）
        # 缺一个包，B/P 帧参考链断裂。
        count = 0
        demux_iter = iter(self._container.demux(self._stream))
        while max_packets is None or count < max_packets:
            try:
                packet = next(demux_iter)
            except StopIteration:
                break

            # Extract raw packet bytes using buffer_ptr (PyAV doesn't expose .data directly)
            packet_bytes = b''
            if packet.buffer_ptr is not None and packet.buffer_size > 0:
                try:
                    buf = (ctypes.c_ubyte * packet.buffer_size).from_address(packet.buffer_ptr)
                    packet_bytes = bytes(buf)
                except Exception as e:
                    logger.warning(f"PacketDemuxer: failed to read packet buffer: {e}")

            compressed = CompressedPacket(
                data=packet_bytes,
                pts=int(packet.pts) if packet.pts is not None else None,
                dts=int(packet.dts) if packet.dts is not None else None,
                duration=int(packet.duration) if packet.duration is not None else None,
                is_keyframe=packet.is_keyframe if hasattr(packet, 'is_keyframe') else False,
                frame_index=self._frame_index,
            )
            self._frame_index += 1
            count += 1
            yield compressed

    def close(self) -> None:
        """关闭容器"""
        if self._container is not None:
            self._container.close()
            self._container = None
        self._stream = None
        self._info = None

    def seek_to_frame(self, frame_index: int) -> None:
        """
        Seek 到指定帧号附近（最近的关键帧）。

        使用 PyAV container.seek() 定位到目标帧对应的时间戳。
        seek 后需要重新 demux 才能读取新的 packet。

        Args:
            frame_index: 目标帧号

        Raises:
            RuntimeError: 容器未打开
        """
        if self._container is None or self._stream is None or self._info is None:
            raise RuntimeError("PacketDemuxer not opened, call open() first")

        # 将帧号转换为流时间戳
        time_base_den = self._info.time_base_den
        time_base_num = self._info.time_base_num
        fps = self._info.fps if self._info.fps > 0 else 30.0

        # 估算目标 pts：frame_index / fps * time_base_den
        target_pts = int(frame_index / fps * time_base_den / time_base_num)

        # 使用 PyAV seek：backward=True 表示 seek 到目标之前最近的关键帧
        self._container.seek(target_pts, stream=self._stream, any_frame=False)
        self._frame_index = frame_index

        logger.debug(
            f"PacketDemuxer: seeked to frame {frame_index}, "
            f"target_pts={target_pts}"
        )

    @property
    def info(self) -> VideoStreamInfo | None:
        """获取视频流信息"""
        return self._info

    @property
    def is_open(self) -> bool:
        """是否已打开"""
        return self._container is not None


# Q01/F1.2 根治：VT 解复用入口加进程级 in-flight 计数。
from src.shared.media.av_concurrency import av_operation_decorator as _av_op_dec  # noqa: E402

for _cls in [c for c in list(globals().values()) if isinstance(c, type)]:
    for _name in ("open", "seek_to_frame", "read_packet", "read_next_packet"):
        if _name in _cls.__dict__:
            setattr(_cls, _name, _av_op_dec(_cls.__dict__[_name]))
