"""Video export preflight — validate a :class:`VideoExportJob` before enqueue.

The preflight layer sits between the settings panel and the queue: it
inspects a candidate job *and* the live project state, and reports a
structured result with blocking errors (cannot enqueue) and warnings (can
enqueue but the user should know). It deliberately never silently fixes
issues — the plan requires that any degradation be surfaced explicitly.

Checks performed
================

* **Context** — project / timeline exist (blocking).
* **Range** — frame range is structurally valid (blocking).
* **Codec / container compatibility** — known combination (blocking).
* **Audio codec compatibility** — e.g. PCM needs mov/mkv (warning, with
  a suggested fix the user must approve).
* **Output path** — non-empty, parent directory writable (blocking).
* **Filename** — non-empty basename (blocking).
* **Disk space** — estimated output fits on the target volume (warning or
  blocking based on severity).
* **Edit-feature preflight** — delegates to the existing
  :func:`preflight_audio_export` / :func:`preflight_video_effects_export`
  / :func:`preflight_fonts_export` so audio backends, video effect
  exporters and subtitle fonts are all checked through the same canonical
  path used by the single-shot exporter.

The edit-feature checks are best-effort: if the edit feature is disabled
the preflight still runs the structural checks and reports the missing
backend as a single warning rather than crashing.
"""

from __future__ import annotations

import logging
import os
import shutil
from dataclasses import dataclass, field
from typing import Any, List, Optional, Tuple

from src.features.deliver.core.video_export_job import (
    OverwritePolicy,
    RangeType,
    SourceKind,
    SubtitleMode,
    VideoExportJob,
)
from src.features.deliver.core.video_export_formats import (
    CODEC_CONTAINER_TABLE,
    codec_container_error,
)

logger = logging.getLogger(__name__)

__all__ = [
    "PreflightSeverity",
    "PreflightIssue",
    "PreflightResult",
    "PreflightContext",
    "run_video_export_preflight",
]


# ── Severity ──

class PreflightSeverity:
    """Issue severity.

    T-P1-70 步骤 2 的三档语义：
    - ``BLOCKING``：阻止入队（AI 必须修复或请求用户决策）；
    - ``DEGRADED``：允许继续，但产出会降级/缺失某能力（encode fallback、
      audio omitted、GPU 不可用、插件 preview-only）——结果中必须显式
      出现，不得只写日志；
    - ``WARNING``：提示性（非阻断也非降级）。
    """
    BLOCKING = "blocking"
    DEGRADED = "degraded"
    WARNING = "warning"
    INFO = "info"


# ── Result types ──


@dataclass
class PreflightIssue:
    """A single preflight finding.

    Attributes:
        severity: one of :class:`PreflightSeverity`.
        code: stable machine-readable code (e.g. ``"range.invalid"``) for
            UI highlighting / testing. Callers should not parse the message.
        message: human-readable, localised-by-caller message.
        field: dotted path to the offending setting (e.g.
            ``"range.end_frame"``) so the UI can focus the right widget.
    """

    severity: str
    code: str
    message: str
    field: str = ""

    @property
    def is_blocking(self) -> bool:
        return self.severity == PreflightSeverity.BLOCKING


@dataclass
class PreflightResult:
    """Aggregated preflight outcome."""

    issues: List[PreflightIssue] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """True if the job may be enqueued (no blocking issues)."""
        return not any(i.is_blocking for i in self.issues)

    @property
    def blocking(self) -> List[PreflightIssue]:
        return [i for i in self.issues if i.severity == PreflightSeverity.BLOCKING]

    @property
    def degraded(self) -> List[PreflightIssue]:
        """降级项（T-P1-70）：产出可行但能力缺失/回退，结果必须携带。"""
        return [i for i in self.issues
                if i.severity == PreflightSeverity.DEGRADED]

    @property
    def warnings(self) -> List[PreflightIssue]:
        return [i for i in self.issues if i.severity == PreflightSeverity.WARNING]

    @property
    def infos(self) -> List[PreflightIssue]:
        return [i for i in self.issues if i.severity == PreflightSeverity.INFO]

    def add(self, issue: PreflightIssue) -> None:
        self.issues.append(issue)

    def add_blocking(self, code: str, message: str, field: str = "") -> None:
        self.issues.append(PreflightIssue(PreflightSeverity.BLOCKING, code, message, field))

    def add_degraded(self, code: str, message: str, field: str = "") -> None:
        """记录降级项（T-P1-70 步骤 2/5：降级必须进结果，不得只写日志）。"""
        self.issues.append(PreflightIssue(PreflightSeverity.DEGRADED, code, message, field))

    def add_warning(self, code: str, message: str, field: str = "") -> None:
        self.issues.append(PreflightIssue(PreflightSeverity.WARNING, code, message, field))

    def add_info(self, code: str, message: str, field: str = "") -> None:
        self.issues.append(PreflightIssue(PreflightSeverity.INFO, code, message, field))


