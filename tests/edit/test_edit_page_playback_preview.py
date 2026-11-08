from __future__ import annotations

import time
from types import SimpleNamespace

from PyQt6.QtCore import QObject, pyqtSignal


class DummyAudioManager(QObject):
    frame_changed = pyqtSignal(int)


def _process_events_for(qapp, ms: int = 30) -> None:
    deadline = time.monotonic() + ms / 1000.0
    while time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.001)


def test_playhead_change_skips_preview_request_when_page_inactive(qapp) -> None:
    """A hidden Edit page must not drive its preview renderer.

    While Composite/Color owns playback, the hidden renderer would decode
    every frame a second time and its backpressure would slow the shared
    playback clock (Composite-page stutter). on_enter() re-renders the
    current frame, so nothing goes stale.
    """
    from src.features.edit.ui.edit_page import EditPage

    page = EditPage()
    requested_frames: list[int] = []
    page.timeline_frame_requested.connect(requested_frames.append)

    assert not page.is_active()
    page._on_playhead_changed(42)
    assert requested_frames == []

    page._is_active = True
    page._on_playhead_changed(43)
    assert requested_frames == [43]


def test_playhead_change_uses_current_edit_page_as_lifecycle_fallback(qapp) -> None:
    """A stale lifecycle flag must not freeze playback on the visible Edit page."""
    from src.features.edit.ui.edit_page import EditPage

    page = EditPage()
    page._main_window = type("Window", (), {"current_page": "Edit"})()
    requested_frames: list[int] = []
    page.timeline_frame_requested.connect(requested_frames.append)

    page._on_playhead_changed(42)

    assert requested_frames == [42]

    page._main_window.current_page = "Color"
    page._on_playhead_changed(43)
    assert requested_frames == [42]


def test_leaving_edit_pauses_active_timeline_playback(qapp) -> None:
    """Color must not compete with a hidden Edit playback pipeline."""
    from src.features.edit.ui.edit_page import EditPage

    class DummyController:
        is_playing = True

    class DummyTimelinePanel:
        def __init__(self) -> None:
            self.controller = DummyController()
            self.pause_calls = 0

        def get_playback_controller(self) -> DummyController:
            return self.controller

        def pause_playback(self) -> None:
            self.pause_calls += 1
            self.controller.is_playing = False

    page = EditPage()
    panel = DummyTimelinePanel()
    page._timeline_panel = panel
    stopped_events: list[bool] = []
    page.signal_hub.playback_stopped.connect(lambda: stopped_events.append(True))

    page.on_leave()

    assert panel.pause_calls == 1
    assert not panel.controller.is_playing
    assert stopped_events == [True]


def test_leaving_edit_fully_cleans_audio_workers(qapp) -> None:
    """Leaving Edit must join playback and scrub QThreads before teardown."""
    from src.features.edit.ui.edit_page import EditPage

    class DummyAudioManager:
        def __init__(self) -> None:
            self.cleanup_calls = 0

        def cleanup(self) -> None:
            self.cleanup_calls += 1

    page = EditPage()
    audio = DummyAudioManager()
    page._audio_manager = audio
    page._audio_active = True
    page._audio_timeline_signature = ("audio",)

    page.on_leave()

    assert audio.cleanup_calls == 1
    assert not page._audio_active
    assert page._audio_timeline_signature is None


def test_leaving_color_stops_its_hidden_playback_session() -> None:
    """A hidden Color source player must not contend with the next page."""
    from src.app.controllers.page_controller import PageManagementMixin

    class DummyController:
        def __init__(self) -> None:
            self.is_playing = True
            self.stop_calls = 0

        def stop_playback(self) -> None:
            self.stop_calls += 1
            self.is_playing = False

    class DummyPageManager(PageManagementMixin):
        def __init__(self) -> None:
            self.feature_registry = None
            self.video_controller = DummyController()

    manager = DummyPageManager()
    manager.leave_page_hook("Color")

    assert manager.video_controller.stop_calls == 1
    assert not manager.video_controller.is_playing


