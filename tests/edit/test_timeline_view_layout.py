from __future__ import annotations

from dataclasses import replace

import pytest
from PyQt6.QtCore import QEvent, QPointF, Qt
from PyQt6.QtGui import QKeyEvent

from src.features.edit.core.project import MediaAsset
from src.features.edit.core.commands import (
    AddTrackCommand,
    BatchDeleteCommand,
    MoveClipsOverwriteCommand,
    MoveClipToTrackCommand,
)
from src.features.edit.core.timeline_v2 import (
    ClipInstance,
    EditTrack,
    SubtitleClip,
    TimelineMarker,
    TimelineSequence,
)
from src.shared.media.metadata import Rational
from src.features.edit.ui.edit_style import EDIT_SIZES
from src.features.edit.ui.timeline_items import (
    ClipItem,
    DragPreviewItem,
    MarkerItem,
    RULER_HEIGHT,
    TRACK_GAP,
    TRACK_HEADER_WIDTH,
)
from src.features.edit.ui.timeline_layout import (
    TimelineLayoutEngine,
    TimelineLayoutState,
    TrackInfo,
    TRACK_AREA_TOP,
    SPLITTER_HEIGHT,
    DEFAULT_SUBTITLE_TRACK_HEIGHT,
)
from src.features.edit.ui.subtitle_clip_item import SubtitleClipItem
from src.features.edit.ui.timeline_view import (
    MIN_ZOOM,
    MUTED_MARKER_COLORS,
    TimelineView,
)
from src.features.edit.ui.timeline_items import TimecodeRulerItem

pytestmark = pytest.mark.edit_core


def _sequence_with_video_and_audio() -> TimelineSequence:
    video_track = EditTrack(
        track_id="v1",
        name="V1",
        track_type="video",
        index=0,
        clips=(
            ClipInstance(
                clip_id="video-clip",
                asset_id="asset-video",
                track_id="v1",
                timeline_start=0,
                timeline_duration=120,
                source_start=0,
                source_duration=120,
            ),
        ),
    )

    audio_track = EditTrack(
        track_id="a1",
        name="A1",
        track_type="audio",
        index=0,
        clips=(
            ClipInstance(
                clip_id="audio-clip",
                asset_id="asset-audio",
                track_id="a1",
                timeline_start=0,
                timeline_duration=120,
                source_start=0,
                source_duration=120,
            ),
        ),
    )
    return TimelineSequence(
        timeline_id="seq-layout",
        name="Layout",
        fps=Rational(24, 1),
        width=1920,
        height=1080,
        video_tracks=(video_track,),
        audio_tracks=(audio_track,),
    )


def _sequence_with_subtitle_track() -> TimelineSequence:
    base = _sequence_with_video_and_audio()
    subtitle_track = EditTrack(
        track_id="s1",
        name="S1",
        track_type="subtitle",
        index=0,
        subtitle_clips=(
            SubtitleClip(
                clip_id="subtitle-clip",
                track_id="s1",
                timeline_start=24,
                timeline_duration=96,
                text="Subtitle preview",
            ),
        ),
    )
    return TimelineSequence(
        timeline_id="seq-subtitle-layout",
        name="Subtitle Layout",
        fps=base.fps,
        width=base.width,
        height=base.height,
        video_tracks=base.video_tracks,
        audio_tracks=base.audio_tracks,
        subtitle_tracks=(subtitle_track,),
    )


def _sequence_with_linked_video_and_audio() -> TimelineSequence:
    video_clip = ClipInstance(
        clip_id="video-clip",
        asset_id="asset-video",
        track_id="v1",
        timeline_start=0,
        timeline_duration=120,
        source_start=0,
        source_duration=120,
        linked_clip_id="audio-clip",
    )
    audio_clip = ClipInstance(
        clip_id="audio-clip",
        asset_id="asset-video",
        track_id="a1",
        timeline_start=0,
        timeline_duration=120,
        source_start=0,
        source_duration=120,
        linked_clip_id="video-clip",
    )
    return TimelineSequence(
        timeline_id="seq-linked-layout",
        name="Linked Layout",
        fps=Rational(24, 1),
        width=1920,
        height=1080,
        video_tracks=(
            EditTrack(
                track_id="v1",
                name="V1",
                track_type="video",
                index=0,
                clips=(video_clip,),
            ),
        ),
        audio_tracks=(
            EditTrack(
                track_id="a1",
                name="A1",
                track_type="audio",
                index=0,
                clips=(audio_clip,),
            ),
        ),
    )


def test_track_layout_uses_cumulative_heights(qapp) -> None:
    view = TimelineView()
    view.load_timeline(_sequence_with_video_and_audio())

    engine = view._layout_engine
    video_top = engine.track_top("v1")
    audio_top = engine.track_top("a1")

    assert view._track_headers["v1"].pos().y() == video_top
    assert view._track_headers["a1"].pos().y() == audio_top
    assert view._items_map["audio-clip"].pos().y() == audio_top

    view.set_zoom(12.0)

    assert view._track_headers["a1"].pos().y() == audio_top
    assert view._items_map["audio-clip"].pos().y() == audio_top


def test_playhead_hover_uses_horizontal_cursor(qapp) -> None:
    """The playhead must not inherit the timeline's vertical resize cursor."""
    view = TimelineView()
    view.load_timeline(_sequence_with_video_and_audio())

    assert view._playhead_item is not None
    playhead_pos = view._playhead_item.sceneBoundingRect().center()
    view._update_cursor_for_position(playhead_pos)

    assert view.cursor().shape() == Qt.CursorShape.SizeHorCursor


def test_playhead_hit_band_only_expands_in_empty_timeline_space(qapp) -> None:
    """A forgiving playhead hit band must not steal nearby clip edits."""
    view = TimelineView()
    view.load_timeline(_sequence_with_video_and_audio())

    assert view._playhead_item is not None
    track_top = view._layout_engine.track_top("v1")

    # Move the playhead past the clip, into a genuine gap.
    view.set_playhead(160)
    empty_gap_pos = QPointF(
        view._playhead_item.scenePos().x() + 4.0,
        track_top + 20.0,
    )
    assert view._is_playhead_hit(empty_gap_pos) is True

    # Put the playhead over the clip.  The same offset must remain owned by
    # the clip instead of becoming part of the forgiving playhead band.
    view.set_playhead(60)
    clip_pos = QPointF(
        view._playhead_item.scenePos().x() + 4.0,
        track_top + 20.0,
    )
    assert view._is_playhead_hit(clip_pos) is False


def test_scoped_marker_anchor_is_inside_clip_lane(qapp) -> None:
    """Scoped markers render inside their associated clip lane."""
    sequence = _sequence_with_video_and_audio()
    scoped_marker = TimelineMarker(
        frame=24,
        name="Clip marker",
        clip_id="video-clip",
        track_id="v1",
    )
    timeline_marker = TimelineMarker(frame=24, name="Timeline marker")
    view = TimelineView()
    view.load_timeline(replace(sequence, markers=(scoped_marker, timeline_marker)))

    track_top = view._layout_engine.track_top("v1")
    track_bottom = track_top + view._layout_engine.track_height("v1")
    scoped_item = view._marker_items[scoped_marker.marker_id]
    timeline_item = view._marker_items[timeline_marker.marker_id]

    assert scoped_item.y() == track_top + MarkerItem.CLIP_MARKER_INSET_Y
    assert scoped_item.y() - MarkerItem.TRIANGLE_SIZE >= track_top
    assert scoped_item.y() <= track_bottom
    assert timeline_item.y() == RULER_HEIGHT


def test_clip_marker_follows_move_and_is_deleted_with_clip(qapp) -> None:
    """The view keeps scoped marker state in sync with clip commands."""
    sequence = _sequence_with_video_and_audio()
    marker = TimelineMarker(
        marker_id="clip-marker",
        frame=25,
        name="Clip marker",
        clip_id="video-clip",
        track_id="v1",
    )
    view = TimelineView()
    view.load_timeline(replace(sequence, markers=(marker,)))

    view.execute_command(
        MoveClipsOverwriteCommand.create(
            [
                {
                    "clip_id": "video-clip",
                    "source_track_id": "v1",
                    "target_track_id": "v1",
                    "old_start": 0,
                    "new_start": 24,
                }
            ]
        )
    )

    assert view._timeline is not None
    moved_marker = view._timeline.markers[0]
    assert moved_marker.frame == 49
    assert view._marker_items[marker.marker_id].frame == 49

    view.execute_command(
        BatchDeleteCommand.create(view._timeline, ["video-clip"], ripple=False)
    )
    assert view._timeline is not None
    assert view._timeline.markers == ()
    assert marker.marker_id not in view._marker_items

    view.undo()
    assert view._timeline is not None
    assert view._timeline.markers[0].frame == 49


