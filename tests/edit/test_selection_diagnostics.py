"""P2-03: selection/link diagnostics — see why an edit carries other clips.

Contract (from the professional-gap audit):
- Selecting either member of a reciprocal linked pair lights BOTH clips'
  group borders (persistent `_linked_to_selection`), so the carried clip
  is visible before the edit, not only on hover.
- get_selection_diagnostics() reports per selected clip: track kind,
  lock state, reciprocal partner, and whether moves carry the partner
  (linked-selection toggle).
"""

from __future__ import annotations

from src.features.edit.core.timeline_v2 import (
    ClipInstance,
    EditTrack,
    Rational,
    TimelineSequence,
)
from src.features.edit.ui.timeline_view import TimelineView


def _linked_sequence(lock_v1: bool = False) -> TimelineSequence:
    video = ClipInstance(
        clip_id="v-clip",
        asset_id="asset-1",
        track_id="v1",
        timeline_start=0,
        timeline_duration=100,
        source_start=0,
        source_duration=100,
        linked_clip_id="a-clip",
    )
    audio = ClipInstance(
        clip_id="a-clip",
        asset_id="asset-1",
        track_id="a1",
        timeline_start=0,
        timeline_duration=100,
        source_start=0,
        source_duration=100,
        linked_clip_id="v-clip",
    )
    return TimelineSequence(
        timeline_id="seq-sel-diag",
        name="Sel Diag",
        fps=Rational(24, 1),
        width=1920,
        height=1080,
        video_tracks=(
            EditTrack(
                track_id="v1", name="V1", track_type="video", index=0,
                clips=(video,), is_locked=lock_v1,
            ),
        ),
        audio_tracks=(
            EditTrack(
                track_id="a1", name="A1", track_type="audio", index=0,
                clips=(audio,),
            ),
        ),
    )


def _view(qapp, sequence: TimelineSequence) -> TimelineView:
    view = TimelineView()
    view.load_timeline(sequence)
    return view


class TestLinkedToSelectionBadge:
    def test_selecting_one_member_lights_both_borders(self, qapp):
        view = _view(qapp, _linked_sequence())

        view.set_selection(
            view._selection.with_clip_selected(
                "v-clip", "v1", view._timeline.timeline_id, replace=True
            )
        )

        assert view._items_map["v-clip"]._linked_to_selection is True
        assert view._items_map["a-clip"]._linked_to_selection is True

    def test_clearing_selection_clears_badges(self, qapp):
        view = _view(qapp, _linked_sequence())
        view.set_selection(
            view._selection.with_clip_selected(
                "v-clip", "v1", view._timeline.timeline_id, replace=True
            )
        )

        view.set_selection(view._selection.with_clips_cleared()
                           if hasattr(view._selection, "with_clips_cleared")
                           else view._selection.__class__())

        assert view._items_map["v-clip"]._linked_to_selection is False
        assert view._items_map["a-clip"]._linked_to_selection is False

    def test_unlinked_selection_does_not_light_others(self, qapp):
        seq = _linked_sequence()
        # Break the reciprocal link on the video side.
        broken = seq.video_tracks[0].clips[0].with_linked_clip_id(None)
        seq = seq.with_track_updated(
            seq.video_tracks[0].with_clip_updated(broken)
        )
        view = _view(qapp, seq)

        view.set_selection(
            view._selection.with_clip_selected(
                "v-clip", "v1", view._timeline.timeline_id, replace=True
            )
        )

        assert view._items_map["v-clip"]._linked_to_selection is False


class TestSelectionDiagnostics:
    def test_reports_partner_and_moves_together(self, qapp):
        view = _view(qapp, _linked_sequence())
        view.set_selection(
            view._selection.with_clip_selected(
                "v-clip", "v1", view._timeline.timeline_id, replace=True
            )
        )

        diag = view.get_selection_diagnostics()

        assert len(diag.entries) == 1
        entry = diag.entries[0]
        assert entry.clip_id == "v-clip"
        assert entry.track_kind == "video"
        assert entry.track_locked is False
        assert entry.linked_clip_id == "a-clip"
        assert entry.linked_track_id == "a1"
        assert entry.moves_together is True
        assert diag.linked_selection_enabled is True
        assert diag.linked_entries == (entry,)

    def test_linked_selection_disabled_reports_moves_separately(self, qapp):
        view = _view(qapp, _linked_sequence())
        view._linked_selection_enabled = False
        view.set_selection(
            view._selection.with_clip_selected(
                "v-clip", "v1", view._timeline.timeline_id, replace=True
            )
        )

        diag = view.get_selection_diagnostics()

        # The partner is still reported (the link exists), but edits no
        # longer carry it.
        assert diag.entries[0].linked_clip_id == "a-clip"
        assert diag.entries[0].moves_together is False

    def test_locked_track_is_flagged(self, qapp):
        view = _view(qapp, _linked_sequence(lock_v1=True))
        view.set_selection(
            view._selection.with_clip_selected(
                "v-clip", "v1", view._timeline.timeline_id, replace=True
            )
        )

        diag = view.get_selection_diagnostics()

        assert diag.entries[0].track_locked is True
        assert diag.locked_entries == diag.entries

    def test_empty_selection_is_empty(self, qapp):
        view = _view(qapp, _linked_sequence())

        diag = view.get_selection_diagnostics()

        assert diag.entries == ()
        assert diag.to_dict()["entries"] == []
