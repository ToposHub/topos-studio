#!/usr/bin/env python3
"""Verify clip versions in the real MainWindow with a disposable video timeline.

Usage: .venv/bin/python tests/benchmark/drive_real_app_clip_versions.py <video>
Uses an isolated temporary config unless TOPOS_CONFIG_PATH is supplied.
The input video is read only. No project is saved. TOPOS_VERSION_AUDIT_CAPTURE
can select the output screenshot path. Run on a desktop, not Qt offscreen.
"""

from __future__ import annotations

import os
import sys
import time
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
os.environ.setdefault("TOPOS_PRODUCT_MODE", "studio")
os.environ["TOPOS_TEST_MODE"] = "1"
os.environ["TOPOS_FEATURE_EDIT"] = "1"
_CONFIG_DIR = tempfile.TemporaryDirectory(prefix="topos-version-audit-")
if not os.environ.get("TOPOS_CONFIG_PATH"):
    os.environ["TOPOS_CONFIG_PATH"] = str(Path(_CONFIG_DIR.name) / "config.json")

VIDEO = Path(next(a for a in sys.argv[1:] if not a.startswith("--"))).resolve()

FAILURES: list[str] = []


def log(msg: str) -> None:
    print(f"[STRIPDIAG] {msg}", flush=True)


def check(name: str, ok: bool, detail: str = "") -> None:
    tag = "PASS" if ok else "FAIL"
    log(f"{tag} {name} {detail}")
    if not ok:
        FAILURES.append(name)


