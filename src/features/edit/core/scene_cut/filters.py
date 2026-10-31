"""
Scene Cut Filters Module.

Implements detection filters for scene cut candidates:
- adaptive_threshold: Rolling median/MAD dynamic threshold
- min_scene_distance: Minimum frame distance from previous cut
- flash_guard: Identify single-frame brightness spikes (FLASH)
- transition_guard: Identify gradual transitions (SOFT_TRANSITION)
- black_gap_detector: Identify black frame intervals (BLACK_GAP)
- fast_motion_suppressor: Identify continuous high motion (FAST_MOTION)

## Design Principles

- All filters produce SceneCutCandidate with proper classification
- Filters work on frame metrics data
- Adaptive threshold using rolling statistics
- False positive suppression based on plan section 5.5-5.6

## Reference

- docs/resolve_like_scene_cut_detection_implementation_plan_2026-06-04.md Section 5.4-5.6
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Callable, Any
import logging

import numpy as np

from .models import (
    SceneCutSettings,
    SceneCutMetrics,
    SceneCutCandidate,
    SceneCutKind,
    SceneCutDecision,
)
from .frame_sampler import SampledFrame


logger = logging.getLogger(__name__)


# ================================================================
# Helper Functions
# ================================================================

def calculate_confidence(
    score: float,
    kind: SceneCutKind,
    reasons: tuple[str, ...],
) -> float:
    """
    Calculate confidence level for a detection.
    
    Args:
        score: Detection score
        kind: Detection type
        reasons: Detection reason tags
    
    Returns:
        Confidence level [0, 1]
    """
    # Base confidence from score
    base_confidence = min(1.0, score * 1.5)
    
    # Adjust based on kind
    kind_multipliers = {
        SceneCutKind.HARD_CUT: 1.0,
        SceneCutKind.SOFT_TRANSITION: 0.7,
        SceneCutKind.BLACK_GAP: 0.75,
        SceneCutKind.FLASH: 0.3,
        SceneCutKind.FAST_MOTION: 0.4,
        SceneCutKind.UNKNOWN: 0.5,
    }
    confidence = base_confidence * kind_multipliers.get(kind, 0.5)
    
    # Reduce confidence for warning reasons
    warning_penalty = 0.1
    warning_reasons = {
        "flash_like", "fast_motion_like", "transition_like",
        "black_gap", "vfr_time_uncertain", "proxy_mapping_approx",
    }
    for reason in reasons:
        if reason in warning_reasons:
            confidence -= warning_penalty
    
    # Ensure confidence stays in valid range
    return max(0.0, min(1.0, confidence))


# ================================================================
# Filter Data Structures
# ================================================================

@dataclass
class FrameMetricsEntry:
    """
    Entry containing frame and its computed metrics.
    
    Attributes:
    - frame: Sampled frame data
    - metrics: Computed metrics for this frame (relative to previous frame)
    - frame_index: Frame index in sequence
    """
    
    frame: SampledFrame
    metrics: SceneCutMetrics
    frame_index: int = 0


@dataclass
class RollingStats:
    """
    Rolling statistics for adaptive threshold.
    
    Uses median and MAD (Median Absolute Deviation) for robust estimation.
    
    Attributes:
    - median: Current median of scores
    - mad: Current MAD value
    - window: Window of recent scores
    - window_size: Maximum window size
    """
    
    median: float = 0.0
    mad: float = 0.0
    window: list[float] = field(default_factory=list)
    window_size: int = 30
    
    def update(self, value: float) -> None:
        """Update rolling statistics with new value."""
        self.window.append(value)
        if len(self.window) > self.window_size:
            self.window.pop(0)
        
        if len(self.window) > 0:
            self.median = float(np.median(self.window))
            # MAD = median(|x - median|)
            deviations = [abs(x - self.median) for x in self.window]
            self.mad = float(np.median(deviations)) if deviations else 0.0
    
    def get_threshold(self, sensitivity: float) -> float:
        """
        Calculate adaptive threshold.
        
        Higher sensitivity = lower threshold = more detections.
        
        Args:
            sensitivity: Sensitivity value [0.35, 0.75]
        
        Returns:
            Adaptive threshold value
        """
        if len(self.window) < 5:
            # Not enough data, use default threshold
            return 0.7 - sensitivity * 0.5
        
        # Base threshold from median + k * MAD
        # k controls how many MADs above median to trigger
        # Higher sensitivity = lower k = more detections
        k = 3.0 - sensitivity * 2.0  # k ranges from ~2.3 to ~1.5
        
        threshold = self.median + k * self.mad
        
        # Ensure threshold is in reasonable range
        return float(np.clip(threshold, 0.15, 0.70))


@dataclass
class DetectionState:
    """
    State for detection process.

    Tracks previous cut positions and current detection context.

    Attributes:
    - last_cut_frame: Frame index of last detected cut
    - last_metrics: Metrics at last cut point
    - transition_frames: Frames in potential transition
    - motion_frames: Frames with high motion
    - black_frames: Frames that are mostly black
    - moderate_score_frames: Frames with moderate scores (transition candidates)
    - consecutive_moderate: Count of consecutive moderate-score frames
    """

    last_cut_frame: int = -1000  # Initialize far in past
    last_metrics: Optional[SceneCutMetrics] = None
    prev_entry_metrics: Optional[SceneCutMetrics] = None
    prev_guard_frame: int = -1  # Frame index of the last entry that passed guards
    transition_frames: list[int] = field(default_factory=list)
    motion_frames: list[int] = field(default_factory=list)
    black_frames: list[int] = field(default_factory=list)
    moderate_score_frames: list[int] = field(default_factory=list)
    consecutive_moderate: int = 0
    # AUD-17913：累计计数器（不含窗口裁剪），供 guard 门控保持与"未裁剪
    # 全表 len"完全一致的语义；列表本体只保留扫描窗口内的近期帧。
    motion_frames_seen: int = 0
    black_frames_seen: int = 0

    #: AUD-17913：_apply_guards 的扫描窗口半径（motion ±5 / black ±3）。
    MOTION_SCAN_RADIUS: int = 5
    BLACK_SCAN_RADIUS: int = 3

    def can_cut_at(self, frame_index: int, min_distance: int) -> bool:
        """Check if frame is sufficiently distant from previous cut."""
        return (frame_index - self.last_cut_frame) >= min_distance

    def register_cut(self, frame_index: int, metrics: SceneCutMetrics) -> None:
        """Register a new cut."""
        self.last_cut_frame = frame_index
        self.last_metrics = metrics
        self.transition_frames.clear()
        self.motion_frames.clear()
        self.black_frames.clear()
        self.moderate_score_frames.clear()
        self.consecutive_moderate = 0
        self.motion_frames_seen = 0
        self.black_frames_seen = 0

    def note_motion_frame(self, frame_index: int) -> None:
        """Record a high-motion frame, pruned to the ±5 scan window (AUD-17913).

        帧号单调递增是管线前提（can_cut_at 同样依赖），早于
        ``frame_index - MOTION_SCAN_RADIUS`` 的帧不可能再落入任何后续
        候选的 ±5 邻域，直接丢弃以防长素材列表无界增长 + 逐候选全表
        扫描的 O(N²)。
        """
        self.motion_frames.append(frame_index)
        self.motion_frames_seen += 1
        cutoff = frame_index - self.MOTION_SCAN_RADIUS
        while self.motion_frames and self.motion_frames[0] < cutoff:
            self.motion_frames.pop(0)

    def note_black_frame(self, frame_index: int) -> None:
        """Record a black frame, pruned to the ±3 scan window (AUD-17913)."""
        self.black_frames.append(frame_index)
        self.black_frames_seen += 1
        cutoff = frame_index - self.BLACK_SCAN_RADIUS
        while self.black_frames and self.black_frames[0] < cutoff:
            self.black_frames.pop(0)


# ================================================================
# Scene Cut Filter Pipeline
# ================================================================

class SceneCutFilterPipeline:
    """
    Pipeline for filtering and classifying scene cut candidates.
    
    Processes frame metrics to identify scene cut candidates,
    suppress false positives, and classify cut types.
    
    Usage:
    ```python
    pipeline = SceneCutFilterPipeline(settings)
    
    # Process frames
    entries = [...]  # List of FrameMetricsEntry
    candidates = pipeline.process(entries)
    
    # Get candidates
    for candidate in candidates:
        print(candidate.source_frame, candidate.kind, candidate.score)
    ```
    
    Attributes:
    - settings: SceneCutSettings configuration
    - diagnostics: Diagnostic information from filtering
    """
    
    def __init__(self, settings: SceneCutSettings) -> None:
        """
        Initialize filter pipeline.
        
        Args:
            settings: SceneCutSettings configuration
        """
        self._settings = settings

        # Rolling statistics for adaptive threshold
        self._rolling_stats = RollingStats(window_size=settings.rolling_window_size)

        # Detection state
        self._state = DetectionState()

        # Diagnostic information（重置逻辑见 _reset_diagnostics）
        self._diagnostics: dict[str, Any] = self._make_diagnostics()

    @staticmethod
    def _make_diagnostics() -> dict[str, Any]:
        """Build a fresh diagnostics dictionary."""
        return {
            "adaptive_threshold_used": False,
            "flash_suppressed_count": 0,
            "transition_detected_count": 0,
            "black_gap_detected_count": 0,
            "fast_motion_suppressed_count": 0,
            "min_distance_filtered_count": 0,
            "total_candidates": 0,
            "hard_cut_count": 0,
        }

    def _reset_diagnostics(self) -> None:
        """Rebuild the diagnostics table (AUD-17911).

        process() 会重建 _state/_rolling_stats，但此前不清 _diagnostics：
        同一 pipeline 实例二次 process 时 flash_suppressed_count 等计数器
        跨次累加，诊断数据翻倍失真。每次 process 从零开始计数。
        """
        self._diagnostics = self._make_diagnostics()
    
    @property
    def diagnostics(self) -> dict[str, Any]:
        """Get diagnostic information."""
        return self._diagnostics.copy()
    
    def process(
        self,
        entries: list[FrameMetricsEntry],
        progress_callback: Optional[Callable[[int, int, str], None]] = None,
    ) -> list[SceneCutCandidate]:
        """
        Process frame metrics entries to generate candidates.
        
        Applies all filters in sequence:
        1. adaptive_threshold - identify high score frames
        2. min_scene_distance - filter by distance from previous cut
        3. flash_guard - classify and suppress flashes
        4. transition_guard - classify transitions
        5. black_gap_detector - classify black gaps
        6. fast_motion_suppressor - classify and suppress fast motion
        
        Args:
            entries: List of FrameMetricsEntry
            progress_callback: Optional progress callback
        
        Returns:
            List of SceneCutCandidate
        """
        candidates: list[SceneCutCandidate] = []

        # Reset state（AUD-17911：诊断计数与 state/rolling_stats 一并归零）
        self._state = DetectionState()
        self._rolling_stats = RollingStats(window_size=self._settings.rolling_window_size)
        self._reset_diagnostics()
        
        total_entries = len(entries)
        
        if progress_callback:
            progress_callback(0, total_entries, "Starting filter pipeline...")
        
        # Adaptive threshold: the threshold applied to each frame is derived
        # from the rolling median/MAD window of PREVIOUS frames' scores only;
        # the current score joins the window after its own threshold has been
        # computed. A single global threshold taken from the final window
        # would describe only the last `rolling_window_size` frames of the
        # footage (AUD-17910).
        use_adaptive = self._settings.adaptive_threshold and len(entries) >= 5
        if self._settings.adaptive_threshold:
            self._diagnostics["adaptive_threshold_used"] = True

        # Sensitivity-based fixed threshold (also the rolling warm-up
        # fallback while the window holds fewer than 5 samples).
        threshold = 0.7 - self._settings.sensitivity * 0.5
        self._diagnostics["threshold_value"] = threshold

        # Second pass: apply filters
        prev_entry_metrics: Optional[SceneCutMetrics] = None
        for i, entry in enumerate(entries):
            # Update progress
            if progress_callback and i % 20 == 0:
                progress_callback(i, total_entries, f"Filtering frame {entry.frame.source_frame}")

            metrics = entry.metrics
            frame = entry.frame
            frame_index = frame.source_frame

            if use_adaptive:
                threshold = self._rolling_stats.get_threshold(
                    self._settings.sensitivity
                )
                # Current score enters the window only after this frame's
                # threshold has been derived (no self-participation).
                self._rolling_stats.update(metrics.combined_score)
                self._diagnostics["threshold_value"] = threshold

            # Store previous entry metrics for flash guard
            self._state.prev_entry_metrics = prev_entry_metrics

            # Check for black frames（AUD-17913：追加即按窗口裁剪）
            if metrics.black_score >= self._settings.black_score_threshold:
                self._state.note_black_frame(frame_index)

            # Check for high motion
            if metrics.motion_score >= self._settings.motion_score_threshold:
                self._state.note_motion_frame(frame_index)

            # Track moderate-score frames for dissolve/transition detection
            moderate_margin = 0.15
            if threshold <= metrics.combined_score < threshold + moderate_margin:
                self._state.moderate_score_frames.append(frame_index)
                self._state.consecutive_moderate += 1
            else:
                self._state.consecutive_moderate = 0

            # Skip if score below threshold
            if metrics.combined_score < threshold:
                continue

            # Check minimum distance from previous cut
            if not self._state.can_cut_at(frame_index, self._settings.min_scene_frames):
                self._diagnostics["min_distance_filtered_count"] += 1
                continue

            # Apply guards to classify and potentially suppress
            candidate = self._apply_guards(entry, metrics, threshold)

            if candidate is not None:
                candidates.append(candidate)
                self._state.register_cut(frame_index, metrics)

            prev_entry_metrics = metrics
            self._state.prev_guard_frame = frame_index
        
        # Post-process: handle transitions and black gaps
        candidates = self._post_process_candidates(candidates, entries)
        
        # Final update
        self._diagnostics["total_candidates"] = len(candidates)
        self._diagnostics["hard_cut_count"] = sum(
            1 for c in candidates if c.kind == SceneCutKind.HARD_CUT
        )
        
        if progress_callback:
            progress_callback(total_entries, total_entries, "Filter pipeline complete")
        
        return candidates
    
    def _apply_guards(
        self,
        entry: FrameMetricsEntry,
        metrics: SceneCutMetrics,
        threshold: float,
    ) -> Optional[SceneCutCandidate]:
        """
        Apply guard filters to classify detection.
        
        Args:
            entry: Frame metrics entry
            metrics: SceneCutMetrics for this frame
            threshold: Detection threshold
        
        Returns:
            SceneCutCandidate if not suppressed, None if suppressed
        """
        frame = entry.frame
        frame_index = frame.source_frame
        
        # Determine initial kind based on metrics
        kind = SceneCutKind.UNKNOWN
        reasons: list[str] = []
        
        # Flash guard: detect flash patterns
        # A real flash: brightness spike that returns to normal within 1-2 frames.
        # A hard cut: brightness change that persists (new scene).
        # Strategy: only classify as FLASH if flash_score significantly exceeds
        # what the luma_delta would explain (flash/luma ratio >= 1.5).
        if metrics.flash_score >= self._settings.flash_score_threshold:
            # Check if this is a true flash anomaly vs a normal scene change
            # True flash: high absolute flash_score (>= 0.5) or
            # flash_score >> luma_delta (disproportionate brightness spike)
            luma = max(metrics.luma_delta, 0.01)  # avoid div by zero
            flash_to_luma_ratio = metrics.flash_score / luma

            is_likely_flash = (
                metrics.flash_score >= 0.5
                or flash_to_luma_ratio >= 1.6
            )

            # Also check for bounce-back pattern
            # Only suppress if the previous passed entry was within 3 frames
            # (otherwise it's not a flash bounce but a distant scene change)
            if self._state.prev_entry_metrics is not None and self._state.prev_guard_frame >= 0:
                prev_dist = frame_index - self._state.prev_guard_frame
                prev = self._state.prev_entry_metrics
                if (prev_dist <= 3
                        and prev.flash_score >= self._settings.flash_score_threshold
                        and prev.luma_delta > metrics.luma_delta):
                    # This is the bounce-back frame of a flash
                    if self._settings.suppress_flash:
                        self._diagnostics["flash_suppressed_count"] += 1
                        return None

            if is_likely_flash:
                reasons.append("flash_like")
                kind = SceneCutKind.FLASH
                self._diagnostics["flash_suppressed_count"] += 1
        
        # Fast motion guard（AUD-17913：门控用累计计数器，语义与未裁剪
        # 的全表 len 一致；邻域扫描只遍历窗口内近期帧）
        if self._state.motion_frames_seen > 3:
            # Check if this is part of continuous motion sequence
            recent_motion = [
                f for f in self._state.motion_frames
                if abs(f - frame_index) <= 5
            ]
            if len(recent_motion) >= 3:
                kind = SceneCutKind.FAST_MOTION
                reasons.append("fast_motion_like")
                self._diagnostics["fast_motion_suppressed_count"] += 1
                
                if self._settings.suppress_fast_motion:
                    # Lower confidence but still include
                    pass
        
        # Black gap detection（AUD-17913：门控用累计计数器保持与旧的全表
        # len 语义一致——陈旧黑帧仍参与"是否曾见黑场"判定，邻域扫描则
        # 只遍历窗口内近期帧）
        if self._state.black_frames_seen > 0:
            # Check if near black frame sequence
            near_black = any(
                abs(f - frame_index) <= 3
                for f in self._state.black_frames
            )
            if near_black:
                # Check if this is a black-gap EXIT (next frame is not black)
                # vs a cut INSIDE the black gap (should be suppressed)
                is_black_exit = False
                if self._state.black_frames:
                    # Check if we're at the edge of a black region:
                    # current frame is near black but has non-trivial luma_delta
                    # (i.e. scene is transitioning FROM black TO content)
                    if metrics.luma_delta > 0.15 and metrics.combined_score > threshold:
                        is_black_exit = True

                if is_black_exit:
                    kind = SceneCutKind.BLACK_GAP
                    reasons.append("black_gap")
                    self._diagnostics["black_gap_detected_count"] += 1
                elif self._settings.suppress_black_fades:
                    # Inside black gap — suppress the cut
                    self._diagnostics["black_gap_detected_count"] += 1
                    return None
        
        # Transition detection (dissolve / soft transition)
        if self._settings.detect_transitions:
            # Detect dissolve pattern: several consecutive moderate-score frames
            # without a single dominant spike.
            # Pattern: N frames with combined_score near threshold (within margin),
            # no extreme flash, and consistent luma/color drift.
            moderate_count = self._state.consecutive_moderate
            is_sharp_spike = (
                metrics.combined_score >= threshold + 0.2
                or metrics.luma_delta >= 0.3
            )

            if moderate_count >= 2 and not is_sharp_spike:
                # This frame is part of a gradual transition, not a hard cut
                kind = SceneCutKind.SOFT_TRANSITION
                reasons.append("transition_like")
                self._diagnostics["transition_detected_count"] += 1
        
        # If no special kind detected, it's a hard cut
        if kind == SceneCutKind.UNKNOWN:
            kind = SceneCutKind.HARD_CUT
        
        # Add high score reason
        if metrics.combined_score >= threshold + 0.2:
            reasons.append("high_score")
        
        # Calculate confidence
        confidence = calculate_confidence(
            metrics.combined_score,
            kind,
            tuple(reasons),
        )
        
        # Create candidate
        candidate = SceneCutCandidate(
            source_frame=frame_index,
            pts=frame.pts,
            seconds=frame.seconds,
            score=metrics.combined_score,
            confidence=confidence,
            kind=kind,
            decision=SceneCutDecision.PENDING,
            metrics=metrics,
            reasons=tuple(reasons),
        )
        
        return candidate
    
    def _post_process_candidates(
        self,
        candidates: list[SceneCutCandidate],
        entries: list[FrameMetricsEntry],
    ) -> list[SceneCutCandidate]:
        """
        Post-process candidates for transitions and black gaps.

        Args:
            candidates: Initial list of candidates
            entries: All frame metrics entries

        Returns:
            Processed list of candidates
        """
        if not candidates:
            return candidates

        # Sort by frame index
        candidates.sort(key=lambda c: c.source_frame)

        # Build entry lookup for fast access
        entry_map: dict[int, FrameMetricsEntry] = {}
        for e in entries:
            entry_map[e.frame.source_frame] = e

        # === Dissolve detection ===
        # Look at the raw frame metrics around each candidate.
        # If there are many consecutive frames with similar moderate scores
        # (low luma, consistent hist) → it's a dissolve, not a hard cut.
        if self._settings.detect_transitions:
            new_candidates: list[SceneCutCandidate] = []
            for cand in candidates:
                if cand.kind != SceneCutKind.HARD_CUT:
                    new_candidates.append(cand)
                    continue

                # Count consecutive moderate-score frames around this candidate
                frame = cand.source_frame
                consecutive = 0
                max_score = 0.0
                min_score = 1.0
                total_luma = 0.0
                check_count = 0

                # Look backward and forward from candidate
                for offset in range(-2, 15):
                    f = frame + offset
                    if f in entry_map:
                        m = entry_map[f].metrics
                        if m.combined_score > 0:
                            consecutive += 1
                            max_score = max(max_score, m.combined_score)
                            min_score = min(min_score, m.combined_score)
                            total_luma += m.luma_delta
                            check_count += 1

                # Dissolve signature:
                # - 5+ consecutive frames with non-zero scores
                # - Score range is narrow (max - min < 0.05, i.e. very uniform)
                # - Average luma_delta is low (gradual, not sharp)
                if check_count >= 5:
                    score_spread = max_score - min_score
                    avg_luma = total_luma / check_count if check_count > 0 else 0
                    if score_spread < 0.05 and avg_luma < 0.05:
                        # Reclassify as SOFT_TRANSITION
                        cand = SceneCutCandidate(
                            source_frame=cand.source_frame,
                            pts=cand.pts,
                            seconds=cand.seconds,
                            score=cand.score,
                            confidence=cand.confidence * 0.7,
                            kind=SceneCutKind.SOFT_TRANSITION,
                            decision=SceneCutDecision.PENDING,
                            metrics=cand.metrics,
                            reasons=tuple(list(cand.reasons) + ["transition_like"]),
                        )
                        self._diagnostics["transition_detected_count"] += 1

                new_candidates.append(cand)
            candidates = new_candidates

        # === Black gap entry suppression ===
        # A black gap ENTRY (content → black) should NOT be a cut.
        # Only the EXIT (black → content) should be kept.
        # Pattern: candidate at frame F, and F+1..F+N are black frames
        if self._settings.suppress_black_fades and candidates and entry_map:
            # Build set of black frames from entries
            black_frame_set = set()
            for e in entries:
                if e.metrics.black_score >= self._settings.black_score_threshold:
                    black_frame_set.add(e.frame.source_frame)

            filtered_bg: list[SceneCutCandidate] = []
            for cand in candidates:
                f = cand.source_frame
                # Check if next few frames are black (this is a gap ENTRY)
                black_count_after = 0
                for offset in range(1, 6):
                    if (f + offset) in black_frame_set:
                        black_count_after += 1
                    else:
                        break

                if black_count_after >= 3:
                    # This is a black gap ENTRY — suppress it
                    self._diagnostics["black_gap_detected_count"] += 1
                    continue

                # Check if previous frames were black (this is a gap EXIT)
                black_count_before = 0
                for offset in range(1, 6):
                    if (f - offset) in black_frame_set:
                        black_count_before += 1
                    else:
                        break

                if black_count_before >= 3 and cand.kind == SceneCutKind.HARD_CUT:
                    # This is a black gap EXIT — reclassify as BLACK_GAP
                    cand = SceneCutCandidate(
                        source_frame=cand.source_frame,
                        pts=cand.pts,
                        seconds=cand.seconds,
                        score=cand.score,
                        confidence=cand.confidence,
                        kind=SceneCutKind.BLACK_GAP,
                        decision=SceneCutDecision.PENDING,
                        metrics=cand.metrics,
                        reasons=tuple(list(cand.reasons) + ["black_gap"]),
                    )
                    self._diagnostics["black_gap_detected_count"] += 1

                filtered_bg.append(cand)
            candidates = filtered_bg

        # Flash bounce suppression: if two candidates are very close (1-3 frames)
        # and both have high flash metrics, the second is likely a flash bounce-back.
        # Remove the weaker one.
        if self._settings.suppress_flash and len(candidates) > 1:
            filtered: list[SceneCutCandidate] = [candidates[0]]
            for i in range(1, len(candidates)):
                prev_c = filtered[-1]
                curr_c = candidates[i]
                distance = curr_c.source_frame - prev_c.source_frame

                if distance <= 3:
                    # Very close pair - check for flash pattern
                    prev_flash = prev_c.metrics.flash_score if prev_c.metrics else 0
                    curr_flash = curr_c.metrics.flash_score if curr_c.metrics else 0

                    if prev_flash >= self._settings.flash_score_threshold or \
                       curr_flash >= self._settings.flash_score_threshold:
                        # Keep the one with higher score (likely the actual event)
                        if curr_c.score > prev_c.score:
                            filtered[-1] = curr_c
                        # Skip the other (flash bounce)
                        self._diagnostics["flash_suppressed_count"] += 1
                        continue

                filtered.append(curr_c)
            candidates = filtered

        # Check for transition patterns between close candidates
        if self._settings.detect_transitions:
            for i in range(len(candidates) - 1):
                curr = candidates[i]
                next_c = candidates[i + 1]

                distance = next_c.source_frame - curr.source_frame

                # If candidates are close, might be part of transition
                if distance <= self._settings.transition_frame_threshold:
                    # Check if scores are similar (transition pattern)
                    score_diff = abs(curr.score - next_c.score)

                    if score_diff < 0.15:
                        # Mark as potential transition
                        # Keep the first one, mark second as transition
                        if next_c.kind == SceneCutKind.HARD_CUT:
                            # Re-evaluate: might be transition
                            candidates[i + 1] = SceneCutCandidate(
                                source_frame=next_c.source_frame,
                                pts=next_c.pts,
                                seconds=next_c.seconds,
                                score=next_c.score,
                                confidence=next_c.confidence * 0.7,
                                kind=SceneCutKind.SOFT_TRANSITION,
                                decision=SceneCutDecision.PENDING,
                                metrics=next_c.metrics,
                                reasons=tuple(list(next_c.reasons) + ["transition_like"]),
                            )
        
        return candidates


# ================================================================
# Standalone Filter Functions
# ================================================================

def adaptive_threshold(
    entries: list[FrameMetricsEntry],
    sensitivity: float,
    window_size: int = 30,
) -> float:
    """
    Calculate adaptive threshold using rolling median/MAD.
    
    Args:
        entries: List of FrameMetricsEntry
        sensitivity: Sensitivity value [0.35, 0.75]
        window_size: Rolling window size
    
    Returns:
        Adaptive threshold value
    """
    stats = RollingStats(window_size=window_size)
    
    for entry in entries:
        stats.update(entry.metrics.combined_score)
    
    return stats.get_threshold(sensitivity)


def min_scene_distance(
    candidates: list[SceneCutCandidate],
    min_frames: int,
) -> list[SceneCutCandidate]:
    """
    Filter candidates by minimum distance from previous cut.
    
    Args:
        candidates: List of SceneCutCandidate
        min_frames: Minimum frames between cuts
    
    Returns:
        Filtered list of candidates
    """
    if not candidates:
        return candidates
    
    # Sort by frame index
    candidates.sort(key=lambda c: c.source_frame)
    
    filtered: list[SceneCutCandidate] = []
    last_frame = -1000
    
    for candidate in candidates:
        if candidate.source_frame - last_frame >= min_frames:
            filtered.append(candidate)
            last_frame = candidate.source_frame
    
    return filtered


def flash_guard(
    candidate: SceneCutCandidate,
    flash_threshold: float = 0.8,
    suppress: bool = True,
) -> Optional[SceneCutCandidate]:
    """
    Check if candidate is a flash (single-frame brightness spike).
    
    Args:
        candidate: SceneCutCandidate to check
        flash_threshold: Flash score threshold
        suppress: Whether to suppress flash detections
    
    Returns:
        Modified candidate (FLASH kind), or None if suppressed
    """
    if candidate.metrics is None:
        return candidate
    
    if candidate.metrics.flash_score >= flash_threshold:
        # Mark as flash
        flash_candidate = SceneCutCandidate(
            source_frame=candidate.source_frame,
            pts=candidate.pts,
            seconds=candidate.seconds,
            score=candidate.score,
            confidence=candidate.confidence * 0.3,
            kind=SceneCutKind.FLASH,
            decision=SceneCutDecision.PENDING,
            metrics=candidate.metrics,
            reasons=tuple(list(candidate.reasons) + ["flash_like"]),
        )
        
        if suppress:
            return None
        return flash_candidate
    
    return candidate


def transition_guard(
    candidates: list[SceneCutCandidate],
    frame_threshold: int = 20,
) -> list[SceneCutCandidate]:
    """
    Identify and mark gradual transitions.
    
    Args:
        candidates: List of SceneCutCandidate
        frame_threshold: Maximum frames for transition
    
    Returns:
        Modified list with transitions marked
    """
    if len(candidates) < 2:
        return candidates
    
    # Sort by frame index
    candidates = list(candidates)
    candidates.sort(key=lambda c: c.source_frame)
    
    result: list[SceneCutCandidate] = []
    
    for i, candidate in enumerate(candidates):
        if i == 0:
            result.append(candidate)
            continue
        
        prev = candidates[i - 1]
        distance = candidate.source_frame - prev.source_frame
        
        if distance <= frame_threshold:
            # Close candidates might be transition
            score_diff = abs(candidate.score - prev.score)
            
            if score_diff < 0.15:
                # Mark as transition
                transition_candidate = SceneCutCandidate(
                    source_frame=candidate.source_frame,
                    pts=candidate.pts,
                    seconds=candidate.seconds,
                    score=candidate.score,
                    confidence=candidate.confidence * 0.7,
                    kind=SceneCutKind.SOFT_TRANSITION,
                    decision=SceneCutDecision.PENDING,
                    metrics=candidate.metrics,
                    reasons=tuple(list(candidate.reasons) + ["transition_like"]),
                )
                result.append(transition_candidate)
            else:
                result.append(candidate)
        else:
            result.append(candidate)
    
    return result


def black_gap_detector(
    entries: list[FrameMetricsEntry],
    black_threshold: float = 0.95,
) -> list[tuple[int, int]]:
    """
    Detect black frame intervals.
    
    Args:
        entries: List of FrameMetricsEntry
        black_threshold: Black score threshold
    
    Returns:
        List of (start_frame, end_frame) tuples for black gaps
    """
    black_gaps: list[tuple[int, int]] = []
    
    in_gap = False
    gap_start = -1
    
    for entry in entries:
        if entry.metrics.black_score >= black_threshold:
            if not in_gap:
                in_gap = True
                gap_start = entry.frame.source_frame
        else:
            if in_gap:
                gap_end = entry.frame.source_frame
                if gap_end - gap_start >= 2:  # Minimum 2 frames
                    black_gaps.append((gap_start, gap_end))
                in_gap = False
                gap_start = -1
    
    # Handle gap at end
    if in_gap and gap_start >= 0:
        gap_end = entries[-1].frame.source_frame
        black_gaps.append((gap_start, gap_end))
    
    return black_gaps


def fast_motion_suppressor(
    entries: list[FrameMetricsEntry],
    motion_threshold: float = 0.5,
    window_size: int = 5,
) -> set[int]:
    """
    Identify frames with continuous high motion.
    
    Args:
        entries: List of FrameMetricsEntry
        motion_threshold: Motion score threshold
        window_size: Window to check for continuous motion
    
    Returns:
        Set of frame indices that should be suppressed
    """
    suppressed: set[int] = set()
    
    # Find motion frames
    motion_frames: list[int] = []
    for entry in entries:
        if entry.metrics.motion_score >= motion_threshold:
            motion_frames.append(entry.frame.source_frame)
    
    # Find continuous motion sequences
    if len(motion_frames) >= window_size:
        for i in range(len(motion_frames) - window_size + 1):
            sequence = motion_frames[i:i + window_size]
            
            # Check if frames are continuous
            is_continuous = all(
                sequence[j + 1] - sequence[j] <= 2
                for j in range(len(sequence) - 1)
            )
            
            if is_continuous:
                # Mark all frames in sequence as suppressed
                suppressed.update(sequence)
    
    return suppressed


# ================================================================
# Module Exports
# ================================================================

__all__ = [
    # Data classes
    "FrameMetricsEntry",
    "RollingStats",
    "DetectionState",
    
    # Main class
    "SceneCutFilterPipeline",
    
    # Standalone functions
    "adaptive_threshold",
    "min_scene_distance",
    "flash_guard",
    "transition_guard",
    "black_gap_detector",
    "fast_motion_suppressor",
]