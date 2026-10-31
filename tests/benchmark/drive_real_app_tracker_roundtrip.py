#!/usr/bin/env python3
"""决定性实验：保存带窗口的工程 → 全新进程重开 → 进 Color → Tracker 是否绑定。

Usage:
  .venv/bin/python drive_real_app_tracker_roundtrip.py save   <video> <project.topos>
  .venv/bin/python drive_real_app_tracker_roundtrip.py reopen <project.topos>
"""
from __future__ import annotations

import os
import sys
import time
import traceback
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
os.environ.setdefault("TOPOS_PRODUCT_MODE", "studio")
os.environ["TOPOS_TEST_MODE"] = "1"
os.environ["TOPOS_FEATURE_EDIT"] = "1"
if not os.environ.get("TOPOS_CONFIG_PATH"):
    os.environ["TOPOS_CONFIG_PATH"] = str(REPO_ROOT / ".topos" / "config.json")

MODE = sys.argv[1] if len(sys.argv) > 1 else ""
VIDEO_ARG = os.environ.get("TOPOS_BENCH_VIDEO", "")
if not VIDEO_ARG:
    raise SystemExit("需要 TOPOS_BENCH_VIDEO 环境变量提供素材路径（P2-BENCH-01：脚本不再内置个人绝对路径默认值）")
VIDEO = VIDEO_ARG
PROJECT = sys.argv[-1] if MODE == "reopen" else sys.argv[-1]


def log(m):
    print(f"[RT] {m}", flush=True)


def pump(app, seconds):
    t0 = time.time()
    while time.time() - t0 < seconds:
        app.processEvents(); time.sleep(0.01)


def build_app():
    from PyQt6.QtCore import Qt
    from PyQt6.QtGui import QSurfaceFormat
    from PyQt6.QtWidgets import QApplication
    fmt = QSurfaceFormat()
    fmt.setVersion(4, 5)
    fmt.setProfile(QSurfaceFormat.OpenGLContextProfile.CoreProfile)
    QSurfaceFormat.setDefaultFormat(fmt)
    QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts, True)
    app = QApplication.instance() or QApplication(sys.argv)
    from src.shared.config.config_manager import ConfigManager
    from src.shared.feature_flags import load_feature_flags
    from src.app.feature_registry import FeatureRegistry
    from src.app.app_context import AppContext
    from src.shared.i18n.i18n import I18N
    from src.app.main_window import MainWindow
    cm = ConfigManager(); ff = load_feature_flags(cm); I18N.load_locale("zh_CN")
    reg = FeatureRegistry()
    reg.initialize(AppContext(config_manager=cm, feature_flags=ff, i18n=I18N, logger=None))
    from src.features.color.module import ColorModule
    reg.register_module(ColorModule())
    from src.features.edit.module import EditModule
    reg.register_module(EditModule())
    mw = MainWindow(feature_flags=ff, feature_registry=reg, test_mode=False)
    mw.show(); pump(app, 2.0)
    return app, mw


def get_timeline_clip(mw):
    for tid, seq in (getattr(mw, "_current_timeline_sequences", {}) or {}).items():
        if getattr(seq, "video_tracks", None) and seq.video_tracks[0].clips:
            return seq, seq.video_tracks[0].clips[0]
    raise AssertionError("无时间线")


def enter_color(mw, app, tl, clip):
    mw.open_timeline_clip_in_color(tl.timeline_id, clip.clip_id, 0)
    t0 = time.time()
    while time.time() - t0 < 30:
        app.processEvents(); time.sleep(0.02)
        if getattr(mw, "current_page", "") == "Color" and getattr(mw, "video_controller", None):
            if mw.video_controller.is_video_loaded():
                break
    pump(app, 2.0)


def main():
    app, mw = build_app()

    if MODE == "save":
        mw.media_pool_panel._import_files([VIDEO])
        t0 = time.time(); asset = None
        while time.time() - t0 < 60:
            app.processEvents(); time.sleep(0.02)
            p = getattr(mw, "_current_edit_project", None)
            if p is not None:
                asset = p.get_asset_by_path(VIDEO)
                if asset: break
        assert asset
        tv = mw.timeline_panel.timeline_view
        tv._ensure_first_asset_timeline_settings(asset)
        pump(app, 1.0)
        tl = mw.edit_page._current_timeline or tv._create_default_timeline(asset)
        tv._add_asset_as_clip(asset, tl.video_tracks[0], 0)
        t0 = time.time()
        while time.time() - t0 < 20:
            app.processEvents()
            if mw.timeline_preview_renderer: break
            time.sleep(0.02)
        tl, clip = get_timeline_clip(mw)
        enter_color(mw, app, tl, clip)
        mw.on_mask_shape_added("circle")
        pump(app, 0.8)
        layer = mw.engine.get_active_layer()
        assert layer and layer.mask_shapes, "画 Mask 失败"
        mw.dock_tracker.setVisible(True)
        pump(app, 0.5)
        log(f"保存前: masks={len(layer.mask_shapes)} bound={mw.tracker_panel._bound_shape!r} "
            f"forward={mw.tracker_panel.btn_forward.isEnabled()}")
        # 显式保存工程（含 color stack 里的窗口）：直接指定路径避免对话框
        mw._current_edit_project_filepath = PROJECT
        mw.save_router.set_project_path(PROJECT)
        ok = mw.save_router.save()
        pump(app, 2.0)
        log(f"保存 ok={ok} -> {PROJECT}")
    else:
        ok = mw._open_project_path(PROJECT)
        pump(app, 3.0)
        log(f"打开工程 ok={ok}")
        tl, clip = get_timeline_clip(mw)
        enter_color(mw, app, tl, clip)
        layer = mw.engine.get_active_layer()
        n = len(layer.mask_shapes) if layer else -1
        tree = mw.mask_panel.tree_widget.topLevelItemCount()
        log(f"水合后: 图层masks={n} 面板树={tree} canvas.active={mw.canvas.active_shape_id!r}")
        mw.dock_tracker.setVisible(True); mw.dock_tracker.raise_()
        pump(app, 0.8)
        tp = mw.tracker_panel
        log(f"开 dock 后: bound={tp._bound_shape!r} forward={tp.btn_forward.isEnabled()} "
            f"nav={tp.nav_buttons['first'].isEnabled()}")
        if not tp.btn_forward.isEnabled():
            log(">>> 复现：重开工程后已保存的窗口不绑定跟踪器（全灰）<<<")
            if tree > 0:
                mw.mask_panel.on_item_clicked(); pump(app, 0.5)
                log(f"点树条目后: bound={tp._bound_shape!r} forward={tp.btn_forward.isEnabled()}")

    log("FINAL done")
    try:
        mw.close()
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
