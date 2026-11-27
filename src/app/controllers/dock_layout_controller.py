from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Literal

from PyQt6.QtCore import Qt, QByteArray, QRect, QSignalBlocker, QTimer
from PyQt6.QtGui import QAction, QFontMetrics
from PyQt6.QtWidgets import (QDockWidget, QFrame, QHBoxLayout, QInputDialog, QLabel, QMainWindow,
                             QListView, QMenu, QMessageBox, QScrollArea, QSizePolicy, QTabWidget,
                             QTabBar, QToolBar, QToolButton, QVBoxLayout, QWidget, QApplication)

from src.app.ui.edit_workspace_layout import EditDockResizeHandles
from src.app.ui.photo_color_layout import PhotoColorResizeHandles, front_dock
from src.app.ui.video_color_layout import (
    VIDEO_COLOR_PRIMARY, VIDEO_COLOR_SCOPES, VIDEO_COLOR_SECONDARY, VideoColorResizeHandles,
)
from src.features.color.ui.panel_chrome import (
    COLOR_TOOL_DOCK_DEFAULT_HEIGHT, COLOR_TOOL_DOCK_MAX_HEIGHT, ColorToolDockWidget,
)
from src.shared.i18n.i18n import I18N
from src.shared.layout import TIMELINE_DOCK_DEFAULT_HEIGHT, TIMELINE_DOCK_MIN_HEIGHT
from src.shared.layout.store import LayoutStore
from src.shared.layout.profile import LayoutProfile
from src.shared.panels.registry import get_panel_registry
from src.shared.ui.style_sheet import DARK_THEME
from src.shared.ui.dock_context_menu import add_dock_management_actions
from src.shared.ui.panel_divider import PanelDockWidget
from src.shared.ui.dock_tab_style import DOCK_TAB_BAR_HEIGHT, apply_dock_tab_bar_style
from src.shared.ui.widgets.compact_scrollbar import CompactVerticalScrollBar

if TYPE_CHECKING:
    from src.features.edit.core.workspace_presets import WorkspacePreset

logger = logging.getLogger(__name__)

_VIEWPORT_MIN_WIDTH = 640
_VIEWPORT_MIN_HEIGHT = 360
_PHOTO_VIEWPORT_MIN_SIZE = (400, 250)
_PHOTO_COLOR_DEFAULTS = {"top": (18, 64, 18), "bottom": (41, 39, 20)}
_PHOTO_COLOR_DUAL_TOOL_WIDTH = 1380
_PHOTO_COLOR_COMPACT_TOP_WIDTH = 1200
_PHOTO_COLOR_STACKED_DETAILS_HEIGHT = 360
_PHOTO_COLOR_STACKED_COMPACT_WINDOW_HEIGHT = 790
_PHOTO_COLOR_STACKED_COMPACT_TOOL_HEIGHT = 430
_PHOTO_COLOR_SIDEBARS_KEY = "photo_color_sidebar_shares"
_DockStacks = tuple[tuple[QDockWidget, ...], ...]

_EDIT_VIEWPORT_PREFERENCES_KEY = "edit_viewport_preferences"

_VIDEO_COLOR_LAYOUT_VERSION = 7
_PREVIOUS_VIDEO_COLOR_LAYOUT_VERSION = 6
_VIDEO_COLOR_LAYOUT_KEY = "video_color_layout_version"
_VIDEO_COLOR_DUAL_TOOL_WIDTH = 1330
_VIDEO_COLOR_COMPACT_CLIP_HEIGHT = 760
_VIDEO_COLOR_SECONDARY_DOCKS = (*VIDEO_COLOR_SECONDARY, "dock_mixer")
_VIDEO_COLOR_GALLERY = ("dock_presets", "dock_lut", "dock_plugin", "dock_grade_sets")
_VIDEO_COLOR_TAB_DOCKS = (
    *_VIDEO_COLOR_GALLERY, *VIDEO_COLOR_PRIMARY, *_VIDEO_COLOR_SECONDARY_DOCKS, *VIDEO_COLOR_SCOPES[1:],
)
_VIDEO_PROJECT_SCREEN_TIERS = (
    ("window_4k", 3840, 2160),
    ("window_2_5k", 2560, 1440),
    ("window_2k", 2048, 1080),
    ("window_compact", 0, 0),
)
_VIDEO_COLOR_DEFAULTS = {
    "window_4k": (1720, 950),
    "window_2_5k": (1520, 880),
    "window_2k": (1360, 800),
    "window_compact": (1200, 740),
    "top": (21, 57, 22),
    "bottom": (39, 39, 22),
    "height": (56, 44),
}

_VIDEO_EDIT_DEFAULTS = {
    "window_4k": (3, 4, 3, 50, 50),
    "window_2_5k": (3, 4, 3, 51, 49),
    "window_2k": (3, 4, 3, 52, 48),
    "window_compact": (3, 4, 3, 54, 46),
}


# Versioned one-shot repair for a persisted Edit layout from builds that let
# the Properties dock claim most of the horizontal workspace.  This is a
# migration, not an ongoing constraint: once applied, subsequent splitter
# changes remain entirely under the user's control.
_EDIT_PROPERTIES_DOCK_LAYOUT_MIGRATION_KEY = "edit_properties_dock_layout_version"
_EDIT_PROPERTIES_DOCK_LAYOUT_MIGRATION_VERSION = 1
_EDIT_PROPERTIES_DOCK_TARGET_WIDTH = 320
_EDIT_INFO_DOCK_LAYOUT_MIGRATION_KEY = "edit_info_dock_layout_version"
_EDIT_INFO_DOCK_LAYOUT_MIGRATION_VERSION = 1
_AI_ASSISTANT_DOCK_LAYOUT_MIGRATION_KEY = "ai_assistant_dock_layout_version"
_AI_ASSISTANT_DOCK_LAYOUT_MIGRATION_VERSION = 1
_EDIT_TIMELINE_DOCK_LAYOUT_MIGRATION_KEY = "edit_timeline_dock_layout_version"
# APP-03：v3 增加"过矮"修复——小窗口/恢复布局下时间线 dock 可能只有
# ~102px，工具条+尺占满后 V/A 轨几乎不可编辑。低于可操作下限时一次性
# 拉回默认高度；用户此后手动调矮不被再次覆盖（one-shot + 版本门槛）。
_EDIT_TIMELINE_DOCK_LAYOUT_MIGRATION_VERSION = 3
_EDIT_TIMELINE_DOCK_MIN_HEIGHT = 160
_EDIT_TIMELINE_DOCK_MAX_HEIGHT = 480
_EDIT_TIMELINE_DOCK_MIGRATION_DELAY_MS = 50
_COLOR_GRADING_DOCK_LAYOUT_MIGRATION_KEY = "color_grading_dock_layout_version"
_COLOR_GRADING_DOCK_LAYOUT_MIGRATION_VERSION = 3
COLOR_TOOL_LAYOUT_RESTORE_DELAY_MS = 450
_BASE_GRADE_DOCK_NAME = "dockBaseGrade"
_CHROMOGEN_DOCK_NAME = "dockChromogen"
_COLOR_TOOL_DOCK_NAMES = frozenset({
    "dockDenoise", "dockDeblur", "dockGrading", _BASE_GRADE_DOCK_NAME,
    _CHROMOGEN_DOCK_NAME, "dockCurves",
    "dockColorMapping", "dockMixer", "dockBlurSharpen", "dockGrain", "dockMasks",
    "dockQualifier", "dockRaw", "dockAnalysis", "dockCrop",
    "dockTracker", "dockColorKeyframes",
})
_COLOR_REMAPPING_DOCK_LAYOUT_MIGRATION_KEY = "color_mapping_dock_layout_version"
_COLOR_REMAPPING_DOCK_LAYOUT_MIGRATION_VERSION = 2
_COLOR_KEYFRAMES_DOCK_PLACEMENT_KEY = "color_keyframes_dock_placed"
_COMPOSITE_SCOPES_DOCK_LAYOUT_MIGRATION_KEY = "composite_scopes_dock_layout_version"
_COMPOSITE_SCOPES_DOCK_LAYOUT_MIGRATION_VERSION = 1
_COMPOSITE_DEFAULT_LAYOUT_VERSION = 1
_MIXER_WORKSPACE_LAYOUT_VERSION = 3
_VIDEO_MIX_WORKSPACE_LAYOUT_VERSION = 3
_MIXER_DOCK_TAB_HEIGHT = 26

# 浮动 dock 标题栏高度（与原生窗口标题栏接近）
_FLOATING_TITLEBAR_HEIGHT = 28


def _is_video_color(window: QMainWindow, page: str) -> bool:
    return page == "Color" and getattr(window, "_active_work_mode", None) == "video"


def save_workspace_dock_state(window: QMainWindow, page: str) -> QByteArray:
    if _is_video_color(window, page):
        return window.saveState(_VIDEO_COLOR_LAYOUT_VERSION)
    return window.saveState()


def _qt_dock_state_version(state: QByteArray) -> int:
    # QMainWindow saves a 32-bit marker followed by the caller's version.
    return int.from_bytes(bytes(state[4:8]), "big") if bytes(state[:4]) == b"\x00\x00\x00\xff" else -1


def _is_supported_video_color_state(state: QByteArray) -> bool:
    return _qt_dock_state_version(state) in (_PREVIOUS_VIDEO_COLOR_LAYOUT_VERSION, _VIDEO_COLOR_LAYOUT_VERSION)


def restore_workspace_dock_state(window: QMainWindow, state: QByteArray, page: str) -> bool:
    """Restore Qt geometry and place new grade tools in older Color layouts."""
    if _is_video_color(window, page):
        if not _is_supported_video_color_state(state):
            return False
        version = _qt_dock_state_version(state)
        if not window.restoreState(state, version):
            return False
        if version == _PREVIOUS_VIDEO_COLOR_LAYOUT_VERSION:
            window._upgrade_video_color_scopes_tabs()
        return True
    if not window.restoreState(state):
        return False
    if page != "Color":
        return True
    snapshot = bytes(state)
    _place_missing_grade_dock(window, snapshot, _BASE_GRADE_DOCK_NAME, "dock_base_grade",
                              ("dock_grading", "dock_raw"))
    _place_missing_grade_dock(window, snapshot, _CHROMOGEN_DOCK_NAME, "dock_chromogen",
                              ("dock_base_grade", "dock_grading", "dock_raw"))
    return True


def _place_missing_grade_dock(
    window: QMainWindow, snapshot: bytes, object_name: str, dock_attr: str,
    anchor_attrs: tuple[str, ...],
) -> None:
    dock = getattr(window, dock_attr, None)
    # Qt saves object names as big-endian QStrings. Only insert the dock if
    # the snapshot predates it, leaving a saved close or float state intact.
    if dock is None or object_name.encode("utf-16-be") in snapshot:
        return
    dock.hide()
    dock.setFloating(False)
    for anchor_attr in anchor_attrs:
        anchor = getattr(window, anchor_attr, None)
        if (anchor is None or anchor.isFloating()
                or window.dockWidgetArea(anchor) == Qt.DockWidgetArea.NoDockWidgetArea):
            continue
        if dock not in window.tabifiedDockWidgets(anchor):
            window.tabifyDockWidget(anchor, dock)
        break
    else:
        window.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, dock)
    dock.show()


class FloatingDockTitleBar(QWidget):
    """浮动 QDockWidget 使用的自定义标题栏。

    背景：项目全局 QSS 把 ``QDockWidget::title`` 的 ``max-height`` 设为 0px
    （停靠态靠自定义 1px widget 占位，标题栏由 tab 承载）。这导致 dock 浮动后，
    若依赖 ``setTitleBarWidget(None)`` 切回原生标题栏，浮动窗口顶部没有任何
    可抓取的拖拽区域——既无法移动浮动窗口，也无法拖到边缘重新停靠。

    本类提供一个有真实高度的标题栏：拖动它即可移动浮动窗口并触发边缘停靠
    判定（QDockWidget 会接管自定义 titlebar 上的鼠标拖拽）。
    """

    def __init__(self, dock: QDockWidget) -> None:
        super().__init__(dock)
        self._dock = dock
        self.setFixedHeight(_FLOATING_TITLEBAR_HEIGHT)
        self.setAutoFillBackground(True)
        # 固定背景色（与暗色主题面板色一致），底部细线与内容区分隔
        self.setStyleSheet(
            "FloatingDockTitleBar { background: #1f1f23; border-bottom: 1px solid #111; }"
        )

        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 0, 4, 0)
        layout.setSpacing(4)

        self._title_label = QLabel(dock.windowTitle())
        self._title_label.setStyleSheet("color: #d0d0d0; background: transparent;")
        layout.addWidget(self._title_label)
        layout.addStretch(1)

        if dock.objectName() != "dockAudioClipEditor":
            close_btn = QToolButton(self)
            close_btn.setText("×")
            close_btn.setFixedSize(20, 20)
            close_btn.setCursor(Qt.CursorShape.ArrowCursor)
            close_btn.setStyleSheet(
                "QToolButton { color: #999; border: none; background: transparent; }"
                "QToolButton:hover { color: #fff; background: #c42b1c; }"
            )
            close_btn.clicked.connect(self._close_dock)
            layout.addWidget(close_btn)

        # 标题随 dock 窗口标题同步
        dock.windowTitleChanged.connect(self._title_label.setText)

    def _close_dock(self) -> None:
        action = self._dock.property("toposPanelCloseAction")
        if isinstance(action, QAction) and action.isChecked() and action.isEnabled():
            action.trigger()
        else:
            self._dock.hide()


