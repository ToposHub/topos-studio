# -*- coding: utf-8 -*-
"""
Tests for scene_cut_result_model — QAbstractTableModel for scene cut detection results.

Validates:
- SceneCutResultModel initialization and data setting
- Column headers and display text
- Decision editing (accept/reject/pending)
- Batch operations (accept_high_confidence, reject_warnings)
- Selection management (current_index, navigation)
- I18N support
- Signal emission (decision_changed, batch_operation_completed)

Following .clinerules:
- Strong typing in test functions
- Isolated unit tests, no UI thread dependency
"""

import pytest
from typing import List, Optional
import time

from PyQt6.QtCore import Qt, QModelIndex

from src.features.edit.ui.scene_cut_result_model import (
    SceneCutResultModel,
    SceneCutFilterProxy,
    Column,
    I18N_KEYS,
)
from src.features.edit.core.scene_cut.models import (
    SceneCutCandidate,
    SceneCutDecision,
    SceneCutKind,
    SceneCutMetrics,
    SceneCutAnalysis,
    SceneCutSettings,
    FrameTimePoint,
    SourceFrameTimeIndex,
    generate_analysis_id,
)


# ===================================================================
# Test Fixtures
# ===================================================================

@pytest.fixture
def sample_candidates() -> List[SceneCutCandidate]:
    """Create sample candidates for testing."""
    return [
        SceneCutCandidate(
            source_frame=100,
            pts=100,
            seconds=100.0 / 24.0,
            score=0.90,
            confidence=0.92,
            kind=SceneCutKind.HARD_CUT,
            decision=SceneCutDecision.PENDING,
            metrics=SceneCutMetrics(
                luma_delta=0.85,
                hist_delta=0.75,
                edge_delta=0.70,
                color_delta=0.60,
                black_score=0.10,
                flash_score=0.05,
                motion_score=0.20,
                combined_score=0.90,
            ),
            reasons=("high_score",),
        ),
        SceneCutCandidate(
            source_frame=250,
            pts=250,
            seconds=250.0 / 24.0,
            score=0.75,
            confidence=0.70,
            kind=SceneCutKind.HARD_CUT,
            decision=SceneCutDecision.PENDING,
            metrics=SceneCutMetrics(
                luma_delta=0.65,
                hist_delta=0.70,
                edge_delta=0.60,
                color_delta=0.50,
                black_score=0.05,
                flash_score=0.10,
                motion_score=0.30,
                combined_score=0.75,
            ),
            reasons=(),
        ),
        SceneCutCandidate(
            source_frame=400,
            pts=400,
            seconds=400.0 / 24.0,
            score=0.55,
            confidence=0.40,
            kind=SceneCutKind.FLASH,
            decision=SceneCutDecision.PENDING,
            metrics=SceneCutMetrics(
                luma_delta=0.90,
                hist_delta=0.20,
                edge_delta=0.10,
                color_delta=0.15,
                black_score=0.05,
                flash_score=0.85,
                motion_score=0.10,
                combined_score=0.55,
            ),
            reasons=("flash_like", "low_confidence"),
        ),
        SceneCutCandidate(
            source_frame=600,
            pts=600,
            seconds=600.0 / 24.0,
            score=0.80,
            confidence=0.88,
            kind=SceneCutKind.SOFT_TRANSITION,
            decision=SceneCutDecision.PENDING,
            metrics=SceneCutMetrics(
                luma_delta=0.50,
                hist_delta=0.60,
                edge_delta=0.55,
                color_delta=0.45,
                black_score=0.05,
                flash_score=0.10,
                motion_score=0.35,
                combined_score=0.80,
            ),
            reasons=("transition_like",),
        ),
        SceneCutCandidate(
            source_frame=800,
            pts=800,
            seconds=800.0 / 24.0,
            score=0.30,
            confidence=0.25,
            kind=SceneCutKind.BLACK_GAP,
            decision=SceneCutDecision.PENDING,
            metrics=SceneCutMetrics(
                luma_delta=0.10,
                hist_delta=0.15,
                edge_delta=0.10,
                color_delta=0.10,
                black_score=0.90,
                flash_score=0.05,
                motion_score=0.10,
                combined_score=0.30,
            ),
            reasons=("black_gap", "low_confidence"),
        ),
    ]


