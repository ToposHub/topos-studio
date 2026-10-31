"""Crop-panel shortcut scoping regressions (the C/R/Enter/Esc steal family).

The crop workflow keys used to be ApplicationShortcut QShortcuts: they fired
(and consumed keys) even while the Motion Graphic editor Tool window was
active, resetting Color-page state from inside the editor and killing its
Esc handling.  C/R/Shift+R are now WindowShortcut + guard; Enter/Esc are
matched in an application event filter owned only while this panel's window
is active and a crop session is running.  See src/shared/ui/shortcut_scope.py.
"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QKeyEvent
from PyQt6.QtWidgets import QApplication, QWidget

from src.features.color.ui.crop_panel import CropPanel


def _enter() -> QKeyEvent:
    return QKeyEvent(QKeyEvent.Type.KeyPress, Qt.Key.Key_Return, Qt.KeyboardModifier.NoModifier)


@pytest.fixture
def panel(qapp: QApplication) -> CropPanel:
    panel = CropPanel()
    panel.show()
    qapp.processEvents()
    yield panel
    QApplication.instance().removeEventFilter(panel)
    panel.close()


def test_enter_ignored_without_crop_session(panel: CropPanel, qapp: QApplication) -> None:
    """Without an active crop the filter must not consume Enter."""
    applied: list[bool] = []
    panel._on_apply_clicked = lambda: applied.append(True)  # type: ignore[method-assign]
    QApplication.sendEvent(panel, _enter())
    qapp.processEvents()
    assert applied == []


def test_enter_ignored_behind_other_top_level_window(panel: CropPanel, qapp: QApplication) -> None:
    """A modeless Tool window (MG editor stand-in) owning activation keeps Enter."""
    panel.crop_active = True
    applied: list[bool] = []
    panel._on_apply_clicked = lambda: applied.append(True)  # type: ignore[method-assign]
    other = QWidget()
    other.show()
    qapp.processEvents()
    QApplication.setActiveWindow(other)

    QApplication.sendEvent(panel, _enter())
    qapp.processEvents()
    assert applied == []
    assert panel.crop_active is True


def test_enter_applies_crop_when_window_owns_activation(panel: CropPanel, qapp: QApplication) -> None:
    """Enter still applies the crop from anywhere in this panel's window."""
    panel.crop_active = True
    applied: list[bool] = []
    original = panel._on_apply_clicked

    def recorder() -> None:
        applied.append(True)
        original()

    panel._on_apply_clicked = recorder  # type: ignore[method-assign]
    qapp.processEvents()
    QApplication.setActiveWindow(panel.window())

    QApplication.sendEvent(panel, _enter())
    qapp.processEvents()
    assert applied == [True]
    assert panel.crop_active is False


def test_toggle_and_reset_silent_behind_other_window(panel: CropPanel, qapp: QApplication) -> None:
    """C/R handlers must not mutate state while another window is active."""
    other = QWidget()
    other.show()
    qapp.processEvents()
    QApplication.setActiveWindow(other)

    before = (panel.aspect_combo.currentIndex(), panel._rotation_angle)
    panel._toggle_crop()
    panel._on_reset_rotation()
    assert (panel.aspect_combo.currentIndex(), panel._rotation_angle) == before
    assert panel.crop_active is False
