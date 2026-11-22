from PyQt6.QtCore import QObject, QTimer, pyqtSignal
from pytestqt.qtbot import QtBot

from src.app.controllers.grade_set_previews import GradeSetPreviews


class _Engine(QObject):
    frame_ready = pyqtSignal(int)
    grade_set_preview_ready = pyqtSignal(object, object)
    preview_requested = pyqtSignal()
    grade_set_preview_key = (1, 3)

    def request_grade_set_preview(self) -> bool:
        self.preview_requested.emit()
        return True


class _PreviewHost(QObject):
    def __init__(self) -> None:
        super().__init__()
        self.engine = _Engine(self)
        self.current_page = "Color"
        self.grade_set_panel = self

    def _active_grade_set_image(self) -> str:
        return "/tmp/test-photo.jpg"

    def isVisible(self) -> bool:
        return True


def test_unchanged_viewport_frames_do_not_postpone_the_idle_preview(qtbot: QtBot) -> None:
    host = _PreviewHost()
    previews = GradeSetPreviews(host)
    previews._idle.setInterval(40)
    viewport_refresh = QTimer(host, interval=2)
    viewport_refresh.timeout.connect(lambda: host.engine.frame_ready.emit(1))
    try:
        with qtbot.waitSignal(host.engine.preview_requested, timeout=1000):
            host.engine.frame_ready.emit(1)
            viewport_refresh.start()
    finally:
        viewport_refresh.stop()
        previews.shutdown()
