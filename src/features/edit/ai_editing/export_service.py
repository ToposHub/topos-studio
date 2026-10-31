"""TAEP Export Service（Phase 4）。

把 TAEP 的 ``request_export`` operation 映射到现有 ``TimelineExportConfig``，
调用已有的 audio/video/font preflight，并以 job 形式提交（TAEP §12 P4）。

设计（TAEP §4 / §5.7）：

- ``request_export`` 只创建 ``TimelineExportConfig`` 提议，先走 preflight 和权限
  确认；不立即覆盖文件。
- 预览使用短范围、代理/低质量 preset；最终 deliver 使用明确 preset，且只在确认后
  写文件。
- 导出前展示：范围、分辨率、fps、codec、音频、字幕、素材离线、effect degradation、
  预计文件大小和覆盖风险。
- 取消、编码失败、effect preflight 失败、离线素材、字体缺失均不会产出“看似成功”
  的文件（TAEP §12 P4 验收）。
- 产物记录到 audit/export manifest，含 project/timeline revision 与 asset 摘要。
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Sequence, TYPE_CHECKING

from src.features.edit.ai_editing.diagnostics import (
    DiagnosticSeverity,
    TaepDiagnosticCode,
    make_diagnostic,
)
from src.features.edit.ai_editing.commercial_gate import check_generated_media_for_export
from src.features.edit.ai_editing.delivery_quality_gate import (
    DeliveryQualityInput,
    DeliveryQualityResult,
    evaluate_delivery_quality,
)
from src.features.edit.ai_editing.delivery_contracts import (
    BrandElementValidationResult,
    PlatformDeliverySpec,
    PlatformValidationResult,
    SubtitleValidationResult,
    validate_brand_elements,
    validate_platform_delivery,
    validate_subtitle_cues,
)
from src.features.edit.ai_editing.edit_project_service import ProjectSequences
from src.features.edit.ai_editing.editorial_models import FrameRange
from src.features.edit.ai_editing.jobs import JobHandle, JobKind, JobRegistry

if TYPE_CHECKING:
    from src.features.edit.ai_editing.diagnostics import Diagnostic
    from src.features.edit.core.timeline_export import TimelineExportConfig
    from src.features.edit.core.timeline_v2 import TimelineSequence


# ---------------------------------------------------------------------- #
# 预览 preset（短范围、低质量；TAEP §11.6 步骤 12）
# ---------------------------------------------------------------------- #


@dataclass(frozen=True)
class PreviewPreset:
    """预览 preset：短范围、低质量，用于快速评审。"""

    name: str
    max_frames: int = 300  # 与 capability manifest limits.max_preview_frames 对齐
    quality: str = "fast"  # 传给 exporter 的质量标识
    scale: float = 0.5  # 代理缩放


DEFAULT_PREVIEW_PRESETS: dict[str, PreviewPreset] = {
    "quick_10s": PreviewPreset("quick_10s", max_frames=240, quality="fast", scale=0.5),
    "quick_30s": PreviewPreset("quick_30s", max_frames=720, quality="fast", scale=0.5),
}


# ---------------------------------------------------------------------- #
# ExportRequest（来自 TAEP request_export operation 的结构化请求）
# ---------------------------------------------------------------------- #


@dataclass(frozen=True)
class ExportRequest:
    """结构化的导出请求（由 ``request_export`` operation 编译而来）。"""

    timeline_id: str
    codec: str = "h264"
    resolution: tuple[int, int] | None = None  # None=原始
    fps: str | None = None  # None=原始；"24/1" 等
    range: FrameRange | None = None  # None=整条
    include_audio: bool = True
    burn_subtitles: bool = True
    output_path: str | None = None  # 必须有 path grant 才能写
    preset_name: str | None = None  # 引用 deliver preset
    commercial_ready_required: bool = False  # 商业导出声明的硬许可门禁
    required_license_region: str | None = None  # 商业导出的目标许可区域（可选）
    # T-P1-70 步骤 4：发起导出的 AI 计划 ID（None=非 AI 发起），随
    # TimelineExportConfig 进交付 manifest。
    plan_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "timeline_id": self.timeline_id,
            "codec": self.codec,
            "include_audio": self.include_audio,
            "burn_subtitles": self.burn_subtitles,
        }
        if self.resolution is not None:
            d["resolution"] = list(self.resolution)
        if self.fps is not None:
            d["fps"] = self.fps
        if self.range is not None:
            d["range"] = self.range.to_dict()
        if self.output_path is not None:
            d["output_path"] = self.output_path
        if self.preset_name is not None:
            d["preset_name"] = self.preset_name
        d["commercial_ready_required"] = self.commercial_ready_required
        if self.required_license_region is not None:
            d["required_license_region"] = self.required_license_region
        if self.plan_id is not None:
            d["plan_id"] = self.plan_id
        return d


# ---------------------------------------------------------------------- #
# PreflightReport
# ---------------------------------------------------------------------- #


@dataclass(frozen=True)
class PreflightReport:
    """导出预检汇总（audio + video-effects + fonts）。"""

    ok: bool
    blocking: tuple[str, ...] = ()
    degraded: tuple[str, ...] = ()
    delivery_quality: DeliveryQualityResult | None = None
    # TAEP 诊断码映射
    diagnostics: tuple = field(default_factory=tuple)

    @property
    def has_blocking(self) -> bool:
        return bool(self.blocking)


@dataclass(frozen=True)
class ExportArtifactResult:
    """Result of materializing the post-export manifest and thumbnail."""

    succeeded: bool
    manifest_path: str = ""
    thumbnail_path: str = ""
    error_code: str = ""
    error_message: str = ""


def materialize_export_artifacts(
    output_path: str | os.PathLike[str],
    *,
    metadata: dict[str, Any] | None = None,
    ffmpeg_bin: str = "ffmpeg",
    timeout_seconds: float = 30.0,
    runner: Callable[..., Any] | None = None,
) -> ExportArtifactResult:
    """Create a redacted manifest and a real first-frame thumbnail.

    This helper intentionally derives both files beside the encoded output and
    never stores the output path in the manifest.  It is called from the video
    export worker, so thumbnail extraction and JSON I/O do not block Qt.
    """
    output = Path(output_path)
    if not output.is_file():
        return ExportArtifactResult(
            False, error_code="OUTPUT_NOT_FOUND", error_message="encoded output is missing",
        )
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")

    stem = output.with_suffix("")
    manifest = stem.with_name(f"{stem.name}.manifest.json")
    thumbnail = stem.with_name(f"{stem.name}.thumbnail.jpg")
    run = runner or subprocess.run
    fd = -1
    temporary = ""
    # 默认 "ffmpeg" 时经 ffmpeg_locator 解析（打包 bin/ → PATH）；显式路径原样使用
    from src.shared.ffmpeg_locator import find_ffmpeg
    if not os.path.dirname(str(ffmpeg_bin)):
        ffmpeg_bin = find_ffmpeg() or ffmpeg_bin
    try:
        completed = run(
            [
                ffmpeg_bin,
                "-hide_banner",
                "-loglevel", "error",
                "-y",
                "-i", str(output),
                "-frames:v", "1",
                "-q:v", "3",
                str(thumbnail),
            ],
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return ExportArtifactResult(
            False,
            error_code="THUMBNAIL_GENERATOR_UNAVAILABLE",
            error_message="ffmpeg thumbnail generation is unavailable",
        )
    if int(getattr(completed, "returncode", 1)) != 0 or not _is_nonempty_file(thumbnail):
        return ExportArtifactResult(
            False,
            error_code="THUMBNAIL_GENERATION_FAILED",
            error_message="ffmpeg did not produce a thumbnail",
        )

    payload = {
        "schema_version": "topos-export-manifest/1",
        "artifact_kind": "video",
        "metadata": _redact_manifest_value(metadata or {}),
    }
    try:
        fd, temporary = tempfile.mkstemp(
            prefix=f".{manifest.name}.", suffix=".tmp", dir=str(manifest.parent),
        )
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            fd = -1
            json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, manifest)
    except (OSError, TypeError, ValueError):
        try:
            if fd >= 0:
                os.close(fd)
        except OSError:
            pass
        try:
            os.remove(temporary)
        except OSError:
            pass
        return ExportArtifactResult(
            False,
            error_code="MANIFEST_WRITE_FAILED",
            error_message="export manifest could not be written",
        )
    return ExportArtifactResult(
        True,
        manifest_path=str(manifest),
        thumbnail_path=str(thumbnail),
    )


def run_export_preflight(
    config: TimelineExportConfig, sequence: TimelineSequence, *, generation_bin: Any | None = None,
    commercial_ready_required: bool = False,
    required_license_region: str | None = None,
    delivery_quality: DeliveryQualityInput | None = None,
) -> PreflightReport:
    """运行三套 preflight（audio/video-effects/fonts），汇总为 PreflightReport。

    任一 blocking 即 ``ok=False``，对应 ``TAEP_EXPORT_PREFLIGHT_FAILED``。
    """
    from src.features.edit.core.timeline_export import (
        preflight_audio_export,
        preflight_fonts_export,
        preflight_video_effects_export,
    )

    blocking: list[str] = []
    degraded: list[str] = []
    diags: list = []

    for name, fn in (
        ("audio", preflight_audio_export),
        ("video_effects", preflight_video_effects_export),
        ("fonts", preflight_fonts_export),
    ):
        try:
            result = fn(config, sequence)
        except Exception as exc:
            blocking.append(f"{name} preflight raised: {exc}")
            continue
        if result.blocking:
            blocking.extend(f"{name}: {m}" for m in result.blocking)
        if result.degraded:
            degraded.extend(f"{name}: {m}" for m in result.degraded)

    if blocking:
        diags.append(make_diagnostic(
            DiagnosticSeverity.ERROR, TaepDiagnosticCode.EXPORT_PREFLIGHT_FAILED,
            detail="; ".join(blocking),
        ))
    for msg in degraded:
        diags.append(make_diagnostic(
            DiagnosticSeverity.WARNING, TaepDiagnosticCode.EXPORT_PREFLIGHT_FAILED,
            detail=msg,
        ))

    commercial_check = check_generated_media_for_export(
        generation_bin,
        require_commercial_ready=commercial_ready_required,
        required_license_region=required_license_region,
    )
    if not commercial_check.allowed:
        blocking.extend(f"commercial: {message}" for message in commercial_check.blocking)
        diags.extend(
            make_diagnostic(
                DiagnosticSeverity.ERROR,
                TaepDiagnosticCode.EXPORT_PREFLIGHT_FAILED,
                detail=message,
            )
            for message in commercial_check.blocking
        )

    quality_result = evaluate_delivery_quality(delivery_quality) if delivery_quality is not None else None
    if quality_result is not None and not quality_result.passed:
        blocking.extend(f"quality: {message}" for message in quality_result.blocking)
        diags.extend(quality_result.diagnostics)

    return PreflightReport(
        ok=not blocking,
        blocking=tuple(blocking),
        degraded=tuple(degraded),
        delivery_quality=quality_result,
        diagnostics=tuple(diags),
    )


# ---------------------------------------------------------------------- #
# ExportService
# ---------------------------------------------------------------------- #


class ExportService:
    """把 ExportRequest 映射到 TimelineExportConfig + preflight + job。

    本服务只负责**准备**导出（config + preflight + job 句柄）；实际编码由
    TimelineExporter 在 worker 线程执行（宿主或 P4 worker 接线）。
    """

    def __init__(
        self, job_registry: JobRegistry | None = None,
        *, generation_bin: Any | None = None,
    ) -> None:
        self._jobs = job_registry or JobRegistry()
        self._generation_bin = generation_bin

    @property
    def jobs(self) -> JobRegistry:
        return self._jobs

    # ---------------- config 构建 ---------------- #

    def build_export_config(
        self, request: ExportRequest, state: ProjectSequences
    ) -> tuple[TimelineExportConfig | None, Diagnostic | None]:
        """把 ExportRequest 映射到 ``TimelineExportConfig``。

        不写文件；只构造配置。返回 (config, error_diagnostic_or_none)。
        """
        from src.features.edit.core.timeline_export import (
            ExportCodec,
            ExportRangeType,
            ExportSource,
            TimelineExportConfig,
        )
        from src.shared.media.metadata import Rational

        sequence = state.sequences.get(request.timeline_id)
        if sequence is None:
            return None, make_diagnostic(
                DiagnosticSeverity.ERROR, TaepDiagnosticCode.TIMELINE_NOT_FOUND,
                timeline_id=request.timeline_id,
            )

        # codec
        try:
            codec = ExportCodec(request.codec)
        except ValueError:
            return None, make_diagnostic(
                DiagnosticSeverity.ERROR, TaepDiagnosticCode.EXPORT_PREFLIGHT_FAILED,
                detail=f"unsupported codec {request.codec!r}",
            )

        # fps
        fps_value: Any = None
        if request.fps:
            try:
                fps_value = Rational.from_string(request.fps)
            except Exception:
                return None, make_diagnostic(
                    DiagnosticSeverity.ERROR, TaepDiagnosticCode.EXPORT_PREFLIGHT_FAILED,
                    detail=f"invalid fps {request.fps!r}",
                )

        # range
        if request.range is not None:
            range_type = ExportRangeType.CUSTOM
            start_frame = request.range.start
            end_frame = request.range.end - 1  # CUSTOM 用闭区间 end
        else:
            range_type = ExportRangeType.ENTIRE
            start_frame = 0
            end_frame = -1

        config = TimelineExportConfig(
            source=ExportSource.TIMELINE,
            timeline_id=request.timeline_id,
            range_type=range_type,
            start_frame=start_frame,
            end_frame=end_frame,
            resolution=request.resolution,
            fps=fps_value,
            codec=codec,
            include_audio=request.include_audio,
            burn_subtitles=request.burn_subtitles,
            output_path=request.output_path or "",
            ai_plan_id=request.plan_id,
        )
        return config, None

    # ---------------- preflight + job 提交 ---------------- #

    def request_export(
        self, request: ExportRequest, state: ProjectSequences
    ) -> tuple[JobHandle | None, PreflightReport, Any]:
        """创建导出提议：build config → preflight → 提交 job（不立即编码）。

        Returns:
            (job_handle_or_none, preflight_report, export_config_or_none)。
            若 preflight 有 blocking，返回 None job + 失败 report。
        """
        config, err = self.build_export_config(request, state)
        if config is None:
            failed = PreflightReport(ok=False, blocking=(err.message,), diagnostics=(err,))
            return None, failed, None

        sequence = state.sequences.get(request.timeline_id)
        report = run_export_preflight(
            config,
            sequence,
            generation_bin=self._generation_bin,
            commercial_ready_required=request.commercial_ready_required,
            required_license_region=request.required_license_region,
        )
        if report.has_blocking:
            return None, report, config

        # 提交 job（queued；实际编码由 worker 接）
        job = self._jobs.submit(
            JobKind.EXPORT,
            label=f"Export {request.codec} {request.timeline_id}",
        )
        # T-P1-70 步骤 4：导出 job ID 回填配置——交付 manifest 可把输出
        # 文件、preflight、GPU trail 关联回本 job（AI 审计回放锚点）。
        config.ai_job_id = job.job_id
        return job, report, config

    # ---------------- 预览 ---------------- #

    def request_preview(
        self,
        timeline_id: str,
        state: ProjectSequences,
        *,
        preset_name: str = "quick_10s",
        clip_range: FrameRange | None = None,
    ) -> tuple[JobHandle | None, str]:
        """创建短范围预览 job（TAEP §11.6 步骤 12）。

        预览不与最终 export 混用；使用低质量 preset，限制帧数。
        """
        preset = DEFAULT_PREVIEW_PRESETS.get(preset_name)
        if preset is None:
            return None, f"unknown preview preset {preset_name!r}"

        sequence = state.sequences.get(timeline_id)
        if sequence is None:
            return None, f"timeline {timeline_id!r} not found"

        # 限制范围：不超过 preset.max_frames；空 timeline 至少 1 帧（FrameRange 合同）
        seq_duration = max(1, sequence.duration_frames)
        if clip_range is not None:
            preview_range = FrameRange(
                clip_range.start,
                max(1, min(clip_range.duration, preset.max_frames)),
            )
        else:
            preview_range = FrameRange(0, min(seq_duration, preset.max_frames))

        job = self._jobs.submit(
            JobKind.PREVIEW,
            label=f"Preview {preset_name} [{preview_range.start},{preview_range.end})",
        )
        return job, ""

    def finalize_export(
        self,
        job: JobHandle,
        result_manifest: dict[str, Any],
        *,
        delivery_quality: DeliveryQualityInput | None = None,
    ) -> bool:
        """Complete an export only after optional post-encode quality checks.

        Workers call this after encoding and probing the output.  A failed
        quality gate turns the job into a visible failure and never reports a
        bad file as successful.
        """
        quality_result = evaluate_delivery_quality(delivery_quality) if delivery_quality is not None else None
        if quality_result is not None and not quality_result.passed:
            job.fail(
                "EXPORT_QUALITY_GATE_FAILED",
                "; ".join(quality_result.blocking)[:500],
            )
            return False
        job.complete(result_manifest)
        return True

    def finalize_export_with_probe(
        self,
        job: JobHandle,
        result_manifest: dict[str, Any],
        probe: Any,
        *,
        quality_analyzer: Any | None = None,
        expected_resolution: tuple[int, int] | None = None,
        expected_fps: str | None = None,
        expected_duration_seconds: float | None = None,
        expected_audio_sample_rate: int | None = None,
        expected_lufs: float | None = None,
        lufs_tolerance: float = 1.0,
        silence_policy: str = "measure_only",
        max_silence_seconds: float | None = None,
        platform_spec: PlatformDeliverySpec | dict[str, Any] | str | None = None,
        subtitle_cues: Sequence[Any] | None = None,
        subtitle_timeline_duration_frames: int | None = None,
        subtitle_fps: float | None = None,
        subtitle_max_words_per_second: float = 20.0,
        subtitle_max_chars_per_line: int | None = None,
        brand_elements: Sequence[Any] | None = None,
        brand_requirements: Sequence[Any] | None = None,
        brand_timeline_duration_frames: int | None = None,
        export_telemetry: dict[str, Any] | None = None,
    ) -> bool:
        """Probe the encoded file, then finalize through the same hard gate.

        ``export_telemetry``（T-P0-20）：统一导出阶段 telemetry dict，随
        result_manifest 进入 job 状态（``topos.get_job_status`` 可读）。
        """
        if export_telemetry:
            result_manifest = dict(result_manifest)
            result_manifest["export_telemetry"] = export_telemetry
        output_path = str(result_manifest.get("output") or result_manifest.get("path") or "")
        if not output_path or not callable(getattr(probe, "probe_and_evaluate", None)):
            job.fail("EXPORT_PROBE_UNAVAILABLE", "post-export media probe is unavailable")
            return False
        try:
            probe_result, quality_result = probe.probe_and_evaluate(
                output_path,
                expected_resolution=expected_resolution,
                expected_fps=expected_fps,
                expected_duration_seconds=expected_duration_seconds,
                expected_audio_sample_rate=expected_audio_sample_rate,
            )
        except Exception as exc:  # noqa: BLE001 — failed verification is terminal
            job.fail("EXPORT_PROBE_FAILED", f"post-export probe raised: {type(exc).__name__}")
            return False
        if not bool(getattr(probe_result, "succeeded", False)):
            error_code = str(getattr(probe_result, "error_code", "PROBE_FAILED"))
            job.fail("EXPORT_QUALITY_GATE_FAILED", f"post-export probe failed: {error_code}")
            return False

        quality_input = getattr(probe_result, "quality_input", None)
        if not isinstance(quality_input, DeliveryQualityInput):
            job.fail("EXPORT_PROBE_FAILED", "post-export probe returned no quality input")
            return False

        quality_input = _attach_artifact_presence(quality_input, result_manifest)
        # M21.2（R05）：深测量——ffprobe 只给容器级元数据，黑/冻结/损坏帧
        # 与静音此前默认 0（未测冒充测得为零）。用 PyAV 实测补齐：
        # 测量失败（深探测 None）按原输入继续，但黑/冻/坏帧字段保持
        # probe 原值——不冒充已测量（这些字段在 ffprobe 输入中从未被
        # 设置为非默认值的情况由深探测覆盖；无法深测时 gate 的 0 值
        # 语义风险登记在 run_summary）。
        try:
            from src.features.edit.ai_editing.delivery_quality_probe import (
                measure_delivery_quality,
            )
            deep = measure_delivery_quality(
                output_path,
                expected_resolution=expected_resolution,
                expected_fps=expected_fps,
                expected_audio_sample_rate=expected_audio_sample_rate,
                expected_lufs=expected_lufs,
                max_silence_seconds=max_silence_seconds,
            )
        except Exception:  # noqa: BLE001 — 深测量异常不掩盖主探测结果
            deep = None
        if deep is not None:
            quality_input = replace(
                quality_input,
                black_frame_count=deep.black_frame_count,
                frozen_frame_count=deep.frozen_frame_count,
                corrupt_frame_count=deep.corrupt_frame_count,
                peak_db=deep.peak_db,
                silence_seconds=deep.silence_seconds,
                integrated_lufs=(deep.integrated_lufs
                                 if deep.integrated_lufs is not None
                                 else quality_input.integrated_lufs),
                loudness_backend_available=(
                    deep.loudness_backend_available
                    if deep.loudness_backend_available is not None
                    else quality_input.loudness_backend_available),
            )
            result_manifest = dict(result_manifest)
            result_manifest["quality_deep_probe"] = {
                "black_frame_count": deep.black_frame_count,
                "frozen_frame_count": deep.frozen_frame_count,
                "corrupt_frame_count": deep.corrupt_frame_count,
                "silence_seconds": deep.silence_seconds,
                "integrated_lufs": deep.integrated_lufs,
                "loudness_backend_available": deep.loudness_backend_available,
            }
        analysis = None
        if quality_analyzer is not None:
            try:
                analysis = quality_analyzer.analyze(
                    output_path,
                    audio_present=quality_input.audio_present,
                    audio_sample_rate=quality_input.audio_sample_rate,
                )
            except Exception as exc:  # noqa: BLE001 — final media analysis is a hard gate
                job.fail("EXPORT_MEDIA_ANALYSIS_FAILED", f"delivery analysis raised: {type(exc).__name__}")
                return False
            quality_input = _attach_delivery_analysis(quality_input, analysis)
        if expected_lufs is not None:
            quality_input = replace(
                quality_input,
                expected_lufs=expected_lufs,
                lufs_tolerance=lufs_tolerance,
            )
        quality_input = replace(
            quality_input,
            silence_policy=silence_policy,
            max_silence_seconds=max_silence_seconds,
        )
        platform_result: PlatformValidationResult | None = None
        if platform_spec is not None:
            platform_result = validate_platform_delivery(
                platform_spec,
                metadata=getattr(probe_result, "metadata", None) or {},
                quality_input=quality_input,
            )
            quality_input = replace(
                quality_input,
                platform_errors=quality_input.platform_errors + platform_result.errors,
            )
        subtitle_result: SubtitleValidationResult | None = None
        if subtitle_cues is not None:
            if subtitle_fps is None:
                job.fail("EXPORT_SUBTITLE_QA_FAILED", "字幕质量校验缺少 fps")
                return False
            try:
                subtitle_result = validate_subtitle_cues(
                    subtitle_cues,
                    timeline_duration_frames=subtitle_timeline_duration_frames,
                    fps=subtitle_fps,
                    max_words_per_second=subtitle_max_words_per_second,
                    max_chars_per_line=subtitle_max_chars_per_line,
                )
            except (TypeError, ValueError) as exc:
                job.fail("EXPORT_SUBTITLE_QA_FAILED", f"字幕质量校验无效：{exc}")
                return False
            quality_input = replace(
                quality_input,
                subtitle_out_of_range=subtitle_result.out_of_range,
                subtitle_overlaps=subtitle_result.overlaps,
                subtitle_empty_count=subtitle_result.empty_text,
                subtitle_long_line_count=subtitle_result.long_lines,
                subtitle_safe_area_violations=subtitle_result.safe_area_violations,
                subtitle_reading_words_per_second=subtitle_result.max_words_per_second,
                max_subtitle_words_per_second=subtitle_max_words_per_second,
            )
        brand_result: BrandElementValidationResult | None = None
        if brand_requirements is not None:
            try:
                brand_result = validate_brand_elements(
                    brand_elements or (),
                    brand_requirements,
                    timeline_duration_frames=brand_timeline_duration_frames,
                )
            except (TypeError, ValueError) as exc:
                job.fail("EXPORT_BRAND_QA_FAILED", f"品牌元素质量校验无效：{exc}")
                return False
            quality_input = replace(
                quality_input,
                missing_brand_elements=brand_result.missing,
                brand_element_out_of_range=brand_result.out_of_range,
                brand_element_safe_area_violations=brand_result.safe_area_violations,
                brand_element_duration_violations=brand_result.duration_violations,
                brand_element_invalid_measurements=brand_result.invalid_measurements,
            )
        quality_result = evaluate_delivery_quality(quality_input)
        manifest = dict(result_manifest)
        manifest["probe"] = {
            "succeeded": True,
            "error_code": str(getattr(probe_result, "error_code", "")),
            "quality": quality_result.to_dict(),
        }
        manifest["artifact_checks"] = {
            "manifest_present": quality_input.manifest_present,
            "thumbnail_present": quality_input.thumbnail_present,
        }
        if analysis is not None and callable(getattr(analysis, "to_dict", None)):
            manifest["delivery_analysis"] = analysis.to_dict()
        if platform_result is not None or subtitle_result is not None or brand_result is not None:
            manifest["delivery_contracts"] = {}
            if platform_result is not None:
                manifest["delivery_contracts"]["platform"] = platform_result.to_dict()
            if subtitle_result is not None:
                manifest["delivery_contracts"]["subtitles"] = subtitle_result.to_dict()
            if brand_result is not None:
                manifest["delivery_contracts"]["brand_elements"] = brand_result.to_dict()
        manifest_report = {"probe": manifest["probe"]}
        if "delivery_analysis" in manifest:
            manifest_report["delivery_analysis"] = manifest["delivery_analysis"]
        if "delivery_contracts" in manifest:
            manifest_report["delivery_contracts"] = manifest["delivery_contracts"]
        manifest["delivery_policies"] = {
            "silence": {
                "policy": silence_policy,
                "max_silence_seconds": max_silence_seconds,
            },
        }
        manifest_report["delivery_policies"] = manifest["delivery_policies"]
        _update_export_manifest(result_manifest.get("manifest_path"), manifest_report)
        return self.finalize_export(job, manifest, delivery_quality=quality_input)

    def complete_host_export(
        self,
        job: JobHandle,
        output_path: str,
        *,
        probe: Any,
        quality_analyzer: Any | None = None,
        expected_resolution: tuple[int, int] | None = None,
        expected_fps: str | None = None,
        expected_duration_seconds: float | None = None,
        expected_audio_sample_rate: int | None = None,
        expected_lufs: float | None = None,
        lufs_tolerance: float = 1.0,
        silence_policy: str = "measure_only",
        max_silence_seconds: float | None = None,
        platform_spec: PlatformDeliverySpec | dict[str, Any] | str | None = None,
        subtitle_cues: Sequence[Any] | None = None,
        subtitle_timeline_duration_frames: int | None = None,
        subtitle_fps: float | None = None,
        subtitle_max_words_per_second: float = 20.0,
        subtitle_max_chars_per_line: int | None = None,
        brand_elements: Sequence[Any] | None = None,
        brand_requirements: Sequence[Any] | None = None,
        brand_timeline_duration_frames: int | None = None,
        artifact_metadata: dict[str, Any] | None = None,
        ffmpeg_bin: str = "ffmpeg",
        export_telemetry: dict[str, Any] | None = None,
    ) -> tuple[bool, str]:
        """Complete a host-encoded export from the encoder worker thread.

        The callback owns the final transition: artifacts are materialized,
        ffprobe runs, the quality gate evaluates, and only then is the job
        marked succeeded.  This is the canonical host completion seam.
        """
        artifacts = materialize_export_artifacts(
            output_path,
            metadata=artifact_metadata,
            ffmpeg_bin=ffmpeg_bin,
        )
        if not artifacts.succeeded:
            job.fail("EXPORT_ARTIFACTS_FAILED", artifacts.error_message or artifacts.error_code)
            return False, artifacts.error_message or artifacts.error_code
        manifest = {
            "output": output_path,
            "manifest_path": artifacts.manifest_path,
            "thumbnail_path": artifacts.thumbnail_path,
        }
        accepted = self.finalize_export_with_probe(
            job,
            manifest,
            probe,
            quality_analyzer=quality_analyzer,
            expected_resolution=expected_resolution,
            expected_fps=expected_fps,
            expected_duration_seconds=expected_duration_seconds,
            expected_audio_sample_rate=expected_audio_sample_rate,
            expected_lufs=expected_lufs,
            lufs_tolerance=lufs_tolerance,
            silence_policy=silence_policy,
            max_silence_seconds=max_silence_seconds,
            platform_spec=platform_spec,
            subtitle_cues=subtitle_cues,
            subtitle_timeline_duration_frames=subtitle_timeline_duration_frames,
            subtitle_fps=subtitle_fps,
            subtitle_max_words_per_second=subtitle_max_words_per_second,
            subtitle_max_chars_per_line=subtitle_max_chars_per_line,
            brand_elements=brand_elements,
            brand_requirements=brand_requirements or None,
            brand_timeline_duration_frames=brand_timeline_duration_frames,
            export_telemetry=export_telemetry,
        )
        return accepted, "" if accepted else (job.error_message or "export quality gate failed")


def _attach_artifact_presence(
    quality_input: DeliveryQualityInput,
    result_manifest: dict[str, Any],
) -> DeliveryQualityInput:
    """Turn explicit artifact paths into fail-closed quality measurements."""
    updates: dict[str, Any] = {}
    for field_name, path_key in (
        ("manifest_present", "manifest_path"),
        ("thumbnail_present", "thumbnail_path"),
    ):
        if path_key in result_manifest:
            value = result_manifest.get(path_key)
            updates[field_name] = bool(value) and _is_nonempty_file(Path(str(value)))
        elif field_name in result_manifest:
            updates[field_name] = bool(result_manifest.get(field_name))
    return replace(quality_input, **updates) if updates else quality_input


def _attach_delivery_analysis(
    quality_input: DeliveryQualityInput,
    analysis: Any,
) -> DeliveryQualityInput:
    """Merge measured frame/audio health into the delivery gate input."""
    updates: dict[str, Any] = {}
    for field_name in (
        "black_frame_count", "frozen_frame_count", "corrupt_frame_count",
        "peak_db", "silence_seconds", "audio_present", "integrated_lufs",
        "loudness_range_lu", "loudness_backend_available",
    ):
        if hasattr(analysis, field_name):
            value = getattr(analysis, field_name)
            if value is not None:
                if field_name.endswith("_count"):
                    value = max(int(getattr(quality_input, field_name)), int(value))
                updates[field_name] = value
    return replace(quality_input, **updates) if updates else quality_input


def _is_nonempty_file(path: Path) -> bool:
    try:
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


_MANIFEST_PATH_KEYS = {
    "path", "output_path", "file_path", "filepath", "filename",
    "source", "destination", "manifest_path", "thumbnail_path",
}


def _redact_manifest_value(value: Any, *, key: str = "") -> Any:
    """Keep manifest metadata useful without persisting machine-local paths."""
    if key.lower() in _MANIFEST_PATH_KEYS:
        return "<redacted>"
    if isinstance(value, dict):
        return {
            str(item_key): _redact_manifest_value(item, key=str(item_key))
            for item_key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redact_manifest_value(item) for item in value]
    if isinstance(value, str):
        return value.replace("/Users/", "[PATH_REDACTED]/").replace(
            "/private/", "[PATH_REDACTED]/",
        )
    return value


def _update_export_manifest(path_value: Any, report: dict[str, Any]) -> None:
    """Add redacted probe/analysis reports to a generated manifest atomically."""
    if not path_value:
        return
    path = Path(str(path_value))
    fd = -1
    temporary = ""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return
        payload.update(report)
        fd, temporary = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent),
        )
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            fd = -1
            json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        try:
            if fd >= 0:
                os.close(fd)
        except (UnboundLocalError, OSError):
            pass
        try:
            if temporary:
                os.remove(temporary)
        except OSError:
            pass


__all__ = [
    "DEFAULT_PREVIEW_PRESETS",
    "ExportArtifactResult",
    "ExportRequest",
    "ExportService",
    "PreflightReport",
    "materialize_export_artifacts",
    "run_export_preflight",
]
