"""Capture all four real-media layouts with a read-only virtual snapshot.

Uses prepared, bounded thumbnails and temporary preferences. No disk scan,
decoder, file mutation or GPU runs in the widget. The mixed order is retained
from the manifest to make portrait/landscape/video geometry directly visible.
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from PyQt6.QtCore import QItemSelectionModel, QSettings, Qt
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import QApplication

from src.app.ui.native_files_panel import NativeFilesPanel
from src.app.ui.native_files_prefs import NativeFilesPreferences
from src.shared.ui.style_sheet import DARK_THEME


def main() -> None:
    output = Path(__file__).parent
    manifest = json.loads((output / "mixed_samples.json").read_text())
    entries = manifest["entries"]
    app = QApplication([])
    app.setFont(QFont("PingFang SC", 10))
    app.setStyleSheet(DARK_THEME)
    with tempfile.TemporaryDirectory(prefix="topos-four-layouts-") as temporary:
        prefs = NativeFilesPreferences(QSettings(str(Path(temporary) / "prefs.ini"),
                                                 QSettings.Format.IniFormat))
        prefs.density = 160
        panel = NativeFilesPanel(home_path=str(Path.home()), preferences=prefs)
        roots = [("照片目录", next(item["path"] for item in entries if item["name"].endswith(".jpg"))),
                 ("测试视频", next(item["path"] for item in entries if item["type"] == "video"))]
        panel.show_libraries([{"name": name, "path": str(Path(path).parent)} for name, path in roots])
        panel._last_search_query = "真实图片与视频 · 布局实测"
        panel.show_library_results({"files": entries, "total": len(entries)})
        panel.proxy.sort(-1)  # Keep the manifest's documented interleaved order.
        for entry in entries:
            panel.set_thumbnail(entry["path"], (output / entry["thumb"]).read_bytes())
        panel.resize(1360, 820)
        panel.show()
        for _ in range(4):
            app.processEvents()
        panel.selection.setCurrentIndex(panel.proxy.index(1, 0),
                                        QItemSelectionModel.SelectionFlag.ClearAndSelect
                                        | QItemSelectionModel.SelectionFlag.Rows)
        layouts: dict[str, Any] = {}
        for mode in ("adaptive", "masonry", "grid", "list"):
            panel.view_combo.setCurrentIndex(panel.view_combo.findData(mode))
            for _ in range(4):
                app.processEvents()
            view = panel.pages.currentWidget()
            view.verticalScrollBar().setValue(0)
            app.processEvents()
            panel.grab().save(str(output / f"mixed_{mode}.png"))
            layouts[mode] = {"page": panel.pages.currentIndex(),
                             "selected_path": panel.selection.currentIndex().data(panel.model.ENTRY_ROLE).path,
                             "viewport": [view.viewport().width(), view.viewport().height()],
                             "scroll_max": view.verticalScrollBar().maximum()}
            if mode == "grid":
                panel._set_grid_fill(True)
                app.processEvents()
                panel.grab().save(str(output / "mixed_grid_fill.png"))
                panel._set_grid_fill(False)
        panel.view_combo.setCurrentIndex(panel.view_combo.findData("masonry"))
        panel.resize(760, 560)
        for _ in range(4):
            app.processEvents()
        panel.grab().save(str(output / "mixed_narrow.png"))
        panel.resize(480, 520)
        for _ in range(4):
            app.processEvents()
        panel.grab().save(str(output / "mixed_compact.png"))
        panel.shutdown()
        panel.close()
        app.processEvents()
        (output / "four_layouts.json").write_text(
            json.dumps({"scope": "21 real items, prepared thumbnails, virtual read-only location",
                        "layouts": layouts, "source_write_operations": 0}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
