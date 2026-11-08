"""W03：Edit 域 Keyframe Tray（接线计划契约 D5）。

TimelinePanel 托管在时间线视图下方的参数轨面板：
- 每个 clip 级 ``animated_params`` 参数一条泳道，菱形关键帧按
  **timeline 帧** 域定位（模型真值，见 ``AnimatableParam.get_value_at_frame``
  的 timeline frame 语义；clip 移动时关键帧经 ``shifted`` 同步）。
- 投影（选区/播放头/时间线变化）只重绘、零命令；拖动为预览，
  释放才提交（时间移动 = Remove+Add 宏，单步 undo）；Esc/换选区取消。
- 命令复用既有 Add/Remove Keyframe 命令，经 TimelineView.execute_command
  提交（revision/undo/增量刷新走既有边界）。效果参数轨与曲线编辑留待
  后续批次（见计划 W03）。
"""

from __future__ import annotations

from typing import Any, Optional

from PyQt6.QtCore import QPoint, QPointF, QRectF, Qt, pyqtSignal
from PyQt6.QtGui import QBrush, QColor, QPainter, QPen
from PyQt6.QtWidgets import QFrame, QWidget

from src.shared.i18n.i18n import I18N

_LANE_HEIGHT = 24
_GUTTER_WIDTH = 118
_DIAMOND = 5.0


