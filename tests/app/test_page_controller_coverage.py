"""Coverage tests for ``src/app/controllers/page_controller.py``.

``PageManagementMixin`` is a MainWindow mixin.  Following the conventions of
``tests/app/test_edit_to_color_playhead_sync.py`` and
``tests/app/test_color_timeline_clip_playback_mapping.py`` the units under
test run on a minimal ``QObject + PageManagementMixin`` host with fake
pages/panels/controllers (no GL, no engine, no modal dialogs).  Offscreen Qt
is only used where a real ``QPixmap``/``QTimer`` is required (thumbnail
preview degradation path, clip-strip debounce).

Covered units:

* page availability matrix and unavailable-page fallback
* ``switch_to_page`` state machine: early return vs ``force``, leave/enter
  dispatch order, layout bookkeeping (store vs legacy restore), Deliver panel
  reparenting, page-specific migration timers, viewport restore helpers,
  ``_safe_switch_to_page`` exception isolation
* leave/enter hooks per page (Edit/Deliver/Color/Composite), including the
  exception isolation of ``edit_page.on_enter`` and the Color clip-strip
  refresh failure path
* signal-connection management: ``_set_composite_playback_frame_sync``
  connect-once semantics, timeline transport control rewiring, comp-clip
  transport idempotency, transport-bar slot closure accumulation
  (BUG-C3-01 — pinned current behaviour)
* ``_KeyframesTimelineTransport`` adapter (play range policy, seek-while-
  playing pause)
* ``_show_color_timeline_clip_static_preview`` retry/degradation state
  machine (bail-outs, already-rendered / already-displayed fast paths,
  retry increment, exact-seek fallback incl. the TypeError retry)
* Composite playhead follow layering (highest enabled video track wins,
  disabled clips are gaps, boundary clamp vs scrub follow, gap no-op)
* thumbnail preview degradation (``_pixmap_to_bgr_array`` round-trip with
  real offscreen pixmaps, controller submit vs engine fallback, stale token)
* Composite workspace restore / auto-bind guards and the remembered comp
  clip resolution
* context entry handlers (Deliver sources, Edit property status updates),
  ``switch_to_page_with_context`` guard reset, ``switch_to_deliver``
* Edit→Color helpers (selection status message, delegation, precondition
  failures, frame clamping, inherited local stack re-fetch, playback pause)
* misc: ``_format_comp_time``, ``_metadata_int``, standalone image session
  guards, clip-strip debounce wiring

Suspected src bugs found while writing these tests are recorded in
``docs/testcoverage/2026-09-28/BUGS-agent-C3.md`` (BUG-C3-xx); nothing in
src is modified here.  Where a test pins the *current* behaviour of a
suspected bug it references the matching BUG-C3-xx number in a comment so
the assertion gets updated together with the fix.
"""

from __future__ import annotations

import dataclasses
import os
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple
from unittest.mock import MagicMock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest
from PyQt6.QtCore import QObject
from PyQt6.QtGui import QImage, QPixmap

from src.app.controllers import page_controller as pc
from src.app.controllers.page_controller import (
    PageManagementMixin,
    _KeyframesTimelineTransport,
)
from src.app.main_window import PageContext, PageContextManager, PageType
from src.features.edit.core.timeline_v2 import ClipInstance


# ═══════════════════════════════════════════════════════════════════════
# Fake infrastructure
# ═══════════════════════════════════════════════════════════════════════


class _FakeSignal:
    """Qt-signal look-alike: records slots, TypeError on unknown disconnect."""

    def __init__(self) -> None:
        self.slots: List[Callable[..., Any]] = []

    def connect(self, slot: Callable[..., Any]) -> None:
        self.slots.append(slot)

    def disconnect(self, slot: Callable[..., Any]) -> None:
        for index, existing in enumerate(self.slots):
            if existing == slot:
                del self.slots[index]
                return
        raise TypeError("no such slot connected")

    def emit(self, *args: Any) -> None:
        for slot in list(self.slots):
            slot(*args)


class _RecordingPlaybackController:
    """EditPlaybackController stand-in with Qt-like fake signals."""

    def __init__(
        self,
        *,
        current_frame: int = 0,
        is_playing: bool = False,
        timeline_duration: int = 40,
        fps: float = 24.0,
        playback_range: Optional[Tuple[int, int]] = None,
    ) -> None:
        self.frame_changed = _FakeSignal()
        self.state_changed = _FakeSignal()
        self.current_frame = current_frame
        self.is_playing = is_playing
        self.timeline_duration = timeline_duration
        self.fps = fps
        self.playback_range = playback_range
        self._playback_range_start: Optional[int] = None
        self._playback_range_end: Optional[int] = None
        self.seek_calls: List[int] = []
        self.range_calls: List[Tuple[Optional[int], Optional[int]]] = []
        self.clear_calls = 0
        self.loop_calls: List[bool] = []
        self.play_calls = 0
        self.pause_calls = 0

    def seek(self, frame: int) -> None:
        self.seek_calls.append(int(frame))
        self.current_frame = int(frame)

    def set_playback_range(self, lo: Optional[int], hi: Optional[int]) -> None:
        self.range_calls.append((lo, hi))
        self._playback_range_start = lo
        self._playback_range_end = hi
        self.playback_range = (lo, hi)

    def clear_playback_range(self) -> None:
        self.clear_calls += 1
        self.playback_range = None
        self._playback_range_start = None
        self._playback_range_end = None

    def set_loop(self, enabled: bool) -> None:
        self.loop_calls.append(bool(enabled))

    def play(self) -> None:
        self.play_calls += 1
        self.is_playing = True

    def pause(self) -> None:
        self.pause_calls += 1
        self.is_playing = False


_BAR_SIGNAL_NAMES: Tuple[str, ...] = (
    "seek_requested",
    "scrub_started",
    "scrub_moved",
    "scrub_finished",
    "play_toggled",
    "next_frame",
    "prev_frame",
    "first_frame",
    "last_frame",
    "mark_in_requested",
    "mark_out_requested",
    "clear_in_out_requested",
    "loop_toggled",
)


class _FakeVideoControlBar:
    """VideoControlBar stand-in exposing the transport signals the mixin wires."""

    def __init__(self) -> None:
        for name in _BAR_SIGNAL_NAMES:
            setattr(self, name, _FakeSignal())
        self.btn_loop = SimpleNamespace(isChecked=lambda: True)
        self.show_calls = 0
        self.total_frames: Optional[int] = None
        self.current_frame: Optional[int] = None
        self.frame_rate: Optional[float] = None
        self.playing: Optional[bool] = None
        self.time_infos: List[str] = []
        self.in_out_calls: List[Tuple[Optional[int], Optional[int]]] = []

    def show_for_video_with_io(self) -> None:
        self.show_calls += 1

    def set_total_frames(self, total: int) -> None:
        self.total_frames = int(total)

    def set_current_frame(self, frame: int) -> None:
        self.current_frame = int(frame)

    def set_frame_rate(self, fps: float) -> None:
        self.frame_rate = float(fps)

    def set_playing(self, playing: bool) -> None:
        self.playing = bool(playing)

    def on_frame_changed(self, frame: int) -> None:
        self.current_frame = int(frame)

    def on_play_state_changed(self, playing: bool) -> None:
        self.playing = bool(playing)

    def set_time_info(self, info: str) -> None:
        self.time_infos.append(info)

    def set_in_out_points(
        self, in_point: Optional[int], out_point: Optional[int]
    ) -> None:
        self.in_out_calls.append((in_point, out_point))


class _FakeFeatureRegistry:
    def __init__(self) -> None:
        self.leave_calls: List[str] = []
        self.enter_calls: List[str] = []

    def on_page_leave(self, page_id: str) -> None:
        self.leave_calls.append(page_id)

    def on_page_enter(self, page_id: str) -> None:
        self.enter_calls.append(page_id)


class _FakeTrack:
    def __init__(self, clips: Sequence[ClipInstance], index: int = 0) -> None:
        self.clips = tuple(clips)
        self.index = index

    def get_clip_by_id(self, clip_id: str) -> Optional[ClipInstance]:
        for clip in self.clips:
            if clip.clip_id == clip_id:
                return clip
        return None

    def get_clip_at(self, frame: int) -> Optional[ClipInstance]:
        for clip in self.clips:
            if clip.timeline_start <= frame < clip.timeline_start + clip.timeline_duration:
                return clip
        return None


def _fake_timeline(
    tracks: Sequence[_FakeTrack],
    *,
    timeline_id: str = "tl-1",
    video_tracks: Optional[Sequence[_FakeTrack]] = None,
) -> SimpleNamespace:
    all_tracks = tuple(tracks)

    def _get_clip_by_id(clip_id: str) -> Optional[ClipInstance]:
        for track in all_tracks:
            clip = track.get_clip_by_id(clip_id)
            if clip is not None:
                return clip
        return None

    return SimpleNamespace(
        timeline_id=timeline_id,
        all_tracks=all_tracks,
        video_tracks=tuple(video_tracks if video_tracks is not None else tracks),
        get_clip_by_id=_get_clip_by_id,
        width=1920,
        height=1080,
    )


def _clip(
    clip_id: str,
    *,
    asset_id: str = "asset-1",
    timeline_start: int = 0,
    duration: int = 40,
    source_start: int = 0,
    color_stack_id: Optional[str] = None,
    composite_id: Optional[str] = None,
    is_enabled: bool = True,
) -> ClipInstance:
    return ClipInstance(
        clip_id=clip_id,
        asset_id=asset_id,
        track_id="v1",
        timeline_start=timeline_start,
        timeline_duration=duration,
        source_start=source_start,
        source_duration=duration,
        color_stack_id=color_stack_id,
        composite_id=composite_id,
        is_enabled=is_enabled,
    )


class _PageHost(QObject, PageManagementMixin):
    """Minimal MainWindow stand-in for PageManagementMixin units."""

    def __init__(self, *, current_page: str = "Library") -> None:
        super().__init__()
        self.current_page = current_page
        self.feature_registry: Optional[_FakeFeatureRegistry] = None
        self._edit_available = True
        self._composite_available = True
        self._deliver_available = True
        self._page_context_manager = PageContextManager()
        self._contextual_page_switch_in_progress = False
        self._current_edit_project: Any = None
        self._current_timeline_sequences: Dict[str, Any] = {}
        self.timeline_panel: Any = None
        self.edit_page: Any = None
        self.deliver_page: Any = None
        self.timeline_preview_renderer: Any = None
        self.video_controller: Any = None
        self.current_file_path: Optional[str] = None
        self.current_file_is_video = False

        self.canvas = SimpleNamespace(
            cancel_active_tools=MagicMock(),
            set_sam_mode=MagicMock(),
            clear_sam_points=MagicMock(),
        )
        self.engine = SimpleNamespace(img_full_original=None)
        self.mask_panel = SimpleNamespace(
            btn_brush=SimpleNamespace(setChecked=MagicMock()),
        )
        self.qualifier_panel = SimpleNamespace(set_picker_active=MagicMock())
        self.status_messages: List[Tuple[str, int]] = []
        self.status_bar = SimpleNamespace(
            showMessage=lambda message, timeout=0: self.status_messages.append(
                (message, timeout)
            )
        )
        self.dock_properties: Any = None
        self.edit_properties_panel: Any = None
        self.composite_page: Any = None
        self.composite_viewer: Any = None
        self.viewport_container: Any = None
        self.dock_viewport: Any = None
        self._composite_adapter: Any = None
        self.page_buttons: Dict[str, Any] = {}
        self.page_layouts: Dict[str, Any] = {}
        self._save_state_counter = 0
        self.restore_state_calls: List[Any] = []
        self.update_all_dock_titlebars = MagicMock()
        self._page_switch_from_page: Optional[str] = None
        self.composite_context_log: List[Any] = []
        self.timeline_clip_context_calls: List[Tuple[Any, str, str]] = []

        # Leave/enter dispatch order recording (wrap the mixin impls).
        self.hook_log: List[Tuple[str, str]] = []

        def _leave(page_name: str) -> None:
            self.hook_log.append(("leave", page_name))
            PageManagementMixin.leave_page_hook(self, page_name)

        def _enter(page_name: str) -> None:
            self.hook_log.append(("enter", page_name))
            PageManagementMixin.enter_page_hook(self, page_name)

        self.leave_page_hook = _leave  # type: ignore[method-assign]
        self.enter_page_hook = _enter  # type: ignore[method-assign]

    def saveState(self) -> str:  # noqa: N802 - Qt naming convention
        self._save_state_counter += 1
        return f"state-{self._save_state_counter}"

    def restoreState(self, state: Any) -> None:  # noqa: N802 - Qt naming
        self.restore_state_calls.append(state)

    def _handle_enter_composite_with_context(self, context: Any) -> None:
        self.composite_context_log.append(context)

    def _setup_timeline_clip_context(
        self, context: Any, composite_id: str, clip_id: str
    ) -> None:
        self.timeline_clip_context_calls.append((context, composite_id, clip_id))


class _SingleShotRecorder:
    """QTimer stand-in capturing ``singleShot`` scheduling deterministically."""

    def __init__(self) -> None:
        self.calls: List[Tuple[int, Callable[..., Any]]] = []

    def singleShot(self, ms: int, callback: Callable[..., Any]) -> None:
        self.calls.append((int(ms), callback))


def _preview_video_controller(**overrides: Any) -> SimpleNamespace:
    """Legacy Color video controller double for the static-preview paths.

    Defaults mirror a controller that has no async preview API wired and is
    paused with a loaded video.
    """
    attrs: Dict[str, Any] = {
        "is_playing": False,
        "is_video_loaded": lambda: True,
        "_last_static_preview_delivered": False,
        "static_preview_shows_frame": lambda frame: False,
        "request_static_preview_frame": None,
        "seek_to_frame": MagicMock(),
        "_use_dual_path": False,
        "_coordinator": None,
    }
    attrs.update(overrides)
    return SimpleNamespace(**attrs)


