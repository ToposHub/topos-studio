"""
Scene Cut Subclips - Add Cuts to Media Pool Workflow.

This module implements the main workflow for adding detected scene cuts
to the media pool as virtual subclips. Virtual subclips reference the
original media file but define a specific source range.

Core Functions:
- create_subclip_asset: Convert SceneSubclipSpec to MediaAsset
- add_subclips_to_project: Main workflow for media pool integration
- check_existing_subclips: Detect duplicate additions
- find_parent_bin: Locate bin containing parent asset

Design Principles:
- Pure functions, no UI dependencies (per .clinerules)
- Complete type annotations (per .clinerules)
- Immutable data flow (EditProject returns new instances)
- No side effects, all mutations via project methods

Reference:
- docs/resolve_like_scene_cut_detection_implementation_plan_2026-06-04.md P6
"""

from __future__ import annotations

import uuid
import logging
from dataclasses import dataclass, replace
from typing import Any, Dict, List, Tuple, Optional

from src.features.edit.core.project import (
    EditProject,
    MediaAsset,
    ProjectBin,
)
from src.features.edit.core.scene_cut.models import (
    SceneSubclipSpec,
    SceneCutAnalysis,
)


logger = logging.getLogger(__name__)


# ================================================================
# Constants
# ================================================================

# Duplicate handling strategies
DUPLICATE_REPLACE = "replace"  # Replace existing subclips with new ones
DUPLICATE_APPEND = "append"    # Add new subclips alongside existing ones
DUPLICATE_CANCEL = "cancel"    # Cancel operation, don't add anything

# Scene-cut subclips are grouped in a child bin next to their parent asset.
SCENE_CUT_BIN_SUFFIX = " - 场景剪切"

# Subclip metadata keys
SUBCLIP_IS_SUBCLIP = "is_subclip"
SUBCLIP_PARENT_ASSET_ID = "parent_asset_id"
SUBCLIP_PARENT_PATH = "parent_path"
SUBCLIP_SOURCE_START = "source_start"
SUBCLIP_SOURCE_DURATION = "source_duration"
SUBCLIP_SOURCE_END = "source_end"
SUBCLIP_START_SECONDS = "start_seconds"
SUBCLIP_DURATION_SECONDS = "duration_seconds"
SUBCLIP_END_SECONDS = "end_seconds"
SUBCLIP_SCENE_INDEX = "scene_index"
SUBCLIP_ANALYSIS_ID = "scene_cut_analysis_id"
SUBCLIP_CONFIDENCE = "scene_cut_confidence"
SUBCLIP_KIND = "scene_cut_kind"
SUBCLIP_START_TIMECODE = "start_timecode"
SUBCLIP_END_TIMECODE = "end_timecode"
SUBCLIP_THUMB_FRAME = "thumbnail_source_frame"


# ================================================================
# Data Classes
# ================================================================

@dataclass(frozen=True)
class ExistingSubclipReport:
    """
    Report of existing subclips from the same analysis.

    Used to detect duplicate additions and decide on handling strategy.
    """

    analysis_id: str
    existing_subclip_ids: Tuple[str, ...]
    existing_subclip_names: Tuple[str, ...]
    parent_asset_id: str

    @property
    def has_existing(self) -> bool:
        """Check if existing subclips were found."""
        return len(self.existing_subclip_ids) > 0

    @property
    def count(self) -> int:
        """Count of existing subclips."""
        return len(self.existing_subclip_ids)


@dataclass(frozen=True)
class AddSubclipsResult:
    """
    Result of add_subclips_to_project operation.

    Contains the updated project, added assets, and operation metadata.
    """

    project: EditProject
    added_assets: Tuple[MediaAsset, ...]
    removed_assets: Tuple[MediaAsset, ...]  # For replace strategy
    added_asset_ids: Tuple[str, ...]
    bin_id: str
    analysis_id: str
    parent_asset_id: str
    duplicate_strategy: str
    warnings: Tuple[str, ...]

    @property
    def added_count(self) -> int:
        """Count of added subclips."""
        return len(self.added_assets)

    @property
    def removed_count(self) -> int:
        """Count of removed subclips (for replace strategy)."""
        return len(self.removed_assets)


