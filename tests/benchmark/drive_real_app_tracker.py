#!/usr/bin/env python3
"""用真实 MainWindow + 真实素材复现「Tracker 面板全灰」问题并实测跟踪全流程。

Usage:
  .venv/bin/python tests/benchmark/drive_real_app_tracker.py <video>
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

VIDEO_ARG = sys.argv[1] if len(sys.argv) > 1 else ""
if not VIDEO_ARG:
    raise SystemExit("usage: drive_real_app_tracker.py <video-file> — " + '需要本地视频素材路径（P2-BENCH-01：脚本不再内置个人绝对路径默认值）')
VIDEO = Path(VIDEO_ARG).resolve()


def log(msg: str) -> None:
    print(f"[TRK] {msg}", flush=True)


def pump(app, seconds: float) -> None:
    t0 = time.time()
    while time.time() - t0 < seconds:
        app.processEvents()
        time.sleep(0.01)


def tracker_state(mw) -> str:
    tp = mw.tracker_panel
    return (f"bound={tp._bound_shape!r} state={tp.state} "
            f"btn_forward={tp.btn_forward.isEnabled()} "
            f"btn_backward={tp.btn_backward.isEnabled()} "
            f"nav_first={tp.nav_buttons['first'].isEnabled()} "
            f"mode_combo={tp.mode_combo.isEnabled()}")


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

    def _auto_dismiss_dialogs():
        for w in QApplication.topLevelWidgets():
            if isinstance(w, QDialog) and w.isVisible():
                log(f"auto-dismiss dialog: {type(w).__name__}")
                w.reject()

    killer = QTimer()
    killer.timeout.connect(_auto_dismiss_dialogs)
    killer.start(200)

    mw = MainWindow(feature_flags=ff, feature_registry=reg, test_mode=False)
    mw.show()
    pump(app, 2.0)
    log(f"窗口就绪 dock_tracker 存在={hasattr(mw, 'dock_tracker')} "
        f"可见={mw.dock_tracker.isVisible()}")

    # --- 导入素材 + 建时间线（真实拖放等价路径） ---
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
    pump(app, 1.0)
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
    log(f"时间线就绪: timeline={tl.timeline_id} clip={clip.clip_id}")

    # --- 打开 Color 页（真实入口） ---
    mw.open_timeline_clip_in_color(tl.timeline_id, clip.clip_id, 0)
    t0 = time.time()
    while time.time() - t0 < 30:
        app.processEvents(); time.sleep(0.02)
        if getattr(mw, "current_page", "") == "Color" and getattr(mw, "video_controller", None):
            if mw.video_controller.is_video_loaded():
                break
    log(f"Color 页就绪 page={mw.current_page} "
        f"video_loaded={mw.video_controller.is_video_loaded()}")

    killer.stop()

    # --- 跟踪上下文自检 ---
    try:
        ctx = mw._tracker_frame_context()
        log(f"跟踪上下文: clip_id={ctx[0]!r} media={ctx[1]!r} ref={ctx[2]} "
            f"range=({ctx[3]},{ctx[4]})")
        session = mw._tracker_session()
        log(f"ColorSession: {session!r}")
    except Exception:
        log("跟踪上下文获取异常:\n" + traceback.format_exc())

    layer = mw.engine.get_active_layer()
    log(f"激活图层: {layer!r} mask_shapes={len(layer.mask_shapes) if layer else '?'}")

    # ============================================================
    # 场景 B：先打开 Tracker dock，再画 Mask（怀疑的用户场景）
    # ============================================================
    log("=== 场景 B: 先开 Tracker dock → 再画 Mask ===")
    mw.dock_tracker.setVisible(True)
    mw.dock_tracker.raise_()
    pump(app, 0.5)
    log(f"打开 dock 后: {tracker_state(mw)}")

    mw.on_mask_shape_added("circle")   # 等价于点击 Mask 面板圆形按钮
    pump(app, 0.5)
    shape = mw.engine.get_active_layer().mask_shapes[-1]
    shape.center = [0.5, 0.56]
    shape.size = [0.16, 0.16]
    mw.canvas.update_mask_overlay(mw.engine.get_active_layer())
    log(f"画完圆形窗口(shape={shape.id[:8]}): {tracker_state(mw)}")

    if not mw.tracker_panel.btn_forward.isEnabled():
        log(">>> 复现「全灰」：dock 已开时画 Mask 不会绑定跟踪器 <<<")
        # 修复路径验证：手动点选 mask 列表项（用户也可这样自救）
        mw.mask_panel.shape_selected.emit(shape.id)
        pump(app, 0.3)
        log(f"点击 mask 列表项后: {tracker_state(mw)}")

    # ============================================================
    # 场景 A：关掉 dock → 再画一个 Mask → 重新打开 dock
    # ============================================================
    log("=== 场景 A: 画 Mask → 再打开 Tracker dock ===")
    mw.dock_tracker.setVisible(False)
    pump(app, 0.3)
    mw.on_mask_shape_added("circle")
    pump(app, 0.3)
    shape2 = mw.engine.get_active_layer().mask_shapes[-1]
    shape2.center = [0.5, 0.56]
    shape2.size = [0.16, 0.16]
    mw.dock_tracker.setVisible(True)
    pump(app, 0.5)
    log(f"重新打开 dock 后: {tracker_state(mw)}")

    # ============================================================
    # 真正点「向前跟踪」
    # ============================================================
    log("=== 点击向前跟踪 ===")
    if not mw.tracker_panel.btn_forward.isEnabled():
        log("FAIL: 按钮仍为灰色，无法跟踪")
        mw.close()
        return 1
    mw._start_tracker_tracking("forward")
    pump(app, 1.0)
    log(f"启动后: {tracker_state(mw)}")

    t0 = time.time()
    done_state = None
    while time.time() - t0 < 240:
        app.processEvents(); time.sleep(0.05)
        st = mw.tracker_panel.state
        if st is not None and st.name not in ("RUNNING",):
            done_state = st
            break
    log(f"跟踪结束: state={done_state or mw.tracker_panel.state} "
        f"耗时={time.time()-t0:.1f}s")

    store = mw.engine.tracking_store
    bindings = list(store.bindings_for_clip(ctx[0])) if ctx and ctx[0] else []
    if bindings:
        b = bindings[0]
        ok = sum(1 for t in b.transforms.values()
                 if getattr(t.status, "value", "") != "failed")
        log(f"绑定结果: {len(b.transforms)} 帧, 有效 {ok}, 失败 "
            f"{len(b.failed_frames())}")
    else:
        log("FAIL: 没有任何跟踪绑定")

    # --- 渲染端验证：跟踪几何随帧移动 ---
    try:
        mw.video_controller.seek_to_frame(0)
        pump(app, 1.0)
        g0 = mw.engine.get_tracked_geometry(shape2)
        mw.video_controller.seek_to_frame(150)
        pump(app, 1.5)
        g150 = mw.engine.get_tracked_geometry(shape2)
        if g0 and g150:
            dx = (g150["center"][0] - g0["center"][0]) * 3840
            dy = (g150["center"][1] - g0["center"][1]) * 2160
            log(f"渲染端几何: frame0={g0['center']} frame150={g150['center']} "
                f"位移=({dx:+.0f}px, {dy:+.0f}px)")
        else:
            log(f"渲染端几何缺失: frame0={g0} frame150={g150}")
    except Exception:
        log("渲染端验证异常:\n" + traceback.format_exc())

    # ============================================================
    # 场景 C：Edit↔Color 页往返（工程水合已有窗口）
    # ============================================================
    log("=== 场景 C: 切到 Edit 再回 Color（水合已保存的窗口） ===")
    try:
        mw.switch_to_page("Edit")
        pump(app, 1.5)
        mw.open_timeline_clip_in_color(tl.timeline_id, clip.clip_id, 0)
        t0 = time.time()
        while time.time() - t0 < 30:
            app.processEvents(); time.sleep(0.02)
            if getattr(mw, "current_page", "") == "Color" and getattr(mw, "video_controller", None):
                if mw.video_controller.is_video_loaded():
                    break
        pump(app, 1.0)
        layer = mw.engine.get_active_layer()
        n_shapes = len(layer.mask_shapes) if layer else -1
        tree_top = mw.mask_panel.tree_widget.topLevelItemCount()
        log(f"回到 Color: {tracker_state(mw)}")
        log(f"  图层 mask_shapes={n_shapes} mask面板树条目={tree_top} "
            f"canvas.active_shape_id={mw.canvas.active_shape_id!r}")
    except Exception:
        log("场景 C 异常:\n" + traceback.format_exc())

    log("FINAL done")
    try:
        mw.close()
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
