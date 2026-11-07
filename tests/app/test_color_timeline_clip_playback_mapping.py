from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np
import pytest

from src.app.controllers.page_controller import PageManagementMixin
from src.app.main_window import MainWindow, PageContextManager
from src.app.controllers.file_controller import FileOpsMixin
from src.features.color.ui.timeline_clip_strip import build_clip_entries
from src.features.edit.core.project import EditProject
from src.features.edit.core.timeline_v2 import ClipInstance, EditTrack
from src.shared.video.video_playback import (
    EnhancedVideoPlaybackEngine,
    _register_playback_engine,
    _unregister_playback_engine,
)


class _FakeVideoBar:
    def __init__(self) -> None:
        self.total_frames = None
        self.current_frame = None

    def set_total_frames(self, total: int) -> None:
        self.total_frames = int(total)

    def on_frame_changed(self, frame: int) -> None:
        self.current_frame = int(frame)


class _FakeVideoController:
    def __init__(self, current_frame: int = 0) -> None:
        self.current_frame = current_frame
        self.seek_calls: list[int] = []
        self.range_calls: list[tuple[int, int | None]] = []
        self.clear_calls = 0
        self.is_playing = False
        self.start_calls = 0

    def seek_to_frame(self, frame: int, **_kwargs) -> None:
        # 真实引擎签名含 churn_generation 等可选参数，替身按 kwargs 兼容
        self.seek_calls.append(int(frame))
        self.current_frame = int(frame)
        # Mirror the real engine, whose seek_to_frame calls stop_playback().
        self.is_playing = False

    def set_in_out_range(self, in_frame: int, out_frame: int | None) -> None:
        self.range_calls.append((int(in_frame), None if out_frame is None else int(out_frame)))

    def clear_in_out_range(self) -> None:
        self.clear_calls += 1

    def toggle_play(self) -> None:
        self.is_playing = not self.is_playing

    def start_playback(self) -> None:
        self.start_calls += 1
        self.is_playing = True


class _ForceDisplayVideoController:
    def __init__(self, current_frame: int = 0) -> None:
        self.current_frame = current_frame
        self.seek_calls: list[tuple[int, bool]] = []
        self.display_scene_size = None

    def is_video_loaded(self) -> bool:
        return True

    def set_display_scene_size(self, size) -> None:
        self.display_scene_size = size

    def seek_to_frame(
        self, frame: int, *, force_display: bool = False, **_kwargs
    ) -> None:
        self.seek_calls.append((int(frame), bool(force_display)))
        self.current_frame = int(frame)


class _FakeSignal:
    def __init__(self) -> None:
        self.values = []

    def emit(self, *args) -> None:
        self.values.append(args)


class _Controller(FileOpsMixin):
    def __init__(self, clip: ClipInstance) -> None:
        self.current_page = "Color"
        self.canvas = type("Canvas", (), {"video_control_bar": _FakeVideoBar()})()
        self.video_controller = _FakeVideoController(current_frame=clip.source_start)
        self._color_timeline_clip_playback_context = None


class _ColorPageController(PageManagementMixin):
    pass


def _clip() -> ClipInstance:
    return ClipInstance(
        clip_id="clip-color",
        asset_id="asset-color",
        track_id="v1",
        timeline_start=100,
        timeline_duration=40,
        source_start=50,
        source_duration=40,
    )


def test_color_timeline_clip_bar_uses_clip_duration_not_source_total() -> None:
    clip = _clip()
    controller = _Controller(clip)

    controller._set_color_timeline_clip_playback_context("tl-1", clip)
    controller._on_color_video_total_frames_changed(200)

    assert controller.canvas.video_control_bar.total_frames == 40
    assert controller.video_controller.range_calls[-1] == (50, 89)


def test_color_timeline_clip_bar_falls_back_to_source_duration() -> None:
    clip = SimpleNamespace(
        source_start=202,
        source_duration=46,
        timeline_duration=None,
    )
    controller = _Controller(_clip())

    controller._set_color_timeline_clip_playback_context("tl-1", clip)
    controller._on_color_video_total_frames_changed(300)

    assert controller.canvas.video_control_bar.total_frames == 46
    assert controller.video_controller.range_calls[-1] == (202, 247)


def test_color_timeline_clip_seek_maps_local_frame_to_source_frame() -> None:
    clip = _clip()
    controller = _Controller(clip)
    controller._set_color_timeline_clip_playback_context("tl-1", clip)

    controller.on_video_seek(10)

    assert controller.video_controller.seek_calls[-1] == 60


def test_color_timeline_clip_seek_keeps_shared_timeline_playhead_aligned() -> None:
    clip = _clip()
    controller = _Controller(clip)
    controller._set_color_timeline_clip_playback_context("tl-1", clip)
    controller.timeline_panel = MagicMock()
    controller.timeline_panel.get_playhead_frame.return_value = clip.timeline_start

    controller.on_video_seek(10)

    controller.timeline_panel.set_playhead_frame.assert_called_once_with(110)


def test_color_clip_entry_moves_shared_timeline_playhead_to_selected_clip() -> None:
    controller = _ColorPageController()
    controller.timeline_panel = MagicMock()
    controller.timeline_panel.get_playhead_frame.return_value = 0

    PageManagementMixin._sync_timeline_playhead_for_color_entry(controller, 120)

    controller.timeline_panel.set_playhead_frame.assert_called_once_with(120)


# NOTE: Color video playback intentionally stays on the graded video_controller
# path (ImageEngine GLSL pipeline) rather than the shared CPU
# TimelinePreviewRenderer.  The latter only applies the primary-grade subset
# and had no pre-buffering, so it made adjustments invisible and stalled at
# clip boundaries.  The cross-clip range-bar-follow / scrub-follow tests that
# relied on _color_timeline_preview_renderer being active have been removed
# because that path is no longer activated for Color; the video_controller
# path keeps playback within the selected clip and grades every frame.


def test_color_timeline_clip_frame_signal_maps_source_frame_to_local_frame() -> None:
    clip = _clip()
    controller = _Controller(clip)
    controller._set_color_timeline_clip_playback_context("tl-1", clip)

    controller._on_color_video_source_frame_changed(73)

    assert controller.canvas.video_control_bar.current_frame == 23


def _make_cross_clip_controller(clip, next_clip, *, same_source: bool):
    """Build a controller whose timeline has clip then next_clip back-to-back."""

    class _Track:
        def __init__(self, clips) -> None:
            self._clips = tuple(clips)

        def get_clip_at(self, frame: int):
            for c in self._clips:
                if c.timeline_start <= frame < c.timeline_start + c.timeline_duration:
                    return c
            return None

    class _Strip:
        def __init__(self) -> None:
            self.active_clip_id = None

        def set_active_clip(self, clip_id) -> None:
            self.active_clip_id = clip_id

    path = "/tmp/source.mov"
    next_path = "/tmp/source.mov" if same_source else "/tmp/other.mov"
    project = SimpleNamespace(
        assets={
            clip.asset_id: SimpleNamespace(path=path),
            next_clip.asset_id: SimpleNamespace(path=next_path),
        }
    )
    strip = _Strip()
    controller = _Controller(clip)
    controller._set_color_timeline_clip_playback_context("tl-1", clip)
    controller._current_edit_project = project
    controller.color_timeline_clip_strip = strip
    controller._current_timeline_sequences = {
        "tl-1": SimpleNamespace(video_tracks=(_Track((clip, next_clip)),)),
    }
    return controller, strip