def _make_composite_page(**extra: Any) -> MagicMock:
    page = MagicMock(**extra)
    page._runtime_context = None
    page._is_image_color_mode = False
    return page


def _make_timeline_panel(
    controller: Optional[_RecordingPlaybackController],
    *,
    playhead: int = 0,
    selected: Optional[Sequence[str]] = None,
    scrubbing: bool = False,
) -> MagicMock:
    panel = MagicMock()
    panel.get_playback_controller.return_value = controller
    panel.get_playhead_frame.return_value = playhead
    panel.get_selected_clip_ids.return_value = list(selected or [])
    panel._scrubbing_playhead = scrubbing
    return panel


# ═══════════════════════════════════════════════════════════════════════
# A. Page availability & switch_to_page state machine
# ═══════════════════════════════════════════════════════════════════════


def test_page_availability_matrix() -> None:
    host = _PageHost()
    host._edit_available = True
    host._composite_available = False
    host._deliver_available = True

    assert host._is_page_available("Edit") is True
    assert host._is_page_available("edit") is True
    assert host._is_page_available("Composite") is False
    assert host._is_page_available("COMPOSITE") is False
    assert host._is_page_available("Deliver") is True
    # Library/Color are always available; anything else is not a page.
    assert host._is_page_available("Library") is True
    assert host._is_page_available("Color") is True
    assert host._is_page_available("Fusion") is False


def test_switch_to_unavailable_page_falls_back_to_color() -> None:
    host = _PageHost(current_page="Library")

    host.switch_to_page("Fusion")

    assert host.current_page == "Color"
    # leave dispatched for the departing page, enter for the fallback target.
    assert host.hook_log == [("leave", "Library"), ("enter", "Color")]


def test_switch_to_same_page_with_known_layout_returns_early() -> None:
    host = _PageHost(current_page="Color")
    host.page_layouts["Color"] = "known-layout"

    host.switch_to_page("Color")

    assert host.hook_log == []
    host.canvas.cancel_active_tools.assert_not_called()
    assert host.status_messages == []


def test_switch_to_same_page_force_runs_the_full_switch() -> None:
    host = _PageHost(current_page="Color")
    host.page_layouts["Color"] = "known-layout"

    host.switch_to_page("Color", force=True)

    assert host.hook_log == [("leave", "Color"), ("enter", "Color")]
    host.canvas.cancel_active_tools.assert_called_once()


def test_switch_dispatches_leave_then_enter_and_records_from_page() -> None:
    host = _PageHost(current_page="Library")
    registry = _FakeFeatureRegistry()
    host.feature_registry = registry

    host.switch_to_page("Color")

    # Registry notification precedes the page-specific hooks (R-005).
    assert registry.leave_calls == ["library"]
    assert registry.enter_calls == ["color"]
    assert host.hook_log == [("leave", "Library"), ("enter", "Color")]
    assert host._page_switch_from_page == "Library"
    assert host.current_page == "Color"
    # Tool state reset ran for the departing canvas.
    host.canvas.cancel_active_tools.assert_called_once()
    host.canvas.set_sam_mode.assert_called_once_with(False)
    host.canvas.clear_sam_points.assert_called_once()
    host.mask_panel.btn_brush.setChecked.assert_called_once_with(False)
    host.qualifier_panel.set_picker_active.assert_called_once_with(False)


def test_switch_resets_sam_engine_points_when_supported() -> None:
    host = _PageHost(current_page="Library")
    host.engine = SimpleNamespace(reset_sam_points=MagicMock())

    host.switch_to_page("Color")

    host.engine.reset_sam_points.assert_called_once()


def test_switch_saves_departing_layout_and_status_message() -> None:
    host = _PageHost(current_page="Library")

    host.switch_to_page("Color")

    assert host.page_layouts["Library"] == "state-1"
    assert host.status_messages == [("Mode: Color", 2000)]


def test_switch_restores_legacy_layout_when_store_unavailable() -> None:
    host = _PageHost(current_page="Library")
    host.page_layouts["Color"] = "color-layout-blob"

    host.switch_to_page("Color")

    assert host.restore_state_calls == ["color-layout-blob"]
    # New page's layout was not overwritten by the departing saveState.
    assert host.page_layouts["Library"] == "state-1"


def test_switch_store_restore_takes_precedence_over_legacy() -> None:
    host = _PageHost(current_page="Library")
    host.page_layouts["Color"] = "legacy-blob"
    host._restore_layout_from_store = MagicMock(return_value=True)  # type: ignore[method-assign]
    host._save_current_layout_to_store = MagicMock()  # type: ignore[method-assign]

    host.switch_to_page("Color")

    host._restore_layout_from_store.assert_called_once_with("Color")
    host._save_current_layout_to_store.assert_called_once_with("Library")
    assert host.restore_state_calls == []


def test_switch_without_any_layout_seeds_target_from_current_state() -> None:
    host = _PageHost(current_page="Library")

    host.switch_to_page("Color")

    # New page without saved layout: current state becomes its layout.
    assert host.page_layouts["Color"] == "state-2"


def test_switch_updates_page_button_checked_states() -> None:
    host = _PageHost(current_page="Library")
    buttons = {
        name: SimpleNamespace(setChecked=MagicMock())
        for name in ("Library", "Color", "Edit")
    }
    host.page_buttons = buttons

    host.switch_to_page("Color")

    for name, button in buttons.items():
        button.setChecked.assert_called_once_with(name == "Color")


def test_switch_from_deliver_restores_edit_panels() -> None:
    host = _PageHost(current_page="Deliver")
    restore = MagicMock()
    host._restore_edit_panels_from_deliver = restore  # type: ignore[method-assign]

    host.switch_to_page("Color")

    restore.assert_called_once()


