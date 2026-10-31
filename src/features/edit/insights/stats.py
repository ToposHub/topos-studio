"""T12 · 素材使用与剪辑节奏统计（纯 DTO，口径显式，无质量分）。

纪律（计划 T12）：

- 直接使用统计语义与既有 ``asset_usage_index.build_asset_usage_index``
  一致（clip 直接引用位置）；间接依赖闭包（外录绑定/嵌套展开/M20）不
  纳入，``scope_notes`` 显式声明；
- 叠轨"时长总和"与"节目覆盖并集"分开给出，禁止相加冒充节目时长；
  间隙/禁用轨口径可见（``include_disabled_tracks`` 参数 + 轨级明细）；
- 每个比率类统计带分母（duration_frames + fps），分母不可得 → None
  （未收集，不冒充 0）；
- 不生成任何"节奏质量分/QC 分"（无依据评分禁止；评分属既有
  ``editorial_quality``，本模块不复制）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from src.features.edit.insights.dto import (
    LOAD_STATE_LOADED,
    InsightSnapshot,
    SequenceEntry,
    TrackEntry,
)

# 口径声明（进入每个报告，UI/导出必须可见）
SCOPE_DIRECT_USAGE_ONLY = (
    "usage = direct clip references only (asset_usage_index semantics); "
    "indirect dependency closures (audio bindings / nested expansion / M20) "
    "are NOT included"
)
SCOPE_NESTED_NOT_EXPANDED = (
    "nested_sequence / generator assets count as one direct occurrence; "
    "inner timelines are not expanded"
)


@dataclass(frozen=True)
class Coverage:
    """半开区间集合的覆盖统计（union/间隙/张成分开）。"""

    segments: tuple[tuple[int, int], ...]   # 合并后的不重叠区段
    union_frames: int
    span_frames: int                        # [0, 最大 end)（0 起）
    gap_frames: int                         # span - union


def merge_intervals(intervals: Iterable[tuple[int, int]]) -> tuple[tuple[int, int], ...]:
    """合并半开区间为不重叠区段（输入可为乱序/相邻/包含）。"""
    ordered = sorted((int(s), int(e)) for s, e in intervals if e > s)
    merged: list[list[int]] = []
    for start, end in ordered:
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return tuple((s, e) for s, e in merged)


def compute_coverage(intervals: Iterable[tuple[int, int]]) -> Coverage:
    segments = merge_intervals(intervals)
    union = sum(e - s for s, e in segments)
    span = max((e for _, e in segments), default=0)
    return Coverage(
        segments=segments,
        union_frames=union,
        span_frames=span,
        gap_frames=span - union,
    )


@dataclass(frozen=True)
class RhythmStats:
    """剪辑节奏（只描述计数/分布，不打分）。"""

    shot_count: int
    lengths: tuple[int, ...]                # 升序
    mean_length: float | None
    median_length: float | None
    min_length: int | None
    max_length: int | None
    cut_count: int | None                   # 相邻边界数；无相邻信息按 count-1
    cuts_per_minute: float | None           # 分母：duration_frames / fps
    denominator_frames: int | None
    fps_num: int
    fps_den: int


def compute_rhythm(
    lengths: Iterable[int],
    *,
    duration_frames: int | None,
    fps_num: int,
    fps_den: int,
) -> RhythmStats:
    values = sorted(lengths)
    count = len(values)
    mean = sum(values) / count if count else None
    if count:
        mid = count // 2
        median = float(values[mid]) if count % 2 else (values[mid - 1] + values[mid]) / 2
    else:
        median = None
    cut_count = max(count - 1, 0) if count else None
    cuts_per_minute: float | None = None
    if count and duration_frames and duration_frames > 0 and fps_num > 0:
        minutes = (duration_frames * fps_den) / (fps_num * 60.0)
        if minutes > 0:
            cuts_per_minute = cut_count / minutes
    return RhythmStats(
        shot_count=count,
        lengths=tuple(values),
        mean_length=mean,
        median_length=median,
        min_length=values[0] if values else None,
        max_length=values[-1] if values else None,
        cut_count=cut_count,
        cuts_per_minute=cuts_per_minute,
        denominator_frames=duration_frames,
        fps_num=fps_num,
        fps_den=fps_den,
    )


@dataclass(frozen=True)
class AssetOccurrence:
    sequence_id: str
    track_id: str
    track_type: str
    clip_id: str
    timeline_start: int
    timeline_end: int


@dataclass(frozen=True)
class AssetUsage:
    asset_id: str
    display_name: str
    media_type: str
    is_offline: bool
    occurrences: tuple[AssetOccurrence, ...]

    @property
    def occurrence_count(self) -> int:
        return len(self.occurrences)

    @property
    def sequence_ids(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(o.sequence_id for o in self.occurrences))


@dataclass(frozen=True)
class TrackCoverage:
    track_id: str
    track_name: str
    track_type: str
    is_enabled: bool
    clip_count: int
    sum_of_durations: int     # 行和（叠轨 ≠ 并集）
    coverage: Coverage


@dataclass(frozen=True)
class SequenceStats:
    sequence_id: str
    name: str
    load_state: str
    fps_num: int
    fps_den: int
    duration_frames: int | None
    clip_count: int                       # 计入的 clip 行数（口径见参数）
    video_shot_rhythm: RhythmStats | None
    video_tracks_union: Coverage | None   # 选中视频轨并集
    all_tracks_union: Coverage | None
    cue_coverage: Coverage | None
    tracks: tuple[TrackCoverage, ...]


@dataclass(frozen=True)
class UsageRhythmReport:
    project_id: str
    project_name: str
    complete: bool                        # 任一序列未加载 → False
    sequences: tuple[SequenceStats, ...]
    assets: tuple[AssetUsage, ...]
    unused_asset_ids: tuple[str, ...]
    scope_notes: tuple[str, ...]
    include_disabled_tracks: bool


def _iter_tracks(
    seq: SequenceEntry, include_disabled: bool
) -> Iterable[tuple[TrackEntry, bool]]:
    for track in seq.tracks:
        yield track, include_disabled or track.is_enabled


def compute_sequence_stats(
    seq: SequenceEntry, *, include_disabled_tracks: bool = True
) -> SequenceStats:
    tracks_out: list[TrackCoverage] = []
    video_intervals: list[tuple[int, int]] = []
    all_intervals: list[tuple[int, int]] = []
    video_lengths: list[int] = []
    clip_count = 0
    cue_intervals: list[tuple[int, int]] = []

    for track, counted in _iter_tracks(seq, include_disabled_tracks):
        clip_intervals = [(c.timeline_start, c.timeline_end) for c in track.clips]
        cue_ivals = [(c.timeline_start, c.timeline_end) for c in track.cues]
        coverage = compute_coverage(clip_intervals + cue_ivals)
        tracks_out.append(TrackCoverage(
            track_id=track.track_id,
            track_name=track.name,
            track_type=track.track_type,
            is_enabled=track.is_enabled,
            clip_count=len(track.clips),
            sum_of_durations=sum(e - s for s, e in clip_intervals),
            coverage=coverage,
        ))
        if not counted:
            continue
        clip_count += len(track.clips)
        all_intervals.extend(clip_intervals)
        cue_intervals.extend(cue_ivals)
        if track.track_type == "video":
            video_intervals.extend(clip_intervals)
            video_lengths.extend(e - s for s, e in clip_intervals)

    loaded = seq.load_state == LOAD_STATE_LOADED
    duration = seq.duration_frames if loaded else None
    return SequenceStats(
        sequence_id=seq.sequence_id,
        name=seq.name,
        load_state=seq.load_state,
        fps_num=seq.fps.num,
        fps_den=seq.fps.den,
        duration_frames=duration,
        clip_count=clip_count,
        video_shot_rhythm=(
            compute_rhythm(
                video_lengths,
                duration_frames=duration,
                fps_num=seq.fps.num,
                fps_den=seq.fps.den,
            )
            if loaded
            else None
        ),
        video_tracks_union=compute_coverage(video_intervals) if loaded else None,
        all_tracks_union=compute_coverage(all_intervals) if loaded else None,
        cue_coverage=compute_coverage(cue_intervals) if loaded else None,
        tracks=tuple(tracks_out),
    )


def compute_usage_rhythm(
    snapshot: InsightSnapshot, *, include_disabled_tracks: bool = True
) -> UsageRhythmReport:
    seq_stats = tuple(
        compute_sequence_stats(s, include_disabled_tracks=include_disabled_tracks)
        for s in snapshot.sequences
    )
    complete = all(s.load_state == LOAD_STATE_LOADED for s in snapshot.sequences)

    asset_by_id = {a.asset_id: a for a in snapshot.assets}
    occurrences: dict[str, list[AssetOccurrence]] = {
        asset_id: [] for asset_id in asset_by_id
    }
    for seq in snapshot.sequences:
        if seq.load_state != LOAD_STATE_LOADED:
            continue
        for track, counted in _iter_tracks(seq, include_disabled_tracks):
            if not counted:
                continue
            for clip in track.clips:
                occurrences.setdefault(clip.asset_id, []).append(AssetOccurrence(
                    sequence_id=seq.sequence_id,
                    track_id=track.track_id,
                    track_type=track.track_type,
                    clip_id=clip.clip_id,
                    timeline_start=clip.timeline_start,
                    timeline_end=clip.timeline_end,
                ))

    assets = tuple(
        AssetUsage(
            asset_id=asset_id,
            display_name=(
                asset_by_id[asset_id].display_name
                if asset_id in asset_by_id else asset_id  # 池外引用：用 id 占位
            ),
            media_type=(
                asset_by_id[asset_id].media_type
                if asset_id in asset_by_id else "unknown"
            ),
            is_offline=asset_by_id[asset_id].is_offline if asset_id in asset_by_id else False,
            occurrences=tuple(entries),
        )
        for asset_id, entries in occurrences.items()
    )
    unused = tuple(asset.asset_id for asset in assets if not asset.occurrences)
    return UsageRhythmReport(
        project_id=snapshot.project.project_id,
        project_name=snapshot.project.name,
        complete=complete,
        sequences=seq_stats,
        assets=assets,
        unused_asset_ids=unused,
        scope_notes=(
            SCOPE_DIRECT_USAGE_ONLY,
            SCOPE_NESTED_NOT_EXPANDED,
            (
                "disabled tracks included"
                if include_disabled_tracks else "disabled tracks excluded"
            ),
        ),
        include_disabled_tracks=include_disabled_tracks,
    )