def test_seek_after_playhead_change_deduplicates_preview_request_but_keeps_audio_seek(qapp) -> None:
    from src.features.edit.ui.edit_page import EditPage

    class DummyTimelineAudio:
        def __init__(self) -> None:
            self.seeked: list[int] = []

        def seek(self, frame: int) -> None:
            self.seeked.append(frame)

    page = EditPage()
    audio = DummyTimelineAudio()
    requested_frames: list[int] = []

    page._audio_manager = audio
    page._audio_active = True
    page._is_active = True
    page.timeline_frame_requested.connect(requested_frames.append)

    page._on_playhead_changed(42)
    page._on_seek_requested(42)

    assert requested_frames == [42]
    assert audio.seeked == [42]


def test_scrub_routes_to_lightweight_preview_until_release(qapp) -> None:
    from src.features.edit.ui.edit_page import EditPage

    class DummyTimelineAudio:
        def __init__(self) -> None:
            self.seeked: list[int] = []

        def seek(self, frame: int) -> None:
            self.seeked.append(frame)

    page = EditPage()
    audio = DummyTimelineAudio()
    exact_requests: list[int] = []
    scrub_starts: list[int] = []
    scrub_requests: list[int] = []
    scrub_finishes: list[int] = []

    page._audio_manager = audio
    page._audio_active = True
    page.timeline_frame_requested.connect(exact_requests.append)
    page.timeline_scrub_started.connect(scrub_starts.append)
    page.timeline_scrub_frame_requested.connect(scrub_requests.append)
    page.timeline_scrub_finished.connect(scrub_finishes.append)

    page._on_scrub_started(10)
    page._on_playhead_changed(12)
    page._on_seek_requested(12)
    _process_events_for(qapp)
    page._on_scrub_finished(12)

    assert scrub_starts == [10]
    assert scrub_requests == [10, 12]
    assert exact_requests == []
    assert scrub_finishes == [12]
    assert audio.seeked == [12]


def test_scrub_release_prefers_audio_preroll_when_available(qapp) -> None:
    from src.features.edit.ui.edit_page import EditPage

    class DummyTimelineAudio:
        def __init__(self) -> None:
            self.prepared: list[int] = []
            self.seeked: list[int] = []

        def get_state(self):
            class State:
                name = "STOPPED"
            return State()

        def prepare_for_playback(self, frame: int) -> None:
            self.prepared.append(frame)

        def seek(self, frame: int) -> None:
            self.seeked.append(frame)

    page = EditPage()
    audio = DummyTimelineAudio()
    page._audio_manager = audio
    page._audio_active = True

    page._on_scrub_started(10)
    page._on_scrub_finished(12)

    assert audio.prepared == [12]
    assert audio.seeked == []


def test_scrub_start_requests_audible_audio_preview(qapp) -> None:
    from src.features.edit.ui.edit_page import EditPage

    class DummyTimelineAudio:
        def __init__(self) -> None:
            self.previewed: list[tuple[int, int]] = []

        def get_state(self):
            class State:
                name = "STOPPED"
            return State()

        def preview_scrub_audio(self, frame: int, duration_ms: int) -> bool:
            self.previewed.append((frame, duration_ms))
            return True

    page = EditPage()
    audio = DummyTimelineAudio()
    page._audio_manager = audio
    page._audio_active = True

    page._on_scrub_started(10)

    assert audio.previewed == [(10, page._scrub_audio_preview_duration_ms)]


def test_scrub_audio_preview_is_throttled_during_fast_moves(qapp) -> None:
    from src.features.edit.ui.edit_page import EditPage

    class DummyTimelineAudio:
        def __init__(self) -> None:
            self.previewed: list[int] = []

        def get_state(self):
            class State:
                name = "STOPPED"
            return State()

        def preview_scrub_audio(self, frame: int, duration_ms: int) -> bool:
            self.previewed.append(frame)
            return True

    page = EditPage()
    audio = DummyTimelineAudio()
    page._audio_manager = audio
    page._audio_active = True
    page._scrub_audio_request_interval_ms = 1000

    page._on_scrub_started(10)
    page._on_scrub_moved(11)
    page._on_scrub_moved(12)

    assert audio.previewed == [10]


def test_scrub_moved_directly_requests_preview_when_playhead_signal_is_missing(qapp) -> None:
    from src.features.edit.ui.edit_page import EditPage

    page = EditPage()
    scrub_starts: list[int] = []
    scrub_requests: list[int] = []

    page.timeline_scrub_started.connect(scrub_starts.append)
    page.timeline_scrub_frame_requested.connect(scrub_requests.append)

    page._on_scrub_moved(15)
    page._on_scrub_moved(16)
    _process_events_for(qapp)

    assert scrub_starts == [15]
    assert scrub_requests == [15, 16]
    assert page._current_frame == 16


