"""Plan 3.1/3.2: exact timestamps and in-container audio for the VideoToolbox muxer.

Covers the packet muxer and compression session directly:

* frame rates stay exact rationals (24000/1001, 30000/1001) through the
  session's CoreMedia time base and the muxer's stream rate;
* CoreMedia PTS, DTS and duration are each converted from their own timescale;
* audio frames carry cumulative sample-count PTS, so the AAC priming edit list
  matches the regular software encoder packet for packet;
* a declared ``required`` audio track blocks publication when no sample arrives.

The muxer opens the real ``h264_videotoolbox`` stream through PyAV.  Machines
without a VideoToolbox encoder skip those cases; the pure arithmetic and the
fake-backed session/bridge cases run everywhere.
"""

from __future__ import annotations

import ctypes
import sys
import threading
from fractions import Fraction
from types import SimpleNamespace
from typing import Any, Callable

import numpy as np
import pytest

from src.shared.export.gpu_encode_surface import (
    GpuEncodeSurface,
    GpuEncodeSurfaceDescriptor,
)
from src.shared.media.gpu_frame import GpuFramePlane, GpuFrameRelease, GpuPlaneResource
from src.shared.media.macos_vt import vt_compression as vc
from src.shared.media.macos_vt.vt_compression import (
    VideoToolboxCompressionError,
    VideoToolboxCompressionSession,
    VideoToolboxEncodedPacket,
    coerce_frame_rate,
    rescale_coremedia_timing,
)
from tests.media.vt_synthetic_h264 import (
    SyntheticH264,
    frames_to_samples,
    muxer_for,
    synthetic_h264,
    videotoolbox_muxer_available,
)

MACOS_ONLY = pytest.mark.skipif(sys.platform != "darwin", reason="VideoToolbox is macOS only")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def vt_output_dir(tmp_path):
    """A directory for muxed files, or skip when the VT stream cannot be opened."""
    probe = tmp_path / ".probe.mp4"
    if not videotoolbox_muxer_available(str(probe)):
        pytest.skip("VideoToolbox H.264 encoder is unavailable for the muxer test size")
    probe.unlink(missing_ok=True)
    return tmp_path


def _skip_samples(packet: Any) -> tuple[int, int] | None:
    """``(skip_samples, discard_padding)`` of a packet, or None."""
    for sidedata in packet.iter_sidedata():
        if sidedata.data_type == "skip_samples":
            raw = ctypes.string_at(sidedata.buffer_ptr, sidedata.buffer_size)
            skip, discard = int.from_bytes(raw[0:4], "little"), int.from_bytes(raw[4:8], "little")
            return skip, discard
    return None


def _audio_packets(path):
    """Audio packets as ``(pts, dts, duration, skip, payload)`` in stream order."""
    import av

    with av.open(str(path)) as container:
        stream = container.streams.audio[0]
        return [
            (packet.pts, packet.dts, packet.duration, _skip_samples(packet), bytes(packet))
            for packet in container.demux(stream)
            if packet.size > 0
        ]


def _decode_audio(path) -> np.ndarray:
    """Decode the first audio stream manually into ``(channels, samples)``."""
    import av

    with av.open(str(path)) as container:
        stream = container.streams.audio[0]
        context = stream.codec_context
        chunks: list[np.ndarray] = []
        for packet in container.demux(stream):
            if packet.size:
                chunks.extend(frame.to_ndarray() for frame in context.decode(packet))
        try:
            chunks.extend(frame.to_ndarray() for frame in context.decode(None))
        except av.error.EOFError:  # pragma: no cover - already drained
            pass
    return np.concatenate(chunks, axis=1) if chunks else np.zeros((0, 0), dtype=np.float32)


def _decode_video_frames(path) -> list[int]:
    """Presentation timestamps of decoded video frames, in output order."""
    import av

    with av.open(str(path)) as container:
        stream = container.streams.video[0]
        context = stream.codec_context
        stamps: list[int] = []
        for packet in container.demux(stream):
            if packet.size:
                stamps.extend(frame.pts for frame in context.decode(packet))
        try:
            stamps.extend(frame.pts for frame in context.decode(None))
        except av.error.EOFError:  # pragma: no cover - already drained
            pass
    return stamps


