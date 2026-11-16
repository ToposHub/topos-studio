"""Capture native Files presentation fixtures, not real media/backend results.

Run from repository root: PYTHONPATH=. .venv/bin/python
docs/validation/files_native_u01/capture_ui.py
"""
from pathlib import Path

from PyQt6.QtCore import QItemSelectionModel
from PyQt6.QtWidgets import QApplication

from src.app.ui.native_files_panel import NativeFilesPanel
from src.shared.ui.style_sheet import DARK_THEME


def main() -> None:
    app = QApplication([])
    app.setStyleSheet(DARK_THEME)
    panel = NativeFilesPanel(home_path="/Users/creator")
    panel.locations.set_children(panel.home_path, [
        {"name": name, "path": panel.home_path + "/" + name}
        for name in ("Desktop", "Documents", "Downloads", "Movies", "Music", "Pictures")
    ])
    panel.start_home_tree()
    entries = [
        {"name": "Interview", "type": "dir"},
        {"name": "Music", "type": "dir"},
        {"name": "Camera_A_001.mov", "type": "video", "size": "1.24 GB", "mtime": "2026-11-16", "rating": 4, "tags": "访谈, A机位", "comments": "主机位 · 正面镜头"},
        {"name": "Camera_B_001.mov", "type": "video", "size": "986 MB", "mtime": "2026-11-16"},
        {"name": "Ambient.wav", "type": "audio", "size": "86 MB"},
        {"name": "B_roll_01.mp4", "type": "video"},
        {"name": "Cover.png", "type": "image", "size": "12 MB"},
        {"name": "Reference.jpg", "type": "image"},
        {"name": "Voice_01.wav", "type": "audio"},
        {"name": "Shot_notes.pdf", "type": "doc"},
    ]
    for entry in entries:
        entry["path"] = "/Volumes/Media/" + entry["name"]
    panel.set_entries("/Volumes/Media", entries)
    panel.resize(1120, 650)
    panel.show()
    app.processEvents()
    index = next(panel.proxy.index(row, 0) for row in range(panel.proxy.rowCount()) if panel.proxy.index(row, 0).data() == "Camera_A_001.mov")
    panel.selection.select(index, QItemSelectionModel.SelectionFlag.ClearAndSelect | QItemSelectionModel.SelectionFlag.Rows)
    output = Path(__file__).parent
    app.processEvents()
    panel.grab().save(str(output / "native_files_grid.png"))
    panel.search_button.click()
    app.processEvents()
    panel.grab().save(str(output / "native_files_search.png"))
    panel.search_button.click()
    panel.view_combo.setCurrentIndex(1)
    app.processEvents()
    panel.grab().save(str(output / "native_files_list.png"))
    panel.resize(680, 520)
    app.processEvents()
    panel.grab().save(str(output / "native_files_narrow.png"))
    panel.resize(480, 560)
    app.processEvents()
    panel.grab().save(str(output / "native_files_compact.png"))
    panel.close()


if __name__ == "__main__":
    main()
