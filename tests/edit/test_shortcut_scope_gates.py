"""Repo-wide gates for the cross-window shortcut-scope rules.

Background: QShortcuts consume a key BEFORE any event filter runs, and on
macOS a main-window shortcut (Window/Application context) still fires while
a parented modeless Tool window (the Motion Graphic editor and its top-level
HTML panels) is active.  That combination silently stole Space from the MG
editor and reset Color-page crop state from inside it — see
src/shared/ui/shortcut_scope.py for the full write-up.

These gates keep new registrations from re-introducing the bug:

* QShortcut + ApplicationShortcut is forbidden outright.
* QShortcut + WindowShortcut requires the shared
  ``shortcut_owner_window_active`` guard (or an explicit opt-out comment
  ``shortcut-scope: window-ok <reason>`` when scoping is provably safe).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = PROJECT_ROOT / "src"

APPLICATION_QSHORTCUT_FORBIDDEN = re.compile(
    r"setContext\(\s*Qt\.ShortcutContext\.ApplicationShortcut"
)
WINDOW_QSHORTCUT = re.compile(
    r"setContext\(\s*Qt\.ShortcutContext\.WindowShortcut"
)
WINDOW_OPT_OUT = "shortcut-scope: window-ok"


def _python_sources() -> list[Path]:
    return [
        path
        for path in SRC_ROOT.rglob("*.py")
        if "__pycache__" not in path.parts
    ]


def test_no_qshortcut_uses_application_shortcut_context() -> None:
    """ApplicationShortcut QShortcuts steal keys from every window."""
    offenders: list[str] = []
    for path in _python_sources():
        source = path.read_text(encoding="utf-8", errors="ignore")
        if APPLICATION_QSHORTCUT_FORBIDDEN.search(source):
            offenders.append(str(path.relative_to(PROJECT_ROOT)))
    assert offenders == [], (
        "QShortcut.setContext(ApplicationShortcut) steals keys from modeless "
        "Tool windows before any event filter can route them "
        "(see src/shared/ui/shortcut_scope.py). Use WidgetWithChildrenShortcut, "
        f"or WindowShortcut + shortcut_owner_window_active guard. Offenders: {offenders}"
    )


def test_window_scoped_qshortcuts_use_owner_guard() -> None:
    """WindowShortcut still fires under a parented Tool window on macOS."""
    offenders: list[str] = []
    for path in _python_sources():
        source = path.read_text(encoding="utf-8", errors="ignore")
        if WINDOW_QSHORTCUT.search(source):
            guarded = "shortcut_owner_window_active" in source
            opted_out = WINDOW_OPT_OUT in source
            if not (guarded or opted_out):
                offenders.append(str(path.relative_to(PROJECT_ROOT)))
    assert offenders == [], (
        "WindowShortcut QShortcuts fire even while the Motion Graphic editor "
        "(parented Qt.Tool window) is active on macOS; guard the handler with "
        "shortcut_owner_window_active() from src/shared/ui/shortcut_scope.py, "
        f"or annotate with '{WINDOW_OPT_OUT} <reason>'. Offenders: {offenders}"
    )
