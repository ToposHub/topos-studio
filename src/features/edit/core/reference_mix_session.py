"""Prepared reference PCM and the monitor-only A/B render stage."""
from __future__ import annotations

import hashlib
import json
import math
import mmap
import re
import tempfile
from dataclasses import dataclass, field, replace
from fractions import Fraction
from pathlib import Path
from typing import Callable

import numpy as np

from src.features.edit.core.channel_layout import ChannelLayout, codec_routing_matrix
from src.features.edit.core.reference_mix import (
    MixVersion, ReferenceMixRef, align_reference_offset, comparison_gain,
)
from src.shared.media.audio_frame_pcm import audio_frame_array

_MAX_PREPARED_BYTES = 8 * 1024 ** 3
_MAX_DELIVERY_MANIFEST_BYTES = 4 * 1024 ** 2
_INPUT_LAYOUTS = {
    "mono": ChannelLayout.MONO,
    "stereo": ChannelLayout.STEREO,
    "quad": ChannelLayout.QUAD,
    "5.1": ChannelLayout.SURROUND_5_1,
    "5.1(side)": ChannelLayout.SURROUND_5_1,
    "7.1": ChannelLayout.SURROUND_7_1,
}


def scale_sample(index: int, source_rate: int, target_rate: int) -> int:
    return round(Fraction(index * target_rate, source_rate))


@dataclass(frozen=True)
class PreparedReference:
    """A read-only scratch mapping independent of the source container and UI."""

    pcm: np.ndarray                     # interleaved float32 (samples, channels)
    sample_rate: int
    source_rate: int
    source_channels: int
    bwf_time_reference: int | None
    source_origin: int                   # sample in prepared PCM at timeline anchor
    timeline_anchor: int
    source_fingerprint: tuple[int, ...]
    sample_peak: float
    token: object = field(default_factory=object, repr=False, compare=False)

    @property
    def channels(self) -> int:
        return self.pcm.shape[1]

    def span(self, timeline_sample: int, frames: int) -> tuple[int, int, int]:
        """Compute a half-open overlap in timeline and source sample coordinates."""
        source = timeline_sample - self.timeline_anchor + self.source_origin
        start = max(0, self.source_origin - source)
        end = min(frames, self.pcm.shape[0] - source)
        return start, max(start, end), source

    def block(self, timeline_sample: int, frames: int) -> np.ndarray:
        start, end, source = self.span(timeline_sample, frames)
        if start == 0 and end == frames:
            return self.pcm[source:source + frames].T
        output = np.zeros((self.channels, frames), dtype=np.float32)
        if end > start:
            output[:, start:end] = self.pcm[source + start:source + end].T
        return output


