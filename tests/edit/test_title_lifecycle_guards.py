"""F2.9 · 标题/模板生命周期守卫与接线。

- 图形 clip 守卫：绑定字幕 cue 的图形 clip 禁止刀片 split（孤儿半段）
  与同步时序下的 trim（与 cue 失步）；detach 后 trim 允许（快照语义）。
- 模板生命周期：rename API（内容不变换名/同名拒绝）；保存→列出→应用
  到第二条时间线（cue 创建即模板复用）端到端。
"""

from __future__ import annotations

import pytest

from src.features.edit.core.captions.commands import (
    DetachCaptionGraphicCommand,
    PromoteSubtitleToGraphicCommand,
)
from src.features.edit.core.captions.model import CaptionCueMetadata
from src.features.edit.core.commands import (
    CommandStack,
    SplitClipCommand,
    TrimClipCommand,
)
from src.features.edit.core.timeline_v2 import (
    EditTrack,
    SubtitleClip,
    TimelineSequence,
)
from src.features.edit.core.title_templates import (
    list_user_title_templates,
    rename_title_template,
    save_title_template,
)

pytestmark = pytest.mark.edit_core


def _timeline() -> TimelineSequence:
    subtitle_track = EditTrack(
        track_id="sub-track", track_type="subtitle", name="Captions",
        subtitle_clips=(SubtitleClip(
            clip_id="cue-1", track_id="sub-track", timeline_start=10,
            timeline_duration=20, text="Hello",
            caption_metadata=CaptionCueMetadata(),
        ),),
    )
    return TimelineSequence(
        timeline_id="timeline-1", name="Test",
        video_tracks=(EditTrack(track_id="v1"),),
        subtitle_tracks=(subtitle_track,),
    )


def _promoted() -> TimelineSequence:
    stack = CommandStack()
    return stack.execute(
        PromoteSubtitleToGraphicCommand.create("cue-1", template_id="fade"),
        _timeline())


def _graphic_clip(timeline: TimelineSequence):
    track = next(
        t for t in timeline.video_tracks if t.track_role == "graphics")
    return track, track.clips[0]


class TestGraphicClipGuards:
    def test_split_bound_graphic_clip_rejected(self) -> None:
        """刀片切绑定图形 clip 会产生无绑定孤儿半段——拒绝并给指引。"""
        timeline = _promoted()
        track, clip = _graphic_clip(timeline)
        cmd = SplitClipCommand.create(
            track.track_id, clip.clip_id,
            clip.timeline_start + clip.timeline_duration // 2)
        with pytest.raises(ValueError, match="cannot be split"):
            cmd.apply(timeline)

    def test_trim_synced_graphic_clip_rejected(self) -> None:
        timeline = _promoted()
        track, clip = _graphic_clip(timeline)
        cmd = TrimClipCommand.create_left_trim(
            track.track_id, clip.clip_id,
            new_start=clip.timeline_start + 2,
            new_source_start=clip.source_start + 2)
        with pytest.raises(ValueError, match="driven by its subtitle cue"):
            cmd.apply(timeline)

    def test_trim_after_detach_allowed(self) -> None:
        """detach 落快照（sync 失效）后允许修剪——快照语义安全。"""
        timeline = _promoted()
        stack = CommandStack()
        timeline = stack.execute(
            DetachCaptionGraphicCommand.create("cue-1"), timeline)
        track, clip = _graphic_clip(timeline)
        binding = timeline.get_caption_graphic_binding_by_clip(clip.clip_id)
        assert binding is None or not binding.sync_timing
        cmd = TrimClipCommand.create_left_trim(
            track.track_id, clip.clip_id,
            new_start=clip.timeline_start + 2,
            new_source_start=clip.source_start + 2)
        trimmed = cmd.apply(timeline)
        new_clip = trimmed.get_clip_by_id(clip.clip_id)
        assert new_clip.timeline_duration == clip.timeline_duration - 2

    def test_regular_clip_unaffected(self) -> None:
        """无绑定的普通视频 clip 不受守卫影响（回归保护）。"""
        timeline = TimelineSequence(
            timeline_id="t2", name="T",
            video_tracks=(EditTrack(track_id="v1"),),
        )
        timeline = timeline.with_clip_added("v1", __import__(
            "src.features.edit.core.timeline_v2", fromlist=["ClipInstance"]
        ).ClipInstance(
            "plain", "a", "v1", 0, 100, 0, 100))
        cmd = SplitClipCommand.create("v1", "plain", 50)
        split = cmd.apply(timeline)
        v1 = split.get_track_by_id("v1")
        assert len(v1.clips) == 2


class TestTemplateRenameLifecycle:
    def test_rename_keeps_style_and_old_removed(self, tmp_path, monkeypatch):
        from src.features.edit.core import title_templates as tt

        monkeypatch.chdir(tmp_path)
        preset = save_title_template(
            "采访条 A", style=__import__(
                "src.features.edit.core.captions.model", fromlist=["CaptionStyle"]
            ).CaptionStyle(), category="lower_third")
        renamed = rename_title_template(preset.preset_id, "采访条 B")
        labels = [p.label for p in list_user_title_templates()]
        assert labels == ["采访条 B"]
        assert renamed.category == "lower_third"

    def test_rename_duplicate_rejected(self, tmp_path, monkeypatch):
        from src.features.edit.core.captions.model import CaptionStyle
        from src.features.edit.core import title_templates as tt

        monkeypatch.chdir(tmp_path)
        first = save_title_template("A", style=CaptionStyle())
        save_title_template("B", style=CaptionStyle())
        with pytest.raises(ValueError, match="duplicate"):
            rename_title_template(first.preset_id, "B")
        # 失败不留半更新：A/B 原样
        assert sorted(p.label for p in list_user_title_templates()) == ["A", "B"]

    def test_saved_template_applies_to_second_timeline(self, tmp_path, monkeypatch):
        """保存→列出→另一条时间线创建 cue（模板跨时间线复用）。"""
        from src.features.edit.core.captions.model import CaptionStyle
        from src.features.edit.core.commands import (
            AddSubtitleClipCommand,
            UpdateSubtitleClipCommand,
        )

        monkeypatch.chdir(tmp_path)
        style = CaptionStyle()
        preset = save_title_template("片头主标题", style=style, category="title")
        templates = list_user_title_templates()
        assert [p.preset_id for p in templates] == [preset.preset_id]

        # 第二条时间线：模板样式落新 cue（模拟 _on_title_template_selected
        # 的 Add+Update 两步生产路径）
        other = TimelineSequence(
            timeline_id="timeline-2", name="Second",
            video_tracks=(EditTrack(track_id="v1"),),
            subtitle_tracks=(EditTrack(
                track_id="sub2", track_type="subtitle"),),)
        stack = CommandStack()
        other = stack.execute(
            AddSubtitleClipCommand.create(
                "sub2", timeline_start=0, duration=30,
                text=preset.label), other)
        cue_id = next(iter(other.subtitle_tracks[0].subtitle_clips)).clip_id
        other = stack.execute(
            UpdateSubtitleClipCommand.create(
                cue_id, {"style_override": preset.style}), other)
        cue = other.get_subtitle_clip_by_id(cue_id)
        assert cue.style_override == preset.style
        # 两步单栈单 Undo：撤销后 cue 回到无覆盖
        other = stack.undo(other)
        assert other.get_subtitle_clip_by_id(cue_id).style_override is None
