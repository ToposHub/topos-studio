"""capture —— 从宿主两层状态构建深冻结 InsightSnapshot（T01）。

数据源是鸭子类型的 ``ProjectSequences`` 形态（``.project`` +
``.sequences: dict[timeline_id, TimelineSequence]``），与
``edit_project_service.ProjectSequences`` 及宿主
``ai_edit_host_bridge_impl.get_project_sequences`` 一致。本模块：

- 只做白名单深复制（构造式，参考 ``snapshot_service`` 纪律），不持有
  任何宿主可变对象；
- ref 存在但 sequence 未物化 → ``unloaded``（不造空时间线）；
- 捕获幂等只读：不触发 revision bump、不改宿主 dict、不触碰文件系统
  或媒体（路径仅取 basename）；
- hash 由 :mod:`codec` 统一计算（``capture_readonly_state`` 出来的
  snapshot 已带 payload/content hash）。
"""

from __future__ import annotations

import os
import time
import uuid
from typing import Any

from src.features.edit.insights.dto import (
    LOAD_STATE_LOADED,
    LOAD_STATE_UNLOADED,
    SCHEMA_VERSION,
    SOURCE_MAPPING_CONSTANT,
    SOURCE_MAPPING_IDENTITY,
    SOURCE_MAPPING_UNKNOWN,
    AssetEntry,
    ClipEntry,
    CueEntry,
    FpsSpec,
    InsightSnapshot,
    MarkerEntry,
    ProjectEntry,
    SequenceEntry,
    TrackEntry,
)
from src.features.edit.insights.codec import finalize_snapshot
from src.shared.media.timecode import Timecode, TimecodeType


def _s(value: Any) -> str:
    """宿主字段 → str（None/缺失归空串，绝不把 None 当 "None"）。"""
    if value is None:
        return ""
    return str(value)


def _fps_spec(fps: Any) -> FpsSpec:
    """宿主 fps（Rational/int/float）→ FpsSpec；DF 判定复用 Timecode。

    float 先按 1e6 精度放大再经 Rational 约分（与 TimelineSequence 的
    fps 归一策略一致），保证 25.0 → 25/1 而不是 25000000/1000000。
    """
    from src.shared.media.metadata import Rational

    if hasattr(fps, "num") and hasattr(fps, "den"):
        rational = Rational(int(fps.num), int(fps.den))
    else:
        rational = Rational(int(round(float(fps) * 1_000_000)), 1_000_000)
    if rational.num <= 0 or rational.den <= 0:
        rational = Rational(24, 1)
    drop = Timecode.auto_detect_type(rational) == TimecodeType.DROP_FRAME
    return FpsSpec(num=rational.num, den=rational.den, drop_frame=drop)


def _source_mapping(speed: Any, reverse: bool, freeze_frame: Any) -> tuple[str, float | None]:
    """速度参数 → (映射状态, 恒速倍率)。

    只有恒定速度（含 1.0）且无反向/冻结/速度点/曲线域才给出非 unknown；
    复杂变速（M04 未验收）一律 ``unknown``，工具不得自行推导映射。
    """
    has_points = bool(getattr(speed, "speed_points", ()) or ())
    has_curve = (
        getattr(speed, "curve_length", None) is not None
        or getattr(speed, "curve_time_offset", None) is not None
        or getattr(speed, "curve_offset", 0) not in (0, None)
    )
    if has_points or has_curve or reverse or freeze_frame is not None:
        return SOURCE_MAPPING_UNKNOWN, None
    value = float(getattr(speed, "speed", speed))  # SpeedParams 或裸 float
    if value == 1.0:
        return SOURCE_MAPPING_IDENTITY, None
    return SOURCE_MAPPING_CONSTANT, value