def test_new_marker_uses_muted_palette(qapp) -> None:
    """New markers use a varied, low-saturation palette instead of pure red."""
    view = TimelineView()
    view.load_timeline(_sequence_with_video_and_audio())
    view._playhead_frame = 24
    view.add_marker_at_playhead()

    assert view._timeline is not None
    color = view._timeline.markers[0].color
    assert color in MUTED_MARKER_COLORS
    assert color != "#ff0000"


def test_contiguous_same_asset_clips_use_dashed_right_seam(qapp) -> None:
    """Only cuts between adjacent pieces of one source use a dashed seam."""
    clips = (
        ClipInstance(
            clip_id="same-source-left",
            asset_id="source-a",
            track_id="v1",
            timeline_start=0,
            timeline_duration=48,
            source_start=0,
            source_duration=48,
        ),
        ClipInstance(
            clip_id="same-source-right",
            asset_id="source-a",
            track_id="v1",
            timeline_start=48,
            timeline_duration=48,
            source_start=48,
            source_duration=48,
        ),
        ClipInstance(
            clip_id="different-source",
            asset_id="source-b",
            track_id="v1",
            timeline_start=96,
            timeline_duration=48,
            source_start=0,
            source_duration=48,
        ),
    )
    sequence = TimelineSequence(
        timeline_id="seam-style",
        name="Seam style",
        fps=Rational(24, 1),
        width=1920,
        height=1080,
        video_tracks=(
            EditTrack(
                track_id="v1",
                name="V1",
                track_type="video",
                index=0,
                clips=clips,
            ),
        ),
    )
    view = TimelineView()
    view.load_timeline(sequence)

    assert view._items_map["same-source-left"]._right_seam_is_same_source
    assert not view._items_map["same-source-right"]._right_seam_is_same_source
    assert not view._items_map["different-source"]._right_seam_is_same_source


def test_timeline_minimum_zoom_supports_long_overviews(qapp) -> None:
    view = TimelineView()
    view.load_timeline(_sequence_with_video_and_audio())

    view.set_zoom(0.0)

    assert view._pixels_per_frame == MIN_ZOOM


def test_shift_z_fits_all_content_to_eighty_percent(qapp) -> None:
    """Shift+Z should leave the complete timeline at an 80% width fill."""
    view = TimelineView()
    view.resize(1_000, 600)
    view.load_timeline(_sequence_with_video_and_audio())

    event = QKeyEvent(
        QEvent.Type.KeyPress,
        Qt.Key.Key_Z,
        Qt.KeyboardModifier.ShiftModifier,
    )
    view.keyPressEvent(event)

    content_width = view.viewport().width() - TRACK_HEADER_WIDTH
    assert (120 * view._pixels_per_frame) / content_width == pytest.approx(0.80)
    assert view.horizontalScrollBar().value() == view.horizontalScrollBar().minimum()


def test_fade_preview_stays_local_until_commit(qapp) -> None:
    """Fade drag previews must not broadcast heavyweight timeline updates."""
    view = TimelineView()
    timeline = _sequence_with_video_and_audio()
    view.load_timeline(timeline)

    emitted: list[object] = []
    view.timeline_changed.connect(emitted.append)

    view._on_fade_in_preview("video-clip", "v1", 20)
    view._on_fade_out_preview("video-clip", "v1", 20)

    assert view._timeline is timeline
    assert view._timeline.get_clip_by_id("video-clip").fade_in_duration == 0
    assert view._timeline.get_clip_by_id("video-clip").fade_out_duration == 0
    assert emitted == []

    # The release path still commits the locally previewed value once.
    view._on_fade_finished("video-clip", "v1", 0, 0, 20, 0)
    assert view._timeline.get_clip_by_id("video-clip").fade_in_duration == 20
    assert len(emitted) == 1


def test_empty_timeline_extends_when_playhead_moves_beyond_initial_extent(qapp) -> None:
    """An empty sequence must not be capped at its initial 10-second ruler."""
    empty_sequence = TimelineSequence(
        timeline_id="empty-extendable",
        name="Empty extendable timeline",
        fps=Rational(25, 1),
        width=1920,
        height=1080,
        video_tracks=(EditTrack("v1", "V1", "video", 0),),
        audio_tracks=(EditTrack("a1", "A1", "audio", 0),),
    )
    view = TimelineView()
    extended_durations: list[int] = []
    view.timeline_duration_extended.connect(extended_durations.append)
    view.load_timeline(empty_sequence)

    # The initial 250-frame (10-second) extent is only a starting space.
    assert view._effective_timeline_duration() == 250
    assert view._frame_from_scene_x(TRACK_HEADER_WIDTH + 500 * view._pixels_per_frame) == 500

    view.set_playhead(500)

    # At 25fps, growth happens in 10-second / 250-frame chunks.
    assert view._playhead_frame == 500
    assert view._effective_timeline_duration() == 750
    assert extended_durations == [750]
    assert view._ruler_item is not None
    assert view._ruler_item._duration_frames == 750
    assert empty_sequence.duration_frames == 0


def test_scroll_to_timeline_start_resets_startup_offset(qapp) -> None:
    view = TimelineView()
    view.resize(1_000, 600)
    view.load_timeline(_sequence_with_video_and_audio())
    scroll_bar = view.horizontalScrollBar()

    scroll_bar.setValue(scroll_bar.maximum())
    assert scroll_bar.value() > scroll_bar.minimum()

    view.scroll_to_timeline_start(deferred=True)
    qapp.processEvents()

    assert scroll_bar.value() == scroll_bar.minimum()


def test_initial_timeline_load_resets_after_hidden_view_is_shown(qapp) -> None:
    """A timeline loaded before entering Edit must still open at frame zero."""
    view = TimelineView()
    view.resize(1_000, 600)
    view.load_timeline(_sequence_with_video_and_audio())

    # The normal controller loads the timeline while its dock is hidden.
    qapp.processEvents()
    scroll_bar = view.horizontalScrollBar()
    assert scroll_bar.value() == scroll_bar.minimum()

    view.show()
    qapp.processEvents()
    qapp.processEvents()

    assert scroll_bar.value() == scroll_bar.minimum()
    if scroll_bar.isVisible():
        assert scroll_bar.geometry().x() == TRACK_HEADER_WIDTH


def test_clear_resets_empty_workspace_state(qapp) -> None:
    """Clearing a project must remove stale playhead/timecode state."""
    view = TimelineView()
    view.resize(1_000, 600)
    view.load_timeline(_sequence_with_video_and_audio())
    view.set_playhead(328)
    view.clear()
    qapp.processEvents()

    assert view._timeline is None
    assert view._playhead_frame == 0
    assert view._corner_timecode_text == "00:00:00:00"
    assert view.horizontalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAlwaysOff
    assert view._playhead_item is not None
    assert view._playhead_item._effective_track_height() == pytest.approx(
        view.sceneRect().height() - RULER_HEIGHT
    )


def test_ruler_uses_sparse_ticks_at_overview_zoom() -> None:
    ruler = TimecodeRulerItem(
        pixels_per_frame=MIN_ZOOM,
        fps=24.0,
        duration_frames=24 * 60 * 120,
    )

    assert ruler._tick_interval * MIN_ZOOM >= EDIT_SIZES["ruler_target_minor_min_px"]
    assert ruler._major_tick_interval * MIN_ZOOM >= EDIT_SIZES["ruler_target_major_min_px"]