#: The programme stays silent for half a second, then starts: an onset at a
#: known sample is what exposes a displaced track.
_ONSET = 24000
#: A short 440 Hz marker follows the onset before the steady tone.  A steady
#: tone alone correlates equally well with itself at every period (48 samples
#: at 1 kHz), which would hide a displaced track.
_MARKER = 12000
#: Per-channel steady ``(frequency, amplitude)`` and marker amplitude.
_SHAPES = ((1000.0, 0.5, 0.8), (500.0, 0.25, 0.4))


def _tone(channels: int, samples: int, sample_rate: int = 48000) -> np.ndarray:
    """Silence until ``_ONSET``, a 440 Hz marker, then one steady tone per channel."""
    time_axis = np.arange(samples, dtype=np.float64) / sample_rate
    marker_phase = 2 * np.pi * 440.0 * time_axis
    planes = []
    for frequency, amplitude, marker_amplitude in _SHAPES[:channels]:
        plane = np.zeros(samples, dtype=np.float64)
        plane[_ONSET:_ONSET + _MARKER] = marker_amplitude * np.sin(marker_phase[_ONSET:_ONSET + _MARKER])
        plane[_ONSET + _MARKER:] = amplitude * np.sin(
            2 * np.pi * frequency * time_axis[_ONSET + _MARKER:])
        planes.append(plane.astype(np.float32))
    return np.stack(planes)


def _max_error(decoded: np.ndarray, ideal: np.ndarray) -> float:
    """Largest sample difference once the steady tone has settled."""
    settled = slice(_ONSET + _MARKER + 1024, ideal.shape[1])
    return float(np.abs(decoded[:, settled] - ideal[:, settled]).max())


def _onset_lag(decoded: np.ndarray, ideal: np.ndarray, channel: int) -> int:
    """Displacement in samples of the programme onset, by cross-correlation."""
    window = slice(_ONSET, _ONSET + 24000)
    return max(
        range(-300, 301),
        key=lambda lag: float(np.dot(
            decoded[channel, window.start + lag:window.stop + lag],
            ideal[channel, window])),
    )


def _fake_surface(pts: int, frame_index: int, duration_seconds: float | None = None) -> GpuEncodeSurface:
    width, height = 64, 32
    planes = tuple(
        GpuFramePlane(
            component=component, api="metal", texture_id=0, target="2d",
            width=width if component == "Y" else width // 2,
            height=height if component == "Y" else height // 2,
            resource=GpuPlaneResource(
                api="metal", native_handle=0,
                resource_format="r8unorm" if component == "Y" else "rg8unorm",
                width=width if component == "Y" else width // 2,
                height=height if component == "Y" else height // 2),
        )
        for component in ("Y", "UV")
    )
    return GpuEncodeSurface(
        GpuEncodeSurfaceDescriptor(
            backend="videotoolbox", pixel_format="nv12", width=width, height=height,
            bit_depth=8, device_identity="test", planes=planes, pts=pts,
            frame_index=frame_index, native_handle=0x1000,
            duration_seconds=duration_seconds),
        GpuFrameRelease(), lambda _surface: None)


class _RecordingVtLib:
    """Stands in for the VideoToolbox C library and records CMTime arguments."""

    def __init__(self) -> None:
        self.encode_calls: list[tuple[int, int, int]] = []
        self.complete_calls: list[int] = []

    def VTCompressionSessionEncodeFrame(self, session, handle, pts, duration, props, ref_con, flags_out):
        self.encode_calls.append((int(pts.value), int(pts.timescale), int(duration.value)))
        return 0

    def VTCompressionSessionCompleteFrames(self, session, until) -> int:
        self.complete_calls.append(int(until.timescale))
        return 0

    def VTCompressionSessionInvalidate(self, session) -> None:
        return None


def _session_without_native(fps: Any) -> VideoToolboxCompressionSession:
    """A session wired for ``submit``/``complete`` without a real encoder."""
    session = VideoToolboxCompressionSession.__new__(VideoToolboxCompressionSession)
    session.width, session.height = 64, 32
    session.fps = fps
    session.codec = "h264"
    session._pixel_format = "nv12"
    session._on_packet = None
    session._lock = threading.RLock()
    session._session_lock = threading.Lock()
    session._pending = {}
    session._next_submission_id = 1
    session._last_error = ""
    session._closed = False
    session._dropped_packets = 0
    session._session = ctypes.c_void_p(0x1234)
    session._callback = None
    session._ref_pointer = None
    session._ref_object = None
    return session


# ---------------------------------------------------------------------------
# exact frame rates
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value, expected", [
    (25, Fraction(25)),
    (Fraction(30000, 1001), Fraction(30000, 1001)),
    (SimpleNamespace(num=24000, den=1001), Fraction(24000, 1001)),
    (25.0, Fraction(25)),
    (np.int64(50), Fraction(50)),
])
def test_coerce_frame_rate_keeps_exact_rates(value, expected) -> None:
    assert coerce_frame_rate(value) == expected
    assert isinstance(coerce_frame_rate(value), Fraction)


