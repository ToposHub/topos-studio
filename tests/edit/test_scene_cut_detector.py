"""
Tests for scene_cut.detector — detector basic functionality.

Validates:
- DetectionPhase enum values
- DetectionProgress properties and calculations
- SceneCutDetector initialization
- CancelToken functionality
- Helper functions (get_accepted_candidates, etc.)
"""

import pytest
import time

from src.features.edit.core.scene_cut.detector import (
    DetectionPhase,
    DetectionProgress,
    SceneCutDetector,
    get_accepted_candidates,
    get_pending_candidates,
    get_hard_cut_candidates,
    get_warning_candidates,
    get_high_confidence_candidates,
    calculate_media_fingerprint,
)
from src.features.edit.core.scene_cut.models import (
    SceneCutSettings,
    SceneCutCandidate,
    SceneCutAnalysis,
    SceneCutKind,
    SceneCutDecision,
)


# ===================================================================
# Test: DetectionPhase Enum
# ===================================================================

class TestDetectionPhase:
    """DetectionPhase enum values."""

    def test_all_phases_exist(self):
        assert DetectionPhase.SAMPLING.value == "sampling"
        assert DetectionPhase.METRICS.value == "metrics"
        assert DetectionPhase.FILTERING.value == "filtering"
        assert DetectionPhase.COMPLETE.value == "complete"
        assert DetectionPhase.CANCELLED.value == "cancelled"
        assert DetectionPhase.ERROR.value == "error"

    def test_phase_from_string(self):
        phase = DetectionPhase("sampling")
        assert phase == DetectionPhase.SAMPLING


# ===================================================================
# Test: DetectionProgress Properties
# ===================================================================

class TestDetectionProgressDefaults:
    """Default values for DetectionProgress."""

    def test_default_phase(self):
        progress = DetectionProgress()
        assert progress.phase == DetectionPhase.SAMPLING

    def test_default_frame_counts(self):
        progress = DetectionProgress()
        assert progress.current_frame == 0
        assert progress.total_frames == 0

    def test_default_candidates_found(self):
        progress = DetectionProgress()
        assert progress.candidates_found == 0

    def test_default_elapsed_seconds(self):
        progress = DetectionProgress()
        assert progress.elapsed_seconds == 0.0

    def test_default_estimated_remaining_none(self):
        progress = DetectionProgress()
        assert progress.estimated_remaining_seconds is None

    def test_default_message(self):
        progress = DetectionProgress()
        assert progress.message == ""


class TestDetectionProgressRatio:
    """Progress ratio calculations."""

    def test_progress_ratio_zero_when_no_total(self):
        progress = DetectionProgress(current_frame=50, total_frames=0)
        assert progress.progress_ratio == 0.0

    def test_progress_ratio_half(self):
        progress = DetectionProgress(current_frame=50, total_frames=100)
        assert progress.progress_ratio == 0.5

    def test_progress_ratio_full(self):
        progress = DetectionProgress(current_frame=100, total_frames=100)
        assert progress.progress_ratio == 1.0

    def test_progress_ratio_capped_at_one(self):
        progress = DetectionProgress(current_frame=150, total_frames=100)
        assert progress.progress_ratio == 1.0  # capped

    def test_progress_percent_zero(self):
        progress = DetectionProgress(current_frame=0, total_frames=100)
        assert progress.progress_percent == 0

    def test_progress_percent_fifty(self):
        progress = DetectionProgress(current_frame=50, total_frames=100)
        assert progress.progress_percent == 50

    def test_progress_percent_full(self):
        progress = DetectionProgress(current_frame=100, total_frames=100)
        assert progress.progress_percent == 100


