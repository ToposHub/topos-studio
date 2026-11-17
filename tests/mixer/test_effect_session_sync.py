"""Effect windows follow immutable local-UI snapshots and undo transactions."""
from __future__ import annotations

from unittest.mock import patch

import pytest
from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QApplication, QWidget

from src.features.mixer.effects import EffectInstance, EffectWindowManager
from src.features.mixer.ui.console_panel import ConsolePanel
from src.features.mixer.ui.session import MixerUiSession


@pytest.fixture
def effect_session(qapp: QApplication):
    parent = QWidget()
    session = MixerUiSession(parent)
    manager = EffectWindowManager(parent)
    console = ConsolePanel(session, manager, parent)
    before: list[tuple] = []

    def all_effects() -> list[EffectInstance]:
        return [effect for owner in (*session.tracks, *session.clips) for effect in owner.effects]

    def about_to_change(_instance: EffectInstance) -> None:
        before.append(session.snapshot())

    def changed(_instance: EffectInstance) -> None:
        session.commit_preview("调整效果参数", before.pop())

    session.changed.connect(lambda: manager.reconcile(all_effects()))
    manager.instanceAboutToChange.connect(about_to_change)
    manager.instanceChanged.connect(changed)
    yield session, manager, console
    manager.dispose_all()
    parent.deleteLater()


def test_window_rebinds_after_unrelated_commit_and_parameter_edit_is_undoable(effect_session) -> None:
    session, manager, console = effect_session
    console.strips["dx"].insert_effect("eq")
    first = session.track_by_id("dx").effects[0]
    window = manager.window_for(first)
    session.set_track_value("dx", "pan", .2)
    current = session.track_by_id("dx").effects[0]
    assert window.instance is current
    old_gain = window.panel.get_state()["master_gain"]
    state = window.panel.get_state()
    state["master_gain"] = -11.0
    window.panel.set_state(state)
    count = session.undo_stack.count()
    window.panel.stateChanged.emit(state)
    assert session.undo_stack.count() == count + 1
    assert session.track_by_id("dx").effects[0].parameters["master_gain"] == -11.0
    assert window.instance is session.track_by_id("dx").effects[0]
    session.undo_stack.undo()
    assert window.panel.get_state()["master_gain"] == old_gain
    session.undo_stack.redo()
    assert window.panel.get_state()["master_gain"] == -11.0


def test_rack_bypass_is_one_undo_action_and_updates_open_panel(effect_session) -> None:
    session, manager, console = effect_session
    strip = console.strips["dx"]
    strip.insert_effect("eq")
    effect = session.track_by_id("dx").effects[0]
    window = manager.window_for(effect)
    count = session.undo_stack.count()
    strip._toggle_effect(effect)
    assert session.undo_stack.count() == count + 1
    assert not session.track_by_id("dx").effects[0].enabled
    assert window.panel.get_state()["enabled"] is False
    assert strip.fx_list.item(0).text().startswith("○")
    session.undo_stack.undo()
    assert session.track_by_id("dx").effects[0].enabled
    assert window.panel.get_state()["enabled"] is True
    assert strip.fx_list.item(0).text().startswith("●")


def test_reconcile_does_not_reload_unchanged_parameters_on_each_session_notification(effect_session) -> None:
    session, manager, console = effect_session
    console.strips["dx"].insert_effect("eq")
    effect = session.track_by_id("dx").effects[0]
    window = manager.window_for(effect)
    with patch.object(window.panel, "set_state", wraps=window.panel.set_state) as restore:
        session.touch()
        session.set_track_value("dx", "pan", .3)
        assert restore.call_count == 0
    assert window.instance is session.track_by_id("dx").effects[0]


def test_remove_undo_reopen_retains_parameters_without_orphan_windows(effect_session) -> None:
    session, manager, console = effect_session
    strip = console.strips["dx"]
    strip.insert_effect("eq")
    effect = session.track_by_id("dx").effects[0]
    window = manager.window_for(effect)
    state = window.panel.get_state()
    state["master_gain"] = -7.0
    window.panel.set_state(state)
    window.panel.stateChanged.emit(state)
    strip._remove_effect(effect.instance_id)
    assert manager.window_for(effect) is None
    assert not session.track_by_id("dx").effects
    session.undo_stack.undo()
    restored = session.track_by_id("dx").effects[0]
    reopened = manager.open(restored, "对白")
    assert reopened is not window
    assert reopened.panel.get_state()["master_gain"] == -7.0
    assert all(not timer.isActive() for timer in reopened.panel.findChildren(QTimer))
    session.undo_stack.redo()
    assert manager.window_for(restored) is None


def test_undo_insert_closes_window_and_redo_rebinds_on_demand(effect_session) -> None:
    session, manager, console = effect_session
    console.strips["dx"].insert_effect("eq")
    effect = session.track_by_id("dx").effects[0]
    session.undo_stack.undo()
    assert not session.track_by_id("dx").effects
    assert manager.window_for(effect) is None
    session.undo_stack.redo()
    restored = session.track_by_id("dx").effects[0]
    window = manager.open(restored, "对白")
    assert window.instance is restored
