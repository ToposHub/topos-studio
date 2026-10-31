"""
Tests for scene_cut.models — data models for scene cut detection.

Validates:
- SceneCutSettings validation and defaults
- SceneCutCandidate decision state judgments
- SceneCutAnalysis serialization (to_dict/from_dict)
- SceneSubclipSpec generation (generate_scene_subclips)
- Timecode conversion functions
- SourceFrameTimeIndex mapping functionality
"""

import pytest
import time
import hashlib

from src.features.edit.core.scene_cut.models import (
    SceneCutSettings,
    SceneCutCandidate,
    SceneCutAnalysis,
    SceneCutMetrics,
    SceneCutKind,
    SceneCutDecision,
    SceneSubclipSpec,
    FrameTimePoint,
    SourceFrameTimeIndex,
    generate_analysis_id,
    generate_scene_subclips,
    timecode_to_frames,
    _frames_to_timecode,
    DETECTOR_VERSION,
)


# ===================================================================
# Test: SceneCutSettings
# ===================================================================

class TestSceneCutSettingsDefaults:
    """Default values for SceneCutSettings."""

    def test_default_sensitivity(self):
        settings = SceneCutSettings()
        assert settings.sensitivity == 0.55

    def test_default_min_scene_frames(self):
        settings = SceneCutSettings()
        assert settings.min_scene_frames == 8

    def test_default_sample_dimensions(self):
        settings = SceneCutSettings()
        assert settings.sample_width == 256
        assert settings.max_sample_height == 144

    def test_default_boolean_flags(self):
        settings = SceneCutSettings()
        assert settings.use_proxy is True
        assert settings.detect_transitions is True
        assert settings.suppress_flash is True
        assert settings.suppress_fast_motion is True
        assert settings.suppress_black_fades is True
        assert settings.adaptive_threshold is True

    def test_default_frame_range(self):
        settings = SceneCutSettings()
        assert settings.start_source_frame == 0
        assert settings.end_source_frame is None


class TestSceneCutSettingsValidation:
    """Validation of SceneCutSettings parameters."""

    def test_sensitivity_below_minimum_raises(self):
        with pytest.raises(ValueError, match="sensitivity must be between"):
            SceneCutSettings(sensitivity=0.34)

    def test_sensitivity_above_maximum_raises(self):
        with pytest.raises(ValueError, match="sensitivity must be between"):
            SceneCutSettings(sensitivity=0.76)

    def test_sensitivity_at_boundaries_ok(self):
        # Lower boundary
        s1 = SceneCutSettings(sensitivity=0.35)
        assert s1.sensitivity == 0.35
        # Upper boundary
        s2 = SceneCutSettings(sensitivity=0.75)
        assert s2.sensitivity == 0.75

    def test_min_scene_frames_below_minimum_raises(self):
        with pytest.raises(ValueError, match="min_scene_frames must be >= 2"):
            SceneCutSettings(min_scene_frames=1)

    def test_min_scene_frames_at_minimum_ok(self):
        s = SceneCutSettings(min_scene_frames=2)
        assert s.min_scene_frames == 2

    def test_sample_width_below_minimum_raises(self):
        with pytest.raises(ValueError, match="sample_width must be between"):
            SceneCutSettings(sample_width=63)

    def test_sample_width_above_maximum_raises(self):
        with pytest.raises(ValueError, match="sample_width must be between"):
            SceneCutSettings(sample_width=513)

    def test_max_sample_height_below_minimum_raises(self):
        with pytest.raises(ValueError, match="max_sample_height must be between"):
            SceneCutSettings(max_sample_height=35)

    def test_max_sample_height_above_maximum_raises(self):
        with pytest.raises(ValueError, match="max_sample_height must be between"):
            SceneCutSettings(max_sample_height=289)

    def test_start_source_frame_negative_raises(self):
        with pytest.raises(ValueError, match="start_source_frame must be >= 0"):
            SceneCutSettings(start_source_frame=-1)

    def test_end_source_frame_not_greater_than_start_raises(self):
        with pytest.raises(ValueError, match="end_source_frame .* must be > start_source_frame"):
            SceneCutSettings(start_source_frame=100, end_source_frame=100)

    def test_end_source_frame_less_than_start_raises(self):
        with pytest.raises(ValueError, match="end_source_frame .* must be > start_source_frame"):
            SceneCutSettings(start_source_frame=100, end_source_frame=50)


class TestSceneCutSettingsMethods:
    """SceneCutSettings helper methods."""

    def test_to_dict_contains_all_fields(self):
        settings = SceneCutSettings(
            sensitivity=0.45,
            min_scene_frames=12,
            sample_width=320,
            max_sample_height=180,
            start_source_frame=100,
            end_source_frame=500,
        )
        d = settings.to_dict()
        assert d["sensitivity"] == 0.45
        assert d["min_scene_frames"] == 12
        assert d["sample_width"] == 320
        assert d["max_sample_height"] == 180
        assert d["start_source_frame"] == 100
        assert d["end_source_frame"] == 500

    def test_from_dict_recreates_instance(self):
        data = {
            "sensitivity": 0.65,
            "min_scene_frames": 15,
            "sample_width": 128,
            "max_sample_height": 72,
            "use_proxy": False,
            "detect_transitions": False,
        }
        settings = SceneCutSettings.from_dict(data)
        assert settings.sensitivity == 0.65
        assert settings.min_scene_frames == 15
        assert settings.sample_width == 128
        assert settings.max_sample_height == 72
        assert settings.use_proxy is False
        assert settings.detect_transitions is False

    def test_from_dict_with_missing_fields_uses_defaults(self):
        data = {"sensitivity": 0.60}
        settings = SceneCutSettings.from_dict(data)
        assert settings.sensitivity == 0.60
        assert settings.min_scene_frames == 8  # default
        assert settings.sample_width == 256  # default

    def test_to_hash_produces_stable_output(self):
        settings = SceneCutSettings(sensitivity=0.55, min_scene_frames=8)
        hash1 = settings.to_hash()
        hash2 = settings.to_hash()
        assert hash1 == hash2
        assert len(hash1) == 16

    def test_to_hash_differs_for_different_settings(self):
        s1 = SceneCutSettings(sensitivity=0.55)
        s2 = SceneCutSettings(sensitivity=0.65)
        assert s1.to_hash() != s2.to_hash()

    def test_with_sensitivity_creates_new_instance(self):
        original = SceneCutSettings(sensitivity=0.55, min_scene_frames=12)
        modified = original.with_sensitivity(0.65)
        assert original.sensitivity == 0.55  # unchanged
        assert modified.sensitivity == 0.65
        assert modified.min_scene_frames == 12  # preserved

    def test_with_range_creates_new_instance(self):
        original = SceneCutSettings(sensitivity=0.55)
        modified = original.with_range(100, 500)
        assert original.start_source_frame == 0
        assert original.end_source_frame is None
        assert modified.start_source_frame == 100
        assert modified.end_source_frame == 500
        assert modified.sensitivity == 0.55  # preserved


# ===================================================================
# Test: SceneCutCandidate Decision States
# ===================================================================