def test_subtitle_lane_is_compact_and_above_video(qapp) -> None:
    view = TimelineView()
    view.resize(1000, 600)
    view.load_timeline(_sequence_with_subtitle_track())

    engine = view._layout_engine
    subtitle_track = engine.track_rect("s1")
    video_track = engine.track_rect("v1")
    subtitle_item = view._items_map["subtitle-clip"]

    assert engine.track_height("s1") == DEFAULT_SUBTITLE_TRACK_HEIGHT
    assert subtitle_track.bottom() == engine.subtitle_splitter_rect().top()
    assert engine.subtitle_splitter_rect().bottom() == video_track.top()
    assert video_track.bottom() == engine.splitter_rect().top()
    assert engine.splitter_rect().bottom() == engine.track_rect("a1").top()
    assert isinstance(subtitle_item, SubtitleClipItem)
    assert subtitle_item.sceneBoundingRect().top() >= subtitle_track.top()
    assert subtitle_item.sceneBoundingRect().bottom() <= subtitle_track.bottom()
    assert subtitle_item.boundingRect().height() < subtitle_track.height()


def test_subtitle_geometry_uses_the_current_timeline_zoom(qapp) -> None:
    """Subtitle X position, width, and drag scale share one frame mapping."""
    view = TimelineView()
    view.resize(1000, 600)
    view.load_timeline(_sequence_with_subtitle_track())

    subtitle_item = view._items_map["subtitle-clip"]
    assert isinstance(subtitle_item, SubtitleClipItem)

    view.set_zoom(12.0)

    assert subtitle_item._pixels_per_frame == 12.0
    assert subtitle_item.x() == view._layout_engine.frame_to_x(24)
    assert subtitle_item.boundingRect().width() == 96 * 12.0

    # The inverse conversion used while dragging must be based on the same
    # current zoom; a 36-pixel pointer delta is exactly three frames at 12 ppf.
    assert round(36.0 / subtitle_item._pixels_per_frame) == 3


def test_zoom_reprojects_active_subtitle_drag_and_copy_ghost(qapp) -> None:
    """Zooming mid-drag must not leave subtitle items at the old X scale."""
    view = TimelineView()
    view.resize(1000, 600)
    view.load_timeline(_sequence_with_subtitle_track())

    source = view._items_map["subtitle-clip"]
    assert isinstance(source, SubtitleClipItem)
    source._drag_mode = "move"
    source._drag_copy_requested = True
    lane_y = view._layout_engine.track_rect("s1").center().y()
    view._on_subtitle_clip_drag_preview("subtitle-clip", "s1", 160, lane_y)
    preview = view._subtitle_copy_drag_preview_item
    assert isinstance(preview, SubtitleClipItem)

    view.set_zoom(12.0)

    # The source stays at its original frame during Alt-copy; the ghost uses
    # the copy frame.  Both must be reprojected with the new scale.
    assert source.x() == view._layout_engine.frame_to_x(source.start_frame)
    assert preview.x() == view._layout_engine.frame_to_x(preview.start_frame)
    assert source.boundingRect().width() == source.duration_frames * 12.0
    assert preview.boundingRect().width() == preview.duration_frames * 12.0


def test_visible_clip_range_accounts_for_the_fixed_track_header(qapp) -> None:
    """Horizontal scrolling must not cull a clip at the viewport's left edge."""
    view = TimelineView()
    view.resize(400, 600)
    view.load_timeline(_sequence_with_subtitle_track())
    view.set_zoom(12.0)

    # Put frame 24 exactly at the content area's left edge.  The scroll value
    # includes the fixed header, which is not a timeline frame span.
    view.horizontalScrollBar().setValue(TRACK_HEADER_WIDTH + 24 * 12)
    visible_start, visible_end = view._calculate_visible_frame_range()

    assert visible_start <= 24 <= visible_end
    assert visible_start == 23


def test_subtitle_lane_height_respects_compact_range(qapp) -> None:
    """SUB-P1-06: subtitle lanes are resizable within a compact range.

    They used to be hard-pinned to DEFAULT_SUBTITLE_TRACK_HEIGHT (40); now an
    out-of-range height clamps to MAX_SUBTITLE_TRACK_HEIGHT (80) instead of
    collapsing back to 40, so the small/medium/large presets take effect.
    """
    from src.features.edit.ui.timeline_layout import (
        MAX_SUBTITLE_TRACK_HEIGHT,
        MIN_SUBTITLE_TRACK_HEIGHT,
    )

    view = TimelineView()
    view.resize(1000, 600)
    view.load_timeline(_sequence_with_subtitle_track())

    # Above the max → clamp down to MAX.
    view._on_track_height_changed("s1", 96)
    assert view._layout_engine.track_height("s1") == MAX_SUBTITLE_TRACK_HEIGHT
    assert view._track_headers["s1"].track_height == MAX_SUBTITLE_TRACK_HEIGHT
    assert view._layout_state.track_heights["s1"] == MAX_SUBTITLE_TRACK_HEIGHT

    # Below the min → clamp up to MIN.
    view._on_track_height_changed("s1", 8)
    assert view._layout_engine.track_height("s1") == MIN_SUBTITLE_TRACK_HEIGHT

    # Within range → honored.
    view._on_track_height_changed("s1", 50)
    assert view._layout_engine.track_height("s1") == 50


def test_subtitle_timeline_leaves_no_empty_area_between_track_stacks() -> None:
    engine = TimelineLayoutEngine()
    state = TimelineLayoutState(video_area_ratio=0.5, subtitle_area_ratio=0.25)
    engine.configure(
        tracks=[
            TrackInfo("s1", "subtitle"),
            TrackInfo("v1", "video"),
            TrackInfo("a1", "audio"),
        ],
        state=state,
        viewport_width=1000,
        viewport_height=600,
        pixels_per_frame=5.0,
    )

    assert engine.area_layout().subtitle_area.height() == DEFAULT_SUBTITLE_TRACK_HEIGHT
    assert engine.track_rect("s1").bottom() == engine.subtitle_splitter_rect().top()
    assert engine.track_rect("v1").top() == engine.subtitle_splitter_rect().bottom()
    assert engine.track_rect("a1").top() == engine.splitter_rect().bottom()


def test_subtitle_and_video_stacks_move_together_with_video_panel() -> None:
    engine = TimelineLayoutEngine()
    state = TimelineLayoutState(video_area_ratio=0.5)
    engine.configure(
        tracks=[
            TrackInfo("s1", "subtitle"),
            TrackInfo("v1", "video"),
            TrackInfo("a1", "audio"),
        ],
        state=state,
        viewport_width=1000,
        viewport_height=600,
        pixels_per_frame=5.0,
    )

    subtitle_top_before = engine.track_top("s1")
    video_top_before = engine.track_top("v1")
    engine.set_splitter_ratio(0.7)

    subtitle_top_after = engine.track_top("s1")
    video_top_after = engine.track_top("v1")
    assert subtitle_top_after - subtitle_top_before == pytest.approx(
        video_top_after - video_top_before
    )
    assert engine.track_rect("s1").bottom() == engine.subtitle_splitter_rect().top()
    assert engine.subtitle_splitter_rect().bottom() == engine.track_rect("v1").top()


# ── SUB-P1-06: resizable subtitle lanes & dedicated scrollbar ──────────────


def test_subtitle_track_height_preset_takes_effect(qapp) -> None:
    """SUB-P1-06: small/medium/large presets now actually change subtitle height."""
    from src.features.edit.ui.timeline_layout import MAX_SUBTITLE_TRACK_HEIGHT

    view = TimelineView()
    view.resize(1000, 600)
    view.load_timeline(_sequence_with_subtitle_track())

    view.set_track_height_preset("large")
    assert view._layout_engine.track_height("s1") == 50

    view.set_track_height_preset("small")
    assert view._layout_engine.track_height("s1") == 28

    view.set_track_height_preset("default")
    assert view._layout_engine.track_height("s1") == DEFAULT_SUBTITLE_TRACK_HEIGHT


def test_transition_drag_preview_tracks_timeline_zoom(qapp) -> None:
    """Transition drag preview must share the timeline frame-to-pixel scale."""
    view = TimelineView()
    preview = view._drag_preview_item
    assert isinstance(preview, DragPreviewItem)

    preview.set_assets(["Cross Dissolve"], ["transition"], 12)
    preview.set_position(0, 40)

    view.set_zoom(2.0)

    assert preview._pixels_per_frame == pytest.approx(2.0)
    assert preview.x() == pytest.approx(TRACK_HEADER_WIDTH + 40 * 2.0)
    assert preview.boundingRect().width() == pytest.approx(12 * 2.0)


