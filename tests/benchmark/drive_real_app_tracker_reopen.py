#!/usr/bin/env python3
"""复现「新会话打开已保存工程 → 进 Color 页 → Tracker 全灰」的用户场景。

Usage:
  .venv/bin/python tests/benchmark/drive_real_app_tracker_reopen.py <project.topos>
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

if len(sys.argv) <= 1:
    raise SystemExit("usage: drive_real_app_tracker_reopen.py <project.topos>（P2-BENCH-01：脚本不再内置个人绝对路径默认值）")
PROJECT = sys.argv[1]


def log(msg: str) -> None:
    print(f"[TRK2] {msg}", flush=True)


def pump(app, seconds: float) -> None:
    t0 = time.time()
    while time.time() - t0 < seconds:
        app.processEvents()
        time.sleep(0.01)


def main() -> int:
    from PyQt6.QtCore import Qt
    from PyQt6.QtGui import QSurfaceFormat
    from PyQt6.QtWidgets import QApplication

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

    mw = MainWindow(feature_flags=ff, feature_registry=reg, test_mode=False)
    mw.show()
    pump(app, 2.0)

    # --- 打开已保存工程（用户日常路径） ---
    log(f"打开工程: {PROJECT}")
    ok = mw._open_project_path(PROJECT)
    pump(app, 3.0)
    proj = getattr(mw, "_current_edit_project", None)
    log(f"_open_project_path -> {ok}; project={proj!r}")
    assert proj is not None, "工程未加载"

    tl = None
    timelines = getattr(mw, "_current_timeline_sequences", {}) or {}
    log(f"timelines dict: {len(timelines)} 个")
    for tid, seq in timelines.items():
        if getattr(seq, "video_tracks", None) and seq.video_tracks[0].clips:
            tl = seq
            break
    if tl is None:
        ep_tl = getattr(mw.edit_page, "_current_timeline", None) if hasattr(mw, "edit_page") else None
        if ep_tl is not None and getattr(ep_tl, "video_tracks", None) and ep_tl.video_tracks[0].clips:
            tl = ep_tl
            log("使用 edit_page._current_timeline")
    assert tl is not None, f"无时间线: sequences={list(timelines)} edit_page_tl={getattr(getattr(mw,'edit_page',None),'_current_timeline',None)}"
    clip = tl.video_tracks[0].clips[0]
    log(f"时间线: {tl.timeline_id[:8]} clip={clip.clip_id[:8]}")

    # --- 进 Color 页 ---
    mw.open_timeline_clip_in_color(tl.timeline_id, clip.clip_id, 0)
    t0 = time.time()
    while time.time() - t0 < 30:
        app.processEvents(); time.sleep(0.02)
        if getattr(mw, "current_page", "") == "Color" and getattr(mw, "video_controller", None):
            if mw.video_controller.is_video_loaded():
                break
    pump(app, 2.0)
    log(f"Color 页就绪 loaded={mw.video_controller.is_video_loaded()}")

    layer = mw.engine.get_active_layer()
    n_shapes = len(layer.mask_shapes) if layer else -1
    tree_top = mw.mask_panel.tree_widget.topLevelItemCount()
    log(f"进入 Color 后: 图层masks={n_shapes} 面板树={tree_top} "
        f"canvas.active={mw.canvas.active_shape_id!r}")

    tp = mw.tracker_panel
    log(f"Tracker(未开dock): bound={tp._bound_shape!r} "
        f"forward={tp.btn_forward.isEnabled()}")

    mw.dock_tracker.setVisible(True)
    mw.dock_tracker.raise_()
    pump(app, 0.8)
    log(f"打开 dock 后: bound={tp._bound_shape!r} "
        f"forward={tp.btn_forward.isEnabled()} "
        f"nav={tp.nav_buttons['first'].isEnabled()} "
        f"mode={tp.mode_combo.isEnabled()}")

    gray = not tp.btn_forward.isEnabled()
    log(f">>> {'复现全灰！' if gray else '面板正常（已绑定）'} <<<")

    # --- 画一个 Mask 再看（用户声称的操作） ---
    mw.on_mask_shape_added("circle")
    pump(app, 0.8)
    layer = mw.engine.get_active_layer()
    log(f"画 Mask 后: 图层masks={len(layer.mask_shapes) if layer else -1} "
        f"面板树={mw.mask_panel.tree_widget.topLevelItemCount()} "
        f"bound={tp._bound_shape!r} forward={tp.btn_forward.isEnabled()}")
    if not tp.btn_forward.isEnabled() and mw.mask_panel.tree_widget.topLevelItemCount() > 0:
        mw.mask_panel.on_item_clicked()
        pump(app, 0.5)
        log(f"点树条目后: bound={tp._bound_shape!r} "
            f"forward={tp.btn_forward.isEnabled()}")

    if gray:
        # 用户自救路径1：点 Mask 面板树里的形状
        if tree_top > 0:
            mw.mask_panel.on_item_clicked()
            pump(app, 0.5)
            log(f"点击树条目后: bound={tp._bound_shape!r} "
                f"forward={tp.btn_forward.isEnabled()}")

    log("FINAL done")
    try:
        mw.close()
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
