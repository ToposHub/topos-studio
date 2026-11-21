"""Composition audio mixdown: exact PCM lengths and pulse-exact mixing (B06).

Online preview and offline export consume the same :class:`AudioPlan`
(B05).  This module provides the sample-exact mixdown contract: the PCM
length of a composition frame range, per-segment placement into the
timeline buffer (gain, mono→stereo upmix, resample-at-decode), and the
silent / missing / no-stream / error classification the export preflight
reports.  Decoding itself reuses the shared PCM services; tests inject
synthetic float arrays.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from typing import Dict, Optional, Tuple

import numpy as np

from .comp_audio import AudioPlan, AudioSegment, sample_boundary
from .layer_time import fps_fraction

# Export audio state codes (B06 §2): distinguish deliberate silence from
# missing material, absent streams and hard errors.
AUDIO_STATE_OK = "ok"
AUDIO_STATE_SILENT = "silent"          # plan built, no audible segments
AUDIO_STATE_MISSING_MEDIA = "missing_media"  # asset offline / unreadable
AUDIO_STATE_NO_STREAM = "no_stream"    # container has no audio stream
AUDIO_STATE_ERROR = "error"


@dataclass(frozen=True)
class AudioExportRange:
    """Selected composition frame range for PCM generation (inclusive)."""

    start_frame: int
    end_frame: int

    def sample_window(self, fps, sample_rate: int) -> Tuple[int, int]:
        """Half-open sample window; adjacent ranges share endpoints."""
        fps = fps_fraction(fps)
        start = sample_boundary(self.start_frame, fps, sample_rate)
        end = sample_boundary(self.end_frame + 1, fps, sample_rate)
        return start, end

    def pcm_length(self, fps, sample_rate: int) -> int:
        start, end = self.sample_window(fps, sample_rate)
        return end - start


def classify_audio_state(plan: Optional[AudioPlan], *,
                         decode_errors: Tuple[str, ...] = (),
                         missing_media: Tuple[str, ...] = (),
                         containers_without_streams: Tuple[str, ...] = ()
                         ) -> str:
    """Map plan + decode outcome to one export state code (B06 §2)."""
    if decode_errors:
        return AUDIO_STATE_ERROR
    if missing_media:
        return AUDIO_STATE_MISSING_MEDIA
    if containers_without_streams or (plan is not None
                                      and plan.missing_streams):
        return AUDIO_STATE_NO_STREAM
    if plan is None or not plan.segments:
        return AUDIO_STATE_SILENT
    return AUDIO_STATE_OK


def _as_stereo(pcm: np.ndarray) -> np.ndarray:
    """Normalize decoded PCM to ``(samples, 2)`` float32."""
    data = np.asarray(pcm, dtype=np.float32)
    if data.ndim == 1:
        return np.stack([data, data], axis=1)
    if data.ndim == 2 and data.shape[1] == 1:
        return np.repeat(data, 2, axis=1)
    return data[:, :2]


def mix_segment(buffer: np.ndarray, segment: AudioSegment, pcm: np.ndarray,
                comp_start_frame: int, fps, sample_rate: int) -> np.ndarray:
    """Add one segment's decoded (plan-rate, stereo-ready) PCM in place.

    ``buffer`` is the composition mixdown buffer, shape ``(samples, 2)``
    for the export range starting at ``comp_start_frame``.  The segment PCM
    must already be resampled to ``sample_rate`` at decode time; here it
    lands at its exact sample offset so a single-sample pulse in the
    source appears at the analytically predicted buffer index.

    Returns the buffer (also mutated in place) so callers can chain.
    """
    fps = fps_fraction(fps)
    if buffer.ndim != 2 or buffer.shape[1] < 2:
        raise ValueError("mix buffer must be (samples, 2)")
    data = _as_stereo(pcm)
    # The segment window's first source sample sits on the boundary sample
    # of its first composition frame.
    seg_base = sample_boundary(
        segment.in_frame - int(comp_start_frame), fps, int(sample_rate))
    offset = seg_base - segment.source_sample_start
    start = max(0, offset + segment.source_sample_start)
    end = min(buffer.shape[0], offset + segment.source_sample_end)
    if end <= start:
        return buffer
    take = data[:end - start]
    if take.shape[0] < end - start:  # decode delivered less than planned
        end = start + take.shape[0]
    buffer[start:end] += take * float(segment.gain_linear)
    return buffer


def mixdown(plan: AudioPlan, decoded: Dict[str, np.ndarray],
            export_range: AudioExportRange, *, fps,
            channels: int = 2) -> Tuple[np.ndarray, str]:
    """Render the composition PCM for one export range.

    ``decoded`` maps ``f"{layer_id}:{asset_id}#{stream_index}"`` to the
    segment's decoded float PCM at the plan sample rate.  Returns
    ``(buffer, state)``; a missing decode silences that segment only and
    the state degrades to ``missing_media`` (B06 §2 classification).
    """
    fps = fps_fraction(fps)
    length = export_range.pcm_length(fps, plan.sample_rate)
    buffer = np.zeros((length, channels), dtype=np.float32)
    missing: list[str] = []
    for segment in plan.segments:
        key = f"{segment.layer_id}:{segment.asset_id}#{segment.stream_index}"
        pcm = decoded.get(key)
        if pcm is None:
            missing.append(key)
            continue
        mix_segment(buffer, segment, pcm, export_range.start_frame, fps,
                    plan.sample_rate)
    if missing:
        return buffer, AUDIO_STATE_MISSING_MEDIA
    return buffer, classify_audio_state(plan)