# ── Context ──
#
# Preflight needs a few live facts about the project that the snapshot
# alone can't provide. We take them through a small context object so the
# function is pure and testable.


@dataclass
class PreflightContext:
    """Live context facts the preflight needs to consult.

    All fields are optional so partial contexts (e.g. in tests) work —
    missing facts simply skip the corresponding check.
    """

    project: Any = None
    sequence: Any = None
    #: 序列解析器（timeline_id → TimelineSequence；nested 音频展开用，
    #: 复验 P1-14）。缺省 None 时退回直接音轨检查。
    sequence_resolver: Any = None
    project_id: str = ""
    project_revision: int = 0
    #: Total frame count of the resolved timeline (for range checks). When
    #: ``None`` the range check is relaxed.
    timeline_frame_count: Optional[int] = None
    #: Caller-provided disk-space probe (bytes free on the output volume).
    #: Defaults to :func:`shutil.disk_usage` on the output parent dir.
    disk_free_bytes: Optional[int] = None
    #: Caller-provided estimate of the output size in bytes. When ``None``
    #: the disk-space check is skipped (cannot estimate without sequence).
    estimated_size_bytes: Optional[int] = None
    #: Whether the edit feature is available at all. When False the
    #: edit-backed checks (audio/effects/fonts) are skipped and a single
    #: INFO is emitted.
    edit_feature_available: bool = True
    #: T-P0-00：注入的 RuntimeCapabilitySnapshot（或提供 ``entry()`` 的兼容
    #: 对象）。None 时跳过运行时能力门禁（保持旧行为）；注入后，topos 系
    #: 交付目标在 ``codec.topos`` 非 AVAILABLE 时入队前即 BLOCKING。
    runtime_snapshot: Any = None


# ── Public entry point ──


def run_video_export_preflight(
    job: VideoExportJob,
    context: Optional[PreflightContext] = None,
) -> PreflightResult:
    """Run every applicable check and return the aggregated result.

    Args:
        job: the candidate :class:`VideoExportJob`.
        context: optional live :class:`PreflightContext`.

    Returns:
        :class:`PreflightResult` — check ``result.ok`` before enqueueing.
    """
    ctx = context or PreflightContext()
    result = PreflightResult()

    _check_context(job, ctx, result)
    _check_structural(job, ctx, result)
    _check_runtime_capability(job, ctx, result)
    _check_output_path(job, ctx, result)
    _check_disk_space(job, ctx, result)
    _check_edit_feature_preflights(job, ctx, result)
    _check_nested_audio(job, ctx, result)
    _check_source_patch(job, ctx, result)
    _check_color_delivery_preflight(job, ctx, result)
    _check_color_transform_provider(job, ctx, result)
    _check_topos_video_only(job, ctx, result)

    return result


