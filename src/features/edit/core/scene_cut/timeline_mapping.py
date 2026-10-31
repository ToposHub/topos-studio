"""
Scene Cut Timeline Mapping.

Maps source frame positions to timeline frame positions for scene cut candidates.
Handles VFR, different frame rates, speed changes, and reverse clips.

## Responsibilities

- ClipSourceMapper: source frame -> timeline frame mapping
- VFR/PTS mapping with error <= 1 frame
- Different fps handling (23.976->24, 29.97->25)
- Speed != 1.0 handling
- Reverse clip handling
- Warning marking for uncertain mappings

## Design Principles

- Uses existing timeline_frame_mapper for CFR calculations
- PTS-based mapping for VFR content
- Fraction arithmetic to avoid float accumulation
- Complete type annotations
- Mapping error tracking

## Reference

- docs/resolve_like_scene_cut_detection_implementation_plan_2026-06-04.md Section 6 (P6, P7)
- src/features/edit/core/timeline_frame_mapper.py
"""

from __future__ import annotations

import bisect
import logging
from dataclasses import dataclass
from fractions import Fraction
from typing import Any

from .models import (
    FrameTimePoint,
    SceneCutCandidate,
    SceneCutAnalysis,
    SourceFrameTimeIndex,
)

logger = logging.getLogger(__name__)


# =============================================================================
# Mapping Result
# =============================================================================

@dataclass(frozen=True)
class SourceToTimelineResult:
    """
    Result of mapping a source frame to timeline frame.

    Attributes:
        source_frame: Source frame number (input).
        timeline_frame: Calculated timeline frame number.
        source_seconds: Source time in seconds.
        timeline_seconds: Timeline time in seconds.
        error_frames: Estimated mapping error in frames.
        is_exact: Whether mapping is exact (error < 0.5 frame).
        warnings: Tuple of warning reasons if mapping is uncertain.
            Possible values: "vfr_uncertain", "fps_conversion", "speed_complex",
            "reverse_boundary", "no_time_index", "interpolated".
    """

    source_frame: int
    timeline_frame: int
    source_seconds: float
    timeline_seconds: float
    error_frames: float = 0.0
    is_exact: bool = True
    warnings: tuple[str, ...] = ()

    @property
    def has_warnings(self) -> bool:
        """Check if mapping has any warnings."""
        return len(self.warnings) > 0

    @property
    def is_within_tolerance(self) -> bool:
        """Check if error is within acceptable tolerance (1 frame)."""
        return self.error_frames <= 1.0


@dataclass(frozen=True)
class TimelineToSourceResult:
    """
    Result of mapping a timeline frame to source frame.

    Attributes:
        timeline_frame: Timeline frame number (input).
        source_frame: Calculated source frame number.
        timeline_seconds: Timeline time in seconds.
        source_seconds: Source time in seconds.
        error_frames: Estimated mapping error in frames.
        is_exact: Whether mapping is exact.
        warnings: Tuple of warning reasons.
    """

    timeline_frame: int
    source_frame: int
    timeline_seconds: float
    source_seconds: float
    error_frames: float = 0.0
    is_exact: bool = True
    warnings: tuple[str, ...] = ()

    @property
    def has_warnings(self) -> bool:
        """Check if mapping has any warnings."""
        return len(self.warnings) > 0


# =============================================================================
# Clip Source Mapper
# =============================================================================

