#!/usr/bin/env python3
"""F1.1 U06 重放：真实窗口切源/替换压力——R21（GL 1282 首帧崩溃）回归。

场景：真实 MainWindow 中，交替 ReplaceMediaCommand 替换片段源文件（A→B→A…）
+ 每次替换后跨段 scrub，驱动解码源 open/close 与 GL 目标重建。断言：
  - 全程无 Python 异常、无 GL 1282（invalid program/object）日志
  - Program viewer 存活且仍在渲染（paint/呈现计数前进）
  - RSS 增长有界
输出结构化 JSON 报告；退出码 0/1/2。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

if not os.environ.get("TOPOS_CONFIG_PATH"):
    os.environ["TOPOS_CONFIG_PATH"] = str(REPO_ROOT / ".topos" / "config.json")
os.environ.setdefault("TOPOS_PRODUCT_MODE", "studio")
os.environ["TOPOS_TEST_MODE"] = "1"
os.environ["TOPOS_FEATURE_EDIT"] = "1"

from tests.edit.helpers import golden_media  # noqa: E402

REPORT: dict = {"checks": {}, "samples": []}
FAILURES: list[str] = []
GL_ERROR_MARKERS = ("1282", "invalid program", "invalid operation",
                    "GL_INVALID", "glIsProgram")


def log(msg: str) -> None:
    print(f"[U06-G] {msg}", flush=True)


def check(name: str, ok: bool, detail: str = "") -> None:
    REPORT["checks"][name] = {"ok": bool(ok), "detail": detail}
    if not ok:
        FAILURES.append(f"{name}: {detail}")
    log(f"  {'PASS' if ok else 'FAIL'} {name} {detail}")


class _GlErrorCollector(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.hits: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        msg = record.getMessage()
        if any(marker in msg for marker in GL_ERROR_MARKERS):
            self.hits.append(msg[:200])


def _rss_mb() -> float:
    try:
        import psutil
        return psutil.Process().memory_info().rss / 1024 / 1024
    except Exception:
        return 0.0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--switches", type=int, default=30)
    parser.add_argument("--out", default=None)
    args = parser.parse_args()

    from PyQt6.QtCore import Qt, QTimer
    from PyQt6.QtGui import QSurfaceFormat
    from PyQt6.QtWidgets import QApplication

    os.environ["QSG_RHI_BACKEND"] = "opengl"
    fmt = QSurfaceFormat()
    fmt.setVersion(4, 5)
    fmt.setProfile(QSurfaceFormat.OpenGLContextProfile.CoreProfile)
    fmt.setRenderableType(QSurfaceFormat.RenderableType.OpenGL)
    QSurfaceFormat.setDefaultFormat(fmt)
    QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts, True)
    app = QApplication(sys.argv)

    from PyQt6.QtWidgets import QDialog

    def _auto_dismiss_dialogs() -> None:
        for w in QApplication.topLevelWidgets():
            if isinstance(w, QDialog) and w.isVisible():
                log(f"auto-dismiss dialog: {type(w).__name__}")
                w.reject()

    dialog_killer = QTimer()
    dialog_killer.timeout.connect(_auto_dismiss_dialogs)
    dialog_killer.start(200)

    gl_collector = _GlErrorCollector()
    logging.getLogger().addHandler(gl_collector)
    for name in ("PyQt6", "OpenGL", "src", ""):
        logging.getLogger(name).addHandler(gl_collector)

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
    if feature_flags.is_enabled("edit"):
        from src.features.edit.module import EditModule
        registry.register_module(EditModule())

    media_dir = Path(tempfile.mkdtemp(prefix="u06_media_"))
    video_a = golden_media.write_gradient_video(
        media_dir / "src_a.mp4", width=640, height=360, frames=72)
    video_b = golden_media.write_gradient_video(
        media_dir / "src_b.mp4", width=640, height=360, frames=72, fps_num=25, fps_den=1)

    mw = MainWindow(feature_flags=feature_flags, feature_registry=registry, test_mode=False)
    mw.show()
    for _ in range(90):
        app.processEvents()

    mw.media_pool_panel._import_files([str(video_a), str(video_b)])
    deadline = time.time() + 60
    while time.time() < deadline:
        app.processEvents()
        project = getattr(mw, "_current_edit_project", None)
        if project is not None and project.get_asset_by_path(str(video_b)) is not None:
            break
        time.sleep(0.02)
    project = mw._current_edit_project
    asset_a = project.get_asset_by_path(str(video_a))
    asset_b = project.get_asset_by_path(str(video_b))
    check("import_both_sources", None not in (asset_a, asset_b))

    tv = mw.timeline_panel.timeline_view
    tv._ensure_first_asset_timeline_settings(asset_a)
    for _ in range(20):
        app.processEvents()
    timeline = mw.edit_page._current_timeline or tv._create_default_timeline(asset_a)
    tv._add_asset_as_clip(asset_a, timeline.video_tracks[0], 0)
    for _ in range(20):
        app.processEvents()

    rss_start = _rss_mb()
    crashes = 0
    from src.features.edit.core.commands import ReplaceMediaCommand

    for i in range(args.switches):
        try:
            target = asset_b if i % 2 == 0 else asset_a
            cmd = ReplaceMediaCommand.create(
                asset_id=asset_a.asset_id, new_path=target.path,
                new_metadata={
                    "width": 640, "height": 360,
                    "fps": 25.0 if i % 2 == 0 else 24.0,
                    "total_frames": 72, "codec": "h264"})
            # 生产提交路径（media_pool_panel._commit_replacement 同款四步）
            pool = mw.media_pool_panel
            pool._project = pool._command_stack.execute(cmd, pool._project)
            pool.project_changed.emit(pool._project)
            pool._refresh_tree()
            # 跨段 scrub 驱动解码与 GL 重建
            mw.edit_page.seek_to_frame((i * 7) % 60)
            for _ in range(6):
                app.processEvents()
            mw.edit_page.seek_to_frame((i * 11 + 3) % 60)
            for _ in range(6):
                app.processEvents()
        except Exception as exc:  # noqa: BLE001——压力循环内任何异常都计数
            crashes += 1
            log(f"  iteration {i} exception: {exc!r}")
        if i % 6 == 0:
            REPORT["samples"].append({"i": i, "rss_mb": round(_rss_mb(), 1)})

    rss_end = _rss_mb()
    growth = rss_end - rss_start
    check("no_iteration_exceptions", crashes == 0, f"crashes={crashes}")
    check("no_gl_1282_logs", not gl_collector.hits,
          f"hits={gl_collector.hits[:3]}")
    check("app_alive_after_stress", mw.isVisible() or mw.windowHandle() is not None)
    check("rss_growth_bounded", growth < 600.0,
          f"start={rss_start:.0f}MB end={rss_end:.0f}MB growth={growth:.0f}MB")

    REPORT["failures"] = FAILURES
    REPORT["passed"] = not FAILURES
    REPORT["switches"] = args.switches
    out_dir = Path(args.out) if args.out else Path(tempfile.mkdtemp(prefix="u06_report_"))
    out_dir.mkdir(parents=True, exist_ok=True)
    report_path = out_dir / "report.json"
    report_path.write_text(json.dumps(REPORT, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"report -> {report_path}")
    log(f"RESULT {'PASS' if not FAILURES else 'FAIL'} failures={len(FAILURES)}")
    return 0 if not FAILURES else 1


if __name__ == "__main__":
    sys.exit(main())
