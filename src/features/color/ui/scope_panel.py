"""
Scope Panel for Topos Color.
Provides professional analysis tools for color grading:
- RGB Waveform: Luminance distribution per channel
- Vectorscope: Chrominance (Cb vs Cr) distribution

Features a compact scope header for mode switching,
inspired by DaVinci Resolve professional UI.
"""
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QComboBox,
    QStackedWidget, QPushButton, QMenu, QSplitter, QLabel, QDockWidget, QGridLayout
)
from PyQt6.QtCore import QTimer, QSize, Qt, pyqtSignal, QPointF, QRectF
from PyQt6.QtGui import QAction, QColor, QFont, QPainter, QPen, QPaintEvent
from typing import Optional
import numpy as np
import cv2
import logging

from src.shared.ui.style_sheet import C

logger = logging.getLogger(__name__)

from src.shared.ui.widgets.vectorscope_widget import (
    CieChromaticityWidget,
    FalseColorWidget,
    HistogramWidget,
    LumaWaveformWidget,
    ParadeWidget,
    VectorscopeWidget,
    WaveformWidget,
)
from src.shared.i18n.i18n import I18N
from src.shared.ui.style_sheet import apply_combo_dropdown_style


def _scope_header_font() -> QFont:
    font = QFont("Arial")
    font.setPixelSize(13)
    font.setWeight(QFont.Weight.Bold)
    return font