def main() -> int:
    from PyQt6.QtCore import Qt, QTimer
    from PyQt6.QtGui import QSurfaceFormat
    from PyQt6.QtWidgets import QApplication, QDialog

    fmt = QSurfaceFormat()
    fmt.setVersion(4, 5)
    fmt.setProfile(QSurfaceFormat.OpenGLContextProfile.CoreProfile)
    QSurfaceFormat.setDefaultFormat(fmt)
    QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts, True)
    app = QApplication(sys.argv)

    from src.shared.config.config_manager import ConfigManager
    from src.shared.feature_flags import load_feature_flags
    from src.app.feature_registry import FeatureRegistry
    from src.app.app_context import AppContext
    from src.shared.i18n.i18n import I18N
    from src.app.main_window import MainWindow

    cm = ConfigManager()
    ff = load_feature_flags(cm)
    I18N.load_locale("zh_CN")
    reg = FeatureRegistry()
    reg.initialize(AppContext(config_manager=cm, feature_flags=ff, i18n=I18N, logger=None))
    from src.features.color.module import ColorModule
    reg.register_module(ColorModule())
    from src.features.edit.module import EditModule
    reg.register_module(EditModule())

    def _auto_dismiss_dialogs() -> None:
        for w in QApplication.topLevelWidgets():
            if isinstance(w, QDialog) and w.isVisible():
                log(f"auto-dismiss dialog: {type(w).__name__}")
                w.reject()

    _dialog_killer = QTimer()
    _dialog_killer.timeout.connect(_auto_dismiss_dialogs)
    _dialog_killer.start(200)

    mw = MainWindow(feature_flags=ff, feature_registry=reg, test_mode=False)
    mw.show()
    for _ in range(60):
        app.processEvents()

    # ---- import + two same-source clips (non-contiguous ranges) ----
    mw.media_pool_panel._import_files([str(VIDEO)])
    t0 = time.time()
    asset = None
    while time.time() - t0 < 60:
        app.processEvents()
        time.sleep(0.02)
        p = getattr(mw, "_current_edit_project", None)
        if p is not None:
            asset = p.get_asset_by_path(str(VIDEO))
            if asset is not None:
                break
    assert asset, "import failed"
    tv = mw.timeline_panel.timeline_view
    tv._ensure_first_asset_timeline_settings(asset)
    for _ in range(20):
        app.processEvents()
    tl = mw.edit_page._current_timeline or tv._create_default_timeline(asset)
    t0 = time.time()
    while time.time() - t0 < 20:
        app.processEvents()
        if mw.timeline_preview_renderer:
            break
        time.sleep(0.02)

    # Build the user's timeline shape: two scene-cut sub-clips of the SAME
    # source file with a gap between their source ranges (non-contiguous),
    # i.e. exactly what scene detection produces and what takes the
    # wrap-advance path on playback.
    from src.features.edit.core.scene_cut.models import SceneCutKind, SceneSubclipSpec
    from src.features.edit.core.scene_cut.subclips import create_subclip_asset

    fps = float(tl.fps) if not hasattr(tl.fps, "num") else (
        tl.fps.num / tl.fps.den
    )

    def _spec(scene_index: int, start: int, dur: int) -> SceneSubclipSpec:
        return SceneSubclipSpec(
            parent_asset_id=asset.asset_id,
            source_path=str(VIDEO),
            scene_index=scene_index,
            source_start=start,
            source_duration=dur,
            start_seconds=start / fps,
            duration_seconds=dur / fps,
            display_name=f"scene {scene_index}",
            start_timecode=f"00:00:{start // int(fps):02d}:{start % int(fps):02d}",
            end_timecode=f"00:00:{(start + dur) // int(fps):02d}:{(start + dur) % int(fps):02d}",
            thumbnail_source_frame=start,
            detection_confidence=1.0,
            detection_kind=SceneCutKind.HARD_CUT,
        )

    short = 24
    sub_a = create_subclip_asset(_spec(1, 0, short), "driver-analysis", asset)
    sub_b = create_subclip_asset(_spec(2, short * 2, short), "driver-analysis", asset)
    project = mw._current_edit_project
    project.assets[sub_a.asset_id] = sub_a
    project.assets[sub_b.asset_id] = sub_b
    tv._add_asset_as_clip(sub_a, tl.video_tracks[0], 0)
    tv._add_asset_as_clip(sub_b, tl.video_tracks[0], short)
    t0 = time.time()
    while time.time() - t0 < 10:
        app.processEvents()
        time.sleep(0.02)
        tl = mw.edit_page._current_timeline or tl
        if len(tl.video_tracks[0].clips) >= 2:
            break

    clip_a, clip_b = tl.video_tracks[0].clips[0], tl.video_tracks[0].clips[1]
    log(f"timeline ready: A={clip_a.source_start}+{clip_a.source_duration} "
        f"B={clip_b.source_start}+{clip_b.source_duration} same_path="
        f"{project.assets[clip_a.asset_id].path == project.assets[clip_b.asset_id].path}")

    mw.open_timeline_clip_in_color(tl.timeline_id, clip_a.clip_id, 0)
    t0 = time.time()
    while time.time() - t0 < 30:
        app.processEvents()
        time.sleep(0.02)
        if getattr(mw, "current_page", "") == "Color" and getattr(mw, "video_controller", None):
            if mw.video_controller.is_video_loaded():
                break
    vc = mw.video_controller
    strip = mw.color_timeline_clip_strip
    log(f"color page ready loaded={vc.is_video_loaded()} cards={len(strip._cards)}")

    try:
        from src.features.color.ui.clip_version_ui import VersionScope as Scope, VersionOperation as Op
        from src.features.edit.core.color_versions import versions_state
        host = mw._clip_version_request_host
        outcomes = []
        strip.version_ui.operation_finished.connect(outcomes.append)
        def drive(op: Op, scope: Scope = Scope.LOCAL, **kwargs: object) -> None:
            entry = next(e for e in strip._all_entries if e.clip_id == clip_a.clip_id)
            target = strip._version_target(entry)
            host.read_versions(target, scope)
            outcomes.clear()
            token = strip.version_ui.submit(target, scope, op, **kwargs)
            assert token and outcomes, (op, token, strip.version_ui.last_error)
            assert not outcomes[-1].error, outcomes[-1].error
            for _ in range(12):
                app.processEvents()
            log(f"PASS real version {op.value} {scope.value}")
        drive(Op.CREATE, name="Audit A")
        mw.grading_panel.exp_slider.setValue(1.25)
        drive(Op.CREATE, name="Audit Bright")
        assert mw.engine.layers[0].grading_params["exposure"] == 1.25
        drive(Op.LOAD, version_id="Audit A", discard_unsaved=True)
        assert mw.engine.layers[0].grading_params["exposure"] == 0.0
        drive(Op.LOAD, version_id="Audit Bright", discard_unsaved=True)
        assert mw.engine.layers[0].grading_params["exposure"] == 1.25
        log("PASS real last-gesture snapshot and different grade restoration")
        drive(Op.DUPLICATE, version_id="Audit A", name="Audit B")
        drive(Op.RENAME, version_id="Audit B", name="Audit C")
        entry = next(e for e in strip._all_entries if e.clip_id == clip_a.clip_id)
        host.read_versions(strip._version_target(entry), Scope.REMOTE)
        drive(Op.COPY_TO_SCOPE, version_id="Audit C", name="Audit Remote", destination_scope=Scope.REMOTE)
        drive(Op.LOAD, Scope.REMOTE, version_id="Audit Remote", discard_unsaved=True)
        session = mw._color_session_manager.get_current_session()
        remote = session.color_stack_id
        assert mw._grading_save_stack_id == remote == mw.grading_panel.current_stack_id
        assert mw._page_context_manager.get_current_context().color_stack_id == remote
        mw.undo()
        for _ in range(12): app.processEvents()
        session = mw._color_session_manager.get_current_session()
        assert session.color_stack_id != remote
        assert mw._grading_save_stack_id == session.color_stack_id
        mw.redo()
        for _ in range(12): app.processEvents()
        assert mw._color_session_manager.get_current_session().color_stack_id == remote
        log("PASS real global undo/redo reference + save callback + session")
        capture = Path(os.environ.get("TOPOS_VERSION_AUDIT_CAPTURE", str(Path(tempfile.gettempdir()) / "topos-version-audit-real.png")))
        capture.parent.mkdir(parents=True, exist_ok=True)
        mw.grab().save(str(capture))
        mw.grading_panel.exp_slider.setValue(0.75)
        drive(Op.CREATE, Scope.REMOTE, name="After Remote Edit")
        assert mw.engine.layers[0].grading_params["exposure"] == 0.75
        assert mw._current_edit_project.color_stacks[remote].grading_params["exposure"] == 0.75
        log("PASS real edit after cross-scope load saves the new stack")
        log("RESULT: ALL REAL APP VERSION CHECKS PASSED")
        return 0
    finally:
        mw.edit_project_dirty = False
        mw.color_session_dirty = False
        mw._current_edit_project_filepath = None
        mw.current_file_path = None
        mw._confirm_save_before_close = lambda: True
        mw.close()
        mw.deleteLater()
        QTimer.singleShot(250, app.quit)
        app.exec()

if __name__ == "__main__":
    sys.exit(main())