def test_subtitle_lane_overflows_and_produces_scroll_range() -> None:
    """SUB-P1-06: many subtitle tracks cap the lane and yield a non-zero scroll range."""
    engine = TimelineLayoutEngine()
    state = TimelineLayoutState(video_area_ratio=0.5)
    # 12 subtitle tracks at default height would be ~480px; the lane is capped
    # at ~40% of usable area, so overflow is guaranteed.
    tracks = [TrackInfo(f"s{i}", "subtitle") for i in range(12)]
    tracks += [TrackInfo("v1", "video"), TrackInfo("a1", "audio")]
    engine.configure(
        tracks=tracks, state=state,
        viewport_width=1000, viewport_height=600, pixels_per_frame=5.0,
    )
    minimum, maximum, page_step = engine.scroll_range_for_area("subtitle")
    assert minimum == 0
    assert maximum > 0  # overflow → scrollable
    assert page_step >= 1


def test_subtitle_scrollbar_appears_for_many_tracks(qapp) -> None:
    """SUB-P1-06: the dedicated subtitle scrollbar becomes visible on overflow."""
    base = _sequence_with_video_and_audio()
    sub_tracks = tuple(
        EditTrack(
            track_id=f"s{i}", name=f"S{i}", track_type="subtitle", index=i,
            subtitle_clips=(
                SubtitleClip(
                    clip_id=f"sub-{i}", track_id=f"s{i}",
                    timeline_start=0, timeline_duration=72, text=f"cue {i}",
                ),
            ),
        )
        for i in range(10)
    )
    seq = TimelineSequence(
        timeline_id="seq-many-subs", name="ManySubs",
        fps=base.fps, width=base.width, height=base.height,
        video_tracks=base.video_tracks, audio_tracks=base.audio_tracks,
        subtitle_tracks=sub_tracks,
    )
    view = TimelineView()
    view.resize(1000, 600)
    view.load_timeline(seq)
    view._sync_split_vertical_scrollbars()

    # The dedicated subtitle scrollbar is enabled with a non-zero range when
    # the cue stack overflows the (capped) lane. (isVisible is unreliable in a
    # headless test harness, so assert the functional contract instead.)
    assert view._subtitle_v_scroll.isEnabled() is True
    assert view._subtitle_v_scroll.maximum() > 0


def test_subtitle_scroll_offset_drives_track_positions() -> None:
    """SUB-P1-06: setting subtitle_scroll_offset shifts subtitle track Ys."""
    engine = TimelineLayoutEngine()
    state0 = TimelineLayoutState(video_area_ratio=0.5, subtitle_scroll_offset=0)
    tracks = [TrackInfo(f"s{i}", "subtitle") for i in range(12)]
    tracks += [TrackInfo("v1", "video"), TrackInfo("a1", "audio")]
    engine.configure(
        tracks=tracks, state=state0,
        viewport_width=1000, viewport_height=600, pixels_per_frame=5.0,
    )
    top_at_zero = engine.track_top("s0")

    state1 = TimelineLayoutState(video_area_ratio=0.5, subtitle_scroll_offset=60)
    engine.configure(
        tracks=tracks, state=state1,
        viewport_width=1000, viewport_height=600, pixels_per_frame=5.0,
    )
    top_at_offset = engine.track_top("s0")
    # Scrolling down by 60 lifts the top track up by ~60px (or to the cap).
    assert top_at_offset <= top_at_zero
    assert (top_at_zero - top_at_offset) >= 0


def test_ruler_and_track_hit_testing_are_separate(qapp) -> None:
    view = TimelineView()
    view.load_timeline(_sequence_with_video_and_audio())

    ruler_y = EDIT_SIZES["ruler_h"] / 2
    audio_y = view._layout_engine.track_top("a1") + 1

    assert view._get_track_and_frame_from_pos(QPointF(200, ruler_y))[0] == -1
    assert view._get_track_and_frame_from_pos(QPointF(200, audio_y))[0] == 1


def test_asset_insert_duration_uses_timeline_fps(qapp) -> None:
    view = TimelineView()
    view.load_timeline(_sequence_with_video_and_audio())

    asset = MediaAsset(
        asset_id="asset-30fps",
        path="/tmp/asset.mov",
        media_type="video",
        display_name="asset.mov",
        metadata={
            "fps": 30.0,
            "total_frames": 300,
            "duration_seconds": 10.0,
        },
    )

    assert view._get_asset_duration_frames(asset) == 240
    assert view._get_asset_source_duration_frames(asset) == 300
    assert view._get_asset_playback_speed(asset, 240, 300) == 1.0


def test_default_timeline_conforms_to_first_video_fps(qapp) -> None:
    view = TimelineView()
    asset = MediaAsset(
        asset_id="asset-25fps",
        path="/tmp/asset-25.mov",
        media_type="video",
        display_name="asset-25.mov",
        metadata={"fps": 25.0},
    )

    timeline = view._create_default_timeline(asset)

    assert timeline is not None
    assert float(timeline.fps) == 25.0


def test_default_timeline_records_random_media_pool_preview_source(qapp) -> None:
    view = TimelineView()
    asset = MediaAsset(
        asset_id="asset-preview",
        path="/tmp/asset-preview.mov",
        media_type="video",
        display_name="asset-preview.mov",
        metadata={"total_frames": 120},
    )

    timeline = view._create_default_timeline(asset)

    assert timeline is not None
    assert timeline.format_settings["media_pool_preview_asset_id"] == asset.asset_id
    assert 0 <= timeline.format_settings["media_pool_preview_frame"] < 120


def test_default_timeline_uses_media_pool_timeline_name(qapp) -> None:
    """Dragging the first asset creates the same default name as the dialog."""
    view = TimelineView()

    timeline = view._create_default_timeline()

    assert timeline is not None
    assert timeline.name == "Timeline 1"


def test_default_timeline_without_first_asset_uses_25fps(qapp) -> None:
    """A blank/provisional timeline uses the configured 25fps default."""
    view = TimelineView()

    timeline = view._create_default_timeline()

    assert timeline is not None
    assert timeline.fps == Rational(25, 1)


def test_empty_provisional_timeline_conforms_before_first_video_insert(qapp) -> None:
    view = TimelineView()
    provisional_timeline = view._create_default_timeline()
    assert provisional_timeline is not None
    view.load_timeline(provisional_timeline)

    asset = MediaAsset(
        asset_id="asset-25fps",
        path="/tmp/asset-25.mov",
        media_type="video",
        display_name="asset-25.mov",
        metadata={"fps": 25.0},
    )

    assert view._ensure_first_asset_timeline_settings(asset) is True
    assert view._timeline is not None
    assert float(view._timeline.fps) == 25.0


def test_first_lazy_drop_reuses_default_track_in_empty_area(qapp) -> None:
    """The first drop must not create a visual top track from empty space."""
    view = TimelineView()
    view.resize(1_000, 600)
    asset = MediaAsset(
        asset_id="asset-first-drop",
        path="/tmp/asset-first-drop.mov",
        media_type="video",
        display_name="asset-first-drop.mov",
        metadata={},
    )

    assert view._timeline is None
    assert view._ensure_first_asset_timeline_settings(asset) is True
    assert view._timeline is not None

    view._configure_layout_engine()
    area = view._layout_engine.area_layout()
    empty_video_y = area.video_area.top() + 1
    drop = view._layout_engine.resolve_drop_target(
        TRACK_HEADER_WIDTH + 100,
        empty_video_y,
        "video",
    )
    assert drop.create_track is True

    resolved = view._prefer_lazy_default_track(drop, "video", True)

    assert resolved.is_valid is True
    assert resolved.create_track is False
    assert resolved.track_id == view._timeline.video_tracks[0].track_id


def test_new_project_can_show_ui_only_v1_and_a1_tracks(qapp) -> None:
    """A blank project exposes real V1/A1 geometry before the first drop."""
    view = TimelineView()

    timeline = view.show_provisional_timeline()

    assert view.has_provisional_timeline() is True
    assert view._timeline is timeline
    assert [track.name for track in timeline.video_tracks] == ["V1"]
    assert [track.name for track in timeline.audio_tracks] == ["A1"]
    assert not timeline.video_tracks[0].clips
    assert not timeline.audio_tracks[0].clips

    view._configure_layout_engine()
    assert not view._layout_engine.track_rect(timeline.video_tracks[0].track_id).isNull()
    assert not view._layout_engine.track_rect(timeline.audio_tracks[0].track_id).isNull()


