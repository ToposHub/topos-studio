"""Idle-deferred Color clip-strip refresh after grading-stack commits.

A grading commit used to rebuild the whole Timeline Clips strip eagerly.
``_rebuild_cards`` destroys every card and the recreated cards paint the
dark placeholder until their thumbnails re-arrive asynchronously — the
black flash on every clip switch and every cross-clip playback advance.
These tests pin the DaVinci-style replacement: debounce while paused,
never rebuild while playback is running.
"""

from __future__ import annotations

import sys
import time
import unittest
from unittest.mock import MagicMock

from PyQt6.QtWidgets import QApplication, QWidget

from src.app.controllers.edit_controller import (
    EditControllerMixin,
    _IDLE_CLIP_STRIP_REFRESH_MS,
)

# Module-level holder keeping the QApplication alive for the whole session.
_app: QApplication | None = None


class _Host(QWidget, EditControllerMixin):
    """Minimal MainWindow stand-in exercising only the mixin's timer logic."""

    def __init__(self) -> None:
        super().__init__()
        self.video_controller = MagicMock()
        self.video_controller.is_playing = False
        self.refresh_calls = 0

    def refresh_color_timeline_clip_strip(self) -> None:
        self.refresh_calls += 1


def _ensure_app() -> QApplication:
    """Create the session QApplication, held by a module global.

    A local reference would be garbage-collected when the creator returns,
    and PyQt destroys the C++ QApplication with it — every later QWidget
    construction then aborts with "Must construct a QApplication first".
    """
    global _app
    if _app is None:
        _app = QApplication.instance() or QApplication(sys.argv)
    return _app


def _pump_events(seconds: float) -> None:
    """Process events while real time passes so QTimers can fire."""
    app = QApplication.instance()
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)


class TestIdleClipStripRefresh(unittest.TestCase):

    @classmethod
    def setUpClass(cls) -> None:
        _ensure_app()

    def setUp(self) -> None:
        self.host = _Host()

    def tearDown(self) -> None:
        timer = getattr(self.host, "_idle_clip_strip_refresh_timer", None)
        if timer is not None:
            timer.stop()
        self.host.deleteLater()

    def test_commit_burst_collapses_into_single_refresh(self) -> None:
        """Repeated commits debounce: one rebuild after the quiet period."""
        for _ in range(5):
            self.host._schedule_idle_color_timeline_clip_strip_refresh()
            _pump_events(0.05)

        self.assertEqual(self.host.refresh_calls, 0)
        _pump_events(_IDLE_CLIP_STRIP_REFRESH_MS / 1000.0 + 0.15)
        self.assertEqual(self.host.refresh_calls, 1)

    def test_no_refresh_while_playback_running(self) -> None:
        """Playback suppresses the rebuild entirely (no thumbnail flash)."""
        self.host.video_controller.is_playing = True
        self.host._schedule_idle_color_timeline_clip_strip_refresh()
        _pump_events(_IDLE_CLIP_STRIP_REFRESH_MS / 1000.0 + 0.3)

        self.assertEqual(self.host.refresh_calls, 0)

        # Playback stops -> the pending refresh finally runs.
        self.host.video_controller.is_playing = False
        _pump_events(_IDLE_CLIP_STRIP_REFRESH_MS / 1000.0 + 0.15)
        self.assertEqual(self.host.refresh_calls, 1)

    def test_missing_video_controller_refreshes_when_idle(self) -> None:
        """A host without a video controller is treated as not playing."""
        self.host.video_controller = None
        self.host._schedule_idle_color_timeline_clip_strip_refresh()
        _pump_events(_IDLE_CLIP_STRIP_REFRESH_MS / 1000.0 + 0.15)

        self.assertEqual(self.host.refresh_calls, 1)


if __name__ == "__main__":
    unittest.main()
