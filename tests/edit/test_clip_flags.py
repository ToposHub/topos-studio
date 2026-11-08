"""W05：clip 级彩色旗标（契约 D4）——模型/命令/序列化/宿主接线。

旗标独立于 MediaAsset.flag（素材级 reject）与 label_color（配色）；
多选批量单步 undo；"" 清除归一 None；无目标零写入；未知色读取保留、
绘制 fallback；旧工程缺字段读为 None；split/复制自动继承。
"""

from __future__ import annotations

from typing import Any

import pytest

from src.features.edit.core.commands import SetClipFlagCommand
from src.features.edit.core.timeline_v2 import (
    CLIP_FLAG_COLORS,
    ClipInstance,
    EditTrack,
    Rational,
    TimelineSequence,
)

pytestmark = pytest.mark.edit_ui


def _timeline() -> TimelineSequence:
    clips = tuple(
        ClipInstance(
            clip_id=f"c{i}", asset_id="a", track_id="v1",
            timeline_start=i * 50, timeline_duration=40,
            source_start=0, source_duration=100,
        ) for i in range(2)
    )
    return TimelineSequence(
        timeline_id="flags", name="Flags", width=64, height=48,
        fps=Rational(24, 1),
        video_tracks=(EditTrack("v1", "V1", "video", 0, clips=clips),),
        audio_tracks=(EditTrack("a1", "A1", "audio", 0),),
    )


def _view(qtbot: Any) -> Any:
    from src.features.edit.ui.timeline_view import TimelineView

    view = TimelineView()
    qtbot.addWidget(view)
    view.load_timeline(_timeline())
    return view


def _select(view: Any, *clip_ids: str) -> None:
    from src.features.edit.core.selection import TimelineSelection

    selection = TimelineSelection()
    for index, clip_id in enumerate(clip_ids):
        selection = selection.with_clip_selected(
            clip_id, "v1", "flags", replace=(index == 0))
    view.set_selection(selection)


# ── 模型与序列化 ──────────────────────────────────────────────────────


def test_flag_color_field_default_and_roundtrip() -> None:
    clip = ClipInstance("x", "a", "v1", 0, 10, 0, 10)
    assert clip.flag_color is None
    flagged = clip.with_flag_color(CLIP_FLAG_COLORS[0])
    assert flagged.flag_color == CLIP_FLAG_COLORS[0]
    data = flagged.to_dict()
    assert data["flag_color"] == CLIP_FLAG_COLORS[0]
    revived = ClipInstance.from_dict(data)
    assert revived.flag_color == CLIP_FLAG_COLORS[0]
    # 旧工程缺字段 → None
    legacy = {k: v for k, v in data.items() if k != "flag_color"}
    assert ClipInstance.from_dict(legacy).flag_color is None
    # 未知颜色值读取保留（不丢字段）
    unknown = dict(data, flag_color="#123456")
    assert ClipInstance.from_dict(unknown).flag_color == "#123456"


# ── 命令层 ────────────────────────────────────────────────────────────


def test_set_flag_command_batch_and_undo() -> None:
    timeline = _timeline()
    cmd = SetClipFlagCommand.create(
        (("v1", "c0"), ("v1", "c1")), CLIP_FLAG_COLORS[4])
    new_timeline = cmd.apply(timeline)
    assert new_timeline.get_clip_by_id("c0").flag_color == CLIP_FLAG_COLORS[4]
    assert new_timeline.get_clip_by_id("c1").flag_color == CLIP_FLAG_COLORS[4]
    # label_color / 足迹不受影响
    assert new_timeline.get_clip_by_id("c0").timeline_start == 0
    assert new_timeline.get_clip_by_id("c0").label_color is None
    restored = cmd.undo(new_timeline)
    assert restored.get_clip_by_id("c0").flag_color is None
    assert restored.get_clip_by_id("c1").flag_color is None
    # 序列化往返
    revived = SetClipFlagCommand.from_dict(cmd.to_dict())
    assert revived.targets == (("v1", "c0"), ("v1", "c1"))


def test_set_flag_command_rejects_malformed_color() -> None:
    timeline = _timeline()
    with pytest.raises(ValueError):
        SetClipFlagCommand.create(
            (("v1", "c0"),), "red").apply(timeline)


