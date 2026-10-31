# DORMANT 注记已解除（P1-02 生产接线，2026-08-28）：
# 创建事务（build_nested_from_selection）+ 预览 resolver 注入 + 导出识别 +
# 时间线右键入口/双击进入已接入生产（见 capability_registry edit.nested_sequence）。
"""Nested sequence support — embed one TimelineSequence as a clip in another.

A nested sequence is a virtual MediaAsset (``media_type="nested_sequence"``)
whose pixels are produced by recursively evaluating the referenced child
TimelineSequence at the requested source frame. This mirrors the existing
comp_clip mechanism but swaps the Composite graph renderer for a child
TimelineRenderPlan evaluation.

Storage convention
------------------
- ``MediaAsset.media_type == "nested_sequence"``
- ``MediaAsset.path == "topos://sequence/<timeline_id>"``
- ``MediaAsset.metadata["timeline_id"] == "<timeline_id>"``  (authoritative)
- ``MediaAsset.metadata["width"] / ["height"] / ["fps"] / ["duration_frames"]``
  mirror the child sequence for convenience (used for clip sizing / thumb).

The child TimelineSequence is resolved at render time via a caller-supplied
``sequence_resolver`` callback (``timeline_id -> TimelineSequence | None``),
because full sequences live in the controller layer, not in EditProject.
"""

from __future__ import annotations

import copy
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Optional

from src.features.edit.core.project import MediaAsset
from src.features.edit.core.timeline_v2 import (
    ClipInstance,
    EditTrack,
    TimelineSequence,
)

__all__ = [
    "NESTED_SEQUENCE_URI_PREFIX",
    "make_nested_sequence_uri",
    "timeline_id_from_uri",
    "create_nested_sequence_asset",
    "create_nested_clip",
    "SequenceResolver",
    "evaluate_nested_frame",
    "NestedSequenceTransaction",
    "build_nested_from_selection",
    "collect_contained_timeline_ids",
    "would_create_cycle",
    "NestedAudioDiagnostic",
    "NestedAudioExpansionError",
    "flatten_nested_audio_tracks",
]

# Virtual URI prefix for nested-sequence assets.
NESTED_SEQUENCE_URI_PREFIX = "topos://sequence/"

# Callback type: resolve a child timeline_id to its full TimelineSequence.
SequenceResolver = Callable[[str], Optional[TimelineSequence]]


def make_nested_sequence_uri(timeline_id: str) -> str:
    """Build the virtual asset path for a nested sequence."""
    return f"{NESTED_SEQUENCE_URI_PREFIX}{timeline_id}"


def timeline_id_from_uri(path: str) -> Optional[str]:
    """Extract the timeline_id from a nested-sequence virtual path."""
    if isinstance(path, str) and path.startswith(NESTED_SEQUENCE_URI_PREFIX):
        return path[len(NESTED_SEQUENCE_URI_PREFIX):]
    return None


def create_nested_sequence_asset(
    child_sequence: TimelineSequence,
    asset_id: Optional[str] = None,
    display_name: Optional[str] = None,
) -> MediaAsset:
    """Create a virtual MediaAsset that wraps a child TimelineSequence.

    The asset carries the child's resolution/fps/duration in metadata so the
    host timeline can size the clip and render thumbnails without resolving
    the child immediately.
    """
    tid = child_sequence.timeline_id
    fps_value = child_sequence.fps.to_float() if hasattr(child_sequence.fps, "to_float") else float(child_sequence.fps)
    duration = int(getattr(child_sequence, "duration_frames", 0) or 0)
    name = display_name or child_sequence.name or f"Nested: {tid[:8]}"
    return MediaAsset(
        asset_id=asset_id or str(uuid.uuid4()),
        path=make_nested_sequence_uri(tid),
        media_type="nested_sequence",
        display_name=name,
        metadata={
            "timeline_id": tid,
            "width": int(child_sequence.width),
            "height": int(child_sequence.height),
            "fps": fps_value,
            "duration_frames": duration,
        },
        is_offline=False,
    )


