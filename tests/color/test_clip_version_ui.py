"""Version UI requests, authoritative readback and stale-context protection."""
from dataclasses import FrozenInstanceError, replace
from unittest.mock import MagicMock
from types import SimpleNamespace

import pytest
from PyQt6.QtCore import Qt, QPoint
from PyQt6.QtWidgets import QDialog, QDialogButtonBox, QMenu, QWidget

from src.features.color.ui.clip_version_ui import (
    ClipVersionCollection, ClipVersionMenu, ClipVersionRequest, ClipVersionResult,
    ClipVersionSummary, ClipVersionTarget, ClipVersionUiController,
    VersionActionDialog, VersionOperation as Op, VersionScope as Scope,
    validate_version_name,
)
from src.features.color.ui.timeline_clip_strip import TimelineClipStrip, ColorClipEntry, BottomLabelMode


@pytest.fixture
def ui(qtbot):
    owner = QWidget()
    qtbot.addWidget(owner)
    controller = ClipVersionUiController(owner)
    controller.bind_context("project", "timeline")
    target = ClipVersionTarget("project", "timeline", "clip-b", "asset", "stack")
    state = ClipVersionCollection(
        target, Scope.LOCAL,
        (ClipVersionSummary("base", "Version 1", True), ClipVersionSummary("warm", "Warm")),
        "base", revision=10, connected=True, loaded=True, in_use=True,
    )
    controller.set_collection(state)
    return owner, controller, target, state


def dialog_for(ui, qtbot, op=Op.CREATE, version=None):
    owner, controller, target, _ = ui
    dialog = VersionActionDialog(controller, target, Scope.LOCAL, op, version, parent=owner)
    qtbot.addWidget(dialog)
    dialog.show()
    return dialog


def test_request_is_immutable_and_does_not_change_displayed_versions(ui):
    _, controller, target, state = ui
    requests = []
    controller.operation_requested.connect(requests.append)
    token = controller.submit(target, Scope.LOCAL, Op.CREATE, name=" Cool ")
    assert controller.collection(target, Scope.LOCAL) == state
    assert len(requests) == 1
    assert requests[0].name == "Cool"
    assert requests[0].target.clip_id == "clip-b"
    assert requests[0].expected_revision == 10
    assert requests[0].request_id == token
    with pytest.raises(FrozenInstanceError):
        requests[0].name = "changed"
    assert controller.submit(target, Scope.LOCAL, Op.CREATE, name="Another") is None
    assert len(requests) == 1


def test_collection_changes_only_on_matching_success(ui):
    _, controller, target, state = ui
    token = controller.submit(target, Scope.LOCAL, Op.LOAD, version_id="warm")
    assert controller.collection(target, Scope.LOCAL).active_version_id == "base"
    assert not controller.finish(ClipVersionResult("wrong-token", target, state))
    assert not controller.finish(ClipVersionResult(token, replace(target, clip_id="other"), state))
    updated = replace(state, active_version_id="warm", revision=11)
    assert controller.finish(ClipVersionResult(token, target, updated))
    assert controller.collection(target, Scope.LOCAL).active_version_id == "warm"
    assert not controller.busy(target, Scope.LOCAL)


def test_unconnected_form_validates_and_reports_unsaved(ui, qtbot):
    _, controller, target, state = ui
    controller.set_collection(replace(state, connected=False))
    calls = []
    controller.operation_requested.connect(calls.append)
    dialog = dialog_for(ui, qtbot)
    dialog.name_edit.setText("Cool")
    qtbot.mouseClick(dialog.buttons.button(QDialogButtonBox.StandardButton.Ok), Qt.MouseButton.LeftButton)
    assert calls == []
    assert dialog.isVisible()
    assert dialog.error_label.text()
    assert not controller.busy(target, Scope.LOCAL)
    assert controller.collection(target, Scope.LOCAL).versions == state.versions


@pytest.mark.parametrize("name", ["", "  ", "Warm", "a\nb", "x" * 129])
def test_invalid_name_never_dispatches(ui, name):
    _, controller, target, state = ui
    assert validate_version_name(name, state)
    assert controller.submit(target, Scope.LOCAL, Op.CREATE, name=name) is None
    assert not controller.busy(target, Scope.LOCAL)


def test_rename_can_keep_its_own_name_and_uses_version_id(ui):
    _, controller, target, state = ui
    assert validate_version_name("Warm", state, excluding_id="warm") == ""
    captured = []
    controller.operation_requested.connect(captured.append)
    assert controller.submit(target, Scope.LOCAL, Op.RENAME, version_id="warm", name="Warm")
    assert captured[0].version_id == "warm"