@pytest.mark.parametrize("value", [29.97, 23.976, 0, -25, Fraction(0, 1), True, "25", object(),
                                   Fraction(1 << 31, 1)])
def test_coerce_frame_rate_rejects_inexact_or_invalid_rates(value) -> None:
    # A float frame rate must not be truncated into a neighbouring integer rate.
    with pytest.raises(VideoToolboxCompressionError, match="frame_rate"):
        coerce_frame_rate(value)


def test_rescale_coremedia_timing_converts_each_component_from_its_own_timescale() -> None:
    # Frame 2 of a 30000/1001 timeline: PTS in the frame timescale, duration in microseconds.
    packet = VideoToolboxEncodedPacket(
        data=b"", codec="h264", pts=2002, dts=1001, duration=33367, timescale=30000,
        keyframe=False, duration_timescale=1_000_000)
    time_base, pts, dts, duration = rescale_coremedia_timing(packet, Fraction(30000, 1001))
    assert (time_base, pts, dts, duration) == (Fraction(1, 3_000_000), 200200, 100100, 100101)
    # 33367 us stays 33367 us: each value was scaled by its own timescale.
    assert Fraction(duration, 3_000_000) == Fraction(33367, 1_000_000)


def test_rescale_coremedia_timing_keeps_legacy_packets_and_invalid_times() -> None:
    legacy = VideoToolboxEncodedPacket(
        data=b"", codec="h264", pts=3, dts=2, duration=1, timescale=25, keyframe=False)
    assert rescale_coremedia_timing(legacy, Fraction(25)) == (Fraction(1, 25), 3, 2, 1)
    # No shared timescale: the frame-rate timescale is the fallback.
    bare = VideoToolboxEncodedPacket(
        data=b"", codec="h264", pts=4, dts=None, duration=None, timescale=None, keyframe=True)
    assert rescale_coremedia_timing(bare, Fraction(30000, 1001)) == (
        Fraction(1, 30000), 4, None, None)


def test_rescale_coremedia_timing_rejects_unrepresentable_time_bases() -> None:
    coarse = (1 << 30) - 1
    packet = VideoToolboxEncodedPacket(
        data=b"", codec="h264", pts=1, dts=1, duration=1, timescale=coarse,
        keyframe=True, duration_timescale=coarse - 1)
    with pytest.raises(VideoToolboxCompressionError, match="time_base_unrepresentable"):
        rescale_coremedia_timing(packet, Fraction(25))
    invalid = VideoToolboxEncodedPacket(
        data=b"", codec="h264", pts=1, dts=None, duration=None, timescale=-5, keyframe=True)
    with pytest.raises(VideoToolboxCompressionError, match="timescale_invalid"):
        rescale_coremedia_timing(invalid, Fraction(25))


