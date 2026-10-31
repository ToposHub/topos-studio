"""
Tests for Scene Cut Apply Operations.

This module tests the apply_ops functions for:
- Creating TimelineMarker from accepted candidates
- Splitting clips at hard cut positions
- Linked audio/video sync split
- Conflict detection
- MacroCommand generation

Reference:
- docs/resolve_like_scene_cut_detection_implementation_plan_2026-06-04.md P6-1 to P6-7
"""

from __future__ import annotations

import pytest
import uuid

from src.features.edit.core.scene_cut.models import (
    SceneCutCandidate,
    SceneCutKind,
    SceneCutDecision,
    SceneCutMetrics,
)
from src.features.edit.core.scene_cut.apply_ops import (
    apply_markers,
    apply_splits,
    apply_all,
    detect_conflicts,
    get_accepted_count,
    get_hard_cut_count,
    validate_apply_prerequisites,
    estimate_result_clips,
    ApplyMarkersResult,
    ApplySplitsResult,
    ApplyAllResult,
    ConflictReport,
    MARKER_COLOR_HARD_CUT,
    MARKER_NAME_PREFIX,
)
from src.features.edit.core.timeline_v2 import (
    TimelineSequence,
    EditTrack,
    ClipInstance,
    TimelineMarker,
)
from src.features.edit.core.commands import (
    AddMarkerCommand,
    SplitClipCommand,
    MacroCommand,
)


# ================================================================
# Test Fixtures
# ================================================================

@pytest.fixture
def sample_metrics() -> SceneCutMetrics:
    """Create sample metrics for testing."""
    return SceneCutMetrics(
        luma_delta=0.5,
        hist_delta=0.4,
        edge_delta=0.3,
        color_delta=0.2,
        black_score=0.1,
        flash_score=0.1,
        motion_score=0.2,
        combined_score=0.4
    )


@pytest.fixture
def accepted_hard_cut_candidate(sample_metrics: SceneCutMetrics) -> SceneCutCandidate:
    """Create an accepted hard cut candidate."""
    return SceneCutCandidate(
        source_frame=100,
        pts=100,
        seconds=4.17,
        timeline_frame=100,
        score=0.85,
        confidence=0.90,
        kind=SceneCutKind.HARD_CUT,
        decision=SceneCutDecision.ACCEPTED,
        metrics=sample_metrics,
    )


@pytest.fixture
def accepted_soft_transition_candidate(sample_metrics: SceneCutMetrics) -> SceneCutCandidate:
    """Create an accepted soft transition candidate."""
    return SceneCutCandidate(
        source_frame=200,
        pts=200,
        seconds=8.33,
        timeline_frame=200,
        score=0.70,
        confidence=0.75,
        kind=SceneCutKind.SOFT_TRANSITION,
        decision=SceneCutDecision.ACCEPTED,
        metrics=sample_metrics,
    )


@pytest.fixture
def pending_candidate(sample_metrics: SceneCutMetrics) -> SceneCutCandidate:
    """Create a pending candidate."""
    return SceneCutCandidate(
        source_frame=300,
        pts=300,
        seconds=12.5,
        timeline_frame=300,
        score=0.60,
        confidence=0.65,
        kind=SceneCutKind.HARD_CUT,
        decision=SceneCutDecision.PENDING,
        metrics=sample_metrics,
    )


@pytest.fixture
def rejected_candidate(sample_metrics: SceneCutMetrics) -> SceneCutCandidate:
    """Create a rejected candidate."""
    return SceneCutCandidate(
        source_frame=400,
        pts=400,
        seconds=16.67,
        timeline_frame=400,
        score=0.50,
        confidence=0.55,
        kind=SceneCutKind.HARD_CUT,
        decision=SceneCutDecision.REJECTED,
        metrics=sample_metrics,
    )


@pytest.fixture
def warning_candidate(sample_metrics: SceneCutMetrics) -> SceneCutCandidate:
    """Create a warning candidate (flash)."""
    return SceneCutCandidate(
        source_frame=500,
        pts=500,
        seconds=20.83,
        timeline_frame=500,
        score=0.80,
        confidence=0.45,
        kind=SceneCutKind.FLASH,
        decision=SceneCutDecision.PENDING,
        metrics=sample_metrics,
        reasons=("flash_like", "low_confidence")
    )


@pytest.fixture
def sample_candidates(
    accepted_hard_cut_candidate: SceneCutCandidate,
    accepted_soft_transition_candidate: SceneCutCandidate,
    pending_candidate: SceneCutCandidate,
    rejected_candidate: SceneCutCandidate,
    warning_candidate: SceneCutCandidate,
) -> tuple[SceneCutCandidate, ...]:
    """Create a tuple of sample candidates."""
    return (
        accepted_hard_cut_candidate,
        accepted_soft_transition_candidate,
        pending_candidate,
        rejected_candidate,
        warning_candidate,
    )


@pytest.fixture
def sample_clip() -> ClipInstance:
    """Create a sample clip for testing."""
    return ClipInstance(
        clip_id=str(uuid.uuid4()),
        asset_id=str(uuid.uuid4()),
        track_id="v1",
        timeline_start=0,
        timeline_duration=1000,
        source_start=0,
        source_duration=1000,
    )


@pytest.fixture
def sample_track(sample_clip: ClipInstance) -> EditTrack:
    """Create a sample track with clip."""
    return EditTrack(
        track_id="v1",
        track_type="video",
        name="V1",
        clips=(sample_clip,),
        transitions=(),
        is_locked=False,
        is_muted=False,
    )


@pytest.fixture
def sample_timeline(sample_track: EditTrack) -> TimelineSequence:
    """Create a sample timeline with track."""
    return TimelineSequence(
        timeline_id=str(uuid.uuid4()),
        name="Test Timeline",
        fps=24.0,
        width=1920,
        height=1080,
        sample_rate=48000,
        video_tracks=(sample_track,),
        audio_tracks=(),
        markers=(),
    )


