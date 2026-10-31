"""
Tests for Source Viewer playback chain integrity.

Verifies:
- EditPlaybackController API completeness (play/pause/stop/seek/tick)
- Monotonic clock-based frame pacing correctness
- Range playback (set/clear/loop/stop-at-end)
- FPS configuration and timer interval
- PlaybackState transitions
"""

from __future__ import annotations

import pytest
import time

pytestmark = pytest.mark.edit_core

from src.features.edit.core.edit_playback_controller import (
    EditPlaybackController,
    PlaybackDirection,
    PlaybackState,
)


class TestPlaybackControllerAPI:
    """Verify EditPlaybackController has all required methods."""

    def test_has_play(self):
        c = EditPlaybackController()
        assert hasattr(c, "play")

    def test_has_pause(self):
        c = EditPlaybackController()
        assert hasattr(c, "pause")

    def test_has_stop(self):
        c = EditPlaybackController()
        assert hasattr(c, "stop")

    def test_has_seek(self):
        c = EditPlaybackController()
        assert hasattr(c, "seek")

    def test_has_tick(self):
        c = EditPlaybackController()
        assert hasattr(c, "tick")

    def test_has_current_frame(self):
        c = EditPlaybackController()
        assert hasattr(c, "current_frame")

    def test_has_state(self):
        c = EditPlaybackController()
        assert hasattr(c, "state")

    def test_has_set_fps(self):
        c = EditPlaybackController()
        assert hasattr(c, "set_fps")

    def test_has_set_timeline_duration(self):
        c = EditPlaybackController()
        assert hasattr(c, "set_timeline_duration")

    def test_has_set_playback_range(self):
        c = EditPlaybackController()
        assert hasattr(c, "set_playback_range")

    def test_has_clear_playback_range(self):
        c = EditPlaybackController()
        assert hasattr(c, "clear_playback_range")

    def test_has_playback_range(self):
        c = EditPlaybackController()
        assert hasattr(c, "playback_range")


class TestPlaybackStateTransitions:
    """Verify state transitions during playback."""

    def test_initial_state_is_stopped(self):
        c = EditPlaybackController()
        assert c.state == PlaybackState.STOPPED

    def test_play_transitions_to_playing(self):
        c = EditPlaybackController()
        c.set_fps(24.0)
        c.set_timeline_duration(240)
        c.play()
        assert c.state == PlaybackState.PLAYING

    def test_pause_transitions_to_paused(self):
        c = EditPlaybackController()
        c.set_fps(24.0)
        c.set_timeline_duration(240)
        c.play()
        c.pause()
        assert c.state == PlaybackState.PAUSED

    def test_stop_transitions_to_stopped(self):
        c = EditPlaybackController()
        c.set_fps(24.0)
        c.set_timeline_duration(240)
        c.play()
        c.stop()
        assert c.state == PlaybackState.STOPPED


class TestFramePacing:
    """Verify monotonic clock-based frame pacing."""

    def test_tick_advances_frame(self):
        c = EditPlaybackController()
        c.set_fps(24.0)
        c.set_timeline_duration(240)
        c.play()
        # Simulate several tick calls at frame intervals
        start_time = c._state.start_time
        for i in range(10):
            t = start_time + (i + 1) * (1.0 / 24.0)
            frame = c.tick(t)
        assert frame > 0

    def test_tick_clamps_to_duration(self):
        c = EditPlaybackController()
        c.set_fps(24.0)
        c.set_timeline_duration(100)
        c.play()
        start_time = c._state.start_time
        # Advance way past duration
        frame = c.tick(start_time + 100.0)
        assert frame <= 99  # last frame is duration - 1

    def test_seek_updates_current_frame(self):
        c = EditPlaybackController()
        c.set_fps(24.0)
        c.set_timeline_duration(240)
        c.seek(100)
        assert c.current_frame == 100

    def test_forward_playback_from_middle_advances_relative_to_seek(self):
        c = EditPlaybackController()
        c.set_fps(24.0)
        c.set_timeline_duration(240)
        c.seek(100)
        c.play()

        frame = c.tick(c._state.start_time + (5.0 / 24.0))

        assert frame == 105

    def test_reverse_playback_from_middle_decrements_relative_to_seek(self):
        c = EditPlaybackController()
        c.set_fps(24.0)
        c.set_timeline_duration(240)
        c.seek(100)
        c.play_with_speed(PlaybackDirection.BACKWARD, 1.0)

        frame = c.tick(c._state.start_time + (5.0 / 24.0))

        assert frame == 95

    def test_seek_clamps_negative(self):
        c = EditPlaybackController()
        c.set_fps(24.0)
        c.set_timeline_duration(240)
        c.seek(-10)
        assert c.current_frame == 0

    def test_seek_clamps_past_end(self):
        c = EditPlaybackController()
        c.set_fps(24.0)
        c.set_timeline_duration(240)
        c.seek(500)
        assert c.current_frame <= 239