@pytest.fixture
def fake_sessions(monkeypatch: pytest.MonkeyPatch):
    """Sessions wired to a recording stand-in for the VideoToolbox library.

    Teardown neutralizes every session before ``monkeypatch`` puts the real
    library back. A session that outlives a failing assertion would otherwise
    reach ``__del__`` with its placeholder handle and call the real C library
    with it, which crashes the test process instead of reporting the failure.
    """
    library = _RecordingVtLib()
    monkeypatch.setattr(vc, "_VT_LIB", library)
    sessions: list[VideoToolboxCompressionSession] = []

    def create(fps: Any) -> VideoToolboxCompressionSession:
        session = _session_without_native(fps)
        sessions.append(session)
        return session

    yield SimpleNamespace(library=library, create=create)
    for session in sessions:
        session._closed = True
        session._session = ctypes.c_void_p()
        session._pending.clear()


@MACOS_ONLY
def test_session_submits_exact_fractional_timestamps(fake_sessions) -> None:
    """Plan 3.2: 30000/1001 frames are 1001 ticks in a timescale of 30000."""
    library = fake_sessions.library
    session = fake_sessions.create(Fraction(30000, 1001))
    for frame in (0, 1, 2, 17981):
        session.submit(_fake_surface(frame, frame))
    assert library.encode_calls == [
        (0, 30000, 1001), (1001, 30000, 1001), (2002, 30000, 1001),
        (17981 * 1001, 30000, 1001)]
    session.complete()
    assert library.complete_calls == [30000]
    session.abort()


@MACOS_ONLY
def test_session_converts_a_source_frame_duration_into_frame_ticks(fake_sessions) -> None:
    session = fake_sessions.create(Fraction(25))
    session.submit(_fake_surface(3, 3, duration_seconds=0.08))   # two 25 fps frames
    session.submit(_fake_surface(4, 4, duration_seconds=0.0))    # too short: one tick
    assert fake_sessions.library.encode_calls == [(3, 25, 2), (4, 25, 1)]
    session.abort()


@MACOS_ONLY
def test_session_accepts_an_integer_fps_assigned_after_construction(fake_sessions) -> None:
    # Existing callers and tests assign ``session.fps`` as a plain int.
    session = fake_sessions.create(30)
    session.submit(_fake_surface(7, 7))
    assert fake_sessions.library.encode_calls == [(7, 30, 1)]
    session.abort()


@MACOS_ONLY
def test_session_rejects_an_inexact_rate_before_any_native_call(fake_sessions) -> None:
    with pytest.raises(VideoToolboxCompressionError, match="frame_rate_not_exact"):
        session = fake_sessions.create(29.97)
        session.submit(_fake_surface(0, 0))
    assert fake_sessions.library.encode_calls == []


# ---------------------------------------------------------------------------
# muxer: frame timing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fps, frames", [
    (Fraction(30000, 1001), 17982),   # ten minutes at 29.97
    (Fraction(24000, 1001), 14386),   # ten minutes at 23.976
])
def test_muxer_keeps_a_ten_minute_fractional_timeline_exact(vt_output_dir, fps, frames) -> None:
    """Plan 3.2: no end-of-file drift for 24000/1001 and 30000/1001."""
    import av

    timescale, ticks = fps.numerator, fps.denominator
    source = synthetic_h264(2, timescale=timescale, frame_ticks=ticks)
    path = vt_output_dir / "ten-minutes.mp4"
    muxer = muxer_for(str(path), fps=fps)
    for frame in range(frames):
        # An IDR first, then the smallest access unit: only timestamps matter.
        muxer.write_packet(source.tick_packet(
            frame, keyframe=frame % 250 == 0, payload_index=0 if frame == 0 else 1))
    assert muxer.packet_count == frames
    muxer.close()

    with av.open(str(path)) as container:
        stream = container.streams.video[0]
        assert stream.time_base == Fraction(1, timescale)
        assert stream.average_rate == fps
        stamps = [(packet.pts, packet.dts, packet.duration)
                  for packet in container.demux(stream) if packet.pts is not None]
        duration = stream.duration * stream.time_base
    assert len(stamps) == frames
    assert [(pts, dts) for pts, dts, _ in stamps] == [
        (frame * ticks, frame * ticks) for frame in range(frames)]
    assert {duration for _, _, duration in stamps} == {ticks}
    # The last frame starts exactly where the frame clock says and the stream
    # ends one frame later; nothing has accumulated over ten minutes.
    assert stamps[-1][0] == (frames - 1) * ticks
    assert duration == Fraction(frames * ticks, timescale)


