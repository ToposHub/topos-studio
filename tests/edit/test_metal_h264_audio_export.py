"""Plan 3.1/3.2: the Metal H.264 fast path carries the master mix.

Exporter-level cases for ``TimelineExporter._try_export_metal_h264_lut``:

* a timeline with audio is delivered through the native path with one AAC
  stream that is declared before the first video packet and written after the
  last frame;
* audio the path cannot carry, a missing or malformed master mix, and every
  cancellation stage fall back or stop without publishing anything;
* 24000/1001 and 30000/1001 timelines reach the bridge as exact fractions;
* a stale audio worker never survives into a later export.

The Metal renderer, the export bridge and the mixer are fakes; the admission
logic (``_metal_h264_lut_clips``), the render plan and the audio worker are
real.  The last case runs both delivery paths on real hardware.
"""

from __future__ import annotations

import os
import sys
import threading
import wave
from dataclasses import replace
from fractions import Fraction
from math import gcd
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

import src.features.edit.core.timeline_metal_export_renderer as renderer_module
import src.shared.gpu.metal.context as metal_context_module
import src.shared.media.gpu_frame as gpu_frame_module
import src.shared.media.macos_vt.gpu_export_bridge as bridge_module
from src.features.color.core.color_graph_service import ColorGraphData, ColorGraphService
from src.features.edit.core.interchange import frames_to_samples
from src.features.edit.core.project import EditProject, MediaAsset
from src.features.edit.core.timeline_export import (
    AudioStemExportTrack,
    ExportCodec,
    ExportRangeType,
    ExportSource,
    TimelineExportConfig,
    TimelineExporter,
)
from src.features.edit.core.timeline_render_plan import TimelineRenderPlan
from src.features.edit.core.timeline_v2 import ClipInstance, EditTrack, Rational, TimelineSequence, TransitionInstance
from src.shared.export.export_stage_telemetry import ExportTelemetryRecorder
from src.shared.export.video_encoder import MACOS_HW_ENCODE_DISABLE_ENV
from src.shared.media.macos_vt.vt_compression import VideoToolboxCompressionError

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="Metal H.264 delivery is macOS only")

FRAMES = 40
SAMPLE_RATE = 48000
SIZE = (1920, 1080)
TEST_ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# timeline
# ---------------------------------------------------------------------------


def _timeline(directory: Path, *, fps: Rational = Rational(25, 1), audio: bool = True):
    """One 1080p H.264 clip with a primary grade, optionally with an audio track.

    A grade is what makes the timeline eligible for the native path at all.
    The audio clip starts ten frames in and comes from another source
    position, so the mix is not simply the programme from sample zero.
    """
    service = ColorGraphService()
    inserted = service.op_insert_corrector(ColorGraphData.create_serial())
    node = next(entry for entry in inserted.graph.nodes if entry.node_type == "corrector")
    graded = service.op_set_node_option(inserted.graph, node.node_id, "grade_patch", ("exposure", 0.35))
    assert graded.ok
    source = MediaAsset(
        asset_id="source", display_name="Source", media_type="video",
        path=str(directory / "media.mov"),
        metadata={"width": SIZE[0], "height": SIZE[1], "codec": "h264", "pixel_format": "yuv420p"})
    project = EditProject.create("Metal H.264 audio test").with_media_asset_added(source)
    project, stack_id = project.create_color_stack(asset_id=source.asset_id)
    project = project.update_color_stack(stack_id, color_graph=graded.graph)
    video_clip = ClipInstance(
        clip_id="v1", asset_id=source.asset_id, track_id="v1", timeline_start=0,
        timeline_duration=FRAMES, source_start=12, source_duration=FRAMES,
        color_stack_id=stack_id)
    video_track = EditTrack(
        track_id="v1", name="V1", track_type="video", index=0, clips=(video_clip,))
    audio_tracks: tuple[EditTrack, ...] = ()
    if audio:
        tone = MediaAsset(
            asset_id="tone", display_name="tone", media_type="audio",
            path=str(directory / "tone.wav"), metadata={})
        project = project.with_media_asset_added(tone)
        mix_clip = ClipInstance(
            clip_id="a1", asset_id="tone", track_id="a1", timeline_start=10,
            timeline_duration=30, source_start=100, source_duration=30)
        audio_tracks = (EditTrack(
            track_id="a1", name="A1", track_type="audio", index=0, clips=(mix_clip,)),)
    sequence = TimelineSequence(
        timeline_id="grade", name="Audio", fps=fps, width=SIZE[0], height=SIZE[1],
        video_tracks=(video_track,), audio_tracks=audio_tracks)
    exporter = TimelineExporter(project, sequence, object())
    exporter._render_plan = TimelineRenderPlan(sequence)
    config = TimelineExportConfig(
        codec=ExportCodec.H264, output_path=str(directory / "out.mp4"), include_audio=True)
    return exporter, config


