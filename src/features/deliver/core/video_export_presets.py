"""Video export presets — canned codec/resolution/audio configurations.

Presets are the primary way users add tasks to the queue: pick a preset
card, optionally tweak a parameter, and "Add to queue". Each preset is a
pure data record that knows how to seed a :class:`VideoExportJob`. The UI
layer (the settings panel) reads the preset list to build its preset bar.

Design notes
============

* Presets carry **no project state** — they're templates that get combined
  with a timeline/range/output-dir at "add to queue" time.
* The default set is small and opinionated (Web / Mobile / Master / a few
  codec-specific ones). It is a module-level constant so it can be shown
  immediately at startup with no I/O.
* Custom presets are not persisted in this first phase; the plan calls for
  a "Custom" pseudo-preset that reflects the current settings panel state.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Tuple

from src.features.deliver.core.video_export_job import (
    AudioConfig,
    OverwritePolicy,
    RangeType,
    SourceKind,
    SubtitleConfig,
    SubtitleMode,
    VideoConfig,
    VideoExportJob,
)

__all__ = [
    "VideoPreset",
    "DEFAULT_VIDEO_PRESETS",
    "get_preset_by_id",
    "build_job_from_preset",
]


@dataclass(frozen=True)
class VideoPreset:
    """A single named video delivery preset.

    Attributes:
        preset_id: stable id (e.g. ``"web_h264"``). Used as the toolbar key.
        title_key: i18n key for the display name.
        fallback_title: English fallback when the i18n key is missing.
        subtitle_key: i18n key for the one-line description shown under the
            title on the preset card (e.g. "MP4 · 1080p · H.264").
        fallback_subtitle: English fallback subtitle.
        video_config: template :class:`VideoConfig` (resolution/fps may be
            ``None`` to mean "follow timeline"); its ``container`` always
            decides the output container.
        audio_config: template :class:`AudioConfig`.
        subtitle_config: template :class:`SubtitleConfig`.
        order: sort hint for the preset bar (smaller = earlier).
    """

    preset_id: str
    title_key: str
    fallback_title: str
    subtitle_key: str
    fallback_subtitle: str
    video_config: VideoConfig
    audio_config: AudioConfig = field(default_factory=AudioConfig)
    subtitle_config: SubtitleConfig = field(default_factory=lambda: SubtitleConfig(mode=SubtitleMode.OFF))
    order: int = 100


# ── Default preset catalogue ──

#: The built-in preset list shown on the preset bar. Order matters — it is
#: the left-to-right order on the card row.
DEFAULT_VIDEO_PRESETS: Tuple[VideoPreset, ...] = (
    VideoPreset(
        preset_id="web_h264",
        title_key="deliver.preset.web_h264.title",
        fallback_title="Web H.264",
        subtitle_key="deliver.preset.web_h264.subtitle",
        fallback_subtitle="MP4 · H.264 · 1080p",
        video_config=VideoConfig(
            container="mp4",
            codec="h264",
            profile="high",
            resolution=(1920, 1080),
            fps=None,
            quality_mode="vbr",
            quality_value=12_000_000,
            pixel_format="yuv420p",
            color_space="rec709",
            bit_depth=8,
        ),
        audio_config=AudioConfig(include_audio=True, codec="aac", sample_rate=48000, bitrate=192, channels=2),
        subtitle_config=SubtitleConfig(mode=SubtitleMode.OFF),
        order=10,
    ),
    VideoPreset(
        preset_id="mobile_h264",
        title_key="deliver.preset.mobile_h264.title",
        fallback_title="Mobile",
        subtitle_key="deliver.preset.mobile_h264.subtitle",
        fallback_subtitle="MP4 · H.264 · 1080×1920",
        video_config=VideoConfig(
            container="mp4",
            codec="h264",
            profile="high",
            resolution=(1080, 1920),
            fps=None,
            quality_mode="vbr",
            quality_value=12_000_000,
            pixel_format="yuv420p",
            color_space="rec709",
            bit_depth=8,
        ),
        audio_config=AudioConfig(include_audio=True, codec="aac", sample_rate=48000, bitrate=128, channels=2),
        subtitle_config=SubtitleConfig(mode=SubtitleMode.OFF),
        order=20,
    ),
    VideoPreset(
        preset_id="master_prores422",
        title_key="deliver.preset.master_prores422.title",
        fallback_title="Master",
        subtitle_key="deliver.preset.master_prores422.subtitle",
        fallback_subtitle="MOV · ProRes 422 · UHD",
        video_config=VideoConfig(
            container="mov",
            codec="prores_422",
            profile=None,
            resolution=None,  # follow timeline (master keeps resolution)
            fps=None,
            quality_mode="auto",
            quality_value=None,
            pixel_format="yuv422p10le",
            color_space="rec709",
            bit_depth=10,
        ),
        audio_config=AudioConfig(include_audio=True, codec="pcm_s16le", sample_rate=48000, bitrate=1536, channels=2),
        subtitle_config=SubtitleConfig(mode=SubtitleMode.OFF),
        order=30,
    ),
    VideoPreset(
        preset_id="master_prores4444",
        title_key="deliver.preset.master_prores4444.title",
        fallback_title="Master (Alpha)",
        subtitle_key="deliver.preset.master_prores4444.subtitle",
        fallback_subtitle="MOV · ProRes 4444 · 4:4:4",
        video_config=VideoConfig(
            container="mov",
            codec="prores_4444",
            profile=None,
            resolution=None,
            fps=None,
            quality_mode="auto",
            quality_value=None,
            pixel_format="yuv444p10le",
            color_space="rec709",
            bit_depth=10,
            alpha=True,
        ),
        audio_config=AudioConfig(include_audio=True, codec="pcm_s24le", sample_rate=48000, bitrate=2304, channels=2),
        subtitle_config=SubtitleConfig(mode=SubtitleMode.OFF),
        order=40,
    ),
    VideoPreset(
        preset_id="hevc_uhd",
        title_key="deliver.preset.hevc_uhd.title",
        fallback_title="HEVC 4K",
        subtitle_key="deliver.preset.hevc_uhd.subtitle",
        fallback_subtitle="MP4 · HEVC · 3840×2160",
        video_config=VideoConfig(
            container="mp4",
            codec="hevc",
            profile="main",
            resolution=(3840, 2160),
            fps=None,
            quality_mode="vbr",
            quality_value=12_000_000,
            pixel_format="yuv420p",
            color_space="rec709",
            bit_depth=8,
        ),
        audio_config=AudioConfig(include_audio=True, codec="aac", sample_rate=48000, bitrate=192, channels=2),
        subtitle_config=SubtitleConfig(mode=SubtitleMode.OFF),
        order=50,
    ),
    VideoPreset(
        preset_id="dnxhr_sq",
        title_key="deliver.preset.dnxhr_sq.title",
        fallback_title="DNxHR SQ",
        subtitle_key="deliver.preset.dnxhr_sq.subtitle",
        fallback_subtitle="MOV · DNxHR SQ · 1080p",
        video_config=VideoConfig(
            container="mov",
            codec="dnxhr_sq",
            profile=None,
            resolution=(1920, 1080),
            fps=None,
            quality_mode="auto",
            quality_value=None,
            pixel_format="yuv422p",
            color_space="rec709",
            bit_depth=8,
        ),
        audio_config=AudioConfig(include_audio=True, codec="pcm_s16le", sample_rate=48000, bitrate=1536, channels=2),
        subtitle_config=SubtitleConfig(mode=SubtitleMode.OFF),
        order=60,
    ),
)


def get_preset_by_id(preset_id: str) -> Optional[VideoPreset]:
    """Look up a preset by id from the default catalogue."""
    for p in DEFAULT_VIDEO_PRESETS:
        if p.preset_id == preset_id:
            return p
    return None


# ── Job builder ──
#
# Combines a preset template with the live "context" the settings panel
# holds (timeline, range, output dir, filename template) to produce a fully
# specified :class:`VideoExportJob` ready for preflight + enqueue.


#: Supported filename-template variables. Anything else is left verbatim.
_TEMPLATE_VARIABLES = ("project", "timeline", "preset", "codec", "resolution", "fps", "date", "index")


def expand_filename_template(
    template: str,
    *,
    project_name: str = "",
    timeline_name: str = "",
    preset_id: str = "",
    codec: str = "",
    resolution: Optional[Tuple[int, int]] = None,
    fps: Optional[float] = None,
    index: int = 0,
    date: str = "",
) -> str:
    """Expand ``{var}`` placeholders in a filename template.

    Unknown placeholders are left intact (so future templates don't break).
    The result is a *basename only* — the caller joins it with the output
    directory and the container-derived extension.
    """
    import datetime
    import re

    if not template:
        template = "{project}_{timeline}_{preset}_{resolution}"

    res_str = ""
    if resolution is not None:
        res_str = f"{int(resolution[0])}x{int(resolution[1])}"
    elif timeline_name:
        # Fall back to "source" so the variable isn't empty for master presets
        res_str = "source"

    fps_str = f"{fps:g}" if fps else ""

    values = {
        "project": project_name or "project",
        "timeline": timeline_name or "timeline",
        "preset": preset_id or "preset",
        "codec": codec or "codec",
        "resolution": res_str,
        "fps": fps_str,
        "date": date or datetime.date.today().strftime("%Y%m%d"),
        "index": f"{index:03d}",
    }

    def _replace(match: "re.Match[str]") -> str:
        key = match.group(1)
        if key in values:
            # Sanitise: no path separators / illegal chars in filenames.
            v = values[key]
            return re.sub(r'[\\/:*?"<>|]', "_", v)
        return match.group(0)  # leave unknown placeholders intact

    expanded = re.sub(r"\{(\w+)\}", _replace, template)
    # AUD-12000: the template itself is user-editable text, so its literal
    # portion must obey the same "basename only" contract as the replaced
    # values — sanitise the *whole* result, otherwise ``../../x`` or an
    # absolute path in the template escapes the chosen output directory
    # when the caller does ``os.path.join(output_dir, filename)``.
    return re.sub(r'[\\/:*?"<>|]', "_", expanded)


def build_job_from_preset(
    preset: VideoPreset,
    *,
    project_id: str = "",
    project_name: str = "",
    project_revision: int = 0,
    timeline_id: str = "",
    timeline_name: str = "",
    timeline_frame_count: Optional[int] = None,
    range_type: str = RangeType.ENTIRE,
    start_frame: int = 0,
    end_frame: int = -1,
    output_dir: str = "",
    filename_template: str = "",
    overwrite_policy: str = OverwritePolicy.ASK,
    index: int = 0,
) -> VideoExportJob:
    """Combine a preset with live context to produce a ready-to-preflight job.

    The preset's :class:`VideoConfig` / :class:`AudioConfig` /
    :class:`SubtitleConfig` are copied (not shared) so the resulting job is
    fully independent and can be safely mutated.
    """
    import os
    import copy

    video_cfg = copy.deepcopy(preset.video_config)
    audio_cfg = copy.deepcopy(preset.audio_config)
    subtitle_cfg = copy.deepcopy(preset.subtitle_config)

    # Derive the output path from the template + container extension.
    base = expand_filename_template(
        filename_template,
        project_name=project_name,
        timeline_name=timeline_name,
        preset_id=preset.preset_id,
        codec=video_cfg.codec,
        resolution=video_cfg.resolution,
        fps=video_cfg.fps,
        index=index,
    )
    ext = _container_extension(video_cfg.container, video_cfg.codec)
    filename = base + ext
    output_path = os.path.join(output_dir, filename) if output_dir else filename

    return VideoExportJob(
        project_id=project_id,
        project_name=project_name,
        project_revision=project_revision,
        timeline_id=timeline_id,
        timeline_name=timeline_name,
        source_kind=SourceKind.TIMELINE,
        range_type=range_type,
        start_frame=start_frame,
        end_frame=end_frame,
        preset_id=preset.preset_id,
        video_config=video_cfg,
        audio_config=audio_cfg,
        subtitle_config=subtitle_cfg,
        output_path=output_path,
        overwrite_policy=overwrite_policy,
        name=filename,
    )


def _container_extension(container: str, codec: str) -> str:
    """Pick the conventional file extension for a container/codec pair."""
    if codec in ("png_sequence", "tiff_sequence", "topos_image_sequence"):
        return ""  # directory-style output
    container = container.lower()
    return {
        "mp4": ".mp4",
        "mov": ".mov",
        "mkv": ".mkv",
        "mxf": ".mxf",
        "avi": ".avi",
        "webm": ".webm",
        "gif": ".gif",
    }.get(container, ".mov")