@MACOS_ONLY
def test_muxer_integer_rate_output_is_unchanged(vt_output_dir) -> None:
    source = synthetic_h264(40)
    path = vt_output_dir / "integer.mp4"
    muxer = muxer_for(str(path), fps=25)
    for index in range(40):
        muxer.write_packet(source.vt_packet(index))
    muxer.close()
    import av

    with av.open(str(path)) as container:
        stream = container.streams.video[0]
        stamps = [(packet.pts, packet.dts, packet.duration)
                  for packet in container.demux(stream) if packet.pts is not None]
        assert (stream.time_base, stream.average_rate) == (Fraction(1, 12800), Fraction(25))
        assert stream.codec_context.color_primaries == stream.codec_context.color_trc == 1
        assert stream.codec_context.color_range == 1
    assert stamps == [(frame * 512, frame * 512, 512) for frame in range(40)]
    assert _decode_video_frames(path) == [frame * 512 for frame in range(40)]


@MACOS_ONLY
def test_muxer_preserves_bframe_presentation_order(vt_output_dir) -> None:
    """Plan 3.2: DTS stays in decode order and PTS keeps the display order."""
    import av

    fps, timescale, ticks = Fraction(30000, 1001), 30000, 1001
    source = synthetic_h264(64, bframes=3, timescale=timescale, frame_ticks=ticks)
    assert any(packet.pts != packet.dts for packet in source.packets)
    path = vt_output_dir / "bframes.mp4"
    muxer = muxer_for(str(path), fps=fps)
    for index in range(len(source.packets)):
        muxer.write_packet(source.vt_packet(index))
    muxer.close()

    with av.open(str(path)) as container:
        stream = container.streams.video[0]
        stamps = [(packet.pts, packet.dts, packet.duration)
                  for packet in container.demux(stream) if packet.pts is not None]
    # An edit list may move the whole track by a constant; relative timing is exact.
    shift = stamps[0][0] - source.packets[0].pts
    assert [(pts - shift, dts - shift) for pts, dts, _ in stamps] == [
        (packet.pts, packet.dts) for packet in source.packets]
    assert {duration for _, _, duration in stamps} == {ticks}
    assert [dts for _, dts, _ in stamps] == sorted(dts for _, dts, _ in stamps)
    decoded = _decode_video_frames(path)
    assert sorted(decoded) == sorted(packet.pts + shift for packet in source.packets)


@MACOS_ONLY
def test_muxer_converts_mixed_component_timescales(vt_output_dir) -> None:
    """Plan 3.2: PTS, DTS and duration arriving in different timescales stay aligned."""
    import av

    source = synthetic_h264(30, timescale=30000, frame_ticks=1001)
    path = vt_output_dir / "mixed.mp4"
    muxer = muxer_for(str(path), fps=Fraction(30000, 1001))
    for index in range(len(source.packets)):
        # DTS and duration come in a 100x finer timescale than PTS.
        muxer.write_packet(source.vt_packet(
            index, dts_timescale=3_000_000, duration_timescale=3_000_000, scale=100))
    muxer.close()
    with av.open(str(path)) as container:
        stream = container.streams.video[0]
        time_base = stream.time_base
        stamps = [(packet.pts, packet.dts, packet.duration)
                  for packet in container.demux(stream) if packet.pts is not None]
    assert time_base == Fraction(1, 30000)
    assert stamps == [(packet.pts, packet.dts, packet.duration) for packet in source.packets]


# ---------------------------------------------------------------------------
# muxer: audio
# ---------------------------------------------------------------------------