@dataclass
class ClipMappingConfig:
    """
    Configuration for clip source-to-timeline mapping.

    Contains all parameters needed for accurate frame mapping.

    Attributes:
        clip_id: Clip identifier.
        asset_id: Source asset identifier.
        timeline_fps: Timeline frame rate (as Fraction).
        source_fps: Source frame rate (as Fraction, may be None for VFR).
        timeline_start: Clip start frame on timeline.
        timeline_duration: Clip duration on timeline in frames.
        source_start: First source frame used in clip.
        source_duration: Number of source frames used.
        speed: Playback speed multiplier (Fraction).
        reverse: Whether clip is reversed.
        source_time_index: PTS time index for VFR (optional).
    """

    clip_id: str
    asset_id: str
    timeline_fps: Fraction
    source_fps: Fraction | None
    timeline_start: int
    timeline_duration: int
    source_start: int
    source_duration: int
    speed: Fraction = Fraction(1, 1)
    reverse: bool = False
    source_time_index: SourceFrameTimeIndex | None = None

    def __post_init__(self) -> None:
        """Validate configuration."""
        if self.timeline_fps <= 0:
            raise ValueError(f"timeline_fps must be positive: {self.timeline_fps}")
        if self.timeline_duration <= 0:
            raise ValueError(f"timeline_duration must be positive: {self.timeline_duration}")
        if self.source_duration <= 0:
            raise ValueError(f"source_duration must be positive: {self.source_duration}")
        if self.source_start < 0:
            raise ValueError(f"source_start must be non-negative: {self.source_start}")
        if self.timeline_start < 0:
            raise ValueError(f"timeline_start must be non-negative: {self.timeline_start}")

    @property
    def source_end(self) -> int:
        """Last source frame (exclusive)."""
        return self.source_start + self.source_duration

    @property
    def timeline_end(self) -> int:
        """Last timeline frame (exclusive)."""
        return self.timeline_start + self.timeline_duration

    @property
    def is_vfr(self) -> bool:
        """Check if source is VFR (has time index)."""
        return self.source_time_index is not None and len(self.source_time_index.points) > 0

    @property
    def has_fps_conversion(self) -> bool:
        """Check if source and timeline fps differ."""
        if self.source_fps is None:
            return True  # Assume conversion needed if no source fps
        return self.source_fps != self.timeline_fps

    @staticmethod
    def from_clip_instance(clip: Any) -> ClipMappingConfig:
        """
        Create configuration from ClipInstance.

        Args:
            clip: ClipInstance from timeline_v2.py.

        Returns:
            ClipMappingConfig instance.
        """
        # Extract fps values
        timeline_fps = Fraction(
            clip._timeline_fps_num or 24,
            clip._timeline_fps_den or 1
        )
        source_fps = None
        if clip._source_fps_num is not None:
            source_fps = Fraction(
                clip._source_fps_num,
                clip._source_fps_den or 1
            )

        # Extract speed
        speed = Fraction(1, 1)
        if hasattr(clip, "effective_speed"):
            speed = Fraction(clip.effective_speed()).limit_denominator(10000)
        elif isinstance(clip.speed, (int, float)):
            speed = Fraction(clip.speed).limit_denominator(10000)

        # Check reverse
        reverse = False
        if hasattr(clip, "speed") and hasattr(clip.speed, "reverse"):
            reverse = clip.speed.reverse
        elif isinstance(clip.speed, (int, float)) and clip.speed < 0:
            reverse = True

        # Build time index from PTS if available
        source_time_index = None
        if clip._source_pts_index is not None:
            points = tuple(
                FrameTimePoint(
                    source_frame=i,
                    pts=None,  # PTS not stored in _source_pts_index
                    seconds=pts_val,
                )
                for i, pts_val in enumerate(clip._source_pts_index)
            )
            source_time_index = SourceFrameTimeIndex(
                asset_id=clip.asset_id,
                media_fingerprint="",  # Not available here
                points=points,
                average_fps=float(source_fps) if source_fps else None,
            )

        return ClipMappingConfig(
            clip_id=clip.clip_id,
            asset_id=clip.asset_id,
            timeline_fps=timeline_fps,
            source_fps=source_fps,
            timeline_start=clip.timeline_start,
            timeline_duration=clip.timeline_duration,
            source_start=clip.source_start,
            source_duration=clip.source_duration,
            speed=speed,
            reverse=reverse,
            source_time_index=source_time_index,
        )