class TestSceneCutCandidateDecisionStates:
    """Decision state judgment properties of SceneCutCandidate."""

    def _make_candidate(
        self,
        decision=SceneCutDecision.PENDING,
        confidence=0.75,
        kind=SceneCutKind.HARD_CUT,
        reasons=(),
    ):
        return SceneCutCandidate(
            source_frame=100,
            pts=1000,
            seconds=4.166,
            score=0.85,
            confidence=confidence,
            kind=kind,
            decision=decision,
            reasons=reasons,
        )

    def test_is_pending_true_for_pending(self):
        c = self._make_candidate(decision=SceneCutDecision.PENDING)
        assert c.is_pending is True
        assert c.is_accepted is False
        assert c.is_rejected is False

    def test_is_accepted_true_for_accepted(self):
        c = self._make_candidate(decision=SceneCutDecision.ACCEPTED)
        assert c.is_accepted is True
        assert c.is_pending is False
        assert c.is_rejected is False

    def test_is_rejected_true_for_rejected(self):
        c = self._make_candidate(decision=SceneCutDecision.REJECTED)
        assert c.is_rejected is True
        assert c.is_pending is False
        assert c.is_accepted is False

    def test_is_high_confidence_at_threshold(self):
        c = self._make_candidate(confidence=0.85)
        assert c.is_high_confidence is True

    def test_is_high_confidence_above_threshold(self):
        c = self._make_candidate(confidence=0.90)
        assert c.is_high_confidence is True

    def test_is_high_confidence_below_threshold(self):
        c = self._make_candidate(confidence=0.84)
        assert c.is_high_confidence is False

    def test_is_low_confidence_at_threshold(self):
        c = self._make_candidate(confidence=0.55)
        assert c.is_low_confidence is False  # 0.55 is NOT low (< 0.55)

    def test_is_low_confidence_below_threshold(self):
        c = self._make_candidate(confidence=0.54)
        assert c.is_low_confidence is True

    def test_is_warning_for_low_confidence(self):
        c = self._make_candidate(confidence=0.40)
        assert c.is_warning is True

    def test_is_warning_for_flash_kind(self):
        c = self._make_candidate(kind=SceneCutKind.FLASH)
        assert c.is_warning is True

    def test_is_warning_for_fast_motion_kind(self):
        c = self._make_candidate(kind=SceneCutKind.FAST_MOTION)
        assert c.is_warning is True

    def test_is_warning_for_soft_transition_kind(self):
        c = self._make_candidate(kind=SceneCutKind.SOFT_TRANSITION)
        assert c.is_warning is True

    def test_is_warning_for_black_gap_kind(self):
        c = self._make_candidate(kind=SceneCutKind.BLACK_GAP)
        assert c.is_warning is True

    def test_is_warning_for_warning_reasons(self):
        c = self._make_candidate(reasons=("flash_like", "low_confidence"))
        assert c.is_warning is True

    def test_is_warning_false_for_hard_cut_high_confidence(self):
        c = self._make_candidate(
            kind=SceneCutKind.HARD_CUT,
            confidence=0.90,
            decision=SceneCutDecision.PENDING,
        )
        assert c.is_warning is False

    def test_is_automatically_acceptable_for_hard_cut_high_confidence(self):
        c = self._make_candidate(
            kind=SceneCutKind.HARD_CUT,
            confidence=0.85,
        )
        assert c.is_automatically_acceptable() is True

    def test_is_automatically_acceptable_false_for_soft_transition(self):
        c = self._make_candidate(
            kind=SceneCutKind.SOFT_TRANSITION,
            confidence=0.90,
        )
        assert c.is_automatically_acceptable() is False

    def test_is_automatically_acceptable_false_for_low_confidence(self):
        c = self._make_candidate(
            kind=SceneCutKind.HARD_CUT,
            confidence=0.70,
        )
        assert c.is_automatically_acceptable() is False

    def test_is_automatically_acceptable_false_for_warning_reason(self):
        c = self._make_candidate(
            kind=SceneCutKind.HARD_CUT,
            confidence=0.90,
            reasons=("vfr_time_uncertain",),
        )
        assert c.is_automatically_acceptable() is False


class TestSceneCutCandidateValidation:
    """Validation of SceneCutCandidate parameters."""

    def test_negative_source_frame_raises(self):
        with pytest.raises(ValueError, match="source_frame must be >= 0"):
            SceneCutCandidate(
                source_frame=-1,
                pts=0,
                seconds=0.0,
            )

    def test_negative_seconds_raises(self):
        with pytest.raises(ValueError, match="seconds must be >= 0.0"):
            SceneCutCandidate(
                source_frame=0,
                pts=0,
                seconds=-1.0,
            )

    def test_score_out_of_range_raises(self):
        with pytest.raises(ValueError, match="score must be in"):
            SceneCutCandidate(
                source_frame=100,
                pts=1000,
                seconds=4.0,
                score=1.5,
            )

    def test_confidence_out_of_range_raises(self):
        with pytest.raises(ValueError, match="confidence must be in"):
            SceneCutCandidate(
                source_frame=100,
                pts=1000,
                seconds=4.0,
                confidence=-0.1,
            )


class TestSceneCutCandidateFactoryMethods:
    """Factory methods for SceneCutCandidate."""

    def test_with_decision_creates_new_instance(self):
        original = SceneCutCandidate(
            source_frame=100,
            pts=1000,
            seconds=4.0,
            decision=SceneCutDecision.PENDING,
            confidence=0.75,
        )
        modified = original.with_decision(SceneCutDecision.ACCEPTED)
        assert original.decision == SceneCutDecision.PENDING
        assert modified.decision == SceneCutDecision.ACCEPTED
        assert modified.source_frame == 100

    def test_with_timeline_frame_creates_new_instance(self):
        original = SceneCutCandidate(
            source_frame=100,
            pts=1000,
            seconds=4.0,
            timeline_frame=None,
        )
        modified = original.with_timeline_frame(150)
        assert original.timeline_frame is None
        assert modified.timeline_frame == 150

    def test_with_reasons_creates_new_instance(self):
        original = SceneCutCandidate(
            source_frame=100,
            pts=1000,
            seconds=4.0,
            reasons=(),
        )
        modified = original.with_reasons(("high_score", "flash_like"))
        assert original.reasons == ()
        assert modified.reasons == ("high_score", "flash_like")


