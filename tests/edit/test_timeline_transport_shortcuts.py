"""Edit timeline transport shortcut scoping regressions.

Window-wide J/K/L must stay silent while another top-level window (the
Motion Graphic editor Tool window) owns activation; Space is panel-scoped
(see tests/ui/test_motion_graphic_manual_controls.py for the MG side).
"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6.QtWidgets import QApplication, QWidget

from src.features.edit.ui.timeline_panel import TimelinePanel


class _StubPlaybackController:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def jkl_backward_press(self) -> None:
        self.calls.append("j")

    def jkl_stop(self) -> None:
        self.calls.append("k")

    def jkl_forward_press(self) -> None:
        self.calls.append("l")

    def jkl_reset(self) -> None:
        self.calls.append("reset")


@pytest.fixture(scope="session")
def qt_app() -> QApplication:
    return QApplication.instance() or QApplication([])


@pytest.fixture
def panel(qt_app: QApplication) -> TimelinePanel:
    panel = TimelinePanel()
    panel.show()
    stub = _StubPlaybackController()
    panel._playback_controller = stub  # type: ignore[assignment]
    yield panel
    # J/K/L arm a 500ms reset timer; stop it so it cannot fire inside a
    # later, unrelated test's event loop.
    panel._jkl_timer.stop()
    panel.close()


def test_jkl_silent_behind_other_top_level_window(panel: TimelinePanel, qt_app: QApplication) -> None:
    """JKL must not shuttle this timeline while another window is active."""
    other = QWidget()
    other.show()
    QApplication.setActiveWindow(other)

    panel._on_j_pressed()
    panel._on_k_pressed()
    panel._on_l_pressed()

    assert panel._playback_controller.calls == []


def test_jkl_flow_with_own_window_active(panel: TimelinePanel, qt_app: QApplication) -> None:
    """JKL keep working when this panel's window owns activation."""
    QApplication.setActiveWindow(panel.window())

    panel._on_j_pressed()
    panel._on_k_pressed()
    panel._on_l_pressed()

    assert panel._playback_controller.calls == ["j", "k", "l"]


def test_jkl_silent_while_text_input_focused(panel: TimelinePanel, qt_app: QApplication) -> None:
    """F1.4：字幕查找/重命名等文本框聚焦时，窗口级 JKL 不得触发变速。"""
    from PyQt6.QtWidgets import QLineEdit

    QApplication.setActiveWindow(panel.window())
    edit = QLineEdit(panel)
    edit.show()
    edit.setFocus()
    QApplication.processEvents()
    assert edit.hasFocus()

    panel._on_j_pressed()
    panel._on_k_pressed()
    panel._on_l_pressed()
    assert panel._playback_controller.calls == [], "文本框聚焦时 JKL 必须静默"

    # 焦点移回普通控件后恢复
    panel.setFocus()
    QApplication.processEvents()
    panel._on_l_pressed()
    assert panel._playback_controller.calls == ["l"]


def test_shift_b_edit_mode_silent_while_text_input_focused(
        panel: TimelinePanel, qt_app: QApplication) -> None:
    """F1.4：与 JKL 同域的 Shift+B 同样在文本框聚焦时静默。"""
    from PyQt6.QtWidgets import QLineEdit

    QApplication.setActiveWindow(panel.window())
    edit = QLineEdit(panel)
    edit.show()
    edit.setFocus()
    QApplication.processEvents()

    before = panel.timeline_view.edit_mode()
    panel.toggle_edit_mode()
    assert panel.timeline_view.edit_mode() == before, "文本框聚焦时 Shift+B 不得切模式"

    panel.setFocus()
    QApplication.processEvents()
    panel.toggle_edit_mode()
    assert panel.timeline_view.edit_mode() != before
