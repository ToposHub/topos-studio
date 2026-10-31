"""第六轮审计 阶段1C 回归：编辑完整性与字幕正确性（C02-C06/D08/D09）。

C02：跨轨移动（新建轨/已有轨）clip+effect 动画关键帧随时间位置平移。
C03：跨轨移动的转场迁移与 Undo 快照恢复。
C04：链接 A/V 变速 Undo 恢复音频轨原始转场时长。
C05：Freeze 复用 Split 的 marker/转场迁移事务。
C06：锁轨下关键帧/效果写命令零提交。
D08：ASS 导入 Text 列逗号完整保留（含本项目自导出往返）。
D09：TTML 按 tickRate/frameRate/multiplier 换算，不支持 timeBase 拒绝。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.features.edit.core.captions.importer import parse_subtitle_text
from src.features.edit.core.clip_params import SpeedParams
from src.features.edit.core.commands import (
    AddKeyframeCommand,
    ChangeClipSpeedCommand,
    FreezeFrameAtPlayheadCommand,
    MoveClipToExistingTrackCommand,
    MoveClipToTrackCommand,
    SetEffectEnabledCommand,
)
from src.features.edit.core.timeline_v2 import (
    ClipInstance,
    EditTrack,
    TimelineMarker,
    TimelineSequence,
    TransitionInstance,
)
from src.shared.keyframe import Keyframe, KeyframeTrack
from src.shared.media.metadata import Rational

pytestmark = pytest.mark.edit_core

FPS = 25


def _track(track_id: str, clips, transitions=(), locked=False) -> EditTrack:
    return EditTrack(
        track_id=track_id, name=track_id.upper(), track_type="video",
        index=0, clips=tuple(clips), transitions=tuple(transitions),
        is_locked=locked,
    )


def _clip(clip_id, track_id, start, duration, **kw) -> ClipInstance:
    return ClipInstance(clip_id, "asset", track_id, start, duration, 0, duration, **kw)


def _seq(video_tracks, audio_tracks=(), fps=FPS) -> TimelineSequence:
    return TimelineSequence(
        timeline_id="seq-c", name="C", width=64, height=48,
        fps=Rational(int(fps), 1),
        video_tracks=tuple(video_tracks),
        audio_tracks=tuple(audio_tracks),
    )


def _clip_with_opacity_kf(clip_id, track_id, start, duration, kfs):
    """clip 带 opacity 关键帧（绝对帧域）。"""
    animated = {}
    from src.features.edit.core.effect_instance import AnimatableParam

    animated["opacity"] = AnimatableParam(
        base_value=1.0, keyframes=None, min_value=0.0, max_value=1.0)
    track_kf = KeyframeTrack(
        param_name="opacity", min_val=0.0, max_val=1.0,
        keyframes=[Keyframe(f, v) for f, v in kfs])
    animated["opacity"] = AnimatableParam(
        base_value=1.0, keyframes=track_kf, min_value=0.0, max_value=1.0)
    clip = ClipInstance(clip_id, "asset", track_id, start, duration, 0, duration)
    object.__setattr__(clip, "animated_params", animated)
    return clip


def _opacity_kfs(clip) -> list[tuple[int, float]]:
    param = clip.animated_params["opacity"]
    return [(k.frame, k.value) for k in param.keyframes.keyframes]


# ── C02：跨轨移动动画平移 ──


class TestC02MoveAnimationShift:
    def test_new_track_move_shifts_keyframes(self):
        clip = _clip_with_opacity_kf("c1", "v1", 100, 100, [(100, 0.0), (199, 1.0)])
        seq = _seq([_track("v1", [clip])])

        cmd = MoveClipToTrackCommand(
            command_id="cmd", label="move", source_track_id="v1", clip_id="c1",
            target_track_type="video", create_new_track=True,
            new_timeline_start=300)
        seq2 = cmd.apply(seq)
        moved = seq2.video_tracks[-1].get_clip_by_id("c1")
        assert moved.timeline_start == 300
        assert _opacity_kfs(moved) == [(300, 0.0), (399, 1.0)], \
            "关键帧必须随片段平移（旧代码仍停在 100/199）"

        seq3 = cmd.undo(seq2)
        restored = seq3.get_track_by_id("v1").get_clip_by_id("c1")
        assert restored.timeline_start == 100
        assert _opacity_kfs(restored) == [(100, 0.0), (199, 1.0)]

    def test_existing_track_move_shifts_keyframes(self):
        clip = _clip_with_opacity_kf("c1", "v1", 100, 100, [(100, 0.0), (199, 1.0)])
        seq = _seq([_track("v1", [clip]), _track("v2", [])])

        cmd = MoveClipToTrackCommand(
            command_id="cmd", label="move", source_track_id="v1", clip_id="c1",
            target_track_type="video", create_new_track=False,
            target_track_id="v2", new_timeline_start=250)
        seq2 = cmd.apply(seq)
        moved = seq2.get_track_by_id("v2").get_clip_by_id("c1")
        assert _opacity_kfs(moved) == [(250, 0.0), (349, 1.0)]

        seq3 = cmd.undo(seq2)
        restored = seq3.get_track_by_id("v1").get_clip_by_id("c1")
        assert _opacity_kfs(restored) == [(100, 0.0), (199, 1.0)]

    def test_legacy_existing_track_command_shifts_too(self):
        clip = _clip_with_opacity_kf("c1", "v1", 50, 50, [(50, 0.0), (99, 1.0)])
        seq = _seq([_track("v1", [clip]), _track("v2", [])])

        cmd = MoveClipToExistingTrackCommand(
            command_id="cmd", label="move", source_track_id="v1",
            target_track_id="v2", clip_id="c1", new_timeline_start=150)
        seq2 = cmd.apply(seq)
        moved = seq2.get_track_by_id("v2").get_clip_by_id("c1")
        assert _opacity_kfs(moved) == [(150, 0.0), (199, 1.0)]
        seq3 = cmd.undo(seq2)
        assert _opacity_kfs(
            seq3.get_track_by_id("v1").get_clip_by_id("c1")) == [(50, 0.0), (99, 1.0)]


# ── C03：跨轨移动转场迁移与 Undo ──


def _tail_fade(track_id, clip_id, end_frame, duration=12):
    return TransitionInstance(
        transition_type="dip_to_color", track_id=track_id,
        left_clip_id=clip_id, right_clip_id="",
        center_frame=end_frame, duration=duration, alignment="end_on_cut")


class TestC03MoveTransitions:
    def test_new_track_move_single_sided_transition_migrates(self):
        clip = _clip("c1", "v1", 0, 100)
        fade = _tail_fade("v1", "c1", 100)
        seq = _seq([_track("v1", [clip], [fade])])
        assert len(seq.video_tracks[0].transitions) == 1

        cmd = MoveClipToTrackCommand(
            command_id="cmd", label="move", source_track_id="v1", clip_id="c1",
            target_track_type="video", create_new_track=True,
            new_timeline_start=200)
        seq2 = cmd.apply(seq)
        new_track = seq2.video_tracks[-1]
        moved = new_track.get_clip_by_id("c1")
        assert moved.timeline_start == 200
        trans = new_track.transitions
        assert len(trans) == 1, "单边尾淡出应随片段迁移到新轨"
        assert trans[0].track_id == new_track.track_id
        assert trans[0].center_frame == 200 + 100, "尾淡出应重锚到新结尾"

        seq3 = cmd.undo(seq2)
        src = seq3.get_track_by_id("v1")
        assert len(src.transitions) == 1
        assert src.transitions[0].center_frame == 100, "undo 后转场回原锚点"

    def test_existing_track_move_double_sided_dropped_and_restored(self):
        clip_a = _clip("cA", "v1", 0, 50)
        clip_b = _clip("cB", "v1", 50, 50)
        double = TransitionInstance(
            transition_type="cross_dissolve", track_id="v1",
            left_clip_id="cA", right_clip_id="cB",
            center_frame=50, duration=10, alignment="centered")
        seq = _seq([_track("v1", [clip_a, clip_b], [double]), _track("v2", [])])

        cmd = MoveClipToTrackCommand(
            command_id="cmd", label="move", source_track_id="v1", clip_id="cB",
            target_track_type="video", create_new_track=False,
            target_track_id="v2", new_timeline_start=100)
        seq2 = cmd.apply(seq)
        # 双边转场随移动失效（邻片留在源轨）
        assert len(seq2.get_track_by_id("v1").transitions) == 0
        assert len(seq2.get_track_by_id("v2").transitions) == 0

        seq3 = cmd.undo(seq2)
        assert len(seq3.get_track_by_id("v1").transitions) == 1, \
            "undo 必须恢复被移除的双边转场"
        t = seq3.get_track_by_id("v1").transitions[0]
        assert (t.left_clip_id, t.right_clip_id, t.center_frame) == ("cA", "cB", 50)

    def test_legacy_existing_track_transition_restore(self):
        clip = _clip("c1", "v1", 0, 100)
        fade = _tail_fade("v1", "c1", 100)
        seq = _seq([_track("v1", [clip], [fade]), _track("v2", [])])

        cmd = MoveClipToExistingTrackCommand(
            command_id="cmd", label="move", source_track_id="v1",
            target_track_id="v2", clip_id="c1", new_timeline_start=10)
        seq2 = cmd.apply(seq)
        # 单边尾淡出迁移并重锚
        v2 = seq2.get_track_by_id("v2")
        assert len(v2.transitions) == 1
        assert v2.transitions[0].center_frame == 110
        seq3 = cmd.undo(seq2)
        v1 = seq3.get_track_by_id("v1")
        assert len(v1.transitions) == 1
        assert v1.transitions[0].center_frame == 100


# ── C04：链接 A/V 变速 Undo 恢复音频转场 ──


class TestC04LinkedSpeedUndoTransition:
    def test_undo_restores_original_audio_transition(self):
        v = _clip("vc", "v0", 0, 100)
        a = ClipInstance("ac", "asset", "a0", 0, 100, 0, 100, linked_clip_id="vc")
        object.__setattr__(v, "linked_clip_id", "ac")
        audio_fade = TransitionInstance(
            transition_type="audio:constant_power", track_id="a0",
            left_clip_id="ac", right_clip_id="",
            center_frame=100, duration=80, alignment="end_on_cut")
        seq = TimelineSequence(
            timeline_id="seq-c4", name="C4", width=64, height=48,
            fps=Rational(25, 1),
            video_tracks=(_track("v0", [v]),),
            audio_tracks=(
                EditTrack("a0", "A1", "audio", 0, clips=(a,),
                          transitions=(audio_fade,)),
            ),
        )

        cmd = ChangeClipSpeedCommand.create("vc", "v0", SpeedParams(speed=2.0))
        seq2, applied = cmd.apply(seq)
        # 变速后音频片段缩短（100→50），转场被钳制
        a2 = seq2.get_track_by_id("a0").get_clip_by_id("ac")
        assert a2.timeline_duration == 50
        t2 = seq2.get_track_by_id("a0").transitions[0]
        assert t2.duration == 50

        seq3 = applied.undo(seq2)
        a3 = seq3.get_track_by_id("a0").get_clip_by_id("ac")
        assert a3.timeline_duration == 100
        t3 = seq3.get_track_by_id("a0").transitions[0]
        assert t3.duration == 80, \
            f"undo 应恢复原始 80 帧音频转场，实测 {t3.duration}"


# ── C05：Freeze 的 marker/转场迁移 ──


class TestC05FreezeMigration:
    def test_freeze_moves_markers_and_tail_fade(self):
        clip = _clip("c1", "v1", 0, 100)
        fade = _tail_fade("v1", "c1", 100)
        marker = TimelineMarker(marker_id="m", clip_id="c1", frame=75, color="#ff0000")
        seq = _seq([_track("v1", [clip], [fade])])
        seq = seq._rebuild(markers=(marker,))

        cmd = FreezeFrameAtPlayheadCommand.create("c1", 50)
        seq2 = cmd.apply(seq)

        # validate 语义：marker75 应挂在冻结右段（frame ≥ 50 的半段）
        right = None
        for t in seq2.all_tracks:
            for c in t.clips:
                if c.clip_id != "c1" and 50 <= c.timeline_start < 100:
                    right = c
        assert right is not None and right.freeze_source_frame is not None
        m2 = seq2.markers[0]
        assert m2.clip_id == right.clip_id, "marker75 必须迁到冻结右段（ owning clip 范围）"

        # 尾淡出应挂右段结尾（100），而不是左段结尾（50）
        t2 = seq2.get_track_by_id("v1").transitions[0]
        assert t2.left_clip_id == right.clip_id
        assert t2.center_frame == 100, \
            f"冻结段的尾部淡出应在最终结尾 100，实测 {t2.center_frame}"

        seq3 = cmd.undo(seq2)
        assert seq3.get_track_by_id("v1").get_clip_by_id("c1").timeline_duration == 100
        assert seq3.markers[0].clip_id == "c1"
        assert seq3.get_track_by_id("v1").transitions[0].center_frame == 100

    def test_freeze_validation_clean(self):
        """迁移后 validate_timeline 不报 marker 越界。"""
        from src.features.edit.core.timeline_ops import validate_timeline

        clip = _clip("c1", "v1", 0, 100)
        marker = TimelineMarker(marker_id="m", clip_id="c1", frame=75, color="#ff0000")
        seq = _seq([_track("v1", [clip])])
        seq = seq._rebuild(markers=(marker,))

        seq2 = FreezeFrameAtPlayheadCommand.create("c1", 50).apply(seq)
        errors = validate_timeline(seq2)
        marker_errors = [e for e in errors if "outside owning clip" in str(e)]
        assert not marker_errors, marker_errors


# ── C06：锁轨关键帧/效果写命令 ──


class TestC06LockedTrackWrites:
    @pytest.fixture
    def locked_seq(self):
        clip = _clip("c1", "v1", 0, 100)
        return _seq([_track("v1", [clip], locked=True)])

    def test_keyframe_add_rejected_on_locked_track(self, locked_seq):
        cmd = AddKeyframeCommand(
            command_id="k", label="kf", track_id="v1", clip_id="c1",
            param_name="opacity", frame=10, value=0.5)
        with pytest.raises(ValueError, match="locked"):
            cmd.apply(locked_seq)

    def test_effect_toggle_rejected_on_locked_track(self, locked_seq):
        cmd = SetEffectEnabledCommand(
            command_id="e", label="fx", track_id="v1", clip_id="c1",
            effect_id="fx1", new_enabled=False)
        with pytest.raises(ValueError, match="locked"):
            cmd.apply(locked_seq)

    def test_undo_still_works_on_locked_track(self):
        """锁轨不拦 undo——撤销必须能恢复合法历史。"""
        clip = _clip("c1", "v1", 0, 100)
        seq = _seq([_track("v1", [clip])])
        cmd = MoveClipToTrackCommand(
            command_id="cmd", label="move", source_track_id="v1", clip_id="c1",
            target_track_type="video", create_new_track=True,
            new_timeline_start=300)
        seq2 = cmd.apply(seq)
        # 之后锁轨
        locked = seq2._rebuild(video_tracks=tuple(
            t if t.track_id != "v1" else t._rebuild(is_locked=True)
            for t in seq2.video_tracks))
        seq3 = cmd.undo(locked)
        assert seq3.get_track_by_id("v1").get_clip_by_id("c1").timeline_start == 0


# ── D08：ASS Text 逗号保留 ──


ASS_WITH_COMMAS = """[Script Info]
Title: comma test

