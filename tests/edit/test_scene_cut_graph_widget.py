"""Regression tests for the Resolve-style scene-cut graph interaction."""

from __future__ import annotations

import pytest
from PyQt6.QtCore import QEvent, QPointF, Qt
from PyQt6.QtGui import QMouseEvent

from src.features.edit.ui.scene_cut_graph_widget import (
    GRAPH_DIMENSIONS,
    GraphScale,
    SceneCutGraphArea,
)


def _mouse_event(
    event_type: QEvent.Type,
    x: float,
    y: float,
    button: Qt.MouseButton,
) -> QMouseEvent:
    """Build a Qt 6 mouse event for direct widget-event testing."""
    return QMouseEvent(
        event_type,
        QPointF(x, y),
        button,
        button,
        Qt.KeyboardModifier.NoModifier,
    )


def test_graph_scale_round_trips_value_and_y() -> None:
    """Threshold dragging must map screen coordinates back to score values."""
    scale = GraphScale(600, 300, 0.0, 1000.0)

    for value in (0.0, 0.15, 0.5, 1.0):
        y = scale.value_to_y(value)
        assert scale.y_to_value(y) == pytest.approx(value, abs=0.01)


def test_threshold_line_can_be_dragged_without_selecting_a_candidate(qapp) -> None:
    """Dragging the purple line emits threshold updates and ends cleanly."""
    graph = SceneCutGraphArea()
    graph.resize(600, 300)
    graph.show()
    qapp.processEvents()

    graph.set_threshold(0.50)
    scale = graph._scale
    assert scale is not None

    updates: list[float] = []
    selected: list[int] = []
    graph.threshold_changed.connect(updates.append)
    graph.candidate_clicked.connect(selected.append)

    x = scale.get_draw_rect().center().x()
    start_y = scale.value_to_y(0.50)
    end_y = scale.value_to_y(0.25)
    graph.mousePressEvent(
        _mouse_event(
            QEvent.Type.MouseButtonPress,
            x,
            start_y,
            Qt.MouseButton.LeftButton,
        )
    )
    graph.mouseMoveEvent(
        _mouse_event(
            QEvent.Type.MouseMove,
            x,
            end_y,
            Qt.MouseButton.LeftButton,
        )
    )
    graph.mouseReleaseEvent(
        _mouse_event(
            QEvent.Type.MouseButtonRelease,
            x,
            end_y,
            Qt.MouseButton.LeftButton,
        )
    )

    assert updates
    assert updates[-1] == pytest.approx(0.25, abs=0.02)
    assert graph._threshold == pytest.approx(0.25, abs=0.02)
    assert selected == []
    assert graph._dragging_threshold is False
    assert GRAPH_DIMENSIONS["peak_width"] == 1


def test_graph_expands_low_score_peaks_for_readability(qapp) -> None:
    """Typical 0.2-0.4 scores should use the graph instead of its floor."""
    from src.features.edit.core.scene_cut.models import SceneCutCandidate

    graph = SceneCutGraphArea()
    graph.resize(600, 300)
    graph.set_threshold(0.15)
    graph.set_candidates(
        [
            SceneCutCandidate(source_frame=100, pts=100, seconds=4.0, score=0.22),
            SceneCutCandidate(source_frame=200, pts=200, seconds=8.0, score=0.40),
        ],
        1000,
    )

    assert graph._display_value_max == pytest.approx(0.40 / 0.90)
    assert graph._scale is not None
    assert graph._scale.value_to_y(0.40) < graph._scale.value_to_y(0.20)
