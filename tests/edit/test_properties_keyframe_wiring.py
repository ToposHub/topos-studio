"""E02: 属性面板关键帧按钮全链路。

审计实测两处断裂：
1. ``EditPropertiesPanel.keyframe_toggled``（普通片段参数 ◆）emit 后全仓
   0 接收者；
2. ``_on_edit_effect_keyframe_toggled`` 用 ``_cmds["AddKeyframeCommand"]``
   直接索引，而 commands_facade 未导出该类 → 点击效果 ◆ 直接 KeyError。

回归锁定：
- facade 导出 Add/RemoveKeyframeCommand（契约键存在）；
- MainWindow 源码连接 keyframe_toggled → _on_edit_clip_keyframe_toggled；
- 片段级关键帧 add → undo 删除 → redo 恢复（同一命令栈精确性）；
- 片段级槽在命令类缺失时结构化诊断（不把 KeyError 抛进 Qt 槽）。
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import src.app.controllers.edit_controller as ec
from src.app.controllers.edit_controller import EditControllerMixin
from src.features.edit.commands_facade import get_command_classes
from src.features.edit.core.commands import (
    AddKeyframeCommand,
    CommandStack,
    RemoveKeyframeCommand,
)
from src.features.edit.core.timeline_v2 import (
    ClipInstance,
    EditTrack,
    TimelineSequence,
)

_MAIN_WINDOW = (
    Path(__file__).resolve().parents[2] / "src" / "app" / "main_window.py"
)


def _make_timeline_with_clip() -> TimelineSequence:
    clip = ClipInstance(
        clip_id="kf-clip", track_id="v1", asset_id="asset-1",
        timeline_start=0, timeline_duration=100,
        source_start=0, source_duration=100,
        opacity=0.5,
    )
    track = EditTrack(track_id="v1", name="V1", track_type="video", index=0)
    return TimelineSequence(
        timeline_id="tl", name="T", fps=25.0,
    ).with_track_added(track).with_clip_added("v1", clip)


class TestFacadeExports:
    def test_keyframe_commands_exported(self) -> None:
        cmds = get_command_classes()
        assert cmds is not None
        assert cmds["AddKeyframeCommand"] is AddKeyframeCommand
        assert cmds["RemoveKeyframeCommand"] is RemoveKeyframeCommand


class TestHostWiring:
    def test_main_window_connects_clip_keyframe_signal(self) -> None:
        text = _MAIN_WINDOW.read_text(encoding="utf-8")
        assert "keyframe_toggled.connect(self._on_edit_clip_keyframe_toggled)" in text

    def test_clip_keyframe_handler_exists(self) -> None:
        assert callable(getattr(EditControllerMixin, "_on_edit_clip_keyframe_toggled"))


class TestClipKeyframeLifecycle:
    """片段级 ◆ 的命令语义：add → undo 恢复 → redo 再加回。"""

    def test_add_undo_redo(self) -> None:
        timeline = _make_timeline_with_clip()
        stack = CommandStack()

        t1 = stack.execute(AddKeyframeCommand.create(
            track_id="v1", clip_id="kf-clip",
            param_name="opacity", frame=40, value=0.5,
        ), timeline)
        assert t1.get_clip_by_id("kf-clip").has_keyframe_at("opacity", 40)

        t2 = stack.undo(t1)
        assert not t2.get_clip_by_id("kf-clip").has_keyframe_at("opacity", 40)

        t3 = stack.redo(t2)
        assert t3.get_clip_by_id("kf-clip").has_keyframe_at("opacity", 40)

        # Remove 命令删除既有关键帧，undo 恢复
        t4 = stack.execute(RemoveKeyframeCommand.create(
            track_id="v1", clip_id="kf-clip",
            param_name="opacity", frame=40,
        ), t3)
        assert not t4.get_clip_by_id("kf-clip").has_keyframe_at("opacity", 40)
        t5 = stack.undo(t4)
        assert t5.get_clip_by_id("kf-clip").has_keyframe_at("opacity", 40)


class TestMissingCommandClassIsStructural:
    def test_clip_slot_missing_cmds_gives_status_not_keyerror(self) -> None:
        timeline = _make_timeline_with_clip()
        ctrl = EditControllerMixin.__new__(EditControllerMixin)
        ctrl.edit_page = SimpleNamespace(_current_timeline=timeline)
        ctrl.timeline_panel = None
        ctrl.timeline_preview_renderer = None
        ctrl._status_messages: list[str] = []
        ctrl.status_bar = SimpleNamespace(
            showMessage=lambda m, t=5000: ctrl._status_messages.append(m))
        ctrl.edit_properties_panel = SimpleNamespace(
            update_current_clip=lambda clip: None)

        original = ec.get_command_classes
        ec.get_command_classes = lambda: None
        try:
            ctrl._on_edit_clip_keyframe_toggled("kf-clip", "opacity", 10, 0.5)
        finally:
            ec.get_command_classes = original
        assert ctrl._status_messages, "命令缺失必须给结构化诊断"
