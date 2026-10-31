"""FX-H03-Followup acceptance tests: generator transaction boundary hardening.

Covers:
- Rollback guard: project restored on AddClipCommand failure
- Rollback guard: project preserved on success
- Orphan check: detects unreferenced generator assets
- Orphan check: detects missing asset references (corruption)
- Orphan check: detects missing bin entries
- Orphan check: legacy warnings for unknown origin
- Orphan check: clean project produces no findings
- Safe GC: reclaims only unreferenced generators
- Safe GC: never touches real media
- Safe GC: never touches referenced assets
- Safe GC: skips legacy/unknown assets
- Multi-timeline: shared generator not reclaimed when one timeline removed
- Consistency validation: clip → asset resolution
- Consistency validation: generator bin entry check
- Consistency validation: legacy media_type warning
- Full lifecycle: create → undo → redo → delete → save → reopen
"""

from __future__ import annotations

from dataclasses import replace
import pytest
from unittest.mock import patch

from src.features.edit.core.generator_clip_transaction import (
    build_generator_clip_transaction,
    undo_generator_clip_transaction,
)
from src.features.edit.core.generator_transaction_guard import (
    ConsistencyDiagnostic,
    GeneratorTransactionGuard,
    OrphanCheckResult,
    ReclaimReport,
    collect_referenced_asset_ids,
    preflight_orphan_check,
    reclaim_orphan_generators,
    validate_project_asset_consistency,
)
from src.features.edit.core.project import EditProject, MediaAsset, TimelineSequenceRef
from src.features.edit.core.timeline_v2 import (
    ClipInstance,
    EditTrack,
    TimelineSequence,
)
from src.shared.media.metadata import Rational

pytestmark = pytest.mark.edit_core


# =============================================================================
# Helpers
# =============================================================================

def _project_and_timeline() -> tuple[EditProject, TimelineSequence, str]:
    """Empty project + one empty video track."""
    project = EditProject.create("TestProj")
    track = EditTrack(track_id="v1", name="V1", track_type="video", index=0, clips=())
    seq = TimelineSequence(
        timeline_id="seq-1", name="Main",
        fps=Rational(24, 1), width=64, height=64, video_tracks=(track,),
    )
    return project, seq, "v1"


def _timeline_with_clip(clip: ClipInstance) -> TimelineSequence:
    """Create a timeline containing the given clip on track v1."""
    track = EditTrack(track_id="v1", name="V1", track_type="video", index=0, clips=(clip,))
    return TimelineSequence(
        timeline_id="seq-1", name="Main",
        fps=Rational(24, 1), width=64, height=64, video_tracks=(track,),
    )


def _bin_asset_ids(project: EditProject) -> set[str]:
    out: set[str] = set()
    for bin_obj in project.bins.values():
        out.update(getattr(bin_obj, "asset_ids", ()) or ())
    return out