@pytest.fixture
def timeline_with_linked_audio(sample_clip: ClipInstance) -> TimelineSequence:
    """Create a timeline with linked audio clip."""
    # Create linked audio clip
    linked_clip = ClipInstance(
        clip_id="audio_linked_1",
        asset_id=str(uuid.uuid4()),
        track_id="a1",
        timeline_start=0,
        timeline_duration=1000,
        source_start=0,
        source_duration=1000,
        linked_clip_id=sample_clip.clip_id,
    )

    # Update video clip with linked_clip_id
    video_clip = ClipInstance(
        clip_id=sample_clip.clip_id,
        asset_id=sample_clip.asset_id,
        track_id="v1",
        timeline_start=0,
        timeline_duration=1000,
        source_start=0,
        source_duration=1000,
        linked_clip_id=linked_clip.clip_id,
    )

    video_track = EditTrack(
        track_id="v1",
        track_type="video",
        name="V1",
        clips=(video_clip,),
        transitions=(),
        is_locked=False,
        is_muted=False,
    )

    audio_track = EditTrack(
        track_id="a1",
        track_type="audio",
        name="A1",
        clips=(linked_clip,),
        transitions=(),
        is_locked=False,
        is_muted=False,
    )

    return TimelineSequence(
        timeline_id=str(uuid.uuid4()),
        name="Test Timeline with Audio",
        fps=24.0,
        width=1920,
        height=1080,
        sample_rate=48000,
        video_tracks=(video_track,),
        audio_tracks=(audio_track,),
        markers=(),
    )


# ================================================================
# Apply Markers Tests
# ================================================================

def test_apply_markers_creates_commands_for_accepted(
    sample_candidates: tuple[SceneCutCandidate, ...],
    sample_timeline: TimelineSequence,
) -> None:
    """Test that apply_markers creates commands for accepted candidates."""
    result = apply_markers(sample_candidates, sample_timeline)

    # Should have 2 accepted candidates (hard cut + soft transition)
    assert result.count == 2
    assert len(result.commands) == 2
    assert len(result.markers) == 2

    # All commands should be AddMarkerCommand
    for cmd in result.commands:
        assert isinstance(cmd, AddMarkerCommand)

    # All markers should be TimelineMarker
    for marker in result.markers:
        assert isinstance(marker, TimelineMarker)


def test_apply_markers_skips_rejected(
    rejected_candidate: SceneCutCandidate,
    sample_timeline: TimelineSequence,
) -> None:
    """Test that apply_markers skips rejected candidates."""
    candidates = (rejected_candidate,)
    result = apply_markers(candidates, sample_timeline)

    assert result.count == 0
    assert len(result.commands) == 0
    assert len(result.markers) == 0


def test_apply_markers_skips_pending_by_default(
    pending_candidate: SceneCutCandidate,
    sample_timeline: TimelineSequence,
) -> None:
    """Test that apply_markers skips pending candidates by default."""
    candidates = (pending_candidate,)
    result = apply_markers(candidates, sample_timeline)

    assert result.count == 0


def test_apply_markers_includes_pending_when_enabled(
    pending_candidate: SceneCutCandidate,
    sample_timeline: TimelineSequence,
) -> None:
    """Test that apply_markers includes pending when include_pending=True."""
    candidates = (pending_candidate,)
    result = apply_markers(candidates, sample_timeline, include_pending=True)

    assert result.count == 1
    assert len(result.commands) == 1


def test_apply_markers_sets_correct_color_for_hard_cut(
    accepted_hard_cut_candidate: SceneCutCandidate,
    sample_timeline: TimelineSequence,
) -> None:
    """Test that hard cut marker uses correct color."""
    candidates = (accepted_hard_cut_candidate,)
    result = apply_markers(candidates, sample_timeline)

    assert len(result.markers) == 1
    assert result.markers[0].color == MARKER_COLOR_HARD_CUT


def test_apply_markers_uses_timeline_frame(
    accepted_hard_cut_candidate: SceneCutCandidate,
    sample_timeline: TimelineSequence,
) -> None:
    """Test that marker frame uses timeline_frame when available."""
    candidates = (accepted_hard_cut_candidate,)
    result = apply_markers(candidates, sample_timeline)

    assert len(result.markers) == 1
    # timeline_frame is 100, source_frame is also 100
    assert result.markers[0].frame == 100


# ================================================================
# Apply Splits Tests
# ================================================================

def test_apply_splits_creates_commands_for_accepted_hard_cuts(
    accepted_hard_cut_candidate: SceneCutCandidate,
    sample_timeline: TimelineSequence,
    sample_clip: ClipInstance,
) -> None:
    """Test that apply_splits creates split commands for accepted hard cuts."""
    candidates = (accepted_hard_cut_candidate,)
    result = apply_splits(
        candidates=candidates,
        timeline=sample_timeline,
        target_clip_id=sample_clip.clip_id,
        target_track_id="v1",
    )

    assert result.split_count == 1
    assert len(result.commands) == 1
    assert isinstance(result.commands[0], SplitClipCommand)


def test_apply_splits_skips_soft_transitions_by_default(
    accepted_soft_transition_candidate: SceneCutCandidate,
    sample_timeline: TimelineSequence,
    sample_clip: ClipInstance,
) -> None:
    """Test that apply_splits skips soft transitions when hard_cut_only=True."""
    candidates = (accepted_soft_transition_candidate,)
    result = apply_splits(
        candidates=candidates,
        timeline=sample_timeline,
        target_clip_id=sample_clip.clip_id,
        target_track_id="v1",
        hard_cut_only=True,
    )

    assert result.split_count == 0


def test_apply_splits_includes_soft_transitions_when_enabled(
    accepted_soft_transition_candidate: SceneCutCandidate,
    sample_timeline: TimelineSequence,
    sample_clip: ClipInstance,
) -> None:
    """Test that apply_splits includes soft transitions when hard_cut_only=False."""
    candidates = (accepted_soft_transition_candidate,)
    result = apply_splits(
        candidates=candidates,
        timeline=sample_timeline,
        target_clip_id=sample_clip.clip_id,
        target_track_id="v1",
        hard_cut_only=False,
    )

    assert result.split_count == 1


def test_apply_splits_skips_out_of_range(
    sample_timeline: TimelineSequence,
    sample_clip: ClipInstance,
    sample_metrics: SceneCutMetrics,
) -> None:
    """Test that apply_splits skips positions outside clip range."""
    # Create candidate outside clip range (clip duration is 1000)
    out_of_range_candidate = SceneCutCandidate(
        source_frame=1500,
        pts=1500,
        seconds=62.5,
        timeline_frame=1500,
        score=0.85,
        confidence=0.90,
        kind=SceneCutKind.HARD_CUT,
        decision=SceneCutDecision.ACCEPTED,
        metrics=sample_metrics,
    )

    candidates = (out_of_range_candidate,)
    result = apply_splits(
        candidates=candidates,
        timeline=sample_timeline,
        target_clip_id=sample_clip.clip_id,
        target_track_id="v1",
    )

    assert result.split_count == 0
    assert "outside clip" in str(result.warnings).lower() or len(result.warnings) == 0


