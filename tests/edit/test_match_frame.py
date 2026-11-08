"""VP-B06：Match Frame——权威源帧映射、候选列表与视图接线。

缺口（计划 VP-B06）：时间线双击"在 Source 打开"用朴素公式
``source_start + (播放头 − 片段起点)``——**不含** retime／reverse／freeze／
混合帧率映射，变速片段打开的就是错帧；也没有"Source→Timeline"的同素材
多用途候选列表（不能任意跳第一处）。

契约（``src/features/edit/core/match_frame.py``，纯领域）：

- ``resolve_match_frame``：显式 clip（双击/选中侧）→ 播放头夹入片段
  时间跨度后走**权威映射** ``clip.timeline_to_source_frame``（覆盖
  source_start/trim/retime/reverse/freeze/mixed fps；motion graphic 由
  该映射给出 deck 帧但**无源媒体可开** → capability 原因）；
- ``resolve_match_frame_at_playhead``：显式选中优先；否则可见视频轨
  优先的覆盖片段；播放头处无片段 → 原因，不移动任何播放头；
- ``find_source_frame_uses``：同素材多次使用 → 有序候选列表（视频轨
  顺序 → 时间线起点），**只列不跳**（宿主弹选择器，不任意跳第一处）；
- 视图接线：``_on_clip_double_clicked`` 改走 resolve_match_frame，
  拒绝时状态栏回因、不发打开请求（零写入）。
"""

from __future__ import annotations

from typing import Any

import pytest

from src.features.edit.core.clip_params import SpeedParams
from src.features.edit.core.match_frame import (
    find_source_frame_uses,
    resolve_match_frame,
    resolve_match_frame_at_playhead,
)
from src.features.edit.core.timeline_v2 import (
    ClipInstance,
    EditTrack,
    Rational,
    TimelineSequence,
    TransitionInstance,
)

pytestmark = pytest.mark.edit_core


def _clip(clip_id: str = "c1", track_id: str = "v1", **kwargs: Any
          ) -> ClipInstance:
    base: dict[str, Any] = dict(
        clip_id=clip_id, asset_id="asset", track_id=track_id,
        timeline_start=0, timeline_duration=100,
        source_start=0, source_duration=100,
    )
    base.update(kwargs)
    return ClipInstance(**base)


def _timeline(*clips: ClipInstance, transitions: tuple = (),
              locked: bool = False) -> TimelineSequence:
    video_tracks = []
    by_track: dict[str, list[ClipInstance]] = {}
    for clip in clips:
        by_track.setdefault(clip.track_id, []).append(clip)
    for track_id, members in by_track.items():
        video_tracks.append(EditTrack(
            track_id, track_id.upper(), "video", 0,
            clips=tuple(sorted(members, key=lambda c: c.timeline_start)),
            transitions=transitions if len(video_tracks) == 0 else (),
            is_locked=locked))
    return TimelineSequence(
        "b06", "B06", width=64, height=48, fps=Rational(24, 1),
        video_tracks=tuple(video_tracks), audio_tracks=())


# ── 权威映射：retime / reverse / freeze / clamp ────────────────────


def test_b06_plain_clip_maps_linearly():
    timeline = _timeline(_clip(source_start=10))
    result = resolve_match_frame(timeline, "c1", 40)
    assert result.ok
    assert result.source_frame == 50  # 10 + 40


def test_b06_retimed_clip_uses_authoritative_mapping():
    """2x 变速：播放头 25 → 源帧 50（朴素公式会给 25）。"""
    retimed = _clip(timeline_duration=50, speed=SpeedParams(speed=2.0))
    timeline = _timeline(retimed)
    result = resolve_match_frame(timeline, "c1", 25)
    assert result.ok
    assert result.source_frame == 50


def test_b06_reverse_clip_maps_backward():
    reverse = _clip(speed=SpeedParams(speed=1.0, reverse=True))
    timeline = _timeline(reverse)
    result = resolve_match_frame(timeline, "c1", 10)
    assert result.ok
    assert result.source_frame == 89  # 100 帧倒放：offset 10 → 99-10


def test_b06_frozen_clip_maps_to_freeze_frame():
    frozen = _clip().with_freeze_frame(42)
    timeline = _timeline(frozen)
    result = resolve_match_frame(timeline, "c1", 77)
    assert result.ok
    assert result.source_frame == 42


