# -*- coding: utf-8 -*-
"""
P10.3 - SubtitleClipItem

Visual representation of a SubtitleClip on the timeline. This is a simplified
counterpart of ClipItem (timeline_items.py) tailored for subtitle clips:

- No thumbnails / waveforms (subtitle clips are pure text)
- Renders a compact "chip" with the subtitle text preview (truncated)
- Supports drag-to-move and left/right trim (same geometry as ClipItem)
- Emits a focused set of signals consumed by TimelineView
- Clicking selects the clip and surfaces it in the properties panel

Design note: ClipItem is 8000+ lines and tightly coupled to media assets.
SubtitleClip has no asset (it's inline text/font/color), so a dedicated item
keeps the codebase maintainable.
"""

from __future__ import annotations

from typing import Optional

from PyQt6.QtCore import Qt, QPointF, QRectF, pyqtSignal
from PyQt6.QtGui import (
    QBrush, QColor, QFont, QPainter, QPen, QPainterPath,
)
from PyQt6.QtWidgets import (
    QGraphicsItem,
    QGraphicsObject,
    QGraphicsSceneContextMenuEvent,
    QGraphicsSceneMouseEvent,
)

from src.features.edit.ui.timeline_items import (
    SUBTITLE_CLIP_BG,
    SUBTITLE_CLIP_BG_SELECTED,
    SUBTITLE_CLIP_TEXT_COLOR,
)

# Trim handle hit zone (pixels from each edge)
_TRIM_HANDLE_WIDTH: int = 8
# Minimum drag distance before a move is recognized (avoids accidental moves)
_DRAG_THRESHOLD: float = 3.0
# Maximum text preview length shown on the chip
_MAX_PREVIEW_CHARS: int = 40
# Subtitle clips are intentionally smaller than the track itself. The space
# around the chip makes the subtitle lane legible and keeps it visually
# distinct from a video clip when users enlarge the lane.
_CHIP_MIN_HEIGHT: float = 22.0
_CHIP_MAX_HEIGHT: float = 36.0
_CHIP_VERTICAL_PADDING: float = 4.0


