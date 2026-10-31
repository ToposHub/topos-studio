"""Color-page playback audio bridge tests.

Color plays timeline clips through the graded ``video_controller`` (a pure
video engine).  The audio bridge mirrors that transport onto Edit's timeline
audio: ``play_state_changed`` starts/pauses ``AudioPlaybackManager`` at the
mapped timeline frame, and per-frame drift/wrap re-anchoring keeps the two
clocks aligned.  These tests pin the bridge wiring without opening any audio
device.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from src.app.controllers.file_controller import FileOpsMixin
from src.features.edit.core.timeline_v2 import ClipInstance
from src.features.edit.ui.edit_page import EditPage


class _FakeVideoBar:
    def __init__(self) -> None:
        self.current_frame = None

    def set_total_frames(self, total: int) -> None:
        pass

    def on_frame_changed(self, frame: int) -> None:
        self.current_frame = int(frame)


class _FakeVideoController:
    def __init__(self, current_frame: int = 0) -> None:
        self.current_frame = current_frame
        self.seek_calls: list[int] = []
        self.range_calls: list[tuple[int, int | None]] = []
        self.is_playing = False

    def seek_to_frame(self, frame: int) -> None:
        self.seek_calls.append(int(frame))
        self.current_frame = int(frame)

    def set_in_out_range(self, in_frame: int, out_frame: int | None) -> None:
        self.range_calls.append((int(in_frame), None if out_frame is None else int(out_frame)))

    def clear_in_out_range(self) -> None:
        pass


class _FakeAudioManager:
    def __init__(self, current_frame: int = 0, state_name: str = "playing") -> None:
        self.play_calls: list[int] = []
        self.pause_calls = 0
        self.seek_calls: list[int] = []
        self._current_frame = current_frame
        self._state_name = state_name

    def play(self, start_frame: int = 0) -> None:
        self.play_calls.append(int(start_frame))

    def pause(self) -> None:
        self.pause_calls += 1

    def seek(self, frame: int) -> None:
        self.seek_calls.append(int(frame))

    def get_state(self):
        return SimpleNamespace(name=self._state_name)

    def get_current_frame(self) -> int:
        return int(self._current_frame)


class _Controller(FileOpsMixin):
    def __init__(self, clip: ClipInstance, edit_page=None) -> None:
        self.current_page = "Color"
        self.canvas = type("Canvas", (), {"video_control_bar": _FakeVideoBar()})()
        self.video_controller = _FakeVideoController(current_frame=clip.source_start)
        self._color_timeline_clip_playback_context = None
        self._last_color_source_frame = None
        self._cross_clip_advancing = False
        self.edit_page = edit_page
        self._current_timeline_sequences = {}
        self._current_edit_project = None


def _clip() -> ClipInstance:
    # source 50..89, timeline 100..139
    return ClipInstance(
        clip_id="clip-color",
        asset_id="asset-color",
        track_id="v1",
        timeline_start=100,
        timeline_duration=40,
        source_start=50,
        source_duration=40,
    )


def _timeline_namespace(clip: ClipInstance) -> SimpleNamespace:
    return SimpleNamespace(timeline_id="tl-1", video_tracks=())


# ---------------------------------------------------------------------------
# play_state_changed -> EditPage public API wiring
# ---------------------------------------------------------------------------


def test_color_play_state_starts_audio_at_mapped_timeline_frame() -> None:
    clip = _clip()
    edit_page = MagicMock()
    controller = _Controller(clip, edit_page=edit_page)
    controller._set_color_timeline_clip_playback_context("tl-1", clip)
    timeline = _timeline_namespace(clip)
    controller._current_timeline_sequences = {"tl-1": timeline}
    controller.video_controller.current_frame = 73  # local 23 -> timeline 123

    controller._on_color_video_play_state_for_audio(True)

    edit_page.start_timeline_audio_playback.assert_called_once_with(timeline, 123)


def test_color_play_state_pause_mirrors_to_audio_pause() -> None:
    clip = _clip()
    edit_page = MagicMock()
    controller = _Controller(clip, edit_page=edit_page)
    controller._set_color_timeline_clip_playback_context("tl-1", clip)

    controller._on_color_video_play_state_for_audio(False)

    edit_page.pause_timeline_audio_playback.assert_called_once_with()
    edit_page.start_timeline_audio_playback.assert_not_called()


def test_color_play_state_ignores_non_color_pages() -> None:
    clip = _clip()
    edit_page = MagicMock()
    controller = _Controller(clip, edit_page=edit_page)
    controller._set_color_timeline_clip_playback_context("tl-1", clip)
    controller.current_page = "Composite"

    controller._on_color_video_play_state_for_audio(True)
    controller._on_color_video_play_state_for_audio(False)

    edit_page.start_timeline_audio_playback.assert_not_called()
    edit_page.pause_timeline_audio_playback.assert_not_called()


def test_color_play_state_ignores_missing_edit_page() -> None:
    clip = _clip()
    controller = _Controller(clip, edit_page=None)
    controller._set_color_timeline_clip_playback_context("tl-1", clip)

    # Must not raise even though there is no Edit page to drive.
    controller._on_color_video_play_state_for_audio(True)


def test_color_play_state_falls_back_to_start_of_clip() -> None:
    """No current/last frame yet (fresh context): anchor at the clip's in-frame."""
    clip = _clip()
    edit_page = MagicMock()
    controller = _Controller(clip, edit_page=edit_page)
    controller._set_color_timeline_clip_playback_context("tl-1", clip)
    controller.video_controller.current_frame = None

    controller._on_color_video_play_state_for_audio(True)

    edit_page.start_timeline_audio_playback.assert_called_once()
    assert edit_page.start_timeline_audio_playback.call_args.args[1] == 100


