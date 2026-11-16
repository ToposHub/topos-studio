"""A10：窄停靠 / 浮动 / 高 DPI 自动化检查（每个缩放因子独立进程）。"""
from __future__ import annotations

import json
import os
import subprocess
import sys

CHILD = "--child"


def child() -> None:
    from PyQt6.QtWidgets import QApplication, QDockWidget, QMainWindow, QWidget

    from src.app.ui.native_files_panel import NativeFilesPanel

    app = QApplication([])
    host = QMainWindow()
    panel = NativeFilesPanel(host, preferences=None)
    host.setCentralWidget(panel)
    host.resize(760, 560)
    host.show()
    app.processEvents()
    controls = {
        "back": panel.back_button, "up": panel.up_button,
        "type": panel.type_combo, "flag": panel.flag_combo,
        "view": panel.view_combo, "inspector": panel.inspector_button,
    }
    reachable = {name: bool(widget.isVisible() and widget.width() > 0
                            and widget.mapTo(panel, widget.rect().center()).x() >= 0)
                 for name, widget in controls.items()}
    floating = QDockWidget("Files", host)
    floating_panel = NativeFilesPanel(floating, preferences=None)
    floating.setWidget(floating_panel)
    floating.setFloating(True)
    floating.resize(700, 500)
    floating.show()
    app.processEvents()
    print(json.dumps({
        "scale": os.environ.get("QT_SCALE_FACTOR", "1"),
        "narrow": {"sidebar": panel.sidebar.width(), "inspector": panel.inspector.width(),
                   "controls": reachable},
        "floating": {"visible": floating.isVisible(),
                     "size": [floating.width(), floating.height()],
                     "panel_visible": floating_panel.isVisible()},
    }))
    app.quit()


def parent() -> None:
    rows = []
    for scale in ("1", "1.5", "2"):
        env = dict(os.environ, QT_SCALE_FACTOR=scale, TOPOS_PLATFORM_CHILD="1")
        result = subprocess.run([sys.executable, __file__, CHILD], capture_output=True,
                                text=True, env=env, timeout=90)
        for line in result.stdout.splitlines():
            if line.startswith("{"):
                rows.append(json.loads(line))
                break
        else:
            print("scale", scale, "failed:", result.stderr[-400:])
    target = os.path.join(os.path.dirname(__file__), "platform.json")
    with open(target, "w", encoding="utf-8") as fh:
        json.dump(rows, fh, ensure_ascii=False, indent=2)
    print(json.dumps(rows, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    if CHILD in sys.argv:
        child()
    else:
        parent()