def test_color_same_source_clip_advances_on_playback_wrap() -> None:
    """Reaching the clip's out-frame crosses into a same-source neighbour.

    Contiguous same-source neighbours advance via pre-advance: on the clip's
    LAST frame (out) the context rebinds with no seek at all, so the decode
    stream flows into the next clip without a stop/seek/restart hitch.  A
    stale wrap signal from the old range must then be ignored.
    """
    clip = _clip()  # source 50..89, timeline 100..139
    next_clip = ClipInstance(
        clip_id="clip-next",
        asset_id="asset-next",
        track_id="v1",
        timeline_start=140,
        timeline_duration=40,
        source_start=90,
        source_duration=40,
    )
    controller, strip = _make_cross_clip_controller(clip, next_clip, same_source=True)
    controller.video_controller.is_playing = True  # wrap fires during playback

    # Play through the clip; on the out frame (89) the pre-advance rebinds.
    controller._on_color_video_source_frame_changed(89)   # out frame

    context = controller._active_color_timeline_clip_playback_context()
    assert context["clip"].clip_id == "clip-next"
    # Pre-advance is seek-less: no stop/seek/restart, playback keeps running.
    assert controller.video_controller.seek_calls == []
    assert controller.video_controller.start_calls == 0
    assert controller.video_controller.range_calls[-1] == (90, 129)
    assert strip.active_clip_id == "clip-next"
    assert controller.video_controller.is_playing is True

    # A stale wrap emission from the OLD range (50 == old in_frame) arrives
    # after the rebind and must not cascade into skipping another clip.
    controller._on_color_video_source_frame_changed(50)
    context = controller._active_color_timeline_clip_playback_context()
    assert context["clip"].clip_id == "clip-next"
    assert controller.video_controller.seek_calls == []


def test_color_cross_clip_advance_switches_color_stack() -> None:
    """Continuous playback into a same-source clip must swap the grade.

    A manual clip-card click runs the full enter-Color transaction and loads
    the target clip's ColorStack.  The same-source pre-advance path used to
    rebind only the playback context, so the engine kept applying the
    previously selected clip's grade and every following clip of the same
    source looked identical during continuous playback.  The advance must
    replay the stack transaction: flush the old stack, rebind the save
    callback and hydrate the new stack.
    """
    clip = _clip()  # source 50..89
    next_clip = ClipInstance(
        clip_id="clip-next",
        asset_id="asset-next",
        track_id="v1",
        timeline_start=140,
        timeline_duration=40,
        source_start=90,
        source_duration=40,
    )

    class _GradingController(_Controller):
        def __init__(self, first_clip: ClipInstance) -> None:
            super().__init__(first_clip)
            self.stack_events: list[str] = []
            self._grading_save_stack_id = "stack-old"

        def _flush_active_grading_stack_before_switch(self, target_stack_id):
            self.stack_events.append(f"flush->{target_stack_id}")

        def _setup_grading_save_callback(self, stack_id: str) -> None:
            self._grading_save_stack_id = stack_id
            self.stack_events.append(f"save_callback->{stack_id}")

        def _load_color_stack_to_grading_panel(self, stack_id: str) -> None:
            self.stack_events.append(f"hydrate->{stack_id}")

        def _ensure_clip_has_color_stack(self, clip_id: str, asset_id: str):
            return f"stack-{clip_id}"

    controller, strip = _make_cross_clip_controller(clip, next_clip, same_source=True)
    graded = _GradingController(clip)
    graded._current_edit_project = controller._current_edit_project
    graded.color_timeline_clip_strip = controller.color_timeline_clip_strip
    graded._current_timeline_sequences = controller._current_timeline_sequences
    graded._set_color_timeline_clip_playback_context("tl-1", clip)
    graded.video_controller.is_playing = True

    graded._on_color_video_source_frame_changed(89)  # out frame -> pre-advance

    context = graded._active_color_timeline_clip_playback_context()
    assert context["clip"].clip_id == "clip-next"
    assert graded.stack_events == [
        "flush->stack-clip-next",
        "save_callback->stack-clip-next",
        "hydrate->stack-clip-next",
    ]
    assert graded._grading_save_stack_id == "stack-clip-next"


def test_color_same_source_noncontiguous_clip_advances_on_wrap_with_seek() -> None:
    """Non-contiguous same-source neighbours still use the wrap + seek path.

    Pre-advance only applies when the next clip's source range starts exactly
    after the current out frame.  Otherwise the engine wraps back to in_frame
    and the advance must stop, seek to the next clip's source start and
    resume playback.
    """
    clip = _clip()  # source 50..89
    next_clip = ClipInstance(
        clip_id="clip-next",
        asset_id="asset-next",
        track_id="v1",
        timeline_start=140,
        timeline_duration=40,
        source_start=10,  # NOT contiguous with 89
        source_duration=40,
    )
    controller, strip = _make_cross_clip_controller(clip, next_clip, same_source=True)
    controller.video_controller.is_playing = True

    controller._on_color_video_source_frame_changed(89)   # out frame: no pre-advance
    assert controller.video_controller.seek_calls == []
    assert strip.active_clip_id is None

    controller._on_color_video_source_frame_changed(50)   # wrap back to in_frame

    context = controller._active_color_timeline_clip_playback_context()
    assert context["clip"].clip_id == "clip-next"
    assert controller.video_controller.seek_calls == [10]
    assert controller.video_controller.range_calls[-1] == (10, 49)
    assert strip.active_clip_id == "clip-next"
    assert controller.video_controller.start_calls == 1
    assert controller.video_controller.is_playing is True


def test_color_different_source_clip_does_not_advance_on_wrap() -> None:
    """A different-source neighbour keeps the current clip looping."""
    clip = _clip()  # source 50..89
    next_clip = ClipInstance(
        clip_id="clip-next",
        asset_id="asset-next",
        track_id="v1",
        timeline_start=140,
        timeline_duration=40,
        source_start=0,
        source_duration=40,
    )
    controller, strip = _make_cross_clip_controller(clip, next_clip, same_source=False)

    controller._on_color_video_source_frame_changed(89)
    controller._on_color_video_source_frame_changed(50)   # wrap

    # Different source -> no reload available; stay on the current clip.
    context = controller._active_color_timeline_clip_playback_context()
    assert context["clip"].clip_id == "clip-color"
    assert controller.video_controller.seek_calls == []
    assert strip.active_clip_id is None


