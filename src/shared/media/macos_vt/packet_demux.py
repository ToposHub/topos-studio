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
import os
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Generator, Iterator

from ..color_metadata import normalize_color_primaries, normalize_color_transfer
from ..mov_atoms import find_mov_atom, iter_file_mov_atoms, iter_mov_atoms

logger = logging.getLogger(__name__)

ScrubMediaIdentity = tuple[int, int, int, int]


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
        position: 索引读取时的样本字节位置
        has_new_extradata: 样本是否声明新的解码配置
    """
    data: bytes
    pts: int | None
    dts: int | None
    duration: int | None
    is_keyframe: bool
    frame_index: int = -1
    position: int | None = None
    has_new_extradata: bool = False


@dataclass(frozen=True)
class H264ScrubGop:
    """Candidate GOP from one open AVC sample index, checked again on read."""

    first_frame: int
    stop_frame: int
    _ticks: int = field(repr=False)
    _positions: tuple[int, ...] = field(repr=False)
    _index_token: object = field(repr=False)

    def frame_for_pts(self, pts: int | None) -> int | None:
        """Identify an exact presentation timestamp inside this GOP."""
        if pts is None:
            return None
        frame, remainder = divmod(pts, self._ticks)
        if remainder or not self.first_frame <= frame < self.stop_frame:
            return None
        return frame


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
    color_primaries: str | None = None
    color_transfer: str | None = None


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

    _MAX_H264_SCRUB_GOP_SAMPLES = 60

    def __init__(self) -> None:
        self._container: object | None = None
        self._stream: object | None = None
        self._info: VideoStreamInfo | None = None
        self._frame_index: int = 0
        self._hevc_nal_length_size: int = 0
        self._h264_scrub_ticks = 0
        self._h264_scrub_nal_length = 0
        self._h264_scrub_index_token = object()
        self.scrub_media_identity: ScrubMediaIdentity | None = None

    @staticmethod
    def _media_identity(path: str) -> ScrubMediaIdentity | None:
        try:
            stat = os.stat(path)
        except (OSError, ValueError):
            return None
        return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns

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

        before_open = self._media_identity(path)
        self.scrub_media_identity = None
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
            color_primaries=normalize_color_primaries(getattr(stream.codec_context, 'color_primaries', None)),
            color_transfer=normalize_color_transfer(getattr(stream.codec_context, 'color_trc', None)),
        )

        if '10' in self._info.pixel_format or 'p010' in self._info.pixel_format.lower():
            self._info.bit_depth = 10
        elif '12' in self._info.pixel_format or '422' in self._info.pixel_format.lower():
            self._info.bit_depth = 10

        self._hevc_nal_length_size = self._single_layer_hevc_nal_length_size(path)
        self._set_h264_scrub_index()
        if self.supports_h264_scrub_gops() and before_open == self._media_identity(path):
            self.scrub_media_identity = before_open
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

    def packets(
        self, max_packets: int | None = None, *, include_sample_identity: bool = False,
    ) -> Iterator[CompressedPacket]:
        """
        迭代压缩 packet。

        不调用 decode/to_ndarray，只读取压缩码流。

        Args:
            max_packets: 最大读取 packet 数（None = 全部）
            include_sample_identity: Include sample positions and format changes for indexed reads.

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

            # PyAV emits a zero-size dummy packet to flush its own decoder at
            # EOF. VT receives compressed samples, not PyAV flush markers:
            # a zero-length CMBlockBuffer fails with BadLengthParameter (-12704).
            # Count only real samples, including after a bounded demux resumes.
            if packet.buffer_size <= 0:
                continue

            # Extract raw packet bytes using buffer_ptr (PyAV doesn't expose .data directly)
            packet_bytes = b''
            if packet.buffer_ptr:
                try:
                    buf = (ctypes.c_ubyte * packet.buffer_size).from_address(packet.buffer_ptr)
                    packet_bytes = bytes(buf)
                except Exception as e:
                    logger.warning(f"PacketDemuxer: failed to read packet buffer: {e}")
            else:
                logger.warning("PacketDemuxer: nonempty packet has no buffer pointer")
            if not packet_bytes:
                continue
            has_new_extradata = False
            if self._hevc_nal_length_size or include_sample_identity:
                has_sidedata = getattr(packet, 'has_sidedata', None)
                has_new_extradata = callable(has_sidedata) and has_sidedata('new_extradata')
                if has_new_extradata:
                    self._hevc_nal_length_size = 0

            compressed = CompressedPacket(
                data=packet_bytes,
                pts=int(packet.pts) if packet.pts is not None else None,
                dts=int(packet.dts) if packet.dts is not None else None,
                duration=int(packet.duration) if packet.duration is not None else None,
                is_keyframe=packet.is_keyframe if hasattr(packet, 'is_keyframe') else False,
                frame_index=self._frame_index,
                position=getattr(packet, 'pos', None) if include_sample_identity else None,
                has_new_extradata=has_new_extradata,
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
        self._h264_scrub_ticks = 0
        self._h264_scrub_nal_length = 0
        self._h264_scrub_index_token = object()

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

        fps = self._info.fps if self._info.fps > 0 else 30.0
        target_pts = int(frame_index / fps * self._info.time_base_den / self._info.time_base_num)

        # 使用 PyAV seek：backward=True 表示 seek 到目标之前最近的关键帧
        self._container.seek(target_pts, stream=self._stream, any_frame=False)
        self._frame_index = frame_index

        logger.debug(
            f"PacketDemuxer: seeked to frame {frame_index}, "
            f"target_pts={target_pts}"
        )

    @staticmethod
    def _ticks_per_frame(info: VideoStreamInfo) -> float:
        fps = info.fps if info.fps > 0 else 30.0
        return info.time_base_den / (fps * info.time_base_num)

    def estimate_seek_frames(self, frame_index: int) -> int | None:
        """Estimate walk length from the already-open container index."""
        entries = getattr(self._stream, "index_entries", None)
        if self._info is None or entries is None or not len(entries):
            return None
        ticks = self._ticks_per_frame(self._info)
        target = int(frame_index * ticks)
        try:
            index = entries.search_timestamp(target, backward=True, any_frame=False)
            anchor = entries[index].timestamp if index >= 0 else None
        except (AttributeError, IndexError, ValueError):
            return None
        if anchor is None:
            return None
        # Container index entries can be in decode order (DTS), so use this
        # as a conservative cost estimate, never for presentation numbering.
        return max(0, round((target - anchor) / ticks))

    def _set_h264_scrub_index(self) -> None:
        """Check inexpensive stream properties before reverse GOP prediction."""
        self._h264_scrub_ticks = 0
        self._h264_scrub_nal_length = 0
        self._h264_scrub_index_token = object()
        stream, info = self._stream, self._info
        if info is None or info.codec_name != 'h264' or info.codec_tag != 'avc1':
            return
        config = info.extradata
        if not config or len(config) < 7 or config[0] != 1:
            return
        length_size = (config[4] & 3) + 1
        if length_size == 3:
            return
        format_names = getattr(getattr(self._container, 'format', None), 'name', '')
        if 'mov' not in format_names.split(',') or getattr(stream, 'start_time', None) != 0:
            return
        if getattr(stream.codec_context, 'has_b_frames', None) is not False:
            return
        entries = getattr(stream, 'index_entries', None)
        rate = getattr(stream, 'average_rate', None)
        time_base = getattr(stream, 'time_base', None)
        if (not rate or rate != getattr(stream, 'base_rate', None) or not time_base
                or entries is None or len(entries) != info.total_frames or len(entries) < 2):
            return
        ticks = 1 / (Fraction(rate) * Fraction(time_base))
        if ticks.denominator != 1 or ticks <= 0 or entries[0].timestamp != 0:
            return
        self._h264_scrub_ticks = ticks.numerator
        self._h264_scrub_nal_length = length_size

    def supports_h264_scrub_gops(self) -> bool:
        """Return whether this open container has an eligible AVC sample index."""
        return self._h264_scrub_ticks > 0

    def h264_scrub_pts_matches(self, frame: int, pts: int | None) -> bool:
        """Check an exact zero-origin AVC timestamp in the open stream."""
        return bool(self._h264_scrub_ticks and self._info and 0 <= frame < self._info.total_frames
                    and pts == frame * self._h264_scrub_ticks)

    def previous_h264_scrub_gop(self, head_frame: int) -> H264ScrubGop | None:
        """Find the indexed sync interval immediately before the head's GOP."""
        ticks = self._h264_scrub_ticks
        if not ticks or not self._info or not 0 < head_frame < self._info.total_frames:
            return None
        entries = self._stream.index_entries
        stop = entries.search_timestamp(head_frame * ticks, backward=True, any_frame=False)
        if not 0 < stop <= head_frame or not entries[stop].is_keyframe:
            return None
        first = entries.search_timestamp(stop * ticks - 1, backward=True, any_frame=False)
        if not 0 <= first < stop or stop - first > self._MAX_H264_SCRUB_GOP_SAMPLES:
            return None
        positions = []
        for frame in range(first, stop + 1):
            entry = entries[frame]
            if (entry.timestamp != frame * ticks or entry.is_keyframe != (frame in (first, stop))
                    or entry.pos is None or entry.pos < 0 or (positions and entry.pos <= positions[-1])):
                return None
            positions.append(entry.pos)
        return H264ScrubGop(first, stop, ticks, tuple(positions[:-1]), self._h264_scrub_index_token)

    def h264_scrub_gop_packets(
        self, gop: H264ScrubGop, stop_frame: int,
    ) -> Generator[CompressedPacket, None, None]:
        """Seek to the exact index timestamp and stop at the first invalid packet."""
        samples = gop.stop_frame - gop.first_frame
        if (not self._h264_scrub_ticks or gop._index_token is not self._h264_scrub_index_token
                or not 0 < samples <= self._MAX_H264_SCRUB_GOP_SAMPLES or len(gop._positions) != samples
                or not gop.first_frame < stop_frame <= gop.stop_frame):
            return
        from av.error import FFmpegError

        try:
            self._container.seek(gop.first_frame * gop._ticks, stream=self._stream,
                                 backward=True, any_frame=False)
            self._frame_index = gop.first_frame
            for offset, packet in enumerate(self.packets(
                max_packets=stop_frame - gop.first_frame, include_sample_identity=True,
            )):
                frame = gop.first_frame + offset
                pts = frame * gop._ticks
                if (packet.frame_index != frame or packet.position != gop._positions[offset]
                        or packet.pts != pts or packet.dts != pts
                        or packet.duration != gop._ticks or packet.has_new_extradata
                        or packet.is_keyframe != (offset == 0)
                        or (offset == 0 and not self._is_avcc_idr(packet.data, self._h264_scrub_nal_length))):
                    return
                yield packet
        except (FFmpegError, OSError, ValueError) as error:
            logger.debug('Indexed AVC scrub seek failed: %s', error)

    @staticmethod
    def _is_avcc_idr(data: bytes, length_size: int) -> bool:
        """Check complete AVCC NAL framing and require an IDR picture."""
        offset = 0
        found_idr = False
        while offset + length_size < len(data):
            header = offset + length_size
            end = header + int.from_bytes(data[offset:header], 'big')
            if end <= header or end > len(data):
                return False
            nal_type = data[header] & 31
            if nal_type == 5:
                found_idr = True
            elif not 6 <= nal_type <= 12:
                return False
            offset = end
        return found_idr and offset == len(data)

    def _single_layer_hevc_nal_length_size(self, path: str) -> int:
        """Enable packet omission only for a verified, fixed HEVC sample entry."""
        info = self._info
        config = info.extradata if info is not None and info.codec_tag == 'hvc1' else None
        if not config or len(config) < 23 or config[0] != 1:
            return 0
        # A non-reference picture may still be referenced by a higher
        # temporal layer. Main/Main 10 with one temporal layer excludes it.
        profile = config[1] & 0x1f
        temporal_layers = (config[21] >> 3) & 7
        length_size = (config[21] & 3) + 1
        if profile not in (1, 2) or temporal_layers != 1 or length_size == 3:
            return 0
        if not self._has_fixed_hevc_sample_entry(path, config):
            return 0
        return length_size

    @staticmethod
    def _has_fixed_hevc_sample_entry(path: str, config: bytes) -> bool:
        try:
            with open(path, 'rb') as source:
                file_size = source.seek(0, 2)
                movie = None
                last_end = 0
                for name, start, end in iter_file_mov_atoms(source, file_size):
                    last_end = end
                    if name == b'moof' or (name == b'moov' and movie is not None):
                        return False
                    if name == b'moov':
                        movie = (start, end)
                if movie is None or last_end != file_size:
                    return False
                size = movie[1] - movie[0]
                if not 0 < size <= 32 * 1024 * 1024:
                    return False
                source.seek(movie[0])
                data = source.read(size)
        except (OSError, ValueError):
            return False
        return len(data) == size and PacketDemuxer._verify_hevc_sample_entry(data, config)

    @staticmethod
    def _verify_hevc_sample_entry(data: bytes, config: bytes) -> bool:
        video_descriptions = []
        for name, start, end in iter_mov_atoms(data, 0, len(data)):
            if name == b'mvex':
                return False
            if name != b'trak':
                continue
            media = find_mov_atom(data, start, end, b'mdia')
            handler = find_mov_atom(data, *media, b'hdlr') if media else None
            if not handler or handler[1] - handler[0] < 12:
                return False
            if data[handler[0] + 8:handler[0] + 12] != b'vide':
                continue
            minf = find_mov_atom(data, *media, b'minf')
            stbl = find_mov_atom(data, *minf, b'stbl') if minf else None
            stsd = find_mov_atom(data, *stbl, b'stsd') if stbl else None
            video_descriptions.append(stsd)
        if len(video_descriptions) != 1 or not video_descriptions[0]:
            return False
        start, end = video_descriptions[0]
        if end - start < 8 or data[start:start + 8] != b'\x00\x00\x00\x00\x00\x00\x00\x01':
            return False
        entries = list(iter_mov_atoms(data, start + 8, end))
        if len(entries) != 1:
            return False
        name, start, entry_end = entries[0]
        if name != b'hvc1' or entry_end != end or entry_end - start < 78:
            return False
        # A VisualSampleEntry has 78 fixed bytes before its child boxes.
        children = list(iter_mov_atoms(data, start + 78, entry_end))
        if not children or children[-1][2] != entry_end:
            return False
        if any(name in (b'lhvC', b'dvcC', b'dvvC', b'dvwC') for name, _, _ in children):
            return False
        return [data[s:e] for name, s, e in children if name == b'hvcC'] == [config]

    def is_disposable_hevc_sample(self, packet: CompressedPacket) -> bool:
        """Recognize an hvc1 sample containing only single-layer TRAIL_N slices."""
        length_size = self._hevc_nal_length_size
        if packet.is_keyframe or not length_size:
            return False
        data = packet.data
        position = 0
        while position + length_size + 3 <= len(data):
            header = position + length_size
            size = int.from_bytes(data[position:header], 'big')
            end = header + size
            # Only type 0 (TRAIL_N), layer 0, and temporal_id_plus1 == 1.
            # Parameter sets, SEI and every other NAL stay on the decode path.
            if size < 3 or end > len(data) or data[header:header + 2] != b'\x00\x01':
                return False
            position = end
        return position > 0 and position == len(data)

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