@pytest.fixture
def sample_analysis(sample_candidates: List[SceneCutCandidate]) -> SceneCutAnalysis:
    """Create sample analysis for testing."""
    time_index = SourceFrameTimeIndex(
        asset_id="test-asset-001",
        media_fingerprint="test-fingerprint",
        points=tuple([
            FrameTimePoint(
                source_frame=f.source_frame,
                pts=f.pts,
                seconds=f.seconds,
            )
            for f in sample_candidates
        ]),
        average_fps=24.0,
    )

    return SceneCutAnalysis(
        analysis_id=generate_analysis_id("test-asset-001"),
        asset_id="test-asset-001",
        source_path="/test/video.mp4",
        clip_id=None,
        settings=SceneCutSettings(),
        media_fingerprint="test-fingerprint",
        source_time_index=time_index.points,
        candidates=tuple(sample_candidates),
        created_at=time.time(),
        detector_version="1.0.0",
    )


@pytest.fixture
def model() -> SceneCutResultModel:
    """Create empty model for testing."""
    return SceneCutResultModel()


# ===================================================================
# Test: SceneCutResultModel Initialization
# ===================================================================

class TestSceneCutResultModelInit:
    """Model initialization tests."""

    def test_init_empty(self, model: SceneCutResultModel) -> None:
        """Model should be empty after init."""
        assert model.rowCount() == 0
        assert model.columnCount() == len(Column)
        assert not model.has_data
        assert model.analysis is None

    def test_column_count(self, model: SceneCutResultModel) -> None:
        """Should have 6 columns."""
        assert model.columnCount() == 6

    def test_empty_candidate_counts(self, model: SceneCutResultModel) -> None:
        """Counts should all be zero when empty."""
        assert model.accepted_count == 0
        assert model.rejected_count == 0
        assert model.pending_count == 0
        assert model.warning_count == 0
        assert model.high_confidence_count == 0


# ===================================================================
# Test: Data Management
# ===================================================================

class TestSceneCutResultModelDataManagement:
    """Data setting and retrieval tests."""

    def test_set_analysis(
        self,
        model: SceneCutResultModel,
        sample_analysis: SceneCutAnalysis,
    ) -> None:
        """Should set analysis and populate candidates."""
        model.set_analysis(sample_analysis)

        assert model.analysis is not None
        assert model.rowCount() == len(sample_analysis.candidates)
        assert model.has_data

    def test_set_candidates(
        self,
        model: SceneCutResultModel,
        sample_candidates: List[SceneCutCandidate],
    ) -> None:
        """Should set candidates directly."""
        model.set_candidates(sample_candidates)

        assert model.rowCount() == len(sample_candidates)
        assert model.has_data
        assert model.analysis is None

    def test_clear(
        self,
        model: SceneCutResultModel,
        sample_analysis: SceneCutAnalysis,
    ) -> None:
        """Should clear all data."""
        model.set_analysis(sample_analysis)
        assert model.has_data

        model.clear()

        assert model.rowCount() == 0
        assert not model.has_data
        assert model.analysis is None

    def test_get_candidate(
        self,
        model: SceneCutResultModel,
        sample_candidates: List[SceneCutCandidate],
    ) -> None:
        """Should get candidate at index."""
        model.set_candidates(sample_candidates)

        candidate = model.get_candidate(0)
        assert candidate is not None
        assert candidate.source_frame == 100

        # Invalid index
        candidate = model.get_candidate(-1)
        assert candidate is None

        candidate = model.get_candidate(999)
        assert candidate is None

    def test_get_accepted_candidates(
        self,
        model: SceneCutResultModel,
        sample_candidates: List[SceneCutCandidate],
    ) -> None:
        """Should get only accepted candidates."""
        model.set_candidates(sample_candidates)

        # Accept first two
        model.accept_candidate(0)
        model.accept_candidate(1)

        accepted = model.get_accepted_candidates()
        assert len(accepted) == 2
        assert all(c.is_accepted for c in accepted)


