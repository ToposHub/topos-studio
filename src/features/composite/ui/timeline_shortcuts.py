"""AE-style keyboard shortcut registry + focus-aware dispatcher (P4, T37-T48).

Centralises the AE shortcut vocabulary so the Keyframes panel, Spline panel
and future Graph Editor share one mapping, and so the keyboard behaviour is
unit-testable without a real QWidget focus chain.

Solves the three issues flagged in the P0 baseline (§6.4 / §6.5):

1. **The ``K`` conflict.** Plain ``K`` historically adds a keyframe; AE
   users expect ``K`` to navigate to the next keyframe and ``Ctrl/Cmd+K``
   to add one. The registry exposes both bindings and a single
   :meth:`ShortcutDispatcher.handle_key` that resolves which command fires
   for a given key press, so the policy lives in one tested place.
2. **Implicit focus filtering.** Today the panel relies on Qt's default
   event propagation to avoid firing shortcuts while a QLineEdit/QSpinBox
   has focus. :meth:`ShortcutDispatcher.is_text_input_focused` makes that
   explicit so a focused search box never triggers a timeline command.
3. **Platform modifier normalisation.** ``Ctrl`` on Win/Linux ↔ ``Cmd``
   on macOS. The registry accepts both and exposes a single canonical
   command string, so handlers don't branch on platform.

Design rules:

* **GUI-free core.** :class:`ShortcutRegistry` and
  :class:`ShortcutDispatcher` import nothing from PyQt at module load; the
  dispatcher calls :func:`_text_input_focused` lazily so headless tests
  that don't touch Qt can exercise the whole key→command mapping.
* **Commands, not handlers.** The registry maps key sequences to command
  *names* (``"nav.prev_keyframe"``, ``"edit.add_keyframe"``, …); the host
  binds those names to methods. This keeps the registry testable and lets
  AI/script enumerate available commands (T062 capability manifest).
* **Sequence strings are stable.** ``"K"``, ``"J"``, ``"Ctrl+K"``,
  ``"Cmd+K"``, ``"Shift+U"`` … — callers can persist them as preferences
  (plan §12 / T06 "AE-habits toggle").
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Set

_logger = logging.getLogger(__name__)


# ── canonical command names ─────────────────────────────────────────────
# One string per high-level timeline action. Hosts bind handlers to these.
# Never rename a command — add a new one and deprecate the old.
CMD_NAV_PREV_KEYFRAME = "nav.prev_keyframe"
CMD_NAV_NEXT_KEYFRAME = "nav.next_keyframe"
CMD_NAV_FIRST_FRAME = "nav.first_frame"
CMD_NAV_LAST_FRAME = "nav.last_frame"
CMD_NAV_STEP_BACK = "nav.step_back"
CMD_NAV_STEP_FWD = "nav.step_forward"
CMD_NAV_STEP_BACK_BIG = "nav.step_back_big"
CMD_NAV_STEP_FWD_BIG = "nav.step_forward_big"
CMD_PLAY_TOGGLE = "transport.play_toggle"
CMD_ADD_KEYFRAME = "edit.add_keyframe"
CMD_REMOVE_KEYFRAME = "edit.remove_keyframe"
CMD_SET_WORK_IN = "edit.set_work_in"
CMD_SET_WORK_OUT = "edit.set_work_out"
CMD_FOCUS_POSITION = "focus.position"
CMD_FOCUS_SCALE = "focus.scale"
CMD_FOCUS_ROTATION = "focus.rotation"
CMD_FOCUS_ANCHOR = "focus.anchor"
CMD_FOCUS_OPACITY = "focus.opacity"
CMD_FILTER_ANIMATED = "filter.animated"
CMD_FILTER_MODIFIED = "filter.modified"
CMD_FILTER_EXPRESSION = "filter.expression"
CMD_FILTER_ALL = "filter.all"
CMD_SELECT_ALL = "select.all"
CMD_COPY = "edit.copy"
CMD_PASTE = "edit.paste"
CMD_DELETE = "edit.delete"
CMD_UNDO = "edit.undo"
CMD_REDO = "edit.redo"
CMD_FIT_SELECTION = "view.fit_selection"
CMD_FIT_ALL = "view.fit_all"
CMD_ZOOM_IN = "view.zoom_in"
CMD_ZOOM_OUT = "view.zoom_out"
CMD_CENTER_PLAYHEAD = "view.center_playhead"
CMD_TOGGLE_SNAP = "view.toggle_snap"
CMD_EDIT_VALUE = "edit.value"
CMD_CANCEL = "edit.cancel"
CMD_TOGGLE_GRAPH_EDITOR = "view.toggle_graph_editor"


# AE mode default bindings (plan §4.2). ``Cmd`` is normalised to ``Ctrl``
# on Win/Linux by the host; the registry accepts both spellings.
DEFAULT_AE_BINDINGS: Dict[str, str] = {
    "J": CMD_NAV_PREV_KEYFRAME,
    "K": CMD_NAV_NEXT_KEYFRAME,
    "Ctrl+K": CMD_ADD_KEYFRAME,
    "Cmd+K": CMD_ADD_KEYFRAME,  # macOS spelling
    "Home": CMD_NAV_FIRST_FRAME,
    "End": CMD_NAV_LAST_FRAME,
    "Left": CMD_NAV_STEP_BACK,
    "Right": CMD_NAV_STEP_FWD,
    "Shift+Left": CMD_NAV_STEP_BACK_BIG,
    "Shift+Right": CMD_NAV_STEP_FWD_BIG,
    "Space": CMD_PLAY_TOGGLE,
    "I": CMD_SET_WORK_IN,
    "O": CMD_SET_WORK_OUT,
    "B": CMD_SET_WORK_IN,
    "N": CMD_SET_WORK_OUT,
    "P": CMD_FOCUS_POSITION,
    "S": CMD_FOCUS_SCALE,
    "R": CMD_FOCUS_ROTATION,
    "A": CMD_FOCUS_ANCHOR,
    "T": CMD_FOCUS_OPACITY,
    "U": CMD_FILTER_ANIMATED,
    "Shift+U": CMD_FILTER_MODIFIED,  # 'UU' in AE is a double-tap; we model it as Shift+U
    "E": CMD_FILTER_EXPRESSION,  # 'EE' double-tap modelled as plain E
    "Ctrl+A": CMD_SELECT_ALL,
    "Cmd+A": CMD_SELECT_ALL,
    "Ctrl+C": CMD_COPY,
    "Cmd+C": CMD_COPY,
    "Ctrl+V": CMD_PASTE,
    "Cmd+V": CMD_PASTE,
    "Delete": CMD_DELETE,
    "Backspace": CMD_DELETE,
    "Ctrl+Z": CMD_UNDO,
    "Cmd+Z": CMD_UNDO,
    "Ctrl+Y": CMD_REDO,
    "Cmd+Y": CMD_REDO,
    "F": CMD_FIT_SELECTION,
    "+": CMD_ZOOM_IN,
    "=": CMD_ZOOM_IN,  # some keyboards need Shift for '+'
    "-": CMD_ZOOM_OUT,
    "Enter": CMD_EDIT_VALUE,
    "Esc": CMD_CANCEL,
    "Shift+F3": CMD_TOGGLE_GRAPH_EDITOR,
}


@dataclass
class ShortcutRegistry:
    """Maps key-sequence strings → command names, with AE-vs-legacy modes.

    A host owns one instance and binds handlers via
    :meth:`ShortcutDispatcher.bind`. The registry is plain data so it can
    be serialised into the user's "AE-habits" preference (T06) and queried
    by AI/script (T062).
    """

    bindings: Dict[str, str] = field(default_factory=lambda: dict(DEFAULT_AE_BINDINGS))

    def command_for(self, sequence: str) -> Optional[str]:
        # AUD-11108: honour the documented Cmd↔Ctrl alias fold at lookup —
        # an exact (whitespace-normalised) match wins so independently bound
        # spellings stay distinguishable; otherwise fall back to the
        # alias-swapped key.
        key = _normalise_sequence(sequence)
        if key in self.bindings:
            return self.bindings[key]
        return self.bindings.get(_swap_cmd_ctrl(key))

    def set_binding(self, sequence: str, command: str) -> None:
        self.bindings[_normalise_sequence(sequence)] = command

    def clear_binding(self, sequence: str) -> None:
        self.bindings.pop(_normalise_sequence(sequence), None)

    def sequences_for_command(self, command: str) -> List[str]:
        return sorted(seq for seq, cmd in self.bindings.items() if cmd == command)

    def disable_ae_nav_conflicts(self) -> None:
        """Toggle off the AE navigation habits (T06 "AE-habits" switch).

        Restores the *legacy* behaviour where plain ``K`` adds a keyframe
        (the pre-AE-mode binding the existing 216 UI tests were written
        against). Users who don't want AE navigation can flip this so the
        keys match their muscle memory.
        """
        # Plain K → add keyframe (legacy), J unmapped, Cmd+K still adds.
        self.bindings["K"] = CMD_ADD_KEYFRAME
        self.bindings.pop("J", None)
        # Keep Ctrl/Cmd+K as a secondary add binding so the AE path is still
        # reachable even in legacy mode.

    def known_commands(self) -> Set[str]:
        return set(self.bindings.values())


# ── sequence normalisation ──────────────────────────────────────────────


def _normalise_sequence(seq: str) -> str:
    """Canonicalise a key sequence string for lookup.

    Strips whitespace, collapses ``Cmd`` ↔ ``Ctrl`` *aliases* only at
    lookup time (both spellings remain valid keys in the registry so a
    macOS user can bind Cmd+K independently from a Win/Linux Ctrl+K).
    """
    if not isinstance(seq, str):
        return ""
    s = seq.strip()
    # Collapse internal whitespace around '+' so "Ctrl + K" == "Ctrl+K".
    parts = [p.strip() for p in s.split("+")]
    return "+".join(parts)


def _swap_cmd_ctrl(seq: str) -> str:
    """Return ``seq`` with every ``Cmd`` token swapped to ``Ctrl`` and back.

    Lookup-time helper implementing the ``Cmd`` ↔ ``Ctrl`` alias fold
    documented on :func:`_normalise_sequence` (AUD-11108). Non-modifier
    tokens pass through unchanged; the registry's own keys are untouched so
    both spellings stay independently bindable.
    """
    swapped = []
    for part in seq.split("+"):
        if part == "Cmd":
            swapped.append("Ctrl")
        elif part == "Ctrl":
            swapped.append("Cmd")
        else:
            swapped.append(part)
    return "+".join(swapped)


# ── dispatcher ──────────────────────────────────────────────────────────


@dataclass
class ShortcutDispatcher:
    """Resolves a key press into a command, with focus + repeat handling.

    The host wires its ``keyPressEvent`` (or a ``QShortcut``) to
    :meth:`handle_key`; the dispatcher returns the command name to execute
    (or ``None`` if the key is unmapped or suppressed because a text input
    has focus). Handlers are bound via :meth:`bind`.

    Why a dispatcher instead of one ``QShortcut`` per binding:

    * double-tap sequences (``UU``, ``EE``) need a small timer state that
      a stateless QShortcut can't model — handled in :meth:`handle_key`;
    * the focus-context filter is one place, not copy-pasted into every
      panel's ``keyPressEvent``;
    * AI/script can enumerate the *exact* commands a key would fire without
      synthesising Qt events (T062 capability manifest).
    """

    registry: ShortcutRegistry = field(default_factory=ShortcutRegistry)
    handlers: Dict[str, Callable[[], None]] = field(default_factory=dict)
    # Double-tap detection: last key + timestamp. None disables it.
    _last_key: Optional[str] = None
    _last_key_command: Optional[str] = None
    # Window for a double-tap, in milliseconds.
    double_tap_ms: int = 350
    _last_key_ms: float = 0.0
    # When True, single-letter shortcuts are suppressed while a text input
    # has focus (plan §4.2 / P0 §6.5). Defaults True.
    text_input_guard: bool = True
    # Focus-query hook (lazy PyQt import kept out of the dataclass). Tests
    # override this to simulate focus; production uses the default
    # QApplication.focusWidget() check.
    focus_query: Optional[Callable[[], Any]] = None

    # ── binding ──
    def bind(self, command: str, handler: Callable[[], None]) -> None:
        self.handlers[command] = handler

    def unbind(self, command: str) -> None:
        self.handlers.pop(command, None)

    # ── focus ──
    def is_text_input_focused(self) -> bool:
        """True when a text-editing widget has keyboard focus.

        Production implementation checks QApplication.focusWidget() against
        the QLineEdit/QSpinBox/QPlainTextEdit/QTextEdit types. Headless
        tests can override ``focus_query`` to return a stand-in object.
        """
        if not self.text_input_guard:
            return False
        try:
            focus = (self.focus_query() if self.focus_query is not None
                     else _default_focus_widget())
        except Exception:
            return False
        if focus is None:
            return False
        return _is_text_input(focus)

    # ── main entry ──
    def handle_key(self, sequence: str, *, now_ms: Optional[float] = None,
                   modifiers_only: bool = False) -> Optional[str]:
        """Resolve ``sequence`` to a command and fire its handler.

        Returns the command name that fired (or ``None`` if suppressed /
        unmapped). ``now_ms`` is the current time for double-tap detection;
        callers pass ``QDateTime.currentMSecsSinceEpoch()``-style values,
        tests pass deterministic ints.

        Suppression rules (in order):

        1. Text-input guard: if a text input has focus AND the command is a
           single-letter navigation/filter shortcut, suppress it so the
           typed character reaches the input. Modifier-bearing sequences
           (``Ctrl+K``, ``Cmd+V`` …) still fire — they're not characters a
           user types into a search box.
        2. Double-tap: ``U`` then ``U`` again within ``double_tap_ms`` maps
           to ``CMD_FILTER_MODIFIED`` (AE ``UU``); same for ``E``/``EE``.
           A lone ``U`` maps to ``CMD_FILTER_ANIMATED`` (the single-U
           behaviour); the dispatcher upgrades it to MODIFIED on the second
           tap, *without* firing ANIMATED twice.
        """
        seq = _normalise_sequence(sequence)
        command = self.registry.command_for(seq)
        if command is None:
            # An unmatched letter may still be the second half of a
            # double-tap; fall through to double-tap detection.
            command = self._check_double_tap(seq, now_ms, matched=False)
            if command is None:
                self._last_key = None
                return None
            # double-tap produced a command — fire it below.
        else:
            command = self._check_double_tap(seq, now_ms, matched=True,
                                             command=command) or command

        # Rule 1: focus guard for character-like shortcuts.
        if (self.is_text_input_focused() and _is_char_like(seq)
                and command in _TEXT_SUPPRESSED_COMMANDS):
            self._last_key = None
            return None

        handler = self.handlers.get(command)
        if handler is not None:
            try:
                handler()
            except Exception:
                # A handler error must not poison the dispatcher (the key
                # dispatch keeps going), but it must never be silent — a
                # domain error swallowed here leaves a dead key with no
                # trace to debug (AUD-11106). Log with traceback.
                _logger.exception(
                    "Shortcut handler for command %r raised", command)
        return command

    # ── double-tap ──
    def _check_double_tap(self, seq: str, now_ms: Optional[float], *,
                          matched: bool,
                          command: Optional[str] = None) -> Optional[str]:
        """Return the *effective* command after considering a double-tap.

        ``UU`` and ``EE`` are the only AE double-taps we model. A first
        tap records state; a second tap within the window upgrades the
        command. Returns ``None`` to mean "no double-tap effect, use the
        matched command as-is".
        """
        if seq not in {"U", "E"}:
            # Reset streak for non-U/E keys so U…K…U doesn't false-positive.
            if matched:
                self._last_key = seq
                self._last_key_command = command
                self._last_key_ms = now_ms if now_ms is not None else 0.0
            return None
        now = now_ms if now_ms is not None else 0.0
        if (self._last_key == seq and self._last_key_command is not None
                and 0.0 <= now - self._last_key_ms <= self.double_tap_ms):
            # Second tap within window → upgrade.
            upgraded = {
                "U": (CMD_FILTER_ANIMATED, CMD_FILTER_MODIFIED),
                "E": (CMD_FILTER_EXPRESSION, CMD_FILTER_EXPRESSION),
            }[seq][1]
            self._last_key = None
            self._last_key_command = None
            return upgraded
        # First tap (or stale streak) — record and let caller use the
        # single-tap command.
        self._last_key = seq
        self._last_key_command = command if matched else None
        self._last_key_ms = now
        return None


# ── text-input classification ───────────────────────────────────────────


# Commands that should NOT fire while typing in a QLineEdit/QSpinBox, since
# they'd swallow characters the user is trying to type. Modifier-bearing
# commands (copy/paste/undo/select-all) are intentionally NOT in this set —
# they don't consume ordinary characters.
_TEXT_SUPPRESSED_COMMANDS: Set[str] = {
    CMD_NAV_PREV_KEYFRAME, CMD_NAV_NEXT_KEYFRAME,
    CMD_NAV_FIRST_FRAME, CMD_NAV_LAST_FRAME,
    CMD_NAV_STEP_BACK, CMD_NAV_STEP_FWD,
    CMD_PLAY_TOGGLE,
    CMD_ADD_KEYFRAME, CMD_REMOVE_KEYFRAME,
    CMD_FOCUS_POSITION, CMD_FOCUS_SCALE, CMD_FOCUS_ROTATION,
    CMD_FOCUS_ANCHOR, CMD_FOCUS_OPACITY,
    CMD_FILTER_ANIMATED, CMD_FILTER_MODIFIED, CMD_FILTER_EXPRESSION,
    CMD_FIT_SELECTION, CMD_FIT_ALL, CMD_ZOOM_IN, CMD_ZOOM_OUT,
    CMD_CENTER_PLAYHEAD, CMD_TOGGLE_SNAP,
}


def _is_char_like(seq: str) -> bool:
    """True for single-letter sequences (``K``, ``J``, ``P`` …).

    These are the ones a user might type into a search box; the focus guard
    suppresses them while a text input has focus. ``Ctrl+K`` / ``Space`` /
    ``Delete`` are NOT char-like in this sense (Space/Delete are handled by
    the input itself; Ctrl+K carries a modifier).
    """
    return len(seq) == 1 and seq.isalpha()


def _is_text_input(widget: Any) -> bool:
    """True if ``widget`` is a text-editing control.

    PyQt is imported lazily so this module loads headlessly. We check two
    things:

    1. An opt-in ``is_text_input`` attribute (True) — lets custom widgets
       and test stand-ins declare themselves without inheriting from Qt.
    2. The widget's class MRO against known Qt text-input class names
       (covers the real QLineEdit/QSpinBox/QComboBox/QPlainTextEdit/QTextEdit
       and their subclasses).
    """
    if widget is None:
        return False
    # 1. Opt-in attribute (testable without Qt).
    try:
        if bool(getattr(widget, "is_text_input", False)):
            return True
    except Exception:
        pass
    # 2. MRO name match.
    try:
        mro = type(widget).__mro__
    except Exception:
        return False
    names = {cls.__name__ for cls in mro}
    return bool(names & {
        "QLineEdit", "QSpinBox", "QDoubleSpinBox", "QComboBox",
        "QPlainTextEdit", "QTextEdit", "QTextBrowser",
    })


def _default_focus_widget() -> Any:
    """Return QApplication.focusWidget(), or None if Qt is unavailable."""
    try:
        from PyQt6.QtWidgets import QApplication
        return QApplication.focusWidget()
    except Exception:
        return None


__all__ = [
    # commands
    "CMD_NAV_PREV_KEYFRAME", "CMD_NAV_NEXT_KEYFRAME",
    "CMD_NAV_FIRST_FRAME", "CMD_NAV_LAST_FRAME",
    "CMD_NAV_STEP_BACK", "CMD_NAV_STEP_FWD",
    "CMD_NAV_STEP_BACK_BIG", "CMD_NAV_STEP_FWD_BIG",
    "CMD_PLAY_TOGGLE", "CMD_ADD_KEYFRAME", "CMD_REMOVE_KEYFRAME",
    "CMD_SET_WORK_IN", "CMD_SET_WORK_OUT",
    "CMD_FOCUS_POSITION", "CMD_FOCUS_SCALE", "CMD_FOCUS_ROTATION",
    "CMD_FOCUS_ANCHOR", "CMD_FOCUS_OPACITY",
    "CMD_FILTER_ANIMATED", "CMD_FILTER_MODIFIED", "CMD_FILTER_EXPRESSION",
    "CMD_FILTER_ALL", "CMD_SELECT_ALL", "CMD_COPY", "CMD_PASTE",
    "CMD_DELETE", "CMD_UNDO", "CMD_REDO", "CMD_FIT_SELECTION",
    "CMD_FIT_ALL", "CMD_ZOOM_IN", "CMD_ZOOM_OUT", "CMD_CENTER_PLAYHEAD",
    "CMD_TOGGLE_SNAP", "CMD_EDIT_VALUE", "CMD_CANCEL",
    "CMD_TOGGLE_GRAPH_EDITOR",
    # core
    "DEFAULT_AE_BINDINGS", "ShortcutRegistry", "ShortcutDispatcher",
]
