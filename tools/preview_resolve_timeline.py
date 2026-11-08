#!/usr/bin/env python3
"""Preview the timeline panel with real local media in an isolated review window.

    .venv/bin/python tools/preview_resolve_timeline.py
    .venv/bin/python tools/preview_resolve_timeline.py --save-project /tmp/review.topos

Uses existing import and project IO APIs; it never changes the running editor's
project or preferences. No synthetic meter activity is generated.
"""
from __future__ import annotations

import argparse
from dataclasses import replace
from fractions import Fraction
import os
from pathlib import Path
import sys
import time
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def build_review_project(media_paths: list[Path]) -> tuple[Any, Any]:
    """Build a disposable A/V fixture through the existing immutable models."""
    from src.features.edit.core.project import EditProject, TimelineSequenceRef, probe_media_metadata
    from src.features.edit.core.timeline_v2 import ClipInstance, EditTrack, TimelineMarker, TimelineSequence
    from src.shared.media.metadata import Rational

    project = EditProject.create(name="Resolve Timeline UI Review")
    assets = []
    for path in media_paths:
        if not path.is_file():
            raise FileNotFoundError(path)
        project = project.import_asset(str(path.resolve()), "video", probe_media_metadata(str(path)))
        assets.append(project.get_asset_by_path(str(path.resolve())))
    rate = Fraction(float(assets[0].metadata.get("fps") or 25)).limit_denominator(1001)
    fps = float(rate)
    tracks: dict[str, list[Any]] = {key: [] for key in ("v1", "v2", "a1", "a2")}
    for lane, count in ((1, 7), (2, 3)):
        cursor = 0 if lane == 1 else round(fps * 2.5)
        for i in range(count):
            asset = assets[i % len(assets)]
            source_rate = float(asset.metadata.get("fps") or fps)
            total = int(asset.metadata.get("total_frames") or source_rate * 5)
            duration = max(1, min(round(fps * (2.0 + (i % 3) * 0.75)), round(total * fps / source_rate)))
            source_duration = max(1, round(duration * source_rate / fps))
            source_start = min(round(i * 1.3 * source_rate), max(0, total - source_duration))
            video_id, audio_id = f"v{lane}-{i}", f"a{lane}-{i}"
            for prefix, clip_id, partner in (("v", video_id, audio_id), ("a", audio_id, video_id)):
                track_id = f"{prefix}{lane}"
                tracks[track_id].append(ClipInstance(
                    clip_id=clip_id, asset_id=asset.asset_id, track_id=track_id,
                    timeline_start=cursor, timeline_duration=duration,
                    source_start=source_start, source_duration=source_duration,
                    linked_clip_id=partner,
                ))
            cursor += duration + (round(fps * 2.0) if lane == 2 else 0)
    sequence = TimelineSequence(
        timeline_id="resolve-ui-review", name="Timeline UI Review", fps=Rational(rate.numerator, rate.denominator),
        width=int(assets[0].metadata.get("width") or 1920),
        height=int(assets[0].metadata.get("height") or 1080),
        video_tracks=tuple(EditTrack(
            track_id=f"v{i}", name=name, track_type="video", index=i - 1, clips=tuple(tracks[f"v{i}"]),
        ) for i, name in ((1, "Video 1"), (2, "Cutaways"))),
        audio_tracks=tuple(EditTrack(
            track_id=f"a{i}", name=name, track_type="audio", index=i - 1, clips=tuple(tracks[f"a{i}"]),
        ) for i, name in ((1, "Dialogue"), (2, "Ambience"))),
        markers=(TimelineMarker(marker_id="review-in", frame=round(fps * 4), name="Review", color="#E29227"),),
    )
    project = project.with_timeline_ref_added(TimelineSequenceRef(
        timeline_id=sequence.timeline_id, name=sequence.name, fps=fps,
        width=sequence.width, height=sequence.height, created_at=time.time(),
    ), set_active=True)
    return project, sequence


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("media", nargs="*", type=Path)
    parser.add_argument("--save-project", type=Path)
    parser.add_argument("--project-only", action="store_true")
    args = parser.parse_args()
    media = args.media or [ROOT / "tests/test_videos/2K_h264.mp4", ROOT / "tests/test_videos/测试素材01.mov"]
    from PyQt6.QtWidgets import QApplication, QMainWindow
    from PyQt6.QtCore import QTimer
    from src.features.edit.ui.timeline_panel import TimelinePanel
    from src.features.edit.ui.edit_style import timeline_style

    app = QApplication(sys.argv[:1])
    app.setStyle("Fusion")
    project, sequence = build_review_project(media)
    if args.save_project:
        from src.features.edit.core.edit_project_io import EditProjectIO
        args.save_project.parent.mkdir(parents=True, exist_ok=True)
        output = args.save_project.resolve()
        # Keep the disposable review fixture relocatable with the repository.
        # The running preview retains absolute paths; only the saved snapshot
        # uses paths relative to its own package, through the existing IO API.
        saved_project = replace(project, assets={
            key: replace(asset, path=os.path.relpath(asset.path, output.parent))
            for key, asset in project.assets.items()
        })
        io = EditProjectIO()
        if not io.save_project(str(output), saved_project,
                               timeline_sequences={sequence.timeline_id: sequence},
                               create_backup=False):
            raise RuntimeError(f"Could not save review project: {args.save_project}")
        print(f"Saved {args.save_project}", flush=True)
    if args.project_only:
        return 0
    window = QMainWindow()
    window.setWindowTitle("Topos — Resolve Timeline UI Review")
    window.resize(1420, 660)
    panel = TimelinePanel()
    panel.setStyleSheet(timeline_style())
    panel.set_project(project)
    panel.load_timeline(sequence)
    window.setCentralWidget(panel)
    window.show()
    QTimer.singleShot(100, panel.timeline_view.zoom_fit_all)
    QTimer.singleShot(200, lambda: panel.set_playhead_frame(round(float(sequence.fps) * 4)))
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
