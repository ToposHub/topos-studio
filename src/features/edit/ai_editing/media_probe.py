"""Provider-neutral media probing for the post-export quality gate (P8).

The probe is deliberately separate from the exporter.  A process exit code is
not a deliverable contract; measurements are converted to the same
``DeliveryQualityInput`` used by fixture tests.  Two backends share one
contract:

- :class:`PyAVMediaProbe` (primary): in-process decode via the bundled
  PyAV/FFmpeg libraries — packaged builds need no system ffprobe.
- :class:`FfprobeMediaProbe` (fallback): bounded, non-shell ``ffprobe``
  subprocess resolved via :mod:`src.shared.ffmpeg_locator`; the command is
  fixed and bounded by a timeout so an output file cannot turn into an
  arbitrary command execution surface.

Use :func:`create_media_probe` to get the best available backend.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from dataclasses import dataclass, replace
from fractions import Fraction
from pathlib import Path
from typing import Any, Callable, Iterable

from src.features.edit.ai_editing.delivery_quality_gate import (
    DeliveryQualityInput,
    DeliveryQualityResult,
    evaluate_delivery_quality,
)
from src.features.edit.ai_editing.privacy_guard import redact_text
from src.features.edit.ai_editing.release_test_matrix import (
    DEFAULT_COMMERCIAL_MATRIX_DIMENSIONS,
    MatrixCase,
    MatrixResult,
    evaluate_test_matrix,
)
from src.shared.ffmpeg_locator import find_ffprobe


@dataclass(frozen=True)
class MediaProbeResult:
    succeeded: bool
    quality_input: DeliveryQualityInput
    error_code: str = ""
    metadata: dict[str, Any] | None = None


class MediaProbeError(RuntimeError):
    """Raised only by direct probe callers that opt into exception semantics."""


@dataclass(frozen=True)
class RealMediaMatrixSpec:
    """Runtime-only input for one real delivery matrix case.

    The path is consumed by the bounded probe and never copied into the
    resulting matrix case or its serialized evidence.
    """

    name: str
    category: str
    output_path: str | os.PathLike[str]
    task_type: str
    platform: str
    codec: str
    provider_id: str
    expected_resolution: tuple[int, int] | None = None
    expected_fps: str | None = None
    expected_duration_seconds: float | None = None
    expected_audio_sample_rate: int | None = None


def build_real_matrix_case_from_probe(
    output_path: str | os.PathLike[str],
    *,
    probe: "FfprobeMediaProbe",
    name: str,
    category: str,
    task_type: str,
    platform: str,
    codec: str,
    provider_id: str,
    expected_resolution: tuple[int, int] | None = None,
    expected_fps: str | None = None,
    expected_duration_seconds: float | None = None,
    expected_audio_sample_rate: int | None = None,
) -> MatrixCase:
    """Turn a bounded real-media probe into a release-matrix case.

    The returned case only carries a hash-based evidence reference.  A case
    can be ``real`` yet failed (for example a corrupt export); this preserves
    the fact that a real provider was exercised while keeping the failed case
    visible to the release gate.
    """
    result, quality = probe.probe_and_evaluate(
        output_path,
        expected_resolution=expected_resolution,
        expected_fps=expected_fps,
        expected_duration_seconds=expected_duration_seconds,
        expected_audio_sample_rate=expected_audio_sample_rate,
    )
    metadata = result.metadata or {}
    output_hash = result.quality_input.output_hash or ""
    evidence_ref = f"sha256:{output_hash}" if output_hash else ""
    codec_matches = _codec_matches(metadata.get("video_codec"), codec)
    blocking = list(quality.blocking)
    if result.succeeded and not codec_matches:
        blocking.append("输出 codec 与矩阵目标不一致")
    passed = result.succeeded and quality.passed and codec_matches and bool(output_hash)
    if not result.succeeded:
        error_code = result.error_code or "PROBE_FAILED"
    elif not output_hash:
        error_code = "PROBE_HASH_MISSING"
        blocking.append("真实矩阵证据缺少输出 hash")
    elif not codec_matches:
        error_code = "CODEC_MISMATCH"
    elif not quality.passed:
        error_code = "QUALITY_GATE_FAILED"
    else:
        error_code = ""
    return MatrixCase(
        name=name,
        category=category,
        passed=passed,
        error_code=error_code,
        error_message=redact_text("; ".join(blocking))[:300],
        execution_mode="real",
        evidence_ref=evidence_ref,
        provider_id=provider_id,
        task_type=task_type,
        platform=platform,
        codec=codec,
    )


def run_real_media_matrix(
    specs: Iterable[RealMediaMatrixSpec],
    *,
    probe: "FfprobeMediaProbe | None" = None,
    required_dimensions: tuple[tuple[str, tuple[str, ...]], ...] = (
        DEFAULT_COMMERCIAL_MATRIX_DIMENSIONS
    ),
) -> MatrixResult:
    """Probe a real-media matrix and return a release-ready aggregate.

    Every spec is evaluated independently.  Missing files, bad encodes and
    absent hashes stay as individual failed cases, while the aggregate also
    reports coverage and real-evidence gaps.
    """
    active_probe = probe or create_media_probe()
    cases = tuple(
        build_real_matrix_case_from_probe(
            spec.output_path,
            probe=active_probe,
            name=spec.name,
            category=spec.category,
            task_type=spec.task_type,
            platform=spec.platform,
            codec=spec.codec,
            provider_id=spec.provider_id,
            expected_resolution=spec.expected_resolution,
            expected_fps=spec.expected_fps,
            expected_duration_seconds=spec.expected_duration_seconds,
            expected_audio_sample_rate=spec.expected_audio_sample_rate,
        )
        for spec in specs
    )
    return evaluate_test_matrix(
        cases,
        min_pass_rate=1.0,
        require_real_evidence=True,
        required_dimensions=required_dimensions,
    )


def _evaluate_with_expectations(
    result: MediaProbeResult,
    *,
    expected_resolution: tuple[int, int] | None = None,
    expected_fps: str | None = None,
    expected_duration_seconds: float | None = None,
    expected_audio_sample_rate: int | None = None,
) -> DeliveryQualityResult:
    """Attach caller expectations to a probe result and run the quality gate."""
    quality = replace(
        result.quality_input,
        expected_resolution=expected_resolution,
        expected_fps=expected_fps,
        expected_duration_seconds=expected_duration_seconds,
        expected_audio_sample_rate=expected_audio_sample_rate,
    )
    return evaluate_delivery_quality(quality)


class PyAVMediaProbe:
    """Read basic video/audio measurements in-process via bundled PyAV.

    Produces the same ``MediaProbeResult``/metadata contract as
    :class:`FfprobeMediaProbe` without any external binary, so packaged
    builds stay self-contained.  ``fps`` is formatted as a rational string
    (``"30000/1001"``) to mirror ffprobe's ``r_frame_rate``.
    """

    def __init__(self, *, include_output_hash: bool = True) -> None:
        self._include_output_hash = include_output_hash

    def probe(self, output_path: str | os.PathLike[str]) -> MediaProbeResult:
        path = Path(output_path)
        if not path.is_file():
            return MediaProbeResult(
                False,
                DeliveryQualityInput(output_exists=False, output_reopenable=False),
                "OUTPUT_NOT_FOUND",
            )
        try:
            import av

            with av.open(str(path)) as container:
                video = next(iter(container.streams.video), None)
                audio = next(iter(container.streams.audio), None)
                if video is None and audio is None:
                    return MediaProbeResult(
                        False,
                        DeliveryQualityInput(output_exists=True, output_reopenable=False),
                        "PROBE_FAILED",
                    )
                duration: float | None = None
                if container.duration:
                    duration = float(container.duration / av.time_base)
                if duration is None and video is not None and video.duration:
                    duration = float(video.duration * video.time_base)
                if duration is None and audio is not None and audio.duration:
                    duration = float(audio.duration * audio.time_base)

                resolution: tuple[int, int] | None = None
                fps: str | None = None
                video_codec = ""
                pixel_format = ""
                video_profile = ""
                video_bitrate: float | None = None
                if video is not None:
                    width = int(video.codec_context.width or 0)
                    height = int(video.codec_context.height or 0)
                    if width > 0 and height > 0:
                        resolution = (width, height)
                    rate: Fraction | None = (
                        video.average_rate or video.base_rate
                    )
                    if rate is not None:
                        fps = f"{rate.numerator}/{rate.denominator}"
                    video_codec = str(video.codec_context.name or "")
                    pix_fmt = getattr(video, "pix_fmt", None)
                    if isinstance(pix_fmt, str):
                        pixel_format = pix_fmt
                    else:
                        pixel_format = str(getattr(pix_fmt, "name", "") or "")
                    video_profile = str(
                        getattr(video.codec_context, "profile", "") or ""
                    )
                    raw_bitrate = video.bit_rate or container.bit_rate
                    if raw_bitrate:
                        video_bitrate = float(raw_bitrate) / 1000.0

                audio_codec = ""
                sample_rate: int | None = None
                if audio is not None:
                    audio_codec = str(audio.codec_context.name or "")
                    if audio.rate:
                        sample_rate = int(audio.rate)

                container_name = str(container.format.name or "")
                file_size = path.stat().st_size
                metadata: dict[str, Any] = {
                    "video_codec": video_codec,
                    "pixel_format": pixel_format,
                    "video_profile": video_profile,
                    "container": container_name,
                    "format_name": container_name,
                    "audio_codec": audio_codec,
                    "audio_sample_rate": sample_rate,
                    "audio_present": audio is not None,
                    "duration_seconds": duration,
                    "file_size_bytes": file_size,
                    "video_bitrate_kbps": video_bitrate,
                    "resolution": list(resolution) if resolution else None,
                    "fps": fps,
                }
                return MediaProbeResult(
                    True,
                    DeliveryQualityInput(
                        resolution=resolution,
                        fps=fps,
                        duration_seconds=duration,
                        audio_present=audio is not None,
                        audio_sample_rate=sample_rate,
                        video_codec=video_codec or None,
                        container=container_name or None,
                        file_size_bytes=file_size,
                        video_bitrate_kbps=video_bitrate,
                        output_exists=True,
                        output_reopenable=True,
                        output_hash=(
                            _sha256(path) if self._include_output_hash else None
                        ),
                    ),
                    metadata=metadata,
                )
        except Exception:
            return MediaProbeResult(
                False,
                DeliveryQualityInput(output_exists=True, output_reopenable=False),
                "PROBE_FAILED",
            )

    def probe_and_evaluate(
        self,
        output_path: str | os.PathLike[str],
        *,
        expected_resolution: tuple[int, int] | None = None,
        expected_fps: str | None = None,
        expected_duration_seconds: float | None = None,
        expected_audio_sample_rate: int | None = None,
    ) -> tuple[MediaProbeResult, DeliveryQualityResult]:
        result = self.probe(output_path)
        return result, _evaluate_with_expectations(
            result,
            expected_resolution=expected_resolution,
            expected_fps=expected_fps,
            expected_duration_seconds=expected_duration_seconds,
            expected_audio_sample_rate=expected_audio_sample_rate,
        )


def create_media_probe(
    *,
    include_output_hash: bool = True,
) -> "PyAVMediaProbe | FfprobeMediaProbe":
    """Return the best available probe backend (PyAV first, ffprobe fallback)."""
    try:
        import av  # noqa: F401
    except ImportError:
        pass
    else:
        return PyAVMediaProbe(include_output_hash=include_output_hash)
    return FfprobeMediaProbe(
        executable=find_ffprobe() or "ffprobe",
        include_output_hash=include_output_hash,
    )


class FfprobeMediaProbe:
    """Read basic video/audio measurements using a bounded ``ffprobe`` call."""

    def __init__(
        self,
        *,
        executable: str = "ffprobe",
        timeout_seconds: float = 20.0,
        include_output_hash: bool = True,
        runner: Callable[..., Any] | None = None,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._executable = executable
        self._timeout = timeout_seconds
        self._include_output_hash = include_output_hash
        self._runner = runner or subprocess.run

    def probe(self, output_path: str | os.PathLike[str]) -> MediaProbeResult:
        path = Path(output_path)
        exists = path.is_file()
        if not exists:
            return MediaProbeResult(
                False,
                DeliveryQualityInput(output_exists=False, output_reopenable=False),
                "OUTPUT_NOT_FOUND",
            )
        try:
            completed = self._runner(
                [
                    self._executable,
                    "-v", "error",
                    "-print_format", "json",
                    "-show_streams",
                    "-show_format",
                    str(path),
                ],
                capture_output=True,
                text=True,
                timeout=self._timeout,
                check=False,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            return MediaProbeResult(
                False,
                DeliveryQualityInput(output_exists=True, output_reopenable=False),
                "PROBE_UNAVAILABLE",
            )
        if int(getattr(completed, "returncode", 1)) != 0:
            return MediaProbeResult(
                False,
                DeliveryQualityInput(output_exists=True, output_reopenable=False),
                "PROBE_FAILED",
            )
        try:
            payload = json.loads(str(getattr(completed, "stdout", "")))
            quality, metadata = self._quality_input(
                path, payload, include_output_hash=self._include_output_hash,
            )
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return MediaProbeResult(
                False,
                DeliveryQualityInput(output_exists=True, output_reopenable=False),
                "PROBE_INVALID_JSON",
            )
        return MediaProbeResult(True, quality, metadata=metadata)

    def probe_and_evaluate(
        self,
        output_path: str | os.PathLike[str],
        *,
        expected_resolution: tuple[int, int] | None = None,
        expected_fps: str | None = None,
        expected_duration_seconds: float | None = None,
        expected_audio_sample_rate: int | None = None,
    ) -> tuple[MediaProbeResult, DeliveryQualityResult]:
        result = self.probe(output_path)
        return result, _evaluate_with_expectations(
            result,
            expected_resolution=expected_resolution,
            expected_fps=expected_fps,
            expected_duration_seconds=expected_duration_seconds,
            expected_audio_sample_rate=expected_audio_sample_rate,
        )

    @staticmethod
    def _quality_input(
        path: Path,
        payload: dict[str, Any],
        *,
        include_output_hash: bool = True,
    ) -> tuple[DeliveryQualityInput, dict[str, Any]]:
        streams = payload.get("streams") or ()
        video = next((item for item in streams if item.get("codec_type") == "video"), {})
        audio = next((item for item in streams if item.get("codec_type") == "audio"), {})
        format_info = payload.get("format") or {}
        resolution = None
        if video.get("width") is not None and video.get("height") is not None:
            resolution = (int(video["width"]), int(video["height"]))
        fps = str(video.get("r_frame_rate") or "") or None
        duration_raw = format_info.get("duration")
        duration = float(duration_raw) if duration_raw not in (None, "") else None
        sample_rate = int(audio["sample_rate"]) if audio.get("sample_rate") else None
        file_size = _optional_int(format_info.get("size"))
        video_bitrate = _optional_float(video.get("bit_rate"))
        if video_bitrate is not None:
            video_bitrate /= 1000.0
        container = str(format_info.get("format_name") or "")
        metadata: dict[str, Any] = {
            "video_codec": str(video.get("codec_name") or ""),
            "pixel_format": str(video.get("pix_fmt") or ""),
            "video_profile": str(video.get("profile") or ""),
            "container": container,
            "format_name": container,
            "audio_codec": str(audio.get("codec_name") or ""),
            "audio_sample_rate": sample_rate,
            "audio_present": bool(audio),
            "duration_seconds": duration,
            "file_size_bytes": file_size if file_size is not None else path.stat().st_size,
            "video_bitrate_kbps": video_bitrate,
            "resolution": list(resolution) if resolution else None,
            "fps": fps,
        }
        return DeliveryQualityInput(
            resolution=resolution,
            fps=fps,
            duration_seconds=duration,
            audio_present=bool(audio),
            audio_sample_rate=sample_rate,
            video_codec=str(video.get("codec_name") or "") or None,
            container=container or None,
            file_size_bytes=metadata["file_size_bytes"],
            video_bitrate_kbps=video_bitrate,
            output_exists=True,
            output_reopenable=True,
            output_hash=_sha256(path) if include_output_hash else None,
        ), metadata


def _sha256(path: Path, *, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _optional_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _optional_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and abs(number) != float("inf") else None


def _codec_matches(actual: Any, expected: str) -> bool:
    """Compare common ffprobe codec aliases without accepting empty values."""
    actual_value = str(actual or "").strip().lower()
    expected_value = str(expected or "").strip().lower()
    aliases = {
        "h264": {"h264", "avc1"},
        "h265": {"h265", "hevc", "x265"},
    }
    allowed = aliases.get(expected_value, {expected_value})
    return bool(actual_value and expected_value and actual_value in allowed)


__all__ = [
    "FfprobeMediaProbe",
    "MediaProbeError",
    "MediaProbeResult",
    "PyAVMediaProbe",
    "RealMediaMatrixSpec",
    "build_real_matrix_case_from_probe",
    "create_media_probe",
    "run_real_media_matrix",
]
