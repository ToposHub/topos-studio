"""
Scene Cut Apply Operations - Marker 和 Split 操作.

This module provides pure functions for applying scene cut detection results
to the timeline. Operations include:
- Creating TimelineMarker from accepted candidates
- Splitting clips at hard cut positions
- Syncing linked audio/video clips
- All operations wrapped in MacroCommand for single undo step

Design Principles:
- Pure functions, no UI dependencies
- Input validation before generating commands
- Split from right to left to avoid position drift
- Preserve all clip attributes (effects, color_stack, composite, fades, animated_params)
- Linked clips split synchronously
- Single MacroCommand for atomic undo/redo

Key Implementation Details:
- Split points must satisfy: clip.timeline_start < frame < clip.timeline_end
- Right-to-left split order prevents position drift (earlier splits don't affect later clips)
- Linked audio/video split uses same timeline frame for both
- Clip attributes preserved: effects, color_stack_id, composite_id, fade_in/out, animated_params
- Conflict detection checks: locked tracks, disabled clips, out-of-range splits, transitions

Operations:
1. apply_markers: Add markers at accepted candidate positions
2. apply_splits: Split clips at accepted hard cut positions
3. apply_all: Markers + splits in single macro command
4. detect_conflicts: Pre-check for issues that would prevent apply

Reference:
- docs/resolve_like_scene_cut_detection_implementation_plan_2026-06-04.md P7
- .clinerules: Pure functions, complete type annotations
"""

from __future__ import annotations

import logging
import uuid
from typing import Optional, Dict, Any, List, Tuple
from dataclasses import dataclass

from src.features.edit.core.timeline_v2 import (
    TimelineSequence,
    ClipInstance,
    TimelineMarker,
)
from src.features.edit.core.commands import (
    SplitClipCommand,
    AddMarkerCommand,
    MacroCommand,
)
from src.features.edit.core.scene_cut.models import (
    SceneCutCandidate,
    SceneCutKind,
)


logger = logging.getLogger(__name__)


# ================================================================
# Constants
# ================================================================

# Marker colors for different cut types
MARKER_COLOR_HARD_CUT = "#50C880"  # Green
MARKER_COLOR_SOFT_TRANSITION = "#B478C8"  # Purple
MARKER_COLOR_BLACK_GAP = "#505060"  # Dark gray
MARKER_COLOR_WARNING = "#FF7828"  # Orange

# Default marker name prefix
MARKER_NAME_PREFIX = "Scene Cut"


# ================================================================
# Data Classes
# ================================================================

@dataclass(frozen=True)
class ApplyMarkersResult:
    """Result of apply_markers operation."""

    commands: Tuple[AddMarkerCommand, ...]
    markers: Tuple[TimelineMarker, ...]
    count: int


@dataclass(frozen=True)
class ApplySplitsResult:
    """Result of apply_splits operation."""

    commands: Tuple[SplitClipCommand, ...]
    split_positions: Tuple[int, ...]
    split_count: int
    linked_split_count: int
    warnings: Tuple[str, ...]


@dataclass(frozen=True)
class ApplyAllResult:
    """Result of apply_all operation (markers + splits)."""

    macro_command: MacroCommand
    markers: Tuple[TimelineMarker, ...]
    split_positions: Tuple[int, ...]
    marker_count: int
    split_count: int
    linked_split_count: int
    warnings: Tuple[str, ...]


@dataclass(frozen=True)
class ConflictReport:
    """Report of conflicts preventing apply."""

    locked_tracks: Tuple[str, ...]
    disabled_clips: Tuple[str, ...]
    out_of_range_splits: Tuple[int, ...]
    duplicate_splits: Tuple[int, ...]
    transition_conflicts: Tuple[str, ...]
    messages: Tuple[str, ...]

    @property
    def has_conflicts(self) -> bool:
        return bool(
            self.locked_tracks
            or self.disabled_clips
            or self.out_of_range_splits
            or self.duplicate_splits
            or self.transition_conflicts
        )


# ================================================================
# Apply Markers
# ================================================================

