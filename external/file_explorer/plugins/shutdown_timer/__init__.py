import os
import logging
logger = logging.getLogger(__name__)

from flask import Blueprint, jsonify
from PyQt6.QtCore import QObject, pyqtSignal
from .timer_gui import ShutdownTimerApp

# --- Thread Bridge ---
class GuiLauncher(QObject):
    launch_signal = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.launch_signal.connect(self._open_on_main_thread)
        self.window = None

    def _open_on_main_thread(self):
        # Singleton-like behavior: If open, bring to front; if closed, recreate
        # [BUG-P2-39] isVisible()=False 有两种情况：正常关闭（应重建）与
        # 倒计时运行中被 closeEvent 隐藏到托盘（必须复用，否则双实例双托盘、
        # 重复下发关机命令）
        try:
            w = self.window
            need_new = w is None
            if not need_new:
                try:
                    need_new = (not w.isVisible()) and (not w.is_running)
                except RuntimeError:
                    need_new = True  # C++ 侧已删除
            if need_new:
                self.window = ShutdownTimerApp()

            self.window.show()
            self.window.raise_()
            self.window.activateWindow()
        except Exception as e:
            logger.error(f"Error launching timer: {e}")
            # Fallback if object was deleted C++ side
            self.window = ShutdownTimerApp()
            self.window.show()

_launcher = None

def setup(context):
    global _launcher
    _launcher = GuiLauncher()

    # 1. Register Top Menu Entry
    context.register_ui_component('top_menu', """
        <div class="dropdown-item" onclick="fetch('/api/plugin/shutdown_timer/launch', {method:'POST'})">
            <i class="bi bi-power me-2 text-danger"></i>定时关机
        </div>
    """)

# Blueprint
bp = Blueprint('shutdown_timer', __name__)

@bp.route('/launch', methods=['POST'])
def launch_gui():
    global _launcher
    if _launcher:
        _launcher.launch_signal.emit()
        return jsonify({'status': 'ok'})
    return jsonify({'status': 'error'}), 500