def test_scrub_moved_deduplicates_playhead_changed_preview_request(qapp) -> None:
    from src.features.edit.ui.edit_page import EditPage

    page = EditPage()
    scrub_requests: list[int] = []
    page.timeline_scrub_frame_requested.connect(scrub_requests.append)

    page._on_scrub_started(10)
    page._on_playhead_changed(12)
    page._on_scrub_moved(12)
    _process_events_for(qapp)

    assert scrub_requests == [10, 12]


def test_scrub_moved_coalesces_high_frequency_frames_to_latest(qapp) -> None:
    from src.features.edit.ui.edit_page import EditPage

    page = EditPage()
    scrub_requests: list[int] = []
    page.timeline_scrub_frame_requested.connect(scrub_requests.append)

    page._on_scrub_started(10)
    page._on_scrub_moved(11)
    page._on_scrub_moved(12)
    page._on_scrub_moved(13)
    _process_events_for(qapp)

    assert scrub_requests == [10, 13]


def test_set_timeline_panel_wires_scrub_moved_to_preview_request(qapp) -> None:
    from src.features.edit.ui.edit_page import EditPage

    class DummyTimelineView(QObject):
        playhead_changed = pyqtSignal(int)
        scrub_started = pyqtSignal(int)
        scrub_moved = pyqtSignal(int)
        scrub_finished = pyqtSignal(int)
        open_in_composite_requested = pyqtSignal(str, str)
        open_in_color_requested = pyqtSignal(str, str, str)
        # set_timeline_panel → _restore_scene_cut_overlay_state 会向视图推状态
        def set_scene_cut_overlay_visible(self, visible: bool) -> None:
            self.scene_cut_overlay_visible = visible

    class DummyPanel(QObject):
        seek_requested = pyqtSignal(int)
        playback_reanchored = pyqtSignal(int)
        create_comp_clip_requested = pyqtSignal(str, str)
        reset_composite_requested = pyqtSignal(str, str)
        remove_composite_binding_requested = pyqtSignal(str, str)
        bypass_composite_requested = pyqtSignal(str, str)
        reveal_comp_clip_in_media_pool_requested = pyqtSignal(str, str)
        render_cache_clip_requested = pyqtSignal(str, str)
        rerender_cache_clip_requested = pyqtSignal(str, str)
        # 以下为 set_timeline_panel 无守卫 connect 的信号（对齐真实
        # TimelinePanel 签名；本测试不发射它们）
        use_shared_grade_requested = pyqtSignal(str, str)
        make_local_grade_copy_requested = pyqtSignal(str, str)
        create_nested_from_selection_requested = pyqtSignal(list)
        enter_nested_sequence_requested = pyqtSignal(str, str)
        multicam_angle_key_requested = pyqtSignal(int)
        source_insert_requested = pyqtSignal()
        source_overwrite_requested = pyqtSignal()
        source_append_requested = pyqtSignal()
        # W09：集中绑定面（Resolve 工具栏请求；真实 TimelinePanel 均具备）
        source_replace_requested = pyqtSignal()
        source_patch_changed = pyqtSignal(str, str, bool)
        keyframe_tray_requested = pyqtSignal(bool)
        voiceover_requested = pyqtSignal()
        flag_requested = pyqtSignal(str)

        def __init__(self, view: DummyTimelineView) -> None:
            super().__init__()
            self._view = view

        def get_timeline_view(self) -> DummyTimelineView:
            return self._view

    page = EditPage()
    view = DummyTimelineView()
    panel = DummyPanel(view)
    scrub_requests: list[int] = []
    page.timeline_scrub_frame_requested.connect(scrub_requests.append)

    page.set_timeline_panel(panel)
    view.scrub_started.emit(10)
    view.scrub_moved.emit(11)
    view.scrub_moved.emit(12)
    _process_events_for(qapp)

    assert scrub_requests == [10, 12]