def apply_markers(
    candidates: Tuple[SceneCutCandidate, ...],
    timeline: TimelineSequence,
    include_pending: bool = False,
    marker_name_prefix: str = MARKER_NAME_PREFIX,
) -> ApplyMarkersResult:
    """
    Create TimelineMarker commands from accepted candidates.

    Args:
        candidates: Scene cut candidates from detection
        timeline: Current timeline (for validation)
        include_pending: Whether to include pending candidates
        marker_name_prefix: Prefix for marker names

    Returns:
        ApplyMarkersResult with commands and markers
    """
    commands: List[AddMarkerCommand] = []
    markers: List[TimelineMarker] = []

    for cand in candidates:
        # Skip rejected candidates
        if cand.is_rejected:
            continue

        # Skip pending unless include_pending
        if cand.is_pending and not include_pending:
            continue

        # Use timeline_frame if available, else source_frame
        # 审计修复：timeline_frame == 0 是合法帧（旧 `or` 会错误回退到
        # 绝对源帧，source_start>0 或变速时错位）
        frame = (cand.timeline_frame if cand.timeline_frame is not None
                 else cand.source_frame)

        # Determine marker color based on kind
        color = _get_marker_color_for_kind(cand.kind)

        # Create marker name
        name = f"{marker_name_prefix} {cand.source_frame}"
        if cand.kind != SceneCutKind.HARD_CUT:
            name += f" ({cand.kind.value})"

        # Create marker
        marker = TimelineMarker(
            marker_id=str(uuid.uuid4()),
            frame=frame,
            name=name,
            color=color,
            comment=f"Score: {cand.score:.2f}, Conf: {cand.confidence:.2f}"
        )

        # Create command
        cmd = AddMarkerCommand.create(marker)

        markers.append(marker)
        commands.append(cmd)

    return ApplyMarkersResult(
        commands=tuple(commands),
        markers=tuple(markers),
        count=len(commands)
    )


def _get_marker_color_for_kind(kind: SceneCutKind) -> str:
    """Get marker color based on cut kind."""
    colors = {
        SceneCutKind.HARD_CUT: MARKER_COLOR_HARD_CUT,
        SceneCutKind.SOFT_TRANSITION: MARKER_COLOR_SOFT_TRANSITION,
        SceneCutKind.BLACK_GAP: MARKER_COLOR_BLACK_GAP,
        SceneCutKind.FLASH: MARKER_COLOR_WARNING,
        SceneCutKind.FAST_MOTION: MARKER_COLOR_WARNING,
        SceneCutKind.UNKNOWN: MARKER_COLOR_WARNING,
    }
    return colors.get(kind, MARKER_COLOR_HARD_CUT)


# ================================================================
# Apply Splits
# ================================================================