def create_nested_clip(
    nested_asset: MediaAsset,
    track_id: str,
    timeline_start: int,
    timeline_duration: Optional[int] = None,
    source_start: int = 0,
) -> ClipInstance:
    """Build a ClipInstance that references a nested-sequence asset.

    ``timeline_duration`` defaults to the child sequence's full duration.
    """
    child_tid = nested_asset.nested_timeline_id
    if child_tid is None:
        raise ValueError("Asset is not a nested_sequence asset")
    full_duration = int(nested_asset.metadata.get("duration_frames", 0) or 0)
    if timeline_duration is None:
        timeline_duration = full_duration
    source_duration = min(
        max(0, full_duration - source_start),
        timeline_duration,
    ) if full_duration > 0 else timeline_duration
    return ClipInstance(
        asset_id=nested_asset.asset_id,
        track_id=track_id,
        timeline_start=timeline_start,
        timeline_duration=max(1, timeline_duration),
        source_start=source_start,
        source_duration=max(1, source_duration if source_duration > 0 else timeline_duration),
    )


def evaluate_nested_frame(
    nested_asset: MediaAsset,
    source_frame: int,
    sequence_resolver: SequenceResolver,
    render_plan_factory: Callable[[TimelineSequence], Any],
    composite_evaluator: Callable[[Any, int], Optional[Any]],
    _render_stack: Optional[set[str]] = None,
) -> Optional[Any]:
    """Recursively evaluate a nested sequence to produce a single frame.

    Args:
        nested_asset: the ``media_type="nested_sequence"`` asset.
        source_frame: frame number within the child sequence to evaluate.
        sequence_resolver: callback to fetch the child TimelineSequence by id.
        render_plan_factory: builds a ``TimelineRenderPlan`` for a sequence
            (e.g. ``TimelineRenderPlan(seq)``).
        composite_evaluator: given a render plan + frame, returns the
            composited frame array (or None). This is supplied by the caller
            so the same compositing code path (preview/export) is reused.
        _render_stack: internal recursion guard — set of timeline_ids
            currently being evaluated, to detect circular nesting.

    Returns:
        The composited frame (caller-defined type), or None if the child
        sequence cannot be resolved or a cycle is detected.
    """
    child_tid = nested_asset.nested_timeline_id
    if child_tid is None:
        return None

    # Cycle detection: a sequence that (transitively) contains itself.
    stack = _render_stack if _render_stack is not None else set()
    if child_tid in stack:
        return None  # circular nesting — abort this branch
    stack = stack | {child_tid}

    child_seq = sequence_resolver(child_tid)
    if child_seq is None:
        return None

    # Clamp the requested frame into the child's valid range.
    duration = int(getattr(child_seq, "duration_frames", 0) or 0)
    if duration > 0:
        source_frame = max(0, min(source_frame, duration - 1))
    else:
        source_frame = max(0, source_frame)

    plan = render_plan_factory(child_seq)
    # The composite_evaluator may itself encounter nested-sequence layers; it
    # is responsible for threading the recursion guard through. For the common
    # case we pass a closure that re-enters evaluate_nested_frame.
    return composite_evaluator(plan, source_frame)


# ================================================================
# P1-02: 从选择创建嵌套序列（生产事务）
# ================================================================


@dataclass(frozen=True)
class NestedSequenceTransaction:
    """一次"从选择创建嵌套序列"事务的全部产物（纯数据，可整体提交/回滚）。

    Attributes:
        child_timeline: 新建的子 TimelineSequence（选择内容已平移归零）。
        new_timeline: 父时间线（选择片段移除、原位替换为一个嵌套片段）。
        nested_asset: 子序列对应的虚拟 MediaAsset（含 duration 元数据）。
        nested_clip_id: 父时间线上嵌套片段的 ID。
        host_track_id: 嵌套片段所在的父轨道（最早选中视频片段的轨道）。
        removed_clip_ids: 被移入子序列的原片段 ID（回滚/展开用）。
        offset: 子序列内容相对父时间线的平移量（child_frame = parent_frame
            - 嵌套片段 start + 0；创建时嵌套片段 start = min_start，故
            offset = min_start）。
    """

    child_timeline: TimelineSequence
    new_timeline: TimelineSequence
    nested_asset: MediaAsset
    nested_clip_id: str
    host_track_id: str
    removed_clip_ids: tuple[str, ...]
    offset: int


