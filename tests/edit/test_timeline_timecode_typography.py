"""Playback digits have stable cells even under the application's QLabel theme."""

from __future__ import annotations

from typing import Any

import pytest
from PyQt6.QtCore import QRectF
from PyQt6.QtGui import QPainter
from PyQt6.QtWidgets import QWidget

from src.features.edit.ui import timeline_view
from src.shared.ui.style_sheet import DARK_THEME


pytestmark = pytest.mark.edit_ui


def test_timecode_uses_large_fixed_cells_without_theme_or_digit_width_jitter(
    qapp: Any, qtbot: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    draws: list[tuple[QRectF, int, str]] = []

    class RecordingPainter(QPainter):
        def drawText(self, rect: QRectF, flags: Any, text: str) -> None:
            draws.append((QRectF(rect), self.font().pixelSize(), text))
            super().drawText(rect, flags, text)

    parent = QWidget()
    qtbot.addWidget(parent)
    parent.setStyleSheet(DARK_THEME)
    label = timeline_view._TimelineTimecodeLabel("00:00:00:00", parent)
    label.resize(220, 38)
    monkeypatch.setattr(timeline_view, "QPainter", RecordingPainter)
    positions: list[list[QRectF]] = []
    for timecode in ("00:00:00:00", "00:00:11:11", "00:00:59:24", "01:00:00;00"):
        label.setText(timecode)
        draws.clear()
        label.grab()
        cells = draws[-11:]
        assert len(cells) == 11
        assert "".join(character for _, _, character in cells) == timecode
        assert all(size == 22 for _, size, _ in cells)
        positions.append([rect for rect, _, _ in cells])
    assert all(cells == positions[0] for cells in positions)
    assert positions[0][0].left() >= 0 and positions[0][-1].right() <= label.width()
