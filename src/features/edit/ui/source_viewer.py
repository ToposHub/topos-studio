"""
Source Viewer - Edit 页面的源素材预览器。

用于预览 Media Pool 中的素材（视频/图片），支持：
- 双击素材打开预览
- Timecode 显示
- 播放控制 (play/pause/stop)
- Mark In / Mark Out 设置
- Insert / Overwrite / Append 按钮（信号连接到 Timeline）

设计原则：
- 不修改 Timeline，只预览素材
- 与 ProgramViewer 类似的 OpenGL 渲染架构
- 简化的播放控制（不需要完整的 VideoPlaybackEngine）

Architecture:
    ┌─────────────────────────────────────────────────────────┐
    │                                                         │
    │  SourceViewer (QOpenGLWidget)                          │
    │                                                         │
    │   ┌─────────────────────────────────────┐               │
    │   │                                     │               │
    │   │   Video Frame Texture               │               │
    │   │   (aspect ratio preserved)          │               │
    │   │                                     │               │
    │   └─────────────────────────────────────┘               │
    │                                                         │
    └─────────────────────────────────────────────────────────┘
    ┌─────────────────────────────────────────────────────────┐
    │ SourceSlider (scrub bar with In/Out markers)            │
    └─────────────────────────────────────────────────────────┘
    ┌─────────────────────────────────────────────────────────┐
    │ [I] [O] [Clear] | [<<] [<] [▶] [>] [>>]  | 00:00:00:00 │
    └─────────────────────────────────────────────────────────┘

Signals:
    in_point_changed: In 点改变 (frame)
    out_point_changed: Out 点改变 (frame)
    insert_requested: 请求插入 (asset_id, in_frame, out_frame)
    overwrite_requested: 请求覆盖插入 (asset_id, in_frame, out_frame)
    append_requested: 请求追加 (asset_id, in_frame, out_frame)

E22: 旧 SourceViewerBar 的无参 insert/overwrite/append_requested 与
SourceViewerWidget 的 source_opened/source_closed 已删除——全仓无
connect 的死信号（Insert/Overwrite/Append 按钮移至 TimelinePanel 工具栏，
经 source_*_requested 走活链路）。
"""

import ctypes
import ctypes.util
import logging
import math
import threading
import time
import numpy as np
from typing import Any, Optional, Tuple

from PyQt6.QtOpenGLWidgets import QOpenGLWidget
from PyQt6.QtWidgets import (
    QWidget, QFrame, QHBoxLayout, QVBoxLayout, QToolButton,
    QLabel, QSizePolicy, QSlider,
)
from PyQt6.QtCore import (
    Qt, QObject, pyqtSignal, QPoint, QSize, QTimer,
)
from PyQt6.QtGui import (
    QPainter, QColor, QPen, QImage,
    QKeySequence, QMouseEvent, QShortcut, QWheelEvent,
)

from src.shared.i18n.i18n import I18N
from src.shared.assets.icon_manager import (
    icon_jump_start, icon_step_back, icon_play, icon_pause, icon_step_forward,
    icon_jump_end, IconColors,
)
from src.features.edit.core.project import MediaAsset
from src.features.edit.ui.preview_view_transform import PreviewViewTransform
from src.features.edit.ui.edit_style import EDIT_COLORS
from src.shared.ui.widgets.realtime_fps_indicator import RealtimeFpsIndicator

logger = logging.getLogger(__name__)


# ── OpenGL bindings via PyOpenGL ──────────────────────────────────────────────
# Windows 上 ctypes 直接加载 opengl32.dll 无法获取 OpenGL 2.0+ 函数
# 使用 PyOpenGL 提供跨平台的 OpenGL 绑定

from OpenGL.GL import (
    # Constants
    GL_VERTEX_SHADER, GL_FRAGMENT_SHADER, GL_ARRAY_BUFFER, GL_STATIC_DRAW,
    GL_TRIANGLE_STRIP, GL_TEXTURE0, GL_TEXTURE_2D, GL_RGBA,
    GL_UNSIGNED_BYTE, GL_FLOAT, GL_FALSE,
    GL_COMPILE_STATUS, GL_LINK_STATUS,
    GL_COLOR_BUFFER_BIT, GL_TEXTURE_MIN_FILTER, GL_TEXTURE_MAG_FILTER,
    GL_LINEAR, GL_TEXTURE_WRAP_S, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE,
    # Functions
    glCreateShader, glShaderSource, glCompileShader, glDeleteShader,
    glCreateProgram, glAttachShader, glLinkProgram, glDeleteProgram, glUseProgram,
    glGenBuffers, glDeleteBuffers, glBindBuffer, glBufferData,
    glGenVertexArrays, glDeleteVertexArrays, glBindVertexArray,
    glGetAttribLocation, glEnableVertexAttribArray, glVertexAttribPointer,
    glGetShaderiv, glGetProgramiv, glGetShaderInfoLog, glGetProgramInfoLog,
    glGetUniformLocation, glUniform1i, glUniform1f, glUniform4f,
    glViewport, glClearColor, glClear, glActiveTexture, glBindTexture,
    glDrawArrays, glGenTextures, glDeleteTextures, glTexImage2D,
    glTexSubImage2D, glTexParameteri, glGetError, glIsTexture,
)
from OpenGL.error import GLError

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
    glUniform1f = glUniform1f
    glUniform4f = glUniform4f
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
    glTexParameteri = glTexParameteri
    glGetError = glGetError
    glIsTexture = glIsTexture

# 防止类属性函数在某些 PyOpenGL 平台实现上是真 Python 函数时被实例
# 访问错误绑定（program_viewer 已用同一归一化）。
for _name, _value in list(_GLWrapper.__dict__.items()):
    if _name.startswith("_") or not callable(_value):
        continue
    setattr(_GLWrapper, _name, staticmethod(_value))

# INT-14（AUD 剪辑页审计）：_render_texture 的静态 quad 显示路径调用
# glUniform4f/u_flip_y 的 glUniform1f，此前两者既未 import 也未加入
# wrapper，双击素材进入 Source 即 AttributeError 崩溃退出。此契约集合
# 与上方 wrapper 保持同步；缺失项在模块导入时记录并暴露给诊断页，
# tests 中另有 AST 扫描防止未来再次漂移。
_GL_FACADE_REQUIRED = frozenset({
    "glCreateShader", "glShaderSource", "glCompileShader", "glDeleteShader",
    "glCreateProgram", "glAttachShader", "glLinkProgram", "glDeleteProgram",
    "glUseProgram", "glGenBuffers", "glDeleteBuffers", "glBindBuffer",
    "glBufferData", "glGenVertexArrays", "glDeleteVertexArrays",
    "glBindVertexArray", "glGetAttribLocation", "glEnableVertexAttribArray",
    "glVertexAttribPointer", "glGetShaderiv", "glGetProgramiv",
    "glGetShaderInfoLog", "glGetProgramInfoLog", "glGetUniformLocation",
    "glUniform1i", "glUniform1f", "glUniform4f",
    "glViewport", "glClearColor", "glClear", "glActiveTexture",
    "glBindTexture", "glDrawArrays", "glGenTextures", "glDeleteTextures",
    "glTexImage2D", "glTexSubImage2D", "glTexParameteri",
    "glGetError", "glIsTexture",
})

GL_FACADE_CONTRACT_ERRORS: list = [
    name for name in sorted(_GL_FACADE_REQUIRED)
    if getattr(_GLWrapper, name, None) is None
]
if GL_FACADE_CONTRACT_ERRORS:
    logger.critical(
        "SourceViewer GL facade contract violated (missing: %s) — "
        "painting will be disabled until fixed",
        ", ".join(GL_FACADE_CONTRACT_ERRORS),
    )

_gl = _GLWrapper()


# ================================================================
# SourceViewerBar - Toolbar
# ================================================================

class SourceViewerBar(QFrame):
    """
    Compact bottom toolbar for the Source Viewer (matches ProgramViewer TransportControlBar style).

    Provides:
    - Source name label (hidden, API compat)
    - Timecode display
    - Mark In/Out buttons
    - Clear In/Out button
    - Transport controls (jump start, prev, play/pause, next, jump end)

    Layout (single row, bottom-positioned):
        [I] [O] [Clear] | [<<] [<] [▶] [>] [>>] | ... | 00:00:00:00
    """

    # Mark point signals
    mark_in_requested = pyqtSignal()
    mark_out_requested = pyqtSignal()
    clear_in_out_requested = pyqtSignal()

    # Transport signals
    jump_start_requested = pyqtSignal()
    prev_frame_requested = pyqtSignal()
    play_pause_requested = pyqtSignal()
    next_frame_requested = pyqtSignal()
    jump_end_requested = pyqtSignal()
    seek_requested = pyqtSignal(int)  # frame
    # F2.1: Source Patch 设置入口（当前素材的 V/A 流开关与源声道映射）
    source_patch_requested = pyqtSignal()
    # E22: 旧无参 insert/overwrite/append_requested 已删除（0 emit 0 connect；
    # 按钮现居 TimelinePanel 工具栏，经 source_*_requested 走活链路）。

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("sourceViewerBar")
        self.setFixedHeight(36)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setAutoFillBackground(False)

        self._is_playing: bool = False
        self._in_point: Optional[int] = None
        self._out_point: Optional[int] = None

        self._setup_ui()
        self._apply_style()

        logger.debug("SourceViewerBar initialized")

    def _setup_ui(self) -> None:
        """Set up the UI layout — compact single-row bottom bar."""
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 2, 8, 4)
        layout.setSpacing(3)

        BTN_SIZE = 28
        ICON_SIZE = 15
        PLAY_BTN_SIZE = 32

        # --- Mark Controls (left) ---
        self.btn_mark_in = QToolButton()
        self.btn_mark_in.setText("I")
        self.btn_mark_in.setToolTip(I18N.tr("edit.source_viewer.mark_in_tooltip", "Mark In (I)"))
        self.btn_mark_in.setFixedSize(BTN_SIZE, BTN_SIZE)
        self.btn_mark_in.clicked.connect(self.mark_in_requested.emit)
        layout.addWidget(self.btn_mark_in)

        self.btn_mark_out = QToolButton()
        self.btn_mark_out.setText("O")
        self.btn_mark_out.setToolTip(I18N.tr("edit.source_viewer.mark_out_tooltip", "Mark Out (O)"))
        self.btn_mark_out.setFixedSize(BTN_SIZE, BTN_SIZE)
        self.btn_mark_out.clicked.connect(self.mark_out_requested.emit)
        layout.addWidget(self.btn_mark_out)

        self.btn_clear = QToolButton()
        self.btn_clear.setText(I18N.tr("edit.source_viewer.clear", "Clear"))
        self.btn_clear.setToolTip(I18N.tr("edit.source_viewer.clear_tooltip", "Clear In/Out Points"))
        self.btn_clear.setFixedHeight(BTN_SIZE)
        self.btn_clear.setEnabled(False)
        self.btn_clear.clicked.connect(self.clear_in_out_requested.emit)
        layout.addWidget(self.btn_clear)

        # Separator (transparent spacing, no widget)
        layout.addSpacing(10)

        # --- Transport Controls ---
        self.btn_jump_start = QToolButton()
        self.btn_jump_start.setToolTip(I18N.tr("edit.source_viewer.jump_start_tooltip", "Jump to Start"))
        self.btn_jump_start.setFixedSize(BTN_SIZE, BTN_SIZE)
        self.btn_jump_start.setIconSize(QSize(ICON_SIZE, ICON_SIZE))
        self.btn_jump_start.setIcon(icon_jump_start(IconColors.DEFAULT))
        self.btn_jump_start.clicked.connect(self.jump_start_requested.emit)
        layout.addWidget(self.btn_jump_start)

        self.btn_prev = QToolButton()
        self.btn_prev.setToolTip(I18N.tr("edit.source_viewer.prev_frame_tooltip", "Previous Frame"))
        self.btn_prev.setFixedSize(BTN_SIZE, BTN_SIZE)
        self.btn_prev.setIconSize(QSize(ICON_SIZE, ICON_SIZE))
        self.btn_prev.setIcon(icon_step_back(IconColors.DEFAULT))
        self.btn_prev.clicked.connect(self.prev_frame_requested.emit)
        layout.addWidget(self.btn_prev)

        self.btn_play = QToolButton()
        self.btn_play.setToolTip(I18N.tr("edit.source_viewer.play_tooltip", "Play/Pause"))
        self.btn_play.setFixedSize(PLAY_BTN_SIZE, BTN_SIZE)
        self.btn_play.setIconSize(QSize(ICON_SIZE + 3, ICON_SIZE + 3))
        self.btn_play.setIcon(icon_play(IconColors.ORANGE))
        self.btn_play.clicked.connect(self.play_pause_requested.emit)
        layout.addWidget(self.btn_play)

        self.btn_next = QToolButton()
        self.btn_next.setToolTip(I18N.tr("edit.source_viewer.next_frame_tooltip", "Next Frame"))
        self.btn_next.setFixedSize(BTN_SIZE, BTN_SIZE)
        self.btn_next.setIconSize(QSize(ICON_SIZE, ICON_SIZE))
        self.btn_next.setIcon(icon_step_forward(IconColors.DEFAULT))
        self.btn_next.clicked.connect(self.next_frame_requested.emit)
        layout.addWidget(self.btn_next)

        self.btn_jump_end = QToolButton()
        self.btn_jump_end.setToolTip(I18N.tr("edit.source_viewer.jump_end_tooltip", "Jump to End"))
        self.btn_jump_end.setFixedSize(BTN_SIZE, BTN_SIZE)
        self.btn_jump_end.setIconSize(QSize(ICON_SIZE, ICON_SIZE))
        self.btn_jump_end.setIcon(icon_jump_end(IconColors.DEFAULT))
        self.btn_jump_end.clicked.connect(self.jump_end_requested.emit)
        layout.addWidget(self.btn_jump_end)

        # F2.1: Source Patch（V/A 流开关 + 源声道映射）
        self.btn_source_patch = QToolButton()
        self.btn_source_patch.setText("V/A")
        self.btn_source_patch.setToolTip(I18N.tr(
            "edit.source_viewer.source_patch_tooltip",
            "Source Patch 设置（视频/音频流与源声道映射）"))
        self.btn_source_patch.setFixedHeight(BTN_SIZE)
        self.btn_source_patch.clicked.connect(self.source_patch_requested.emit)
        layout.addWidget(self.btn_source_patch)

        # Separator (transparent spacing, no widget)
        layout.addSpacing(10)

        # Center stretch
        layout.addStretch()

        # --- Timecode (right) ---
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
        layout.addWidget(self.timecode_label)

        # Hidden labels kept for API compatibility (source name, frame, duration)
        self.source_label = QLabel("")
        self.source_label.setVisible(False)
        self.frame_label = QLabel("")
        self.frame_label.setVisible(False)
        self.duration_label = QLabel("")
        self.duration_label.setVisible(False)

    def _apply_style(self) -> None:
        """Apply style sheet — matches TransportControlBar clean look."""
        self.setStyleSheet("""
            QFrame#sourceViewerBar {
                background-color: #1a1a1c;
            }
        """)

        # Clean transport button style (no heavy backgrounds/borders)
        transport_btn_style = """
            QToolButton {
                background: transparent;
                border: none;
                border-radius: 4px;
                padding: 3px;
                margin: 1px;
                color: #bbb;
                font-size: 12px;
                font-weight: bold;
            }
            QToolButton:hover {
                background-color: rgba(255, 255, 255, 18);
            }
            QToolButton:pressed {
                background-color: rgba(255, 255, 255, 10);
            }
            QToolButton:disabled {
                color: #555;
            }
        """
        for btn in [
            self.btn_mark_in, self.btn_mark_out, self.btn_clear,
            self.btn_jump_start, self.btn_prev, self.btn_play,
            self.btn_next, self.btn_jump_end,
        ]:
            btn.setStyleSheet(transport_btn_style)

    # ==================== Update Methods ====================

    def set_source_name(self, name: str) -> None:
        """Set the source name label."""
        self.source_label.setText(name if name else I18N.tr("edit.source_viewer.no_source", "No Source"))

    def set_timecode(self, timecode: str, fps: float) -> None:
        """Set the timecode display."""
        self.timecode_label.setText(timecode)

    def set_frame_number(self, frame: int) -> None:
        """Set the frame number display."""
        self.frame_label.setText(str(frame))

    def set_duration(self, timecode: str) -> None:
        """Set the duration display."""
        self.duration_label.setText(timecode)

    def update_play_state(self, is_playing: bool) -> None:
        """Update play/pause button icon."""
        self._is_playing = is_playing
        if is_playing:
            self.btn_play.setIcon(icon_pause(IconColors.ORANGE))
        else:
            self.btn_play.setIcon(icon_play(IconColors.ORANGE))

    def update_in_out_state(self, in_point: Optional[int], out_point: Optional[int]) -> None:
        """Update In/Out button states."""
        self._in_point = in_point
        self._out_point = out_point

        # Enable Clear button if any point is set
        has_points = in_point is not None or out_point is not None
        self.btn_clear.setEnabled(has_points)

        # Update button styles to indicate marks (using EDIT_COLORS accent)
        mark_active_style = """
            QToolButton {
                background-color: rgba(226, 146, 39, 80);
                border: none;
                border-radius: 4px;
                color: white;
                font-weight: bold;
                font-size: 12px;
            }
            QToolButton:hover {
                background-color: rgba(226, 146, 39, 120);
            }
        """
        if in_point is not None:
            self.btn_mark_in.setStyleSheet(mark_active_style)
        else:
            # Reset to default transport style
            self.btn_mark_in.setStyleSheet("")

        if out_point is not None:
            self.btn_mark_out.setStyleSheet(mark_active_style)
        else:
            self.btn_mark_out.setStyleSheet("")

        # Track whether edit operations are available (both In and Out set)
        # The actual Insert/Overwrite/Append buttons now live in TimelinePanel toolbar;
        # EditPage bridges the enabled state between SourceViewer and TimelinePanel.
        # FE-B05：Out 为 inclusive，I=O 是合法单帧范围（可插 1 帧）
        self._can_edit = in_point is not None and out_point is not None and in_point <= out_point

    def set_enabled(self, enabled: bool) -> None:
        """Enable/disable all transport controls."""
        self.btn_jump_start.setEnabled(enabled)
        self.btn_prev.setEnabled(enabled)
        self.btn_play.setEnabled(enabled)
        self.btn_next.setEnabled(enabled)
        self.btn_jump_end.setEnabled(enabled)
        self.btn_mark_in.setEnabled(enabled)
        self.btn_mark_out.setEnabled(enabled)


