"""ShortcutManager-backed shortcuts for the Scene Cut dialog.

The dialog is a local transport surface.  Definitions are registered with
the shared manager so users can customize them, while key events are matched
inside the active dialog instead of using a window/application-wide
``QShortcut`` that could steal keys from another editor window.
"""

from __future__ import annotations

from typing import Iterable, Tuple

from PyQt6.QtCore import QEvent, Qt
from PyQt6.QtGui import QKeySequence


SCENE_CUT_SHORTCUTS: Tuple[Tuple[str, str, str], ...] = (
    ("scene_cut/accept_current", "Accept Current Cut", "Enter"),
    ("scene_cut/reject_current", "Reject Current Cut", "Delete"),
    ("scene_cut/previous_cut", "Previous Cut", "Up"),
    ("scene_cut/next_cut", "Next Cut", "Down"),
    ("scene_cut/step_previous_frame", "Previous Frame", "Left"),
    ("scene_cut/step_next_frame", "Next Frame", "Right"),
    ("scene_cut/play_pause", "Play / Pause", "Space"),
)

SCENE_CUT_SHORTCUT_CATEGORY = "Scene Cut"

ACCEPT_CURRENT_ACTION_ID = "scene_cut/accept_current"
REJECT_CURRENT_ACTION_ID = "scene_cut/reject_current"
PREVIOUS_CUT_ACTION_ID = "scene_cut/previous_cut"
NEXT_CUT_ACTION_ID = "scene_cut/next_cut"
STEP_PREVIOUS_FRAME_ACTION_ID = "scene_cut/step_previous_frame"
STEP_NEXT_FRAME_ACTION_ID = "scene_cut/step_next_frame"
PLAY_PAUSE_ACTION_ID = "scene_cut/play_pause"


def register_scene_cut_shortcuts(shortcut_manager) -> None:  # noqa: ANN001
    """Register all Scene Cut definitions with the shared manager."""
    for action_id, label, default in SCENE_CUT_SHORTCUTS:
        shortcut_manager.register(
            action_id,
            label,
            SCENE_CUT_SHORTCUT_CATEGORY,
            default,
            feature_id="edit",
        )


def configured_shortcut(action_id: str, default: str) -> QKeySequence:
    """Return the configured shortcut, falling back for headless tests."""
    manager = _manager()
    if manager is not None:
        value = manager.get_shortcut(action_id)
        if value:
            return QKeySequence(value)
    return QKeySequence(default)


def event_matches(event: QEvent, action_id: str, default: str) -> bool:
    """Return whether a key event matches the configured shortcut."""
    if event.type() != QEvent.Type.KeyPress:
        return False
    configured = configured_shortcut(action_id, default)
    if _sequence_for_event(event) == configured:
        return True

    # Preserve the dialog's historical Enter/Return and Delete/Backspace
    # aliases while the action is still using its built-in default.  A user
    # customization remains exact and never gains an unexpected alias.
    if event.modifiers() != Qt.KeyboardModifier.NoModifier:
        return False
    if configured != QKeySequence(default):
        return False
    aliases = {
        "Enter": (Qt.Key.Key_Enter, Qt.Key.Key_Return),
        "Delete": (Qt.Key.Key_Delete, Qt.Key.Key_Backspace),
    }
    return event.key() in aliases.get(default, ())


def _sequence_for_event(event: QEvent) -> QKeySequence:
    # PyQt6 returns a plain int for QKeyEvent.key(); wrap it back so the
    # resulting combination compares correctly with QKeySequence.
    return QKeySequence(event.modifiers() | Qt.Key(event.key()))


def _manager():  # noqa: ANN202 - avoids importing the manager at module load
    try:
        from src.shared.ui.shortcut_manager import ShortcutManager
    except ImportError:  # pragma: no cover - defensive
        return None
    return ShortcutManager.instance()


def iter_shortcuts() -> Iterable[Tuple[str, str, str]]:
    """Iterate over the definitions for diagnostics and tests."""
    return iter(SCENE_CUT_SHORTCUTS)