def test_color_play_state_uses_edit_page_timeline_when_sequence_missing() -> None:
    clip = _clip()
    edit_page = MagicMock()
    edit_page._current_timeline = _timeline_namespace(clip)
    controller = _Controller(clip, edit_page=edit_page)
    controller._set_color_timeline_clip_playback_context("tl-1", clip)

    controller._on_color_video_play_state_for_audio(True)

    edit_page.start_timeline_audio_playback.assert_called_once_with(
        edit_page._current_timeline, 100
    )


# ---------------------------------------------------------------------------
# per-frame drift / wrap re-anchoring
# ---------------------------------------------------------------------------


def test_color_frame_change_reanchors_audio_with_mapped_timeline_frame() -> None:
    clip = _clip()
    edit_page = MagicMock()
    controller = _Controller(clip, edit_page=edit_page)
    controller._set_color_timeline_clip_playback_context("tl-1", clip)
    controller.video_controller.is_playing = True

    controller._on_color_video_source_frame_changed(74)  # local 24 -> timeline 124

    edit_page.reanchor_timeline_audio.assert_called_once_with(124)


def test_color_frame_change_skips_reanchor_while_paused() -> None:
    clip = _clip()
    edit_page = MagicMock()
    controller = _Controller(clip, edit_page=edit_page)
    controller._set_color_timeline_clip_playback_context("tl-1", clip)
    controller.video_controller.is_playing = False

    controller._on_color_video_source_frame_changed(74)

    edit_page.reanchor_timeline_audio.assert_not_called()


def test_color_wrap_with_same_source_successor_skips_reanchor() -> None:
    """Backward jump + same-source neighbour: the advance path re-anchors audio.

    That path stops + seeks + restarts the engine, which re-fires
    ``play_state_changed(True)`` and starts audio at the successor's frame;
    re-anchoring here as well would seek audio backwards first (a glitch).
    """
    clip = _clip()
    next_clip = ClipInstance(
        clip_id="clip-next",
        asset_id="asset-next",
        track_id="v1",
        timeline_start=140,
        timeline_duration=40,
        source_start=90,
        source_duration=40,
    )

    class _Track:
        def get_clip_at(self, frame: int):
            for c in (clip, next_clip):
                if c.timeline_start <= frame < c.timeline_start + c.timeline_duration:
                    return c
            return None

    edit_page = MagicMock()
    controller = _Controller(clip, edit_page=edit_page)
    controller._set_color_timeline_clip_playback_context("tl-1", clip)
    controller._current_edit_project = SimpleNamespace(
        assets={
            clip.asset_id: SimpleNamespace(path="/tmp/source.mov"),
            next_clip.asset_id: SimpleNamespace(path="/tmp/source.mov"),
        }
    )
    controller._current_timeline_sequences = {
        "tl-1": SimpleNamespace(video_tracks=(_Track(),)),
    }
    controller.video_controller.is_playing = True
    controller._last_color_source_frame = 89  # tail of the clip...

    # ...then the wrap emission back to the clip's in-frame.
    controller._maybe_reanchor_color_audio(50, 0)

    edit_page.reanchor_timeline_audio.assert_not_called()


