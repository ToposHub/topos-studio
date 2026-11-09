"""R02：Source drop 的工程与历史事务（单宏命令、失败零痕迹）。

复现（2026-11-09 复查 R02）：需要新轨时先执行 AddTrack、插入失败后用
公开 Undo 回滚——轨数虽恢复，但 revision 0→2、Undo 清空、Redo 残留 1
项（再 Redo 复活空轨），且 AddTrack 已先清掉用户原有 Redo 历史。

契约（修复后）：

- 建轨＋插入（含配对音频/ripple）合并为一个 MacroCommand 提交；
- 失败（验证拒绝 / 命令构造失败 / 宏应用抛异常）：零 revision、零
  Undo/Redo 变化、既有 Redo 历史原样保留、时间线与工程快照相等；
- 成功：只新增一条历史，单步 undo 覆盖建轨＋全部片段，redo 完整还原。

矩阵：av/video/audio × insert/overwrite × 新轨/现有轨。
"""

from __future__ import annotations

from typing import Any

import pytest
from PyQt6.QtCore import QByteArray, QMimeData, QPointF, Qt

from src.features.edit.core.project import EditProject, MediaAsset
from src.features.edit.core.source_drag_payload import (
    build_source_drag_payload,
)
from src.features.edit.core.timeline_v2 import EditTrack, Rational
from src.features.edit.ui.timeline_view import TimelineView

pytestmark = pytest.mark.edit_ui


def _asset(asset_id: str = "src-1") -> MediaAsset:
    return MediaAsset(
        asset_id=asset_id,
        path=f"/tmp/{asset_id}.mov",
        media_type="video",
        display_name=f"{asset_id}.mov",
        metadata={
            "fps": 24.0, "total_frames": 240, "width": 640, "height": 480,
            "audio_info": {"channels": 2, "channel_layout": "stereo"},
        },
    )


def _setup(qtbot: Any, *, with_audio_track: bool = True) -> TimelineView:
    from src.features.edit.core.timeline_v2 import TimelineSequence

    project = EditProject.create("r02")
    asset = _asset()
    project.assets[asset.asset_id] = asset
    view = TimelineView()
    qtbot.addWidget(view)
    view.resize(1200, 650)
    view.set_project(project)
    view.load_timeline(TimelineSequence(
        "r02", "R02", width=64, height=48, fps=Rational(24, 1),
        video_tracks=(EditTrack("V1", "V1", "video", 0),),
        audio_tracks=(EditTrack("A1", "A1", "audio", 0),)
        if with_audio_track else (),
    ))
    view.show()
    return view


def _drop_on(view: TimelineView, *, audio_area: bool = False,
             frame: int = 40, intent: str = "av", in_frame: int = 0,
             out_frame: int = 19) -> Any:
    """构造并投放一次 Source 拖放（audio_area=True → 空白音频区建新轨）。"""
    from PyQt6.QtGui import QDragEnterEvent, QDropEvent

    payload = build_source_drag_payload("src-1", in_frame, out_frame, intent)
    mime = QMimeData()
    from src.features.edit.core.source_drag_payload import MIME_TYPE_SOURCE_CLIP
    mime.setData(MIME_TYPE_SOURCE_CLIP, QByteArray(payload.encode()))
    if audio_area:
        area = view._layout_engine._area.audio_area
        scene_pos = QPointF(view._layout_engine.frame_to_x(frame),
                            area.center().y())
    else:
        rect = view._layout_engine.track_rect("V1")
        scene_pos = QPointF(view._layout_engine.frame_to_x(frame),
                            rect.center().y())
    point = view.mapFromScene(scene_pos)
    enter = QDragEnterEvent(point, Qt.DropAction.CopyAction, mime,
                            Qt.MouseButton.LeftButton,
                            Qt.KeyboardModifier.NoModifier)
    view.dragEnterEvent(enter)
    assert enter.isAccepted()
    drop = QDropEvent(scene_pos, Qt.DropAction.CopyAction, mime,
                      Qt.MouseButton.LeftButton,
                      Qt.KeyboardModifier.NoModifier)
    view.dropEvent(drop)
    return drop


