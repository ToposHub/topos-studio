#!/usr/bin/env python3
"""达芬奇式两步流程全形状验证：
  第1步: Mask 面板画形状（circle / rect / curve / gradient）
  第2步: 打开 Tracker 面板 → 点击向前跟踪
验证每种形状都能：绑定 → 按钮点亮 → 跟踪成功 → 结果应用。

Usage:
  .venv/bin/python tests/benchmark/drive_real_app_tracker_shapes.py <video>
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
    raise SystemExit("usage: drive_real_app_tracker_shapes.py <video-file> — " + '需要本地视频素材路径（P2-BENCH-01：脚本不再内置个人绝对路径默认值）')
VIDEO = Path(VIDEO_ARG).resolve()
SHAPES = ["circle", "rect", "curve"]


def log(msg: str) -> None:
    print(f"[SHP] {msg}", flush=True)


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

    tp = mw.tracker_panel
    results = {}
    for shape_type in SHAPES:
        tag = shape_type
        try:
            # ---- 第 1 步：Mask 面板画形状（按钮等价调用） ----
            mw.on_mask_shape_added(shape_type)
            pump(app, 0.6)
            layer = mw.engine.get_active_layer()
            shape = layer.mask_shapes[-1] if layer and layer.mask_shapes else None
            if shape is None:
                results[tag] = "画形状失败"
                log(f"[{tag}] FAIL: 形状未创建")
                continue
            # 模拟用户拖放窗口到方向盘 LOGO 区域（几何属性就是窗口区域）
            shape.center = [0.5, 0.56]
            shape.size = [0.16, 0.16]
            layer.mask_dirty = True
            # 曲线：补两个点模拟用户画完曲线（形状已存在，绑定不受影响）
            if shape_type == "curve" and not shape.points:
                shape.points = [
                    {"pos": [0.44, 0.50], "c1": [0.0, 0.0], "c2": [0.0, 0.0]},
                    {"pos": [0.56, 0.50], "c1": [0.0, 0.0], "c2": [0.0, 0.0]},
                    {"pos": [0.50, 0.62], "c1": [0.0, 0.0], "c2": [0.0, 0.0]},
                ]
            pump(app, 0.3)
            bound = tp._bound_shape
            enabled = tp.btn_forward.isEnabled()

            # ---- 第 2 步：打开跟踪面板点击向前跟踪 ----
            mw.dock_tracker.setVisible(True)
            pump(app, 0.4)
            if not tp.btn_forward.isEnabled():
                results[tag] = f"面板灰(bound={bound!r})"
                log(f"[{tag}] FAIL: 按钮未点亮 bound={bound!r}")
                # 清理本形状，继续下一个
                layer.mask_shapes.remove(shape)
                mw.canvas.set_active_shape(None)
                continue

            mw._start_tracker_tracking("forward")
            pump(app, 0.8)
            t0 = time.time()
            final = None
            while time.time() - t0 < 180:
                app.processEvents(); time.sleep(0.05)
                st = tp.state
                if st is not None and st.name != "RUNNING":
                    final = st.name
                    break
            if final != "SUCCESS":
                reason = tp.stats_label.text()
                results[tag] = f"跟踪状态={final}({reason[:40]})"
                log(f"[{tag}] FAIL: state={final} {reason}")
            else:
                # 渲染端应用验证
                mw.video_controller.seek_to_frame(0)
                pump(app, 0.8)
                g0 = mw.engine.get_tracked_geometry(shape)
                mw.video_controller.seek_to_frame(150)
                pump(app, 1.2)
                g150 = mw.engine.get_tracked_geometry(shape)
                if g0 and g150:
                    dx = (g150["center"][0] - g0["center"][0]) * 3840
                    dy = (g150["center"][1] - g0["center"][1]) * 2160
                    results[tag] = "OK"
                    log(f"[{tag}] OK: 跟踪成功, 渲染位移=({dx:+.0f}, {dy:+.0f})px")
                else:
                    results[tag] = "跟踪成功但渲染端几何缺失"
                    log(f"[{tag}] WARN: 跟踪成功但 get_tracked_geometry 缺失 "
                        f"(f0={g0 is not None}, f150={g150 is not None})")

            # ---- 清理：删除本形状，回到干净状态测下一个 ----
            mw.on_mask_shape_removed(shape.id)
            pump(app, 0.5)
        except Exception:
            results[tag] = "异常"
            log(f"[{tag}] EXC:\n{traceback.format_exc()}")

    ok = [t for t, r in results.items() if r == "OK"]
    bad = {t: r for t, r in results.items() if r != "OK"}
    log(f"汇总: {len(ok)}/{len(SHAPES)} 形状通过 -> {results}")
    log("FINAL done")
    try:
        mw.close()
    except Exception:
        pass
    return 0 if not bad else 1


if __name__ == "__main__":
    raise SystemExit(main())
