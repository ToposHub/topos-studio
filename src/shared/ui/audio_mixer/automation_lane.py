"""
Automation Lane（W22/W23）— 混音自动化曲线编辑器。

独立对话框式组件（不侵入时间轴渲染），显示某轨道某参数的关键帧曲线，
支持点击添加、拖动移动、右键删除、双击改插值类型。

W23 自动化模式（Read/Write/Touch/Latch/Off）的选择器也在此组件顶部。

信号链：
  AutomationLane.add_keyframe(str, str, int, float) → controller 提交 AddAutomationKeyframeCommand
  AutomationLane.remove_keyframe(str, str, int) → controller 提交 RemoveAutomationKeyframeCommand
  AutomationLane.clear_param(str, str) → controller 提交 ClearAutomationCommand
  AutomationLane.automation_mode_changed(str) → 全局模式切换（Read/Write/Touch/Latch/Off）
"""

from __future__ import annotations

from typing import Optional

from PyQt6.QtCore import Qt, pyqtSignal, QPointF
from PyQt6.QtGui import (
    QPainter, QPen, QColor, QBrush, QPainterPath, QPalette, QFont,
    QMouseEvent,
)
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QWidget,
    QSizePolicy, QComboBox,
)

from src.shared.keyframe import KeyframeTrack, Keyframe, InterpolationType
from src.shared.i18n.i18n import I18N


# 自动化模式枚举（W23）
AUTOMATION_MODES = ["Off", "Read", "Write", "Touch", "Latch"]


