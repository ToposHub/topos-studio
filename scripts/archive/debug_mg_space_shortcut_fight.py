"""Does an ApplicationShortcut Space (like the Edit timeline panel's) steal
the key before the MG editor's app-level filter can consume it?
"""

from __future__ import annotations

import os
import sys
from dataclasses import replace

try:
    from PyQt6.QtWebEngineWidgets import QWebEngineView  # noqa: F401
except ImportError:
    pass

from PyQt6.QtCore import Qt, QTimer, QEvent
from PyQt6.QtGui import QShortcut
from PyQt6.QtWidgets import QApplication, QMainWindow

from src.features.teaching_animation.core.html_layer import build_html_layer
from src.features.teaching_animation.core.html_layer_commands import SetHtmlLayerCommand
from src.features.teaching_animation.core.model import TeachingSceneDocument
from src.features.teaching_animation.ui.motion_graphic_editor_window import (
    MotionGraphicEditorWindow,
)


def _scene() -> TeachingSceneDocument:
    base = TeachingSceneDocument.create_default("repro", duration_frames=240)
    html = (
        '<section data-topos-page="1">A</section>'
        '<section data-topos-page="2">B</section>'
    )
    layer = build_html_layer(
        layer_id="repro",
        sanitized_html=html,
        authored_source=html,
        viewport_width=1920,
        viewport_height=1080,
        fps_num=24,
        fps_den=1,
        duration_frames=240,
    )
    return SetHtmlLayerCommand(layer).apply(replace(base, elements=()))


def real_space() -> None:
    os.system("""osascript -e 'tell application "System Events" to keystroke " "'""")


def main() -> int:
    app = QApplication(sys.argv)
    pid = os.getpid()

    host = QMainWindow()
    host.resize(1000, 700)
    host.move(80, 120)
    host.show()

    # Mimic the FIXED src/features/edit/ui/timeline_panel.py registration
    shortcut_fired = {"n": 0}

    def on_shortcut() -> None:
        shortcut_fired["n"] += 1
        print(f"[host-shortcut fired #{shortcut_fired['n']}] activeWindow={QApplication.activeWindow()!r} "
              f"host.isActiveWindow={host.isActiveWindow()} editor.isActiveWindow={editor.isActiveWindow()} "
              f"focusWindow={QApplication.focusWindow()!r} focusWidget={QApplication.focusWidget()!r}", flush=True)

    app_shortcut = QShortcut(Qt.Key.Key_Space, host)
    app_shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
    app_shortcut.activated.connect(on_shortcut)

    editor = MotionGraphicEditorWindow(_scene(), parent=host)
    editor.show()
    editor.raise_()
    editor.activateWindow()

    filter_presses = {"n": 0}
    original_filter = editor.eventFilter

    def probe(watched, event):
        if event.type() == QEvent.Type.KeyPress and event.key() == Qt.Key.Key_Space and not event.isAutoRepeat():
            filter_presses["n"] += 1
            print(f"[mg-filter space#{filter_presses['n']}] context={editor._space_context_active()}", flush=True)
        return original_filter(watched, event)

    editor.eventFilter = probe  # type: ignore[method-assign]

    def t(ms, fn):
        QTimer.singleShot(ms, fn)

    def activate() -> None:
        os.system(
            f"""osascript -e 'tell application "System Events" to set frontmost of the first process whose unix id is {pid} to true'"""
        )

    def phase() -> None:
        print(f"[pre] editor.isActiveWindow={editor.isActiveWindow()} "
              f"activeWindow={QApplication.activeWindow()!r}", flush=True)
        print("\n=== real space with ApplicationShortcut Space registered on host ===", flush=True)
        real_space()
        t(900, report)

    def report() -> None:
        print(f"\n[RESULT] mg_timer={editor.timeline._timer.isActive()} "
              f"keypress_seen_by_filter={filter_presses['n']} "
              f"app_shortcut_fired={shortcut_fired['n']}", flush=True)
        t(100, app.quit)

    t(1200, activate)
    t(2200, phase)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