def _file_identity(path: Path) -> tuple[int, ...]:
    info = path.stat()
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def reference_from_delivery_manifest(path: str, reference: ReferenceMixRef) -> ReferenceMixRef:
    """Bind only a passed full programme print to its delivered evidence.

    The linked file's bytes are checked before preparation and on reopen.
    """
    from src.features.edit.core.delivery_profile import DeliveryProfile, MeasurementResult
    from src.features.edit.core.delivery_readback import delivery_profile_rejection

    wav = Path(path)
    manifest_path = wav.with_name("delivery.json")
    with manifest_path.open("rb") as file:
        raw = file.read(_MAX_DELIVERY_MANIFEST_BYTES + 1)
    if len(raw) > _MAX_DELIVERY_MANIFEST_BYTES:
        raise ValueError("Delivery manifest exceeds the reference metadata size budget")
    manifest = json.loads(raw)
    if (not isinstance(manifest, dict) or manifest.get("manifest_schema") != "topos.delivery_manifest.v2"
            or manifest.get("qc_passed") is not True):
        raise ValueError("Reference must have a QC-passed delivery manifest")
    prints = manifest.get("full_mix_prints")
    meta = prints.get(wav.name) if isinstance(prints, dict) else None
    if not isinstance(meta, dict) or type(meta.get("version")) is not int or meta["version"] != 1:
        raise ValueError("The selected file is not a verified full Master print")
    sequence_id = meta.get("sequence_id")
    revision = meta.get("source_revision")
    start = meta.get("start_sample")
    if (not isinstance(sequence_id, str) or not sequence_id or type(revision) is not int
            or revision < 0 or type(start) is not int or start < 0):
        raise ValueError("Full Mix print is missing its sequence identity or sample anchor")
    files = manifest.get("files")
    matches = [entry for entry in files if isinstance(entry, dict) and entry.get("path") == wav.name
               and entry.get("stem") == wav.name] if isinstance(files, list) else []
    if len(matches) != 1:
        raise ValueError("Full Mix print has no unique delivery entry")
    entry = matches[0]
    sha = entry.get("sha256")
    sound_hash = manifest.get("sound_hash")
    if (not isinstance(sha, str) or not re.fullmatch(r"[a-f0-9]{64}", sha)
            or not isinstance(sound_hash, str) or not re.fullmatch(r"[a-f0-9]{16}|[a-f0-9]{64}", sound_hash)):
        raise ValueError("Full Mix print has no valid file or graph checksum")
    try:
        profile = DeliveryProfile.from_dict(manifest["profile"])
        result = MeasurementResult.from_dict(entry["measurement"])
    except (ValueError, KeyError, TypeError, AttributeError) as error:
        raise ValueError("Invalid Full Mix delivery evidence") from error
    rejection = delivery_profile_rejection(result, profile, expected_sha256=sha)
    if (rejection is not None or result.stem_id != "FullMix"
            or result.sample_count != entry.get("sample_count") or result.sample_rate != entry.get("sample_rate")):
        raise ValueError(rejection or "Full Mix measurement does not match the delivery record")
    spec = {
        "sound_hash": sound_hash, "source": meta, "profile": profile.to_dict(),
        "sample_count": result.sample_count, "file_sha256": sha,
    }
    render_hash = hashlib.sha256(json.dumps(spec, sort_keys=True, separators=(",", ":"),
                                          allow_nan=False).encode("utf-8")).hexdigest()
    version = MixVersion(reference.version_id, revision, render_hash, reference.asset_id,
                         label=reference.label, measured_report_ref=manifest_path.name,
                         source_sequence_id=sequence_id, file_sha256=sha)
    return replace(reference, mix_version=version, provenance="verified_full_mix",
                   original_rate=result.sample_rate, timeline_rate=result.sample_rate,
                   timeline_anchor_sample=start + reference.source_start_sample)


def _matches_file_checksum(
    path: Path, expected: str, cancel_check: Callable[[], bool],
) -> bool | None:
    checksum = hashlib.sha256()
    with path.open("rb") as source:
        while raw := source.read(1024 * 1024):
            if cancel_check():
                return None
            checksum.update(raw)
    return checksum.hexdigest() == expected


