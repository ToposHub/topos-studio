"""Settings and ordinary clip edits must undo in their original time domain."""
from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

from PyQt6.QtWidgets import QDialog, QWidget

from src.app.controllers.edit_controller import EditControllerMixin
from src.features.edit.core.clip_params import TransformParams
from src.features.edit.core.commands import UpdateClipParamCommand
from src.features.edit.core.project import EditProject, TimelineSequenceRef
from src.features.edit.core.timeline_settings_command import TimelineSettingsCommand
from src.features.edit.core.timeline_v2 import ClipInstance, EditTrack, TimelineSequence
from src.features.edit.ui.new_timeline_dialog import NewTimelineDialog
from src.features.edit.ui.timeline_view import TimelineView
from src.shared.media.metadata import Rational


class _Host(QWidget, EditControllerMixin):
    def _invalidate_motion_graphic_caches_for_timeline_change(self, before: Any, after: Any) -> None:
        pass

    def _request_current_timeline_preview_frame(self) -> None:
        pass

    def _schedule_audio_mixer_sync(self, delay_ms: int = 100) -> None:
        pass

    def _schedule_auto_render_cache(self) -> None:
        pass


def _sequence(timeline_id: str = "t") -> TimelineSequence:
    clip = ClipInstance(
        clip_id="c", asset_id="a", track_id="v", timeline_start=0,
        timeline_duration=100, source_start=0, source_duration=100,
    )
    return TimelineSequence(
        timeline_id=timeline_id, name="Before", fps=Rational(24, 1), revision=3,
        video_tracks=(EditTrack(track_id="v", name="V", track_type="video", index=0,
                                clips=(clip,)),),
    )


def _edit_x(view: TimelineView, value: float) -> None:
    clip = view._timeline.get_clip_by_id("c")
    view.execute_command(UpdateClipParamCommand(
        command_id=f"x{value}", label="X", track_id="v", clip_id="c",
        param_type="transform", old_value=clip.transform,
        new_value=replace(clip.transform, translate_x=value),
    ))


def test_settings_then_clip_edit_undo_preserves_history_order(qtbot: Any, monkeypatch: Any) -> None:
    host = _Host()
    qtbot.addWidget(host)
    sequence = _sequence()
    project = EditProject(
        project_id="p", name="P", active_timeline_id="t", revision=7,
        timelines={"t": TimelineSequenceRef(timeline_id="t", name="Before", fps=24.0)},
    )
    view = TimelineView()
    qtbot.addWidget(view)
    view.set_project(project)
    view.load_timeline(sequence)
    host.media_pool_panel = MagicMock()
    host._current_edit_project = project
    host._current_timeline_sequences = {"t": sequence}
    host.timeline_panel = SimpleNamespace(get_timeline_view=lambda: view)
    host.edit_page = SimpleNamespace(_current_timeline=sequence)
    host.edit_page.set_timeline = lambda value: setattr(host.edit_page, "_current_timeline", value)
    host.timeline_preview_renderer = MagicMock()
    host.edit_properties_panel = None
    view.timeline_changed.connect(host._on_edit_timeline_changed)
    monkeypatch.setattr(NewTimelineDialog, "exec", lambda self: QDialog.DialogCode.Accepted)
    monkeypatch.setattr(NewTimelineDialog, "get_settings", lambda self: {
        "name": "After", "fps": 48.0, "fps_num": 48, "fps_den": 1,
        "width": 1280, "height": 720, "sample_rate": 48000,
    })
    _edit_x(view, 10)
    host._on_timeline_settings_requested("t")
    assert isinstance(view._command_stack._undo_stack[-1], TimelineSettingsCommand)
    assert view._timeline.get_clip_by_id("c").timeline_duration == 200
    _edit_x(view, 20)
    view.undo()
    assert view._timeline.get_clip_by_id("c").transform.translate_x == 10
    assert view._timeline.fps == Rational(48, 1)
    view.undo()
    assert view._timeline.fps == Rational(24, 1)
    assert view._timeline.get_clip_by_id("c").timeline_duration == 100
    assert view._timeline.get_clip_by_id("c").transform.translate_x == 10
    view.undo()
    assert view._timeline.get_clip_by_id("c").transform.translate_x == 0
    assert host._current_edit_project.revision == 13
    assert host._current_timeline_sequences["t"].revision == 9
    assert host._current_edit_project.timelines["t"].fps == 24.0
    for _ in range(3):
        view.redo()
    assert view._timeline.fps == Rational(48, 1)
    assert view._timeline.get_clip_by_id("c").transform.translate_x == 20
    assert host._current_edit_project.timelines["t"].fps == 48.0