def test_error_preserves_name_and_allows_retry(ui, qtbot):
    _, controller, target, state = ui
    dialog = dialog_for(ui, qtbot)
    dialog.name_edit.setText("Cool")
    dialog._submit()
    token = dialog._request_id
    assert not dialog.buttons.button(QDialogButtonBox.StandardButton.Ok).isEnabled()
    controller.finish(ClipVersionResult(token, target, error="Write was rejected"))
    assert dialog.name_edit.text() == "Cool"
    assert dialog.error_label.text() == "Write was rejected"
    assert dialog.buttons.button(QDialogButtonBox.StandardButton.Ok).isEnabled()
    assert controller.collection(target, Scope.LOCAL) == state
    dialog._submit()
    assert dialog._request_id != token


def test_synchronous_success_closes_dialog(ui, qtbot):
    _, controller, target, state = ui
    dialog = dialog_for(ui, qtbot)

    def finish(request):
        state_after = replace(state, versions=state.versions + (ClipVersionSummary("new", request.name),), revision=11)
        controller.finish(ClipVersionResult(request.request_id, target, state_after))

    controller.operation_requested.connect(finish)
    dialog._submit()
    assert dialog.result() == QDialog.DialogCode.Accepted
    assert not dialog.isVisible()


def test_cancel_confirmation_does_not_submit(ui, qtbot):
    _, controller, _, state = ui
    requests = []
    controller.operation_requested.connect(requests.append)
    dialog = dialog_for(ui, qtbot, Op.DELETE, state.versions[1])
    qtbot.mouseClick(dialog.buttons.button(QDialogButtonBox.StandardButton.Cancel), Qt.MouseButton.LeftButton)
    assert requests == []
    assert dialog.result() == QDialog.DialogCode.Rejected


def test_scope_copy_request_names_exact_source_and_destination(ui):
    _, controller, target, _ = ui
    controller.set_collection(ClipVersionCollection(target, Scope.REMOTE, loaded=True, connected=True))
    requests = []
    controller.operation_requested.connect(requests.append)
    assert controller.submit(target, Scope.LOCAL, Op.COPY_TO_SCOPE, version_id="warm", name="Warm copy", destination_scope=Scope.REMOTE)
    assert requests[0].version_id == "warm"
    assert requests[0].destination_scope == Scope.REMOTE


def test_active_and_default_versions_cannot_be_deleted(ui):
    _, controller, target, state = ui
    assert controller.submit(target, Scope.LOCAL, Op.DELETE, version_id="base") is None
    controller.set_collection(replace(state, active_version_id="warm"))
    assert controller.submit(target, Scope.LOCAL, Op.DELETE, version_id="warm") is None


def test_dirty_load_needs_confirmation(ui, qtbot):
    _, controller, target, state = ui
    controller.set_collection(replace(state, live_modified=True))
    assert controller.submit(target, Scope.LOCAL, Op.LOAD, version_id="warm") is None
    requests = []
    controller.operation_requested.connect(requests.append)
    controller.open_action(target, Scope.LOCAL, Op.LOAD, state.versions[1])
    dialog = next(iter(controller._dialogs))
    dialog._submit()
    assert requests[0].discard_unsaved is True


def test_context_change_rejects_old_result_and_closes_form(ui, qtbot):
    _, controller, target, state = ui
    dialog = dialog_for(ui, qtbot)
    dialog._submit()
    token = dialog._request_id
    controller.bind_context("new-project", "timeline")
    assert not dialog.isVisible()
    assert not controller.finish(ClipVersionResult(token, target, replace(state, revision=11)))
    assert not controller.set_collection(state)


def test_cancelled_interaction_cannot_accept_late_ack(ui):
    _, controller, target, state = ui
    token = controller.submit(target, Scope.LOCAL, Op.LOAD, version_id="warm")
    controller.cancel_interactions()
    assert not controller.finish(ClipVersionResult(token, target, replace(state, revision=11)))
    assert controller.collection(target, Scope.LOCAL) == state


def test_newer_list_cannot_be_replaced_by_older_ack(ui):
    _, controller, target, state = ui
    token = controller.submit(target, Scope.LOCAL, Op.LOAD, version_id="warm")
    controller.set_collection(replace(state, revision=12))
    assert controller.finish(ClipVersionResult(token, target, replace(state, revision=11)))
    assert controller.collection(target, Scope.LOCAL).revision == 12
    assert not controller.busy(target, Scope.LOCAL)


