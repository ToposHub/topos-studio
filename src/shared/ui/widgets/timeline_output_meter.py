"""Compact stereo peak meter shared by the Edit and Mixer timelines."""

from __future__ import annotations

import math

from PyQt6.QtCore import QRectF, Qt
from PyQt6.QtGui import QColor, QFont, QPaintEvent, QPainter
from PyQt6.QtWidgets import QWidget

from src.shared.i18n.i18n import I18N
from src.shared.ui.horizontal_peak_meter import PEAK_RED, paint_horizontal_peak_bar
from src.shared.ui.style_sheet import C, UI_MONO_FONT_FAMILIES


_SILENCE_DB = -60.0
_SCALE_MIN_DB = -36.0


class TimelineOutputMeter(QWidget):
    """Display the live L/R levels and the louder channel's dBFS reading."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("TimelineOutputMeter")
        self.setFixedSize(124, 24)
        self._levels = (_SILENCE_DB, _SILENCE_DB)
        self._channel_font = QFont(UI_MONO_FONT_FAMILIES)
        self._channel_font.setPixelSize(8)
        self._value_font = QFont(UI_MONO_FONT_FAMILIES)
        self._value_font.setPixelSize(10)
        self._value_font.setWeight(QFont.Weight.DemiBold)
        name = I18N.tr("mixer.transport.output_peak", "Master output peak (dBFS)")
        self.setAccessibleName(name)
        self.setToolTip(name)

    @property
    def peak_text(self) -> str:
        peak = max(self._levels)
        if peak <= _SILENCE_DB:
            return "-∞"
        return f"{peak:+.1f}" if peak > 0 else f"{peak:.1f}"

    @property
    def levels_db(self) -> tuple[float, float]:
        return self._levels

    def update_output_levels(self, levels: list[float]) -> None:
        left = levels[0] if levels else _SILENCE_DB
        right = levels[1] if len(levels) > 1 else left
        values = (
            left if math.isfinite(left) else _SILENCE_DB,
            right if math.isfinite(right) else _SILENCE_DB,
        )
        if values != self._levels:
            self._levels = values
            self.update()

    def reset_meter(self) -> None:
        self.update_output_levels([])

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802 - Qt API
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        width = float(self.width())
        left, right = 15.0, width - 36.0
        bar_width = right - left

        painter.setFont(self._channel_font)
        for channel, level, y in zip(("L", "R"), self._levels, (4.0, 14.0)):
            painter.setPen(QColor(C.TEXT_SECONDARY))
            painter.drawText(QRectF(1, y - 2, 10, 10), Qt.AlignmentFlag.AlignCenter, channel)
            paint_horizontal_peak_bar(painter, QRectF(left, y, bar_width, 6), level, _SCALE_MIN_DB)

        painter.setFont(self._value_font)
        painter.setPen(QColor(PEAK_RED if max(self._levels) >= 0.0 else C.TEXT_BRIGHT))
        painter.drawText(QRectF(right + 4, 0, width - right - 6, self.height()),
                         Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, self.peak_text)
        painter.end()
