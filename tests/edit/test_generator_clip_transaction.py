"""FX-H03 regression: atomic generator-clip transaction (no orphan assets).

Audit FX-H03: the generator drop handler created the asset DIRECTLY on
the project and then ran a separate AddClipCommand for the timeline.
Undo only removed the clip, leaving the asset + bin reference as orphans
— repeated drop/undo polluted the saved file with unreferenced
Adjustment/Solid assets.

This test pins the pure-data transaction contract:
  - build_generator_clip_transaction creates asset + clip atomically;
  - undo_generator_clip_transaction removes the asset ONLY when no clip
    references it (so a legitimate second clip is never orphaned);
  - a non-generator asset is never stripped by mistake.
"""

from __future__ import annotations

import pytest

from src.features.edit.core.generator_clip_transaction import (
    build_generator_clip_transaction,
    undo_generator_clip_transaction,
)
from src.features.edit.core.project import EditProject, MediaAsset
from src.features.edit.core.timeline_v2 import (
    ClipInstance,
    EditTrack,
    TimelineSequence,
)
from src.shared.media.metadata import Rational

pytestmark = pytest.mark.edit_core


def _project_with_video_track() -> tuple[EditProject, TimelineSequence, str]:
    """Empty project + one empty video track; returns (project, seq, track_id)."""
    project = EditProject.create("Gen")
    track = EditTrack(track_id="v1", name="V1", track_type="video", index=0, clips=())
    seq = TimelineSequence(
        timeline_id="seq-1", name="Main",
        fps=Rational(24, 1), width=64, height=64, video_tracks=(track,),
    )
    return project, seq, "v1"


def _bin_asset_ids(project: EditProject) -> set[str]:
    """Flatten all asset ids referenced by every bin."""
    out: set[str] = set()
    for bin_obj in project.bins.values():
        out.update(getattr(bin_obj, "asset_ids", ()) or ())
    return out


class TestBuildTransaction:
    def test_adjustment_transaction_creates_asset_and_clip(self) -> None:
        project, seq, track_id = _project_with_video_track()
        before_assets = set(project.assets)
        txn = build_generator_clip_transaction(
            project, seq,
            generator_type="adjustment_clip",
            track_id=track_id, timeline_start=0, timeline_duration=120,
            clip_id="clip-1",
        )
        # New asset exists in the project and is referenced by the clip.
        assert txn.asset_id not in before_assets
        assert txn.asset_id in txn.project.assets
        assert txn.clip.asset_id == txn.asset_id
        assert txn.clip.clip_id == "clip-1"
        # Bin now references the new asset (no orphan-in-waiting).
        assert txn.asset_id in _bin_asset_ids(txn.project)

    def test_solid_transaction_carries_color(self) -> None:
        project, seq, track_id = _project_with_video_track()
        txn = build_generator_clip_transaction(
            project, seq,
            generator_type="solid_color",
            track_id=track_id, timeline_start=0, timeline_duration=60,
            clip_id="clip-s",
            color_rgb=(0.2, 0.4, 0.6),
        )
        asset = txn.project.assets[txn.asset_id]
        assert asset.media_type == "solid"

    def test_unknown_generator_type_raises(self) -> None:
        project, seq, track_id = _project_with_video_track()
        with pytest.raises(ValueError, match="Unknown generator type"):
            build_generator_clip_transaction(
                project, seq, generator_type="nope",
                track_id=track_id, timeline_start=0, timeline_duration=10,
                clip_id="x",
            )

    def test_unknown_track_raises(self) -> None:
        project, seq, _ = _project_with_video_track()
        with pytest.raises(ValueError, match="Track not found"):
            build_generator_clip_transaction(
                project, seq, generator_type="adjustment_clip",
                track_id="missing", timeline_start=0, timeline_duration=10,
                clip_id="x",
            )


class TestUndoTransaction:
    def test_undo_removes_unreferenced_asset(self) -> None:
        """FX-H03 core: after the clip is gone, the asset must be removed."""
        project, seq, track_id = _project_with_video_track()
        txn = build_generator_clip_transaction(
            project, seq, generator_type="adjustment_clip",
            track_id=track_id, timeline_start=0, timeline_duration=120,
            clip_id="clip-1",
        )
        # Clip is gone (empty reference set) → asset removed.
        undone = undo_generator_clip_transaction(
            txn.project, txn.asset_id, referenced_asset_ids=set(),
        )
        assert txn.asset_id not in undone.assets
        assert txn.asset_id not in _bin_asset_ids(undone)

    def test_undo_keeps_asset_when_clip_still_references_it(self) -> None:
        """A legitimate second clip on the same asset must not be orphaned."""
        project, seq, track_id = _project_with_video_track()
        txn = build_generator_clip_transaction(
            project, seq, generator_type="solid_color",
            track_id=track_id, timeline_start=0, timeline_duration=60,
            clip_id="clip-1",
        )
        # The asset is still referenced by a clip on the timeline.
        undone = undo_generator_clip_transaction(
            txn.project, txn.asset_id,
            referenced_asset_ids={txn.asset_id},
        )
        assert txn.asset_id in undone.assets

    def test_undo_default_removes_when_called_without_refs(self) -> None:
        """When the caller omits referenced_asset_ids, the asset is removed
        (they have confirmed the clip is gone). This is the simple undo path."""
        project, seq, track_id = _project_with_video_track()
        txn = build_generator_clip_transaction(
            project, seq, generator_type="adjustment_clip",
            track_id=track_id, timeline_start=0, timeline_duration=10,
            clip_id="c",
        )
        undone = undo_generator_clip_transaction(txn.project, txn.asset_id)
        assert txn.asset_id not in undone.assets

    def test_undo_never_strips_non_generator_asset(self) -> None:
        """A real imported asset must never be removed by this helper."""
        project, seq, _ = _project_with_video_track()
        real = MediaAsset(
            asset_id="real-1", path="/tmp/real.mp4", media_type="video",
            display_name="Real", metadata={},
        )
        project_with_real = EditProject(
            project_id=project.project_id, name=project.name,
            assets={**project.assets, "real-1": real},
            bins=project.bins, timelines=project.timelines,
            compositions=project.compositions, color_stacks=project.color_stacks,
            comp_clips=project.comp_clips,
            render_cache_manifests=project.render_cache_manifests,
            render_cache_settings=project.render_cache_settings,
            root_bin_id=project.root_bin_id,
            active_timeline_id=project.active_timeline_id,
            active_comp_clip_id=project.active_comp_clip_id,
            created_at=project.created_at, modified_at=project.modified_at,
        )
        undone = undo_generator_clip_transaction(project_with_real, "real-1")
        assert "real-1" in undone.assets

    def test_undo_unknown_asset_is_noop(self) -> None:
        project, seq, _ = _project_with_video_track()
        undone = undo_generator_clip_transaction(project, "does-not-exist")
        assert undone is project
