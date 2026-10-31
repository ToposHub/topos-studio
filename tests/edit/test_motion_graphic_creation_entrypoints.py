from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from PyQt6.QtCore import QPoint, QPointF
from PyQt6.QtGui import QAction, QContextMenuEvent
from PyQt6.QtWidgets import QGraphicsScene, QGraphicsView, QMenu

from src.features.edit.core.project import EditProject
from src.features.edit.ui.media_pool_panel import MediaPoolPanel
from src.features.edit.ui.timeline_items import TrackHeaderItem
from src.features.edit.ui.timeline_view import TimelineView
from src.features.teaching_animation.module import TeachingAnimationModule


_CREATE_LABEL = "New Motion Graphic Clip"


def _menu_spy(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    labels: list[str] = []

    class _MenuSpy(QMenu):
        def exec(self, *args: Any, **kwargs: Any) -> QAction | None:
            del args, kwargs
            labels.extend(action.text() for action in self.actions())
            action = next(
                (item for item in self.actions() if item.text() == _CREATE_LABEL),
                None,
            )
            if action is not None:
                action.trigger()
            return action

    monkeypatch.setattr("PyQt6.QtWidgets.QMenu", _MenuSpy)
    monkeypatch.setattr("src.features.edit.ui.media_pool_panel.QMenu", _MenuSpy)
    return labels


def test_media_pool_blank_area_creates_motion_graphic_through_feature(
    qapp: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    labels = _menu_spy(monkeypatch)
    panel = MediaPoolPanel()
    panel.set_project(EditProject.create("MG context menu"))
    panel.resize(800, 600)
    panel.show()
    qapp.processEvents()

    window = object()
    created_for: list[object] = []
    module = TeachingAnimationModule()
    module.create_empty_motion_graphic = lambda host: created_for.append(host)  # type: ignore[method-assign]
    module._attach_media_pool(window, panel)

    assert panel.grid_view is not None
    panel._on_grid_context_menu(panel.grid_view.viewport().rect().center())

    assert _CREATE_LABEL in labels
    assert created_for == [window]
    panel.close()


def test_list_mode_media_pool_menu_offers_motion_graphic_creation(
    qapp: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    labels = _menu_spy(monkeypatch)
    panel = MediaPoolPanel()
    panel.set_project(EditProject.create("MG list context menu"))
    panel.resize(800, 600)
    panel.show()
    qapp.processEvents()
    panel.set_motion_graphic_creation_available(True)

    panel._on_tree_context_menu(QPoint(1, 1))

    assert _CREATE_LABEL in labels
    panel.close()


def test_media_pool_hides_creation_when_teaching_animation_is_not_attached(
    qapp: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    labels = _menu_spy(monkeypatch)
    panel = MediaPoolPanel()
    panel.set_project(EditProject.create("MG disabled context menu"))
    panel.resize(800, 600)
    panel.show()
    qapp.processEvents()

    assert panel.grid_view is not None
    panel._on_grid_context_menu(panel.grid_view.viewport().rect().center())

    assert _CREATE_LABEL not in labels
    panel.close()


def test_video_track_header_menu_has_no_motion_graphic_create_action(
    qapp: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    labels = _menu_spy(monkeypatch)
    header = TrackHeaderItem("video-1", "V1", 0, "video")
    scene = QGraphicsScene()
    scene.addItem(header)

    header._show_context_menu(QPointF(0.0, 0.0))

    assert _CREATE_LABEL not in labels


def test_empty_video_timeline_context_has_no_motion_graphic_create_action(
    qapp: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    labels = _menu_spy(monkeypatch)
    view = TimelineView()
    view._timeline = SimpleNamespace(
        get_track_by_id=lambda _track_id: SimpleNamespace(track_type="video")
    )
    view._layout_engine = SimpleNamespace(
        track_at_y_with_area=lambda _scene_y: ("video-1", "video")
    )
    monkeypatch.setattr(
        TimelineView, "mapToScene", lambda _self, _pos: QPointF(300.0, 200.0)
    )
    monkeypatch.setattr(TimelineView, "itemAt", lambda _self, _pos: None)
    monkeypatch.setattr(QGraphicsView, "contextMenuEvent", lambda _self, event: event.accept())
    event = QContextMenuEvent(
        QContextMenuEvent.Reason.Mouse, QPoint(300, 200), QPoint(300, 200)
    )

    view.contextMenuEvent(event)

    assert _CREATE_LABEL not in labels
    view.close()
