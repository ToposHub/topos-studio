"""Repro of the REAL user flow: app already active, main window has key focus,
then the MG editor opens via a click in the main window (the owner keeps
keyboard focus until the user's first click INSIDE the editor).
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
from PyQt6.QtGui import QCursor
from PyQt6.QtWidgets import QApplication, QMainWindow, QPushButton, QVBoxLayout, QWidget

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


CG = None


def real_click(x: int, y: int) -> None:
    global CG
    if CG is None:
        CG = ctypes_mod()
    point = CG.Point(float(x), float(y))
    for kind in (5, 1, 2):  # move, down, up
        CG.lib.CGEventPost(0, CG.lib.CGEventCreateMouseEvent(None, kind, point, 0))


def ctypes_mod():
    import ctypes
    lib = ctypes.CDLL("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
    Point = ctypes.c_double * 2
    lib.CGEventCreateMouseEvent.argtypes = [ctypes.c_void_p, ctypes.c_uint32, Point, ctypes.c_uint32]
    lib.CGEventCreateMouseEvent.restype = ctypes.c_void_p
    lib.CGEventPost.argtypes = [ctypes.c_uint32, ctypes.c_void_p]

    class M:
        pass

    m = M()
    m.lib, m.Point = lib, Point
    return m


def real_space() -> None:
    os.system("""osascript -e 'tell application "System Events" to keystroke " "'""")


def main() -> int:
    app = QApplication(sys.argv)
    pid = os.getpid()

    # Stand-in host with a button that opens the editor, like the real module.
    host = QMainWindow()
    host.resize(1000, 700)
    host.move(80, 120)
    central = QWidget()
    layout = QVBoxLayout(central)
    button = QPushButton("Open MG Editor")
    layout.addWidget(button)
    host.setCentralWidget(central)
    host.show()

    state = {"editor": None}

    def open_editor() -> None:
        state["editor"] = MotionGraphicEditorWindow(_scene(), parent=host)
        state["editor"].show()

    button.clicked.connect(open_editor)

    presses = {"n": 0}

    def t(ms, fn):
        QTimer.singleShot(ms, fn)

    def activate() -> None:
        os.system(
            f"""osascript -e 'tell application "System Events" to set frontmost of the first process whose unix id is {pid} to true'"""
        )

    def click_open_button() -> None:
        # Synthesized clicks don't reach the app in this session; invoke the
        # same slot a button click runs, with the app already active and the
        # main window holding key focus (the real post-click state).
        print("\n=== invoking the open-editor slot (app active, main window key) ===", flush=True)
        open_editor()

    def probe_editor() -> None:
        editor = state["editor"]
        if editor is None:
            print("editor never opened!", flush=True)
            t(100, app.quit)
            return
        original_filter = editor.eventFilter

        def probe(watched, event):
            if event.type() == QEvent.Type.KeyPress and event.key() == Qt.Key.Key_Space and not event.isAutoRepeat():
                presses["n"] += 1
                print(f"[mg-filter space#{presses['n']}] context={editor._space_context_active()}", flush=True)
            return original_filter(watched, event)

        editor.eventFilter = probe  # type: ignore[method-assign]
        t(400, dump_and_space)

    def dump_and_space() -> None:
        editor = state["editor"]
        focus = QApplication.focusWidget()
        print(f"[pre-space] activeWindow={QApplication.activeWindow()!r} "
              f"editor.isActiveWindow={editor.isActiveWindow()} focus={focus!r} "
              f"cursor_in_geom={editor.frameGeometry().contains(QCursor.pos())}", flush=True)
        print("\n=== real space right after opening (no click in editor) ===", flush=True)
        real_space()
        t(900, report)

    def report() -> None:
        editor = state["editor"]
        print(f"[RESULT] mg_timer={editor.timeline._timer.isActive()} presses_seen_by_filter={presses['n']}", flush=True)
        t(100, app.quit)

    # The real-user sequence: app activated FIRST, then user clicks the button.
    t(800, activate)
    t(1800, click_open_button)
    t(2800, probe_editor)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
