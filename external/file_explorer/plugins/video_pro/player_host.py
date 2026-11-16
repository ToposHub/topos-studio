"""Compatibility shim: playback core moved to src/shared/playback.

Kept for the standalone file_explorer plugin/tests during the native migration.
Remove this package once the legacy Web UI is retired.
"""
from src.shared.playback.player_host import *  # noqa: F401,F403