def _expected_samples(exporter: TimelineExporter, frames: int = FRAMES) -> int:
    """Master-mix length the regular exporter produces for this export window."""
    return frames_to_samples(frames, exporter._get_sequence_fps_rational(), SAMPLE_RATE)


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------


class _FakeBridge:
    """Records what the exporter does with the native export bridge.

    ``close`` obeys the bridge's publication contract: a required audio track
    with no sample is an error, and a cancellation seen at the end means the
    movie is not published.
    """

    instances: list["_FakeBridge"] = []

    def __init__(self, context, output_path, *, width, height, fps, **options) -> None:
        self.output_path = output_path
        self.width, self.height, self.fps = width, height, fps
        self.options = options
        self.should_cancel = options["should_cancel"]
        self.acquired: list[dict] = []
        self.audio_stream: dict | None = None
        self.audio_writes: list[tuple[np.ndarray, int]] = []
        self.close_hook = None
        self.closed = False
        self.published = False
        self.aborted = False
        _FakeBridge.instances.append(self)

    def add_audio_stream(self, codec="aac", sample_rate=48000, channels=2, bitrate=None,
                         *, required=False) -> None:
        if self.acquired:
            raise VideoToolboxCompressionError("audio_stream_must_be_added_before_video_mux")
        self.audio_stream = dict(codec=codec, sample_rate=sample_rate, channels=channels,
                                 bitrate=bitrate, required=required)

    def acquire(self, **kwargs) -> SimpleNamespace:
        self.acquired.append(kwargs)
        return SimpleNamespace(frame_index=kwargs["frame_index"])

    def write_audio_samples(self, samples, sample_rate=48000) -> int:
        if self.audio_stream is None:
            raise VideoToolboxCompressionError("audio_stream_not_configured")
        self.audio_writes.append((np.array(samples, copy=True), sample_rate))
        return int(np.asarray(samples).shape[-1])

    def close(self) -> bool:
        self.closed = True
        if self.close_hook is not None:
            self.close_hook()
        fed = int(sum(int(samples.shape[-1]) for samples, _ in self.audio_writes))
        if self.audio_stream and self.audio_stream["required"] and fed <= 0:
            raise VideoToolboxCompressionError("videotoolbox_audio_track_missing")
        if self.should_cancel():
            return False
        self.published = True
        Path(self.output_path).write_bytes(b"published")
        return True

    def abort(self) -> None:
        self.aborted = True


class _FakeRenderer:
    instances: list["_FakeRenderer"] = []

    def __init__(self, *args, **kwargs) -> None:
        self.released = False
        _FakeRenderer.instances.append(self)

    def prepare_nv12_lut(self, operations):
        return operations

    def render_lut_to_nv12(self, *args) -> float:
        return 0.0

    def release(self) -> None:
        self.released = True


class _FakeSource:
    def __init__(self, frame_index: int) -> None:
        self.frame_index = frame_index