class ClipSourceMapper:
    """
    Mapper for source-to-timeline and timeline-to-source frame conversions.

    Handles:
    - VFR content using PTS-based lookup
    - Different fps (23.976, 29.97, etc.)
    - Speed changes (0.5x, 2x, etc.)
    - Reverse clips

    Key principle: Mapping error should never exceed 1 frame.

    Usage:
        mapper = ClipSourceMapper(config)
        result = mapper.source_to_timeline(100)  # Map source frame 100
        result = mapper.timeline_to_source(50)   # Map timeline frame 50
    """

    def __init__(self, config: ClipMappingConfig) -> None:
        """
        Initialize mapper with configuration.

        Args:
            config: Clip mapping configuration.
        """
        self._config = config

        # Pre-calculate common ratios
        self._fps_ratio: Fraction = Fraction(1, 1)
        if config.source_fps is not None:
            self._fps_ratio = config.source_fps / config.timeline_fps

        # Pre-build time lookup tables for VFR
        self._source_seconds_list: list[float] = []
        self._source_frames_list: list[int] = []
        if config.is_vfr and config.source_time_index is not None:
            for point in config.source_time_index.points:
                # Only include frames within clip range
                if config.source_start <= point.source_frame < config.source_end:
                    self._source_seconds_list.append(point.seconds)
                    self._source_frames_list.append(point.source_frame)

    @property
    def config(self) -> ClipMappingConfig:
        """Configuration."""
        return self._config

    @property
    def clip_id(self) -> str:
        """Clip ID."""
        return self._config.clip_id

    @property
    def asset_id(self) -> str:
        """Asset ID."""
        return self._config.asset_id

    @property
    def timeline_start(self) -> int:
        """Timeline start frame."""
        return self._config.timeline_start

    @property
    def timeline_duration(self) -> int:
        """Timeline duration."""
        return self._config.timeline_duration

    @property
    def source_start(self) -> int:
        """Source start frame."""
        return self._config.source_start

    @property
    def source_duration(self) -> int:
        """Source duration."""
        return self._config.source_duration

    # ==================== Source to Timeline ====================

    def source_to_timeline(self, source_frame: int) -> SourceToTimelineResult:
        """
        Map source frame to timeline frame.

        Args:
            source_frame: Source frame number (absolute in source media).

        Returns:
            SourceToTimelineResult with mapping details.
        """
        config = self._config

        # Check if source frame is within clip range
        if not (config.source_start <= source_frame < config.source_end):
            logger.warning(
                f"Source frame {source_frame} outside clip range "
                f"[{config.source_start}, {config.source_end})"
            )
            # Return boundary result
            return self._boundary_source_to_timeline(source_frame)

        # Relative source frame within clip
        relative_source = source_frame - config.source_start

        # Handle VFR
        if config.is_vfr:
            return self._vfr_source_to_timeline(source_frame, relative_source)

        # CFR mapping
        return self._cfr_source_to_timeline(source_frame, relative_source)

    def _cfr_source_to_timeline(
        self,
        source_frame: int,
        relative_source: int,
    ) -> SourceToTimelineResult:
        """CFR source-to-timeline mapping."""
        config = self._config

        # Calculate source time
        if config.source_fps is not None:
            source_seconds = float(relative_source) / float(config.source_fps)
        else:
            # Fallback: use timeline fps
            source_seconds = float(relative_source) / float(config.timeline_fps)

        # Apply speed and fps ratio
        timeline_seconds = source_seconds / float(config.speed)
        timeline_relative = timeline_seconds * float(config.timeline_fps)

        # Handle reverse
        if config.reverse:
            # In reverse, source frames go from end to start. Reuse the
            # SAME conversion chain as the forward branch (source seconds
            # at source_fps, divided by speed, scaled to timeline fps) —
            # the timeline_fps terms cancelling out here previously
            # dropped the timeline_fps/source_fps ratio entirely, so
            # reverse+fps-conversion clips drifted ~1 frame per 1000
            # source frames and broke round-trip symmetry with
            # _cfr_timeline_to_source's reverse branch (AUD-18110).
            relative_source_reversed = config.source_duration - 1 - relative_source
            if config.source_fps is not None:
                reverse_source_seconds = (
                    float(relative_source_reversed) / float(config.source_fps)
                )
            else:
                reverse_source_seconds = (
                    float(relative_source_reversed) / float(config.timeline_fps)
                )
            timeline_seconds = reverse_source_seconds / float(config.speed)
            timeline_relative = timeline_seconds * float(config.timeline_fps)

        # Calculate timeline frame
        timeline_frame = int(round(timeline_relative)) + config.timeline_start

        # Clamp to valid range
        timeline_frame = max(
            config.timeline_start,
            min(timeline_frame, config.timeline_end - 1)
        )

        # Estimate error
        error_frames = abs(timeline_relative - (timeline_frame - config.timeline_start))
        is_exact = error_frames < 0.5

        # Build warnings
        warnings: list[str] = []
        if config.has_fps_conversion:
            warnings.append("fps_conversion")
        if config.speed != Fraction(1, 1):
            warnings.append("speed_complex")
        if config.reverse:
            warnings.append("reverse_boundary")

        return SourceToTimelineResult(
            source_frame=source_frame,
            timeline_frame=timeline_frame,
            source_seconds=source_seconds,
            timeline_seconds=timeline_seconds,
            error_frames=error_frames,
            is_exact=is_exact,
            warnings=tuple(warnings),
        )

    def _vfr_source_to_timeline(
        self,
        source_frame: int,
        relative_source: int,
    ) -> SourceToTimelineResult:
        """VFR source-to-timeline mapping using PTS."""
        config = self._config
        time_index = config.source_time_index

        if time_index is None:
            # Fallback to CFR
            return self._cfr_source_to_timeline(source_frame, relative_source)

        # Get source time from index
        source_seconds = time_index.source_frame_to_seconds(source_frame)

        # Calculate clip-local source time
        start_seconds = time_index.source_frame_to_seconds(config.source_start)
        local_source_seconds = source_seconds - start_seconds

        # Apply speed
        timeline_seconds = local_source_seconds / float(config.speed)

        # Handle reverse
        if config.reverse:
            end_seconds = time_index.source_frame_to_seconds(config.source_end - 1)
            total_source_seconds = end_seconds - start_seconds
            # Reverse: time goes from total to 0
            timeline_seconds = (total_source_seconds - local_source_seconds) / float(config.speed)

        # Calculate timeline frame
        timeline_relative = timeline_seconds * float(config.timeline_fps)
        timeline_frame = int(round(timeline_relative)) + config.timeline_start

        # Clamp to valid range
        timeline_frame = max(
            config.timeline_start,
            min(timeline_frame, config.timeline_end - 1)
        )

        # Estimate error (VFR has more uncertainty)
        error_frames = abs(timeline_relative - (timeline_frame - config.timeline_start))
        is_exact = error_frames < 0.5

        # Build warnings
        warnings: list[str] = ["vfr_uncertain"]
        if config.has_fps_conversion:
            warnings.append("fps_conversion")
        if config.speed != Fraction(1, 1):
            warnings.append("speed_complex")
        if config.reverse:
            warnings.append("reverse_boundary")

        return SourceToTimelineResult(
            source_frame=source_frame,
            timeline_frame=timeline_frame,
            source_seconds=source_seconds,
            timeline_seconds=timeline_seconds,
            error_frames=error_frames,
            is_exact=is_exact,
            warnings=tuple(warnings),
        )

    def _boundary_source_to_timeline(self, source_frame: int) -> SourceToTimelineResult:
        """Handle source frame outside clip range."""
        config = self._config

        if source_frame < config.source_start:
            return SourceToTimelineResult(
                source_frame=source_frame,
                timeline_frame=config.timeline_start,
                source_seconds=0.0,
                timeline_seconds=0.0,
                error_frames=abs(source_frame - config.source_start),
                is_exact=False,
                warnings=("outside_range",),
            )
        else:
            return SourceToTimelineResult(
                source_frame=source_frame,
                timeline_frame=config.timeline_end - 1,
                source_seconds=float(config.source_duration) / float(config.timeline_fps),
                timeline_seconds=float(config.timeline_duration) / float(config.timeline_fps),
                error_frames=abs(source_frame - config.source_end),
                is_exact=False,
                warnings=("outside_range",),
            )

    # ==================== Timeline to Source ====================

    def timeline_to_source(self, timeline_frame: int) -> TimelineToSourceResult:
        """
        Map timeline frame to source frame.

        Args:
            timeline_frame: Timeline frame number (absolute).

        Returns:
            TimelineToSourceResult with mapping details.
        """
        config = self._config

        # Check if timeline frame is within clip range
        if not (config.timeline_start <= timeline_frame < config.timeline_end):
            logger.warning(
                f"Timeline frame {timeline_frame} outside clip range "
                f"[{config.timeline_start}, {config.timeline_end})"
            )
            return self._boundary_timeline_to_source(timeline_frame)

        # Relative timeline frame
        relative_timeline = timeline_frame - config.timeline_start

        # Handle VFR
        if config.is_vfr:
            return self._vfr_timeline_to_source(timeline_frame, relative_timeline)

        # CFR mapping
        return self._cfr_timeline_to_source(timeline_frame, relative_timeline)

    def _cfr_timeline_to_source(
        self,
        timeline_frame: int,
        relative_timeline: int,
    ) -> TimelineToSourceResult:
        """CFR timeline-to-source mapping."""
        config = self._config

        # Calculate timeline time
        timeline_seconds = float(relative_timeline) / float(config.timeline_fps)

        # Apply speed
        source_seconds = timeline_seconds * float(config.speed)

        # Handle reverse
        if config.reverse:
            # Reverse: timeline 0 maps to source end
            total_timeline_seconds = float(config.timeline_duration) / float(config.timeline_fps)
            source_seconds = (total_timeline_seconds - timeline_seconds) * float(config.speed)

        # Calculate source frame
        if config.source_fps is not None:
            relative_source = source_seconds * float(config.source_fps)
        else:
            relative_source = source_seconds * float(config.timeline_fps)

        source_frame = int(round(relative_source)) + config.source_start

        # Clamp to valid range
        source_frame = max(
            config.source_start,
            min(source_frame, config.source_end - 1)
        )

        # Estimate error
        error_frames = abs(relative_source - (source_frame - config.source_start))
        is_exact = error_frames < 0.5

        # Build warnings
        warnings: list[str] = []
        if config.has_fps_conversion:
            warnings.append("fps_conversion")
        if config.speed != Fraction(1, 1):
            warnings.append("speed_complex")
        if config.reverse:
            warnings.append("reverse_boundary")

        return TimelineToSourceResult(
            timeline_frame=timeline_frame,
            source_frame=source_frame,
            timeline_seconds=timeline_seconds,
            source_seconds=source_seconds,
            error_frames=error_frames,
            is_exact=is_exact,
            warnings=tuple(warnings),
        )

    def _vfr_timeline_to_source(
        self,
        timeline_frame: int,
        relative_timeline: int,
    ) -> TimelineToSourceResult:
        """VFR timeline-to-source mapping using PTS."""
        config = self._config
        time_index = config.source_time_index

        if time_index is None or not self._source_seconds_list:
            return self._cfr_timeline_to_source(timeline_frame, relative_timeline)

        # Calculate timeline time
        timeline_seconds = float(relative_timeline) / float(config.timeline_fps)

        # Apply speed
        source_seconds_local = timeline_seconds * float(config.speed)

        # Handle reverse
        if config.reverse:
            total_timeline_seconds = float(config.timeline_duration) / float(config.timeline_fps)
            source_seconds_local = (total_timeline_seconds - timeline_seconds) * float(config.speed)

        # Add clip start offset
        start_seconds = time_index.source_frame_to_seconds(config.source_start)
        target_source_seconds = start_seconds + source_seconds_local

        # Bisect to find nearest source frame
        idx = bisect.bisect_left(self._source_seconds_list, target_source_seconds)

        # Initialize error_frames
        error_frames = 0.0

        if idx == 0:
            source_frame = self._source_frames_list[0] if self._source_frames_list else config.source_start
            # No error calculation needed for first frame
        elif idx >= len(self._source_seconds_list):
            source_frame = self._source_frames_list[-1] if self._source_frames_list else config.source_end - 1
            # No error calculation needed for last frame
        else:
            # Compare distances to find nearest
            before_seconds = self._source_seconds_list[idx - 1]
            after_seconds = self._source_seconds_list[idx]
            before_frame = self._source_frames_list[idx - 1]
            after_frame = self._source_frames_list[idx]

            if abs(target_source_seconds - before_seconds) <= abs(target_source_seconds - after_seconds):
                source_frame = before_frame
                actual_seconds = before_seconds
            else:
                source_frame = after_frame
                actual_seconds = after_seconds

            # Calculate error based on actual vs target seconds
            error_seconds = abs(actual_seconds - target_source_seconds)
            if config.source_fps:
                error_frames = error_seconds * float(config.source_fps)
            else:
                error_frames = error_seconds * float(config.timeline_fps)

        # Clamp
        source_frame = max(config.source_start, min(source_frame, config.source_end - 1))

        # Calculate source time
        actual_source_seconds = time_index.source_frame_to_seconds(source_frame)

        # Build warnings
        warnings: list[str] = ["vfr_uncertain"]
        if config.has_fps_conversion:
            warnings.append("fps_conversion")
        if config.speed != Fraction(1, 1):
            warnings.append("speed_complex")
        if config.reverse:
            warnings.append("reverse_boundary")

        return TimelineToSourceResult(
            timeline_frame=timeline_frame,
            source_frame=source_frame,
            timeline_seconds=timeline_seconds,
            source_seconds=actual_source_seconds,
            error_frames=error_frames,
            is_exact=error_frames < 0.5,
            warnings=tuple(warnings),
        )

    def _boundary_timeline_to_source(self, timeline_frame: int) -> TimelineToSourceResult:
        """Handle timeline frame outside clip range."""
        config = self._config

        if timeline_frame < config.timeline_start:
            return TimelineToSourceResult(
                timeline_frame=timeline_frame,
                source_frame=config.source_start,
                timeline_seconds=0.0,
                source_seconds=0.0,
                error_frames=abs(timeline_frame - config.timeline_start),
                is_exact=False,
                warnings=("outside_range",),
            )
        else:
            return TimelineToSourceResult(
                timeline_frame=timeline_frame,
                source_frame=config.source_end - 1,
                timeline_seconds=float(config.timeline_duration) / float(config.timeline_fps),
                source_seconds=float(config.source_duration) / float(config.timeline_fps or 24),
                error_frames=abs(timeline_frame - config.timeline_end),
                is_exact=False,
                warnings=("outside_range",),
            )

    # ==================== Batch Operations ====================

    def map_candidates(
        self,
        candidates: tuple[SceneCutCandidate, ...],
    ) -> tuple[SceneCutCandidate, ...]:
        """
        Map all candidates to timeline frames.

        Updates each candidate with timeline_frame and adds warnings
        to uncertain mappings.

        Args:
            candidates: Tuple of SceneCutCandidate.

        Returns:
            Updated candidates with timeline frames.
        """
        updated: list[SceneCutCandidate] = []

        for candidate in candidates:
            result = self.source_to_timeline(candidate.source_frame)

            # Combine existing reasons with mapping warnings
            combined_reasons = set(candidate.reasons)
            if result.has_warnings:
                for warning in result.warnings:
                    if warning not in ("fps_conversion", "reverse_boundary"):
                        # Skip expected warnings
                        combined_reasons.add(warning)

            # Create updated candidate
            updated_candidate = SceneCutCandidate(
                source_frame=candidate.source_frame,
                pts=candidate.pts,
                seconds=candidate.seconds,
                timeline_frame=result.timeline_frame,
                score=candidate.score,
                confidence=candidate.confidence,
                kind=candidate.kind,
                decision=candidate.decision,
                metrics=candidate.metrics,
                reasons=tuple(combined_reasons),
                before_thumb_cache_key=candidate.before_thumb_cache_key,
                after_thumb_cache_key=candidate.after_thumb_cache_key,
            )
            updated.append(updated_candidate)

        return tuple(updated)

    def map_analysis(self, analysis: SceneCutAnalysis) -> SceneCutAnalysis:
        """
        Map all candidates in an analysis to timeline frames.

        Args:
            analysis: SceneCutAnalysis to map.

        Returns:
            New analysis with mapped candidates.
        """
        mapped_candidates = self.map_candidates(analysis.candidates)

        return SceneCutAnalysis(
            analysis_id=analysis.analysis_id,
            asset_id=analysis.asset_id,
            source_path=analysis.source_path,
            clip_id=analysis.clip_id,
            settings=analysis.settings,
            media_fingerprint=analysis.media_fingerprint,
            source_time_index=analysis.source_time_index,
            candidates=mapped_candidates,
            created_at=analysis.created_at,
            detector_version=analysis.detector_version,
        )

    def map_source_frames(
        self,
        source_frames: tuple[int, ...],
    ) -> tuple[SourceToTimelineResult, ...]:
        """
        Map multiple source frames to timeline.

        Args:
            source_frames: Tuple of source frame numbers.

        Returns:
            Tuple of mapping results.
        """
        return tuple(self.source_to_timeline(f) for f in source_frames)

    def map_timeline_frames(
        self,
        timeline_frames: tuple[int, ...],
    ) -> tuple[TimelineToSourceResult, ...]:
        """
        Map multiple timeline frames to source.

        Args:
            timeline_frames: Tuple of timeline frame numbers.

        Returns:
            Tuple of mapping results.
        """
        return tuple(self.timeline_to_source(f) for f in timeline_frames)


