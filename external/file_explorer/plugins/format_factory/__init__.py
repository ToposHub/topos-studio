import os
from flask import Blueprint, request, jsonify
from PyQt6.QtCore import QObject, pyqtSignal
from .converter_gui import MainWindow

# --- Thread Bridge (The Life Saver) ---
class GuiLauncher(QObject):
    launch_signal = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self.launch_signal.connect(self._open_on_main_thread)
        self.window = None

    def _open_on_main_thread(self, file_path):
        """Runs on Main Thread to safely create UI"""
        if self.window is None:
            self.window = MainWindow(file_path)
        else:
            if file_path:
                try:
                    self.window.tab_1.add_card(file_path)
                    self.window.tab_2.add_card(file_path)
                except: pass
        
        self.window.show()
        self.window.raise_()
        self.window.activateWindow()

# Global instance
_launcher = None

def setup(context):
    global _launcher
    _launcher = GuiLauncher()

    # 1. Register JS
    context.register_js('/static_plugin/format_factory/plugin.js')
    
    # 2. Register Context Menu (Right Click)
    context.register_ui_component('context_menu', """
        <div class="menu-divider"></div>
        <div class="menu-item" onclick="FormatFactoryPlugin.open(window.STATE.contextTarget ? window.STATE.contextTarget.path : null)">
            <i class="bi bi-magic me-2 text-primary"></i>转码 / 格式工厂 Pro
        </div>
    """)

    # 3. Register Top Menu (Plugin Bar) - NEW FEATURE
    # Note: Passing 'null' to open() triggers the logic to use current selection
    context.register_ui_component('top_menu', """
        <div class="dropdown-item" onclick="FormatFactoryPlugin.open(null)">
            <i class="bi bi-magic me-2 text-primary"></i>格式工厂 Pro
        </div>
    """)

# Blueprint
bp = Blueprint('format_factory', __name__)

@bp.route('/launch', methods=['POST'])
def launch_gui():
    global _launcher
    data = request.json
    path = data.get('path')
    if _launcher:
        _launcher.launch_signal.emit(path or "")
        return jsonify({'status': 'ok'})
    return jsonify({'status': 'error'}), 500