def test_timeline_switch_preserves_separate_undo_histories(qtbot: Any) -> None:
    view = TimelineView()
    qtbot.addWidget(view)
    a, b = _sequence("A"), _sequence("B")
    view.load_timeline(a)
    _edit_x(view, 10)
    a_edited = view._timeline
    view.load_timeline(b)
    assert not view.can_undo()
    _edit_x(view, 20)
    b_edited = view._timeline
    view.load_timeline(a_edited)
    view.undo()
    assert view._timeline.timeline_id == "A"
    assert view._timeline.get_clip_by_id("c").transform.translate_x == 0
    assert b_edited.get_clip_by_id("c").transform.translate_x == 20
    view.load_timeline(b_edited)
    view.undo()
    assert view._timeline.timeline_id == "B"
    assert view._timeline.get_clip_by_id("c").transform.translate_x == 0


def test_new_project_same_timeline_id_cannot_reuse_prior_history(qtbot: Any) -> None:
    view = TimelineView()
    qtbot.addWidget(view)
    view.set_project(EditProject(project_id="P1", name="P1"))
    view.load_timeline(_sequence())
    _edit_x(view, 10)
    view.set_project(EditProject(project_id="P2", name="P2"))
    view.load_timeline(_sequence())
    assert not view.can_undo()
    assert not view.can_redo()
    assert view._timeline.get_clip_by_id("c").transform.translate_x == 0


def test_settings_snapshot_serialization_preserves_unknown_payload() -> None:
    before = _sequence()
    after = replace(before, fps=Rational(48, 1), _extra_fields={"future": {"keep": True}})
    command = TimelineSettingsCommand.create(before, after)
    restored = TimelineSettingsCommand.from_dict(command.to_dict())
    assert restored.apply(before).to_dict() == after.to_dict()
    assert restored.undo(after).to_dict() == before.to_dict()


def test_settings_command_public_macro_roundtrip_in_real_stack() -> None:
    from src.features.edit.core.commands import CommandStack, MacroCommand

    before = _sequence()
    after = replace(before, fps=Rational(48, 1), name="Changed")
    macro = MacroCommand.create("Settings transaction", [TimelineSettingsCommand.create(before, after)])
    restored_macro = MacroCommand.from_dict(macro.to_dict())
    assert isinstance(restored_macro.sub_commands[0], TimelineSettingsCommand)
    stack = CommandStack()
    applied = stack.execute(restored_macro, before)
    assert applied.to_dict() == after.to_dict()
    undone = stack.undo(applied)
    assert undone.to_dict() == before.to_dict()
    redone = stack.redo(undone)
    assert redone.to_dict() == after.to_dict()


def test_settings_payload_cannot_change_target_timeline() -> None:
    import pytest

    command = TimelineSettingsCommand.create(_sequence(), replace(_sequence(), name="New"))
    data = command.to_dict()
    data["after_sequence"]["timeline_id"] = "other"
    with pytest.raises(ValueError, match="another timeline"):
        TimelineSettingsCommand.from_dict(data)


def test_external_replacement_invalidates_old_timeline_commands(qtbot: Any) -> None:
    view = TimelineView()
    qtbot.addWidget(view)
    view.load_timeline(_sequence())
    _edit_x(view, 10)
    external = view._timeline.with_clip_updated(replace(
        view._timeline.get_clip_by_id("c"), transform=TransformParams(translate_x=99)))
    view.load_timeline(external)
    assert not view.can_undo()
    assert view._timeline.get_clip_by_id("c").transform.translate_x == 99


def test_inactive_settings_activates_target_and_retains_other_history(qtbot: Any, monkeypatch: Any) -> None:
    host = _Host()
    qtbot.addWidget(host)
    a, b = _sequence("A"), _sequence("B")
    project = EditProject(
        project_id="p", name="P", active_timeline_id="A", revision=7,
        timelines={tid: TimelineSequenceRef(timeline_id=tid, name="Before", fps=24.0)
                   for tid in ("A", "B")},
    )
    view = TimelineView()
    qtbot.addWidget(view)
    view.set_project(project)
    view.load_timeline(a)
    host.media_pool_panel = MagicMock()
    host._current_edit_project = project
    host._current_timeline_sequences = {"A": a, "B": b}
    host.timeline_panel = MagicMock()
    host.timeline_panel.get_timeline_view.return_value = view
    host.timeline_panel.load_timeline.side_effect = view.load_timeline
    host.timeline_panel.set_project.side_effect = view.set_project
    host.edit_page = SimpleNamespace(_current_timeline=a)
    host.edit_page.set_timeline = lambda value: setattr(host.edit_page, "_current_timeline", value)
    host.timeline_preview_renderer = MagicMock()
    host.edit_properties_panel = None
    view.timeline_changed.connect(host._on_edit_timeline_changed)
    _edit_x(view, 10)
    monkeypatch.setattr(NewTimelineDialog, "exec", lambda self: QDialog.DialogCode.Accepted)
    monkeypatch.setattr(NewTimelineDialog, "get_settings", lambda self: {
        "name": "B48", "fps": 48.0, "width": 1280, "height": 720, "sample_rate": 48000,
    })
    host._on_timeline_settings_requested("B")
    assert host._current_edit_project.active_timeline_id == "B"
    assert view._timeline.timeline_id == "B"
    assert view._timeline.fps == Rational(48, 1)
    view.undo()
    assert view._timeline.fps == Rational(24, 1)
    host._on_timeline_switched("A")
    assert view._timeline.get_clip_by_id("c").transform.translate_x == 10
    view.undo()
    assert view._timeline.get_clip_by_id("c").transform.translate_x == 0


