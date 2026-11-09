from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from PyQt6.QtCore import Qt, QByteArray, QTimer
from PyQt6.QtGui import QAction
from PyQt6.QtWidgets import (QDockWidget, QHBoxLayout, QInputDialog, QLabel,
                             QMenu, QMessageBox, QSizePolicy, QTabWidget,
                             QTabBar, QToolButton, QVBoxLayout, QWidget, QApplication)

from src.shared.i18n.i18n import I18N
from src.shared.layout.store import LayoutStore
from src.shared.layout.profile import LayoutProfile
from src.shared.panels.registry import get_panel_registry
from src.shared.ui.style_sheet import DARK_THEME
from src.shared.ui.dock_context_menu import add_dock_management_actions
from src.shared.ui.panel_divider import PanelDockWidget
from src.shared.ui.dock_tab_style import apply_dock_tab_bar_style

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

_VIEWPORT_MIN_WIDTH = 640
_VIEWPORT_MIN_HEIGHT = 360
_EDIT_VIEWPORT_PREFERENCES_KEY = "edit_viewport_preferences"


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
_EDIT_TIMELINE_DOCK_DEFAULT_HEIGHT = 280
_EDIT_TIMELINE_DOCK_MIN_HEIGHT = 160
_EDIT_TIMELINE_DOCK_MAX_HEIGHT = 480
# 可容纳工具条+时间尺+一条可辨 V 轨与 A 轨的最小高度
_EDIT_TIMELINE_DOCK_OPERABLE_MIN_HEIGHT = 240
_EDIT_TIMELINE_DOCK_MIGRATION_DELAY_MS = 50
_COLOR_GRADING_DOCK_LAYOUT_MIGRATION_KEY = "color_grading_dock_layout_version"
_COLOR_GRADING_DOCK_LAYOUT_MIGRATION_VERSION = 2
_COLOR_TOOLS_DOCK_HEIGHT_RATIO = 0.32
_COLOR_TOOLS_DOCK_HEIGHT_MIN_RATIO = 0.27
_COLOR_TOOLS_DOCK_HEIGHT_MAX_RATIO = 0.37
_COLOR_GRADING_DOCK_MIN_HEIGHT = 160
# The Color Wheel canvas stops growing at radius 100px. At a 1260px panel
# width it reaches that radius with a ~403px GradingPanel height; the dock
# frame/title spacer needs a few more pixels. Extra dock height is blank UI.
_COLOR_TOOL_DOCK_MAX_HEIGHT = 410
_COLOR_GRADING_DOCK_MAX_HEIGHT = _COLOR_TOOL_DOCK_MAX_HEIGHT
_UNRESTRICTED_DOCK_HEIGHT = 16777215  # Qt's QWIDGETSIZE_MAX
_COLOR_TOOL_DOCK_NAMES = frozenset({
    "dockDenoise", "dockDeblur", "dockGrading", "dockCurves",
    "dockColorMapping", "dockBlurSharpen", "dockGrain", "dockMasks",
    "dockQualifier", "dockRaw", "dockAnalysis", "dockCrop",
    "dockTracker", "dockColorKeyframes",
})
_COLOR_MAPPING_DOCK_LAYOUT_MIGRATION_KEY = "color_mapping_dock_layout_version"
_COLOR_MAPPING_DOCK_LAYOUT_MIGRATION_VERSION = 1
_COLOR_KEYFRAMES_DOCK_PLACEMENT_KEY = "color_keyframes_dock_placed"
_COMPOSITE_SCOPES_DOCK_LAYOUT_MIGRATION_KEY = "composite_scopes_dock_layout_version"
_COMPOSITE_SCOPES_DOCK_LAYOUT_MIGRATION_VERSION = 1

# 浮动 dock 标题栏高度（与原生窗口标题栏接近）
_FLOATING_TITLEBAR_HEIGHT = 28