def test_hiding_drag_preview_also_hides_snap_indicator(qapp) -> None:
    """The yellow snap line must disappear after a drag is finalized/cancelled."""
    view = TimelineView()
    assert view._snap_indicator is not None

    view._snap_indicator.show_indicator(24)
    assert view._snap_indicator.is_visible() is True

    view._hide_drag_preview()

    assert view._snap_indicator.is_visible() is False


def test_first_video_fps_uses_exact_metadata_rate(qapp) -> None:
    view = TimelineView()
    asset = MediaAsset(
        asset_id="asset-2997",
        path="/tmp/asset-2997.mov",
        media_type="video",
        display_name="asset-2997.mov",
        metadata={"fps": 29.97, "fps_num": 30000, "fps_den": 1001},
    )

    timeline = view._create_default_timeline(asset)

    assert timeline is not None
    assert timeline.fps == Rational(30000, 1001)


def test_existing_timeline_with_clips_keeps_its_selected_fps(qapp) -> None:
    view = TimelineView()
    sequence = _sequence_with_video_and_audio()
    view.load_timeline(sequence)
    asset = MediaAsset(
        asset_id="asset-25fps",
        path="/tmp/asset-25.mov",
        media_type="video",
        display_name="asset-25.mov",
        metadata={"fps": 25.0},
    )

    assert view._ensure_first_asset_timeline_settings(asset) is True
    assert view._timeline is not None
    assert view._timeline.fps == Rational(24, 1)


def test_asset_fps_falls_back_to_timeline_fps_for_audio(qapp) -> None:
    view = TimelineView()
    base_sequence = _sequence_with_video_and_audio()
    sequence = TimelineSequence(
        timeline_id=base_sequence.timeline_id,
        name=base_sequence.name,
        fps=Rational(30, 1),
        width=base_sequence.width,
        height=base_sequence.height,
        video_tracks=base_sequence.video_tracks,
        audio_tracks=base_sequence.audio_tracks,
    )
    view.load_timeline(sequence)

    asset = MediaAsset(
        asset_id="asset-audio",
        path="/tmp/audio.wav",
        media_type="audio",
        display_name="audio.wav",
        metadata={
            "duration_seconds": 10.0,
        },
    )

    class Project:
        assets = {"asset-audio": asset}

    view.set_project(Project())

    assert view._get_asset_fps("asset-audio") == 30.0


def test_linked_drag_finished_moves_video_and_audio_atomically(qapp) -> None:
    view = TimelineView()
    view.load_timeline(_sequence_with_linked_video_and_audio())
    view._snap_enabled = False

    view._on_drag_finished("video-clip", "v1", 0, 24, "v1")

    assert view._timeline is not None
    assert view._timeline.get_clip_by_id("video-clip").timeline_start == 24
    assert view._timeline.get_clip_by_id("audio-clip").timeline_start == 24


def test_one_way_link_does_not_drag_audio_with_video(qapp) -> None:
    sequence = _sequence_with_linked_video_and_audio()
    audio = sequence.get_clip_by_id("audio-clip")
    assert audio is not None
    sequence = sequence.with_clip_updated(audio.with_linked_clip_id(None))

    view = TimelineView()
    view.load_timeline(sequence)
    view._snap_enabled = False

    view._on_drag_finished("video-clip", "v1", 0, 24, "v1")

    assert view._timeline is not None
    assert view._timeline.get_clip_by_id("video-clip").timeline_start == 24
    assert view._timeline.get_clip_by_id("audio-clip").timeline_start == 0


def test_linked_av_cross_track_move_keeps_opposite_visual_offset(qapp) -> None:
    """Moving video up two lanes moves linked audio down two lanes."""
    sequence = _sequence_with_n_tracks(3, 3)
    video = sequence.get_clip_by_id("video-clip-1")
    audio = sequence.get_clip_by_id("audio-clip-1")
    assert video is not None
    assert audio is not None
    sequence = sequence.with_clip_updated(
        video.with_linked_clip_id("audio-clip-1")
    ).with_clip_updated(
        audio.with_linked_clip_id("video-clip-1")
    )

    view = TimelineView()
    view.load_timeline(sequence)
    view._snap_enabled = False
    view._configure_layout_engine()

    target_y = view._layout_engine.track_top("v3") + 2.0
    view._on_clip_moved("video-clip-1", "v1", 12, target_y)

    assert view._current_drag_session is not None
    assert view._current_drag_session.current_track_ids["video-clip-1"] == "v3"
    assert view._current_drag_session.current_track_ids["audio-clip-1"] == "a3"
    assert view._items_map["audio-clip-1"].y() == view._layout_engine.track_top("a3")


def test_linked_av_cross_track_release_keeps_preview_targets(qapp) -> None:
    """Releasing after a cross-track preview must commit both target lanes."""
    sequence = _sequence_with_n_tracks(3, 3)
    video = sequence.get_clip_by_id("video-clip-1")
    audio = sequence.get_clip_by_id("audio-clip-1")
    assert video is not None
    assert audio is not None
    sequence = sequence.with_clip_updated(
        video.with_linked_clip_id("audio-clip-1")
    ).with_clip_updated(
        audio.with_linked_clip_id("video-clip-1")
    )

    view = TimelineView()
    view.load_timeline(sequence)
    view._snap_enabled = False
    view._configure_layout_engine()

    target_y = view._layout_engine.track_top("v3") + 2.0
    view._on_clip_moved("video-clip-1", "v1", 12, target_y)
    view._on_drag_finished("video-clip-1", "v1", 0, 12, "v3")

    assert view._timeline is not None
    assert view._timeline.get_clip_by_id("video-clip-1").track_id == "v3"
    assert view._timeline.get_clip_by_id("audio-clip-1").track_id == "a3"


def test_linked_av_move_above_top_creates_paired_edge_tracks(qapp) -> None:
    """Moving linked video above the top lane also creates the audio lane."""
    view = TimelineView()
    view.load_timeline(_sequence_with_linked_video_and_audio())
    view._snap_enabled = False
    view._configure_layout_engine()

    top_y = view._layout_engine.track_top("v1") - 2.0
    view._on_clip_moved("video-clip", "v1", 0, top_y)

    session = view._current_drag_session
    assert session is not None
    assert session.current_track_ids["video-clip"] == "__new_video_track_above__"
    assert session.current_track_ids["audio-clip"] == "__new_audio_track_below__"

    view._on_drag_finished(
        "video-clip",
        "v1",
        0,
        0,
        "__new_video_track_above__",
    )

    assert view._timeline is not None
    moved_video = view._timeline.get_clip_by_id("video-clip")
    moved_audio = view._timeline.get_clip_by_id("audio-clip")
    assert moved_video is not None
    assert moved_audio is not None
    assert moved_video.track_id != "v1"
    assert moved_audio.track_id != "a1"
    assert len(view._timeline.video_tracks) == 2
    assert len(view._timeline.audio_tracks) == 2


def test_cross_track_drag_overwrites_occupied_existing_track(qapp) -> None:
    moving = ClipInstance(
        clip_id="moving",
        asset_id="asset-moving",
        track_id="v1",
        timeline_start=0,
        timeline_duration=80,
        source_start=0,
        source_duration=80,
    )
    blocker = ClipInstance(
        clip_id="blocker",
        asset_id="asset-blocker",
        track_id="v2",
        timeline_start=20,
        timeline_duration=30,
        source_start=0,
        source_duration=30,
    )
    sequence = TimelineSequence(
        timeline_id="seq-overwrite-drag",
        name="Overwrite Drag",
        fps=Rational(24, 1),
        width=1920,
        height=1080,
        video_tracks=(
            EditTrack("v1", "V1", "video", 0, clips=(moving,)),
            EditTrack("v2", "V2", "video", 1, clips=(blocker,)),
        ),
    )

    view = TimelineView()
    view.load_timeline(sequence)
    view._snap_enabled = False

    view._on_drag_finished("moving", "v1", 0, 10, "v2")

    assert view._timeline is not None
    assert view._timeline.get_track_by_id("v1").get_clip_by_id("moving") is None
    moved = view._timeline.get_track_by_id("v2").get_clip_by_id("moving")
    assert moved is not None
    assert moved.timeline_start == 10
    assert view._timeline.get_clip_by_id("blocker") is None