def _check_runtime_capability(
    job: VideoExportJob, ctx: PreflightContext, result: PreflightResult
) -> None:
    """T-P0-00：交付目标的运行时能力门禁（fail-closed）。

    注入 RuntimeCapabilitySnapshot 后，topos 系 codec 在 ``codec.topos``
    非 AVAILABLE（库缺失/不兼容）时直接 BLOCKING——编码器必然失败，不应
    入队后才暴露。未注入快照时保持旧行为（不探测）。
    """
    snapshot = ctx.runtime_snapshot
    if snapshot is None:
        return
    codec = str(job.video_config.codec)
    if not codec.startswith("topos"):
        return
    entry = None
    try:
        entry = snapshot.entry("codec.topos")
    except Exception:  # noqa: BLE001 — 兼容对象缺 entry() 时跳过（不误报）
        entry = None
    if entry is None:
        return
    state = getattr(getattr(entry, "state", None), "value", None)
    if state == "AVAILABLE":
        return
    reason = getattr(entry, "reason_code", None) or "CODEC_TOPOS_NOT_AVAILABLE"
    detail = getattr(entry, "detail", None) or ""
    result.add_blocking(
        f"runtime.{reason}",
        f"TOPOS codec 当前不可用（state={state}, reason={reason}）"
        f"{('：' + detail) if detail else ''}。请选择其他交付格式或修复环境。",
        field="video.codec",
    )


def _check_topos_video_only(
    job: VideoExportJob, ctx: PreflightContext, result: PreflightResult
) -> None:
    """v1.1 音频轨（container_spec_v1.1）：Topos 容器已支持音频轨。

    选了音频（include_audio）→ 原警告解除（音频将写入输出容器）；
    仅当音频编解码不被 topos 容器支持（mp3/flac/opus 等）时保留
    "将被省略"提示。未选音频但有音频内容 → 维持提示（导出将不含音轨，
    须事先告知——R5 任务 4 语义延续）。
    """
    codec = job.video_config.codec
    if not str(codec).startswith("topos"):
        return
    seq = ctx.sequence
    tracks = getattr(seq, "audio_tracks", None) if seq is not None else None

    # 复验 P1-14：与真实导出一致——先展开 nested sequence 音频轨
    # （父序列无直接音轨、子序列有音频时，此前不触发告警，导出无预警丢音）
    if tracks is not None and ctx.project is not None:
        try:
            from src.features.edit.core.timeline_export import (
                expand_sequence_audio_for_render,
            )

            seq = expand_sequence_audio_for_render(
                seq, ctx.sequence_resolver, ctx.project)
            tracks = getattr(seq, "audio_tracks", None)
        except Exception:  # noqa: BLE001 — 展开失败退回直接音轨（同导出降级）
            tracks = getattr(seq, "audio_tracks", None)
    if not tracks:
        return
    # 只有带 clip 的音频轨才算真实音频内容（空轨不算）
    has_audio = any(getattr(t, "clips", None) for t in tracks)
    if not has_audio:
        return

    if job.audio_config.include_audio:
        # v1.1：音频将写入 topos 容器；不支持的音乐编解码仍须提示
        audio_codec = str(getattr(job.audio_config, "codec", "") or "").lower()
        if audio_codec not in ("aac", "pcm_s16", "pcm_s16le",
                               "pcm_s24", "pcm_s24le"):
            result.add_warning(
                "topos.video_only",
                f"Topos 容器不支持音频编解码 {audio_codec or '(空)'}："
                "音频将被省略。可选 aac（对外交付）或 pcm_s16/pcm_s24"
                "（制作链路无损）。",
                field="audio.codec",
            )
        return
    result.add_warning(
        "topos.video_only",
        "时间线包含音频轨，但导出未勾选「包含音频」：输出文件将不含音轨。",
        field="audio.include_audio",
    )