def _seed_redo_history(view: TimelineView) -> Any:
    """预置一段可 redo 的历史（undo 一次），返回 undo 前的 redo 快照。"""
    from src.features.edit.core.timeline_v2 import ClipInstance
    from src.features.edit.core.commands import AddClipCommand

    clip = ClipInstance(clip_id="hist", asset_id="src-1", track_id="V1",
                        timeline_start=0, timeline_duration=5,
                        source_start=0, source_duration=5)
    view._execute_command(AddClipCommand.create("V1", clip))
    assert view.can_undo()
    view.undo()
    assert view.can_redo()
    redo_snapshot = list(view._command_stack._redo_stack)
    assert len(redo_snapshot) == 1
    return redo_snapshot


def _history_snapshot(view: TimelineView) -> dict:
    timeline = view.get_timeline()
    return {
        "revision": timeline.revision,
        "undo_len": len(view._command_stack._undo_stack),
        "redo_len": len(view._command_stack._redo_stack),
        "video_clips": tuple(
            (t.track_id, tuple(c.clip_id for c in t.clips))
            for t in timeline.video_tracks),
        "audio_tracks": tuple(t.track_id for t in timeline.audio_tracks),
        "audio_clips": tuple(
            (t.track_id, tuple(c.clip_id for c in t.clips))
            for t in timeline.audio_tracks),
    }


# ── 失败矩阵：历史快照相等（含既有 Redo 保留） ──────────────────────

@pytest.mark.parametrize("intent,mode", [
    ("av", "insert"), ("video", "insert"), ("audio", "insert"),
    ("av", "overwrite"), ("video", "overwrite"), ("audio", "overwrite"),
])
def test_failed_drop_after_new_track_leaves_history_and_redo_intact(
        qtbot, intent, mode):
    """新轨 + 插入构造失败：零 revision、Undo 空、既有 Redo 原样（旧实现
    会 revision+2、Undo 清空、Redo 残留可复活空轨）。"""
    view = _setup(qtbot, with_audio_track=False)  # audio 区空白 → 建轨路径
    view.set_edit_mode(mode)
    redo_before = _seed_redo_history(view)
    before = _history_snapshot(view)

    view.insert_clip = lambda **_kw: False  # 构造阶段失败（新旧路径通用）
    if mode == "overwrite":
        view._add_asset_as_clip = (
            lambda *a, **_k: False)  # noqa: E731 — 构造失败注入
    drop = _drop_on(view, audio_area=True, intent=intent)

    assert not drop.isAccepted()
    after = _history_snapshot(view)
    assert after == before
    assert view._command_stack._redo_stack == redo_before  # 既有 Redo 不丢


@pytest.mark.parametrize("mode", ["insert", "overwrite"])
def test_macro_apply_failure_rolls_back_everything(qtbot, monkeypatch,
                                                   mode):
    """宏应用阶段抛异常（如落轨校验失败）：AddTrack 已应用也被宏内回滚，
    历史/revision/Redo 全部原样。"""
    from src.features.edit.core.commands import (
        AddClipCommand, InsertClipCommand,
    )

    view = _setup(qtbot, with_audio_track=False)
    view.set_edit_mode(mode)
    redo_before = _seed_redo_history(view)
    before = _history_snapshot(view)

    def _boom(self, timeline):  # noqa: ANN001
        raise ValueError("simulated apply failure")

    monkeypatch.setattr(AddClipCommand, "apply", _boom)
    monkeypatch.setattr(InsertClipCommand, "apply", _boom)
    drop = _drop_on(view, audio_area=True, intent="audio")

    assert not drop.isAccepted()
    assert _history_snapshot(view) == before
    assert view._command_stack._redo_stack == redo_before


