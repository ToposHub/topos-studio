#!/usr/bin/env python3
"""Drive the REAL Topos app: import real footage, add to timeline, scrub,
blade-split, scrub again. Measures actual viewer frame updates in the real
environment (all UI layers, thumbnails, waveform, page controllers active).

Run as a subprocess; stdout (incl. TOPOS_EDIT_PLAYBACK_TRACE=full lines) is
captured by the caller. A real app window appears during the run.
"""

from __future__ import annotations

import os
import sys
import time
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("TOPOS_PRODUCT_MODE", "studio")
os.environ["TOPOS_TEST_MODE"] = "1"
os.environ["TOPOS_EDIT_PLAYBACK_TRACE"] = "full"
# work_mode=image 的覆盖会禁用 edit 功能页，驱动需要完整编辑页。
os.environ["TOPOS_FEATURE_EDIT"] = "1"

VIDEO = Path(sys.argv[1] if len(sys.argv) > 1 else REPO_ROOT / "tests/test_videos/2K_h264.mp4").resolve()
SPLIT_AT = [int(x) for x in (sys.argv[2].split(",") if len(sys.argv) > 2 else ["60", "90", "120"])]
CADENCE_MS = float(sys.argv[3]) if len(sys.argv) > 3 else 10.0


def log(msg: str) -> None:
    print(f"[DRIVER] {msg}", flush=True)