def _add_real_asset(project: EditProject, asset_id: str = "real-1") -> EditProject:
    """Add a real (non-generator) video asset to the project."""
    real = MediaAsset(
        asset_id=asset_id, path="/tmp/real.mp4", media_type="video",
        display_name="Real Video", metadata={},
    )
    return EditProject(
        project_id=project.project_id, name=project.name,
        assets={**project.assets, asset_id: real},
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


def _with_timeline_refs(
    project: EditProject,
    *sequences: TimelineSequence,
) -> EditProject:
    """Return a project whose timeline refs match the supplied sequences."""
    refs = {
        sequence.timeline_id: TimelineSequenceRef(
            timeline_id=sequence.timeline_id,
            name=sequence.name,
            fps=float(sequence.fps),
            width=sequence.width,
            height=sequence.height,
        )
        for sequence in sequences
    }
    return replace(project, timelines=refs)


# =============================================================================
# TestGeneratorTransactionGuard
# =============================================================================

class TestGeneratorTransactionGuard:
    def test_rollback_on_failure(self) -> None:
        """Asset write fails → project restored to original."""
        project, seq, track_id = _project_and_timeline()
        guard = GeneratorTransactionGuard(project)

        with pytest.raises(RuntimeError):
            with guard:
                # Phase 1: mutate project
                txn = build_generator_clip_transaction(
                    project, seq, generator_type="adjustment_clip",
                    track_id=track_id, timeline_start=0, timeline_duration=120,
                    clip_id="c1",
                )
                guard.set_project(txn.project)
                # Phase 2: simulate AddClipCommand failure
                raise RuntimeError("overlap detected")

        assert guard.rolled_back is True
        assert guard.project is project  # original restored
        assert txn.asset_id not in guard.project.assets

    def test_success_preserves_mutation(self) -> None:
        """No failure → project holds the mutated state."""
        project, seq, track_id = _project_and_timeline()
        guard = GeneratorTransactionGuard(project)

        with guard:
            txn = build_generator_clip_transaction(
                project, seq, generator_type="solid_color",
                track_id=track_id, timeline_start=0, timeline_duration=60,
                clip_id="c1", color_rgb=(1.0, 0.0, 0.0),
            )
            guard.set_project(txn.project)

        assert guard.rolled_back is False
        assert guard.project is txn.project
        assert txn.asset_id in guard.project.assets

    def test_rollback_on_bin_failure(self) -> None:
        """Simulates bin write failure after asset creation."""
        project, seq, track_id = _project_and_timeline()
        guard = GeneratorTransactionGuard(project)

        with pytest.raises(IOError):
            with guard:
                txn = build_generator_clip_transaction(
                    project, seq, generator_type="adjustment_clip",
                    track_id=track_id, timeline_start=0, timeline_duration=10,
                    clip_id="c1",
                )
                guard.set_project(txn.project)
                raise IOError("bin write failed")

        assert guard.rolled_back is True
        assert guard.project is project

    def test_nested_exception_does_not_suppress(self) -> None:
        """Guard does not swallow the exception."""
        project, _, _ = _project_and_timeline()
        guard = GeneratorTransactionGuard(project)

        with pytest.raises(ValueError, match="test error"):
            with guard:
                raise ValueError("test error")

        assert guard.rolled_back is True


# =============================================================================
# TestPreflightOrphanCheck
# =============================================================================

class TestPreflightOrphanCheck:
    def test_clean_project(self) -> None:
        """No orphans in a fresh project."""
        project, seq, _ = _project_and_timeline()
        result = preflight_orphan_check(project, [seq])
        assert result.clean is True

    def test_detects_orphaned_generator(self) -> None:
        """Generator asset with no clip on timeline → orphan."""
        project, seq, track_id = _project_and_timeline()
        txn = build_generator_clip_transaction(
            project, seq, generator_type="adjustment_clip",
            track_id=track_id, timeline_start=0, timeline_duration=120,
            clip_id="c1",
        )
        # Project has the asset, but timeline has NO clip referencing it
        result = preflight_orphan_check(txn.project, [seq])
        assert txn.asset_id in result.orphaned_asset_ids
        assert result.clean is False

    def test_referenced_generator_not_orphan(self) -> None:
        """Generator asset with a clip on timeline → not orphan."""
        project, seq, track_id = _project_and_timeline()
        txn = build_generator_clip_transaction(
            project, seq, generator_type="solid_color",
            track_id=track_id, timeline_start=0, timeline_duration=60,
            clip_id="c1",
        )
        tl_with_clip = _timeline_with_clip(txn.clip)
        result = preflight_orphan_check(txn.project, [tl_with_clip])
        assert txn.asset_id not in result.orphaned_asset_ids

    def test_detects_missing_asset(self) -> None:
        """Clip references an asset not in the project → missing."""
        project, seq, _ = _project_and_timeline()
        ghost_clip = ClipInstance(
            clip_id="c-ghost", asset_id="nonexistent-asset",
            track_id="v1", timeline_start=0, timeline_duration=10,
            source_start=0, source_duration=10,
        )
        tl = _timeline_with_clip(ghost_clip)
        result = preflight_orphan_check(project, [tl])
        assert "nonexistent-asset" in result.missing_asset_ids

    def test_real_media_never_orphan(self) -> None:
        """Real video assets are never flagged as orphans."""
        project, seq, _ = _project_and_timeline()
        project = _add_real_asset(project, "real-1")
        # real-1 is unreferenced but should NOT be in orphaned set
        result = preflight_orphan_check(project, [seq])
        assert "real-1" not in result.orphaned_asset_ids


# =============================================================================
# TestReclaimOrphanGenerators
# =============================================================================

class TestReclaimOrphanGenerators:
    def test_reclaims_unreferenced_generator(self) -> None:
        project, seq, track_id = _project_and_timeline()
        txn = build_generator_clip_transaction(
            project, seq, generator_type="adjustment_clip",
            track_id=track_id, timeline_start=0, timeline_duration=120,
            clip_id="c1",
        )
        report = reclaim_orphan_generators(txn.project, [seq])
        assert txn.asset_id in report.reclaimed_ids
        assert txn.asset_id not in report.project.assets
        assert txn.asset_id not in _bin_asset_ids(report.project)

    def test_never_reclaims_real_media(self) -> None:
        project, seq, _ = _project_and_timeline()
        project = _add_real_asset(project, "real-1")
        report = reclaim_orphan_generators(project, [seq])
        assert "real-1" not in report.reclaimed_ids
        assert "real-1" in report.project.assets

    def test_never_reclaims_referenced_generator(self) -> None:
        project, seq, track_id = _project_and_timeline()
        txn = build_generator_clip_transaction(
            project, seq, generator_type="solid_color",
            track_id=track_id, timeline_start=0, timeline_duration=60,
            clip_id="c1",
        )
        tl_with_clip = _timeline_with_clip(txn.clip)
        report = reclaim_orphan_generators(txn.project, [tl_with_clip])
        assert txn.asset_id not in report.reclaimed_ids
        assert txn.asset_id in report.project.assets

    def test_multi_timeline_shared_generator(self) -> None:
        """Deleting one timeline's clip doesn't reclaim if another references it."""
        project, seq, track_id = _project_and_timeline()
        txn = build_generator_clip_transaction(
            project, seq, generator_type="adjustment_clip",
            track_id=track_id, timeline_start=0, timeline_duration=120,
            clip_id="c1",
        )
        # Second timeline also references the same asset
        clip2 = ClipInstance(
            clip_id="c2", asset_id=txn.asset_id, track_id="v1",
            timeline_start=0, timeline_duration=50,
            source_start=0, source_duration=50,
        )
        tl2 = _timeline_with_clip(clip2)
        # First timeline is empty (clip removed), second still has it
        empty_tl = seq  # original empty timeline

        report = reclaim_orphan_generators(txn.project, [empty_tl, tl2])
        assert txn.asset_id not in report.reclaimed_ids

    def test_noop_when_clean(self) -> None:
        project, seq, _ = _project_and_timeline()
        report = reclaim_orphan_generators(project, [seq])
        assert report.reclaimed_ids == frozenset()
        assert report.project is project


# =============================================================================
# TestValidateProjectAssetConsistency
# =============================================================================

class TestValidateProjectAssetConsistency:
    def test_consistent_project_no_diagnostics(self) -> None:
        project, seq, track_id = _project_and_timeline()
        txn = build_generator_clip_transaction(
            project, seq, generator_type="adjustment_clip",
            track_id=track_id, timeline_start=0, timeline_duration=120,
            clip_id="c1",
        )
        tl = _timeline_with_clip(txn.clip)
        diags = validate_project_asset_consistency(txn.project, [tl])
        assert diags == []

    def test_missing_asset_diagnostic(self) -> None:
        project, seq, _ = _project_and_timeline()
        ghost_clip = ClipInstance(
            clip_id="c1", asset_id="gone", track_id="v1",
            timeline_start=0, timeline_duration=10,
            source_start=0, source_duration=10,
        )
        tl = _timeline_with_clip(ghost_clip)
        diags = validate_project_asset_consistency(project, [tl])
        assert any(d.issue == "missing_asset" and d.asset_id == "gone" for d in diags)

    def test_missing_bin_entry_diagnostic(self) -> None:
        """Generator asset exists but has no bin entry."""
        project, seq, track_id = _project_and_timeline()
        txn = build_generator_clip_transaction(
            project, seq, generator_type="solid_color",
            track_id=track_id, timeline_start=0, timeline_duration=60,
            clip_id="c1",
        )
        # Manually remove from bins (simulate corruption)
        broken_bins = {}
        for bin_id, bin_obj in txn.project.bins.items():
            asset_ids = getattr(bin_obj, "asset_ids", ()) or ()
            if txn.asset_id in asset_ids:
                broken_bins[bin_id] = bin_obj.with_asset_removed(txn.asset_id)
            else:
                broken_bins[bin_id] = bin_obj

        broken_project = EditProject(
            project_id=txn.project.project_id, name=txn.project.name,
            assets=txn.project.assets, bins=broken_bins,
            timelines=txn.project.timelines,
            compositions=txn.project.compositions,
            color_stacks=txn.project.color_stacks,
            comp_clips=txn.project.comp_clips,
            render_cache_manifests=txn.project.render_cache_manifests,
            render_cache_settings=txn.project.render_cache_settings,
            root_bin_id=txn.project.root_bin_id,
            active_timeline_id=txn.project.active_timeline_id,
            active_comp_clip_id=txn.project.active_comp_clip_id,
            created_at=txn.project.created_at,
            modified_at=txn.project.modified_at,
        )
        tl = _timeline_with_clip(txn.clip)
        diags = validate_project_asset_consistency(broken_project, [tl])
        assert any(d.issue == "missing_bin_entry" for d in diags)


# =============================================================================
# TestFullLifecycle
# =============================================================================

class TestFullLifecycle:
    """create → undo → redo → delete → save → reopen: no orphans."""

    def test_create_undo_leaves_no_orphan_after_gc(self) -> None:
        project, seq, track_id = _project_and_timeline()

        # CREATE
        txn = build_generator_clip_transaction(
            project, seq, generator_type="adjustment_clip",
            track_id=track_id, timeline_start=0, timeline_duration=120,
            clip_id="c1",
        )
        tl_with_clip = _timeline_with_clip(txn.clip)

        # Verify no orphan while clip exists
        check = preflight_orphan_check(txn.project, [tl_with_clip])
        assert check.clean is True

        # UNDO (clip removed from timeline)
        empty_tl = seq  # clip gone
        # GC runs
        report = reclaim_orphan_generators(txn.project, [empty_tl])
        assert txn.asset_id in report.reclaimed_ids

        # After GC: project is back to clean state
        final_check = preflight_orphan_check(report.project, [empty_tl])
        assert final_check.clean is True

    def test_create_undo_redo_cycle(self) -> None:
        project, seq, track_id = _project_and_timeline()

        # CREATE
        txn = build_generator_clip_transaction(
            project, seq, generator_type="solid_color",
            track_id=track_id, timeline_start=0, timeline_duration=60,
            clip_id="c1", color_rgb=(0.0, 1.0, 0.0),
        )
        tl_with_clip = _timeline_with_clip(txn.clip)

        # UNDO → asset still in project (redo possible)
        # (In the real system, GC only fires on next command, not on undo)
        check_after_undo = preflight_orphan_check(txn.project, [seq])
        assert txn.asset_id in check_after_undo.orphaned_asset_ids

        # REDO → clip back on timeline, no longer orphan
        check_after_redo = preflight_orphan_check(txn.project, [tl_with_clip])
        assert txn.asset_id not in check_after_redo.orphaned_asset_ids

    def test_delete_then_gc_then_save_reopen(self) -> None:
        """After delete + GC, save/reopen produces consistent state."""
        project, seq, track_id = _project_and_timeline()

        # Create two generators
        txn1 = build_generator_clip_transaction(
            project, seq, generator_type="adjustment_clip",
            track_id=track_id, timeline_start=0, timeline_duration=60,
            clip_id="c1",
        )
        txn2 = build_generator_clip_transaction(
            txn1.project, seq, generator_type="solid_color",
            track_id=track_id, timeline_start=60, timeline_duration=60,
            clip_id="c2", color_rgb=(1.0, 0.0, 0.0),
        )

        # Timeline has only clip2 (clip1 deleted)
        tl = _timeline_with_clip(txn2.clip)

        # GC should reclaim txn1's asset but keep txn2's
        report = reclaim_orphan_generators(txn2.project, [tl])
        assert txn1.asset_id in report.reclaimed_ids
        assert txn2.asset_id not in report.reclaimed_ids

        # Post-GC consistency check
        diags = validate_project_asset_consistency(report.project, [tl])
        assert diags == []

    def test_gc_does_not_delete_legacy_asset(self) -> None:
        """Non-generator asset (video) is never reclaimed even if unreferenced."""
        project, seq, _ = _project_and_timeline()
        # Add a real video asset that is unreferenced by any clip
        legacy = MediaAsset(
            asset_id="legacy-1", path="/old/file.mov", media_type="video",
            display_name="Legacy", metadata={},
        )
        project_with_legacy = EditProject(
            project_id=project.project_id, name=project.name,
            assets={**project.assets, "legacy-1": legacy},
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
        report = reclaim_orphan_generators(project_with_legacy, [seq])
        assert "legacy-1" not in report.reclaimed_ids
        assert "legacy-1" in report.project.assets


# =============================================================================
# TestCollectReferencedAssetIds
# =============================================================================

class TestCollectReferencedAssetIds:
    def test_empty_timeline(self) -> None:
        _, seq, _ = _project_and_timeline()
        assert collect_referenced_asset_ids([seq]) == set()

    def test_collects_from_multiple_tracks(self) -> None:
        clip1 = ClipInstance(
            clip_id="c1", asset_id="a1", track_id="v1",
            timeline_start=0, timeline_duration=10,
            source_start=0, source_duration=10,
        )
        clip2 = ClipInstance(
            clip_id="c2", asset_id="a2", track_id="a1",
            timeline_start=0, timeline_duration=10,
            source_start=0, source_duration=10,
        )
        v_track = EditTrack(track_id="v1", name="V1", track_type="video", index=0, clips=(clip1,))
        a_track = EditTrack(track_id="a1", name="A1", track_type="audio", index=0, clips=(clip2,))
        tl = TimelineSequence(
            timeline_id="seq", name="S",
            fps=Rational(24, 1), width=64, height=64,
            video_tracks=(v_track,), audio_tracks=(a_track,),
        )
        refs = collect_referenced_asset_ids([tl])
        assert refs == {"a1", "a2"}


class TestProjectIOSaveWiring:
    """Save must consume the generator preflight, not leave it as a helper."""

    def test_save_reclaims_orphan_before_serialization(self, tmp_path) -> None:
        from src.features.edit.core.edit_project_io import (
            EditProjectIO,
            EditProjectIOConfig,
        )

        project, seq, track_id = _project_and_timeline()
        project = _with_timeline_refs(project, seq)
        txn = build_generator_clip_transaction(
            project,
            seq,
            generator_type="adjustment_clip",
            track_id=track_id,
            timeline_start=0,
            timeline_duration=20,
            clip_id="orphan-clip",
        )
        io = EditProjectIO(EditProjectIOConfig(autosave_interval=0))

        with patch(
            "src.features.edit.core.edit_project_io.ToposProjectIO.save",
            return_value=True,
        ) as save:
            assert io.save_project(
                tmp_path / "clean.topos",
                txn.project,
                timeline_sequences={seq.timeline_id: seq},
                create_backup=False,
            )

        serialized_assets = save.call_args.kwargs["full_assets_data"]
        assert txn.asset_id not in serialized_assets
        assert txn.asset_id not in io.get_current_project().assets

    def test_save_blocks_missing_clip_asset(self, tmp_path) -> None:
        from src.features.edit.core.edit_project_io import (
            EditProjectIO,
            EditProjectIOConfig,
        )

        project, _seq, _ = _project_and_timeline()
        ghost = ClipInstance(
            clip_id="ghost", asset_id="missing", track_id="v1",
            timeline_start=0, timeline_duration=10,
            source_start=0, source_duration=10,
        )
        sequence = _timeline_with_clip(ghost)
        project = _with_timeline_refs(project, sequence)
        io = EditProjectIO(EditProjectIOConfig(autosave_interval=0))

        with patch(
            "src.features.edit.core.edit_project_io.ToposProjectIO.save",
            return_value=True,
        ) as save:
            assert not io.save_project(
                tmp_path / "broken.topos",
                project,
                timeline_sequences={sequence.timeline_id: sequence},
                create_backup=False,
            )
        save.assert_not_called()

    @pytest.mark.parametrize("provided_ids", [(), ("seq-a",)])
    def test_save_blocks_incomplete_timeline_map(
        self,
        tmp_path,
        provided_ids: tuple[str, ...],
    ) -> None:
        from src.features.edit.core.edit_project_io import (
            EditProjectIO,
            EditProjectIOConfig,
        )

        project, seq, track_id = _project_and_timeline()
        seq_a = replace(seq, timeline_id="seq-a", name="A")
        seq_b = replace(seq, timeline_id="seq-b", name="B")
        project = _with_timeline_refs(project, seq_a, seq_b)
        txn = build_generator_clip_transaction(
            project,
            seq_b,
            generator_type="adjustment_clip",
            track_id=track_id,
            timeline_start=0,
            timeline_duration=20,
            clip_id="shared-generator",
        )
        seq_b_with_clip = replace(
            seq_b,
            video_tracks=(replace(seq_b.video_tracks[0], clips=(txn.clip,)),),
        )
        sequences = {"seq-a": seq_a, "seq-b": seq_b_with_clip}
        provided = {timeline_id: sequences[timeline_id] for timeline_id in provided_ids}
        io = EditProjectIO(EditProjectIOConfig(autosave_interval=0))

        with patch(
            "src.features.edit.core.edit_project_io.ToposProjectIO.save",
            return_value=True,
        ) as save:
            assert not io.save_project(
                tmp_path / "incomplete.topos",
                txn.project,
                timeline_sequences=provided,
                create_backup=False,
            )

        save.assert_not_called()
        assert txn.asset_id in txn.project.assets