def test_scope_menu_checkmark_is_authoritative_and_refresh_is_read_only(ui):
    owner, controller, target, state = ui
    root = QMenu(owner)
    menu = ClipVersionMenu(controller, target, Scope.LOCAL, root)
    fetched = []
    controller.state_requested.connect(lambda *args: fetched.append(args))
    menu._about_to_show()
    assert fetched == [(target, Scope.LOCAL)]
    assert any(action.text().startswith("✓ ") and "Version 1" in action.text() for action in menu.actions())
    assert controller.collection(target, Scope.LOCAL) == state


def test_unloaded_and_loaded_empty_menu_are_distinct(ui):
    owner, controller, target, _ = ui
    root = QMenu(owner)
    menu = ClipVersionMenu(controller, target, Scope.REMOTE, root)
    assert len(menu.actions()) == 6  # status, separator, create, refresh, separator, delete all
    controller.set_collection(ClipVersionCollection(target, Scope.REMOTE, loaded=True, connected=True))
    assert len(menu.actions()) == 4


def test_strip_binds_right_clicked_clip_and_rehydrates_footer(qtbot, monkeypatch):
    from src.features.color.ui import timeline_clip_strip as module
    monkeypatch.setattr(module.ThumbnailProvider, "instance", lambda: MagicMock())
    monkeypatch.setattr(module, "_save_bottom_label_mode_setting", lambda mode: None)
    strip = TimelineClipStrip()
    qtbot.addWidget(strip)
    strip._current_project = SimpleNamespace(project_id="project")
    strip.version_ui.bind_context("project", "timeline")
    entry = ColorClipEntry("timeline", "clip-b", "V1", "V1", "asset", "source.mov", "/x.mov",
                           "video", 0, 48, 0, 48, "stack", False)
    strip._entries = strip._all_entries = [entry]
    strip._rebuild_cards()
    target = strip._version_target(entry)
    state = ClipVersionCollection(target, Scope.LOCAL, (ClipVersionSummary("base", "Approved", True),),
                                  "base", loaded=True, connected=True, in_use=True)
    assert strip.set_clip_version_collection(state)
    strip._set_bottom_mode(BottomLabelMode.VERSION)
    assert strip._cards[0]._bottom_label.text() == "Approved"
    strip._rebuild_cards()
    assert strip._cards[0]._bottom_label.text() == "Approved"
    strip._toggle_version_labels()
    assert strip._bottom_mode == BottomLabelMode.CODEC
    assert not strip.set_clip_version_collection(replace(state, target=replace(target, stack_id="stale")))


def test_create_ack_can_return_new_stack_for_the_same_clip(ui):
    _, controller, target, state = ui
    token = controller.submit(target, Scope.LOCAL, Op.CREATE, name="Cool")
    rebound = replace(state, target=replace(target, stack_id="new-local-stack"), revision=11)
    assert controller.finish(ClipVersionResult(token, target, rebound))
    assert controller.collection(rebound.target, Scope.LOCAL) == rebound


def test_copy_name_conflict_is_checked_in_destination_scope(ui):
    _, controller, target, state = ui
    controller.set_collection(replace(state, scope=Scope.REMOTE))
    assert controller.submit(target, Scope.LOCAL, Op.COPY_TO_SCOPE, version_id="warm",
                             name="Warm", destination_scope=Scope.REMOTE) is None


def test_ack_with_a_different_clip_cannot_rebind_target(ui):
    _, controller, target, state = ui
    token = controller.submit(target, Scope.LOCAL, Op.CREATE, name="Cool")
    bad = replace(state, target=replace(target, clip_id="other"), revision=11)
    assert controller.finish(ClipVersionResult(token, target, bad))
    assert controller.collection(target, Scope.LOCAL) == state
    assert not controller.busy(target, Scope.LOCAL)


def test_rebuilding_menu_does_not_accumulate_child_menus(ui):
    from PyQt6.QtCore import QCoreApplication, QEvent
    owner, controller, target, state = ui
    root = QMenu(owner)
    menu = ClipVersionMenu(controller, target, Scope.LOCAL, root)
    for revision in range(11, 31):
        controller.set_collection(replace(state, revision=revision))
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    direct = menu.findChildren(QMenu, options=Qt.FindChildOption.FindDirectChildrenOnly)
    assert len(direct) == len(state.versions)


def test_remote_use_ack_clears_local_display_scope(ui):
    _, controller, target, state = ui
    remote = replace(state, scope=Scope.REMOTE, in_use=True)
    assert controller.set_collection(remote)
    assert controller.collection(target, Scope.REMOTE).in_use
    assert not controller.collection(target, Scope.LOCAL).in_use