def apply_splits(
    candidates: Tuple[SceneCutCandidate, ...],
    timeline: TimelineSequence,
    target_clip_id: str,
    target_track_id: str,
    split_linked: bool = True,
    hard_cut_only: bool = True,
) -> ApplySplitsResult:
    """
    Create SplitClipCommand commands from accepted hard cut candidates.

    Key rules:
    - Split from right to left to avoid position drift
    - Only accepted candidates are split
    - hard_cut_only: Only HARD_CUT kind, skip transitions
    - split_linked: Sync linked audio/video clips

    Args:
        candidates: Scene cut candidates
        timeline: Current timeline
        target_clip_id: Clip ID to split
        target_track_id: Track ID containing the clip
        split_linked: Whether to split linked audio/video
        hard_cut_only: Only split hard cuts, not transitions

    Returns:
        ApplySplitsResult with commands and warnings
    """
    commands: List[SplitClipCommand] = []
    split_positions: List[int] = []
    warnings: List[str] = []
    linked_split_count = 0

    # Get target track and clip
    track = timeline.get_track_by_id(target_track_id)
    if track is None:
        warnings.append(f"Track not found: {target_track_id}")
        return ApplySplitsResult(
            commands=(),
            split_positions=(),
            split_count=0,
            linked_split_count=0,
            warnings=tuple(warnings)
        )

    clip = track.get_clip_by_id(target_clip_id)
    if clip is None:
        warnings.append(f"Clip not found: {target_clip_id}")
        return ApplySplitsResult(
            commands=(),
            split_positions=(),
            split_count=0,
            linked_split_count=0,
            warnings=tuple(warnings)
        )

    # Check track lock
    if track.is_locked:
        warnings.append(f"Track {target_track_id} is locked")
        return ApplySplitsResult(
            commands=(),
            split_positions=(),
            split_count=0,
            linked_split_count=0,
            warnings=tuple(warnings)
        )

    # Check clip enabled
    if not clip.is_enabled:
        warnings.append(f"Clip {target_clip_id} is disabled")
        return ApplySplitsResult(
            commands=(),
            split_positions=(),
            split_count=0,
            linked_split_count=0,
            warnings=tuple(warnings)
        )

    # Filter candidates for valid split positions
    valid_splits = _filter_split_candidates(
        candidates=candidates,
        clip=clip,
        timeline=timeline,
        hard_cut_only=hard_cut_only
    )

    # Deduplicate candidates that collide on the same timeline frame.
    # Source→timeline mapping (cross-fps / speed rounding) is not
    # injective, so two accepted cuts can land on one frame; a second
    # SplitClipCommand at an already-split boundary raises ValueError and
    # rolls back the whole macro (AUD-17801). Keep the highest-confidence
    # candidate per frame, mirroring detect_conflicts' duplicate_splits
    # handling.
    splits_by_frame: dict[int, SceneCutCandidate] = {}
    for cand in valid_splits:
        frame = cand.timeline_frame
        if frame is None:
            continue
        existing = splits_by_frame.get(frame)
        if existing is None or cand.confidence > existing.confidence:
            splits_by_frame[frame] = cand
    if len(splits_by_frame) < len(valid_splits):
        removed = len(valid_splits) - len(splits_by_frame)
        warnings.append(
            f"Merged {removed} duplicate split position(s) "
            f"(kept highest confidence per timeline frame)"
        )

    # Sort split positions (right to left for stability)
    # This prevents earlier splits from affecting later positions
    sorted_splits = sorted(
        splits_by_frame.values(), key=lambda x: x.timeline_frame, reverse=True
    )

    # Generate split commands (right to left)
    for cand in sorted_splits:
        frame = cand.timeline_frame
        if frame is None:
            continue

        # Create split command for video clip
        split_cmd = SplitClipCommand.create(
            track_id=target_track_id,
            clip_id=target_clip_id,
            split_frame=frame
        )
        commands.append(split_cmd)
        split_positions.append(frame)

        # Handle linked audio split
        if split_linked and clip.linked_clip_id:
            linked_result = _split_linked_clip(
                timeline=timeline,
                linked_clip_id=clip.linked_clip_id,
                split_frame=frame
            )
            if linked_result:
                commands.append(linked_result)
                linked_split_count += 1
            else:
                warnings.append(f"Linked clip {clip.linked_clip_id} split failed")

    return ApplySplitsResult(
        commands=tuple(commands),
        split_positions=tuple(split_positions),  # Keep right-to-left order (descending)
        split_count=len(split_positions),
        linked_split_count=linked_split_count,
        warnings=tuple(warnings)
    )


def _filter_split_candidates(
    candidates: Tuple[SceneCutCandidate, ...],
    clip: ClipInstance,
    timeline: TimelineSequence,
    hard_cut_only: bool
) -> List[SceneCutCandidate]:
    """
    Filter candidates for valid split positions.

    Validation:
    - Candidate is accepted
    - Timeline frame is inside clip range
    - Not at clip boundary (start or end)
    - Kind is HARD_CUT if hard_cut_only
    """
    valid: List[SceneCutCandidate] = []

    for cand in candidates:
        # Must be accepted
        if not cand.is_accepted:
            continue

        # Must have timeline_frame
        if cand.timeline_frame is None:
            continue

        frame = cand.timeline_frame

        # Must be inside clip range
        if not (clip.timeline_start < frame < clip.timeline_end):
            continue

        # hard_cut_only filter
        if hard_cut_only and cand.kind != SceneCutKind.HARD_CUT:
            continue

        valid.append(cand)

    return valid