def test_color_wrap_without_next_clip_keeps_looping() -> None:
    """No neighbour at the timeline boundary -> keep looping the current clip."""

    class _EmptyTrack:
        def get_clip_at(self, frame: int):
            return None

    clip = _clip()
    controller = _Controller(clip)
    controller._set_color_timeline_clip_playback_context("tl-1", clip)
    controller._current_edit_project = SimpleNamespace(assets={clip.asset_id: SimpleNamespace(path="/tmp/x.mov")})
    controller.color_timeline_clip_strip = SimpleNamespace(set_active_clip=lambda _id: None)
    controller._current_timeline_sequences = {
        "tl-1": SimpleNamespace(video_tracks=(_EmptyTrack(),)),
    }

    controller._on_color_video_source_frame_changed(89)
    controller._on_color_video_source_frame_changed(50)   # wrap

    context = controller._active_color_timeline_clip_playback_context()
    assert context["clip"].clip_id == "clip-color"
    assert controller.video_controller.seek_calls == []


def test_color_timeline_clip_navigation_stays_inside_clip_range() -> None:
    clip = _clip()
    controller = _Controller(clip)
    controller._set_color_timeline_clip_playback_context("tl-1", clip)

    controller.on_video_last_frame()
    assert controller.video_controller.seek_calls[-1] == 89

    controller.on_video_next_frame()
    assert controller.video_controller.seek_calls[-1] == 89

    controller.on_video_first_frame()
    assert controller.video_controller.seek_calls[-1] == 50


def test_color_timeline_clip_playback_context_uses_effective_source_start() -> None:
    clip = ClipInstance(
        clip_id="clip-effective",
        asset_id="asset-effective",
        track_id="v1",
        timeline_start=0,
        timeline_duration=40,
        source_start=0,
        source_duration=40,
    )
    controller = _Controller(clip)

    controller._set_color_timeline_clip_playback_context(
        "tl-effective",
        clip,
        effective_source_start=100,
    )
    controller.on_video_seek(10)
    controller._on_color_video_source_frame_changed(115)

    assert controller.video_controller.range_calls[-1] == (100, 139)
    assert controller.video_controller.seek_calls[-1] == 110
    assert controller.canvas.video_control_bar.current_frame == 15


@pytest.mark.parametrize("reverse", [False, True])
def test_effective_source_override_keeps_the_retime_mapping(reverse: bool) -> None:
    from src.features.edit.core.clip_params import SpeedParams
    from src.features.color.core.keyframe_frame_map import KeyframeFrameMap
    clip = ClipInstance(clip_id="retime", asset_id="a", track_id="v", timeline_start=100,
                        timeline_duration=20, source_start=50, source_duration=40,
                        speed=SpeedParams(speed=2.0, reverse=reverse))
    controller = _Controller(clip)
    controller._set_color_timeline_clip_playback_context("tl", clip, effective_source_start=62)
    mapping = KeyframeFrameMap(clip, 20, 62)
    for local in (0, 5, 12, 19):
        source = mapping.source_frame(local)
        assert controller._color_local_frame_to_source_frame(local) == source
        assert controller._color_source_frame_to_local_frame(source) == local


def test_color_clip_play_resyncs_range_before_first_play() -> None:
    clip = _clip()
    controller = _Controller(clip)
    controller._set_color_timeline_clip_playback_context("tl-1", clip)
    controller.video_controller.current_frame = 0
    controller.video_controller.range_calls.clear()
    controller.video_controller.seek_calls.clear()

    controller.on_video_play_toggled()

    assert controller.video_controller.range_calls[-1] == (50, 89)
    assert controller.video_controller.seek_calls[-1] == 50
    assert controller.video_controller.is_playing is True


def test_enhanced_video_range_uses_coordinator_total_without_legacy_decoder() -> None:
    engine = EnhancedVideoPlaybackEngine.__new__(EnhancedVideoPlaybackEngine)
    engine._coordinator = type("Coordinator", (), {"total_frames": 200})()
    engine._source_manager = None
    engine.decoder = None
    engine.in_frame = 0
    engine.out_frame = None

    engine.set_in_out_range(50, 89)

    assert engine.in_frame == 50
    assert engine.out_frame == 89
    assert engine._effective_out_frame() == 89


def test_enhanced_video_range_is_preserved_before_total_frames_known() -> None:
    engine = EnhancedVideoPlaybackEngine.__new__(EnhancedVideoPlaybackEngine)
    engine._coordinator = type("Coordinator", (), {"total_frames": 0})()
    engine._source_manager = None
    engine.decoder = None
    engine.in_frame = 0
    engine.out_frame = None

    engine.set_in_out_range(50, 89)

    assert engine.in_frame == 50
    assert engine.out_frame == 89


def test_enhanced_seek_is_nonblocking_when_any_engine_is_playing() -> None:
    """A seek issued while an engine is playing must stay non-blocking.

    Previously seek_to_frame sampled _is_any_engine_playing() AFTER its own
    stop_playback(), so the check always saw "no engine playing" and forced a
    blocking master decode on the UI thread (the cross-clip boundary hitch).
    The playing state is now snapshotted before stopping.  See the Color
    clip-boundary fix in src/shared/video/video_playback.py.
    """
    class _Coordinator:
        total_frames = 200

        def __init__(self) -> None:
            self.seek_calls = []

        def seek_to_frame(
            self, frame: int, *, blocking: bool = True, **_kwargs
        ) -> bool:
            # 真实协调器签名含 churn_generation（高频拖拽跳过代次递增）
            self.seek_calls.append((frame, blocking))
            return True

        def get_current_frame_data(self, is_master: bool = False):
            # The non-blocking path may probe for an ALREADY-READY proxy frame;
            # only a blocking MASTER fetch would violate the contract.
            if is_master:
                raise AssertionError("blocking master frame fetch must be skipped")
            return None

    other_engine_id = 987654
    _register_playback_engine(other_engine_id, True)
    try:
        engine = EnhancedVideoPlaybackEngine.__new__(EnhancedVideoPlaybackEngine)
        engine._use_dual_path = True
        engine._coordinator = _Coordinator()
        engine._source_manager = None
        engine.decoder = None
        engine.is_playing = False
        engine.stop_playback = lambda: None
        engine.current_frame = 0
        engine.current_path = "/tmp/source.mov"
        engine.frame_changed = _FakeSignal()
        engine.frame_ready = _FakeSignal()

        engine.seek_to_frame(58)

        assert engine._coordinator.seek_calls == [(58, False)]
        assert engine.current_frame == 58
        assert engine.frame_changed.values == [(58,)]
        assert engine.frame_ready.values == []
    finally:
        _unregister_playback_engine(other_engine_id)