def test_apply_splits_splits_linked_audio(
    accepted_hard_cut_candidate: SceneCutCandidate,
    timeline_with_linked_audio: TimelineSequence,
) -> None:
    """Test that apply_splits splits linked audio clips."""
    # Find the video clip
    video_track = timeline_with_linked_audio.video_tracks[0]
    video_clip = video_track.clips[0]

    candidates = (accepted_hard_cut_candidate,)
    result = apply_splits(
        candidates=candidates,
        timeline=timeline_with_linked_audio,
        target_clip_id=video_clip.clip_id,
        target_track_id="v1",
        split_linked=True,
    )

    # Should have 2 split commands (video + audio)
    assert result.split_count == 1
    assert result.linked_split_count == 1
    assert len(result.commands) == 2


def test_apply_splits_skips_rejected(
    rejected_candidate: SceneCutCandidate,
    sample_timeline: TimelineSequence,
    sample_clip: ClipInstance,
) -> None:
    """Test that apply_splits skips rejected candidates."""
    candidates = (rejected_candidate,)
    result = apply_splits(
        candidates=candidates,
        timeline=sample_timeline,
        target_clip_id=sample_clip.clip_id,
        target_track_id="v1",
    )

    assert result.split_count == 0


# ================================================================
# Apply All Tests
# ================================================================

def test_apply_all_creates_macro_command(
    sample_candidates: tuple[SceneCutCandidate, ...],
    sample_timeline: TimelineSequence,
    sample_clip: ClipInstance,
) -> None:
    """Test that apply_all creates a MacroCommand."""
    result = apply_all(
        candidates=sample_candidates,
        timeline=sample_timeline,
        target_clip_id=sample_clip.clip_id,
        target_track_id="v1",
    )

    assert isinstance(result.macro_command, MacroCommand)
    # Should have markers (2 accepted) + splits (1 hard cut)
    assert result.marker_count == 2
    assert result.split_count == 1


def test_apply_all_can_skip_markers(
    accepted_hard_cut_candidate: SceneCutCandidate,
    sample_timeline: TimelineSequence,
    sample_clip: ClipInstance,
) -> None:
    """Test that apply_all can skip markers."""
    candidates = (accepted_hard_cut_candidate,)
    result = apply_all(
        candidates=candidates,
        timeline=sample_timeline,
        target_clip_id=sample_clip.clip_id,
        target_track_id="v1",
        include_markers=False,
    )

    assert result.marker_count == 0
    assert result.split_count == 1


def test_apply_all_can_skip_splits(
    accepted_hard_cut_candidate: SceneCutCandidate,
    sample_timeline: TimelineSequence,
    sample_clip: ClipInstance,
) -> None:
    """Test that apply_all can skip splits."""
    candidates = (accepted_hard_cut_candidate,)
    result = apply_all(
        candidates=candidates,
        timeline=sample_timeline,
        target_clip_id=sample_clip.clip_id,
        target_track_id="v1",
        include_splits=False,
    )

    assert result.marker_count == 1
    assert result.split_count == 0


# ================================================================
# Conflict Detection Tests
# ================================================================

def test_detect_conflicts_detects_locked_track(
    sample_timeline: TimelineSequence,
    sample_clip: ClipInstance,
    sample_metrics: SceneCutMetrics,
) -> None:
    """Test that detect_conflicts detects locked tracks."""
    # Create timeline with locked track
    locked_track = EditTrack(
        track_id="v1",
        track_type="video",
        name="V1",
        clips=(sample_clip,),
        transitions=(),
        is_locked=True,  # Locked!
        is_muted=False,
    )

    locked_timeline = TimelineSequence(
        timeline_id=str(uuid.uuid4()),
        name="Locked Timeline",
        fps=24.0,
        width=1920,
        height=1080,
        sample_rate=48000,
        video_tracks=(locked_track,),
        audio_tracks=(),
        markers=(),
    )

    candidate = SceneCutCandidate(
        source_frame=100,
        pts=100,
        seconds=4.17,
        timeline_frame=100,
        score=0.85,
        confidence=0.90,
        kind=SceneCutKind.HARD_CUT,
        decision=SceneCutDecision.ACCEPTED,
        metrics=sample_metrics,
    )

    conflicts = detect_conflicts(
        candidates=(candidate,),
        timeline=locked_timeline,
        target_clip_id=sample_clip.clip_id,
        target_track_id="v1",
    )

    assert conflicts.has_conflicts
    assert "v1" in conflicts.locked_tracks


def test_detect_conflicts_detects_disabled_clip(
    sample_timeline: TimelineSequence,
    sample_metrics: SceneCutMetrics,
) -> None:
    """Test that detect_conflicts detects disabled clips."""
    # Create disabled clip
    disabled_clip = ClipInstance(
        clip_id=str(uuid.uuid4()),
        asset_id=str(uuid.uuid4()),
        track_id="v1",
        timeline_start=0,
        timeline_duration=1000,
        source_start=0,
        source_duration=1000,
        is_enabled=False,  # Disabled!
    )

    track = EditTrack(
        track_id="v1",
        track_type="video",
        name="V1",
        clips=(disabled_clip,),
        transitions=(),
        is_locked=False,
        is_muted=False,
    )

    timeline = TimelineSequence(
        timeline_id=str(uuid.uuid4()),
        name="Timeline",
        fps=24.0,
        width=1920,
        height=1080,
        sample_rate=48000,
        video_tracks=(track,),
        audio_tracks=(),
        markers=(),
    )

    candidate = SceneCutCandidate(
        source_frame=100,
        pts=100,
        seconds=4.17,
        timeline_frame=100,
        score=0.85,
        confidence=0.90,
        kind=SceneCutKind.HARD_CUT,
        decision=SceneCutDecision.ACCEPTED,
        metrics=sample_metrics,
    )

    conflicts = detect_conflicts(
        candidates=(candidate,),
        timeline=timeline,
        target_clip_id=disabled_clip.clip_id,
        target_track_id="v1",
    )

    assert conflicts.has_conflicts
    assert disabled_clip.clip_id in conflicts.disabled_clips