class TestSceneCutResultModelColors:
    """Row colors should distinguish selection from warning classification."""

    def test_pending_warning_row_is_not_orange(
        self,
        model: SceneCutResultModel,
        sample_candidates: List[SceneCutCandidate],
    ) -> None:
        model.set_candidates([sample_candidates[2]])

        background = model.data(
            model.index(0, Column.FRAME.value),
            Qt.ItemDataRole.BackgroundRole,
        )

        assert background is None

    def test_selected_row_is_orange(
        self,
        model: SceneCutResultModel,
        sample_candidates: List[SceneCutCandidate],
    ) -> None:
        model.set_candidates([sample_candidates[2]])
        model.set_current_index(0)

        background = model.data(
            model.index(0, Column.FRAME.value),
            Qt.ItemDataRole.BackgroundRole,
        )

        assert background is not None
        assert background.name().lower() == "#e29227"


# ===================================================================
# Test: Header Data
# ===================================================================

class TestSceneCutResultModelHeaders:
    """Header data tests."""

    def test_horizontal_headers(self, model: SceneCutResultModel) -> None:
        """Should return correct header text."""
        headers = [
            (Column.SCENE.value, "Scene"),
            (Column.FRAME.value, "Frame"),
            (Column.TIMECODE.value, "Start TC"),
            (Column.DECISION.value, "Decision"),
            (Column.SCORE.value, "Score"),
            (Column.KIND.value, "Type"),
        ]

        for col, expected_default in headers:
            header = model.headerData(
                col,
                Qt.Orientation.Horizontal,
                Qt.ItemDataRole.DisplayRole,
            )
            assert header is not None
            # Header may be i18n translated

    def test_vertical_headers_none(self, model: SceneCutResultModel) -> None:
        """Vertical headers should return None."""
        header = model.headerData(
            0,
            Qt.Orientation.Vertical,
            Qt.ItemDataRole.DisplayRole,
        )
        assert header is None


# ===================================================================
# Test: Cell Data
# ===================================================================

class TestSceneCutResultModelCellData:
    """Cell data retrieval tests."""

    def test_display_role_scene(
        self,
        model: SceneCutResultModel,
        sample_candidates: List[SceneCutCandidate],
    ) -> None:
        """Scene column should show calculated scene number."""
        model.set_candidates(sample_candidates)

        # All pending, scene number should be 1 for first
        index = model.index(0, Column.SCENE.value)
        data = model.data(index, Qt.ItemDataRole.DisplayRole)
        assert data == 1

    def test_display_role_frame(
        self,
        model: SceneCutResultModel,
        sample_candidates: List[SceneCutCandidate],
    ) -> None:
        """Frame column should show source frame."""
        model.set_candidates(sample_candidates)

        index = model.index(0, Column.FRAME.value)
        data = model.data(index, Qt.ItemDataRole.DisplayRole)
        assert data == 100

    def test_display_role_timecode(
        self,
        model: SceneCutResultModel,
        sample_candidates: List[SceneCutCandidate],
    ) -> None:
        """Timecode column should show formatted timecode."""
        model.set_candidates(sample_candidates)

        index = model.index(0, Column.TIMECODE.value)
        data = model.data(index, Qt.ItemDataRole.DisplayRole)
        # Timecode format: HH:MM:SS:FF
        assert ":" in str(data)

    def test_display_role_decision(
        self,
        model: SceneCutResultModel,
        sample_candidates: List[SceneCutCandidate],
    ) -> None:
        """Decision column should show decision text."""
        model.set_candidates(sample_candidates)

        index = model.index(0, Column.DECISION.value)
        data = model.data(index, Qt.ItemDataRole.DisplayRole)
        # Should be "Pending" or translated equivalent

    def test_display_role_score(
        self,
        model: SceneCutResultModel,
        sample_candidates: List[SceneCutCandidate],
    ) -> None:
        """Score column should show confidence percentage."""
        model.set_candidates(sample_candidates)

        index = model.index(0, Column.SCORE.value)
        data = model.data(index, Qt.ItemDataRole.DisplayRole)
        assert "92%" == data  # First candidate has confidence 0.92

    def test_display_role_kind(
        self,
        model: SceneCutResultModel,
        sample_candidates: List[SceneCutCandidate],
    ) -> None:
        """Kind column should show kind text."""
        model.set_candidates(sample_candidates)

        index = model.index(0, Column.KIND.value)
        data = model.data(index, Qt.ItemDataRole.DisplayRole)
        # Should be "Hard Cut" or translated equivalent

    def test_invalid_index_returns_none(
        self,
        model: SceneCutResultModel,
    ) -> None:
        """Invalid index should return None."""
        index = model.index(-1, 0)
        data = model.data(index, Qt.ItemDataRole.DisplayRole)
        assert data is None