def _split_linked_clip(
    timeline: TimelineSequence,
    linked_clip_id: str,
    split_frame: int
) -> Optional[SplitClipCommand]:
    """
    Create split command for linked audio clip.

    The linked clip (typically audio) is split at the same timeline frame
    as the video clip. All attributes are preserved (fade, color, effects, etc.).

    Args:
        timeline: Current timeline
        linked_clip_id: Linked clip ID (audio)
        split_frame: Timeline frame to split at

    Returns:
        SplitClipCommand for linked clip, or None if invalid

    Note:
        SplitClipCommand.apply() handles attribute preservation automatically.
    """
    # Find linked clip
    for audio_track in timeline.audio_tracks:
        linked_clip = audio_track.get_clip_by_id(linked_clip_id)
        if linked_clip:
            # Check track lock
            if audio_track.is_locked:
                logger.warning(f"Linked audio track {audio_track.track_id} is locked")
                return None

            # Validate split position is inside linked clip
            if not (linked_clip.timeline_start < split_frame < linked_clip.timeline_end):
                logger.warning(
                    f"Split frame {split_frame} outside linked clip "
                    f"[{linked_clip.timeline_start}, {linked_clip.timeline_end})"
                )
                return None

            # Create split command - attributes preserved by SplitClipCommand.apply()
            return SplitClipCommand.create(
                track_id=audio_track.track_id,
                clip_id=linked_clip_id,
                split_frame=split_frame
            )

    logger.warning(f"Linked clip {linked_clip_id} not found in audio tracks")
    return None


# ================================================================
# Attribute Preservation Validation
# ================================================================

def verify_split_preserves_attributes(
    original_clip: ClipInstance,
    left_clip: ClipInstance,
    right_clip: ClipInstance,
) -> Tuple[bool, List[str]]:
    """
    Verify that split operation preserved all clip attributes.

    Checks:
    - effects preserved on both clips
    - color_stack_id preserved
    - color_shared preserved
    - composite_id preserved
    - composite_bypassed preserved
    - animated_params preserved
    - transform/crop/opacity preserved
    - speed preserved
    - fade_in_duration preserved on left
    - fade_out_duration preserved on right
    - FPS metadata preserved

    Args:
        original_clip: Clip before split
        left_clip: Left clip after split
        right_clip: Right clip after split

    Returns:
        Tuple of (is_valid, list of errors)
    """
    errors: List[str] = []

    # Check effects
    if left_clip.effects != original_clip.effects:
        errors.append(f"Left clip effects mismatch: {left_clip.effects} != {original_clip.effects}")
    if right_clip.effects != original_clip.effects:
        errors.append(f"Right clip effects mismatch: {right_clip.effects} != {original_clip.effects}")

    # Check color_stack_id
    if left_clip.color_stack_id != original_clip.color_stack_id:
        errors.append(f"Left clip color_stack_id lost: {original_clip.color_stack_id} -> {left_clip.color_stack_id}")
    if right_clip.color_stack_id != original_clip.color_stack_id:
        errors.append(f"Right clip color_stack_id lost: {original_clip.color_stack_id} -> {right_clip.color_stack_id}")

    # Check color_shared
    if left_clip.color_shared != original_clip.color_shared:
        errors.append(f"Left clip color_shared changed: {original_clip.color_shared} -> {left_clip.color_shared}")
    if right_clip.color_shared != original_clip.color_shared:
        errors.append(f"Right clip color_shared changed: {original_clip.color_shared} -> {right_clip.color_shared}")

    # Check composite_id
    if left_clip.composite_id != original_clip.composite_id:
        errors.append(f"Left clip composite_id lost: {original_clip.composite_id} -> {left_clip.composite_id}")
    if right_clip.composite_id != original_clip.composite_id:
        errors.append(f"Right clip composite_id lost: {original_clip.composite_id} -> {right_clip.composite_id}")

    # Check composite_bypassed
    if left_clip.composite_bypassed != original_clip.composite_bypassed:
        errors.append(f"Left clip composite_bypassed changed: {original_clip.composite_bypassed} -> {left_clip.composite_bypassed}")
    if right_clip.composite_bypassed != original_clip.composite_bypassed:
        errors.append(f"Right clip composite_bypassed changed: {original_clip.composite_bypassed} -> {right_clip.composite_bypassed}")

    # Check animated_params
    if left_clip.animated_params != original_clip.animated_params:
        errors.append("Left clip animated_params mismatch")
    if right_clip.animated_params != original_clip.animated_params:
        errors.append("Right clip animated_params mismatch")

    # Check transform
    if left_clip.transform != original_clip.transform:
        errors.append("Left clip transform mismatch")
    if right_clip.transform != original_clip.transform:
        errors.append("Right clip transform mismatch")

    # Check crop
    if left_clip.crop != original_clip.crop:
        errors.append("Left clip crop mismatch")
    if right_clip.crop != original_clip.crop:
        errors.append("Right clip crop mismatch")

    # Check opacity
    if left_clip.opacity != original_clip.opacity:
        errors.append(f"Left clip opacity mismatch: {original_clip.opacity} -> {left_clip.opacity}")
    if right_clip.opacity != original_clip.opacity:
        errors.append(f"Right clip opacity mismatch: {original_clip.opacity} -> {right_clip.opacity}")

    # Check speed
    if left_clip.speed != original_clip.speed:
        errors.append("Left clip speed mismatch")
    if right_clip.speed != original_clip.speed:
        errors.append("Right clip speed mismatch")

    # Check fade in/out (special rules)
    # Left clip preserves fade_in, fade_out
    if left_clip.fade_in_duration != original_clip.fade_in_duration:
        errors.append(f"Left clip fade_in_duration lost: {original_clip.fade_in_duration} -> {left_clip.fade_in_duration}")
    # Right clip should have fade_in=0, preserves fade_out
    if right_clip.fade_in_duration != 0:
        errors.append(f"Right clip should have fade_in=0, got {right_clip.fade_in_duration}")
    if right_clip.fade_out_duration != original_clip.fade_out_duration:
        errors.append(f"Right clip fade_out_duration lost: {original_clip.fade_out_duration} -> {right_clip.fade_out_duration}")

    # Check FPS metadata
    if left_clip._source_fps_num != original_clip._source_fps_num:
        errors.append("Left clip _source_fps_num lost")
    if right_clip._source_fps_num != original_clip._source_fps_num:
        errors.append("Right clip _source_fps_num lost")

    # Check linked_clip_id (right clip should have None)
    if right_clip.linked_clip_id is not None:
        errors.append(f"Right clip should have linked_clip_id=None, got {right_clip.linked_clip_id}")

    return len(errors) == 0, errors


