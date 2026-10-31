"""Cross-window shortcut scoping helpers.

════════════════════════════════════════════════════════════════════════
⚠ THE TRAP THIS MODULE EXISTS FOR — read before registering any shortcut
════════════════════════════════════════════════════════════════════════

Two Qt/macOS facts combine into a bug class that is invisible in unit
tests and maddening in the field (see the Motion Graphic Space-playback
incident, fixed 2026-08):

1. **QShortcut consumes a key BEFORE any event filter runs.**  The Qt
   shortcut system dispatches during key delivery, ahead of every
   QApplication event filter and every keyPressEvent.  Code that "just
   adds one more fallback in the filter chain" cannot recover a key that
   a QShortcut already swallowed.

2. **A Tool window parented to the project window does NOT isolate
   shortcut scope on macOS.**  A `Qt.WindowShortcut` registered on the
   main window still fires while a modeless `Qt.Tool` child window (the
   Motion Graphic editor) is the active window — verified empirically
   with `host.isActiveWindow() == False` while its shortcut fired.
   `Qt.ApplicationShortcut` never isolates, by definition.

The Motion Graphic editor, its HTML source editor and its DevTools panel
are all modeless top-level windows.  Any window-wide or application-wide
shortcut therefore fires *from under them*, and its handler — written for
the main window — either no-ops silently (Space died this way) or mutates
main-window state invisibly (crop C/R reset the Color page from inside
the editor this way).

Rules for new shortcuts (enforced in part by
tests/edit/test_shortcut_scope_gates.py):

* Panel-transport / tool keys (Space, single letters, JKL…):
  ``Qt.WidgetWithChildrenShortcut``.  They can never leave their panel.
* Keys that need window-wide ergonomics: ``Qt.WindowShortcut`` PLUS a
  ``shortcut_owner_window_active()`` guard in the handler.  The guard is
  what actually stops the Tool-window steal; the context alone does not.
  A window shortcut that is provably conflict-free may instead carry an
  adjacent comment ``shortcut-scope: window-ok <reason>`` — the gate test
  accepts that explicit opt-out.
* Keys that must cooperate with the context-aware event-filter chain
  (Esc-style UI-flow keys): do not use QShortcut at all — match them in
  ``keyPressEvent`` / an event filter, like the MG editor does.
* ``Qt.ApplicationShortcut`` only for Cmd-level app commands, and accept
  that they target the main window; route to the active modeless editor
  inside the handler when one owns activation (see
  ``active_motion_graphic_editor``).
"""

from __future__ import annotations

from typing import Optional

from PyQt6.QtWidgets import QApplication, QWidget


def shortcut_owner_window_active(widget: QWidget) -> bool:
    """Return True unless another top-level window owns activation.

    Window-wide QShortcuts on the main window still fire while a parented
    Qt.Tool window (Motion Graphic editor) is active on macOS, so the
    *handler* must bail out with this check; see the module docstring.
    ``active is None`` (native-child focus, app switching) passes so keys
    keep working in those transient states, matching pre-guard behaviour.
    """
    active = QApplication.activeWindow()
    return active is None or active is widget.window()


def active_motion_graphic_editor() -> Optional[QWidget]:
    """Return the Motion Graphic editor that currently owns activation.

    Used by main-window handlers of application-level commands (Cmd+S /
    Cmd+W) so they can hand the command to the focused editor instead of
    silently acting on the project underneath it.
    """
    for candidate in QApplication.topLevelWidgets():
        if candidate.objectName() != "MotionGraphicEditor" or not candidate.isVisible():
            continue
        if QApplication.activeWindow() is candidate or candidate.isActiveWindow():
            return candidate
    return None


def text_input_focused() -> bool:
    """Return True if the current focus widget is a text-input control.

    F1.4：窗口级字符快捷键（Edit JKL/Shift+B 等）的 handler 必须在文本
    输入框聚焦时静默——与 main_window Space/方向键 handler 及 Composite
    dispatcher 的既有屏蔽同类（main_window.py 四处 isinstance 判断的共享
    版本；Ctrl 组合是否放行由调用方决定，本函数只报告焦点类型）。
    """
    from PyQt6.QtWidgets import (
        QAbstractSpinBox,
        QComboBox,
        QLineEdit,
        QPlainTextEdit,
        QTextEdit,
    )

    focus = QApplication.focusWidget()
    if focus is None:
        return False
    if isinstance(focus, (QLineEdit, QTextEdit, QPlainTextEdit, QAbstractSpinBox)):
        return True
    return isinstance(focus, QComboBox) and focus.isEditable()