def test_context_menu_uses_pointer_target_and_not_blank_active_fallback(qtbot, monkeypatch):
    from src.features.color.ui import timeline_clip_strip as module
    monkeypatch.setattr(module.ThumbnailProvider, "instance", lambda: MagicMock())
    monkeypatch.setattr(QMenu, "exec", lambda *args: None)
    strip = TimelineClipStrip()
    qtbot.addWidget(strip)
    strip.resize(800, 147)
    strip._current_project = SimpleNamespace(project_id="project")
    strip.version_ui.bind_context("project", "timeline")
    a = ColorClipEntry("timeline", "a", "V1", "V1", "asset", "a.mov", "/a.mov",
                       "video", 0, 48, 0, 48, "a-stack", False)
    strip._entries = [a, replace(a, clip_id="b", color_stack_id="b-stack")]
    strip._rebuild_cards()
    strip.show()
    strip.set_active_clip("a")
    qtbot.waitUntil(lambda: strip._cards[1].x() > 100)
    targets = []
    monkeypatch.setattr(strip.version_ui, "add_menus", lambda menu, target: targets.append(target))
    card_b_point = strip._cards[1].mapTo(strip, QPoint(20, 20))
    strip._show_context_menu(card_b_point)
    assert [target.clip_id for target in targets] == ["b"]
    strip._show_context_menu(QPoint(790, 30))
    assert [target.clip_id for target in targets] == ["b"]


def test_missing_backend_ack_times_out_without_changing_list(qtbot):
    owner = QWidget()
    qtbot.addWidget(owner)
    controller = ClipVersionUiController(owner, request_timeout_ms=25)
    controller.bind_context("project", "timeline")
    target = ClipVersionTarget("project", "timeline", "clip", "asset", "stack")
    state = ClipVersionCollection(target, Scope.LOCAL, loaded=True, connected=True)
    controller.set_collection(state)
    with qtbot.waitSignal(controller.operation_finished, timeout=1000) as finished:
        token = controller.submit(target, Scope.LOCAL, Op.CREATE, name="Look")
    assert finished.args[0].error
    assert not controller.busy(target, Scope.LOCAL)
    assert controller.collection(target, Scope.LOCAL) == state
    assert not controller.finish(ClipVersionResult(token, target, state))


def test_inactive_scope_can_load_its_remembered_current_version(ui):
    owner, controller, target, state = ui
    controller.set_collection(replace(state, scope=Scope.REMOTE, in_use=True))
    menu = ClipVersionMenu(controller, target, Scope.LOCAL, QMenu(owner))
    version_menu = next(action.menu() for action in menu.actions() if action.menu())
    load = version_menu.actions()[0]
    assert load.isEnabled()
    assert not version_menu.title().startswith("✓")
    requests = []
    controller.operation_requested.connect(requests.append)
    load.trigger()
    assert requests[0].scope == Scope.LOCAL
    assert requests[0].version_id == "base"
    assert controller.finish(ClipVersionResult(requests[0].request_id, target, replace(state, revision=11)))
    assert controller.collection(target, Scope.LOCAL).in_use
    assert not controller.collection(target, Scope.REMOTE).in_use


def test_copy_waits_for_authoritative_destination_list(ui):
    _, controller, target, _ = ui
    requests = []
    controller.operation_requested.connect(requests.append)
    assert controller.submit(target, Scope.LOCAL, Op.COPY_TO_SCOPE, version_id="warm",
                             name="Warm copy", destination_scope=Scope.REMOTE) is None
    controller.set_collection(ClipVersionCollection(target, Scope.REMOTE, connected=True))
    assert controller.submit(target, Scope.LOCAL, Op.COPY_TO_SCOPE, version_id="warm",
                             name="Warm copy", destination_scope=Scope.REMOTE) is None
    assert requests == []


def test_keyboard_escape_cancels_and_return_submits_valid_name(ui, qtbot):
    _, controller, _, _ = ui
    requests = []
    controller.operation_requested.connect(requests.append)
    cancelled = dialog_for(ui, qtbot)
    qtbot.keyClick(cancelled.name_edit, Qt.Key.Key_Escape)
    assert cancelled.result() == QDialog.DialogCode.Rejected
    assert requests == []
    submitted = dialog_for(ui, qtbot)
    submitted.name_edit.setText("Keyboard version")
    qtbot.keyClick(submitted.name_edit, Qt.Key.Key_Return)
    assert len(requests) == 1
    assert requests[0].name == "Keyboard version"