class TestRangePlayback:
    """Verify range-based playback."""

    def test_set_playback_range(self):
        c = EditPlaybackController()
        c.set_fps(24.0)
        c.set_timeline_duration(240)
        c.set_playback_range(48, 144)
        assert c.playback_range == (48, 144)

    def test_clear_playback_range(self):
        c = EditPlaybackController()
        c.set_fps(24.0)
        c.set_timeline_duration(240)
        c.set_playback_range(48, 144)
        c.clear_playback_range()
        assert c.playback_range is None

    def test_range_loop_at_end(self):
        c = EditPlaybackController()
        c.set_fps(24.0)
        c.set_timeline_duration(240)
        c.set_loop(True)
        c.set_playback_range(48, 144)
        c.seek(48)
        c.play()
        start_time = c._state.start_time
        # Advance past range end
        elapsed = (144 - 48 + 10) / 24.0
        frame = c.tick(start_time + elapsed)
        assert frame == 48  # looped back to start

    def test_range_stop_at_end_no_loop(self):
        c = EditPlaybackController()
        c.set_fps(24.0)
        c.set_timeline_duration(240)
        c.set_loop(False)
        c.set_playback_range(48, 144)
        c.seek(48)
        c.play()
        start_time = c._state.start_time
        elapsed = (144 - 48 + 10) / 24.0
        frame = c.tick(start_time + elapsed)
        assert frame == 144
        assert c.state == PlaybackState.STOPPED


class TestFPSConfiguration:
    """Verify FPS configuration."""

    def test_default_fps(self):
        c = EditPlaybackController()
        assert c._fps == 24.0  # default

    def test_set_fps_custom(self):
        c = EditPlaybackController()
        c.set_fps(30.0)
        assert c._fps == 30.0

    def test_set_fps_25(self):
        c = EditPlaybackController()
        c.set_fps(25.0)
        assert c._fps == 25.0

    def test_set_fps_affects_pacing(self):
        """Verify that higher FPS yields proportionally more frames.

        Note: tick() is designed to be called frequently by a timer.
        It uses a drop-frame mechanism that limits advancement per call.
        So we call tick multiple times at appropriate intervals to simulate
        real playback.
        """
        n_ticks = 100

        # 24 fps controller
        c = EditPlaybackController()
        c.set_fps(24.0)
        c.set_timeline_duration(24000)
        c.play()
        start_time = c._state.start_time
        for i in range(n_ticks):
            t = start_time + (i + 1) * (1.0 / 24.0)  # one frame interval per tick
            c.tick(t)
        frame_24 = c._state.current_frame

        # 48 fps controller
        c2 = EditPlaybackController()
        c2.set_fps(48.0)
        c2.set_timeline_duration(48000)
        c2.play()
        start_time2 = c2._state.start_time
        for i in range(n_ticks):
            t = start_time2 + (i + 1) * (1.0 / 24.0)
            c2.tick(t)
        frame_48 = c2._state.current_frame

        # 48fps should advance ~2x more frames with same number of ticks
        assert frame_48 > frame_24
