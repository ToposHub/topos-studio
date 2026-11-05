"""Read-only source preview cache, scope safety and async late-result guards."""
from __future__ import annotations

from typing import Any

from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtGui import QColor, QPixmap
from PyQt6.QtWidgets import QMainWindow

from src.app.controllers.color_node_preview import ColorNodeSourcePreview
from src.features.color.ui.node_editor_panel import ColorNodePanel


class Provider(QObject):
    thumbnail_ready = pyqtSignal(str, QPixmap)

    def __init__(self) -> None:
        super().__init__()
        self.requests: list[tuple[str, int]] = []
        self.cached: QPixmap | None = None

    def get_thumbnail(self, path: str, size: int) -> QPixmap | None:
        self.requests.append((path, size))
        return self.cached


def _host(qtbot: Any) -> tuple[Any, Provider, ColorNodeSourcePreview]:
    host = QMainWindow()
    host.current_file_path = "clip_a.mov"
    host.color_node_panel = ColorNodePanel(host)
    host.setCentralWidget(host.color_node_panel)
    host.resize(700, 420)
    qtbot.addWidget(host)
    host.show()
    provider = Provider()
    bridge = ColorNodeSourcePreview(host, provider)
    bridge._timer.stop()
    return host, provider, bridge


def _image(color: str = "#bb7744") -> QPixmap:
    image = QPixmap(160, 90)
    image.fill(QColor(color))
    return image


def test_cached_source_thumbnail_reaches_card_without_edit(qtbot: Any) -> None:
    host, provider, bridge = _host(qtbot)
    provider.cached = _image()
    edits: list[str] = []
    panel = host.color_node_panel
    before = panel.snapshot()
    panel.edit_requested.connect(lambda name, _: edits.append(name))
    bridge.refresh()
    card = panel.view.cards[panel.view.state.graph.active_id]
    assert not card._thumbnail.isNull()
    assert "Source preview" in card.toolTip()
    assert panel.snapshot() == before
    assert not edits
    bridge.refresh()
    assert len(provider.requests) == 1


def test_late_result_from_departed_source_is_ignored(qtbot: Any) -> None:
    host, provider, bridge = _host(qtbot)
    changes: list[QPixmap | None] = []
    bridge.source_thumbnail_changed.connect(changes.append)
    bridge.refresh()
    host.current_file_path = "clip_b.mov"
    bridge.refresh()
    provider.thumbnail_ready.emit("clip_a.mov", _image())
    assert host.color_node_panel.view._source_thumbnail is None
    assert changes == [None, None]
    provider.thumbnail_ready.emit("clip_b.mov", _image())
    assert host.color_node_panel.view._source_thumbnail is not None
    assert isinstance(changes[-1], QPixmap)


def test_clearing_media_clears_card_picture(qtbot: Any) -> None:
    host, provider, bridge = _host(qtbot)
    provider.cached = _image()
    bridge.refresh()
    host.current_file_path = ""
    bridge.refresh()
    view = host.color_node_panel.view
    assert view._source_thumbnail is None
    assert view.cards[view.state.graph.active_id]._thumbnail.isNull()


def test_added_node_inherits_source_preview_and_hide_toggle_works(qtbot: Any) -> None:
    host, provider, bridge = _host(qtbot)
    provider.cached = _image()
    bridge.refresh()
    panel = host.color_node_panel
    node_id = panel.view.apply_edit("add_serial", panel.view.state.add_serial)
    assert not panel.view.cards[node_id]._thumbnail.isNull()
    panel._toggle_thumbnails()
    assert panel.view.cards[node_id]._thumbnail.isNull()
    panel._toggle_thumbnails()
    assert not panel.view.cards[node_id]._thumbnail.isNull()


def test_evaluated_node_picture_is_not_overwritten_by_source(qtbot: Any) -> None:
    host, provider, bridge = _host(qtbot)
    view = host.color_node_panel.view
    node_id = view.state.graph.active_id
    view.set_thumbnail(node_id, _image("#00ff00"))
    view.set_source_thumbnail(_image("#ff0000"), "Source preview")
    assert view.cards[node_id]._thumbnail.toImage().pixelColor(5, 5) == QColor("#00ff00")
    assert "Evaluated node output" in view.cards[node_id].toolTip()


def test_hidden_panel_does_not_request_or_decode(qtbot: Any) -> None:
    host, provider, bridge = _host(qtbot)
    host.color_node_panel.hide()
    bridge.refresh()
    assert not provider.requests