# ================================================================
# Core Functions
# ================================================================

def create_subclip_asset(
    spec: SceneSubclipSpec,
    analysis_id: str,
    parent_asset: MediaAsset,
) -> MediaAsset:
    """
    Create a MediaAsset from a SceneSubclipSpec.

    The resulting asset is a virtual subclip that references the parent
    asset's file path but defines a specific source range via metadata.

    Args:
        spec: SceneSubclipSpec with source range information.
        analysis_id: ID of the scene cut analysis.
        parent_asset: Original media asset being subclipped.

    Returns:
        MediaAsset instance with is_subclip=True metadata.
    """
    # Generate new UUID for subclip (independent from parent)
    subclip_id = str(uuid.uuid4())

    # Build metadata with subclip information
    # Merge parent metadata first, then add subclip-specific fields
    metadata: Dict[str, Any] = {
        **parent_asset.metadata,
        SUBCLIP_IS_SUBCLIP: True,
        SUBCLIP_PARENT_ASSET_ID: spec.parent_asset_id,
        SUBCLIP_PARENT_PATH: spec.source_path,
        SUBCLIP_SOURCE_START: spec.source_start,
        SUBCLIP_SOURCE_DURATION: spec.source_duration,
        SUBCLIP_SOURCE_END: spec.source_end,
        SUBCLIP_START_SECONDS: spec.start_seconds,
        SUBCLIP_DURATION_SECONDS: spec.duration_seconds,
        SUBCLIP_END_SECONDS: spec.end_seconds,
        SUBCLIP_SCENE_INDEX: spec.scene_index,
        SUBCLIP_ANALYSIS_ID: analysis_id,
        SUBCLIP_CONFIDENCE: spec.detection_confidence,
        SUBCLIP_KIND: spec.detection_kind.value,
        SUBCLIP_START_TIMECODE: spec.start_timecode,
        SUBCLIP_END_TIMECODE: spec.end_timecode,
        SUBCLIP_THUMB_FRAME: spec.thumbnail_source_frame,
    }

    # Override duration/frames to reflect subclip range
    metadata["duration_seconds"] = spec.duration_seconds
    metadata["total_frames"] = spec.source_duration
    # Keep the full source length separately.  ``total_frames`` describes the
    # virtual clip after this override, but poster-frame extraction still seeks
    # into the parent file at ``thumbnail_source_frame``.
    parent_total_frames = parent_asset.metadata.get("total_frames")
    if parent_total_frames is not None:
        try:
            metadata["source_total_frames"] = max(
                int(parent_total_frames), spec.source_end
            )
        except (TypeError, ValueError):
            metadata["source_total_frames"] = spec.source_end
    else:
        metadata["source_total_frames"] = spec.source_end

    # Create subclip asset
    # Uses parent path (no new file created)
    # Inherits proxy_path and proxy_status from parent
    now = parent_asset.created_at  # Use parent creation time for ordering

    return MediaAsset(
        asset_id=subclip_id,
        path=spec.source_path,  # Same path as parent
        media_type="video",  # Always video (per plan section 4.1)
        display_name=spec.display_name,
        metadata=metadata,
        thumbnail_key=None,  # Will be set by thumbnail service
        proxy_path=parent_asset.proxy_path,
        proxy_status=parent_asset.proxy_status,
        is_offline=parent_asset.is_offline,
        color_metadata=parent_asset.color_metadata,
        created_at=now,
    )


