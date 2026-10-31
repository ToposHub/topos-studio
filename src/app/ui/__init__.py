"""App-level UI components for MainWindow.

Real implementations live in this package. src.ui.* files are thin
compatibility wrappers that re-export from here.
"""

from src.app.ui.resize_dialog import ResizeDialog
from src.app.ui.shortcut_dialog import ShortcutDialog
from src.app.ui.info_panel import InfoPanel
from src.app.ui.library_panel import LibraryPanel
from src.app.ui.files_panel import FilesPanel
from src.app.ui.audio_mixer import AudioMixerPanel
from src.app.ui.script_panel import ScriptPanel
from src.app.ui.preferences_dialog import PreferencesDialog
from src.app.ui.welcome_dialog import WelcomeDialog

__all__ = [
    "ResizeDialog",
    "ShortcutDialog",
    "InfoPanel",
    "LibraryPanel",
    "FilesPanel",
    "AudioMixerPanel",
    "ScriptPanel",
    "PreferencesDialog",
    "WelcomeDialog",
]
