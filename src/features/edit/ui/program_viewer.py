"""
Program Viewer - Edit 页面的节目监视器。

显示时间线播放头位置的合成画面，支持：
- 播放头移动时自动请求对应帧
- 时间码叠加显示
- 全屏预览模式
- 保持视频宽高比

设计原则：
- 参考 CompositeViewer 的架构但简化
- 不需要完整工具系统，仅支持 PAN 和基础交互
- 被 TimelinePreviewRenderer 的 frame_ready 结果驱动
- 异步渲染，不阻塞 UI

Architecture:
    ┌─────────────────────────────────────────────────────────┐
    │  ProgramViewerBar (toolbar)                             │
    │  [View] [Guides]                         [Tools] [Quality] │
    └─────────────────────────────────────────────────────────┘
    ┌─────────────────────────────────────────────────────────┐
    │                                                         │
    │  ProgramViewer (QOpenGLWidget)                         │
    │                                                         │
    │   ┌─────────────────────────────────────┐               │
    │   │                                     │               │
    │   │   Video Frame Texture               │               │
    │   │   (aspect ratio preserved)          │               │
    │   │                                     │               │
    │   │                                     │               │
    │   └─────────────────────────────────────┘               │
    │                                                         │
    │   ┌──────────────────────┐                              │
    │   │ HH:MM:SS:FF          │  ← Timecode overlay          │
    │   └──────────────────────┘                              │
    │                                                         │
    └─────────────────────────────────────────────────────────┘

Signals:
    viewport_resized: 预览视口尺寸变化
    fullscreen_requested: 进入/退出全屏
"""

import ctypes
import ctypes.util
import logging
import math
import numpy as np
import time
from typing import Optional, Tuple, Dict, Any

from PyQt6.QtOpenGLWidgets import QOpenGLWidget
from PyQt6.QtWidgets import (
    QWidget, QFrame, QHBoxLayout, QVBoxLayout, QToolButton,
    QLabel, QSizePolicy,
    QMenu, QComboBox,
)
from PyQt6.QtCore import (
    Qt, pyqtSignal, QPoint, QPointF, QSize, QRectF, QSignalBlocker,
)
from PyQt6.QtGui import (
    QMouseEvent, QWheelEvent, QKeyEvent, QPainter, QColor, QPen, QBrush,
    QFont, QFontMetrics, QImage, QTransform, QPolygon,
)

from src.shared.i18n.i18n import I18N
from src.shared.assets.icon_manager import get_icon
from src.shared.assets.icon_manager import (
    icon_jump_start, icon_step_back, icon_play, icon_pause, icon_step_forward,
    icon_jump_end, icon_loop, IconColors,
)
from src.features.edit.ui.preview_view_transform import PreviewViewTransform
from src.features.edit.ui.edit_style import EDIT_COLORS
from src.features.edit.core.playback_debug import playback_trace
from src.features.edit.ui.subtitle_handle_controller import SubtitleHandleController
from src.features.edit.ui.clip_transform_handle_controller import (
    ClipTransformHandleController,
)
from src.shared.ui.widgets.viewport_toolbar_base import ViewportToolBarBase
from src.shared.ui.widgets.chevron_combo import ChevronComboBox

logger = logging.getLogger(__name__)


def _modifiers_int(modifiers) -> int:
    """Coerce a Qt.KeyboardModifiers value to an int bitmask.

    PyQt6's strict enums reject ``int(keyboard_modifier)`` (raises TypeError),
    so we read ``.value`` when present. Drag handle controllers expect a raw
    int so they can bit-test for Shift (see viewer_handles.compute_transform_delta_params).
    """
    if isinstance(modifiers, int):
        return modifiers
    return int(getattr(modifiers, "value", 0))


def _rgb888_qimage_to_rgba(qimage: QImage) -> Optional[np.ndarray]:
    """Extract a Format_RGB888 QImage into a compact HxWx4 RGBA buffer.

    AUD-20400: QImage scanlines are padded to 32-bit alignment
    (``bytesPerLine() >= width * 3``). When ``width * 3 % 4 != 0``
    (e.g. width 1366/854) the raw buffer must NOT be interpreted with a
    compact row pitch — each row has to be sliced out of its padded
    stride and re-packed, or the whole image shifts over by 1-2 bytes
    per row (diagonal skew).

    Returns a writable ``(height, width, 4)`` uint8 array with alpha=255,
    or None when the image is empty / has invalid dimensions.
    """
    width = qimage.width()
    height = qimage.height()
    if width <= 0 or height <= 0:
        return None
    bits = qimage.bits()
    bytes_per_pixel = 3  # RGB888
    stride = qimage.bytesPerLine()
    row_bytes = width * bytes_per_pixel
    bits_size = width * height * bytes_per_pixel
    if stride != row_bytes:
        # 行跨距 ≠ 紧凑行宽：逐行切片去 padding（切片是视图，不拷贝）
        bits.setsize(height * stride)
        padded = np.frombuffer(
            bits, dtype=np.uint8, count=height * stride,
        ).reshape(height, stride)
        rgb = padded[:, :row_bytes].reshape(height, width, 3)
    else:
        bits.setsize(bits_size)
        rgb = np.frombuffer(
            bits, dtype=np.uint8, count=bits_size,
        ).reshape(height, width, 3)
    # AUD-20406: 直接预分配 RGBA 目标，RGB 一次跨距拷贝入位 + alpha 广播
    # 填充 —— 取代 concatenate（+ ascontiguousarray）的 1-2 份全帧临时数组，
    # 每帧 CPU 上传路径最多省 2 次全帧级拷贝。
    rgba = np.empty((height, width, 4), dtype=np.uint8)
    rgba[:, :, :3] = rgb
    rgba[:, :, 3] = 255
    return rgba


# ── OpenGL bindings via PyOpenGL ──────────────────────────────────────────────
# Windows 上 ctypes 直接加载 opengl32.dll 无法获取 OpenGL 2.0+ 函数
# 使用 PyOpenGL 提供跨平台的 OpenGL 绑定

from OpenGL.GL import (
    # Constants
    GL_VERTEX_SHADER, GL_FRAGMENT_SHADER, GL_ARRAY_BUFFER, GL_STATIC_DRAW,
    GL_TRIANGLE_STRIP, GL_TEXTURE0, GL_TEXTURE_2D, GL_RGBA,
    GL_UNSIGNED_BYTE, GL_FLOAT, GL_FALSE, GL_TRUE,
    GL_COMPILE_STATUS, GL_LINK_STATUS,
    GL_COLOR_BUFFER_BIT, GL_TEXTURE_MIN_FILTER, GL_TEXTURE_MAG_FILTER,
    GL_LINEAR, GL_TEXTURE_WRAP_S, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE,
    GL_UNPACK_ALIGNMENT,
    GL_FRAMEBUFFER, GL_READ_FRAMEBUFFER, GL_DRAW_FRAMEBUFFER,
    GL_COLOR_ATTACHMENT0, GL_NEAREST,
    GL_SCISSOR_TEST, GL_DEPTH_TEST, GL_BLEND,
    GL_PIXEL_PACK_BUFFER, GL_STREAM_READ, GL_STREAM_DRAW,
    # Functions
    glCreateShader, glShaderSource, glCompileShader, glDeleteShader,
    glCreateProgram, glAttachShader, glLinkProgram, glDeleteProgram, glUseProgram,
    glGenBuffers, glDeleteBuffers, glBindBuffer, glBufferData, glBufferSubData,
    glGenVertexArrays, glDeleteVertexArrays, glBindVertexArray,
    glGetAttribLocation, glEnableVertexAttribArray, glVertexAttribPointer,
    glGetShaderiv, glGetProgramiv, glGetShaderInfoLog, glGetProgramInfoLog,
    glGetUniformLocation, glUniform1i, glUniform2f,
    glViewport, glClearColor, glClear, glActiveTexture, glBindTexture,
    glDrawArrays, glGenTextures, glDeleteTextures, glTexImage2D,
    glTexSubImage2D, glPixelStorei, glTexParameteri, glGetError,
    glIsTexture, glGenFramebuffers, glBindFramebuffer,
    glFramebufferTexture2D, glBlitFramebuffer, glDisable, glColorMask,
    glReadPixels, glGetBufferSubData,
    # R21 资源有效性校验用（c1cf36fe5 引入调用但未入 wrapper 清单——
    # 首次视频 paint 即 AttributeError 使 Qt abort，真实 App 驱动基准暴露）。
    glIsProgram, glIsBuffer, glIsVertexArray,
)

# PyOpenGL wrapper for easier usage (matches original _gl.xxx pattern)
class _GLWrapper:
    """Wrapper to make PyOpenGL functions callable with _gl.xxx pattern."""
    glCreateShader = glCreateShader
    glShaderSource = glShaderSource
    glCompileShader = glCompileShader
    glDeleteShader = glDeleteShader
    glCreateProgram = glCreateProgram
    glAttachShader = glAttachShader
    glLinkProgram = glLinkProgram
    glDeleteProgram = glDeleteProgram
    glUseProgram = glUseProgram
    glGenBuffers = glGenBuffers
    glDeleteBuffers = glDeleteBuffers
    glBindBuffer = glBindBuffer
    glBufferData = glBufferData
    glBufferSubData = glBufferSubData
    glGenVertexArrays = glGenVertexArrays
    glDeleteVertexArrays = glDeleteVertexArrays
    glBindVertexArray = glBindVertexArray
    glGetAttribLocation = glGetAttribLocation
    glEnableVertexAttribArray = glEnableVertexAttribArray
    glVertexAttribPointer = glVertexAttribPointer
    glGetShaderiv = glGetShaderiv
    glGetProgramiv = glGetProgramiv
    glGetShaderInfoLog = glGetShaderInfoLog
    glGetProgramInfoLog = glGetProgramInfoLog
    glGetUniformLocation = glGetUniformLocation
    glUniform1i = glUniform1i
    glUniform2f = glUniform2f
    glViewport = glViewport
    glClearColor = glClearColor
    glClear = glClear
    glActiveTexture = glActiveTexture
    glBindTexture = glBindTexture
    glDrawArrays = glDrawArrays
    glGenTextures = glGenTextures
    glDeleteTextures = glDeleteTextures
    glTexImage2D = glTexImage2D
    glTexSubImage2D = glTexSubImage2D
    glPixelStorei = glPixelStorei
    glTexParameteri = glTexParameteri
    glGenFramebuffers = glGenFramebuffers
    glBindFramebuffer = glBindFramebuffer
    glFramebufferTexture2D = glFramebufferTexture2D
    glBlitFramebuffer = glBlitFramebuffer
    glDisable = glDisable
    glColorMask = glColorMask
    glReadPixels = glReadPixels
    glGetBufferSubData = glGetBufferSubData
    glGetError = glGetError
    glIsTexture = glIsTexture
    glIsProgram = glIsProgram
    glIsBuffer = glIsBuffer
    glIsVertexArray = glIsVertexArray

for _name, _value in list(_GLWrapper.__dict__.items()):
    if _name.startswith("_") or not callable(_value):
        continue
    setattr(_GLWrapper, _name, staticmethod(_value))

_gl = _GLWrapper()


# ================================================================
# ProgramViewerBar - Toolbar
# ================================================================

