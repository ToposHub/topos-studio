#!/usr/bin/env python3
"""Drive the REAL app: measure true viewer update rate during playhead drags
and watch resource growth over a long editing session (scrub + split + seek).

Measures at the ProgramViewer boundary (what the user actually sees), not the
renderer boundary. Samples EditGpuDecodeManager source/pool growth, renderer
caches, and process RSS over time to expose the "slower the longer it runs"
degradation.

A real app window appears during the run. stdout captured by the caller.
"""

from __future__ import annotations

import os
import sys
import time
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

# Same config isolation main.py applies for source checkouts.
if not os.environ.get("TOPOS_CONFIG_PATH"):
    os.environ["TOPOS_CONFIG_PATH"] = str(REPO_ROOT / ".topos" / "config.json")

os.environ.setdefault("TOPOS_PRODUCT_MODE", "studio")
os.environ["TOPOS_TEST_MODE"] = "1"
os.environ["TOPOS_EDIT_PLAYBACK_TRACE"] = "full"
# work_mode=image 的覆盖会禁用 edit 功能页，驱动需要完整编辑页。
os.environ["TOPOS_FEATURE_EDIT"] = "1"

VIDEO = Path(sys.argv[1] if len(sys.argv) > 1 else REPO_ROOT / "tests/test_videos/4K_prores422HQ.mov").resolve()
MINUTES = float(sys.argv[2]) if len(sys.argv) > 2 else 1.5


def log(msg: str) -> None:
    print(f"[DRIVER] {msg}", flush=True)


def rss_mb() -> float:
    try:
        import psutil
        return psutil.Process().memory_info().rss / 1024 / 1024
    except Exception:
        return 0.0


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

    # Headless guard: startup restore can pop a modal dialog (WelcomeDialog)
    # whose exec() blocks the first processEvents() call.
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
    for _ in range(90):
        app.processEvents()
    log(f"booted page={mw.current_page} rss={rss_mb():.0f}MB")

    # import media
    mw.media_pool_panel._import_files([str(VIDEO)])
    deadline = time.time() + 60
    while time.time() < deadline:
        app.processEvents()
        project = getattr(mw, "_current_edit_project", None)
        if project is not None and project.get_asset_by_path(str(VIDEO)) is not None:
            break
        time.sleep(0.02)
    asset = mw._current_edit_project.get_asset_by_path(str(VIDEO))
    log(f"asset imported fps={asset.metadata.get('fps')} frames={asset.metadata.get('total_frames')}")

    tv = mw.timeline_panel.timeline_view
    tv._ensure_first_asset_timeline_settings(asset)
    for _ in range(20):
        app.processEvents()
    timeline = mw.edit_page._current_timeline or tv._create_default_timeline(asset)
    tv._add_asset_as_clip(asset, timeline.video_tracks[0], 0)
    deadline = time.time() + 20
    while time.time() < deadline:
        app.processEvents()
        if mw.timeline_preview_renderer is not None:
            break
        time.sleep(0.02)
    renderer = mw.timeline_preview_renderer
    assert renderer is not None
    timeline = mw.edit_page._current_timeline
    total = max(c.timeline_end for c in timeline.video_tracks[0].clips)
    log(f"timeline total={total} fps={timeline.fps} rss={rss_mb():.0f}MB")

    ep = mw.edit_page

    def scrub_velocity(step: int, cadence_ms: float, sweeps: int) -> dict:
        """Drag the playhead through the REAL widget/signal chain."""
        frames = []
        for s in range(sweeps):
            rng = range(0, total, step) if s % 2 == 0 else range(total - 1, -1, -step)
            frames.extend(rng)
        emissions: list[tuple[float, int]] = []

        def on_ready(frame, result):
            if result is not None:
                emissions.append((time.perf_counter(), int(frame)))

        renderer.frame_ready.connect(on_ready)
        t0 = time.perf_counter()
        ep._on_scrub_started(frames[0])
        next_at = time.perf_counter()
        active_end = None
        for f in frames:
            now = time.perf_counter()
            while now < next_at:
                time.sleep(min(0.002, max(0.0, next_at - now)))
                app.processEvents()
                now = time.perf_counter()
            tv.set_playhead(f)
            ep._on_scrub_moved(f)
            app.processEvents()
            next_at = time.perf_counter() + cadence_ms / 1000.0
        active_end = time.perf_counter()
        ep._on_scrub_finished(frames[-1])
        drain_deadline = time.perf_counter() + 0.6
        while time.perf_counter() < drain_deadline:
            app.processEvents()
            time.sleep(0.004)
        renderer.frame_ready.disconnect(on_ready)
        active_s = max(1e-6, active_end - t0)
        real = [(t, f) for t, f in emissions if t <= active_end]
        distinct = len({f for _, f in real})
        transitions = sum(1 for i in range(1, len(real)) if real[i][1] != real[i - 1][1])
        return {
            "step": step, "cadence_ms": cadence_ms, "inputs": len(frames),
            "active_s": round(active_s, 2),
            "emissions_active": len(real),
            "active_ups": round(len(real) / active_s, 1),
            "transitions": transitions,
            "distinct": distinct,
        }

    def resource_snapshot() -> dict:
        r = mw.timeline_preview_renderer
        worker = getattr(r, "_worker", None)
        dm = getattr(worker, "_gpu_decode_manager", None) if worker else None
        snap = {"rss_mb": round(rss_mb(), 0)}
        if dm is not None:
            with dm._lock:
                snap["sources"] = len(dm._source_cache)
                snap["pool_keys"] = len(dm._interactive_source_pool)
                snap["pool_sources"] = sum(len(v) for v in dm._interactive_source_pool.values())
                snap["warmups"] = len(getattr(dm, "_warmup_events", {}) or {})
        snap["req_latency_map"] = len(getattr(r, "_scrub_request_started_by_frame", {}) or {})
        fp = getattr(r, "_frame_provider", None)
        if fp is not None:
            snaps = getattr(fp, "_clips", None)
            snap["fp_sessions"] = len(snaps) if snaps is not None else -1
        return snap

    results = {"velocity": [], "resources": []}
    # ---- velocity sweep (fresh app) ----
    for step, cad in [(1, 60), (1, 20), (3, 10), (15, 8), (40, 8)]:
        r = scrub_velocity(step, cad, 3)
        results["velocity"].append(r)
        log(f"VELOCITY {r}")
        results["resources"].append({"after": f"vel_{step}_{cad}", **resource_snapshot()})
        log(f"RES {results['resources'][-1]}")

    # ---- long-session stress: scrub + split + zoom-ish seeks ----
    end_at = time.time() + MINUTES * 60
    rounds = 0
    while time.time() < end_at:
        scrub_velocity(15, 8, 2)
        # random-ish seeks (zoom-like jumps)
        for f in (0, total // 3, total // 2, total - 1, total // 4):
            tv.set_playhead(f)
            ep._on_scrub_started(f)
            ep._on_scrub_finished(f)
            app.processEvents()
        rounds += 1
        if rounds % 5 == 0:
            results["resources"].append({"after": f"stress_r{rounds}", **resource_snapshot()})
            log(f"RES {results['resources'][-1]}")
    # ---- velocity again after stress (degradation check) ----
    for step, cad in [(3, 10), (15, 8)]:
        r = scrub_velocity(step, cad, 3)
        results["velocity"].append({**r, "phase": "after_stress"})
        log(f"VELOCITY-AFTER {r}")

    log("FINAL " + repr(results))
    try:
        mw.close()
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