def test_enhanced_force_display_seek_blocks_even_when_other_engine_is_playing() -> None:
    class _Coordinator:
        total_frames = 200

        def __init__(self) -> None:
            self.seek_calls = []
            self.get_calls = []

        def seek_to_frame(
            self, frame: int, *, blocking: bool = True, **_kwargs
        ) -> bool:
            # 真实协调器签名含 churn_generation（高频拖拽跳过代次递增）
            self.seek_calls.append((frame, blocking))
            return True

        def get_current_frame_data(self, is_master: bool = False):
            self.get_calls.append(is_master)
            return SimpleNamespace(frame_index=58)

    other_engine_id = 987655
    _register_playback_engine(other_engine_id, True)
    try:
        engine = EnhancedVideoPlaybackEngine.__new__(EnhancedVideoPlaybackEngine)
        engine._use_dual_path = True
        engine._coordinator = _Coordinator()
        engine._source_manager = None
        engine.decoder = None
        engine.is_playing = False
        engine.stop_playback = lambda: None
        engine.current_frame = 0
        engine.current_path = "/tmp/source.mov"
        engine.frame_changed = _FakeSignal()
        engine.frame_ready = _FakeSignal()
        submitted = []

        def _submit(frame, frame_num: int, **kwargs) -> None:
            submitted.append((frame, frame_num, kwargs))
            engine.current_frame = frame_num
            engine.frame_changed.emit(frame_num)
            engine.frame_ready.emit()

        engine._submit_decoded_frame_playback = _submit

        engine.seek_to_frame(58, force_display=True)

        assert engine._coordinator.seek_calls == [(58, True)]
        assert engine._coordinator.get_calls == [True]
        assert submitted[0][1] == 58
        assert submitted[0][2]["reason"] == "seek"
        assert submitted[0][2]["drop_if_stale"] is True
        assert engine.current_frame == 58
    finally:
        _unregister_playback_engine(other_engine_id)


def test_enhanced_force_display_seek_retries_exact_master_when_missing() -> None:
    class _Coordinator:
        total_frames = 200

        def __init__(self) -> None:
            self.seek_calls = []
            self.proxy_calls = []

        def seek_to_frame(
            self, frame: int, *, blocking: bool = True, **_kwargs
        ) -> bool:
            # 真实协调器签名含 churn_generation（高频拖拽跳过代次递增）
            self.seek_calls.append((frame, blocking))
            return True

        def get_current_frame_data(self, is_master: bool = False):
            return None

        def get_frame_at(self, frame: int, is_master: bool = False):
            self.proxy_calls.append((frame, is_master))
            return SimpleNamespace(frame_index=frame)

    engine = EnhancedVideoPlaybackEngine.__new__(EnhancedVideoPlaybackEngine)
    engine._use_dual_path = True
    engine._coordinator = _Coordinator()
    engine._source_manager = None
    engine.decoder = None
    engine.is_playing = False
    engine.stop_playback = lambda: None
    engine.current_frame = 0
    engine.current_path = "/tmp/source.mov"
    engine.frame_changed = _FakeSignal()
    engine.frame_ready = _FakeSignal()
    submitted = []

    def _submit(frame, frame_num: int, **kwargs) -> None:
        submitted.append((frame, frame_num, kwargs))
        engine.current_frame = frame_num
        engine.frame_changed.emit(frame_num)
        engine.frame_ready.emit()

    engine._submit_decoded_frame_playback = _submit

    engine.seek_to_frame(88, force_display=True)

    assert engine._coordinator.seek_calls == [(88, True)]
    assert engine._coordinator.proxy_calls == [(88, True)]
    assert submitted[0][1] == 88
    assert submitted[0][2]["reason"] == "seek"
    assert submitted[0][2]["drop_if_stale"] is True
    assert engine.frame_ready.values == [()]


def test_enhanced_tick_range_loop_uses_nonblocking_seek() -> None:
    class _Coordinator:
        total_frames = 200
        current_frame = 90

        def __init__(self) -> None:
            self.seek_calls = []
            self.session = None

        def tick(self):
            return SimpleNamespace(frame_index=90)

        def seek_to_frame(
            self, frame: int, *, blocking: bool = True, **_kwargs
        ) -> bool:
            # 真实协调器签名含 churn_generation（高频拖拽跳过代次递增）
            self.seek_calls.append((frame, blocking))
            return True

    class _Perf:
        def track_tick_time(self, _value) -> None:
            pass

    engine = EnhancedVideoPlaybackEngine.__new__(EnhancedVideoPlaybackEngine)
    engine._use_dual_path = True
    engine._coordinator = _Coordinator()
    engine._source_manager = None
    engine.decoder = None
    engine.is_playing = True
    engine.engine = SimpleNamespace(renderer=None)
    engine._perf_monitor = _Perf()
    engine.in_frame = 50
    engine.out_frame = 89
    engine.loop_playback = False
    engine.current_frame = 89
    engine.frame_changed = _FakeSignal()

    engine._on_play_tick()

    assert engine._coordinator.seek_calls == [(50, False)]
    assert engine.current_frame == 50
    assert engine.frame_changed.values == [(50,)]


def test_enhanced_tick_drops_stale_pre_seek_frame_from_previous_clip() -> None:
    """A frame decoded before the clip's in-point must not flash through.

    After a clip switch + play, the coordinator can still hand back the
    previous clip's tail frame (decoded while the seek to the new in-point
    was in flight).  Submitting it stamped the current generation id, so the
    renderer's stale guard could not reject it and the departing clip flashed
    for one frame.  The tick must drop any frame below the active in-point.
    """

    class _Coordinator:
        total_frames = 200
        current_frame = 50

        def tick(self):
            # Stale tail frame of the departing clip, below the new in_frame.
            return SimpleNamespace(frame_index=30)

    renderer = MagicMock()
    renderer.get_renderer_stats.return_value = {}
    engine = EnhancedVideoPlaybackEngine.__new__(EnhancedVideoPlaybackEngine)
    engine._use_dual_path = True
    engine._coordinator = _Coordinator()
    engine._source_manager = None
    engine.decoder = None
    engine.is_playing = True
    engine.engine = SimpleNamespace(renderer=renderer)
    engine._perf_monitor = MagicMock()
    engine._video_preview_max_queue = 8
    engine.in_frame = 50
    engine.out_frame = 89
    engine.loop_playback = False
    engine.current_frame = 50
    engine._last_displayed_frame_index = -1
    engine._last_submitted_video_frame_index = -1
    engine._render_params_generation = 1
    engine._last_submitted_render_params_generation = 1
    engine._last_displayed_decoded_frame = None

    engine._on_play_tick()

    # The stale pre-in-point frame is dropped: nothing is submitted and the
    # displayed-frame marker is untouched.
    renderer.submit.assert_not_called()
    assert engine._last_displayed_frame_index == -1


def test_color_clip_switch_inside_color_page_uses_lightweight_context_apply() -> None:
    project = EditProject.create("Color Clip Switch")
    project = project.import_asset(
        path="/tmp/source.mov",
        media_type="video",
        metadata={"width": 1920, "height": 1080, "fps": 24.0, "total_frames": 200},
    )
    asset_id = next(iter(project.assets))
    clip = ClipInstance(
        clip_id="clip-light",
        asset_id=asset_id,
        track_id="v1",
        timeline_start=100,
        timeline_duration=40,
        source_start=50,
        source_duration=40,
        color_stack_id="stack-light",
    )
    timeline = SimpleNamespace(
        timeline_id="tl-light",
        get_clip_by_id=lambda clip_id: clip if clip_id == clip.clip_id else None,
    )
    controller = _ColorPageController()
    controller._current_edit_project = project
    controller._current_timeline_sequences = {timeline.timeline_id: timeline}
    controller._page_context_manager = PageContextManager()
    controller.current_page = "Color"
    controller.timeline_panel = None
    controller.switch_to_page_with_context = MagicMock()
    controller._handle_enter_color_with_context = MagicMock()

    PageManagementMixin.open_timeline_clip_in_color(
        controller,
        timeline.timeline_id,
        clip.clip_id,
        timeline_frame=123,
    )

    controller.switch_to_page_with_context.assert_not_called()
    controller._handle_enter_color_with_context.assert_called_once()
    context = controller._handle_enter_color_with_context.call_args.args[0]
    assert context.clip_id == "clip-light"
    assert context.source_frame == 73
    assert controller._page_context_manager.get_current_context() is context


