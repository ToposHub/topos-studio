import logging
logger = logging.getLogger(__name__)

from flask import Blueprint, jsonify
from PyQt6.QtCore import QObject, pyqtSignal
from .image_gui import MainWindow

# --- Thread Bridge ---
class GuiLauncher(QObject):
    launch_signal = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.launch_signal.connect(self._open_on_main_thread)
        self.window = None

    def _open_on_main_thread(self):
        try:
            if self.window is None:
                self.window = MainWindow()
            
            self.window.show()
            self.window.raise_()
            self.window.activateWindow()
        except Exception as e:
            logger.error(f"Error launching Image Factory: {e}")
            # Recreate if dead
            self.window = MainWindow()
            self.window.show()

_launcher = None

def setup(context):
    global _launcher
    _launcher = GuiLauncher()

    # Register Top Menu Entry
    context.register_ui_component('top_menu', """
        <div class="dropdown-item" onclick="fetch('/api/plugin/image_factory/launch', {method:'POST'})">
            <i class="bi bi-images me-2 text-info"></i>图片工厂 Pro
        </div>
    """)

# Blueprint
bp = Blueprint('image_factory', __name__)

@bp.route('/launch', methods=['POST'])
def launch_gui():
    global _launcher
    if _launcher:
        _launcher.launch_signal.emit()
        return jsonify({'status': 'ok'})
    return jsonify({'status': 'error'}), 500