class TestSceneCutCandidateSerialization:
    """Serialization of SceneCutCandidate."""

    def test_to_dict_contains_all_fields(self):
        metrics = SceneCutMetrics(
            luma_delta=0.85,
            hist_delta=0.72,
            edge_delta=0.65,
            color_delta=0.55,
            black_score=0.10,
            flash_score=0.05,
            motion_score=0.30,
            combined_score=0.68,
        )
        c = SceneCutCandidate(
            source_frame=100,
            pts=1000,
            seconds=4.166,
            timeline_frame=120,
            score=0.85,
            confidence=0.90,
            kind=SceneCutKind.HARD_CUT,
            decision=SceneCutDecision.ACCEPTED,
            metrics=metrics,
            reasons=("high_score",),
        )
        d = c.to_dict()
        assert d["source_frame"] == 100
        assert d["pts"] == 1000
        assert d["seconds"] == 4.166
        assert d["timeline_frame"] == 120
        assert d["score"] == 0.85
        assert d["confidence"] == 0.90
        assert d["kind"] == "hard_cut"
        assert d["decision"] == "accepted"
        assert d["metrics"]["luma_delta"] == 0.85
        assert d["reasons"] == ["high_score"]

    def test_to_dict_without_metrics(self):
        c = SceneCutCandidate(
            source_frame=100,
            pts=1000,
            seconds=4.0,
            metrics=None,
        )
        d = c.to_dict()
        assert d["metrics"] is None

    def test_from_dict_recreates_instance(self):
        data = {
            "source_frame": 200,
            "pts": 2000,
            "seconds": 8.333,
            "timeline_frame": 240,
            "score": 0.75,
            "confidence": 0.80,
            "kind": "soft_transition",
            "decision": "pending",
            "metrics": {
                "luma_delta": 0.60,
                "hist_delta": 0.50,
                "edge_delta": 0.40,
                "color_delta": 0.30,
                "black_score": 0.05,
                "flash_score": 0.02,
                "motion_score": 0.70,
                "combined_score": 0.45,
            },
            "reasons": ["transition_like"],
        }
        c = SceneCutCandidate.from_dict(data)
        assert c.source_frame == 200
        assert c.pts == 2000
        assert c.seconds == 8.333
        assert c.timeline_frame == 240
        assert c.score == 0.75
        assert c.confidence == 0.80
        assert c.kind == SceneCutKind.SOFT_TRANSITION
        assert c.decision == SceneCutDecision.PENDING
        assert c.metrics is not None
        assert c.metrics.luma_delta == 0.60
        assert c.reasons == ("transition_like",)

    def test_from_dict_without_metrics(self):
        data = {
            "source_frame": 100,
            "seconds": 4.0,
            "kind": "hard_cut",
            "decision": "accepted",
        }
        c = SceneCutCandidate.from_dict(data)
        assert c.metrics is None


# ===================================================================
# Test: SceneCutAnalysis Serialization
# ===================================================================

class TestSceneCutAnalysisSerialization:
    """Serialization of SceneCutAnalysis."""

    def _make_analysis(self):
        settings = SceneCutSettings(sensitivity=0.55)
        time_points = (
            FrameTimePoint(source_frame=0, pts=0, seconds=0.0),
            FrameTimePoint(source_frame=100, pts=1000, seconds=4.166),
        )
        time_index = SourceFrameTimeIndex(
            asset_id="asset-001",
            media_fingerprint="fp-001",
            points=time_points,
        )
        candidates = (
            SceneCutCandidate(
                source_frame=100,
                pts=1000,
                seconds=4.166,
                score=0.85,
                confidence=0.90,
                kind=SceneCutKind.HARD_CUT,
                decision=SceneCutDecision.PENDING,
            ),
        )
        return SceneCutAnalysis(
            analysis_id="analysis-001",
            asset_id="asset-001",
            source_path="/path/to/media.mp4",
            clip_id="clip-001",
            settings=settings,
            media_fingerprint="fp-001",
            source_time_index=time_index,
            candidates=candidates,
            created_at=1700000000.0,
            detector_version=DETECTOR_VERSION,
        )

    def test_to_dict_contains_all_fields(self):
        analysis = self._make_analysis()
        d = analysis.to_dict()
        assert d["analysis_id"] == "analysis-001"
        assert d["asset_id"] == "asset-001"
        assert d["source_path"] == "/path/to/media.mp4"
        assert d["clip_id"] == "clip-001"
        assert d["settings"]["sensitivity"] == 0.55
        assert d["media_fingerprint"] == "fp-001"
        assert d["source_time_index"]["asset_id"] == "asset-001"
        assert len(d["candidates"]) == 1
        assert d["created_at"] == 1700000000.0
        assert d["detector_version"] == DETECTOR_VERSION

    def test_from_dict_recreates_instance(self):
        data = {
            "analysis_id": "analysis-002",
            "asset_id": "asset-002",
            "source_path": "/path/to/video.mp4",
            "clip_id": None,
            "settings": {"sensitivity": 0.65},
            "media_fingerprint": "fp-002",
            "source_time_index": {
                "asset_id": "asset-002",
                "media_fingerprint": "fp-002",
                "points": [
                    {"source_frame": 0, "pts": 0, "seconds": 0.0},
                ],
                "average_fps": 24.0,
            },
            "candidates": [
                {
                    "source_frame": 50,
                    "pts": 500,
                    "seconds": 2.083,
                    "score": 0.70,
                    "confidence": 0.75,
                    "kind": "hard_cut",
                    "decision": "accepted",
                },
            ],
            "created_at": 1700000100.0,
            "detector_version": "1.0.0",
        }
        analysis = SceneCutAnalysis.from_dict(data)
        assert analysis.analysis_id == "analysis-002"
        assert analysis.asset_id == "asset-002"
        assert analysis.source_path == "/path/to/video.mp4"
        assert analysis.clip_id is None
        assert analysis.settings.sensitivity == 0.65
        assert analysis.source_time_index is not None
        assert analysis.source_time_index.asset_id == "asset-002"
        assert len(analysis.candidates) == 1
        assert analysis.candidates[0].source_frame == 50
        assert analysis.candidates[0].decision == SceneCutDecision.ACCEPTED

    def test_from_dict_without_time_index(self):
        data = {
            "analysis_id": "analysis-003",
            "asset_id": "asset-003",
            "source_path": "/path/to/media.mp4",
            "settings": {},
            "source_time_index": None,
            "candidates": [],
        }
        analysis = SceneCutAnalysis.from_dict(data)
        assert analysis.source_time_index is None
        assert analysis.candidates == ()

    def test_roundtrip_preserves_data(self):
        original = self._make_analysis()
        d = original.to_dict()
        restored = SceneCutAnalysis.from_dict(d)
        assert restored.analysis_id == original.analysis_id
        assert restored.asset_id == original.asset_id
        assert restored.settings.sensitivity == original.settings.sensitivity
        assert len(restored.candidates) == len(original.candidates)


class TestSceneCutAnalysisCounts:
    """Computed count properties of SceneCutAnalysis."""

    def test_accepted_count(self):
        candidates = (
            SceneCutCandidate(source_frame=100, pts=1000, seconds=4.0, decision=SceneCutDecision.ACCEPTED),
            SceneCutCandidate(source_frame=200, pts=2000, seconds=8.0, decision=SceneCutDecision.ACCEPTED),
            SceneCutCandidate(source_frame=300, pts=3000, seconds=12.0, decision=SceneCutDecision.PENDING),
        )
        analysis = SceneCutAnalysis(
            analysis_id="test-001",
            asset_id="asset-001",
            source_path="/path/media.mp4",
            candidates=candidates,
        )
        assert analysis.accepted_count == 2

    def test_rejected_count(self):
        candidates = (
            SceneCutCandidate(source_frame=100, pts=1000, seconds=4.0, decision=SceneCutDecision.REJECTED),
            SceneCutCandidate(source_frame=200, pts=2000, seconds=8.0, decision=SceneCutDecision.PENDING),
        )
        analysis = SceneCutAnalysis(
            analysis_id="test-001",
            asset_id="asset-001",
            source_path="/path/media.mp4",
            candidates=candidates,
        )
        assert analysis.rejected_count == 1

    def test_pending_count(self):
        candidates = (
            SceneCutCandidate(source_frame=100, pts=1000, seconds=4.0, decision=SceneCutDecision.PENDING),
            SceneCutCandidate(source_frame=200, pts=2000, seconds=8.0, decision=SceneCutDecision.PENDING),
            SceneCutCandidate(source_frame=300, pts=3000, seconds=12.0, decision=SceneCutDecision.ACCEPTED),
        )
        analysis = SceneCutAnalysis(
            analysis_id="test-001",
            asset_id="asset-001",
            source_path="/path/media.mp4",
            candidates=candidates,
        )
        assert analysis.pending_count == 2