def _clip_entry(clip: Any, sequence_id: str, track_id: str, track_type: str) -> ClipEntry:
    start = int(getattr(clip, "timeline_start", 0) or 0)
    duration = int(getattr(clip, "timeline_duration", 0) or 0)
    src_start = int(getattr(clip, "source_start", 0) or 0)
    src_duration = int(getattr(clip, "source_duration", 0) or 0)
    speed = getattr(clip, "speed", 1.0)
    reverse = bool(getattr(speed, "reverse", False)) if not isinstance(speed, (int, float)) else False
    freeze = getattr(clip, "freeze_source_frame", None)
    mapping, rate = _source_mapping(speed, reverse, freeze)
    return ClipEntry(
        clip_id=_s(getattr(clip, "clip_id", "")),
        sequence_id=sequence_id,
        track_id=track_id,
        track_type=track_type,
        asset_id=_s(getattr(clip, "asset_id", "")),
        timeline_start=start,
        timeline_end=start + duration,
        source_start=src_start,
        source_end=src_start + src_duration,
        is_enabled=bool(getattr(clip, "is_enabled", True)),
        source_mapping=mapping,
        reverse=reverse,
        constant_speed=rate,
        linked_clip_id=getattr(clip, "linked_clip_id", None),
        label_color=getattr(clip, "label_color", None),
    )


def _cue_entry(cue: Any, sequence_id: str, track_id: str) -> CueEntry:
    start = int(getattr(cue, "timeline_start", 0) or 0)
    duration = int(getattr(cue, "timeline_duration", 0) or 0)
    return CueEntry(
        clip_id=_s(getattr(cue, "clip_id", "")),
        sequence_id=sequence_id,
        track_id=track_id,
        text=_s(getattr(cue, "text", "")),
        timeline_start=start,
        timeline_end=start + duration,
        is_enabled=bool(getattr(cue, "is_enabled", True)),
    )


def _track_entry(
    track: Any, sequence_id: str, track_type: str
) -> TrackEntry:
    clips = tuple(
        _clip_entry(c, sequence_id, _s(track.track_id), track_type)
        for c in getattr(track, "clips", ())
    )
    cues = tuple(
        _cue_entry(c, sequence_id, _s(track.track_id))
        for c in getattr(track, "subtitle_clips", ())
    )
    return TrackEntry(
        track_id=_s(getattr(track, "track_id", "")),
        sequence_id=sequence_id,
        name=_s(getattr(track, "name", "")),
        track_type=track_type,
        index=int(getattr(track, "index", 0) or 0),
        is_enabled=bool(getattr(track, "is_enabled", True)),
        track_role=_s(getattr(track, "track_role", "standard")) or "standard",
        clips=clips,
        cues=cues,
    )


def _marker_entry(marker: Any, sequence_id: str) -> MarkerEntry:
    return MarkerEntry(
        marker_id=_s(getattr(marker, "marker_id", "")),
        sequence_id=sequence_id,
        frame=int(getattr(marker, "frame", 0) or 0),
        name=_s(getattr(marker, "name", "")),
        color=_s(getattr(marker, "color", "")),
        comment=_s(getattr(marker, "comment", "")),
        kind=_s(getattr(marker, "kind", "standard")) or "standard",
        clip_id=getattr(marker, "clip_id", None),
        track_id=getattr(marker, "track_id", None),
    )


def _asset_entry(asset: Any) -> AssetEntry:
    path = _s(getattr(asset, "path", ""))
    keywords = getattr(asset, "keywords", ()) or ()
    return AssetEntry(
        asset_id=_s(getattr(asset, "asset_id", "")),
        display_name=_s(getattr(asset, "display_name", "")),
        media_type=_s(getattr(asset, "media_type", "")),
        is_offline=bool(getattr(asset, "is_offline", False)),
        file_basename=os.path.basename(path) if path else "",
        reel=_s(getattr(asset, "reel", "")),
        scene=_s(getattr(asset, "scene", "")),
        shot=_s(getattr(asset, "shot", "")),
        take=_s(getattr(asset, "take", "")),
        keywords=tuple(_s(k) for k in keywords),
        rating=int(getattr(asset, "rating", 0) or 0),
        flag=_s(getattr(asset, "flag", "none")) or "none",
        color_label=_s(getattr(asset, "color_label", "none")) or "none",
        comment=_s(getattr(asset, "comment", "")),
    )


