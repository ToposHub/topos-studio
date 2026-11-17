#!/usr/bin/env python3
"""Preview the real Mixer widgets and native docks without loading audio/GPU.

This captures a lightweight shell using the production dock-layout functions;
the normal Topos Studio launcher exposes the same widgets through the Mixer tab.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication, QDockWidget, QMainWindow, QWidget

from src.app.controllers.dock_layout_controller import DockLayoutMixin
from src.features.mixer.ui.mixer_page import MixerPage
from src.shared.ui.style_sheet import DARK_THEME


class MixerPreviewWindow(QMainWindow, DockLayoutMixin):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Topos Studio · Mixer UI Preview")
        self.setStyleSheet(DARK_THEME)
        self.setDockOptions(QMainWindow.DockOption.AllowNestedDocks | QMainWindow.DockOption.AllowTabbedDocks)
        self.resize(1480, 960)
        self._mixer_available = True
        self.current_page = "Mixer"
        self.page_layouts: dict[str, object] = {}
        self.mixer_page = MixerPage(self)
        self.all_docks: list[QDockWidget] = []
        self._create_audio_workspace_docks(self._factory)
        self.all_docks = list(self._audio_workspace_docks())
        self._seed_audio_workspace_layout()
        self.restoreState(self.page_layouts["Mixer"])
        self.mixer_page.on_enter()

    def _factory(self, title: str, widget: QWidget, name: str) -> QDockWidget:
        dock = QDockWidget(title, self)
        dock.setObjectName(name)
        dock.setWidget(widget)
        return dock


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--size", default="1480x960")
    args = parser.parse_args()
    app = QApplication(sys.argv)
    window = MixerPreviewWindow()
    width, height = (int(value) for value in args.size.lower().split("x"))
    window.resize(width, height)
    window.show()
    app.processEvents()
    window.mixer_page.timeline_panel.fit_all()
    app.processEvents()
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        saved = window.grab().save(str(args.output))
        window.mixer_page.dispose_ui()
        window.close()
        return 0 if saved else 1
    app.aboutToQuit.connect(window.mixer_page.dispose_ui)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