def find_parent_bin(
    project: EditProject,
    parent_asset_id: str,
    target_bin_id: Optional[str] = None,
) -> Optional[str]:
    """
    Find the bin where subclips should be added.

    Priority:
    1. target_bin_id if specified
    2. Bin containing parent asset
    3. Root bin as fallback

    Args:
        project: EditProject to search.
        parent_asset_id: ID of the parent media asset.
        target_bin_id: Optional target bin ID override.

    Returns:
        Bin ID where subclips should be added, or None if project empty.
    """
    # Use specified target bin if valid
    if target_bin_id and target_bin_id in project.bins:
        return target_bin_id

    # Find bin containing parent asset
    for bin_id, bin_obj in project.bins.items():
        if parent_asset_id in bin_obj.asset_ids:
            return bin_id

    # Fallback to root bin
    if project.root_bin_id and project.root_bin_id in project.bins:
        return project.root_bin_id

    return None


def get_or_create_scene_cut_bin(
    project: EditProject,
    parent_asset: MediaAsset,
    target_bin_id: Optional[str] = None,
) -> tuple[EditProject, str]:
    """Get or create the bin used to group an asset's scene-cut subclips.

    The scene-cut bin is created as a child of the asset's containing bin.
    This keeps generated subclips out of the root asset list while keeping
    them close to their source asset. Existing bins with the same name and
    parent are reused so repeated detections do not create duplicate folders.

    Args:
        project: Project containing the parent asset.
        parent_asset: Original media asset being subclipped.
        target_bin_id: Optional bin to use as the parent of the scene-cut bin.

    Returns:
        Tuple of (possibly updated project, scene-cut bin ID).
    """
    container_bin_id = target_bin_id or find_parent_bin(
        project,
        parent_asset.asset_id,
    )
    if container_bin_id not in project.bins:
        container_bin_id = project.root_bin_id or None

    parent_name = parent_asset.display_name.strip() or parent_asset.asset_id
    bin_name = f"{parent_name}{SCENE_CUT_BIN_SUFFIX}"
    for bin_id, bin_obj in project.bins.items():
        if bin_obj.name == bin_name and bin_obj.parent_id == container_bin_id:
            return project, bin_id

    project = project.create_bin(bin_name, parent_id=container_bin_id)
    created_bin = next(
        bin_obj
        for bin_obj in project.bins.values()
        if bin_obj.name == bin_name and bin_obj.parent_id == container_bin_id
    )
    return project, created_bin.bin_id


def check_existing_subclips(
    project: EditProject,
    analysis_id: str,
    parent_asset_id: str,
) -> ExistingSubclipReport:
    """
    Check for existing subclips from the same analysis.

    Used to detect duplicate additions and decide on handling strategy.

    Args:
        project: EditProject to search.
        analysis_id: Scene cut analysis ID.
        parent_asset_id: Parent asset ID to filter by.

    Returns:
        ExistingSubclipReport with found subclips.
    """
    existing_ids: List[str] = []
    existing_names: List[str] = []

    for asset_id, asset in project.assets.items():
        # Check if asset is a subclip
        is_subclip = asset.metadata.get(SUBCLIP_IS_SUBCLIP, False)
        if not is_subclip:
            continue

        # Check if from same analysis
        asset_analysis_id = asset.metadata.get(SUBCLIP_ANALYSIS_ID, "")
        if asset_analysis_id != analysis_id:
            continue

        # Check if from same parent
        asset_parent_id = asset.metadata.get(SUBCLIP_PARENT_ASSET_ID, "")
        if asset_parent_id != parent_asset_id:
            continue

        existing_ids.append(asset_id)
        existing_names.append(asset.display_name)

    return ExistingSubclipReport(
        analysis_id=analysis_id,
        existing_subclip_ids=tuple(existing_ids),
        existing_subclip_names=tuple(existing_names),
        parent_asset_id=parent_asset_id,
    )


