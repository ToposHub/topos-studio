"""Measured pre/post-export quality gates for deliverables (P8/P10).

The exporter remains responsible for encoding.  This module only evaluates
validated measurements supplied by the encoder/probe, so a successful process
exit cannot be mistaken for a deliverable that is safe to ship.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from src.features.edit.ai_editing.diagnostics import (
    Diagnostic,
    DiagnosticSeverity,
    TaepDiagnosticCode,
    make_diagnostic,
)


SILENCE_POLICY_MEASURE_ONLY = "measure_only"
SILENCE_POLICY_HARD_MAX = "hard_max"
_SILENCE_POLICIES = frozenset({SILENCE_POLICY_MEASURE_ONLY, SILENCE_POLICY_HARD_MAX})


@dataclass(frozen=True)
class DeliveryQualityInput:
    """Probe results for a timeline/export; fields are counts, flags and rates."""

    project_references_valid: bool | None = None
    timeline_valid: bool | None = None
    unresolved_provider_handles: int = 0
    resolution: tuple[int, int] | None = None
    expected_resolution: tuple[int, int] | None = None
    fps: str | None = None
    expected_fps: str | None = None
    duration_seconds: float | None = None
    expected_duration_seconds: float | None = None
    duration_tolerance_seconds: float = 0.5
    black_frame_count: int = 0
    frozen_frame_count: int = 0
    corrupt_frame_count: int = 0
    audio_present: bool | None = None
    audio_sample_rate: int | None = None
    expected_audio_sample_rate: int | None = None
    video_codec: str | None = None
    container: str | None = None
    file_size_bytes: int | None = None
    video_bitrate_kbps: float | None = None
    peak_db: float | None = None
    max_peak_db: float = 0.0
    integrated_lufs: float | None = None
    loudness_range_lu: float | None = None
    loudness_backend_available: bool | None = None
    expected_lufs: float | None = None
    lufs_tolerance: float = 1.0
    silence_policy: str = SILENCE_POLICY_MEASURE_ONLY
    silence_seconds: float | None = None
    max_silence_seconds: float | None = None
    subtitle_out_of_range: int = 0
    subtitle_overlaps: int = 0
    subtitle_empty_count: int = 0
    subtitle_long_line_count: int = 0
    subtitle_safe_area_violations: int = 0
    subtitle_reading_words_per_second: float | None = None
    max_subtitle_words_per_second: float = 20.0
    missing_brand_elements: tuple[str, ...] = ()
    brand_element_out_of_range: int = 0
    brand_element_safe_area_violations: int = 0
    brand_element_duration_violations: int = 0
    brand_element_invalid_measurements: int = 0
    unknown_license_count: int = 0
    platform_errors: tuple[str, ...] = ()
    output_exists: bool | None = None
    output_reopenable: bool | None = None
    output_hash: str | None = None
    manifest_present: bool | None = None
    thumbnail_present: bool | None = None


@dataclass(frozen=True)
class DeliveryQualityResult:
    passed: bool
    blocking: tuple[str, ...] = ()
    diagnostics: tuple[Diagnostic, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "blocking": list(self.blocking),
            "diagnostics": [item.to_dict() for item in self.diagnostics],
        }


def evaluate_delivery_quality(
    inp: DeliveryQualityInput,
) -> DeliveryQualityResult:
    """Fail closed on supplied measurements that violate delivery contracts."""
    blocking: list[str] = []

    def require(condition: bool, message: str) -> None:
        if not condition:
            blocking.append(message)

    if inp.project_references_valid is not None:
        require(inp.project_references_valid, "工程素材引用无效")
    if inp.timeline_valid is not None:
        require(inp.timeline_valid, "时间线结构无效")
    require(inp.unresolved_provider_handles == 0, "存在未解析的 provider 句柄")
    if inp.expected_resolution is not None:
        require(inp.resolution == inp.expected_resolution, "输出分辨率不符合目标规格")
    if inp.expected_fps is not None:
        require(inp.fps == inp.expected_fps, "输出帧率不符合目标规格")
    if inp.expected_duration_seconds is not None and inp.duration_seconds is not None:
        require(
            abs(inp.duration_seconds - inp.expected_duration_seconds) <= inp.duration_tolerance_seconds,
            "输出时长超出允许误差",
        )
    require(inp.black_frame_count == 0, "检测到黑帧")
    require(inp.frozen_frame_count == 0, "检测到冻结帧")
    require(inp.corrupt_frame_count == 0, "检测到损坏帧")
    if inp.audio_present is not None:
        require(inp.audio_present, "音频轨缺失")
    if inp.expected_audio_sample_rate is not None:
        require(inp.audio_sample_rate == inp.expected_audio_sample_rate, "音频采样率不符合目标规格")
    if inp.peak_db is not None:
        require(inp.peak_db <= inp.max_peak_db, "音频存在削波风险")
    if inp.expected_lufs is not None:
        require(inp.loudness_backend_available is True, "LUFS 分析后端不可用")
        require(inp.integrated_lufs is not None, "缺少 integrated LUFS 测量")
        if inp.integrated_lufs is not None:
            require(
                abs(inp.integrated_lufs - inp.expected_lufs) <= inp.lufs_tolerance,
                "integrated LUFS 不符合目标规格",
            )
    if inp.silence_policy not in _SILENCE_POLICIES:
        require(False, "静音策略无效")
    elif inp.silence_policy == SILENCE_POLICY_HARD_MAX or inp.max_silence_seconds is not None:
        require(inp.max_silence_seconds is not None, "静音硬门缺少最大静音时长")
        require(inp.silence_seconds is not None, "静音硬门缺少测量结果")
        if inp.max_silence_seconds is not None and inp.silence_seconds is not None:
            require(inp.silence_seconds <= inp.max_silence_seconds, "静音段超过允许时长")
    require(inp.subtitle_out_of_range == 0, "字幕超出时间线范围")
    require(inp.subtitle_overlaps == 0, "字幕存在重叠")
    require(inp.subtitle_empty_count == 0, "字幕存在空文本")
    require(inp.subtitle_long_line_count == 0, "字幕单行长度超过门槛")
    require(inp.subtitle_safe_area_violations == 0, "字幕超出安全区")
    if inp.subtitle_reading_words_per_second is not None:
        require(
            inp.subtitle_reading_words_per_second <= inp.max_subtitle_words_per_second,
            "字幕阅读速度超过门槛",
        )
    if inp.missing_brand_elements:
        blocking.append("缺少品牌必需元素")
    require(inp.brand_element_out_of_range == 0, "品牌元素超出时间线范围")
    require(inp.brand_element_safe_area_violations == 0, "品牌元素超出安全区")
    require(inp.brand_element_duration_violations == 0, "品牌元素展示时长不足")
    require(inp.brand_element_invalid_measurements == 0, "品牌元素缺少有效测量")
    require(inp.unknown_license_count == 0, "存在许可未知的素材")
    if inp.platform_errors:
        blocking.extend(f"平台规格：{item}" for item in inp.platform_errors)
    if inp.output_exists is not None:
        require(inp.output_exists, "导出文件不存在")
    if inp.output_reopenable is not None:
        require(inp.output_reopenable, "导出文件无法重新打开")
    if inp.output_hash is not None:
        require(bool(inp.output_hash.strip()), "导出文件缺少 hash")
    if inp.manifest_present is not None:
        require(inp.manifest_present, "导出 manifest 缺失")
    if inp.thumbnail_present is not None:
        require(inp.thumbnail_present, "导出缩略图缺失")

    diagnostics = tuple(
        make_diagnostic(
            DiagnosticSeverity.ERROR,
            TaepDiagnosticCode.QUALITY_GATE_FAILED,
            detail=message,
        )
        for message in blocking
    )
    return DeliveryQualityResult(not blocking, tuple(blocking), diagnostics)


__all__ = [
    "DeliveryQualityInput",
    "DeliveryQualityResult",
    "SILENCE_POLICY_HARD_MAX",
    "SILENCE_POLICY_MEASURE_ONLY",
    "evaluate_delivery_quality",
]