def collect_contained_timeline_ids(
    sequence: TimelineSequence,
    sequences_by_id: dict[str, TimelineSequence],
    nested_child_by_asset_id: dict[str, str],
    _visited: Optional[set[str]] = None,
) -> set[str]:
    """收集 ``sequence`` 直接/间接包含的全部子序列 timeline_id。

    沿嵌套素材（``media_type == "nested_sequence"``）递归向下走：
    ``nested_child_by_asset_id`` 把嵌套素材的 asset_id 映射到子序列 id
    （调用方从 project.assets 构建）；``sequences_by_id`` 提供各时间线
    完整形态。环安全（已访问即返回）。
    """
    tid = sequence.timeline_id
    visited = _visited if _visited is not None else set()
    if tid in visited:
        return set()
    visited.add(tid)

    contained: set[str] = set()
    for track in sequence.all_tracks:
        for clip in track.clips:
            child_tid = nested_child_by_asset_id.get(clip.asset_id)
            if child_tid:
                contained.add(child_tid)
    for child_tid in tuple(contained):
        child = sequences_by_id.get(child_tid)
        if child is not None:
            contained |= collect_contained_timeline_ids(
                child, sequences_by_id, nested_child_by_asset_id, visited
            )
    return contained


def would_create_cycle(
    parent_timeline: TimelineSequence,
    selected_clips: tuple[ClipInstance, ...],
    sequences_by_id: dict[str, TimelineSequence],
    nested_child_by_asset_id: dict[str, str],
) -> Optional[str]:
    """判断把 ``selected_clips`` 移入新子序列是否会构成循环嵌套。

    返回导致循环的 timeline_id（用于报错），无循环返回 None。

    规则：任何被选片段如果是嵌套素材且其子序列就是父序列、或（直接/
    间接）包含父序列，则父包含子的同时子又（间接）包含父 → 环。
    """
    for clip in selected_clips:
        child_tid = nested_child_by_asset_id.get(clip.asset_id)
        if not child_tid:
            continue
        if child_tid == parent_timeline.timeline_id:
            return child_tid
        child = sequences_by_id.get(child_tid)
        if child is None:
            continue
        if parent_timeline.timeline_id in collect_contained_timeline_ids(
            child, sequences_by_id, nested_child_by_asset_id
        ):
            return child_tid
    return None


