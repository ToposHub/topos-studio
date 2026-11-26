"""Painter geometry shared by the project clip editor and timeline."""
from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections.abc import Callable, Sequence

from PyQt6.QtGui import QColor, QPainterPath

from src.features.edit.core.audio_edit import ENVELOPE_BEZIER
from src.features.edit.core.audio_edit import envelope_bezier_weight


def envelope_db_at(
    points: Sequence[tuple[float, float]], modes: Sequence[str], position: float,
) -> float:
    """Evaluate the same outgoing segments used by the painter path and DSP."""
    if not points:
        return 0.0
    index = bisect_right(points, position, key=lambda point: point[0]) - 1
    if index < 0:
        return points[0][1]
    if index >= len(points) - 1:
        return points[-1][1]
    start, start_db = points[index]
    end, end_db = points[index + 1]
    weight = (position - start) / (end - start)
    if index < len(modes) and modes[index] == ENVELOPE_BEZIER:
        weight = float(envelope_bezier_weight(weight))
    return start_db + (end_db - start_db) * weight


def envelope_accent(background: QColor) -> QColor:
    """Keep the clip's hue while lifting the curve above the waveform."""
    hue, saturation, _, _ = background.getHslF()
    return QColor.fromHslF(max(0.0, hue), min(0.72, saturation * 1.35), 0.8)


def envelope_gain_readout(db: float, start_db: float) -> str:
    delta = db - start_db
    if abs(delta) < 0.05:
        return f"{db:+.1f} dB"
    return f"{db:+.1f} dB  ({delta:+.1f} dB)"


def envelope_path(
    points: Sequence[tuple[float, float]],
    modes: Sequence[str],
    start: float,
    end: float,
    to_x: Callable[[float], float],
    to_y: Callable[[float], float],
) -> QPainterPath:
    """Project the model curve through affine time and gain scales."""
    path = QPainterPath()
    if not points:
        path.moveTo(to_x(start), to_y(0.0))
        path.lineTo(to_x(end), to_y(0.0))
        return path

    first = max(0, bisect_right(points, start, key=lambda point: point[0]) - 1)
    last = min(len(points) - 1, bisect_left(points, end, key=lambda point: point[0]))
    time, db = points[first]
    x, y = to_x(time), to_y(db)
    path.moveTo(to_x(min(start, time)), y)
    path.lineTo(x, y)
    for index in range(first, last):
        time, db = points[index + 1]
        next_x, next_y = to_x(time), to_y(db)
        if index < len(modes) and modes[index] == ENVELOPE_BEZIER:
            third = (next_x - x) / 3.0
            path.cubicTo(x + third, y, next_x - third, next_y, next_x, next_y)
        else:
            path.lineTo(next_x, next_y)
        x, y = next_x, next_y
    path.lineTo(to_x(max(end, points[last][0])), y)
    return path
