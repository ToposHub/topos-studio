"""Audio timeline presentation and local editing gestures for the Mixer UI prototype.

No decoder, device, project mutation, or production playback clock is created here.
The host session records local changes on its own undo stack.
"""
from __future__ import annotations

import copy
import math
import uuid
from typing import Any

from PyQt6.QtCore import QPoint, QPointF, QRectF, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QKeySequence, QMouseEvent, QPainter, QPainterPath, QPen, QShortcut, QWheelEvent
from PyQt6.QtWidgets import QDoubleSpinBox, QFrame, QHBoxLayout, QLabel, QLineEdit, QMenu, QPushButton, QScrollArea, QScrollBar, QSizePolicy, QSlider, QToolButton, QVBoxLayout, QWidget

from src.features.edit.ui.edit_style import EDIT_COLORS, EDIT_SIZES
from src.features.edit.ui.timeline_icons import timeline_icon
from src.shared.assets.icon_manager import IconColors, icon_folder_open
from .session import MixerUiSession, UiClip
from .workspace_style import PANEL_QSS, compact_combo, tool_button, toolbar_divider


class TimelineCanvas(QWidget):
    """A viewport-sized horizontal canvas with fixed track headers."""

    clipEditorRequested = pyqtSignal(str)
    rangeChanged = pyqtSignal(float, float)

    HEADER_WIDTH = 186
    RULER_HEIGHT = 30

    def __init__(self, session: MixerUiSession, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.session = session
        self.pixels_per_second = 20.0
        self.track_height = EDIT_SIZES["track_h_audio"]
        self.scroll_x = 0
        self.snap_seconds: float | None = None
        self.tool = "select"
        self.range_start = 0.0
        self.range_end = 0.0
        self._drag: dict[str, Any] | None = None
        self._hover: tuple[str, str] | None = None
        self._clipboard: UiClip | None = None
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMinimumWidth(300)
        self.session.changed.connect(self._refresh)
        self.session.playhead_changed.connect(lambda _: self.update())
        self.session.selection_changed.connect(lambda _: self.update())
        self.session.timeline_format_changed.connect(self.update)
        self._refresh()

    def _refresh(self) -> None:
        self.setMinimumHeight(self.RULER_HEIGHT + max(1, len(self.session.tracks)) * self.track_height + 22)
        self.update()

    def set_scroll(self, value: int) -> None:
        self.scroll_x = value
        self.update()

    def time_to_x(self, seconds: float) -> float:
        return self.HEADER_WIDTH + seconds * self.pixels_per_second - self.scroll_x

    def x_to_time(self, x: float) -> float:
        return max(0.0, min(self.session.duration, (x - self.HEADER_WIDTH + self.scroll_x) / self.pixels_per_second))

    def snap_time(self, seconds: float) -> float:
        if self.session.snap_enabled and self.session.snap_frames > 0:
            step = self.snap_seconds if self.snap_seconds is not None else self.session.frame_duration * self.session.snap_frames
            seconds = round(seconds / step) * step
        return max(0.0, seconds)

    def clip_rect(self, clip: UiClip) -> QRectF:
        index = next((i for i, track in enumerate(self.session.tracks) if track.id == clip.track_id), -1)
        return QRectF(self.time_to_x(clip.start), self.RULER_HEIGHT + index * self.track_height + 5,
                      max(4.0, clip.duration * self.pixels_per_second), self.track_height - 10)

    def _hit(self, point: QPointF) -> tuple[UiClip | None, str]:
        if point.x() < self.HEADER_WIDTH or point.y() < self.RULER_HEIGHT:
            return None, ""
        for clip in reversed(self.session.clips):
            rect = self.clip_rect(clip)
            if not rect.adjusted(-4, 0, 4, 0).contains(point):
                continue
            fi_x = rect.left() + clip.fade_in * self.pixels_per_second
            fo_x = rect.right() - clip.fade_out * self.pixels_per_second
            if rect.top() + 19 <= point.y() <= rect.top() + 33:
                if abs(point.x() - fi_x) <= 7:
                    return clip, "fade_in"
                if abs(point.x() - fo_x) <= 7:
                    return clip, "fade_out"
            if abs(point.x() - rect.left()) < 6:
                return clip, "left"
            if abs(point.x() - rect.right()) < 6:
                return clip, "right"
            return clip, "move"
        return None, ""

    def paintEvent(self, event: Any) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor(EDIT_COLORS["timeline_canvas_bg"]))
        font = painter.font()
        font.setPixelSize(11)
        painter.setFont(font)
        width, height = self.width(), self.height()
        for index, track in enumerate(self.session.tracks):
            y = self.RULER_HEIGHT + index * self.track_height
            painter.fillRect(QRectF(0, y, width, self.track_height), QColor(EDIT_COLORS["bg_panel_alt"] if index % 2 else EDIT_COLORS["timeline_canvas_bg"]))
            painter.setPen(QPen(QColor("#34343C"), 1))
            painter.drawLine(QPointF(0, y + self.track_height), QPointF(width, y + self.track_height))
        painter.save()
        painter.setClipRect(QRectF(self.HEADER_WIDTH, 0, max(0, width - self.HEADER_WIDTH), height))
        self._draw_ruler(painter)
        for clip in self.session.clips:
            self._draw_clip(painter, clip)
        if self.range_end > self.range_start:
            start_x, end_x = self.time_to_x(self.range_start), self.time_to_x(self.range_end)
            painter.fillRect(QRectF(start_x, self.RULER_HEIGHT, end_x - start_x, height), QColor(226, 146, 39, 22))
            painter.setPen(QPen(QColor(EDIT_COLORS["accent"]), 1))
            painter.drawLine(QPointF(start_x, self.RULER_HEIGHT), QPointF(start_x, height))
            painter.drawLine(QPointF(end_x, self.RULER_HEIGHT), QPointF(end_x, height))
        play_x = self.time_to_x(self.session.playhead)
        painter.setPen(QPen(QColor(EDIT_COLORS["playhead"]), 1.3))
        painter.drawLine(QPointF(play_x, 0), QPointF(play_x, height))
        painter.setBrush(QColor(EDIT_COLORS["playhead"]))
        path = QPainterPath(QPointF(play_x - 5, 0))
        path.lineTo(play_x + 5, 0)
        path.lineTo(play_x + 5, 8)
        path.lineTo(play_x, 13)
        path.lineTo(play_x - 5, 8)
        path.closeSubpath()
        painter.drawPath(path)
        if self.session.loop_enabled:
            lx = self.time_to_x(self.session.loop_start)
            rx = self.time_to_x(self.session.loop_end)
            painter.fillRect(QRectF(lx, 0, max(1.0, rx - lx), 3), QColor("#D7AE79"))
        painter.restore()
        self._draw_headers(painter)

    def _draw_ruler(self, painter: QPainter) -> None:
        left, right = self.HEADER_WIDTH, self.width()
        painter.fillRect(QRectF(left, 0, right - left, self.RULER_HEIGHT), QColor(EDIT_COLORS["ruler_bg"]))
        start = max(0.0, self.scroll_x / self.pixels_per_second)
        end = start + max(1, right - left) / self.pixels_per_second
        frame_px = self.pixels_per_second * self.session.frame_duration
        nominal = max(1, round(self.session.fps))
        major_frames = nominal
        while major_frames * frame_px < 105:
            major_frames *= 2
        minor_frames = next((n for n in (1, 2, 5, 10, 20, 50, major_frames) if n * frame_px >= 7), major_frames)
        first = int(start * self.session.fps)
        last = int(end * self.session.fps) + 1
        frames = set(range(first // minor_frames * minor_frames, last + 1, minor_frames))
        frames.update(range(first // major_frames * major_frames, last + 1, major_frames))
        for frame in sorted(frames):
            seconds = frame / self.session.fps
            x = self.time_to_x(seconds)
            major = frame % major_frames == 0
            painter.setPen(QPen(QColor(EDIT_COLORS["ruler_major_tick"] if major else EDIT_COLORS["ruler_minor_tick"]), 1))
            painter.drawLine(QPointF(x, 19 if major else 25), QPointF(x, self.RULER_HEIGHT))
            if major:
                painter.setPen(QColor("#B8B8C3"))
                painter.drawText(QPointF(x + 4, 16), self.session.timecode(seconds))
                painter.setPen(QPen(QColor("#2D2D35"), 1))
                painter.drawLine(QPointF(x, self.RULER_HEIGHT), QPointF(x, self.height()))
            elif frame_px >= 28 and self.session.time_format == "timecode":
                painter.setPen(QColor("#92929E"))
                painter.drawText(QPointF(x + 3, 17), f"{frame % nominal:02d}f")

    def _draw_headers(self, painter: QPainter) -> None:
        painter.fillRect(QRectF(0, 0, self.HEADER_WIDTH, self.RULER_HEIGHT), QColor("#27272B"))
        painter.setPen(QColor("#A5A5B0"))
        painter.drawText(QRectF(12, 0, self.HEADER_WIDTH - 20, self.RULER_HEIGHT), Qt.AlignmentFlag.AlignVCenter,
                         f"音频轨道  {len(self.session.tracks)}")
        for index, track in enumerate(self.session.tracks):
            y = self.RULER_HEIGHT + index * self.track_height
            painter.fillRect(QRectF(0, y, self.HEADER_WIDTH, self.track_height), QColor(EDIT_COLORS["track_header_bg_selected"] if track.id == self.session.selected_track_id else EDIT_COLORS["track_header_bg"]))
            color = QColor(track.color)
            painter.fillRect(QRectF(0, y, 3, self.track_height), color)
            painter.setPen(QColor("#E4E4EA"))
            text = painter.fontMetrics().elidedText(track.name, Qt.TextElideMode.ElideRight, self.HEADER_WIDTH - 24)
            painter.drawText(QPointF(12, y + 22), text)
            painter.setPen(QColor("#92929E"))
            painter.drawText(QPointF(12, y + 44), f"{track.role}  ·  {track.automation_mode}")
            for x, label, flag in ((99, "M", track.muted), (124, "S", track.solo), (149, "R", track.armed)):
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QColor("#704936" if flag else "#35353D"))
                painter.drawRoundedRect(QRectF(x, y + 30, 20, 20), 3, 3)
                painter.setPen(QColor("#FFE1B8" if flag else "#A5A5B0"))
                painter.drawText(QRectF(x, y + 30, 20, 20), Qt.AlignmentFlag.AlignCenter, label)
            painter.setPen(QColor("#B8B8C3"))
            painter.drawText(QPointF(12, y + self.track_height - 12), f"{track.volume_db:+.1f} dB")
            painter.setPen(QPen(QColor("#3A3A43"), 1))
            painter.drawLine(QPointF(3, y + self.track_height), QPointF(self.HEADER_WIDTH, y + self.track_height))
        painter.setPen(QPen(QColor("#3A3A43"), 1))
        painter.drawLine(QPointF(self.HEADER_WIDTH, 0), QPointF(self.HEADER_WIDTH, self.height()))

    def _draw_clip(self, painter: QPainter, clip: UiClip) -> None:
        rect = self.clip_rect(clip)
        if rect.right() < self.HEADER_WIDTH or rect.left() > self.width() or rect.top() < self.RULER_HEIGHT:
            return
        track = self.session.track_by_id(clip.track_id)
        if track is None:
            return
        color = QColor("#777782") if clip.muted else QColor(track.color)
        selected = clip.id == self.session.selected_clip_id
        painter.save()
        path = QPainterPath()
        path.addRoundedRect(rect, 4, 4)
        painter.setClipPath(path, Qt.ClipOperation.IntersectClip)
        painter.fillRect(rect, QColor(color.red(), color.green(), color.blue(), 23))
        painter.fillRect(QRectF(rect.left(), rect.top(), rect.width(), 20), QColor(color.red(), color.green(), color.blue(), 43))
        painter.setPen(color.lighter(140))
        title = ("[LOCK] " if clip.locked else "") + clip.name
        painter.drawText(QRectF(rect.left() + 7, rect.top(), max(0, rect.width() - 14), 20), Qt.AlignmentFlag.AlignVCenter,
                         painter.fontMetrics().elidedText(title, Qt.TextElideMode.ElideRight, max(0, int(rect.width() - 14))))
        top = rect.top() + 24
        wave_h = rect.height() - 28
        center = top + wave_h / 2
        painter.setPen(QPen(QColor(color.red(), color.green(), color.blue(), 70), 1))
        painter.drawLine(QPointF(rect.left(), center), QPointF(rect.right(), center))
        if clip.peaks:
            source_duration = max(clip.duration + clip.offset, float(getattr(clip, "source_duration", clip.duration + clip.offset)))
            left = max(self.HEADER_WIDTH, math.floor(rect.left()))
            right = min(self.width(), math.ceil(rect.right()))
            painter.setPen(QPen(QColor(color.red(), color.green(), color.blue(), 190), 1.2))
            scale = 10 ** (clip.gain_db / 20)
            for x in range(left, right, 2):
                local_t = max(0.0, (x - rect.left()) / self.pixels_per_second)
                sample = min(len(clip.peaks) - 1, int((local_t + clip.offset) / source_duration * len(clip.peaks)))
                fade = 1.0
                if clip.fade_in > 0:
                    fade *= min(1.0, local_t / clip.fade_in)
                if clip.fade_out > 0:
                    fade *= min(1.0, max(0.0, clip.duration - local_t) / clip.fade_out)
                amplitude = max(1, min(1.0, clip.peaks[sample] * scale * fade) * wave_h * 0.46)
                painter.drawLine(QPointF(x, center - amplitude), QPointF(x, center + amplitude))
        else:
            painter.setPen(QColor("#92929E"))
            painter.drawText(QRectF(rect.left() + 8, top, max(0, rect.width() - 16), wave_h), Qt.AlignmentFlag.AlignVCenter, "波形解码待接入")
        painter.setPen(QPen(QColor("#D7AE79"), 1))
        painter.drawLine(QPointF(rect.left(), rect.bottom()), QPointF(rect.left() + min(clip.fade_in, clip.duration) * self.pixels_per_second, top))
        painter.drawLine(QPointF(rect.right() - min(clip.fade_out, clip.duration) * self.pixels_per_second, top), QPointF(rect.right(), rect.bottom()))
        if selected:
            painter.setBrush(QColor("#D7AE79"))
            for x in (rect.left() + clip.fade_in * self.pixels_per_second, rect.right() - clip.fade_out * self.pixels_per_second):
                painter.drawRoundedRect(QRectF(x - 3, rect.top() + 21, 6, 7), 1, 1)
        painter.restore()
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(QColor(EDIT_COLORS["accent"]) if selected else QColor(color.red(), color.green(), color.blue(), 100), 1.3 if selected else 1))
        painter.drawRoundedRect(rect.adjusted(0.5, 0.5, -0.5, -0.5), 4, 4)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.RightButton:
            self._show_context(event.position(), event.globalPosition().toPoint())
            return
        if event.button() != Qt.MouseButton.LeftButton:
            return
        self.setFocus()
        point = event.position()
        if point.x() < self.HEADER_WIDTH:
            index = int((point.y() - self.RULER_HEIGHT) // self.track_height)
            if 0 <= index < len(self.session.tracks):
                track = self.session.tracks[index]
                self.session.select_track(track.id)
                local_y = point.y() - self.RULER_HEIGHT - index * self.track_height
                if 30 <= local_y <= 50:
                    for x, flag in ((99, "muted"), (124, "solo"), (149, "armed")):
                        if x <= point.x() <= x + 20:
                            track_id = track.id
                            value = not getattr(track, flag)
                            self.session.commit("更改轨道状态", lambda tid=track_id, key=flag, val=value: setattr(self.session.track_by_id(tid), key, val))
                            break
            return
        seconds = self.snap_time(self.x_to_time(point.x()))
        if point.y() < self.RULER_HEIGHT:
            self.session.seek(seconds)
            self._drag = {"zone": "seek"}
            return
        clip, zone = self._hit(point)
        if self.tool == "range" or event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
            self.range_start = seconds
            self.range_end = seconds
            self._drag = {"zone": "range", "start": seconds}
            self.update()
            return
        if clip is None:
            self.session.select_clip(None)
            self.session.seek(seconds)
            return
        self.session.select_clip(clip.id)
        if self.tool == "razor":
            self.session.split_clip(clip.id, seconds)
            return
        if clip.locked:
            self.session.status_changed.emit("片段已锁定；可在右键菜单中解锁")
            return
        keys = ("start", "duration", "offset", "fade_in", "fade_out", "track_id")
        self._drag = {"zone": zone, "clip_id": clip.id, "origin": point.x(), "before": {key: getattr(clip, key) for key in keys}, "snapshot": self.session.snapshot()}

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        point = event.position()
        if self._drag is None:
            clip, zone = self._hit(point)
            self._hover = (clip.id, zone) if clip else None
            self.setCursor(Qt.CursorShape.SizeHorCursor if zone in ("left", "right", "fade_in", "fade_out") else Qt.CursorShape.ArrowCursor)
            return
        zone = self._drag["zone"]
        seconds = self.snap_time(self.x_to_time(point.x()))
        if zone == "seek":
            self.session.seek(seconds)
            return
        if zone == "range":
            self.range_start, self.range_end = sorted((self._drag["start"], seconds))
            self.rangeChanged.emit(self.range_start, self.range_end)
            self.update()
            return
        clip = self.session.clip_by_id(self._drag["clip_id"])
        if clip is None:
            return
        before = self._drag["before"]
        delta = (point.x() - self._drag["origin"]) / self.pixels_per_second
        minimum = self.session.frame_duration
        if zone == "move":
            clip.start = self.snap_time(before["start"] + delta)
            index = int((point.y() - self.RULER_HEIGHT) // self.track_height)
            if 0 <= index < len(self.session.tracks):
                candidate = self.session.tracks[index]
                if getattr(candidate, "role", "") not in ("BUS", "MASTER"):
                    clip.track_id = candidate.id
        elif zone == "left":
            maximum = before["start"] + before["duration"] - minimum
            start = min(maximum, self.snap_time(max(0.0, before["start"] - before["offset"], before["start"] + delta)))
            shift = start - before["start"]
            clip.start, clip.duration, clip.offset = start, before["duration"] - shift, before["offset"] + shift
        elif zone == "right":
            end = self.snap_time(before["start"] + before["duration"] + delta)
            available = clip.source_duration - before["offset"] if clip.source_duration else float("inf")
            clip.duration = min(available, max(minimum, end - before["start"]))
        elif zone == "fade_in":
            clip.fade_in = min(clip.duration, self.snap_time(before["fade_in"] + delta))
        elif zone == "fade_out":
            clip.fade_out = min(clip.duration, self.snap_time(before["fade_out"] - delta))
        clip.fade_in = min(clip.fade_in, clip.duration)
        clip.fade_out = min(clip.fade_out, clip.duration)
        self.session.touch()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if self._drag and "clip_id" in self._drag:
            clip = self.session.clip_by_id(self._drag["clip_id"])
            before = self._drag["before"]
            if clip is not None:
                after = {key: getattr(clip, key) for key in before}
                if after != before:
                    self.session.commit_preview("编辑音频片段", self._drag["snapshot"])
        self._drag = None
        self.update()

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        clip, _ = self._hit(event.position())
        if clip is not None:
            self.session.select_clip(clip.id)
            self.clipEditorRequested.emit(clip.id)

    def copy_selected(self) -> None:
        clip = self.session.clip_by_id(self.session.selected_clip_id)
        if clip is not None:
            self._clipboard = copy.deepcopy(clip)
            self.session.status_changed.emit("已复制片段（本地 UI 状态）")

    def paste_clip(self) -> None:
        if self._clipboard is None:
            return
        clip = copy.deepcopy(self._clipboard)
        clip.id = uuid.uuid4().hex
        clip.start = self.session.playhead
        clip.effects = []
        self.session.commit("粘贴片段", lambda: self.session.clips.append(clip))
        self.session.select_clip(clip.id)

    def context_menu_for_clip(self, clip_id: str) -> QMenu:
        """Build without executing, allowing both keyboard use and focused tests."""
        menu = QMenu(self)
        clip = self.session.clip_by_id(clip_id)
        if clip is None:
            return menu
        self.session.select_clip(clip_id)
        menu.addAction("编辑片段…", lambda: self.clipEditorRequested.emit(clip_id))
        menu.addSeparator()
        menu.addAction("在播放头处分割", lambda: self.session.split_clip(clip_id, self.session.playhead)).setEnabled(not clip.locked)
        menu.addAction("复制", self.copy_selected)
        menu.addAction("重复片段", lambda: self.session.duplicate_clip(clip_id))
        menu.addAction("粘贴到播放头", self.paste_clip).setEnabled(self._clipboard is not None)
        menu.addSeparator()
        for label, key in (("片段静音", "muted"), ("锁定位置", "locked")):
            action = menu.addAction(label)
            action.setCheckable(True)
            action.setChecked(bool(getattr(clip, key)))
            action.triggered.connect(lambda checked, field=key: self.session.commit("更改片段状态", lambda: setattr(self.session.clip_by_id(clip_id), field, checked)))
        menu.addSeparator()
        menu.addAction("删除片段", lambda: self.session.delete_clip(clip_id)).setEnabled(not clip.locked)
        return menu

    def _show_context(self, point: QPointF, global_point: QPoint) -> None:
        clip, _ = self._hit(point)
        if clip is not None:
            menu = self.context_menu_for_clip(clip.id)
            menu.exec(global_point)
            menu.deleteLater()

    def wheelEvent(self, event: QWheelEvent) -> None:
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            panel = self.parent()
            while panel is not None and not isinstance(panel, TimelinePanel):
                panel = panel.parent()
            if isinstance(panel, TimelinePanel):
                change = 1 if event.angleDelta().y() > 0 else -1
                panel.zoom.setValue(panel.zoom.value() + change * 8)
            event.accept()
        else:
            super().wheelEvent(event)


class TimelinePanel(QWidget):
    """Dock-ready timeline; all edits are local UI state only."""

    clipEditorRequested = pyqtSignal(str)
    importRequested = pyqtSignal()

    def __init__(self, session: MixerUiSession, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.session = session
        self._syncing = False
        self.setObjectName("MixerTimeline")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setMinimumSize(480, 210)
        self.setStyleSheet(PANEL_QSS)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        toolbar = QFrame()
        toolbar.setObjectName("MixerToolbar")
        toolbar.setFixedHeight(32)
        tools = QHBoxLayout(toolbar)
        tools.setContentsMargins(8, 0, 8, 0)
        tools.setSpacing(3)
        self.tool_buttons: dict[str, QToolButton] = {}
        self.tool_icons = {"select": "select", "range": "trim", "razor": "blade"}
        for text, mode in (("选择 (V)", "select"), ("范围 (R)", "range"), ("剃刀 (B)", "razor")):
            color = EDIT_COLORS["accent"] if mode == "select" else IconColors.DEFAULT
            button = tool_button(text, timeline_icon(self.tool_icons[mode], color))
            button.setCheckable(True)
            button.setChecked(mode == "select")
            button.clicked.connect(lambda _checked, value=mode: self._set_tool(value))
            tools.addWidget(button)
            self.tool_buttons[mode] = button
        tools.addWidget(toolbar_divider())
        self.snap_button = tool_button("磁吸", timeline_icon("snap", EDIT_COLORS["accent"]))
        self.snap_button.setCheckable(True)
        self.snap_button.setChecked(session.snap_enabled)
        self.snap_button.toggled.connect(self._set_snap)
        tools.addWidget(self.snap_button)
        self.snap_combo = compact_combo()
        self.snap_combo.addItems(["1 帧", "5 帧", "10 帧", "1 秒"])
        self.snap_combo.setFixedWidth(69)
        self.snap_combo.currentIndexChanged.connect(self._set_snap_step)
        tools.addWidget(self.snap_combo)
        tools.addWidget(toolbar_divider())
        self.details_button = tool_button("片段参数", timeline_icon("view_options"))
        self.details_button.setCheckable(True)
        self.details_button.toggled.connect(lambda enabled: self.details_button.setIcon(
            timeline_icon("view_options", EDIT_COLORS["accent"] if enabled else IconColors.DEFAULT)))
        tools.addWidget(self.details_button)
        tools.addWidget(tool_button("导入素材", icon_folder_open(), self.importRequested.emit))
        tools.addStretch(1)
        tools.addWidget(tool_button("缩小时间线", timeline_icon("zoom_out"), lambda: self._step_zoom(-1)))
        self.zoom = QSlider(Qt.Orientation.Horizontal)
        self.zoom.setRange(5, 800)
        self.zoom.setValue(20)
        self.zoom.setFixedWidth(86)
        self.zoom.setToolTip("水平缩放（Ctrl+滚轮）")
        self.zoom.valueChanged.connect(self._set_zoom)
        tools.addWidget(self.zoom)
        tools.addWidget(tool_button("放大时间线", timeline_icon("zoom_in"), lambda: self._step_zoom(1)))
        tools.addWidget(tool_button("适配时间线", timeline_icon("zoom_full"), self.fit_all))
        height_button = tool_button("轨道高度", timeline_icon("dynamic_trim"))
        height_menu = QMenu(height_button)
        for label, height in (("紧凑", 52), ("标准", EDIT_SIZES["track_h_audio"]), ("展开", 84), ("大波形", 112)):
            height_menu.addAction(label, lambda _=False, value=height: self._set_track_height(value))
        height_button.setMenu(height_menu)
        height_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        tools.addWidget(height_button)
        layout.addWidget(toolbar)
        self.details = QFrame()
        detail_layout = QHBoxLayout(self.details)
        detail_layout.setContentsMargins(10, 3, 10, 5)
        detail_layout.setSpacing(7)
        self.clip_name = QLabel("未选择片段")
        self.clip_name.setMinimumWidth(30)
        self.clip_name.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        detail_layout.addWidget(self.clip_name, 1)
        detail_layout.addWidget(QLabel("起点"))
        self.start_edit = QLineEdit()
        self.start_edit.setFixedWidth(108)
        self.start_edit.editingFinished.connect(self._set_start)
        detail_layout.addWidget(self.start_edit)
        detail_layout.addWidget(QLabel("增益"))
        self.gain = QDoubleSpinBox()
        self.gain.setRange(-36, 24)
        self.gain.setDecimals(1)
        self.gain.setSuffix(" dB")
        self.gain.setFixedWidth(88)
        self.gain.editingFinished.connect(lambda: self._set_clip_value("gain_db", self.gain.value()))
        detail_layout.addWidget(self.gain)
        self.fade_in = self._fade_control(detail_layout, "淡入")
        self.fade_out = self._fade_control(detail_layout, "淡出")
        self.fade_in.editingFinished.connect(lambda: self._set_fade("fade_in", self.fade_in.value()))
        self.fade_out.editingFinished.connect(lambda: self._set_fade("fade_out", self.fade_out.value()))
        edit_button = QPushButton("编辑片段…")
        edit_button.clicked.connect(self._open_selected)
        detail_layout.addWidget(edit_button)
        self.details.hide()
        self.details_button.toggled.connect(self.details.setVisible)
        layout.addWidget(self.details)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.canvas = TimelineCanvas(session)
        self.canvas.clipEditorRequested.connect(self.clipEditorRequested.emit)
        self.canvas.rangeChanged.connect(self._range_changed)
        self.scroll.setWidget(self.canvas)
        layout.addWidget(self.scroll, 1)
        self.h_scroll = QScrollBar(Qt.Orientation.Horizontal)
        self.h_scroll.setSingleStep(30)
        self.h_scroll.valueChanged.connect(self.canvas.set_scroll)
        layout.addWidget(self.h_scroll)
        bottom = QHBoxLayout()
        bottom.setContentsMargins(10, 2, 10, 4)
        self.selection_label = QLabel("UI 原型 · 音频处理后端未连接")
        self.selection_label.setStyleSheet("color:#92929E;font-size:10px;")
        bottom.addWidget(self.selection_label, 1)
        self.cursor_label = QLabel(session.timecode(session.playhead))
        self.cursor_label.setStyleSheet("color:#B8B8C3;font-family:Menlo;font-size:10px;")
        bottom.addWidget(self.cursor_label)
        layout.addLayout(bottom)
        session.changed.connect(self._sync)
        session.selection_changed.connect(lambda _: self._sync())
        session.playhead_changed.connect(lambda value: self.cursor_label.setText(session.timecode(value)))
        session.timeline_format_changed.connect(self._sync)
        self._install_shortcuts()
        self._sync()

    def _fade_control(self, layout: QHBoxLayout, title: str) -> QDoubleSpinBox:
        layout.addWidget(QLabel(title))
        control = QDoubleSpinBox()
        control.setRange(0, 99999)
        control.setDecimals(0)
        control.setSuffix(" f")
        control.setFixedWidth(76)
        layout.addWidget(control)
        return control

    def _install_shortcuts(self) -> None:
        bindings = (("V", lambda: self._set_tool("select")), ("R", lambda: self._set_tool("range")),
                    ("B", lambda: self._set_tool("razor")), ("Delete", self._delete_selected),
                    ("Backspace", self._delete_selected), ("Ctrl+C", self.canvas.copy_selected),
                    ("Ctrl+V", self.canvas.paste_clip), ("Ctrl+D", self._duplicate_selected),
                    ("Ctrl+Z", self.session.undo_stack.undo), ("Ctrl+Shift+Z", self.session.undo_stack.redo))
        self._shortcuts: list[QShortcut] = []
        for key, callback in bindings:
            shortcut = QShortcut(QKeySequence(key), self)
            shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            shortcut.activated.connect(callback)
            self._shortcuts.append(shortcut)

    def _set_tool(self, tool: str) -> None:
        self.canvas.tool = tool
        for key, button in self.tool_buttons.items():
            button.blockSignals(True)
            button.setChecked(key == tool)
            color = EDIT_COLORS["accent"] if key == tool else IconColors.DEFAULT
            button.setIcon(timeline_icon(self.tool_icons[key], color))
            button.blockSignals(False)

    def _set_snap(self, enabled: bool) -> None:
        self.session.snap_enabled = enabled
        self.snap_button.setIcon(timeline_icon("snap", EDIT_COLORS["accent"] if enabled else IconColors.DEFAULT))

    def _set_snap_step(self, index: int) -> None:
        self.session.snap_frames = (1, 5, 10, max(1, round(self.session.fps)))[index]
        self.canvas.snap_seconds = 1.0 if index == 3 else None

    def _step_zoom(self, direction: int) -> None:
        scale = 1.25 if direction > 0 else 0.8
        self.zoom.setValue(round(self.zoom.value() * scale))

    def _set_track_height(self, height: int) -> None:
        self.canvas.track_height = height
        self.canvas._refresh()

    def _set_zoom(self, value: int) -> None:
        self.canvas.pixels_per_second = float(value)
        self._update_scroll_range()
        self.canvas.update()

    def _update_scroll_range(self) -> None:
        available = max(1, self.scroll.viewport().width() - self.canvas.HEADER_WIDTH)
        maximum = max(0, int(self.session.duration * self.canvas.pixels_per_second) - available)
        self.h_scroll.setRange(0, maximum)
        self.h_scroll.setPageStep(available)

    def fit_all(self) -> None:
        end = max([1.0] + [clip.start + clip.duration for clip in self.session.clips])
        available = max(1, self.scroll.viewport().width() - self.canvas.HEADER_WIDTH - 20)
        self.zoom.setValue(max(self.zoom.minimum(), min(self.zoom.maximum(), math.floor(available / end))))
        self.h_scroll.setValue(0)

    def resizeEvent(self, event: Any) -> None:
        super().resizeEvent(event)
        if hasattr(self, "canvas"):
            self._update_scroll_range()

    def _sync(self) -> None:
        self._syncing = True
        clip = self.session.clip_by_id(self.session.selected_clip_id)
        for widget in (self.start_edit, self.gain, self.fade_in, self.fade_out):
            widget.setEnabled(clip is not None)
        if clip is not None:
            self.clip_name.setText(clip.name)
            self.clip_name.setToolTip(clip.name)
            if not self.start_edit.hasFocus():
                self.start_edit.setText(self.session.rate.format(clip.start))
            self.gain.setValue(clip.gain_db)
            self.fade_in.setValue(clip.fade_in * self.session.fps)
            self.fade_out.setValue(clip.fade_out * self.session.fps)
        else:
            self.clip_name.setText("未选择片段")
            self.start_edit.clear()
        self.cursor_label.setText(self.session.timecode(self.session.playhead))
        self._update_scroll_range()
        self._syncing = False

    def _set_clip_value(self, key: str, value: float) -> None:
        if self._syncing:
            return
        clip = self.session.clip_by_id(self.session.selected_clip_id)
        if clip is None or (clip.locked and key == "start") or getattr(clip, key) == value:
            return
        clip_id = clip.id
        self.session.commit("更改片段参数", lambda: setattr(self.session.clip_by_id(clip_id), key, value))

    def _set_start(self) -> None:
        clip = self.session.clip_by_id(self.session.selected_clip_id)
        if clip is None:
            return
        try:
            value = self.session.seconds_from_timecode(self.start_edit.text())
        except ValueError:
            self.session.status_changed.emit("请输入有效时间码 HH:MM:SS:FF")
            self.start_edit.setText(self.session.rate.format(clip.start))
            return
        self._set_clip_value("start", max(0.0, value))

    def _set_fade(self, key: str, frames: float) -> None:
        clip = self.session.clip_by_id(self.session.selected_clip_id)
        if clip is not None:
            self._set_clip_value(key, min(clip.duration, max(0, round(frames)) * self.session.frame_duration))

    def _open_selected(self) -> None:
        if self.session.selected_clip_id:
            self.clipEditorRequested.emit(self.session.selected_clip_id)

    def _delete_selected(self) -> None:
        if self.session.selected_clip_id:
            self.session.delete_clip(self.session.selected_clip_id)

    def _duplicate_selected(self) -> None:
        if self.session.selected_clip_id:
            self.session.duplicate_clip(self.session.selected_clip_id)

    def _range_changed(self, start: float, end: float) -> None:
        self.session.loop_start, self.session.loop_end = start, end
        self.selection_label.setText(f"选区 {self.session.timecode(start)} → {self.session.timecode(end)}  ·  {(end - start):.3f} s")