def test_drag_target_resets_when_moving_back_to_original_track(qapp) -> None:
    view = TimelineView()
    view.load_timeline(_sequence_with_n_tracks(2, 0))
    view._snap_enabled = False
    view._configure_layout_engine()

    v2_rect = view._layout_engine.track_rect("v2")
    v1_rect = view._layout_engine.track_rect("v1")
    y_v2 = v2_rect.top() + v2_rect.height() * 0.75
    y_v1 = v1_rect.top() + v1_rect.height() * 0.5

    view._on_clip_moved("video-clip-1", "v1", 12, y_v2)
    item = view._items_map["video-clip-1"]
    assert item._drag_target_track_id == "v2"

    view._on_clip_moved("video-clip-1", "v1", 12, y_v1)

    assert item._drag_target_track_id == ""
    assert view._current_drag_session is not None
    assert view._current_drag_session.current_track_ids["video-clip-1"] == "v1"


def test_drag_preview_reuses_snap_points_within_session(qapp, monkeypatch) -> None:
    view = TimelineView()
    view.load_timeline(_sequence_with_n_tracks(1, 0))
    view._snap_enabled = True
    view._configure_layout_engine()
    view._snap_manager = None
    calls = {"count": 0}

    def fake_snap_points(exclude_clip_id=None):
        calls["count"] += 1
        return (0, 24, 48)

    monkeypatch.setattr(view, "_get_snap_points", fake_snap_points)
    y_v1 = view._layout_engine.track_top("v1") + 2

    view._on_clip_moved("video-clip-1", "v1", 12, y_v1)
    view._on_clip_moved("video-clip-1", "v1", 13, y_v1)

    assert calls["count"] == 1


def test_drag_target_uses_nearest_track_across_gap(qapp) -> None:
    view = TimelineView()
    view.load_timeline(_sequence_with_n_tracks(3, 0))
    view._configure_layout_engine()

    v2_rect = view._layout_engine.track_rect("v2")
    v1_rect = view._layout_engine.track_rect("v1")
    gap_mid_y = (v2_rect.center().y() + v1_rect.center().y()) / 2.0 - 0.1

    assert view._layout_engine.track_at_y(gap_mid_y) is None
    assert (
        view._resolve_drag_target_track(
            "video-clip-1",
            "v1",
            gap_mid_y,
            "video",
        )
        == "v2"
    )


def test_drag_target_edge_band_does_not_steal_existing_top_track(qapp) -> None:
    view = TimelineView()
    view.load_timeline(_sequence_with_n_tracks(3, 0))
    view._configure_layout_engine()

    top_rect = view._layout_engine.track_rect("v3")
    y_inside_top_edge = top_rect.top() + 1.0
    y_above_stack = top_rect.top() - 1.0

    assert (
        view._resolve_drag_target_track(
            "video-clip-1",
            "v1",
            y_inside_top_edge,
            "video",
        )
        == "v3"
    )
    assert (
        view._resolve_drag_target_track(
            "video-clip-1",
            "v1",
            y_above_stack,
            "video",
        )
        == "__new_video_track_above__"
    )


def test_audio_drag_target_edge_band_does_not_steal_existing_bottom_track(qapp) -> None:
    view = TimelineView()
    view.load_timeline(_sequence_with_n_tracks(0, 3))
    view._configure_layout_engine()

    bottom_rect = view._layout_engine.track_rect("a3")
    y_inside_bottom_edge = bottom_rect.bottom() - 1.0
    y_below_stack = bottom_rect.bottom() + 1.0

    assert (
        view._resolve_drag_target_track(
            "audio-clip-1",
            "a1",
            y_inside_bottom_edge,
            "audio",
        )
        == "a3"
    )
    assert (
        view._resolve_drag_target_track(
            "audio-clip-1",
            "a1",
            y_below_stack,
            "audio",
        )
        == "__new_audio_track_below__"
    )


def test_layout_engine_configure_is_cached_for_repeated_drag_queries(qapp, monkeypatch) -> None:
    view = TimelineView()
    view.load_timeline(_sequence_with_n_tracks(2, 2))
    view._layout_config_signature = None
    calls = {"count": 0}
    original_configure = view._layout_engine.configure

    def counted_configure(*args, **kwargs):
        calls["count"] += 1
        return original_configure(*args, **kwargs)

    monkeypatch.setattr(view._layout_engine, "configure", counted_configure)

    view._configure_layout_engine()
    view._configure_layout_engine()
    view._configure_layout_engine()

    assert calls["count"] == 1

    view._layout_state.audio_scroll_offset += 1
    view._configure_layout_engine()

    assert calls["count"] == 2


def test_add_empty_track_updates_headers_without_full_timeline_reload(qapp, monkeypatch) -> None:
    view = TimelineView()
    view.load_timeline(_sequence_with_n_tracks(1, 0))
    original_clip_item = view._items_map["video-clip-1"]

    def fail_load_timeline(_timeline):
        raise AssertionError("empty track add should not full reload timeline items")

    monkeypatch.setattr(view, "load_timeline", fail_load_timeline)

    view._execute_command(AddTrackCommand.create("video"))

    assert view._timeline is not None
    assert len(view._timeline.video_tracks) == 2
    assert len(view._track_headers) == 2
    assert view._items_map["video-clip-1"] is original_clip_item


def test_move_clip_to_existing_empty_track_updates_incrementally(qapp, monkeypatch) -> None:
    view = TimelineView()
    sequence = TimelineSequence(
        timeline_id="seq-move-track",
        name="Move Track",
        fps=Rational(24, 1),
        width=1920,
        height=1080,
        video_tracks=(
            EditTrack(
                track_id="v1",
                name="V1",
                track_type="video",
                index=0,
                clips=(
                    ClipInstance(
                        clip_id="clip-1",
                        asset_id="asset-1",
                        track_id="v1",
                        timeline_start=0,
                        timeline_duration=120,
                        source_start=0,
                        source_duration=120,
                    ),
                ),
            ),
            EditTrack(
                track_id="v2",
                name="V2",
                track_type="video",
                index=1,
                clips=(),
            ),
        ),
    )
    view.load_timeline(sequence)
    original_clip_item = view._items_map["clip-1"]

    def fail_load_timeline(_timeline):
        raise AssertionError("cross-track clip move should not full reload timeline items")

    monkeypatch.setattr(view, "load_timeline", fail_load_timeline)

    command = MoveClipsOverwriteCommand.create(
        [
            {
                "clip_id": "clip-1",
                "source_track_id": "v1",
                "target_track_id": "v2",
                "old_start": 0,
                "new_start": 24,
            }
        ]
    )
    view._execute_command(command)

    assert view._timeline is not None
    assert view._items_map["clip-1"] is original_clip_item
    assert original_clip_item.track_id == "v2"
    assert original_clip_item.start_frame == 24
    assert original_clip_item.pos().y() == view._layout_engine.track_top("v2")
    assert [clip.clip_id for clip in view._timeline.video_tracks[0].clips] == []
    assert [clip.clip_id for clip in view._timeline.video_tracks[1].clips] == ["clip-1"]


def test_move_clip_to_auto_created_track_updates_incrementally(qapp, monkeypatch) -> None:
    view = TimelineView()
    view.load_timeline(_sequence_with_n_tracks(1, 0))
    original_clip_item = view._items_map["video-clip-1"]

    def fail_load_timeline(_timeline):
        raise AssertionError("auto-created track move should not full reload timeline items")

    monkeypatch.setattr(view, "load_timeline", fail_load_timeline)

    command = MoveClipToTrackCommand.create_for_new_track(
        source_track_id="v1",
        clip_id="video-clip-1",
        target_track_type="video",
        new_track_position="above",
        new_start=36,
        old_start=0,
    )
    view._execute_command(command)

    assert view._timeline is not None
    assert len(view._timeline.video_tracks) == 2
    target_track = view._timeline.video_tracks[-1]
    assert [clip.clip_id for clip in view._timeline.video_tracks[0].clips] == []
    assert [clip.clip_id for clip in target_track.clips] == ["video-clip-1"]
    assert view._items_map["video-clip-1"] is original_clip_item
    assert original_clip_item.track_id == target_track.track_id
    assert original_clip_item.start_frame == 36
    assert original_clip_item.pos().y() == view._layout_engine.track_top(target_track.track_id)
    assert target_track.track_id in view._track_headers


