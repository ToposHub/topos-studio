"""Regression tests for responsive TimelinePanel slider zoom."""

from __future__ import annotations

from typing import Any
from types import SimpleNamespace

import pytest

from src.features.edit.core.timeline_v2 import ClipInstance, EditTrack, TimelineSequence
from src.shared.media.metadata import Rational

pytestmark = pytest.mark.edit_core


def _panel_with_recording_zoom(monkeypatch: pytest.MonkeyPatch, qapp: Any) -> tuple[Any, list[float]]:
    from src.features.edit.ui.timeline_panel import TimelinePanel

    panel = TimelinePanel()
    applied: list[float] = []
    monkeypatch.setattr(panel, "_defer_background_media_work_for_zoom", lambda: None)
    monkeypatch.setattr(panel.timeline_view, "set_zoom", applied.append)
    return panel, applied


def test_slider_drag_coalesces_multiple_values_to_one_frame(
    monkeypatch: pytest.MonkeyPatch,
    qapp: Any,
) -> None:
    """A drag should not rebuild the timeline once for every slider pixel."""
    panel, applied = _panel_with_recording_zoom(monkeypatch, qapp)
    panel.zoom_slider.setSliderDown(True)

    panel._on_zoom_changed(100)
    panel._on_zoom_changed(350)
    panel._on_zoom_changed(700)

    assert applied == []
    panel._apply_pending_slider_zoom()

    assert len(applied) == 1
    assert applied[0] == pytest.approx(panel._slider_to_ppf(700))


def test_slider_release_applies_final_value_immediately(
    monkeypatch: pytest.MonkeyPatch,
    qapp: Any,
) -> None:
    """The final slider position must not wait for the 16ms coalescing timer."""
    panel, applied = _panel_with_recording_zoom(monkeypatch, qapp)
    panel.zoom_slider.setSliderDown(True)
    panel._on_zoom_changed(550)

    panel._on_zoom_slider_released()

    assert applied == [pytest.approx(panel._slider_to_ppf(550))]


def test_slider_minimum_keeps_longest_content_at_ten_percent(
    qapp: Any,
) -> None:
    """The minimum zoom endpoint should show the longest content at 10%."""
    from src.features.edit.ui.timeline_panel import (
        MIN_ZOOM_CONTENT_FILL_RATIO,
        TimelinePanel,
    )
    from src.features.edit.ui.timeline_items import TRACK_HEADER_WIDTH

    panel = TimelinePanel()
    panel.timeline_view.resize(1_000, 600)
    timeline = SimpleNamespace(
        video_tracks=[
            SimpleNamespace(
                clips=[SimpleNamespace(timeline_end=1_000)],
                subtitle_clips=[],
            )
        ],
        audio_tracks=[],
        subtitle_tracks=[],
    )

    min_zoom = panel._calculate_slider_min_zoom(timeline)
    content_width = panel.timeline_view.viewport().width() - TRACK_HEADER_WIDTH

    assert 0.0 < min_zoom < 5.0
    assert (1_000 * min_zoom) / content_width == pytest.approx(
        MIN_ZOOM_CONTENT_FILL_RATIO
    )


def test_new_empty_timeline_starts_at_slider_minimum(qapp: Any) -> None:
    """A newly created empty timeline opens at the overview endpoint."""
    from src.features.edit.ui.timeline_panel import TimelinePanel

    panel = TimelinePanel()
    panel.timeline_view.resize(1_000, 600)
    timeline = TimelineSequence(
        timeline_id="empty-zoom-default",
        name="Timeline 1",
        fps=Rational(24, 1),
        width=1920,
        height=1080,
        video_tracks=(EditTrack("v1", "V1", "video", 0),),
        audio_tracks=(EditTrack("a1", "A1", "audio", 0),),
    )

    panel.load_timeline(timeline)

    assert panel.zoom_slider.value() == panel.zoom_slider.minimum()
    assert panel.timeline_view._pixels_per_frame == pytest.approx(
        panel._slider_min_zoom
    )

    # Lazy timeline creation emits timeline_changed from TimelineView rather
    # than calling TimelinePanel.load_timeline(), so the signal path must have
    # the same default.
    panel.timeline_view.set_zoom(5.0)
    panel._on_timeline_content_changed(timeline)
    assert panel.zoom_slider.value() == panel.zoom_slider.minimum()
    assert panel.timeline_view._pixels_per_frame == pytest.approx(
        panel._slider_min_zoom
    )


