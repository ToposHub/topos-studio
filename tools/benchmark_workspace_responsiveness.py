#!/usr/bin/env python3
"""Measure real Qt widget resize/layout/paint work without a decoder or GL.

Run with QT_QPA_PLATFORM=offscreen for a repeatable local comparison. These
measurements exclude live video, GPU presentation and OS window composition.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import statistics
import sys
import time
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _resize_samples(app: Any, widget: Any, width: int, height: int, iterations: int) -> dict[str, float]:
    widget.resize(width, height)
    widget.show()
    for _ in range(4):
        app.processEvents()
    samples: list[float] = []
    for index in range(1, iterations + 1):
        started = time.perf_counter()
        widget.resize(width + (index % 10) * 7, height + (index % 9) * 5)
        app.processEvents()
        samples.append((time.perf_counter() - started) * 1000.0)
    widget.close()
    return {"median_ms": round(statistics.median(samples), 3),
            "p95_ms": round(sorted(samples)[math.ceil(len(samples) * 0.95) - 1], 3),
            "max_ms": round(max(samples), 3)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iterations", type=int, default=40)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.iterations < 2:
        parser.error("--iterations must be at least 2")
    from PyQt6.QtWidgets import QApplication
    from src.features.edit.core.timeline_v2 import ClipInstance, EditTrack, TimelineSequence
    from src.features.edit.ui.timeline_view import TimelineView
    from src.shared.ui.audio_mixer.audio_mixer_panel import AudioMixerPanel

    app = QApplication([])
    results: dict[str, Any] = {
        "platform": app.platformName(), "iterations": args.iterations,
        "scope": "Qt resize/layout/paint; no decoding, GL or OS composition",
    }
    for count in (200, 2000):
        tracks: list[EditTrack] = []
        for kind in ("video", "audio"):
            for index in range(4):
                track_id = f"{kind}-{index}"
                clips = tuple(ClipInstance(
                    f"{track_id}-{i}", "unresolved", track_id, i * 100, 100, 0, 100,
                ) for i in range(count // 8))
                tracks.append(EditTrack(track_id, track_id, kind, index, clips=clips))
        view = TimelineView()
        view.load_timeline(TimelineSequence(
            "resize-benchmark", video_tracks=tuple(tracks[:4]), audio_tracks=tuple(tracks[4:]),
        ))
        results[f"timeline_{count}_clips"] = _resize_samples(app, view, 1200, 500, args.iterations)
        view.deleteLater()
        app.processEvents()
    mixer = AudioMixerPanel()
    for index in range(8):
        mixer.register_track(f"audio-{index}", f"Audio {index + 1}", f"A{index + 1}")
    results["mixer_8_tracks"] = _resize_samples(app, mixer, 540, 500, args.iterations)
    serialized = json.dumps(results, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(serialized + "\n", encoding="utf-8")
    print(serialized)


if __name__ == "__main__":
    main()
