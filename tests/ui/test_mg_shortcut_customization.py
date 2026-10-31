"""Motion Graphic editor shortcuts must follow the global settings panel.

Covers the ShortcutManager integration from ui/mg_shortcuts.py: the five
editor shortcuts appear in the settings categories, rebinding them updates
the editor's QActions and the keyframes panel shortcut, and the context
filter accepts the customized play key (and no longer the default).
"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QKeyEvent, QKeySequence
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QWidget

from src.features.teaching_animation.ui.mg_shortcuts import (
    MG_SHORTCUT_CATEGORY,
    PLAY_PAUSE_ACTION_ID,
    configured_shortcut,
    event_matches,
    register_motion_graphic_shortcuts,
)
from src.features.teaching_animation.ui.motion_graphic_editor_window import (
    MotionGraphicEditorWindow,
)
from src.shared.ui.shortcut_manager import ShortcutManager
from tests.ui.test_motion_graphic_manual_controls import _scene


class _DictConfig:
    """Minimal ConfigManager stand-in so tests never touch config.json."""

    def __init__(self) -> None:
        self.values: dict = {}
        self.saved: list[dict] = []

    def get(self, key, default=None):
        return self.values.get(key, default)

    def set(self, key, value) -> None:
        self.values[key] = value

    def save_config(self) -> None:
        self.saved.append(dict(self.values))


@pytest.fixture
def shortcut_manager():
    """Install a fresh ShortcutManager for the test, then restore the old one."""
    previous = ShortcutManager._instance
    manager = ShortcutManager(_DictConfig())
    ShortcutManager._instance = manager
    register_motion_graphic_shortcuts(manager)
    yield manager
    ShortcutManager._instance = previous


def _key_event(key: int, modifiers=Qt.KeyboardModifier.NoModifier) -> QKeyEvent:
    return QKeyEvent(QKeyEvent.Type.KeyPress, key, modifiers)


def test_settings_panel_lists_motion_graphic_category(shortcut_manager) -> None:
    ids = [sa.id for sa in shortcut_manager.get_actions_by_category(MG_SHORTCUT_CATEGORY)]
    assert ids == [
        "motion_graphic/play_pause",
        "motion_graphic/save_scene",
        "motion_graphic/auto_key",
        "motion_graphic/undo",
        "motion_graphic/redo",
    ]


def test_rebinding_updates_editor_actions_and_panel(shortcut_manager, qapp) -> None:
    window = MotionGraphicEditorWindow(_scene())
    try:
        shortcut_manager.set_shortcut("motion_graphic/save_scene", "Ctrl+Alt+S")
        shortcut_manager.set_shortcut("motion_graphic/auto_key", "Shift+K")
        shortcut_manager.set_shortcut("motion_graphic/undo", "Ctrl+U")
        shortcut_manager.set_shortcut("motion_graphic/redo", "Ctrl+R")
        shortcut_manager.set_shortcut(PLAY_PAUSE_ACTION_ID, "P")

        assert window._save_action.shortcut() == QKeySequence("Ctrl+Alt+S")
        assert window._auto_key_action.shortcut() == QKeySequence("Shift+K")
        assert window._undo_action.shortcut() == QKeySequence("Ctrl+U")
        assert window._redo_action.shortcut() == QKeySequence("Ctrl+R")
        assert window.keyframes_panel._space_shortcut.key() == QKeySequence("P")

        # The context filter follows the customized play key, not just Space.
        window.show()
        qapp.processEvents()
        window.timeline.stop()
        target = window.keyframes_panel._tracks
        target.setFocus()
        window.activateWindow()
        qapp.processEvents()

        QTest.keyClick(target, Qt.Key.Key_P)
        qapp.processEvents()
        assert window.timeline._timer.isActive() is True

        QTest.keyClick(target, Qt.Key.Key_P)
        qapp.processEvents()
        assert window.timeline._timer.isActive() is False

        # Default Space no longer matches the configured play key.
        assert event_matches(_key_event(Qt.Key.Key_P), PLAY_PAUSE_ACTION_ID, "Space") is True
        assert event_matches(_key_event(Qt.Key.Key_Space), PLAY_PAUSE_ACTION_ID, "Space") is False
    finally:
        window.close()


def test_defaults_apply_without_customization(shortcut_manager, qapp) -> None:
    window = MotionGraphicEditorWindow(_scene())
    try:
        assert window._save_action.shortcut() == QKeySequence("Ctrl+S")
        assert window._auto_key_action.shortcut() == QKeySequence("K")
        assert window.keyframes_panel._space_shortcut.key() == QKeySequence("Space")
        assert configured_shortcut(PLAY_PAUSE_ACTION_ID, "Space") == QKeySequence("Space")
    finally:
        window.close()


class _StatusStub:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def showMessage(self, text: str, timeout: int = 0) -> None:
        self.messages.append(text)


class _SaveRouterStub:
    def __init__(self) -> None:
        self.save_calls = 0

    def save(self) -> bool:
        self.save_calls += 1
        return False

    def get_save_target(self):
        return None


def _bare_host():
    """MainWindow without running its heavy __init__."""
    from src.app.main_window import MainWindow

    host = MainWindow.__new__(MainWindow)
    host.status_bar = _StatusStub()
    host.save_router = _SaveRouterStub()
    return host


def test_project_save_routes_to_active_mg_editor(qapp) -> None:
    """Cmd+S with the MG editor active must save the scene, not the project."""
    window = MotionGraphicEditorWindow(_scene())
    try:
        window.show()
        window.activateWindow()
        qapp.processEvents()

        saved: list[bool] = []
        window.save = lambda: saved.append(True)  # type: ignore[method-assign]
        QApplication.setActiveWindow(window)
        host = _bare_host()
        host.save_file()
        assert saved == [True]
        assert host.save_router.save_calls == 0
    finally:
        window.close()


def test_visible_but_inactive_editor_does_not_capture_save(qapp) -> None:
    """A visible-but-background MG editor must not capture Cmd+S routing."""
    from src.shared.ui.shortcut_scope import active_motion_graphic_editor

    window = MotionGraphicEditorWindow(_scene())
    plain = QWidget()
    try:
        window.show()
        plain.show()
        qapp.processEvents()
        QApplication.setActiveWindow(plain)
        assert window.isVisible() is True
        assert active_motion_graphic_editor() is None
    finally:
        window.editor_controller.mark_clean()
        window.close()
        plain.close()


def test_project_close_routes_to_active_mg_editor(qapp) -> None:
    """Cmd+W with the MG editor active closes the editor, not the project."""
    window = MotionGraphicEditorWindow(_scene())
    try:
        window.show()
        window.activateWindow()
        qapp.processEvents()

        closed: list[bool] = []
        real_close = window.close
        window.close = lambda: closed.append(True)  # type: ignore[method-assign]
        QApplication.setActiveWindow(window)
        host = _bare_host()
        assert host.close_project() is True
        assert closed == [True]
        window.close = real_close  # type: ignore[method-assign]
    finally:
        window.editor_controller.mark_clean()
        window.close()