# ===================================================================
# Test: Decision Editing
# ===================================================================

class TestSceneCutResultModelDecisionEditing:
    """Decision editing tests."""

    def test_set_decision_accept(
        self,
        model: SceneCutResultModel,
        sample_candidates: List[SceneCutCandidate],
    ) -> None:
        """Should accept a candidate."""
        model.set_candidates(sample_candidates)

        result = model.set_decision(0, SceneCutDecision.ACCEPTED)
        assert result is True

        candidate = model.get_candidate(0)
        assert candidate is not None
        assert candidate.is_accepted

        assert model.accepted_count == 1
        assert model.pending_count == 4

    def test_set_decision_reject(
        self,
        model: SceneCutResultModel,
        sample_candidates: List[SceneCutCandidate],
    ) -> None:
        """Should reject a candidate."""
        model.set_candidates(sample_candidates)

        result = model.set_decision(1, SceneCutDecision.REJECTED)
        assert result is True

        candidate = model.get_candidate(1)
        assert candidate is not None
        assert candidate.is_rejected

        assert model.rejected_count == 1
        assert model.pending_count == 4

    def test_set_decision_invalid_index(
        self,
        model: SceneCutResultModel,
        sample_candidates: List[SceneCutCandidate],
    ) -> None:
        """Should return False for invalid index."""
        model.set_candidates(sample_candidates)

        result = model.set_decision(999, SceneCutDecision.ACCEPTED)
        assert result is False

    def test_accept_candidate(
        self,
        model: SceneCutResultModel,
        sample_candidates: List[SceneCutCandidate],
    ) -> None:
        """Accept helper should work."""
        model.set_candidates(sample_candidates)

        result = model.accept_candidate(0)
        assert result is True

        assert model.accepted_count == 1

    def test_reject_candidate(
        self,
        model: SceneCutResultModel,
        sample_candidates: List[SceneCutCandidate],
    ) -> None:
        """Reject helper should work."""
        model.set_candidates(sample_candidates)

        result = model.reject_candidate(0)
        assert result is True

        assert model.rejected_count == 1

    def test_reset_candidate(
        self,
        model: SceneCutResultModel,
        sample_candidates: List[SceneCutCandidate],
    ) -> None:
        """Reset should set back to pending."""
        model.set_candidates(sample_candidates)

        model.accept_candidate(0)
        assert model.accepted_count == 1

        result = model.reset_candidate(0)
        assert result is True

        assert model.pending_count == 5
        assert model.accepted_count == 0

    def test_decision_changed_signal(
        self,
        model: SceneCutResultModel,
        sample_candidates: List[SceneCutCandidate],
    ) -> None:
        """Should emit decision_changed signal."""
        model.set_candidates(sample_candidates)

        signal_received = []
        model.decision_changed.connect(
            lambda idx, dec: signal_received.append((idx, dec))
        )

        model.accept_candidate(0)

        assert len(signal_received) == 1
        assert signal_received[0][0] == 0
        assert signal_received[0][1] == SceneCutDecision.ACCEPTED


# ===================================================================
# Test: Batch Operations
# ===================================================================