def test_timeline_panel_emits_only_final_seek_during_scrub(qapp) -> None:
    from src.features.edit.ui.timeline_panel import TimelinePanel

    panel = TimelinePanel()
    panel._playback_controller.set_timeline_duration(100)
    seek_requests: list[int] = []
    panel.seek_requested.connect(seek_requests.append)

    panel._on_scrub_started(10)
    panel._on_playhead_changed(12)
    panel._on_scrub_finished(12)

    assert seek_requests == [12]


def test_timeline_panel_pauses_playback_when_scrub_starts(qapp) -> None:
    from src.features.edit.ui.timeline_panel import TimelinePanel

    panel = TimelinePanel()
    panel._playback_controller.set_timeline_duration(100)
    panel._playback_controller.play()

    assert panel._playback_controller.is_playing

    panel._on_scrub_started(10)

    assert not panel._playback_controller.is_playing


def test_set_timeline_refresh_uses_timeline_frame_path(qapp) -> None:
    from src.features.edit.ui.edit_page import EditPage

    class DummyTimeline:
        fps = 24.0

    page = EditPage()
    page._current_frame = 17
    requested_frames: list[int] = []
    page.timeline_frame_requested.connect(requested_frames.append)

    page.set_timeline(DummyTimeline())

    assert requested_frames == [17]
    assert not hasattr(page.signal_hub, "frame_requested")


def test_set_empty_timeline_clears_program_viewer_frame(qapp) -> None:
    """An empty timeline must not keep the previous timeline's preview frame."""
    from src.features.edit.core.timeline_v2 import TimelineSequence
    from src.features.edit.ui.edit_page import EditPage

    class DummyViewer:
        def __init__(self) -> None:
            self.clear_calls = 0

        def clear_texture(self) -> None:
            self.clear_calls += 1

        def set_timeline_choices(self, _timelines, _active_id) -> None:
            pass

        def set_guide(self, _guide_type, _guide_id, _params) -> None:
            pass

    page = EditPage()
    viewer = DummyViewer()
    page._program_viewer = viewer

    page.set_timeline(TimelineSequence(timeline_id="empty", name="Empty"))

    assert viewer.clear_calls == 1


def test_audio_sync_skips_rebuild_when_audio_graph_is_unchanged(qapp, tmp_path) -> None:
    from src.features.edit.core.timeline_v2 import ClipInstance, EditTrack, TimelineSequence
    from src.features.edit.ui.edit_page import EditPage

    class DummyAudio:
        def __init__(self) -> None:
            self.track_ids: set[str] = set()
            self.added: list[tuple[str, int]] = []
            self.removed: list[str] = []
            self.prepared: list[int] = []
            self.quiesced = 0
            self.events: list[str] = []

        def get_all_track_ids(self):
            return list(self.track_ids)

        def remove_track(self, track_id: str) -> None:
            self.removed.append(track_id)
            self.events.append(f"remove:{track_id}")
            self.track_ids.discard(track_id)

        def add_track(self, track_id, asset_path, initial_state, **kwargs):
            self.added.append((track_id, int(kwargs["timeline_start"])))
            self.events.append(f"add:{track_id}")
            self.track_ids.add(track_id)
            return True

        def set_timeline_fps(self, fps: float) -> None:
            self.fps = fps

        def set_timeline_duration(self, duration: int) -> None:
            self.duration = duration

        def get_state(self):
            class State:
                name = "STOPPED"
            return State()

        def pause(self) -> None:
            pass

        def quiesce_for_timeline_rebuild(self) -> None:
            self.quiesced += 1
            self.events.append("quiesce")

        def prepare_for_playback(self, frame: int) -> bool:
            self.prepared.append(frame)
            return True

    media_path = tmp_path / "clip.mov"
    media_path.write_bytes(b"")
    project = SimpleNamespace(
        assets={"asset-av": SimpleNamespace(path=str(media_path), fps=24.0)}
    )

    def make_timeline(video_start: int) -> TimelineSequence:
        video = ClipInstance(
            clip_id="v1",
            asset_id="asset-av",
            track_id="v1",
            timeline_start=video_start,
            timeline_duration=100,
            source_start=0,
            source_duration=100,
            linked_clip_id="a1",
        )
        audio = ClipInstance(
            clip_id="a1",
            asset_id="asset-av",
            track_id="a1",
            timeline_start=0,
            timeline_duration=100,
            source_start=0,
            source_duration=100,
            linked_clip_id="v1",
        )
        return TimelineSequence(
            timeline_id="tl",
            name="TL",
            fps=24.0,
            width=1920,
            height=1080,
            sample_rate=48000,
            video_tracks=(EditTrack("v1", "V1", "video", 0, clips=(video,)),),
            audio_tracks=(EditTrack("a1", "A1", "audio", 0, clips=(audio,)),),
        )

    page = EditPage()
    audio = DummyAudio()
    page._audio_manager = audio
    page._current_frame = 42
    page._timeline_panel = SimpleNamespace(
        get_timeline_view=lambda: SimpleNamespace(_project=project)
    )

    page.set_timeline(make_timeline(0))
    page.set_timeline(make_timeline(20))

    assert audio.added == [("a1", 0)]
    assert audio.removed == []
    assert audio.quiesced == 0
    assert audio.prepared == [42]