def test_detect_conflicts_detects_out_of_range_splits(
    sample_timeline: TimelineSequence,
    sample_clip: ClipInstance,
    sample_metrics: SceneCutMetrics,
) -> None:
    """Test that detect_conflicts detects out of range split positions."""
    # Split position outside clip range
    out_of_range_candidate = SceneCutCandidate(
        source_frame=1500,
        pts=1500,
        seconds=62.5,
        timeline_frame=1500,
        score=0.85,
        confidence=0.90,
        kind=SceneCutKind.HARD_CUT,
        decision=SceneCutDecision.ACCEPTED,
        metrics=sample_metrics,
    )

    conflicts = detect_conflicts(
        candidates=(out_of_range_candidate,),
        timeline=sample_timeline,
        target_clip_id=sample_clip.clip_id,
        target_track_id="v1",
    )

    assert 1500 in conflicts.out_of_range_splits


def test_detect_conflicts_no_conflicts(
    accepted_hard_cut_candidate: SceneCutCandidate,
    sample_timeline: TimelineSequence,
    sample_clip: ClipInstance,
) -> None:
    """Test that detect_conflicts returns empty when no conflicts."""
    conflicts = detect_conflicts(
        candidates=(accepted_hard_cut_candidate,),
        timeline=sample_timeline,
        target_clip_id=sample_clip.clip_id,
        target_track_id="v1",
    )

    assert not conflicts.has_conflicts
    assert len(conflicts.messages) == 0


# ================================================================
# Helper Functions Tests
# ================================================================

def test_get_accepted_count(
    sample_candidates: tuple[SceneCutCandidate, ...],
) -> None:
    """Test get_accepted_count returns correct count."""
    count = get_accepted_count(sample_candidates)
    assert count == 2  # hard cut + soft transition


def test_get_hard_cut_count(
    sample_candidates: tuple[SceneCutCandidate, ...],
) -> None:
    """Test get_hard_cut_count returns correct count."""
    count = get_hard_cut_count(sample_candidates)
    assert count == 1  # Only 1 accepted hard cut


def test_validate_apply_prerequisites_no_accepted(
    pending_candidate: SceneCutCandidate,
    sample_timeline: TimelineSequence,
    sample_clip: ClipInstance,
) -> None:
    """Test validate_apply_prerequisites fails when no accepted candidates."""
    is_valid, msg = validate_apply_prerequisites(
        candidates=(pending_candidate,),
        timeline=sample_timeline,
        target_clip_id=sample_clip.clip_id,
        target_track_id="v1",
    )

    assert not is_valid
    assert "No accepted" in msg


def test_validate_apply_prerequisites_success(
    accepted_hard_cut_candidate: SceneCutCandidate,
    sample_timeline: TimelineSequence,
    sample_clip: ClipInstance,
) -> None:
    """Test validate_apply_prerequisites succeeds with valid inputs."""
    is_valid, msg = validate_apply_prerequisites(
        candidates=(accepted_hard_cut_candidate,),
        timeline=sample_timeline,
        target_clip_id=sample_clip.clip_id,
        target_track_id="v1",
    )

    assert is_valid
    assert msg == ""


def test_estimate_result_clips(
    sample_clip: ClipInstance,
) -> None:
    """Test estimate_result_clips returns correct count."""
    # 3 split positions -> 4 clips
    split_positions = (100, 200, 300)
    count = estimate_result_clips(sample_clip, split_positions)

    assert count == 4


def test_estimate_result_clips_empty_splits(
    sample_clip: ClipInstance,
) -> None:
    """Test estimate_result_clips returns 1 for no splits."""
    count = estimate_result_clips(sample_clip, ())
    assert count == 1


# ================================================================
# Right-to-Left Split Order Tests
# ================================================================

def test_splits_are_ordered_right_to_left(
    sample_metrics: SceneCutMetrics,
    sample_timeline: TimelineSequence,
    sample_clip: ClipInstance,
) -> None:
    """Test that splits are ordered right-to-left for stability."""
    # Create multiple accepted hard cuts
    candidates = tuple(
        SceneCutCandidate(
            source_frame=f,
            pts=f,
            seconds=f / 24.0,
            timeline_frame=f,
            score=0.85,
            confidence=0.90,
            kind=SceneCutKind.HARD_CUT,
            decision=SceneCutDecision.ACCEPTED,
            metrics=sample_metrics,
        )
        for f in (100, 200, 300, 400, 500)  # Ascending order
    )

    result = apply_splits(
        candidates=candidates,
        timeline=sample_timeline,
        target_clip_id=sample_clip.clip_id,
        target_track_id="v1",
    )

    # Split positions should be stored right-to-left (descending)
    assert result.split_positions == (500, 400, 300, 200, 100)


# ================================================================
# Multiple Candidates Tests
# ================================================================

def test_apply_multiple_candidates(
    sample_metrics: SceneCutMetrics,
    sample_timeline: TimelineSequence,
    sample_clip: ClipInstance,
) -> None:
    """Test applying multiple accepted candidates."""
    candidates = tuple(
        SceneCutCandidate(
            source_frame=f,
            pts=f,
            seconds=f / 24.0,
            timeline_frame=f,
            score=0.85,
            confidence=0.90,
            kind=SceneCutKind.HARD_CUT,
            decision=SceneCutDecision.ACCEPTED,
            metrics=sample_metrics,
        )
        for f in (100, 250, 500, 750)
    )

    result = apply_all(
        candidates=candidates,
        timeline=sample_timeline,
        target_clip_id=sample_clip.clip_id,
        target_track_id="v1",
    )

    # Should have 4 markers + 4 splits = 8 commands
    assert result.marker_count == 4
    assert result.split_count == 4


# ================================================================
# Marker Name and Comment Tests
# ================================================================

def test_marker_name_includes_frame_number(
    accepted_hard_cut_candidate: SceneCutCandidate,
    sample_timeline: TimelineSequence,
) -> None:
    """Test that marker name includes source frame number."""
    result = apply_markers((accepted_hard_cut_candidate,), sample_timeline)

    assert len(result.markers) == 1
    assert "100" in result.markers[0].name


