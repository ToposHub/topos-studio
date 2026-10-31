#!/usr/bin/env python3
"""复现用户报告：Masks 画圆 → 切换到 Tracker 面板后形状消失、无法绑定跟踪。

Usage:
  .venv/bin/python tests/benchmark/drive_repro_tracker_switch.py [video]
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
    raise SystemExit("usage: drive_repro_tracker_switch.py <video-file> — " + '需要本地视频素材路径（P2-BENCH-01：脚本不再内置个人绝对路径默认值）')
VIDEO = Path(VIDEO_ARG).resolve()


def log(msg: str) -> None:
    print(f"[REPRO] {msg}", flush=True)


def pump(app, seconds: float) -> None:
    t0 = time.time()
    while time.time() - t0 < seconds:
        app.processEvents()
        time.sleep(0.01)


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

    killer = QTimer()
    killer.timeout.connect(lambda: [w.reject() for w in QApplication.topLevelWidgets()
                                    if isinstance(w, QDialog) and w.isVisible()])
    killer.start(200)

    mw = MainWindow(feature_flags=ff, feature_registry=reg, test_mode=False)
    mw.show()
    pump(app, 2.0)

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

    mw.open_timeline_clip_in_color(tl.timeline_id, clip.clip_id, 0)
    t0 = time.time()
    while time.time() - t0 < 30:
        app.processEvents(); time.sleep(0.02)
        if getattr(mw, "current_page", "") == "Color" and getattr(mw, "video_controller", None):
            if mw.video_controller.is_video_loaded():
                break
    pump(app, 1.5)
    killer.stop()
    log(f"Color 页就绪 loaded={mw.video_controller.is_video_loaded()}")

    results = {}
    try:
        # ---- 用户步骤 1：打开 Masks 面板 ----
        mw.dock_masks.setVisible(True)
        mw.dock_masks.raise_()
        pump(app, 0.4)
        log(f"步骤1 Masks dock 可见={mw.dock_masks.isVisible()} "
            f"_show_mask_overlays={mw.canvas._show_mask_overlays}")

        # ---- 用户步骤 2：点击"圆形"按钮画一个圆 ----
        mw.on_mask_shape_added("circle")
        pump(app, 0.5)
        layer = mw.engine.get_active_layer()
        shape = layer.mask_shapes[-1] if layer and layer.mask_shapes else None
        assert shape is not None, "形状未创建"
        shape.center = [0.5, 0.56]
        shape.size = [0.16, 0.16]
        layer.mask_dirty = True
        pump(app, 0.3)
        item = mw.canvas.mask_items.get(shape.id)
        results["画圆后_overlay可见"] = bool(item and item.isVisible())
        log(f"步骤2 画圆 id={shape.id[:8]} active={mw.canvas.active_shape_id == shape.id} "
            f"item.isVisible={item.isVisible() if item else None} "
            f"tracker._bound_shape={mw.tracker_panel._bound_shape!r} "
            f"ctrl._tracker_shape_id={getattr(mw, '_tracker_shape_id', None)!r}")

        # ---- 用户步骤 3：切换到 Tracker 面板（隐藏 Masks，显示 Tracker）----
        mw.dock_masks.setVisible(False)   # 用户切走 Masks
        pump(app, 0.2)
        log(f"  3a Masks 隐藏后 _show_mask_overlays={mw.canvas._show_mask_overlays} "
            f"tracker._bound_shape={mw.tracker_panel._bound_shape!r} "
            f"ctrl._tracker_shape_id={getattr(mw, '_tracker_shape_id', None)!r}")
        mw.dock_tracker.setVisible(True)  # 打开 Tracker
        mw.dock_tracker.raise_()
        pump(app, 0.5)
        item = mw.canvas.mask_items.get(shape.id)
        results["切到Tracker后_形状仍显示"] = bool(item and item.isVisible())
        log(f"步骤3 Tracker 可见={mw.dock_tracker.isVisible()} "
            f"tracker._bound_shape={mw.tracker_panel._bound_shape!r} "
            f"ctrl._tracker_shape_id={getattr(mw, '_tracker_shape_id', None)!r} "
            f"形状item.isVisible={item.isVisible() if item else None}")

        results["Tracker绑定成功"] = mw.tracker_panel._bound_shape is not None
        results["向前跟踪按钮可用"] = mw.tracker_panel.btn_forward.isEnabled()

        # ---- 用户步骤 4：在视图中点击圆形选择跟踪目标 ----
        from PyQt6.QtCore import QPointF, QPoint
        from PyQt6.QtGui import QMouseEvent
        vp = mw.canvas.viewport()
        center_scene = QPointF(0.5 * 3840, 0.56 * 2160)
        vp_pos = mw.canvas.mapFromScene(center_scene)
        log(f"步骤4 模拟点击形状中心 scene={center_scene} vp={vp_pos}")
        if not vp.rect().contains(vp_pos):
            log(f"  点击点不在视口内 rect={vp.rect()}，跳过点击模拟")
        else:
            me = QMouseEvent(
                QMouseEvent.Type.MouseButtonPress, QPointF(vp_pos),
                QPointF(vp.mapToGlobal(vp_pos)),
                Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
                Qt.KeyboardModifier.NoModifier)
            QApplication.sendEvent(vp, me)
            pump(app, 0.3)
        results["点击形状后_选中态"] = mw.canvas.active_shape_id == shape.id
        results["点击形状后_Tracker绑定"] = (
            mw.tracker_panel._bound_shape is not None)

        # ---- 用户步骤 5：尝试直接启动跟踪 ----
        if mw.tracker_panel.btn_forward.isEnabled():
            try:
                mw._start_tracker_tracking("forward")
                pump(app, 1.0)
                t0 = time.time(); final = None
                while time.time() - t0 < 60:
                    app.processEvents(); time.sleep(0.05)
                    st = mw.tracker_panel.state
                    if st is not None and st.name != "RUNNING":
                        final = st.name
                        break
                results["向前跟踪"] = f"state={final}"
            except Exception as exc:  # noqa: BLE001
                results["向前跟踪"] = f"异常: {exc}"
                log(traceback.format_exc())
        else:
            results["向前跟踪"] = "按钮禁用(无法跟踪)"

    except Exception:
        log("EXC:\n" + traceback.format_exc())
        results["异常"] = True

    log("========== 复现结果 ==========")
    for k, v in results.items():
        log(f"  {k}: {v}")
    log("FINAL done")
    try:
        mw.close()
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
