"""Shared media playback core.

The player used to live inside ``external/file_explorer/plugins/video_pro``.
It now belongs to the application layer so the native Files panel can keep
previewing video after the legacy Web UI is retired.
"""
from __future__ import annotations

__all__ = [
    "base_player",
    "icons",
    "libav_player",
    "player_host",
    "player_qt",
    "topos_media",
    "topos_player",
]