[V4+ Styles]
Format: Name, Fontname
Style: Default,Arial

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:01.00,0:00:02.00,Default,,0,0,0,,Hello, world
Dialogue: 0,0:00:03.00,0:00:04.00,Default,,0,0,0,,你好，世界, Good morning, twice
"""


class TestD08AssCommas:
    def test_commas_preserved_in_text(self):
        cues = parse_subtitle_text(ASS_WITH_COMMAS, ".ass", 25.0)
        assert [c.text for c in cues] == [
            "Hello, world",
            "你好，世界, Good morning, twice",
        ]

    def test_roundtrip_with_project_export_format(self):
        """与本项目 ASS 导出同构的行重导入逐字一致。"""
        exported = (
            "Dialogue: 0,0:00:00.40,0:00:02.00,Default,,0,0,0,,"
            "Hello, world\\Nsecond, line"
        )
        content = ASS_WITH_COMMAS.replace(
            "Dialogue: 0,0:00:01.00,0:00:02.00,Default,,0,0,0,,Hello, world\n",
            exported + "\n",
        ).replace(
            "Dialogue: 0,0:00:03.00,0:00:04.00,Default,,0,0,0,,"
            "你好，世界, Good morning, twice\n", "")
        cues = parse_subtitle_text(content, ".ass", 25.0)
        assert len(cues) == 1
        assert cues[0].text == "Hello, world\nsecond, line"

    def test_ssa_variant_with_marked(self):
        ssa = """[Events]