class _Mixer:
    """Stands in for ``_render_audio_master`` on the audio worker thread."""

    def __init__(self, exporter: TimelineExporter) -> None:
        self.exporter = exporter
        self.threads: list[threading.Thread] = []
        self.calls: list[tuple[int, int]] = []
        self.result = "mix"    # mix | silence | none | error | short | wrong_channels | float64
        self.channels = 2
        self.tail_samples = 0
        self.error: Exception = RuntimeError("mixer exploded")
        self.after_mix = None
        # When set, the mix only completes once the gate opens.
        self.gate: threading.Event | None = None

    def __call__(self, start_frame, end_frame, config, encoder=None,
                 only_track_ids=None, channels_override=None):
        self.threads.append(threading.current_thread())
        self.calls.append((start_frame, end_frame))
        assert encoder is not None, "the mixer needs the exporter's liveness guard"
        if self.result == "error":
            raise self.error
        if self.result == "none":
            return None
        if self.gate is not None:
            assert self.gate.wait(timeout=5.0), "the mix was never released"
        samples = _expected_samples(self.exporter, end_frame - start_frame) + self.tail_samples
        if self.result == "short":
            samples -= 1
        channels = 1 if self.result == "wrong_channels" else self.channels
        fill = 0.0 if self.result == "silence" else 0.25
        dtype = np.float64 if self.result == "float64" else np.float32
        if self.after_mix is not None:
            self.after_mix()
        return np.full((channels, samples), fill, dtype=dtype)


@pytest.fixture
def wire(monkeypatch: pytest.MonkeyPatch):
    """Return a builder that connects one timeline to the fakes."""
    monkeypatch.setenv("TOPOS_EXPORT_NATIVE_VT", "")
    monkeypatch.delenv(MACOS_HW_ENCODE_DISABLE_ENV, raising=False)

    class FakeContext:
        def create_buffer_from_numpy(self, values):
            return object()

        def write_buffer(self, buffer, values) -> None:
            return None

    def build(directory: Path, **timeline_options) -> SimpleNamespace:
        directory.mkdir(parents=True, exist_ok=True)
        _FakeBridge.instances.clear()
        _FakeRenderer.instances.clear()
        monkeypatch.setattr(metal_context_module, "MetalComputeContext", FakeContext)
        monkeypatch.setattr(renderer_module, "TimelineMetalExportRenderer", _FakeRenderer)
        monkeypatch.setattr(bridge_module, "VideoToolboxGpuExportBridge", _FakeBridge)
        monkeypatch.setattr(gpu_frame_module, "GpuDecodedFrame", _FakeSource)
        released: list[int] = []
        monkeypatch.setattr(gpu_frame_module, "safe_release_gpu_frame",
                            lambda source: released.append(source.frame_index))
        exporter, config = _timeline(directory, **timeline_options)
        knobs = SimpleNamespace(
            exporter=exporter, config=config, released=released, encoded=[], manifest=[])
        knobs.backend = exporter._timeline_graphics_backend
        exporter._gpu_render_disabled = False
        exporter._export_telemetry = ExportTelemetryRecorder("metal-h264-audio-test")
        exporter._request_video_metal_frame = (
            lambda path, source_frame, asset_id, session_key="": _FakeSource(source_frame))
        exporter._encode_frame = lambda surface: knobs.encoded.append(surface)
        exporter._cleanup_clips = lambda: None
        exporter._write_success_manifest_async = lambda config: knobs.manifest.append(config)
        knobs.mixer = _Mixer(exporter)
        exporter._render_audio_master = knobs.mixer
        knobs.encoder_config = exporter._create_encoder_config(config)
        return knobs

    return build


def _run(knobs, config=None, encoder_config=None, start=0, end=FRAMES):
    return knobs.exporter._try_export_metal_h264_lut(
        config or knobs.config, encoder_config or knobs.encoder_config, start, end)


def _bridge() -> _FakeBridge:
    assert len(_FakeBridge.instances) == 1, "exactly one native bridge per attempt"
    return _FakeBridge.instances[0]


def _assert_quiescent(knobs, *, renderer_released: bool = True) -> None:
    """No worker, no shared audio state, no provider, the backend restored."""
    exporter = knobs.exporter
    assert exporter._audio_render_thread is None
    assert exporter._audio_master_pcm is None
    assert exporter._audio_render_error is None
    assert exporter._gpu_encode_surface_provider is None
    assert exporter._timeline_graphics_backend == knobs.backend
    if renderer_released:
        assert all(renderer.released for renderer in _FakeRenderer.instances)
    assert not any(thread.is_alive() for thread in knobs.mixer.threads)