def test_audio_sync_rebuilds_and_prerolls_when_audio_clip_moves(qapp, tmp_path) -> None:
    from src.features.edit.core.timeline_v2 import ClipInstance, EditTrack, TimelineSequence
    from src.features.edit.ui.edit_page import EditPage

    class DummyAudio:
        def __init__(self) -> None:
            self.track_ids: set[str] = set()
            self.added: list[tuple[str, int]] = []
            self.removed: list[str] = []
            self.prepared: list[int] = []
            self.quiesced = 0
            self.events: list[str] = []

        def get_all_track_ids(self):
            return list(self.track_ids)

        def remove_track(self, track_id: str) -> None:
            self.removed.append(track_id)
            self.events.append(f"remove:{track_id}")
            self.track_ids.discard(track_id)

        def add_track(self, track_id, asset_path, initial_state, **kwargs):
            self.added.append((track_id, int(kwargs["timeline_start"])))
            self.events.append(f"add:{track_id}")
            self.track_ids.add(track_id)
            return True

        def set_timeline_fps(self, fps: float) -> None:
            self.fps = fps

        def set_timeline_duration(self, duration: int) -> None:
            self.duration = duration

        def get_state(self):
            class State:
                name = "STOPPED"
            return State()

        def pause(self) -> None:
            pass

        def quiesce_for_timeline_rebuild(self) -> None:
            self.quiesced += 1
            self.events.append("quiesce")

        def prepare_for_playback(self, frame: int) -> bool:
            self.prepared.append(frame)
            return True

    media_path = tmp_path / "clip.mov"
    media_path.write_bytes(b"")
    project = SimpleNamespace(
        assets={"asset-av": SimpleNamespace(path=str(media_path), fps=24.0)}
    )

    def make_timeline(audio_start: int) -> TimelineSequence:
        audio = ClipInstance(
            clip_id="a1",
            asset_id="asset-av",
            track_id="a1",
            timeline_start=audio_start,
            timeline_duration=100,
            source_start=0,
            source_duration=100,
        )
        return TimelineSequence(
            timeline_id="tl",
            name="TL",
            fps=24.0,
            width=1920,
            height=1080,
            sample_rate=48000,
            audio_tracks=(EditTrack("a1", "A1", "audio", 0, clips=(audio,)),),
        )

    page = EditPage()
    audio = DummyAudio()
    page._audio_manager = audio
    page._current_frame = 18
    page._timeline_panel = SimpleNamespace(
        get_timeline_view=lambda: SimpleNamespace(_project=project)
    )

    page.set_timeline(make_timeline(0))
    page.set_timeline(make_timeline(12))

    assert audio.added == [("a1", 0), ("a1", 12)]
    assert audio.removed == ["a1"]
    assert audio.quiesced == 1
    assert audio.events == ["add:a1", "quiesce", "remove:a1", "add:a1"]
    assert audio.prepared == [18, 18]