class ProgramViewerBar(ViewportToolBarBase):
    """
    Compact toolbar overlay for the ProgramViewer.

    Provides:
    - View menu (Zoom In/Out, Fit, Actual Size, Fullscreen)
    - Left-aligned View and Guides menus
    - Right-aligned edit/display tools
    - Preview quality menu
    - Zoom percentage display
    """

    # Page-specific signals (common signals inherited from ViewPortToolBarBase)
    preview_quality_changed = pyqtSignal(float)  # scale: 0.0(auto), 1.0, 0.5, 0.333, 0.25
    # Transform tool: toggles the on-canvas clip transform handles.
    transform_tool_requested = pyqtSignal(bool)
    timeline_switch_requested = pyqtSignal(str)

    def __init__(self, viewer, parent=None):
        super().__init__("programViewerBar", parent)
        self._viewer = viewer

        layout = self._layout

        # Navigation menus stay together on the left.
        self.view_menu_btn = self._make_menu_button("View")
        layout.addWidget(self.view_menu_btn)
        self.view_menu = self._create_view_menu()
        self.view_menu_btn.clicked.connect(
            lambda: self._show_menu_at_button(self.view_menu, self.view_menu_btn))

        # Guides button
        self._make_guides_btn()
        layout.addWidget(self.guides_menu_btn)

        # Keep interactive controls grouped on the right side of the toolbar.
        right_spacer = QWidget()
        right_spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        layout.addWidget(right_spacer)

        # Transform tool button — toggles on-canvas move/scale/rotate handles
        # for the selected clip.
        self.transform_btn = QToolButton()
        self.transform_btn.setIcon(get_icon('fa5s.arrows-alt', IconColors.WHITE))
        self.transform_btn.setIconSize(QSize(14, 14))
        self.transform_btn.setFixedSize(26, 22)
        self.transform_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.transform_btn.setAutoRaise(True)
        self.transform_btn.setStyleSheet(self._BTN_STYLE)
        self.transform_btn.setCheckable(True)
        self.transform_btn.setChecked(False)
        self.transform_btn.setToolTip(
            I18N.tr("tooltip.transform_tool", "Transform Tool (move/scale/rotate)"))
        self.transform_btn.toggled.connect(self.transform_tool_requested.emit)
        layout.addWidget(self.transform_btn)

        self._add_toolbar_separator()

        # Display tools
        self._make_mask_btn()
        layout.addWidget(self.mask_btn)

        # Fit button
        self._make_fit_btn()
        layout.addWidget(self.fit_btn)

        # Split button
        self._make_split_btn()
        layout.addWidget(self.split_btn)

        self._add_toolbar_separator()

        # Preview proxy resolution uses the same compact menu treatment as
        # the navigation controls, rather than a native-looking combo box.
        self._quality_labels: Dict[float, str] = {
            0.0: I18N.tr("preview_quality.smooth", "流畅"),
            1.0: I18N.tr("preview_quality.full", "完整"),
            0.5: "1/2",
            round(1.0 / 3.0, 4): "1/3",
            0.25: "1/4",
        }
        self._preview_quality: float = 0.0
        self.quality_menu_btn = self._make_menu_button("")
        self.quality_menu_btn.setObjectName("previewQualityMenu")
        self.quality_menu_btn.setToolTip(
            I18N.tr("tooltip.preview_quality", "预览代理分辨率"))
        self.quality_menu = self._create_quality_menu()
        self.quality_menu_btn.clicked.connect(
            lambda: self._show_menu_at_button(self.quality_menu, self.quality_menu_btn))
        self._update_quality_menu_label()
        layout.addWidget(self.quality_menu_btn)

        # Zoom percentage label
        self._add_fps_indicator()
        self._make_zoom_label()
        layout.addWidget(self.zoom_label)

        # The active timeline indicator belongs to this toolbar row. Keep it
        # outside the layout so it can stay exactly centered in the full row,
        # independent of the different-width left and right control groups.
        self._timeline_selector = ProgramTimelineSelector(self)
        self._timeline_selector.timeline_switch_requested.connect(
            self.timeline_switch_requested.emit
        )
        self._timeline_selector.hide()
        self._position_timeline_selector()

    def resizeEvent(self, event: Any) -> None:
        """Keep the timeline selector centered in the toolbar row."""
        super().resizeEvent(event)
        self._position_timeline_selector()

    def _position_timeline_selector(self) -> None:
        """Position the borderless timeline selector at toolbar center."""
        if not hasattr(self, "_timeline_selector"):
            return
        selector = self._timeline_selector
        width = min(320, max(80, selector.sizeHint().width()))
        x = max(0, (self.width() - width) // 2)
        selector.setGeometry(x, 0, width, self.height())
        selector.raise_()

    def set_timeline_choices(
        self,
        timelines: Any,
        active_timeline_id: Optional[str],
    ) -> None:
        """Show all project timelines and mark the active one."""
        self._timeline_selector.set_timeline_choices(timelines, active_timeline_id)
        self._position_timeline_selector()

    def _add_toolbar_separator(self) -> None:
        """Add a subtle fixed separator between right-aligned tool groups."""
        separator = QFrame()
        separator.setFrameShape(QFrame.Shape.VLine)
        separator.setFrameShadow(QFrame.Shadow.Plain)
        separator.setFixedSize(1, 14)
        separator.setStyleSheet("color: #45464c;")
        self._layout.addWidget(separator)

    def _create_view_menu(self) -> QMenu:
        """Create the View dropdown menu for the Edit page."""
        menu = QMenu(self)
        menu.addAction(I18N.tr("viewport.zoom_in", "Zoom In"), self.zoom_in_requested.emit)
        menu.addAction(I18N.tr("viewport.zoom_out", "Zoom Out"), self.zoom_out_requested.emit)
        menu.addSeparator()
        menu.addAction(I18N.tr("viewport.fit", "Fit to Window"), self.fit_requested.emit)
        menu.addAction(I18N.tr("viewport.actual_size", "Actual Size"), self.actual_size_requested.emit)
        menu.addSeparator()
        menu.addAction(I18N.tr("viewport.fullscreen", "Fullscreen"), self.fullscreen_requested.emit)
        return menu

    def _create_quality_menu(self) -> QMenu:
        """Create the checkable preview-quality menu."""
        menu = QMenu(self)
        for scale, label in self._quality_labels.items():
            action = menu.addAction(label)
            action.setCheckable(True)
            action.setData(scale)
            action.setChecked(scale == self._preview_quality)
            action.triggered.connect(lambda checked=False, value=scale: self._set_preview_quality(value))
        return menu

    def _set_preview_quality(self, scale: float) -> None:
        """Update the selected proxy resolution and notify the renderer."""
        self._preview_quality = scale
        for action in self.quality_menu.actions():
            action.setChecked(float(action.data()) == scale)
        self._update_quality_menu_label()
        self.preview_quality_changed.emit(scale)

    def _update_quality_menu_label(self) -> None:
        """Show the active preview quality in the compact menu button."""
        label = self._quality_labels[self._preview_quality]
        self.quality_menu_btn.setText(
            I18N.tr("preview_quality.menu_label", "{quality}").format(quality=label))


# ================================================================
# ProgramTimelineSelector - Active timeline switcher
# ================================================================

class ProgramTimelineSelector(ChevronComboBox):
    """Borderless timeline selector shown over the Program monitor.

    The popup keeps the shared ``ChevronComboBox`` styling, while the closed
    control is intentionally reduced to centered text plus a chevron so it
    matches the compact timeline indicator used by DaVinci Resolve.
    """

    timeline_switch_requested = pyqtSignal(str)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("programTimelineSelector")
        self.setFixedHeight(28)
        # Keep short names compact so the chevron sits close to the label.
        # The popup itself can still show arbitrarily long timeline names.
        self.setMinimumWidth(80)
        self.setMaximumWidth(320)
        self.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setToolTip(
            I18N.tr("edit.program_viewer.switch_timeline", "Switch timeline")
        )
        self.setStyleSheet("""
            QComboBox#programTimelineSelector {
                background: transparent;
                border: none;
                border-radius: 0px;
                padding: 2px 22px 2px 10px;
                color: #f0f0f0;
                font-size: 12px;
                font-weight: 600;
                min-height: 24px;
                max-height: 24px;
            }
            QComboBox#programTimelineSelector:hover {
                color: #ffffff;
            }
            QComboBox#programTimelineSelector:focus {
                background: transparent;
                border: none;
            }
            QComboBox#programTimelineSelector::drop-down {
                border: none;
                width: 0px;
            }
            QComboBox#programTimelineSelector::down-arrow {
                image: none;
                border: none;
            }
        """)
        self.currentIndexChanged.connect(self._on_index_changed)

    def set_timeline_choices(
        self,
        timelines: Any,
        active_timeline_id: Optional[str],
    ) -> None:
        """Replace the choices and preserve the active timeline selection."""
        entries = list(getattr(timelines, "items", lambda: ())())
        blocker = QSignalBlocker(self)
        try:
            self.clear()
            for timeline_id, timeline_ref in entries:
                name = str(getattr(timeline_ref, "name", "") or timeline_id)
                self.addItem(name, str(timeline_id))

            if not entries:
                self.hide()
                return

            active_index = self.findData(str(active_timeline_id))
            self.setCurrentIndex(active_index if active_index >= 0 else 0)
            self.show()
        finally:
            del blocker

    def _preferred_popup_width(self) -> int:
        """Return a popup width that can display the longest timeline name."""
        metrics = QFontMetrics(self.font())
        longest_name = max(
            (metrics.horizontalAdvance(self.itemText(index))
             for index in range(self.count())),
            default=0,
        )
        # Include list-view margins, horizontal padding, and a little room
        # for the popup's native frame without changing the compact closed UI.
        return max(self.width(), longest_name + 36)

    def showPopup(self) -> None:
        """Open a wider popup when a timeline name needs more room."""
        super().showPopup()
        popup = self.view().window()
        if popup is None:
            return

        popup_width = self._preferred_popup_width()
        popup.setMinimumWidth(popup_width)
        popup.resize(popup_width, popup.height())

        # Reposition after resizing so a long list remains inside the screen.
        pos = self.mapToGlobal(QPointF(0, self.height()))
        x = int(pos.x())
        y = int(pos.y()) + 2
        screen = self.screen()
        if screen:
            available = screen.availableGeometry()
            x = min(x, available.right() - popup_width + 1)
            x = max(x, available.left())
        popup.move(x, y)

    def _on_index_changed(self, index: int) -> None:
        """Emit only real timeline IDs; placeholders can never switch state."""
        timeline_id = self.itemData(index)
        if timeline_id:
            self.timeline_switch_requested.emit(str(timeline_id))


# ================================================================
# TransportControlBar - Playback transport controls
# ================================================================

class TransportControlBar(QFrame):
    """
    Compact transport control bar for the Program Viewer bottom.

    Provides playback controls: jump_start, prev_frame, play/pause,
    next_frame, jump_end, loop, speed indicator, and timecode display.

    Signals are emitted for each button click so that an external
    controller (TimelinePanel) can handle the actual playback logic.
    """

    # Signals for transport actions
    jump_start_clicked = pyqtSignal()
    prev_frame_clicked = pyqtSignal()
    play_clicked = pyqtSignal()
    next_frame_clicked = pyqtSignal()
    jump_end_clicked = pyqtSignal()
    loop_toggled = pyqtSignal(bool)
    timecode_clicked = pyqtSignal()  # Request timecode input dialog

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("transportControlBar")
        self.setFixedHeight(36)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setAutoFillBackground(False)

        self._is_playing: bool = False
        self._loop_enabled: bool = False

        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 2, 8, 4)
        layout.setSpacing(3)

        BTN_SIZE = 28
        ICON_SIZE = 15
        PLAY_BTN_SIZE = 32

        # Apply background style
        self.setStyleSheet("""
            QFrame#transportControlBar {
                background-color: #1a1a1c;
                border-top: 1px solid #2a2a2a;
            }
        """)

        # --- Transport Controls ---
        # Jump to start
        self.btn_jump_start = QToolButton()
        self.btn_jump_start.setToolTip(I18N.tr("edit.playback_control.jump_start", "Jump to Start (Home)"))
        self.btn_jump_start.setFixedSize(BTN_SIZE, BTN_SIZE)
        self.btn_jump_start.setIconSize(QSize(ICON_SIZE, ICON_SIZE))
        self.btn_jump_start.setIcon(icon_jump_start(IconColors.DEFAULT))
        self.btn_jump_start.clicked.connect(self.jump_start_clicked.emit)
        layout.addWidget(self.btn_jump_start)

        # Previous frame
        self.btn_prev_frame = QToolButton()
        self.btn_prev_frame.setToolTip(I18N.tr("edit.playback_control.prev_frame", "Previous Frame (Left)"))
        self.btn_prev_frame.setFixedSize(BTN_SIZE, BTN_SIZE)
        self.btn_prev_frame.setIconSize(QSize(ICON_SIZE, ICON_SIZE))
        self.btn_prev_frame.setIcon(icon_step_back(IconColors.DEFAULT))
        self.btn_prev_frame.clicked.connect(self.prev_frame_clicked.emit)
        layout.addWidget(self.btn_prev_frame)

        # Play/Pause (larger button, centered)
        self.btn_play = QToolButton()
        self.btn_play.setToolTip(I18N.tr("edit.playback_control.play_pause", "Play/Pause (Space)"))
        self.btn_play.setFixedSize(PLAY_BTN_SIZE, BTN_SIZE)
        self.btn_play.setIconSize(QSize(ICON_SIZE + 3, ICON_SIZE + 3))
        self.btn_play.setIcon(icon_play(IconColors.ORANGE))
        self.btn_play.clicked.connect(self.play_clicked.emit)
        layout.addWidget(self.btn_play)

        # Next frame
        self.btn_next_frame = QToolButton()
        self.btn_next_frame.setToolTip(I18N.tr("edit.playback_control.next_frame", "Next Frame (Right)"))
        self.btn_next_frame.setFixedSize(BTN_SIZE, BTN_SIZE)
        self.btn_next_frame.setIconSize(QSize(ICON_SIZE, ICON_SIZE))
        self.btn_next_frame.setIcon(icon_step_forward(IconColors.DEFAULT))
        self.btn_next_frame.clicked.connect(self.next_frame_clicked.emit)
        layout.addWidget(self.btn_next_frame)

        # Jump to end
        self.btn_jump_end = QToolButton()
        self.btn_jump_end.setToolTip(I18N.tr("edit.playback_control.jump_end", "Jump to End (End)"))
        self.btn_jump_end.setFixedSize(BTN_SIZE, BTN_SIZE)
        self.btn_jump_end.setIconSize(QSize(ICON_SIZE, ICON_SIZE))
        self.btn_jump_end.setIcon(icon_jump_end(IconColors.DEFAULT))
        self.btn_jump_end.clicked.connect(self.jump_end_clicked.emit)
        layout.addWidget(self.btn_jump_end)

        # Loop toggle
        self.btn_loop = QToolButton()
        self.btn_loop.setToolTip(I18N.tr("edit.playback_control.loop", "Loop (L)"))
        self.btn_loop.setFixedSize(BTN_SIZE, BTN_SIZE)
        self.btn_loop.setIconSize(QSize(ICON_SIZE, ICON_SIZE))
        self.btn_loop.setIcon(icon_loop(IconColors.DEFAULT))
        self.btn_loop.setCheckable(True)
        self.btn_loop.clicked.connect(self._on_loop_clicked)
        layout.addWidget(self.btn_loop)

        # Speed indicator
        self.speed_label = QLabel("1x")
        self.speed_label.setStyleSheet(
            f"color: {EDIT_COLORS['text_muted']}; font-size: 12px; font-weight: bold;"
            f" background: transparent;"
        )
        self.speed_label.setFixedWidth(32)
        self.speed_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.speed_label)

        # Center stretch
        layout.addStretch()

        # Timecode display
        self.timecode_label = QLabel("00:00:00:00")
        self.timecode_label.setStyleSheet(f"""
            QLabel {{
                color: {EDIT_COLORS['text']};
                font-size: 14px;
                font-weight: bold;
                background-color: {EDIT_COLORS['bg_input']};
                padding: 3px 8px;
                border-radius: 4px;
                font-family: 'Courier New', monospace;
            }}
            QLabel:hover {{
                background-color: {EDIT_COLORS['bg_panel_alt']};
            }}
        """)
        self.timecode_label.setFixedWidth(110)
        self.timecode_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.timecode_label.setCursor(Qt.CursorShape.PointingHandCursor)
        self.timecode_label.mousePressEvent = self._on_timecode_clicked
        layout.addWidget(self.timecode_label)

        # Apply clean transport button style (no heavy backgrounds)
        transport_btn_style = """
            QToolButton {
                background: transparent;
                border: none;
                border-radius: 4px;
                padding: 3px;
                margin: 1px;
            }
            QToolButton:hover {
                background-color: rgba(255, 255, 255, 18);
            }
            QToolButton:pressed {
                background-color: rgba(255, 255, 255, 10);
            }
            QToolButton:checked {
                background: transparent;
            }
            QToolButton:checked:hover {
                background-color: rgba(255, 255, 255, 18);
            }
        """
        for i in range(layout.count()):
            item = layout.itemAt(i)
            if item and item.widget() and isinstance(item.widget(), QToolButton):
                item.widget().setStyleSheet(transport_btn_style)

    def _on_loop_clicked(self) -> None:
        """Handle loop button toggle."""
        self._loop_enabled = self.btn_loop.isChecked()
        if self._loop_enabled:
            self.btn_loop.setIcon(icon_loop(IconColors.ORANGE))
        else:
            self.btn_loop.setIcon(icon_loop(IconColors.DEFAULT))
        self.loop_toggled.emit(self._loop_enabled)

    def _on_timecode_clicked(self, event: Any) -> None:
        """Handle timecode label click."""
        if event.button() == Qt.MouseButton.LeftButton:
            self.timecode_clicked.emit()

    def update_timecode(self, timecode: str) -> None:
        """Update the timecode display."""
        self.timecode_label.setText(timecode)

    def update_play_button(self, is_playing: bool) -> None:
        """Update play button icon based on playback state."""
        self._is_playing = is_playing
        if is_playing:
            self.btn_play.setIcon(icon_pause(IconColors.ORANGE))
        else:
            self.btn_play.setIcon(icon_play(IconColors.ORANGE))

    def update_speed_label(self, speed: float, direction: int = 1) -> None:
        """Update speed indicator label."""
        direction_symbol = "" if direction == 1 else "-"
        self.speed_label.setText(f"{direction_symbol}{speed}x")
        if speed != 1.0:
            self.speed_label.setStyleSheet(
                f"color: {EDIT_COLORS['accent']}; font-size: 12px; font-weight: bold;"
                f" background: transparent;"
            )
        else:
            self.speed_label.setStyleSheet(
                f"color: {EDIT_COLORS['text_muted']}; font-size: 12px; font-weight: bold;"
                f" background: transparent;"
            )

    def set_loop_checked(self, checked: bool) -> None:
        """Set loop button checked state without emitting signal."""
        self.btn_loop.blockSignals(True)
        self.btn_loop.setChecked(checked)
        self._loop_enabled = checked
        if checked:
            self.btn_loop.setIcon(icon_loop(IconColors.ORANGE))
        else:
            self.btn_loop.setIcon(icon_loop(IconColors.DEFAULT))
        self.btn_loop.blockSignals(False)


# ================================================================
# ProgramViewer - OpenGL Viewport
# ================================================================