# =============================================================================
# Factory Functions
# =============================================================================

def create_mapper_from_clip(clip: Any) -> ClipSourceMapper:
    """
    Create mapper from ClipInstance.

    Args:
        clip: ClipInstance from timeline_v2.py.

    Returns:
        ClipSourceMapper instance.
    """
    config = ClipMappingConfig.from_clip_instance(clip)
    return ClipSourceMapper(config)


def create_mapper(
    clip_id: str,
    asset_id: str,
    timeline_fps: float,
    source_fps: float | None,
    timeline_start: int,
    timeline_duration: int,
    source_start: int,
    source_duration: int,
    speed: float = 1.0,
    reverse: bool = False,
    source_pts_index: SourceFrameTimeIndex | None = None,
) -> ClipSourceMapper:
    """
    Create mapper with explicit parameters.

    Args:
        clip_id: Clip identifier.
        asset_id: Asset identifier.
        timeline_fps: Timeline frame rate.
        source_fps: Source frame rate (may be None for VFR).
        timeline_start: Timeline start frame.
        timeline_duration: Timeline duration in frames.
        source_start: Source start frame.
        source_duration: Source duration in frames.
        speed: Playback speed (1.0 = normal).
        reverse: Whether clip is reversed.
        source_pts_index: Time index for VFR content.

    Returns:
        ClipSourceMapper instance.
    """
    config = ClipMappingConfig(
        clip_id=clip_id,
        asset_id=asset_id,
        timeline_fps=Fraction(timeline_fps).limit_denominator(10000),
        source_fps=Fraction(source_fps).limit_denominator(10000) if source_fps else None,
        timeline_start=timeline_start,
        timeline_duration=timeline_duration,
        source_start=source_start,
        source_duration=source_duration,
        speed=Fraction(speed).limit_denominator(10000),
        reverse=reverse,
        source_time_index=source_pts_index,
    )
    return ClipSourceMapper(config)