def _check_color_delivery_preflight(
    job: VideoExportJob, ctx: PreflightContext, result: PreflightResult
) -> None:
    """P1-COLOR-25: surface color operations the CPU video export drops.

    The timeline export path only applies the flattened primary grade;
    curves/HSL/mixer/LUT/soft-clip/windows/qualifier/plugins are silently
    absent from the output. Preflight deliberately never silently fixes
    issues — each lossy operation becomes a stable-code WARNING so the
    settings panel can show it before the job is enqueued.
    """
    if ctx.project is None or ctx.sequence is None:
        return
    if job.source_kind != SourceKind.TIMELINE:
        return
    try:
        from src.features.color.core.delivery_matrix import (
            delivery_preflight_issues,
            report_from_color_stacks,
            sequence_color_stacks,
            sequence_video_clip_ids,
        )
    except ImportError as e:
        # The color feature being absent is a supported product mode, not an
        # error — a single-line notice is enough (AUD-11914: never silent).
        logger.info("Color delivery matrix unavailable; skipping color preflight: %s", e)
        return
    try:
        stacks = sequence_color_stacks(ctx.sequence, ctx.project)
        if not stacks:
            return
        report = report_from_color_stacks(
            stacks, "video",
            tracking_clip_ids=sequence_video_clip_ids(ctx.sequence),
        )
        # N03：丢效果升级为阻断（与 ExportGradeDegradedError 硬门同
        # 口径）——CPU 视频导出会丢弃的色彩操作不再只是 WARNING 放行
        # （视觉错误标成功）；仅 job 显式声明 allow_degraded_grade 时
        # 降回 WARNING（声明式取舍，与 timeline_export 降级开关联动）。
        config = getattr(job, "config", None) or {}
        try:
            allow_degraded = bool(
                int(config.get("allow_degraded_grade", 0)))
        except (TypeError, ValueError):
            allow_degraded = False
        for code, message in delivery_preflight_issues(report):
            if allow_degraded:
                result.add_warning(code, message, field="color_delivery")
            else:
                result.add_blocking(
                    f"{code}.blocking",
                    f"{message}——视频导出将丢弃该色彩处理，默认拒绝入队"
                    "（allow_degraded_grade=1 可显式接受降级）",
                    field="color_delivery")
    except Exception:
        # 报告生成失败不阻断导出（能力探测是尽力而为），但绝不静默：
        # 该检查的全部职责就是把“视频导出会丢弃哪些色彩处理”提示给
        # 用户，集成错误必须留下日志（与同文件其它检查一致）。
        logger.exception("Color delivery preflight failed; continuing without color warnings")


def _check_color_transform_provider(
    job: VideoExportJob, ctx: PreflightContext, result: PreflightResult
) -> None:
    """M19（R01）：交付色彩转换可用性预检——必要转换不可达时入队前
    明示（不冒充转换）。

    导出管线在场景线性 → 交付传输信号（默认 bt709/limited）走
    ``transform_color``；provider 不可用/processor 缺失时该函数静默
    返回原输入（恒等降级），成片颜色错而任务"成功"。此检查以 1×1
    测试像素探测同一转换的 typed 结果，非 ok → 稳定码 WARNING
    （与 lossy color 确认门同面板呈现）。
    """
    try:
        import numpy as np
        from src.shared.color.color_space_descriptor import (
            LINEAR_SCENE, ColorSpaceDescriptor,
        )
        from src.shared.color.color_transform_provider import (
            transform_color_typed,
        )
        from src.shared.color.types import (
            ColorPrimaries, ColorRange, ColorTransfer,
        )

        dst = ColorSpaceDescriptor(
            primaries=ColorPrimaries.BT709,
            transfer=ColorTransfer.BT709,
            range=ColorRange.LIMITED,
        )
        probe = np.full((1, 1, 3), 0.5, dtype=np.float32)
        outcome = transform_color_typed(probe, LINEAR_SCENE, dst)
        if not outcome.ok:
            result.add_warning(
                "color.transform.unavailable",
                f"场景线性→交付信号色彩转换不可用（{outcome.status}"
                f"{': ' + outcome.detail if outcome.detail else ''}，"
                f"provider={outcome.provider}）——导出将回退恒等/显式裁剪，"
                f"颜色不正确",
                field="color_delivery",
            )
    except ImportError as e:
        logger.info(
            "Color transform provider unavailable; skipping provider "
            "preflight: %s", e)
    except Exception:
        logger.exception(
            "Color transform provider preflight failed; continuing")


# ── Individual checks ──


def _check_context(job: VideoExportJob, ctx: PreflightContext, result: PreflightResult) -> None:
    """Project / timeline existence."""
    if ctx.project is None:
        result.add_blocking(
            "context.no_project",
            "No project is open. Open or create a project before adding a video task.",
            field="project",
        )
        return

    if job.source_kind == SourceKind.TIMELINE:
        if not job.timeline_id:
            result.add_blocking(
                "context.no_timeline",
                "A timeline is required. Select a timeline before adding a video task.",
                field="timeline",
            )
            return

        if ctx.sequence is None:
            result.add_blocking(
                "context.timeline_missing",
                f"The timeline “{job.timeline_name or job.timeline_id}” could not be found. "
                "It may have been removed.",
                field="timeline",
            )
            return

        # Detect project-revision drift.
        if ctx.project_revision and job.project_revision and ctx.project_revision != job.project_revision:
            result.add_warning(
                "context.revision_drift",
                f"The project has changed since this task was configured "
                f"(task revision {job.project_revision}, current {ctx.project_revision}). "
                "The output may not match what you previewed.",
                field="project",
            )