def test_b06_playhead_outside_clip_clamps_to_span():
    timeline = _timeline(_clip(source_start=0))
    result = resolve_match_frame(timeline, "c1", 500)  # 远超片段尾部
    assert result.ok
    assert result.source_frame == 99  # 夹到片段最后一帧


def test_b06_motion_graphic_has_no_source_media():
    mg = _clip(asset_id="motion_graphic:mg1")
    timeline = _timeline(mg)
    result = resolve_match_frame(timeline, "c1", 10)
    assert not result.ok
    assert result.reason == "no_source_media"


def test_b06_unknown_clip_reason():
    timeline = _timeline(_clip())
    result = resolve_match_frame(timeline, "nope", 10)
    assert not result.ok
    assert result.reason == "clip_not_found"


# ── 播放头策略：选中优先 / 覆盖片段 / 转场选中侧 ───────────────────


def test_b06_at_playhead_selection_wins_over_covering():
    a = _clip("a", timeline_start=0, timeline_duration=100, source_start=0)
    b = _clip("b", timeline_start=50, timeline_duration=100,
              track_id="v2", source_start=500)
    timeline = _timeline(a, b)
    # 播放头 60 同时被 a/b 覆盖；显式选中 b → b 的映射
    result = resolve_match_frame_at_playhead(
        timeline, 60, selected_clip_ids=("b",))
    assert result.ok and result.clip_id == "b"
    assert result.source_frame == 510


def test_b06_at_playhead_no_selection_uses_first_video_track():
    a = _clip("a", timeline_start=0, timeline_duration=100, source_start=0)
    b = _clip("b", timeline_start=0, timeline_duration=100,
              track_id="v2", source_start=500)
    timeline = _timeline(a, b)
    result = resolve_match_frame_at_playhead(timeline, 10)
    assert result.ok and result.clip_id == "a"


def test_b06_at_playhead_nothing_covering_rejects():
    timeline = _timeline(_clip())
    result = resolve_match_frame_at_playhead(timeline, 150)
    assert not result.ok
    assert result.reason == "outside_clip"


def test_b06_transition_selected_side_wins():
    """转场覆盖剪辑点：双击/选中哪一侧就用哪一侧的映射。"""
    a = _clip("a", timeline_start=0, timeline_duration=100, source_start=0)
    b = _clip("b", timeline_start=100, timeline_duration=100,
              source_start=200)
    transition = (TransitionInstance(
        transition_id="t1", track_id="v1", left_clip_id="a",
        center_frame=100, duration=20, alignment="centered"),)
    timeline = _timeline(a, b, transitions=transition)

    left = resolve_match_frame(timeline, "a", 100)   # 播放头在剪辑点
    right = resolve_match_frame(timeline, "b", 100)
    assert left.ok and left.source_frame == 99       # a 的最后一帧
    assert right.ok and right.source_frame == 200    # b 的第一帧


# ── 反向 Match Frame：候选列表只列不跳 ─────────────────────────────


def test_b06_find_source_frame_uses_lists_candidates_in_order():
    a = _clip("a", timeline_start=0, timeline_duration=100, source_start=0)
    b = _clip("b", timeline_start=50, timeline_duration=100,
              track_id="v2", source_start=10)  # 源窗口 [10,109] 含帧 60
    c = _clip("c", timeline_start=200, timeline_duration=100,
              source_start=30)
    d = _clip("d", timeline_start=300, timeline_duration=100,
              source_start=900)  # 同轨但源窗口不含帧 60
    timeline = _timeline(a, b, c, d)

    uses = find_source_frame_uses(timeline, "asset", 60)

    # 轨道存储序优先（v1 → v2），同轨按时间线起点
    assert [u.clip_id for u in uses] == ["a", "c", "b"]
    assert all(u.asset_id == "asset" for u in uses)
    # 只列不跳：返回纯数据，宿主负责弹选择器


def test_b06_find_source_frame_uses_empty_when_none():
    timeline = _timeline(_clip(source_start=900))
    assert find_source_frame_uses(timeline, "asset", 5) == ()
    assert find_source_frame_uses(timeline, "other", 0) == ()