# =============================================================================
# FPS Conversion Helpers
# =============================================================================

def fps_to_fraction(fps: float) -> Fraction:
    """
    Convert fps to Fraction with proper handling of common NTSC rates.

    Handles:
    - 23.976 -> 24000/1001
    - 29.97 -> 30000/1001
    - 59.94 -> 60000/1001

    Args:
        fps: Frame rate as float.

    Returns:
        Fraction representation.
    """
    # Common NTSC rates
    if abs(fps - 23.976) < 0.001:
        return Fraction(24000, 1001)
    elif abs(fps - 29.97) < 0.001:
        return Fraction(30000, 1001)
    elif abs(fps - 59.94) < 0.001:
        return Fraction(60000, 1001)
    elif abs(fps - 23.98) < 0.001:
        return Fraction(24000, 1001)
    elif abs(fps - 29.98) < 0.001:
        return Fraction(30000, 1001)

    # General case
    return Fraction(fps).limit_denominator(10000)


def estimate_fps_conversion_error(
    source_fps: float,
    timeline_fps: float,
    duration_frames: int,
) -> float:
    """
    Estimate cumulative error from fps conversion.

    Args:
        source_fps: Source frame rate.
        timeline_fps: Timeline frame rate.
        duration_frames: Duration in source frames.

    Returns:
        Estimated error in frames.
    """
    source_frac = fps_to_fraction(source_fps)
    timeline_frac = fps_to_fraction(timeline_fps)

    duration_seconds = Fraction(duration_frames) / source_frac
    expected_timeline_frames = duration_seconds * timeline_frac

    # Float calculation
    float_expected = float(duration_frames) / source_fps * timeline_fps
    error = abs(float(expected_timeline_frames) - float_expected)

    return error


