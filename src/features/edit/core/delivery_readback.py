"""Read and measure the encoded delivery, without changing its sound.

The current stem writer produces mono/stereo integer PCM WAV. Only that
verified subset is accepted here; unsupported codecs/layouts never inherit
metadata from a requested profile and masquerade as measured output.
"""
from __future__ import annotations

import hashlib
import math
import wave
from pathlib import Path
from typing import Callable, Optional

import numpy as np

from src.features.edit.core.audio_capabilities import is_scipy_available_for_dsp
from src.features.edit.core.delivery_profile import (
    DeliveryProfile, MeasurementResult, measurement_rejection,
)
from src.features.edit.core.loudness_analyzer import _TruePeakEstimator
from src.features.edit.core.loudness_meter import LoudnessMeter

READBACK_ALGORITHM = "topos.wav-readback/1;bs1770-4;true-peak-sinc4x/1"


def _pcm_float(raw: bytes, width: int, channels: int) -> np.ndarray:
    """Decode interleaved little-endian signed integer PCM to planar float."""
    if width == 2:
        values = np.frombuffer(raw, dtype="<i2").astype(np.float64) / 32768.0
    elif width == 3:
        octets = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3).astype(np.int32)
        integers = octets[:, 0] | (octets[:, 1] << 8) | (octets[:, 2] << 16)
        integers = (integers ^ 0x800000) - 0x800000
        values = integers.astype(np.float64) / 8388608.0
    else:
        raise ValueError("readback supports signed 16/24-bit PCM WAV only")
    return values.reshape(-1, channels).T


def measure_delivery_wav(
    path: Path,
    *,
    stem_id: str = "FullMix",
    cancel_check: Optional[Callable[[], bool]] = None,
) -> MeasurementResult:
    """Stream the actual final PCM file and bind evidence to its SHA-256.

    Failure/cancellation returns an unusable structured result. Memory remains
    bounded by a 100 ms PCM block and the existing meters' state. LUFS is
    omitted if its backend/rate or minimum analysis window is unavailable.
    """
    path = Path(path)
    identity: dict = {"stem_id": stem_id, "algorithm_version": READBACK_ALGORITHM}
    try:
        before = path.stat()
        sha = hashlib.sha256()
        with path.open("rb") as source:
            while raw := source.read(1024 * 1024):
                if cancel_check is not None and cancel_check():
                    return MeasurementResult(status="cancelled", **identity)
                sha.update(raw)
        identity["file_sha256"] = sha.hexdigest()
        with wave.open(str(path), "rb") as source:
            channels, rate, width, declared_count = (
                source.getnchannels(), source.getframerate(),
                source.getsampwidth(), source.getnframes(),
            )
            if channels not in (1, 2):
                raise ValueError("WAV readback supports verified mono/stereo channel order only")
            if width not in (2, 3) or source.getcomptype() != "NONE":
                raise ValueError("WAV readback requires 16/24-bit integer PCM")
            identity.update(sample_rate=rate,
                            channel_order=("M",) if channels == 1 else ("L", "R"),
                            bit_depth=width * 8)
            meter = LoudnessMeter(rate, channels)
            true_peak = _TruePeakEstimator(channels)
            peak = 0.0
            count = 0
            # One meter hop per feed, rather than duplicating a final window
            # when an entire long decode frame spans several 100 ms hops.
            block_size = max(1, rate // 10)
            while raw := source.readframes(block_size):
                if cancel_check is not None and cancel_check():
                    return MeasurementResult(status="cancelled", **identity)
                if len(raw) % (width * channels):
                    raise ValueError("truncated WAV sample frame")
                pcm = _pcm_float(raw, width, channels)
                count += pcm.shape[1]
                peak = max(peak, float(np.max(np.abs(pcm))))
                meter.process(pcm)
                true_peak.process(pcm)
            if count <= 0 or count != declared_count:
                raise ValueError(f"WAV frame count mismatch or empty file: {count}/{declared_count}")
            true_peak.finalize()
        after = path.stat()
        if (before.st_size, before.st_mtime_ns, before.st_ctime_ns, before.st_dev, before.st_ino) != (
                after.st_size, after.st_mtime_ns, after.st_ctime_ns, after.st_dev, after.st_ino):
            raise ValueError("WAV changed during readback")
        identity["sample_count"] = count
        if peak == 0.0:
            return MeasurementResult(status="silence", **identity)
        measurements = {
            "peak_dbfs": 20.0 * math.log10(peak),
            "true_peak_dbtp": 20.0 * math.log10(max(peak, true_peak.peak)),
        }
        warnings: list[str] = []
        if (is_scipy_available_for_dsp() and rate in (44100, 48000)
                and count >= math.ceil(rate * 0.4) and meter.integrated_lufs > -70.0):
            measurements["integrated_lufs"] = meter.integrated_lufs
        else:
            warnings.append("programme LUFS unavailable (backend/rate/window/gating)")
        return MeasurementResult(status="complete", measurements=measurements,
                                 warnings=tuple(warnings), **identity)
    except (OSError, ValueError, EOFError, wave.Error) as error:
        return MeasurementResult(status="failed", error=str(error), **identity)


def delivery_profile_rejection(
    result: MeasurementResult,
    profile: DeliveryProfile,
    *,
    expected_sha256: str = "",
) -> Optional[str]:
    """Validate evidence first, then apply this stem's declared numeric limits."""
    rejected = measurement_rejection(result, profile, expected_sha256=expected_sha256)
    if rejected is None and result.bit_depth != profile.bit_depth:
        return f"encoded bit depth mismatch: {result.bit_depth} != profile {profile.bit_depth}"
    if rejected is not None or result.status == "silence":
        return rejected
    rule = profile.stem_rule(result.stem_id)
    target = (rule.target_lufs if rule is not None and rule.target_lufs is not None
              else profile.target_lufs)
    peak_limit = (rule.max_true_peak_dbtp if rule is not None
                  and rule.max_true_peak_dbtp is not None else profile.max_true_peak_dbtp)
    if target is not None and abs(result.measurements["integrated_lufs"] - target) > profile.tolerance_lu:
        return f"stem {result.stem_id} programme loudness exceeds profile tolerance"
    if peak_limit is not None and result.measurements["true_peak_dbtp"] > peak_limit:
        return f"stem {result.stem_id} true peak exceeds profile limit"
    if (profile.max_sample_peak_dbfs is not None
            and result.measurements["peak_dbfs"] > profile.max_sample_peak_dbfs):
        return f"stem {result.stem_id} sample peak exceeds profile limit"
    return None
