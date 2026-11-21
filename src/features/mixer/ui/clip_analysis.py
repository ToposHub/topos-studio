"""Audio measurements and temporary audition buffers, independent of Qt widgets."""
from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from fractions import Fraction
from typing import TYPE_CHECKING

import numpy as np

from src.features.edit.core.audio_edit import (
    append_step,
    find_measurement_step_index,
    invert_position_through_step,
    length_step_sample_maps,
    make_step,
    map_selection_to_insertion,
    render_audio_edit_full,
    render_to_step_insertion,
    selection_measurement_envelope,
    tag_legacy_measurement_operations,
    upsert_step,
    time_stretch_output_length,
)

if TYPE_CHECKING:
    from src.features.edit.core.project import EditProject
    from src.features.edit.core.timeline_v2 import ClipInstance


@dataclass(frozen=True)
class GainMeasurementRequest:
    pcm: np.ndarray
    rate: int
    audio_edit: dict | None
    channels: tuple[int, ...]
    start: int
    end: int
    target: float
    true_peak_ceiling: float | None
    processor: str
    operation: str
    label: str
    legacy_labels: tuple[str, ...]


@dataclass(frozen=True)
class GainMeasurementResult:
    status: str
    edit: dict | None = None
    peak_or_lufs: float | None = None
    true_peak: float | None = None


def make_timepitch_draft(
    edit: dict | None, request: tuple[int, int, float, float, bool],
    total: int, channels: tuple[int, ...],
) -> tuple[dict, dict]:
    lo, hi, ratio, semitones, preserve = request
    step = make_step(
        "time_stretch", lo, hi, channels, ratio_pct=ratio,
        pitch_semitones=semitones, preserve_pitch=preserve,
        output_length=total - (hi - lo) + time_stretch_output_length(hi - lo, ratio))
    return append_step(edit, step), step


@dataclass(frozen=True)
class ClipAuditionCoordinates:
    """Maps the audition PCM back to a common pre-stack sample clock."""

    maps: tuple[tuple[int, int, int], ...] = ()

    @classmethod
    def from_edit(cls, edit: dict | None) -> ClipAuditionCoordinates:
        return cls(length_step_sample_maps(edit))

    def map_to(self, other: ClipAuditionCoordinates, point: int) -> int:
        source = Fraction(point)
        for start, end, output in reversed(self.maps):
            if not output and source == start:
                source = Fraction(end)
            else:
                source = invert_position_through_step(source, start, end, output)
        for start, end, output in other.maps:
            if source >= end:
                source += output - (end - start)
            elif source > start:
                source = start + (source - start) * output / (end - start)
        return round(source)


def measure_gain_edit(
    request: GainMeasurementRequest, cancelled: Callable[[], bool],
) -> GainMeasurementResult | None:
    """Render to the replacement point, build a step, then meter its actual output."""
    edit = tag_legacy_measurement_operations(request.audio_edit, legacy_labels=request.legacy_labels)
    index = find_measurement_step_index(
        edit, processor_id=request.processor, operation=request.operation,
        start=request.start, end=request.end, channels=request.channels,
        legacy_labels=request.legacy_labels)
    start, end = map_selection_to_insertion(edit, request.start, request.end, index)
    baseline = render_to_step_insertion(
        request.pcm, edit, insertion_index=index, sample_rate=request.rate, cancel_check=cancelled)
    if cancelled():
        return None
    segment = _selection(baseline, request.channels, start, end)
    if not segment.size:
        return GainMeasurementResult("no_signal")
    ceiling = request.true_peak_ceiling
    if ceiling is None:
        envelope = selection_measurement_envelope(baseline.shape[1], start, end, sample_rate=request.rate)
        peak = float(np.max(np.abs(segment * envelope[start:start + segment.shape[1]])))
        if peak <= 1e-6:
            return GainMeasurementResult("no_signal")
        gain = request.target - 20.0 * math.log10(peak)
    else:
        from src.features.edit.core.loudness_analyzer import analyze_loudness

        if segment.shape[1] * 5 < 2 * request.rate:
            return GainMeasurementResult("too_short")
        measured = analyze_loudness(np.ascontiguousarray(segment), float(request.rate))
        if not measured.backend_available:
            return GainMeasurementResult("unavailable")
        if not math.isfinite(measured.integrated_lufs) or measured.integrated_lufs <= -70.0:
            return GainMeasurementResult("no_signal")
        gain = min(request.target - measured.integrated_lufs, ceiling - measured.true_peak_dbtp)
    if abs(gain) < 0.05 and index >= len((edit or {}).get("steps", [])):
        return GainMeasurementResult("unchanged")

    for _attempt in range(3 if ceiling is not None else 1):
        if cancelled():
            return None
        step = make_step(
            request.processor, request.start, request.end, request.channels,
            gain_db=round(gain, 2), operation=request.operation, label=request.label)
        candidate = upsert_step(edit, step)
        full = render_audio_edit_full(request.pcm, candidate, sample_rate=request.rate, cancel_check=cancelled)
        if cancelled():
            return None
        output = _selection(full, request.channels, request.start, request.end)
        if not output.size:
            return GainMeasurementResult("no_signal")
        if ceiling is None:
            peak = float(np.max(np.abs(output)))
            return GainMeasurementResult("ready", candidate, 20.0 * math.log10(max(peak, 1e-12)))
        measured = analyze_loudness(np.ascontiguousarray(output), float(request.rate))
        if not measured.backend_available or not math.isfinite(measured.true_peak_dbtp):
            return GainMeasurementResult("unavailable")
        if measured.true_peak_dbtp <= ceiling + 0.05:
            return GainMeasurementResult("ready", candidate, measured.integrated_lufs, measured.true_peak_dbtp)
        gain -= measured.true_peak_dbtp - ceiling + 0.05
    return GainMeasurementResult("ceiling_not_met", true_peak=measured.true_peak_dbtp)


