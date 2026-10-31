"""FX-H03 wiring regression: orphan generator assets are GC'd.

The transaction helper (undo_generator_clip_transaction) existed but was
never called at runtime, so undoing a generator-clip drop left the asset
+ bin entry as an orphan. This test pins the runtime GC wiring:
``_gc_orphan_generator_assets`` removes unreferenced generator assets
(adjustment/solid) and leaves everything else (referenced generators,
real imported assets) untouched.

We bypass TimelineView.__init__ (needs a full Qt widget) and stub just
the attributes the GC reads: _project (EditProject) and _timeline.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from dataclasses import replace

from src.features.edit.core.project import (
    EditProject,
    MediaAsset,
    TimelineSequenceRef,
)
from src.features.edit.core.timeline_v2 import (
    ClipInstance,
    EditTrack,
    TimelineSequence,
)
from src.features.edit.core.generator_clip_transaction import (
    build_generator_clip_transaction,
)
from src.features.edit.ui.timeline_view import TimelineView
from src.shared.media.metadata import Rational

pytestmark = pytest.mark.edit_core


def _seq_with_clips(asset_ids: list[str]) -> TimelineSequence:
    clips = tuple(
        ClipInstance(
            clip_id=f"c-{aid}", asset_id=aid, track_id="v1",
            timeline_start=i * 10, timeline_duration=10,
            source_start=0, source_duration=10,
        )
        for i, aid in enumerate(asset_ids)
    )
    track = EditTrack(track_id="v1", name="V1", track_type="video", index=0, clips=clips)
    return TimelineSequence(
        timeline_id="seq", name="M", fps=Rational(24, 1),
        width=8, height=8, video_tracks=(track,),
    )


def _project_with_assets(assets: dict[str, str]) -> EditProject:
    """Build a project where assets maps id -> media_type."""
    project = EditProject.create("GC")
    asset_objs = {
        aid: MediaAsset(
            asset_id=aid, path=f"topos://g/{aid}" if mtype in ("adjustment", "solid") else f"/tmp/{aid}.mp4",
            media_type=mtype, display_name=aid, metadata={},
        )
        for aid, mtype in assets.items()
    }
    return EditProject(
        project_id=project.project_id, name=project.name,
        assets=asset_objs, bins=project.bins,
        timelines=project.timelines, compositions=project.compositions,
        color_stacks=project.color_stacks, comp_clips=project.comp_clips,
        render_cache_manifests=project.render_cache_manifests,
        render_cache_settings=project.render_cache_settings,
        root_bin_id=project.root_bin_id,
        active_timeline_id=project.active_timeline_id,
        active_comp_clip_id=project.active_comp_clip_id,
        created_at=project.created_at, modified_at=project.modified_at,
    )


def _make_view(project, timeline) -> TimelineView:
    view = TimelineView.__new__(TimelineView)
    view._project = project
    view._timeline = timeline
    view.project_changed = MagicMock()
    return view


class TestGcOrphanGeneratorAssets:
    def test_removes_unreferenced_generator_asset(self) -> None:
        """An adjustment asset with no clip referencing it is removed."""
        project = _project_with_assets({
            "adj-orphan": "adjustment",
            "adj-used": "adjustment",
        })
        timeline = _seq_with_clips(["adj-used"])  # only adj-used is on timeline
        view = _make_view(project, timeline)

        view._gc_orphan_generator_assets()

        assert "adj-orphan" not in view._project.assets
        assert "adj-used" in view._project.assets

    def test_keeps_referenced_generator_asset(self) -> None:
        """A generator asset still referenced by a clip is kept."""
        project = _project_with_assets({"sol-1": "solid"})
        timeline = _seq_with_clips(["sol-1"])
        view = _make_view(project, timeline)

        view._gc_orphan_generator_assets()

        assert "sol-1" in view._project.assets

    def test_never_removes_real_imported_asset(self) -> None:
        """An unreferenced real (video) asset must NOT be GC'd — only
        adjustment/solid generator assets are eligible."""
        project = _project_with_assets({
            "vid-orphan": "video",
            "adj-orphan": "adjustment",
        })
        timeline = _seq_with_clips([])  # nothing on timeline
        view = _make_view(project, timeline)

        view._gc_orphan_generator_assets()

        assert "vid-orphan" in view._project.assets, "real asset must never be GC'd"
        assert "adj-orphan" not in view._project.assets

    def test_no_project_is_noop(self) -> None:
        view = TimelineView.__new__(TimelineView)
        view._project = None
        view._timeline = None
        # Must not raise.
        view._gc_orphan_generator_assets()

    def test_emits_project_changed_when_cleaned(self) -> None:
        project = _project_with_assets({"adj-orphan": "adjustment"})
        timeline = _seq_with_clips([])
        view = _make_view(project, timeline)

        view._gc_orphan_generator_assets()

        view.project_changed.emit.assert_called_once()

    def test_multi_timeline_project_never_uses_active_only_gc(self) -> None:
        """Active-view GC must not delete an asset another sequence may use."""
        project = _project_with_assets({"shared-generator": "adjustment"})
        project = replace(
            project,
            timelines={
                "seq-a": TimelineSequenceRef("seq-a", "A"),
                "seq-b": TimelineSequenceRef("seq-b", "B"),
            },
        )
        active_timeline = _seq_with_clips([])
        view = _make_view(project, active_timeline)

        view._gc_orphan_generator_assets()

        assert "shared-generator" in view._project.assets
        view.project_changed.emit.assert_not_called()


class TestGeneratorDropCommitWiring:
    def _transaction(self):
        project = EditProject.create("Drop")
        sequence = _seq_with_clips([])
        txn = build_generator_clip_transaction(
            project,
            sequence,
            generator_type="adjustment_clip",
            track_id="v1",
            timeline_start=0,
            timeline_duration=20,
            clip_id="stable-generator-clip",
        )
        return project, txn

    def test_add_clip_failure_leaves_project_unchanged(self) -> None:
        project, txn = self._transaction()
        view = TimelineView.__new__(TimelineView)
        view._project = project
        view._execute_command = MagicMock(side_effect=ValueError("overlap"))
        view.project_changed = MagicMock()

        with pytest.raises(ValueError, match="overlap"):
            view._commit_generator_clip_transaction(
                txn.project,
                txn.clip,
                "v1",
            )

        assert view._project is project
        view.project_changed.emit.assert_not_called()

    def test_success_consumes_transaction_clip_without_new_id(self) -> None:
        _project, txn = self._transaction()
        view = TimelineView.__new__(TimelineView)
        view._project = _project
        view._execute_command = MagicMock()
        view.project_changed = MagicMock()

        view._commit_generator_clip_transaction(txn.project, txn.clip, "v1")

        command = view._execute_command.call_args.args[0]
        assert command.clip.clip_id == "stable-generator-clip"
        assert command.clip is txn.clip
        assert view._project is txn.project
        view.project_changed.emit.assert_called_once_with(txn.project)