class _ScopeModeCombo(QComboBox):
    """Flat text selector with a drawn chevron on every Qt platform."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setFont(_scope_header_font())
        self.setFixedHeight(24)
        self.setMinimumWidth(56)
        self.setStyleSheet("QComboBox { border: none; background: transparent; }")

    def sizeHint(self) -> QSize:
        return QSize(self.fontMetrics().horizontalAdvance(self.currentText()) + 30, 24)

    def minimumSizeHint(self) -> QSize:
        return QSize(56, 24)

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        if self.underMouse():
            painter.fillRect(self.rect(), QColor("#2a2a2e"))
        painter.setFont(self.font())
        painter.setPen(QColor("#e4e4e6"))
        text_rect = self.rect().adjusted(4, 0, -23, 0)
        text = self.fontMetrics().elidedText(
            self.currentText(), Qt.TextElideMode.ElideRight, max(0, text_rect.width()))
        painter.drawText(text_rect, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, text)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor("#929297"), 1.1))
        x, y = self.width() - 13, self.height() / 2.0
        painter.drawLine(QPointF(x - 3, y - 2), QPointF(x, y + 1))
        painter.drawLine(QPointF(x, y + 1), QPointF(x + 3, y - 2))
        painter.end()


class _ScopeHeaderButton(QPushButton):
    """Resolve-like monochrome controls with full-height dividers."""

    def __init__(self, kind: str, tooltip: str, parent: QWidget) -> None:
        super().__init__(parent)
        self.kind = kind
        self.setFixedSize(36, 24)
        self.setToolTip(tooltip)
        self.setAccessibleName(tooltip)

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        background = QColor("#2a2a2e" if self.underMouse() or self.isDown() else "#202024")
        painter.fillRect(self.rect(), background)
        painter.setPen(QColor("#111114"))
        painter.drawLine(0, 0, 0, self.height())
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor("#85858a"), 1.0))
        if self.kind == 'settings':
            painter.setBrush(background)
            for y, knob in ((7, 21), (12, 15), (17, 23)):
                painter.drawLine(QPointF(11, y), QPointF(27, y))
                painter.drawEllipse(QPointF(knob, y), 1.8, 1.8)
        elif self.kind == 'expand':
            for sx in (-1, 1):
                for sy in (-1, 1):
                    corner = QPointF(18 + sx * 5, 12 + sy * 5)
                    painter.drawLine(corner, QPointF(corner.x() - sx * 3, corner.y()))
                    painter.drawLine(corner, QPointF(corner.x(), corner.y() - sy * 3))
                    painter.drawLine(corner, QPointF(18 + sx * 2, 12 + sy * 2))
        elif self.kind in ('single', 'dual', 'quad', 'all'):
            columns, rows = {'single': (1, 1), 'dual': (2, 1),
                             'quad': (2, 2), 'all': (3, 3)}[self.kind]
            if self.isChecked():
                painter.setPen(QPen(QColor("#e4e4e6"), 1.0))
            for row in range(rows):
                for col in range(columns):
                    painter.drawRect(QRectF(10 + col * 16 / columns,
                                            6 + row * 12 / rows,
                                            16 / columns - 1, 12 / rows - 1))
        else:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor("#85858a"))
            for x in (12, 18, 24):
                painter.drawEllipse(QPointF(x, 12), 1.3, 1.3)
        painter.end()


# Scope mode definitions
SCOPE_MODES = {
    'waveform': {
        'name': 'rgb_waveform',
        'widget_class': WaveformWidget,
    },
    'vectorscope': {
        'name': 'vectorscope',
        'widget_class': VectorscopeWidget,
    },
    'split': {
        'name': 'split_view',
        'widget_class': None,  # Special mode - uses splitter
    },
    # P1-COLOR-06 专业 scope 扩展：parade / luma / histogram / cie /
    # falsecolor 现已全部具备 GPU pass（共享 GpuScopeEngine），实时路径
    # 与 waveform/vectorscope 一样走 GPU 页；CPU 页仅保留为诊断 fallback
    # 与 source/reference 输入源路径。
    'parade': {
        'name': 'rgb_parade',
        'widget_class': ParadeWidget,
    },
    'luma': {
        'name': 'luma_waveform',
        'widget_class': LumaWaveformWidget,
    },
    'histogram': {
        'name': 'histogram',
        'widget_class': HistogramWidget,
    },
    'cie': {
        'name': 'cie',
        'widget_class': CieChromaticityWidget,
    },
    'falsecolor': {
        'name': 'false_color',
        'widget_class': FalseColorWidget,
    },
}

# 历史上部分模式没有 GPU presenter 而被钉在 CPU 页；现在全部模式都有
# GPU pass，该集合保留为空以维持扩展点语义。
_CPU_ONLY_MODES: set = set()

MODE_ORDER = ['waveform', 'vectorscope', 'split', 'parade', 'luma',
              'histogram', 'cie', 'falsecolor', 'quad', 'all']
SCOPE_MODES.update({
    'quad': {'name': 'quad_view', 'widget_class': None},
    'all': {'name': 'all_scopes', 'widget_class': None},
})
_MULTI_LAYOUTS = {
    'split': ('waveform', 'vectorscope'),
    'quad': ('parade', 'waveform', 'vectorscope', 'histogram'),
    'all': ('parade', 'waveform', 'vectorscope', 'histogram', 'luma', 'cie', 'falsecolor'),
}
_SCOPE_NAMES = {
    'waveform': 'Waveform', 'vectorscope': 'Vectorscope', 'split': 'Dual View',
    'parade': 'Parade', 'luma': 'Luma', 'histogram': 'Histogram',
    'cie': 'CIE Chromaticity', 'falsecolor': 'False Color',
    'quad': 'Quad View', 'all': 'All Scopes',
}


def _scope_name(mode: str) -> str:
    return I18N.tr(f"scope.mode_{mode}", _SCOPE_NAMES[mode])

# ScopeGpuFrame descriptor field prefix per mode (split handled specially).
_MODE_FRAME_FIELD = {
    'waveform': 'waveform',
    'vectorscope': 'vectorscope',
    'parade': 'parade',
    'luma': 'luma',
    'histogram': 'histogram',
    'cie': 'cie',
    'falsecolor': 'falsecolor',
}

# ScopePanel presenter attribute per ScopeGpuFrame field prefix.
_MODE_PRESENTER_ATTR = {
    'waveform': '_gpu_wf_presenter',
    'vectorscope': '_gpu_vs_presenter',
    'parade': '_gpu_parade_presenter',
    'luma': '_gpu_luma_presenter',
    'histogram': '_gpu_histogram_presenter',
    'cie': '_gpu_cie_presenter',
    'falsecolor': '_gpu_fc_presenter',
}


class ScopePanel(QWidget):
    """
    Professional analysis panel with waveform and vectorscope.

    Uses a compact header for mode switching and scope settings.

    Features:
    - RGB Waveform mode
    - Vectorscope mode
    - Split view mode (side-by-side display)
    - Fixed size mode (scope doesn't resize with panel)
    - Vectorscope zoom (x1/x2)

    M19.3 · 测量域声明（Scope 独立全帧采样通路）：
    - **输入**：``set_scope_image`` 收到渲染管线 scope 专用回读
      （``ImageEngine.render_full_image_for_scope``——FBO f4 全帧，非
      预览视口降采样），保持**显示域**编码（预览 ODT 后）；波形/矢量
      读数是**显示域值**——与导出域（目标 ODT 后）或场景线性域的换算
      差一层显示变换，对比工程间读数时须声明。
    - **采样**：全帧逐像素（无下采样）；``_cpu_analysis_max_width``
      非 None 时消费侧自行下采样（Composite 专用 lane）。
    - **几何**：波形横轴=图像列（逐列投影），矢量=色度平面极坐标。
    - **单位**：波形纵轴 0–1 显示域；矢量刻度按显示域色度归一。
    """

    # Notify the controller when only the desired GPU output changed. This
    # avoids waiting for another video frame and never carries pixel data.
    demand_changed = pyqtSignal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setMinimumSize(200, 150)
        self._current_mode = 'waveform'
        self._pending_image: np.ndarray = None
        self._pending_mask: np.ndarray = None
        self._last_image: np.ndarray = None
        self._last_mask: np.ndarray = None
        # Optional CPU analysis bound for consumers that receive a full-size
        # renderer readback. The Color controller normally supplies a compact
        # frame already; Composite enables this guard when it uses the shared
        # panel with its dedicated async readback lane.
        self._cpu_analysis_max_width: Optional[int] = None
        self._cpu_analysis_max_height: Optional[int] = None

        # Settings state
        self._zoom_level = 1  # 1 or 2 for vectorscope
        # P1-COLOR-06: scope 输入色彩状态（标尺/CIE 线性化用）
        self._scope_transfer = 'srgb'
        self._scope_primaries = 'bt709'
        self._waveform_scale = 'percent'
        # P1-COLOR-07: 输入源（graded 调色后 / source 未调色 / reference 参考帧）
        self._scope_source = 'graded'
        # 可用输入源集合：Color 页提供全部三项（GPU graded + CPU 缓存帧）；
        # Composite 只有渲染输出，限制为 graded 以免切到永远没有数据的源。
        self._available_sources = ('graded', 'source', 'reference')
        self._source_images = {}  # source -> 最近一帧

        # Throttle timer for updates
        self._update_timer = QTimer(self)
        self._update_timer.setSingleShot(True)
        self._update_timer.setInterval(50)  # ~20 FPS max
        self._update_timer.timeout.connect(self._process_pending_image)

        self.init_ui()

    def _add_single_page(self, stack: QStackedWidget, mode: str,
                         widget: QWidget, pages: dict) -> None:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        widget.setMinimumSize(0, 0)
        layout.addWidget(widget)
        stack.addWidget(page)
        pages[mode] = page

    def _make_multi_page(self, mode: str, backend: str) -> QWidget:
        page = QWidget()
        grid = QGridLayout(page)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(1)
        cells = {}
        columns = 2 if mode == 'quad' else 3
        for index, scope_mode in enumerate(_MULTI_LAYOUTS[mode]):
            cell = QWidget()
            body = QVBoxLayout(cell)
            body.setContentsMargins(0, 0, 0, 0)
            body.setSpacing(0)
            header = QWidget()
            header.setFixedHeight(24)
            header.setStyleSheet("background: #202024;")
            controls = QHBoxLayout(header)
            controls.setContentsMargins(8, 0, 0, 0)
            controls.setSpacing(0)
            label = QLabel(_scope_name(scope_mode))
            label.setFont(_scope_header_font())
            label.setStyleSheet("color: #e4e4e6; background: transparent;")
            controls.addStretch(1)
            controls.addWidget(label)
            controls.addSpacing(12)
            settings = _ScopeHeaderButton('settings', I18N.tr('analysis.settings', 'Settings'), header)
            settings.clicked.connect(lambda _=False, b=settings: self._show_settings_menu(anchor=b))
            controls.addWidget(settings)
            body.addWidget(header)
            cells[scope_mode] = body
            grid.addWidget(cell, index // columns, index % columns)
        for column in range(columns):
            grid.setColumnStretch(column, 1)
        for row in range((len(cells) + columns - 1) // columns):
            grid.setRowStretch(row, 1)
        self._multi_cells[backend][mode] = cells
        return page

    def _cpu_scope_widgets(self) -> dict:
        return {
            'waveform': self.waveform_widget, 'vectorscope': self.vectorscope_widget,
            'parade': self.parade_widget, 'luma': self.luma_widget,
            'histogram': self.histogram_widget, 'cie': self.cie_widget,
            'falsecolor': self.falsecolor_widget,
        }

    def _configure_scope_layout(self, mode: str) -> None:
        """Move the existing presenters; allocate no extra GL contexts."""
        gpu_widgets = {key: getattr(self, attr, None)
                       for key, attr in _MODE_PRESENTER_ATTR.items()}
        for backend, pages, widgets in (
            ('cpu', self._cpu_single_pages, self._cpu_scope_widgets()),
            ('gpu', self._gpu_single_pages, gpu_widgets),
        ):
            targets = self._multi_cells[backend].get(mode, {})
            for key, page in pages.items():
                widget = widgets.get(key)
                if widget is None:
                    continue
                target = targets.get(key, page.layout())
                target.addWidget(widget, 1)
                widget.show()
        if mode == 'split' and self._gpu_splitter is not None:
            self._gpu_splitter.addWidget(self._gpu_wf_presenter)
            self._gpu_splitter.addWidget(self._gpu_vs_presenter)
            self._gpu_splitter.setSizes([400, 400])
        self._update_layout_controls()

    def _update_layout_controls(self, *_) -> None:
        dock = self._host_dock()
        multi = self._current_mode in _MULTI_LAYOUTS
        self.layout_toolbar.setVisible(multi or (dock is not None and dock.isFloating()))
        self.mode_combo.setVisible(not multi)
        self.settings_btn.setVisible(not multi)
        for mode, button in self.layout_buttons.items():
            button.setChecked(self._current_mode == mode or (
                mode == 'waveform' and not multi))

    def _host_dock(self) -> Optional[QDockWidget]:
        parent = self.parentWidget()
        while parent is not None:
            if isinstance(parent, QDockWidget):
                return parent
            parent = parent.parentWidget()
        return None

    def open_floating(self) -> None:
        """Open the live scope dock from the viewer context menu."""
        if self._current_mode not in _MULTI_LAYOUTS:
            self.set_mode('quad')
        dock = self._host_dock()
        if dock is not None:
            dock.setFloating(True)
            dock.resize(max(900, dock.width()), max(600, dock.height()))
            dock.show()
            dock.raise_()
            dock.activateWindow()
        else:
            self.resize(900, 600)
            self.show()
        self._update_layout_controls()

    def init_ui(self) -> None:
        """Initialize the UI layout."""
        # Main layout
        main_layout = QVBoxLayout()
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)
        self.setLayout(main_layout)

        # Outer display stack: [0]=legacy CPU widgets, [1]=GPU presenters.
        # The realtime Color-page path is GPU-only; the CPU page is retained
        # solely for an explicitly enabled diagnostic fallback and is never
        # fed by normal frame updates.
        self.display_stack = QStackedWidget()
        self.display_stack.setStyleSheet("background-color: #050505;")

        # ---- CPU scope page (legacy diagnostic fallback) ----
        cpu_page = QWidget()
        cpu_page.setStyleSheet("background-color: #050505;")
        cpu_layout = QVBoxLayout(cpu_page)
        cpu_layout.setContentsMargins(0, 0, 0, 0)
        cpu_layout.setSpacing(0)

        # Stacked widget for scope modes (waveform/vectorscope)
        self.stack = QStackedWidget()
        self.stack.setStyleSheet("background-color: #050505;")

        # Create scope widgets
        self.waveform_widget = WaveformWidget()
        self.vectorscope_widget = VectorscopeWidget()

        self._cpu_single_pages = {}
        self._gpu_single_pages = {}
        self._multi_cells = {'cpu': {}, 'gpu': {}}
        self._add_single_page(self.stack, 'waveform', self.waveform_widget, self._cpu_single_pages)
        self._add_single_page(self.stack, 'vectorscope', self.vectorscope_widget, self._cpu_single_pages)
        self.parade_widget = ParadeWidget()
        self.luma_widget = LumaWaveformWidget()
        self.histogram_widget = HistogramWidget()
        self.cie_widget = CieChromaticityWidget()
        self.falsecolor_widget = FalseColorWidget()

        # Split view container (for side-by-side display)
        self.split_container = QWidget()
        self.split_container.setStyleSheet("background-color: #050505;")
        split_layout = QHBoxLayout(self.split_container)
        split_layout.setContentsMargins(0, 0, 0, 0)
        split_layout.setSpacing(2)

        # Create separate widgets for split view
        self.split_waveform = WaveformWidget()
        self.split_vectorscope = VectorscopeWidget()

        # Use splitter for resizable split view
        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.setHandleWidth(1)
        self.splitter.setStyleSheet(f"""
            QSplitter {{
                background-color: #050505;
            }}
            QSplitter::handle {{
                background-color: {C.PANEL_DIVIDER};
                width: 1px;
            }}
        """)
        self.splitter.addWidget(self.split_waveform)
        self.splitter.addWidget(self.split_vectorscope)
        self.splitter.setSizes([200, 200])  # Equal split

        split_layout.addWidget(self.splitter)
        self.stack.addWidget(self.split_container)  # Index 2
        self._add_single_page(self.stack, 'parade', self.parade_widget, self._cpu_single_pages)
        self._add_single_page(self.stack, 'luma', self.luma_widget, self._cpu_single_pages)
        self._add_single_page(self.stack, 'histogram', self.histogram_widget, self._cpu_single_pages)
        self._add_single_page(self.stack, 'cie', self.cie_widget, self._cpu_single_pages)
        self._add_single_page(self.stack, 'falsecolor', self.falsecolor_widget, self._cpu_single_pages)

        cpu_layout.addWidget(self.stack)
        self.display_stack.addWidget(cpu_page)  # outer index 0

        # ---- GPU scope page (display-only QOpenGLWidget presenters) ----
        gpu_page = QWidget()
        gpu_page.setStyleSheet("background-color: #050505;")
        self._gpu_stack = QStackedWidget()
        self._gpu_stack.setStyleSheet("background-color: #050505;")

        self._gpu_display_active = False
        self._gpu_fallback_reason = None
        try:
            from src.shared.ui.widgets.gpu_scope_widget import GpuScopeWidget
            self._gpu_wf_presenter = GpuScopeWidget(
                # The waveform shader already maps 0% to the GL bottom and
                # 100% to the GL top.  Keep the blit unflipped so the
                # presenter screen coordinates match the 1023..0 axis labels.
                preserve_aspect=False, axis_left_px=38, flip_y=False,
            )
            self._gpu_vs_presenter = GpuScopeWidget(
                preserve_aspect=True, flip_y=False,
                show_vectorscope_targets=True,
            )
            # GPU split container mirrors the CPU splitter layout.
            self._gpu_splitter = QSplitter(Qt.Orientation.Horizontal)
            self._gpu_splitter.setHandleWidth(1)
            self._gpu_splitter.setStyleSheet(f"""
                QSplitter {{ background-color: #050505; }}
                QSplitter::handle {{ background-color: {C.PANEL_DIVIDER}; width: 1px; }}
            """)

            # Professional scope presenters (parade / luma / histogram /
            # cie / falsecolor). Parade and luma share the waveform value
            # axis; the histogram bakes its own graticule into the shader;
            # CIE and False Color are aspect-fitted plots, not value plots.
            self._gpu_parade_presenter = GpuScopeWidget(
                preserve_aspect=False, axis_left_px=38, flip_y=False,
            )
            self._gpu_luma_presenter = GpuScopeWidget(
                preserve_aspect=False, axis_left_px=38, flip_y=False,
            )
            self._gpu_histogram_presenter = GpuScopeWidget(
                preserve_aspect=False, show_value_axis=False,
                show_histogram_axis=True,
            )
            try:
                from src.shared.ui.widgets.cie_scope_widget import (
                    CieGpuScopeWidget,
                )
                self._gpu_cie_presenter = CieGpuScopeWidget()
            except Exception as exc:
                logger.warning("CIE presenter unavailable (%s); "
                               "falling back to plain blit", exc)
                self._gpu_cie_presenter = GpuScopeWidget(
                    preserve_aspect=True, show_value_axis=False,
                )
            self._gpu_fc_presenter = GpuScopeWidget(
                preserve_aspect=True, show_value_axis=False,
                legend='falsecolor_sdr',
            )
            # Mirror the CPU stack page order exactly (MODE_ORDER indices):
            # 0=waveform, 1=vectorscope, 2=split, 3=parade, 4=luma,
            # 5=histogram, 6=cie, 7=falsecolor.
            self._add_single_page(self._gpu_stack, 'waveform', self._gpu_wf_presenter, self._gpu_single_pages)
            self._add_single_page(self._gpu_stack, 'vectorscope', self._gpu_vs_presenter, self._gpu_single_pages)
            self._gpu_stack.addWidget(self._gpu_splitter)           # index 2
            self._add_single_page(self._gpu_stack, 'parade', self._gpu_parade_presenter, self._gpu_single_pages)
            self._add_single_page(self._gpu_stack, 'luma', self._gpu_luma_presenter, self._gpu_single_pages)
            self._add_single_page(self._gpu_stack, 'histogram', self._gpu_histogram_presenter, self._gpu_single_pages)
            self._add_single_page(self._gpu_stack, 'cie', self._gpu_cie_presenter, self._gpu_single_pages)
            self._add_single_page(self._gpu_stack, 'falsecolor', self._gpu_fc_presenter, self._gpu_single_pages)
            # Back-compat alias used by older present paths.
            self._gpu_presenter = self._gpu_wf_presenter
        except Exception as exc:
            # P2-COLOR-12：GPU presenter 创建失败不能静默——记录原因并
            # 在 GPU 页显示降级提示，避免 CPU fallback 伪装成实时。
            logger.warning("GPU scope presenter unavailable, "
                           "falling back to CPU page: %s", exc)
            self._gpu_fallback_reason = str(exc)
            self._gpu_wf_presenter = None
            self._gpu_vs_presenter = None
            self._gpu_splitter = None
            self._gpu_parade_presenter = None
            self._gpu_luma_presenter = None
            self._gpu_histogram_presenter = None
            self._gpu_cie_presenter = None
            self._gpu_fc_presenter = None
            self._gpu_presenter = None

        for layout_mode in ('quad', 'all'):
            self.stack.addWidget(self._make_multi_page(layout_mode, 'cpu'))
            self._gpu_stack.addWidget(self._make_multi_page(layout_mode, 'gpu'))

        gpu_page_layout = QVBoxLayout(gpu_page)
        gpu_page_layout.setContentsMargins(0, 0, 0, 0)
        gpu_page_layout.setSpacing(0)
        if getattr(self, '_gpu_fallback_reason', None):
            from src.shared.i18n.i18n import I18N as _I18N
            banner = QLabel(_I18N.tr(
                "scope.gpu_fallback_banner",
                "GPU 示波器不可用（{reason}）——已回退 CPU 诊断页",
                reason=self._gpu_fallback_reason))
            banner.setStyleSheet(
                "color: #e0a030; background: #2a1e0a; padding: 4px;")
            banner.setWordWrap(True)
            gpu_page_layout.addWidget(banner)
        gpu_page_layout.addWidget(self._gpu_stack)

        self.display_stack.addWidget(gpu_page)  # outer index 1
        main_layout.addWidget(self.display_stack)
        # GPU page is visible by default whenever the presenters were
        # created. This prevents the normal path from painting the legacy
        # QPainter page even during startup before the first GPU frame.
        gpu_available = (
            self._gpu_wf_presenter is not None
            and self._gpu_vs_presenter is not None
        )
        self.display_stack.setCurrentIndex(1 if gpu_available else 0)
        self._gpu_display_active = gpu_available

        # FalseColor 模式/阈值变化会改变 GPU demand 内容（HDR 分档、zebra
        # 阈值），需要 bump revision 让在途旧帧作废。
        if hasattr(self.falsecolor_widget, 'mode_changed'):
            self.falsecolor_widget.mode_changed.connect(
                self._on_falsecolor_variant_changed
            )
        if hasattr(self.falsecolor_widget, 'zebra_threshold_changed'):
            self.falsecolor_widget.zebra_threshold_changed.connect(
                self._on_falsecolor_variant_changed
            )

        # Resolve-style scope header: keep controls outside the plot so they
        # never obscure live waveform detail.
        self.selector_container = QWidget(self)
        self.selector_container.setObjectName("scopeHeader")
        self.selector_container.setFixedHeight(24)
        self.selector_container.setStyleSheet("""
            QWidget#scopeHeader {
                background-color: #202024;
                border-bottom: 1px solid #111114;
            }
            QLabel#scopeTitle {
                color: #e4e4e6;
                background: transparent;
                border: none;
                padding: 0;
            }
        """)
        selector_layout = QHBoxLayout(self.selector_container)
        selector_layout.setContentsMargins(10, 0, 0, 0)
        selector_layout.setSpacing(0)

        self.scope_title = QLabel(I18N.tr("workspace.scopes", "Scopes"))
        self.scope_title.setObjectName("scopeTitle")
        self.scope_title.setFont(_scope_header_font())
        selector_layout.addWidget(self.scope_title)
        selector_layout.addStretch(1)

        self.layout_toolbar = QWidget(self.selector_container)
        layout_controls = QHBoxLayout(self.layout_toolbar)
        layout_controls.setContentsMargins(0, 0, 0, 0)
        layout_controls.setSpacing(0)
        self.layout_buttons = {}
        for kind, mode in (('single', 'waveform'), ('dual', 'split'),
                           ('quad', 'quad'), ('all', 'all')):
            button = _ScopeHeaderButton(kind, _scope_name(mode), self.layout_toolbar)
            button.setCheckable(True)
            button.clicked.connect(lambda _=False, m=mode: self.set_mode(m))
            layout_controls.addWidget(button)
            self.layout_buttons[mode] = button
        selector_layout.addWidget(self.layout_toolbar)
        self.layout_toolbar.hide()

        # Mode selector ComboBox
        self.mode_combo = _ScopeModeCombo()
        for mode in MODE_ORDER:
            self.mode_combo.addItem(_scope_name(mode), mode)

        apply_combo_dropdown_style(self.mode_combo)
        self.mode_combo.currentIndexChanged.connect(self._on_mode_changed)
        selector_layout.addWidget(self.mode_combo)

        self.settings_btn = _ScopeHeaderButton(
            'settings', I18N.tr("analysis.settings", "Settings"), self.selector_container)
        self.settings_btn.clicked.connect(self._show_settings_menu)
        selector_layout.addWidget(self.settings_btn)
        self.expand_btn = _ScopeHeaderButton(
            'expand', I18N.tr("scope.expand", "Expand"), self.selector_container)
        self.expand_btn.clicked.connect(self._expand_scope)
        selector_layout.addWidget(self.expand_btn)
        self.more_btn = _ScopeHeaderButton(
            'more', I18N.tr("scope.options", "Scope Options"), self.selector_container)
        self.more_btn.clicked.connect(self._show_options_menu)
        selector_layout.addWidget(self.more_btn)

        # P1-COLOR-07: 输入源标签（明确 scope 分析的是哪一阶段）
        self.source_label = QLabel(self)
        self.source_label.hide()
        self.source_label.setStyleSheet(
            "QLabel { background-color: transparent;"
            " color: rgba(120, 220, 160, 200); border: none;"
            " padding: 0px 4px; font-size: 11px; }")
        self._update_source_label()

        # M19.3b: 测量域角标——域/采样/单位三要素短标注（完整声明在
        # tooltip：输入通路/几何/与导出域差一层显示变换，见类 docstring）
        self.domain_badge = QLabel(self)
        self.domain_badge.hide()
        self.domain_badge.setStyleSheet(
            "QLabel { background-color: transparent;"
            " color: rgba(150, 190, 255, 200); border: none;"
            " padding: 0px 4px; font-size: 11px; }")
        self._update_domain_badge()

        self.selector_container.adjustSize()

        # The selector spans the panel header; the plot starts below it.
        main_layout.insertWidget(0, self.selector_container)

        # Panel border
        self.setStyleSheet("""
            ScopePanel {
                background-color: #050505;
                border: 1px solid #333;
            }
        """)

    def _expand_scope(self) -> None:
        """Float/redock the host dock, or expand a standalone scope window."""
        dock = self._host_dock()
        if dock is not None:
            if dock.isFloating():
                dock.setFloating(False)
            else:
                self.open_floating()
            self._update_layout_controls()
            return
        window = self.window()
        if window.isMaximized():
            window.showNormal()
        else:
            window.showMaximized()

    def _show_options_menu(self) -> None:
        self._show_settings_menu(anchor=self.more_btn)

    def _show_settings_menu(self, *, anchor: Optional[QWidget] = None) -> None:
        """Show the settings menu."""
        menu = QMenu(self)
        menu.setStyleSheet(
            "QMenu { background: #252529; color: #dddde0; border: 1px solid #111114; }"
            "QMenu::item { padding: 5px 20px; }"
            "QMenu::item:selected { background: #414146; }"
        )
        for label in (self.source_label, self.domain_badge):
            info = menu.addAction(label.text())
            info.setEnabled(False)
            info.setToolTip(label.toolTip())
        menu.setToolTipsVisible(True)
        menu.addSeparator()
        for mode in ('waveform', 'split', 'quad', 'all'):
            action = menu.addAction(_scope_name(mode))
            action.setCheckable(True)
            action.setChecked(self._current_mode == mode or (
                mode == 'waveform' and self._current_mode not in _MULTI_LAYOUTS))
            action.triggered.connect(lambda _=False, m=mode: self.set_mode(m))
        menu.addSeparator()

        # Vectorscope Zoom section
        zoom_label = QAction(I18N.tr("analysis.vectorscope_zoom", "Vectorscope Zoom"), menu)
        zoom_label.setEnabled(False)
        menu.addAction(zoom_label)

        zoom_x1 = QAction(I18N.tr("analysis.zoom_x1", "x1 (Normal)"), menu)
        zoom_x1.setCheckable(True)
        zoom_x1.setChecked(self._zoom_level == 1)
        zoom_x1.triggered.connect(lambda: self._set_zoom(1))
        menu.addAction(zoom_x1)

        zoom_x2 = QAction(I18N.tr("analysis.zoom_x2", "x2 (Magnified)"), menu)
        zoom_x2.setCheckable(True)
        zoom_x2.setChecked(self._zoom_level == 2)
        zoom_x2.triggered.connect(lambda: self._set_zoom(2))
        menu.addAction(zoom_x2)

        menu.addSeparator()

        # P1-COLOR-07: scope 输入源
        source_label = QAction(I18N.tr("analysis.scope_input", "Scope Input"), menu)
        source_label.setEnabled(False)
        menu.addAction(source_label)
        for src, label in (
            ('graded', I18N.tr("analysis.source_graded", "Graded")),
            ('source', I18N.tr("analysis.source_source", "Source")),
            ('reference', I18N.tr("analysis.source_reference", "Reference")),
        ):
            act = QAction(label, menu)
            act.setCheckable(True)
            act.setChecked(self._scope_source == src)
            # 不可用源灰显（如 Composite 只有渲染输出这一种源）。
            act.setEnabled(src in self._available_sources)
            act.triggered.connect(
                lambda _=False, sc=src: self.set_scope_source(sc))
            menu.addAction(act)

        menu.addSeparator()

        # P1-COLOR-06: 波形标尺（% / nits / 10-bit code）
        scale_label = QAction(I18N.tr("analysis.waveform_scale", "Waveform Scale"), menu)
        scale_label.setEnabled(False)
        menu.addAction(scale_label)
        for mode, transfer, label in (
            ('percent', 'srgb', I18N.tr("analysis.scale_percent", "% (SDR)")),
            ('nits', 'pq', I18N.tr("analysis.scale_nits_pq", "nits (PQ)")),
            ('nits', 'hlg', I18N.tr("analysis.scale_nits_hlg", "nits (HLG)")),
            ('code10', 'srgb', I18N.tr("analysis.scale_code10", "10-bit (64-940)")),
        ):
            act = QAction(label, menu)
            act.setCheckable(True)
            act.setChecked(
                self._waveform_scale == mode
                and (mode != 'nits' or self._scope_transfer == transfer))
            act.triggered.connect(
                lambda _=False, m=mode, t=transfer: self._set_waveform_scale(m, t))
            menu.addAction(act)

        menu.addSeparator()

        # P1-COLOR-06: CIE 图像域矩阵
        gamut_label = QAction(I18N.tr("analysis.source_gamut", "Source Gamut"), menu)
        gamut_label.setEnabled(False)
        menu.addAction(gamut_label)
        for primaries, label in (
            ('bt709', "Rec.709"),
            ('p3', "P3-D65"),
            ('bt2020', "Rec.2020"),
        ):
            act = QAction(label, menu)
            act.setCheckable(True)
            act.setChecked(self._scope_primaries == primaries)
            act.triggered.connect(
                lambda _=False, pm=primaries: self._set_scope_primaries(pm))
            menu.addAction(act)

        # Apply settings to all vectorscope widgets
        button = anchor if anchor is not None else self.settings_btn
        menu.exec(button.mapToGlobal(button.rect().bottomLeft()))

    def set_scope_source(self, source: str) -> None:
        """切换 scope 输入源（P1-COLOR-07）。

        graded: GPU/CPU 分析调色后结果（默认）
        source: 未调色原图（CPU 路径）
        reference: 参考 still（CPU 路径）

        切换即丢弃在途旧源帧：只接受与新源匹配的 update_scope 调用；
        无缓存帧时清空显示，避免把上一源的画面误标为新源。
        """
        if source not in ('graded', 'source', 'reference'):
            return
        if source not in self._available_sources:
            return  # 该宿主页没有此源的数据路径（如 Composite）
        if source == self._scope_source:
            return
        self._scope_source = source
        self._pending_image = None
        self._pending_mask = None
        self._update_source_label()
        cached = self._source_images.get(source)
        if cached is not None:
            self._pending_image = cached
            if not self._update_timer.isActive():
                self._update_timer.start()
        else:
            self._clear_scope_display()
        # source/reference 没有 GPU 分析路径，强制 CPU 页显示
        if source != 'graded':
            self._gpu_display_active = False
            try:
                self.display_stack.setCurrentIndex(0)
            except Exception:
                pass
        self.bump_demand_revision()

    @property
    def scope_source(self) -> str:
        return self._scope_source

    def set_available_sources(self, sources) -> None:
        """声明宿主页可用的输入源集合。

        不可用的源在设置菜单中灰显且切换被拒绝。Composite 只有渲染输出
        （graded）这一条数据路径，创建时传入 ``('graded',)``。
        """
        allowed = tuple(s for s in sources
                        if s in ('graded', 'source', 'reference'))
        if not allowed:
            return
        self._available_sources = allowed
        if self._scope_source not in allowed:
            self.set_scope_source(allowed[0])

    def _update_source_label(self) -> None:
        label = getattr(self, 'source_label', None)
        if label is None:
            return
        texts = {
            'graded': I18N.tr("analysis.source_graded", "Graded"),
            'source': I18N.tr("analysis.source_source", "Source"),
            'reference': I18N.tr("analysis.source_reference", "Reference"),
        }
        prefix = I18N.tr("analysis.input_prefix", "In")
        label.setText(
            f"{prefix}: {texts.get(self._scope_source, self._scope_source)}"
        )
        self.selector_container.adjustSize()

    def _update_domain_badge(self) -> None:
        """M19.3b · 测量域角标：域/采样/单位三要素短标注。

        域=显示域（预览 ODT 后——与导出域/场景线性域差一层显示变换）；
        采样=全帧（Composite lane 设定 ``_cpu_analysis_max_width`` 后
        显示下采样上限）；单位随波形标尺（percent 0–1 / nits / code10
        64..940；矢量恒为显示域色度归一）。完整声明在 tooltip，不占
        面板空间——跨工程对比读数前先看此标注。
        """
        badge = getattr(self, 'domain_badge', None)
        if badge is None:
            return
        domain = I18N.tr("analysis.domain_display", "显示域")
        if self._cpu_analysis_max_width is not None:
            sampling = I18N.tr(
                "analysis.sampling_downsampled", "下采样",
            ) + f"≤{self._cpu_analysis_max_width}px"
        else:
            sampling = I18N.tr("analysis.sampling_fullframe", "全帧")
        units = {
            'percent': '0–1',
            'nits': I18N.tr("analysis.unit_nits", "nit"),
            'code10': '64–940',
        }.get(self._waveform_scale, '0–1')
        badge.setText(f"{domain} · {sampling} · {units}")
        badge.setToolTip(I18N.tr(
            "analysis.domain_tooltip",
            "测量域：显示域（预览 ODT 后）——与导出域（目标 ODT 后）/场景"
            "线性域差一层显示变换，跨工程对比读数须声明。\n采样：{sampling}"
            "（输入为 scope 专用全帧回读，非预览视口降采样）。\n单位：波形"
            " {units}；矢量=显示域色度归一；几何：波形逐列投影、矢量色度"
            "极坐标。", sampling=sampling, units=units))
        if hasattr(self, 'selector_container'):
            self.selector_container.adjustSize()

    def _clear_scope_display(self) -> None:
        """清空所有 CPU scope widget 的显示（切换输入源防旧帧）。"""
        for attr in ('waveform_widget', 'split_waveform', 'parade_widget',
                     'luma_widget'):
            widget = getattr(self, attr, None)
            if widget is not None and hasattr(widget, 'q_scope_image'):
                widget.q_scope_image = None
                widget.update()
        for attr in ('vectorscope_widget', 'split_vectorscope'):
            widget = getattr(self, attr, None)
            if widget is not None and hasattr(widget, '_heatmap_image'):
                widget._heatmap_image = None
                widget.update()
        if hasattr(self, 'cie_widget') and self.cie_widget is not None:
            self.cie_widget._density = None
            self.cie_widget._density_image = None
            self.cie_widget.update()
        if hasattr(self, 'falsecolor_widget') and self.falsecolor_widget is not None:
            self.falsecolor_widget._display_image = None
            self.falsecolor_widget.update()

    def set_color_state(self, transfer: str = 'srgb',
                        primaries: str = 'bt709') -> None:
        """声明 scope 输入图像的色彩状态（P1-COLOR-06/P1-COLOR-07）。

        驱动波形 nits 标尺、CIE 线性化与 False Color HDR 分档。
        控制器在知道源媒体传输特性时调用；未调用时按 SDR 处理。
        """
        transfer = transfer if transfer in ('srgb', 'bt709', 'pq', 'hlg',
                                            'scene') else 'srgb'
        primaries = primaries if primaries in ('bt709', 'p3', 'bt2020') \
            else 'bt709'
        if transfer == self._scope_transfer and primaries == self._scope_primaries:
            return
        self._scope_transfer = transfer
        self._scope_primaries = primaries
        self._apply_color_state()
        # 传输特性/色域会改变 CIE 线性化与 False Color HDR 分档 → 在途
        # GPU demand 帧需要作废。
        self.bump_demand_revision()

    def _apply_color_state(self) -> None:
        """把色彩状态下发到各 scope widget 与 GPU presenter 标尺。"""
        self.waveform_widget.set_color_state(self._scope_transfer)
        self.split_waveform.set_color_state(self._scope_transfer)
        self.cie_widget.set_color_state(self._scope_transfer,
                                        self._scope_primaries)
        self.falsecolor_widget.set_color_state(self._scope_transfer)
        if self._waveform_scale == 'code10':
            scale = 'code10_video'
        elif self._waveform_scale == 'nits':
            scale = 'nits'
        else:
            scale = 'code10'
        # 波形值轴 presenter：RGB waveform / parade / luma 共用同一标尺。
        for attr in ('_gpu_wf_presenter', '_gpu_parade_presenter',
                     '_gpu_luma_presenter'):
            presenter = getattr(self, attr, None)
            if presenter is not None:
                if self._waveform_scale == 'nits':
                    presenter.set_scale_mode('nits', self._scope_transfer)
                else:
                    presenter.set_scale_mode(scale)

    def _set_waveform_scale(self, mode: str, transfer: str = 'srgb') -> None:
        """波形标尺模式：'percent' | 'nits' | 'code10'（64..940）。"""
        if mode not in ('percent', 'nits', 'code10'):
            return
        self._waveform_scale = mode
        if mode == 'nits':
            self._scope_transfer = transfer if transfer in ('pq', 'hlg') \
                else 'pq'
        self.waveform_widget.set_scale_mode(mode)
        self.split_waveform.set_scale_mode(mode)
        self._apply_color_state()
        # M19.3b：单位随标尺变化 → 角标同步
        self._update_domain_badge()
        # nits 标尺会改变 _scope_transfer，进而改变 GPU CIE 线性化与
        # False Color HDR 分档 → 在途 demand 帧作废。
        self.bump_demand_revision()

    def _set_scope_primaries(self, primaries: str) -> None:
        if primaries in ('bt709', 'p3', 'bt2020'):
            self._scope_primaries = primaries
            self.cie_widget.set_color_state(self._scope_transfer, primaries)
            self.bump_demand_revision()

    def _set_zoom(self, level: int) -> None:
        """Set vectorscope zoom level."""
        if level in (1, 2) and level != self._zoom_level:
            self._zoom_level = level
            # Apply to all vectorscope widgets
            self.vectorscope_widget.set_zoom(level)
            self.split_vectorscope.set_zoom(level)
            if self._gpu_vs_presenter is not None:
                self._gpu_vs_presenter.set_vectorscope_zoom(float(level))
            # Zoom is a demand state change -> bump revision so in-flight
            # GPU scope frames for the old zoom are discarded.
            self.bump_demand_revision()

            # Reprocess if we have pending image
            if self._pending_image is not None:
                self._process_pending_image()

    def _apply_mode_page(self, mode: str, index: int) -> None:
        """CPU/GPU 页选择：全部模式都有 GPU pass，presenter 可用时保持
        GPU 页（CPU 页仅作为诊断 fallback 与 source/reference 路径）。"""
        if getattr(self, '_gpu_display_active', False):
            try:
                self.display_stack.setCurrentIndex(1)
            except Exception:
                pass

    def _on_mode_changed(self, index: int) -> None:
        """Handle mode combo box selection change."""
        mode = MODE_ORDER[index]
        if mode != self._current_mode:
            self._current_mode = mode
            self._configure_scope_layout(mode)
            self.stack.setCurrentIndex(index)
            self._apply_mode_page(mode, index)
            # Keep the GPU stack in sync so a switch while GPU display is
            # active shows the correct presenter immediately.
            if getattr(self, '_gpu_stack', None) is not None:
                self._gpu_stack.setCurrentIndex(min(index, self._gpu_stack.count() - 1))
            # Mode is a demand state change -> bump revision.
            self.bump_demand_revision()

            # 切换模式时用最后一次的缓存图像立即更新新模式的 widget
            if hasattr(self, '_last_image') and self._last_image is not None:
                self._pending_image = self._last_image
                self._pending_mask = getattr(self, '_last_mask', None)
                self._process_pending_image()

    def resizeEvent(self, event) -> None:
        """Handle resize and invalidate size-dependent scope output."""
        super().resizeEvent(event)
        # A resize changes display_size, so the next demand needs a new
        # revision to invalidate any in-flight GPU scope frame.
        self.bump_demand_revision()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        dock = self._host_dock()
        if dock is not None and getattr(self, '_bound_scope_dock', None) is not dock:
            dock.topLevelChanged.connect(self._update_layout_controls)
            self._bound_scope_dock = dock
        self._update_layout_controls()

    def hideEvent(self, event) -> None:
        """Panel hidden: clear the GPU presenter.

        Discarding the pending GPU scope demand is the controller's
        responsibility (it owns the engine reference); here we just drop the
        currently displayed frame so a stale frame is never shown on re-show.
        """
        super().hideEvent(event)
        for attr in ('_gpu_wf_presenter', '_gpu_vs_presenter', '_gpu_presenter',
                     '_gpu_parade_presenter', '_gpu_luma_presenter',
                     '_gpu_histogram_presenter', '_gpu_cie_presenter',
                     '_gpu_fc_presenter'):
            presenter = getattr(self, attr, None)
            if presenter is not None:
                try:
                    presenter.clear()
                except Exception:
                    pass

    def update_scope(self, image_bgr_float: np.ndarray, mask: Optional[np.ndarray] = None,
                     source: str = 'graded') -> None:
        """
        Update scope with new image data.

        Args:
            image_bgr_float: BGR image as float32 array (0.0-1.0 range)
            mask: Optional mask array (float32, 0.0-1.0), shape (H, W).
                  If provided, vectorscope will only display masked areas.
            source: 帧来源标签（P1-COLOR-07）——与当前输入源不符的在途
                    帧直接丢弃，保证切换后不显示旧源画面。
        """
        if image_bgr_float is None or image_bgr_float.size == 0:
            return
        if source != self._scope_source:
            return  # 在途旧源帧

        # Store pending image and mask for throttled update
        self._pending_image = image_bgr_float
        self._pending_mask = mask

        # Trigger throttled update
        if not self._update_timer.isActive():
            self._update_timer.start()

    def set_cpu_analysis_bounds(self, max_width: int | None,
                                max_height: int | None) -> None:
        """Limit CPU scope analysis without changing the displayed UI.

        A render-thread readback can be full resolution while scope detail
        remains visually unchanged beyond a moderate sample size. ``None``
        disables the bound for callers that already provide compact frames.
        """
        self._cpu_analysis_max_width = (
            max(1, int(max_width)) if max_width is not None else None
        )
        self._cpu_analysis_max_height = (
            max(1, int(max_height)) if max_height is not None else None
        )
        # M19.3b：采样口径变化 → 角标同步
        self._update_domain_badge()

    def _process_pending_image(self) -> None:
        """Process the pending image and update only the current mode's widget."""
        if self._pending_image is None:
            return

        # 面板不可见时跳过计算
        if not self.isVisible():
            self._pending_image = None
            self._pending_mask = None
            return

        image = self._pending_image
        mask = self._pending_mask

        max_width = self._cpu_analysis_max_width
        max_height = self._cpu_analysis_max_height
        if (
            max_width is not None
            and max_height is not None
            and image.ndim == 3
            and image.shape[2] >= 3
        ):
            height, width = image.shape[:2]
            scale = min(1.0, max_width / float(width), max_height / float(height))
            if scale < 1.0:
                target_size = (
                    max(1, int(round(width * scale))),
                    max(1, int(round(height * scale))),
                )
                image = cv2.resize(
                    image[:, :, :3],
                    target_size,
                    interpolation=cv2.INTER_AREA,
                )
                if mask is not None:
                    mask = cv2.resize(
                        np.asarray(mask, dtype=np.float32),
                        target_size,
                        interpolation=cv2.INTER_NEAREST,
                    )

        visible_modes = (_MULTI_LAYOUTS[self._current_mode]
                         if self._current_mode in ('quad', 'all')
                         else (self._current_mode,))
        for mode in visible_modes:
            # 只计算当前模式需要的 widget，避免后台浪费CPU
            if mode == 'waveform':
                self.waveform_widget.update_waveform(image)
            elif mode == 'vectorscope':
                if image.shape[2] >= 3:
                    image_rgb = image[:, :, ::-1]
                    self.vectorscope_widget.update_vectorscope(image_rgb, mask)
            elif mode == 'split':
                self.split_waveform.update_waveform(image)
                if image.shape[2] >= 3:
                    image_rgb = image[:, :, ::-1]
                    self.split_vectorscope.update_vectorscope(image_rgb, mask)
            elif mode == 'parade':
                self.parade_widget.update_parade(image)
            elif mode == 'luma':
                self.luma_widget.update_luma_waveform(image)
            elif mode == 'histogram':
                self.histogram_widget.update_histogram(image)
            elif mode == 'cie':
                if image.shape[2] >= 3:
                    image_rgb = image[:, :, ::-1]
                    self.cie_widget.update_cie(image_rgb, mask)
            elif mode == 'falsecolor':
                self.falsecolor_widget.update_false_color(image)

        # 缓存最后一次图像，供模式切换时使用
        self._last_image = image
        self._last_mask = mask
        # P1-COLOR-07: 按输入源缓存，切回时立即显示
        self._source_images[self._scope_source] = image

        self._pending_image = None
        self._pending_mask = None

    @property
    def current_mode(self) -> str:
        """Get the current active scope mode."""
        return self._current_mode

    def set_mode(self, mode: str) -> None:
        """Set the scope mode programmatically."""
        if mode in SCOPE_MODES and mode != self._current_mode:
            idx = MODE_ORDER.index(mode)
            self.mode_combo.blockSignals(True)
            self.mode_combo.setCurrentIndex(idx)
            self.mode_combo.blockSignals(False)
            self._on_mode_changed(idx)

    # ------------------------------------------------------------------
    # GPU scope path (native pipeline)
    # ------------------------------------------------------------------
    def demand_state(self) -> dict:
        """Snapshot the current demand state for the controller.

        Returns a dict with the values the controller needs to build a
        ``ScopeDemand``: mode, display size, vectorscope zoom, plus the
        color state (transfer/gamut) and false color variant the GPU
        passes require.  The ``revision`` is owned by the panel and
        increments on any change.
        """
        return {
            'mode': self._current_mode,
            'display_size': (self.width(), self.height()),
            'vectorscope_zoom': float(self._zoom_level),
            'scope_transfer': self._scope_transfer,
            'scope_primaries': self._scope_primaries,
            'falsecolor_mode': getattr(
                self.falsecolor_widget, '_mode', 'sdr'),
            'zebra_threshold': float(getattr(
                self.falsecolor_widget, '_zebra_threshold', 1.0)),
        }

    def _on_falsecolor_variant_changed(self, *_) -> None:
        """FalseColor mode/threshold changed: sync legend + bump revision."""
        presenter = getattr(self, '_gpu_fc_presenter', None)
        if presenter is not None and hasattr(presenter, 'set_legend'):
            mode = getattr(self.falsecolor_widget, '_mode', 'sdr')
            presenter.set_legend(f'falsecolor_{mode}')
        self.bump_demand_revision()

    def bump_demand_revision(self) -> int:
        """Increment and return the panel-side demand revision.

        Called whenever mode / size / zoom changes so the controller can
        stamp the new revision onto the next ``ScopeDemand``.
        """
        self._gpu_demand_revision = int(getattr(self, '_gpu_demand_revision', 0)) + 1
        self.demand_changed.emit()
        return self._gpu_demand_revision

    @property
    def gpu_demand_revision(self) -> int:
        return int(getattr(self, '_gpu_demand_revision', 0))

    def update_gpu_scope(self, frame) -> None:
        """Receive a ``ScopeGpuFrame`` descriptor from the renderer.

        Generation-guarded: only frames matching the current demand
        revision are forwarded to the GPU presenter.  Stale frames are
        silently dropped.  No pixel arrays are involved.
        """
        if frame is None:
            return
        if int(getattr(frame, 'revision', -1)) != self.gpu_demand_revision:
            return
        if self._scope_source != 'graded':
            return  # GPU 分析只针对调色后输出（P1-COLOR-07）
        # Present the texture.  The presenter only stores the descriptor;
        # actual GL work happens in paintGL under the GL mutex.
        self._present_gpu_frame(frame)
        # Keep presentation on the GPU page. Sharing failures degrade to an
        # empty/last frame in the presenter and never trigger CPU analysis.
        self._activate_gpu_display()

    def _activate_gpu_display(self) -> None:
        """Keep the outer display stack on the GPU page once available."""
        if getattr(self, '_gpu_display_active', False):
            return
        if self._gpu_wf_presenter is None and self._gpu_vs_presenter is None:
            return
        self._gpu_display_active = True
        try:
            self.display_stack.setCurrentIndex(1)
        except Exception:
            pass

    def _present_gpu_frame(self, frame) -> None:
        """Forward the frame to the matching GPU presenter(s).

        Multi-view layouts distribute independent texture descriptors to
        the visible presenters; single view forwards just its chosen mode.
        """
        try:
            modes = _MULTI_LAYOUTS.get(self._current_mode, (self._current_mode,))
            for prefix in modes:
                presenter = getattr(self, _MODE_PRESENTER_ATTR.get(prefix, ''), None)
                if presenter is None:
                    continue
                tex_id = getattr(frame, f'{prefix}_texture_id', None)
                size = getattr(frame, f'{prefix}_size', (0, 0))
                if tex_id is not None:
                    presenter.set_scope_frame(tex_id, *size)
        except Exception as exc:
            # AUD-25610：GPU 呈现失败不能静默——frame 描述符字段缺失/更名
            # （AttributeError）、size 解包不符（TypeError）或共享纹理失败
            # 都会让示波器停在旧帧。记录警告以便排查（对齐 P2-COLOR-12
            # "GPU 失败不能静默"原则）。
            logger.warning("GPU scope present failed: %s", exc)
