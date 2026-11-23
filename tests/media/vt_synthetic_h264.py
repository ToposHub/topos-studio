"""Synthetic VideoToolbox-shaped H.264 packets for muxer timing tests.

libx264 emits Annex-B packets and keeps SPS/PPS in ``extradata``; VideoToolbox
emits 4-byte length-prefixed NAL units and hands the parameter sets to the
packet sink out of band.  These helpers build that shape without a hardware
encoder, so timestamp conversion can be tested on any machine that has PyAV
with libx264.  The container still opens the real ``h264_videotoolbox``
stream, which needs a VideoToolbox encoder; see
:func:`videotoolbox_muxer_available`.
"""

from __future__ import annotations

import contextlib
import io
import os
from dataclasses import dataclass
from fractions import Fraction

import av
import numpy as np

from src.shared.media.macos_vt.vt_compression import (
    VideoToolboxEncodedPacket,
    VideoToolboxPacketMuxer,
)

# A size the hardware encoder accepts when the muxer opens the codec context.
MUXER_TEST_SIZE = (1920, 1080)


def annexb_nalus(data: bytes) -> list[bytes]:
    """Split an Annex-B byte stream into NAL units without start codes."""
    units: list[bytes] = []
    start: int | None = None
    index = 0
    size = len(data)
    while index + 2 < size:
        if data[index] == 0 and data[index + 1] == 0 and (
                data[index + 2] == 1
                or (data[index + 2] == 0 and index + 3 < size and data[index + 3] == 1)):
            if start is not None:
                units.append(data[start:index])
            index += 4 if data[index + 2] == 0 else 3
            start = index
        else:
            index += 1
    if start is not None:
        units.append(data[start:])
    return units


def to_length_prefixed(data: bytes, nal_length_size: int = 4) -> bytes:
    """Rewrite Annex-B start codes as big-endian NAL length prefixes."""
    return b"".join(
        len(unit).to_bytes(nal_length_size, "big") + unit
        for unit in annexb_nalus(data)
    )


@dataclass(frozen=True)
class SyntheticPacket:
    """One encoded access unit in encoder time base ``frame_ticks / timescale``."""

    data: bytes
    pts: int
    dts: int
    duration: int
    keyframe: bool


@dataclass(frozen=True)
class SyntheticH264:
    packets: tuple[SyntheticPacket, ...]
    parameter_sets: tuple[bytes, ...]
    timescale: int

    def vt_packet(
        self,
        index: int,
        *,
        dts_timescale: int | None = None,
        duration_timescale: int | None = None,
        scale: int = 1,
    ) -> VideoToolboxEncodedPacket:
        """Return packet ``index`` as VideoToolbox hands it to the packet sink.

        PTS stays in the packet timescale.  DTS and duration can be given
        their own, ``scale`` times finer timescale, naming the same instants.
        """
        packet = self.packets[index]
        return VideoToolboxEncodedPacket(
            data=packet.data,
            codec="h264",
            pts=packet.pts,
            dts=packet.dts * scale if dts_timescale else packet.dts,
            duration=packet.duration * scale if duration_timescale else packet.duration,
            timescale=self.timescale,
            keyframe=packet.keyframe,
            parameter_sets=self.parameter_sets,
            nal_unit_length_size=4,
            dts_timescale=dts_timescale,
            duration_timescale=duration_timescale,
        )

    def tick_packet(
        self, frame: int, *, keyframe: bool = True, payload_index: int = 0,
    ) -> VideoToolboxEncodedPacket:
        """One payload stamped at ``frame`` with the sequence's own frame ticks.

        The muxer never decodes, so long timelines can reuse a single access
        unit to measure timestamp arithmetic alone.
        """
        packet = self.packets[payload_index % len(self.packets)]
        return VideoToolboxEncodedPacket(
            data=packet.data,
            codec="h264",
            pts=frame * packet.duration,
            dts=frame * packet.duration,
            duration=packet.duration,
            timescale=self.timescale,
            keyframe=keyframe,
            parameter_sets=self.parameter_sets,
            nal_unit_length_size=4,
        )


