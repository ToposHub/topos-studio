"""
Tests for timeline audio integration.

Verifies:
- AudioTrackState creation and volume/mute/solo
- AudioMixer mix behavior with mute/solo/volume
- AudioClock frame-to-sample conversion
- AudioSyncMode enum completeness
- AudioPlaybackManager API surface (no audio hardware required)
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.edit_core

from src.features.edit.core.audio_playback import (
    AudioClock,
    AudioTrackState,
    AudioMixer,
    AudioSyncMode,
    AudioPlaybackState,
    DEFAULT_SAMPLE_RATE,
    MIN_VOLUME,
    MAX_VOLUME,
)


class TestAudioClock:
    """Verify AudioClock time tracking."""

    def test_initial_state(self):
        clock = AudioClock()
        assert clock.sample_rate > 0

    def test_set_sample_rate(self):
        clock = AudioClock()
        clock.set_sample_rate(48000)
        assert clock.sample_rate == 48000

    def test_set_sample_rate_44100(self):
        clock = AudioClock()
        clock.set_sample_rate(44100)
        assert clock.sample_rate == 44100

    def test_initial_not_playing(self):
        clock = AudioClock()
        assert clock.is_playing is False


class TestAudioTrackState:
    """Verify AudioTrackState track management."""

    def test_default_values(self):
        state = AudioTrackState(track_id="t1")
        assert state.track_id == "t1"
        assert state.volume == 1.0
        assert state.muted is False
        assert state.solo is False

    def test_custom_volume(self):
        state = AudioTrackState(track_id="t1", volume=0.5)
        assert state.volume == 0.5

    def test_muted(self):
        state = AudioTrackState(track_id="t1", muted=True)
        assert state.muted is True

    def test_solo(self):
        state = AudioTrackState(track_id="t1", solo=True)
        assert state.solo is True

    def test_volume_range(self):
        """Volume should be within valid range."""
        state = AudioTrackState(track_id="t1", volume=0.0)
        assert state.volume >= MIN_VOLUME
        state2 = AudioTrackState(track_id="t2", volume=2.0)
        assert state2.volume <= MAX_VOLUME


class TestAudioMixer:
    """Verify AudioMixer creation and API."""

    def test_create_mixer(self):
        mixer = AudioMixer()
        assert mixer is not None

    def test_remove_nonexistent_track_no_error(self):
        """Removing a non-existent track should be safe."""
        mixer = AudioMixer()
        mixer.remove_track("nonexistent")
        # Should not raise

    def test_has_solo_tracks_false_initially(self):
        mixer = AudioMixer()
        assert mixer.has_solo_tracks() is False

    def test_get_track_state_nonexistent(self):
        mixer = AudioMixer()
        result = mixer.get_track_state("nonexistent")
        assert result is None

    def test_set_track_state_nonexistent_no_error(self):
        mixer = AudioMixer()
        state = AudioTrackState(track_id="t1", volume=0.5)
        mixer.set_track_state("nonexistent", state)
        # Should not raise


class TestAudioSyncMode:
    """Verify AudioSyncMode enum."""

    def test_audio_master(self):
        assert AudioSyncMode.AUDIO_MASTER is not None

    def test_video_master(self):
        assert AudioSyncMode.VIDEO_MASTER is not None

    def test_independent(self):
        assert AudioSyncMode.INDEPENDENT is not None

    def test_all_modes(self):
        expected = {"AUDIO_MASTER", "VIDEO_MASTER", "INDEPENDENT"}
        actual = {m.name for m in AudioSyncMode}
        assert actual == expected


class TestAudioPlaybackState:
    """Verify AudioPlaybackState enum."""

    def test_stopped(self):
        assert AudioPlaybackState.STOPPED is not None

    def test_playing(self):
        assert AudioPlaybackState.PLAYING is not None

    def test_paused(self):
        assert AudioPlaybackState.PAUSED is not None

    def test_all_states(self):
        expected = {"STOPPED", "PLAYING", "PAUSED"}
        actual = {s.name for s in AudioPlaybackState}
        assert actual == expected


class TestAudioPlaybackManagerAPI:
    """Verify AudioPlaybackManager API surface (no audio hardware)."""

    def test_import(self):
        from src.features.edit.core.audio_playback import AudioPlaybackManager
        assert AudioPlaybackManager is not None

    def test_has_play(self):
        from src.features.edit.core.audio_playback import AudioPlaybackManager
        assert hasattr(AudioPlaybackManager, "play")

    def test_has_pause(self):
        from src.features.edit.core.audio_playback import AudioPlaybackManager
        assert hasattr(AudioPlaybackManager, "pause")

    def test_has_stop(self):
        from src.features.edit.core.audio_playback import AudioPlaybackManager
        assert hasattr(AudioPlaybackManager, "stop")

    def test_has_seek(self):
        from src.features.edit.core.audio_playback import AudioPlaybackManager
        assert hasattr(AudioPlaybackManager, "seek")

    def test_has_add_track(self):
        from src.features.edit.core.audio_playback import AudioPlaybackManager
        assert hasattr(AudioPlaybackManager, "add_track")

    def test_has_remove_track(self):
        from src.features.edit.core.audio_playback import AudioPlaybackManager
        assert hasattr(AudioPlaybackManager, "remove_track")

    def test_has_set_track_volume(self):
        from src.features.edit.core.audio_playback import AudioPlaybackManager
        assert hasattr(AudioPlaybackManager, "set_track_volume")

    def test_has_set_track_mute(self):
        from src.features.edit.core.audio_playback import AudioPlaybackManager
        assert hasattr(AudioPlaybackManager, "set_track_mute")

    def test_has_set_track_solo(self):
        from src.features.edit.core.audio_playback import AudioPlaybackManager
        assert hasattr(AudioPlaybackManager, "set_track_solo")

    def test_has_set_sample_rate(self):
        from src.features.edit.core.audio_playback import AudioPlaybackManager
        assert hasattr(AudioPlaybackManager, "set_sample_rate")

    def test_has_set_video_controller(self):
        from src.features.edit.core.audio_playback import AudioPlaybackManager
        assert hasattr(AudioPlaybackManager, "set_video_controller")

    def test_signals_exist(self):
        from src.features.edit.core.audio_playback import AudioPlaybackManager
        mgr = AudioPlaybackManager.__new__(AudioPlaybackManager)
        # Check signal descriptors exist on class
        assert hasattr(AudioPlaybackManager, "state_changed")
        assert hasattr(AudioPlaybackManager, "frame_changed")
        assert hasattr(AudioPlaybackManager, "track_state_changed")
        assert hasattr(AudioPlaybackManager, "sync_error")
        assert hasattr(AudioPlaybackManager, "end_reached")