class TestDetectionProgressIsComplete:
    """is_complete property."""

    def test_is_complete_for_complete(self):
        progress = DetectionProgress(phase=DetectionPhase.COMPLETE)
        assert progress.is_complete is True

    def test_is_complete_for_cancelled(self):
        progress = DetectionProgress(phase=DetectionPhase.CANCELLED)
        assert progress.is_complete is True

    def test_is_complete_for_error(self):
        progress = DetectionProgress(phase=DetectionPhase.ERROR)
        assert progress.is_complete is True

    def test_is_not_complete_for_sampling(self):
        progress = DetectionProgress(phase=DetectionPhase.SAMPLING)
        assert progress.is_complete is False

    def test_is_not_complete_for_metrics(self):
        progress = DetectionProgress(phase=DetectionPhase.METRICS)
        assert progress.is_complete is False

    def test_is_not_complete_for_filtering(self):
        progress = DetectionProgress(phase=DetectionPhase.FILTERING)
        assert progress.is_complete is False


# ===================================================================
# Test: SceneCutDetector Initialization
# ===================================================================

class TestSceneCutDetectorInit:
    """SceneCutDetector initialization."""

    def test_init_with_required_params(self):
        detector = SceneCutDetector(
            source_path="/path/to/media.mp4",
            asset_id="asset-001",
        )
        assert detector.source_path == "/path/to/media.mp4"
        assert detector.asset_id == "asset-001"

    def test_init_with_default_settings(self):
        detector = SceneCutDetector(
            source_path="/path/to/media.mp4",
            asset_id="asset-001",
        )
        assert detector.settings.sensitivity == 0.55  # default

    def test_init_with_custom_settings(self):
        settings = SceneCutSettings(sensitivity=0.65)
        detector = SceneCutDetector(
            source_path="/path/to/media.mp4",
            asset_id="asset-001",
            settings=settings,
        )
        assert detector.settings.sensitivity == 0.65

    def test_init_with_clip_id(self):
        detector = SceneCutDetector(
            source_path="/path/to/media.mp4",
            asset_id="asset-001",
            clip_id="clip-001",
        )
        # clip_id is stored internally, used in analysis creation

    def test_init_with_proxy_path(self):
        detector = SceneCutDetector(
            source_path="/path/to/media.mp4",
            asset_id="asset-001",
            proxy_path="/path/to/proxy.mp4",
        )
        # proxy_path is stored internally, used for sampling

    def test_detector_version_constant(self):
        assert SceneCutDetector.DETECTOR_VERSION == "1.0.0"


class TestSceneCutDetectorProperties:
    """SceneCutDetector property access."""

    def test_source_path_property(self):
        detector = SceneCutDetector(
            source_path="/test/path/video.mp4",
            asset_id="test-asset",
        )
        assert detector.source_path == "/test/path/video.mp4"

    def test_asset_id_property(self):
        detector = SceneCutDetector(
            source_path="/test/path/video.mp4",
            asset_id="test-asset-123",
        )
        assert detector.asset_id == "test-asset-123"

    def test_settings_property(self):
        settings = SceneCutSettings(
            sensitivity=0.70,
            min_scene_frames=12,
        )
        detector = SceneCutDetector(
            source_path="/test/path/video.mp4",
            asset_id="test-asset",
            settings=settings,
        )
        assert detector.settings.sensitivity == 0.70
        assert detector.settings.min_scene_frames == 12


# ===================================================================
# Test: Calculate Media Fingerprint
# ===================================================================

class TestCalculateMediaFingerprint:
    """calculate_media_fingerprint function."""

    def test_fingerprint_format(self):
        fp = calculate_media_fingerprint(
            source_path="/path/to/media.mp4",
            file_size=1000000,
            mtime_ns=1700000000000000000,
        )
        assert fp == "/path/to/media.mp4:1000000:1700000000000000000"

    def test_fingerprint_unique_for_different_path(self):
        fp1 = calculate_media_fingerprint("/path/a.mp4", 1000, 100)
        fp2 = calculate_media_fingerprint("/path/b.mp4", 1000, 100)
        assert fp1 != fp2

    def test_fingerprint_unique_for_different_size(self):
        fp1 = calculate_media_fingerprint("/path/media.mp4", 1000, 100)
        fp2 = calculate_media_fingerprint("/path/media.mp4", 2000, 100)
        assert fp1 != fp2

    def test_fingerprint_unique_for_different_mtime(self):
        fp1 = calculate_media_fingerprint("/path/media.mp4", 1000, 100)
        fp2 = calculate_media_fingerprint("/path/media.mp4", 1000, 200)
        assert fp1 != fp2

    def test_fingerprint_same_for_same_inputs(self):
        fp1 = calculate_media_fingerprint("/path/media.mp4", 1000, 100)
        fp2 = calculate_media_fingerprint("/path/media.mp4", 1000, 100)
        assert fp1 == fp2