class DockLayoutMixin:
    """Mixin providing dock layout management for MainWindow.

    Extracted methods:
    - setup_docks
    - initialize_page_layouts
    - update_all_dock_titlebars
    - _on_dock_floating_changed
    - _show_dock_context_menu
    - _perform_update_all_titlebars
    - apply_default_layout
    - _get_all_page_layouts
    - _apply_all_page_layouts
    - save_layout_dialog
    - delete_layout_dialog
    - update_layout_combobox
    - on_layout_combo_activated
    - update_load_layout_menu
    - apply_layout
    - reset_layout_to_default
    - set_current_layout_as_default
    - restore_layout_state
    - _migrate_ai_assistant_dock_layout
    - _migrate_edit_timeline_dock_height
    - _migrate_color_grading_dock_height
    - _migrate_composite_scopes_dock_visibility
    - _enforce_page_dock_visibility
    - _save_all_docked_sizes
    - _restore_docked_sizes
    - _sync_floating_docks
    """

    @staticmethod
    def _ensure_window_on_screen(window) -> None:
        """Clamp a restored window onto the screen holding its center.

        A geometry saved under a different monitor setup or DPI scale can be
        larger than the current screen or sit past its edges.  A center-only
        check would leave such a window sprawling off-screen with an
        unreachable title bar, so both size and position are clamped to that
        screen's available geometry (primary screen as fallback).

        M13.6：拔屏判定先经 ``dpi_restore_geometry``（可视比 <25% 视为
        屏失效——中心点判定漏掉"中心在屏但大半溢出失效屏"的场景）；
        模型层纯函数见 core/multicam_adaptive.py。
        """
        screens = QApplication.screens()
        if not screens:
            return
        geom = window.geometry()
        # M13.6：可视比域判定（屏失效 → 回退首个存活屏 + 钳制）
        try:
            from src.features.edit.core.multicam_adaptive import (
                dpi_restore_geometry,
            )
            restored = dpi_restore_geometry(
                (geom.x(), geom.y(), geom.width(), geom.height()),
                [tuple(s.geometry().getRect()) for s in screens])
            if restored != (geom.x(), geom.y(), geom.width(),
                            geom.height()):
                window.setGeometry(*restored)
                geom = window.geometry()
        except Exception:  # noqa: BLE001 — DPI 判定失败不阻断恢复
            logger.debug("dpi_restore_geometry unavailable", exc_info=True)
        target = None
        for screen in screens:
            if screen.geometry().contains(geom.center()):
                target = screen
                break
        if target is None:
            target = QApplication.primaryScreen()
        if target is None:
            return
        avail = target.availableGeometry()
        width = min(geom.width(), avail.width())
        height = min(geom.height(), avail.height())
        x = max(avail.left(), min(geom.x(), avail.right() - width))
        y = max(avail.top(), min(geom.y(), avail.bottom() - height))
        if (width, height) != (geom.width(), geom.height()):
            window.resize(width, height)
        if (x, y) != (geom.x(), geom.y()):
            window.move(x, y)

    # ------------------------------------------------------------------
    # setup_docks
    # ------------------------------------------------------------------
    def setup_docks(self):
        # Configure corners so the bottom area spans the full width (under left/right)
        self.setCorner(Qt.Corner.BottomLeftCorner, Qt.DockWidgetArea.BottomDockWidgetArea)
        self.setCorner(Qt.Corner.BottomRightCorner, Qt.DockWidgetArea.BottomDockWidgetArea)
        # Let TopDockWidgetArea (viewport) occupy full width at top, not competing with Left/Right
        self.setCorner(Qt.Corner.TopLeftCorner, Qt.DockWidgetArea.TopDockWidgetArea)
        self.setCorner(Qt.Corner.TopRightCorner, Qt.DockWidgetArea.TopDockWidgetArea)

        # Unified Tab position for all areas as North
        for area in [Qt.DockWidgetArea.BottomDockWidgetArea, Qt.DockWidgetArea.LeftDockWidgetArea,
                     Qt.DockWidgetArea.RightDockWidgetArea, Qt.DockWidgetArea.TopDockWidgetArea]:
            self.setTabPosition(area, QTabWidget.TabPosition.North)

        # Helper to create docks
        def create_custom_dock(title, widget, obj_name):
            dock_type = ColorToolDockWidget if obj_name in _COLOR_TOOL_DOCK_NAMES else PanelDockWidget
            dock = dock_type(title, self)
            dock.setObjectName(obj_name)
            dock.setWidget(widget)
            # Ensure dock has all features enabled for proper floating/docking
            dock.setFeatures(
                QDockWidget.DockWidgetFeature.DockWidgetClosable |
                QDockWidget.DockWidgetFeature.DockWidgetMovable |
                QDockWidget.DockWidgetFeature.DockWidgetFloatable
            )
            # Hide the title bar by default for tabified docks (show only tabs)
            # Use a minimal height widget to allow dragging (important for floating)
            hidden_titlebar = QWidget()
            hidden_titlebar.setFixedHeight(1)  # 1px is enough for dragging
            dock.setTitleBarWidget(hidden_titlebar)
            dock.refresh_divider_overlay()

            # Right-click context menu on the dock widget
            dock.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            dock.customContextMenuRequested.connect(
                lambda pos, d=dock: self._show_dock_context_menu(pos, d)
            )

            # More robust triggers for titlebar updates
            dock.dockLocationChanged.connect(self.update_all_dock_titlebars)
            # When floating changes, immediately handle titlebar for floating docks
            dock.topLevelChanged.connect(lambda is_floating, d=dock: self._on_dock_floating_changed(is_floating, d))
            dock.visibilityChanged.connect(self.update_all_dock_titlebars)
            return dock

        # Viewport container with menu bar
        viewport_container = QWidget()
        viewport_container.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)
        viewport_container.setAutoFillBackground(False)
        # Set size policy to prevent viewport from being squeezed by left/right panels
        viewport_container.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        viewport_container.setMinimumSize(
            _VIEWPORT_MIN_WIDTH, _VIEWPORT_MIN_HEIGHT
        )
        viewport_layout = QVBoxLayout(viewport_container)
        viewport_layout.setContentsMargins(0, 0, 0, 0)
        viewport_layout.setSpacing(0)
        viewport_layout.addWidget(self.canvas.viewport_menu)
        viewport_layout.addWidget(self.canvas)
        self.viewport_container = viewport_container  # saved for widget swapping

        self.dock_viewport = create_custom_dock(I18N.tr("panel.viewport", "Viewport"), viewport_container, "dockViewport")
        # Set minimum size on the dock widget itself to prevent being squeezed
        self.dock_viewport.setMinimumWidth(_VIEWPORT_MIN_WIDTH)
        self.dock_viewport.setMinimumHeight(_VIEWPORT_MIN_HEIGHT)
        self.addDockWidget(Qt.DockWidgetArea.TopDockWidgetArea, self.dock_viewport)
        self.setCentralWidget(QWidget())
        self.centralWidget().hide()

        # Library Panel - Direct dock without TabWidget
        self.dock_library = create_custom_dock(I18N.tr("panel.library", "Library"), self.library_panel, "dockLibrary")
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, self.dock_library)

        # Files Panel - Independent Dock (can be freely dragged and docked)
        self.dock_files = create_custom_dock(I18N.tr("panel.files", "Files"), self.files_panel, "dockFiles")
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, self.dock_files)

        self.dock_info = create_custom_dock(I18N.tr("panel.info", "Info"), self.info_panel, "dockInfo")
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.dock_info)

        # Keep the serialized dock identity and each workflow's own layout.
        # Image users edit the real layer stack; video keeps its node editor.
        from src.shared.feature_flags import uses_image_layer_workflow
        image_layers = uses_image_layer_workflow(getattr(self, "config_manager", None))
        stack_widget = self.layer_panel if image_layers else self.color_node_panel
        stack_title = I18N.tr("panel.layers", "Layers") if image_layers else I18N.tr("color_nodes.nodes", "Nodes")
        self.dock_layers = create_custom_dock(stack_title, stack_widget, "dockLayers")
        stack_widget.show()
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.dock_layers)
        if self._current_work_mode() != "video":
            self.color_node_panel.view.toggle_display_mode = self._toggle_color_node_display_mode

        # Tracker 面板：按达芬奇曲线窗口提供足够横向空间，默认隐藏
        self.dock_tracker = create_custom_dock(I18N.tr("panel.tracker", "Tracker"), self.tracker_panel, "dockTracker")
        self.dock_tracker.setMinimumWidth(320)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.dock_tracker)
        self.resizeDocks([self.dock_tracker], [520], Qt.Orientation.Horizontal)
        self.dock_tracker.setVisible(False)
        # 达芬奇行为：切换到跟踪器时自动识别 Mask 当前激活的窗口，
        # 而不是停留在最后一次信号推送的形状
        self.dock_tracker.visibilityChanged.connect(self._on_tracker_dock_shown)

        # Independent Tool Docks
        self.dock_denoise = create_custom_dock(I18N.tr("panel.denoise", "Denoise"), self.denoise_panel, "dockDenoise")
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self.dock_denoise)

        self.dock_deblur = create_custom_dock(I18N.tr("panel.deblur", "Deblur"), self.deblur_panel, "dockDeblur")
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self.dock_deblur)

        self.dock_grading = create_custom_dock(I18N.tr("panel.color_wheel", "Color Wheel"), self.grading_panel, "dockGrading")
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self.dock_grading)
        base_grade = self.grading_panel.base_grade_panel
        self.dock_base_grade = create_custom_dock(
            I18N.tr("base_grade.title", "Tonal Grade"), base_grade, _BASE_GRADE_DOCK_NAME
        )
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self.dock_base_grade)
        self.dock_base_grade.visibilityChanged.connect(
            lambda visible: base_grade.finish_edit(commit=True) if not visible else None
        )
        self.dock_base_grade.hide()

        chromogen = self.grading_panel.chromogen_panel
        self.dock_chromogen = create_custom_dock(
            I18N.tr("chromogen.title", "Look Dev"), chromogen, _CHROMOGEN_DOCK_NAME
        )
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self.dock_chromogen)
        self.dock_chromogen.visibilityChanged.connect(
            lambda visible: chromogen.finish_edit(commit=True) if not visible else None
        )
        self.dock_chromogen.hide()

        self.dock_curves = create_custom_dock(I18N.tr("panel.curves", "Curves"), self.curves_panel, "dockCurves")
        self.dock_curves.setMinimumWidth(420)
        self.dock_curves.setMaximumWidth(1800)
        self.dock_curves.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred
        )
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self.dock_curves)
        self.dock_curves.visibilityChanged.connect(self._on_curves_scope_visibility_changed)

        self.dock_warper = create_custom_dock(
            I18N.tr("panel.warper", "Warper"),
            self.warper_panel,
            # Retain the serialized identity so saved Color Mapping layouts
            # restore Warper's exact tab position, hidden/floating state and
            # geometry.
            "dockColorMapping",
        )
        set_dock_tool_visible = getattr(self.warper_panel, "set_dock_tool_visible", None)
        if callable(set_dock_tool_visible):
            self.dock_warper.visibilityChanged.connect(set_dock_tool_visible)
        self.addDockWidget(
            Qt.DockWidgetArea.BottomDockWidgetArea,
            self.dock_warper,
        )
        self.dock_mixer = create_custom_dock(
            I18N.tr("panel.mixer", "Mixer"), self.mixer_panel, "dockMixer"
        )
        self.dock_mixer.toggleViewAction().triggered.connect(
            lambda checked: self.dock_mixer.raise_() if checked else None,
        )
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self.dock_mixer)

        self.dock_audio_mixer = create_custom_dock(I18N.tr("panel.audio_mixer", "Audio Mixer"), self.audio_mixer_panel, "dockAudioMixer")
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self.dock_audio_mixer)

        self.dock_effects = create_custom_dock(I18N.tr("panel.blur_sharpen", "Blur/Sharpen"), self.effects_panel, "dockBlurSharpen")
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self.dock_effects)

        self.dock_grain = create_custom_dock(I18N.tr("panel.film_grain", "Film Grain"), self.film_grain_panel, "dockGrain")
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self.dock_grain)

        self.dock_masks = create_custom_dock(I18N.tr("panel.masks", "Masks"), self.mask_panel, "dockMasks")
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self.dock_masks)
        # Task 3: 在 Mask 面板隐藏时重置工具状态，防止残留绘制状态
        self.dock_masks.visibilityChanged.connect(self._on_masks_panel_visibility_changed)

        self.dock_qualifier = create_custom_dock(I18N.tr("panel.qualifier", "Qualifier"), self.qualifier_panel, "dockQualifier")
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self.dock_qualifier)
        self.dock_qualifier.visibilityChanged.connect(lambda v: self.qualifier_panel.set_picker_active(False) if not v else None)

        if self._current_work_mode() == "video":
            # The LUT grid scrolls, so its extra height need not constrain the whole viewer row.
            self.color_lut_browser.setMinimumHeight(210)
        self.dock_lut = create_custom_dock(I18N.tr("panel.lut", "3D LUT"), self.color_lut_browser, "dockLut")
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, self.dock_lut)

        self.dock_raw = create_custom_dock(I18N.tr("panel.camera_raw", "Camera Raw"), self.raw_panel, "dockRaw")
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self.dock_raw)

        self.dock_analysis = create_custom_dock(I18N.tr("panel.analysis", "示波器"), self.scope_panel, "dockAnalysis")
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self.dock_analysis)
        # Hidden scopes must not consume GPU analysis time. Showing the dock
        # also triggers an immediate demand for the current front texture.
        self.dock_analysis.visibilityChanged.connect(self._on_analysis_scope_visibility_changed)
        self.canvas.scopes_requested.connect(self.scope_panel.open_floating)

        from src.features.color.ui.color_keyframes_panel import ColorKeyframesPanel
        from src.features.color.ui.color_keyframes_view_state import tr as keyframes_tr

        self.color_keyframes_panel = ColorKeyframesPanel()
        self.dock_color_keyframes = create_custom_dock(
            keyframes_tr("title", "Keyframes"), self.color_keyframes_panel, "dockColorKeyframes"
        )
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self.dock_color_keyframes)
        self.dock_color_keyframes.hide()
        self._color_keyframes_compact_width = 0
        self.dock_color_keyframes.toggleViewAction().triggered.connect(self._raise_color_keyframes)
        self.color_keyframes_panel.expand_requested.connect(self._resize_color_keyframes)

        # The image batch-export panel. Renamed for clarity so a future
        # video batch-export panel can coexist without confusing users.
        export_title = (I18N.tr("export.batch_export", "Batch Export") if image_layers
                        else I18N.tr("panel.image_export", "Image Export"))
        self.dock_export = create_custom_dock(export_title, self.export_panel, "dockExport")
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.dock_export)

        self.dock_crop = create_custom_dock(I18N.tr("panel.crop_transform", "Crop & Transform"), self.crop_panel, "dockCrop")
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self.dock_crop)

        # Effects Panel Dock (formerly Plugin Panel; D2 后唯一宿主是新浏览器)
        self.dock_plugin = create_custom_dock(I18N.tr("panel.effects", "Effects"), self.color_effects_browser, "dockEffects")
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, self.dock_plugin)

        # Script Editor dock
        self.dock_script = create_custom_dock(I18N.tr("panel.script", "Script"), self.script_panel, "dockScript")
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self.dock_script)

        # Gallery Panel Dock (P1-COLOR-04: 原 Presets 面板升级为 Gallery；
        # objectName 保持 "dockPresets" 以兼容已保存的布局状态)
        self.dock_presets = create_custom_dock(I18N.tr("panel.gallery", "Gallery"), self.presets_panel, "dockPresets")
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, self.dock_presets)
        self.tabifyDockWidget(self.dock_presets, self.dock_lut)
        self.tabifyDockWidget(self.dock_lut, self.dock_plugin)
        self.dock_grade_sets = None
        if self.grade_set_panel is not None:
            self.dock_grade_sets = create_custom_dock(
                I18N.tr("grade_sets.title", "Grade Sets"), self.grade_set_panel, "dockGradeSets",
            )
            self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, self.dock_grade_sets)
            self.tabifyDockWidget(self.dock_presets, self.dock_grade_sets)
            self.dock_grade_sets.visibilityChanged.connect(
                lambda visible: self._grade_set_previews.schedule() if visible else None)

        # P9.1: Timeline Clip Strip Dock (Color page)
        self.dock_color_timeline_clips = create_custom_dock(
            I18N.tr("panel.timeline_clips", "Timeline Clips"),
            self.color_timeline_clip_strip,
            "dockColorTimelineClips",
        )
        self.addDockWidget(Qt.DockWidgetArea.TopDockWidgetArea, self.dock_color_timeline_clips)
        # Connect clip_activated to open_timeline_clip_in_color
        self.color_timeline_clip_strip.clip_activated.connect(self.open_timeline_clip_in_color)
        # 2026-11-01 片段版本菜单：WIRE-01/02 宿主接线（读取/写入经
        # clip_version_host 单一出口，权威工程 + 真实命令栈 + 回填）。
        from src.app.controllers.clip_version_host import ClipVersionRequestHost
        self._clip_version_request_host = ClipVersionRequestHost(self)
        self._clip_version_request_host.connect_strip(
            self.color_timeline_clip_strip)
        # P1-COLOR-16: 分类调色复制/粘贴（edit_controller mixin 方法）
        self.color_timeline_clip_strip.grade_copy_requested.connect(
            self._on_grade_copy_requested)
        self.color_timeline_clip_strip.grade_paste_requested.connect(
            self._on_grade_paste_requested)
        self.color_timeline_clip_strip.grade_paste_all_requested.connect(
            self._on_grade_paste_all_requested)
        # Connect float/close signals from right-click menu
        self.color_timeline_clip_strip.float_requested.connect(
            lambda: self.dock_color_timeline_clips.setFloating(
                not self.dock_color_timeline_clips.isFloating()
            )
        )
        self.color_timeline_clip_strip.close_requested.connect(
            lambda: self.dock_color_timeline_clips.hide()
        )

        # Edit Page Docks (P1 phase) - Phase 8: Conditional creation
        self.dock_edit_media_pool = None
        self.dock_program_viewer = None
        self.edit_viewport = None
        # Kept as an absent legacy attribute for old layout/host adapters.
        self.dock_source_viewer = None
        self.dock_edit_timeline = None

        if self._edit_available and self.media_pool_panel is not None:
            # Media Pool - Left area
            self.dock_edit_media_pool = create_custom_dock(
                I18N.tr("panel.media_pool", "Media Pool"), self.media_pool_panel, "dockEditMediaPool"
            )
            self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, self.dock_edit_media_pool)

            from src.features.edit.ui.edit_viewport import EditViewport

            self.edit_viewport = EditViewport(
                self.program_viewer_widget, self.source_viewer_widget
            )
            self._setup_edit_viewport_preferences()
            # Retain the stable dock identity so existing workspace snapshots
            # restore the Viewport to the old Program monitor's position.
            self.dock_program_viewer = create_custom_dock(
                I18N.tr("panel.edit_viewport", "Viewport"), self.edit_viewport, "dockProgramViewer"
            )
            self.addDockWidget(Qt.DockWidgetArea.TopDockWidgetArea, self.dock_program_viewer)
            self.dock_program_viewer.raise_()

            # Timeline - Bottom area
            self.dock_edit_timeline = create_custom_dock(
                I18N.tr("panel.timeline", "Timeline"), self.timeline_panel, "dockEditTimeline"
            )
            self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self.dock_edit_timeline)

        # Connect MediaPoolPanel project_changed to MainWindow for state sync
        # This updates _current_edit_project and marks dirty state on project changes
        if self._edit_available and self.media_pool_panel is not None:
            self.media_pool_panel.project_changed.connect(self._on_media_pool_project_changed)
        if self._edit_available and self.timeline_panel is not None:
            self.timeline_panel.project_changed.connect(self._on_timeline_project_changed)

        # Connect EditPage to panels
        if self._edit_available and self.edit_page is not None:
            # 注入 MainWindow 引用：EditPage 无 Qt parent，self.window() 返回自身，
            # 无法通过 window() 访问 dock 级面板（如 audio_mixer_panel）。
            # 显式注入让 EditPage 能连接音频电平信号到 mixer 面板。
            self.edit_page.set_main_window(self)
            if self.program_viewer_widget is not None:
                self.edit_page.set_program_viewer(self.program_viewer_widget)
                self.program_viewer_widget.timeline_switch_requested.connect(
                    self.edit_page.switch_timeline
                )
            # Color's viewport uses the same project timeline switch entry
            # point as the Program monitor, while keeping its own toolbar.
            if hasattr(self.canvas, "viewport_menu"):
                self.canvas.viewport_menu.timeline_switch_requested.connect(
                    self.edit_page.switch_timeline
                )
                menu = self.canvas.viewport_menu
                if self.dock_program_viewer is not None:
                    menu.timeline_composite_monitor_act.triggered.connect(self.set_color_timeline_composite_monitor)
                    menu.view_menu.aboutToShow.connect(self._sync_color_timeline_monitor_action)
                    self.dock_program_viewer.visibilityChanged.connect(self._on_color_program_dock_visibility)
            if self.timeline_panel is not None:
                self.edit_page.set_timeline_panel(self.timeline_panel)
            if self.media_pool_panel is not None:
                self.edit_page.set_media_pool_panel(self.media_pool_panel)
                self.media_pool_panel.project_changed.connect(
                    self._sync_color_timeline_selector
                )
                self._sync_color_timeline_selector()
            if self.source_viewer_widget is not None:
                self.edit_page.set_source_viewer(self.source_viewer_widget)
            if self.edit_viewport is not None and self.timeline_panel is not None:
                controller = self.timeline_panel.get_playback_controller()
                if controller is not None:
                    self.edit_viewport.program_seek_requested.connect(controller.seek)
                    stop = getattr(self, "_on_program_transport_stop_requested", controller.stop_in_place)
                    self.edit_viewport.program_stop_requested.connect(stop)
                    controller.frame_changed.connect(self.edit_viewport.set_program_position)
                self.edit_page.timeline_display_changed.connect(self._sync_edit_viewport_timeline)
                self.edit_viewport.bind_timeline_widget(self.timeline_panel)
                timeline_view = self.timeline_panel.get_timeline_view()
                timeline_view.range_display_changed.connect(self.edit_viewport.set_program_range)
                self._sync_edit_viewport_timeline(
                    timeline_view.get_timeline()
                )

            # I01（复查计划）：全入口播放仲裁。协调器一次性安装（所有权归
            # 本宿主引用，重复创建走此守卫），随后立即同步初始状态（已打开
            # 素材/时间线可用性/两路实际播放/布局/激活/页面），并把两路
            # 入口仲裁门挂到真实 widget/panel 上。按钮/Space/JKL/scrub 的
            # 原有准备链保持不变——门只在"起播"前裁决与暂停对侧。
            if (self.edit_viewport is not None
                    and self.timeline_panel is not None
                    and self.source_viewer_widget is not None
                    and getattr(self, "_viewport_playback_coordinator", None) is None):
                from src.features.edit.ui.viewport_playback_coordinator import (
                    install_viewport_playback_coordinator,
                )
                from src.features.edit.core.viewport_playback_arbiter import (
                    PAGE_DELIVER, PAGE_EDIT, PAGE_OTHER,
                )
                coordinator = install_viewport_playback_coordinator(
                    viewport=self.edit_viewport,
                    source_widget=self.source_viewer_widget,
                    timeline_panel=self.timeline_panel,
                    parent=self)
                self._viewport_playback_coordinator = coordinator
                # BR08：菜单期间 transport 门的生产 feed——app 级事件过滤
                # 把 Qt/原生菜单生命周期接到 set_menu_open（Space/J/K/L
                # 菜单打开时不穿透触发底层播放）。
                from src.features.edit.ui.viewport_playback_coordinator import (
                    install_menu_transport_gate,
                )
                self._menu_transport_gate = install_menu_transport_gate(
                    coordinator)
                # I01-5：拒绝原因回填宿主状态栏（不静默吞掉仲裁拒绝）
                coordinator.status_message.connect(
                    lambda message: self.statusBar().showMessage(message, 4000))
                page = str(getattr(self, "current_page", "Edit") or "Edit")
                coordinator.sync_initial_state(page_mode=(
                    PAGE_EDIT if page == "Edit"
                    else PAGE_DELIVER if page == "Deliver"
                    else PAGE_OTHER))
                self.source_viewer_widget.set_playback_gate(
                    coordinator.authorize_source_play)
                self.timeline_panel.set_playback_gate(
                    coordinator.authorize_timeline_play)
                # 文本输入焦点门：重命名/查找等输入框聚焦时起播静默
                try:
                    from src.shared.ui.shortcut_scope import text_input_focused
                    app = QApplication.instance()
                    if app is not None:
                        app.focusChanged.connect(
                            lambda *_a: coordinator.set_text_input_active(
                                text_input_focused()))
                except Exception:
                    logger.debug("text-input gate feed unavailable",
                                 exc_info=True)
                # I03（复查计划）：capability 宿主——真实上下文喂注册表，
                # 菜单/底栏按钮按 supported/reason 回填；Match Frame 两向
                # 接真实 handler（timeline→source 走既有打开链，
                # source→timeline 多使用时出候选，取消不动播放头）。
                try:
                    from src.app.controllers.viewport_capability_host import (
                        install_viewport_capability_host,
                    )
                    self._viewport_capability_host = (
                        install_viewport_capability_host(self))
                except Exception:
                    logger.exception("Failed to install viewport capability host")
                    self._viewport_capability_host = None
                # I06（复查计划）：双视图预算执行器——政策（B03）真实作用
                # 到既有调度面（Timeline 预览分辨率档 + 结构性单在飞/可见性
                # 守卫的核验记录），诊断可查询。渲染器在进入 Edit 页时懒建，
                # 经 provider 每次取最新引用。
                try:
                    from src.features.edit.ui.viewport_budget_executor import (
                        ViewportBudgetExecutor,
                    )
                    self._viewport_budget_executor = ViewportBudgetExecutor(
                        viewport=self.edit_viewport,
                        source_widget=self.source_viewer_widget,
                        timeline_panel=self.timeline_panel,
                        coordinator=coordinator,
                        renderer_provider=(
                            lambda: getattr(self, "timeline_preview_renderer",
                                            None)),
                        parent=self)
                except Exception:
                    logger.exception("Failed to install budget executor")
                    self._viewport_budget_executor = None

        # CV-B01（Color 后端计划）：Color 视口能力宿主——聚合页面/呈现
        # 路径/引擎事实喂注册表，chrome 按钮与 View 菜单按 supported/
        # reason 回填；无 handler 的动作保持禁用（诚实禁用不虚亮）。
        if getattr(self, "_color_viewport_capability_host", None) is None:
            try:
                from src.app.controllers.color_viewport_capability_host import (
                    install_color_viewport_capability_host,
                )
                self._color_viewport_capability_host = (
                    install_color_viewport_capability_host(self))
            except Exception:
                logger.exception("Failed to install color viewport "
                                 "capability host")
                self._color_viewport_capability_host = None

        # Connect MediaPoolPanel double-click to Source Viewer (P3.1)
        if self._edit_available and self.media_pool_panel is not None:
            self.media_pool_panel.asset_double_clicked.connect(self._on_asset_double_clicked)
            self.media_pool_panel.asset_selected.connect(self._on_asset_selected)
            self.media_pool_panel.asset_preview_requested.connect(self._on_asset_preview_requested)

        # Connect MediaPoolPanel right-click "Add to Timeline"
        if self._edit_available and self.media_pool_panel is not None:
            self.media_pool_panel.add_to_timeline_requested.connect(self._on_add_asset_to_timeline)

        # E08: 图片素材右键 "Open Asset Grade in Color"（此前 0 接收者）
        if self._edit_available and self.media_pool_panel is not None:
            if hasattr(self.media_pool_panel, "open_asset_grade_requested"):
                self.media_pool_panel.open_asset_grade_requested.connect(
                    self._on_open_asset_grade_in_color
                )

        # Connect MediaPoolPanel timeline signals (V-10, UI-27 to UI-34)
        if self._edit_available and self.media_pool_panel is not None:
            self.media_pool_panel.timeline_switch_requested.connect(self._on_timeline_switch_requested)
            self.media_pool_panel.timeline_create_requested.connect(self._on_timeline_create_requested)
            self.media_pool_panel.timeline_settings_requested.connect(self._on_timeline_settings_requested)
            self.media_pool_panel.timeline_delete_requested.connect(self._on_timeline_delete_requested)
            # Delete-key batch delete: request full TimelineSequence snapshots
            # before deletion so tracks/clips survive undo/redo.
            self.media_pool_panel.timeline_command_snapshot_requested.connect(
                self._on_timeline_command_snapshot_requested
            )

        # Auto-create project when user imports/drags without an active project
        if self._edit_available and self.media_pool_panel is not None:
            self.media_pool_panel.auto_project_needed.connect(self._ensure_edit_project)
        if self._edit_available and self.timeline_panel is not None:
            self.timeline_panel.project_needed.connect(self._ensure_edit_project)

        # UI-34: Refresh renderer project reference when new assets are imported
        if self._edit_available and self.media_pool_panel is not None:
            self.media_pool_panel.asset_imported.connect(self._on_asset_imported_renderer_sync)

        # P5.1: Connect EditPage asset selection to Properties panel
        if self._edit_available and self.edit_page is not None:
            self.edit_page.signal_hub.assets_selection_changed.connect(self._on_assets_selection_changed)
            self.edit_page.signal_hub.timeline_selected.connect(
                self._on_media_pool_timeline_selected
            )

        # P9.2: Connect EditPage open in composite request
        if self._edit_available and self.edit_page is not None:
            self.edit_page.signal_hub.open_in_composite_requested.connect(self._on_open_in_composite_requested)

        # P5: Connect MediaPoolPanel comp clip signals
        if self._edit_available and self.media_pool_panel is not None:
            self.media_pool_panel.open_comp_clip_in_composite_requested.connect(self._on_open_comp_clip_in_composite)
            self.media_pool_panel.create_empty_comp_clip_requested.connect(self._on_create_empty_comp_clip)
            self.media_pool_panel.edit_comp_clip_settings_requested.connect(self._on_edit_comp_clip_settings)
            # P1-01: 多机位组创建（多选视频素材）
            if hasattr(self.media_pool_panel, "create_multicam_group_requested"):
                self.media_pool_panel.create_multicam_group_requested.connect(
                    self._on_create_multicam_group
                )

        # P1-01: Multicam 角度面板切换 + 波形同步
        if self._edit_available and getattr(self, "multicam_panel", None) is not None:
            self.multicam_panel.switch_angle_requested.connect(
                lambda gid, aid, off: self._switch_multicam_angle(gid, aid, int(off))
            )
            # F2.10：视频素材绑定外录音频入口
            if hasattr(self.media_pool_panel, "bind_external_audio_requested"):
                self.media_pool_panel.bind_external_audio_requested.connect(
                    self._on_bind_external_audio
                )
            # F2.7：批量解释覆盖 → 提示应用到当前时间线片段
            if hasattr(self.media_pool_panel, "interpretation_applied"):
                self.media_pool_panel.interpretation_applied.connect(
                    self._on_interpretation_applied
                )
            if hasattr(self.multicam_panel, "sync_requested"):
                self.multicam_panel.sync_requested.connect(
                    self._on_multicam_sync_requested
                )
            # F2.6：时间码同步入口 + 分析中途取消
            if hasattr(self.multicam_panel, "tc_sync_requested"):
                self.multicam_panel.tc_sync_requested.connect(
                    self._on_multicam_tc_sync_requested
                )
            if hasattr(self.multicam_panel, "sync_cancel_requested"):
                self.multicam_panel.sync_cancel_requested.connect(
                    self._cancel_multicam_sync
                )
            # F2.5：播放起停驱动实时网格流会话（暂停即回落静帧路径）
            if hasattr(self.multicam_panel, "set_playback_active") \
                    and self.edit_page is not None:
                if hasattr(self.edit_page.signal_hub, "playback_started"):
                    self.edit_page.signal_hub.playback_started.connect(
                        lambda: self.multicam_panel.set_playback_active(True)
                    )
                if hasattr(self.edit_page.signal_hub, "playback_stopped"):
                    self.edit_page.signal_hub.playback_stopped.connect(
                        lambda: self.multicam_panel.set_playback_active(False)
                    )
            # F2.5：切时间线/关闭工程：结束网格会话（不跨时间线解码）
            if hasattr(self.edit_page.signal_hub, "timeline_switched"):
                self.edit_page.signal_hub.timeline_switched.connect(
                    lambda _tid: self.multicam_panel.set_playback_active(False)
                )

        # Timeline switching — load timeline into TimelinePanel
        if self._edit_available and self.edit_page is not None:
            self.edit_page.signal_hub.timeline_switched.connect(self._on_timeline_switched)

        # T7: Connect Edit right-click "Open in Color" to open_timeline_clip_in_color
        if self._edit_available and self.edit_page is not None:
            self.edit_page.signal_hub.open_in_color_requested.connect(self._on_open_in_color_requested)

        # Phase 2: Connect Comp Clip context menu signals
        if self._edit_available and self.edit_page is not None:
            self.edit_page.signal_hub.create_comp_clip_requested.connect(self._on_create_comp_clip_requested)
            self.edit_page.signal_hub.reset_composite_requested.connect(self._on_reset_composite_requested)
            self.edit_page.signal_hub.remove_composite_binding_requested.connect(self._on_remove_composite_binding_requested)
            self.edit_page.signal_hub.bypass_composite_requested.connect(self._on_bypass_composite_requested)
            self.edit_page.signal_hub.reveal_comp_clip_in_media_pool_requested.connect(self._on_reveal_comp_clip_in_media_pool)
            self.edit_page.signal_hub.render_cache_clip_requested.connect(self._on_render_cache_clip_requested)
            self.edit_page.signal_hub.rerender_cache_clip_requested.connect(self._on_rerender_cache_clip_requested)
            # P9.1: 共享素材调色 / 本地调色副本（此前菜单项发射后无接收者）
            self.edit_page.signal_hub.use_shared_grade_requested.connect(self._on_use_shared_grade_requested)
            self.edit_page.signal_hub.make_local_grade_copy_requested.connect(self._on_make_local_grade_copy_requested)
            # P1-02: 嵌套序列工作流
            self.edit_page.signal_hub.create_nested_from_selection_requested.connect(self._on_create_nested_from_selection)
            self.edit_page.signal_hub.enter_nested_sequence_requested.connect(self._on_enter_nested_sequence)
            # P1-01: 多机位角度键 + 选择变化刷新角度面板
            self.edit_page.signal_hub.multicam_angle_key_requested.connect(self._on_multicam_angle_key)
            self.edit_page.signal_hub.clip_selected.connect(
                lambda _clip_id: self._refresh_multicam_panel()
            )
            # P1-01: playhead 变化 → 角度网格取帧（面板内 250ms 去抖合并）
            self.edit_page.signal_hub.playhead_changed.connect(
                self._on_multicam_playhead_frame
            )
            # Transform handles must follow the clip currently under the
            # playhead and must not remain bound to a previous clip.
            self.edit_page.signal_hub.playhead_changed.connect(
                self._on_edit_playhead_changed
            )
            # P10.3: 字幕手柄交互
            self.edit_page.signal_hub.subtitle_interaction_started.connect(self.on_subtitle_interaction_started)
            self.edit_page.signal_hub.subtitle_interaction_updated.connect(self.on_subtitle_interaction_updated)
            self.edit_page.signal_hub.subtitle_interaction_ended.connect(self.on_subtitle_interaction_ended)
            # Clip Transform on-canvas handle interaction
            self.edit_page.signal_hub.clip_transform_interaction_updated.connect(self.on_clip_transform_drag_updated)
            self.edit_page.signal_hub.clip_transform_interaction_ended.connect(self.on_clip_transform_drag_ended)
            self.edit_page.signal_hub.transform_tool_toggled.connect(self.on_transform_tool_toggled)
            self.edit_page.signal_hub.program_guide_changed.connect(self.on_program_guide_changed)

        # Edit properties and contextual inspectors occupy the right side.
        self.dock_properties = None
        self.dock_edit_info = None
        self.dock_edit_effects = None
        self.dock_caption_workspace = None  # SUB-P2-03: Caption Workspace
        self.dock_adr_take_sheet = None  # P1-10: ADR Take Sheet
        self.dock_multicam = None  # P1-01: Multicam angles
        self.dock_ai_edit_assistant = None
        self.ai_edit_assistant_panel = None
        if self._edit_available and self.edit_properties_panel is not None:
            self.dock_properties = create_custom_dock(
                I18N.tr("panel.properties", "Properties"), self.edit_properties_panel, "dockProperties"
            )
            self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.dock_properties)

            # Read-only media inspector for Edit, with fuller video metadata
            # than the image-oriented Color Info panel.
            if self.edit_info_panel is not None:
                self.dock_edit_info = create_custom_dock(
                    I18N.tr("panel.info", "Info"), self.edit_info_panel, "dockEditInfo"
                )
                self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.dock_edit_info)
                self.tabifyDockWidget(self.dock_properties, self.dock_edit_info)

            # Effects are browsed alongside the media pool.
            if self.edit_effects_panel is not None:
                self.dock_edit_effects = create_custom_dock(
                    I18N.tr("panel.edit_effects", "Effects"), self.edit_effects_panel, "dockEditEffects"
                )
                self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, self.dock_edit_effects)
                if self.dock_edit_media_pool is not None:
                    self.tabifyDockWidget(self.dock_edit_media_pool, self.dock_edit_effects)

            # SUB-P2-03: Caption Workspace - Right area, tabified with Properties
            # so it shares the same dock stack as Properties/Info on Edit page.
            if self.caption_workspace_panel is not None:
                self.dock_caption_workspace = create_custom_dock(
                    I18N.tr("panel.caption_workspace", "Captions"),
                    self.caption_workspace_panel, "dockCaptionWorkspace"
                )
                self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea,
                                   self.dock_caption_workspace)
                # Tabify with properties so the three right-area panels stack.
                self.tabifyDockWidget(self.dock_properties, self.dock_caption_workspace)

            # Mixer hosts the same ADR widget that Edit embeds in Subtitles.
            if self.adr_take_sheet_panel is not None:
                adr_host = QWidget()
                adr_layout = QVBoxLayout(adr_host)
                adr_layout.setContentsMargins(0, 0, 0, 0)
                adr_layout.addWidget(self.adr_take_sheet_panel)
                self.dock_adr_take_sheet = create_custom_dock(
                    I18N.tr("panel.adr_take_sheet", "ADR / Takes"),
                    adr_host, "dockAdrTakeSheet"
                )
                self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea,
                                   self.dock_adr_take_sheet)
                if self.dock_properties is not None:
                    self.tabifyDockWidget(self.dock_properties,
                                          self.dock_adr_take_sheet)
                self.dock_adr_take_sheet.visibilityChanged.connect(self._on_adr_dock_visibility_changed)
                self.dock_adr_take_sheet.setVisible(False)

            # P1-01: Multicam angles - Right area, tabified with Properties
            # (default hidden; auto-refreshed from the multicam clip under
            # the playhead / current selection).
            if self.multicam_panel is not None:
                self.dock_multicam = create_custom_dock(
                    I18N.tr("panel.multicam", "Multicam"),
                    self.multicam_panel, "dockMulticam"
                )
                self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea,
                                   self.dock_multicam)
                if self.dock_properties is not None:
                    self.tabifyDockWidget(self.dock_properties, self.dock_multicam)
                self.dock_multicam.setVisible(False)

            # TAEP UI-0: AI Edit Assistant dock — right area, tabified with
            # Properties. Only the shell + state contract; no model wiring yet.
            # Lazy import to keep the panel module out of the startup hot path
            # when the dock is never shown.
            if self._edit_available:
                try:
                    from src.features.edit.ui.ai_edit_assistant_panel import (
                        AiEditAssistantPanel,
                    )
                    self.ai_edit_assistant_panel = AiEditAssistantPanel(self)
                    # AiEditAssistantPanel 本身就是 QDockWidget。不要再通过
                    # create_custom_dock 包一层，否则会产生嵌套 Dock，导致浮动、
                    # 标签化和焦点行为异常。
                    self.dock_ai_edit_assistant = self.ai_edit_assistant_panel
                    self.dock_ai_edit_assistant.setFeatures(
                        QDockWidget.DockWidgetFeature.DockWidgetClosable |
                        QDockWidget.DockWidgetFeature.DockWidgetMovable |
                        QDockWidget.DockWidgetFeature.DockWidgetFloatable
                    )
                    hidden_titlebar = QWidget()
                    hidden_titlebar.setFixedHeight(1)
                    self.dock_ai_edit_assistant.setTitleBarWidget(hidden_titlebar)
                    self.dock_ai_edit_assistant.setContextMenuPolicy(
                        Qt.ContextMenuPolicy.CustomContextMenu
                    )
                    # AiEditAssistantPanel 自己提供合并后的右键菜单（AI 操作 +
                    # 浮动/停靠/关闭），这里不要再连接通用 Dock 菜单，避免双菜单。
                    self.dock_ai_edit_assistant.dockLocationChanged.connect(
                        self.update_all_dock_titlebars
                    )
                    self.dock_ai_edit_assistant.topLevelChanged.connect(
                        lambda is_floating, d=self.dock_ai_edit_assistant:
                        self._on_dock_floating_changed(is_floating, d)
                    )
                    self.dock_ai_edit_assistant.visibilityChanged.connect(
                        self.update_all_dock_titlebars
                    )
                    self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea,
                                       self.dock_ai_edit_assistant)
                    # Tabify with properties so it joins the right-area stack.
                    if self.dock_properties is not None:
                        self.tabifyDockWidget(self.dock_properties, self.dock_ai_edit_assistant)
                    # Default hidden (UI-0): user opts in via Window > Panels.
                    self.dock_ai_edit_assistant.setVisible(False)
                except Exception:
                    logger.exception("Failed to create AI Edit Assistant dock")
                    self.dock_ai_edit_assistant = None

            # AI Workflow node-canvas（P8）。改为弹窗触发（用户右键 AI Assistant
            # 面板 → 显示工作流），不再作为 dock 固定在 Edit 顶部区域。flag 默认开
            # （feature_flags.py），但仍 gate 创建；弹窗懒建，关闭只 hide。
            self.ai_workflow_panel = None
            self._ai_workflow_dialog = None  # P5：弹窗懒建单例
            # P9：AI Workflow 专用执行 runtime + compiler + controller（首个 app 级
            # HarnessRuntime 实例；不与 Edit Assistant 共享，避免回归）。
            self._ai_workflow_runtime = None
            self._ai_workflow_compiler = None
            self._ai_workflow_fake_service = None
            self._ai_workflow_action_service = None
            self._ai_workflow_backend_mode = "unavailable"
            self._ai_workflow_run_controller = None
            if self._edit_available and self.feature_flags.is_enabled("ai_workflow"):
                try:
                    from src.features.ai_workflow.ui.workflow_panel import WorkflowPanel
                    from src.features.ai_workflow.core.node_specs import (
                        NodeSpecRegistry, register_official_specs,
                    )
                    from src.features.ai_workflow.core.template_registry import (
                        WorkflowTemplateRegistry, register_official_templates,
                    )
                    # 装配 registries
                    self._ai_workflow_node_specs = NodeSpecRegistry()
                    register_official_specs(self._ai_workflow_node_specs)
                    self._ai_workflow_templates = WorkflowTemplateRegistry()
                    register_official_templates(self._ai_workflow_templates)
                    # 创建 panel（不放 dock；弹窗懒建时移入）
                    self.ai_workflow_panel = WorkflowPanel(
                        registry=self._ai_workflow_node_specs,
                        template_registry=self._ai_workflow_templates,
                    )

                    # P9：装配执行链 runtime + compiler + controller。
                    # P0.2 收敛：runtime 复用 Edit Assistant 已建的单一实例
                    # （``self._shared_runtime``，由 _setup_ai_editing 解析），
                    # 消除 plan §0.2 "三种入口只有一个 HarnessRuntime" 双实例红线。
                    # compiler 用 P8 的 node spec registry。生产默认从已经装配的
                    # AssistantService 取真实后端；fake 仅在显式开发开关下启用。
                    from src.features.ai_harness.runtime import HarnessRuntime
                    from src.features.ai_workflow.core.workflow_compiler import (
                        WorkflowCompiler,
                    )
                    from src.features.ai_workflow.runtime.workflow_run_controller import (
                        WorkflowRunController,
                    )
                    import os
                    fake_service = None
                    if os.environ.get("AI_WORKFLOW_FAKE_PROVIDER") == "1":
                        from src.features.ai_workflow.runtime.fake_workflow_service import (
                            FakeWorkflowService,
                        )
                        fake_service = FakeWorkflowService()
                        self._ai_workflow_backend_mode = "synthetic"
                    else:
                        self._ai_workflow_backend_mode = "real_assistant"
                    # 复用 ai_editing_mixin.resolve_shared_runtime 的结果（若已建）；
                    # 否则新建（_setup_ai_editing 之后会复用本实例）。
                    self._ai_workflow_runtime = (
                        getattr(self, "_shared_runtime", None) or HarnessRuntime()
                    )
                    self._shared_runtime = self._ai_workflow_runtime
                    self._ai_workflow_compiler = WorkflowCompiler(
                        self._ai_workflow_node_specs,
                    )
                    from src.features.ai_workflow.runtime.workflow_context import (
                        resolve_workflow_context,
                    )
                    from src.features.ai_workflow.runtime.workflow_action_service import (
                        WorkflowActionService,
                    )
                    self._ai_workflow_action_service = WorkflowActionService(
                        service_provider=lambda: getattr(self, "_ai_assistant_service", None),
                        bridge_provider=lambda: getattr(self, "_ai_host_bridge", None),
                        approval_provider=lambda: (
                            getattr(self, "_ai_host_bridge", None).approval_service()
                            if getattr(self, "_ai_host_bridge", None) is not None
                            else None
                        ),
                    )
                    self._ai_workflow_fake_service = fake_service
                    # plan §9.5：composite 渲染 provider（worker 线程执行）+
                    # exposed input 解析器（主线程执行——读 composite 页/工程
                    # 涉及 Qt，不能推迟到 worker）。
                    from src.app.controllers.ai_workflow_composite_render import (
                        make_composite_render_provider,
                        make_exposed_input_resolver,
                    )
                    self._ai_workflow_run_controller = WorkflowRunController(
                        runtime=self._ai_workflow_runtime,
                        compiler=self._ai_workflow_compiler,
                        adapter=self.ai_workflow_panel.adapter,
                        panel=self.ai_workflow_panel,
                        fake_service=fake_service,
                        service_provider=lambda: getattr(self, "_ai_assistant_service", None),
                        context_provider=lambda service: resolve_workflow_context(
                            getattr(self, "_ai_host_bridge", None), service,
                        ),
                        action_service=self._ai_workflow_action_service,
                        composite_render_provider=make_composite_render_provider(self),
                        exposed_input_resolver=make_exposed_input_resolver(self),
                        parent=self,
                    )
                    # panel run/cancel 信号 → controller
                    self.ai_workflow_panel.run_requested.connect(
                        self._ai_workflow_run_controller.run
                    )
                    self.ai_workflow_panel.cancel_requested.connect(
                        self._ai_workflow_run_controller.cancel
                    )
                    self.ai_workflow_panel.preview_plan_requested.connect(
                        self._ai_workflow_run_controller.preview_plan
                    )
                    self.ai_workflow_panel.approval_requested.connect(
                        self._ai_workflow_run_controller.approve_current_plan
                    )
                    self.ai_workflow_panel.apply_plan_requested.connect(
                        self._ai_workflow_run_controller.apply_current_plan
                    )
                    self.ai_workflow_panel.artifact_requested.connect(
                        self._ai_workflow_run_controller.show_artifact
                    )
                    self.ai_workflow_panel.run_drawer.retry_clicked.connect(
                        self._ai_workflow_run_controller.retry
                    )
                    self.ai_workflow_panel.run_drawer.diagnostics_clicked.connect(
                        self._ai_workflow_run_controller.show_diagnostics
                    )
                    self.ai_workflow_panel.run_drawer.resume_clicked.connect(
                        self._ai_workflow_run_controller.resume
                    )
                    self.ai_workflow_panel.run_drawer.handoff_clicked.connect(
                        self._ai_workflow_run_controller.handoff
                    )
                    self.ai_workflow_panel.run_drawer.rollback_clicked.connect(
                        self._ai_workflow_run_controller.rollback
                    )
                except Exception:
                    logger.exception("Failed to create AI Workflow panel")
                    self.ai_workflow_panel = None
                    self._ai_workflow_runtime = None
                    self._ai_workflow_compiler = None
                    self._ai_workflow_fake_service = None
                    self._ai_workflow_action_service = None
                    self._ai_workflow_run_controller = None

        # Composite Page Dock - optional module (wired through adapter)
        self.dock_composite = None
        self._node_properties_panel = None
        self.dock_node_properties = None  # Independent Composite properties dock
        self.dock_composite_scope = None  # P1-11: Composite QC scopes
        self.dock_perf = None
        self.dock_composite_keyframes = None  # P3: bottom Keyframes dock
        self.dock_composite_spline = None    # Phase 3: bottom Spline dock
        self.dock_composite_import_report = None  # F0: Model3D import report
        self.dock_composite_asset_dependency = None  # P1-14: asset dependencies
        self.dock_composite_scene_outliner = None  # P1-18: Model3D outliner
        self.dock_composite_material = None  # P1-23: Model3D material editor
        self.composite_page = None  # May be set by _create_pages_from_registry
        self.composite_viewer = None
        self._composite_adapter = None
        if self._composite_available:
            try:
                from src.features.composite.facade import get_main_window_adapter_class
                _AdapterCls = get_main_window_adapter_class()
                if _AdapterCls is None:
                    raise ImportError("MainWindowAdapter unavailable")
                MainWindowAdapter = _AdapterCls
                self._composite_adapter = MainWindowAdapter(self)
                if self._composite_adapter.create(self):
                    runtime = self._composite_adapter.runtime
                    self.composite_page = runtime.page
                    self.composite_viewer = runtime.viewer
                    self._node_properties_panel = runtime.properties_panel

                    docks = self._composite_adapter.create_docks(create_custom_dock)
                    if docks.get("composite"):
                        self.dock_composite = docks["composite"]
                        self.addDockWidget(Qt.DockWidgetArea.TopDockWidgetArea, self.dock_composite)
                    # Composite owns its Node Properties dock so it shows in
                    # every work mode (Image mode disables Edit, which used
                    # to remove the shared Properties container).
                    if docks.get("node_properties"):
                        self.dock_node_properties = docks["node_properties"]
                        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.dock_node_properties)
                    if docks.get("scope"):
                        self.dock_composite_scope = docks["scope"]
                        self.addDockWidget(
                            Qt.DockWidgetArea.BottomDockWidgetArea,
                            self.dock_composite_scope)
                        self.dock_composite_scope.setVisible(False)
                    if docks.get("perf"):
                        self.dock_perf = docks["perf"]
                        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self.dock_perf)
                    # P3: Fusion-style Keyframes dock at the bottom.
                    # Image mode has no animation workflow, so the keyframes
                    # and spline docks are not added to the main window (they
                    # would only show an empty timeline with no time source).
                    image_mode = self._current_work_mode() == "image"
                    if docks.get("keyframes") and not image_mode:
                        self.dock_composite_keyframes = docks["keyframes"]
                        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self.dock_composite_keyframes)
                    # Phase 3: Spline dock remains independent for now. It
                    # will be folded into Keyframes after its editor is fully
                    # integrated there.
                    if docks.get("spline") and not image_mode:
                        self.dock_composite_spline = docks["spline"]
                        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self.dock_composite_spline)
                    # Model3D panels are optional tools. They are created as
                    # independent docks so the Panels menu can still address
                    # each capability, then grouped into workflow stacks
                    # below. Keeping the widgets independent preserves their
                    # existing signal/lifecycle contracts while presenting a
                    # less cluttered Composite workspace.
                    if docks.get("import_report"):
                        self.dock_composite_import_report = docks["import_report"]
                        self.addDockWidget(
                            Qt.DockWidgetArea.RightDockWidgetArea,
                            self.dock_composite_import_report)
                        self.dock_composite_import_report.setVisible(False)
                    if docks.get("asset_dependency"):
                        self.dock_composite_asset_dependency = docks[
                            "asset_dependency"]
                        self.addDockWidget(
                            Qt.DockWidgetArea.RightDockWidgetArea,
                            self.dock_composite_asset_dependency)
                        self.dock_composite_asset_dependency.setVisible(False)
                    if docks.get("scene_outliner"):
                        self.dock_composite_scene_outliner = docks["scene_outliner"]
                        self.addDockWidget(
                            Qt.DockWidgetArea.RightDockWidgetArea,
                            self.dock_composite_scene_outliner)
                        self.dock_composite_scene_outliner.setVisible(False)
                    if docks.get("material"):
                        self.dock_composite_material = docks["material"]
                        self.addDockWidget(
                            Qt.DockWidgetArea.RightDockWidgetArea,
                            self.dock_composite_material)
                        self.dock_composite_material.setVisible(False)

                    # Composite workflow groups. QDockWidget tabification is
                    # deliberately used instead of a monolithic wrapper so
                    # existing Panels-menu actions, floating behavior and
                    # user-customized layouts remain intact.
                    if (self.dock_composite_asset_dependency and
                            self.dock_composite_import_report):
                        self.tabifyDockWidget(
                            self.dock_composite_asset_dependency,
                            self.dock_composite_import_report)
                    inspector_anchor = self.dock_node_properties
                    if inspector_anchor is None:
                        inspector_anchor = self.dock_composite_scene_outliner
                    if (inspector_anchor is not None and
                            self.dock_composite_scene_outliner is not None and
                            self.dock_composite_scene_outliner is not inspector_anchor):
                        self.tabifyDockWidget(
                            inspector_anchor,
                            self.dock_composite_scene_outliner)
                    if (inspector_anchor is not None and
                            self.dock_composite_material is not None and
                            self.dock_composite_material is not inspector_anchor):
                        self.tabifyDockWidget(
                            inspector_anchor,
                            self.dock_composite_material)
                else:
                    logger.warning("Composite adapter creation failed; feature will be disabled")
                    self._composite_available = False
                    self._composite_adapter = None
            except ImportError as e:
                logger.warning(f"Composite adapter unavailable: {e}; falling back to legacy path")
                self._composite_available = False
                self._composite_adapter = None

        # Deliver Page Dock (P9.3) - Phase 9: Conditional creation
        self.dock_deliver = None
        # The video queue is embedded in DeliverWorkspace by default.  It is
        # detached lazily by DetachablePanelHost, so there is no second
        # always-visible bottom dock that can collapse into a narrow strip.
        self.dockVideoBatchExport = None
        if self._deliver_available and self.deliver_page is not None:
            self.dock_deliver = create_custom_dock(
                I18N.tr("page.deliver", "Export"), self.deliver_page, "dockDeliver"
            )
            self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.dock_deliver)
            attach_batch_host = getattr(self.deliver_page, "attach_batch_panel_host", None)
            if callable(attach_batch_host):
                attach_batch_host(self)

        self._bind_timeline_recording()
        self._create_audio_workspace_docks(create_custom_dock)

        self.all_docks = [
            self.dock_viewport, self.dock_library, self.dock_files, self.dock_info,
            self.dock_layers, self.dock_denoise, self.dock_deblur,
            self.dock_grading, self.dock_base_grade, self.dock_chromogen,
            self.dock_curves, self.dock_warper, self.dock_mixer,
            self.dock_audio_mixer, self.dock_effects, self.dock_grain,
            self.dock_masks, self.dock_qualifier, self.dock_lut,
            self.dock_raw, self.dock_analysis, self.dock_export,
            self.dock_crop, self.dock_plugin, self.dock_script, self.dock_presets,
            self.dock_color_timeline_clips, self.dock_tracker, self.dock_color_keyframes,
        ]
        # Add Edit docks if available
        if self.dock_grade_sets is not None:
            self.all_docks.append(self.dock_grade_sets)
        if self.dock_edit_media_pool:
            self.all_docks.append(self.dock_edit_media_pool)
        if self.dock_program_viewer:
            self.all_docks.append(self.dock_program_viewer)
        if self.dock_edit_timeline:
            self.all_docks.append(self.dock_edit_timeline)
        if self.dock_edit_info:
            self.all_docks.append(self.dock_edit_info)
        if self.dock_ai_edit_assistant:
            self.all_docks.append(self.dock_ai_edit_assistant)
        # [BUG-B4-01] Edit 页右栈 5 个 dock 同样必须注册进 all_docks：
        # 否则 _perform_update_all_titlebars / _sync_floating_docks 跳过它们
        # （浮动后只剩 1px 隐形标题栏，无法拖动也无法拖回停靠），
        # _save_all_docked_sizes / _restore_docked_sizes 也跳过它们（停靠
        # 尺寸不持久化）。与上面 Keyframes/Spline 的教训同一机制。
        if self.dock_properties:
            self.all_docks.append(self.dock_properties)
        if self.dock_edit_effects:
            self.all_docks.append(self.dock_edit_effects)
        if self.dock_caption_workspace:
            self.all_docks.append(self.dock_caption_workspace)
        if self.dock_adr_take_sheet:
            self.all_docks.append(self.dock_adr_take_sheet)
        if self.dock_multicam:
            self.all_docks.append(self.dock_multicam)
        # AI Workflow 已改为弹窗（不再注册 dock）；见 _show_ai_workflow_dialog。
        # Add Composite dock if available
        if self.dock_composite:
            self.all_docks.append(self.dock_composite)
        # Composite's independent Node Properties dock — same membership rule
        # as Keyframes/Spline below (floating titlebar + size persistence).
        if self.dock_node_properties:
            self.all_docks.append(self.dock_node_properties)
        # Add Perf dock if available
        if hasattr(self, 'dock_perf') and self.dock_perf:
            self.all_docks.append(self.dock_perf)
        # Keyframes/Spline 底部 dock 必须在 all_docks 里：否则浮动时
        # _perform_update_all_titlebars 不会给它们安装 FloatingDockTitleBar，
        # 顶部只剩 1px 隐形标题栏——既无法拖动浮动窗口，也无法拖回停靠。
        if self.dock_composite_keyframes:
            self.all_docks.append(self.dock_composite_keyframes)
        if self.dock_composite_spline:
            self.all_docks.append(self.dock_composite_spline)
        if self.dock_composite_import_report:
            self.all_docks.append(self.dock_composite_import_report)
        if self.dock_composite_asset_dependency:
            self.all_docks.append(self.dock_composite_asset_dependency)
        if self.dock_composite_scene_outliner:
            self.all_docks.append(self.dock_composite_scene_outliner)
        if self.dock_composite_material:
            self.all_docks.append(self.dock_composite_material)
        if self.dock_composite_scope:
            self.all_docks.append(self.dock_composite_scope)
        # Add Deliver dock if available (Phase 9)
        if self.dock_deliver:
            self.all_docks.append(self.dock_deliver)
        self.all_docks.extend(self._audio_workspace_docks())
        if self._current_work_mode() == "image":
            # Resolve visibility after a page switch has restored Qt state
            # and published the destination page.
            for dock in self.all_docks:
                dock.visibilityChanged.connect(
                    self._guard_image_library_layout, Qt.ConnectionType.QueuedConnection,
                )
            self.dock_library.topLevelChanged.connect(
                self._guard_image_library_layout, Qt.ConnectionType.QueuedConnection,
            )
            self.dock_export.visibilityChanged.connect(
                self._guard_image_batch_export, Qt.ConnectionType.QueuedConnection,
            )
        # Initial splitting and proportions
        self.apply_default_layout()

        # Update initial titlebar states immediately
        self._perform_update_all_titlebars()

    def _create_audio_workspace_docks(
        self, factory: Callable[[str, QWidget, str], QDockWidget],
    ) -> None:
        """Create independent hosts for the Mixer workspace."""
        for dock_attr in ("dock_audio_console", "dock_audio_viewer",
                          "dock_audio_timeline", "dock_audio_clip_editor",
                          "dock_audio_media_pool", "dock_audio_effects", "dock_audio_properties"):
            setattr(self, dock_attr, None)
        page = getattr(self, "mixer_page", None)
        if not getattr(self, "_mixer_available", False) or page is None:
            return
        # The feature directory can be physically absent from other products.
        from src.features.mixer.module import (
            MIXER_PANEL_SPECS, MIXER_PANEL_TITLE_KEYS, MIXER_PROJECT_ONLY_KEYS,
        )
        page.project_backed = bool(
            getattr(self, "_edit_available", False)
            and self.edit_page is not None and self.timeline_panel is not None
            and self.program_viewer_widget is not None and self.edit_properties_panel is not None
        )
        object_names = {
            "console": "dockAudioConsole", "viewer": "dockAudioViewer",
            "timeline": "dockAudioTimeline", "clip_editor": "dockAudioClipEditor",
            "media_pool": "dockAudioMediaPool", "effects": "dockAudioEffects",
            "properties": "dockAudioProperties",
        }
        for key, dock_attr, panel_attr, title in MIXER_PANEL_SPECS:
            if not page.project_backed and key in MIXER_PROJECT_ONLY_KEYS:
                continue
            title_key = MIXER_PANEL_TITLE_KEYS.get(key, f"panel.audio_{key}")
            dock_title = I18N.tr(title_key, title)
            if page.project_backed and key == "console":
                panel = QScrollArea()
                panel.setFrameShape(QFrame.Shape.NoFrame)
                panel.setWidgetResizable(True)
                panel.setVerticalScrollBar(CompactVerticalScrollBar())
            elif page.project_backed and key == "clip_editor":
                from src.features.mixer.ui.project_clip_editor import ProjectClipEditor

                panel = ProjectClipEditor()
            elif page.project_backed and key == "effects":
                from src.features.mixer.ui.effects_panel import MixerEffectsPanel

                panel = MixerEffectsPanel()
                panel.effect_activated.connect(self._apply_selected_mixer_effect)
            elif page.project_backed:
                panel = QWidget()
                if key == "properties":
                    panel.setMinimumWidth(self.edit_properties_panel.minimumWidth())
                layout = QVBoxLayout(panel)
                layout.setContentsMargins(0, 0, 0, 0)
            else:
                panel = getattr(page, panel_attr)
            dock = factory(dock_title, panel, object_names[key])
            if key == "timeline":
                dock.setMinimumHeight(TIMELINE_DOCK_MIN_HEIGHT)
            self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea if key == "properties" else
                               Qt.DockWidgetArea.TopDockWidgetArea
                               if key in ("console", "viewer",
                                          "media_pool", "effects") else
                               Qt.DockWidgetArea.BottomDockWidgetArea, dock)
            dock.hide()
            setattr(self, dock_attr, dock)
        if page.project_backed:
            self._bind_project_audio_clip_editor()
            self._bind_mixer_transport()
            self._mixer_dock_resize_timer = QTimer(self)
            self._mixer_dock_resize_timer.setSingleShot(True)
            self._mixer_dock_resize_timer.timeout.connect(self._fit_video_mix_workspace_height)
            for dock in (self.dock_audio_viewer, self.dock_audio_console, self.dock_audio_media_pool):
                dock.installEventFilter(self)

    def _bind_mixer_transport(self) -> None:
        viewport = getattr(self, "edit_viewport", None)
        if viewport is None:
            return
        from src.features.mixer.ui.output_meter import MixerOutputMeter

        meter = MixerOutputMeter()
        viewport.bind_mixer_transport(self.timeline_panel.install_mixer_toolbar(meter))
        self.audio_mixer_panel.output_levels_changed.connect(meter.update_output_levels)
        self.audio_mixer_panel.output_meter_reset.connect(meter.reset_meter)

    def _apply_selected_mixer_effect(self, effect_type: str) -> None:
        """Apply the library effect through the regular timeline command path."""
        if self.current_page != "Mixer":
            return
        from src.features.mixer.ui.selection_effect_dialog import SELECTION_EFFECTS

        view = self.timeline_panel.get_timeline_view()
        timeline = view.get_timeline()
        clip_id = view.selected_audio_clip_id()
        clip = timeline.get_clip_by_id(clip_id) if timeline is not None and clip_id else None
        track = timeline.get_track_by_id(clip.track_id) if clip is not None else None
        if effect_type == "clip_editor" and clip_id and track is not None:
            self._open_project_audio_clip_editor(clip_id)
            return
        if effect_type in SELECTION_EFFECTS:
            if not clip_id or track is None or track.track_type != "audio" or track.is_locked:
                self.statusBar().showMessage(I18N.tr(
                    "mixer.selection_effect.select_clip", "Select an unlocked audio clip first."),
                    4500)
            elif self._load_project_audio_clip_editor(clip_id, activate_workspace=True):
                self._show_project_audio_clip_editor("clip")
                self.dock_audio_clip_editor.widget().open_selection_effect(effect_type)
            return
        if not clip_id or track is None or track.is_locked:
            self.statusBar().showMessage(I18N.tr(
                "mixer.effects_browser.select_clip",
                "Select an unlocked audio clip, or drag the effect to a clip or a mixer channel.",
            ), 4500)
            return
        self._on_edit_effect_add_requested(effect_type, clip_id)

    def _bind_timeline_recording(self) -> None:
        """Bind Edit and Mixer to the same timeline recording workflow."""
        timeline = getattr(self, "timeline_panel", None)
        record = getattr(self, "_on_mixer_transport_record_requested", None)
        if timeline is None or not callable(record):
            return
        view = timeline.get_timeline_view()
        if getattr(self, "_timeline_recording_view", None) is view:
            return
        self._timeline_recording_view = view
        view.track_record_requested.connect(self._on_mixer_timeline_record_requested)
        view.track_recording_input_requested.connect(self._on_mixer_input_requested)
        recording_mode = getattr(self, "_on_timeline_recording_mode_requested", None)
        if callable(recording_mode) and hasattr(timeline, "recording_mode_requested"):
            timeline.recording_mode_requested.connect(recording_mode)
        viewport = getattr(self, "edit_viewport", None)
        if viewport is not None:
            viewport.program_record_requested.connect(record)
        playback = timeline.get_playback_controller()
        playback_state = getattr(self, "_on_mixer_recording_playback_state", None)
        if playback is not None and callable(playback_state):
            playback.state_changed.connect(playback_state)
        display_changed = getattr(self.edit_page, "timeline_display_changed", None)
        if display_changed is not None:
            display_changed.connect(self._sync_mixer_timeline_recorders)
        else:
            view.timeline_changed.connect(self._sync_mixer_timeline_recorders)

    def _bind_project_audio_clip_editor(self) -> None:
        dock = self.dock_audio_clip_editor
        panel = dock.widget()
        view = self.timeline_panel.get_timeline_view()
        view.set_professional_audio_editor_available(True)
        view.audio_clip_editor_requested.connect(self._open_project_audio_clip_editor)
        display_changed = getattr(self.edit_page, "timeline_display_changed", None)
        if display_changed is not None:
            display_changed.connect(panel.sync_clip)
        else:
            view.timeline_changed.connect(panel.sync_clip)
        media_pool = getattr(self, "media_pool_panel", None)
        if media_pool is not None:
            media_pool.project_changed.connect(panel.sync_project)
            media_pool.set_professional_audio_editor_available(True)
            media_pool.professional_audio_editor_requested.connect(self._open_project_audio_source_editor)
        panel.gain_committed.connect(self._commit_mixer_clip_gain)
        panel.fade_committed.connect(self._commit_mixer_clip_fade)
        panel.sample_fade_committed.connect(self._commit_mixer_clip_sample_fade)
        panel.fade_curve_committed.connect(self._commit_mixer_clip_fade_curve)
        panel.fade_end_curve_committed.connect(self._commit_mixer_clip_fade_end_curve)
        # P0-2：试听开始时暂停时间线播放，两套输出不叠音。
        panel.audition_active_changed.connect(self._pause_timeline_for_clip_audition)
        # P0-3：处理栈提交走时间线命令（旁路/删除/清空可撤销）。
        panel.audio_edit_committed.connect(self._commit_mixer_clip_audio_edit)
        # §6.2：改长度提交走项目/序列事务（一次撤销还原长度与后移）。
        panel.length_change_committed.connect(self._commit_mixer_clip_length_change)
        panel.effect_add_requested.connect(self._on_edit_effect_add_requested)
        panel.effect_settings_requested.connect(self._on_edit_effect_open_dialog_requested)
        panel.effect_enabled_changed.connect(self._on_edit_effect_enabled_changed)
        panel.effect_reorder_requested.connect(self._on_edit_effect_reorder_requested)
        panel.effect_remove_requested.connect(self._on_edit_effect_remove_requested)
        panel.undo_requested.connect(view.undo)
        panel.redo_requested.connect(view.redo)
        panel.float_requested.connect(lambda: self._show_project_audio_clip_editor("floating"))
        panel.tab_requested.connect(self._tab_project_audio_clip_editor)
        panel.timeline_requested.connect(self._raise_mixer_audio_timeline)
        self._audio_clip_editor_placed = False
        self._opening_audio_clip_editor = False
        self.tabifiedDockWidgetActivated.connect(self._on_mixer_audio_dock_activated)
        dock.topLevelChanged.connect(self._sync_mixer_clip_editor_dock_mode)
        dock.visibilityChanged.connect(self._on_project_audio_clip_editor_visibility_changed)

    def _pause_timeline_for_clip_audition(self, active: bool) -> None:
        """Clip Editor 试听开始时暂停时间线播放（P0-2 §2.3）。"""
        if not active:
            return
        manager = getattr(getattr(self, "edit_page", None), "_audio_manager", None)
        pause = getattr(manager, "pause", None)
        if callable(pause):
            try:
                pause()
            except RuntimeError:
                pass

    def _load_project_audio_clip_editor(self, clip_id: str, *, activate_workspace: bool = False) -> bool:
        view = self.timeline_panel.get_timeline_view()
        timeline = view.get_timeline()
        clip = timeline.get_clip_by_id(clip_id) if timeline is not None else None
        track = timeline.get_track_by_id(clip.track_id) if clip is not None else None
        project = getattr(self, "_current_edit_project", None)
        asset = project.assets.get(clip.asset_id) if clip is not None and project is not None else None
        if track is None or track.track_type != "audio" or asset is None:
            self.statusBar().showMessage(I18N.tr("mixer.clip_editor.offline", "Audio media is unavailable"), 4000)
            return False
        if activate_workspace and not self._activate_workspace_for_audio_editor():
            return False
        self.dock_audio_clip_editor.widget().open_clip(timeline, clip, asset, project=project)
        return True

    def _open_project_audio_clip_editor(self, clip_id: str) -> None:
        if not self._load_project_audio_clip_editor(clip_id, activate_workspace=True):
            return
        self._show_project_audio_clip_editor("clip")

    def _on_mixer_audio_dock_activated(self, active_dock: QDockWidget) -> None:
        dock = self.dock_audio_clip_editor
        if active_dock not in (dock, self.dock_audio_timeline) or self.current_page != "Mixer":
            return
        self._sync_mixer_clip_editor_dock_mode()
        if active_dock is dock:
            self._load_selected_audio_clip_for_editor()

    def _on_project_audio_clip_editor_visibility_changed(self, visible: bool) -> None:
        dock = self.dock_audio_clip_editor
        editor = dock.widget()
        if editor is not None:
            editor.set_dock_active(visible)
        if visible and self.current_page == "Mixer" and not self.tabifiedDockWidgets(dock):
            self._load_selected_audio_clip_for_editor()

    def _load_selected_audio_clip_for_editor(self) -> None:
        if self._opening_audio_clip_editor:
            return
        panel = self.dock_audio_clip_editor.widget()
        clip_id = self.timeline_panel.get_timeline_view().selected_audio_clip_id()
        if clip_id is not None and self._load_project_audio_clip_editor(clip_id):
            return
        if clip_id is not None or panel.clip_id is not None:
            panel.reset()

    def _sync_mixer_clip_editor_dock_mode(self, _floating: bool = False) -> None:
        dock = self.dock_audio_clip_editor
        floating = dock.isFloating()
        if floating:
            was_visible = dock.isVisible()
            # Qt recomputes the dock's window flags when its title bar changes.
            # Suppress selection reloads during the internal hide/show transition.
            with QSignalBlocker(dock):
                if not isinstance(dock.titleBarWidget(), FloatingDockTitleBar):
                    dock.setTitleBarWidget(FloatingDockTitleBar(dock))
                top_hint = Qt.WindowType.WindowStaysOnTopHint
                if not dock.windowFlags() & top_hint:
                    dock.setWindowFlag(top_hint)
                if was_visible and not dock.isVisible():
                    dock.show()
        tabbed = not floating and self.dock_audio_timeline in self.tabifiedDockWidgets(dock)
        dock.widget().set_dock_mode(tabbed=tabbed)

    def _open_project_audio_source_editor(self, asset_id: str) -> None:
        project = getattr(self, "_current_edit_project", None)
        asset = project.assets.get(asset_id) if project is not None else None
        if asset is None or not asset.is_audio or not self._activate_workspace_for_audio_editor():
            return
        self.dock_audio_clip_editor.widget().open_source(asset, project=project)
        self._show_project_audio_clip_editor("source")

    def _activate_workspace_for_audio_editor(self) -> bool:
        if self.current_page != "Mixer":
            self.switch_to_page("Mixer")
        return self.current_page == "Mixer"

    def _show_project_audio_clip_editor(self, placement: Literal["clip", "source", "tabbed", "floating"]) -> None:
        dock = self.dock_audio_clip_editor
        self._opening_audio_clip_editor = True
        try:
            first_open = not (self._audio_clip_editor_placed or dock.isVisible() or dock.isFloating()
                              or self.tabifiedDockWidgets(dock))
            if placement == "tabbed" or (placement == "clip" and first_open):
                self._dock_project_audio_clip_editor_with_timeline()
            elif placement == "floating":
                dock.setFloating(True)
            elif placement == "source" and first_open:
                dock.setFloating(True)
                dock.resize(1220, 780)
            self._audio_clip_editor_placed = True
            self._sync_mixer_clip_editor_dock_mode()
            self._focus_project_audio_clip_editor()
        finally:
            self._opening_audio_clip_editor = False
        if dock.isFloating():
            # The native context menu may be relinquishing focus this turn.
            QTimer.singleShot(0, self._refocus_floating_audio_clip_editor)

    def _focus_project_audio_clip_editor(self) -> None:
        dock = self.dock_audio_clip_editor
        if dock.isFloating() and dock.isMinimized():
            dock.showNormal()
        dock.show()
        if dock.isFloating():
            self._ensure_window_on_screen(dock)
        dock.raise_()
        if dock.isFloating():
            dock.activateWindow()
        dock.widget().focus_waveform()

    def _refocus_floating_audio_clip_editor(self) -> None:
        dock = getattr(self, "dock_audio_clip_editor", None)
        if dock is not None and self.current_page == "Mixer" and dock.isFloating() and dock.isVisible():
            self._focus_project_audio_clip_editor()

    def _clear_project_audio_clip_editor(self) -> None:
        dock = getattr(self, "dock_audio_clip_editor", None)
        if dock is not None and getattr(getattr(self, "mixer_page", None), "project_backed", False):
            dock.widget().reset()
            dock.hide()

    def _tab_project_audio_clip_editor(self) -> None:
        self._show_project_audio_clip_editor("tabbed")

    def _dock_project_audio_clip_editor_with_timeline(self) -> None:
        dock = self.dock_audio_clip_editor
        timeline = self.dock_audio_timeline
        dock.setFloating(False)
        if timeline not in self.tabifiedDockWidgets(dock):
            self.tabifyDockWidget(timeline, dock)
        self._sync_mixer_clip_editor_dock_mode()
        timeline.show()

    def _raise_mixer_audio_timeline(self) -> None:
        dock = self.dock_audio_clip_editor
        if self.dock_audio_timeline not in self.tabifiedDockWidgets(dock):
            dock.hide()
        self.dock_audio_timeline.show()
        self.dock_audio_timeline.raise_()
        self.timeline_panel.get_timeline_view().setFocus(Qt.FocusReason.OtherFocusReason)

    def _show_audio_adr_dock(self) -> None:
        if self.current_page == "Edit":
            effects = getattr(self, "edit_effects_panel", None)
            effects_dock = getattr(self, "dock_edit_effects", None)
            if effects is not None and effects_dock is not None:
                self._sync_adr_panel_host()
                effects.select_subtitles()
                effects_dock.show()
                effects_dock.raise_()
            return
        dock = getattr(self, "dock_adr_take_sheet", None)
        if dock is None or self.current_page != "Mixer":
            return
        self._sync_adr_panel_host()
        # Qt drops tab group membership while every dock in the group is
        # hidden, so tabifiedDockWidgets() cannot tell where the ADR dock
        # lives. Fall back to its area: on Mixer the right stack is reserved
        # for the (hidden) Properties dock, so give ADR its own left slot.
        if self.current_page == "Mixer" and self.dockWidgetArea(dock) == Qt.DockWidgetArea.RightDockWidgetArea:
            self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, dock)
        dock.show()
        dock.raise_()

    def _sync_adr_panel_host(self) -> None:
        panel = getattr(self, "adr_take_sheet_panel", None)
        dock = getattr(self, "dock_adr_take_sheet", None)
        if panel is None or dock is None:
            return
        effects = getattr(self, "edit_effects_panel", None)
        if self.current_page == "Edit" and effects is not None:
            # Layouts saved before the move can still open the standalone ADR dock.
            reopen_subtitles = not dock.isHidden()
            dock.hide()
            effects.host_voiceover_panel(panel)
            self._wire_adr_panel()
            if reopen_subtitles and (effects_dock := getattr(self, "dock_edit_effects", None)) is not None:
                effects.select_subtitles()
                effects_dock.show()
                effects_dock.raise_()
        elif self.current_page == "Mixer" and panel.parentWidget() is not dock.widget():
            panel.leave_caption_workspace()
            dock.widget().layout().addWidget(panel)
            panel.show()

    def _on_adr_dock_visibility_changed(self, visible: bool) -> None:
        if visible:
            self._wire_adr_panel()

    def _attach_edit_panels_to_mixer(self) -> None:
        """Present the live sequence and transport in the mixing workspace."""
        if not getattr(getattr(self, "mixer_page", None), "project_backed", False):
            return
        self.program_viewer_widget.get_viewer().exit_fullscreen()
        if self.edit_viewport is not None:
            self.edit_viewport.set_program_only(True)
        self.timeline_panel.set_display_mode("mixer")
        self.timeline_panel.get_timeline_view().set_audio_only(True)
        audio_inspector = getattr(self.edit_properties_panel, "set_audio_workspace", None)
        if callable(audio_inspector):
            audio_inspector(True)
        self.audio_mixer_panel.close_effect_editors()
        console = self.dock_audio_console.widget()
        console.setWidget(self.audio_mixer_panel)
        panel_minimum = self.audio_mixer_panel.minimumSizeHint().width()
        console.setMinimumWidth(panel_minimum + console.verticalScrollBar().sizeHint().width())
        self.audio_mixer_panel.show()
        borrowed: list[tuple[QDockWidget, QWidget]] = [
            (self.dock_audio_viewer, self.edit_viewport or self.program_viewer_widget),
            (self.dock_audio_timeline, self.timeline_panel),
            (self.dock_audio_properties, self.edit_properties_panel),
        ]
        media_pool_dock = getattr(self, "dock_audio_media_pool", None)
        media_pool_panel = getattr(self, "media_pool_panel", None)
        if media_pool_dock is not None and media_pool_panel is not None:
            media_pool_panel.enter_audio_workspace()
            borrowed.append((media_pool_dock, media_pool_panel))
        for dock, widget in borrowed:
            dock.widget().layout().addWidget(widget)
            widget.show()

    def _restore_edit_panels_from_mixer(self) -> None:
        """Return the shared widgets before restoring the destination dock state."""
        if not getattr(getattr(self, "mixer_page", None), "project_backed", False):
            return
        self.program_viewer_widget.get_viewer().exit_fullscreen()
        self.audio_mixer_panel.close_effect_editors()
        clip_effect_dialogs = getattr(self, "_clip_audio_fx_dialog_mgr", None)
        if clip_effect_dialogs is not None:
            clip_effect_dialogs.close_all()
        self.dock_audio_clip_editor.widget().reset()
        audio_inspector = getattr(self.edit_properties_panel, "set_audio_workspace", None)
        if callable(audio_inspector):
            audio_inspector(False)
        self.timeline_panel.get_timeline_view().set_audio_only(False)
        self.timeline_panel.set_display_mode("edit")
        self.dock_audio_console.widget().takeWidget()
        for dock, widget in (
            (self.dock_audio_mixer, self.audio_mixer_panel),
            (self.dock_edit_timeline, self.timeline_panel),
            (self.dock_properties, self.edit_properties_panel),
        ):
            dock.setWidget(widget)
            widget.show()
        media_pool_dock = getattr(self, "dock_audio_media_pool", None)
        media_pool_panel = getattr(self, "media_pool_panel", None)
        if (media_pool_dock is not None and media_pool_panel is not None
                and getattr(self, "dock_edit_media_pool", None) is not None):
            media_pool_panel.leave_audio_workspace()
            self.dock_edit_media_pool.setWidget(media_pool_panel)
            media_pool_panel.show()
        viewport = self.edit_viewport or self.program_viewer_widget
        self.dock_program_viewer.setWidget(viewport)
        if self.edit_viewport is not None:
            self.edit_viewport.set_program_only(False)
        viewport.show()

    def _audio_workspace_docks(self) -> tuple[QDockWidget, ...]:
        """Include optional docks for visibility and layout persistence."""
        return tuple(dock for attr in (
            "dock_audio_console", "dock_audio_viewer", "dock_audio_timeline",
            "dock_audio_clip_editor", "dock_audio_media_pool", "dock_audio_effects",
            "dock_audio_properties",
        ) if isinstance((dock := getattr(self, attr, None)), QDockWidget))

    def _seed_audio_workspace_layout(self) -> bool:
        """Capture the factory layout without disturbing the active page."""
        docks = self._audio_workspace_docks()
        if len(docks) < 3:
            return False
        if getattr(self, "_mixer_default_layout", None) is not None:
            self.page_layouts.setdefault("Mixer", self._mixer_default_layout)
            return True
        console = getattr(self, "dock_audio_console", None)
        viewer = getattr(self, "dock_audio_viewer", None)
        timeline = getattr(self, "dock_audio_timeline", None)
        if not all(isinstance(d, QDockWidget) for d in (console, viewer, timeline)):
            return False
        previous = self.saveState()
        try:
            self._arrange_video_mix_workspace_layout()
            self._mixer_default_layout = self.saveState()
            self.page_layouts.setdefault("Mixer", self._mixer_default_layout)
        finally:
            self.restoreState(previous)
        return True

    def _detach_audio_workspace_docks(self, *, preserve: bool = False) -> None:
        docks = self._audio_workspace_docks()
        if not preserve:
            for dock in self.all_docks:
                dock.hide()
        for dock in docks:
            dock.hide()
            dock.setFloating(False)
            self.removeDockWidget(dock)

    def _arrange_audio_workspace_layout(
        self, *, compact: bool = False, preserve: bool = False,
    ) -> None:
        """Build the Editing Layout rows with media in the upper-left."""
        console, viewer, timeline = self.dock_audio_console, self.dock_audio_viewer, self.dock_audio_timeline
        pool, effects = self.dock_audio_media_pool, self.dock_audio_effects
        editor, properties = self.dock_audio_clip_editor, self.dock_audio_properties
        docks = self._audio_workspace_docks()
        shown = {dock for dock in docks if not dock.isHidden()} if preserve else set(docks)
        window_rect = self.contentsRect()
        front = {dock for dock in shown if dock.isVisible() and dock.geometry().intersects(window_rect)} if preserve else set()
        left = pool or console
        timeline_tabs = self.tabifiedDockWidgets(timeline) if preserve else ()
        old_left = next((dock for dock in (left, effects, console) if dock in front
                         and dock not in timeline_tabs), left)
        old_widths = {left: old_left.width(), viewer: viewer.width()}
        if properties is not None:
            old_widths[properties] = properties.width()
        self._detach_audio_workspace_docks(preserve=preserve)

        top = Qt.DockWidgetArea.TopDockWidgetArea
        horizontal = Qt.Orientation.Horizontal
        vertical = Qt.Orientation.Vertical
        self.addDockWidget(top, left)
        if compact:
            self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, timeline)
            self.splitDockWidget(left, viewer, horizontal)
        else:
            self.splitDockWidget(left, timeline, vertical)
            self.splitDockWidget(left, viewer, horizontal)
        if properties is not None:
            self.splitDockWidget(viewer, properties, horizontal)
        if effects is not None:
            self.tabifyDockWidget(left, effects)
        if pool is not None and not compact:
            self.tabifyDockWidget(left, console)
        if editor is not None:
            self.tabifyDockWidget(timeline, editor)
        if compact:
            self.tabifyDockWidget(timeline, console)
        for dock in shown:
            dock.show()
        upper_tabs = (left, effects) if compact else (console, left, effects)
        lower_tabs = (console, editor, timeline) if compact else (editor, timeline)
        upper_default = left if compact else console
        # Keep the currently active editing tool when it moves between rows.
        for stack, default in ((upper_tabs, upper_default), (lower_tabs, timeline)):
            active = next((dock for dock in stack if dock in front), default)
            if active in shown:
                active.raise_()
        if editor is not None and getattr(self.mixer_page, "project_backed", False):
            self._sync_mixer_clip_editor_dock_mode()

        layout = self.layout()
        layout.activate()
        geometry = layout.geometry()
        if geometry.isEmpty():
            geometry = self.contentsRect()
        layout.setGeometry(geometry)
        left_dock = next((dock for dock in (left, *upper_tabs) if dock in shown), None)
        columns = [(anchor, dock) for anchor, dock in ((left, left_dock), (viewer, viewer),
                                                        (properties, properties)) if dock in shown]
        widths = old_widths if preserve else self._mixer_editing_default_column_widths(
            geometry.width(), left, viewer, properties,
        )
        if len(columns) > 1:
            self.resizeDocks([dock for _, dock in columns], [max(1, widths[anchor]) for anchor, _ in columns], horizontal)
            layout.setGeometry(geometry)
        upper = next((dock for dock in (viewer, properties, *upper_tabs)
                      if dock in shown and dock.geometry().intersects(window_rect)), None)
        lower = next((dock for dock in (*lower_tabs, timeline)
                      if dock in shown and dock.geometry().intersects(window_rect)), None)
        if not preserve:
            upper, lower = viewer, timeline
        if upper is not None and lower is not None:
            # Include the lower tab bar and any upper-left tab-strip offset.
            available = lower.geometry().bottom() - min(
                dock.y() for dock in (upper, viewer) if dock in shown
            )
            target = round(available * 3 / 5)
            if not compact and console in shown and getattr(self.mixer_page, "project_backed", False):
                mixer_height = self.audio_mixer_panel.minimumSizeHint().height() + _MIXER_DOCK_TAB_HEIGHT
                target = min(target, available - mixer_height)
            self.resizeDocks([upper, lower], [max(1, available - target), max(1, target)], vertical)
            layout.setGeometry(geometry)
            if not preserve and left_dock is not None and properties is not None:
                self._size_mixer_editing_picture(left_dock, viewer, properties, lower, widths[properties], geometry)
        if compact:
            self._order_compact_mixer_tabs()

    def _mixer_editing_default_column_widths(
        self, available: int, left: QDockWidget, viewer: QDockWidget, properties: QDockWidget | None,
    ) -> dict[QDockWidget, int]:
        if properties is None:
            return {left: 1, viewer: 1}
        panel_minimum = max(properties.minimumWidth(), properties.minimumSizeHint().width())
        left_width = max(left.minimumWidth(), left.minimumSizeHint().width())
        viewer_minimum = max(viewer.minimumWidth(), viewer.minimumSizeHint().width())
        property_width = max(panel_minimum, min(round(panel_minimum * 1.5), available - left_width - viewer_minimum))
        return {left: left_width, viewer: max(viewer_minimum, available - left_width - property_width),
                properties: property_width}

    def _size_mixer_editing_picture(
        self, left: QDockWidget, viewer: QDockWidget, properties: QDockWidget, timeline: QDockWidget,
        property_width: int, geometry: QRect,
    ) -> None:
        program = getattr(self, "program_viewer_widget", None)
        if program is None or not viewer.isAncestorOf(program):
            return
        layout = self.layout()
        picture = program.picture_size()
        chrome_width = max(0, viewer.width() - picture.width())
        requested_width = round(picture.height() * 16 / 9) + chrome_width
        separator = max(0, properties.x() - viewer.geometry().right() - 1)
        left_and_viewer = geometry.width() - property_width - 2 * separator
        left_width = max(left.minimumWidth(), left.minimumSizeHint().width(), left_and_viewer - requested_width)
        viewer_width = max(viewer.minimumWidth(), viewer.minimumSizeHint().width(), left_and_viewer - left_width)
        self.resizeDocks([left, viewer, properties], [max(1, left_width), viewer_width, property_width],
                         Qt.Orientation.Horizontal)
        layout.setGeometry(geometry)

        picture = program.picture_size()
        adjustment = round(picture.width() * 9 / 16) - picture.height()
        if abs(adjustment) > 1:
            rows_height = timeline.geometry().bottom() + 1 - viewer.y()
            self.resizeDocks([viewer, timeline], [max(1, viewer.height() + adjustment),
                                                  max(1, rows_height - viewer.height() - adjustment)],
                             Qt.Orientation.Vertical)
            layout.setGeometry(geometry)
        canvas = program.get_viewer()
        if not canvas.is_fit_to_window:
            canvas.fit_to_window()

    def _arrange_video_mix_workspace_layout(self) -> None:
        """Put a small preview above the mixer next to a full-height timeline."""
        console, viewer, timeline = self.dock_audio_console, self.dock_audio_viewer, self.dock_audio_timeline
        pool, effects = self.dock_audio_media_pool, self.dock_audio_effects
        editor, properties = self.dock_audio_clip_editor, self.dock_audio_properties
        self._detach_audio_workspace_docks()

        horizontal = Qt.Orientation.Horizontal
        vertical = Qt.Orientation.Vertical
        self.addDockWidget(Qt.DockWidgetArea.TopDockWidgetArea, viewer)
        self.splitDockWidget(viewer, timeline, horizontal)
        lower = pool or console
        self.splitDockWidget(viewer, lower, vertical)
        if pool is not None:
            self.tabifyDockWidget(pool, console)
            if effects is not None:
                self.tabifyDockWidget(pool, effects)
            if properties is not None:
                self.tabifyDockWidget(pool, properties)
        if editor is not None:
            self.tabifyDockWidget(timeline, editor)
        for dock in self._audio_workspace_docks():
            dock.show()
        layout = self.layout()
        layout.activate()
        console.raise_()
        timeline.raise_()
        if editor is not None and getattr(self.mixer_page, "project_backed", False):
            self._sync_mixer_clip_editor_dock_mode()

        width = self.contentsRect().width()
        sidebar = round(width * 0.34)
        self.resizeDocks([viewer, timeline], [sidebar, width - sidebar], horizontal)
        geometry = layout.geometry()
        if geometry.isEmpty():
            geometry = self.contentsRect()
        layout.setGeometry(geometry)
        left_height = timeline.geometry().bottom() + 1 - viewer.y()
        if left_height <= 0:
            left_height = geometry.height()
        upper = max(1, round(left_height / 3))
        self.resizeDocks([viewer, console], [upper, 2 * upper], vertical)
        layout.setGeometry(geometry)

        program = getattr(self, "program_viewer_widget", None)
        if program is None or not viewer.isAncestorOf(program):
            return
        picture = program.picture_size()
        chrome_width = max(0, viewer.width() - picture.width())
        chrome_height = max(0, viewer.height() - picture.height())
        aspect = 16 / 9
        sidebar = min(sidebar, round(picture.height() * aspect) + chrome_width)
        self.resizeDocks([viewer, timeline], [sidebar, width - sidebar], horizontal)
        layout.setGeometry(geometry)

        # A minimum dock width or a tall window may make width the limiting axis.
        picture_height = round((viewer.width() - chrome_width) / aspect)
        upper = picture_height + chrome_height
        self.resizeDocks([viewer, console], [upper, max(1, left_height - upper)], vertical)
        layout.setGeometry(geometry)
        # The native dock layout can round or redistribute the initial weights.
        correction = picture_height - program.picture_size().height()
        if abs(correction) > 1:
            upper += correction
            self.resizeDocks([viewer, console], [max(1, upper), max(1, left_height - upper)], vertical)
            layout.setGeometry(geometry)
        canvas = program.get_viewer()
        if not canvas.is_fit_to_window:
            canvas.fit_to_window()

    def _mixer_needs_compact_layout(self) -> bool:
        if not getattr(getattr(self, "mixer_page", None), "project_backed", False):
            return False
        height = self.contentsRect().height()
        for widget in (getattr(self, "main_toolbar", None), getattr(self, "status_bar", None)):
            if widget is not None and not widget.isHidden():
                height -= widget.height()
        # Keep the timeline taller than the upper row when both need the space.
        minimum = self.audio_mixer_panel.minimumSizeHint().height() + _MIXER_DOCK_TAB_HEIGHT
        return height < minimum * 2

    def _order_compact_mixer_tabs(self) -> None:
        console, timeline = self.dock_audio_console, self.dock_audio_timeline
        if console not in self.tabifiedDockWidgets(timeline):
            return
        self._order_mixer_tabs(console, timeline, self.dock_audio_clip_editor)

    def _order_mixer_tabs(self, *docks: QDockWidget | None) -> None:
        titles = [dock.windowTitle() for dock in docks if dock is not None and not dock.isHidden()]
        if len(titles) < 2:
            return
        self.layout().activate()
        for bar in self.findChildren(QTabBar, options=Qt.FindChildOption.FindDirectChildrenOnly):
            labels = [bar.tabText(index) for index in range(bar.count())]
            if not set(titles).issubset(labels):
                continue
            for target, title in enumerate(titles):
                source = labels.index(title)
                # Native dock tabs update the dock ordering after each adjacent move.
                for index in range(source, target, -1):
                    bar.moveTab(index, index - 1)
                labels.insert(target, labels.pop(source))
            return

    def _fit_video_mix_workspace_height(self) -> None:
        if (self.current_page != "Mixer" or not getattr(self.mixer_page, "project_backed", False)
                or self.config_manager.get(self._active_page_layout_key()) not in (None, {"preset_id": "mixer_tabbed"})):
            return
        # Wait for the divider drag to end before moving a dock out of its group.
        if QApplication.mouseButtons() & Qt.MouseButton.LeftButton:
            self._mixer_dock_resize_timer.start(50)
            return
        console, pool, timeline = self.dock_audio_console, self.dock_audio_media_pool, self.dock_audio_timeline
        if console.isHidden() or any(dock.isFloating() for dock in (console, pool, timeline)):
            return
        left_tabs = (pool, *self.tabifiedDockWidgets(pool))
        on_left = console in left_tabs
        on_right = console in self.tabifiedDockWidgets(timeline)
        if on_right:
            self._order_compact_mixer_tabs()
        if not (on_left or on_right):
            return
        # Qt parks inactive dock tabs outside the window. Geometry also works
        # when the active tab's child widgets cover its whole visible region.
        window_rect = self.contentsRect()
        left_active = next((dock for dock in left_tabs
                            if dock.isVisible() and dock.geometry().intersects(window_rect)), None)
        if left_active is None:
            return

        minimum = self.audio_mixer_panel.minimumSizeHint().height()
        # If only one left tab is open, moving the mixer back creates a tab bar.
        visible_tabs = sum(not dock.isHidden() for dock in left_tabs)
        return_minimum = minimum + (_MIXER_DOCK_TAB_HEIGHT if visible_tabs == 1 else 0)
        height = left_active.height()
        if not (on_left and height < minimum or on_right and height >= return_minimum):
            return
        destination = timeline if on_left else pool
        destination_tabs = (destination, *self.tabifiedDockWidgets(destination))
        preferred = (*destination_tabs, console) if on_left else (console, *destination_tabs)
        active = next((dock for dock in preferred
                       if dock.isVisible() and dock.geometry().intersects(window_rect)), None)
        self.tabifyDockWidget(destination, console)
        if active is not None:
            active.raise_()
        if on_left:
            self._order_compact_mixer_tabs()
        else:
            self._order_mixer_tabs(pool, console, self.dock_audio_effects, self.dock_audio_properties)

    def _fit_mixer_workspace_height(self) -> None:
        if self.current_page != "Mixer" or not getattr(self, "_mixer_default_layout", None):
            return
        selection = self.config_manager.get(self._active_page_layout_key())
        if selection in (None, {"preset_id": "mixer_tabbed"}):
            self._fit_video_mix_workspace_height()
            return
        if selection != {"preset_id": "factory"}:
            return
        compact = self._mixer_needs_compact_layout()
        current = self.dockWidgetArea(self.dock_audio_timeline) == Qt.DockWidgetArea.BottomDockWidgetArea
        if any(dock.isFloating() for dock in self._audio_workspace_docks()):
            return
        if current != compact:
            self._arrange_audio_workspace_layout(compact=compact, preserve=True)
        elif compact:
            self._order_compact_mixer_tabs()

    def _migrate_mixer_workspace_layout(self) -> None:
        """Upgrade the built-in Mixer once per layout profile."""
        if self.current_page != "Mixer" or not getattr(self, "_mixer_default_layout", None):
            return
        choices = self._page_layout_choices()
        index = self._active_page_layout_index(choices)
        preset_id = choices[index - 1][1] if index else None
        video_mix = preset_id == "mixer_tabbed"
        key, version = ((self._video_mix_workspace_layout_version_key(), _VIDEO_MIX_WORKSPACE_LAYOUT_VERSION)
                        if video_mix else (f"mixer_workspace_layout_version_{self._layout_profile_mode()}",
                                          _MIXER_WORKSPACE_LAYOUT_VERSION))
        if self.config_manager.get(key) == version:
            return
        if preset_id == "factory" and not self.isVisible():
            # The preview's native surface is sized after the window is shown.
            QTimer.singleShot(0, lambda: self._migrate_mixer_workspace_layout() if self.isVisible() else None)
            return
        if preset_id in ("factory", "mixer_tabbed") and not any(dock.isFloating() for dock in self._audio_workspace_docks()):
            if video_mix:
                self._arrange_video_mix_workspace_layout()
            else:
                self._arrange_audio_workspace_layout(compact=self._mixer_needs_compact_layout())
            self._reset_workspace_panel_controls()
            state = save_workspace_dock_state(self, "Mixer")
            self.page_layouts["Mixer"] = state
            self._save_current_layout_to_store("Mixer", qt_state=state, auto_save=False)
        self.config_manager.set(key, version)

    def _video_mix_workspace_layout_version_key(self) -> str:
        # Keep the persisted key so saved Audition-style layouts upgrade in place.
        return f"mixer_audition_layout_version_{self._layout_profile_mode()}"

    def _apply_default_composite_layout(self) -> None:
        """Arrange the Composite workspace for its available editing surface."""
        top = Qt.DockWidgetArea.TopDockWidgetArea
        left = Qt.DockWidgetArea.LeftDockWidgetArea
        right = Qt.DockWidgetArea.RightDockWidgetArea
        bottom = Qt.DockWidgetArea.BottomDockWidgetArea
        horizontal = Qt.Orientation.Horizontal
        vertical = Qt.Orientation.Vertical
        timeline = self.dock_composite_keyframes
        graph = self.dock_composite
        viewer = self.dock_viewport
        inspector = self.dock_node_properties
        media = self.dock_edit_media_pool
        scopes = self.dock_composite_scope
        width, height = self.contentsRect().width(), self.contentsRect().height()
        media_width = max(230, media.minimumWidth(), media.minimumSizeHint().width()) if media else 0
        inspector_width = max(300, inspector.minimumWidth(), inspector.minimumSizeHint().width()) if inspector else 0
        if scopes is not None:
            inspector_width = max(inspector_width, scopes.minimumWidth(), scopes.minimumSizeHint().width())
        viewer_minimum = max(viewer.minimumWidth(), viewer.minimumSizeHint().width())
        compact = timeline is not None and width < media_width + viewer_minimum + inspector_width + 2

        for dock in self.all_docks:
            dock.hide()
        self.setCorner(Qt.Corner.TopLeftCorner, left if timeline else top)
        self.setCorner(Qt.Corner.TopRightCorner, right if timeline else top)
        self.setCorner(Qt.Corner.BottomLeftCorner, bottom)
        self.setCorner(Qt.Corner.BottomRightCorner, bottom)

        primary = [(viewer, top), (inspector, right), (media, left), (timeline, bottom)]
        for dock, area in primary:
            if dock is not None:
                dock.setFloating(False)
                self.addDockWidget(area, dock)
                dock.setVisible(dock is not media or not compact)

        graph.setFloating(False)
        if timeline is not None:
            self.addDockWidget(bottom, graph)
            self.tabifyDockWidget(timeline, graph)
            graph.hide()
        else:
            self.splitDockWidget(viewer, graph, horizontal)
            graph.show()

        # Rebuild the optional groups with their own dock identities so
        # Show Panels and saved Qt state can still address each tool.
        for dock in (
            self.dock_composite_scene_outliner, self.dock_composite_material,
        ):
            if dock is not None:
                dock.setFloating(False)
                self.addDockWidget(right, dock)
                if inspector is not None:
                    self.tabifyDockWidget(inspector, dock)
                dock.hide()

        assets = self.dock_composite_asset_dependency
        report = self.dock_composite_import_report
        for dock in (assets, report):
            if dock is not None:
                dock.setFloating(False)
                self.addDockWidget(right, dock)
                dock.hide()
        if assets is not None and report is not None:
            self.tabifyDockWidget(assets, report)

        spline = self.dock_composite_spline
        if spline is not None:
            spline.setFloating(False)
            self.addDockWidget(bottom, spline)
            spline.hide()

        if scopes is not None:
            scopes.setFloating(False)
            self.addDockWidget(right if timeline is not None else bottom, scopes)
            scopes.show()
            if timeline is not None and inspector is not None:
                self.resizeDocks([inspector, scopes], [370, 220], vertical)

        if timeline is not None:
            layout = self.layout()
            layout.activate()
            for bar in (getattr(self, "main_toolbar", None), getattr(self, "status_bar", None)):
                if bar is not None and not bar.isHidden():
                    height -= bar.height()
            if compact:
                media_width = 0
            column_sizes = ((media, media_width), (viewer, max(1, width - media_width - inspector_width)),
                            (inspector or scopes, inspector_width))
            columns, widths = zip(*((dock, size) for dock, size in column_sizes if dock is not None and size > 0))
            self.resizeDocks(list(columns), list(widths), horizontal)
            timeline_height = max(timeline.minimumHeight(), round(height * 0.38))
            self.resizeDocks([viewer, timeline], [max(1, height - timeline_height), timeline_height], vertical)
            timeline.raise_()
        else:
            self.resizeDocks([viewer, graph], [800, 600], horizontal)

    def _capture_default_composite_layout(self) -> QByteArray:
        """Create the default without changing the currently displayed layout."""
        previous = self.saveState()
        try:
            self._apply_default_composite_layout()
            return self.saveState()
        finally:
            self.restoreState(previous)

    def _migrate_composite_default_layout(self) -> None:
        if self.current_page != "Composite" or not self.dock_composite or not self.dock_composite_keyframes:
            return
        key = f"composite_default_layout_version_{self._layout_profile_mode()}"
        if self.config_manager.get(key) == _COMPOSITE_DEFAULT_LAYOUT_VERSION:
            return
        choices = self._page_layout_choices()
        index = self._active_page_layout_index(choices)
        if not index or choices[index - 1][1] != "factory":
            return
        if any(dock.isFloating() and not dock.isHidden() for dock in self.all_docks):
            return
        self._apply_default_composite_layout()
        self.page_layouts["Composite"] = save_workspace_dock_state(self, "Composite")
        self._save_current_layout_to_store("Composite", auto_save=False)
        self.config_manager.set(key, _COMPOSITE_DEFAULT_LAYOUT_VERSION)

    def _color_program_monitor_available(self) -> bool:
        project = getattr(self, "_current_edit_project", None)
        return (getattr(self, "current_page", None) == "Color"
                and getattr(self, "edit_viewport", None) is not None
                and project is not None
                and project.active_timeline_id in getattr(self, "_current_timeline_sequences", {}))

    def _sync_color_timeline_monitor_action(self) -> None:
        action = self.canvas.viewport_menu.timeline_composite_monitor_act
        if getattr(self, "_color_program_monitor_active", False):
            project = getattr(self, "_current_edit_project", None)
            if (self.current_page != "Color" or project is None or not project.active_timeline_id
                    or project.project_id != self._color_monitor_project_id):
                self.set_color_timeline_composite_monitor(False)
        # A newly selected timeline may materialize its sequence just after
        # the pool's project_changed notification. The user can always close.
        action.setEnabled(getattr(self, "_color_program_monitor_active", False)
                          or self._color_program_monitor_available())

    def _on_color_program_dock_visibility(self, visible: bool) -> None:
        if visible or not getattr(self, "_color_program_monitor_active", False):
            return
        session = self._color_monitor_session

        def finish_hide() -> None:
            # Floating and re-docking briefly hide the same widget during its
            # Qt parent change. A user close stays hidden after that operation.
            if (self._color_monitor_session is session and self.dock_program_viewer.isHidden()):
                self.set_color_timeline_composite_monitor(False)

        QTimer.singleShot(0, finish_hide)

    def set_color_timeline_composite_monitor(self, enabled: bool) -> None:
        """Borrow the existing Program viewer and its single texture consumer on Color."""
        action = self.canvas.viewport_menu.timeline_composite_monitor_act
        enabled = bool(enabled and self._color_program_monitor_available())
        action.setChecked(enabled)
        if enabled == getattr(self, "_color_program_monitor_active", False):
            return
        controller = self.timeline_panel.get_playback_controller()
        slider = self.edit_viewport.program_slider
        dock = self.dock_program_viewer
        if enabled:
            if self.timeline_preview_renderer is None:
                self._init_timeline_preview_renderer()
            if self.timeline_preview_renderer is None:
                action.setChecked(False)
                self.statusBar().showMessage(I18N.tr(
                    "viewport.monitor_unavailable", "Timeline preview could not be initialized"), 4000)
                return
            source = getattr(self, "video_controller", None)
            if source is not None and source.is_playing:
                source.stop_playback()
            self._color_program_monitor_active = True
            self._color_monitor_session = object()
            self._color_program_output_kind = None
            self._color_monitor_project_id = self._current_edit_project.project_id
            self._color_monitor_previous_title = dock.windowTitle()
            dock.setWindowTitle(action.text())
            self.edit_viewport.set_program_only(True, color_monitor=True)
            self.edit_viewport.timeline_monitor_status.setText(
                I18N.tr("viewport.monitor_waiting", "Waiting for timeline frame"))
            self.program_viewer_widget.clear_texture()
            # The hidden Edit timeline deliberately skips controller callbacks to
            # avoid waking its GL viewport. Drive only the visible Program picture.
            self.timeline_panel.set_detached_program_playhead(self.edit_page._on_playhead_changed)
            slider.scrub_started_at.connect(self._begin_color_program_scrub)
            slider.scrub_finished_at.connect(self._finish_color_program_scrub)
            slider.scrub_cancelled.connect(self._cancel_color_program_scrub)
            self.edit_viewport.set_program_position(controller.current_frame)
            dock.show()
            dock.raise_()
            self._request_current_timeline_preview_frame()
        else:
            self._color_program_monitor_active = False
            self._color_monitor_session = None
            self.timeline_panel.set_detached_program_playhead(None)
            slider.scrub_started_at.disconnect(self._begin_color_program_scrub)
            slider.scrub_finished_at.disconnect(self._finish_color_program_scrub)
            slider.scrub_cancelled.disconnect(self._cancel_color_program_scrub)
            if controller.is_playing:
                controller.pause()
            if self.edit_page._scrub_active:
                self.edit_page._on_scrub_finished(controller.current_frame, settle=False)
            elif self.timeline_preview_renderer is not None:
                # A release or a pause can still have an exact frame in flight.
                # Retire its generation before a hidden viewer can take the lease.
                self.timeline_preview_renderer.finish_scrub(controller.current_frame, settle=False)
            if not getattr(getattr(self, "video_controller", None), "is_playing", False):
                self.edit_page.shutdown_audio_playback()
            self.program_viewer_widget.clear_texture()
            self.edit_viewport.set_program_only(False)
            dock.setWindowTitle(self._color_monitor_previous_title)
            dock.hide()
        if hasattr(self, "_sync_timeline_idle_prebuffer"):
            self._sync_timeline_idle_prebuffer()

    def _begin_color_program_scrub(self, frame: int) -> None:
        source = getattr(self, "video_controller", None)
        if source is not None and source.is_playing:
            source.stop_playback()
        controller = self.timeline_panel.get_playback_controller()
        if controller.is_playing:
            controller.pause()
        self.edit_page._on_scrub_started(frame)

    def _finish_color_program_scrub(self, frame: int) -> None:
        if self.edit_page._scrub_active:
            self.edit_page._on_scrub_finished(frame)

    def _cancel_color_program_scrub(self) -> None:
        if self.edit_page._scrub_active:
            self.edit_page._on_scrub_finished(self.edit_viewport.program_slider.value(), settle=False)

    def _sync_color_timeline_selector(self, *_args: object) -> None:
        """Refresh the Color viewport's active-timeline selector."""
        viewport_menu = getattr(getattr(self, "canvas", None), "viewport_menu", None)
        if viewport_menu is None:
            return

        project = getattr(self, "_current_edit_project", None)
        media_pool = getattr(self, "media_pool_panel", None)
        media_pool_project = None
        get_project = getattr(media_pool, "get_project", None)
        if callable(get_project):
            media_pool_project = get_project()
        if project is None or (
            media_pool_project is not None
            and not getattr(project, "timelines", {})
            and getattr(media_pool_project, "timelines", {})
        ):
            project = media_pool_project

        timelines = getattr(project, "timelines", {}) if project is not None else {}
        active_timeline_id = (
            getattr(project, "active_timeline_id", None)
            if project is not None
            else None
        )

        # During contextual Edit → Color entry, the full project replacement
        # can arrive a tick after the page switch. Keep the current EditPage
        # timeline as a temporary fallback so the center control is visible
        # immediately and gets replaced by the project mapping on sync.
        if not timelines:
            edit_page = getattr(self, "edit_page", None)
            current_timeline = getattr(edit_page, "_current_timeline", None)
            timeline_id = getattr(current_timeline, "timeline_id", None)
            if current_timeline is not None and timeline_id:
                timelines = {str(timeline_id): current_timeline}
                active_timeline_id = str(timeline_id)
        viewport_menu.set_timeline_choices(timelines, active_timeline_id)
        if hasattr(viewport_menu, "timeline_composite_monitor_act") and hasattr(self, "_sync_color_timeline_monitor_action"):
            self._sync_color_timeline_monitor_action()

    # ------------------------------------------------------------------
    # initialize_page_layouts
    # ------------------------------------------------------------------
    def initialize_page_layouts(self) -> None:
        """Pre-define the look of each page or restore from config."""
        # Older factory/user profiles predate Mixer. Seed its own layout
        # before loading them, so first entry never inherits a Color layout.
        self._seed_audio_workspace_layout()
        # Legacy Qt states are read below without promoting them to an
        # unscoped profile. The next normal save records a scoped profile.
        self.layout_store = LayoutStore(self.config_manager)
        if _is_video_color(self, "Color"):
            self._clear_legacy_video_color_layouts()
        saved_layouts = self.config_manager.get("page_layouts")
        _factory_geom, factory_state, factory_panels = self._load_factory_layout()
        # page_layouts may be mode-scoped: {effective_mode: {page: hex}}, or a
        # legacy flat {page: hex}. Pick the current mode's bucket (fallback to
        # "all") so image/video layouts stay independent.
        if saved_layouts and isinstance(saved_layouts, dict):
            elm = self._layout_profile_mode()
            mode_bucket = None
            if all(isinstance(v, str) for v in saved_layouts.values()):
                # Legacy flat structure — use as-is for backward compatibility.
                mode_bucket = saved_layouts
            elif isinstance(saved_layouts.get(elm), dict):
                mode_bucket = saved_layouts[elm]
            elif isinstance(saved_layouts.get("all"), dict):
                mode_bucket = saved_layouts["all"]
            if mode_bucket:
                for page, state_hex in mode_bucket.items():
                    if isinstance(state_hex, str):
                        self.page_layouts[page] = QByteArray.fromHex(state_hex.encode())

        # Overlay mode-scoped profiles on the older page-layout mirror.
        self._load_layouts_from_store()

        if _is_video_color(self, "Color"):
            color_state = self.page_layouts.get("Color", QByteArray())
            if not _is_supported_video_color_state(color_state):
                previous = self.saveState()
                self._apply_default_video_color_layout(initial=True)
                self.page_layouts["Color"] = save_workspace_dock_state(self, "Color")
                self.restoreState(previous)
                self._configure_video_color_dock_controls(False)

        edit_key = self._active_page_layout_key("Edit")
        edit_selection = self.config_manager.get(edit_key)
        retired_edit = isinstance(edit_selection, dict) and edit_selection.get("preset_id") in (
            "audio", "captions", "review",
        )
        if self._edit_available and ("Edit" not in self.page_layouts or retired_edit):
            from src.features.edit.core.workspace_presets import EDITING_PRESET

            previous = self.saveState()
            initial_window = not (
                self.config_manager.get("window_geometry") or self.db.get_setting("last_geometry")
            )
            self._arrange_edit_workspace(EDITING_PRESET, initial_window=initial_window)
            self.page_layouts["Edit"] = self.saveState()
            if retired_edit:
                self._save_current_layout_to_store("Edit", auto_save=False)
            self.restoreState(previous)
            self._mark_default_edit_layout()
            if retired_edit or edit_selection is None:
                self.config_manager.set(edit_key, {"preset_id": EDITING_PRESET.preset_id}, auto_save=False)

        # A partial factory file leaves the layer workspace to the live
        # dock layout. User profiles and explicit captures take precedence.
        if (
            factory_state and self._composite_available and self.dock_composite
            and self.dock_composite_keyframes and "Composite" not in self.page_layouts
            and "Composite" not in (factory_panels or {})
        ):
            self.page_layouts["Composite"] = self._capture_default_composite_layout()

        # Seed every missing page before using a partial saved layout.
        # When no factory capture exists for this mode, generate the layout
        # for each page below and overlay these saved states afterwards.
        for page, state_hex in (factory_panels or {}).items():
            if isinstance(state_hex, str) and page not in self.page_layouts:
                self.page_layouts[page] = QByteArray.fromHex(state_hex.encode())
        if factory_state and saved_layouts:
            self.page_layouts.setdefault(self.current_page, QByteArray.fromHex(factory_state.encode()))

            # Hide page-specific docks before restore_layout_state applies global state
            # This prevents composite/edit docks from flashing on the wrong page
            for d in [self.dock_edit_media_pool, self.dock_program_viewer, self.dock_source_viewer,
                      self.dock_edit_timeline, self.dock_edit_info, self.dock_edit_effects, self.dock_properties,
                      self.dock_caption_workspace, self.dock_ai_edit_assistant,
                      self.dock_adr_take_sheet, self.dock_multicam,
                      # BUG-B4-03: 页面专属 dock 补齐：Deliver / Node Properties
                      # 此前不在前置隐藏清单，泄漏进恢复状态时无防线。
                      self.dock_deliver, self.dock_node_properties]:
                if d is not None:
                    d.setVisible(False)
            if self.dock_composite:
                self.dock_composite.setVisible(False)
            if self.dock_perf:
                self.dock_perf.setVisible(False)
            if self.dock_composite_keyframes:
                self.dock_composite_keyframes.setVisible(False)
            if self.dock_composite_spline:
                self.dock_composite_spline.setVisible(False)
            if self.dock_composite_import_report:
                self.dock_composite_import_report.setVisible(False)
            if self.dock_composite_asset_dependency:
                self.dock_composite_asset_dependency.setVisible(False)
            if self.dock_composite_scene_outliner:
                self.dock_composite_scene_outliner.setVisible(False)
            if self.dock_composite_material:
                self.dock_composite_material.setVisible(False)
            if self.dock_composite_scope:
                self.dock_composite_scope.setVisible(False)
            if self.dock_color_timeline_clips:
                self.dock_color_timeline_clips.setVisible(False)

            # Layout state will be applied by restore_layout_state() after restoreGeometry
            return

        # First run (nothing saved): prefer the committed factory default
        # (repo ``default_layout.json``) for the CURRENT work mode.  Video and
        # image modes capture separately; a mode without its own capture (and
        # no "all" bucket) falls through to the hardcoded default below.
        if factory_state:
            if self.current_page not in self.page_layouts:
                self.page_layouts[self.current_page] = QByteArray.fromHex(factory_state.encode())

            # Hide page-specific docks so they don't leak into the restored
            # state, then apply the factory arrangement for the start page.
            for d in [self.dock_edit_media_pool, self.dock_program_viewer, self.dock_source_viewer,
                      self.dock_edit_timeline, self.dock_edit_info, self.dock_edit_effects, self.dock_properties,
                      self.dock_caption_workspace, self.dock_ai_edit_assistant,
                      self.dock_adr_take_sheet, self.dock_multicam,
                      # BUG-B4-03: 页面专属 dock 补齐：Deliver / Node Properties
                      # 此前不在前置隐藏清单，泄漏进恢复状态时无防线。
                      self.dock_deliver, self.dock_node_properties]:
                if d is not None:
                    d.setVisible(False)
            if self.dock_composite:
                self.dock_composite.setVisible(False)
            if self.dock_perf:
                self.dock_perf.setVisible(False)
            if self.dock_composite_keyframes:
                self.dock_composite_keyframes.setVisible(False)
            if self.dock_composite_spline:
                self.dock_composite_spline.setVisible(False)
            if self.dock_composite_import_report:
                self.dock_composite_import_report.setVisible(False)
            if self.dock_composite_asset_dependency:
                self.dock_composite_asset_dependency.setVisible(False)
            if self.dock_composite_scene_outliner:
                self.dock_composite_scene_outliner.setVisible(False)
            if self.dock_composite_material:
                self.dock_composite_material.setVisible(False)
            if self.dock_composite_scope:
                self.dock_composite_scope.setVisible(False)
            if self.dock_color_timeline_clips:
                self.dock_color_timeline_clips.setVisible(False)

            restore_workspace_dock_state(self, self.page_layouts[self.current_page], self.current_page)
            # force=True 与硬编码分支一致：首次启动必须走完整页面激活路径
            # （dock 可见性、enter_page_hook / on_enter）。switch_to_page 会先
            # 把当前（出厂）排列快照进 page_layouts 再原样恢复，播种不会破坏。
            self.switch_to_page(self.current_page, force=True)
            return

        saved_page_layouts = self.page_layouts.copy()
        # Seed the pages without a captured default.
        # Hide page-specific docks before saving Color layout so they
        # don't leak into the Color page's saved state
        for d in [self.dock_edit_media_pool, self.dock_program_viewer, self.dock_source_viewer,
                  self.dock_edit_timeline, self.dock_edit_info, self.dock_edit_effects, self.dock_properties,
                  self.dock_ai_edit_assistant,
                  # BUG-B4-03: 与其余前置/收尾隐藏清单对齐：Deliver /
                  # Node Properties 不得泄漏进 Color 页的保存状态。
                  self.dock_deliver, self.dock_node_properties]:
            if d is not None:
                d.setVisible(False)
        if self.dock_composite:
            self.dock_composite.setVisible(False)
        if self.dock_perf:
            self.dock_perf.setVisible(False)
        if self.dock_composite_keyframes:
            self.dock_composite_keyframes.setVisible(False)
        if self.dock_composite_spline:
            self.dock_composite_spline.setVisible(False)
        if self.dock_composite_import_report:
            self.dock_composite_import_report.setVisible(False)
        if self.dock_composite_asset_dependency:
            self.dock_composite_asset_dependency.setVisible(False)
        if self.dock_composite_scene_outliner:
            self.dock_composite_scene_outliner.setVisible(False)
        if self.dock_composite_material:
            self.dock_composite_material.setVisible(False)
        if self.dock_composite_scope:
            self.dock_composite_scope.setVisible(False)
        # Save current state as base for Color (default page)
        if "Color" not in self.page_layouts:
            self.page_layouts["Color"] = save_workspace_dock_state(self, "Color")

        # 1. Library Layout
        self._apply_default_library_layout()
        self.page_layouts["Library"] = self.saveState()

        # 3. Deliver Layout (P9.3) - conditional
        if self._deliver_available and self._current_work_mode() == "image":
            self._apply_image_deliver_layout(reset=True)
            self.page_layouts["Deliver"] = self.saveState()
            if "Deliver" not in saved_page_layouts:
                self._image_deliver_default_size_pending = True
        elif self._deliver_available and self.dock_deliver:
            self._apply_default_video_deliver_layout()
            self.page_layouts["Deliver"] = self.saveState()

        # 4. Composite Layout (if available)
        if self._composite_available and self.dock_composite:
            self._apply_default_composite_layout()
            self.page_layouts["Composite"] = self.saveState()

        self.page_layouts.update(saved_page_layouts)
        # Ensure current_page has a layout state (for pages other than Color/Library/Deliver)
        if self.current_page not in self.page_layouts:
            self.page_layouts[self.current_page] = save_workspace_dock_state(self, self.current_page)

        # Return to current page (usually Color for first time)
        # Before switching, hide page-specific docks that apply_default_layout made visible
        for d in [self.dock_edit_media_pool, self.dock_program_viewer, self.dock_source_viewer,
                  self.dock_edit_timeline, self.dock_edit_info, self.dock_edit_effects, self.dock_properties,
                  self.dock_ai_edit_assistant,
                  # BUG-B4-03: Deliver 分支与 Composite 分支各自 setVisible(True)
                  # 过 dock_deliver / dock_node_properties，收尾清单此前不含它
                  # 们——前者仅靠 Composite 分支的 hide-all 循环顺带隐藏，后者
                  # 直接泄漏到当前非 Composite 页。此处按设计收口。
                  self.dock_deliver, self.dock_node_properties]:
            if d is not None:
                d.setVisible(False)
        if self.dock_composite:
            self.dock_composite.setVisible(False)
        if self.dock_perf:
            self.dock_perf.setVisible(False)
        if self.dock_composite_import_report:
            self.dock_composite_import_report.setVisible(False)
        if self.dock_composite_asset_dependency:
            self.dock_composite_asset_dependency.setVisible(False)
        if self.dock_composite_scene_outliner:
            self.dock_composite_scene_outliner.setVisible(False)
        if self.dock_composite_material:
            self.dock_composite_material.setVisible(False)
        if self.dock_composite_scope:
            self.dock_composite_scope.setVisible(False)
        if self.current_page in saved_page_layouts:
            # restore_layout_state applies the established page after geometry.
            return
        restore_workspace_dock_state(self, self.page_layouts[self.current_page], self.current_page)
        # force=True so that when the app opens directly on a page (e.g.
        # Composite), switch_to_page runs the full activation path — restoring
        # dock visibility, installing the correct Properties panel widget, and
        # firing enter_page_hook / on_enter(). Without it, the early-return
        # guard in switch_to_page leaves docks hidden and the page
        # uninitialized (Properties panel appears empty until the user
        # switches away and back).
        self.switch_to_page(self.current_page, force=True)

    # ------------------------------------------------------------------
    # LayoutStore helpers
    # ------------------------------------------------------------------
    def _current_product_mode(self) -> str:
        """Return the current product mode string."""
        from src.shared.feature_flags import get_product_mode
        return get_product_mode()

    def _current_work_mode(self) -> str:
        """Return the work mode of the current UI, which changes at restart."""
        active_mode = getattr(self, "_active_work_mode", None)
        if active_mode is not None:
            return active_mode
        from src.shared.feature_flags import get_workspace_work_mode
        try:
            return get_workspace_work_mode(getattr(self, "config_manager", None))
        except Exception:
            return "all"

    def _get_registered_panel_ids(self) -> set[str]:
        """Return the set of currently registered panel IDs."""
        registry = get_panel_registry()
        return {desc.panel_id for desc in registry.get_all()}

    def _layout_profile_mode(self) -> str:
        """Keep Studio image and video panel states in separate profiles."""
        product_mode = self._current_product_mode()
        work_mode = self._current_work_mode()
        if product_mode == "studio" and work_mode in ("image", "video"):
            return f"{product_mode}_{work_mode}"
        return product_mode

    def _get_enabled_features(self) -> set[str]:
        """Return the set of currently enabled feature IDs."""
        if hasattr(self, 'feature_flags') and self.feature_flags:
            return set(self.feature_flags.get_enabled_features())
        return {"color", "library", "edit", "composite", "deliver"}

    def _load_layouts_from_store(self) -> None:
        """Load layout states from LayoutStore into self.page_layouts.

        Only loads profiles for the current product mode. Existing
        entries in page_layouts are preserved if no LayoutStore
        profile exists for that page.
        """
        product_mode = self._layout_profile_mode()
        registered_ids = self._get_registered_panel_ids()
        enabled_features = self._get_enabled_features()

        profiles = self.layout_store.get_profiles_for_mode(product_mode)
        for page_id, profile in profiles.items():
            filtered = self.layout_store.filter_panels(
                profile,
                enabled_features=enabled_features,
                registered_panel_ids=registered_ids,
            )
            if filtered.qt_state_hex:
                self.page_layouts[page_id] = QByteArray.fromHex(
                    filtered.qt_state_hex.encode()
                )

    def _save_current_layout_to_store(
        self, page_id: str, *, qt_state: QByteArray | None = None, auto_save: bool = True,
    ) -> None:
        """Save the current dock layout to LayoutStore.

        Called alongside the legacy page_layouts save so both
        systems stay in sync during the transition period.
        """
        try:
            product_mode = self._layout_profile_mode()
            if qt_state is None:
                qt_state = save_workspace_dock_state(self, page_id)
            qt_state_hex = qt_state.toHex().data().decode()

            # Collect visible panel IDs from registered panels
            registry = get_panel_registry()
            visible_panel_ids: list[str] = []
            for desc in registry.get_all():
                dock = getattr(self, desc.dock_attr, None) if desc.dock_attr else None
                if isinstance(dock, QDockWidget) and not dock.isHidden():
                    visible_panel_ids.append(desc.panel_id)

            profile = LayoutProfile(
                product_mode=product_mode,
                page_id=page_id,
                visible_panels=tuple(visible_panel_ids),
                qt_state_hex=qt_state_hex,
            )
            if not auto_save and profile == self.layout_store.get_profile(product_mode, page_id):
                return
            self.layout_store.save_profile(profile, auto_save=auto_save)
            if not auto_save:
                # Restore from memory on the next visit. Coalesce disk writes
                # when the user tries several pages in quick succession.
                timer = getattr(self, "_layout_profile_save_timer", None)
                if timer is None:
                    timer = QTimer(self)
                    timer.setSingleShot(True)
                    timer.setInterval(350)
                    timer.timeout.connect(self.config_manager.save_config)
                    self._layout_profile_save_timer = timer
                timer.start()
        except Exception as exc:
            logger.warning("Failed to save layout to LayoutStore: %s", exc)

    def _restore_layout_from_store(self, page_id: str) -> bool:
        """Try to restore layout from LayoutStore.

        The older unscoped profile is a fallback for installs that have not
        yet saved a mode-specific layout. Once a mode bucket exists, the
        per-page legacy reader handles missing profiles for that mode.
        """
        try:
            profile_mode = self._layout_profile_mode()
            product_mode = self._current_product_mode()
            registered_ids = self._get_registered_panel_ids()
            enabled_features = self._get_enabled_features()

            profile = self.layout_store.get_or_default(
                profile_mode, page_id,
                enabled_features=enabled_features,
                registered_panel_ids=registered_ids,
            )
            if not profile.qt_state_hex and profile_mode != product_mode:
                legacy = self.config_manager.get("page_layouts")
                has_mode_buckets = isinstance(legacy, dict) and any(
                    isinstance(legacy.get(key), dict) for key in ("studio_image", "studio_video")
                )
                if not has_mode_buckets:
                    profile = self.layout_store.get_or_default(
                        product_mode, page_id,
                        enabled_features=enabled_features,
                        registered_panel_ids=registered_ids,
                    )

            if profile and profile.qt_state_hex:
                state = QByteArray.fromHex(profile.qt_state_hex.encode())
                if restore_workspace_dock_state(self, state, page_id):
                    return True
                logger.debug("LayoutStore restoreState failed for %s/%s", profile_mode, page_id)
        except Exception as exc:
            logger.warning("LayoutStore restore failed (non-fatal): %s", exc)

        return False

    def _migrate_edit_properties_dock_width(self) -> None:
        """Repair the legacy oversized Edit Properties dock exactly once.

        Older saved ``QMainWindow`` states can retain a Properties splitter
        width that leaves the Program Viewer with very little usable space.
        The panel itself intentionally has no maximum width, so a one-shot
        migration is the correct repair: it updates the old saved state, then
        never interferes with manual splitter changes or playback.
        """
        migration_version = self.config_manager.get(
            _EDIT_PROPERTIES_DOCK_LAYOUT_MIGRATION_KEY
        )
        if (
            isinstance(migration_version, int)
            and migration_version >= _EDIT_PROPERTIES_DOCK_LAYOUT_MIGRATION_VERSION
        ):
            return

        dock = self.dock_properties
        if (
            self.current_page != "Edit"
            or dock is None
            or dock.isFloating()
            or self.dockWidgetArea(dock) != Qt.DockWidgetArea.RightDockWidgetArea
        ):
            return

        self.resizeDocks(
            [dock],
            [_EDIT_PROPERTIES_DOCK_TARGET_WIDTH],
            Qt.Orientation.Horizontal,
        )

        # Persist the corrected state into both layout stores immediately, so
        # a restart cannot restore the broken proportions again.
        edit_state = self.saveState()
        self.page_layouts["Edit"] = edit_state
        saved_layouts = self.config_manager.get("page_layouts")
        if isinstance(saved_layouts, dict):
            saved_layouts = dict(saved_layouts)
            saved_layouts["Edit"] = edit_state.toHex().data().decode()
            self.config_manager.set("page_layouts", saved_layouts, auto_save=False)

        self.config_manager.set(
            _EDIT_PROPERTIES_DOCK_LAYOUT_MIGRATION_KEY,
            _EDIT_PROPERTIES_DOCK_LAYOUT_MIGRATION_VERSION,
            auto_save=False,
        )
        self._save_current_layout_to_store("Edit")
        self.config_manager.save_config()
        logger.info(
            "Migrated legacy Edit Properties dock width to %dpx",
            _EDIT_PROPERTIES_DOCK_TARGET_WIDTH,
        )

    def _migrate_edit_info_dock_visibility(self) -> None:
        """Add the new Info tab once to existing saved Edit layouts.

        Qt layouts saved before this dock existed do not contain its object
        name, so restoring them leaves the newly created dock hidden.  This
        one-shot migration makes it available without later overriding a
        user's choice to close or float the panel.
        """
        migration_version = self.config_manager.get(
            _EDIT_INFO_DOCK_LAYOUT_MIGRATION_KEY
        )
        if (
            isinstance(migration_version, int)
            and migration_version >= _EDIT_INFO_DOCK_LAYOUT_MIGRATION_VERSION
        ):
            return

        dock = getattr(self, "dock_edit_info", None)
        if self.current_page != "Edit" or dock is None:
            return

        dock.setVisible(True)
        self.page_layouts["Edit"] = self.saveState()
        self.config_manager.set(
            _EDIT_INFO_DOCK_LAYOUT_MIGRATION_KEY,
            _EDIT_INFO_DOCK_LAYOUT_MIGRATION_VERSION,
            auto_save=False,
        )
        self._save_current_layout_to_store("Edit")
        self.config_manager.save_config()
        logger.info("Added Edit Info dock to legacy layout")

    def _migrate_composite_scopes_dock_visibility(self) -> None:
        """Add the Composite Scopes dock to existing layouts once.

        The dock existed before it was registered with the panel/layout
        system, so older saved Qt states either omit it or restore it hidden.
        Show it once when Composite is entered, persist that repaired state,
        and leave later visibility changes entirely under the user's control.
        """
        migration_version = self.config_manager.get(
            _COMPOSITE_SCOPES_DOCK_LAYOUT_MIGRATION_KEY
        )
        if (
            isinstance(migration_version, int)
            and migration_version >= _COMPOSITE_SCOPES_DOCK_LAYOUT_MIGRATION_VERSION
        ):
            return

        dock = getattr(self, "dock_composite_scope", None)
        if self.current_page != "Composite" or dock is None:
            return

        dock.setVisible(True)
        composite_state = self.saveState()
        self.page_layouts["Composite"] = composite_state

        # Keep the legacy page_layouts mirror valid for both its historical
        # flat shape and the newer mode-scoped shape.
        saved_layouts = self.config_manager.get("page_layouts")
        if isinstance(saved_layouts, dict):
            saved_layouts = dict(saved_layouts)
            state_hex = composite_state.toHex().data().decode()
            if all(isinstance(value, str) for value in saved_layouts.values()):
                saved_layouts["Composite"] = state_hex
            else:
                mode_key = self._layout_profile_mode()
                bucket = saved_layouts.get(mode_key)
                if not isinstance(bucket, dict):
                    mode_key = "all"
                    bucket = saved_layouts.get(mode_key)
                if isinstance(bucket, dict):
                    bucket = dict(bucket)
                    bucket["Composite"] = state_hex
                    saved_layouts[mode_key] = bucket
            self.config_manager.set("page_layouts", saved_layouts, auto_save=False)

        self.config_manager.set(
            _COMPOSITE_SCOPES_DOCK_LAYOUT_MIGRATION_KEY,
            _COMPOSITE_SCOPES_DOCK_LAYOUT_MIGRATION_VERSION,
            auto_save=False,
        )
        self._save_current_layout_to_store("Composite")
        self.config_manager.save_config()
        logger.info("Added Composite Scopes dock to legacy layout")

    def _migrate_ai_assistant_dock_layout(self) -> None:
        """Merge the AI assistant into the Edit right-side tab group once.

        Early versions persisted an outer ``dockAIEditAssistant`` wrapper;
        the current panel is itself the dock and is named
        ``dock_ai_edit_assistant``.  Restoring those older states loses the
        tab relationship.  In addition, rebuilding the default Edit layout
        re-adds Properties, which also detaches it from hidden tabs.  Repair
        that legacy state once without overriding a later user choice to move
        or float either dock.
        """
        migration_version = self.config_manager.get(
            _AI_ASSISTANT_DOCK_LAYOUT_MIGRATION_KEY
        )
        if (
            isinstance(migration_version, int)
            and migration_version >= _AI_ASSISTANT_DOCK_LAYOUT_MIGRATION_VERSION
        ):
            return

        properties_dock = self.dock_properties
        assistant_dock = self.dock_ai_edit_assistant
        if (
            self.current_page != "Edit"
            or properties_dock is None
            or assistant_dock is None
            or properties_dock.isFloating()
            or assistant_dock.isFloating()
            or self.dockWidgetArea(properties_dock)
            != Qt.DockWidgetArea.RightDockWidgetArea
            or self.dockWidgetArea(assistant_dock)
            != Qt.DockWidgetArea.RightDockWidgetArea
        ):
            return

        # Calling tabify while the assistant is hidden preserves that hidden
        # state, but records the shared splitter/tab stack for its next show.
        self.tabifyDockWidget(properties_dock, assistant_dock)

        edit_state = self.saveState()
        self.page_layouts["Edit"] = edit_state
        saved_layouts = self.config_manager.get("page_layouts")
        if isinstance(saved_layouts, dict):
            saved_layouts = dict(saved_layouts)
            saved_layouts["Edit"] = edit_state.toHex().data().decode()
            self.config_manager.set("page_layouts", saved_layouts, auto_save=False)

        self.config_manager.set(
            _AI_ASSISTANT_DOCK_LAYOUT_MIGRATION_KEY,
            _AI_ASSISTANT_DOCK_LAYOUT_MIGRATION_VERSION,
            auto_save=False,
        )
        self._save_current_layout_to_store("Edit")
        self.config_manager.save_config()
        logger.info("Migrated AI assistant dock into the Edit tab group")

    def _migrate_edit_timeline_dock_height(self) -> None:
        """Repair a stale, oversized Edit Timeline splitter once.

        ``QMainWindow.saveState`` stores dock splitter proportions separately
        from the per-dock size backup.  A legacy Edit state can therefore
        restore Timeline to most of the window even though its saved dock
        height is much smaller.  The splitter must be resized with *both*
        vertical neighbours: passing Timeline by itself does not establish a
        new splitter ratio in Qt and can leave the oversized state intact.
        This is intentionally a one-shot repair, so later manual splitter
        adjustments remain intact.
        """
        migration_version = self.config_manager.get(
            _EDIT_TIMELINE_DOCK_LAYOUT_MIGRATION_KEY
        )
        if (
            isinstance(migration_version, int)
            and migration_version >= _EDIT_TIMELINE_DOCK_LAYOUT_MIGRATION_VERSION
        ):
            return

        dock = self.dock_edit_timeline
        if (
            self.current_page != "Edit"
            or dock is None
            or dock.isFloating()
            or self.dockWidgetArea(dock) != Qt.DockWidgetArea.BottomDockWidgetArea
        ):
            return

        program_dock = self.dock_program_viewer
        if (
            program_dock is None
            or program_dock.isFloating()
            or self.dockWidgetArea(program_dock)
            != Qt.DockWidgetArea.TopDockWidgetArea
        ):
            return

        saved_height: int | None = None
        saved_size = self.config_manager.get(
            f"dock_{dock.objectName()}_docked_size"
        )
        if isinstance(saved_size, str):
            try:
                _, saved_height_text = saved_size.split("x", 1)
                saved_height = int(saved_height_text)
            except ValueError:
                logger.warning("Ignoring invalid Timeline dock size: %r", saved_size)

        # A previously broken layout may already have been written back as a
        # giant per-dock value (for example 938px).  Treat values outside the
        # valid range as corrupted and restore the known-good default rather
        # than clamping the corrupted value to the maximum.
        if (
            saved_height is None
            or not _EDIT_TIMELINE_DOCK_MIN_HEIGHT
            <= saved_height
            <= _EDIT_TIMELINE_DOCK_MAX_HEIGHT
        ):
            target_height = TIMELINE_DOCK_DEFAULT_HEIGHT
        else:
            target_height = saved_height
        # Only migrate when the saved splitter state clearly disagrees with
        # the user's recorded dock height.  This avoids resetting a deliberately
        # tall Timeline layout created after the migration is available.
        current_height = dock.height()
        undersized = (
            current_height < TIMELINE_DOCK_MIN_HEIGHT
            and target_height < TIMELINE_DOCK_MIN_HEIGHT
        )
        if undersized:
            # APP-03：过矮布局（如 102px）无法编辑——拉回可操作默认高度
            target_height = TIMELINE_DOCK_DEFAULT_HEIGHT
        elif current_height <= max(
            target_height + 200, int(target_height * 1.6)
        ):
            return

        available_height = program_dock.height() + current_height
        program_target_height = max(
            program_dock.minimumHeight(),
            available_height - target_height,
        )
        self.resizeDocks(
            [program_dock, dock],
            [program_target_height, target_height],
            Qt.Orientation.Vertical,
        )

        edit_state = self.saveState()
        self.page_layouts["Edit"] = edit_state
        saved_layouts = self.config_manager.get("page_layouts")
        if isinstance(saved_layouts, dict):
            saved_layouts = dict(saved_layouts)
            saved_layouts["Edit"] = edit_state.toHex().data().decode()
            self.config_manager.set("page_layouts", saved_layouts, auto_save=False)

        self.config_manager.set(
            _EDIT_TIMELINE_DOCK_LAYOUT_MIGRATION_KEY,
            _EDIT_TIMELINE_DOCK_LAYOUT_MIGRATION_VERSION,
            auto_save=False,
        )
        self._save_current_layout_to_store("Edit")
        self.config_manager.save_config()
        logger.info(
            "Migrated oversized Edit Timeline dock from %dpx to %dpx",
            current_height, target_height,
        )

    def _migrate_color_grading_dock_height(self) -> None:
        """Apply the default bottom row height once, including its dock tabs."""
        if (self.current_page != "Color" or _is_video_color(self, self.current_page)
                or self._current_work_mode() == "image"):
            return
        migration_version = self.config_manager.get(
            _COLOR_GRADING_DOCK_LAYOUT_MIGRATION_KEY
        )
        if (
            not getattr(self, "_color_tool_height_reset_pending", False)
            and isinstance(migration_version, int)
            and migration_version >= _COLOR_GRADING_DOCK_LAYOUT_MIGRATION_VERSION
        ):
            return

        # The factory layout may have just hidden or regrouped sibling tabs.
        for candidate in self.all_docks:
            if isinstance(candidate, ColorToolDockWidget):
                candidate.sync_height_limit()

        dock = next((
            candidate for candidate in self.all_docks
            if candidate.objectName() in _COLOR_TOOL_DOCK_NAMES
            and not candidate.isHidden() and not candidate.isFloating()
            and self.dockWidgetArea(candidate) == Qt.DockWidgetArea.BottomDockWidgetArea
            and self.rect().intersects(candidate.geometry())
        ), None)
        viewport_dock = self.dock_viewport
        if (
            dock is None
            or viewport_dock is None
            or viewport_dock.isFloating()
            or viewport_dock.isHidden()
            or self.dockWidgetArea(viewport_dock)
            != Qt.DockWidgetArea.TopDockWidgetArea
        ):
            return

        current_height = dock.height() + dock.tab_height()
        available_height = viewport_dock.height() + current_height
        target_height = min(
            COLOR_TOOL_DOCK_DEFAULT_HEIGHT,
            available_height - viewport_dock.minimumHeight(),
        )
        if target_height <= 0:
            return
        if current_height != target_height:
            self.resizeDocks(
                [viewport_dock, dock],
                [available_height - target_height, target_height],
                Qt.Orientation.Vertical,
            )

        color_state = self.saveState()
        self.page_layouts["Color"] = color_state
        saved_layouts = self.config_manager.get("page_layouts")
        if isinstance(saved_layouts, dict):
            saved_layouts = dict(saved_layouts)
            saved_layouts["Color"] = color_state.toHex().data().decode()
            self.config_manager.set("page_layouts", saved_layouts, auto_save=False)

        self.config_manager.set(
            _COLOR_GRADING_DOCK_LAYOUT_MIGRATION_KEY,
            _COLOR_GRADING_DOCK_LAYOUT_MIGRATION_VERSION,
            auto_save=False,
        )
        self.config_manager.set(
            f"dock_{dock.objectName()}_docked_size",
            f"{dock.width()}x{dock.height()}",
            auto_save=False,
        )
        self._color_tool_height_reset_pending = False
        self._save_current_layout_to_store("Color")
        self.config_manager.save_config()
        QTimer.singleShot(100, self._save_all_docked_sizes)
        logger.info(
            "Applied Color tools dock height %dpx (previously %dpx)",
            target_height, current_height,
        )

    def _migrate_color_remapping_dock_layout(self, force: bool = False) -> None:
        """Add the newly independent Mixer once, preserving Warper's layout.

        Version 1 merged the tools; version 2 splits them. Warper retains the
        old serialized dock identity, including in named/factory Qt layouts.
        Only Mixer needs initial placement. Later launches honor both docks'
        independent visibility, floating geometry and user tab arrangements.
        """
        if _is_video_color(self, "Color") or (
            self._current_work_mode() == "image"
            and self.dock_warper in self.tabifiedDockWidgets(self.dock_curves)
            and self.dockWidgetArea(self.dock_warper) == Qt.DockWidgetArea.TopDockWidgetArea
        ):
            return
        self._migrate_color_resource_dock_layout(force=force)
        self._place_grade_sets_dock(force=force)
        migration_version = self.config_manager.get(
            _COLOR_REMAPPING_DOCK_LAYOUT_MIGRATION_KEY
        )
        if (
            not force
            and isinstance(migration_version, int)
            and migration_version >= _COLOR_REMAPPING_DOCK_LAYOUT_MIGRATION_VERSION
        ):
            self._compact_factory_color_tools()
            return

        if self.current_page != "Color":
            return

        warper = getattr(self, "dock_warper", None)
        mixer = getattr(self, "dock_mixer", None)
        if warper is None or mixer is None:
            return

        if self._current_work_mode() != "image" or mixer not in self.tabifiedDockWidgets(warper):
            was_hidden = warper.isHidden()
            mixer.setFloating(False)
            self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, mixer)
            if not warper.isFloating():
                self.tabifyDockWidget(warper, mixer)
            mixer.setVisible(True)
            if was_hidden:
                warper.hide()
            elif not warper.isFloating():
                warper.raise_()

        self._compact_factory_color_tools(force=force)
        color_state = self.saveState()
        self.page_layouts["Color"] = color_state
        saved_layouts = self.config_manager.get("page_layouts")
        if isinstance(saved_layouts, dict):
            saved_layouts = dict(saved_layouts)
            if all(isinstance(value, str) for value in saved_layouts.values()):
                saved_layouts["Color"] = color_state.toHex().data().decode()
                self.config_manager.set(
                    "page_layouts", saved_layouts, auto_save=False
                )

        self.config_manager.set(
            _COLOR_REMAPPING_DOCK_LAYOUT_MIGRATION_KEY,
            _COLOR_REMAPPING_DOCK_LAYOUT_MIGRATION_VERSION,
            auto_save=False,
        )
        self._save_current_layout_to_store("Color")
        self.config_manager.save_config()
        logger.info("Split Color Mapping into independent Warper and Mixer docks")

    def _compact_factory_color_tools(self, *, force: bool = False) -> None:
        if self.current_page != "Color" or self._current_work_mode() == "image":
            return
        if not force and (getattr(self, "_has_saved_state", False)
                          or getattr(self, "_factory_color_tools_compacted", False)):
            return
        bar = getattr(self, "workspace_panel_bar", None)
        if bar is not None:
            bar.compact_default_color_tools()
            self._factory_color_tools_compacted = True

    def _toggle_color_node_display_mode(self) -> None:
        if not _is_video_color(self, self.current_page):
            self.dock_viewport.setVisible(self.dock_viewport.isHidden())

    def _migrate_color_resource_dock_layout(self, force: bool = False) -> None:
        """One-time upgrade; later launches preserve custom/floating arrangements."""
        key = "color_resource_browser_layout_v1"
        if self.current_page != "Color" or (not force and self.config_manager.get(key) == 1):
            return
        gallery = getattr(self, "dock_presets", None)
        if gallery is None:
            return
        if (self._current_work_mode() == "image" and not gallery.isFloating()
                and all(dock in self.tabifiedDockWidgets(gallery) for dock in (self.dock_lut, self.dock_plugin))):
            self.config_manager.set(key, 1)
            return
        if force:
            gallery.setFloating(False)
        if not gallery.isFloating() and self.dockWidgetArea(gallery) != Qt.DockWidgetArea.LeftDockWidgetArea:
            self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, gallery)
        for dock in (self.dock_lut, self.dock_plugin):
            if dock.isFloating() and not force:
                continue
            if force:
                dock.setFloating(False)
            self.removeDockWidget(dock)
            self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, dock)
            if not gallery.isFloating():
                self.tabifyDockWidget(gallery, dock)
            dock.show()
        self.dock_layers.show()
        self.dock_layers.raise_()
        self.page_layouts["Color"] = self.saveState()
        self.config_manager.set(key, 1, auto_save=False)
        self._save_current_layout_to_store("Color")
        self.config_manager.save_config()

    def _place_grade_sets_dock(self, *, force: bool = False) -> None:
        """Seat the new tab once, then let saved dock layouts own its position."""
        dock = getattr(self, "dock_grade_sets", None)
        key = f"grade_sets_dock_placed_{self._layout_profile_mode()}"
        if dock is None or self.current_page != "Color" or (not force and self.config_manager.get(key)):
            return
        if dock not in self.tabifiedDockWidgets(self.dock_presets):
            self._tabify_grade_sets_dock()
        self.config_manager.set(key, True, auto_save=False)
        self.page_layouts["Color"] = self.saveState()
        self._save_current_layout_to_store("Color")
        self.config_manager.save_config()

    def _tabify_grade_sets_dock(self) -> None:
        dock = getattr(self, "dock_grade_sets", None)
        if dock is None:
            return
        anchor = self.dock_presets
        area = self.dockWidgetArea(anchor)
        if area == Qt.DockWidgetArea.NoDockWidgetArea:
            area = Qt.DockWidgetArea.LeftDockWidgetArea
        dock.setFloating(False)
        self.addDockWidget(area, dock)
        if not anchor.isFloating():
            self.tabifyDockWidget(anchor, dock)
        dock.show()
        if not anchor.isHidden() and not anchor.isFloating():
            anchor.raise_()

    def reset_product_layout(self) -> None:
        """Clear all saved layouts for the current product mode only.

        Layouts for other product modes are preserved.
        """
        product_mode = self._layout_profile_mode()
        profiles = self.layout_store.get_profiles_for_mode(product_mode)
        for page_id in list(profiles.keys()):
            self.layout_store.delete_profile(product_mode, page_id)

        # Also clear the legacy page_layouts dict entries
        for page_id in list(self.page_layouts.keys()):
            del self.page_layouts[page_id]

        logger.info("Reset all layouts for product mode: %s", product_mode)

    # ------------------------------------------------------------------
    # update_all_dock_titlebars
    # ------------------------------------------------------------------
    def update_all_dock_titlebars(self) -> None:
        """Debounced titlebar update to prevent recursive signal loops."""
        if not self._updating_titlebars:
            self._titlebar_update_timer.start()

    # ------------------------------------------------------------------
    # _on_dock_floating_changed
    # ------------------------------------------------------------------
    def _on_dock_floating_changed(self, is_floating, dock) -> None:
        """Handle dock floating state change - save/restore window size.

        Args:
            is_floating: True if dock is now floating, False if docked
            dock: The QDockWidget that changed state
        """
        obj_name = dock.objectName()

        if is_floating:
            # Restore saved floating window size
            dock_size_key = f"dock_{obj_name}_floating_size"
            saved_size = self.config_manager.get(dock_size_key)
            if saved_size:
                # [AUD-2602] 解析失败是预期的坏配置路径：捕获具体异常并
                # 记录原始值，不再用裸 except 连同 dock 已销毁等意外错误
                # 一起吞掉；非正尺寸也拒绝应用。
                try:
                    width, height = map(int, str(saved_size).split('x'))
                except (TypeError, ValueError):
                    logger.warning(
                        "Ignoring corrupt floating size %r for dock %s",
                        saved_size, obj_name)
                else:
                    if width > 0 and height > 0:
                        dock.resize(width, height)
                    else:
                        logger.warning(
                            "Ignoring non-positive floating size %r for "
                            "dock %s", saved_size, obj_name)

            # Also restore position if available
            dock_pos_key = f"dock_{obj_name}_floating_pos"
            saved_pos = self.config_manager.get(dock_pos_key)
            if saved_pos:
                try:
                    x, y = map(int, str(saved_pos).split(','))
                except (TypeError, ValueError):
                    logger.warning(
                        "Ignoring corrupt floating position %r for dock %s",
                        saved_pos, obj_name)
                else:
                    # 负坐标合法（显示器在主屏左侧/上方），不拒绝
                    dock.move(x, y)
            # [AUD-2600] 恢复值可能指向已不存在的显示器（换显示器/DPI 变
            # 化后），复用主窗口恢复路径的钳制逻辑把浮动窗口拉回可见屏。
            self._ensure_window_on_screen(dock)
        else:
            # [AUD-2600] 停靠瞬间不回写 floating_* 配置键：此时 dock 已被
            # reparent 进主窗口，x()/y() 是主窗口子部件坐标而非屏幕坐标，
            # width()/height() 也不是浮动窗口尺寸——旧代码在此写入的值会
            # 覆盖 file_controller.closeEvent 保存的「真正浮动几何」，
            # 下次浮动跳到无关位置甚至屏幕外。浮动几何的正确持久化时机
            # 是仍处于浮动状态时（closeEvent 对浮动中 dock 的保存路径）。
            pass

        # NOTE: 标题栏切换必须异步执行，不能在此同步调用 setTitleBarWidget。
        # 此槽直连 topLevelChanged 信号——拖动浮动窗口重新嵌入主窗口时，Qt 在
        # 嵌入瞬间触发 topLevelChanged(False)；若此时同步更换标题栏，会打断 Qt
        # 内部的几何/状态协商，导致 dock 弹回浮动态（表现为浮动后无法拖回合并）。
        # 延迟到下一轮事件循环，让 Qt 完整完成嵌入协商后再统一更新标题栏。
        QTimer.singleShot(0, self.update_all_dock_titlebars)

    # ------------------------------------------------------------------
    # _show_ai_workflow_dialog（P5：dock → 弹窗）
    # ------------------------------------------------------------------
    def _show_ai_workflow_dialog(self) -> None:
        """显示 AI Workflow 弹窗（懒建单例，关闭只 hide 不 delete）。

        首次调用时构造 ``QDialog``，把已建好的 ``ai_workflow_panel`` 移入并
        ``setParent(dialog)``。后续调用只 ``show/raise/activateWindow``。
        runtime/compiler/controller 留在 MainWindow，与 dialog 生命周期解耦
        （弹窗关闭不影响正在跑的 workflow；不破坏"单一 HarnessRuntime"红线）。
        """
        if self._ai_workflow_dialog is None:
            panel = getattr(self, "ai_workflow_panel", None)
            if panel is None:
                return
            from PyQt6.QtWidgets import QDialog, QVBoxLayout
            dlg = QDialog(self)
            dlg.setWindowTitle(I18N.tr("panel.ai_workflow", "AI Workflow"))
            dlg.setMinimumSize(900, 600)
            dlg.resize(1100, 720)
            layout = QVBoxLayout(dlg)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.addWidget(panel)
            panel.setParent(dlg)  # 重定向 parent 到弹窗
            self._ai_workflow_dialog = dlg
            # 工程加载时 panel 可能尚未创建：还原回调暂存了 payload，这里消费。
            pending = getattr(self, "_pending_ai_workflow_payload", None)
            if pending is not None:
                try:
                    self._restore_ai_workflow_payload(pending)
                except Exception:  # noqa: BLE001
                    pass
                finally:
                    self._pending_ai_workflow_payload = None
        self._ai_workflow_dialog.show()
        self._ai_workflow_dialog.raise_()
        self._ai_workflow_dialog.activateWindow()

    # ------------------------------------------------------------------
    # _show_dock_context_menu
    # ------------------------------------------------------------------
    def _show_dock_context_menu(self, pos, dock) -> None:
        """Show right-click context menu for a dock widget.

        Provides Float/Dock toggle and Close actions.
        """
        if self._is_image_library_page():
            return
        name = dock.objectName()
        panel = None
        if name == "dockEditEffects":
            panel = getattr(self, "edit_effects_panel", None)
        elif name == "dockAudioClipEditor":
            panel = dock.widget()
        elif dock in (getattr(self, "dock_audio_mixer", None), getattr(self, "dock_audio_console", None)):
            panel = getattr(self, "audio_mixer_panel", None)
        show_menu = getattr(panel, "show_unified_context_menu", None)
        if callable(show_menu):
            show_menu(dock.mapToGlobal(pos))
            return

        # Skip context menu for docks that have their own internal menus
        if name in ("dockCurves", "dockMasks", "dockEditTimeline"):
            return

        features = QDockWidget.DockWidgetFeature
        if not dock.features() & (features.DockWidgetFloatable | features.DockWidgetClosable):
            return
        menu = QMenu(self)
        menu.setStyleSheet(DARK_THEME)
        add_dock_management_actions(menu, dock)
        menu.exec(dock.mapToGlobal(pos))

    # ------------------------------------------------------------------
    # _style_dock_tab_bars
    # ------------------------------------------------------------------
    def _style_dock_tab_bars(self, *, only_changed: bool = False) -> None:
        """Apply platform-neutral styling to Qt's internal dock tab bars."""
        photo = self.current_page == "Color" and self._current_work_mode() == "image"
        for tab_bar in self.findChildren(QTabBar):
            # QMainWindow creates this internal bar directly under itself;
            # regular panel QTabBars are descendants of their own widgets.
            if tab_bar.parent() is not self:
                continue
            compact = False
            if photo:
                font = tab_bar.font()
                font.setBold(True)
                metrics = QFontMetrics(font)
                normal_width = 16 + sum(metrics.horizontalAdvance(tab_bar.tabText(index)) + 28
                                        for index in range(tab_bar.count()))
                compact = not tab_bar.isVisible() or tab_bar.width() < normal_width
            if not only_changed or tab_bar.property("_topos_dock_tabs_compact") != compact:
                apply_dock_tab_bar_style(tab_bar, compact=compact)
        # Qt can leave a dormant dock tab bar at (0, 0) during a layout
        # restore. Keep the toolbar above those internal widgets so they
        # cannot paint over the menu or intercept its mouse input.
        toolbar = self.findChild(QToolBar, "mainToolbar")
        if toolbar is not None:
            toolbar.raise_()

    # ------------------------------------------------------------------
    # _perform_update_all_titlebars
    # ------------------------------------------------------------------
    def _perform_update_all_titlebars(self) -> None:
        """Update titlebars based on dock state - with recursion protection.

        - 浮动 dock：安装 FloatingDockTitleBar（有真实高度的拖拽把手）
        - 停靠 dock：1px 高 QWidget 占位（标题栏由 tab 承载）
        只在状态实际变化时更新，避免无谓的 setTitleBarWidget 调用。
        """
        # Prevent recursive calls that cause performance issues
        if self._updating_titlebars:
            return

        self._updating_titlebars = True
        try:
            # Use the explicit list of docks we created.
            # AUD-2610: getattr 的默认值表达式每次都会求值——原先的写法在
            # all_docks 存在时也无条件执行 findChildren(QDockWidget) 全树
            # 枚举再丢弃结果（本函数经 50ms 去抖后仍会在每次 dock 移动/
            # 可见性变化后执行）。改为惰性回退。
            docks_to_check = getattr(self, 'all_docks', None) or self.findChildren(QDockWidget)

            for dock in docks_to_check:
                if not isinstance(dock, QDockWidget):
                    continue

                # Detect floating state
                is_floating = dock.isFloating()
                if isinstance(dock, ColorToolDockWidget):
                    dock.sync_height_limit(is_floating)

                if is_floating:
                    # Floating dock: 必须用一个有真实高度的自定义标题栏，而不能
                    # 调用 setTitleBarWidget(None) 依赖原生标题栏。原因：项目全局
                    # QSS 把 QDockWidget::title 的 max-height 钉死成 0px，Qt 在有
                    # QSS 时对浮动 dock 也走样式化绘制，导致原生标题栏被压成 0 高度
                    # ——浮动窗口顶部没有任何可抓取的拖拽区域，既无法移动也无法拖
                    # 回主窗口停靠。FloatingDockTitleBar 提供真实高度（28px）的拖拽
                    # 把手，QDockWidget 会自动接管其上的鼠标拖拽以移动窗口并触发
                    # 边缘重新停靠判定。
                    current_tw = dock.titleBarWidget()
                    if not isinstance(current_tw, FloatingDockTitleBar):
                        if dock is getattr(self, "dock_audio_clip_editor", None):
                            self._sync_mixer_clip_editor_dock_mode()
                        else:
                            dock.setTitleBarWidget(FloatingDockTitleBar(dock))
                else:
                    # Docked/tabified: use minimal height titlebar for dragging (1px)
                    current_tw = dock.titleBarWidget()
                    needs_update = False

                    # Check if we need to update: no titlebar, wrong type, or not 1px height
                    if current_tw is None:
                        needs_update = True
                    elif isinstance(current_tw, FloatingDockTitleBar):
                        # 刚从浮动切回停靠，标题栏需换回 1px
                        needs_update = True
                    elif not isinstance(current_tw, QWidget):
                        needs_update = True
                    else:
                        # 用 maximumHeight() 判定是否已是 1px 标题栏：
                        # setFixedHeight(1) 同时设定 minimumHeight 与 maximumHeight，
                        # maximumHeight() 会稳定返回 1（minimumSizeHint() 对无 layout
                        # 的 QWidget 返回 -1，永远 != 1，会反复误判重建）。
                        if current_tw.maximumHeight() != 1:
                            needs_update = True

                    if needs_update:
                        hidden_titlebar = QWidget()
                        hidden_titlebar.setFixedHeight(1)  # 1px for dragging
                        dock.setTitleBarWidget(hidden_titlebar)
        finally:
            # Always reset the flag, even if an error occurred
            self._updating_titlebars = False

        self._style_dock_tab_bars()

    def _raise_color_keyframes(self, checked: bool) -> None:
        if not checked:
            return
        dock = self.dock_color_keyframes
        if _is_video_color(self, self.current_page):
            dock.raise_()
            return
        if (
            (getattr(self, "_color_keyframes_reseat", False)
             or not self.config_manager.get(_COLOR_KEYFRAMES_DOCK_PLACEMENT_KEY))
            and not self.dock_analysis.isFloating()
        ):
            # Old layouts have no slot for the new dock. Place it once when
            # opened, keeping the arrangement of the other Color tools.
            self.tabifyDockWidget(self.dock_analysis, dock)
            dock.raise_()
            self.page_layouts["Color"] = self.saveState()
            self._color_keyframes_reseat = False
            self.config_manager.set(_COLOR_KEYFRAMES_DOCK_PLACEMENT_KEY, True)
        dock.raise_()

    def _resize_color_keyframes(self, expanded: bool) -> None:
        dock = self.dock_color_keyframes
        if expanded:
            self._color_keyframes_compact_width = dock.width()
            screen_width = dock.screen().availableGeometry().width()
            target = max(dock.width(), round((screen_width if dock.isFloating() else self.width()) * 0.62))
        else:
            target = self._color_keyframes_compact_width or 360
        if dock.isFloating():
            dock.resize(target, dock.height())
        else:
            self.resizeDocks([dock], [target], Qt.Orientation.Horizontal)

    # ------------------------------------------------------------------
    # apply_default_layout
    # ------------------------------------------------------------------
    def apply_default_layout(self) -> None:
        """Hardcoded default layout with separated tool docks.

        Work-mode aware: keeps the image-only panels (Library, Batch Export)
        hidden in video mode, and the video-only panel (Color Timeline Clips)
        hidden in image mode, so the first-run layout matches the workflow.
        """
        work_mode = self._current_work_mode()
        if _is_video_color(self, "Color"):
            self._apply_default_video_color_layout()
            return
        if work_mode == "image":
            self._apply_default_photo_color_layout()
            return

        for d in self.all_docks:
            d.setVisible(True)
            d.setFloating(False)

        # Work-mode scoped docks — keep _enforce_page_dock_visibility semantics
        # in the hardcoded fallback so there is no first-run flash of panels
        # that do not belong to the active workflow.
        if work_mode == "video":
            # Video workflow: hide image-only Library, Batch Export and the
            # image-only tool panels (Deblur / Crop / Info).
            for attr in ("dock_library", "dock_export",
                         "dock_deblur", "dock_crop", "dock_info"):
                d = getattr(self, attr, None)
                if d is not None:
                    d.setVisible(False)

        # Assign basic areas
        self.addDockWidget(Qt.DockWidgetArea.TopDockWidgetArea, self.dock_viewport)
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, self.dock_library)
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, self.dock_files)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.dock_info)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.dock_layers)

        # Tabify Left Side Panels (Library, Files, and Presets)
        self.tabifyDockWidget(self.dock_library, self.dock_files)
        self.tabifyDockWidget(self.dock_files, self.dock_presets)
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, self.dock_lut)
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, self.dock_plugin)
        self.tabifyDockWidget(self.dock_presets, self.dock_lut)
        self.tabifyDockWidget(self.dock_lut, self.dock_plugin)
        self._tabify_grade_sets_dock()

        # Bottom Area Distribution
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self.dock_grading)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self.dock_base_grade)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self.dock_chromogen)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self.dock_denoise)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self.dock_deblur)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self.dock_curves)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self.dock_warper)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self.dock_mixer)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self.dock_effects)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self.dock_grain)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self.dock_masks)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self.dock_qualifier)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self.dock_analysis)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self.dock_raw)

        # Tabify Bottom Tools
        self.tabifyDockWidget(self.dock_denoise, self.dock_deblur)
        self.tabifyDockWidget(self.dock_deblur, self.dock_grading)
        self.tabifyDockWidget(self.dock_grading, self.dock_base_grade)
        self.tabifyDockWidget(self.dock_base_grade, self.dock_chromogen)
        self.tabifyDockWidget(self.dock_grading, self.dock_curves)
        self.tabifyDockWidget(self.dock_curves, self.dock_warper)
        self.tabifyDockWidget(self.dock_warper, self.dock_mixer)
        self.tabifyDockWidget(self.dock_mixer, self.dock_effects)
        self.tabifyDockWidget(self.dock_effects, self.dock_grain)
        self.tabifyDockWidget(self.dock_grain, self.dock_masks)
        self.tabifyDockWidget(self.dock_masks, self.dock_qualifier)
        self.tabifyDockWidget(self.dock_qualifier, self.dock_raw)
        # dock_crop is kept separate and not tabified, allowing user to drag it freely

        # NEW: Tabify Right Side Panels (Info and Layers)
        self.tabifyDockWidget(self.dock_info, self.dock_layers)

        # Default Focus
        self.dock_grading.raise_()
        self.dock_layers.raise_()

        # Split UI sections
        self.splitDockWidget(self.dock_analysis, self.dock_denoise, Qt.Orientation.Horizontal)
        self.tabifyDockWidget(self.dock_analysis, self.dock_color_keyframes)
        self.dock_color_keyframes.hide()

        # Attempt to set proportions
        self.resizeDocks([self.dock_analysis, self.dock_denoise], [400, 800], Qt.Orientation.Horizontal)
        self.resizeDocks([self.dock_library, self.dock_viewport, self.dock_info], [300, 1000, 300], Qt.Orientation.Horizontal)

        # Update dock titlebars after layout is applied
        self._enforce_work_mode_dock_visibility()
        self._perform_update_all_titlebars()

        # Save default dock sizes to config
        self._save_all_docked_sizes()

    def _clear_legacy_video_color_layouts(self) -> None:
        """Retire old video Color snapshots while retaining other page layouts."""
        config = self.config_manager
        mode = self._layout_profile_mode()
        version_key = f"{_VIDEO_COLOR_LAYOUT_KEY}_{mode}"
        if config.get(version_key) == _PREVIOUS_VIDEO_COLOR_LAYOUT_VERSION:
            return
        layouts = config.get("page_layouts")
        if isinstance(layouts, dict) and isinstance(layouts.get(mode), dict):
            scoped = dict(layouts[mode])
            scoped.pop("Color", None)
            config.set("page_layouts", {**layouts, mode: scoped}, auto_save=False)
        self.layout_store.delete_profile(mode, "Color")
        for name in self.db.list_layouts(work_mode="video"):
            try:
                geometry, state, pages = self.db.load_layout(name, work_mode="video")
            except json.JSONDecodeError:
                logger.warning("Skipping malformed video layout preset %r", name)
                continue
            if not isinstance(pages, dict) or not isinstance(color := pages.get("Color"), str):
                continue
            if _is_supported_video_color_state(QByteArray.fromHex(color.encode())):
                continue
            remaining = {page: capture for page, capture in pages.items() if page != "Color"}
            if remaining:
                self.db.save_layout(name, geometry, next(iter(remaining.values())) if state == color else state,
                                    remaining, work_mode="video")
            else:
                self.db.delete_layout(name, work_mode="video")
        config.set(f"workspace_panel_restore_{mode}_Color_{self.dock_presets.objectName()}", None, auto_save=False)
        config.set(version_key, _PREVIOUS_VIDEO_COLOR_LAYOUT_VERSION)

    def _configure_video_color_dock_controls(self, active: bool) -> None:
        """Keep required panels open and limit child panels to their tab groups."""
        feature = QDockWidget.DockWidgetFeature
        for attr in ("dock_viewport", *_VIDEO_COLOR_TAB_DOCKS):
            dock = getattr(self, attr, None)
            if dock is None:
                continue
            original = dock.property("videoColorOriginalFeatures")
            if active and original is None:
                dock.setProperty("videoColorOriginalFeatures", dock.features().value)
                dock.setFeatures(feature.DockWidgetClosable if attr == "dock_mixer"
                                 else feature.NoDockWidgetFeatures)
            elif not active and original is not None:
                dock.setFeatures(feature(original))
                dock.setProperty("videoColorOriginalFeatures", None)
            dock.toggleViewAction().setVisible(not active or attr == "dock_mixer")

        width, height = (400, 225) if active else (_VIEWPORT_MIN_WIDTH, _VIEWPORT_MIN_HEIGHT)
        self.dock_viewport.setMinimumSize(width, height)
        self.viewport_container.setMinimumSize(width, height)
        handles = getattr(self, "_video_color_resize_handles", None)
        if handles is None and active:
            handles = VideoColorResizeHandles(
                self, gallery=self._video_color_docks(_VIDEO_COLOR_GALLERY),
                viewer=self.dock_viewport, nodes=self.dock_layers, clips=self.dock_color_timeline_clips,
                primary=self._video_color_docks(VIDEO_COLOR_PRIMARY),
                secondary=self._video_color_docks(_VIDEO_COLOR_SECONDARY_DOCKS),
                scopes=self._video_color_docks(VIDEO_COLOR_SCOPES),
                fit_layout=self._fit_video_color_layout,
            )
            self._video_color_resize_handles = handles
        if handles is not None:
            handles.set_active(active)
        if active:
            screen = self.screen()
            if screen is not None and screen.availableGeometry().width() < _VIDEO_COLOR_DUAL_TOOL_WIDTH:
                self._fit_video_color_tools_to_screen(screen.availableGeometry().width())
            for dock in (self.dock_viewport, self.dock_grading, self.dock_curves):
                if dock.isHidden():
                    dock.show()

    def _video_color_docks(self, names: tuple[str, ...]) -> tuple[QDockWidget, ...]:
        return tuple(dock for name in names if (dock := getattr(self, name, None)) is not None)

    def _read_workspace_defaults(
        self, section: str, fallback: dict[str, tuple[int, ...]],
    ) -> dict[str, tuple[int, ...]]:
        defaults = dict(fallback)
        try:
            with open(self._get_factory_layout_path(), encoding="utf-8") as file:
                data = json.load(file)
        except (OSError, json.JSONDecodeError):
            return defaults
        section_data = data.get(section) if isinstance(data, dict) else None
        if isinstance(section_data, dict):
            for key, default in fallback.items():
                values = section_data.get(key)
                if (isinstance(values, list) and len(values) == len(default)
                        and all(type(value) is int and value > 0 for value in values)):
                    defaults[key] = tuple(values)
        return defaults

    def _video_color_defaults(self) -> dict[str, tuple[int, ...]]:
        return self._read_workspace_defaults("video_color", _VIDEO_COLOR_DEFAULTS)

    def _video_project_screen_tier(
        self, defaults: dict[str, tuple[int, ...]],
    ) -> tuple[str, tuple[int, int]]:
        screen = self.screen()
        if screen is None:
            return "window_2k", defaults["window_2k"]
        geometry, available = screen.geometry(), screen.availableGeometry()
        dpr = screen.devicePixelRatio()
        pixels = round(geometry.width() * dpr), round(geometry.height() * dpr)
        # Leave room for the native window frame inside the OS work area.
        width_limit = max(1, available.width() - 24)
        height_limit = max(1, available.height() - 30)
        for key, min_width, min_height in _VIDEO_PROJECT_SCREEN_TIERS:
            width, height = defaults[key]
            if (pixels[0] >= min_width and pixels[1] >= min_height
                    and width <= width_limit and height <= height_limit):
                return key, (width, height)
        width, height = defaults["window_compact"]
        return "window_compact", (min(width, width_limit), min(height, height_limit))

    def _video_project_window_size(self) -> tuple[int, int]:
        return self._video_project_screen_tier(self._video_color_defaults())[1]

    def _apply_video_project_window_size(self) -> None:
        if self._current_work_mode() != "video" or self.isFullScreen() or self.isMaximized():
            return
        self.resize(*self._video_project_window_size())
        self._ensure_window_on_screen(self)
        if getattr(self, "current_page", None) == "Edit":
            self._configure_edit_resize_handles()

    def _fit_video_color_layout(self, width: int, height: int) -> None:
        self._fit_video_color_tools_to_screen(width)
        clips = self.dock_color_timeline_clips
        strip = clips.widget()
        preferred_extra = getattr(strip, "preferred_height_delta", None)
        if preferred_extra is not None and not clips.isHidden() and not clips.isFloating():
            # Anticipate Qt's minimum window height with the chosen preset.
            # A Large strip can otherwise block resizing before the cutoff.
            preferred_minimum = self.minimumSizeHint().height() + preferred_extra()
            compact_height = max(_VIDEO_COLOR_COMPACT_CLIP_HEIGHT, preferred_minimum + 12)
            strip.set_compact_height(height < compact_height)

    def _fit_video_color_tools_to_screen(self, available_width: int) -> None:
        primary, secondary = self.dock_grading, self.dock_curves
        if primary.isHidden() or secondary.isHidden():
            return
        compact = secondary in self.tabifiedDockWidgets(primary)
        if not compact:
            # Qt can clamp the window above the normal breakpoint when the
            # translated tools need more room. Retain that limit while compact.
            self._video_color_tool_breakpoint = max(
                _VIDEO_COLOR_DUAL_TOOL_WIDTH, self.minimumSizeHint().width() + 1,
            )
        needs_compact = available_width < getattr(
            self, "_video_color_tool_breakpoint", _VIDEO_COLOR_DUAL_TOOL_WIDTH,
        )
        if compact == needs_compact:
            return
        if not needs_compact:
            self._expand_video_color_tools(available_width)
            return
        area = self.dockWidgetArea(primary)
        for dock in self._video_color_docks(_VIDEO_COLOR_SECONDARY_DOCKS):
            visible = not dock.isHidden()
            self.removeDockWidget(dock)
            self.addDockWidget(area, dock)
            self.tabifyDockWidget(primary, dock)
            dock.setVisible(visible)
        primary.raise_()

    def _expand_video_color_tools(self, available_width: int) -> None:
        clips, viewer = self.dock_color_timeline_clips, self.dock_viewport
        primary, secondary, scopes = self.dock_grading, self.dock_curves, self.dock_analysis
        area = self.dockWidgetArea(clips)
        if clips.isFloating() or self.dockWidgetArea(primary) != area:
            return

        groups = (
            (primary, self._video_color_docks(VIDEO_COLOR_PRIMARY)),
            (secondary, self._video_color_docks(_VIDEO_COLOR_SECONDARY_DOCKS)),
            (scopes, self._video_color_docks(VIDEO_COLOR_SCOPES)),
        )
        prior_front = {dock for _, docks in groups for dock in docks
                       if not dock.visibleRegion().isEmpty() and not dock.isFloating()}
        tool_front = next((dock for dock in prior_front if dock not in groups[-1][1]), primary)
        scope_front = next((dock for dock in groups[-1][1] if dock in prior_front), None)
        prior_scope_width = scope_front.width() if scope_front is not None else 0
        prior_view_height = viewer.height()
        prior_tools_height = tool_front.geometry().bottom() - clips.geometry().bottom()
        visibility = {dock: not dock.isHidden() for _, docks in groups for dock in docks if not dock.isFloating()}
        # Leave any separately floating scopes in place. The remaining tabs
        # still belong to the scopes column when the main window grows.
        scopes_anchor = next((dock for dock in groups[-1][1] if dock in visibility), None)
        raw = getattr(self, "dock_raw", None)
        primary_anchor = raw if visibility.get(raw) else primary
        for dock in visibility:
            self.removeDockWidget(dock)

        vertical, horizontal = Qt.Orientation.Vertical, Qt.Orientation.Horizontal
        self.splitDockWidget(clips, primary_anchor, vertical)
        self.splitDockWidget(primary_anchor, secondary, horizontal)
        if scopes_anchor is not None:
            self.splitDockWidget(secondary, scopes_anchor, horizontal)
        for anchor, docks in ((primary_anchor, groups[0][1]), groups[1], (scopes_anchor, groups[-1][1])):
            if anchor is None:
                continue
            for dock in docks:
                if dock not in visibility:
                    continue
                if dock is not anchor:
                    self.tabifyDockWidget(anchor, dock)
                dock.setVisible(visibility[dock])
            anchor.raise_()
        primary.raise_()
        for dock in (tool_front, scope_front):
            if dock is not None:
                dock.raise_()
        if prior_tools_height > 0:
            self.resizeDocks([viewer, primary], [prior_view_height, prior_tools_height], vertical)
        if scope_front is not None:
            tool_minimum = primary.minimumSizeHint().width() + secondary.minimumSizeHint().width()
            scope_width = min(prior_scope_width, max(scope_front.minimumWidth(), available_width - tool_minimum))
            tools_width = (available_width - scope_width) // 2
            self.resizeDocks([primary, secondary, scope_front],
                             [tools_width, tools_width, scope_width], horizontal)

    def _upgrade_video_color_scopes_tabs(self) -> None:
        scopes = self.dock_analysis
        shown, floating = not scopes.isHidden(), scopes.isFloating()
        scopes.setFloating(False)
        scopes.show()
        area = self.dockWidgetArea(scopes)
        tabs = self._video_color_docks(VIDEO_COLOR_SCOPES[1:])
        for dock in tabs:
            self.removeDockWidget(dock)
            self.addDockWidget(area, dock)
            dock.show()
            self.tabifyDockWidget(scopes, dock)
        for dock in (scopes, *tabs):
            dock.setVisible(shown)
        if shown:
            scopes.raise_()
        if floating:
            scopes.setFloating(True)
        self.dock_mixer.hide()

    def _apply_default_video_color_layout(self, *, initial: bool = False) -> None:
        """Build the same six regions for first use and Reset to Default."""
        top = Qt.DockWidgetArea.TopDockWidgetArea
        horizontal, vertical = Qt.Orientation.Horizontal, Qt.Orientation.Vertical
        gallery, viewer, nodes = self.dock_presets, self.dock_viewport, self.dock_layers
        clips, primary, secondary, scopes = (self.dock_color_timeline_clips, self.dock_grading,
                                             self.dock_curves, self.dock_analysis)
        primary_anchor = getattr(self, "dock_raw", primary)
        for dock in self.all_docks:
            dock.hide()
        # Remove all participating docks first, otherwise Qt keeps an old tab
        # relationship when a previously grouped dock is passed to splitDockWidget.
        for attr in ("dock_viewport", "dock_layers", "dock_color_timeline_clips",
                     "dock_analysis", *_VIDEO_COLOR_TAB_DOCKS):
            dock = getattr(self, attr, None)
            if dock is not None:
                dock.setFloating(False)
                self.removeDockWidget(dock)

        self.addDockWidget(top, gallery)
        self.splitDockWidget(gallery, clips, vertical)
        self.splitDockWidget(clips, primary_anchor, vertical)
        self.splitDockWidget(gallery, viewer, horizontal)
        self.splitDockWidget(viewer, nodes, horizontal)
        defaults = self._video_color_defaults()
        width, height = self._video_project_window_size() if initial else (self.width(), self.height())
        screen = self.screen()
        available_width = min(width, screen.availableGeometry().width()) if screen else width
        expanded_tools = available_width >= _VIDEO_COLOR_DUAL_TOOL_WIDTH
        tool_groups = [(primary_anchor, VIDEO_COLOR_PRIMARY)]
        if expanded_tools:
            self.splitDockWidget(primary_anchor, secondary, horizontal)
            tool_groups.append((secondary, _VIDEO_COLOR_SECONDARY_DOCKS))
        else:
            tool_groups[0] = (primary_anchor, (*VIDEO_COLOR_PRIMARY, *_VIDEO_COLOR_SECONDARY_DOCKS))
        self.splitDockWidget(secondary if expanded_tools else primary_anchor, scopes, horizontal)

        for anchor, members in [(gallery, _VIDEO_COLOR_GALLERY), *tool_groups, (scopes, VIDEO_COLOR_SCOPES)]:
            for attr in members:
                dock = getattr(self, attr, None)
                if dock is None:
                    continue
                if dock is not anchor:
                    self.tabifyDockWidget(anchor, dock)
                dock.setVisible(attr not in ("dock_script", "dock_color_keyframes", "dock_mixer"))
            anchor.raise_()
        primary.raise_()
        for dock in (viewer, nodes, clips, scopes):
            dock.show()

        self._configure_video_color_dock_controls(True)
        top_width = available_width
        available_height = max(1, height - clips.sizeHint().height())
        self.resizeDocks([gallery, viewer, nodes],
                         [max(1, top_width * share // sum(defaults["top"]))
                          for share in defaults["top"]], horizontal)
        primary_size, secondary_size, scopes_size = (
            max(1, top_width * share // sum(defaults["bottom"]))
            for share in defaults["bottom"]
        )
        if expanded_tools:
            self.resizeDocks([primary, secondary, scopes],
                             [primary_size, secondary_size, scopes_size], horizontal)
        else:
            self.resizeDocks([primary, scopes], [primary_size + secondary_size, scopes_size], horizontal)
        self.resizeDocks([viewer, primary],
                         [max(1, available_height * share // sum(defaults["height"]))
                          for share in defaults["height"]], vertical)

    def _photo_color_groups(self) -> tuple[
        tuple[QDockWidget, ...], tuple[QDockWidget, ...], tuple[QDockWidget, ...],
        tuple[QDockWidget, ...], tuple[QDockWidget, ...],
    ]:
        resources = tuple(dock for dock in (
            getattr(self, "dock_grade_sets", None), self.dock_presets, self.dock_lut, self.dock_plugin,
        ) if dock is not None)
        return (
            resources, (self.dock_layers, self.dock_info, self.dock_crop),
            (self.dock_raw, self.dock_grading, self.dock_base_grade, self.dock_chromogen,
             self.dock_denoise, self.dock_deblur),
            (self.dock_curves, self.dock_warper, self.dock_mixer,
             self.dock_qualifier, self.dock_masks, self.dock_effects),
            (self.dock_analysis, self.dock_grain),
        )

    def _photo_color_defaults(self) -> dict[str, tuple[int, ...]]:
        defaults = getattr(self, "_photo_color_default_sizes", None)
        if defaults is None:
            defaults = self._read_workspace_defaults("photo_color", _PHOTO_COLOR_DEFAULTS)
            self._photo_color_default_sizes = defaults
        return defaults

    def _photo_color_tabs_match(self, group: tuple[QDockWidget, ...]) -> bool:
        visible = tuple(dock for dock in group if not dock.isHidden())
        if not visible:
            return True
        anchor = visible[0]
        tabs = set(self.tabifiedDockWidgets(anchor))
        return set(visible[1:]) <= tabs <= set(group) - {anchor}

    def _photo_color_stacks(
        self, compact: bool, merge_tools: bool, stack_details: bool = False,
    ) -> tuple[_DockStacks, _DockStacks]:
        resources, right, primary, secondary, analysis = self._photo_color_groups()
        layers, *details = right
        if compact:
            resources = (*resources, self.dock_deblur, self.dock_grain)
            primary = tuple(dock for dock in primary if dock is not self.dock_deblur)
            analysis = (self.dock_analysis, *(details if stack_details else right))
        upper = (resources, (self.dock_viewport,))
        if not compact:
            upper += (tuple(details), (layers,)) if stack_details else (right,)
        lower = ((*primary, *secondary), analysis) if merge_tools else (primary, secondary, analysis)
        if compact and stack_details:
            lower += ((layers,),)
        return upper, lower

    def _photo_color_stacks_for_size(self, width: int, *, initializing: bool = False) -> tuple[_DockStacks, _DockStacks]:
        _, right, primary, secondary, analysis = self._photo_color_groups()
        minimum = sum(max(max(dock.minimumWidth(), dock.minimumSizeHint().width()) for dock in group)
                      for group in (primary, secondary, analysis))
        compact = width < _PHOTO_COLOR_COMPACT_TOP_WIDTH
        height = self.height()
        if compact:
            stack_details = height >= _PHOTO_COLOR_STACKED_COMPACT_WINDOW_HEIGHT
        else:
            preview = self.dock_viewport
            upper_height = (height - COLOR_TOOL_DOCK_DEFAULT_HEIGHT - 60
                            if initializing or preview.isHidden() else preview.height())
            layers, *details = right
            minimum_heights = [max(dock.minimumHeight(), dock.minimumSizeHint().height()) for dock in details]
            stacked_minimum = (max(minimum_heights) + max(layers.minimumHeight(), layers.minimumSizeHint().height())
                               + DOCK_TAB_BAR_HEIGHT + 12)
            stack_details = upper_height >= max(_PHOTO_COLOR_STACKED_DETAILS_HEIGHT, stacked_minimum)
        # Fold before Qt clamps the window at the expanded layout's minimum.
        return self._photo_color_stacks(
            compact, width < max(_PHOTO_COLOR_DUAL_TOOL_WIDTH, minimum + 52), stack_details,
        )

    def _arrange_photo_color(
        self, upper: _DockStacks, lower: _DockStacks, shown: dict[QDockWidget, bool], *,
        previous_view_height: int = 0, previous_tool_height: int = 0,
        selected: tuple[QDockWidget, ...] = (),
    ) -> None:
        top = Qt.DockWidgetArea.TopDockWidgetArea
        horizontal, vertical = Qt.Orientation.Horizontal, Qt.Orientation.Vertical
        for dock in shown:
            self.removeDockWidget(dock)

        # One nested split for both rows lets a vertical divider transfer space
        # between the preview and tools, independent of the side column widths.
        self.addDockWidget(top, upper[0][0])
        upper[0][0].show()
        self.splitDockWidget(upper[0][0], lower[0][0], vertical)
        lower[0][0].show()
        for row in (upper, lower):
            for before, after in zip(row, row[1:]):
                self.splitDockWidget(before[0], after[0],
                                     vertical if after == (self.dock_layers,) else horizontal)
                after[0].show()
            for group in row:
                for dock in group[1:]:
                    self.tabifyDockWidget(group[0], dock)
                    dock.show()
        for dock, visible in shown.items():
            dock.setVisible(visible)

        self._sync_photo_color_height_limits(upper, lower)
        split_lower = lower[-1] == (self.dock_layers,)
        split_upper = upper[-1] == (self.dock_layers,)
        defaults = (self.dock_layers, self.dock_info, self.dock_grading, self.dock_curves, self.dock_analysis)
        fronts = {}
        for group in (*upper, *lower):
            front = next((dock for dock in (*selected, *defaults, *group)
                          if dock in group and shown[dock]), None)
            if front is not None:
                front.raise_()
                fronts[group] = front

        first, second, scopes = self._photo_color_defaults()["bottom"]
        lower_columns = lower[:-2] + (lower[-2] + lower[-1],) if split_lower else lower
        shares = (first, second, scopes) if len(lower_columns) == 3 else (first + second, scopes)
        columns = [(dock, share) for group, share in zip(lower_columns, shares)
                   if (dock := next((dock for dock in group if shown[dock]), None)) is not None]
        if columns:
            self.resizeDocks([dock for dock, _ in columns],
                             [max(1, round(self.width() * share / sum(shares))) for _, share in columns],
                             horizontal)
            tools_height = previous_tool_height or COLOR_TOOL_DOCK_DEFAULT_HEIGHT
            if split_lower:
                tools_height = _PHOTO_COLOR_STACKED_COMPACT_TOOL_HEIGHT
            view_height = previous_view_height or max(1, self.height() - tools_height)
            self.resizeDocks([self.dock_viewport, columns[0][0]], [view_height, tools_height], vertical)
        if split_upper or split_lower:
            details, layers = (upper if split_upper else lower)[-2:]
            if details in fronts and layers in fronts:
                self.resizeDocks([fronts[details], fronts[layers]], [240, 180], vertical)
        self._size_photo_color_top(self.width(), upper)
        self._perform_update_all_titlebars()

    def _apply_default_photo_color_layout(self) -> None:
        """Build the same layout for a new profile and Reset to Default."""
        for dock in self.all_docks:
            dock.hide()
            if dock.isFloating():
                dock.setFloating(False)
        self._set_photo_color_viewport_minimum(True)
        upper, lower = self._photo_color_stacks_for_size(
            self._photo_color_available_width(self.width()), initializing=True,
        )
        shown = dict.fromkeys((dock for group in (*upper, *lower) for dock in group), True)
        self._arrange_photo_color(upper, lower, shown)
        self._enforce_work_mode_dock_visibility()

    def _photo_color_sidebar_shares(self) -> tuple[float, float]:
        left, center, right = self._photo_color_defaults()["top"]
        default = (left / (left + center + right), right / (left + center + right))
        saved = self.config_manager.get(_PHOTO_COLOR_SIDEBARS_KEY)
        if (isinstance(saved, list) and len(saved) == 2
                and all(type(share) in (int, float) and 0 < share < 1 for share in saved)):
            return saved[0], saved[1]
        return default

    def _fit_photo_color_left_labels(self, tabs: tuple[QDockWidget, ...] = ()) -> int:
        originals = getattr(self, "_photo_color_left_minimums", {})
        visible = [dock for dock in tabs if not dock.isHidden()]
        width = 0
        if len(visible) > 1:
            # Reserve enough room for every left-hand tab, including the
            # selected tab's heavier text and the compact horizontal padding.
            font = visible[0].font()
            font.setBold(True)
            metrics = QFontMetrics(font)
            width = 16 + sum(metrics.horizontalAdvance(dock.windowTitle()) + 10 for dock in visible)
        for dock in originals.keys() - set(tabs):
            dock.setMinimumWidth(originals[dock])
        self._photo_color_left_minimums = {dock: originals.get(dock, dock.minimumWidth()) for dock in tabs}
        for dock, original_width in self._photo_color_left_minimums.items():
            dock.setMinimumWidth(max(original_width, width))
        return width

    def _size_photo_color_top(self, width: int, upper: _DockStacks) -> None:
        viewer = self.dock_viewport
        if viewer.isHidden():
            return
        left_share, right_share = self._photo_color_sidebar_shares()
        left_minimum = self._fit_photo_color_left_labels(upper[0])
        left_share = max(left_share, left_minimum / max(1, width))
        sides = ((upper[0], left_share), (tuple(dock for group in upper[2:] for dock in group), right_share))
        columns = [(dock, share) for group, share in sides
                   if (dock := next((dock for dock in group if not dock.isHidden()), None)) is not None]
        if not columns:
            return
        available = width - len(columns)
        sizes = [max(1, round(available * share)) for _, share in columns]
        self.resizeDocks([viewer, *(dock for dock, _ in columns)],
                         [max(1, available - sum(sizes)), *sizes], Qt.Orientation.Horizontal)

    def _photo_color_is_factory(self) -> bool:
        selection = self.config_manager.get(self._active_page_layout_key("Color"))
        return not isinstance(selection, dict) or selection.get("preset_id") == "factory"

    def _remember_photo_color_sidebar(self, side: int) -> None:
        if not self._photo_color_is_factory():
            return
        self.layout().activate()
        resources, right, *_ = self._photo_color_groups()
        group = (*resources, self.dock_deblur, self.dock_grain) if side == 0 else right
        dock = front_dock(group)
        if dock is not None and dock.geometry().top() < self.dock_viewport.geometry().bottom():
            shares = list(self._photo_color_sidebar_shares())
            shares[side] = dock.width() / self.contentsRect().width()
            self.config_manager.set(_PHOTO_COLOR_SIDEBARS_KEY, shares)

    def _photo_color_available_width(self, width: int) -> int:
        screen = self.screen()
        return min(width, screen.availableGeometry().width()) if screen else width

    def _is_stock_photo_color(self, upper: _DockStacks, lower: _DockStacks) -> bool:
        top = Qt.DockWidgetArea.TopDockWidgetArea
        groups = (*upper, *lower)
        return (all(not dock.isFloating() and self.dockWidgetArea(dock) == top
                    for group in groups for dock in group)
                and all(self._photo_color_tabs_match(group) for group in groups))

    def _fit_photo_color_layout(self, width: int, resize_columns: bool = True) -> None:
        upper, lower = self._photo_color_stacks_for_size(self._photo_color_available_width(width))
        wanted = (upper, lower)
        current = wanted if self._is_stock_photo_color(*wanted) else None
        if current is None:
            for compact, merge_tools, stack_details in (
                (False, False, False), (False, False, True), (False, True, False),
                (False, True, True), (True, True, False), (True, True, True),
            ):
                candidate = self._photo_color_stacks(compact, merge_tools, stack_details)
                if self._is_stock_photo_color(*candidate):
                    current = candidate
                    break

        # A named preset keeps its geometry. If its tool groups match the
        # factory stacks, they still use the photo workspace's height limits.
        if current is None or not self._photo_color_is_factory():
            self._fit_photo_color_left_labels()
            self._sync_photo_color_height_limits(*(current or ((), ())))
        elif current == wanted:
            self._sync_photo_color_height_limits(upper, lower)
            if resize_columns:
                self._size_photo_color_top(width, upper)
        else:
            self._reflow_photo_color(upper, lower, *current)

    def _sync_photo_color_height_limits(self, upper: _DockStacks = (), lower: _DockStacks = ()) -> None:
        # A capped tab also caps the row that contains it. Only keep the
        # grading-tool cap on the lower row so the photo preview can grow.
        upper_docks = {dock for group in upper for dock in group}
        tall_lower = lower and lower[-1] == (self.dock_layers,)
        lower_docks = {dock for group in lower for dock in group} if tall_lower else set()
        for group in self._photo_color_groups():
            for dock in group:
                if isinstance(dock, ColorToolDockWidget):
                    limit = None if dock in upper_docks else COLOR_TOOL_DOCK_MAX_HEIGHT
                    if dock in lower_docks:
                        limit = _PHOTO_COLOR_STACKED_COMPACT_TOOL_HEIGHT
                    dock.set_docked_height_limit(limit)

    def _reflow_photo_color(
        self, upper: _DockStacks, lower: _DockStacks, previous_upper: _DockStacks, previous_lower: _DockStacks,
    ) -> None:
        previous_tools = next((dock for group in previous_lower if (dock := front_dock(group)) is not None), None)
        tool_height = 0
        view_height = self.dock_viewport.height()
        if previous_tools is not None:
            tool_height = previous_tools.geometry().bottom() - self.dock_viewport.geometry().bottom()
        selected = tuple(dock for group in (*previous_upper, *previous_lower)
                         if (dock := front_dock(group)) is not None)
        if (self.dock_crop not in selected and (self.dock_layers,) in (*previous_upper, *previous_lower)):
            selected = (self.dock_layers, *selected)
        shown = {dock: not dock.isHidden() for group in (*upper, *lower) for dock in group}
        self._arrange_photo_color(upper, lower, shown, previous_view_height=view_height,
                                  previous_tool_height=max(0, tool_height), selected=selected)

    def _set_photo_color_viewport_minimum(self, active: bool) -> None:
        size = _PHOTO_VIEWPORT_MIN_SIZE if active else (_VIEWPORT_MIN_WIDTH, _VIEWPORT_MIN_HEIGHT)
        self.dock_viewport.setMinimumSize(*size)
        container = getattr(self, "viewport_container", None)
        if container is not None:
            container.setMinimumSize(*size)

    def _migrate_photo_color_layout(self) -> None:
        if not self._photo_color_is_factory():
            return
        resources, right, primary, secondary, analysis = self._photo_color_groups()
        left_area = Qt.DockWidgetArea.LeftDockWidgetArea
        right_area = Qt.DockWidgetArea.RightDockWidgetArea
        top = Qt.DockWidgetArea.TopDockWidgetArea
        bottom = Qt.DockWidgetArea.BottomDockWidgetArea
        previous_upper = (resources, (self.dock_viewport,), (right[0],), right[1:])
        previous_lower = (primary, secondary, analysis)
        old_groups = (*previous_upper, *previous_lower)
        old_areas = (left_area, top, right_area, right_area, bottom, bottom, bottom)
        if any(dock.isFloating() or self.dockWidgetArea(dock) != area
               for group, area in zip(old_groups, old_areas) for dock in group):
            return
        if not all(self._photo_color_tabs_match(group) for group in old_groups):
            return
        if any(not dock.isHidden() and not dock.isFloating()
               and all(dock not in group for group in old_groups) for dock in self.all_docks):
            return
        upper, lower = self._photo_color_stacks_for_size(self._photo_color_available_width(self.width()))
        self._reflow_photo_color(upper, lower, previous_upper, previous_lower)

    def _configure_photo_color_layout(self, active: bool) -> None:
        self._set_photo_color_viewport_minimum(active)
        if not active:
            self._fit_photo_color_left_labels()
            self._sync_photo_color_height_limits()
        if self.canvas is not None:
            self.canvas.photo_fit_on_resize = active
        handles = getattr(self, "_photo_color_resize_handles", None)
        if active:
            resources, right, primary, secondary, analysis = self._photo_color_groups()
            self._migrate_photo_color_layout()
            if handles is None:
                handles = PhotoColorResizeHandles(
                    self, self.dock_viewport, sides=((*resources, self.dock_deblur, self.dock_grain), right),
                    tools=(primary, secondary, (*analysis, *right)),
                    fit_layout=self._fit_photo_color_layout, remember_sidebar=self._remember_photo_color_sidebar,
                    restyle_tabs=lambda: self._style_dock_tab_bars(only_changed=True),
                )
                self._photo_color_resize_handles = handles
        if handles is not None:
            handles.set_active(active)

    # ------------------------------------------------------------------
    # _get_all_page_layouts
    # ------------------------------------------------------------------
    def _get_all_page_layouts(self) -> dict:
        """获取所有页面的布局状态（保存当前页面状态后再收集）"""
        # 确保当前页面状态已保存
        self.page_layouts[self.current_page] = save_workspace_dock_state(self, self.current_page)

        # 收集所有页面的布局状态（转换为hex字符串）
        panel_states = {}
        for page, state in self.page_layouts.items():
            if isinstance(state, QByteArray):
                panel_states[page] = state.toHex().data().decode()
        return panel_states

    # ------------------------------------------------------------------
    # _apply_all_page_layouts
    # ------------------------------------------------------------------
    def _apply_all_page_layouts(self, panel_states: dict[str, str]) -> set[str]:
        """Replace each captured page in memory and in its persisted profile."""
        profile_mode = self._layout_profile_mode()
        applied = set()
        for page, state_hex in panel_states.items():
            state = QByteArray.fromHex(state_hex.encode())
            if page == self.current_page and not restore_workspace_dock_state(self, state, page):
                continue
            self.page_layouts[page] = state
            self.layout_store.save_profile(LayoutProfile(
                product_mode=profile_mode, page_id=page, qt_state_hex=state_hex,
            ), auto_save=False)
            applied.add(page)
        return applied

    # ------------------------------------------------------------------
    # save_layout_dialog
    # ------------------------------------------------------------------
    def _custom_page_layouts(self) -> dict[str, str]:
        layouts = self.db.list_page_layouts(self._current_work_mode(), self.current_page)
        if _is_video_color(self, self.current_page):
            return {name: state for name, state in layouts.items()
                    if _is_supported_video_color_state(QByteArray.fromHex(state.encode()))}
        return layouts

    def _layout_bundles(self) -> dict[str, dict[str, str]]:
        bundles = {}
        for name, captures in self.db.list_layout_bundles(self._current_work_mode()).items():
            valid = {}
            for page, state_hex in captures.items():
                if page not in self.page_layouts and page != self.current_page:
                    continue
                state = QByteArray.fromHex(state_hex.encode())
                if _is_video_color(self, page):
                    usable = _is_supported_video_color_state(state)
                else:
                    usable = _qt_dock_state_version(state) == 0
                if usable:
                    valid[page] = state_hex
            if valid:
                bundles[name] = valid
        return bundles

    def _choose_layout_scope(self, title: str) -> bool | None:
        scopes = [I18N.tr("workspace.layout_current_page", "Current Page"),
                  I18N.tr("workspace.layout_all_pages", "All Pages")]
        selected, ok = QInputDialog.getItem(
            self, title, I18N.tr("workspace.layout_scope", "Layout scope"), scopes, 0, False)
        return selected == scopes[1] if ok else None

    def save_layout_dialog(self) -> None:
        title = I18N.tr("dialog.save_layout", "Save Layout")
        all_pages = self._choose_layout_scope(title)
        if all_pages is None:
            return
        name, ok = QInputDialog.getText(self, title, I18N.tr("dialog.layout_name", "Layout name"))
        name = name.strip()
        if ok and name:
            mode, page = self._current_work_mode(), self.current_page
            if all_pages:
                captures = self._get_all_page_layouts()
                self.db.save_layout(name, "", captures[page], captures, work_mode=mode)
            else:
                state = save_workspace_dock_state(self, page).toHex().data().decode()
                self.db.save_page_layout(mode, page, name, state)
            self.status_bar.showMessage(
                I18N.tr("workspace.layout_saved", "Layout '{name}' saved.").format(name=name), 3000)
            self.update_layout_combobox()

    def delete_layout_dialog(self) -> None:
        title = I18N.tr("dialog.delete_layout", "Delete Layout")
        current, bundles = self._custom_page_layouts(), self._layout_bundles()
        if not current and not bundles:
            QMessageBox.information(self, title, I18N.tr("dialog.no_layouts", "No custom layouts to delete."))
            return
        all_pages = self._choose_layout_scope(title) if current and bundles else bool(bundles)
        if all_pages is None:
            return

        name, ok = QInputDialog.getItem(
            self, title, I18N.tr("dialog.select_layout_delete", "Select Layout to Delete"),
            list(bundles if all_pages else current), 0, False,
        )
        if ok and name:
            message = I18N.tr("dialog.confirm_delete_layout", "Delete layout '{name}'?").format(name=name)
            confirm = QMessageBox.question(self, title, message,
                                          QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if confirm == QMessageBox.StandardButton.Yes:
                mode = self._current_work_mode()
                if all_pages:
                    self.db.delete_layout(name, work_mode=mode)
                    for page in bundles[name]:
                        key = self._active_page_layout_key(page)
                        if self.config_manager.get(key) == {"bundle": name}:
                            self.config_manager.set(key, None, auto_save=False)
                    self.config_manager.save_config()
                else:
                    self.db.delete_page_layout(mode, self.current_page, name)
                    key = self._active_page_layout_key()
                    if self.config_manager.get(key) == {"name": name}:
                        self.config_manager.set(key, None)
                self.status_bar.showMessage(
                    I18N.tr("workspace.layout_deleted", "Layout '{name}' deleted.").format(name=name), 3000)
                self.update_layout_combobox()

    def _builtin_page_layouts(self) -> list[tuple[str, str]]:
        if self.current_page == "Color":
            title = I18N.tr("workspace.horizontal_color", "Landscape Color")
            if _is_video_color(self, self.current_page):
                return [(title, "horizontal_color")]
            if self._current_work_mode() == "image":
                return [(title, "factory")]
        if self.current_page == "Edit" and getattr(self, "_edit_available", False):
            return [(I18N.tr("edit.workspace.editing", "默认布局"), "editing")]
        default = I18N.tr("edit.workspace.editing", "Default Layout")
        if self.current_page == "Mixer":
            video_mix = I18N.tr("workspace.mixer_video_mix", "Video Mixing")
            editing = I18N.tr("workspace.mixer_editing_layout", "Editing Layout")
            return [(video_mix, "mixer_tabbed"), (editing, "factory")]
        return [(default, "factory")]

    def _page_layout_choices(self) -> list[tuple[str, str | None]]:
        return [*self._builtin_page_layouts(), *((name, None) for name in self._custom_page_layouts())]

    def _active_page_layout_key(self, page: str | None = None) -> str:
        return f"active_layout_preset_{self._layout_profile_mode()}_{page or self.current_page}"

    def _active_page_layout_index(self, choices: list[tuple[str, str | None]]) -> int:
        config = getattr(self, "config_manager", None)
        selected = config.get(self._active_page_layout_key()) if config is not None else None
        for index, (name, preset_id) in enumerate(choices, start=1):
            if selected == ({"preset_id": preset_id} if preset_id is not None else {"name": name}):
                return index
        bundle = selected.get("bundle") if isinstance(selected, dict) else None
        if isinstance(bundle, str) and self.current_page in self._layout_bundles().get(bundle, {}):
            return 0
        return int(bool(choices))

    def _remember_page_layout_choice(self, name: str, preset_id: str | None) -> None:
        config = getattr(self, "config_manager", None)
        if config is not None:
            key = self._active_page_layout_key()
            selection = {"preset_id": preset_id} if preset_id is not None else {"name": name}
            if config.get(key) != selection:
                config.set(key, selection)
        self._configure_edit_resize_handles()
        if getattr(self, "layout_combo", None) is not None:
            self.update_layout_combobox()

    def update_layout_combobox(self) -> None:
        choices = self._page_layout_choices()
        signals_blocked = self.layout_combo.blockSignals(True)
        try:
            self.layout_combo.clear()
            self.layout_combo.addItem(I18N.tr("workspace.layout_presets", "Layout Presets"))
            for name, preset_id in choices:
                self.layout_combo.addItem(name, preset_id)
            if isinstance(view := self.layout_combo.view(), QListView):
                view.setRowHidden(0, True)
            self.layout_combo.setCurrentIndex(self._active_page_layout_index(choices))
        finally:
            self.layout_combo.blockSignals(signals_blocked)

    def _apply_page_layout_choice(self, name: str, preset_id: str | None) -> bool:
        if preset_id is None:
            return self.apply_layout(name)
        video_mix = preset_id == "mixer_tabbed" and self.current_page == "Mixer"
        if preset_id == "factory" or video_mix or (
            preset_id == "horizontal_color" and _is_video_color(self, self.current_page)
        ):
            if video_mix:
                self._reset_factory_page_layout(video_mix=True)
            else:
                self._reset_factory_page_layout()
            self._remember_page_layout_choice(name, preset_id)
            if video_mix:
                self._fit_video_mix_workspace_height()
            self.status_bar.showMessage(
                I18N.tr("workspace.layout_loaded", "Layout '{name}' loaded.").format(name=name), 3000)
            return True
        return self.current_page == "Edit" and self.apply_edit_workspace_preset(preset_id)

    def on_layout_combo_activated(self, index: int) -> None:
        if not 0 < index < self.layout_combo.count() or not self._apply_page_layout_choice(
            self.layout_combo.itemText(index), self.layout_combo.itemData(index),
        ):
            self.update_layout_combobox()

    def update_load_layout_menu(self) -> None:
        self.load_layout_menu.clear()
        page = self.current_page
        current = self.load_layout_menu.addMenu(I18N.tr("workspace.layout_current_page", "Current Page"))
        all_pages = self.load_layout_menu.addMenu(I18N.tr("workspace.layout_all_pages", "All Pages"))
        for name, preset_id in self._page_layout_choices():
            action = current.addAction(name)
            action.triggered.connect(
                lambda checked, n=name, pid=preset_id: self._apply_page_layout_choice(n, pid)
                if self.current_page == page else None
            )
        bundles = self._layout_bundles()
        from_bundle = [name for name, captures in bundles.items() if page in captures]
        if from_bundle:
            menu = current.addMenu(I18N.tr("workspace.layout_from_bundle", "From an All Pages Preset"))
            for name in from_bundle:
                menu.addAction(name, lambda checked=False, n=name: self.apply_layout_bundle(n, all_pages=False)
                               if self.current_page == page else None)
        for name in bundles:
            all_pages.addAction(name, lambda checked=False, n=name: self.apply_layout_bundle(n, all_pages=True))
        for menu in (current, all_pages):
            if not menu.actions():
                menu.addAction(I18N.tr("dialog.no_saved_layouts", "No saved layouts")).setEnabled(False)

    def apply_layout(self, name: str) -> bool:
        page = self.current_page
        state_hex = self._custom_page_layouts().get(name)
        if not state_hex or not restore_workspace_dock_state(self, QByteArray.fromHex(state_hex.encode()), page):
            return False

        self._remember_page_layout_choice(name, None)
        self._refresh_after_named_layout()
        self.status_bar.showMessage(
            I18N.tr("workspace.layout_loaded", "Layout '{name}' loaded.").format(name=name), 3000)
        return True

    def apply_layout_bundle(self, name: str, *, all_pages: bool) -> bool:
        page = self.current_page
        captures = self._layout_bundles().get(name, {})
        if not all_pages:
            captures = {page: captures[page]} if page in captures else {}
        applied = self._apply_all_page_layouts(captures)
        if not applied:
            return False

        for applied_page in applied:
            self.config_manager.set(self._active_page_layout_key(applied_page), {"bundle": name}, auto_save=False)
        if page in applied:
            self._refresh_after_named_layout()
        self.config_manager.save_config()
        self._configure_edit_resize_handles()
        self.update_layout_combobox()
        self.status_bar.showMessage(
            I18N.tr("workspace.layout_loaded", "Layout '{name}' loaded.").format(name=name), 3000)
        return True

    def _refresh_after_named_layout(self) -> None:
        page = self.current_page
        self._enforce_page_dock_visibility()
        panel_bar = getattr(self, "workspace_panel_bar", None)
        if panel_bar is not None:
            panel_bar.set_page(page)
        self._sync_floating_docks()
        self.update_all_dock_titlebars()
        state = save_workspace_dock_state(self, page)
        self.page_layouts[page] = state
        self._save_current_layout_to_store(page, qt_state=state)

    # ------------------------------------------------------------------
    # Edit workspace presets (P1-24)
    # ------------------------------------------------------------------
    def _workspace_dock_map(self) -> dict[str, QDockWidget]:
        """objectName → QDockWidget，仅覆盖 Edit 工作区受管 dock。

        缺失的 dock（图像模式没有 Media Pool、字幕工作区未构建等）直接
        不出现在映射里，预设应用对其静默跳过。
        """
        from src.features.edit.core.workspace_presets import (
            EDIT_WORKSPACE_MANAGED_DOCKS,
        )

        docks: dict[str, QDockWidget] = {}
        for name in EDIT_WORKSPACE_MANAGED_DOCKS:
            dock = self.findChild(QDockWidget, name)
            if dock is not None:
                docks[name] = dock
        return docks

    def apply_edit_workspace_preset(self, preset_id: str) -> bool:
        """应用内建剪辑布局。

        流程：隐藏全部受管 dock → 按预设重摆区域 → tab 化 → 显示 →
        尺寸提示 → 快照进 page_layouts["Edit"] 与 LayoutStore。之后
        switch_to_page / 重启恢复走既有双系统路径，预设布局自然持久。

        Returns:
            True 表示已应用；False 表示预设不存在或 Edit 功能不可用。
        """
        from src.features.edit.core.workspace_presets import get_workspace_preset

        if not getattr(self, "_edit_available", False):
            return False
        preset = get_workspace_preset(preset_id)
        if preset is None:
            return False
        if getattr(self, "current_page", "Edit") != "Edit":
            self.switch_to_page("Edit")

        handles = getattr(self, "_edit_dock_resize_handles", None)
        if handles is not None:
            handles.reset_column_shares()
        self._arrange_edit_workspace(preset)
        if getattr(self, "config_manager", None) is not None:
            self._mark_default_edit_layout()
            self._reset_workspace_panel_controls()

        # 6) 持久化到两套布局系统 + 遵守页面 dock 可见性契约。
        self._enforce_page_dock_visibility()
        if hasattr(self, "page_layouts"):
            self.page_layouts["Edit"] = self.saveState()
        save_to_store = getattr(self, "_save_current_layout_to_store", None)
        if callable(save_to_store):
            save_to_store("Edit")
        # dock 实际尺寸要等布局稳定后再写 config（save_layout_dialog 同款延迟）。
        if getattr(self, "config_manager", None) is not None:
            QTimer.singleShot(60, self._save_all_docked_sizes)
        # 标题栏刷新依赖 _titlebar_update_timer（setup 阶段创建），最小宿主没有。
        if getattr(self, "_titlebar_update_timer", None) is not None:
            QTimer.singleShot(50, self.update_all_dock_titlebars)

        if getattr(self, "status_bar", None) is not None:
            self.status_bar.showMessage(
                I18N.tr(
                    "edit.workspace.applied",
                    "Workspace applied: {name}",
                    name=I18N.tr(f"edit.workspace.{preset.preset_id}", preset.label),
                ),
                3000,
            )
        self._remember_page_layout_choice(preset.label, preset_id)
        return True

    def _arrange_edit_workspace(self, preset: WorkspacePreset, *, initial_window: bool = False) -> None:
        from src.features.edit.core.workspace_presets import EDITING_PRESET

        dock_map = self._workspace_dock_map()

        # 1) 全部受管 dock 先隐藏，避免预设外的残留（用户手动开的面板、
        #    上一个预设的 dock）泄漏进快照。
        for dock in self.all_docks:
            dock.hide()

        # 2) 重摆区域。addDockWidget 会把 dock 从旧堆栈拆出，等价于
        #    “重置归位”，避免在用户当前布局的碎片上叠加 tab。
        area_enum = {
            "left": Qt.DockWidgetArea.LeftDockWidgetArea,
            "right": Qt.DockWidgetArea.RightDockWidgetArea,
            "top": Qt.DockWidgetArea.TopDockWidgetArea,
            "bottom": Qt.DockWidgetArea.BottomDockWidgetArea,
        }
        self.setCorner(Qt.Corner.TopLeftCorner, area_enum["left"])
        self.setCorner(Qt.Corner.TopRightCorner, area_enum["right"])
        self.setCorner(Qt.Corner.BottomLeftCorner, area_enum["bottom"])
        self.setCorner(Qt.Corner.BottomRightCorner, area_enum["bottom"])
        for name, area in preset.dock_areas:
            dock = dock_map.get(name)
            if dock is not None:
                dock.setFloating(False)
                self.removeDockWidget(dock)
                self.addDockWidget(area_enum[area], dock)

        if preset is EDITING_PRESET:
            # As in Color, build the upper row as a single horizontal dock split.
            media = dock_map.get("dockEditMediaPool")
            viewer = dock_map.get("dockProgramViewer")
            inspector = dock_map.get("dockProperties")
            if media is not None and viewer is not None:
                self.splitDockWidget(media, viewer, Qt.Orientation.Horizontal)
            if viewer is not None and inspector is not None:
                self.splitDockWidget(viewer, inspector, Qt.Orientation.Horizontal)

        # 3) tab 化（组内首成员为基准）。
        for group in preset.tabify_groups:
            base = dock_map.get(group[0])
            if base is None:
                continue
            for member in group[1:]:
                dock = dock_map.get(member)
                if dock is not None:
                    self.tabifyDockWidget(base, dock)

        # 4) 按预设显示。
        for name in preset.visible_docks:
            dock = dock_map.get(name)
            if dock is not None:
                dock.setVisible(True)

        for group in preset.tabify_groups:
            if (anchor := dock_map.get(group[0])) is not None:
                anchor.raise_()

        if preset is EDITING_PRESET:
            if (inspector := dock_map.get("dockProperties")) is not None:
                for name in ("dockCaptionWorkspace", "dockMulticam"):
                    if (dock := dock_map.get(name)) is not None:
                        dock.setFloating(False)
                        self.removeDockWidget(dock)
                        self.addDockWidget(area_enum["top"], dock)
                        self.tabifyDockWidget(inspector, dock)
                        dock.hide()
            if initial_window:
                self._apply_video_project_window_size()
            self._size_default_edit_workspace(dock_map)

        for sizes, orientation in (
            (preset.h_sizes, Qt.Orientation.Horizontal),
            (preset.v_sizes, Qt.Orientation.Vertical),
        ):
            docks_list = [dock_map[n] for n, _s in sizes if n in dock_map]
            size_vals = [s for n, s in sizes if n in dock_map]
            if docks_list:
                self.resizeDocks(docks_list, size_vals, orientation)

        focus = dock_map.get(preset.focus_dock) if preset.focus_dock else None
        if focus is not None:
            focus.raise_()

    def _mark_default_edit_layout(self) -> None:
        """Keep legacy dock repair callbacks from changing the new default."""
        for key, version in (
            (_EDIT_PROPERTIES_DOCK_LAYOUT_MIGRATION_KEY, _EDIT_PROPERTIES_DOCK_LAYOUT_MIGRATION_VERSION),
            (_EDIT_INFO_DOCK_LAYOUT_MIGRATION_KEY, _EDIT_INFO_DOCK_LAYOUT_MIGRATION_VERSION),
            (_AI_ASSISTANT_DOCK_LAYOUT_MIGRATION_KEY, _AI_ASSISTANT_DOCK_LAYOUT_MIGRATION_VERSION),
            (_EDIT_TIMELINE_DOCK_LAYOUT_MIGRATION_KEY, _EDIT_TIMELINE_DOCK_LAYOUT_MIGRATION_VERSION),
        ):
            current = self.config_manager.get(key)
            if not isinstance(current, int) or current < version:
                self.config_manager.set(key, version, auto_save=False)

    def _size_default_edit_workspace(self, docks: dict[str, QDockWidget]) -> None:
        tier, _ = self._video_project_screen_tier(self._video_color_defaults())
        defaults = self._read_workspace_defaults("video_edit", _VIDEO_EDIT_DEFAULTS)
        shares = defaults[tier]
        layout = self.layout()
        layout.activate()
        names = ("dockProgramViewer", "dockEditTimeline")
        panels = [(docks[name], ratio) for name, ratio in zip(names, shares[3:]) if name in docks]
        if panels:
            height = self.contentsRect().height()
            total = sum(shares[3:])
            self.resizeDocks(
                [dock for dock, _ in panels], [max(1, height * ratio // total) for _, ratio in panels],
                Qt.Orientation.Vertical,
            )

    def update_workspace_menu(self) -> None:
        """Show the current page's factory preset in Window > Workspaces."""
        self.workspace_menu.clear()
        for name, preset_id in self._builtin_page_layouts():
            self.workspace_menu.addAction(
                name, lambda checked=False, label=name, key=preset_id: self._apply_page_layout_choice(label, key),
            )

    # ------------------------------------------------------------------
    # Factory default layout (committed with the repository)
    # ------------------------------------------------------------------
    def _get_factory_layout_path(self) -> str:
        """Return the path of the committed factory default layout.

        The factory default is a tracked source file (``default_layout.json``
        at the project root) and can only change through a git commit — the
        same rule as any other source file.  ``TOPOS_DEFAULT_LAYOUT_PATH``
        overrides it for tests and maintenance workflows.
        """
        override = os.environ.get("TOPOS_DEFAULT_LAYOUT_PATH")
        if override:
            return os.path.abspath(os.path.expanduser(override))
        if getattr(sys, "frozen", False):
            # PyInstaller: datas land under sys._MEIPASS (onefile) or beside
            # the executable (onedir); check both.
            candidates = []
            meipass = getattr(sys, "_MEIPASS", "")
            if meipass:
                candidates.append(Path(meipass) / "default_layout.json")
            candidates.append(Path(sys.executable).resolve().parent / "default_layout.json")
            for candidate in candidates:
                if candidate.is_file():
                    return str(candidate)
            return str(candidates[-1])
        # Source checkout: project root, three levels above this file.
        return str(Path(__file__).resolve().parents[3] / "default_layout.json")

    def _load_factory_layout(self, work_mode: str | None = None) -> tuple[str | None, str | None, dict | None]:
        """Load the committed factory default layout for a work mode.

        Video and image modes keep different layouts, so the file stores one
        capture per mode (``{"schema": 1, "modes": {"video": {...}}}``).  The
        ``"all"`` bucket applies to every mode lacking its own capture; a
        legacy flat file (geometry/state at the top level) is treated as
        ``"all"``.

        Returns ``(geometry, state, panel_states)``; geometry/state are hex
        strings, or ``(None, None, None)`` when no usable capture exists.
        """
        path = self._get_factory_layout_path()
        if not os.path.isfile(path):
            return None, None, None
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Failed to load factory layout %s: %s", path, exc)
            return None, None, None

        bucket = None
        if isinstance(data.get("modes"), dict):
            if work_mode is None:
                work_mode = self._current_work_mode()
            bucket = data["modes"].get(work_mode)
            if bucket is None and work_mode != "all":
                bucket = data["modes"].get("all")
        elif isinstance(data.get("state"), str):
            # Legacy flat capture — applies to every mode.
            bucket = data
        if not isinstance(bucket, dict):
            return None, None, None

        geometry = bucket.get("geometry")
        state = bucket.get("state")
        if not (isinstance(geometry, str) and geometry and isinstance(state, str) and state):
            logger.warning("Factory layout %s has no usable capture for mode %s", path, work_mode)
            return None, None, None
        panel_states = bucket.get("panel_states")
        if panel_states is not None and not isinstance(panel_states, dict):
            panel_states = None
        return geometry, state, panel_states

    # ------------------------------------------------------------------
    # reset_layout_to_default
    # ------------------------------------------------------------------
    def reset_layout_to_default(self) -> None:
        """Restore the current page's selected preset, or its shipped default."""
        choices = self._page_layout_choices()
        index = self._active_page_layout_index(choices)
        selection = self.config_manager.get(self._active_page_layout_key())
        bundle = selection.get("bundle") if isinstance(selection, dict) else None
        if index and choices[index - 1][1] == "factory":
            self._reset_factory_page_layout()
            return
        restored = (self._apply_page_layout_choice(*choices[index - 1]) if index else
                    isinstance(bundle, str) and self.apply_layout_bundle(bundle, all_pages=False))
        if restored:
            if not index or choices[index - 1][1] not in ("horizontal_color", "editing"):
                self._reset_workspace_panel_controls()
            self.status_bar.showMessage(I18N.tr("status.layout_reset", "Layout reset to default."), 3000)
            return
        self._reset_factory_page_layout(video_mix=self.current_page == "Mixer")

    def _reset_factory_page_layout(self, *, video_mix: bool = False) -> None:
        page = self.current_page
        hardcoded = False
        if page == "Mixer" and self._seed_audio_workspace_layout():
            if video_mix:
                self._arrange_video_mix_workspace_layout()
            else:
                self._arrange_audio_workspace_layout(compact=self._mixer_needs_compact_layout())
        elif _is_video_color(self, page):
            self._apply_default_video_color_layout()
        elif page == "Color" and self._current_work_mode() == "image":
            self.config_manager.set(_PHOTO_COLOR_SIDEBARS_KEY, None, auto_save=False)
            self._apply_default_photo_color_layout()
        elif page == "Edit" and getattr(self, "_edit_available", False):
            from src.features.edit.core.workspace_presets import EDITING_PRESET

            self._arrange_edit_workspace(EDITING_PRESET)
            self._mark_default_edit_layout()
        elif page == "Library":
            self._apply_default_library_layout()
            browser_attr = "library_panel" if self._current_work_mode() == "image" else "files_panel"
            browser = getattr(self, browser_attr, None)
            reset_sizes = getattr(browser, "reset_workspace_sizes", None)
            if callable(reset_sizes):
                reset_sizes()
        elif page == "Composite" and self.dock_composite:
            self._apply_default_composite_layout()
            key = f"composite_default_layout_version_{self._layout_profile_mode()}"
            self.config_manager.set(key, _COMPOSITE_DEFAULT_LAYOUT_VERSION)
        else:
            _, _, panel_states = self._load_factory_layout()
            state_hex = (panel_states or {}).get(page)
            restored = isinstance(state_hex, str) and restore_workspace_dock_state(
                self, QByteArray.fromHex(state_hex.encode()), page)
            if not restored:
                hardcoded = True
                self._apply_hardcoded_page_layout(page)

        if page == "Color" and not _is_video_color(self, page) and self._current_work_mode() != "image":
            keyframes = getattr(self, "dock_color_keyframes", None)
            if keyframes is not None:
                keyframes.hide()
                self._color_keyframes_reseat = True
            self._migrate_color_remapping_dock_layout(force=True)
            self._color_tool_height_reset_pending = True
            QTimer.singleShot(50, self._migrate_color_grading_dock_height)
        if page == "Composite":
            self._migrate_composite_scopes_dock_visibility()
        self._enforce_page_dock_visibility()
        self._reset_workspace_panel_controls()
        self._sync_floating_docks()
        self._perform_update_all_titlebars()
        self.page_layouts[page] = save_workspace_dock_state(self, page)
        self._save_current_layout_to_store(page)
        if video_mix:
            self.config_manager.set(self._video_mix_workspace_layout_version_key(), _VIDEO_MIX_WORKSPACE_LAYOUT_VERSION)
        message = (I18N.tr("status.layout_reset_hardcoded", "Layout reset to hardcoded default.")
                   if hardcoded else I18N.tr("status.layout_reset", "Layout reset to default."))
        self.status_bar.showMessage(message, 3000)

    def _apply_hardcoded_page_layout(self, page: str) -> None:
        if page == "Composite" and self.dock_composite:
            self._apply_default_composite_layout()
        elif page == "Edit" and self._edit_available:
            self.apply_edit_workspace_preset("editing")
        elif page == "Library":
            self._apply_default_library_layout()
        elif page == "Deliver":
            if self._current_work_mode() == "image":
                self._apply_image_deliver_layout(reset=True)
            elif self.dock_deliver:
                self._apply_default_video_deliver_layout()
        elif page == "Color":
            self.apply_default_layout()

    def _reset_workspace_panel_controls(self) -> None:
        panel_bar = getattr(self, "workspace_panel_bar", None)
        if panel_bar is not None:
            panel_bar.reset_groups(self.current_page)
            panel_bar.set_page(self.current_page)
        if self.current_page == "Deliver":
            reset_queue = getattr(getattr(self, "deliver_page", None), "reset_render_queue_visibility", None)
            if callable(reset_queue):
                reset_queue()

    # ------------------------------------------------------------------
    # set_current_layout_as_default (developer mode only)
    # ------------------------------------------------------------------
    def set_current_layout_as_default(self) -> None:
        """Write the current layout to the committed factory default file.

        Only reachable when developer mode is on (Preferences > Advanced,
        applied after restart).  The capture is stored under the CURRENT work
        mode (video/image keep different layouts); captures for other modes
        already in the file are preserved.  The written ``default_layout.json``
        is a tracked source file — review the diff and commit it; that commit
        is the only way the shipped default ever changes.  To ship both
        modes: set the default in video mode, switch the app to image mode,
        set it again, then commit.
        """
        geom = self.saveGeometry().toHex().data().decode()
        state = save_workspace_dock_state(self, self.current_page).toHex().data().decode()
        panel_states = self._get_all_page_layouts()
        work_mode = self._current_work_mode()

        path = self._get_factory_layout_path()
        try:
            existing: dict = {}
            if os.path.isfile(path):
                with open(path, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                if isinstance(loaded, dict):
                    existing = loaded
            # 迁移旧版扁平结构，避免丢失既有捕获
            modes = existing.get("modes")
            if not isinstance(modes, dict):
                modes = {}
                if isinstance(existing.get("state"), str):
                    modes["all"] = {
                        "geometry": existing.get("geometry"),
                        "state": existing.get("state"),
                        "panel_states": existing.get("panel_states"),
                    }
            if work_mode == "video":
                # Video Color and Edit use responsive factory defaults.
                live_states = (panel_states.pop("Color", None), panel_states.pop("Edit", None))
                if state in live_states:
                    state = panel_states.get("Library") or state
            modes[work_mode] = {
                "geometry": geom,
                "state": state,
                "panel_states": panel_states,
            }
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump({**existing, "schema": 1, "modes": modes}, f, indent=2)
        except (OSError, json.JSONDecodeError) as exc:
            logger.error("Failed to export factory layout to %s: %s", path, exc)
            self.status_bar.showMessage(I18N.tr("status.factory_layout_export_failed", "Failed to export factory layout."), 3000)
            return
        logger.info("Factory layout exported for mode '%s': %s (commit it to ship)", work_mode, path)
        self.status_bar.showMessage(
            I18N.tr("status.factory_layout_exported", "Default layout updated — commit default_layout.json to ship it."),
            5000,
        )

    # ------------------------------------------------------------------
    # restore_layout_state
    # ------------------------------------------------------------------
    def restore_layout_state(self):
        # Restore window geometry (position/size)
        last_geom = self.config_manager.get("window_geometry")
        if not last_geom:
            last_geom = self.db.get_setting("last_geometry")

        if last_geom:
            self.restoreGeometry(QByteArray.fromHex(last_geom.encode()))
            self._ensure_window_on_screen(self)

        # Restore dock layout: try LayoutStore first, then per-page state,
        # then global window_state as final fallback.
        restored_from_store = False
        if hasattr(self, '_restore_layout_from_store'):
            restored_from_store = self._restore_layout_from_store(self.current_page)

        if not restored_from_store:
            if self.current_page in self.page_layouts:
                # Composite owns its Node Properties dock (widget bound at
                # creation), so there is no swap needed for it. Edit still
                # uses the shared dock_properties with its own panel.
                if (self.current_page != "Mixer" and self.dock_properties is not None
                        and self.edit_properties_panel is not None):
                    self.dock_properties.setWidget(self.edit_properties_panel)
                restore_workspace_dock_state(self, self.page_layouts[self.current_page], self.current_page)
            else:
                # No per-page layout saved — fall back to global window_state
                last_state = self.config_manager.get("window_state")
                if not last_state:
                    last_state = self.db.get_setting("last_state")
                if last_state:
                    restore_workspace_dock_state(
                        self, QByteArray.fromHex(last_state.encode()), self.current_page
                    )

        if self.current_page == "Mixer":
            self._attach_edit_panels_to_mixer()
            self._migrate_mixer_workspace_layout()
            self._fit_mixer_workspace_height()

        # Sync floating docks and update titlebars
        self._sync_floating_docks()

        # Post-restore: enforce page-specific dock visibility.
        # Saved layouts from older versions (or from a time when composite was
        # on-screen during Color save) may have these docks visible incorrectly.
        self._enforce_page_dock_visibility()
        if not last_geom and (_is_video_color(self, self.current_page) or self.current_page == "Edit"):
            self._apply_video_project_window_size()
        panel_bar = getattr(self, "workspace_panel_bar", None)
        if panel_bar is not None:
            panel_bar.set_page(self.current_page)

        self.updateGeometry()
        self._perform_update_all_titlebars()

        # ``restoreState`` applies persisted splitter proportions after all
        # widgets have been reparented.  Defer the one-shot Edit migration to
        # the next event-loop turn so Qt has finished that restore first.
        if self.current_page == "Edit":
            QTimer.singleShot(0, self._migrate_edit_properties_dock_width)
            QTimer.singleShot(0, self._migrate_ai_assistant_dock_layout)
            # Qt applies the restored splitter geometry after the window is
            # shown.  Running this at 0ms sees the pre-restore height and
            # misses the malformed Timeline state we need to repair.
            QTimer.singleShot(
                _EDIT_TIMELINE_DOCK_MIGRATION_DELAY_MS,
                self._migrate_edit_timeline_dock_height,
            )
        elif self.current_page == "Color":
            QTimer.singleShot(0, self._migrate_color_remapping_dock_layout)
            # Measure the available height after the startup screen clamps.
            QTimer.singleShot(
                COLOR_TOOL_LAYOUT_RESTORE_DELAY_MS,
                self._migrate_color_grading_dock_height,
            )
        elif self.current_page == "Composite":
            QTimer.singleShot(0, self._migrate_composite_default_layout)
            QTimer.singleShot(0, self._migrate_composite_scopes_dock_visibility)
        elif self.current_page == "Deliver" and self._current_work_mode() == "image":
            QTimer.singleShot(50, self._migrate_deliver_layout)

        # Deferred dock-size restoration (``resizeDocks`` and the migrations
        # above) can grow the main window back beyond the screen when the
        # saved sizes came from a different monitor setup or DPI scale.
        # Re-clamp once those steps have settled so the window always ends
        # up fully on-screen; the helper no-ops when it already fits.
        # N16：测试收尾的 DeferredDelete 冲刷可能先于延迟回调销毁宿主
        # （以及真实关窗场景）——回调内防御已删对象。
        def _clamp_safely():
            try:
                self._ensure_window_on_screen(self)
            except RuntimeError:
                pass
        QTimer.singleShot(150, _clamp_safely)
        QTimer.singleShot(400, _clamp_safely)

    def set_library_media_pool_visible(self, visible: bool) -> None:
        """Move the live pool between the Library browser and its Edit dock."""
        pool = self.media_pool_panel
        dock = self.dock_edit_media_pool
        if self.current_page != "Library" or pool is None or dock is None:
            return
        browser = self.files_panel if self._current_work_mode() == "video" else self.library_panel
        if visible:
            browser.show_media_pool(pool)
        elif browser.hosts_media_pool(pool):
            dock.setWidget(pool)
            pool.show()

    # ------------------------------------------------------------------
    # _enforce_page_dock_visibility
    # ------------------------------------------------------------------
    def _is_image_library_page(self) -> bool:
        return getattr(self, "current_page", None) == "Library" and self._current_work_mode() == "image"

    def _apply_default_library_layout(self) -> None:
        if self._current_work_mode() == "image":
            self._apply_image_library_layout()
            return
        for dock in self.all_docks:
            dock.hide()
        self.dock_files.setFloating(False)
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, self.dock_files)
        self.dock_files.show()

    def _apply_default_video_deliver_layout(self) -> None:
        for dock in self.all_docks:
            if dock is not self.dock_deliver:
                dock.hide()
        self.dock_deliver.setFloating(False)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.dock_deliver)
        self.dock_deliver.show()

    def _apply_image_library_layout(self) -> None:
        """Give the image browser the whole workspace, including old layouts."""
        library = self.dock_library
        for dock in self.all_docks:
            if dock is not library and not dock.isHidden():
                dock.hide()
        if library.isFloating():
            library.setFloating(False)
        if self.dockWidgetArea(library) == Qt.DockWidgetArea.NoDockWidgetArea:
            self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, library)
        if library.isHidden():
            library.show()

    def _guard_image_library_layout(self, _visible: bool) -> None:
        """Keep global dock actions from reopening tools on the Library page."""
        if self.isVisible() and self._is_image_library_page():
            self._apply_image_library_layout()

    def _guard_image_batch_export(self, visible: bool) -> None:
        if visible and self.current_page != "Deliver":
            self.dock_export.hide()

    def _apply_image_deliver_layout(self, *, reset: bool = False) -> None:
        """Place the shared Grade Sets dock beside image Batch Export."""
        grade_sets = getattr(self, "dock_grade_sets", None)
        batch_export = self.dock_export
        left = Qt.DockWidgetArea.LeftDockWidgetArea
        right = Qt.DockWidgetArea.RightDockWidgetArea
        placements = [(grade_sets, left), (batch_export, right)] if grade_sets is not None else [
            (batch_export, right)
        ]
        visible = {dock for dock, _ in placements}
        for dock in self.all_docks:
            if dock not in visible:
                dock.hide()

        repair = reset or any(
            dock.isHidden() or dock.isFloating() or self.dockWidgetArea(dock) != area
            or self.tabifiedDockWidgets(dock)
            for dock, area in placements
        )
        if repair:
            for dock, area in placements:
                dock.setFloating(False)
                self.removeDockWidget(dock)
                self.addDockWidget(area, dock)
        for dock, _ in placements:
            dock.show()
        if repair and grade_sets is not None:
            self._resize_image_deliver_docks(grade_sets, batch_export)
            if not reset:
                self._image_deliver_default_size_pending = True

    def _resize_image_deliver_docks(self, grade_sets: QDockWidget, batch_export: QDockWidget) -> None:
        width = self.contentsRect().width()
        export_width = width // 3
        self.resizeDocks([grade_sets, batch_export], [width - export_width, export_width],
                         Qt.Orientation.Horizontal)

    def _enforce_work_mode_dock_visibility(self) -> None:
        """Keep unavailable tools hidden after old Qt layouts are restored."""
        mode = self._current_work_mode()
        if mode not in ("image", "video"):
            return
        registry = get_panel_registry()
        allowed = {desc.dock_attr for group in registry.get_panels_by_category(
            work_mode=mode).values() for desc in group}
        excluded = {desc.dock_attr for desc in registry.get_all()
                    if desc.work_mode not in ("all", mode) and desc.dock_attr not in allowed}
        if mode == "image":
            # These core docks also exist when their video modules are off.
            excluded.update(("dock_tracker", "dock_color_keyframes", "dock_color_timeline_clips",
                             "dock_audio_mixer", "dock_deliver", "dockVideoBatchExport"))
        for attr in excluded:
            dock = getattr(self, attr, None)
            if dock is None:
                continue
            dock.hide()
            action = dock.toggleViewAction()
            action.setVisible(False)
            action.setEnabled(False)
            if not dock.property("toposWorkModeHidden"):
                dock.setProperty("toposWorkModeHidden", True)
                # A saved state or delayed tool callback must not resurrect it.
                dock.visibilityChanged.connect(lambda visible, d=dock: d.hide() if visible else None)

    def _edit_uses_default_columns(self) -> bool:
        config = getattr(self, "config_manager", None)
        selection = config.get(self._active_page_layout_key("Edit")) if config is not None else None
        if not isinstance(selection, dict):
            return True
        if selection.get("name"):
            return selection["name"] not in self._custom_page_layouts()
        bundle = selection.get("bundle")
        return not isinstance(bundle, str) or "Edit" not in self._layout_bundles().get(bundle, {})

    def _configure_edit_resize_handles(self) -> None:
        active = self.current_page == "Edit" and getattr(self, "_edit_available", False)
        handles = getattr(self, "_edit_dock_resize_handles", None)
        if active and handles is None and self.dock_program_viewer and self.dock_edit_timeline:
            from src.features.edit.core.workspace_presets import EDITING_PRESET

            dock_map = self._workspace_dock_map()
            sides = tuple(dock for dock in dock_map.values()
                          if dock not in (self.dock_program_viewer, self.dock_edit_timeline))
            media, inspector = EDITING_PRESET.tabify_groups
            groups = tuple(
                tuple(dock_map[name] for name in names if name in dock_map)
                for names in (media, (*inspector, "dockCaptionWorkspace", "dockMulticam"))
            )
            config = getattr(self, "config_manager", None)
            # Start the rebuilt default from its configured ratios, without old pixel overrides.
            mode = self._layout_profile_mode()
            key = f"edit_column_shares_{mode}"
            compact_key = f"edit_compact_column_share_{mode}"
            stored = config.get(key) if config is not None else None
            compact = config.get(compact_key) if config is not None else None
            shares = None
            if (isinstance(stored, list) and len(stored) == 3
                    and all(type(share) is int and share > 0 for share in stored)):
                shares = stored[0], stored[1], stored[2]
            handles = EditDockResizeHandles(
                self, self.dock_program_viewer, self.dock_edit_timeline, sides, groups, saved_shares=shares,
                saved_compact_share=compact if type(compact) is float and 0 < compact < 1 else None,
            )
            if config is not None:
                handles.column_shares_changed.connect(
                    lambda shares: config.set(key, list(shares), auto_save=False),
                )
                handles.compact_share_changed.connect(
                    lambda share: config.set(compact_key, share, auto_save=False),
                )
            self._edit_dock_resize_handles = handles
        if handles is not None:
            shares = None
            if active and self._edit_uses_default_columns():
                tier, _ = self._video_project_screen_tier(self._video_color_defaults())
                left, viewer, right, *_ = self._read_workspace_defaults("video_edit", _VIDEO_EDIT_DEFAULTS)[tier]
                shares = left, viewer, right
            handles.set_active(active, shares)

    def _enforce_page_dock_visibility(self) -> None:
        """Ensure page-specific docks are only visible on their own page.

        This corrects layouts that were saved with incorrect dock visibility,
        e.g. an old Color page layout that includes dock_composite as visible.

        Work-mode aware: in video mode the image-only Library, Batch Export,
        Deblur, Crop & Transform and Info docks are forced hidden; in image
        mode they stay usable. Media Pool is already unavailable in image mode
        (Edit feature disabled at startup).
        """
        self._configure_edit_resize_handles()
        color_active = self.current_page == "Color"
        if _is_video_color(self, "Color"):
            self._configure_video_color_dock_controls(color_active)
        if self._current_work_mode() == "image" and getattr(self, "viewport_container", None) is not None:
            self._configure_photo_color_layout(color_active)
        for attr in ("dock_base_grade", "dock_chromogen"):
            dock = getattr(self, attr, None)
            if dock is not None:
                dock.toggleViewAction().setEnabled(color_active)
                if not color_active:
                    dock.hide()
        for attr in ("files_panel", "library_panel"):
            set_active = getattr(getattr(self, attr, None), "set_color_workspace_active", None)
            if callable(set_active):
                set_active(color_active)
        self._enforce_work_mode_dock_visibility()
        image_workflow = self._current_work_mode() == "image"
        if image_workflow and getattr(self, "dock_export", None) is not None:
            allowed = self.current_page == "Deliver"
            action = self.dock_export.toggleViewAction()
            action.setVisible(allowed)
            action.setEnabled(allowed)
            if not allowed:
                self.dock_export.hide()
        image_library = self._is_image_library_page()
        panels_menu = getattr(self, "_window_panels_menu", None)
        if panels_menu is not None:
            panels_menu.menuAction().setVisible(not image_library)
        if image_library:
            self._apply_image_library_layout()
            return
        grade_sets = getattr(self, "dock_grade_sets", None)
        if grade_sets is not None:
            grade_sets_available = self.current_page == "Color" or (
                image_workflow and self.current_page == "Deliver")
            grade_sets.toggleViewAction().setEnabled(grade_sets_available)
            if not grade_sets_available:
                grade_sets.hide()
        if image_workflow and self.current_page == "Deliver":
            self._apply_image_deliver_layout()
            return
        activate_deliver = getattr(getattr(self, "deliver_page", None), "set_workspace_active", None)
        if callable(activate_deliver):
            activate_deliver(self.current_page == "Deliver")

        mixer_docks = self._audio_workspace_docks()
        mixer_active = self.current_page == "Mixer"
        adr = getattr(self, "dock_adr_take_sheet", None)
        if adr is not None:
            self._sync_adr_panel_host()
            adr.toggleViewAction().setEnabled(mixer_active)
            adr.toggleViewAction().setVisible(mixer_active)
            if not mixer_active:
                adr.hide()
        if getattr(getattr(self, "mixer_page", None), "project_backed", False):
            self.dock_audio_mixer.toggleViewAction().setEnabled(not mixer_active)
        for dock in mixer_docks:
            dock.toggleViewAction().setEnabled(mixer_active)
            if not mixer_active:
                dock.hide()
        if mixer_active:
            # The ADR dock is shared with Edit alongside the Mixer frames.
            for dock in getattr(self, "all_docks", ()):
                if dock not in mixer_docks and dock is not adr:
                    dock.hide()
            return

        # Work-mode scoped docks. Video mode is timeline-centric: the image
        # Library, the image Batch Export (dock_export) and the image-only
        # tool panels (Deblur / Crop & Transform / Info) are hidden. Image
        # mode hides the video-only Color Timeline Clips panel (it has no
        # Edit timeline to read from).
        work_mode = self._current_work_mode()
        if work_mode == "video":
            for attr in ("dock_library", "dock_export",
                         "dock_deblur", "dock_crop", "dock_info"):
                d = getattr(self, attr, None)
                if d is not None:
                    d.setVisible(False)
            if self.current_page == "Library" and self.dock_files.isHidden():
                self.dock_files.show()
        elif work_mode == "image":
            if getattr(self, "dock_color_timeline_clips", None) is not None:
                self.dock_color_timeline_clips.setVisible(False)
            # Video Batch Export is video-only — force hidden in image mode.
            if getattr(self, "dockVideoBatchExport", None) is not None:
                self.dockVideoBatchExport.setVisible(False)

        # Edit page docks — only visible on Edit page.
        # Media Pool is shared by Edit & Composite (like Properties) so users
        # can drag assets into the node editor without leaving Composite.
        edit_only = [self.dock_program_viewer, self.dock_source_viewer,
                     self.dock_edit_timeline, self.dock_edit_info, self.dock_edit_effects,
                     self.dock_ai_edit_assistant, self.dock_adr_take_sheet,
                     self.dock_multicam,
                     # BUG-B4-02: Captions 是 Edit 专属面板（与 Properties 同栈
                     # tab 化），此前漏在 edit_only 清单外，泄漏到其他页面时不
                     # 被强制隐藏，与 ADR / Multicam 行为不一致。
                     self.dock_caption_workspace]
        if self.current_page != "Edit":
            for d in edit_only:
                if d is self.dock_program_viewer and color_active and getattr(self, "_color_program_monitor_active", False):
                    continue
                if d is not None:
                    d.setVisible(False)
        elif getattr(self, "edit_viewport", None) is not None:
            self._migrate_edit_viewport_visibility()

        # Media Pool dock — shared by Edit & Composite; hide on other pages
        if self.current_page not in ("Edit", "Composite"):
            if self.dock_edit_media_pool is not None:
                self.dock_edit_media_pool.setVisible(False)

        # Edit's Properties dock — only visible on the Edit page now that
        # Composite has its own dock_node_properties.
        if self.current_page != "Edit":
            if self.dock_properties is not None:
                self.dock_properties.setVisible(False)

        # Composite page docks — only visible on Composite page
        if self.current_page != "Composite":
            if self.dock_composite:
                self.dock_composite.setVisible(False)
            if self.dock_node_properties is not None:
                self.dock_node_properties.setVisible(False)
            if self.dock_perf:
                self.dock_perf.setVisible(False)
            if self.dock_composite_keyframes:
                self.dock_composite_keyframes.setVisible(False)
            if self.dock_composite_spline:
                self.dock_composite_spline.setVisible(False)
            if self.dock_composite_import_report:
                self.dock_composite_import_report.setVisible(False)
            if self.dock_composite_asset_dependency:
                self.dock_composite_asset_dependency.setVisible(False)
            if self.dock_composite_scene_outliner:
                self.dock_composite_scene_outliner.setVisible(False)
            if self.dock_composite_material:
                self.dock_composite_material.setVisible(False)
            if self.dock_composite_scope:
                self.dock_composite_scope.setVisible(False)

        # Color page docks — only visible on Color page
        for attr in ("dock_warper", "dock_mixer"):
            dock = getattr(self, attr, None)
            if dock is not None:
                allowed = self.current_page == "Color"
                dock.toggleViewAction().setEnabled(allowed)
                if not allowed:
                    if attr == "dock_warper":
                        panel = getattr(self, "warper_panel", None)
                        collapse = getattr(panel, "collapse_expanded", None)
                        if callable(collapse):
                            collapse()
                    dock.hide()
        keyframes = getattr(self, "dock_color_keyframes", None)
        if keyframes is not None:
            allowed = self.current_page == "Color" and work_mode != "image"
            keyframes.toggleViewAction().setEnabled(allowed)
            if not allowed:
                keyframes.hide()
        if self.current_page != "Color":
            if self.dock_color_timeline_clips:
                self.dock_color_timeline_clips.setVisible(False)

        # Deliver page dock — only visible on Deliver page
        # [BUG-B4-03] 此前 _enforce_page_dock_visibility 无 dock_deliver 分支，
        # 旧布局把 Deliver dock 带到其他页面时不会被强制隐藏。Deliver 页自身
        # 的显示由 _migrate_deliver_layout（page_controller 延迟 50ms）负责，
        # 此处只在非 Deliver 页隐藏，不会与之冲突。
        if self.current_page != "Deliver":
            if getattr(self, "dock_deliver", None) is not None:
                self.dock_deliver.setVisible(False)

    # ------------------------------------------------------------------
    # _migrate_deliver_layout
    # ------------------------------------------------------------------
    def _attach_edit_panels_to_deliver(self) -> None:
        """Reuse the live Edit viewer and timeline in Deliver's right workspace."""
        workspace = self.deliver_page
        attach = getattr(workspace, "attach_edit_panels", None)
        if not callable(attach):
            return
        viewport = getattr(self, "edit_viewport", None)
        if viewport is not None:
            viewport.set_deliver_mode(True)
        attach(
            self.program_viewer_widget,
            self.timeline_panel,
            None if viewport is not None else self.dock_program_viewer,
            self.dock_edit_timeline,
        )
        # I01：Deliver 借出即进入 Deliver 页模式——强制 Timeline 监看、
        # 暂停 Source（与 deliver_workspace.edit_panels_borrowed 同步触发）。
        coordinator = getattr(self, "_viewport_playback_coordinator", None)
        if coordinator is not None:
            coordinator.enter_deliver_page()

    def _restore_edit_panels_from_deliver(self) -> None:
        """Put the shared viewer and timeline back into the Edit docks."""
        workspace = self.deliver_page
        detach = getattr(workspace, "detach_edit_panels", None)
        if callable(detach):
            detach()
        viewport = getattr(self, "edit_viewport", None)
        if viewport is not None:
            viewport.restore_program_viewer()
            viewport.set_deliver_mode(False)
        # Returning the widgets does not imply entering Edit. switch_to_page
        # invokes this while current_page is still Deliver; its leave hook has
        # already set PAGE_OTHER. Only the destination Edit enter hook should
        # restore Edit mode and synchronize the saved monitor selection.

    def _sync_edit_viewport_timeline(self, timeline: object) -> None:
        """Keep the monitor's scrub range on the existing timeline timebase."""
        viewport = getattr(self, "edit_viewport", None)
        if viewport is None:
            return
        duration, start, end = self.timeline_panel.get_timeline_view().get_range_presentation(timeline)
        viewport.set_program_range(duration, getattr(timeline, "fps", 24.0), start, end)

    def _setup_edit_viewport_preferences(self) -> None:
        """Restore the versioned display preference before observing UI changes."""
        from src.features.edit.core.viewport_preferences import (
            LEGACY_KEY_DUAL_VIEW,
            LEGACY_KEY_SPLITTER_SIZES,
            parse_viewport_preferences,
        )

        self._edit_viewport_preferences_timer = QTimer(self)
        self._edit_viewport_preferences_timer.setSingleShot(True)
        self._edit_viewport_preferences_timer.setInterval(300)
        self._edit_viewport_preferences_timer.timeout.connect(
            self.config_manager.save_config
        )
        raw = self.config_manager.get(_EDIT_VIEWPORT_PREFERENCES_KEY)
        if raw is None:
            raw = {
                LEGACY_KEY_DUAL_VIEW: self.config_manager.get(
                    LEGACY_KEY_DUAL_VIEW, False
                ),
                LEGACY_KEY_SPLITTER_SIZES: self.config_manager.get(
                    LEGACY_KEY_SPLITTER_SIZES
                ),
            }
        preferences, diagnostics = parse_viewport_preferences(raw)
        for diagnostic in diagnostics:
            logger.info("Edit Viewport preferences: %s", diagnostic)
        self._edit_viewport_preferences = preferences
        viewport = self.edit_viewport
        if preferences.splitter_sizes is not None:
            viewport.restore_splitter_sizes(list(preferences.splitter_sizes))
        viewport.set_dual_view(preferences.dual_view)
        if preferences.single_view_target == "source":
            viewport.show_source(focus=False)
        else:
            viewport.show_program(focus=False)

        # Persist normalized/migrated data without deleting old keys or touching
        # other page layouts. Restoring the widgets itself does not echo a save.
        if raw != preferences.to_dict():
            self.config_manager.set(
                _EDIT_VIEWPORT_PREFERENCES_KEY,
                preferences.to_dict(), auto_save=False,
            )
            self._edit_viewport_preferences_timer.start()
        viewport.mode_changed.connect(self._save_edit_viewport_preferences)
        viewport.active_monitor_changed.connect(self._save_edit_viewport_preferences)
        viewport.splitter.splitterMoved.connect(self._save_edit_viewport_preferences)

    def _save_edit_viewport_preferences(self, *_args: object) -> None:
        """Debounce validated display preferences; ignore Deliver's borrowed UI."""
        from src.features.edit.core.viewport_preferences import (
            ViewportPreferences,
            parse_viewport_preferences,
        )

        viewport = self.edit_viewport
        if (
            getattr(self, "current_page", None) != "Edit"
            or getattr(viewport, "_deliver_mode", False)
        ):
            return
        previous = self._edit_viewport_preferences
        sizes = previous.splitter_sizes
        if viewport.dual_view:
            candidate, _ = parse_viewport_preferences({
                "splitter_sizes": viewport.splitter.sizes(),
            })
            if candidate.splitter_sizes is not None:
                sizes = candidate.splitter_sizes
        preferences = ViewportPreferences(
            dual_view=viewport.dual_view,
            splitter_sizes=sizes,
            single_view_target=(
                "source" if viewport.active_monitor == "source" else "timeline"
            ),
        )
        if preferences == previous:
            return
        self._edit_viewport_preferences = preferences
        self.config_manager.set(
            _EDIT_VIEWPORT_PREFERENCES_KEY,
            preferences.to_dict(), auto_save=False,
        )
        self._edit_viewport_preferences_timer.start()

    def _migrate_edit_viewport_visibility(self) -> None:
        """Reveal the merged monitor once when replacing legacy viewer docks.

        A saved Source-only layout can hide the old Program dock. Qt retains
        that hidden flag even though Source no longer exists, so keep the
        unified panel discoverable on the first Edit visit in each workspace.
        Later user choices, including closing the Viewport, remain respected.
        """
        if self.current_page != "Edit" or getattr(self, "edit_viewport", None) is None:
            return
        key = f"edit_viewport_layout_version_{self._current_product_mode()}_{self._current_work_mode()}"
        if self.config_manager.get(key) == 1:
            return
        self.dock_program_viewer.show()
        self.dock_program_viewer.raise_()
        self.config_manager.set(key, 1, auto_save=False)
        self.page_layouts["Edit"] = self.saveState()
        self._save_current_layout_to_store("Edit")
        self.config_manager.save_config()

    def _migrate_deliver_layout(self) -> None:
        """Repair old Deliver layouts that collapsed the settings dock.

        Earlier builds created the video queue as a separate bottom dock.
        Saved Qt layouts from earlier builds can restore the Deliver dock at a
        width where a side-by-side inspector and queue are unusable.  The
        queue is now inside DeliverWorkspace, which sizes its panes from the
        available space after restoreState() settles.
        """
        if self.current_page != "Deliver":
            return
        if self._current_work_mode() == "image":
            self._enforce_page_dock_visibility()
            if getattr(self, "_image_deliver_default_size_pending", False):
                self._image_deliver_default_size_pending = False
                grade_sets = getattr(self, "dock_grade_sets", None)
                if grade_sets is not None:
                    # Factory states are captured before the window reaches its restored size.
                    self._resize_image_deliver_docks(grade_sets, self.dock_export)
            return
        dock = getattr(self, "dock_deliver", None)
        if dock is None:
            return
        dock.setVisible(True)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, dock)
        workspace = getattr(self, "deliver_page", None)
        ensure_layout = getattr(workspace, "ensure_deliver_layout", None)
        if callable(ensure_layout):
            ensure_layout()

    # ------------------------------------------------------------------
    # _save_all_docked_sizes
    # ------------------------------------------------------------------
    def _save_all_docked_sizes(self) -> None:
        """Save sizes of all docked (non-floating) docks to config.

        This is called after layout is applied to remember dock sizes.
        """
        for dock in self.all_docks:
            obj_name = dock.objectName()
            if obj_name and not dock.isFloating():
                dock_docked_size_key = f"dock_{obj_name}_docked_size"
                self.config_manager.set(dock_docked_size_key, f"{dock.width()}x{dock.height()}", auto_save=False)
        self.config_manager.save_config()

    # ------------------------------------------------------------------
    # _restore_docked_sizes
    # ------------------------------------------------------------------
    def _restore_docked_sizes(self) -> None:
        """Restore sizes for docked (tabified) docks from config.

        This is called after restoreState to apply saved dock sizes.
        Uses resizeDocks for proper splitter proportion restoration.
        """
        if _is_video_color(self, self.current_page) or (
            self.current_page == "Color" and self._current_work_mode() == "image"
        ):
            return  # Qt already restored the nested row/column proportions.

        # Group docks by their dock area to restore proportions correctly
        dock_areas = {}
        for dock in self.all_docks:
            obj_name = dock.objectName()
            if obj_name and not dock.isFloating():
                dock_area = self.dockWidgetArea(dock)
                if dock_area not in dock_areas:
                    dock_areas[dock_area] = []

                dock_docked_size_key = f"dock_{obj_name}_docked_size"
                saved_size = self.config_manager.get(dock_docked_size_key)
                if saved_size:
                    # [AUD-2602] 捕获具体解析异常并记录原始值，不用裸
                    # except 吞掉一切；非正尺寸同样拒绝。
                    try:
                        width, height = map(int, str(saved_size).split('x'))
                        # For horizontal areas (Left/Right), use width; for vertical (Bottom/Top), use height
                        if dock_area in (Qt.DockWidgetArea.LeftDockWidgetArea, Qt.DockWidgetArea.RightDockWidgetArea):
                            if width > 0:
                                dock_areas[dock_area].append((dock, width))
                        elif dock_area in (Qt.DockWidgetArea.BottomDockWidgetArea, Qt.DockWidgetArea.TopDockWidgetArea):
                            if height > 0:
                                if isinstance(dock, ColorToolDockWidget):
                                    height = min(COLOR_TOOL_DOCK_MAX_HEIGHT, height + dock.tab_height())
                                dock_areas[dock_area].append((dock, height))
                    except (TypeError, ValueError):
                        logger.warning(
                            "Ignoring corrupt docked size %r for dock %s",
                            saved_size, obj_name)

        # Apply sizes using resizeDocks for each area
        for area, docks_with_sizes in dock_areas.items():
            if docks_with_sizes:
                docks = [d for d, s in docks_with_sizes]
                sizes = [s for d, s in docks_with_sizes]
                if area in (Qt.DockWidgetArea.LeftDockWidgetArea, Qt.DockWidgetArea.RightDockWidgetArea):
                    self.resizeDocks(docks, sizes, Qt.Orientation.Horizontal)
                elif area in (Qt.DockWidgetArea.BottomDockWidgetArea, Qt.DockWidgetArea.TopDockWidgetArea):
                    self.resizeDocks(docks, sizes, Qt.Orientation.Vertical)

        # Force update the layout and ensure all widgets are properly sized
        self.updateGeometry()
        self.repaint()

    # ------------------------------------------------------------------
    # _sync_floating_docks
    # ------------------------------------------------------------------
    def _sync_floating_docks(self) -> None:
        """Sync floating docks state - ensure floating docks have a draggable titlebar.

        This method is called during initialization to restore saved floating dock states.
        浮动 dock 必须用 FloatingDockTitleBar（有真实高度可拖拽），不能用原生标题栏
        （详见 _perform_update_all_titlebars 浮动分支注释）。
        """
        self._floating_docks.clear()

        for dock in self.all_docks:
            if dock.isFloating():
                self._floating_docks.add(dock)
                # 浮动 dock 用有真实高度的 FloatingDockTitleBar（可拖拽移动/停靠）
                if dock is getattr(self, "dock_audio_clip_editor", None):
                    self._sync_mixer_clip_editor_dock_mode()
                elif not isinstance(dock.titleBarWidget(), FloatingDockTitleBar):
                    dock.setTitleBarWidget(FloatingDockTitleBar(dock))
            else:
                # Ensure docked docks have minimal height titlebar (1px for dragging)
                # 与 _perform_update_all_titlebars 一致，用 maximumHeight() 判定：
                # 已是 1px 标题栏则跳过，避免无谓重建。
                current_tw = dock.titleBarWidget()
                if isinstance(current_tw, FloatingDockTitleBar) or \
                        not isinstance(current_tw, QWidget) or \
                        current_tw.maximumHeight() != 1:
                    hidden_titlebar = QWidget()
                    hidden_titlebar.setFixedHeight(1)
                    dock.setTitleBarWidget(hidden_titlebar)