class TestSceneCutAnalysisUpdateMethods:
    """Update methods of SceneCutAnalysis."""

    def test_with_updated_candidate(self):
        candidates = (
            SceneCutCandidate(source_frame=100, pts=1000, seconds=4.0, decision=SceneCutDecision.PENDING),
            SceneCutCandidate(source_frame=200, pts=2000, seconds=8.0, decision=SceneCutDecision.PENDING),
        )
        original = SceneCutAnalysis(
            analysis_id="test-001",
            asset_id="asset-001",
            source_path="/path/media.mp4",
            candidates=candidates,
        )
        modified = original.with_updated_candidate(100, SceneCutDecision.ACCEPTED)
        assert original.candidates[0].decision == SceneCutDecision.PENDING
        assert modified.candidates[0].decision == SceneCutDecision.ACCEPTED
        assert modified.candidates[1].decision == SceneCutDecision.PENDING

    def test_with_all_decisions(self):
        candidates = (
            SceneCutCandidate(source_frame=100, pts=1000, seconds=4.0, decision=SceneCutDecision.PENDING),
            SceneCutCandidate(source_frame=200, pts=2000, seconds=8.0, decision=SceneCutDecision.PENDING),
        )
        original = SceneCutAnalysis(
            analysis_id="test-001",
            asset_id="asset-001",
            source_path="/path/media.mp4",
            candidates=candidates,
        )
        decisions = {100: SceneCutDecision.ACCEPTED, 200: SceneCutDecision.REJECTED}
        modified = original.with_all_decisions(decisions)
        assert modified.candidates[0].decision == SceneCutDecision.ACCEPTED
        assert modified.candidates[1].decision == SceneCutDecision.REJECTED

    def test_with_accept_high_confidence(self):
        candidates = (
            SceneCutCandidate(source_frame=100, pts=1000, seconds=4.0, confidence=0.90, decision=SceneCutDecision.PENDING),
            SceneCutCandidate(source_frame=200, pts=2000, seconds=8.0, confidence=0.70, decision=SceneCutDecision.PENDING),
        )
        original = SceneCutAnalysis(
            analysis_id="test-001",
            asset_id="asset-001",
            source_path="/path/media.mp4",
            candidates=candidates,
        )
        modified = original.with_accept_high_confidence(threshold=0.85)
        assert modified.candidates[0].decision == SceneCutDecision.ACCEPTED
        assert modified.candidates[1].decision == SceneCutDecision.PENDING

    def test_with_reject_warnings(self):
        candidates = (
            SceneCutCandidate(source_frame=100, pts=1000, seconds=4.0, kind=SceneCutKind.FLASH, confidence=0.90, decision=SceneCutDecision.PENDING),
            SceneCutCandidate(source_frame=200, pts=2000, seconds=8.0, kind=SceneCutKind.HARD_CUT, confidence=0.90, decision=SceneCutDecision.PENDING),
        )
        original = SceneCutAnalysis(
            analysis_id="test-001",
            asset_id="asset-001",
            source_path="/path/media.mp4",
            candidates=candidates,
        )
        modified = original.with_reject_warnings()
        assert modified.candidates[0].decision == SceneCutDecision.REJECTED
        assert modified.candidates[1].decision == SceneCutDecision.PENDING


class TestSceneCutAnalysisValidation:
    """Validation of SceneCutAnalysis parameters."""

    def test_empty_analysis_id_raises(self):
        with pytest.raises(ValueError, match="analysis_id cannot be empty"):
            SceneCutAnalysis(
                analysis_id="",
                asset_id="asset-001",
                source_path="/path/media.mp4",
            )

    def test_empty_asset_id_raises(self):
        with pytest.raises(ValueError, match="asset_id cannot be empty"):
            SceneCutAnalysis(
                analysis_id="analysis-001",
                asset_id="",
                source_path="/path/media.mp4",
            )

    def test_empty_source_path_raises(self):
        with pytest.raises(ValueError, match="source_path cannot be empty"):
            SceneCutAnalysis(
                analysis_id="analysis-001",
                asset_id="asset-001",
                source_path="",
            )


# ===================================================================
# Test: Timecode Conversion
# ===================================================================

class TestFramesToTimecode:
    """Frame to SMPTE timecode conversion."""

    def test_frame_zero(self):
        tc = _frames_to_timecode(0, 24.0)
        assert tc == "00:00:00:00"

    def test_frame_24_is_one_second(self):
        tc = _frames_to_timecode(24, 24.0)
        assert tc == "00:00:01:00"

    def test_frame_100(self):
        tc = _frames_to_timecode(100, 24.0)
        # 100 frames = 4 seconds + 4 frames
        assert tc == "00:00:04:04"

    def test_frame_1440_is_one_minute(self):
        tc = _frames_to_timecode(1440, 24.0)
        assert tc == "00:01:00:00"

    def test_frame_86400_is_one_hour(self):
        tc = _frames_to_timecode(86400, 24.0)
        assert tc == "01:00:00:00"

    def test_30fps_frame_30_is_one_second(self):
        tc = _frames_to_timecode(30, 30.0)
        assert tc == "00:00:01:00"

    def test_25fps_frame_25_is_one_second(self):
        tc = _frames_to_timecode(25, 25.0)
        assert tc == "00:00:01:00"

    def test_handles_fractional_fps_23_976(self):
        # 23.976 is treated as 24 for display
        tc = _frames_to_timecode(24, 23.976)
        assert tc == "00:00:01:00"

    def test_handles_fractional_fps_29_97(self):
        # 审计修复：委托 interchange 后 29.97 使用 SMPTE drop-frame
        # （旧副本当 30 NDF 显示，1 小时后差 108 帧）
        tc = _frames_to_timecode(30, 29.97)
        assert tc == "00:00:01;00"

    def test_zero_fps_uses_default_24(self):
        tc = _frames_to_timecode(24, 0.0)
        assert tc == "00:00:01:00"

    def test_negative_fps_uses_default_24(self):
        tc = _frames_to_timecode(24, -1.0)
        assert tc == "00:00:01:00"


