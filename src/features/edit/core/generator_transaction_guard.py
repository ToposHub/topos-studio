"""FX-H03-Followup: Generator transaction boundary hardening.

Extends the FX-H03 atomic transaction with:

1. **Rollback guard** — a context manager that captures the original
   project state and restores it if the timeline command (AddClipCommand)
   fails after the project has been mutated. This closes the two-phase
   commit gap where the asset exists but the clip was never placed.

2. **Save-time orphan check** — a read-only scan that detects generator
   assets (adjustment/solid) with no clip reference on any timeline.
   Called before save so the user can be warned (or auto-cleaned).

3. **Post-load consistency validation** — verifies that every clip's
   asset_id resolves to a real asset, every generator asset has a bin
   entry, and legacy projects missing metadata get a recoverable
   diagnostic rather than a silent failure.

4. **Safe GC policy** — reclaims only assets that are unambiguously
   generator-created (media_type in adjustment/solid) AND unreferenced
   by any clip on any timeline. Never touches real media, referenced
   assets, or legacy assets whose origin cannot be confirmed.

Design constraints:
  - Pure data, no Qt/GL imports (unit-testable headlessly).
  - Never mutates input objects — all functions return new instances.
  - GC is conservative: when in doubt, keep the asset.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, FrozenSet, List, Sequence, Set

from src.features.edit.core.project import EditProject
from src.features.edit.core.timeline_v2 import TimelineSequence

logger = logging.getLogger(__name__)


# =============================================================================
# Rollback guard
# =============================================================================

class GeneratorTransactionGuard:
    """Context manager ensuring project rollback on timeline command failure.

    Usage::

        guard = GeneratorTransactionGuard(original_project)
        try:
            with guard:
                # Phase 1: mutate project (add asset + bin entry)
                project = txn.project
                guard.set_project(project)
                # Phase 2: execute timeline command (may raise)
                new_timeline = add_clip_command.apply(timeline)
        except Exception:
            # guard.rolled_back is True; guard.project is original_project
            project = guard.project
            raise

    If the body completes without exception, ``guard.project`` holds the
    final (mutated) project. If an exception propagates, ``guard.project``
    is restored to the original and ``guard.rolled_back`` is True.
    """

    def __init__(self, original_project: EditProject) -> None:
        self._original = original_project
        self._current = original_project
        self._rolled_back = False

    @property
    def project(self) -> EditProject:
        """The current project state (original if rolled back)."""
        return self._current

    @property
    def rolled_back(self) -> bool:
        """Whether a rollback occurred."""
        return self._rolled_back

    def set_project(self, project: EditProject) -> None:
        """Update the working project (called after phase 1 mutation)."""
        self._current = project

    def __enter__(self) -> "GeneratorTransactionGuard":
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> bool:
        if exc_type is not None:
            # Rollback: restore original project state.
            self._current = self._original
            self._rolled_back = True
            logger.warning(
                "FX-H03: generator transaction rolled back due to: %s",
                exc_val,
            )
        # Do NOT suppress the exception — let the caller handle it.
        return False


# =============================================================================
# Orphan check
# =============================================================================

@dataclass(frozen=True)
class OrphanCheckResult:
    """Result of a read-only orphan scan across project + timelines.

    Attributes:
        orphaned_asset_ids: Generator assets with no clip reference on any
            timeline. Safe to reclaim.
        missing_asset_ids: Clip asset_ids that don't resolve to any asset
            in the project. Indicates data corruption or incomplete save.
        missing_bin_entries: Generator assets that exist in project.assets
            but have no bin reference. Indicates a partial transaction.
        legacy_warnings: Assets whose origin cannot be confirmed (no
            media_type or unknown type). Not safe to auto-reclaim.
        clean: True when no issues found.
    """

    orphaned_asset_ids: FrozenSet[str] = frozenset()
    missing_asset_ids: FrozenSet[str] = frozenset()
    missing_bin_entries: FrozenSet[str] = frozenset()
    legacy_warnings: FrozenSet[str] = frozenset()

    @property
    def clean(self) -> bool:
        return not (self.orphaned_asset_ids or self.missing_asset_ids
                    or self.missing_bin_entries or self.legacy_warnings)


def collect_referenced_asset_ids(timelines: Sequence[TimelineSequence]) -> Set[str]:
    """Scan all clips on all timelines and collect their asset_ids."""
    referenced: Set[str] = set()
    for tl in timelines:
        for track in tl.all_tracks:
            for clip in track.clips:
                if clip.asset_id:
                    referenced.add(clip.asset_id)
    return referenced


def _collect_bin_asset_ids(project: EditProject) -> Set[str]:
    """Flatten all asset ids referenced by every bin in the project."""
    out: Set[str] = set()
    for bin_obj in project.bins.values():
        asset_ids = getattr(bin_obj, "asset_ids", None)
        if asset_ids:
            out.update(asset_ids)
    return out


# Generator-eligible media types (created by build_generator_clip_transaction)
_GENERATOR_MEDIA_TYPES = frozenset({"adjustment", "solid"})


def preflight_orphan_check(
    project: EditProject,
    timelines: Sequence[TimelineSequence],
) -> OrphanCheckResult:
    """Read-only save-time scan for orphaned/inconsistent generator assets.

    This function never mutates anything. It produces a diagnostic the UI
    can present to the user (or use to auto-clean before save).

    Args:
        project: The EditProject to scan.
        timelines: All timelines in the project (typically just the active
            one, but multi-timeline projects should pass all).

    Returns:
        OrphanCheckResult with categorized findings.
    """
    referenced = collect_referenced_asset_ids(timelines)
    bin_ids = _collect_bin_asset_ids(project)

    orphaned: Set[str] = set()
    missing_assets: Set[str] = set()
    missing_bins: Set[str] = set()
    legacy: Set[str] = set()

    # Check each asset in the project
    for asset_id, asset in project.assets.items():
        media_type = getattr(asset, "media_type", "")

        # Only generator-eligible types can be orphans
        if media_type in _GENERATOR_MEDIA_TYPES:
            if asset_id not in referenced:
                orphaned.add(asset_id)
            if asset_id not in bin_ids:
                missing_bins.add(asset_id)
        elif not media_type:
            # No media_type — legacy asset, cannot confirm origin
            if asset_id not in referenced:
                legacy.add(asset_id)

    # Check each referenced asset resolves to a real asset
    for asset_id in referenced:
        if asset_id not in project.assets:
            missing_assets.add(asset_id)

    return OrphanCheckResult(
        orphaned_asset_ids=frozenset(orphaned),
        missing_asset_ids=frozenset(missing_assets),
        missing_bin_entries=frozenset(missing_bins),
        legacy_warnings=frozenset(legacy),
    )


# =============================================================================
# Safe GC (reclaim orphaned generators)
# =============================================================================

@dataclass(frozen=True)
class ReclaimReport:
    """Report of what the GC reclaimed.

    Attributes:
        reclaimed_ids: Asset IDs that were removed.
        project: The new project with orphans removed.
        skipped_legacy: IDs skipped because origin is unconfirmable.
        skipped_referenced: IDs skipped because still referenced.
    """

    reclaimed_ids: FrozenSet[str]
    project: EditProject
    skipped_legacy: FrozenSet[str] = frozenset()
    skipped_referenced: FrozenSet[str] = frozenset()


def reclaim_orphan_generators(
    project: EditProject,
    timelines: Sequence[TimelineSequence],
) -> ReclaimReport:
    """Remove unreferenced generator assets from the project.

    Conservative policy:
      - Only removes assets with media_type in ("adjustment", "solid").
      - Only removes assets NOT referenced by any clip on any timeline.
      - Never removes real media (video/audio/image).
      - Never removes assets whose origin cannot be confirmed (legacy).
      - Logs every removal for audit trail.

    Args:
        project: The EditProject to clean.
        timelines: All timelines to scan for references.

    Returns:
        ReclaimReport with the new project and details of what was done.
    """
    check = preflight_orphan_check(project, timelines)

    if not check.orphaned_asset_ids:
        return ReclaimReport(
            reclaimed_ids=frozenset(),
            project=project,
            skipped_legacy=check.legacy_warnings,
        )

    # Remove orphans one by one (reusing the existing undo helper logic)
    from src.features.edit.core.generator_clip_transaction import (
        undo_generator_clip_transaction,
    )

    current = project
    reclaimed: Set[str] = set()
    for asset_id in check.orphaned_asset_ids:
        new_project = undo_generator_clip_transaction(
            current, asset_id, referenced_asset_ids=set(),
        )
        if new_project is not current:
            reclaimed.add(asset_id)
            current = new_project

    if reclaimed:
        logger.info(
            "FX-H03 GC: reclaimed %d orphaned generator asset(s): %s",
            len(reclaimed), sorted(reclaimed),
        )

    return ReclaimReport(
        reclaimed_ids=frozenset(reclaimed),
        project=current,
        skipped_legacy=check.legacy_warnings,
    )


# =============================================================================
# Post-load consistency validation
# =============================================================================

@dataclass(frozen=True)
class ConsistencyDiagnostic:
    """One consistency issue found during post-load validation.

    Attributes:
        asset_id: The asset involved.
        issue: Machine-readable issue code.
        detail: Human-readable description.
        recoverable: Whether the issue can be auto-fixed.
    """

    asset_id: str
    issue: str
    detail: str
    recoverable: bool = True


def validate_project_asset_consistency(
    project: EditProject,
    timelines: Sequence[TimelineSequence],
) -> List[ConsistencyDiagnostic]:
    """Post-load validation of asset/clip/bin/metadata consistency.

    Checks:
      1. Every clip's asset_id resolves to a real asset in the project.
      2. Every generator asset has a bin entry.
      3. Generator assets have valid media_type metadata.
      4. Legacy projects missing metadata get a recoverable diagnostic.

    Returns an empty list when everything is consistent.
    """
    diagnostics: List[ConsistencyDiagnostic] = []
    bin_ids = _collect_bin_asset_ids(project)

    # Check 1: clip → asset resolution
    referenced = collect_referenced_asset_ids(timelines)
    for asset_id in referenced:
        if asset_id not in project.assets:
            diagnostics.append(ConsistencyDiagnostic(
                asset_id=asset_id,
                issue="missing_asset",
                detail=f"Clip references asset '{asset_id}' which is not in the project",
                recoverable=False,  # data loss — cannot auto-fix
            ))

    # Check 2 & 3: generator asset integrity
    for asset_id, asset in project.assets.items():
        media_type = getattr(asset, "media_type", "")

        if media_type in _GENERATOR_MEDIA_TYPES:
            # Must have a bin entry
            if asset_id not in bin_ids:
                diagnostics.append(ConsistencyDiagnostic(
                    asset_id=asset_id,
                    issue="missing_bin_entry",
                    detail=f"Generator asset '{asset_id}' ({media_type}) has no bin entry",
                    recoverable=True,  # can re-add to root bin
                ))
        elif not media_type:
            # Legacy asset with no type — warn but don't block
            diagnostics.append(ConsistencyDiagnostic(
                asset_id=asset_id,
                issue="legacy_no_media_type",
                detail=f"Asset '{asset_id}' has no media_type (legacy project?)",
                recoverable=True,
            ))

    return diagnostics