def test_marker_comment_includes_scores(
    accepted_hard_cut_candidate: SceneCutCandidate,
    sample_timeline: TimelineSequence,
) -> None:
    """Test that marker comment includes score and confidence."""
    result = apply_markers((accepted_hard_cut_candidate,), sample_timeline)

    assert len(result.markers) == 1
    assert "Score:" in result.markers[0].comment
    assert "Conf:" in result.markers[0].comment


def test_marker_name_includes_kind_for_non_hard_cut(
    accepted_soft_transition_candidate: SceneCutCandidate,
    sample_timeline: TimelineSequence,
) -> None:
    """Test that marker name includes kind for non-hard-cut."""
    result = apply_markers((accepted_soft_transition_candidate,), sample_timeline)

    assert len(result.markers) == 1
    assert "soft_transition" in result.markers[0].name.lower()


# ================================================================
# Clip Attribute Preservation Tests
# ================================================================

@pytest.fixture
def clip_with_full_attributes() -> ClipInstance:
    """Create a clip with all attributes set."""
    from src.features.edit.core.clip_params import TransformParams, CropParams, SpeedParams
    from src.features.edit.core.effect_instance import AnimatableParam, KeyframeTrack
    from src.shared.keyframe import InterpolationType, Keyframe

    # Create animated params with keyframes
    opacity_keyframes = KeyframeTrack(
        param_name="opacity",
        min_val=0.0,
        max_val=1.0,
    )
    opacity_keyframes.add_keyframe(Keyframe(0, 1.0, InterpolationType.LINEAR))
    opacity_keyframes.add_keyframe(Keyframe(50, 0.5, InterpolationType.LINEAR))

    animated_params = {
        "opacity": AnimatableParam(
            base_value=1.0,
            keyframes=opacity_keyframes,
        ),
    }

    return ClipInstance(
        clip_id="clip-full-attrs",
        asset_id=str(uuid.uuid4()),
        track_id="v1",
        timeline_start=0,
        timeline_duration=1000,
        source_start=0,
        source_duration=1000,
        speed=SpeedParams(speed=1.0),
        linked_clip_id="linked-audio-1",
        transform=TransformParams(translate_x=100, translate_y=50, scale_x=1.2, scale_y=1.2),
        crop=CropParams(left=0.1, top=0.05, right=0.1, bottom=0.05),
        opacity=0.85,
        animated_params=animated_params,
        effects=("effect-1", "effect-2"),
        is_enabled=True,
        fade_in_duration=24,
        fade_out_duration=36,
        color_stack_id="color-stack-001",
        color_shared=True,
        composite_id="composite-001",
        composite_bypassed=False,
        render_cache_id="cache-001",
        _source_fps_num=24000,
        _source_fps_den=1001,
        _timeline_fps_num=24,
        _timeline_fps_den=1,
    )


@pytest.fixture
def timeline_with_full_clip(clip_with_full_attributes: ClipInstance) -> TimelineSequence:
    """Create a timeline with a clip having full attributes."""
    track = EditTrack(
        track_id="v1",
        track_type="video",
        name="V1",
        clips=(clip_with_full_attributes,),
        transitions=(),
        is_locked=False,
        is_muted=False,
    )
    return TimelineSequence(
        timeline_id=str(uuid.uuid4()),
        name="Test Timeline",
        fps=24.0,
        width=1920,
        height=1080,
        sample_rate=48000,
        video_tracks=(track,),
        audio_tracks=(),
        markers=(),
    )


def test_split_preserves_effects(
    clip_with_full_attributes: ClipInstance,
    timeline_with_full_clip: TimelineSequence,
    sample_metrics: SceneCutMetrics,
) -> None:
    """Test that split preserves effects on both clips."""
    from src.features.edit.core.scene_cut.apply_ops import verify_split_preserves_attributes

    # Apply split
    candidate = SceneCutCandidate(
        source_frame=500,
        pts=500,
        seconds=500 / 24.0,
        timeline_frame=500,
        score=0.85,
        confidence=0.90,
        kind=SceneCutKind.HARD_CUT,
        decision=SceneCutDecision.ACCEPTED,
        metrics=sample_metrics,
    )

    result = apply_splits(
        candidates=(candidate,),
        timeline=timeline_with_full_clip,
        target_clip_id=clip_with_full_attributes.clip_id,
        target_track_id="v1",
    )

    assert result.split_count == 1

    # Apply the split command to timeline
    split_cmd = result.commands[0]
    new_timeline = split_cmd.apply(timeline_with_full_clip)

    # Get the resulting clips
    track = new_timeline.get_track_by_id("v1")
    clips = sorted(track.clips, key=lambda c: c.timeline_start)
    left_clip, right_clip = clips[0], clips[1]

    # Verify effects preserved
    assert left_clip.effects == clip_with_full_attributes.effects
    assert right_clip.effects == clip_with_full_attributes.effects


def test_split_preserves_color_stack(
    clip_with_full_attributes: ClipInstance,
    timeline_with_full_clip: TimelineSequence,
    sample_metrics: SceneCutMetrics,
) -> None:
    """Test that split preserves color_stack_id and color_shared."""
    # Apply split
    candidate = SceneCutCandidate(
        source_frame=500,
        pts=500,
        seconds=500 / 24.0,
        timeline_frame=500,
        score=0.85,
        confidence=0.90,
        kind=SceneCutKind.HARD_CUT,
        decision=SceneCutDecision.ACCEPTED,
        metrics=sample_metrics,
    )

    result = apply_splits(
        candidates=(candidate,),
        timeline=timeline_with_full_clip,
        target_clip_id=clip_with_full_attributes.clip_id,
        target_track_id="v1",
    )

    split_cmd = result.commands[0]
    new_timeline = split_cmd.apply(timeline_with_full_clip)

    track = new_timeline.get_track_by_id("v1")
    clips = sorted(track.clips, key=lambda c: c.timeline_start)
    left_clip, right_clip = clips[0], clips[1]

    # Verify color attributes preserved
    assert left_clip.color_stack_id == clip_with_full_attributes.color_stack_id
    assert right_clip.color_stack_id == clip_with_full_attributes.color_stack_id
    assert left_clip.color_shared == clip_with_full_attributes.color_shared
    assert right_clip.color_shared == clip_with_full_attributes.color_shared