class TestTimecodeToFrames:
    """SMPTE timecode to frame conversion."""

    def test_zero_timecode(self):
        frames = timecode_to_frames("00:00:00:00", 24.0)
        assert frames == 0

    def test_one_second_timecode(self):
        frames = timecode_to_frames("00:00:01:00", 24.0)
        assert frames == 24

    def test_one_minute_timecode(self):
        frames = timecode_to_frames("00:01:00:00", 24.0)
        assert frames == 1440

    def test_one_hour_timecode(self):
        frames = timecode_to_frames("01:00:00:00", 24.0)
        assert frames == 86400

    def test_complex_timecode(self):
        frames = timecode_to_frames("01:23:45:12", 24.0)
        # 1*24*3600 + 23*24*60 + 45*24 + 12 = 86400 + 33120 + 1080 + 12
        assert frames == 86400 + 33120 + 1080 + 12

    def test_30fps_one_second(self):
        frames = timecode_to_frames("00:00:01:00", 30.0)
        assert frames == 30

    def test_25fps_one_second(self):
        frames = timecode_to_frames("00:00:01:00", 25.0)
        assert frames == 25

    def test_invalid_format_raises(self):
        # AUD-18012：委托 interchange 后消息措辞变化（语义不变：ValueError）
        with pytest.raises(ValueError, match="Invalid timecode"):
            timecode_to_frames("00:00:01", 24.0)

    def test_invalid_format_too_many_parts_raises(self):
        # AUD-18012：委托 interchange 后消息措辞变化（语义不变：ValueError）
        with pytest.raises(ValueError, match="Invalid timecode"):
            timecode_to_frames("00:00:01:00:00", 24.0)

    def test_roundtrip_preserves_frames(self):
        original_frames = 100
        tc = _frames_to_timecode(original_frames, 24.0)
        restored_frames = timecode_to_frames(tc, 24.0)
        assert restored_frames == original_frames


# ===================================================================
# Test: SourceFrameTimeIndex Mapping
# ===================================================================

class TestSourceFrameTimeIndexMapping:
    """Frame-to-time mapping functionality."""

    def _make_index(self):
        points = (
            FrameTimePoint(source_frame=0, pts=0, seconds=0.0),
            FrameTimePoint(source_frame=24, pts=1001, seconds=1.0),
            FrameTimePoint(source_frame=48, pts=2002, seconds=2.0),
            FrameTimePoint(source_frame=72, pts=3003, seconds=3.0),
        )
        return SourceFrameTimeIndex(
            asset_id="asset-001",
            media_fingerprint="fp-001",
            points=points,
            average_fps=24.0,
        )

    def test_source_frame_to_seconds_exact_match(self):
        index = self._make_index()
        assert index.source_frame_to_seconds(0) == 0.0
        assert index.source_frame_to_seconds(24) == 1.0
        assert index.source_frame_to_seconds(48) == 2.0

    def test_source_frame_to_seconds_interpolation(self):
        index = self._make_index()
        # Frame 12 is between 0 and 24
        seconds = index.source_frame_to_seconds(12)
        assert seconds == 0.5  # Halfway between 0.0 and 1.0

    def test_source_frame_to_seconds_extrapolation_after_last(self):
        index = self._make_index()
        # Frame 100 is beyond last known point (72)
        seconds = index.source_frame_to_seconds(100)
        # 3.0 + (100-72)/24.0 = 3.0 + 28/24 = 4.166...
        assert seconds == 3.0 + 28.0 / 24.0

    def test_source_frame_to_seconds_extrapolation_before_first(self):
        index = self._make_index()
        # This would require points starting at a frame > 0
        points = (
            FrameTimePoint(source_frame=10, pts=0, seconds=0.416),
            FrameTimePoint(source_frame=24, pts=1001, seconds=1.0),
        )
        index2 = SourceFrameTimeIndex(
            asset_id="asset-002",
            media_fingerprint="fp-002",
            points=points,
            average_fps=24.0,
        )
        seconds = index2.source_frame_to_seconds(5)
        # Extrapolate before first point
        assert seconds == 0.416 - (10 - 5) / 24.0

    def test_source_frame_to_seconds_empty_index_with_fps(self):
        index = SourceFrameTimeIndex(
            asset_id="asset-003",
            media_fingerprint="fp-003",
            points=(),
            average_fps=24.0,
        )
        seconds = index.source_frame_to_seconds(48)
        assert seconds == 2.0  # 48/24

    def test_source_frame_to_seconds_empty_index_no_fps_raises(self):
        index = SourceFrameTimeIndex(
            asset_id="asset-004",
            media_fingerprint="fp-004",
            points=(),
            average_fps=None,
        )
        with pytest.raises(ValueError, match="No time points available"):
            index.source_frame_to_seconds(48)

    def test_seconds_to_nearest_source_frame_exact_match(self):
        index = self._make_index()
        assert index.seconds_to_nearest_source_frame(0.0) == 0
        assert index.seconds_to_nearest_source_frame(1.0) == 24
        assert index.seconds_to_nearest_source_frame(2.0) == 48

    def test_seconds_to_nearest_source_frame_interpolation(self):
        index = self._make_index()
        # 0.5 seconds is closest to frame 12 (0.5s) or 24 (1.0s)
        # Actually 12 frames would be 0.5s, but 12 isn't in the index
        # So nearest is either 0 (0.0s) or 24 (1.0s)
        frame = index.seconds_to_nearest_source_frame(0.5)
        # 0.5 is equally distant from 0.0 and 1.0, algorithm picks closest
        assert frame == 0 or frame == 24

    def test_seconds_to_nearest_source_frame_empty_index_with_fps(self):
        index = SourceFrameTimeIndex(
            asset_id="asset-005",
            media_fingerprint="fp-005",
            points=(),
            average_fps=24.0,
        )
        frame = index.seconds_to_nearest_source_frame(2.0)
        assert frame == 48  # 2.0 * 24

    def test_seconds_to_nearest_source_frame_empty_index_no_fps_raises(self):
        index = SourceFrameTimeIndex(
            asset_id="asset-006",
            media_fingerprint="fp-006",
            points=(),
            average_fps=None,
        )
        with pytest.raises(ValueError, match="No time points available"):
            index.seconds_to_nearest_source_frame(2.0)

    def test_get_point_for_frame_found(self):
        index = self._make_index()
        point = index.get_point_for_frame(24)
        assert point is not None
        assert point.source_frame == 24
        assert point.seconds == 1.0

    def test_get_point_for_frame_not_found(self):
        index = self._make_index()
        point = index.get_point_for_frame(12)
        assert point is None

    def test_roundtrip_preserves_frame(self):
        index = self._make_index()
        original_frame = 48
        seconds = index.source_frame_to_seconds(original_frame)
        restored_frame = index.seconds_to_nearest_source_frame(seconds)
        assert restored_frame == original_frame


class TestSourceFrameTimeIndexValidation:
    """Validation of SourceFrameTimeIndex."""

    def test_empty_asset_id_raises(self):
        with pytest.raises(ValueError, match="asset_id cannot be empty"):
            SourceFrameTimeIndex(
                asset_id="",
                media_fingerprint="fp",
                points=(),
            )

    def test_negative_average_fps_raises(self):
        with pytest.raises(ValueError, match="average_fps must be > 0.0"):
            SourceFrameTimeIndex(
                asset_id="asset-001",
                media_fingerprint="fp",
                points=(),
                average_fps=-24.0,
            )

    def test_zero_average_fps_raises(self):
        with pytest.raises(ValueError, match="average_fps must be > 0.0"):
            SourceFrameTimeIndex(
                asset_id="asset-001",
                media_fingerprint="fp",
                points=(),
                average_fps=0.0,
            )


