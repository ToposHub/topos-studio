"""KeyframesTimelineView — the right-hand time/track side of the panel.

A self-painted QWidget drawing (plan §3.4):
  - a frame ruler with major/minor ticks and frame labels (supports negative)
  - the red playhead line + cap
  - per-row range bars (node effective time range)
  - per-row keyframe diamonds + thin curve segments
  - shared vertical scroll with the left tree view

Phase 3/4: static painting + ruler + playhead.
Phase 5: mouse interaction (drag playhead / keyframe / range bar edges).
"""

from __future__ import annotations

import logging
import math
from typing import Any, List, Optional, Tuple

from PyQt6.QtCore import Qt, QPointF, QRectF, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QPainter, QPen, QPolygonF
from PyQt6.QtWidgets import QMenu, QWidget

from src.shared.i18n.i18n import I18N
from src.shared.ui.style_sheet import C
from . import timeline_layout
from .keyframes_model import (
    CompositeTimelineState, FrameMapper, KeyframePointVisual,
    KeyframesModel, KeyframesTrackVisual, KEYFRAMES_RULER_H,
    KEYFRAMES_ROW_H)

logger = logging.getLogger(__name__)


# Layout constants (plan §3.4)
_RULER_H = KEYFRAMES_RULER_H
_ROW_H = KEYFRAMES_ROW_H
_PLAYHEAD_W = 1
_ORIGIN_X = 0.0  # frame 0 x position is governed by FrameMapper
_PARAM_ROW_ID_FMT = "param:{}:{}"  # AUD-10210: 统一 param 行 row_id 格式（原 3 处重复字面量）
# Work-area (play range) band painted at the very top of the ruler (AE style).
_WORKAREA_H = 5
_WORKAREA_EDGE_W = 6
# Fixed project/composition extent shown below the editable orange work area.
_PROJECT_RANGE_Y = _WORKAREA_H
_PROJECT_RANGE_H = 3
_WHEEL_ZOOM_FACTOR = 1.2
# Hit-test radii (plan §P5)
_KF_HIT_RADIUS = 8       # keyframe diamond pick radius (px)
_RANGE_EDGE_W = 6        # range bar left/right edge drag zone (px)
_SEEK_THROTTLE_MS = 16   # ~60fps playhead drag throttle
_MARQUEE_THRESHOLD = 4   # px moved before an empty-area press becomes a marquee
# T46: snap tolerance in pixels. A drag within this many pixels of a snap
# target snaps to it. 8 px is roughly the keyframe diamond's hit radius,
# so a user aiming at a neighbour keyframe reliably snaps to it.
_SNAP_PX_TOL = 8

_INTERPOLATIONS = (
    ("linear", "keyframes.interp_linear", "Linear"),
    ("bezier", "keyframes.interp_bezier", "Bezier"),
    ("hold", "keyframes.interp_hold", "Hold"),
    ("ease_in", "keyframes.interp_ease_in", "Ease In"),
    ("ease_out", "keyframes.interp_ease_out", "Ease Out"),
    ("ease_in_out", "keyframes.interp_ease_in_out", "Ease In/Out"),
)