@pytest.mark.parametrize("target,expected_mode", [
    ("Color", "other"), ("Composite", "other"), ("Edit", "edit"),
])
def test_deliver_return_preserves_destination_playback_mode(
    target: str, expected_mode: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Returning the borrowed widgets must not force other pages into Edit."""
    from src.app.controllers.dock_layout_controller import DockLayoutMixin
    from src.features.edit.ui.viewport_playback_coordinator import (
        ViewportPlaybackCoordinator,
    )

    monkeypatch.setattr(pc, "QTimer", _SingleShotRecorder())
    host = _PageHost(current_page="Deliver")
    source = SimpleNamespace(current_asset=None, is_playing=False)
    controller = SimpleNamespace(is_playing=False)
    coordinator = ViewportPlaybackCoordinator(source, controller, parent=host)
    viewport = SimpleNamespace(
        dual_view=False, active_monitor="source",
        restore_program_viewer=MagicMock(), set_deliver_mode=MagicMock(),
    )
    coordinator.attach_viewport(viewport)
    coordinator.enter_deliver_page()
    host._viewport_playback_coordinator = coordinator
    host.edit_viewport = viewport
    host.deliver_page = SimpleNamespace(detach_edit_panels=MagicMock())
    host._restore_edit_panels_from_deliver = lambda: (
        DockLayoutMixin._restore_edit_panels_from_deliver(host)
    )

    host.switch_to_page(target)

    assert host.current_page == target
    assert coordinator.snapshot().page_mode == expected_mode
    viewport.restore_program_viewer.assert_called_once()
    host.deliver_page.detach_edit_panels.assert_called_once()


def test_switch_to_deliver_attaches_edit_panels() -> None:
    host = _PageHost(current_page="Library")
    attach = MagicMock()
    host._attach_edit_panels_to_deliver = attach  # type: ignore[method-assign]

    host.switch_to_page("Deliver")

    attach.assert_called_once()
    assert host.current_page == "Deliver"


def test_switch_staying_in_deliver_does_not_restore_edit_panels() -> None:
    host = _PageHost(current_page="Deliver")
    restore = MagicMock()
    host._restore_edit_panels_from_deliver = restore  # type: ignore[method-assign]

    host.switch_to_page("Deliver")

    restore.assert_not_called()


def test_switch_schedules_edit_migration_timers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = _SingleShotRecorder()
    monkeypatch.setattr(pc, "QTimer", recorder)
    host = _PageHost(current_page="Library")
    migrations = {
        "_migrate_edit_properties_dock_width": MagicMock(),
        "_migrate_edit_info_dock_visibility": MagicMock(),
        "_migrate_ai_assistant_dock_layout": MagicMock(),
        "_migrate_edit_timeline_dock_height": MagicMock(),
    }
    for name, mock in migrations.items():
        setattr(host, name, mock)

    host.switch_to_page("Edit")

    scheduled = [callback for _ms, callback in recorder.calls]
    for mock in migrations.values():
        assert mock in scheduled
    # The trailing viewport-restore pass is scheduled too.
    assert host._restore_viewport_after_page_switch in scheduled
    # None of the timers fired synchronously.
    for mock in migrations.values():
        mock.assert_not_called()


def test_switch_schedules_color_composite_deliver_migrations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = _SingleShotRecorder()
    monkeypatch.setattr(pc, "QTimer", recorder)
    host = _PageHost(current_page="Library")
    host._migrate_color_grading_dock_height = MagicMock()
    host._migrate_color_mapping_dock_layout = MagicMock()
    host._migrate_composite_scopes_dock_visibility = MagicMock()
    host._migrate_deliver_layout = MagicMock()

    host.switch_to_page("Color")
    host.switch_to_page("Composite")
    host.switch_to_page("Deliver")

    scheduled = [callback for _ms, callback in recorder.calls]
    assert host._migrate_color_grading_dock_height in scheduled
    assert host._migrate_color_mapping_dock_layout in scheduled
    assert host._migrate_composite_scopes_dock_visibility in scheduled
    assert host._migrate_deliver_layout in scheduled


def test_switch_enforces_page_dock_visibility_when_present() -> None:
    host = _PageHost(current_page="Library")
    enforce = MagicMock()
    host._enforce_page_dock_visibility = enforce  # type: ignore[method-assign]

    host.switch_to_page("Color")

    enforce.assert_called_once()


def test_safe_switch_to_page_reports_failure_without_raising() -> None:
    host = _PageHost(current_page="Library")

    def _boom(page_name: str) -> None:
        raise RuntimeError("boom")

    host.switch_to_page = _boom  # type: ignore[method-assign]

    host._safe_switch_to_page("Edit")

    assert len(host.status_messages) == 1
    message, timeout = host.status_messages[0]
    assert "手动切换" in message
    assert timeout == 10000


def test_safe_switch_to_page_swallows_success_path() -> None:
    host = _PageHost(current_page="Library")
    host.switch_to_page = MagicMock()  # type: ignore[method-assign]

    host._safe_switch_to_page("Color")

    host.switch_to_page.assert_called_once_with("Color")
    assert host.status_messages == []


# ═══════════════════════════════════════════════════════════════════════
# B. Leave / enter hooks per page
# ═══════════════════════════════════════════════════════════════════════


def test_leave_hook_edit_calls_edit_page_on_leave() -> None:
    host = _PageHost()
    edit_page = MagicMock()
    host._edit_available = True
    host.edit_page = edit_page

    host.leave_page_hook("Edit")

    edit_page.on_leave.assert_called_once()


def test_leave_hook_deliver_calls_edit_page_on_leave() -> None:
    host = _PageHost()
    edit_page = MagicMock()
    host._edit_available = True
    host.edit_page = edit_page

    host.leave_page_hook("Deliver")

    edit_page.on_leave.assert_called_once()


def test_leave_hook_color_stops_playback_and_timeline_preview() -> None:
    host = _PageHost()
    video_controller = _preview_video_controller(is_playing=True)
    stop = MagicMock()
    video_controller.stop_playback = stop
    host.video_controller = video_controller
    deactivate = MagicMock()
    host._deactivate_color_timeline_preview = deactivate  # type: ignore[method-assign]

    host.leave_page_hook("Color")

    stop.assert_called_once()
    deactivate.assert_called_once()


def test_leave_hook_color_isolates_stop_playback_failure() -> None:
    host = _PageHost()
    video_controller = _preview_video_controller(is_playing=True)
    video_controller.stop_playback = MagicMock(
        side_effect=RuntimeError("dead decoder")
    )
    host.video_controller = video_controller

    host.leave_page_hook("Color")  # must not raise


def test_leave_hook_composite_resets_binding_and_playback_state() -> None:
    host = _PageHost()
    composite_page = _make_composite_page()
    host.composite_page = composite_page
    host._composite_adapter = MagicMock()
    host._suppress_composite_auto_bind_once = True
    controller = _RecordingPlaybackController()
    host.timeline_panel = _make_timeline_panel(controller)
    host._set_composite_playback_frame_sync(True)
    assert controller.frame_changed.slots  # connected

    host._configure_composite_timeline_playback_controls(True)
    host.leave_page_hook("Composite")

    assert host._suppress_composite_auto_bind_once is False
    assert controller.frame_changed.slots == []  # frame sync disconnected
    assert controller.clear_calls == 1
    composite_page.comp_pause.assert_called_once()
    # Keyframes panel falls back to its internal transport when leaving.
    composite_page.set_keyframes_transport.assert_called_with(None)


def test_leave_hook_composite_uses_adapter_when_present() -> None:
    host = _PageHost()
    adapter = MagicMock()
    host._composite_adapter = adapter
    host.composite_page = _make_composite_page()
    host.timeline_panel = _make_timeline_panel(None)

    host.leave_page_hook("Composite")

    adapter.on_leave_page.assert_called_once()


def test_leave_hook_composite_legacy_path_restores_viewport() -> None:
    host = _PageHost()
    host._composite_adapter = None
    composite_page = _make_composite_page()
    host.composite_page = composite_page
    host.timeline_panel = _make_timeline_panel(None)
    viewer = MagicMock()
    host.composite_viewer = viewer
    container = object()
    host.viewport_container = container
    dock = MagicMock()
    host.dock_viewport = dock
    engine = SimpleNamespace(
        renderer=object(),
        render_pipeline=MagicMock(),
    )
    host.engine = engine

    host.leave_page_hook("Composite")

    composite_page.on_leave.assert_called_once()
    dock.setWidget.assert_called_once_with(container)
    viewer.hide.assert_called_once()
    engine.render_pipeline.assert_called_once_with(
        render_reason="viewport_refresh"
    )


def test_enter_hook_edit_binds_properties_dock_and_enters_page() -> None:
    host = _PageHost()
    properties_dock = MagicMock()
    properties_panel = object()
    host.dock_properties = properties_dock
    host.edit_properties_panel = properties_panel
    edit_page = MagicMock()
    host.edit_page = edit_page
    host._current_edit_project = None

    host.enter_page_hook("Edit")

    properties_dock.setWidget.assert_called_once_with(properties_panel)
    edit_page.on_enter.assert_called_once()


def test_enter_hook_edit_isolates_on_enter_exception() -> None:
    host = _PageHost()
    edit_page = MagicMock()
    edit_page.on_enter.side_effect = RuntimeError("ui init failed")
    host.edit_page = edit_page

    host.enter_page_hook("Edit")  # must not raise

    edit_page.on_enter.assert_called_once()


def test_enter_hook_edit_refreshes_composite_preview_when_flagged() -> None:
    host = _PageHost()
    edit_page = MagicMock()
    host.edit_page = edit_page
    refresh = MagicMock()
    host._refresh_edit_preview_after_composite_change = refresh  # type: ignore[method-assign]
    host._edit_preview_needs_composite_refresh = True

    host.enter_page_hook("Edit")

    refresh.assert_called_once()

    # Failure of the refresh must not break page entry.
    refresh.side_effect = RuntimeError("no composite")
    host.enter_page_hook("Edit")


def test_enter_hook_edit_inits_timeline_preview_with_project() -> None:
    host = _PageHost()
    edit_page = MagicMock()
    host.edit_page = edit_page
    host._current_edit_project = object()
    init = MagicMock()
    host._init_timeline_preview_renderer = init  # type: ignore[method-assign]

    host.enter_page_hook("Edit")

    init.assert_called_once()


def test_enter_hook_color_syncs_selector_and_refreshes_strip() -> None:
    host = _PageHost()
    sync = MagicMock()
    host._sync_color_timeline_selector = sync  # type: ignore[method-assign]
    strip = MagicMock()
    host.color_timeline_clip_strip = strip
    host._current_edit_project = None

    host.enter_page_hook("Color")

    sync.assert_called_once()
    strip.set_project_and_timeline.assert_called_once_with(None, None)


def test_enter_hook_color_isolates_strip_refresh_failure() -> None:
    host = _PageHost()
    strip = MagicMock()
    strip.set_project_and_timeline.side_effect = RuntimeError("strip boom")
    host.color_timeline_clip_strip = strip

    host.enter_page_hook("Color")  # must not raise


def test_enter_hook_deliver_sets_project_sequence_and_edit_entry() -> None:
    host = _PageHost()
    deliver_page = MagicMock()
    host.deliver_page = deliver_page
    edit_page = MagicMock()
    host.edit_page = edit_page
    sequence = object()
    project = SimpleNamespace(active_timeline_id="tl-1", project_id="p1")
    host._current_edit_project = project
    host._current_timeline_sequences = {"tl-1": sequence}

    host.enter_page_hook("Deliver")

    deliver_page.set_project.assert_called_once_with(project)
    deliver_page.set_sequence.assert_called_once_with(sequence)
    deliver_page.set_timeline_sequences.assert_called_once_with({"tl-1": sequence})
    edit_page.on_enter.assert_called_once()


def test_enter_hook_deliver_without_active_timeline_clears_sequence() -> None:
    host = _PageHost()
    deliver_page = MagicMock()
    host.deliver_page = deliver_page
    host._current_edit_project = SimpleNamespace(
        active_timeline_id=None, project_id="p1"
    )

    host.enter_page_hook("Deliver")

    deliver_page.set_sequence.assert_called_once_with(None)


def test_enter_hook_deliver_sets_export_size_from_engine_image() -> None:
    host = _PageHost()
    deliver_page = MagicMock()
    host.deliver_page = deliver_page
    export_panel = MagicMock()
    host.export_panel = export_panel
    host._current_edit_project = SimpleNamespace(
        active_timeline_id=None, project_id="p1"
    )
    host.engine = SimpleNamespace(
        img_full_original=np.zeros((4, 6, 3), dtype=np.uint8)
    )
    host.current_file_path = "/tmp/img.exr"

    host.enter_page_hook("Deliver")

    export_panel.set_original_size.assert_called_once_with(6, 4)
    export_panel.set_default_path.assert_called_once_with("/tmp/img.exr")


# ═══════════════════════════════════════════════════════════════════════
# C. Viewport restore helpers
# ═══════════════════════════════════════════════════════════════════════


def test_restore_viewport_updates_composite_viewer_on_composite_page() -> None:
    host = _PageHost(current_page="Composite")
    viewer = MagicMock()
    host.composite_viewer = viewer
    engine = MagicMock()
    host.engine = engine

    host._restore_viewport_after_page_switch()

    viewer.update.assert_called_once()
    engine.render_pipeline.assert_not_called()


def test_restore_viewport_normal_path_triggers_render_and_finalizes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = _SingleShotRecorder()
    monkeypatch.setattr(pc, "QTimer", recorder)
    host = _PageHost(current_page="Color")
    gl_viewport = MagicMock()
    gl_viewport.isVisible.return_value = True
    scene = MagicMock()
    host.canvas = SimpleNamespace(
        gl_viewport=gl_viewport,
        _update_lod=MagicMock(),
        scene=scene,
    )
    render = MagicMock()
    host.engine = SimpleNamespace(render_pipeline=render)
    dock = MagicMock()
    dock.isVisible.return_value = True
    host.dock_viewport = dock

    host._restore_viewport_after_page_switch()

    # AUD-3107：UI 线程不得调用 makeCurrent（无后续 GL 调用也无 doneCurrent
    # 配对）；可见性 + update/invalidate 已足以触发重绘。
    gl_viewport.makeCurrent.assert_not_called()
    gl_viewport.update.assert_called_once()
    host.canvas._update_lod.assert_called_once()
    scene.invalidate.assert_called_once()
    render.assert_called_once_with(render_reason="viewport_refresh")
    assert (50, host._finalize_viewport_restore) in recorder.calls


def test_restore_viewport_swallows_exceptions() -> None:
    host = _PageHost(current_page="Color")
    host.canvas = SimpleNamespace(
        gl_viewport=None,
        _update_lod=MagicMock(side_effect=RuntimeError("no gl")),
    )

    host._restore_viewport_after_page_switch()  # must not raise


def test_finalize_viewport_restore_skips_composite_and_handles_errors() -> None:
    host = _PageHost(current_page="Composite")
    host._finalize_viewport_restore()

    switching = _PageHost(current_page="Color")
    switching.canvas = SimpleNamespace(
        viewport=MagicMock(side_effect=RuntimeError("gone"))
    )
    switching.engine = SimpleNamespace(render_pipeline=MagicMock())
    switching._finalize_viewport_restore()  # must not raise


def test_finalize_viewport_restore_renders_second_pass() -> None:
    host = _PageHost(current_page="Color")
    viewport = MagicMock()
    host.canvas = SimpleNamespace(viewport=lambda: viewport)
    render = MagicMock()
    host.engine = SimpleNamespace(render_pipeline=render)

    host._finalize_viewport_restore()

    viewport.update.assert_called_once()
    render.assert_called_once_with(render_reason="viewport_refresh")


# ═══════════════════════════════════════════════════════════════════════
# D. Signal connection management
# ═══════════════════════════════════════════════════════════════════════


def test_playback_frame_sync_connects_once_and_disconnects_cleanly() -> None:
    host = _PageHost()
    controller = _RecordingPlaybackController()
    host.timeline_panel = _make_timeline_panel(controller)

    host._set_composite_playback_frame_sync(True)
    assert len(controller.frame_changed.slots) == 1
    assert host._composite_playback_frame_sync_connected is True

    # Re-enabling must not stack a second connection.
    host._set_composite_playback_frame_sync(True)
    assert len(controller.frame_changed.slots) == 1

    host._set_composite_playback_frame_sync(False)
    assert controller.frame_changed.slots == []
    assert host._composite_playback_frame_sync_connected is False

    # Disabling again is a no-op.
    host._set_composite_playback_frame_sync(False)
    assert controller.frame_changed.slots == []

    # Re-enable after disconnect reconnects exactly once.
    host._set_composite_playback_frame_sync(True)
    assert len(controller.frame_changed.slots) == 1


def test_playback_frame_sync_without_signal_bearing_controller() -> None:
    host = _PageHost()
    host.timeline_panel = _make_timeline_panel(None)
    host._set_composite_playback_frame_sync(True)
    assert getattr(host, "_composite_playback_frame_sync_connected", False) is False

    host.timeline_panel = None
    host._set_composite_playback_frame_sync(True)  # no crash


def _timeline_controls_host(
    controller: _RecordingPlaybackController,
) -> Tuple[_PageHost, _FakeVideoControlBar, MagicMock]:
    host = _PageHost()
    bar = _FakeVideoControlBar()
    host.composite_viewer = SimpleNamespace(video_control_bar=bar)
    host.composite_page = _make_composite_page()
    host.composite_page._comp_clip_timeline_start = 100
    host.composite_page.comp_clip_total_frames.return_value = 48
    host.timeline_panel = _make_timeline_panel(controller)
    return host, bar, host.composite_page


def test_timeline_controls_rewire_disconnects_previous_frame_state_slots() -> None:
    controller = _RecordingPlaybackController(current_frame=5)
    host, _bar, page = _timeline_controls_host(controller)

    host._configure_composite_timeline_playback_controls(True)
    assert len(controller.frame_changed.slots) == 1
    assert len(controller.state_changed.slots) == 2  # bar + keyframes notify

    host._configure_composite_timeline_playback_controls(True)
    # Previous closures were disconnected: no accumulation on the controller.
    assert len(controller.frame_changed.slots) == 1
    assert len(controller.state_changed.slots) == 2
    # Keyframes panel keeps the shared transport adapter.
    transport = page.set_keyframes_transport.call_args.args[0]
    assert isinstance(transport, _KeyframesTimelineTransport)


def test_timeline_controls_bar_slots_accumulate_across_rewires() -> None:
    """BUG-C3-01 (fixed): the 13 transport-bar closures used to be freshly
    created on every rewire while the disconnect loop only targeted the *new*
    closures, so previous enters stayed connected and captured stale
    controllers.  Now the previous closures are recorded in
    ``_composite_timeline_bar_slots`` and torn down on the next rewire.
    See docs/testcoverage/2026-09-28/BUGS-agent-C3.md.
    """
    controller = _RecordingPlaybackController(current_frame=5)
    host, bar, _page = _timeline_controls_host(controller)

    host._configure_composite_timeline_playback_controls(True)
    assert len(bar.play_toggled.slots) == 1
    first_slots = list(bar.play_toggled.slots)

    host._configure_composite_timeline_playback_controls(True)
    assert len(bar.play_toggled.slots) == 1, "BUG-C3-01: stale slot must be disconnected"
    assert bar.play_toggled.slots[0] not in first_slots, "rewire installs fresh closures"
    # 其它 bar 信号同样不得累积。
    assert len(bar.seek_requested.slots) == 1
    assert len(bar.loop_toggled.slots) == 1


def test_timeline_controls_disabled_clears_keyframes_transport() -> None:
    host = _PageHost()
    host.composite_page = _make_composite_page()
    host.composite_viewer = None
    host.timeline_panel = _make_timeline_panel(None)

    host._configure_composite_timeline_playback_controls(True)

    host.composite_page.set_keyframes_transport.assert_called_once_with(None)


def test_timeline_controls_image_color_mode_is_a_noop() -> None:
    host = _PageHost()
    page = _make_composite_page()
    page._is_image_color_mode = True
    host.composite_page = page
    host.timeline_panel = _make_timeline_panel(_RecordingPlaybackController())

    host._configure_composite_timeline_playback_controls(True)

    page.set_keyframes_transport.assert_not_called()


def test_timeline_controls_map_bar_signals_to_global_frames() -> None:
    controller = _RecordingPlaybackController(current_frame=110)
    host, bar, _page = _timeline_controls_host(controller)

    host._configure_composite_timeline_playback_controls(True)

    # Seeding seeks to the clip start and constrains the playback range.
    assert controller.seek_calls == [100]
    assert controller.range_calls == [(100, 147)]
    assert controller.loop_calls == [True]

    # clip_start=100, clip_total=48 → local 10 maps to global 110.
    bar.seek_requested.emit(10)
    assert controller.seek_calls == [100, 110]

    # Playing → play button pauses.
    controller.is_playing = True
    bar.play_toggled.emit()
    assert controller.pause_calls == 1

    # Not playing → keep the valid in-clip range, then play.
    controller.range_calls.clear()
    bar.play_toggled.emit()
    assert controller.range_calls == []
    assert controller.play_calls == 1

    # Mark in/out operate in global frames and mirror to the bar (local).
    controller.current_frame = 120
    controller._playback_range_end = 140
    host._composite_default_playback_range = False
    bar.mark_in_requested.emit()
    assert controller.range_calls[-1] == (120, 140)
    assert bar.in_out_calls[-1] == (20, 40)

    bar.clear_in_out_requested.emit()
    assert controller.clear_calls == 1
    assert bar.in_out_calls[-1] == (None, None)

    bar.loop_toggled.emit(True)
    assert controller.loop_calls == [True, True]


def test_timeline_controls_seed_bar_from_controller_duration() -> None:
    controller = _RecordingPlaybackController(
        current_frame=30, fps=25.0, timeline_duration=48
    )
    host = _PageHost()
    bar = _FakeVideoControlBar()
    host.composite_viewer = SimpleNamespace(video_control_bar=bar)
    # Page without the comp-clip frame API → fall back to timeline_duration.
    host.composite_page = SimpleNamespace(
        _comp_clip_timeline_start=0, _is_image_color_mode=False
    )
    host.timeline_panel = _make_timeline_panel(controller)

    host._configure_composite_timeline_playback_controls(True)

    assert bar.show_calls == 1
    assert bar.total_frames == 48
    assert bar.frame_rate == 25.0
    assert controller.seek_calls == [0]  # playhead reset to clip start
    assert controller.range_calls == [(0, 47)]
    assert controller.loop_calls == [True]  # synced from bar's ⟳ button


def test_comp_clip_controls_are_idempotent_across_rewires() -> None:
    host = _PageHost()
    bar = _FakeVideoControlBar()
    host.composite_viewer = SimpleNamespace(
        video_control_bar=bar,
        update_video_control_bar_position=MagicMock(),
    )
    page = _make_composite_page()
    page.is_comp_clip_playback_active.return_value = True
    page.comp_clip_total_frames.return_value = 48
    page.comp_clip_fps.return_value = 24.0
    page._composite_current_frame = 5
    page._comp_playing = False
    page._comp_in_frame = 0
    page._comp_out_frame = 47
    page.composite_frame_changed = _FakeSignal()
    host.composite_page = page

    host._configure_composite_comp_clip_controls()
    host._configure_composite_comp_clip_controls()

    for name in _BAR_SIGNAL_NAMES:
        signal = getattr(bar, name)
        assert len(signal.slots) == 1, name
    assert len(page.composite_frame_changed.slots) == 1


def test_comp_clip_controls_skip_when_playback_inactive() -> None:
    host = _PageHost()
    bar = _FakeVideoControlBar()
    host.composite_viewer = SimpleNamespace(video_control_bar=bar)
    page = _make_composite_page()
    page.is_comp_clip_playback_active.return_value = False
    host.composite_page = page

    host._configure_composite_comp_clip_controls()

    assert bar.show_calls == 0
    for name in _BAR_SIGNAL_NAMES:
        assert getattr(bar, name).slots == []


def test_comp_clip_controls_drive_page_engine_and_bar() -> None:
    host = _PageHost()
    bar = _FakeVideoControlBar()
    host.composite_viewer = SimpleNamespace(
        video_control_bar=bar,
        update_video_control_bar_position=MagicMock(),
    )
    page = _make_composite_page()
    page.is_comp_clip_playback_active.return_value = True
    page.comp_clip_total_frames.return_value = 48
    page.comp_clip_fps.return_value = 24.0
    page._composite_current_frame = 5
    page._comp_playing = False
    page._comp_in_frame = 0
    page._comp_out_frame = 47
    page.composite_frame_changed = _FakeSignal()
    host.composite_page = page

    host._configure_composite_comp_clip_controls()

    assert bar.total_frames == 48
    assert bar.current_frame == 5
    assert bar.frame_rate == 24.0
    assert bar.time_infos == ["00:00:00.21 / 00:00:02.00"]
    page.set_comp_loop.assert_called_once_with(True)  # bar default ⟳ checked

    bar.seek_requested.emit(12)
    page.comp_seek.assert_called_once_with(12)
    assert bar.current_frame == page._composite_current_frame

    bar.scrub_started.emit(3)
    # Not playing → no pause toggle, just the seek.
    assert page.comp_toggle_play.call_count == 0
    page.comp_seek.assert_called_with(3)

    bar.next_frame.emit()
    page.comp_next_frame.assert_called_once()

    bar.mark_in_requested.emit()
    page.set_comp_clip_in_out.assert_called_once_with(5, 47)

    page.composite_frame_changed.emit(30)
    assert bar.current_frame == 30


def test_format_comp_time_formats_and_clamps_fps() -> None:
    assert (
        PageManagementMixin._format_comp_time(5, 48, 24.0)
        == "00:00:00.21 / 00:00:02.00"
    )
    # fps clamped to >= 1 so a bogus 0 does not divide by zero.
    assert "00:00:05.00 / 00:00:10.00" == PageManagementMixin._format_comp_time(
        5, 10, 0.0
    )
    assert PageManagementMixin._format_comp_time(-3, 10, 24.0).startswith(
        "00:00:00.00"
    )


# ═══════════════════════════════════════════════════════════════════════
# E. _KeyframesTimelineTransport
# ═══════════════════════════════════════════════════════════════════════


def test_keyframes_transport_play_applies_workspace_range() -> None:
    controller = _RecordingPlaybackController()
    transport = _KeyframesTimelineTransport(controller, clip_start=100, clip_total=48)

    transport.set_play_range(5, 20)
    transport.play()

    # set_play_range applies immediately; play() re-applies the workspace.
    assert controller.range_calls == [(105, 120), (105, 120)]
    assert controller.play_calls == 1


def test_keyframes_transport_play_keeps_valid_existing_range() -> None:
    controller = _RecordingPlaybackController(playback_range=(110, 130))
    transport = _KeyframesTimelineTransport(controller, clip_start=100, clip_total=48)

    transport.play()

    assert controller.range_calls == []  # existing in-clip range preserved
    assert controller.play_calls == 1


def test_keyframes_transport_play_resets_out_of_clip_range() -> None:
    controller = _RecordingPlaybackController(playback_range=(0, 500))
    transport = _KeyframesTimelineTransport(controller, clip_start=100, clip_total=48)

    transport.play()

    assert controller.range_calls == [(100, 147)]


def test_keyframes_transport_play_without_range_support() -> None:
    controller = SimpleNamespace(is_playing=False, play=MagicMock())
    transport = _KeyframesTimelineTransport(controller, clip_start=0, clip_total=10)

    transport.play()

    controller.play.assert_called_once()


def test_keyframes_transport_seek_pauses_playing_controller() -> None:
    controller = _RecordingPlaybackController(is_playing=True)
    transport = _KeyframesTimelineTransport(controller, clip_start=100, clip_total=48)

    assert transport.is_playing() is True
    transport.seek(7)

    assert controller.pause_calls == 1
    assert controller.seek_calls == [107]
    assert transport.is_playing() is False


def test_keyframes_transport_pause_and_seek_while_paused() -> None:
    controller = _RecordingPlaybackController()
    transport = _KeyframesTimelineTransport(controller, clip_start=10, clip_total=30)

    transport.pause()
    assert controller.pause_calls == 1
    assert transport.is_playing() is False

    transport.seek(4)
    assert controller.pause_calls == 1  # no redundant pause
    assert controller.seek_calls == [14]


# ═══════════════════════════════════════════════════════════════════════
# F. Static preview retry logic
# ═══════════════════════════════════════════════════════════════════════


def _preview_context(source_frame: int = 50) -> PageContext:
    return PageContextManager().create_context_for_edit_to_color(
        clip_id="clip-p",
        asset_id="asset-p",
        timeline_id="tl-p",
        timeline_frame=100,
        source_frame=source_frame,
        clip_local_frame=0,
        color_stack_id="stack-p",
    )


def test_static_preview_bails_without_controller_and_disarms_guard() -> None:
    host = _PageHost()
    host._color_clip_switch_guard_armed = True

    host._show_color_timeline_clip_static_preview(_preview_context())

    assert host._color_clip_switch_guard_armed is False


def test_static_preview_bails_while_playing() -> None:
    host = _PageHost()
    host._color_clip_switch_guard_armed = True
    withdraw = MagicMock()
    video_controller = _preview_video_controller(
        is_playing=True, withdraw_static_preview_pending=withdraw
    )
    host.video_controller = video_controller

    host._show_color_timeline_clip_static_preview(_preview_context())

    withdraw.assert_called_once()
    assert host._color_clip_switch_guard_armed is False


def test_static_preview_bails_when_video_not_loaded() -> None:
    host = _PageHost()
    video_controller = _preview_video_controller(is_video_loaded=lambda: False)
    host.video_controller = video_controller

    host._show_color_timeline_clip_static_preview(_preview_context())

    video_controller.seek_to_frame.assert_not_called()


def test_static_preview_isolates_is_loaded_probe_failure() -> None:
    host = _PageHost()

    def _raise() -> bool:
        raise RuntimeError("probe failed")

    host.video_controller = _preview_video_controller(is_video_loaded=_raise)

    host._show_color_timeline_clip_static_preview(_preview_context())  # no raise


def test_static_preview_skips_already_rendered_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = _SingleShotRecorder()
    monkeypatch.setattr(pc, "QTimer", recorder)
    host = _PageHost()
    video_controller = _preview_video_controller()
    host.video_controller = video_controller
    sync = MagicMock()
    host._sync_color_timeline_clip_video_controls = sync  # type: ignore[method-assign]
    context = _preview_context(source_frame=50)
    host._color_static_preview_token = 1
    host._color_static_preview_rendered_key = (1, 50)

    host._show_color_timeline_clip_static_preview(context, token=1)

    video_controller.seek_to_frame.assert_not_called()
    sync.assert_called_once()
    assert recorder.calls == []


def test_static_preview_stale_token_returns_early() -> None:
    host = _PageHost()
    video_controller = _preview_video_controller()
    host.video_controller = video_controller
    host._color_static_preview_token = 7

    host._show_color_timeline_clip_static_preview(_preview_context(), token=1)

    video_controller.seek_to_frame.assert_not_called()


def test_static_preview_fast_completes_already_displayed_frame() -> None:
    host = _PageHost()
    withdraw = MagicMock()
    video_controller = _preview_video_controller(
        _last_static_preview_delivered=True,
        static_preview_shows_frame=lambda frame: frame == 50,
        withdraw_static_preview_pending=withdraw,
    )
    host.video_controller = video_controller
    context = _preview_context(source_frame=50)
    host._color_static_preview_token = 3

    host._show_color_timeline_clip_static_preview(context, token=3)

    withdraw.assert_called_once()
    video_controller.seek_to_frame.assert_not_called()
    assert host._color_static_preview_rendered_key == (3, 50)
    assert host._color_clip_switch_guard_armed is False


def test_static_preview_first_call_mints_token(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = _SingleShotRecorder()
    monkeypatch.setattr(pc, "QTimer", recorder)
    request = MagicMock(return_value=True)
    host = _PageHost()
    host.video_controller = _preview_video_controller(
        request_static_preview_frame=request
    )

    host._show_color_timeline_clip_static_preview(_preview_context(source_frame=42))

    assert host._color_static_preview_token == 1
    assert host._color_static_preview_rendered_key == (1, 42)
    request.assert_called_once_with(42)
    assert recorder.calls == []


def test_static_preview_retries_with_incremented_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = _SingleShotRecorder()
    monkeypatch.setattr(pc, "QTimer", recorder)
    results = [False, True]
    request = MagicMock(side_effect=lambda frame: results.pop(0))
    host = _PageHost()
    host.video_controller = _preview_video_controller(
        request_static_preview_frame=request
    )
    context = _preview_context(source_frame=50)

    host._show_color_timeline_clip_static_preview(context)
    assert request.call_count == 1
    assert len(recorder.calls) == 1
    delay_ms, retry = recorder.calls[0]
    assert delay_ms == 50

    retry()  # simulate the timer firing

    assert request.call_count == 2
    assert host._color_static_preview_rendered_key == (1, 50)


def test_static_preview_exhausted_retries_fall_back_to_exact_seek(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = _SingleShotRecorder()
    monkeypatch.setattr(pc, "QTimer", recorder)
    host = _PageHost()
    video_controller = _preview_video_controller(
        request_static_preview_frame=MagicMock(return_value=False)
    )
    host.video_controller = video_controller
    host._color_static_preview_token = 1

    host._show_color_timeline_clip_static_preview(
        _preview_context(source_frame=77), retry_count=8, token=1
    )

    video_controller.seek_to_frame.assert_called_once_with(77, force_display=True)
    # AUD-3101 (fixed): the controller implements the exact-seek interface,
    # so after the single forced exact seek no further retry is scheduled —
    # the old code re-scheduled itself with the same retry_count forever
    # (unbounded 100ms seek storm).
    assert recorder.calls == []


def test_static_preview_exhausted_retries_legacy_controller_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AUD-3101 (fixed): legacy controllers (no seek_to_frame) retry with an
    incremented count and stop at the hard cap instead of looping forever."""
    recorder = _SingleShotRecorder()
    monkeypatch.setattr(pc, "QTimer", recorder)
    host = _PageHost()
    video_controller = _preview_video_controller(
        request_static_preview_frame=MagicMock(return_value=False),
        seek_to_frame=None,
    )
    host.video_controller = video_controller
    host._color_static_preview_token = 1
    host._color_clip_switch_guard_armed = True

    host._show_color_timeline_clip_static_preview(
        _preview_context(source_frame=77), retry_count=8, token=1
    )

    # One 100ms retry scheduled with the incremented count.
    assert len(recorder.calls) == 1
    assert recorder.calls[0][0] == 100
    assert host._color_clip_switch_guard_armed is True  # not disarmed yet

    # Drain retries to the hard cap: no further scheduling beyond
    # _COLOR_STATIC_PREVIEW_MAX_RETRIES, then the guard is disarmed.
    while recorder.calls:
        _, retry = recorder.calls.pop(0)
        retry()

    assert host._color_clip_switch_guard_armed is False
    assert host._color_static_preview_token == 1  # same request throughout


def test_static_preview_exact_seek_falls_back_without_kwarg_support(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = _SingleShotRecorder()
    monkeypatch.setattr(pc, "QTimer", recorder)
    host = _PageHost()
    calls: List[int] = []

    def _strict_seek(frame: int) -> None:
        calls.append(int(frame))

    host.video_controller = _preview_video_controller(
        request_static_preview_frame=MagicMock(return_value=False),
        seek_to_frame=_strict_seek,
    )
    host._color_static_preview_token = 1

    host._show_color_timeline_clip_static_preview(
        _preview_context(source_frame=33), retry_count=8, token=1
    )

    # force_display kwarg raised TypeError → retried positional-only call.
    assert calls == [33]


def test_static_preview_renders_via_legacy_seek_without_preview_api() -> None:
    host = _PageHost()
    video_controller = _preview_video_controller()
    host.video_controller = video_controller

    host._show_color_timeline_clip_static_preview(_preview_context(source_frame=9))

    video_controller.seek_to_frame.assert_called_once_with(9)
    assert host._color_static_preview_rendered_key == (1, 9)


def test_static_preview_dual_path_without_api_stays_pending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = _SingleShotRecorder()
    monkeypatch.setattr(pc, "QTimer", recorder)
    host = _PageHost()
    video_controller = _preview_video_controller(
        _use_dual_path=True, _coordinator=object()
    )
    host.video_controller = video_controller

    host._show_color_timeline_clip_static_preview(_preview_context(source_frame=9))

    # Dual path without the preview API must not hard-seek; a retry is queued.
    video_controller.seek_to_frame.assert_not_called()
    assert len(recorder.calls) == 1


def test_immediate_thumbnail_preview_falls_back_to_controller_seek() -> None:
    host = _PageHost()
    host._color_static_preview_token = 0
    host._request_color_clip_thumbnail_static_preview = MagicMock(  # type: ignore[method-assign]
        return_value=False
    )
    video_controller = _preview_video_controller()
    host.video_controller = video_controller

    rendered = host._show_color_timeline_clip_immediate_thumbnail_preview(
        _preview_context(source_frame=61), source_frame=61
    )

    assert rendered is True
    video_controller.seek_to_frame.assert_called_once_with(61)


# ═══════════════════════════════════════════════════════════════════════
# G. Composite playhead follow (track layering)
# ═══════════════════════════════════════════════════════════════════════


def test_find_clip_under_playhead_prefers_highest_video_track() -> None:
    lower = _FakeTrack([_clip("clip-low")], index=0)
    upper = _FakeTrack([_clip("clip-high")], index=1)
    timeline = _fake_timeline([lower, upper], video_tracks=[lower, upper])
    host = _PageHost()

    clip = host._find_clip_under_composite_playhead(timeline, 10)

    assert clip is not None
    assert clip.clip_id == "clip-high"


def test_find_clip_under_playhead_treats_disabled_clip_as_gap() -> None:
    disabled = _FakeTrack([_clip("clip-off", is_enabled=False)], index=1)
    enabled = _FakeTrack([_clip("clip-on")], index=0)
    timeline = _fake_timeline(
        [enabled, disabled], video_tracks=[disabled, enabled]
    )
    host = _PageHost()

    clip = host._find_clip_under_composite_playhead(timeline, 10)

    assert clip is not None
    assert clip.clip_id == "clip-on"


def test_find_clip_under_playhead_returns_none_for_gap() -> None:
    track = _FakeTrack([_clip("clip-a", timeline_start=0, duration=10)])
    timeline = _fake_timeline([track])
    host = _PageHost()

    assert host._find_clip_under_composite_playhead(timeline, 99) is None


def _playhead_host(
    *,
    transport_playing: bool = False,
    scrubbing: bool = False,
) -> Tuple[_PageHost, PageContextManager]:
    host = _PageHost(current_page="Composite")
    host.composite_page = _make_composite_page()
    controller = _RecordingPlaybackController(
        is_playing=transport_playing, current_frame=0
    )
    host.timeline_panel = _make_timeline_panel(
        controller, playhead=0, scrubbing=scrubbing
    )
    manager = PageContextManager()
    host._page_context_manager = manager
    return host, manager


def test_playhead_follow_guards_page_and_page_object() -> None:
    host, _manager = _playhead_host()

    host.current_page = "Color"
    host._on_composite_playback_frame_changed(10)
    assert host.timeline_clip_context_calls == []

    host.current_page = "Composite"
    host.composite_page = None
    host._on_composite_playback_frame_changed(10)
    assert host.timeline_clip_context_calls == []


def test_playhead_follow_ignores_frame_matching_runtime_context() -> None:
    host, manager = _playhead_host()
    host.composite_page._runtime_context = SimpleNamespace(timeline_frame=25)
    context = manager.create_context_for_edit_to_composite(
        clip_id="clip-a", composite_id="comp-a", asset_id="asset-1"
    )
    context.timeline_id = "tl-x"
    manager.set_current_context(context)
    track = _FakeTrack([_clip("clip-a")])
    host._current_timeline_sequences = {
        "tl-x": _fake_timeline([track], timeline_id="tl-x")
    }

    host._on_composite_playback_frame_changed(25)

    assert host.timeline_clip_context_calls == []


def test_playhead_follow_rebinds_in_range_clip_with_context_fallback_ids() -> None:
    host, manager = _playhead_host()
    context = manager.create_context_for_edit_to_composite(
        clip_id="clip-a",
        composite_id="comp-context",
        asset_id="asset-context",
    )
    context.timeline_id = "tl-x"
    manager.set_current_context(context)
    clip = _clip("clip-a", color_stack_id=None, composite_id=None, asset_id="asset-1")
    track = _FakeTrack([clip])
    host._current_timeline_sequences = {
        "tl-x": _fake_timeline([track], timeline_id="tl-x")
    }

    host._on_composite_playback_frame_changed(10)

    assert len(host.timeline_clip_context_calls) == 1
    rebound, composite_id, clip_id = host.timeline_clip_context_calls[0]
    # Clip lacks ids → falls back to the departing context's ids.
    assert composite_id == "comp-context"
    assert clip_id == "clip-a"
    assert rebound.source_frame == 10  # source_start 0 + local 10
    assert host._page_context_manager.get_current_context() is rebound


def test_playhead_follow_clamps_to_clip_range_while_transport_playing() -> None:
    host, manager = _playhead_host(transport_playing=True)
    context = manager.create_context_for_edit_to_composite(
        clip_id="clip-a", composite_id="comp-a"
    )
    context.timeline_id = "tl-x"
    manager.set_current_context(context)
    clip = _clip("clip-a", timeline_start=100, duration=40, source_start=1000)
    track = _FakeTrack([clip])
    host._current_timeline_sequences = {
        "tl-x": _fake_timeline([track], timeline_id="tl-x")
    }

    host._on_composite_playback_frame_changed(150)  # one past clip end

    controller = host.timeline_panel.get_playback_controller()
    assert isinstance(controller, _RecordingPlaybackController)
    assert controller.pause_calls == 1
    assert controller.seek_calls == [139]  # clamped to last frame
    assert host.timeline_clip_context_calls == []


def test_playhead_follow_enters_neighbour_clip_when_scrubbing() -> None:
    host, manager = _playhead_host(transport_playing=True, scrubbing=True)
    context = manager.create_context_for_edit_to_composite(
        clip_id="clip-a",
        composite_id="comp-a",
        color_stack_id="stack-a",
        asset_id="asset-a",
    )
    context.timeline_id = "tl-x"
    manager.set_current_context(context)
    clip_a = _clip(
        "clip-a",
        timeline_start=0,
        duration=40,
        color_stack_id="stack-a",
        composite_id="comp-a",
        asset_id="asset-a",
    )
    clip_b = _clip(
        "clip-b",
        timeline_start=40,
        duration=40,
        source_start=500,
        color_stack_id="stack-b",
        composite_id="comp-b",
        asset_id="asset-b",
    )
    track = _FakeTrack([clip_a, clip_b])
    host._current_timeline_sequences = {
        "tl-x": _fake_timeline([track], timeline_id="tl-x")
    }

    host._on_composite_playback_frame_changed(50)

    assert len(host.timeline_clip_context_calls) == 1
    rebound, composite_id, clip_id = host.timeline_clip_context_calls[0]
    # Followed clip must NOT inherit the departed clip's stack/composite ids.
    assert clip_id == "clip-b"
    assert composite_id == "comp-b"
    assert rebound.color_stack_id == "stack-b"
    assert rebound.source_frame == 510
    assert rebound.clip_local_frame == 10


def test_playhead_follow_keeps_last_frame_on_gap() -> None:
    host, manager = _playhead_host()
    context = manager.create_context_for_edit_to_composite(
        clip_id="clip-a", composite_id="comp-a"
    )
    context.timeline_id = "tl-x"
    manager.set_current_context(context)
    track = _FakeTrack([_clip("clip-a", timeline_start=0, duration=10)])
    host._current_timeline_sequences = {
        "tl-x": _fake_timeline([track], timeline_id="tl-x")
    }

    host._on_composite_playback_frame_changed(50)  # gap after the clip

    assert host.timeline_clip_context_calls == []
    assert host._page_context_manager.get_current_context() is context


def test_transport_playing_and_scrubbing_probes() -> None:
    host = _PageHost()
    controller = _RecordingPlaybackController(is_playing=True)
    host.timeline_panel = _make_timeline_panel(controller, scrubbing=True)

    assert host._composite_playback_is_transport_playing() is True
    assert host._composite_playhead_is_scrubbing() is True

    host.timeline_panel.get_playback_controller.side_effect = TypeError("gone")
    assert host._composite_playback_is_transport_playing() is False

    host.timeline_panel = None
    assert host._composite_playback_is_transport_playing() is False
    assert host._composite_playhead_is_scrubbing() is False


def test_clamp_composite_playback_seeks_boundary_and_isolates_errors() -> None:
    host = _PageHost()
    controller = _RecordingPlaybackController(is_playing=True, current_frame=0)
    host.timeline_panel = _make_timeline_panel(controller)
    clip = _clip("clip-a", timeline_start=100, duration=40)

    host._clamp_composite_playback_to_clip_range(150, clip)
    assert controller.pause_calls == 1
    assert controller.seek_calls == [139]

    # Before the clip start clamps to the start frame.
    host._clamp_composite_playback_to_clip_range(10, clip)
    assert controller.seek_calls == [139, 100]

    # Failures inside the controller are logged, not raised.
    controller.current_frame = -1
    controller.seek = MagicMock(side_effect=RuntimeError("seek failed"))  # type: ignore[method-assign]
    host._clamp_composite_playback_to_clip_range(10, clip)

    # No controller at all: no-op.
    host.timeline_panel = _make_timeline_panel(None)
    host._clamp_composite_playback_to_clip_range(10, clip)


# ═══════════════════════════════════════════════════════════════════════
# H. Thumbnail preview degradation (offscreen Qt)
# ═══════════════════════════════════════════════════════════════════════


def _solid_pixmap(red: int, green: int, blue: int) -> QPixmap:
    image = QImage(2, 2, QImage.Format.Format_RGB888)
    image.fill(int((red << 16) | (green << 8) | blue))
    return QPixmap.fromImage(image)


def test_pixmap_to_bgr_array_swaps_channels(qapp: Any) -> None:
    pixmap = _solid_pixmap(200, 100, 10)

    frame = PageManagementMixin._pixmap_to_bgr_array(pixmap)

    assert frame is not None
    assert frame.shape == (2, 2, 3)
    # Pure-red pixel must come back as BGR.
    assert frame[0, 0].tolist() == [10, 100, 200]


def test_pixmap_to_bgr_array_returns_none_for_empty_pixmap(qapp: Any) -> None:
    empty = QPixmap()
    assert PageManagementMixin._pixmap_to_bgr_array(empty) is None


def test_submit_thumbnail_prefers_controller_submit(qapp: Any) -> None:
    host = _PageHost()
    submit = MagicMock(return_value=True)
    host.video_controller = SimpleNamespace(submit_static_preview_array=submit)
    engine = SimpleNamespace(set_image=MagicMock())
    host.engine = engine
    host._color_static_preview_token = 4

    rendered = host._submit_color_clip_thumbnail_pixmap(
        _solid_pixmap(1, 2, 3), source_frame=12, token=4
    )

    assert rendered is True
    submit.assert_called_once()
    assert host._color_static_preview_rendered_key == (4, 12)
    engine.set_image.assert_not_called()


def test_submit_thumbnail_falls_back_to_engine_and_canvas(qapp: Any) -> None:
    host = _PageHost()
    host.video_controller = SimpleNamespace()  # no submit API
    engine = SimpleNamespace(set_image=MagicMock())
    host.engine = engine
    viewport = MagicMock()
    host.canvas = SimpleNamespace(viewport=lambda: viewport)
    host.current_file_path = "/tmp/x.mov"
    host._color_static_preview_token = 2

    rendered = host._submit_color_clip_thumbnail_pixmap(
        _solid_pixmap(5, 6, 7), source_frame=3, token=2
    )

    assert rendered is True
    engine.set_image.assert_called_once()
    args, kwargs = engine.set_image.call_args
    assert args[0].shape == (2, 2, 3)
    assert kwargs["is_new_file"] is False
    assert kwargs["is_video"] is True
    assert kwargs["video_path"] == "/tmp/x.mov"
    viewport.update.assert_called_once()
    assert host._color_static_preview_rendered_key == (2, 3)


def test_submit_thumbnail_rejects_stale_token_and_none_pixmap(qapp: Any) -> None:
    host = _PageHost()
    host.video_controller = SimpleNamespace()
    host.engine = SimpleNamespace(set_image=MagicMock())
    host._color_static_preview_token = 9

    assert (
        host._submit_color_clip_thumbnail_pixmap(
            _solid_pixmap(0, 0, 0), source_frame=0, token=1
        )
        is False
    )
    assert (
        host._submit_color_clip_thumbnail_pixmap(None, source_frame=0, token=9)
        is False
    )
    host.engine.set_image.assert_not_called()


def test_request_thumbnail_preview_early_return_guards() -> None:
    host = _PageHost()
    host._current_edit_project = SimpleNamespace(assets={})
    context = _preview_context()
    context.asset_id = None
    assert (
        host._request_color_clip_thumbnail_static_preview(
            context, source_frame=5, token=1
        )
        is False
    )

    host._current_edit_project = None
    context.asset_id = "asset-p"
    assert (
        host._request_color_clip_thumbnail_static_preview(
            context, source_frame=5, token=1
        )
        is False
    )

    # Asset present but without a path.
    host._current_edit_project = SimpleNamespace(
        assets={"asset-p": SimpleNamespace()}
    )
    assert (
        host._request_color_clip_thumbnail_static_preview(
            context, source_frame=5, token=1
        )
        is False
    )


def test_request_thumbnail_preview_survives_provider_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _raise() -> Any:
        raise RuntimeError("provider down")

    monkeypatch.setattr(
        "src.shared.media.thumbnail_provider.ThumbnailProvider.instance",
        _raise,
    )
    host = _PageHost()
    host._current_edit_project = SimpleNamespace(
        assets={"asset-p": SimpleNamespace(path="/tmp/x.mov")}
    )
    context = _preview_context()
    host._color_static_preview_token = 1

    rendered = host._request_color_clip_thumbnail_static_preview(
        context, source_frame=5, token=1
    )

    assert rendered is False


def test_request_thumbnail_disconnects_previous_subscription(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Provider:
        def __init__(self) -> None:
            self.frame_thumbnail_ready = _FakeSignal()
            self.async_calls: List[Tuple[Any, ...]] = []

        def get_cached_frame_pixmap(self, *_args: Any, **_kwargs: Any) -> None:
            return None

        def request_frame_pixmap_async(self, *args: Any, **_kwargs: Any) -> None:
            self.async_calls.append(args)

    provider = _Provider()
    monkeypatch.setattr(
        "src.shared.media.thumbnail_provider.ThumbnailProvider.instance",
        lambda: provider,
    )
    host = _PageHost()
    host._current_edit_project = SimpleNamespace(
        assets={
            "asset-p": SimpleNamespace(
                path="/tmp/x.mov", metadata={"total_frames": 200}
            )
        }
    )
    context = _preview_context()
    host._color_static_preview_token = 1

    def _stale_slot(*_args: Any) -> None:
        return None

    host._color_static_preview_thumb_subscription = (provider, _stale_slot)
    provider.frame_thumbnail_ready.connect(_stale_slot)

    host._request_color_clip_thumbnail_static_preview(
        context, source_frame=5, token=1
    )

    # The stale one-shot listener was disconnected before subscribing anew.
    assert _stale_slot not in provider.frame_thumbnail_ready.slots
    assert len(provider.frame_thumbnail_ready.slots) == 1
    assert len(provider.async_calls) == 1


def test_color_clip_preview_total_frames_sources() -> None:
    host = _PageHost()
    context = _preview_context()
    # Metadata total_frames wins.
    host._current_edit_project = SimpleNamespace(
        assets={"asset-p": SimpleNamespace(metadata={"total_frames": 120})}
    )
    assert host._color_clip_preview_total_frames(context, 5) == 120

    # No metadata → clip duration after the requested frame.
    host._current_edit_project = SimpleNamespace(assets={})
    host._color_timeline_clip_playback_context = {
        "clip": SimpleNamespace(source_duration=40)
    }
    assert host._color_clip_preview_total_frames(context, 5) == 45
    host._color_timeline_clip_playback_context = None
    assert host._color_clip_preview_total_frames(context, 5) == 6


# ═══════════════════════════════════════════════════════════════════════
# I. Composite workspace restore / auto-bind guards
# ═══════════════════════════════════════════════════════════════════════


def test_restore_workspace_early_return_guards() -> None:
    host = _PageHost()
    host.composite_page = _make_composite_page()
    host._current_edit_project = SimpleNamespace(
        get_composition_by_id=MagicMock(return_value={"nodes": []}),
        active_comp_clip_id=None,
    )

    # Suppression flag from an explicit graph open.
    host._suppress_composite_auto_bind_once = True
    assert host._restore_current_composite_workspace() is False
    host._suppress_composite_auto_bind_once = False

    # Contextual switch in progress.
    host._contextual_page_switch_in_progress = True
    assert host._restore_current_composite_workspace() is False
    host._contextual_page_switch_in_progress = False

    # Missing page / project.
    page = host.composite_page
    host.composite_page = None
    assert host._restore_current_composite_workspace() is False
    host.composite_page = page
    project = host._current_edit_project
    host._current_edit_project = None
    assert host._restore_current_composite_workspace() is False
    host._current_edit_project = project

    # No remembered identity anywhere.
    host._current_composite_id = ""
    host.composite_page._current_composite_id = ""
    assert host._restore_current_composite_workspace() is False

    # Standalone images restore via the Color-session path instead.
    host._current_composite_id = "standalone_composite"
    assert host._restore_current_composite_workspace() is False


def test_restore_workspace_uses_legacy_direct_load_path() -> None:
    host = _PageHost()
    page = _make_composite_page()
    host.composite_page = page
    composition = {"nodes": [], "edges": []}
    project = SimpleNamespace(
        get_composition_by_id=MagicMock(return_value=composition),
        active_comp_clip_id=None,
    )
    host._current_edit_project = project
    host._current_composite_id = "graph-1"

    assert host._restore_current_composite_workspace() is True

    page.load_composite_data.assert_called_once_with(composition)
    page.set_timeline_clip_context.assert_called_once_with(None, None)
    page.set_composition_active.assert_called_once_with(True)
    assert host._current_composite_id == "graph-1"


def test_restore_workspace_rebuilds_runtime_context_when_available() -> None:
    host = _PageHost()
    page = _make_composite_page()
    host.composite_page = page
    project = SimpleNamespace(
        get_composition_by_id=MagicMock(return_value={"nodes": []}),
        active_comp_clip_id=None,
    )
    host._current_edit_project = project
    host._current_composite_id = "graph-2"
    context = PageContext(target_page=PageType.COMPOSITE, clip_id="c")
    host._context_for_composite_data = MagicMock(return_value=context)  # type: ignore[method-assign]

    assert host._restore_current_composite_workspace() is True

    host._context_for_composite_data.assert_called_once_with("graph-2", {"nodes": []})
    assert host.composite_context_log == [context]
    assert host._page_context_manager.get_current_context() is context


def test_restore_workspace_resolves_remembered_comp_clip() -> None:
    host = _PageHost()
    page = _make_composite_page()
    host.composite_page = page
    project = SimpleNamespace(
        get_composition_by_id=MagicMock(return_value={"nodes": []}),
        active_comp_clip_id="cc-1",
        comp_clips={"cc-1": {"graph_id": "graph-9"}},
    )
    host._current_edit_project = project
    host._current_composite_id = ""
    page._current_composite_id = ""

    assert host._restore_current_composite_workspace() is True

    project.get_composition_by_id.assert_called_once_with("graph-9")
    assert host._current_composite_id == "graph-9"


def test_restore_workspace_isolates_lookup_and_load_failures() -> None:
    host = _PageHost()
    page = _make_composite_page()
    host.composite_page = page
    host._current_composite_id = "graph-x"

    project = SimpleNamespace(
        get_composition_by_id=MagicMock(side_effect=TypeError("bad id")),
        active_comp_clip_id=None,
    )
    host._current_edit_project = project
    assert host._restore_current_composite_workspace() is False

    project.get_composition_by_id = MagicMock(return_value=None)
    assert host._restore_current_composite_workspace() is False

    composition = {"nodes": []}
    project.get_composition_by_id = MagicMock(return_value=composition)
    page.load_composite_data.side_effect = ValueError("corrupt graph")
    assert host._restore_current_composite_workspace() is False


def test_resolve_remembered_comp_clip_variants() -> None:
    host = _PageHost()
    host._current_edit_project = None
    assert host._resolve_remembered_comp_clip_composite_id(None) == ""

    project = SimpleNamespace(active_comp_clip_id=None, comp_clips={})
    assert host._resolve_remembered_comp_clip_composite_id(project) == ""

    project = SimpleNamespace(
        active_comp_clip_id="cc-2",
        comp_clips={"cc-2": SimpleNamespace(graph_id="graph-obj")},
    )
    assert host._resolve_remembered_comp_clip_composite_id(project) == "graph-obj"

    project = SimpleNamespace(
        active_comp_clip_id="cc-3", comp_clips={"cc-3": {"graph_id": None}}
    )
    assert host._resolve_remembered_comp_clip_composite_id(project) == ""


def test_prepare_empty_composite_workspace_resets_state() -> None:
    host = _PageHost()
    page = _make_composite_page()
    host.composite_page = page
    host._current_composite_id = "graph-old"
    host._suppress_composite_auto_bind_once = True

    host._prepare_empty_composite_workspace()

    page.reset_to_empty_workspace.assert_called_once()
    assert host._current_composite_id == ""
    assert host._suppress_composite_auto_bind_once is False


def test_prepare_empty_composite_workspace_fallback_without_reset_api() -> None:
    host = _PageHost()
    page = MagicMock(spec=["_current_composite_id", "set_timeline_clip_context"])
    host.composite_page = page

    host._prepare_empty_composite_workspace()

    assert page._current_composite_id == ""
    page.set_timeline_clip_context.assert_called_once_with(None, None)


def test_try_standalone_image_composite_guards() -> None:
    host = _PageHost()
    host.composite_page = _make_composite_page()

    # No current file.
    assert host._try_standalone_image_composite() is False

    # Video sources must not use the standalone path.
    host.current_file_path = "/tmp/video.mov"
    host.current_file_is_video = True
    assert host._try_standalone_image_composite() is False
    host.current_file_is_video = False

    # No composite page.
    page = host.composite_page
    host.composite_page = None
    assert host._try_standalone_image_composite() is False
    host.composite_page = page

    # A timeline-clip Color session is not a standalone image.
    host._color_session_manager = SimpleNamespace(
        get_current_session=lambda: SimpleNamespace(
            is_standalone_image=lambda: False
        )
    )
    assert host._try_standalone_image_composite() is False


def test_try_standalone_image_reuses_graph_for_same_image() -> None:
    host = _PageHost()
    page = _make_composite_page()
    host.composite_page = page
    host.current_file_path = "/tmp/pic.png"
    host._composite_graph_image_path = "/tmp/pic.png"

    assert host._try_standalone_image_composite() is True
    page.switch_to_color_output_graph.assert_not_called()
    assert host._current_composite_id == "standalone_composite"
    assert page._current_composite_id == "standalone_composite"
    page.set_composition_active.assert_called_once_with(True)


def test_try_standalone_image_switches_graph_for_new_image() -> None:
    host = _PageHost()
    page = _make_composite_page()
    host.composite_page = page
    host.current_file_path = "/tmp/pic-b.png"
    host._composite_graph_image_path = "/tmp/pic-a.png"

    assert host._try_standalone_image_composite() is True
    page.switch_to_color_output_graph.assert_called_once()
    assert host._composite_graph_image_path == "/tmp/pic-b.png"


def test_auto_bind_to_color_session_guards() -> None:
    host = _PageHost()

    host.composite_page = None
    assert host._auto_bind_composite_to_color_session() is False

    host.composite_page = _make_composite_page()
    host._color_session_manager = SimpleNamespace(get_current_session=lambda: None)
    assert host._auto_bind_composite_to_color_session() is False

    host._color_session_manager = SimpleNamespace(
        get_current_session=lambda: SimpleNamespace(
            is_timeline_clip=lambda: False
        )
    )
    assert host._auto_bind_composite_to_color_session() is False

    session = SimpleNamespace(
        is_timeline_clip=lambda: True, clip_id=None, timeline_id=None
    )
    host._color_session_manager = SimpleNamespace(get_current_session=lambda: session)
    assert host._auto_bind_composite_to_color_session() is False


def test_auto_bind_to_color_session_binds_clip() -> None:
    host = _PageHost()
    host.composite_page = _make_composite_page()
    clip = _clip(
        "clip-c",
        timeline_start=100,
        duration=40,
        source_start=500,
        color_stack_id="stack-c",
        composite_id="comp-c",
    )
    track = _FakeTrack([clip])
    host._current_timeline_sequences = {"tl-1": _fake_timeline([track])}
    host.timeline_panel = _make_timeline_panel(None)
    session = SimpleNamespace(
        is_timeline_clip=lambda: True,
        clip_id="clip-c",
        timeline_id="tl-1",
        timeline_frame=None,
        source_frame=None,
        color_stack_id=None,
        asset_id=None,
    )
    host._color_session_manager = SimpleNamespace(get_current_session=lambda: session)

    assert host._auto_bind_composite_to_color_session() is True

    context = host.composite_context_log[0]
    assert context.clip_id == "clip-c"
    assert context.timeline_frame == 100  # session frame None → clip start
    assert context.source_frame == 500
    assert context.color_stack_id == "stack-c"
    assert host._page_context_manager.get_current_context() is context


def test_auto_bind_to_selected_clip_guards() -> None:
    host = _PageHost()
    host.composite_page = _make_composite_page()

    host._suppress_composite_auto_bind_once = True
    assert host._auto_bind_composite_to_selected_clip() is False
    # The suppression flag is consumed by the attempt itself.
    assert host._suppress_composite_auto_bind_once is False

    host._contextual_page_switch_in_progress = True
    assert host._auto_bind_composite_to_selected_clip() is False
    host._contextual_page_switch_in_progress = False

    host._edit_available = False
    assert host._auto_bind_composite_to_selected_clip() is False
    host._edit_available = True

    host.timeline_panel = _make_timeline_panel(None)
    assert host._auto_bind_composite_to_selected_clip() is False

    page = host.composite_page
    host.composite_page = None
    assert host._auto_bind_composite_to_selected_clip() is False
    host.composite_page = page

    host.edit_page = SimpleNamespace(_current_timeline=None)
    assert host._auto_bind_composite_to_selected_clip() is False


def test_auto_bind_to_selected_clip_binds_selected_clip() -> None:
    host = _PageHost()
    host.composite_page = _make_composite_page()
    clip = _clip(
        "clip-sel",
        timeline_start=100,
        duration=40,
        source_start=500,
        color_stack_id="stack-sel",
        composite_id="comp-sel",
    )
    track = _FakeTrack([clip])
    timeline = _fake_timeline([track])
    host.edit_page = SimpleNamespace(_current_timeline=timeline)
    host.timeline_panel = _make_timeline_panel(
        None, playhead=110, selected=["clip-sel"]
    )

    assert host._auto_bind_composite_to_selected_clip() is True

    context = host.composite_context_log[0]
    assert context.clip_id == "clip-sel"
    assert context.timeline_frame == 110
    assert context.source_frame == 510
    assert context.composite_id == "comp-sel"


def test_auto_bind_to_selected_clip_requires_composite_id() -> None:
    host = _PageHost()
    host.composite_page = _make_composite_page()
    clip = _clip("clip-plain", timeline_start=0, duration=40)  # no composite
    timeline = _fake_timeline([_FakeTrack([clip])])
    host.edit_page = SimpleNamespace(_current_timeline=timeline)
    host.timeline_panel = _make_timeline_panel(
        None, playhead=5, selected=["clip-plain"]
    )
    host._current_edit_project = None

    assert host._auto_bind_composite_to_selected_clip() is False
    assert host.composite_context_log == []


def test_ensure_temporary_composition_stub_never_creates() -> None:
    host = _PageHost()
    clip = _clip("clip-x", composite_id="comp-existing")
    assert host._ensure_temporary_composition_for_clip(None, None, clip) == "comp-existing"
    assert (
        host._ensure_temporary_composition_for_clip(None, None, _clip("clip-y"))
        is None
    )


def test_resolve_composite_id_for_clip_variants() -> None:
    host = _PageHost()
    host._current_edit_project = None
    assert (
        host._resolve_composite_id_for_clip(_clip("c1", composite_id="comp-1"))
        == "comp-1"
    )
    assert host._resolve_composite_id_for_clip(_clip("c2")) is None

    project = SimpleNamespace(
        get_asset_by_id=MagicMock(
            return_value=SimpleNamespace(
                is_comp_clip=True, metadata={"graph_id": "graph-asset"}
            )
        )
    )
    host._current_edit_project = project
    assert host._resolve_composite_id_for_clip(_clip("c3")) == "graph-asset"

    project.get_asset_by_id = MagicMock(side_effect=TypeError("nope"))
    assert host._resolve_composite_id_for_clip(_clip("c4")) is None


def test_should_block_implicit_composite_entry_paths() -> None:
    host = _PageHost()

    # No timeline state on Edit → not blocked.
    host.edit_page = None
    host.timeline_panel = None
    assert host._should_block_implicit_composite_entry("Edit") is False

    # Selected normal clip without composite → blocked.
    clip = _clip("clip-n")
    timeline = _fake_timeline([_FakeTrack([clip])])
    host.edit_page = SimpleNamespace(_current_timeline=timeline)
    host.timeline_panel = _make_timeline_panel(None, selected=["clip-n"])
    assert host._should_block_implicit_composite_entry("Edit") is True

    # Clip already bound → not blocked.
    bound = _clip("clip-b", composite_id="comp-b")
    host.edit_page = SimpleNamespace(
        _current_timeline=_fake_timeline([_FakeTrack([bound])])
    )
    host.timeline_panel = _make_timeline_panel(None, selected=["clip-b"])
    assert host._should_block_implicit_composite_entry("Edit") is False

    # Color page without a timeline-clip session → not blocked.
    host._color_session_manager = SimpleNamespace(get_current_session=lambda: None)
    assert host._should_block_implicit_composite_entry("Color") is False

    # Unknown source page → not blocked.
    assert host._should_block_implicit_composite_entry("Library") is False


# ═══════════════════════════════════════════════════════════════════════
# J. Context entry handlers & context switching
# ═══════════════════════════════════════════════════════════════════════


def test_handle_enter_deliver_with_context_sources() -> None:
    host = _PageHost()
    export_panel = MagicMock()
    host.export_panel = export_panel
    manager = PageContextManager()

    timeline_context = manager.create_context_for_to_deliver(
        source="timeline", timeline_id="tl-9"
    )
    host._handle_enter_deliver_with_context(timeline_context)
    export_panel.set_export_source.assert_called_with("timeline")
    export_panel.set_timeline_id.assert_called_once_with("tl-9")

    composite_context = manager.create_context_for_to_deliver(source="composite")
    host._handle_enter_deliver_with_context(composite_context)
    export_panel.set_export_source.assert_called_with("composite")

    color_context = manager.create_context_for_to_deliver(source="color")
    host.engine = SimpleNamespace(
        img_full_original=np.zeros((9, 16, 3), dtype=np.uint8)
    )
    host.current_file_path = "/tmp/grade.exr"
    host._handle_enter_deliver_with_context(color_context)
    export_panel.set_original_size.assert_called_once_with(16, 9)
    export_panel.set_default_path.assert_called_once_with("/tmp/grade.exr")


def test_handle_enter_edit_with_context_updates_property_status() -> None:
    host = _PageHost()
    properties = MagicMock()
    host.edit_properties_panel = properties
    context = PageContext(
        source_page=PageType.COLOR,
        target_page=PageType.EDIT,
        clip_id="clip-e",
        color_stack_id="stack-e",
        composite_id="comp-e",
    )

    host._handle_enter_edit_with_context(context)

    properties.update_color_status.assert_called_once_with("stack-e")
    properties.update_composite_status.assert_called_once_with("comp-e")


def test_handle_enter_edit_without_clip_returns_early() -> None:
    host = _PageHost()
    properties = MagicMock()
    host.edit_properties_panel = properties

    host._handle_enter_edit_with_context(
        PageContext(target_page=PageType.EDIT, clip_id=None)
    )

    properties.update_color_status.assert_not_called()
    properties.update_composite_status.assert_not_called()


def test_switch_to_page_with_context_applies_and_resets_guard() -> None:
    host = _PageHost(current_page="Color")
    seen_flags: List[bool] = []
    switched: List[str] = []

    def _record_switch(page_name: str) -> None:
        seen_flags.append(host._contextual_page_switch_in_progress)
        switched.append(page_name)

    host.switch_to_page = _record_switch  # type: ignore[method-assign]
    context = PageContext(
        source_page=PageType.COLOR, target_page=PageType.EDIT, clip_id="c"
    )
    handler = MagicMock()
    host._handle_enter_edit_with_context = handler  # type: ignore[method-assign]

    host.switch_to_page_with_context(context)

    assert switched == ["Edit"]
    assert seen_flags == [True]  # guard covers the inner plain switch
    assert host._contextual_page_switch_in_progress is False
    handler.assert_called_once_with(context)
    assert host._page_context_manager.get_current_context() is context


def test_switch_to_deliver_creates_context_and_switches() -> None:
    host = _PageHost()
    switched: List[Any] = []
    host.switch_to_page_with_context = switched.append  # type: ignore[method-assign]

    host.switch_to_deliver(source="timeline", timeline_id="tl-d")

    assert len(switched) == 1
    context = switched[0]
    assert context.target_page is PageType.DELIVER
    assert context.deliver_source == "timeline"
    assert context.timeline_id == "tl-d"


def test_get_page_context_manager_returns_manager() -> None:
    host = _PageHost()
    assert host.get_page_context_manager() is host._page_context_manager


# ═══════════════════════════════════════════════════════════════════════
# K. Edit→Color helpers
# ═══════════════════════════════════════════════════════════════════════


def test_switch_from_edit_to_color_reports_missing_selection() -> None:
    from src.shared.i18n.i18n import I18N

    host = _PageHost()
    host.timeline_panel = _make_timeline_panel(None, selected=[])

    host.switch_from_edit_to_color()

    expected = I18N.tr("edit.no_clip_selected", "No clip selected")
    assert host.status_messages == [(expected, 3000)]


def test_switch_from_edit_to_color_ignores_unknown_clip() -> None:
    host = _PageHost()
    host.timeline_panel = _make_timeline_panel(None, selected=["clip-ghost"])
    host._current_edit_project = SimpleNamespace(
        active_timeline_id="tl-1", project_id="p1"
    )
    host._current_timeline_sequences = {"tl-1": _fake_timeline([_FakeTrack([])])}
    delegate = MagicMock()
    host.open_timeline_clip_in_color = delegate  # type: ignore[method-assign]

    host.switch_from_edit_to_color("clip-ghost")

    delegate.assert_not_called()


def test_switch_from_edit_to_color_requires_active_timeline() -> None:
    host = _PageHost()
    host.timeline_panel = _make_timeline_panel(None, selected=["clip-1"])
    clip = _clip("clip-1")
    host._current_edit_project = SimpleNamespace(
        active_timeline_id=None, project_id="p1"
    )
    host._current_timeline_sequences = {"tl-1": _fake_timeline([_FakeTrack([clip])])}
    delegate = MagicMock()
    host.open_timeline_clip_in_color = delegate  # type: ignore[method-assign]

    host.switch_from_edit_to_color("clip-1")

    delegate.assert_not_called()


def test_switch_from_edit_to_color_delegates_with_playhead() -> None:
    host = _PageHost()
    host.timeline_panel = _make_timeline_panel(None, playhead=17, selected=["clip-1"])
    clip = _clip("clip-1")
    host._current_edit_project = SimpleNamespace(
        active_timeline_id="tl-1", project_id="p1"
    )
    host._current_timeline_sequences = {"tl-1": _fake_timeline([_FakeTrack([clip])])}
    delegate = MagicMock()
    host.open_timeline_clip_in_color = delegate  # type: ignore[method-assign]

    host.switch_from_edit_to_color("clip-1")

    delegate.assert_called_once_with("tl-1", "clip-1", 17)


def test_open_timeline_clip_in_color_precondition_failures() -> None:
    from src.shared.i18n.i18n import I18N

    host = _PageHost()
    host.timeline_panel = _make_timeline_panel(None, playhead=0)

    host._current_edit_project = None
    assert host.open_timeline_clip_in_color("tl-1", "clip-1") is False

    host._current_edit_project = SimpleNamespace(
        assets={}, active_timeline_id="tl-1"
    )
    host._current_timeline_sequences = {}
    assert host.open_timeline_clip_in_color("tl-1", "clip-1") is False

    host._current_timeline_sequences = {"tl-1": _fake_timeline([_FakeTrack([])])}
    assert host.open_timeline_clip_in_color("tl-1", "clip-1") is False

    # Asset missing.
    host._current_timeline_sequences = {
        "tl-1": _fake_timeline([_FakeTrack([_clip("clip-1")])])
    }
    assert host.open_timeline_clip_in_color("tl-1", "clip-1") is False

    # Non-visual asset: status message + False.
    host._current_edit_project = SimpleNamespace(
        assets={"asset-1": SimpleNamespace(is_video=False, is_image=False)},
        color_stacks={},
        project_id="p1",
        active_timeline_id="tl-1",
    )
    assert host.open_timeline_clip_in_color("tl-1", "clip-1") is False
    expected = I18N.tr(
        "edit.clip_not_video_or_image", "Clip is not video or image"
    )
    assert host.status_messages[-1] == (expected, 3000)


def test_open_timeline_clip_in_color_clamps_frames_to_clip_start() -> None:
    host = _PageHost()
    clip = _clip(
        "clip-1",
        timeline_start=100,
        duration=40,
        source_start=500,
        color_stack_id="stack-1",
    )
    host._current_edit_project = SimpleNamespace(
        assets={
            "asset-1": SimpleNamespace(
                is_video=True, is_image=False, metadata={}
            )
        },
        color_stacks={},
        project_id="p1",
        active_timeline_id="tl-1",
    )
    host._current_timeline_sequences = {"tl-1": _fake_timeline([_FakeTrack([clip])])}
    host.timeline_panel = _make_timeline_panel(None, playhead=0)
    host._pause_playback_before_color_clip_switch = MagicMock()  # type: ignore[method-assign]
    host._handle_enter_color_with_context = MagicMock()  # type: ignore[method-assign]
    host.current_page = "Color"

    assert (
        host.open_timeline_clip_in_color("tl-1", "clip-1", timeline_frame=50) is True
    )
    context = host._handle_enter_color_with_context.call_args.args[0]
    assert context.timeline_frame == 100  # before start → clamped to start

    assert (
        host.open_timeline_clip_in_color("tl-1", "clip-1", timeline_frame=999) is True
    )
    context = host._handle_enter_color_with_context.call_args.args[0]
    assert context.timeline_frame == 100  # past end → back to start


def test_open_timeline_clip_re_fetches_clip_after_stack_creation() -> None:
    host = _PageHost()
    stale_clip = _clip(
        "clip-inherit",
        timeline_start=100,
        duration=40,
        source_start=500,
        color_stack_id="stack-inherited",
    )
    updated_clip = dataclasses.replace(stale_clip, color_stack_id="stack-fresh")
    host._current_edit_project = SimpleNamespace(
        assets={
            "asset-1": SimpleNamespace(
                is_video=True, is_image=False, metadata={}
            )
        },
        color_stacks={
            # A clip-local stack still owned by another clip.
            "stack-inherited": SimpleNamespace(
                scope="clip_local", clip_id="clip-other"
            )
        },
        project_id="p1",
        active_timeline_id="tl-1",
    )
    host._current_timeline_sequences = {
        "tl-1": _fake_timeline([_FakeTrack([stale_clip])])
    }
    host.timeline_panel = _make_timeline_panel(None, playhead=110)
    host._pause_playback_before_color_clip_switch = MagicMock()  # type: ignore[method-assign]
    host.current_page = "Edit"
    host.switch_to_page_with_context = MagicMock()  # type: ignore[method-assign]

    def _ensure_and_replace(clip_id: str, asset_id: str) -> str:
        # The real helper swaps in an immutable timeline snapshot that
        # carries the freshly created per-clip stack id.
        host._current_timeline_sequences = {
            "tl-1": _fake_timeline([_FakeTrack([updated_clip])])
        }
        return "stack-fresh"

    ensure = MagicMock(side_effect=_ensure_and_replace)
    host._ensure_clip_has_color_stack = ensure  # type: ignore[method-assign]

    assert host.open_timeline_clip_in_color("tl-1", "clip-inherit") is True

    ensure.assert_called_once_with("clip-inherit", "asset-1")
    context = host.switch_to_page_with_context.call_args.args[0]
    assert context.color_stack_id == "stack-fresh"


def test_pause_playback_before_color_clip_switch_stops_both_engines() -> None:
    host = _PageHost()
    stop = MagicMock()
    host.video_controller = _preview_video_controller(is_playing=True)
    host.video_controller.stop_playback = stop
    controller = _RecordingPlaybackController(is_playing=True)
    host.timeline_panel = _make_timeline_panel(controller)

    host._pause_playback_before_color_clip_switch()

    stop.assert_called_once()
    assert controller.pause_calls == 1

    # Failures are swallowed on both paths.
    stop.side_effect = RuntimeError("vc dead")
    bad_panel = MagicMock()
    bad_panel.get_playback_controller.side_effect = TypeError("nope")
    host.timeline_panel = bad_panel
    host._pause_playback_before_color_clip_switch()  # no raise

    host.timeline_panel = None
    host._pause_playback_before_color_clip_switch()


def test_sync_timeline_playhead_for_composite_entry() -> None:
    host = _PageHost()
    host.timeline_panel = None
    host._sync_timeline_playhead_for_composite_entry(30)  # no-op without panel

    panel = _make_timeline_panel(None, playhead=30)
    host.timeline_panel = panel
    host._sync_timeline_playhead_for_composite_entry(30)
    panel.set_playhead_frame.assert_not_called()  # already aligned

    host._sync_timeline_playhead_for_composite_entry(44)
    panel.set_playhead_frame.assert_called_once_with(44)

    panel.set_playhead_frame.side_effect = RuntimeError("panel gone")
    host._sync_timeline_playhead_for_composite_entry(50)  # logged, not raised


def test_normalize_color_clip_source_frame_branches() -> None:
    host = _PageHost()
    plain_asset = SimpleNamespace(metadata={"is_subclip": False})
    assert (
        host._normalize_color_clip_source_frame(_clip("c"), plain_asset, 42) == 42
    )

    subclip_asset = SimpleNamespace(
        metadata={"is_subclip": True, "source_start": 100}
    )
    clip = _clip("c", source_start=0)
    # Local frame inside a subclip maps into the parent media domain.
    assert host._normalize_color_clip_source_frame(clip, subclip_asset, 10) == 110
    # Already-absolute frames pass through.
    assert host._normalize_color_clip_source_frame(clip, subclip_asset, 150) == 150
    # Clip whose own start is already parent-absolute passes through.
    absolute_clip = _clip("c2", source_start=120)
    assert (
        host._normalize_color_clip_source_frame(absolute_clip, subclip_asset, 10)
        == 10
    )


def test_metadata_int_and_subclip_source_start() -> None:
    host = _PageHost()
    assert (
        PageManagementMixin._metadata_int({"source_start": "12"}, "source_start")
        == 12
    )
    assert PageManagementMixin._metadata_int({}, "missing", 7) == 7
    assert (
        PageManagementMixin._metadata_int(
            SimpleNamespace(source_start=None), "source_start", 3
        )
        == 3
    )
    assert PageManagementMixin._metadata_int("not-metadata", "k") == 0

    assert host._asset_subclip_source_start(None) == 0
    assert (
        host._asset_subclip_source_start(
            SimpleNamespace(metadata={"is_subclip": False})
        )
        == 0
    )
    assert (
        host._asset_subclip_source_start(
            SimpleNamespace(metadata={"is_subclip": True, "source_start": 55})
        )
        == 55
    )
    assert (
        host._asset_subclip_source_start(
            SimpleNamespace(
                metadata=SimpleNamespace(is_subclip=True, source_start=66)
            )
        )
        == 66
    )


def test_create_standalone_image_session_rejects_video() -> None:
    host = _PageHost()
    with pytest.raises(ValueError, match="timeline clip flow"):
        host._create_standalone_image_color_session("/tmp/movie.mov")


def test_create_standalone_image_session_requires_canonical_project() -> None:
    host = _PageHost()
    host._current_edit_project = None
    with pytest.raises(RuntimeError, match="canonical image project"):
        host._create_standalone_image_color_session("/tmp/pic.png")

    # Project without the asset also fails.
    host._current_edit_project = SimpleNamespace(get_asset_by_path=lambda path: None)
    with pytest.raises(RuntimeError, match="canonical image project"):
        host._create_standalone_image_color_session("/tmp/pic.png")


def test_load_color_stack_to_grading_panel_warns_without_project_or_stack() -> None:
    host = _PageHost()
    host._current_edit_project = None
    host._load_color_stack_to_grading_panel("stack-x")  # early return

    host._current_edit_project = SimpleNamespace(color_stacks={})
    host._load_color_stack_to_grading_panel("stack-x")  # stack missing


def test_refresh_video_preview_after_hydration_paths() -> None:
    host = _PageHost()
    host.current_file_is_video = False
    invalidate = MagicMock()
    host._invalidate_video_render_params_if_playing = invalidate  # type: ignore[method-assign]
    host._refresh_video_preview_after_color_stack_hydration()
    invalidate.assert_not_called()

    host.current_file_is_video = True
    host.video_controller = None
    host._refresh_video_preview_after_color_stack_hydration()  # returns quietly
    invalidate.assert_called_once()

    refresh = MagicMock()
    host.video_controller = SimpleNamespace(
        refresh_current_frame_view=refresh, is_video_loaded=lambda: False
    )
    host._refresh_video_preview_after_color_stack_hydration()
    refresh.assert_not_called()

    host.video_controller = SimpleNamespace(
        refresh_current_frame_view=refresh, is_video_loaded=lambda: True
    )
    host._refresh_video_preview_after_color_stack_hydration()
    refresh.assert_called_once()

    def _raise() -> bool:
        raise RuntimeError("refresh failed")

    host.video_controller = SimpleNamespace(
        refresh_current_frame_view=_raise, is_video_loaded=lambda: True
    )
    host._refresh_video_preview_after_color_stack_hydration()  # no raise


def test_collect_color_panels_gathers_truthy_panels() -> None:
    host = _PageHost()
    grading = object()
    curves = object()
    host.grading_panel = grading
    host.curves_widget = curves
    host.hsl_curves_panel = None  # falsy entries skipped
    host.mixer_panel = None
    host.lut_panel = None
    host.denoise_panel = None

    panels = host._collect_color_panels()

    assert panels == {"grading_panel": grading, "curves_panel": curves}


def test_update_color_monitor_reads_cached_asset_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.shared.color.color_transform_provider.get_color_provider_diagnostics",
        lambda config_manager=None: {},
    )
    host = _PageHost()
    update = MagicMock()
    host.canvas = SimpleNamespace(update_color_monitor=update)
    asset = SimpleNamespace(
        path="/tmp/v.mov",
        metadata={
            "color_transfer": "bt709",
            "color_primaries": "bt709",
            "color_range": "tv",
        },
    )

    host._update_color_monitor_for_asset(asset, dict(asset.metadata))

    update.assert_called_once()
    source = update.call_args.kwargs["source"]
    assert source == {"transfer": "bt709", "primaries": "bt709", "range": "tv"}
    assert update.call_args.kwargs["working"] == "scene-linear"
    assert update.call_args.kwargs["display"] == "sRGB"


def test_update_color_monitor_probes_and_backfills_metadata(
    monkeypatch: pytest.MonkeyPatch,
    qapp: Any,
) -> None:
    monkeypatch.setattr(
        "src.shared.color.color_transform_provider.get_color_provider_diagnostics",
        lambda config_manager=None: {},
    )
    probed = SimpleNamespace(
        color_transfer="smpte2084",
        color_primaries="bt2020",
        color_range="full",
    )
    monkeypatch.setattr(
        "src.shared.media.factory.MediaSourceFactory.probe_video_metadata",
        classmethod(lambda cls, path: probed),
    )
    host = _PageHost()
    # AUD-3102 (fixed): the probe runs on a background thread.  Inject an
    # inline executor so the worker → probe_finished emit → UI-thread handler
    # chain runs deterministically inside the test (same-thread emit uses a
    # direct connection, mirroring the queued path).
    host._color_tag_probe_spawn = lambda worker: worker()  # type: ignore[method-assign]
    update = MagicMock()
    host.canvas = SimpleNamespace(update_color_monitor=update)
    metadata: Dict[str, Any] = {}
    asset = SimpleNamespace(path="/tmp/hdr.mov", metadata=metadata)

    host._update_color_monitor_for_asset(asset, metadata)

    # First refresh uses the placeholder (unknown) tags, the probe-completion
    # refresh carries the probed tags; the last call wins for the monitor.
    assert update.call_count == 2
    source = update.call_args.kwargs["source"]
    assert source == {
        "transfer": "smpte2084",
        "primaries": "bt2020",
        "range": "full",
    }
    # The probe result is written back into the asset cache.
    assert metadata["color_transfer"] == "smpte2084"
    assert metadata["color_primaries"] == "bt2020"
    # The in-flight probe bookkeeping is cleaned up.
    assert getattr(host, "_color_tag_probes_inflight", set()) == set()


def test_update_color_monitor_async_probe_stale_generation_dropped(
    monkeypatch: pytest.MonkeyPatch,
    qapp: Any,
) -> None:
    """AUD-3102: a probe result for a superseded clip must not overwrite the
    monitor of the newly opened clip."""
    monkeypatch.setattr(
        "src.shared.color.color_transform_provider.get_color_provider_diagnostics",
        lambda config_manager=None: {},
    )
    probed = SimpleNamespace(
        color_transfer="smpte2084",
        color_primaries="bt2020",
        color_range="full",
    )
    monkeypatch.setattr(
        "src.shared.media.factory.MediaSourceFactory.probe_video_metadata",
        classmethod(lambda cls, path: probed),
    )
    host = _PageHost()
    # Swallow the worker instead of spawning a thread: this test drives the
    # completion handler directly with a stale generation.
    host._color_tag_probe_spawn = lambda worker: None  # type: ignore[method-assign]
    updates: List[Any] = []
    host.canvas = SimpleNamespace(
        update_color_monitor=lambda **kwargs: updates.append(kwargs)
    )
    asset = SimpleNamespace(path="/tmp/first.mov", metadata={})
    host._launch_color_tag_probe(asset)
    first_calls = len(updates)

    # A second open (different asset) bumps the generation before the first
    # probe's completion is processed.
    second = SimpleNamespace(path="/tmp/second.mov", metadata={})
    host._launch_color_tag_probe(second)

    host._on_color_tag_probe_finished(1, asset, {
        "color_transfer": "smpte2084",
        "color_primaries": "bt2020",
        "color_range": "full",
    })

    assert first_calls == 0  # launch itself does not refresh the monitor
    assert updates == []  # stale generation dropped without a refresh
    # The stale path was still removed from the in-flight set.
    assert host._color_tag_probes_inflight == {"/tmp/second.mov"}


def test_update_color_monitor_without_canvas_support_returns() -> None:
    host = _PageHost()
    host.canvas = None
    host._update_color_monitor_for_asset(SimpleNamespace(metadata={}), {})  # no-op

    host.canvas = SimpleNamespace()  # no update_color_monitor attr
    host._update_color_monitor_for_asset(SimpleNamespace(metadata={}), {})


def test_refresh_color_timeline_clip_strip_sets_project_and_sequence() -> None:
    host = _PageHost()
    strip = MagicMock()
    host.color_timeline_clip_strip = strip

    host.refresh_color_timeline_clip_strip()
    strip.set_project_and_timeline.assert_called_with(None, None)

    sequence = object()
    project = SimpleNamespace(active_timeline_id="tl-1")
    host._current_edit_project = project
    host._current_timeline_sequences = {"tl-1": sequence}
    host.refresh_color_timeline_clip_strip()
    strip.set_project_and_timeline.assert_called_with(project, sequence)


# ═══════════════════════════════════════════════════════════════════════
# L. Clip-strip debounce wiring (real QTimer, offscreen Qt)
# ═══════════════════════════════════════════════════════════════════════


def test_clip_strip_sync_debounce_and_cleanup(qapp: Any) -> None:
    host = _PageHost()
    timeline_signal = _FakeSignal()
    switched_signal = _FakeSignal()
    host.timeline_panel = SimpleNamespace(timeline_changed=timeline_signal)
    host.edit_page = SimpleNamespace(
        signal_hub=SimpleNamespace(timeline_switched=switched_signal)
    )

    host._setup_clip_strip_sync()
    assert host._clip_strip_refresh_timer.isSingleShot() is True
    assert host._clip_strip_refresh_timer.interval() == 80

    # A content change and a timeline switch both (re)start the debounce.
    strip = MagicMock()
    host.color_timeline_clip_strip = strip
    host._on_clip_strip_timeline_changed(object())
    assert host._clip_strip_refresh_timer.isActive() is True
    host._on_clip_strip_timeline_switched("tl-2")
    assert host._clip_strip_refresh_timer.isActive() is True

    host._cleanup_clip_strip_sync()
    assert host._clip_strip_refresh_timer.isActive() is False
    assert timeline_signal.slots == []
    assert switched_signal.slots == []

    # Cleanup is idempotent.
    host._cleanup_clip_strip_sync()


def test_clip_strip_debounce_fires_single_refresh(qapp: Any) -> None:
    from PyQt6.QtCore import QEventLoop, QTimer

    host = _PageHost()
    host.timeline_panel = SimpleNamespace(timeline_changed=_FakeSignal())
    host.edit_page = SimpleNamespace(
        signal_hub=SimpleNamespace(timeline_switched=_FakeSignal())
    )
    host._setup_clip_strip_sync()
    strip = MagicMock()
    host.color_timeline_clip_strip = strip

    # Burst of changes collapses into one refresh.
    for _ in range(5):
        host._on_clip_strip_timeline_changed(object())

    loop = QEventLoop()
    QTimer.singleShot(200, loop.quit)
    loop.exec()

    assert strip.set_project_and_timeline.call_count == 1


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__])