class TestSourceFrameTimeIndexSerialization:
    """Serialization of SourceFrameTimeIndex."""

    def test_to_dict(self):
        points = (
            FrameTimePoint(source_frame=0, pts=0, seconds=0.0, is_keyframe=True),
            FrameTimePoint(source_frame=24, pts=1001, seconds=1.0),
        )
        index = SourceFrameTimeIndex(
            asset_id="asset-001",
            media_fingerprint="fp-001",
            points=points,
            average_fps=24.0,
        )
        d = index.to_dict()
        assert d["asset_id"] == "asset-001"
        assert d["media_fingerprint"] == "fp-001"
        assert len(d["points"]) == 2
        assert d["points"][0]["source_frame"] == 0
        assert d["points"][0]["is_keyframe"] is True
        assert d["average_fps"] == 24.0

    def test_from_dict(self):
        data = {
            "asset_id": "asset-002",
            "media_fingerprint": "fp-002",
            "points": [
                {"source_frame": 0, "pts": 0, "seconds": 0.0},
                {"source_frame": 30, "pts": 1000, "seconds": 1.0},
            ],
            "average_fps": 30.0,
        }
        index = SourceFrameTimeIndex.from_dict(data)
        assert index.asset_id == "asset-002"
        assert index.media_fingerprint == "fp-002"
        assert len(index.points) == 2
        assert index.average_fps == 30.0

    def test_roundtrip_preserves_data(self):
        points = (
            FrameTimePoint(source_frame=0, pts=0, seconds=0.0),
            FrameTimePoint(source_frame=24, pts=1001, seconds=1.0),
        )
        original = SourceFrameTimeIndex(
            asset_id="asset-001",
            media_fingerprint="fp-001",
            points=points,
            average_fps=24.0,
        )
        d = original.to_dict()
        restored = SourceFrameTimeIndex.from_dict(d)
        assert restored.asset_id == original.asset_id
        assert len(restored.points) == len(original.points)


# ===================================================================
# Test: FrameTimePoint
# ===================================================================

class TestFrameTimePointValidation:
    """Validation of FrameTimePoint."""

    def test_negative_source_frame_raises(self):
        with pytest.raises(ValueError, match="source_frame must be >= 0"):
            FrameTimePoint(source_frame=-1, pts=0, seconds=0.0)

    def test_negative_seconds_raises(self):
        with pytest.raises(ValueError, match="seconds must be >= 0.0"):
            FrameTimePoint(source_frame=0, pts=0, seconds=-1.0)


class TestFrameTimePointSerialization:
    """Serialization of FrameTimePoint."""

    def test_to_dict(self):
        point = FrameTimePoint(
            source_frame=100,
            pts=1000,
            seconds=4.166,
            duration_seconds=0.0416,
            is_keyframe=True,
            is_duplicate_time=False,
        )
        d = point.to_dict()
        assert d["source_frame"] == 100
        assert d["pts"] == 1000
        assert d["seconds"] == 4.166
        assert d["duration_seconds"] == 0.0416
        assert d["is_keyframe"] is True
        assert d["is_duplicate_time"] is False

    def test_from_dict(self):
        data = {
            "source_frame": 200,
            "pts": 2000,
            "seconds": 8.333,
            "duration_seconds": None,
            "is_keyframe": False,
        }
        point = FrameTimePoint.from_dict(data)
        assert point.source_frame == 200
        assert point.pts == 2000
        assert point.seconds == 8.333
        assert point.duration_seconds is None
        assert point.is_keyframe is False


# ===================================================================
# Test: SceneSubclipSpec Generation
# ===================================================================

class TestSceneSubclipSpecValidation:
    """Validation of SceneSubclipSpec."""

    def test_empty_parent_asset_id_raises(self):
        with pytest.raises(ValueError, match="parent_asset_id cannot be empty"):
            SceneSubclipSpec(
                parent_asset_id="",
                source_path="/path/media.mp4",
                scene_index=1,
                source_start=0,
                source_duration=100,
                start_seconds=0.0,
                duration_seconds=4.0,
                display_name="Scene 001",
                start_timecode="00:00:00:00",
                end_timecode="00:00:04:00",
                thumbnail_source_frame=0,
                detection_confidence=0.9,
                detection_kind=SceneCutKind.HARD_CUT,
            )

    def test_zero_scene_index_raises(self):
        with pytest.raises(ValueError, match="scene_index must be >= 1"):
            SceneSubclipSpec(
                parent_asset_id="asset-001",
                source_path="/path/media.mp4",
                scene_index=0,
                source_start=0,
                source_duration=100,
                start_seconds=0.0,
                duration_seconds=4.0,
                display_name="Scene 001",
                start_timecode="00:00:00:00",
                end_timecode="00:00:04:00",
                thumbnail_source_frame=0,
                detection_confidence=0.9,
                detection_kind=SceneCutKind.HARD_CUT,
            )

    def test_negative_source_start_raises(self):
        with pytest.raises(ValueError, match="source_start must be >= 0"):
            SceneSubclipSpec(
                parent_asset_id="asset-001",
                source_path="/path/media.mp4",
                scene_index=1,
                source_start=-1,
                source_duration=100,
                start_seconds=0.0,
                duration_seconds=4.0,
                display_name="Scene 001",
                start_timecode="00:00:00:00",
                end_timecode="00:00:04:00",
                thumbnail_source_frame=0,
                detection_confidence=0.9,
                detection_kind=SceneCutKind.HARD_CUT,
            )

    def test_zero_source_duration_raises(self):
        with pytest.raises(ValueError, match="source_duration must be >= 1"):
            SceneSubclipSpec(
                parent_asset_id="asset-001",
                source_path="/path/media.mp4",
                scene_index=1,
                source_start=0,
                source_duration=0,
                start_seconds=0.0,
                duration_seconds=4.0,
                display_name="Scene 001",
                start_timecode="00:00:00:00",
                end_timecode="00:00:04:00",
                thumbnail_source_frame=0,
                detection_confidence=0.9,
                detection_kind=SceneCutKind.HARD_CUT,
            )

    def test_confidence_out_of_range_raises(self):
        with pytest.raises(ValueError, match="detection_confidence must be in"):
            SceneSubclipSpec(
                parent_asset_id="asset-001",
                source_path="/path/media.mp4",
                scene_index=1,
                source_start=0,
                source_duration=100,
                start_seconds=0.0,
                duration_seconds=4.0,
                display_name="Scene 001",
                start_timecode="00:00:00:00",
                end_timecode="00:00:04:00",
                thumbnail_source_frame=0,
                detection_confidence=1.5,
                detection_kind=SceneCutKind.HARD_CUT,
            )


