"""FX-H03: atomic project+timeline transaction for generator clips.

Audit FX-H03: the generator drop handler created the asset/bin entry
DIRECTLY on the EditProject and then ran a separate AddClipCommand for
the timeline. Undo only removed the clip, leaving the asset + bin
reference as orphans — repeated drop/undo polluted the saved file with
unreferenced Adjustment/Solid assets.

This module provides the pure-data transaction the audit asks for
(``CreateGeneratorClipCommand`` in spirit): a single helper that, given
a project + timeline + drop target, returns a NEW project (with asset +
bin entry) AND the new clip, plus a paired undo helper that removes the
asset when its clip is gone. It is pure data (no UI), so the atomicity
contract is unit-testable without a Qt drop harness.

The UI drop handler (timeline_view._handle_generator_drop) will be
wired to this in a follow-up; this lands the testable contract first.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Optional

from src.features.edit.core.project import EditProject
from src.features.edit.core.timeline_v2 import ClipInstance, TimelineSequence


@dataclass(frozen=True)
class GeneratorClipTransaction:
    """Result of creating a generator clip atomically.

    ``project`` carries the new asset + bin entry; ``clip`` references it.
    Both must be applied together and reverted together (see
    ``undo_generator_clip_transaction``), so a drop/undo never leaves an
    orphan asset.
    """

    project: EditProject
    clip: ClipInstance
    asset_id: str


def build_generator_clip_transaction(
    project: EditProject,
    timeline: TimelineSequence,
    *,
    generator_type: str,
    track_id: str,
    timeline_start: int,
    timeline_duration: int,
    clip_id: str,
    color_rgb: tuple[float, float, float] = (0.0, 0.0, 0.0),
) -> GeneratorClipTransaction:
    """Atomically create the generator asset AND the referencing clip.

    Args:
        project: current EditProject (the asset/bin host).
        timeline: current timeline (only used for validation — the clip is
            returned, the caller adds it via AddClipCommand so the
            command stack records the timeline mutation).
        generator_type: ``"adjustment_clip"`` or ``"solid_color"``.
        track_id: target video track.
        timeline_start / timeline_duration: clip placement.
        clip_id: pre-generated clip id (so the UI can select it post-drop).
        color_rgb: solid colour (solid_color only).

    Returns:
        ``GeneratorClipTransaction`` holding the new project (asset + bin)
        and the new clip. The caller applies BOTH: set project := result,
        then AddClipCommand(result.clip).

    Raises:
        ValueError: unknown generator type, or track not in timeline.
    """
    if generator_type == "adjustment_clip":
        new_project, asset_id = project.create_adjustment_clip()
    elif generator_type == "solid_color":
        new_project, asset_id = project.create_solid_color_asset(color_rgb=color_rgb)
    else:
        raise ValueError(f"Unknown generator type: {generator_type}")

    if timeline.get_track_by_id(track_id) is None:
        raise ValueError(f"Track not found in timeline: {track_id}")

    clip = ClipInstance(
        clip_id=clip_id,
        asset_id=asset_id,
        track_id=track_id,
        timeline_start=timeline_start,
        timeline_duration=timeline_duration,
        source_start=0,
        source_duration=timeline_duration,
    )
    return GeneratorClipTransaction(project=new_project, clip=clip, asset_id=asset_id)


def undo_generator_clip_transaction(
    project: EditProject,
    asset_id: str,
    *,
    referenced_asset_ids: Optional[set[str]] = None,
) -> EditProject:
    """Remove a generator asset that was created by the transaction.

    Called when the clip is undone (removed from the timeline). The asset
    is only removed if NO remaining clip references it, so a legitimate
    second clip on the same asset is never orphaned. This is the audit's
    'delete unreferenced asset on undo' safety.

    Args:
        project: current EditProject.
        asset_id: the asset created by ``build_generator_clip_transaction``.
        referenced_asset_ids: the set of asset ids still referenced by any
            clip on the active timeline(s). When provided, the asset is kept
            if it appears here. When None, the asset is removed unconditionally
            (the caller has already confirmed the clip is gone).

    Returns:
        New EditProject with the asset + bin entry removed, or the input
        unchanged when the asset is still referenced (or never existed).
    """
    asset = project.assets.get(asset_id)
    if asset is None:
        return project

    # Only generator assets created by this transaction are eligible; guard
    # against accidentally stripping a real imported asset.
    if asset.media_type not in ("adjustment", "solid"):
        return project

    # If the caller told us which assets are still in use, keep this one
    # when it is referenced — never orphan a legitimate second clip.
    if referenced_asset_ids is not None and asset_id in referenced_asset_ids:
        return project

    return _remove_asset_and_bin_entry(project, asset_id)


# ── internals ────────────────────────────────────────────────────────

def _remove_asset_and_bin_entry(project: EditProject, asset_id: str) -> EditProject:
    """Return a new project with the asset and its bin reference removed."""
    new_assets = {k: v for k, v in project.assets.items() if k != asset_id}

    new_bins = dict(project.bins)
    for bin_id, bin_obj in list(new_bins.items()):
        asset_ids = getattr(bin_obj, "asset_ids", None)
        if asset_ids is not None and asset_id in asset_ids:
            new_bins[bin_id] = bin_obj.with_asset_removed(asset_id)

    import time
    return replace(project, 
        assets=new_assets,
        bins=new_bins,
        modified_at=time.time(),
        # AUD-16812：逐字段重建漏传 revision 会让 undo/GC 后的新实例取
        # 默认 0，破坏 TAEP P0 乐观锁的单调性与 AI plan base_revision
        # 一致性比较。
    )