def _fallback_root_cause(knobs) -> str:
    return str(knobs.exporter._export_telemetry.snapshot().fallback_root_cause)


# ---------------------------------------------------------------------------
# admission and delivery
# ---------------------------------------------------------------------------


def test_audio_timeline_is_delivered_through_the_native_path(wire, tmp_path) -> None:
    """Plan 3.1: the stream is declared first and the mix written after the last frame."""
    knobs = wire(tmp_path)
    outcome = _run(knobs)
    assert outcome.success, outcome.cause
    assert outcome.delivery_path == "metal_h264_nv12_serial_grade"
    bridge = _bridge()
    assert bridge.audio_stream == dict(
        codec="aac", sample_rate=SAMPLE_RATE, channels=2, bitrate=192, required=True)
    assert (bridge.width, bridge.height, bridge.fps) == (SIZE[0], SIZE[1], Fraction(25))
    assert bridge.options["expected_frames"] == FRAMES and bridge.options["bt709_limited"] is True
    assert len(bridge.acquired) == FRAMES and bridge.published and not bridge.aborted
    # The mix ran beside the GPU loop on its own thread, for the whole window.
    assert knobs.mixer.calls == [(0, FRAMES)]
    assert knobs.mixer.threads[0] is not threading.main_thread()
    assert knobs.mixer.threads[0].name == "topos-export-audio"
    (samples, sample_rate), = bridge.audio_writes
    assert samples.shape == (2, _expected_samples(knobs.exporter)) and samples.dtype == np.float32
    assert sample_rate == SAMPLE_RATE
    assert knobs.manifest == [knobs.config]
    assert knobs.released == [12 + frame for frame in range(FRAMES)]
    _assert_quiescent(knobs)


@pytest.mark.parametrize("channels, bitrate, layout", [(1, 192, "mono"), (2, 256, "stereo")])
def test_mono_and_the_delivery_bitrate_are_passed_through(wire, tmp_path, channels, bitrate, layout) -> None:
    knobs = wire(tmp_path)
    knobs.mixer.channels = channels
    config = replace(knobs.config, audio_channels=channels, audio_bitrate=bitrate,
                     audio_channel_layout=layout)
    assert _run(knobs, config).success
    assert _bridge().audio_stream == dict(
        codec="aac", sample_rate=SAMPLE_RATE, channels=channels, bitrate=bitrate, required=True)
    assert _bridge().audio_writes[0][0].shape[0] == channels


def test_a_layout_name_decides_the_channel_count(wire, tmp_path) -> None:
    """P1-09: the explicit layout is the single source of truth for channels."""
    knobs = wire(tmp_path)
    knobs.mixer.channels = 1
    config = replace(knobs.config, audio_channels=2, audio_channel_layout="mono")
    assert _run(knobs, config).success
    assert _bridge().audio_stream["channels"] == 1


def test_silent_master_mix_is_still_a_delivered_track(wire, tmp_path) -> None:
    """Plan 3.1: a silent section is audio, not a missing track."""
    knobs = wire(tmp_path)
    knobs.mixer.result = "silence"
    assert _run(knobs).success
    bridge = _bridge()
    assert bridge.published and len(bridge.audio_writes) == 1
    assert bridge.audio_writes[0][0].shape == (2, _expected_samples(knobs.exporter))
    assert not np.any(bridge.audio_writes[0][0])


def test_fx_tail_mix_longer_than_the_video_is_accepted(wire, tmp_path) -> None:
    """Plan 3.1: the tail is delivered; nothing trims the track to the video."""
    knobs = wire(tmp_path)
    knobs.mixer.tail_samples = 4800
    assert _run(knobs, replace(knobs.config, include_fx_tail=True)).success
    assert _bridge().audio_writes[0][0].shape[1] == _expected_samples(knobs.exporter) + 4800


def test_video_without_audio_clips_keeps_the_video_only_path(wire, tmp_path) -> None:
    """No audio clip in the window: no stream, no worker, and the audio
    settings (even unsupported ones) are irrelevant."""
    knobs = wire(tmp_path, audio=False)
    config = replace(knobs.config, audio_codec="pcm_s24le", audio_sample_rate=44100)
    outcome = _run(knobs, config)
    assert outcome.success and outcome.delivery_path == "metal_h264_nv12_serial_grade"
    assert _bridge().audio_stream is None and _bridge().published
    assert knobs.mixer.calls == [] and len(_bridge().acquired) == FRAMES
    _assert_quiescent(knobs)