def _sequence_entry(ref: Any, sequence: Any | None) -> SequenceEntry:
    sequence_id = _s(getattr(ref, "timeline_id", ""))
    if sequence is None:
        # ref 存在但未物化：unloaded，分辨率/fps 以 ref 为准（ref 有值），
        # 内容字段为空、duration 为 None——不冒充空时间线。
        fps = _fps_spec(getattr(ref, "fps", 24.0))
        return SequenceEntry(
            sequence_id=sequence_id,
            name=_s(getattr(ref, "name", "")),
            fps=fps,
            width=int(getattr(ref, "width", 1920) or 1920),
            height=int(getattr(ref, "height", 1080) or 1080),
            sample_rate=int(getattr(ref, "sample_rate", 48000) or 48000),
            revision=0,
            load_state=LOAD_STATE_UNLOADED,
            duration_frames=None,
        )
    tracks: list[TrackEntry] = []
    for collection, ttype in (
        ("video_tracks", "video"),
        ("audio_tracks", "audio"),
        ("subtitle_tracks", "subtitle"),
    ):
        for track in getattr(sequence, collection, ()) or ():
            tracks.append(_track_entry(track, sequence_id, ttype))
    markers = tuple(
        _marker_entry(m, sequence_id) for m in getattr(sequence, "markers", ()) or ()
    )
    return SequenceEntry(
        sequence_id=sequence_id,
        name=_s(getattr(sequence, "name", "")),
        fps=_fps_spec(getattr(sequence, "fps", 24.0)),
        width=int(getattr(sequence, "width", 1920) or 1920),
        height=int(getattr(sequence, "height", 1080) or 1080),
        sample_rate=int(getattr(sequence, "sample_rate", 48000) or 48000),
        revision=int(getattr(sequence, "revision", 0) or 0),
        load_state=LOAD_STATE_LOADED,
        duration_frames=int(getattr(sequence, "duration_frames", 0) or 0),
        tracks=tuple(tracks),
        markers=markers,
    )


def capture_readonly_state(
    state: Any,
    *,
    capture_id: str | None = None,
    captured_at: float | None = None,
) -> InsightSnapshot:
    """从两层状态（project + 完整 sequence map）构建带 hash 的快照。

    Args:
        state: ``ProjectSequences`` 形态（``.project``、``.sequences``）。
        capture_id: 注入捕获 ID（测试固定值用）；None 则生成 uuid4。
        captured_at: 注入捕获时间戳；None 则取当前时间。二者均不参与
            content_hash（见 dto 模块 docstring）。

    Returns:
        深冻结、已计算 payload/content hash 的 :class:`InsightSnapshot`。

    Raises:
        ValueError: state 缺 project/sequences（无工程是宿主层 CaptureResult
            的 ``NO_PROJECT``，本纯函数只认结构错误）。
    """
    project = getattr(state, "project", None)
    sequences_map = getattr(state, "sequences", None)
    if project is None or not isinstance(sequences_map, dict):
        raise ValueError(
            "capture_readonly_state expects a ProjectSequences-shaped state "
            "(.project + .sequences dict)"
        )

    refs = getattr(project, "timelines", None) or {}
    entries = tuple(
        _sequence_entry(ref, sequences_map.get(timeline_id))
        for timeline_id, ref in refs.items()
    )
    assets = tuple(
        _asset_entry(a)
        for a in (getattr(project, "assets", None) or {}).values()
    )
    snapshot = InsightSnapshot(
        schema_version=SCHEMA_VERSION,
        capture_id=capture_id or str(uuid.uuid4()),
        captured_at=captured_at if captured_at is not None else time.time(),
        project=ProjectEntry(
            project_id=_s(getattr(project, "project_id", "")),
            name=_s(getattr(project, "name", "")),
            revision=int(getattr(project, "revision", 0) or 0),
        ),
        sequences=entries,
        assets=assets,
    )
    return finalize_snapshot(snapshot)
