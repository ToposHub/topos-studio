"""Regression tests for two Edit↔Color round-trip defects.

1. Static-preview transaction must not kill playback started inside the
   async master-decode window: the retry chain and its retry-exhausted
   exact seek both call stop_playback(), so a Space press right after the
   tab switch appeared dead until the user clicked the page and retried.
2. Returning to the Edit page must realign the visible playhead/timecode:
   Color-driven seeks update only the playback controller while the
   timeline view is hidden (``_on_controller_frame_changed`` early-returns
   and ``set_playhead_frame`` never touches the view).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.app.controllers.page_controller import PageManagementMixin
from src.app.main_window import PageContextManager
from src.features.edit.ui.edit_page import EditPage
from src.features.edit.ui.timeline_panel import TimelinePanel


# ---------------------------------------------------------------------------
# Fix 1: static preview bails out once playback is running
# ---------------------------------------------------------------------------


class _PreviewController(PageManagementMixin):
    def __init__(self, *, is_playing: bool, loaded: bool = True) -> None:
        self._color_clip_switch_guard_armed = True
        self._color_static_preview_token = 1
        self.video_controller = SimpleNamespace(
            is_playing=is_playing,
            is_video_loaded=lambda: loaded,
            request_static_preview_frame=MagicMock(return_value=False),
            seek_to_frame=MagicMock(),
            withdraw_static_preview_pending=MagicMock(),
        )


def _preview_context() -> object:
    return PageContextManager().create_context_for_edit_to_color(
        clip_id="clip-preview",
        asset_id="asset-preview",
        timeline_id="tl-preview",
        timeline_frame=10,
        source_frame=50,
        clip_local_frame=0,
        color_stack_id="stack-preview",
    )


def test_static_preview_aborts_once_playback_is_running() -> None:
    """Playback started in the decode window must survive the preview retry."""
    controller = _PreviewController(is_playing=True)
    context = _preview_context()

    # A late retry (user pressed Space before the master frame landed).
    PageManagementMixin._show_color_timeline_clip_static_preview(
        controller, context, retry_count=5, token=1
    )

    controller.video_controller.request_static_preview_frame.assert_not_called()
    controller.video_controller.seek_to_frame.assert_not_called()
    # The armed flash guard is withdrawn so the viewport is not left pending.
    controller.video_controller.withdraw_static_preview_pending.assert_called_once()
    assert controller._color_clip_switch_guard_armed is False


def test_static_preview_exact_seek_fallback_never_fires_while_playing() -> None:
    """The retry-exhausted exact seek (which stops playback) must be skipped."""
    controller = _PreviewController(is_playing=True)
    context = _preview_context()

    PageManagementMixin._show_color_timeline_clip_static_preview(
        controller, context, retry_count=8, token=1
    )

    controller.video_controller.seek_to_frame.assert_not_called()


def test_static_preview_still_runs_when_paused() -> None:
    """The paused preview path keeps requesting the master frame."""
    controller = _PreviewController(is_playing=False)
    context = _preview_context()

    PageManagementMixin._show_color_timeline_clip_static_preview(
        controller, context, token=1
    )
    controller.video_controller.request_static_preview_frame.assert_called_once_with(50)
    controller.video_controller.withdraw_static_preview_pending.assert_not_called()


# ---------------------------------------------------------------------------
# Fix 2: Edit entry realigns the visible playhead with the controller
# ---------------------------------------------------------------------------


class _ViewFake:
    def __init__(self) -> None:
        self.playhead_frames: list[int] = []
        self.corner_timecodes: list[str] = []

    def set_playhead(self, frame: int) -> None:
        self.playhead_frames.append(int(frame))

    def set_corner_timecode(self, timecode: str) -> None:
        self.corner_timecodes.append(timecode)


class _PanelFake(TimelinePanel):
    """TimelinePanel surface limited to the playhead refresh collaborators."""

    def __init__(self, current_frame: int) -> None:  # noqa: D107 - no QWidget init
        self._playback_controller = SimpleNamespace(current_frame=current_frame)
        self._updating = False
        self.timeline_view = _ViewFake()
        self.timecode_label = MagicMock()

    def _frame_to_timecode(self, frame: int) -> str:
        return f"tc-{frame}"


def test_refresh_playhead_pushes_controller_frame_into_view() -> None:
    panel = _PanelFake(current_frame=137)

    TimelinePanel.refresh_playhead_from_controller(panel)

    assert panel.timeline_view.playhead_frames == [137]
    assert panel.timeline_view.corner_timecodes == ["tc-137"]
    # The controller-driven guard must remain balanced.
    assert panel._updating is False


class _EditPageFake(EditPage):
    """EditPage surface limited to on_enter collaborators."""

    def __init__(self) -> None:  # noqa: D107 - no QWidget init
        self._is_active = False
        self._timeline_panel = SimpleNamespace(
            refresh_playhead_from_controller=MagicMock(),
        )
        self._sync_audio_tracks_from_timeline = MagicMock()
        self._audio_active = False
        self._program_viewer = None
        self._current_frame = 0

    def _setup_audio_video_sync(self) -> None:
        raise AssertionError("must not run when audio is inactive")


def test_edit_page_on_enter_refreshes_timeline_playhead() -> None:
    page = _EditPageFake()

    EditPage.on_enter(page)

    page._timeline_panel.refresh_playhead_from_controller.assert_called_once()
    page._sync_audio_tracks_from_timeline.assert_called_once()
    assert page._is_active is True


def test_edit_page_on_enter_tolerates_panels_without_refresh() -> None:
    page = _EditPageFake()
    page._timeline_panel = SimpleNamespace()  # legacy panel / test double

    EditPage.on_enter(page)  # must not raise

    page._sync_audio_tracks_from_timeline.assert_called_once()


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__])