class ProgramViewer(QOpenGLWidget):
    """
    OpenGL viewport for the Program Viewer (Edit page).

    Displays video frames at the current playhead position with:
    - Aspect ratio preservation
    - Zoom/Pan support
    - Timecode overlay
    - Safe frame indicators (optional)
    - Fullscreen mode

    Signals:
        zoom_changed: Zoom level changed
        fullscreen_changed: Fullscreen state changed
        double_clicked: Double-click event (for fullscreen trigger)
        viewport_resized: Viewport size changed (P8-02)
    """

    zoom_changed = pyqtSignal(float)
    fullscreen_changed = pyqtSignal(bool)
    double_clicked = pyqtSignal()
    viewport_resized = pyqtSignal(int, int)  # width, height (P8-02)
    gl_initialized = pyqtSignal(object)  # QOpenGLContext (for GPU pipeline)
    monitor_frame_ready = pyqtSignal(int, QImage)  # GPU readback for external monitoring
    monitor_gpu_frame_ready = pyqtSignal(int, int, int, int, bool)  # frame, tex, w, h, gl-oriented
    frame_presented = pyqtSignal(int)  # emitted after a frame is painted

    # P10.3 字幕手柄交互信号（与 composite CompositeViewer 对齐）
    subtitle_interaction_started = pyqtSignal(str)             # clip_id
    subtitle_interaction_updated = pyqtSignal(str, dict)       # clip_id, delta_dict
    subtitle_interaction_ended = pyqtSignal(str, dict, dict)   # clip_id, final_delta, original

    # Clip Transform on-canvas handle interaction.
    # clip_id + new TransformParams (the controller reports a whole new
    # TransformParams, not a delta, because the drag math recomputes params
    # absolutely each move).
    clip_transform_interaction_updated = pyqtSignal(str, object)              # clip_id, TransformParams
    clip_transform_interaction_ended = pyqtSignal(str, object, object)        # clip_id, final, original
    # Tool-mode toggle from the viewer toolbar.
    transform_tool_toggled = pyqtSignal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("ProgramViewer")

        # Texture state
        self._current_tex_id: Optional[int] = None
        # AUD-20402: viewer-owned texture queued for deletion by clear_texture;
        # the glDeleteTextures happens at the start of the next paintGL, where
        # this widget's GL context is guaranteed current (deferred release).
        self._pending_tex_delete: Optional[int] = None
        self._tex_width: int = 0
        self._tex_height: int = 0
        self._video_aspect_ratio: float = 16 / 9  # Default aspect ratio
        self._shared_tex_id: Optional[int] = None  # GPU pipeline: shared texture from render thread
        self._shared_tex_width: int = 0
        self._shared_tex_height: int = 0
        self._current_tex_is_gl_oriented: bool = False
        self._copy_read_fbo: Optional[int] = None
        self._copy_draw_fbo: Optional[int] = None
        self._monitor_readback_enabled: bool = False
        self._monitor_gpu_signal_enabled: bool = False
        self._monitor_readback_pbos: list[int] = []
        self._monitor_readback_size: tuple[int, int] = (0, 0)
        self._monitor_readback_index: int = 0
        self._monitor_readback_pending: Optional[tuple[int, int, int, bool]] = None
        self._monitor_readback_last_emit_ts: float = 0.0
        self._monitor_readback_debug_last: dict[str, float] = {}

        # Batch 1 (缺陷计划 RP-P0-01/02): present frame fence + ack 协议。
        # _pending_present_frame: 待 paintGL 处理的 GpuPresentFrame（携带 fence/slot_id）。
        # _pending_present_ack: blit 完成后调用的 ack 回调（释放 renderer slot）。
        # _gl_fence_wait_fn: 注入的 fence wait 函数（测试用 mock；生产用 glWaitSync）。
        self._pending_present_frame: Optional[object] = None
        self._pending_present_ack: Optional[callable] = None
        self._gl_fence_wait_fn: Optional[callable] = None

        # P2.1: Pending QImage for GL upload (MVP)
        self._pending_qimage: Optional[QImage] = None

        # P8: Error state display
        self._error_state: str = "none"  # "none", "no_clip", "render_error", "decode_error", "composite_error", "offline_media"
        self._error_message: str = ""
        self._show_source_ghost: bool = False  # R-8: show ghosted source on composite error
        self._offline_media: tuple[dict[str, str], ...] = ()

        # GL resources
        self._gl_initialized: bool = False
        self._quad_vbo: Optional[int] = None
        self._vao: Optional[int] = None
        self._prog: Optional[int] = None
        # AUD-20405: u_texture 的UniformLocation 只依赖 program，缓存避免每帧查询。
        self._tex_uniform_loc: Optional[int] = None

        # Zoom/Pan state
        self._view_transform = PreviewViewTransform()
        self._is_panning: bool = False
        self._pan_start_pos: QPoint = QPoint()
        self._pan_start_px: tuple[float, float] = (0.0, 0.0)
        self._active_guide: Optional[Dict[str, Any]] = None

        # Timecode overlay
        self._timecode: str = "00:00:00:00"
        self._fps: float = 24.0

        # P10.3: Subtitle overlay state. _subtitle_overlays holds the list of
        # active SubtitleClip objects at the current playhead frame. The clips
        # themselves are rendered to RGBA on demand in _paint_subtitle_overlay
        # (cached by content signature, see subtitle_renderer).
        self._subtitle_overlays: list = []
        self._subtitle_canvas_size: tuple[int, int] = (1920, 1080)
        # Cache the small, already-converted glyph images.  The renderer cache
        # owns float32 glyph textures; this UI cache avoids recreating QImages
        # on every paint tick while a subtitle is visible.
        self._subtitle_qimage_cache: dict[tuple[object, ...], QImage] = {}

        # P10.3: 字幕手柄控制器（处理 drag/缩放/旋转的命中测试和绘制）
        self._subtitle_handle_controller = SubtitleHandleController(self)
        self._subtitle_handle_controller.on_drag_started = self._on_subtitle_drag_started
        self._subtitle_handle_controller.on_drag_updated = self._on_subtitle_drag_updated
        self._subtitle_handle_controller.on_drag_ended = self._on_subtitle_drag_ended

        # Clip Transform on-canvas handle controller. Active only when the
        # Transform tool is toggled on (see set_transform_tool_active).
        self._clip_transform_controller = ClipTransformHandleController(self)
        self._clip_transform_controller.on_drag_updated = self._on_clip_transform_drag_updated
        self._clip_transform_controller.on_drag_ended = self._on_clip_transform_drag_ended
        self._transform_tool_active: bool = False

        # Fullscreen state
        self._is_fullscreen: bool = False
        self._fullscreen_window: Optional[QWidget] = None
        self._original_parent: Optional[QWidget] = None  # P8: 保存原始父级
        self._saved_geometry: Optional[QRectF] = None

        # Current frame info (for requesting frames)
        self._current_clip_id: Optional[str] = None
        self._current_frame_index: int = 0

        # Mouse tracking for pan
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        logger.debug("ProgramViewer initialized")

    # ==================== Initialization ====================

    def initializeGL(self) -> None:
        """Initialize OpenGL resources."""
        # GUI GL 栈计数：让 EditGpuPreviewRenderer 的延迟共享上下文初始化
        # 能区分"GL 调用栈上"与"paint 后 context 常驻 current"（后者是
        # QOpenGLWidget 的正常状态，不应阻塞初始化）。
        from src.features.edit.core.edit_gpu_preview_renderer import (
            enter_gui_gl_stack,
            exit_gui_gl_stack,
        )
        enter_gui_gl_stack()
        try:
            self._initializeGL_impl()
        finally:
            exit_gui_gl_stack()

    def _initializeGL_impl(self):
        # Clear any pending GL errors from context setup
        while _gl.glGetError() != 0:
            pass
        try:
            # AUD-20405: program 将被重建 → uniform 位置缓存随之失效。
            self._tex_uniform_loc = None
            # Create shader program for texture display
            self._prog = self._create_shader_program(
                """
                #version 330
                in vec2 in_vert;
                in vec2 in_texcoord;
                out vec2 v_texcoord;
                void main() {
                    gl_Position = vec4(in_vert, 0.0, 1.0);
                    v_texcoord = in_texcoord;
                }
                """,
                """
                #version 330
                uniform sampler2D u_texture;
                in vec2 v_texcoord;
                out vec4 f_color;
                void main() {
                    f_color = texture(u_texture, v_texcoord);
                }
                """
            )
            # Create quad VBO (full-screen quad for texture display)
            self._create_quad_vbo()

            # Set clear color (dark background)
            _gl.glClearColor(0.08, 0.08, 0.08, 1.0)

            self._gl_initialized = True
            logger.info("ProgramViewer GL initialized")

            # Emit GL context for GPU pipeline (shared context creation)
            ctx = self.context()
            if ctx is not None:
                self.gl_initialized.emit(ctx)

        except Exception as e:
            logger.error(f"Failed to initialize GL: {e}")
            self._gl_initialized = False

    def _create_shader_program(self, vs_src: str, fs_src: str) -> int:
        """Create and compile shader program."""
        # Compile vertex shader
        vs = _gl.glCreateShader(GL_VERTEX_SHADER)
        _gl.glShaderSource(vs, vs_src)
        _gl.glCompileShader(vs)

        # Check compile status
        if not _gl.glGetShaderiv(vs, GL_COMPILE_STATUS):
            log = _gl.glGetShaderInfoLog(vs)
            logger.error(f"VS compile error: {log}")
            _gl.glDeleteShader(vs)
            return 0

        # Compile fragment shader
        fs = _gl.glCreateShader(GL_FRAGMENT_SHADER)
        _gl.glShaderSource(fs, fs_src)
        _gl.glCompileShader(fs)

        if not _gl.glGetShaderiv(fs, GL_COMPILE_STATUS):
            log = _gl.glGetShaderInfoLog(fs)
            logger.error(f"FS compile error: {log}")
            _gl.glDeleteShader(vs)
            _gl.glDeleteShader(fs)
            return 0

        # Create program
        prog = _gl.glCreateProgram()
        _gl.glAttachShader(prog, vs)
        _gl.glAttachShader(prog, fs)
        _gl.glLinkProgram(prog)

        if not _gl.glGetProgramiv(prog, GL_LINK_STATUS):
            log = _gl.glGetProgramInfoLog(prog)
            logger.error(f"Program link error: {log}")
            _gl.glDeleteShader(vs)
            _gl.glDeleteShader(fs)
            _gl.glDeleteProgram(prog)
            return 0

        # Cleanup shaders (linked into program)
        _gl.glDeleteShader(vs)
        _gl.glDeleteShader(fs)

        return prog

    def _create_quad_vbo(self) -> None:
        """Create VBO for full-screen quad."""
        # Quad vertices: position (x, y), texcoord (u, v)
        # NDC coordinates: (-1, -1) to (1, 1)
        vertices = np.array([
            # Position   Texcoord
            -1.0, -1.0,   0.0, 1.0,  # Bottom-left (texcoord flipped for OpenGL)
            1.0, -1.0,    1.0, 1.0,   # Bottom-right
            -1.0, 1.0,    0.0, 0.0,   # Top-left
            1.0, 1.0,     1.0, 0.0,    # Top-right
        ], dtype=np.float32)

        # Create VBO
        self._quad_vbo = _gl.glGenBuffers(1)
        _gl.glBindBuffer(GL_ARRAY_BUFFER, self._quad_vbo)
        _gl.glBufferData(GL_ARRAY_BUFFER, vertices, GL_STATIC_DRAW)

        # Create VAO
        self._vao = _gl.glGenVertexArrays(1)
        _gl.glBindVertexArray(self._vao)

        # Get attribute locations
        if self._prog:
            in_vert_loc = _gl.glGetAttribLocation(self._prog, b'in_vert')
            in_texcoord_loc = _gl.glGetAttribLocation(self._prog, b'in_texcoord')

            # Setup attributes
            stride = 4 * 4  # 4 floats per vertex (pos + texcoord)
            _gl.glEnableVertexAttribArray(in_vert_loc)
            _gl.glVertexAttribPointer(
                in_vert_loc, 2, GL_FLOAT, GL_FALSE, stride, None
            )
            _gl.glEnableVertexAttribArray(in_texcoord_loc)
            _gl.glVertexAttribPointer(
                in_texcoord_loc, 2, GL_FLOAT, GL_FALSE, stride, ctypes.c_void_p(2 * 4)
            )

        _gl.glBindVertexArray(0)
        _gl.glBindBuffer(GL_ARRAY_BUFFER, 0)

    def _set_frame_from_qimage(self, qimage: QImage) -> None:
        """
        在 GL 上下文中将 QImage 上传为纹理并显示。

        关键修复（两个独立 bug 合并修）：
        1. BGR 字节序 bug：渲染管线产生的 QImage 是 Format_BGR888（OpenCV
           BGR 字节序）。必须先转成 RGB888 再上传，否则 GL_RGBA 会把 B 当
           R、R 当 B → 红蓝反转。拖拽时间轴走 CPU 路径时表现为"随机插入
           颜色怪异的缓存帧"（正常播放走 GPU blit 颜色正确，两者交替触发）。
        2. alpha 残留 bug：始终用 GL_RGBA + alpha=255 上传，避免与 GPU blit
           路径混用时残留 alpha 导致 blend shader 半透明叠加（"两帧叠在一起"）。

        Args:
            qimage: QImage 实例（Format_RGB888 或 Format_BGR888）
        """
        if not self._gl_initialized:
            logger.warning("GL not initialized, cannot upload QImage")
            return

        if qimage is None or qimage.isNull():
            self.clear_texture()
            return

        self._ack_pending_present_without_display()

        # 统一成 RGB888 字节序，再拼 alpha=255，用 GL_RGBA 上传。
        # 渲染管线产生的 QImage 是 Format_BGR888（OpenCV BGR，为跳过 BGR→RGB
        # 翻转优化）。旧实现把 BGR 字节直接按 GL_RGBA 解读，导致 R/B 互换
        # （红蓝反转），在拖拽时间轴走 CPU 路径时表现为"随机插入怪异缓存帧"。
        # 这里先转成 RGB888，保证 GL 看到的是正确的 R,G,B 字节序。
        fmt = qimage.format()
        if fmt != QImage.Format.Format_RGB888:
            qimage = qimage.convertToFormat(QImage.Format.Format_RGB888)

        width = qimage.width()
        height = qimage.height()

        if width <= 0 or height <= 0:
            logger.warning(f"Invalid QImage dimensions: {width}x{height}")
            return

        # 获取 QImage 像素数据（现在是 RGB 字节序），拼 alpha=255 成 RGBA。
        # AUD-20400: QImage 扫描行按 4 字节对齐（bytesPerLine ≥ width*3），
        # width*3 % 4 != 0 时（如宽 1366/854）不能把缓冲按紧凑行距解读，
        # 必须按行切片拷贝成紧凑缓冲再 reshape，否则整幅画面逐行错位。
        rgba_data = _rgb888_qimage_to_rgba(qimage)
        if rgba_data is None:
            logger.warning("Failed to extract RGB888 pixel data from QImage")
            return

        _gl.glPixelStorei(GL_UNPACK_ALIGNMENT, 1)

        # 始终用 glTexImage2D（不用 SubImage）重新分配 RGBA 纹理，
        # 确保 alpha 通道被正确设为 255（不残留旧 RGBA blit 的 alpha）。
        if self._current_tex_id is None:
            self._current_tex_id = _gl.glGenTextures(1)

        _gl.glBindTexture(GL_TEXTURE_2D, self._current_tex_id)
        _gl.glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR)
        _gl.glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR)
        _gl.glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE)
        _gl.glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE)
        _gl.glTexImage2D(
            GL_TEXTURE_2D,
            0,
            GL_RGBA,  # 内部格式 RGBA
            width,
            height,
            0,
            GL_RGBA,  # 源格式 RGBA（已拼 alpha）
            GL_UNSIGNED_BYTE,
            rgba_data
        )

        _gl.glPixelStorei(GL_UNPACK_ALIGNMENT, 4)

        # 解绑纹理
        _gl.glBindTexture(GL_TEXTURE_2D, 0)

        # 更新纹理尺寸
        self._tex_width = width
        self._tex_height = height
        if width > 0 and height > 0:
            self._video_aspect_ratio = width / height

        # 清除 pending QImage（已上传到 GPU）
        self._pending_qimage = None
        self._shared_tex_id = None
        self._current_tex_is_gl_oriented = False

        # 清除错误状态
        self._error_state = "none"
        self._error_message = ""
        self._offline_media = ()

        # Auto-fit if mode is still "fit"
        if self._view_transform.mode == "fit":
            self._fit_to_window()

    def clear_texture(self) -> None:
        """清除当前纹理和错误状态"""
        self._ack_pending_present_without_display()
        # AUD-20402: clear_texture 可在 GL 回调之外的任意槽里被调用
        # （set_qimage 空图 / set_frame 未知类型 / 面板公开 API）。此时
        # 本 widget 的上下文不保证 current，直接 glDeleteTextures 会在
        # 错误上下文里删名（错删他人纹理/泄漏）或直接抛 GLError。改为
        # deferred-release：排队到下一次 paintGL 开头（上下文必然
        # current）再删除；期间新上传的纹理使用新的名字，不受影响。
        if self._current_tex_id is not None:
            self._pending_tex_delete = self._current_tex_id
        self._current_tex_id = None
        self._shared_tex_id = None
        self._current_tex_is_gl_oriented = False

        self._tex_width = 0
        self._tex_height = 0
        self._pending_qimage = None
        self._offline_media = ()

        # 触发重绘
        self.update()

    def set_error_state(self, state: str, message: str = "") -> None:
        """
        设置错误状态显示。

        Args:
            state: 错误状态 ("none", "no_clip", "render_error", "decode_error")
            message: 错误消息
        """
        self._error_state = state
        self._error_message = message or ""

        # Keep the last good frame visible while a render/decode retry or
        # timeline recompute is in flight. Clearing on transient "no_clip"
        # results exposes the empty GL surface during first-frame warmup, which
        # can look like a blue/blank flash on some drivers. The overlay still
        # communicates the state when there is truly no texture underneath.

        # 触发重绘
        self.update()

    def set_offline_media(self, assets: object) -> None:
        """Show a dedicated warning when one or more source files are gone."""
        if isinstance(assets, (list, tuple)):
            normalized = tuple(
                dict(item) for item in assets if isinstance(item, dict)
            )
        else:
            normalized = ()

        self._offline_media = normalized
        if normalized:
            names = [
                str(item.get("name") or item.get("path") or "")
                for item in normalized
            ]
            names = [name for name in names if name]
            self._error_state = "offline_media"
            self._error_message = ", ".join(names[:3])
            if len(names) > 3:
                self._error_message += f" (+{len(names) - 3})"
        elif self._error_state == "offline_media":
            self._error_state = "none"
            self._error_message = ""
        self.update()

    def set_composite_error(self, message: str, show_ghost: bool = False) -> None:
        """Display composite render error overlay (R-8).

        Unlike set_error_state, this preserves the last good texture so
        a ghosted (dimmed) version can be shown behind the error overlay.

        Args:
            message: Error message to display
            show_ghost: If True, render the existing texture dimmed behind the overlay
        """
        self._error_state = "composite_error"
        self._error_message = message
        self._show_source_ghost = show_ghost

        if not show_ghost:
            # Clear texture only when not showing ghost
            self.clear_texture()

        self.update()

    def resizeGL(self, width: int, height: int) -> None:
        """Handle widget resize (P8-02)."""
        # Only auto-fit if mode is "fit"; manual/actual modes preserve zoom/pan
        if self._view_transform.mode == "fit" and not self._is_fullscreen:
            self._fit_to_window()

        # Emit viewport size change signal for renderer sync
        if width > 0 and height > 0:
            self.viewport_resized.emit(width, height)

    def paintGL(self) -> None:
        """Render the viewer content.

        IMPORTANT: On macOS, QPainter(self) in paintEvent AFTER super().paintEvent()
        overwrites the GL framebuffer content. All QPainter overlays (guides,
        subtitles, error states, handles) MUST be painted INSIDE paintGL with a
        plain QPainter(self) — never wrapped in beginNativePainting()/
        endNativePainting(), which is reserved for raw GL calls only.
        """
        # GUI GL 栈计数（同 initializeGL，见那里注释）。
        from src.features.edit.core.edit_gpu_preview_renderer import (
            enter_gui_gl_stack,
            exit_gui_gl_stack,
        )
        enter_gui_gl_stack()
        try:
            self._paintGL_impl()
        finally:
            exit_gui_gl_stack()

    def _paintGL_impl(self) -> None:
        if not self._gl_initialized:
            return

        # AUD-20402: 执行 clear_texture 排队的 deferred 纹理释放（此处
        # 上下文必然 current）。防御性比对当前纹理名，绝不删除已被
        # 重新分配给当前显示的名字。
        pending_delete = self._pending_tex_delete
        if pending_delete is not None:
            self._pending_tex_delete = None
            if self._current_tex_id != pending_delete:
                try:
                    _gl.glDeleteTextures([pending_delete])
                except Exception as exc:  # noqa: BLE001 — 删除失败只记日志
                    logger.warning(
                        f"ProgramViewer: deferred texture delete failed: {exc}"
                    )

        # Copy shared render-thread textures into a viewer-owned display
        # texture. The GPU renderer recycles its output pool; displaying that
        # pool directly lets later background renders mutate the visible frame.
        if self._shared_tex_id is not None:
            self._copy_shared_texture_to_display()

        # CPU fallback: upload pending QImage to texture.
        pending = self._pending_qimage
        if pending is not None and not pending.isNull():
            self._set_frame_from_qimage(pending)

        # Clear background
        self._reset_copy_gl_state()
        _gl.glViewport(0, 0, max(1, self.width()), max(1, self.height()))
        _gl.glClearColor(0.08, 0.08, 0.08, 1.0)
        _gl.glClear(GL_COLOR_BUFFER_BIT)

        # Get viewport dimensions
        w = self.width()
        h = self.height()

        if w > 0 and h > 0:
            # Calculate display rect preserving aspect ratio
            display_rect = self._calculate_display_rect()

            # Render video texture if available
            if self._current_tex_id is not None and self._prog:
                self._render_texture(display_rect)
                # A0.1：可观测呈现（paint 事件内 _render_texture 之后）。
                # 这是 paint 级观测点，不是物理屏幕扫描时间（无平台
                # vsync 反馈，不得如此解读）；present_ms 由 annotate 用
                # present_submitted_mono 单调时钟差计算。
                _display_trace = 0
                try:
                    _display_trace = int(getattr(self, '_current_display_trace_id', 0) or 0)
                except RuntimeError:
                    pass
                if _display_trace:
                    try:
                        from src.shared.video.playback_frame_trace import (
                            get_trace_collector,
                        )
                        get_trace_collector().annotate(
                            int(_display_trace), present_status="displayed")
                    except Exception:
                        pass
                    self._current_display_trace_id = 0
                self.frame_presented.emit(int(self._current_frame_index))

        # Guides are a screen-space QPainter overlay bound to the same
        # letterbox/zoom/pan rectangle as the displayed frame.
        self._draw_guides_overlay()

        # P10.3: Subtitle overlay — drawn via QPainter on top of GL content.
        # On macOS QPainter(self) after super().paintGL() would clobber the
        # framebuffer, so it is painted here, inside paintGL.
        # AUD-20404: no begin/endNativePainting wrapper — between those calls
        # Qt only permits raw GL; QPainter operations there are undefined
        # (the subtitle path depends on opacity/SmoothPixmapTransform state).
        # Direct QPainter(self) inside paintGL is the established pattern
        # (see _draw_guides_overlay above and the transform handles below).
        show_subtitle_handles = self._selected_subtitle_is_visible()
        if self._subtitle_overlays or show_subtitle_handles:
            painter = QPainter(self)
            try:
                self._paint_subtitle_overlay(painter)
                # 绘制选中字幕的变换手柄
                if show_subtitle_handles:
                    self._subtitle_handle_controller.draw_handles(painter)
            finally:
                painter.end()

        # AUD-20401: error-state feedback overlay ("Media Offline"、render/
        # decode error、R-8 composite ghost)。set_error_state/set_offline_
        # media/set_composite_error 只置状态并 update()；以前没有任何调用
        # 点绘制这些状态，错误反馈 UI 完全失效。此处接通 overlay 绘制。
        if self._error_state != "none":
            painter = QPainter(self)
            try:
                self._paint_error_overlay(painter)
            finally:
                painter.end()

        # Clip Transform tool handles — drawn on top of everything when the
        # tool is active and a clip is selected.  These are QPainter primitives
        # (not raw GL), so keep them outside beginNativePainting: that mode is
        # only for raw OpenGL and bypasses QPainter's antialiasing pipeline.
        if self._transform_tool_active and self._clip_transform_controller.has_clip():
            painter = QPainter(self)
            try:
                self._clip_transform_controller.draw_handles(painter)
            finally:
                painter.end()

    def _calculate_display_rect(self) -> Tuple[float, float, float, float]:
        """
        Calculate display rectangle preserving aspect ratio.

        Returns:
            (x, y, width, height) in NDC coordinates (-1 to 1)
        """
        w = self.width()
        h = self.height()

        if w <= 0 or h <= 0:
            return (-1.0, -1.0, 2.0, 2.0)

        # Use texture dimensions if available, otherwise use aspect ratio
        tex_w = self._tex_width if self._tex_width > 0 else int(self._video_aspect_ratio * h)
        tex_h = self._tex_height if self._tex_height > 0 else h

        return self._view_transform.display_rect_ndc(tex_w, tex_h, w, h)

    def _tex_uniform_location(self) -> int:
        """``u_texture`` 的 UniformLocation（AUD-20405：缓存，避免每帧查询）。

        Location 只依赖 program 对象；program 在 ``_initializeGL_impl`` 重建
        时缓存随之置 None。
        """
        if self._tex_uniform_loc is None:
            self._tex_uniform_loc = _gl.glGetUniformLocation(
                self._prog, b'u_texture')
        return self._tex_uniform_loc

    def _render_texture(self, display_rect: Tuple[float, float, float, float]) -> None:
        """Render video texture to display rectangle."""
        x, y, w, h = display_rect

        # Validate texture ID before use — shared textures may become invalid
        # if the render thread deleted them or shared context creation failed.
        tex_id = self._current_tex_id
        if tex_id is None or tex_id == 0:
            return
        if not _gl.glIsTexture(tex_id):
            logger.warning(f"ProgramViewer: texture ID {tex_id} is invalid, clearing")
            self._current_tex_id = None
            self._shared_tex_id = None
            self._current_tex_is_gl_oriented = False
            return

        # GPU FBO textures have GL orientation (y=0 = bottom of image).
        # CPU-uploaded QImages are flipped by glTexImage2D (y=0 = top of image),
        # so texcoords need to be inverted depending on the source.
        if self._current_tex_is_gl_oriented:
            bot_y, top_y = 0.0, 1.0  # GPU texture: standard GL orientation
        else:
            bot_y, top_y = 1.0, 0.0  # CPU QImage: flipped by glTexImage2D
        playback_trace(
            "program_viewer.paint_texture",
            tex_id=tex_id,
            shared=self._current_tex_is_gl_oriented,
            tex_width=self._tex_width,
            tex_height=self._tex_height,
            rect=tuple(round(v, 4) for v in display_rect),
        )

        # Create custom quad for this display rect
        vertices = np.array([
            # Position   Texcoord
            x, y,         0.0, bot_y,    # Bottom-left
            x + w, y,     1.0, bot_y,    # Bottom-right
            x, y + h,     0.0, top_y,    # Top-left
            x + w, y + h, 1.0, top_y,    # Top-right
        ], dtype=np.float32)

        # R21（第七轮审计）：切源/上下文重建可能已删除旧 program/VBO，
        # 迟到的 paint 仍持旧 id → glUseProgram 报 GL 1282 且该帧纹理
        # 未按 present 协议释放。绘制前校验资源有效性，失效时重建一次
        #（重建仍失败则跳过本帧绘制，不向驱动刷无效调用）。
        if not (
            _gl.glIsProgram(self._prog)
            and _gl.glIsBuffer(self._quad_vbo)
            and _gl.glIsVertexArray(self._vao)
        ):
            logger.warning(
                "ProgramViewer GL resources invalid at paint (source "
                "switch/context rebuild); re-initializing")
            self._tex_uniform_loc = None
            try:
                self._initializeGL_impl()
            except Exception:  # noqa: BLE001 — 重建失败跳过本帧
                return
            if not _gl.glIsProgram(self._prog):
                return

        # Update VBO (use glBufferData like SourceViewer does).
        # AUD-20405: quad 内容每帧都变 → 用 GL_STREAM_DRAW 提示驱动走
        # 流式上传路径（原 GL_STATIC_DRAW 与每帧重传语义相悖）。
        _gl.glBindBuffer(GL_ARRAY_BUFFER, self._quad_vbo)
        _gl.glBufferData(GL_ARRAY_BUFFER, vertices, GL_STREAM_DRAW)

        # Use shader program
        _gl.glUseProgram(self._prog)

        # Bind texture
        _gl.glActiveTexture(GL_TEXTURE0)
        _gl.glBindTexture(GL_TEXTURE_2D, tex_id)

        # Set uniform (AUD-20405: location 缓存，不再每帧 glGetUniformLocation)
        tex_loc = self._tex_uniform_location()
        _gl.glUniform1i(tex_loc, 0)

        # Draw
        _gl.glBindVertexArray(self._vao)
        _gl.glDrawArrays(GL_TRIANGLE_STRIP, 0, 4)

        # Cleanup
        _gl.glBindVertexArray(0)
        _gl.glUseProgram(0)
        _gl.glBindTexture(GL_TEXTURE_2D, 0)

    def _draw_guides_overlay(self) -> None:
        """Draw the active aspect-ratio or safe-area guide over the frame."""
        guide = self._active_guide
        if guide is None or self._tex_width <= 0 or self._tex_height <= 0:
            return

        x, y, width, height = self._view_transform.display_rect_px(
            self._tex_width, self._tex_height, self.width(), self.height())
        image_rect = QRectF(x, y, width, height)
        if image_rect.width() <= 1.0 or image_rect.height() <= 1.0:
            return

        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setClipRect(image_rect)
            params = guide["params"]
            guide_type = guide["type"]
            if guide_type == "aspect_ratio":
                ratio = float(params.get("ratio", 0.0))
                if ratio > 0.0:
                    self._paint_aspect_ratio_guide(
                        painter,
                        image_rect,
                        ratio,
                        bool(params.get("fill_black", False)),
                    )
            elif guide_type == "safe_area":
                self._paint_safe_area_guide(
                    painter,
                    image_rect,
                    str(guide["id"]),
                    float(params.get("inset", 0.0)),
                    float(params.get("secondary", 0.0)),
                )
        finally:
            painter.end()

    def _paint_aspect_ratio_guide(
        self,
        painter: QPainter,
        image_rect: QRectF,
        ratio: float,
        fill_black: bool,
    ) -> None:
        """Draw an outline or opaque black cinema matte for an aspect ratio."""
        if image_rect.width() / image_rect.height() > ratio:
            frame_height = image_rect.height()
            frame_width = frame_height * ratio
        else:
            frame_width = image_rect.width()
            frame_height = frame_width / ratio

        center = image_rect.center()
        frame = QRectF(
            center.x() - frame_width / 2.0,
            center.y() - frame_height / 2.0,
            frame_width,
            frame_height,
        )
        if fill_black:
            self._fill_opaque_matte_bars(painter, image_rect, frame)
        else:
            shade = QColor(0, 0, 0, 80)
            painter.fillRect(
                QRectF(image_rect.left(), image_rect.top(), image_rect.width(),
                       max(0.0, frame.top() - image_rect.top())), shade)
            painter.fillRect(
                QRectF(image_rect.left(), frame.bottom(), image_rect.width(),
                       max(0.0, image_rect.bottom() - frame.bottom())), shade)
            painter.fillRect(
                QRectF(image_rect.left(), frame.top(),
                       max(0.0, frame.left() - image_rect.left()), frame.height()), shade)
            painter.fillRect(
                QRectF(frame.right(), frame.top(),
                       max(0.0, image_rect.right() - frame.right()), frame.height()), shade)

        if not fill_black:
            pen = QPen(QColor(255, 255, 255, 200), 1.0)
            pen.setCosmetic(True)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRect(frame)

    def _fill_opaque_matte_bars(
        self,
        painter: QPainter,
        image_rect: QRectF,
        frame: QRectF,
    ) -> None:
        """Fill cinema bars on whole pixels to prevent fractional-edge leaks.

        ``QRectF`` clipping anti-aliases a fractional display rect, which can
        leave a one-pixel blend of the source frame at the matte boundary.
        Integer-aligned bars intentionally extend one pixel outward, where
        they only overlap the viewer's already-dark letterbox background.
        """
        left = math.floor(image_rect.left()) - 1
        right = math.ceil(image_rect.right()) + 1
        top = math.floor(image_rect.top()) - 1
        bottom = math.ceil(image_rect.bottom()) + 1
        frame_left = math.ceil(frame.left())
        frame_right = math.floor(frame.right())
        frame_top = math.ceil(frame.top())
        frame_bottom = math.ceil(frame.bottom())
        has_top_bar = frame.top() > image_rect.top() + 1e-6
        has_bottom_bar = frame.bottom() < image_rect.bottom() - 1e-6
        has_left_bar = frame.left() > image_rect.left() + 1e-6
        has_right_bar = frame.right() < image_rect.right() - 1e-6

        painter.save()
        painter.setClipping(False)
        black = QColor(0, 0, 0)
        if has_top_bar and frame_top > top:
            painter.fillRect(left, top, right - left, frame_top - top, black)
        if has_bottom_bar and bottom > frame_bottom:
            painter.fillRect(left, frame_bottom, right - left, bottom - frame_bottom, black)
        if has_left_bar and frame_left > left:
            painter.fillRect(left, top, frame_left - left, bottom - top, black)
        if has_right_bar and right > frame_right:
            painter.fillRect(frame_right, top, right - frame_right, bottom - top, black)
        painter.restore()

    def _paint_safe_area_guide(
        self,
        painter: QPainter,
        image_rect: QRectF,
        guide_id: str,
        inset: float,
        secondary: float,
    ) -> None:
        """Draw an extents, action/title, or center safe-area guide."""
        pen = QPen(QColor(255, 255, 255, 180), 1.0)
        pen.setCosmetic(True)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)

        if guide_id == "center":
            center = image_rect.center()
            half_length = secondary * min(image_rect.width(), image_rect.height())
            painter.drawLine(
                QPointF(center.x() - half_length, center.y()),
                QPointF(center.x() + half_length, center.y()),
            )
            painter.drawLine(
                QPointF(center.x(), center.y() - half_length),
                QPointF(center.x(), center.y() + half_length),
            )
            return

        safe_rect = image_rect.adjusted(
            image_rect.width() * inset,
            image_rect.height() * inset,
            -image_rect.width() * inset,
            -image_rect.height() * inset,
        )
        painter.drawRect(safe_rect)

        if guide_id in ("action", "title"):
            tick_length = 8.0
            center = safe_rect.center()
            painter.drawLine(
                QPointF(center.x(), safe_rect.top()),
                QPointF(center.x(), safe_rect.top() + tick_length),
            )
            painter.drawLine(
                QPointF(center.x(), safe_rect.bottom()),
                QPointF(center.x(), safe_rect.bottom() - tick_length),
            )
            painter.drawLine(
                QPointF(safe_rect.left(), center.y()),
                QPointF(safe_rect.left() + tick_length, center.y()),
            )
            painter.drawLine(
                QPointF(safe_rect.right(), center.y()),
                QPointF(safe_rect.right() - tick_length, center.y()),
            )

    # ==================== Public API ====================

    def set_texture(self, tex_id: int, width: int, height: int) -> None:
        """
        Set the texture to display (CPU fallback path).

        Args:
            tex_id: OpenGL texture ID
            width: Texture width
            height: Texture height
        """
        self._ack_pending_present_without_display()
        self._current_tex_id = tex_id
        self._tex_width = width
        self._tex_height = height
        self._shared_tex_id = None  # Clear shared texture
        self._current_tex_is_gl_oriented = False

        if width > 0 and height > 0:
            self._video_aspect_ratio = width / height

        # Auto-fit if mode is still "fit"
        if self._view_transform.mode == "fit":
            self._fit_to_window()

        self.update()

    def set_shared_texture(self, tex_id: int, width: int, height: int) -> None:
        """
        Set a shared GPU texture for display (GPU pipeline path).

        The texture was created in the shared GL context of the render thread.
        No upload needed — just reference the texture ID directly.

        Args:
            tex_id: OpenGL texture ID from shared context
            width: Texture width
            height: Texture height
        """
        if tex_id is None or tex_id == 0:
            logger.warning(f"ProgramViewer: rejecting invalid shared texture ID {tex_id}")
            playback_trace("program_viewer.reject_shared_texture", tex_id=tex_id, width=width, height=height)
            return
        self._ack_pending_present_without_display()
        # Queue for paintGL(), where this QOpenGLWidget context is current.
        # paintGL copies the shared render-thread texture into a viewer-owned
        # texture, so later renderer pool reuse cannot mutate the visible image.
        self._shared_tex_id = tex_id
        self._shared_tex_width = width
        self._shared_tex_height = height
        self._pending_qimage = None  # Clear CPU path
        self._error_state = "none"
        self._error_message = ""
        self._offline_media = ()
        playback_trace(
            "program_viewer.set_shared_texture",
            tex_id=tex_id,
            width=width,
            height=height,
            previous_tex_id=self._current_tex_id,
        )

        if width > 0 and height > 0:
            self._video_aspect_ratio = width / height

        # Auto-fit if mode is still "fit"
        if self._view_transform.mode == "fit":
            self._fit_to_window()

        self.update()

    def set_present_frame(
        self, present_frame, ack_cb: Optional[callable] = None,
        trace_id: int = 0,
    ) -> None:
        """
        Set a GpuPresentFrame for display (Batch 1 present protocol).

        携带 fence + slot_id，让 paintGL 在 blit 前 wait fence、blit 后 ack slot。
        替代裸 set_shared_texture，根治跨 context 半写纹理导致的混帧。

        Args:
            present_frame: GpuPresentFrame descriptor
            ack_cb: blit 完成后调用的 ack 回调（释放 renderer slot）。
                若为 None 且 present_frame 携带 slot_id>=0，则使用注入的
                ack handler。
            trace_id: A0.1 发起帧 trace id（TPR result dict 携带）；
                0 = 旁路帧（呈现状态 annotate 会被明确拒收并计数）。
        """
        if present_frame is None:
            return
        tex_id = present_frame.texture_id
        if tex_id is None or tex_id == 0:
            logger.warning(f"ProgramViewer: rejecting present frame tex_id={tex_id}")
            return

        self._ack_pending_present_without_display(status="superseded")

        # Queue present frame + ack for paintGL
        self._pending_present_frame = present_frame
        self._pending_present_ack = ack_cb
        self._pending_present_trace_id = int(trace_id or 0)
        # 复用 set_shared_texture 的 texture 引用逻辑
        self._shared_tex_id = tex_id
        self._shared_tex_width = present_frame.width
        self._shared_tex_height = present_frame.height
        self._pending_qimage = None
        self._error_state = "none"
        self._error_message = ""
        self._offline_media = ()
        playback_trace(
            "program_viewer.set_present_frame",
            tex_id=tex_id,
            width=present_frame.width,
            height=present_frame.height,
            slot_id=present_frame.slot_id,
            has_fence=present_frame.has_fence,
            frame=present_frame.frame,
        )

        if present_frame.width > 0 and present_frame.height > 0:
            self._video_aspect_ratio = present_frame.width / present_frame.height
        if self._view_transform.mode == "fit":
            self._fit_to_window()
        self.update()

    def set_fence_wait_function(self, wait_fn: Optional[callable]) -> None:
        """注入 fence wait 函数（生产用 glWaitSync，测试用 mock）。"""
        self._gl_fence_wait_fn = wait_fn

    def _ack_pending_present_without_display(self, status: str = "superseded") -> None:
        """Ack a queued present frame when it leaves the pending slot.

        A0.1：``status`` 区分两种语义——
        - 'superseded'：pending 帧被更新的帧顶掉（未 paint 即回收）；
        - 'viewer_copy'：paintGL 已把 present slot blit 进 viewer 自有纹理
          （blit 命令入队后的 finally 路径），随后仍会正常上屏。
        状态按 pending trace_id annotate 回发起帧 trace。
        """
        present = self._pending_present_frame
        ack_cb = self._pending_present_ack
        try:
            trace_id = int(getattr(self, '_pending_present_trace_id', 0) or 0)
        except RuntimeError:
            # __new__ 构造的未初始化实例（单测）：无属性可读，跳过 annotate。
            trace_id = 0
        if present is not None and ack_cb is not None:
            if trace_id and status in ("superseded", "viewer_copy"):
                try:
                    from src.shared.video.playback_frame_trace import (
                        get_trace_collector,
                    )
                    get_trace_collector().annotate(
                        int(trace_id), present_status=status,
                        present_slot_id=int(getattr(present, 'slot_id', -1)))
                except Exception:
                    pass
            try:
                ack_cb(getattr(present, 'slot_id', -1))
            except Exception as e:
                logger.debug(f"pending present ack failed: {e}")
        if status == "viewer_copy" and trace_id:
            # viewer_copy 之后紧接 _render_texture 上屏：记住本帧身份，
            # paintGL 末尾的 frame_presented 处 annotate displayed。
            self._current_display_trace_id = int(trace_id)
        self._pending_present_frame = None
        self._pending_present_ack = None
        self._pending_present_trace_id = 0

    def _reset_copy_gl_state(self) -> None:
        """Reset GL state that can make clear/blit update only part of a texture."""
        try:
            _gl.glDisable(GL_SCISSOR_TEST)
            _gl.glDisable(GL_DEPTH_TEST)
            _gl.glDisable(GL_BLEND)
            _gl.glColorMask(GL_TRUE, GL_TRUE, GL_TRUE, GL_TRUE)
        except Exception:
            pass

    def _copy_shared_texture_to_display(self) -> None:
        """Copy the pending shared GPU texture into this viewer's own texture.

        Batch 1 (缺陷计划 RP-P0-01): 若当前 present frame 携带 fence，在 blit
        前 glWaitSync 等待 renderer 写完；blit 后触发 ack 释放 renderer slot。
        """
        source_tex = self._shared_tex_id
        width = self._shared_tex_width
        height = self._shared_tex_height
        if source_tex is None or source_tex == 0 or width <= 0 or height <= 0:
            # AUD-20403: 无效尺寸早退也要释放 renderer slot（恰好一次）。
            self._ack_pending_present_without_display()
            return

        # Batch 1: present frame fence wait（在 blit 前确保 renderer 写完）。
        # 这替代了 renderer 端的 glFinish()——把同步点移到 Viewer 自己的 context，
        # 不阻塞 renderer 的 GPU pipeline。
        present = self._pending_present_frame
        if present is not None and getattr(present, 'has_fence', False) and present.fence_ns != 0:
            wait_fn = self._gl_fence_wait_fn
            if wait_fn is not None:
                try:
                    wait_fn(present.fence_ns)
                except Exception as e:
                    logger.debug(f"present fence wait failed: {e}")
            else:
                # 默认实现：glWaitSync（非阻塞，等 fence signaled）
                try:
                    from OpenGL.GL import (
                        glWaitSync, GL_SYNC_FLUSH_COMMANDS_COMPLETE,
                        GL_TIMEOUT_IGNORED,
                    )
                    glWaitSync(present.fence_ns, GL_SYNC_FLUSH_COMMANDS_COMPLETE, GL_TIMEOUT_IGNORED)
                except Exception as e:
                    logger.debug(f"glWaitSync failed: {e}")

        if not _gl.glIsTexture(source_tex):
            playback_trace(
                "program_viewer.copy_shared_invalid",
                tex_id=source_tex,
                width=width,
                height=height,
            )
            self._shared_tex_id = None
            # AUD-20403: 源纹理无效早退时同样 ack——否则 renderer slot 悬挂，
            # 只能等下一次 set_present_frame 补偿；若这是暂停/停止前最后
            # 一帧，slot 长期占用甚至耗尽输出池。
            self._ack_pending_present_without_display()
            return

        if self._current_tex_id is None:
            self._current_tex_id = _gl.glGenTextures(1)

        _gl.glBindTexture(GL_TEXTURE_2D, self._current_tex_id)
        _gl.glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR)
        _gl.glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR)
        _gl.glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE)
        _gl.glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE)

        target_is_gl_oriented = bool(
            getattr(present, 'is_gl_oriented', True) if present is not None else True
        )
        if (
            self._tex_width != width
            or self._tex_height != height
            or self._current_tex_is_gl_oriented != target_is_gl_oriented
        ):
            _gl.glTexImage2D(
                GL_TEXTURE_2D,
                0,
                GL_RGBA,
                width,
                height,
                0,
                GL_RGBA,
                GL_UNSIGNED_BYTE,
                None,
            )

        if self._copy_read_fbo is None:
            self._copy_read_fbo = _gl.glGenFramebuffers(1)
        if self._copy_draw_fbo is None:
            self._copy_draw_fbo = _gl.glGenFramebuffers(1)

        try:
            self._reset_copy_gl_state()
            _gl.glBindFramebuffer(GL_READ_FRAMEBUFFER, self._copy_read_fbo)
            _gl.glFramebufferTexture2D(
                GL_READ_FRAMEBUFFER,
                GL_COLOR_ATTACHMENT0,
                GL_TEXTURE_2D,
                source_tex,
                0,
            )
            _gl.glBindFramebuffer(GL_DRAW_FRAMEBUFFER, self._copy_draw_fbo)
            _gl.glFramebufferTexture2D(
                GL_DRAW_FRAMEBUFFER,
                GL_COLOR_ATTACHMENT0,
                GL_TEXTURE_2D,
                self._current_tex_id,
                0,
            )
            _gl.glClearColor(0.0, 0.0, 0.0, 0.0)
            _gl.glClear(GL_COLOR_BUFFER_BIT)
            _gl.glBlitFramebuffer(
                0,
                0,
                width,
                height,
                0,
                0,
                width,
                height,
                GL_COLOR_BUFFER_BIT,
                GL_NEAREST,
            )
        finally:
            _gl.glBindFramebuffer(GL_FRAMEBUFFER, self.defaultFramebufferObject())
            _gl.glBindTexture(GL_TEXTURE_2D, 0)
            # AUD-20403: blit 抛异常的路径也要恰好一次地 ack renderer slot
            # （_ack_pending_present_without_display 内部先触发回调再清空，
            # 下方成功路径的重复调用因引用已清空而自然成为 no-op）。
            # A0.1：此处的语义是 viewer_copy（blit 命令已入队 + fence 已
            # 等待），不是可观测显示——displayed 在 paintGL 末尾
            # frame_presented 处 annotate。
            self._ack_pending_present_without_display(status="viewer_copy")

        self._tex_width = width
        self._tex_height = height
        self._current_tex_is_gl_oriented = target_is_gl_oriented
        self._shared_tex_id = None

        playback_trace(
            "program_viewer.copy_shared_texture",
            source_tex_id=source_tex,
            display_tex_id=self._current_tex_id,
            width=width,
            height=height,
        )
        if self._monitor_readback_enabled:
            self._submit_monitor_pbo_readback(
                frame_index=int(getattr(present, 'frame', self._current_frame_index) or 0),
                width=width,
                height=height,
                is_gl_oriented=target_is_gl_oriented,
            )
        if self._monitor_gpu_signal_enabled and self._current_tex_id is not None:
            self.monitor_gpu_frame_ready.emit(
                int(getattr(present, 'frame', self._current_frame_index) or 0),
                int(self._current_tex_id),
                int(width),
                int(height),
                bool(target_is_gl_oriented),
            )

        # Batch 1: renderer slot 的 ack 已在上方 blit 的 finally 里统一触发
        # （AUD-20403：成功/异常/早退路径均恰好一次）。
        if present is not None:
            self._pending_present_frame = None
            self._pending_present_ack = None

    def set_monitor_readback_enabled(self, enabled: bool) -> None:
        """Enable GPU texture readback for external monitoring output."""
        self._monitor_readback_enabled = bool(enabled)
        if not enabled:
            self._monitor_readback_pending = None

    def set_monitor_gpu_signal_enabled(self, enabled: bool) -> None:
        """Enable zero-copy-ready GPU texture signaling for native monitor output."""
        self._monitor_gpu_signal_enabled = bool(enabled)

    def _ensure_monitor_pbos(self, width: int, height: int) -> bool:
        width = int(width)
        height = int(height)
        if width <= 0 or height <= 0:
            return False
        if self._monitor_readback_pbos and self._monitor_readback_size == (width, height):
            return True

        if self._monitor_readback_pbos:
            try:
                _gl.glDeleteBuffers(self._monitor_readback_pbos)
            except Exception:
                pass
        try:
            pbos = _gl.glGenBuffers(2)
            if isinstance(pbos, int):
                pbos = [pbos]
            self._monitor_readback_pbos = list(pbos)
            if len(self._monitor_readback_pbos) < 2:
                self._monitor_readback_pbos = []
                return False
            byte_count = width * height * 4
            for pbo in self._monitor_readback_pbos:
                _gl.glBindBuffer(GL_PIXEL_PACK_BUFFER, pbo)
                _gl.glBufferData(GL_PIXEL_PACK_BUFFER, byte_count, None, GL_STREAM_READ)
            _gl.glBindBuffer(GL_PIXEL_PACK_BUFFER, 0)
        except Exception as exc:
            logger.debug("ProgramViewer monitor PBO allocation failed: %s", exc)
            self._monitor_readback_pbos = []
            return False

        self._monitor_readback_size = (width, height)
        self._monitor_readback_index = 0
        self._monitor_readback_pending = None
        return True

    def _note_monitor_readback_bytes(self, nbytes: int, frame_index: int) -> None:
        """A0.1：监看回读按 READBACK_KINDS='monitor' 归因到当前显示帧。

        PBO 取回与直读兜底都走这里；字节同时进全局 pixel_copy 审计
        （此前监看回读只写 GL，不进 CPU_DOWNLOAD 计数，审计漏记）。
        """
        try:
            from src.shared.video.pixel_copy_counters import (
                CopyKind,
                add_pixel_copy,
            )
            add_pixel_copy(CopyKind.CPU_DOWNLOAD, int(nbytes))
        except Exception:
            pass
        try:
            trace_id = int(getattr(self, '_current_display_trace_id', 0) or 0)
        except RuntimeError:
            trace_id = 0
        if not trace_id:
            return
        try:
            from src.shared.video.playback_frame_trace import get_trace_collector
            get_trace_collector().annotate(
                int(trace_id),
                cpu_download_bytes=int(nbytes),
                readback_kind="monitor",
                extra={"monitor_readback_frame": int(frame_index)},
            )
        except Exception:
            pass

    def _submit_monitor_pbo_readback(
        self,
        *,
        frame_index: int,
        width: int,
        height: int,
        is_gl_oriented: bool,
    ) -> None:
        """Issue async PBO readback of the viewer-owned display texture."""
        if self._current_tex_id is None or not self._ensure_monitor_pbos(width, height):
            return

        pbos = self._monitor_readback_pbos
        read_index = self._monitor_readback_index
        map_index = 1 - read_index

        try:
            pending = self._monitor_readback_pending
            if pending is not None and len(pbos) > map_index:
                pending_frame, pending_w, pending_h, pending_oriented = pending
                _gl.glBindBuffer(GL_PIXEL_PACK_BUFFER, pbos[map_index])
                pending_byte_count = int(pending_w) * int(pending_h) * 4
                raw = _gl.glGetBufferSubData(GL_PIXEL_PACK_BUFFER, 0, pending_byte_count)
                if raw is not None:
                    self._note_monitor_readback_bytes(
                        pending_byte_count, pending_frame)
                    image = self._pbo_rgba_to_qimage(
                        raw,
                        pending_w,
                        pending_h,
                        pending_oriented,
                    )
                    if image is not None:
                        self._monitor_readback_last_emit_ts = time.monotonic()
                        self._decklink_monitor_debug_log(
                            "program_viewer_pbo_emit",
                            frame=pending_frame,
                            input=f"{pending_w}x{pending_h}",
                            throttle=1.0,
                        )
                        self.monitor_frame_ready.emit(int(pending_frame), image)

            self._reset_copy_gl_state()
            _gl.glBindFramebuffer(GL_READ_FRAMEBUFFER, self._copy_draw_fbo)
            _gl.glFramebufferTexture2D(
                GL_READ_FRAMEBUFFER,
                GL_COLOR_ATTACHMENT0,
                GL_TEXTURE_2D,
                self._current_tex_id,
                0,
            )
            _gl.glBindBuffer(GL_PIXEL_PACK_BUFFER, pbos[read_index])
            _gl.glReadPixels(
                0,
                0,
                int(width),
                int(height),
                GL_RGBA,
                GL_UNSIGNED_BYTE,
                ctypes.c_void_p(0),
            )
            self._monitor_readback_pending = (
                int(frame_index),
                int(width),
                int(height),
                bool(is_gl_oriented),
            )
            self._monitor_readback_index = map_index
            self._emit_direct_monitor_readback_if_needed(
                frame_index=frame_index,
                width=width,
                height=height,
                is_gl_oriented=is_gl_oriented,
            )
        except Exception as exc:
            self._decklink_monitor_debug_log(
                "program_viewer_pbo_failed",
                frame=frame_index,
                input=f"{width}x{height}",
                error=exc,
                throttle=1.0,
            )
            logger.debug("ProgramViewer monitor PBO readback failed: %s", exc)
        finally:
            try:
                _gl.glBindBuffer(GL_PIXEL_PACK_BUFFER, 0)
                _gl.glBindFramebuffer(GL_FRAMEBUFFER, self.defaultFramebufferObject())
            except Exception:
                pass

    def _emit_direct_monitor_readback_if_needed(
        self,
        *,
        frame_index: int,
        width: int,
        height: int,
        is_gl_oriented: bool,
    ) -> None:
        """Fallback readback so DeckLink sees edit frames even if PBO stalls."""
        now = time.monotonic()
        # The external monitor is fixed at 25fps for the current DeckLink path.
        if now - self._monitor_readback_last_emit_ts < (1.0 / 25.0):
            return
        try:
            _gl.glBindBuffer(GL_PIXEL_PACK_BUFFER, 0)
            raw = _gl.glReadPixels(
                0,
                0,
                int(width),
                int(height),
                GL_RGBA,
                GL_UNSIGNED_BYTE,
                outputType=bytes,
            )
            self._note_monitor_readback_bytes(
                int(width) * int(height) * 4, frame_index)
            image = self._pbo_rgba_to_qimage(
                raw,
                int(width),
                int(height),
                bool(is_gl_oriented),
            )
            if image is None:
                return
            self._monitor_readback_last_emit_ts = now
            self._decklink_monitor_debug_log(
                "program_viewer_direct_emit",
                frame=frame_index,
                input=f"{width}x{height}",
                throttle=1.0,
            )
            self.monitor_frame_ready.emit(int(frame_index), image)
        except Exception as exc:
            self._decklink_monitor_debug_log(
                "program_viewer_direct_failed",
                frame=frame_index,
                input=f"{width}x{height}",
                error=exc,
                throttle=1.0,
            )

    @staticmethod
    def _pbo_rgba_to_qimage(
        raw: object,
        width: int,
        height: int,
        is_gl_oriented: bool,
    ) -> Optional[QImage]:
        """Convert RGBA PBO bytes to an owned RGB888 QImage.

        A1.2（H2 实测监看链 85ms/帧@4K，其中本函数 NumPy 路径 53ms）：
        主路径改用 Qt C++——RGBX8888 直接包装 raw（R,G,B,X 字节序与小端
        RGBA 一致）→ mirrored 垂直翻转（Qt C++ SIMD，兼深拷贝取得所有权）
        → convertToFormat(RGB888)。输出与旧 NumPy 路径逐字节一致
        （tests/edit/test_frame_budget_wiring.py 对拍锁定）；Qt 路径异常时
        回退旧实现，行为不变。
        """
        try:
            if not isinstance(raw, (bytes, bytearray, memoryview)):
                raw = bytes(raw)
            # W07 修复：Qt 快路径此前不校验缓冲长度——短缓冲直接包装 =
            # 越界读（平台相关 UB，Windows 上不抛异常而是"成功"返回
            # 垃圾图）。先按 bytesPerLine*height 验长，不足走 None 契约
            # （NumPy 回退路径的 reshape 校验同语义）。
            if len(raw) < int(width) * int(height) * 4:
                return None
            image = QImage(
                raw, int(width), int(height), int(width) * 4,
                QImage.Format.Format_RGBX8888,
            )
            if is_gl_oriented:
                # GL 朝向（y=0 在底）→ 垂直翻转；mirrored 返回深拷贝，
                # 此后不引用 raw（QImage 构造不拷贝输入缓冲）。
                image = image.mirrored(False, True)
            else:
                image = image.copy()
            return image.convertToFormat(QImage.Format.Format_RGB888)
        except Exception:
            logger.debug("monitor readback Qt fast path failed; "
                         "falling back to NumPy", exc_info=True)
        try:
            data = np.frombuffer(raw, dtype=np.uint8).reshape((height, width, 4))
        except Exception:
            return None
        if is_gl_oriented:
            data = np.flipud(data)
        rgb = np.ascontiguousarray(data[:, :, :3])
        image = QImage(
            rgb.data,
            int(width),
            int(height),
            int(width) * 3,
            QImage.Format.Format_RGB888,
        )
        return image.copy()

    def _decklink_monitor_debug_log(
        self,
        event: str,
        *,
        throttle: float = 1.0,
        **fields,
    ) -> None:
        now = time.monotonic()
        last = float(self._monitor_readback_debug_last.get(event, 0.0) or 0.0)
        if now - last < throttle:
            return
        self._monitor_readback_debug_last[event] = now
        detail = " ".join(f"{key}={value}" for key, value in fields.items())
        logger.debug("[DECKLINK_MONITOR_DEBUG] event=%s %s", event, detail)

    def set_timecode(self, timecode: str, fps: float = 24.0) -> None:
        """
        Set the timecode overlay text.

        Args:
            timecode: Timecode string (HH:MM:SS:FF)
            fps: Frame rate
        """
        self._timecode = timecode
        self._fps = fps
        self.update()

    def set_clip_info(self, clip_id: str, frame_index: int) -> None:
        """
        Store current clip metadata for overlays/debug UI only.

        Args:
            clip_id: Clip ID
            frame_index: Current frame index
        """
        self._current_clip_id = clip_id
        self._current_frame_index = frame_index

    def set_guide(
        self,
        guide_type: str,
        guide_id: str,
        params: Dict[str, Any],
    ) -> None:
        """Set or clear the Program monitor's Guides overlay."""
        if guide_id == "default":
            self._active_guide = None
        else:
            self._active_guide = {
                "type": guide_type,
                "id": guide_id,
                "params": dict(params),
            }
        self.update()

    def set_zoom(self, zoom_level: float) -> None:
        """
        Set zoom level.

        Args:
            zoom_level: Zoom multiplier (1.0 = fit-to-window)
        """
        self._view_transform.zoom = max(self._view_transform.min_zoom, min(self._view_transform.max_zoom, zoom_level))
        self._view_transform.mode = "manual"
        self.zoom_changed.emit(self._view_transform.zoom)
        self.update()

    def set_pan(self, pan_x: float, pan_y: float) -> None:
        """
        Set pan offset (stored as pixel offset internally).

        Args:
            pan_x: X offset (in NDC-like units, converted to px)
            pan_y: Y offset (in NDC-like units, converted to px)
        """
        w = self.width()
        h = self.height()
        self._view_transform.pan_x_px = pan_x * w / 2.0
        self._view_transform.pan_y_px = -pan_y * h / 2.0
        self._view_transform.mode = "manual"
        self.update()

    def _fit_to_window(self) -> None:
        """Fit video to window (reset zoom and pan)."""
        self._view_transform.reset_fit()
        self.zoom_changed.emit(self._view_transform.zoom)
        self.update()

    def _actual_size(self) -> None:
        """Set to actual pixel size (1:1)."""
        w = self.width()
        h = self.height()

        tex_w = self._tex_width if self._tex_width > 0 else int(self._video_aspect_ratio * h)
        tex_h = self._tex_height if self._tex_height > 0 else h

        self._view_transform.set_actual_size(tex_w, tex_h, w, h)
        self.zoom_changed.emit(self._view_transform.zoom)
        self.update()

    def zoom_in(self) -> None:
        """Zoom in by factor."""
        self.set_zoom(self._view_transform.zoom * 1.2)

    def zoom_out(self) -> None:
        """Zoom out by factor."""
        self.set_zoom(self._view_transform.zoom / 1.2)

    # ==================== Fullscreen ====================

    def enter_fullscreen(self) -> None:
        """Enter fullscreen mode."""
        if self._is_fullscreen:
            return

        self._is_fullscreen = True

        # P8: Save original parent and geometry
        self._original_parent = self.parent()
        self._saved_geometry = QRectF(self.geometry())

        # Create fullscreen window
        self._fullscreen_window = QWidget()
        self._fullscreen_window.setObjectName("ProgramViewerFullscreen")
        self._fullscreen_window.setStyleSheet("background-color: #1a1a1a;")
        self._fullscreen_window.setWindowFlags(
            Qt.WindowType.Window |
            Qt.WindowType.FramelessWindowHint
        )
        self._fullscreen_window.setWindowState(Qt.WindowState.WindowFullScreen)

        # Move this viewer to fullscreen window
        self.setParent(self._fullscreen_window)

        # Layout
        layout = QVBoxLayout(self._fullscreen_window)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self)

        # Show fullscreen window
        self._fullscreen_window.showFullScreen()
        self._fullscreen_window.activateWindow()

        # Focus on this viewer
        self.setFocus()

        # Emit signal
        self.fullscreen_changed.emit(True)

        logger.info("ProgramViewer entered fullscreen")

    def exit_fullscreen(self) -> None:
        """Exit fullscreen mode (P8-03 fix)."""
        if not self._is_fullscreen:
            return

        self._is_fullscreen = False

        # P8-03: Restore original parent with proper layout handling
        if self._original_parent:
            # Close fullscreen window first (to avoid parenting issues)
            if self._fullscreen_window:
                self._fullscreen_window.close()
                self._fullscreen_window.deleteLater()
                self._fullscreen_window = None

            # Restore parent
            self.setParent(self._original_parent)

            # Ensure widget is in the parent's layout
            layout = self._original_parent.layout()
            if layout:
                # Check if widget is already in layout
                for i in range(layout.count()):
                    if layout.itemAt(i) and layout.itemAt(i).widget() == self:
                        # Already in layout, skip
                        break
                else:
                    # Widget not in layout, add it
                    layout.addWidget(self)

            # Restore geometry
            if self._saved_geometry:
                self.setGeometry(int(self._saved_geometry.x()),
                                int(self._saved_geometry.y()),
                                int(self._saved_geometry.width()),
                                int(self._saved_geometry.height()))
        else:
            logger.warning("ProgramViewer: No saved parent, cannot restore from fullscreen")
            # Close fullscreen window anyway
            if self._fullscreen_window:
                self._fullscreen_window.close()
                self._fullscreen_window.deleteLater()
                self._fullscreen_window = None

        # Clear saved state
        self._original_parent = None
        self._saved_geometry = None

        # Emit signal
        self.fullscreen_changed.emit(False)

        logger.info("ProgramViewer exited fullscreen")

    def toggle_fullscreen(self) -> None:
        """Toggle fullscreen mode."""
        if self._is_fullscreen:
            self.exit_fullscreen()
        else:
            self.enter_fullscreen()

    # ==================== Mouse Events ====================

    def mousePressEvent(self, event: QMouseEvent) -> None:
        """Handle mouse press."""
        # Space is routed by MainWindow according to the focused playback
        # surface, so clicking the Program monitor must make it active.
        self.setFocus(Qt.FocusReason.MouseFocusReason)
        if event.button() == Qt.MouseButton.MiddleButton:
            # Start pan
            self._is_panning = True
            self._pan_start_pos = event.pos()
            self._pan_start_px = (self._view_transform.pan_x_px, self._view_transform.pan_y_px)
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            event.accept()
        elif event.button() == Qt.MouseButton.LeftButton:
            pos = event.position()
            # Compute modifiers lazily per branch (PyQt6 strict enums reject
            # int(); only the handle controllers need it).
            # Clip Transform tool takes priority when active — it's an
            # explicitly engaged editing mode, so it wins over the subtitle
            # handles and the default viewer behaviour.
            if self._transform_tool_active:
                tctrl = self._clip_transform_controller
                if tctrl.has_clip():
                    handle_id = tctrl.hit_test(pos)
                    if handle_id is not None or tctrl.hit_test_body(pos):
                        modifiers = _modifiers_int(event.modifiers())
                        if tctrl.start_drag(handle_id, pos, modifiers):
                            event.accept()
                            return
                # Tool active but no handle hit → still consume so the viewer
                # doesn't pan/zoom underneath an active editing session.
                event.accept()
                return
            # P10.3: 字幕手柄优先命中测试
            ctrl = self._subtitle_handle_controller
            if ctrl.has_clip() and self._selected_subtitle_is_visible():
                handle_id = ctrl.hit_test(pos)
                # 命中手柄或字幕 body
                if handle_id is not None or ctrl.hit_test_body(pos):
                    modifiers = _modifiers_int(event.modifiers())
                    if ctrl.start_drag(handle_id, pos, modifiers):
                        event.accept()
                        return
            super().mousePressEvent(event)
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        """Handle mouse move."""
        if self._is_panning:
            # Calculate pan delta in pixels
            dx = event.pos().x() - self._pan_start_pos.x()
            dy = event.pos().y() - self._pan_start_pos.y()

            # Apply pixel-based pan (direct 1:1 mapping)
            self._view_transform.pan_x_px = self._pan_start_px[0] + dx
            self._view_transform.pan_y_px = self._pan_start_px[1] + dy
            self._view_transform.mode = "manual"

            self.update()
            event.accept()
        else:
            pos = event.position()
            # NOTE: don't call int(event.modifiers()) here unconditionally —
            # PyQt6's strict KeyboardModifier enum rejects int() and crashes
            # every mouseMove (only the handle controllers need it, computed
            # lazily inside each branch via _modifiers_int()).
            # Clip Transform tool drag / hover.
            if self._transform_tool_active:
                tctrl = self._clip_transform_controller
                if tctrl.has_clip():
                    modifiers = _modifiers_int(event.modifiers())
                    if tctrl._active_handle is not None:
                        tctrl.update_drag(pos, modifiers)
                        event.accept()
                        return
                    else:
                        tctrl.update_hover(pos)
                event.accept()
                return
            # P10.3: 字幕手柄 drag 或 hover
            ctrl = self._subtitle_handle_controller
            if ctrl.has_clip() and self._selected_subtitle_is_visible():
                modifiers = _modifiers_int(event.modifiers())
                if ctrl._active_handle is not None:
                    ctrl.update_drag(pos, modifiers)
                    event.accept()
                    return
                else:
                    ctrl.update_hover(pos)
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        """Handle mouse release."""
        if event.button() == Qt.MouseButton.MiddleButton:
            self._is_panning = False
            self.setCursor(Qt.CursorShape.ArrowCursor)
            event.accept()
        elif event.button() == Qt.MouseButton.LeftButton:
            # Clip Transform tool drag end.
            if self._transform_tool_active:
                tctrl = self._clip_transform_controller
                if tctrl._active_handle is not None:
                    tctrl.end_drag()
                    event.accept()
                    return
                event.accept()
                return
            # P10.3: 字幕手柄 drag 结束
            ctrl = self._subtitle_handle_controller
            if ctrl._active_handle is not None:
                ctrl.end_drag()
                event.accept()
                return
            super().mouseReleaseEvent(event)
        else:
            super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        """Handle double-click for fullscreen toggle."""
        self.double_clicked.emit()
        event.accept()

    def wheelEvent(self, event: QWheelEvent) -> None:
        """Handle mouse wheel for zoom (no Ctrl required)."""
        w = self.width()
        h = self.height()
        tex_w = self._tex_width if self._tex_width > 0 else int(self._video_aspect_ratio * h)
        tex_h = self._tex_height if self._tex_height > 0 else h

        if w <= 0 or h <= 0 or tex_w <= 0 or tex_h <= 0:
            super().wheelEvent(event)
            return

        # Determine anchor point (mouse position in widget coords)
        anchor = event.position()
        anchor_x = anchor.x()
        anchor_y = anchor.y()

        # Distinguish trackpad (pixelDelta) from mouse wheel (angleDelta)
        pixel_delta = event.pixelDelta().y()
        angle_delta = event.angleDelta().y()

        if pixel_delta != 0:
            # Trackpad: smooth zoom
            factor = math.pow(1.01, pixel_delta)
        elif angle_delta != 0:
            # Mouse wheel: stepped zoom
            steps = angle_delta / 120.0
            factor = math.pow(1.12, steps)
        else:
            super().wheelEvent(event)
            return

        self._view_transform.zoom_at(factor, anchor_x, anchor_y, w, h, tex_w, tex_h)
        self.zoom_changed.emit(self._view_transform.zoom)
        self.update()
        event.accept()

    def keyPressEvent(self, event: QKeyEvent) -> None:
        """Handle keyboard shortcuts."""
        key = event.key()

        if key == Qt.Key.Key_F:
            self._fit_to_window()
        elif key == Qt.Key.Key_1:
            self._actual_size()
        elif key == Qt.Key.Key_Escape:
            if self._is_fullscreen:
                self.exit_fullscreen()
        elif key == Qt.Key.Key_F11:
            self.toggle_fullscreen()
        elif key == Qt.Key.Key_Plus or key == Qt.Key.Key_Equal:
            self.zoom_in()
        elif key == Qt.Key.Key_Minus:
            self.zoom_out()
        else:
            super().keyPressEvent(event)

    # ==================== Paint Overlay ====================

    def paintEvent(self, event) -> None:
        """Paint OpenGL content.

        NOTE: All QPainter overlays are painted INSIDE paintGL() (guides /
        subtitles / error states / handles) with a plain QPainter. Do NOT
        create QPainter(self) here after super().paintEvent() — on macOS this
        overwrites the GL framebuffer.
        """
        super().paintEvent(event)

    def _paint_error_overlay(self, painter: QPainter) -> None:
        """绘制错误状态 overlay (called from paintGL with active QPainter)"""
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        w = self.width()
        h = self.height()

        # 绘制半透明背景
        painter.fillRect(self.rect(), QColor(30, 30, 30, 200))

        # 根据错误状态绘制不同内容
        if self._error_state == "no_clip":
            # 显示"无片段"提示
            font = QFont("Arial", 16)
            painter.setFont(font)
            painter.setPen(QColor(180, 180, 180))
            text = I18N.tr("program_viewer.no_clip", "No clips at this frame")
            text_rect = painter.fontMetrics().boundingRect(text)
            x = (w - text_rect.width()) / 2
            y = (h - text_rect.height()) / 2
            painter.drawText(int(x), int(y + text_rect.height()), text)

        elif self._error_state == "offline_media":
            # Match the familiar NLE treatment: keep the last frame dimmed
            # underneath a clear red warning instead of reporting a generic
            # renderer failure.
            painter.fillRect(self.rect(), QColor(72, 0, 0, 225))

            center_x = w // 2
            triangle_width = max(52, min(112, int(min(w, h) * 0.16)))
            triangle_height = max(46, int(triangle_width * 0.86))
            triangle_top = max(24, h // 2 - triangle_height - 18)
            triangle = QPolygon([
                QPoint(center_x, triangle_top),
                QPoint(center_x - triangle_width // 2, triangle_top + triangle_height),
                QPoint(center_x + triangle_width // 2, triangle_top + triangle_height),
            ])
            painter.setBrush(QBrush(QColor(255, 35, 35)))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawPolygon(triangle)

            warning_font = QFont("Arial", max(26, triangle_width // 2))
            warning_font.setBold(True)
            painter.setFont(warning_font)
            painter.setPen(QColor(20, 0, 0))
            painter.drawText(
                QRectF(
                    center_x - triangle_width // 2,
                    triangle_top + triangle_height * 0.14,
                    triangle_width,
                    triangle_height * 0.72,
                ),
                Qt.AlignmentFlag.AlignCenter,
                "!",
            )

            title_font = QFont("Arial", max(20, min(42, int(w * 0.045))))
            title_font.setBold(True)
            painter.setFont(title_font)
            painter.setPen(QColor(255, 45, 45))
            title = I18N.tr("program_viewer.media_offline", "Media Offline")
            title_rect = painter.fontMetrics().boundingRect(title)
            painter.drawText(
                int(center_x - title_rect.width() / 2),
                triangle_top + triangle_height + title_rect.height() + 24,
                title,
            )

            painter.setFont(QFont("Arial", 12))
            painter.setPen(QColor(240, 210, 210))
            detail = self._error_message or I18N.tr(
                "program_viewer.media_offline_hint",
                "Relink this media from the Media Pool",
            )
            detail_rect = QRectF(24, h / 2 + 82, max(0, w - 48), 42)
            painter.drawText(
                detail_rect,
                Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop,
                detail,
            )

        elif self._error_state == "render_error":
            # 显示错误信息
            font = QFont("Arial", 14)
            painter.setFont(font)
            painter.setPen(QColor(255, 100, 100))

            # 错误标题
            title = I18N.tr("program_viewer.render_error", "Render Error")
            title_rect = painter.fontMetrics().boundingRect(title)
            x = (w - title_rect.width()) / 2
            y = (h - title_rect.height()) / 2 - 20
            painter.drawText(int(x), int(y + title_rect.height()), title)

            # 错误消息
            if self._error_message:
                msg_font = QFont("Arial", 12)
                painter.setFont(msg_font)
                painter.setPen(QColor(200, 200, 200))
                msg_rect = painter.fontMetrics().boundingRect(self._error_message)
                x = (w - msg_rect.width()) / 2
                y = (h - msg_rect.height()) / 2 + 20
                painter.drawText(int(x), int(y + msg_rect.height()), self._error_message)

        elif self._error_state == "decode_error":
            # 显示解码错误
            font = QFont("Arial", 14)
            painter.setFont(font)
            painter.setPen(QColor(255, 150, 50))

            title = I18N.tr("program_viewer.decode_error", "Decode Error")
            title_rect = painter.fontMetrics().boundingRect(title)
            x = (w - title_rect.width()) / 2
            y = (h - title_rect.height()) / 2 - 20
            painter.drawText(int(x), int(y + title_rect.height()), title)

            if self._error_message:
                msg_font = QFont("Arial", 12)
                painter.setFont(msg_font)
                painter.setPen(QColor(200, 200, 200))
                msg_rect = painter.fontMetrics().boundingRect(self._error_message)
                x = (w - msg_rect.width()) / 2
                y = (h - msg_rect.height()) / 2 + 20
                painter.drawText(int(x), int(y + msg_rect.height()), self._error_message)

        elif self._error_state == "composite_error":
            # R-8: Composite render error overlay.
            # When show_ghost is True, the last good texture is still rendered
            # under this semi-transparent red overlay.
            painter.fillRect(self.rect(), QColor(80, 20, 20, 120))

            # Title
            font = QFont("Arial", 14)
            painter.setFont(font)
            painter.setPen(QColor(255, 80, 80))

            title = I18N.tr("program_viewer.composite_error", "Composite Render Error")
            title_rect = painter.fontMetrics().boundingRect(title)
            x = (w - title_rect.width()) / 2
            y = (h - title_rect.height()) / 2 - 20
            painter.drawText(int(x), int(y + title_rect.height()), title)

            # Error message
            if self._error_message:
                msg_font = QFont("Arial", 12)
                painter.setFont(msg_font)
                painter.setPen(QColor(220, 200, 200))
                msg_rect = painter.fontMetrics().boundingRect(self._error_message)
                x = (w - msg_rect.width()) / 2
                y = (h - msg_rect.height()) / 2 + 20
                painter.drawText(int(x), int(y + msg_rect.height()), self._error_message)

        # No painter.end() here -- caller manages the QPainter lifecycle

    # AUD-20401: _paint_qimage_overlay 已删除（唯一读取者就是它自己：
    # pending QImage 现在在 paintGL 内上传为纹理）。错误状态 overlay 已
    # 在 _paintGL_impl 中接通 _paint_error_overlay；时间码 overlay 保留
    # 实现但由 transport bar 承担显示（见 _paint_timecode_overlay 注释）。

    def _calculate_display_rect_qt(self) -> QRectF:
        """
        Calculate display rectangle for Qt painting (in widget coordinates).

        Returns:
            QRectF in widget pixel coordinates
        """
        w = self.width()
        h = self.height()

        if w <= 0 or h <= 0:
            return QRectF(0, 0, 0, 0)

        # Use stored dimensions from pending image
        if self._tex_width > 0 and self._tex_height > 0:
            tex_w = self._tex_width
            tex_h = self._tex_height
        else:
            tex_w = int(self._video_aspect_ratio * h)
            tex_h = h

        x, y, display_w, display_h = self._view_transform.display_rect_px(tex_w, tex_h, w, h)

        return QRectF(x, y, display_w, display_h)

    def _paint_timecode_overlay(self, painter: QPainter) -> None:
        """Paint timecode overlay (called from paintGL with active QPainter).

        AUD-20401: 保留方法本身（画布内时间码的既有实现），当前由底部
        transport bar 承担时间码显示，无调用点；若后续需要画布内时间码，
        在 _paintGL_impl 中按 guides overlay 模式接通即可。
        """
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        # Timecode font
        font = QFont("Monospace", 14, QFont.Weight.Bold)
        painter.setFont(font)

        # Calculate text size
        text_rect = painter.fontMetrics().boundingRect(self._timecode)
        padding = 8

        # Position: bottom-right corner
        x = self.width() - text_rect.width() - padding * 2 - 10
        y = self.height() - text_rect.height() - padding * 2 - 10

        # Draw background
        bg_rect = QRectF(x - padding, y - padding,
                        text_rect.width() + padding * 2,
                        text_rect.height() + padding * 2)
        painter.fillRect(bg_rect, QColor(35, 35, 35, 180))

        # Draw border
        border_pen = QPen(QColor(80, 80, 80), 1)
        painter.setPen(border_pen)
        painter.drawRect(bg_rect)

        # Draw text
        painter.setPen(QColor(255, 255, 255))
        painter.drawText(x, y + text_rect.height(), self._timecode)

        # No painter.end() here — caller manages the QPainter lifecycle

    # ==================== P10.3: Subtitle Overlay ====================

    def set_subtitle_overlays(self, clips: list) -> None:
        """设置当前帧需要绘制的字幕片段列表。

        由 EditPage 在播放头变化时调用，传入
        ``timeline.get_active_subtitle_clips(frame)`` 的结果。
        设置后触发重绘。

        每个元素可以是裸 ``SubtitleClip``（旧行为，按扁平字段渲染），也可以是
        ``(clip, style[, graphic])``。``style`` 是已解析的样式字典（见
        captions.model.resolve_style）；可选 ``graphic`` 携带动画模板的逐帧状态。
        """
        # 统一存成 (clip, style[, graphic])；裸 clip 的 style 留 None（走 legacy 字段）。
        pairs = []
        for item in clips:
            if isinstance(item, tuple):
                clip, style = item[0], (item[1] if len(item) > 1 else None)
                graphic = item[2] if len(item) > 2 else None
                pairs.append((clip, style, graphic) if graphic is not None else (clip, style))
            else:
                pairs.append((item, None))
        # Track edits reuse the same immutable cue object, so compare the
        # resolved style contents as well as object identity.
        def _style_key(style: object) -> object:
            if style is None:
                return None
            if isinstance(style, dict):
                return tuple(sorted((str(key), repr(value))
                                    for key, value in style.items()))
            return repr(style)

        prev_keys = [
            (id(item[0]), _style_key(item[1]), repr(item[2]) if len(item) > 2 else None)
            for item in self._subtitle_overlays
        ]
        new_keys = [
            (id(item[0]), _style_key(item[1]), repr(item[2]) if len(item) > 2 else None)
            for item in pairs
        ]
        self._subtitle_overlays = pairs
        if prev_keys != new_keys:
            self.update()

    def set_subtitle_canvas_size(self, width: int, height: int) -> None:
        """设置字幕渲染所用的画布尺寸（通常等于时间线分辨率）。"""
        new_size = (max(1, int(width)), max(1, int(height)))
        if new_size != self._subtitle_canvas_size:
            self._subtitle_canvas_size = new_size
            # 画布尺寸变化 → 缓存失效
            try:
                from src.features.edit.core.subtitle_renderer import invalidate_cache
                invalidate_cache()
            except Exception:
                pass
            if self._subtitle_overlays:
                self.update()
        # 同步给手柄控制器
        self._subtitle_handle_controller.set_canvas_size(width, height)

    # ==================== P10.3: Subtitle Selection / Handles ====================

    def set_selected_subtitle_clip(self, clip, style=None) -> None:
        """设置当前选中的字幕片段（用于显示变换手柄）。None 表示清除。

        ``style`` 为已解析的分层样式字典，使手柄 bbox 与渲染像素一致
        （SUB-P2-01/06）；``None`` 走 legacy 扁平字段。
        """
        self._subtitle_handle_controller.set_clip(clip, style=style)
        self.update()

    def _selected_subtitle_is_visible(self) -> bool:
        """Return whether the selected subtitle is active at this playhead.

        A selected cue can remain selected after playback leaves its time
        range. Skipping its overlay in that state prevents repeated glyph
        geometry work on every playback frame while keeping selection intact
        for when the playhead returns.
        """
        selected_id = self._subtitle_handle_controller.selected_clip_id()
        if selected_id is None:
            return False
        return any(getattr(clip, "clip_id", None) == selected_id
                   for item in self._subtitle_overlays
                   for clip in (item[0],))

    # ==================== Clip Transform Tool / Handles ====================

    def set_transform_tool_active(self, active: bool) -> None:
        """Toggle the on-canvas Transform handle tool.

        When active, selecting a clip shows move/scale/rotate handles over
        the program viewer and left-drag edits the clip's TransformParams.
        """
        if self._transform_tool_active == active:
            return
        self._transform_tool_active = active
        if not active:
            # Tool off → clear any in-flight drag and hide handles.
            self._clip_transform_controller.set_clip(None, None)
        self.transform_tool_toggled.emit(active)
        self.update()

    def is_transform_tool_active(self) -> bool:
        return self._transform_tool_active

    def set_selected_clip_transform(
        self, clip_id, transform, canvas_width: int, canvas_height: int,
    ) -> None:
        """Set the currently selected clip for the Transform tool.

        Args:
            clip_id: clip id (or None to clear).
            transform: TransformParams (or None).
            canvas_width, canvas_height: sequence resolution — handle
                geometry and the normalized↔pixel bridge depend on it.
        """
        self._clip_transform_controller.set_canvas_size(canvas_width, canvas_height)
        self._clip_transform_controller.set_clip(clip_id, transform)
        self.update()

    def update_clip_transform(self, transform) -> None:
        """Refresh handle positions when the clip's transform changes outside
        of a drag (e.g. edited via the properties panel). No-op if the tool
        isn't active or no clip is selected.
        """
        if self._transform_tool_active:
            self._clip_transform_controller.set_transform(transform)
            self.update()

    def _on_clip_transform_drag_updated(self, clip_id, new_transform) -> None:
        """Handle drag in progress — emit for the controller to apply live."""
        self.clip_transform_interaction_updated.emit(clip_id, new_transform)

    def _on_clip_transform_drag_ended(self, clip_id, final_transform, original) -> None:
        """Handle drag ended — emit for the controller to push an undo command."""
        self.clip_transform_interaction_ended.emit(clip_id, final_transform, original)

    def _on_subtitle_drag_started(self, clip_id: str) -> None:
        """手柄 drag 开始。"""
        self.subtitle_interaction_started.emit(clip_id)

    def _on_subtitle_drag_updated(self, clip_id: str, delta: dict) -> None:
        """手柄 drag 进行中。delta 是参数变化字典。

        controller 不直接修改 clip，而是发信号给 controller(EditController)
        由它构造 UpdateSubtitleClipCommand 应用到 timeline。
        """
        self.subtitle_interaction_updated.emit(clip_id, delta)

    def _on_subtitle_drag_ended(self, clip_id: str, final_delta: dict,
                                original: dict) -> None:
        """手柄 drag 结束。final_delta 是相对原始值的最终变化。"""
        self.subtitle_interaction_ended.emit(clip_id, final_delta, original)

    def _paint_subtitle_overlay(self, painter: QPainter) -> None:
        """绘制字幕 overlay（paintGL 内由 caller 以普通 QPainter 调用）。

        对每个 SubtitleClip：
        1. 从 subtitle_renderer 取得缓存的紧凑字形 RGBA 纹理；
        2. 首次出现时转换为小型 QImage；
        3. 在 QPainter 中直接应用字幕变换并映射至 display rect。

        这样相同字幕不会在每帧生成或转换一张全画布图像。
        """
        if not self._subtitle_overlays:
            return

        try:
            from src.features.edit.core.subtitle_renderer import (
                get_subtitle_texture_cached,
            )
            # AUD-20411: render_plan 导入原在每帧×每字幕的循环体内；提升到
            # 每帧一次（帧级热路径上的 import 查找是纯损耗）。
            from src.features.edit.core.captions.render_plan import from_clip
        except Exception:
            logger.exception("Failed to import subtitle_renderer")
            return
        # AUD-20411: caption_graphic_animation_state 同病提升；导入失败只
        # 禁用动画，不影响字幕本体绘制（与原循环内 try/except 语义一致）。
        try:
            from src.features.edit.core.captions.graphics import (
                caption_graphic_animation_state,
            )
        except Exception:
            logger.exception("Failed to import caption graphics animation")
            caption_graphic_animation_state = None

        canvas_w, canvas_h = self._subtitle_canvas_size
        # 计算 display rect（widget 像素空间，保持宽高比）
        try:
            display_rect = self._calculate_display_rect_qt()
        except Exception:
            return
        if display_rect.width() <= 0 or display_rect.height() <= 0:
            return

        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        for item in self._subtitle_overlays:
            clip = item[0]
            style = item[1] if len(item) > 1 else None
            graphic = item[2] if len(item) > 2 else None
            try:
                texture = get_subtitle_texture_cached(clip, canvas_w, canvas_h, style=style)
            except Exception:
                logger.exception(f"Failed to render subtitle clip {getattr(clip, 'clip_id', '?')}")
                continue
            img = self._subtitle_qimage_cache.get(texture.cache_key)
            if img is None:
                # QImage is copied so it remains valid even after the renderer
                # LRU evicts its numpy backing array.
                img_data = np.ascontiguousarray(
                    (np.clip(texture.rgba, 0.0, 1.0) * 255.0 + 0.5).astype(np.uint8)
                )
                img = QImage(
                    img_data.data, texture.width, texture.height,
                    int(img_data.strides[0]), QImage.Format.Format_RGBA8888,
                ).copy()
                if len(self._subtitle_qimage_cache) >= 16:
                    self._subtitle_qimage_cache.pop(next(iter(self._subtitle_qimage_cache)))
                self._subtitle_qimage_cache[texture.cache_key] = img

            # SUB-P1-10: consume the canonical transform (shared with the
            # export warp and the on-canvas handles) so preview and burn-in
            # cannot drift on pivot/transform order. The canonical affine maps
            # texture-local → texture-local-anchored; we add the bbox origin to
            # land in canvas space, then map canvas → displayed video rect.
            animation = None
            render_style = style
            if isinstance(graphic, dict) and caption_graphic_animation_state is not None:
                try:
                    animation = caption_graphic_animation_state(
                        str(graphic.get("effect", "fade")),
                        int(graphic.get("local_frame", 0)),
                        int(getattr(clip, "timeline_duration", 1)),
                        graphic.get("parameters", {}),
                        content_width=texture.width,
                        content_height=texture.height,
                    )
                    # ``from_clip`` accepts resolved styles, so fold the
                    # time-varying animation transform into that same
                    # canonical matrix used by burn-in/export.
                    render_style = dict(style or {})
                    for name, fallback in (
                        ("scale_x", 1.0), ("scale_y", 1.0),
                        ("translate_x", 0.0), ("translate_y", 0.0),
                    ):
                        value = render_style.get(name)
                        if value is None:
                            value = getattr(clip, name, fallback)
                        render_style[name] = float(value)
                    render_style["scale_x"] *= animation.scale
                    render_style["scale_y"] *= animation.scale
                    render_style["translate_x"] += animation.translate_x
                    render_style["translate_y"] += animation.translate_y
                except Exception:
                    logger.exception("Failed to resolve subtitle graphic animation")
                    animation = None
                    render_style = style

            tf = from_clip(clip, texture.x, texture.y,
                           float(texture.width), float(texture.height),
                           style=render_style)
            m00, m01, m02, m10, m11, m12 = tf.m
            # bbox origin offset (texture-local-anchored → canvas space).
            m02 += texture.x
            m12 += texture.y
            painter.save()
            if animation is not None:
                painter.setOpacity(animation.opacity)
            painter.translate(display_rect.left(), display_rect.top())
            painter.scale(display_rect.width() / canvas_w,
                          display_rect.height() / canvas_h)
            # QTransform(m11, m12, m21, m22, dx, dy) = row-major affine.
            painter.setTransform(
                QTransform(m00, m10, m01, m11, m02, m12), True
            )
            if animation is not None and animation.reveal < 1.0:
                reveal_width = max(0.0, texture.width * animation.reveal)
                if reveal_width > 0.0:
                    reveal_rect = QRectF(0.0, 0.0, reveal_width, float(texture.height))
                    painter.drawImage(reveal_rect, img, reveal_rect)
            else:
                painter.drawImage(QPointF(0.0, 0.0), img)
            painter.restore()


# ================================================================
# ProgramViewerWidget - Main Container
# ================================================================

class ProgramViewerWidget(QWidget):
    """
    Main container widget for the Program Viewer.

    Contains:
    - ProgramViewerBar (toolbar)
    - ProgramViewer (OpenGL viewport)
    - TransportControlBar (playback controls at bottom)

    Signals:
        fullscreen_changed: Fullscreen state changed
        viewport_resized: Viewport size changed (P8-02)
        transport_jump_start: User clicked jump to start
        transport_prev_frame: User clicked previous frame
        transport_play: User clicked play/pause
        transport_next_frame: User clicked next frame
        transport_jump_end: User clicked jump to end
        transport_loop_toggled: User toggled loop mode
        transport_timecode_clicked: User clicked timecode display
    """

    fullscreen_changed = pyqtSignal(bool)
    viewport_resized = pyqtSignal(int, int)  # width, height (P8-02)
    preview_quality_changed = pyqtSignal(float)  # scale: 0.0(auto), 1.0, 0.5, 0.333, 0.25
    guide_changed = pyqtSignal(str, str, dict)  # guide_type, guide_id, params
    monitor_frame_ready = pyqtSignal(int, QImage)
    monitor_gpu_frame_ready = pyqtSignal(int, int, int, int, bool)
    # Transport control signals (forwarded to TimelinePanel)
    transport_jump_start = pyqtSignal()
    transport_prev_frame = pyqtSignal()
    transport_play = pyqtSignal()
    transport_next_frame = pyqtSignal()
    transport_jump_end = pyqtSignal()
    transport_loop_toggled = pyqtSignal(bool)
    transport_timecode_clicked = pyqtSignal()
    timeline_switch_requested = pyqtSignal(str)

    # P10.3: 字幕手柄交互（转发自内部 ProgramViewer）
    subtitle_interaction_started = pyqtSignal(str)
    subtitle_interaction_updated = pyqtSignal(str, dict)
    subtitle_interaction_ended = pyqtSignal(str, dict, dict)

    # Clip Transform on-canvas handle interaction (forwarded from inner viewer).
    clip_transform_interaction_updated = pyqtSignal(str, object)
    clip_transform_interaction_ended = pyqtSignal(str, object, object)
    transform_tool_toggled = pyqtSignal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("ProgramViewerWidget")

        # Build UI
        self._setup_ui()

        logger.debug("ProgramViewerWidget initialized")

    def _setup_ui(self) -> None:
        """Build the UI layout."""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Toolbar (top)
        self._toolbar = ProgramViewerBar(None, self)
        layout.addWidget(self._toolbar)

        # Viewer container (for fullscreen parent tracking)
        self._viewer_container = QWidget()
        self._viewer_container.setObjectName("viewerContainer")
        viewer_layout = QVBoxLayout(self._viewer_container)
        viewer_layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._viewer_container, stretch=1)

        # OpenGL Viewer
        self._viewer = ProgramViewer(self._viewer_container)
        viewer_layout.addWidget(self._viewer)

        # Transport Control Bar (bottom)
        self._transport_bar = TransportControlBar(self)
        layout.addWidget(self._transport_bar)

        # Connect toolbar signals
        self._toolbar.fit_requested.connect(self._viewer._fit_to_window)
        self._toolbar.actual_size_requested.connect(self._viewer._actual_size)
        self._toolbar.zoom_in_requested.connect(self._viewer.zoom_in)
        self._toolbar.zoom_out_requested.connect(self._viewer.zoom_out)
        self._toolbar.fullscreen_requested.connect(self._viewer.toggle_fullscreen)
        self._toolbar.guide_changed.connect(self._on_guide_changed)
        self._toolbar.preview_quality_changed.connect(self.preview_quality_changed.emit)
        self._toolbar.timeline_switch_requested.connect(
            self.timeline_switch_requested.emit
        )

        # Connect viewer signals
        self._viewer.zoom_changed.connect(self._on_zoom_changed)
        self._viewer.fullscreen_changed.connect(self._on_fullscreen_changed)
        self._viewer.double_clicked.connect(self._viewer.toggle_fullscreen)
        self._viewer.viewport_resized.connect(self.viewport_resized.emit)  # P8-02
        self._viewer.monitor_frame_ready.connect(self.monitor_frame_ready.emit)
        self._viewer.monitor_gpu_frame_ready.connect(self.monitor_gpu_frame_ready.emit)
        self._viewer.frame_presented.connect(self._toolbar.record_presented_frame)
        # P10.3: 字幕手柄交互信号转发
        self._viewer.subtitle_interaction_started.connect(self.subtitle_interaction_started.emit)
        self._viewer.subtitle_interaction_updated.connect(self.subtitle_interaction_updated.emit)
        self._viewer.subtitle_interaction_ended.connect(self.subtitle_interaction_ended.emit)
        # Clip Transform tool: toolbar button → viewer activation; viewer
        # interaction signals → this widget's matching signals (which EditPage
        # forwards to the signal_hub → EditController).
        self._toolbar.transform_tool_requested.connect(self._viewer.set_transform_tool_active)
        self._viewer.clip_transform_interaction_updated.connect(self.clip_transform_interaction_updated.emit)
        self._viewer.clip_transform_interaction_ended.connect(self.clip_transform_interaction_ended.emit)
        self._viewer.transform_tool_toggled.connect(self.transform_tool_toggled.emit)

        # Connect transport bar signals (forward to external listeners)
        self._transport_bar.jump_start_clicked.connect(self.transport_jump_start.emit)
        self._transport_bar.prev_frame_clicked.connect(self.transport_prev_frame.emit)
        self._transport_bar.play_clicked.connect(self.transport_play.emit)
        self._transport_bar.next_frame_clicked.connect(self.transport_next_frame.emit)
        self._transport_bar.jump_end_clicked.connect(self.transport_jump_end.emit)
        self._transport_bar.loop_toggled.connect(self.transport_loop_toggled.emit)
        self._transport_bar.timecode_clicked.connect(self.transport_timecode_clicked.emit)

        # Set minimum size
        self.setMinimumSize(200, 150)

    def _on_zoom_changed(self, zoom_level: float) -> None:
        """Handle zoom change."""
        self._toolbar.update_zoom_label(zoom_level)

    def _on_guide_changed(
        self,
        guide_type: str,
        guide_id: str,
        params: Dict[str, Any],
    ) -> None:
        """Apply a Guides selection and expose it to the project controller."""
        self._viewer.set_guide(guide_type, guide_id, params)
        self.guide_changed.emit(guide_type, guide_id, params)

    def _on_fullscreen_changed(self, is_fullscreen: bool) -> None:
        """Handle fullscreen state change."""
        self.fullscreen_changed.emit(is_fullscreen)

    # ==================== Public API ====================

    def get_viewer(self) -> ProgramViewer:
        """Get the ProgramViewer instance."""
        return self._viewer

    def set_monitor_readback_enabled(self, enabled: bool) -> None:
        """Enable GPU PBO readback for external monitor output."""
        self._viewer.set_monitor_readback_enabled(enabled)

    def set_monitor_gpu_signal_enabled(self, enabled: bool) -> None:
        """Enable GPU texture metadata signaling for native monitor output."""
        self._viewer.set_monitor_gpu_signal_enabled(enabled)

    def get_toolbar(self) -> ProgramViewerBar:
        """Get the ProgramViewerBar instance."""
        return self._toolbar

    def get_transport_bar(self) -> 'TransportControlBar':
        """Get the TransportControlBar instance."""
        return self._transport_bar

    def set_timeline_choices(
        self,
        timelines: Any,
        active_timeline_id: Optional[str],
    ) -> None:
        """Show all project timelines and mark the active one in the monitor."""
        self._toolbar.set_timeline_choices(timelines, active_timeline_id)

    def set_texture(self, tex_id: int, width: int, height: int) -> None:
        """Set the texture to display."""
        self._viewer.set_texture(tex_id, width, height)

    def set_timecode(self, timecode: str, fps: float = 24.0) -> None:
        """Set the timecode overlay text."""
        self._viewer.set_timecode(timecode, fps)

    def set_clip_info(self, clip_id: str, frame_index: int) -> None:
        """Set current clip info for frame requests."""
        self._viewer.set_clip_info(clip_id, frame_index)

    def set_guide(self, guide_type: str, guide_id: str, params: Dict[str, Any]) -> None:
        """Set the monitor guide without emitting a project-change signal."""
        self._viewer.set_guide(guide_type, guide_id, params)

    # ==================== P10.3: Subtitle Overlay Forwarding ====================

    def set_subtitle_overlays(self, clips: list) -> None:
        """Forward subtitle overlay list to the inner ProgramViewer."""
        self._viewer.set_subtitle_overlays(clips)

    def set_subtitle_canvas_size(self, width: int, height: int) -> None:
        """Forward subtitle canvas size to the inner ProgramViewer."""
        self._viewer.set_subtitle_canvas_size(width, height)

    def set_selected_subtitle_clip(self, clip, style=None) -> None:
        """Forward selected subtitle clip to the inner ProgramViewer."""
        self._viewer.set_selected_subtitle_clip(clip, style=style)

    # ── Clip Transform tool delegation ──
    def set_transform_tool_active(self, active: bool) -> None:
        """Forward Transform tool toggle to the inner ProgramViewer."""
        self._viewer.set_transform_tool_active(active)

    def is_transform_tool_active(self) -> bool:
        return self._viewer.is_transform_tool_active()

    def set_selected_clip_transform(self, clip_id, transform,
                                    canvas_width: int, canvas_height: int) -> None:
        """Forward selected clip + canvas size to the inner ProgramViewer."""
        self._viewer.set_selected_clip_transform(clip_id, transform, canvas_width, canvas_height)

    def update_clip_transform(self, transform) -> None:
        """Forward a transform refresh (e.g. from panel edit) to the viewer."""
        self._viewer.update_clip_transform(transform)

    def clear_texture(self) -> None:
        """Clear the current texture and pending QImage (P2.1 / P8)."""
        self._viewer.clear_texture()
        # P8: Reset error state to none (show empty frame, not error)
        self._viewer._error_state = "none"
        self._viewer._error_message = ""

    def toggle_fullscreen(self) -> None:
        """Toggle fullscreen mode."""
        self._viewer.toggle_fullscreen()

    # ==================== Transport Bar Public API ====================

    def update_transport_timecode(self, timecode: str) -> None:
        """Update the transport bar timecode display."""
        self._transport_bar.update_timecode(timecode)

    def update_transport_play_button(self, is_playing: bool) -> None:
        """Update the transport bar play button icon."""
        self._transport_bar.update_play_button(is_playing)

    def set_playback_state(self, is_playing: bool, target_fps: float = 24.0) -> None:
        """Update the top-right realtime FPS indicator."""
        self._toolbar.set_playback_state(is_playing, target_fps)

    def update_transport_speed(self, speed: float, direction: int = 1) -> None:
        """Update the transport bar speed label."""
        self._transport_bar.update_speed_label(speed, direction)

    def set_transport_loop_checked(self, checked: bool) -> None:
        """Set transport bar loop button checked state."""
        self._transport_bar.set_loop_checked(checked)

    # ==================== P2.1: Timeline Preview Renderer Support ====================

    def set_qimage(self, image: QImage) -> None:
        """
        Set a QImage to display (P2.1 / P8).

        将 QImage 存储为 pending_qimage，在 paintGL 中上传为 OpenGL 纹理。

        Args:
            image: QImage to display (BGR888 or RGB888 format)
        """
        if image is None or image.isNull():
            self.clear_texture()
            return

        # Accept both BGR888 and RGB888 formats to avoid unnecessary conversions
        if image.format() not in (QImage.Format.Format_BGR888, QImage.Format.Format_RGB888):
            image = image.convertToFormat(QImage.Format.Format_BGR888)

        # P8-05 的防御性 copy 已移除：worker 的 _numpy_to_qimage 用零拷贝
        # 构造 QImage 并把 numpy 缓冲挂为 _bgr_data_ref（随 QImage 存活），
        # 且 readback 每帧新分配缓冲、绝不复用改写——copy 只剩每帧一次
        # 全帧拷贝+分配的纯开销（播放热路径）。外部传入的 QImage 若引用
        # 可复用缓冲，请在构造侧同样挂引用。

        self._viewer._ack_pending_present_without_display()

        # Store image for GL context upload
        self._viewer._pending_qimage = image
        self._viewer._tex_width = image.width()
        self._viewer._tex_height = image.height()
        playback_trace(
            "program_viewer.set_qimage",
            width=image.width(),
            height=image.height(),
            format=image.format(),
        )

        # Clear error state
        self._viewer._error_state = "none"
        self._viewer._error_message = ""
        self._viewer._offline_media = ()

        # Trigger repaint (will upload texture in paintGL)
        self._viewer.update()

    def set_frame(self, frame: int, result: object) -> None:
        """
        Unified frame display method (P2.1 / P8).

        Handles both QImage and texture_id results from TimelinePreviewRenderer.

        Args:
            frame: Frame number (for logging/debugging)
            result: Either a QImage or a texture_id (int or tuple)
        """
        # Recursion guard: track call depth to detect signal loops
        self._set_frame_depth = getattr(self, '_set_frame_depth', 0) + 1
        if self._set_frame_depth > 3:
            print(f"[RECURSION WARNING] set_frame called {self._set_frame_depth} times, possible signal loop!")
            self._set_frame_depth -= 1
            return

        try:
            try:
                scrub_active_for_trace = bool(getattr(self, "_timeline_scrub_active_for_trace", False))
            except RuntimeError:
                scrub_active_for_trace = False
            self._viewer._current_frame_index = int(frame)
            if result is None:
                # P8: 显示"无片段"状态
                playback_trace("program_viewer.set_frame_none", frame=frame, scrub_active=scrub_active_for_trace)
                self._viewer.set_error_state("no_clip")
                return

            # Handle GPU present descriptor (fence + slot ack path).
            if isinstance(result, dict) and result.get("present_frame") is not None:
                present = result.get("present_frame")
                ack_cb = result.get("ack_cb")
                trace_id = int(result.get("trace_id") or 0)
                playback_trace(
                    "program_viewer.set_frame_present_dict",
                    frame=frame,
                    tex_id=getattr(present, "texture_id", 0),
                    slot_id=getattr(present, "slot_id", -1),
                    has_fence=getattr(present, "has_fence", False),
                    scrub_active=scrub_active_for_trace,
                )
                self._viewer.set_present_frame(
                    present, ack_cb=ack_cb, trace_id=trace_id)
                return

            if hasattr(result, "texture_id") and hasattr(result, "slot_id"):
                playback_trace(
                    "program_viewer.set_frame_present",
                    frame=frame,
                    tex_id=getattr(result, "texture_id", 0),
                    slot_id=getattr(result, "slot_id", -1),
                    has_fence=getattr(result, "has_fence", False),
                    scrub_active=scrub_active_for_trace,
                )
                self._viewer.set_present_frame(result)
                return

            # Handle QImage result (primary path)
            if isinstance(result, QImage):
                playback_trace(
                    "program_viewer.set_frame_qimage",
                    frame=frame,
                    width=result.width(),
                    height=result.height(),
                    format=result.format(),
                    scrub_active=scrub_active_for_trace,
                )
                self.set_qimage(result)
                return

            # Handle GPU texture result (tuple: tex_id, width, height)
            if isinstance(result, tuple) and len(result) == 3:
                # Final-4 (FINAL-P0-09 / FINAL-P1-10): REALTIME 模式禁止裸 tuple。
                # 裸 tuple 绕过 fence/ack 协议，在播放热路径会导致混帧/半帧。
                # 仅允许非 realtime（paused/step/scrub/测试）使用此路径。
                if getattr(self, "_realtime_mode", False):
                    tex_id_t = result[0]
                    playback_trace(
                        "program_viewer.set_frame_realtime_tuple_rejected",
                        frame=frame,
                        tex_id=tex_id_t,
                        scrub_active=scrub_active_for_trace,
                    )
                    # 拒绝：保持上一帧（hold），不调用 set_shared_texture。
                    return
                tex_id, width, height = result
                if tex_id is None or tex_id == 0:
                    playback_trace(
                        "program_viewer.set_frame_invalid_texture",
                        frame=frame,
                        tex_id=tex_id,
                        scrub_active=scrub_active_for_trace,
                    )
                    self._viewer.set_error_state("render_error", "Invalid texture ID")
                    return
                playback_trace(
                    "program_viewer.set_frame_texture",
                    frame=frame,
                    tex_id=tex_id,
                    width=width,
                    height=height,
                    scrub_active=scrub_active_for_trace,
                )
                self._viewer.set_shared_texture(tex_id, width, height)
                return

            # Unknown result type
            playback_trace(
                "program_viewer.set_frame_unknown_result",
                frame=frame,
                result_type=type(result).__name__,
                scrub_active=scrub_active_for_trace,
            )
            self.clear_texture()
        finally:
            self._set_frame_depth -= 1

    def set_realtime_mode(self, enabled: bool) -> None:
        """
        Final-4 (FINAL-P0-09): 通知 Viewer 当前是否处于 REALTIME 播放热路径。

        当 enabled=True 时，set_frame 拒绝裸 (tex_id, w, h) tuple —— REALTIME
        必须走 GpuPresentFrame（fence + slot ack）协议，避免混帧/半帧。
        非 realtime（paused/step/scrub/测试）允许 tuple 兼容路径。

        Args:
            enabled: True=REALTIME 播放中，False=暂停/step/scrub/export。
        """
        self._realtime_mode = bool(enabled)
        playback_trace("program_viewer.set_realtime_mode", enabled=enabled)

    def set_error_state(self, state: str, message: str = "") -> None:
        """
        设置错误状态显示。

        Args:
            state: 错误状态 ("none", "no_clip", "render_error", "decode_error")
            message: 错误消息
        """
        self._viewer.set_error_state(state, message)

    def set_offline_media(self, assets: object) -> None:
        """Show or clear the dedicated Media Offline overlay."""
        self._viewer.set_offline_media(assets)