def test_flag_noop_does_not_enter_history() -> None:
    from src.features.edit.core.commands import CommandStack

    timeline = _timeline()
    flagged = timeline.get_clip_by_id("c0").with_flag_color(CLIP_FLAG_COLORS[0])
    timeline = timeline.with_track_updated(
        timeline.video_tracks[0]._rebuild(clips=(
            flagged, timeline.get_clip_by_id("c1"))))
    stack = CommandStack()
    cmd = SetClipFlagCommand.create((("v1", "c0"),), CLIP_FLAG_COLORS[0])
    stack.execute(cmd, timeline)
    # CommandStack no-op 同一性判定：不 push
    assert not stack._undo_stack


# ── view 层（含 W09 宿主桥） ──────────────────────────────────────────


def test_view_flags_selected_clips_single_undo(qtbot: Any) -> None:
    view = _view(qtbot)
    _select(view, "c0", "c1")
    assert view.set_clip_flags(CLIP_FLAG_COLORS[2]) is True
    timeline = view.get_timeline()
    assert timeline.get_clip_by_id("c0").flag_color == CLIP_FLAG_COLORS[2]
    assert timeline.get_clip_by_id("c1").flag_color == CLIP_FLAG_COLORS[2]
    assert len(view._command_stack._undo_stack) == 1
    view.undo()
    assert view.get_timeline().get_clip_by_id("c0").flag_color is None


def test_view_clear_and_playhead_fallback(qtbot: Any) -> None:
    from src.features.edit.core.selection import TimelineSelection

    view = _view(qtbot)
    _select(view, "c0")
    view.set_clip_flags(CLIP_FLAG_COLORS[1])
    assert view.get_timeline().get_clip_by_id("c0").flag_color == \
        CLIP_FLAG_COLORS[1]
    # 清除选中目标
    assert view.set_clip_flags(None) is True
    assert view.get_timeline().get_clip_by_id("c0").flag_color is None
    # 无选中 → 播放头下 clip（c1: [50, 90)）
    view.set_selection(TimelineSelection())
    view._playhead_frame = 60
    assert view.set_clip_flags(CLIP_FLAG_COLORS[3]) is True
    assert view.get_timeline().get_clip_by_id("c1").flag_color == \
        CLIP_FLAG_COLORS[3]
    assert view.get_timeline().get_clip_by_id("c0").flag_color is None


def test_view_rejects_unknown_color(qtbot: Any) -> None:
    view = _view(qtbot)
    messages: list[str] = []
    view.status_message_requested.connect(messages.append)
    assert view.set_clip_flags("#FF00FF") is False
    assert not view._command_stack._undo_stack
    assert messages


def test_flag_not_propagated_to_linked_partner(qtbot: Any) -> None:
    view = _view(qtbot)
    timeline = view.get_timeline()
    # 给 c0 建一个 a1 轨上的 linked 伙伴
    partner = ClipInstance(
        "ac0", "a", "a1", 0, 40, 0, 100, linked_clip_id="c0")
    c0 = timeline.get_clip_by_id("c0")
    timeline = timeline.with_track_updated(
        timeline.video_tracks[0]._rebuild(clips=(
            type(c0)(**{**c0.__dict__, "linked_clip_id": "ac0"}),
            timeline.get_clip_by_id("c1"))))
    timeline = timeline.with_track_updated(
        timeline.audio_tracks[0]._rebuild(clips=(partner,)))
    view.load_timeline(timeline)
    _select(view, "c0")
    view.set_clip_flags(CLIP_FLAG_COLORS[5])
    new_timeline = view.get_timeline()
    assert new_timeline.get_clip_by_id("c0").flag_color == CLIP_FLAG_COLORS[5]
    assert new_timeline.get_clip_by_id("ac0").flag_color is None


def test_flag_requested_from_toolbar_reaches_view(
        qtbot: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from src.features.edit.ui.edit_page import EditPage
    from src.features.edit.ui.timeline_panel import TimelinePanel

    monkeypatch.setattr(EditPage, "_ensure_audio_manager", lambda self: None)
    page = EditPage()
    qtbot.addWidget(page)
    panel = TimelinePanel()
    qtbot.addWidget(panel)
    panel.load_timeline(_timeline())
    page.set_timeline_panel(panel)

    _select(panel.get_timeline_view(), "c0")
    panel.flag_requested.emit(CLIP_FLAG_COLORS[0])
    assert panel.get_timeline_view().get_timeline().get_clip_by_id(
        "c0").flag_color == CLIP_FLAG_COLORS[0]
    # 清除信号 "" → None
    panel.flag_requested.emit("")
    assert panel.get_timeline_view().get_timeline().get_clip_by_id(
        "c0").flag_color is None