class AutomationCurveWidget(QWidget):
    """自动化曲线绘制 + 编辑控件。

    鼠标交互：
      - 左键点击空白：添加关键帧
      - 左键拖动关键帧：移动（value + frame）——拖动期纯本地预览，
        松手时一次性提交（AUD-37701）
      - 右键关键帧：删除
      - 双击关键帧：切换插值类型（linear → bezier → hold → linear）
    """

    # 信号：frame, value（线性值，未 clamp）
    keyframe_added = pyqtSignal(int, float)
    keyframe_moved = pyqtSignal(int, float, int, float)  # old_frame, old_val, new_frame, new_val
    keyframe_removed = pyqtSignal(int)  # frame
    keyframe_interpolation_changed = pyqtSignal(int, str)  # frame, interpolation_name

    def __init__(
        self,
        param_name: str,
        min_val: float,
        max_val: float,
        total_frames: int,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._param_name = param_name
        self._min_val = float(min_val)
        self._max_val = float(max_val)
        self._total_frames = int(total_frames)
        self._track: Optional[KeyframeTrack] = None
        self._dragging_kf_frame: Optional[int] = None  # 正在拖动的关键帧 frame
        self._hover_kf_frame: Optional[int] = None
        # AUD-37701: 拖动期本地预览状态。拖动期间不再逐 mouseMove 发
        # remove+add（会在鼠标经过的每个中间帧留下杂散关键帧，且每次移动
        # 提交 2 条命令刷爆撤销栈）；改为本地预览，mouseRelease 时一次性
        # 提交一对 remove(原帧)+add(终帧) 命令。
        self._drag_origin: Optional[tuple[int, float]] = None   # (frame, value) 拖动起点
        self._drag_preview: Optional[tuple[int, float]] = None  # (frame, value) 当前预览位置

        self.setMinimumSize(400, 120)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMouseTracking(True)
        # 右键用于删除，需自定义 contextMenuPolicy
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.DefaultContextMenu)

    def set_track(self, track: Optional[KeyframeTrack]) -> None:
        """设置要显示的 KeyframeTrack。"""
        self._track = track
        self.update()

    def set_total_frames(self, total: int) -> None:
        self._total_frames = max(1, int(total))
        self.update()

    # ── 坐标转换 ──

    def _frame_to_x(self, frame: int) -> float:
        w = self.width() - 40  # 左边距 40 给 value 轴标签
        return 40 + (frame / max(1, self._total_frames)) * w

    def _x_to_frame(self, x: float) -> int:
        w = self.width() - 40
        rel = (x - 40) / max(1, w)
        return max(0, min(self._total_frames, int(rel * self._total_frames)))

    def _value_to_y(self, value: float) -> float:
        h = self.height() - 20
        norm = (value - self._min_val) / max(1e-9, self._max_val - self._min_val)
        norm = max(0.0, min(1.0, norm))
        return 10 + (1.0 - norm) * h  # 上正下负

    def _y_to_value(self, y: float) -> float:
        h = self.height() - 20
        rel = (y - 10) / max(1, h)
        norm = 1.0 - max(0.0, min(1.0, rel))
        return self._min_val + norm * (self._max_val - self._min_val)

    # ── 绘制 ──

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        palette = self.palette()
        painter.fillRect(self.rect(), palette.color(QPalette.ColorRole.Window))

        # 网格
        grid_pen = QPen(QColor(120, 120, 120, 60), 1)
        painter.setPen(grid_pen)
        # 水平线（value 刻度）
        for i in range(5):
            y = 10 + i * (self.height() - 20) / 4
            painter.drawLine(40, int(y), self.width(), int(y))
        # 垂直线（frame 刻度）
        for i in range(11):
            x = 40 + i * (self.width() - 40) / 10
            painter.drawLine(int(x), 10, int(x), self.height() - 10)

        # 边框
        painter.setPen(QPen(palette.color(QPalette.ColorRole.Dark), 1))
        painter.drawRect(40, 10, self.width() - 40, self.height() - 20)

        # 曲线
        if self._track is None or not self._track.keyframes:
            painter.setPen(palette.color(QPalette.ColorRole.WindowText))
            font = QFont()
            font.setPointSize(10)
            painter.setFont(font)
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter,
                             I18N.tr("audio.mixer.automation.empty",
                                     "No automation — click to add keyframes"))
            return

        # 画插值曲线
        # AUD-37701: 拖动期间用预览位置替换原关键帧做本地渲染（模型不动）
        dragging = (self._dragging_kf_frame is not None and self._drag_preview is not None)
        kfs = sorted(self._track.keyframes, key=lambda k: k.frame)
        if dragging and self._drag_origin is not None:
            origin_kf = self._track.get_keyframe_at(self._drag_origin[0])
            preview_interp = (origin_kf.interpolation if origin_kf is not None
                              else InterpolationType.LINEAR)
            kfs = [k for k in kfs if k.frame != self._drag_origin[0]]
            kfs.append(Keyframe(
                frame=self._drag_preview[0],
                value=self._drag_preview[1],
                interpolation=preview_interp,
            ))
            kfs.sort(key=lambda k: k.frame)

        path = QPainterPath()
        if dragging:
            # 拖动期曲线为简单折线（模型未变，无法对预览点采样插值）
            for i, kf in enumerate(kfs):
                x = self._frame_to_x(kf.frame)
                y = self._value_to_y(kf.value)
                if i == 0:
                    path.moveTo(x, y)
                else:
                    path.lineTo(x, y)
        else:
            for i, kf in enumerate(kfs):
                x = self._frame_to_x(kf.frame)
                y = self._value_to_y(kf.value)
                if i == 0:
                    path.moveTo(x, y)
                else:
                    # 简化：用 KeyframeTrack 的 get_value 在中间帧采样画线
                    prev = kfs[i - 1]
                    steps = 20
                    for s in range(1, steps + 1):
                        mid_frame = prev.frame + (kf.frame - prev.frame) * s / steps
                        mid_val = self._track.get_value(int(mid_frame), kf.value)
                        path.lineTo(self._frame_to_x(int(mid_frame)), self._value_to_y(mid_val))

        curve_pen = QPen(QColor(80, 160, 240), 2)
        painter.setPen(curve_pen)
        painter.drawPath(path)

        # 画关键帧点
        for kf in kfs:
            x = self._frame_to_x(kf.frame)
            y = self._value_to_y(kf.value)
            is_hover = (kf.frame == self._hover_kf_frame)
            is_drag = dragging or (kf.frame == self._dragging_kf_frame)
            radius = 7 if (is_hover or is_drag) else 5
            color = QColor(255, 200, 60) if is_drag else (QColor(100, 200, 100) if is_hover else QColor(80, 160, 240))
            painter.setBrush(QBrush(color))
            painter.setPen(QPen(QColor(40, 40, 40), 1))
            painter.drawEllipse(QPointF(x, y), radius, radius)

    # ── 鼠标交互 ──

    def _find_keyframe_at(self, pos: QPointF, radius: float = 10.0) -> Optional[int]:
        """找到 pos 附近的关键帧 frame，无则 None。"""
        if self._track is None:
            return None
        for kf in self._track.keyframes:
            x = self._frame_to_x(kf.frame)
            y = self._value_to_y(kf.value)
            if (pos.x() - x) ** 2 + (pos.y() - y) ** 2 <= radius ** 2:
                return kf.frame
        return None

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            pos = event.position()
            kf_frame = self._find_keyframe_at(pos)
            if kf_frame is not None:
                # 开始拖动（AUD-37701: 记录拖动起点，移动期仅本地预览）
                self._dragging_kf_frame = kf_frame
                origin_val = self._min_val
                if self._track is not None:
                    kf = self._track.get_keyframe_at(kf_frame)
                    if kf is not None:
                        origin_val = kf.value
                self._drag_origin = (kf_frame, origin_val)
                self._drag_preview = None
            else:
                # 空白处：添加关键帧
                frame = self._x_to_frame(pos.x())
                value = self._y_to_value(pos.y())
                self.keyframe_added.emit(frame, value)
            self.update()
        elif event.button() == Qt.MouseButton.RightButton:
            # 右键：删除关键帧
            pos = event.position()
            kf_frame = self._find_keyframe_at(pos)
            if kf_frame is not None:
                self.keyframe_removed.emit(kf_frame)
                self.update()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        pos = event.position()
        if self._dragging_kf_frame is not None:
            # AUD-37701: 拖动期纯本地预览（模型不动、不发信号）。旧实现
            # 每次 mouseMove 发 remove(old)+add(new)：
            #   1) _dragging_kf_frame 从不更新，第一次移动后 remove 命中已
            #      不存在的帧（静默无操作），add 在鼠标经过的每个中间帧
            #      追加杂散关键帧，曲线被破坏；
            #   2) 每次移动提交 2 条命令，撤销栈被刷爆。
            self._drag_preview = (self._x_to_frame(pos.x()), self._y_to_value(pos.y()))
            self.update()
        else:
            # hover 检测
            new_hover = self._find_keyframe_at(pos)
            if new_hover != self._hover_kf_frame:
                self._hover_kf_frame = new_hover
                self.update()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            # AUD-37701: 松手才一次性提交移动（一对 remove+add 命令，
            # 不再逐 move 刷栈）；预览位置与起点重合则视为未移动，不发信号。
            if (self._dragging_kf_frame is not None
                    and self._drag_origin is not None
                    and self._drag_preview is not None):
                old_frame, old_val = self._drag_origin
                new_frame, new_val = self._drag_preview
                if new_frame != old_frame or abs(new_val - old_val) > 1e-9:
                    self.keyframe_moved.emit(old_frame, old_val, new_frame, new_val)
            self._dragging_kf_frame = None
            self._drag_origin = None
            self._drag_preview = None
            self.update()

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            pos = event.position()
            kf_frame = self._find_keyframe_at(pos)
            if kf_frame is not None and self._track is not None:
                # 切换插值类型
                kf = self._track.get_keyframe_at(kf_frame)
                if kf is not None:
                    order = [InterpolationType.LINEAR, InterpolationType.BEZIER, InterpolationType.HOLD]
                    try:
                        idx = order.index(kf.interpolation)
                    except ValueError:
                        idx = -1
                    new_interp = order[(idx + 1) % len(order)]
                    self.keyframe_interpolation_changed.emit(kf_frame, new_interp.name.lower())
                    self.update()


