"""
CompositeViewer - Independent OpenGL viewport for the Composite page.

Displays textures from the CompositeRenderer with its own zoom/pan state.
Fully decoupled from the Color Canvas viewport.

Uses ctypes to call OpenGL directly, since QOpenGLFunctions_4_1_Core in PyQt6
does not expose shader compilation functions (glCreateShader, glShaderSource, etc.).

Tool System (S5-08):
- Supports SELECT, PAN, TRANSFORM, CROP, MASK, ROTO, CORNERPIN tool modes.
- Mouse events dispatch based on current tool mode.
- Coordinate conversion: View -> NDC -> Image -> Format/BBox -> Node local.
"""

import ctypes
import ctypes.util
import logging
import math
import numpy as np
from typing import TYPE_CHECKING, Optional, Any
from PyQt6.QtOpenGLWidgets import QOpenGLWidget
from PyQt6.QtCore import Qt, pyqtSignal, QPoint, QPointF, QSize, QTimer, QEvent, QRectF
from PyQt6.QtGui import QActionGroup, QMouseEvent, QWheelEvent, QKeyEvent
from PyQt6.QtWidgets import (
    QWidget, QFrame, QHBoxLayout, QToolButton,
    QMenu, QLabel, QSizePolicy, QButtonGroup,
)
# Import VideoControlBar from Color page for consistent video playback UI
from src.features.color.ui.canvas import VideoControlBar
from src.shared.i18n.i18n import I18N
from src.shared.assets.icon_manager import (
    IconColors,
    icon_3d_select, icon_3d_move, icon_3d_rotate, icon_3d_scale,
    icon_3d_model, icon_3d_point, icon_3d_edge, icon_3d_polygon,
)
from src.shared.ui.widgets.viewport_toolbar_base import ViewportToolBarBase
from src.features.composite.ui.viewport3d_state import (
    Viewer3DToolMode, ComponentMode, OrbitCenterMode,
    TOOL3D_MODE_LABELS, TOOL3D_MODE_SHORTCUTS, ORBIT_CENTER_LABELS,
)
from src.features.composite.ui.viewer_tool_system import (
    ViewerToolMode,
    ViewerInteractionState,
    ViewerSelectionState,
    NodeOutputRegion,
    TOOL_MODE_SHORTCUTS,
    view_pos_to_ndc,
    ndc_to_view_pos,
    ndc_to_image_pos,
    image_pos_to_ndc,
    hit_test_handle,
    hit_test_node_output,
    hit_test_handles_in_rect,
    compute_crop_handles,
    compute_reformat_handles,
    compute_crop_rect_from_handles,
    # Snapping System (S5-14)
    SnapResult,
    DEFAULT_SNAP_CONFIG,
    apply_snapping,
    # Constraint System (S5-14)
    ConstraintConfig,
    apply_axis_constraint,
    determine_locked_axis,
    get_constraint_config_from_modifiers,
)

if TYPE_CHECKING:  # pragma: no cover - annotation-only import
    from src.features.composite.render3d.node_icon_overlay import IconHitTarget


# Keep the Composite viewer aligned with the shared Viewport dock contract.
# The dock is shared by Color, Edit, and Composite and is allowed to shrink to
# 640px, so the swapped-in Composite widget must not reintroduce a larger
# minimum width.
COMPOSITE_VIEWPORT_MIN_WIDTH = 640
COMPOSITE_VIEWPORT_MIN_HEIGHT = 360

logger = logging.getLogger(__name__)

# ── OpenGL bindings via PyOpenGL ──────────────────────────────────────────────
# Windows 上 ctypes 直接加载 opengl32.dll 无法获取 OpenGL 2.0+ 函数
# 使用 PyOpenGL 提供跨平台的 OpenGL 绑定

from OpenGL.GL import (
    # Constants
    GL_VERTEX_SHADER, GL_FRAGMENT_SHADER, GL_ARRAY_BUFFER,     GL_DYNAMIC_DRAW, GL_FLOAT, GL_FALSE, GL_TRUE,
    GL_COMPILE_STATUS, GL_LINK_STATUS, GL_INFO_LOG_LENGTH,
    GL_COLOR_BUFFER_BIT, GL_TRIANGLE_STRIP, GL_TRIANGLE_FAN,
    GL_TEXTURE0, GL_TEXTURE1, GL_TEXTURE2, GL_TEXTURE_2D, GL_FRAMEBUFFER_SRGB, GL_RGBA, GL_RGBA16F,
    GL_RGB, GL_UNSIGNED_BYTE, GL_HALF_FLOAT, GL_TEXTURE_MIN_FILTER, GL_TEXTURE_MAG_FILTER,
    GL_LINEAR, GL_TEXTURE_WRAP_S, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE,
    GL_REPEAT, GL_TEXTURE_WIDTH, GL_TEXTURE_HEIGHT,
    GL_BLEND, GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA,
    GL_LINES, GL_LINE_LOOP,
    GL_FRAMEBUFFER, GL_READ_FRAMEBUFFER, GL_DRAW_FRAMEBUFFER,
    GL_COLOR_ATTACHMENT0, GL_NEAREST,
    GL_R32F, GL_RED, GL_RGBA8,
    GL_SCISSOR_TEST,
    GL_TEXTURE_3D, GL_TEXTURE_WRAP_R, GL_RGB32F, GL_UNPACK_ALIGNMENT,
    # Functions
    glCreateShader, glShaderSource, glCompileShader, glDeleteShader,
    glCreateProgram, glAttachShader, glLinkProgram, glDeleteProgram, glUseProgram,
    glGenBuffers, glDeleteBuffers, glBindBuffer, glBufferData, glBufferSubData,
    glGenVertexArrays, glDeleteVertexArrays, glBindVertexArray,
    glGetAttribLocation, glEnableVertexAttribArray, glVertexAttribPointer,
    glGetShaderiv, glGetProgramiv, glGetShaderInfoLog, glGetProgramInfoLog,
    glGetUniformLocation, glUniform1i, glUniform2f, glUniform3f,
    glViewport, glEnable, glDisable, glBlendFunc, glScissor,
    glClearColor, glClear, glActiveTexture, glBindTexture, glDrawArrays,
    glGenTextures, glDeleteTextures, glTexImage2D, glTexParameteri,
    glBindFramebuffer, glGetTexLevelParameteriv, glGenFramebuffers,
    glDeleteFramebuffers, glFramebufferTexture2D, glBlitFramebuffer, glIsTexture,
    glUniform1f, glUniform4f, glUniformMatrix4fv, glReadPixels, glLineWidth,
    glGetError, glGetIntegerv, glTexImage3D,
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
    glUniform3f = glUniform3f
    glUniformMatrix4fv = glUniformMatrix4fv
    glViewport = glViewport
    glEnable = glEnable
    glDisable = glDisable
    glBlendFunc = glBlendFunc
    glScissor = glScissor
    glGetIntegerv = glGetIntegerv
    glClearColor = glClearColor
    glClear = glClear
    glActiveTexture = glActiveTexture
    glBindTexture = glBindTexture
    glDrawArrays = glDrawArrays
    glGenTextures = glGenTextures
    glDeleteTextures = glDeleteTextures
    glTexImage2D = glTexImage2D
    glTexImage3D = glTexImage3D
    glTexParameteri = glTexParameteri
    glBindFramebuffer = glBindFramebuffer
    glGetTexLevelParameteriv = glGetTexLevelParameteriv
    glGenFramebuffers = glGenFramebuffers
    glDeleteFramebuffers = glDeleteFramebuffers
    glFramebufferTexture2D = glFramebufferTexture2D
    glBlitFramebuffer = glBlitFramebuffer
    glIsTexture = glIsTexture
    glUniform1f = glUniform1f
    glUniform4f = glUniform4f
    glReadPixels = glReadPixels
    glLineWidth = glLineWidth
    glGetError = glGetError

for _name, _value in list(_GLWrapper.__dict__.items()):
    if _name.startswith("_") or not callable(_value):
        continue
    setattr(_GLWrapper, _name, staticmethod(_value))

_gl = _GLWrapper()

# ── Channel display modes ────────────────────────────────────────────────────

CHANNEL_MODES = [
    (0, "RGBA",          "RGB"),
    (1, "Red",           "R"),
    (2, "Green",         "G"),
    (3, "Blue",          "B"),
    (4, "Alpha",         "A"),
    (5, "Luma",          "Luma"),
    (6, "Alpha Overlay", "A.Ovr"),
]

# ── 3D 工具按钮标签/启用状态（与 viewport3d_toolbar.py 保持一致）──────────────
_3D_COMPONENT_LABELS = {
    ComponentMode.OBJECT: "Model",
    ComponentMode.POINT: "Point",
    ComponentMode.EDGE: "Edge",
    ComponentMode.POLYGON: "Polygon",
}
_3D_COMPONENT_ENABLED = {
    ComponentMode.OBJECT: True,
    ComponentMode.POINT: True,     # P1-22：BVH face hit → barycentric vertex
    ComponentMode.EDGE: True,      # P1-22：BVH face hit → stable canonical edge
    ComponentMode.POLYGON: True,   # P7-5/P1-22：shared BVH face hit
}


class CompositeViewerBar(ViewportToolBarBase):
    """Compact toolbar overlay for the CompositeViewer.

    Provides View menu (zoom, fit, actual size, fullscreen, focus mode),
    Guides/Mask/Split view tools, channel selector, exposure/gamma controls,
    the 3D-mode tools (Select/Move/Rotate/Scale + component mode), and the
    zoom percentage display.
    """

    # ── Page-specific signals ───────────────────────────────────────────
    dock_float_requested = pyqtSignal()
    channel_mode_changed = pyqtSignal(int)
    render_pass_changed = pyqtSignal(str)
    exposure_changed = pyqtSignal(float)
    # CP-056: eyedropper pick — linear-space RGBA of the clicked pixel.
    color_picked = pyqtSignal(float, float, float, float)
    gamma_changed = pyqtSignal(float)
    display_srgb_changed = pyqtSignal(bool)
    overlay_bbox_toggled = pyqtSignal(bool)
    overlay_format_toggled = pyqtSignal(bool)
    split_mode_changed = pyqtSignal(str)
    # B2-1: preview resolution tier chosen from the View menu.
    resolution_mode_changed = pyqtSignal(str)
    # B2-2: A/B compare buffer commands ("A"/"B").
    ab_snapshot_store_requested = pyqtSignal(str)
    ab_compare_slot_changed = pyqtSignal(str)
    # B2-3: magnifier loupe controls.
    magnifier_toggled = pyqtSignal(bool)
    magnifier_zoom_changed = pyqtSignal(float)
    # B2-4: 2D background mode ("checker" | "black" | "white").
    background_mode_changed = pyqtSignal(str)
    # 3D-mode tools (were a separate floating Viewport3DToolbar).
    tool_mode_requested = pyqtSignal(object)        # Viewer3DToolMode
    component_mode_requested = pyqtSignal(object)   # ComponentMode
    orbit_center_mode_requested = pyqtSignal(object)  # OrbitCenterMode
    set_orbit_cursor_requested = pyqtSignal(str)      # "view_target" | "selected"

    def __init__(self, viewer, parent=None) -> None:
        super().__init__("compositeViewerBar", parent)
        self._viewer = viewer

        # View menu button
        self.view_menu_btn = self._make_menu_button("View")
        self._layout.addWidget(self.view_menu_btn)

        # Guides menu button — pops up the same panel as the Color page.
        self.guides_menu_btn = self._make_guides_btn()
        self._layout.addWidget(self.guides_menu_btn)

        # Spacer between menus and icon buttons
        spacer = QWidget()
        spacer.setFixedWidth(2)
        self._layout.addWidget(spacer)

        # Mask (alpha preview) toggle button
        self._make_mask_btn()
        self._layout.addWidget(self.mask_btn)

        # Fit to Window button
        self._make_fit_btn()
        self._layout.addWidget(self.fit_btn)

        # Split view toggle button (compare selected node vs final)
        self._make_split_btn()
        self._layout.addWidget(self.split_btn)

        # Spacer
        spacer_ch = QWidget()
        spacer_ch.setFixedWidth(2)
        self._layout.addWidget(spacer_ch)

        # Channel selector button
        self.channel_btn = QToolButton()
        self.channel_btn.setObjectName("channelBtn")
        self.channel_btn.setText(I18N.tr("viewport.channel_rgb", "RGB"))
        self.channel_btn.setFixedHeight(22)
        self.channel_btn.setFixedWidth(42)
        self.channel_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.channel_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.channel_btn.setAutoRaise(True)
        self.channel_btn.setStyleSheet(self._BTN_STYLE)
        self.channel_menu = self._create_channel_menu()
        self.channel_btn.clicked.connect(
            lambda: self._show_menu_at_button(self.channel_menu, self.channel_btn)
        )
        self._layout.addWidget(self.channel_btn)

        # Render-pass/AOV selector. The menu is refreshed from the latest
        # renderer metadata; Beauty remains available for ordinary 2D graphs.
        self.render_pass_btn = QToolButton()
        self.render_pass_btn.setObjectName("renderPassBtn")
        self.render_pass_btn.setText(I18N.tr("viewport.pass_beauty", "Beauty"))
        self.render_pass_btn.setFixedHeight(22)
        self.render_pass_btn.setFixedWidth(66)
        self.render_pass_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.render_pass_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.render_pass_btn.setAutoRaise(True)
        self.render_pass_btn.setStyleSheet(self._BTN_STYLE)
        self.render_pass_menu = QMenu(self)
        self.render_pass_btn.clicked.connect(
            lambda: self._show_menu_at_button(
                self.render_pass_menu, self.render_pass_btn)
        )
        self._render_pass_action_group = QActionGroup(self)
        self._render_pass_action_group.setExclusive(True)
        self._render_pass_actions = {}
        self._layout.addWidget(self.render_pass_btn)

        # Spacer
        spacer_exp = QWidget()
        spacer_exp.setFixedWidth(2)
        self._layout.addWidget(spacer_exp)

        # Exposure display label
        self.exposure_label = QLabel(I18N.tr("viewport.exposure_initial", "Exp: 0.0"))
        self.exposure_label.setObjectName("exposureLabel")
        self.exposure_label.setFixedWidth(58)
        self.exposure_label.setStyleSheet("color: #999; font-size: 11px; padding: 0 4px;")
        self._layout.addWidget(self.exposure_label)

        # ── 3D-mode tools (merged into the main toolbar) ───────────────────
        # Only visible in 3D view mode. Was a separate floating
        # Viewport3DToolbar; now embedded for a single, unified toolbar row.
        self._build_3d_tools(self._layout)

        # Right spacer (pushes zoom label to right)
        right_spacer = QWidget()
        right_spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self._layout.addWidget(right_spacer)

        # Zoom percentage label
        self._add_fps_indicator()
        self._make_zoom_label()
        self._layout.addWidget(self.zoom_label)

        # View menu
        self.view_menu = self._create_view_menu()
        self.view_menu_btn.clicked.connect(
            lambda: self._show_menu_at_button(self.view_menu, self.view_menu_btn)
        )

    # ── 3D tools (embedded; merged from Viewport3DToolbar) ─────────────────

    def _build_3d_tools(self, layout: QHBoxLayout) -> None:
        """Build the 3D-mode tool buttons in a container that is hidden in 2D."""
        self._3d_tools_container = QWidget()
        t3d_layout = QHBoxLayout(self._3d_tools_container)
        t3d_layout.setContentsMargins(0, 0, 0, 0)
        t3d_layout.setSpacing(2)

        BTN_SIZE = 26
        ICON_SIZE = 15

        # Icon factory map for color switching
        self._3d_tool_icon_map = {
            Viewer3DToolMode.SELECT: icon_3d_select,
            Viewer3DToolMode.MOVE: icon_3d_move,
            Viewer3DToolMode.ROTATE: icon_3d_rotate,
            Viewer3DToolMode.SCALE: icon_3d_scale,
        }
        self._3d_comp_icon_map = {
            ComponentMode.OBJECT: icon_3d_model,
            ComponentMode.POINT: icon_3d_point,
            ComponentMode.EDGE: icon_3d_edge,
            ComponentMode.POLYGON: icon_3d_polygon,
        }

        # Tool-mode group (Select/Move/Rotate/Scale) — mutually exclusive.
        self._3d_tool_btn_group = QButtonGroup(self)
        self._3d_tool_btn_group.setExclusive(True)
        self._3d_tool_buttons: dict = {}
        for mode in (Viewer3DToolMode.SELECT, Viewer3DToolMode.MOVE,
                     Viewer3DToolMode.ROTATE, Viewer3DToolMode.SCALE):
            btn = QToolButton()
            btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            btn.setAutoRaise(True)
            btn.setFixedSize(BTN_SIZE, BTN_SIZE)
            btn.setIconSize(QSize(ICON_SIZE, ICON_SIZE))
            btn.setIcon(self._3d_tool_icon_map[mode](IconColors.DEFAULT))
            btn.setCheckable(True)
            btn.setStyleSheet(self._BTN_STYLE)
            shortcut = TOOL3D_MODE_SHORTCUTS[mode]
            btn.setToolTip(f"{TOOL3D_MODE_LABELS[mode]} ({shortcut})")
            btn.clicked.connect(lambda _checked=False, m=mode:
                                self.tool_mode_requested.emit(m))
            self._3d_tool_btn_group.addButton(btn)
            self._3d_tool_buttons[mode] = btn
            t3d_layout.addWidget(btn)

        self._3d_comp_btn_group = QButtonGroup(self)
        self._3d_comp_btn_group.setExclusive(True)
        self._3d_comp_buttons: dict = {}
        for cm in (ComponentMode.OBJECT, ComponentMode.POINT,
                   ComponentMode.EDGE, ComponentMode.POLYGON):
            btn = QToolButton()
            btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            btn.setAutoRaise(True)
            btn.setFixedSize(BTN_SIZE, BTN_SIZE)
            btn.setIconSize(QSize(ICON_SIZE, ICON_SIZE))
            btn.setIcon(self._3d_comp_icon_map[cm](IconColors.DEFAULT))
            btn.setCheckable(True)
            btn.setStyleSheet(self._BTN_STYLE)
            enabled = _3D_COMPONENT_ENABLED[cm]
            btn.setEnabled(enabled)
            tip = _3D_COMPONENT_LABELS[cm]
            if not enabled:
                tip += "（待 mesh picking 实现）"
            btn.setToolTip(tip)
            if enabled:
                btn.clicked.connect(lambda _checked=False, c=cm:
                                    self.component_mode_requested.emit(c))
            self._3d_comp_btn_group.addButton(btn)
            self._3d_comp_buttons[cm] = btn
            t3d_layout.addWidget(btn)

        layout.addWidget(self._3d_tools_container)

        # Local icon color update for component buttons (no roundtrip signal)
        self._3d_comp_btn_group.buttonClicked.connect(self._update_comp_icon_colors)

        # Default selection: SELECT + OBJECT.
        self._3d_tool_buttons[Viewer3DToolMode.SELECT].setChecked(True)
        self._3d_tool_buttons[Viewer3DToolMode.SELECT].setIcon(
            icon_3d_select(IconColors.ORANGE))
        self._3d_comp_buttons[ComponentMode.OBJECT].setChecked(True)
        self._3d_comp_buttons[ComponentMode.OBJECT].setIcon(
            icon_3d_model(IconColors.ORANGE))
        self._3d_tools_container.setVisible(False)  # hidden until 3D mode

    def show_3d_tools(self) -> None:
        """Show the embedded 3D tool buttons (called on entering 3D view mode)."""
        self._3d_tools_container.setVisible(True)

    def hide_3d_tools(self) -> None:
        """Hide the 3D tool buttons and reset to SELECT (called on 2D view mode)."""
        self._3d_tools_container.setVisible(False)
        self.set_tool_mode(Viewer3DToolMode.SELECT)

    def set_tool_mode(self, mode) -> None:
        """Sync the 3D tool-mode button selection and icon colors (no signal recursion)."""
        btn = self._3d_tool_buttons.get(mode)
        if btn is not None and not btn.isChecked():
            btn.setChecked(True)
        # Update icon colors: active=orange, inactive=default
        for m, b in self._3d_tool_buttons.items():
            color = IconColors.ORANGE if m == mode else IconColors.DEFAULT
            b.setIcon(self._3d_tool_icon_map[m](color))

    def set_component_mode(self, mode) -> None:
        """Sync the 3D component-mode button selection and icon colors (no signal recursion)."""
        btn = self._3d_comp_buttons.get(mode)
        if btn is not None and not btn.isChecked():
            btn.setChecked(True)
        # Update icon colors: active=orange, inactive=default
        for m, b in self._3d_comp_buttons.items():
            color = IconColors.ORANGE if m == mode else IconColors.DEFAULT
            b.setIcon(self._3d_comp_icon_map[m](color))

    def set_orbit_center_mode(self, mode) -> None:
        """Sync the View > 3D Orbit Center menu check mark."""
        action = getattr(self, "_orbit_center_actions", {}).get(mode)
        if action is not None and not action.isChecked():
            action.setChecked(True)

    def _update_comp_icon_colors(self, clicked_btn) -> None:
        """Update component button icon colors when a button is clicked directly."""
        for m, b in self._3d_comp_buttons.items():
            color = IconColors.ORANGE if b is clicked_btn else IconColors.DEFAULT
            b.setIcon(self._3d_comp_icon_map[m](color))

    def _create_view_menu(self):
        menu = QMenu(self)

        # Zoom
        menu.addAction(I18N.tr("viewport.zoom_in", "Zoom In"), self.zoom_in_requested.emit)
        menu.addAction(I18N.tr("viewport.zoom_out", "Zoom Out"), self.zoom_out_requested.emit)
        menu.addSeparator()

        # Fit / Actual
        menu.addAction(I18N.tr("viewport.fit", "Fit to Window"), self.fit_requested.emit)
        menu.addAction(I18N.tr("viewport.actual_size", "Actual Size"), self.actual_size_requested.emit)
        menu.addSeparator()

        # 3D orbit center preference. This is viewer-only state: it affects
        # Alt+Left navigation but does not dirty the node graph.
        orbit_menu = menu.addMenu("3D Orbit Center")
        self._orbit_center_action_group = QActionGroup(self)
        self._orbit_center_action_group.setExclusive(True)
        self._orbit_center_actions = {}
        for mode in (
                OrbitCenterMode.AUTO,
                OrbitCenterMode.SCREEN,
                OrbitCenterMode.WORLD,
                OrbitCenterMode.SELECTED,
                OrbitCenterMode.CURSOR):
            action = orbit_menu.addAction(ORBIT_CENTER_LABELS[mode])
            action.setCheckable(True)
            action.setData(mode)
            action.setChecked(mode == OrbitCenterMode.AUTO)
            action.triggered.connect(
                lambda _checked=False, m=mode:
                    self.orbit_center_mode_requested.emit(m))
            self._orbit_center_action_group.addAction(action)
            self._orbit_center_actions[mode] = action
        orbit_menu.addSeparator()
        orbit_menu.addAction(
            I18N.tr("viewport.set_cursor_view_target", "Set 3D Cursor to View Target"),
            lambda: self.set_orbit_cursor_requested.emit("view_target"))
        orbit_menu.addAction(
            I18N.tr("viewport.set_cursor_selected", "Set 3D Cursor to Selected Object"),
            lambda: self.set_orbit_cursor_requested.emit("selected"))
        menu.addSeparator()

        # Exposure controls
        exp_menu = menu.addMenu("Exposure")
        exp_menu.addAction(I18N.tr("viewport.exp_plus_1_stop", "+1 Stop"),
                           lambda: self._adjust_exposure(1.0))
        exp_menu.addAction(I18N.tr("viewport.exp_plus_0_5_stop", "+0.5 Stop"),
                           lambda: self._adjust_exposure(0.5))
        exp_menu.addAction(I18N.tr("viewport.exp_plus_0_1_stop", "+0.1 Stop"),
                           lambda: self._adjust_exposure(0.1))
        exp_menu.addSeparator()
        exp_menu.addAction(I18N.tr("viewport.exp_minus_0_1_stop", "-0.1 Stop"),
                           lambda: self._adjust_exposure(-0.1))
        exp_menu.addAction(I18N.tr("viewport.exp_minus_0_5_stop", "-0.5 Stop"),
                           lambda: self._adjust_exposure(-0.5))
        exp_menu.addAction(I18N.tr("viewport.exp_minus_1_stop", "-1 Stop"),
                           lambda: self._adjust_exposure(-1.0))
        exp_menu.addSeparator()
        exp_menu.addAction(I18N.tr("viewport.exposure_reset", "Reset Exposure"),
                           lambda: self.exposure_changed.emit(0.0))

        # Gamma controls
        gamma_menu = menu.addMenu("Gamma")
        gamma_menu.addAction("0.5", lambda: self.gamma_changed.emit(0.5))
        gamma_menu.addAction("0.8", lambda: self.gamma_changed.emit(0.8))
        gamma_menu.addAction(I18N.tr("viewport.gamma_1_linear", "1.0 (Linear)"),
                             lambda: self.gamma_changed.emit(1.0))
        gamma_menu.addAction("1.8", lambda: self.gamma_changed.emit(1.8))
        gamma_menu.addAction(I18N.tr("viewport.gamma_2_2_srgb", "2.2 (sRGB)"),
                             lambda: self.gamma_changed.emit(2.2))
        gamma_menu.addSeparator()
        gamma_menu.addAction(I18N.tr("viewport.gamma_reset", "Reset Gamma"),
                             lambda: self.gamma_changed.emit(1.0))

        # CP-055: Nuke-style linear-domain viewer controls (inspection only —
        # they never alter the composite's data; applied before the ODT).
        viewer = self._viewer
        gain_menu = menu.addMenu("Viewer Gain")
        for label, val in (("\u00d70.25", 0.25), ("\u00d70.5", 0.5),
                           ("\u00d71", 1.0), ("\u00d72", 2.0), ("\u00d74", 4.0)):
            gain_menu.addAction(label, lambda v=val: viewer.set_viewer_gain(v))
        gain_menu.addAction(I18N.tr("viewport.reset", "Reset"),
                            lambda: viewer.set_viewer_gain(1.0))

        lift_menu = menu.addMenu("Viewer Lift")
        for label, val in (("-0.1", -0.1), ("-0.05", -0.05), ("0", 0.0),
                           ("+0.05", 0.05), ("+0.1", 0.1)):
            lift_menu.addAction(label, lambda v=val: viewer.set_viewer_lift(v))
        lift_menu.addAction(I18N.tr("viewport.reset", "Reset"),
                            lambda: viewer.set_viewer_lift(0.0))

        offset_menu = menu.addMenu("Viewer Offset")
        for label, val in (("-0.1", -0.1), ("-0.05", -0.05), ("0", 0.0),
                           ("+0.05", 0.05), ("+0.1", 0.1)):
            offset_menu.addAction(label, lambda v=val: viewer.set_viewer_offset(v))
        offset_menu.addAction(I18N.tr("viewport.reset", "Reset"),
                              lambda: viewer.set_viewer_offset(0.0))

        # Display transform (ODT): linear→sRGB encode for correct display of
        # scene-linear working-space data. Distinct from viewer gamma above,
        # which is a manual inspection tool layered on top of the ODT.
        # Reads initial state from the viewer (which owns _display_srgb) and
        # talks back via the display_srgb_changed signal.
        display_menu = menu.addMenu("Display Transform")
        initial_srgb = getattr(self._viewer, '_display_srgb', True)
        self._display_srgb_action = display_menu.addAction(
            I18N.tr("viewport.display_srgb", "sRGB (Standard)"))
        self._display_srgb_action.setCheckable(True)
        self._display_srgb_action.setChecked(initial_srgb)
        self._display_srgb_action.triggered.connect(
            lambda checked: self.display_srgb_changed.emit(True))
        self._display_linear_action = display_menu.addAction(
            I18N.tr("viewport.display_linear", "Linear (Raw)"))
        self._display_linear_action.setCheckable(True)
        self._display_linear_action.setChecked(not initial_srgb)
        self._display_linear_action.triggered.connect(
            lambda checked: self.display_srgb_changed.emit(False))
        # Keep the two checkable actions in sync (mutually exclusive).
        self._display_srgb_action.toggled.connect(
            lambda checked: self._display_linear_action.setChecked(not checked))
        menu.addSeparator()

        # B2-1: preview resolution tiers. Maps onto the page's proxy_scale
        # (full=1.0, half=0.5, quarter=0.25); "auto" downgrades to half while
        # the user interacts with the view and restores when idle. Export
        # always renders at full resolution regardless of this setting.
        resolution_menu = menu.addMenu("Resolution")
        self._resolution_action_group = QActionGroup(self)
        self._resolution_action_group.setExclusive(True)
        self._resolution_actions = {}
        resolution_labels = {
            "full": "Full",
            "half": "Half (1/2)",
            "quarter": "Quarter (1/4)",
            "auto": "Auto (Half during interaction)",
        }
        current_resolution = getattr(self._viewer, "_resolution_mode", "full")
        for mode, label in resolution_labels.items():
            action = resolution_menu.addAction(label)
            action.setCheckable(True)
            action.setChecked(mode == current_resolution)
            action.triggered.connect(
                lambda _checked=False, selected=mode:
                self.resolution_mode_changed.emit(selected)
            )
            self._resolution_action_group.addAction(action)
            self._resolution_actions[mode] = action
        menu.addSeparator()

        # Overlays
        self._bbox_action = menu.addAction(
            I18N.tr("viewport.show_bbox_overlay", "Show BBox Overlay"))
        self._bbox_action.setCheckable(True)
        self._bbox_action.toggled.connect(self.overlay_bbox_toggled.emit)

        self._format_action = menu.addAction(
            I18N.tr("viewport.show_format_overlay", "Show Format Overlay"))
        self._format_action.setCheckable(True)
        self._format_action.toggled.connect(self.overlay_format_toggled.emit)
        menu.addSeparator()

        # A/B comparison mode.  The split button captures the "before"
        # texture; this menu chooses whether that texture is shown as a wipe
        # or converted into a difference heatmap against the live frame.
        compare_menu = menu.addMenu("A/B Compare")
        self._split_mode_action_group = QActionGroup(self)
        self._split_mode_action_group.setExclusive(True)
        self._split_mode_actions = {}
        split_mode_labels = {
            "wipe": "Wipe / Split",
            "difference": "Difference Heatmap",
        }
        current_split_mode = getattr(self._viewer, "_split_compare_mode", "wipe")
        for mode, label in split_mode_labels.items():
            action = compare_menu.addAction(label)
            action.setCheckable(True)
            action.setChecked(mode == current_split_mode)
            action.triggered.connect(
                lambda _checked=False, selected=mode:
                self.split_mode_changed.emit(selected)
            )
            self._split_mode_action_group.addAction(action)
            self._split_mode_actions[mode] = action
        compare_menu.addSeparator()
        # B2-2: explicit snapshot slots. Each slot holds a frozen copy of the
        # frame at store time; the compare source selects which one wipes
        # against the live frame.
        compare_menu.addAction(
            I18N.tr("viewport.store_snapshot_a", "Store Snapshot to A\tCtrl+Shift+A"),
            lambda: self.ab_snapshot_store_requested.emit("A"))
        compare_menu.addAction(
            I18N.tr("viewport.store_snapshot_b", "Store Snapshot to B\tCtrl+Shift+B"),
            lambda: self.ab_snapshot_store_requested.emit("B"))
        source_menu = compare_menu.addMenu("Compare Source")
        self._ab_slot_action_group = QActionGroup(self)
        self._ab_slot_action_group.setExclusive(True)
        self._ab_slot_actions = {}
        current_slot = getattr(self._viewer, "_ab_active_slot", "A")
        for slot in ("A", "B"):
            action = source_menu.addAction(f"Slot {slot}")
            action.setCheckable(True)
            action.setChecked(slot == current_slot)
            action.triggered.connect(
                lambda _checked=False, s=slot:
                self.ab_compare_slot_changed.emit(s)
            )
            self._ab_slot_action_group.addAction(action)
            self._ab_slot_actions[slot] = action
        compare_menu.addSeparator()
        compare_menu.addAction(
            I18N.tr("viewport.clear_ab_snapshots", "Clear A/B Snapshots"),
            lambda: self._viewer.clear_split_snapshot())
        menu.addSeparator()

        # B2-3: magnifier loupe — cursor-following nearest-neighbour zoom for
        # pixel-level edge inspection (half-res proxy work, matte edges).
        magnifier_menu = menu.addMenu("Magnifier")
        self._magnifier_toggle_action = magnifier_menu.addAction(
            I18N.tr("viewport.enable_magnifier", "Enable Magnifier (Z)"))
        self._magnifier_toggle_action.setCheckable(True)
        self._magnifier_toggle_action.setChecked(
            getattr(self._viewer, "_magnifier_enabled", False))
        self._magnifier_toggle_action.toggled.connect(
            self.magnifier_toggled.emit)
        magnifier_menu.addSeparator()
        self._magnifier_zoom_action_group = QActionGroup(self)
        self._magnifier_zoom_action_group.setExclusive(True)
        self._magnifier_zoom_actions = {}
        current_zoom = getattr(self._viewer, "_magnifier_zoom", 8.0)
        for zoom in (4.0, 8.0):
            action = magnifier_menu.addAction(f"{int(zoom)}x")
            action.setCheckable(True)
            action.setChecked(abs(zoom - current_zoom) < 1e-6)
            action.triggered.connect(
                lambda _checked=False, z=zoom:
                self.magnifier_zoom_changed.emit(z)
            )
            self._magnifier_zoom_action_group.addAction(action)
            self._magnifier_zoom_actions[zoom] = action
        menu.addSeparator()

        # B2-4: 2D background — transparency checker, black or white field.
        # Only affects the single-image 2D path; quad/3D views keep the
        # global 3D scene background colour.
        background_menu = menu.addMenu("Background")
        self._background_action_group = QActionGroup(self)
        self._background_action_group.setExclusive(True)
        self._background_actions = {}
        background_labels = {
            "checker": "Checkerboard",
            "black": "Black",
            "white": "White",
        }
        current_bg = getattr(self._viewer, "_background_mode", "checker")
        for mode, label in background_labels.items():
            action = background_menu.addAction(label)
            action.setCheckable(True)
            action.setChecked(mode == current_bg)
            action.triggered.connect(
                lambda _checked=False, selected=mode:
                self.background_mode_changed.emit(selected)
            )
            self._background_action_group.addAction(action)
            self._background_actions[mode] = action
        menu.addSeparator()

        # Window
        menu.addAction(I18N.tr("viewport.fullscreen", "Fullscreen"), self.fullscreen_requested.emit)
        menu.addAction(I18N.tr("viewport.focus", "Focus Mode"), self.focus_mode_requested.emit)

        return menu

    def _create_channel_menu(self):
        menu = QMenu(self)
        for mode_id, full_name, short_name in CHANNEL_MODES:
            action = menu.addAction(full_name)
            action.setData(mode_id)
            action.triggered.connect(lambda checked, m=mode_id: self.channel_mode_changed.emit(m))
        return menu

    def _adjust_exposure(self, delta) -> None:
        """Adjust exposure by delta stops, emit the new absolute value."""
        # The viewer stores the actual exposure; we read from the label
        try:
            current = float(self.exposure_label.text().replace("Exp: ", ""))
        except ValueError:
            current = 0.0
        new_val = round(current + delta, 1)
        new_val = max(-10.0, min(10.0, new_val))
        self.exposure_changed.emit(new_val)

    def update_channel_label(self, short_name: str) -> None:
        self.channel_btn.setText(short_name)

    def set_available_render_passes(self, pass_names) -> None:
        """Refresh the AOV menu from semantic pass names."""
        labels = {
            "beauty": "Beauty",
            "depth": "Depth",
            "normal": "Normal",
            "position": "Position",
            "uv": "UV",
            "object_id": "Object ID",
            "material_id": "Material ID",
            "motion_vector": "Motion Vector",
            "diffuse": "Diffuse",
            "specular": "Specular",
            "emission": "Emission",
            "ao": "Ambient Occlusion",
            "shadow": "Shadow",
        }
        available = {str(value) for value in (pass_names or ())}
        ordered = ["beauty"] + [
            name for name in (
                "depth", "normal", "position", "uv", "object_id",
                "material_id", "motion_vector", "diffuse", "specular",
                "emission", "ao", "shadow",
            ) if name in available
        ]
        self.render_pass_menu.clear()
        self._render_pass_action_group = QActionGroup(self)
        self._render_pass_action_group.setExclusive(True)
        self._render_pass_actions = {}
        for name in ordered:
            action = self.render_pass_menu.addAction(labels.get(name, name))
            action.setCheckable(True)
            action.setData(name)
            action.triggered.connect(
                lambda _checked=False, selected=name:
                    self.render_pass_changed.emit(selected)
            )
            self._render_pass_action_group.addAction(action)
            self._render_pass_actions[name] = action
        self.update_render_pass_label("beauty")

    def update_render_pass_label(self, pass_name: str) -> None:
        """Update the compact selector label and checked menu item."""
        action = self._render_pass_actions.get(str(pass_name))
        if action is None:
            action = self._render_pass_actions.get("beauty")
        if action is not None:
            action.setChecked(True)
            self.render_pass_btn.setText(action.text())

    def update_exposure_label(self, exposure: float) -> None:
        self.exposure_label.setText(f"Exp: {exposure:.1f}")

    def update_split_compare_mode(self, mode: str) -> None:
        """Sync the A/B compare mode check mark without emitting a signal."""
        action = self._split_mode_actions.get(str(mode))
        if action is not None and not action.isChecked():
            action.setChecked(True)

    def update_resolution_mode(self, mode: str) -> None:
        """B2-1: sync the Resolution check mark without emitting a signal."""
        action = self._resolution_actions.get(str(mode))
        if action is not None and not action.isChecked():
            action.setChecked(True)

    def update_ab_compare_slot(self, slot: str) -> None:
        """B2-2: sync the compare-source radio without emitting a signal."""
        action = self._ab_slot_actions.get("B" if str(slot).upper().startswith("B") else "A")
        if action is not None and not action.isChecked():
            action.setChecked(True)

    def update_magnifier(self, enabled: bool, zoom: float | None = None) -> None:
        """B2-3: sync magnifier toggle/zoom state without emitting signals."""
        toggle = getattr(self, "_magnifier_toggle_action", None)
        if toggle is not None and toggle.isChecked() != bool(enabled):
            toggle.setChecked(bool(enabled))
        if zoom is None:
            return
        for z, act in self._magnifier_zoom_actions.items():
            if abs(z - float(zoom)) < 1e-6 and not act.isChecked():
                act.setChecked(True)

    def update_background_mode(self, mode: str) -> None:
        """B2-4: sync the background radio without emitting a signal."""
        action = self._background_actions.get(str(mode))
        if action is not None and not action.isChecked():
            action.setChecked(True)


class PixelInspectorBar(QFrame):
    """Bottom overlay bar showing pixel values under the cursor.

    Displays image-space coordinates, RGBA float values, and viewer info.
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("pixelInspectorBar")
        self.setFixedHeight(22)
        self.setMaximumHeight(22)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setAutoFillBackground(False)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(6, 0, 6, 0)
        layout.setSpacing(8)

        self.setStyleSheet("""
            QFrame#pixelInspectorBar {
                background-color: transparent;
            }
            QLabel {
                color: #999;
                font-size: 11px;
                font-family: monospace;
            }
        """)

        self._pos_label = QLabel(I18N.tr("viewport.pos_empty", "x: —  y: —"))
        self._pos_label.setFixedWidth(120)
        layout.addWidget(self._pos_label)

        self._rgba_label = QLabel(
            I18N.tr("viewport.rgba_empty", "R: —  G: —  B: —  A: —"))
        layout.addWidget(self._rgba_label)

        layout.addStretch()

        # Node-preview indicator: shows when the viewer is displaying a specific
        # node's output rather than the Output node's composite (the "1" shortcut).
        self._preview_label = QLabel("")
        self._preview_label.setStyleSheet(
            "color: #5acce6; font-size: 10px; font-weight: bold;"
        )
        self._preview_label.setVisible(False)
        layout.addWidget(self._preview_label)

        self._format_label = QLabel("")
        self._format_label.setStyleSheet("color: #666; font-size: 10px;")
        layout.addWidget(self._format_label)

    def set_preview_node(self, node_id: Optional[str], label: Optional[str] = None) -> None:
        """Show/hide the 'Previewing: <node>' indicator.

        Visible only while node-preview is active (node_id set). Hidden
        otherwise — i.e. the viewer shows the Output composite by default.
        """
        if node_id:
            text = f"◎ Preview: {label or node_id}"
            self._preview_label.setText(text)
            self._preview_label.setVisible(True)
        else:
            self._preview_label.setText("")
            self._preview_label.setVisible(False)

    def paintEvent(self, event) -> None:
        from PyQt6.QtGui import QPainter, QColor
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor(35, 35, 35, 64))
        pen = painter.pen()
        pen.setColor(QColor(80, 80, 80, 100))
        pen.setWidth(1)
        painter.setPen(pen)
        painter.drawLine(0, 0, self.width(), 0)
        painter.end()

    def update_pixel_info(self, img_x: int, img_y: int, r: float, g: float,
                          b: float, a: float, valid: bool = True,
                          space: str = "") -> None:
        """Update the pixel inspector readout.

        Args:
            space: Optional label appended to the RGBA readout to indicate
                which colour space the values are in (e.g. "linear"). This
                is important now that the composite working space is
                scene-linear — without the tag, users would assume the
                numbers are sRGB-encoded.
        """
        if not valid:
            self._pos_label.setText(I18N.tr("viewport.pos_empty", "x: —  y: —"))
            self._rgba_label.setText(
                I18N.tr("viewport.rgba_empty", "R: —  G: —  B: —  A: —"))
            return
        self._pos_label.setText(f"x: {img_x}  y: {img_y}")
        suffix = f"  ({space})" if space else ""
        self._rgba_label.setText(
            f"R: {r:.3f}  G: {g:.3f}  B: {b:.3f}  A: {a:.3f}{suffix}"
        )

    def update_format_info(self, width: int = 0, height: int = 0) -> None:
        if width > 0 and height > 0:
            self._format_label.setText(f"{width}×{height}")
        else:
            self._format_label.setText("")


class CompositeViewer(QOpenGLWidget):
    """
    Independent OpenGL viewport for the Composite page.

    Displays textures from the CompositeRenderer with independent zoom/pan.
    Does not depend on ImageEngine or the Color page's rendering infrastructure.

    Tool System:
    - tool_mode_changed: Signal emitted when tool mode changes.
    - interaction_started: Signal emitted when a drag interaction starts.
    - interaction_updated: Signal emitted during drag (preview updates).
    - interaction_ended: Signal emitted when drag ends (commit parameters).
    - set_tool_mode(): Set current tool mode.
    - get_tool_mode(): Get current tool mode.
    - coordinate conversion helpers for tools.
    """

    zoom_changed = pyqtSignal(float)
    tool_mode_changed = pyqtSignal(object)  # ViewerToolMode
    interaction_started = pyqtSignal(object)  # ViewerInteractionState
    interaction_updated = pyqtSignal(object)  # ViewerInteractionState
    interaction_ended = pyqtSignal(object, dict)  # ViewerInteractionState, original_params
    handle_hit_changed = pyqtSignal(object)  # Optional[HandleType] or str

    # B1: user ROI committed/cleared — payload is a GL-convention BBox
    # (y-up, frame pixels) or None when cleared
    user_roi_changed = pyqtSignal(object)

    # C2-3: the PAINT tool committed one stroke. Payload is
    # (paint_node_id, geometry dict with normalized points/size/src_offset);
    # the page assembles the full stroke from the node's brush defaults
    # and records exactly ONE undo action per stroke.
    paint_stroke_committed = pyqtSignal(str, object)
    # C2-3: [ / ] resized the brush (canvas-px radius) while painting.
    paint_brush_size_changed = pyqtSignal(str, float)

    # B2-1: preview resolution mode ("full" | "half" | "quarter" | "auto").
    # The viewer owns the UI state; the page maps it onto RenderRequest
    # proxy_scale (full=1.0, half=0.5, quarter=0.25).
    resolution_mode_changed = pyqtSignal(str)
    # B2-1 "auto" mode hint: True when the user starts interacting with the
    # view (pan/zoom/tool drag), False after a short idle. The page decides
    # whether to temporarily downgrade proxy_scale.
    auto_proxy_interaction = pyqtSignal(bool)
    # B2-4: 2D background mode changed ("checker" | "black" | "white").
    background_mode_changed = pyqtSignal(str)

    # Selection signals (S5-10)
    node_selected = pyqtSignal(str, bool)  # node_id, is_multi_select
    nodes_selected = pyqtSignal(list)  # list of node_ids (rubber band result)
    handles_selected = pyqtSignal(list)  # list of handle_ids (rubber band result)
    selection_changed = pyqtSignal(object)  # ViewerSelectionState
    component_selection_changed = pyqtSignal(object)  # render3d.SelectionState
    node_hovered = pyqtSignal(object)  # Optional[NodeOutputRegion] for hover highlight
    gl_initialized = pyqtSignal()

    # Phase 2: 3D navigation (fused viewer, Nuke-style single viewport).
    # view_mode is "2d" or "3d"; toggled by Tab or the properties panel.
    view_mode_changed = pyqtSignal(str)  # "2d" | "3d"
    viewer3d_content_size_changed = pyqtSignal(int, int)
    scene3d_nav_started = pyqtSignal(str)            # node_id (undo before)
    scene3d_nav_preview = pyqtSignal(str, dict)      # node_id, preview cam params
    scene3d_nav_committed = pyqtSignal(str, dict)    # node_id, final cam params
    # M2: 四视图开关变化（MMB click toggle）。CompositePage 据此带 quad_view 重新提交渲染。
    quad_view_changed = pyqtSignal(bool)
    # 活动摄影机切换请求（双击 Camera3D 图标触发）。
    # 参数：(scene3d_node_id, camera3d_node_id)。CompositePage 据此写 scene3d 的
    # active_camera_id 参数（带 undo）并重渲染。
    active_camera_requested = pyqtSignal(str, str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        # Texture state
        self._current_tex_id = None
        self._tex_width = 0
        self._tex_height = 0
        # Beauty is copied into _owned_display_tex_id; auxiliary passes are
        # copied into this viewer-owned map. The renderer never hands ownership
        # of its textures to the UI thread.
        self._selected_render_pass = "beauty"
        self._owned_aov_tex_ids: dict[str, int] = {}
        self._owned_aov_tex_sizes: dict[str, tuple[int, int]] = {}
        self._owned_aov_tex_storage: dict[str, str] = {}
        self._beauty_dimensions: tuple[int, int] = (0, 0)

        # GL resources
        self._gl_initialized = False
        self._init_error = None
        self._quad_vbo = None
        self._vao = None
        self._prog = None
        self._pos_loc = -1
        self._uv_loc = -1
        self._checker_prog = None
        self._checker_tex_id = None
        self._overlay_prog = None
        self._owned_display_tex_id = None
        self._owned_display_tex_size: Optional[tuple[int, int]] = None
        # Split-view secondary texture (viewer-owned snapshot of a prior frame,
        # kept stable so the comparison texture cannot be evicted mid-paint).
        self._owned_secondary_tex_id: int = 0
        self._owned_secondary_tex_size: Optional[tuple[int, int]] = None
        # B2-2: second compare buffer (slot "B"). Slot "A" reuses the fields
        # above; _ab_active_slot selects which one feeds u_texture_secondary.
        self._owned_secondary_b_tex_id: int = 0
        self._owned_secondary_b_tex_size: Optional[tuple[int, int]] = None
        self._ab_active_slot: str = "A"
        # The renderer owns/recycles its output ring.  Copy every shared frame
        # into this context's texture before paintGL samples it.
        self._copy_read_fbo: Optional[int] = None
        self._copy_draw_fbo: Optional[int] = None

        # Context sharing probe state
        _sharing_probe_passed: bool | None = None  # None = not tested, True/False
        _sharing_probe_attempted: bool = False

        # Display state
        self._channel_mode = 0       # 0=RGBA, 1=R, 2=G, 3=B, 4=A, 5=Luma, 6=AlphaOverlay
        self._exposure = 0.0         # stops (0 = no change)
        self._viewer_gamma = 1.0     # 1.0 = no change
        # CP-055: Nuke 风格查看器线性域检查控制（gain 乘 / lift 抬黑 / offset 加）
        self._viewer_gain = 1.0
        self._viewer_lift = 0.0
        self._viewer_offset = 0.0
        # Display transform: when True (default), the viewer applies a
        # linear→sRGB encode so scene-linear working-space data displays
        # correctly on an sRGB monitor. This is the composite page's ODT
        # (Output Device Transform). User can disable it to inspect raw
        # linear values (equivalent to the old gamma=1.0 passthrough).
        # _display_transfer: 0=none(linear), 1=sRGB, 2=Rec.709 (video target).
        # _display_srgb kept as a back-compat bool alias (= transfer!=0).
        self._display_transfer = 1
        self._display_srgb = True
        # Stage-2 HDR/wide-gamut preview state.
        self._display_primaries = 0   # 0=BT.709, 1=BT.2020, 2=DCI-P3
        self._tonemap = 0             # 0=off, 1=Reinhard, 2=ACES (HDR->SDR)
        self._target_peak = 100.0     # SDR display peak nits for tonemap
        # C6-2: OCIO ODT LUT state. Descriptor of the active display space
        # (set by set_display_space) drives a one-time 3D LUT bake from the
        # working space via the active colour provider; anything that fails
        # keeps the builtin GLSL ODT in charge.
        self._display_space_descriptor = None
        self._odt_lut_tex_id = 0
        self._odt_lut_key = None        # descriptor key of the baked LUT
        self._odt_lut_failed_key = None  # avoid re-attempting a failing bake

        # Overlay state
        self._show_bbox_overlay = False
        self._show_format_overlay = False
        self._overlay_format = None  # ImageFormat
        self._overlay_bbox = None    # BBox

        # View-tool state (ported from Color page viewport menu bar).
        # Mask preview: 0=off, 1=alpha-as-luma (set by set_mask_preview).
        self._alpha_preview = 0
        # Split view: compares a captured "before" texture vs the live result.
        self._split_active = False
        self._split_pos = 0.5          # 0..1 horizontal split position
        self._split_compare_mode = "wipe"  # wipe or difference heatmap
        self._split_difference_scale = 4.0
        self._secondary_tex_id = 0     # captured "before" texture for split view
        self._owned_secondary_tex_id = 0
        self._split_dragging = False   # user is dragging the split divider
        # B2-1: preview resolution mode. Mirrored onto the page's
        # _preview_quality via resolution_mode_changed; persisted in
        # viewer_state so a project reopens at the same proxy tier.
        self._resolution_mode: str = "full"
        # B2-1 "auto" interaction hint: active while the user pans/zooms or
        # drags in the view; ends after a short idle timer.
        self._auto_proxy_interacting = False
        self._auto_proxy_idle_timer: Optional[QTimer] = None
        # B2-3: magnifier — cursor-following nearest-neighbour loupe for
        # pixel-level edge inspection. Samples the composited framebuffer,
        # so it shows exactly what the user sees (ODT/overlay included).
        self._magnifier_enabled: bool = False
        self._magnifier_zoom: float = 8.0
        # Active guide overlay (None or {type, id, params}).
        self._active_guide = None
        self._last_viewer3d_content_size: Optional[tuple[int, int]] = None

        # B1: user region of interest (image pixels, top-left origin).
        # None = full frame. _user_roi_drag_rect is the live rectangle while
        # the user drags a new ROI in ROI tool mode.
        self._user_roi_rect: Optional[tuple[float, float, float, float]] = None
        self._user_roi_drag_rect: Optional[tuple[float, float, float, float]] = None

        # Pixel inspector state
        self._mouse_pos = QPoint()
        self._mouse_in_viewer = False
        self._pixel_r = 0.0
        self._pixel_g = 0.0
        self._pixel_b = 0.0
        self._pixel_a = 0.0
        self._pixel_valid = False
        self._pixel_img_x = 0
        self._pixel_img_y = 0

        # Zoom / Pan state
        self._zoom_level = 1.0       # 1.0 = fit-to-window scale
        self._pan_x = 0.0            # NDC offset
        self._pan_y = 0.0

        # Mouse interaction state (legacy pan, kept for compatibility)
        self._is_panning = False
        self._pan_start_pos = QPoint()
        self._pan_start_ndc_x = 0.0
        self._pan_start_ndc_y = 0.0

        # Phase 2: 3D view mode (fused viewer). "2d" = normal composite view;
        # "3d" = C4D-style Alt+three-button camera navigation for Scene3D nodes.
        self._view_mode: str = "2d"
        # 3D camera navigation state (mirrors Scene3D node cam_* params)
        self._orbit_yaw: float = 30.0
        self._orbit_pitch: float = 20.0
        self._cam_dist: float = 5.0
        # orbit 相机的注视中心（target），Alt+中键平移更新它（真正的 pan）
        self._cam_target_x: float = 0.0
        self._cam_target_y: float = 0.0
        self._cam_target_z: float = 0.0
        # Active 3D navigation drag
        self._nav_mode: Optional[str] = None  # "orbit" | "pan" | "zoom"
        self._nav_last_pos = QPoint()
        self._nav_is_active = False
        self._orbit_pivot_world: Optional[tuple[float, float, float]] = None
        # Throttle timer for 3D nav preview updates (~60fps)
        self._nav_preview_timer = QTimer(self)
        self._nav_preview_timer.setSingleShot(True)
        self._nav_preview_timer.setInterval(16)
        self._nav_preview_timer.timeout.connect(self._emit_nav_preview)

        # Phase 3: 3D 视口状态/控制器（与 2D ViewerToolMode 分离）。
        # 持有 Viewport3DController（含 Viewport3DState）：布局模式、3D 工具模式、
        # 组件模式、吸附/量化、MMB click/drag 区分。Phase 4（四视图/工具栏）、
        # Phase 5（变换 gizmo）、Phase 6（组件/吸附）会接线使用。
        # 当前阶段：仅持有实例，现有 Alt 导航逻辑不变（控制器逐步接管）。
        from src.features.composite.ui.viewport3d_controller import Viewport3DController
        self._viewport3d_ctrl = Viewport3DController(parent=self)

        # Phase 3: Transform Gizmo (object transform, drawn as viewport overlay).
        # 3D 模式 + scene3d 选中时显示；拖单轴移动物体。
        from src.features.composite.render3d.gizmo_overlay import TransformGizmo
        self._transform_gizmo: TransformGizmo = TransformGizmo()
        self._gizmo_visible: bool = False          # 仅 3D + scene3d 选中时为 True
        self._gizmo_drag_active: bool = False      # 正在拖轴
        self._gizmo_hovered_node_id: Optional[str] = None  # gizmo 所属节点（去重）
        # P4 Camera3D/Light3D 视口图标渲染器（3D 模式下显示，不进合成输出）
        from src.features.composite.render3d.node_icon_overlay import NodeIconRenderer
        self._node_icon_renderer: NodeIconRenderer = NodeIconRenderer()
        # P5 图标拖动状态（拖动 Camera3D/Light3D 图标时激活）
        self._icon_drag_target = None      # IconHitTarget 或 None
        self._icon_drag_active: bool = False
        self._icon_drag_last_pos = None    # QPointF 上次鼠标位置
        # Gizmo 拖动前的 object_pos（undo before 快照）
        self._gizmo_drag_before: dict = {}
        # Gizmo drag source state. 3D object nodes write object_pos_* directly;
        # Camera3D maps the generic gizmo result back to cam_eye/target params.
        self._gizmo_drag_source_params: dict = {}
        self._gizmo_drag_anchor_start: tuple = (0.0, 0.0, 0.0)
        # 物体当前位置（gizmo 锚点）。set_node_info 同步；nav_preview 期间自维护。
        self._gizmo_obj_pos: tuple = (0.0, 0.0, 0.0)
        # Phase 5: 最近一次 gizmo 拖动结果（含 pos/rot/scale，release 时提交 undo）
        self._last_gizmo_drag_result: dict = {}
        # Auto orbit-center uses the selected object until the user manually
        # pans/reframes. After a pan, Auto respects the new screen center so
        # the next orbit does not pull the selected object back to the middle.
        self._orbit_auto_screen_center_after_pan: bool = False

        # Background
        self._checkerboard_enabled = True
        # B2-4: 2D background mode — "checker" (classic transparency grid
        # over the user's 3D-scene viewport colour), "black" or "white".
        # Alpha (mask preview) behaviour is independent of this choice.
        self._background_mode: str = "checker"

        # ── Tool System State (S5-08) ───────────────────────────────────────
        self._tool_mode = ViewerToolMode.PAN
        self._interaction_state = ViewerInteractionState()
        self._hover_handle: Optional[str] = None  # Current handle under mouse
        self._tool_handles: dict[str, QPointF] = {}  # Handle positions in view coords
        self._hit_radius_pixels = 10.0  # Screen-pixel hit radius for handles
        from src.features.composite.ui.viewer_handles import TRANSFORM_ROTATE_HANDLE_INSET
        self._rotate_handle_inset = TRANSFORM_ROTATE_HANDLE_INSET

        # ── Node-specific Tool State (S5-12) ────────────────────────────────
        self._selected_node_type: Optional[str] = None  # Node type for tool context
        self._selected_node_params: dict[str, Any] = {}  # Current node parameters
        self._selected_node_format: Optional[Any] = None  # Node's output format
        # Primary input image (width, height) for geometry-transform handles
        # (transform/cornerpin), so the on-canvas frame hugs the actual image
        # instead of the composite canvas. None → fall back to composite size.
        self._selected_node_input_size: Optional[tuple] = None

        # ── C2-3: PAINT tool state ──
        # In-progress stroke as normalized [[x, y], ...] (top-left origin).
        self._paint_drag_points: Optional[list] = None
        # Alt+click clone source point, normalized. Strokes sample
        # 落点 − 源点 offset from the node's input image.
        self._paint_clone_src: Optional[list] = None
        # Last mouse position in view px (brush-circle overlay).
        self._paint_cursor_view: Optional[QPointF] = None
        # Brush radius in canvas px (session state; [ / ] adjust it and
        # mirror the value onto the node's brush_size parameter).
        self._paint_brush_size_px: float = 40.0
        # Node the in-progress stroke was started on (guards against
        # committing to a different node after a mid-drag selection change).
        self._paint_drag_node_id: Optional[str] = None

        # ── Selection & Hit Testing State (S5-10) ────────────────────────────
        self._selection_state = ViewerSelectionState()
        self._node_output_regions: list[NodeOutputRegion] = []  # For hit testing
        self._hovered_node_region: Optional[NodeOutputRegion] = None  # Node under mouse
        self._is_rubber_band_selecting = False  # Rubber band active flag
        # P1-18: selected child inside the active Model3D hierarchy.  This is
        # separate from the graph node selection so ID/outline passes can
        # target one imported mesh while Properties still edits Model3D.
        self._selected_3d_scene_node_id: Optional[str] = None

        # ── Snapping & Constraint State (S5-14) ─────────────────────────────
        self._snap_config = DEFAULT_SNAP_CONFIG  # Current snap configuration
        self._snap_result: Optional[SnapResult] = None  # Current snap result during drag
        self._constraint_config: Optional[ConstraintConfig] = None  # Current constraint config
        self._locked_axis: Optional[str] = None  # Axis locked during Ctrl+drag
        self._initial_drag_delta: Optional[QPointF] = None  # First delta for axis lock determination

        # ── F6-04: LOD Performance for Drag Interactions ───────────────────────
        self._drag_lod_mode: bool = False  # LOD mode active during drag
        self._drag_lod_timer = QTimer(self)  # Timer to trigger LOD mode
        self._drag_lod_timer.setSingleShot(True)
        self._drag_lod_timer.setInterval(100)  # ms - start LOD after 100ms drag
        self._drag_lod_timer.timeout.connect(self._enable_drag_lod)
        self._drag_throttle_timer = QTimer(self)  # Timer to throttle updates
        self._drag_throttle_timer.setSingleShot(True)
        self._drag_throttle_timer.setInterval(16)  # ms - ~60fps max update rate
        self._drag_throttle_timer.timeout.connect(self._process_pending_drag_update)
        self._pending_drag_update: bool = False  # Flag for pending update
        self._pending_drag_view_pos: Optional[QPointF] = None
        self._pending_drag_modifiers: Any = Qt.KeyboardModifier.NoModifier
        self._drag_lod_level: int = 2  # LOD level (0=full, 2=quarter)

        # ── P0 / Milestone A：3D 视口性能 overlay ──────────────────────────────
        # _perf_diagnostics：最近一帧 Scene3D 的 render_diagnostics（含 perf_stats）。
        # _perf_show_overlay：是否绘制角落 FPS/ms/tris overlay（默认关，debug 用）。
        # _perf_fps：最近估算的 FPS（按帧到达间隔平滑）。
        # _perf_last_frame_monotonic：上一帧到达时间，用于算帧间隔→FPS。
        self._perf_diagnostics: Optional[dict] = None
        self._perf_show_overlay: bool = False
        self._perf_fps: float = 0.0
        self._perf_last_frame_monotonic: float = 0.0
        self._full_render_pending: bool = False  # Flag for full render after drag ends

        self.setMinimumSize(
            COMPOSITE_VIEWPORT_MIN_WIDTH, COMPOSITE_VIEWPORT_MIN_HEIGHT
        )
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMouseTracking(True)

        # Toolbar overlay (top)
        self._toolbar = CompositeViewerBar(self, parent=self)
        self._toolbar.zoom_in_requested.connect(self.zoom_in)
        self._toolbar.zoom_out_requested.connect(self.zoom_out)
        self._toolbar.fit_requested.connect(self.fit_to_window)
        self._toolbar.actual_size_requested.connect(self.actual_size)
        self._toolbar.fullscreen_requested.connect(self._toggle_fullscreen)
        self._toolbar.focus_mode_requested.connect(self._toggle_focus_mode)
        self._toolbar.channel_mode_changed.connect(self.set_channel_mode)
        self._toolbar.render_pass_changed.connect(self.set_render_pass)
        self._toolbar.exposure_changed.connect(self.set_exposure)
        self._toolbar.gamma_changed.connect(self.set_gamma)
        self._toolbar.display_srgb_changed.connect(self.set_display_srgb)
        self._toolbar.overlay_bbox_toggled.connect(self._set_bbox_overlay)
        self._toolbar.overlay_format_toggled.connect(self._set_format_overlay)
        # View tools (ported from Color page viewport menu bar).
        self._toolbar.mask_toggled.connect(self.set_mask_preview)
        self._toolbar.split_toggled.connect(self.set_split_view)
        self._toolbar.split_mode_changed.connect(self.set_split_compare_mode)
        # B2-1: View-menu resolution tier → viewer state (+ page via signal).
        self._toolbar.resolution_mode_changed.connect(self.set_resolution_mode)
        # B2-2: A/B snapshot slots (store commands + compare source switch).
        self._toolbar.ab_snapshot_store_requested.connect(self.store_ab_snapshot)
        self._toolbar.ab_compare_slot_changed.connect(self.set_ab_compare_slot)
        # B2-3: magnifier loupe toggle + zoom.
        self._toolbar.magnifier_toggled.connect(self.set_magnifier_enabled)
        self._toolbar.magnifier_zoom_changed.connect(self.set_magnifier_zoom)
        # B2-4: 2D background tri-state (checker / black / white).
        self._toolbar.background_mode_changed.connect(self.set_background_mode)
        self._toolbar.guide_changed.connect(self._on_guide_changed)
        self.zoom_changed.connect(self._toolbar.update_zoom_label)
        self._toolbar.set_available_render_passes(("beauty",))

        # Pixel inspector overlay (bottom) — default hidden, toggle via right-click menu
        self._inspector_bar = PixelInspectorBar(self)
        self._inspector_bar.setVisible(False)
        self._inspector_bar_user_visible = False  # Track user preference

        # Video control bar (bottom) — shown only for videos, style matches Color page
        self.video_control_bar = VideoControlBar(self)
        self.video_control_bar.setVisible(False)  # Initially hidden for image mode

        # Phase 4: 3D 工具按钮（Select/Move/Rotate/Scale + 组件模式）已合并进主工具栏
        # (CompositeViewerBar)，不再使用独立浮层 Viewport3DToolbar。
        # 信号接线：按钮点击 → 写 Viewer3DToolMode/component_mode 到 controller；
        # 快捷键改工具模式时反向同步工具栏选中态。
        self._toolbar.tool_mode_requested.connect(
            self._viewport3d_ctrl.set_tool_mode)
        self._toolbar.component_mode_requested.connect(
            lambda cm: setattr(self._viewport3d_ctrl.state, "component_mode", cm))
        self._toolbar.orbit_center_mode_requested.connect(
            self.set_3d_orbit_center_mode)
        self._toolbar.set_orbit_cursor_requested.connect(
            self.set_3d_orbit_cursor_from)
        self._viewport3d_ctrl.tool_mode_changed.connect(
            self._toolbar.set_tool_mode)
        self._viewport3d_ctrl.tool_mode_changed.connect(
            self._sync_transform_gizmo_mode)

        # M2: 四视图 owned 正交纹理。渲染器共享纹理（来自 Scene3D 渲染线程）必须每帧
        # blit 到 viewer-owned 纹理（_copy_read_fbo/_copy_draw_fbo 已在 __init__ 上半部建好）。
        # key ∈ top/right/front；0 = 未就绪/未渲染。
        self._owned_quad_tex_ids: dict = {"top": 0, "right": 0, "front": 0}
        self._owned_quad_tex_size: Optional[tuple] = None

        # P7：GPU ID picking。_id_pick_info 由 page 从 metadata_ready 转发：
        # {tex_glo, w, h, pick_map, component_meshes, proj, view}。
        # object pick 读 1 像素；component pick 在同一帧 CPU 几何快照上查 BVH。
        self._id_pick_info: Optional[dict] = None
        self._component_pick_cache = None
        from src.features.composite.render3d.selection import SelectionState
        self._component_selection = SelectionState()

    # ── Public API ──────────────────────────────────────────────────────────

    def set_texture(self, tex_id: int, width: int = 0, height: int = 0,
                    image_format=None, bbox=None) -> bool:
        """Update the beauty pass and trigger repaint.

        Args:
            tex_id: OpenGL texture ID from the shared renderer context.
            width: Texture width in pixels.
            height: Texture height in pixels.
            image_format: Optional ImageFormat for overlay display.
            bbox: Optional BBox for overlay display.
        """
        display_tex_id = tex_id
        actual_size = (int(width), int(height))
        if tex_id and tex_id > 0 and self._gl_initialized:
            self.makeCurrent()
            try:
                actual_w, actual_h = self._query_texture_size(tex_id)
                size_mismatch = (
                    width > 0 and height > 0
                    and (actual_w != width or actual_h != height)
                )
                if actual_w <= 0 or actual_h <= 0 or size_mismatch:
                    logger.warning(
                        "CompositeViewer cannot access renderer texture "
                        f"tex={tex_id} actual={actual_w}x{actual_h} "
                        f"expected={width}x{height}; OpenGL context sharing may be unavailable"
                    )
                    return False
                if not self._copy_shared_texture_to_owned_display(tex_id, actual_w, actual_h):
                    return False
                display_tex_id = self._owned_display_tex_id
                actual_size = (actual_w, actual_h)
            finally:
                self.doneCurrent()
        elif tex_id <= 0:
            self._delete_owned_display_texture()
            self._delete_owned_aov_textures()
            self._selected_render_pass = "beauty"

        self._beauty_dimensions = actual_size
        prev_size = (self._tex_width, self._tex_height)
        if self._selected_render_pass == "beauty" or not self._owned_aov_tex_ids:
            self._current_tex_id = display_tex_id
            self._tex_width, self._tex_height = actual_size
        if image_format is not None:
            self._overlay_format = image_format
        if bbox is not None:
            self._overlay_bbox = bbox
        # Clear overlays when texture is invalid
        if tex_id <= 0:
            self._overlay_format = None
            self._overlay_bbox = None
        # A user ROI captured at another texture size (proxy tier switch,
        # first frame after startup restore) maps to a DIFFERENT region of
        # the frame now — re-emit so the page re-evaluates with the
        # recalculated bbox instead of keeping the stale one.
        if (self._user_roi_rect is not None
                and prev_size != (self._tex_width, self._tex_height)):
            try:
                self.user_roi_changed.emit(self.get_user_roi_bbox())
            except Exception:
                pass
        self._inspector_bar.update_format_info(self._tex_width, self._tex_height)
        self.update()
        return True

    def set_render_pass(self, pass_name: str) -> bool:
        """Select Beauty or a viewer-owned auxiliary render pass.

        Switching passes is a display-only operation. It never re-evaluates
        the graph and never exposes renderer-owned texture IDs to paintGL.
        """
        requested = str(pass_name or "beauty")
        if requested != "beauty" and requested not in self._owned_aov_tex_ids:
            requested = "beauty"
            success = False
        else:
            success = True
        self._selected_render_pass = requested
        if requested == "beauty":
            self._current_tex_id = self._owned_display_tex_id
            self._tex_width, self._tex_height = self._beauty_dimensions
        else:
            self._current_tex_id = self._owned_aov_tex_ids[requested]
            self._tex_width, self._tex_height = self._owned_aov_tex_sizes[requested]
        self._toolbar.update_render_pass_label(requested)
        self._inspector_bar.update_format_info(self._tex_width, self._tex_height)
        self.update()
        return success

    def set_aov_textures(self, aovs: dict | None) -> None:
        """Copy renderer AOVs into viewer-owned textures.

        ``aovs`` is metadata from ``CompositeRenderer`` with entries of the
        form ``{texture_id, width, height, storage}``. The copy occurs while
        this widget's context is current; the renderer may therefore recycle
        its source textures immediately after the metadata signal returns.
        """
        if not self._gl_initialized:
            return
        normalized = {}
        for name, descriptor in (aovs or {}).items():
            if not isinstance(descriptor, dict):
                continue
            try:
                source_id = int(descriptor.get("texture_id", 0))
                width = int(descriptor.get("width", 0))
                height = int(descriptor.get("height", 0))
            except (TypeError, ValueError):
                continue
            if source_id > 0 and width > 0 and height > 0:
                normalized[str(name)] = {
                    "texture_id": source_id,
                    "width": width,
                    "height": height,
                    "storage": str(descriptor.get("storage", "rgba16f")),
                }

        made_current = False
        try:
            self.makeCurrent()
            made_current = True
            for name in list(self._owned_aov_tex_ids):
                if name not in normalized:
                    self._delete_owned_aov_texture(name, assume_current=True)
            for name, descriptor in normalized.items():
                source_id = descriptor["texture_id"]
                if not _gl.glIsTexture(source_id):
                    self._delete_owned_aov_texture(name, assume_current=True)
                    continue
                owned_id = self._owned_aov_tex_ids.get(name, 0)
                if not owned_id:
                    owned_id = int(_gl.glGenTextures(1))
                    self._owned_aov_tex_ids[name] = owned_id
                if self._copy_shared_texture_to_owned_aov(
                        source_id, owned_id, descriptor["width"],
                        descriptor["height"], descriptor["storage"], name):
                    self._owned_aov_tex_sizes[name] = (
                        descriptor["width"], descriptor["height"])
                    self._owned_aov_tex_storage[name] = descriptor["storage"]
                else:
                    self._delete_owned_aov_texture(name, assume_current=True)
        except Exception as exc:
            logger.warning("CompositeViewer AOV texture copy failed: %s", exc)
        finally:
            if made_current:
                self.doneCurrent()

        self._toolbar.set_available_render_passes(normalized.keys())
        self.set_render_pass(self._selected_render_pass)

    def set_image_data(self, data: np.ndarray, width: int = 0, height: int = 0,
                       image_format=None, bbox=None) -> bool:
        """Upload CPU readback data into a viewer-local texture.

        Used as a display-only fallback when the renderer texture cannot be
        accessed from this QOpenGLWidget context.

        Origin convention (CPU -> GPU viewer upload boundary):
            Input data is CPU top-left BGR float32 (from renderer readback).
            OpenGL textures use bottom-left origin, so we must flip once
            before uploading. BGR->RGB channel swap is also applied.
            Flip only happens at this CPU->GL boundary, never in multiple places.
        """
        if data is None or not self._gl_initialized:
            return False

        try:
            arr = np.asarray(data)
            if arr.ndim != 3 or arr.shape[2] < 3:
                return False

            h, w = arr.shape[:2]
            rgb = arr[:, :, :3]
            # Renderer readback returns BGR float32 in 0..1.
            if np.issubdtype(rgb.dtype, np.floating):
                rgb = (np.clip(rgb, 0.0, 1.0) * 255.0).astype(np.uint8)
            else:
                rgb = np.clip(rgb, 0, 255).astype(np.uint8)
            # CPU top-left -> GL bottom-left: flip once at upload boundary,
            # then BGR -> RGB for OpenGL texture format
            rgb = np.ascontiguousarray(np.flipud(rgb[:, :, [2, 1, 0]]))

            self.makeCurrent()
            self._delete_owned_display_texture(assume_current=True)
            tex = ctypes.c_uint()
            _gl.glGenTextures(1, ctypes.byref(tex))
            self._owned_display_tex_id = tex.value
            self._owned_display_tex_size = (w, h)
            _gl.glBindTexture(GL_TEXTURE_2D, self._owned_display_tex_id)
            _gl.glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR)
            _gl.glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR)
            _gl.glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE)
            _gl.glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE)
            _gl.glTexImage2D(
                GL_TEXTURE_2D, 0, GL_RGB, w, h, 0,
                GL_RGB, GL_UNSIGNED_BYTE, rgb.ctypes.data_as(ctypes.c_void_p)
            )
            _gl.glBindTexture(GL_TEXTURE_2D, 0)
            self.doneCurrent()

            self._current_tex_id = self._owned_display_tex_id
            self._tex_width = width if width > 0 else w
            self._tex_height = height if height > 0 else h
            self._beauty_dimensions = (self._tex_width, self._tex_height)
            self._selected_render_pass = "beauty"
            self._toolbar.set_available_render_passes(("beauty",))
            if image_format is not None:
                self._overlay_format = image_format
            if bbox is not None:
                self._overlay_bbox = bbox
            self._inspector_bar.update_format_info(self._tex_width, self._tex_height)
            self.update()
            return True
        except Exception as e:
            logger.warning(f"CompositeViewer CPU fallback upload failed: {e}")
            return False

    def _copy_shared_texture_to_owned_display(
        self,
        source_tex_id: int,
        width: int,
        height: int,
    ) -> bool:
        """Blit a shared renderer texture into a viewer-owned texture.

        ``CompositeRenderer`` rotates and occasionally recreates its output
        ring while the UI event loop paints asynchronously. Holding its raw
        texture ID therefore permits the renderer to overwrite or release the
        texture mid-paint. This copy happens while ``set_texture`` owns the
        viewer context, so subsequent paints never sample renderer-owned memory.
        """
        if not _gl.glIsTexture(source_tex_id):
            logger.warning("CompositeViewer shared texture is not valid: tex=%s", source_tex_id)
            return False

        if self._owned_display_tex_id is None:
            self._owned_display_tex_id = int(_gl.glGenTextures(1))

        _gl.glBindTexture(GL_TEXTURE_2D, self._owned_display_tex_id)
        _gl.glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR)
        _gl.glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR)
        _gl.glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE)
        _gl.glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE)
        if self._owned_display_tex_size != (width, height):
            _gl.glTexImage2D(
                GL_TEXTURE_2D, 0, GL_RGBA16F, width, height, 0,
                GL_RGBA, GL_HALF_FLOAT, None,
            )
            self._owned_display_tex_size = (width, height)

        if self._copy_read_fbo is None:
            self._copy_read_fbo = int(_gl.glGenFramebuffers(1))
        if self._copy_draw_fbo is None:
            self._copy_draw_fbo = int(_gl.glGenFramebuffers(1))

        try:
            _gl.glBindFramebuffer(GL_READ_FRAMEBUFFER, self._copy_read_fbo)
            _gl.glFramebufferTexture2D(
                GL_READ_FRAMEBUFFER, GL_COLOR_ATTACHMENT0, GL_TEXTURE_2D, source_tex_id, 0,
            )
            _gl.glBindFramebuffer(GL_DRAW_FRAMEBUFFER, self._copy_draw_fbo)
            _gl.glFramebufferTexture2D(
                GL_DRAW_FRAMEBUFFER,
                GL_COLOR_ATTACHMENT0,
                GL_TEXTURE_2D,
                self._owned_display_tex_id,
                0,
            )
            _gl.glBlitFramebuffer(
                0, 0, width, height,
                0, 0, width, height,
                GL_COLOR_BUFFER_BIT, GL_NEAREST,
            )
            return _gl.glGetError() == 0
        except Exception as exc:
            logger.warning("CompositeViewer shared texture copy failed: %s", exc)
            return False
        finally:
            _gl.glBindFramebuffer(GL_FRAMEBUFFER, self.defaultFramebufferObject())
            _gl.glBindTexture(GL_TEXTURE_2D, 0)

    def _copy_shared_texture_to_owned_aov(
        self,
        source_tex_id: int,
        target_tex_id: int,
        width: int,
        height: int,
        storage: str,
        pass_name: str,
    ) -> bool:
        """Copy one auxiliary pass into a texture with a compatible format."""
        if not _gl.glIsTexture(source_tex_id) or target_tex_id <= 0:
            return False
        storage = str(storage or "rgba16f")
        if storage == "r32f":
            internal_format, pixel_format, pixel_type = GL_R32F, GL_RED, GL_FLOAT
        elif storage == "rgba8":
            internal_format, pixel_format, pixel_type = GL_RGBA8, GL_RGBA, GL_UNSIGNED_BYTE
        else:
            internal_format, pixel_format, pixel_type = GL_RGBA16F, GL_RGBA, GL_HALF_FLOAT

        _gl.glBindTexture(GL_TEXTURE_2D, target_tex_id)
        _gl.glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR)
        _gl.glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR)
        _gl.glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE)
        _gl.glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE)
        target_signature = (int(width), int(height))
        if (self._owned_aov_tex_sizes.get(pass_name) != target_signature
                or self._owned_aov_tex_storage.get(pass_name) != storage):
            _gl.glTexImage2D(
                GL_TEXTURE_2D, 0, internal_format, width, height, 0,
                pixel_format, pixel_type, None,
            )
        if self._copy_read_fbo is None:
            self._copy_read_fbo = int(_gl.glGenFramebuffers(1))
        if self._copy_draw_fbo is None:
            self._copy_draw_fbo = int(_gl.glGenFramebuffers(1))
        try:
            _gl.glBindFramebuffer(GL_READ_FRAMEBUFFER, self._copy_read_fbo)
            _gl.glFramebufferTexture2D(
                GL_READ_FRAMEBUFFER, GL_COLOR_ATTACHMENT0, GL_TEXTURE_2D,
                source_tex_id, 0,
            )
            _gl.glBindFramebuffer(GL_DRAW_FRAMEBUFFER, self._copy_draw_fbo)
            _gl.glFramebufferTexture2D(
                GL_DRAW_FRAMEBUFFER, GL_COLOR_ATTACHMENT0, GL_TEXTURE_2D,
                target_tex_id, 0,
            )
            _gl.glBlitFramebuffer(
                0, 0, width, height, 0, 0, width, height,
                GL_COLOR_BUFFER_BIT, GL_NEAREST,
            )
            return _gl.glGetError() == 0
        except Exception as exc:
            logger.warning("CompositeViewer AOV blit failed: %s", exc)
            return False
        finally:
            _gl.glBindFramebuffer(GL_FRAMEBUFFER, self.defaultFramebufferObject())
            _gl.glBindTexture(GL_TEXTURE_2D, 0)

    def fit_to_window(self) -> None:
        """Reset zoom and pan to fit the texture in the viewport."""
        self._zoom_level = 1.0
        self._pan_x = 0.0
        self._pan_y = 0.0
        self.zoom_changed.emit(1.0)
        self.update()

    def actual_size(self) -> None:
        """Set zoom to 1:1 pixel mapping."""
        if self._tex_width <= 0 or self._tex_height <= 0:
            return
        dpr = self.devicePixelRatio()
        left, top, right, bottom = self._content_rect_pixels(dpr)
        vp_w = right - left
        vp_h = bottom - top
        if vp_w <= 0 or vp_h <= 0:
            return
        fit_scale = min(vp_w / self._tex_width, vp_h / self._tex_height)
        # At 1:1, the zoom level relative to fit is 1/fit_scale
        self._zoom_level = 1.0 / fit_scale
        self._pan_x = 0.0
        self._pan_y = 0.0
        self.zoom_changed.emit(self._zoom_level)
        self.update()

    def zoom_in(self) -> None:
        self._apply_zoom(1.25, QPoint(self.width() // 2, self.height() // 2))

    def zoom_out(self) -> None:
        self._apply_zoom(1.0 / 1.25, QPoint(self.width() // 2, self.height() // 2))

    @property
    def zoom_level(self):
        return self._zoom_level

    # ── Tool System API (S5-08) ───────────────────────────────────────────────

    def set_tool_mode(self, mode: ViewerToolMode) -> None:
        """Set the current tool mode.

        Args:
            mode: ViewerToolMode enum value (SELECT, PAN, TRANSFORM, etc.)
        """
        if self._tool_mode != mode:
            self._tool_mode = mode
            self._interaction_state.tool = mode
            self.tool_mode_changed.emit(mode)
            # Reset hover handle when changing tools
            self._hover_handle = None
            self._update_cursor_for_tool(mode)
            self.update()

    def get_tool_mode(self) -> ViewerToolMode:
        """Get the current tool mode."""
        return self._tool_mode

    def get_interaction_state(self) -> ViewerInteractionState:
        """Get the current interaction state (for external coordination)."""
        return self._interaction_state

    def set_selected_node(self, node_id: Optional[str]) -> None:
        """Set the selected node for tool interactions.

        Args:
            node_id: Node ID to select, or None to clear selection.
        """
        previous_node_id: Optional[str] = self._interaction_state.selected_node_id
        self._interaction_state.selected_node_id = node_id
        if previous_node_id != node_id:
            self._orbit_auto_screen_center_after_pan = False
        self._update_tool_handles()
        self.update()

    def get_selected_node(self) -> Optional[str]:
        """Get the currently selected node ID."""
        return self._interaction_state.selected_node_id

    def set_selected_3d_scene_node(self, node_id: Optional[str]) -> None:
        """Select an imported Model3D child for outline/picking passes."""
        self._selected_3d_scene_node_id = str(node_id) if node_id else None
        self.update()

    def get_selected_3d_scene_node(self) -> Optional[str]:
        """Return the selected imported Model3D child, if any."""
        return self._selected_3d_scene_node_id

    def set_3d_orbit_center_mode(self, mode) -> None:
        """Set Alt+Left orbit pivot preference from the View menu."""
        try:
            if not isinstance(mode, OrbitCenterMode):
                mode = OrbitCenterMode(str(mode))
        except Exception:
            mode = OrbitCenterMode.AUTO
        self._viewport3d_ctrl.state.orbit_center_mode = mode
        self._orbit_auto_screen_center_after_pan = False
        toolbar = getattr(self, "_toolbar", None)
        if toolbar is not None and hasattr(toolbar, "set_orbit_center_mode"):
            toolbar.set_orbit_center_mode(mode)
        self.update()

    def set_3d_orbit_cursor_from(self, source: str) -> None:
        """Set the stored 3D orbit cursor from a useful scene reference."""
        point = None
        if source == "selected":
            point = self._selected_3d_orbit_center()
        elif source == "view_target":
            point = (self._cam_target_x, self._cam_target_y, self._cam_target_z)
        if point is None:
            return
        self._viewport3d_ctrl.state.orbit_cursor_world = tuple(float(v) for v in point)
        self.update()

    def set_preview_node(self, node_id: Optional[str],
                         label: Optional[str] = None) -> None:
        """Show/hide the 'Previewing: <node>' indicator on the inspector bar.

        Called by CompositePage when node-preview is toggled. ``label`` is the
        previewed node's title for display; pass node_id=None to clear.
        """
        bar = getattr(self, "_inspector_bar", None)
        if bar is not None:
            bar.set_preview_node(node_id, label)

    def set_tool_handles(self, handles: dict[str, QPointF]) -> None:
        """Refresh handle positions for the current tool.

        When called by an external controller (e.g. CompositePage after parameter
        changes), this re-runs the viewport's internal handle computation to
        ensure the full handle set is always produced (including rotate, pivot,
        scale corners, etc.).  The *handles* argument is intentionally ignored
        so that a partial external dict can never clobber the internal state.
        """
        self._update_tool_handles()
        self.update()

    def get_image_pos_at_view_pos(self, view_pos: QPointF, check_bounds: bool = True) -> Optional[QPointF]:
        """Convert view pixel position to image pixel position.

        Args:
            view_pos: Position in Qt widget pixels.
            check_bounds: If True, check if the point is inside the image bounds.

        Returns:
            QPointF in image pixels (top-left origin), or None if outside image (when check_bounds is True).
        """
        ndc_rect = self._compute_current_image_ndc_rect()
        ndc_pos = view_pos_to_ndc(
            view_pos, self.width(), self.height(), self.devicePixelRatio()
        )
        return ndc_to_image_pos(ndc_pos, ndc_rect, self._tex_width, self._tex_height, check_bounds)

    def get_view_pos_at_image_pos(self, image_pos: QPointF) -> Optional[QPointF]:
        """Convert image pixel position to view pixel position.

        Args:
            image_pos: Position in image pixels (top-left origin).

        Returns:
            QPointF in view pixels.
        """
        ndc_rect = self._compute_current_image_ndc_rect()
        ndc_pos = image_pos_to_ndc(image_pos, ndc_rect, self._tex_width, self._tex_height)
        return ndc_to_view_pos(ndc_pos, self.width(), self.height(), self.devicePixelRatio())

    # ── Selection & Hit Testing API (S5-10) ───────────────────────────────────

    def set_node_output_regions(self, regions: list[NodeOutputRegion]) -> None:
        """Set node output regions for hit testing.

        This is called by CompositePage when the node graph changes,
        providing the list of node output bounding boxes that should be
        used for click-to-select functionality.

        Args:
            regions: List of NodeOutputRegion instances.
        """
        self._node_output_regions = regions.copy()
        self.update()

    def get_node_output_regions(self) -> list[NodeOutputRegion]:
        """Get the current node output regions."""
        return self._node_output_regions.copy()

    def get_selection_state(self) -> ViewerSelectionState:
        """Get the current selection state."""
        return self._selection_state

    def clear_selection(self) -> None:
        """Clear all selections."""
        self._selection_state.clear_selection()
        self._interaction_state.selected_node_id = None
        self.selection_changed.emit(self._selection_state)
        self.update()

    def select_node(self, node_id: str, multi_select: bool = False) -> None:
        """Select a single node.

        Args:
            node_id: Node ID to select.
            multi_select: If True, add to existing selection; else replace.
        """
        if not multi_select:
            self._selection_state.clear_selection()
        self._selection_state.add_to_selection(node_id=node_id)
        self._interaction_state.selected_node_id = node_id
        self._update_tool_handles()
        self.selection_changed.emit(self._selection_state)
        self.node_selected.emit(node_id, multi_select)
        self.update()

    def hit_test_node_at_pos(self, view_pos: QPointF) -> Optional[NodeOutputRegion]:
        """Test which node output region is at the given view position.

        Args:
            view_pos: Position in view pixels.

        Returns:
            NodeOutputRegion if hit, None otherwise.
        """
        image_pos = self.get_image_pos_at_view_pos(view_pos)
        if image_pos is None:
            return None

        return hit_test_node_output(image_pos, self._node_output_regions)

    def hit_test_handle_at_pos(self, view_pos: QPointF) -> Optional[str]:
        """Test which handle is at the given view position.

        Args:
            view_pos: Position in view pixels.

        Returns:
            Handle identifier if hit, None otherwise.
        """
        for handle_id, handle_pos in self._tool_handles.items():
            if hit_test_handle(view_pos, handle_pos, self._hit_radius_pixels):
                return handle_id
        return None

    def _compute_current_image_ndc_rect(self) -> tuple[float, float, float, float]:
        """Compute the current image rectangle in NDC with zoom/pan applied."""
        dpr = self.devicePixelRatio()
        return self._compute_vertices(
            int(self.width() * dpr),
            int(self.height() * dpr),
            self._tex_width,
            self._tex_height,
        )

    def _update_cursor_for_tool(self, mode: ViewerToolMode) -> None:
        """Update cursor shape based on tool mode."""
        if mode == ViewerToolMode.PAN or mode == ViewerToolMode.SELECT:
            # Always-on node overlays (Transform/CornerPin/Mask/Roto): hovering
            # a visible handle previews the drag cursor, same as TRANSFORM mode.
            if self._hover_handle and self._selected_node_type in (
                    "transform", "text", "cornerpin", "mask", "roto_shape",
                    "mesh_warp"):
                self.setCursor(Qt.CursorShape.SizeAllCursor)
            elif mode == ViewerToolMode.SELECT:
                self.setCursor(Qt.CursorShape.CrossCursor)
            else:
                self.setCursor(Qt.CursorShape.ArrowCursor)
        elif mode == ViewerToolMode.TRANSFORM:
            if self._hover_handle:
                self.setCursor(Qt.CursorShape.SizeAllCursor)
            else:
                self.setCursor(Qt.CursorShape.ArrowCursor)
        elif mode == ViewerToolMode.CROP:
            self.setCursor(Qt.CursorShape.CrossCursor)
        elif mode == ViewerToolMode.ROI:
            self.setCursor(Qt.CursorShape.CrossCursor)
        elif mode == ViewerToolMode.MASK or mode == ViewerToolMode.ROTO:
            self.setCursor(Qt.CursorShape.CrossCursor)
        elif mode == ViewerToolMode.CORNERPIN:
            self.setCursor(Qt.CursorShape.SizeAllCursor)
        elif mode == ViewerToolMode.PAINT:
            # Circle overlay is drawn in _draw_paint_overlay; crosshair as
            # the fallback glyph when no cursor position is known yet.
            self.setCursor(Qt.CursorShape.CrossCursor)
        else:
            self.setCursor(Qt.CursorShape.ArrowCursor)

    def _update_tool_handles(self) -> None:
        """Update tool handle positions based on selected node.

        All tool types now write handles to the unified _tool_handles table,
        ensuring hit test and drawing use the same data source.

        Uses compute_crop_handles / compute_reformat_handles from viewer_tool_system,
        compute_transform_handles / compute_cornerpin_handles from viewer_handles,
        and compute_mask_handles / compute_roto_handles from viewer_handles.
        """
        if self._interaction_state.selected_node_id is None:
            self._tool_handles.clear()
            return

        if self._tex_width <= 0 or self._tex_height <= 0:
            self._tool_handles.clear()
            return

        ndc_rect = self._compute_current_image_ndc_rect()
        dpr = self.devicePixelRatio()

        node_type = self._selected_node_type
        if node_type == "crop":
            self._tool_handles = compute_crop_handles(
                self._selected_node_params,
                self._tex_width, self._tex_height,
                ndc_rect, self.width(), self.height(), dpr
            )
        elif node_type == "reformat":
            self._tool_handles = compute_reformat_handles(
                self._selected_node_params,
                self._tex_width, self._tex_height,
                ndc_rect, self.width(), self.height(), dpr
            )
        elif node_type in ("transform", "text"):
            self._tool_handles = self._compute_transform_handles_dict(
                ndc_rect, dpr
            )
        elif node_type == "cornerpin":
            self._tool_handles = self._compute_cornerpin_handles_dict(
                ndc_rect, dpr
            )
        elif node_type == "mask":
            self._tool_handles = self._compute_mask_handles_dict(
                ndc_rect, dpr
            )
        elif node_type == "roto_shape":
            self._tool_handles = self._compute_roto_handles_dict(
                ndc_rect, dpr
            )
        elif node_type == "mesh_warp":
            self._tool_handles = self._compute_warp_handles_dict(
                ndc_rect, dpr
            )
        elif node_type == "constant":
            self._tool_handles = self._compute_constant_handles_dict(
                ndc_rect, dpr
            )
        else:
            self._tool_handles.clear()

    def _compute_transform_handles_dict(
        self,
        ndc_rect: tuple[float, float, float, float],
        dpr: float,
    ) -> dict[str, QPointF]:
        """Compute Transform node handle positions as view pixel dict.

        Args:
            ndc_rect: Current image NDC rect (x0, y0, x1, y1).
            dpr: Device pixel ratio.

        Returns:
            Dict mapping handle IDs to view pixel positions.
        """
        from src.features.composite.ui.viewer_handles import compute_transform_handles

        params = self._effective_text_transform_params(
            self._current_transform_params_for_overlay())

        # Transform handles should hug the node's input image (e.g. a 1500×800
        # foreground), not the composite canvas (1920×1080). Fall back to the
        # displayed texture size when the input size isn't known.
        img_w, img_h = self._selected_transform_input_size()

        handles_data = compute_transform_handles(
            params.get("translate_x", 0.0),
            params.get("translate_y", 0.0),
            params.get("scale_x", 1.0),
            params.get("scale_y", 1.0),
            params.get("rotation", 0.0),
            params.get("pivot_mode", "center"),
            params.get("pivot_x", img_w / 2.0),
            params.get("pivot_y", img_h / 2.0),
            img_w, img_h, ndc_rect,
            canvas_width=self._tex_width,
            canvas_height=self._tex_height,
        )

        handles = {}
        # Helper: NDC to view position
        def ndc_to_view(ndc_pos: tuple) -> QPointF:
            return ndc_to_view_pos(
                QPointF(ndc_pos[0], ndc_pos[1]),
                self.width(), self.height(), dpr
            )

        # Map handle IDs to positions.
        # Insertion order matters for hit-test priority (first match wins).
        # The center dot is the pivot/anchor; image-body drags use a virtual
        # "move" handle and are hit-tested against the transformed frame.
        handles["rotate"] = ndc_to_view(self._effective_rotate_handle_ndc(handles_data))
        handles["pivot"] = ndc_to_view(handles_data.pivot)
        handles["scale_x_left"] = ndc_to_view(handles_data.scale_x_left)
        handles["scale_x_right"] = ndc_to_view(handles_data.scale_x_right)
        handles["scale_y_top"] = ndc_to_view(handles_data.scale_y_top)
        handles["scale_y_bottom"] = ndc_to_view(handles_data.scale_y_bottom)
        handles["scale_corner_tl"] = ndc_to_view(handles_data.scale_corner_tl)
        handles["scale_corner_tr"] = ndc_to_view(handles_data.scale_corner_tr)
        handles["scale_corner_bl"] = ndc_to_view(handles_data.scale_corner_bl)
        handles["scale_corner_br"] = ndc_to_view(handles_data.scale_corner_br)

        return handles

    def _selected_transform_input_size(self) -> tuple[float, float]:
        """Return the Transform input size, falling back to the canvas safely.

        For Text nodes the output texture IS the text bbox (tight around the
        glyphs), so the handle frame naturally hugs the text when we return
        the bbox size. No translate/pivot remapping is needed beyond the
        base-position offset applied in _effective_text_transform_params.
        """
        if getattr(self, "_selected_node_type", None) == "text":
            bbox = self._text_bbox()
            if bbox is not None:
                return (bbox["w"], bbox["h"])
            # Fall back to canvas if bbox can't be measured (no Qt, empty text).
            params = self._selected_node_params or {}
            w = int(params.get("width", 0)) or 0
            h = int(params.get("height", 0)) or 0
            if w > 0 and h > 0:
                return (float(w), float(h))
        input_size = self._selected_node_input_size
        if input_size and input_size[0] > 0 and input_size[1] > 0:
            return (float(input_size[0]), float(input_size[1]))
        return (float(max(1, self._tex_width)), float(max(1, self._tex_height)))

    def _text_bbox(self) -> Optional[dict]:
        """Measure the selected Text node's glyph bounding box.

        Returns {x, y, w, h} in canvas pixels (top-left origin). None when
        the text node / bbox can't be resolved.
        """
        if getattr(self, "_selected_node_type", None) != "text":
            return None
        node = getattr(self, "_selected_node_instance", None)
        if node is None:
            return None
        try:
            bbox_x, bbox_y, bbox_w, bbox_h = node.text_handle_bbox()
        except Exception:
            return None
        if bbox_w <= 0 or bbox_h <= 0:
            return None
        return {
            "x": bbox_x, "y": bbox_y,
            "w": float(bbox_w), "h": float(bbox_h),
        }

    def _effective_text_transform_params(self, params: dict) -> dict:
        """Remap Text transform params for the bbox-sized output texture.

        TextNode outputs a bbox-sized texture (tight around the glyphs) and
        applies a GPU transform to place it on the canvas. The GPU transform's
        translate is (bbox_x + user_tx, bbox_y + user_ty) — the bbox's
        laid-out position plus the user offset. compute_transform_handles
        must see the SAME translate so the handle frame tracks the rendered
        text. Pivot is remapped from canvas space to bbox-texture space.

        This keeps the handle frame glued to the text AND makes scale/rotate
        math consistent (image space == bbox texture space).

        Returns a new params dict; for non-text nodes it returns the input
        unchanged.
        """
        if getattr(self, "_selected_node_type", None) != "text":
            return params
        bbox = self._text_bbox()
        if bbox is None:
            return params
        out = dict(params)
        # The GPU transform places texture (0,0) on the canvas so the text
        # lands at its laid-out position (bbox_x, bbox_y). compute_transform_handles
        # must see the equivalent translate. Its default image centre is at
        # (bbox_w/2, canvas_h - bbox_h/2) in Y-down pixels (lower-left area);
        # moving it to (bbox_x + bbox_w/2, bbox_y + bbox_h/2) requires:
        #   tx = bbox_x                          (X: img-centre → text-centre)
        #   ty = bbox_y + bbox_h - canvas_h      (Y: default-centre → text-centre)
        # User translate (Y-down, +ve = down) is added on top.
        canvas_h = self._tex_height or 1
        base_tx = bbox["x"]
        base_ty = bbox["y"] + bbox["h"] - canvas_h
        out["translate_x"] = base_tx + float(params.get("translate_x", 0.0))
        out["translate_y"] = base_ty + float(params.get("translate_y", 0.0))
        # Pivot: custom is stored in canvas space → remap to texture space.
        if str(params.get("pivot_mode", "center")) == "custom":
            out["pivot_x"] = float(params.get("pivot_x", 0.0)) - bbox["x"]
            out["pivot_y"] = float(params.get("pivot_y", 0.0)) - bbox["y"]
        else:
            out["pivot_x"] = bbox["w"] / 2.0
            out["pivot_y"] = bbox["h"] / 2.0
        return out

    def _current_transform_params_for_overlay(self) -> dict[str, Any]:
        """Return Transform params for immediate overlay feedback.

        During a drag, the renderer and node model are throttled, but the
        on-screen control should still track the pointer every repaint.
        """
        input_w, input_h = self._selected_transform_input_size()
        params = dict(self._selected_node_params) if self._selected_node_params else {
            "translate_x": 0.0, "translate_y": 0.0,
            "scale_x": 1.0, "scale_y": 1.0,
            "rotation": 0.0,
            "pivot_mode": "center",
            "pivot_x": input_w / 2.0,
            "pivot_y": input_h / 2.0,
        }

        st = self._interaction_state
        if not (
            st.is_dragging
            and st.tool == ViewerToolMode.TRANSFORM
            and st.original_params
            and st.active_handle
            and self._tex_width > 0
            and self._tex_height > 0
        ):
            return params

        preview_params = st.preview_params or {}
        handle = st.active_handle
        params = dict(st.original_params)

        if handle in ("center", "move"):
            params["translate_x"] = st.original_params.get("translate_x", 0.0) + preview_params.get("delta_x", 0.0)
            params["translate_y"] = st.original_params.get("translate_y", 0.0) + preview_params.get("delta_y", 0.0)
            return params

        if handle == "pivot":
            params["pivot_mode"] = "custom"
            if st.original_params.get("pivot_mode", "center") == "center":
                orig_px = input_w / 2.0
                orig_py = input_h / 2.0
            else:
                orig_px = st.original_params.get("pivot_x", input_w / 2.0)
                orig_py = st.original_params.get("pivot_y", input_h / 2.0)
            params["pivot_x"] = orig_px + preview_params.get("delta_x", 0.0)
            params["pivot_y"] = orig_py + preview_params.get("delta_y", 0.0)
            return params

        if handle.startswith("scale_") or handle == "rotate":
            from src.features.composite.ui.viewer_handles import compute_transform_delta_params

            start_p = (st.drag_start_image_pos.x(), st.drag_start_image_pos.y())
            cur_p = (st.drag_current_image_pos.x(), st.drag_current_image_pos.y())
            delta_params_input_w = input_w
            delta_params_input_h = input_h
            delta_original = st.original_params
            # For Text nodes the handle system works in bbox-texture-local
            # space (image_w/h = bbox size). The viewer's drag positions are
            # in canvas space, so remap them into bbox-local space and use
            # user-facing translate/pivot (0-baselined, NOT the GPU translate
            # which includes the bbox base offset). compute_transform_delta_params
            # then produces correct scale/rotation ratios.
            if self._selected_node_type == "text":
                bbox = self._text_bbox()
                if bbox is not None:
                    start_p = (start_p[0] - bbox["x"], start_p[1] - bbox["y"])
                    cur_p = (cur_p[0] - bbox["x"], cur_p[1] - bbox["y"])
                    delta_original = self._text_user_facing_params(st.original_params)
            # Text allows scaling up to 20x (its PARAM_DEFS max_val), vs the
            # 5x cap that suits Transform nodes.
            max_scale = 20.0 if self._selected_node_type == "text" else 5.0
            params.update(compute_transform_delta_params(
                handle,
                start_p,
                cur_p,
                delta_original,
                delta_params_input_w,
                delta_params_input_h,
                st.modifier_keys,
                max_scale=max_scale,
            ))

        return params

    def _text_user_facing_params(self, params: dict) -> dict:
        """Return user-facing transform params for a Text node's delta math.

        compute_transform_delta_params needs the ORIGINAL (pre-drag) params in
        the same space as the drag coordinates. For Text that's bbox-texture-
        local space: translate is the user offset (0-baselined, not the GPU
        translate that includes the bbox base position), and a 'center' pivot
        resolves to the bbox centre. Custom pivot is remapped from canvas to
        bbox-local space.
        """
        out = dict(params)
        # Keep translate_x/y as-is (user-facing, 0 = no offset).
        if str(params.get("pivot_mode", "center")) == "custom":
            bbox = self._text_bbox()
            if bbox is not None:
                out["pivot_x"] = float(params.get("pivot_x", 0.0)) - bbox["x"]
                out["pivot_y"] = float(params.get("pivot_y", 0.0)) - bbox["y"]
        # 'center' pivot: compute_transform_delta_params resolves it to
        # (image_w/2, image_h/2) = bbox centre, which is correct. No override.
        return out

    def _current_cornerpin_params_for_overlay(self) -> dict[str, Any]:
        """Return CornerPin params for immediate overlay feedback.

        Mirrors _current_transform_params_for_overlay: during a drag the node
        model is throttled, but the corner handles and outline should still
        track the pointer every repaint. Corners are normalized (0-1) coords;
        the image-space drag delta is normalized by the texture size, exactly
        like the page-side commit math in _compute_params_from_interaction.
        """
        params = dict(self._selected_node_params) if self._selected_node_params else {}

        st = self._interaction_state
        if not (
            st.is_dragging
            and st.tool == ViewerToolMode.CORNERPIN
            and st.original_params
            and st.active_handle
            and self._tex_width > 0
            and self._tex_height > 0
        ):
            return params

        handle = st.active_handle
        if not handle.startswith(("to", "from")):
            return params

        # Recompute from the drag-start snapshot so throttled node updates can
        # never make the overlay drift while the pointer moves.
        params = dict(st.original_params)
        delta = st.get_image_delta()
        norm_dx = delta.x() / self._tex_width
        norm_dy = delta.y() / self._tex_height

        # Alignment mode (Alt-drag a source corner without Shift): the matching
        # destination corner follows by the same delta, mirroring the page-side
        # commit math — while from==to the warp stays identity and the image
        # never deforms while corners are pinned onto the content.
        shift = int(getattr(Qt.KeyboardModifier.ShiftModifier, "value",
                            Qt.KeyboardModifier.ShiftModifier))
        mods = int(getattr(st.modifier_keys, "value", st.modifier_keys))
        targets = [handle]
        if handle.startswith("from") and not (shift & mods):
            targets.append("to" + handle[len("from"):])

        for target in targets:
            params[f"{target}_x"] = (
                float(st.original_params.get(f"{target}_x", 0.0)) + norm_dx)
            params[f"{target}_y"] = (
                float(st.original_params.get(f"{target}_y", 0.0)) + norm_dy)
        return params

    def _effective_rotate_handle_ndc(self, handles_data) -> tuple:
        """Rotate-handle NDC for the current frame, orbiting the pivot during a drag.

        At rest this is ``handles_data.rotate`` (Fusion-style, on the top-edge →
        pivot guide, always pointing at the top-middle). During an active rotate
        drag the handle orbits the pivot at the home radius in the direction of
        the cursor, so it follows the mouse instead of staying glued to the
        frame's top-middle. On release the drag ends and the handle snaps back.
        """
        from src.features.composite.ui.viewer_handles import (
            rotate_handle_ndc_following_mouse,
        )

        st = self._interaction_state
        follow = (
            getattr(st, "is_dragging", False)
            and getattr(st, "active_handle", None) == "rotate"
            and getattr(st, "tool", None) == ViewerToolMode.TRANSFORM
            and self._mouse_in_viewer
            and self._tex_width > 0
        )

        mouse_ndc = (0.0, 0.0)
        if follow:
            dpr = self.devicePixelRatio()
            m = view_pos_to_ndc(
                QPointF(self._mouse_pos), self.width(), self.height(), dpr
            )
            mouse_ndc = (m.x(), m.y())

        return rotate_handle_ndc_following_mouse(
            handles_data.rotate, handles_data.pivot, mouse_ndc, follow
        )

    def _rotate_handle_ndc_from_frame(
        self,
        top_mid_ndc: tuple,
        pivot_ndc: tuple,
    ) -> tuple:
        """Rotate-handle NDC on the top-edge → pivot guide inside the frame."""
        t = max(0.0, min(1.0, self._rotate_handle_inset))
        return (
            top_mid_ndc[0] + (pivot_ndc[0] - top_mid_ndc[0]) * t,
            top_mid_ndc[1] + (pivot_ndc[1] - top_mid_ndc[1]) * t,
        )

    def _compute_cornerpin_handles_dict(
        self,
        ndc_rect: tuple[float, float, float, float],
        dpr: float,
    ) -> dict[str, QPointF]:
        """Compute CornerPin node handle positions as view pixel dict.

        Args:
            ndc_rect: Current image NDC rect (x0, y0, x1, y1).
            dpr: Device pixel ratio.

        Returns:
            Dict mapping handle IDs to view pixel positions.
        """
        from src.features.composite.ui.viewer_handles import compute_cornerpin_handles

        params = self._current_cornerpin_params_for_overlay()
        if not params:
            params = {
                "to1_x": 0.0, "to1_y": 0.0,
                "to2_x": 1.0, "to2_y": 0.0,
                "to3_x": 1.0, "to3_y": 1.0,
                "to4_x": 0.0, "to4_y": 1.0,
                "from1_x": 0.0, "from1_y": 0.0,
                "from2_x": 1.0, "from2_y": 0.0,
                "from3_x": 1.0, "from3_y": 1.0,
                "from4_x": 0.0, "from4_y": 1.0,
            }

        to_corners = [
            (params.get("to1_x", 0.0), params.get("to1_y", 0.0)),
            (params.get("to2_x", 1.0), params.get("to2_y", 0.0)),
            (params.get("to3_x", 1.0), params.get("to3_y", 1.0)),
            (params.get("to4_x", 0.0), params.get("to4_y", 1.0)),
        ]
        from_corners = [
            (params.get("from1_x", 0.0), params.get("from1_y", 0.0)),
            (params.get("from2_x", 1.0), params.get("from2_y", 0.0)),
            (params.get("from3_x", 1.0), params.get("from3_y", 1.0)),
            (params.get("from4_x", 0.0), params.get("from4_y", 1.0)),
        ]

        handles_data = compute_cornerpin_handles(
            to_corners, from_corners,
            self._tex_width, self._tex_height, ndc_rect,
        )

        handles = {}
        # Helper: NDC to view position
        def ndc_to_view(ndc_pos: tuple) -> QPointF:
            return ndc_to_view_pos(
                QPointF(ndc_pos[0], ndc_pos[1]),
                self.width(), self.height(), dpr
            )

        # Destination corners (to1-4)
        handles["to1"] = ndc_to_view(handles_data.to_corners[0])
        handles["to2"] = ndc_to_view(handles_data.to_corners[1])
        handles["to3"] = ndc_to_view(handles_data.to_corners[2])
        handles["to4"] = ndc_to_view(handles_data.to_corners[3])

        # Source corners (from1-4) - reference handles
        handles["from1"] = ndc_to_view(handles_data.from_corners[0])
        handles["from2"] = ndc_to_view(handles_data.from_corners[1])
        handles["from3"] = ndc_to_view(handles_data.from_corners[2])
        handles["from4"] = ndc_to_view(handles_data.from_corners[3])

        return handles

    def _compute_mask_handles_dict(
        self,
        ndc_rect: tuple[float, float, float, float],
        dpr: float,
    ) -> dict[str, QPointF]:
        """Compute Mask node handle positions as view pixel dict.

        Mask handles include center, rotate, width/height edges,
        corners, and feather controls.

        Args:
            ndc_rect: Current image NDC rect (x0, y0, x1, y1).
            dpr: Device pixel ratio.

        Returns:
            Dict mapping handle IDs to view pixel positions.
        """
        from src.features.composite.ui.viewer_handles import compute_mask_handles

        params = self._selected_node_params
        if not params:
            # Default mask parameters
            params = {
                "mask_type": "rectangle",
                "center_x": 0.5,
                "center_y": 0.5,
                "width": 1.0,
                "height": 1.0,
                "rotation": 0.0,
                "feather": 0.0,
            }

        handles_data = compute_mask_handles(
            params.get("mask_type", "rectangle"),
            params.get("center_x", 0.5),
            params.get("center_y", 0.5),
            params.get("width", 1.0),
            params.get("height", 1.0),
            params.get("rotation", 0.0),
            params.get("feather", 0.0),
            self._tex_width, self._tex_height, ndc_rect,
        )

        handles = {}
        # Helper: NDC to view position
        def ndc_to_view(ndc_pos: tuple) -> QPointF:
            return ndc_to_view_pos(
                QPointF(ndc_pos[0], ndc_pos[1]),
                self.width(), self.height(), dpr
            )

        # Map handle IDs to positions (using mask_ prefix for compatibility)
        handles["mask_center"] = ndc_to_view(handles_data.center)
        handles["mask_rotate"] = ndc_to_view(handles_data.rotate)
        handles["mask_width_l"] = ndc_to_view(handles_data.width_left)
        handles["mask_width_r"] = ndc_to_view(handles_data.width_right)
        handles["mask_height_t"] = ndc_to_view(handles_data.height_top)
        handles["mask_height_b"] = ndc_to_view(handles_data.height_bottom)
        handles["mask_corner_tl"] = ndc_to_view(handles_data.corner_tl)
        handles["mask_corner_tr"] = ndc_to_view(handles_data.corner_tr)
        handles["mask_corner_bl"] = ndc_to_view(handles_data.corner_bl)
        handles["mask_corner_br"] = ndc_to_view(handles_data.corner_br)
        handles["mask_feather_t"] = ndc_to_view(handles_data.feather_t)
        handles["mask_feather_b"] = ndc_to_view(handles_data.feather_b)
        handles["mask_feather_l"] = ndc_to_view(handles_data.feather_l)
        handles["mask_feather_r"] = ndc_to_view(handles_data.feather_r)

        if handles_data.polygon_radius is not None:
            handles["mask_polygon_radius"] = ndc_to_view(handles_data.polygon_radius)

        return handles

    def _compute_warp_handles_dict(
        self,
        ndc_rect: tuple[float, float, float, float],
        dpr: float,
    ) -> dict[str, QPointF]:
        """C7: MeshWarp lattice point handles (base + offset, view px).

        Handle ``warp_point_{i}`` sits at (grid position + displacement);
        the page maps the drag back onto warp_point_{i}_dx/dy channels.
        """
        from src.features.composite.nodes.warp_node import WarpNode

        params = self._selected_node_params or {}
        grid = max(4, min(16, int(params.get("grid_size", 4))))
        offsets = WarpNode.parse_offsets(
            params.get("offsets", "[]"), WarpNode.point_count(grid))
        offsets = WarpNode.apply_point_parameter_overrides(offsets, params)

        x0, y0, x1, y1 = ndc_rect

        def point_to_view(nx: float, ny: float) -> QPointF:
            ndc = (x0 + nx * (x1 - x0), y1 - ny * (y1 - y0))
            return ndc_to_view_pos(
                QPointF(ndc[0], ndc[1]),
                self.width(), self.height(), dpr
            )

        handles: dict[str, QPointF] = {}
        n = grid + 1
        for row in range(n):
            for col in range(n):
                i = row * n + col
                dx, dy = offsets[i]
                handles[f"warp_point_{i}"] = point_to_view(
                    col / grid + dx, row / grid + dy)
        return handles

    def _compute_roto_handles_dict(
        self,
        ndc_rect: tuple[float, float, float, float],
        dpr: float,
    ) -> dict[str, QPointF]:
        """Compute RotoShape node handle positions as view pixel dict.

        Extends Mask handles with opacity control.

        Args:
            ndc_rect: Current image NDC rect (x0, y0, x1, y1).
            dpr: Device pixel ratio.

        Returns:
            Dict mapping handle IDs to view pixel positions.
        """
        from src.features.composite.ui.viewer_handles import compute_roto_handles

        params = self._selected_node_params
        if not params:
            # Default roto parameters
            params = {
                "shape_type": "rectangle",
                "center_x": 0.5,
                "center_y": 0.5,
                "width": 1.0,
                "height": 1.0,
                "rotation": 0.0,
                "feather": 0.0,
                "opacity": 1.0,
            }

        handles_data = compute_roto_handles(
            params.get("shape_type", "rectangle"),
            params.get("center_x", 0.5),
            params.get("center_y", 0.5),
            params.get("width", 1.0),
            params.get("height", 1.0),
            params.get("rotation", 0.0),
            params.get("feather", 0.0),
            params.get("opacity", 1.0),
            self._tex_width, self._tex_height, ndc_rect,
        )

        handles = {}

        # Helper: NDC to view position
        def ndc_to_view(ndc_pos: tuple) -> QPointF:
            return ndc_to_view_pos(
                QPointF(ndc_pos[0], ndc_pos[1]),
                self.width(), self.height(), dpr
            )

        if params.get("shape_type", "rectangle") == "bezier":
            # Free Roto points are stored in normalized image coordinates and
            # are edited directly on the node's JSON path.  Do not expose the
            # primitive bbox handles in this mode: they would change
            # center/width parameters that the Bezier renderer does not use.
            from src.features.composite.nodes.roto_shape_node import RotoShapeNode
            points = RotoShapeNode.parse_points(params.get("points", "[]"))
            points = RotoShapeNode.apply_point_parameter_overrides(
                points, params)
            x0, y0, x1, y1 = ndc_rect

            def point_to_view(point: list[float]) -> QPointF:
                ndc = (x0 + float(point[0]) * (x1 - x0),
                       y1 - float(point[1]) * (y1 - y0))
                return ndc_to_view(ndc)

            for index, point in enumerate(points):
                handles[f"roto_point_{index}"] = point_to_view(point["pos"])
                handles[f"roto_point_{index}_c1"] = point_to_view(point["c1"])
                handles[f"roto_point_{index}_c2"] = point_to_view(point["c2"])
            if handles:
                return handles

        # Map handle IDs to positions (using roto_ prefix)
        handles["roto_center"] = ndc_to_view(handles_data.center)
        handles["roto_rotate"] = ndc_to_view(handles_data.rotate)
        handles["roto_width_l"] = ndc_to_view(handles_data.width_left)
        handles["roto_width_r"] = ndc_to_view(handles_data.width_right)
        handles["roto_height_t"] = ndc_to_view(handles_data.height_top)
        handles["roto_height_b"] = ndc_to_view(handles_data.height_bottom)
        handles["roto_corner_tl"] = ndc_to_view(handles_data.corner_tl)
        handles["roto_corner_tr"] = ndc_to_view(handles_data.corner_tr)
        handles["roto_corner_bl"] = ndc_to_view(handles_data.corner_bl)
        handles["roto_corner_br"] = ndc_to_view(handles_data.corner_br)
        handles["roto_feather_t"] = ndc_to_view(handles_data.feather_t)
        handles["roto_feather_b"] = ndc_to_view(handles_data.feather_b)
        handles["roto_feather_l"] = ndc_to_view(handles_data.feather_l)
        handles["roto_feather_r"] = ndc_to_view(handles_data.feather_r)

        if handles_data.polygon_radius is not None:
            handles["roto_polygon_radius"] = ndc_to_view(handles_data.polygon_radius)

        if handles_data.opacity is not None:
            handles["roto_opacity"] = ndc_to_view(handles_data.opacity)

        return handles

    def _compute_constant_handles_dict(
        self,
        ndc_rect: tuple[float, float, float, float],
        dpr: float,
    ) -> dict[str, QPointF]:
        """Compute Constant node handle positions as view pixel dict.

        Only gradient/radial modes produce handles; solid/checker return an
        empty dict (no on-screen controls). The handle set depends on mode:
          - gradient: start, end, feather
          - radial:   center, radius_edge, feather
        """
        params = self._selected_node_params or {}
        mode = params.get("mode", "solid")
        if mode not in ("gradient", "radial"):
            return {}

        from src.features.composite.ui.viewer_handles import compute_constant_handles

        handles_data = compute_constant_handles(
            mode,
            params.get("start_x", 0.0), params.get("start_y", 0.5),
            params.get("end_x", 1.0), params.get("end_y", 0.5),
            params.get("center_x", 0.5), params.get("center_y", 0.5),
            params.get("radial_radius", 0.5),
            params.get("feather", 0.0),
            self._tex_width, self._tex_height, ndc_rect,
        )

        handles: dict[str, QPointF] = {}

        def ndc_to_view(ndc_pos: tuple) -> QPointF:
            return ndc_to_view_pos(
                QPointF(ndc_pos[0], ndc_pos[1]),
                self.width(), self.height(), dpr
            )

        if handles_data.active_mode == "radial":
            handles["constant_radial_center"] = ndc_to_view(handles_data.center)
            handles["constant_radial_radius"] = ndc_to_view(handles_data.radius_edge)
        else:
            handles["constant_grad_start"] = ndc_to_view(handles_data.start)
            handles["constant_grad_end"] = ndc_to_view(handles_data.end)
        # NOTE: feather is intentionally NOT given an on-screen handle. It's a
        # scalar softness knob controlled only via the properties-panel slider,
        # so users don't have to chase a second control point around the canvas.

        return handles

    def set_node_info(self, node_type: Optional[str], params: dict[str, Any],
                       node_format: Optional[Any] = None,
                       input_size: Optional[tuple] = None,
                       node_instance: Optional[Any] = None,
                       node_id: Optional[str] = None) -> None:
        """Set selected node's type and parameters for tool handle computation.

        Called by CompositePage when a node is selected.

        Args:
            input_size: Optional (width, height) of the node's primary input
                image, in pixels. Geometry-transform handles (transform /
                cornerpin) should be sized to this input image, not to the
                composite output, so the on-canvas frame hugs the actual image. When
                None, handles fall back to the displayed composite texture
                size (``_tex_width/_tex_height``).
            node_instance: Optional live node object. Text nodes need this to
                measure their glyph bounding box (via text_handle_bbox) so the
                handle frame hugs the text (AE-style) rather than the canvas.
            node_id: Optional selected node id. Recorded as ``_viewing_node_id``
                when the node is a 3D type (scene3d/camera3d),供安全框等
                overlay 判断"当前查看的 3D 节点"用。
        """
        self._cancel_3d_interaction_state()
        self._selected_node_type = node_type
        self._selected_node_params = params.copy() if params else {}
        self._selected_node_format = node_format
        self._selected_node_input_size = input_size
        self._selected_node_instance = node_instance
        # 当前查看的 3D 节点 id（供 _should_show_safe_areas 等 overlay 用）。
        # 仅 scene3d/camera3d 记录；其他类型清空（避免残留）。
        self._viewing_node_id = node_id if node_type in ("scene3d", "camera3d") else None
        # Selection must not implicitly replace the global 3D Viewer camera.
        # Scene3D is a renderer, while Camera3D is a scene object until the user
        # explicitly activates it from the Camera properties header.
        # Phase 3: show/hide the transform gizmo based on selection + view mode.
        self._update_gizmo_visibility()
        self._update_tool_handles()
        self.update()

    def _cancel_3d_interaction_state(self) -> None:
        """Clear transient 3D drag/navigation state on selection changes."""
        try:
            self._nav_is_active = False
            self._nav_mode = None
            self._orbit_pivot_world = None
            timer = getattr(self, "_nav_preview_timer", None)
            if timer is not None:
                timer.stop()
        except Exception:
            pass
        try:
            self._gizmo_drag_active = False
            self._gizmo_drag_before = {}
            self._gizmo_drag_source_params = {}
            self._gizmo_drag_anchor_start = (0.0, 0.0, 0.0)
            self._last_gizmo_drag_result = {}
            gizmo = getattr(self, "_transform_gizmo", None)
            if gizmo is not None:
                gizmo.end_drag()
                gizmo.hovered_axis = None
        except Exception:
            pass
        try:
            self._icon_drag_active = False
            self._icon_drag_target = None
            self._icon_drag_last_pos = None
        except Exception:
            pass

    def _update_hover_handle(self, view_pos: QPointF) -> Optional[str]:
        """Check if mouse is over any tool handle.

        Args:
            view_pos: Current mouse position in view pixels.

        Returns:
            Handle identifier if hit, None otherwise.
        """
        for handle_id, handle_pos in self._tool_handles.items():
            if hit_test_handle(view_pos, handle_pos, self._hit_radius_pixels):
                return handle_id
        return None

    def _update_hover_node(self, view_pos: QPointF) -> Optional[NodeOutputRegion]:
        """Check if mouse is over any node output region.

        Args:
            view_pos: Current mouse position in view pixels.

        Returns:
            NodeOutputRegion if hit, None otherwise.
        """
        return self.hit_test_node_at_pos(view_pos)

    def _hit_test_transform_body(self, view_pos: QPointF) -> bool:
        """Return True when *view_pos* falls inside the transformed image frame."""
        # Text nodes share the Transform handle channel, so the body (image
        # interior) is also grabbable for move — same as a real Transform node.
        if self._selected_node_type not in ("transform", "text"):
            return False
        if self._tex_width <= 0 or self._tex_height <= 0:
            return False

        try:
            from src.features.composite.ui.viewer_handles import compute_transform_handles

            params = self._effective_text_transform_params(
                self._current_transform_params_for_overlay())
            # Body hit-test must use the same image/canvas sizing as the draw
            # path (_compute_transform_handles_dict / _draw_transform_auxiliary),
            # otherwise the hit polygon drifts away from the drawn frame when
            # the input image is smaller than the composite canvas.
            img_w, img_h = self._selected_transform_input_size()
            handles_data = compute_transform_handles(
                params.get("translate_x", 0.0),
                params.get("translate_y", 0.0),
                params.get("scale_x", 1.0),
                params.get("scale_y", 1.0),
                params.get("rotation", 0.0),
                params.get("pivot_mode", "center"),
                params.get("pivot_x", img_w / 2.0),
                params.get("pivot_y", img_h / 2.0),
                img_w, img_h,
                self._compute_current_image_ndc_rect(),
                canvas_width=self._tex_width,
                canvas_height=self._tex_height,
            )
            ndc = view_pos_to_ndc(
                view_pos, self.width(), self.height(), self.devicePixelRatio()
            )
            return self._point_in_convex_quad(
                (ndc.x(), ndc.y()), handles_data.frame_corners
            )
        except Exception as exc:
            logger.debug("Transform body hit-test failed: %s", exc)
            return False

    @staticmethod
    def _point_in_convex_quad(
        point: tuple[float, float],
        quad: tuple[tuple[float, float], tuple[float, float],
                    tuple[float, float], tuple[float, float]],
    ) -> bool:
        """Convex quad hit-test in NDC; accepts either winding direction."""
        px, py = point
        sign: Optional[bool] = None
        eps = 1e-6
        for idx, a in enumerate(quad):
            b = quad[(idx + 1) % len(quad)]
            cross = (b[0] - a[0]) * (py - a[1]) - (b[1] - a[1]) * (px - a[0])
            if abs(cross) <= eps:
                continue
            side = cross > 0.0
            if sign is None:
                sign = side
            elif side != sign:
                return False
        return True

    def _start_rubber_band(self, view_pos: QPointF) -> None:
        """Start rubber band selection at the given position."""
        self._is_rubber_band_selecting = True
        self._selection_state.start_rubber_band(view_pos)
        self.setCursor(Qt.CursorShape.CrossCursor)

    def _update_rubber_band(self, view_pos: QPointF) -> None:
        """Update rubber band end position."""
        self._selection_state.update_rubber_band(view_pos)
        self.update()

    def _end_rubber_band(self) -> tuple[float, float, float, float]:
        """End rubber band selection and return the selection rectangle."""
        self._is_rubber_band_selecting = False
        rect = self._selection_state.end_rubber_band()
        self._update_cursor_for_tool(self._tool_mode)
        return rect

    def _select_handles_in_rubber_band(self) -> list[str]:
        """Select all handles inside the current rubber band rectangle."""
        if not self._selection_state.rubber_band_active:
            return []

        rect = (
            self._selection_state.rubber_band_start.x(),
            self._selection_state.rubber_band_start.y(),
            self._selection_state.rubber_band_end.x(),
            self._selection_state.rubber_band_end.y(),
        )

        return hit_test_handles_in_rect(rect, self._tool_handles, self._hit_radius_pixels)

    # ── OpenGL Lifecycle ────────────────────────────────────────────────────

    def _reset_owned_gl_handles_for_rebuild(self) -> None:
        """Zero every Python-side GL handle ahead of a (re)initializeGL.

        A context rebuild invalidates every GL handle. Without this reset,
        ``_ensure_odt_lut`` would short-circuit on a stale key and bind a
        dead texture id (black frames after context loss).

        AUD-9703: the same is true for every other owned texture and FBO.
        Names from the dead context are invalid in the new one, and the
        blit helpers reuse a non-zero id (skipping glTexImage2D when the
        cached size matches), so stale handles produced black frames /
        failed copies until a texture size change happened to force a
        fresh allocation. Zero everything so the next frame takes the
        allocate-on-demand path; the old objects died with the old
        context, so there is nothing to delete explicitly.
        """
        self._odt_lut_tex_id = 0
        self._odt_lut_key = None
        self._odt_lut_failed_key = None
        self._current_tex_id = None
        self._owned_display_tex_id = None
        self._owned_display_tex_size = None
        self._owned_aov_tex_ids = {}
        self._owned_aov_tex_sizes = {}
        self._owned_secondary_tex_id = 0
        self._owned_secondary_tex_size = None
        self._owned_secondary_b_tex_id = 0
        self._owned_secondary_b_tex_size = None
        quad_ids = getattr(self, "_owned_quad_tex_ids", None)
        if isinstance(quad_ids, dict):
            for quad_key in list(quad_ids):
                quad_ids[quad_key] = 0
        self._owned_quad_tex_size = None
        self._copy_read_fbo = None
        self._copy_draw_fbo = None

    def initializeGL(self) -> None:
        """Initialize OpenGL resources.

        On failure, releases any partial GL objects and sets _init_error
        so paintGL() and callers can detect the failure.
        """
        self._init_error = None

        # A context rebuild invalidates every GL handle (ODT LUT plus every
        # other owned texture/FBO — see _reset_owned_gl_handles_for_rebuild).
        self._reset_owned_gl_handles_for_rebuild()

        # Clear any pending GL errors from context setup
        while _gl.glGetError() != 0:
            pass

        try:
            # ── Shared vertex shader ──
            vert_src = b"""
                #version 330
                in vec2 in_vert;
                in vec2 in_uv;
                out vec2 v_uv;
                void main() {
                    gl_Position = vec4(in_vert, 0.0, 1.0);
                    v_uv = in_uv;
                }
            """

            # ── Display shader (enhanced with channel/exposure/gamma/ODT) ──
            # Pipeline order (composite color management, stage 1):
            #   linear working-space data
            #     → ×2^exposure        (viewer exposure, linear-domain)
            #     → linear→sRGB encode (ODT, when u_display_srgb is true)
            #     → pow(1/gamma)       (optional viewer gamma, on top of ODT)
            #     → channel mode       (RGBA / R / G / B / A / Luma / ...)
            #
            # The ODT (Output Device Transform) encodes scene-linear pixels
            # to sRGB for correct monitor display. It mirrors the inverse of
            # _convert_to_linear (the IDT applied to source nodes), so an
            # unprocessed sRGB image looks identical before/after the linear
            # working-space switch.
            frag_src = b"""
                #version 330
                uniform sampler2D u_texture;
                uniform int u_channel_mode;
                uniform float u_exposure;
                uniform float u_viewer_gain;    // CP-055: linear-domain gain
                uniform float u_viewer_lift;    // CP-055: lift (raises blacks)
                uniform float u_viewer_offset;  // CP-055: additive offset
                uniform float u_viewer_gamma;
                uniform int u_display_transfer;  // 0=none(linear), 1=sRGB, 2=Rec.709, 3=PQ, 4=HLG
                uniform int u_display_primaries; // 0=BT.709, 1=BT.2020, 2=DCI-P3 (gamut of the display target)
                uniform int u_tonemap;           // 0=off, 1=Reinhard, 2=ACES (HDR->SDR preview)
                uniform float u_target_peak;     // SDR display peak nits for tonemap (default 100)
                // C6-2: OCIO-driven ODT. When active, the gamut + transfer
                // encode stage below is replaced by one lookup into a 3D LUT
                // baked from the active OCIO processor (working space ->
                // display space), so preview matches OCIO-managed export.
                uniform int u_odt_lut_active;    // 0=builtin GLSL ODT, 1=LUT
                uniform sampler3D u_odt_lut;
                uniform float u_odt_lut_size;    // LUT resolution (e.g. 32)
                // View tools (ported from Color page viewport menu bar).
                uniform int u_alpha_preview;     // 0=off, 1=alpha-as-luma, 2=alpha-over-checker
                uniform int u_split_active;      // 0=off, 1=compare secondary texture vs current
                uniform int u_split_mode;        // 0=wipe, 1=difference heatmap
                uniform float u_difference_scale;
                uniform sampler2D u_texture_secondary;  // selected-node texture (split view)
                uniform float u_split_pos;       // 0..1 horizontal split position (v_uv.x threshold)
                in vec2 v_uv;
                out vec4 fragColor;

                // sRGB OETF (linear -> sRGB), piecewise per IEC 61966-2-1.
                vec3 linear_to_srgb_vec3(vec3 c) {
                    vec3 lo = c * 12.92;
                    vec3 hi = 1.055 * pow(max(c, vec3(0.0)), vec3(1.0 / 2.4)) - 0.055;
                    return mix(hi, lo, step(c, vec3(0.0031308)));
                }

                // Rec.709 OETF (linear -> Rec.709), BT.709 camera transfer.
                vec3 linear_to_bt709_vec3(vec3 c) {
                    vec3 lo = c * 4.5;
                    vec3 hi = 1.099 * pow(max(c, vec3(0.0)), vec3(0.45)) - 0.099;
                    return mix(hi, lo, step(c, vec3(0.018)));
                }

                // PQ (SMPTE ST 2084) OETF: linear nits -> PQ code [0,1].
                const float PQ_M1 = 0.1593017578125;
                const float PQ_M2 = 78.84375;
                const float PQ_C1 = 0.8359375;
                const float PQ_C2 = 18.8515625;
                const float PQ_C3 = 18.6875;
                const float PQ_LMAX = 10000.0;
                vec3 linear_to_pq_vec3(vec3 c) {
                    vec3 n = clamp(c / PQ_LMAX, 0.0, 1.0);
                    vec3 v = pow(n, vec3(PQ_M1));
                    vec3 num = PQ_C1 + PQ_C2 * v;
                    vec3 den = 1.0 + PQ_C3 * v;
                    return pow(num / den, vec3(PQ_M2));
                }

                // HLG (ARIB STD-B67) OETF: linear (relative) -> HLG code [0,1].
                // Scene-referred; the linear input is taken relative to a
                // reference white (the caller normalises HDR nits accordingly).
                const float HLG_A = 0.17883277;
                const float HLG_B = 0.28466892;
                const float HLG_C = 0.55991073;
                vec3 linear_to_hlg_vec3(vec3 c) {
                    vec3 lo = sqrt(3.0 * c);
                    vec3 hi = HLG_A * log(max(12.0 * c - HLG_B, 1e-10)) + HLG_C;
                    return clamp(mix(hi, lo, step(c, vec3(1.0 / 12.0))), 0.0, 1.0);
                }

                // HDR -> SDR tone mapping for preview on a normal (SDR) display.
                // Reinhard: L_out = (L/target)/(1 + L/target).
                vec3 tonemap_reinhard(vec3 c, float peak) {
                    vec3 n = c / peak;
                    return n / (1.0 + n);
                }
                // ACES (Narkowicz approx): good highlight rolloff.
                vec3 tonemap_aces(vec3 c, float peak) {
                    vec3 x = c / peak;
                    float a = 2.51; float b = 0.03; float cc = 2.43; float d = 0.59; float e = 0.14;
                    return clamp((x*(a*x+b))/(x*(cc*x+d)+e), 0.0, 1.0);
                }

                // Gamut (primaries) conversion in linear light, BT.709-centric.
                // Working space is BT.709; convert to the display target's gamut.
                // GLSL mat3() fills COLUMN-major: the first 3 values are column 0,
                // the next 3 column 1, the last 3 column 2. So to represent the
                // row-major BT709->BT2020 matrix M (where M * c converts a BT.709
                // column vector c), we must write each ORIGINAL ROW as a column.
                // Writing rows verbatim would yield M-transposed (a visible
                // saturation/hue shift).
                const mat3 BT709_TO_BT2020 = mat3(
                    0.6274040, 0.0690970, 0.0163916,   // column 0 = original row 0
                    0.3292820, 0.9195400, 0.0880132,   // column 1 = original row 1
                    0.0433136, 0.0113612, 0.8955950);  // column 2 = original row 2
                const mat3 BT709_TO_P3D65 = mat3(
                    0.8224620, 0.0331946, 0.0170827,
                    0.1775380, 0.9668054, 0.0723974,
                    0.0000000, 0.0000000, 0.9105199);

                vec3 convert_primaries(vec3 c, int target) {
                    if (target == 1) return BT709_TO_BT2020 * c;
                    if (target == 2) return BT709_TO_P3D65 * c;
                    return c;  // BT.709 = working space, no-op
                }

                // Composite color-management + display pipeline, applied to the
                // scene-linear RGB of a sampled pixel. Factored out so the split
                // view can run both textures through the identical pipeline.
                vec3 process_display(vec3 rgb) {
                    rgb = rgb * pow(2.0, u_exposure);
                    // CP-055: viewer gain/lift/offset (Nuke-style linear
                    // inspection controls, applied before tonemap/ODT.
                    if (u_viewer_gain != 1.0) {
                        rgb *= u_viewer_gain;
                    }
                    if (u_viewer_lift != 0.0) {
                        rgb = rgb + u_viewer_lift * (vec3(1.0) - rgb);
                    }
                    if (u_viewer_offset != 0.0) {
                        rgb += vec3(u_viewer_offset);
                    }
                    if (u_tonemap == 1) {
                        rgb = tonemap_reinhard(rgb, u_target_peak);
                    } else if (u_tonemap == 2) {
                        rgb = tonemap_aces(rgb, u_target_peak);
                    }
                    if (u_odt_lut_active == 1) {
                        // OCIO path: one trilinear lookup replaces the
                        // primaries + transfer encode. Texel-centre mapping
                        // (same convention as the lut3d node). Values outside
                        // [0,1] clamp to the LUT domain - with tonemap off and
                        // an HDR display transfer the CPU keeps this path off
                        // (nits would exceed the domain).
                        vec3 lut_coord = (clamp(rgb, vec3(0.0), vec3(1.0))
                                          * (u_odt_lut_size - 1.0) + 0.5)
                                         / u_odt_lut_size;
                        rgb = texture(u_odt_lut, lut_coord).rgb;
                    } else {
                        rgb = convert_primaries(rgb, u_display_primaries);
                        if (u_display_transfer == 1) {
                            rgb = linear_to_srgb_vec3(rgb);
                        } else if (u_display_transfer == 2) {
                            rgb = linear_to_bt709_vec3(rgb);
                        } else if (u_display_transfer == 3) {
                            rgb = linear_to_pq_vec3(rgb);
                        } else if (u_display_transfer == 4) {
                            rgb = linear_to_hlg_vec3(rgb);
                        }
                    }
                    if (u_viewer_gamma != 1.0) {
                        rgb = pow(max(rgb, vec3(0.0)), vec3(1.0 / u_viewer_gamma));
                    }
                    return rgb;
                }

                void main() {
                    // Difference mode compares both A/B textures after the
                    // same display transform.  This makes the heatmap useful
                    // for QC rather than falsely highlighting exposure/ODT
                    // differences introduced by the viewer itself.
                    if (u_split_active == 1 && u_split_mode == 1) {
                        vec4 before_color = texture(u_texture_secondary, v_uv);
                        vec4 after_color = texture(u_texture, v_uv);
                        vec3 before_rgb = process_display(before_color.rgb);
                        vec3 after_rgb = process_display(after_color.rgb);
                        vec3 difference = abs(before_rgb - after_rgb);
                        vec3 heatmap = clamp(
                            difference * u_difference_scale, vec3(0.0), vec3(1.0));
                        float alpha_difference = abs(
                            before_color.a - after_color.a) * u_difference_scale;
                        heatmap = max(heatmap, vec3(alpha_difference));
                        fragColor = vec4(heatmap, 1.0);
                        return;
                    }

                    // Split view: left half shows the captured "before" texture,
                    // right half shows the final composite. Both pass
                    // through the same color pipeline so the comparison is fair.
                    vec4 color;
                    if (u_split_active == 1) {
                        if (v_uv.x < u_split_pos) {
                            color = texture(u_texture_secondary, v_uv);
                        } else {
                            color = texture(u_texture, v_uv);
                        }
                    } else {
                        color = texture(u_texture, v_uv);
                    }
                    vec3 rgb = color.rgb;

                    // Mask (alpha) preview: applied before the normal display
                    // pipeline so it reads raw alpha rather than encoded output.
                    if (u_alpha_preview == 1) {
                        float a = color.a;
                        fragColor = vec4(a, a, a, 1.0);
                        return;
                    } else if (u_alpha_preview == 2) {
                        // Checkerboard backdrop + premultiplied RGB to isolate
                        // the alpha region against a known background.
                        vec2 cell = floor(v_uv * vec2(32.0, 18.0));
                        float checker = mod(cell.x + cell.y, 2.0);
                        vec3 bg = mix(vec3(0.5), vec3(0.3), checker);
                        vec3 out_rgb = mix(bg, process_display(rgb), color.a);
                        fragColor = vec4(out_rgb, 1.0);
                        return;
                    }

                    rgb = process_display(rgb);

                    // Channel mode selection
                    float ch;
                    switch (u_channel_mode) {
                        case 0:  // RGBA (normal display)
                            fragColor = vec4(rgb, color.a);
                            break;
                        case 1:  // Red only
                            fragColor = vec4(rgb.r, 0.0, 0.0, 1.0);
                            break;
                        case 2:  // Green only
                            fragColor = vec4(0.0, rgb.g, 0.0, 1.0);
                            break;
                        case 3:  // Blue only
                            fragColor = vec4(0.0, 0.0, rgb.b, 1.0);
                            break;
                        case 4:  // Alpha only (grayscale)
                            ch = color.a;
                            fragColor = vec4(ch, ch, ch, 1.0);
                            break;
                        case 5:  // Luma
                            ch = dot(rgb, vec3(0.2126, 0.7152, 0.0722));
                            fragColor = vec4(ch, ch, ch, 1.0);
                            break;
                        case 6:  // Alpha overlay (premultiplied display)
                            fragColor = vec4(rgb * color.a, 1.0);
                            break;
                        default:
                            fragColor = vec4(rgb, color.a);
                    }
                }
            """
            self._prog = self._compile_program(vert_src, frag_src)
            if not self._prog:
                self._init_error = "Failed to compile display shader"
                logger.error(f"{self._init_error}, aborting GL init")
                return

            # ── Overlay line shader ──
            overlay_vert = b"""
                #version 330
                in vec2 in_vert;
                void main() {
                    gl_Position = vec4(in_vert, 0.0, 1.0);
                }
            """
            overlay_frag = b"""
                #version 330
                uniform vec4 u_color;
                out vec4 fragColor;
                void main() {
                    fragColor = u_color;
                }
            """
            self._overlay_prog = self._compile_program(overlay_vert, overlay_frag)
            if not self._overlay_prog:
                logger.warning("Overlay shader failed, overlays disabled")

            # ── Checkerboard shader ──
            checker_vert = b"""
                #version 330
                in vec2 in_vert;
                in vec2 in_uv;
                out vec2 v_uv;
                void main() {
                    gl_Position = vec4(in_vert, 0.0, 1.0);
                    v_uv = in_uv;
                }
            """
            checker_frag = b"""
                #version 330
                uniform sampler2D u_checker;
                uniform vec2 u_scale;
                in vec2 v_uv;
                out vec4 fragColor;
                void main() {
                    vec2 uv = v_uv * u_scale;
                    fragColor = texture(u_checker, uv);
                }
            """
            self._checker_prog = self._compile_program(checker_vert, checker_frag)
            if not self._checker_prog:
                self._init_error = "Failed to compile checkerboard shader"
                logger.error(f"{self._init_error}, aborting GL init")
                # Release the already-created texture shader program
                _gl.glDeleteProgram(self._prog)
                self._prog = None
                return

            # ── Dynamic VBO (for changing vertices) ──
            vbo = ctypes.c_uint()
            _gl.glGenBuffers(1, ctypes.byref(vbo))
            self._quad_vbo = vbo.value
            _gl.glBindBuffer(GL_ARRAY_BUFFER, self._quad_vbo)
            # Pre-allocate with room for 4 vertices (position + UV, 4 floats each)
            empty = np.zeros(16, dtype=np.float32)
            _gl.glBufferData(GL_ARRAY_BUFFER, empty.nbytes, empty, GL_DYNAMIC_DRAW)
            _gl.glBindBuffer(GL_ARRAY_BUFFER, 0)

            # ── VAO ──
            vao = ctypes.c_uint()
            _gl.glGenVertexArrays(1, ctypes.byref(vao))
            self._vao = vao.value
            _gl.glBindVertexArray(self._vao)
            _gl.glBindBuffer(GL_ARRAY_BUFFER, self._quad_vbo)
            self._pos_loc = _gl.glGetAttribLocation(self._prog, b"in_vert")
            self._uv_loc = _gl.glGetAttribLocation(self._prog, b"in_uv")
            _gl.glEnableVertexAttribArray(self._pos_loc)
            _gl.glVertexAttribPointer(self._pos_loc, 2, GL_FLOAT, GL_FALSE, 16, ctypes.c_void_p(0))
            _gl.glEnableVertexAttribArray(self._uv_loc)
            _gl.glVertexAttribPointer(self._uv_loc, 2, GL_FLOAT, GL_FALSE, 16, ctypes.c_void_p(8))
            _gl.glBindBuffer(GL_ARRAY_BUFFER, 0)
            _gl.glBindVertexArray(0)

            # ── Checkerboard texture ──
            self._init_checkerboard()

            self._gl_initialized = True
            self.gl_initialized.emit()

        except Exception as e:
            self._init_error = str(e)
            logger.exception(f"GL init exception: {e}")
            self._cleanup_partial_init()

    def resizeGL(self, width, height) -> None:
        self._update_tool_handles()
        self._emit_viewer3d_content_size_changed()
        self.update()

    def paintGL(self) -> None:
        if not self._gl_initialized:
            return

        dpr = self.devicePixelRatio()
        vp_w = int(self.width() * dpr)
        vp_h = int(self.height() * dpr)
        _gl.glViewport(0, 0, vp_w, vp_h)

        _gl.glDisable(GL_FRAMEBUFFER_SRGB)
        _vr, _vg, _vb = self._viewport_clear_color_rgb()
        _gl.glClearColor(_vr, _vg, _vb, 1.0)
        _gl.glClear(GL_COLOR_BUFFER_BIT)

        if self._current_tex_id is None or self._current_tex_id <= 0:
            self._update_inspector_from_framebuffer(vp_w, vp_h)
            return

        # Determine texture dimensions
        tw, th = self._tex_width, self._tex_height
        if tw <= 0 or th <= 0:
            # Fallback: query from GL
            tw, th = self._query_texture_size(self._current_tex_id)
            if tw <= 0 or th <= 0:
                self._update_inspector_from_framebuffer(vp_w, vp_h)
                return
            self._tex_width = tw
            self._tex_height = th

        # M2: 四视图模式——4 个象限分别 blit 对应纹理（透视 + 顶/右/前正交）。
        # quad 模式跳过单图像的 letterbox/overlay（bbox/format/selection/tool 等
        # 都是针对单张图像的），只画四象限纹理 + 分割线/标签 overlay + gizmo。
        if self.is_quad_view():
            self._draw_quad_view(vp_w, vp_h, tw, th)
            # gizmo 画在活动象限内（投影/hit-test 已按 active_view 适配）。
            self._draw_gizmo_overlay()
            self._draw_node_icons_overlay()
            self._draw_3d_viewport_overlay()
            self._update_inspector_from_framebuffer(vp_w, vp_h)
            if self.video_control_bar.is_playing:
                self._toolbar.record_presented_frame(self.video_control_bar.current_frame)
            return

        # Compute letterbox + zoom + pan vertices
        # 2D 和 3D 用同一套 letterbox 显示（项目比例纹理居中）：
        #  - 视角一致：2D/3D 都是同一个项目比例摄影机渲染的纹理。
        #  - 零变形：letterbox 按纹理比例缩放，绝不拉伸。
        #  - resize 稳定：纹理尺寸=项目尺寸（固定），不随视口变 → 无拉伸窗口期。
        x0, y0, x1, y1 = self._compute_vertices(vp_w, vp_h, tw, th)

        # Draw checkerboard behind the image region. B2-4: only in checker
        # background mode — black/white replace the grid with a flat field.
        if (self._background_mode == "checker"
                and self._checkerboard_enabled and self._checker_tex_id):
            self._draw_checkerboard(x0, y0, x1, y1)

        # Draw texture with display shader
        # Restore VAO attribute configuration (overlays may have modified it in previous frame)
        _gl.glBindVertexArray(self._vao)
        _gl.glBindBuffer(GL_ARRAY_BUFFER, self._quad_vbo)
        _gl.glEnableVertexAttribArray(self._pos_loc)
        _gl.glVertexAttribPointer(self._pos_loc, 2, GL_FLOAT, GL_FALSE, 16, ctypes.c_void_p(0))
        _gl.glEnableVertexAttribArray(self._uv_loc)
        _gl.glVertexAttribPointer(self._uv_loc, 2, GL_FLOAT, GL_FALSE, 16, ctypes.c_void_p(8))
        _gl.glBindBuffer(GL_ARRAY_BUFFER, 0)
        _gl.glBindVertexArray(0)

        vertices = np.array([
            x0, y1, 0.0, 1.0,
            x0, y0, 0.0, 0.0,
            x1, y1, 1.0, 1.0,
            x1, y0, 1.0, 0.0,
        ], dtype=np.float32)

        _gl.glBindBuffer(GL_ARRAY_BUFFER, self._quad_vbo)
        # Resize buffer back to 64 bytes if overlays enlarged it
        _gl.glBufferData(GL_ARRAY_BUFFER, 64, ctypes.c_void_p(0), GL_DYNAMIC_DRAW)
        _gl.glBufferSubData(GL_ARRAY_BUFFER, 0, vertices.nbytes, vertices)
        _gl.glBindBuffer(GL_ARRAY_BUFFER, 0)

        _gl.glUseProgram(self._prog)
        _gl.glEnable(GL_BLEND)
        _gl.glBlendFunc(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA)
        _gl.glActiveTexture(GL_TEXTURE0)
        _gl.glBindTexture(GL_TEXTURE_2D, self._current_tex_id)
        # Ensure clamp-to-edge to prevent tiling artifacts from recycled textures
        _gl.glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE)
        _gl.glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE)

        # Set display uniforms
        tex_loc = _gl.glGetUniformLocation(self._prog, b"u_texture")
        _gl.glUniform1i(tex_loc, 0)
        ch_loc = _gl.glGetUniformLocation(self._prog, b"u_channel_mode")
        _gl.glUniform1i(ch_loc, self._channel_mode)
        exp_loc = _gl.glGetUniformLocation(self._prog, b"u_exposure")
        _gl.glUniform1f(exp_loc, ctypes.c_float(self._exposure))
        gamma_loc = _gl.glGetUniformLocation(self._prog, b"u_viewer_gamma")
        _gl.glUniform1f(gamma_loc, ctypes.c_float(self._viewer_gamma))
        for uname, uval in ((b"u_viewer_gain", self._viewer_gain),
                            (b"u_viewer_lift", self._viewer_lift),
                            (b"u_viewer_offset", self._viewer_offset)):
            loc_g = _gl.glGetUniformLocation(self._prog, uname)
            if loc_g >= 0:
                _gl.glUniform1f(loc_g, ctypes.c_float(uval))
        # u_display_transfer: 0=linear(none), 1=sRGB, 2=Rec.709, 3=PQ, 4=HLG.
        # Back-compat: if the compiled shader still exposes the old
        # u_display_srgb bool, set it too (older shaders fall back to it).
        dt_loc = _gl.glGetUniformLocation(self._prog, b"u_display_transfer")
        if dt_loc >= 0:
            _gl.glUniform1i(dt_loc, int(self._display_transfer))
        display_srgb_loc = _gl.glGetUniformLocation(self._prog, b"u_display_srgb")
        if display_srgb_loc >= 0:
            _gl.glUniform1i(display_srgb_loc, 1 if self._display_srgb else 0)
        # Stage 2: gamut (primaries) + HDR tonemap for preview. Guard each
        # against older shaders that lack the uniform.
        dp_loc = _gl.glGetUniformLocation(self._prog, b"u_display_primaries")
        if dp_loc >= 0:
            _gl.glUniform1i(dp_loc, int(getattr(self, "_display_primaries", 0)))
        tm_loc = _gl.glGetUniformLocation(self._prog, b"u_tonemap")
        if tm_loc >= 0:
            _gl.glUniform1i(tm_loc, int(getattr(self, "_tonemap", 0)))
        tp_loc = _gl.glGetUniformLocation(self._prog, b"u_target_peak")
        if tp_loc >= 0:
            _gl.glUniform1f(tp_loc, ctypes.c_float(getattr(self, "_target_peak", 100.0)))

        # C6-2: OCIO ODT 3D LUT (single-image 2D path). Bake is cached per
        # display-space descriptor; when unavailable the builtin GLSL ODT
        # above stays in charge (u_odt_lut_active = 0).
        odt_active_loc = _gl.glGetUniformLocation(self._prog, b"u_odt_lut_active")
        odt_ready = False
        if odt_active_loc >= 0:
            if not self.is_quad_view():
                try:
                    odt_ready = self._ensure_odt_lut()
                except Exception:
                    odt_ready = False
            _gl.glUniform1i(odt_active_loc, 1 if odt_ready else 0)
        lut_loc = _gl.glGetUniformLocation(self._prog, b"u_odt_lut")
        size_loc = _gl.glGetUniformLocation(self._prog, b"u_odt_lut_size")
        # Sampler uniforms are validated at draw time even when the runtime
        # branch does not sample the LUT. Keep the active sampler3D off unit
        # 0, which already contains the main sampler2D image.
        if lut_loc >= 0:
            _gl.glUniform1i(lut_loc, 2)
        if odt_ready and lut_loc >= 0:
            _gl.glActiveTexture(GL_TEXTURE2)
            _gl.glBindTexture(GL_TEXTURE_3D, self._odt_lut_tex_id)
            if size_loc >= 0:
                _gl.glUniform1f(size_loc, ctypes.c_float(
                    float(self._ODT_LUT_SIZE)))
            _gl.glActiveTexture(GL_TEXTURE0)

        # View tools (ported from Color page).
        # Mask (alpha) preview mode: 0=off, 1=alpha-as-luma, 2=checker overlay.
        ap_loc = _gl.glGetUniformLocation(self._prog, b"u_alpha_preview")
        if ap_loc >= 0:
            _gl.glUniform1i(ap_loc, int(getattr(self, "_alpha_preview", 0)))
        # Split view: bind the captured "before" texture to TU1.
        split_loc = _gl.glGetUniformLocation(self._prog, b"u_split_active")
        split_active = int(getattr(self, "_split_active", False))
        # Only activate split when a valid secondary texture is present.
        if split_active and not (self._secondary_tex_id and self._secondary_tex_id > 0):
            split_active = 0
        if split_loc >= 0:
            _gl.glUniform1i(split_loc, split_active)
        split_mode_loc = _gl.glGetUniformLocation(self._prog, b"u_split_mode")
        if split_mode_loc >= 0:
            _gl.glUniform1i(
                split_mode_loc,
                1 if getattr(self, "_split_compare_mode", "wipe") == "difference" else 0,
            )
        difference_scale_loc = _gl.glGetUniformLocation(
            self._prog, b"u_difference_scale")
        if difference_scale_loc >= 0:
            _gl.glUniform1f(
                difference_scale_loc,
                ctypes.c_float(getattr(self, "_split_difference_scale", 4.0)),
            )
        sec_tex_loc = _gl.glGetUniformLocation(self._prog, b"u_texture_secondary")
        if split_active == 1 and sec_tex_loc >= 0:
            _gl.glActiveTexture(GL_TEXTURE1)
            _gl.glBindTexture(GL_TEXTURE_2D, self._secondary_tex_id)
            _gl.glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE)
            _gl.glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE)
            _gl.glUniform1i(sec_tex_loc, 1)
            sp_loc = _gl.glGetUniformLocation(self._prog, b"u_split_pos")
            if sp_loc >= 0:
                _gl.glUniform1f(sp_loc, ctypes.c_float(getattr(self, "_split_pos", 0.5)))
            # Restore active texture unit to TU0 for subsequent state.
            _gl.glActiveTexture(GL_TEXTURE0)

        _gl.glBindVertexArray(self._vao)
        _gl.glDrawArrays(GL_TRIANGLE_STRIP, 0, 4)
        _gl.glBindVertexArray(0)

        _gl.glBindTexture(GL_TEXTURE_2D, 0)
        _gl.glDisable(GL_BLEND)
        _gl.glUseProgram(0)

        # Draw bbox/format overlays
        self._draw_overlays(x0, y0, x1, y1, tw, th)

        # 2D/3D 共用项目比例 letterbox 显示，letterbox 区即导出范围，
        # 不再单独画 Resolution Gate（避免与 letterbox 双重压暗）。

        # Draw selection overlays (S5-10)
        self._draw_selection_overlays(x0, y0, x1, y1, tw, th)

        # Draw tool handles (S5-09)
        self._draw_tool_handles(x0, y0, x1, y1, tw, th)

        # Phase 3: transform gizmo (3D mode) — drawn last so it sits on top.
        self._draw_gizmo_overlay()

        # P4: Camera3D/Light3D 节点图标（3D 模式下显示，不进合成输出）。
        self._draw_node_icons_overlay()

        # Phase 4: 四视图叠加层（分割线/活动边框/标签），3D + QUAD 时绘制。
        # 在所有 GL 绘制之后用 QPainter 画屏幕空间辅助元素（不写深度、不进合成）。
        self._draw_3d_viewport_overlay()

        # Guides overlay (aspect-ratio / safe-area) — ported from Color page.
        self._draw_guides_overlay()
        # Split-view divider line + drag handle.
        self._draw_split_overlay()
        # B1: user ROI frame + outside dimming.
        self._draw_roi_overlay()
        # B2-3: magnifier loupe (cursor-following, nearest-neighbour).
        self._draw_magnifier_overlay(vp_w, vp_h)

        # Read pixel at mouse position for inspector
        self._update_inspector_from_framebuffer(vp_w, vp_h)

        # Count the frame after the viewer has completed its paint pass. The
        # transport frame token prevents ordinary repaints from inflating FPS.
        if self.video_control_bar.is_playing:
            self._toolbar.record_presented_frame(self.video_control_bar.current_frame)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if self._gl_initialized:
            self.makeCurrent()
            self.update()
        # Update video control bar position when shown
        self.update_video_control_bar_position()

    def _relayout_overlay_bars(self) -> None:
        """Reposition floating overlay bars (top toolbar / inspector).

        与 resizeEvent 解耦，使非 resize 场景可安全触发重新布局，而不必用
        resizeEvent(None) —— 后者会把 None 传给 QWidget.resizeEvent 的 C++
        实现在 PyQt6 下导致段崩溃。3D 工具按钮已合并进主工具栏（不再是浮层）。
        """
        # Reposition toolbar at top
        if hasattr(self, '_toolbar'):
            self._toolbar.setGeometry(0, 0, self.width(), self._toolbar.height())
        # Reposition inspector bar at bottom (if visible)
        if hasattr(self, '_inspector_bar'):
            bar_h = self._inspector_bar.height()
            self._inspector_bar.setGeometry(0, self.height() - bar_h, self.width(), bar_h)
        # Update video control bar position
        self.update_video_control_bar_position()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._relayout_overlay_bars()
        self._emit_viewer3d_content_size_changed()

    def update_video_control_bar_position(self) -> None:
        """Update video control bar position to bottom of viewer."""
        if hasattr(self, 'video_control_bar') and self.video_control_bar.isVisible():
            bar = self.video_control_bar
            bar_width = self.width()
            bar_height = bar.height()
            # Position at bottom, accounting for inspector bar if visible
            inspector_height = 0
            if hasattr(self, '_inspector_bar') and self._inspector_bar.isVisible():
                inspector_height = self._inspector_bar.height()
            # Video control bar sits above inspector bar
            bar.setGeometry(0, self.height() - bar_height - inspector_height, bar_width, bar_height)

    def contextMenuEvent(self, event) -> None:
        """Right-click context menu."""
        # Phase 2: suppress the context menu in 3D mode so Alt+right-drag
        # (dolly) doesn't pop up the 2D zoom/channel menu on release.
        if self._view_mode == "3d":
            event.accept()
            return
        menu = QMenu(self)

        menu.addAction(I18N.tr("viewport.fit", "Fit to Window"), self.fit_to_window)
        menu.addAction(I18N.tr("viewport.actual_size", "Actual Size"), self.actual_size)
        menu.addSeparator()
        menu.addAction(I18N.tr("viewport.zoom_in", "Zoom In"), self.zoom_in)
        menu.addAction(I18N.tr("viewport.zoom_out", "Zoom Out"), self.zoom_out)
        menu.addSeparator()

        # Channel submenu
        ch_menu = menu.addMenu("Channel")
        for mode_id, full_name, _ in CHANNEL_MODES:
            action = ch_menu.addAction(full_name)
            action.setCheckable(True)
            action.setChecked(self._channel_mode == mode_id)
            action.triggered.connect(lambda checked, m=mode_id: self.set_channel_mode(m))

        # Exposure submenu
        exp_menu = menu.addMenu("Exposure")
        exp_menu.addAction(I18N.tr("viewport.exp_plus_1_stop", "+1 Stop"),
                           lambda: self._adjust_exposure_by(1.0))
        exp_menu.addAction(I18N.tr("viewport.exp_minus_1_stop", "-1 Stop"),
                           lambda: self._adjust_exposure_by(-1.0))
        exp_menu.addAction(I18N.tr("viewport.reset", "Reset"),
                           lambda: self.set_exposure(0.0))

        # Overlay toggles
        menu.addSeparator()
        bbox_action = menu.addAction(I18N.tr("viewport.show_bbox", "Show BBox"))
        bbox_action.setCheckable(True)
        bbox_action.setChecked(self._show_bbox_overlay)
        bbox_action.toggled.connect(self._set_bbox_overlay)

        fmt_action = menu.addAction(I18N.tr("viewport.show_format", "Show Format"))
        fmt_action.setCheckable(True)
        fmt_action.setChecked(self._show_format_overlay)
        fmt_action.toggled.connect(self._set_format_overlay)

        inspector_action = menu.addAction(
            I18N.tr("viewport.show_pixel_inspector", "Show Pixel Inspector"))
        inspector_action.setCheckable(True)
        inspector_action.setChecked(self._inspector_bar_user_visible)
        def _toggle_inspector(checked) -> None:
            self._inspector_bar_user_visible = checked
            self._inspector_bar.setVisible(checked)
        inspector_action.toggled.connect(_toggle_inspector)

        menu.addSeparator()
        menu.addAction(I18N.tr("viewport.dock_float", "Dock/Float"), self._toggle_dock_float)

        menu.exec(event.globalPos())

    def _toggle_fullscreen(self) -> None:
        window = self.window()
        if window:
            if window.isFullScreen():
                window.showNormal()
            else:
                window.showFullScreen()

    def _toggle_focus_mode(self) -> None:
        window = self.window()
        if hasattr(window, 'toggle_focus_mode'):
            window.toggle_focus_mode()

    def _toggle_dock_float(self) -> None:
        """Toggle the viewport dock between docked and floating."""
        window = self.window()
        if hasattr(window, 'dock_viewport') and window.dock_viewport:
            dock = window.dock_viewport
            dock.setFloating(not dock.isFloating())

    def cleanup(self) -> None:
        """Release OpenGL resources. Safe to call even after partial init."""
        # B2-1: don't leave the auto-proxy hint latched while tearing down.
        self._end_auto_proxy_interaction()
        # C6-2: release the baked OCIO ODT LUT.
        self._delete_odt_lut_texture()
        self._delete_owned_display_texture()
        self._delete_owned_aov_textures()
        self._delete_owned_secondary_texture()
        self._delete_owned_secondary_b_texture()
        self._delete_copy_framebuffers()
        if self._quad_vbo:
            vbo = ctypes.c_uint(self._quad_vbo)
            _gl.glDeleteBuffers(1, ctypes.byref(vbo))
            self._quad_vbo = None
        if self._vao:
            vao = ctypes.c_uint(self._vao)
            _gl.glDeleteVertexArrays(1, ctypes.byref(vao))
            self._vao = None
        if self._prog:
            _gl.glDeleteProgram(self._prog)
            self._prog = None
        if self._checker_prog:
            _gl.glDeleteProgram(self._checker_prog)
            self._checker_prog = None
        if self._overlay_prog:
            _gl.glDeleteProgram(self._overlay_prog)
            self._overlay_prog = None
        if self._checker_tex_id:
            tex = ctypes.c_uint(self._checker_tex_id)
            _gl.glDeleteTextures(1, ctypes.byref(tex))
            self._checker_tex_id = None
        self._gl_initialized = False

    def _delete_owned_aov_texture(self, pass_name: str,
                                  assume_current: bool = False) -> None:
        """Release one viewer-owned auxiliary pass texture."""
        texture_id = int(self._owned_aov_tex_ids.pop(pass_name, 0) or 0)
        self._owned_aov_tex_sizes.pop(pass_name, None)
        self._owned_aov_tex_storage.pop(pass_name, None)
        if texture_id <= 0:
            return
        made_current = False
        try:
            if self._gl_initialized and not assume_current:
                self.makeCurrent()
                made_current = True
            tex = ctypes.c_uint(texture_id)
            _gl.glDeleteTextures(1, ctypes.byref(tex))
        except Exception:
            pass
        finally:
            if made_current:
                self.doneCurrent()

    def _delete_owned_aov_textures(self, assume_current: bool = False) -> None:
        """Release all viewer-owned auxiliary pass textures."""
        for pass_name in list(self._owned_aov_tex_ids):
            self._delete_owned_aov_texture(
                pass_name, assume_current=assume_current)
        self._owned_aov_tex_sizes.clear()
        self._owned_aov_tex_storage.clear()

    def _delete_owned_display_texture(self, assume_current: bool = False) -> None:
        if not self._owned_display_tex_id:
            self._owned_display_tex_size = None
            return
        made_current = False
        try:
            if self._gl_initialized and not assume_current:
                self.makeCurrent()
                made_current = True
            tex = ctypes.c_uint(self._owned_display_tex_id)
            _gl.glDeleteTextures(1, ctypes.byref(tex))
        except Exception:
            pass
        finally:
            if made_current:
                self.doneCurrent()
        self._owned_display_tex_id = None
        self._owned_display_tex_size = None

    # ── Split-view secondary texture (viewer-owned, safe snapshot) ────────

    def _snapshot_display_texture_to_secondary(self) -> bool:
        """Copy the current display texture into the active compare slot."""
        return self._snapshot_display_texture_to_slot(self._ab_active_slot)

    def _ab_owned_tex_id(self, slot: str) -> int:
        """B2-2: viewer-owned texture id backing compare slot ``slot``."""
        return (self._owned_secondary_b_tex_id if slot == "B"
                else self._owned_secondary_tex_id)

    def _snapshot_display_texture_to_slot(self, slot: str) -> bool:
        """Copy the current owned display texture into compare slot A or B.

        Used by split view / A-B buffers to provide a stable comparison frame
        that is not owned by the renderer's texture cache (so it cannot be
        evicted mid-paint). Mirrors the FBO blit used for the primary display
        copy. On success the slot becomes the active compare source.
        """
        slot = "B" if str(slot).upper().startswith("B") else "A"
        owned_attr = ("_owned_secondary_b_tex_id" if slot == "B"
                      else "_owned_secondary_tex_id")
        size_attr = ("_owned_secondary_b_tex_size" if slot == "B"
                     else "_owned_secondary_tex_size")
        source_tex_id = self._current_tex_id
        source_size = (
            self._owned_aov_tex_sizes.get(self._selected_render_pass)
            if self._selected_render_pass != "beauty"
            else self._owned_display_tex_size
        )
        if not source_tex_id or not source_size:
            return False
        width, height = source_size

        made_current = False
        try:
            if self._gl_initialized:
                self.makeCurrent()
                made_current = True
            # AUD-9705: glIsTexture needs a current context. Probing the
            # source name before makeCurrent() raised GLError from Qt slot
            # context (e.g. the Ctrl+Shift+A toolbar action), which the
            # except below swallowed into a silent snapshot failure. Match
            # set_texture's order: make current first, query second.
            if not _gl.glIsTexture(source_tex_id):
                return False
            owned_id = getattr(self, owned_attr)
            if owned_id is None or owned_id <= 0:
                owned_id = int(_gl.glGenTextures(1))
                setattr(self, owned_attr, owned_id)

            _gl.glBindTexture(GL_TEXTURE_2D, owned_id)
            _gl.glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR)
            _gl.glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR)
            _gl.glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE)
            _gl.glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE)
            if getattr(self, size_attr) != (width, height):
                _gl.glTexImage2D(
                    GL_TEXTURE_2D, 0, GL_RGBA16F, width, height, 0,
                    GL_RGBA, GL_HALF_FLOAT, None,
                )
                setattr(self, size_attr, (width, height))

            if self._copy_read_fbo is None:
                self._copy_read_fbo = int(_gl.glGenFramebuffers(1))
            if self._copy_draw_fbo is None:
                self._copy_draw_fbo = int(_gl.glGenFramebuffers(1))

            _gl.glBindFramebuffer(GL_READ_FRAMEBUFFER, self._copy_read_fbo)
            _gl.glFramebufferTexture2D(
                GL_READ_FRAMEBUFFER, GL_COLOR_ATTACHMENT0, GL_TEXTURE_2D,
                source_tex_id, 0,
            )
            _gl.glBindFramebuffer(GL_DRAW_FRAMEBUFFER, self._copy_draw_fbo)
            _gl.glFramebufferTexture2D(
                GL_DRAW_FRAMEBUFFER, GL_COLOR_ATTACHMENT0, GL_TEXTURE_2D,
                owned_id, 0,
            )
            _gl.glBlitFramebuffer(
                0, 0, width, height,
                0, 0, width, height,
                GL_COLOR_BUFFER_BIT, GL_NEAREST,
            )
            ok = _gl.glGetError() == 0
            if ok:
                self._ab_active_slot = slot
                self._secondary_tex_id = owned_id
            return ok
        except Exception as exc:
            logger.warning("CompositeViewer secondary texture snapshot failed: %s", exc)
            return False
        finally:
            if self._copy_read_fbo is not None or self._copy_draw_fbo is not None:
                _gl.glBindFramebuffer(GL_FRAMEBUFFER, self.defaultFramebufferObject())
            _gl.glBindTexture(GL_TEXTURE_2D, 0)
            if made_current:
                self.doneCurrent()

    def _delete_owned_secondary_texture(self, assume_current: bool = False) -> None:
        """Release the split-view secondary texture."""
        if not self._owned_secondary_tex_id:
            self._owned_secondary_tex_size = None
            self._secondary_tex_id = 0
            return
        made_current = False
        try:
            if self._gl_initialized and not assume_current:
                self.makeCurrent()
                made_current = True
            tex = ctypes.c_uint(self._owned_secondary_tex_id)
            _gl.glDeleteTextures(1, ctypes.byref(tex))
        except Exception:
            pass
        finally:
            if made_current:
                self.doneCurrent()
        self._owned_secondary_tex_id = 0
        self._owned_secondary_tex_size = None
        self._secondary_tex_id = 0

    def _delete_owned_secondary_b_texture(self, assume_current: bool = False) -> None:
        """B2-2: release the slot-B compare texture."""
        if not self._owned_secondary_b_tex_id:
            self._owned_secondary_b_tex_size = None
            if self._ab_active_slot == "B":
                self._secondary_tex_id = 0
            return
        made_current = False
        try:
            if self._gl_initialized and not assume_current:
                self.makeCurrent()
                made_current = True
            tex = ctypes.c_uint(self._owned_secondary_b_tex_id)
            _gl.glDeleteTextures(1, ctypes.byref(tex))
        except Exception:
            pass
        finally:
            if made_current:
                self.doneCurrent()
        self._owned_secondary_b_tex_id = 0
        self._owned_secondary_b_tex_size = None
        if self._ab_active_slot == "B":
            self._secondary_tex_id = 0

    def _delete_copy_framebuffers(self) -> None:
        """Release the viewer-local FBOs used to copy shared render output."""
        for attr_name in ("_copy_read_fbo", "_copy_draw_fbo"):
            fbo_id = getattr(self, attr_name, None)
            if not fbo_id:
                continue
            try:
                _gl.glDeleteFramebuffers(1, [fbo_id])
            except Exception:
                pass
            setattr(self, attr_name, None)

    # ── Mouse Events (Zoom/Pan) ─────────────────────────────────────────────

    def wheelEvent(self, event: QWheelEvent) -> None:
        delta = event.angleDelta().y()
        if delta == 0:
            return
        # Phase 2: 3D mode — wheel does camera dolly (zoom toward target).
        # Uses a merged-undo coalescing window so continuous scrolling collapses
        # into one undo step (rather than one step per notch).
        if self._view_mode == "3d":
            factor = 1.0 - delta * 0.001
            self._cam_dist = max(0.1, self._cam_dist * factor)
            # Single preview+commit per notch; page merges via coalescing window.
            self.scene3d_nav_started.emit(self._nav_target_node_id())
            self._emit_nav_preview()
            self._emit_nav_committed()
            event.accept()
            return
        factor = 1.15 if delta > 0 else 1.0 / 1.15
        pos = event.position().toPoint()
        # B2-1: wheel zoom is a view interaction (auto resolution hint).
        self._note_auto_proxy_interaction()
        self._apply_zoom(factor, pos)

    # ── Phase 2: 3D View Mode (C4D-style camera navigation) ───────────────

    def set_view_mode(self, mode: str) -> None:
        """Switch the fused viewer between "2d" and "3d" mode.

        In 3D mode, Alt+left/middle/right drag orbit/pan/dolly the camera of
        the selected Scene3D node; the wheel dollies. Display pipeline (blit
        + display shader) is unchanged — only interaction differs.
        """
        mode = "3d" if mode == "3d" else "2d"
        if mode == self._view_mode:
            return
        self._view_mode = mode
        self.view_mode_changed.emit(mode)
        # 阶段七：切模式时重置画面平移缩放，避免残留。
        # 3D 模式用满屏显示（_compute_vertices 3D 分支忽略 zoom/pan），
        # 但残留的 pan/zoom 会在切回 2D 时导致画面跳动，故双向都重置。
        self._zoom_level = 1.0
        self._pan_x = 0.0
        self._pan_y = 0.0
        # Restore a sensible cursor when leaving 3D mode.
        if mode == "2d":
            self.unsetCursor()
        # Phase 4: 3D 工具按钮显隐（3D 模式显示，2D 隐藏并重置）。
        # 3D 工具按钮已合并进主工具栏 CompositeViewerBar（不再用独立浮层）。
        if hasattr(self, '_toolbar'):
            if mode == "3d":
                self._toolbar.show_3d_tools()
            else:
                self._toolbar.hide_3d_tools()
            self._relayout_overlay_bars()
        # Phase 3: gizmo visibility depends on view mode.
        self._update_gizmo_visibility()
        self._last_viewer3d_content_size = None
        self._emit_viewer3d_content_size_changed()
        self.update()

    def get_view_mode(self) -> str:
        """Return the current view mode ("2d" or "3d")."""
        return self._view_mode

    def _reset_3d_camera(self) -> None:
        """Reset the 3D camera to the default framing view (F key).

        Resets orbit yaw/pitch, distance AND target to defaults and commits
        them to the selected Scene3D node (with undo). This is the 3D
        equivalent of the 2D "Fit to Window".
        """
        self._orbit_yaw = 30.0
        self._orbit_pitch = 20.0
        self._cam_dist = 5.0
        self._cam_target_x = 0.0
        self._cam_target_y = 0.0
        self._cam_target_z = 0.0
        self.scene3d_nav_started.emit(self._nav_target_node_id())
        self._emit_nav_preview()
        self._emit_nav_committed()

    def _frame_selected_3d(self) -> bool:
        """P7：把相机定位到选中对象（target 移到对象中心 + dist 按对象尺度 fit）。

        复用 _selected_3d_orbit_center 拿对象中心。dist 按对象类型估算：
        Geo3D/Model3D 用 mesh_scale 估算；Camera3D/Light3D 用固定 dist。
        精确 mesh AABB 聚合需跨线程，首版用估算。
        返回 True 表示执行了 frame（有选中对象），False 表示无选中（调用方回退 reset）。
        """
        center = self._selected_3d_orbit_center()
        if center is None:
            return False
        params = self._selected_node_params or {}
        node_type = self._selected_node_type
        # 估算 fit 距离
        if node_type in ("geo3d", "model3d"):
            ms = max(0.1, float(params.get("mesh_scale", 1.0)))
            fit_dist = max(3.0, ms * 4.0)  # 估算：4 倍对象尺度
        else:
            fit_dist = 5.0  # Camera3D/Light3D 固定
        self._cam_target_x = float(center[0])
        self._cam_target_y = float(center[1])
        self._cam_target_z = float(center[2])
        self._cam_dist = fit_dist
        self.scene3d_nav_started.emit(self._nav_target_node_id())
        self._emit_nav_preview()
        self._emit_nav_committed()
        return True

    def _toggle_isolate_selected(self) -> None:
        """P7：Shift+I toggle 隔离选中对象（只渲染该节点的 mesh）。

        有选中 + 未隔离 → 进入隔离（isolate_node_id = 选中节点）。
        已隔离 → 退出隔离（isolate_node_id = None）。
        无选中 → 退出隔离（保险）。
        toggle 后触发重渲染（isolate 变化经 RenderRequest → eval_ctx）。
        """
        state = self._viewport3d_ctrl.state
        if state.isolate_node_id is not None:
            # 已隔离 → 退出
            state.isolate_node_id = None
        else:
            # 进入隔离：用当前选中节点
            sel_nid = self._selected_3d_node_id_for_isolate()
            if sel_nid is None:
                return  # 无选中，不进入隔离
            state.isolate_node_id = sel_nid
        # 触发重渲染（isolate 经 page request 注入 eval_ctx）
        page = getattr(self, '_composite_page', None)
        if page is not None and hasattr(page, '_force_next_view_refresh'):
            page._force_next_view_refresh = True
            if hasattr(page, '_evaluate_graph'):
                page._evaluate_graph()
        self.update()

    def _selected_3d_node_id_for_isolate(self) -> Optional[str]:
        """获取当前选中的 3D 节点 id（供 isolate 用）。"""
        scene_id = self.get_selected_3d_scene_node()
        if scene_id:
            return scene_id
        nid = getattr(self, '_viewing_node_id', None)
        if nid:
            return str(nid)
        sel = getattr(self, '_interaction_state', None)
        if sel is not None:
            snid = getattr(sel, 'selected_node_id', None)
            if snid:
                return str(snid)
        return None

    def sync_scene3d_params(self, params: dict) -> None:
        """Sync the internal 3D nav state from node params (e.g. on undo/redo).

        Keeps the cursor continuous so redo re-applies smoothly. Also syncs the
        object position so the gizmo tracks undo/redo of object_pos_*.
        """
        if "cam_orbit_yaw" in params:
            self._orbit_yaw = float(params["cam_orbit_yaw"])
        if "cam_orbit_pitch" in params:
            self._orbit_pitch = float(params["cam_orbit_pitch"])
        if "cam_dist" in params:
            self._cam_dist = float(params["cam_dist"])
        if "cam_target_x" in params:
            self._cam_target_x = float(params["cam_target_x"])
        if "cam_target_y" in params:
            self._cam_target_y = float(params["cam_target_y"])
        if "cam_target_z" in params:
            self._cam_target_z = float(params["cam_target_z"])
        if self._selected_node_type == "camera3d" and any(k in params for k in (
                "camera_mode", "cam_eye_x", "cam_eye_y", "cam_eye_z",
                "cam_target_x", "cam_target_y", "cam_target_z",
                "cam_dist", "cam_orbit_yaw", "cam_orbit_pitch")):
            self._gizmo_obj_pos = self._camera_gizmo_anchor(
                {**self._selected_node_params, **params})
        if self._selected_node_type == "light3d" and any(k in params for k in (
                "light_type", "light_pos_x", "light_pos_y", "light_pos_z",
                "icon_pos_x", "icon_pos_y", "icon_pos_z")):
            self._gizmo_obj_pos = self._light_gizmo_anchor(
                {**self._selected_node_params, **params})
        # Phase 3: keep the gizmo's object anchor in sync (undo/redo of pos).
        if any(k in params for k in ("object_pos_x", "object_pos_y", "object_pos_z")):
            ox = float(params.get("object_pos_x", self._gizmo_obj_pos[0]))
            oy = float(params.get("object_pos_y", self._gizmo_obj_pos[1]))
            oz = float(params.get("object_pos_z", self._gizmo_obj_pos[2]))
            self._gizmo_obj_pos = (ox, oy, oz)

    # ── Phase 3: Transform Gizmo ──────────────────────────────────────────

    def _update_gizmo_visibility(self) -> None:
        """Recompute whether the transform gizmo should be shown.

        P0 缺口修复：gizmo 在选中 Scene3D / Geo3D / Model3D /
        Camera3D / Light3D 时都显示。Camera3D/Light3D 没有通用
        object_pos_*，但在视口里应作为真实辅助对象操作，因此把 gizmo
        锚点映射到各自的位置参数。

        F13 扩展：选中 Deformer3D 时也显示 gizmo，锚点映射到变形中心
        center_x/y/z，拖拽 gizmo 改变变形中心位置（而非 object_pos）。
        """
        was = self._gizmo_visible
        self._gizmo_visible = (self._view_mode == "3d"
                               and self._selected_node_type in
                               ("scene3d", "geo3d", "model3d",
                                "camera3d", "light3d", "deformer3d")
                               and not self._selected_camera_is_active_viewer_camera())
        # 切走时清掉 hover/drag 残留
        if not self._gizmo_visible:
            self._transform_gizmo.hovered_axis = None
            self._transform_gizmo.end_drag()
            self._gizmo_drag_active = False
        else:
            # 选中几何节点时同步 object_pos；Camera3D/Light3D 同步各自锚点。
            params = self._selected_node_params or {}
            if self._selected_node_type == "camera3d":
                self._gizmo_obj_pos = self._camera_gizmo_anchor(params)
            elif self._selected_node_type == "light3d":
                self._gizmo_obj_pos = self._light_gizmo_anchor(params)
            elif self._selected_node_type == "deformer3d":
                # F13：Deformer3D gizmo 锚点 = 变形中心 center。
                # 注：center 是对象空间；若上游 Model3D 有 transform，世界空间
                # 位置需乘 world_matrix。第一版简化为对象空间≈世界空间（假设
                # Model3D 在原点无变换），后续可加 world_matrix 变换。
                self._gizmo_obj_pos = (
                    float(params.get("center_x", 0.0)),
                    float(params.get("center_y", 0.0)),
                    float(params.get("center_z", 0.0)))
            else:
                self._gizmo_obj_pos = (
                    float(params.get("object_pos_x", 0.0)),
                    float(params.get("object_pos_y", 0.0)),
                    float(params.get("object_pos_z", 0.0)))
        if was != self._gizmo_visible:
            self.update()

    def _selected_camera_is_active_viewer_camera(self) -> bool:
        """Return True when the selected Camera3D is driving the 3D viewport."""
        if self._selected_node_type != "camera3d":
            return False
        try:
            selected_id = self._interaction_state.selected_node_id
            page = getattr(self, "_composite_page", None)
            active_id = getattr(page, "_viewer3d_active_camera_id", None)
            return bool(selected_id and active_id
                        and str(selected_id) == str(active_id))
        except Exception:
            return False

    def _camera_gizmo_anchor(self, params: dict) -> tuple:
        """Return the world-space Camera3D body position for the gizmo."""
        try:
            mode = str(params.get("camera_mode", "orbit"))
            if mode == "free":
                return (
                    float(params.get("cam_eye_x", 4.0)),
                    float(params.get("cam_eye_y", 3.0)),
                    float(params.get("cam_eye_z", 5.0)),
                )
            from src.features.composite.render3d import math_utils as m3d
            target = m3d.vec3(
                float(params.get("cam_target_x", 0.0)),
                float(params.get("cam_target_y", 0.0)),
                float(params.get("cam_target_z", 0.0)))
            eye = m3d.orbit_to_eye(
                max(0.1, float(params.get("cam_dist", 5.0))),
                float(params.get("cam_orbit_yaw", 30.0)),
                float(params.get("cam_orbit_pitch", 20.0)),
                target=target)
            return tuple(float(v) for v in eye)
        except Exception:
            return (0.0, 0.0, 0.0)

    def _light_gizmo_anchor(self, params: dict) -> tuple:
        """Return the world-space Light3D icon/body position for the gizmo."""
        keys = self._light_gizmo_param_keys(params)
        defaults = (0.0, 0.0, 0.0)
        try:
            return (
                float(params.get(keys[0], defaults[0])),
                float(params.get(keys[1], defaults[1])),
                float(params.get(keys[2], defaults[2])),
            )
        except Exception:
            return defaults

    def _light_gizmo_param_keys(self, params: dict) -> tuple[str, str, str]:
        """Return Light3D position keys edited by the transform gizmo."""
        light_type = str(params.get("light_type", "directional"))
        if light_type in ("point", "spot"):
            return ("light_pos_x", "light_pos_y", "light_pos_z")
        return ("icon_pos_x", "icon_pos_y", "icon_pos_z")

    def _camera_source_eye_target(self, params: dict) -> tuple:
        """Return Camera3D eye/target vectors from the current parameter set."""
        from src.features.composite.render3d import math_utils as m3d
        target = m3d.vec3(
            float(params.get("cam_target_x", 0.0)),
            float(params.get("cam_target_y", 0.0)),
            float(params.get("cam_target_z", 0.0)))
        mode = str(params.get("camera_mode", "orbit"))
        if mode == "free":
            eye = m3d.vec3(
                float(params.get("cam_eye_x", 4.0)),
                float(params.get("cam_eye_y", 3.0)),
                float(params.get("cam_eye_z", 5.0)))
        else:
            eye = m3d.orbit_to_eye(
                max(0.1, float(params.get("cam_dist", 5.0))),
                float(params.get("cam_orbit_yaw", 30.0)),
                float(params.get("cam_orbit_pitch", 20.0)),
                target=target)
        return eye, target

    def _orbit_angles_from_eye_target(self, eye, target) -> tuple[float, float, float]:
        """Return (dist, yaw, pitch) matching orbit_to_eye semantics."""
        import math as _math
        import numpy as _np
        offset = _np.asarray(eye, dtype=_np.float64) - _np.asarray(target, dtype=_np.float64)
        dist = max(0.1, float(_np.linalg.norm(offset)))
        if dist <= 1e-6:
            return 0.1, 0.0, 0.0
        yaw = float(_math.degrees(_math.atan2(offset[0], offset[2])))
        horiz = float(_math.sqrt(offset[0] * offset[0] + offset[2] * offset[2]))
        pitch = float(_math.degrees(_math.atan2(offset[1], horiz)))
        return dist, yaw, max(-89.0, min(89.0, pitch))

    def _camera_gizmo_result_to_params(self, result: dict) -> dict:
        """Map generic gizmo object_pos/rot/scale results to Camera3D params."""
        if self._selected_node_type != "camera3d":
            return result
        import numpy as _np
        from src.features.composite.render3d import math_utils as m3d
        source = self._gizmo_drag_source_params or (self._selected_node_params or {})
        mode = str(source.get("camera_mode", "orbit"))
        start_anchor = _np.asarray(self._gizmo_drag_anchor_start, dtype=_np.float64)
        eye, target = self._camera_source_eye_target(source)
        out: dict = {}

        # MOVE: treat the camera body as a scene object. In free mode move eye
        # and target together; in orbit/ortho, moving target moves the derived
        # eye by the same offset while preserving yaw/pitch/dist.
        moved = any(k in result for k in ("object_pos_x", "object_pos_y", "object_pos_z"))
        if moved:
            new_anchor = start_anchor.copy()
            if "object_pos_x" in result:
                new_anchor[0] = float(result["object_pos_x"])
            if "object_pos_y" in result:
                new_anchor[1] = float(result["object_pos_y"])
            if "object_pos_z" in result:
                new_anchor[2] = float(result["object_pos_z"])
            delta = new_anchor - start_anchor
            new_target = _np.asarray(target, dtype=_np.float64) + delta
            out.update({
                "cam_target_x": float(new_target[0]),
                "cam_target_y": float(new_target[1]),
                "cam_target_z": float(new_target[2]),
            })
            if mode == "free":
                new_eye = _np.asarray(eye, dtype=_np.float64) + delta
                out.update({
                    "cam_eye_x": float(new_eye[0]),
                    "cam_eye_y": float(new_eye[1]),
                    "cam_eye_z": float(new_eye[2]),
                })

        # ROTATE: map C4D-like ring axes onto camera orbit angles / roll.
        _, source_yaw, source_pitch = self._orbit_angles_from_eye_target(eye, target)
        yaw = float(source.get("cam_orbit_yaw", source_yaw))
        pitch = float(source.get("cam_orbit_pitch", source_pitch))
        roll = float(source.get("cam_roll", 0.0))
        if "object_rot_y" in result:
            yaw = float(result["object_rot_y"])
            out["cam_orbit_yaw"] = yaw
        if "object_rot_x" in result:
            pitch = max(-89.0, min(89.0, float(result["object_rot_x"])))
            out["cam_orbit_pitch"] = pitch
        if "object_rot_z" in result:
            roll = float(result["object_rot_z"])
            out["cam_roll"] = roll
        if mode == "free" and any(k in result for k in (
                "object_rot_x", "object_rot_y")):
            dist = max(0.1, float(_np.linalg.norm(_np.asarray(eye) - _np.asarray(target))))
            offset = m3d.orbit_to_eye(dist, yaw, pitch, target=m3d.vec3(0.0, 0.0, 0.0))
            new_target = _np.asarray(eye, dtype=_np.float64) - _np.asarray(offset, dtype=_np.float64)
            out.update({
                "cam_target_x": float(new_target[0]),
                "cam_target_y": float(new_target[1]),
                "cam_target_z": float(new_target[2]),
            })

        # SCALE: for camera objects, scale means dolly/focal framing distance.
        if "mesh_scale" in result:
            factor = max(0.01, float(result["mesh_scale"]))
            if mode == "ortho":
                out["cam_ortho_scale"] = max(
                    0.1, float(source.get("cam_ortho_scale", 5.0)) * factor)
            elif mode == "free":
                eye_np = _np.asarray(eye, dtype=_np.float64)
                target_np = _np.asarray(target, dtype=_np.float64)
                new_target = eye_np + (target_np - eye_np) * factor
                out.update({
                    "cam_target_x": float(new_target[0]),
                    "cam_target_y": float(new_target[1]),
                    "cam_target_z": float(new_target[2]),
                })
            else:
                out["cam_dist"] = max(0.1, float(source.get("cam_dist", 5.0)) * factor)
        return out

    def _light_gizmo_result_to_params(self, result: dict) -> dict:
        """Map generic gizmo object_pos results to Light3D position params."""
        if self._selected_node_type != "light3d":
            return result
        import numpy as _np
        source = self._gizmo_drag_source_params or (self._selected_node_params or {})
        keys = self._light_gizmo_param_keys(source)
        start_anchor = _np.asarray(self._gizmo_drag_anchor_start, dtype=_np.float64)
        out: dict = {}
        moved = any(k in result for k in ("object_pos_x", "object_pos_y", "object_pos_z"))
        if moved:
            new_anchor = start_anchor.copy()
            if "object_pos_x" in result:
                new_anchor[0] = float(result["object_pos_x"])
            if "object_pos_y" in result:
                new_anchor[1] = float(result["object_pos_y"])
            if "object_pos_z" in result:
                new_anchor[2] = float(result["object_pos_z"])
            out.update({
                keys[0]: float(new_anchor[0]),
                keys[1]: float(new_anchor[1]),
                keys[2]: float(new_anchor[2]),
            })
        return out

    def _deformer_gizmo_result_to_params(self, result: dict) -> dict:
        """F13：把通用 gizmo object_pos 结果映射到 Deformer3D 的 center 参数。

        gizmo 的 drag_offset 永远产出 object_pos_*（_AXIS_PARAM 硬编码），
        Deformer3D 的锚点是 center_x/y/z，所以这里做 1:1 映射：
        object_pos_x → center_x，依此类推。

        注：center 是对象空间，gizmo 工作在世界空间。若上游 Model3D 有
        object_pos 变换，这里应有 world→object 逆变换。第一版简化为两者
        一致（假设 Model3D 在原点无变换）。
        """
        if self._selected_node_type != "deformer3d":
            return result
        out: dict = {}
        if "object_pos_x" in result:
            out["center_x"] = float(result["object_pos_x"])
        if "object_pos_y" in result:
            out["center_y"] = float(result["object_pos_y"])
        if "object_pos_z" in result:
            out["center_z"] = float(result["object_pos_z"])
        return out

    def look_through_camera_params(self, params: dict) -> None:
        """Set the viewer navigation camera from a Camera3D node without wiring it."""
        try:
            eye, target = self._camera_source_eye_target(params)
            dist, yaw, pitch = self._orbit_angles_from_eye_target(eye, target)
            self._cam_target_x = float(target[0])
            self._cam_target_y = float(target[1])
            self._cam_target_z = float(target[2])
            self._cam_dist = max(0.1, dist)
            self._orbit_yaw = yaw
            self._orbit_pitch = pitch
            self.update()
        except Exception:
            self.sync_scene3d_params(params)

    def _active_viewer_camera_params(self) -> Optional[dict]:
        """Return params for the Camera3D currently driving the 3D Viewer."""
        try:
            page = getattr(self, "_composite_page", None)
            graph = getattr(page, "_node_graph", None) if page is not None else None
            active_id = getattr(page, "_viewer3d_active_camera_id", None)
            if not active_id or graph is None:
                return None
            node = graph.get_node(str(active_id))
            if node is None or getattr(node, "node_type", None) != "camera3d":
                return None
            return dict(getattr(node, "parameters", {}) or {})
        except Exception:
            return None

    def _camera_projection_view_from_params(
            self, params: dict, tex_w: int, tex_h: int):
        """Build the exact active Camera3D projection/view used by overlays."""
        from src.features.composite.render3d import math_utils as m3d
        aspect = max(1, int(tex_w)) / max(1, int(tex_h))
        mode = str(params.get("camera_mode", "orbit"))
        fov = float(params.get("cam_fov", 50.0))
        near = max(0.001, float(params.get("cam_near", 0.1)))
        far = max(near + 1.0, float(params.get("cam_far", 1000.0)))
        if mode == "ortho":
            proj = m3d.ortho(
                max(0.1, float(params.get("cam_ortho_scale", 5.0))),
                aspect, near, far)
        elif (abs(float(params.get("cam_lens_shift_x", 0.0))) > 1e-4
              or abs(float(params.get("cam_lens_shift_y", 0.0))) > 1e-4):
            proj = m3d.perspective_with_shift(
                fov, aspect, near, far,
                float(params.get("cam_lens_shift_x", 0.0)),
                float(params.get("cam_lens_shift_y", 0.0)))
        else:
            proj = m3d.perspective(fov, aspect, near, far)

        eye, target = self._camera_source_eye_target(params)
        forward = target - eye
        if abs(float(params.get("cam_roll", 0.0))) > 1e-3:
            up = m3d.rotate_around_axis(
                m3d.vec3(0.0, 1.0, 0.0), forward,
                float(params.get("cam_roll", 0.0)))
        else:
            up = m3d.vec3(0.0, 1.0, 0.0)
        view = m3d.look_at(eye, target, up)
        dist, _yaw, _pitch = self._orbit_angles_from_eye_target(eye, target)
        return proj, view, dist, fov

    def suggested_camera3d_params_for_new_node(self) -> dict:
        """Return visible initial Camera3D params for a newly added camera node.

        A new camera should appear in the current 3D Viewer like a DCC object.
        If it reuses the viewer camera exactly, the icon lands at the viewer eye
        and cannot be projected. Place it inside the current view with a small
        right/up offset and aim it at the current target.
        """
        try:
            from src.features.composite.render3d import math_utils as m3d
            target = m3d.vec3(self._cam_target_x, self._cam_target_y, self._cam_target_z)
            eye = m3d.orbit_to_eye(
                max(0.1, self._cam_dist), self._orbit_yaw, self._orbit_pitch,
                target=target)
            forward = m3d.normalize(target - eye)
            world_up = m3d.vec3(0.0, 1.0, 0.0)
            right = m3d.normalize(np.cross(forward, world_up))
            if float(np.linalg.norm(right)) < 1e-6:
                right = m3d.vec3(1.0, 0.0, 0.0)
            up = m3d.normalize(np.cross(right, forward))
            dist = max(1.0, float(self._cam_dist))
            cam_eye = target - forward * (dist * 0.55) + right * (dist * 0.35) + up * (dist * 0.18)
            return {
                "camera_mode": "free",
                "cam_eye_x": float(cam_eye[0]),
                "cam_eye_y": float(cam_eye[1]),
                "cam_eye_z": float(cam_eye[2]),
                "cam_target_x": float(target[0]),
                "cam_target_y": float(target[1]),
                "cam_target_z": float(target[2]),
            }
        except Exception:
            return {
                "camera_mode": "free",
                "cam_eye_x": 4.0,
                "cam_eye_y": 3.0,
                "cam_eye_z": 5.0,
                "cam_target_x": 0.0,
                "cam_target_y": 0.0,
                "cam_target_z": 0.0,
            }

    def suggested_light3d_params_for_new_node(self) -> dict:
        """Return origin-centered initial Light3D params for a new light node."""
        return {
            "icon_pos_x": 0.0,
            "icon_pos_y": 0.0,
            "icon_pos_z": 0.0,
            "light_pos_x": 0.0,
            "light_pos_y": 0.0,
            "light_pos_z": 0.0,
        }

    def _suggested_visible_3d_helper_position(
            self, right_factor: float, up_factor: float, depth_factor: float):
        """Place a new helper inside the current 3D view instead of at the eye."""
        from src.features.composite.render3d import math_utils as m3d
        target = m3d.vec3(self._cam_target_x, self._cam_target_y, self._cam_target_z)
        eye = m3d.orbit_to_eye(
            max(0.1, self._cam_dist), self._orbit_yaw, self._orbit_pitch,
            target=target)
        forward = m3d.normalize(target - eye)
        world_up = m3d.vec3(0.0, 1.0, 0.0)
        right = m3d.normalize(np.cross(forward, world_up))
        if float(np.linalg.norm(right)) < 1e-6:
            right = m3d.vec3(1.0, 0.0, 0.0)
        up = m3d.normalize(np.cross(right, forward))
        dist = max(1.0, float(self._cam_dist))
        return (
            target
            - forward * (dist * float(depth_factor))
            + right * (dist * float(right_factor))
            + up * (dist * float(up_factor))
        )

    def _viewer3d_projection_params(self) -> tuple[float, int, int]:
        """Return FOV/format for the current 3D Viewer camera.

        Camera3D nodes are ordinary scene objects until explicitly activated.
        Selecting a Camera3D and editing its FOV must not alter the default
        viewer projection, gizmo size, or grid navigation. When a Camera3D is
        active, its projection drives the viewer and overlays.
        """
        fallback_w = max(1, int(self._tex_width or 1))
        fallback_h = max(1, int(self._tex_height or 1))
        params = {}
        try:
            page = getattr(self, "_composite_page", None)
            graph = getattr(page, "_node_graph", None) if page is not None else None
            active_id = getattr(page, "_viewer3d_active_camera_id", None)
            if active_id and graph is not None:
                node = graph.get_node(str(active_id))
                if node is not None and getattr(node, "node_type", None) == "camera3d":
                    params = dict(getattr(node, "parameters", {}) or {})
            elif graph is not None:
                viewer_state = getattr(graph, "_viewer_state", {}) or {}
                default_scene = viewer_state.get("default_3d_scene", {}) or {}
                params = dict(default_scene)
        except Exception:
            params = {}

        fov = float(params.get("cam_fov", 50.0))
        tex_w = max(1, int(params.get("width", fallback_w)))
        tex_h = max(1, int(params.get("height", fallback_h)))
        return fov, tex_w, tex_h

    def _update_gizmo_projection(self) -> None:
        """Refresh gizmo world→screen projection from current cam + object pos.

        Called every paintGL before drawing the overlay, so the gizmo tracks
        camera nav and object moves live. Also caches the computed proj/view/
        target/pitch as instance state so the ground-grid overlay can reuse the
        exact same camera projection (no duplicate math, guaranteed alignment).

        注意：投影计算本身与 gizmo 三轴显隐（_gizmo_visible）无关——地面网格
        叠加层也依赖这个缓存。只要处于 3D + scene3d 场景就计算并填充缓存；
        gizmo 三轴是否绘制由 _draw_gizmo_overlay 各自判断。
        """
        # 只要处于 3D 模式就计算投影（不依赖当前选择）。默认虚拟 3D Viewer
        # 会显示所有 Camera3D/Light3D 图标和 Geo3D/Model3D 内容；如果投影
        # 依赖选中节点，新增 Light/Camera 后在未选中它时图标会消失。
        if self._view_mode != "3d":
            return
        from src.features.composite.render3d import math_utils as m3d
        fov, tex_w, tex_h = self._viewer3d_projection_params()
        opx, opy, opz = self._gizmo_obj_pos
        # 当前 Viewer 摄影机的渲染目标尺寸（决定 gizmo 轴像素长度 + 投影 aspect）。
        # 未激活 Camera3D 时使用项目默认 3D Viewer 设置，不使用选中相机参数。
        aspect = tex_w / tex_h if tex_h > 0 else 1.0
        target = m3d.vec3(self._cam_target_x, self._cam_target_y, self._cam_target_z)
        # M2: 有效视图（quad 的活动象限 或 单视图正交 override）是正交视图时用 ortho
        # 投影（与 Scene3D 正交渲染一致），gizmo 才能正确 hit-test。透视走 perspective。
        _eff = self._effective_view_kind()
        from src.features.composite.ui.viewport3d_state import ViewportViewKind
        if _eff in (ViewportViewKind.TOP, ViewportViewKind.RIGHT,
                    ViewportViewKind.FRONT):
            proj, view, eye = self._ortho_view_camera(_eff)
            # 纹理尺寸与活动视图渲染分辨率一致（quad 正交=半分辨率，单视图正交=全分辨率）
            tex_w, tex_h = self._active_view_tex_size()
            self._transform_gizmo.update(
                m3d.vec3(opx, opy, opz), proj, view, tex_w, tex_h,
                cam_dist=self._cam_dist, cam_fov=fov)
            return
        active_camera_params = self._active_viewer_camera_params()
        if active_camera_params is not None:
            proj, view, active_dist, active_fov = self._camera_projection_view_from_params(
                active_camera_params, tex_w, tex_h)
            self._transform_gizmo.update(
                m3d.vec3(opx, opy, opz), proj, view, tex_w, tex_h,
                cam_dist=active_dist, cam_fov=active_fov)
            return
        proj = m3d.perspective(fov, aspect, 0.1, 1000.0)
        eye = m3d.orbit_to_eye(self._cam_dist, self._orbit_yaw,
                               self._orbit_pitch, target=target)
        view = m3d.look_at(eye, target, m3d.vec3(0.0, 1.0, 0.0))
        self._transform_gizmo.update(
            m3d.vec3(opx, opy, opz), proj, view, tex_w, tex_h,
            cam_dist=self._cam_dist, cam_fov=fov)

    def _sync_transform_gizmo_mode(self, mode: object) -> None:
        """Mirror the active 3D tool mode into the transform gizmo draw mode."""
        from src.features.composite.render3d.gizmo_overlay import GizmoMode
        mode_value = getattr(mode, "value", "")
        mode_map = {
            "select3d": GizmoMode.MOVE,
            "move3d": GizmoMode.MOVE,
            "rotate3d": GizmoMode.ROTATE,
            "scale3d": GizmoMode.SCALE,
        }
        self._transform_gizmo.mode = mode_map.get(mode_value, GizmoMode.MOVE)
        self._transform_gizmo.hovered_axis = None
        self.update()

    def _draw_3d_viewport_overlay(self) -> None:
        """Phase 4: 绘制四视图叠加层（分割线/活动边框/标签）+ P3 安全框。

        仅 3D 模式绘制；四视图分割线等仅 QUAD 布局时画；安全框在任何 3D 布局下，
        当连接的 Camera3D 启用 cam_show_safe_areas 时画。用 QPainter 画屏幕空间
        辅助元素，在 paintGL 末尾调用（所有 GL 绘制之后），不写深度、不进合成输出。
        """
        if self._view_mode != "3d":
            return
        from PyQt6.QtGui import QPainter
        from src.features.composite.render3d.viewport_overlays import (
            draw_quad_view_overlay, draw_render_region_mask, draw_safe_areas)
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            content_rect = self._viewer3d_content_rect_widget()
            render_rect = self._viewer3d_render_region_rect(content_rect)
            if not self._viewport3d_ctrl.state.is_quad():
                render_rect = draw_render_region_mask(
                    painter,
                    content_rect,
                    self._viewer3d_project_aspect(),
                    self._viewer3d_overlay_settings(),
                )
            # 四视图分割线/标签/活动边框（仅 QUAD 布局）
            if self._viewport3d_ctrl.state.is_quad():
                draw_quad_view_overlay(
                    painter, self._viewport3d_ctrl.state,
                    content_rect)
            # P3 安全框（Camera3D 启用 cam_show_safe_areas 时）
            if self._should_show_safe_areas():
                draw_safe_areas(painter, render_rect)
            # P0 / Milestone A：3D 性能 overlay（角落 FPS/ms/tris/draws）。
            # 默认关闭；通过 toggle_perf_overlay() 或环境变量开启（debug 用）。
            self._draw_perf_overlay(painter, content_rect)
            # P1-6：导航 LOD 状态徽章（导航态时右上角显示 "Navigation LOD"）。
            # 不受 perf overlay 开关影响——它是面向用户的状态反馈（计划 §5.4）。
            self._draw_nav_lod_badge(painter, content_rect)
        finally:
            painter.end()

    def _draw_nav_lod_badge(self, painter, content_rect) -> None:
        """P1-6：导航态时在视口右上角画 "Navigation LOD" 徽章。

        从最近一帧 render_diagnostics 读 quality_profile / lod_active。
        NAVIGATION/FAST_SOLID 时显示（含 LOD 等级），PREVIEW/FINAL 不显示。
        轻量半透明胶囊 + 暖色文字，不干扰主视图。
        """
        diag = self._perf_diagnostics
        if not diag or not isinstance(diag, dict):
            return
        profile = str(diag.get("quality_profile", "preview") or "preview")
        if profile not in ("navigation", "fast_solid"):
            return
        perf = diag.get("perf_stats") or {}
        lod_level = int(perf.get("lod_level", 1)) if isinstance(perf, dict) else 1
        from PyQt6.QtGui import QColor, QFont, QPen
        label = f"Navigation LOD{lod_level}" if lod_level > 0 else "Navigation"
        if profile == "fast_solid":
            label = f"Fast Solid LOD{lod_level}"
        font = QFont("Menlo", 9)
        font.setStyleHint(QFont.StyleHint.Monospace)
        painter.setFont(font)
        fm = painter.fontMetrics()
        text_w = fm.horizontalAdvance(label)
        line_h = fm.height()
        box_w = text_w + 14
        box_h = line_h + 6
        margin = 8
        x = content_rect.right() - box_w - margin
        y = content_rect.top() + margin
        # 半透明胶囊背景
        painter.setPen(Qt.PenStyle.NoPen)
        bg = QColor(180, 90, 20, 180) if profile == "fast_solid" else QColor(40, 90, 160, 170)
        painter.setBrush(bg)
        painter.drawRoundedRect(x, y, box_w, box_h, box_h // 2, box_h // 2)
        # 文字
        painter.setPen(QPen(QColor(255, 240, 200, 235)))
        painter.drawText(x + 7, y + line_h - 1, label)

    # ── P0 / Milestone A：3D 性能统计 overlay ──

    def set_render_diagnostics(self, diagnostics: dict) -> None:
        """接收最近一帧 Scene3D 的 render_diagnostics（含 perf_stats）。"""
        self._perf_diagnostics = diagnostics if isinstance(diagnostics, dict) else None
        # 按帧到达间隔估算 FPS（平滑，避免单帧抖动）
        import time as _time
        now = _time.monotonic()
        prev = self._perf_last_frame_monotonic
        self._perf_last_frame_monotonic = now
        if prev > 0.0:
            dt = now - prev
            if 0.001 < dt < 1.0:
                instant_fps = 1.0 / dt
                # EMA 平滑（α=0.2）：兼顾稳定性和响应
                self._perf_fps = self._perf_fps * 0.8 + instant_fps * 0.2

    def toggle_perf_overlay(self, enabled: Optional[bool] = None) -> bool:
        """开关 3D 视口性能 overlay。None=toggle，返回开启后的状态。"""
        if enabled is None:
            self._perf_show_overlay = not self._perf_show_overlay
        else:
            self._perf_show_overlay = bool(enabled)
        self.update()
        return self._perf_show_overlay

    # ── P7：GPU ID picking ──

    def set_id_pick_info(self, info: dict) -> None:
        """接收最近一帧 Scene3D 的 ID/component picking 信息。"""
        self._id_pick_info = info if isinstance(info, dict) else None

    def get_component_selection(self):
        """Return the pure-data 3D component selection state."""
        return self._component_selection

    def clear_component_selection(self) -> None:
        """Clear Point/Edge/Polygon selection and notify listeners."""
        self._component_selection.clear_components()
        self.component_selection_changed.emit(self._component_selection)
        self.update()

    def pick_component_at(self, widget_pos) -> Optional[dict]:
        """Pick the active 3D component from the latest CPU mesh snapshot.

        Returns ``{"node_id": str, "hit": ComponentHit}``, or ``None``. The
        renderer owns the snapshot arrays; this method only builds/query-caches
        CPU BVHs and never calls OpenGL.
        """
        info = self._id_pick_info
        if not isinstance(info, dict):
            return None
        meshes = info.get("component_meshes")
        proj = info.get("proj")
        view = info.get("view")
        tw = int(info.get("w", 0) or 0)
        th = int(info.get("h", 0) or 0)
        if not meshes or proj is None or view is None or tw <= 0 or th <= 0:
            return None
        pick_map = info.get("pick_map")
        dpr = self.devicePixelRatio()
        tex_px = self._widget_pos_to_main_tex_px(widget_pos, tw, th, dpr)
        if tex_px is None:
            return None
        fx, fy_bottom = tex_px
        if not (0 <= fx < tw and 0 <= fy_bottom < th):
            return None
        # screen_to_world_ray accepts a top-left-origin pixel position; the
        # main texture mapping above deliberately returns GL bottom-origin y.
        from src.features.composite.render3d.mesh_picking import (
            MeshPickCache, pick_component, screen_to_world_ray)
        if self._component_pick_cache is None:
            self._component_pick_cache = MeshPickCache(max_entries=64)
        ray_origin, ray_dir = screen_to_world_ray(
            (float(fx), float(th - 1 - fy_bottom)), tw, th, proj, view)
        mode = self._viewport3d_ctrl.state.component_mode
        best = None
        for descriptor in meshes:
            if not isinstance(descriptor, dict):
                continue
            vertices = descriptor.get("vertices")
            indices = descriptor.get("indices")
            if vertices is None or indices is None:
                continue
            try:
                mesh_key = str(descriptor.get("mesh_key")
                               or descriptor.get("node_id") or "mesh")
                node_id = str(descriptor.get("node_id", ""))
                pick_id = int(descriptor.get("pick_id", 0) or 0)
                if (isinstance(pick_map, dict) and pick_id > 0
                        and pick_map.get(pick_id) != node_id):
                    # Component CPU geometry and the GPU ID pass must refer to
                    # the same visible object. Skip stale/mismatched data.
                    continue
                bvh = self._component_pick_cache.get(
                    mesh_key, vertices, indices, descriptor.get("revision"))
                hit = pick_component(
                    ray_origin, ray_dir, vertices, indices, mode,
                    model_matrix=descriptor.get("model_matrix"), bvh=bvh)
            except (TypeError, ValueError, np.linalg.LinAlgError):
                continue
            if not hit.hit:
                continue
            candidate = (float(hit.distance), node_id, mesh_key, pick_id, hit)
            if best is None or candidate[:2] < best[:2]:
                best = candidate
        if best is None:
            return None
        return {
            "node_id": best[1],
            "mesh_id": best[2],
            "pick_id": best[3],
            "hit": best[4],
        }

    def pick_object_at(self, widget_pos) -> Optional[str]:
        """P7：读 ID texture 在 widget_pos 处的 1 像素 → 解码 pick_id → node_id。

        复用 scratch-FBO 读纹理模式（与 _read_linear_pixel_from_texture 同套路）。
        返回命中的 node_id，或 None（背景/未就绪/读回失败）。
        必须在 viewer GL context 内调（调用方 ensure makeCurrent）。
        """
        info = self._id_pick_info
        if not info or not isinstance(info, dict):
            return None
        tex_glo = info.get("tex_glo")
        pick_map = info.get("pick_map")
        tw = info.get("w", 0)
        th = info.get("h", 0)
        if (not tex_glo or not pick_map or tw <= 0 or th <= 0):
            return None
        # widget pos → 纹理像素（与 quad pick 同一套 letterbox 映射，简化用 content rect）
        dpr = self.devicePixelRatio()
        tex_px = self._widget_pos_to_main_tex_px(widget_pos, tw, th, dpr)
        if tex_px is None:
            return None
        fx, fy = tex_px
        if not (0 <= fx < tw and 0 <= fy < th):
            return None
        # 读 1 像素 RGBA8（共享纹理 glo → scratch FBO → glReadPixels）
        try:
            from OpenGL import GL as gl
            fb = gl.glGenFramebuffers(1)
            gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, fb)
            gl.glFramebufferTexture2D(gl.GL_FRAMEBUFFER, gl.GL_COLOR_ATTACHMENT0,
                                      gl.GL_TEXTURE_2D, int(tex_glo), 0)
            pixel = gl.glReadPixels(int(fx), int(fy), 1, 1,
                                    gl.GL_RGBA, gl.GL_UNSIGNED_BYTE,
                                    outputType=bytes)
            gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, 0)
            gl.glDeleteFramebuffers(1, [fb])
            if not pixel or len(pixel) < 4:
                return None
            r, g, b, a = pixel[0], pixel[1], pixel[2], pixel[3]
            if a == 0:
                return None  # 背景
            pick_id = int(r) | (int(g) << 8) | (int(b) << 16)
            return pick_map.get(pick_id)
        except Exception:
            return None

    def _widget_pos_to_main_tex_px(self, widget_pos, tw: int, th: int, dpr: float):
        """widget 坐标 → 主纹理像素坐标（原点左下）。

        单视图 letterbox 映射（与 _compute_vertices 同套，简化版）。
        quad 模式暂不支持 picking（返回 None）。
        """
        if self.is_quad_view():
            return None
        # widget → device px
        wd = widget_pos.x() * dpr
        hd = widget_pos.y() * dpr
        vp_w = int(self.width() * dpr)
        vp_h = int(self.height() * dpr)
        # content rect（letterbox）：与 _compute_vertices 一致的 fit 逻辑
        if tw <= 0 or th <= 0 or vp_w <= 0 or vp_h <= 0:
            return None
        scale = min(vp_w / tw, vp_h / th)
        disp_w = tw * scale
        disp_h = th * scale
        x0 = (vp_w - disp_w) / 2.0
        y0_top = (vp_h - disp_h) / 2.0
        # widget y 是从上往下，纹理 y 从下往上
        ndc_x = (wd - x0) / disp_w if disp_w > 0 else 0.0
        ndc_y = 1.0 - (hd - y0_top) / disp_h if disp_h > 0 else 0.0
        fx = int(ndc_x * tw)
        fy = int(ndc_y * th)
        return (fx, fy)

    def _draw_perf_overlay(self, painter, content_rect) -> None:
        """在视口左上角画性能统计小字（仅 _perf_show_overlay 开启时）。"""
        if not self._perf_show_overlay:
            return
        diag = self._perf_diagnostics
        if not diag or not isinstance(diag, dict):
            return
        perf = diag.get("perf_stats")
        if not perf or not isinstance(perf, dict):
            return
        from PyQt6.QtGui import QColor, QFont, QPen
        from src.features.composite.render3d.perf_stats import (
            format_overlay, format_quad_overlay)
        # 重建一个轻量 stats-like dict 给 format_overlay（字段名一致）
        class _S:
            pass
        s = _S()
        for k, v in perf.items():
            setattr(s, k, v)
        line1 = format_overlay(s, fps=self._perf_fps)
        line2 = format_quad_overlay(s)
        # P4-6UI / P6：第 3 行显示 cache 状态（scene content + model + material 命中率）。
        cache_parts = []
        # P6：场景内容缓存命中（仅相机变化时 True——核心导航加速指标）
        perf_sc = perf.get("scene_content_cache_hit") if isinstance(perf, dict) else None
        if perf_sc is True:
            cache_parts.append("scene:hit")
        elif perf_sc is False:
            cache_parts.append("scene:miss")
        mcs = diag.get("model_cache_status")
        if mcs:
            cache_parts.append(f"model:{mcs}")
        mchr = diag.get("material_cache_hit_rate")
        if mchr is not None:
            cache_parts.append(f"mat:{int(float(mchr) * 100)}%")
        line3 = " | ".join(cache_parts) if cache_parts else ""
        # P8-0：第 4 行显示 uniform/shader 计数（量化 §7.2 bind 开销）。
        # uniform_sets=每帧 uniform 赋值次数；shader_switches=program 切换次数。
        # 这两个指标用于验证 P8-2（view 级 uniform 缓存）的收益。
        line4_parts = []
        _us = perf.get("uniform_sets")
        _ss = perf.get("shader_switches")
        _vbo = perf.get("vbo_cache_hit")
        if _us is not None:
            line4_parts.append(f"uni:{_us}")
        if _ss is not None:
            line4_parts.append(f"sh:{_ss}")
        if _vbo is True:
            line4_parts.append("vbo:hit")
        elif _vbo is False:
            line4_parts.append("vbo:miss")
        line4 = " | ".join(line4_parts) if line4_parts else ""
        # 字体：等宽小字
        font = QFont("Menlo", 9)
        font.setStyleHint(QFont.StyleHint.Monospace)
        painter.setFont(font)
        # 半透明黑底 + 亮文字（仿 DCC overlay）
        painter.setPen(Qt.PenStyle.NoPen)
        bg = QColor(0, 0, 0, 160)
        painter.setBrush(bg)
        margin = 8
        # 先量文字宽度
        fm = painter.fontMetrics()
        w1 = fm.horizontalAdvance(line1)
        w2 = fm.horizontalAdvance(line2) if line2 else 0
        w3 = fm.horizontalAdvance(line3) if line3 else 0
        w4 = fm.horizontalAdvance(line4) if line4 else 0
        text_w = max(w1, w2, w3, w4)
        line_h = fm.height()
        n_lines = (1 if line1 else 0) + (1 if line2 else 0) + (1 if line3 else 0) + (1 if line4 else 0)
        box_w = text_w + 12
        box_h = line_h * n_lines + 8
        x = content_rect.left() + margin
        y = content_rect.top() + margin
        painter.drawRoundedRect(x, y, box_w, box_h, 4, 4)
        # 文字
        painter.setPen(QPen(QColor(255, 220, 120, 230)))
        ty = y + line_h
        painter.drawText(x + 6, ty, line1)
        if line2:
            ty += line_h
            painter.setPen(QPen(QColor(160, 220, 255, 220)))
            painter.drawText(x + 6, ty, line2)
        if line3:
            ty += line_h
            painter.setPen(QPen(QColor(160, 255, 180, 220)))
            painter.drawText(x + 6, ty, line3)
        if line4:
            ty += line_h
            painter.setPen(QPen(QColor(200, 180, 255, 220)))
            painter.drawText(x + 6, ty, line4)

    def _should_show_safe_areas(self) -> bool:
        """检查当前渲染的 Camera3D 节点是否启用安全框显示。

        查找当前查看的 3D 节点关联的 Camera3D，读其 cam_show_safe_areas 参数：
          - _viewing_node_id 是 scene3d → 找其 in 端口上游的 camera3d
            （优先 active_camera_id 指定的；否则任一连接的 camera3d）
          - _viewing_node_id 是 camera3d → 直接查该节点
          - _viewing_node_id 为 None（未选中 3D 节点）→ 兜底遍历全图 camera3d
        任一匹配的 camera3d 启用 cam_show_safe_areas 即返回 True。
        """
        try:
            graph = self._get_composite_graph()
            if graph is None:
                return False
            viewing_node_id = getattr(self, '_viewing_node_id', None)
            # 收集要检查的 Camera3D 节点
            cam_nodes = []
            if viewing_node_id is not None:
                viewing_node = graph.get_node(viewing_node_id)
                if viewing_node is not None:
                    if viewing_node.node_type == "camera3d":
                        cam_nodes = [viewing_node]
                    elif viewing_node.node_type == "scene3d":
                        # 找 scene3d 的 in 端口上游 camera3d（优先 active_camera_id）
                        upstream_cams = []
                        for conn in graph.connections:
                            if (conn.dst_node_id == viewing_node_id
                                    and conn.dst_port_name == "in"):
                                up = graph.get_node(conn.src_node_id)
                                if up is not None and up.node_type == "camera3d":
                                    upstream_cams.append(up)
                        # 活动相机优先；其余也保留（任一启用即显示）
                        # AUD-9712: 原 if/else 两分支完全相同，收敛为直读。
                        cam_nodes = upstream_cams
            else:
                # 兜底：未选中 3D 节点时遍历全图 camera3d
                cam_nodes = [n for n in graph.nodes.values()
                             if n.node_type == "camera3d"]
            for node in cam_nodes:
                if bool(node.parameters.get("cam_show_safe_areas", False)):
                    return True
            return False
        except Exception:
            return False

    def _get_composite_graph(self):
        """获取当前 composite 节点图（从 page 引用）。"""
        # CompositeViewer 通过 page 持有 graph 引用；此处用弱查找避免循环依赖
        page = getattr(self, '_composite_page', None)
        if page is not None:
            return getattr(page, '_node_graph', None)
        return None

    # ── M2: 四视图真实渲染（4 象限分别 blit 透视 + 正交纹理）─────────────────

    def _overlay_bar_heights(self) -> tuple:
        """返回被浮层占用的顶部/底部高度（设备像素）。

        顶部工具栏始终在；底部播放条仅视频时可见。quad 内容区需排除它们，
        否则象限会被浮层遮挡。返回 (top_h_px, bottom_h_px)，均为设备像素。
        """
        dpr = self.devicePixelRatio() if self._gl_initialized else 1.0
        top_h = 0
        if hasattr(self, '_toolbar') and self._toolbar is not None:
            top_h = int(self._toolbar.height() * dpr)
        bottom_h = 0
        if (hasattr(self, 'video_control_bar')
                and self.video_control_bar is not None
                and self.video_control_bar.isVisible()):
            bottom_h = int(self.video_control_bar.height() * dpr)
            # inspector bar 在播放条下方，也计入
            if hasattr(self, '_inspector_bar') and self._inspector_bar.isVisible():
                bottom_h += int(self._inspector_bar.height() * dpr)
        return top_h, bottom_h

    def _fit_letterbox_ndc(self, dst_w, dst_h, tex_w, tex_h) -> tuple:
        """在 dst 矩形内对 tex 做 fit-to-window letterbox，返回 NDC (x0,y0,x1,y1)。

        与 _compute_vertices 不同：本方法忽略 zoom/pan（quad 子视图是只读参考，
        不接受 2D 的缩放/平移交互），纯按比例居中。
        NDC 坐标系：x∈[-1,1] 横向，y∈[-1,1] 纵向（OpenGL 标准）。
        """
        if dst_w <= 0 or dst_h <= 0 or tex_w <= 0 or tex_h <= 0:
            return -1.0, -1.0, 1.0, 1.0
        tex_aspect = tex_w / tex_h
        dst_aspect = dst_w / dst_h
        if tex_aspect > dst_aspect:
            sy = dst_aspect / tex_aspect
            return -1.0, -sy, 1.0, sy
        else:
            sx = tex_aspect / dst_aspect
            return -sx, -1.0, sx, 1.0

    def _blit_quad_texture(self, tex_id, dst_x, dst_y, dst_w, dst_h, tex_w, tex_h) -> None:
        """在设备像素矩形 (dst_x,dst_y,dst_w,dst_h) 内 blit 一张纹理（带 letterbox）。

        dst 坐标系：设备像素，y 原点在**左下角**（GL scissor/viewport 约定）。
        已假定 scissor/viewport 由调用方设好；本方法只画一个带 letterbox 的 quad。
        """
        x0, y0, x1, y1 = self._fit_letterbox_ndc(dst_w, dst_h, tex_w, tex_h)
        _gl.glBindVertexArray(self._vao)
        _gl.glBindBuffer(GL_ARRAY_BUFFER, self._quad_vbo)
        _gl.glEnableVertexAttribArray(self._pos_loc)
        _gl.glVertexAttribPointer(self._pos_loc, 2, GL_FLOAT, GL_FALSE, 16, ctypes.c_void_p(0))
        _gl.glEnableVertexAttribArray(self._uv_loc)
        _gl.glVertexAttribPointer(self._uv_loc, 2, GL_FLOAT, GL_FALSE, 16, ctypes.c_void_p(8))
        _gl.glBindBuffer(GL_ARRAY_BUFFER, 0)
        vertices = np.array([
            x0, y1, 0.0, 1.0,
            x0, y0, 0.0, 0.0,
            x1, y1, 1.0, 1.0,
            x1, y0, 1.0, 0.0,
        ], dtype=np.float32)
        _gl.glBindBuffer(GL_ARRAY_BUFFER, self._quad_vbo)
        _gl.glBufferData(GL_ARRAY_BUFFER, 64, ctypes.c_void_p(0), GL_DYNAMIC_DRAW)
        _gl.glBufferSubData(GL_ARRAY_BUFFER, 0, vertices.nbytes, vertices)
        _gl.glBindBuffer(GL_ARRAY_BUFFER, 0)
        _gl.glUseProgram(self._prog)
        _gl.glEnable(GL_BLEND)
        _gl.glBlendFunc(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA)
        # The quad path uses the same fragment shader but bypasses the normal
        # paintGL display-uniform setup. Keep its 3D sampler away from TU0 too.
        odt_active_loc = _gl.glGetUniformLocation(
            self._prog, b"u_odt_lut_active")
        if odt_active_loc >= 0:
            _gl.glUniform1i(odt_active_loc, 0)
        lut_loc = _gl.glGetUniformLocation(self._prog, b"u_odt_lut")
        if lut_loc >= 0:
            _gl.glUniform1i(lut_loc, 2)
        _gl.glActiveTexture(GL_TEXTURE0)
        _gl.glBindTexture(GL_TEXTURE_2D, tex_id)
        _gl.glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE)
        _gl.glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE)
        tex_loc = _gl.glGetUniformLocation(self._prog, b"u_texture")
        _gl.glUniform1i(tex_loc, 0)
        # quad 子视图：关闭通道/曝光/split 等显示调节（用中性默认值）
        for uname, val in ((b"u_channel_mode", 0), (b"u_alpha_preview", 0),
                           (b"u_split_active", 0), (b"u_split_mode", 0)):
            loc = _gl.glGetUniformLocation(self._prog, uname)
            if loc >= 0:
                _gl.glUniform1i(loc, val)
        exp_loc = _gl.glGetUniformLocation(self._prog, b"u_exposure")
        if exp_loc >= 0:
            _gl.glUniform1f(exp_loc, ctypes.c_float(0.0))
        gamma_loc = _gl.glGetUniformLocation(self._prog, b"u_viewer_gamma")
        if gamma_loc >= 0:
            _gl.glUniform1f(gamma_loc, ctypes.c_float(1.0))
        for uname in (b"u_viewer_gain", b"u_viewer_lift", b"u_viewer_offset"):
            loc_d = _gl.glGetUniformLocation(self._prog, uname)
            if loc_d >= 0:
                _gl.glUniform1f(
                    loc_d,
                    ctypes.c_float(1.0 if uname == b"u_viewer_gain" else 0.0))
        dt_loc = _gl.glGetUniformLocation(self._prog, b"u_display_transfer")
        if dt_loc >= 0:
            _gl.glUniform1i(dt_loc, int(self._display_transfer))
        _gl.glBindVertexArray(self._vao)
        _gl.glDrawArrays(GL_TRIANGLE_STRIP, 0, 4)
        _gl.glBindVertexArray(0)
        _gl.glBindTexture(GL_TEXTURE_2D, 0)
        _gl.glDisable(GL_BLEND)
        _gl.glUseProgram(0)

    def _draw_quad_view(self, vp_w, vp_h, tex_w, tex_h) -> None:
        """绘制四视图：4 个象限分别 scissor + blit 对应纹理（M2）。

        content_rect 排除顶部工具栏/底部播放条。透视象限用主纹理，
        top/right/front 用 viewer-owned 正交纹理。各象限独立 letterbox（不拉伸）。
        """
        from src.features.composite.ui.viewport3d_state import (
            ViewportViewKind, QUAD_VIEW_ORDER)

        top_h, bottom_h = self._overlay_bar_heights()
        # 内容区（设备像素，y 原点左下）
        content_x = 0
        content_y = bottom_h
        content_w = vp_w
        content_h = vp_h - top_h - bottom_h
        if content_h <= 0:
            return
        # viewport_overlays.quad_view_rects 用 QRectF（y 向下，widget 坐标）。
        # 这里在 GL 坐标（y 向上）里算象限，避免转换混乱。
        cx = content_x + content_w / 2.0
        cy = content_y + content_h / 2.0
        half_w = content_w / 2.0
        half_h = content_h / 2.0
        # 各象限的 GL 矩形：(x, y, w, h)，y 原点左下
        # PERSPECTIVE 左上(widget) = GL 左上 = (left, top) → x=content_x, y=cy, w=half_w, h=half_h
        # TOP 右上(widget) = GL 右上 → x=cx, y=cy
        # RIGHT 左下(widget) = GL 左下 → x=content_x, y=content_y
        # FRONT 右下(widget) = GL 右下 → x=cx, y=content_y
        rects_gl = {
            ViewportViewKind.PERSPECTIVE: (content_x, cy, half_w, half_h),
            ViewportViewKind.TOP:         (cx, cy, half_w, half_h),
            ViewportViewKind.RIGHT:       (content_x, content_y, half_w, half_h),
            ViewportViewKind.FRONT:       (cx, content_y, half_w, half_h),
        }
        # 各象限用的纹理（透视=主纹理；其余=owned 正交纹理）
        ow, oh = (self._owned_quad_tex_size
                  if self._owned_quad_tex_size else (tex_w // 2, tex_h // 2))
        view_textures = {
            ViewportViewKind.PERSPECTIVE: (self._current_tex_id, tex_w, tex_h),
            ViewportViewKind.TOP:         (self._owned_quad_tex_ids.get("top", 0), ow, oh),
            ViewportViewKind.RIGHT:       (self._owned_quad_tex_ids.get("right", 0), ow, oh),
            ViewportViewKind.FRONT:       (self._owned_quad_tex_ids.get("front", 0), ow, oh),
        }

        _gl.glEnable(GL_SCISSOR_TEST)
        try:
            # AUD-9714: the clear color is identical for every quadrant but
            # used to be re-merged from the global 3D scene settings once per
            # quadrant per frame (4× settings merge + float parses each
            # frame). Resolve it once per paint instead.
            _qr, _qg, _qb = self._viewport_bg_color_rgb()
            for view_kind in QUAD_VIEW_ORDER:
                rx, ry, rw, rh = rects_gl[view_kind]
                if rw <= 0 or rh <= 0:
                    continue
                # GL scissor: (x, y, w, h)，y 原点左下
                _gl.glScissor(int(rx), int(ry), int(rw), int(rh))
                _gl.glViewport(int(rx), int(ry), int(rw), int(rh))
                # 各象限清背景（与全屏背景色一致）
                _gl.glClearColor(_qr, _qg, _qb, 1.0)
                _gl.glClear(GL_COLOR_BUFFER_BIT)
                tex_id, tw, th = view_textures[view_kind]
                if tex_id and tex_id > 0 and _gl.glIsTexture(tex_id):
                    self._blit_quad_texture(tex_id, rx, ry, rw, rh, tw, th)
        finally:
            _gl.glDisable(GL_SCISSOR_TEST)
            _gl.glViewport(0, 0, vp_w, vp_h)

    def _quad_view_at(self, widget_pos) -> "Optional[object]":
        """返回 widget 坐标落在哪个 quad 象限（M2 点击切换用）。

        widget_pos: QPoint/QPointF（widget 逻辑像素，y 向下）。
        返回 ViewportViewKind 或 None（不在内容区/非 quad 模式）。
        布局同 _draw_quad_view：
            ┌─────────────┬─────────────┐
            │ PERSPECTIVE │     TOP     │
            ├─────────────┼─────────────┤
            │    RIGHT    │    FRONT    │
            └─────────────┴─────────────┘
        """
        if not self.is_quad_view():
            return None
        from src.features.composite.ui.viewport3d_state import ViewportViewKind
        dpr = self.devicePixelRatio() if self._gl_initialized else 1.0
        dpr = dpr if dpr > 0 else 1.0
        top_h, bottom_h = self._overlay_bar_heights()
        # 转换回 widget 逻辑像素
        top_w = top_h / dpr
        bottom_w = bottom_h / dpr
        content_top = top_w
        content_bottom = self.height() - bottom_w
        content_left = 0
        content_right = self.width()
        if content_bottom <= content_top:
            return None
        x = widget_pos.x()
        y = widget_pos.y()
        # 不在内容区 → None
        if (x < content_left or x >= content_right
                or y < content_top or y >= content_bottom):
            return None
        mid_x = (content_left + content_right) / 2.0
        mid_y = (content_top + content_bottom) / 2.0
        left = x < mid_x
        top_half = y < mid_y
        if top_half:
            return ViewportViewKind.PERSPECTIVE if left else ViewportViewKind.TOP
        else:
            return ViewportViewKind.RIGHT if left else ViewportViewKind.FRONT

    def _handle_quad_view_click(self, event) -> bool:
        """quad 模式下点击象限切换活动视图（M2）。

        点击**非当前活动视图** → 切换活动视图（高亮）并消费事件。
        点击**当前活动视图** → 放行（返回 False），让 gizmo pick 处理（支持编辑）。
        Alt 修饰时不处理（留给相机导航）。
        """
        if not self.is_quad_view():
            return False
        if event.modifiers() & Qt.KeyboardModifier.AltModifier:
            return False
        if event.button() != Qt.MouseButton.LeftButton:
            return False
        hit = self._quad_view_at(event.pos())
        if hit is None:
            return False
        state = self._viewport3d_ctrl.state
        if hit == state.active_view:
            # 点的是当前活动视图 → 放行给 gizmo 编辑
            return False
        # 切换活动视图
        state.active_view = hit
        self.update()
        return True

    def _delete_owned_quad_texture(self, key: str, assume_current: bool = False) -> None:
        """释放单个 owned 正交纹理。"""
        owned = self._owned_quad_tex_ids.get(key, 0)
        if not owned:
            return
        made_current = False
        try:
            if not assume_current:
                self.makeCurrent()
                made_current = True
            _gl.glDeleteTextures(1, [owned])
        except Exception:
            pass
        finally:
            if made_current:
                self.doneCurrent()
        self._owned_quad_tex_ids[key] = 0

    def _delete_owned_quad_textures(self, assume_current: bool = False) -> None:
        """释放所有 owned 正交纹理。"""
        for key in ("top", "right", "front"):
            self._delete_owned_quad_texture(key, assume_current=assume_current)
        self._owned_quad_tex_size = None

    def clear_quad_textures(self, assume_current: bool = False) -> None:
        """释放所有正交 owned 纹理（quad 关闭或无正交纹理时）。"""
        if not any(self._owned_quad_tex_ids.values()):
            self._owned_quad_tex_size = None
            return
        self._delete_owned_quad_textures(assume_current=assume_current)

    def set_quad_textures(self, ortho_views: dict) -> None:
        """把渲染器共享的正交视图纹理 blit 到 viewer-owned 纹理（M2）。

        ortho_views: {view_kind: (shared_tex_id, w, h)}，view_kind ∈ top/right/front。
        共享纹理来自渲染器线程的 Scene3D 节点，可能被回收 → 必须每帧拷贝到
        viewer-owned 纹理后再在 paintGL 里采样（与主纹理同模式）。
        复用 _copy_read_fbo/_copy_draw_fbo（__init__ 已建好，主纹理 blit 同用）。
        """
        if not isinstance(ortho_views, dict) or not ortho_views:
            self.clear_quad_textures()
            return
        if not self._gl_initialized:
            return
        made_current = False
        try:
            self.makeCurrent()
            made_current = True
            # 取所有视图统一的尺寸（Scene3D 用同一尺寸渲染三个正交视图）
            size = None
            for _k, val in ortho_views.items():
                if isinstance(val, (tuple, list)) and len(val) >= 3:
                    size = (int(val[1]), int(val[2]))
                    break
            if size is None or size[0] <= 0 or size[1] <= 0:
                self.clear_quad_textures(assume_current=True)
                return
            size_changed = self._owned_quad_tex_size != size
            if size_changed:
                self._delete_owned_quad_textures(assume_current=True)
                self._owned_quad_tex_size = size
            w, h = size
            for key in ("top", "right", "front"):
                val = ortho_views.get(key)
                if not (isinstance(val, (tuple, list)) and len(val) >= 3 and int(val[0]) > 0):
                    # 该视图缺失 → 释放旧的 owned tex（标记为 0）
                    if self._owned_quad_tex_ids.get(key, 0):
                        self._delete_owned_quad_texture(key, assume_current=True)
                    continue
                src_tex = int(val[0])
                if not _gl.glIsTexture(src_tex):
                    continue
                owned = self._owned_quad_tex_ids.get(key, 0)
                if not owned:
                    owned = int(_gl.glGenTextures(1))
                    self._owned_quad_tex_ids[key] = owned
                _gl.glBindTexture(GL_TEXTURE_2D, owned)
                _gl.glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR)
                _gl.glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR)
                _gl.glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE)
                _gl.glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE)
                _gl.glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA16F, w, h, 0,
                                 GL_RGBA, GL_HALF_FLOAT, None)
                if self._copy_read_fbo is None:
                    self._copy_read_fbo = int(_gl.glGenFramebuffers(1))
                if self._copy_draw_fbo is None:
                    self._copy_draw_fbo = int(_gl.glGenFramebuffers(1))
                _gl.glBindFramebuffer(GL_READ_FRAMEBUFFER, self._copy_read_fbo)
                _gl.glFramebufferTexture2D(GL_READ_FRAMEBUFFER, GL_COLOR_ATTACHMENT0,
                                           GL_TEXTURE_2D, src_tex, 0)
                _gl.glBindFramebuffer(GL_DRAW_FRAMEBUFFER, self._copy_draw_fbo)
                _gl.glFramebufferTexture2D(GL_DRAW_FRAMEBUFFER, GL_COLOR_ATTACHMENT0,
                                           GL_TEXTURE_2D, owned, 0)
                _gl.glBlitFramebuffer(0, 0, w, h, 0, 0, w, h,
                                      GL_COLOR_BUFFER_BIT, GL_NEAREST)
            _gl.glBindFramebuffer(GL_FRAMEBUFFER, self.defaultFramebufferObject())
            _gl.glBindTexture(GL_TEXTURE_2D, 0)
            self.update()
        except Exception as exc:
            logger.warning("CompositeViewer quad texture blit failed: %s", exc)
        finally:
            if made_current:
                self.doneCurrent()

    def is_quad_view(self) -> bool:
        """Whether the viewer is currently in 3D quad-view mode (M2).

        Quad view only applies in 3D mode (2D mode ignores the layout toggle).
        """
        return (self._view_mode == "3d"
                and self._viewport3d_ctrl.state.is_quad())

    def _ortho_view_camera(self, view_kind) -> "Optional[tuple]":
        """返回 quad 正交视图的 (proj, view, eye)（与 Scene3D 正交渲染一致）。

        复用当前 Viewer 的 cam_target / cam_dist / FOV；ortho scale 匹配透视在
        cam_dist 处的可见半高，使 gizmo 投影与正交渲染 + 透视视图大小一致。
        """
        from src.features.composite.render3d import math_utils as m3d
        from src.features.composite.ui.viewport3d_state import ViewportViewKind
        target = m3d.vec3(self._cam_target_x, self._cam_target_y, self._cam_target_z)
        cam_dist = max(0.1, float(self._cam_dist))
        # aspect 用项目比例（与正交纹理比例一致）
        cam_fov, tw, th = self._viewer3d_projection_params()
        aspect = tw / th if th > 0 else 1.0
        ortho_scale = m3d.ortho_scale_for_perspective_match(cam_dist, cam_fov)
        proj = m3d.ortho(ortho_scale, aspect, 0.1, 1000.0)
        # 相机配置（与 Scene3D._render_ortho_views 一致）
        _DIST = 500.0
        if view_kind == ViewportViewKind.TOP:
            eye = m3d.vec3(target[0], target[1] + _DIST, target[2])
            up = m3d.vec3(0.0, 0.0, -1.0)
        elif view_kind == ViewportViewKind.RIGHT:
            eye = m3d.vec3(target[0] + _DIST, target[1], target[2])
            up = m3d.vec3(0.0, 1.0, 0.0)
        elif view_kind == ViewportViewKind.FRONT:
            eye = m3d.vec3(target[0], target[1], target[2] + _DIST)
            up = m3d.vec3(0.0, 1.0, 0.0)
        else:
            return None
        view = m3d.look_at(eye, target, up)
        return proj, view, eye

    def _draw_guides_overlay(self) -> None:
        """Draw the aspect-ratio / safe-area guide overlay (QPainter).

        Ported from the Color page. Bound to the displayed image rectangle
        (computed from the texture corners mapped to view pixels), so the
        guides follow zoom/pan. Only active guides set via _on_guide_changed
        are drawn.
        """
        guide = getattr(self, "_active_guide", None)
        if not guide:
            return
        if not (self._tex_width and self._tex_height):
            return
        if self._view_mode == "3d":
            img_rect = self._viewer3d_render_region_rect()
        else:
            # Map the image's four corners into view pixels to get the on-screen
            # image rectangle (top-left origin, Qt widget coords).
            tl = self.get_view_pos_at_image_pos(QPointF(0.0, 0.0))
            br = self.get_view_pos_at_image_pos(
                QPointF(float(self._tex_width), float(self._tex_height)))
            if tl is None or br is None:
                return
            from PyQt6.QtCore import QRectF
            img_rect = QRectF(tl.x(), tl.y(), br.x() - tl.x(), br.y() - tl.y())
        from PyQt6.QtGui import QPainter
        if img_rect.width() <= 1 or img_rect.height() <= 1:
            return
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setClipRect(img_rect)
            g_type = guide.get("type")
            params = guide.get("params", {})
            if g_type == "aspect_ratio":
                ratio = float(params.get("ratio", 0.0))
                if ratio > 0.0:
                    self._paint_aspect_ratio(painter, img_rect, ratio)
            elif g_type == "safe_area":
                inset = float(params.get("inset", 0.0))
                secondary = float(params.get("secondary", 0.0))
                self._paint_safe_area(painter, img_rect, guide.get("id"), inset, secondary)
        finally:
            painter.end()

    def _paint_aspect_ratio(self, painter, img_rect: QRectF, ratio: float) -> None:
        """Draw a centered aspect-ratio frame with letterbox bars."""
        from PyQt6.QtGui import QColor, QPen
        # Fit the target ratio inside the image rect (contain).
        if img_rect.width() / img_rect.height() > ratio:
            # Wider than target: letterbox left/right.
            frame_h = img_rect.height()
            frame_w = frame_h * ratio
        else:
            frame_w = img_rect.width()
            frame_h = frame_w / ratio
        cx = img_rect.center().x()
        cy = img_rect.center().y()
        frame = QRectF(cx - frame_w / 2.0, cy - frame_h / 2.0, frame_w, frame_h)
        # Letterbox bars (darken the area outside the frame). Avoid
        # CompositionMode_Clear in QOpenGLWidget paint passes.
        shade = QColor(0, 0, 0, 80)
        painter.fillRect(QRectF(
            img_rect.left(), img_rect.top(),
            img_rect.width(), max(0.0, frame.top() - img_rect.top())), shade)
        painter.fillRect(QRectF(
            img_rect.left(), frame.bottom(),
            img_rect.width(), max(0.0, img_rect.bottom() - frame.bottom())), shade)
        painter.fillRect(QRectF(
            img_rect.left(), frame.top(),
            max(0.0, frame.left() - img_rect.left()), frame.height()), shade)
        painter.fillRect(QRectF(
            frame.right(), frame.top(),
            max(0.0, img_rect.right() - frame.right()), frame.height()), shade)
        # Frame outline.
        pen = QPen(QColor(255, 255, 255, 200))
        pen.setWidthF(1.0)
        painter.setPen(pen)
        painter.setBrush(QColor(0, 0, 0, 0))
        painter.drawRect(frame)

    def _paint_safe_area(self, painter, img_rect: QRectF, guide_id,
                         inset: float, secondary: float) -> None:
        """Draw a safe-area outline (action/title/center) inside the image rect."""
        from PyQt6.QtGui import QColor, QPen
        if guide_id == "center":
            # Center crosshair.
            pen = QPen(QColor(255, 255, 255, 160))
            pen.setWidthF(1.0)
            painter.setPen(pen)
            cx = img_rect.center().x()
            cy = img_rect.center().y()
            painter.drawLine(int(cx), int(img_rect.top()), int(cx), int(img_rect.bottom()))
            painter.drawLine(int(img_rect.left()), int(cy), int(img_rect.right()), int(cy))
            return
        if inset <= 0.0:
            # "Extents" — just outline the image rect.
            pen = QPen(QColor(255, 255, 255, 200))
            pen.setWidthF(1.0)
            painter.setPen(pen)
            painter.setBrush(QColor(0, 0, 0, 0))
            painter.drawRect(img_rect)
            return
        dx = img_rect.width() * inset
        dy = img_rect.height() * inset
        safe = img_rect.adjusted(dx, dy, -dx, -dy)
        pen = QPen(QColor(255, 255, 255, 200))
        pen.setWidthF(1.0)
        painter.setPen(pen)
        painter.setBrush(QColor(0, 0, 0, 0))
        painter.drawRect(safe)
        # Optional secondary (e.g. title within action).
        if secondary > 0.0:
            dx2 = img_rect.width() * secondary
            dy2 = img_rect.height() * secondary
            inner = img_rect.adjusted(dx2, dy2, -dx2, -dy2)
            pen2 = QPen(QColor(255, 255, 255, 120))
            pen2.setWidthF(1.0)
            pen2.setStyle(Qt.PenStyle.DashLine)
            painter.setPen(pen2)
            painter.drawRect(inner)

    def _draw_roi_overlay(self) -> None:
        """Draw the user ROI frame and dim everything outside it (B1).

        The evaluated sub-image is recomposited into a full frame by the
        renderer, so the picture stays in place — the dim mask is purely a
        viewing aid showing which region is excluded from preview eval.
        """
        rect = self._user_roi_drag_rect or self._user_roi_rect
        if rect is None:
            return
        if not (self._tex_width and self._tex_height):
            return
        x, y, w, h = rect
        # Normalize negative drag dims (live drag can go either direction)
        # BEFORE the degenerate check — returning early on w<0 would make
        # the overlay vanish whenever the drag crosses its start corner.
        if w < 0:
            x, w = x + w, -w
        if h < 0:
            y, h = y + h, -h
        if w <= 0 or h <= 0:
            return
        tl = self.get_view_pos_at_image_pos(QPointF(0.0, 0.0))
        br = self.get_view_pos_at_image_pos(QPointF(
            float(self._tex_width), float(self._tex_height)))
        if tl is None or br is None:
            return
        from PyQt6.QtCore import QRectF
        from PyQt6.QtGui import QPainter, QColor, QPen, QRegion
        img_rect = QRectF(tl.x(), tl.y(), br.x() - tl.x(), br.y() - tl.y())
        if img_rect.width() <= 1:
            return
        sx = img_rect.width() / float(self._tex_width)
        sy = img_rect.height() / float(self._tex_height)
        roi_screen = QRectF(img_rect.left() + x * sx, img_rect.top() + y * sy,
                            w * sx, h * sy)
        painter = QPainter(self)
        try:
            # Dim the kept-visible area outside the ROI.
            dim = QColor(0, 0, 0, 130)
            outside = QRegion(img_rect.toRect())
            outside -= QRegion(roi_screen.toRect())
            painter.setClipRegion(outside)
            painter.fillRect(img_rect, dim)
            painter.setClipping(False)
            # Frame + label.
            pen = QPen(QColor(80, 180, 255, 230))
            pen.setWidthF(1.5)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRect(roi_screen)
        finally:
            painter.end()

    # B2-3: magnifier loupe — window diameter in logical pixels.
    _MAGNIFIER_WINDOW_PX = 160.0

    @staticmethod
    def _magnifier_source_span_px(window_px: float, magnification: float,
                                  dpr: float) -> int:
        """Device pixels of framebuffer sampled for the loupe window.

        The loupe magnifies relative to the on-screen view: at 1x view zoom
        and 8x loupe, a 160px window samples 160/8 = 20 device pixels.
        """
        if magnification <= 0.0 or window_px <= 0.0 or dpr <= 0.0:
            return 1
        return max(1, int(round(window_px / magnification * dpr)))

    def _grab_framebuffer_region_image(self, cx_logical: float,
                                       cy_logical: float,
                                       vp_w: int, vp_h: int):
        """Read the composited (post-ODT) framebuffer near the cursor.

        Returns a top-down RGB QImage, or None when the region is empty or
        the readback fails. Sampling the framebuffer (not the raw texture)
        means the loupe shows exactly what the user sees — display transform,
        channel isolation and A/B wipe included.
        """
        dpr = self.devicePixelRatio() or 1.0
        span = self._magnifier_source_span_px(
            self._MAGNIFIER_WINDOW_PX, self._magnifier_zoom, dpr)
        half = max(1, span // 2)
        cx = int(round(cx_logical * dpr))
        cy = int(round(cy_logical * dpr))
        x0 = max(0, cx - half)
        y0_top = max(0, cy - half)
        x1 = min(vp_w, cx + half)
        y1 = min(vp_h, cy + half)
        w, h = x1 - x0, y1 - y0_top
        if w <= 1 or h <= 1:
            return None
        y_gl = vp_h - y1  # GL origin is bottom-left
        try:
            raw = _gl.glReadPixels(x0, y_gl, w, h, GL_RGB, GL_UNSIGNED_BYTE,
                                   outputType=bytes)
        except Exception as exc:
            logger.debug(f"Magnifier readback failed: {exc}")
            return None
        if not raw or len(raw) < w * h * 3:
            return None
        from PyQt6.QtGui import QImage
        img = QImage(raw, w, h, w * 3, QImage.Format.Format_RGB888)
        # glReadPixels is bottom-up; .copy() deep-copies so the temporary
        # byte buffer stays valid after this function returns.
        return img.mirrored(False, True).copy()

    def _draw_magnifier_overlay(self, vp_w: int, vp_h: int) -> None:
        """B2-3: cursor-following nearest-neighbour loupe."""
        if not self._magnifier_enabled:
            return
        if (self._view_mode == "3d" or self.is_quad_view()
                or not self._gl_initialized or not self._current_tex_id):
            return
        from PyQt6.QtGui import QCursor
        cursor = self.mapFromGlobal(QCursor.pos())
        if not (0 <= cursor.x() < self.width()
                and 0 <= cursor.y() < self.height()):
            return
        # Only meaningful while hovering the image itself.
        if self.get_image_pos_at_view_pos(
                QPointF(cursor), check_bounds=True) is None:
            return
        image = self._grab_framebuffer_region_image(
            cursor.x(), cursor.y(), vp_w, vp_h)
        if image is None:
            return

        from PyQt6.QtCore import QRectF
        from PyQt6.QtGui import QColor, QPainter, QPainterPath, QPen
        radius = self._MAGNIFIER_WINDOW_PX * 0.5
        # Park the loupe above-right of the cursor; flip at widget edges.
        cx = cursor.x() + radius + 14.0
        cy = cursor.y() - radius - 14.0
        if cx + radius > self.width() - 2:
            cx = cursor.x() - radius - 14.0
        if cy - radius < 2:
            cy = cursor.y() + radius + 14.0
        center = QPointF(cx, cy)
        target = QRectF(cx - radius, cy - radius, radius * 2.0, radius * 2.0)

        painter = QPainter(self)
        try:
            # Nearest-neighbour: pixels must stay crisp, never smoothed.
            painter.setRenderHint(
                QPainter.RenderHint.SmoothPixmapTransform, False)
            loupe = QPainterPath()
            loupe.addEllipse(center, radius, radius)
            painter.setClipPath(loupe)
            painter.drawImage(target, image)
            painter.setClipping(False)
            pen = QPen(QColor(80, 180, 255, 230))
            pen.setWidthF(1.5)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawEllipse(center, radius, radius)
            # Center crosshair.
            painter.setPen(QPen(QColor(80, 180, 255, 160)))
            painter.drawLine(QPointF(cx - 8.0, cy), QPointF(cx + 8.0, cy))
            painter.drawLine(QPointF(cx, cy - 8.0), QPointF(cx, cy + 8.0))
            # Magnification label.
            painter.setPen(QPen(QColor(230, 230, 230, 220)))
            font = painter.font()
            font.setPointSizeF(8.0)
            painter.setFont(font)
            painter.drawText(
                QRectF(cx - radius, cy + radius - 18.0,
                       radius * 2.0, 16.0),
                int(Qt.AlignmentFlag.AlignCenter),
                f"{int(self._magnifier_zoom)}x")
        finally:
            painter.end()

    def _draw_split_overlay(self) -> None:
        """Draw the split-view divider line and drag handle (QPainter)."""
        if not getattr(self, "_split_active", False):
            return
        if not (self._secondary_tex_id and self._secondary_tex_id > 0):
            return
        if not (self._tex_width and self._tex_height):
            return
        # Compute the image rectangle in view pixels (same as guides).
        tl = self.get_view_pos_at_image_pos(QPointF(0.0, 0.0))
        br = self.get_view_pos_at_image_pos(
            QPointF(float(self._tex_width), float(self._tex_height)))
        if tl is None or br is None:
            return
        from PyQt6.QtCore import QRectF
        from PyQt6.QtGui import QPainter, QColor, QPen
        img_rect = QRectF(tl.x(), tl.y(), br.x() - tl.x(), br.y() - tl.y())
        if img_rect.width() <= 1:
            return
        split_x = img_rect.left() + img_rect.width() * self._split_pos
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            # Divider line.
            pen = QPen(QColor(255, 136, 0, 220))
            pen.setWidthF(1.5)
            painter.setPen(pen)
            painter.drawLine(
                split_x, img_rect.top(), split_x, img_rect.bottom())
            # Drag handle (small triangle marker at the centre).
            cy = img_rect.center().y()
            handle = QRectF(split_x - 5, cy - 9, 10, 18)
            painter.setBrush(QColor(255, 136, 0, 220))
            painter.setPen(QPen(QColor(255, 255, 255, 200)))
            painter.drawRoundedRect(handle, 2.0, 2.0)
        finally:
            painter.end()

    def _hit_split_handle(self, pos) -> bool:
        """Return True if ``pos`` is within the split-divider grab radius."""
        if not (self._tex_width and self._tex_height):
            return False
        tl = self.get_view_pos_at_image_pos(QPointF(0.0, 0.0))
        br = self.get_view_pos_at_image_pos(
            QPointF(float(self._tex_width), float(self._tex_height)))
        if tl is None or br is None:
            return False
        img_x0, img_x1 = tl.x(), br.x()
        img_y0, img_y1 = tl.y(), br.y()
        split_x = img_x0 + (img_x1 - img_x0) * self._split_pos
        # Grab band: 6px on either side of the divider, within image height.
        if pos.y() < img_y0 or pos.y() > img_y1:
            return False
        return abs(pos.x() - split_x) <= 8.0

    def _draw_gizmo_overlay(self) -> None:
        """Draw the transform gizmo axes on the viewport overlay (paintGL).

        Gizmo axes are projected into texture UV space (0..1); here we map
        them to viewport NDC. M2: quad 模式下 gizmo 画在**活动象限**内（用象限的
        设备像素矩形 + 象限内 letterbox，无 zoom/pan），与 _draw_quad_view 的纹理
        blit 区域对齐；非 quad 模式用整张纹理的 letterbox 矩形（带 zoom/pan）。
        """
        if self._selected_camera_is_active_viewer_camera():
            self._gizmo_visible = False
            return
        if not self._gizmo_visible or not self._overlay_prog:
            return
        self._update_gizmo_projection()
        draw_list = self._transform_gizmo.build_draw_list()
        if not draw_list:
            return
        tw, th = self._tex_width, self._tex_height
        if tw <= 0 or th <= 0:
            return

        if self.is_quad_view():
            # M2: quad 模式——gizmo 画在活动象限内。
            # 活动象限的设备像素矩形（GL 坐标）+ 象限内 letterbox（无 zoom/pan）。
            rect = self._active_quad_device_rect()
            if rect is None:
                return
            tex_w, tex_h = self._active_view_tex_size()
            dpr = self.devicePixelRatio() if self._gl_initialized else 1.0
            dpr = dpr if dpr > 0 else 1.0
            vp_w = float(self.width() * dpr)
            vp_h = float(self.height() * dpr)
            if vp_w <= 0 or vp_h <= 0:
                return
            rect_x, rect_y, rect_w, rect_h = rect
            # 象限内 letterbox（纹理 UV 全域 → 象限 NDC 子矩形内的 letterbox 区）
            lb = self._fit_letterbox_ndc(rect_w, rect_h, tex_w, tex_h)
            if lb is None or lb[2] == lb[0] or lb[3] == lb[1]:
                return
            lbx0, lby0, lbx1, lby1 = lb
            # 象限 NDC(lbx0..lbx1) → 全视口 NDC(-1..1)：象限矩形占全视口的比例
            # rect 在 GL 坐标（y 向上，原点左下），转 NDC：x=rect_x/(vp_w/2)-1
            qx0 = rect_x / (vp_w / 2.0) - 1.0
            qy0 = rect_y / (vp_h / 2.0) - 1.0
            qx1 = (rect_x + rect_w) / (vp_w / 2.0) - 1.0
            qy1 = (rect_y + rect_h) / (vp_h / 2.0) - 1.0

            def _uv_to_vp_ndc(uv):
                u, v = uv
                # UV(0..1) → 象限内 letterbox NDC，再映射到全视口 NDC
                lx = lbx0 + (lbx1 - lbx0) * u
                ly = lby0 + (lby1 - lby0) * v
                ndc_x = qx0 + (qx1 - qx0) * ((lx + 1.0) * 0.5)
                ndc_y = qy0 + (qy1 - qy0) * ((ly + 1.0) * 0.5)
                return (ndc_x, ndc_y)
        else:
            # 非 quad：整张纹理的 letterbox 矩形（带 zoom/pan，与场景对齐）
            full_ndc = self._ndc_from_pixel_rect(0, 0, tw, th, tw, th)
            if not full_ndc:
                return
            nx0, ny0, nx1, ny1 = full_ndc

            def _uv_to_vp_ndc(uv):
                u, v = uv
                return (nx0 + (nx1 - nx0) * u, ny0 + (ny1 - ny0) * v)

        _gl.glUseProgram(self._overlay_prog)
        color_loc = _gl.glGetUniformLocation(self._overlay_prog, b"u_color")
        pos_loc = _gl.glGetAttribLocation(self._overlay_prog, b"in_vert")
        # macOS Core Profile only supports glLineWidth(1.0); draw thick axes as
        # screen-space quad strips (constant pixel thickness) instead.
        dpr = self.devicePixelRatio()
        vp_w = max(1.0, float(self.width() * dpr))
        vp_h = max(1.0, float(self.height() * dpr))
        for item in draw_list:
            if hasattr(item, "start_uv") and hasattr(item, "end_uv"):
                s_ndc = _uv_to_vp_ndc(item.start_uv)
                e_ndc = _uv_to_vp_ndc(item.end_uv)
                self._draw_gizmo_axis(
                    s_ndc, e_ndc, item.color, pos_loc, color_loc, vp_w, vp_h,
                    arrow_head=(self._transform_gizmo.mode.value == "move"),
                    square_head=(self._transform_gizmo.mode.value == "scale"),
                )
            elif hasattr(item, "points_uv"):
                pts_ndc = [_uv_to_vp_ndc(uv) for uv in item.points_uv]
                if getattr(item, "name", "") in ("xy", "xz", "yz"):
                    self._draw_gizmo_polyline(
                        pts_ndc, item.color, pos_loc, color_loc, vp_w, vp_h,
                        thickness_px=2.0)
                else:
                    self._draw_gizmo_ring(
                        pts_ndc, item.color, pos_loc, color_loc, vp_w, vp_h,
                        thickness_px=float(getattr(item, "thickness_px", 2.6)))
            elif hasattr(item, "center_uv"):
                center_ndc = _uv_to_vp_ndc(item.center_uv)
                if hasattr(item, "side_px"):
                    self._draw_filled_screen_square(
                        center_ndc, float(item.side_px), item.color,
                        pos_loc, color_loc)
                else:
                    self._draw_glow_dot(
                        center_ndc, float(getattr(item, "radius_px", 3.2)),
                        item.color, pos_loc, color_loc,
                        glow_color=(1.0, 0.86, 0.08, 0.22))
        _gl.glUseProgram(0)

    def _draw_node_icons_overlay(self) -> None:
        """P4: 绘制 Camera3D/Light3D 节点图标（视锥线框/灯光形态）。

        仅 3D 模式 + 有 overlay prog 时绘制。从节点图收集所有 Camera3D/Light3D
        节点参数，更新 NodeIconRenderer 投影，注册图标，用 _draw_overlay_thick_line
        画线框。图标不进合成输出（仅视口辅助显示）。

        投影矩阵取自 _update_gizmo_projection 同款逻辑（当前查看相机）。
        """
        if self._view_mode != "3d":
            return
        if not self._overlay_prog:
            return
        # 收集节点图的 Camera3D/Light3D 节点
        icons_data = self._collect_node_icons()
        if not icons_data:
            return

        # 获取当前投影矩阵（复用 gizmo 的投影更新逻辑）
        proj, view, cam_dist, cam_fov, tex_w, tex_h = self._get_overlay_projection()
        if proj is None:
            return

        # 更新图标渲染器投影 + 注册图标
        self._node_icon_renderer.update(proj, view, tex_w, tex_h, cam_dist, cam_fov)
        selected_id = self._get_selected_node_id()
        active_cam_id = self._get_active_camera_id()
        self._node_icon_renderer.active_node_id = active_cam_id
        for icon in icons_data:
            icon["selected"] = (icon["node_id"] == selected_id)
            if icon["type"] == "camera3d":
                self._node_icon_renderer.add_camera_icon(
                    node_id=icon["node_id"],
                    eye=icon["eye"], target=icon["target"],
                    near=icon["near"], far=icon["far"],
                    fov=icon["fov"], aspect=icon["aspect"],
                    selected=icon["selected"],
                    active=(icon["node_id"] == active_cam_id),
                    camera_mode=icon.get("camera_mode", "orbit"),
                    ortho_scale=icon.get("ortho_scale", 5.0),
                    roll=icon.get("roll", 0.0),
                    lens_shift_x=icon.get("lens_shift_x", 0.0),
                    lens_shift_y=icon.get("lens_shift_y", 0.0),
                    focus_distance=icon.get("focus_distance", 5.0),
                    dof_enable=icon.get("dof_enable", False),
                    fstop=icon.get("fstop", 2.8),
                    focal_length=icon.get("focal_length", 38.6))
            else:
                self._node_icon_renderer.add_light_icon(
                    node_id=icon["node_id"],
                    light_type=icon["light_type"],
                    pos=icon["pos"], direction=icon["direction"],
                    color=icon["color"], icon_pos=icon["icon_pos"],
                    range=icon["range"], outer_cone=icon["outer_cone"],
                    selected=icon["selected"])

        # 生成绘制数据 + 绘制
        draw_list = self._node_icon_renderer.build_draw_list()
        if not draw_list:
            return

        # 用 overlay prog 画线（与 _draw_gizmo_overlay 同模式）
        from OpenGL import GL as GL
        _gl.glUseProgram(self._overlay_prog)
        try:
            pos_loc = _gl.glGetAttribLocation(self._overlay_prog, b"in_vert")
            color_loc = _gl.glGetUniformLocation(self._overlay_prog, b"u_color")
        except Exception:
            pos_loc = color_loc = -1
        if pos_loc < 0:
            _gl.glUseProgram(0)
            return

        # UV → 视口 NDC 映射（与 gizmo overlay 一致）
        dpr = self.devicePixelRatio() if self._gl_initialized else 1.0
        vp_w = max(1.0, float(self.width() * dpr))
        vp_h = max(1.0, float(self.height() * dpr))
        if self.is_quad_view():
            rect = self._active_quad_device_rect()
            if rect is None:
                _gl.glUseProgram(0)
                return
            tex_w, tex_h = self._active_view_tex_size()
            rect_x, rect_y, rect_w, rect_h = rect
            lb = self._fit_letterbox_ndc(rect_w, rect_h, tex_w, tex_h)
            if lb is None or lb[2] == lb[0] or lb[3] == lb[1]:
                _gl.glUseProgram(0)
                return
            lbx0, lby0, lbx1, lby1 = lb
            qx0 = rect_x / (vp_w / 2.0) - 1.0
            qy0 = rect_y / (vp_h / 2.0) - 1.0
            qx1 = (rect_x + rect_w) / (vp_w / 2.0) - 1.0
            qy1 = (rect_y + rect_h) / (vp_h / 2.0) - 1.0

            def _uv_to_vp_ndc(uv):
                u, v = uv
                lx = lbx0 + (lbx1 - lbx0) * u
                ly = lby0 + (lby1 - lby0) * v
                return (
                    qx0 + (qx1 - qx0) * ((lx + 1.0) * 0.5),
                    qy0 + (qy1 - qy0) * ((ly + 1.0) * 0.5),
                )
        else:
            tw, th = self._tex_width, self._tex_height
            full_ndc = self._ndc_from_pixel_rect(0, 0, tw, th, tw, th)
            if not full_ndc:
                _gl.glUseProgram(0)
                return
            nx0, ny0, nx1, ny1 = full_ndc

            def _uv_to_vp_ndc(uv):
                u, v = uv
                return (nx0 + (nx1 - nx0) * u, ny0 + (ny1 - ny0) * v)

        for icon_line in draw_list:
            pts = icon_line.points_uv
            if len(pts) < 2:
                continue
            # 画折线（逐段）
            seg_count = len(pts) if icon_line.closed else len(pts) - 1
            r, g, b = icon_line.color
            color = (r, g, b, 1.0)
            for i in range(seg_count):
                s = pts[i]
                e = pts[(i + 1) % len(pts)] if icon_line.closed else pts[i + 1]
                s_ndc = _uv_to_vp_ndc(s)
                e_ndc = _uv_to_vp_ndc(e)
                self._draw_overlay_thick_line(
                    s_ndc, e_ndc, color, pos_loc, color_loc,
                    thickness_px=icon_line.width, vp_w=vp_w, vp_h=vp_h)
        _gl.glUseProgram(0)

    def _collect_node_icons(self) -> list:
        """从节点图收集所有 Camera3D/Light3D 节点的图标数据。

        遍历图中 camera3d/light3d 节点，提取位置/方向/类型等参数供图标渲染用。
        只收集参数（不做投影），投影在 _draw_node_icons_overlay 中完成。
        """
        icons = []
        try:
            graph = self._get_composite_graph()
            if graph is None:
                return icons
            page = getattr(self, '_composite_page', None)
            active_viewer_camera = str(
                getattr(page, '_viewer3d_active_camera_id', '') or '')
            for node in graph.nodes.values():
                if node.node_type == "camera3d":
                    # When looking through a Camera3D, hide that camera's own
                    # icon/frustum. Showing the body of the camera currently
                    # used as the view camera puts an overlay directly in front
                    # of the user and makes framing harder. Other cameras stay
                    # visible and selectable.
                    if active_viewer_camera and node.id == active_viewer_camera:
                        continue
                    p = node.parameters
                    from src.features.composite.render3d.camera_model import (
                        filmback_spec, fov_from_focal_and_filmback,
                        world_unit_scale)
                    unit_scale = world_unit_scale(str(p.get("cam_unit", "m")))
                    camera_mode = str(p.get("camera_mode", "orbit"))
                    if camera_mode == "free":
                        eye = (float(p.get("cam_eye_x", 4.0)) * unit_scale,
                               float(p.get("cam_eye_y", 3.0)) * unit_scale,
                               float(p.get("cam_eye_z", 5.0)) * unit_scale)
                    else:
                        # orbit/ortho：用参数推算 eye（近似，用于图标定位）
                        from src.features.composite.render3d import math_utils as m3d
                        target = m3d.vec3(float(p.get("cam_target_x", 0.0)) * unit_scale,
                                          float(p.get("cam_target_y", 0.0)) * unit_scale,
                                          float(p.get("cam_target_z", 0.0)) * unit_scale)
                        eye = m3d.orbit_to_eye(
                            float(p.get("cam_dist", 5.0)) * unit_scale,
                            float(p.get("cam_orbit_yaw", 30.0)),
                            float(p.get("cam_orbit_pitch", 20.0)),
                            target=target)
                        eye = tuple(float(x) for x in eye)
                    w = max(1, int(p.get("width", 1920)))
                    h = max(1, int(p.get("height", 1080)))
                    aspect = w / h if h > 0 else 1.0
                    spec = filmback_spec(
                        str(p.get("cam_filmback", "custom")),
                        custom_width_mm=float(p.get("cam_sensor_width", 36.0)),
                        custom_height_mm=float(p.get("cam_sensor_size", 36.0)))
                    fov = fov_from_focal_and_filmback(
                        float(p.get("cam_focal_length", 38.6)), spec, aspect,
                        str(p.get("cam_gate_fit", "vertical")))
                    icons.append({
                        "type": "camera3d",
                        "node_id": node.id,
                        "eye": eye,
                        "target": (float(p.get("cam_target_x", 0.0)) * unit_scale,
                                   float(p.get("cam_target_y", 0.0)) * unit_scale,
                                   float(p.get("cam_target_z", 0.0)) * unit_scale),
                        "near": float(p.get("cam_near", 0.1)) * unit_scale,
                        "far": float(p.get("cam_far", 1000.0)) * unit_scale,
                        "fov": fov,
                        "aspect": aspect,
                        "camera_mode": camera_mode,
                        "ortho_scale": float(p.get("cam_ortho_scale", 5.0)) * unit_scale,
                        "roll": float(p.get("cam_roll", 0.0)),
                        "lens_shift_x": float(p.get("cam_lens_shift_x", 0.0)),
                        "lens_shift_y": float(p.get("cam_lens_shift_y", 0.0)),
                        "focus_distance": float(p.get("cam_focus_distance", 5.0)) * unit_scale,
                        "dof_enable": bool(p.get("cam_dof_enable", False)),
                        "fstop": float(p.get("cam_fstop", 2.8)),
                        "focal_length": float(p.get("cam_focal_length", 38.6)),
                        "selected": False,
                    })
                elif node.node_type == "light3d":
                    p = node.parameters
                    from src.features.composite.render3d import math_utils as m3d
                    from src.features.composite.render3d.camera_model import world_unit_scale
                    unit_scale = world_unit_scale(str(p.get("light_unit", "m")))
                    ldx = float(p.get("light_dir_x", -0.6))
                    ldy = float(p.get("light_dir_y", 0.7))
                    ldz = float(p.get("light_dir_z", 0.4))
                    icons.append({
                        "type": "light3d",
                        "node_id": node.id,
                        "light_type": str(p.get("light_type", "directional")),
                        "pos": (float(p.get("light_pos_x", 0.0)) * unit_scale,
                                float(p.get("light_pos_y", 0.0)) * unit_scale,
                                float(p.get("light_pos_z", 0.0)) * unit_scale),
                        "direction": (ldx, ldy, ldz),
                        "color": (float(p.get("light_color_r", 1.0)),
                                  float(p.get("light_color_g", 1.0)),
                                  float(p.get("light_color_b", 1.0))),
                        "icon_pos": (float(p.get("icon_pos_x", 0.0)) * unit_scale,
                                     float(p.get("icon_pos_y", 0.0)) * unit_scale,
                                     float(p.get("icon_pos_z", 0.0)) * unit_scale),
                        "range": float(p.get("light_range", 10.0)) * unit_scale,
                        "outer_cone": float(p.get("light_outer_cone", 45.0)),
                        "selected": False,
                    })
        except Exception:
            pass
        return icons

    def _get_overlay_projection(self):
        """获取当前 overlay 投影参数（proj/view/dist/fov/tex_size）。

        复用 gizmo 的投影更新逻辑，返回当前查看相机的投影矩阵和参数。
        """
        try:
            # 调用 gizmo 投影更新，它会填充 _transform_gizmo 的投影状态
            self._update_gizmo_projection()
            g = self._transform_gizmo
            return (g._proj, g._view, g._cam_dist, g._cam_fov,
                    g._tex_w_px, g._tex_h_px)
        except Exception:
            return None, None, 5.0, 50.0, 1, 1

    def _get_selected_node_id(self) -> Optional[str]:
        """获取当前选中的节点 id（用于图标高亮）。"""
        try:
            return self.get_selected_node_id()
        except Exception:
            return None

    def _get_active_camera_id(self) -> Optional[str]:
        """获取当前查看的 Scene3D 的活动渲染相机 node_id（用于图标高亮）。

        新的全局 3D Viewer 里，Camera3D 是普通场景对象；只有属性栏的
        激活按钮会让它临时接管 Viewer 导航相机。这个状态优先于旧的
        Scene3D.active_camera_id。

        从 page 的 _viewer3d_node_id 找到查看的 scene3d，读其 active_camera_id
        参数。若未指定 active_camera_id（""），退化为该 scene3d 连接的第一个
        camera3d（与 Scene3D 渲染逻辑一致）。无查看 scene3d / 无 camera 连接
        时返回 None。
        """
        try:
            page = getattr(self, '_composite_page', None)
            active_viewer_camera = getattr(page, '_viewer3d_active_camera_id', None)
            if active_viewer_camera:
                return str(active_viewer_camera)
            graph = getattr(page, '_node_graph', None) if page is not None else None
            if graph is None:
                return None
            scene3d_id = getattr(page, '_viewer3d_node_id', None) if page is not None else None
            if not scene3d_id:
                # 兜底：找图中第一个 scene3d
                for n in graph.nodes.values():
                    if n.node_type == "scene3d":
                        scene3d_id = n.id
                        break
            if not scene3d_id:
                return None
            node = graph.get_node(scene3d_id)
            if node is None or node.node_type != "scene3d":
                return None
            active_id = str(node.parameters.get("active_camera_id", ""))
            if active_id:
                return active_id
            # 退化：active_camera_id 为空 → 用第一个连接的 camera3d（与渲染一致）
            for conn in graph.connections:
                if (conn.dst_node_id == scene3d_id
                        and conn.dst_port_name == "in"):
                    up = graph.get_node(conn.src_node_id)
                    if up is not None and up.node_type == "camera3d":
                        return up.id
            return None
        except Exception:
            return None

    def _hit_test_node_icon(self, view_pos) -> Optional["IconHitTarget"]:
        """P5：在 view_pos 处拾取 Camera3D/Light3D 节点图标。

        复用 _draw_node_icons_overlay 的图标注册逻辑，把鼠标位置转到纹理像素
        坐标后调 NodeIconRenderer.hit_test。命中返回 IconHitTarget，否则 None。
        """
        try:
            icons_data = self._collect_node_icons()
            if not icons_data:
                return None
            proj, view, cam_dist, cam_fov, tex_w, tex_h = self._get_overlay_projection()
            if proj is None:
                return None
            self._node_icon_renderer.update(proj, view, tex_w, tex_h, cam_dist, cam_fov)
            for icon in icons_data:
                icon["selected"] = False
                if icon["type"] == "camera3d":
                    self._node_icon_renderer.add_camera_icon(
                        node_id=icon["node_id"], eye=icon["eye"], target=icon["target"],
                        near=icon["near"], far=icon["far"],
                        fov=icon["fov"], aspect=icon["aspect"],
                        camera_mode=icon.get("camera_mode", "orbit"),
                        ortho_scale=icon.get("ortho_scale", 5.0),
                        roll=icon.get("roll", 0.0),
                        lens_shift_x=icon.get("lens_shift_x", 0.0),
                        lens_shift_y=icon.get("lens_shift_y", 0.0),
                        focus_distance=icon.get("focus_distance", 5.0),
                        dof_enable=icon.get("dof_enable", False),
                        fstop=icon.get("fstop", 2.8),
                        focal_length=icon.get("focal_length", 38.6))
                else:
                    self._node_icon_renderer.add_light_icon(
                        node_id=icon["node_id"], light_type=icon["light_type"],
                        pos=icon["pos"], direction=icon["direction"],
                        color=icon["color"], icon_pos=icon["icon_pos"],
                        range=icon["range"], outer_cone=icon["outer_cone"])
            # view_pos → 纹理像素坐标
            tex_px = self._view_pos_to_gizmo_tex_px(view_pos)
            if tex_px is None:
                return None
            return self._node_icon_renderer.hit_test(tex_px[0], tex_px[1])
        except Exception:
            return None

    def _select_node_by_id(self, node_id: str) -> None:
        """P5：选中指定节点（触发节点图高亮 + 属性面板聚焦）。

        通过 page 的节点编辑器接口选中节点，复用现有选中机制。
        """
        try:
            page = getattr(self, '_composite_page', None)
            if page is not None and hasattr(page, '_node_editor'):
                editor = page._node_editor
                if hasattr(editor, 'select_node') and node_id in editor._node_items:
                    editor.select_node(node_id)
                    return
                if hasattr(editor, 'select_node_by_id'):
                    editor.select_node_by_id(node_id)
                    return
                # ID passes may return a stable imported-mesh id rather than
                # a graph node id. Let CompositePage map that child selection
                # back to the owning Model3D while preserving the child id.
                scene_handler = getattr(
                    page, '_on_scene_outliner_node_selected', None)
                if callable(scene_handler):
                    scene_handler(node_id)
                    return
                # 回退：清空选中后设选中
                editor.clear_selection()
                item = editor._node_items.get(node_id)
                if item is not None:
                    item.setSelected(True)
        except Exception:
            pass

    def _icon_drag_param_keys(self, node_type: str, params: dict) -> list:
        """返回图标拖动会修改的参数键（按节点类型 + 子类型）。

        Camera3D orbit → cam_target_*；free → cam_eye_*；
        Light3D point/spot → light_pos_*；directional/ambient → icon_pos_*。
        """
        if node_type == "camera3d":
            mode = str(params.get("camera_mode", "orbit"))
            if mode == "free":
                return ["cam_eye_x", "cam_eye_y", "cam_eye_z"]
            return ["cam_target_x", "cam_target_y", "cam_target_z"]
        # light3d
        light_type = str(params.get("light_type", "directional"))
        if light_type in ("point", "spot"):
            return ["light_pos_x", "light_pos_y", "light_pos_z"]
        return ["icon_pos_x", "icon_pos_y", "icon_pos_z"]

    def _handle_icon_drag_move(self, event) -> None:
        """P5：拖动 Camera3D/Light3D 图标时，把鼠标位移映射到节点参数。

        简化策略：用屏幕空间 delta 转世界空间平移（沿视图平面），
        回写到节点的 pos/icon_pos 参数。Camera3D orbit 模式回写 target；
        free 模式回写 eye；Light3D point/spot 回写 pos，directional 回写 icon_pos。

        undo before 由 page 端 _on_viewer3d_nav_started 记录（图标拾取时通过
        scene3d_nav_started 信号触发），release 时 emit committed 走标准 undo。
        """
        if not self._icon_drag_active or self._icon_drag_target is None:
            return
        try:
            from PyQt6.QtCore import QPointF
            target = self._icon_drag_target
            node_id = target.node_id
            node_type = target.node_type
            page = getattr(self, '_composite_page', None)
            if page is None:
                return
            graph = getattr(page, '_node_graph', None)
            if graph is None:
                return
            node = graph.get_node(node_id)
            if node is None:
                return
            current_pos = QPointF(event.pos())
            last_pos = self._icon_drag_last_pos
            if last_pos is None:
                self._icon_drag_last_pos = current_pos
                return
            # 图标拖动会修改的参数键（undo before 由 page 端 nav_started 记录）
            param_keys = self._icon_drag_param_keys(node_type, node.parameters)
            # 屏幕像素 delta → 世界 delta（沿视图平面）
            import numpy as np
            dpr = self.devicePixelRatio() if self._gl_initialized else 1.0
            dx_px = (current_pos.x() - last_pos.x()) * dpr
            dy_px = (current_pos.y() - last_pos.y()) * dpr
            # 用 gizmo 投影的 world_per_tex_px 近似世界单位/像素
            world_per_px = self._node_icon_renderer._world_per_tex_px()
            # 视图右方/上方在世界空间的方向（从 view 矩阵提取）
            view = self._transform_gizmo._view
            if view is not None:
                right = np.array([view[0][0], view[1][0], view[2][0]])  # view 矩阵列 = 世界轴
                up = np.array([view[0][1], view[1][1], view[2][1]])
            else:
                right = np.array([1.0, 0.0, 0.0])
                up = np.array([0.0, 1.0, 0.0])
            world_dx = right * (dx_px * world_per_px)
            world_dy = up * (-dy_px * world_per_px)  # 屏幕 y 向下，世界 y 向上
            world_delta = world_dx + world_dy

            # 回写到节点参数（增量累加：param_keys 顺序对应 world_delta 的 xyz 分量）
            params = node.parameters
            for idx, k in enumerate(param_keys):
                params[k] = float(params.get(k, 0.0)) + float(world_delta[idx])
            node.param_revision += 1
            self._icon_drag_last_pos = current_pos
            # 触发重渲染
            if hasattr(page, '_evaluate_graph'):
                page._evaluate_graph()
            self.update()
        except Exception:
            pass

    def _handle_icon_drag_release(self, event) -> None:
        """P5：图标拖动结束，提交 undo 快照。

        收集本次拖动实际修改的参数新值，连同 node_id emit 给 page 端
        走 nav_committed 的 undo 合并流程（与 gizmo/相机导航同款 undo 路径）。
        page 端 _on_viewer3d_nav_started 已记录 affected keys 的 before 值，
        committed 据此 diff 生成 undo 步骤。
        """
        if not self._icon_drag_active:
            return
        target = self._icon_drag_target
        self._icon_drag_active = False
        self._icon_drag_target = None
        self._icon_drag_last_pos = None
        self.setCursor(Qt.CursorShape.ArrowCursor)
        if target is None:
            return
        # 收集 affected keys 的当前值，emit 给 page 提交 undo
        try:
            node_id = target.node_id
            page = getattr(self, '_composite_page', None)
            graph = getattr(page, '_node_graph', None) if page is not None else None
            node = graph.get_node(node_id) if graph is not None else None
            if node is None:
                return
            param_keys = self._icon_drag_param_keys(target.node_type, node.parameters)
            params = {k: float(node.parameters.get(k, 0.0)) for k in param_keys}
            self.scene3d_nav_committed.emit(node_id, params)
        except Exception as e:
            logger.warning("Icon drag undo commit failed: %s", e)

    def _draw_overlay_thick_line(self, start_ndc: tuple, end_ndc: tuple,
                                 color: tuple, pos_loc: int, color_loc: int,
                                 thickness_px: float = 3.0,
                                 vp_w: float = 0.0, vp_h: float = 0.0) -> None:
        """Draw a line as a screen-space quad strip with constant pixel thickness.

        macOS Core Profile caps glLineWidth at 1.0 (GL_INVALID_VALUE otherwise),
        so thick lines must be emulated by expanding the segment into a quad in
        NDC space, with half-thickness offsets along the screen-space normal.

        Args:
            start_ndc, end_ndc: line endpoints in viewport NDC (-1..1).
            thickness_px: desired thickness in device pixels.
            vp_w, vp_h: viewport size in device pixels (for px→NDC conversion).
        """
        if vp_w <= 0 or vp_h <= 0:
            return
        r, g, b, a = color
        x0, y0 = start_ndc
        x1, y1 = end_ndc
        # NDC per pixel (account for aspect so thickness is round on screen)
        ndc_per_px_x = 2.0 / vp_w
        ndc_per_px_y = 2.0 / vp_h
        # Segment direction + screen-space normal (perpendicular)
        dx = (x1 - x0)
        dy = (y1 - y0)
        seg_len2 = dx * dx + dy * dy
        if seg_len2 < 1e-12:
            return
        # normal in pixel space, then scaled to NDC (so offset is thickness_px/2)
        # normal direction (in uniform NDC) perpendicular to (dx, dy)
        nx = -dy
        ny = dx
        nlen = np.hypot(nx, ny)
        if nlen < 1e-12:
            return
        nx /= nlen
        ny /= nlen
        # Convert normal to pixel units, scale by half-thickness, back to NDC
        off_px = thickness_px * 0.5
        ox = nx * off_px * ndc_per_px_x
        oy = ny * off_px * ndc_per_px_y

        # Quad strip: 4 verts (triangle strip), same vertex layout as
        # _draw_overlay_line (stride 16, but we only fill pos2 + pad2).
        vertices = np.array([
            x0 - ox, y0 - oy, 0.0, 0.0,   # near left
            x0 + ox, y0 + oy, 0.0, 0.0,   # near right
            x1 - ox, y1 - oy, 0.0, 0.0,   # far left
            x1 + ox, y1 + oy, 0.0, 0.0,   # far right
        ], dtype=np.float32)

        _gl.glUniform4f(color_loc, ctypes.c_float(r), ctypes.c_float(g),
                        ctypes.c_float(b), ctypes.c_float(a))
        _gl.glBindBuffer(GL_ARRAY_BUFFER, self._quad_vbo)
        _gl.glBufferSubData(GL_ARRAY_BUFFER, 0, vertices.nbytes, vertices)
        _gl.glBindVertexArray(self._vao)
        _gl.glEnableVertexAttribArray(pos_loc)
        _gl.glVertexAttribPointer(pos_loc, 2, GL_FLOAT, GL_FALSE, 16, ctypes.c_void_p(0))
        _gl.glEnable(GL_BLEND)
        _gl.glBlendFunc(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA)
        _gl.glDrawArrays(GL_TRIANGLE_STRIP, 0, 4)
        _gl.glDisable(GL_BLEND)
        _gl.glBindVertexArray(0)
        _gl.glBindBuffer(GL_ARRAY_BUFFER, 0)

    def _draw_gizmo_axis(self, start_ndc: tuple, end_ndc: tuple, color: tuple,
                         pos_loc: int, color_loc: int, vp_w: float, vp_h: float,
                         arrow_head: bool = False,
                         square_head: bool = False) -> None:
        """Draw one C4D-style gizmo axis in screen space."""
        self._draw_overlay_thick_line(
            start_ndc, end_ndc, color, pos_loc, color_loc,
            thickness_px=2.0, vp_w=vp_w, vp_h=vp_h)
        if arrow_head:
            self._draw_gizmo_arrow_head(
                start_ndc, end_ndc, color, pos_loc, color_loc, vp_w, vp_h)
        if square_head:
            self._draw_filled_screen_square(
                end_ndc, 9.0, color, pos_loc, color_loc)

    def _draw_gizmo_polyline(self, points_ndc: list[tuple], color: tuple,
                             pos_loc: int, color_loc: int,
                             vp_w: float, vp_h: float,
                             thickness_px: float = 2.0) -> None:
        """Draw an open polyline with constant pixel thickness."""
        if len(points_ndc) < 2:
            return
        for i in range(len(points_ndc) - 1):
            self._draw_overlay_thick_line(
                points_ndc[i], points_ndc[i + 1], color, pos_loc, color_loc,
                thickness_px=thickness_px, vp_w=vp_w, vp_h=vp_h)

    def _draw_gizmo_ring(self, points_ndc: list[tuple], color: tuple,
                         pos_loc: int, color_loc: int,
                         vp_w: float, vp_h: float,
                         thickness_px: float = 2.6) -> None:
        """Draw a closed C4D-style rotation ring."""
        if len(points_ndc) < 3:
            return
        for i in range(len(points_ndc)):
            self._draw_overlay_thick_line(
                points_ndc[i], points_ndc[(i + 1) % len(points_ndc)],
                color, pos_loc, color_loc,
                thickness_px=thickness_px, vp_w=vp_w, vp_h=vp_h)

    def _draw_gizmo_arrow_head(self, start_ndc: tuple, end_ndc: tuple,
                               color: tuple, pos_loc: int, color_loc: int,
                               vp_w: float, vp_h: float) -> None:
        """Draw a filled triangular arrowhead at the end of a gizmo axis."""
        sx, sy = self._ndc_to_screen_px(start_ndc, vp_w, vp_h)
        ex, ey = self._ndc_to_screen_px(end_ndc, vp_w, vp_h)
        dx = ex - sx
        dy = ey - sy
        length = float(np.hypot(dx, dy))
        if length < 1e-6:
            return
        ux = dx / length
        uy = dy / length
        nx = -uy
        ny = ux
        head_len = 17.0
        half_w = 7.0
        bx = ex - ux * head_len
        by = ey - uy * head_len
        pts_px = [
            (ex, ey),
            (bx + nx * half_w, by + ny * half_w),
            (bx - nx * half_w, by - ny * half_w),
        ]
        pts_ndc = [self._screen_px_to_ndc(p, vp_w, vp_h) for p in pts_px]
        self._draw_filled_ndc_polygon(pts_ndc, color, pos_loc, color_loc)

    def _draw_filled_screen_square(self, center_ndc: tuple, side_pixels: float,
                                   color: tuple, pos_loc: int,
                                   color_loc: int) -> None:
        """Draw a filled C4D-style screen-aligned square with dark outline."""
        dpr = self.devicePixelRatio()
        vp_w = max(1.0, float(self.width() * dpr))
        vp_h = max(1.0, float(self.height() * dpr))
        cx, cy = self._ndc_to_screen_px(center_ndc, vp_w, vp_h)
        half = side_pixels * 0.5
        pts_px = [
            (cx - half, cy - half),
            (cx + half, cy - half),
            (cx + half, cy + half),
            (cx - half, cy + half),
        ]
        pts_ndc = [self._screen_px_to_ndc(p, vp_w, vp_h) for p in pts_px]
        self._draw_filled_ndc_polygon(pts_ndc, color, pos_loc, color_loc)
        self._draw_overlay_quad(pts_ndc, (0.02, 0.02, 0.02, 0.85),
                                pos_loc, color_loc)

    def _draw_filled_ndc_polygon(self, points_ndc: list[tuple], color: tuple,
                                 pos_loc: int, color_loc: int) -> None:
        """Draw a convex filled polygon using GL_TRIANGLE_FAN."""
        if len(points_ndc) < 3:
            return
        r, g, b, a = color
        verts = []
        for x, y in points_ndc:
            verts.extend([x, y, 0.0, 0.0])
        vertices = np.array(verts, dtype=np.float32)
        _gl.glUniform4f(color_loc, ctypes.c_float(r), ctypes.c_float(g),
                        ctypes.c_float(b), ctypes.c_float(a))
        _gl.glBindBuffer(GL_ARRAY_BUFFER, self._quad_vbo)
        if vertices.nbytes > 64:
            _gl.glBufferData(GL_ARRAY_BUFFER, vertices.nbytes, vertices, GL_DYNAMIC_DRAW)
        else:
            _gl.glBufferSubData(GL_ARRAY_BUFFER, 0, vertices.nbytes, vertices)
        _gl.glBindVertexArray(self._vao)
        _gl.glEnableVertexAttribArray(pos_loc)
        _gl.glVertexAttribPointer(pos_loc, 2, GL_FLOAT, GL_FALSE, 16, ctypes.c_void_p(0))
        _gl.glEnable(GL_BLEND)
        _gl.glBlendFunc(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA)
        _gl.glDrawArrays(GL_TRIANGLE_FAN, 0, len(points_ndc))
        _gl.glDisable(GL_BLEND)
        _gl.glBindVertexArray(0)
        _gl.glBindBuffer(GL_ARRAY_BUFFER, 0)

    @staticmethod
    def _ndc_to_screen_px(ndc: tuple, vp_w: float, vp_h: float) -> tuple[float, float]:
        """Viewport NDC (-1..1) to device pixels with a bottom-left origin."""
        return ((float(ndc[0]) + 1.0) * 0.5 * vp_w,
                (float(ndc[1]) + 1.0) * 0.5 * vp_h)

    @staticmethod
    def _screen_px_to_ndc(px: tuple, vp_w: float, vp_h: float) -> tuple[float, float]:
        """Device pixels with a bottom-left origin to viewport NDC."""
        return ((float(px[0]) / max(1.0, vp_w)) * 2.0 - 1.0,
                (float(px[1]) / max(1.0, vp_h)) * 2.0 - 1.0)

    def _view_pos_to_gizmo_tex_px(self, view_pos: QPointF) -> Optional[tuple]:
        """Widget pixel → texture-pixel (origin bottom-left) for gizmo hit test.

        Reuses the image-pos mapping (top-left origin) then flips Y to GL.
        Returns None if the cursor is outside the texture (no gizmo hit).
        """
        img_pos = self.get_image_pos_at_view_pos(view_pos, check_bounds=False)
        if img_pos is None:
            return None
        th = max(1, self._tex_height)
        # img_pos 是 top-left origin；转 bottom-left origin 的纹理像素
        return (float(img_pos.x()), float(th - img_pos.y()))

    def _view_pos_to_gizmo_tex_ndc(self, view_pos: QPointF) -> Optional[tuple]:
        """Widget pixel → texture NDC (-1..1, Y up) for gizmo drag raycast."""
        tex_px = self._view_pos_to_gizmo_tex_px(view_pos)
        if tex_px is None:
            return None
        tw = max(1, self._tex_width)
        th = max(1, self._tex_height)
        return (tex_px[0] / tw * 2.0 - 1.0, tex_px[1] / th * 2.0 - 1.0)

    # ── M2: 象限感知坐标映射（quad 模式 gizmo 编辑核心）──────────────────────
    # 问题：原 _view_pos_to_gizmo_tex_* 假设纹理占满整个 widget（带 zoom/pan），
    # 但 quad 模式下纹理只在活动象限内（无 zoom/pan，独立 letterbox）。
    # 这里提供 quad 感知的转换器，hit_test/drag 收到的纹理空间必须与
    # _update_gizmo_projection 传给 gizmo 的 _tex_w_px × _tex_h_px 一致。

    def _active_quad_device_rect(self) -> Optional[tuple]:
        """返回当前活动视图的设备像素矩形（GL 坐标，y 原点左下）。

        SINGLE 模式 = 全内容区（排除工具栏/播放条后的整个区域）。
        QUAD 模式 = 活动象限的子矩形。返回 (x, y, w, h) 或 None（无可绘制区）。
        """
        from src.features.composite.ui.viewport3d_state import ViewportViewKind
        dpr = self.devicePixelRatio() if self._gl_initialized else 1.0
        vp_w = int(self.width() * dpr)
        vp_h = int(self.height() * dpr)
        top_h, bottom_h = self._overlay_bar_heights()
        content_x = 0
        content_y = bottom_h
        content_w = vp_w
        content_h = vp_h - top_h - bottom_h
        if content_h <= 0:
            return None
        if not self._viewport3d_ctrl.state.is_quad():
            # SINGLE：全内容区
            return (content_x, content_y, content_w, content_h)
        # QUAD：2×2 布局（与 _draw_quad_view 的 rects_gl 一致）
        cx = content_x + content_w / 2.0
        cy = content_y + content_h / 2.0
        half_w = content_w / 2.0
        half_h = content_h / 2.0
        active = self._viewport3d_ctrl.state.active_view
        rects_gl = {
            ViewportViewKind.PERSPECTIVE: (content_x, cy, half_w, half_h),
            ViewportViewKind.TOP:         (cx, cy, half_w, half_h),
            ViewportViewKind.RIGHT:       (content_x, content_y, half_w, half_h),
            ViewportViewKind.FRONT:       (cx, content_y, half_w, half_h),
        }
        return rects_gl.get(active)

    def _active_view_tex_size(self) -> tuple:
        """返回活动视图对应纹理的尺寸 (w, h)。

        QUAD 模式：透视=全分辨率，正交象限=半分辨率（与 _draw_quad_view 一致）。
        SINGLE 模式：正交 override 时也是全分辨率（Scene3D 用正交投影渲染主纹理）。
        必须与 _update_gizmo_projection 传给 gizmo 的 tex_w_px/tex_h_px 一致。
        """
        from src.features.composite.ui.viewport3d_state import ViewportViewKind
        tw = max(1, self._tex_width)
        th = max(1, self._tex_height)
        active = self._effective_view_kind()
        # 仅 quad 模式下的正交象限是半分辨率；单视图正交是全分辨率（主纹理）
        if (active in (ViewportViewKind.TOP, ViewportViewKind.RIGHT,
                       ViewportViewKind.FRONT)
                and self._viewport3d_ctrl.state.is_quad()):
            return (max(1, tw // 2), max(1, th // 2))
        return (tw, th)

    def _effective_view_kind(self):
        """返回当前应渲染/编辑的视图类型。

        QUAD 模式 = active_view；SINGLE 模式 = single_ortho_view 或 PERSPECTIVE。
        """
        from src.features.composite.ui.viewport3d_state import ViewportViewKind
        state = self._viewport3d_ctrl.state
        if state.is_quad():
            return state.active_view
        if state.single_ortho_view is not None:
            return state.single_ortho_view
        return ViewportViewKind.PERSPECTIVE

    def get_single_ortho_view(self) -> int:
        """M2: 单视图正交 override（0=perspective，1=top，2=right，3=front）。

        仅在 3D + SINGLE 模式下返回非 0；2D 或 quad 模式返回 0。
        """
        if self._view_mode != "3d":
            return 0
        state = self._viewport3d_ctrl.state
        if state.is_quad():
            return 0
        sov = state.single_ortho_view
        if sov is None:
            return 0
        from src.features.composite.ui.viewport3d_state import ViewportViewKind
        return {ViewportViewKind.TOP: 1, ViewportViewKind.RIGHT: 2,
                ViewportViewKind.FRONT: 3}.get(sov, 0)

    def get_quad_active_view(self) -> int:
        """Milestone C / P2：四视图活动视图代码（0=perspective，1=top，2=right，3=front）。

        quad 模式下返回鼠标所在象限（state.active_view）；非 quad / 非 3D 返回 0。
        Scene3D 据此对非活动正交视图降质/降频，保活动视图流畅（§6 四视图调度）。
        """
        if self._view_mode != "3d":
            return 0
        state = self._viewport3d_ctrl.state
        if not state.is_quad():
            return 0
        av = state.active_view
        from src.features.composite.ui.viewport3d_state import ViewportViewKind
        return {ViewportViewKind.PERSPECTIVE: 0, ViewportViewKind.TOP: 1,
                ViewportViewKind.RIGHT: 2, ViewportViewKind.FRONT: 3}.get(av, 0)

    def _view_pos_to_gizmo_tex_px_quad(self, view_pos: QPointF) -> Optional[tuple]:
        """象限感知的 widget px → 纹理像素（原点左下）。

        quad 模式：widget px → 减去活动象限 widget 原点 → 象限内 fit-letterbox
        （无 zoom/pan）→ 活动象限纹理尺寸空间的像素。
        非 quad 模式：退化为全内容区映射（无 zoom/pan）。
        返回 None 表示鼠标不在活动象限内。
        """
        rect = self._active_quad_device_rect()
        if rect is None:
            return None
        dpr = self.devicePixelRatio() if self._gl_initialized else 1.0
        dpr = dpr if dpr > 0 else 1.0
        # widget 逻辑像素 → 设备像素（y 仍向下）
        mx = float(view_pos.x()) * dpr
        my = float(view_pos.y()) * dpr
        vp_h = float(self.height()) * dpr
        # GL 坐标（y 原点左下）：widget my(向下) → gl_y = vp_h - my
        gl_x, gl_y = mx, vp_h - my
        rect_x, rect_y, rect_w, rect_h = rect
        # 鼠标在活动矩形内的局部坐标（GL，y 向上，原点左下）
        local_x = gl_x - rect_x
        local_y = gl_y - rect_y
        # 不在矩形内 → 无命中（gizmo 只在活动象限内）
        if local_x < 0.0 or local_x >= rect_w or local_y < 0.0 or local_y >= rect_h:
            return None
        # 象限内 fit-letterbox（复用 _fit_letterbox_ndc 的比例算式，无 zoom/pan）
        tex_w, tex_h = self._active_view_tex_size()
        lb = self._fit_letterbox_ndc(rect_w, rect_h, tex_w, tex_h)
        # lb 是象限内 NDC (x0,y0,x1,y1)；把局部像素映射到象限 NDC 再到纹理 UV
        qx_ndc_x = (local_x / rect_w) * 2.0 - 1.0
        qx_ndc_y = (local_y / rect_h) * 2.0 - 1.0
        lbx0, lby0, lbx1, lby1 = lb
        if lbx1 == lbx0 or lby1 == lby0:
            return None
        u = (qx_ndc_x - lbx0) / (lbx1 - lbx0)
        v = (qx_ndc_y - lby0) / (lby1 - lby0)
        # UV(0..1) → 纹理像素（已经是 bottom-left origin，因 NDC y 向上）
        return (u * tex_w, v * tex_h)

    def _view_pos_to_gizmo_tex_ndc_quad(self, view_pos: QPointF) -> Optional[tuple]:
        """象限感知的 widget px → 纹理 NDC (-1..1, Y up)。"""
        tex_px = self._view_pos_to_gizmo_tex_px_quad(view_pos)
        if tex_px is None:
            return None
        tex_w, tex_h = self._active_view_tex_size()
        return (tex_px[0] / tex_w * 2.0 - 1.0, tex_px[1] / tex_h * 2.0 - 1.0)

    def _gizmo_tex_px_for(self, view_pos: QPointF) -> Optional[tuple]:
        """调度：quad 模式用象限感知映射，否则用原全纹理映射。"""
        if self.is_quad_view():
            return self._view_pos_to_gizmo_tex_px_quad(view_pos)
        return self._view_pos_to_gizmo_tex_px(view_pos)

    def _gizmo_tex_ndc_for(self, view_pos: QPointF) -> Optional[tuple]:
        """调度：quad 模式用象限感知映射，否则用原全纹理映射。"""
        if self.is_quad_view():
            return self._view_pos_to_gizmo_tex_ndc_quad(view_pos)
        return self._view_pos_to_gizmo_tex_ndc(view_pos)

    def _selected_3d_orbit_center(self) -> Optional[tuple[float, float, float]]:
        """Return the selected 3D object's orbit center, if one is available.

        This uses the same anchors as the transform gizmo. For Geo3D/Model3D
        that is the object transform center; for Camera3D/Light3D it is their
        visible scene icon/body position. Mesh-level bounding boxes can be
        layered in later without changing the orbit-center mode contract.
        """
        params = self._selected_node_params or {}
        node_type = self._selected_node_type
        try:
            if node_type == "camera3d":
                return tuple(float(v) for v in self._camera_gizmo_anchor(params))
            if node_type == "light3d":
                return tuple(float(v) for v in self._light_gizmo_anchor(params))
            if node_type in ("geo3d", "model3d"):
                return (
                    float(params.get("object_pos_x", 0.0)),
                    float(params.get("object_pos_y", 0.0)),
                    float(params.get("object_pos_z", 0.0)),
                )
        except Exception:
            return None
        return None

    def _orbit_center_for_mode(self, mode: OrbitCenterMode) -> Optional[tuple[float, float, float]]:
        """Resolve the current orbit-center preference to a world point."""
        if mode == OrbitCenterMode.SCREEN:
            return None
        if mode == OrbitCenterMode.WORLD:
            return (0.0, 0.0, 0.0)
        if mode == OrbitCenterMode.CURSOR:
            return tuple(float(v) for v in self._viewport3d_ctrl.state.orbit_cursor_world)
        if mode == OrbitCenterMode.SELECTED:
            return self._selected_3d_orbit_center()
        if mode == OrbitCenterMode.AUTO:
            return self._selected_3d_orbit_center()
        return None

    def _prepare_orbit_center_for_drag(self) -> None:
        """Apply the configured orbit center before an Alt+Left orbit drag.

        The orbit pivot is independent from the view target. This lets an
        off-center selected object act as the rotation center without snapping
        it to the middle of the viewport at drag start.
        """
        self._orbit_pivot_world = None
        mode = self._viewport3d_ctrl.state.orbit_center_mode
        center = self._orbit_center_for_mode(mode)
        if center is None:
            return
        self._orbit_pivot_world = (
            float(center[0]), float(center[1]), float(center[2]))

    def _orbit_camera_around_pivot(self, yaw_delta: float, pitch_delta: float) -> None:
        """Orbit eye and target around a world-space pivot, preserving framing."""
        from src.features.composite.render3d import math_utils as m3d
        pivot_tuple = self._orbit_pivot_world
        if pivot_tuple is None:
            return
        pivot = m3d.vec3(*pivot_tuple)
        current_target = m3d.vec3(
            self._cam_target_x, self._cam_target_y, self._cam_target_z)
        eye = m3d.orbit_to_eye(
            max(0.1, self._cam_dist), self._orbit_yaw, self._orbit_pitch,
            target=current_target)
        eye_rel = eye - pivot
        target_rel = current_target - pivot
        world_up = m3d.vec3(0.0, 1.0, 0.0)
        if abs(yaw_delta) > 1e-6:
            eye_rel = m3d.rotate_around_axis(eye_rel, world_up, yaw_delta)
            target_rel = m3d.rotate_around_axis(target_rel, world_up, yaw_delta)
        yawed_eye = pivot + eye_rel
        yawed_target = pivot + target_rel
        if abs(pitch_delta) > 1e-6:
            forward = m3d.normalize(yawed_target - yawed_eye)
            right = m3d.normalize(np.cross(forward, world_up))
            if float(np.linalg.norm(right)) < 1e-6:
                right = m3d.vec3(1.0, 0.0, 0.0)
            eye_rel = m3d.rotate_around_axis(yawed_eye - pivot, right, -pitch_delta)
            target_rel = m3d.rotate_around_axis(yawed_target - pivot, right, -pitch_delta)
        next_eye = pivot + eye_rel
        next_target = pivot + target_rel
        dist, yaw, pitch = self._orbit_angles_from_eye_target(next_eye, next_target)
        self._cam_target_x = float(next_target[0])
        self._cam_target_y = float(next_target[1])
        self._cam_target_z = float(next_target[2])
        self._cam_dist = max(0.1, float(dist))
        self._orbit_yaw = float(yaw)
        self._orbit_pitch = max(-89.0, min(89.0, float(pitch)))

    def _handle_3d_press(self, event: QMouseEvent) -> bool:
        """Begin a 3D camera-navigation drag (Alt + button). Returns True if handled.

        Only Alt+button drags are handled here. Plain left-click (gizmo pick /
        object pick) is dispatched separately by ``_handle_3d_left_press`` so the
        two interaction classes stay distinct (Alt = navigate camera, plain =
        manipulate object).

        C4D convention: Alt+Left = orbit, Alt+Middle = pan, Alt+Right = dolly.

        M2: 正交视图（quad 的正交象限 或 单正交视图）下，Alt+Left 也做平移而非
        旋转（正交视图是轴对齐固定视角，旋转无意义）。Alt+Middle=平移，Alt+Right=缩放。
        """
        if not (event.modifiers() & Qt.KeyboardModifier.AltModifier):
            return False
        _is_ortho = self._viewport3d_ctrl.state.is_ortho_effective()
        if event.button() == Qt.MouseButton.LeftButton:
            # 正交视图下 Alt+Left → 平移（旋转无意义）；透视 → 轨道旋转
            self._nav_mode = "pan" if _is_ortho else "orbit"
            if self._nav_mode == "orbit":
                self._prepare_orbit_center_for_drag()
        elif event.button() == Qt.MouseButton.MiddleButton:
            self._nav_mode = "pan"
        elif event.button() == Qt.MouseButton.RightButton:
            self._nav_mode = "zoom"
        else:
            return False
        self._nav_is_active = True
        self._nav_last_pos = event.pos()
        self.setCursor(self._nav_cursor_for_mode(self._nav_mode))
        self.scene3d_nav_started.emit(self._nav_target_node_id())
        return True

    def _handle_3d_left_press(self, event: QMouseEvent) -> bool:
        """Handle a plain left-click in 3D mode: gizmo pick > icon pick > object pick.

        Priority (Nuke/Blender convention):
        1. If gizmo is visible and an axis is under the cursor → begin an axis
           drag (move object along that axis via projection constraint).
        2. Else try to pick a Camera3D/Light3D node icon → if hit, select the
           node and begin an icon drag (position the camera/light in the viewport).
           This branch also runs when gizmo is hidden (selected node is a
           camera3d/light3d) so those nodes remain operable in the viewport.
        3. Else raycast against the object mesh → if hit, select/feedback.

        Returns True if the click was consumed (so 2D tool dispatch is skipped).
        """
        view_pos = QPointF(event.pos())
        # ── 1. Gizmo axis pick（仅 gizmo 可见时尝试）──
        # gizmo 不可见（选中 camera3d/light3d）时跳过此步，直接走图标拾取。
        if self._gizmo_visible:
            # Phase 5: gizmo 模式跟随 3D 工具模式（MOVE/ROTATE/SCALE）。
            from src.features.composite.render3d.gizmo_overlay import GizmoMode
            _tool = self._viewport3d_ctrl.state.tool_mode
            _MODE_MAP = {
                "select3d": GizmoMode.MOVE,  # SELECT 默认退化到 MOVE（可点选物体）
                "move3d": GizmoMode.MOVE,
                "rotate3d": GizmoMode.ROTATE,
                "scale3d": GizmoMode.SCALE,
            }
            self._transform_gizmo.mode = _MODE_MAP.get(_tool.value, GizmoMode.MOVE)
            # 同步物体旋转/缩放到 gizmo（旋转/缩放锚点用）
            params = self._selected_node_params or {}
            if self._selected_node_type == "camera3d":
                eye, target = self._camera_source_eye_target(params)
                _, yaw, pitch = self._orbit_angles_from_eye_target(eye, target)
                self._transform_gizmo.set_object_rot((
                    float(params.get("cam_orbit_pitch", pitch)),
                    float(params.get("cam_orbit_yaw", yaw)),
                    float(params.get("cam_roll", 0.0))))
                self._transform_gizmo.set_object_scale(1.0)
            elif self._selected_node_type == "light3d":
                self._transform_gizmo.set_object_rot((0.0, 0.0, 0.0))
                self._transform_gizmo.set_object_scale(1.0)
            else:
                self._transform_gizmo.set_object_rot((
                    float(params.get("object_rot_x", 0.0)),
                    float(params.get("object_rot_y", 0.0)),
                    float(params.get("object_rot_z", 0.0))))
                self._transform_gizmo.set_object_scale(float(params.get("mesh_scale", 1.0)))
            self._update_gizmo_projection()
            # M2: quad 模式用象限感知映射（gizmo 在活动象限内编辑）
            tex_px = self._gizmo_tex_px_for(view_pos)
            axis = self._transform_gizmo.hit_test(tex_px) if tex_px is not None else None
            if axis is not None:
                # Phase 5: begin_drag 传入 mouse NDC（旋转/缩放增量锚点用）
                tex_ndc = self._gizmo_tex_ndc_for(view_pos)
                self._transform_gizmo.begin_drag(axis, tex_ndc)
                self._gizmo_drag_active = True
                self._gizmo_drag_source_params = dict(params)
                self._gizmo_drag_anchor_start = tuple(self._gizmo_obj_pos)
                # Record undo-before snapshot of object_pos_*/rot_*/scale
                self._gizmo_drag_before = {
                    "object_pos_x": float(params.get("object_pos_x", 0.0)),
                    "object_pos_y": float(params.get("object_pos_y", 0.0)),
                    "object_pos_z": float(params.get("object_pos_z", 0.0)),
                    "object_rot_x": float(params.get("object_rot_x", 0.0)),
                    "object_rot_y": float(params.get("object_rot_y", 0.0)),
                    "object_rot_z": float(params.get("object_rot_z", 0.0)),
                    "mesh_scale": float(params.get("mesh_scale", 1.0)),
                }
                self._gizmo_hovered_node_id = self._interaction_state.selected_node_id
                # Signal nav_started so the page records undo-before (we reuse the
                # cam-nav undo path for object moves too).
                self.scene3d_nav_started.emit(self._interaction_state.selected_node_id or "")
                self.setCursor(Qt.CursorShape.SizeAllCursor)
                self.update()
                return True
        # ── 2. Node icon pick (Camera3D/Light3D) ──
        # P4/P5：gizmo 未命中或 gizmo 不可见（选中 camera3d/light3d）时，
        # 尝试拾取 Camera3D/Light3D 图标。命中则选中对应节点（节点图高亮 +
        # 属性面板聚焦），并记录拖动起点（_handle_icon_drag_move 负责参数回写）。
        # 注意：图标投影依赖 _update_gizmo_projection 填充的缓存，gizmo 不可见
        # 分支下该方法仍会计算投影（已放宽到所有 3D 节点类型）。
        self._update_gizmo_projection()
        icon_hit = self._hit_test_node_icon(view_pos)
        if icon_hit is not None:
            self._select_node_by_id(icon_hit.node_id)
            self._icon_drag_target = icon_hit
            self._icon_drag_active = True
            self._icon_drag_last_pos = view_pos
            # 记录 undo-before 快照（图标拖动提交时用）
            self.scene3d_nav_started.emit(icon_hit.node_id)
            self.setCursor(Qt.CursorShape.SizeAllCursor)
            self.update()
            return True
        # ── 3. P1-22：Point/Edge/Polygon pick (CPU BVH snapshot) ──
        from src.features.composite.ui.viewport3d_state import ComponentMode
        component_mode = self._viewport3d_ctrl.state.component_mode
        if component_mode != ComponentMode.OBJECT:
            component = self.pick_component_at(view_pos)
            if component is not None:
                from src.features.composite.render3d.selection import (
                    SELECT_ADD, SELECT_SUBTRACT, SELECT_TOGGLE,
                    apply_component_hit)
                if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
                    selection_mode = SELECT_TOGGLE
                elif event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                    selection_mode = SELECT_ADD
                elif event.modifiers() & Qt.KeyboardModifier.AltModifier:
                    selection_mode = SELECT_SUBTRACT
                else:
                    selection_mode = "set"
                node_id = str(component.get("node_id", ""))
                mesh_id = str(component.get("mesh_id") or node_id)
                if node_id and apply_component_hit(
                        self._component_selection, mesh_id, component_mode,
                        component.get("hit"), selection_mode):
                    self._select_node_by_id(node_id)
                    self.set_selected_3d_scene_node(node_id)
                    self.component_selection_changed.emit(self._component_selection)
                    self.update()
            # Component mode owns the click even when the mesh snapshot is
            # unavailable or the ray misses; never fall through to 2D tools.
            return True

        # ── 4. P7：Object pick (GPU ID buffer) ──
        # 读 ID texture 在点击位置的 1 像素 → pick_id → node_id → 选中该节点。
        # 需在 viewer GL context 内执行（makeCurrent）；命中则选中 + 返回 True。
        try:
            self.makeCurrent()
            picked_nid = self.pick_object_at(view_pos)
            self.doneCurrent()
        except Exception:
            picked_nid = None
        if picked_nid:
            self._select_node_by_id(picked_nid)
            self.update()
            return True
        return False

    def _handle_3d_move(self, event: QMouseEvent) -> None:
        """Update camera params during a 3D navigation drag."""
        delta = event.pos() - self._nav_last_pos
        self._nav_last_pos = event.pos()
        if self._nav_mode == "orbit":
            yaw_delta = -delta.x() * 0.4
            pitch_delta = delta.y() * 0.4
            if self._orbit_pivot_world is not None:
                self._orbit_camera_around_pivot(yaw_delta, pitch_delta)
            else:
                self._orbit_yaw += yaw_delta
                self._orbit_pitch += pitch_delta
                self._orbit_pitch = max(-89.0, min(89.0, self._orbit_pitch))
        elif self._nav_mode == "zoom":
            factor = 1.0 + delta.y() * 0.01
            self._cam_dist = max(0.1, self._cam_dist * factor)
        elif self._nav_mode == "pan":
            # 正交视图用正交平移（沿视图固定轴）；透视用屏幕空间平移
            if self._viewport3d_ctrl.state.is_ortho_effective():
                self._pan_target_ortho(delta.x(), delta.y())
            else:
                self._pan_target_screen(delta.x(), delta.y())
        # Throttle preview emission to ~60fps.
        if not self._nav_preview_timer.isActive():
            self._nav_preview_timer.start()

    def _pan_target_screen(self, dx_px: float, dy_px: float) -> None:
        """Move cam_target along the camera's right/up vectors (real pan).

        dx/dy are screen pixel deltas (right/down positive). The target moves
        opposite to the drag so the scene follows the cursor.
        """
        from src.features.composite.render3d import math_utils as m3d
        target = m3d.vec3(self._cam_target_x, self._cam_target_y, self._cam_target_z)
        eye = m3d.orbit_to_eye(self._cam_dist, self._orbit_yaw,
                               self._orbit_pitch, target=target)
        forward = m3d.normalize(target - eye)
        world_up = m3d.vec3(0.0, 1.0, 0.0)
        right = m3d.normalize(np.cross(forward, world_up))
        up = np.cross(right, forward)
        # World units per pixel: scale with distance & fov so panning is
        # consistent regardless of zoom. fov≈50°, viewport height in world ≈
        # 2*dist*tan(fov/2); per-pixel ≈ that / viewport_height_px.
        fov_rad = math.radians(50.0)
        vp_h_px = max(1, self.height())
        world_per_px = (2.0 * self._cam_dist * math.tan(fov_rad * 0.5)) / vp_h_px
        # Move target opposite to drag direction (drag right → scene moves right
        # under cursor → target shifts left).
        shift = (-right * (dx_px * world_per_px)) + (up * (dy_px * world_per_px))
        self._cam_target_x += float(shift[0])
        self._cam_target_y += float(shift[1])
        self._cam_target_z += float(shift[2])
        self._orbit_auto_screen_center_after_pan = True

    def _pan_target_ortho(self, dx_px: float, dy_px: float) -> None:
        """正交视图平移：沿视图固定 right/up 轴移动 cam_target。

        正交视图是轴对齐固定视角，right/up 轴取决于视图类型（与 _ortho_view_camera
        一致）。ortho_scale 匹配当前 Viewer 透视在 cam_dist 处的可见半高，故
        world_per_px 跟随默认/激活 Viewer 相机，而不是选中 Camera3D。
        拖拽方向取反（场景跟随光标）。
        """
        from src.features.composite.ui.viewport3d_state import ViewportViewKind
        view_kind = self._effective_view_kind()
        # 各正交视图的屏幕 right/up 轴（与 _ortho_view_camera 的 up 配置一致）
        # TOP（+Y 眼，up=-Z）：屏幕 right=+X，up=-Z
        # RIGHT（+X 眼，up=+Y）：屏幕 right=-Z，up=+Y
        # FRONT（+Z 眼，up=+Y）：屏幕 right=+X，up=+Y
        if view_kind == ViewportViewKind.TOP:
            right = np.array([1.0, 0.0, 0.0])
            up = np.array([0.0, 0.0, -1.0])
        elif view_kind == ViewportViewKind.RIGHT:
            right = np.array([0.0, 0.0, -1.0])
            up = np.array([0.0, 1.0, 0.0])
        else:  # FRONT
            right = np.array([1.0, 0.0, 0.0])
            up = np.array([0.0, 1.0, 0.0])
        # world_per_px: ortho 视野高度 = ortho_scale*2，对应 viewport 高度像素。
        # ortho_scale 匹配透视在 cam_dist 处的可见半高（与渲染一致）。
        from src.features.composite.render3d import math_utils as m3d
        cam_fov, _tw, _th = self._viewer3d_projection_params()
        ortho_scale = m3d.ortho_scale_for_perspective_match(self._cam_dist, cam_fov)
        vp_h_px = max(1, self.height())
        world_per_px = (ortho_scale * 2.0) / vp_h_px
        shift = (-right * (dx_px * world_per_px)) + (up * (dy_px * world_per_px))
        self._cam_target_x += float(shift[0])
        self._cam_target_y += float(shift[1])
        self._cam_target_z += float(shift[2])
        self._orbit_auto_screen_center_after_pan = True

    def _handle_3d_release(self, event: QMouseEvent) -> None:
        """End a 3D navigation drag and commit the final params (with undo)."""
        self._nav_is_active = False
        self._nav_mode = None
        self._orbit_pivot_world = None
        self._nav_preview_timer.stop()
        self.unsetCursor()
        self._emit_nav_preview()
        self._emit_nav_committed()
        # Phase 4: MMB click（位移<阈值）→ toggle 四视图布局。
        # controller.on_mmb_release 判定 click vs drag：click 时 toggle 并返回 True。
        # 仅 MMB 释放走此分支（左/右 Alt 导航的 release 不触发，因 controller
        # 只在 on_mmb_press 记录了 MMB 起点）。
        if event.button() == Qt.MouseButton.MiddleButton:
            if self._viewport3d_ctrl.on_mmb_release(event):
                # 切换了布局 → 触发重绘（四视图分区/标签由 overlay 绘制）。
                # M2: 同时通知 CompositePage 带 quad_view 重新提交渲染（Scene3D 据此
                # 渲染正交纹理）；无 quad 纹理时 _draw_quad_view 退化为只画透视象限。
                self.quad_view_changed.emit(self._viewport3d_ctrl.state.is_quad())
                self.update()

    def _apply_snap_quantize(self, result: dict) -> dict:
        """Phase 6: 对 gizmo 拖动结果应用吸附/量化。

        - 量化（quantize_enabled）：object_pos_* 吸附到 move_step 网格，
          object_rot_* 吸附到 rotate_step 度，mesh_scale 吸附到 scale_step 比例。
        - 网格吸附（snap_mode=GRID）：移动时吸附到 move_step（与量化同效果，
          但语义不同——snap 是吸附到世界网格，quantize 是步进锁定）。
        本阶段两者合并实现（都做 round 到 step），Phase 6+ 区分吸附目标。
        """
        snap = self._viewport3d_ctrl.state.snap
        if not snap.quantize_enabled and snap.snap_mode.value == "none":
            return result  # 无吸附/量化
        out = dict(result)
        step_move = snap.move_step if snap.move_step > 0 else None
        step_rot = snap.rotate_step if snap.rotate_step > 0 else None
        step_scale = snap.scale_step if snap.scale_step > 0 else None
        for k, v in out.items():
            if k.startswith("object_pos_") and step_move is not None:
                out[k] = round(v / step_move) * step_move
            elif k.startswith("object_rot_") and step_rot is not None:
                out[k] = round(v / step_rot) * step_rot
            elif k == "mesh_scale" and step_scale is not None:
                # 缩放量化：round 到 step 的幂（指数式更自然）
                import math
                if v > 0 and step_scale > 0:
                    exp = round(math.log(v) / math.log(1.0 + step_scale))
                    out[k] = (1.0 + step_scale) ** exp
        return out

    def _handle_gizmo_drag_move(self, event: QMouseEvent) -> None:
        """Move the object along the active gizmo axis (projection constraint).

        Projects the mouse onto the active axis ray; the new object position
        is emitted via the cam-nav preview path (reused for object moves) so
        the page writes the param and re-renders. object_pos is also kept
        locally so the gizmo anchor follows the cursor live.
        """
        if not self._gizmo_drag_active:
            return
        # M2: quad 模式用象限感知映射
        tex_ndc = self._gizmo_tex_ndc_for(QPointF(event.pos()))
        if tex_ndc is None:
            return
        result = self._transform_gizmo.drag_offset(tex_ndc)
        if not result:
            return
        # Phase 6: 应用吸附/量化（移动/旋转/缩放步进）。
        raw_result = self._apply_snap_quantize(result)
        if self._selected_node_type == "camera3d":
            result = self._camera_gizmo_result_to_params(raw_result)
        elif self._selected_node_type == "light3d":
            result = self._light_gizmo_result_to_params(raw_result)
        elif self._selected_node_type == "deformer3d":
            result = self._deformer_gizmo_result_to_params(raw_result)
        else:
            result = raw_result
        if not result:
            return
        # Phase 5: 记录最近一次拖动结果（release 时提交 undo）
        self._last_gizmo_drag_result = dict(result)
        # 更新本地 object_pos 锚点（按轴），使 gizmo 跟随鼠标。
        # 平面手柄会一次返回两个坐标，需逐项合并。
        gx, gy, gz = self._gizmo_obj_pos
        if "object_pos_x" in raw_result:
            gx = raw_result["object_pos_x"]
        if "object_pos_y" in raw_result:
            gy = raw_result["object_pos_y"]
        if "object_pos_z" in raw_result:
            gz = raw_result["object_pos_z"]
        self._gizmo_obj_pos = (gx, gy, gz)
        # 也同步进 selected_node_params（drag 期间 hit_test 依赖最新投影）
        if self._selected_node_params is not None:
            self._selected_node_params.update(result)
        from src.features.composite.render3d import math_utils as m3d
        self._transform_gizmo.object_pos = m3d.vec3(*self._gizmo_obj_pos)
        # 复用 nav preview 信号（page 写参数 + 重渲染）
        node_id = self._interaction_state.selected_node_id
        if node_id:
            self.scene3d_nav_preview.emit(node_id, result)
        self.update()

    def _handle_gizmo_drag_release(self, event: QMouseEvent) -> None:
        """End a gizmo drag and commit a single merged undo entry.

        Phase 5: 通用化提交——不再硬编码 object_pos_*，而是提交最近一次拖动结果
        （可能是 object_pos_x/y/z、object_rot_x/y/z、或 mesh_scale）。
        """
        if not self._gizmo_drag_active:
            return
        self._gizmo_drag_active = False
        axis = self._transform_gizmo.active_axis
        self._transform_gizmo.end_drag()
        self.unsetCursor()
        node_id = self._interaction_state.selected_node_id
        if node_id:
            # 提交最近一次拖动结果（pos/rot/scale 任一）。即使没有有效 delta，
            # 也发一个空 commit，让 page 端恢复 navigation LOD；否则四视图会
            # 卡在 solid/导航态，表现为正交视图长期没有材质。
            final = ({k: float(v) for k, v in self._last_gizmo_drag_result.items()}
                     if axis is not None and self._last_gizmo_drag_result else {})
            self.scene3d_nav_committed.emit(node_id, final)
        self._last_gizmo_drag_result = {}
        self._gizmo_drag_before = {}
        self._gizmo_drag_source_params = {}
        self._gizmo_drag_anchor_start = (0.0, 0.0, 0.0)
        self.update()

    def _emit_nav_preview(self) -> None:
        """Emit preview cam params (page writes them + triggers re-render)."""
        node_id = self._nav_target_node_id()
        if not node_id:
            return
        self.scene3d_nav_preview.emit(node_id, self._nav_params_dict())

    def _emit_nav_committed(self) -> None:
        """Emit final cam params (page pushes a merged undo entry)."""
        node_id = self._nav_target_node_id()
        if not node_id:
            return
        self.scene3d_nav_committed.emit(node_id, self._nav_params_dict())

    def _nav_target_node_id(self) -> str:
        """Return the 3D Viewer context that Alt-navigation should control.

        Object selection and viewer navigation are separate in 3D apps: selecting
        Camera3D should expose its icon/gizmo, while Alt+mouse still navigates
        the current 3D Viewer scene. The page renders that scene through a
        non-persistent virtual Scene3D snapshot, so navigation must never fall
        back to the selected Camera3D/Light3D object.
        """
        try:
            page = getattr(self, "_composite_page", None)
            active_camera_id = getattr(page, "_viewer3d_active_camera_id", None)
            if active_camera_id:
                graph = getattr(page, "_node_graph", None)
                node = graph.get_node(str(active_camera_id)) if graph is not None else None
                if node is not None and getattr(node, "node_type", None) == "camera3d":
                    return str(active_camera_id)
                # A stale active camera id makes default-viewer navigation emit
                # to a missing node, so Alt/MMB/wheel appear completely frozen.
                # Fall back to the virtual default camera and clear the stale id.
                try:
                    page._viewer3d_active_camera_id = None
                except Exception:
                    pass
            virtual_id = getattr(page, "_VIEWER3D_VIRTUAL_NODE_ID", "")
            if virtual_id:
                return str(virtual_id)
        except Exception:
            pass
        return ""

    def _nav_params_dict(self) -> dict:
        """Current 3D camera params as a dict (matches Scene3D param names)."""
        active_params = self._active_viewer_camera_params()
        if active_params is not None:
            target = (
                float(self._cam_target_x),
                float(self._cam_target_y),
                float(self._cam_target_z),
            )
            mode = str(active_params.get("camera_mode", "orbit"))
            if mode == "free":
                from src.features.composite.render3d import math_utils as m3d
                eye = m3d.orbit_to_eye(
                    max(0.1, float(self._cam_dist)),
                    float(self._orbit_yaw),
                    float(self._orbit_pitch),
                    target=m3d.vec3(*target))
                return {
                    "cam_eye_x": float(eye[0]),
                    "cam_eye_y": float(eye[1]),
                    "cam_eye_z": float(eye[2]),
                    "cam_target_x": target[0],
                    "cam_target_y": target[1],
                    "cam_target_z": target[2],
                }
            return {
                "cam_orbit_yaw": self._orbit_yaw,
                "cam_orbit_pitch": self._orbit_pitch,
                "cam_dist": self._cam_dist,
                "cam_target_x": target[0],
                "cam_target_y": target[1],
                "cam_target_z": target[2],
            }
        return {
            "cam_orbit_yaw": self._orbit_yaw,
            "cam_orbit_pitch": self._orbit_pitch,
            "cam_dist": self._cam_dist,
            "cam_target_x": self._cam_target_x,
            "cam_target_y": self._cam_target_y,
            "cam_target_z": self._cam_target_z,
        }

    @staticmethod
    def _nav_cursor_for_mode(mode: Optional[str]) -> Qt.CursorShape:
        if mode == "orbit":
            return Qt.CursorShape.ForbiddenCursor
        elif mode == "pan":
            return Qt.CursorShape.ClosedHandCursor
        elif mode == "zoom":
            return Qt.CursorShape.SizeVerCursor
        return Qt.CursorShape.ArrowCursor

    def _clear_drag_overlay_state(self) -> None:
        """Clear snap/constraint drag-overlay state when a drag ends.

        ``_snap_result`` drives the snap guide lines drawn in
        ``_draw_snap_lines``. If it is left set when a drag ends (mouse
        release or Esc cancel), the active guide line persists on screen
        until the next press — because the per-tool release handlers that
        cleared it (``_handle_transform_release`` etc.) are never invoked on
        the live release path. Clear it here, alongside the constraint
        scratch state, so every drag-end removes the overlay.
        """
        self._snap_result = None
        self._locked_axis = None
        self._initial_drag_delta = None
        self._constraint_config = None

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        """Handle double-click.

        Camera activation is intentionally not bound to viewport double-click.
        The only Camera view activation path is the Camera properties header
        toggle, so selecting or inspecting Camera icons cannot unexpectedly
        replace the global 3D Viewer camera.
        """
        super().mouseDoubleClickEvent(event)

    def _handle_3d_double_click(self, event: QMouseEvent) -> bool:
        """双击 Camera3D 图标 → 设为活动渲染相机。

        查找双击位置命中的图标，若是 camera3d 则找到其连接的 Scene3D
        （取当前查看的 scene3d），emit active_camera_requested 请求切换。
        返回 True 表示消费了事件。
        """
        try:
            view_pos = QPointF(event.pos())
            self._update_gizmo_projection()
            icon_hit = self._hit_test_node_icon(view_pos)
            if icon_hit is None or icon_hit.node_type != "camera3d":
                return False
            camera_id = icon_hit.node_id
            # 找当前查看的 Scene3D：优先 page 的 _viewer3d_node_id，否则遍历找
            # 连接了该 camera 的 scene3d。
            scene3d_id = self._find_viewing_scene3d_for_camera(camera_id)
            if scene3d_id is None:
                return False
            self.active_camera_requested.emit(scene3d_id, camera_id)
            return True
        except Exception:
            return False

    def _find_viewing_scene3d_for_camera(self, camera_id: str):
        """查找应作为活动相机切换目标的 Scene3D 节点 id。

        优先级：
          1. page._viewer3d_node_id（若它是 scene3d 且连接了该 camera）
          2. 遍历图中 scene3d，找 in 端口连接了该 camera_id 的第一个
        找不到返回 None。
        """
        try:
            page = getattr(self, '_composite_page', None)
            graph = getattr(page, '_node_graph', None) if page is not None else None
            if graph is None:
                return None
            # 1. 当前查看的 3D 节点
            viewer3d_id = getattr(page, '_viewer3d_node_id', None) if page is not None else None
            if viewer3d_id:
                node = graph.get_node(viewer3d_id)
                if node is not None and node.node_type == "scene3d":
                    if self._scene3d_connects_camera(graph, viewer3d_id, camera_id):
                        return viewer3d_id
            # 2. 遍历找连接了该 camera 的 scene3d
            for node in graph.nodes.values():
                if node.node_type == "scene3d":
                    if self._scene3d_connects_camera(graph, node.id, camera_id):
                        return node.id
            return None
        except Exception:
            return None

    @staticmethod
    def _scene3d_connects_camera(graph, scene3d_id: str, camera_id: str) -> bool:
        """检查 scene3d 的 in 端口是否连接了指定的 camera3d 节点。"""
        for conn in graph.connections:
            if (conn.dst_node_id == scene3d_id
                    and conn.dst_port_name == "in"
                    and conn.src_node_id == camera_id):
                up = graph.get_node(conn.src_node_id)
                if up is not None and up.node_type == "camera3d":
                    return True
        return False

    def mousePressEvent(self, event: QMouseEvent) -> None:
        """Handle mouse press, dispatching based on tool mode.

        - Middle mouse: Always triggers pan (regardless of tool mode)
        - Left mouse: Dispatched to current tool handler
          (AUD-9711: the unreachable "Space + Left: Pan" branch was removed —
          Space is reserved for transport play/pause.)
        """
        # Split-view divider drag: intercept left-click near the divider before
        # any 2D/3D tool dispatch so the user can scrub the split position.
        if (self._split_active
                and event.button() == Qt.MouseButton.LeftButton
                and self._hit_split_handle(event.pos())):
            self._split_dragging = True
            self.setCursor(Qt.CursorShape.SplitHCursor)
            event.accept()
            return

        # M2: 四视图模式——点击非活动象限切换活动视图（编辑焦点）。
        # 必须先于 3D 导航/gizmo pick：切换视图优先；点活动象限则放行（_handle_quad_view_click 返回 False）。
        if (self._view_mode == "3d"
                and self._handle_quad_view_click(event)):
            event.accept()
            return

        # Phase 2: 3D mode intercepts Alt+button for C4D-style camera nav.
        if self._view_mode == "3d" and self._handle_3d_press(event):
            event.accept()
            return

        # Phase 3: 3D mode plain left-click → gizmo axis pick / object pick.
        # Alt-nav above already returned; here only plain left-click reaches.
        if (self._view_mode == "3d"
                and event.button() == Qt.MouseButton.LeftButton
                and self._handle_3d_left_press(event)):
            event.accept()
            return

        # Phase 3: in 3D mode, a plain left-click that missed the gizmo must
        # NOT fall through to 2D tool dispatch (transform/crop handles etc.),
        # which would mis-fire on the scene3d node. Swallow it so empty-space
        # clicks do nothing (object-pick feedback is added in a later stage).
        # Middle-mouse pan still works below. (AUD-9711: the "space+left pan"
        # branch was unreachable — _space_pressed() was hard-wired to False —
        # and has been removed; Space stays reserved for play/pause.)
        if (self._view_mode == "3d"
                and event.button() == Qt.MouseButton.LeftButton):
            event.accept()
            return

        # Middle mouse always pans
        if event.button() == Qt.MouseButton.MiddleButton:
            # 阶段七：3D 模式下中键重定向为相机 pan（target 平移），不泄漏到 2D 画面平移。
            # 这样 3D 模式所有鼠标交互都只操作相机：滚轮 dolly / Alt+左 orbit /
            # 中键或 Alt+中 pan / Alt+右 dolly，画面平移缩放完全冻结。
            # Phase 4：MMB 通过 viewport3d_controller 记录起点（区分 click vs drag）。
            # click（位移<阈值）在 release 时 toggle 四视图；drag 平移相机。
            # Alt+MMB 永远 pan（controller 标记 drag，release 不 toggle）。
            if self._view_mode == "3d":
                self._viewport3d_ctrl.on_mmb_press(event)
                self._nav_mode = "pan"
                self._nav_is_active = True
                self._nav_last_pos = event.pos()
                self.setCursor(Qt.CursorShape.ClosedHandCursor)
                self.scene3d_nav_started.emit(self._nav_target_node_id())
                event.accept()
                return
            self._start_pan(event.pos())
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            event.accept()
            return

        # AUD-9711: the old "Space + Left also pans" branch is gone — it was
        # unreachable (dead `_space_pressed()` stub) and Space is bound to
        # transport play/pause. Left clicks fall through to tool dispatch.

        # Dispatch left mouse to tool handler
        if event.button() == Qt.MouseButton.LeftButton:
            view_pos = QPointF(event.pos())
            # Always-on overlay: if a handle is under the cursor, start a drag
            # regardless of tool mode. Transform also lets you grab the image
            # body; Constant's gradient/radial handles are routed through the
            # SELECT drag path (hit → _handle_select_press). This mirrors the
            # always-on draw gate in _draw_tool_handles so anything you can see
            # you can also grab.
            if (self._interaction_state.selected_node_id is not None
                    and not self._interaction_state.is_dragging):
                node_type = self._selected_node_type
                if node_type in ("transform", "text"):
                    hit = self.hit_test_handle_at_pos(view_pos)
                    if hit is not None or self._hit_test_transform_body(view_pos):
                        self._hover_handle = hit
                        if self._handle_transform_press(view_pos, event.modifiers()):
                            event.accept()
                            return
                elif self._constant_always_on_active():
                    hit = self.hit_test_handle_at_pos(view_pos)
                    if hit is not None:
                        self._hover_handle = hit
                        if self._handle_select_press(view_pos, event.modifiers()):
                            event.accept()
                            return
                elif self._selected_node_type in ("mask", "roto_shape",
                                                  "mesh_warp"):
                    # Mask/Roto/Warp handles are always-on overlays.  Route a
                    # hit directly to the matching editor even when the
                    # current viewport tool is PAN or SELECT, mirroring
                    # Transform and the Color-page Masks interaction model.
                    hit = self.hit_test_handle_at_pos(view_pos)
                    if hit is not None:
                        self._hover_handle = hit
                        handler = (
                            self._handle_mask_press
                            if self._selected_node_type == "mask"
                            else self._handle_roto_press
                        )
                        if handler(view_pos, event.modifiers()):
                            event.accept()
                            return
                elif self._selected_node_type == "cornerpin":
                    # CornerPin corner handles are node-owned overlays just
                    # like Transform: grab them in any tool mode.
                    hit = self.hit_test_handle_at_pos(view_pos)
                    if hit is not None:
                        self._hover_handle = hit
                        if self._handle_cornerpin_press(view_pos, event.modifiers()):
                            event.accept()
                            return
            if self._handle_tool_press(view_pos, event.modifiers()):
                event.accept()
                return

        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        """Handle mouse move, dispatching based on tool mode and drag state."""
        # Track mouse position for pixel inspector
        self._mouse_pos = event.pos()
        self._mouse_in_viewer = True

        # B2-3: the magnifier follows the cursor — repaint on every move.
        if self._magnifier_enabled:
            self.update()

        # B2-1: a button-held move (pan / tool-handle drag) counts as a view
        # interaction for the auto resolution mode. Bare cursor moves don't.
        if event.buttons() != Qt.MouseButton.NoButton:
            self._note_auto_proxy_interaction()

        # Split-view divider drag: update the split position from cursor x.
        if self._split_dragging:
            if (self._tex_width and self._tex_height):
                tl = self.get_view_pos_at_image_pos(QPointF(0.0, 0.0))
                br = self.get_view_pos_at_image_pos(
                    QPointF(float(self._tex_width), float(self._tex_height)))
                if tl is not None and br is not None:
                    span = br.x() - tl.x()
                    if span > 0:
                        rel = (event.pos().x() - tl.x()) / span
                        self._split_pos = max(0.02, min(0.98, rel))
                        self.update()
            event.accept()
            return

        # Phase 2: 3D mode handles its own navigation drags.
        if self._nav_is_active:
            self._handle_3d_move(event)
            event.accept()
            return

        # Phase 3: 3D gizmo axis drag (object move).
        if self._gizmo_drag_active:
            self._handle_gizmo_drag_move(event)
            event.accept()
            return

        # P5: Camera3D/Light3D 图标拖动（移动相机/灯光位置）。
        if self._icon_drag_active:
            self._handle_icon_drag_move(event)
            event.accept()
            return

        # Phase 3: gizmo hover highlight (3D mode, not dragging).
        if self._gizmo_visible and not self._interaction_state.is_dragging:
            self._update_gizmo_projection()
            # M2: quad 模式用象限感知映射
            tex_px = self._gizmo_tex_px_for(QPointF(event.pos()))
            new_hover = (self._transform_gizmo.hit_test(tex_px)
                         if tex_px is not None else None)
            if new_hover != self._transform_gizmo.hovered_axis:
                self._transform_gizmo.hovered_axis = new_hover
                self.update()

        # Handle active pan (from middle mouse or space+left)
        if self._is_panning:
            delta = event.pos() - self._pan_start_pos
            dpr = self.devicePixelRatio()
            vp_w = self.width() * dpr
            vp_h = self.height() * dpr
            if vp_w > 0 and vp_h > 0:
                # Convert pixel delta to NDC delta
                ndc_dx = (delta.x() * dpr) / (vp_w / 2.0)
                ndc_dy = -(delta.y() * dpr) / (vp_h / 2.0)  # flip Y
                self._pan_x = self._pan_start_ndc_x + ndc_dx
                self._pan_y = self._pan_start_ndc_y + ndc_dy
                # Handles are stored in view coords — recompute so the
                # always-on overlay tracks the image during pan/zoom.
                self._update_tool_handles()
                self.update()
            event.accept()
            return

        # Handle tool drag or hover
        view_pos = QPointF(event.pos())
        if self._interaction_state.is_dragging:
            # F6-04: LOD performance during drag
            # Start LOD timer if not already active
            if not self._drag_lod_timer.isActive():
                self._drag_lod_timer.start()

            # F6-04: Throttle updates during drag (max 60fps)
            if self._drag_throttle_timer.isActive():
                # Queue pending update, skip this one
                self._pending_drag_update = True
                self._pending_drag_view_pos = view_pos
                self._pending_drag_modifiers = event.modifiers()
            else:
                # Process immediately and start throttle timer
                self._handle_tool_move(view_pos, event.modifiers())
                self._drag_throttle_timer.start()

            event.accept()
            return
        else:
            # Update hover handle (for cursor and visual feedback)
            old_hover_handle = self._hover_handle
            self._hover_handle = self._update_hover_handle(view_pos)

            # Update hover node region (for SELECT tool)
            old_hover_node = self._hovered_node_region
            if self._tool_mode == ViewerToolMode.SELECT:
                self._hovered_node_region = self._update_hover_node(view_pos)

            # Emit signals and update cursor if hover changed
            if self._hover_handle != old_hover_handle:
                self._update_cursor_for_tool(self._tool_mode)
                self.handle_hit_changed.emit(self._hover_handle)

            if self._hovered_node_region != old_hover_node:
                self.node_hovered.emit(self._hovered_node_region)

            if self._hover_handle != old_hover_handle or self._hovered_node_region != old_hover_node:
                self.update()

        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        """Handle mouse release, ending pan or tool drag."""
        # End an active split-divider drag.
        if self._split_dragging:
            self._split_dragging = False
            self.unsetCursor()
            event.accept()
            return

        # Phase 2: end an active 3D navigation drag.
        if self._nav_is_active:
            self._handle_3d_release(event)
            event.accept()
            return

        # Phase 3: end an active gizmo axis drag (object move).
        if self._gizmo_drag_active and event.button() == Qt.MouseButton.LeftButton:
            self._handle_gizmo_drag_release(event)
            event.accept()
            return

        # P5: 结束 Camera3D/Light3D 图标拖动。
        if self._icon_drag_active and event.button() == Qt.MouseButton.LeftButton:
            self._handle_icon_drag_release(event)
            event.accept()
            return

        # End pan (from middle mouse or space+left)
        if self._is_panning:
            if event.button() in (Qt.MouseButton.MiddleButton, Qt.MouseButton.LeftButton):
                self._is_panning = False
                self._update_cursor_for_tool(self._tool_mode)
                event.accept()
                return

        # End tool drag
        if event.button() == Qt.MouseButton.LeftButton:
            if self._interaction_state.is_dragging:
                # F6-04: Disable LOD mode and stop timers
                self._disable_drag_lod()

                # AUD-9700/9701: PAINT and ROI commit through their release
                # handlers (stroke commit / rectangle apply). The other tools
                # intentionally stay on the generic parameter path below —
                # their per-tool release handlers are legacy and never
                # invoked on the live release path (see the note in
                # _clear_drag_overlay_state).
                release_tool = self._interaction_state.tool
                if release_tool in (ViewerToolMode.PAINT, ViewerToolMode.ROI):
                    self._handle_tool_release(QPointF(event.pos()))

                # Get original params before reset
                original_params = self._interaction_state.original_params.copy()
                selected_node_id = self._interaction_state.selected_node_id
                # Emit interaction_ended signal BEFORE reset
                # so that CompositePage can access final_params from node
                self.interaction_ended.emit(self._interaction_state, original_params)
                # Reset drag state but keep the selected node so handles remain
                # visible after the transform commit.
                self._interaction_state.reset()
                self._interaction_state.selected_node_id = selected_node_id
                self._clear_drag_overlay_state()
                self._update_tool_handles()
                self._update_cursor_for_tool(self._tool_mode)
                self.update()
                event.accept()
                return

        super().mouseReleaseEvent(event)

    def event(self, event):
        """Intercept Tab before Qt's focus-chain tab navigation consumes it.

        Tab is Qt's default focus-traversal key: QWidget::event() routes it to
        focusNextPrevChild() and never delivers it to keyPressEvent. So we
        handle Tab/Backtab here at the event() level to toggle 2D/3D mode
        (Nuke-style single-viewport). All other events fall through unchanged.
        """
        if event.type() == QEvent.Type.KeyPress:
            if event.key() in (Qt.Key.Key_Tab, Qt.Key.Key_Backtab):
                new_mode = "3d" if self._view_mode == "2d" else "2d"
                self.set_view_mode(new_mode)
                event.accept()
                return True
        return super().event(event)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        """Handle keyboard shortcuts for tool mode switching."""
        # I/O playback-range shortcuts: only active when the video control bar
        # is visible (Composite video mode). Forwards to the bar's I/O handlers.
        if (getattr(self, 'video_control_bar', None) is not None
                and self.video_control_bar.isVisible()):
            if event.key() == Qt.Key.Key_I:
                self.video_control_bar.mark_in_requested.emit()
                event.accept()
                return
            if event.key() == Qt.Key.Key_O:
                self.video_control_bar.mark_out_requested.emit()
                event.accept()
                return

        # P7：F = frame selected（有选中对象）或 reset（无选中）。
        if event.key() == Qt.Key.Key_F and self._view_mode == "3d":
            if not self._frame_selected_3d():
                self._reset_3d_camera()
            event.accept()
            return

        # P7：Shift+I = toggle isolate selected（只渲染选中对象）。
        if (event.key() == Qt.Key.Key_I and self._view_mode == "3d"
                and event.modifiers() & Qt.KeyboardModifier.ShiftModifier):
            self._toggle_isolate_selected()
            event.accept()
            return

        # 3D mode: Q/W/E/R switch Select/Move/Rotate/Scale through the
        # dedicated 3D controller so toolbar state and gizmo mode stay in sync.
        if self._view_mode == "3d" and self._viewport3d_ctrl.handle_key(event):
            event.accept()
            return

        key_text = event.text().upper()
        # B1: ROI shortcuts must be checked before the bare tool matching
        # below, which ignores modifiers (Shift+R would otherwise enter
        # ROTO). Shift+R arms the ROI drag tool; Ctrl+Shift+R clears the
        # active ROI and returns to full frame.
        # Match on Key_R — event.text() is unreliable under Ctrl on some
        # platforms (control character / empty text).
        if event.key() == Qt.Key.Key_R and self._view_mode != "3d":
            mods = event.modifiers()
            if mods & Qt.KeyboardModifier.ControlModifier \
                    and mods & Qt.KeyboardModifier.ShiftModifier:
                self.clear_user_roi()
                event.accept()
                return
            if mods & Qt.KeyboardModifier.ShiftModifier:
                self.set_tool_mode(ViewerToolMode.ROI)
                event.accept()
                return
        # B2-2: Ctrl+Shift+A/B freeze the current frame into the A/B compare
        # buffers (checked before bare tool matching, 2D mode only).
        if (event.key() in (Qt.Key.Key_A, Qt.Key.Key_B)
                and self._view_mode != "3d"):
            mods = event.modifiers()
            if (mods & Qt.KeyboardModifier.ControlModifier
                    and mods & Qt.KeyboardModifier.ShiftModifier):
                slot = "B" if event.key() == Qt.Key.Key_B else "A"
                self.store_ab_snapshot(slot)
                event.accept()
                return
        # Check if key matches a tool shortcut
        if not event.modifiers():
            # B2-3: Z toggles the magnifier loupe (2D mode only).
            if key_text == "Z" and self._view_mode != "3d":
                self.toggle_magnifier()
                event.accept()
                return
            # C2-3: [ / ] resize the paint brush while in PAINT mode on a
            # selected Paint node (PS convention). Not undoable — the brush
            # size is a tool preference, not a content edit.
            if self._tool_mode == ViewerToolMode.PAINT \
                    and key_text in ("[", "]") \
                    and self._selected_node_type == "paint" \
                    and self.get_selected_node():
                factor = 1.0 / 1.15 if key_text == "[" else 1.15
                self._paint_brush_size_px = max(
                    1.0, min(1024.0, self._paint_brush_size_px * factor))
                self.paint_brush_size_changed.emit(
                    str(self.get_selected_node()), float(self._paint_brush_size_px))
                self.update()
                event.accept()
                return
            for mode, shortcut in TOOL_MODE_SHORTCUTS.items():
                if key_text == shortcut:
                    self.set_tool_mode(mode)
                    event.accept()
                    return

        # Escape: Cancel current drag or reset to PAN
        if event.key() == Qt.Key.Key_Escape:
            if self._interaction_state.is_dragging:
                # Cancel drag without committing
                self._interaction_state.reset()
                self._clear_drag_overlay_state()
                # an in-flight ROI rubber band must not survive the cancel
                self._user_roi_drag_rect = None
                # AUD-9700: nor an in-progress paint stroke polyline
                self._paint_drag_points = None
                self._paint_drag_node_id = None
                self._update_cursor_for_tool(self._tool_mode)
                self.update()
                event.accept()
                return
            else:
                self.set_tool_mode(ViewerToolMode.PAN)
                event.accept()
                return

        super().keyPressEvent(event)

    # ── Tool Event Handlers ──────────────────────────────────────────────────

    def _handle_tool_press(self, view_pos: QPointF, modifiers: int) -> bool:
        """Dispatch tool press event based on current tool mode.

        Returns True if the event was handled.
        """
        mode = self._tool_mode

        if mode == ViewerToolMode.PAN:
            # Pan mode left-click does nothing (use middle mouse or space)
            return False

        elif mode == ViewerToolMode.SELECT:
            return self._handle_select_press(view_pos, modifiers)

        elif mode == ViewerToolMode.TRANSFORM:
            return self._handle_transform_press(view_pos, modifiers)

        elif mode == ViewerToolMode.CROP:
            return self._handle_crop_press(view_pos, modifiers)

        elif mode == ViewerToolMode.MASK:
            return self._handle_mask_press(view_pos, modifiers)

        elif mode == ViewerToolMode.ROTO:
            return self._handle_roto_press(view_pos, modifiers)

        elif mode == ViewerToolMode.CORNERPIN:
            return self._handle_cornerpin_press(view_pos, modifiers)

        elif mode == ViewerToolMode.ROI:
            return self._handle_roi_press(view_pos)

        elif mode == ViewerToolMode.PAINT:
            return self._handle_paint_press(view_pos, modifiers)

        elif mode == ViewerToolMode.COLOR_PICKER:
            return self._handle_color_picker_press()

        return False

    def _handle_color_picker_press(self) -> bool:
        """CP-056: sample the clicked pixel's linear color.

        Reads the owned display texture (HDR float path first, 8-bit
        framebuffer fallback), emits ``color_picked`` with linear RGBA and
        mirrors a readable form onto the clipboard. The page forwards the
        signal to the properties panel (last-touched color parameter).
        """
        tw, th = self._tex_width, self._tex_height
        vp_w, vp_h = self.width(), self.height()
        uv = self._compute_uv_at_mouse(vp_w, vp_h, tw, th)
        if uv is None:
            return True

        r = g = b = a = None
        if self._owned_display_tex_id and self._owned_display_tex_id > 0:
            r, g, b, a = self._read_linear_pixel_from_texture(
                uv[0], uv[1], tw, th)
        if r is None:
            # 8-bit fallback: sample the presenter's framebuffer.
            dpr = self.devicePixelRatio()
            img = self.grabFramebuffer() if hasattr(self, "grabFramebuffer") else None
            if img is not None:
                px = img.pixel(
                    int(uv[0] * self.width() * dpr),
                    int((1.0 - uv[1]) * self.height() * dpr))
                from PyQt6.QtGui import QColor
                c = QColor(px)
                # Approximate sRGB→linear for the fallback path.
                def _s2l(v8):
                    v = v8 / 255.0
                    return (v / 12.92 if v <= 0.04045
                            else ((v + 0.055) / 1.055) ** 2.4)
                r, g, b, a = _s2l(c.red()), _s2l(c.green()), _s2l(c.blue()), c.alphaF()
        if r is None:
            return True

        self.color_picked.emit(float(r), float(g), float(b), float(a or 1.0))
        try:
            from PyQt6.QtWidgets import QApplication
            QApplication.clipboard().setText(
                f"{r:.5f}, {g:.5f}, {b:.5f}, {a:.5f}  (linear RGBA)")
        except Exception:
            pass
        return True

    def _handle_tool_move(self, view_pos: QPointF, modifiers: int) -> None:
        """Dispatch tool move event during drag."""
        mode = self._interaction_state.tool

        if mode == ViewerToolMode.SELECT:
            self._handle_select_move(view_pos, modifiers)

        elif mode == ViewerToolMode.TRANSFORM:
            self._handle_transform_move(view_pos, modifiers)

        elif mode == ViewerToolMode.CROP:
            self._handle_crop_move(view_pos, modifiers)

        elif mode == ViewerToolMode.MASK:
            self._handle_mask_move(view_pos, modifiers)

        elif mode == ViewerToolMode.ROTO:
            self._handle_roto_move(view_pos, modifiers)

        elif mode == ViewerToolMode.CORNERPIN:
            self._handle_cornerpin_move(view_pos, modifiers)

        elif mode == ViewerToolMode.ROI:
            self._handle_roi_move(view_pos)

        elif mode == ViewerToolMode.PAINT:
            self._handle_paint_move(view_pos)

    def _handle_tool_release(self, view_pos: QPointF) -> dict:
        """Dispatch tool release event and return original params for undo."""
        mode = self._interaction_state.tool

        if mode == ViewerToolMode.SELECT:
            return self._handle_select_release(view_pos)

        elif mode == ViewerToolMode.TRANSFORM:
            return self._handle_transform_release(view_pos)

        elif mode == ViewerToolMode.CROP:
            return self._handle_crop_release(view_pos)

        elif mode == ViewerToolMode.MASK:
            return self._handle_mask_release(view_pos)

        elif mode == ViewerToolMode.ROTO:
            return self._handle_roto_release(view_pos)

        elif mode == ViewerToolMode.CORNERPIN:
            return self._handle_cornerpin_release(view_pos)

        elif mode == ViewerToolMode.ROI:
            return self._handle_roi_release(view_pos)

        elif mode == ViewerToolMode.PAINT:
            return self._handle_paint_release(view_pos)

        return {}

    # ── F6-04: LOD Performance Helper Methods ────────────────────────────────

    def _enable_drag_lod(self) -> None:
        """Enable LOD mode during long drag interactions.

        F6-04: When drag continues for more than 100ms, switch to
        quarter-resolution (LOD level 2) rendering to maintain
        smooth interaction. The viewer signals to CompositePage
        to use lower quality during drag.
        """
        if not self._drag_lod_mode:
            self._drag_lod_mode = True
            logger.debug("[F6-04] Enabling LOD mode for drag (level=2)")
            # Signal to CompositePage to use LOD rendering
            # This is done via interaction_updated signal with LOD hint

    def _disable_drag_lod(self) -> None:
        """Disable LOD mode after drag ends and request full-resolution render.

        F6-04: Stop all LOD timers and request a full-resolution
        render to get crisp final result after interaction ends.
        """
        pending_update = self._pending_drag_update
        pending_view_pos = self._pending_drag_view_pos
        pending_modifiers = self._pending_drag_modifiers

        # Stop timers
        self._drag_lod_timer.stop()
        self._drag_throttle_timer.stop()

        if self._drag_lod_mode:
            self._drag_lod_mode = False
            self._full_render_pending = True
            logger.debug("[F6-04] Disabling LOD mode, requesting full render")

        # Process any pending drag update before disabling
        if pending_update and pending_view_pos is not None and self._interaction_state.is_dragging:
            self._handle_tool_move(pending_view_pos, pending_modifiers)

        self._pending_drag_update = False
        self._pending_drag_view_pos = None
        self._pending_drag_modifiers = Qt.KeyboardModifier.NoModifier

    def _process_pending_drag_update(self) -> None:
        """Process the latest queued drag position at the throttle interval."""
        if not self._pending_drag_update or not self._interaction_state.is_dragging:
            self._pending_drag_update = False
            self._pending_drag_view_pos = None
            return

        view_pos = self._pending_drag_view_pos
        modifiers = self._pending_drag_modifiers
        self._pending_drag_update = False
        self._pending_drag_view_pos = None

        if view_pos is not None:
            self._handle_tool_move(view_pos, modifiers)

        if self._pending_drag_update:
            self._drag_throttle_timer.start()

    def get_lod_level(self) -> int:
        """Get current LOD level for rendering.

        Returns:
            LOD level: 0=full resolution, 2=quarter resolution.
        """
        return self._drag_lod_level if self._drag_lod_mode else 0

    def is_drag_active(self) -> bool:
        """Check if a drag interaction is currently active.

        Used by CompositePage to determine if LOD rendering should be used.
        """
        return self._interaction_state.is_dragging

    # ── Tool Implementation Stubs (S5-08) ─────────────────────────────────────
    # These are stub implementations. Full implementations will be added
    # in subsequent tasks (S5-09 to S5-12) and will coordinate with
    # CompositePage/NodeGraph for actual node parameter modifications.

    def _handle_select_press(self, view_pos: QPointF, modifiers: int) -> bool:
        """Handle SELECT tool press - click to select node output region.

        Implements two behaviors:
        1. Click on a node output region -> select that node
        2. Click on empty area -> start rubber band selection

        Modifier keys:
        - Shift: Add to existing selection (multi-select)
        - Ctrl: Toggle selection state
        """
        # First check if we hit a handle (if tool handles are defined)
        hit_handle = self.hit_test_handle_at_pos(view_pos)
        if hit_handle is not None:
            # Handle hit - start handle drag
            image_pos = self.get_image_pos_at_view_pos(view_pos, check_bounds=False)
            if image_pos is None:
                return False

            ndc_pos = view_pos_to_ndc(view_pos, self.width(), self.height(),
                                      self.devicePixelRatio())

            # Check if handle is already selected

            # Handle modifier keys
            if modifiers & Qt.KeyboardModifier.ShiftModifier:
                # Add handle to selection
                self._selection_state.add_to_selection(handle_id=hit_handle)
            elif modifiers & Qt.KeyboardModifier.ControlModifier:
                # Toggle handle selection
                self._selection_state.toggle_selection(handle_id=hit_handle)
            else:
                # Single select - clear others and select this handle
                self._selection_state.clear_selection()
                self._selection_state.add_to_selection(handle_id=hit_handle)

            self._hover_handle = hit_handle
            self._interaction_state.start_drag(
                tool=ViewerToolMode.SELECT,
                node_id=self._interaction_state.selected_node_id,
                handle=hit_handle,
                view_pos=view_pos,
                ndc_pos=ndc_pos,
                image_pos=image_pos,
                format_pos=QPointF(),
                original_params={},
                modifiers=modifiers,
            )
            self.selection_changed.emit(self._selection_state)
            self.interaction_started.emit(self._interaction_state)
            return True

        # Check if we hit a node output region
        hit_region = self.hit_test_node_at_pos(view_pos)
        if hit_region is not None:
            # Node hit - select the node
            is_multi_select = bool(modifiers & Qt.KeyboardModifier.ShiftModifier)
            is_toggle = bool(modifiers & Qt.KeyboardModifier.ControlModifier)

            if is_toggle:
                # Toggle selection
                self._selection_state.toggle_selection(node_id=hit_region.node_id)
            elif is_multi_select:
                # Add to selection
                self._selection_state.add_to_selection(node_id=hit_region.node_id)
            else:
                # Single select - replace selection
                self._selection_state.clear_selection()
                self._selection_state.add_to_selection(node_id=hit_region.node_id)

            self._interaction_state.selected_node_id = hit_region.node_id
            self._update_tool_handles()

            image_pos = self.get_image_pos_at_view_pos(view_pos, check_bounds=False)
            ndc_pos = view_pos_to_ndc(view_pos, self.width(), self.height(),
                                      self.devicePixelRatio())

            self._interaction_state.start_drag(
                tool=ViewerToolMode.SELECT,
                node_id=hit_region.node_id,
                handle=None,
                view_pos=view_pos,
                ndc_pos=ndc_pos,
                image_pos=image_pos or QPointF(),
                format_pos=QPointF(),
                original_params={},
                modifiers=modifiers,
            )

            self.selection_changed.emit(self._selection_state)
            self.node_selected.emit(hit_region.node_id, is_multi_select)
            self.interaction_started.emit(self._interaction_state)
            self.update()
            return True

        # No hit - start rubber band selection
        self._start_rubber_band(view_pos)
        self._interaction_state.start_drag(
            tool=ViewerToolMode.SELECT,
            node_id=None,
            handle=None,
            view_pos=view_pos,
            ndc_pos=view_pos_to_ndc(view_pos, self.width(), self.height(),
                                    self.devicePixelRatio()),
            image_pos=self.get_image_pos_at_view_pos(view_pos, check_bounds=False) or QPointF(),
            format_pos=QPointF(),
            original_params={},
            modifiers=modifiers,
        )
        self.interaction_started.emit(self._interaction_state)
        return True

    def _handle_select_move(self, view_pos: QPointF, modifiers: int) -> None:
        """Handle SELECT tool move during drag.

        Supports:
        - Rubber band selection (when rubber band is active)
        - Handle drag preview (when a handle is active)
        """
        image_pos = self.get_image_pos_at_view_pos(view_pos, check_bounds=False)

        if self._selection_state.rubber_band_active:
            # Update rubber band rectangle
            self._update_rubber_band(view_pos)
        elif self._interaction_state.active_handle is not None:
            # Handle drag preview
            if image_pos is not None:
                self._interaction_state.update_drag(view_pos, image_pos=image_pos)
                self.interaction_updated.emit(self._interaction_state)
        else:
            # Node drag preview (if node is selected)
            if image_pos is not None:
                self._interaction_state.update_drag(view_pos, image_pos=image_pos)
                self.interaction_updated.emit(self._interaction_state)

    def _handle_select_release(self, view_pos: QPointF) -> dict:
        """Handle SELECT tool release.

        Handles:
        - Rubber band selection end: select all nodes/handles in the rubber band
        - Handle drag end: commit changes
        - Node selection: emit selection signals
        """
        original_params = self._interaction_state.end_drag()

        if self._selection_state.rubber_band_active:
            # End rubber band and select nodes/handles in the region
            rect = self._end_rubber_band()

            # Find handles in rubber band region
            selected_handles = hit_test_handles_in_rect(
                rect, self._tool_handles, self._hit_radius_pixels
            )

            # Find nodes in rubber band region (check if their bbox intersects)
            selected_nodes: list[str] = []
            rect_img = self._rubber_band_rect_to_image_rect(rect)

            if rect_img is not None:
                for region in self._node_output_regions:
                    x, y, w, h = region.bbox
                    # Check intersection
                    if (x + w > rect_img[0] and x < rect_img[2] and
                        y + h > rect_img[1] and y < rect_img[3]):
                        selected_nodes.append(region.node_id)

            # Apply selection based on modifiers
            if self._interaction_state.modifier_keys & Qt.KeyboardModifier.ShiftModifier:
                # Add to selection
                for node_id in selected_nodes:
                    self._selection_state.add_to_selection(node_id=node_id)
                for handle_id in selected_handles:
                    self._selection_state.add_to_selection(handle_id=handle_id)
            elif self._interaction_state.modifier_keys & Qt.KeyboardModifier.ControlModifier:
                # Toggle selection
                for node_id in selected_nodes:
                    self._selection_state.toggle_selection(node_id=node_id)
                for handle_id in selected_handles:
                    self._selection_state.toggle_selection(handle_id=handle_id)
            else:
                # Replace selection
                self._selection_state.clear_selection()
                for node_id in selected_nodes:
                    self._selection_state.add_to_selection(node_id=node_id)
                for handle_id in selected_handles:
                    self._selection_state.add_to_selection(handle_id=handle_id)

            # Emit signals
            self.nodes_selected.emit(selected_nodes)
            self.handles_selected.emit(selected_handles)
            self.selection_changed.emit(self._selection_state)

        self.interaction_ended.emit(self._interaction_state, original_params)
        self.update()
        return original_params

    def _rubber_band_rect_to_image_rect(
        self, rect_view: tuple[float, float, float, float]
    ) -> Optional[tuple[float, float, float, float]]:
        """Convert rubber band view rect to image pixel rect.

        Args:
            rect_view: Rubber band rect in view pixels (x0, y0, x1, y1).

        Returns:
            Image rect in pixels (x0, y0, x1, y1), or None if outside image.
        """
        x0_v, y0_v, x1_v, y1_v = rect_view

        # Normalize rect (ensure x0 <= x1, y0 <= y1)
        if x0_v > x1_v:
            x0_v, x1_v = x1_v, x0_v
        if y0_v > y1_v:
            y0_v, y1_v = y1_v, y0_v

        # Convert corners to image positions
        top_left_view = QPointF(x0_v, y0_v)
        bottom_right_view = QPointF(x1_v, y1_v)

        top_left_img = self.get_image_pos_at_view_pos(top_left_view, check_bounds=False)
        bottom_right_img = self.get_image_pos_at_view_pos(bottom_right_view, check_bounds=False)

        if top_left_img is None or bottom_right_img is None:
            return None

        return (
            top_left_img.x(),
            top_left_img.y(),
            bottom_right_img.x(),
            bottom_right_img.y(),
        )

    def _handle_transform_press(self, view_pos: QPointF, modifiers: int) -> bool:
        """Handle TRANSFORM tool press: handles edit, image body moves."""
        handle = self.hit_test_handle_at_pos(view_pos)
        if handle is not None:
            self._hover_handle = handle
        elif self._hit_test_transform_body(view_pos):
            self._hover_handle = None
            handle = "move"
        else:
            return False

        image_pos = self.get_image_pos_at_view_pos(view_pos, check_bounds=False)
        if image_pos is None:
            return False

        ndc_pos = view_pos_to_ndc(view_pos, self.width(), self.height(),
                                  self.devicePixelRatio())

        # Store original node params for undo/preview
        original_params = self._selected_node_params.copy()

        # Initialize constraint state for this drag (S5-14)
        self._initial_drag_delta = None
        self._locked_axis = None
        self._snap_result = None

        # Classify operation by handle id prefix (ids: move / pivot / rotate /
        # scale_corner_* / scale_x_* / scale_y_*). The old split("_")[0] scheme
        # misclassified scale handles ("scale" ∉ the tuple) so scaling did nothing.
        is_scale = handle.startswith("scale_")
        is_rotate = handle == "rotate"
        self._constraint_config = get_constraint_config_from_modifiers(modifiers, is_scale, is_rotate)

        self._interaction_state.start_drag(
            tool=ViewerToolMode.TRANSFORM,
            node_id=self._interaction_state.selected_node_id,
            handle=handle,
            view_pos=view_pos,
            ndc_pos=ndc_pos,
            image_pos=image_pos,
            format_pos=QPointF(),
            original_params=original_params,
            modifiers=modifiers,
        )
        self.interaction_started.emit(self._interaction_state)
        return True

    def _handle_transform_move(self, view_pos: QPointF, modifiers: int) -> None:
        """Handle TRANSFORM tool move - update preview with constraints and snapping.

        Applies:
        - Scale handles: aspect ratio locked by default; Shift frees x/y
        - Rotate: Shift enables angle snapping
        - Ctrl: axis lock for translate handles
        - Snapping: to format edges/center
        """
        image_pos = self.get_image_pos_at_view_pos(view_pos, check_bounds=False)
        if image_pos is None:
            return

        self._interaction_state.update_drag(view_pos, image_pos=image_pos)
        self._interaction_state.modifier_keys = modifiers

        # Get raw delta
        delta = self._interaction_state.get_image_delta()

        # Get handle type (prefix-based classification; see _handle_transform_press)
        handle = self._interaction_state.active_handle
        if handle is None:
            return
        is_scale = handle.startswith("scale_")
        is_rotate = handle == "rotate"
        is_translate = handle in ("center", "move")
        is_pivot = handle == "pivot"

        # ── Apply Constraints (S5-14) ──
        ctrl_pressed = bool(modifiers & Qt.KeyboardModifier.ControlModifier)

        if ctrl_pressed and (is_translate or is_pivot):
            # Axis lock for translate / pivot (both are 2D point drags)
            if self._initial_drag_delta is None:
                self._initial_drag_delta = delta
            self._locked_axis = determine_locked_axis(self._initial_drag_delta)
            delta = apply_axis_constraint(delta, self._locked_axis)

        # Update constraint config
        self._constraint_config = get_constraint_config_from_modifiers(modifiers, is_scale, is_rotate)

        # ── Apply Snapping (S5-14) ──
        if self._tex_width > 0 and self._tex_height > 0:
            # Compute position to snap (depends on handle type)
            if is_translate:
                # Snap the translate position
                orig_params = self._interaction_state.original_params
                orig_tx = orig_params.get("translate_x", 0.0)
                orig_ty = orig_params.get("translate_y", 0.0)

                # Current position after delta
                current_pos = QPointF(
                    orig_tx + delta.x(),
                    orig_ty + delta.y()
                )

                self._snap_result = apply_snapping(
                    current_pos,
                    self._snap_config,
                    self._tex_width,
                    self._tex_height,
                )

                # Adjust delta for snap
                if self._snap_result.snapped_x:
                    snap_delta_x = self._snap_result.snapped_position.x() - current_pos.x()
                    delta = QPointF(delta.x() + snap_delta_x, delta.y())
                if self._snap_result.snapped_y:
                    snap_delta_y = self._snap_result.snapped_position.y() - current_pos.y()
                    delta = QPointF(delta.x(), delta.y() + snap_delta_y)

            elif is_pivot:
                # Snap pivot position
                orig_params = self._interaction_state.original_params
                orig_px = orig_params.get("pivot_x", self._tex_width / 2.0)
                orig_py = orig_params.get("pivot_y", self._tex_height / 2.0)

                current_pos = QPointF(
                    orig_px + delta.x(),
                    orig_py + delta.y()
                )

                self._snap_result = apply_snapping(
                    current_pos,
                    self._snap_config,
                    self._tex_width,
                    self._tex_height,
                )

                # Adjust delta for snap
                if self._snap_result.snapped_x:
                    snap_delta_x = self._snap_result.snapped_position.x() - current_pos.x()
                    delta = QPointF(delta.x() + snap_delta_x, delta.y())
                if self._snap_result.snapped_y:
                    snap_delta_y = self._snap_result.snapped_position.y() - current_pos.y()
                    delta = QPointF(delta.x(), delta.y() + snap_delta_y)

        # Store constrained delta in preview params for external handler
        self._interaction_state.preview_params = {
            "delta_x": delta.x(),
            "delta_y": delta.y(),
            "handle": handle,
            "is_scale": is_scale,
            "is_rotate": is_rotate,
            "lock_aspect_ratio": self._constraint_config.lock_aspect_ratio if self._constraint_config else False,
            "angle_snap_enabled": self._constraint_config.angle_snap_enabled if self._constraint_config else False,
        }

        self.interaction_updated.emit(self._interaction_state)
        self._update_tool_handles()
        self.update()  # Trigger repaint to show snap lines

    def _handle_transform_release(self, view_pos: QPointF) -> dict:
        """Handle TRANSFORM tool release - commit changes and clear state."""
        original_params = self._interaction_state.end_drag()

        # Clear snap and constraint state (S5-14)
        self._snap_result = None
        self._locked_axis = None
        self._initial_drag_delta = None
        self._constraint_config = None

        self.interaction_ended.emit(self._interaction_state, original_params)
        self.update()  # Clear snap lines
        return original_params

    def _handle_crop_press(self, view_pos: QPointF, modifiers: int) -> bool:
        """Handle CROP tool press - start drag on handle.

        Initializes snap and constraint state.
        """
        handle = self._hover_handle
        if handle is None:
            return False

        image_pos = self.get_image_pos_at_view_pos(view_pos, check_bounds=False)
        if image_pos is None:
            return False

        ndc_pos = view_pos_to_ndc(view_pos, self.width(), self.height(),
                                  self.devicePixelRatio())

        # Store original node params for undo/preview
        original_params = self._selected_node_params.copy()

        # Initialize constraint state for this drag (S5-14)
        self._initial_drag_delta = None
        self._locked_axis = None
        self._snap_result = None
        self._constraint_config = ConstraintConfig()

        self._interaction_state.start_drag(
            tool=ViewerToolMode.CROP,
            node_id=self._interaction_state.selected_node_id,
            handle=handle,
            view_pos=view_pos,
            ndc_pos=ndc_pos,
            image_pos=image_pos,
            format_pos=QPointF(),
            original_params=original_params,
            modifiers=modifiers,
        )
        self.interaction_started.emit(self._interaction_state)
        return True

    def _handle_crop_move(self, view_pos: QPointF, modifiers: int) -> None:
        """Handle CROP tool move - compute new params and emit preview.

        Applies constraints (Shift for aspect ratio, Ctrl for axis lock)
        and snapping (to format edges/center) during drag.
        """
        image_pos = self.get_image_pos_at_view_pos(view_pos, check_bounds=False)
        if image_pos is None:
            return

        self._interaction_state.update_drag(view_pos, image_pos=image_pos)

        # Get raw delta
        delta = self._interaction_state.get_image_delta()

        # ── Apply Constraints (S5-14) ──
        # Ctrl: axis lock
        ctrl_pressed = bool(modifiers & Qt.KeyboardModifier.ControlModifier)
        if ctrl_pressed:
            # Determine locked axis from initial delta or current delta
            if self._initial_drag_delta is None:
                self._initial_drag_delta = delta
            self._locked_axis = determine_locked_axis(self._initial_drag_delta)
            delta = apply_axis_constraint(delta, self._locked_axis)

        # ── Apply Snapping (S5-14) ──
        # Compute current handle position in image coordinates
        handle = self._interaction_state.active_handle
        if handle and self._selected_node_type == "crop" and self._tex_width > 0 and self._tex_height > 0:
            # Get original crop params
            orig_crop = self._interaction_state.original_params
            orig_x = orig_crop.get("x", 0)
            orig_y = orig_crop.get("y", 0)
            orig_w = orig_crop.get("width", self._tex_width)
            orig_h = orig_crop.get("height", self._tex_height)

            # Compute where the handle would be after applying delta
            handle_image_pos = self._compute_crop_handle_position(
                handle, orig_x, orig_y, orig_w, orig_h, delta
            )

            # Apply snapping to the handle position
            self._snap_result = apply_snapping(
                QPointF(handle_image_pos.x(), handle_image_pos.y()),
                self._snap_config,
                self._tex_width,
                self._tex_height,
            )

            # Adjust delta to account for snap
            if self._snap_result.snapped_x:
                snap_delta_x = self._snap_result.snapped_position.x() - handle_image_pos.x()
                delta = QPointF(delta.x() + snap_delta_x, delta.y())
            if self._snap_result.snapped_y:
                snap_delta_y = self._snap_result.snapped_position.y() - handle_image_pos.y()
                delta = QPointF(delta.x(), delta.y() + snap_delta_y)

        # Compute new crop parameters from constrained and snapped delta
        if handle and self._selected_node_type == "crop":
            new_params = compute_crop_rect_from_handles(
                handle, self._interaction_state.original_params, delta
            )
            # Emit preview update (external handler will update node params)
            self._interaction_state.preview_params = new_params

        self.interaction_updated.emit(self._interaction_state)
        self.update()  # Trigger repaint to show snap lines

    def _compute_crop_handle_position(
        self,
        handle_id: str,
        orig_x: float, orig_y: float,
        orig_w: float, orig_h: float,
        delta: QPointF,
    ) -> QPointF:
        """Compute the position of a crop handle after applying delta.

        Args:
            handle_id: Handle identifier (e.g., "crop_left", "crop_top_left").
            orig_x, orig_y, orig_w, orig_h: Original crop rectangle.
            delta: Drag delta in image pixels.

        Returns:
            QPointF with the handle position in image pixel coordinates.
        """
        dx, dy = delta.x(), delta.y()

        # Compute handle position based on handle type
        if handle_id == "crop_left":
            return QPointF(orig_x + dx, orig_y + orig_h / 2.0)
        elif handle_id == "crop_right":
            return QPointF(orig_x + orig_w + dx, orig_y + orig_h / 2.0)
        elif handle_id == "crop_top":
            return QPointF(orig_x + orig_w / 2.0, orig_y + dy)
        elif handle_id == "crop_bottom":
            return QPointF(orig_x + orig_w / 2.0, orig_y + orig_h + dy)
        elif handle_id == "crop_top_left":
            return QPointF(orig_x + dx, orig_y + dy)
        elif handle_id == "crop_top_right":
            return QPointF(orig_x + orig_w + dx, orig_y + dy)
        elif handle_id == "crop_bottom_left":
            return QPointF(orig_x + dx, orig_y + orig_h + dy)
        elif handle_id == "crop_bottom_right":
            return QPointF(orig_x + orig_w + dx, orig_y + orig_h + dy)
        elif handle_id == "crop_center":
            return QPointF(orig_x + orig_w / 2.0 + dx, orig_y + orig_h / 2.0 + dy)
        else:
            return QPointF()

    def _handle_crop_release(self, view_pos: QPointF) -> dict:
        """Handle CROP tool release - commit final params and clear snap state."""
        original_params = self._interaction_state.end_drag()

        # Clear snap and constraint state (S5-14)
        self._snap_result = None
        self._locked_axis = None
        self._initial_drag_delta = None
        self._constraint_config = None

        self.interaction_ended.emit(self._interaction_state, original_params)
        self.update()  # Clear snap lines
        return original_params

    # ── B1: user ROI tool ────────────────────────────────────────────────

    def _handle_roi_press(self, view_pos: QPointF) -> bool:
        """Start dragging a new user ROI rectangle."""
        img_pos = self.get_image_pos_at_view_pos(view_pos, check_bounds=False)
        if img_pos is None:
            return False
        self._user_roi_drag_rect = (img_pos.x(), img_pos.y(), 0.0, 0.0)
        # AUD-9701: engage the interaction state so the drag feeds the
        # generic move dispatch (mouseMoveEvent only routes
        # _handle_tool_move while is_dragging) and the release dispatch
        # below can commit the rectangle. No interaction_started emission:
        # ROI commits through its own user_roi_changed path, not the
        # parameter-undo pipeline.
        self._interaction_state.start_drag(
            tool=ViewerToolMode.ROI,
            node_id=self._interaction_state.selected_node_id,
            handle=None,
            view_pos=view_pos,
            ndc_pos=view_pos_to_ndc(view_pos, self.width(), self.height(),
                                    self.devicePixelRatio()),
            image_pos=img_pos,
            format_pos=QPointF(),
            original_params={},
            modifiers=0,
        )
        return True

    def _handle_roi_move(self, view_pos: QPointF) -> None:
        """Update the live ROI rectangle while dragging."""
        if self._user_roi_drag_rect is None:
            return
        img_pos = self.get_image_pos_at_view_pos(view_pos, check_bounds=False)
        if img_pos is None:
            return
        x0, y0, _, _ = self._user_roi_drag_rect
        self._user_roi_drag_rect = (
            x0, y0, img_pos.x() - x0, img_pos.y() - y0)
        self.update()

    def _handle_roi_release(self, view_pos: QPointF) -> dict:
        """Commit (or discard) the dragged rectangle as the active ROI."""
        drag = self._user_roi_drag_rect
        self._user_roi_drag_rect = None
        if drag is None:
            return {}
        x0, y0, dx, dy = drag
        # A click (no real rectangle) clears the ROI — quick reset gesture.
        if abs(dx) < 3.0 or abs(dy) < 3.0:
            self.clear_user_roi()
            return {}
        x, y = min(x0, x0 + dx), min(y0, y0 + dy)
        w, h = abs(dx), abs(dy)
        self._apply_user_roi((float(x), float(y), float(w), float(h)))
        return {}

    # ── C2-3: PAINT tool ──────────────────────────────────────────────────

    def _paint_normalized_pos(self, view_pos: QPointF):
        """View position → normalized image coords ([0,1], top-left), or
        None when the position cannot be mapped to the current frame."""
        img_pos = self.get_image_pos_at_view_pos(view_pos, check_bounds=False)
        if img_pos is None:
            return None
        tex_w = max(1.0, float(self._tex_width))
        tex_h = max(1.0, float(self._tex_height))
        return (img_pos.x() / tex_w, img_pos.y() / tex_h)

    def _handle_paint_press(self, view_pos: QPointF, modifiers: int) -> bool:
        """PAINT tool: Alt+click sets the clone source point, plain drag
        records stroke points. Requires a selected Paint node."""
        if self._selected_node_type != "paint" or not self.get_selected_node():
            return False
        pos = self._paint_normalized_pos(view_pos)
        if pos is None:
            return False
        try:
            alt_down = bool(modifiers & Qt.KeyboardModifier.AltModifier)
        except TypeError:
            # Callers may pass a plain int instead of the flag enum.
            alt_down = bool(int(modifiers or 0)
                            & int(Qt.KeyboardModifier.AltModifier.value))
        if alt_down:
            # Clone stamp source: subsequent strokes sample the input at
            # 落点 − (首落点 − 源点), i.e. click where to copy FROM.
            self._paint_clone_src = [pos[0], pos[1]]
            self.update()
            return True
        self._paint_drag_points = [[pos[0], pos[1]]]
        self._paint_drag_node_id = str(self.get_selected_node())
        # AUD-9700: engage the interaction state so the stroke feeds the
        # generic move dispatch (mouseMoveEvent only routes
        # _handle_tool_move while is_dragging) and the release dispatch
        # can commit via paint_stroke_committed. No interaction_started
        # emission: strokes commit through the page's dedicated
        # _on_paint_stroke_committed undo path, not the parameter pipeline.
        img_pos = self.get_image_pos_at_view_pos(view_pos, check_bounds=False)
        self._interaction_state.start_drag(
            tool=ViewerToolMode.PAINT,
            node_id=self._paint_drag_node_id,
            handle=None,
            view_pos=view_pos,
            ndc_pos=view_pos_to_ndc(view_pos, self.width(), self.height(),
                                    self.devicePixelRatio()),
            image_pos=img_pos or QPointF(),
            format_pos=QPointF(),
            original_params={},
            modifiers=modifiers,
        )
        return True

    def _handle_paint_move(self, view_pos: QPointF,
                           modifiers: int = 0) -> None:
        self._paint_cursor_view = QPointF(view_pos)
        if self._paint_drag_points is not None:
            pos = self._paint_normalized_pos(view_pos)
            if pos is not None:
                tex_w = max(1.0, float(self._tex_width))
                tex_h = max(1.0, float(self._tex_height))
                last = self._paint_drag_points[-1]
                dx_px = (pos[0] - last[0]) * tex_w
                dy_px = (pos[1] - last[1]) * tex_h
                # Decimate: only append once the pen moved ≥ 2 canvas px.
                if dx_px * dx_px + dy_px * dy_px >= 4.0:
                    self._paint_drag_points.append([pos[0], pos[1]])
        self.update()

    def _handle_paint_release(self, view_pos: QPointF) -> dict:
        """Commit the finished stroke geometry to the page (which assembles
        the full stroke from the node's brush defaults — one undo each)."""
        points = self._paint_drag_points
        self._paint_drag_points = None
        self._paint_cursor_view = QPointF(view_pos)
        self.update()
        node_id = self._paint_drag_node_id
        self._paint_drag_node_id = None
        if not node_id or not points:
            return {}
        if str(self.get_selected_node()) != node_id:
            # Selection changed mid-drag — discard instead of misfiring.
            return {}
        tex_w = max(1.0, float(self._tex_width))
        stroke_geometry = {
            "points": points,
            # Brush radius is stored normalized against the canvas width so
            # strokes keep their relative footprint across resolutions.
            "size": max(1e-4, self._paint_brush_size_px / tex_w),
            "src_offset": [0.0, 0.0],
        }
        if self._paint_clone_src is not None:
            stroke_geometry["src_offset"] = [
                points[0][0] - self._paint_clone_src[0],
                points[0][1] - self._paint_clone_src[1],
            ]
        self.paint_stroke_committed.emit(str(node_id), stroke_geometry)
        return {}

    def _draw_paint_overlay(self) -> None:
        """QPainter overlay for the PAINT tool: brush circle at the cursor,
        the in-progress stroke polyline, and the clone source crosshair."""
        from PyQt6.QtGui import QPainter, QColor, QPen

        if self._selected_node_type != "paint" \
                or self._tool_mode != ViewerToolMode.PAINT:
            return
        if (self._paint_cursor_view is None
                and self._paint_drag_points is None
                and self._paint_clone_src is None):
            return

        def to_view(norm_xy):
            return self.get_view_pos_at_image_pos(
                QPointF(norm_xy[0] * float(self._tex_width),
                        norm_xy[1] * float(self._tex_height)))

        p = QPainter(self)
        try:
            p.setRenderHint(QPainter.RenderHint.Antialiasing, True)

            # Brush circle (screen radius follows the current zoom).
            if self._paint_cursor_view is not None:
                center = to_view((0.0, 0.0))
                edge = to_view((self._paint_brush_size_px
                                / max(1.0, float(self._tex_width)), 0.0))
                if center is not None and edge is not None:
                    r = abs(edge.x() - center.x())
                    pen = QPen(QColor(255, 255, 255, 200))
                    pen.setWidthF(1.0)
                    p.setPen(pen)
                    p.drawEllipse(self._paint_cursor_view, max(2.0, r),
                                  max(2.0, r))

            # In-progress stroke polyline.
            if self._paint_drag_points:
                pts = [to_view(pt) for pt in self._paint_drag_points]
                pts = [pt for pt in pts if pt is not None]
                if len(pts) > 1:
                    pen2 = QPen(QColor(120, 200, 255, 220))
                    pen2.setWidthF(1.5)
                    p.setPen(pen2)
                    for a, b in zip(pts[:-1], pts[1:]):
                        p.drawLine(a, b)

            # Clone source crosshair.
            if self._paint_clone_src is not None:
                c = to_view(self._paint_clone_src)
                if c is not None:
                    pen3 = QPen(QColor(255, 200, 80, 230))
                    pen3.setWidthF(1.5)
                    p.setPen(pen3)
                    p.drawLine(int(c.x()) - 8, int(c.y()), int(c.x()) + 8,
                               int(c.y()))
                    p.drawLine(int(c.x()), int(c.y()) - 8, int(c.x()),
                               int(c.y()) + 8)
        finally:
            p.end()

    def _apply_user_roi(self, rect) -> None:
        """Store a user ROI (image coords, top-left origin) and notify."""
        self._user_roi_rect = tuple(float(v) for v in rect)
        self.update()
        self.user_roi_changed.emit(self.get_user_roi_bbox())

    def set_user_roi(self, rect) -> None:
        """Set the user ROI from persisted state (image coords, y-down)."""
        if rect is None:
            self.clear_user_roi()
            return
        self._user_roi_rect = tuple(float(v) for v in rect[:4])
        self.update()

    def clear_user_roi(self) -> None:
        """Clear the user ROI — full frame rendering resumes."""
        had = self._user_roi_rect is not None
        self._user_roi_rect = None
        self._user_roi_drag_rect = None
        if self._tool_mode == ViewerToolMode.ROI:
            self.set_tool_mode(ViewerToolMode.PAN)
        self.update()
        if had:
            self.user_roi_changed.emit(None)

    def get_user_roi_bbox(self):
        """Return the user ROI as a GL-convention BBox (y-up), or None.

        Image coords are top-left origin; the render pipeline's BBox is
        bottom-left origin — flip y against the frame height.
        The rect is clamped to the frame so the request stays valid when
        the user dragged past the image edge.

        The frame reference is the pipeline's OUTPUT FORMAT when known
        (``_overlay_format``): the display texture is proxy-tier sized and
        an ROI anchored to it lands on the wrong region of the frame after
        a resolution switch or startup restore. With no format AND no
        texture yet (startup restore before the first frame) there is no
        honest answer — return None rather than a degenerate 1×1 box.
        """
        rect = self._user_roi_rect
        if rect is None:
            return None
        x, y, w, h = rect
        fmt = self._overlay_format
        if fmt is not None and getattr(fmt, "width", 0) > 0:
            frame_w = float(fmt.width)
            frame_h = float(fmt.height)
        else:
            frame_w = float(self._tex_width) or 0.0
            frame_h = float(self._tex_height) or 0.0
            if frame_w <= 0.0 or frame_h <= 0.0:
                return None
        x0 = max(0.0, min(x, frame_w - 1.0))
        y0 = max(0.0, min(y, frame_h - 1.0))
        x1 = max(x0 + 1.0, min(x + w, frame_w))
        y1 = max(y0 + 1.0, min(y + h, frame_h))
        from src.features.composite.core.format import BBox
        # Round endpoints once and derive width/height from the SAME
        # rounded values so odd sizes don't go off by one pixel.
        rx0, ry0 = int(round(x0)), int(round(frame_h - y1))
        rx1, ry1 = int(round(x1)), int(round(frame_h - y0))
        return BBox(rx0, ry0, max(1, rx1 - rx0), max(1, ry1 - ry0))

    def _handle_mask_press(self, view_pos: QPointF, modifiers: int) -> bool:
        """Handle MASK tool press."""
        handle = self._hover_handle
        check_bounds = (handle is None)
        image_pos = self.get_image_pos_at_view_pos(view_pos, check_bounds=check_bounds)
        if image_pos is None:
            return False

        ndc_pos = view_pos_to_ndc(view_pos, self.width(), self.height(),
                                  self.devicePixelRatio())

        # If no handle hit, may create new point (Ctrl+click)
        is_new_point = (modifiers & Qt.KeyboardModifier.ControlModifier) and handle is None

        self._interaction_state.start_drag(
            tool=ViewerToolMode.MASK,
            node_id=self._interaction_state.selected_node_id,
            handle=handle if not is_new_point else "new_point",
            view_pos=view_pos,
            ndc_pos=ndc_pos,
            image_pos=image_pos,
            format_pos=QPointF(),
            original_params={},
            modifiers=modifiers,
        )
        self.interaction_started.emit(self._interaction_state)
        return True

    def _handle_mask_move(self, view_pos: QPointF, modifiers: int) -> None:
        """Handle MASK tool move."""
        image_pos = self.get_image_pos_at_view_pos(view_pos, check_bounds=False)
        if image_pos is not None:
            self._interaction_state.update_drag(view_pos, image_pos=image_pos)
            self.interaction_updated.emit(self._interaction_state)

    def _handle_mask_release(self, view_pos: QPointF) -> dict:
        """Handle MASK tool release."""
        original_params = self._interaction_state.end_drag()
        self.interaction_ended.emit(self._interaction_state, original_params)
        return original_params

    def _handle_roto_press(self, view_pos: QPointF, modifiers: int) -> bool:
        """Handle ROTO tool press (Bezier spline editing)."""
        handle = self._hover_handle
        check_bounds = (handle is None)
        image_pos = self.get_image_pos_at_view_pos(view_pos, check_bounds=check_bounds)
        if image_pos is None:
            return False

        ndc_pos = view_pos_to_ndc(view_pos, self.width(), self.height(),
                                  self.devicePixelRatio())

        # Ctrl+click can create new point
        is_new_point = (modifiers & Qt.KeyboardModifier.ControlModifier) and handle is None

        self._interaction_state.start_drag(
            tool=ViewerToolMode.ROTO,
            node_id=self._interaction_state.selected_node_id,
            handle=handle if not is_new_point else "new_point",
            view_pos=view_pos,
            ndc_pos=ndc_pos,
            image_pos=image_pos,
            format_pos=QPointF(),
            original_params={},
            modifiers=modifiers,
        )
        self.interaction_started.emit(self._interaction_state)
        return True

    def _handle_roto_move(self, view_pos: QPointF, modifiers: int) -> None:
        """Handle ROTO tool move."""
        image_pos = self.get_image_pos_at_view_pos(view_pos, check_bounds=False)
        if image_pos is not None:
            self._interaction_state.update_drag(view_pos, image_pos=image_pos)
            self.interaction_updated.emit(self._interaction_state)

    def _handle_roto_release(self, view_pos: QPointF) -> dict:
        """Handle ROTO tool release."""
        original_params = self._interaction_state.end_drag()
        self.interaction_ended.emit(self._interaction_state, original_params)
        return original_params

    def _hit_test_cornerpin_handle(self, view_pos: QPointF, prefix: str) -> Optional[str]:
        """Hit-test CornerPin handles restricted to a handle family.

        Args:
            view_pos: Mouse position in view pixels.
            prefix: "to" for destination corners, "from" for source corners.

        Returns:
            Matching handle id, or None. Needed because to/from corners
            coincide at identity and the generic hit test always returns the
            first (to*) match.
        """
        for handle_id, handle_pos in self._tool_handles.items():
            if handle_id.startswith(prefix) and hit_test_handle(
                    view_pos, handle_pos, self._hit_radius_pixels):
                return handle_id
        return None

    def _handle_cornerpin_press(self, view_pos: QPointF, modifiers: int) -> bool:
        """Handle CORNERPIN tool press: grab a corner handle.

        Default drag moves the destination corners (to1-4); holding Alt
        grabs the source corners (from1-4) instead — the two families
        coincide at identity, so the modifier is the only unambiguous switch.
        """
        # Hit-test at the press position (hover may be stale when the user
        # clicks without moving, e.g. after switching tools).
        prefix = "from" if (modifiers & Qt.KeyboardModifier.AltModifier) else "to"
        handle = self._hit_test_cornerpin_handle(view_pos, prefix)
        if handle is None:
            h = self._hover_handle
            if h is not None and h.startswith(prefix):
                handle = h
        if handle is None:
            return False
        self._hover_handle = handle

        image_pos = self.get_image_pos_at_view_pos(view_pos, check_bounds=False)
        if image_pos is None:
            return False

        ndc_pos = view_pos_to_ndc(view_pos, self.width(), self.height(),
                                  self.devicePixelRatio())

        # Store original node params for undo/preview (same as Transform).
        # Without this the drag math baselines on defaults and the corner
        # jumps, and release records an empty undo snapshot.
        original_params = (self._selected_node_params.copy()
                           if self._selected_node_params else {})

        self._interaction_state.start_drag(
            tool=ViewerToolMode.CORNERPIN,
            node_id=self._interaction_state.selected_node_id,
            handle=handle,
            view_pos=view_pos,
            ndc_pos=ndc_pos,
            image_pos=image_pos,
            format_pos=QPointF(),
            original_params=original_params,
            modifiers=modifiers,
        )
        self.interaction_started.emit(self._interaction_state)
        return True

    def _handle_cornerpin_move(self, view_pos: QPointF, modifiers: int) -> None:
        """Handle CORNERPIN tool move."""
        image_pos = self.get_image_pos_at_view_pos(view_pos, check_bounds=False)
        if image_pos is not None:
            # Forward modifiers so the page can honor mid-drag switches
            # (Shift = Nuke-style freeze instead of the live warp preview).
            self._interaction_state.update_drag(
                view_pos, image_pos=image_pos, modifiers=modifiers)
            self.interaction_updated.emit(self._interaction_state)
            # Keep the handle dots in sync with the pointer while the node
            # model update is throttled (same as _handle_transform_move).
            self._update_tool_handles()
            self.update()

    def _handle_cornerpin_release(self, view_pos: QPointF) -> dict:
        """Handle CORNERPIN tool release."""
        original_params = self._interaction_state.end_drag()
        self.interaction_ended.emit(self._interaction_state, original_params)
        return original_params

    def enterEvent(self, event) -> None:
        super().enterEvent(event)
        self._mouse_in_viewer = True

    def leaveEvent(self, event) -> None:
        super().leaveEvent(event)
        self._mouse_in_viewer = False
        self._pixel_valid = False
        if hasattr(self, '_inspector_bar'):
            self._inspector_bar.update_pixel_info(0, 0, 0, 0, 0, 0, valid=False)
        self.update()

    # ── Display Controls ────────────────────────────────────────────────────

    def set_channel_mode(self, mode: int) -> None:
        """Set the channel display mode.

        Args:
            mode: 0=RGBA, 1=R, 2=G, 3=B, 4=A, 5=Luma, 6=AlphaOverlay
        """
        self._channel_mode = max(0, min(mode, 6))
        # Update toolbar button label
        for m_id, _, short_name in CHANNEL_MODES:
            if m_id == self._channel_mode:
                self._toolbar.update_channel_label(short_name)
                break
        self.update()

    def set_exposure(self, stops: float) -> None:
        """Set viewer exposure in stops (0 = no change)."""
        self._exposure = max(-10.0, min(10.0, round(stops, 1)))
        self._toolbar.update_exposure_label(self._exposure)
        self.update()

    def set_gamma(self, gamma: float) -> None:
        """Set viewer gamma (1.0 = no change)."""
        self._viewer_gamma = max(0.1, min(5.0, round(gamma, 2)))
        self.update()

    def set_viewer_gain(self, gain: float) -> None:
        """CP-055: viewer gain — linear-domain multiply (1.0 = no change)."""
        self._viewer_gain = max(0.0, min(16.0, round(gain, 3)))
        self.update()

    def set_viewer_lift(self, lift: float) -> None:
        """CP-055: viewer lift — raises blacks (0.0 = no change)."""
        self._viewer_lift = max(-1.0, min(1.0, round(lift, 3)))
        self.update()

    def set_viewer_offset(self, offset: float) -> None:
        """CP-055: viewer offset — linear additive (0.0 = no change)."""
        self._viewer_offset = max(-1.0, min(4.0, round(offset, 3)))
        self.update()

    def set_display_srgb(self, enabled: bool) -> None:
        """Toggle the linear→sRGB display transform (the composite ODT).

        When True (default), scene-linear working-space data is encoded to
        sRGB for correct monitor display. When False, raw linear values are
        shown directly (useful for inspecting HDR/linear data, equivalent to
        the pre-color-management gamma=1.0 passthrough).

        Back-compat alias for set_display_transfer(1 if enabled else 0).
        New code should call set_display_transfer to choose sRGB vs Rec.709.
        """
        self.set_display_transfer(1 if enabled else 0)

    def set_display_transfer(self, transfer: int) -> None:
        """Set the viewer ODT transfer function.

        Args:
            transfer: 0 = none (show raw linear), 1 = sRGB (~gamma 2.2,
                for still-image targets), 2 = Rec.709 (~gamma 2.4, for
                video targets), 3 = PQ (HDR target — linear nits→PQ code),
                4 = HLG (broadcast HDR target — linear→HLG OETF). Driven by
                the color.preview.* preferences + smart default.
        """
        self._display_transfer = max(0, min(4, int(transfer)))
        # Keep the legacy bool alias in sync (used by the toolbar toggle).
        self._display_srgb = self._display_transfer != 0
        self.update()

    def set_display_space(self, descriptor, tonemap: int = 0,
                          target_peak: float = 100.0) -> None:
        """Set the full viewer ODT from a ColorSpaceDescriptor.

        Drives display transfer + gamut (primaries) + optional HDR tonemap.
        This is the stage-2 entry point: pass an HDR descriptor (e.g.
        HDR10_VIDEO) with tonemap=1 to preview HDR content on an SDR display.

        Args:
            descriptor: ColorSpaceDescriptor for the preview/display target.
            tonemap: 0=off, 1=Reinhard, 2=ACES (HDR->SDR for SDR displays).
            target_peak: SDR display peak nits for the tonemap (default 100).
        """
        from src.shared.color.types import ColorTransfer, ColorPrimaries
        _TRANSFER_MODE = {
            ColorTransfer.LINEAR: 0, ColorTransfer.SRGB: 1,
            ColorTransfer.BT709: 2, ColorTransfer.PQ: 3, ColorTransfer.HLG: 4,
        }
        _PRIMARIES_MODE = {
            ColorPrimaries.BT709: 0, ColorPrimaries.BT2020: 1,
            ColorPrimaries.P3_DCI: 2, ColorPrimaries.P3_DISPLAY: 2,
        }
        self._display_transfer = _TRANSFER_MODE.get(descriptor.transfer, 1)
        self._display_primaries = _PRIMARIES_MODE.get(descriptor.primaries, 0)
        self._tonemap = max(0, min(2, int(tonemap)))
        self._target_peak = max(1.0, float(target_peak))
        self._display_srgb = self._display_transfer != 0
        # C6-2: remember the descriptor and invalidate any baked ODT LUT —
        # the next paint re-bakes from the active provider if it applies.
        self._display_space_descriptor = descriptor
        self._odt_lut_key = None
        self.update()

    # ── C6-2: OCIO-driven ODT (3D LUT) ──────────────────────────────────

    _ODT_LUT_SIZE = 32

    def _odt_color_provider(self):
        """Resolve the active colour provider for the ODT bake.

        Adapts the ConfigManager's single-arg ``get`` to the factory's
        ``(key, default)`` contract so the user's engine choice is honoured;
        any failure falls back to the default engine probe.
        """
        try:
            from src.shared.color.color_transform_provider import (
                get_color_provider)
            try:
                from src.shared.config.config_manager import ConfigManager
                base = ConfigManager()

                class _Cfg:
                    @staticmethod
                    def get(key, default=None):
                        try:
                            value = base.get(key)
                        except Exception:
                            return default
                        return default if value is None else value

                return get_color_provider(_Cfg())
            except Exception:
                return get_color_provider(None)
        except Exception:
            return None

    def _odt_descriptor_key(self, desc) -> tuple:
        return (str(getattr(desc, "primaries", "")),
                str(getattr(desc, "transfer", "")),
                str(getattr(desc, "range", "")))

    def _ensure_odt_lut(self) -> bool:
        """Bake/upload the OCIO ODT 3D LUT for the current display space.

        Returns True when the shader should drive the ODT stage from the
        LUT. Uses the provider only when it is actually OCIO (the builtin
        GLSL ODT already matches it — parity-tested). Any failure keeps the
        builtin path alive and is remembered so we don't re-attempt the same
        bake every frame. Never raises.
        """
        desc = self._display_space_descriptor
        if desc is None:
            return False
        key = self._odt_descriptor_key(desc)
        if self._odt_lut_key == key and self._odt_lut_tex_id:
            return True
        if self._odt_lut_failed_key == key:
            return False
        # Guard: with tonemap off and an HDR (nits) display transfer the
        # working values exceed the [0,1] LUT domain — keep builtin math.
        try:
            from src.shared.color.types import ColorTransfer
            if (self._tonemap == 0
                    and desc.transfer in (ColorTransfer.PQ, ColorTransfer.HLG)):
                self._odt_lut_failed_key = key
                return False
            provider = self._odt_color_provider()
            if provider is None or provider.name != "ocio" \
                    or not provider.is_available():
                self._odt_lut_failed_key = key
                return False
            dst_name = desc.to_ocio_name()
            # An unsupported primaries×transfer combo falls back to "lin"
            # in to_ocio_name() — baking that would silently activate an
            # IDENTITY LUT and switch the display encoding OFF (raw linear
            # preview). Keep the builtin path for those instead.
            from src.shared.color.color_space_descriptor import (
                OCIO_LINEAR_NAME)
            from src.shared.color.types import ColorTransfer as _CT
            if dst_name == OCIO_LINEAR_NAME and desc.transfer != _CT.LINEAR:
                self._odt_lut_failed_key = key
                return False
            data = self._bake_odt_lut_data(provider, dst_name)
            if data is None:
                self._odt_lut_failed_key = key
                return False
            tex_id = self._upload_odt_lut(data)
            if not tex_id:
                self._odt_lut_failed_key = key
                return False
            # Delete the previous LUT directly: we are inside paintGL where
            # the context is already current — makeCurrent/doneCurrent here
            # would release the context mid-draw and break the rest of the
            # frame (black/garbled first frame after a display switch).
            if self._odt_lut_tex_id:
                try:
                    tex = ctypes.c_uint(self._odt_lut_tex_id)
                    _gl.glDeleteTextures(1, ctypes.byref(tex))
                except Exception:
                    pass
                self._odt_lut_tex_id = 0
                self._odt_lut_key = None
            self._odt_lut_tex_id = tex_id
            self._odt_lut_key = key
            logger.info("ODT: using OCIO 3D LUT (%s, %d^3)",
                        dst_name, self._ODT_LUT_SIZE)
            return True
        except Exception as exc:
            logger.debug("ODT LUT bake failed, builtin ODT stays active: %s",
                         exc)
            self._odt_lut_failed_key = key
            return False

    def _bake_odt_lut_data(self, provider, dst_ocio_name: str):
        """Bake a working-space→display RGB LUT through the OCIO processor.

        Data layout matches glTexImage3D: x varies fastest and the shader
        indexes x with the RED input value, so red must be the fastest
        axis. meshgrid(indexing="ij") + C-order flatten makes BLUE fastest
        (last axis), so transpose before slicing. Without the transpose
        the processor is evaluated on red/blue-swapped inputs.
        Returns a contiguous (N, N, N, 3) float32 array, or None.
        """
        cpu = provider._get_processor("lin", dst_ocio_name)
        if cpu is None:
            return None
        n = self._ODT_LUT_SIZE
        axis = np.linspace(0.0, 1.0, n, dtype=np.float32)
        r, g, b = np.meshgrid(axis, axis, axis, indexing="ij")
        grid = np.stack([r, g, b, np.ones_like(r)], axis=-1)
        flat = np.ascontiguousarray(grid.reshape(-1, 4), dtype=np.float32)
        cpu.applyRGBA(flat)
        return np.ascontiguousarray(
            flat.reshape(n, n, n, 4).transpose(2, 1, 0, 3)[..., :3],
            dtype=np.float32)

    def _upload_odt_lut(self, data) -> int:
        """Upload the baked LUT as an RGB32F 3D texture (must be current)."""
        tex_id = int(_gl.glGenTextures(1))
        _gl.glBindTexture(GL_TEXTURE_3D, tex_id)
        _gl.glTexParameteri(GL_TEXTURE_3D, GL_TEXTURE_MIN_FILTER, GL_LINEAR)
        _gl.glTexParameteri(GL_TEXTURE_3D, GL_TEXTURE_MAG_FILTER, GL_LINEAR)
        _gl.glTexParameteri(GL_TEXTURE_3D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE)
        _gl.glTexParameteri(GL_TEXTURE_3D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE)
        _gl.glTexParameteri(GL_TEXTURE_3D, GL_TEXTURE_WRAP_R, GL_CLAMP_TO_EDGE)
        _gl.glPixelStorei(GL_UNPACK_ALIGNMENT, 1)
        n = self._ODT_LUT_SIZE
        _gl.glTexImage3D(GL_TEXTURE_3D, 0, GL_RGB32F, n, n, n, 0,
                         GL_RGB, GL_FLOAT, data)
        ok = _gl.glGetError() == 0
        _gl.glBindTexture(GL_TEXTURE_3D, 0)
        if not ok:
            tex = ctypes.c_uint(tex_id)
            _gl.glDeleteTextures(1, ctypes.byref(tex))
            return 0
        return tex_id

    def _delete_odt_lut_texture(self) -> None:
        if not self._odt_lut_tex_id:
            return
        made_current = False
        try:
            if self._gl_initialized:
                self.makeCurrent()
                made_current = True
            tex = ctypes.c_uint(self._odt_lut_tex_id)
            _gl.glDeleteTextures(1, ctypes.byref(tex))
        except Exception:
            pass
        finally:
            if made_current:
                self.doneCurrent()
        self._odt_lut_tex_id = 0
        self._odt_lut_key = None

    def set_tonemap(self, mode: int) -> None:
        """Set the HDR->SDR tonemap for preview (0=off, 1=Reinhard, 2=ACES)."""
        mode = max(0, min(2, int(mode)))
        if mode != self._tonemap:
            # The HDR guard keys off tonemap — a stale LUT (or a stale
            # failure latch from the HDR path) must not survive the switch.
            self._odt_lut_key = None
            self._odt_lut_failed_key = None
        self._tonemap = mode
        self.update()

    def set_target_peak(self, peak: float) -> None:
        """Set the SDR display peak luminance (nits) for HDR tonemap preview."""
        self._target_peak = max(1.0, float(peak))
        self.update()

    def _adjust_exposure_by(self, delta: float) -> None:
        """Adjust exposure by delta stops."""
        self.set_exposure(self._exposure + delta)

    def _set_bbox_overlay(self, enabled: bool) -> None:
        self._show_bbox_overlay = enabled
        self.update()

    def _set_format_overlay(self, enabled: bool) -> None:
        self._show_format_overlay = enabled
        self.update()

    def set_mask_preview(self, enabled: bool) -> None:
        """Toggle the alpha-mask preview mode (alpha channel as luma / overlay).

        Ported from the Color page viewport menu bar. Phase 3 wires this into
        the display shader via the ``u_alpha_preview`` uniform.
        """
        self._alpha_preview = 1 if enabled else 0
        self.update()

    def set_split_view(self, enabled: bool) -> None:
        """Toggle the split-view comparison.

        Ported from the Color page viewport menu bar. When enabled, the left
        half shows a snapshot of the frame captured at toggle time (the
        "before"), and the right half shows the live composite ("after"). The
        snapshot is copied into a viewer-owned texture so it stays stable
        across renderer texture-cache eviction.

        Note: a full selected-node comparison would require a second render
        pass; this snapshot-based compare is the safe default that reuses the
        existing owned-texture blit path.
        """
        enabled = bool(enabled)
        # Same-state guard: store_ab_snapshot() arms split view through the
        # toolbar button, whose toggled signal lands here — without the guard
        # every store would trigger a redundant second GPU blit.
        if self._split_active == enabled:
            return
        self._split_active = enabled
        if enabled:
            # Snapshot the current display texture into the secondary slot.
            self._snapshot_display_texture_to_secondary()
        else:
            # Clear the secondary texture when leaving split view.
            self._secondary_tex_id = 0
        self.update()

    def set_split_compare_mode(self, mode: str) -> None:
        """Set the A/B display mode without changing the captured snapshot."""
        normalized = str(mode).strip().lower()
        if normalized not in {"wipe", "difference"}:
            normalized = "wipe"
        self._split_compare_mode = normalized
        if hasattr(self, "_toolbar"):
            self._toolbar.update_split_compare_mode(normalized)
        self.update()

    def set_resolution_mode(self, mode: str) -> None:
        """B2-1: select the preview resolution tier.

        ``full``/``half``/``quarter`` map onto RenderRequest.proxy_scale
        (1.0/0.5/0.25); ``auto`` lets the page downgrade to half while the
        user interacts with the view. Pure UI state — the evaluation layer
        only ever sees proxy_scale through the page.
        """
        normalized = str(mode).strip().lower()
        if normalized not in {"full", "half", "quarter", "auto"}:
            normalized = "full"
        if normalized == self._resolution_mode:
            return
        self._resolution_mode = normalized
        if hasattr(self, "_toolbar"):
            self._toolbar.update_resolution_mode(normalized)
        # The page listens to this to apply proxy_scale (and to re-evaluate).
        self.resolution_mode_changed.emit(normalized)

    def _note_auto_proxy_interaction(self) -> None:
        """B2-1: report that the user is actively navigating the view.

        Called from wheel zoom and mouse drags. Emits ``auto_proxy_interaction
        (True)`` once per gesture burst and ``(False)`` after the idle timer
        fires, so the page can downgrade proxy_scale only while moving.
        """
        if self._auto_proxy_idle_timer is None:
            timer = QTimer(self)
            timer.setSingleShot(True)
            timer.setInterval(600)
            timer.timeout.connect(self._end_auto_proxy_interaction)
            self._auto_proxy_idle_timer = timer
        if not self._auto_proxy_interacting:
            self._auto_proxy_interacting = True
            self.auto_proxy_interaction.emit(True)
        self._auto_proxy_idle_timer.start()

    def _end_auto_proxy_interaction(self) -> None:
        if self._auto_proxy_interacting:
            self._auto_proxy_interacting = False
            self.auto_proxy_interaction.emit(False)

    def set_magnifier_enabled(self, enabled: bool) -> None:
        """B2-3: toggle the cursor-following magnifier loupe."""
        self._magnifier_enabled = bool(enabled)
        if hasattr(self, "_toolbar"):
            self._toolbar.update_magnifier(self._magnifier_enabled)
        self.update()

    def toggle_magnifier(self) -> None:
        self.set_magnifier_enabled(not self._magnifier_enabled)

    def set_magnifier_zoom(self, zoom: float) -> None:
        """B2-3: set the loupe magnification (4x or 8x)."""
        zoom = 8.0 if float(zoom) >= 6.0 else 4.0
        if abs(zoom - self._magnifier_zoom) < 1e-6:
            return
        self._magnifier_zoom = zoom
        if hasattr(self, "_toolbar"):
            self._toolbar.update_magnifier(self._magnifier_enabled, zoom)
        self.update()

    def set_background_mode(self, mode: str) -> None:
        """B2-4: select the 2D background ("checker" | "black" | "white")."""
        normalized = str(mode).strip().lower()
        if normalized not in {"checker", "black", "white"}:
            normalized = "checker"
        if normalized == self._background_mode:
            return
        self._background_mode = normalized
        if hasattr(self, "_toolbar"):
            self._toolbar.update_background_mode(normalized)
        self.background_mode_changed.emit(normalized)
        self.update()

    def _viewport_clear_color_rgb(self) -> tuple:
        """B2-4: clear colour for the 2D single-image path.

        Checker mode keeps the user's 3D-scene viewport colour; black/white
        override it (matte inspection against a flat field). The quad/3D
        paths always use the scene colour regardless of this mode.
        """
        if self._background_mode == "black":
            return (0.0, 0.0, 0.0)
        if self._background_mode == "white":
            return (1.0, 1.0, 1.0)
        return self._viewport_bg_color_rgb()

    def clear_split_snapshot(self) -> None:
        """Drop the captured A/B textures and leave split mode inactive."""
        self._split_active = False
        self._delete_owned_secondary_texture()
        self._delete_owned_secondary_b_texture()
        self._ab_active_slot = "A"
        if hasattr(self, "_toolbar"):
            self._toolbar.split_btn.setChecked(False)
            self._toolbar.update_ab_compare_slot("A")
        self.update()

    def store_ab_snapshot(self, slot: str) -> bool:
        """B2-2: capture the current frame into compare slot A or B.

        Arming a slot makes it the active compare source and enables split
        view, so ``Ctrl+Shift+A`` / ``Ctrl+Shift+B`` immediately wipe against
        the live frame. Snapshots live in viewer-owned GPU textures and are
        intentionally not persisted across sessions.
        """
        slot = "B" if str(slot).upper().startswith("B") else "A"
        if not self._snapshot_display_texture_to_slot(slot):
            return False
        self._split_active = True
        if hasattr(self, "_toolbar"):
            self._toolbar.split_btn.setChecked(True)
            self._toolbar.update_ab_compare_slot(slot)
        self.update()
        return True

    def set_ab_compare_slot(self, slot: str) -> None:
        """B2-2: choose which stored slot the wipe/difference compares."""
        slot = "B" if str(slot).upper().startswith("B") else "A"
        if slot == self._ab_active_slot:
            return
        tex_id = self._ab_owned_tex_id(slot)
        if not tex_id:
            # Nothing stored in that slot yet — keep the current source.
            return
        self._ab_active_slot = slot
        self._secondary_tex_id = tex_id
        if hasattr(self, "_toolbar"):
            self._toolbar.update_ab_compare_slot(slot)
        self.update()

    def _on_guide_changed(self, guide_type: str, guide_id: str, params: dict) -> None:
        """Handle guide selection from the Guides popup.

        Ported from the Color page viewport. Phase 2 draws the overlay via a
        QPainter pass in paintGL.
        """
        if guide_id == "default":
            self._active_guide = None
        else:
            self._active_guide = {
                "type": guide_type,
                "id": guide_id,
                "params": dict(params or {}),
            }
        self.update()

    def set_overlay_data(self, image_format=None, bbox=None) -> None:
        """Set format/bbox data for overlay display.

        Args:
            image_format: ImageFormat instance, or None to clear.
            bbox: BBox instance, or None to clear.
            Pass empty dict {} as sentinel to explicitly clear a field.
        """
        if image_format is not None:
            self._overlay_format = image_format if image_format else None
        if bbox is not None:
            self._overlay_bbox = bbox if bbox else None
        self.update()

    def _compute_uv_at_mouse(self, vp_w, vp_h, tw, th):
        """Map the current mouse position to texture UV coordinates.

        Returns (u, v) in [0,1] texture space, or None if outside the image.
        """
        if not self._mouse_in_viewer or tw <= 0 or th <= 0:
            return None

        dpr = self.devicePixelRatio()
        mx = self._mouse_pos.x() * dpr
        my = self._mouse_pos.y() * dpr

        # Convert mouse to NDC
        ndc_x = (mx / (vp_w / 2.0)) - 1.0
        ndc_y = 1.0 - (my / (vp_h / 2.0))

        # AUD-9702: reuse the exact on-screen quad rect — zoom/pan plus the
        # content-rect reservation for the toolbar / playback / inspector
        # bars — instead of re-deriving a full-viewport letterbox here. The
        # old duplicate assumed the image was centered in the whole
        # viewport, so the inspector read pixels offset from the cursor by
        # the reserved overlay space while the hit-test path (which uses
        # _compute_vertices) stayed correct.
        x0, y0, x1, y1 = self._compute_vertices(vp_w, vp_h, tw, th)

        # Map NDC to UV
        if x1 == x0 or y1 == y0:
            return None
        u = (ndc_x - x0) / (x1 - x0)
        v = (ndc_y - y0) / (y1 - y0)

        if u < 0.0 or u > 1.0 or v < 0.0 or v > 1.0:
            return None
        return u, v

    def _update_inspector_from_framebuffer(self, vp_w, vp_h) -> None:
        """Read the raw scene-linear pixel at the mouse position.

        Reads directly from the viewer's owned display texture
        the currently selected viewer-owned texture via a float readback,
        so the inspector shows the texture's actual linear values — including
        HDR values >1.0 that the on-screen framebuffer clamps to 8-bit. No
        reverse display transform is needed because we bypass the viewer
        shader entirely.

        Falls back to the legacy 8-bit framebuffer read when the owned
        texture is unavailable (e.g. the CPU uint8 set_image_data path).
        """
        if not self._mouse_in_viewer:
            self._pixel_valid = False
            return

        # Compute image-space coordinates (shared by both paths).
        tw, th = self._tex_width, self._tex_height
        uv = self._compute_uv_at_mouse(vp_w, vp_h, tw, th)
        if uv is not None:
            self._pixel_img_x = int(uv[0] * tw)
            self._pixel_img_y = int((1.0 - uv[1]) * th)  # Flip Y for image coords
            self._pixel_valid = True
        else:
            self._pixel_valid = False
            self._inspector_bar.update_pixel_info(0, 0, 0, 0, 0, 0, valid=False)
            return

        # Preferred path: read raw linear float from the owned texture.
        if self._owned_display_tex_id and self._owned_display_tex_id > 0:
            r, g, b, a = self._read_linear_pixel_from_texture(uv[0], uv[1], tw, th)
            if r is not None:
                self._inspector_bar.update_pixel_info(
                    self._pixel_img_x, self._pixel_img_y,
                    r, g, b, a, valid=True, space="linear"
                )
                return
            # Float readback failed — fall through to legacy path.

        # Legacy fallback: 8-bit framebuffer read + reverse display transform.
        # Used when the owned texture is unavailable (CPU uint8 path) or the
        # float readback hit a GL error.
        dpr = self.devicePixelRatio()
        fb_x = int(self._mouse_pos.x() * dpr)
        fb_y = int(vp_h - self._mouse_pos.y() * dpr)  # Flip Y for GL
        if fb_x < 0 or fb_x >= vp_w or fb_y < 0 or fb_y >= vp_h:
            self._pixel_valid = False
            self._inspector_bar.update_pixel_info(0, 0, 0, 0, 0, 0, valid=False)
            return
        try:
            pixel = _gl.glReadPixels(
                fb_x, fb_y, 1, 1,
                GL_RGBA, GL_UNSIGNED_BYTE,
                outputType=bytes,
            )
        except Exception as e:
            logger.debug(f"Pixel inspector readback failed: {e}")
            self._pixel_valid = False
            self._inspector_bar.update_pixel_info(0, 0, 0, 0, 0, 0, valid=False)
            return
        if pixel is None or len(pixel) < 4:
            self._pixel_valid = False
            self._inspector_bar.update_pixel_info(0, 0, 0, 0, 0, 0, valid=False)
            return
        r = pixel[0] / 255.0
        g = pixel[1] / 255.0
        b = pixel[2] / 255.0
        a = pixel[3] / 255.0
        # Legacy path reads display-space (post-ODT) values; tag accordingly.
        self._inspector_bar.update_pixel_info(
            self._pixel_img_x, self._pixel_img_y,
            r, g, b, a, valid=True,
            space="srgb" if self._display_srgb else "linear"
        )

    def _read_linear_pixel_from_texture(self, u: float, v: float,
                                        tw: int, th: int):
        """Read a single raw linear float pixel from the selected pass.

        Attaches the owned RGBA16F texture to a scratch FBO and does a
        1-pixel ``glReadPixels(..., GL_FLOAT)`` readback. The returned
        values are the texture's actual scene-linear data (can exceed 1.0
        for HDR pixels).

        Args:
            u, v: texture-space UV (origin bottom-left, from _compute_uv_at_mouse).
            tw, th: texture dimensions.

        Returns:
            (r, g, b, a) as floats in linear space, or (None, None, None, None)
            if the readback failed (GL error / invalid texture).
        """
        tex = self._current_tex_id
        if not tex or tex <= 0 or tw <= 0 or th <= 0:
            return None, None, None, None

        # Texture pixel coordinates (bottom-left origin, matching the FBO).
        tx = int(u * tw)
        ty = int(v * th)
        if tx < 0 or tx >= tw or ty < 0 or ty >= th:
            return None, None, None, None

        # Reuse the copy-read FBO as a scratch attachment target. We bind it
        # fresh here rather than relying on leftover state from _copy_shared.
        fbo = self._copy_read_fbo
        if fbo is None:
            fbo = int(_gl.glGenFramebuffers(1))
            self._copy_read_fbo = fbo
        default_fb = self.defaultFramebufferObject()
        try:
            _gl.glBindFramebuffer(GL_READ_FRAMEBUFFER, fbo)
            _gl.glFramebufferTexture2D(
                GL_READ_FRAMEBUFFER, GL_COLOR_ATTACHMENT0,
                GL_TEXTURE_2D, tex, 0,
            )
            buf = (ctypes.c_float * 4)()
            _gl.glReadPixels(tx, ty, 1, 1, GL_RGBA, GL_FLOAT, buf)
            r, g, b, a = float(buf[0]), float(buf[1]), float(buf[2]), float(buf[3])
        except Exception as e:
            logger.debug(f"Linear float pixel readback failed: {e}")
            return None, None, None, None
        finally:
            # Restore the default framebuffer so paintGL state stays clean.
            _gl.glBindFramebuffer(GL_FRAMEBUFFER, default_fb)
        return r, g, b, a

    def _ndc_from_pixel_rect(self, px_x, px_y, px_w, px_h, tex_w, tex_h):
        """Convert a pixel rect (in texture space) to NDC coordinates.

        Args:
            px_x, px_y: Top-left corner in image pixels (Y from top).
            px_w, px_h: Size in image pixels.
            tex_w, tex_h: Full texture dimensions.

        Returns (x0, y0, x1, y1) in NDC, or None if invalid.
        """
        if tex_w <= 0 or tex_h <= 0:
            return None

        # Convert pixel rect to UV (flip Y: pixel Y=0 is top, UV Y=0 is bottom)
        u0 = px_x / tex_w
        v0 = 1.0 - (px_y + px_h) / tex_h
        u1 = (px_x + px_w) / tex_w
        v1 = 1.0 - px_y / tex_h

        # Get the image rect in NDC from the same path used to draw the texture.
        # This keeps overlays/gizmos aligned with the displayed content, including
        # toolbar/playback-bar safe areas and any zoom/pan state.
        dpr = self.devicePixelRatio()
        vp_w = int(self.width() * dpr)
        vp_h = int(self.height() * dpr)
        img_x0, img_y0, img_x1, img_y1 = self._compute_vertices(
            vp_w, vp_h, tex_w, tex_h)

        # Map UV to NDC
        ndc_x0 = img_x0 + (img_x1 - img_x0) * u0
        ndc_x1 = img_x0 + (img_x1 - img_x0) * u1
        ndc_y0 = img_y0 + (img_y1 - img_y0) * v0
        ndc_y1 = img_y0 + (img_y1 - img_y0) * v1

        return ndc_x0, ndc_y0, ndc_x1, ndc_y1

    def _draw_overlays(self, img_x0, img_y0, img_x1, img_y1, tex_w, tex_h) -> None:
        """Draw bbox and format overlay rectangles."""
        if not self._overlay_prog:
            return
        if not self._show_bbox_overlay and not self._show_format_overlay:
            return

        _gl.glUseProgram(self._overlay_prog)

        # Get the color uniform location
        color_loc = _gl.glGetUniformLocation(self._overlay_prog, b"u_color")

        # Draw lines using the quad VBO (reuse with position-only data)
        _gl.glLineWidth(1.0)

        # We need position-only vertices for the overlay lines.
        # Since the VAO is set up for position+UV (stride=16, pos at offset 0),
        # we can still use it - the UV data will be ignored by the overlay shader
        # which only reads in_vert. But we need to make sure in_vert location matches.
        # Actually, let's use the overlay program's in_vert location.
        overlay_pos_loc = _gl.glGetAttribLocation(self._overlay_prog, b"in_vert")

        # If the overlay shader's in_vert location differs from the display shader's,
        # we need to set up the VAO differently. To keep things simple, we'll
        # just bind VAO and reconfigure the attribute for the overlay shader.

        if self._show_format_overlay and self._overlay_format:
            fmt = self._overlay_format
            ndc = self._ndc_from_pixel_rect(
                0, 0, fmt.width, fmt.height, tex_w, tex_h
            )
            if ndc:
                self._draw_overlay_rect(ndc, color_loc, (0.0, 0.8, 1.0, 0.7),
                                        overlay_pos_loc)

        if self._show_bbox_overlay and self._overlay_bbox:
            bbox = self._overlay_bbox
            if not bbox.is_empty:
                # BBox Y convention: 0 = bottom (OpenGL). Convert to top-left origin.
                ndc = self._ndc_from_pixel_rect(
                    bbox.x, tex_h - bbox.top, bbox.width, bbox.height,
                    tex_w, tex_h
                )
                if ndc:
                    self._draw_overlay_rect(ndc, color_loc, (1.0, 0.3, 0.3, 0.7),
                                            overlay_pos_loc)

        _gl.glUseProgram(0)

    # ── Overlay helpers ──

    def _draw_overlay_rect(self, ndc_rect, color_loc, color, pos_loc) -> None:
        x0, y0, x1, y1 = ndc_rect
        r, g, b, a = color

        # 4 corners for LINE_LOOP
        vertices = np.array([
            x0, y0, 0.0, 0.0,
            x1, y0, 0.0, 0.0,
            x1, y1, 0.0, 0.0,
            x0, y1, 0.0, 0.0,
        ], dtype=np.float32)

        _gl.glUniform4f(color_loc, ctypes.c_float(r), ctypes.c_float(g),
                         ctypes.c_float(b), ctypes.c_float(a))

        _gl.glBindBuffer(GL_ARRAY_BUFFER, self._quad_vbo)
        _gl.glBufferSubData(GL_ARRAY_BUFFER, 0, vertices.nbytes, vertices)

        # Reconfigure VAO for the overlay shader's attribute location
        _gl.glBindVertexArray(self._vao)
        _gl.glEnableVertexAttribArray(pos_loc)
        _gl.glVertexAttribPointer(pos_loc, 2, GL_FLOAT, GL_FALSE, 16, ctypes.c_void_p(0))

        _gl.glEnable(GL_BLEND)
        _gl.glBlendFunc(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA)
        _gl.glDrawArrays(GL_LINE_LOOP, 0, 4)
        _gl.glDisable(GL_BLEND)

        _gl.glBindVertexArray(0)
        _gl.glBindBuffer(GL_ARRAY_BUFFER, 0)

    def _draw_overlay_circle(self, center_ndc: tuple, 
                              radius_pixels: float, color: tuple,
                              pos_loc: int, color_loc: int, filled: bool = False) -> None:
        """Draw a circle handle in NDC space with fixed screen-pixel radius.

        Implements cosmetic pen effect: radius stays constant regardless of zoom.
        """
        import math as _math

        r, g, b, a = color
        cx, cy = center_ndc

        # Convert screen-pixel radius to NDC radius
        dpr = self.devicePixelRatio()
        vp_w = self.width() * dpr
        vp_h = self.height() * dpr

        ndc_radius_x = radius_pixels / (vp_w / 2.0)
        ndc_radius_y = radius_pixels / (vp_h / 2.0)

        # Generate circle vertices (32 segments)
        num_segments = 32
        vertices = []
        for i in range(num_segments):
            angle = 2.0 * _math.pi * i / num_segments
            x = cx + ndc_radius_x * _math.cos(angle)
            y = cy + ndc_radius_y * _math.sin(angle)
            vertices.extend([x, y, 0.0, 0.0])

        vertices_arr = np.array(vertices, dtype=np.float32)

        _gl.glUniform4f(color_loc, ctypes.c_float(r), ctypes.c_float(g),
                        ctypes.c_float(b), ctypes.c_float(a))

        _gl.glBindBuffer(GL_ARRAY_BUFFER, self._quad_vbo)
        if vertices_arr.nbytes > 64:
            # Pass the numpy array directly; _gl.glBufferData is PyOpenGL's
            # default binding, which rejects a raw .ctypes.data int pointer
            # ("Don't know how to convert None to an array type"). Matches the
            # pattern used elsewhere (e.g. initializeGL texture VBO upload).
            _gl.glBufferData(GL_ARRAY_BUFFER, vertices_arr.nbytes,
                            vertices_arr, GL_DYNAMIC_DRAW)
        else:
            _gl.glBufferSubData(GL_ARRAY_BUFFER, 0, vertices_arr.nbytes, vertices_arr)

        _gl.glBindVertexArray(self._vao)
        _gl.glEnableVertexAttribArray(pos_loc)
        _gl.glVertexAttribPointer(pos_loc, 2, GL_FLOAT, GL_FALSE, 16, ctypes.c_void_p(0))

        _gl.glEnable(GL_BLEND)
        _gl.glBlendFunc(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA)
        _gl.glDrawArrays(GL_LINE_LOOP, 0, num_segments)
        _gl.glDisable(GL_BLEND)

        _gl.glBindVertexArray(0)
        _gl.glBindBuffer(GL_ARRAY_BUFFER, 0)

    def _draw_overlay_line(self, start_ndc: tuple, end_ndc: tuple,
                           color: tuple, pos_loc: int, color_loc: int) -> None:
        """Draw a line in NDC space."""
        r, g, b, a = color

        vertices = np.array([
            start_ndc[0], start_ndc[1], 0.0, 0.0,
            end_ndc[0], end_ndc[1], 0.0, 0.0,
        ], dtype=np.float32)

        _gl.glUniform4f(color_loc, ctypes.c_float(r), ctypes.c_float(g),
                        ctypes.c_float(b), ctypes.c_float(a))

        _gl.glBindBuffer(GL_ARRAY_BUFFER, self._quad_vbo)
        _gl.glBufferSubData(GL_ARRAY_BUFFER, 0, vertices.nbytes, vertices)

        _gl.glBindVertexArray(self._vao)
        _gl.glEnableVertexAttribArray(pos_loc)
        _gl.glVertexAttribPointer(pos_loc, 2, GL_FLOAT, GL_FALSE, 16, ctypes.c_void_p(0))

        _gl.glEnable(GL_BLEND)
        _gl.glBlendFunc(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA)
        _gl.glLineWidth(1.0)
        _gl.glDrawArrays(GL_LINES, 0, 2)
        _gl.glDisable(GL_BLEND)

        _gl.glBindVertexArray(0)
        _gl.glBindBuffer(GL_ARRAY_BUFFER, 0)

    def _draw_overlay_cross(self, center_ndc: tuple, size_pixels: float,
                            color: tuple, pos_loc: int, color_loc: int) -> None:
        """Draw a crosshair (十字线) for center/pivot handles."""
        cx, cy = center_ndc
        r, g, b, a = color

        dpr = self.devicePixelRatio()
        vp_w = self.width() * dpr
        vp_h = self.height() * dpr

        ndc_size_x = size_pixels / (vp_w / 2.0)
        ndc_size_y = size_pixels / (vp_h / 2.0)

        vertices = np.array([
            cx - ndc_size_x, cy, 0.0, 0.0,
            cx + ndc_size_x, cy, 0.0, 0.0,
            cx, cy - ndc_size_y, 0.0, 0.0,
            cx, cy + ndc_size_y, 0.0, 0.0,
        ], dtype=np.float32)

        _gl.glUniform4f(color_loc, ctypes.c_float(r), ctypes.c_float(g),
                        ctypes.c_float(b), ctypes.c_float(a))

        _gl.glBindBuffer(GL_ARRAY_BUFFER, self._quad_vbo)
        _gl.glBufferSubData(GL_ARRAY_BUFFER, 0, vertices.nbytes, vertices)

        _gl.glBindVertexArray(self._vao)
        _gl.glEnableVertexAttribArray(pos_loc)
        _gl.glVertexAttribPointer(pos_loc, 2, GL_FLOAT, GL_FALSE, 16, ctypes.c_void_p(0))

        _gl.glEnable(GL_BLEND)
        _gl.glBlendFunc(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA)
        _gl.glLineWidth(1.0)
        _gl.glDrawArrays(GL_LINES, 0, 4)
        _gl.glDisable(GL_BLEND)

        _gl.glBindVertexArray(0)
        _gl.glBindBuffer(GL_ARRAY_BUFFER, 0)

    def _draw_overlay_quad(self, corners_ndc, color: tuple,
                           pos_loc: int, color_loc: int) -> None:
        """Draw a quadrilateral outline (GL_LINE_LOOP) through 4 NDC points.

        Used for the Transform rotated frame (the 4 transformed image corners).
        """
        r, g, b, a = color
        verts = []
        for cx, cy in corners_ndc:
            verts.extend([cx, cy, 0.0, 0.0])
        vertices = np.array(verts, dtype=np.float32)

        _gl.glUniform4f(color_loc, ctypes.c_float(r), ctypes.c_float(g),
                         ctypes.c_float(b), ctypes.c_float(a))
        _gl.glBindBuffer(GL_ARRAY_BUFFER, self._quad_vbo)
        _gl.glBufferSubData(GL_ARRAY_BUFFER, 0, vertices.nbytes, vertices)

        _gl.glBindVertexArray(self._vao)
        _gl.glEnableVertexAttribArray(pos_loc)
        _gl.glVertexAttribPointer(pos_loc, 2, GL_FLOAT, GL_FALSE, 16, ctypes.c_void_p(0))

        _gl.glEnable(GL_BLEND)
        _gl.glBlendFunc(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA)
        _gl.glDrawArrays(GL_LINE_LOOP, 0, 4)
        _gl.glDisable(GL_BLEND)

        _gl.glBindVertexArray(0)
        _gl.glBindBuffer(GL_ARRAY_BUFFER, 0)

    def _draw_overlay_square(self, center_ndc: tuple, side_pixels: float,
                             color: tuple, pos_loc: int, color_loc: int) -> None:
        """Draw a screen-aligned square handle with a fixed pixel side (cosmetic).

        Size stays constant regardless of zoom (Fusion-style corner/edge handle).
        """
        r, g, b, a = color
        cx, cy = center_ndc
        dpr = self.devicePixelRatio()
        vp_w = self.width() * dpr
        vp_h = self.height() * dpr
        hx = side_pixels / (vp_w / 2.0)
        hy = side_pixels / (vp_h / 2.0)

        vertices = np.array([
            cx - hx, cy - hy, 0.0, 0.0,
            cx + hx, cy - hy, 0.0, 0.0,
            cx + hx, cy + hy, 0.0, 0.0,
            cx - hx, cy + hy, 0.0, 0.0,
        ], dtype=np.float32)

        _gl.glUniform4f(color_loc, ctypes.c_float(r), ctypes.c_float(g),
                         ctypes.c_float(b), ctypes.c_float(a))
        _gl.glBindBuffer(GL_ARRAY_BUFFER, self._quad_vbo)
        _gl.glBufferSubData(GL_ARRAY_BUFFER, 0, vertices.nbytes, vertices)

        _gl.glBindVertexArray(self._vao)
        _gl.glEnableVertexAttribArray(pos_loc)
        _gl.glVertexAttribPointer(pos_loc, 2, GL_FLOAT, GL_FALSE, 16, ctypes.c_void_p(0))

        _gl.glEnable(GL_BLEND)
        _gl.glBlendFunc(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA)
        _gl.glDrawArrays(GL_LINE_LOOP, 0, 4)
        _gl.glDisable(GL_BLEND)

        _gl.glBindVertexArray(0)
        _gl.glBindBuffer(GL_ARRAY_BUFFER, 0)

    def _draw_glow_dot(self, center_ndc: tuple, radius_pixels: float,
                        color: tuple, pos_loc: int, color_loc: int,
                        glow_color: tuple = (0.4, 0.75, 1.0, 0.55)) -> None:
        """Draw a small filled dot with a blue glow halo (DaVinci-style control point).

        The dot is rendered as two concentric filled circles:
        1. An outer semi-transparent glow circle (larger, blue-tinted).
        2. An inner solid core circle (smaller, uses ``color``).

        Both circles use GL_TRIANGLE_FAN for a filled appearance.
        The cosmetic-pen effect keeps the pixel radius constant regardless of zoom.

        Args:
            center_ndc: (x, y) in NDC coordinates.
            radius_pixels: Core dot radius in screen pixels.
            color: RGBA for the core dot (e.g. white).
            pos_loc, color_loc: OpenGL uniform/attribute locations.
            glow_color: RGBA for the outer glow halo (default: soft blue).
        """
        import math as _math

        cx, cy = center_ndc
        dpr = self.devicePixelRatio()
        vp_w = self.width() * dpr
        vp_h = self.height() * dpr

        num_segments = 24

        def _build_fan_verts(r_px: float) -> np.ndarray:
            """Build GL_TRIANGLE_FAN vertex array for a filled circle."""
            r_ndc_x = r_px / (vp_w / 2.0)
            r_ndc_y = r_px / (vp_h / 2.0)
            verts = [cx, cy, 0.0, 0.0]  # center vertex
            for i in range(num_segments + 1):
                angle = 2.0 * _math.pi * i / num_segments
                verts.extend([cx + r_ndc_x * _math.cos(angle),
                              cy + r_ndc_y * _math.sin(angle),
                              0.0, 0.0])
            return np.array(verts, dtype=np.float32)

        def _upload_and_draw(arr: np.ndarray) -> None:
            _gl.glBindBuffer(GL_ARRAY_BUFFER, self._quad_vbo)
            if arr.nbytes > 64:
                _gl.glBufferData(GL_ARRAY_BUFFER, arr.nbytes, arr, GL_DYNAMIC_DRAW)
            else:
                _gl.glBufferSubData(GL_ARRAY_BUFFER, 0, arr.nbytes, arr)
            _gl.glBindVertexArray(self._vao)
            _gl.glEnableVertexAttribArray(pos_loc)
            _gl.glVertexAttribPointer(pos_loc, 2, GL_FLOAT, GL_FALSE, 16, ctypes.c_void_p(0))
            _gl.glEnable(GL_BLEND)
            _gl.glBlendFunc(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA)
            _gl.glDrawArrays(GL_TRIANGLE_FAN, 0, num_segments + 2)
            _gl.glDisable(GL_BLEND)
            _gl.glBindVertexArray(0)
            _gl.glBindBuffer(GL_ARRAY_BUFFER, 0)

        # 1. Outer glow halo (larger, semi-transparent blue)
        gr, gg, gb, ga = glow_color
        _gl.glUniform4f(color_loc, ctypes.c_float(gr), ctypes.c_float(gg),
                        ctypes.c_float(gb), ctypes.c_float(ga))
        glow_verts = _build_fan_verts(radius_pixels * 1.8)
        _upload_and_draw(glow_verts)

        # 2. Inner solid core (white or provided color)
        cr, cg, cb, ca = color
        _gl.glUniform4f(color_loc, ctypes.c_float(cr), ctypes.c_float(cg),
                        ctypes.c_float(cb), ctypes.c_float(ca))
        core_verts = _build_fan_verts(radius_pixels)
        _upload_and_draw(core_verts)

    def _constant_always_on_active(self) -> bool:
        """True when the selected Constant node should show always-on handles.

        Constant's gradient/radial modes expose on-screen handles that are
        editable in any tool mode (Fusion-style, same as Transform). This
        centralizes the condition so the draw path and the mouse-press path
        stay in sync.
        """
        if self._selected_node_type != "constant":
            return False
        params = self._selected_node_params or {}
        return params.get("mode", "solid") in ("gradient", "radial")

    def _draw_tool_handles(self, img_x0: float, img_y0: float,
                           img_x1: float, img_y1: float, tex_w: int, tex_h: int) -> None:
        """Draw tool handles for all tool modes.

        All tool types now use unified _tool_handles table for hit test consistency.
        Specialized drawing (bbox, auxiliary lines) is done first, then handles
        are drawn from _tool_handles dict.
        """
        if not self._overlay_prog:
            return

        tool_mode = self._tool_mode
        # Always-on overlay: a selected Transform node shows its handles in any
        # tool mode (Fusion-style). Text nodes do the same — they share the
        # Transform handle channel so you can move/scale/rotate the text layer
        # directly on the canvas without switching tools. Constant nodes in
        # gradient/radial mode do the same — their gradient/radial handles are
        # always editable when the node is selected, without switching to a
        # dedicated tool mode. Other tools only draw in their own mode.
        always_on_transform = (self._interaction_state.selected_node_id is not None
                               and self._selected_node_type in ("transform", "text"))
        always_on_constant = (self._interaction_state.selected_node_id is not None
                              and self._constant_always_on_active())
        # Mask/Roto controls are node-owned overlays just like Transform and
        # Color-page Masks.  Keep them visible when the node is selected so
        # choosing PAN/SELECT does not make the controls disappear.
        always_on_mask = (self._interaction_state.selected_node_id is not None
                          and self._selected_node_type in ("mask", "roto_shape"))
        # CornerPin: the four corner handles are node-owned overlays too —
        # keep them visible whenever the node is selected, so users can drag
        # corners without switching to the dedicated corner-pin tool.
        always_on_cornerpin = (self._interaction_state.selected_node_id is not None
                               and self._selected_node_type == "cornerpin")
        always_on = (always_on_transform or always_on_constant or always_on_mask
                     or always_on_cornerpin)
        if tool_mode in (ViewerToolMode.PAN, ViewerToolMode.SELECT) and not always_on:
            return

        if self._interaction_state.selected_node_id is None:
            return

        _gl.glUseProgram(self._overlay_prog)
        color_loc = _gl.glGetUniformLocation(self._overlay_prog, b"u_color")
        pos_loc = _gl.glGetAttribLocation(self._overlay_prog, b"in_vert")

        image_ndc_rect = (img_x0, img_y0, img_x1, img_y1)

        # Draw auxiliary lines/bboxes for specialized tools (before handles)
        if tool_mode == ViewerToolMode.TRANSFORM or always_on_transform:
            self._draw_transform_auxiliary(image_ndc_rect, tex_w, tex_h, pos_loc, color_loc)
        elif tool_mode == ViewerToolMode.CORNERPIN or always_on_cornerpin:
            self._draw_cornerpin_auxiliary(image_ndc_rect, tex_w, tex_h, pos_loc, color_loc)
        elif tool_mode == ViewerToolMode.MASK or (always_on_mask and self._selected_node_type == "mask"):
            self._draw_mask_auxiliary(image_ndc_rect, tex_w, tex_h, pos_loc, color_loc)
        elif tool_mode == ViewerToolMode.ROTO or (always_on_mask and self._selected_node_type == "roto_shape"):
            self._draw_roto_auxiliary(image_ndc_rect, tex_w, tex_h, pos_loc, color_loc)
        # NOTE: constant gradient/radial overlay is drawn with QPainter below
        # ( Masks-quality rendering: QPen cosmetic borders, antialiasing, dash
        # patterns — none of which the GL overlay primitives support).

        # Unified handle drawing from _tool_handles dict (all tool types).
        # Constant handles are skipped here and drawn by QPainter instead.
        self._draw_handles_from_dict(pos_loc, color_loc)

        _gl.glUseProgram(0)

        # Constant gradient/radial: render the whole overlay (auxiliary lines +
        # handles) with QPainter so it matches the Color page Masks tool exactly.
        # QPainter on a QOpenGLWidget draws on top of the GL framebuffer after
        # beginNativePainting()/endNativePainting() state management is handled
        # automatically.
        if always_on_constant:
            self._draw_constant_handles_qpainter(image_ndc_rect, tex_w, tex_h)

        # C2-3: PAINT tool brush circle / in-progress stroke / clone source.
        if self._tool_mode == ViewerToolMode.PAINT:
            self._draw_paint_overlay()

    def _draw_handles_from_dict(self, pos_loc: int, color_loc: int) -> None:
        """Draw handles from _tool_handles dict (generic method).

        Used for ALL tool types: CROP, REFORMAT, TRANSFORM, CORNERPIN, MASK, ROTO.
        Handles are already in view pixel coordinates from _update_tool_handles().
        This ensures hit test and drawing use the same data source.
        """
        if not self._tool_handles:
            return

        dpr = self.devicePixelRatio()

        # Import colors for specialized handle types
        from src.features.composite.ui.viewer_handles import (
            HANDLE_COLOR_SCALE_X,
            HANDLE_COLOR_SCALE_Y,
            HANDLE_COLOR_ROTATE,
            HANDLE_COLOR_CORNERPIN,
            HANDLE_COLOR_SOURCE,
            MASK_COLOR_FEATHER,
            MASK_COLOR_ROTATE,
        )

        for handle_id, view_pos in self._tool_handles.items():
            # Constant handles are drawn by QPainter (_draw_constant_handles_qpainter)
            # for Masks-quality rendering; skip them in the GL path.
            if handle_id.startswith("constant_"):
                continue
            # Convert view position back to NDC for drawing
            ndc_pos = view_pos_to_ndc(view_pos, self.width(), self.height(), dpr)

            # Determine color based on state
            is_hovered = (handle_id == self._hover_handle)
            is_selected = (handle_id in self._selection_state.selected_handles)
            is_active = (handle_id == self._interaction_state.active_handle)

            if is_active:
                color = (1.0, 0.5, 0.0, 1.0)  # Orange for active/dragging
            elif is_selected:
                color = (1.0, 0.7, 0.0, 0.8)  # Orange for selected
            elif is_hovered:
                color = (1.0, 0.9, 0.3, 1.0)  # Yellow for hovered
            else:
                # Color based on handle type (use viewer_handles color definitions)
                if handle_id.startswith("crop_") or handle_id.startswith("reformat_"):
                    color = (1.0, 1.0, 1.0, 0.7)  # White for crop/reformat
                elif handle_id == "center" or handle_id == "pivot":
                    color = (1.0, 1.0, 1.0, 0.8)  # White for transform anchor
                elif handle_id.startswith("scale_x_"):
                    color = HANDLE_COLOR_SCALE_X  # Light green for scale-X
                elif handle_id.startswith("scale_y_"):
                    color = HANDLE_COLOR_SCALE_Y  # Light blue for scale-Y
                elif handle_id == "rotate":
                    color = HANDLE_COLOR_ROTATE  # Cyan for rotate
                elif handle_id.startswith("scale_corner_"):
                    color = (1.0, 1.0, 1.0, 0.9)  # White for corners
                elif handle_id.startswith("to"):
                    color = HANDLE_COLOR_CORNERPIN  # Gold for destination
                elif handle_id.startswith("from"):
                    color = HANDLE_COLOR_SOURCE  # Gray for source
                elif handle_id.startswith("mask_feather") or handle_id.startswith("roto_feather"):
                    color = MASK_COLOR_FEATHER  # Cyan for feather
                elif handle_id.startswith("constant_"):
                    color = (1.0, 1.0, 1.0, 0.94)  # White for constant handles
                elif handle_id.startswith("mask_rotate") or handle_id.startswith("roto_rotate"):
                    color = MASK_COLOR_ROTATE  # Cyan for rotate
                elif handle_id.startswith("mask_") or handle_id.startswith("roto_"):
                    color = (1.0, 1.0, 1.0, 0.94)  # White for mask/roto handles
                else:
                    color = (0.5, 0.5, 0.5, 0.6)  # Gray for unknown

            # Draw handle shape based on type
            # Transform handles: all rendered as glow dots (white core + blue halo)
            TRANSFORM_IDS = {"center", "pivot", "rotate"}
            is_transform_handle = (
                handle_id in TRANSFORM_IDS
                or handle_id.startswith("scale_")
            )

            if is_transform_handle:
                # Glow dot style: white core + blue halo for all transform handles
                core_color = (1.0, 1.0, 1.0, 0.95)  # Bright white core
                glow_color = (0.4, 0.75, 1.0, 0.55)  # Soft blue halo
                if is_active:
                    core_color = (1.0, 0.75, 0.3, 1.0)   # Orange core when dragging
                    glow_color = (1.0, 0.5, 0.0, 0.55)   # Orange glow when dragging
                elif is_hovered:
                    core_color = (1.0, 0.95, 0.6, 1.0)   # Warm yellow on hover
                    glow_color = (1.0, 0.85, 0.2, 0.6)   # Yellow glow on hover
                # 控制点尺寸整体缩小 1/3（原 5.0 / 7.0 → 3.3 / 4.7）
                radius = 3.3 if handle_id not in ("rotate", "pivot", "center") else 4.7
                self._draw_glow_dot(
                    (ndc_pos.x(), ndc_pos.y()), radius,
                    core_color, pos_loc, color_loc,
                    glow_color=glow_color,
                )
            elif handle_id.endswith("_center"):
                # Mask/Roto center crosshair
                self._draw_overlay_cross((ndc_pos.x(), ndc_pos.y()), 12.0, color, pos_loc, color_loc)
            elif handle_id.startswith("crop_") or handle_id.startswith("reformat_"):
                # Crop/Reformat: keep square style
                if handle_id.endswith("_center"):
                    self._draw_overlay_cross((ndc_pos.x(), ndc_pos.y()), 12.0, color, pos_loc, color_loc)
                else:
                    side = 8.0
                    self._draw_overlay_square((ndc_pos.x(), ndc_pos.y()), side, color, pos_loc, color_loc)
            else:
                # Circle handles: cornerpin, mask/roto points
                radius = 8.0
                if handle_id.endswith("_rotate"):
                    radius = 10.0  # Larger rotate handle
                elif handle_id.startswith("from"):
                    radius = 6.0  # Smaller source handles
                elif handle_id.endswith("_feather"):
                    radius = 7.0  # Feather handles slightly smaller
                self._draw_overlay_circle((ndc_pos.x(), ndc_pos.y()), radius, color, pos_loc, color_loc)

    def _draw_transform_auxiliary(self, image_ndc_rect: tuple, tex_w: int, tex_h: int,
                                  pos_loc: int, color_loc: int) -> None:
        """Draw the Transform on-screen control: rotated frame + rotate guide.

        The frame is the rotated rectangle through the four transformed image
        corners (not an axis-aligned bbox). During a drag, params are recomputed
        live from the current mouse so the frame tracks the interaction. The
        handles themselves are drawn separately by _draw_handles_from_dict().
        """
        from src.features.composite.ui.viewer_handles import (
            compute_transform_handles,
            HANDLE_COLOR_BBOX,
            HANDLE_COLOR_ROTATE,
        )

        params = self._effective_text_transform_params(
            self._current_transform_params_for_overlay())

        # The frame should hug the input image, not the composite canvas.
        # Resolve the input size the same way as _compute_transform_handles_dict
        # so the frame outline stays in sync with the handle dots.
        img_w, img_h = self._selected_transform_input_size()

        handles_data = compute_transform_handles(
            params.get("translate_x", 0.0),
            params.get("translate_y", 0.0),
            params.get("scale_x", 1.0),
            params.get("scale_y", 1.0),
            params.get("rotation", 0.0),
            params.get("pivot_mode", "center"),
            params.get("pivot_x", img_w / 2.0),
            params.get("pivot_y", img_h / 2.0),
            img_w, img_h, image_ndc_rect,
            canvas_width=tex_w,
            canvas_height=tex_h,
        )

        # Rotated frame outline (quad through the 4 transformed corners)
        self._draw_overlay_quad(handles_data.frame_corners, HANDLE_COLOR_BBOX,
                                pos_loc, color_loc)

        # Draw the internal rotation guide from the rotate handle to the pivot.
        # The handle orbits the pivot during a rotate drag, so recompute its NDC
        # so the guide line tracks the orbiting handle.
        rotate_ndc = self._effective_rotate_handle_ndc(handles_data)
        self._draw_overlay_line(rotate_ndc, handles_data.pivot,
                                HANDLE_COLOR_ROTATE, pos_loc, color_loc)

    def _draw_cornerpin_auxiliary(self, image_ndc_rect: tuple, tex_w: int, tex_h: int,
                                  pos_loc: int, color_loc: int) -> None:
        """Draw CornerPin node auxiliary lines (destination and source outlines).

        Handles are drawn by _draw_handles_from_dict() using _tool_handles table.
        This method only draws the outline quads for destination and source corners.
        """
        from src.features.composite.ui.viewer_handles import compute_cornerpin_handles

        # Use live overlay params: during a drag this recomputes the active
        # corner from the pointer (node model is throttled), so the outlines
        # track the interaction the same way the Transform frame does.
        params = self._current_cornerpin_params_for_overlay()
        if not params:
            params = {
                "to1_x": 0.0, "to1_y": 0.0,
                "to2_x": 1.0, "to2_y": 0.0,
                "to3_x": 1.0, "to3_y": 1.0,
                "to4_x": 0.0, "to4_y": 1.0,
                "from1_x": 0.0, "from1_y": 0.0,
                "from2_x": 1.0, "from2_y": 0.0,
                "from3_x": 1.0, "from3_y": 1.0,
                "from4_x": 0.0, "from4_y": 1.0,
            }

        to_corners = [
            (params.get("to1_x", 0.0), params.get("to1_y", 0.0)),
            (params.get("to2_x", 1.0), params.get("to2_y", 0.0)),
            (params.get("to3_x", 1.0), params.get("to3_y", 1.0)),
            (params.get("to4_x", 0.0), params.get("to4_y", 1.0)),
        ]
        from_corners = [
            (params.get("from1_x", 0.0), params.get("from1_y", 0.0)),
            (params.get("from2_x", 1.0), params.get("from2_y", 0.0)),
            (params.get("from3_x", 1.0), params.get("from3_y", 1.0)),
            (params.get("from4_x", 0.0), params.get("from4_y", 1.0)),
        ]

        handles_data = compute_cornerpin_handles(
            to_corners, from_corners, tex_w, tex_h, image_ndc_rect,
        )

        # Draw destination outline (gold)
        outline_color = (1.0, 0.8, 0.2, 0.6)
        for i in range(4):
            self._draw_overlay_line(handles_data.to_corners[i],
                                    handles_data.to_corners[(i + 1) % 4],
                                    outline_color, pos_loc, color_loc)

        # Draw source outline (semi-transparent gray)
        source_color = (0.6, 0.6, 0.6, 0.3)
        for i in range(4):
            self._draw_overlay_line(handles_data.from_corners[i],
                                    handles_data.from_corners[(i + 1) % 4],
                                    source_color, pos_loc, color_loc)

    def _draw_mask_auxiliary(self, image_ndc_rect: tuple, tex_w: int, tex_h: int,
                             pos_loc: int, color_loc: int) -> None:
        """Draw Mask node auxiliary lines (bbox outline).

        Handles are drawn by _draw_handles_from_dict() using _tool_handles table.
        This method only draws the bbox outline.
        """
        from src.features.composite.ui.viewer_handles import compute_mask_handles

        params = self._selected_node_params
        if not params:
            params = {
                "mask_type": "rectangle",
                "center_x": 0.5,
                "center_y": 0.5,
                "width": 1.0,
                "height": 1.0,
                "rotation": 0.0,
                "feather": 0.0,
            }

        handles_data = compute_mask_handles(
            params.get("mask_type", "rectangle"),
            params.get("center_x", 0.5),
            params.get("center_y", 0.5),
            params.get("width", 1.0),
            params.get("height", 1.0),
            params.get("rotation", 0.0),
            params.get("feather", 0.0),
            tex_w, tex_h, image_ndc_rect,
        )

        # Draw bbox outline (white, semi-transparent)
        bbox_color = (1.0, 1.0, 1.0, 0.5)
        self._draw_overlay_rect(handles_data.bbox_rect, color_loc,
                                bbox_color, pos_loc)

        # Draw rotate handle connection line
        rotate_color = (0.0, 1.0, 1.0, 0.4)
        self._draw_overlay_line(handles_data.center, handles_data.rotate,
                                rotate_color, pos_loc, color_loc)

    def _draw_constant_handles_qpainter(self, image_ndc_rect: tuple, tex_w: int, tex_h: int):
        """Draw the Constant gradient/radial overlay with QPainter.

        This mirrors the Color page Masks tool rendering exactly (same QPen
        cosmetic borders, QBrush fills, antialiasing, and dash patterns) so the
        handles look and feel identical to Masks' Circle/Gradient. QPainter on a
        QOpenGLWidget composites on top of the GL framebuffer.

        The handle positions are already in self._tool_handles as view pixel
        QPointF (logical pixels), which is exactly the coordinate system QPainter
        uses on this widget — no conversion needed.
        """
        from PyQt6.QtCore import QLineF
        from PyQt6.QtGui import QPainter, QColor, QPen, QBrush

        if not self._tool_handles:
            return

        # Resolve handle view positions (logical pixels).
        def pos(hid: str) -> QPointF:
            return self._tool_handles[hid]

        params = self._selected_node_params or {}
        mode = params.get("mode", "solid")
        is_radial = (mode == "radial")

        # Interaction state → per-handle highlight color (matches the
        # is_active/is_hovered logic used by the GL path).
        active_handle = self._interaction_state.active_handle
        hover_handle = self._hover_handle

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        # ── Masks-style color palette (copied from canvas.py MaskHandle) ──
        border_color = QColor(60, 60, 60)            # cosmetic pen border
        fill_white = QColor(255, 255, 255, 240)      # default white fill
        fill_center = QColor(255, 255, 255, 180)     # subtle white for center

        # Outline color for auxiliary lines. Masks uses an orange when active,
        # warm yellow on hover, gray otherwise — replicate that here.
        if active_handle is not None:
            aux_color = QColor(255, 136, 0)
        elif hover_handle is not None:
            aux_color = QColor(255, 180, 100, 200)
        else:
            aux_color = QColor(100, 100, 100, 180)

        def make_pen(color: QColor, width: float, style=None) -> QPen:
            pen = QPen(color, width)
            pen.setCosmetic(True)
            if style is not None:
                pen.setStyle(style)
            return pen

        def handle_fill_color(hid: str) -> QColor:
            # Highlight takes precedence over the type-based fill.
            if hid == active_handle:
                return QColor(255, 136, 0)
            if hid == hover_handle:
                return QColor(255, 180, 100)
            if hid == "constant_radial_center":
                return fill_center
            return fill_white

        # ── 1. Auxiliary lines (gradient axis / radial boundary) ──
        if is_radial:
            center = pos("constant_radial_center")
            edge = pos("constant_radial_radius")
            # center → radius edge line
            painter.setPen(make_pen(aux_color, 1.0))
            painter.drawLine(center, edge)
            # boundary circle through the radius edge (QPainter draws a true
            # ellipse, unlike the GL approximation)
            dpr = self.devicePixelRatio()
            # radius in logical pixels: the handle positions are already in
            # logical pixels, so distance is directly the pixel radius.
            dx = (edge.x() - center.x()) * dpr
            dy = (edge.y() - center.y()) * dpr
            radius_px = math.hypot(dx, dy) / dpr
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(make_pen(QColor(255, 255, 255, 90), 1.0))
            painter.drawEllipse(center, radius_px, radius_px)
        else:
            start = pos("constant_grad_start")
            end = pos("constant_grad_end")
            # gradient axis (Masks uses a 2.0px solid line)
            painter.setPen(make_pen(aux_color, 2.0))
            painter.drawLine(start, end)
            # perpendicular boundary dashed lines at start and end (Masks style)
            line = QLineF(start, end)
            normal = line.normalVector().unitVector()
            n_vec = QPointF(normal.dx() * 500, normal.dy() * 500)
            dash_pen = make_pen(aux_color, 1.0, Qt.PenStyle.DashLine)
            painter.setPen(dash_pen)
            painter.drawLine(start - n_vec, start + n_vec)
            painter.drawLine(end - n_vec, end + n_vec)

        # ── 2. Handles (Masks-style filled dots with cosmetic borders) ──
        def draw_dot(center: QPointF, diameter: float, fill: QColor) -> None:
            """Draw a Masks-style handle: filled circle with a dark border."""
            painter.setBrush(QBrush(fill))
            painter.setPen(make_pen(border_color, 1.0))
            r = diameter / 2.0
            painter.drawEllipse(center, r, r)

        for hid in self._tool_handles:
            if not hid.startswith("constant_"):
                continue
            center = pos(hid)
            if hid == "constant_radial_center":
                # 11px dot + black crosshair (Masks center style)
                draw_dot(center, 11.0, handle_fill_color(hid))
                cross_pen = make_pen(QColor(0, 0, 0, 200), 1.0)
                painter.setPen(cross_pen)
                painter.drawLine(QPointF(center.x() - 6, center.y()),
                                 QPointF(center.x() + 6, center.y()))
                painter.drawLine(QPointF(center.x(), center.y() - 6),
                                 QPointF(center.x(), center.y() + 6))
            else:
                # grad_start / grad_end / radial_radius: 9px dot (Masks node size)
                draw_dot(center, 9.0, handle_fill_color(hid))

        painter.end()

    def _draw_roto_auxiliary(self, image_ndc_rect: tuple, tex_w: int, tex_h: int,
                             pos_loc: int, color_loc: int) -> None:
        """Draw RotoShape node auxiliary lines (bbox outline).

        Handles are drawn by _draw_handles_from_dict() using _tool_handles table.
        This method only draws the bbox outline and auxiliary lines.
        """
        from src.features.composite.ui.viewer_handles import compute_roto_handles

        params = self._selected_node_params
        if not params:
            params = {
                "shape_type": "rectangle",
                "center_x": 0.5,
                "center_y": 0.5,
                "width": 1.0,
                "height": 1.0,
                "rotation": 0.0,
                "feather": 0.0,
                "opacity": 1.0,
            }

        if params.get("shape_type", "rectangle") == "bezier":
            from src.features.composite.nodes.roto_shape_node import RotoShapeNode
            points = RotoShapeNode.parse_points(params.get("points", "[]"))
            points = RotoShapeNode.apply_point_parameter_overrides(
                points, params)
            if len(points) >= 3:
                x0, y0, x1, y1 = image_ndc_rect
                path: list[tuple[float, float]] = []
                for index, point in enumerate(points):
                    nxt = points[(index + 1) % len(points)]
                    start = point["pos"]
                    c1 = point["c2"]
                    c2 = nxt["c1"]
                    end = nxt["pos"]
                    if not path:
                        path.append((x0 + start[0] * (x1 - x0),
                                     y1 - start[1] * (y1 - y0)))
                    for sample in range(1, 7):
                        t = sample / 6.0
                        omt = 1.0 - t
                        px = (omt ** 3 * start[0]
                              + 3.0 * omt ** 2 * t * c1[0]
                              + 3.0 * omt * t ** 2 * c2[0]
                              + t ** 3 * end[0])
                        py = (omt ** 3 * start[1]
                              + 3.0 * omt ** 2 * t * c1[1]
                              + 3.0 * omt * t ** 2 * c2[1]
                              + t ** 3 * end[1])
                        path.append((x0 + px * (x1 - x0),
                                     y1 - py * (y1 - y0)))
                if path:
                    path.append(path[0])
                    self._draw_gizmo_polyline(
                        path, (0.0, 1.0, 1.0, 0.8), pos_loc, color_loc,
                        self.width(), self.height(), thickness_px=2.0)
                # Tangent guides use the same grey used by Color Masks.
                for point in points:
                    anchor = (x0 + point["pos"][0] * (x1 - x0),
                              y1 - point["pos"][1] * (y1 - y0))
                    for tangent_key in ("c1", "c2"):
                        tangent = (x0 + point[tangent_key][0] * (x1 - x0),
                                   y1 - point[tangent_key][1] * (y1 - y0))
                        self._draw_overlay_line(
                            anchor, tangent, (0.65, 0.65, 0.65, 0.7),
                            pos_loc, color_loc)
                return

        handles_data = compute_roto_handles(
            params.get("shape_type", "rectangle"),
            params.get("center_x", 0.5),
            params.get("center_y", 0.5),
            params.get("width", 1.0),
            params.get("height", 1.0),
            params.get("rotation", 0.0),
            params.get("feather", 0.0),
            params.get("opacity", 1.0),
            tex_w, tex_h, image_ndc_rect,
        )

        # Draw bbox outline (white, semi-transparent)
        bbox_color = (1.0, 1.0, 1.0, 0.5)
        self._draw_overlay_rect(handles_data.bbox_rect, color_loc,
                                bbox_color, pos_loc)

        # Draw rotate handle connection line
        rotate_color = (0.0, 1.0, 1.0, 0.4)
        self._draw_overlay_line(handles_data.center, handles_data.rotate,
                                rotate_color, pos_loc, color_loc)

    def _draw_selection_overlays(self, img_x0, img_y0, img_x1, img_y1, tex_w, tex_h) -> None:
        """Draw selection-related overlays: hover highlights and rubber band.

        This includes:
        - Hover highlight for node output regions
        - Rubber band selection rectangle
        - Handle highlight for hovered/selected handles
        """
        if not self._overlay_prog:
            return

        _gl.glUseProgram(self._overlay_prog)
        color_loc = _gl.glGetUniformLocation(self._overlay_prog, b"u_color")
        overlay_pos_loc = _gl.glGetAttribLocation(self._overlay_prog, b"in_vert")
        _gl.glLineWidth(1.0)

        # ── Draw hovered node output region bbox ──
        if self._hovered_node_region is not None:
            region = self._hovered_node_region
            x, y, w, h = region.bbox
            ndc = self._ndc_from_pixel_rect(x, y, w, h, tex_w, tex_h)
            if ndc:
                # Cyan highlight for hovered region
                self._draw_overlay_rect(ndc, color_loc, (0.0, 1.0, 0.8, 0.6), overlay_pos_loc)

        # ── Draw selected node output region bboxes ──
        for node_id in self._selection_state.selected_node_ids:
            # Find the region for this node
            region = None
            for r in self._node_output_regions:
                if r.node_id == node_id:
                    region = r
                    break
            if region is not None:
                x, y, w, h = region.bbox
                ndc = self._ndc_from_pixel_rect(x, y, w, h, tex_w, tex_h)
                if ndc:
                    # Orange/yellow highlight for selected region
                    self._draw_overlay_rect(ndc, color_loc, (1.0, 0.7, 0.0, 0.7), overlay_pos_loc)

        # ── Draw rubber band ──
        if self._selection_state.rubber_band_active:
            self._draw_rubber_band(color_loc, overlay_pos_loc)

        # ── Draw handle highlights ──
        self._draw_handle_highlights(color_loc, overlay_pos_loc, img_x0, img_y0, img_x1, img_y1)

        # ── Draw snap lines (S5-14) ──
        self._draw_snap_lines(color_loc, overlay_pos_loc, tex_w, tex_h)

        _gl.glUseProgram(0)

    def _draw_rubber_band(self, color_loc: int, pos_loc: int) -> None:
        """Draw the rubber band selection rectangle.

        Uses screen coordinates directly (not image coordinates), since
        rubber band is drawn in the view space.
        """
        # Rubber band is drawn in screen coordinates, but overlay shader uses NDC.
        # Convert view rect to NDC.
        x0 = self._selection_state.rubber_band_start.x()
        y0 = self._selection_state.rubber_band_start.y()
        x1 = self._selection_state.rubber_band_end.x()
        y1 = self._selection_state.rubber_band_end.y()

        # Normalize to ensure x0 <= x1, y0 <= y1
        if x0 > x1:
            x0, x1 = x1, x0
        if y0 > y1:
            y0, y1 = y1, y0

        # Convert to NDC (Y flip: Qt top-left origin, NDC bottom-left origin)
        dpr = self.devicePixelRatio()
        vp_w = self.width() * dpr
        vp_h = self.height() * dpr

        ndc_x0 = (x0 * dpr) / (vp_w / 2.0) - 1.0
        ndc_x1 = (x1 * dpr) / (vp_w / 2.0) - 1.0
        ndc_y0 = 1.0 - (y0 * dpr) / (vp_h / 2.0)
        ndc_y1 = 1.0 - (y1 * dpr) / (vp_h / 2.0)

        # Draw filled rectangle with dashed outline
        vertices = np.array([
            ndc_x0, ndc_y0, 0.0, 0.0,
            ndc_x1, ndc_y0, 0.0, 0.0,
            ndc_x1, ndc_y1, 0.0, 0.0,
            ndc_x0, ndc_y1, 0.0, 0.0,
        ], dtype=np.float32)

        # Draw filled background
        _gl.glUniform4f(color_loc, ctypes.c_float(0.2), ctypes.c_float(0.4),
                        ctypes.c_float(0.8), ctypes.c_float(0.3))

        _gl.glBindBuffer(GL_ARRAY_BUFFER, self._quad_vbo)
        _gl.glBufferSubData(GL_ARRAY_BUFFER, 0, vertices.nbytes, vertices)

        _gl.glBindVertexArray(self._vao)
        _gl.glEnableVertexAttribArray(pos_loc)
        _gl.glVertexAttribPointer(pos_loc, 2, GL_FLOAT, GL_FALSE, 16, ctypes.c_void_p(0))

        # Draw as filled quad (two triangles)
        _gl.glEnable(GL_BLEND)
        _gl.glBlendFunc(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA)
        # Draw filled (4 vertices as triangle strip won't work for filled rect,
        # use LINE_LOOP for outline instead)
        _gl.glDrawArrays(GL_LINE_LOOP, 0, 4)
        _gl.glDisable(GL_BLEND)

        _gl.glBindVertexArray(0)
        _gl.glBindBuffer(GL_ARRAY_BUFFER, 0)

    def _compute_handle_size_ndc(self, handle_size_pixels: float = 10.0) -> tuple:
        """Convert screen-pixel handle size to NDC coordinates.

        Computes handle size in NDC space that corresponds to a fixed pixel size
        on screen, ensuring handles remain constant size regardless of window
        dimensions, zoom level, or DPI scaling.

        Args:
            handle_size_pixels: Desired handle size in screen pixels (default 10.0).

        Returns:
            Tuple of (ndc_size_x, ndc_size_y) for horizontal and vertical dimensions.
        """
        dpr = self.devicePixelRatio()
        vp_w = self.width() * dpr  # Physical pixel width
        vp_h = self.height() * dpr  # Physical pixel height

        # NDC range is [-1, 1], so width/2 is the scale factor
        ndc_size_x = handle_size_pixels / (vp_w / 2.0) if vp_w > 0 else 0.02
        ndc_size_y = handle_size_pixels / (vp_h / 2.0) if vp_h > 0 else 0.02

        return (ndc_size_x, ndc_size_y)

    def _draw_handle_highlights(self, color_loc: int, pos_loc: int,
                                img_x0: float, img_y0: float,
                                img_x1: float, img_y1: float) -> None:
        """Draw highlights for hovered and selected handles.

        Transform handles are SKIPPED here — their visual feedback is provided
        entirely by the glow-dot rendering in _draw_handles_from_dict().
        For non-transform handles (cornerpin, mask, roto, crop, reformat),
        a highlight square is drawn only when hovered or selected.
        """
        if not self._tool_handles:
            return

        # Identify transform handle IDs (skip them; glow dots handle their visuals)
        TRANSFORM_IDS = {"center", "pivot", "rotate"}

        # Check if there's anything to draw before setting up GL state
        has_highlight = False
        for handle_id, view_pos in self._tool_handles.items():
            if handle_id in TRANSFORM_IDS or handle_id.startswith("scale_"):
                continue  # Transform handles: skip
            is_hovered = (handle_id == self._hover_handle)
            is_selected = (handle_id in self._selection_state.selected_handles)
            if is_hovered or is_selected:
                has_highlight = True
                break

        if not has_highlight:
            return

        # Compute handle size in NDC for constant screen-pixel size (10 pixels)
        ndc_size_x, ndc_size_y = self._compute_handle_size_ndc(10.0)

        _gl.glBindBuffer(GL_ARRAY_BUFFER, self._quad_vbo)
        _gl.glBindVertexArray(self._vao)
        _gl.glEnableVertexAttribArray(pos_loc)
        _gl.glVertexAttribPointer(pos_loc, 2, GL_FLOAT, GL_FALSE, 16, ctypes.c_void_p(0))

        _gl.glEnable(GL_BLEND)
        _gl.glBlendFunc(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA)

        for handle_id, view_pos in self._tool_handles.items():
            # Skip transform handles (glow dots handle their visuals)
            if handle_id in TRANSFORM_IDS or handle_id.startswith("scale_"):
                continue

            is_hovered = (handle_id == self._hover_handle)
            is_selected = (handle_id in self._selection_state.selected_handles)

            # Only draw highlights for hovered or selected handles
            if is_selected:
                color = (1.0, 0.7, 0.0, 0.8)  # Orange for selected
            elif is_hovered:
                color = (0.0, 1.0, 0.8, 0.8)  # Cyan for hovered
            else:
                continue  # No gray background for normal handles

            # Draw a small square with constant screen-pixel size
            ndc_pos = view_pos_to_ndc(view_pos, self.width(), self.height(),
                                      self.devicePixelRatio())
            hx, hy = ndc_pos.x(), ndc_pos.y()
            handle_vertices = np.array([
                hx - ndc_size_x, hy - ndc_size_y, 0.0, 0.0,
                hx + ndc_size_x, hy - ndc_size_y, 0.0, 0.0,
                hx + ndc_size_x, hy + ndc_size_y, 0.0, 0.0,
                hx - ndc_size_x, hy + ndc_size_y, 0.0, 0.0,
            ], dtype=np.float32)

            _gl.glBufferSubData(GL_ARRAY_BUFFER, 0, handle_vertices.nbytes,
                                handle_vertices)
            _gl.glUniform4f(color_loc, ctypes.c_float(color[0]),
                            ctypes.c_float(color[1]),
                            ctypes.c_float(color[2]),
                            ctypes.c_float(color[3]))
            _gl.glDrawArrays(GL_TRIANGLE_FAN, 0, 4)

        _gl.glDisable(GL_BLEND)
        _gl.glBindVertexArray(0)
        _gl.glBindBuffer(GL_ARRAY_BUFFER, 0)

    def _draw_snap_lines(self, color_loc: int, pos_loc: int, tex_w: int, tex_h: int) -> None:
        """Draw snap guide lines when snapping is active (S5-14).

        Draws vertical/horizontal lines at snap positions when snapping is active
        during a drag operation. Lines are drawn across the entire viewport.

        Args:
            color_loc: GL uniform location for color.
            pos_loc: GL attribute location for position.
            tex_w: Texture width for coordinate conversion.
            tex_h: Texture height for coordinate conversion.
        """
        if self._snap_result is None or not self._snap_result.is_snapped():
            return

        # Snap line color: bright cyan with high visibility
        snap_color = (0.0, 1.0, 0.8, 0.8)

        # Get NDC bounds of the image
        ndc_rect = self._compute_current_image_ndc_rect()
        img_x0, img_y0, img_x1, img_y1 = ndc_rect

        # Draw vertical snap line (if X snapped)
        if self._snap_result.snapped_x and self._snap_result.snap_line_x is not None:
            # Convert snap_line_x (image pixel) to NDC
            snap_x_image = self._snap_result.snap_line_x
            snap_ndc_x = img_x0 + (img_x1 - img_x0) * (snap_x_image / tex_w)

            # Draw line from bottom to top of image
            self._draw_overlay_line(
                (snap_ndc_x, img_y0),
                (snap_ndc_x, img_y1),
                snap_color,
                pos_loc,
                color_loc
            )

        # Draw horizontal snap line (if Y snapped)
        if self._snap_result.snapped_y and self._snap_result.snap_line_y is not None:
            # Convert snap_line_y (image pixel) to NDC
            # Note: Y is top-left origin in image coords, NDC is bottom-left origin
            snap_y_image = self._snap_result.snap_line_y
            # UV.y = 1 - (y / tex_height), NDC.y = y0 + (y1 - y0) * UV.y
            snap_ndc_y = img_y0 + (img_y1 - img_y0) * (1.0 - snap_y_image / tex_h)

            # Draw line from left to right of image
            self._draw_overlay_line(
                (img_x0, snap_ndc_y),
                (img_x1, snap_ndc_y),
                snap_color,
                pos_loc,
                color_loc
            )

    def get_state(self) -> dict:
        """Return serializable viewer state for project persistence."""
        return {
            "zoom_level": self._zoom_level,
            "pan_x": self._pan_x,
            "pan_y": self._pan_y,
            "channel_mode": self._channel_mode,
            "exposure": self._exposure,
            "viewer_gamma": self._viewer_gamma,
            "viewer_gain": self._viewer_gain,
            "viewer_lift": self._viewer_lift,
            "viewer_offset": self._viewer_offset,
            "display_srgb": self._display_srgb,
            "display_transfer": self._display_transfer,
            "display_primaries": getattr(self, "_display_primaries", 0),
            "tonemap": getattr(self, "_tonemap", 0),
            "target_peak": getattr(self, "_target_peak", 100.0),
            "show_bbox_overlay": self._show_bbox_overlay,
            "show_format_overlay": self._show_format_overlay,
            # Keep legacy keys for projects saved before the viewer state
            # contract was expanded.
            "show_bbox": self._show_bbox_overlay,
            "show_format": self._show_format_overlay,
            "split_compare_mode": self._split_compare_mode,
            "split_difference_scale": self._split_difference_scale,
            # B2-1: preview resolution tier (proxy menu selection)
            "resolution_mode": self._resolution_mode,
            # B2-3: magnifier loupe state
            "magnifier_enabled": self._magnifier_enabled,
            "magnifier_zoom": self._magnifier_zoom,
            # B2-4: 2D background mode
            "background_mode": self._background_mode,
            "render_pass": self._selected_render_pass,
            "tool_mode": self._tool_mode.value,
            "selected_3d_scene_node_id": self._selected_3d_scene_node_id,
            # B1: user ROI in image coords (top-left origin, y-down)
            "user_roi": (list(self._user_roi_rect)
                         if self._user_roi_rect is not None else None),
        }

    @staticmethod
    def _restore_float(state: dict, key: str, default: float,
                       lo: Optional[float] = None,
                       hi: Optional[float] = None) -> float:
        """Validate-and-restore a float field from persisted state.

        AUD-9704: set_state consumes the project JSON verbatim (external
        data). A hand-edited or corrupt value (string, None, ...) must fall
        back to the default instead of raising TypeError/ValueError up
        through restore_viewer_state and aborting the whole load.
        """
        raw = state.get(key, default)
        try:
            value = float(raw)
        except (TypeError, ValueError):
            logger.warning(
                "CompositeViewer set_state: invalid %s=%r; using default %r",
                key, raw, default)
            return float(default)
        if lo is not None:
            value = max(lo, value)
        if hi is not None:
            value = min(hi, value)
        return value

    @staticmethod
    def _restore_int(state: dict, key: str, default: int,
                     lo: Optional[int] = None,
                     hi: Optional[int] = None) -> int:
        """Validate-and-restore an int field from persisted state (AUD-9704)."""
        raw = state.get(key, default)
        try:
            value = int(raw)
        except (TypeError, ValueError):
            logger.warning(
                "CompositeViewer set_state: invalid %s=%r; using default %r",
                key, raw, default)
            return int(default)
        if lo is not None:
            value = max(lo, value)
        if hi is not None:
            value = min(hi, value)
        return value

    def set_state(self, state: dict) -> None:
        """Restore viewer state from a persisted dict."""
        if state:
            self._zoom_level = self._restore_float(state, "zoom_level", 1.0)
            self._pan_x = self._restore_float(state, "pan_x", 0.0)
            self._pan_y = self._restore_float(state, "pan_y", 0.0)
            self.set_channel_mode(
                self._restore_int(state, "channel_mode", 0, lo=0))
            self.set_exposure(self._restore_float(state, "exposure", 0.0))
            self.set_gamma(self._restore_float(state, "viewer_gamma", 1.0))
            self.set_viewer_gain(self._restore_float(state, "viewer_gain", 1.0))
            self.set_viewer_lift(self._restore_float(state, "viewer_lift", 0.0))
            self.set_viewer_offset(self._restore_float(state, "viewer_offset", 0.0))
            self._display_srgb = bool(state.get("display_srgb", True))
            self._display_transfer = self._restore_int(
                state, "display_transfer", 1 if self._display_srgb else 0)
            self._display_primaries = self._restore_int(
                state, "display_primaries", 0)
            self._tonemap = self._restore_int(state, "tonemap", 0)
            self._target_peak = self._restore_float(
                state, "target_peak", 100.0)
            self._show_bbox_overlay = bool(state.get(
                "show_bbox_overlay", state.get("show_bbox", False)))
            self._show_format_overlay = bool(state.get(
                "show_format_overlay", state.get("show_format", False)))
            self.set_split_compare_mode(state.get("split_compare_mode", "wipe"))
            self._split_difference_scale = self._restore_float(
                state, "split_difference_scale", 4.0, lo=0.0, hi=20.0)
            # B2-1: restore the resolution tier. set_resolution_mode only
            # emits when the value actually changes, so the page applies
            # proxy_scale exactly once on load.
            if "resolution_mode" in state:
                self.set_resolution_mode(str(state.get("resolution_mode")))
            # B2-3: magnifier state (no re-eval — display-layer only).
            self._magnifier_enabled = bool(state.get("magnifier_enabled", False))
            zoom = self._restore_float(state, "magnifier_zoom", 8.0)
            self._magnifier_zoom = 8.0 if zoom >= 6.0 else 4.0
            # B2-4: background mode (validated/normalized by the setter).
            self.set_background_mode(str(state.get("background_mode", "checker")))
            selected_render_pass = str(state.get("render_pass", "beauty") or "beauty")
            if selected_render_pass in getattr(self, "_available_render_passes", {"beauty"}):
                self._selected_render_pass = selected_render_pass
            scene_node_id = state.get("selected_3d_scene_node_id")
            self._selected_3d_scene_node_id = (
                str(scene_node_id) if scene_node_id else None)
            # B1: restore persisted user ROI (image coords, y-down)
            saved_roi = state.get("user_roi")
            if isinstance(saved_roi, (list, tuple)) and len(saved_roi) == 4:
                try:
                    self._user_roi_rect = tuple(float(v) for v in saved_roi)
                except (TypeError, ValueError):
                    self._user_roi_rect = None
            else:
                self._user_roi_rect = None
            # tool_mode round-trips through get_state — restore it too.
            try:
                self._tool_mode = ViewerToolMode(
                    state.get("tool_mode", self._tool_mode.value))
            except (ValueError, TypeError):
                pass
            self.zoom_changed.emit(self._zoom_level)
            self.update()
            # A restored ROI must drive a fresh (ROI-cropped) evaluation —
            # the render pipeline has no other signal that this changed.
            if self._user_roi_rect is not None:
                bbox = self.get_user_roi_bbox()
                if bbox is not None:
                    self.user_roi_changed.emit(bbox)

    # ── Internal Helpers ────────────────────────────────────────────────────

    # AUD-9711: the `_space_pressed()` stub was removed. It could never
    # return True (Space is a key, not a keyboard modifier), so the
    # documented "Space + Left: Pan" interaction never existed; the dead
    # branches in mousePressEvent and this stub are deleted so the
    # docstrings no longer promise an interaction that doesn't happen.

    def _start_pan(self, pos) -> None:
        self._is_panning = True
        self._pan_start_pos = pos
        self._pan_start_ndc_x = self._pan_x
        self._pan_start_ndc_y = self._pan_y

    def _apply_zoom(self, factor, center_pos) -> None:
        """Zoom centered on the given widget-space position."""
        dpr = self.devicePixelRatio()
        vp_w = self.width() * dpr
        vp_h = self.height() * dpr

        if vp_w <= 0 or vp_h <= 0:
            return

        # Current zoom before this operation
        old_zoom = self._zoom_level
        new_zoom = old_zoom * factor
        new_zoom = max(0.05, min(new_zoom, 50.0))

        # Convert center to NDC (-1 to 1)
        cx_ndc = (center_pos.x() * dpr) / (vp_w / 2.0) - 1.0
        cy_ndc = 1.0 - (center_pos.y() * dpr) / (vp_h / 2.0)

        # Adjust pan so the point under cursor stays fixed
        # The image point at NDC (cx, cy) maps to:
        #   img_ndc = (cx - pan_x) / old_zoom * fit_scale
        # After zoom: new_screen = img_ndc / fit_scale * new_zoom + new_pan
        # We want: new_screen = cx → new_pan = cx - img_ndc / fit_scale * new_zoom
        # Since old_screen = img_ndc / fit_scale * old_zoom + pan_x = cx
        # → img_ndc / fit_scale = (cx - pan_x) / old_zoom
        # → new_pan = cx - (cx - pan_x) / old_zoom * new_zoom
        self._pan_x = cx_ndc - (cx_ndc - self._pan_x) * (new_zoom / old_zoom)
        self._pan_y = cy_ndc - (cy_ndc - self._pan_y) * (new_zoom / old_zoom)

        self._zoom_level = new_zoom
        self.zoom_changed.emit(new_zoom)
        self._update_tool_handles()
        self.update()

    def _compute_vertices(self, vp_w, vp_h, tex_w, tex_h):
        """Compute NDC vertices for letterbox + zoom + pan.

        Returns (x0, y0, x1, y1) in NDC space.
        """
        if tex_w <= 0 or tex_h <= 0 or vp_w <= 0 or vp_h <= 0:
            return -1.0, -1.0, 1.0, 1.0

        dpr = self.devicePixelRatio()
        left_px, top_px, right_px, bottom_px = self._content_rect_pixels(dpr)
        content_w = max(1.0, right_px - left_px)
        content_h = max(1.0, bottom_px - top_px)

        tex_aspect = tex_w / tex_h
        vp_aspect = content_w / content_h

        # Base letterbox rect in the content-safe pixel area.  The Composite
        # toolbar and playback controls are QWidget overlays, so the default
        # fit must reserve their space instead of centering under them.
        if tex_aspect > vp_aspect:
            # Texture wider — fit width, letterbox top/bottom
            draw_w = content_w
            draw_h = content_w / tex_aspect
        else:
            # Texture taller — fit height, letterbox left/right
            draw_h = content_h
            draw_w = content_h * tex_aspect
        center_x = (left_px + right_px) * 0.5
        center_y = (top_px + bottom_px) * 0.5
        px0 = center_x - draw_w * 0.5
        px1 = center_x + draw_w * 0.5
        py0 = center_y - draw_h * 0.5
        py1 = center_y + draw_h * 0.5

        bx0 = (px0 / vp_w) * 2.0 - 1.0
        bx1 = (px1 / vp_w) * 2.0 - 1.0
        by1 = 1.0 - (py0 / vp_h) * 2.0
        by0 = 1.0 - (py1 / vp_h) * 2.0

        # Apply zoom (scale around center)
        cx = (bx0 + bx1) / 2.0
        cy = (by0 + by1) / 2.0
        z = self._zoom_level
        x0 = cx + (bx0 - cx) * z
        x1 = cx + (bx1 - cx) * z
        y0 = cy + (by0 - cy) * z
        y1 = cy + (by1 - cy) * z

        # Apply pan
        x0 += self._pan_x
        x1 += self._pan_x
        y0 += self._pan_y
        y1 += self._pan_y

        return x0, y0, x1, y1

    def _content_rect_pixels(self, dpr: float) -> tuple[float, float, float, float]:
        """Return the viewport area not covered by Composite overlay controls."""
        width = max(1.0, float(self.width() * dpr))
        height = max(1.0, float(self.height() * dpr))
        top = 0.0
        bottom_reserved = 0.0
        toolbar = getattr(self, "_toolbar", None)
        if toolbar is not None and toolbar.isVisible():
            top += float(toolbar.height() * dpr)
        video_bar = getattr(self, "video_control_bar", None)
        if video_bar is not None and video_bar.isVisible():
            bottom_reserved += float(video_bar.height() * dpr)
        inspector = getattr(self, "_inspector_bar", None)
        if inspector is not None and inspector.isVisible():
            bottom_reserved += float(inspector.height() * dpr)
        bottom = height - bottom_reserved
        if bottom <= top + 8.0:
            return 0.0, 0.0, width, height
        return 0.0, top, width, bottom

    def viewer3d_content_size(self) -> tuple[int, int]:
        """Return the live 3D Viewer render size excluding overlay controls."""
        dpr = self.devicePixelRatio() if self._gl_initialized else 1.0
        left, top, right, bottom = self._content_rect_pixels(dpr)
        width = max(1, int(round(right - left)))
        height = max(1, int(round(bottom - top)))
        return width, height

    def _viewer3d_content_rect_widget(self) -> QRectF:
        """Return the 3D editor content rect in widget coordinates."""
        left, top, right, bottom = self._content_rect_pixels(1.0)
        return QRectF(left, top, max(1.0, right - left), max(1.0, bottom - top))

    def _viewer3d_project_aspect(self) -> float:
        """Return the final render aspect ratio from the project output size."""
        page = getattr(self, "_composite_page", None)
        getter = getattr(page, "_project_output_size", None)
        if callable(getter):
            try:
                width, height = getter()
                width = float(width)
                height = float(height)
                if width > 0.0 and height > 0.0:
                    return width / height
            except Exception:
                pass
        if self._tex_width > 0 and self._tex_height > 0:
            return float(self._tex_width) / float(self._tex_height)
        return 16.0 / 9.0

    def _viewer3d_render_region_rect(self, content_rect: Optional[QRectF] = None) -> QRectF:
        """Return the final project-aspect render region inside the live 3D view."""
        from src.features.composite.render3d.viewport_overlays import (
            render_region_rect)
        rect = content_rect if content_rect is not None else self._viewer3d_content_rect_widget()
        return render_region_rect(rect, self._viewer3d_project_aspect())

    def _viewer3d_overlay_settings(self) -> dict:
        """Return merged 3D viewer settings used by viewport-only overlays."""
        from src.features.composite.ui.viewer3d_settings_section import (
            merged_viewer3d_scene_settings)
        page = getattr(self, "_composite_page", None)
        getter = getattr(page, "_global_3d_scene_settings", None)
        if callable(getter):
            try:
                return getter()
            except Exception:
                pass
        return merged_viewer3d_scene_settings()

    def _viewport_bg_color_rgb(self) -> tuple:
        """视口 clear color（letterbox/未填满区底色），来自全局 3D Scene Settings。

        复用 _viewer3d_overlay_settings 的合并逻辑；失败时退回原硬编码 0.15 灰。
        """
        try:
            settings = self._viewer3d_overlay_settings()
            r = float(settings.get("viewport_bg_r", 0.15))
            g = float(settings.get("viewport_bg_g", 0.15))
            b = float(settings.get("viewport_bg_b", 0.15))
            return (max(0.0, min(1.0, r)),
                    max(0.0, min(1.0, g)),
                    max(0.0, min(1.0, b)))
        except Exception:
            return (0.15, 0.15, 0.15)

    def _emit_viewer3d_content_size_changed(self) -> None:
        """Notify the page when the live 3D render target should be recreated."""
        if self._view_mode != "3d":
            return
        size = self.viewer3d_content_size()
        if size == self._last_viewer3d_content_size:
            return
        self._last_viewer3d_content_size = size
        self.viewer3d_content_size_changed.emit(size[0], size[1])

    def _draw_checkerboard(self, x0, y0, x1, y1) -> None:
        """Draw checkerboard pattern behind the image region."""
        # Full-screen quad in the image region with checkerboard texture
        vertices = np.array([
            x0, y1, 0.0, 1.0,
            x0, y0, 0.0, 0.0,
            x1, y1, 1.0, 1.0,
            x1, y0, 1.0, 0.0,
        ], dtype=np.float32)

        _gl.glBindBuffer(GL_ARRAY_BUFFER, self._quad_vbo)
        _gl.glBufferSubData(GL_ARRAY_BUFFER, 0, vertices.nbytes, vertices)
        _gl.glBindBuffer(GL_ARRAY_BUFFER, 0)

        _gl.glUseProgram(self._checker_prog)
        _gl.glActiveTexture(GL_TEXTURE0)
        _gl.glBindTexture(GL_TEXTURE_2D, self._checker_tex_id)

        tex_loc = _gl.glGetUniformLocation(self._checker_prog, b"u_checker")
        _gl.glUniform1i(tex_loc, 0)

        # Compute UV scale to tile the checkerboard at a reasonable pixel size
        # Each checker square = 8 pixels at 1:1 zoom, tile based on image size
        tw, th = self._tex_width, self._tex_height
        if tw <= 0 or th <= 0:
            tw, th = 1, 1
        checker_size = 16  # pixels per checker square
        scale_x = tw / checker_size
        scale_y = th / checker_size
        scale_loc = _gl.glGetUniformLocation(self._checker_prog, b"u_scale")
        _gl.glUniform2f(scale_loc, scale_x, scale_y)

        _gl.glBindVertexArray(self._vao)
        _gl.glDrawArrays(GL_TRIANGLE_STRIP, 0, 4)
        _gl.glBindVertexArray(0)

        _gl.glBindTexture(GL_TEXTURE_2D, 0)
        _gl.glUseProgram(0)

    def _cleanup_partial_init(self) -> None:
        """Release GL objects created during a failed initializeGL()."""
        if self._quad_vbo:
            vbo = ctypes.c_uint(self._quad_vbo)
            _gl.glDeleteBuffers(1, ctypes.byref(vbo))
            self._quad_vbo = None
        if self._vao:
            vao = ctypes.c_uint(self._vao)
            _gl.glDeleteVertexArrays(1, ctypes.byref(vao))
            self._vao = None
        if self._prog:
            _gl.glDeleteProgram(self._prog)
            self._prog = None
        if self._checker_prog:
            _gl.glDeleteProgram(self._checker_prog)
            self._checker_prog = None
        if self._overlay_prog:
            _gl.glDeleteProgram(self._overlay_prog)
            self._overlay_prog = None
        if self._checker_tex_id:
            tex = ctypes.c_uint(self._checker_tex_id)
            _gl.glDeleteTextures(1, ctypes.byref(tex))
            self._checker_tex_id = None
        self._gl_initialized = False

    def _init_checkerboard(self) -> None:
        """Generate a 16x16 checkerboard texture."""
        size = 16
        data = np.zeros((size, size, 4), dtype=np.uint8)
        light = np.array([200, 200, 200, 255], dtype=np.uint8)
        dark = np.array([140, 140, 140, 255], dtype=np.uint8)
        half = size // 2
        # Top-left quadrant
        data[:half, :half] = light
        data[:half, half:] = dark
        data[half:, :half] = dark
        data[half:, half:] = light

        tex = ctypes.c_uint()
        _gl.glGenTextures(1, ctypes.byref(tex))
        self._checker_tex_id = tex.value
        _gl.glBindTexture(GL_TEXTURE_2D, self._checker_tex_id)
        _gl.glTexImage2D(
            GL_TEXTURE_2D, 0, GL_RGBA,
            size, size, 0,
            GL_RGBA, GL_UNSIGNED_BYTE, data,
        )
        _gl.glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR)
        _gl.glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR)
        _gl.glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_REPEAT)
        _gl.glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_REPEAT)
        _gl.glBindTexture(GL_TEXTURE_2D, 0)

    @staticmethod
    def _query_texture_size(tex_id):
        """Query actual GL texture dimensions."""
        if tex_id is None or tex_id <= 0:
            return 0, 0
        try:
            w = ctypes.c_int()
            h = ctypes.c_int()
            _gl.glBindTexture(GL_TEXTURE_2D, tex_id)
            _gl.glGetTexLevelParameteriv(GL_TEXTURE_2D, 0, GL_TEXTURE_WIDTH, ctypes.byref(w))
            _gl.glGetTexLevelParameteriv(GL_TEXTURE_2D, 0, GL_TEXTURE_HEIGHT, ctypes.byref(h))
            _gl.glBindTexture(GL_TEXTURE_2D, 0)
            return w.value, h.value
        except Exception:
            return 0, 0

    def _probe_context_sharing(self, renderer_tex_id: int) -> bool:
        """Probe whether the renderer's texture ID is accessible from this context.

        Strategy:
        1. Try GLContextHub's centralized probe (if contexts are registered)
        2. Fallback: query texture size from viewer's GL context directly
        Only probes once, then caches the result.
        """
        if self._sharing_probe_attempted:
            return self._sharing_probe_passed or False

        self._sharing_probe_attempted = True

        if not self._gl_initialized or renderer_tex_id <= 0:
            self._sharing_probe_passed = False
            return False

        # Try GLContextHub first (centralized, cached, 2x2 probe)
        try:
            from src.shared.gpu.gl_context_hub import GLContextHub
            hub = GLContextHub.instance()
            # Register viewer context
            try:
                self.makeCurrent()
                hub.register_context("composite_viewer", self.context())
            except Exception:
                pass
            result = hub.probe_sharing("composite_renderer", "composite_viewer")
            if result:
                self._sharing_probe_passed = True
                logger.info(
                    "[COMPOSITE-CONTEXT-SHARING] GLContextHub probe PASSED"
                )
                return True
        except Exception:
            pass

        # Fallback: direct size query
        try:
            self.makeCurrent()
            w, h = self._query_texture_size(renderer_tex_id)
            self.doneCurrent()

            if w > 0 and h > 0:
                self._sharing_probe_passed = True
                logger.info(
                    f"[COMPOSITE-CONTEXT-SHARING] probe PASSED: "
                    f"renderer tex {renderer_tex_id} readable from viewer context "
                    f"({w}x{h})"
                )
                return True
            else:
                self._sharing_probe_passed = False
                logger.info(
                    "[COMPOSITE-CONTEXT-SHARING] probe FAILED: "
                    "renderer texture not readable from viewer context, "
                    "using readback fallback"
                )
                return False
        except Exception as e:
            self._sharing_probe_passed = False
            logger.warning(f"[COMPOSITE-CONTEXT-SHARING] probe error: {e}")
            return False

    @staticmethod
    def _compile_program(vert_src, frag_src):
        """Compile a GLSL program from vertex and fragment shader source."""
        def _check_shader(shader_id, label):
            status = ctypes.c_int()
            _gl.glGetShaderiv(shader_id, GL_COMPILE_STATUS, ctypes.byref(status))
            if status.value != GL_TRUE:
                log_len = ctypes.c_int()
                _gl.glGetShaderiv(shader_id, GL_INFO_LOG_LENGTH, ctypes.byref(log_len))
                try:
                    log = _gl.glGetShaderInfoLog(shader_id)
                    if isinstance(log, bytes):
                        log_text = log.decode('utf-8', errors='replace')
                    else:
                        log_text = str(log)
                except Exception as e:
                    log_text = f"<failed to read shader log: {e}>"
                logger.error(f"{label} compile error: {log_text}")
                return False
            return True

        vs = _gl.glCreateShader(GL_VERTEX_SHADER)
        _gl.glShaderSource(vs, [vert_src])
        _gl.glCompileShader(vs)

        fs = _gl.glCreateShader(GL_FRAGMENT_SHADER)
        _gl.glShaderSource(fs, [frag_src])
        _gl.glCompileShader(fs)

        if not _check_shader(vs, "Vertex shader") or not _check_shader(fs, "Fragment shader"):
            _gl.glDeleteShader(vs)
            _gl.glDeleteShader(fs)
            return 0

        prog = _gl.glCreateProgram()
        _gl.glAttachShader(prog, vs)
        _gl.glAttachShader(prog, fs)
        _gl.glLinkProgram(prog)

        # Check link status
        link_status = ctypes.c_int()
        _gl.glGetProgramiv(prog, GL_LINK_STATUS, ctypes.byref(link_status))
        if link_status.value != GL_TRUE:
            log_len = ctypes.c_int()
            _gl.glGetProgramiv(prog, GL_INFO_LOG_LENGTH, ctypes.byref(log_len))
            try:
                log = _gl.glGetProgramInfoLog(prog)
                if isinstance(log, bytes):
                    log_text = log.decode('utf-8', errors='replace')
                else:
                    log_text = str(log)
            except Exception as e:
                log_text = f"<failed to read program log: {e}>"
            logger.error(f"Program link error: {log_text}")
            _gl.glDeleteProgram(prog)
            _gl.glDeleteShader(vs)
            _gl.glDeleteShader(fs)
            return 0

        _gl.glDeleteShader(vs)
        _gl.glDeleteShader(fs)

        return prog