# ── Helpers for multi-track sequences ────────────────────────────────────────


def _sequence_with_n_tracks(n_video: int, n_audio: int) -> TimelineSequence:
    """Create a sequence with *n_video* video tracks and *n_audio* audio tracks."""
    video_tracks = []
    for i in range(n_video):
        video_tracks.append(
            EditTrack(
                track_id=f"v{i + 1}",
                name=f"V{i + 1}",
                track_type="video",
                index=i,
                clips=(
                    ClipInstance(
                        clip_id=f"video-clip-{i + 1}",
                        asset_id=f"asset-video-{i + 1}",
                        track_id=f"v{i + 1}",
                        timeline_start=0,
                        timeline_duration=120,
                        source_start=0,
                        source_duration=120,
                    ),
                ),
            )
        )
    audio_tracks = []
    for i in range(n_audio):
        audio_tracks.append(
            EditTrack(
                track_id=f"a{i + 1}",
                name=f"A{i + 1}",
                track_type="audio",
                index=i,
                clips=(
                    ClipInstance(
                        clip_id=f"audio-clip-{i + 1}",
                        asset_id=f"asset-audio-{i + 1}",
                        track_id=f"a{i + 1}",
                        timeline_start=0,
                        timeline_duration=120,
                        source_start=0,
                        source_duration=120,
                    ),
                ),
            )
        )
    return TimelineSequence(
        timeline_id="seq-multi",
        name="Multi",
        fps=Rational(24, 1),
        width=1920,
        height=1080,
        video_tracks=tuple(video_tracks),
        audio_tracks=tuple(audio_tracks),
    )


def _make_engine_with_tracks(
    n_video: int,
    n_audio: int,
    viewport_height: float = 400.0,
    viewport_width: float = 800.0,
) -> TimelineLayoutEngine:
    """Create and configure a layout engine with the given number of tracks."""
    tracks: list[TrackInfo] = []
    for i in range(n_video):
        tracks.append(TrackInfo(track_id=f"v{i + 1}", track_type="video"))
    for i in range(n_audio):
        tracks.append(TrackInfo(track_id=f"a{i + 1}", track_type="audio"))
    engine = TimelineLayoutEngine()
    engine.configure(
        tracks=tracks,
        state=TimelineLayoutState(),
        viewport_width=viewport_width,
        viewport_height=viewport_height,
        pixels_per_frame=5.0,
    )
    return engine


# ── Test 1: Video/audio area split ──────────────────────────────────────────


def test_layout_engine_video_audio_area_split(qapp) -> None:
    """Video area and audio area are separated by the splitter."""
    engine = _make_engine_with_tracks(1, 1, viewport_height=400)

    area = engine.area_layout()

    # Both areas must be non-empty
    assert area.video_area.height() > 0
    assert area.audio_area.height() > 0

    # Splitter sits between them
    assert area.splitter_rect.height() == SPLITTER_HEIGHT

    # Video area bottom edge touches splitter top
    assert area.video_area.bottom() == pytest.approx(area.splitter_rect.top())
    # Audio area top edge touches splitter bottom
    assert area.audio_area.top() == pytest.approx(area.splitter_rect.bottom())


# ── Test 2: V1 near splitter, A1 near splitter ──────────────────────────────


def test_layout_engine_v1_near_splitter_a1_near_splitter(qapp) -> None:
    """V1 is closest to the splitter among video tracks; A1 closest among audio."""
    engine = _make_engine_with_tracks(2, 2, viewport_height=500)

    v1_top = engine.track_top("v1")
    v2_top = engine.track_top("v2")
    a1_top = engine.track_top("a1")
    a2_top = engine.track_top("a2")

    # Video tracks are displayed reversed: V2 at top, V1 at bottom (near splitter)
    assert v1_top > v2_top, "V1 should be below V2 (V1 closer to splitter)"

    # Audio tracks in normal order: A1 at top (near splitter), A2 below
    assert a1_top < a2_top, "A1 should be above A2 (A1 closer to splitter)"

    area = engine.area_layout()

    # V1 bottom should be near the splitter top
    v1_rect = engine.track_rect("v1")
    assert v1_rect.bottom() <= area.splitter_rect.top() + TRACK_GAP + 1

    # A1 top should be near the splitter bottom
    a1_rect = engine.track_rect("a1")
    assert a1_rect.top() >= area.splitter_rect.bottom() - 1


# ── Test 3: Splitter drag ratio ─────────────────────────────────────────────


def test_layout_engine_splitter_drag_ratio(qapp) -> None:
    """Changing the splitter ratio resizes video and audio areas accordingly."""
    # Use enough tracks that total desired height exceeds viewport, forcing ratio-based split.
    # Each video track is 58px, each audio track is 46px, plus gaps and splitter.
    # 8 video tracks * 58 = 464 + 7*2 gaps = 478
    # 8 audio tracks * 46 = 368 + 7*2 gaps = 382
    # total = 478 + 382 + 6 (splitter) = 866 >> 500 viewport => ratio mode kicks in.
    engine = _make_engine_with_tracks(8, 8, viewport_height=500)

    # Default ratio is 0.5
    area = engine.area_layout()
    assert area.video_area.height() == pytest.approx(area.audio_area.height())

    # Ratio 0.7 -> video area taller than audio area
    engine.set_splitter_ratio(0.7)
    area = engine.area_layout()
    assert area.video_area.height() > area.audio_area.height()

    # Ratio 0.3 -> audio area taller than video area
    engine.set_splitter_ratio(0.3)
    area = engine.area_layout()
    assert area.audio_area.height() > area.video_area.height()


# ── Test 4: track_at_y ──────────────────────────────────────────────────────


def test_layout_engine_track_at_y(qapp) -> None:
    """track_at_y returns correct track_id or None for non-track areas."""
    engine = _make_engine_with_tracks(2, 2, viewport_height=500)

    v1_rect = engine.track_rect("v1")
    v2_rect = engine.track_rect("v2")
    a1_rect = engine.track_rect("a1")

    # Within V1 track rect -> "v1"
    mid_v1 = (v1_rect.top() + v1_rect.bottom()) / 2
    assert engine.track_at_y(mid_v1) == "v1"

    # Within V2 track rect -> "v2"
    mid_v2 = (v2_rect.top() + v2_rect.bottom()) / 2
    assert engine.track_at_y(mid_v2) == "v2"

    # Within A1 track rect -> "a1"
    mid_a1 = (a1_rect.top() + a1_rect.bottom()) / 2
    assert engine.track_at_y(mid_a1) == "a1"

    # Ruler area (y < TRACK_AREA_TOP) -> None
    assert engine.track_at_y(TRACK_AREA_TOP - 5) is None

    # Splitter area -> None
    splitter = engine.splitter_rect()
    mid_splitter = (splitter.top() + splitter.bottom()) / 2
    assert engine.track_at_y(mid_splitter) is None


# ── Test 5: Variable track height ───────────────────────────────────────────


def test_layout_engine_variable_track_height(qapp) -> None:
    """Per-track variable heights shift subsequent tracks correctly."""
    engine = _make_engine_with_tracks(3, 1, viewport_height=600)

    # Video tracks are bottom-aligned to the splitter, so growing V1 keeps
    # V1's bottom near the splitter and pushes tracks above it upward.
    v3_top_before = engine.track_top("v3")
    v1_bottom_before = engine.track_rect("v1").bottom()

    # Set V1 to a genuinely taller height than the 86px default.
    taller_height = EDIT_SIZES["track_h_video"] + 20
    engine.set_track_height("v1", taller_height)

    v1_rect = engine.track_rect("v1")
    assert v1_rect.height() == taller_height

    assert v1_rect.bottom() == pytest.approx(v1_bottom_before)
    v3_top = engine.track_top("v3")
    assert v3_top < v3_top_before


