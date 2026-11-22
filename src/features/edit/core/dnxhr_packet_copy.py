"""Copy frame-aligned DNxHR HQX edits into a video-only QuickTime movie."""

from __future__ import annotations

import os
from fractions import Fraction
from pathlib import Path
from tempfile import mkstemp
from typing import TYPE_CHECKING, Callable, Iterator, Sequence

if TYPE_CHECKING:
    import av


class DnxhrPacketCopyUnsupported(ValueError):
    """The source cannot satisfy the requested export by copying packets."""


def _video_stream(
    container: av.container.InputContainer,
    width: int,
    height: int,
    fps: Fraction,
) -> tuple[av.VideoStream, int]:
    if ("mov" not in container.format.name.split(",")
            or container.metadata.get("major_brand", "").strip() != "qt"
            or len(container.streams.video) != 1):
        raise DnxhrPacketCopyUnsupported("Expected one video stream in a QuickTime movie")

    stream = container.streams.video[0]
    codec = stream.codec_context
    if (codec.name != "dnxhd" or codec.profile != "DNXHR HQX"
            or codec.codec_tag != "AVdh"
            or getattr(codec.format, "name", None) != "yuv422p10le"
            or (codec.width, codec.height) != (width, height)):
        raise DnxhrPacketCopyUnsupported("Expected matching 10-bit DNxHR HQX video")

    # FFmpeg uses 1 for BT.709 primaries/transfer/matrix and MPEG (limited) range.
    if (codec.color_primaries, codec.color_trc, codec.colorspace, codec.color_range) != (1, 1, 1, 1):
        raise DnxhrPacketCopyUnsupported("Expected limited-range BT.709 video")
    if stream.sample_aspect_ratio != 1 or codec.field_order not in (0, 1):
        raise DnxhrPacketCopyUnsupported("Expected progressive, square-pixel video")

    if (fps <= 0 or stream.average_rate != fps or stream.base_rate != fps
            or stream.start_time != 0 or not stream.time_base):
        raise DnxhrPacketCopyUnsupported("Expected matching, zero-based constant frame rate")
    frame_ticks = 1 / (fps * stream.time_base)
    if frame_ticks.denominator != 1:
        raise DnxhrPacketCopyUnsupported("Frame duration is not exact in the source time base")
    return stream, int(frame_ticks)


def _source_packets(
    container: av.container.InputContainer,
    stream: av.VideoStream,
    start_frame: int,
    frame_ticks: int,
) -> Iterator[av.Packet]:
    import av

    try:
        container.seek(start_frame * frame_ticks, stream=stream,
                       backward=True, any_frame=False)
        yield from container.demux(stream)
    except av.error.FFmpegError as error:
        raise DnxhrPacketCopyUnsupported("Unable to read a DNxHR source segment") from error


def _check_first_frame(packet: av.Packet, width: int, height: int) -> None:
    """Check scan and display geometry that may be absent from stream metadata."""
    import av

    try:
        frames = packet.decode()
    except av.error.FFmpegError as error:
        raise DnxhrPacketCopyUnsupported("Unable to inspect the first DNxHR frame") from error
    if len(frames) != 1:
        raise DnxhrPacketCopyUnsupported("Expected one picture per DNxHR packet")
    frame = frames[0]
    if ((frame.width, frame.height, frame.format.name) != (width, height, "yuv422p10le")
            or frame.interlaced_frame or frame.is_corrupt
            or getattr(frame, "rotation", None) != 0
            or any(data.type.name == "DISPLAYMATRIX" for data in frame.side_data)):
        raise DnxhrPacketCopyUnsupported("Expected one progressive frame with no display transform")


def copy_dnxhr_hqx_mov(
    source: str,
    target: str,
    runs: Sequence[tuple[int, int]],
    *,
    width: int,
    height: int,
    fps: Fraction,
    continue_export: Callable[[], bool],
    progress: Callable[[int], None],
) -> bool:
    """Copy runs of (source start frame, frame count) and publish atomically.

    Calls ``progress`` with the number of muxed video frames. The last frame
    is reported after publication. Return False on cancellation. A source
    mismatch permits normal rendering, while output I/O failures propagate.
    """
    import av

    source_path, target_path = Path(source), Path(target)
    if (source_path.suffix.lower() != ".mov" or target_path.suffix.lower() != ".mov"
            or not source_path.is_file()):
        raise DnxhrPacketCopyUnsupported("Expected a local MOV source and MOV destination")
    if target_path.exists() and source_path.samefile(target_path):
        raise ValueError("Export destination is the source movie")
    if not runs or any(start < 0 or count <= 0 for start, count in runs):
        raise DnxhrPacketCopyUnsupported("Expected nonempty frame-aligned source runs")
    if (not hasattr(av.container.OutputContainer, "add_stream_from_template")
            or not hasattr(av.Packet, "iter_sidedata")):
        raise DnxhrPacketCopyUnsupported("This PyAV version cannot copy and validate stream templates")
    if not continue_export():
        return False

    try:
        source_movie = av.open(str(source_path))
    except av.error.FFmpegError as error:
        raise DnxhrPacketCopyUnsupported("Unable to open the DNxHR source movie") from error

    with source_movie:
        stream, frame_ticks = _video_stream(source_movie, width, height, fps)
        if not stream.frames or any(start + count > stream.frames for start, count in runs):
            raise DnxhrPacketCopyUnsupported("A requested frame lies outside the source movie")
        total = sum(count for _, count in runs)
        descriptor, temporary_name = mkstemp(
            prefix=f".{target_path.stem}.", suffix=".topos-partial", dir=target_path.parent,
        )
        temporary = Path(temporary_name)
        try:
            os.close(descriptor)
            written = 0
            packet_size = None
            with av.open(str(temporary), mode="w", format="mov",
                         options={"write_tmcd": "0", "movflags": "write_colr"}) as output:
                video = output.add_stream_from_template(stream, opaque=True)
                video.time_base = stream.time_base
                video.metadata.pop("timecode", None)
                for start, count in runs:
                    packets = _source_packets(source_movie, stream, start, frame_ticks)
                    for frame_number in range(start, start + count):
                        if not continue_export():
                            return False
                        packet = next(packets, None)
                        expected_pts = frame_number * frame_ticks
                        if (packet is None or not packet.size or not packet.is_keyframe
                                or packet.is_corrupt or packet.time_base != stream.time_base
                                or (packet.pts, packet.dts) != (expected_pts, expected_pts)
                                or packet.duration != frame_ticks
                                or next(packet.iter_sidedata(), None) is not None):
                            raise DnxhrPacketCopyUnsupported(
                                f"Invalid DNxHR packet at source frame {frame_number}")
                        if packet_size is None:
                            _check_first_frame(packet, width, height)
                            packet_size = packet.size
                        if packet.size != packet_size:
                            raise DnxhrPacketCopyUnsupported("Inconsistent DNxHR packet size")
                        packet.stream = video
                        packet.time_base = stream.time_base
                        packet.pts = packet.dts = written * frame_ticks
                        output.mux(packet)
                        written += 1
                        if written < total:
                            progress(written)
            if not continue_export():
                return False
            os.replace(temporary, target_path)
        finally:
            temporary.unlink(missing_ok=True)

    progress(total)
    return True