class SubtitleClipItem(QGraphicsObject):
    """
    Timeline item for a SubtitleClip.

    Signals:
        subtitle_clip_clicked(clip_id, track_id): selection
        subtitle_clip_moved(clip_id, track_id, new_start): live drag preview
        subtitle_clip_drag_preview(clip_id, track_id, new_start, scene_y):
            live move preview; TimelineView chooses the subtitle target lane
        subtitle_clip_drag_finished(clip_id, track_id, old_start, new_start,
                                    scene_y, copy_requested):
            mouseRelease — caller issues a command
        subtitle_clip_trim_left(clip_id, track_id, new_start): live trim preview
        subtitle_clip_trim_right(clip_id, track_id, new_duration): live trim preview
        subtitle_clip_trim_left_finished(clip_id, track_id, old_start, old_duration,
                                          new_start, new_duration): command-ready
        subtitle_clip_trim_right_finished(clip_id, track_id, old_duration, new_duration):
            command-ready
        subtitle_clip_delete(clip_id, track_id): delete requested
        subtitle_clip_context_menu_requested(clip_id, track_id, screen_pos):
            right-click context menu requested
    """

    # Selection / drag
    subtitle_clip_clicked = pyqtSignal(str, str)  # clip_id, track_id
    subtitle_clip_moved = pyqtSignal(str, str, int)  # clip_id, track_id, new_start
    subtitle_clip_drag_preview = pyqtSignal(str, str, int, float)  # clip_id, track_id, start, scene_y
    subtitle_clip_drag_finished = pyqtSignal(str, str, int, int, float, bool)
    # Trim
    subtitle_clip_trim_left = pyqtSignal(str, str, int)  # clip_id, track_id, new_start
    subtitle_clip_trim_right = pyqtSignal(str, str, int)  # clip_id, track_id, new_duration
    subtitle_clip_trim_left_finished = pyqtSignal(str, str, int, int, int, int)  # clip_id, track_id, old_start, old_dur, new_start, new_dur
    subtitle_clip_trim_right_finished = pyqtSignal(str, str, int, int)  # clip_id, track_id, old_dur, new_dur
    # Delete
    subtitle_clip_delete = pyqtSignal(str, str)  # clip_id, track_id
    subtitle_clip_context_menu_requested = pyqtSignal(str, str, object)
    # Edit text (double-click)
    subtitle_clip_edit_text_requested = pyqtSignal(str, str, str)  # clip_id, track_id, new_text

    def __init__(
        self,
        clip_id: str,
        track_id: str,
        text: str,
        start_frame: int,
        duration_frames: int,
        pixels_per_frame: float,
        track_height: int,
        is_enabled: bool = True,
        is_track_locked: bool = False,
        label_color: Optional[str] = None,
    ) -> None:
        super().__init__()
        self._clip_id = clip_id
        self._track_id = track_id
        self._text = text or ""
        self._start_frame = start_frame
        self._duration_frames = duration_frames
        self._pixels_per_frame = pixels_per_frame
        self._track_height = track_height
        self._is_enabled = is_enabled
        self._is_track_locked = is_track_locked
        self._label_color = label_color
        self._is_selected = False

        # Drag / trim state
        self._drag_mode: str = "none"  # "none" | "move" | "trim_left" | "trim_right"
        self._drag_start_pos = QPointF()
        self._drag_orig_start = start_frame
        self._drag_orig_duration = duration_frames
        self._has_dragged = False
        self._drag_copy_requested: bool = False
        # Alt-drag keeps this source item at its original location.  The
        # TimelineView renders a separate translucent copy preview using this
        # frame, then creates the real duplicate only on mouse release.
        self._drag_copy_preview_start: int = start_frame
        # The view updates this as the pointer crosses subtitle lanes.  Source
        # lane clamping must be disabled once a drag targets another lane (or
        # is a copy), otherwise neighbours on the source lane incorrectly
        # restrict an otherwise valid destination drop.
        self._drag_target_track_id: str = track_id
        # SUB-P0-04: optional live-clamp callback injected by TimelineView.
        # Signature: (mode, orig_start, orig_dur, proposed_start, proposed_dur)
        #         -> (clamped_start, clamped_dur). None => no clamping (legacy).
        self._drag_clamp_callback = None

        # Interaction flags
        self.setAcceptHoverEvents(True)
        self.setFlag(QGraphicsObject.GraphicsItemFlag.ItemIsSelectable, True)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsFocusable, True)
        self.setFlag(QGraphicsObject.GraphicsItemFlag.ItemSendsGeometryChanges, True)
        self.setCursor(Qt.CursorShape.OpenHandCursor)

        # Tooltip with full text (so truncated preview doesn't hide content)
        self.setToolTip(self._text if self._text else "(empty subtitle)")

    # ── Public API ────────────────────────────────────────────────────────────

    @property
    def clip_id(self) -> str:
        return self._clip_id

    @property
    def track_id(self) -> str:
        return self._track_id

    @property
    def start_frame(self) -> int:
        return self._start_frame

    @property
    def duration_frames(self) -> int:
        return self._duration_frames

    def set_pixels_per_frame(self, ppf: float) -> None:
        if ppf != self._pixels_per_frame and ppf > 0:
            self.prepareGeometryChange()
            self._pixels_per_frame = ppf
            self.update()

    def set_drag_clamp_callback(self, callback) -> None:
        """Inject a live-clamp callback for SUB-P0-04 drag collision policy.

        Signature: (mode: str, orig_start: int, orig_dur: int,
                    proposed_start: int, proposed_dur: int) ->
                   (clamped_start: int, clamped_dur: int)
        The TimelineView supplies one backed by SubtitleEditPolicy so a drag
        stays within the legal range in real time, not only on release. Pass
        None to disable clamping.
        """
        self._drag_clamp_callback = callback

    def set_drag_target_track(self, track_id: str) -> None:
        """Record the current subtitle-lane target for live drag policy."""
        self._drag_target_track_id = track_id

    def set_drag_copy_preview_start(self, start_frame: int) -> None:
        """Store the collision/snapping-resolved position of an Alt-copy."""
        self._drag_copy_preview_start = max(0, int(start_frame))

    def drag_release_start(self) -> int:
        """Return the final start frame for the current drag operation."""
        if self._drag_copy_requested:
            return self._drag_copy_preview_start
        return self._start_frame

    def set_track_height(self, h: int) -> None:
        if h != self._track_height and h > 0:
            self.prepareGeometryChange()
            self._track_height = h
            self.update()

    def set_selected_state(self, selected: bool) -> None:
        if self._is_selected != selected:
            self._is_selected = selected
            self.update()

    def set_track_locked(self, locked: bool) -> None:
        self._is_track_locked = locked
        self.update()

    def update_clip_data(
        self,
        text: Optional[str] = None,
        start_frame: Optional[int] = None,
        duration_frames: Optional[int] = None,
        is_enabled: Optional[bool] = None,
    ) -> None:
        """Refresh displayed data after an undoable edit."""
        changed = False
        geometry_changed = (
            (start_frame is not None and start_frame != self._start_frame)
            or (duration_frames is not None and duration_frames != self._duration_frames)
        )
        if geometry_changed:
            self.prepareGeometryChange()
        if text is not None and text != self._text:
            self._text = text
            self.setToolTip(self._text if self._text else "(empty subtitle)")
            changed = True
        if start_frame is not None and start_frame != self._start_frame:
            self._start_frame = start_frame
            changed = True
        if duration_frames is not None and duration_frames != self._duration_frames:
            self._duration_frames = duration_frames
            changed = True
        if is_enabled is not None and is_enabled != self._is_enabled:
            self._is_enabled = is_enabled
            changed = True
        if changed:
            self.update()

    def restore_geometry(self, start_frame: int, duration_frames: int) -> None:
        """Restore this chip to a prior geometry after a failed drag commit.

        SUB-P0-04: when the release-time move/trim command fails validation
        (e.g. overlap), the controller calls this to snap the chip back to its
        pre-drag (start, duration) so the UI never lingers in an illegal
        position. Mirrors ``update_clip_data`` geometry handling.
        """
        if start_frame == self._start_frame and duration_frames == self._duration_frames:
            return
        self.prepareGeometryChange()
        self._start_frame = int(start_frame)
        self._duration_frames = int(duration_frames)
        self.update()

    # ── Qt geometry / painting ─────────────────────────────────────────────────

    def boundingRect(self) -> QRectF:
        w = max(2.0, self._duration_frames * self._pixels_per_frame)
        available = max(0.0, float(self._track_height) - 2.0 * _CHIP_VERTICAL_PADDING)
        h = max(_CHIP_MIN_HEIGHT, min(_CHIP_MAX_HEIGHT, available))
        # Center the compact chip within the lane so it remains contained even
        # when the user changes the subtitle-track height.
        y = max(_CHIP_VERTICAL_PADDING, (float(self._track_height) - h) / 2.0)
        return QRectF(0.0, y, w, h)

    def shape(self) -> QPainterPath:
        path = QPainterPath()
        path.addRect(self.boundingRect())
        return path

    def paint(self, painter: QPainter, option, widget=None) -> None:
        rect = self.boundingRect()
        radius = 3.0

        # Body color
        if self._is_selected:
            bg = SUBTITLE_CLIP_BG_SELECTED
        else:
            bg = SUBTITLE_CLIP_BG
        if self._label_color:
            try:
                bg = QColor(self._label_color)
            except Exception:
                pass

        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setBrush(QBrush(bg))
        painter.setPen(QPen(QColor(0, 0, 0, 120), 1))
        painter.drawRoundedRect(rect, radius, radius)

        # Disabled state — dim
        if not self._is_enabled or self._is_track_locked:
            painter.setBrush(QBrush(QColor(0, 0, 0, 120)))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawRoundedRect(rect, radius, radius)

        # Selection border
        if self._is_selected:
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(QColor(255, 200, 80, 220), 1.5))
            painter.drawRoundedRect(rect.adjusted(0.5, 0.5, -0.5, -0.5), radius, radius)

        # Text preview (truncated, single line)
        preview = self._preview_text()
        if preview:
            painter.setPen(QPen(SUBTITLE_CLIP_TEXT_COLOR))
            font = QFont()
            font.setPixelSize(max(9, min(13, int(rect.height() * 0.42))))
            painter.setFont(font)
            text_rect = rect.adjusted(
                _TRIM_HANDLE_WIDTH, 2, -_TRIM_HANDLE_WIDTH, -2
            )
            painter.drawText(
                text_rect,
                int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                preview,
            )

        # Trim handle indicators (subtle lines near edges)
        if self._duration_frames * self._pixels_per_frame > 24:
            painter.setPen(QPen(QColor(255, 255, 255, 60), 1))
            painter.drawLine(
                QPointF(_TRIM_HANDLE_WIDTH, rect.top() + 2),
                QPointF(_TRIM_HANDLE_WIDTH, rect.bottom() - 2),
            )
            painter.drawLine(
                QPointF(rect.right() - _TRIM_HANDLE_WIDTH, rect.top() + 2),
                QPointF(rect.right() - _TRIM_HANDLE_WIDTH, rect.bottom() - 2),
            )

    def _preview_text(self) -> str:
        """Single-line, truncated preview of the subtitle text."""
        t = " ".join(self._text.split())  # collapse whitespace/newlines
        if len(t) > _MAX_PREVIEW_CHARS:
            return t[:_MAX_PREVIEW_CHARS - 1] + "…"
        return t

    # ── Mouse interaction ──────────────────────────────────────────────────────

    def _edge_at(self, pos: QPointF) -> str:
        """Return 'left', 'right', or 'none' depending on which edge pos is over."""
        if pos.x() < _TRIM_HANDLE_WIDTH:
            return "left"
        if pos.x() > self.boundingRect().width() - _TRIM_HANDLE_WIDTH:
            return "right"
        return "none"

    def mousePressEvent(self, event: QGraphicsSceneMouseEvent) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            if event.button() == Qt.MouseButton.RightButton:
                # Right-click → select + show context menu via signal
                self.subtitle_clip_clicked.emit(self._clip_id, self._track_id)
            event.accept()
            return

        if self._is_track_locked:
            event.accept()
            return

        # Subtitle items accept the mouse event themselves, so the parent
        # QGraphicsView does not necessarily receive focus.  Give the item
        # focus explicitly so Delete/Backspace reaches keyPressEvent below.
        self.setFocus(Qt.FocusReason.MouseFocusReason)

        pos = event.pos()
        edge = self._edge_at(pos)
        self._drag_start_pos = QPointF(event.scenePos())
        self._drag_orig_start = self._start_frame
        self._drag_orig_duration = self._duration_frames
        self._has_dragged = False
        # Capture this on pointer-down. Reading keyboard state on release can
        # lose Alt if the user releases it a fraction before the mouse button.
        self._drag_copy_requested = bool(
            event.modifiers() & Qt.KeyboardModifier.AltModifier
        )
        self._drag_copy_preview_start = self._start_frame
        self._drag_target_track_id = self._track_id

        if edge == "left":
            self._drag_mode = "trim_left"
            self.setCursor(Qt.CursorShape.SizeHorCursor)
        elif edge == "right":
            self._drag_mode = "trim_right"
            self.setCursor(Qt.CursorShape.SizeHorCursor)
        else:
            self._drag_mode = "move"
            self.setCursor(Qt.CursorShape.ClosedHandCursor)

        # Always select on press
        self.subtitle_clip_clicked.emit(self._clip_id, self._track_id)
        event.accept()

    def mouseMoveEvent(self, event: QGraphicsSceneMouseEvent) -> None:
        if self._drag_mode == "none":
            event.ignore()
            return

        delta_px = event.scenePos().x() - self._drag_start_pos.x()
        delta_y = event.scenePos().y() - self._drag_start_pos.y()
        if not self._has_dragged:
            # A subtitle move can be purely vertical when changing lanes, so
            # use two-dimensional distance for move mode. Trim operations are
            # intentionally horizontal-only.
            drag_distance = (
                max(abs(delta_px), abs(delta_y))
                if self._drag_mode == "move"
                else abs(delta_px)
            )
            if drag_distance < _DRAG_THRESHOLD:
                event.accept()
                return
            self._has_dragged = True

        delta_frames = int(round(delta_px / self._pixels_per_frame))
        cb = self._drag_clamp_callback
        if self._drag_mode == "move":
            new_start = max(0, self._drag_orig_start + delta_frames)
            if self._drag_copy_requested:
                # Keep the source cue still and let TimelineView draw a
                # translucent duplicate that follows the pointer.
                self._drag_copy_preview_start = new_start
                self.subtitle_clip_moved.emit(self._clip_id, self._track_id, new_start)
            elif new_start != self._start_frame:
                self._start_frame = new_start
                self.subtitle_clip_moved.emit(self._clip_id, self._track_id, new_start)
            # The TimelineView owns track geometry, so report the scene Y and
            # let it choose a subtitle destination lane for cross-track drag.
            self.subtitle_clip_drag_preview.emit(
                self._clip_id, self._track_id, self.drag_release_start(),
                float(event.scenePos().y()),
            )
        elif self._drag_mode == "trim_left":
            new_start = max(0, self._drag_orig_start + delta_frames)
            new_dur = self._drag_orig_duration - delta_frames
            if new_start >= 0 and new_dur >= 1:
                if cb is not None:
                    new_start, new_dur = cb(
                        "trim_left", self._drag_orig_start, self._drag_orig_duration,
                        new_start, new_dur,
                    )
                    new_start, new_dur = int(new_start), int(new_dur)
                if new_start >= 0 and new_dur >= 1:
                    self.prepareGeometryChange()
                    self._start_frame = new_start
                    self._duration_frames = new_dur
                    self.update()
                    self.subtitle_clip_trim_left.emit(self._clip_id, self._track_id, new_start)
        elif self._drag_mode == "trim_right":
            new_dur = max(1, self._drag_orig_duration + delta_frames)
            if cb is not None:
                _start, new_dur = cb(
                    "trim_right", self._drag_orig_start, self._drag_orig_duration,
                    self._drag_orig_start, new_dur,
                )
                new_dur = int(new_dur)
            if new_dur >= 1 and new_dur != self._duration_frames:
                self.prepareGeometryChange()
                self._duration_frames = new_dur
                self.update()
                self.subtitle_clip_trim_right.emit(self._clip_id, self._track_id, new_dur)

        event.accept()

    def mouseReleaseEvent(self, event: QGraphicsSceneMouseEvent) -> None:
        mode = self._drag_mode
        old_start = self._drag_orig_start
        old_dur = self._drag_orig_duration
        self._drag_mode = "none"
        self.setCursor(Qt.CursorShape.OpenHandCursor)

        if not self._has_dragged:
            event.accept()
            return

        if mode == "move":
            self.subtitle_clip_drag_finished.emit(
                self._clip_id, self._track_id, old_start, self.drag_release_start(),
                float(event.scenePos().y()),
                self._drag_copy_requested,
            )
        elif mode == "trim_left":
            self.subtitle_clip_trim_left_finished.emit(
                self._clip_id, self._track_id,
                old_start, old_dur, self._start_frame, self._duration_frames,
            )
        elif mode == "trim_right":
            self.subtitle_clip_trim_right_finished.emit(
                self._clip_id, self._track_id, old_dur, self._duration_frames
            )
        event.accept()

    def contextMenuEvent(self, event: QGraphicsSceneContextMenuEvent) -> None:
        """Ask TimelineView to show the subtitle clip context menu."""
        self.subtitle_clip_context_menu_requested.emit(
            self._clip_id,
            self._track_id,
            event.screenPos(),
        )
        event.accept()

    def mouseDoubleClickEvent(self, event: QGraphicsSceneMouseEvent) -> None:
        """Double-click selects the clip and opens an inline text edit dialog."""
        if event.button() == Qt.MouseButton.LeftButton:
            # Select first (so the clip is highlighted while editing)
            self.subtitle_clip_clicked.emit(self._clip_id, self._track_id)
            # Open a multi-line text edit dialog for the subtitle content
            from src.shared.i18n.i18n import I18N
            try:
                from PyQt6.QtWidgets import QInputDialog
                view = self.scene().views()[0] if self.scene() else None
                parent = view.window() if view else None
                new_text, ok = QInputDialog.getMultiLineText(
                    parent,
                    I18N.tr("timeline.subtitle.edit_title", "Edit Subtitle Text"),
                    I18N.tr("timeline.subtitle.edit_label", "Subtitle text:"),
                    self._text,
                )
                if ok and new_text != self._text:
                    self.subtitle_clip_edit_text_requested.emit(
                        self._clip_id, self._track_id, new_text
                    )
            except Exception:
                # Dialog failed (e.g. no view) — just keep the selection
                pass
        event.accept()

    def keyPressEvent(self, event) -> None:  # noqa: ANN001 - Qt override
        """Delete key removes the clip (locked tracks reject, as mouse does)."""
        if event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            # AUD-21003: mirror the mouse/context-menu path — locked tracks
            # never delete; the command layer would only reject later with a
            # confusing error instead of the key being a no-op.
            if self._is_track_locked:
                event.accept()
                return
            self.subtitle_clip_delete.emit(self._clip_id, self._track_id)
            event.accept()
            return
        super().keyPressEvent(event)
