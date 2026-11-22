"""Composition audio plan: per-layer PCM segments, gains and envelopes (B05).

The composition owns an immutable :class:`AudioPlan` built from per-layer
audio state and the B03 rational time contract: each audible layer
contributes one segment with exact composition-frame bounds, a half-open
source sample window (adjacent windows share sample endpoints so durations
never drift with frame rounding), a linear gain and the stream identity.
Mute/solo priority, stream probing and waveform envelopes are pure data
here; PCM decoding reuses the shared/edit services and playback takes the
device arbitration outside this module.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from fractions import Fraction
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from .layer_time import fps_fraction


# ── Stream identity and per-layer audio state ───────────────────────────────


@dataclass(frozen=True)
class AudioStreamInfo:
    """One probed audio stream (index in its container)."""

    asset_id: str
    stream_index: int
    sample_rate: int
    channels: int
    time_base: Fraction = Fraction(1)  # seconds per stream tick
    start_pts: int = 0                 # first PTS (encoder delay / non-zero start)
    duration_samples: Optional[int] = None

    @property
    def valid(self) -> bool:
        return self.sample_rate > 0 and self.channels > 0


@dataclass(frozen=True)
class LayerAudio:
    """Per-layer audio definition persisted on the Layer (B05 §2).

    ``gain_db``/``mute``/``solo`` follow the priority resolved by
    :func:`audible_layers`: mute wins over solo, and any solo anywhere
    silences non-solo layers.  Constant forward rate first; variable-rate
    audio rides the B03 time contract when the host enables it.
    """

    asset_id: str
    stream_index: int = 0
    gain_db: float = 0.0
    mute: bool = False
    solo: bool = False
    # Optional per-frame gain animation is resolved by the host into the
    # segment's linear gain; the plan stays frame-constant per segment.

    def to_dict(self) -> Dict[str, Any]:
        return {
            "asset_id": self.asset_id,
            "stream_index": int(self.stream_index),
            "gain_db": float(self.gain_db),
            "mute": bool(self.mute),
            "solo": bool(self.solo),
        }

    @classmethod
    def from_dict(cls, data: Any) -> Optional["LayerAudio"]:
        if not isinstance(data, dict):
            return None
        asset_id = data.get("asset_id")
        if not isinstance(asset_id, str) or not asset_id:
            return None
        try:
            return cls(
                asset_id=asset_id,
                stream_index=int(data.get("stream_index", 0) or 0),
                gain_db=float(data.get("gain_db", 0.0) or 0.0),
                mute=bool(data.get("mute", False)),
                solo=bool(data.get("solo", False)),
            )
        except (TypeError, ValueError):
            return None


def db_to_linear(gain_db: float) -> float:
    """dB → linear amplitude (clamped at -120 dB ≈ silence)."""
    try:
        gain_db = float(gain_db)
    except (TypeError, ValueError, OverflowError):
        return 1.0
    if not math.isfinite(gain_db):
        return 1.0
    if gain_db <= -120.0:
        return 0.0
    try:
        return 10.0 ** (gain_db / 20.0)
    except OverflowError:
        return 1.0


# ── Frame/sample boundary math (shared endpoints) ──────────────────────────


def sample_boundary(frame: int, fps: Any, sample_rate: int) -> int:
    """Sample index where composition ``frame`` starts (floor).

    Adjacent frames share the boundary sample's window: cumulative audio
    duration never drifts with per-frame rounding (B05 §3).
    """
    fps = fps_fraction(fps)
    if fps <= 0 or int(sample_rate) <= 0:
        return 0
    position = Fraction(int(frame) * int(sample_rate)) / fps
    return position.numerator // position.denominator


# ── Mute/solo resolution ────────────────────────────────────────────────────


def audible_layers(
        specs: Sequence[Tuple[str, LayerAudio]]) -> List[Tuple[str, LayerAudio]]:
    """Apply the mute/solo priority (B05 §2).

    Priority: layer mute wins over its own solo; when *any* audible layer
    is soloed, non-solo layers are silent.
    """
    entries = [(str(layer_id), audio) for layer_id, audio in specs
               if isinstance(audio, LayerAudio)]
    unmuted = [entry for entry in entries if not entry[1].mute]
    has_solo = any(audio.solo for _, audio in unmuted)
    if not has_solo:
        return unmuted
    return [entry for entry in unmuted if entry[1].solo]


# ── The immutable plan ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class AudioSegment:
    """One layer's contribution to the composition audio track."""

    layer_id: str
    asset_id: str
    stream_index: int
    in_frame: int                     # inclusive composition frames
    out_frame: int                    # inclusive
    source_sample_start: int          # half-open source sample window
    source_sample_end: int            # exclusive; == start of next frame's window
    sample_rate: int
    channels: int
    gain_linear: float
    start_pts: int = 0
    time_base: Fraction = Fraction(1)

    @property
    def frame_count(self) -> int:
        return self.out_frame - self.in_frame + 1

    @property
    def sample_count(self) -> int:
        return self.source_sample_end - self.source_sample_start


