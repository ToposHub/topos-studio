"""Waveform silhouettes shared by the timeline and its display options."""

from collections.abc import Sequence

from PyQt6.QtGui import QPainterPath

WAVEFORM_STYLE_BOTTOM = "bottom_fill"
WAVEFORM_STYLE_CENTERED = "centered"
WAVEFORM_STYLES = (WAVEFORM_STYLE_BOTTOM, WAVEFORM_STYLE_CENTERED)


def waveform_baseline(style: str, center: float, height: float) -> float:
    return center + height / 2 if style == WAVEFORM_STYLE_BOTTOM else center


def waveform_path(
    lows: Sequence[float], highs: Sequence[float],
    left: float, right: float, center: float, height: float, style: str,
) -> QPainterPath:
    """Draw the peaks across the visible span, including both cut edges."""
    path = QPainterPath()
    count = len(highs)
    if count == 0:
        return path

    step = (right - left) / count
    if style == WAVEFORM_STYLE_BOTTOM:
        base = waveform_baseline(style, center, height)
        path.moveTo(left, base)
        for index, (low, high) in enumerate(zip(lows, highs)):
            peak = max(abs(float(low)), abs(float(high)))
            path.lineTo(left + index * step, base - height * peak)
        path.lineTo(right, path.currentPosition().y())
        path.lineTo(right, base)
    else:
        half = height / 2
        path.moveTo(left, center - half * float(highs[0]))
        for index, high in enumerate(highs):
            path.lineTo(left + index * step, center - half * float(high))
        path.lineTo(right, center - half * float(highs[-1]))
        path.lineTo(right, center + half * abs(float(lows[-1])))
        for index in range(count - 1, -1, -1):
            path.lineTo(left + index * step, center + half * abs(float(lows[index])))

    path.closeSubpath()
    return path