class EditKeyframeTray(QFrame):
    """Clip-level keyframe lanes projected from the committed model."""

    #: 用户请求把 Tray 收起（宿主回写工具栏按钮）
    close_requested = pyqtSignal()

    def __init__(self, timeline_view: Any, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._view = timeline_view
        self._clip_id: Optional[str] = None
        self._track_id: Optional[str] = None
        self._lanes: list[tuple[str, list[tuple[int, float]]]] = []
        # 拖动会话：预览态，释放才建命令
        self._drag_param: Optional[str] = None
        self._drag_from_frame: Optional[int] = None
        self._drag_to_frame: Optional[int] = None
        self._drag_value: float = 0.0
        self._selected: Optional[tuple[str, int]] = None

        self.setFixedHeight(6 + _LANE_HEIGHT * 2 + 4)  # 标题行 + 2 泳道起步
        self.setMinimumHeight(6 + _LANE_HEIGHT + 2)
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setToolTip(I18N.tr(
            "timeline.keyframe_tray_hint",
            "Click to add a keyframe; drag to move in time; "
            "right-click to remove; Esc cancels the drag"))
        self._connect_view()

    # ---------------- projection (no commands) ----------------

    def _connect_view(self) -> None:
        """Projection-only wiring: model/selection changes repaint the tray."""
        view = self._view
        if view is None:
            return
        try:
            view.selection_changed.connect(self.refresh_from_model)
            view.timeline_changed.connect(lambda _tl: self.refresh_from_model())
            view.playhead_changed.connect(self._on_playhead)
            bar = view.horizontalScrollBar()
            if bar is not None:
                bar.valueChanged.connect(self.update)
            if hasattr(view, "zoom_changed"):
                view.zoom_changed.connect(lambda _ppf: self.update())
        except (AttributeError, TypeError):
            pass

    def _on_playhead(self, _frame: int) -> None:
        self.update()

    def current_clip(self) -> tuple[Optional[str], Optional[str]]:
        """(track_id, clip_id)：选中优先，回退播放头下 clip。"""
        view = self._view
        timeline = view.get_timeline() if view is not None else None
        if timeline is None:
            return (None, None)
        selected = list(getattr(view._selection, "selected_clip_ids", ()) or ())
        if len(selected) == 1:
            for track in (list(timeline.video_tracks)
                          + list(timeline.audio_tracks)):
                clip = track.get_clip_by_id(selected[0])
                if clip is not None:
                    return (track.track_id, clip.clip_id)
            return (None, None)
        if not selected:
            frame = view.get_playhead_frame()
            for track in (list(timeline.video_tracks)
                          + list(timeline.audio_tracks)):
                for clip in track.clips:
                    if (clip.timeline_start <= frame
                            < clip.timeline_start + clip.timeline_duration):
                        return (track.track_id, clip.clip_id)
        return (None, None)

    def refresh_from_model(self) -> None:
        """重投影：从提交后的模型读参数轨（不产生任何写入）。"""
        self._cancel_drag()
        self._track_id, self._clip_id = self.current_clip()
        self._lanes = []
        timeline = self._view.get_timeline() if self._view else None
        if timeline is not None and self._clip_id is not None:
            clip = timeline.get_clip_by_id(self._clip_id)
            if clip is not None:
                for param_name in sorted(clip.animated_params.keys()):
                    param = clip.animated_params[param_name]
                    track = getattr(param, "keyframes", None)
                    keys = [
                        (int(k.frame), float(k.value))
                        for k in (track.keyframes if track else ())
                    ]
                    self._lanes.append((param_name, keys))
        self.setFixedHeight(6 + _LANE_HEIGHT * max(1, len(self._lanes)) + 4)
        self.update()

    # ---------------- coordinate mapping ----------------

    def _frame_to_x(self, frame: int) -> float:
        view = self._view
        ppf = float(getattr(view, "_pixels_per_frame", 1.0) or 1.0)
        offset = 0.0
        bar = view.horizontalScrollBar() if view is not None else None
        if bar is not None:
            offset = float(bar.value())
        return _GUTTER_WIDTH + frame * ppf - offset

    def _x_to_frame(self, x: float) -> int:
        view = self._view
        ppf = float(getattr(view, "_pixels_per_frame", 1.0) or 1.0)
        if ppf <= 0:
            return 0
        bar = view.horizontalScrollBar() if view is not None else None
        offset = float(bar.value()) if bar is not None else 0.0
        return int(round((x - _GUTTER_WIDTH + offset) / ppf))

    def _clip_extent(self) -> Optional[tuple[int, int]]:
        timeline = self._view.get_timeline() if self._view else None
        if timeline is None or self._clip_id is None:
            return None
        clip = timeline.get_clip_by_id(self._clip_id)
        if clip is None:
            return None
        return (clip.timeline_start, clip.timeline_start + clip.timeline_duration)

    def _lane_at(self, y: float) -> Optional[int]:
        index = int((y - 4.0) // _LANE_HEIGHT)
        if 0 <= index < len(self._lanes):
            return index
        return None

    # ---------------- painting ----------------

    def paintEvent(self, event: Any) -> None:
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        w = self.width()
        h = self.height()

        bg = QColor(24, 26, 30)
        lane_bg = QColor(30, 33, 38)
        gutter_bg = QColor(20, 22, 25)
        painter.fillRect(0, 0, w, h, bg)

        extent = self._clip_extent()
        if extent is not None:
            x0 = max(self._frame_to_x(extent[0]), _GUTTER_WIDTH)
            x1 = min(self._frame_to_x(extent[1]), w)
            if x1 > x0:
                painter.fillRect(QRectF(x0, 0, x1 - x0, h),
                                 QColor(38, 44, 52))

        playhead_x = self._frame_to_x(
            self._view.get_playhead_frame() if self._view else 0)
        for index, (param_name, keys) in enumerate(self._lanes):
            top = 4.0 + index * _LANE_HEIGHT
            rect = QRectF(_GUTTER_WIDTH, top, w - _GUTTER_WIDTH,
                          _LANE_HEIGHT - 2)
            painter.fillRect(rect, lane_bg)
            painter.fillRect(QRectF(0, top, _GUTTER_WIDTH, _LANE_HEIGHT - 2),
                             gutter_bg)
            painter.setPen(QPen(QColor(120, 128, 138), 1))
            painter.drawText(QRectF(6, top, _GUTTER_WIDTH - 10,
                                    _LANE_HEIGHT - 2),
                             Qt.AlignmentFlag.AlignVCenter
                             | Qt.AlignmentFlag.AlignLeft, param_name)
            # 值折线（轻量：直线段）
            if len(keys) >= 2:
                painter.setPen(QPen(QColor(90, 160, 200, 160), 1))
                prev: Optional[QPointF] = None
                for frame, _value in keys:
                    point = QPointF(self._frame_to_x(frame),
                                    rect.center().y())
                    if prev is not None:
                        painter.drawLine(prev, point)
                    prev = point
            for frame, _value in keys:
                self._paint_diamond(painter, rect, param_name, frame)

        # 播放头
        if _GUTTER_WIDTH <= playhead_x <= w:
            painter.setPen(QPen(QColor(230, 120, 60), 1))
            painter.drawLine(QPointF(playhead_x, 0), QPointF(playhead_x, h))
        painter.end()

    def _paint_diamond(self, painter: QPainter, rect: QRectF,
                       param_name: str, frame: int) -> None:
        x = self._frame_to_x(frame)
        if not _GUTTER_WIDTH <= x <= self.width():
            return
        y = rect.center().y()
        is_drag = (self._drag_param == param_name
                   and self._drag_from_frame == frame)
        is_selected = (self._selected == (param_name, frame))
        color = QColor(240, 200, 90) if (is_drag or is_selected) \
            else QColor(170, 190, 210)
        painter.setBrush(QBrush(color))
        painter.setPen(QPen(QColor(10, 10, 12), 1))
        painter.drawPolygon(
            QPointF(x, y - _DIAMOND), QPointF(x + _DIAMOND, y),
            QPointF(x, y + _DIAMOND), QPointF(x - _DIAMOND, y))
        # 拖动预览：目标位置画幽灵菱形
        if is_drag and self._drag_to_frame is not None:
            gx = self._frame_to_x(self._drag_to_frame)
            ghost = QColor(240, 200, 90, 130)
            painter.setBrush(QBrush(ghost))
            painter.drawPolygon(
                QPointF(gx, y - _DIAMOND), QPointF(gx + _DIAMOND, y),
                QPointF(gx, y + _DIAMOND), QPointF(gx - _DIAMOND, y))

    # ---------------- interaction ----------------

    def _key_at(self, lane_index: int, frame: int,
                tolerance: float = _DIAMOND + 3.0) -> Optional[int]:
        _param, keys = self._lanes[lane_index]
        for key_frame, _value in keys:
            if abs(self._frame_to_x(key_frame)
                   - self._frame_to_x(frame)) <= tolerance:
                return key_frame
        return None

    def mousePressEvent(self, event: Any) -> None:
        pos = event.position()
        if pos.x() < _GUTTER_WIDTH:
            return
        lane_index = self._lane_at(pos.y())
        if lane_index is None:
            return
        param_name, _keys = self._lanes[lane_index]
        frame = self._x_to_frame(pos.x())
        hit = self._key_at(lane_index, frame)
        if event.button() == Qt.MouseButton.RightButton and hit is not None:
            self._remove_keyframe(param_name, hit)
            return
        if event.button() != Qt.MouseButton.LeftButton:
            return
        if hit is not None:
            # 开始拖动（预览态，不建命令）
            self._drag_param = param_name
            self._drag_from_frame = hit
            self._drag_to_frame = hit
            for frame_, value in _keys:
                if frame_ == hit:
                    self._drag_value = value
            self._selected = (param_name, hit)
        else:
            self._add_keyframe(param_name, frame)
        self.update()

    def mouseMoveEvent(self, event: Any) -> None:
        if self._drag_param is None:
            return
        pos = event.position()
        frame = self._x_to_frame(pos.x())
        extent = self._clip_extent()
        if extent is not None:
            frame = max(extent[0], min(extent[1] - 1, frame))
        if frame != self._drag_from_frame:
            self._drag_to_frame = frame
            self.update()

    def mouseReleaseEvent(self, event: Any) -> None:
        if (self._drag_param is None or self._drag_from_frame is None
                or self._drag_to_frame is None):
            return
        if event.button() == Qt.MouseButton.LeftButton:
            self._commit_drag()
        else:
            self._cancel_drag()
        self.update()

    def keyPressEvent(self, event: Any) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self._cancel_drag()
            self.update()
            return
        if (event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace)
                and self._selected is not None):
            param_name, frame = self._selected
            self._remove_keyframe(param_name, frame)
            return
        super().keyPressEvent(event)

    def _cancel_drag(self) -> None:
        self._drag_param = None
        self._drag_from_frame = None
        self._drag_to_frame = None

    # ---------------- command bridges (single undo each) ----------------

    def _add_keyframe(self, param_name: str, frame: int) -> None:
        from src.features.edit.core.commands import AddKeyframeCommand

        timeline = self._view.get_timeline()
        if timeline is None or self._clip_id is None:
            return
        clip = timeline.get_clip_by_id(self._clip_id)
        if clip is None:
            return
        extent = self._clip_extent()
        if extent is not None:
            frame = max(extent[0], min(extent[1] - 1, frame))
        param = clip.animated_params.get(param_name)
        value = (param.get_value_at_frame(frame)
                 if param is not None else 0.0)
        cmd = AddKeyframeCommand.create(
            self._track_id, self._clip_id, param_name, frame,
            value=float(value))
        try:
            self._view.execute_command(cmd)
        except ValueError:
            pass  # 已有同帧关键帧等——保持零写入
        self.refresh_from_model()

    def _remove_keyframe(self, param_name: str, frame: int) -> None:
        from src.features.edit.core.commands import RemoveKeyframeCommand

        if self._track_id is None or self._clip_id is None:
            return
        cmd = RemoveKeyframeCommand.create(
            self._track_id, self._clip_id, param_name, frame)
        try:
            self._view.execute_command(cmd)
        except ValueError:
            return
        self._selected = None
        self.refresh_from_model()

    def _commit_drag(self) -> None:
        """释放：时间移动 = Remove(旧帧)+Add(新帧) 宏，单步 undo。"""
        from src.features.edit.core.commands import (
            AddKeyframeCommand, MacroCommand, RemoveKeyframeCommand,
        )

        if (self._drag_param is None or self._drag_from_frame is None
                or self._drag_to_frame is None
                or self._drag_from_frame == self._drag_to_frame):
            self._cancel_drag()
            return
        if self._track_id is None or self._clip_id is None:
            self._cancel_drag()
            return
        remove = RemoveKeyframeCommand.create(
            self._track_id, self._clip_id, self._drag_param,
            self._drag_from_frame)
        add = AddKeyframeCommand.create(
            self._track_id, self._clip_id, self._drag_param,
            self._drag_to_frame, value=float(self._drag_value))
        macro = MacroCommand.create("Move keyframe", (remove, add))
        try:
            self._view.execute_command(macro)
        except ValueError:
            self._cancel_drag()
            self.refresh_from_model()
            return
        self._selected = (self._drag_param, self._drag_to_frame)
        self._cancel_drag()
        self.refresh_from_model()
