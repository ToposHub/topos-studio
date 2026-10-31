"""批 D 回归：INT-06 滚轮归一 / INT-07 空快捷键与 N 键标签 / INT-10 字幕
边界 / INT-11 轨道删除统计 / INT-13 多选上下文 / FE-B14 能力表。

对应 docs/edit_page_full_audit_optimization_plan_2026-10-14.md §10/§11。
"""

from __future__ import annotations

from unittest.mock import MagicMock
from typing import Any

import pytest

from src.features.edit.core.timeline_v2 import (
    ClipInstance,
    EditTrack,
    SubtitleClip,
    TimelineSequence,
)
from src.features.edit.ui.timeline_shortcuts import (
    TIMELINE_KEY_ACTIONS,
    matches_key_event,
)

pytestmark = pytest.mark.edit_core


def _clip(clip_id: str, track_id: str, start: int, duration: int) -> ClipInstance:
    return ClipInstance(
        clip_id=clip_id,
        asset_id=f"a-{clip_id}",
        track_id=track_id,
        timeline_start=start,
        timeline_duration=duration,
        source_start=0,
        source_duration=500,
    )


def _timeline_with_subtitle() -> TimelineSequence:
    video = EditTrack(track_id="v1", name="V1", track_type="video", index=0)
    subs = EditTrack(track_id="s1", name="S1", track_type="subtitle", index=0)
    timeline = TimelineSequence(timeline_id="t1", name="T", fps=24.0)
    timeline = timeline.with_track_added(video).with_track_added(subs)
    timeline = timeline.with_clip_added(
        "v1", _clip("vc", "v1", 239, 100)
    )
    return timeline.with_subtitle_clip_added(
        "s1",
        SubtitleClip(
            clip_id="sc",
            track_id="s1",
            timeline_start=100,
            timeline_duration=100,
            text="cue",
        ),
    )


def _view(timeline: TimelineSequence):
    from src.features.edit.ui.timeline_view import TimelineView

    view = MagicMock()
    view._timeline = timeline
    view._playhead_frame = 50
    view.set_playhead = lambda f: setattr(view, "_playhead_frame", f)
    view._ensure_playhead_visible = lambda: None
    view._jump_to_end = lambda: setattr(view, "_playhead_frame", 1000)
    view._jump_to_start = lambda: setattr(view, "_playhead_frame", 0)
    # unbound 调用下内部方法需要显式绑定真实实现
    view._iter_timeline_object_boundaries = (
        lambda: TimelineView._iter_timeline_object_boundaries(view)
    )
    return view


# ---------------------------------------------------------------- INT-10

def test_next_boundary_includes_subtitle_start() -> None:
    """审计实测：字幕[100,200)唯一内容，playhead50 Down 跳 239。"""
    from src.features.edit.ui.timeline_view import TimelineView

    timeline = _timeline_with_subtitle()
    view = _view(timeline)
    TimelineView._jump_to_next_clip_boundary(view)
    assert view._playhead_frame == 100  # 字幕 cue 起点是最近边界


def test_prev_boundary_includes_subtitle_end() -> None:
    from src.features.edit.ui.timeline_view import TimelineView

    timeline = _timeline_with_subtitle()
    view = _view(timeline)
    view._playhead_frame = 210
    TimelineView._jump_to_prev_clip_boundary(view)
    assert view._playhead_frame == 200


# ---------------------------------------------------------------- INT-07

def test_explicitly_cleared_binding_does_not_fall_back(qtbot) -> None:
    """审计实测：显式清空 blade 绑定后按 B 仍触发。"""
    from src.shared.ui.shortcut_manager import ShortcutManager

    manager = ShortcutManager.instance()
    if manager is None:
        pytest.skip("ShortcutManager unavailable")
    from PyQt6.QtCore import QEvent, Qt
    from PyQt6.QtGui import QKeyEvent

    from src.features.edit.ui.timeline_shortcuts import register_timeline_shortcuts

    register_timeline_shortcuts(manager)
    # 显式清空 blade 绑定
    manager.set_shortcut("edit.timeline.tool.blade", "")
    event = QKeyEvent(
        QEvent.Type.KeyPress, Qt.Key.Key_B, Qt.KeyboardModifier.NoModifier
    )
    assert matches_key_event(
        event, "edit.timeline.tool.blade", "B"
    ) is False


def test_n_key_label_matches_real_behavior() -> None:
    """N 键注册标签应与真实 handler（切换吸附）一致。"""
    action = next(
        a for a in TIMELINE_KEY_ACTIONS
        if a.action_id == "edit.timeline.tool.next_enable"
    )
    assert "Snap" in action.label


# ---------------------------------------------------------------- INT-11

def test_track_delete_counts_subtitles_and_transitions() -> None:
    from src.features.edit.ui.timeline_view import TimelineView

    timeline = _timeline_with_subtitle()
    view = _view(timeline)
    asked: list = []

    import src.features.edit.ui.timeline_view as tv_mod

    box = MagicMock()
    box.question.side_effect = (
        lambda *a, **k: (asked.append(a), 0x00004000)[1]  # No
    )
    original = tv_mod.QMessageBox if hasattr(tv_mod, "QMessageBox") else None
    # _on_track_delete_requested 里是局部 import——注入 sys.modules 太重，
    # 直接验证逻辑：patch QMessageBox.question
    import PyQt6.QtWidgets as qw
    qw.QMessageBox.question = staticmethod(box.question)
    try:
        TimelineView._on_track_delete_requested(view, "s1")
    finally:
        if original is not None:
            qw.QMessageBox.question = original.question
    assert asked, "含字幕 cue 的轨道删除必须询问"
    # 拒绝后未删除
    assert timeline.get_track_by_id("s1") is not None


# ---------------------------------------------------------------- INT-13

def test_set_clips_keeps_full_selection_context(qtbot: Any) -> None:
    """审计实测：set_clips([c,d]) 后 _current_clips 只剩 [c]。"""
    from src.features.edit.ui.edit_properties_panel import EditPropertiesPanel

    panel = EditPropertiesPanel()
    qtbot.addWidget(panel)
    a = _clip("a", "v1", 0, 10)
    b = _clip("b", "v1", 20, 10)
    EditPropertiesPanel.set_clips(panel, [a, b], fps=24.0)
    assert [c.clip_id for c in panel._current_clips] == ["a", "b"]


# ---------------------------------------------------------------- FE-B14

def test_stem_export_registry_available():
    from src.features.edit.core.capability_registry import (
        CapabilityStatus,
        build_capability_registry,
    )

    registry = build_capability_registry()
    cap = registry.get("edit.interchange.audio_stems")
    assert cap is not None
    assert cap.status is CapabilityStatus.AVAILABLE
    evidence = " ".join(cap.evidence)
    assert "export_stem_tracks" in evidence