def get_preserved_attributes_summary(clip: ClipInstance) -> Dict[str, Any]:
    """
    Get a summary of clip attributes that should be preserved during split.

    Args:
        clip: Clip instance

    Returns:
        Dictionary of preserved attributes
    """
    return {
        "effects_count": len(clip.effects),
        "has_color_stack": clip.color_stack_id is not None,
        "color_stack_id": clip.color_stack_id,
        "color_shared": clip.color_shared,
        "has_composite": clip.composite_id is not None,
        "composite_id": clip.composite_id,
        "composite_bypassed": clip.composite_bypassed,
        "animated_params_count": len(clip.animated_params),
        "has_fade_in": clip.fade_in_duration > 0,
        "fade_in_duration": clip.fade_in_duration,
        "has_fade_out": clip.fade_out_duration > 0,
        "fade_out_duration": clip.fade_out_duration,
        "opacity": clip.opacity,
        "speed": str(clip.speed),
        "has_linked_clip": clip.linked_clip_id is not None,
        "linked_clip_id": clip.linked_clip_id,
        "has_fps_metadata": clip._source_fps_num is not None,
    }


# ================================================================
# Apply All (Markers + Splits)
# ================================================================

def apply_all(
    candidates: Tuple[SceneCutCandidate, ...],
    timeline: TimelineSequence,
    target_clip_id: str,
    target_track_id: str,
    include_markers: bool = True,
    include_splits: bool = True,
    split_linked: bool = True,
    hard_cut_only: bool = True,
    marker_name_prefix: str = MARKER_NAME_PREFIX,
) -> ApplyAllResult:
    """
    Apply both markers and splits in single MacroCommand.

    This is the main apply function for Scene Cut Detection results.
    Creates a single MacroCommand containing all marker and split commands.

    Args:
        candidates: Scene cut candidates
        timeline: Current timeline
        target_clip_id: Clip ID to split (if include_splits)
        target_track_id: Track ID containing the clip
        include_markers: Whether to add markers
        include_splits: Whether to split clips
        split_linked: Whether to split linked audio/video
        hard_cut_only: Only split hard cuts
        marker_name_prefix: Prefix for marker names

    Returns:
        ApplyAllResult with macro command and details
    """
    all_commands: List[Any] = []
    markers: List[TimelineMarker] = []
    split_positions: List[int] = []
    warnings: List[str] = []
    linked_split_count = 0

    # Apply markers
    if include_markers:
        marker_result = apply_markers(
            candidates=candidates,
            timeline=timeline,
            marker_name_prefix=marker_name_prefix
        )
        all_commands.extend(marker_result.commands)
        markers.extend(marker_result.markers)
        logger.debug(f"Created {marker_result.count} marker commands")

    # Apply splits
    if include_splits:
        split_result = apply_splits(
            candidates=candidates,
            timeline=timeline,
            target_clip_id=target_clip_id,
            target_track_id=target_track_id,
            split_linked=split_linked,
            hard_cut_only=hard_cut_only
        )
        all_commands.extend(split_result.commands)
        split_positions.extend(split_result.split_positions)
        warnings.extend(split_result.warnings)
        linked_split_count = split_result.linked_split_count
        logger.debug(f"Created {split_result.split_count} split commands")

    # Create macro command
    macro = MacroCommand.create(
        label=f"Apply Scene Cuts ({len(all_commands)} operations)",
        commands=all_commands
    )

    return ApplyAllResult(
        macro_command=macro,
        markers=tuple(markers),
        split_positions=tuple(split_positions),
        marker_count=len(markers),
        split_count=len(split_positions),
        linked_split_count=linked_split_count,
        warnings=tuple(warnings)
    )


