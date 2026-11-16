"""Real builtin window lifetime regression; no image processing is started."""
from __future__ import annotations

from PyQt6.QtGui import QCloseEvent

from external.file_explorer.plugins.image_factory.image_gui import MainWindow


def test_unused_image_factory_closes_without_treating_qobject_thread_as_worker(qtbot):
    window = MainWindow()
    qtbot.addWidget(window)
    window.show()
    assert callable(window.thread)
    assert window._processor_thread is None
    assert window.close()
    assert not window.isVisible()


def test_running_processor_that_wont_stop_keeps_window_alive(qtbot):
    window = MainWindow()
    qtbot.addWidget(window)
    calls = []
    class Worker:
        def isRunning(self):
            return True
        def cancel(self):
            calls.append("cancel")
        def wait(self, milliseconds):
            calls.append(milliseconds)
            return False
    window._processor_thread = Worker()
    event = QCloseEvent()
    window.closeEvent(event)
    assert not event.isAccepted()
    assert calls == ["cancel", 8000]
    window._processor_thread = None
