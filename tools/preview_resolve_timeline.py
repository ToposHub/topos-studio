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
import tempfile
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
    parser.add_argument("--speed-dialog", action="store_true", help="Open the real speed dialog for visual review")
    parser.add_argument("--viewer", action="store_true", help="Include the real Program Viewer and an isolated trim-preview trigger")
    parser.add_argument("--screenshot", type=Path, help="Save the review window and exit")
    parser.add_argument("--size", default="1420x660", help="Review window WIDTHxHEIGHT")
    parser.add_argument("--extra-tracks", type=int, default=0,
                        help="Add empty video/audio tracks to review overflow scrolling")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="topos-timeline-review-") as config:
        os.environ["TOPOS_CONFIG_PATH"] = str(Path(config) / "config.json")
        return _show_preview(args)


def _show_preview(args: argparse.Namespace) -> int:
    media = args.media or [ROOT / "tests/test_videos/2K_h264.mp4", ROOT / "tests/test_videos/2K_H265.mov"]
    from PyQt6.QtWidgets import QApplication, QMainWindow
    from PyQt6.QtCore import QTimer
    from src.features.edit.ui.timeline_panel import TimelinePanel
    from src.shared.ui.style_sheet import DARK_THEME

    if args.viewer:
        from PyQt6.QtGui import QSurfaceFormat

        surface = QSurfaceFormat()
        surface.setVersion(4, 1)
        surface.setProfile(QSurfaceFormat.OpenGLContextProfile.CoreProfile)
        QSurfaceFormat.setDefaultFormat(surface)
    app = QApplication(sys.argv[:1])
    app.setStyle("Fusion")
    app.setStyleSheet(DARK_THEME)
    project, sequence = build_review_project(media)
    if args.extra_tracks > 0:
        from src.features.edit.core.timeline_v2 import EditTrack
        sequence = replace(sequence, **{
            f"{kind}_tracks": getattr(sequence, f"{kind}_tracks") + tuple(
                EditTrack(f"{prefix}{index + 1}", f"{kind.title()} {index + 1}", kind, index)
                for index in range(len(getattr(sequence, f"{kind}_tracks")),
                                   len(getattr(sequence, f"{kind}_tracks")) + args.extra_tracks)
            )
            for kind, prefix in (("video", "v"), ("audio", "a"))
        })
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
    width, height = (int(value) for value in args.size.split("x"))
    window.resize(width, height)
    panel = TimelinePanel()
    panel.set_project(project)
    panel.load_timeline(sequence)
    if args.viewer:
        from PyQt6.QtWidgets import QWidget, QVBoxLayout, QPushButton
        from src.features.edit.core.timeline_preview_renderer import create_timeline_preview_renderer
        from src.features.edit.ui.program_viewer import ProgramViewerWidget
        central = QWidget(window)
        column = QVBoxLayout(central)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(0)
        viewer = ProgramViewerWidget(central)
        renderer = create_timeline_preview_renderer(
            project=project, sequence=sequence, main_context=None,
            config={"viewport_size": (1280, 720), "cache_size": 8},
        )
        view = panel.timeline_view
        view.timeline_changed.connect(renderer.set_sequence)
        view.playhead_changed.connect(renderer.request_render)
        view.trim_frame_preview_requested.connect(renderer.request_scrub_preview)
        view.trim_frame_preview_finished.connect(renderer.finish_scrub)
        renderer.frame_ready.connect(viewer.set_frame)
        app.aboutToQuit.connect(renderer.shutdown)
        trigger = QPushButton("Trim preview", central)
        trigger.setMaximumWidth(120)
        def begin_trim_preview() -> None:
            view.setFocus()
            clip = view.get_timeline().get_clip_by_id("v1-0")
            view._on_roll_trim_started("v1-0", "v1-1", clip.timeline_end)
        trigger.clicked.connect(begin_trim_preview)
        column.addWidget(trigger)
        column.addWidget(viewer, stretch=4)
        column.addWidget(panel, stretch=5)
        window.resize(1420, 980)
        window.setCentralWidget(central)
    else:
        window.setCentralWidget(panel)
    window.show()
    QTimer.singleShot(100, panel.timeline_view.zoom_fit_all)
    QTimer.singleShot(200, lambda: panel.set_playhead_frame(round(float(sequence.fps) * 4)))
    if args.speed_dialog:
        QTimer.singleShot(300, lambda: panel.timeline_view._on_change_clip_speed_dialog_requested("v1-0", "v1"))
    if args.screenshot:
        def capture() -> None:
            args.screenshot.parent.mkdir(parents=True, exist_ok=True)
            if not window.grab().save(str(args.screenshot.resolve())):
                raise RuntimeError(f"Could not save {args.screenshot}")
            print(args.screenshot.resolve(), flush=True)
            window.close()
            app.quit()

        QTimer.singleShot(3000, capture)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