def test_deliver_mode_hides_edit_tools_but_keeps_zoom_controls(qapp: Any) -> None:
    """Deliver may compact the shared panel without changing Edit state."""
    from src.features.edit.ui.timeline_panel import TimelinePanel

    panel = TimelinePanel()
    panel.resize(900, 400)
    panel.show()
    qapp.processEvents()
    panel.btn_marker.hide()

    panel.set_display_mode("deliver")

    assert panel.btn_select.isHidden()
    assert panel.btn_snap.isHidden()
    assert panel.btn_marker.isHidden()
    assert panel.btn_mixer.isHidden()
    assert not panel.btn_zoom_out.isHidden()
    assert not panel.zoom_slider.isHidden()
    assert not panel.btn_zoom_in.isHidden()
    assert not panel.btn_zoom_fit.isHidden()

    panel.set_display_mode("edit")

    assert not panel.btn_select.isHidden()
    assert not panel.btn_snap.isHidden()
    assert panel.btn_marker.isHidden()
    assert not panel.btn_mixer.isHidden()


def test_deliver_in_out_defaults_to_full_timeline_range(qapp: Any) -> None:
    """Selecting Deliver In/Out exposes a full-duration editable range bar."""
    from src.features.edit.ui.timeline_panel import TimelinePanel

    panel = TimelinePanel()
    clip = ClipInstance(
        clip_id="deliver-range-clip",
        asset_id="deliver-range-asset",
        track_id="v1",
        timeline_start=0,
        timeline_duration=120,
        source_start=0,
        source_duration=120,
    )
    timeline = TimelineSequence(
        timeline_id="deliver-range-timeline",
        name="Deliver Range",
        fps=Rational(24, 1),
        width=1920,
        height=1080,
        video_tracks=(EditTrack("v1", "V1", "video", 0, clips=(clip,)),),
        audio_tracks=(),
    )
    panel.load_timeline(timeline)

    panel.set_deliver_range_type("in_out")

    current = panel.timeline_view._timeline
    assert current is not None
    assert current.range_start == 0
    assert current.range_end == current.duration_frames - 1
    assert panel.timeline_view._range_item is not None
    assert panel.timeline_view._range_item.isVisible()

    panel.set_deliver_range_type(None)


def test_first_clip_after_empty_timeline_is_fitted_to_eighty_percent(qapp: Any) -> None:
    """The first committed clip should not inherit the empty overview zoom."""
    from src.features.edit.ui.timeline_panel import TimelinePanel
    from src.features.edit.ui.timeline_items import TRACK_HEADER_WIDTH

    panel = TimelinePanel()
    panel.timeline_view.resize(1_000, 600)
    empty = TimelineSequence(
        timeline_id="empty-first-clip-fit",
        name="Timeline 1",
        fps=Rational(24, 1),
        width=1920,
        height=1080,
        video_tracks=(EditTrack("v1", "V1", "video", 0),),
        audio_tracks=(EditTrack("a1", "A1", "audio", 0),),
    )
    panel.load_timeline(empty)

    content = TimelineSequence(
        timeline_id=empty.timeline_id,
        name=empty.name,
        fps=empty.fps,
        width=empty.width,
        height=empty.height,
        video_tracks=(
            EditTrack(
                "v1",
                "V1",
                "video",
                0,
                clips=(
                    ClipInstance(
                        clip_id="first-clip",
                        asset_id="asset-first-clip",
                        track_id="v1",
                        timeline_start=0,
                        timeline_duration=120,
                        source_start=0,
                        source_duration=120,
                    ),
                ),
            ),
        ),
        audio_tracks=empty.audio_tracks,
    )
    panel.timeline_view.load_timeline(content)
    panel._on_timeline_content_changed(content)

    content_width = panel.timeline_view.viewport().width() - TRACK_HEADER_WIDTH
    assert (120 * panel.timeline_view._pixels_per_frame) / content_width == pytest.approx(
        0.80
    )