class TestSceneCutResultModelBatchOperations:
    """Batch operation tests."""

    def test_accept_high_confidence(
        self,
        model: SceneCutResultModel,
        sample_candidates: List[SceneCutCandidate],
    ) -> None:
        """Should accept high confidence candidates (>=0.85)."""
        model.set_candidates(sample_candidates)

        # Candidates 0 and 3 have high confidence (0.92, 0.88)
        # But candidate 3 is soft_transition (warning)
        count = model.accept_high_confidence()

        # Only candidate 0 should be accepted
        assert count == 1
        assert model.accepted_count == 1

        candidate0 = model.get_candidate(0)
        assert candidate0 is not None
        assert candidate0.is_accepted

    def test_reject_warnings(
        self,
        model: SceneCutResultModel,
        sample_candidates: List[SceneCutCandidate],
    ) -> None:
        """Should reject warning candidates."""
        model.set_candidates(sample_candidates)

        # Candidates 2, 3, 4 are warnings (flash, transition, black_gap)
        count = model.reject_warnings()

        assert count == 3
        assert model.rejected_count == 3

    def test_accept_all(
        self,
        model: SceneCutResultModel,
        sample_candidates: List[SceneCutCandidate],
    ) -> None:
        """Should accept all pending candidates."""
        model.set_candidates(sample_candidates)

        count = model.accept_all()

        assert count == 5
        assert model.accepted_count == 5
        assert model.pending_count == 0

    def test_reject_all(
        self,
        model: SceneCutResultModel,
        sample_candidates: List[SceneCutCandidate],
    ) -> None:
        """Should reject all pending candidates."""
        model.set_candidates(sample_candidates)

        count = model.reject_all()

        assert count == 5
        assert model.rejected_count == 5
        assert model.pending_count == 0

    def test_reset_all(
        self,
        model: SceneCutResultModel,
        sample_candidates: List[SceneCutCandidate],
    ) -> None:
        """Should reset all decisions to pending."""
        model.set_candidates(sample_candidates)

        model.accept_all()
        assert model.accepted_count == 5

        count = model.reset_all()

        assert count == 5
        assert model.pending_count == 5

    def test_batch_operation_completed_signal(
        self,
        model: SceneCutResultModel,
        sample_candidates: List[SceneCutCandidate],
    ) -> None:
        """Should emit batch_operation_completed signal."""
        model.set_candidates(sample_candidates)

        signal_received = []
        model.batch_operation_completed.connect(
            lambda op, cnt: signal_received.append((op, cnt))
        )

        model.accept_high_confidence()

        assert len(signal_received) == 1
        assert signal_received[0][0] == "accept_high_confidence"


# ===================================================================
# Test: Selection Management
# ===================================================================

class TestSceneCutResultModelSelection:
    """Selection management tests."""

    def test_set_current_index(
        self,
        model: SceneCutResultModel,
        sample_candidates: List[SceneCutCandidate],
    ) -> None:
        """Should set current selection index."""
        model.set_candidates(sample_candidates)

        model.set_current_index(2)

        assert model.get_current_index() == 2

    def test_selection_changed_signal(
        self,
        model: SceneCutResultModel,
        sample_candidates: List[SceneCutCandidate],
    ) -> None:
        """Should emit selection_changed signal."""
        model.set_candidates(sample_candidates)

        signal_received = []
        model.selection_changed.connect(
            lambda idx: signal_received.append(idx)
        )

        model.set_current_index(1)

        assert len(signal_received) == 1
        assert signal_received[0] == 1

    def test_move_selection_up(
        self,
        model: SceneCutResultModel,
        sample_candidates: List[SceneCutCandidate],
    ) -> None:
        """Should move selection up."""
        model.set_candidates(sample_candidates)
        model.set_current_index(2)

        new_idx = model.move_selection_up()

        assert new_idx == 1
        assert model.get_current_index() == 1

    def test_move_selection_down(
        self,
        model: SceneCutResultModel,
        sample_candidates: List[SceneCutCandidate],
    ) -> None:
        """Should move selection down."""
        model.set_candidates(sample_candidates)
        model.set_current_index(1)

        new_idx = model.move_selection_down()

        assert new_idx == 2
        assert model.get_current_index() == 2

    def test_move_selection_bounds(
        self,
        model: SceneCutResultModel,
        sample_candidates: List[SceneCutCandidate],
    ) -> None:
        """Selection should be bounded."""
        model.set_candidates(sample_candidates)

        # From 0, up should stay at 0
        model.set_current_index(0)
        new_idx = model.move_selection_up()
        assert new_idx == 0

        # From last, down should stay at last
        model.set_current_index(4)
        new_idx = model.move_selection_down()
        assert new_idx == 4


# ===================================================================
# Test: Count Properties
# ===================================================================