class TestSceneSubclipSpecProperties:
    """Computed properties of SceneSubclipSpec."""

    def test_source_end(self):
        spec = SceneSubclipSpec(
            parent_asset_id="asset-001",
            source_path="/path/media.mp4",
            scene_index=1,
            source_start=100,
            source_duration=50,
            start_seconds=4.0,
            duration_seconds=2.0,
            display_name="Scene 001",
            start_timecode="00:00:04:04",
            end_timecode="00:00:06:04",
            thumbnail_source_frame=100,
            detection_confidence=0.9,
            detection_kind=SceneCutKind.HARD_CUT,
        )
        assert spec.source_end == 150

    def test_end_seconds(self):
        spec = SceneSubclipSpec(
            parent_asset_id="asset-001",
            source_path="/path/media.mp4",
            scene_index=1,
            source_start=0,
            source_duration=24,
            start_seconds=0.0,
            duration_seconds=1.0,
            display_name="Scene 001",
            start_timecode="00:00:00:00",
            end_timecode="00:00:01:00",
            thumbnail_source_frame=0,
            detection_confidence=0.9,
            detection_kind=SceneCutKind.HARD_CUT,
        )
        assert spec.end_seconds == 1.0


class TestSceneSubclipSpecSerialization:
    """Serialization of SceneSubclipSpec."""

    def test_to_dict(self):
        spec = SceneSubclipSpec(
            parent_asset_id="asset-001",
            source_path="/path/media.mp4",
            scene_index=1,
            source_start=0,
            source_duration=100,
            start_seconds=0.0,
            duration_seconds=4.0,
            display_name="Scene 001",
            start_timecode="00:00:00:00",
            end_timecode="00:00:04:00",
            thumbnail_source_frame=0,
            detection_confidence=0.9,
            detection_kind=SceneCutKind.HARD_CUT,
        )
        d = spec.to_dict()
        assert d["parent_asset_id"] == "asset-001"
        assert d["scene_index"] == 1
        assert d["source_start"] == 0
        assert d["source_duration"] == 100
        assert d["detection_kind"] == "hard_cut"

    def test_from_dict(self):
        data = {
            "parent_asset_id": "asset-002",
            "source_path": "/path/video.mp4",
            "scene_index": 2,
            "source_start": 100,
            "source_duration": 50,
            "start_seconds": 4.0,
            "duration_seconds": 2.0,
            "display_name": "Scene 002",
            "start_timecode": "00:00:04:04",
            "end_timecode": "00:00:06:04",
            "thumbnail_source_frame": 100,
            "detection_confidence": 0.85,
            "detection_kind": "soft_transition",
        }
        spec = SceneSubclipSpec.from_dict(data)
        assert spec.parent_asset_id == "asset-002"
        assert spec.scene_index == 2
        assert spec.detection_kind == SceneCutKind.SOFT_TRANSITION

    def test_to_metadata(self):
        spec = SceneSubclipSpec(
            parent_asset_id="asset-001",
            source_path="/path/media.mp4",
            scene_index=1,
            source_start=0,
            source_duration=100,
            start_seconds=0.0,
            duration_seconds=4.0,
            display_name="Scene 001",
            start_timecode="00:00:00:00",
            end_timecode="00:00:04:00",
            thumbnail_source_frame=0,
            detection_confidence=0.9,
            detection_kind=SceneCutKind.HARD_CUT,
        )
        metadata = spec.to_metadata("analysis-001")
        assert metadata["is_subclip"] is True
        assert metadata["parent_asset_id"] == "asset-001"
        assert metadata["scene_cut_analysis_id"] == "analysis-001"
        assert metadata["scene_index"] == 1


class TestGenerateSceneSubclips:
    """generate_scene_subclips function."""

    def _make_analysis_with_cuts(self, cut_frames: list[int]):
        """Helper to create analysis with accepted cuts at given frames."""
        candidates = tuple(
            SceneCutCandidate(
                source_frame=f,
                pts=f * 42,  # approx for 24fps
                seconds=f / 24.0,
                score=0.85,
                confidence=0.90,
                kind=SceneCutKind.HARD_CUT,
                decision=SceneCutDecision.ACCEPTED,
            )
            for f in cut_frames
        )
        return SceneCutAnalysis(
            analysis_id="test-analysis",
            asset_id="asset-001",
            source_path="/path/media.mp4",
            settings=SceneCutSettings(start_source_frame=0, end_source_frame=None),
            candidates=candidates,
        )

    def test_no_cuts_single_scene(self):
        analysis = SceneCutAnalysis(
            analysis_id="test-analysis",
            asset_id="asset-001",
            source_path="/path/media.mp4",
            settings=SceneCutSettings(),
            candidates=(),
        )
        specs = generate_scene_subclips(
            analysis,
            total_source_frames=240,
            total_duration_seconds=10.0,
            parent_display_name="MyVideo",
            fps=24.0,
        )
        assert len(specs) == 1
        assert specs[0].scene_index == 1
        assert specs[0].source_start == 0
        assert specs[0].source_duration == 240
        assert specs[0].display_name == "MyVideo Scene 001"

    def test_single_cut_two_scenes(self):
        analysis = self._make_analysis_with_cuts([100])
        specs = generate_scene_subclips(
            analysis,
            total_source_frames=240,
            total_duration_seconds=10.0,
            parent_display_name="MyVideo",
            fps=24.0,
        )
        assert len(specs) == 2
        assert specs[0].scene_index == 1
        assert specs[0].source_start == 0
        assert specs[0].source_duration == 100
        assert specs[1].scene_index == 2
        assert specs[1].source_start == 100
        assert specs[1].source_duration == 140

    def test_multiple_cuts_multiple_scenes(self):
        analysis = self._make_analysis_with_cuts([60, 120, 180])
        specs = generate_scene_subclips(
            analysis,
            total_source_frames=240,
            total_duration_seconds=10.0,
            parent_display_name="MyVideo",
            fps=24.0,
        )
        assert len(specs) == 4
        assert specs[0].source_start == 0
        assert specs[0].source_duration == 60
        assert specs[1].source_start == 60
        assert specs[1].source_duration == 60
        assert specs[2].source_start == 120
        assert specs[2].source_duration == 60
        assert specs[3].source_start == 180
        assert specs[3].source_duration == 60

    def test_scene_display_names_increment(self):
        analysis = self._make_analysis_with_cuts([100, 200])
        specs = generate_scene_subclips(
            analysis,
            total_source_frames=300,
            total_duration_seconds=12.5,
            parent_display_name="TestVideo",
            fps=24.0,
        )
        assert specs[0].display_name == "TestVideo Scene 001"
        assert specs[1].display_name == "TestVideo Scene 002"
        assert specs[2].display_name == "TestVideo Scene 003"

    def test_timecode_generation(self):
        analysis = self._make_analysis_with_cuts([24])
        specs = generate_scene_subclips(
            analysis,
            total_source_frames=48,
            total_duration_seconds=2.0,
            parent_display_name="MyVideo",
            fps=24.0,
        )
        assert len(specs) == 2
        assert specs[0].start_timecode == "00:00:00:00"
        assert specs[0].end_timecode == "00:00:01:00"
        assert specs[1].start_timecode == "00:00:01:00"
        assert specs[1].end_timecode == "00:00:02:00"

    def test_with_custom_frame_range(self):
        analysis = self._make_analysis_with_cuts([150])
        analysis = SceneCutAnalysis(
            analysis_id="test-analysis",
            asset_id="asset-001",
            source_path="/path/media.mp4",
            settings=SceneCutSettings(start_source_frame=100, end_source_frame=300),
            candidates=(
                SceneCutCandidate(
                    source_frame=150,
                    pts=150 * 42,
                    seconds=150 / 24.0,
                    score=0.85,
                    confidence=0.90,
                    kind=SceneCutKind.HARD_CUT,
                    decision=SceneCutDecision.ACCEPTED,
                ),
            ),
        )
        specs = generate_scene_subclips(
            analysis,
            total_source_frames=200,  # frames from 100 to 300
            total_duration_seconds=200 / 24.0,
            parent_display_name="MyVideo",
            fps=24.0,
        )
        # start_source_frame=100, so scenes should start from 100
        assert specs[0].source_start == 100