# ================================================================
# SourceSlider - Custom slider with In/Out markers
# ================================================================

class SourceSlider(QSlider):
    """
    Custom slider with In/Out point markers.

    Visual markers:
    - In point: Vertical orange line
    - Out point: Vertical orange line
    - Selection range: Orange shaded area
    """

    def __init__(self, parent=None):
        super().__init__(Qt.Orientation.Horizontal, parent)
        self._in_point: Optional[int] = None
        self._out_point: Optional[int] = None
        self.setMinimum(0)
        self.setMaximum(100)
        self.setFixedHeight(16)

    def set_in_out_points(self, in_point: Optional[int], out_point: Optional[int]) -> None:
        """Set the In/Out point positions."""
        self._in_point = in_point
        self._out_point = out_point
        self.update()

    def paintEvent(self, event) -> None:
        """Custom paint to show In/Out markers."""
        super().paintEvent(event)

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        # Draw selection range (if both points are set)
        if self._in_point is not None and self._out_point is not None:
            width = self.width()
            max_val = self.maximum()

            in_x = int((self._in_point / max_val) * width) if max_val > 0 else 0
            out_x = int((self._out_point / max_val) * width) if max_val > 0 else width

            # Draw selection range background
            painter.fillRect(
                in_x, 2,
                out_x - in_x, self.height() - 4,
                QColor(255, 136, 0, 40)
            )

            # Draw In point marker
            painter.setPen(QPen(QColor(255, 136, 0), 2))
            painter.drawLine(in_x, 0, in_x, self.height())

            # Draw Out point marker
            painter.drawLine(out_x, 0, out_x, self.height())

        painter.end()


# ================================================================
# SourceViewer - OpenGL Viewport
# ================================================================