class KeyframesTimelineView(QWidget):
    """Right side: ruler + track rows + playhead."""

    vertical_scroll_changed = pyqtSignal(int)
    current_frame_changed = pyqtSignal(int)
    track_changed = pyqtSignal(str, str, list)  # node_id, param_name, keyframes
    selection_changed = pyqtSignal(object)      # ((row_id, frame), ...)
    # Bracket multi-track timeline operations so the host can create one undo
    # record for a drag, paste, delete, interpolation change, or time scale.
    track_batch_about_to_commit = pyqtSignal(str, int)
    track_batch_committed = pyqtSignal(str, int)
    range_changed = pyqtSignal(str, dict)       # node_id, time_range dict
    play_range_changed = pyqtSignal(int, int)   # work-area in/out frames
    display_mode_changed = pyqtSignal(str)      # ruler label mode changed
    expression_edit_requested = pyqtSignal(str, str)

    def __init__(self, model: KeyframesModel, frame_mapper: FrameMapper,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._model = model
        self._fm = frame_mapper
        self._current_frame = 0
        self._scroll_y = 0
        self._dragging_playhead = False
        # AE-style work area (play range). None = whole timeline.
        self._play_range: Optional[Tuple[int, int]] = None
        # Actual composition extent. This clamps the orange work area; the
        # ruler itself may extend farther to expose a longer media source.
        self._composition_range: Optional[Tuple[int, int]] = None
        # MediaPool drops are accepted on the whole visible timeline side as
        # well as the left Layer tree. The panel maps the y coordinate back
        # to the shared row model and owns the actual graph mutation.
        self._media_drop_handler = None
        self.setAcceptDrops(True)
        # Ruler label mode + fps (pushed by the panel via set_display()).
        self._display_mode_str: str = "frame"
        self._fps_val: float = 24.0
        self.setMinimumWidth(300)
        self.setMouseTracking(True)
        # P5: accept focus so Delete/Backspace can remove keyframes.
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.__init_state()

    # ── model / state binding ──

    def refresh(self) -> None:
        self.update()

    def set_current_frame(self, frame: int) -> None:
        if frame != self._current_frame:
            self._current_frame = int(frame)
            self.update()

    def set_vertical_scroll(self, value: int) -> None:
        if value != self._scroll_y:
            self._scroll_y = value
            self.update()

    def set_media_drop_handler(self, handler) -> None:
        """Install ``(mime_data, y) -> bool`` for MediaPool drops."""
        self._media_drop_handler = handler

    @staticmethod
    def _is_media_mime(mime) -> bool:
        if mime is None:
            return False
        return any(mime.hasFormat(name) for name in (
            "application/x-topos-media-pool-assets",
            "application/x-topos-media-pool-asset",
            "application/x-topos-media-asset",
        ))

    def dragEnterEvent(self, event) -> None:  # noqa: N802
        if (self._media_drop_handler is not None
                and self._is_media_mime(event.mimeData())):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event) -> None:  # noqa: N802
        if (self._media_drop_handler is not None
                and self._is_media_mime(event.mimeData())):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragLeaveEvent(self, event) -> None:  # noqa: N802
        event.ignore()

    def dropEvent(self, event) -> None:  # noqa: N802
        handler = self._media_drop_handler
        if handler is None or not self._is_media_mime(event.mimeData()):
            event.ignore()
            return
        pos = event.position().toPoint() if hasattr(event, "position") \
            else event.pos()
        try:
            handled = bool(handler(event.mimeData(), float(pos.y())))
        except Exception:
            # AUD-10201: the handler is host-owned code resolving the MIME
            # payload and mutating the layer stack inside a Qt drop event; it
            # must never propagate into the event loop, but the failure has
            # to be visible or every drop bug degrades to "nothing happened".
            logger.warning("Timeline media drop handler failed",
                           exc_info=True)
            handled = False
        if handled:
            event.acceptProposedAction()
        else:
            event.ignore()

    def set_play_range(self, start: Optional[int], end: Optional[int]) -> None:
        """Set the work-area (play range) band. (None, None) clears it."""
        if start is None or end is None:
            self._play_range = None
        else:
            s, e = int(start), int(end)
            s, e = min(s, e), max(s, e)
            if self._composition_range is not None:
                lo, hi = self._composition_range
                s = max(lo, min(s, hi))
                e = max(s, min(e, hi))
            self._play_range = (s, e)
        self.update()

    def set_composition_range(self, start: int, end: int) -> None:
        """Set the composition extent used to clamp the work area.

        The model still keeps a video bar's real source length for trimming,
        and the ruler can extend to that source length. This extent only
        limits the playback/work-area band to the active composition.
        """
        lo, hi = sorted((int(start), int(end)))
        self._composition_range = (lo, hi)
        if self._play_range is not None:
            play_start, play_end = self._play_range
            play_start = max(lo, min(play_start, hi))
            play_end = max(play_start, min(play_end, hi))
            self._play_range = (play_start, play_end)
        self.update()

    def set_display(self, display_mode: str, fps: float) -> None:
        """Push the ruler display mode + fps from the panel state."""
        mode = str(display_mode) if display_mode else "frame"
        fps_f = float(fps) if fps else 24.0
        changed = (mode != self._display_mode_str or
                   abs(fps_f - self._fps_val) > 1e-6)
        self._display_mode_str = mode
        self._fps_val = fps_f if fps_f > 0 else 24.0
        if changed:
            self.update()

    def play_range(self) -> Optional[Tuple[int, int]]:
        return self._play_range

    def project_range(self) -> Optional[Tuple[int, int]]:
        """Return the fixed project/composition extent shown on the ruler."""
        return self._composition_range

    def fit_time_range(self, state: CompositeTimelineState) -> None:
        """Fit [start_frame, end_frame] into the visible width.

        ``end_frame`` is inclusive (the last frame of the comp clip), so the
        fitted range spans one extra frame to keep the end tick visible.
        """
        start = state.start_frame
        end = max(state.start_frame + 2, state.end_frame + 1)
        w = max(1, self.width())
        ppf = w / float(end - start) if end > start else 4.0
        # Keep frame 0 at origin_x by shifting scroll so start maps to x=0.
        self._fm.set_zoom(ppf)
        self._fm.set_scroll(-start * ppf)
        self.update()

    def fit_range(self, start: int, end: int,
                  state: Optional[CompositeTimelineState] = None) -> None:
        """T27/§3.2.D: fit an arbitrary frame range into the visible width.

        Used by the panel's "fit selection" button to zoom in on the
        selected keyframes. Unlike :meth:`fit_time_range`, this accepts an
        explicit ``[start, end]`` pair (both inclusive). The ``state``
        argument is accepted for forward-compat (future callers may want to
        also clamp to the comp range) but currently unused.
        """
        lo = int(start)
        hi = max(lo + 2, int(end) + 1)  # +1 so the end tick stays visible
        w = max(1, self.width())
        ppf = w / float(hi - lo) if hi > lo else 4.0
        self._fm.set_zoom(ppf)
        self._fm.set_scroll(-lo * ppf)
        self.update()

    def pan_to_frame(self, frame: int) -> None:
        """Center the playhead in the visible area."""
        w = self.width()
        target_x = w / 2.0
        # We want frame_to_x(frame) == target_x → scroll = target_x - frame*ppf
        self._fm.set_scroll(target_x - frame * self._fm.pixels_per_frame)
        self.update()

    # ── painting ──

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        w = self.width()
        h = self.height()

        # Both work areas share the same base tone. Work-area shading is
        # applied separately below, so it remains a meaningful timeline cue.
        p.fillRect(0, 0, w, h, QColor(timeline_layout.resolve_token(
            "left_panel_bg")))

        # Ruler
        self._draw_ruler(p, w)

        # Grid goes BEFORE the rows so lines never cover layer bars/keyframes.
        self._draw_grid(p, w, h)

        # Rows
        rows = self._model.visible_rows()
        for i, row in enumerate(rows):
            y = _RULER_H + i * _ROW_H - self._scroll_y
            if y + _ROW_H < _RULER_H or y > h:
                continue
            track = self._model.track_for(row.row_id)
            self._draw_row(p, row, track, y, w)

        # Dim the track area outside the work area (AE style).
        self._draw_out_of_range_shade(p, w, h)

        # Marquee rubber band (on top of rows, below the playhead).
        if self._marquee_rect is not None:
            x0, y0, x1, y1 = self._marquee_rect
            p.setPen(QPen(QColor(C.ORANGE), 1))
            fill = QColor(C.ORANGE)
            fill.setAlpha(40)
            p.setBrush(fill)
            p.drawRect(int(x0), int(y0), int(x1 - x0), int(y1 - y0))

        # Playhead (drawn last so it overlays everything)
        self._draw_playhead(p, w, h)

    def _draw_ruler(self, p: QPainter, w: int) -> None:
        """Edit-page style ruler: adaptive major/minor ticks + labels.

        Label spacing adapts to zoom (1-2-5 series) so labels never
        overlap; labels show frame numbers or timecode depending on
        ``state.display_mode``.
        """
        p.fillRect(0, 0, w, _RULER_H, QColor(C.BG_DARK))

        # Project extent is fixed by the composition; the orange band below
        # is the user-editable playback/work area.
        self._draw_project_range(p, w)
        self._draw_workarea(p, w)

        minor_step, major_step = self._tick_steps()
        fm = self._fm
        start_frame = int(fm.x_to_frame(-32.0, _ORIGIN_X))
        if start_frame < 0:
            start_frame = 0
        end_frame = int(fm.x_to_frame(w + 32.0, _ORIGIN_X)) + 1

        minor_col = QColor(C.BORDER_DARK)
        major_col = QColor(C.TEXT_HINT)

        # Minor ticks: short marks at the ruler bottom.
        if minor_step < major_step and minor_step * fm.pixels_per_frame >= 4.0:
            p.setPen(QPen(minor_col))
            f = (start_frame // minor_step) * minor_step
            while f <= end_frame:
                if f % major_step != 0:
                    x = fm.frame_to_x(f, _ORIGIN_X)
                    if 0 <= x <= w:
                        p.drawLine(int(x), _RULER_H - 5, int(x), _RULER_H)
                f += minor_step

        # Major ticks + labels.
        font = QFont(p.font())
        font.setPointSize(8)
        p.setFont(font)
        fmetrics = p.fontMetrics()
        label_col = QColor(C.TEXT_HINT)
        f = (start_frame // major_step) * major_step
        while f <= end_frame:
            x = fm.frame_to_x(f, _ORIGIN_X)
            if 0 <= x <= w:
                p.setPen(QPen(major_col))
                p.drawLine(int(x), _RULER_H - 9, int(x), _RULER_H)
                label = self._ruler_label(f)
                lx = int(x) + 3
                if lx < 2:
                    lx = 2
                # Skip labels that would run past the right edge.
                if lx + fmetrics.horizontalAdvance(label) <= w:
                    p.setPen(QPen(label_col))
                    p.drawText(lx, _WORKAREA_H + 1, w - lx,
                               _RULER_H - _WORKAREA_H - 7,
                               Qt.AlignmentFlag.AlignVCenter |
                               Qt.AlignmentFlag.AlignLeft, label)
            f += major_step

        p.setPen(QPen(QColor(C.BORDER_DARK)))
        p.drawLine(0, _RULER_H - 1, w, _RULER_H - 1)

    def _tick_steps(self) -> Tuple[int, int]:
        """Adaptive (minor, major) tick steps in frames (1-2-5 series).

        The major step is the smallest 1/2/5*10^k whose pixel spacing is
        >= the minimum label gap, so labels never crowd or overlap.
        """
        mode = self._display_mode()
        min_px = 78.0 if mode == "timecode" else 56.0
        ppf = max(self._fm.pixels_per_frame, 1e-6)
        target = min_px / ppf
        if target <= 1.0:
            return 1, 1
        exp = math.floor(math.log10(target))
        base = 10.0 ** exp
        major = base * 10.0
        mantissa = 10.0
        for m in (1.0, 2.0, 5.0, 10.0):
            if base * m >= target - 1e-9:
                major = base * m
                mantissa = m
                break
        major_i = max(1, int(round(major)))
        subdiv = 4 if 1.5 < mantissa <= 2.5 else 5
        minor_i = max(1, major_i // subdiv)
        if minor_i >= major_i:
            minor_i = major_i
        return minor_i, major_i

    def _display_mode(self) -> str:
        return self._display_mode_str

    def _ruler_label(self, frame: int) -> str:
        """Major-tick label: frame number or timecode by display mode."""
        mode = self._display_mode()
        if mode == "timecode":
            return self._format_timecode(frame)
        if mode == "seconds":
            fps = self._fps()
            return f"{frame / fps:.2f}s" if fps > 0 else str(frame)
        return str(frame)

    def _fps(self) -> float:
        return self._fps_val if self._fps_val > 0 else 24.0

    def _format_timecode(self, frame: int) -> str:
        fps_i = max(1, int(round(self._fps())))
        total = max(0, int(frame))
        ff = total % fps_i
        ss = (total // fps_i) % 60
        mm = (total // fps_i // 60) % 60
        hh = total // fps_i // 3600
        return f"{hh:02d}:{mm:02d}:{ss:02d}:{ff:02d}"

    def _draw_workarea(self, p: QPainter, w: int) -> None:
        """Paint the play-range band + edge handles at the top of the ruler."""
        if self._play_range is None:
            return
        start, end = self._play_range
        x0 = self._fm.frame_to_x(start, _ORIGIN_X)
        x1 = self._fm.frame_to_x(end + 1, _ORIGIN_X)
        color = QColor(C.ORANGE)
        # Band
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(color)
        p.drawRect(int(max(0, x0)), 0, int(min(w, x1) - max(0, x0)),
                   _WORKAREA_H)
        # Edge handles (slightly taller so they are easy to grab)
        p.setBrush(color.lighter(130))
        if 0 <= x0 <= w:
            p.drawRect(int(x0) - 1, 0, 3, _WORKAREA_H + 3)
        if 0 <= x1 <= w:
            p.drawRect(int(x1) - 2, 0, 3, _WORKAREA_H + 3)

    def _draw_project_range(self, p: QPainter, w: int) -> None:
        """Paint the fixed project range below the orange work area.

        A long source can extend past this guide. That is intentional: the
        guide marks the current composition duration, not the visible media
        extent and not the playback I/O selection.
        """
        if self._composition_range is None:
            return
        start, end = self._composition_range
        x0 = self._fm.frame_to_x(start, _ORIGIN_X)
        x1 = self._fm.frame_to_x(end + 1, _ORIGIN_X)
        if x1 <= 0 or x0 >= w:
            return
        color = QColor(C.CURVE_HUE)
        color.setAlpha(210)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(color)
        left = int(max(0, x0))
        right = int(min(w, x1))
        if right > left:
            p.drawRect(left, _PROJECT_RANGE_Y, right - left,
                       _PROJECT_RANGE_H)
        cap = QColor(C.CURVE_HUE)
        cap.setAlpha(235)
        p.setBrush(cap)
        if 0 <= x0 <= w:
            p.drawRect(int(x0) - 1, _PROJECT_RANGE_Y, 2,
                       _PROJECT_RANGE_H + 2)
        if 0 <= x1 <= w:
            p.drawRect(int(x1) - 1, _PROJECT_RANGE_Y, 2,
                       _PROJECT_RANGE_H + 2)

    def _draw_grid(self, p: QPainter, w: int, h: int) -> None:
        """Keep the track canvas clean; row separators provide the grid."""
        # AE uses the ruler for temporal orientation and horizontal row
        # separators for structure. Vertical guides made the panel read like
        # a spreadsheet and competed with the playhead, so they are omitted.
        del p, w, h

    def _draw_out_of_range_shade(self, p: QPainter, w: int, h: int) -> None:
        """Subtle shade over the track area outside the work area."""
        if self._play_range is None:
            return
        start, end = self._play_range
        x0 = self._fm.frame_to_x(start, _ORIGIN_X)
        x1 = self._fm.frame_to_x(end + 1, _ORIGIN_X)
        p.setPen(Qt.PenStyle.NoPen)
        shade = QColor(0, 0, 0, 70)
        p.setBrush(shade)
        if x0 > 0:
            p.drawRect(0, _RULER_H, int(min(x0, w)), h - _RULER_H)
        if x1 < w:
            p.drawRect(int(max(0, x1)), _RULER_H, int(w - max(0, x1)),
                       h - _RULER_H)

    def _draw_row(self, p: QPainter, row, track: Optional[KeyframesTrackVisual],
                  y: int, w: int) -> None:
        rect_h = _ROW_H
        # alternating row background for readability
        if row.row_type in ("node", "layer"):
            p.fillRect(0, y, w, rect_h,
                       QColor(timeline_layout.resolve_token("layer_row_bg")))
        if row.selected:
            p.fillRect(0, y, w, rect_h, QColor(C.BG_ACTIVE))

        if track is None:
            return

        # Range bar (node rows)
        if track.range_bar is not None:
            self._draw_range_bar(p, track.range_bar, y, w)

        # Keyframe diamonds (param rows)
        if track.keyframes:
            self._draw_keyframes(p, track, y)

        # Row separator
        p.setPen(QPen(QColor(timeline_layout.resolve_token("grid_minor"))))
        p.drawLine(0, y + rect_h - 1, w, y + rect_h - 1)

    def _draw_range_bar(self, p: QPainter, bar, y: int, w: int) -> None:
        # Keep the visual edge on the same coordinate used by the drag hit
        # test: the stored end frame is the inclusive right-hand handle.
        x0, x1 = self._range_bar_x_bounds(bar)
        if x1 < 0 or x0 > w:
            return
        x0 = max(0, x0)
        x1 = min(w, x1)
        if x1 <= x0:
            return
        bar_h = _ROW_H - 6
        bar_y = y + 3
        color = QColor(bar.color)
        # Default (whole-comp) layer bars render dimmer than explicit ranges.
        if getattr(bar, "default", False):
            color = color.darker(130)
        p.setBrush(color)
        p.setPen(QPen(color.lighter(140)))
        p.drawRect(int(x0), bar_y, int(x1 - x0), bar_h)
        bar_w = int(x1 - x0)
        if bar_w < 34:
            # At low zoom there is no safe space for text. Painting a clipped
            # number here is worse than leaving the bar clean.
            return

        p.setPen(QPen(QColor(C.TEXT_SECONDARY)))
        font = p.font()
        font.setPointSize(8)
        p.setFont(font)
        flags = (Qt.AlignmentFlag.AlignVCenter |
                 Qt.AlignmentFlag.AlignLeft |
                 Qt.TextFlag.TextSingleLine)
        edge_label_w = min(52.0, max(1.0, bar_w / 2 - 6.0))
        p.drawText(QRectF(x0 + 4, bar_y, edge_label_w, bar_h),
                   flags, str(bar.start_frame))
        flags = (Qt.AlignmentFlag.AlignVCenter |
                 Qt.AlignmentFlag.AlignRight |
                 Qt.TextFlag.TextSingleLine)
        p.drawText(QRectF(x1 - 4 - edge_label_w, bar_y, edge_label_w,
                          bar_h),
                   flags, str(bar.end_frame))

        # Layer name is only drawn when a dedicated center lane exists. This
        # prevents the name from colliding with either endpoint label.
        label = str(getattr(bar, "label", "") or "")
        if bar_w >= 150 and label:
            p.setPen(QPen(QColor(C.TEXT_LABEL)))
            center_rect = QRectF(x0 + 58, bar_y, bar_w - 116, bar_h)
            label = p.fontMetrics().elidedText(
                label, Qt.TextElideMode.ElideMiddle,
                max(1, int(center_rect.width())))
            p.drawText(center_rect, Qt.AlignmentFlag.AlignCenter |
                       Qt.TextFlag.TextSingleLine, label)

    def _draw_keyframes(self, p: QPainter, track: KeyframesTrackVisual,
                        y: int) -> None:
        color = QColor(track.color)
        cy = y + _ROW_H // 2
        for kf in track.keyframes:
            x = self._fm.frame_to_x(kf.frame, _ORIGIN_X)
            if x < -10 or x > self.width() + 10:
                continue
            # The playhead is an editing context: a point directly under it
            # uses the same orange as the AE property diamond, even when the
            # point is not part of the multi-selection.
            if kf.selected or kf.frame == self._current_frame:
                kf_color = QColor(C.KEYFRAME_SELECTED)
            else:
                kf_color = color
            p.setBrush(kf_color)
            p.setPen(QPen(kf_color.darker(160)))
            # diamond
            size = 5 if kf.frame == self._current_frame else 4
            poly = QPolygonF([
                QPointF(x, cy - size), QPointF(x + size, cy),
                QPointF(x, cy + size), QPointF(x - size, cy),
            ])
            p.drawPolygon(poly)

    def _draw_playhead(self, p: QPainter, w: int, h: int) -> None:
        x = self._fm.frame_to_x(self._current_frame, _ORIGIN_X)
        if x < -5 or x > w + 5:
            return
        p.setPen(QPen(QColor(C.KEYFRAME_PLAYHEAD), _PLAYHEAD_W))
        p.drawLine(int(x), 0, int(x), h)
        # cap triangle at the top
        p.setBrush(QColor(C.KEYFRAME_PLAYHEAD))
        p.setPen(Qt.PenStyle.NoPen)
        cap = QPolygonF([
            QPointF(x - 5, 0), QPointF(x + 5, 0), QPointF(x, 6),
        ])
        p.drawPolygon(cap)

    # ── interaction (P5) ──

    def __init_state(self) -> None:
        # Lazy-init drag state so the constructor stays readable.
        self._drag_mode: str = "none"      # none|playhead|keyframe|range_edge|
                                           # range_body|pan|workarea_*|marquee
        self._drag_row_id: Optional[str] = None
        self._drag_kf_frame: Optional[int] = None
        self._drag_edge: Optional[str] = None  # "left" | "right"
        self._drag_last_emit = 0.0
        self._drag_offset_frames: int = 0
        # Middle-button pan state.
        self._pan_press_x: float = 0.0
        self._pan_press_scroll: float = 0.0
        # Work-area body drag offset (frames between cursor and range start).
        self._workarea_press_offset: int = 0
        # Multi-keyframe drag: snapshot {row_id: [(frame, selected), ...]}.
        self._multi_drag_snapshot: Optional[dict] = None
        self._multi_drag_press_frame: int = 0
        # Rows whose keyframes changed during the current drag.
        self._drag_affected_rows: set = set()
        # Marquee (rubber-band) selection state.
        self._marquee_rect: Optional[Tuple[float, float, float, float]] = None
        self._marquee_press: Optional[Tuple[float, float]] = None
        self._marquee_additive: bool = False
        # In-panel keyframe clipboard: [{row_id, rel_frame, data}, ...].
        self._kf_clipboard: List[dict] = []
        # T46: snap toggle. When True, dragged keyframes (and the playhead)
        # snap to integer frames, neighbouring keyframes and the work-area
        # edges within _SNAP_PX_TOL pixels. Off by default so the legacy
        # drag-to-any-frame behaviour is preserved until the user opts in.
        self._snap_enabled: bool = False
        # T42: Alt/Option-drag copy. Set on press, consumed by the keyframe
        # drag handler so the original keyframe survives the drag.
        self._drag_copy: bool = False
        # AUD-10203: T42 duplication is deferred until the drag actually
        # moves. Alt+click without a drag must not leave two keyframes on
        # the same frame (one-keyframe-per-frame invariant).
        self._drag_copy_pending: bool = False
        # AUD-10200: (start, end) of the range bar when a range drag begins.
        # A release without movement commits nothing — a plain click must
        # not freeze a default layer bar into an explicit time_range.
        self._range_press_bounds: Optional[Tuple[int, int]] = None

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.MiddleButton:
            # Middle-button drag pans the time axis (AE/NLE convention).
            self._drag_mode = "pan"
            self._pan_press_x = float(event.position().x())
            self._pan_press_scroll = self._fm.scroll_offset
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            return
        if event.button() == Qt.MouseButton.LeftButton:
            pos = event.position()
            x, y = float(pos.x()), float(pos.y())
            # 0. Work-area band (top of the ruler) — in/out handles first.
            wa_hit = self._hit_workarea(x, y)
            if wa_hit is not None:
                self._start_workarea_drag(wa_hit, x)
                return
            # 1. Playhead (ruler click) — highest priority in the ruler band
            if y <= _RULER_H:
                self._start_drag("playhead", x)
                return
            # 2. Keyframe hit-test (Shift/Ctrl = additive selection).
            hit = self._hit_keyframe(x, y)
            if hit is not None:
                row_id, kf_frame = hit
                additive = bool(event.modifiers() &
                                (Qt.KeyboardModifier.ShiftModifier |
                                 Qt.KeyboardModifier.ControlModifier))
                # T42: Alt/Option + drag on a keyframe duplicates the
                # selection in place and drags the copies, leaving the
                # originals behind. Detected here so the modifier doesn't
                # also toggle additive selection (Alt is its own gesture).
                self._drag_copy = bool(
                    event.modifiers() & Qt.KeyboardModifier.AltModifier)
                track = self._model.track_for(row_id)
                already_sel = bool(track) and any(
                    kf.selected for kf in track.keyframes
                    if kf.frame == kf_frame)
                # Pressing an already-selected keyframe keeps the whole
                # selection (AE-style group drag); otherwise reselect.
                if not (already_sel and not additive):
                    self._select_keyframe(row_id, kf_frame,
                                          additive=additive)
                self._start_multi_keyframe_drag(x, row_id)
                return
            # 3. Range bar edge / body
            rbar_hit = self._hit_range_bar(x, y)
            if rbar_hit is not None:
                row_id, edge = rbar_hit
                # AUD-10200: remember the bar's bounds at press time so the
                # release handler can tell a plain click from a real drag.
                press_track = self._model.track_for(row_id)
                if press_track is not None and press_track.range_bar is not None:
                    self._range_press_bounds = (
                        press_track.range_bar.start_frame,
                        press_track.range_bar.end_frame)
                self._start_drag("range_" + ("edge" if edge else "body"), x,
                                 row_id=row_id, edge=edge)
                return
            # 4. Empty track area → pending marquee: a plain click still
            #    seeks the playhead; moving past the threshold rubber-bands.
            if y > _RULER_H:
                self._drag_mode = "marquee"
                self._marquee_press = (x, y)
                self._marquee_rect = None
                self._marquee_additive = bool(
                    event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
                return
            # AUD-10210: y<=_RULER_H 与 y>_RULER_H 两分支均已 return，
            # 此处 fallthrough 的 playhead 拖拽不可达，按死代码移除。

    def mouseMoveEvent(self, event) -> None:
        if self._drag_mode == "none":
            self._update_hover_cursor(float(event.position().x()),
                                      float(event.position().y()))
            return
        x = float(event.position().x())
        if self._drag_mode == "playhead":
            self._do_playhead_drag(x)
        elif self._drag_mode == "keyframe":
            self._do_multi_keyframe_drag(x)
        elif self._drag_mode == "marquee":
            self._do_marquee(float(event.position().x()),
                             float(event.position().y()))
        elif self._drag_mode == "range_edge":
            self._do_range_edge_drag(x)
        elif self._drag_mode == "range_body":
            self._do_range_body_drag(x)
        elif self._drag_mode == "pan":
            self._fm.set_scroll(self._pan_press_scroll +
                                (x - self._pan_press_x))
            self.update()
        elif self._drag_mode.startswith("workarea"):
            self._do_workarea_drag(self._drag_mode, x)

    def mouseReleaseEvent(self, event) -> None:
        # On release of a keyframe/range drag, commit the edit so CompositePage
        # pushes undo + re-renders. Playhead drags don't edit tracks.
        if self._drag_mode == "marquee":
            self._finish_marquee(event)
        elif self._drag_mode in ("keyframe", "range_edge", "range_body"):
            self._commit_drag_edit()
        elif self._drag_mode == "playhead":
            # T48: the playhead drag throttles emits to ~60fps (see
            # _do_playhead_drag). The last mouseMoveEvent can land inside
            # the throttle window and be dropped — so on release we force
            # one final emit at the release position. This guarantees the
            # settled frame is published exactly once at the end of the
            # gesture (plan §3.5: "鼠标释放后只发布一次当前帧变化"),
            # regardless of where in the throttle cycle the user let go.
            self._do_playhead_drag(float(event.position().x()),
                                   force=True)
        if self._drag_mode == "pan":
            self.unsetCursor()
        self._drag_mode = "none"
        self._drag_row_id = None
        # T42: clear the copy-drag flag at the end of every gesture so a
        # stale Alt-press doesn't carry into the next plain drag.
        self._drag_copy = False
        # AUD-10203: an Alt-press that never became a drag has nothing
        # duplicated or committed — just drop the pending flag.
        self._drag_copy_pending = False
        # AUD-10200: drop the press snapshot with the gesture it belonged to.
        self._range_press_bounds = None
        self._drag_kf_frame = None
        self._drag_edge = None
        self._multi_drag_snapshot = None
        self._marquee_press = None
        self._marquee_rect = None

    def mouseDoubleClickEvent(self, event) -> None:
        """Double-click on a param track adds a keyframe at that frame."""
        if event.button() != Qt.MouseButton.LeftButton:
            return
        pos = event.position()
        hit = self._hit_param_row(float(pos.y()))
        if hit is None:
            return
        row_id = hit
        row = self._row_for_id(hit)
        # AUD-10202: P9 — locked rows are not editable; the double-click add
        # path must respect the same lock the press hit-tests enforce.
        if row is None or row.row_type != "param" or getattr(row, "locked", False):
            return
        frame = int(round(self._fm.x_to_frame(float(pos.x()), _ORIGIN_X)))
        # AUD-10202: double-clicking an existing diamond is a no-op, not a
        # re-emit of the unchanged track (which pushed a redundant undo
        # record and a pointless panel rebuild).
        track = self._model.track_for(row_id)
        if track is not None and any(
                kf.frame == frame for kf in track.keyframes):
            return
        self._add_keyframe(row.node_id, row.param_name, frame)

    def wheelEvent(self, event) -> None:
        """Wheel navigation on the track area.

        - Plain wheel / Ctrl+wheel: zoom the time axis anchored at the
          cursor (frame-by-frame editing at high zoom).
        - Shift + wheel: horizontal pan.
        - Alt + wheel: ignore → bubbles up to the panel (vertical scroll).
        """
        delta = event.angleDelta().y()
        if delta == 0:
            super().wheelEvent(event)
            return
        mods = event.modifiers()
        if mods & Qt.KeyboardModifier.AltModifier:
            event.ignore()  # panel handles vertical scroll
            return
        if mods & Qt.KeyboardModifier.ShiftModifier:
            self._fm.set_scroll(self._fm.scroll_offset + delta * 0.5)
            self.update()
            return
        # Zoom anchored at the cursor: the frame under the mouse stays put.
        x = float(event.position().x())
        anchor_frame = self._fm.x_to_frame(x, _ORIGIN_X)
        factor = _WHEEL_ZOOM_FACTOR if delta > 0 else (1.0 / _WHEEL_ZOOM_FACTOR)
        new_ppf = self._fm.pixels_per_frame * factor
        self._fm.set_zoom(new_ppf)
        self._fm.set_scroll(x - anchor_frame * self._fm.pixels_per_frame)
        self.update()

    def _update_hover_cursor(self, x: float, y: float) -> None:
        """Show resize/pan cursors when hovering draggable regions."""
        if self._hit_workarea(x, y) in ("in", "out"):
            self.setCursor(Qt.CursorShape.SplitHCursor)
            return
        idx = self._row_index_at_y(y)
        if idx >= 0:
            hit = self._hit_range_bar(x, y)
            if hit is not None and hit[1] in ("left", "right"):
                self.setCursor(Qt.CursorShape.SplitHCursor)
                return
        self.unsetCursor()

    def contextMenuEvent(self, event) -> None:
        """Right-click: keyframe editing + interpolation + ruler mode."""
        has_sel = bool(self.selected_keyframes())
        menu = QMenu(self)

        if has_sel:
            act_copy = menu.addAction(
                I18N.tr("keyframes.copy_kf", "Copy keyframes"))
            act_copy.triggered.connect(self.copy_selected_keyframes)
        act_paste = menu.addAction(
            I18N.tr("keyframes.paste_kf", "Paste keyframes at playhead"))
        act_paste.setEnabled(self.has_clipboard())
        if has_sel:
            act_del = menu.addAction(
                I18N.tr("keyframes.delete_kf", "Delete keyframes"))
            act_del.triggered.connect(self._delete_selected_keyframes)
            menu.addSeparator()
            # Interpolation submenu — keep this list aligned with the
            # KeyframeEditorWidget so both animation surfaces expose the same
            # interpolation modes.
            interp_menu = menu.addMenu(
                I18N.tr("keyframes.interpolation", "Interpolation"))
            current = self.selected_interpolation()
            interp_actions = []
            for mode, i18n_key, fallback in _INTERPOLATIONS:
                act = interp_menu.addAction(I18N.tr(i18n_key, fallback))
                act.setCheckable(True)
                act.setChecked(current == mode)
                interp_actions.append((act, mode))
            scale_menu = menu.addMenu(
                I18N.tr("keyframes.scale_selected", "Scale selected in time"))
            for factor, label in (
                    (0.5, I18N.tr("keyframes.scale_half", "Compress 0.5×")),
                    (1.5, I18N.tr("keyframes.scale_one_half", "Stretch 1.5×")),
                    (2.0, I18N.tr("keyframes.scale_double", "Stretch 2×"))):
                action = scale_menu.addAction(label)
                action.triggered.connect(
                    lambda _checked=False, value=factor:
                    self.scale_selected_keyframes(value))
            menu.addSeparator()

        mode = self._display_mode()
        act_frame = menu.addAction(
            I18N.tr("keyframes.ruler_frames", "Frame numbers"))
        act_frame.setCheckable(True)
        act_frame.setChecked(mode in ("frame", "time", "seconds"))
        act_tc = menu.addAction(
            I18N.tr("keyframes.ruler_timecode", "Timecode"))
        act_tc.setCheckable(True)
        act_tc.setChecked(mode == "timecode")

        chosen = menu.exec(event.globalPos())
        if chosen is None:
            return
        if chosen is act_frame:
            self._set_display_mode("frame")
        elif chosen is act_tc:
            self._set_display_mode("timecode")
        elif chosen is act_paste:
            self.paste_keyframes()
        elif has_sel:
            for act, imode in interp_actions:
                if chosen is act:
                    self.set_selected_interpolation(imode)
                    break

    def _set_display_mode(self, mode: str) -> None:
        if mode == self._display_mode_str:
            return
        self._display_mode_str = mode
        self.display_mode_changed.emit(mode)
        self.update()

    def keyPressEvent(self, event) -> None:
        """P9: keyboard navigation.

        Delete/Backspace → remove selected keyframes.
        Ctrl+C / Ctrl+V  → copy / paste selected keyframes.
        Ctrl+A           → select all keyframes.
        Left/Right       → step playhead by 1 frame (Shift = 10 frames).
        Home/End         → jump to first / last keyframe.
        """
        key = event.key()
        mods = event.modifiers()
        if mods & Qt.KeyboardModifier.ControlModifier:
            if key == Qt.Key.Key_C:
                self.copy_selected_keyframes()
                return
            if key == Qt.Key.Key_V:
                self.paste_keyframes()
                return
            if key == Qt.Key.Key_A:
                self.select_all_keyframes()
                return
        if key in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            if self._delete_selected_keyframes():
                return
            super().keyPressEvent(event)
            return
        step = 1
        if mods & Qt.KeyboardModifier.ShiftModifier:
            step = 10
        if key == Qt.Key.Key_Left:
            new_frame = self._current_frame - step
            self.set_current_frame(new_frame)
            self.current_frame_changed.emit(new_frame)
            return
        if key == Qt.Key.Key_Right:
            new_frame = self._current_frame + step
            self.set_current_frame(new_frame)
            self.current_frame_changed.emit(new_frame)
            return
        if key == Qt.Key.Key_Home:
            self._goto_boundary(-1)
            return
        if key == Qt.Key.Key_End:
            self._goto_boundary(+1)
            return
        super().keyPressEvent(event)

    def _goto_boundary(self, direction: int) -> None:
        """Jump the playhead to the first/last keyframe across all tracks."""
        frames = []
        for track in self._model.tracks().values():
            for kf in track.keyframes:
                frames.append(kf.frame)
        if not frames:
            return
        target = min(frames) if direction < 0 else max(frames)
        self.set_current_frame(target)
        self.current_frame_changed.emit(target)

    # ── drag orchestration ──

    def _start_drag(self, mode: str, x: float, *,
                    row_id: Optional[str] = None,
                    kf_frame: Optional[int] = None,
                    edge: Optional[str] = None) -> None:
        self._drag_mode = mode
        self._drag_row_id = row_id
        self._drag_kf_frame = kf_frame
        self._drag_edge = edge
        if mode == "playhead":
            self._do_playhead_drag(x)

    def _do_playhead_drag(self, x: float, force: bool = False) -> None:
        """Advance the playhead to the cursor's frame.

        Emits are throttled to ~60fps (``_SEEK_THROTTLE_MS``) so a fast drag
        doesn't flood CompositePage with re-render requests. Pass
        ``force=True`` to bypass the throttle — used on release (T48) so the
        settled frame is always published exactly once at the end of the
        gesture, even if the final mouseMoveEvent landed inside the throttle
        window.
        """
        import time
        now = time.monotonic()
        if not force and now - self._drag_last_emit < _SEEK_THROTTLE_MS / 1000.0:
            return
        self._drag_last_emit = now
        frame = int(round(self._fm.x_to_frame(x, _ORIGIN_X)))
        # T46: snap the playhead when the toggle is on. Snapping to other
        # keyframes makes it easy to land exactly on a keyframe for editing;
        # snapping to the work-area edges makes it easy to set the playhead
        # exactly at In/Out.
        if self._snap_enabled:
            frame = self._snap_frame(frame, row_id=None)
        self.set_current_frame(frame)
        self.current_frame_changed.emit(frame)

    # ── T46: snap ──

    def set_snap_enabled(self, enabled: bool) -> None:
        """Enable/disable drag snapping (T46).

        When enabled, dragged keyframes and the playhead snap to:
          * integer frames (always — rounding already does this);
          * the nearest keyframe on any visible track, within
            ``_SNAP_PX_TOL`` pixels;
          * the work-area In/Out edges, within ``_SNAP_PX_TOL`` pixels.

        The snap math is pure (no widget state beyond the model), so it's
        unit-testable without a render. The panel's snap toggle (T27)
        forwards through here.
        """
        self._snap_enabled = bool(enabled)

    @property
    def snap_enabled(self) -> bool:
        return self._snap_enabled

    def _snap_frame(self, frame: int,
                    row_id: Optional[str] = None) -> int:
        """Apply snap targets to a candidate frame.

        ``row_id`` (optional) is the dragged keyframe's own row; its own
        keyframes are excluded from the snap candidates so a keyframe
        doesn't snap to itself or its sibling on the same track (you want
        to snap to *other* tracks' keyframes when aligning).

        Returns the (possibly adjusted) frame. When snap is disabled, or no
        target is within tolerance, returns ``frame`` unchanged.
        """
        if not self._snap_enabled:
            return int(frame)
        # Pixel tolerance → frame tolerance. At least 1 frame so a very
        # zoomed-out view (small ppf) still snaps when the user is clearly
        # aiming at the same frame as a target.
        px_per_frame = max(0.05, self._fm.pixels_per_frame)
        frame_tol = max(1, int(round(_SNAP_PX_TOL / px_per_frame)))
        candidates: List[int] = []
        # Neighbouring keyframes (excluding the dragged row's own).
        for rid, track in self._model.tracks().items():
            if rid == row_id:
                continue
            for kf in track.keyframes:
                candidates.append(int(kf.frame))
        # Work-area edges.
        play_range = self.play_range()
        if play_range is not None:
            candidates.append(int(play_range[0]))
            candidates.append(int(play_range[1]))
        if not candidates:
            return int(frame)
        # Find the closest candidate within tolerance.
        best = None
        best_dist = frame_tol + 1
        for c in candidates:
            dist = abs(c - frame)
            if dist <= frame_tol and dist < best_dist:
                best = c
                best_dist = dist
        return int(best) if best is not None else int(frame)

    def _start_multi_keyframe_drag(self, x: float, grabbed_row_id: str) -> None:
        """Begin a drag that moves every selected keyframe together.

        A snapshot of the original frames is taken so each move recomputes
        positions from the press point (no accumulated rounding drift).

        T42: when ``_drag_copy`` is set (Alt/Option was held at press time),
        each selected keyframe is duplicated in place first; the originals
        are deselected and the copies become the drag targets. The originals
        stay behind, so releasing the drag leaves both the source and the
        moved copies in place — one undo step.

        AUD-10203: the duplication itself is deferred to the first real
        move (see :meth:`_do_multi_keyframe_drag`). Duplicating on press
        meant a plain Alt+click — press, no drag, release — committed two
        keyframes on the same frame, breaking the one-keyframe-per-frame
        invariant.
        """
        self._drag_mode = "keyframe"
        self._drag_row_id = grabbed_row_id
        self._drag_affected_rows = set()
        self._multi_drag_press_frame = int(round(
            self._fm.x_to_frame(x, _ORIGIN_X)))
        # T42/AUD-10203: mark the copy intent; the copies are only created
        # once the user actually drags past the current frame.
        self._drag_copy_pending = self._drag_copy
        if self._drag_copy_pending:
            self._drag_copy = False
        snapshot: dict = {}
        for row_id, track in self._model.tracks().items():
            if any(kf.selected for kf in track.keyframes):
                snapshot[row_id] = [(kf.frame, True) if kf.selected
                                    else (kf.frame, False)
                                    for kf in track.keyframes]
        self._multi_drag_snapshot = snapshot

    def _duplicate_selected_in_place(self) -> None:
        """T42 helper: copy every selected keyframe in place.

        For each track that has any selected keyframe, append a copy of
        each selected point at the same frame with the same value/interp/
        extra fields, then flip selection so only the copies remain
        selected. The originals become ordinary (deselected) keyframes.

        The duplicates are appended after the originals and the list is
        re-sorted by frame so painting stays ordered. Because the copy
        lands on the *same* frame as the original, the drag's first move
        must shift it elsewhere — the conflict guard naturally enforces
        this (a no-delta move is a no-op, and any real delta tries to put
        the copy on a free frame).
        """
        for row_id, track in self._model.tracks().items():
            selected = [kf for kf in track.keyframes if kf.selected]
            if not selected:
                continue
            # Deselect originals; the copies inherit selection.
            for kf in track.keyframes:
                kf.selected = False
            for src in selected:
                dup = KeyframePointVisual(
                    frame=src.frame,
                    value=src.value,
                    interpolation=src.interpolation,
                    selected=True,
                    extra=dict(src.extra),
                )
                track.keyframes.append(dup)
            track.keyframes.sort(key=lambda k: k.frame)
            self._drag_affected_rows.add(row_id)

    def _do_multi_keyframe_drag(self, x: float) -> None:
        """Move all selected keyframes by the cursor delta (snap to frames).

        Rules (mirror the single-kf guard): frames clamp to >= 0 and a move
        is refused when a non-moving keyframe already occupies a target
        frame — otherwise add_keyframe would silently destroy a neighbour.

        T46: when snap is enabled, the cursor frame snaps to neighbouring
        keyframes / work-area edges before the delta is computed, so the
        whole selection lands on the snapped offset (not just the grabbed
        keyframe).
        """
        if not self._multi_drag_snapshot:
            return
        cur_frame = int(round(self._fm.x_to_frame(x, _ORIGIN_X)))
        # T46: snap the cursor frame to neighbours / work-area edges. The
        # dragged row's own keyframes are excluded so a keyframe doesn't
        # snap to itself.
        if self._snap_enabled:
            cur_frame = self._snap_frame(
                cur_frame, row_id=self._drag_row_id)
        delta = cur_frame - self._multi_drag_press_frame
        if delta == 0:
            return
        if self._drag_copy_pending:
            # AUD-10203: the gesture became a real drag — NOW materialize the
            # T42 copies and rebuild the snapshot from the post-duplication
            # state (copies selected, originals as static obstacles), exactly
            # as the old press-time duplication did.
            self._drag_copy_pending = False
            self._duplicate_selected_in_place()
            snapshot = {}
            for row_id, track in self._model.tracks().items():
                if any(kf.selected for kf in track.keyframes):
                    snapshot[row_id] = [
                        (kf.frame, True) if kf.selected else (kf.frame, False)
                        for kf in track.keyframes]
            self._multi_drag_snapshot = snapshot
        # Phase 1: validate every track first (all-or-nothing per move).
        moves: dict = {}
        for row_id, entries in self._multi_drag_snapshot.items():
            track = self._model.track_for(row_id)
            if track is None:
                continue
            sel_frames = [f for f, sel in entries if sel]
            new_frames = [max(0, f + delta) for f in sel_frames]
            # No-op rows can be skipped.
            if new_frames == sel_frames:
                continue
            static = {f for f, sel in entries if not sel}
            if any(nf in static or
                   (nf not in sel_frames and
                    any(kf.frame == nf for kf in track.keyframes))
                   for nf in new_frames):
                return  # conflict somewhere → refuse the whole move
            moves[row_id] = dict(zip(sel_frames, new_frames))
        if not moves:
            return
        # Phase 2: apply.
        for row_id, mapping in moves.items():
            track = self._model.track_for(row_id)
            if track is None:
                continue
            for kf in track.keyframes:
                if kf.selected and kf.frame in mapping:
                    kf.frame = mapping[kf.frame]
            track.keyframes.sort(key=lambda k: k.frame)
            self._drag_affected_rows.add(row_id)
        self._multi_drag_press_frame = cur_frame
        # Keep the snapshot in sync with the new positions.
        for row_id, entries in self._multi_drag_snapshot.items():
            mapping = moves.get(row_id)
            if mapping is None:
                continue
            self._multi_drag_snapshot[row_id] = [
                (mapping.get(f, f), sel) for f, sel in entries]
        self.update()

    def _do_range_edge_drag(self, x: float) -> None:
        if self._drag_row_id is None:
            return
        track = self._model.track_for(self._drag_row_id)
        if track is None or track.range_bar is None:
            return
        new_frame = int(round(self._fm.x_to_frame(x, _ORIGIN_X)))
        # Video media bars are clamped to the source length [0, total-1].
        media_end = track.source_total - 1 if track.source_total > 0 else None
        if self._drag_edge == "left":
            start = min(new_frame, track.range_bar.end_frame - 1)
            track.range_bar.start_frame = max(0, start)
        else:
            end = max(new_frame, track.range_bar.start_frame + 1)
            if media_end is not None:
                end = min(end, media_end)
            track.range_bar.end_frame = end
        self.update()

    def _do_range_body_drag(self, x: float) -> None:
        # Body drag moves the whole range; tracked via offset from press frame.
        if self._drag_row_id is None:
            return
        track = self._model.track_for(self._drag_row_id)
        if track is None or track.range_bar is None:
            return
        new_frame = int(round(self._fm.x_to_frame(x, _ORIGIN_X)))
        if not hasattr(self, "_range_press_frame"):
            self._range_press_frame = new_frame
            self._range_press_start = track.range_bar.start_frame
            self._range_press_end = track.range_bar.end_frame
        delta = new_frame - self._range_press_frame
        length = self._range_press_end - self._range_press_start
        # Video media bars cannot slide outside the source length.
        if track.source_total > 0:
            delta = max(-self._range_press_start, delta)
            delta = min(track.source_total - 1 - self._range_press_end, delta)
        track.range_bar.start_frame = self._range_press_start + delta
        track.range_bar.end_frame = track.range_bar.start_frame + length
        self.update()

    def _commit_drag_edit(self) -> None:
        """Emit track_changed so CompositePage writes the node + pushes undo."""
        if self._drag_row_id is None:
            return
        row = self._row_for_id(self._drag_row_id)
        if row is None:
            return
        track = self._model.track_for(self._drag_row_id)
        if track is None:
            return
        if self._drag_mode in ("range_edge", "range_body") and track.range_bar:
            # AUD-10200: a press+release without an actual bar change is a
            # plain click, not an edit. Committing it anyway froze the AE
            # default (whole-comp) layer bar into an explicit time_range and
            # pushed a pointless undo record.
            press_bounds = self._range_press_bounds
            self._range_press_bounds = None
            if press_bounds is not None and press_bounds == (
                    track.range_bar.start_frame, track.range_bar.end_frame):
                return
            # Range edits go to node.time_range via CompositePage slot.
            # enabled=True turns AE-style default layer bars into explicit
            # time ranges; CompositePage merges into any existing dict.
            # The edit kind lets CompositePage apply NLE trim semantics to
            # video source params (source_in/out).
            if self._drag_mode == "range_edge":
                edit = "trim_in" if self._drag_edge == "left" else "trim_out"
            else:
                edit = "slide"
            self.range_changed.emit(row.node_id, {
                "comp_start": track.range_bar.start_frame,
                "comp_end": track.range_bar.end_frame,
                "enabled": True,
                "edit": edit,
            })
            return
        if self._drag_mode == "keyframe":
            # P1-1: preserve handle/tangent fields via to_keyframe_dict().
            # Multi-drag may touch several rows — commit every affected one.
            # Rows that never moved emit nothing (no pointless undo record).
            #
            # Snapshot FIRST, emit after: each track_changed emit makes the
            # page write the node and synchronously rebuild this panel's
            # model from the nodes — mid-loop the not-yet-committed rows
            # would then read back their old positions and only the first
            # row's move would survive.
            pending = []
            for row_id in sorted(self._drag_affected_rows):
                row = self._row_for_id(row_id)
                track = self._model.track_for(row_id)
                if row is None or row.row_type != "param" or track is None:
                    continue
                kfs = [kf.to_keyframe_dict() for kf in track.keyframes]
                pending.append((row.node_id, row.param_name, kfs))
            self._drag_affected_rows = set()
            self._emit_track_batch("drag", pending)
        # Clean up body-drag press cache
        for attr in ("_range_press_frame", "_range_press_start", "_range_press_end"):
            if hasattr(self, attr):
                delattr(self, attr)

    # ── work area (play range) interaction ──

    def _hit_workarea(self, x: float, y: float) -> Optional[str]:
        """Hit-test the work-area band: 'in' | 'out' | 'body' | None."""
        if self._play_range is None or y > _WORKAREA_H + 3:
            return None
        start, end = self._play_range
        x0 = self._fm.frame_to_x(start, _ORIGIN_X)
        x1 = self._fm.frame_to_x(end + 1, _ORIGIN_X)
        # The strip immediately below the orange band is the fixed project
        # guide. Only the taller orange edge handles remain clickable there;
        # clicking its body must not accidentally edit the playback range.
        if y > _WORKAREA_H and abs(x - x0) > _WORKAREA_EDGE_W \
                and abs(x - x1) > _WORKAREA_EDGE_W:
            return None
        if abs(x - x0) <= _WORKAREA_EDGE_W:
            return "in"
        if abs(x - x1) <= _WORKAREA_EDGE_W:
            return "out"
        if x0 <= x <= x1:
            return "body"
        return None

    def _start_workarea_drag(self, hit: str, x: float) -> None:
        if self._play_range is None:
            return
        self._drag_mode = f"workarea_{hit}"
        frame = int(round(self._fm.x_to_frame(x, _ORIGIN_X)))
        self._workarea_press_offset = frame - self._play_range[0]

    def _do_workarea_drag(self, mode: str, x: float) -> None:
        if self._play_range is None:
            return
        start, end = self._play_range
        frame = int(round(self._fm.x_to_frame(x, _ORIGIN_X)))
        if mode == "workarea_in":
            start = max(0, min(frame, end))
        elif mode == "workarea_out":
            end = max(start, frame)
        elif mode == "workarea_body":
            # Move the whole range keeping its length.
            length = end - start
            start = max(0, frame - self._workarea_press_offset)
            end = start + length
        if self._composition_range is not None:
            comp_start, comp_end = self._composition_range
            if mode == "workarea_in":
                start = max(comp_start, min(start, comp_end))
            elif mode == "workarea_out":
                end = max(start, min(end, comp_end))
            else:
                length = min(end - start, comp_end - comp_start)
                start = max(comp_start, min(start, comp_end - length))
                end = start + length
        if (start, end) == self._play_range:
            return
        self._play_range = (start, end)
        self.update()
        self.play_range_changed.emit(start, end)

    # ── hit testing ──

    def _row_index_at_y(self, y: float) -> int:
        """Visible-row index for a widget y (track area only)."""
        if y < _RULER_H:
            return -1
        idx = int((y - _RULER_H + self._scroll_y) // _ROW_H)
        rows = self._model.visible_rows()
        if 0 <= idx < len(rows):
            return idx
        return -1

    def _row_for_id(self, row_id: str):
        """Resolve a row id to its :class:`KeyframesRow`, or None.

        AUD-10204: visible rows are searched first, but collapsed or
        filtered-out rows still carry (selected) keyframes in the model.
        Resolving only visible rows made scale/interpolation/paste mutate a
        hidden row's visual track and then skip the host commit — the edit
        silently vanished on the next rebuild. Falling back to the full row
        list keeps model mutation and commit consistent.
        """
        rows = self._model.visible_rows()
        for r in rows:
            if r.row_id == row_id:
                return r
        for r in self._model.rows:
            if r.row_id == row_id:
                return r
        return None

    def _hit_keyframe(self, x: float, y: float):
        """Return (row_id, kf_frame) if a keyframe diamond is under (x,y).

        P9: locked rows are not pickable.
        """
        idx = self._row_index_at_y(y)
        if idx < 0:
            return None
        rows = self._model.visible_rows()
        row = rows[idx]
        if getattr(row, "locked", False):
            return None
        track = self._model.track_for(row.row_id)
        if track is None or not track.keyframes:
            return None
        cy = _RULER_H + idx * _ROW_H - self._scroll_y + _ROW_H // 2
        if abs(y - cy) > _KF_HIT_RADIUS:
            return None
        for kf in track.keyframes:
            kx = self._fm.frame_to_x(kf.frame, _ORIGIN_X)
            if abs(x - kx) <= _KF_HIT_RADIUS:
                return (row.row_id, kf.frame)
        return None

    def _hit_range_bar(self, x: float, y: float):
        """Return (row_id, edge) where edge is 'left'/'right'/None(body)/False(none).

        P9: locked rows are not pickable.
        """
        idx = self._row_index_at_y(y)
        if idx < 0:
            return None
        rows = self._model.visible_rows()
        row = rows[idx]
        if getattr(row, "locked", False):
            return None
        track = self._model.track_for(row.row_id)
        if track is None or track.range_bar is None:
            return None
        x0, x1 = self._range_bar_x_bounds(track.range_bar)
        if x < x0 - 2 or x > x1 + 2:
            return None
        if abs(x - x0) <= _RANGE_EDGE_W:
            return (row.row_id, "left")
        if abs(x - x1) <= _RANGE_EDGE_W:
            return (row.row_id, "right")
        if x0 <= x <= x1:
            return (row.row_id, None)  # body
        return None

    def _range_bar_x_bounds(self, bar) -> Tuple[float, float]:
        """Return the full x bounds for a range bar.

        A source video can be longer than the current composition. Its
        ``TimeRangeBar`` retains that full source end and the ruler is sized
        to expose it. Do not clip it to ``_composition_range``: the media
        tail is the part the user needs to trim or slide.
        """
        x0 = self._fm.frame_to_x(bar.start_frame, _ORIGIN_X)
        x1 = self._fm.frame_to_x(bar.end_frame, _ORIGIN_X)
        return x0, x1

    def _hit_param_row(self, y: float) -> Optional[str]:
        idx = self._row_index_at_y(y)
        if idx < 0:
            return None
        rows = self._model.visible_rows()
        return rows[idx].row_id

    # ── keyframe editing ──

    def _select_keyframe(self, row_id: str, kf_frame: int,
                         additive: bool = False) -> None:
        """Select one keyframe; additive keeps the rest of the selection."""
        track = self._model.track_for(row_id)
        if track is None:
            return
        if not additive:
            # Exclusive: clear every track first.
            for tr in self._model.tracks().values():
                for kf in tr.keyframes:
                    kf.selected = False
        for kf in track.keyframes:
            if kf.frame == kf_frame:
                kf.selected = True if not additive else (not kf.selected)
        self._emit_selection_changed()
        self.update()

    def clear_keyframe_selection(self) -> None:
        had_selection = bool(self.selected_keyframes())
        for tr in self._model.tracks().values():
            for kf in tr.keyframes:
                kf.selected = False
        if had_selection:
            self._emit_selection_changed()
        self.update()

    def select_all_keyframes(self) -> None:
        """Select every keyframe in the current timeline projection."""
        changed = False
        for track in self._model.tracks().values():
            for kf in track.keyframes:
                if not kf.selected:
                    kf.selected = True
                    changed = True
        if changed:
            self._emit_selection_changed()
        self.update()

    def selected_keyframes(self) -> List[Tuple[str, int]]:
        """All selected (row_id, frame) pairs across tracks."""
        out: List[Tuple[str, int]] = []
        for row_id, track in self._model.tracks().items():
            for kf in track.keyframes:
                if kf.selected:
                    out.append((row_id, kf.frame))
        return out

    def _emit_selection_changed(self) -> None:
        """Publish the current per-point selection to the shared panel model."""
        self.selection_changed.emit(tuple(self.selected_keyframes()))

    def _emit_track_batch(self, label: str, pending: list) -> None:
        """Emit a prepared multi-track edit with one host undo boundary."""
        if not pending:
            return
        count = len(pending)
        self.track_batch_about_to_commit.emit(str(label), count)
        try:
            for node_id, param_name, keyframes in pending:
                self.track_changed.emit(node_id, param_name, keyframes)
        finally:
            self.track_batch_committed.emit(str(label), count)

    def scale_selected_keyframes(self, factor: float,
                                  pivot_frame: Optional[int] = None) -> bool:
        """Scale selected keyframe times around an anchor frame.

        The operation is atomic: if rounding would collide with an unselected
        keyframe or collapse two selected points onto one frame, no track is
        changed. This keeps handle/extra fields intact and prevents a batch
        edit from silently losing animation data.
        """
        try:
            factor = float(factor)
        except (TypeError, ValueError):
            return False
        import math
        if not math.isfinite(factor) or factor <= 0.0:
            return False
        selected = set(self.selected_keyframes())
        if not selected:
            return False
        if pivot_frame is None:
            pivot_frame = min(frame for _, frame in selected)
        pivot = int(pivot_frame)

        proposed = {}
        pending_rows = {}
        for row_id, track in self._model.tracks().items():
            selected_points = [
                kf for kf in track.keyframes
                if (row_id, int(kf.frame)) in selected
            ]
            if not selected_points:
                continue
            occupied = {
                int(kf.frame) for kf in track.keyframes
                if (row_id, int(kf.frame)) not in selected
            }
            targets = []
            for point in selected_points:
                new_frame = int(round(
                    pivot + (int(point.frame) - pivot) * factor))
                if new_frame < 0 or new_frame in occupied \
                        or new_frame in targets:
                    return False
                targets.append(new_frame)
                proposed[(row_id, int(point.frame))] = new_frame
            pending_rows[row_id] = track

        if not proposed or all(
                old_frame == new_frame
                for (row_id, old_frame), new_frame in proposed.items()):
            return False

        for row_id, track in pending_rows.items():
            for point in track.keyframes:
                new_frame = proposed.get((row_id, int(point.frame)))
                if new_frame is not None:
                    point.frame = new_frame
            track.keyframes.sort(key=lambda point: point.frame)

        pending = []
        for row_id, track in pending_rows.items():
            row = self._row_for_id(row_id)
            if row is None or row.row_type != "param":
                continue
            pending.append((row.node_id, row.param_name,
                            [kf.to_keyframe_dict()
                             for kf in track.keyframes]))
        self._emit_track_batch("scale", pending)
        self._emit_selection_changed()
        self.update()
        return bool(pending)

    # ── marquee (rubber-band) selection ──

    def _do_marquee(self, x: float, y: float) -> None:
        if self._marquee_press is None:
            return
        px, py = self._marquee_press
        if (self._marquee_rect is None and
                abs(x - px) < _MARQUEE_THRESHOLD and
                abs(y - py) < _MARQUEE_THRESHOLD):
            return  # still a plain click — wait for the threshold
        rect = (min(px, x), min(py, y), max(px, x), max(py, y))
        if self._marquee_rect is None and not hasattr(self, "_marquee_base_sel"):
            # Snapshot the pre-existing selection once for Shift-add mode.
            self._marquee_base_sel = set(self.selected_keyframes()) \
                if self._marquee_additive else set()
        self._marquee_rect = rect
        self._apply_marquee_selection(rect)
        self.update()

    def _apply_marquee_selection(self, rect: Tuple[float, float,
                                                   float, float]) -> None:
        """Select every keyframe whose diamond centre lies inside ``rect``."""
        x0, y0, x1, y1 = rect
        base = getattr(self, "_marquee_base_sel", set())
        for tr in self._model.tracks().values():
            for kf in tr.keyframes:
                kf.selected = False
        rows = self._model.visible_rows()
        row_ids = {r.row_id for r in rows}
        for i, row in enumerate(rows):
            if row.row_type != "param":
                continue
            cy = _RULER_H + i * _ROW_H - self._scroll_y + _ROW_H // 2
            if cy < y0 or cy > y1:
                continue
            track = self._model.track_for(row.row_id)
            if track is None:
                continue
            for kf in track.keyframes:
                kx = self._fm.frame_to_x(kf.frame, _ORIGIN_X)
                if x0 <= kx <= x1:
                    kf.selected = True
        if base:
            for row_id, frame in base:
                if row_id not in row_ids:
                    continue
                track = self._model.track_for(row_id)
                if track is None:
                    continue
                for kf in track.keyframes:
                    if kf.frame == frame:
                        kf.selected = True

    def _finish_marquee(self, event) -> None:
        if self._marquee_rect is None:
            # Plain click on empty track area → seek the playhead (old
            # behaviour).
            if self._marquee_press is not None:
                frame = int(round(self._fm.x_to_frame(
                    self._marquee_press[0], _ORIGIN_X)))
                self.set_current_frame(frame)
                self.current_frame_changed.emit(frame)
                # A bare click clears the selection unless Shift is held.
                if not self._marquee_additive:
                    self.clear_keyframe_selection()
            return
        if hasattr(self, "_marquee_base_sel"):
            delattr(self, "_marquee_base_sel")
        self._emit_selection_changed()

    def _add_keyframe(self, node_id: str, param_name: str, frame: int) -> None:
        """Add a keyframe at frame using the current interpolated value.

        The actual node write happens via track_changed → CompositePage.
        """
        self.add_keyframe_at(node_id, param_name, frame)

    def add_keyframe_at(self, node_id: str, param_name: str, frame: int,
                         value_hint: Optional[Any] = None) -> None:
        """Add — or update — a keyframe at ``frame`` on a param track.

        Set-keyframe semantics (Fusion-style): when a keyframe already
        exists at the frame its value is updated in place; otherwise a new
        point is inserted, seeded with ``value_hint`` (the live node param
        value) when provided, else the track's interpolated value.
        """
        frame = max(0, int(frame))
        row_id = _PARAM_ROW_ID_FMT.format(node_id, param_name)
        track = self._model.track_for(row_id)
        if track is None:
            return

        existing = next((kf for kf in track.keyframes if kf.frame == frame),
                        None)
        if existing is not None:
            if value_hint is None:
                # AUD-10202: set-keyframe with nothing to set — the track is
                # unchanged, so do not re-emit it (redundant undo + rebuild).
                return
            existing.value = value_hint
            kfs = [kf.to_keyframe_dict() for kf in track.keyframes]
            self.track_changed.emit(node_id, param_name, kfs)
            self.update()
            return

        # New point: prefer the live param value, else use the preceding
        # visual value. This preserves bool/option/vector domains; the typed
        # runtime track performs interpolation where that is meaningful.
        value: Any = value_hint
        if value is None and track.keyframes:
            previous = [kf for kf in track.keyframes if kf.frame <= frame]
            value = (previous[-1] if previous else track.keyframes[0]).value
        if value is None:
            value = 0.0
        track.keyframes.append(KeyframePointVisual(frame=frame, value=value))
        track.keyframes.sort(key=lambda k: k.frame)
        # P1-1: preserve handle/tangent fields via to_keyframe_dict().
        kfs = [kf.to_keyframe_dict() for kf in track.keyframes]
        self.track_changed.emit(node_id, param_name, kfs)
        self.update()

    def remove_keyframe_at(self, node_id: str, param_name: str,
                           frame: int) -> None:
        """Remove the keyframe at ``frame`` (no-op when absent)."""
        frame = int(frame)
        row_id = _PARAM_ROW_ID_FMT.format(node_id, param_name)
        track = self._model.track_for(row_id)
        if track is None:
            return
        kept = [kf for kf in track.keyframes if kf.frame != frame]
        if len(kept) == len(track.keyframes):
            return
        track.keyframes = kept
        kfs = [kf.to_keyframe_dict() for kf in kept]
        self.track_changed.emit(node_id, param_name, kfs)
        self.update()

    def update_keyframe_at(self, node_id: str, param_name: str,
                           old_frame: int, new_frame: int, value: Any,
                           interpolation: str) -> bool:
        """Edit one point from the inline Alt-click keyframe row.

        The visual track is updated before emitting the normal track signal,
        so the editor feels live while CompositePage remains the sole owner
        of the graph write, undo record and render refresh.
        """
        row_id = _PARAM_ROW_ID_FMT.format(node_id, param_name)
        track = self._model.track_for(row_id)
        if track is None:
            return False
        old_frame = int(old_frame)
        new_frame = max(0, int(new_frame))
        point = next((kf for kf in track.keyframes
                      if kf.frame == old_frame), None)
        if point is None:
            return False
        if new_frame != old_frame and any(
                kf.frame == new_frame for kf in track.keyframes):
            return False
        point.frame = new_frame
        point.value = value
        point.interpolation = str(interpolation or point.interpolation)
        track.keyframes.sort(key=lambda kf: kf.frame)
        self.track_changed.emit(node_id, param_name,
                                [kf.to_keyframe_dict() for kf in track.keyframes])
        self.update()
        return True

    def _delete_selected_keyframes(self) -> bool:
        """Delete all selected keyframes; emit track_changed for affected rows."""
        affected = {}
        for row_id, track in self._model.tracks().items():
            kept = [kf for kf in track.keyframes if not kf.selected]
            if len(kept) == len(track.keyframes):
                continue
            row = self._row_for_id(row_id)
            if row is None or row.row_type != "param":
                continue
            track.keyframes = kept
            affected[(row.node_id, row.param_name)] = [
                kf.to_keyframe_dict() for kf in kept]
        if not affected:
            return False
        pending = [(nid, pname, kfs)
                   for (nid, pname), kfs in affected.items()]
        self._emit_track_batch("delete", pending)
        self._emit_selection_changed()
        self.update()
        return True

    # ── copy / paste ──

    def copy_selected_keyframes(self) -> int:
        """Copy the selected keyframes into the panel clipboard.

        Frames are stored relative to the earliest copied keyframe so a
        paste re-anchors the whole block at the playhead (AE convention).
        Returns the number of copied keyframes.
        """
        sel = self.selected_keyframes()
        if not sel:
            return 0
        base_frame = min(f for _, f in sel)
        clip: List[dict] = []
        for row_id, frame in sorted(sel):
            track = self._model.track_for(row_id)
            if track is None:
                continue
            kf = next((k for k in track.keyframes if k.frame == frame), None)
            if kf is None:
                continue
            clip.append({
                "row_id": row_id,
                "rel_frame": frame - base_frame,
                "data": kf.to_keyframe_dict(),
            })
        if clip:
            self._kf_clipboard = clip
        return len(clip)

    def has_clipboard(self) -> bool:
        return bool(self._kf_clipboard)

    def paste_keyframes(self, at_frame: Optional[int] = None) -> bool:
        """Paste the clipboard at ``at_frame`` (default: playhead).

        Items land back on their original param rows with frames re-anchored;
        existing keyframes at colliding frames are replaced. Emits
        track_changed per affected row so CompositePage persists + undo.
        """
        if not self._kf_clipboard:
            return False
        if at_frame is None:
            at_frame = self._current_frame
        at_frame = max(0, int(at_frame))
        # The pasted points become the new selection.
        for tr in self._model.tracks().values():
            for kf in tr.keyframes:
                kf.selected = False
        affected: dict = {}
        for item in self._kf_clipboard:
            row_id = item.get("row_id")
            track = self._model.track_for(row_id)
            row = self._row_for_id(row_id)
            if track is None or row is None or row.row_type != "param":
                continue
            data = dict(item.get("data") or {})
            frame = at_frame + int(item.get("rel_frame", 0))
            data["frame"] = frame
            extra = {k: v for k, v in data.items()
                     if k not in ("frame", "value", "interpolation")}
            point = KeyframePointVisual(
                frame=frame,
                value=data.get("value", 0.0),
                interpolation=str(data.get("interpolation", "linear")),
                selected=True,
                extra=extra)
            # Replace any keyframe at the target frame.
            track.keyframes = [kf for kf in track.keyframes
                               if kf.frame != frame]
            track.keyframes.append(point)
            track.keyframes.sort(key=lambda k: k.frame)
            affected[(row.node_id, row.param_name)] = track
        if not affected:
            return False
        # Fresh paste selection = the pasted points. Snapshot every track
        # before the first emit because the host may synchronously rebuild the
        # panel after each track_changed signal.
        pending = [
            (nid, pname,
             [kf.to_keyframe_dict() for kf in track.keyframes])
            for (nid, pname), track in affected.items()
        ]
        self._emit_track_batch("paste", pending)
        self._emit_selection_changed()
        self.update()
        return True

    # ── interpolation ──

    def set_selected_interpolation(self, mode: str) -> bool:
        """Apply an interpolation type to all selected keyframes."""
        if mode not in {item[0] for item in _INTERPOLATIONS}:
            return False
        affected = {}
        for row_id, track in self._model.tracks().items():
            changed = False
            for kf in track.keyframes:
                if kf.selected and kf.interpolation != mode:
                    kf.interpolation = mode
                    changed = True
            if not changed:
                continue
            row = self._row_for_id(row_id)
            if row is None or row.row_type != "param":
                continue
            affected[(row.node_id, row.param_name)] = [
                kf.to_keyframe_dict() for kf in track.keyframes]
        if not affected:
            return False
        pending = [(nid, pname, kfs)
                   for (nid, pname), kfs in affected.items()]
        self._emit_track_batch("interpolation", pending)
        self.update()
        return True

    def selected_interpolation(self) -> Optional[str]:
        """Common interpolation of the selection, or None when mixed/empty."""
        modes = set()
        for track in self._model.tracks().values():
            for kf in track.keyframes:
                if kf.selected:
                    modes.add(kf.interpolation)
        if len(modes) == 1:
            return next(iter(modes))
        return None