def build_nested_from_selection(
    timeline: TimelineSequence,
    clip_ids: tuple[str, ...] | list[str],
    name: Optional[str] = None,
    child_timeline_id: Optional[str] = None,
) -> NestedSequenceTransaction:
    """把选中的媒体片段打包成一个新子序列，并在父时间线原位替换为嵌套片段。

    纯函数：不触碰 project / 控制器状态。调用方负责把 ``child_timeline``
    注册进序列缓存、把 ``nested_asset`` 加进 project、发布 ``new_timeline``。

    Args:
        timeline: 当前父时间线。
        clip_ids: 选中的媒体片段 ID（字幕片段不支持，找不到/为空抛错）。
        name: 子序列显示名（默认 "Nested <父名>"）。
        child_timeline_id: 测试可注入的子序列 ID（默认 uuid）。

    前置条件（AUD-16911）：嵌套片段原位占据整个选择跨度
    ``[min_start, min_start+span)``。宿主轨道按"有选中片段的视频轨（纯
    音频选择为首条选中轨道）、按最早选中片段排序"选取，并跳过跨度内仍
    留有未选片段的候选（分层放置）；所有候选都冲突时抛带用户语义的
    ValueError——此时应把跨度内同轨片段全部纳入选择，或先移出跨度。

    Returns:
        NestedSequenceTransaction。

    Raises:
        ValueError: 选择为空、含未知/字幕片段、所有候选宿主轨道在选择
            跨度内仍有未选片段，或嵌套层级会构成循环（需调用方先用
            :func:`would_create_cycle` 检查——此处仅做直接自引用防御）。
    """
    from dataclasses import replace as dc_replace

    if not clip_ids:
        raise ValueError("No clips selected for nested sequence")

    selected: list[tuple[EditTrack, ClipInstance]] = []
    for cid in dict.fromkeys(clip_ids):  # 去重保序
        found: Optional[tuple[EditTrack, ClipInstance]] = None
        for track in timeline.all_tracks:
            clip = track.get_clip_by_id(cid)
            if clip is not None:
                found = (track, clip)
                break
        if found is None:
            raise ValueError(f"Clip not found in timeline: {cid}")
        if found[0].track_type == "subtitle":
            raise ValueError(f"Subtitle clips cannot be nested: {cid}")
        if found[0].is_locked:
            raise ValueError(f"Track is locked: {found[0].track_id}")
        selected.append(found)

    selected_ids = {c.clip_id for _t, c in selected}
    min_start = min(c.timeline_start for _t, c in selected)
    max_end = max(c.timeline_end for _t, c in selected)
    span = max(1, max_end - min_start)

    child_tid = child_timeline_id or str(uuid.uuid4())
    child_name = name or f"Nested {timeline.name}"

    # ── FE-B08：子轨道语义保真构造 ──
    def _build_child_track(
        source_track: EditTrack,
        new_track_id: str,
        track_type: str,
        index: int,
        members: list,
        offset: int,
        bus_id_map: dict[str, str] | None = None,
    ) -> EditTrack:
        """拷贝完整轨道语义：volume/pan/mute/solo/FX/routing/sends/
        automation/bus——旧构造只传 ID/name/type/index/clips，审计实测
        音轨 (.3 volume, -.7 pan, muted) 创建后变 (1, 0, False)。

        clip 动画关键帧同步减 offset：子序列时间轴从 0 起，绝对时间域
        关键帧必须平移，否则第 24 帧仍取旧值（审计实测动画延后 24 帧）。
        """
        moved_clips = tuple(
            dc_replace(
                c, track_id=new_track_id,
                timeline_start=c.timeline_start - offset,
            ).with_temporal_shift(-offset)
            for c in members
        )
        automation = getattr(source_track, "automation", None) or {}
        scaled_automation = {
            param_name: track_kf.shifted(-offset)
            if hasattr(track_kf, "shifted") else track_kf
            for param_name, track_kf in automation.items()
        }
        # F-R05/R24：Bus 路由闭包重写——子序列自包含。此前原样复制
        # output_bus_id/sends 指向父轨 ID，child 里没有对应 Bus，进入
        # child/换父后路由悬空。
        mapped_output = (
            bus_id_map.get(source_track.output_bus_id)
            if source_track.output_bus_id else source_track.output_bus_id
        )
        mapped_sends = tuple(
            dc_replace(
                send,
                target_bus_id=bus_id_map.get(
                    send.target_bus_id, send.target_bus_id
                ),
            )
            if bus_id_map and bus_id_map.get(send.target_bus_id)
            else send
            for send in (source_track.sends or ())
        )
        # N24/RF08：从 source immutable track replace 而非列字段 fresh 构
        # 造——fresh 构造丢 _extra_fields（未来轨字段在 Nest 后消失），
        # 新增列字段也要在此同步。extras 深拷贝防共享可变 dict。
        child_track = dc_replace(
            source_track,
            track_id=new_track_id,
            index=index,
            clips=moved_clips,
            transitions=(),  # 迁入转场由下方 moved-transitions 逻辑显式添加
            is_locked=False,
            output_bus_id=mapped_output,
            sends=mapped_sends,
            automation=(
                scaled_automation if scaled_automation else automation
            ),
        )
        extras = getattr(child_track, "_extra_fields", None)
        if extras:
            object.__setattr__(
                child_track, "_extra_fields",
                {key: copy.deepcopy(value) for key, value in extras.items()},
            )
        return child_track

    # ── F-R05/R24：Bus 依赖闭包——子序列自包含。
    # 选中片段所在轨引用的 output/send Bus（含 bus→bus 递归）整体拷入
    # child，路由 ID 重写到子本地副本；此前原样复制父轨 ID，child 内
    # 无对应 Bus，进入 child/换父后路由悬空。
    bus_tracks_by_id = {
        t.track_id: t
        for t in timeline.audio_tracks
        if getattr(t, "is_bus", False)
    }
    _referenced: set[str] = set()
    for track in timeline.video_tracks + timeline.audio_tracks:
        if not any(c.clip_id in selected_ids for c in track.clips):
            continue
        if track.output_bus_id:
            _referenced.add(track.output_bus_id)
        for _send in (track.sends or ()):
            _bid = getattr(_send, "target_bus_id", None)
            if _bid:
                _referenced.add(_bid)
    _closure: set[str] = set()
    _stack = list(_referenced)
    while _stack:
        _bus_id = _stack.pop()
        if _bus_id in _closure or _bus_id not in bus_tracks_by_id:
            continue
        _closure.add(_bus_id)
        _bus_track = bus_tracks_by_id[_bus_id]
        if _bus_track.output_bus_id:
            _stack.append(_bus_track.output_bus_id)
        for _send in (_bus_track.sends or ()):
            _bid = getattr(_send, "target_bus_id", None)
            if _bid:
                _stack.append(_bid)
    bus_id_map: dict[str, str] = {}
    for _bus_id in sorted(_closure):
        bus_id_map[_bus_id] = f"{child_tid[:8]}-bus{len(bus_id_map)}"

    # ── 子轨道：按原轨道分组、保持原顺序（视频轨道顺序即 z-order）。
    video_tracks: list[EditTrack] = []
    audio_tracks: list[EditTrack] = []
    track_id_map: dict[str, str] = {}
    for i, track in enumerate(timeline.video_tracks):
        members = [c for c in track.clips if c.clip_id in selected_ids]
        if not members:
            continue
        new_track_id = f"{child_tid[:8]}-v{len(video_tracks)}"
        track_id_map[track.track_id] = new_track_id
        video_tracks.append(_build_child_track(
            track, new_track_id, "video", len(video_tracks), members, min_start,
            bus_id_map=bus_id_map,
        ))
    for track in timeline.audio_tracks:
        members = [c for c in track.clips if c.clip_id in selected_ids]
        if not members:
            continue
        new_track_id = f"{child_tid[:8]}-a{len(audio_tracks)}"
        track_id_map[track.track_id] = new_track_id
        audio_tracks.append(_build_child_track(
            track, new_track_id, "audio", len(audio_tracks), members, min_start,
            bus_id_map=bus_id_map,
        ))
    # F-R05/R24：闭包内 Bus 拷入 child（bus→bus 路由同步重映射）
    for _bus_id in sorted(_closure):
        audio_tracks.append(_build_child_track(
            bus_tracks_by_id[_bus_id], bus_id_map[_bus_id], "audio",
            len(audio_tracks), [], min_start, bus_id_map=bus_id_map,
        ))

    # ── 子序列转场：两端都在选择内才搬运（单端转场取非空端判断）。
    child_transitions_by_track: dict[str, tuple] = {}
    for track in timeline.video_tracks + timeline.audio_tracks:
        new_track_id = track_id_map.get(track.track_id)
        if new_track_id is None:
            continue
        moved = []
        for tr in getattr(track, "transitions", ()) or ():
            ends = {tr.left_clip_id, tr.right_clip_id} - {""}
            if ends and ends <= selected_ids:
                moved.append(dc_replace(
                    tr, track_id=new_track_id,
                    center_frame=max(0, tr.center_frame - min_start),
                ))
        if moved:
            child_transitions_by_track[new_track_id] = tuple(moved)
    if child_transitions_by_track:
        def _attach(t: EditTrack) -> EditTrack:
            extra = child_transitions_by_track.get(t.track_id)
            return t if not extra else dc_replace(t, transitions=t.transitions + extra)
        video_tracks = [_attach(t) for t in video_tracks]
        audio_tracks = [_attach(t) for t in audio_tracks]

    # ── 子序列标记：clip 作用域标记随片段搬移并平移。
    child_markers = tuple(
        dc_replace(m, frame=max(0, m.frame - min_start),
                   track_id=track_id_map.get(m.track_id))
        for m in timeline.markers
        if m.clip_id is not None and m.clip_id in selected_ids
    )

    child = TimelineSequence(
        timeline_id=child_tid,
        name=child_name,
        fps=timeline.fps,
        width=timeline.width,
        height=timeline.height,
        sample_rate=timeline.sample_rate,
        format_settings=timeline.format_settings,
        video_tracks=tuple(video_tracks),
        audio_tracks=tuple(audio_tracks),
        markers=child_markers,
    )

    # ── 嵌套虚拟素材 + 父时间线片段。
    asset = create_nested_sequence_asset(child, display_name=child_name)
    if int(asset.metadata.get("duration_frames", 0) or 0) < span:
        asset = dc_replace(asset, metadata={**asset.metadata, "duration_frames": span})

    host_track_id: Optional[str] = None
    ordered_by_start = sorted(selected, key=lambda pair: pair[1].timeline_start)
    video_candidates = list(dict.fromkeys(
        t.track_id for t, _c in ordered_by_start if t.track_type == "video"
    ))
    # AUD-16911：候选宿主轨道 = 有选中片段的视频轨（纯音频选择回退首条
    # 选中轨道），按最早选中片段排序。嵌套片段占据整个选择跨度
    # [min_start, min_start+span)，若某候选轨道的跨度内还留有未选片段，
    # with_clip_added 的重叠校验会抛内部 UUID 错误——改为显式检测并
    # 顺延到下一候选（分层放置），全部候选都冲突时抛带用户语义的错误。
    candidate_track_ids: list[str] = video_candidates or [selected[0][0].track_id]
    span_end = min_start + span
    for candidate_id in candidate_track_ids:
        candidate_track = next(
            t for t in timeline.all_tracks if t.track_id == candidate_id
        )
        blocking = next(
            (
                c for c in candidate_track.clips
                if c.clip_id not in selected_ids
                and c.timeline_start < span_end
                and c.timeline_end > min_start
            ),
            None,
        )
        if blocking is None:
            host_track_id = candidate_id
            break
    if host_track_id is None:
        raise ValueError(
            "Cannot nest selection: every candidate host track still holds "
            f"unselected clips between frames {min_start} and {span_end}. "
            "Include every clip that overlaps the selection span on the "
            "target track, or move those clips out of the span first."
        )

    nested_clip = ClipInstance(
        clip_id=str(uuid.uuid4()),
        asset_id=asset.asset_id,
        track_id=host_track_id,
        timeline_start=min_start,
        timeline_duration=span,
        source_start=0,
        source_duration=span,
    )

    # ── 父时间线：移除选择片段 + 移除已搬移的 clip 标记，再插入嵌套片段。
    new_timeline = timeline
    for track in timeline.video_tracks + timeline.audio_tracks:
        removed_here = [c.clip_id for c in track.clips if c.clip_id in selected_ids]
        if not removed_here:
            continue
        t = track
        for cid in removed_here:
            t = t.with_clip_removed(cid)
        if track.track_id == host_track_id:
            t = t.with_clip_added(nested_clip)
        new_timeline = new_timeline.with_track_updated(t)

    moved_marker_ids = {
        m.marker_id for m in timeline.markers
        if m.clip_id is not None and m.clip_id in selected_ids
    }
    if moved_marker_ids:
        new_timeline = new_timeline._rebuild(markers=tuple(
            m for m in new_timeline.markers if m.marker_id not in moved_marker_ids
        ))

    return NestedSequenceTransaction(
        child_timeline=child,
        new_timeline=new_timeline,
        nested_asset=asset,
        nested_clip_id=nested_clip.clip_id,
        host_track_id=host_track_id,
        removed_clip_ids=tuple(sorted(selected_ids)),
        offset=min_start,
    )