class SourceViewer(QOpenGLWidget):
    """
    OpenGL viewport for the Source Viewer.

    Displays source media frames with:
    - Aspect ratio preservation
    - Timecode overlay

    Signals:
        seek_requested: User requested seek to frame
    """

    seek_requested = pyqtSignal(int)
    zoom_changed = pyqtSignal(float)
    gl_initialized = pyqtSignal(object)  # QOpenGLContext (for GPU pipeline)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("SourceViewer")
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._fps_indicator = RealtimeFpsIndicator(self)

        # Texture state
        self._current_tex_id: Optional[int] = None
        self._tex_width: int = 0
        self._tex_height: int = 0
        self._video_aspect_ratio: float = 16 / 9

        # Shared GPU texture state (from GPU pipeline, zero-copy display)
        self._shared_tex_id: Optional[int] = None
        self._shared_tex_width: int = 0
        self._shared_tex_height: int = 0

        # GL resources
        self._gl_initialized: bool = False
        # INT-14: paint 管线一旦失败即进入错误占位态，避免每帧重复异常
        self._gl_paint_failed: bool = False
        self._quad_vbo: Optional[int] = None
        self._vao: Optional[int] = None
        self._prog: Optional[int] = None
        # AUD-20904: uniform locations are fixed once the program is linked;
        # resolving them every frame is pure driver-call overhead.
        self._u_texture_loc: Optional[int] = None
        self._u_display_rect_loc: Optional[int] = None
        self._u_flip_y_loc: Optional[int] = None

        # Pending QImage for rendering (CPU fallback) — staged by
        # submit_qimage() and uploaded inside paintGL where the GL
        # context is current (AUD-20902).
        self._pending_qimage: Optional[QImage] = None
        self._pending_display_size: Optional[Tuple[int, int]] = None

        # Zoom/Pan state
        self._view_transform = PreviewViewTransform()
        self._is_panning: bool = False
        self._pan_start_pos: 'QPoint' = QPoint()
        self._pan_start_px: tuple[float, float] = (0.0, 0.0)

        # Mouse tracking for pan
        self.setMouseTracking(True)

        logger.debug("SourceViewer initialized")

    # ==================== Initialization ====================

    def initializeGL(self) -> None:
        """Initialize OpenGL resources."""
        if GL_FACADE_CONTRACT_ERRORS:
            # facade 契约破坏（缺 API）时不初始化绘制管线，保持占位态
            logger.critical(
                "SourceViewer GL facade contract violated, skipping GL init: %s",
                ", ".join(GL_FACADE_CONTRACT_ERRORS),
            )
            self._gl_initialized = False
            return
        # Clear any pending GL errors from context setup
        while _gl.glGetError() != 0:
            pass
        try:
            # Create shader program for texture display.
            # AUD-20904: the VBO holds a static unit quad in NDC; the display
            # rectangle and the texcoord y-flip are passed as uniforms instead
            # of rewriting and re-uploading vertex data every frame.
            self._prog = self._create_shader_program(
                """
                #version 330
                in vec2 in_vert;
                in vec2 in_texcoord;
                uniform vec4 u_display_rect;  // x, y, width, height in NDC
                uniform float u_flip_y;       // 1.0 flips texcoord y (GPU FBO textures)
                out vec2 v_texcoord;
                void main() {
                    vec2 half_size = u_display_rect.zw * 0.5;
                    vec2 center = u_display_rect.xy + half_size;
                    gl_Position = vec4(center + in_vert * half_size, 0.0, 1.0);
                    v_texcoord = vec2(in_texcoord.x, mix(in_texcoord.y, 1.0 - in_texcoord.y, u_flip_y));
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

            # AUD-20904: cache uniform locations once after linking.
            if self._prog:
                self._u_texture_loc = _gl.glGetUniformLocation(self._prog, b"u_texture")
                self._u_display_rect_loc = _gl.glGetUniformLocation(
                    self._prog, b"u_display_rect"
                )
                self._u_flip_y_loc = _gl.glGetUniformLocation(self._prog, b"u_flip_y")

            # Create quad VBO
            self._create_quad_vbo()

            # Set clear color
            _gl.glClearColor(0.08, 0.08, 0.08, 1.0)

            self._gl_initialized = True
            self._gl_paint_failed = False
            logger.info("SourceViewer GL initialized")

            # Emit GL context for GPU pipeline (shared context creation)
            ctx = self.context()
            if ctx is not None:
                self.gl_initialized.emit(ctx)

        except Exception as e:
            logger.error(f"Failed to initialize GL: {e}")
            self._gl_initialized = False

    def _handle_paint_failure(self) -> None:
        """INT-14: contain a paint pipeline failure once, then park the widget.

        Logs the first failure with traceback (no per-frame log spam), drops
        back to a cleared-background placeholder and leaves the widget/Qt
        event loop usable. Shared texture ownership is untouched; the error
        state only clears on a fresh successful initializeGL.
        """
        if not self._gl_paint_failed:
            self._gl_paint_failed = True
            logger.exception("SourceViewer paintGL failed; entering error placeholder state")
        # Best-effort state reset; guarded so a second failure cannot escape
        try:
            _gl.glBindVertexArray(0)
            _gl.glUseProgram(0)
        except Exception:
            pass

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

        # Cleanup shaders
        _gl.glDeleteShader(vs)
        _gl.glDeleteShader(fs)

        return prog

    def _create_quad_vbo(self) -> None:
        """Create VBO for full-screen quad."""
        vertices = np.array([
            # Position   Texcoord
            -1.0, -1.0,   0.0, 1.0,  # Bottom-left
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

        if self._prog:
            in_vert_loc = _gl.glGetAttribLocation(self._prog, b'in_vert')
            in_texcoord_loc = _gl.glGetAttribLocation(self._prog, b'in_texcoord')

            stride = 4 * 4
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

    def resizeGL(self, width: int, height: int) -> None:
        """Handle widget resize."""
        if self._view_transform.mode == "fit":
            self.fit_to_window()

    def resizeEvent(self, event: Any) -> None:
        """Keep the FPS readout in the preview's top-right corner."""
        super().resizeEvent(event)
        self._fps_indicator.adjustSize()
        self._fps_indicator.move(
            max(8, self.width() - self._fps_indicator.width() - 8), 8)

    def set_playback_state(self, is_playing: bool, target_fps: float = 24.0) -> None:
        """Update the source preview FPS indicator state."""
        self._fps_indicator.set_playback_state(is_playing, target_fps)

    def paintGL(self) -> None:
        """Render the viewer content.

        INT-14：绘制链路（含 CPU 暂存上传与纹理绘制）任何异常都不允许
        以未捕获 Qt 槽异常终止进程。首个失败记录一次 traceback 并进入
        明确错误态（后续帧只清屏占位），直到下一次 initializeGL 成功；
        不用宽泛 except 继续使用半初始化的 GL 状态。
        """
        if not self._gl_initialized:
            return
        if self._gl_paint_failed:
            # 错误占位：保持可用（清屏），不再执行绘制路径
            try:
                _gl.glClear(GL_COLOR_BUFFER_BIT)
            except Exception:
                pass
            return
        try:
            self._paintGL_content()
        except Exception:
            self._handle_paint_failure()

    def _paintGL_content(self) -> None:
        """Actual paint pipeline; exceptions are contained by paintGL."""

        # Determine which texture to render: shared (GPU pipeline) or uploaded
        # (CPU fallback).  Ownership rule (AUD-20900/AUD-20901): the shared
        # texture belongs to the render thread's OutputSlotPool and is only
        # referenced read-only via _shared_tex_id.  It is NEVER copied into
        # _current_tex_id — that field is reserved for viewer-owned textures,
        # because clear_texture deletes _current_tex_id and the CPU fallback
        # upload paths write into it in place.
        if self._shared_tex_id is not None:
            # GPU pipeline: shared texture from render thread (no upload needed)
            self._tex_width = self._shared_tex_width
            self._tex_height = self._shared_tex_height
        else:
            # CPU fallback: upload the staged QImage inside the GL paint
            # callback, where the context is guaranteed current (AUD-20902:
            # 上传曾在 UI 槽内无 makeCurrent 直调，PyOpenGL 无当前上下文
            # 时抛 GLError，表现为永久黑帧或驱动相关未定义行为).
            pending = self._pending_qimage
            if pending is not None:
                if not pending.isNull():
                    display_size = self._pending_display_size
                    if display_size is not None:
                        self.set_texture_from_qimage_with_display_size(
                            pending, display_size[0], display_size[1]
                        )
                    else:
                        self.set_texture_from_qimage(pending)
                self._pending_qimage = None
                self._pending_display_size = None

        # Clear background
        _gl.glClear(GL_COLOR_BUFFER_BIT)

        w = self.width()
        h = self.height()

        if w <= 0 or h <= 0:
            return

        # Calculate display rect preserving aspect ratio
        display_rect = self._calculate_display_rect()

        # Render video texture if available (shared pool texture referenced
        # read-only, or the viewer-owned uploaded texture)
        if self._display_tex_id() is not None and self._prog:
            self._render_texture(display_rect)
            frame = getattr(self.parentWidget(), "_current_frame", None)
            self._fps_indicator.record_presented_frame(frame)

    def _display_tex_id(self) -> Optional[int]:
        """Resolve which texture ID to draw this frame.

        The render-thread shared texture (OutputSlotPool) takes precedence and
        is referenced read-only; otherwise fall back to the viewer-owned
        texture uploaded by the CPU path.  This keeps ownership separated:
        ``_shared_tex_id`` is pool-owned (never deleted/written by this
        viewer), ``_current_tex_id`` is viewer-owned.
        """
        if self._shared_tex_id is not None:
            return self._shared_tex_id
        return self._current_tex_id

    def _calculate_display_rect(self) -> Tuple[float, float, float, float]:
        """Calculate display rectangle preserving aspect ratio."""
        w = self.width()
        h = self.height()

        if w <= 0 or h <= 0:
            return (-1.0, -1.0, 2.0, 2.0)

        # Use texture dimensions if available, otherwise use aspect ratio
        tex_w = self._tex_width if self._tex_width > 0 else int(self._video_aspect_ratio * h)
        tex_h = self._tex_height if self._tex_height > 0 else h

        return self._view_transform.display_rect_ndc(tex_w, tex_h, w, h)

    def _render_texture(self, display_rect: Tuple[float, float, float, float]) -> None:
        """Render the texture to the display rect."""
        x, y, width, height = display_rect

        # Validate texture ID before use.  Never delete here: the shared
        # texture is owned by the render-thread pool (deferred release via
        # slot ack); an invalid viewer-owned ID just gets dereferenced.
        tex_id = self._display_tex_id()
        if tex_id is None or tex_id == 0:
            return
        if not _gl.glIsTexture(tex_id):
            logger.warning(f"SourceViewer: texture ID {tex_id} is invalid, clearing")
            if self._shared_tex_id == tex_id:
                self._shared_tex_id = None
            elif self._current_tex_id == tex_id:
                self._current_tex_id = None
            return

        # GPU FBO textures have GL orientation (y=0 = bottom of image).
        # CPU-uploaded QImages are flipped by glTexImage2D (y=0 = top of image),
        # so texcoords need to be inverted depending on the source — AUD-20904:
        # this flip is now a shader uniform (u_flip_y) instead of rewritten
        # per-frame vertex texcoords.

        # Use shader program
        _gl.glUseProgram(self._prog)

        # Bind texture
        _gl.glActiveTexture(GL_TEXTURE0)
        _gl.glBindTexture(GL_TEXTURE_2D, tex_id)

        # AUD-20904: set cached uniforms instead of re-resolving locations and
        # re-uploading vertex data every frame.  The VBO stays a static unit
        # quad; the display rect and texcoord y-flip are uniforms now.
        # (A location of -1 means the uniform was optimised out; glUniform*
        # silently ignores it per the GL spec.)
        _gl.glUniform1i(self._u_texture_loc, 0)
        _gl.glUniform4f(self._u_display_rect_loc, x, y, width, height)
        # Static unit quad carries v=1 at the bottom / v=0 at the top, which
        # matches the CPU QImage orientation; GPU FBO textures need the flip.
        _gl.glUniform1f(
            self._u_flip_y_loc,
            1.0 if self._shared_tex_id is not None else 0.0,
        )

        # Bind VAO (static unit quad) and draw
        _gl.glBindVertexArray(self._vao)
        _gl.glDrawArrays(GL_TRIANGLE_STRIP, 0, 4)

        # Cleanup
        _gl.glBindVertexArray(0)
        _gl.glUseProgram(0)

    # ==================== Texture Management ====================

    def submit_qimage(
        self,
        qimage: QImage,
        display_width: int = 0,
        display_height: int = 0,
    ) -> None:
        """Stage a CPU-decoded frame for deferred upload inside paintGL.

        AUD-20902: 上传方法此前由 UI 槽（_load_frame/解码回调/watchdog
        回退）在 paintGL/initializeGL/resizeGL 之外直接调用且全程无
        makeCurrent——Qt 文档规定此情形必须先确保上下文 current；无当前
        上下文时 glTexImage2D 抛 GLError（被上层捕获记为
        cpu_decode_error，表现为永久黑帧）或按驱动不同产生未定义行为。
        UI 槽现在只调用本方法暂存 QImage（使 _pending_qimage 恢复为活
        机制），实际 glGen/glTexImage/glTexSubImage 全部发生在 paintGL
        内（参照 program_viewer 的 pending 上传模式）。宽高比与 fit
        计算不依赖 GL，仍在提交时完成，下一次 paint 即用正确显示矩形。

        Args:
            qimage: Decoded frame (RGB888 或 RGBA8888).
            display_width: 媒体显示宽（0 表示用图像自身宽）.
            display_height: 媒体显示高（0 表示用图像自身高）.
        """
        if qimage.isNull():
            return
        self._pending_qimage = qimage
        if display_width > 0 and display_height > 0:
            self._pending_display_size = (display_width, display_height)
            # 显示比来自媒体元数据，与代理解码尺寸无关
            self._video_aspect_ratio = display_width / display_height
        else:
            self._pending_display_size = None
        # CPU 路径接管显示，直到下一帧 GPU 共享纹理到达
        # （set_shared_texture 会清掉 pending）。
        self._shared_tex_id = None
        if self._view_transform.mode == "fit":
            self.fit_to_window()
        self.update()

    def set_texture(self, tex_id: int, width: int, height: int) -> None:
        """Set a viewer-owned texture for display (CPU-side upload path).

        Ownership contract: ``tex_id`` must be a texture created for (or
        handed over to) this viewer — it is stored in ``_current_tex_id`` and
        deleted by ``clear_texture``.  Render-thread pool textures must go
        through ``set_shared_texture`` instead; they are referenced read-only
        and never deleted by this widget.
        """
        self._current_tex_id = tex_id
        self._tex_width = width
        self._tex_height = height
        self._shared_tex_id = None  # Clear shared texture

        # DON'T update aspect ratio from texture dimensions — it comes from metadata.
        # Only set as fallback if no metadata-based ratio is available.
        if width > 0 and height > 0 and self._video_aspect_ratio <= 0:
            self._video_aspect_ratio = width / height

        # Auto-fit if mode is still "fit"
        if self._view_transform.mode == "fit":
            self.fit_to_window()

        self.update()

    def set_shared_texture(self, tex_id: int, width: int, height: int) -> None:
        """
        Set a shared GPU texture for display (GPU pipeline path).

        The texture was created in the shared GL context of the render thread.
        No upload needed — just reference the texture ID directly.

        NOTE: Aspect ratio is NOT updated here. The display aspect should come
        from media metadata, not texture dimensions. The parent widget sets
        _video_aspect_ratio from metadata when opening a source.

        Args:
            tex_id: OpenGL texture ID from shared context
            width: Texture width
            height: Texture height
        """
        if tex_id is None or tex_id == 0:
            logger.warning(f"SourceViewer: rejecting invalid shared texture ID {tex_id}")
            return
        self._shared_tex_id = tex_id
        self._shared_tex_width = width
        self._shared_tex_height = height
        self._pending_qimage = None  # Clear CPU path
        self._pending_display_size = None

        # Use texture dimensions for sampling, but DON'T update aspect ratio.
        # Aspect ratio comes from media metadata via the parent widget.
        if width > 0 and height > 0 and self._video_aspect_ratio <= 0:
            # Only set aspect ratio if it hasn't been set from metadata yet
            self._video_aspect_ratio = width / height

        # Auto-fit if mode is still "fit"
        if self._view_transform.mode == "fit":
            self.fit_to_window()

        self.update()

    def set_texture_from_qimage(self, qimage: QImage) -> None:
        """Create texture from QImage and display it.

        GL-context contract (AUD-20902): 本方法执行 glGen/glTexImage，
        必须在上下文 current 时调用（paintGL 内）。UI 槽请改用
        submit_qimage 暂存图像，由 paintGL 消费。
        """
        if not self._gl_initialized or qimage.isNull():
            return

        # Convert QImage to RGBA format if needed
        if qimage.format() != QImage.Format.Format_RGBA8888:
            qimage = qimage.convertToFormat(QImage.Format.Format_RGBA8888)

        width = qimage.width()
        height = qimage.height()

        # Get image data as numpy array
        # PyOpenGL 的 glTexImage2D 需要 numpy array 或 bytes，不接受 ctypes.c_void_p
        ptr = qimage.bits()
        ptr.setsize(width * height * 4)
        pixel_data = np.frombuffer(ptr, dtype=np.uint8, count=width * height * 4).reshape(height, width, 4)
        pixel_data = np.ascontiguousarray(pixel_data)

        # Create or update texture.  Only a viewer-owned texture may be
        # (re)defined here: if _current_tex_id somehow still aliases the
        # render-thread pool texture, allocate a fresh viewer texture instead
        # of glTexImage2D-redefining the pool slot (AUD-20901).
        if self._current_tex_id is None or self._current_tex_id == self._shared_tex_id:
            self._current_tex_id = _gl.glGenTextures(1)

        _gl.glBindTexture(GL_TEXTURE_2D, self._current_tex_id)
        _gl.glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR)
        _gl.glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR)
        _gl.glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE)
        _gl.glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE)
        _gl.glTexImage2D(
            GL_TEXTURE_2D, 0, GL_RGBA,
            width, height, 0,
            GL_RGBA, GL_UNSIGNED_BYTE,
            pixel_data
        )
        _gl.glBindTexture(GL_TEXTURE_2D, 0)

        self._tex_width = width
        self._tex_height = height
        self.update()

    def set_texture_from_qimage_with_display_size(
        self,
        qimage: QImage,
        display_width: int,
        display_height: int,
    ) -> None:
        """
        Upload a QImage as texture but use display_width/display_height for aspect ratio.

        This prevents the CPU fallback path from corrupting the display aspect ratio
        with proxy decode dimensions. The display ratio should always come from media
        metadata, not from the decoded proxy size.

        Uses glTexSubImage2D for texture reuse when dimensions match,
        avoiding costly glTexImage2D reallocation every frame.

        GL-context contract (AUD-20902): 同 set_texture_from_qimage，仅
        在 paintGL（上下文 current）内调用；UI 槽用 submit_qimage。

        Args:
            qimage: Source image
            display_width: Media's display width (from metadata)
            display_height: Media's display height (from metadata)
        """
        # Upload QImage to GPU texture (same as set_texture_from_qimage)
        if not self._gl_initialized or qimage.isNull():
            return

        if qimage.format() != QImage.Format.Format_RGBA8888:
            qimage = qimage.convertToFormat(QImage.Format.Format_RGBA8888)

        width = qimage.width()
        height = qimage.height()

        ptr = qimage.bits()
        ptr.setsize(width * height * 4)
        pixel_data = np.frombuffer(ptr, dtype=np.uint8, count=width * height * 4).reshape(height, width, 4)
        pixel_data = np.ascontiguousarray(pixel_data)

        # Reuse existing texture with glTexSubImage2D when dimensions match.
        # The reuse target must be a texture this viewer owns: the shared
        # render-thread pool texture is displayed read-only and is never
        # written in place, even when its dimensions happen to match the
        # CPU-decoded frame (AUD-20901).
        size_matches = (
            self._current_tex_id is not None
            and self._current_tex_id != self._shared_tex_id
            and self._tex_width == width
            and self._tex_height == height
        )

        if size_matches:
            _gl.glBindTexture(GL_TEXTURE_2D, self._current_tex_id)
            _gl.glTexSubImage2D(
                GL_TEXTURE_2D, 0,
                0, 0,
                width, height,
                GL_RGBA, GL_UNSIGNED_BYTE,
                pixel_data
            )
        else:
            if self._current_tex_id is None or self._current_tex_id == self._shared_tex_id:
                self._current_tex_id = _gl.glGenTextures(1)

            _gl.glBindTexture(GL_TEXTURE_2D, self._current_tex_id)
            _gl.glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR)
            _gl.glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR)
            _gl.glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE)
            _gl.glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE)
            _gl.glTexImage2D(
                GL_TEXTURE_2D, 0, GL_RGBA,
                width, height, 0,
                GL_RGBA, GL_UNSIGNED_BYTE,
                pixel_data
            )
            self._tex_width = width
            self._tex_height = height

        _gl.glBindTexture(GL_TEXTURE_2D, 0)

        # Use TEXTURE dimensions for tex_width/tex_height (for UV sampling)
        self._tex_width = width
        self._tex_height = height

        # Use MEDIA DISPLAY dimensions for aspect ratio (for display rect fitting)
        # This is the key fix: display ratio comes from metadata, not proxy size
        if display_width > 0 and display_height > 0:
            self._video_aspect_ratio = display_width / display_height

        # Clear shared texture reference
        self._shared_tex_id = None

        # Auto-fit if mode is still "fit"
        if self._view_transform.mode == "fit":
            self.fit_to_window()

        self.update()

    def clear_texture(self) -> None:
        """Clear the viewer-owned texture and shared texture state.

        Deferred-release ownership rule (AUD-20900): only a texture this
        viewer created (_current_tex_id) is deleted here.  The shared texture
        (_shared_tex_id) belongs to the render thread's OutputSlotPool and is
        merely dereferenced — the UI thread never deletes pool textures;
        releasing them back to the pool goes through the slot-ack callback.

        AUD-20902: 本方法经 close_source/素材切换在 paint 回调之外调用，
        删除前必须 makeCurrent（Qt 契约：三个 paint 回调之外使用 GL 需
        自行确保上下文 current）。
        """
        if self._current_tex_id is not None:
            # Defensive: never delete an ID that still aliases the shared
            # pool texture (should not happen — see paintGL ownership rule).
            if self._current_tex_id != self._shared_tex_id:
                self._delete_viewer_texture(self._current_tex_id)
            self._current_tex_id = None

        # Clear shared texture reference (do NOT delete — owned by render thread)
        self._shared_tex_id = None
        self._shared_tex_width = 0
        self._shared_tex_height = 0

        self._tex_width = 0
        self._tex_height = 0
        self._pending_qimage = None
        self._pending_display_size = None
        self.update()

    def _delete_viewer_texture(self, tex_id: int) -> None:
        """Delete a viewer-owned texture with the GL context made current.

        部件关闭中（C++ 对象已销毁）时跳过：Qt 侧上下文销毁会连同纹理
        一起释放，强行删除只会得到 RuntimeError/无上下文 GLError。
        """
        if not self._gl_initialized or tex_id is None or tex_id == 0:
            return
        made_current = False
        try:
            self.makeCurrent()
            made_current = True
            _gl.glDeleteTextures([tex_id])
        except RuntimeError:
            logger.debug(
                "SourceViewer: skip texture delete, widget already destroyed"
            )
        except GLError as e:
            logger.warning(f"SourceViewer: texture delete failed: {e}")
        finally:
            if made_current:
                try:
                    self.doneCurrent()
                except RuntimeError:
                    pass

    # ==================== Zoom/Pan ====================

    def fit_to_window(self) -> None:
        """Fit video to window (reset zoom and pan)."""
        self._view_transform.reset_fit()
        self.zoom_changed.emit(self._view_transform.zoom)
        self.update()

    def actual_size(self) -> None:
        """Set to actual pixel size (1:1)."""
        w = self.width()
        h = self.height()
        tex_w = self._tex_width if self._tex_width > 0 else int(self._video_aspect_ratio * h)
        tex_h = self._tex_height if self._tex_height > 0 else h
        self._view_transform.set_actual_size(tex_w, tex_h, w, h)
        self.zoom_changed.emit(self._view_transform.zoom)
        self.update()

    # ==================== Mouse Events ====================

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

    def mousePressEvent(self, event: QMouseEvent) -> None:
        """Handle mouse press."""
        # Source In/Out shortcuts are local to this viewer.  Claim focus on
        # every click so I/O always operate on the media currently displayed.
        self.setFocus(Qt.FocusReason.MouseFocusReason)
        if event.button() == Qt.MouseButton.MiddleButton:
            self._is_panning = True
            self._pan_start_pos = event.pos()
            self._pan_start_px = (self._view_transform.pan_x_px, self._view_transform.pan_y_px)
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        """Handle mouse move."""
        if self._is_panning:
            dx = event.pos().x() - self._pan_start_pos.x()
            dy = event.pos().y() - self._pan_start_pos.y()

            self._view_transform.pan_x_px = self._pan_start_px[0] + dx
            self._view_transform.pan_y_px = self._pan_start_px[1] + dy
            self._view_transform.mode = "manual"

            self.update()
            event.accept()
        else:
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        """Handle mouse release."""
        if event.button() == Qt.MouseButton.MiddleButton:
            self._is_panning = False
            self.setCursor(Qt.CursorShape.ArrowCursor)
            event.accept()
        else:
            super().mouseReleaseEvent(event)


