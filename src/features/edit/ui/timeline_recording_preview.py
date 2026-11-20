"""Transient waveform for audio being recorded into a timeline track."""

from __future__ import annotations

import math

from PyQt6.QtCore import QRectF
from PyQt6.QtGui import QColor, QPainter, QPainterPath, QPen

from src.shared.i18n.i18n import I18N


class RecordingPreview:
    _MAX_BUCKETS = 4096

    def __init__(self, start_frame: int) -> None:
        self.start_frame = start_frame
        self.duration_seconds = 0.0
        self._bucket_seconds = 1 / 40
        self._peaks: list[float] = []
        self.rect = QRectF()
        self._pixels_per_second = 1.0
        self._sample_origin = 0.0
        self._label = I18N.tr("mixer.record.live_clip", "Recording…")

    def add_audio(self, duration_seconds: float, peak: float) -> None:
        """Accumulate a bounded peak envelope without retaining audio samples."""
        if duration_seconds <= self.duration_seconds:
            return
        while math.ceil(duration_seconds / self._bucket_seconds) > self._MAX_BUCKETS:
            self._peaks = [max(self._peaks[index:index + 2])
                           for index in range(0, len(self._peaks), 2)]
            self._bucket_seconds *= 2
        start = int(self.duration_seconds / self._bucket_seconds)
        end = math.ceil(duration_seconds / self._bucket_seconds)
        self._peaks.extend([0.0] * max(0, end - len(self._peaks)))
        peak = min(1.0, max(0.0, peak))
        for index in range(start, end):
            self._peaks[index] = max(self._peaks[index], peak)
        self.duration_seconds = duration_seconds

    def move_to_track(self, x: float, y: float, height: float, pixels_per_frame: float, fps: float) -> None:
        """Project the recording time into the current track geometry."""
        self._pixels_per_second = pixels_per_frame * fps
        self._sample_origin = min(0, self.start_frame) * pixels_per_frame
        width = max(3.0, self._sample_origin + self.duration_seconds * self._pixels_per_second)
        self.rect = QRectF(x, y + 1.0, width, max(1.0, height - 2.0))

    def paint(self, painter: QPainter, exposed_rect: QRectF) -> None:
        if not exposed_rect.intersects(self.rect):
            return
        origin = self.rect.topLeft()
        rect = self.rect.translated(-origin)
        exposed = self.rect.intersected(exposed_rect).translated(-origin)
        painter.save()
        painter.translate(origin)
        painter.fillRect(rect, QColor(154, 48, 58, 218))
        painter.fillRect(QRectF(rect.right() - 2.0, 0, 2.0, rect.height()), QColor("#f6797f"))

        label_height = 0.0
        metrics = painter.fontMetrics()
        if rect.width() >= metrics.horizontalAdvance(self._label) + 10 and rect.height() >= metrics.height() + 8:
            label_height = metrics.height() + 3
            painter.setPen(QColor("#fff4f4"))
            painter.drawText(5, metrics.ascent() + 2, self._label)

        bucket_width = self._bucket_seconds * self._pixels_per_second
        first = max(0, math.floor((exposed.left() - self._sample_origin) / bucket_width))
        last = min(len(self._peaks), math.ceil((exposed.right() - self._sample_origin) / bucket_width))
        center = (label_height + rect.height()) / 2
        amplitude = max(0.0, (rect.height() - label_height) / 2 - 2)
        path = QPainterPath()
        for index in range(first, last):
            x = min(rect.right() - 2, self._sample_origin + (index + 0.5) * bucket_width)
            if 0 <= x < rect.right() - 1:
                peak = max(0.5, self._peaks[index] * amplitude)
                path.moveTo(x, center - peak)
                path.lineTo(x, center + peak)
        pen = QPen(QColor("#f6a8aa"))
        pen.setCosmetic(True)
        painter.setPen(pen)
        painter.drawPath(path)
        painter.restore()
