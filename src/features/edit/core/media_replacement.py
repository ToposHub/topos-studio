"""Pure preflight for replacing a source while retaining existing clip payloads."""
from __future__ import annotations

import copy
import math
from fractions import Fraction
from typing import Any, Iterable, Mapping

from src.features.edit.core.project import MediaAsset


# Fields derived from the old file must not survive when a new probe omits them.
# All other keys (logging, multicam and future feature payloads) remain intact.
_SOURCE_FIELDS = frozenset({
    "width", "height", "fps", "fps_num", "fps_den", "total_frames",
    "duration_frames", "duration_seconds", "codec", "codec_profile",
    "pixel_format", "bit_depth", "field_order", "timecode", "rotation",
    "has_alpha", "format", "file_size", "audio", "audio_info", "has_audio",
    "media_backend", "media_identity", "color_primaries", "color_transfer",
    "color_matrix", "color_range", "is_hdr", "camera", "lens", "iso",
    "shutter", "aperture", "date", "error", "pts_index", "source_pts_index",
})


def merge_replacement_metadata(
    previous: Mapping[str, Any], probed: Mapping[str, Any],
) -> dict[str, Any]:
    """Keep feature payloads, replace the entire source-derived metadata domain."""
    merged = copy.deepcopy({k: v for k, v in previous.items() if k not in _SOURCE_FIELDS})
    merged.update(copy.deepcopy(dict(probed)))
    merged.pop("error", None)
    return merged


def source_fps_from_metadata(metadata: Mapping[str, Any]) -> Fraction | None:
    num, den = metadata.get("fps_num"), metadata.get("fps_den")
    try:
        if num is not None or den is not None:
            if num is None or den is None or isinstance(num, bool) or isinstance(den, bool):
                raise ValueError("帧率分子/分母必须同时提供正整数")
            n, d = int(num), int(den)
            if Fraction(str(num)) != n or Fraction(str(den)) != d or n <= 0 or d <= 0:
                raise ValueError("帧率分子/分母必须是正整数")
            return Fraction(n, d)
        value = metadata.get("fps")
        if value is None:
            return None
        if isinstance(value, bool) or not math.isfinite(float(value)) or float(value) <= 0:
            raise ValueError("帧率必须是有限正数")
        return Fraction(str(float(value))).limit_denominator(100_000)
    except (TypeError, ValueError, ZeroDivisionError, OverflowError) as exc:
        raise ValueError("新源元数据中的帧率无效，请重新探测素材。") from exc


def validate_replacement_usages(
    asset: MediaAsset, media_type: str, metadata: Mapping[str, Any],
    sequences: Iterable[Any],
) -> None:
    """Reject replacements that cannot retain existing clip source windows.

    Retiming or changing streams requires a clip transaction; a pool-only
    replacement must never silently change that contract. Unused sources may
    change type/fps freely. Images have no finite frame window.
    """
    usages = [
        (seq, track, clip)
        for seq in sequences for track in seq.all_tracks
        for clip in track.clips if clip.asset_id == asset.asset_id
    ]
    if not usages:
        return
    if media_type != asset.media_type:
        raise ValueError("该素材已被时间线引用，不能直接替换为不同类型。请先将新素材作为独立素材导入。")
    new_fps = source_fps_from_metadata(metadata)
    if media_type == "video":
        if new_fps is None:
            raise ValueError("新视频缺少有效帧率，无法核验现有剪辑的源时间范围。")
        for _seq, _track, clip in usages:
            old_num = getattr(clip, "_source_fps_num", None)
            old_den = getattr(clip, "_source_fps_den", None)
            old_fps = Fraction(old_num, old_den or 1) if old_num else source_fps_from_metadata(asset.metadata)
            if old_fps is not None and old_fps != new_fps:
                raise ValueError("新视频帧率与现有剪辑不同。请将新素材独立导入，以免改变现有剪辑的源入出点。")
        if any(track.track_type == "audio" for _seq, track, _clip in usages) and not metadata.get("audio_info"):
            raise ValueError("现有音轨引用了该素材，但新视频没有可用音频流。")
    if media_type == "image":
        return
    total_frames = metadata.get("total_frames")
    duration = metadata.get("duration_seconds")
    for sequence, _track, clip in usages:
        # Video source windows are expressed in source frames; audio-only
        # windows use the sequence frame base stored on the clip.
        if media_type == "video":
            try:
                if (total_frames is None or isinstance(total_frames, bool)
                        or not math.isfinite(float(total_frames))
                        or int(total_frames) <= 0
                        or Fraction(str(total_frames)) != int(total_frames)):
                    raise ValueError("invalid frame count")
                available = int(total_frames)
            except (TypeError, ValueError, OverflowError) as exc:
                raise ValueError("新视频缺少有效总帧数，无法核验现有剪辑的源入出点。") from exc
        else:
            try:
                seconds = float(duration)
                if not math.isfinite(seconds) or seconds <= 0:
                    raise ValueError("invalid duration")
            except (TypeError, ValueError, OverflowError) as exc:
                raise ValueError("新音频缺少有效时长，无法核验现有剪辑的源入出点。") from exc
            num = getattr(clip, "_source_fps_num", None)
            den = getattr(clip, "_source_fps_den", None)
            if not num:
                num = getattr(clip, "_timeline_fps_num", None)
                den = getattr(clip, "_timeline_fps_den", None)
            rate = Fraction(num, den or 1) if num else Fraction(str(float(sequence.fps))).limit_denominator(100_000)
            available = math.floor(Fraction(str(seconds)) * rate)
        if clip.source_start + clip.source_duration > available:
            raise ValueError(
                f"新素材过短：剪辑 {clip.clip_id} 的源范围至第 "
                f"{clip.source_start + clip.source_duration} 帧，"
                f"新素材只有 {available} 帧。已保留原素材。")