class TestGenerateAnalysisId:
    """generate_analysis_id function."""

    def test_generates_16_char_id(self):
        id1 = generate_analysis_id("asset-001")
        assert len(id1) == 16

    def test_unique_for_different_assets(self):
        id1 = generate_analysis_id("asset-001")
        id2 = generate_analysis_id("asset-002")
        assert id1 != id2

    def test_unique_for_same_asset_different_time(self):
        id1 = generate_analysis_id("asset-001", timestamp=1000.0)
        id2 = generate_analysis_id("asset-001", timestamp=2000.0)
        assert id1 != id2

    def test_stable_for_same_inputs(self):
        id1 = generate_analysis_id("asset-001", timestamp=1000.0)
        id2 = generate_analysis_id("asset-001", timestamp=1000.0)
        assert id1 == id2


# ===================================================================
# Test: SceneCutMetrics
# ===================================================================

class TestSceneCutMetricsValidation:
    """Validation of SceneCutMetrics."""

    def test_all_values_in_range(self):
        metrics = SceneCutMetrics(
            luma_delta=0.5,
            hist_delta=0.5,
            edge_delta=0.5,
            color_delta=0.5,
            black_score=0.5,
            flash_score=0.5,
            motion_score=0.5,
            combined_score=0.5,
        )
        assert metrics.luma_delta == 0.5

    def test_luma_out_of_range_raises(self):
        with pytest.raises(ValueError, match="luma_delta must be in"):
            SceneCutMetrics(
                luma_delta=1.5,
                hist_delta=0.5,
                edge_delta=0.5,
                color_delta=0.5,
                black_score=0.5,
                flash_score=0.5,
                motion_score=0.5,
                combined_score=0.5,
            )

    def test_combined_score_out_of_range_raises(self):
        with pytest.raises(ValueError, match="combined_score must be in"):
            SceneCutMetrics(
                luma_delta=0.5,
                hist_delta=0.5,
                edge_delta=0.5,
                color_delta=0.5,
                black_score=0.5,
                flash_score=0.5,
                motion_score=0.5,
                combined_score=-0.1,
            )


class TestSceneCutMetricsJudgments:
    """Judgment methods of SceneCutMetrics."""

    def test_is_likely_flash(self):
        metrics = SceneCutMetrics(
            luma_delta=0.5,
            hist_delta=0.5,
            edge_delta=0.5,
            color_delta=0.5,
            black_score=0.0,
            flash_score=0.7,
            motion_score=0.3,
            combined_score=0.5,
        )
        assert metrics.is_likely_flash(threshold=0.6) is True

    def test_is_not_likely_flash(self):
        metrics = SceneCutMetrics(
            luma_delta=0.5,
            hist_delta=0.5,
            edge_delta=0.5,
            color_delta=0.5,
            black_score=0.0,
            flash_score=0.5,
            motion_score=0.3,
            combined_score=0.5,
        )
        assert metrics.is_likely_flash(threshold=0.6) is False

    def test_is_likely_black_gap(self):
        metrics = SceneCutMetrics(
            luma_delta=0.5,
            hist_delta=0.5,
            edge_delta=0.5,
            color_delta=0.5,
            black_score=0.8,
            flash_score=0.0,
            motion_score=0.3,
            combined_score=0.5,
        )
        assert metrics.is_likely_black_gap(threshold=0.7) is True

    def test_is_likely_fast_motion(self):
        metrics = SceneCutMetrics(
            luma_delta=0.3,
            hist_delta=0.3,
            edge_delta=0.3,
            color_delta=0.3,
            black_score=0.0,
            flash_score=0.0,
            motion_score=0.7,
            combined_score=0.35,
        )
        assert metrics.is_likely_fast_motion(threshold=0.5) is True

    def test_is_not_likely_fast_motion_high_combined(self):
        metrics = SceneCutMetrics(
            luma_delta=0.3,
            hist_delta=0.3,
            edge_delta=0.3,
            color_delta=0.3,
            black_score=0.0,
            flash_score=0.0,
            motion_score=0.7,
            combined_score=0.6,
        )
        assert metrics.is_likely_fast_motion(threshold=0.5) is False


class TestSceneCutMetricsSerialization:
    """Serialization of SceneCutMetrics."""

    def test_to_dict(self):
        metrics = SceneCutMetrics(
            luma_delta=0.85,
            hist_delta=0.72,
            edge_delta=0.65,
            color_delta=0.55,
            black_score=0.10,
            flash_score=0.05,
            motion_score=0.30,
            combined_score=0.68,
        )
        d = metrics.to_dict()
        assert d["luma_delta"] == 0.85
        assert d["combined_score"] == 0.68

    def test_from_dict(self):
        data = {
            "luma_delta": 0.60,
            "hist_delta": 0.50,
            "edge_delta": 0.40,
            "color_delta": 0.30,
            "black_score": 0.05,
            "flash_score": 0.02,
            "motion_score": 0.70,
            "combined_score": 0.45,
        }
        metrics = SceneCutMetrics.from_dict(data)
        assert metrics.luma_delta == 0.60
        assert metrics.motion_score == 0.70


# ===================================================================
# Test: SceneCutKind Enum
# ===================================================================

class TestSceneCutKind:
    """SceneCutKind enum values."""

    def test_all_kinds_exist(self):
        assert SceneCutKind.HARD_CUT.value == "hard_cut"
        assert SceneCutKind.SOFT_TRANSITION.value == "soft_transition"
        assert SceneCutKind.BLACK_GAP.value == "black_gap"
        assert SceneCutKind.FLASH.value == "flash"
        assert SceneCutKind.FAST_MOTION.value == "fast_motion"
        assert SceneCutKind.UNKNOWN.value == "unknown"

    def test_from_string(self):
        kind = SceneCutKind("hard_cut")
        assert kind == SceneCutKind.HARD_CUT


# ===================================================================
# Test: SceneCutDecision Enum
# ===================================================================

class TestSceneCutDecision:
    """SceneCutDecision enum values."""

    def test_all_decisions_exist(self):
        assert SceneCutDecision.PENDING.value == "pending"
        assert SceneCutDecision.ACCEPTED.value == "accepted"
        assert SceneCutDecision.REJECTED.value == "rejected"

    def test_from_string(self):
        decision = SceneCutDecision("accepted")
        assert decision == SceneCutDecision.ACCEPTED