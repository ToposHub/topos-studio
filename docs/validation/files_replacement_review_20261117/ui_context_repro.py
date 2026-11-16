"""Read-only, offscreen reproduction of remaining Files UI context gaps.

Run from the repository root with:
QT_QPA_PLATFORM=offscreen .venv/bin/python \
    docs/validation/files_replacement_review_20261117/ui_context_repro.py

All network replies are synthetic. No material, database, or user preference
is written. This records observations, not an acceptance-test completion.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtNetwork import QNetworkReply
from PyQt6.QtWidgets import QApplication

from src.app.ui.native_files_batch_rename import BatchRenameDialog
from src.app.ui.native_files_transport import NativeFilesTransport


class FakeReply(QObject):
    finished = pyqtSignal()

    def __init__(self) -> None:
        super().__init__()
        self.aborted = False

    def abort(self) -> None:
        self.aborted = True

    def deleteLater(self) -> None:
        pass

    def attribute(self, *_: Any) -> int:
        return 200

    def readAll(self) -> bytes:
        return json.dumps({"files": [{"path": "/synthetic/s.png",
                                     "name": "s.png", "type": "image"}]}).encode()

    def read(self, *_: Any) -> bytes:
        return b""

    def error(self) -> QNetworkReply.NetworkError:
        return QNetworkReply.NetworkError.NoError

    def errorString(self) -> str:
        return ""


class FakeNetwork:
    def __init__(self) -> None:
        self.pending: list[FakeReply] = []

    def get(self, _: Any) -> FakeReply:
        reply = FakeReply()
        self.pending.append(reply)
        return reply


def main() -> None:
    app = QApplication.instance() or QApplication([])
    dialog = BatchRenameDialog(["/synthetic/a.png", "/synthetic/b.png"])
    emitted: list[tuple[str, str]] = []
    dialog.preview_requested.connect(lambda template, token: emitted.append((template, token)))
    dialog.template_edit.setText("{name}_OLD")
    dialog._preview()
    old_token = emitted[-1][1]
    dialog.template_edit.setText("{name}_NEW")
    dialog.show_preview({"session_id": old_token,
                         "plan": [{"src_name": "a.png", "dst_name": "a_OLD.png"}],
                         "errors": []})
    print(json.dumps({"case": "FR-13 old preview after template edit",
                      "confirmed_template": dialog._confirmed_template,
                      "execute_enabled": dialog.execute_button.isEnabled()}, ensure_ascii=False))

    transport = NativeFilesTransport(lambda: "http://127.0.0.1:8765", "synthetic-token")
    network = FakeNetwork()
    transport._network = network
    events: list[str] = []
    transport.search_loaded.connect(lambda _: events.append("search"))
    transport.collection_loaded.connect(lambda _: events.append("collection"))
    transport.search_library("s")
    old_search = network.pending[-1]
    transport.load_collection(3)
    new_collection = network.pending[-1]
    new_collection.finished.emit()
    old_search.finished.emit()
    print(json.dumps({"case": "FR-09 search after newer collection",
                      "loaded_events": events,
                      "old_search_aborted": old_search.aborted}, ensure_ascii=False))

    transport.shutdown()
    dialog.close()
    app.processEvents()


if __name__ == "__main__":
    main()
