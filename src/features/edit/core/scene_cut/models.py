"""
Scene cut detection data models.

This module defines the core data structures for scene cut detection,
including detection settings, frame time points, metrics, candidates,
analysis results, and subclip specifications.

Design principles:
- Immutable data classes (frozen=True)
- Complete type annotations
- No PyQt dependencies, pure Python data models
- Serializable for caching

Reference:
- docs/resolve_like_scene_cut_detection_implementation_plan_2026-06-04.md Section 4
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any
import hashlib
import time


class SceneCutKind(Enum):
    """Classification of detected scene cut type."""
    
    HARD_CUT = "hard_cut"
    """Abrupt cut between two distinct scenes."""
    
    SOFT_TRANSITION = "soft_transition"
    """Gradual transition like dissolve or fade."""
    
    BLACK_GAP = "black_gap"
    """Black frame gap between scenes."""
    
    FLASH = "flash"
    """Single or few frame flash, likely false positive."""
    
    FAST_MOTION = "fast_motion"
    """High motion sequence, likely false positive."""
    
    UNKNOWN = "unknown"
    """Unable to classify the cut type."""


class SceneCutDecision(Enum):
    """User review decision for a scene cut candidate."""
    
    PENDING = "pending"
    """Not yet reviewed."""
    
    ACCEPTED = "accepted"
    """User confirmed this cut."""
    
    REJECTED = "rejected"
    """User rejected this cut."""


@dataclass(frozen=True)
class SceneCutSettings:
    """
    Configuration for scene cut detection.
    
    All parameters have reasonable defaults that can be adjusted via UI.
    
    Attributes:
        sensitivity: Detection sensitivity (0.35-0.75), default 0.55.
            Lower values = more sensitive (more candidates).
            0.35: Conservative, only strong hard cuts.
            0.55: Default, balanced recall and precision.
            0.75: Aggressive, more candidates need review.
        min_scene_frames: Minimum frames between cuts, default 8.
        sample_width: Width for downsampled frame analysis, default 256.
        max_sample_height: Maximum height for downsampled frames, default 144.
        use_proxy: Whether to use proxy media if available, default True.
        detect_transitions: Whether to detect soft transitions, default True.
        suppress_flash: Whether to suppress flash false positives, default True.
        suppress_fast_motion: Whether to suppress fast motion false positives, default True.
        suppress_black_fades: Whether to suppress black fade cuts, default True.
        adaptive_threshold: Whether to use adaptive thresholding, default True.
        start_source_frame: First frame to analyze (inclusive), default 0.
        end_source_frame: Last frame to analyze (exclusive), None = end of media.
    """
    
    sensitivity: float = 0.55
    min_scene_frames: int = 8
    sample_width: int = 256
    max_sample_height: int = 144
    use_proxy: bool = True
    detect_transitions: bool = True
    suppress_flash: bool = True
    suppress_fast_motion: bool = True
    suppress_black_fades: bool = True
    adaptive_threshold: bool = True
    start_source_frame: int = 0
    end_source_frame: int | None = None

    # Thresholds for false positive guards (used by filters.py)
    rolling_window_size: int = 30
    black_score_threshold: float = 0.7
    motion_score_threshold: float = 0.5
    flash_score_threshold: float = 0.3
    transition_frame_threshold: int = 12
    
    def __post_init__(self) -> None:
        """Validate settings after initialization."""
        if not 0.35 <= self.sensitivity <= 0.75:
            raise ValueError(
                f"sensitivity must be between 0.35 and 0.75, got {self.sensitivity}"
            )
        if self.min_scene_frames < 2:
            raise ValueError(
                f"min_scene_frames must be >= 2, got {self.min_scene_frames}"
            )
        if self.sample_width < 64 or self.sample_width > 512:
            raise ValueError(
                f"sample_width must be between 64 and 512, got {self.sample_width}"
            )
        if self.max_sample_height < 36 or self.max_sample_height > 288:
            raise ValueError(
                f"max_sample_height must be between 36 and 288, got {self.max_sample_height}"
            )
        if self.start_source_frame < 0:
            raise ValueError(
                f"start_source_frame must be >= 0, got {self.start_source_frame}"
            )
        if self.end_source_frame is not None:
            if self.end_source_frame <= self.start_source_frame:
                raise ValueError(
                    f"end_source_frame ({self.end_source_frame}) must be > "
                    f"start_source_frame ({self.start_source_frame})"
                )
    
    def to_hash(self) -> str:
        """Generate a stable hash for cache key purposes.

        Must cover EVERY settings field that can change detection results.
        The five false-positive-suppression thresholds below materially
        drive filters.py; omitting them kept stale cache entries valid
        after threshold changes (AUD-18010). A regression test asserts
        parity with the field set (tests/edit/test_fix_wd10g60_aud.py).
        """
        data = (
            f"{self.sensitivity:.4f}"
            f"|{self.min_scene_frames}"
            f"|{self.sample_width}x{self.max_sample_height}"
            f"|{self.use_proxy}"
            f"|{self.detect_transitions}"
            f"|{self.suppress_flash}"
            f"|{self.suppress_fast_motion}"
            f"|{self.suppress_black_fades}"
            f"|{self.adaptive_threshold}"
            f"|{self.rolling_window_size}"
            f"|{self.black_score_threshold:.4f}"
            f"|{self.motion_score_threshold:.4f}"
            f"|{self.flash_score_threshold:.4f}"
            f"|{self.transition_frame_threshold}"
            f"|{self.start_source_frame}-{self.end_source_frame or 'end'}"
        )
        return hashlib.sha256(data.encode()).hexdigest()[:16]
    
    def with_sensitivity(self, sensitivity: float) -> SceneCutSettings:
        """Create a new instance with modified sensitivity."""
        return SceneCutSettings(
            sensitivity=sensitivity,
            min_scene_frames=self.min_scene_frames,
            sample_width=self.sample_width,
            max_sample_height=self.max_sample_height,
            use_proxy=self.use_proxy,
            detect_transitions=self.detect_transitions,
            suppress_flash=self.suppress_flash,
            suppress_fast_motion=self.suppress_fast_motion,
            suppress_black_fades=self.suppress_black_fades,
            adaptive_threshold=self.adaptive_threshold,
            start_source_frame=self.start_source_frame,
            end_source_frame=self.end_source_frame,
            rolling_window_size=self.rolling_window_size,
            black_score_threshold=self.black_score_threshold,
            motion_score_threshold=self.motion_score_threshold,
            flash_score_threshold=self.flash_score_threshold,
            transition_frame_threshold=self.transition_frame_threshold,
        )
    
    def with_range(
        self,
        start_source_frame: int,
        end_source_frame: int | None
    ) -> SceneCutSettings:
        """Create a new instance with modified detection range."""
        return SceneCutSettings(
            sensitivity=self.sensitivity,
            min_scene_frames=self.min_scene_frames,
            sample_width=self.sample_width,
            max_sample_height=self.max_sample_height,
            use_proxy=self.use_proxy,
            detect_transitions=self.detect_transitions,
            suppress_flash=self.suppress_flash,
            suppress_fast_motion=self.suppress_fast_motion,
            suppress_black_fades=self.suppress_black_fades,
            adaptive_threshold=self.adaptive_threshold,
            start_source_frame=start_source_frame,
            end_source_frame=end_source_frame,
            rolling_window_size=self.rolling_window_size,
            black_score_threshold=self.black_score_threshold,
            motion_score_threshold=self.motion_score_threshold,
            flash_score_threshold=self.flash_score_threshold,
            transition_frame_threshold=self.transition_frame_threshold,
        )
    
    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "sensitivity": self.sensitivity,
            "min_scene_frames": self.min_scene_frames,
            "sample_width": self.sample_width,
            "max_sample_height": self.max_sample_height,
            "use_proxy": self.use_proxy,
            "detect_transitions": self.detect_transitions,
            "suppress_flash": self.suppress_flash,
            "suppress_fast_motion": self.suppress_fast_motion,
            "suppress_black_fades": self.suppress_black_fades,
            "adaptive_threshold": self.adaptive_threshold,
            "start_source_frame": self.start_source_frame,
            "end_source_frame": self.end_source_frame,
            "rolling_window_size": self.rolling_window_size,
            "black_score_threshold": self.black_score_threshold,
            "motion_score_threshold": self.motion_score_threshold,
            "flash_score_threshold": self.flash_score_threshold,
            "transition_frame_threshold": self.transition_frame_threshold,
        }
    
    @staticmethod
    def from_dict(data: dict[str, Any]) -> SceneCutSettings:
        """Create instance from dictionary."""
        return SceneCutSettings(
            sensitivity=data.get("sensitivity", 0.55),
            min_scene_frames=data.get("min_scene_frames", 8),
            sample_width=data.get("sample_width", 256),
            max_sample_height=data.get("max_sample_height", 144),
            use_proxy=data.get("use_proxy", True),
            detect_transitions=data.get("detect_transitions", True),
            suppress_flash=data.get("suppress_flash", True),
            suppress_fast_motion=data.get("suppress_fast_motion", True),
            suppress_black_fades=data.get("suppress_black_fades", True),
            adaptive_threshold=data.get("adaptive_threshold", True),
            start_source_frame=data.get("start_source_frame", 0),
            end_source_frame=data.get("end_source_frame"),
            rolling_window_size=data.get("rolling_window_size", 30),
            black_score_threshold=data.get("black_score_threshold", 0.7),
            motion_score_threshold=data.get("motion_score_threshold", 0.5),
            flash_score_threshold=data.get("flash_score_threshold", 0.3),
            transition_frame_threshold=data.get("transition_frame_threshold", 12),
        )


@dataclass(frozen=True)
class FrameTimePoint:
    """
    Time mapping for a single source frame.
    
    Captures multiple time representations for accurate VFR handling.
    Essential for mapping frame indices to seconds when PTS is non-linear.
    
    Attributes:
        source_frame: Zero-based frame index in source media.
        pts: Presentation timestamp from container, may be None for CFR.
        seconds: Time in seconds from media start.
        duration_seconds: Frame duration in seconds, None if unknown.
        is_keyframe: Whether this frame is a keyframe.
        is_duplicate_time: Whether PTS duplicates a previous frame (VFR anomaly).
    """
    
    source_frame: int
    pts: int | None
    seconds: float
    duration_seconds: float | None = None
    is_keyframe: bool = False
    is_duplicate_time: bool = False
    
    def __post_init__(self) -> None:
        """Validate frame time point after initialization."""
        if self.source_frame < 0:
            raise ValueError(f"source_frame must be >= 0, got {self.source_frame}")
        if self.seconds < 0.0:
            raise ValueError(f"seconds must be >= 0.0, got {self.seconds}")
    
    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "source_frame": self.source_frame,
            "pts": self.pts,
            "seconds": self.seconds,
            "duration_seconds": self.duration_seconds,
            "is_keyframe": self.is_keyframe,
            "is_duplicate_time": self.is_duplicate_time,
        }
    
    @staticmethod
    def from_dict(data: dict[str, Any]) -> FrameTimePoint:
        """Create instance from dictionary."""
        return FrameTimePoint(
            source_frame=data["source_frame"],
            pts=data.get("pts"),
            seconds=data["seconds"],
            duration_seconds=data.get("duration_seconds"),
            is_keyframe=data.get("is_keyframe", False),
            is_duplicate_time=data.get("is_duplicate_time", False),
        )


@dataclass(frozen=True)
class SourceFrameTimeIndex:
    """
    Time index for mapping source frames to seconds and back.
    
    Essential for accurate VFR (Variable Frame Rate) handling where
    frame indices don't map linearly to time.
    
    Attributes:
        asset_id: Unique identifier of the source media asset.
        media_fingerprint: Hash of media metadata for cache validation.
        points: Tuple of frame time points, sorted by source_frame.
        average_fps: Average frames per second, for fallback calculations.
    """
    
    asset_id: str
    media_fingerprint: str
    points: tuple[FrameTimePoint, ...]
    average_fps: float | None = None
    
    def __post_init__(self) -> None:
        """Validate index after initialization."""
        if not self.asset_id:
            raise ValueError("asset_id cannot be empty")
        if self.average_fps is not None and self.average_fps <= 0.0:
            raise ValueError(f"average_fps must be > 0.0, got {self.average_fps}")

    # ── R04（2026-11-09 复查）：导入 PTS 秒表 → 索引 ──────────────────

    @staticmethod
    def from_pts_seconds(asset_id: str, media_fingerprint: str,
                         seconds_list: Any) -> "SourceFrameTimeIndex | None":
        """由导入器保存的逐帧 PTS 秒表（``metadata["pts_index"]``）构建。

        清洗政策（显式、可诊断，不静默接受异常时间轴）：

        - 非有限值（NaN/±inf）与非数值条目：该点丢弃（帧经插值访问）；
        - 秒列必须严格递增：重复/回退时间戳只保留首个（VFR 异常包）；
        - 清洗后不足 2 点 → 返回 ``None``（调用方显式回退 CFR，不伪称
          精确 VFR）。

        ``average_fps`` 由保留点的首末秒距推导（外推 fallback 用）。
        """
        import math

        points: list[FrameTimePoint] = []
        last_seconds: float | None = None
        try:
            iterator = list(seconds_list)
        except TypeError:
            return None
        for frame, raw in enumerate(iterator):
            try:
                value = float(raw)
            except (TypeError, ValueError):
                continue
            if not math.isfinite(value) or value < 0.0:
                continue
            if last_seconds is not None and value <= last_seconds:
                continue  # 重复/回退 PTS：保留首个
            points.append(FrameTimePoint(
                source_frame=frame, pts=None, seconds=value))
            last_seconds = value
        if len(points) < 2:
            return None
        span = points[-1].seconds - points[0].seconds
        average_fps = ((len(points) - 1) / span if span > 0.0 else None)
        return SourceFrameTimeIndex(
            asset_id=asset_id, media_fingerprint=media_fingerprint,
            points=tuple(points), average_fps=average_fps)

    def seconds_column(self) -> tuple[float, ...]:
        """预计算秒列（懒缓存；供 O(log n) 二分，不逐点属性访问）。

        反查（秒→帧）复用本列：单次查询 O(log n)，不随全片长度线性
        增长（R04 之前 Color 侧反查每次遍历全部 points 构造秒列）。
        """
        cached = self.__dict__.get("_seconds_column_cache")
        if cached is None:
            cached = tuple(float(p.seconds) for p in self.points)
            object.__setattr__(self, "_seconds_column_cache", cached)
        return cached

    
    def source_frame_to_seconds(self, source_frame: int) -> float:
        """
        Convert source frame index to seconds.
        
        For VFR media, uses exact PTS mapping. Falls back to average FPS
        if frame not found in index.
        
        Args:
            source_frame: Zero-based frame index.
            
        Returns:
            Time in seconds from media start.
            
        Raises:
            ValueError: If no time points available and no average_fps fallback.
        """
        points = self.points
        if not points:
            if self.average_fps is not None:
                return source_frame / self.average_fps
            raise ValueError("No time points available and no average_fps fallback")
        
        # Binary search through points
        low, high = 0, len(points) - 1
        while low <= high:
            mid = (low + high) // 2
            point = points[mid]
            if point.source_frame == source_frame:
                return point.seconds
            elif point.source_frame < source_frame:
                low = mid + 1
            else:
                high = mid - 1
        
        # Frame not found, interpolate using nearest points
        if low >= len(points):
            # After last known point, extrapolate
            last = points[-1]
            if self.average_fps is not None:
                return last.seconds + (source_frame - last.source_frame) / self.average_fps
            return last.seconds
        if high < 0:
            # Before first known point
            first = points[0]
            if self.average_fps is not None:
                return first.seconds - (first.source_frame - source_frame) / self.average_fps
            return 0.0
        
        # Interpolate between adjacent points
        prev_point = points[high]
        next_point = points[low]
        frame_ratio = (source_frame - prev_point.source_frame) / (
            next_point.source_frame - prev_point.source_frame
        )
        return prev_point.seconds + frame_ratio * (next_point.seconds - prev_point.seconds)
    
    def seconds_to_nearest_source_frame(self, seconds: float) -> int:
        """
        Convert seconds to nearest source frame index.

        R04：在缓存的秒列（:meth:`seconds_column`）上二分——单次查询
        O(log n)，不逐点属性访问，不随全片长度线性增长。

        Args:
            seconds: Time in seconds from media start.

        Returns:
            Zero-based frame index closest to the given time.

        Raises:
            ValueError: If no time points available and no average_fps fallback.
        """
        points = self.points
        if not points:
            if self.average_fps is not None:
                return max(0, int(seconds * self.average_fps + 0.5))
            raise ValueError("No time points available and no average_fps fallback")

        from bisect import bisect_left

        times = self.seconds_column()
        low = bisect_left(times, seconds)  # 首个 >= seconds 的点

        # Check neighbors for closest（与旧实现同语义，含边界外推）
        best_idx = min(low, len(points) - 1)
        best_diff = abs(times[best_idx] - seconds)

        if best_idx > 0:
            prev_diff = abs(times[best_idx - 1] - seconds)
            if prev_diff < best_diff:
                best_idx -= 1
                best_diff = prev_diff

        if best_idx < len(points) - 1:
            next_diff = abs(times[best_idx + 1] - seconds)
            if next_diff < best_diff:
                best_idx += 1

        return points[best_idx].source_frame
    
    def get_point_for_frame(self, source_frame: int) -> FrameTimePoint | None:
        """
        Get the exact time point for a source frame.
        
        Args:
            source_frame: Zero-based frame index.
            
        Returns:
            FrameTimePoint if found, None otherwise.
        """
        points = self.points
        low, high = 0, len(points) - 1
        while low <= high:
            mid = (low + high) // 2
            point = points[mid]
            if point.source_frame == source_frame:
                return point
            elif point.source_frame < source_frame:
                low = mid + 1
            else:
                high = mid - 1
        return None
    
    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "asset_id": self.asset_id,
            "media_fingerprint": self.media_fingerprint,
            "points": [p.to_dict() for p in self.points],
            "average_fps": self.average_fps,
        }
    
    @staticmethod
    def from_dict(data: dict[str, Any]) -> SourceFrameTimeIndex:
        """Create instance from dictionary."""
        points = tuple(
            FrameTimePoint.from_dict(p)
            for p in data.get("points", [])
        )
        return SourceFrameTimeIndex(
            asset_id=data["asset_id"],
            media_fingerprint=data.get("media_fingerprint", ""),
            points=points,
            average_fps=data.get("average_fps"),
        )


@dataclass(frozen=True)
class SceneCutMetrics:
    """
    Detailed metrics for a scene cut candidate.
    
    All metric values are normalized to [0.0, 1.0] range where higher
    values indicate stronger detection signals.
    
    Attributes:
        luma_delta: Luminance change between frames.
        hist_delta: Histogram difference (chi-square or similar).
        edge_delta: Edge structure change.
        color_delta: Color distribution change.
        black_score: Likelihood of black frame gap.
        flash_score: Likelihood of flash artifact.
        motion_score: Overall motion between frames.
        combined_score: Weighted combination of metrics.
            Formula: 0.35*luma + 0.30*hist + 0.20*edge + 0.10*color
            (AUD-18015: motion_score is NOT part of the combination —
            metrics.py weights sum to these four terms only; the pHash
            weight documented there was folded into color_delta).
    """
    
    luma_delta: float
    hist_delta: float
    edge_delta: float
    color_delta: float
    black_score: float
    flash_score: float
    motion_score: float
    combined_score: float
    
    def __post_init__(self) -> None:
        """Validate metrics after initialization."""
        for name, value in [
            ("luma_delta", self.luma_delta),
            ("hist_delta", self.hist_delta),
            ("edge_delta", self.edge_delta),
            ("color_delta", self.color_delta),
            ("black_score", self.black_score),
            ("flash_score", self.flash_score),
            ("motion_score", self.motion_score),
            ("combined_score", self.combined_score),
        ]:
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"{name} must be in [0.0, 1.0], got {value}")
    
    def is_likely_flash(self, threshold: float = 0.6) -> bool:
        """Check if metrics indicate a flash artifact."""
        return self.flash_score > threshold
    
    def is_likely_black_gap(self, threshold: float = 0.7) -> bool:
        """Check if metrics indicate a black frame gap."""
        return self.black_score > threshold
    
    def is_likely_fast_motion(self, threshold: float = 0.5) -> bool:
        """Check if metrics indicate fast motion without cut."""
        return self.motion_score > threshold and self.combined_score < 0.4
    
    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "luma_delta": self.luma_delta,
            "hist_delta": self.hist_delta,
            "edge_delta": self.edge_delta,
            "color_delta": self.color_delta,
            "black_score": self.black_score,
            "flash_score": self.flash_score,
            "motion_score": self.motion_score,
            "combined_score": self.combined_score,
        }
    
    @staticmethod
    def from_dict(data: dict[str, Any]) -> SceneCutMetrics:
        """Create instance from dictionary."""
        return SceneCutMetrics(
            luma_delta=data["luma_delta"],
            hist_delta=data["hist_delta"],
            edge_delta=data["edge_delta"],
            color_delta=data["color_delta"],
            black_score=data["black_score"],
            flash_score=data["flash_score"],
            motion_score=data["motion_score"],
            combined_score=data["combined_score"],
        )


@dataclass(frozen=True)
class SceneCutCandidate:
    """
    A single detected scene cut candidate.
    
    Represents a potential scene boundary with timing, confidence,
    classification, and user decision.
    
    Attributes:
        source_frame: Zero-based frame index in source media (cut point frame).
        pts: Presentation timestamp, None for CFR media.
        seconds: Time in seconds from media start.
        timeline_frame: Frame position in timeline, None if from media pool.
        score: Raw detection score (combined_score).
        confidence: Classification confidence (0.0-1.0).
            >= 0.85: High confidence, can auto-accept.
            0.55-0.85: Normal, default pending.
            < 0.55: Low confidence, warning state.
        kind: Classification of the cut type.
        decision: User review decision.
        metrics: Detailed detection metrics, may be None.
        reasons: Tuple of reason strings for why this was flagged.
            Possible values: "high_score", "low_confidence", "flash_like",
            "fast_motion_like", "transition_like", "black_gap",
            "vfr_time_uncertain", "proxy_mapping_approx".
        before_thumb_cache_key: Cache key for pre-cut frame thumbnail.
        after_thumb_cache_key: Cache key for post-cut frame thumbnail.
    """
    
    source_frame: int
    pts: int | None
    seconds: float
    timeline_frame: int | None = None
    score: float = 0.0
    confidence: float = 0.0
    kind: SceneCutKind = SceneCutKind.UNKNOWN
    decision: SceneCutDecision = SceneCutDecision.PENDING
    metrics: SceneCutMetrics | None = None
    reasons: tuple[str, ...] = ()
    before_thumb_cache_key: str | None = None
    after_thumb_cache_key: str | None = None
    
    def __post_init__(self) -> None:
        """Validate candidate after initialization."""
        if self.source_frame < 0:
            raise ValueError(f"source_frame must be >= 0, got {self.source_frame}")
        if self.seconds < 0.0:
            raise ValueError(f"seconds must be >= 0.0, got {self.seconds}")
        if not 0.0 <= self.score <= 1.0:
            raise ValueError(f"score must be in [0.0, 1.0], got {self.score}")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError(f"confidence must be in [0.0, 1.0], got {self.confidence}")
    
    # ==================== Factory methods ====================
    
    def with_decision(self, decision: SceneCutDecision) -> SceneCutCandidate:
        """Create a new candidate with updated decision."""
        return SceneCutCandidate(
            source_frame=self.source_frame,
            pts=self.pts,
            seconds=self.seconds,
            timeline_frame=self.timeline_frame,
            score=self.score,
            confidence=self.confidence,
            kind=self.kind,
            decision=decision,
            metrics=self.metrics,
            reasons=self.reasons,
            before_thumb_cache_key=self.before_thumb_cache_key,
            after_thumb_cache_key=self.after_thumb_cache_key,
        )
    
    def with_timeline_frame(self, timeline_frame: int) -> SceneCutCandidate:
        """Create a new candidate with timeline frame mapping."""
        return SceneCutCandidate(
            source_frame=self.source_frame,
            pts=self.pts,
            seconds=self.seconds,
            timeline_frame=timeline_frame,
            score=self.score,
            confidence=self.confidence,
            kind=self.kind,
            decision=self.decision,
            metrics=self.metrics,
            reasons=self.reasons,
            before_thumb_cache_key=self.before_thumb_cache_key,
            after_thumb_cache_key=self.after_thumb_cache_key,
        )
    
    def with_reasons(self, reasons: tuple[str, ...]) -> SceneCutCandidate:
        """Create a new candidate with updated reasons."""
        return SceneCutCandidate(
            source_frame=self.source_frame,
            pts=self.pts,
            seconds=self.seconds,
            timeline_frame=self.timeline_frame,
            score=self.score,
            confidence=self.confidence,
            kind=self.kind,
            decision=self.decision,
            metrics=self.metrics,
            reasons=reasons,
            before_thumb_cache_key=self.before_thumb_cache_key,
            after_thumb_cache_key=self.after_thumb_cache_key,
        )
    
    # ==================== Judgment properties ====================
    
    @property
    def is_accepted(self) -> bool:
        """Check if candidate is accepted."""
        return self.decision == SceneCutDecision.ACCEPTED
    
    @property
    def is_rejected(self) -> bool:
        """Check if candidate is rejected."""
        return self.decision == SceneCutDecision.REJECTED
    
    @property
    def is_pending(self) -> bool:
        """Check if candidate is pending review."""
        return self.decision == SceneCutDecision.PENDING
    
    @property
    def is_high_confidence(self) -> bool:
        """Check if confidence >= 0.85."""
        return self.confidence >= 0.85
    
    @property
    def is_low_confidence(self) -> bool:
        """Check if confidence < 0.55."""
        return self.confidence < 0.55
    
    @property
    def is_warning(self) -> bool:
        """
        Check if candidate has warning flags.
        
        Warning states: low confidence, flash, fast motion, soft transition,
        black gap, or specific reason flags.
        """
        warning_kinds = {
            SceneCutKind.FLASH,
            SceneCutKind.FAST_MOTION,
            SceneCutKind.SOFT_TRANSITION,
            SceneCutKind.BLACK_GAP,
        }
        warning_reasons = {
            "flash_like",
            "fast_motion_like",
            "transition_like",
            "black_gap",
            "vfr_time_uncertain",
            "proxy_mapping_approx",
            "low_confidence",
        }
        return (
            self.is_low_confidence
            or self.kind in warning_kinds
            or bool(set(self.reasons) & warning_reasons)
        )
    
    def is_automatically_acceptable(self) -> bool:
        """
        Check if candidate can be auto-accepted without review.
        
        Criteria: hard cut, high confidence, no warnings.
        """
        return (
            self.kind == SceneCutKind.HARD_CUT
            and self.confidence >= 0.85
            and not self.is_warning
        )
    
    # ==================== Serialization ====================
    
    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "source_frame": self.source_frame,
            "pts": self.pts,
            "seconds": self.seconds,
            "timeline_frame": self.timeline_frame,
            "score": self.score,
            "confidence": self.confidence,
            "kind": self.kind.value,
            "decision": self.decision.value,
            "metrics": self.metrics.to_dict() if self.metrics else None,
            "reasons": list(self.reasons),
            "before_thumb_cache_key": self.before_thumb_cache_key,
            "after_thumb_cache_key": self.after_thumb_cache_key,
        }
    
    @staticmethod
    def from_dict(data: dict[str, Any]) -> SceneCutCandidate:
        """Create instance from dictionary."""
        metrics_data = data.get("metrics")
        metrics = SceneCutMetrics.from_dict(metrics_data) if metrics_data else None
        
        return SceneCutCandidate(
            source_frame=data["source_frame"],
            pts=data.get("pts"),
            seconds=data["seconds"],
            timeline_frame=data.get("timeline_frame"),
            score=data.get("score", 0.0),
            confidence=data.get("confidence", 0.0),
            kind=SceneCutKind(data.get("kind", "unknown")),
            decision=SceneCutDecision(data.get("decision", "pending")),
            metrics=metrics,
            reasons=tuple(data.get("reasons", [])),
            before_thumb_cache_key=data.get("before_thumb_cache_key"),
            after_thumb_cache_key=data.get("after_thumb_cache_key"),
        )


@dataclass(frozen=True)
class SceneCutAnalysis:
    """
    Complete scene cut detection analysis result.
    
    Contains all detection results including settings, time index,
    and candidates. Designed for caching and serialization.
    
    Attributes:
        analysis_id: Unique identifier for this analysis.
        asset_id: Source media asset identifier.
        source_path: File path to source media.
        clip_id: Timeline clip ID if analysis was on a clip, None for full media.
        settings: Detection settings used.
        media_fingerprint: Hash of media metadata for cache validation.
            Should include: file size, mtime_ns, source range.
        source_time_index: Frame-to-time mapping for VFR handling.
        candidates: Tuple of detected cut candidates, sorted by source_frame.
        created_at: Unix timestamp of analysis creation.
        detector_version: Version string for cache invalidation.
    """
    
    analysis_id: str
    asset_id: str
    source_path: str
    clip_id: str | None = None
    settings: SceneCutSettings = SceneCutSettings()
    media_fingerprint: str = ""
    source_time_index: SourceFrameTimeIndex | None = None
    candidates: tuple[SceneCutCandidate, ...] = ()
    created_at: float = 0.0
    detector_version: str = "1.0.0"
    diagnostics: dict[str, Any] | None = None
    proxy_fingerprint: str | None = None
    
    def __post_init__(self) -> None:
        """Validate analysis after initialization."""
        if not self.analysis_id:
            raise ValueError("analysis_id cannot be empty")
        if not self.asset_id:
            raise ValueError("asset_id cannot be empty")
        if not self.source_path:
            raise ValueError("source_path cannot be empty")
        if not self.detector_version:
            raise ValueError("detector_version cannot be empty")
    
    # ==================== Computed properties ====================
    
    @property
    def accepted_count(self) -> int:
        """Count of accepted candidates."""
        return sum(1 for c in self.candidates if c.is_accepted)
    
    @property
    def rejected_count(self) -> int:
        """Count of rejected candidates."""
        return sum(1 for c in self.candidates if c.is_rejected)
    
    @property
    def pending_count(self) -> int:
        """Count of pending candidates."""
        return sum(1 for c in self.candidates if c.is_pending)
    
    @property
    def warning_count(self) -> int:
        """Count of candidates with warning flags."""
        return sum(1 for c in self.candidates if c.is_warning)
    
    @property
    def total_frames(self) -> int:
        """Total frames in time index."""
        if self.source_time_index is None:
            return 0
        return len(self.source_time_index.points)
    
    # ==================== Query methods ====================
    
    def get_accepted_candidates(self) -> tuple[SceneCutCandidate, ...]:
        """Get all accepted candidates in source frame order."""
        return tuple(c for c in self.candidates if c.is_accepted)
    
    def get_candidate_at_frame(self, source_frame: int) -> SceneCutCandidate | None:
        """Get candidate at a specific frame, if any."""
        for candidate in self.candidates:
            if candidate.source_frame == source_frame:
                return candidate
        return None
    
    # ==================== Update methods ====================
    
    def with_updated_candidate(
        self, source_frame: int, decision: SceneCutDecision
    ) -> SceneCutAnalysis:
        """Create a new analysis with an updated candidate decision."""
        new_candidates = tuple(
            c.with_decision(decision) if c.source_frame == source_frame else c
            for c in self.candidates
        )
        return SceneCutAnalysis(
            analysis_id=self.analysis_id,
            asset_id=self.asset_id,
            source_path=self.source_path,
            clip_id=self.clip_id,
            settings=self.settings,
            media_fingerprint=self.media_fingerprint,
            source_time_index=self.source_time_index,
            candidates=new_candidates,
            created_at=self.created_at,
            detector_version=self.detector_version,
            diagnostics=self.diagnostics,
            # Preserve the proxy fingerprint: dropping it here drifted
            # to_cache_key() from "<proxy_fp>" to "no-proxy" and let
            # derived analyses poison the wrong cache slot (AUD-18011).
            proxy_fingerprint=self.proxy_fingerprint,
        )

    def with_all_decisions(
        self, decisions: dict[int, SceneCutDecision]
    ) -> SceneCutAnalysis:
        """Create a new analysis with multiple updated decisions."""
        new_candidates = tuple(
            c.with_decision(decisions.get(c.source_frame, c.decision))
            for c in self.candidates
        )
        return SceneCutAnalysis(
            analysis_id=self.analysis_id,
            asset_id=self.asset_id,
            source_path=self.source_path,
            clip_id=self.clip_id,
            settings=self.settings,
            media_fingerprint=self.media_fingerprint,
            source_time_index=self.source_time_index,
            candidates=new_candidates,
            created_at=self.created_at,
            detector_version=self.detector_version,
            diagnostics=self.diagnostics,
            proxy_fingerprint=self.proxy_fingerprint,  # AUD-18011
        )
    
    def with_accept_high_confidence(self, threshold: float = 0.85) -> SceneCutAnalysis:
        """Create a new analysis with high confidence candidates auto-accepted."""
        decisions: dict[int, SceneCutDecision] = {}
        for c in self.candidates:
            if c.confidence >= threshold and not c.is_warning:
                decisions[c.source_frame] = SceneCutDecision.ACCEPTED
        return self.with_all_decisions(decisions)
    
    def with_reject_warnings(self) -> SceneCutAnalysis:
        """Create a new analysis with warning candidates rejected."""
        decisions: dict[int, SceneCutDecision] = {}
        for c in self.candidates:
            if c.is_warning:
                decisions[c.source_frame] = SceneCutDecision.REJECTED
        return self.with_all_decisions(decisions)
    
    def to_cache_key(self) -> str:
        """Generate cache key matching generate_cache_key() format."""
        range_str = f"{self.settings.start_source_frame}-{self.settings.end_source_frame or 'end'}"
        clip_str = self.clip_id or "full-media"
        proxy_str = self.proxy_fingerprint or "no-proxy"

        data = (
            f"{self.media_fingerprint}"
            f"|{range_str}"
            f"|{self.settings.to_hash()}"
            f"|{self.detector_version}"
            f"|{proxy_str}"
            f"|{clip_str}"
        )
        return hashlib.sha256(data.encode()).hexdigest()[:24]
    
    # ==================== Serialization ====================
    
    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "analysis_id": self.analysis_id,
            "asset_id": self.asset_id,
            "source_path": self.source_path,
            "clip_id": self.clip_id,
            "settings": self.settings.to_dict(),
            "media_fingerprint": self.media_fingerprint,
            "source_time_index": (
                self.source_time_index.to_dict()
                if self.source_time_index else None
            ),
            "candidates": [c.to_dict() for c in self.candidates],
            "created_at": self.created_at,
            "detector_version": self.detector_version,
            "diagnostics": self.diagnostics,
            "proxy_fingerprint": self.proxy_fingerprint,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> SceneCutAnalysis:
        """Create instance from dictionary."""
        settings = SceneCutSettings.from_dict(data.get("settings", {}))

        time_index_data = data.get("source_time_index")
        time_index = (
            SourceFrameTimeIndex.from_dict(time_index_data)
            if time_index_data else None
        )

        candidates = tuple(
            SceneCutCandidate.from_dict(c)
            for c in data.get("candidates", [])
        )

        return SceneCutAnalysis(
            analysis_id=data["analysis_id"],
            asset_id=data["asset_id"],
            source_path=data["source_path"],
            clip_id=data.get("clip_id"),
            settings=settings,
            media_fingerprint=data.get("media_fingerprint", ""),
            source_time_index=time_index,
            candidates=candidates,
            created_at=data.get("created_at", 0.0),
            detector_version=data.get("detector_version", "1.0.0"),
            diagnostics=data.get("diagnostics"),
            proxy_fingerprint=data.get("proxy_fingerprint"),
        )


@dataclass(frozen=True)
class SceneSubclipSpec:
    """
    Specification for creating a virtual subclip from scene detection.
    
    Virtual subclips reference the original media file but define
    a specific source range, allowing scene-based media organization
    without creating new files.
    
    Attributes:
        parent_asset_id: Asset ID of the original media.
        source_path: File path to source media (same as parent).
        scene_index: 1-based scene number for display.
        source_start: First frame of this scene (inclusive).
        source_duration: Number of frames in this scene.
        start_seconds: Start time in seconds.
        duration_seconds: Duration in seconds.
        display_name: Human-readable name for media pool display.
        start_timecode: SMPTE timecode for start frame.
        end_timecode: SMPTE timecode for end frame (exclusive).
        thumbnail_source_frame: Frame to use for thumbnail.
        detection_confidence: Confidence score from detection.
        detection_kind: Classification from detection.
    """
    
    parent_asset_id: str
    source_path: str
    scene_index: int
    source_start: int
    source_duration: int
    start_seconds: float
    duration_seconds: float
    display_name: str
    start_timecode: str
    end_timecode: str
    thumbnail_source_frame: int
    detection_confidence: float
    detection_kind: SceneCutKind
    
    def __post_init__(self) -> None:
        """Validate subclip spec after initialization."""
        if not self.parent_asset_id:
            raise ValueError("parent_asset_id cannot be empty")
        if not self.source_path:
            raise ValueError("source_path cannot be empty")
        if self.scene_index < 1:
            raise ValueError(f"scene_index must be >= 1, got {self.scene_index}")
        if self.source_start < 0:
            raise ValueError(f"source_start must be >= 0, got {self.source_start}")
        if self.source_duration < 1:
            raise ValueError(f"source_duration must be >= 1, got {self.source_duration}")
        if self.start_seconds < 0.0:
            raise ValueError(f"start_seconds must be >= 0.0, got {self.start_seconds}")
        if self.duration_seconds <= 0.0:
            raise ValueError(f"duration_seconds must be > 0.0, got {self.duration_seconds}")
        if not self.display_name:
            raise ValueError("display_name cannot be empty")
        if not 0.0 <= self.detection_confidence <= 1.0:
            raise ValueError(
                f"detection_confidence must be in [0.0, 1.0], got {self.detection_confidence}"
            )
    
    @property
    def source_end(self) -> int:
        """Last frame index (exclusive) of this scene."""
        return self.source_start + self.source_duration
    
    @property
    def end_seconds(self) -> float:
        """End time in seconds (exclusive) of this scene."""
        return self.start_seconds + self.duration_seconds
    
    def to_metadata(self, analysis_id: str) -> dict[str, Any]:
        """
        Generate metadata dictionary for MediaAsset.
        
        Args:
            analysis_id: ID of the scene cut analysis that created this subclip.
            
        Returns:
            Metadata dictionary with subclip information.
        """
        return {
            "is_subclip": True,
            "parent_asset_id": self.parent_asset_id,
            "parent_path": self.source_path,
            "source_start": self.source_start,
            "source_duration": self.source_duration,
            "source_end": self.source_end,
            "start_seconds": self.start_seconds,
            "duration_seconds": self.duration_seconds,
            "end_seconds": self.end_seconds,
            "scene_index": self.scene_index,
            "scene_cut_analysis_id": analysis_id,
            "scene_cut_confidence": self.detection_confidence,
            "scene_cut_kind": self.detection_kind.value,
            "start_timecode": self.start_timecode,
            "end_timecode": self.end_timecode,
            "thumbnail_source_frame": self.thumbnail_source_frame,
        }
    
    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "parent_asset_id": self.parent_asset_id,
            "source_path": self.source_path,
            "scene_index": self.scene_index,
            "source_start": self.source_start,
            "source_duration": self.source_duration,
            "start_seconds": self.start_seconds,
            "duration_seconds": self.duration_seconds,
            "display_name": self.display_name,
            "start_timecode": self.start_timecode,
            "end_timecode": self.end_timecode,
            "thumbnail_source_frame": self.thumbnail_source_frame,
            "detection_confidence": self.detection_confidence,
            "detection_kind": self.detection_kind.value,
        }
    
    @staticmethod
    def from_dict(data: dict[str, Any]) -> SceneSubclipSpec:
        """Create instance from dictionary."""
        return SceneSubclipSpec(
            parent_asset_id=data["parent_asset_id"],
            source_path=data["source_path"],
            scene_index=data["scene_index"],
            source_start=data["source_start"],
            source_duration=data["source_duration"],
            start_seconds=data["start_seconds"],
            duration_seconds=data["duration_seconds"],
            display_name=data["display_name"],
            start_timecode=data["start_timecode"],
            end_timecode=data["end_timecode"],
            thumbnail_source_frame=data["thumbnail_source_frame"],
            detection_confidence=data["detection_confidence"],
            detection_kind=SceneCutKind(data["detection_kind"]),
        )


# ==================== Helper functions ====================

DETECTOR_VERSION = "1.0.0"


def generate_analysis_id(asset_id: str, timestamp: float | None = None) -> str:
    """
    Generate a unique analysis ID.
    
    Args:
        asset_id: Source asset ID.
        timestamp: Unix timestamp, defaults to current time.
        
    Returns:
        Unique analysis ID string (16 chars).
    """
    if timestamp is None:
        timestamp = time.time()
    
    data = f"{asset_id}|{timestamp:.6f}|{DETECTOR_VERSION}"
    return hashlib.sha256(data.encode()).hexdigest()[:16]


def generate_scene_subclips(
    analysis: SceneCutAnalysis,
    total_source_frames: int,
    total_duration_seconds: float,
    parent_display_name: str,
    fps: float = 24.0,
) -> tuple[SceneSubclipSpec, ...]:
    """
    Generate subclip specifications from accepted scene cuts.
    
    Takes all accepted cuts and generates N+1 scene subclips that
    cover the entire source range without gaps.
    
    Args:
        analysis: Scene cut analysis with accepted candidates.
        total_source_frames: Total frames in the analyzed source range.
        total_duration_seconds: Total duration in seconds of analyzed range.
        parent_display_name: Name of parent asset for subclip naming.
        fps: Frame rate for timecode calculation, default 24.0.
        
    Returns:
        Tuple of SceneSubclipSpec in scene order.
    """
    accepted = analysis.get_accepted_candidates()
    start_frame = analysis.settings.start_source_frame
    end_frame = analysis.settings.end_source_frame or (start_frame + total_source_frames)

    def _frame_seconds(frame: int) -> float:
        """Absolute seconds of a source frame, via the analysis time index
        (PTS-exact for VFR) with an fps fallback.

        Candidate.seconds are absolute ("from media start"), so the first
        scene's start must be the real seconds of ``start_frame`` — a
        hardcoded 0.0 mislabelled range analyses whose start_source_frame
        is non-zero (AUD-18013).
        """
        index = analysis.source_time_index
        # Defensive: persisted/deserialized analyses may carry a plain
        # tuple of points instead of a SourceFrameTimeIndex instance.
        to_seconds = getattr(index, "source_frame_to_seconds", None)
        if callable(to_seconds):
            try:
                return to_seconds(frame)
            except ValueError:
                pass
        if fps and fps > 0.0:
            return float(frame) / float(fps)
        return 0.0

    if not accepted:
        # No cuts = single scene covering entire source
        return (
            SceneSubclipSpec(
                parent_asset_id=analysis.asset_id,
                source_path=analysis.source_path,
                scene_index=1,
                source_start=start_frame,
                source_duration=end_frame - start_frame,
                start_seconds=_frame_seconds(start_frame),
                duration_seconds=total_duration_seconds,
                display_name=f"{parent_display_name} Scene 001",
                start_timecode=_frames_to_timecode(start_frame, fps),
                end_timecode=_frames_to_timecode(end_frame, fps),
                thumbnail_source_frame=start_frame,
                detection_confidence=1.0,
                detection_kind=SceneCutKind.HARD_CUT,
            ),
        )

    specs: list[SceneSubclipSpec] = []
    prev_frame = start_frame
    prev_seconds = _frame_seconds(start_frame)
    
    for cut in sorted(accepted, key=lambda c: c.source_frame):
        # Scene from previous cut to this cut
        frame_count = cut.source_frame - prev_frame
        duration = cut.seconds - prev_seconds
        
        if frame_count < 1:
            continue  # Skip empty scenes
        
        scene_index = len(specs) + 1
        spec = SceneSubclipSpec(
            parent_asset_id=analysis.asset_id,
            source_path=analysis.source_path,
            scene_index=scene_index,
            source_start=prev_frame,
            source_duration=frame_count,
            start_seconds=prev_seconds,
            duration_seconds=duration,
            display_name=f"{parent_display_name} Scene {scene_index:03d}",
            start_timecode=_frames_to_timecode(prev_frame, fps),
            end_timecode=_frames_to_timecode(cut.source_frame, fps),
            thumbnail_source_frame=prev_frame,
            detection_confidence=cut.confidence,
            detection_kind=cut.kind,
        )
        specs.append(spec)
        
        prev_frame = cut.source_frame
        prev_seconds = cut.seconds
    
    # Final scene from last cut to end
    final_frame_count = end_frame - prev_frame
    # prev_seconds is absolute, so the end bound must be the absolute
    # seconds of end_frame (via time index / fps) — subtracting an
    # absolute prev_seconds from the caller's range duration could go
    # negative for range analyses (AUD-18013 连带).
    final_duration = _frame_seconds(end_frame) - prev_seconds

    if final_frame_count >= 1:
        scene_index = len(specs) + 1
        spec = SceneSubclipSpec(
            parent_asset_id=analysis.asset_id,
            source_path=analysis.source_path,
            scene_index=scene_index,
            source_start=prev_frame,
            source_duration=final_frame_count,
            start_seconds=prev_seconds,
            duration_seconds=final_duration,
            display_name=f"{parent_display_name} Scene {scene_index:03d}",
            start_timecode=_frames_to_timecode(prev_frame, fps),
            end_timecode=_frames_to_timecode(prev_frame + final_frame_count, fps),
            thumbnail_source_frame=prev_frame,
            detection_confidence=1.0,
            detection_kind=SceneCutKind.HARD_CUT,
        )
        specs.append(spec)
    
    return tuple(specs)


def _frames_to_timecode(frames: int, fps: float) -> str:
    """
    Convert frame count to SMPTE timecode string.
    
    Args:
        frames: Frame count from start.
        fps: Frames per second.
        
    Returns:
        SMPTE timecode string (HH:MM:SS:FF).
    """
    # 审计修复：委托 interchange 的 SMPTE 实现（P0-05 DF 双向转换）。
    # 旧副本把 29.97 当 30 NDF 显示，1 小时后与时间线标尺/ADR/Trim 监看
    # 的时间码差 108 帧。
    from src.features.edit.core.interchange import frames_to_timecode
    from src.features.edit.core.recording_manager import is_drop_frame_rate
    if fps is None or fps <= 0.0:
        fps = 24.0
    return frames_to_timecode(max(0, int(frames)), fps, is_drop_frame_rate(fps))


def timecode_to_frames(timecode: str, fps: float) -> int:
    """
    Convert SMPTE timecode string to frame count.

    Args:
        timecode: SMPTE timecode string (HH:MM:SS:FF).
        fps: Frames per second.

    Returns:
        Frame count from start.

    Raises:
        ValueError: Invalid timecode format or out-of-range fields.
    """
    # 审计修复（AUD-18012）：与 _frames_to_timecode 的 P0-05 修复对称，
    # 委托 interchange 的精确逆运算。旧副本用 int(fps) 截断（29.97→29，
    # 连 NDF 也算错），且完全忽略 drop-frame（29.97 DF 每分钟偏 2 帧、
    # 1 小时偏 108 帧）、不校验字段越界。
    from src.features.edit.core.interchange import timecode_to_frames as _tc_to_frames
    from src.features.edit.core.recording_manager import is_drop_frame_rate
    if fps is None or fps <= 0.0:
        fps = 24.0
    return _tc_to_frames(timecode, fps, is_drop_frame_rate(fps))