def test_color_clip_strip_click_uses_clicked_clip_start_not_edit_playhead() -> None:
    project = EditProject.create("Color Clip Strip Click")
    project = project.import_asset(
        path="/tmp/source.mov",
        media_type="video",
        metadata={"width": 1920, "height": 1080, "fps": 24.0, "total_frames": 300},
    )
    asset_id = next(iter(project.assets))
    clip = ClipInstance(
        clip_id="clip-second",
        asset_id=asset_id,
        track_id="v1",
        timeline_start=200,
        timeline_duration=40,
        source_start=120,
        source_duration=40,
        color_stack_id="stack-second",
    )
    timeline = SimpleNamespace(
        timeline_id="tl-strip",
        get_clip_by_id=lambda clip_id: clip if clip_id == clip.clip_id else None,
    )
    controller = _ColorPageController()
    controller._current_edit_project = project
    controller._current_timeline_sequences = {timeline.timeline_id: timeline}
    controller._page_context_manager = PageContextManager()
    controller.current_page = "Color"
    controller.timeline_panel = SimpleNamespace(get_playhead_frame=lambda: 10)
    controller.switch_to_page_with_context = MagicMock()
    controller._handle_enter_color_with_context = MagicMock()

    PageManagementMixin.open_timeline_clip_in_color(
        controller,
        timeline.timeline_id,
        clip.clip_id,
    )

    context = controller._handle_enter_color_with_context.call_args.args[0]
    assert context.timeline_frame == 200
    assert context.source_frame == 120


def test_color_clip_strip_click_source_frame_override_wins() -> None:
    project = EditProject.create("Color Clip Strip Source Override")
    project = project.import_asset(
        path="/tmp/source.mov",
        media_type="video",
        metadata={"width": 1920, "height": 1080, "fps": 24.0, "total_frames": 300},
    )
    asset_id = next(iter(project.assets))
    stale_clip = ClipInstance(
        clip_id="clip-card",
        asset_id=asset_id,
        track_id="v1",
        timeline_start=0,
        timeline_duration=40,
        source_start=0,
        source_duration=40,
        color_stack_id="stack-card",
    )
    timeline = SimpleNamespace(
        timeline_id="tl-card",
        get_clip_by_id=lambda clip_id: stale_clip if clip_id == stale_clip.clip_id else None,
    )
    controller = _ColorPageController()
    controller._current_edit_project = project
    controller._current_timeline_sequences = {timeline.timeline_id: timeline}
    controller._page_context_manager = PageContextManager()
    controller.current_page = "Color"
    controller.timeline_panel = None
    controller.switch_to_page_with_context = MagicMock()
    controller._handle_enter_color_with_context = MagicMock()

    PageManagementMixin.open_timeline_clip_in_color(
        controller,
        timeline.timeline_id,
        stale_clip.clip_id,
        timeline_frame=200,
        source_frame_override=120,
    )

    context = controller._handle_enter_color_with_context.call_args.args[0]
    assert context.timeline_frame == 0
    assert context.source_frame == 120


def test_color_clip_entries_use_subclip_metadata_source_start() -> None:
    project = EditProject.create("Color Subclip Entries")
    project = project.import_asset(
        path="/tmp/source.mov",
        media_type="video",
        metadata={
            "width": 1920,
            "height": 1080,
            "fps": 24.0,
            "total_frames": 40,
            "is_subclip": True,
            "source_start": 100,
            "source_duration": 40,
            "parent_asset_id": "parent-1",
        },
    )
    asset_id = next(iter(project.assets))
    clip = ClipInstance(
        clip_id="clip-sub-entry",
        asset_id=asset_id,
        track_id="v1",
        timeline_start=0,
        timeline_duration=40,
        source_start=0,
        source_duration=40,
    )
    track = EditTrack(
        track_id="v1",
        name="V1",
        track_type="video",
        index=0,
        clips=(clip,),
        transitions=(),
    )
    timeline = SimpleNamespace(timeline_id="tl-sub", video_tracks=(track,))

    entries = build_clip_entries(project, timeline, "tl-sub")

    assert entries[0].source_start == 100
    assert entries[0].source_duration == 40


def test_color_clip_open_normalizes_subclip_asset_source_frame() -> None:
    project = EditProject.create("Color Subclip Open")
    project = project.import_asset(
        path="/tmp/source.mov",
        media_type="video",
        metadata={
            "width": 1920,
            "height": 1080,
            "fps": 24.0,
            "total_frames": 40,
            "is_subclip": True,
            "source_start": 100,
            "source_duration": 40,
            "parent_asset_id": "parent-1",
        },
    )
    asset_id = next(iter(project.assets))
    clip = ClipInstance(
        clip_id="clip-sub-open",
        asset_id=asset_id,
        track_id="v1",
        timeline_start=0,
        timeline_duration=40,
        source_start=0,
        source_duration=40,
        color_stack_id="stack-sub",
    )
    timeline = SimpleNamespace(
        timeline_id="tl-sub-open",
        get_clip_by_id=lambda clip_id: clip if clip_id == clip.clip_id else None,
    )
    controller = _ColorPageController()
    controller._current_edit_project = project
    controller._current_timeline_sequences = {timeline.timeline_id: timeline}
    controller._page_context_manager = PageContextManager()
    controller.current_page = "Color"
    controller.timeline_panel = None
    controller.switch_to_page_with_context = MagicMock()
    controller._handle_enter_color_with_context = MagicMock()

    PageManagementMixin.open_timeline_clip_in_color(
        controller,
        timeline.timeline_id,
        clip.clip_id,
    )

    context = controller._handle_enter_color_with_context.call_args.args[0]
    assert context.source_frame == 100


def test_color_clip_open_does_not_double_add_effective_source_override() -> None:
    project = EditProject.create("Color Subclip Override")
    project = project.import_asset(
        path="/tmp/source.mov",
        media_type="video",
        metadata={
            "width": 1920,
            "height": 1080,
            "fps": 24.0,
            "total_frames": 40,
            "is_subclip": True,
            "source_start": 100,
            "source_duration": 40,
            "parent_asset_id": "parent-1",
        },
    )
    asset_id = next(iter(project.assets))
    clip = ClipInstance(
        clip_id="clip-sub-override",
        asset_id=asset_id,
        track_id="v1",
        timeline_start=0,
        timeline_duration=40,
        source_start=0,
        source_duration=40,
        color_stack_id="stack-sub",
    )
    timeline = SimpleNamespace(
        timeline_id="tl-sub-override",
        get_clip_by_id=lambda clip_id: clip if clip_id == clip.clip_id else None,
    )
    controller = _ColorPageController()
    controller._current_edit_project = project
    controller._current_timeline_sequences = {timeline.timeline_id: timeline}
    controller._page_context_manager = PageContextManager()
    controller.current_page = "Color"
    controller.timeline_panel = None
    controller.switch_to_page_with_context = MagicMock()
    controller._handle_enter_color_with_context = MagicMock()

    PageManagementMixin.open_timeline_clip_in_color(
        controller,
        timeline.timeline_id,
        clip.clip_id,
        source_frame_override=100,
    )

    context = controller._handle_enter_color_with_context.call_args.args[0]
    assert context.source_frame == 100


