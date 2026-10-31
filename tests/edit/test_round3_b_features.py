"""第三轮复查批B-3回归：N17–N20（词级字幕 / Source fps / 源切换历史 /
设置事务协议）。

对应 topos_round3_features.md RF01–RF04。
"""

from __future__ import annotations

import json

import pytest

from src.features.edit.core.clip_params import SpeedParams
from src.features.edit.core.commands import (
    InsertClipCommand,
    OverwriteClipCommand,
    UpdateClipSourceCommand,
)
from src.features.edit.core.timeline_v2 import (
    ClipInstance, EditTrack, TimelineSequence,
)

pytestmark = pytest.mark.edit_core


def _clip(cid="c", tid="v1", start=0, dur=100, ss=100, sd=100, **kw):
    return ClipInstance(
        clip_id=cid, asset_id=f"a-{cid}", track_id=tid,
        timeline_start=start, timeline_duration=dur,
        source_start=ss, source_duration=sd, **kw)


def _seq(clips, fps=24.0):
    tl = TimelineSequence(timeline_id="t1", name="T", fps=fps)
    tl = tl.with_track_added(EditTrack(
        track_id="v1", name="V1", track_type="video", index=0))
    for c in clips:
        tl = tl.with_clip_added("v1", c)
    return tl


# ------------------------------------------------------------------ N17

def test_n17_word_timings_scale_in_real_caption_metadata() -> None:
    """24→48：cue 与真实 caption_metadata.word_timings 同秒。"""
    from src.app.controllers.edit_controller import EditControllerMixin
    from src.features.edit.core.captions.model import (
        CaptionCueMetadata, CaptionWordTiming,
    )
    from src.shared.media.metadata import Rational

    class _Host(EditControllerMixin):
        pass

    tl = TimelineSequence(timeline_id="t1", name="T", fps=Rational(24, 1))
    tl = tl.with_track_added(EditTrack(
        track_id="s1", name="S", track_type="subtitle", index=0))
    tl = tl.with_subtitle_clip_added("s1", __import__(
        "src.features.edit.core.timeline_v2", fromlist=["SubtitleClip"]
    ).SubtitleClip(
        clip_id="sc", track_id="s1",
        timeline_start=24, timeline_duration=24, text="cue",
        caption_metadata=CaptionCueMetadata(
            speaker="spk",
            word_timings=(CaptionWordTiming(
                text="w", start_frame=24, end_frame=48),),
        ),
    ))
    rebuilt = _Host()._with_timeline_fps(tl, Rational(48, 1))
    scaled = rebuilt.subtitle_tracks[0].subtitle_clips[0]
    assert (scaled.timeline_start, scaled.timeline_duration) == (48, 48)
    word = scaled.caption_metadata.word_timings[0]
    assert (word.start_frame, word.end_frame) == (48, 96)
    assert scaled.caption_metadata.speaker == "spk"


# ------------------------------------------------------------------ N18

def test_n18_silent_overwrite_carries_fps_timebase() -> None:
    """无声视频 Overwrite 兜底分支带完整 fps：48fps 源 30 帧在 24fps
    序列 duration=15、采样 2 源帧/时间线帧（此前 48 帧 1:1 采样）。"""
    tl = _seq([_clip("exist", "v1", 0, 100, ss=0, sd=100)])
    cmd = OverwriteClipCommand.create(
        asset_id="silent", in_frame=0, out_frame=29, playhead=110,
        track_id="v1",
        source_fps_num=48, source_fps_den=1,
        timeline_fps_num=24, timeline_fps_den=1,
    )
    tl2, updated = cmd.apply(tl)
    new_clip = next(
        c for c in tl2.get_track_by_id("v1").clips
        if c.asset_id == "silent")
    assert new_clip.timeline_duration == 15
    assert new_clip._source_fps_num == 48
    assert new_clip._timeline_fps_num == 24
    # 采样域：每时间线帧消耗 2 源帧
    assert new_clip.timeline_to_source_frame(
        new_clip.timeline_start + 1) == 2


