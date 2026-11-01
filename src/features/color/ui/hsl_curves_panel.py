"""
HSL Curves Panel for Topos Color.
Provides 6 professional HSL curve editing modes:

1. Lum vs Sat (Luminance → Saturation mapping)
2. Hue vs Hue (Hue shift based on hue)
3. Hue vs Sat (Hue → Saturation mapping)
4. Hue vs Lum (Hue → Luminance mapping)
5. Sat vs Sat (Saturation → Saturation mapping)
6. Sat vs Lum (Saturation → Luminance mapping)
"""
from PyQt6.QtWidgets import QWidget, QVBoxLayout, QScrollArea, QHBoxLayout, QLabel, QPushButton
from PyQt6.QtCore import Qt, pyqtSignal
from src.shared.ui.widgets.hsl_curves_widget import HSLCurvesWidget, CURVE_MODES
from src.shared.ui.widgets.drag_value import DragValue
from src.shared.i18n.i18n import I18N


class HSLCurvesPanel(QWidget):
    """
    Panel wrapper for HSL Curves widget.
    Provides a clean interface for integration into the main window.
    """

    # Forward HSL picker signals
    hsl_picker_toggled = pyqtSignal(bool)  # True = activate picker, False = deactivate

    def __init__(self, engine=None) -> None:
        super().__init__()
        self.engine = engine
        self.hsl_curves_widget = None
        self.init_ui()

    def init_ui(self) -> None:
        """Initialize the panel UI."""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Create HSL curves widget
        self.hsl_curves_widget = HSLCurvesWidget(self)
        self.hsl_curves_widget.setStyleSheet("""
            HSLCurvesWidget {
                background-color: #232326;
            }
        """)

        # Wrap in scroll area for small screens
        scroll = QScrollArea()
        self.scroll = scroll
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.hsl_curves_widget)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setStyleSheet("""
            QScrollArea {
                background-color: #1a1a1d;
                border: none;
            }
            QScrollBar:vertical {
                background-color: rgba(255, 255, 255, 10);
                width: 8px;
                border-radius: 4px;
            }
            QScrollBar::handle:vertical {
                background-color: rgba(255, 255, 255, 30);
                border-radius: 4px;
                min-height: 20px;
            }
            QScrollBar::handle:vertical:hover {
                background-color: rgba(255, 255, 255, 50);
            }
        """)

        layout.addWidget(scroll)

        # Connect signals
        # AUD-25418: 面板层不再连接空转发槽——实际链路是 main_window 直接
        # 连接 hsl_curves_widget.changed；面板级的转发信号并不存在，原空槽
        # 已随本修复删除（每曲线变更空跑一次槽调用属于死代码）。
        self.hsl_curves_widget.hsl_picker_toggled.connect(self.hsl_picker_toggled)
        self.footer = QWidget(self)
        footer = QHBoxLayout(self.footer)
        footer.setContentsMargins(8, 2, 8, 2)
        footer.setSpacing(8)
        self._preset_row = QHBoxLayout()
        self._preset_row.setSpacing(10)
        footer.addLayout(self._preset_row)
        footer.addStretch()
        self.input_label = QLabel()
        self.output_label = QLabel()
        self.input_value = DragValue('', 0.0, 360.0, 0.0, decimals=2, compact=True)
        self.output_value = DragValue('', 0.0, 2.0, 1.0, decimals=2, compact=True)
        for label, value in ((self.input_label, self.input_value), (self.output_label, self.output_value)):
            label.setStyleSheet('color: #96969b; font-size: 11px;')
            footer.addWidget(label)
            footer.addWidget(value)
        self.input_value.valueChanged.connect(lambda v: self.hsl_curves_widget.set_selected_value('x', v))
        self.output_value.valueChanged.connect(lambda v: self.hsl_curves_widget.set_selected_value('y', v))
        for field in (self.input_value, self.output_value):
            field.interaction_started.connect(self.hsl_curves_widget.interaction_started)
            field.interaction_ended.connect(self._finish_numeric_edit)
        self._preset_mode = ''
        self._syncing_footer = False
        self.hsl_curves_widget.mode_changed.connect(self._sync_footer)
        self.hsl_curves_widget.point_state_changed.connect(self._sync_footer)
        layout.addWidget(self.footer)
        self.footer.hide()

    def set_integrated(self, integrated: bool) -> None:
        """Use the full palette canvas and bottom controls when embedded."""
        if not integrated:
            return
        canvas = self.hsl_curves_widget
        self.scroll.takeWidget()
        self.scroll.hide()
        self.layout().insertWidget(0, canvas, 1)
        self._preset_row.insertWidget(0, canvas.picker_btn)
        canvas.btn_container.hide()
        self.footer.show()
        self._sync_footer()

    def _finish_numeric_edit(self) -> None:
        self.hsl_curves_widget.flush()
        self.hsl_curves_widget.interaction_ended.emit()

    def _sync_footer(self, *_args: object) -> None:
        if self._syncing_footer:
            return
        self._syncing_footer = True
        try:
            canvas = self.hsl_curves_widget
            mode = CURVE_MODES[canvas.current_mode]
            axis = mode['x_axis']
            if self._preset_mode != axis:
                while self._preset_row.count() > 1:
                    item = self._preset_row.takeAt(1)
                    if item.widget() is not None:
                        item.widget().deleteLater()
                presets = [(0.0, '#e52d34'), (1/6, '#d9bd29'), (2/6, '#41a635'),
                           (3/6, '#61aec3'), (4/6, '#1765dd'), (5/6, '#ac1dbb')]
                if axis == 'lum':
                    presets = [(0.0, '#111111'), (1.0, '#cfcfcf')]
                elif axis == 'sat':
                    presets = []
                for position, color in presets:
                    button = QPushButton(self.footer)
                    button.setFixedSize(13, 13)
                    button.setToolTip(I18N.tr('hsl_curves.add_anchor', 'Add curve control point'))
                    button.setStyleSheet(f'QPushButton {{ background: {color}; border: 1px solid #404044; border-radius: 6px; }}')
                    button.clicked.connect(lambda _checked=False, x=position: canvas.add_anchor(x))
                    self._preset_row.addWidget(button)
                self._preset_mode = axis
            names = {'hue': ('input_hue', 'Input Hue'), 'lum': ('input_lum', 'Input Lum'),
                     'sat': ('input_sat', 'Input Sat')}
            key, fallback = names[axis]
            self.input_label.setText(I18N.tr(f'hsl_curves.{key}', fallback))
            shift = mode['y_type'] == 'shift'
            output_key, output_name = ('hue_rotate', 'Hue Rotate') if shift else (
                ('saturation', 'Saturation') if mode['y_axis'] == 'sat' else ('lum', 'Lum'))
            self.output_label.setText(I18N.tr(f'hsl_curves.{output_key}', output_name))
            input_blocked = self.input_value.blockSignals(True)
            output_blocked = self.output_value.blockSignals(True)
            try:
                self.input_value.setRange(0.0, 360.0 if axis == 'hue' else 1.0)
                self.output_value.setRange(-180.0 if shift else 0.0, 180.0 if shift else 2.0)
                self.output_value.setDefaultValue(0.0 if shift else 1.0)
                x, y = canvas.selected_values()
                self.input_value.setValue(x)
                self.output_value.setValue(y)
                index = min(canvas._inspected_point_idx, len(canvas.curves[canvas.current_mode]) - 1)
                self.input_value.setEnabled(index not in (0, len(canvas.curves[canvas.current_mode]) - 1))
            finally:
                self.input_value.blockSignals(input_blocked)
                self.output_value.blockSignals(output_blocked)
        finally:
            self._syncing_footer = False

    def get_curves_data(self):
        """Get current curve data for saving."""
        if self.hsl_curves_widget:
            return self.hsl_curves_widget.get_curves_data()
        return {}

    def set_curves_data(self, data) -> None:
        """Restore curve data from saved state."""
        if self.hsl_curves_widget and data:
            self.hsl_curves_widget.set_curves_data(data)

    def reset(self, emit_changed: bool = False) -> None:
        """Reset all curves to default."""
        if self.hsl_curves_widget:
            self.hsl_curves_widget.reset_all_curves(emit_changed=emit_changed)

    def add_point_from_color(self, r: float, g: float, b: float) -> bool:
        """Add a control point on current curve based on RGB color."""
        if self.hsl_curves_widget:
            return self.hsl_curves_widget.add_point_from_color(r, g, b)
        return False

    def deactivate_picker(self) -> None:
        """Deactivate the HSL picker mode."""
        if self.hsl_curves_widget:
            self.hsl_curves_widget.deactivate_picker()

    def is_picker_active(self) -> bool:
        """Check if HSL picker is currently active."""
        if self.hsl_curves_widget:
            return self.hsl_curves_widget.is_picker_active()
        return False