@dataclass(frozen=True)
class AudioPlan:
    """Immutable composition audio plan (render-ready, no Qt)."""

    fps: Fraction
    sample_rate: int
    segments: Tuple[AudioSegment, ...] = ()
    # Composition audio extent in samples (max segment end; hosts derive
    # the export PCM length from this).
    total_samples: int = 0
    # Streams referenced but unusable (offline / probe failed) so the UI
    # can diagnose instead of silently dropping audio.
    missing_streams: Tuple[str, ...] = ()

    def segments_at_frame(self, frame: int) -> Tuple[AudioSegment, ...]:
        return tuple(s for s in self.segments
                     if s.in_frame <= frame <= s.out_frame)


@dataclass(frozen=True)
class LayerAudioTiming:
    """Host-resolved timing for one layer's audio (comp frames + media)."""

    layer_id: str
    audio: LayerAudio
    in_frame: int
    out_frame: int
    source_origin_samples: int = 0   # media sample at the layer's first frame, at the stream rate


def build_audio_plan(timings: Iterable[LayerAudioTiming], *,
                     fps: Any,
                     sample_rate: int,
                     streams: Mapping[Any, AudioStreamInfo],
                     stream_key: Optional[Callable[[Any], Tuple[str, int]]] = None
                     ) -> AudioPlan:
    """Build the immutable plan from layer timings and probed streams.

    ``streams`` maps ``(asset_id, stream_index)`` → :class:`AudioStreamInfo`
    (or the host's own mapping via ``stream_key``).  Frame bounds come from
    the layer rows; the source window is anchored at the layer's media
    origin and expressed in samples at the probed stream rate.
    """
    fps = fps_fraction(fps)
    rate = int(sample_rate)
    stream_map = streams if stream_key is None else {
        stream_key(k): v for k, v in streams.items()}

    segments: List[AudioSegment] = []
    missing: List[str] = []
    total = 0
    timings = tuple(timings)
    audible = set(audible_layers([(t.layer_id, t.audio) for t in timings]))
    for timing in timings:
        if (str(timing.layer_id), timing.audio) not in audible:
            continue
        info = stream_map.get(
            (timing.audio.asset_id, int(timing.audio.stream_index)))
        if info is None or not info.valid:
            key = f"{timing.audio.asset_id}#{timing.audio.stream_index}"
            if key not in missing:
                missing.append(key)
            continue
        end = sample_boundary(timing.out_frame + 1, fps, rate)
        origin = int(timing.source_origin_samples)
        source_start = sample_boundary(timing.in_frame, fps, info.sample_rate)
        source_end = sample_boundary(timing.out_frame + 1, fps, info.sample_rate)
        segments.append(AudioSegment(
            layer_id=timing.layer_id,
            asset_id=timing.audio.asset_id,
            stream_index=int(timing.audio.stream_index),
            in_frame=int(timing.in_frame),
            out_frame=int(timing.out_frame),
            source_sample_start=origin,
            source_sample_end=origin + source_end - source_start,
            sample_rate=info.sample_rate,
            channels=info.channels,
            gain_linear=db_to_linear(timing.audio.gain_db),
            start_pts=info.start_pts,
            time_base=info.time_base,
        ))
        total = max(total, end)
    segments.sort(key=lambda s: (s.in_frame, s.layer_id))
    return AudioPlan(
        fps=fps, sample_rate=rate, segments=tuple(segments),
        total_samples=total, missing_streams=tuple(missing))


# ── Waveform envelopes (worker-thread bucketing) ────────────────────────────


def waveform_envelope(pcm: Any, bucket_samples: int) -> Tuple[Any, Any]:
    """Min/max envelope per temporal bucket for decoded float PCM.

    ``pcm`` is a 1-D (mono) or 2-D ``(samples, channels)`` float
    array.  Returns ``(mins, maxs)`` 1-D float arrays with one entry per
    full bucket; a trailing partial bucket is dropped so zoom levels map
    to stable bucket counts.
    """
    import numpy as np

    data = np.asarray(pcm, dtype=np.float32)
    if data.ndim not in (1, 2):
        raise ValueError("waveform PCM must be mono or (samples, channels)")
    bucket = int(bucket_samples)
    if bucket <= 0 or data.size == 0:
        return np.zeros(0, dtype=np.float32), np.zeros(0, dtype=np.float32)
    full = (data.shape[0] // bucket) * bucket
    if full == 0:
        return np.zeros(0, dtype=np.float32), np.zeros(0, dtype=np.float32)
    trimmed = data[:full].reshape(full // bucket, -1)
    return trimmed.min(axis=1), trimmed.max(axis=1)


def waveform_cache_key(asset_revision: str, stream_index: int,
                       window_start_sample: int, window_samples: int,
                       bucket_samples: int) -> tuple:
    """Cache identity distinguishing asset revision, stream, window, zoom."""
    return (str(asset_revision), int(stream_index), int(window_start_sample),
            int(window_samples), int(bucket_samples))


def plan_epoch(plan: AudioPlan) -> str:
    """Stable signature for epoch invalidation on project/seek/loop changes."""
    import hashlib
    payload = repr((plan.segments, plan.total_samples, plan.missing_streams))
    digest = hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]
    return f"{plan.fps}|{plan.sample_rate}|{digest}"
