"""Capture target changes while an overwrite dialog is open."""
from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PyQt6.QtWidgets import QMessageBox

from src.app.controllers.layer_controller import LayerOpsMixin
from src.features.color.core.gallery_contracts import GalleryCaptureRequest
from src.features.color.core.gallery_repository import GalleryFolderRepository


@pytest.mark.parametrize("change", ["revision", "removed"])
def test_target_is_rechecked_after_overwrite_dialog(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str) -> None:
    root = tmp_path / "Presets"
    folder = root / "User"
    folder.mkdir(parents=True)
    existing = folder / "Still.topos"
    existing.write_bytes(b"old")
    repository = GalleryFolderRepository(str(root))
    album = repository.album_for_folder(str(folder))
    request = GalleryCaptureRequest(
        request_id="capture-review", album_id=album.album_id,
        expected_target_revision=album.revision)
    controller = LayerOpsMixin.__new__(LayerOpsMixin)
    controller.presets_panel = SimpleNamespace(_repository=repository)
    controller._new_still_name = lambda: "Still"
    controller._commit_still_capture = Mock(return_value=True)

    def accept_after_target_change(*_args: object) -> QMessageBox.StandardButton:
        if change == "removed":
            existing.unlink()
            folder.rmdir()
        else:
            stat = folder.stat()
            os.utime(folder, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
        return QMessageBox.StandardButton.Yes

    monkeypatch.setattr(QMessageBox, "question", accept_after_target_change)
    session = SimpleNamespace(check_before_write=lambda: None)
    result = controller._persist_still_capture(
        object(), False, {}, request=request, session=session)
    assert result.status == "rejected"
    assert result.request_id == request.request_id
    assert result.reason == ("album_unavailable" if change == "removed"
                             else "album_revision_changed")
    controller._commit_still_capture.assert_not_called()
