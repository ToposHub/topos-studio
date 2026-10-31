#!/usr/bin/env python3
"""遍历用户工程中所有有调色栈的片段，逐个进 Color 验证：
激活图层是否存在、画 Mask 是否成功、Tracker 是否绑定。

Usage:
  .venv/bin/python tests/benchmark/drive_real_app_tracker_clips.py <project.topos>
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
os.environ.setdefault("TOPOS_PRODUCT_MODE", "studio")
os.environ["TOPOS_TEST_MODE"] = "1"
os.environ["TOPOS_FEATURE_EDIT"] = "1"
if not os.environ.get("TOPOS_CONFIG_PATH"):
    os.environ["TOPOS_CONFIG_PATH"] = str(REPO_ROOT / ".topos" / "config.json")

PROJECT = sys.argv[1] if len(sys.argv) > 1 else "/tmp/user_project2_copy.topos"


def log(m):
    print(f"[CLIP] {m}", flush=True)


def pump(app, seconds):
    t0 = time.time()
    while time.time() - t0 < seconds:
        app.processEvents(); time.sleep(0.01)


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
    cm = ConfigManager(); ff = load_feature_flags(cm); I18N.load_locale("zh_CN")
    reg = FeatureRegistry()
    reg.initialize(AppContext(config_manager=cm, feature_flags=ff, i18n=I18N, logger=None))
    from src.features.color.module import ColorModule
    reg.register_module(ColorModule())
    from src.features.edit.module import EditModule
    reg.register_module(EditModule())

    mw = MainWindow(feature_flags=ff, feature_registry=reg, test_mode=False)
    mw.show(); pump(app, 2.0)
    assert mw._open_project_path(PROJECT)
    pump(app, 3.0)

    seqs = getattr(mw, "_current_timeline_sequences", {}) or {}
    tl = next(s for s in seqs.values()
              if getattr(s, "video_tracks", None) and s.video_tracks[0].clips)
    clips = []
    for track in tl.video_tracks:
        clips.extend(track.clips)
    log(f"时间线 {tl.timeline_id[:8]} 共 {len(clips)} 个视频 clip")

    mw.dock_tracker.setVisible(True)
    pump(app, 0.5)

    results = []
    for clip in clips:
        mw.open_timeline_clip_in_color(tl.timeline_id, clip.clip_id, 0)
        t0 = time.time()
        while time.time() - t0 < 25:
            app.processEvents(); time.sleep(0.02)
            if getattr(mw, "current_page", "") == "Color" and getattr(mw, "video_controller", None):
                if mw.video_controller.is_video_loaded():
                    break
        pump(app, 1.2)
        layer = mw.engine.get_active_layer()
        n0 = len(layer.mask_shapes) if layer else -1
        # 清掉上个 clip 残留状态，模拟用户单独进入该 clip
        if layer and layer.mask_shapes:
            layer.mask_shapes.clear()
            mw.canvas.set_active_shape(None)
            mw.mask_panel.update_shapes_list([])
            pump(app, 0.3)
            mw.tracker_panel.set_bound_shape(None)
        pump(app, 0.2)
        n_base = len(mw.engine.get_active_layer().mask_shapes) if mw.engine.get_active_layer() else -1
        mw.on_mask_shape_added("circle")
        pump(app, 0.6)
        layer = mw.engine.get_active_layer()
        n1 = len(layer.mask_shapes) if layer else -1
        bound = mw.tracker_panel._bound_shape
        enabled = mw.tracker_panel.btn_forward.isEnabled()
        ok = (n1 == n_base + 1) and bound is not None and enabled
        results.append((clip.clip_id, n1 == n_base + 1, bound is not None, enabled))
        log(f"clip={clip.clip_id[:8]} layer={'None!' if layer is None else 'ok'} "
            f"画Mask={'ok' if n1 == n_base + 1 else f'失败(base={n0},清后={n_base},后={n1})'} "
            f"bound={bound!r} forward={enabled} => {'OK' if ok else '!!! 问题'}")
        # 回 Edit 便于下一个 clip 进入
        mw.switch_to_page("Edit")
        pump(app, 0.8)

    bad = [r for r in results if not (r[1] and r[2] and r[3])]
    log(f"汇总: {len(results)} 个 clip, 异常 {len(bad)} 个")
    log("FINAL done")
    try:
        mw.close()
    except Exception:
        pass
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
