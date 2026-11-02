#!/usr/bin/env python3
"""Import real media through the app services and verify source-node previews.

Run in a dedicated application process. The script uses an isolated temporary
configuration and project; it does not open/save an existing user project.
This validates source thumbnail presentation, NOT per-node grade evaluation.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("media", type=Path)
    parser.add_argument("--output", type=Path, default=ROOT / "docs/validation/color_nodes")
    parser.add_argument("--keep-open", action="store_true")
    args = parser.parse_args()
    source = args.media.resolve(strict=True)
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    config_dir = Path(tempfile.mkdtemp(prefix="topos-node-media-"))
    config_path = config_dir / "config.json"
    config_path.write_text(json.dumps({"locale": "en_US", "work_mode": "video", "current_page": "Color",
                                       "show_welcome_on_startup": False, "auto_load_last_project": False,
                                       "check_updates_on_startup": False, "auto_save_interval": 0}))
    os.environ["TOPOS_CONFIG_PATH"] = str(config_path)
    os.environ["TOPOS_PRODUCT_MODE"] = "studio"
    os.environ["TOPOS_TEST_MODE"] = "1"
    os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")

    import src.main as app_main
    from PyQt6.QtCore import QObject, QTimer
    from PyQt6.QtWidgets import QApplication
    from src.app.main_window import MainWindow
    from src.features.color.ui.node_editor_state import NodeKind

    class Driver(QObject):
        def __init__(self, host: Any) -> None:
            super().__init__(host)
            self.host = host
            self.phase = "start"
            self.started = time.monotonic()
            self.asset: Any = None
            self.clip: Any = None
            self.timeline: Any = None
            self.report: dict[str, Any] = {
                "media": str(source.relative_to(ROOT)) if source.is_relative_to(ROOT) else source.name,
                "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                "validation": "real_app_import_and_source_thumbnail",
                "per_node_rendering": "not_implemented",
            }
            self.timer = QTimer(self)
            self.timer.setInterval(200)
            self.timer.timeout.connect(self.tick)
            self.timer.start()

        def finish(self, success: bool, error: str = "") -> None:
            self.timer.stop()
            self.report.update(status="passed" if success else "failed", error=error,
                               elapsed_seconds=round(time.monotonic() - self.started, 2))
            (output / "media_preview_report.json").write_text(json.dumps(self.report, ensure_ascii=False, indent=2) + "\n")
            print(json.dumps(self.report, ensure_ascii=False), flush=True)
            if not args.keep_open:
                app = QApplication.instance()
                app.exit(0 if success else 1)

        def tick(self) -> None:
            try:
                if time.monotonic() - self.started > 90:
                    raise TimeoutError("media import or source thumbnail did not complete in 90 seconds")
                if self.phase == "start":
                    self.host._auto_create_edit_project(name="Color Node Preview Verification")
                    self.host.media_pool_panel._import_files([str(source)])
                    self.phase = "import"
                    return
                if self.phase == "import":
                    project = self.host._current_edit_project
                    self.asset = project.get_asset_by_path(str(source))
                    if self.asset is None:
                        return
                    self.report["asset_id"] = self.asset.asset_id
                    self.report["metadata"] = {"width": self.asset.width, "height": self.asset.height,
                                               "fps": self.asset.fps, "total_frames": self.asset.total_frames}
                    tv = self.host.timeline_panel.timeline_view
                    if not tv._ensure_first_asset_timeline_settings(self.asset):
                        raise RuntimeError("could not create timeline")
                    self.timeline = self.host.edit_page._current_timeline
                    if not tv._add_asset_as_clip(self.asset, self.timeline.video_tracks[0], 0):
                        raise RuntimeError("could not insert timeline clip")
                    self.phase = "timeline"
                    return
                if self.phase == "timeline":
                    self.timeline = self.host.edit_page._current_timeline
                    self.clip = self.timeline.video_tracks[0].clips[0]
                    self.host.open_timeline_clip_in_color(self.timeline.timeline_id, self.clip.clip_id, 0)
                    self.phase = "preview"
                    return
                if self.phase == "preview":
                    panel = self.host.color_node_panel
                    bridge = self.host.color_node_source_preview
                    bridge.refresh()
                    pixmap = panel.view._source_thumbnail
                    if pixmap is None or pixmap.isNull():
                        return
                    import numpy as np
                    image = pixmap.toImage().convertToFormat(pixmap.toImage().Format.Format_RGBA8888)
                    ptr = image.constBits()
                    ptr.setsize(image.sizeInBytes())
                    pixels = np.frombuffer(ptr, dtype=np.uint8).reshape(image.height(), image.bytesPerLine())
                    pixels = pixels[:, :image.width() * 4].reshape(image.height(), image.width(), 4)[..., :3]
                    if float(pixels.std()) < 5.0:
                        raise AssertionError("source thumbnail is blank or flat")
                    first = panel.view.state.graph.active_id
                    second = panel.view.apply_edit("add_serial", panel.view.state.add_serial)
                    for node_id in (first, second):
                        card = panel.view.cards[node_id]
                        if card._thumbnail.isNull():
                            raise AssertionError("node card did not receive source thumbnail")
                    panel.view.fit_graph()
                    pixmap.save(str(output / "source_thumbnail.png"))
                    panel.grab().save(str(output / "nodes_with_media.png"))
                    self.host.grab().save(str(output / "studio_with_media.png"))
                    self.report.update(
                        clip_id=self.clip.clip_id, timeline_id=self.timeline.timeline_id,
                        current_file_matches_source=self.host.current_file_path == str(source),
                        current_page=self.host.current_page,
                        thumbnail_size=[pixmap.width(), pixmap.height()],
                        thumbnail_rgb_std=round(float(pixels.std()), 3),
                        corrector_thumbnails_present=2,
                        source_tooltip=panel.view.cards[first].toolTip(),
                    )
                    self.finish(True)
            except (RuntimeError, ValueError, TypeError, AttributeError, IndexError, OSError, AssertionError) as exc:
                self.finish(False, f"{type(exc).__name__}: {exc}")

    original_init = MainWindow.__init__

    def validation_init(self: Any, *init_args: Any, **kwargs: Any) -> None:
        original_init(self, *init_args, **kwargs)
        self._node_validation_driver = Driver(self)

    MainWindow.__init__ = validation_init
    sys.argv = [sys.argv[0], "--product", "studio", "--no-startup-dialog"]
    return app_main.main()


if __name__ == "__main__":
    raise SystemExit(main())