# ================================================================
# P1-02 深化：嵌套序列音频展开（父时间线混音）
# ================================================================

_NESTED_AUDIO_MAX_DEPTH = 8


def _shift_automation(automation: dict, offset: int) -> dict:
    """平移子轨自动化到父坐标，保留负帧插值锚点，不修改原轨。"""

    from src.shared.keyframe import KeyframeTrack

    shifted: dict = {}
    for param, track in (automation or {}).items():
        if not isinstance(track, KeyframeTrack):
            shifted[param] = track
            continue
        # Virtual tracks may legitimately need a key before parent frame 0
        # to retain the incoming interpolation at the nested source In. A
        # clamp changes a volume/pan ramp and can collapse several keys to 0.
        shifted[param] = track.shifted(offset)
    return shifted


def _map_clip_into_window(
    clip: ClipInstance,
    offset: int,
    window: "tuple[int, int]",
) -> Optional[ClipInstance]:
    """把子序列 clip 平移 ``offset`` 帧进父坐标，并按源窗口钳制。

    窗口 = 嵌套片段的 ``[source_start, source_start + timeline_duration)``
    （子序列坐标）。窗口外的子内容不会出现在父时间线里，也不应发声。
    无重叠返回 None。
    """
    from dataclasses import replace as _dc_replace

    cs = int(clip.timeline_start)
    ce = cs + int(clip.timeline_duration)
    ws, we = window
    start_trim = max(0, ws - cs)
    end_trim = max(0, ce - we)
    new_duration = int(clip.timeline_duration) - start_trim - end_trim
    if new_duration < 1:
        return None
    # The child clip can itself be reversed, retimed or mixed-FPS. Reuse the
    # same source-window clock as timeline trims instead of treating a timeline
    # tick as one source frame. Keep animation in the parent coordinate space.
    from src.features.edit.core.timeline_ops import copy_clip_with

    source_start, source_duration = clip.with_left_edge_source_values(cs + start_trim)
    cropped = copy_clip_with(
        clip, timeline_start=cs + start_trim,
        timeline_duration=int(clip.timeline_duration) - start_trim,
        source_start=source_start, source_duration=source_duration,
        **clip.source_window_mapping_updates(source_start, source_duration, start_trim),
    )
    source_start, source_duration = cropped.with_right_edge_source_values(new_duration)
    cropped = copy_clip_with(
        cropped, timeline_duration=new_duration,
        source_start=source_start, source_duration=source_duration,
        **cropped.source_window_mapping_updates(source_start, source_duration, 0),
    )
    return _dc_replace(
        cropped, timeline_start=cropped.timeline_start + offset,
    ).with_temporal_shift(offset)


