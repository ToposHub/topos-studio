"""Native panel player host: routing, switching and close semantics."""
from __future__ import annotations

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import QWidget

from src.shared.playback import player_host


class _FakePlayer(QWidget):
    request_prev = pyqtSignal()
    request_next = pyqtSignal()
    play_failed = pyqtSignal(str)
    closed = pyqtSignal()
    toast_requested = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.played = []
        self.stopped = 0

    def play_video(self, path):
        self.played.append(path)
        self.show()

    def stop(self):
        self.stopped += 1
        self.hide()
        self.closed.emit()


def test_host_routes_switches_and_closes(qtbot, monkeypatch):
    container = QWidget()
    qtbot.addWidget(container)
    container.resize(320, 180)

    monkeypatch.setattr(player_host, "decide_route", lambda path: "topos")
    import src.shared.playback.topos_player as topos_module
    monkeypatch.setattr(topos_module, "ToposPlayer", _FakePlayer)

    host = player_host.NativePlayerHost(container)
    closed = []
    host.closed.connect(lambda: closed.append(True))
    host.play("/media/a.topos")

    player = host._players["topos"]
    assert player.played == ["/media/a.topos"]
    assert player.geometry().size() == container.size()
    assert host.current_route == "topos"

    # Player-originated close must collapse the native preview.
    player.closed.emit()
    assert closed == [True]
    assert host.current_route == ""

    # Route switch stops the previous engine without reporting a user close.
    monkeypatch.setattr(player_host, "decide_route", lambda path: "qt")
    import src.shared.playback.player_qt as qt_module
    monkeypatch.setattr(qt_module, "UltimatePlayer", _FakePlayer)
    host.play("/media/b.mov")
    assert player.stopped == 1
    assert host.current_route == "qt"
    assert closed == [True]

    host.stop()
    assert host._players["qt"].stopped == 1
    host.shutdown()
    assert host._players == {}