def _check_structural(job: VideoExportJob, ctx: PreflightContext, result: PreflightResult) -> None:
    """Range / codec / container structural validity."""
    # Range
    if job.range_type == RangeType.CUSTOM:
        if job.end_frame >= 0 and job.end_frame < job.start_frame:
            result.add_blocking(
                "range.invalid",
                "End frame must be greater than or equal to the start frame.",
                field="range.end_frame",
            )
        elif ctx.timeline_frame_count is not None and job.start_frame >= ctx.timeline_frame_count:
            result.add_blocking(
                "range.start_out_of_bounds",
                f"Start frame {job.start_frame} is beyond the timeline length "
                f"({ctx.timeline_frame_count} frames).",
                field="range.start_frame",
            )

    # Codec / container compatibility
    codec = job.video_config.codec
    container = job.video_config.container.lower()
    codec_container_ok, reason = _check_codec_container(codec, container)
    if not codec_container_ok:
        result.add_blocking(
            "codec.container_incompatible",
            reason or f"Codec “{codec}” is not compatible with container “{container}”.",
            field="video.container",
        )

    if codec == "gif":
        color_count = int(getattr(job.video_config, "gif_color_count", 256))
        if not 2 <= color_count <= 256:
            result.add_blocking(
                "gif.colors_invalid",
                "GIF color count must be between 2 and 256.",
                field="video.gif_color_count",
            )
        if job.video_config.fps is not None and job.video_config.fps > 100:
            result.add_blocking(
                "gif.fps_invalid",
                "GIF frame rate cannot exceed 100 fps.",
                field="video.fps",
            )
        if job.audio_config.include_audio:
            result.add_warning(
                "gif.audio_unsupported",
                "Animated GIF has no audio track; the audio setting will be ignored.",
                field="audio.include_audio",
            )

    # Audio codec compatibility (warning — we suggest a fix rather than block)
    if job.audio_config.include_audio and codec != "gif":
        audio_ok, suggestion = _check_audio_codec(job.audio_config.codec, container)
        if not audio_ok:
            result.add_warning(
                "audio.codec_incompatible",
                suggestion or f"Audio codec “{job.audio_config.codec}” may not be supported in {container}.",
                field="audio.codec",
            )

    # Subtitle sidecar requires a writable directory next to the output
    if job.subtitle_config.mode == SubtitleMode.SIDECAR and job.output_path:
        parent = os.path.dirname(job.output_path) or "."
        if not os.path.isdir(parent):
            result.add_warning(
                "subtitle.sidecar_no_dir",
                "Subtitle sidecar export needs the output directory to exist.",
                field="subtitle",
            )


def _check_output_path(job: VideoExportJob, ctx: PreflightContext, result: PreflightResult) -> None:
    """Output path validity and writability."""
    if not job.output_path:
        result.add_blocking(
            "output.empty",
            "An output path is required.",
            field="output.path",
        )
        return

    basename = os.path.basename(job.output_path)
    if not basename:
        result.add_blocking(
            "output.empty_filename",
            "The output filename is empty.",
            field="output.path",
        )

    if (
        job.video_config.codec == "gif"
        and os.path.splitext(job.output_path)[1].lower() != ".gif"
    ):
        result.add_blocking(
            "output.gif_extension",
            "GIF output must use a .gif filename.",
            field="output.path",
        )

    # For a single video file the parent directory must exist & be writable.
    codec = job.video_config.codec
    is_sequence = codec in (
        "png_sequence", "tiff_sequence", "topos_image_sequence",
    )
    target_dir = job.output_path if (is_sequence and os.path.isdir(job.output_path)) else (
        os.path.dirname(job.output_path) or "."
    )

    if not os.path.exists(target_dir):
        result.add_blocking(
            "output.dir_missing",
            f"The output directory does not exist: {target_dir}",
            field="output.path",
        )
        return

    if not os.access(target_dir, os.W_OK):
        result.add_blocking(
            "output.dir_not_writable",
            f"The output directory is not writable: {target_dir}",
            field="output.path",
        )

    # Existing-file handling per policy
    if (
        not is_sequence
        and os.path.exists(job.output_path)
        and os.path.isfile(job.output_path)
    ):
        if job.overwrite_policy == OverwritePolicy.ASK:
            result.add_warning(
                "output.exists",
                f"A file already exists at “{job.output_path}”. You will be asked before overwriting.",
                field="output.path",
            )
        elif job.overwrite_policy == OverwritePolicy.OVERWRITE:
            result.add_warning(
                "output.will_overwrite",
                f'The existing file "{basename}" will be overwritten.',
                field="output.path",
            )
        elif job.overwrite_policy == OverwritePolicy.SKIP:
            result.add_info(
                "output.will_skip",
                f"The existing file “{basename}” will be skipped.",
                field="output.path",
            )