class FloatingDockTitleBar(QWidget):
    """浮动 QDockWidget 使用的自定义标题栏。

    背景：项目全局 QSS 把 ``QDockWidget::title`` 的 ``max-height`` 设为 0px
    （停靠态靠自定义 1px widget 占位，标题栏由 tab 承载）。这导致 dock 浮动后，
    若依赖 ``setTitleBarWidget(None)`` 切回原生标题栏，浮动窗口顶部没有任何
    可抓取的拖拽区域——既无法移动浮动窗口，也无法拖到边缘重新停靠。

    本类提供一个有真实高度的标题栏：拖动它即可移动浮动窗口并触发边缘停靠
    判定（QDockWidget 会接管自定义 titlebar 上的鼠标拖拽），右侧附带关闭按钮。
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

        close_btn = QToolButton(self)
        close_btn.setText("×")
        close_btn.setFixedSize(20, 20)
        close_btn.setCursor(Qt.CursorShape.ArrowCursor)
        close_btn.setStyleSheet(
            "QToolButton { color: #999; border: none; background: transparent; }"
            "QToolButton:hover { color: #fff; background: #c42b1c; }"
        )
        close_btn.clicked.connect(lambda: dock.setVisible(False))
        layout.addWidget(close_btn)

        # 标题随 dock 窗口标题同步
        dock.windowTitleChanged.connect(self._title_label.setText)


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

    def _sync_color_tool_dock_max_height(
        self, dock: QDockWidget, *, is_floating: bool | None = None
    ) -> None:
        """Cap docked Color tools where the wheel reaches its maximum radius."""
        floating = dock.isFloating() if is_floating is None else is_floating
        is_bottom = self.dockWidgetArea(dock) == Qt.DockWidgetArea.BottomDockWidgetArea
        dock.setMaximumHeight(
            _COLOR_TOOL_DOCK_MAX_HEIGHT
            if is_bottom and not floating
            else _UNRESTRICTED_DOCK_HEIGHT
        )

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
            dock = PanelDockWidget(title, self)
            dock.setObjectName(obj_name)
            dock.setWidget(widget)
            if obj_name in _COLOR_TOOL_DOCK_NAMES:
                dock.setMaximumHeight(_COLOR_TOOL_DOCK_MAX_HEIGHT)
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
            if obj_name in _COLOR_TOOL_DOCK_NAMES:
                dock.dockLocationChanged.connect(
                    lambda _area, d=dock: self._sync_color_tool_dock_max_height(d)
                )
            if obj_name in _COLOR_TOOL_DOCK_NAMES:
                # Remove the docked cap before restoring a saved floating size.
                dock.topLevelChanged.connect(
                    lambda is_floating, d=dock: self._sync_color_tool_dock_max_height(
                        d, is_floating=is_floating
                    )
                )
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

        # Preserve the dock identity used by saved layouts; replace only its
        # visible UI. Backend migration is tracked in the Color Nodes plan.
        self.dock_layers = create_custom_dock(I18N.tr("color_nodes.nodes", "Nodes"), self.color_node_panel, "dockLayers")
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.dock_layers)
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


        self.dock_curves = create_custom_dock(I18N.tr("panel.curves", "Curves"), self.curves_panel, "dockCurves")
        self.dock_curves.setMinimumWidth(420)
        self.dock_curves.setMaximumWidth(1800)
        self.dock_curves.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred
        )
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self.dock_curves)
        self.dock_curves.visibilityChanged.connect(self._on_curves_scope_visibility_changed)

        self.dock_color_mapping = create_custom_dock(
            I18N.tr("panel.color_mapping", "Color Mapping"),
            self.color_mapping_panel,
            "dockColorMapping",
        )
        self.addDockWidget(
            Qt.DockWidgetArea.BottomDockWidgetArea,
            self.dock_color_mapping,
        )

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
        self.dock_export = create_custom_dock(I18N.tr("panel.image_export", "Image Export"), self.export_panel, "dockExport")
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
            self.timeline_panel.project_changed.connect(self._on_media_pool_project_changed)

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
                    self.edit_viewport.program_stop_requested.connect(controller.stop_in_place)
                    controller.frame_changed.connect(self.edit_viewport.set_program_position)
                self.edit_page.timeline_display_changed.connect(self._sync_edit_viewport_timeline)
                self.edit_viewport.bind_timeline_widget(
                    self.timeline_panel.get_timeline_view().viewport()
                )
                self._sync_edit_viewport_timeline(
                    self.timeline_panel.get_timeline_view().get_timeline()
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

        # Connect MediaPoolPanel double-click to Source Viewer (P3.1)
        if self._edit_available and self.media_pool_panel is not None:
            self.media_pool_panel.asset_double_clicked.connect(self._on_asset_double_clicked)
            # Single-click preview: load asset into Source Viewer without
            # stealing focus or switching pages (Finder-like quick preview).
            self.media_pool_panel.asset_selected.connect(self._on_asset_selected)

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

        # Unified Properties Dock - Right area (shared by Edit & Composite pages)
        # Create generic properties dock if edit_properties_panel exists
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

            # Edit Effects - Right area, tabified with Properties
            if self.edit_effects_panel is not None:
                self.dock_edit_effects = create_custom_dock(
                    I18N.tr("panel.edit_effects", "Effects"), self.edit_effects_panel, "dockEditEffects"
                )
                self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.dock_edit_effects)
                self.tabifyDockWidget(self.dock_properties, self.dock_edit_effects)

            # SUB-P2-03: Caption Workspace - Right area, tabified with Properties
            # so it shares the same dock stack as Properties/Effects on Edit page.
            if self.caption_workspace_panel is not None:
                self.dock_caption_workspace = create_custom_dock(
                    I18N.tr("panel.caption_workspace", "Captions"),
                    self.caption_workspace_panel, "dockCaptionWorkspace"
                )
                self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea,
                                   self.dock_caption_workspace)
                # Tabify with properties so the three right-area panels stack.
                self.tabifyDockWidget(self.dock_properties, self.dock_caption_workspace)

            # P1-10: ADR Take Sheet - Right area, tabified with Properties
            # (same stack as Captions; default hidden, open via Panels menu or
            # the Audio workspace preset).
            if self.adr_take_sheet_panel is not None:
                self.dock_adr_take_sheet = create_custom_dock(
                    I18N.tr("panel.adr_take_sheet", "ADR / Takes"),
                    self.adr_take_sheet_panel, "dockAdrTakeSheet"
                )
                self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea,
                                   self.dock_adr_take_sheet)
                if self.dock_properties is not None:
                    self.tabifyDockWidget(self.dock_properties,
                                          self.dock_adr_take_sheet)
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
                I18N.tr("panel.deliver", "Deliver"), self.deliver_page, "dockDeliver"
            )
            self.dock_deliver.setMinimumWidth(1220)
            self.dock_deliver.setMinimumHeight(620)
            self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.dock_deliver)
            attach_batch_host = getattr(self.deliver_page, "attach_batch_panel_host", None)
            if callable(attach_batch_host):
                attach_batch_host(self)

        self.all_docks = [
            self.dock_viewport, self.dock_library, self.dock_files, self.dock_info,
            self.dock_layers, self.dock_denoise, self.dock_deblur,
            self.dock_grading, self.dock_curves, self.dock_color_mapping,
            self.dock_audio_mixer, self.dock_effects, self.dock_grain,
            self.dock_masks, self.dock_qualifier, self.dock_lut,
            self.dock_raw, self.dock_analysis, self.dock_export,
            self.dock_crop, self.dock_plugin, self.dock_script, self.dock_presets,
            self.dock_color_timeline_clips, self.dock_tracker, self.dock_color_keyframes,
        ]
        # Add Edit docks if available
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
        # Initial splitting and proportions
        self.apply_default_layout()

        # Update initial titlebar states immediately
        self._perform_update_all_titlebars()

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

    # ------------------------------------------------------------------
    # initialize_page_layouts
    # ------------------------------------------------------------------
    def initialize_page_layouts(self) -> None:
        """Pre-define the look of each page or restore from config."""
        # Phase A: Create LayoutStore and migrate legacy page_layouts
        self.layout_store = LayoutStore(self.config_manager)
        try:
            self.layout_store.migrate_legacy_page_layouts()
        except Exception as exc:
            logger.warning("LayoutStore migration failed (non-fatal): %s", exc)

        saved_layouts = self.config_manager.get("page_layouts")
        # page_layouts may be mode-scoped: {effective_mode: {page: hex}}, or a
        # legacy flat {page: hex}. Pick the current mode's bucket (fallback to
        # "all") so image/video layouts stay independent.
        if saved_layouts and isinstance(saved_layouts, dict):
            try:
                from src.shared.feature_flags import get_effective_layout_mode
                elm = get_effective_layout_mode(self.config_manager)
            except Exception:
                elm = "studio"
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

            # Phase A: Also try to load from LayoutStore for current product mode.
            # This overrides legacy data if LayoutStore has newer profiles.
            self._load_layouts_from_store()

            # Ensure current_page has a layout state (use default if missing)
            if self.current_page not in self.page_layouts:
                self.page_layouts[self.current_page] = self.saveState()

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
        _factory_geom, factory_state, factory_panels = self._load_factory_layout()
        if factory_state:
            for page, state_hex in (factory_panels or {}).items():
                if isinstance(state_hex, str):
                    self.page_layouts[page] = QByteArray.fromHex(state_hex.encode())
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

            self.restoreState(self.page_layouts[self.current_page])
            # force=True 与硬编码分支一致：首次启动必须走完整页面激活路径
            # （dock 可见性、enter_page_hook / on_enter）。switch_to_page 会先
            # 把当前（出厂）排列快照进 page_layouts 再原样恢复，播种不会破坏。
            self.switch_to_page(self.current_page, force=True)
            return

        # Fallback to hardcoded default layouts if none saved
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
        self.page_layouts["Color"] = self.saveState()

        # 1. Library Layout
        for d in self.all_docks: d.setVisible(False)
        self.dock_viewport.setVisible(True)
        self.dock_library.setVisible(True)
        self.dock_info.setVisible(True)
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, self.dock_library)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.dock_info)
        self.page_layouts["Library"] = self.saveState()

        # 2. Edit Layout (P1 phase) - conditional
        # Default layout: Media Pool left, unified Viewport top, Timeline bottom.
        if self._edit_available:
            for d in self.all_docks: d.setVisible(False)
            if self.dock_properties is not None and self.edit_properties_panel is not None:
                self.dock_properties.setWidget(self.edit_properties_panel)
            if self.dock_program_viewer:
                self.dock_program_viewer.setVisible(True)
            if self.dock_edit_media_pool:
                self.dock_edit_media_pool.setVisible(True)
            if self.dock_edit_timeline:
                self.dock_edit_timeline.setVisible(True)
            if self.dock_properties:
                self.dock_properties.setVisible(True)
            if self.dock_edit_info:
                self.dock_edit_info.setVisible(True)
            if self.dock_edit_effects:
                self.dock_edit_effects.setVisible(True)
            if self.dock_program_viewer:
                self.addDockWidget(Qt.DockWidgetArea.TopDockWidgetArea, self.dock_program_viewer)
            if self.dock_program_viewer:
                self.dock_program_viewer.raise_()
            if self.dock_edit_media_pool:
                self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, self.dock_edit_media_pool)
            if self.dock_edit_timeline:
                self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self.dock_edit_timeline)
            if self.dock_properties:
                self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.dock_properties)
            if self.dock_edit_info:
                self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.dock_edit_info)
            if self.dock_edit_effects:
                self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.dock_edit_effects)
            if self.dock_properties and self.dock_edit_effects:
                self.tabifyDockWidget(self.dock_properties, self.dock_edit_effects)
            if self.dock_properties and self.dock_edit_info:
                self.tabifyDockWidget(self.dock_properties, self.dock_edit_info)
            # SUB-P2-03: Caption Workspace joins the right-area stack.
            if self.dock_caption_workspace:
                self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea,
                                   self.dock_caption_workspace)
                if self.dock_properties:
                    self.tabifyDockWidget(self.dock_properties,
                                          self.dock_caption_workspace)
                # Default to hidden; user opens via Panels menu / tab.
                self.dock_caption_workspace.setVisible(False)
            # P1-10: ADR Take Sheet joins the right-area stack (default hidden).
            if self.dock_adr_take_sheet:
                self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea,
                                   self.dock_adr_take_sheet)
                if self.dock_properties:
                    self.tabifyDockWidget(self.dock_properties,
                                          self.dock_adr_take_sheet)
                self.dock_adr_take_sheet.setVisible(False)
            # P1-01: Multicam 同一右侧栈（默认隐藏）。
            if self.dock_multicam:
                self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea,
                                   self.dock_multicam)
                if self.dock_properties:
                    self.tabifyDockWidget(self.dock_properties, self.dock_multicam)
                self.dock_multicam.setVisible(False)
            # Re-adding Properties above detaches hidden tabs from its stack.
            # Keep the default Edit layout's AI panel in that shared group;
            # the panel itself remains hidden until the user enables it.
            if self.dock_properties and self.dock_ai_edit_assistant:
                self.tabifyDockWidget(
                    self.dock_properties, self.dock_ai_edit_assistant
                )
            # Resize docks (only if they exist)
            resize_docks_list = []
            resize_docks_sizes = []
            if self.dock_edit_media_pool:
                resize_docks_list.append(self.dock_edit_media_pool)
                resize_docks_sizes.append(250)
            if self.dock_program_viewer:
                resize_docks_list.append(self.dock_program_viewer)
                resize_docks_sizes.append(600)
            if self.dock_properties:
                resize_docks_list.append(self.dock_properties)
                resize_docks_sizes.append(280)
            if resize_docks_list:
                self.resizeDocks(
                    resize_docks_list,
                    resize_docks_sizes,
                    Qt.Orientation.Horizontal
                )
            # Vertical resize for timeline
            if self.dock_edit_timeline:
                self.resizeDocks(
                    [self.dock_edit_timeline],
                    [_EDIT_TIMELINE_DOCK_DEFAULT_HEIGHT],
                    Qt.Orientation.Vertical
                )
            self.page_layouts["Edit"] = self.saveState()

        # 3. Deliver Layout (P9.3) - conditional
        if self._deliver_available and self.dock_deliver:
            for d in self.all_docks: d.setVisible(False)
            self.dock_viewport.setVisible(True)
            self.dock_deliver.setVisible(True)
            self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.dock_deliver)
            self.resizeDocks(
                [self.dock_deliver],
                [1320],
                Qt.Orientation.Horizontal
            )
            self.resizeDocks(
                [self.dock_deliver],
                [680],
                Qt.Orientation.Vertical
            )
            self.page_layouts["Deliver"] = self.saveState()

        # 4. Composite Layout (if available)
        if self._composite_available and self.dock_composite:
            for d in self.all_docks: d.setVisible(False)
            self.dock_viewport.setVisible(True)
            self.dock_composite.setVisible(True)
            # Composite has its own Node Properties dock (no longer borrows
            # Edit's dock_properties, which is absent in Image mode).
            if self.dock_node_properties:
                self.dock_node_properties.setVisible(True)
            # Media Pool is shared with Composite so assets can be dragged
            # into the node editor.
            if self.dock_edit_media_pool is not None:
                self.dock_edit_media_pool.setVisible(True)
                self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, self.dock_edit_media_pool)
            self.addDockWidget(Qt.DockWidgetArea.TopDockWidgetArea, self.dock_composite)
            if self.dock_node_properties:
                self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.dock_node_properties)
            # The Inspector stack is contextual: Node Properties is the
            # default tab, while Scene Outliner and Material remain available
            # without consuming additional right-side columns.
            if self.dock_composite_scene_outliner:
                self.addDockWidget(
                    Qt.DockWidgetArea.RightDockWidgetArea,
                    self.dock_composite_scene_outliner)
                self.dock_composite_scene_outliner.setVisible(False)
                if self.dock_node_properties:
                    self.tabifyDockWidget(
                        self.dock_node_properties,
                        self.dock_composite_scene_outliner)
            if self.dock_composite_material:
                self.addDockWidget(
                    Qt.DockWidgetArea.RightDockWidgetArea,
                    self.dock_composite_material)
                self.dock_composite_material.setVisible(False)
                inspector_anchor = (self.dock_node_properties or
                                    self.dock_composite_scene_outliner)
                if inspector_anchor:
                    self.tabifyDockWidget(
                        inspector_anchor,
                        self.dock_composite_material)
            # Scopes is an independent bottom dock, visible in the default
            # Composite workspace. It intentionally is not tabified with the
            # animation docks: it is a persistent monitoring surface, just as
            # on the Color page.
            if self.dock_composite_scope:
                self.addDockWidget(
                    Qt.DockWidgetArea.BottomDockWidgetArea,
                    self.dock_composite_scope)
                self.dock_composite_scope.setVisible(True)
            # Asset health is a separate workflow from the node inspector.
            # Keep the two asset-facing reports in their own tab group.
            if self.dock_composite_asset_dependency:
                self.addDockWidget(
                    Qt.DockWidgetArea.RightDockWidgetArea,
                    self.dock_composite_asset_dependency)
                self.dock_composite_asset_dependency.setVisible(False)
            if self.dock_composite_import_report:
                self.addDockWidget(
                    Qt.DockWidgetArea.RightDockWidgetArea,
                    self.dock_composite_import_report)
                self.dock_composite_import_report.setVisible(False)
                if self.dock_composite_asset_dependency:
                    self.tabifyDockWidget(
                        self.dock_composite_asset_dependency,
                        self.dock_composite_import_report)
            # Bottom animation docks belong to the Composite layout. Keep
            # Keyframes and Spline as separate docks until Spline is fully
            # integrated into the Keyframes editor.
            if self.dock_composite_keyframes:
                self.dock_composite_keyframes.setVisible(True)
                self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self.dock_composite_keyframes)
            if self.dock_composite_spline:
                # Keep the future graph-editor surface available but opt-in
                # until it is fully integrated into Keyframes.
                self.dock_composite_spline.setVisible(False)
                self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self.dock_composite_spline)
            self.resizeDocks(
                [self.dock_composite, self.dock_viewport, self.dock_node_properties if self.dock_node_properties else QWidget()],
                [600, 800, 300],
                Qt.Orientation.Horizontal
            )
            self.page_layouts["Composite"] = self.saveState()

        # Ensure current_page has a layout state (for pages other than Color/Library/Deliver)
        if self.current_page not in self.page_layouts:
            self.page_layouts[self.current_page] = self.saveState()

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
        """Return the current studio work mode ('all'/'image'/'video').

        Non-studio products always report 'all' (they keep their own preset).
        Used to scope saved layout presets per work mode.
        """
        from src.shared.feature_flags import get_work_mode
        try:
            return get_work_mode(getattr(self, "config_manager", None))
        except Exception:
            return "all"

    def _get_registered_panel_ids(self) -> set[str]:
        """Return the set of currently registered panel IDs."""
        registry = get_panel_registry()
        return {desc.panel_id for desc in registry.get_all()}

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
        product_mode = self._current_product_mode()
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

    def _save_current_layout_to_store(self, page_id: str) -> None:
        """Save the current dock layout to LayoutStore.

        Called alongside the legacy page_layouts save so both
        systems stay in sync during the transition period.
        """
        try:
            product_mode = self._current_product_mode()
            qt_state = self.saveState()
            qt_state_hex = qt_state.toHex().data().decode()

            # Collect visible panel IDs from registered panels
            registry = get_panel_registry()
            visible_panel_ids: list[str] = []
            for desc in registry.get_all():
                dock = getattr(self, desc.dock_attr, None) if desc.dock_attr else None
                if dock is not None and isinstance(dock, QDockWidget) and dock.isVisible():
                    visible_panel_ids.append(desc.panel_id)

            profile = LayoutProfile(
                product_mode=product_mode,
                page_id=page_id,
                visible_panels=tuple(visible_panel_ids),
                qt_state_hex=qt_state_hex,
            )
            self.layout_store.save_profile(profile)
        except Exception as exc:
            logger.warning("Failed to save layout to LayoutStore: %s", exc)

    def _restore_layout_from_store(self, page_id: str) -> bool:
        """Try to restore layout from LayoutStore.

        Returns True if a LayoutStore profile was found (regardless of
        whether restoreState succeeded), False if no profile exists.
        """
        try:
            product_mode = self._current_product_mode()
            registered_ids = self._get_registered_panel_ids()
            enabled_features = self._get_enabled_features()

            profile = self.layout_store.get_or_default(
                product_mode, page_id,
                enabled_features=enabled_features,
                registered_panel_ids=registered_ids,
            )

            if profile and profile.qt_state_hex:
                state = QByteArray.fromHex(profile.qt_state_hex.encode())
                if not self.restoreState(state):
                    logger.debug(
                        "LayoutStore restoreState failed for %s/%s, "
                        "will fall back to default layout",
                        product_mode, page_id,
                    )
                return True
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
                try:
                    from src.shared.feature_flags import get_effective_layout_mode
                    mode_key = get_effective_layout_mode(self.config_manager)
                except Exception:
                    mode_key = "all"
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
            target_height = _EDIT_TIMELINE_DOCK_DEFAULT_HEIGHT
        else:
            target_height = saved_height
        # Only migrate when the saved splitter state clearly disagrees with
        # the user's recorded dock height.  This avoids resetting a deliberately
        # tall Timeline layout created after the migration is available.
        current_height = dock.height()
        undersized = (
            current_height < _EDIT_TIMELINE_DOCK_OPERABLE_MIN_HEIGHT
            and target_height < _EDIT_TIMELINE_DOCK_OPERABLE_MIN_HEIGHT
        )
        if undersized:
            # APP-03：过矮布局（如 102px）无法编辑——拉回可操作默认高度
            target_height = _EDIT_TIMELINE_DOCK_DEFAULT_HEIGHT
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
        """Balance the shared Color tools dock height once for this layout.

        Color Wheel, Qualifier, and Scopes share the same bottom dock row. A
        layout restored from a different display can leave that row too short
        or too tall, so migrate outlier ratios to a screen-relative height.
        The ratio applies once; users can then resize the row themselves.
        """
        migration_version = self.config_manager.get(
            _COLOR_GRADING_DOCK_LAYOUT_MIGRATION_KEY
        )
        if (
            isinstance(migration_version, int)
            and migration_version >= _COLOR_GRADING_DOCK_LAYOUT_MIGRATION_VERSION
        ):
            return

        dock = self.dock_grading
        viewport_dock = self.dock_viewport
        if (
            self.current_page != "Color"
            or dock is None
            or viewport_dock is None
            or dock.isFloating()
            or viewport_dock.isFloating()
            or self.dockWidgetArea(dock)
            != Qt.DockWidgetArea.BottomDockWidgetArea
            or self.dockWidgetArea(viewport_dock)
            != Qt.DockWidgetArea.TopDockWidgetArea
        ):
            return

        current_height = dock.height()
        available_height = viewport_dock.height() + current_height
        if available_height <= 0:
            return

        current_ratio = current_height / available_height
        if _COLOR_TOOLS_DOCK_HEIGHT_MIN_RATIO <= current_ratio <= \
                _COLOR_TOOLS_DOCK_HEIGHT_MAX_RATIO:
            self.config_manager.set(
                _COLOR_GRADING_DOCK_LAYOUT_MIGRATION_KEY,
                _COLOR_GRADING_DOCK_LAYOUT_MIGRATION_VERSION,
                auto_save=False,
            )
            self.config_manager.save_config()
            return

        target_height = round(available_height * _COLOR_TOOLS_DOCK_HEIGHT_RATIO)
        target_height = max(
            _COLOR_GRADING_DOCK_MIN_HEIGHT,
            min(_COLOR_GRADING_DOCK_MAX_HEIGHT, target_height),
        )
        # Keep the viewer above its existing minimum when the window is short.
        max_tools_height = available_height - viewport_dock.minimumHeight()
        if max_tools_height <= 0:
            return
        target_height = min(target_height, max_tools_height)
        viewport_target_height = max(
            viewport_dock.minimumHeight(),
            available_height - target_height,
        )
        self.resizeDocks(
            [viewport_dock, dock],
            [viewport_target_height, target_height],
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
            f"{dock.width()}x{target_height}",
            auto_save=False,
        )
        self._save_current_layout_to_store("Color")
        self.config_manager.save_config()
        QTimer.singleShot(100, self._save_all_docked_sizes)
        logger.info(
            "Balanced Color tools dock from %dpx to %dpx (%.0f%% to %.0f%%)",
            current_height, target_height, current_ratio * 100.0,
            target_height / available_height * 100.0,
        )

    def _migrate_color_mapping_dock_layout(self, force: bool = False) -> None:
        """Merge the legacy color-remapping docks into one tabbed dock once.

        ``QMainWindow.restoreState`` ignores docks that no longer exist and
        hides newly created docks that are absent from an old state blob.  The
        one-shot migration rehomes the new dock beside the existing Curves /
        Effects tab stack, then persists the upgraded Color layout.  Later
        launches leave the user's chosen dock arrangement untouched.
        """
        self._migrate_color_resource_dock_layout(force=force)
        migration_version = self.config_manager.get(
            _COLOR_MAPPING_DOCK_LAYOUT_MIGRATION_KEY
        )
        if (
            not force
            and isinstance(migration_version, int)
            and migration_version >= _COLOR_MAPPING_DOCK_LAYOUT_MIGRATION_VERSION
        ):
            return

        if self.current_page != "Color":
            return

        mapping_dock = getattr(self, "dock_color_mapping", None)
        curves_dock = getattr(self, "dock_curves", None)
        effects_dock = getattr(self, "dock_effects", None)
        if mapping_dock is None or curves_dock is None:
            return

        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, mapping_dock)
        self.tabifyDockWidget(curves_dock, mapping_dock)
        if effects_dock is not None:
            self.tabifyDockWidget(mapping_dock, effects_dock)
        mapping_dock.setVisible(True)

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
            _COLOR_MAPPING_DOCK_LAYOUT_MIGRATION_KEY,
            _COLOR_MAPPING_DOCK_LAYOUT_MIGRATION_VERSION,
            auto_save=False,
        )
        self._save_current_layout_to_store("Color")
        self.config_manager.save_config()
        logger.info("Migrated legacy color remapping docks to Color Mapping")

    def _toggle_color_node_display_mode(self) -> None:
        self.dock_viewport.setVisible(self.dock_viewport.isHidden())

    def _migrate_color_resource_dock_layout(self, force: bool = False) -> None:
        """One-time upgrade; later launches preserve custom/floating arrangements."""
        key = "color_resource_browser_layout_v1"
        if self.current_page != "Color" or (not force and self.config_manager.get(key) == 1):
            return
        gallery = getattr(self, "dock_presets", None)
        if gallery is None:
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

    def reset_product_layout(self) -> None:
        """Clear all saved layouts for the current product mode only.

        Layouts for other product modes are preserved.
        """
        product_mode = self._current_product_mode()
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
        if dock.objectName() == "dockEditEffects":
            panel = getattr(self, "edit_effects_panel", None)
            show_menu = getattr(panel, "show_unified_context_menu", None)
            if callable(show_menu):
                show_menu(dock.mapToGlobal(pos))
                return

        # Skip context menu for docks that have their own internal menus
        if dock.objectName() in ("dockCurves", "dockColorMapping", "dockMasks", "dockEditTimeline"):
            return

        menu = QMenu(self)
        menu.setStyleSheet(DARK_THEME)

        add_dock_management_actions(menu, dock)

        menu.exec(dock.mapToGlobal(pos))

    # ------------------------------------------------------------------
    # _style_dock_tab_bars
    # ------------------------------------------------------------------
    def _style_dock_tab_bars(self) -> None:
        """Apply platform-neutral styling to Qt's internal dock tab bars."""
        for tab_bar in self.findChildren(QTabBar):
            # QMainWindow creates this internal bar directly under itself;
            # regular panel QTabBars are descendants of their own widgets.
            if tab_bar.parent() is self:
                apply_dock_tab_bar_style(tab_bar)

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
        for d in self.all_docks:
            d.setVisible(True)
            d.setFloating(False)

        # Work-mode scoped docks — keep _enforce_page_dock_visibility semantics
        # in the hardcoded fallback so there is no first-run flash of panels
        # that do not belong to the active workflow.
        work_mode = self._current_work_mode()
        if work_mode == "video":
            # Video workflow: hide image-only Library, Batch Export and the
            # image-only tool panels (Deblur / Crop / Info).
            for attr in ("dock_library", "dock_export",
                         "dock_deblur", "dock_crop", "dock_info"):
                d = getattr(self, attr, None)
                if d is not None:
                    d.setVisible(False)
        elif work_mode == "image":
            # Image workflow: hide the video-only Color Timeline Clips panel
            # (it has no Edit timeline to read from in image mode).
            d = getattr(self, "dock_color_timeline_clips", None)
            if d is not None:
                d.setVisible(False)
            # Video Batch Export is also video-only — hide in image mode.
            d = getattr(self, "dockVideoBatchExport", None)
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

        # Bottom Area Distribution
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self.dock_grading)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self.dock_denoise)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self.dock_deblur)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self.dock_curves)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self.dock_color_mapping)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self.dock_effects)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self.dock_grain)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self.dock_masks)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self.dock_qualifier)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self.dock_analysis)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self.dock_raw)

        # Tabify Bottom Tools
        self.tabifyDockWidget(self.dock_denoise, self.dock_deblur)
        self.tabifyDockWidget(self.dock_deblur, self.dock_grading)
        self.tabifyDockWidget(self.dock_grading, self.dock_curves)
        self.tabifyDockWidget(self.dock_curves, self.dock_color_mapping)
        self.tabifyDockWidget(self.dock_color_mapping, self.dock_effects)
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
        self._perform_update_all_titlebars()

        # Save default dock sizes to config
        self._save_all_docked_sizes()

    # ------------------------------------------------------------------
    # _get_all_page_layouts
    # ------------------------------------------------------------------
    def _get_all_page_layouts(self) -> dict:
        """获取所有页面的布局状态（保存当前页面状态后再收集）"""
        # 确保当前页面状态已保存
        self.page_layouts[self.current_page] = self.saveState()

        # 收集所有页面的布局状态（转换为hex字符串）
        panel_states = {}
        for page, state in self.page_layouts.items():
            if isinstance(state, QByteArray):
                panel_states[page] = state.toHex().data().decode()
        return panel_states

    # ------------------------------------------------------------------
    # _apply_all_page_layouts
    # ------------------------------------------------------------------
    def _apply_all_page_layouts(self, panel_states: dict) -> None:
        """应用所有页面的布局状态到内存（不立即恢复）"""
        if not panel_states:
            return

        # 恢复所有页面的布局状态到 page_layouts
        for page, state_hex in panel_states.items():
            self.page_layouts[page] = QByteArray.fromHex(state_hex.encode())

        # 立即应用当前页面的布局
        if self.current_page in self.page_layouts:
            self.restoreState(self.page_layouts[self.current_page])

    # ------------------------------------------------------------------
    # save_layout_dialog
    # ------------------------------------------------------------------
    def save_layout_dialog(self) -> None:
        name, ok = QInputDialog.getText(self, "Save Layout", "Layout Name:")
        if ok and name:
            geom = self.saveGeometry().toHex().data().decode()
            state = self.saveState().toHex().data().decode()
            panel_states = self._get_all_page_layouts()
            self.db.save_layout(name, geom, state, panel_states, work_mode=self._current_work_mode())

            # Also save dock sizes to config.json so _restore_docked_sizes uses correct sizes
            self._save_all_docked_sizes()

            self.status_bar.showMessage(f"Layout '{name}' saved.", 3000)
            self.update_layout_combobox()

    # ------------------------------------------------------------------
    # delete_layout_dialog
    # ------------------------------------------------------------------
    def delete_layout_dialog(self) -> None:
        layouts = self.db.list_layouts(work_mode=self._current_work_mode())
        if not layouts:
            QMessageBox.information(self, "Delete Layout", "No custom layouts to delete.")
            return

        name, ok = QInputDialog.getItem(self, "Delete Layout", "Select Layout to Delete:", layouts, 0, False)
        if ok and name:
            confirm = QMessageBox.question(self, "Confirm Delete", f"Are you sure you want to delete layout '{name}'?",
                                          QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if confirm == QMessageBox.StandardButton.Yes:
                self.db.delete_layout(name, work_mode=self._current_work_mode())
                self.status_bar.showMessage(f"Layout '{name}' deleted.", 3000)
                self.update_layout_combobox()

    # ------------------------------------------------------------------
    # update_layout_combobox
    # ------------------------------------------------------------------
    def update_layout_combobox(self) -> None:
        from src.features.edit.core.workspace_presets import (
            list_workspace_presets,
        )

        self.layout_combo.blockSignals(True)
        self.layout_combo.clear()
        self.layout_combo.addItem("Layout Presets")
        # P1-24：内建工作区预设排最前，itemData 携带 preset_id；
        # 用户自定义布局 itemData 为 None，on_layout_combo_activated 据此分流。
        if getattr(self, "_edit_available", False):
            for preset in list_workspace_presets():
                self.layout_combo.addItem(preset.label, preset.preset_id)
        layouts = self.db.list_layouts(work_mode=self._current_work_mode())
        if layouts:
            self.layout_combo.addItems(layouts)
        self.layout_combo.blockSignals(False)

    # ------------------------------------------------------------------
    # on_layout_combo_activated
    # ------------------------------------------------------------------
    def on_layout_combo_activated(self, index) -> None:
        if index > 0:
            preset_id = self.layout_combo.itemData(index)
            if isinstance(preset_id, str) and preset_id:
                self.apply_edit_workspace_preset(preset_id)
            else:
                name = self.layout_combo.itemText(index)
                self.apply_layout(name)
        self.layout_combo.setCurrentIndex(0)

    # ------------------------------------------------------------------
    # update_load_layout_menu
    # ------------------------------------------------------------------
    def update_load_layout_menu(self) -> None:
        self.load_layout_menu.clear()
        layouts = self.db.list_layouts(work_mode=self._current_work_mode())
        if not layouts:
            self.load_layout_menu.addAction(I18N.tr("dialog.no_saved_layouts", "No saved layouts")).setEnabled(False)
            return

        for name in layouts:
            action = QAction(name, self)
            action.triggered.connect(lambda checked, n=name: self.apply_layout(n))
            self.load_layout_menu.addAction(action)

    # ------------------------------------------------------------------
    # apply_layout
    # ------------------------------------------------------------------
    def apply_layout(self, name) -> None:
        geom, state, panel_states = self.db.load_layout(name, work_mode=self._current_work_mode())
        if geom and state:
            self.restoreGeometry(QByteArray.fromHex(geom.encode()))
            self._ensure_window_on_screen(self)
            self.restoreState(QByteArray.fromHex(state.encode()))
            # 恢复所有页面的布局状态
            if panel_states:
                self._apply_all_page_layouts(panel_states)
            # Restore docked dock sizes
            QTimer.singleShot(60, self._restore_docked_sizes)
            # Sync floating docks tracking after restore
            QTimer.singleShot(100, self._sync_floating_docks)
            # Update dock titlebars after layout restore
            QTimer.singleShot(50, self.update_all_dock_titlebars)
            # ``resizeDocks`` in _restore_docked_sizes can grow the window
            # past the screen for layouts saved on another monitor/DPI
            # setup; re-clamp once its 60ms timer has settled.
            # N16：DeferredDelete 冲刷可能先于延迟回调销毁宿主——回调内
            # 防御已删对象（与 apply_layout 路径的 _clamp_safely 同款）。
            def _clamp_after_restore():
                try:
                    self._ensure_window_on_screen(self)
                except RuntimeError:
                    pass
            QTimer.singleShot(300, _clamp_after_restore)
            self.status_bar.showMessage(f"Layout '{name}' loaded.", 3000)

    # ------------------------------------------------------------------
    # Edit workspace presets (P1-24)
    # ------------------------------------------------------------------
    def _workspace_dock_map(self) -> dict:
        """objectName → QDockWidget，仅覆盖 Edit 工作区受管 dock。

        缺失的 dock（图像模式没有 Media Pool、字幕工作区未构建等）直接
        不出现在映射里，预设应用对其静默跳过。
        """
        from src.features.edit.core.workspace_presets import (
            EDIT_WORKSPACE_MANAGED_DOCKS,
        )

        docks: dict = {}
        for name in EDIT_WORKSPACE_MANAGED_DOCKS:
            dock = self.findChild(QDockWidget, name)
            if dock is not None:
                docks[name] = dock
        return docks

    def apply_edit_workspace_preset(self, preset_id: str) -> bool:
        """P1-24：应用内建 Edit 工作区预设（Editing/Audio/Captions/Review）。

        流程：隐藏全部受管 dock → 按预设重摆区域 → tab 化 → 显示 →
        尺寸提示 → 快照进 page_layouts["Edit"] 与 LayoutStore。之后
        switch_to_page / 重启恢复走既有双系统路径，预设布局自然持久。

        Returns:
            True 表示已应用；False 表示预设不存在或 Edit 功能不可用。
        """
        from src.features.edit.core.workspace_presets import (
            get_workspace_preset,
        )

        if not getattr(self, "_edit_available", False):
            return False
        preset = get_workspace_preset(preset_id)
        if preset is None:
            return False
        if getattr(self, "current_page", "Edit") != "Edit":
            self.switch_to_page("Edit")

        dock_map = self._workspace_dock_map()

        # 1) 全部受管 dock 先隐藏，避免预设外的残留（用户手动开的面板、
        #    上一个预设的 dock）泄漏进快照。
        for dock in dock_map.values():
            dock.setVisible(False)

        # 2) 重摆区域。addDockWidget 会把 dock 从旧堆栈拆出，等价于
        #    “重置归位”，避免在用户当前布局的碎片上叠加 tab。
        area_enum = {
            "left": Qt.DockWidgetArea.LeftDockWidgetArea,
            "right": Qt.DockWidgetArea.RightDockWidgetArea,
            "top": Qt.DockWidgetArea.TopDockWidgetArea,
            "bottom": Qt.DockWidgetArea.BottomDockWidgetArea,
        }
        for name, area in preset.dock_areas:
            dock = dock_map.get(name)
            if dock is not None:
                self.addDockWidget(area_enum[area], dock)

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

        # 5) 尺寸提示（与出厂 Edit 布局的同步 resizeDocks 用法一致）。
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
                    f"工作区已切换：{preset.label} ({preset.label_en})",
                ),
                3000,
            )
        return True

    def update_workspace_menu(self) -> None:
        """Window > Workspaces 子菜单的 aboutToShow 刷新。"""
        from src.features.edit.core.workspace_presets import (
            list_workspace_presets,
        )

        self.workspace_menu.clear()
        if not getattr(self, "_edit_available", False):
            placeholder = self.workspace_menu.addAction(
                I18N.tr("edit.workspace.unavailable", "Editing Unavailable")
            )
            placeholder.setEnabled(False)
            return
        for preset in list_workspace_presets():
            action = QAction(
                I18N.tr(f"edit.workspace.{preset.preset_id}", preset.label),
                self,
            )
            action.setToolTip(preset.description)
            action.triggered.connect(
                lambda checked, pid=preset.preset_id: self.apply_edit_workspace_preset(pid)
            )
            self.workspace_menu.addAction(action)

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
        """Reset the layout to the factory default committed with the repo.

        Priority:
        1. ``default_layout.json`` at the project root (tracked by git, only
           changes through a commit — runtime user actions can never
           contaminate it)
        2. Hardcoded ``apply_default_layout()`` when the file is missing,
           corrupt or rejected by this build's ``restoreState``

        The legacy runtime default sources (DB ``layouts`` row and the
        ``.topos`` seed file) are deliberately NOT consulted: they were
        user-overwritable, which is exactly how the "default" could end up
        permanently messed up with no way back.
        """
        factory_geom, factory_state, panel_states = self._load_factory_layout()
        restored = False
        if factory_geom and factory_state:
            self.restoreGeometry(QByteArray.fromHex(factory_geom.encode()))
            self._ensure_window_on_screen(self)
            restored = self.restoreState(QByteArray.fromHex(factory_state.encode()))

        if restored:
            # 恢复所有页面的布局状态
            if panel_states:
                self._apply_all_page_layouts(panel_states)
            keyframes = getattr(self, "dock_color_keyframes", None)
            if keyframes is not None:
                keyframes.hide()
                self._color_keyframes_reseat = True
            # 出厂文件可能采集自另一种工作模式，修正越界的 dock 可见性
            self._enforce_page_dock_visibility()
            self._sync_floating_docks()
            self._perform_update_all_titlebars()
            if self.current_page == "Composite":
                self._migrate_composite_scopes_dock_visibility()
            if self.current_page == "Color":
                self._migrate_color_mapping_dock_layout(force=True)
            self.status_bar.showMessage(I18N.tr("status.layout_reset", "Layout reset to default."), 3000)
            return

        # Factory file missing or incompatible with this build — the
        # hardcoded programmatic default keeps the button working.
        logger.warning(
            "Factory layout unavailable or restoreState rejected it; "
            "falling back to hardcoded default layout"
        )
        self.apply_default_layout()
        self._enforce_page_dock_visibility()
        self._sync_floating_docks()
        self._perform_update_all_titlebars()
        if self.current_page == "Composite":
            self._migrate_composite_scopes_dock_visibility()
        if self.current_page == "Color":
            self._migrate_color_mapping_dock_layout(force=True)
        self.status_bar.showMessage(I18N.tr("status.layout_reset_hardcoded", "Layout reset to hardcoded default."), 3000)

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
        state = self.saveState().toHex().data().decode()
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
            modes[work_mode] = {
                "geometry": geom,
                "state": state,
                "panel_states": panel_states,
            }
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"schema": 1, "modes": modes}, f, indent=2)
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
                if self.dock_properties is not None and self.edit_properties_panel is not None:
                    self.dock_properties.setWidget(self.edit_properties_panel)
                self.restoreState(self.page_layouts[self.current_page])
            else:
                # No per-page layout saved — fall back to global window_state
                last_state = self.config_manager.get("window_state")
                if not last_state:
                    last_state = self.db.get_setting("last_state")
                if last_state:
                    self.restoreState(QByteArray.fromHex(last_state.encode()))

        # Sync floating docks and update titlebars
        self._sync_floating_docks()

        # Post-restore: enforce page-specific dock visibility.
        # Saved layouts from older versions (or from a time when composite was
        # on-screen during Color save) may have these docks visible incorrectly.
        self._enforce_page_dock_visibility()

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
            QTimer.singleShot(0, self._migrate_color_mapping_dock_layout)
            QTimer.singleShot(
                _EDIT_TIMELINE_DOCK_MIGRATION_DELAY_MS,
                self._migrate_color_grading_dock_height,
            )
        elif self.current_page == "Composite":
            QTimer.singleShot(0, self._migrate_composite_scopes_dock_visibility)

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

    # ------------------------------------------------------------------
    # _enforce_page_dock_visibility
    # ------------------------------------------------------------------
    def _enforce_page_dock_visibility(self) -> None:
        """Ensure page-specific docks are only visible on their own page.

        This corrects layouts that were saved with incorrect dock visibility,
        e.g. an old Color page layout that includes dock_composite as visible.

        Work-mode aware: in video mode the image-only Library, Batch Export,
        Deblur, Crop & Transform and Info docks are forced hidden; in image
        mode they stay usable. Media Pool is already unavailable in image mode
        (Edit feature disabled at startup).
        """
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
        # I01：归还后恢复 Edit 页模式，并把仲裁快照对齐回 Viewport 的真实
        # 显示（用户单/双与激活选择在借还期间不被覆盖）。
        coordinator = getattr(self, "_viewport_playback_coordinator", None)
        if coordinator is not None:
            coordinator.return_to_edit_page()

    def _sync_edit_viewport_timeline(self, timeline: object) -> None:
        """Keep the monitor's scrub range on the existing timeline timebase."""
        viewport = getattr(self, "edit_viewport", None)
        if viewport is None:
            return
        from src.features.edit.core.timeline_v2 import effective_timeline_duration_frames

        duration = effective_timeline_duration_frames(timeline) if timeline is not None else 0
        viewport.set_program_range(duration, getattr(timeline, "fps", 24.0))

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
        queue is now inside DeliverWorkspace, so enforce a usable minimum and
        reapply the intended right-side geometry after restoreState() settles.
        """
        dock = getattr(self, "dock_deliver", None)
        if dock is None:
            return
        dock.setMinimumWidth(1220)
        dock.setMinimumHeight(620)
        dock.setVisible(True)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, dock)
        self.resizeDocks([dock], [1320], Qt.Orientation.Horizontal)
        self.resizeDocks([dock], [680], Qt.Orientation.Vertical)
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
                if not isinstance(dock.titleBarWidget(), FloatingDockTitleBar):
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