# ------------------------------------------------------------------ N19

def test_n19_source_swap_undo_restores_full_payload() -> None:
    """A24fps → B48fps → Undo：完整还原 fps/源窗/时长/PTS。"""
    tl = _seq([_clip("c", "v1", 0, 72, ss=0, sd=200,
                     _source_fps_num=24, _source_fps_den=1,
                     _timeline_fps_num=24, _timeline_fps_den=1,
                     _source_pts_index=(0, 1, 2))])
    cmd = UpdateClipSourceCommand(
        command_id="swap", label="swap", track_id="v1", clip_id="c",
        new_asset_id="B", new_source_start=24,
        _new_source_fps_num=48, _new_source_fps_den=1,
    )
    tl2, updated = cmd.apply(tl)
    swapped = tl2.get_clip_by_id("c")
    assert swapped.asset_id == "B"
    assert swapped._source_fps_num == 48
    assert swapped.timeline_duration == 72  # 时间线窗口保持
    # 换源后 PTS 不复用旧流 cadence
    assert swapped._source_pts_index is None

    tl3 = updated.undo(tl2)
    restored = tl3.get_clip_by_id("c")
    assert restored.asset_id == "a-c"
    assert restored._source_fps_num == 24
    assert restored.source_duration == 200
    assert restored.timeline_duration == 72
    assert restored._source_pts_index == (0, 1, 2)

    # JSON 载荷完整往返
    loaded = UpdateClipSourceCommand.from_dict(
        json.loads(json.dumps(updated.to_dict())))
    tl4 = loaded.undo(tl2)
    r4 = tl4.get_clip_by_id("c")
    assert r4._source_fps_num == 24
    assert r4.timeline_duration == 72


def test_n19_updated_command_keeps_fps_for_redo() -> None:
    tl = _seq([_clip("c", "v1", 0, 72, ss=0, sd=200,
                     _source_fps_num=24, _source_fps_den=1,
                     _timeline_fps_num=24, _timeline_fps_den=1)])
    cmd = UpdateClipSourceCommand(
        command_id="swap", label="swap", track_id="v1", clip_id="c",
        new_asset_id="B", new_source_start=24,
        _new_source_fps_num=48, _new_source_fps_den=1,
    )
    _, updated = cmd.apply(tl)
    assert updated._new_source_fps_num == 48


# ------------------------------------------------------------------ N20

def test_n20_settings_command_real_stack_protocol() -> None:
    from src.features.edit.core.commands import (
        ProjectCommandStack, UpdateTimelineSettingsCommand,
    )
    from src.features.edit.core.project import EditProject, TimelineSequenceRef
    from src.shared.media.metadata import Rational

    before = TimelineSequence(timeline_id="t1", name="T", fps=Rational(24, 1))
    after = TimelineSequence(timeline_id="t1", name="T48", fps=Rational(48, 1))
    project = EditProject(project_id="p1", name="P")
    project = project.__class__(**{
        **{f: getattr(project, f) for f in project.__dataclass_fields__},
        "timelines": {"t1": TimelineSequenceRef(
            timeline_id="t1", name="T", fps=24.0)},
        "revision": 7,
    })

    cmd = UpdateTimelineSettingsCommand.create("t1", before, after)
    stack = ProjectCommandStack()
    p2 = stack.execute(cmd, project)  # 此前 TypeError: replace() on tuple
    assert p2.revision == 8
    assert stack.last_sequence_updates["t1"].fps == Rational(48, 1)
    p3 = stack.undo(p2)
    assert p3.revision == 9
    assert stack.last_sequence_updates["t1"].fps == Rational(24, 1)
    p4 = stack.redo(p3)
    assert p4.revision == 10
    assert stack.last_sequence_updates["t1"].fps == Rational(48, 1)