def remove_existing_subclips(
    project: EditProject,
    report: ExistingSubclipReport,
) -> Tuple[EditProject, Tuple[MediaAsset, ...]]:
    """
    Remove existing subclips from project (for replace strategy).

    Args:
        project: EditProject to modify.
        report: ExistingSubclipReport with subclips to remove.

    Returns:
        Tuple of (new EditProject, removed MediaAsset instances).
    """
    removed: List[MediaAsset] = []

    for asset_id in report.existing_subclip_ids:
        asset = project.get_asset_by_id(asset_id)
        if asset:
            removed.append(asset)
            project = project.remove_asset(asset_id)

    return project, tuple(removed)


def add_subclips_to_bin(
    project: EditProject,
    assets: Tuple[MediaAsset, ...],
    bin_id: str,
) -> EditProject:
    """
    Add subclip assets to a specific bin.

    Args:
        project: EditProject to modify.
        assets: Subclip MediaAsset instances to add.
        bin_id: Target bin ID.

    Returns:
        New EditProject with assets added to bin.
    """
    if not assets:
        return project

    # Get target bin
    target_bin = project.bins.get(bin_id)
    if target_bin is None:
        logger.warning(f"Target bin {bin_id} not found, using root bin")
        bin_id = project.root_bin_id
        target_bin = project.bins.get(bin_id)
        if target_bin is None:
            return project

    # Add each asset ID to bin
    new_bins: Dict[str, ProjectBin] = dict(project.bins)
    for asset in assets:
        target_bin = target_bin.with_asset_added(asset.asset_id)
    new_bins[bin_id] = target_bin

    # Add assets to project
    new_assets: Dict[str, MediaAsset] = dict(project.assets)
    for asset in assets:
        new_assets[asset.asset_id] = asset

    # Rebuild via dataclasses.replace so EVERY other field — including the
    # optimistic-locking `revision` — carries over. The previous manual
    # reconstruction silently reset revision to its default 0, breaking
    # base_revision consistency checks after "Add Cuts to Media Pool"
    # (AUD-18100).
    return replace(
        project,
        assets=new_assets,
        bins=new_bins,
    )