# ================================================================
# Conflict Detection
# ================================================================

def detect_conflicts(
    candidates: Tuple[SceneCutCandidate, ...],
    timeline: TimelineSequence,
    target_clip_id: str,
    target_track_id: str,
) -> ConflictReport:
    """
    Detect conflicts that would prevent apply operations.

    Checks:
    - Locked tracks
    - Disabled clips
    - Split positions outside clip range
    - Duplicate split positions
    - Transition conflicts

    Args:
        candidates: Scene cut candidates
        timeline: Current timeline
        target_clip_id: Clip ID to split
        target_track_id: Track ID containing the clip

    Returns:
        ConflictReport with all detected issues
    """
    locked_tracks: List[str] = []
    disabled_clips: List[str] = []
    out_of_range_splits: List[int] = []
    duplicate_splits: List[int] = []
    transition_conflicts: List[str] = []
    messages: List[str] = []

    # Check target track
    track = timeline.get_track_by_id(target_track_id)
    if track is None:
        messages.append(f"Track not found: {target_track_id}")
        return ConflictReport(
            locked_tracks=(),
            disabled_clips=(),
            out_of_range_splits=(),
            duplicate_splits=(),
            transition_conflicts=(),
            messages=tuple(messages)
        )

    if track.is_locked:
        locked_tracks.append(target_track_id)
        messages.append(f"Track {target_track_id} is locked")

    # Check target clip
    clip = track.get_clip_by_id(target_clip_id)
    if clip is None:
        messages.append(f"Clip not found: {target_clip_id}")
        return ConflictReport(
            locked_tracks=tuple(locked_tracks),
            disabled_clips=(),
            out_of_range_splits=(),
            duplicate_splits=(),
            transition_conflicts=(),
            messages=tuple(messages)
        )

    if not clip.is_enabled:
        disabled_clips.append(target_clip_id)
        messages.append(f"Clip {target_clip_id} is disabled")

    # Check transitions on track
    for trans in track.transitions:
        # Check if any split position is inside transition
        for cand in candidates:
            if cand.is_accepted and cand.timeline_frame:
                frame = cand.timeline_frame
                trans_start = trans.center_frame - trans.duration // 2
                trans_end = trans.center_frame + trans.duration // 2
                if trans_start <= frame <= trans_end:
                    transition_conflicts.append(f"Split at {frame} conflicts with transition {trans.transition_id}")
                    messages.append(f"Split at frame {frame} inside transition {trans.transition_id}")

    # Check split positions
    seen_frames: Dict[int, int] = {}
    for cand in candidates:
        if not cand.is_accepted:
            continue
        if cand.timeline_frame is None:
            continue

        frame = cand.timeline_frame

        # Check inside clip range
        if not (clip.timeline_start < frame < clip.timeline_end):
            out_of_range_splits.append(frame)
            messages.append(f"Split frame {frame} outside clip [{clip.timeline_start}, {clip.timeline_end})")

        # Check duplicates
        if frame in seen_frames:
            duplicate_splits.append(frame)
            messages.append(f"Duplicate split at frame {frame}")
        else:
            seen_frames[frame] = 1

    # Check linked audio track
    if clip.linked_clip_id:
        for audio_track in timeline.audio_tracks:
            linked_clip = audio_track.get_clip_by_id(clip.linked_clip_id)
            if linked_clip and audio_track.is_locked:
                locked_tracks.append(audio_track.track_id)
                messages.append(f"Linked audio track {audio_track.track_id} is locked")

    return ConflictReport(
        locked_tracks=tuple(locked_tracks),
        disabled_clips=tuple(disabled_clips),
        out_of_range_splits=tuple(out_of_range_splits),
        duplicate_splits=tuple(duplicate_splits),
        transition_conflicts=tuple(transition_conflicts),
        messages=tuple(messages)
    )