# ================================================================
# SourceViewerWidget - Main container widget
# ================================================================

def _open_cpu_decode_source(path: str, target_size: Optional[tuple[int, int]]):
    """创建 CPU 回退解码器：Topos 素材经 factory 路由，其余走 FFmpeg。

    Topos 私有编码（TPIC）没有 FFmpeg 解码器，硬编码 FFmpegMediaSource
    会 open 失败并让 Source Viewer 黑屏；经 factory topos 后端得到
    planar uint16 帧，再由 _decoded_frame_to_rgb24 转 8-bit 显示。

    AUD-20903: 该函数只应在后台解码线程内调用（FFmpeg/Topos 解码器句柄
    非线程安全，由 _CpuFallbackDecodeWorker 独占）。
    """
    from src.shared.media.topos_source import is_topos_movie

    if is_topos_movie(path):
        from src.shared.media.factory import MediaSourceFactory

        source = MediaSourceFactory.create(
            path, backend='topos', target_size=target_size,
        )
        source.open()
        return source

    from src.shared.media.ffmpeg_source import FFmpegMediaSource

    decoder = FFmpegMediaSource(
        path,
        target_size=target_size,
        output_mode="packed_rgb",
        output_format="rgb24",
    )
    decoder.open()
    return decoder


def _decoded_frame_to_rgb24(decoded) -> Optional[np.ndarray]:
    """解码帧 → 连续 RGB24 数组（packed 直通；planar 经显示助手转换）。"""
    if decoded is None:
        return None
    data = getattr(decoded, "data", None)
    if data is not None:
        return data
    from src.shared.media.topos_source import topos_frame_to_rgb

    rgb = topos_frame_to_rgb(decoded)
    return rgb


class _CpuFallbackDecodeSignals(QObject):
    """AUD-20903: 后台 CPU 解码线程 → UI 线程的排队信号载体。"""

    frame_ready = pyqtSignal(int, QImage, float, int)  # frame, image, decode_ms, generation
    decode_failed = pyqtSignal(int, str, int)          # frame, error, generation


class _CpuFallbackDecodeWorker:
    """AUD-20903: CPU 回退解码在后台线程执行。

    read_frame 的关键帧 seek + 前向解码在长 GOP 大分辨率素材上单帧可达
    数百毫秒，此前在播放 tick / scrub / GPU watchdog 里同步执行会直接
    阻塞 UI 事件循环。本 worker 独占 CPU 解码器，在后台线程完成解码，
    把 QImage 经排队信号送回 UI 线程，UI 侧只保留上传与缓存（上传走
    paintGL 延迟通道，见 AUD-20902）。

    线程模型与 file_controller._SourceViewerDecodeWorker 相同：daemon
    threading.Thread + QObject 信号（跨线程自动排队投递到 UI 线程），
    latest-wins 合并请求，播放时不会按帧堆积队列。
    """

    _CLOSE = object()  # 关闭解码器的哨兵请求

    def __init__(self, owner: Optional[QObject] = None) -> None:
        # AUD-20910 同型教训：signals QObject 不挂 owner 父对象——owner
        # （widget）先于后台线程销毁时，已删除的 QObject 上 emit 会抛
        # RuntimeError；无父对象时由 worker 持有 Python 引用，接收方销毁
        # 由 Qt 自动断开连接（emit 变 no-op）。
        self._signals = _CpuFallbackDecodeSignals()
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        # (asset_path, frame, generation, target_size) 或 _CLOSE
        self._request: Optional[tuple] = None
        self._running = False
        self._thread: Optional[threading.Thread] = None
        # 解码器只能在本线程内创建/关闭；key 变化时重建。
        self._decoder: Optional[Any] = None
        self._decoder_key: Optional[tuple] = None

    @property
    def signals(self) -> "_CpuFallbackDecodeSignals":
        return self._signals

    def start(self) -> None:
        with self._cond:
            if self._running:
                return
            self._running = True
        self._thread = threading.Thread(
            target=self._run,
            daemon=True,
            name="SourceViewerCpuFallback",
        )
        self._thread.start()

    def stop(self) -> None:
        """请求线程退出（不 join、不动解码器句柄）。

        解码器（FFmpeg/Topos 句柄）只能在其创建线程内关闭，因此 run 循环
        在感知到停止后自行 dispose；UI 线程只翻转标志并唤醒。
        """
        with self._cond:
            self._running = False
            self._request = None
            self._cond.notify()

    def request(
        self,
        asset_path: str,
        frame: int,
        generation: int,
        target_size: Optional[tuple[int, int]],
    ) -> None:
        """提交解码请求（latest-wins：只保留最新一条）。"""
        with self._cond:
            if not self._running:
                return
            self._request = (asset_path, int(frame), int(generation), target_size)
            self._cond.notify()

    def close_decoder(self) -> None:
        """请求丢弃当前解码器（源切换/关闭时由 UI 线程调用）。"""
        with self._cond:
            if not self._running:
                return
            self._request = self._CLOSE
            self._cond.notify()

    def _run(self) -> None:
        try:
            while True:
                with self._cond:
                    while self._running and self._request is None:
                        self._cond.wait(timeout=1.0)
                    if not self._running:
                        return
                    request = self._request
                    self._request = None
                if request is self._CLOSE:
                    self._dispose_decoder()
                    continue
                asset_path, frame, generation, target_size = request
                key = (asset_path, target_size)
                if self._decoder_key != key:
                    self._dispose_decoder()
                    try:
                        self._decoder = _open_cpu_decode_source(asset_path, target_size)
                        self._decoder_key = key
                    except Exception as exc:
                        logger.debug(
                            f"SourceViewerWidget: CPU fallback decoder open failed: {exc}"
                        )
                        self._signals.decode_failed.emit(frame, str(exc), generation)
                        continue
                decoder = self._decoder
                if decoder is None:
                    self._signals.decode_failed.emit(frame, "no_decoder", generation)
                    continue
                decode_t0 = time.perf_counter()
                try:
                    decoded = decoder.read_frame(frame)
                    frame_data = _decoded_frame_to_rgb24(decoded)
                    if frame_data is None:
                        self._signals.decode_failed.emit(frame, "empty_frame", generation)
                        continue
                    frame_data = np.ascontiguousarray(frame_data)
                    height, width = frame_data.shape[:2]
                    qimage = QImage(
                        frame_data.data,
                        int(width),
                        int(height),
                        int(3 * width),
                        QImage.Format.Format_RGB888
                    ).copy()  # Copy to ensure data ownership
                    decode_ms = (time.perf_counter() - decode_t0) * 1000.0
                    self._signals.frame_ready.emit(frame, qimage, decode_ms, generation)
                except Exception as exc:
                    self._signals.decode_failed.emit(frame, str(exc), generation)
        finally:
            # 解码器句柄只能在本线程内关闭（stop() 不触碰）
            self._dispose_decoder()

    def _dispose_decoder(self) -> None:
        decoder = self._decoder
        self._decoder = None
        self._decoder_key = None
        if decoder is not None:
            try:
                decoder.close()
            except Exception as exc:
                logger.debug(f"SourceViewerWidget: CPU fallback decoder close failed: {exc}")


def _release_displayed_slot_on_destroy(state: dict) -> None:
    """VP-B02: 归还 widget 销毁时仍在显示的 present slot。

    ``destroyed`` 在 C++ 析构期发出，处理器不得触碰 widget 自身（同
    AUD-20910 教训）：只读普通 dict 里的渲染器回调与最新 slot id。
    """
    ack = state.get("ack")
    slot_id = state.get("slot_id", -1)
    if ack is None or slot_id is None or slot_id < 0:
        return
    try:
        ack(int(slot_id))
    except Exception:
        pass  # 渲染器可能已先行释放（迟到 ack 属已知竞态）