def _regular_export(path, pcm: np.ndarray, bitrate: int, channels: int) -> None:
    """Write the same PCM through the regular software encoder for reference."""
    from src.shared.codec.color_metadata_config import ColorMetadataConfig
    from src.shared.export.video_encoder import FFmpegEncoderConfig, FFmpegVideoEncoder
    from src.shared.media.frame import DecodedFrame
    from src.shared.media.metadata import Rational

    encoder = FFmpegVideoEncoder(str(path), FFmpegEncoderConfig(
        codec="h264", width=64, height=64, fps=Rational(25, 1), pix_fmt="yuv420p",
        color_metadata=ColorMetadataConfig.sdr(), crf=30, hwaccel=None, threads=1))
    encoder.open()
    try:
        encoder.add_audio_stream("aac", 48000, channels, bitrate)
        black = np.zeros((64, 64, 3), dtype=np.uint8)
        for index in range(3):
            encoder.encode_frame(DecodedFrame(
                frame_index=index, pts=index, time_seconds=index / 25, data=black,
                pixel_format="bgr24", width=64, height=64))
        encoder.write_audio_samples(pcm, 48000)
    finally:
        encoder.close()


@MACOS_ONLY
@pytest.mark.parametrize("channels, layout", [(2, "stereo"), (1, "mono")])
def test_muxer_audio_matches_the_regular_encoder_and_keeps_channel_order(
    vt_output_dir, channels, layout,
) -> None:
    """Plan 3.1/3.2: cumulative sample PTS give the regular export's AAC edit list."""
    import av

    samples = 48000 * 3 // 2            # 1.5 s
    pcm = _tone(channels, samples)
    source = synthetic_h264(3)
    path = vt_output_dir / f"audio-{layout}.mp4"
    muxer = muxer_for(str(path), fps=Fraction(25))
    muxer.add_audio_stream("aac", 48000, channels, 256, required=True)
    for index in range(3):
        muxer.write_packet(source.vt_packet(index))
    # Three chunks whose boundaries are not 1024-sample frames: PTS continue by count.
    offsets = (0, 20000, 60000, samples)
    next_sample = 0
    for start, stop in zip(offsets, offsets[1:]):
        next_sample = muxer.write_audio_samples(pcm[:, start:stop], 48000)
        assert next_sample == stop
    assert muxer.audio_samples_written == samples
    muxer.close()

    reference = vt_output_dir / f"regular-{layout}.mp4"
    _regular_export(reference, pcm, 256, channels)

    packets, expected = _audio_packets(path), _audio_packets(reference)
    assert packets == expected, "AAC payload and timing differ from the regular exporter"
    # AAC priming: the first packet starts one frame early and is skipped by an edit list.
    assert packets[0][0] == -1024 and packets[0][3] == (1024, 0)
    # Chunk boundaries off the 1024-sample AAC grid do not move a single sample.
    whole = vt_output_dir / f"audio-{layout}-single.mp4"
    muxer = muxer_for(str(whole), fps=Fraction(25))
    muxer.add_audio_stream("aac", 48000, channels, 256, required=True)
    muxer.write_audio_samples(pcm, 48000)
    muxer.close()
    assert _audio_packets(whole) == packets
    with av.open(str(path)) as container:
        stream = container.streams.audio[0]
        assert stream.start_time == 0
        # The programme is one sample clock long; the container reports the
        # final partial AAC frame only to frame granularity.
        assert abs(stream.duration * stream.time_base - Fraction(samples, 48000)) < Fraction(1024, 48000)
        assert (stream.codec_context.sample_rate, stream.codec_context.layout.name) == (48000, layout)

    decoded = _decode_audio(path)
    assert decoded.shape[1] >= samples
    # Identical AAC packets decode identically to the regular exporter.
    assert np.array_equal(decoded, _decode_audio(reference))
    # The lead-in is silent up to the frame that carries the onset.
    assert float(np.abs(decoded[:, :_ONSET - 1024]).max()) < 1e-6
    # Settled programme: one misplaced sample of the 1 kHz plane is an error
    # of 0.066; a swapped plane is an error of about 0.53.
    assert _max_error(decoded[:, :samples], pcm) < 0.04, "channel order or timing"
    assert _max_error(decoded[:, 1:samples + 1], pcm) > 0.04
    if channels == 2:
        assert _max_error(decoded[:, :samples], pcm[::-1]) > 0.2
    for channel in range(channels):
        assert _onset_lag(decoded, pcm, channel) == 0
        # The marker makes the onset unambiguous: a reference moved 48 samples
        # later (six periods of the steady tone) is reported 48 samples late.
        assert _onset_lag(decoded, np.roll(pcm, 48, axis=1), channel) == -48