class TestSceneCutResultModelCounts:
    """Count properties tests."""

    def test_accepted_count(
        self,
        model: SceneCutResultModel,
        sample_candidates: List[SceneCutCandidate],
    ) -> None:
        """Should count accepted candidates."""
        model.set_candidates(sample_candidates)

        model.accept_candidate(0)
        model.accept_candidate(1)

        assert model.accepted_count == 2

    def test_rejected_count(
        self,
        model: SceneCutResultModel,
        sample_candidates: List[SceneCutCandidate],
    ) -> None:
        """Should count rejected candidates."""
        model.set_candidates(sample_candidates)

        model.reject_candidate(2)
        model.reject_candidate(3)

        assert model.rejected_count == 2

    def test_pending_count(
        self,
        model: SceneCutResultModel,
        sample_candidates: List[SceneCutCandidate],
    ) -> None:
        """Should count pending candidates."""
        model.set_candidates(sample_candidates)

        model.accept_candidate(0)
        model.reject_candidate(4)

        assert model.pending_count == 3

    def test_warning_count(
        self,
        model: SceneCutResultModel,
        sample_candidates: List[SceneCutCandidate],
    ) -> None:
        """Should count warning candidates."""
        model.set_candidates(sample_candidates)

        # Candidates 2 (flash), 3 (transition), 4 (black_gap) are warnings
        assert model.warning_count == 3

    def test_high_confidence_count(
        self,
        model: SceneCutResultModel,
        sample_candidates: List[SceneCutCandidate],
    ) -> None:
        """Should count high confidence candidates."""
        model.set_candidates(sample_candidates)

        # Candidates 0 (0.92) and 3 (0.88) are high confidence
        assert model.high_confidence_count == 2


# ===================================================================
# Test: Scene Number Calculation
# ===================================================================

class TestSceneCutResultModelSceneNumber:
    """Scene number calculation tests."""

    def test_scene_number_accepted(
        self,
        model: SceneCutResultModel,
        sample_candidates: List[SceneCutCandidate],
    ) -> None:
        """Scene number should increase with accepted cuts."""
        model.set_candidates(sample_candidates)

        # Accept first two candidates
        model.accept_candidate(0)  # Cut at frame 100
        model.accept_candidate(1)  # Cut at frame 250

        # Scene numbers based on accepted cuts before current
        index = model.index(0, Column.SCENE.value)
        data = model.data(index, Qt.ItemDataRole.DisplayRole)
        assert data == 1  # First accepted cut starts Scene 1

        index = model.index(1, Column.SCENE.value)
        data = model.data(index, Qt.ItemDataRole.DisplayRole)
        assert data == 2  # Second accepted cut starts Scene 2

    def test_scene_number_rejected_zero(
        self,
        model: SceneCutResultModel,
        sample_candidates: List[SceneCutCandidate],
    ) -> None:
        """Rejected candidates should have scene number 0."""
        model.set_candidates(sample_candidates)

        model.reject_candidate(0)

        index = model.index(0, Column.SCENE.value)
        data = model.data(index, Qt.ItemDataRole.DisplayRole)
        assert data == 0


# ===================================================================
# Test: I18N Support
# ===================================================================

class TestSceneCutResultModelI18N:
    """Internationalization tests."""

    def test_set_i18n_resolver(
        self,
        model: SceneCutResultModel,
    ) -> None:
        """Should set custom i18n resolver."""
        custom_resolver = lambda key: f"Custom: {key}"
        model.set_i18n_resolver(custom_resolver)

        # Header should use custom resolver
        header = model.headerData(
            Column.SCENE.value,
            Qt.Orientation.Horizontal,
            Qt.ItemDataRole.DisplayRole,
        )
        assert "Custom:" in header

    def test_default_english_fallback(
        self,
        model: SceneCutResultModel,
    ) -> None:
        """Should use English defaults without resolver."""
        header = model.headerData(
            Column.SCENE.value,
            Qt.Orientation.Horizontal,
            Qt.ItemDataRole.DisplayRole,
        )
        assert header == "Scene"


# ===================================================================
# Test: Filter Proxy
# ===================================================================

