"""Repro with the REAL MainWindow as host + real keyboard events.

Constructs the real src/app/main_window.MainWindow (test_mode=True keeps it
from loading a project), opens an MG scene editor exactly like the module
does, then sends real OS Space keystrokes while reporting every decision.
"""

from __future__ import annotations

import os
import sys

try:
    from PyQt6.QtWebEngineWidgets import QWebEngineView  # noqa: F401
except ImportError:
    pass

from PyQt6.QtCore import Qt, QTimer, QEvent
from PyQt6.QtGui import QCursor
from PyQt6.QtWidgets import QApplication

from src.app.main_window import MainWindow
from src.features.teaching_animation.core.html_layer import build_html_layer
from src.features.teaching_animation.core.html_layer_commands import SetHtmlLayerCommand
from src.features.teaching_animation.core.model import TeachingSceneDocument
from src.features.teaching_animation.ui.motion_graphic_editor_window import (
    MotionGraphicEditorWindow,
)
from dataclasses import replace


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

    host = MainWindow(test_mode=True)
    host.show()

    editor = MotionGraphicEditorWindow(_scene(), parent=host)
    editor.show()

    presses = {"n": 0}
    original_filter = editor.eventFilter

    def probe(watched, event):
        if event.type() == QEvent.Type.KeyPress and event.key() == Qt.Key.Key_Space and not event.isAutoRepeat():
            presses["n"] += 1
            focus = QApplication.focusWidget()
            print(f"[mg-filter space#{presses['n']}] context={editor._space_context_active()} "
                  f"activeWindow={QApplication.activeWindow()!r} "
                  f"focusWidget={focus!r} "
                  f"text_focus={editor._is_text_editing_focus(focus)} "
                  f"cursor_in_geom={editor.frameGeometry().contains(QCursor.pos())}",
                  flush=True)
        return original_filter(watched, event)

    editor.eventFilter = probe  # type: ignore[method-assign]

    host_presses = {"n": 0}
    original_host_filter = MainWindow.eventFilter

    def host_probe(self, obj, event):
        if event.type() == QEvent.Type.KeyPress and event.key() == Qt.Key.Key_Space and not event.isAutoRepeat():
            host_presses["n"] += 1
            print(f"[host-filter space#{host_presses['n']}]", flush=True)
        return original_host_filter(self, obj, event)

    MainWindow.eventFilter = host_probe

    def activate() -> None:
        os.system(
            f"""osascript -e 'tell application "System Events" to set frontmost of the first process whose unix id is {pid} to true'"""
        )

    def phase1() -> None:
        print(f"\n[setup] editor visible={editor.isVisible()} html={editor._html_browser_active} "
              f"host_page={getattr(host, 'current_page', None)}", flush=True)
        activate()

    def phase2() -> None:
        print("\n=== real space: fresh editor, no click ===", flush=True)
        real_space()
        QTimer.singleShot(900, report1)

    def report1() -> None:
        print(f"[result 1] mg_timer={editor.timeline._timer.isActive()} "
              f"mg_presses={presses['n']} host_presses={host_presses['n']}", flush=True)
        if editor.timeline._timer.isActive():
            editor.timeline.stop()
        # Click on the MG keyframe tracks area (real Qt widget) then space.
        track = editor.keyframes_panel._tracks
        center = track.mapToGlobal(track.rect().center())
        os.system(
            f"""osascript -e 'tell application "System Events" to click at {{{center.x()}, {center.y()}}}' 2>/dev/null || true"""
        )
        QTimer.singleShot(600, phase3)

    def phase3() -> None:
        print("\n=== real space: after clicking MG timeline area ===", flush=True)
        real_space()
        QTimer.singleShot(900, report2)

    def report2() -> None:
        print(f"[result 2] mg_timer={editor.timeline._timer.isActive()} "
              f"mg_presses={presses['n']} host_presses={host_presses['n']}", flush=True)
        if editor.timeline._timer.isActive():
            editor.timeline.stop()
        QTimer.singleShot(100, app.quit)

    QTimer.singleShot(1500, phase1)
    QTimer.singleShot(2600, phase2)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
