"""VP-B06：Match Frame——权威源帧映射、播放头策略与候选列表（纯领域）。

计划：docs/plans/2026-11-09-edit-viewport-backend-plan.md §VP-B06。

背景缺口：时间线"在 Source 打开"用朴素公式
``source_start + (播放头 − 片段起点)``——不含 retime／reverse／freeze／
混合帧率映射，变速片段打开的就是错帧；Source→Timeline 的同素材多用途
没有候选列表（不能任意跳第一处）。

设计：

- 帧映射一律委托 ``ClipInstance.timeline_to_source_frame``——它已经是
  覆盖 source_start/trim/retime 曲线/reverse/freeze/mixed fps/motion
  graphic deck 的**权威**实现（预览与导出同源）；本模块不重算映射，
  只负责播放头/选中/转场侧的策略与 capability 原因；
- 拒绝必须带稳定原因（``clip_not_found``/``no_source_media``/
  ``no_timeline``/``outside_clip``），宿主据此回填状态栏；不移动任何
  播放头、零写入；
- Motion Graphic / Teaching Animation 是虚拟素材（无源媒体可在 Source
  打开）→ ``no_source_media``，即使 deck 帧映射本身存在；
- ``find_source_frame_uses`` 只列候选（视频轨存储顺序 → 时间线起点），
  选择目标 clip 是宿主 UI 的职责——不任意跳第一处。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Optional, Tuple

# 与 ClipInstance.timeline_to_source_frame 内的判定保持一致的热路径前缀
VIRTUAL_SOURCE_PREFIXES = ("teaching_animation:", "motion_graphic:")

REASON_NO_TIMELINE = "no_timeline"
REASON_CLIP_NOT_FOUND = "clip_not_found"
REASON_NO_SOURCE_MEDIA = "no_source_media"
REASON_OUTSIDE_CLIP = "outside_clip"

_REASON_TEXT = {
    REASON_NO_TIMELINE: "No timeline is open for Match Frame.",
    REASON_CLIP_NOT_FOUND: "The clip no longer exists.",
    REASON_NO_SOURCE_MEDIA:
        "This clip has no source media to open (motion graphic or virtual "
        "asset).",
    REASON_OUTSIDE_CLIP: "No clip under the playhead to match.",
}


def describe_match_frame_reason(reason: str) -> str:
    return _REASON_TEXT.get(reason, reason)


def _is_virtual_asset(clip: Any) -> bool:
    return str(clip.asset_id or "").startswith(VIRTUAL_SOURCE_PREFIXES)


def _clip_span(clip: Any) -> tuple[int, int]:
    start = int(clip.timeline_start)
    end = start + max(1, int(clip.timeline_duration))
    return start, end


@dataclass(frozen=True)
class MatchFrameResult:
    """一次 Match Frame 的结果：权威源帧或稳定拒绝原因。"""

    ok: bool
    clip_id: Optional[str] = None
    asset_id: Optional[str] = None
    source_frame: Optional[int] = None
    reason: Optional[str] = None


@dataclass(frozen=True)
class SourceFrameUse:
    """同素材的一次使用（反向 Match Frame 候选）。"""

    clip_id: str
    track_id: str
    asset_id: str
    timeline_start: int
    timeline_end: int  # exclusive
    source_start: int
    source_end: int  # inclusive


def resolve_match_frame(sequence: Any, clip_id: str,
                        timeline_frame: int) -> MatchFrameResult:
    """显式 clip（双击/选中侧）→ 播放头夹入片段跨度后的权威源帧。"""
    if sequence is None:
        return MatchFrameResult(False, reason=REASON_NO_TIMELINE)
    clip = sequence.get_clip_by_id(clip_id)
    if clip is None:
        return MatchFrameResult(False, reason=REASON_CLIP_NOT_FOUND)
    if _is_virtual_asset(clip):
        return MatchFrameResult(
            False, clip_id=clip_id, asset_id=clip.asset_id,
            reason=REASON_NO_SOURCE_MEDIA)
    start, end = _clip_span(clip)
    clamped = min(max(int(timeline_frame), start), end - 1)
    # 权威映射：覆盖 trim/retime 曲线/reverse/freeze/mixed fps
    source_frame = int(clip.timeline_to_source_frame(clamped))
    return MatchFrameResult(
        True, clip_id=clip_id, asset_id=clip.asset_id,
        source_frame=source_frame)


def resolve_match_frame_at_playhead(
        sequence: Any, timeline_frame: int,
        selected_clip_ids: Iterable[str] = ()) -> MatchFrameResult:
    """播放头处解析：显式选中优先，否则可见视频轨优先的覆盖片段。

    转场覆盖剪辑点时由选中侧决定（谁被选中/双击就用谁的映射）；播放头
    处无片段 → 拒绝，不移动任何播放头。
    """
    if sequence is None:
        return MatchFrameResult(False, reason=REASON_NO_TIMELINE)
    for clip_id in selected_clip_ids:
        clip = sequence.get_clip_by_id(clip_id)
        if clip is not None:
            return resolve_match_frame(sequence, clip_id, timeline_frame)
    frame = int(timeline_frame)
    for track in (*sequence.video_tracks, *sequence.audio_tracks):
        for clip in track.clips:
            start, end = _clip_span(clip)
            if start <= frame < end:
                return resolve_match_frame(sequence, clip.clip_id, frame)
    return MatchFrameResult(False, reason=REASON_OUTSIDE_CLIP)


def find_source_frame_uses(sequence: Any, asset_id: str,
                           source_frame: int
                           ) -> Tuple[SourceFrameUse, ...]:
    """同素材包含该源帧的全部使用（候选列表，只列不跳）。

    排序：视频轨存储顺序（上→下）在前，音频轨随后；同轨按时间线起点。
    """
    if sequence is None or not asset_id:
        return ()
    frame = int(source_frame)
    uses: list[SourceFrameUse] = []
    for track in (*sequence.video_tracks, *sequence.audio_tracks):
        for clip in sorted(track.clips, key=lambda c: int(c.timeline_start)):
            if str(clip.asset_id) != asset_id or _is_virtual_asset(clip):
                continue
            source_start = int(clip.source_start)
            source_end = source_start + max(1, int(clip.source_duration)) - 1
            if source_start <= frame <= source_end:
                start, end = _clip_span(clip)
                uses.append(SourceFrameUse(
                    clip_id=clip.clip_id, track_id=track.track_id,
                    asset_id=asset_id,
                    timeline_start=start, timeline_end=end,
                    source_start=source_start, source_end=source_end))
    return tuple(uses)
