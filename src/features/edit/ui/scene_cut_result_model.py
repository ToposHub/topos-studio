# -*- coding: utf-8 -*-
"""
Scene Cut Result Model.

QAbstractTableModel for displaying and editing scene cut detection results.
Supports decision editing, sorting, filtering by status, and i18n.

## Design Principles

- Following .clinerules: signal anti-recursion, strong typing
- Decoupled from rendering - only data management
- Support keyboard navigation (Up/Down, Enter, Delete)
- Support batch operations (accept high confidence, reject warnings)

## Columns

- Scene: Scene number (1-based, calculated from accepted cuts)
- Frame: Source frame number where cut occurs
- Start TC: Timecode for cut point
- Decision: Accept/Pending/Reject status
- Score: Detection confidence score
- Kind: Cut type (hard_cut, flash, transition, etc.)

## Reference

- docs/resolve_like_scene_cut_detection_implementation_plan_2026-06-04.md Section P4
"""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import Any, Optional, List, Callable
from enum import Enum

from PyQt6.QtCore import (
    Qt,
    QAbstractTableModel,
    QModelIndex,
    pyqtSignal,
    QObject,
)

from src.features.edit.core.scene_cut.models import (
    SceneCutCandidate,
    SceneCutDecision,
    SceneCutKind,
    SceneCutAnalysis,
)


logger = logging.getLogger(__name__)


# ================================================================
# Column Definition
# ================================================================

class Column(Enum):
    """Column indices for the result table."""

    SCENE = 0
    """Scene number (1-based)."""

    FRAME = 1
    """Source frame number."""

    TIMECODE = 2
    """Timecode string."""

    DECISION = 3
    """User decision (Accepted/Pending/Rejected)."""

    SCORE = 4
    """Confidence score (0-100%)."""

    KIND = 5
    """Cut type classification."""


# ================================================================
# I18N Keys
# ================================================================

I18N_KEYS = {
    "header_scene": "scene_cut.header_scene",
    "header_frame": "scene_cut.header_frame",
    "header_timecode": "scene_cut.header_timecode",
    "header_decision": "scene_cut.header_decision",
    "header_score": "scene_cut.header_score",
    "header_kind": "scene_cut.header_kind",
    "decision_accepted": "scene_cut.decision_accepted",
    "decision_pending": "scene_cut.decision_pending",
    "decision_rejected": "scene_cut.decision_rejected",
    "kind_hard_cut": "scene_cut.kind_hard_cut",
    "kind_soft_transition": "scene_cut.kind_soft_transition",
    "kind_black_gap": "scene_cut.kind_black_gap",
    "kind_flash": "scene_cut.kind_flash",
    "kind_fast_motion": "scene_cut.kind_fast_motion",
    "kind_unknown": "scene_cut.kind_unknown",
}


# ================================================================
# Scene Cut Result Model
# ================================================================

