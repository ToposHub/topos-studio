"""Standalone Word application and optional Topos Protocol v1 adapter."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import sys
from typing import Any
from uuid import uuid4

from PyQt6.QtCore import QLockFile, QStandardPaths, QTimer
from PyQt6.QtGui import QCloseEvent, QTextCursor
from PyQt6.QtWidgets import QApplication, QMessageBox

from topos_app_sdk import AppBridge, parse_args
from topos_word import ToposWordWindow, WordDocumentBridge


class HostedWordWindow(ToposWordWindow):
    """Keep application functionality independent; adapt only the boundary."""

    def __init__(self, bridge: AppBridge) -> None:
        self.bridge = bridge
        self._request_id = ""
        self._command_busy = False
        self._recovery_lock: QLockFile | None = None
        super().__init__()
        self._set_recovery_path(None)
        self.editor.document().modificationChanged.connect(self._dirty_changed)
        bridge.command_received.connect(self.handle_command)

    def _set_recovery_path(self, path: str | None) -> None:
        if self._recovery_lock is not None:
            self._recovery_lock.unlock()
        root = Path(QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppDataLocation)) / "recovery"
        root.mkdir(parents=True, exist_ok=True)
        identity = str(Path(path).resolve()) if path else "untitled"
        key = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
        lock = QLockFile(str(root / (key + ".lock")))
        lock.setStaleLockTime(0)
        if not lock.tryLock(0):
            # A second editor for the same file must never share its backup.
            key += "-" + uuid4().hex
            lock = QLockFile(str(root / (key + ".lock")))
            lock.setStaleLockTime(0)
            if not lock.tryLock(0):
                raise OSError("Cannot reserve a document recovery file")
        self._recovery_lock = lock
        self._autosave_path = str(root / (key + ".docx"))

    def _clear_autosave(self) -> None:
        """Clean only this window's files, never another application's backup."""
        for path in (self._autosave_path, self._autosave_path + ".tmp"):
            try:
                Path(path).unlink(missing_ok=True)
            except OSError as exc:
                print(f"Cannot remove recovery file {path}: {exc}", file=sys.stderr)

    def _check_autosave_recovery(self) -> None:
        if not Path(self._autosave_path).is_file():
            return
        answer = QMessageBox.question(
            self, "恢复未保存文档", "检测到此文档的自动备份，是否恢复？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if answer == QMessageBox.StandardButton.Yes:
            try:
                WordDocumentBridge.load_docx_into_qdoc(
                    self._autosave_path, self.editor.document(), self.editor.get_printable_width())
            except (OSError, ValueError, KeyError) as exc:
                QMessageBox.warning(self, "恢复失败", str(exc))
                return
            self.editor.normalize_document_colors()
            self.editor.document().clearUndoRedoStacks()
            self.editor.document().setModified(True)
            self._update_window_title()
            self._update_word_count()
            self.outline_sidebar.refresh()
            cursor = self.editor.textCursor()
            cursor.movePosition(QTextCursor.MoveOperation.End)
            self.editor.setTextCursor(cursor)
        self._clear_autosave()

    def _dirty_changed(self, dirty: bool) -> None:
        self.bridge.emit_event("dirty", self._request_id, dirty=dirty,
                               path=self.current_file or "")

    def _load_file_from_path(self, path: str) -> bool:
        request_id = self._request_id if self._command_busy else ""
        result = super()._load_file_from_path(path)
        if result:
            self._set_recovery_path(path)
            self.bridge.emit_event("opened", request_id, path=str(Path(path).resolve()))
            QTimer.singleShot(0, self._check_autosave_recovery)
        else:
            self.bridge.emit_event("error", request_id,
                                   code="OPEN_FAILED", message="无法打开文档", path=path)
        return result

    def _write_to_path(self, path: str) -> bool:
        previous_path = self.current_file
        result = super()._write_to_path(path)
        if result:
            if previous_path != path:
                self._set_recovery_path(path)
            self.bridge.emit_event("saved", self._request_id, path=str(Path(path).resolve()))
        else:
            self.bridge.emit_event("error", self._request_id,
                                   code="SAVE_FAILED", message="文档保存失败", path=path)
        return result

    def new_document(self) -> None:
        previous = self.current_file
        super().new_document()
        if previous is not None and self.current_file is None:
            self._set_recovery_path(None)

    def _notify_export(self, path: str) -> None:
        self.bridge.emit_event("exported", self._request_id,
                               paths=[str(Path(path).resolve())], source_path=self.current_file or "")

    def handle_command(self, command: dict[str, Any]) -> None:
        request_id = command["request_id"]
        if self._command_busy:
            self.bridge.emit_event("error", request_id, code="BUSY", message="文档操作尚未结束")
            return
        self._command_busy = True
        try:
            name = command["command"]
            if name == "activate":
                self.showNormal()
                self.raise_()
                self.activateWindow()
            elif name == "close":
                self._request_id = request_id
                if not self.close():
                    self.bridge.emit_event("error", request_id, code="CLOSE_CANCELLED", message="用户取消关闭")
            elif name == "open":
                path = command["path"]
                if not os.path.isfile(path):
                    self.bridge.emit_event("error", request_id, code="FILE_NOT_FOUND", message="文件不存在", path=path)
                    return
                # Keep the old request attached to saves prompted during a switch.
                if not self._maybe_save_changes():
                    self.bridge.emit_event("error", request_id, code="OPEN_CANCELLED", message="用户取消打开")
                    return
                self._request_id = request_id
                self._load_file_from_path(path)
                self.showNormal()
                self.raise_()
                self.activateWindow()
        finally:
            self._command_busy = False
            # User-driven actions after this command are not replies to it.
            self._request_id = ""

    def closeEvent(self, event: QCloseEvent) -> None:
        super().closeEvent(event)
        if event.isAccepted():
            self.bridge.emit_event("closed", self._request_id)
            self.bridge.detach()
            if self._recovery_lock is not None:
                self._recovery_lock.unlock()


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    app = QApplication([sys.argv[0]])
    app.setApplicationName("Topos Word")
    app.setApplicationDisplayName("Topos Word")
    bridge = AppBridge(args.topos_session, app)
    window = HostedWordWindow(bridge)
    window.show()
    bridge.start()
    bridge.emit_event("ready")
    if args.file:
        QTimer.singleShot(0, lambda: window.handle_command(
            {"command": "open", "request_id": "", "path": str(Path(args.file).resolve())}))
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