def _selection(pcm: np.ndarray, channels: tuple[int, ...], start: int, end: int) -> np.ndarray:
    usable = [channel for channel in channels if 0 <= channel < pcm.shape[0]]
    return pcm[usable, max(0, start):max(0, end)]


def build_band_preview(pcm: np.ndarray, rate: int, start: int, end: int,
                       low: float, high: float) -> np.ndarray:
    """Filter the selected range with bounded context and a short ramp at each end."""
    from scipy.signal import butter, sosfiltfilt

    context = round(rate * 0.1)
    lo, hi = max(0, start - context), min(pcm.shape[1], end + context)
    sos = butter(4, [low, high], btype="band", fs=rate, output="sos")
    filtered = sosfiltfilt(sos, pcm[:, lo:hi].astype(np.float64), axis=1)
    output = np.ascontiguousarray(filtered[:, start - lo:end - lo], dtype=np.float32)
    edge = min(round(rate * 0.01), output.shape[1] // 2)
    if edge > 0:
        ramp = np.linspace(0.0, 1.0, edge, dtype=np.float32)
        output[:, :edge] *= ramp
        output[:, -edge:] *= ramp[::-1]
    return output


def render_native_clip_pcm(project: EditProject, clip: ClipInstance, fps: float,
                           rate: int, cancelled: Callable[[], bool]) -> np.ndarray | None:
    """Read the same native source-time mapping the offline timeline uses."""
    from src.features.edit.core.channel_layout import adapt_channels
    from src.features.edit.core.clip_pcm import MAX_CLIP_PCM_SECONDS
    from src.features.edit.core.interchange import frames_to_samples
    from src.features.edit.core.stem_export import create_clip_decode_fn

    frame_count = clip.timeline_duration
    total = frames_to_samples(frame_count, fps, rate)
    if total <= 0 or total > round(MAX_CLIP_PCM_SECONDS * rate):
        raise ValueError(f"Native retime audition supports up to {MAX_CLIP_PCM_SECONDS:g} seconds")
    decode, close = create_clip_decode_fn(project, fps, rate)
    output = None
    try:
        lo = 0
        for offset in range(frame_count):
            if cancelled():
                return None
            hi = frames_to_samples(offset + 1, fps, rate)
            block = decode(None, clip, clip.timeline_start + offset, fps, hi - lo)
            if block is not None and block.shape[1] and hi > lo:
                if output is None:
                    output = np.zeros((min(2, block.shape[0]), total), dtype=np.float32)
                if block.shape[0] != output.shape[0]:
                    block = adapt_channels(block, output.shape[0])
                count = min(hi - lo, block.shape[1])
                output[:, lo:lo + count] = block[:, :count]
            lo = hi
    finally:
        close()
    if output is None:
        raise ValueError("Native retime produced no audio samples")
    return output
