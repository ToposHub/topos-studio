"""Visible, full-app Qt event audit. Uses an isolated config and scratch project.

Run with the repository .venv Python. Place Python snippets in the session
directory as command-*.py. They run on the Qt UI thread and their inputs,
outputs, screenshots and exceptions are retained for reproducibility.
This is a test harness, not a production control API.
"""
from __future__ import annotations

import json
import os
import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
OUT = Path(__file__).resolve().parent / "evidence"
SESSION = Path("/tmp/topos-edit-audit-20261013")
OUT.mkdir(parents=True, exist_ok=True)
SESSION.mkdir(parents=True, exist_ok=True)
sys.path.insert(0, str(ROOT))
os.environ["TOPOS_CONFIG_PATH"] = str(SESSION / "config.json")
os.environ["TOPOS_PRODUCT_MODE"] = "studio"
os.environ["TOPOS_TEST_MODE"] = "1"
os.environ["TOPOS_FEATURE_EDIT"] = "1"
os.environ["OPENBLAS_NUM_THREADS"] = "1"
os.environ["QSG_RHI_BACKEND"] = "opengl"


def retain_qt_exception(exc_type, exc, tb) -> None:
    """Retain uncaught slot errors so the remaining audit can proceed.

    The first run used the default hook and crashed in the source viewer.
    This hook is solely an audit continuation measure, not a fix.
    """
    detail = "".join(traceback.format_exception(exc_type, exc, tb))
    with (OUT / "uncaught-qt-exceptions.log").open("a", encoding="utf-8") as handle:
        handle.write(detail)
    print(detail, file=sys.stderr, flush=True)


sys.excepthook = retain_qt_exception
(SESSION / "config.json").write_text(json.dumps({
    "locale": "zh_CN", "current_page": "Edit", "work_mode": "video",
    "auto_load_last_project": False, "check_updates_on_startup": False,
    "show_welcome_on_startup": False,
    "features": {"color": True, "edit": True, "composite": True,
                 "deliver": True, "teaching_animation": True, "ai_assistant": True},
}), encoding="utf-8")

# Use production startup native-symbol isolation and WebEngine import order.
import src.main  # noqa: E402,F401
from PyQt6.QtCore import Qt, QTimer, QPoint, QPointF, QEvent, QMimeData, QUrl  # noqa: E402
from PyQt6.QtGui import QSurfaceFormat, QMouseEvent, QWheelEvent, QDragEnterEvent, QDragMoveEvent, QDropEvent  # noqa: E402
from PyQt6.QtWidgets import QApplication, QDialog, QFileDialog, QToolButton, QPushButton, QLineEdit  # noqa: E402
from PyQt6.QtTest import QTest  # noqa: E402

fmt = QSurfaceFormat()
fmt.setVersion(4, 5)  # same request as production startup
fmt.setProfile(QSurfaceFormat.OpenGLContextProfile.CoreProfile)
fmt.setSamples(4)
fmt.setDepthBufferSize(24)
fmt.setStencilBufferSize(8)
QSurfaceFormat.setDefaultFormat(fmt)
QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts, True)
app = QApplication([sys.argv[0]])

from src.shared.config.config_manager import ConfigManager  # noqa: E402
from src.shared.feature_flags import load_feature_flags  # noqa: E402
from src.app.feature_registry import FeatureRegistry  # noqa: E402
from src.app.app_context import AppContext  # noqa: E402
from src.shared.i18n.i18n import I18N  # noqa: E402
from src.features.color.module import ColorModule  # noqa: E402
from src.features.edit.module import EditModule  # noqa: E402
from src.features.composite.module import CompositeModule  # noqa: E402
from src.app.main_window import MainWindow  # noqa: E402

config = ConfigManager()
flags = load_feature_flags(config)
I18N.load_locale("zh_CN")
registry = FeatureRegistry()
registry.initialize(AppContext(config_manager=config, feature_flags=flags, i18n=I18N, logger=None))
registry.register_module(ColorModule())
registry.register_module(EditModule())
comp = CompositeModule()
registry.register_module(comp)
comp.discover_and_register_plugins()
mw = MainWindow(feature_flags=flags, feature_registry=registry,
                test_mode=False, startup_no_dialog=True)
mw.resize(1440, 1000)
mw.show()
mw.switch_to_page("Edit")
tv = mw.timeline_panel.timeline_view
panel = mw.timeline_panel
ep = mw.edit_page
sv = ep._source_viewer
records: list[dict] = []


def wait(ms: int = 200) -> None:
    QTest.qWait(ms)


def shot(name: str, widget=None) -> str:
    path = OUT / (name + ".png")
    (widget or mw).grab().save(str(path))
    return str(path)


def record(name: str, **data) -> None:
    value = {"name": name, **data}
    records.append(value)
    (OUT / "interaction_records.json").write_text(
        json.dumps(records, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print("AUDIT " + json.dumps(value, ensure_ascii=False, default=str), flush=True)


def timeline_state() -> dict:
    tl = tv._timeline
    if tl is None:
        return {"timeline": None}
    return {"revision": tl.revision, "fps": tl.fps,
            "playhead": tv.get_playhead_frame(),
            "tracks": [{"id": t.track_id, "type": t.track_type, "locked": t.is_locked,
                        "clips": [{"id": c.clip_id, "start": c.timeline_start,
                                   "end": c.timeline_end, "in": c.source_start,
                                   "source_duration": c.source_duration, "speed": c.speed,
                                   "link": c.linked_clip_id} for c in t.clips]}
                       for t in tl.all_tracks]}


def drag(start: QPoint, end: QPoint, modifier=Qt.KeyboardModifier.NoModifier,
         steps: int = 12, release: bool = True) -> None:
    viewport = tv.viewport()
    QTest.mousePress(viewport, Qt.MouseButton.LeftButton, modifier, start)
    for index in range(1, steps + 1):
        p = start + (end - start) * (index / steps)
        e = QMouseEvent(QEvent.Type.MouseMove, QPointF(p),
                        QPointF(viewport.mapToGlobal(p)), Qt.MouseButton.NoButton,
                        Qt.MouseButton.LeftButton, modifier)
        QApplication.sendEvent(viewport, e)
        wait(20)
    if release:
        QTest.mouseRelease(viewport, Qt.MouseButton.LeftButton, modifier, end)
    wait(180)


def click(widget) -> None:
    QTest.mouseClick(widget, Qt.MouseButton.LeftButton)
    wait(150)


def poll() -> None:
    # qWait processes events; stop polling while a command is in progress to
    # prevent nested audit commands from interleaving their interactions.
    timer.stop()
    for command in sorted(SESSION.glob("command-*.py")):
        code = command.read_text(encoding="utf-8")
        (OUT / command.name).write_text(code, encoding="utf-8")
        command.unlink()
        try:
            exec(compile(code, str(command), "exec"), globals())
            (SESSION / (command.stem + ".done")).write_text("ok", encoding="utf-8")
        except BaseException:
            error = traceback.format_exc()
            record("command_exception", command=command.name, error=error)
            (SESSION / (command.stem + ".done")).write_text(error, encoding="utf-8")
    timer.start(500)


timer = QTimer()
timer.timeout.connect(poll)
timer.start(500)
QTimer.singleShot(2500, lambda: record("startup", page=mw.current_page,
                                    platform=app.platformName(), qt=src.main.QT_VERSION_STR
                                    if hasattr(src.main, "QT_VERSION_STR") else "6.10.0",
                                    screenshot=shot("01-empty")))
app.exec()