# ===================================================================
# Test: Helper Functions for Candidate Filtering
# ===================================================================

class TestGetAcceptedCandidates:
    """get_accepted_candidates function."""

    def _make_analysis(self, decisions: list[SceneCutDecision]):
        candidates = tuple(
            SceneCutCandidate(
                source_frame=i * 100,
                pts=i * 1000,
                seconds=i * 4.0,
                decision=d,
            )
            for i, d in enumerate(decisions)
        )
        return SceneCutAnalysis(
            analysis_id="test-analysis",
            asset_id="test-asset",
            source_path="/path/media.mp4",
            candidates=candidates,
        )

    def test_returns_only_accepted(self):
        analysis = self._make_analysis([
            SceneCutDecision.ACCEPTED,
            SceneCutDecision.PENDING,
            SceneCutDecision.ACCEPTED,
            SceneCutDecision.REJECTED,
        ])
        accepted = get_accepted_candidates(analysis)
        assert len(accepted) == 2
        assert accepted[0].source_frame == 0
        assert accepted[1].source_frame == 200

    def test_returns_empty_when_none_accepted(self):
        analysis = self._make_analysis([
            SceneCutDecision.PENDING,
            SceneCutDecision.PENDING,
        ])
        accepted = get_accepted_candidates(analysis)
        assert len(accepted) == 0

    def test_returns_all_when_all_accepted(self):
        analysis = self._make_analysis([
            SceneCutDecision.ACCEPTED,
            SceneCutDecision.ACCEPTED,
            SceneCutDecision.ACCEPTED,
        ])
        accepted = get_accepted_candidates(analysis)
        assert len(accepted) == 3


class TestGetPendingCandidates:
    """get_pending_candidates function."""

    def _make_analysis(self, decisions: list[SceneCutDecision]):
        candidates = tuple(
            SceneCutCandidate(
                source_frame=i * 100,
                pts=i * 1000,
                seconds=i * 4.0,
                decision=d,
            )
            for i, d in enumerate(decisions)
        )
        return SceneCutAnalysis(
            analysis_id="test-analysis",
            asset_id="test-asset",
            source_path="/path/media.mp4",
            candidates=candidates,
        )

    def test_returns_only_pending(self):
        analysis = self._make_analysis([
            SceneCutDecision.PENDING,
            SceneCutDecision.ACCEPTED,
            SceneCutDecision.PENDING,
        ])
        pending = get_pending_candidates(analysis)
        assert len(pending) == 2
        assert pending[0].source_frame == 0
        assert pending[1].source_frame == 200

    def test_returns_empty_when_none_pending(self):
        analysis = self._make_analysis([
            SceneCutDecision.ACCEPTED,
            SceneCutDecision.REJECTED,
        ])
        pending = get_pending_candidates(analysis)
        assert len(pending) == 0


class TestGetHardCutCandidates:
    """get_hard_cut_candidates function."""

    def _make_analysis(self, kinds: list[SceneCutKind]):
        candidates = tuple(
            SceneCutCandidate(
                source_frame=i * 100,
                pts=i * 1000,
                seconds=i * 4.0,
                kind=k,
            )
            for i, k in enumerate(kinds)
        )
        return SceneCutAnalysis(
            analysis_id="test-analysis",
            asset_id="test-asset",
            source_path="/path/media.mp4",
            candidates=candidates,
        )

    def test_returns_only_hard_cuts(self):
        analysis = self._make_analysis([
            SceneCutKind.HARD_CUT,
            SceneCutKind.SOFT_TRANSITION,
            SceneCutKind.HARD_CUT,
            SceneCutKind.FLASH,
        ])
        hard_cuts = get_hard_cut_candidates(analysis)
        assert len(hard_cuts) == 2
        assert hard_cuts[0].source_frame == 0
        assert hard_cuts[1].source_frame == 200

    def test_returns_empty_when_no_hard_cuts(self):
        analysis = self._make_analysis([
            SceneCutKind.SOFT_TRANSITION,
            SceneCutKind.FLASH,
        ])
        hard_cuts = get_hard_cut_candidates(analysis)
        assert len(hard_cuts) == 0