def _check_disk_space(job: VideoExportJob, ctx: PreflightContext, result: PreflightResult) -> None:
    """Disk space estimate vs free space."""
    if ctx.estimated_size_bytes is None:
        return  # nothing to compare against
    if not job.output_path:
        return  # already reported as blocking

    free = ctx.disk_free_bytes
    if free is None:
        try:
            target_dir = (
                os.path.dirname(job.output_path)
                or os.path.dirname(job.output_path)
                or "."
            )
            free = shutil.disk_usage(target_dir).free
        except OSError:
            return  # can't probe — skip silently

    estimate = int(ctx.estimated_size_bytes)
    # Treat <5% headroom as blocking, <25% as warning.
    if estimate > free:
        result.add_blocking(
            "disk.insufficient",
            _format_disk_message(estimate, free, "not enough"),
            field="output.path",
        )
    elif estimate > free * 0.95:
        result.add_blocking(
            "disk.too_tight",
            _format_disk_message(estimate, free, "dangerously low"),
            field="output.path",
        )
    elif estimate > free * 0.75:
        result.add_warning(
            "disk.low",
            _format_disk_message(estimate, free, "low"),
            field="output.path",
        )


def _list_placeholder_clips(project, sequence):
    """B7: 收集时间线上的占位片段 [(clip, asset), ...]。"""
    from src.features.edit.core.placeholder_frame import is_placeholder_asset

    results = []
    assets = getattr(project, "assets", None)
    if not isinstance(assets, dict):
        return results
    for track in getattr(sequence, "all_tracks", ()) or ():
        for clip in getattr(track, "clips", ()) or ():
            asset = assets.get(clip.asset_id)
            if asset is not None and is_placeholder_asset(asset):
                results.append((clip, asset))
    results.sort(key=lambda item: (item[0].timeline_start, item[0].track_id))
    return results


