#!/usr/bin/env python3
"""Native macOS Keyframes regression using an isolated temporary project.

Example:
    QT_QPA_PLATFORM=cocoa .venv/bin/python scripts/validate_color_keyframes_media.py \
        tests/test_videos/测试素材01.mov --trim 12 --output /tmp/keyframes-qa

Requires the app's normal FFmpeg setup and a video with at least 60 visible frames.
No existing user project is opened or saved. JSON reports UI/project-model behavior;
real GL pixel parity is covered by tests/color/test_color_graph_texture.py.
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
    parser.add_argument("--output", type=Path, default=Path(tempfile.mkdtemp(prefix="topos-keyframes-report-")))
    parser.add_argument("--keep-open", action="store_true")
    parser.add_argument("--scrub-check", action="store_true", help="Check repeated graded seeks and current preview snapshots")
    parser.add_argument("--trim", type=int, default=0, help="Trim this many source frames from the start")
    parser.add_argument("--speed", type=float, default=1.0, help="Retimed playback speed")
    args = parser.parse_args()
    if args.trim < 0:
        parser.error("--trim must be nonnegative")
    if not 0 < args.speed <= 2:
        parser.error("--speed must be positive and at most 2 for this gesture fixture")
    source = args.media.resolve(strict=True)
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    config_dir = Path(tempfile.mkdtemp(prefix="topos-node-media-"))
    config_path = config_dir / "config.json"
    config_path.write_text(json.dumps({"locale": "en_US", "work_mode": "video", "current_page": "Edit" if args.scrub_check else "Color",
                                       "show_welcome_on_startup": False, "auto_load_last_project": False,
                                       "check_updates_on_startup": False, "auto_save_interval": 0,
                                       # 开发草稿与正式资源适配器互斥。
                                       "color_resource_ui_preview_mode": False}))
    os.environ["TOPOS_CONFIG_PATH"] = str(config_path)
    os.environ["TOPOS_PRODUCT_MODE"] = "studio"
    os.environ["TOPOS_TEST_MODE"] = "1"
    os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")

    import src.main as app_main
    from PyQt6.QtCore import QObject, QTimer
    from PyQt6.QtWidgets import QApplication
    from src.app.main_window import MainWindow

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
                "validation": "native_keyframes_ui_and_project",
                "per_node_rendering": "not_checked",
            }
            self.timer = QTimer(self)
            self.timer.setInterval(200)
            self.timer.timeout.connect(self.tick)
            self.timer.start()

        def finish(self, success: bool, error: str = "") -> None:
            self.timer.stop()
            if success:
                self.host.color_keyframes_panel.grab().save(str(output / "keyframes_panel.png"))
                self.report["screenshot"] = "keyframes_panel.png"
            self.report.update(status="passed" if success else "failed", error=error,
                               elapsed_seconds=round(time.monotonic() - self.started, 2))
            (output / "keyframes_media_report.json").write_text(json.dumps(self.report, ensure_ascii=False, indent=2) + "\n")
            print(json.dumps(self.report, ensure_ascii=False), flush=True)
            if not args.keep_open:
                app = QApplication.instance()
                app.exit(0 if success else 1)

        def tick(self) -> None:
            try:
                if time.monotonic() - self.started > 60:
                    raise TimeoutError(f"Keyframes validation did not complete in 60 seconds (phase={self.phase}, expected={getattr(self, 'scrub_expected', None)}, captures={list(getattr(self, 'scrub_frames', {}))})")
                if self.phase == "start":
                    print("phase: creating test project",flush=True)
                    self.host._auto_create_edit_project(name="Color Keyframes Verification")
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
                    if args.trim:
                        from uuid import uuid4
                        from src.features.edit.core.commands.clip_commands import TrimClipCommand
                        tv = self.host.timeline_panel.timeline_view
                        tv._execute_command(TrimClipCommand(
                            str(uuid4()), "Trim for keyframe QA", self.clip.track_id,
                            self.clip.clip_id, "left", new_timeline_start=args.trim,
                            new_timeline_duration=self.clip.timeline_duration - args.trim,
                            new_source_start=self.clip.source_start + args.trim,
                            new_source_duration=self.clip.source_duration - args.trim))
                        self.timeline = self.host._current_timeline_sequences[self.timeline.timeline_id]
                        self.clip = self.timeline.get_clip_by_id(self.clip.clip_id)
                    if args.speed != 1.0:
                        from uuid import uuid4
                        from src.features.edit.core.commands.clip_props_commands import ChangeClipSpeedCommand
                        self.host.timeline_panel.timeline_view._execute_command(ChangeClipSpeedCommand(
                            str(uuid4()), "Retime for keyframe QA", self.clip.clip_id,
                            self.clip.track_id, args.speed))
                        self.timeline = self.host._current_timeline_sequences[self.timeline.timeline_id]
                        self.clip = self.timeline.get_clip_by_id(self.clip.clip_id)
                    assert self.clip.source_duration >= 60, "Need at least 60 source video frames"
                    if args.scrub_check:
                        preview = self.host.timeline_preview_renderer
                        preview.set_project(self.host._current_edit_project)
                        preview.set_sequence(self.timeline)
                    assert self.host.open_timeline_clip_in_color(
                        self.timeline.timeline_id, self.clip.clip_id, self.clip.timeline_start)
                    print("phase: clip opened", flush=True)
                    self.phase = "preview"
                    return
                if self.phase == "preview":
                    from PyQt6.QtCore import QPoint, Qt
                    from PyQt6.QtTest import QTest
                    host = self.host
                    panel = host.color_keyframes_panel
                    kf_host = host._color_keyframes_host
                    assert kf_host is not None, "MainWindow did not attach Keyframes host"
                    assert not panel._demo, "production panel is in demo mode"
                    assert host.grading_panel.keyframe_mode_btn.isHidden(), "duplicate animation control is visible"
                    assert panel.snapshot.context, (host.current_page,
                        bool(host._active_color_timeline_clip_playback_context()),
                        bool(host._color_session_manager.get_current_session()),
                        bool(host.color_graph_host.bound_graph()))
                    if not panel.snapshot.corrector:
                        app_graph = host.color_graph_host.bound_graph()
                        accepted = host.color_graph_host.submit_panel_action("add_serial", {
                            "active_node_id": app_graph.source_node_id, "before": False})
                        assert accepted, "could not add Corrector to empty clip"
                        print("phase: Corrector added", flush=True)
                        host._color_keyframes_host_refresh(force=True)
                        assert panel.snapshot.corrector, panel.snapshot.tracks
                    dock = host.dock_color_keyframes
                    if dock.isHidden():
                        dock.toggleViewAction().trigger()
                    assert not dock.isHidden(), "menu did not show Keyframes"
                    if panel.timeline.width() < 140:
                        return
                    seek_frame = min(24, panel.snapshot.frame_count - 1)
                    timeline = panel.timeline
                    x = timeline.frame_x(seek_frame)
                    QTest.mouseClick(timeline, Qt.MouseButton.LeftButton, pos=QPoint(x, 8))
                    frame = panel.playhead
                    assert frame > 0, frame
                    corrector = panel.snapshot.corrector.removeprefix("node:")
                    panel.mode.setCurrentIndex(panel.mode.findData("color"))
                    QTest.keyClick(timeline, Qt.Key.Key_BracketRight, Qt.KeyboardModifier.ControlModifier)
                    graph = host.color_graph_host.bound_graph()
                    node = next(n for n in graph.nodes if n.node_id == corrector)
                    written = node.params["grade_keyframes"]["exposure"]["keyframes"]
                    source_frame = kf_host._source_frame(frame)
                    assert any(k["frame"] == source_frame and k["interpolation"] == "hold" for k in written)
                    assert (f"node:{corrector}|exposure", frame) in panel._markers
                    QTest.mouseClick(timeline, Qt.MouseButton.LeftButton,
                        pos=QPoint(timeline.frame_x(47),8))
                    dynamic_frame = panel.playhead
                    QTest.keyClick(timeline, Qt.Key.Key_BracketLeft, Qt.KeyboardModifier.ControlModifier)
                    leaf = f"node:{corrector}|exposure"
                    assert panel._markers[leaf, dynamic_frame].kind == "dynamic"
                    row_y = next(y + 12 for row, _, y in timeline._visible_rows()
                                 if row == panel.snapshot.corrector)
                    dest = dynamic_frame + 6
                    QTest.mousePress(timeline, Qt.MouseButton.LeftButton,
                        pos=QPoint(timeline.frame_x(dynamic_frame),row_y))
                    QTest.mouseMove(timeline,QPoint(timeline.frame_x(dest),row_y))
                    QTest.mouseRelease(timeline, Qt.MouseButton.LeftButton,
                        pos=QPoint(timeline.frame_x(dest),row_y))
                    moved = next(f for t, f in panel._markers if t == leaf and f != frame)
                    assert abs(moved-dest) <= 1 and moved != dynamic_frame
                    assert (leaf,moved) in panel.selected
                    QTest.mouseClick(timeline, Qt.MouseButton.LeftButton,pos=QPoint(28,row_y))
                    assert panel._tracks[panel.snapshot.corrector].locked
                    QTest.keyClick(timeline, Qt.Key.Key_Delete)
                    assert (leaf,moved) in panel._markers
                    QTest.mouseClick(timeline, Qt.MouseButton.LeftButton,pos=QPoint(28,row_y))
                    QTest.keyClick(timeline, Qt.Key.Key_Delete)
                    assert (leaf,moved) not in panel._markers
                    assert (leaf,frame) in panel._markers
                    graph = host.color_graph_host.bound_graph()
                    self.report["validation"] = "macOS Cocoa MainWindow with imported media and project-backed Keyframes"
                    self.report["keyframes"] = {"context": bool(panel.snapshot.context),
                        "static_panel_frame": frame, "static_source_frame": source_frame,
                        "ruler_start": panel.timeline._timecode(0), "speed": args.speed,
                        "transport_frame": kf_host._frame_map.panel_to_local(frame),
                        "dynamic_panel_frame": dynamic_frame, "moved_to_panel_frame": moved,
                        "lock_prevented_delete": True, "unlocked_delete": True,
                        "fps": float(panel.snapshot.timecode_fps), "marker_count": len(panel._markers),
                        "graph_revision": graph.graph_revision}
                    self.phase = "record"
                    return
                if self.phase == "record":
                    from PyQt6.QtCore import QPoint, Qt
                    from PyQt6.QtTest import QTest
                    host = self.host
                    if host.engine.get_current_image() is None:
                        return
                    graph_host = host.color_graph_host
                    corrector = host.color_keyframes_panel.snapshot.corrector.removeprefix("node:")
                    graph_host.panel_host._on_active_node_changed(corrector)
                    host._color_keyframes_host_refresh(force=True)
                    panel = host.color_keyframes_panel
                    corrector = panel.snapshot.corrector.removeprefix("node:")
                    row_y = next(y + 12 for row, _, y in panel.timeline._visible_rows()
                                 if row == panel.snapshot.corrector)
                    QTest.mouseClick(panel.timeline, Qt.MouseButton.LeftButton, pos=QPoint(44, row_y))
                    assert panel._tracks[panel.snapshot.corrector].auto
                    depth = len(graph_host.bridge._project_stack._undo_stack)
                    frame = host._color_last_source_frame
                    control = host.grading_panel.temp_slider.spin
                    before = control.value()
                    before_temp_track = next(n for n in graph_host.bound_graph().nodes if n.node_id == corrector).params.get("grade_keyframes", {}).get("temp")
                    QTest.mousePress(control, Qt.MouseButton.LeftButton, pos=QPoint(10, 10))
                    QTest.mouseMove(control, QPoint(30, 10))
                    QTest.qWait(20)
                    QTest.mouseRelease(control, Qt.MouseButton.LeftButton, pos=QPoint(30, 10))
                    assert control.value() != before, ("temperature drag did not change the control", control.isEnabled())
                    node = next(n for n in graph_host.bound_graph().nodes if n.node_id == corrector)
                    keys = node.params["grade_keyframes"]["temp"]["keyframes"]
                    assert any(key["frame"] == frame and key["value"] == control.value() for key in keys)
                    assert len(graph_host.bridge._project_stack._undo_stack) == depth + 1
                    assert graph_host.undo(), "recorded gesture could not undo"
                    restored = next(n for n in graph_host.bound_graph().nodes if n.node_id == corrector)
                    assert restored.params.get("grade_keyframes", {}).get("temp") == before_temp_track
                    self.report["auto_record"] = {"selected_corrector": True,
                        "source_frame": frame, "single_history_entry": True, "undo_restored": True}
                    if not args.scrub_check:
                        self.finish(True)
                        return
                    renderer = (getattr(host, "_color_timeline_preview_renderer", None)
                                or getattr(host, "timeline_preview_renderer", None))
                    assert renderer is not None, "Timeline preview was not created"
                    self.scrub_preview_renderer = renderer
                    print("phase: scrub setup", {"gpu": getattr(renderer, "_gpu_renderer", None) is not None,
                          "sequence": getattr(getattr(renderer, "_sequence", None), "timeline_id", None)}, flush=True)
                    assert renderer._project is host._current_edit_project, "preview still holds an older project snapshot"
                    start = self.report["keyframes"]["static_source_frame"]
                    end = self.clip.source_start + self.clip.source_duration - 1
                    assert graph_host.submit_keyframe_batch([
                        {"action": "set", "node_id": corrector, "param": "exposure", "frame": start,
                         "value": 0.0, "kind": "dynamic"},
                        {"action": "set", "node_id": corrector, "param": "exposure", "frame": end,
                         "value": 30.0, "kind": "dynamic"}])
                    assert renderer._project is host._current_edit_project
                    self.scrub_frames: dict[int, tuple[int, bytes, float]] = {}
                    self.scrub_errors: list[str] = []
                    self.scrub_last_capture = 0.0
                    self.scrub_results: list[dict[str, Any]] = []
                    self.scrub_hashes: dict[int, str] = {}
                    self.scrub_gpu_renderer = getattr(renderer, "_gpu_renderer", None)
                    if self.scrub_gpu_renderer is not None:
                        self.scrub_gpu_renderer.snapshot_ready.connect(
                            lambda frame, generation, image: self.capture_scrub_frame(frame, image))
                        self.scrub_gpu_renderer.render_error.connect(
                            lambda frame, message: self.scrub_errors.append(f"GPU {frame}: {message}"))
                    else:
                        renderer.frame_ready.connect(self.capture_scrub_frame)
                    renderer.render_error.connect(lambda frame, message: self.scrub_errors.append(f"{frame}: {message}"))
                    span = panel.snapshot.frame_count - 1
                    self.scrub_positions = [0, span // 3, 2 * span // 3, span, 2 * span // 3, span // 3, 0]
                    self.phase = "scrub_request"
                    return
                if self.phase == "scrub_request":
                    from PyQt6.QtCore import QPoint, Qt
                    from PyQt6.QtTest import QTest
                    panel = self.host.color_keyframes_panel
                    position = self.scrub_positions.pop(0)
                    local = self.host._color_keyframes_host._frame_map.panel_to_local(position)
                    self.scrub_expected = self.clip.timeline_start + local
                    print("phase: scrub request", self.scrub_expected, flush=True)
                    self.scrub_before = self.scrub_frames.get(self.scrub_expected, (0, b"", 0.0))[0]
                    self.phase = "scrub_wait"
                    preview = self.scrub_preview_renderer
                    preview.invalidate_composite_cache()
                    if self.scrub_gpu_renderer is not None:
                        self.scrub_gpu_renderer.arm_frame_snapshot()
                    QTest.mouseClick(panel.timeline, Qt.MouseButton.LeftButton,
                                     pos=QPoint(panel.timeline.frame_x(position), 8))
                    preview.request_render(self.scrub_expected)
                    return
                if self.phase == "scrub_wait":
                    assert not self.scrub_errors, self.scrub_errors
                    captured = self.scrub_frames.get(self.scrub_expected)
                    if captured is None or captured[0] <= self.scrub_before:
                        return
                    renderer = self.scrub_preview_renderer
                    if renderer._render_in_flight or time.monotonic() - self.scrub_last_capture < 0.15:
                        return
                    _count, pixels, mean = captured
                    digest = hashlib.sha256(pixels).hexdigest()
                    previous = self.scrub_hashes.get(self.scrub_expected)
                    assert previous is None or previous == digest, "same frame produced alternating graded pixels"
                    self.scrub_hashes[self.scrub_expected] = digest
                    self.scrub_results.append({"timeline_frame": self.scrub_expected, "sha256": digest, "mean": mean})
                    if self.scrub_positions:
                        self.phase = "scrub_request"
                        return
                    from src.app.controllers.color_graph_controller import graph_payload_to_draft
                    graph = self.host.color_graph_host.bound_graph()
                    draft = graph_payload_to_draft(graph.to_dict())
                    assert all(n.label != n.id for n in draft.nodes.values() if n.kind.value == "corrector")
                    self.report["scrub"] = {"current_project_snapshot": True,
                        "repeat_pixels_identical": True, "errors": self.scrub_errors, "frames": self.scrub_results}
                    self.report["per_node_rendering"] = ("Timeline GPU snapshot" if self.scrub_gpu_renderer is not None
                                                         else "Timeline CPU image") + "; repeated graded-frame pixels checked"
                    self.finish(True)
                    return

            except (StopIteration, KeyError, RuntimeError, ValueError, TypeError, AttributeError, IndexError, OSError, AssertionError) as exc:
                import traceback
                traceback.print_exc()
                self.finish(False, f"{type(exc).__name__}: {exc}")

        def capture_scrub_frame(self, frame: int, result: Any) -> None:
            from PyQt6.QtGui import QImage
            if not isinstance(result, QImage) or result.isNull():
                return
            if int(frame) != getattr(self, "scrub_expected", int(frame)):
                return
            import numpy as np
            converted = result.convertToFormat(QImage.Format.Format_RGBA8888)
            pixels = converted.constBits().asstring(converted.sizeInBytes())
            mean = float(np.frombuffer(pixels, dtype=np.uint8).reshape(converted.height(), converted.width(), 4)[..., :3].mean())
            count = self.scrub_frames.get(int(frame), (0, b"", 0.0))[0] + 1
            self.scrub_frames[int(frame)] = (count, pixels, mean)
            self.scrub_last_capture = time.monotonic()

    original_init = MainWindow.__init__

    def validation_init(self: Any, *init_args: Any, **kwargs: Any) -> None:
        print("constructing MainWindow",flush=True)
        original_init(self, *init_args, **kwargs)
        print("MainWindow ready",flush=True)
        self._node_validation_driver = Driver(self)

    MainWindow.__init__ = validation_init
    sys.argv = [sys.argv[0], "--product", "studio", "--no-startup-dialog"]
    return app_main.main()


if __name__ == "__main__":
    raise SystemExit(main())