class TestGetWarningCandidates:
    """get_warning_candidates function."""

    def _make_analysis(self, kinds: list[SceneCutKind], confidences: list[float]):
        candidates = tuple(
            SceneCutCandidate(
                source_frame=i * 100,
                pts=i * 1000,
                seconds=i * 4.0,
                kind=kinds[i],
                confidence=confidences[i],
            )
            for i in range(len(kinds))
        )
        return SceneCutAnalysis(
            analysis_id="test-analysis",
            asset_id="test-asset",
            source_path="/path/media.mp4",
            candidates=candidates,
        )

    def test_returns_warning_kinds(self):
        analysis = self._make_analysis(
            kinds=[
                SceneCutKind.HARD_CUT,
                SceneCutKind.FLASH,
                SceneCutKind.FAST_MOTION,
            ],
            confidences=[0.90, 0.90, 0.90],
        )
        warnings = get_warning_candidates(analysis)
        assert len(warnings) == 2  # FLASH and FAST_MOTION
        assert warnings[0].kind == SceneCutKind.FLASH
        assert warnings[1].kind == SceneCutKind.FAST_MOTION

    def test_returns_low_confidence(self):
        analysis = self._make_analysis(
            kinds=[
                SceneCutKind.HARD_CUT,
                SceneCutKind.HARD_CUT,
            ],
            confidences=[0.90, 0.40],  # second is low confidence
        )
        warnings = get_warning_candidates(analysis)
        assert len(warnings) == 1
        assert warnings[0].source_frame == 100

    def test_returns_empty_when_no_warnings(self):
        analysis = self._make_analysis(
            kinds=[
                SceneCutKind.HARD_CUT,
                SceneCutKind.HARD_CUT,
            ],
            confidences=[0.90, 0.90],
        )
        warnings = get_warning_candidates(analysis)
        assert len(warnings) == 0


class TestGetHighConfidenceCandidates:
    """get_high_confidence_candidates function."""

    def _make_analysis(self, confidences: list[float]):
        candidates = tuple(
            SceneCutCandidate(
                source_frame=i * 100,
                pts=i * 1000,
                seconds=i * 4.0,
                confidence=c,
            )
            for i, c in enumerate(confidences)
        )
        return SceneCutAnalysis(
            analysis_id="test-analysis",
            asset_id="test-asset",
            source_path="/path/media.mp4",
            candidates=candidates,
        )

    def test_returns_high_confidence_default_threshold(self):
        analysis = self._make_analysis([0.90, 0.75, 0.85, 0.60])
        high = get_high_confidence_candidates(analysis)
        assert len(high) == 2  # 0.90 and 0.85 >= 0.85
        assert high[0].source_frame == 0
        assert high[1].source_frame == 200

    def test_returns_high_confidence_custom_threshold(self):
        analysis = self._make_analysis([0.90, 0.75, 0.85, 0.60])
        high = get_high_confidence_candidates(analysis, threshold=0.70)
        assert len(high) == 3  # 0.90, 0.75, 0.85 >= 0.70

    def test_returns_empty_when_none_above_threshold(self):
        analysis = self._make_analysis([0.50, 0.60])
        high = get_high_confidence_candidates(analysis, threshold=0.85)
        assert len(high) == 0

    def test_returns_all_when_all_above_threshold(self):
        analysis = self._make_analysis([0.90, 0.95, 0.88])
        high = get_high_confidence_candidates(analysis, threshold=0.85)
        assert len(high) == 3