@pytest.mark.parametrize("patch", [
    {"audio_codec": "pcm_s24le"},
    {"audio_sample_rate": 44100},
    {"audio_channel_layout": "5.1(side)"},
    {"audio_channel_layout": "7.1"},
    {"audio_channel_layout": "not-a-layout"},
    {"audio_stems": (AudioStemExportTrack(stem_group="dialogue"),)},
])
def test_unsupported_audio_stays_on_the_regular_path(wire, tmp_path, patch) -> None:
    """Plan 3.1 fallback: stems, other rates, other codecs and layouts are declined."""
    knobs = wire(tmp_path)
    assert _run(knobs, replace(knobs.config, **patch)) is None
    assert _FakeBridge.instances == [] and knobs.mixer.calls == []
    # Declined at admission, like every other ineligible timeline: this is the
    # regular path, not a GPU fallback, so nothing is recorded as one.
    assert _fallback_root_cause(knobs) == "None"
    assert not os.path.exists(knobs.config.output_path)
    _assert_quiescent(knobs, renderer_released=False)


# ---------------------------------------------------------------------------
# fallback when the mix is unusable
# ---------------------------------------------------------------------------


class _MixerError(RuntimeError):
    pass


@pytest.mark.parametrize("result, cause", [
    ("none", "RuntimeError"),             # nothing renderable: never publish a movie without it
    ("error", "_MixerError"),             # the mixer raised
    ("short", "RuntimeError"),            # one sample short of the export window
    ("wrong_channels", "RuntimeError"),   # not the declared channel count
    ("float64", "RuntimeError"),          # not the float32 plane contract of the writer
])
def test_unusable_master_mix_falls_back_without_publishing(wire, tmp_path, result, cause) -> None:
    knobs = wire(tmp_path)
    destination = Path(knobs.config.output_path)
    destination.write_bytes(b"previous delivery")
    knobs.mixer.result = result
    knobs.mixer.error = _MixerError("decode failed")

    assert _run(knobs) is None
    bridge = _bridge()
    assert bridge.aborted and not bridge.closed and not bridge.published
    assert destination.read_bytes() == b"previous delivery"
    assert knobs.manifest == []
    assert _fallback_root_cause(knobs) == f"metal_h264_lut_retry:{cause}"
    assert knobs.exporter._current_frame == 0
    _assert_quiescent(knobs)


def test_a_bridge_that_lost_the_audio_refuses_to_publish(wire, tmp_path, monkeypatch) -> None:
    """Plan 3.1: the bridge's own contract is the last line of defence."""
    knobs = wire(tmp_path)
    destination = Path(knobs.config.output_path)
    destination.write_bytes(b"previous delivery")
    original = _FakeBridge.write_audio_samples

    def lose_the_mix(self, samples, sample_rate=48000):
        return original(self, samples[:, :0], sample_rate)   # not one sample reaches it

    monkeypatch.setattr(_FakeBridge, "write_audio_samples", lose_the_mix)
    assert _run(knobs) is None
    bridge = _bridge()
    assert bridge.aborted and not bridge.published
    assert destination.read_bytes() == b"previous delivery"
    assert _fallback_root_cause(knobs) == "metal_h264_lut_retry:VideoToolboxCompressionError"
    _assert_quiescent(knobs)


def test_stale_audio_worker_never_survives_a_fallback(wire, tmp_path) -> None:
    """AUD-18401: a worker from a failed attempt must not feed the next export."""
    knobs = wire(tmp_path)
    knobs.mixer.result = "none"
    assert _run(knobs) is None
    worker = knobs.mixer.threads[0]
    assert not worker.is_alive()
    _FakeBridge.instances.clear()
    # A later native attempt starts clean and gets its own mix.
    knobs.mixer.result = "mix"
    assert _run(knobs).success
    assert knobs.mixer.threads[1] is not worker
    assert _bridge().audio_writes[0][0].shape == (2, _expected_samples(knobs.exporter))
    _assert_quiescent(knobs)


