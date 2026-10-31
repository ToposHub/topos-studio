"""时间线命令的片段字段保留回归测试。

背景（2026-09-07 复查发现）：SplitClipCommand / TrimClipCommand.undo /
SlipClipCommand.undo / UpdateClipParamCommand / FreezeFrameAtPlayheadCommand /
ReplaceClipAssetCommand 曾经手工重建 ClipInstance，丢掉 blend_mode /
audio_gain_db / freeze_source_frame / label_color / waveform_show_channels /
fps 元数据等字段。修复后统一走 dataclasses.replace()。

本文件锁定契约：凡"从既有片段派生新片段"的命令路径，除命令明确要改的
字段外，其余字段必须原样保留。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.features.edit.core.clip_params import (  # noqa: E402
    ClipBlendMode,
    CropParams,
    SpeedParams,
    TransformParams,
)
from src.features.edit.core.commands import (  # noqa: E402
    FreezeFrameAtPlayheadCommand,
    ReplaceClipAssetCommand,
    SlipClipCommand,
    SplitClipCommand,
    TrimClipCommand,
    UpdateClipParamCommand,
)
from src.features.edit.core.timeline_v2 import (  # noqa: E402
    ClipInstance,
    EditTrack,
    Rational,
    TimelineSequence,
)

BLEND = ClipBlendMode.SCREEN
GAIN = -6.0
LABEL = "#ff5500"


def _clip(**overrides) -> ClipInstance:
    fields = dict(
        clip_id="c1",
        asset_id="asset_x",
        track_id="v1",
        timeline_start=0,
        timeline_duration=96,
        source_start=100,
        source_duration=96,
        blend_mode=BLEND,
        audio_gain_db=GAIN,
        label_color=LABEL,
        waveform_show_channels=True,
    )
    fields.update(overrides)
    return ClipInstance(**fields)


def _sequence(*clips: ClipInstance) -> TimelineSequence:
    track = EditTrack(
        track_id="v1", name="V1", track_type="video", index=0,
        clips=tuple(clips),
    )
    return TimelineSequence(video_tracks=(track,), fps=Rational(24, 1))


def _clips(seq: TimelineSequence) -> list[ClipInstance]:
    return [c for t in seq.all_tracks for c in t.clips]


def _assert_decorators_kept(clip: ClipInstance, where: str) -> None:
    assert clip.blend_mode == BLEND, f"{where}: blend_mode 被重置为 {clip.blend_mode}"
    assert clip.audio_gain_db == GAIN, f"{where}: audio_gain_db 被重置为 {clip.audio_gain_db}"
    assert clip.label_color == LABEL, f"{where}: label_color 被重置为 {clip.label_color}"
    assert clip.waveform_show_channels is True, (
        f"{where}: waveform_show_channels 被重置"
    )


class TestClipWithMethodsKeepUnchangedFields:
    def test_single_field_helpers_keep_waveform_and_label(self) -> None:
        original = _clip()
        keyed = original.with_keyframe_added("opacity", 10, 0.5)
        variants = [
            original.with_timeline_start(10),
            original.with_timeline_duration(80),
            original.with_source_range(new_start=110, new_duration=80),
            original.with_speed(2.0),
            original.with_trim_left(10, 110),
            original.with_trim_right(80, 80),
            original.with_transform(TransformParams(translate_x=12.0)),
            original.with_crop(CropParams(left=0.1)),
            original.with_opacity(0.5),
            original.with_speed_params(SpeedParams(speed=2.0)),
            original.with_enabled(False),
            original.with_linked_clip_id("a1"),
            original.with_fade_in(5),
            original.with_fade_out(5),
            original.with_fade(5, 6),
            original.with_composite_id("comp1"),
            original.with_render_cache_id("cache1"),
            original.with_composite_bypassed(True),
            original.with_asset_id("asset_y"),
            original.with_color_stack("grade1", True),
            keyed,
            keyed.with_keyframe_updated("opacity", 10, 0.75),
            keyed.with_keyframe_removed("opacity", 10),
        ]

        for index, variant in enumerate(variants):
            _assert_decorators_kept(variant, f"with helper #{index}")


class TestSplitKeepsFields:
    def test_split_keeps_blend_gain_label_on_both_segments(self) -> None:
        seq = _sequence(_clip())
        cmd = SplitClipCommand(
            command_id="x", label="l", track_id="v1",
            clip_id="c1", split_frame=48,
        )
        segments = _clips(cmd.apply(seq))
        assert len(segments) == 2
        for seg in segments:
            _assert_decorators_kept(seg, "split")


class TestTrimUndoKeepsFields:
    def test_right_trim_undo_restores_duration_and_decorators(self) -> None:
        seq = _sequence(_clip())
        cmd = TrimClipCommand.create_right_trim(
            "v1", "c1", 48, 48, old_duration=96, old_source_duration=96,
        )
        undone = cmd.undo(cmd.apply(seq))
        (clip,) = _clips(undone)
        assert clip.timeline_duration == 96
        _assert_decorators_kept(clip, "trim undo")

    def test_left_trim_undo_restores_start_and_decorators(self) -> None:
        seq = _sequence(_clip(timeline_start=10))
        cmd = TrimClipCommand.create_left_trim(
            "v1", "c1", 20, 110,
            old_start=10, old_source_start=100,
            old_duration=96, old_source_duration=96,
        )
        undone = cmd.undo(cmd.apply(seq))
        (clip,) = _clips(undone)
        assert clip.timeline_start == 10
        _assert_decorators_kept(clip, "left-trim undo")


class TestSlipUndoKeepsFields:
    def test_slip_undo_restores_source_start_and_decorators(self) -> None:
        seq = _sequence(_clip(source_duration=200))
        cmd = SlipClipCommand(
            command_id="x", label="l", track_id="v1", clip_id="c1",
            delta_frames=10, old_source_start=100, old_source_duration=96,
        )
        undone = cmd.undo(seq)
        (clip,) = _clips(undone)
        assert clip.source_start == 100
        _assert_decorators_kept(clip, "slip undo")


class TestUpdateClipParamKeepsFields:
    def test_opacity_update_keeps_decorators(self) -> None:
        seq = _sequence(_clip())
        cmd = UpdateClipParamCommand(
            command_id="x", label="l", track_id="v1", clip_id="c1",
            param_type="opacity", new_value=0.5,
        )
        (clip,) = _clips(cmd.apply(seq))
        assert clip.opacity == 0.5
        _assert_decorators_kept(clip, "opacity update")

    def test_is_enabled_update_keeps_decorators(self) -> None:
        seq = _sequence(_clip())
        cmd = UpdateClipParamCommand(
            command_id="x", label="l", track_id="v1", clip_id="c1",
            param_type="is_enabled", new_value=False,
        )
        (clip,) = _clips(cmd.apply(seq))
        assert clip.is_enabled is False
        _assert_decorators_kept(clip, "is_enabled update")

    def test_speed_update_keeps_decorators(self) -> None:
        from src.features.edit.core.clip_params import SpeedParams

        seq = _sequence(_clip())
        cmd = UpdateClipParamCommand(
            command_id="x", label="l", track_id="v1", clip_id="c1",
            param_type="speed", new_value=SpeedParams(speed=2.0),
        )
        (clip,) = _clips(cmd.apply(seq))
        assert clip.timeline_duration == 48  # 96 / 2.0
        _assert_decorators_kept(clip, "speed update")


class TestFreezeKeepsFields:
    def test_frozen_segment_keeps_decorators(self) -> None:
        seq = _sequence(_clip())
        cmd = FreezeFrameAtPlayheadCommand.create("c1", 48)
        segments = _clips(cmd.apply(seq))
        frozen = [c for c in segments if c.clip_id != "c1"]
        assert len(frozen) == 1
        _assert_decorators_kept(frozen[0], "freeze")

    def test_frozen_segment_keeps_freeze_frame_mapping(self) -> None:
        seq = _sequence(_clip())
        cmd = FreezeFrameAtPlayheadCommand.create("c1", 48)
        frozen = [c for c in _clips(cmd.apply(seq)) if c.clip_id != "c1"][0]
        assert frozen.freeze_source_frame is not None
        assert frozen.timeline_to_source_frame(60) == frozen.freeze_source_frame
        assert frozen.timeline_to_source_frame(90) == frozen.freeze_source_frame


class TestReplaceAssetKeepsFields:
    def test_replace_asset_keeps_everything_but_asset(self) -> None:
        original = _clip(freeze_source_frame=140)
        seq = _sequence(original)
        cmd = ReplaceClipAssetCommand.create("c1", "v1", "asset_real")
        (clip,) = _clips(cmd.apply(seq))
        assert clip.asset_id == "asset_real"
        assert clip.timeline_start == original.timeline_start
        assert clip.timeline_duration == original.timeline_duration
        assert clip.speed == original.speed
        assert clip.freeze_source_frame == 140
        _assert_decorators_kept(clip, "replace asset")