@MACOS_ONLY
def test_muxer_silent_programme_still_produces_a_full_length_track(vt_output_dir) -> None:
    """Plan 3.1: a silent section is delivered as samples, not as a missing track."""
    import av

    samples = 48000 * 2
    source = synthetic_h264(2)
    path = vt_output_dir / "silence.mp4"
    muxer = muxer_for(str(path), fps=Fraction(25))
    muxer.add_audio_stream("aac", 48000, 2, 192, required=True)
    muxer.write_audio_samples(np.zeros((2, samples), dtype=np.float32), 48000)
    for index in range(2):
        muxer.write_packet(source.vt_packet(index))
    muxer.close()
    reference = vt_output_dir / "regular-silence.mp4"
    _regular_export(reference, np.zeros((2, samples), dtype=np.float32), 192, 2)
    assert _audio_packets(path) == _audio_packets(reference)
    with av.open(str(path)) as container:
        stream = container.streams.audio[0]
        assert stream.duration * stream.time_base == Fraction(samples, 48000)
    assert float(np.abs(_decode_audio(path)[:, :samples]).max()) < 1e-6


@MACOS_ONLY
def test_muxer_audio_and_video_share_one_clock_at_a_fractional_rate(vt_output_dir) -> None:
    """Plan 3.2: the sample clock and the 30000/1001 frame clock agree at the end."""
    import av

    fps = Fraction(30000, 1001)
    frames = 1798                                   # one minute
    samples = frames_to_samples(frames, fps, 48000)
    source = synthetic_h264(1, timescale=30000, frame_ticks=1001)
    path = vt_output_dir / "clock.mp4"
    muxer = muxer_for(str(path), fps=fps)
    muxer.add_audio_stream("aac", 48000, 2, 192, required=True)
    muxer.write_audio_samples(_tone(2, samples), 48000)
    for frame in range(frames):
        muxer.write_packet(source.tick_packet(frame, keyframe=frame % 250 == 0))
    muxer.close()

    with av.open(str(path)) as container:
        video, audio = container.streams.video[0], container.streams.audio[0]
        # One pass over the container: demuxing a stream reads to end of file.
        video_stamps, audio_stamps = [], []
        for packet in container.demux():
            if packet.size == 0 or packet.pts is None:
                continue
            if packet.stream is video:
                video_stamps.append(packet.pts)
            elif packet.stream is audio:
                audio_stamps.append((packet.pts, packet.duration))
        video_end = video.duration * video.time_base
        audio_end = audio.duration * audio.time_base
    assert video_stamps[0] == 0 and video_stamps[-1] == (frames - 1) * 1001
    assert audio_stamps[0][0] == -1024 and audio_stamps[-1][0] + audio_stamps[-1][1] == samples
    # Both tracks end on the same instant, to within the AAC frame granularity
    # the container reports.
    assert abs(video_end - Fraction(samples, 48000)) < Fraction(1, 1000)
    assert abs(audio_end - video_end) < Fraction(1, 1000)
    # Cross-track schedule: the audio frame holding a frame's first sample
    # covers that frame's presentation time.
    for frame in (0, 1, 299, 900, frames - 1):
        sample = frames_to_samples(frame, fps, 48000)
        start, duration = next(
            (pts, length) for pts, length in audio_stamps if pts <= sample < pts + length)
        assert Fraction(start, 48000) <= Fraction(frame * 1001, 30000) < Fraction(start + duration, 48000)
    assert [dts for dts in video_stamps] == sorted(video_stamps)