# =============================================================================
# Utility Functions
# =============================================================================

def is_vfr_content(source_fps: float | None, pts_index: Any) -> bool:
    """
    Check if content is VFR.

    Args:
        source_fps: Source fps (None indicates VFR).
        pts_index: PTS index (SourceFrameTimeIndex or None, presence indicates VFR).

    Returns:
        True if content is VFR.
    """
    # Check if pts_index has points
    if pts_index is not None:
        if hasattr(pts_index, 'points') and len(pts_index.points) > 0:
            return True
    if source_fps is None:
        return True
    return False


def validate_mapping_tolerance(
    mapper: ClipSourceMapper,
    source_frames: tuple[int, ...],
    tolerance: float = 1.0,
) -> tuple[bool, list[int]]:
    """
    Validate that all mappings are within tolerance.

    Args:
        mapper: ClipSourceMapper instance.
        source_frames: Source frames to test.
        tolerance: Maximum allowed error in frames.

    Returns:
        Tuple of (all_within_tolerance, list of frames exceeding tolerance).
    """
    exceeding: list[int] = []

    for source_frame in source_frames:
        result = mapper.source_to_timeline(source_frame)
        if result.error_frames > tolerance:
            exceeding.append(source_frame)

    return (len(exceeding) == 0, exceeding)