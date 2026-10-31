from PyQt6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, QFrame, QGroupBox,
                             QScrollArea, QPushButton)
from src.shared.i18n.i18n import I18N
from src.shared.ui.style_sheet import (
    PANEL_TITLE_LABEL_STYLE,
    SCROLL_AREA_TRANSPARENT_STYLE, TRANSPARENT_STYLE)
from PyQt6.QtCore import Qt, pyqtSignal, QPointF, QSize, QTimer
from PyQt6.QtGui import QPainter, QPen, QColor, QBrush, QPainterPath, QPalette
from src.shared.ui.widgets.param_slider_row import ParamSliderRow
from src.shared.ui.widgets.toggle_switch import ToggleSwitch
from src.shared.assets.icon_manager import icon_reset
import math

# ==================== 曲线可视化器 ====================
# 用于显示 Soft Clip 映射曲线的自定义小部件
class CurveVisualizer(QWidget):
    """
    自定义小部件，用于可视化 Soft Clip 映射曲线
    """
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.params = {}
        self.setBackgroundRole(QPalette.ColorRole.Base)
        self.setMinimumHeight(100)  # 最小高度
        self.setMinimumWidth(100)   # 最小宽度

    def set_params(self, params) -> None:
        self.params = params
        self.update()

    # 绘制曲线的主方法
    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)  # 开启抗锯齿

        # 1. 绘制背景 - 深色背景 #050505
        rect = self.rect().toRectF()
        bg_path = QPainterPath()
        bg_path.addRoundedRect(rect, 4, 4)  # 圆角矩形
        painter.fillPath(bg_path, QBrush(QColor("#050505")))

        # 定义绘图区域（带边距）
        # 左:45, 右:20, 上:20, 下:30
        margin_l, margin_r = 45, 20
        margin_t, margin_b = 20, 30
        draw_w = rect.width() - margin_l - margin_r
        draw_h = rect.height() - margin_t - margin_b
        
        # 坐标系统映射: X[-0.5, 2.0], Y[-0.2, 1.2]
        def to_screen_x(x_val):
            return margin_l + ((x_val - (-0.5)) / 2.5) * draw_w
        def to_screen_y(y_val):
            return margin_t + ((1.2 - y_val) / 1.4) * draw_h

        # 2. 绘制网格和参考线
        # 绝对黑 (0.0) - 灰色实线
        black_pen = QPen(QColor("#666"), 1, Qt.PenStyle.SolidLine)
        painter.setPen(black_pen)
        x0_pos = to_screen_x(0.0)
        y0_pos = to_screen_y(0.0)
        painter.drawLine(QPointF(x0_pos, margin_t), QPointF(x0_pos, margin_t + draw_h))
        painter.drawLine(QPointF(margin_l, y0_pos), QPointF(margin_l + draw_w, y0_pos))
        
        # 标准白 (1.0) - 浅蓝色/深灰色实线
        white_pen = QPen(QColor("#557799"), 1, Qt.PenStyle.SolidLine)
        painter.setPen(white_pen)
        x1_pos = to_screen_x(1.0)
        y1_pos = to_screen_y(1.0)
        painter.drawLine(QPointF(x1_pos, margin_t), QPointF(x1_pos, margin_t + draw_h))
        painter.drawLine(QPointF(margin_l, y1_pos), QPointF(margin_l + draw_w, y1_pos))

        # 3. 绘制坐标轴标签和刻度
        text_pen = QPen(QColor("#888"))
        painter.setPen(text_pen)
        font = painter.font()
        font.setPointSize(8)
        painter.setFont(font)
        
        # 绝对黑标记
        painter.drawText(int(x0_pos + 5), int(y0_pos - 5), "0.0 (Black)")
        # 标准白标记
        painter.setPen(QPen(QColor("#55aaff")))
        painter.drawText(int(x1_pos + 5), int(y1_pos - 5), "1.0 (White)")
        
        # Y轴标记
        painter.setPen(text_pen)
        painter.drawText(int(margin_l - 30), int(y0_pos + 4), "0.0")
        painter.drawText(int(margin_l - 30), int(y1_pos + 4), "1.0")

        # 输出标签 (垂直)
        painter.save()
        painter.translate(10, margin_t + 20)
        painter.rotate(-90)
        painter.drawText(0, 0, "Output")
        painter.restore()
        
        # 输入标签 (水平)
        painter.drawText(int(rect.width() - 50), int(rect.height() - 5), "Input (X)")

        # 4. 计算并绘制曲线
        if not self.params:
            return

        # 曲线颜色 #ff8800 (橙色), 宽度2
        curve_pen = QPen(QColor("#ff8800"), 2, Qt.PenStyle.SolidLine)
        painter.setPen(curve_pen)
        
        path = QPainterPath()
        first = True
        
        # 采样点数，越多曲线越平滑
        samples = 200
        enabled = self.params.get("enabled", False)
        
        # 获取参数
        exp = self.params.get("exposure", 0.0)           # 曝光
        contrast = self.params.get("contrast", 0.0)       # 对比度
        high = self.params.get("high", 1.0)              # 高光阈值
        white_point = self.params.get("white_point", 1.1) # 白点
        shoulder_strength = self.params.get("shoulder_strength", 1.0)  # 肩部强度
        low = self.params.get("low", 0.0)               # 暗部阈值
        black_point = self.params.get("black_point", -0.1) # 黑点
        toe_strength = self.params.get("toe_strength", 1.0)  # 趾部强度

        # 对比度因子映射
        if contrast >= 0:
            contrast_factor = 1.0 + contrast
        else:
            contrast_factor = 1.0 / (1.0 + abs(contrast))

        for i in range(samples + 1):
            x_val = -0.5 + (i / samples) * 2.5
            
            if enabled:
                y_val = x_val
                
                # 1. 曝光调整
                y_val *= math.pow(2.0, exp)
                
                # 2. 幂律对比度 (Pivot 0.18)
                if contrast != 0.0:
                    y_val = math.copysign(1.0, y_val) * math.pow(abs(y_val) / 0.18, contrast_factor) * 0.18
                
                # 3. 肩部 (高光压缩)
                if y_val > high and white_point > high:
                    T, W = high, white_point
                    k = max(shoulder_strength, 0.01)
                    diff, rng = y_val - T, W - T
                    y_val = W - rng * math.pow(1.0 + k * diff / rng, -1.0 / k)
                
                # 4. 趾部 (暗部压缩)
                if y_val < low and black_point < low:
                    T, B = low, black_point
                    k = max(toe_strength, 0.01)
                    diff, rng = T - y_val, T - B
                    y_val = B + rng * math.pow(1.0 + k * diff / rng, -1.0 / k)
            else:
                y_val = x_val
            
            # 映射到屏幕坐标
            screen_x = to_screen_x(x_val)
            screen_y = to_screen_y(y_val)
            
            if first:
                path.moveTo(screen_x, screen_y)
                first = False
            else:
                path.lineTo(screen_x, screen_y)
        
        painter.drawPath(path)