def add_subclips_to_project(
    project: EditProject,
    analysis: SceneCutAnalysis,
    specs: Tuple[SceneSubclipSpec, ...],
    parent_asset: MediaAsset,
    target_bin_id: Optional[str] = None,
    duplicate_strategy: str = DUPLICATE_APPEND,
) -> AddSubclipsResult:
    """
    Main workflow: Add scene cut subclips to media pool.

    This is the primary function for the "Add Cuts to Media Pool" button
    in the Scene Detect dialog. It:

    1. Checks for existing subclips from same analysis
    2. Handles duplicates according to strategy
    3. Creates MediaAsset instances for each scene
    4. Adds assets to project and bin

    Args:
        project: EditProject to modify.
        analysis: Scene cut analysis result.
        specs: SceneSubclipSpec tuples from generate_scene_subclips.
        parent_asset: Original media asset being subclipped.
        target_bin_id: Optional target bin ID (None = parent's bin or root).
        duplicate_strategy: How to handle existing subclips:
            - DUPLICATE_REPLACE: Remove old, add new
            - DUPLICATE_APPEND: Keep old, add new alongside
            - DUPLICATE_CANCEL: Return unchanged project

    Returns:
        AddSubclipsResult with updated project and metadata.

    Raises:
        ValueError: If parent_asset not found in project.
    """
    warnings: List[str] = []

    # Validate parent asset exists in project
    if parent_asset.asset_id not in project.assets:
        raise ValueError(
            f"Parent asset {parent_asset.asset_id} not found in project"
        )

    # Check for existing subclips
    existing_report = check_existing_subclips(
        project=project,
        analysis_id=analysis.analysis_id,
        parent_asset_id=parent_asset.asset_id,
    )

    # Handle duplicate strategy
    removed_assets: Tuple[MediaAsset, ...] = ()
    if existing_report.has_existing:
        if duplicate_strategy == DUPLICATE_CANCEL:
            logger.info(
                f"Cancelled add subclips: {existing_report.count} existing "
                f"subclips found for analysis {analysis.analysis_id}"
            )
            return AddSubclipsResult(
                project=project,  # Unchanged
                added_assets=(),
                removed_assets=(),
                added_asset_ids=(),
                bin_id=target_bin_id or project.root_bin_id,
                analysis_id=analysis.analysis_id,
                parent_asset_id=parent_asset.asset_id,
                duplicate_strategy=DUPLICATE_CANCEL,
                warnings=(f"Cancelled: {existing_report.count} existing subclips found",),
            )

        elif duplicate_strategy == DUPLICATE_REPLACE:
            logger.info(
                f"Replacing {existing_report.count} existing subclips "
                f"for analysis {analysis.analysis_id}"
            )
            project, removed_assets = remove_existing_subclips(
                project=project,
                report=existing_report,
            )
            warnings.append(
                f"Replaced {existing_report.count} existing subclips"
            )

        elif duplicate_strategy == DUPLICATE_APPEND:
            logger.info(
                f"Appending subclips alongside {existing_report.count} existing"
            )
            warnings.append(
                f"Added alongside {existing_report.count} existing subclips"
            )

    # Create/reuse a dedicated child bin instead of putting generated
    # subclips directly in the parent's bin or at the project root.
    project, bin_id = get_or_create_scene_cut_bin(
        project=project,
        parent_asset=parent_asset,
        target_bin_id=target_bin_id,
    )

    # Create MediaAsset instances
    added_assets: List[MediaAsset] = []
    for spec in specs:
        asset = create_subclip_asset(
            spec=spec,
            analysis_id=analysis.analysis_id,
            parent_asset=parent_asset,
        )
        added_assets.append(asset)

    # Add to project and bin
    project = add_subclips_to_bin(
        project=project,
        assets=tuple(added_assets),
        bin_id=bin_id,
    )

    logger.info(
        f"Added {len(added_assets)} subclips to bin {bin_id} "
        f"for analysis {analysis.analysis_id}"
    )

    return AddSubclipsResult(
        project=project,
        added_assets=tuple(added_assets),
        removed_assets=removed_assets,
        added_asset_ids=tuple(a.asset_id for a in added_assets),
        bin_id=bin_id,
        analysis_id=analysis.analysis_id,
        parent_asset_id=parent_asset.asset_id,
        duplicate_strategy=duplicate_strategy,
        warnings=tuple(warnings),
    )


# ================================================================
# Helper Functions
# ================================================================

def is_subclip(asset: MediaAsset) -> bool:
    """
    Check if a MediaAsset is a virtual subclip.

    Args:
        asset: MediaAsset to check.

    Returns:
        True if asset has is_subclip=True in metadata.
    """
    return asset.metadata.get(SUBCLIP_IS_SUBCLIP, False)


def get_parent_asset_id(asset: MediaAsset) -> Optional[str]:
    """
    Get parent asset ID from subclip metadata.

    Args:
        asset: Subclip MediaAsset.

    Returns:
        Parent asset ID if subclip, None otherwise.
    """
    if not is_subclip(asset):
        return None
    return asset.metadata.get(SUBCLIP_PARENT_ASSET_ID)


def get_subclip_source_range(asset: MediaAsset) -> Tuple[int, int]:
    """
    Get source range (start, duration) from subclip metadata.

    Args:
        asset: Subclip MediaAsset.

    Returns:
        Tuple of (source_start, source_duration) in frames.

    Raises:
        ValueError: If asset is not a valid subclip.
    """
    if not is_subclip(asset):
        raise ValueError(f"Asset {asset.asset_id} is not a subclip")

    source_start = asset.metadata.get(SUBCLIP_SOURCE_START, 0)
    source_duration = asset.metadata.get(SUBCLIP_SOURCE_DURATION, 0)

    return source_start, source_duration