@dataclass(frozen=True)
class NestedAudioDiagnostic:
    """Pure, serializable reason why an audible nested instance cannot expand."""

    code: str
    clip_id: str
    child_timeline_id: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {
            "code": self.code, "clip_id": self.clip_id,
            "child_timeline_id": self.child_timeline_id, "message": self.message,
        }


class NestedAudioExpansionError(ValueError):
    """An audio delivery must not silently omit these nested instances."""

    def __init__(self, diagnostics: tuple[NestedAudioDiagnostic, ...]) -> None:
        self.diagnostics = diagnostics
        super().__init__("; ".join(item.message for item in diagnostics))


def flatten_nested_audio_tracks(
    sequence: TimelineSequence,
    sequence_resolver: Optional[SequenceResolver],
    asset_lookup: Optional[Callable[[str], Optional[MediaAsset]]] = None,
    max_depth: int = _NESTED_AUDIO_MAX_DEPTH,
    *,
    strict: bool = False,
    diagnostics: Optional[list[NestedAudioDiagnostic]] = None,
    frame_range: Optional[tuple[int, int]] = None,
) -> "tuple[EditTrack, ...]":
    """把嵌套片段引用的子序列音频轨展开到父时间线坐标（纯函数）。

    返回 = 父序列自身 audio_tracks（原样） + 各嵌套片段实例贡献的虚拟轨。
    实时播放（EditPage 音频同步）、离线导出（timeline_export）与 Stem
    共用本函数，保证三条路径语义一致。

    虚拟轨语义：
    - ``track_id = f"nest:{嵌套片段id}:{子轨id}"`` —— 同一子序列的多个
      实例各自独立轨状态（互不串音量/路由）；
    - clips 平移：``parent = child + (nested.timeline_start -
      nested.source_start)``，并按嵌套片段源窗口钳制（窗口外不发声）；
    - 子轨音量/声像/EQ/FX/自动化（关键帧平移）/Bus 拓扑全部保留，
      ``output_bus_id`` / ``sends.target_bus_id`` 重映射到同命名空间的
      虚拟 Bus 轨（子 Bus 轨也一并展开）；
    - 子轨转场不映射（child 坐标无法直接平移，clip 级淡化保留）；
    - 不支持的映射在预览中跳过并写入 diagnostics；strict 交付抛出领域异常。
    - frame_range 是父坐标的半开区间，仅检查/展开本次范围内的实例。
    """
    own = tuple(sequence.audio_tracks or ())
    if asset_lookup is None:
        return own

    import logging as _logging

    logger = _logging.getLogger(__name__)

    extra: list[EditTrack] = []
    issues: list[NestedAudioDiagnostic] = []
    root_id = getattr(sequence, "timeline_id", "")

    def _is_nested_asset(asset: Optional[MediaAsset]) -> bool:
        return (
            asset is not None
            and getattr(asset, "is_nested_sequence", False)
        )

    def _diagnose(code: str, clip: ClipInstance, child_id: str, reason: str) -> None:
        item = NestedAudioDiagnostic(
            code, clip.clip_id, child_id,
            f"nested audio: clip {clip.clip_id}, child {child_id or '<unknown>'}: {reason}",
        )
        issues.append(item)
        logger.warning(item.message)

    def _expand(nested_clip: ClipInstance, path_ids: frozenset[str], depth: int) -> None:
        if not nested_clip.is_enabled:
            return
        if frame_range is not None and (
                nested_clip.timeline_start >= frame_range[1]
                or nested_clip.timeline_end <= frame_range[0]):
            return
        asset = asset_lookup(nested_clip.asset_id)
        if not _is_nested_asset(asset):
            return
        child_id = asset.nested_timeline_id or ""
        if depth > max_depth:
            _diagnose("nested_audio_depth_exceeded", nested_clip, child_id,
                      f"maximum nesting depth {max_depth} exceeded")
            return
        if not child_id:
            _diagnose("nested_audio_child_missing", nested_clip, child_id,
                      "nested asset has no child timeline identity")
            return
        if child_id in path_ids:
            _diagnose("nested_audio_cycle", nested_clip, child_id, "cycle detected")
            return
        if sequence_resolver is None:
            _diagnose("nested_audio_resolver_missing", nested_clip, child_id,
                      "child sequence resolver is unavailable")
            return
        child = sequence_resolver(child_id)
        if child is None:
            _diagnose("nested_audio_child_missing", nested_clip, child_id,
                      "child sequence is unavailable")
            return
        nested_speed = getattr(nested_clip, "speed", None)
        if (abs(float(getattr(nested_clip, "effective_speed", lambda: 1.0)()) - 1.0) > 1e-6
                or bool(getattr(nested_speed, "reverse", False))
                or bool(getattr(nested_speed, "speed_points", ()))
                or nested_clip.freeze_source_frame is not None
                or nested_clip.source_frames_per_timeline_frame() != 1
                or nested_clip._source_frame_phase_num != 0):
            _diagnose("nested_audio_retime_unsupported", nested_clip, child_id,
                      "requires parent retime/reverse resampling; audio skipped")
            return
        if abs(float(child.fps) - float(sequence.fps)) > 1e-6:
            _diagnose("nested_audio_fps_unsupported", nested_clip, child_id,
                      f"mixed sequence FPS requires resampling ({child.fps} -> {sequence.fps})")
            return

        offset = int(nested_clip.timeline_start) - int(nested_clip.source_start)
        window = (
            int(nested_clip.source_start),
            int(nested_clip.source_start) + int(nested_clip.timeline_duration),
        )
        prefix = f"nest:{nested_clip.clip_id}:"
        child_bus_ids = {
            t.track_id for t in (child.audio_tracks or ()) if getattr(t, "is_bus", False)
        }

        def _remap_bus(bus_id):
            if bus_id in child_bus_ids:
                return prefix + bus_id
            return bus_id

        for child_track in child.audio_tracks or ():
            from dataclasses import replace as _dc_replace

            # F3.1：子音轨 crossfade 此前在 transitions=() 处静默丢弃——
            # strict 交付也不知情。补结构化诊断（clip 级 fade_in/out 仍随
            # 字段复制保留），完整转场平移登记为后续差量。
            if child_track.transitions:
                _diagnose(
                    "nested_audio_transition_dropped", nested_clip, child_id,
                    f"child audio crossfades dropped: "
                    f"{len(child_track.transitions)} transition(s) not mapped by flatten")

            mapped_clips = []
            for cc in child_track.clips:
                mapped = _map_clip_into_window(cc, offset, window)
                if mapped is not None:
                    mapped_clips.append(_dc_replace(
                        mapped,
                        clip_id=prefix + cc.clip_id,
                        track_id=prefix + child_track.track_id,
                        linked_clip_id=(prefix + cc.linked_clip_id)
                        if cc.linked_clip_id else None,
                    ))
            new_sends = tuple(
                _dc_replace(s, target_bus_id=_remap_bus(s.target_bus_id))
                for s in (child_track.sends or ())
            )
            virtual = _dc_replace(
                child_track,
                track_id=prefix + child_track.track_id,
                name=f"{child_track.name or child_track.track_id} (nested)",
                clips=tuple(mapped_clips),
                transitions=(),
                output_bus_id=_remap_bus(child_track.output_bus_id)
                if child_track.output_bus_id else None,
                sends=new_sends,
                automation=_shift_automation(child_track.automation, offset),
            )
            if mapped_clips or virtual.is_bus:
                extra.append(virtual)

        # 孙辈：子序列里再嵌套的片段也展开（坐标映射到父坐标后递归）。
        next_path = path_ids | {child_id}
        for track in list(child.video_tracks or ()) + list(child.audio_tracks or ()):
            if not track.is_enabled or track.is_muted:
                continue
            for cc in track.clips:
                ca = asset_lookup(cc.asset_id)
                if not _is_nested_asset(ca):
                    continue
                mapped = _map_clip_into_window(cc, offset, window)
                if mapped is not None:
                    from dataclasses import replace as _dc_replace
                    _expand(_dc_replace(
                        mapped, clip_id=prefix + cc.clip_id,
                        track_id=prefix + track.track_id,
                    ), next_path, depth + 1)

    root_path = frozenset({root_id} if root_id else ())
    for track in list(sequence.video_tracks or ()) + list(sequence.audio_tracks or ()):
        if not track.is_enabled or track.is_muted:
            continue
        for clip in track.clips:
            _expand(clip, root_path, 1)

    if diagnostics is not None:
        diagnostics.extend(issues)
    if strict and issues:
        raise NestedAudioExpansionError(tuple(issues))
    if not extra:
        return own
    return own + tuple(extra)