def _check_edit_feature_preflights(
    job: VideoExportJob, ctx: PreflightContext, result: PreflightResult
) -> None:
    """Delegate to the edit feature's own preflight functions."""
    if not ctx.edit_feature_available or ctx.sequence is None:
        if not ctx.edit_feature_available:
            result.add_info(
                "edit_feature.unavailable",
                "The edit feature is unavailable; audio/effect/font checks were skipped.",
            )
        return

    try:
        from src.features.edit.core.timeline_export import (
            preflight_audio_export,
            preflight_video_effects_export,
            preflight_fonts_export,
        )
    except ImportError:
        logger.debug("Edit feature preflight unavailable; skipping")
        return

    # Build a TimelineExportConfig solely for the preflight functions; this
    # is cheap and the same conversion runs at export time.
    try:
        cfg = job.to_timeline_export_config()
    except Exception as e:  # noqa: BLE001 — don't crash preflight on mapping errors
        result.add_warning(
            "edit_feature.config_map_failed",
            f"Could not map the task to the exporter config: {e}",
        )
        return

    # Audio backend (RMA-01). GIF cannot write audio, so it must not block on
    # an audio-only dependency when a legacy job still has include_audio=true.
    if job.video_config.codec != "gif":
        try:
            r = preflight_audio_export(cfg, ctx.sequence)
            for msg in r.blocking_messages:
                result.add_blocking("edit.audio_backend", msg, field="audio")
            for msg in r.degraded_messages:
                result.add_warning("edit.audio_backend", msg, field="audio")
        except Exception as e:  # noqa: BLE001
            logger.exception("preflight_audio_export raised")
            result.add_warning("edit.audio_backend", f"Audio preflight failed: {e}")

    # Video effect exporters (FX-M05). T-P1-20：结构化阻断码按 effect
    # 透传（edit.video_effects.<code>），旧消息文本保留为 detail。
    try:
        r = preflight_video_effects_export(ctx.sequence)
        blockers = getattr(r, "blockers", ()) or ()
        for blocker in blockers:
            result.add_blocking(
                f"edit.video_effects.{blocker.code}",
                blocker.message,
                field="video",
            )
        for msg in r.blocking_messages:
            if not any(b.message == msg for b in blockers):
                result.add_blocking("edit.video_effects", msg, field="video")
        for msg in r.degraded_messages:
            result.add_warning("edit.video_effects", msg, field="video")
    except Exception as e:  # noqa: BLE001
        logger.exception("preflight_video_effects_export raised")
        result.add_warning("edit.video_effects", f"Video-effects preflight failed: {e}")

    # B7: 占位符片段拦截——默认 blocking 并逐条列出（位置+备注）；
    # 用户显式允许时降级为 warning（渲染黑场+边框警告）。
    try:
        placeholders = _list_placeholder_clips(ctx.project, ctx.sequence)
    except Exception as e:  # noqa: BLE001 — 检查失败不阻断其它项
        logger.debug("placeholder preflight failed: %s", e)
        placeholders = None
    if placeholders:
        allow = bool(
            getattr(job, "allow_placeholder_clips", False)
            or (job.extra_options or {}).get("allow_placeholder_clips", False)
            if isinstance(getattr(job, "extra_options", None), dict)
            else getattr(job, "allow_placeholder_clips", False)
        )
        for clip, asset in placeholders:
            note = getattr(asset, "metadata", {}).get("note", "") or ""
            message = (
                f"占位片段 {clip.clip_id}@{clip.timeline_start}f "
                f"({asset.display_name})"
                + (f"：{note}" if note else "")
            )
            if allow:
                result.add_warning("edit.placeholder_clips", message, field="video")
            else:
                result.add_blocking("edit.placeholder_clips", message, field="video")

    # Subtitle fonts (only when burning)
    if job.subtitle_config.mode == SubtitleMode.BURN:
        try:
            r = preflight_fonts_export(ctx.sequence)
            for msg in r.blocking_messages:
                result.add_blocking("edit.fonts", msg, field="subtitle")
            for msg in r.degraded_messages:
                result.add_warning("edit.fonts", msg, field="subtitle")
        except Exception as e:  # noqa: BLE001
            logger.exception("preflight_fonts_export raised")
            result.add_warning("edit.fonts", f"Fonts preflight failed: {e}")


# ── compatibility tables ──


# Backwards-compatible alias for callers that imported the old private table.
_CODEC_CONTAINER_TABLE = CODEC_CONTAINER_TABLE

# F3.1：嵌套音频诊断 code → 预检级别。渲染期 strict 会阻断的组合映射
# BLOCKING（入队前即拒绝，与 Stem 对话框开箱预检对齐）；静默降级的组合
# （子音轨 crossfade 丢弃）映射 WARNING。
_NESTED_AUDIO_WARNING_CODES = {"nested_audio_transition_dropped"}


