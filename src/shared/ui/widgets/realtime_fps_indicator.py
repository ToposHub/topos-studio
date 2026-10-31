"""Realtime FPS indicator shared by preview viewers."""

from __future__ import annotations

from collections import deque
from time import monotonic
from typing import Callable, Deque, Optional

from PyQt6.QtCore import QTimer, Qt
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import QLabel


_SAMPLE_WINDOW_SECONDS = 1.0
_REFRESH_INTERVAL_MS = 500
_REALTIME_RATIO = 0.9


class RealtimeFpsIndicator(QLabel):
    """Display FPS measured from frames painted by a preview widget.

    ``record_presented_frame`` should be called after a viewer has painted a
    frame. A frame token prevents a repaint of the same frame being counted
    twice.
    """

    def __init__(
        self,
        parent=None,
        now_fn: Callable[[], float] = monotonic,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("previewFpsIndicator")
        self.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.setFixedWidth(76)
        from src.shared.i18n.i18n import I18N

        self.setToolTip(I18N.tr("fps_indicator.tooltip", "Realtime preview FPS / 实时预览帧率"))
        font = QFont("Menlo")
        font.setStyleHint(QFont.StyleHint.Monospace)
        font.setPointSize(10)
        self.setFont(font)

        self._now = now_fn
        self._target_fps: float = 24.0
        self._is_playing: bool = False
        self._last_frame_token: object = object()
        self._samples: Deque[float] = deque()
        self._measured_fps: Optional[float] = None
        self._displayed_fps: Optional[int] = None
        self._display_text: Optional[str] = None
        self._display_color: Optional[str] = None

        self._refresh_timer = QTimer(self)
        self._refresh_timer.setInterval(_REFRESH_INTERVAL_MS)
        self._refresh_timer.timeout.connect(self._refresh_display)
        self._refresh_display()

    @property
    def measured_fps(self) -> Optional[float]:
        """Return the most recent measured display FPS, if available."""
        return self._measured_fps

    @property
    def is_realtime(self) -> bool:
        """Whether the latest display measurement is close to target FPS."""
        last_sample = self._samples[-1] if self._samples else None
        stale_after = max(0.6, 2.0 / max(self._target_fps, 1.0))
        return bool(
            self._is_playing
            and self._measured_fps is not None
            and last_sample is not None
            and float(self._now()) - last_sample <= stale_after
            and self._measured_fps >= self._target_fps * _REALTIME_RATIO
        )

    def set_playback_state(self, is_playing: bool, target_fps: float = 24.0) -> None:
        """Set playback state and the target timeline frame rate."""
        try:
            target = float(target_fps)
        except (TypeError, ValueError):
            target = self._target_fps
        if target > 0.0:
            self._target_fps = target

        playing = bool(is_playing)
        if playing and not self._is_playing:
            self._samples.clear()
            self._measured_fps = None
            self._last_frame_token = object()
        self._is_playing = playing
        if playing:
            self._refresh_timer.start()
        else:
            self._refresh_timer.stop()
        self._refresh_display()

    def record_presented_frame(self, frame_token: object = None) -> None:
        """Record one frame after it has reached the preview paint path."""
        if not self._is_playing:
            return
        if frame_token is not None and frame_token == self._last_frame_token:
            return

        now = float(self._now())
        if frame_token is not None:
            self._last_frame_token = frame_token
        self._samples.append(now)
        cutoff = now - _SAMPLE_WINDOW_SECONDS
        while len(self._samples) > 2 and self._samples[0] < cutoff:
            self._samples.popleft()

        if len(self._samples) >= 2:
            elapsed = self._samples[-1] - self._samples[0]
            if elapsed > 0.0:
                self._measured_fps = (len(self._samples) - 1) / elapsed
        # UI updates are intentionally throttled by _refresh_timer. Recording
        # a frame stays on the playback hot path and only touches small local
        # data structures.

    def _refresh_display(self, now: Optional[float] = None) -> None:
        now = float(self._now()) if now is None else float(now)
        if not self._is_playing:
            if self._displayed_fps is None:
                self._set_text("FPS —")
            else:
                self._set_text(f"{self._displayed_fps} FPS")
            self._set_color("#9a9a9a")
            return

        last_sample = self._samples[-1] if self._samples else None
        stale_after = max(0.6, 2.0 / max(self._target_fps, 1.0))
        if last_sample is None:
            self._show_last_displayed_fps()
            self._set_color("#9a9a9a")
            return
        if now - last_sample > stale_after:
            self._show_last_displayed_fps()
            self._set_color(
                "#ff6b6b" if self._displayed_fps is not None else "#9a9a9a"
            )
            return

        measured = self._measured_fps
        if measured is None:
            self._show_last_displayed_fps()
            self._set_color("#9a9a9a")
            return

        self._displayed_fps = max(0, int(round(measured)))
        self._show_last_displayed_fps()
        self._set_color(
            "#7fd18b"
            if measured >= self._target_fps * _REALTIME_RATIO
            else "#ff6b6b"
        )

    def _show_last_displayed_fps(self) -> None:
        """Keep the last stable integer visible when no fresh sample exists."""
        if self._displayed_fps is None:
            self._set_text("FPS --")
        else:
            self._set_text(f"{self._displayed_fps} FPS")

    def _set_text(self, text: str) -> None:
        if self._display_text == text:
            return
        self._display_text = text
        self.setText(text)

    def _set_color(self, color: str) -> None:
        if self._display_color == color:
            return
        self._display_color = color
        self.setStyleSheet(
            f"color: {color}; font-size: 10px; font-weight: 600; "
            "padding: 0 4px; background: transparent;"
        )
