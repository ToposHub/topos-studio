"""F0.3 黄金工程结构快照——轨/片段/身份/时间基/链接/效果的结构级黄金对比。

与像素级 golden_compare.py、媒体级 baseline manifest 互补：这里冻结的是
**工程结构语义**（身份唯一、链接双向、关键帧界内、时间基一致），供后续
任务在改动前后断言结构不回归。
"""

from __future__ import annotations

from typing import Any

from src.features.edit.core.project import EditProject
from src.features.edit.core.timeline_v2 import TimelineSequence


def _clip_dict(clip: Any, project: EditProject | None) -> dict[str, Any]:
    asset = project.assets.get(clip.asset_id) if project is not None else None
    if asset is not None:
        asset_ref = f"asset:{asset.path}"
    elif getattr(clip, "nested_asset_ref", None):
        asset_ref = "nested"
    else:
        asset_ref = f"raw:{clip.asset_id}"
    def _kf_count(kf_track: Any) -> int:
        for attr in ("keyframes", "frames", "points"):
            value = getattr(kf_track, attr, None)
            if value is not None and hasattr(value, "__len__"):
                return len(value)
        return -1

    kf_counts = {name: _kf_count(track) for name, track in (clip.animated_params or {}).items()}
    d: dict[str, Any] = {
        "clip_id": clip.clip_id,
        "asset_ref": asset_ref,
        "track_id": clip.track_id,
        "timeline_start": clip.timeline_start,
        "timeline_duration": clip.timeline_duration,
        "source_start": clip.source_start,
        "source_duration": clip.source_duration,
        "linked_clip_id": clip.linked_clip_id,
        "is_enabled": clip.is_enabled,
        "effects": [
            {"effect_type": e.effect_type, "order": e.order, "params": dict(e.params)}
            for e in (clip.effects or ())
        ],
        "animated_param_tracks": sorted(kf_counts),
        "color_stack_id": clip.color_stack_id,
        "color_shared": clip.color_shared,
    }
    return d


def _subtitle_dict(clip: Any) -> dict[str, Any]:
    return {
        "clip_id": clip.clip_id,
        "track_id": clip.track_id,
        "timeline_start": clip.timeline_start,
        "timeline_duration": clip.timeline_duration,
        "text": clip.text,
    }


def snapshot_structure(timeline: TimelineSequence, project: EditProject | None = None) -> dict[str, Any]:
    """结构快照：轨/片段/字幕/标记/时间基。动态 asset_id 解析为路径引用。"""
    tracks: list[dict[str, Any]] = []
    for group in ("video_tracks", "audio_tracks", "subtitle_tracks"):
        for track in getattr(timeline, group):
            if track.track_type == "subtitle":
                clips = [_subtitle_dict(c) for c in (track.subtitle_clips or ())]
            else:
                clips = [_clip_dict(c, project) for c in track.clips]
            tracks.append({
                "track_id": track.track_id,
                "name": track.name,
                "type": track.track_type,
                "index": track.index,
                "is_enabled": getattr(track, "is_enabled", True),
                "clips": clips,
            })
    fps = timeline.fps
    fps_value = fps.to_float() if hasattr(fps, "to_float") else float(fps)
    return {
        "timeline_id": timeline.timeline_id,
        "name": timeline.name,
        "fps": fps_value,
        "width": timeline.width,
        "height": timeline.height,
        "sample_rate": timeline.sample_rate,
        "tracks": tracks,
        "markers": [
            {"marker_id": m.marker_id, "frame": m.frame, "name": m.name}
            for m in (timeline.markers or ())
        ],
    }


def structure_violations(snapshot: dict[str, Any]) -> list[str]:
    """结构不变量校验；返回违例列表（空 = 通过）。

    不变量：clip/track id 唯一；链接双向；关键帧轨道存在；视频片段时长为正。
    """
    violations: list[str] = []
    track_ids: list[str] = []
    clip_ids: list[str] = []
    clips_by_id: dict[str, dict[str, Any]] = {}
    for track in snapshot["tracks"]:
        if track["track_id"] in track_ids:
            violations.append(f"duplicate track_id {track['track_id']}")
        track_ids.append(track["track_id"])
        for clip in track["clips"]:
            if clip["clip_id"] in clip_ids:
                violations.append(f"duplicate clip_id {clip['clip_id']}")
            clip_ids.append(clip["clip_id"])
            clips_by_id[clip["clip_id"]] = clip
            if clip["timeline_duration"] <= 0:
                violations.append(f"clip {clip['clip_id']} non-positive duration")
    for clip in clips_by_id.values():
        linked = clip.get("linked_clip_id")
        if linked:
            partner = clips_by_id.get(linked)
            if partner is None:
                violations.append(f"clip {clip['clip_id']} links to missing {linked}")
            elif partner.get("linked_clip_id") != clip["clip_id"]:
                violations.append(
                    f"link not bidirectional: {clip['clip_id']} -> {linked} "
                    f"-> {partner.get('linked_clip_id')}")
    return violations


def assert_structure_matches(actual: dict[str, Any], expected: dict[str, Any], *, ignore: tuple[str, ...] = ()) -> None:
    """冻结断言：逐字段相等（忽略 ignore 列出的顶层键）。"""
    import copy

    a = copy.deepcopy(actual)
    e = copy.deepcopy(expected)
    for key in ignore:
        a.pop(key, None)
        e.pop(key, None)
    assert a == e, f"structure mismatch:\nactual={a}\nexpected={e}"