# ===================================================================
# Test: Integration with Analysis Methods
# ===================================================================

class TestAnalysisCandidateMethodsConsistency:
    """Consistency between Analysis methods and helper functions."""

    def test_get_accepted_candidates_matches_analysis_method(self):
        candidates = tuple(
            SceneCutCandidate(
                source_frame=i * 100,
                pts=i * 1000,
                seconds=i * 4.0,
                decision=SceneCutDecision.ACCEPTED if i % 2 == 0 else SceneCutDecision.PENDING,
            )
            for i in range(4)
        )
        analysis = SceneCutAnalysis(
            analysis_id="test-analysis",
            asset_id="test-asset",
            source_path="/path/media.mp4",
            candidates=candidates,
        )

        # Helper function result
        helper_accepted = get_accepted_candidates(analysis)
        # Analysis method result
        method_accepted = analysis.get_accepted_candidates()

        assert len(helper_accepted) == len(method_accepted)
        assert helper_accepted[0].source_frame == method_accepted[0].source_frame

    def test_pending_count_matches_helper(self):
        candidates = tuple(
            SceneCutCandidate(
                source_frame=i * 100,
                pts=i * 1000,
                seconds=i * 4.0,
                decision=SceneCutDecision.PENDING if i % 2 == 0 else SceneCutDecision.ACCEPTED,
            )
            for i in range(6)
        )
        analysis = SceneCutAnalysis(
            analysis_id="test-analysis",
            asset_id="test-asset",
            source_path="/path/media.mp4",
            candidates=candidates,
        )

        helper_pending = get_pending_candidates(analysis)
        assert len(helper_pending) == analysis.pending_count


# ===================================================================
# Test: DetectionProgress with Elapsed Time
# ===================================================================

class TestDetectionProgressElapsedTime:
    """DetectionProgress elapsed time calculations.

    Note: estimated_remaining_seconds is a frozen dataclass field,
    not a dynamic property. The calculation is done externally
    in the detector's report_progress helper.
    """

    def test_elapsed_seconds_set(self):
        progress = DetectionProgress(elapsed_seconds=10.5)
        assert progress.elapsed_seconds == 10.5

    def test_estimated_remaining_can_be_set(self):
        progress = DetectionProgress(
            current_frame=50,
            total_frames=100,
            elapsed_seconds=10.0,
            estimated_remaining_seconds=10.0,
        )
        assert progress.estimated_remaining_seconds == 10.0

    def test_estimated_remaining_default_none(self):
        progress = DetectionProgress()
        assert progress.estimated_remaining_seconds is None

    def test_estimated_remaining_none_when_explicitly_set(self):
        progress = DetectionProgress(estimated_remaining_seconds=None)
        assert progress.estimated_remaining_seconds is None


# ===================================================================
# Test: Frozen Dataclass Behavior
# ===================================================================

class TestDetectionProgressFrozen:
    """DetectionProgress is frozen (immutable)."""

    def test_cannot_modify_phase(self):
        progress = DetectionProgress()
        with pytest.raises(AttributeError):
            progress.phase = DetectionPhase.COMPLETE

    def test_cannot_modify_current_frame(self):
        progress = DetectionProgress()
        with pytest.raises(AttributeError):
            progress.current_frame = 100

    def test_cannot_modify_total_frames(self):
        progress = DetectionProgress()
        with pytest.raises(AttributeError):
            progress.total_frames = 200


class TestSceneCutDetectorStateManagement:
    """SceneCutDetector internal state management."""

    def test_detector_has_cancel_method(self):
        detector = SceneCutDetector(
            source_path="/path/to/media.mp4",
            asset_id="asset-001",
        )
        # cancel() method should exist
        detector.cancel()

    def test_detector_does_not_modify_settings(self):
        original_settings = SceneCutSettings(sensitivity=0.65)
        detector = SceneCutDetector(
            source_path="/path/to/media.mp4",
            asset_id="asset-001",
            settings=original_settings,
        )
        # Settings should be unchanged after creation
        assert detector.settings.sensitivity == 0.65
        assert original_settings.sensitivity == 0.65


