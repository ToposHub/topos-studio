"""Customizable Motion Graphic editor shortcuts (ShortcutManager-backed).

The Motion Graphic editor is a modeless ``Qt.Tool`` window, so its shortcut
keys must cooperate with the main window instead of being registered as
window/application-wide QShortcuts that would steal keys across windows
(see ``src/shared/ui/shortcut_scope.py`` for the full story).

Every entry here appears in the global shortcut-settings dialog under the
"Motion Graphic" category and can be rebound by the user.  Consumers read
the *configured* sequence through :func:`configured_shortcut`, which falls
back to the built-in default when the ShortcutManager is absent (tests,
headless runs).

Integration contract:

* QAction-bound entries (save/auto-key/undo/redo) are applied to the
  editor's own QActions — those stay window-scoped to the editor, which is
  correct and steal-free.
* The play/pause entry is matched inside the editor's event filter /
  keyPressEvent and inside the host's event filter routing, never via a
  cross-window QShortcut.
"""

from __future__ import annotations

from typing import Iterable, Tuple

from PyQt6.QtCore import QEvent, Qt
from PyQt6.QtGui import QKeySequence

# (action_id, label, default sequence)
MG_SHORTCUTS: Tuple[Tuple[str, str, str], ...] = (
    ("motion_graphic/play_pause", "Play / Pause", "Space"),
    ("motion_graphic/save_scene", "Save Scene", "Ctrl+S"),
    ("motion_graphic/auto_key", "Toggle Auto Keyframe", "K"),
    ("motion_graphic/undo", "Undo", "Ctrl+Z"),
    ("motion_graphic/redo", "Redo", "Ctrl+Shift+Z"),
)

MG_SHORTCUT_CATEGORY = "Motion Graphic"

PLAY_PAUSE_ACTION_ID = "motion_graphic/play_pause"
PLAY_PAUSE_DEFAULT = "Space"


def register_motion_graphic_shortcuts(shortcut_manager) -> None:  # noqa: ANN001 - duck-typed ShortcutManager
    """(Re-)register every Motion Graphic shortcut definition.

    Called once at app startup (teaching_animation module) and again by
    each editor window; registration is idempotent.  No QAction is bound
    here on purpose — see the module docstring.
    """
    for action_id, label, default in MG_SHORTCUTS:
        shortcut_manager.register(action_id, label, MG_SHORTCUT_CATEGORY, default)


def configured_shortcut(action_id: str, default: str) -> QKeySequence:
    """Return the user-configured sequence for *action_id* or *default*."""
    manager = _manager()
    if manager is not None:
        value = manager.get_shortcut(action_id)
        if value:
            return QKeySequence(value)
    return QKeySequence(default)


def event_matches(event: QEvent, action_id: str, default: str) -> bool:
    """Return True when a key event matches the configured sequence."""
    if event.type() != QEvent.Type.KeyPress:
        return False
    return _sequence_for_event(event) == configured_shortcut(action_id, default)


def motion_graphic_play_key(event: QEvent) -> bool:
    """Whether *event* is the configured Motion Graphic play/pause key."""
    return event_matches(event, PLAY_PAUSE_ACTION_ID, PLAY_PAUSE_DEFAULT)


def _sequence_for_event(event: QEvent) -> QKeySequence:
    # PyQt6 returns a plain int for QKeyEvent.key(); wrap it back so the
    # | operator produces the QKeyCombination QKeySequence compares against.
    return QKeySequence(event.modifiers() | Qt.Key(event.key()))


def _manager():  # noqa: ANN202 - avoids importing the manager for isinstance
    try:
        from src.shared.ui.shortcut_manager import ShortcutManager
    except ImportError:  # pragma: no cover - defensive
        return None
    return ShortcutManager.instance()


def iter_shortcuts() -> Iterable[Tuple[str, str, str]]:
    return iter(MG_SHORTCUTS)