# ================================================================
# Helper Functions
# ================================================================

def get_accepted_count(candidates: Tuple[SceneCutCandidate, ...]) -> int:
    """Get count of accepted candidates."""
    return sum(1 for c in candidates if c.is_accepted)


def get_hard_cut_count(candidates: Tuple[SceneCutCandidate, ...]) -> int:
    """Get count of accepted hard cut candidates."""
    return sum(
        1 for c in candidates
        if c.is_accepted and c.kind == SceneCutKind.HARD_CUT
    )


def validate_apply_prerequisites(
    candidates: Tuple[SceneCutCandidate, ...],
    timeline: TimelineSequence,
    target_clip_id: str,
    target_track_id: str,
    require_accepted: bool = True,
) -> Tuple[bool, str]:
    """
    Validate prerequisites for apply operations.

    Args:
        candidates: Scene cut candidates
        timeline: Current timeline
        target_clip_id: Clip ID to split
        target_track_id: Track ID
        require_accepted: Whether to require at least one accepted candidate

    Returns:
        Tuple of (is_valid, error_message)
    """
    # Check for accepted candidates
    if require_accepted:
        accepted_count = get_accepted_count(candidates)
        if accepted_count == 0:
            return False, "No accepted candidates to apply"

    # Check for conflicts
    conflicts = detect_conflicts(
        candidates=candidates,
        timeline=timeline,
        target_clip_id=target_clip_id,
        target_track_id=target_track_id
    )
    if conflicts.has_conflicts:
        return False, "; ".join(conflicts.messages)

    return True, ""


def estimate_result_clips(
    original_clip: ClipInstance,
    split_positions: Tuple[int, ...]
) -> int:
    """
    Estimate number of clips after split operations.

    Args:
        original_clip: Original clip before split
        split_positions: Sorted split positions (ascending)

    Returns:
        Estimated number of clips (original splits into N+1 clips)
    """
    # Each split creates one additional clip
    # N splits -> N+1 clips
    valid_splits = [
        f for f in split_positions
        if original_clip.timeline_start < f < original_clip.timeline_end
    ]
    return len(valid_splits) + 1


# ================================================================
# Export
# ================================================================

__all__ = [
    # Result classes
    "ApplyMarkersResult",
    "ApplySplitsResult",
    "ApplyAllResult",
    "ConflictReport",
    # Main functions
    "apply_markers",
    "apply_splits",
    "apply_all",
    "detect_conflicts",
    # Helper functions
    "get_accepted_count",
    "get_hard_cut_count",
    "validate_apply_prerequisites",
    "estimate_result_clips",
    # Attribute preservation validation
    "verify_split_preserves_attributes",
    "get_preserved_attributes_summary",
    # Constants
    "MARKER_COLOR_HARD_CUT",
    "MARKER_COLOR_SOFT_TRANSITION",
    "MARKER_COLOR_BLACK_GAP",
    "MARKER_COLOR_WARNING",
    "MARKER_NAME_PREFIX",
]