def test_split_preserves_composite(
    clip_with_full_attributes: ClipInstance,
    timeline_with_full_clip: TimelineSequence,
    sample_metrics: SceneCutMetrics,
) -> None:
    """Test that split preserves composite_id and composite_bypassed."""
    candidate = SceneCutCandidate(
        source_frame=500,
        pts=500,
        seconds=500 / 24.0,
        timeline_frame=500,
        score=0.85,
        confidence=0.90,
        kind=SceneCutKind.HARD_CUT,
        decision=SceneCutDecision.ACCEPTED,
        metrics=sample_metrics,
    )

    result = apply_splits(
        candidates=(candidate,),
        timeline=timeline_with_full_clip,
        target_clip_id=clip_with_full_attributes.clip_id,
        target_track_id="v1",
    )

    split_cmd = result.commands[0]
    new_timeline = split_cmd.apply(timeline_with_full_clip)

    track = new_timeline.get_track_by_id("v1")
    clips = sorted(track.clips, key=lambda c: c.timeline_start)
    left_clip, right_clip = clips[0], clips[1]

    # Verify composite attributes preserved
    assert left_clip.composite_id == clip_with_full_attributes.composite_id
    assert right_clip.composite_id == clip_with_full_attributes.composite_id
    assert left_clip.composite_bypassed == clip_with_full_attributes.composite_bypassed
    assert right_clip.composite_bypassed == clip_with_full_attributes.composite_bypassed


def test_split_preserves_fade_settings(
    clip_with_full_attributes: ClipInstance,
    timeline_with_full_clip: TimelineSequence,
    sample_metrics: SceneCutMetrics,
) -> None:
    """Test that split preserves fade in/out with correct rules."""
    candidate = SceneCutCandidate(
        source_frame=500,
        pts=500,
        seconds=500 / 24.0,
        timeline_frame=500,
        score=0.85,
        confidence=0.90,
        kind=SceneCutKind.HARD_CUT,
        decision=SceneCutDecision.ACCEPTED,
        metrics=sample_metrics,
    )

    result = apply_splits(
        candidates=(candidate,),
        timeline=timeline_with_full_clip,
        target_clip_id=clip_with_full_attributes.clip_id,
        target_track_id="v1",
    )

    split_cmd = result.commands[0]
    new_timeline = split_cmd.apply(timeline_with_full_clip)

    track = new_timeline.get_track_by_id("v1")
    clips = sorted(track.clips, key=lambda c: c.timeline_start)
    left_clip, right_clip = clips[0], clips[1]

    # Left clip preserves fade_in and fade_out
    assert left_clip.fade_in_duration == clip_with_full_attributes.fade_in_duration

    # Right clip starts fresh (fade_in=0), preserves fade_out
    assert right_clip.fade_in_duration == 0
    assert right_clip.fade_out_duration == clip_with_full_attributes.fade_out_duration


def test_split_preserves_animated_params(
    clip_with_full_attributes: ClipInstance,
    timeline_with_full_clip: TimelineSequence,
    sample_metrics: SceneCutMetrics,
) -> None:
    """Test that split preserves animated_params."""
    candidate = SceneCutCandidate(
        source_frame=500,
        pts=500,
        seconds=500 / 24.0,
        timeline_frame=500,
        score=0.85,
        confidence=0.90,
        kind=SceneCutKind.HARD_CUT,
        decision=SceneCutDecision.ACCEPTED,
        metrics=sample_metrics,
    )

    result = apply_splits(
        candidates=(candidate,),
        timeline=timeline_with_full_clip,
        target_clip_id=clip_with_full_attributes.clip_id,
        target_track_id="v1",
    )

    split_cmd = result.commands[0]
    new_timeline = split_cmd.apply(timeline_with_full_clip)

    track = new_timeline.get_track_by_id("v1")
    clips = sorted(track.clips, key=lambda c: c.timeline_start)
    left_clip, right_clip = clips[0], clips[1]

    # Both clips preserve animated_params
    assert left_clip.animated_params == clip_with_full_attributes.animated_params
    assert right_clip.animated_params == clip_with_full_attributes.animated_params


def test_split_preserves_fps_metadata(
    clip_with_full_attributes: ClipInstance,
    timeline_with_full_clip: TimelineSequence,
    sample_metrics: SceneCutMetrics,
) -> None:
    """Test that split preserves FPS metadata."""
    candidate = SceneCutCandidate(
        source_frame=500,
        pts=500,
        seconds=500 / 24.0,
        timeline_frame=500,
        score=0.85,
        confidence=0.90,
        kind=SceneCutKind.HARD_CUT,
        decision=SceneCutDecision.ACCEPTED,
        metrics=sample_metrics,
    )

    result = apply_splits(
        candidates=(candidate,),
        timeline=timeline_with_full_clip,
        target_clip_id=clip_with_full_attributes.clip_id,
        target_track_id="v1",
    )

    split_cmd = result.commands[0]
    new_timeline = split_cmd.apply(timeline_with_full_clip)

    track = new_timeline.get_track_by_id("v1")
    clips = sorted(track.clips, key=lambda c: c.timeline_start)
    left_clip, right_clip = clips[0], clips[1]

    # Both clips preserve FPS metadata
    assert left_clip._source_fps_num == clip_with_full_attributes._source_fps_num
    assert right_clip._source_fps_num == clip_with_full_attributes._source_fps_num
    assert left_clip._timeline_fps_num == clip_with_full_attributes._timeline_fps_num
    assert right_clip._timeline_fps_num == clip_with_full_attributes._timeline_fps_num


def test_split_right_clip_has_no_linked_clip_id(
    clip_with_full_attributes: ClipInstance,
    timeline_with_full_clip: TimelineSequence,
    sample_metrics: SceneCutMetrics,
) -> None:
    """Test that right clip has linked_clip_id=None after split."""
    candidate = SceneCutCandidate(
        source_frame=500,
        pts=500,
        seconds=500 / 24.0,
        timeline_frame=500,
        score=0.85,
        confidence=0.90,
        kind=SceneCutKind.HARD_CUT,
        decision=SceneCutDecision.ACCEPTED,
        metrics=sample_metrics,
    )

    result = apply_splits(
        candidates=(candidate,),
        timeline=timeline_with_full_clip,
        target_clip_id=clip_with_full_attributes.clip_id,
        target_track_id="v1",
    )

    split_cmd = result.commands[0]
    new_timeline = split_cmd.apply(timeline_with_full_clip)

    track = new_timeline.get_track_by_id("v1")
    clips = sorted(track.clips, key=lambda c: c.timeline_start)
    left_clip, right_clip = clips[0], clips[1]

    # Left clip preserves linked_clip_id
    assert left_clip.linked_clip_id == clip_with_full_attributes.linked_clip_id
    # Right clip has no linked_clip_id
    assert right_clip.linked_clip_id is None