# ---------------------------------------------------------------------------
# cancellation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("stage", [
    "before_first_frame", "first_frame", "mid_render", "while_draining", "during_mux",
])
def test_cancellation_at_every_stage_publishes_nothing(wire, tmp_path, monkeypatch, stage) -> None:
    """Plan 3.2: first frame, render, drain and mux all stop without a delivery."""
    knobs = wire(tmp_path)
    exporter = knobs.exporter
    destination = Path(knobs.config.output_path)
    destination.write_bytes(b"previous delivery")

    def cancel() -> None:
        exporter._cancel_requested = True

    if stage == "before_first_frame":
        cancel()
    elif stage in ("first_frame", "mid_render"):
        stop_after = 1 if stage == "first_frame" else 7
        acquire = _FakeBridge.acquire

        def acquire_then_cancel(self, **kwargs):
            surface = acquire(self, **kwargs)
            if len(self.acquired) == stop_after:
                cancel()
            return surface

        monkeypatch.setattr(_FakeBridge, "acquire", acquire_then_cancel)
    elif stage == "while_draining":
        # The frames are all in and the export thread waits for the mix. The
        # request lands exactly inside that wait, which must stay cancellable.
        gate = threading.Event()
        knobs.mixer.gate = gate
        knobs.mixer.after_mix = cancel
        join = exporter._join_audio_render_thread

        def join_after_the_request_lands() -> None:
            gate.set()
            join()

        exporter._join_audio_render_thread = join_after_the_request_lands
    else:
        initialize = _FakeBridge.__init__

        def initialize_with_hook(self, *args, **kwargs):
            initialize(self, *args, **kwargs)
            self.close_hook = cancel             # the request lands at the trailer

        monkeypatch.setattr(_FakeBridge, "__init__", initialize_with_hook)

    outcome = _run(knobs)
    assert outcome.cancelled and not outcome.success
    if stage == "before_first_frame":
        assert _FakeBridge.instances == []
    else:
        bridge = _bridge()
        assert bridge.aborted and not bridge.published
        if stage == "while_draining":
            assert bridge.audio_writes == []     # a cancelled mix is never handed over
            assert len(bridge.acquired) == FRAMES
            # Nothing was written, so the bridge is not asked to finalize.
            assert bridge.closed is False and bridge.aborted
    assert destination.read_bytes() == b"previous delivery"
    assert knobs.manifest == []
    _assert_quiescent(knobs)


