"""
Scene Cut Detector Module.

Main detector class that integrates sampler, metrics calculator,
and filter pipeline for complete scene cut detection.

## Design Principles

- Integrates VideoFrameSampler, FrameMetricsCalculator, SceneCutFilterPipeline
- Supports progress callback and cancel token
- Returns SceneCutAnalysis with full results
- Records diagnostics (decode path, fallback reason, frame count)
- Pure Python, no PyQt dependencies

## Reference

- docs/resolve_like_scene_cut_detection_implementation_plan_2026-06-04.md Section 5
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Optional, Callable, Any
from enum import Enum
import logging
import time

import numpy as np

from .models import (
    SceneCutSettings,
    SceneCutAnalysis,
    SceneCutCandidate,
    SceneCutKind,
    FrameTimePoint,
    SourceFrameTimeIndex,
    generate_analysis_id,
)
from .frame_sampler import (
    VideoFrameSampler,
    SampledFrame,
    CancelToken,
    CancelledError,
)
from .metrics import FrameMetricsCalculator
from .filters import (
    SceneCutFilterPipeline,
    FrameMetricsEntry,
)


logger = logging.getLogger(__name__)

# Shared zero-size pixel placeholder. The filter pipeline only consumes
# entry.frame.time_point metadata (source_frame/pts/seconds), so retained
# metrics entries drop the ~110KB pixel payload and the "streaming"
# pipeline actually stays O(1) in frame memory; only the previous frame
# keeps real pixels for the difference metrics (AUD-17904).
_EMPTY_FRAME_DATA: np.ndarray = np.empty((0, 0, 3), dtype=np.uint8)


# ================================================================
# Detection Phase and Progress
# ================================================================

class DetectionPhase(Enum):
    """
    Phase of detection process.
    
    Values:
    - SAMPLING: Decoding and sampling frames
    - METRICS: Computing frame metrics
    - FILTERING: Applying detection filters
    - COMPLETE: Detection finished
    - CANCELLED: Detection cancelled by user
    - ERROR: Detection failed with error
    """
    
    SAMPLING = "sampling"
    METRICS = "metrics"
    FILTERING = "filtering"
    COMPLETE = "complete"
    CANCELLED = "cancelled"
    ERROR = "error"


@dataclass(frozen=True)
class DetectionProgress:
    """
    Progress information for detection process.
    
    Attributes:
    - phase: Current detection phase
    - current_frame: Current frame being processed
    - total_frames: Total frames to process
    - message: Human-readable progress message
    - candidates_found: Number of candidates found so far
    - elapsed_seconds: Time elapsed since start
    - estimated_remaining_seconds: Estimated time remaining
    """
    
    phase: DetectionPhase = DetectionPhase.SAMPLING
    current_frame: int = 0
    total_frames: int = 0
    message: str = ""
    candidates_found: int = 0
    elapsed_seconds: float = 0.0
    estimated_remaining_seconds: Optional[float] = None
    
    @property
    def progress_ratio(self) -> float:
        """Calculate progress ratio [0, 1]."""
        if self.total_frames <= 0:
            return 0.0
        return min(1.0, self.current_frame / self.total_frames)
    
    @property
    def progress_percent(self) -> int:
        """Calculate progress percentage."""
        return int(self.progress_ratio * 100)
    
    @property
    def is_complete(self) -> bool:
        """Check if detection is complete."""
        return self.phase in (DetectionPhase.COMPLETE, DetectionPhase.CANCELLED, DetectionPhase.ERROR)


# ================================================================
# Helper Functions
# ================================================================

def calculate_media_fingerprint(
    source_path: str,
    file_size: int,
    mtime_ns: int,
) -> str:
    """
    Calculate media fingerprint for cache invalidation.
    
    Args:
        source_path: Source file path
        file_size: File size in bytes
        mtime_ns: Modification time in nanoseconds
    
    Returns:
        Fingerprint string
    """
    # Simple fingerprint combining path, size, and mtime
    return f"{source_path}:{file_size}:{mtime_ns}"


# ================================================================
# Detector Progress Callback
# ================================================================

DetectorProgressCallback = Callable[[DetectionProgress], None]
"""
Progress callback for detector.