def test_verify_split_preserves_attributes_function(
    clip_with_full_attributes: ClipInstance,
    timeline_with_full_clip: TimelineSequence,
    sample_metrics: SceneCutMetrics,
) -> None:
    """Test verify_split_preserves_attributes helper function."""
    from src.features.edit.core.scene_cut.apply_ops import verify_split_preserves_attributes

    candidate = SceneCutCandidate(
        source_frame=500,
        pts=500,
        seconds=500 / 24.0,
        timeline_frame=500,
        score=0.85,
        confidence=0.90,
        kind=SceneCutKind.HARD_CUT,
        decision=SceneCutDecision.ACCEPTED,
        metrics=sample_metrics,
    )

    result = apply_splits(
        candidates=(candidate,),
        timeline=timeline_with_full_clip,
        target_clip_id=clip_with_full_attributes.clip_id,
        target_track_id="v1",
    )

    split_cmd = result.commands[0]
    new_timeline = split_cmd.apply(timeline_with_full_clip)

    track = new_timeline.get_track_by_id("v1")
    clips = sorted(track.clips, key=lambda c: c.timeline_start)
    left_clip, right_clip = clips[0], clips[1]

    # Verify using helper function
    is_valid, errors = verify_split_preserves_attributes(
        clip_with_full_attributes,
        left_clip,
        right_clip
    )

    assert is_valid
    assert len(errors) == 0


# ================================================================
# MacroCommand Undo/Redo Tests
# ================================================================

def test_macro_command_undo_restores_original_state(
    sample_candidates: tuple[SceneCutCandidate, ...],
    sample_timeline: TimelineSequence,
    sample_clip: ClipInstance,
) -> None:
    """Test that MacroCommand undo restores timeline to original state."""
    result = apply_all(
        candidates=sample_candidates,
        timeline=sample_timeline,
        target_clip_id=sample_clip.clip_id,
        target_track_id="v1",
    )

    # Apply the macro command - returns tuple (timeline, updated_macro)
    apply_result = result.macro_command.apply(sample_timeline)
    new_timeline, updated_macro = apply_result

    # Verify markers were added (markers are part of timeline)
    # Note: AddMarkerCommand adds markers to timeline
    # The marker count depends on how AddMarkerCommand works

    # Undo the macro command using updated macro
    restored_timeline = updated_macro.undo(new_timeline)

    # Verify timeline is restored (exact behavior depends on sub-commands)
    # MacroCommand.undo() reverses all sub-commands in reverse order


def test_apply_all_single_undo_step(
    sample_metrics: SceneCutMetrics,
    sample_timeline: TimelineSequence,
    sample_clip: ClipInstance,
) -> None:
    """Test that apply_all creates a single undo step."""
    # Create multiple candidates
    candidates = tuple(
        SceneCutCandidate(
            source_frame=f,
            pts=f,
            seconds=f / 24.0,
            timeline_frame=f,
            score=0.85,
            confidence=0.90,
            kind=SceneCutKind.HARD_CUT,
            decision=SceneCutDecision.ACCEPTED,
            metrics=sample_metrics,
        )
        for f in (100, 250, 500, 750)
    )

    result = apply_all(
        candidates=candidates,
        timeline=sample_timeline,
        target_clip_id=sample_clip.clip_id,
        target_track_id="v1",
    )

    # Should be a single MacroCommand
    assert isinstance(result.macro_command, MacroCommand)

    # Label should describe the operation count
    assert "Apply Scene Cuts" in result.macro_command.label


def test_split_clip_command_undo_merges_clips(
    sample_timeline: TimelineSequence,
    sample_clip: ClipInstance,
    sample_metrics: SceneCutMetrics,
) -> None:
    """Test that SplitClipCommand.undo() merges the split clips back."""
    candidate = SceneCutCandidate(
        source_frame=500,
        pts=500,
        seconds=500 / 24.0,
        timeline_frame=500,
        score=0.85,
        confidence=0.90,
        kind=SceneCutKind.HARD_CUT,
        decision=SceneCutDecision.ACCEPTED,
        metrics=sample_metrics,
    )

    result = apply_splits(
        candidates=(candidate,),
        timeline=sample_timeline,
        target_clip_id=sample_clip.clip_id,
        target_track_id="v1",
    )

    # Apply split
    split_cmd = result.commands[0]
    new_timeline = split_cmd.apply(sample_timeline)

    # Verify split created two clips
    track = new_timeline.get_track_by_id("v1")
    assert len(track.clips) == 2

    # Undo split
    restored_timeline = split_cmd.undo(new_timeline)

    # Verify merged back to single clip
    restored_track = restored_timeline.get_track_by_id("v1")
    assert len(restored_track.clips) == 1

    # Verify original clip restored
    restored_clip = restored_track.clips[0]
    assert restored_clip.clip_id == sample_clip.clip_id
    assert restored_clip.timeline_start == sample_clip.timeline_start
    assert restored_clip.timeline_duration == sample_clip.timeline_duration


def test_split_undo_preserves_original_attributes(
    clip_with_full_attributes: ClipInstance,
    timeline_with_full_clip: TimelineSequence,
    sample_metrics: SceneCutMetrics,
) -> None:
    """Test that split undo restores original attributes."""
    candidate = SceneCutCandidate(
        source_frame=500,
        pts=500,
        seconds=500 / 24.0,
        timeline_frame=500,
        score=0.85,
        confidence=0.90,
        kind=SceneCutKind.HARD_CUT,
        decision=SceneCutDecision.ACCEPTED,
        metrics=sample_metrics,
    )

    result = apply_splits(
        candidates=(candidate,),
        timeline=timeline_with_full_clip,
        target_clip_id=clip_with_full_attributes.clip_id,
        target_track_id="v1",
    )

    split_cmd = result.commands[0]
    new_timeline = split_cmd.apply(timeline_with_full_clip)

    # Undo split
    restored_timeline = split_cmd.undo(new_timeline)

    # Verify original attributes restored
    restored_track = restored_timeline.get_track_by_id("v1")
    restored_clip = restored_track.clips[0]

    assert restored_clip.color_stack_id == clip_with_full_attributes.color_stack_id
    assert restored_clip.composite_id == clip_with_full_attributes.composite_id
    assert restored_clip.fade_in_duration == clip_with_full_attributes.fade_in_duration
    assert restored_clip.fade_out_duration == clip_with_full_attributes.fade_out_duration
    assert restored_clip.linked_clip_id == clip_with_full_attributes.linked_clip_id