def _check_nested_audio(job: VideoExportJob, ctx: PreflightContext, result: PreflightResult) -> None:
    """F3.1：嵌套序列音频组合预检——此前该组合在任务运行时才失败。

    非 strict 展开收集结构化 diagnostics：父 retime / 混合序列 fps /
    深度 / 环 / resolver 缺失等渲染期必然阻断的 code → BLOCKING；子音轨
    转场丢弃（渲染可完成但有损）→ WARNING。include_audio=False 的纯视频
    任务完全跳过（与渲染门一致）。
    """
    if not getattr(job, "include_audio", True):
        return
    sequence = ctx.sequence
    resolver = ctx.sequence_resolver
    project = ctx.project
    if sequence is None or resolver is None or project is None:
        return
    from src.features.edit.core.nested_sequence import flatten_nested_audio_tracks

    assets = getattr(project, "assets", None) or {}
    diagnostics: list = []
    try:
        flatten_nested_audio_tracks(
            sequence, resolver,
            lambda aid: assets.get(aid),
            diagnostics=diagnostics,
        )
    except Exception as exc:  # noqa: BLE001 — 展开自身异常按 BLOCKING 呈现
        result.add_blocking(
            "nested_audio.expansion_failed",
            f"嵌套音频展开失败：{exc}", field="include_audio")
        return
    for issue in diagnostics:
        code = f"nested_audio.{issue.code}"
        message = f"{issue.code}：{issue.message}（片段 {issue.clip_id}）"
        if issue.code in _NESTED_AUDIO_WARNING_CODES:
            result.add_warning(code, message, field="include_audio")
        else:
            result.add_blocking(code, message, field="include_audio")


def _check_source_patch(
    job: VideoExportJob, ctx: PreflightContext, result: PreflightResult
) -> None:
    """F2.1：素材级 Source Patch 声道映射预检。

    映射越界（素材已知声道数时）入队前即拒绝——与
    ``UpdateSourcePatchCommand.apply`` 同一谓词，避免任务运行才失败。
    素材声道数未知（无探测元数据）不判越界：渲染期解码布局与映射
    基线不符时回退直通并留警告（渲染侧已收口语义）。纯音频任务外，
    纯视频任务（include_audio=False）完全跳过。
    """
    if not getattr(job, "include_audio", True):
        return
    project = ctx.project
    sequence = ctx.sequence
    if project is None or sequence is None:
        return
    overrides = getattr(project, "source_patch_overrides", None) or {}
    if not overrides:
        return
    from src.features.edit.core.source_patch import patch_for_asset

    assets = getattr(project, "assets", None) or {}
    seen: set = set()
    for track in sequence.audio_tracks:
        for clip in track.clips:
            if clip.asset_id in seen:
                continue
            seen.add(clip.asset_id)
            asset = assets.get(clip.asset_id)
            if asset is None:
                continue
            spec = patch_for_asset(overrides, clip.asset_id)
            if not spec.audio_channel_map:
                continue
            n_channels = getattr(asset, "audio_channels", None)
            if not n_channels:
                continue
            try:
                spec.validate(int(n_channels))
            except ValueError as exc:
                result.add_blocking(
                    "source_patch.invalid_map",
                    f"素材 {asset.display_name} 的源声道映射无效：{exc}",
                    field="include_audio",
                )


_AUDIO_CODEC_CONTAINER_TABLE: dict[str, set[str]] = {
    "aac": {"mp4", "mov", "mkv", "webm"},
    "mp3": {"mp4", "mov", "mkv"},
    "pcm_s16le": {"mov", "mkv", "avi"},
    "pcm_s24le": {"mov", "mkv", "mxf"},
    "pcm_s32le": {"mov", "mkv", "mxf"},
    "pcm_f32le": {"mov", "mkv"},
    "flac": {"mkv", "mov"},
    "opus": {"webm", "mkv"},
}


def _check_codec_container(codec: str, container: str) -> Tuple[bool, Optional[str]]:
    reason = codec_container_error(codec, container)
    if reason is None:
        return True, None
    return False, reason


def _check_audio_codec(audio_codec: str, container: str) -> Tuple[bool, Optional[str]]:
    allowed = _AUDIO_CODEC_CONTAINER_TABLE.get(audio_codec)
    if allowed is None:
        return False, f"Unknown audio codec “{audio_codec}”."
    if container not in allowed:
        return False, (
            f"Audio codec “{audio_codec}” is not supported in “{container}”. "
            f"Consider {', '.join(sorted(allowed))}."
        )
    return True, None


# ── formatting helpers ──


def _format_bytes(n: int) -> str:
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024.0 or unit == "TB":
            return f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} TB"


def _format_disk_message(estimate: int, free: int, severity_label: str) -> str:
    return (
        f"Estimated output {_format_bytes(estimate)}; "
        f"free space on target volume is {_format_bytes(free)} ({severity_label})."
    )
