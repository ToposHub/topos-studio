#!/usr/bin/env python3
"""Edit-page scrub freeze repro with REALISTIC event cadence.

Earlier drivers deliver scrub moves from a Python loop interleaved with heavy
processEvents() calls, which stretches inter-event gaps well past the 4ms
coalescing interval — a different regime from real mouse drags (60-120Hz
delivered by the OS event loop). This driver feeds moves from a QTimer at
mouse-like cadence and lets Qt dispatch them naturally.

Usage:
  .venv/bin/python tests/benchmark/drive_real_app_edit_scrub_realistic.py <video>
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

if not os.environ.get("TOPOS_CONFIG_PATH"):
    os.environ["TOPOS_CONFIG_PATH"] = str(REPO_ROOT / ".topos" / "config.json")
os.environ.setdefault("TOPOS_PRODUCT_MODE", "studio")
os.environ["TOPOS_TEST_MODE"] = "1"
os.environ["TOPOS_EDIT_PLAYBACK_TRACE"] = "full"
os.environ["TOPOS_FEATURE_EDIT"] = "1"

VIDEO = Path(sys.argv[1] if len(sys.argv) > 1 else REPO_ROOT / "tests/test_videos/4K_prores422HQ.mov").resolve()


def log(msg: str) -> None:
    print(f"[RDRIVER] {msg}", flush=True)


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

    from PyQt6.QtWidgets import QDialog

    def _auto_dismiss_dialogs():
        for w in QApplication.topLevelWidgets():
            if isinstance(w, QDialog) and w.isVisible():
                log(f"auto-dismiss dialog: {type(w).__name__}")
                w.reject()

    _dialog_killer = QTimer()
    _dialog_killer.timeout.connect(_auto_dismiss_dialogs)
    _dialog_killer.start(200)

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

    mw = MainWindow(feature_flags=ff, feature_registry=reg, test_mode=False)
    mw.show()
    for _ in range(90):
        app.processEvents()
    log(f"booted page={mw.current_page}")

    mw.media_pool_panel._import_files([str(VIDEO)])
    t0 = time.time()
    while time.time() - t0 < 60:
        app.processEvents()
        p = getattr(mw, "_current_edit_project", None)
        if p is not None and p.get_asset_by_path(str(VIDEO)) is not None:
            break
        time.sleep(0.02)
    asset = mw._current_edit_project.get_asset_by_path(str(VIDEO))
    tv = mw.timeline_panel.timeline_view
    tv._ensure_first_asset_timeline_settings(asset)
    for _ in range(20):
        app.processEvents()
    tl = mw.edit_page._current_timeline or tv._create_default_timeline(asset)
    tv._add_asset_as_clip(asset, tl.video_tracks[0], 0)
    t0 = time.time()
    while time.time() - t0 < 20:
        app.processEvents()
        if mw.timeline_preview_renderer is not None:
            break
        time.sleep(0.02)
    renderer = mw.timeline_preview_renderer
    tl = mw.edit_page._current_timeline
    total = max(c.timeline_end for c in tl.video_tracks[0].clips)
    ep = mw.edit_page
    log(f"timeline ready total={total}")

    emissions: list[tuple[float, int]] = []
    stats = {"emit_total": 0, "emit_none": 0, "present_dict": 0}

    def on_ready(frame, result):
        stats["emit_total"] += 1
        if result is None:
            stats["emit_none"] += 1
        else:
            emissions.append((time.perf_counter(), int(frame)))

    renderer.frame_ready.connect(on_ready)

    # 注意：不要包装 ProgramViewerWidget.set_frame —— 实例属性遮蔽会破坏
    # GPU 预览管线的探测（所有带包装器的运行 worker.gpu_try=0 全程走 CPU
    # direct 路径，测量结果失真）。viewer 边界用 file_controller 的
    # frame_ready 槽间接观察即可（renderer.frame_ready 同源）。
    viewer_shown: list[float] = []

    if "--color-first" in sys.argv:
        # 用户序列：先在调色页打开同一素材并拖拽（调色引擎活跃），再切回
        # 剪辑页快拖——检验跨页面状态/资源竞争。
        clip = tl.video_tracks[0].clips[0]
        mw.open_timeline_clip_in_color(tl.timeline_id, clip.clip_id, 0)
        t0 = time.time()
        while time.time() - t0 < 30:
            app.processEvents(); time.sleep(0.02)
            if getattr(mw, "current_page", "") == "Color" and getattr(mw, "video_controller", None):
                if mw.video_controller.is_video_loaded():
                    break
        log(f"color page opened loaded={mw.video_controller.is_video_loaded()}")
        color_frames = list(range(0, min(600, total), 5))
        mw.on_video_scrub_started(color_frames[0])
        for f in color_frames:
            mw.on_video_scrub_moved(f)
            app.processEvents()
        mw.on_video_scrub_finished(color_frames[-1])
        time.sleep(0.3)
        for _ in range(30):
            app.processEvents(); time.sleep(0.01)
        mw.switch_to_page("Edit")
        for _ in range(60):
            app.processEvents()
        # 页面往返可能重建 TimelinePreviewRenderer —— 必须重新获取当前实例，
        # 否则计数连在旧对象上，测出来的是假象。
        if mw.timeline_preview_renderer is not renderer:
            log("renderer RECREATED after color round-trip")
            try:
                renderer.frame_ready.disconnect(on_ready)
            except (TypeError, RuntimeError):
                pass
            renderer = mw.timeline_preview_renderer
            renderer.frame_ready.connect(on_ready)
        log(f"back on edit page={mw.current_page}")

    state = {"phase": "idle", "frames": [], "idx": 0, "t0": 0.0, "n0": 0, "timer": None}

    def run_phase(name: str, tick_ms: int, step: int, sweeps: int):
        frames = []
        for s in range(sweeps):
            rng = range(10, total - 10, step) if s % 2 == 0 else range(total - 11, 9, -step)
            frames.extend(rng)
        state.update(phase=name, frames=frames, idx=0, t0=time.perf_counter(),
                     n0=len(emissions), v0=len(viewer_shown),
                     s0={"emit_total": stats["emit_total"], "emit_none": stats["emit_none"],
                         "present_dict": stats["present_dict"]})
        ep._on_scrub_started(frames[0])
        tv.set_playhead(frames[0])

        def tick():
            if state["phase"] != name:
                return
            idx = state["idx"]
            if idx >= len(state["frames"]):
                state["timer"].stop()
                active = time.perf_counter() - state["t0"]
                n = len(emissions) - state["n0"]
                v = len(viewer_shown) - state.get("v0", 0)
                s0 = state.get("s0", {"emit_total": 0, "emit_none": 0, "present_dict": 0})
                d_total = stats["emit_total"] - s0["emit_total"]
                d_none = stats["emit_none"] - s0["emit_none"]
                d_pd = stats["present_dict"] - s0["present_dict"]
                log(f"PHASE {name} tick={tick_ms}ms step={step} inputs={len(state['frames'])} "
                    f"active={active:.1f}s displayed={n} viewer={v} "
                    f"emit_total={d_total} emit_none={d_none} present_dict={d_pd} "
                    f"ups={n / max(active, 1e-6):.1f} vups={v / max(active, 1e-6):.1f}")
                ep._on_scrub_finished(state["frames"][-1])
                state["phase"] = "idle"
                QTimer.singleShot(800, app.quit if name == "fast120" else start_next)
                return
            f = state["frames"][idx]
            state["idx"] = idx + 1
            tv.set_playhead(f)
            ep._on_scrub_moved(f)

        state["timer"] = QTimer()
        state["timer"].timeout.connect(tick)
        state["timer"].start(tick_ms)

    def start_next():
        if not app.property("_phases_done"):
            app.setProperty("_phases_done", True)
            run_phase("slow60", 60, 2, 2)
        elif state.get("_stage") is None:
            state["_stage"] = 1
            run_phase("fast60", 16, 20, 6)
        elif state["_stage"] == 1:
            state["_stage"] = 2
            run_phase("fast120", 8, 30, 6)

    start_next()
    app.exec()
    log("FINAL done")
    try:
        mw.close()
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