def test_enter_color_timeline_clip_force_refreshes_target_frame() -> None:
    project = EditProject.create("Color Clip Refresh")
    project = project.import_asset(
        path="/tmp/source.mov",
        media_type="video",
        metadata={"width": 1920, "height": 1080, "fps": 24.0, "total_frames": 200},
    )
    asset_id = next(iter(project.assets))
    clip = ClipInstance(
        clip_id="clip-refresh",
        asset_id=asset_id,
        track_id="v1",
        timeline_start=100,
        timeline_duration=40,
        source_start=50,
        source_duration=40,
        color_stack_id="stack-refresh",
    )
    timeline = SimpleNamespace(
        timeline_id="tl-refresh",
        width=1920,
        height=1080,
        get_clip_by_id=lambda clip_id: clip if clip_id == clip.clip_id else None,
    )
    canvas = SimpleNamespace(
        set_display_scene_size_override=MagicMock(),
        fit_to_window=MagicMock(),
    )
    video_controller = _ForceDisplayVideoController(current_frame=50)
    manager = MagicMock()
    controller = _ColorPageController()
    controller._current_edit_project = project
    controller._current_timeline_sequences = {timeline.timeline_id: timeline}
    controller.canvas = canvas
    controller.video_controller = video_controller
    controller.current_file_path = "/tmp/source.mov"
    controller.load_video = MagicMock()
    controller.edit_page = None
    controller._setup_grading_save_callback = MagicMock()
    controller._load_color_stack_to_grading_panel = MagicMock()
    controller._request_color_clip_thumbnail_static_preview = MagicMock(return_value=False)
    controller._color_session_manager = manager
    controller.color_timeline_clip_strip = SimpleNamespace(set_active_clip=MagicMock())
    controller._page_context_manager = PageContextManager()
    context = controller._page_context_manager.create_context_for_edit_to_color(
        clip_id=clip.clip_id,
        asset_id=clip.asset_id,
        timeline_id=timeline.timeline_id,
        timeline_frame=100,
        source_frame=50,
        clip_local_frame=0,
        color_stack_id=clip.color_stack_id,
    )

    PageManagementMixin._handle_enter_color_with_context(controller, context)

    assert video_controller.seek_calls == [(50, False)]
    controller.load_video.assert_not_called()
    manager.create_from_page_context.assert_called_once()


def test_color_clip_static_preview_waits_for_master_before_thumbnail_fallback(monkeypatch) -> None:
    project = EditProject.create("Color Clip Thumbnail Preview")
    project = project.import_asset(
        path="/tmp/source.mov",
        media_type="video",
        metadata={"width": 1920, "height": 1080, "fps": 24.0, "total_frames": 200},
    )
    asset_id = next(iter(project.assets))
    clip = ClipInstance(
        clip_id="clip-thumb",
        asset_id=asset_id,
        track_id="v1",
        timeline_start=100,
        timeline_duration=40,
        source_start=50,
        source_duration=40,
        color_stack_id="stack-thumb",
    )

    class _Signal:
        def __init__(self) -> None:
            self.slots = []

        def connect(self, slot) -> None:
            self.slots.append(slot)

        def disconnect(self, slot) -> None:
            if slot in self.slots:
                self.slots.remove(slot)

    class _Provider:
        def __init__(self) -> None:
            self.frame_thumbnail_ready = _Signal()
            self.cache_calls = []
            self.async_calls = []

        def get_cached_frame_pixmap(self, asset_id, frame_number, target_height, *, path=None):
            self.cache_calls.append((asset_id, frame_number, target_height, path))
            if target_height == 144:
                return "cached-pixmap"
            return None

        def request_frame_pixmap_async(
            self,
            asset_id,
            file_path,
            frame_number,
            total_frames,
            target_height=48,
        ) -> None:
            self.async_calls.append(
                (asset_id, file_path, frame_number, total_frames, target_height)
            )

    provider = _Provider()
    monkeypatch.setattr(
        "src.shared.media.thumbnail_provider.ThumbnailProvider.instance",
        lambda: provider,
    )

    manager = MagicMock()
    controller = _ColorPageController()
    controller._current_edit_project = project
    controller._color_timeline_clip_playback_context = {"clip": clip}
    controller._color_static_preview_token = 1
    controller.video_controller = SimpleNamespace(
        is_video_loaded=lambda: True,
        request_static_preview_frame=MagicMock(return_value=False),
    )
    controller._submit_color_clip_thumbnail_pixmap = MagicMock(return_value=True)
    controller.canvas = SimpleNamespace(video_control_bar=_FakeVideoBar())
    controller._color_session_manager = manager
    context = PageContextManager().create_context_for_edit_to_color(
        clip_id=clip.clip_id,
        asset_id=clip.asset_id,
        timeline_id="tl-thumb",
        timeline_frame=100,
        source_frame=50,
        clip_local_frame=0,
        color_stack_id=clip.color_stack_id,
    )

    PageManagementMixin._show_color_timeline_clip_static_preview(
        controller,
        context,
        token=1,
    )

    controller._submit_color_clip_thumbnail_pixmap.assert_not_called()
    assert provider.cache_calls == []
    assert provider.async_calls == []
    controller.video_controller.request_static_preview_frame.assert_called_once_with(50)


def test_color_clip_static_preview_uses_exact_master_seek_after_async_timeout() -> None:
    project = EditProject.create("Color Clip Exact Preview")
    project = project.import_asset(
        path="/tmp/source.mov",
        media_type="video",
        metadata={"width": 1920, "height": 1080, "fps": 24.0, "total_frames": 200},
    )
    asset_id = next(iter(project.assets))
    clip = ClipInstance(
        clip_id="clip-exact",
        asset_id=asset_id,
        track_id="v1",
        timeline_start=100,
        timeline_duration=40,
        source_start=50,
        source_duration=40,
        color_stack_id="stack-exact",
    )
    controller = _ColorPageController()
    controller._current_edit_project = project
    controller._color_static_preview_token = 1
    controller.video_controller = SimpleNamespace(
        is_video_loaded=lambda: True,
        request_static_preview_frame=MagicMock(return_value=False),
        seek_to_frame=MagicMock(),
    )
    context = PageContextManager().create_context_for_edit_to_color(
        clip_id=clip.clip_id,
        asset_id=asset_id,
        timeline_id="tl-exact",
        timeline_frame=100,
        source_frame=50,
        clip_local_frame=0,
        color_stack_id=clip.color_stack_id,
    )

    PageManagementMixin._show_color_timeline_clip_static_preview(
        controller,
        context,
        retry_count=8,
        token=1,
    )

    controller.video_controller.seek_to_frame.assert_called_once_with(50, force_display=True)
    assert not hasattr(controller, "_color_static_preview_rendered_key")