class SceneCutResultModel(QAbstractTableModel):
    """
    Table model for scene cut detection results.

    Provides editable decision column, sortable rows, and i18n support.

    Signals:
        decision_changed: Emitted when a candidate decision changes.
            Parameters: (candidate_index, new_decision)
        selection_changed: Emitted when selection changes.
            Parameters: (selected_index)
        batch_operation_completed: Emitted after batch operation.
            Parameters: (operation_type, count)

    Usage:
        ```python
        model = SceneCutResultModel()
        model.set_analysis(analysis)

        # Connect to decision changes
        model.decision_changed.connect(self._on_decision_changed)

        # Batch operations
        model.accept_high_confidence()
        model.reject_warnings()
        ```

    Following .clinerules:
        - Strong typing for all methods
        - Signal anti-recursion with _updating flag
        - No direct rendering calls
    """

    # Signals
    decision_changed = pyqtSignal(int, object)  # index, SceneCutDecision
    selection_changed = pyqtSignal(int)  # selected_index
    batch_operation_completed = pyqtSignal(str, int)  # operation_type, count

    def __init__(self, parent: Optional[QObject] = None) -> None:
        """
        Initialize the model.

        Args:
            parent: Optional parent QObject.
        """
        super().__init__(parent)

        # Data storage
        self._candidates: List[SceneCutCandidate] = []
        self._analysis: Optional[SceneCutAnalysis] = None

        # Anti-recursion lock
        self._updating: bool = False

        # I18N resolver (can be set externally)
        self._i18n_resolver: Optional[Callable[[str], str]] = None

        # Timecode frame rate (AUD-20800: previously hardcoded 24fps in
        # _format_timecode with no setter; non-24fps media's Start TC
        # column drifted with duration. The consumer sets the real rate
        # via set_fps.)
        self._fps: float = 24.0

        # Current selection tracking
        self._current_index: int = -1

        logger.debug("SceneCutResultModel initialized")

    # ==================== Properties ====================

    @property
    def candidates(self) -> List[SceneCutCandidate]:
        """Get current candidates list."""
        return self._candidates

    @property
    def analysis(self) -> Optional[SceneCutAnalysis]:
        """Get current analysis."""
        return self._analysis

    @property
    def has_data(self) -> bool:
        """Check if model has data."""
        return len(self._candidates) > 0

    @property
    def accepted_count(self) -> int:
        """Count accepted candidates."""
        return sum(1 for c in self._candidates if c.is_accepted)

    @property
    def rejected_count(self) -> int:
        """Count rejected candidates."""
        return sum(1 for c in self._candidates if c.is_rejected)

    @property
    def pending_count(self) -> int:
        """Count pending candidates."""
        return sum(1 for c in self._candidates if c.is_pending)

    @property
    def warning_count(self) -> int:
        """Count candidates with warning flags."""
        return sum(1 for c in self._candidates if c.is_warning)

    @property
    def high_confidence_count(self) -> int:
        """Count high confidence candidates (>= 0.85)."""
        return sum(1 for c in self._candidates if c.is_high_confidence)

    # ==================== Data Management ====================

    def set_analysis(self, analysis: SceneCutAnalysis) -> None:
        """
        Set the analysis data.

        Replaces all existing candidates with analysis.candidates.

        Args:
            analysis: SceneCutAnalysis from detector.
        """
        if self._updating:
            return

        self._updating = True

        # Clear old data
        self.beginResetModel()
        self._analysis = analysis
        self._candidates = list(analysis.candidates)
        self._current_index = -1
        self.endResetModel()

        self._updating = False

        logger.debug(
            f"SceneCutResultModel.set_analysis: {len(self._candidates)} candidates"
        )

    def set_candidates(self, candidates: List[SceneCutCandidate]) -> None:
        """
        Set candidates directly (without analysis).

        Args:
            candidates: List of SceneCutCandidate.
        """
        if self._updating:
            return

        self._updating = True

        self.beginResetModel()
        self._candidates = candidates
        self._analysis = None
        self._current_index = -1
        self.endResetModel()

        self._updating = False

    def clear(self) -> None:
        """Clear all data."""
        if self._updating:
            return

        self._updating = True

        self.beginResetModel()
        self._candidates = []
        self._analysis = None
        self._current_index = -1
        self.endResetModel()

        self._updating = False

    def get_candidate(self, index: int) -> Optional[SceneCutCandidate]:
        """
        Get candidate at index.

        Args:
            index: Row index (0-based).

        Returns:
            SceneCutCandidate if index valid, None otherwise.
        """
        if 0 <= index < len(self._candidates):
            return self._candidates[index]
        return None

    def get_accepted_candidates(self) -> List[SceneCutCandidate]:
        """
        Get all accepted candidates.

        Returns:
            List of accepted SceneCutCandidate.
        """
        return [c for c in self._candidates if c.is_accepted]

    def to_updated_analysis(self) -> Optional[SceneCutAnalysis]:
        """
        Create an updated SceneCutAnalysis reflecting current decisions.

        Takes the original analysis and applies all decision changes
        from the model's candidate list.

        Returns:
            Updated SceneCutAnalysis or None if no analysis set.
        """
        if self._analysis is None:
            return None

        # Use the model's complete candidate list here.  Besides decision
        # changes, the list can now contain manually added cuts or have cuts
        # removed by the review UI.
        decisions = {
            c.source_frame: c.decision
            for c in self._candidates
        }
        updated_candidates = tuple(
            c.with_decision(decisions.get(c.source_frame, c.decision))
            for c in self._candidates
        )
        return replace(self._analysis, candidates=updated_candidates)

    def insert_candidate(self, candidate: SceneCutCandidate) -> int:
        """Insert a candidate in source-frame order.

        Returns the inserted row, or ``-1`` when that source frame already
        exists.  Structural changes use the normal Qt model notifications so
        the scene list refreshes immediately.
        """
        if self._updating:
            return -1
        if any(c.source_frame == candidate.source_frame for c in self._candidates):
            return -1

        row = 0
        while row < len(self._candidates):
            if self._candidates[row].source_frame > candidate.source_frame:
                break
            row += 1

        self.beginInsertRows(QModelIndex(), row, row)
        self._candidates.insert(row, candidate)
        self.endInsertRows()

        if self._current_index >= row:
            self._current_index += 1
        return row

    def remove_candidate(self, index: int) -> Optional[SceneCutCandidate]:
        """Remove a candidate from the model and return the removed item."""
        if self._updating or not (0 <= index < len(self._candidates)):
            return None

        self.beginRemoveRows(QModelIndex(), index, index)
        removed = self._candidates.pop(index)
        self.endRemoveRows()

        if self._current_index == index:
            self._current_index = -1
        elif self._current_index > index:
            self._current_index -= 1
        return removed

    # ==================== Decision Editing ====================

    def set_decision(self, index: int, decision: SceneCutDecision) -> bool:
        """
        Set decision for a candidate.

        Args:
            index: Row index.
            decision: New decision value.

        Returns:
            True if successful, False if index invalid or recursion blocked.
        """
        if self._updating:
            return False

        self._updating = True
        try:
            changed = self._apply_decision(index, decision)
        finally:
            self._updating = False

        if changed:
            # Emit decision_changed signal (outside the lock, so a handler may
            # safely call back into the model).
            self.decision_changed.emit(index, decision)

            logger.debug(
                f"Decision changed: index={index}, decision={decision.value}"
            )

        return changed

    def _apply_decision(self, index: int, decision: SceneCutDecision) -> bool:
        """
        Apply a decision to a candidate without emitting ``decision_changed``.

        Emits ``dataChanged`` for the decision column.  The caller owns the
        ``_updating`` lock and decides when (and in which order) to notify
        listeners.  Used by :meth:`set_decision` and the batch operations so a
        ``decision_changed`` handler can never re-enter while a batch loop is
        still mutating the candidate list (AUD-20803).

        Args:
            index: Row index.
            decision: New decision value.

        Returns:
            True if the decision was applied, False otherwise.
        """
        if not (0 <= index < len(self._candidates)):
            return False

        candidate = self._candidates[index]
        if candidate.decision == decision:
            return False  # No change needed

        # Create new candidate with updated decision
        new_candidate = candidate.with_decision(decision)
        self._candidates[index] = new_candidate

        # Emit dataChanged for the decision column
        self.dataChanged.emit(
            self.index(index, Column.DECISION.value),
            self.index(index, Column.DECISION.value),
            [Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.BackgroundRole]
        )

        return True

    def accept_candidate(self, index: int) -> bool:
        """
        Accept a candidate.

        Args:
            index: Row index.

        Returns:
            True if successful.
        """
        return self.set_decision(index, SceneCutDecision.ACCEPTED)

    def reject_candidate(self, index: int) -> bool:
        """
        Reject a candidate.

        Args:
            index: Row index.

        Returns:
            True if successful.
        """
        return self.set_decision(index, SceneCutDecision.REJECTED)

    def reset_candidate(self, index: int) -> bool:
        """
        Reset candidate to pending.

        Args:
            index: Row index.

        Returns:
            True if successful.
        """
        return self.set_decision(index, SceneCutDecision.PENDING)

    # ==================== Batch Operations ====================

    def _run_batch(
        self,
        operation_type: str,
        target_decision: SceneCutDecision,
        predicate: Callable[[SceneCutCandidate], bool],
    ) -> int:
        """
        Apply a decision to every candidate matching ``predicate``.

        AUD-20803: the ``_updating`` lock is held across the whole loop and
        every ``decision_changed`` emission is deferred until after the loop
        finishes, so a handler can never re-enter (batch operation,
        insert/remove_candidate) while the candidate list is still being
        enumerated.

        Args:
            operation_type: Batch operation name for the completion signal.
            target_decision: Decision to apply to matching candidates.
            predicate: Which candidates to update.

        Returns:
            Number of candidates changed.
        """
        if self._updating:
            return 0

        changed: List[tuple[int, SceneCutDecision]] = []
        self._updating = True
        try:
            for i, candidate in enumerate(self._candidates):
                if predicate(candidate):
                    if self._apply_decision(i, target_decision):
                        changed.append((i, target_decision))
        finally:
            self._updating = False

        count = len(changed)
        for index, decision in changed:
            self.decision_changed.emit(index, decision)

        self.batch_operation_completed.emit(operation_type, count)
        logger.debug(f"Batch {operation_type}: {count} candidates")
        return count

    def accept_high_confidence(self) -> int:
        """
        Accept all high confidence candidates (>= 0.85).

        Only accepts candidates that are not already rejected
        and are not warnings (flash, fast_motion, etc.).

        Returns:
            Number of candidates accepted.
        """
        return self._run_batch(
            "accept_high_confidence",
            SceneCutDecision.ACCEPTED,
            lambda candidate: (
                candidate.is_high_confidence
                and not candidate.is_warning
                and candidate.is_pending
            ),
        )

    def reject_warnings(self) -> int:
        """
        Reject all warning candidates.

        Warning candidates have low confidence, flash, fast_motion,
        soft_transition, or black_gap kinds.

        Returns:
            Number of candidates rejected.
        """
        return self._run_batch(
            "reject_warnings",
            SceneCutDecision.REJECTED,
            lambda candidate: candidate.is_warning and candidate.is_pending,
        )

    def accept_all(self) -> int:
        """
        Accept all pending candidates.

        Returns:
            Number of candidates accepted.
        """
        return self._run_batch(
            "accept_all",
            SceneCutDecision.ACCEPTED,
            lambda candidate: candidate.is_pending,
        )

    def reject_all(self) -> int:
        """
        Reject all pending candidates.

        Returns:
            Number of candidates rejected.
        """
        return self._run_batch(
            "reject_all",
            SceneCutDecision.REJECTED,
            lambda candidate: candidate.is_pending,
        )

    def reset_all(self) -> int:
        """
        Reset all candidates to pending.

        Returns:
            Number of candidates reset.
        """
        return self._run_batch(
            "reset_all",
            SceneCutDecision.PENDING,
            lambda candidate: not candidate.is_pending,
        )

    # ==================== Sorting ====================

    def sort(  # noqa: N802 - Qt API naming
        self,
        column: int,
        order: Qt.SortOrder = Qt.SortOrder.AscendingOrder,
    ) -> None:
        """
        Sort candidates by column (AUD-20807).

        Supports the Scene, Frame and Score columns; other columns (decision,
        timecode, kind) are ignored.  The current-candidate index is remapped
        so the selection follows the candidate it pointed at.

        Args:
            column: Column index to sort by.
            order: Ascending or descending sort order.
        """
        if self._updating or not self._candidates:
            return

        if column == Column.FRAME.value:
            key = lambda candidate: candidate.source_frame  # noqa: E731
        elif column == Column.SCORE.value:
            key = lambda candidate: (candidate.confidence, candidate.score)  # noqa: E731
        elif column == Column.SCENE.value:
            # Scene number mirrors the detection order (frame-ordered);
            # sorting by it restores the original sequence.
            key = lambda candidate: candidate.source_frame  # noqa: E731
        else:
            return

        reverse = order == Qt.SortOrder.DescendingOrder
        current = (
            self._candidates[self._current_index]
            if 0 <= self._current_index < len(self._candidates)
            else None
        )

        self.layoutAboutToBeChanged.emit()
        self._candidates.sort(key=key, reverse=reverse)
        if current is not None:
            for new_index, candidate in enumerate(self._candidates):
                if candidate is current:
                    self._current_index = new_index
                    break
        self.layoutChanged.emit()

    # ==================== Selection ====================

    def set_current_index(self, index: int) -> None:
        """
        Set current selection index.

        Args:
            index: Row index, -1 for no selection.
        """
        if self._current_index != index:
            self._current_index = index
            if 0 <= index < len(self._candidates):
                self.selection_changed.emit(index)

    def get_current_index(self) -> int:
        """Get current selection index."""
        return self._current_index

    def move_selection_up(self) -> int:
        """
        Move selection up by one.

        Returns:
            New selection index.
        """
        new_index = max(0, self._current_index - 1)
        self.set_current_index(new_index)
        return new_index

    def move_selection_down(self) -> int:
        """
        Move selection down by one.

        Returns:
            New selection index.
        """
        new_index = min(len(self._candidates) - 1, self._current_index + 1)
        self.set_current_index(new_index)
        return new_index

    # ==================== Frame Rate ====================

    def set_fps(self, fps: float) -> None:
        """Set the frame rate used for the Start TC column.

        AUD-20800: the model previously formatted every timecode at a
        hardcoded 24fps with no way for the consumer to inject the real
        rate; non-24fps media showed drifting Start TC values. Refreshes
        the TIMECODE column when the rate changes.
        """
        safe_fps = max(float(fps), 1.0)
        if abs(safe_fps - self._fps) < 1e-9:
            return
        self._fps = safe_fps
        if self._candidates:
            self.dataChanged.emit(
                self.index(0, Column.TIMECODE.value),
                self.index(len(self._candidates) - 1, Column.TIMECODE.value),
                [Qt.ItemDataRole.DisplayRole],
            )

    def get_fps(self) -> float:
        """Get the frame rate used for timecode formatting."""
        return self._fps

    # ==================== I18N ====================

    def set_i18n_resolver(self, resolver: Callable[[str], str]) -> None:
        """
        Set i18n resolver function.

        Args:
            resolver: Function that takes a key and returns translated string.
        """
        self._i18n_resolver = resolver

    def _translate(self, key: str) -> str:
        """
        Translate a key using resolver or return default.

        Args:
            key: I18N key.

        Returns:
            Translated string.
        """
        if self._i18n_resolver:
            return self._i18n_resolver(key)

        # Default English fallback
        defaults = {
            "scene_cut.header_scene": "Scene",
            "scene_cut.header_frame": "Frame",
            "scene_cut.header_timecode": "Start TC",
            "scene_cut.header_decision": "Decision",
            "scene_cut.header_score": "Score",
            "scene_cut.header_kind": "Type",
            "scene_cut.decision_accepted": "Accepted",
            "scene_cut.decision_pending": "Pending",
            "scene_cut.decision_rejected": "Rejected",
            "scene_cut.kind_hard_cut": "Hard Cut",
            "scene_cut.kind_soft_transition": "Transition",
            "scene_cut.kind_black_gap": "Black Gap",
            "scene_cut.kind_flash": "Flash",
            "scene_cut.kind_fast_motion": "Fast Motion",
            "scene_cut.kind_unknown": "Unknown",
        }
        return defaults.get(key, key)

    # ==================== Scene Number Calculation ====================

    def _calculate_scene_number(self, index: int) -> int:
        """
        Calculate scene number for a candidate.

        Scene number is based on position among accepted cuts.
        N cuts generate N+1 scenes.

        Args:
            index: Candidate index.

        Returns:
            Scene number (1-based).
        """
        if not (0 <= index < len(self._candidates)):
            return 0

        candidate = self._candidates[index]
        if candidate.is_rejected:
            return 0  # Rejected cuts don't have scene numbers

        # Count accepted cuts before this one
        accepted_before = sum(
            1 for i, c in enumerate(self._candidates)
            if i < index and c.is_accepted
        )

        # Scene number = accepted_before + 1
        # (each accepted cut starts a new scene)
        return accepted_before + 1

    # ==================== Timecode Generation ====================

    def _format_timecode(self, seconds: float, fps: float = 24.0) -> str:
        """
        Format seconds to timecode string.

        Args:
            seconds: Time in seconds.
            fps: Frames per second (default 24.0).

        Returns:
            Timecode string (HH:MM:SS:FF).
        """
        if seconds < 0:
            return "00:00:00:00"

        # AUD-20800: total_frames 与 h/m/s 分桶此前混用 float fps 与
        # int(fps)（29.97 截断为 29），分数帧率下分钟位随时长漂移；
        # 统一按四舍五入后的整数帧率换算，保证自洽。
        fps_int = max(1, int(round(fps)))
        total_frames = int(seconds * fps_int + 0.5)
        hours = total_frames // (3600 * fps_int)
        remaining = total_frames % (3600 * fps_int)
        minutes = remaining // (60 * fps_int)
        remaining = remaining % (60 * fps_int)
        secs = remaining // fps_int
        frames = remaining % fps_int

        return f"{hours:02d}:{minutes:02d}:{secs:02d}:{frames:02d}"

    # ==================== QAbstractTableModel Implementation ====================

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        """
        Return number of rows.

        Args:
            parent: Parent index (unused for table model).

        Returns:
            Number of candidates.
        """
        if parent.isValid():
            return 0
        return len(self._candidates)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        """
        Return number of columns.

        Args:
            parent: Parent index (unused for table model).

        Returns:
            Number of columns (6).
        """
        if parent.isValid():
            return 0
        return len(Column)

    def headerData(
        self,
        section: int,
        orientation: Qt.Orientation,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> Any:
        """
        Return header data.

        Args:
            section: Column/row index.
            orientation: Horizontal or Vertical.
            role: Data role.

        Returns:
            Header string or None.
        """
        if orientation != Qt.Orientation.Horizontal:
            return None

        if role != Qt.ItemDataRole.DisplayRole:
            return None

        if section == Column.SCENE.value:
            return self._translate(I18N_KEYS["header_scene"])
        elif section == Column.FRAME.value:
            return self._translate(I18N_KEYS["header_frame"])
        elif section == Column.TIMECODE.value:
            return self._translate(I18N_KEYS["header_timecode"])
        elif section == Column.DECISION.value:
            return self._translate(I18N_KEYS["header_decision"])
        elif section == Column.SCORE.value:
            return self._translate(I18N_KEYS["header_score"])
        elif section == Column.KIND.value:
            return self._translate(I18N_KEYS["header_kind"])

        return None

    def data(
        self,
        index: QModelIndex,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> Any:
        """
        Return cell data.

        Args:
            index: Cell index.
            role: Data role.

        Returns:
            Cell data or None.
        """
        if not index.isValid():
            return None

        row = index.row()
        col = index.column()

        if not (0 <= row < len(self._candidates)):
            return None

        candidate = self._candidates[row]

        # Display role
        if role == Qt.ItemDataRole.DisplayRole:
            if col == Column.SCENE.value:
                return self._calculate_scene_number(row)
            elif col == Column.FRAME.value:
                return candidate.source_frame
            elif col == Column.TIMECODE.value:
                return self._format_timecode(candidate.seconds, self._fps)
            elif col == Column.DECISION.value:
                return self._decision_to_string(candidate.decision)
            elif col == Column.SCORE.value:
                return f"{candidate.confidence * 100:.0f}%"
            elif col == Column.KIND.value:
                return self._kind_to_string(candidate.kind)

        # Background color role
        if role == Qt.ItemDataRole.BackgroundRole:
            return self._get_background_color(row, col, candidate)

        # Foreground color role
        if role == Qt.ItemDataRole.ForegroundRole:
            return self._get_foreground_color(row, col, candidate)

        # Text alignment role
        if role == Qt.ItemDataRole.TextAlignmentRole:
            if col == Column.SCENE.value:
                return Qt.AlignmentFlag.AlignCenter
            elif col == Column.FRAME.value:
                return Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
            elif col == Column.SCORE.value:
                return Qt.AlignmentFlag.AlignCenter
            else:
                return Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter

        return None

    def setData(
        self,
        index: QModelIndex,
        value: Any,
        role: int = Qt.ItemDataRole.EditRole,
    ) -> bool:
        """
        Set cell data (for editable columns).

        Only DECISION column is editable.

        Args:
            index: Cell index.
            value: New value.
            role: Data role.

        Returns:
            True if successful.
        """
        if not index.isValid():
            return False

        col = index.column()
        if col != Column.DECISION.value:
            return False  # Only decision column is editable

        # Convert value to decision
        if isinstance(value, SceneCutDecision):
            decision = value
        elif isinstance(value, str):
            decision = self._string_to_decision(value)
        else:
            return False

        return self.set_decision(index.row(), decision)

    def flags(self, index: QModelIndex) -> Qt.ItemFlag:
        """
        Return item flags.

        Args:
            index: Cell index.

        Returns:
            Item flags.
        """
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags

        flags = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable

        # Decision column is editable
        if index.column() == Column.DECISION.value:
            flags |= Qt.ItemFlag.ItemIsEditable

        return flags

    # ==================== Helper Methods ====================

    def _decision_to_string(self, decision: SceneCutDecision) -> str:
        """Convert decision enum to display string."""
        if decision == SceneCutDecision.ACCEPTED:
            return self._translate(I18N_KEYS["decision_accepted"])
        elif decision == SceneCutDecision.PENDING:
            return self._translate(I18N_KEYS["decision_pending"])
        elif decision == SceneCutDecision.REJECTED:
            return self._translate(I18N_KEYS["decision_rejected"])
        return decision.value

    def _string_to_decision(self, value: str) -> SceneCutDecision:
        """Convert string to decision enum."""
        value_lower = value.lower()
        if value_lower in ("accepted", "accept", self._translate(I18N_KEYS["decision_accepted"]).lower()):
            return SceneCutDecision.ACCEPTED
        elif value_lower in ("rejected", "reject", self._translate(I18N_KEYS["decision_rejected"]).lower()):
            return SceneCutDecision.REJECTED
        elif value_lower in ("pending", self._translate(I18N_KEYS["decision_pending"]).lower()):
            return SceneCutDecision.PENDING
        return SceneCutDecision.PENDING

    def _kind_to_string(self, kind: SceneCutKind) -> str:
        """Convert kind enum to display string."""
        if kind == SceneCutKind.HARD_CUT:
            return self._translate(I18N_KEYS["kind_hard_cut"])
        elif kind == SceneCutKind.SOFT_TRANSITION:
            return self._translate(I18N_KEYS["kind_soft_transition"])
        elif kind == SceneCutKind.BLACK_GAP:
            return self._translate(I18N_KEYS["kind_black_gap"])
        elif kind == SceneCutKind.FLASH:
            return self._translate(I18N_KEYS["kind_flash"])
        elif kind == SceneCutKind.FAST_MOTION:
            return self._translate(I18N_KEYS["kind_fast_motion"])
        return self._translate(I18N_KEYS["kind_unknown"])

    def _get_background_color(
        self,
        row: int,
        col: int,
        candidate: SceneCutCandidate,
    ) -> Optional[Any]:
        """
        Get background color for cell.

        Args:
            row: Row index.
            col: Column index.
            candidate: SceneCutCandidate.

        Returns:
            QColor or None.
        """
        from PyQt6.QtGui import QColor

        # Highlight current selection
        if row == self._current_index:
            return QColor("#E29227")  # Accent orange

        # Accepted: subtle green tint
        if candidate.is_accepted:
            return QColor(0x73, 0xb6, 0x6b, 0x30)  # 30% green

        # Rejected: muted gray
        if candidate.is_rejected:
            return QColor(0x55, 0x55, 0x55, 0x40)  # 40% gray

        # Pending candidates stay neutral. Warning status is represented by
        # the decision/status controls, not by tinting every row orange;
        # orange is reserved for the currently selected row.
        return None

    def _get_foreground_color(
        self,
        row: int,
        col: int,
        candidate: SceneCutCandidate,
    ) -> Optional[Any]:
        """
        Get foreground color for cell.

        Args:
            row: Row index.
            col: Column index.
            candidate: SceneCutCandidate.

        Returns:
            QColor or None.
        """
        from PyQt6.QtGui import QColor

        # Selected row: white text
        if row == self._current_index:
            return QColor("#FFFFFF")

        # Rejected: muted text
        if candidate.is_rejected:
            return QColor("#8E929A")

        return None


# ================================================================
# Filter Proxy Model (Optional)
# ================================================================

class SceneCutFilterProxy(QObject):
    """
    Filter proxy for candidates.

    Provides filtering by decision status and kind.

    Usage:
        ```python
        proxy = SceneCutFilterProxy(model)
        proxy.set_filter_decision(SceneCutDecision.PENDING)
        filtered = proxy.get_filtered_candidates()
        ```
    """

    def __init__(
        self,
        model: SceneCutResultModel,
        parent: Optional[QObject] = None,
    ) -> None:
        """
        Initialize filter proxy.

        Args:
            model: Source model.
            parent: Optional parent QObject.
        """
        super().__init__(parent)
        self._model = model
        self._filter_decision: Optional[SceneCutDecision] = None
        self._filter_kinds: Optional[List[SceneCutKind]] = None
        self._show_warnings_only: bool = False

    def set_filter_decision(self, decision: Optional[SceneCutDecision]) -> None:
        """
        Set decision filter.

        Args:
            decision: Decision to filter, None for all.
        """
        self._filter_decision = decision

    def set_filter_kinds(self, kinds: Optional[List[SceneCutKind]]) -> None:
        """
        Set kind filter.

        Args:
            kinds: Kinds to filter, None for all.
        """
        self._filter_kinds = kinds

    def set_show_warnings_only(self, value: bool) -> None:
        """
        Set warning-only filter.

        Args:
            value: True to show only warnings.
        """
        self._show_warnings_only = value

    def get_filtered_candidates(self) -> List[SceneCutCandidate]:
        """
        Get filtered candidates.

        Returns:
            List of candidates matching filters.
        """
        candidates = self._model.candidates
        result = []

        for candidate in candidates:
            # Decision filter
            if self._filter_decision is not None:
                if candidate.decision != self._filter_decision:
                    continue

            # Kind filter
            if self._filter_kinds is not None:
                if candidate.kind not in self._filter_kinds:
                    continue

            # Warning filter
            if self._show_warnings_only:
                if not candidate.is_warning:
                    continue

            result.append(candidate)

        return result

    def get_filtered_indices(self) -> List[int]:
        """
        Get original indices of filtered candidates.

        Returns:
            List of original indices.
        """
        candidates = self._model.candidates
        result = []

        for i, candidate in enumerate(candidates):
            # Decision filter
            if self._filter_decision is not None:
                if candidate.decision != self._filter_decision:
                    continue

            # Kind filter
            if self._filter_kinds is not None:
                if candidate.kind not in self._filter_kinds:
                    continue

            # Warning filter
            if self._show_warnings_only:
                if not candidate.is_warning:
                    continue

            result.append(i)

        return result