def test_set_timeline_with_only_empty_track_added_does_not_request_preview(qapp) -> None:
    from src.features.edit.core.timeline_v2 import ClipInstance, EditTrack, TimelineSequence
    from src.features.edit.ui.edit_page import EditPage

    clip = ClipInstance(
        clip_id="v1",
        asset_id="asset-v",
        track_id="v1",
        timeline_start=0,
        timeline_duration=100,
        source_start=0,
        source_duration=100,
    )
    base = TimelineSequence(
        timeline_id="tl",
        name="TL",
        fps=24.0,
        width=1920,
        height=1080,
        video_tracks=(EditTrack("v1", "V1", "video", 0, clips=(clip,)),),
    )
    with_empty_track = TimelineSequence(
        timeline_id="tl",
        name="TL",
        fps=24.0,
        width=1920,
        height=1080,
        video_tracks=(
            base.video_tracks[0],
            EditTrack("v2", "V2", "video", 1, clips=()),
        ),
    )
    page = EditPage()
    page._current_frame = 37
    requested_frames: list[int] = []
    page.timeline_frame_requested.connect(requested_frames.append)

    page.set_timeline(base)
    page.set_timeline(with_empty_track)

    assert requested_frames == [37]


def test_legacy_emit_frame_request_uses_timeline_frame_path(qapp) -> None:
    from src.features.edit.ui.edit_page import EditPage

    page = EditPage()
    page._current_frame = 23
    requested_frames: list[int] = []
    page.timeline_frame_requested.connect(requested_frames.append)

    page._emit_frame_request()

    assert requested_frames == [23]
    assert not hasattr(page.signal_hub, "frame_requested")


def test_legacy_frame_ready_does_not_update_viewer_directly(qapp) -> None:
    from src.features.edit.ui.edit_page import EditPage

    class DummyViewer:
        def __init__(self) -> None:
            self.textures: list[tuple[int, int, int]] = []

        def set_texture(self, tex_id: int, width: int, height: int) -> None:
            self.textures.append((tex_id, width, height))

    page = EditPage()
    viewer = DummyViewer()
    page._program_viewer = viewer

    page.on_frame_ready(7, 1920, 1080)

    assert viewer.textures == []
    assert not hasattr(page.signal_hub, "frame_ready")


def test_audio_frame_sync_does_not_drive_timeline_preview_requests(qapp) -> None:
    from src.features.edit.ui.edit_page import EditPage

    page = EditPage()
    audio = DummyAudioManager()
    requested_frames: list[int] = []

    page._audio_manager = audio
    page._audio_active = True
    page.timeline_frame_requested.connect(requested_frames.append)

    page._setup_audio_video_sync()
    page._setup_audio_video_sync()
    audio.frame_changed.emit(12)

    assert requested_frames == []


def test_audio_reanchors_only_after_visible_drift(qapp) -> None:
    from src.features.edit.ui.edit_page import EditPage

    class DummyTimelineAudio:
        def __init__(self) -> None:
            self.seeked: list[int] = []

        def get_current_frame(self) -> int:
            return 20

        def seek(self, frame: int) -> None:
            self.seeked.append(frame)

    page = EditPage()
    audio = DummyTimelineAudio()
    page._audio_manager = audio
    page._audio_active = True

    page._on_playback_reanchored_for_audio(10)

    assert audio.seeked == [10]


def test_audio_reanchor_seeks_large_drift_during_live_playback(qapp) -> None:
    from src.features.edit.ui.edit_page import EditPage

    class DummyTimelineAudio:
        def __init__(self) -> None:
            self.seeked: list[int] = []

        def get_current_frame(self) -> int:
            return 20

        def seek(self, frame: int) -> None:
            self.seeked.append(frame)

    class DummyController:
        is_playing = True

    class DummyTimelinePanel:
        def get_playback_controller(self) -> DummyController:
            return DummyController()

    page = EditPage()
    audio = DummyTimelineAudio()
    page._audio_manager = audio
    page._audio_active = True
    page._timeline_panel = DummyTimelinePanel()

    page._on_playback_reanchored_for_audio(10)

    assert audio.seeked == [10]

def test_audio_reanchor_ignores_subframe_sized_drift(qapp) -> None:
    from src.features.edit.ui.edit_page import EditPage

    class DummyTimelineAudio:
        def __init__(self) -> None:
            self.seeked: list[int] = []

        def get_current_frame(self) -> int:
            return 12

        def seek(self, frame: int) -> None:
            self.seeked.append(frame)

    page = EditPage()
    audio = DummyTimelineAudio()
    page._audio_manager = audio
    page._audio_active = True

    page._on_playback_reanchored_for_audio(10)

    assert audio.seeked == []