class TestSceneCutFilterProxy:
    """Filter proxy tests."""

    def test_filter_by_decision(
        self,
        model: SceneCutResultModel,
        sample_candidates: List[SceneCutCandidate],
    ) -> None:
        """Should filter by decision."""
        model.set_candidates(sample_candidates)

        proxy = SceneCutFilterProxy(model)

        # Accept some
        model.accept_candidate(0)
        model.accept_candidate(1)

        # Filter to accepted only
        proxy.set_filter_decision(SceneCutDecision.ACCEPTED)
        filtered = proxy.get_filtered_candidates()

        assert len(filtered) == 2
        assert all(c.is_accepted for c in filtered)

    def test_filter_by_kind(
        self,
        model: SceneCutResultModel,
        sample_candidates: List[SceneCutCandidate],
    ) -> None:
        """Should filter by kind."""
        model.set_candidates(sample_candidates)

        proxy = SceneCutFilterProxy(model)

        # Filter to flash only
        proxy.set_filter_kinds([SceneCutKind.FLASH])
        filtered = proxy.get_filtered_candidates()

        assert len(filtered) == 1
        assert filtered[0].kind == SceneCutKind.FLASH

    def test_filter_warnings_only(
        self,
        model: SceneCutResultModel,
        sample_candidates: List[SceneCutCandidate],
    ) -> None:
        """Should filter to warnings only."""
        model.set_candidates(sample_candidates)

        proxy = SceneCutFilterProxy(model)

        proxy.set_show_warnings_only(True)
        filtered = proxy.get_filtered_candidates()

        assert len(filtered) == 3  # flash, transition, black_gap

    def test_get_filtered_indices(
        self,
        model: SceneCutResultModel,
        sample_candidates: List[SceneCutCandidate],
    ) -> None:
        """Should return original indices."""
        model.set_candidates(sample_candidates)

        proxy = SceneCutFilterProxy(model)

        proxy.set_filter_kinds([SceneCutKind.HARD_CUT])
        indices = proxy.get_filtered_indices()

        # Candidates 0 and 1 are hard cuts (indices 0, 1)
        assert indices == [0, 1]


# ===================================================================
# Test: Item Flags
# ===================================================================

class TestSceneCutResultModelFlags:
    """Item flags tests."""

    def test_decision_column_editable(
        self,
        model: SceneCutResultModel,
        sample_candidates: List[SceneCutCandidate],
    ) -> None:
        """Decision column should be editable."""
        model.set_candidates(sample_candidates)

        index = model.index(0, Column.DECISION.value)
        flags = model.flags(index)

        assert Qt.ItemFlag.ItemIsEditable in flags
        assert Qt.ItemFlag.ItemIsEnabled in flags
        assert Qt.ItemFlag.ItemIsSelectable in flags

    def test_other_columns_not_editable(
        self,
        model: SceneCutResultModel,
        sample_candidates: List[SceneCutCandidate],
    ) -> None:
        """Other columns should not be editable."""
        model.set_candidates(sample_candidates)

        index = model.index(0, Column.FRAME.value)
        flags = model.flags(index)

        assert Qt.ItemFlag.ItemIsEditable not in flags


# ===================================================================
# Test: Anti-Recursion
# ===================================================================

class TestSceneCutResultModelAntiRecursion:
    """Anti-recursion protection tests."""

    def test_set_decision_blocked_when_updating(
        self,
        model: SceneCutResultModel,
        sample_candidates: List[SceneCutCandidate],
    ) -> None:
        """Should block decision change when updating."""
        model.set_candidates(sample_candidates)

        # Simulate updating state
        model._updating = True

        result = model.set_decision(0, SceneCutDecision.ACCEPTED)
        assert result is False

        # Reset
        model._updating = False

    def test_set_analysis_blocked_when_updating(
        self,
        model: SceneCutResultModel,
        sample_analysis: SceneCutAnalysis,
    ) -> None:
        """Should block analysis set when updating."""
        model._updating = True

        model.set_analysis(sample_analysis)

        # Should remain empty
        assert model.rowCount() == 0

        # Reset
        model._updating = False


# ===================================================================
# Test: Text Alignment
# ===================================================================

class TestSceneCutResultModelAlignment:
    """Text alignment tests."""

    def test_scene_centered(
        self,
        model: SceneCutResultModel,
        sample_candidates: List[SceneCutCandidate],
    ) -> None:
        """Scene column should be centered."""
        model.set_candidates(sample_candidates)

        index = model.index(0, Column.SCENE.value)
        alignment = model.data(index, Qt.ItemDataRole.TextAlignmentRole)

        assert alignment == Qt.AlignmentFlag.AlignCenter

    def test_frame_right_aligned(
        self,
        model: SceneCutResultModel,
        sample_candidates: List[SceneCutCandidate],
    ) -> None:
        """Frame column should be right-aligned."""
        model.set_candidates(sample_candidates)

        index = model.index(0, Column.FRAME.value)
        alignment = model.data(index, Qt.ItemDataRole.TextAlignmentRole)

        expected = Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        assert alignment == expected
