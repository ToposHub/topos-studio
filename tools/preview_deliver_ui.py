#!/usr/bin/env python3
"""Preview Deliver with real shared panels and disposable local media.

    .venv/bin/python tools/preview_deliver_ui.py
    .venv/bin/python tools/preview_deliver_ui.py --locale zh_CN --screenshot /tmp/deliver.png

The review project, queue and configuration are isolated from the editor.
No render is started. The viewer shows a decoded frame from the review media;
transport is not connected to a playback controller in this UI-only harness.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--locale", choices=("en_US", "zh_CN"), default="en_US")
    parser.add_argument("--screenshot", type=Path)
    parser.add_argument("--size", default="1580x1020")
    parser.add_argument("--tab", choices=("video", "audio", "file"), default="video")
    parser.add_argument("--advanced", action="store_true")
    args = parser.parse_args()
    width, height = (int(value) for value in args.size.split("x"))

    # Decode before starting the UI/event loop; the editor's renderer is not
    # involved, and the preview never synchronously decodes on interaction.
    import av

    media = [ROOT / "tests/test_videos/2K_h264.mp4", ROOT / "tests/test_videos/2K_H265.mov"]
    with av.open(str(media[0])) as container:
        pixels = next(container.decode(video=0)).to_ndarray(format="rgb24")

    from PyQt6.QtCore import QTimer
    from PyQt6.QtGui import QImage, QSurfaceFormat
    from PyQt6.QtWidgets import QApplication, QDockWidget, QMainWindow, QToolButton
    from src.features.deliver.ui.deliver_workspace import DeliverWorkspace
    from src.features.edit.ui.program_viewer import ProgramViewerWidget
    from src.features.edit.ui.source_viewer import SourceViewerWidget
    from src.features.edit.ui.edit_viewport import EditViewport
    from src.features.edit.ui.timeline_panel import TimelinePanel
    from src.shared.i18n.i18n import I18N
    from src.shared.ui.style_sheet import DARK_THEME
    from tools.preview_resolve_timeline import build_review_project

    with tempfile.TemporaryDirectory(prefix="topos-deliver-review-") as config:
        os.environ["TOPOS_CONFIG_PATH"] = str(Path(config) / "config.json")
        surface = QSurfaceFormat()
        surface.setVersion(4, 1)
        surface.setProfile(QSurfaceFormat.OpenGLContextProfile.CoreProfile)
        QSurfaceFormat.setDefaultFormat(surface)
        app = QApplication(sys.argv[:1])
        app.setStyle("Fusion")
        app.setStyleSheet(DARK_THEME)
        I18N.load_locale(args.locale)
        project, sequence = build_review_project(media)
        window = QMainWindow()
        window.setWindowTitle("Topos — Deliver UI Review")
        window.resize(width, height)
        workspace = DeliverWorkspace()
        window.setCentralWidget(workspace)
        workspace.set_project(project)
        workspace.set_sequence(sequence)
        workspace.attach_batch_panel_host(window)

        timeline = TimelinePanel()
        timeline.set_project(project)
        timeline.load_timeline(sequence)
        viewer = ProgramViewerWidget()
        viewport = EditViewport(viewer, SourceViewerWidget())
        viewport.prepare_for_deliver()
        viewport.set_program_range(sequence.duration_frames, sequence.fps)
        viewer.set_timeline_choices(project.timelines, sequence.timeline_id)
        viewer.set_timecode("01:00:00:00", float(sequence.fps))
        image = QImage(pixels.data, pixels.shape[1], pixels.shape[0], pixels.strides[0],
                       QImage.Format.Format_RGB888).copy()
        viewer.set_qimage(image)
        viewer_dock = QDockWidget("Program", window)
        timeline_dock = QDockWidget("Timeline", window)
        viewer_dock.setWidget(viewport)
        timeline_dock.setWidget(timeline)
        workspace.attach_edit_panels(viewer, timeline, viewer_dock, timeline_dock)
        viewer_dock.hide()
        timeline_dock.hide()

        settings = workspace.settings_panel
        settings._output_dir_edit.setText(config)
        jobs, _ = settings.build_jobs(preflight=False)
        workspace.controller.add_jobs(jobs)
        settings._mode_tabs.setCurrentIndex(("video", "audio", "file").index(args.tab))
        if args.advanced:
            for button in settings.findChildren(QToolButton, "DeliverDisclosure"):
                button.setChecked(True)
        window.show()
        settings._filename_edit.clearFocus()
        workspace.setFocus()

        def arrange() -> None:
            workspace.ensure_deliver_layout()
            timeline.timeline_view.zoom_fit_all()

        QTimer.singleShot(100, arrange)
        if args.screenshot:
            def capture() -> None:
                args.screenshot.parent.mkdir(parents=True, exist_ok=True)
                if not window.grab().save(str(args.screenshot.resolve())):
                    raise RuntimeError(f"Could not save {args.screenshot}")
                print(args.screenshot.resolve(), flush=True)
                window.close()
                app.quit()

            QTimer.singleShot(1800, capture)
        return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