class SourceViewerWidget(QWidget):
    """
    Main Source Viewer container widget.

    Contains:
    - SourceViewer (OpenGL viewport) — top
    - SourceSlider (timeline scrubber)
    - SourceViewerBar (bottom toolbar — playback controls, timecode, edit buttons)

    Attributes:
        current_asset: Current MediaAsset being previewed
        current_frame: Current frame number
        in_point: Mark In frame number
        out_point: Mark Out frame number
        total_frames: Total frames in source
        fps: Source frame rate
        is_playing: Playing state

    Signals:
        in_point_changed: In point changed (frame)
        out_point_changed: Out point changed (frame)
        insert_requested: Insert edit requested (asset_id, in_frame, out_frame)
        overwrite_requested: Overwrite edit requested (asset_id, in_frame, out_frame)
        append_requested: Append edit requested (asset_id, in_frame, out_frame)
    """

    # Public signals
    # E22: source_opened/source_closed 已删除（全仓 0 connect；打开/关闭
    # 状态已由 range_changed + EditPage 的按钮同步覆盖）。
    in_point_changed = pyqtSignal(int)  # frame
    out_point_changed = pyqtSignal(int)  # frame
    # FE-B05：统一源范围状态信号。open/clear/close/set 任一端点变化都
    # 发 (in_point, out_point)（None 表示未设），EditPage 只依赖本信号
    # 同步按钮状态，不再拼接两个 changed 信号推断。
    range_changed = pyqtSignal(object, object)
    insert_requested = pyqtSignal(str, int, int)  # asset_id, in_frame, out_frame
    overwrite_requested = pyqtSignal(str, int, int)  # asset_id, in_frame, out_frame
    append_requested = pyqtSignal(str, int, int)  # asset_id, in_frame, out_frame
    # F2.1: Source Patch 设置入口（当前素材 V/A 流开关与源声道映射）
    source_patch_requested = pyqtSignal()

    # How many frames behind the expected frame an async GPU result may be
    # and still get displayed (covers background-decode round-trip latency).
    _GPU_STALE_WINDOW: int = 12

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("SourceViewerWidget")
        self.setStyleSheet("QWidget#SourceViewerWidget { background-color: #1a1a1c; }")
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        # State
        self._current_asset: Optional[MediaAsset] = None
        self._current_frame: int = 0
        self._in_point: Optional[int] = None
        self._out_point: Optional[int] = None
        self._total_frames: int = 0
        self._fps: float = 24.0
        self._is_playing: bool = False

        # Playback timer with monotonic clock for accurate frame pacing
        self._playback_timer = QTimer(self)
        self._playback_timer.setInterval(16)  # ~60Hz tick for smooth frame pacing
        self._playback_timer.timeout.connect(self._on_playback_tick)

        # Monotonic clock for drop-frame chase during playback
        self._playback_start_time: float = 0.0
        self._playback_start_frame: int = 0

        # Generation counter for GPU result validation (prevents stale results
        # after seek/asset-change from overwriting current frame)
        self._gpu_generation: int = 0
        self._gpu_expected_frame: int = -1

        # Present-slot ack protocol: the GPU renderer's output textures live
        # in an OutputSlotPool whose invariant is "an unacked slot is never
        # reused".  We track the slot currently on display and ack it via
        # _gpu_slot_ack_callback once it is superseded (or on close); results
        # discarded by the guards are acked immediately.  Without this the
        # pool exhausts after one full pass of slots and playback freezes.
        self._gpu_displayed_slot_id: int = -1
        self._gpu_slot_ack_callback: Optional[Any] = None  # Callable[[int], None]

        # VP-B02: hidden-view policy + slot lifecycle diagnostics.  The
        # visibility revision is deliberately separate from the media
        # generation (_gpu_generation): repeated show/hide must never
        # invalidate the current asset's in-flight results.
        self._view_hidden: bool = False
        self._view_visibility_revision: int = 0
        self._gpu_stats: dict[str, int] = {
            "decode_submitted": 0,
            "display_accepted": 0,
            "results_discarded": 0,
            "slots_acked": 0,
        }
        # destroyed 回调不得触碰 widget：slot 归还状态放普通 dict，
        # 只携带渲染器回调与最新显示 slot id（VP-B02 销毁兜底）。
        self._slot_release_state: dict = {"slot_id": -1, "ack": None}

        # VP-B11: shutdown token — once set, play/tick decode submits,
        # watchdog fallbacks, CPU fallback requests and results are all
        # rejected (no new requests after the shutdown sequence starts).
        self._shutdown_prepared: bool = False

        # GPU timeout watchdog: if GPU async result doesn't arrive within
        # _gpu_timeout_ms, fall back to CPU decode for that frame
        self._gpu_timeout_ms: int = 100  # 100ms timeout
        self._gpu_watchdog_timer: QTimer = QTimer(self)
        self._gpu_watchdog_timer.setSingleShot(True)
        self._gpu_watchdog_timer.timeout.connect(self._on_gpu_timeout)
        self._gpu_pending_frame: int = -1  # frame waiting for GPU result

        # Playback backpressure (mirrors timeline's
        # should_defer_playback_tick).  _gpu_pending_frame already acts as
        # the in-flight flag; this constant is the lag tolerance before we
        # reanchor the clock to the visible frame so the playhead waits for
        # the in-flight decode instead of racing ahead of it.
        self._PLAYBACK_DEFER_LAG_FRAMES: int = 3

        # Last good frame: keep the last successfully displayed frame so we
        # can show it instead of a blank/blue screen when decode fails
        self._last_good_qimage: Optional[QImage] = None
        self._last_good_frame: int = -1

        # Error overlay state
        self._error_message: Optional[str] = None

        # Video decoder (for frame extraction)
        # AUD-20903: CPU 回退解码在后台线程执行（解码器由 worker 独占），
        # UI 线程只接收 QImage 并经 paintGL 延迟通道上传；同步 read_frame
        # 曾在播放 tick / scrub / watchdog 里直接阻塞事件循环。
        self._cpu_worker = _CpuFallbackDecodeWorker()
        self._cpu_worker.signals.frame_ready.connect(self._on_cpu_frame_ready)
        self._cpu_worker.signals.decode_failed.connect(self._on_cpu_decode_failed)
        self._cpu_worker.start()
        # AUD-20910 同型教训：widget 销毁时停止后台线程，避免悬挂解码
        # 线程在已删除对象上继续 emit（destroyed 在 C++ 析构期发出，处理
        # 器不得触碰 widget 自身，只捕获 worker）。
        _cpu_worker_ref = self._cpu_worker
        self.destroyed.connect(lambda _obj=None: _cpu_worker_ref.stop())
        # VP-B02: widget 销毁时归还仍在显示的 present slot（池不变量：
        # 未 ack 的 slot 永不复用）；处理器只读普通 dict，不碰 widget。
        _slot_release_state = self._slot_release_state
        self.destroyed.connect(
            lambda _obj=None: _release_displayed_slot_on_destroy(
                _slot_release_state))

        # GPU frame provider callback: callable(asset_path, frame) -> (tex_id, w, h) | None
        # When set, the Source Viewer will attempt GPU path first before CPU fallback.
        self._gpu_frame_provider: Optional[Any] = None

        # Frame cache (LRU cache using OrderedDict)
        self._frame_cache: dict[int, QImage] = {}
        self._cache_order: list[int] = []  # LRU order list
        self._cache_size: int = 30  # Increased from 10 for smoother playback

        # Media display size from metadata (not from viewer dimensions)
        self._media_display_width: Optional[int] = None
        self._media_display_height: Optional[int] = None

        # Lightweight scrub preview state.  During slider drags we chase only
        # the latest requested frame and keep GPU generation stable; final
        # release still performs an exact seek.
        self._scrub_active: bool = False
        self._scrub_pending_frame: Optional[int] = None
        self._scrub_timer_pending: bool = False
        self._scrub_timer_token: int = 0
        self._scrub_last_emit_ms: float = 0.0
        self._scrub_request_interval_ms: int = 8
        self._scrub_audio_last_emit_ms: float = 0.0
        self._scrub_audio_request_interval_ms: int = 60
        self._scrub_audio_preview_duration_ms: int = 72
        self._scrub_audio_manager: Optional[Any] = None
        self._scrub_audio_asset_id: Optional[str] = None
        # F2.1: 素材级源声道映射（open_asset 时随素材传入，None=直通）
        self._scrub_source_channel_map: Optional[tuple] = None
        self._scrub_audio_map_applied: Optional[tuple] = None
        # F2.10: 外录绑定的预听源（None=机内声）与提前量
        self._scrub_audio_source_path: Optional[str] = None
        self._scrub_audio_offset: float = 0.0

        self._setup_ui()
        self._connect_signals()

        # Keep I/O local to the source viewer and every one of its child
        # controls.  The timeline owns the same keys while it has focus; no
        # application- or window-wide shortcut is involved.
        self._mark_in_shortcut: QShortcut = QShortcut(QKeySequence("I"), self)
        self._mark_in_shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self._mark_in_shortcut.activated.connect(self._on_mark_in)
        self._mark_out_shortcut: QShortcut = QShortcut(QKeySequence("O"), self)
        self._mark_out_shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self._mark_out_shortcut.activated.connect(self._on_mark_out)

        logger.debug("SourceViewerWidget initialized")

    def _setup_ui(self) -> None:
        """Set up the UI layout — toolbar at bottom (matches Program Viewer layout)."""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # OpenGL viewer (top)
        self.viewer = SourceViewer(self)
        self.viewer.setMinimumSize(320, 180)
        self.viewer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        layout.addWidget(self.viewer, stretch=1)

        # Slider (for scrubbing)
        self.slider = SourceSlider(self)
        self.slider.setRange(0, 100)
        self.slider.setValue(0)
        self.slider.sliderPressed.connect(self._on_slider_pressed)
        self.slider.sliderReleased.connect(self._on_slider_released)
        self.slider.valueChanged.connect(self._on_slider_changed)
        layout.addWidget(self.slider)

        # Toolbar (bottom — playback controls, timecode, edit buttons)
        self.toolbar = SourceViewerBar(self)
        layout.addWidget(self.toolbar)

    def _connect_signals(self) -> None:
        """Connect toolbar signals."""
        # Mark signals
        self.toolbar.mark_in_requested.connect(self._on_mark_in)
        self.toolbar.mark_out_requested.connect(self._on_mark_out)
        self.toolbar.clear_in_out_requested.connect(self._on_clear_in_out)

        # Transport signals
        self.toolbar.jump_start_requested.connect(self._on_jump_start)
        self.toolbar.prev_frame_requested.connect(self._on_prev_frame)
        self.toolbar.play_pause_requested.connect(self._on_play_pause)
        self.toolbar.next_frame_requested.connect(self._on_next_frame)
        self.toolbar.jump_end_requested.connect(self._on_jump_end)
        # F2.1: Source Patch 设置（转发 EditPage 打开对话框）
        self.toolbar.source_patch_requested.connect(
            self.source_patch_requested.emit)

        # Note: Insert/Overwrite/Append buttons are now in TimelinePanel toolbar.
        # EditPage bridges TimelinePanel button clicks to SourceViewer data.

    # ==================== Asset Management ====================

    @property
    def current_asset(self) -> Optional[MediaAsset]:
        """当前打开的素材（F2.1 Source Patch 对话框等外部入口用）。"""
        return self._current_asset

    def set_source_channel_map(self, channel_map: Optional[tuple]) -> None:
        """F2.1：更新当前素材的源声道映射（scrub 试听下次重建生效）。"""
        self._scrub_source_channel_map = channel_map

    def open_asset(
        self,
        asset: MediaAsset,
        source_channel_map: Optional[tuple] = None,
        audio_source_path: Optional[str] = None,
        audio_offset_seconds: float = 0.0,
    ) -> None:
        """
        Open a media asset for preview.

        Args:
            asset: MediaAsset to preview
            source_channel_map: F2.1 素材级源声道映射（((src, dst), ...) 对，
                None=直通）。供 scrub 试听与时间线播放听到同一声道选择。
            audio_source_path: F2.10 外录绑定的音频源路径（None=机内声）。
            audio_offset_seconds: F2.10 WAV 提前开录量（秒）。
        """
        logger.debug(f"Opening asset: {asset.asset_id} ({asset.display_name})")

        # Close previous asset if not already closed
        if self._current_asset:
            self.close_source()

        self._current_asset = asset
        self._scrub_source_channel_map = source_channel_map
        self._scrub_audio_source_path = audio_source_path
        self._scrub_audio_offset = float(audio_offset_seconds or 0.0)

        # Get asset properties
        self._total_frames = asset.total_frames or 1
        self._fps = asset.fps or 24.0

        # Capture media display size from metadata for correct aspect ratio
        self._media_display_width = asset.width
        self._media_display_height = asset.height

        # Reset playback state
        self._current_frame = 0
        self._in_point = None
        self._out_point = None
        self._is_playing = False

        # Bump generation on asset change to invalidate all pending GPU results
        self._gpu_generation += 1
        self._gpu_watchdog_timer.stop()
        self._gpu_pending_frame = -1
        self._last_good_qimage = None
        self._last_good_frame = -1
        self._error_message = None
        self._scrub_active = False
        self._scrub_pending_frame = None
        self._scrub_timer_pending = False
        self._scrub_audio_last_emit_ms = 0.0
        self._sync_scrub_audio_for_asset()

        # Reset zoom/pan to fit mode for new asset
        self.viewer._view_transform.reset_fit()

        # Update playback timer interval.  The tick runs faster than the frame
        # period; actual frame selection comes from the monotonic clock in
        # _on_playback_tick, which keeps presentation aligned with wall time
        # instead of accumulating QTimer jitter frame after frame.
        if self._fps > 0:
            interval = int(1000 / self._fps)
            self._playback_timer.setInterval(max(8, min(interval, 16)))

        # Update UI
        self.toolbar.set_source_name(asset.display_name)
        self.toolbar.set_enabled(True)
        self.toolbar.update_play_state(False)
        self.viewer.set_playback_state(False, self._fps)
        self.toolbar.update_in_out_state(None, None)

        # Update slider
        self.slider.setRange(0, self._total_frames - 1 if self._total_frames > 1 else 100)
        self.slider.setValue(0)
        self.slider.set_in_out_points(None, None)
        # FE-B05：切源重置范围也要发状态信号，按钮不残留上一素材状态
        self.range_changed.emit(None, None)

        # Update timecode
        timecode = self._frame_to_timecode(0)
        duration_tc = self._frame_to_timecode(self._total_frames - 1)
        self.toolbar.set_timecode(timecode, self._fps)
        self.toolbar.set_frame_number(0)
        self.toolbar.set_duration(duration_tc)

        # Load first frame
        self._load_frame(0)

        # E22: source_opened 信号已删除（无接收者）

        logger.info(f"Source opened: {asset.display_name} ({self._total_frames} frames @ {self._fps} fps)")

    def prepare_for_shutdown(self) -> None:
        """VP-B11: 关闭序列的 widget 侧入口（幂等）。

        对齐计划 B11-1 的顺序：先停止一切新请求（播放/周期 tick/
        watchdog/CPU 回退入口全部关门并撤销会话 generation），再释放
        独占资源（CPU 解码器、显示中的 present slot、scrub 音频线程），
        widget 本体留给宿主销毁。closeEvent/换工程必须**先**调本方法、
        再停 decode worker／GPU renderer——顺序颠倒会让 scrub 音频
        QThread（parent 为本 widget）随窗口析构（abort 风险）且显示
        slot 永不归还（池耗尽）。
        """
        if self._shutdown_prepared:
            return
        self._shutdown_prepared = True

        # 1) 停止新请求：播放/时钟/watchdog + 撤销会话 generation
        self._is_playing = False
        self._playback_timer.stop()
        self._gpu_watchdog_timer.stop()
        self._gpu_pending_frame = -1
        self._gpu_generation += 1  # 在飞结果作废

        # 2) 释放独占资源
        self._cpu_worker.close_decoder()
        self._frame_cache.clear()
        self._cache_order.clear()
        self._ack_gpu_slot(self._gpu_displayed_slot_id)
        self._gpu_displayed_slot_id = -1
        self._slot_release_state["slot_id"] = -1
        self._cleanup_scrub_audio_manager()
        self._last_good_qimage = None
        self._last_good_frame = -1

    def close_source(self) -> None:
        """Close the current source."""
        # Stop playback
        self._playback_timer.stop()
        self._is_playing = False

        # AUD-20903: CPU 解码器由后台 worker 独占，丢弃动作投递给 worker
        # 线程执行（FFmpeg 句柄只能在其创建线程内关闭）。
        self._cpu_worker.close_decoder()

        # Clear frame cache
        self._frame_cache.clear()
        self._cache_order.clear()

        # Clear viewer
        self.viewer.clear_texture()

        # Reset GPU state
        self._gpu_generation += 1
        self._gpu_watchdog_timer.stop()
        self._gpu_pending_frame = -1
        # The displayed slot is no longer referenced by any viewer: release
        # it back to the pool so the next source starts with a full pool.
        self._ack_gpu_slot(self._gpu_displayed_slot_id)
        self._gpu_displayed_slot_id = -1
        self._slot_release_state["slot_id"] = -1
        self._last_good_qimage = None
        self._last_good_frame = -1
        self._error_message = None
        self._scrub_active = False
        self._scrub_pending_frame = None
        self._scrub_timer_pending = False
        self._cleanup_scrub_audio_manager()

        # Reset state
        self._current_asset = None
        self._current_frame = 0
        self._media_display_width = None
        self._media_display_height = None
        self._total_frames = 0
        self._in_point = None
        self._out_point = None

        # Update UI
        self.toolbar.set_source_name("")
        self.toolbar.set_enabled(False)
        self.toolbar.set_timecode("00:00:00:00", 24.0)
        self.toolbar.set_frame_number(0)
        self.toolbar.set_duration("--:--:--:--")
        self.toolbar.update_play_state(False)
        self.viewer.set_playback_state(False, 24.0)
        self.toolbar.update_in_out_state(None, None)

        self.slider.setRange(0, 100)
        self.slider.setValue(0)
        self.slider.set_in_out_points(None, None)

        # E22: source_closed 信号已删除（无接收者）

        # Notify listeners (EditPage) to sync TimelinePanel edit button state
        self.in_point_changed.emit(0)
        self.out_point_changed.emit(0)
        self.range_changed.emit(None, None)

        logger.debug("Source closed")

    # ==================== GPU Frame Provider ====================

    def set_gpu_frame_provider(self, provider: Any) -> None:
        """
        Set a GPU frame provider for zero-copy texture display.

        The provider is called as provider(asset_path, frame_index) and should
        return (tex_id, width, height) or None if GPU path is unavailable.

        Args:
            provider: Callable[[str, int], tuple[int, int, int] | None]
        """
        self._gpu_frame_provider = provider
        logger.info("SourceViewerWidget: GPU frame provider configured")

    def set_gpu_slot_ack_callback(self, callback: Any) -> None:
        """
        Set the callback used to ack a consumed present slot.

        The callback receives a slot_id and is invoked on the UI thread when
        a previously displayed slot has been superseded (or discarded), so
        the GPU renderer's OutputSlotPool can recycle it.

        Args:
            callback: Callable[[int], None] (typically renderer.ack_present_slot)
        """
        self._gpu_slot_ack_callback = callback
        # VP-B02: keep the destroy-path release state in sync (plain dict,
        # readable from the destroyed handler without touching the widget).
        self._slot_release_state["ack"] = callback

    def _ack_gpu_slot(self, slot_id: int) -> None:
        """Ack a present slot if it is valid and an ack callback is set."""
        if slot_id < 0 or self._gpu_slot_ack_callback is None:
            return
        try:
            self._gpu_slot_ack_callback(slot_id)
            self._gpu_stats["slots_acked"] += 1
        except Exception as e:
            logger.debug(f"SourceViewerWidget: slot ack failed for {slot_id}: {e}")

    def set_gpu_frame(self, tex_id: int, width: int, height: int, frame: int = -1,
                      generation: int = -1, slot_id: int = -1) -> bool:
        """
        Directly set a GPU texture for display (called by GPU provider path).

        Includes a generation+frame guard: if generation doesn't match or the
        frame is far behind the currently expected value (set during
        _load_video_frame), the stale GPU result is discarded.  The frame
        check is a trailing window rather than exact equality: decode now
        runs on a background thread, so results routinely arrive one decode
        latency after the request — requiring exact equality would discard
        every frame during playback whenever the round trip approaches a
        frame period, freezing the picture while the playhead keeps moving.

        Present-slot lifecycle: the renderer's output texture belongs to an
        OutputSlotPool that never reuses an unacked slot.  Accepted frames
        ack the slot they supersede; discarded frames ack their own slot
        immediately so the pool cannot exhaust during playback.

        Args:
            tex_id: OpenGL texture ID from shared context
            width: Texture width
            height: Texture height
            frame: Frame index this texture represents (for staleness check)
            generation: Generation counter when this GPU task was submitted
            slot_id: Present pool slot backing this texture (-1 if untracked)

        Returns:
            True if the texture was accepted for display, False if discarded.
        """
        # Generation guard: discard results from previous asset/seek
        if generation >= 0 and generation != self._gpu_generation:
            logger.debug(f"SourceViewerWidget: discarding GPU frame from old generation {generation} (current {self._gpu_generation})")
            self._gpu_stats["results_discarded"] += 1
            self._ack_gpu_slot(slot_id)
            return False

        # Frame guard: allow results up to _GPU_STALE_WINDOW frames behind the
        # expected frame (decode latency), but never ahead of it.  Results
        # further behind come from a superseded playback/scrub position.
        if (
            frame >= 0
            and self._gpu_expected_frame >= 0
            and (frame > self._gpu_expected_frame
                 or frame < self._gpu_expected_frame - self._GPU_STALE_WINDOW)
        ):
            logger.debug(f"SourceViewerWidget: discarding stale GPU frame {frame} (expected {self._gpu_expected_frame})")
            self._gpu_stats["results_discarded"] += 1
            self._ack_gpu_slot(slot_id)
            return False

        # Cancel watchdog — GPU result arrived in time
        self._gpu_watchdog_timer.stop()
        self._gpu_pending_frame = -1

        self.viewer.set_shared_texture(tex_id, width, height)
        self._last_good_frame = frame
        self._error_message = None

        # This slot is now on display; the slot it replaced has been consumed
        # (its content is no longer referenced), so ack it for recycling.
        previous_slot = self._gpu_displayed_slot_id
        self._gpu_displayed_slot_id = slot_id
        self._slot_release_state["slot_id"] = slot_id
        self._gpu_stats["display_accepted"] += 1
        if previous_slot >= 0 and previous_slot != slot_id:
            self._ack_gpu_slot(previous_slot)
        return True

    def hideEvent(self, event) -> None:
        """VP-B02: hidden view policy — stop periodic decode/readback.

        Display-only switch (single/dual layout toggle, dock hide, page
        switch): keep the last presented frame and the playhead; stop
        issuing periodic preview requests and stop the GPU watchdog so a
        hidden view never spins up new (CPU fallback) decodes.  The media
        generation is untouched — visibility has its own revision.
        """
        self._view_hidden = True
        self._view_visibility_revision += 1
        self._gpu_watchdog_timer.stop()
        super().hideEvent(event)

    def showEvent(self, event) -> None:
        """VP-B02: re-shown — exactly one refresh request for the current frame."""
        was_hidden = self._view_hidden
        self._view_hidden = False
        self._view_visibility_revision += 1
        super().showEvent(event)
        if was_hidden and self._current_asset is not None:
            self._load_frame(self._current_frame)

    # ==================== Mark In/Out ====================

    def set_in_point(self, frame: int) -> None:
        """Set the In point."""
        self._in_point = frame
        self.toolbar.update_in_out_state(self._in_point, self._out_point)
        self.slider.set_in_out_points(self._in_point, self._out_point)
        self.in_point_changed.emit(frame)
        self.range_changed.emit(self._in_point, self._out_point)
        logger.debug(f"In point set: {frame}")

    def set_out_point(self, frame: int) -> None:
        """Set the Out point."""
        self._out_point = frame
        self.toolbar.update_in_out_state(self._in_point, self._out_point)
        self.slider.set_in_out_points(self._in_point, self._out_point)
        self.out_point_changed.emit(frame)
        self.range_changed.emit(self._in_point, self._out_point)
        logger.debug(f"Out point set: {frame}")

    def clear_in_out(self) -> None:
        """Clear In/Out points."""
        self._in_point = None
        self._out_point = None
        self.toolbar.update_in_out_state(None, None)
        self.slider.set_in_out_points(None, None)
        # FE-B05：清空必须通知（此前 Clear 后 TimelinePanel 按钮仍启用，
        # 点击只剩无参数 warning 的"假按钮"）
        self.range_changed.emit(None, None)
        logger.debug("In/Out points cleared")

    # ==================== Playback Controls ====================

    def play(self) -> None:
        """Start playback."""
        if self._shutdown_prepared:
            return  # VP-B11: 关闭序列后不再起播
        if not self._current_asset:
            return

        if self._current_frame >= self._total_frames - 1:
            # Restart from beginning if at end
            self._current_frame = 0

        self._is_playing = True
        # Initialize monotonic clock for frame pacing
        import time as _time
        self._playback_start_time = _time.perf_counter()
        self._playback_start_frame = self._current_frame
        self._playback_timer.start()
        self.toolbar.update_play_state(True)
        self.viewer.set_playback_state(True, self._fps)
        # Start continuous audio playback.  The audio manager (built by
        # _sync_scrub_audio_for_asset) is a full AudioPlaybackManager: it
        # already has the asset wired as a track with the right fps/duration,
        # so play(start_frame) drives a continuous mix alongside the video.
        self._start_source_audio(self._current_frame)
        logger.debug("Playback started")

    def pause(self) -> None:
        """Pause playback."""
        self._is_playing = False
        self._playback_timer.stop()
        self.toolbar.update_play_state(False)
        self.viewer.set_playback_state(False, self._fps)
        # Pause continuous audio to match the frozen playhead.
        self._pause_source_audio()
        logger.debug("Playback paused")

    def stop(self) -> None:
        """Stop playback and reset to beginning."""
        self.pause()
        self.seek(0)
        logger.debug("Playback stopped")

    def seek(self, frame: int, *, force: bool = False) -> None:
        """Seek to a specific frame."""
        frame = max(0, min(frame, self._total_frames - 1))
        if frame == self._current_frame and not force:
            return

        # Bump generation on every seek to invalidate pending GPU results
        self._gpu_generation += 1
        self._gpu_watchdog_timer.stop()
        self._gpu_pending_frame = -1

        self._current_frame = frame
        self.slider.setValue(frame)
        self._update_timecode()
        self._load_frame(frame)
        # While playing, keep continuous audio aligned with the new playhead.
        # When paused, the scrub path (_request_scrub_audio_preview) owns the
        # short-blip audio preview, so don't touch the manager here.
        if self._is_playing:
            self._seek_source_audio(frame)
        logger.debug(f"Seeked to frame {frame}")

    # ==================== Signal Handlers ====================

    def _on_mark_in(self) -> None:
        """Handle Mark In request."""
        if self._current_asset:
            self.set_in_point(self._current_frame)

    def _on_mark_out(self) -> None:
        """Handle Mark Out request."""
        if self._current_asset:
            self.set_out_point(self._current_frame)

    def _on_clear_in_out(self) -> None:
        """Handle Clear In/Out request."""
        self.clear_in_out()

    def _on_jump_start(self) -> None:
        """Handle Jump to Start request."""
        self.seek(0)

    def _on_prev_frame(self) -> None:
        """Handle Previous Frame request."""
        if self._current_frame > 0:
            self.seek(self._current_frame - 1)

    def _on_play_pause(self) -> None:
        """Handle Play/Pause request."""
        self.toggle_playback()

    def toggle_playback(self) -> bool:
        """Toggle source playback and report whether a source was available."""
        if self._current_asset is None:
            return False
        if self._is_playing:
            self.pause()
        else:
            self.play()
        return True

    def _on_next_frame(self) -> None:
        """Handle Next Frame request."""
        if self._current_frame < self._total_frames - 1:
            self.seek(self._current_frame + 1)

    def _on_jump_end(self) -> None:
        """Handle Jump to End request."""
        self.seek(self._total_frames - 1)

    def _on_slider_pressed(self) -> None:
        """Start lightweight source scrub preview."""
        if not self._current_asset:
            return
        if self._is_playing:
            self.pause()
        self._scrub_active = True
        self._scrub_pending_frame = None
        self._scrub_timer_pending = False
        self._scrub_last_emit_ms = 0.0
        frame = self._clamp_source_frame(self.slider.value())
        self._request_scrub_video_frame(frame)
        self._request_scrub_audio_preview(frame, force=True)

    def _on_slider_released(self) -> None:
        """Finish scrub preview with one exact seek."""
        if not self._current_asset:
            return
        frame = self._clamp_source_frame(self.slider.value())
        self._scrub_active = False
        self._scrub_pending_frame = None
        self._scrub_timer_pending = False
        self.seek(frame, force=True)

    def _on_slider_changed(self, value: int) -> None:
        """Handle slider value change."""
        if not self._current_asset:
            return
        value = self._clamp_source_frame(value)
        if self._scrub_active or self.slider.isSliderDown():
            self._current_frame = value
            self._update_timecode()
            self._request_scrub_video_frame(value)
            self._request_scrub_audio_preview(value)
        elif value != self._current_frame:
            self.seek(value)

    def _clamp_source_frame(self, frame: int) -> int:
        if self._total_frames <= 0:
            return 0
        return max(0, min(int(frame), self._total_frames - 1))

    def _request_scrub_video_frame(self, frame: int) -> None:
        """Coalesce high-frequency source scrubs to the latest frame."""
        frame = self._clamp_source_frame(frame)
        self._scrub_pending_frame = frame
        now_ms = time.monotonic() * 1000.0
        elapsed_ms = now_ms - self._scrub_last_emit_ms
        if elapsed_ms >= self._scrub_request_interval_ms and not self._scrub_timer_pending:
            self._emit_pending_scrub_frame()
            return
        if self._scrub_timer_pending:
            return
        delay_ms = max(1, int(self._scrub_request_interval_ms - elapsed_ms))
        self._scrub_timer_pending = True
        self._scrub_timer_token += 1
        token = self._scrub_timer_token

        def _flush() -> None:
            if token != self._scrub_timer_token:
                return
            self._scrub_timer_pending = False
            self._emit_pending_scrub_frame()

        QTimer.singleShot(delay_ms, _flush)

    def _emit_pending_scrub_frame(self) -> None:
        frame = self._scrub_pending_frame
        self._scrub_pending_frame = None
        if frame is None or not self._current_asset:
            return
        self._scrub_last_emit_ms = time.monotonic() * 1000.0
        self._preview_scrub_frame(frame)

    def _preview_scrub_frame(self, frame: int) -> None:
        """Display a scrub frame without invalidating every in-flight GPU task."""
        frame = self._clamp_source_frame(frame)
        self._current_frame = frame
        if self.slider.value() != frame:
            was_blocked = self.slider.blockSignals(True)
            self.slider.setValue(frame)
            self.slider.blockSignals(was_blocked)
        self._update_timecode()
        self._load_frame(frame)

    def _sync_scrub_audio_for_asset(self) -> None:
        """Prepare a single-track audio preview graph for the source asset."""
        asset = self._current_asset
        if asset is None or not getattr(asset, "is_video", False):
            self._cleanup_scrub_audio_manager()
            return
        if (
            self._scrub_audio_asset_id == asset.asset_id
            and self._scrub_audio_manager is not None
            and self._scrub_audio_map_applied == self._scrub_source_channel_map
        ):
            return
        self._cleanup_scrub_audio_manager()
        try:
            from pathlib import Path
            from src.features.edit.core.audio_playback import (
                AudioPlaybackManager,
                AudioTrackState,
            )

            manager = AudioPlaybackManager(parent=self)
            manager.set_timeline_fps(float(self._fps or 24.0))
            manager.set_timeline_duration(max(1, int(self._total_frames or 1)))
            # F2.10：外录绑定——预听同源（open_asset 时由 EditPage 解析
            # 传入，与时间线播放/导出同一关系）
            from src.features.edit.core.audio_sync import bound_audio_frame
            scrub_source_start = 0
            if self._scrub_audio_source_path:
                audio_path = self._scrub_audio_source_path
                if self._scrub_audio_offset:
                    scrub_source_start = bound_audio_frame(
                        0, self._scrub_audio_offset, float(self._fps or 24.0))
            else:
                audio_path = asset.path
            ok = manager.add_track(
                "source_preview",
                Path(audio_path),
                AudioTrackState(track_id="source_preview"),
                source_start=scrub_source_start,
                source_fps=float(self._fps or 24.0),
                timeline_start=0,
                timeline_duration=max(1, int(self._total_frames or 1)),
                replace_existing=True,
                source_channel_map=self._scrub_source_channel_map,
            )
            if not ok:
                manager.cleanup()
                return
            self._scrub_audio_manager = manager
            self._scrub_audio_asset_id = asset.asset_id
            self._scrub_audio_map_applied = self._scrub_source_channel_map
        except Exception as exc:
            logger.debug("Source scrub audio preview unavailable: %s", exc)
            self._scrub_audio_manager = None
            self._scrub_audio_asset_id = None
            self._scrub_audio_map_applied = None

    def _cleanup_scrub_audio_manager(self) -> None:
        manager = self._scrub_audio_manager
        self._scrub_audio_manager = None
        self._scrub_audio_asset_id = None
        self._scrub_audio_map_applied = None
        if manager is not None:
            try:
                manager.cleanup()
            except Exception as exc:
                logger.debug("Source scrub audio cleanup failed: %s", exc)

    def _request_scrub_audio_preview(self, frame: int, *, force: bool = False) -> None:
        manager = self._scrub_audio_manager
        if manager is None:
            return
        now_ms = time.monotonic() * 1000.0
        if (
            not force
            and now_ms - self._scrub_audio_last_emit_ms < self._scrub_audio_request_interval_ms
        ):
            return
        self._scrub_audio_last_emit_ms = now_ms
        try:
            manager.preview_scrub_audio(
                self._clamp_source_frame(frame),
                duration_ms=self._scrub_audio_preview_duration_ms,
            )
        except Exception as exc:
            logger.debug("Source scrub audio preview failed: %s", exc)

    def _audio_playback_manager(self) -> Optional[Any]:
        """Return the source-asset audio manager, or None if unavailable.

        The manager is built by ``_sync_scrub_audio_for_asset`` and is a full
        ``AudioPlaybackManager`` (continuous playback + scrub preview share the
        same graph); it is named ``_scrub_audio_manager`` for historical
        reasons.  Centralising the lookup here keeps play/pause/seek readable
        and tolerant of a not-yet-built manager.
        """
        return self._scrub_audio_manager

    def _start_source_audio(self, start_frame: int) -> None:
        """Begin continuous audio playback from ``start_frame`` (no-op if the
        asset has no audio graph or isn't video)."""
        if self._current_asset is None or not getattr(self._current_asset, "is_video", False):
            return
        manager = self._audio_playback_manager()
        if manager is None:
            return
        try:
            manager.play(start_frame=start_frame)
        except Exception as exc:
            logger.debug("Source playback audio start failed: %s", exc)

    def _pause_source_audio(self) -> None:
        """Pause continuous audio playback (no-op if none is active)."""
        manager = self._audio_playback_manager()
        if manager is None:
            return
        try:
            manager.pause()
        except Exception as exc:
            logger.debug("Source playback audio pause failed: %s", exc)

    def _seek_source_audio(self, frame: int) -> None:
        """Reposition continuous audio to ``frame`` during playback."""
        manager = self._audio_playback_manager()
        if manager is None:
            return
        try:
            manager.seek(self._clamp_source_frame(frame))
        except Exception as exc:
            logger.debug("Source playback audio seek failed: %s", exc)

    def _should_defer_playback_tick(self, requested_frame: int) -> bool:
        """True when the GPU decode of a prior frame is still in flight and
        the wall-clock playhead has run far enough ahead that submitting the
        next frame would only be discarded by set_gpu_frame's staleness guard.

        Mirrors timeline_preview_renderer.should_defer_playback_tick: we
        defer only when playing AND a frame is in flight AND the lag exceeds
        the tolerance, so ordinary 1-2 frame Qt scheduling jitter does not
        stall low-fps clips.
        """
        if not self._is_playing:
            return False
        pending = self._gpu_pending_frame
        if pending < 0:
            return False  # nothing in flight — submit freely
        lag = requested_frame - pending
        return lag > self._PLAYBACK_DEFER_LAG_FRAMES

    def _reanchor_playback_clock(self, frame: int) -> None:
        """Reset the monotonic-clock baseline so the next tick computes its
        target frame relative to ``frame`` instead of chasing wall-clock
        time.  Called on backpressure-deferred ticks so the playhead waits
        for the in-flight decode instead of racing ahead of it."""
        import time as _time
        self._playback_start_time = _time.perf_counter()
        self._playback_start_frame = frame

    def _on_playback_tick(self) -> None:
        """Handle playback timer tick with monotonic clock-based frame pacing.

        The tick is faster than the frame period.  Each tick computes the
        target frame from elapsed wall-clock time and presents it directly:
        ticks that arrive before the next frame is due load nothing, and a
        late tick jumps straight to the due frame (dropping intermediates)
        instead of drifting behind wall time.

        Backpressure: when a GPU decode is still in flight and the wall-clock
        playhead has run more than ``_PLAYBACK_DEFER_LAG_FRAMES`` ahead of
        it, this tick holds the playhead at the currently visible frame and
        reanchors the clock so the next tick resumes from there once the
        decode lands.  This keeps the slider and the picture locked together
        instead of the picture freezing while the slider keeps moving.
        """
        if not self._current_asset or self._current_frame >= self._total_frames - 1:
            self.pause()
            return

        if self._shutdown_prepared:
            return  # VP-B11: 关闭序列后的 tick 不再提交解码

        import time as _time
        now = _time.perf_counter()
        elapsed = now - self._playback_start_time
        # Calculate where we should be based on real time
        target_frame = self._playback_start_frame + int(elapsed * self._fps)

        # Clamp to valid range
        target_frame = min(target_frame, self._total_frames - 1)

        # Backpressure: don't let the playhead outrun the decoder.  If a GPU
        # decode is still in flight and wall-clock has run far enough ahead
        # that submitting the next frame would only be discarded, hold the
        # playhead at the visible frame and reanchor the clock so the next
        # tick waits for the decode.  See _should_defer_playback_tick.
        if self._should_defer_playback_tick(target_frame):
            visible = self._last_good_frame if self._last_good_frame >= 0 else self._current_frame
            self._current_frame = visible
            self.slider.setValue(visible)
            self._update_timecode()
            self._reanchor_playback_clock(visible)
            return

        # Not yet due — wait for a later tick.
        if target_frame <= self._current_frame:
            return

        if target_frame > self._current_frame + 1:
            logger.debug(
                f"SourceViewerWidget: dropping {target_frame - self._current_frame - 1} frames to catch up"
            )

        self._current_frame = target_frame

        self.slider.setValue(self._current_frame)
        self._update_timecode()
        if self._view_hidden:
            # VP-B02: hidden view issues no periodic decode/readback; the
            # clock and playhead keep running and the single refresh in
            # showEvent picks the picture back up.
            return
        self._load_frame(self._current_frame)

    def _on_insert(self) -> None:
        """Handle Insert request."""
        if self._current_asset and self._in_point is not None and self._out_point is not None:
            self.insert_requested.emit(
                self._current_asset.asset_id,
                self._in_point,
                self._out_point
            )
            logger.debug(f"Insert requested: {self._in_point}-{self._out_point}")

    def _on_overwrite(self) -> None:
        """Handle Overwrite request."""
        if self._current_asset and self._in_point is not None and self._out_point is not None:
            self.overwrite_requested.emit(
                self._current_asset.asset_id,
                self._in_point,
                self._out_point
            )
            logger.debug(f"Overwrite requested: {self._in_point}-{self._out_point}")

    def _on_append(self) -> None:
        """Handle Append request."""
        if self._current_asset and self._in_point is not None and self._out_point is not None:
            self.append_requested.emit(
                self._current_asset.asset_id,
                self._in_point,
                self._out_point
            )
            logger.debug(f"Append requested: {self._in_point}-{self._out_point}")

    # ==================== Helper Methods ====================

    def _update_timecode(self) -> None:
        """Update timecode display."""
        timecode = self._frame_to_timecode(self._current_frame)
        self.toolbar.set_timecode(timecode, self._fps)
        self.toolbar.set_frame_number(self._current_frame)

    def _frame_to_timecode(self, frame: int) -> str:
        """Convert frame number to timecode (HH:MM:SS:FF).

        FE-B13：改用 interchange.frames_to_timecode（Rational SMPTE，
        fractional FPS 不再用浮点除/模产生漂移帧号）。
        """
        from src.features.edit.core.interchange import frames_to_timecode

        return frames_to_timecode(int(frame), float(self._fps))

    def _load_frame(self, frame: int) -> None:
        """Load and display a specific frame."""
        if not self._current_asset:
            return

        # Check cache first
        if frame in self._frame_cache:
            # AUD-20902: UI 槽只暂存图像，GL 上传发生在 paintGL 内
            self.viewer.submit_qimage(self._frame_cache[frame])
            return

        # Load frame based on media type
        if self._current_asset.is_image:
            self._load_image_frame()
        elif self._current_asset.is_video:
            self._load_video_frame(frame)

    def _load_image_frame(self) -> None:
        """Load an image asset."""
        try:
            from PyQt6.QtGui import QPixmap
            pixmap = QPixmap(self._current_asset.path)
            if not pixmap.isNull():
                qimage = pixmap.toImage()
                self.viewer.submit_qimage(qimage)
                # Cache it
                self._frame_cache[0] = qimage
        except Exception as e:
            logger.error(f"Failed to load image: {e}")

    def _get_media_display_target_size(self) -> Optional[tuple[int, int]]:
        """
        Get the decode target size based on media metadata, not viewer dimensions.

        This prevents the aspect ratio distortion bug where viewer size was used
        as the decode target, causing the proxy frame to match the panel ratio
        instead of the media's native display ratio.

        Returns:
            (width, height) tuple for decode target, or None to use decoder default.
        """
        if self._media_display_width and self._media_display_height:
            # Use the media's native display size capped at a reasonable proxy size
            # to avoid decoding full-res on every frame
            max_dim = 1920  # Max proxy dimension
            w, h = self._media_display_width, self._media_display_height
            if max(w, h) > max_dim:
                scale = max_dim / max(w, h)
                w = int(w * scale)
                h = int(h * scale)
            # Ensure even dimensions for video decoder compatibility
            w += w % 2
            h += h % 2
            return (w, h)
        return None

    def _load_video_frame(self, frame: int) -> None:
        """Load a video frame — GPU path first, CPU fallback only when GPU unavailable."""
        if self._shutdown_prepared:
            return  # VP-B11: 关闭后不再提交任何解码
        from src.shared.video.playback_frame_trace import (
            EditPlaybackFrameTrace,
            get_trace_collector,
        )
        import time as _time

        trace = EditPlaybackFrameTrace(
            frame_index=frame,
            source_frame=frame,
            page='edit_source',
        )
        t0 = _time.perf_counter()

        # Track expected frame for async GPU result guard
        self._gpu_expected_frame = frame

        # ---- GPU path: try shared texture from provider ----
        if self._gpu_frame_provider is not None and self._current_asset is not None:
            try:
                result = self._gpu_frame_provider(self._current_asset.path, frame)
                # New protocol: result is a dict with 'submitted' key
                if isinstance(result, dict):
                    if result.get('submitted', False):
                        # GPU task submitted — start watchdog timer for fallback
                        self._gpu_pending_frame = frame
                        self._gpu_watchdog_timer.start(self._gpu_timeout_ms)
                        self._gpu_stats["decode_submitted"] += 1
                        trace.decode_path = 'gpu_async_submitted'
                        trace.render_path = 'gpu_async_pending'
                        trace.total_ms = (_time.perf_counter() - t0) * 1000
                        get_trace_collector().record(trace)
                        return
                    # GPU path unavailable, fall through to CPU
                    trace.fallback_reason = 'gpu_path_unavailable'
                else:
                    # Legacy protocol: (tex_id, w, h) or None
                    if result is not None:
                        tex_id, w, h = result
                        if tex_id is not None and tex_id != 0:
                            self.viewer.set_shared_texture(tex_id, w, h)
                            trace.decode_path = 'gpu_provider'
                            trace.render_path = 'gpu_direct'
                            trace.total_ms = (_time.perf_counter() - t0) * 1000
                            get_trace_collector().record(trace)
                            return
                    trace.fallback_reason = 'gpu_provider_returned_none'
            except Exception as e:
                logger.debug(f"Source GPU provider failed, falling back to CPU: {e}")
                trace.fallback_reason = f'gpu_provider_error: {e}'

        # ---- CPU fallback: FFmpegMediaSource（Topos 素材经 factory 路由） ----
        # AUD-20903: 解码（关键帧 seek + 前向解码）移入后台线程，UI 线程只
        # 提交请求；QImage 结果经排队信号回 UI 上传（submit_qimage 暂存，
        # paintGL 内完成 GL 上传），watchdog 与播放 tick 不再被同步解码阻塞。
        trace.fallback_reason = trace.fallback_reason or 'no_gpu_provider'
        trace.decode_path = 'cpu_async_submitted'
        trace.render_path = 'cpu_async_pending'
        trace.sync_seek_count = 1  # read_frame seeks keyframe + decodes forward
        trace.total_ms = (_time.perf_counter() - t0) * 1000
        get_trace_collector().record(trace)
        self._submit_cpu_fallback(frame)

    def _on_gpu_timeout(self) -> None:
        """GPU watchdog: fall back to CPU decode when GPU result doesn't arrive in time."""
        if self._shutdown_prepared:
            return  # VP-B11: 关闭后超时不再拉起新解码
        timed_out_frame = self._gpu_pending_frame
        if timed_out_frame < 0:
            return
        self._gpu_pending_frame = -1

        if timed_out_frame != self._gpu_expected_frame:
            return  # Already moved on to a different frame

        logger.debug(f"SourceViewerWidget: GPU timeout for frame {timed_out_frame}, falling back to CPU")
        self._error_message = "gpu_timeout"
        # Trigger CPU fallback for the current expected frame
        # AUD-20903: 解码在后台线程执行，UI 线程不再被阻塞。
        self._submit_cpu_fallback(timed_out_frame)

    def request_cpu_fallback(self, frame: int) -> None:
        """CPU decode requested by the background decode worker.

        Delivered as a queued signal from the decode worker thread; runs on
        the UI thread because it dispatches the decode request whose result
        is uploaded from the UI thread (paintGL deferred channel).
        """
        if self._shutdown_prepared:
            return  # VP-B11: 关闭 token
        if self._current_asset is None or not self._current_asset.is_video:
            return
        # The viewer already moved on (playback/seek): skip stale frames.
        # Use the same trailing window as set_gpu_frame because the worker
        # signal arrives one decode latency after the request.
        expected = self._gpu_expected_frame
        if expected >= 0 and (
            frame > expected or frame < expected - self._GPU_STALE_WINDOW
        ):
            return
        self._gpu_pending_frame = -1
        self._gpu_watchdog_timer.stop()
        self._submit_cpu_fallback(frame)

    def _submit_cpu_fallback(self, frame: int) -> None:
        """AUD-20903: 把 CPU 回退解码请求交给后台线程（latest-wins 合并）。

        ``_gpu_pending_frame`` 同时充当 CPU 解码的在途标记，播放背压
        （_should_defer_playback_tick）据此让播放头等待解码而不是空转。
        """
        asset = self._current_asset
        if asset is None or not asset.is_video:
            return
        self._gpu_pending_frame = frame
        self._cpu_worker.request(
            asset.path,
            frame,
            generation=self._gpu_generation,
            target_size=self._get_media_display_target_size(),
        )

    def _on_cpu_frame_ready(self, frame: int, image: QImage, decode_ms: float,
                            generation: int) -> None:
        """后台 CPU 解码结果（UI 线程）：staleness 守卫后上传并写缓存。"""
        # VP-B11: 关闭 token 在 generation 守卫之前——即使代数巧合相同，
        # 关闭序列启动后的结果也不上屏。
        if self._shutdown_prepared:
            return
        # Generation guard: discard results from previous asset/seek
        if self._current_asset is None or generation != self._gpu_generation:
            return
        # Frame guard: allow results up to _GPU_STALE_WINDOW frames behind the
        # expected frame (decode latency), but never ahead of it.
        expected = self._gpu_expected_frame
        if expected >= 0 and (
            frame > expected or frame < expected - self._GPU_STALE_WINDOW
        ):
            return

        # CPU result arrived — nothing in flight any more
        self._gpu_pending_frame = -1
        self._gpu_watchdog_timer.stop()

        # （AUD-20902: 暂存待传，paintGL 内完成 GL 上传）
        self.viewer.submit_qimage(
            image,
            display_width=self._media_display_width or image.width(),
            display_height=self._media_display_height or image.height(),
        )
        self._frame_cache_lru_put(frame, image)
        self._last_good_qimage = image
        self._last_good_frame = frame
        self._error_message = None

        from src.shared.video.playback_frame_trace import (
            EditPlaybackFrameTrace,
            get_trace_collector,
        )
        trace = EditPlaybackFrameTrace(
            frame_index=frame,
            source_frame=frame,
            page='edit_source',
        )
        trace.decode_path = 'cpu_packed_rgb'
        trace.render_path = 'cpu_qimage_upload'
        trace.decode_ms = float(decode_ms)
        trace.qimage_copy = True
        trace.gl_tex_image_upload = True
        trace.total_ms = float(decode_ms)
        get_trace_collector().record(trace)

    def _on_cpu_decode_failed(self, frame: int, message: str,
                              generation: int) -> None:
        """后台 CPU 解码失败（UI 线程）：回退显示 last good frame。"""
        if self._current_asset is None or generation != self._gpu_generation:
            return
        logger.debug(
            f"SourceViewerWidget: CPU fallback decode failed for frame {frame}: {message}"
        )
        self._gpu_pending_frame = -1
        self._gpu_watchdog_timer.stop()
        # Last good frame strategy: show previous frame instead of blank
        if self._last_good_qimage is not None:
            self.viewer.submit_qimage(
                self._last_good_qimage,
                display_width=self._media_display_width or self._last_good_qimage.width(),
                display_height=self._media_display_height or self._last_good_qimage.height(),
            )
            self._error_message = "decode_fallback"
        else:
            self._error_message = "decode_error"

        from src.shared.video.playback_frame_trace import (
            EditPlaybackFrameTrace,
            get_trace_collector,
        )
        trace = EditPlaybackFrameTrace(
            frame_index=frame,
            source_frame=frame,
            page='edit_source',
        )
        trace.decode_path = 'cpu_packed_rgb'
        trace.render_path = 'cpu_qimage_upload'
        trace.fallback_reason = f'cpu_decode_error: {message}'
        get_trace_collector().record(trace)

    def _frame_cache_lru_put(self, frame: int, qimage: QImage) -> None:
        """Add frame to LRU cache, evicting oldest entry when cache is full."""
        if frame in self._frame_cache:
            self._cache_order.remove(frame)
        self._frame_cache[frame] = qimage
        self._cache_order.append(frame)
        while len(self._frame_cache) > self._cache_size:
            oldest = self._cache_order.pop(0)
            self._frame_cache.pop(oldest, None)

    # ==================== Properties ====================

    @property
    def is_playing(self) -> bool:
        """VP-B01: public playback state (read-only feed for the viewer
        arbiter's state feedback and monitor snapshots)."""
        return self._is_playing

    @property
    def view_visibility_revision(self) -> int:
        """VP-B02: visibility revision (independent of media generation).

        Bumped on every hide/show so hidden-view scheduling can invalidate
        visibility-scoped state without ever treating the current asset's
        media generation as stale.
        """
        return self._view_visibility_revision

    @property
    def gpu_slot_stats(self) -> dict[str, int]:
        """VP-B02: decode/discard/ack counters for leak & starvation triage."""
        return dict(self._gpu_stats)

    @property
    def current_frame(self) -> int:
        """Get the current frame."""
        return self._current_frame

    @property
    def in_point(self) -> Optional[int]:
        """Get the In point."""
        return self._in_point

    @property
    def out_point(self) -> Optional[int]:
        """Get the Out point."""
        return self._out_point

    @property
    def total_frames(self) -> int:
        """Get total frames."""
        return self._total_frames

    @property
    def fps(self) -> float:
        """Get frame rate."""
        return self._fps

    @property
    def gpu_generation(self) -> int:
        """Current GPU generation counter (bumped on seek/asset change)."""
        return self._gpu_generation