def test_color_clip_static_preview_skips_async_when_full_cached(monkeypatch) -> None:
    project = EditProject.create("Color Clip Thumbnail Full Cache")
    project = project.import_asset(
        path="/tmp/source.mov",
        media_type="video",
        metadata={"width": 1920, "height": 1080, "fps": 24.0, "total_frames": 200},
    )
    asset_id = next(iter(project.assets))
    clip = ClipInstance(
        clip_id="clip-full-cache",
        asset_id=asset_id,
        track_id="v1",
        timeline_start=100,
        timeline_duration=40,
        source_start=50,
        source_duration=40,
        color_stack_id="stack-full-cache",
    )

    class _Signal:
        def connect(self, _slot) -> None:
            raise AssertionError("720p cached frame should not subscribe async")

        def disconnect(self, _slot) -> None:
            pass

    class _Provider:
        def __init__(self) -> None:
            self.frame_thumbnail_ready = _Signal()
            self.cache_calls = []
            self.async_calls = []

        def get_cached_frame_pixmap(self, asset_id, frame_number, target_height, *, path=None):
            self.cache_calls.append((asset_id, frame_number, target_height, path))
            if target_height == 720:
                return "cached-720"
            return None

        def request_frame_pixmap_async(self, *args, **kwargs) -> None:
            self.async_calls.append((args, kwargs))

    provider = _Provider()
    monkeypatch.setattr(
        "src.shared.media.thumbnail_provider.ThumbnailProvider.instance",
        lambda: provider,
    )

    controller = _ColorPageController()
    controller._current_edit_project = project
    controller._color_timeline_clip_playback_context = {"clip": clip}
    controller._color_static_preview_token = 2
    controller.video_controller = SimpleNamespace(is_video_loaded=lambda: True)
    context = PageContextManager().create_context_for_edit_to_color(
        clip_id=clip.clip_id,
        asset_id=clip.asset_id,
        timeline_id="tl-full-cache",
        timeline_frame=100,
        source_frame=50,
        clip_local_frame=0,
        color_stack_id=clip.color_stack_id,
    )
    controller._submit_color_clip_thumbnail_pixmap = MagicMock(return_value=True)

    rendered = PageManagementMixin._request_color_clip_thumbnail_static_preview(
        controller,
        context,
        source_frame=50,
        token=2,
    )

    assert rendered is True
    controller._submit_color_clip_thumbnail_pixmap.assert_called_once_with(
        "cached-720",
        source_frame=50,
        token=2,
    )
    assert provider.cache_calls == [(asset_id, 50, 720, "/tmp/source.mov")]
    assert provider.async_calls == []


def test_immediate_color_clip_preview_uses_safe_request_when_thumbnail_misses() -> None:
    controller = _ColorPageController()
    controller._color_static_preview_token = 0
    controller._request_color_clip_thumbnail_static_preview = MagicMock(return_value=False)
    controller.video_controller = SimpleNamespace(
        render_static_preview_frame=MagicMock(return_value=True),
        request_static_preview_frame=MagicMock(return_value=True),
    )
    context = PageContextManager().create_context_for_edit_to_color(
        clip_id="clip-miss",
        asset_id="asset-miss",
        timeline_id="tl-miss",
        timeline_frame=100,
        source_frame=416,
        clip_local_frame=0,
        color_stack_id="stack-miss",
    )

    rendered = PageManagementMixin._show_color_timeline_clip_immediate_thumbnail_preview(
        controller,
        context,
        source_frame=416,
    )

    assert rendered is True
    controller._request_color_clip_thumbnail_static_preview.assert_called_once_with(
        context,
        source_frame=416,
        token=1,
    )
    controller.video_controller.render_static_preview_frame.assert_not_called()
    controller.video_controller.request_static_preview_frame.assert_called_once_with(416)


def test_color_clip_static_preview_avoids_hard_video_render_path() -> None:
    controller = _ColorPageController()
    controller._color_static_preview_token = 1
    controller.video_controller = SimpleNamespace(
        is_video_loaded=lambda: True,
        render_static_preview_frame=MagicMock(return_value=True),
        request_static_preview_frame=MagicMock(return_value=True),
    )
    controller._request_color_clip_thumbnail_static_preview = MagicMock(return_value=True)
    controller._sync_color_timeline_clip_video_controls = MagicMock()
    context = PageContextManager().create_context_for_edit_to_color(
        clip_id="clip-hard",
        asset_id="asset-hard",
        timeline_id="tl-hard",
        timeline_frame=0,
        source_frame=64,
        clip_local_frame=0,
        color_stack_id="stack-hard",
    )

    PageManagementMixin._show_color_timeline_clip_static_preview(
        controller,
        context,
        token=1,
    )

    controller.video_controller.render_static_preview_frame.assert_not_called()
    controller._request_color_clip_thumbnail_static_preview.assert_not_called()
    controller.video_controller.request_static_preview_frame.assert_called_once_with(64)


def test_enhanced_video_hard_static_preview_sets_image_engine_frame() -> None:
    frame = np.zeros((4, 6, 3), dtype=np.uint8)
    frame[:, :, 0] = 25
    engine = EnhancedVideoPlaybackEngine.__new__(EnhancedVideoPlaybackEngine)
    engine._use_dual_path = False
    engine._coordinator = None
    engine._source_manager = SimpleNamespace(get_proxy_frame=MagicMock(return_value=frame))
    engine.decoder = None
    engine.current_path = "/tmp/source.mov"
    engine.current_frame = 0
    engine._generation_id = 0
    engine.engine = SimpleNamespace(set_image=MagicMock())
    engine.frame_changed = _FakeSignal()
    engine.frame_ready = _FakeSignal()
    engine.stop_playback = MagicMock()
    engine._last_displayed_frame = None
    engine._last_displayed_decoded_frame = None
    engine._last_displayed_frame_index = None

    assert EnhancedVideoPlaybackEngine.render_static_preview_frame(engine, 64) is True

    engine.engine.set_image.assert_called_once()
    args, kwargs = engine.engine.set_image.call_args
    assert args[0] is frame
    assert kwargs["is_new_file"] is False
    assert kwargs["is_video"] is True
    assert kwargs["video_path"] == "/tmp/source.mov"
    assert engine.current_frame == 64
    assert engine._last_displayed_frame_index == 64
    assert engine._last_frame_ready_direct_render is True
    assert engine.frame_changed.values == [(64,)]
    assert engine.frame_ready.values == [()]


def test_v3_static_preview_request_does_not_sync_decode_missing_frame() -> None:
    engine = EnhancedVideoPlaybackEngine.__new__(EnhancedVideoPlaybackEngine)
    engine._use_dual_path = True
    engine._coordinator = SimpleNamespace(
        seek_to_frame=MagicMock(),
        request_master_frame=MagicMock(),
        take_ready_master_frame=MagicMock(return_value=None),
    )
    engine.current_path = "/tmp/source.mov"
    engine.current_frame = 0
    engine._generation_id = 4
    engine.stop_playback = MagicMock()
    engine.frame_changed = _FakeSignal()

    assert EnhancedVideoPlaybackEngine.request_static_preview_frame(engine, 125) is False

    engine._coordinator.seek_to_frame.assert_called_once_with(125, blocking=False)
    engine._coordinator.request_master_frame.assert_called_once_with(125)
    engine._coordinator.take_ready_master_frame.assert_called_once_with(125)
    assert engine._generation_id == 5
    assert engine.current_frame == 125
    assert engine.frame_changed.values == [(125,)]


