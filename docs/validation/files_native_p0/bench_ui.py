"""§9.3 规模基准：100/1000/10000 条目的模型填充、筛选、排序、首帧与内存。"""
from __future__ import annotations

import json
import os
import resource
import time

from PyQt6.QtWidgets import QApplication

from src.app.ui.native_files_panel import NativeFilesPanel
from src.features.files.contracts import Entry, TechnicalMetadata, UserMetadata

SIZES = (100, 1000, 10000)


def _entries(count: int) -> list[Entry]:
    items = []
    for index in range(count):
        kind = ("image", "video", "audio", "doc")[index % 4]
        name = f"素材_{index:05d}.{'jpg' if kind == 'image' else 'mov' if kind == 'video' else 'wav' if kind == 'audio' else 'pdf'}"
        items.append(Entry(
            path=f"/Users/creator/Library/{name}", name=name, kind=kind,
            is_dir=False, size_bytes=1024 * (index + 1), mtime_ts=time.time() - index,
            technical=TechnicalMetadata(duration_seconds=float(index % 90) if kind == "video" else 0.0),
            user=UserMetadata(rating=index % 6, flag=index % 3 == 0 and 2 or 0),
        ))
    return items


def _rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (1024 * 1024)


def _make_panel() -> NativeFilesPanel:
    panel = NativeFilesPanel(home_path="/Users/creator", preferences=None)
    panel.resize(900, 640)
    panel.show()
    QApplication.processEvents()
    return panel


def main() -> None:
    app = QApplication([])
    warm = _make_panel()
    warm.set_entries("/Users/creator/Library", _entries(50))
    warm.close()
    rows = []
    for count in SIZES:
        panel = _make_panel()
        entries = _entries(count)
        start = time.perf_counter()
        panel.set_entries("/Users/creator/Library", entries)
        app.processEvents()
        cold_ms = (time.perf_counter() - start) * 1000
        panel.set_entries("/Users/creator/Library", [])
        app.processEvents()
        start = time.perf_counter()
        panel.set_entries("/Users/creator/Library", entries)
        app.processEvents()
        fill_ms = (time.perf_counter() - start) * 1000
        start = time.perf_counter()
        panel.proxy.sort(1)
        app.processEvents()
        sort_ms = (time.perf_counter() - start) * 1000
        start = time.perf_counter()
        panel.search_edit.setText("00042")
        app.processEvents()
        filter_ms = (time.perf_counter() - start) * 1000
        panel.search_edit.clear()
        app.processEvents()
        start = time.perf_counter()
        panel.grab()
        app.processEvents()
        paint_ms = (time.perf_counter() - start) * 1000
        rows.append({
            "count": count, "cold_ms": round(cold_ms, 1), "hot_ms": round(fill_ms, 1),
            "sort_ms": round(sort_ms, 1),
            "filter_ms": round(filter_ms, 1), "first_paint_ms": round(paint_ms, 1),
            "peak_rss_mb": round(_rss_mb(), 1),
        })
        print(rows[-1])
        panel.close()
    target = os.path.join(os.path.dirname(__file__), "performance.json")
    with open(target, "w", encoding="utf-8") as fh:
        json.dump(rows, fh, ensure_ascii=False, indent=2)
    print("written", target)


if __name__ == "__main__":
    main()