# ---------------------------------------------------------------------------
# EditPage public audio API (bridge targets)
# ---------------------------------------------------------------------------


def _edit_page(timeline, manager, *, active=True) -> EditPage:
    page = EditPage.__new__(EditPage)
    page._current_timeline = timeline
    page._audio_active = active
    page._audio_manager = manager
    return page


def test_edit_page_start_timeline_audio_plays_at_frame() -> None:
    timeline = _timeline_namespace(_clip())
    manager = _FakeAudioManager()
    page = _edit_page(timeline, manager)

    assert page.start_timeline_audio_playback(timeline, 123) is True
    assert manager.play_calls == [123]


def test_edit_page_start_timeline_audio_without_timeline_returns_false() -> None:
    manager = _FakeAudioManager()
    page = _edit_page(None, manager)

    assert page.start_timeline_audio_playback(None, 0) is False
    assert manager.play_calls == []


def test_edit_page_start_timeline_audio_resyncs_after_leave_shutdown() -> None:
    """on_leave() cleared the audio state; the bridge must rebuild it lazily."""
    timeline = _timeline_namespace(_clip())
    manager = _FakeAudioManager()
    page = _edit_page(timeline, manager, active=False)
    synced = []
    page._sync_audio_tracks_from_timeline = lambda: synced.append(True) or setattr(
        page, "_audio_active", True
    )

    assert page.start_timeline_audio_playback(timeline, 100) is True
    assert synced == [True]
    assert manager.play_calls == [100]


def test_edit_page_start_timeline_audio_follows_foreign_timeline() -> None:
    """A clip from another sequence must rebind Edit's timeline (audio coords)."""
    timeline_a = SimpleNamespace(timeline_id="tl-a")
    timeline_b = SimpleNamespace(timeline_id="tl-b")
    manager = _FakeAudioManager()
    page = _edit_page(timeline_a, manager)
    set_calls = []
    page.set_timeline = lambda tl: set_calls.append(tl) or setattr(
        page, "_current_timeline", tl
    )

    assert page.start_timeline_audio_playback(timeline_b, 5) is True
    assert set_calls == [timeline_b]
    assert manager.play_calls == [5]


def test_edit_page_pause_timeline_audio_pauses_manager() -> None:
    manager = _FakeAudioManager()
    page = _edit_page(_timeline_namespace(_clip()), manager)

    page.pause_timeline_audio_playback()

    assert manager.pause_calls == 1


def test_edit_page_reanchor_ignores_small_drift() -> None:
    manager = _FakeAudioManager(current_frame=120)  # 4 frames behind 124
    page = _edit_page(_timeline_namespace(_clip()), manager)

    page.reanchor_timeline_audio(124)

    assert manager.seek_calls == []


def test_edit_page_reanchor_seeks_on_large_drift() -> None:
    manager = _FakeAudioManager(current_frame=100)
    page = _edit_page(_timeline_namespace(_clip()), manager)

    page.reanchor_timeline_audio(200)

    assert manager.seek_calls == [200]


def test_edit_page_reanchor_seeks_on_loop_wrap_backwards() -> None:
    manager = _FakeAudioManager(current_frame=139)  # clip tail
    page = _edit_page(_timeline_namespace(_clip()), manager)

    page.reanchor_timeline_audio(100)  # wrapped back to the clip's in-frame

    assert manager.seek_calls == [100]


def test_edit_page_reanchor_ignores_paused_audio() -> None:
    manager = _FakeAudioManager(current_frame=0, state_name="paused")
    page = _edit_page(_timeline_namespace(_clip()), manager)

    page.reanchor_timeline_audio(200)

    assert manager.seek_calls == []
