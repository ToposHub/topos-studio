#!/usr/bin/env python3
"""Drive the REAL Color page: open a timeline clip, scrub the viewer transport,
count actual displayed frames. Run once with the shared-frame-provider flag off
and once on (--enable-shared-provider) to measure VT hardware decode benefit.

Usage:
  .venv/bin/python tests/benchmark/drive_real_app_color_scrub.py <video> [--enable-shared-provider]
"""

from __future__ import annotations

import os
import sys
import time
import statistics
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
os.environ.setdefault("TOPOS_PRODUCT_MODE", "studio")
os.environ["TOPOS_TEST_MODE"] = "1"
# work_mode=image 的 Step-7 覆盖会禁用 edit 功能（隐藏编辑页），驱动需要
# 完整时间线/媒体池，用最高优先级的环境变量强制开启。
os.environ["TOPOS_FEATURE_EDIT"] = "1"
if not os.environ.get("TOPOS_CONFIG_PATH"):
    os.environ["TOPOS_CONFIG_PATH"] = str(REPO_ROOT / ".topos" / "config.json")

ENABLE_FLAG = "--enable-shared-provider" in sys.argv
VIDEO = Path(next(a for a in sys.argv[1:] if not a.startswith("--"))).resolve()


def log(msg: str) -> None:
    print(f"[CDIAG] {msg}", flush=True)


def main() -> int:
    from PyQt6.QtCore import Qt
    from PyQt6.QtGui import QSurfaceFormat
    from PyQt6.QtWidgets import QApplication

    if ENABLE_FLAG:
        # Flip the preference before any VideoSession is constructed.
        from src.shared.config.config_manager import ConfigManager
        cm = ConfigManager()
        cm.set("video_color_use_shared_frame_provider", True)
        log("shared frame provider flag: ON")

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
    # Headless guard: startup restore (load_last_file) can pop a modal QDialog
    # whose exec() blocks the very first processEvents() call. Arm the killer
    # BEFORE any event processing happens.
    from PyQt6.QtWidgets import QDialog
    from PyQt6.QtCore import QTimer

    def _auto_dismiss_dialogs():
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

    # import + timeline
    mw.media_pool_panel._import_files([str(VIDEO)])
    t0 = time.time(); asset = None
    while time.time() - t0 < 60:
        app.processEvents(); time.sleep(0.02)
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
    tv._add_asset_as_clip(asset, tl.video_tracks[0], 0)
    t0 = time.time()
    while time.time() - t0 < 20:
        app.processEvents()
        if mw.timeline_preview_renderer:
            break
        time.sleep(0.02)
    tl = mw.edit_page._current_timeline
    clip = tl.video_tracks[0].clips[0]
    total = clip.timeline_end
    log(f"timeline ready total={total}")

    # open the clip in Color (real entry)
    mw.open_timeline_clip_in_color(tl.timeline_id, clip.clip_id, 0)
    t0 = time.time()
    while time.time() - t0 < 30:
        app.processEvents(); time.sleep(0.02)
        if getattr(mw, "current_page", "") == "Color" and getattr(mw, "video_controller", None):
            if mw.video_controller.is_video_loaded():
                break
    vc = mw.video_controller
    log(f"color page ready page={mw.current_page} loaded={vc.is_video_loaded()} "
        f"total={getattr(vc, 'total_frames', '?')}")

    # count DECODED frames actually submitted for display (not seek requests)
    shown = {"n": 0}
    orig_submit = vc._submit_decoded_frame_playback

    def counted_submit(*a, **k):
        shown["n"] += 1
        return orig_submit(*a, **k)

    vc._submit_decoded_frame_playback = counted_submit

    # probe: master decode rate in the worker + latest-ready take hit rate
    probe = {"dec_n": 0, "dec_s": 0.0, "tl_calls": 0, "tl_hit": 0}
    coord = getattr(vc, "_coordinator", None)
    session = getattr(coord, "_session", None) if coord is not None else None
    sm = getattr(session, "_source_manager", None) if session is not None else None
    if sm is not None and coord is not None:
        _orig_gmf = sm.get_master_frame

        def _counted_gmf(idx):
            t0 = time.perf_counter()
            r = _orig_gmf(idx)
            probe["dec_s"] += time.perf_counter() - t0
            probe["dec_n"] += 1
            return r

        sm.get_master_frame = _counted_gmf
        _orig_tl = getattr(coord, "take_latest_ready_master_frame", None)

        if callable(_orig_tl):
            def _counted_tl(t):
                probe["tl_calls"] += 1
                r = _orig_tl(t)
                if r is not None:
                    probe["tl_hit"] += 1
                return r

            coord.take_latest_ready_master_frame = _counted_tl

    # clip-local range for the bar
    clip_total = int(getattr(clip, "timeline_duration", 0) or total)

    def scrub(step, cad, sweeps=2):
        frames = []
        for s in range(sweeps):
            rng = range(0, clip_total, step) if s % 2 == 0 else range(clip_total - 1, -1, -step)
            frames.extend(rng)
        n0 = shown["n"]
        mw.on_video_scrub_started(frames[0])
        nxt = time.perf_counter()
        t_begin = time.perf_counter()
        for f in frames:
            now = time.perf_counter()
            while now < nxt:
                time.sleep(min(0.002, max(0, nxt - now)))
                app.processEvents()
                now = time.perf_counter()
            mw.on_video_scrub_moved(f)
            app.processEvents()
            nxt = time.perf_counter() + cad / 1000.0
        active = time.perf_counter() - t_begin
        mw.on_video_scrub_finished(frames[-1])
        t0 = time.time()
        while time.time() - t0 < 1.5:
            app.processEvents(); time.sleep(0.004)
        ups = (shown["n"] - n0) / max(active, 1e-6)
        d0, t0c, t0h = probe["dec_n"], probe["tl_calls"], probe["tl_hit"]
        dec_ms = (probe["dec_s"] / d0 * 1000.0) if d0 else 0.0
        probe["dec_n"] = probe["tl_calls"] = probe["tl_hit"] = 0
        probe["dec_s"] = 0.0
        log(f"SCRUB step={step} cad={cad} inputs={len(frames)} active={active:.1f}s "
            f"displayed={shown['n']-n0} ups={ups:.1f} "
            f"decode_n={d0} dec_ms_avg={dec_ms:.1f} tl_calls={t0c} tl_hit={t0h}")

    scrub(1, 60)   # slow
    scrub(1, 20)   # medium
    scrub(3, 10)   # fast
    scrub(10, 8)   # very fast
    log("FINAL done")
    try:
        mw.close()
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