def get_subclips_for_analysis(
    project: EditProject,
    analysis_id: str,
) -> Tuple[MediaAsset, ...]:
    """
    Get all subclips created from a specific analysis.

    Useful for cleanup/undo operations.

    Args:
        project: EditProject to search.
        analysis_id: Scene cut analysis ID.

    Returns:
        Tuple of MediaAsset instances from the analysis.
    """
    subclips: List[MediaAsset] = []

    for asset in project.assets.values():
        if is_subclip(asset):
            asset_analysis_id = asset.metadata.get(SUBCLIP_ANALYSIS_ID, "")
            if asset_analysis_id == analysis_id:
                subclips.append(asset)

    return tuple(subclips)


def remove_subclips_for_analysis(
    project: EditProject,
    analysis_id: str,
) -> Tuple[EditProject, Tuple[MediaAsset, ...]]:
    """
    Remove all subclips created from a specific analysis.

    Used for undo/cleanup operations.

    Args:
        project: EditProject to modify.
        analysis_id: Scene cut analysis ID.

    Returns:
        Tuple of (new EditProject, removed MediaAsset instances).
    """
    removed: List[MediaAsset] = []

    for asset in project.assets.values():
        if is_subclip(asset):
            asset_analysis_id = asset.metadata.get(SUBCLIP_ANALYSIS_ID, "")
            if asset_analysis_id == analysis_id:
                removed.append(asset)
                project = project.remove_asset(asset.asset_id)

    logger.info(f"Removed {len(removed)} subclips for analysis {analysis_id}")

    return project, tuple(removed)


def get_scene_index(asset: MediaAsset) -> int:
    """
    Get scene index from subclip metadata.

    Args:
        asset: Subclip MediaAsset.

    Returns:
        Scene index (1-based), or 0 if not a subclip.
    """
    if not is_subclip(asset):
        return 0
    return asset.metadata.get(SUBCLIP_SCENE_INDEX, 0)


def validate_subclip_for_timeline(asset: MediaAsset) -> Tuple[bool, str]:
    """
    Validate a subclip asset for timeline insertion.

    Checks that required metadata fields are present and valid.

    Args:
        asset: MediaAsset to validate.

    Returns:
        Tuple of (is_valid, error_message).
    """
    if not is_subclip(asset):
        return True, ""  # Regular assets are always valid

    # Check required metadata fields
    required_fields = [
        SUBCLIP_SOURCE_START,
        SUBCLIP_SOURCE_DURATION,
        SUBCLIP_PARENT_ASSET_ID,
        SUBCLIP_PARENT_PATH,
    ]

    for field in required_fields:
        if field not in asset.metadata:
            return False, f"Missing required field: {field}"

    # Validate ranges
    source_start = asset.metadata.get(SUBCLIP_SOURCE_START, 0)
    source_duration = asset.metadata.get(SUBCLIP_SOURCE_DURATION, 0)

    if source_start < 0:
        return False, f"Invalid source_start: {source_start}"

    if source_duration < 1:
        return False, f"Invalid source_duration: {source_duration}"

    return True, ""


# ================================================================
# Export
# ================================================================

__all__ = [
    # Constants
    "DUPLICATE_REPLACE",
    "DUPLICATE_APPEND",
    "DUPLICATE_CANCEL",
    "SUBCLIP_IS_SUBCLIP",
    "SUBCLIP_PARENT_ASSET_ID",
    "SUBCLIP_SOURCE_START",
    "SUBCLIP_SOURCE_DURATION",
    "SUBCLIP_ANALYSIS_ID",
    # Data classes
    "ExistingSubclipReport",
    "AddSubclipsResult",
    # Core functions
    "create_subclip_asset",
    "find_parent_bin",
    "get_or_create_scene_cut_bin",
    "check_existing_subclips",
    "remove_existing_subclips",
    "add_subclips_to_bin",
    "add_subclips_to_project",
    # Helper functions
    "is_subclip",
    "get_parent_asset_id",
    "get_subclip_source_range",
    "get_subclips_for_analysis",
    "remove_subclips_for_analysis",
    "get_scene_index",
    "validate_subclip_for_timeline",
]