Args:
    progress: DetectionProgress instance

Returns:
    None
"""


# ================================================================
# Scene Cut Detector
# ================================================================

class SceneCutDetector:
    """
    Main detector for scene cut detection.

    Integrates all components for complete detection workflow:
    1. Frame sampling (FrameSampler)
    2. Metrics calculation (FrameMetricsCalculator)
    3. Filter pipeline (SceneCutFilterPipeline)
    
    Usage:
    ```python
    detector = SceneCutDetector(source_path, asset_id, settings)
    cancel_token = CancelToken()
    
    def on_progress(progress):
        print(f"{progress.phase}: {progress.progress_percent}%")
    
    analysis = detector.detect(
        progress_callback=on_progress,
        cancel_token=cancel_token,
    )
    
    for candidate in analysis.candidates:
        print(candidate.source_frame, candidate.kind, candidate.confidence)
    ```
    
    Attributes:
    - source_path: Path to source video file
    - asset_id: Asset ID for analysis
    - settings: SceneCutSettings configuration
    """
    
    # Detector version for cache invalidation
    DETECTOR_VERSION: str = "1.0.0"
    
    def __init__(
        self,
        source_path: str,
        asset_id: str,
        settings: SceneCutSettings = SceneCutSettings(),
        clip_id: Optional[str] = None,
        proxy_path: Optional[str] = None,
    ) -> None:
        """
        Initialize scene cut detector.
        
        Args:
            source_path: Path to source video file
            asset_id: Asset ID for analysis
            settings: SceneCutSettings configuration
            clip_id: Clip ID if detecting on timeline clip
            proxy_path: Path to proxy media (if available)
        """
        self._source_path = source_path
        self._asset_id = asset_id
        self._settings = settings
        self._clip_id = clip_id
        self._proxy_path = proxy_path
        
        # Components
        self._sampler: Optional[VideoFrameSampler] = None
        self._metrics_calculator: Optional[FrameMetricsCalculator] = None
        self._filter_pipeline: Optional[SceneCutFilterPipeline] = None
        
        # State
        self._start_time: float = 0.0
        self._cancelled: bool = False
        # Active detection's cancel token (assigned in detect()), so that
        # cancel() can cancel an in-flight detection (AUD-17903).
        self._cancel_token: Optional[CancelToken] = None
    
    @property
    def source_path(self) -> str:
        """Get source path."""
        return self._source_path
    
    @property
    def asset_id(self) -> str:
        """Get asset ID."""
        return self._asset_id
    
    @property
    def settings(self) -> SceneCutSettings:
        """Get settings."""
        return self._settings
    
    def detect(
        self,
        progress_callback: Optional[DetectorProgressCallback] = None,
        cancel_token: Optional[CancelToken] = None,
    ) -> SceneCutAnalysis:
        """
        Execute complete scene cut detection.
        
        Workflow:
        1. Initialize components
        2. Sample frames (with progress updates)
        3. Calculate metrics
        4. Apply filter pipeline
        5. Generate analysis result
        
        Args:
            progress_callback: Optional callback for progress updates
            cancel_token: Optional token for cancellation
        
        Returns:
            SceneCutAnalysis with detection results
        
        Raises:
            RuntimeError: If detection fails
            CancelledError: If the cancel token is triggered during detection
        """
        # Initialize
        self._start_time = time.time()
        self._cancelled = False
        
        # Create cancel token if not provided
        if cancel_token is None:
            cancel_token = CancelToken()
        # Remember the active token so the public cancel() API can forward
        # to the in-flight detection (AUD-17903).
        self._cancel_token = cancel_token
        
        # Progress helper
        def report_progress(
            phase: DetectionPhase,
            current: int,
            total: int,
            message: str,
            candidates_found: int = 0,
        ) -> None:
            if progress_callback:
                elapsed = time.time() - self._start_time
                estimated_remaining = None
                if current > 0 and total > 0:
                    rate = elapsed / current
                    estimated_remaining = rate * (total - current)
                
                progress_callback(DetectionProgress(
                    phase=phase,
                    current_frame=current,
                    total_frames=total,
                    message=message,
                    candidates_found=candidates_found,
                    elapsed_seconds=elapsed,
                    estimated_remaining_seconds=estimated_remaining,
                ))
        
        # Diagnostic information
        diagnostics: dict[str, Any] = {
            "detector_version": self.DETECTOR_VERSION,
            "source_path": self._source_path,
            "asset_id": self._asset_id,
            "clip_id": self._clip_id,
            "settings": self._settings.to_dict(),
            "decode_path": "master",
            "fallback_reason": None,
            "frames_sampled": 0,
            "frames_processed": 0,
            "cancelled": False,
            "error": None,
        }
        
        # Initialize components
        # Use proxy path if available and settings allow
        decode_path = self._source_path
        if self._proxy_path and self._settings.use_proxy:
            import os
            if os.path.exists(self._proxy_path):
                decode_path = self._proxy_path
                diagnostics["decode_path"] = "proxy"
                logger.info(f"Using proxy for detection: {self._proxy_path}")

        self._sampler = VideoFrameSampler(
            decode_path,
            self._settings,
            cancel_token=cancel_token,
        )
        self._metrics_calculator = FrameMetricsCalculator()
        self._filter_pipeline = SceneCutFilterPipeline(self._settings)
        
        # Streaming pipeline: only keep time points and metrics entries,
        # not full frame data (reduces memory for long footage)
        time_points: list[FrameTimePoint] = []
        metrics_entries: list[FrameMetricsEntry] = []
        total_sampled = 0

        try:
            # Open sampler first
            self._sampler.open()
            diagnostics["source_width"] = self._sampler.original_width
            diagnostics["source_height"] = self._sampler.original_height
            diagnostics["sample_width"] = self._sampler.target_width
            diagnostics["sample_height"] = self._sampler.target_height

            # Phase 1: Sampling
            report_progress(DetectionPhase.SAMPLING, 0, self._sampler.total_frames, "Starting frame sampling...")

            frame_index = 0
            prev_frame: Optional[SampledFrame] = None

            # Sample frames using VideoFrameSampler's generator
            for sampled_frame in self._sampler.sample_frames():
                # Check for cancellation
                if cancel_token.is_cancelled():
                    diagnostics["cancelled"] = True
                    report_progress(
                        DetectionPhase.CANCELLED,
                        frame_index,
                        self._sampler.total_frames,
                        "Detection cancelled by user",
                    )
                    self._cancelled = True
                    break

                # Collect time point (lightweight)
                time_points.append(sampled_frame.time_point)

                # Calculate metrics if we have previous frame
                if prev_frame is not None:
                    metrics = self._metrics_calculator.calculate(
                        prev_frame.frame_data,
                        sampled_frame.frame_data,
                    )
                    metrics_entries.append(FrameMetricsEntry(
                        # Strip the pixel payload: downstream filters only
                        # read time-point metadata (AUD-17904).
                        frame=replace(
                            sampled_frame, frame_data=_EMPTY_FRAME_DATA
                        ),
                        metrics=metrics,
                        frame_index=frame_index,
                    ))

                # Only keep previous frame (streaming)
                prev_frame = sampled_frame
                frame_index += 1
                total_sampled = frame_index

                # Update progress every 10 frames
                if frame_index % 10 == 0:
                    report_progress(
                        DetectionPhase.SAMPLING,
                        frame_index,
                        self._sampler.total_frames,
                        f"Sampling frame {frame_index}",
                    )

            # If cancelled, return empty analysis
            if self._cancelled:
                return self._create_analysis_from_timepoints(
                    time_points,
                    [],
                    diagnostics,
                    phase=DetectionPhase.CANCELLED,
                )

            # Update diagnostics
            diagnostics["frames_sampled"] = total_sampled
            diagnostics["total_frames"] = self._sampler.total_frames
            diagnostics["fps"] = self._sampler.fps
            # Pixel-conversion fallback count, so upper layers can tell
            # degraded (black-fill) frames from clean decodes (AUD-17921).
            diagnostics["frame_conversion_failures"] = (
                self._sampler.total_convert_failures
            )
            
            # Phase 2: Metrics calculation (already done during sampling)
            report_progress(
                DetectionPhase.METRICS,
                len(metrics_entries),
                total_sampled,
                "Metrics calculation complete",
            )
            diagnostics["frames_processed"] = len(metrics_entries)

            # Check for cancellation again
            if cancel_token.is_cancelled():
                diagnostics["cancelled"] = True
                return self._create_analysis_from_timepoints(
                    time_points,
                    [],
                    diagnostics,
                )
            
            # Phase 3: Filtering
            report_progress(DetectionPhase.FILTERING, 0, len(metrics_entries), "Starting filter pipeline...")
            
            def filter_progress(current: int, total: int, message: str) -> None:
                report_progress(
                    DetectionPhase.FILTERING,
                    current,
                    total,
                    message,
                )
            
            candidates = self._filter_pipeline.process(
                metrics_entries,
                progress_callback=filter_progress,
            )
            
            # Update diagnostics from filter
            filter_diagnostics = self._filter_pipeline.diagnostics
            diagnostics.update(filter_diagnostics)
            
            # Phase 4: Complete
            report_progress(
                DetectionPhase.COMPLETE,
                len(metrics_entries),
                total_sampled,
                f"Detection complete. Found {len(candidates)} candidates.",
                len(candidates),
            )

            # Create analysis result
            analysis = self._create_analysis_from_timepoints(
                time_points,
                candidates,
                diagnostics,
            )

            logger.info(
                f"Scene cut detection complete: "
                f"{len(candidates)} candidates from {total_sampled} frames"
            )
            
            return analysis
        
        except CancelledError:
            # Cancellation from the sampler's cancel token must be reported
            # through the CANCELLED phase / the worker's cancelled channel,
            # never misreported as a detection ERROR (AUD-17901). Re-raise
            # so the caller (worker) handles it in its CancelledError path.
            diagnostics["cancelled"] = True
            self._cancelled = True
            report_progress(
                DetectionPhase.CANCELLED,
                total_sampled,
                self._sampler.total_frames if self._sampler is not None else 0,
                "Detection cancelled by user",
            )
            raise

        except Exception as e:
            logger.error(f"Scene cut detection error: {e}")
            diagnostics["error"] = str(e)

            report_progress(
                DetectionPhase.ERROR,
                0,
                0,
                f"Detection error: {e}",
            )

            # Honour the documented "Raises: RuntimeError" contract: a
            # decode/pipeline failure propagates to the caller (the worker
            # maps it onto its error channel) instead of being masked as a
            # successfully completed analysis with zero candidates
            # (AUD-17902).
            raise RuntimeError(f"Scene cut detection failed: {e}") from e
    
    def _create_analysis(
        self,
        sampled_frames: list[SampledFrame],
        candidates: list[SceneCutCandidate],
        diagnostics: dict[str, Any],
        phase: DetectionPhase,
    ) -> SceneCutAnalysis:
        """Create analysis from sampled frames (legacy, kept for compatibility)."""
        time_points = [frame.time_point for frame in sampled_frames]
        return self._build_analysis(time_points, candidates, diagnostics)

    def _create_analysis_from_timepoints(
        self,
        time_points: list[FrameTimePoint],
        candidates: list[SceneCutCandidate],
        diagnostics: dict[str, Any],
        phase: DetectionPhase = DetectionPhase.COMPLETE,
    ) -> SceneCutAnalysis:
        """Create analysis from pre-collected time points (streaming mode)."""
        return self._build_analysis(time_points, candidates, diagnostics)

    def _build_analysis(
        self,
        time_points: list[FrameTimePoint],
        candidates: list[SceneCutCandidate],
        diagnostics: dict[str, Any],
    ) -> SceneCutAnalysis:
        """Build SceneCutAnalysis from time points and candidates."""
        time_points_tuple = tuple(time_points)

        # Build SourceFrameTimeIndex from time points
        time_index: SourceFrameTimeIndex | None = None
        if time_points_tuple:
            source_fps = self._sampler.fps if self._sampler.fps > 0 else None
            time_index = SourceFrameTimeIndex(
                asset_id=self._asset_id,
                media_fingerprint="",  # Set after fingerprint is calculated
                points=time_points_tuple,
                average_fps=source_fps,
            )

        # Create candidates tuple
        candidates_tuple = tuple(candidates)

        # Calculate media fingerprint from actual file stats
        # Must match MediaFingerprint.to_hash() format used by generate_cache_key()
        media_fingerprint = ""
        proxy_fingerprint = None
        try:
            import os
            import hashlib
            stat = os.stat(self._source_path)
            from .cache import MediaFingerprint
            fp = MediaFingerprint(
                path=self._source_path,
                size=stat.st_size,
                mtime_ns=stat.st_mtime_ns,
                asset_id=self._asset_id,
            )
            media_fingerprint = fp.to_hash()

            # Calculate proxy fingerprint if proxy was used
            if self._proxy_path and self._settings.use_proxy:
                try:
                    proxy_stat = os.stat(self._proxy_path)
                    proxy_fp_data = (
                        f"{self._proxy_path}|{proxy_stat.st_size}|{proxy_stat.st_mtime_ns}"
                    )
                    proxy_fingerprint = hashlib.sha256(
                        proxy_fp_data.encode()
                    ).hexdigest()[:16]
                except OSError:
                    proxy_fingerprint = "proxy-missing"
        except OSError:
            media_fingerprint = ""

        # Create analysis
        analysis = SceneCutAnalysis(
            analysis_id=generate_analysis_id(self._asset_id),
            asset_id=self._asset_id,
            source_path=self._source_path,
            clip_id=self._clip_id,
            settings=self._settings,
            media_fingerprint=media_fingerprint,
            source_time_index=time_index,
            candidates=candidates_tuple,
            created_at=time.time(),
            detector_version=self.DETECTOR_VERSION,
            diagnostics=diagnostics,
            proxy_fingerprint=proxy_fingerprint,
        )
        
        return analysis
    
    def cancel(self) -> None:
        """Request cancellation of a running detection.

        Forwards to the active detection's cancel token — the sampling
        loop only observes token state, so a flag alone could never stop
        an in-flight detection (AUD-17903).
        """
        self._cancelled = True
        token = self._cancel_token
        if token is not None:
            token.cancel()


# ================================================================
# Detection Result Helpers
# ================================================================

def get_accepted_candidates(analysis: SceneCutAnalysis) -> list[SceneCutCandidate]:
    """
    Get accepted candidates from analysis.
    
    Args:
        analysis: SceneCutAnalysis
    
    Returns:
        List of accepted candidates
    """
    return [c for c in analysis.candidates if c.is_accepted]


def get_pending_candidates(analysis: SceneCutAnalysis) -> list[SceneCutCandidate]:
    """
    Get pending candidates from analysis.
    
    Args:
        analysis: SceneCutAnalysis
    
    Returns:
        List of pending candidates
    """
    return [c for c in analysis.candidates if c.is_pending]


def get_hard_cut_candidates(analysis: SceneCutAnalysis) -> list[SceneCutCandidate]:
    """
    Get hard cut candidates from analysis.
    
    Args:
        analysis: SceneCutAnalysis
    
    Returns:
        List of hard cut candidates
    """
    return [c for c in analysis.candidates if c.kind == SceneCutKind.HARD_CUT]


def get_warning_candidates(analysis: SceneCutAnalysis) -> list[SceneCutCandidate]:
    """
    Get candidates with warnings.
    
    Args:
        analysis: SceneCutAnalysis
    
    Returns:
        List of warning candidates
    """
    return [c for c in analysis.candidates if c.is_warning]


def get_high_confidence_candidates(
    analysis: SceneCutAnalysis,
    threshold: float = 0.85,
) -> list[SceneCutCandidate]:
    """
    Get high confidence candidates.
    
    Args:
        analysis: SceneCutAnalysis
        threshold: Confidence threshold
    
    Returns:
        List of high confidence candidates
    """
    return [c for c in analysis.candidates if c.confidence >= threshold]


# ================================================================
# Module Exports
# ================================================================

__all__ = [
    # Main class
    "SceneCutDetector",
    
    # Progress callback type
    "DetectorProgressCallback",
    
    # Helper functions
    "get_accepted_candidates",
    "get_pending_candidates",
    "get_hard_cut_candidates",
    "get_warning_candidates",
    "get_high_confidence_candidates",
]
