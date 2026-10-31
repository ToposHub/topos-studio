"""Video export job snapshot — the typed model for a single queued video task.

This module defines a *video-specific* task snapshot (``VideoExportJob``) that
captures everything needed to reproduce a timeline export independently of the
UI controls that created it. The snapshot is designed to be:

* **JSON-friendly** — round-trips through the existing ``ExportQueue``
  persistence (``~/.topos/deliver_export_queue.json``) without holding any Qt
  object reference.
* **Typed** — exposes nested :class:`VideoConfig` / :class:`AudioConfig` /
  :class:`SubtitleConfig` dataclasses instead of the flat
  :class:`~src.features.edit.core.timeline_export.TimelineExportConfig`.
* **Version-tolerant** — carries ``schema_version`` and preserves unknown
  fields so future revisions stay loadable.
* **Decoupled** — does *not* import the edit feature at module load time. The
  conversion to :class:`TimelineExportConfig` happens lazily inside
  :meth:`VideoExportJob.to_timeline_export_config` and only when the edit
  feature is available.

The bridge to the legacy :class:`ExportJob` (the queue's internal dataclass) is
provided through :meth:`to_legacy_job` / :meth:`from_legacy_job` so the
existing ``ExportQueue`` state machine, signals and persistence can be reused
verbatim.
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

if TYPE_CHECKING:  # pragma: no cover - annotation-only import
    from src.features.deliver.core.export_queue import ExportJob

logger = logging.getLogger(__name__)

__all__ = [
    "VideoJobStatus",
    "OverwritePolicy",
    "RangeType",
    "SourceKind",
    "RenderMode",
    "FilenameUse",
    "VideoConfig",
    "AudioConfig",
    "StemTrackConfig",
    "SubtitleConfig",
    "NamingConfig",
    "SubtitleMode",
    "VideoExportJob",
    "VIDEO_EXPORT_JOB_SCHEMA_VERSION",
]


# ── Schema version ──

#: Bumped whenever the on-disk shape changes in a backwards-incompatible way.
#: Older schemas are still loadable (unknown fields preserved); newer fields
#: simply default to safe values when absent.
VIDEO_EXPORT_JOB_SCHEMA_VERSION: int = 4


# ── Status constants ──
#
# These mirror the legacy ``JobStatus`` in ``export_queue.py`` (same string
# values) so the two layers can talk without translation. A dedicated
# ``NEEDS_RELINK`` value is added for the video-specific recovery state
# described in the plan (timeline missing after restart).


class VideoJobStatus:
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"
    PAUSED = "paused"
    #: Recovery state — the referenced timeline no longer exists (or the
    #: project revision changed in a way that requires explicit user relink).
    #: Such jobs never auto-run; the UI must prompt the user.
    NEEDS_RELINK = "needs_relink"


#: Jobs that should be restored from disk on startup (the rest are terminal).
RESTORABLE_VIDEO_STATUSES: Tuple[str, ...] = (
    VideoJobStatus.PENDING,
    VideoJobStatus.RUNNING,
    VideoJobStatus.FAILED,
    VideoJobStatus.NEEDS_RELINK,
)


class OverwritePolicy:
    ASK = "ask"
    OVERWRITE = "overwrite"
    AUTO_NUMBER = "auto_number"
    SKIP = "skip"


class RangeType:
    ENTIRE = "entire"
    IN_OUT = "in_out"
    SELECTED = "selected"
    CUSTOM = "custom"


class SourceKind:
    TIMELINE = "timeline"
    COMPOSITE = "composite"
    COLOR_IMAGE = "color_image"


class RenderMode:
    """How a timeline is delivered.

    These values deliberately mirror DaVinci Resolve's Deliver-page wording:
    one complete timeline file, or one file for every timeline video clip.
    """

    SINGLE_CLIP = "single_clip"
    INDIVIDUAL_CLIPS = "individual_clips"


class FilenameUse:
    """The base name source selected in the File inspector."""

    CUSTOM = "custom"
    TIMELINE = "timeline"
    SOURCE = "source"


class SubtitleMode:
    OFF = "off"
    BURN = "burn"
    SIDECAR = "sidecar"


# ── Nested config dataclasses ──
#
# Each is a plain dataclass (no Qt, no enums from the edit feature) so the
# whole job can be serialised with dataclasses.asdict / json.dump. The
# mapping to real enums happens in ``VideoExportJob.to_timeline_export_config``.


@dataclass
class VideoConfig:
    """Video encoder configuration snapshot."""

    container: str = "mp4"
    codec: str = "h264"
    profile: Optional[str] = None
    resolution: Optional[Tuple[int, int]] = None
    fps: Optional[float] = None
    quality_mode: str = "auto"  # "auto" | "vbr" | "cbr" | "crf"
    quality_value: Optional[int] = None  # CRF or target bitrate in bps
    gif_color_count: int = 256  # GIF palette entries (2..256)
    pixel_format: str = "yuv420p"
    color_space: str = "rec709"
    bit_depth: int = 8
    #: R17（第七轮审计）：PNG/TIFF 序列帧号 padding（与 individual 剪辑
    #: 的 unique_filename_digits 去重语义不同，独立配置）
    image_sequence_digits: int = 6
    #: 序列首帧号（如从 1001 起）
    image_sequence_start_index: int = 0
    #: R15：同名序列覆盖授权（默认 False——同名旧帧冲突即失败，保护
    #: 既有文件；True 时成功提交才替换并清理旧尾帧）
    image_sequence_overwrite: bool = False
    alpha: bool = False
    #: R7：Topos Alpha 预算超硬上限交付策略（record/error/continue；
    #: 仅 Topos 路径消费，非 Topos codec 忽略——ADR-C014 C-114 UI 授权开关）
    alpha_budget_policy: str = "record"
    #: Extra encoder hints that don't deserve first-class fields yet
    #: (e.g. ``{"gop_size": 60}``). Preserved across round-trips.
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        d = {
            "container": self.container,
            "codec": self.codec,
            "profile": self.profile,
            "resolution": list(self.resolution) if self.resolution else None,
            "fps": self.fps,
            "quality_mode": self.quality_mode,
            "quality_value": self.quality_value,
            "gif_color_count": self.gif_color_count,
            "pixel_format": self.pixel_format,
            "color_space": self.color_space,
            "bit_depth": self.bit_depth,
            "image_sequence_digits": self.image_sequence_digits,
            "image_sequence_start_index": self.image_sequence_start_index,
            "image_sequence_overwrite": self.image_sequence_overwrite,
            "alpha": self.alpha,
            "alpha_budget_policy": self.alpha_budget_policy,
            "extra": dict(self.extra),
        }
        return d

    @staticmethod
    def from_dict(data: Dict[str, Any]) -> "VideoConfig":
        resolution = data.get("resolution")
        if isinstance(resolution, list):
            resolution = (int(resolution[0]), int(resolution[1])) if len(resolution) == 2 else None
        return VideoConfig(
            container=data.get("container", "mp4"),
            codec=data.get("codec", "h264"),
            profile=data.get("profile"),
            resolution=resolution,
            fps=data.get("fps"),
            quality_mode=data.get("quality_mode", "auto"),
            quality_value=data.get("quality_value"),
            gif_color_count=int(data.get("gif_color_count", 256)),
            pixel_format=data.get("pixel_format", "yuv420p"),
            color_space=data.get("color_space", "rec709"),
            bit_depth=int(data.get("bit_depth", 8)),
            image_sequence_digits=int(data.get("image_sequence_digits", 6) or 6),
            image_sequence_start_index=int(
                data.get("image_sequence_start_index", 0) or 0),
            image_sequence_overwrite=bool(
                data.get("image_sequence_overwrite", False)),
            alpha=bool(data.get("alpha", False)),
            alpha_budget_policy=str(data.get("alpha_budget_policy", "record")),
            extra=dict(data.get("extra", {})),
        )


@dataclass(frozen=True)
class StemTrackConfig:
    """容器追加音轨（stems 角色轨）声明（v1.6 M-B8，ADR-C054 §4）。

    交付作业边界的轻量快照（字符串字段，同 NamingConfig 惯例）；
    to_timeline_export_config 转为 AudioStemExportTrack。仅 topos MOV
    容器消费；其它编码器由导出侧整体降级单轨并记录警告。
    """

    stem_group: str = ""      # EditTrack.stem_group 标签（对白/音乐/…）
    name: str = ""            # 容器轨名（©nam）；空 = stem_group 首字母大写
    codec: str = "pcm_s24le"  # aac / pcm_s16le / pcm_s24le / pcm_s32le / pcm_f32le
    channels: int = 2         # 1/2/6/8（1 = mono 对白轨惯例）
    channel_layout: str = ""  # 显式布局名；空回落 channels
    bitrate: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "stem_group": self.stem_group,
            "name": self.name,
            "codec": self.codec,
            "channels": self.channels,
            "channel_layout": self.channel_layout,
            "bitrate": self.bitrate,
        }

    @staticmethod
    def from_dict(data: Dict[str, Any]) -> "StemTrackConfig":
        return StemTrackConfig(
            stem_group=str(data.get("stem_group", "") or ""),
            name=str(data.get("name", "") or ""),
            codec=str(data.get("codec", "pcm_s24le") or "pcm_s24le"),
            channels=int(data.get("channels", 2)),
            channel_layout=str(data.get("channel_layout", "") or ""),
            bitrate=(
                int(data["bitrate"]) if data.get("bitrate") is not None else None),
        )


@dataclass
class AudioConfig:
    """Audio encoder configuration snapshot."""

    include_audio: bool = True
    codec: str = "aac"
    sample_rate: int = 48000
    bitrate: int = 192  # kbps
    channels: int = 2
    # v1.6 M-B8：stems 追加音轨（空 = 单音轨行为不变）
    stems: Tuple[StemTrackConfig, ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "include_audio": self.include_audio,
            "codec": self.codec,
            "sample_rate": self.sample_rate,
            "bitrate": self.bitrate,
            "channels": self.channels,
            "stems": [s.to_dict() for s in self.stems],
        }

    @staticmethod
    def from_dict(data: Dict[str, Any]) -> "AudioConfig":
        return AudioConfig(
            include_audio=bool(data.get("include_audio", True)),
            codec=data.get("codec", "aac"),
            sample_rate=int(data.get("sample_rate", 48000)),
            bitrate=int(data.get("bitrate", 192)),
            channels=int(data.get("channels", 2)),
            stems=tuple(
                StemTrackConfig.from_dict(s)
                for s in (data.get("stems", ()) or ())
            ),
        )


@dataclass
class SubtitleConfig:
    """Subtitle delivery configuration snapshot."""

    mode: str = SubtitleMode.OFF
    track_id: Optional[str] = None
    # M10.3：多版本编译声明的字幕语言（BCP-47 风格标签；空 = 未声明）。
    # version_compiler 写入 config["subtitle_language"]，经 from_flat_config
    # 进入类型化快照随队列持久化——消费侧（srt 命名/语言标签/校验）
    # 从本字段读取，不再散读 flat config。
    language: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {"mode": self.mode, "track_id": self.track_id,
                "language": self.language}

    @staticmethod
    def from_dict(data: Dict[str, Any]) -> "SubtitleConfig":
        return SubtitleConfig(
            mode=data.get("mode", SubtitleMode.OFF),
            track_id=data.get("track_id"),
            language=str(data.get("language", "") or ""),
        )


@dataclass
class NamingConfig:
    """Resolve-style file naming snapshot.

    ``TIMELINE`` is meaningful for a single clip, ``SOURCE`` for individual
    clips.  The renderer accepts all values defensively so older saved jobs
    keep working when their render mode is changed later.
    """

    render_mode: str = RenderMode.SINGLE_CLIP
    filename_use: str = FilenameUse.TIMELINE
    custom_name: str = ""
    file_suffix: str = ""
    unique_filenames: bool = True
    unique_filename_position: str = "suffix"  # "prefix" | "suffix"
    unique_filename_digits: int = 4
    add_source_frame_count: bool = False
    frame_handles: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "render_mode": self.render_mode,
            "filename_use": self.filename_use,
            "custom_name": self.custom_name,
            "file_suffix": self.file_suffix,
            "unique_filenames": self.unique_filenames,
            "unique_filename_position": self.unique_filename_position,
            "unique_filename_digits": self.unique_filename_digits,
            "add_source_frame_count": self.add_source_frame_count,
            "frame_handles": self.frame_handles,
        }

    @staticmethod
    def from_dict(data: Dict[str, Any]) -> "NamingConfig":
        return NamingConfig(
            render_mode=data.get("render_mode", RenderMode.SINGLE_CLIP),
            filename_use=data.get("filename_use", FilenameUse.TIMELINE),
            custom_name=str(data.get("custom_name", "")),
            file_suffix=str(data.get("file_suffix", "")),
            unique_filenames=bool(data.get("unique_filenames", True)),
            unique_filename_position=data.get("unique_filename_position", "suffix"),
            unique_filename_digits=int(data.get("unique_filename_digits", 4)),
            add_source_frame_count=bool(data.get("add_source_frame_count", False)),
            frame_handles=max(0, int(data.get("frame_handles", 0))),
        )


# ── Main job dataclass ──


@dataclass
class VideoExportJob:
    """A single video delivery task snapshot.

    The job is a self-contained description of one video output: which
    timeline, which frame range, which encoder settings, where to write the
    file. Once enqueued it must *not* be mutated implicitly by the UI — the
    settings panel edits a live draft, the queue holds immutable snapshots.

    Attributes:
        job_id: stable unique id (uuid4 hex). Used as the queue key.
        schema_version: on-disk schema version this job was written with.
        project_id: stable project identifier (for relink checks).
        project_name: human-readable project name captured for queue display.
        project_revision: project revision at enqueue time. Used to detect
            edits that may invalidate the snapshot.
        timeline_id: the timeline to export.
        timeline_name: human-readable timeline name (captured for display
            even after the project is closed).
        source_kind: what to render (timeline / composite / color image).
        range_type: frame-range selector.
        start_frame / end_frame: explicit range (only when range_type ==
            ``custom``; ``end_frame = -1`` means "to end").
        preset_id: optional preset id that produced this job (for labelling).
        video_config / audio_config / subtitle_config / naming_config: typed
            nested configs.
        output_path: destination file or directory (image sequences).
        overwrite_policy: how to handle existing output files.
        status: current :class:`VideoJobStatus`.
        progress: 0.0..1.0 (only meaningful while RUNNING).
        export_fps: measured encoded frames per second for the current/last run.
        error: error message if FAILED.
        retry_count: number of times this job has been retried.
        created_at / started_at / finished_at: epoch seconds.
        name: human-readable label (usually the output filename).
    """

    # Identity & provenance
    job_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    schema_version: int = VIDEO_EXPORT_JOB_SCHEMA_VERSION
    project_id: str = ""
    project_name: str = ""
    project_revision: int = 0
    timeline_id: str = ""
    timeline_name: str = ""
    source_kind: str = SourceKind.TIMELINE

    # Range
    range_type: str = RangeType.ENTIRE
    start_frame: int = 0
    end_frame: int = -1

    # Preset linkage (informational — presets don't change the snapshot)
    preset_id: str = ""

    # Typed configs
    video_config: VideoConfig = field(default_factory=VideoConfig)
    audio_config: AudioConfig = field(default_factory=AudioConfig)
    subtitle_config: SubtitleConfig = field(default_factory=SubtitleConfig)
    naming_config: NamingConfig = field(default_factory=NamingConfig)

    # Output
    output_path: str = ""
    overwrite_policy: str = OverwritePolicy.ASK
    # B7: 显式允许含占位片段的时间线导出（preflight 默认 blocking；
    # 放行后降级为 warning，占位片段按彩条渲染进成片）。
    allow_placeholder_clips: bool = False

    # Runtime state
    status: str = VideoJobStatus.PENDING
    progress: float = 0.0
    export_fps: float = 0.0
    error: str = ""
    retry_count: int = 0

    # Timestamps (epoch)
    created_at: float = field(default_factory=time.time)
    started_at: float = 0.0
    finished_at: float = 0.0

    # Display
    name: str = ""

    # Preserve unknown fields from future/newer schema versions so we never
    # silently drop data during a round-trip.
    _unknown_fields: Dict[str, Any] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        if not self.job_id:
            # Fall back to a fresh id rather than raising — jobs loaded from
            # a corrupted queue file shouldn't take the whole app down.
            self.job_id = str(uuid.uuid4())
            logger.warning("VideoExportJob created without job_id; generated %s", self.job_id)
        if not self.name:
            self.name = self._derive_default_name()

    # ── helpers ──

    def _derive_default_name(self) -> str:
        import os

        if self.output_path:
            return os.path.basename(self.output_path) or self.timeline_name or self.job_id[:8]
        return self.timeline_name or self.job_id[:8]

    @property
    def is_restorable(self) -> bool:
        return self.status in RESTORABLE_VIDEO_STATUSES

    @property
    def is_terminal(self) -> bool:
        return self.status in (VideoJobStatus.DONE, VideoJobStatus.FAILED, VideoJobStatus.CANCELLED)

    @property
    def is_running(self) -> bool:
        return self.status == VideoJobStatus.RUNNING

    def estimated_frame_count(self, default_total: int = 0) -> int:
        """Best-effort frame count from the range fields.

        Returns ``default_total`` when the range is open-ended (``end = -1``)
        or invalid; the caller (typically the runner) should supply the real
        total from the sequence.
        """
        if self.range_type == RangeType.ENTIRE or self.range_type == RangeType.IN_OUT:
            return default_total
        if self.range_type == RangeType.CUSTOM and self.end_frame >= 0:
            return max(0, self.end_frame - self.start_frame + 1)
        return default_total

    # ── validation ──

    def validate(self) -> List[str]:
        """Return a list of human-readable validation errors (empty == ok).

        This performs structural validation only — it does not check the
        timeline existence or codec backend availability (that is the job of
        the preflight module). It is safe to call without the edit feature.
        """
        errors: List[str] = []

        if self.source_kind == SourceKind.TIMELINE and not self.timeline_id:
            errors.append("Timeline source requires a timeline_id")

        if self.range_type == RangeType.CUSTOM:
            if self.end_frame >= 0 and self.end_frame < self.start_frame:
                errors.append("end_frame must be >= start_frame")

        if self.video_config.resolution is not None:
            w, h = self.video_config.resolution
            if w <= 0 or h <= 0:
                errors.append("Invalid video resolution")

        if self.video_config.fps is not None and self.video_config.fps <= 0:
            errors.append("fps must be positive")

        if self.video_config.codec == "gif":
            if not 2 <= int(self.video_config.gif_color_count) <= 256:
                errors.append("GIF color count must be between 2 and 256")
            if self.video_config.fps is not None and self.video_config.fps > 100:
                errors.append("GIF frame rate cannot exceed 100 fps")

        if not self.output_path:
            errors.append("output_path is required")
        else:
            errors.extend(self._validate_output_extension())

        if self.overwrite_policy not in (
            OverwritePolicy.ASK,
            OverwritePolicy.OVERWRITE,
            OverwritePolicy.AUTO_NUMBER,
            OverwritePolicy.SKIP,
        ):
            errors.append(f"Unknown overwrite_policy: {self.overwrite_policy}")

        if self.subtitle_config.mode not in (
            SubtitleMode.OFF,
            SubtitleMode.BURN,
            SubtitleMode.SIDECAR,
        ):
            errors.append(f"Unknown subtitle mode: {self.subtitle_config.mode}")

        if self.naming_config.render_mode not in (
            RenderMode.SINGLE_CLIP,
            RenderMode.INDIVIDUAL_CLIPS,
        ):
            errors.append(f"Unknown render mode: {self.naming_config.render_mode}")
        if self.naming_config.filename_use not in (
            FilenameUse.CUSTOM,
            FilenameUse.TIMELINE,
            FilenameUse.SOURCE,
        ):
            errors.append(f"Unknown filename use: {self.naming_config.filename_use}")
        if self.naming_config.unique_filename_position not in ("prefix", "suffix"):
            errors.append("unique_filename_position must be 'prefix' or 'suffix'")
        if not 2 <= self.naming_config.unique_filename_digits <= 10:
            errors.append("unique_filename_digits must be between 2 and 10")
        if self.naming_config.frame_handles < 0:
            errors.append("frame_handles cannot be negative")

        return errors

    def _validate_output_extension(self) -> List[str]:
        import os

        ext = os.path.splitext(self.output_path)[1].lower()
        codec = self.video_config.codec

        # Image sequences can target a directory or carry a frame extension.
        if codec in ("png_sequence", "tiff_sequence", "topos_image_sequence"):
            if ext not in ("", ".png", ".tiff", ".tif") and not os.path.isdir(self.output_path):
                return ["Image sequence output requires a directory or image extension"]
            return []

        video_extensions = (".mov", ".mp4", ".mkv", ".avi", ".webm", ".mxf", ".gif")
        if ext not in video_extensions:
            return [f"Video codec '{codec}' requires a video file extension (got '{ext}')"]
        if codec == "gif" and ext != ".gif":
            return ["GIF codec requires a .gif output extension"]
        return []

    # ── serialisation ──

    def to_dict(self) -> Dict[str, Any]:
        """Serialise to a JSON-friendly dict (no Qt / no enum references)."""
        return {
            "schema_version": self.schema_version,
            "job_id": self.job_id,
            "project_id": self.project_id,
            "project_name": self.project_name,
            "project_revision": self.project_revision,
            "timeline_id": self.timeline_id,
            "timeline_name": self.timeline_name,
            "source_kind": self.source_kind,
            "range_type": self.range_type,
            "start_frame": self.start_frame,
            "end_frame": self.end_frame,
            "preset_id": self.preset_id,
            "video_config": self.video_config.to_dict(),
            "audio_config": self.audio_config.to_dict(),
            "subtitle_config": self.subtitle_config.to_dict(),
            "naming_config": self.naming_config.to_dict(),
            "output_path": self.output_path,
            "overwrite_policy": self.overwrite_policy,
            "allow_placeholder_clips": self.allow_placeholder_clips,
            "status": self.status,
            "progress": self.progress,
            "export_fps": self.export_fps,
            "error": self.error,
            "retry_count": self.retry_count,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "name": self.name,
            # Stash unknown fields so a round-trip through an older build
            # doesn't strip data written by a newer build.
            "unknown_fields": dict(self._unknown_fields),
        }

    @staticmethod
    def from_dict(data: Dict[str, Any]) -> "VideoExportJob":
        """Deserialise, tolerating missing / extra keys (version-tolerant)."""
        known_keys = {
            "schema_version", "job_id", "project_id", "project_name", "project_revision",
            "timeline_id", "timeline_name", "source_kind", "range_type",
            "start_frame", "end_frame", "preset_id", "video_config",
            "audio_config", "subtitle_config", "naming_config", "output_path",
            "overwrite_policy", "allow_placeholder_clips",
            "status", "progress", "error", "retry_count",
            "export_fps",
            "created_at", "started_at", "finished_at", "name", "unknown_fields",
        }
        unknown = {k: v for k, v in data.items() if k not in known_keys}

        # Be defensive about stale RUNNING state: a process that crashed
        # mid-export leaves a job marked RUNNING on disk. Demote to PENDING
        # so the queue can pick it up again (or to NEEDS_RELINK if the
        # caller decides the timeline is gone — handled separately).
        status = data.get("status", VideoJobStatus.PENDING)
        progress = float(data.get("progress", 0.0))
        if status == VideoJobStatus.RUNNING:
            status = VideoJobStatus.PENDING
            progress = 0.0

        return VideoExportJob(
            schema_version=int(data.get("schema_version", VIDEO_EXPORT_JOB_SCHEMA_VERSION)),
            job_id=data.get("job_id") or str(uuid.uuid4()),
            project_id=data.get("project_id", ""),
            project_name=data.get("project_name", ""),
            project_revision=int(data.get("project_revision", 0)),
            timeline_id=data.get("timeline_id", ""),
            timeline_name=data.get("timeline_name", ""),
            source_kind=data.get("source_kind", SourceKind.TIMELINE),
            range_type=data.get("range_type", RangeType.ENTIRE),
            start_frame=int(data.get("start_frame", 0)),
            end_frame=int(data.get("end_frame", -1)),
            preset_id=data.get("preset_id", ""),
            video_config=VideoConfig.from_dict(data.get("video_config", {})),
            audio_config=AudioConfig.from_dict(data.get("audio_config", {})),
            subtitle_config=SubtitleConfig.from_dict(data.get("subtitle_config", {})),
            naming_config=NamingConfig.from_dict(data.get("naming_config", {})),
            output_path=data.get("output_path", ""),
            overwrite_policy=data.get("overwrite_policy", OverwritePolicy.ASK),
            allow_placeholder_clips=bool(data.get("allow_placeholder_clips", False)),
            status=status,
            progress=progress,
            export_fps=float(data.get("export_fps", 0.0)),
            error=data.get("error", ""),
            retry_count=int(data.get("retry_count", 0)),
            created_at=float(data.get("created_at", 0.0)),
            started_at=float(data.get("started_at", 0.0)),
            finished_at=float(data.get("finished_at", 0.0)),
            name=data.get("name", ""),
            _unknown_fields=unknown,
        )

    # ── bridge to legacy ExportJob ──
    #
    # The existing ExportQueue stores its own ExportJob dataclass. Rather than
    # fork the queue, we pack the typed VideoExportJob into the legacy job's
    # ``config`` dict (full snapshot) plus mirror the runtime fields. This
    # keeps a single source of truth on disk.

    def to_legacy_job(self) -> "ExportJob":  # type: ignore[name-defined]
        """Pack into a legacy :class:`ExportJob` for the existing queue.

        The full typed snapshot is stored under ``config["video_job"]`` so it
        can be recovered losslessly via :meth:`from_legacy_job`. Runtime
        fields (status / progress / timestamps) are mirrored so the legacy
        queue's state machine operates on consistent values.
        """
        from src.features.deliver.core.export_queue import ExportJob

        # The video status space and the queue status space now share the
        # same NEEDS_RELINK value, so we pass the status through verbatim.
        # The queue's _process_next only picks up PENDING jobs, so a
        # NEEDS_RELINK job will never auto-run.
        return ExportJob(
            job_id=self.job_id,
            name=self.name or self._derive_default_name(),
            config={
                "video_job": self.to_dict(),
                # Also surface a few convenience fields for tools that only
                # read the flat config (e.g. the old queue view).
                "timeline_name": self.timeline_name,
                "project_name": self.project_name,
                "preset_id": self.preset_id,
                "output_path": self.output_path,
            },
            output_path=self.output_path,
            status=self.status,
            progress=self.progress,
            export_fps=self.export_fps,
            error=self.error,
            retry_count=self.retry_count,
            created_at=self.created_at,
            started_at=self.started_at,
            finished_at=self.finished_at,
        )

    @staticmethod
    def from_legacy_job(job: "ExportJob") -> "VideoExportJob":  # type: ignore[name-defined]
        """Recover a typed job from a legacy :class:`ExportJob`.

        Jobs written by older builds (no ``video_job`` key) are synthesised
        from the flat ``config`` dict so we stay backward compatible with the
        pre-video-batch queue.
        """
        config = job.config or {}
        snapshot = config.get("video_job")
        if isinstance(snapshot, dict):
            recovered = VideoExportJob.from_dict(snapshot)
        else:
            # Legacy flat config (pre-video-batch). Best-effort recovery.
            recovered = VideoExportJob.from_flat_config(config)

        # Mirror live runtime fields back onto the typed view (the legacy
        # queue is the source of truth for these while running).
        recovered.job_id = job.job_id or recovered.job_id
        recovered.name = job.name or recovered.name
        recovered.output_path = job.output_path or recovered.output_path
        recovered.progress = job.progress
        recovered.export_fps = getattr(job, "export_fps", 0.0)
        recovered.error = job.error
        recovered.retry_count = job.retry_count
        recovered.created_at = job.created_at or recovered.created_at
        recovered.started_at = job.started_at
        recovered.finished_at = job.finished_at
        # The legacy queue's status is the runtime authority — always trust
        # it over the snapshot (which is only the enqueue-time snapshot).
        # This ensures retry / pause / cancel / completion transitions are
        # reflected correctly. The snapshot's status is only consulted when
        # the job is freshly loaded from disk (handled by the caller).
        recovered.status = job.status
        return recovered

    @staticmethod
    def from_flat_config(config: Dict[str, Any]) -> "VideoExportJob":
        """Build a job from a legacy flat config dict (no nested snapshot).

        This is the migration path for queues written before the video batch
        panel existed. We pull out whatever fields are available and default
        the rest.
        """
        return VideoExportJob(
            project_id=str(config.get("project_id", "")),
            project_name=str(config.get("project_name", "")),
            timeline_id=str(config.get("timeline_id", "")),
            timeline_name=str(config.get("timeline_name", "")),
            output_path=str(config.get("output_path", "")),
            video_config=VideoConfig(
                codec=str(config.get("codec", "h264")),
                container=str(config.get("container", "mp4")),
                resolution=config.get("resolution"),
                fps=config.get("fps"),
                quality_mode=str(config.get("quality_mode", "auto")),
                quality_value=config.get("quality"),
                gif_color_count=int(config.get("gif_color_count", 256)),
            ),
            range_type=str(config.get("range_type", RangeType.ENTIRE)),
            start_frame=int(config.get("start_frame", 0)),
            end_frame=int(config.get("end_frame", -1)),
            # M10.3：多版本编译声明的字幕语言随快照入队（消费者统一从
            # 类型化字段读取，不再散读 flat config）
            subtitle_config=SubtitleConfig(
                mode=str(config.get("subtitle_mode", SubtitleMode.OFF)),
                track_id=config.get("subtitle_track_id"),
                language=str(config.get("subtitle_language", "") or ""),
            ),
        )

    # ── bridge to TimelineExportConfig ──

    def to_timeline_export_config(self) -> Any:
        """Convert to a :class:`TimelineExportConfig` for the real exporter.

        Raises ``ImportError`` if the edit feature is unavailable, and
        ``ValueError`` if the snapshot references an unknown enum value (the
        caller should preflight this).
        """
        from src.features.edit.core.timeline_export import (
            TimelineExportConfig,
        )

        return TimelineExportConfig(
            source=_map_source_kind(self.source_kind),
            timeline_id=self.timeline_id or None,
            range_type=_map_range_type(self.range_type),
            start_frame=self.start_frame,
            end_frame=self.end_frame,
            resolution=self.video_config.resolution,
            fps=self.video_config.fps,
            codec=_map_codec(self.video_config.codec),
            codec_profile=self.video_config.profile,
            quality=self.video_config.quality_value,
            quality_mode=self.video_config.quality_mode,
            gif_color_count=int(self.video_config.gif_color_count),
            # R15/R17（第七轮审计）：图像序列作业配置贯通（位深/帧号
            # padding/首帧号/覆盖授权）——此前编码器只能用默认值。
            bit_depth=int(getattr(self.video_config, "bit_depth", 8) or 8),
            image_sequence_digits=int(
                getattr(self.video_config, "image_sequence_digits", 6) or 6),
            image_sequence_start_index=int(
                getattr(self.video_config, "image_sequence_start_index", 0)
                or 0),
            image_sequence_overwrite=bool(
                getattr(self.video_config, "image_sequence_overwrite", False)),
            # P8-Topos：Alpha 开关显式转发（此前只持久化不消费——Topos 等
            # 自带 Alpha 语义的 codec 依赖它）
            has_alpha=bool(self.video_config.alpha),
            # R7：Topos pix_fmt 显式覆盖（空 = 编码侧 codec_map 档位默认）。
            # 只在 Topos codec 且值在编码域内透传——防 VideoConfig 的
            # yuv420p 类默认值/非 Topos 残留闯入 Topos 编码域被拒
            pixel_format=(
                _topos_pixel_format_override(self.video_config)
                or _hevc_pixel_format_override(self.video_config)),
            # R7：Alpha 预算超限策略 UI 授权开关（ADR-C014 C-114）
            alpha_budget_policy=str(self.video_config.alpha_budget_policy
                                    or "record"),
            include_audio=self.audio_config.include_audio,
            audio_codec=self.audio_config.codec,
            audio_bitrate=self.audio_config.bitrate,
            audio_sample_rate=self.audio_config.sample_rate,
            audio_channels=self.audio_config.channels,
            # v1.6 M-B8：stems 追加音轨（交付快照 → 导出声明）
            audio_stems=tuple(
                _stem_track_to_export_config(s)
                for s in self.audio_config.stems
            ),
            output_path=self.output_path,
            burn_subtitles=self.subtitle_config.mode == SubtitleMode.BURN,
            render_mode=self.naming_config.render_mode,
            filename_use=self.naming_config.filename_use,
            custom_name=self.naming_config.custom_name,
            file_suffix=self.naming_config.file_suffix,
            unique_filenames=self.naming_config.unique_filenames,
            unique_filename_position=self.naming_config.unique_filename_position,
            unique_filename_digits=self.naming_config.unique_filename_digits,
            add_source_frame_count=self.naming_config.add_source_frame_count,
            frame_handles=self.naming_config.frame_handles,
        )


# ── enum mapping helpers ──
#
# These translate our decoupled string constants into the real edit-feature
# enums. They live at module scope so they can be unit-tested in isolation
# and so the mapping table is in one place.


def _stem_track_to_export_config(stem: Any) -> Any:
    """StemTrackConfig → edit 域 AudioStemExportTrack（v1.6 M-B8）。

    延迟导入（edit 域模块较重）；非 StemTrackConfig 输入原样返回
    （容错历史作业数据）。
    """
    if not isinstance(stem, StemTrackConfig):
        return stem
    from src.features.edit.core.timeline_export import AudioStemExportTrack

    return AudioStemExportTrack(
        stem_group=stem.stem_group,
        name=stem.name,
        codec=stem.codec,
        channels=stem.channels,
        channel_layout=stem.channel_layout,
        bitrate=stem.bitrate,
    )


def _map_source_kind(source_kind: str) -> Any:
    from src.features.edit.core.timeline_export import ExportSource

    mapping = {
        SourceKind.TIMELINE: ExportSource.TIMELINE,
        SourceKind.COLOR_IMAGE: ExportSource.COLOR_IMAGE,
        SourceKind.COMPOSITE: ExportSource.COMPOSITE_OUTPUT,
    }
    if source_kind not in mapping:
        raise ValueError(f"Unknown source_kind: {source_kind!r}")
    return mapping[source_kind]


def _map_range_type(range_type: str) -> Any:
    from src.features.edit.core.timeline_export import ExportRangeType

    mapping = {
        RangeType.ENTIRE: ExportRangeType.ENTIRE,
        RangeType.IN_OUT: ExportRangeType.IN_OUT,
        RangeType.SELECTED: ExportRangeType.SELECTED,
        RangeType.CUSTOM: ExportRangeType.CUSTOM,
    }
    if range_type not in mapping:
        raise ValueError(f"Unknown range_type: {range_type!r}")
    return mapping[range_type]


def _topos_pixel_format_override(video_config: "VideoConfig") -> str:
    """Topos codec 的 pix_fmt 覆盖值（域内）或空串。

    域判定委托 topos_encoder 单一解析器（O0/O1 决策）——此处不做
    第二张格式表；非 Topos codec 一律空串（编码侧走 codec_map 默认）。
    """
    codec = str(video_config.codec or "")
    if not codec.startswith("topos"):
        return ""
    from src.shared.export.topos_encoder import parse_topos_pix_fmt
    name = str(video_config.pixel_format or "")
    parsed = parse_topos_pix_fmt(name)
    if parsed is None:
        # 批 5（计划点名）：静默降级改显式告警——未知/默认 pix_fmt 回落
        # codec_map 默认格式必须有日志痕迹（用户显式选择的非法值走
        # export 前置校验报错；此处覆盖的是未选择默认值场景）。
        import logging
        logging.getLogger(__name__).warning(
            "topos export: pixel_format %r not in the Topos encoder domain; "
            "falling back to codec-map default (expected e.g. yuv422p10le / "
            "gbrp12le / topos_yuva422p10a10)", name)
        return ""
    return name


def _hevc_pixel_format_override(video_config: "VideoConfig") -> str:
    """HEVC 交付位深覆盖值（域内）或空串。

    HEVC 编码域只认 yuv420p（8-bit，2026-09-16 平面直通档）与
    yuv420p10le（10-bit）。deliver 的 VideoConfig 一直以 pixel_format
    携带交付位深（预设 hevc_uhd 即 yuv420p/profile main/bit_depth 8），
    此处在 HEVC 域内透传并显式拒绝域外值；profile 与位深的一致性由
    timeline_export._normalize_hevc_profile 兜底。非 HEVC codec 一律
    空串（编码侧走 codec_map 默认）。

    历史队列兼容：pixel_format 曾是 HEVC 不可编辑的占位默认值，显式
    profile="main10" 是 10-bit 交付的唯一信号——profile 语义优先。
    """
    codec = str(video_config.codec or "")
    if codec != "hevc":
        return ""
    if str(video_config.profile or "").strip().lower() == "main10":
        return "yuv420p10le"
    name = str(video_config.pixel_format or "").strip().lower()
    return name if name in ("yuv420p", "yuv420p10le") else ""


def _map_codec(codec: str) -> Any:
    from src.features.edit.core.timeline_export import ExportCodec

    mapping = {
        "h264": ExportCodec.H264,
        "hevc": ExportCodec.HEVC,
        "gif": ExportCodec.GIF,
        "prores_proxy": ExportCodec.PRORES_PROXY,
        "prores_lt": ExportCodec.PRORES_LT,
        "prores_422": ExportCodec.PRORES_422,
        "prores_422hq": ExportCodec.PRORES_422HQ,
        "prores_4444": ExportCodec.PRORES_4444,
        "prores_4444xq": ExportCodec.PRORES_4444XQ,
        "dnxhr_lb": ExportCodec.DNXHR_LB,
        "dnxhr_sq": ExportCodec.DNXHR_SQ,
        "dnxhr_hq": ExportCodec.DNXHR_HQ,
        "dnxhr_hqx": ExportCodec.DNXHR_HQX,
        "dnxhr_444": ExportCodec.DNXHR_444,
        "topos_proxy": ExportCodec.TOPOS_PROXY,
        "topos_lt": ExportCodec.TOPOS_LT,
        "topos": ExportCodec.TOPOS,
        "topos_hq": ExportCodec.TOPOS_HQ,
        "topos_4444": ExportCodec.TOPOS_4444,      # R4.6
        "topos_4444xq": ExportCodec.TOPOS_4444XQ,    # R4.6
        "topos_lp": ExportCodec.TOPOS_LP,          # v1.8（ADR-C056）帧间档
        "topos_netproxy": ExportCodec.TOPOS_NETPROXY,  # v1.9 P2 网络代理档
        "topos_raw": ExportCodec.TOPOS_RAW,        # M4-R8（ADR-C058）视频 RAW
        "png_sequence": ExportCodec.PNG_SEQUENCE,
        "tiff_sequence": ExportCodec.TIFF_SEQUENCE,
        "topos_image_sequence": ExportCodec.TOPOS_IMAGE_SEQUENCE,
    }
    if codec not in mapping:
        raise ValueError(f"Unknown codec: {codec!r}")
    return mapping[codec]