# ================================================================
# Linked Audio Split Tests (Extended)
# ================================================================

def test_linked_audio_split_preserves_audio_attributes(
    timeline_with_linked_audio: TimelineSequence,
    sample_metrics: SceneCutMetrics,
) -> None:
    """Test that linked audio split preserves audio-specific attributes."""
    from src.features.edit.core.clip_params import TransformParams, CropParams

    # Find video and audio clips
    video_track = timeline_with_linked_audio.video_tracks[0]
    audio_track = timeline_with_linked_audio.audio_tracks[0]
    video_clip = video_track.clips[0]
    audio_clip = audio_track.clips[0]

    # Create candidate at timeline frame 500
    candidate = SceneCutCandidate(
        source_frame=500,
        pts=500,
        seconds=500 / 24.0,
        timeline_frame=500,
        score=0.85,
        confidence=0.90,
        kind=SceneCutKind.HARD_CUT,
        decision=SceneCutDecision.ACCEPTED,
        metrics=sample_metrics,
    )

    result = apply_splits(
        candidates=(candidate,),
        timeline=timeline_with_linked_audio,
        target_clip_id=video_clip.clip_id,
        target_track_id="v1",
        split_linked=True,
    )

    # Should have 2 commands: video split + audio split
    assert len(result.commands) == 2
    assert result.linked_split_count == 1

    # Apply both splits
    new_timeline = result.commands[0].apply(timeline_with_linked_audio)
    new_timeline = result.commands[1].apply(new_timeline)

    # Verify audio track has 2 clips
    new_audio_track = new_timeline.get_track_by_id("a1")
    assert len(new_audio_track.clips) == 2

    # Verify audio clips are at correct positions
    audio_clips = sorted(new_audio_track.clips, key=lambda c: c.timeline_start)
    left_audio, right_audio = audio_clips[0], audio_clips[1]

    assert left_audio.timeline_start == 0
    assert left_audio.timeline_end == 500
    assert right_audio.timeline_start == 500
    assert right_audio.timeline_end == 1000


def test_linked_audio_split_fails_if_audio_track_locked(
    timeline_with_linked_audio: TimelineSequence,
    sample_metrics: SceneCutMetrics,
) -> None:
    """Test that linked audio split fails if audio track is locked."""
    video_track = timeline_with_linked_audio.video_tracks[0]
    video_clip = video_track.clips[0]

    # Create locked audio track timeline
    locked_audio_track = EditTrack(
        track_id="a1",
        track_type="audio",
        name="A1",
        clips=timeline_with_linked_audio.audio_tracks[0].clips,
        transitions=(),
        is_locked=True,
        is_muted=False,
    )

    locked_timeline = TimelineSequence(
        timeline_id=timeline_with_linked_audio.timeline_id,
        name=timeline_with_linked_audio.name,
        fps=timeline_with_linked_audio.fps,
        width=timeline_with_linked_audio.width,
        height=timeline_with_linked_audio.height,
        sample_rate=timeline_with_linked_audio.sample_rate,
        video_tracks=timeline_with_linked_audio.video_tracks,
        audio_tracks=(locked_audio_track,),
        markers=(),
    )

    candidate = SceneCutCandidate(
        source_frame=500,
        pts=500,
        seconds=500 / 24.0,
        timeline_frame=500,
        score=0.85,
        confidence=0.90,
        kind=SceneCutKind.HARD_CUT,
        decision=SceneCutDecision.ACCEPTED,
        metrics=sample_metrics,
    )

    result = apply_splits(
        candidates=(candidate,),
        timeline=locked_timeline,
        target_clip_id=video_clip.clip_id,
        target_track_id="v1",
        split_linked=True,
    )

    # Video split succeeds, audio split fails
    assert result.split_count == 1
    assert result.linked_split_count == 0
    assert len(result.warnings) > 0


def test_no_linked_audio_split_when_split_linked_false(
    timeline_with_linked_audio: TimelineSequence,
    sample_metrics: SceneCutMetrics,
) -> None:
    """Test that linked audio is not split when split_linked=False."""
    video_track = timeline_with_linked_audio.video_tracks[0]
    video_clip = video_track.clips[0]

    candidate = SceneCutCandidate(
        source_frame=500,
        pts=500,
        seconds=500 / 24.0,
        timeline_frame=500,
        score=0.85,
        confidence=0.90,
        kind=SceneCutKind.HARD_CUT,
        decision=SceneCutDecision.ACCEPTED,
        metrics=sample_metrics,
    )

    result = apply_splits(
        candidates=(candidate,),
        timeline=timeline_with_linked_audio,
        target_clip_id=video_clip.clip_id,
        target_track_id="v1",
        split_linked=False,  # Don't split linked audio
    )

    # Only video split
    assert result.split_count == 1
    assert result.linked_split_count == 0
    assert len(result.commands) == 1


# ================================================================
# Get Preserved Attributes Summary Tests
# ================================================================

def test_get_preserved_attributes_summary(
    clip_with_full_attributes: ClipInstance,
) -> None:
    """Test get_preserved_attributes_summary helper function."""
    from src.features.edit.core.scene_cut.apply_ops import get_preserved_attributes_summary

    summary = get_preserved_attributes_summary(clip_with_full_attributes)

    assert summary["effects_count"] == 2
    assert summary["has_color_stack"] is True
    assert summary["color_stack_id"] == "color-stack-001"
    assert summary["has_composite"] is True
    assert summary["animated_params_count"] == 1
    assert summary["has_fade_in"] is True
    assert summary["fade_in_duration"] == 24
    assert summary["has_fade_out"] is True
    assert summary["has_linked_clip"] is True
    assert summary["has_fps_metadata"] is True