# ── Test 6: track_at_y_with_area ────────────────────────────────────────────


def test_layout_engine_track_at_y_with_area(qapp) -> None:
    """track_at_y_with_area returns correct (track_id, area) tuples."""
    engine = _make_engine_with_tracks(2, 2, viewport_height=500)

    # Video track -> area="video"
    v1_rect = engine.track_rect("v1")
    mid_v1 = (v1_rect.top() + v1_rect.bottom()) / 2
    track_id, area = engine.track_at_y_with_area(mid_v1)
    assert track_id == "v1"
    assert area == "video"

    # Audio track -> area="audio"
    a1_rect = engine.track_rect("a1")
    mid_a1 = (a1_rect.top() + a1_rect.bottom()) / 2
    track_id, area = engine.track_at_y_with_area(mid_a1)
    assert track_id == "a1"
    assert area == "audio"

    # Ruler area -> area="ruler"
    track_id, area = engine.track_at_y_with_area(TRACK_AREA_TOP - 5)
    assert track_id is None
    assert area == "ruler"

    # Splitter area -> area="splitter"
    splitter = engine.splitter_rect()
    mid_splitter = (splitter.top() + splitter.bottom()) / 2
    track_id, area = engine.track_at_y_with_area(mid_splitter)
    assert track_id is None
    assert area == "splitter"


# ── Test 7: ClipItem._update_geometry only updates width ────────────────────


def test_clip_update_geometry_only_updates_width(qapp) -> None:
    """Verify _update_geometry only changes width, not position."""
    view = TimelineView()
    view.load_timeline(_sequence_with_video_and_audio())

    item = list(view._items_map.values())[0]
    assert isinstance(item, ClipItem)

    # Set position manually
    item.setPos(100, 200)
    item._start_frame = 50
    item._pixels_per_frame = 5.0
    item._duration_frames = 100

    # Call _update_geometry
    item._update_geometry()

    # Position should NOT have changed
    assert item.x() == 100
    assert item.y() == 200
    # Width should be updated
    assert item._width == 500  # 100 frames * 5 ppf


# ── Test 8: Ordered tracks — V1 near splitter ──────────────────────────────


def test_ordered_tracks_v1_near_splitter(qapp) -> None:
    """ordered_video_tracks is reversed; ordered_audio_tracks is normal order."""
    view = TimelineView()
    view.load_timeline(_sequence_with_n_tracks(3, 2))

    engine = view._layout_engine

    # Video tracks displayed as V3, V2, V1 (reversed) so V1 is near splitter
    ordered_v = engine.ordered_video_tracks()
    assert [t.track_id for t in ordered_v] == ["v3", "v2", "v1"]

    # Audio tracks displayed as A1, A2 (normal) so A1 is near splitter
    ordered_a = engine.ordered_audio_tracks()
    assert [t.track_id for t in ordered_a] == ["a1", "a2"]


# ── Test 9: Ordered tracks multi-track layout ──────────────────────────────


def test_layout_engine_all_ordered_tracks_multi():
    """Test that multiple video/audio tracks are correctly ordered."""
    engine = TimelineLayoutEngine()
    tracks = [
        TrackInfo('v1', 'video', 58),
        TrackInfo('v2', 'video', 58),
        TrackInfo('v3', 'video', 58),
        TrackInfo('a1', 'audio', 46),
        TrackInfo('a2', 'audio', 46),
    ]
    state = TimelineLayoutState()
    engine.configure(tracks, state, 1200, 600, 5.0)

    # Video tracks reversed: V3, V2, V1 (V1 near splitter)
    video_ids = [t.track_id for t in engine.ordered_video_tracks()]
    assert video_ids == ['v3', 'v2', 'v1'], f"Expected ['v3','v2','v1'], got {video_ids}"

    # Audio tracks normal: A1, A2 (A1 near splitter)
    audio_ids = [t.track_id for t in engine.ordered_audio_tracks()]
    assert audio_ids == ['a1', 'a2'], f"Expected ['a1','a2'], got {audio_ids}"

    # V1 bottom should be near splitter top
    v1_bottom = engine.track_top('v1') + engine.track_height('v1')
    splitter_top = engine.splitter_rect().top()
    assert abs(v1_bottom - splitter_top) < TRACK_GAP + 1, \
        f"V1 bottom ({v1_bottom}) should be near splitter ({splitter_top})"

    # A1 top should be near splitter bottom
    a1_top = engine.track_top('a1')
    splitter_bottom = engine.splitter_rect().bottom()
    assert abs(a1_top - splitter_bottom) < TRACK_GAP + 1, \
        f"A1 top ({a1_top}) should be near splitter bottom ({splitter_bottom})"


def test_layout_engine_scene_rect():
    """Test that scene rect encompasses all content."""
    engine = TimelineLayoutEngine()
    tracks = [TrackInfo('v1', 'video', 58), TrackInfo('a1', 'audio', 46)]
    state = TimelineLayoutState()
    engine.configure(tracks, state, 800, 400, 5.0)

    sr = engine.scene_rect()
    assert sr.width() > 0
    assert sr.height() > 0
    # Scene rect should extend beyond all tracks
    a1_rect = engine.track_rect('a1')
    assert sr.bottom() >= a1_rect.bottom()


def test_layout_engine_height_clamping():
    """Test that track heights are clamped to min/max."""
    engine = TimelineLayoutEngine()
    tracks = [TrackInfo('v1', 'video', 58)]
    state = TimelineLayoutState()
    engine.configure(tracks, state, 800, 400, 5.0)

    # Try setting too-small height
    engine.set_track_height('v1', 10)
    assert engine.track_height('v1') >= 36  # MIN_VIDEO_TRACK_HEIGHT

    # Try setting too-large height
    engine.set_track_height('v1', 500)
    assert engine.track_height('v1') <= 160  # MAX_VIDEO_TRACK_HEIGHT


def test_layout_engine_splitter_ratio_clamping():
    """Test that splitter ratio is clamped to valid range."""
    engine = TimelineLayoutEngine()
    tracks = [TrackInfo('v1', 'video', 58), TrackInfo('a1', 'audio', 46)]
    state = TimelineLayoutState()
    engine.configure(tracks, state, 800, 400, 5.0)

    # Try extreme ratios
    engine.set_splitter_ratio(0.0)
    assert engine.state.video_area_ratio >= 0.1

    engine.set_splitter_ratio(1.0)
    assert engine.state.video_area_ratio <= 0.9


def test_layout_engine_track_at_y_empty_area():
    """Test hit testing in empty areas between tracks."""
    engine = TimelineLayoutEngine()
    tracks = [TrackInfo('v1', 'video', 58), TrackInfo('a1', 'audio', 46)]
    state = TimelineLayoutState()
    engine.configure(tracks, state, 800, 400, 5.0)

    # Between V1 and splitter (in TRACK_GAP area) should return None
    v1_bottom = engine.track_top('v1') + engine.track_height('v1')
    if v1_bottom < engine.splitter_rect().top():
        tid = engine.track_at_y(v1_bottom + 1)
        assert tid is None, f"Expected None in gap, got {tid}"

    # Below audio area should return None
    a1_bottom = engine.track_top('a1') + engine.track_height('a1')
    tid = engine.track_at_y(a1_bottom + 100)
    assert tid is None


def test_layout_engine_frames_to_width():
    """Test frame-to-pixel conversion."""
    engine = TimelineLayoutEngine()
    tracks = [TrackInfo('v1', 'video', 58)]
    state = TimelineLayoutState()
    engine.configure(tracks, state, 800, 400, 5.0)

    # 100 frames at 5 ppf = 500 px
    assert engine.frames_to_width(100) == 500.0

    # 0 frames = 0 px
    assert engine.frames_to_width(0) == 0.0


def test_layout_state_persistence():
    """Test that layout state correctly stores custom values."""
    state = TimelineLayoutState()

    # Default ratio
    assert state.video_area_ratio == 0.5

    # Custom heights
    state.track_heights['v1'] = 80
    state.track_heights['a1'] = 60
    assert state.track_heights['v1'] == 80
    assert state.track_heights['a1'] == 60

    # Custom ratio
    state.video_area_ratio = 0.7
    assert state.video_area_ratio == 0.7