Format: Marked, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: Marked=0,0:00:01.00,0:00:02.00,Default,,0,0,0,,a, b, c
"""
        cues = parse_subtitle_text(ssa, ".ssa", 25.0)
        assert len(cues) == 1
        assert cues[0].text == "a, b, c"

    def test_no_format_line_sniffs_ass_layout(self):
        raw = "[Events]\nDialogue: 0,0:00:01.00,0:00:02.00,Default,,0,0,0,,x, y\n"
        cues = parse_subtitle_text(raw, ".ass", 25.0)
        assert cues[0].text == "x, y"


# ── D09：TTML 时间基准 ──


class TestD09TtmlTimebase:
    def _ttml(self, attrs: str, body: str) -> str:
        return (
            '<tt xmlns="http://www.w3.org/ns/ttml" '
            'xmlns:ttp="http://www.w3.org/ns/ttml#parameter" '
            f'{attrs}>'
            '<body><div>'
            f'{body}'
            '</div></body></tt>'
        )

    def test_tick_rate_respected(self):
        content = self._ttml(
            'ttp:tickRate="10" ttp:timeBase="media"',
            '<p begin="10t" end="20t">tick</p>')
        cues = parse_subtitle_text(content, ".ttml", 24.0)
        assert len(cues) == 1
        # 10t @ tickRate=10 → 1s → 24fps 第 24 帧
        assert cues[0].start_frame == 24
        assert cues[0].duration_frames == 24

    def test_frame_rate_and_multiplier_respected(self):
        content = self._ttml(
            'ttp:frameRate="30" ttp:frameRateMultiplier="1000 1001"',
            '<p begin="0f" end="30f">frames</p>')
        cues = parse_subtitle_text(content, ".ttml", 24.0)
        # 30f @ 30*1000/1001 fps ≈ 1.001s → 24fps ≈ 24 帧
        assert cues[0].start_frame == 0
        assert abs((cues[0].duration_frames) - 24) <= 1

    def test_nested_begin_accumulates(self):
        content = self._ttml(
            'ttp:tickRate="10"',
            '<div begin="5t"><p begin="5t" end="20t">nested</p></div>')
        cues = parse_subtitle_text(content, ".ttml", 24.0)
        assert cues[0].start_frame == 24, "嵌套 begin 应累加（5t+5t=1s）"

    def test_unsupported_timebase_rejected(self):
        content = self._ttml(
            'ttp:timeBase="smpte"',
            '<p begin="00:00:01:00" end="00:00:02:00">smpte</p>')
        with pytest.raises(ValueError, match="timeBase"):
            parse_subtitle_text(content, ".ttml", 24.0)

    def test_dur_endpoint(self):
        content = self._ttml(
            'ttp:tickRate="10"',
            '<p begin="10t" dur="10t">dur</p>')
        cues = parse_subtitle_text(content, ".ttml", 24.0)
        assert cues[0].start_frame == 24
        assert cues[0].duration_frames == 24