def prepare_reference(
    path: str, reference: ReferenceMixRef, sample_rate: int, channels: int,
    *, offset_seconds: float = 0.0, allow_unknown_source_rate: bool = False,
    cancel_check: Callable[[], bool] = lambda: False,
) -> PreparedReference | None:
    """Decode one continuous resampler stream off-thread into a bounded PCM file."""
    import av

    if not 1 <= sample_rate <= 768000 or not math.isfinite(offset_seconds):
        raise ValueError("Invalid monitor format or source offset")
    target = ChannelLayout.from_channel_count(channels)
    source_path = Path(path)
    fingerprint = _file_identity(source_path)
    if not source_path.is_file():
        raise ValueError("Reference source must be a local media file")
    if reference.mix_version is not None:
        if offset_seconds:
            raise ValueError("Verified prints require their original, unbound audio asset")
        matches = _matches_file_checksum(source_path, reference.mix_version.file_sha256, cancel_check)
        if matches is None:
            return None
        if not matches:
            raise ValueError("Reference audio differs from the verified Full Mix delivery")
    written = 0
    sample_peak = 0.0
    with tempfile.TemporaryFile(prefix="topos-reference-") as scratch:
        try:
            with av.open(str(source_path)) as container:
                stream = next(iter(container.streams.audio), None)
                if stream is None:
                    raise ValueError("Reference file has no audio stream")
                source_rate = stream.codec_context.sample_rate
                if not 1 <= source_rate <= 768000:
                    raise ValueError("Invalid reference source rate")
                if not allow_unknown_source_rate and source_rate != reference.original_rate:
                    raise ValueError("Reference source sample rate has changed")
                raw_bwf = container.metadata.get("time_reference", "")
                bwf = int(raw_bwf) if raw_bwf.isascii() and raw_bwf.isdecimal() else None
                source = None
                resampler = None
                source_channels = 0
                matrix = None

                def store(frame: av.AudioFrame) -> None:
                    nonlocal written, sample_peak
                    data = audio_frame_array(frame)
                    if reference.channel_map:
                        data = data[list(reference.channel_map)]
                    if matrix is not None:
                        data = matrix @ data
                    if not np.isfinite(data).all():
                        raise ValueError("Reference contains non-finite PCM")
                    sample_peak = max(sample_peak, float(np.max(np.abs(data), initial=0.0)))
                    packed = np.ascontiguousarray(data.T, dtype=np.float32)
                    if written + packed.nbytes > _MAX_PREPARED_BYTES:
                        raise ValueError("Reference exceeds the 8 GiB decoded PCM budget")
                    scratch.write(packed.tobytes())
                    written += packed.nbytes

                for frame in container.decode(stream):
                    if cancel_check():
                        return None
                    if resampler is None:
                        source_channels = len(frame.layout.channels)
                        # Unlabelled PCM mono/stereo WAV follows the container
                        # channel order. Surround requires a declared layout.
                        input_layout = frame.layout.name
                        if input_layout in ("1 channels", "2 channels"):
                            source = ChannelLayout.from_channel_count(source_channels)
                            layout = source.av_layout_name
                        else:
                            layout = input_layout
                            source = _INPUT_LAYOUTS.get(layout)
                        if source is None:
                            raise ValueError(f"Unsupported reference layout: {layout}")
                        if reference.channel_map:
                            if max(reference.channel_map) >= source_channels:
                                raise ValueError("Reference channel map exceeds source channels")
                            mapped = ChannelLayout.from_channel_count(len(reference.channel_map))
                        else:
                            mapped = source
                        if mapped != target:
                            if mapped is ChannelLayout.MONO and target is ChannelLayout.STEREO:
                                # Stereo playback of mono uses a phantom centre.
                                matrix = np.full((2, 1), np.sqrt(0.5), np.float32)
                            else:
                                matrix = codec_routing_matrix(
                                    mapped, target, "" if reference.channel_map else layout)
                        resampler = av.AudioResampler(format="fltp", layout=layout, rate=sample_rate)
                    if frame.sample_rate != source_rate or frame.layout.name != input_layout:
                        raise ValueError("Reference audio format changed within the file")
                    for audio in resampler.resample(frame):
                        store(audio)
                if resampler is None:
                    raise ValueError("Reference source contains no decoded audio")
                for audio in resampler.resample(None):
                    store(audio)
        except av.error.FFmpegError as error:
            raise ValueError(f"Reference decode failed: {error}") from error
        if cancel_check():
            return None
        if fingerprint != _file_identity(source_path):
            raise ValueError("Reference file changed during preparation")
        offset = round(offset_seconds * sample_rate)
        origin = scale_sample(reference.source_start_sample, source_rate, sample_rate) + offset
        if origin < 0 or origin >= written // (4 * channels):
            raise ValueError("Reference source in-point falls outside decoded audio")
        scratch.flush()
        mapping = mmap.mmap(scratch.fileno(), written, access=mmap.ACCESS_READ)
        pcm = np.ndarray((written // (4 * channels), channels), dtype=np.float32, buffer=mapping)
    anchor = scale_sample(reference.timeline_anchor_sample, reference.timeline_rate, sample_rate)
    return PreparedReference(pcm, sample_rate, source_rate, source_channels, bwf,
                             origin, anchor, fingerprint, sample_peak)


@dataclass(frozen=True)
class ReferenceCapture:
    """Memory is allocated by the UI and filled once by the output worker."""

    start_sample: int | None
    sample_rate: int
    programme: np.ndarray
    reference: np.ndarray


@dataclass(frozen=True)
class ReferenceMeasurement:
    programme_lufs: float | None
    reference_lufs: float | None
    reference_true_peak: float | None
    match_gain: float | None
    estimated_delay: int | None
    confidence: float | None
    start_sample: int
    sample_count: int
    sample_rate: int


def measure_reference_capture(
    capture: ReferenceCapture, cancel_check: Callable[[], bool],
) -> ReferenceMeasurement | None:
    """Use shared programme loudness and true peak meters on the same interval."""
    from src.features.edit.core.audio_capabilities import is_scipy_available_for_dsp
    from src.features.edit.core.loudness_analyzer import _TruePeakEstimator
    from src.features.edit.core.loudness_meter import LoudnessMeter

    if capture.start_sample is None:
        raise ValueError("Reference measurement has no sample interval")
    channels, frames = capture.programme.shape
    rate = capture.sample_rate
    programme_lufs = reference_lufs = peak = gain = None
    if rate in (44100, 48000) and channels in (1, 2, 6, 8) and is_scipy_available_for_dsp():
        programme_meter = LoudnessMeter(rate, channels)
        reference_meter = LoudnessMeter(rate, channels)
        peak_meter = _TruePeakEstimator(channels)
        for start in range(0, frames, rate // 10):
            if cancel_check():
                return None
            end = start + rate // 10
            programme_meter.process(capture.programme[:, start:end])
            block = capture.reference[:, start:end]
            reference_meter.process(block)
            peak_meter.process(block)
        peak_meter.finalize()
        if peak_meter.peak > 0.0:
            peak = 20.0 * math.log10(peak_meter.peak)
        if programme_meter.integrated_lufs > -70.0:
            programme_lufs = programme_meter.integrated_lufs
        if reference_meter.integrated_lufs > -70.0:
            reference_lufs = reference_meter.integrated_lufs
        if programme_lufs is not None and reference_lufs is not None and peak is not None:
            try:
                proposed = comparison_gain(reference_lufs, programme_lufs)
                if peak + 20.0 * math.log10(proposed) <= -1.0:
                    gain = proposed
            except ValueError:
                pass
    delay = confidence = None
    if cancel_check():
        return None
    try:
        delay, confidence = align_reference_offset(capture.programme, capture.reference,
                                                  max_offset_samples=rate)
    except ValueError:
        pass
    return ReferenceMeasurement(programme_lufs, reference_lufs, peak, gain, delay, confidence,
                                capture.start_sample, frames, rate)


@dataclass(frozen=True)
class _MonitorRequest:
    source: PreparedReference | None = None
    active: bool = False
    gain: float = 1.0
    capture: ReferenceCapture | None = None


class ReferenceMixMonitor:
    """UI publishes a single immutable request; the output worker owns ramps."""

    def __init__(self) -> None:
        self._request = _MonitorRequest()
        self.render_format: tuple[int, int] | None = None
        self._render_token: object | None = None
        self._weight = 0.0
        self._gain = 1.0
        self._next_sample: int | None = None
        self._capture: ReferenceCapture | None = None
        self._capture_position = 0
        self._capture_start = 0
        self._capture_result: tuple[ReferenceCapture, ReferenceCapture | str] | None = None

    @property
    def source(self) -> PreparedReference | None:
        return self._request.source

    @property
    def reference_selected(self) -> bool:
        return self._request.active

    @property
    def match_gain(self) -> float:
        return self._request.gain

    def set_source(self, source: PreparedReference) -> None:
        self._request = _MonitorRequest(source)

    def clear(self) -> None:
        self._request = _MonitorRequest()

    def select_reference(self, active: bool) -> None:
        self._request = replace(self._request, active=bool(active and self._request.source))

    def set_match_gain(self, gain: float) -> None:
        if not math.isfinite(gain) or not 0 < gain <= 10 ** (12.0 / 20.0):
            raise ValueError("Invalid reference gain")
        self._request = replace(self._request, gain=gain)

    def start_measurement(self) -> ReferenceCapture:
        source = self._request.source
        if source is None:
            raise ValueError("Prepare a reference mix before measuring")
        shape = (source.channels, source.sample_rate * 3)
        capture = ReferenceCapture(None, source.sample_rate,
                                   np.empty(shape, np.float32), np.empty(shape, np.float32))
        self._request = replace(self._request, capture=capture)
        return capture

    def cancel_measurement(self) -> None:
        self._request = replace(self._request, capture=None)

    def measurement_result(self, capture: ReferenceCapture) -> ReferenceCapture | str | None:
        result = self._capture_result
        return result[1] if result is not None and result[0] is capture else None

    def reset_transport(self) -> None:
        self._next_sample = None
        self._weight = 0.0
        self._fail_capture("transport")

    def _fail_capture(self, reason: str) -> None:
        if self._capture is not None and self._capture_position < self._capture.programme.shape[1]:
            self._capture_result = self._capture, reason

    def _collect(
        self, capture: ReferenceCapture | None, programme: np.ndarray,
        reference: PreparedReference, start: int,
    ) -> None:
        if capture is None:
            self._capture = None
            self._capture_result = None
            return
        if capture is not self._capture:
            self._capture = capture
            self._capture_result = None
            self._capture_position = 0
            self._capture_start = start
        if self._capture_result is not None:
            return
        size = min(programme.shape[1], capture.programme.shape[1] - self._capture_position)
        lo, hi, source_start = reference.span(start, size)
        if lo or hi != size:
            self._fail_capture("reference_range")
            return
        destination = slice(self._capture_position, self._capture_position + size)
        capture.programme[:, destination] = programme[:, :size]
        capture.reference[:, destination] = reference.pcm[source_start:source_start + size].T
        self._capture_position += size
        if self._capture_position == capture.programme.shape[1]:
            capture.programme.setflags(write=False)
            capture.reference.setflags(write=False)
            self._capture_result = capture, replace(capture, start_sample=self._capture_start)

    def render_block(self, programme: np.ndarray, timeline_sample: int, *, sample_rate: int) -> np.ndarray:
        self.render_format = (sample_rate, programme.shape[0])
        request = self._request
        source = request.source
        if source is None or self.render_format != (source.sample_rate, source.channels):
            self._render_token = None
            self._weight = 0.0
            self._gain = 1.0
            self._fail_capture("format")
            return programme
        frames = programme.shape[1]
        if not frames:
            return programme
        if self._next_sample not in (None, timeline_sample):
            self.reset_transport()
        self._next_sample = timeline_sample + frames
        if self._render_token is not source.token:
            self._render_token = source.token
            self._weight = 0.0
            self._gain = 1.0
        self._collect(request.capture, programme, source, timeline_sample)
        start, end, _ = source.span(timeline_sample, frames)
        if start >= frames or end <= start:
            # A reference which has ended cannot strand the programme in silence.
            self._weight = 0.0
            return programme
        target = float(request.active)
        if self._weight == target == 0.0:
            return programme
        reference = source.block(timeline_sample, frames)
        ramp = max(1, round(sample_rate * 0.005))
        step = np.arange(1, frames + 1, dtype=np.float32) / ramp
        weight_gap = target - self._weight
        weights = self._weight + np.sign(weight_gap) * np.minimum(np.abs(weight_gap), step)
        gain_gap = request.gain - self._gain
        gains = self._gain + np.sign(gain_gap) * np.minimum(np.abs(gain_gap), step * (10 ** (12.0 / 20.0)))
        # Programme remains audible outside the reference window. Fade at the
        # anchored boundaries too, including a split across device blocks.
        samples_from_start = timeline_sample - source.timeline_anchor
        samples_to_end = source.pcm.shape[0] - (samples_from_start + source.source_origin)
        position = np.arange(frames, dtype=np.float32)
        coverage = np.clip(np.minimum(samples_from_start + position + 1, samples_to_end - position)
                           / ramp, 0.0, 1.0)
        self._weight = float(weights[-1])
        self._gain = float(gains[-1])
        weights *= coverage
        return programme * (1.0 - weights) + reference * (weights * gains)