def synthetic_h264(
    count: int,
    *,
    bframes: int = 0,
    timescale: int = 25,
    frame_ticks: int = 1,
) -> SyntheticH264:
    """Encode ``count`` tiny frames with libx264 and return VT-shaped packets.

    The result is a closed, decodable sequence in decode order.  ``bframes``
    selects a fixed number of bi-directional frames between references, so
    PTS differs from DTS on demand.  Decode timestamps start at zero: x264
    reports its reorder delay as negative DTS, which VideoToolbox never does,
    so every stamp is shifted by the same constant.
    """
    # ``bf`` is FFmpeg's generic B-frame option; x264's private spelling
    # ``bframes`` is silently ignored here.  ``b-adapt=0`` pins the count.
    options = {"crf": "30", "bf": str(bframes), "keyint": "12", "threads": "1"}
    if bframes:
        options["b-adapt"] = "0"
    level = av.logging.get_level()
    av.logging.set_level(av.logging.PANIC)
    try:
        container = av.open(os.devnull, "w", format="mp4")
        try:
            stream = container.add_stream(
                "libx264", rate=Fraction(timescale, frame_ticks), options=options)
            stream.width = 64
            stream.height = 64
            stream.pix_fmt = "yuv420p"
            encoded: list[av.Packet] = []
            noise = np.random.default_rng(3)
            luma = noise.integers(16, 235, size=(64, 64), dtype=np.uint8)
            chroma = noise.integers(64, 192, size=(32, 64), dtype=np.uint8)
            for index in range(count):
                # yuv420p input is (h * 3 / 2, w): the two 32x32 chroma
                # planes are laid out as 16 rows of the 64-byte frame width.
                shifted = np.roll(luma, index * 3, axis=1)
                plane = av.VideoFrame.from_ndarray(
                    np.vstack([shifted, chroma]), format="yuv420p")
                plane.pts = index
                plane.time_base = Fraction(frame_ticks, timescale)
                encoded.extend(stream.encode(plane))
            encoded.extend(stream.encode(None))
            extradata = bytes(stream.codec_context.extradata)
        finally:
            container.close()
    finally:
        av.logging.set_level(level)
    units = annexb_nalus(extradata)
    parameter_sets = (
        next(unit for unit in units if unit[0] & 0x1F == 7),
        next(unit for unit in units if unit[0] & 0x1F == 8),
    )
    encoded = [packet for packet in encoded if packet.size > 0]
    if len(encoded) != count:
        raise RuntimeError(f"libx264 produced {len(encoded)} packets, expected {count}")
    shift = -min(int(packet.dts) for packet in encoded)
    packets = tuple(
        SyntheticPacket(
            data=to_length_prefixed(bytes(packet)),
            pts=(int(packet.pts) + shift) * frame_ticks,
            dts=(int(packet.dts) + shift) * frame_ticks,
            duration=frame_ticks,
            keyframe=bool(packet.is_keyframe),
        )
        for packet in encoded
    )
    return SyntheticH264(packets=packets, parameter_sets=parameter_sets, timescale=timescale)


def muxer_for(path: str, **settings: object) -> VideoToolboxPacketMuxer:
    """A packet muxer for the hardware-acceptable test size."""
    return VideoToolboxPacketMuxer(
        path, width=MUXER_TEST_SIZE[0], height=MUXER_TEST_SIZE[1],
        bt709_limited=True, **settings,  # type: ignore[arg-type]
    )


def videotoolbox_muxer_available(path: str) -> bool:
    """Whether this machine can open the muxer's ``h264_videotoolbox`` stream.

    The MOV muxer asks FFmpeg to open the VideoToolbox encoder for the stream
    even though packets arrive pre-encoded.  Machines without a hardware
    encoder for the test size refuse it, and the timing tests then skip.
    """
    source = synthetic_h264(1)
    try:
        with contextlib.redirect_stderr(io.StringIO()):
            muxer = muxer_for(path)
            try:
                muxer.write_packet(source.vt_packet(0))
            finally:
                muxer.abort()
    except Exception:  # noqa: BLE001 - capability probe; any refusal means skip
        return False
    return True


def frames_to_samples(frames: int, fps: Fraction, sample_rate: int) -> int:
    """Frame count to sample count with exact rational arithmetic."""
    return int(Fraction(frames * sample_rate) / fps)
