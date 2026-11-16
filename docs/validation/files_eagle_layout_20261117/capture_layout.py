"""Render real-photo UI snapshots using isolated preferences and worker-made thumbs.

This is a presentation harness: directory entries are read-only snapshots, not
an assertion of Flask integration, file mutation safety, GPU or audio playback.
--variant before loads the two visual modules from HEAD for the comparison.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import types
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from PyQt6.QtCore import QBuffer, QIODevice, QItemSelectionModel, QSettings, QSize, Qt
from PyQt6.QtGui import QFont, QImageReader
from PyQt6.QtWidgets import QApplication


def use_baseline() -> None:
    for name in ("native_files_gallery", "native_files_panel"):
        relative = f"src/app/ui/{name}.py"
        source = subprocess.run(["git", "show", f"HEAD:{relative}"], cwd=ROOT,
                                check=True, capture_output=True, text=True).stdout
        module = types.ModuleType(f"src.app.ui.{name}")
        module.__file__ = str(ROOT / relative)
        sys.modules[module.__name__] = module
        exec(compile(source, module.__file__, "exec"), module.__dict__)


def photo_snapshot(path: Path) -> tuple[dict[str, Any], bytes]:
    reader = QImageReader(str(path))
    reader.setAutoTransform(True)
    size = reader.size()
    reader.setScaledSize(size.scaled(QSize(480, 480), Qt.AspectRatioMode.KeepAspectRatio))
    image = reader.read()
    if image.isNull():
        raise RuntimeError(f"{path.name}: {reader.errorString()}")
    data = QBuffer()
    data.open(QIODevice.OpenModeFlag.WriteOnly)
    if not image.save(data, "PNG"):
        raise RuntimeError(f"Unable to encode thumb: {path.name}")
    stat = path.stat()
    entry = {"name": path.name, "path": str(path), "type": "image",
             "size_bytes": stat.st_size, "mtime_ts": stat.st_mtime,
             "width": size.width(), "height": size.height(), "codec": "JPEG"}
    return entry, bytes(data.data())


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--variant", choices=("before", "after"), default="after")
    parser.add_argument("--photo-dir", type=Path, required=True,
                        help="照片目录（含私人内容，不写默认值）")
    parser.add_argument("--video-dir", type=Path,
                        default=Path("/Volumes/Project_1/开发项目/topos-color-multitask /topos-color01/tests/test_videos"))
    args = parser.parse_args()
    if args.variant == "before":
        use_baseline()
    from src.app.ui.native_files_panel import NativeFilesPanel
    from src.app.ui.native_files_prefs import NativeFilesPreferences
    from src.shared.ui.style_sheet import DARK_THEME

    app = QApplication([])
    app.setFont(QFont("PingFang SC", 10))
    app.setStyleSheet(DARK_THEME)
    photos = sorted(path for path in args.photo_dir.iterdir() if path.suffix.lower() == ".jpg")
    # Decode all 123 small thumbs off the GUI thread; never touch source caches.
    with ThreadPoolExecutor(max_workers=2) as pool:
        snapshots = list(pool.map(photo_snapshot, photos))
    output = Path(__file__).parent
    with tempfile.TemporaryDirectory(prefix="topos-files-layout-") as temporary:
        settings = QSettings(str(Path(temporary) / "prefs.ini"), QSettings.Format.IniFormat)
        prefs = NativeFilesPreferences(settings)
        prefs.density = 160
        panel = NativeFilesPanel(home_path=str(Path.home()), preferences=prefs)
        panel.show_libraries([{"name": "照片目录", "path": str(args.photo_dir)},
                              {"name": "测试视频", "path": str(args.video_dir)}])
        panel.locations.set_children(panel.home_path, [
            {"name": name, "path": str(Path.home() / name)}
            for name in ("Desktop", "Documents", "Downloads", "Movies", "Pictures")])
        panel.start_home_tree()
        panel.set_entries(str(args.photo_dir), [entry for entry, _ in snapshots])
        for entry, data in snapshots:
            panel.set_thumbnail(entry["path"], data)
        panel.resize(1360, 820)
        panel.show()
        app.processEvents()
        panel.selection.select(panel.proxy.index(1, 0),
                               QItemSelectionModel.SelectionFlag.ClearAndSelect
                               | QItemSelectionModel.SelectionFlag.Rows)
        app.processEvents()
        panel.grab().save(str(output / f"photos_{args.variant}.png"))
        geometry = {"size": [panel.width(), panel.height()],
                    "sidebar": panel.sidebar.width(), "inspector": panel.inspector.width(),
                    "gallery_viewport": [panel.icon_view.viewport().width(), panel.icon_view.viewport().height()],
                    "grid": [panel.icon_view.gridSize().width(), panel.icon_view.gridSize().height()]}
        if args.variant == "after":
            panel.resize(760, 560)
            app.processEvents()
            panel.grab().save(str(output / "photos_narrow.png"))
            panel.resize(480, 520)
            app.processEvents()
            panel.grab().save(str(output / "photos_compact.png"))
            panel.resize(1180, 460)
            app.processEvents()
            panel.inspector_scroll.ensureWidgetVisible(panel.comment_edit)
            app.processEvents()
            panel.grab().save(str(output / "photos_short_inspector.png"))
            panel.view_combo.setCurrentIndex(panel.view_combo.findData("list"))
            panel.resize(1360, 820)
            app.processEvents()
            panel.grab().save(str(output / "photos_list.png"))
        (output / f"layout_{args.variant}.json").write_text(
            json.dumps({"variant": args.variant, "photos": len(snapshots), "geometry": geometry,
                        "source_write_operations": 0, "preferences": "temporary INI"}, indent=2))
        panel.shutdown()
        panel.close()
        app.processEvents()


if __name__ == "__main__":
    main()
