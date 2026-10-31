"""Run the production entry point and accept local, reviewable audit steps.

Usage: .venv/bin/python docs/audit/2026-10-21-edit-full-review/scripts/interactive_session.py
Write numbered Python steps under $TOPOS_AUDIT_SESSION/commands (default /tmp/topos-edit-audit-20261021).
Each step runs in Qt's GUI thread. This driver only adds test instrumentation;
application edits must still use the production UI/command/service interfaces.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import traceback
from typing import Any

ROOT = Path(__file__).resolve().parents[4]
EVIDENCE = Path(__file__).resolve().parents[1]
SESSION = Path(os.environ.get("TOPOS_AUDIT_SESSION", "/tmp/topos-edit-audit-20261021"))
COMMANDS = SESSION / "commands"
for directory in (COMMANDS, EVIDENCE / "records", EVIDENCE / "screenshots", EVIDENCE / "logs"):
    directory.mkdir(parents=True, exist_ok=True)
sys.path.insert(0, str(ROOT))
os.environ["TOPOS_CONFIG_PATH"] = str(SESSION / "config.json")
os.environ["TOPOS_TEST_MODE"] = "1"  # Disable update checks; no renderer mocks.
os.environ["TOPOS_PRODUCT_MODE"] = "studio"
os.environ["OPENBLAS_NUM_THREADS"] = "1"
(SESSION / "config.json").write_text(json.dumps({
    "locale": "zh_CN", "current_page": "Edit", "work_mode": "video",
    "auto_load_last_project": False, "check_updates_on_startup": False,
    "show_welcome_on_startup": False,
}), encoding="utf-8")

import src.main as entry  # Native-symbol isolation and WebEngine before Qt app.
from PyQt6.QtCore import QPoint, QPointF, Qt, QTimer
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QDialog, QMenu, QWidget
from src.app.main_window import MainWindow

namespace: dict[str, Any] = globals()
busy = False
timer: QTimer | None = None


def save_record(name: str, data: Any) -> None:
    """Persist observations before proceeding to the next test step."""
    (EVIDENCE / "records" / f"{name}.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    print("AUDIT " + name + " " + json.dumps(data, ensure_ascii=False, default=str), flush=True)


def capture(name: str, widget: QWidget | None = None) -> str:
    """Capture the actual on-screen Qt widget using its existing paint path."""
    path = EVIDENCE / "screenshots" / f"{name}.png"
    target = widget if widget is not None else namespace["mw"]
    if not target.grab().save(str(path)):
        raise RuntimeError(f"Failed to save screenshot: {path}")
    return str(path)


def on_exception(exc_type: type[BaseException], value: BaseException, tb: Any) -> None:
    """Record unhandled Qt slot errors without hiding the traceback."""
    detail = "".join(traceback.format_exception(exc_type, value, tb))
    with (EVIDENCE / "logs" / "slot_exceptions.log").open("a", encoding="utf-8") as stream:
        stream.write(detail + "\n")
    print(detail, file=sys.stderr, flush=True)


def poll_commands() -> None:
    """Execute one complete file; qWait must not cause recursive execution."""
    global busy
    if busy:
        return
    pending = sorted(COMMANDS.glob("[0-9]*.py"))
    if not pending:
        return
    busy = True
    path = pending[0]
    source = path.read_text(encoding="utf-8")
    running = path.with_suffix(".running")
    path.rename(running)
    namespace["result"] = None
    try:
        exec(compile(source, str(path), "exec"), namespace)
        save_record(path.stem, {"ok": True, "result": namespace.get("result")})
    except BaseException as exc:
        save_record(path.stem, {"ok": False, "error": str(exc), "traceback": traceback.format_exc()})
    finally:
        running.rename(path.with_suffix(".done"))
        busy = False


original_show = MainWindow.show


def show_with_audit(self: MainWindow) -> None:
    """Attach after normal production construction; never replace renderers."""
    global timer
    original_show(self)
    namespace["mw"] = self
    namespace["app"] = QApplication.instance()
    self.resize(1440, 1000)
    timer = QTimer(self)
    timer.timeout.connect(poll_commands)
    timer.start(150)
    save_record("startup", {"entry": "src.main.main", "visible": self.isVisible(),
                           "page": self.current_page, "root": str(ROOT),
                           "config": os.environ["TOPOS_CONFIG_PATH"]})


MainWindow.show = show_with_audit
sys.excepthook = on_exception
sys.argv = [str(ROOT / "src/main.py"), "--product", "studio", "--no-startup-dialog"]
entry.main()
