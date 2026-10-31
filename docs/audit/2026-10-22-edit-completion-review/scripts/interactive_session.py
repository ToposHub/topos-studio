"""Visible production app instrumentation for round-seven verification.

Numbered scripts in /tmp/topos-round7-gui/commands execute on the Qt thread.
Edits must use existing production command/service/UI entry points.
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
SESSION = Path('/tmp/topos-round7-gui')
COMMANDS = SESSION / 'commands'
for directory in (COMMANDS, EVIDENCE / 'records', EVIDENCE / 'screenshots', EVIDENCE / 'logs'):
    directory.mkdir(parents=True, exist_ok=True)
sys.path.insert(0, str(ROOT))
os.environ['TOPOS_CONFIG_PATH'] = str(SESSION / 'config.json')
os.environ['TOPOS_TEST_MODE'] = '1'
os.environ['TOPOS_PRODUCT_MODE'] = 'studio'
os.environ['OPENBLAS_NUM_THREADS'] = '1'
(SESSION / 'config.json').write_text(json.dumps({
    'locale': 'zh_CN', 'current_page': 'Edit', 'work_mode': 'video',
    'auto_load_last_project': False, 'check_updates_on_startup': False,
    'show_welcome_on_startup': False,
}), encoding='utf-8')
import src.main as entry
from PyQt6.QtCore import QPoint, QPointF, Qt, QTimer
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QDialog, QMenu, QWidget
from src.app.main_window import MainWindow

namespace: dict[str, Any] = globals()
busy = False


def save_record(name: str, data: Any) -> None:
    (EVIDENCE / 'records' / f'{name}.json').write_text(
        json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding='utf-8')
    print('ROUND7 ' + name, flush=True)


def capture(name: str) -> None:
    if not namespace['mw'].grab().save(str(EVIDENCE / 'screenshots' / f'{name}.png')):
        raise RuntimeError('capture failed')


def exception_hook(kind: Any, value: Any, tb: Any) -> None:
    detail = ''.join(traceback.format_exception(kind, value, tb))
    with (EVIDENCE / 'logs' / 'slot_exceptions.log').open('a') as stream:
        stream.write(detail + '\n')
    print(detail, file=sys.stderr, flush=True)


def poll() -> None:
    global busy
    if busy:
        return
    pending = sorted(COMMANDS.glob('[0-9]*.py'))
    if not pending:
        return
    busy = True
    path = pending[0]
    running = path.with_suffix('.running')
    path.rename(running)
    namespace['result'] = None
    try:
        exec(compile(running.read_text(), str(path), 'exec'), namespace)
        save_record(path.stem, {'ok': True, 'result': namespace.get('result')})
    except BaseException as exc:
        save_record(path.stem, {'ok': False, 'error': str(exc), 'traceback': traceback.format_exc()})
    finally:
        running.rename(path.with_suffix('.done'))
        busy = False


original_show = MainWindow.show


def show_with_audit(self: MainWindow) -> None:
    original_show(self)
    namespace['mw'] = self
    self.resize(1440, 1000)
    timer = QTimer(self)
    timer.timeout.connect(poll)
    timer.start(150)
    namespace['audit_timer'] = timer
    save_record('startup', {'visible': self.isVisible(), 'entry': 'src.main.main', 'page': self.current_page})


MainWindow.show = show_with_audit
sys.excepthook = exception_hook
sys.argv = [str(ROOT / 'src/main.py'), '--product', 'studio', '--no-startup-dialog']
entry.main()