def test_lower_fps_collapsed_edits_are_rejected_without_history_or_publication(
        qtbot: Any, monkeypatch: Any) -> None:
    host = _Host()
    qtbot.addWidget(host)
    before = _sequence()
    first = replace(before.get_clip_by_id("c"), timeline_duration=1, source_duration=1)
    second = replace(first, clip_id="d", timeline_start=1, source_start=1)
    sequence = replace(before, fps=Rational(48, 1), video_tracks=(replace(
        before.video_tracks[0], clips=(first, second)),))
    project = EditProject(
        project_id="p", name="P", active_timeline_id="t", revision=7,
        timelines={"t": TimelineSequenceRef(timeline_id="t", name="Before", fps=48.0)},
    )
    view = TimelineView()
    qtbot.addWidget(view)
    view.set_project(project)
    view.load_timeline(sequence)
    host._current_edit_project = project
    host._current_timeline_sequences = {"t": sequence}
    host.timeline_panel = SimpleNamespace(get_timeline_view=lambda: view)
    host.edit_page = SimpleNamespace(_current_timeline=sequence)
    host.status_bar = MagicMock()
    monkeypatch.setattr(NewTimelineDialog, "exec", lambda self: QDialog.DialogCode.Accepted)
    monkeypatch.setattr(NewTimelineDialog, "get_settings", lambda self: {
        "name": "After", "fps": 24.0, "width": 1280, "height": 720, "sample_rate": 48000,
    })
    host._on_timeline_settings_requested("t")
    assert host._current_edit_project is project
    assert host._current_timeline_sequences["t"] is sequence
    assert view._timeline is sequence
    assert not view.can_undo()
    host.status_bar.showMessage.assert_called_once()
    assert "overlap" in host.status_bar.showMessage.call_args.args[0]


def test_settings_modal_preserves_edits_published_while_dialog_is_open(qtbot: Any, monkeypatch: Any) -> None:
    host = _Host()
    qtbot.addWidget(host)
    sequence = _sequence()
    project = EditProject(project_id="p", name="P", active_timeline_id="t", revision=7,
        timelines={"t": TimelineSequenceRef(timeline_id="t", name="Before")})
    view = TimelineView()
    qtbot.addWidget(view)
    view.set_project(project)
    view.load_timeline(sequence)
    host._current_edit_project = project
    host._current_timeline_sequences = {"t": sequence}
    host.timeline_panel = SimpleNamespace(get_timeline_view=lambda: view)
    host.edit_page = SimpleNamespace(_current_timeline=sequence)
    host.edit_page.set_timeline = lambda value: setattr(host.edit_page, "_current_timeline", value)
    host.media_pool_panel = MagicMock()
    host.timeline_preview_renderer = None
    host.edit_properties_panel = None
    host.edit_info_panel = None
    view.timeline_changed.connect(host._on_edit_timeline_changed)
    def accepted_after_edit(dialog: Any) -> Any:
        _edit_x(view, 99)
        return QDialog.DialogCode.Accepted
    monkeypatch.setattr(NewTimelineDialog, "exec", accepted_after_edit)
    monkeypatch.setattr(NewTimelineDialog, "get_settings", lambda self: {
        "name": "After", "fps": 48.0, "width": 1280, "height": 720, "sample_rate": 48000,
    })
    host._on_timeline_settings_requested("t")
    assert view._timeline.fps == Rational(48, 1)
    assert view._timeline.get_clip_by_id("c").transform.translate_x == 99
    view.undo()
    assert view._timeline.fps == Rational(24, 1)
    assert view._timeline.get_clip_by_id("c").transform.translate_x == 99
    view.undo()
    assert view._timeline.get_clip_by_id("c").transform.translate_x == 0


def test_settings_modal_discards_result_after_project_switch(qtbot: Any, monkeypatch: Any) -> None:
    host = _Host()
    qtbot.addWidget(host)
    sequence = _sequence()
    project = EditProject(project_id="p", name="P", active_timeline_id="t",
        timelines={"t": TimelineSequenceRef(timeline_id="t", name="Before")})
    host._current_edit_project = project
    host._current_timeline_sequences = {"t": sequence}
    replacement = replace(project, project_id="replacement")
    def accepted_after_switch(dialog: Any) -> Any:
        host._current_edit_project = replacement
        return QDialog.DialogCode.Accepted
    monkeypatch.setattr(NewTimelineDialog, "exec", accepted_after_switch)
    host._on_timeline_settings_requested("t")
    assert host._current_edit_project is replacement
    assert host._current_timeline_sequences["t"] is sequence