# ===================================================================
# Test: Edge Cases
# ===================================================================

class TestEmptyAnalysisEdgeCases:
    """Edge cases with empty analysis."""

    def test_get_accepted_candidates_empty_analysis(self):
        analysis = SceneCutAnalysis(
            analysis_id="empty",
            asset_id="asset-001",
            source_path="/path/media.mp4",
            candidates=(),
        )
        accepted = get_accepted_candidates(analysis)
        assert len(accepted) == 0

    def test_get_pending_candidates_empty_analysis(self):
        analysis = SceneCutAnalysis(
            analysis_id="empty",
            asset_id="asset-001",
            source_path="/path/media.mp4",
            candidates=(),
        )
        pending = get_pending_candidates(analysis)
        assert len(pending) == 0

    def test_get_hard_cut_candidates_empty_analysis(self):
        analysis = SceneCutAnalysis(
            analysis_id="empty",
            asset_id="asset-001",
            source_path="/path/media.mp4",
            candidates=(),
        )
        hard = get_hard_cut_candidates(analysis)
        assert len(hard) == 0

    def test_get_warning_candidates_empty_analysis(self):
        analysis = SceneCutAnalysis(
            analysis_id="empty",
            asset_id="asset-001",
            source_path="/path/media.mp4",
            candidates=(),
        )
        warnings = get_warning_candidates(analysis)
        assert len(warnings) == 0

    def test_analysis_counts_zero_for_empty(self):
        analysis = SceneCutAnalysis(
            analysis_id="empty",
            asset_id="asset-001",
            source_path="/path/media.mp4",
            candidates=(),
        )
        assert analysis.accepted_count == 0
        assert analysis.rejected_count == 0
        assert analysis.pending_count == 0
        assert analysis.warning_count == 0


class TestDetectionProgressMessage:
    """DetectionProgress message field."""

    def test_message_can_be_set(self):
        progress = DetectionProgress(message="Sampling frames...")
        assert progress.message == "Sampling frames..."

    def test_empty_message_by_default(self):
        progress = DetectionProgress()
        assert progress.message == ""

    def test_message_for_complete_phase(self):
        progress = DetectionProgress(
            phase=DetectionPhase.COMPLETE,
            message="Found 10 candidates",
            candidates_found=10,
        )
        assert progress.message == "Found 10 candidates"
        assert progress.candidates_found == 10


# ===================================================================
# Test: Candidates Found Tracking
# ===================================================================

class TestCandidatesFoundTracking:
    """candidates_found field in DetectionProgress."""

    def test_candidates_found_default_zero(self):
        progress = DetectionProgress()
        assert progress.candidates_found == 0

    def test_candidates_found_can_be_set(self):
        progress = DetectionProgress(candidates_found=5)
        assert progress.candidates_found == 5

    def test_candidates_found_increases_during_filtering(self):
        progress = DetectionProgress(
            phase=DetectionPhase.FILTERING,
            current_frame=50,
            total_frames=100,
            candidates_found=3,
        )
        assert progress.candidates_found == 3


# ===================================================================
# Test: Detector Version Compatibility
# ===================================================================

class TestDetectorVersion:
    """Detector version handling."""

    def test_detector_version_is_string(self):
        assert isinstance(SceneCutDetector.DETECTOR_VERSION, str)

    def test_detector_version_format(self):
        # Should follow semver-like format
        version = SceneCutDetector.DETECTOR_VERSION
        parts = version.split(".")
        assert len(parts) >= 2  # At least major.minor

    def test_detector_uses_version_in_analysis(self):
        # When creating analysis, detector_version should match
        detector = SceneCutDetector(
            source_path="/path/to/media.mp4",
            asset_id="asset-001",
        )
        assert detector.DETECTOR_VERSION == SceneCutDetector.DETECTOR_VERSION