def test_locked_track_rejection_preserves_history(qtbot):
    from dataclasses import replace as _dc_replace

    view = _setup(qtbot)
    timeline = view.get_timeline()
    v1 = timeline.get_track_by_id("V1")
    locked = timeline.with_track_updated(_dc_replace(v1, is_locked=True))
    view.load_timeline(locked)
    redo_before = _seed_redo_history(view)
    before = _history_snapshot(view)

    drop = _drop_on(view, intent="av")

    assert not drop.isAccepted()
    assert _history_snapshot(view) == before
    assert view._command_stack._redo_stack == redo_before


def test_construct_exception_reports_and_keeps_history(qtbot):
    view = _setup(qtbot)
    view.set_edit_mode("insert")
    redo_before = _seed_redo_history(view)
    before = _history_snapshot(view)

    def _raise(**_kw):
        raise ValueError("planner rejected")

    view.insert_clip = _raise
    drop = _drop_on(view, intent="av")

    assert not drop.isAccepted()
    assert _history_snapshot(view) == before
    assert view._command_stack._redo_stack == redo_before


# ── 成功矩阵：单条历史、单步 undo/redo 覆盖全部效果 ─────────────────

@pytest.mark.parametrize("intent,mode,new_track", [
    ("av", "insert", False), ("video", "insert", False),
    ("av", "overwrite", False), ("video", "overwrite", False),
    ("audio", "insert", True), ("audio", "overwrite", True),
])
def test_successful_drop_single_history_entry_full_undo_redo(
        qtbot, intent, mode, new_track):
    # 新轨场景：audio 意图投放到空白音频区（无音频轨 → 建轨）；
    # 现有轨场景：V1 直接命中。
    view = _setup(qtbot, with_audio_track=not new_track)
    view.set_edit_mode(mode)
    _seed_redo_history(view)  # 成功路径：redo 按惯例清空
    assert view.can_redo()
    undo_before = len(view._command_stack._undo_stack)

    drop = _drop_on(view, audio_area=new_track, intent=intent)
    assert drop.isAccepted()

    # 只新增一条历史；redo 按惯例被新编辑清空
    assert len(view._command_stack._undo_stack) == undo_before + 1
    assert not view.can_redo()

    timeline = view.get_timeline()
    if intent == "av":
        assert timeline.video_tracks[0].clips
        assert any(t.clips for t in timeline.audio_tracks)
    elif intent == "video":
        assert timeline.video_tracks[0].clips
    else:
        assert any(t.clips for t in timeline.audio_tracks)

    # 单步 undo：建轨（若有）＋全部片段一起还原
    view.undo()
    undone = view.get_timeline()
    if new_track:
        assert not undone.audio_tracks
    else:
        assert not undone.video_tracks[0].clips
        assert not any(t.clips for t in undone.audio_tracks)

    # 单步 redo：全部效果回来
    view.redo()
    redone = view.get_timeline()
    if intent == "audio":
        assert any(t.clips for t in redone.audio_tracks)
    else:
        assert redone.video_tracks[0].clips
        if intent == "av":
            assert any(t.clips for t in redone.audio_tracks)


def test_new_track_drop_revision_advances_once(qtbot):
    """旧实现失败时 revision +2；成功新轨路径也只应 +1（单宏提交）。"""
    view = _setup(qtbot, with_audio_track=False)
    view.set_edit_mode("overwrite")
    before_rev = view.get_timeline().revision

    assert _drop_on(view, audio_area=True, intent="audio").isAccepted()

    assert view.get_timeline().revision == before_rev + 1
    assert len(view._command_stack._undo_stack) == 1


def test_existing_track_drop_still_atomic(qtbot):
    """现有轨（无建轨）：av 配对插入仍是一条历史、undo 双轨同撤。"""
    view = _setup(qtbot)
    assert _drop_on(view, intent="av").isAccepted()
    assert len(view._command_stack._undo_stack) == 1
    view.undo()
    undone = view.get_timeline()
    assert not undone.video_tracks[0].clips
    assert not undone.audio_tracks[0].clips