@MACOS_ONLY
def test_muxer_required_audio_never_finalizes_without_samples(vt_output_dir) -> None:
    """Plan 3.1: a delivery that asked for audio is not published without it."""
    import av

    source = synthetic_h264(2)
    missing = vt_output_dir / "missing.mp4"
    muxer = muxer_for(str(missing), fps=Fraction(25))
    muxer.add_audio_stream("aac", 48000, 2, 192, required=True)
    for index in range(2):
        muxer.write_packet(source.vt_packet(index))
    with pytest.raises(VideoToolboxCompressionError, match="audio_track_missing"):
        muxer.close()
    with pytest.raises(VideoToolboxCompressionError, match="muxer_closed"):
        muxer.write_audio_samples(np.zeros((2, 48000), dtype=np.float32), 48000)

    # Without ``required`` the historical contract holds: the empty stream is
    # dropped by the container and the video is finalized.
    optional = vt_output_dir / "optional.mp4"
    muxer = muxer_for(str(optional), fps=Fraction(25))
    muxer.add_audio_stream("aac", 48000, 2, 192)
    for index in range(2):
        muxer.write_packet(source.vt_packet(index))
    muxer.close()
    with av.open(str(optional)) as container:
        assert [stream.type for stream in container.streams] == ["video"]


@MACOS_ONLY
def test_muxer_audio_must_match_the_declared_stream(vt_output_dir) -> None:
    muxer = muxer_for(str(vt_output_dir / "declared.mp4"), fps=Fraction(25))
    with pytest.raises(VideoToolboxCompressionError, match="audio_stream_not_configured"):
        muxer.write_audio_samples(np.zeros((2, 1024), dtype=np.float32), 48000)
    muxer.add_audio_stream("aac", 48000, 2, 192, required=True)
    with pytest.raises(VideoToolboxCompressionError, match="sample_rate_mismatch:44100"):
        muxer.write_audio_samples(np.zeros((2, 1024), dtype=np.float32), 44100)
    with pytest.raises(VideoToolboxCompressionError, match="channel_mismatch:1"):
        muxer.write_audio_samples(np.zeros((1, 1024), dtype=np.float32), 48000)
    with pytest.raises(VideoToolboxCompressionError, match="already_added"):
        muxer.add_audio_stream("aac", 48000, 2, 192)
    muxer.abort()


@MACOS_ONLY
def test_muxer_audio_stream_must_be_declared_before_video(vt_output_dir) -> None:
    source = synthetic_h264(1)
    muxer = muxer_for(str(vt_output_dir / "order.mp4"), fps=Fraction(25))
    muxer.write_packet(source.vt_packet(0))
    with pytest.raises(VideoToolboxCompressionError, match="before_video_mux"):
        muxer.add_audio_stream("aac", 48000, 2, 192)
    muxer.abort()


@MACOS_ONLY
def test_muxer_audio_bitrate_option_reaches_the_encoder(vt_output_dir) -> None:
    """The delivery bitrate is honoured; the old ``b:a`` key was silently ignored."""
    import av

    rng = np.random.default_rng(11)
    # A dense stereo signal that needs the bits: independent noise per channel.
    samples = 48000 * 2
    pcm = rng.uniform(-0.9, 0.9, size=(2, samples)).astype(np.float32)
    rates: dict[str, int] = {}
    for label, bitrate in (("requested", 256), ("default", None)):
        path = vt_output_dir / f"bitrate-{label}.mp4"
        muxer = muxer_for(str(path), fps=Fraction(25))
        muxer.add_audio_stream("aac", 48000, 2, bitrate, required=True)
        muxer.write_audio_samples(pcm, 48000)
        muxer.write_packet(synthetic_h264(1).vt_packet(0))
        muxer.close()
        with av.open(str(path)) as container:
            rates[label] = int(container.streams.audio[0].codec_context.bit_rate)
    assert rates["requested"] > 200_000
    assert rates["default"] < 160_000