# ---------------------------------------------------------------------------
# fractional frame rates
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fps", [
    Rational(30000, 1001), Rational(24000, 1001), Rational(60000, 1001), Rational(50, 1),
])
def test_fractional_frame_rates_reach_the_bridge_exactly(wire, tmp_path, fps) -> None:
    """Plan 3.2: 24000/1001 and 30000/1001 are no longer rounded away."""
    knobs = wire(tmp_path, fps=fps)
    divisor = gcd(int(fps.num), int(fps.den))
    assert knobs.encoder_config.fps == fps

    outcome = _run(knobs)
    assert outcome.success, outcome.cause
    bridge = _bridge()
    assert bridge.fps == Fraction(int(fps.num) // divisor, int(fps.den) // divisor)
    assert len(bridge.acquired) == FRAMES
    assert bridge.audio_writes[0][0].shape == (
        2, int(Fraction(FRAMES * SAMPLE_RATE) / Fraction(int(fps.num), int(fps.den))))
    assert knobs.released == [12 + frame for frame in range(FRAMES)]
    _assert_quiescent(knobs)


def test_encoder_rate_must_still_match_the_sequence(wire, tmp_path) -> None:
    """An export frame-rate override keeps the regular path, as before."""
    knobs = wire(tmp_path)
    encoder_config = replace(knobs.encoder_config, fps=Rational(30000, 1001))
    assert _run(knobs, encoder_config=encoder_config) is None
    assert _FakeBridge.instances == []


# ---------------------------------------------------------------------------
# real hardware
# ---------------------------------------------------------------------------


def _write_tone(path: Path, seconds: float = 4.0) -> None:
    """Stereo test tone: silent until 2.0 s, then one tone per channel."""
    time_axis = np.arange(int(SAMPLE_RATE * seconds)) / SAMPLE_RATE
    left = np.where(time_axis >= 2.0, 0.4 * np.sin(2 * np.pi * 1000.0 * time_axis), 0.0)
    right = np.where(time_axis >= 2.0, 0.2 * np.sin(2 * np.pi * 500.0 * time_axis), 0.0)
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(2)
        stream.setsampwidth(2)
        stream.setframerate(SAMPLE_RATE)
        stream.writeframes(
            (np.clip(np.stack([left, right], axis=1), -1, 1) * 32767).astype("<i2").tobytes())


def _audio_payloads(path: Path) -> list[tuple[int, int, int, bytes]]:
    import av

    with av.open(str(path)) as container:
        stream = container.streams.audio[0]
        return [(packet.pts, packet.dts, packet.duration, bytes(packet))
                for packet in container.demux(stream) if packet.size > 0]


def _decode_audio(path: Path) -> np.ndarray:
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
        except av.error.EOFError:
            pass
    return np.concatenate(chunks, axis=1)


@pytest.mark.parametrize("with_dip", [False, True], ids=["primary_grade", "dip_to_color"])
def test_native_audio_delivery_matches_the_regular_export(tmp_path, monkeypatch, with_dip: bool) -> None:
    """Real VideoToolbox: both delivery paths produce the same audio track."""
    from src.shared.gpu.metal.context import MetalComputeContext

    source = TEST_ROOT / "tests" / "test_videos" / "2K_h264.mp4"
    if not source.is_file() or not MetalComputeContext.is_supported():
        pytest.skip("real H.264 test material or Metal is unavailable")
    from src.features.edit.core.project import probe_media_metadata
    from src.features.edit.core.timeline_export import create_timeline_exporter

    frames = 24
    tone = tmp_path / "tone.wav"
    _write_tone(tone)
    metadata = dict(probe_media_metadata(str(source)))
    assert (metadata.get("width"), metadata.get("height")) == SIZE
    service = ColorGraphService()
    inserted = service.op_insert_corrector(ColorGraphData.create_serial())
    node = next(entry for entry in inserted.graph.nodes if entry.node_type == "corrector")
    graded = service.op_set_node_option(inserted.graph, node.node_id, "grade_patch", ("exposure", 0.35))
    assert graded.ok
    video_asset = MediaAsset(
        asset_id="source", display_name="Source", media_type="video", path=str(source),
        metadata=metadata)
    audio_asset = MediaAsset(
        asset_id="tone", display_name="tone", media_type="audio", path=str(tone),
        metadata=probe_media_metadata(str(tone)) or {})
    project = (EditProject.create("native audio").with_media_asset_added(video_asset)
               .with_media_asset_added(audio_asset))
    project, stack_id = project.create_color_stack(asset_id="source")
    project = project.update_color_stack(stack_id, color_graph=graded.graph)
    video_clip = ClipInstance(
        clip_id="v1", asset_id="source", track_id="v1", timeline_start=0,
        timeline_duration=frames, source_start=100, source_duration=frames,
        color_stack_id=stack_id)
    video_track = EditTrack(track_id="v1", name="V1", track_type="video", index=0, clips=(video_clip,))
    dip_color = (0.7, 0.15, 0.35)
    if with_dip:
        middle = frames // 2
        left = replace(video_clip, timeline_duration=middle, source_duration=middle, color_stack_id=None)
        right = replace(left, clip_id="v1-right", timeline_start=middle, source_start=180)
        transition = TransitionInstance(
            track_id="v1", left_clip_id=left.clip_id, right_clip_id=right.clip_id,
            transition_type="dip_to_color", center_frame=middle, duration=8,
            params={"color": dip_color},
        )
        video_track = replace(video_track, clips=(left, right), transitions=(transition,))
    # An offset cut: source frame 50 (2.0 s, where the tone starts) plays at
    # timeline frame 6, so the programme opens with 0.24 s of silence.
    mix_clip = ClipInstance(
        clip_id="a1", asset_id="tone", track_id="a1", timeline_start=6,
        timeline_duration=18, source_start=50, source_duration=18)
    sequence = TimelineSequence(
        timeline_id="native", name="Native audio", fps=Rational(25, 1),
        width=SIZE[0], height=SIZE[1],
        video_tracks=(video_track,),
        audio_tracks=(EditTrack(track_id="a1", name="A1", track_type="audio", index=0,
                                clips=(mix_clip,)),))
    monkeypatch.setenv("TOPOS_EXPORT_GPU_RENDER", "1")
    monkeypatch.delenv(MACOS_HW_ENCODE_DISABLE_ENV, raising=False)
    outputs: dict[str, Path] = {}
    selections: dict[str, dict] = {}
    delivery_paths: dict[str, str] = {}
    for label, native in (("fast", ""), ("regular", "0")):
        monkeypatch.setenv("TOPOS_EXPORT_NATIVE_VT", native)
        path = tmp_path / f"{label}.mp4"
        exporter = create_timeline_exporter(project=project, sequence=sequence)
        exporter._write_success_manifest_async = lambda config: None
        config = TimelineExportConfig(
            source=ExportSource.TIMELINE, timeline_id=sequence.timeline_id,
            range_type=ExportRangeType.ENTIRE,
            codec=ExportCodec.H264, codec_profile="high", quality=20_000_000,
            quality_mode="vbr", include_audio=True, output_path=str(path))
        outcome = exporter.export_timeline(config)
        assert outcome.success, f"{label}: {outcome.cause}"
        outputs[label] = path
        selections[label] = dict(exporter.bridge_selection)
        delivery_paths[label] = outcome.delivery_path
    if not selections["fast"]["selected"]:
        pytest.skip(f"the native path declined this machine: {selections['fast']}")
    assert selections["regular"]["selected"] is False
    if with_dip:
        assert delivery_paths["fast"] == "metal_h264_nv12_dip_to_color"

    import av

    fast, regular = _audio_payloads(outputs["fast"]), _audio_payloads(outputs["regular"])
    assert fast and fast == regular, "the native path must deliver the regular exporter's AAC track"
    # AAC priming is trimmed by an edit list: the first packet starts a frame early.
    assert fast[0][0] == -1024
    samples = frames_to_samples(frames, Rational(25, 1), SAMPLE_RATE)
    dip_samples: dict[str, dict[int, np.ndarray]] = {}
    for label, path in outputs.items():
        dip_samples[label] = {}
        with av.open(str(path)) as container:
            assert [stream.type for stream in container.streams] == ["video", "audio"]
            video_count = 0
            for frame in container.decode(video=0):
                if with_dip and middle - 4 <= video_count < middle + 4:
                    dip_samples[label][video_count] = frame.to_ndarray(format="rgb24")[::12, ::12].astype(np.int16)
                video_count += 1
            assert video_count == frames
            audio = container.streams.audio[0]
            assert audio.start_time == 0
            assert abs(audio.duration * audio.time_base - Fraction(samples, SAMPLE_RATE)) < Fraction(
                1024, SAMPLE_RATE)
    if with_dip:
        np.testing.assert_allclose(
            dip_samples["fast"][middle].mean(axis=(0, 1)), np.array(dip_color) * 255, atol=4,
        )
        for index, pixels in dip_samples["fast"].items():
            assert float(np.abs(pixels - dip_samples["regular"][index]).mean()) <= 3.0, index
    # The tone starts 0.24 s in, in both channels, in the right order.
    decoded = _decode_audio(outputs["fast"])[:, :samples]
    onset = frames_to_samples(6, Rational(25, 1), SAMPLE_RATE)
    assert onset == 11520
    assert float(np.abs(decoded[:, :onset - 1024]).max()) < 1e-6
    window = slice(onset + 2048, samples)
    time_axis = np.arange(samples) / SAMPLE_RATE
    assert float(np.abs(decoded[0, window] - 0.4 * np.sin(2 * np.pi * 1000.0 * time_axis[window])).max()) < 0.04
    assert float(np.abs(decoded[1, window] - 0.2 * np.sin(2 * np.pi * 500.0 * time_axis[window])).max()) < 0.04