def main() -> int:
    from PyQt6.QtCore import Qt, QTimer
    from PyQt6.QtGui import QSurfaceFormat
    from PyQt6.QtWidgets import QApplication

    os.environ["QSG_RHI_BACKEND"] = "opengl"
    fmt = QSurfaceFormat()
    fmt.setVersion(4, 5)
    fmt.setProfile(QSurfaceFormat.OpenGLContextProfile.CoreProfile)
    fmt.setRenderableType(QSurfaceFormat.RenderableType.OpenGL)
    fmt.setRedBufferSize(8); fmt.setGreenBufferSize(8); fmt.setBlueBufferSize(8); fmt.setAlphaBufferSize(8)
    fmt.setDepthBufferSize(24); fmt.setStencilBufferSize(8)
    if sys.platform != "win32":
        fmt.setSamples(4)
    fmt.setSwapBehavior(QSurfaceFormat.SwapBehavior.DoubleBuffer)
    QSurfaceFormat.setDefaultFormat(fmt)
    QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts, True)
    app = QApplication(sys.argv)

    # Headless guard: startup restore can pop a modal dialog whose exec()
    # blocks the first processEvents() call.
    from PyQt6.QtWidgets import QDialog

    def _auto_dismiss_dialogs():
        for w in QApplication.topLevelWidgets():
            if isinstance(w, QDialog) and w.isVisible():
                log(f"auto-dismiss dialog: {type(w).__name__}")
                w.reject()

    _dialog_killer = QTimer()
    _dialog_killer.timeout.connect(_auto_dismiss_dialogs)
    _dialog_killer.start(200)

    try:
        from PyQt6.QtWebEngineWidgets import QWebEngineView  # noqa: F401
    except ImportError:
        pass

    from src.shared.config.config_manager import ConfigManager
    from src.shared.feature_flags import load_feature_flags
    from src.app.feature_registry import FeatureRegistry
    from src.app.app_context import AppContext
    from src.shared.i18n.i18n import I18N
    from src.app.main_window import MainWindow

    config_manager = ConfigManager()
    feature_flags = load_feature_flags(config_manager)
    I18N.load_locale("zh_CN")
    registry = FeatureRegistry()
    registry.initialize(AppContext(config_manager=config_manager, feature_flags=feature_flags, i18n=I18N, logger=None))
    from src.features.color.module import ColorModule
    registry.register_module(ColorModule())
    if feature_flags.is_enabled("composite"):
        from src.features.composite.module import CompositeModule
        m = CompositeModule(); registry.register_module(m); m.discover_and_register_plugins()
    if feature_flags.is_enabled("edit"):
        from src.features.edit.module import EditModule
        registry.register_module(EditModule())

    mw = MainWindow(feature_flags=feature_flags, feature_registry=registry, test_mode=False)
    mw.show()
    for _ in range(60):
        app.processEvents()
    log(f"app booted, page={getattr(mw, 'current_page', '?')}")

    # ---- import media (real async path) ----
    mw.media_pool_panel._import_files([str(VIDEO)])
    deadline = time.time() + 60
    project = None
    while time.time() < deadline:
        app.processEvents()
        project = getattr(mw, "_current_edit_project", None)
        if project is not None and project.get_asset_by_path(str(VIDEO)) is not None:
            break
        time.sleep(0.05)
    assert project is not None and project.get_asset_by_path(str(VIDEO)), "import failed"
    asset = project.get_asset_by_path(str(VIDEO))
    log(f"asset imported: {asset.asset_id} fps={asset.metadata.get('fps')} frames={asset.metadata.get('total_frames')}")

    # ---- add to timeline (real command path) ----
    tv = mw.timeline_panel.timeline_view
    tv._ensure_first_asset_timeline_settings(asset)
    for _ in range(20):
        app.processEvents()
    timeline = mw.edit_page._current_timeline or getattr(mw, "_current_edit_timeline", None)
    if timeline is None:
        # create default timeline through the view
        tl = tv._create_default_timeline(asset)
        timeline = tl if tl is not None else mw.edit_page._current_timeline
    assert timeline is not None, "no timeline"
    track = timeline.video_tracks[0]
    ok = tv._add_asset_as_clip(asset, track, 0)
    log(f"clip added: {ok}")
    deadline = time.time() + 20
    while time.time() < deadline:
        app.processEvents()
        if mw.timeline_preview_renderer is not None:
            break
        time.sleep(0.05)
    renderer = mw.timeline_preview_renderer
    assert renderer is not None, "renderer not created"

    timeline = mw.edit_page._current_timeline
    total = max(c.timeline_end for c in timeline.video_tracks[0].clips)
    log(f"timeline ready: total_frames={total} fps={timeline.fps}")

    # ---- frame accounting ----
    emissions: list[tuple[float, int, str]] = []

    def on_ready(frame, result):
        kind = "none" if result is None else (type(result).__name__)
        emissions.append((time.perf_counter(), int(frame), kind))

    renderer.frame_ready.connect(on_ready)

    def scrub(label: str, frames: list[int]) -> dict:
        emissions.clear()
        t0 = time.perf_counter()
        ep = mw.edit_page
        tv.set_playhead(frames[0])
        ep._on_scrub_started(frames[0])
        next_at = time.perf_counter()
        for f in frames:
            now = time.perf_counter()
            if now < next_at:
                time.sleep(next_at - now)
            tv.set_playhead(f)
            ep._on_scrub_moved(f)
            app.processEvents()
            next_at = time.perf_counter() + CADENCE_MS / 1000.0
        ep._on_scrub_finished(frames[-1])
        for _ in range(150):
            app.processEvents()
            time.sleep(0.008)
        dur = time.perf_counter() - t0
        real = [(t, f, k) for t, f, k in emissions if k != "none"]
        distinct = len({f for _, f, _ in real})
        transitions = sum(1 for i in range(1, len(real)) if real[i][1] != real[i - 1][1])
        distance = sum(abs(real[i][1] - real[i - 1][1]) for i in range(1, len(real)))
        kinds = Counter(k for _, _, k in real)
        stats = {
            "label": label, "requested": len(frames), "duration_s": round(dur, 2),
            "real_emissions": len(real), "none_emissions": len(emissions) - len(real),
            "distinct_frames": distinct, "picture_transitions": transitions,
            "frame_distance": distance,
            "updates_per_sec": round(len(real) / dur, 1),
            "transitions_per_sec": round(transitions / dur, 1),
            "kinds": dict(kinds),
        }
        log("RESULT " + repr(stats))
        return stats

    def drag_pattern(n: int, step: int, sweeps: int) -> list[int]:
        out = []
        for s in range(sweeps):
            rng = range(0, n, step) if s % 2 == 0 else range(n - 1, -1, -step)
            out.extend(rng)
        return out

    results = []
    # warmup (decode source open + first frames)
    scrub("warmup", drag_pattern(min(total, 60), 6, 1))
    # A: single clip
    results.append(scrub("single_clip", drag_pattern(total, 3, 3)))

    # ---- blade split (real command path) ----
    for cp in SPLIT_AT:
        if 0 < cp < total:
            tv._split_all_at_frame(cp)
            for _ in range(30):
                app.processEvents()
    timeline = mw.edit_page._current_timeline
    nseg = len(timeline.video_tracks[0].clips)
    log(f"split done: {nseg} segments")
    time.sleep(1.0)
    for _ in range(50):
        app.processEvents()

    # B: split segments
    results.append(scrub(f"split_{nseg}seg", drag_pattern(total, 3, 3)))

    try:
        renderer.frame_ready.disconnect(on_ready)
    except Exception:
        pass
    log("FINAL " + repr(results))

    # shutdown
    QTimer.singleShot(100, app.quit)
    app.exec() if False else None
    try:
        mw.close()
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
