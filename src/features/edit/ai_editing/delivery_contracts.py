"""Provider-neutral delivery contracts for platform and subtitle QA (P8).

The contracts in this module intentionally do not encode a vendor API.  A
platform profile is a small, serializable export contract supplied by the
brief, a preset, or the host.  The built-in profiles are conservative project
defaults and can be replaced with a custom profile whenever a vendor changes
its limits.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from fractions import Fraction
from typing import Any


@dataclass(frozen=True)
class PlatformDeliverySpec:
    """Machine-checkable platform/export contract."""

    spec_id: str
    allowed_resolutions: tuple[tuple[int, int], ...] = ()
    allowed_aspect_ratios: tuple[str, ...] = ()
    allowed_fps: tuple[str, ...] = ()
    allowed_video_codecs: tuple[str, ...] = ()
    allowed_containers: tuple[str, ...] = ()
    min_duration_seconds: float | None = None
    max_duration_seconds: float | None = None
    max_file_size_bytes: int | None = None
    max_video_bitrate_kbps: int | None = None
    allowed_audio_sample_rates: tuple[int, ...] = ()
    require_audio: bool | None = None

    def __post_init__(self) -> None:
        if not self.spec_id.strip():
            raise ValueError("spec_id cannot be empty")
        for width, height in self.allowed_resolutions:
            if width <= 0 or height <= 0:
                raise ValueError("platform resolutions must be positive")
        for value in (
            self.min_duration_seconds,
            self.max_duration_seconds,
        ):
            if value is not None and (not math.isfinite(value) or value < 0):
                raise ValueError("platform duration limits must be finite and non-negative")
        if (
            self.min_duration_seconds is not None
            and self.max_duration_seconds is not None
            and self.min_duration_seconds > self.max_duration_seconds
        ):
            raise ValueError("min_duration_seconds cannot exceed max_duration_seconds")
        if self.max_file_size_bytes is not None and self.max_file_size_bytes <= 0:
            raise ValueError("max_file_size_bytes must be positive")
        if self.max_video_bitrate_kbps is not None and self.max_video_bitrate_kbps <= 0:
            raise ValueError("max_video_bitrate_kbps must be positive")
        if any(rate <= 0 for rate in self.allowed_audio_sample_rates):
            raise ValueError("audio sample rates must be positive")

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "PlatformDeliverySpec":
        """Build a contract from a JSON-safe host/brief payload."""
        resolutions = data.get("allowed_resolutions", data.get("resolutions", ())) or ()
        normalized_resolutions = tuple(
            (int(item[0]), int(item[1])) for item in resolutions
        )
        return cls(
            spec_id=str(data.get("spec_id", data.get("id", data.get("platform", "custom")))),
            allowed_resolutions=normalized_resolutions,
            allowed_aspect_ratios=_strings(
                data.get("allowed_aspect_ratios", data.get("aspect_ratios", ()))
            ),
            allowed_fps=_strings(data.get("allowed_fps", data.get("fps", ()))),
            allowed_video_codecs=_strings(
                data.get("allowed_video_codecs", data.get("video_codecs", ()))
            ),
            allowed_containers=_strings(
                data.get("allowed_containers", data.get("containers", ()))
            ),
            min_duration_seconds=_optional_float(data.get("min_duration_seconds")),
            max_duration_seconds=_optional_float(data.get("max_duration_seconds")),
            max_file_size_bytes=_optional_int(data.get("max_file_size_bytes")),
            max_video_bitrate_kbps=_optional_int(data.get("max_video_bitrate_kbps")),
            allowed_audio_sample_rates=tuple(
                int(rate) for rate in (data.get("allowed_audio_sample_rates", ()) or ())
            ),
            require_audio=(
                bool(data["require_audio"])
                if data.get("require_audio") is not None
                else None
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "spec_id": self.spec_id,
            "allowed_resolutions": [list(item) for item in self.allowed_resolutions],
            "allowed_aspect_ratios": list(self.allowed_aspect_ratios),
            "allowed_fps": list(self.allowed_fps),
            "allowed_video_codecs": list(self.allowed_video_codecs),
            "allowed_containers": list(self.allowed_containers),
            "min_duration_seconds": self.min_duration_seconds,
            "max_duration_seconds": self.max_duration_seconds,
            "max_file_size_bytes": self.max_file_size_bytes,
            "max_video_bitrate_kbps": self.max_video_bitrate_kbps,
            "allowed_audio_sample_rates": list(self.allowed_audio_sample_rates),
            "require_audio": self.require_audio,
        }


@dataclass(frozen=True)
class PlatformValidationResult:
    spec_id: str
    passed: bool
    errors: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "spec_id": self.spec_id,
            "passed": self.passed,
            "errors": list(self.errors),
        }


def _strings(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,) if value else ()
    return tuple(str(item) for item in (value or ()) if str(item))


def _optional_int(value: Any) -> int | None:
    return None if value in (None, "") else int(value)


def _optional_float(value: Any) -> float | None:
    return None if value in (None, "") else float(value)


def _as_fraction(value: Any) -> Fraction | None:
    try:
        if value in (None, ""):
            return None
        if isinstance(value, Fraction):
            return value
        if isinstance(value, str) and "/" in value:
            numerator, denominator = value.split("/", 1)
            return Fraction(int(numerator), int(denominator))
        return Fraction(str(float(value))).limit_denominator(100_000)
    except (TypeError, ValueError, ZeroDivisionError, OverflowError):
        return None


#: AUD-13313：帧率匹配容差（帧/秒）。覆盖十进制 NTSC 计数（29.97 vs
#: 30000/1001≈29.97003、23.976 vs 24000/1001≈23.97602，差 < 0.001）等探测
#: 舍入；同时保持 30/29.97、24/23.976 等相邻帧率仍可区分（差 ≥ 0.024）。
_FPS_MATCH_TOLERANCE: float = 0.02


def _fps_matches(actual: Any, allowed: Sequence[str]) -> bool:
    actual_fraction = _as_fraction(actual)
    if actual_fraction is None:
        return False
    for item in allowed:
        expected_fraction = _as_fraction(item)
        if expected_fraction is None:
            continue
        if expected_fraction == actual_fraction:
            return True
        if abs(float(expected_fraction) - float(actual_fraction)) < _FPS_MATCH_TOLERANCE:
            return True
    return False


def _aspect_ratio(resolution: tuple[int, int] | None) -> str | None:
    if resolution is None or resolution[0] <= 0 or resolution[1] <= 0:
        return None
    ratio = Fraction(resolution[0], resolution[1])
    return f"{ratio.numerator}:{ratio.denominator}"


def _format_tokens(value: Any) -> set[str]:
    return {
        token.strip().lower()
        for token in str(value or "").split(",")
        if token.strip()
    }


def _first_number(metadata: Mapping[str, Any], *keys: str) -> float | None:
    for key in keys:
        value = metadata.get(key)
        if value in (None, ""):
            continue
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(number):
            return number
    return None


def resolve_platform_spec(
    spec: PlatformDeliverySpec | Mapping[str, Any] | str,
) -> PlatformDeliverySpec | None:
    if isinstance(spec, PlatformDeliverySpec):
        return spec
    if isinstance(spec, Mapping):
        return PlatformDeliverySpec.from_dict(spec)
    key = str(spec).strip().lower()
    aliases = {
        "douyin": "vertical_social",
        "tiktok": "vertical_social",
        "reels": "vertical_social",
        "instagram_reels": "vertical_social",
        "youtube": "horizontal_social",
        "bilibili": "horizontal_social",
    }
    return DEFAULT_PLATFORM_SPECS.get(aliases.get(key, key))


def validate_platform_delivery(
    spec: PlatformDeliverySpec | Mapping[str, Any] | str,
    *,
    metadata: Mapping[str, Any] | None = None,
    quality_input: Any | None = None,
) -> PlatformValidationResult:
    """Validate ffprobe-like metadata against a platform delivery contract."""
    resolved = resolve_platform_spec(spec)
    spec_id = str(getattr(resolved, "spec_id", spec))
    if resolved is None:
        return PlatformValidationResult(spec_id, False, (f"未知平台规格：{spec_id}",))

    observed = dict(metadata or {})
    quality = quality_input
    # AUD-13312：resolution 来自 ffprobe 式外部探测（外部数据须验证）。
    # 仅接受恰好两个可转 int 元素的 list/tuple；标量、单元素、三元组或
    # 字符串等畸形值一律视为"缺少输出分辨率测量"，fail closed 而非让
    # TypeError/IndexError 炸掉整个校验。
    resolution: tuple[int, int] | None = None
    resolution_raw = observed.get("resolution", getattr(quality, "resolution", None))
    if isinstance(resolution_raw, (list, tuple)) and len(resolution_raw) == 2:
        try:
            resolution = (int(resolution_raw[0]), int(resolution_raw[1]))
        except (TypeError, ValueError):
            resolution = None
    fps = observed.get("fps", getattr(quality, "fps", None))
    duration = _first_number(observed, "duration_seconds")
    if duration is None:
        duration = getattr(quality, "duration_seconds", None)
    file_size = _first_number(observed, "file_size_bytes")
    if file_size is None:
        file_size = getattr(quality, "file_size_bytes", None)
    bitrate = _first_number(observed, "video_bitrate_kbps")
    if bitrate is None:
        bitrate = getattr(quality, "video_bitrate_kbps", None)
    sample_rate = observed.get("audio_sample_rate", getattr(quality, "audio_sample_rate", None))
    audio_present = observed.get("audio_present", getattr(quality, "audio_present", None))
    codec = str(observed.get("video_codec", getattr(quality, "video_codec", "")) or "").lower()
    containers = _format_tokens(observed.get("container", observed.get("format_name", "")))
    errors: list[str] = []

    if resolved.allowed_resolutions and resolution is None:
        errors.append("缺少输出分辨率测量")
    elif resolved.allowed_resolutions and resolution not in resolved.allowed_resolutions:
        errors.append(f"分辨率 {resolution} 不在平台允许列表")
    if resolved.allowed_aspect_ratios:
        observed_aspect = _aspect_ratio(resolution)
        if observed_aspect is None:
            errors.append("缺少画幅比例测量")
        elif observed_aspect not in resolved.allowed_aspect_ratios:
            errors.append(f"画幅 {observed_aspect} 不符合平台规格")
    if resolved.allowed_fps:
        if fps in (None, ""):
            errors.append("缺少输出帧率测量")
        elif not _fps_matches(fps, resolved.allowed_fps):
            errors.append(f"帧率 {fps} 不在平台允许列表")
    if resolved.allowed_video_codecs:
        if not codec:
            errors.append("缺少视频编码格式测量")
        elif codec not in {item.lower() for item in resolved.allowed_video_codecs}:
            errors.append(f"视频编码 {codec} 不符合平台规格")
    if resolved.allowed_containers:
        if not containers:
            errors.append("缺少封装格式测量")
        elif not containers.intersection({item.lower() for item in resolved.allowed_containers}):
            errors.append("封装格式不符合平台规格")
    if resolved.min_duration_seconds is not None:
        if duration is None:
            errors.append("缺少输出时长测量")
        elif duration < resolved.min_duration_seconds:
            errors.append("输出时长低于平台最小时长")
    if resolved.max_duration_seconds is not None:
        if duration is None:
            errors.append("缺少输出时长测量")
        elif duration > resolved.max_duration_seconds:
            errors.append("输出时长超过平台最大时长")
    if resolved.max_file_size_bytes is not None:
        if file_size is None:
            errors.append("缺少输出文件大小测量")
        elif file_size > resolved.max_file_size_bytes:
            errors.append("输出文件超过平台大小限制")
    if resolved.max_video_bitrate_kbps is not None:
        if bitrate is None:
            errors.append("缺少视频码率测量")
        elif bitrate > resolved.max_video_bitrate_kbps:
            errors.append("视频码率超过平台限制")
    if resolved.allowed_audio_sample_rates:
        if sample_rate is None:
            errors.append("缺少音频采样率测量")
        elif int(sample_rate) not in resolved.allowed_audio_sample_rates:
            errors.append("音频采样率不符合平台规格")
    if resolved.require_audio is True and audio_present is not True:
        errors.append("平台要求音频轨")
    if resolved.require_audio is False and audio_present is True:
        errors.append("平台不允许音频轨")

    return PlatformValidationResult(spec_id, not errors, tuple(errors))


@dataclass(frozen=True)
class SubtitleValidationResult:
    cue_count: int
    out_of_range: int = 0
    overlaps: int = 0
    empty_text: int = 0
    max_words_per_second: float | None = None
    over_speed: int = 0
    long_lines: int = 0
    safe_area_violations: int = 0

    @property
    def passed(self) -> bool:
        return not any((
            self.out_of_range,
            self.overlaps,
            self.empty_text,
            self.over_speed,
            self.long_lines,
            self.safe_area_violations,
        ))

    def to_dict(self) -> dict[str, Any]:
        return {
            "cue_count": self.cue_count,
            "passed": self.passed,
            "out_of_range": self.out_of_range,
            "overlaps": self.overlaps,
            "empty_text": self.empty_text,
            "max_words_per_second": self.max_words_per_second,
            "over_speed": self.over_speed,
            "long_lines": self.long_lines,
            "safe_area_violations": self.safe_area_violations,
        }


@dataclass(frozen=True)
class BrandElementRequirement:
    """A required, measured brand element in the final deliverable."""

    element_id: str
    kind: str = ""
    min_duration_frames: int | None = None
    required: bool = True
    safe_area_required: bool = True

    def __post_init__(self) -> None:
        if not self.element_id.strip():
            raise ValueError("brand element_id cannot be empty")
        if self.min_duration_frames is not None and self.min_duration_frames <= 0:
            raise ValueError("brand min_duration_frames must be positive")

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "BrandElementRequirement":
        minimum = data.get("min_duration_frames")
        return cls(
            element_id=str(data.get("element_id", data.get("id", ""))),
            kind=str(data.get("kind", "") or ""),
            min_duration_frames=(int(minimum) if minimum not in (None, "") else None),
            required=bool(data.get("required", True)),
            safe_area_required=bool(data.get("safe_area_required", True)),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "element_id": self.element_id,
            "kind": self.kind,
            "min_duration_frames": self.min_duration_frames,
            "required": self.required,
            "safe_area_required": self.safe_area_required,
        }


@dataclass(frozen=True)
class BrandElementValidationResult:
    """Measured brand-element contract result for a final export."""

    requirement_count: int
    measured_count: int
    missing: tuple[str, ...] = ()
    out_of_range: int = 0
    safe_area_violations: int = 0
    duration_violations: int = 0
    invalid_measurements: int = 0

    @property
    def passed(self) -> bool:
        return not any((
            self.missing,
            self.out_of_range,
            self.safe_area_violations,
            self.duration_violations,
            self.invalid_measurements,
        ))

    def to_dict(self) -> dict[str, Any]:
        return {
            "requirement_count": self.requirement_count,
            "measured_count": self.measured_count,
            "passed": self.passed,
            "missing": list(self.missing),
            "out_of_range": self.out_of_range,
            "safe_area_violations": self.safe_area_violations,
            "duration_violations": self.duration_violations,
            "invalid_measurements": self.invalid_measurements,
        }


def _cue_value(cue: Any, key: str, default: Any = None) -> Any:
    if isinstance(cue, Mapping):
        return cue.get(key, default)
    return getattr(cue, key, default)


def _cue_range(cue: Any) -> tuple[int | None, int | None]:
    start = _cue_value(cue, "start_frame")
    if start is None:
        start = _cue_value(cue, "timeline_start")
    end = _cue_value(cue, "end_frame")
    if end is None:
        duration = _cue_value(cue, "duration_frames")
        if duration is None:
            duration = _cue_value(cue, "timeline_duration")
        if start is not None and duration is not None:
            end = int(start) + int(duration)
    try:
        return int(start), int(end)
    except (TypeError, ValueError):
        return None, None


def _reading_units(text: str) -> int:
    # CJK characters are readable units; Latin/number runs are words. This
    # avoids making Chinese captions appear artificially slow because they have
    # no whitespace-delimited words.
    cjk = len(re.findall(r"[\u3400-\u9fff]", text))
    non_cjk = re.sub(r"[\u3400-\u9fff]", " ", text)
    tokens = re.findall(r"[^\W_]+", non_cjk, flags=re.UNICODE)
    return cjk + len(tokens)


def _safe_area_violation(cue: Any) -> bool:
    explicit = _cue_value(cue, "safe_area_violation")
    if explicit is not None:
        return bool(explicit)
    bbox = _cue_value(cue, "bbox")
    safe = _cue_value(cue, "safe_area")
    if bbox is None or safe is None:
        return False
    try:
        if len(bbox) != 4 or len(safe) != 4:
            return True
        x, y, width, height = (float(item) for item in bbox)
        left, top, right, bottom = (float(item) for item in safe)
    except (TypeError, ValueError):
        return True
    if not all(
        math.isfinite(value)
        for value in (x, y, width, height, left, top, right, bottom)
    ):
        return True
    if width < 0.0 or height < 0.0 or right < left or bottom < top:
        return True
    return x < left or y < top or x + width > right or y + height > bottom


def _brand_requirement(value: Any) -> BrandElementRequirement:
    if isinstance(value, BrandElementRequirement):
        return value
    if isinstance(value, Mapping):
        return BrandElementRequirement.from_dict(value)
    raise TypeError("brand requirements must be mappings or BrandElementRequirement")


def validate_brand_elements(
    elements: Sequence[Any],
    requirements: Sequence[Any],
    *,
    timeline_duration_frames: int | None,
) -> BrandElementValidationResult:
    """Validate measured Logo/片尾/免责声明 elements before delivery.

    ``elements`` are provider-neutral measured records. Each matched record
    must carry ``element_id``, a valid frame range, and—unless explicitly
    disabled by the requirement—a rendered ``bbox`` plus ``safe_area``.
    Missing measurements are failures, never inferred from a plan label.
    """
    normalized_requirements = tuple(_brand_requirement(item) for item in requirements)
    measured = tuple(elements or ())
    by_id: dict[str, list[Any]] = {}
    for element in measured:
        element_id = str(_cue_value(element, "element_id", _cue_value(element, "id", "")) or "")
        if element_id:
            by_id.setdefault(element_id, []).append(element)

    missing: list[str] = []
    out_of_range = safe_violations = duration_violations = invalid_measurements = 0
    for requirement in normalized_requirements:
        candidates = by_id.get(requirement.element_id, ())
        candidate = next(
            (
                item for item in candidates
                if _cue_value(item, "present", True) is not False
                and (
                    not requirement.kind
                    or str(_cue_value(item, "kind", "") or "") == requirement.kind
                )
            ),
            None,
        )
        if candidate is None:
            if requirement.required:
                missing.append(requirement.element_id)
            continue

        if _cue_value(candidate, "measurement_error"):
            invalid_measurements += 1
            continue
        start, end = _cue_range(candidate)
        if start is None or end is None or end <= start:
            invalid_measurements += 1
            continue
        if start < 0 or (
            timeline_duration_frames is not None and end > timeline_duration_frames
        ):
            out_of_range += 1
        if (
            requirement.min_duration_frames is not None
            and end - start < requirement.min_duration_frames
        ):
            duration_violations += 1
        if requirement.safe_area_required:
            bbox = _cue_value(candidate, "bbox")
            safe_area = _cue_value(candidate, "safe_area")
            if bbox is None or safe_area is None:
                invalid_measurements += 1
            elif _safe_area_violation(candidate):
                safe_violations += 1

    return BrandElementValidationResult(
        requirement_count=len(normalized_requirements),
        measured_count=len(measured),
        missing=tuple(sorted(set(missing))),
        out_of_range=out_of_range,
        safe_area_violations=safe_violations,
        duration_violations=duration_violations,
        invalid_measurements=invalid_measurements,
    )


def materialize_brand_elements_from_timeline(
    sequence: Any,
    assets: Mapping[str, Any] | None,
    *,
    expected_resolution: tuple[int, int] | None,
    safe_area: Sequence[float] | None = None,
) -> tuple[dict[str, Any], ...]:
    """Materialize declared asset annotations into timeline-frame measurements.

    An asset may persist explicit ``metadata.brand_elements`` annotations. The
    annotation contract is intentionally deterministic and provider-neutral:

    .. code-block:: json

        {"element_id": "logo-main", "kind": "logo",
         "source_start_frame": 0, "source_end_frame": 120,
         "bbox": [0.80, 0.04, 0.14, 0.08],
         "bbox_space": "canvas_normalized"}

    This function maps source ranges through each enabled timeline clip. It
    does not infer a logo from pixels or filenames; an unannotated asset
    produces no record and therefore remains subject to the required-element
    missing gate.
    """
    if expected_resolution is None:
        raise ValueError("brand materialization requires an output resolution")
    try:
        canvas_w, canvas_h = int(expected_resolution[0]), int(expected_resolution[1])
    except (IndexError, TypeError, ValueError) as exc:
        raise ValueError("brand materialization resolution is invalid") from exc
    if canvas_w <= 0 or canvas_h <= 0:
        raise ValueError("brand materialization resolution must be positive")
    normalized_assets = assets or {}
    output: list[dict[str, Any]] = []

    def _asset(asset_id: str) -> Any:
        if isinstance(normalized_assets, Mapping):
            return normalized_assets.get(asset_id)
        return None

    def _metadata(asset: Any) -> Mapping[str, Any] | None:
        value = _cue_value(asset, "metadata", {})
        return value if isinstance(value, Mapping) else None

    def _annotations(asset: Any) -> tuple[Any, ...]:
        metadata = _metadata(asset)
        if metadata is None:
            return ()
        value = metadata.get("brand_elements")
        if value is None and isinstance(metadata.get("visual_annotations"), Mapping):
            value = metadata["visual_annotations"].get("brand_elements")
        if isinstance(value, Mapping):
            return (value,)
        return tuple(value or ())

    def _bbox(annotation: Mapping[str, Any]) -> tuple[list[float] | None, str | None]:
        raw = annotation.get("bbox")
        if raw is None:
            raw = annotation.get("bbox_normalized")
        try:
            values = [float(item) for item in raw]
        except (TypeError, ValueError):
            return None, "brand annotation bbox must contain four numbers"
        if len(values) != 4 or not all(math.isfinite(item) for item in values):
            return None, "brand annotation bbox is not finite or has invalid shape"
        space = str(annotation.get("bbox_space", "canvas_pixels") or "canvas_pixels").strip().lower()
        if raw is annotation.get("bbox_normalized") or space in {
            "canvas_normalized", "normalized_canvas", "normalized",
        }:
            values = [
                values[0] * canvas_w,
                values[1] * canvas_h,
                values[2] * canvas_w,
                values[3] * canvas_h,
            ]
        elif space not in {"canvas_pixels", "pixels", "canvas"}:
            return None, f"unsupported brand annotation bbox_space: {space}"
        if values[2] < 0.0 or values[3] < 0.0:
            return None, "brand annotation bbox dimensions must be non-negative"
        return values, None

    def _source_range(annotation: Mapping[str, Any], clip: Any) -> tuple[int, int] | None:
        clip_start = int(getattr(clip, "source_start", 0))
        clip_end = clip_start + int(getattr(clip, "source_duration", 0))
        start_value = annotation.get("source_start_frame", clip_start)
        end_value = annotation.get("source_end_frame")
        if end_value in (None, ""):
            duration_value = annotation.get("source_duration_frames")
            end_value = (
                int(start_value) + int(duration_value)
                if duration_value not in (None, "")
                else clip_end
            )
        try:
            start, end = int(start_value), int(end_value)
        except (TypeError, ValueError):
            return None
        overlap_start, overlap_end = max(start, clip_start), min(end, clip_end)
        if overlap_end <= overlap_start:
            return None
        return overlap_start, overlap_end

    def _timeline_range(clip: Any, source_range: tuple[int, int]) -> tuple[int, int] | None:
        clip_source_start = int(getattr(clip, "source_start", 0))
        clip_source_duration = int(getattr(clip, "source_duration", 0))
        clip_timeline_start = int(getattr(clip, "timeline_start", 0))
        clip_timeline_duration = int(getattr(clip, "timeline_duration", 0))
        if clip_source_duration <= 0 or clip_timeline_duration <= 0:
            return None
        source_offset_start = source_range[0] - clip_source_start
        source_offset_end = source_range[1] - clip_source_start
        reverse = bool(getattr(getattr(clip, "speed", None), "reverse", False))
        if reverse:
            timeline_offset_start = math.ceil(
                (clip_source_duration - source_offset_end)
                * clip_timeline_duration / clip_source_duration
            )
            timeline_offset_end = math.ceil(
                (clip_source_duration - source_offset_start)
                * clip_timeline_duration / clip_source_duration
            )
        else:
            timeline_offset_start = math.floor(
                source_offset_start * clip_timeline_duration / clip_source_duration
            )
            timeline_offset_end = math.ceil(
                source_offset_end * clip_timeline_duration / clip_source_duration
            )
        start = clip_timeline_start + timeline_offset_start
        end = clip_timeline_start + timeline_offset_end
        return (start, max(start + 1, end)) if end > start else None

    for track in tuple(getattr(sequence, "video_tracks", ()) or ()):
        if not getattr(track, "is_enabled", True) or not getattr(track, "is_visible", True):
            continue
        for clip in tuple(getattr(track, "clips", ()) or ()):
            if not getattr(clip, "is_enabled", True):
                continue
            asset_id = str(getattr(clip, "asset_id", "") or "")
            asset = _asset(asset_id)
            if asset is None:
                continue
            for raw_annotation in _annotations(asset):
                if not isinstance(raw_annotation, Mapping):
                    continue
                source_range = _source_range(raw_annotation, clip)
                if source_range is None:
                    continue
                timeline_range = _timeline_range(clip, source_range)
                if timeline_range is None:
                    continue
                bbox, measurement_error = _bbox(raw_annotation)
                record: dict[str, Any] = {
                    "element_id": str(raw_annotation.get("element_id", raw_annotation.get("id", ""))),
                    "kind": str(raw_annotation.get("kind", "") or ""),
                    "start_frame": timeline_range[0],
                    "end_frame": timeline_range[1],
                    "asset_id": asset_id,
                    "clip_id": str(getattr(clip, "clip_id", "") or ""),
                    "track_id": str(getattr(track, "track_id", "") or ""),
                    "measurement_source": "asset_annotation",
                }
                if bbox is not None:
                    record["bbox"] = bbox
                if safe_area is not None:
                    record["safe_area"] = [float(value) for value in safe_area]
                if measurement_error:
                    record["measurement_error"] = measurement_error
                output.append(record)
    return tuple(output)


def validate_subtitle_cues(
    cues: Sequence[Any],
    *,
    timeline_duration_frames: int | None,
    fps: float,
    max_words_per_second: float = 20.0,
    max_chars_per_line: int | None = None,
) -> SubtitleValidationResult:
    """Validate timeline subtitle cues without depending on a host model."""
    if fps <= 0 or not math.isfinite(fps):
        raise ValueError("fps must be finite and positive")
    if max_words_per_second <= 0 or not math.isfinite(max_words_per_second):
        raise ValueError("max_words_per_second must be finite and positive")
    if max_chars_per_line is not None and max_chars_per_line <= 0:
        raise ValueError("max_chars_per_line must be positive")

    out_of_range = overlaps = empty_text = over_speed = long_lines = safe_violations = 0
    measured_ranges: list[tuple[int, int]] = []
    max_wps: float | None = None
    for cue in cues:
        start, end = _cue_range(cue)
        if start is None or end is None or end <= start:
            out_of_range += 1
            continue
        if start < 0 or (
            timeline_duration_frames is not None and end > timeline_duration_frames
        ):
            out_of_range += 1
        else:
            measured_ranges.append((start, end))

        text = str(_cue_value(cue, "text", "") or "").strip()
        if not text:
            empty_text += 1
            continue
        seconds = (end - start) / fps
        if seconds > 0:
            words_per_second = _reading_units(text) / seconds
            if max_wps is None or words_per_second > max_wps:
                max_wps = words_per_second
            if words_per_second > max_words_per_second:
                over_speed += 1
        if max_chars_per_line is not None and any(
            len(line.replace(" ", "")) > max_chars_per_line
            for line in text.splitlines()
        ):
            long_lines += 1
        if _safe_area_violation(cue):
            safe_violations += 1

    measured_ranges.sort()
    active_end: int | None = None
    for start, end in measured_ranges:
        if active_end is not None and start < active_end:
            overlaps += 1
        active_end = max(active_end or end, end)
    return SubtitleValidationResult(
        cue_count=len(cues),
        out_of_range=out_of_range,
        overlaps=overlaps,
        empty_text=empty_text,
        max_words_per_second=max_wps,
        over_speed=over_speed,
        long_lines=long_lines,
        safe_area_violations=safe_violations,
    )


def _profile(
    spec_id: str,
    resolution: tuple[int, int],
    aspect: str,
) -> PlatformDeliverySpec:
    return PlatformDeliverySpec(
        spec_id=spec_id,
        allowed_resolutions=(resolution,),
        allowed_aspect_ratios=(aspect,),
        allowed_fps=("24/1", "25/1", "30/1", "50/1", "60/1", "24000/1001", "30000/1001", "60000/1001"),
        allowed_video_codecs=("h264", "hevc"),
        allowed_containers=("mp4", "mov"),
        max_duration_seconds=600.0,
        max_file_size_bytes=4 * 1024 * 1024 * 1024,
        max_video_bitrate_kbps=25_000,
        allowed_audio_sample_rates=(44_100, 48_000),
        require_audio=True,
    )


DEFAULT_PLATFORM_SPECS: dict[str, PlatformDeliverySpec] = {
    "vertical_social": _profile("vertical_social", (1080, 1920), "9:16"),
    "horizontal_social": _profile("horizontal_social", (1920, 1080), "16:9"),
    "square_social": _profile("square_social", (1080, 1080), "1:1"),
}


SUBTITLE_SAFE_AREA_INSETS: dict[str, float] = {
    # Default title-safe rectangle: 90% of the frame centered in the canvas.
    "title_safe": 0.05,
    # Action-safe leaves extra room for platform controls and overlays.
    "action_safe": 0.10,
    # Editorial brief/platform aliases.
    "vertical_social": 0.10,
    "horizontal_social": 0.05,
    "square_social": 0.05,
}


def build_subtitle_safe_area(
    canvas_w: int,
    canvas_h: int,
    *,
    inset: float = 0.05,
) -> tuple[float, float, float, float]:
    """Return a safe rectangle as ``(left, top, right, bottom)`` pixels."""
    if canvas_w <= 0 or canvas_h <= 0:
        raise ValueError("canvas dimensions must be positive")
    inset = float(inset)
    if not math.isfinite(inset) or not 0.0 <= inset < 0.5:
        raise ValueError("safe-area inset must be finite and in [0, 0.5)")
    inset_x = float(canvas_w) * inset
    inset_y = float(canvas_h) * inset
    return inset_x, inset_y, float(canvas_w) - inset_x, float(canvas_h) - inset_y


__all__ = [
    "DEFAULT_PLATFORM_SPECS",
    "BrandElementRequirement",
    "BrandElementValidationResult",
    "PlatformDeliverySpec",
    "PlatformValidationResult",
    "SUBTITLE_SAFE_AREA_INSETS",
    "SubtitleValidationResult",
    "build_subtitle_safe_area",
    "materialize_brand_elements_from_timeline",
    "resolve_platform_spec",
    "validate_brand_elements",
    "validate_platform_delivery",
    "validate_subtitle_cues",
]