class AutomationLane(QDialog):
    """Automation Lane 对话框（W22）+ 模式选择器（W23）。

    显示某轨道某参数的 automation 曲线，可编辑。
    顶部有参数选择器（volume/pan/mute）+ 全局 automation 模式选择器。
    """

    # 信号
    add_keyframe = pyqtSignal(str, str, int, float)  # track_id, param, frame, value
    remove_keyframe = pyqtSignal(str, str, int)      # track_id, param, frame
    clear_param = pyqtSignal(str, str)               # track_id, param
    set_keyframe_interp = pyqtSignal(str, str, int, str)  # track_id, param, frame, interp_name
    automation_mode_changed = pyqtSignal(str)        # mode: Off/Read/Write/Touch/Latch

    # 参数配置（name → (min, max, default)）
    PARAM_CONFIG = {
        "volume": (0.0, 2.0, 1.0),
        "pan": (-1.0, 1.0, 0.0),
        "mute": (0.0, 1.0, 0.0),
    }

    def __init__(
        self,
        track_id: str,
        track_name: str,
        total_frames: int = 1000,
        initial_param: str = "volume",
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._track_id = track_id
        self._track_name = track_name
        self._total_frames = int(total_frames)
        self._current_param = initial_param
        # 各参数的 KeyframeTrack（由 controller 同步进来）
        self._tracks: dict[str, Optional[KeyframeTrack]] = {}

        self.setWindowTitle(I18N.tr(
            "audio.mixer.automation.title",
            "Automation — {name}"
        ).format(name=track_name))
        self.setMinimumSize(640, 300)
        self.setModal(False)

        self._init_ui()

    def _init_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        # ── 顶部：参数选择 + 模式选择 ──
        top = QHBoxLayout()

        top.addWidget(QLabel(I18N.tr("audio.mixer.automation.param", "Parameter:")))
        self._param_combo = QComboBox()
        self._param_combo.addItems(["volume", "pan", "mute"])
        self._param_combo.setCurrentText(self._current_param)
        self._param_combo.currentTextChanged.connect(self._on_param_changed)
        top.addWidget(self._param_combo)

        top.addSpacing(20)
        top.addWidget(QLabel(I18N.tr("audio.mixer.automation.mode", "Mode:")))
        self._mode_combo = QComboBox()
        self._mode_combo.addItems(AUTOMATION_MODES)
        self._mode_combo.setCurrentText("Off")
        self._mode_combo.currentTextChanged.connect(self.automation_mode_changed.emit)
        top.addWidget(self._mode_combo)

        top.addStretch()

        self._clear_btn = QPushButton(I18N.tr("audio.mixer.automation.clear", "Clear All"))
        self._clear_btn.clicked.connect(self._on_clear)
        top.addWidget(self._clear_btn)

        self._close_btn = QPushButton(I18N.tr("common.close", "Close"))
        self._close_btn.clicked.connect(self.accept)
        top.addWidget(self._close_btn)

        layout.addLayout(top)

        # ── 曲线编辑器 ──
        min_v, max_v, _ = self.PARAM_CONFIG.get(self._current_param, (0.0, 1.0, 0.0))
        self._curve = AutomationCurveWidget(
            self._current_param, min_v, max_v, self._total_frames, self
        )
        # 连接曲线信号
        self._curve.keyframe_added.connect(self._on_kf_added)
        self._curve.keyframe_moved.connect(self._on_kf_moved)
        self._curve.keyframe_removed.connect(self._on_kf_removed)
        self._curve.keyframe_interpolation_changed.connect(self._on_kf_interp)
        layout.addWidget(self._curve, 1)

        # ── 底部提示 ──
        hint = QLabel(I18N.tr(
            "audio.mixer.automation.hint",
            "Left-click: add keyframe | Drag: move | Right-click: delete | Double-click: change interpolation"
        ))
        hint.setWordWrap(True)
        layout.addWidget(hint)

    def _on_param_changed(self, param: str) -> None:
        self._current_param = param
        min_v, max_v, _ = self.PARAM_CONFIG.get(param, (0.0, 1.0, 0.0))
        self._curve._param_name = param
        self._curve._min_val = min_v
        self._curve._max_val = max_v
        self._curve.set_track(self._tracks.get(param))
        self._curve.update()

    def _on_clear(self) -> None:
        self.clear_param.emit(self._track_id, self._current_param)

    def _on_kf_added(self, frame: int, value: float) -> None:
        self.add_keyframe.emit(self._track_id, self._current_param, frame, value)

    def _on_kf_moved(self, old_frame: int, old_val: float, new_frame: int, new_val: float) -> None:
        # 移动 = remove old + add new（AUD-37701 后：仅在拖动结束时发射一次，
        # 拖动过程为本地预览）。两次命令的原子化（单条 Move 命令，可基于
        # EditTrack.with_automation_keyframe_updated）需 controller 侧新增
        # 命令类与接线，超出本组件职责范围。
        self.remove_keyframe.emit(self._track_id, self._current_param, old_frame)
        self.add_keyframe.emit(self._track_id, self._current_param, new_frame, new_val)

    def _on_kf_removed(self, frame: int) -> None:
        self.remove_keyframe.emit(self._track_id, self._current_param, frame)

    def _on_kf_interp(self, frame: int, interp: str) -> None:
        self.set_keyframe_interp.emit(self._track_id, self._current_param, frame, interp)

    # ── 外部同步（controller 调用）──

    def update_track_data(self, automation: dict) -> None:
        """从 EditTrack.automation 同步曲线显示。

        Args:
            automation: {param: KeyframeTrack}
        """
        self._tracks = {p: automation.get(p) for p in ["volume", "pan", "mute"]}
        self._curve.set_track(self._tracks.get(self._current_param))
        self._curve.update()

    def set_total_frames(self, total: int) -> None:
        self._total_frames = max(1, int(total))
        self._curve.set_total_frames(self._total_frames)