def test_v3_static_preview_poll_does_not_cancel_inflight_master_request() -> None:
    """A same-frame poll must not invalidate its pending master decode."""
    engine = EnhancedVideoPlaybackEngine.__new__(EnhancedVideoPlaybackEngine)
    engine._use_dual_path = True
    engine._coordinator = SimpleNamespace(
        seek_to_frame=MagicMock(),
        request_master_frame=MagicMock(),
        take_ready_master_frame=MagicMock(return_value=None),
    )
    engine.current_path = "/tmp/source.mov"
    engine.current_frame = 0
    engine._generation_id = 4
    engine.stop_playback = MagicMock()
    engine.frame_changed = _FakeSignal()

    assert EnhancedVideoPlaybackEngine.request_static_preview_frame(engine, 125) is False
    assert EnhancedVideoPlaybackEngine.request_static_preview_frame(engine, 125) is False

    engine._coordinator.seek_to_frame.assert_called_once_with(125, blocking=False)
    engine._coordinator.request_master_frame.assert_called_once_with(125)
    assert engine._generation_id == 5
    assert engine._coordinator.take_ready_master_frame.call_count == 2


def test_direct_video_seek_ready_does_not_rerender_stale_canvas() -> None:
    viewport = SimpleNamespace(update=MagicMock())
    controller = SimpleNamespace(
        video_controller=SimpleNamespace(
            is_playing=False,
            _last_frame_ready_direct_render=True,
        ),
        canvas=SimpleNamespace(viewport=lambda: viewport),
        update_canvas=MagicMock(),
        _last_video_canvas_update=0.0,
    )

    FileOpsMixin.on_video_frame_ready(controller)

    assert controller.video_controller._last_frame_ready_direct_render is False
    viewport.update.assert_called_once()
    controller.update_canvas.assert_not_called()


def test_static_color_output_reads_current_frame_without_pbo_delay() -> None:
    monitor = SimpleNamespace()
    controller = SimpleNamespace(
        current_page="Color",
        video_controller=SimpleNamespace(is_playing=False),
        _ensure_program_monitor_output=MagicMock(return_value=monitor),
        _color_monitor_matches_current_video_frame=MagicMock(return_value=True),
        _discard_color_monitor_pending_readback=MagicMock(),
        _submit_color_monitor_direct_readback=MagicMock(return_value=True),
        _submit_color_monitor_pbo_readback=MagicMock(),
    )

    FileOpsMixin._on_color_output_monitor_ready(controller, 17, 1920, 1080)

    controller._discard_color_monitor_pending_readback.assert_called_once_with()
    controller._submit_color_monitor_direct_readback.assert_called_once_with(
        17,
        monitor,
        1920,
        1080,
        source="monitor_color_output_static",
        fit_mode="cover",
    )
    controller._submit_color_monitor_pbo_readback.assert_not_called()


def test_playback_rejects_late_static_monitor_repaint() -> None:
    monitor = SimpleNamespace()
    controller = SimpleNamespace(
        current_page="Color",
        video_controller=SimpleNamespace(
            is_playing=True,
            _last_submitted_video_reason="seek",
        ),
        _ensure_program_monitor_output=MagicMock(return_value=monitor),
        _discard_color_monitor_pending_readback=MagicMock(),
        _submit_color_monitor_pbo_readback=MagicMock(),
    )

    FileOpsMixin._on_color_monitor_frame_ready(controller, 17)

    controller._discard_color_monitor_pending_readback.assert_called_once_with()
    controller._submit_color_monitor_pbo_readback.assert_not_called()


def test_color_space_shortcut_toggles_color_video_playback(monkeypatch) -> None:
    controller = SimpleNamespace(
        current_page="Color",
        video_controller=SimpleNamespace(is_video_loaded=lambda: True),
        on_video_play_toggled=MagicMock(),
    )
    monkeypatch.setattr("src.app.main_window.QApplication.focusWidget", lambda: None)

    assert MainWindow._handle_color_space_playback_shortcut(controller) is True
    controller.on_video_play_toggled.assert_called_once_with()


def test_color_space_shortcut_ignores_text_input_focus(monkeypatch, qapp) -> None:
    from PyQt6.QtWidgets import QLineEdit

    line_edit = QLineEdit()
    controller = SimpleNamespace(
        current_page="Color",
        video_controller=SimpleNamespace(is_video_loaded=lambda: True),
        on_video_play_toggled=MagicMock(),
    )
    monkeypatch.setattr(
        "src.app.main_window.QApplication.focusWidget",
        lambda: line_edit,
    )

    assert MainWindow._handle_color_space_playback_shortcut(controller) is False
    controller.on_video_play_toggled.assert_not_called()


class _FocusContainer:
    """Small Qt-free focus container used by Edit shortcut routing tests."""

    def __init__(self, focused_child: object | None = None) -> None:
        self.focused_child = focused_child

    def isAncestorOf(self, widget: object) -> bool:
        return widget is self.focused_child


def test_edit_space_shortcut_plays_source_and_reveals_source_monitor(monkeypatch) -> None:
    source_focus = object()
    source = _FocusContainer(source_focus)
    source.current_asset = object()
    source.toggle_playback = MagicMock(return_value=True)
    source_dock = SimpleNamespace(show=MagicMock(), raise_=MagicMock())
    controller = SimpleNamespace(
        current_page="Edit",
        source_viewer_widget=source,
        timeline_panel=None,
        program_viewer_widget=None,
        dock_source_viewer=source_dock,
    )
    monkeypatch.setattr(
        "src.app.main_window.QApplication.focusWidget",
        lambda: source_focus,
    )

    assert MainWindow._handle_edit_space_playback_shortcut(controller) is True
    source.toggle_playback.assert_called_once_with()
    source_dock.show.assert_called_once_with()
    source_dock.raise_.assert_called_once_with()


def test_edit_space_shortcut_plays_timeline_and_reveals_program_monitor(monkeypatch) -> None:
    timeline_focus = object()
    timeline = _FocusContainer(timeline_focus)
    timeline.toggle_playback = MagicMock()
    timeline._playback_controller = SimpleNamespace(jkl_reset=MagicMock())
    program_dock = SimpleNamespace(show=MagicMock(), raise_=MagicMock())
    controller = SimpleNamespace(
        current_page="Edit",
        source_viewer_widget=None,
        timeline_panel=timeline,
        program_viewer_widget=None,
        dock_program_viewer=program_dock,
    )
    monkeypatch.setattr(
        "src.app.main_window.QApplication.focusWidget",
        lambda: timeline_focus,
    )

    assert MainWindow._handle_edit_space_playback_shortcut(controller) is True
    timeline._playback_controller.jkl_reset.assert_called_once_with()
    timeline.toggle_playback.assert_called_once_with()
    program_dock.show.assert_called_once_with()
    program_dock.raise_.assert_called_once_with()
