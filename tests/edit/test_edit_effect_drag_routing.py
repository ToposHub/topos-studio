"""Internal effect MIME must take precedence over the external-file fallback."""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import QByteArray, QMimeData, QPoint, Qt, QUrl
from PyQt6.QtGui import QDragEnterEvent

from src.features.edit.core.project import EditProject
from src.features.edit.ui.timeline_view import MIME_TYPE_EFFECT, TimelineView


def _enter(view: TimelineView, mime: QMimeData) -> QDragEnterEvent:
    event = QDragEnterEvent(
        QPoint(220, 120), Qt.DropAction.CopyAction, mime,
        Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
    )
    view.dragEnterEvent(event)
    return event


def test_effect_drag_is_accepted_even_when_project_unset(qapp, qtbot) -> None:
    view = TimelineView()
    qtbot.addWidget(view)
    mime = QMimeData()
    mime.setData(MIME_TYPE_EFFECT, QByteArray(b"blur"))

    assert _enter(view, mime).isAccepted()
    assert view._drag_effect_type == "blur"


def test_effect_drag_wins_over_hasurls(qapp, qtbot) -> None:
    view = TimelineView()
    qtbot.addWidget(view)
    mime = QMimeData()
    mime.setData(MIME_TYPE_EFFECT, QByteArray(b"sharpen"))
    mime.setUrls([QUrl.fromLocalFile("/tmp/fake.mp4")])

    assert _enter(view, mime).isAccepted()
    assert view._drag_effect_type == "sharpen"
    assert view._drag_file_paths == []


def test_external_file_drag_auto_creates_project_when_unset(qapp, qtbot, tmp_path: Path) -> None:
    """A Finder drop may be the first action in an otherwise empty session."""
    view = TimelineView()
    qtbot.addWidget(view)
    project = EditProject.create("finder-drop")
    view.project_needed.connect(lambda: view.set_project(project))
    path = tmp_path / "example.mp4"
    path.touch()
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(path))])

    assert _enter(view, mime).isAccepted()
    assert view._project is project
    assert view._drag_file_paths == [str(path)]
