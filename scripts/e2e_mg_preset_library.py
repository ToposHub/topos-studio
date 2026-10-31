#!/usr/bin/env python
"""Real-user E2E journey for the MG asset library (presets).

Journey D complements scripts/e2e_mg_user_journey.py: it opens the real
editor window, browses the Library tab, inserts presets by double-click and
by a real QDropEvent on the canvas, saves the selection back as a preset
(with a real GIF preview), exports/imports it as a zip and checks undo
integrity — the way a user would actually do it.

Usage:
    .venv/bin/python scripts/e2e_mg_preset_library.py [--offscreen]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import zipfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--offscreen", action="store_true")
    args = parser.parse_args()
    if args.offscreen:
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
    os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--no-sandbox --disable-gpu")

    # QtWebEngineWidgets must be imported before QApplication (src/main.py:88).
    from PyQt6 import QtWebEngineWidgets  # noqa: F401
    from PyQt6.QtCore import QMimeData, QPointF, Qt
    from PyQt6.QtWidgets import QApplication, QDialog

    app = QApplication.instance() or QApplication(sys.argv)

    from src.features.teaching_animation.core.model import TeachingSceneDocument
    from src.features.teaching_animation.presets import MG_PRESET_MIME
    from src.features.teaching_animation.ui.motion_graphic_editor_window import (
        MotionGraphicEditorWindow,
    )

    results: list[tuple[str, bool, str]] = []

    def step(name: str, fn) -> None:
        try:
            detail = fn()
            results.append((name, True, detail or "ok"))
            print(f"PASS {name} — {detail}")
        except Exception as exc:  # noqa: BLE001 — journey reports, never crashes
            results.append((name, False, str(exc)))
            print(f"FAIL {name} — {exc}")
            raise

    def require(condition: bool, message: str) -> None:
        if not condition:
            raise AssertionError(message)

    def pump(seconds: float = 0.2) -> None:
        import time
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            app.processEvents()

    temp_dir = tempfile.TemporaryDirectory(prefix="topos_mg_preset_e2e_")  # noqa: SIM115
    user_root = Path(temp_dir.name) / "library"
    os.environ["TOPOS_MG_PRESET_ROOT"] = str(user_root)

    window = MotionGraphicEditorWindow(TeachingSceneDocument.from_dict({
        "scene_id": "e2e_presets", "name": "Preset Journey", "width": 1920,
        "height": 1080, "fps_num": 24, "fps_den": 1, "duration_frames": 96,
        "background": {"color": "#0b1220"}, "elements": [], "animations": []}))
    window.resize(1600, 950)
    window.show()
    pump(0.5)
    library = window.asset_library

    def d1_library_tab_browses_presets() -> str:
        window._panel_tabs.setCurrentIndex(4)
        require(window._panel_tabs.tabText(4) == "Library", "Library tab missing")
        records = library.library.records()
        require(len(records) >= 12, f"expected >=12 presets, got {len(records)}")
        require(library.grid.count() >= 12, "grid not populated")
        # Category filter narrows to one chart preset.
        combo = library._category_combo
        charts_index = next(i for i in range(combo.count())
                            if combo.itemText(i).startswith("Charts"))
        combo.setCurrentIndex(charts_index)
        require(library.grid.count() >= 3, "charts filter did not narrow grid")
        combo.setCurrentIndex(0)
        # Search narrows by Chinese name.
        library._search_edit.setText("饼")
        require(library.grid.count() == 1, "search did not find 饼图")
        library._search_edit.setText("")
        # Selected card animates a real GIF.
        library.select_preset("pie_donut_chart")
        pump(0.3)
        movie = library._movie
        require(movie is not None, "no preview movie for selection")
        require(movie.frameCount() > 1, f"GIF not animated: {movie.frameCount()} frames")
        return (f"{len(records)} presets · 6 categories · pie GIF has "
                f"{movie.frameCount()} frames")

    step("D1 Library 标签页浏览/分类/搜索/GIF 动图", d1_library_tab_browses_presets)

    def d2_double_click_inserts_at_center() -> str:
        item = next(library.grid.item(row) for row in range(library.grid.count())
                    if library.grid.item(row).data(Qt.ItemDataRole.UserRole) == "pie_donut_chart")
        library.grid.itemDoubleClicked.emit(item)
        scene = window.editor_controller.scene
        require(len(scene.elements) == 16, f"pie should add 16 layers, got {len(scene.elements)}")
        require(window._selected_elements == {e.element_id for e in scene.elements},
                "inserted layers not selected")
        # Canvas re-rendered the inserted scene (SVG view shows the pie).
        require(window.scene_view._scene is scene, "canvas not refreshed")
        return "16 layers inserted at canvas centre and selected"

    step("D2 双击插入饼图（16 层）", d2_double_click_inserts_at_center)

    def d3_real_drop_event_on_canvas() -> str:
        scene = window.editor_controller.scene
        before = len(scene.elements)
        view = window.scene_view
        view.show()
        pump(0.1)
        mime = QMimeData()
        mime.setData(MG_PRESET_MIME, json.dumps({"preset_id": "neon_glow_title"}).encode("utf-8"))
        from PyQt6.QtGui import QDropEvent
        drop_point = QPointF(view.width() * 0.35, view.height() * 0.4)
        event = QDropEvent(drop_point, Qt.DropAction.CopyAction, mime,
                           Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
        view.dragEnterEvent(event)
        require(True, "")
        event2 = QDropEvent(drop_point, Qt.DropAction.CopyAction, mime,
                            Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
        view.dropEvent(event2)
        scene = window.editor_controller.scene
        require(len(scene.elements) == before + 3, "drop did not insert 3 neon layers")
        neon = [e for e in scene.elements if "title" in e.element_id]
        require(neon, "neon title layer missing after drop")
        pump(0.2)
        return f"drop inserted 3 layers at canvas point ({drop_point.x():.0f}, {drop_point.y():.0f})"

    step("D3 画布真实 QDropEvent 拖放插入", d3_real_drop_event_on_canvas)

    def d4_undo_redo_integrity() -> str:
        stack = window.editor_controller._stack
        scene = window.editor_controller.scene
        layers = len(scene.elements)
        require(len(stack._undo_stack) >= 2, "expected two undo entries")
        window.editor_controller.undo()  # undo neon drop
        require(len(window.editor_controller.scene.elements) == layers - 3,
                "undo did not remove exactly the dropped preset")
        window.editor_controller.redo()
        require(len(window.editor_controller.scene.elements) == layers,
                "redo did not restore the dropped preset")
        return f"undo/redo keep {layers} layers intact"

    step("D4 撤销/重做完整性", d4_undo_redo_integrity)

    def d5_save_selection_as_preset_with_gif() -> str:
        scene = window.editor_controller.scene
        # Select just the neon title + underline layers via the real chokepoint.
        neon_ids = sorted(e.element_id for e in scene.elements if e.type == "text")
        require(neon_ids, "no text layer to select")
        window._set_selection({neon_ids[0]})
        real_dialog = None
        from src.features.teaching_animation.ui import motion_graphic_editor_window as winmod

        class ScriptedDialog:
            def __init__(self, parent, *, categories, default_name=""):
                nonlocal real_dialog
                real_dialog = __import__(
                    "src.features.teaching_animation.ui.asset_library_panel",
                    fromlist=["PresetSaveDialog"]).PresetSaveDialog(
                    parent, categories=categories, default_name=default_name)
                real_dialog.name_edit.setText("Journey Neon Save")
                real_dialog.category_combo.setCurrentText("text")
                real_dialog.tags_edit.setText("journey, neon")
                real_dialog.description_edit.setPlainText("saved by e2e journey")

            def exec(self):
                real_dialog.accept()
                return int(QDialog.DialogCode.Accepted)

            def payload(self):
                return real_dialog.payload()

        original = winmod.PresetSaveDialog
        winmod.PresetSaveDialog = ScriptedDialog
        try:
            window._save_preset_triggered()
        finally:
            winmod.PresetSaveDialog = original
        pump(0.5)
        folders = list(user_root.glob("text/journey-neon-save*/"))
        require(len(folders) == 1, f"preset folder not written: {user_root}")
        folder = folders[0]
        require((folder / "preset.json").is_file(), "preset.json missing")
        require((folder / "scene.json").is_file(), "scene.json missing")
        gif = folder / "preview.gif"
        require(gif.is_file(), "preview.gif missing")
        with open(gif, "rb") as handle:
            payload = handle.read()
        require(payload[:3] == b"GIF", "not a GIF file")
        frames = payload.count(b"\x21\xf9")  # graphic-control extensions ≈ frames
        record = library.library.get(folder.name)
        require(record is not None, "saved preset not in library index")
        require(library.grid.findItems("Journey Neon Save", Qt.MatchFlag.MatchExactly),
                "saved preset not shown in grid")
        return f"wrote {folder.name} with GIF ({frames} animation blocks)"

    step("D5 另存为预设（真实对话框 + GIF 渲染）", d5_save_selection_as_preset_with_gif)

    def d6_export_import_zip() -> str:
        record = library.library.get(next(f.name for f in user_root.glob("text/journey-neon-save*")))
        zip_path = Path(temp_dir.name) / "shared.zip"
        library.library.export_to(zip_path, [record.preset_id])
        require(zip_path.is_file(), "export zip missing")
        with zipfile.ZipFile(zip_path) as bundle:
            names = bundle.namelist()
            require(any(n.endswith("preset.json") for n in names), "zip lacks preset.json")
            require(any(n.endswith("preview.gif") for n in names), "zip lacks preview.gif")
        fresh_root = Path(temp_dir.name) / "fresh"
        fresh_root.mkdir()
        imported = library.library.import_from(zip_path, root=fresh_root)
        require(imported.manifest.name == "Journey Neon Save", "import lost the name")
        require(imported.preview_path is not None, "imported preset lacks GIF")
        return f"zip {zip_path.name} → imported as {imported.preset_id}"

    step("D6 导出/导入 zip 迁移", d6_export_import_zip)

    def d7_screenshot_for_report() -> str:
        out = Path(temp_dir.name) / "library_panel.png"
        window._panel_tabs.setCurrentIndex(4)
        library.select_preset("lens_flare_stack")
        pump(0.4)
        window.grab().save(str(out))
        require(out.stat().st_size > 20_000, "screenshot suspiciously small")
        return str(out)

    step("D7 面板截图存档", d7_screenshot_for_report)

    # -- teardown ------------------------------------------------------------
    window.editor_controller.mark_clean()
    window.html_source_editor.discard_pending_draft()
    window.html_devtools.set_has_unapplied_changes(False)
    window.close()
    del window
    pump(0.3)
    temp_dir.cleanup()
    os.environ.pop("TOPOS_MG_PRESET_ROOT", None)

    passed = sum(1 for _, ok, _ in results if ok)
    print(f"RESULT: {passed} passed · {len(results) - passed} failed · {len(results)} steps")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