# ==================== 柔化裁切面板主类 ====================
class SoftClipPanel(QWidget):
    # 参数变化信号
    params_changed = pyqtSignal(dict)
    # 交互生命周期信号
    interaction_started = pyqtSignal()
    interaction_ended = pyqtSignal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._syncing_ui = False
        # Keep the preview responsive without rebuilding render parameters for
        # every physical mouse-move event.
        self._params_timer = QTimer(self)
        self._params_timer.setSingleShot(True)
        self._params_timer.setInterval(16)
        self._params_timer.timeout.connect(self._emit_params_now)
        self.init_ui()

    # ==================== 样式表 ====================
    def get_style_sheet(self):
        """Return panel-specific style sheet.
        
        QGroupBox/QDoubleSpinBox/QPushButton use global DARK_THEME styles.
        """
        return """
            SoftClipPanel {
                background-color: #1a1a1a;
            }
        """

    # ==================== UI 初始化 ====================
    def init_ui(self) -> None:
        self.setStyleSheet(self.get_style_sheet())
        self.setMinimumHeight(220)  # 面板最小高度
        
        # 主布局 - 水平布局，左右分栏
        main_layout = QHBoxLayout(self)
        # 边距: 左, 上, 右, 下 (全部为4px，紧凑布局)
        main_layout.setContentsMargins(4, 4, 4, 4)
        main_layout.setSpacing(4)  # 元素间距

        # --- 左侧: 控件区域 (可滚动) ---
        scroll_area = QScrollArea()
        scroll_area.setWidgetResizable(True)  # 自动调整大小
        scroll_area.setMinimumWidth(200)     # 最小宽度
        scroll_area.setFrameShape(QFrame.Shape.NoFrame)  # 无边框
        scroll_area.setStyleSheet(SCROLL_AREA_TRANSPARENT_STYLE)
        
        left_container = QWidget()
        left_container.setStyleSheet(TRANSPARENT_STYLE)
        controls_layout = QVBoxLayout(left_container)  # 垂直布局
        controls_layout.setContentsMargins(4, 4, 4, 4)
        controls_layout.setSpacing(4)

        # 标题栏 - 启用开关 + 重置按钮
        header_layout = QHBoxLayout()
        self.enable_check = ToggleSwitch(checked=False)
        self.enable_check.toggled.connect(self.emit_params)
        header_layout.addWidget(self.enable_check)

        self._title_label = QLabel(I18N.tr("soft_clip.title", "软裁切"))
        self._title_label.setStyleSheet(PANEL_TITLE_LABEL_STYLE)
        header_layout.addWidget(self._title_label)
        
        header_layout.addStretch()  # 弹簧，将按钮推到右边
        
        # 重置按钮 - 使用 icon_manager
        self.reset_btn = QPushButton()
        self.reset_btn.setObjectName("reset_btn")
        self.reset_btn.setIcon(icon_reset())
        self.reset_btn.setIconSize(QSize(12, 12))
        self.reset_btn.setFixedSize(16, 16)  # 固定大小 16x16
        self.reset_btn.setToolTip(I18N.tr(
            "soft_clip.reset_tooltip", "Reset All Parameters"))
        self.reset_btn.clicked.connect(self.reset_all)
        header_layout.addWidget(self.reset_btn)
        controls_layout.addLayout(header_layout)

        # --- 第一栏: 高光/肩部 (HIGH / SHOULDER) ---
        col1 = QGroupBox(I18N.tr("soft_clip.high_shoulder", "高光/肩部"))
        col1_layout = QVBoxLayout()
        # 内容边距: 左0, 上0, 右4, 下4 (紧凑)
        col1_layout.setContentsMargins(0, 0, 4, 4)
        col1_layout.setSpacing(1)  # 参数间距1px (紧凑)
        col1.setLayout(col1_layout)

        # 参数1: High Threshold (高光阈值) - 范围0.0-2.0, 默认1.0
        self.high_control = self._create_row(I18N.tr("soft_clip.high_threshold", "高光阈值"), 0.0, 2.0, 1.0, 0.01, label_width=90)
        col1_layout.addWidget(self.high_control)
        
        # 参数2: White Point (白点) - 范围1.0-5.0, 默认1.1
        self.white_point_control = self._create_row(I18N.tr("soft_clip.white_point", "白点"), 1.0, 5.0, 1.1, 0.01, label_width=90)
        col1_layout.addWidget(self.white_point_control)
        
        # 参数3: Shoulder (肩部强度) - 范围0.05-20.0, 默认1.0
        self.shoulder_strength_control = self._create_row(I18N.tr("soft_clip.shoulder", "肩部强度"), 0.05, 20.0, 1.0, 0.01, label_width=90)
        col1_layout.addWidget(self.shoulder_strength_control)

        col1_layout.addStretch()
        controls_layout.addWidget(col1)

        # --- 第二栏: 暗部/趾部 (LOW / TOE) ---
        col2 = QGroupBox(I18N.tr("soft_clip.low_toe", "暗部/趾部"))
        col2_layout = QVBoxLayout()
        col2_layout.setContentsMargins(0, 0, 4, 4)
        col2_layout.setSpacing(1)
        col2.setLayout(col2_layout)

        # 参数4: Low Threshold (暗部阈值) - 范围-0.5-0.5, 默认0.0
        self.low_control = self._create_row(I18N.tr("soft_clip.low_threshold", "暗部阈值"), -0.5, 0.5, 0.0, 0.01, label_width=90)
        col2_layout.addWidget(self.low_control)
        
        # 参数5: Black Point (黑点) - 范围-1.0-0.1, 默认-0.1
        self.black_point_control = self._create_row(I18N.tr("soft_clip.black_point", "黑点"), -1.0, 0.1, -0.1, 0.01, label_width=90)
        col2_layout.addWidget(self.black_point_control)
        
        # 参数6: Toe (趾部强度) - 范围0.05-20.0, 默认1.0
        self.toe_strength_control = self._create_row(I18N.tr("soft_clip.toe", "趾部强度"), 0.05, 20.0, 1.0, 0.01, label_width=90)
        col2_layout.addWidget(self.toe_strength_control)

        col2_layout.addStretch()
        controls_layout.addWidget(col2)

        # --- 第三栏: 全局/色调 (GLOBAL / TONE) ---
        col3 = QGroupBox(I18N.tr("soft_clip.global_tone", "全局/色调"))
        col3_layout = QVBoxLayout()
        col3_layout.setContentsMargins(0, 0, 4, 4)
        col3_layout.setSpacing(1)
        col3.setLayout(col3_layout)

        # 参数7: Exposure (曝光) - 范围-5.0-5.0, 默认0.0
        self.exposure_control = self._create_row(I18N.tr("soft_clip.exposure", "曝光"), -5.0, 5.0, 0.0, 0.01, label_width=90)
        col3_layout.addWidget(self.exposure_control)
        
        # 参数8: Contrast (对比度) - 范围-4.0-2.0, 默认0.0
        self.contrast_control = self._create_row(I18N.tr("soft_clip.contrast", "对比度"), -4.0, 2.0, 0.0, 0.01, label_width=90)
        col3_layout.addWidget(self.contrast_control)

        col3_layout.addStretch()
        controls_layout.addWidget(col3)

        controls_layout.addStretch()  # 底部弹簧
        
        scroll_area.setWidget(left_container)
        main_layout.addWidget(scroll_area, stretch=1)  # 左侧占比1

        # --- 右侧: 曲线可视化器 ---
        self.curve_viz = CurveVisualizer()
        main_layout.addWidget(self.curve_viz, stretch=1)  # 右侧占比1

        # 初始化可视化器参数 (不 emit 外部信号)
        params = {
            "enabled": False,
            "exposure": 0.0,
            "contrast": 0.0,
            "high": 1.0,
            "white_point": 1.1,
            "shoulder_strength": 1.0,
            "low": 0.0,
            "black_point": -0.1,
            "toe_strength": 1.0
        }
        self.curve_viz.set_params(params)

    # ==================== 向后兼容属性 ====================
    # 为了适配 main_window.py 中访问滑块的旧代码
    @property
    def high_slider(self):
        return self.high_control.slider
    
    @property
    def white_point_slider(self):
        return self.white_point_control.slider
    
    @property
    def shoulder_strength_slider(self):
        return self.shoulder_strength_control.slider
    
    @property
    def low_slider(self):
        return self.low_control.slider
    
    @property
    def black_point_slider(self):
        return self.black_point_control.slider
    
    @property
    def toe_strength_slider(self):
        return self.toe_strength_control.slider
    
    @property
    def exposure_slider(self):
        return self.exposure_control.slider
    
    @property
    def contrast_slider(self):
        return self.contrast_control.slider

    # ==================== 创建参数行 ====================
    def _create_row(self, label, min_val, max_val, default_val, step, label_width=90):
        """Create a ParamSliderRow with panel interaction wiring."""
        row = ParamSliderRow(
            label, min_val, max_val, default_val,
            step=step, decimals=2, label_width=label_width)
        row.interactionStarted.connect(self.interaction_started.emit)
        row.interactionEnded.connect(self._on_row_interaction_ended)
        row.valueChanged.connect(lambda _: self.emit_params())
        return row

    # ==================== 发送参数信号 ====================
    def emit_params(self) -> None:
        if self._syncing_ui:
            return
        if not self._params_timer.isActive():
            self._params_timer.start()

    def _on_row_interaction_ended(self) -> None:
        """Flush the final slider value before completing the interaction."""
        if self._params_timer.isActive():
            self._params_timer.stop()
            self._emit_params_now()
        self.interaction_ended.emit()

    def _emit_params_now(self) -> None:
        if self._syncing_ui:
            return
        params = {
            "enabled": self.enable_check.isChecked(),
            "exposure": self.exposure_control.spin.value(),
            "contrast": self.contrast_control.spin.value(),
            "high": self.high_control.spin.value(),
            "white_point": self.white_point_control.spin.value(),
            "shoulder_strength": self.shoulder_strength_control.spin.value(),
            "low": self.low_control.spin.value(),
            "black_point": self.black_point_control.spin.value(),
            "toe_strength": self.toe_strength_control.spin.value()
        }
        if hasattr(self, 'curve_viz'):
            self.curve_viz.set_params(params)
        self.params_changed.emit(params)

    # ==================== 重置所有参数 ====================
    def reset_all(self) -> None:
        """重置所有参数到默认值"""
        self._syncing_ui = True
        controls = [
            self.exposure_control, self.contrast_control,
            self.high_control, self.white_point_control, self.shoulder_strength_control,
            self.low_control, self.black_point_control, self.toe_strength_control
        ]
        widgets = [self.enable_check] + controls
        for w in widgets:
            w.blockSignals(True)
        
        self.enable_check.setChecked(False)
        for ctrl in controls:
            ctrl.reset()
            
        for w in widgets:
            w.blockSignals(False)
        self._syncing_ui = False
        self.emit_params()

    # ==================== 设置参数 ====================
    def set_params(self, params, emit=False) -> None:
        self._syncing_ui = True
        controls_by_key = {
            "exposure": self.exposure_control,
            "contrast": self.contrast_control,
            "high": self.high_control,
            "white_point": self.white_point_control,
            "shoulder_strength": self.shoulder_strength_control,
            "low": self.low_control,
            "black_point": self.black_point_control,
            "toe_strength": self.toe_strength_control,
        }
        widgets = [self.enable_check] + list(controls_by_key.values())
        for w in widgets:
            w.blockSignals(True)
        
        if "enabled" in params:
            self.enable_check.setChecked(params["enabled"])
        for key, ctrl in controls_by_key.items():
            if key in params:
                ctrl.setValue(params[key])
        
        for w in widgets:
            w.blockSignals(False)
        self._syncing_ui = False
        
        if hasattr(self, 'curve_viz'):
            self.curve_viz.set_params({
                "enabled": self.enable_check.isChecked(),
                "exposure": self.exposure_control.spin.value(),
                "contrast": self.contrast_control.spin.value(),
                "high": self.high_control.spin.value(),
                "white_point": self.white_point_control.spin.value(),
                "shoulder_strength": self.shoulder_strength_control.spin.value(),
                "low": self.low_control.spin.value(),
                "black_point": self.black_point_control.spin.value(),
                "toe_strength": self.toe_strength_control.spin.value()
            })
        if emit:
            self.emit_params()

    # NOTE: 不重写 resizeEvent — soft_clip 用左右分栏布局，
    # 左侧 ParamSliderRow 宽度 ≠ 面板宽度，
    # 由 ParamSliderRow.resizeEvent 根据自身实际宽度判断 slider 显隐。
