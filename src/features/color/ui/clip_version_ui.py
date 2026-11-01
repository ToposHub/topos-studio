"""Color clip version presentation; persistence and command dispatch are host-owned.

Only display metadata crosses this boundary. No grades/snapshots are stored here.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
from uuid import uuid4

from PyQt6.QtCore import QObject, Qt, QTimer, pyqtSignal
from PyQt6.QtWidgets import (
    QDialog, QDialogButtonBox, QLabel, QLineEdit, QMenu, QVBoxLayout, QWidget,
)

from src.shared.i18n.i18n import I18N


class VersionScope(str, Enum):
    LOCAL = "local"
    REMOTE = "remote"  # Shared by source media; this does not mean network storage.


class VersionOperation(str, Enum):
    CREATE = "create"
    LOAD = "load"
    RENAME = "rename"
    DUPLICATE = "duplicate"
    DELETE = "delete"
    DELETE_ALL = "delete_all"
    COPY_TO_SCOPE = "copy_to_scope"


@dataclass(frozen=True)
class ClipVersionTarget:
    project_id: str
    timeline_id: str
    clip_id: str
    asset_id: str
    stack_id: str | None


@dataclass(frozen=True)
class ClipVersionSummary:
    version_id: str
    name: str
    is_default: bool = False
    diagnostic: str = ""


@dataclass(frozen=True)
class ClipVersionCollection:
    target: ClipVersionTarget
    scope: VersionScope
    versions: tuple[ClipVersionSummary, ...] = ()
    active_version_id: str | None = None
    revision: int = 0
    loaded: bool = False
    connected: bool = False
    live_modified: bool = False
    in_use: bool = False
    capabilities: frozenset[VersionOperation] = frozenset(VersionOperation)
    unavailable_reason: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.versions, tuple) or not isinstance(self.capabilities, frozenset):
            raise TypeError("Version collections require immutable tuples and capability sets")
        ids = [version.version_id for version in self.versions]
        if self.revision < 0 or len(ids) != len(set(ids)):
            raise ValueError("Version collection requires a non-negative revision and unique IDs")
        if self.active_version_id is not None and self.active_version_id not in ids:
            raise ValueError("Active version must exist in the collection")


@dataclass(frozen=True)
class ClipVersionRequest:
    request_id: str
    target: ClipVersionTarget
    scope: VersionScope
    operation: VersionOperation
    expected_revision: int
    version_id: str | None = None
    name: str = ""
    destination_scope: VersionScope | None = None
    discard_unsaved: bool = False


@dataclass(frozen=True)
class ClipVersionResult:
    request_id: str
    target: ClipVersionTarget
    collection: ClipVersionCollection | None = None
    error: str = ""


def _tr(key: str, default: str, **values: object) -> str:
    return I18N.tr(f"clip_versions.{key}", default, **values)


def _scope_title(scope: VersionScope) -> str:
    return (_tr("local", "Local Versions") if scope == VersionScope.LOCAL
            else _tr("remote", "Remote Versions"))


def validate_version_name(name: str, collection: ClipVersionCollection,
                          excluding_id: str | None = None) -> str:
    """Use case-sensitive trimmed names plus the UI's 128-character policy.

    Control characters include DEL (0x7f) — aligned with the backend's
    ``color_versions.validate_version_name`` so both layers reject the
    same inputs (review S8).
    """
    cleaned = name.strip()
    if not cleaned:
        return _tr("name_required", "Enter a version name.")
    if len(cleaned) > 128 or any(
            ord(character) < 32 or ord(character) == 127
            for character in cleaned):
        return _tr("name_invalid", "Use at most 128 characters without line breaks or control characters.")
    if any(version.name == cleaned and version.version_id != excluding_id for version in collection.versions):
        return _tr("name_exists", "A version with this name already exists.")
    return ""


class ClipVersionUiController(QObject):
    """UI state/acknowledgement boundary. The host supplies all authoritative data."""

    state_requested = pyqtSignal(object, object)  # target, scope
    operation_requested = pyqtSignal(object)  # ClipVersionRequest
    state_changed = pyqtSignal(object, object)  # target, scope
    operation_finished = pyqtSignal(object)  # ClipVersionResult
    interactions_cancelled = pyqtSignal()
    feedback = pyqtSignal(str)

    def __init__(self, parent: QWidget, *, request_timeout_ms: int = 15000) -> None:
        super().__init__(parent)
        if request_timeout_ms <= 0:
            raise ValueError("Version request timeout must be positive")
        self._owner = parent
        self._request_timeout_ms = request_timeout_ms
        self._context: tuple[str, str] | None = None
        self._collections: dict[tuple[ClipVersionTarget, VersionScope], ClipVersionCollection] = {}
        self._pending: dict[str, ClipVersionRequest] = {}
        self._timers: dict[str, QTimer] = {}
        self._dialogs: set[VersionActionDialog] = set()
        self.last_error = ""

    def bind_context(self, project_id: str, timeline_id: str) -> None:
        context = (project_id, timeline_id)
        if context != self._context:
            self.cancel_interactions()
            self._collections.clear()
            self._context = context

    def cancel_interactions(self) -> None:
        pending = list(self._pending.values())
        self._pending.clear()
        for timer in self._timers.values():
            timer.stop()
            timer.deleteLater()
        self._timers.clear()
        self.interactions_cancelled.emit()
        for request in pending:
            self.state_changed.emit(request.target, request.scope)

    def collection(self, target: ClipVersionTarget, scope: VersionScope) -> ClipVersionCollection:
        return self._collections.get((target, scope), ClipVersionCollection(target, scope))

    def set_collection(self, collection: ClipVersionCollection) -> bool:
        target = collection.target
        if (target.project_id, target.timeline_id) != self._context:
            return False
        previous = self._collections.get((target, collection.scope))
        if previous is not None and collection.revision < previous.revision:
            return False
        if collection.in_use:
            for key, other in list(self._collections.items()):
                if key[0] == target and key[1] != collection.scope and other.in_use:
                    self._collections[key] = replace(other, in_use=False)
        self._collections[(target, collection.scope)] = collection
        self.state_changed.emit(target, collection.scope)
        return True

    def busy(self, target: ClipVersionTarget, scope: VersionScope) -> bool:
        return any(request.target == target for request in self._pending.values())

    def submit(self, target: ClipVersionTarget, scope: VersionScope, operation: VersionOperation,
               *, version_id: str | None = None, name: str = "",
               destination_scope: VersionScope | None = None,
               discard_unsaved: bool = False) -> str | None:
        state = self.collection(target, scope)
        self.last_error = ""
        if (target.project_id, target.timeline_id) != self._context:
            self.last_error = _tr("context_changed", "The project or timeline changed. Reopen the version menu.")
        elif not state.connected:
            self.last_error = state.unavailable_reason or _tr(
                "not_connected", "Version management is not enabled. No changes were saved.")
        elif not state.loaded:
            self.last_error = _tr("loading", "Versions have not loaded yet.")
        elif operation not in state.capabilities:
            self.last_error = _tr("unsupported", "This version operation is unavailable.")
        elif self.busy(target, scope):
            self.last_error = _tr("pending", "A version operation is in progress.")
        if self.last_error:
            self.feedback.emit(self.last_error)
            return None
        if operation in (VersionOperation.CREATE, VersionOperation.RENAME, VersionOperation.DUPLICATE):
            self.last_error = validate_version_name(
                name, state, version_id if operation == VersionOperation.RENAME else None,
            )
        if operation == VersionOperation.COPY_TO_SCOPE and destination_scope is not None:
            destination = self.collection(target, destination_scope)
            if not destination.connected:
                self.last_error = destination.unavailable_reason or _tr(
                    "not_connected", "Version management is not enabled. No changes were saved.")
            elif not destination.loaded:
                self.last_error = _tr("loading", "Versions have not loaded yet.")
            else:
                self.last_error = validate_version_name(name, destination)
        version = next((item for item in state.versions if item.version_id == version_id), None)
        if operation not in (VersionOperation.CREATE, VersionOperation.DELETE_ALL) and version is None:
            self.last_error = _tr("version_missing", "This version no longer exists. Refresh the list.")
        if operation == VersionOperation.DELETE and version is not None and (
                version.is_default or version_id == state.active_version_id or len(state.versions) <= 1):
            self.last_error = _tr("delete_protected", "Switch to another version before deleting. The default version is protected.")
        if operation == VersionOperation.DELETE_ALL and len(state.versions) <= 1:
            self.last_error = _tr("nothing_to_delete", "There are no alternate versions to delete.")
        if operation == VersionOperation.LOAD and state.live_modified and not discard_unsaved:
            self.last_error = _tr("confirm_replace", "Confirm replacing the current unsaved grade before loading this version.")
        if operation == VersionOperation.COPY_TO_SCOPE and (destination_scope is None or destination_scope == scope):
            self.last_error = _tr("invalid_destination", "Choose the other version scope.")
        if self.last_error:
            self.feedback.emit(self.last_error)
            return None
        request = ClipVersionRequest(str(uuid4()), target, scope, operation, state.revision,
                                     version_id, name.strip(), destination_scope, discard_unsaved)
        self._pending[request.request_id] = request
        timer = QTimer(self)
        timer.setSingleShot(True)
        timer.timeout.connect(lambda: self.finish(ClipVersionResult(
            request.request_id, request.target,
            error=_tr("timeout", "The version operation timed out. Refresh the list before retrying."),
        )))
        self._timers[request.request_id] = timer
        timer.start(self._request_timeout_ms)
        self.state_changed.emit(target, scope)
        self.operation_requested.emit(request)
        return request.request_id

    def finish(self, result: ClipVersionResult) -> bool:
        request = self._pending.get(result.request_id)
        if request is None or request.target != result.target:
            return False
        if (result.target.project_id, result.target.timeline_id) != self._context:
            return False
        collection = result.collection
        valid_target = collection is not None and collection.target == request.target
        if collection is not None and request.operation in (VersionOperation.CREATE, VersionOperation.LOAD, VersionOperation.DELETE_ALL):
            # The backend may create/rebind a stack atomically. Clip identity
            # stays fixed; the acknowledged collection names the new binding.
            a, b = collection.target, request.target
            valid_target = (a.project_id, a.timeline_id, a.clip_id, a.asset_id) == (b.project_id, b.timeline_id, b.clip_id, b.asset_id)
        if not result.error and (collection is None or not valid_target
                                 or collection.scope != request.scope
                                 or collection.revision < request.expected_revision):
            result = ClipVersionResult(result.request_id, result.target, error=_tr(
                "invalid_result", "The version result could not be verified. Refresh the list and retry."))
        self._pending.pop(request.request_id)
        timer = self._timers.pop(request.request_id, None)
        if timer is not None:
            timer.stop()
            timer.deleteLater()
        if not result.error and collection is not None:
            if not self.set_collection(collection):
                result = ClipVersionResult(result.request_id, result.target, error=_tr(
                    "stale_result", "A newer version list is already displayed. Refresh and retry."))
        self.state_changed.emit(request.target, request.scope)
        if result.error:
            self.feedback.emit(result.error)
        self.operation_finished.emit(result)
        return True

    def open_action(self, target: ClipVersionTarget, scope: VersionScope,
                    operation: VersionOperation, version: ClipVersionSummary | None = None,
                    destination_scope: VersionScope | None = None) -> None:
        state = self.collection(target, scope)
        if operation == VersionOperation.LOAD and not state.live_modified:
            self.submit(target, scope, operation, version_id=version.version_id if version else None)
            return
        if operation == VersionOperation.COPY_TO_SCOPE and destination_scope is not None:
            self.state_requested.emit(target, destination_scope)
        dialog = VersionActionDialog(self, target, scope, operation, version, destination_scope, self._owner)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self._dialogs.add(dialog)
        dialog.finished.connect(lambda _result: self._dialogs.discard(dialog))
        dialog.open()

    def add_menus(self, parent: QMenu, target: ClipVersionTarget) -> None:
        for scope in VersionScope:
            parent.addMenu(ClipVersionMenu(self, target, scope, parent))


class VersionActionDialog(QDialog):
    """Validated naming/confirmation form; closes only after a successful ack."""

    def __init__(self, controller: ClipVersionUiController, target: ClipVersionTarget,
                 scope: VersionScope, operation: VersionOperation,
                 version: ClipVersionSummary | None = None,
                 destination_scope: VersionScope | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._controller, self._target, self._scope = controller, target, scope
        self._operation, self._version, self._destination = operation, version, destination_scope
        self._request_id: str | None = None
        self._submitting = False
        self._early_result: ClipVersionResult | None = None
        self.setWindowTitle(_operation_title(operation))
        self.setMinimumWidth(380)
        self.setStyleSheet(
            "QDialog { background: #242426; color: #dedee0; }"
            "QLabel { color: #dedee0; background: transparent; }"
            "QLineEdit { background: #171719; color: #eeeeef; border: 1px solid #66666a;"
            " border-radius: 3px; padding: 6px; }"
        )
        layout = QVBoxLayout(self)
        summary = _scope_title(scope)
        if destination_scope is not None:
            summary += f" → {_scope_title(destination_scope)}"
        context_label = QLabel(summary)
        context_label.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(context_label)
        self.name_edit: QLineEdit | None = None
        if operation in (VersionOperation.CREATE, VersionOperation.RENAME, VersionOperation.DUPLICATE, VersionOperation.COPY_TO_SCOPE):
            layout.addWidget(QLabel(_tr("name", "Version name")))
            self.name_edit = QLineEdit(self)
            self.name_edit.setMaxLength(128)
            state = controller.collection(target, scope)
            if operation in (VersionOperation.RENAME, VersionOperation.COPY_TO_SCOPE) and version is not None:
                suggested = version.name
            else:
                number = 1
                while any(item.name == _tr("default_name", "Version {number}", number=number) for item in state.versions):
                    number += 1
                suggested = _tr("default_name", "Version {number}", number=number)
            self.name_edit.setText(suggested)
            self.name_edit.selectAll()
            layout.addWidget(self.name_edit)
            self.name_edit.textChanged.connect(self._validate)
        else:
            name = version.name if version else ""
            messages = {
                VersionOperation.DELETE: _tr("delete_confirm", 'Delete version “{name}”? The current grade will be kept.', name=name),
                VersionOperation.DELETE_ALL: _tr("delete_all_confirm", "Delete all alternate versions and load the default version?"),
                VersionOperation.LOAD: _tr("load_confirm", 'Load “{name}” and replace the current unsaved grade?', name=name),
                VersionOperation.COPY_TO_SCOPE: _tr("copy_confirm", 'Copy version “{name}” to {scope}?', name=name,
                                                  scope=_scope_title(destination_scope or VersionScope.LOCAL)),
            }
            message = QLabel(messages.get(operation, ""))
            message.setTextFormat(Qt.TextFormat.PlainText)
            message.setWordWrap(True)
            layout.addWidget(message)
        self.error_label = QLabel(self)
        self.error_label.setTextFormat(Qt.TextFormat.PlainText)
        self.error_label.setWordWrap(True)
        self.error_label.setStyleSheet("color: #f38c83;")
        layout.addWidget(self.error_label)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setText(_operation_title(operation))
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setText(_tr("cancel", "Cancel"))
        self.buttons.accepted.connect(self._submit)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        controller.operation_finished.connect(self._finished_request)
        controller.interactions_cancelled.connect(self.reject)
        controller.state_changed.connect(self._state_changed)
        self._validate()

    def _validate(self) -> None:
        error = ""
        if self.name_edit is not None:
            validation_scope = self._destination if self._operation == VersionOperation.COPY_TO_SCOPE else self._scope
            error = validate_version_name(self.name_edit.text(), self._controller.collection(self._target, validation_scope or self._scope),
                                          self._version.version_id if self._operation == VersionOperation.RENAME and self._version else None)
        self.error_label.setText(error)
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(
            not error and self._request_id is None and not self._controller.busy(self._target, self._scope))

    def _state_changed(self, target: ClipVersionTarget, scope: VersionScope) -> None:
        if target == self._target and scope in (self._scope, self._destination) and self._request_id is None:
            self._validate()

    def _submit(self) -> None:
        self._submitting = True
        self._early_result = None
        self._request_id = self._controller.submit(
            self._target, self._scope, self._operation,
            version_id=self._version.version_id if self._version else None,
            name=self.name_edit.text() if self.name_edit is not None else "",
            destination_scope=self._destination, discard_unsaved=self._operation == VersionOperation.LOAD,
        )
        self._submitting = False
        if self._request_id is None:
            self.error_label.setText(self._controller.last_error)
            return
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(False)
        self.error_label.setText(_tr("pending", "A version operation is in progress."))
        if self._early_result is not None:
            self._finished_request(self._early_result)

    def _finished_request(self, result: ClipVersionResult) -> None:
        if self._submitting:
            self._early_result = result
            return
        if result.request_id != self._request_id:
            return
        self._request_id = None
        if result.error:
            self._validate()
            self.error_label.setText(result.error)
        else:
            self.accept()


def _operation_title(operation: VersionOperation) -> str:
    labels = {
        VersionOperation.CREATE: ("create", "Create New Version"),
        VersionOperation.LOAD: ("load", "Load"),
        VersionOperation.RENAME: ("rename", "Rename"),
        VersionOperation.DUPLICATE: ("duplicate", "Duplicate"),
        VersionOperation.DELETE: ("delete", "Delete"),
        VersionOperation.DELETE_ALL: ("delete_all", "Delete All Versions"),
        VersionOperation.COPY_TO_SCOPE: ("copy_scope", "Copy to Other Scope"),
    }
    return _tr(*labels[operation])


class ClipVersionMenu(QMenu):
    """A live submenu whose current-version mark is supplied by the host."""

    def __init__(self, controller: ClipVersionUiController, target: ClipVersionTarget,
                 scope: VersionScope, parent: QMenu) -> None:
        super().__init__(_scope_title(scope), parent)
        self._controller, self._target, self._scope = controller, target, scope
        self.setToolTipsVisible(True)
        self.setToolTip(_tr("remote_hint", "Remote versions are shared by clips using the same source media.")
                        if scope == VersionScope.REMOTE else _tr("local_hint", "Local versions belong to this timeline clip."))
        controller.state_changed.connect(self._state_changed)
        self.aboutToShow.connect(self._about_to_show)
        self._rebuild()

    def _about_to_show(self) -> None:
        self._controller.state_requested.emit(self._target, self._scope)
        self._rebuild()

    def _state_changed(self, target: ClipVersionTarget, scope: VersionScope) -> None:
        if target == self._target and scope == self._scope:
            self._rebuild()

    def _rebuild(self) -> None:
        old_submenus = [action.menu() for action in self.actions() if action.menu() is not None]
        self.clear()
        for submenu in old_submenus:
            submenu.deleteLater()
        controller, target, scope = self._controller, self._target, self._scope
        state = controller.collection(target, scope)
        busy = controller.busy(target, scope)
        if not state.connected or not state.loaded or busy:
            text = (_tr("pending", "A version operation is in progress.") if busy else
                    state.unavailable_reason or (_tr("loading", "Versions have not loaded yet.") if state.connected
                    else _tr("not_connected", "Version management is not enabled. No changes were saved.")))
            self.addAction(text).setEnabled(False)
            self.addSeparator()
        create = self.addAction(_operation_title(VersionOperation.CREATE) + "…")
        create.setEnabled(not busy and VersionOperation.CREATE in state.capabilities and (state.loaded or not state.connected))
        create.triggered.connect(lambda: controller.open_action(target, scope, VersionOperation.CREATE))
        refresh = self.addAction(_tr("refresh", "Refresh Versions"))
        refresh.setEnabled(not busy)
        refresh.triggered.connect(lambda: controller.state_requested.emit(target, scope))
        self.addSeparator()
        for version in state.versions:
            label = ("✓ " if state.in_use and version.version_id == state.active_version_id else "    ") + version.name.replace("&", "&&")
            submenu = self.addMenu(label)
            submenu.setToolTipsVisible(True)
            submenu.setToolTip(version.diagnostic)
            for operation in (VersionOperation.LOAD, VersionOperation.RENAME, VersionOperation.DUPLICATE, VersionOperation.DELETE):
                action = submenu.addAction(_operation_title(operation) + ("…" if operation != VersionOperation.LOAD else ""))
                action.setData((operation.value, version.version_id))
                enabled = not busy and state.loaded and operation in state.capabilities
                if operation == VersionOperation.LOAD and state.in_use and version.version_id == state.active_version_id:
                    enabled = False
                if operation == VersionOperation.DELETE and (version.is_default or version.version_id == state.active_version_id or len(state.versions) <= 1):
                    enabled = False
                action.setEnabled(enabled)
                action.triggered.connect(lambda _, item=version, op=operation: controller.open_action(target, scope, op, item))
            other = VersionScope.REMOTE if scope == VersionScope.LOCAL else VersionScope.LOCAL
            copy = submenu.addAction(_tr("copy_to", "Copy to {scope}", scope=_scope_title(other)) + "…")
            copy.setEnabled(not busy and state.loaded and VersionOperation.COPY_TO_SCOPE in state.capabilities)
            copy.triggered.connect(lambda _, item=version, destination=other: controller.open_action(
                target, scope, VersionOperation.COPY_TO_SCOPE, item, destination))
        delete_all = self.addAction(_operation_title(VersionOperation.DELETE_ALL) + "…")
        delete_all.setEnabled(not busy and state.loaded and len(state.versions) > 1 and VersionOperation.DELETE_ALL in state.capabilities)
        delete_all.triggered.connect(lambda: controller.open_action(target, scope, VersionOperation.DELETE_ALL))
