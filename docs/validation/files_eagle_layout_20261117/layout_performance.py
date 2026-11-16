"""Bounded offscreen layout probe, synthetic metadata and no media/GPU work."""
from __future__ import annotations

import json
import os
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

from src.app.ui.native_files_gallery_view import FilesGalleryView
from src.app.ui.native_files_panel import FilesEntryModel


def main() -> None:
    app = QApplication([])
    results = []
    for count in (100, 1000, 10_000):
        model = FilesEntryModel()
        model.set_entries([{"name": f"{row:05}.jpg", "path": f"/synthetic/{row}.jpg", "type": "image",
                            "width": (4000, 1920, 1600)[row % 3],
                            "height": (6000, 1080, 1600)[row % 3]} for row in range(count)])
        view = FilesGalleryView()
        view.setModel(model)
        view.resize(1000, 700)
        view.show()
        app.processEvents()
        for mode in ("adaptive", "masonry", "grid"):
            start = time.perf_counter()
            view.setLayoutMode(mode)
            view.doItemsLayout()
            app.processEvents()
            mode_ms = (time.perf_counter() - start) * 1000
            timings = []
            for width in (900, 1020, 880, 1000):
                start = time.perf_counter()
                view.resize(width, 700)
                app.processEvents()
                timings.append((time.perf_counter() - start) * 1000)
            results.append({"items": count, "mode": mode, "change_layout_ms": round(mode_ms, 2),
                            "resize_median_ms": round(statistics.median(timings), 2),
                            "resize_max_ms": round(max(timings), 2),
                            "visible_items": len(view.visibleIndexes())})
        view.close()
        view.deleteLater()
        app.processEvents()
    report = {"scope": "offscreen Qt layout+visible paint; synthetic metadata, no thumbnails/IO/GPU",
              "fps_claim": None, "results": results}
    (Path(__file__).parent / "layout_performance.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
