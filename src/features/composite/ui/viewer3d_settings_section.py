"""Viewer3DSettingsSection - default 3D Viewer scene settings."""

from __future__ import annotations

import math
from typing import Any, Optional

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QCheckBox, QFrame, QGroupBox, QHBoxLayout, QLabel, QPushButton, QToolButton,
    QVBoxLayout, QWidget,
)

from src.shared.assets.icon_manager import get_icon, IconColors
from src.shared.color import get_color
from src.shared.i18n.i18n import I18N
from src.shared.ui.style_sheet import (
    SETTINGS_SECTION_FRAME_STYLE,
    SETTINGS_SECTION_HEADER_STYLE,
    SETTINGS_SECTION_TITLE_STYLE,
    SETTINGS_SECTION_COLLAPSE_BTN_STYLE,
    TOOLBAR_FLAT_BTN_STYLE,
)
from src.shared.ui.widgets.chevron_combo import ChevronComboBox
from src.shared.ui.widgets.drag_value import DragValue


DEFAULT_VIEWER3D_SCENE_SETTINGS = {
    # ── 默认相机（无 Camera3D 连接时用这组内置轨道相机）──
    "cam_fov": 50.0,
    "cam_dist": 5.0,
    "cam_orbit_yaw": 30.0,
    "cam_orbit_pitch": 20.0,
    "cam_target_x": 0.0,
    "cam_target_y": 0.0,
    "cam_target_z": 0.0,
    # 活动相机：多个 Camera3D 连接时选哪个作为渲染视角（""=用第一个）。
    "active_camera_id": "",
    # ── 默认灯光（无 Light3D 连接时用这组内置方向光）──
    "light_dir_x": -0.6,
    "light_dir_y": 0.7,
    "light_dir_z": 0.4,
    "light_intensity": 2.0,
    "light_color_r": 1.0,
    "light_color_g": 1.0,
    "light_color_b": 1.0,
    # ── 预览背景与显示 ──
    "bg_r": 0.10980392156862745,  # #1C1C1C in display sRGB
    "bg_g": 0.10980392156862745,
    "bg_b": 0.10980392156862745,
    "bg_opaque": True,
    # ── 视口外框底色（letterbox/未填满区，paintGL clear color，
    #    与上面的 bg_* 渲染背景分离：bg_* 烘焙进画面，viewport_bg_* 是画面外的底色）──
    "viewport_bg_r": 0.15,
    "viewport_bg_g": 0.15,
    "viewport_bg_b": 0.15,
    "show_grid": True,
    "show_axes": True,
    # ── 地面网格视觉参数（show_grid=True 时生效，仅视口预览，不进合成输出）──
    "grid_cell_size": 0.35,   # 细格间距（世界单位）
    "grid_opacity": 0.25,     # 整体网格不透明度倍率（缩放 minor/major alpha，1.0=默认）
    "grid_line_width": 0.1,   # 线宽系数（<1 更细，>1 更粗）
    "grid_minor_color_r": 0.10,
    "grid_minor_color_g": 0.10,
    "grid_minor_color_b": 0.12,
    "grid_major_color_r": 0.20,
    "grid_major_color_g": 0.20,
    "grid_major_color_b": 0.23,
    "msaa": 4,
    # ── HDRI/IBL 环境光（无 Light3D-IBL 连接时用此内置环境）──
    # hdri_preset 语义："" → 无 IBL（纯黑占位 + 解析光源）；
    #                    "__sky__" → 程序天空（实验性）；
    #                    文件名/绝对路径 → 该 HDRI 贴图。
    "hdri_preset": "",
    "use_ibl": True,
    "env_intensity": 1.0,
    # ── 合成输出控制（与预览分离，控制输出到下游合成时的背景/网格）──
    "output_bg_opaque": False,
    "output_show_grid": False,
    # ── AOV 输出（beauty 始终存在；depth/object_id 默认保留现有行为）──
    "aov_depth": True,
    "aov_normal": False,
    "aov_position": False,
    "aov_uv": False,
    "aov_material_id": False,
    "aov_motion_vector": False,
    "aov_object_id": True,
    # ── 3D Viewer 最终渲染区域遮罩（仅视口辅助，不进合成输出）──
    "render_region_enabled": True,
    "render_region_mask_enabled": True,
    "render_region_mask_opacity": 0.25,
    "render_region_mask_r": 0.0,
    "render_region_mask_g": 0.0,
    "render_region_mask_b": 0.0,
    "render_region_border_enabled": True,
    "render_region_border_opacity": 0.1,
    "render_region_border_width": 1.0,
    "render_region_border_r": 1.0,
    "render_region_border_g": 1.0,
    "render_region_border_b": 1.0,
    # ── Milestone B/C：Navigation LOD + 四视图调度（§15.1 用户设置）──
    # interactive_lod：拖动视图时是否自动降质（MSAA/IBL/分辨率），默认开。
    # nav_restore_delay_ms：停止操作后恢复高质量的延迟（ms），默认 150。
    # nav_resolution_scale：导航中内部分辨率比例（仅诊断用，实际不缩放渲染目标）。
    # quad_view_quality：四视图策略——"all_textured"（默认，四视图都显示材质）
    # / "active_textured"（非活动视图导航时降频）
    # / "all_textured"（所有视图同质量）/ "all_solid"（全 solid，超大模型）。
    # inactive_view_skip_in_nav：导航时是否对非活动正交视图降频（保留上一帧）。
    # inactive_view_update_rate：非活动视图降频目标 FPS（默认 8，§15.1）。
    "interactive_lod": True,
    "nav_restore_delay_ms": 150,
    "nav_resolution_scale": 0.65,
    "quad_view_quality": "all_textured",
    "inactive_view_skip_in_nav": True,
    "inactive_view_update_rate": 8,
}


def _coerce_scene_setting(default: Any, value: Any) -> Any:
    """Validate one externally-loaded setting against its default (AUD-11201).

    ``default_3d_scene`` lives in the project file, so every value coming
    out of it is untrusted: a hand-edited / old / corrupted project may
    carry ``"cam_fov": None`` or ``"msaa": "4x"``. Each incoming value is
    coerced to the default's type; anything uncoercible (or non-finite)
    falls back to the default instead of raising inside ``_sync_widgets``.

    Strictness notes:
    - bool keys accept only real bools (``bool("x")`` would be True and a
      1/0 int is ambiguous with legacy data).
    - numeric keys accept int/float/numeric-string, then range-checked only
      for finiteness (widget ranges clamp at display time).
    - str keys accept only str.
    """
    if isinstance(default, bool):
        return value if isinstance(value, bool) else default
    if isinstance(default, int):
        if isinstance(value, bool):
            return default
        if isinstance(value, (int, float)):
            return int(value) if math.isfinite(float(value)) else default
        if isinstance(value, str):
            try:
                return int(float(value))
            except ValueError:
                return default
        return default
    if isinstance(default, float):
        if isinstance(value, bool):
            return default
        if isinstance(value, (int, float)):
            return float(value) if math.isfinite(float(value)) else default
        if isinstance(value, str):
            try:
                coerced = float(value)
            except ValueError:
                return default
            return coerced if math.isfinite(coerced) else default
        return default
    if isinstance(default, str):
        return value if isinstance(value, str) else default
    # No other default types exist in DEFAULT_VIEWER3D_SCENE_SETTINGS today;
    # pass through only exact type matches.
    return value if type(value) is type(default) else default


def merged_viewer3d_scene_settings(settings: Optional[dict] = None) -> dict:
    """Merge externally persisted scene settings over the defaults.

    AUD-11201: the incoming dict (from ``viewer_state["default_3d_scene"]``,
    i.e. the project file) is treated as untrusted — keys are whitelisted
    against ``DEFAULT_VIEWER3D_SCENE_SETTINGS`` and values are type-coerced
    (or reset to the default) so a malformed project can no longer crash
    ``load_from_graph`` → ``_sync_widgets`` with TypeError/ValueError.
    """
    merged = dict(DEFAULT_VIEWER3D_SCENE_SETTINGS)
    if isinstance(settings, dict):
        for key, value in settings.items():
            if key in merged:
                merged[key] = _coerce_scene_setting(merged[key], value)
        # Unknown keys are dropped (whitelist) — they are not consumable by
        # the settings widget nor by the Scene3D parameter contract.
    return merged


def _swatch_style(rgb: tuple[float, float, float]) -> str:
    r = max(0, min(255, int(round(float(rgb[0]) * 255.0))))
    g = max(0, min(255, int(round(float(rgb[1]) * 255.0))))
    b = max(0, min(255, int(round(float(rgb[2]) * 255.0))))
    return (
        "QPushButton {"
        f"background-color: rgb({r}, {g}, {b});"
        "border: 1px solid #555; border-radius: 3px;"
        "}"
        "QPushButton:hover { border-color: #e29227; }"
    )


class Viewer3DSettingsWidget(QWidget):
    """Editor for the virtual/global 3D Viewer scene defaults."""

    settings_changed = pyqtSignal(dict)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._block_emit = False
        # AUD-11200: combo row of the dynamically-inserted "custom HDRI"
        # item (None = no dynamic item present), so repeated syncs don't
        # accumulate duplicate rows.
        self._hdri_custom_index: Optional[int] = None
        self._settings = merged_viewer3d_scene_settings()
        self.setStyleSheet("background-color: transparent;")
        self._build_ui()

    def _label(self, text: str) -> QLabel:
        label = QLabel(text)
        label.setStyleSheet("color: #bbb; font-size: 10px;")
        return label

    def _drag(self, key: str, minimum: float, maximum: float,
              decimals: int = 2, width: int = 64) -> DragValue:
        drag = DragValue("", minimum, maximum, float(self._settings[key]), decimals=decimals)
        drag.setFixedValueWidth(width)
        drag.valueChanged.connect(lambda value, k=key: self._set_value(k, value))
        return drag

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        layout.addWidget(self._build_camera_group())
        layout.addWidget(self._build_light_group())
        layout.addWidget(self._build_environment_group())
        layout.addWidget(self._build_display_group())
        layout.addWidget(self._build_render_region_group())
        layout.addWidget(self._build_output_group())
        layout.addWidget(self._build_aov_group())
        layout.addWidget(self._build_performance_group())
        layout.addStretch(1)
        # 所有分组（含 environment/output）构建完毕后才同步控件状态——
        # _sync_widgets 要访问所有分组的控件，不能在某个分组内部提前调用。
        self._sync_widgets()

    def _build_camera_group(self) -> QGroupBox:
        group = QGroupBox(I18N.tr("composite.viewer3d_settings.camera", "Default Camera"))
        v = QVBoxLayout(group)
        v.setContentsMargins(8, 6, 8, 6)
        v.setSpacing(4)

        row = QWidget()
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 2, 0, 2)
        h.setSpacing(4)
        h.addWidget(self._label("FOV"))
        self._cam_fov = self._drag("cam_fov", 1.0, 170.0, decimals=1)
        h.addWidget(self._cam_fov)
        h.addWidget(self._label("Dist"))
        self._cam_dist = self._drag("cam_dist", 0.1, 1000.0, decimals=2)
        h.addWidget(self._cam_dist)
        h.addStretch(1)
        v.addWidget(row)

        row = QWidget()
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 2, 0, 2)
        h.setSpacing(4)
        h.addWidget(self._label("Yaw"))
        self._cam_yaw = self._drag("cam_orbit_yaw", -360.0, 360.0, decimals=1)
        h.addWidget(self._cam_yaw)
        h.addWidget(self._label("Pitch"))
        self._cam_pitch = self._drag("cam_orbit_pitch", -89.0, 89.0, decimals=1)
        h.addWidget(self._cam_pitch)
        h.addStretch(1)
        v.addWidget(row)

        row = QWidget()
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 2, 0, 2)
        h.setSpacing(4)
        h.addWidget(self._label("Target"))
        self._cam_tx = self._drag("cam_target_x", -1000.0, 1000.0, decimals=2)
        self._cam_ty = self._drag("cam_target_y", -1000.0, 1000.0, decimals=2)
        self._cam_tz = self._drag("cam_target_z", -1000.0, 1000.0, decimals=2)
        h.addWidget(self._cam_tx)
        h.addWidget(self._cam_ty)
        h.addWidget(self._cam_tz)
        h.addStretch(1)
        v.addWidget(row)
        return group

    def _build_light_group(self) -> QGroupBox:
        group = QGroupBox(I18N.tr("composite.viewer3d_settings.light", "Default Light"))
        v = QVBoxLayout(group)
        v.setContentsMargins(8, 6, 8, 6)
        v.setSpacing(4)

        row = QWidget()
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 2, 0, 2)
        h.setSpacing(4)
        h.addWidget(self._label("Direction"))
        self._light_dx = self._drag("light_dir_x", -1.0, 1.0, decimals=2)
        self._light_dy = self._drag("light_dir_y", -1.0, 1.0, decimals=2)
        self._light_dz = self._drag("light_dir_z", -1.0, 1.0, decimals=2)
        h.addWidget(self._light_dx)
        h.addWidget(self._light_dy)
        h.addWidget(self._light_dz)
        h.addStretch(1)
        v.addWidget(row)

        row = QWidget()
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 2, 0, 2)
        h.setSpacing(4)
        h.addWidget(self._label("Intensity"))
        self._light_intensity = self._drag("light_intensity", 0.0, 50.0, decimals=2)
        h.addWidget(self._light_intensity)
        h.addStretch(1)
        v.addWidget(row)

        # 默认灯光颜色（颜色拾取器，仿 Background swatch 模式）。
        row = QWidget()
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 2, 0, 2)
        h.setSpacing(6)
        h.addWidget(self._label(I18N.tr(
            "composite.viewer3d_settings.light_color", "Color")))
        self._light_swatch = QPushButton()
        self._light_swatch.setFixedSize(28, 22)
        self._light_swatch.clicked.connect(self._pick_light_color)
        h.addWidget(self._light_swatch)
        h.addStretch(1)
        v.addWidget(row)
        return group

    def _build_environment_group(self) -> QGroupBox:
        """HDRI/IBL 环境光设置（无 Light3D-IBL 连接时用此内置环境）。"""
        group = QGroupBox(I18N.tr(
            "composite.viewer3d_settings.environment", "Environment"))
        v = QVBoxLayout(group)
        v.setContentsMargins(8, 6, 8, 6)
        v.setSpacing(4)

        # HDRI Preset 下拉：None / 内置预设。
        row = QWidget()
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 2, 0, 2)
        h.setSpacing(6)
        h.addWidget(self._label(I18N.tr(
            "composite.viewer3d_settings.hdri_preset", "HDRI Preset")))
        self._hdri_preset = ChevronComboBox()
        # 选项：(显示文本, 实际值)。顺序固定以便稳定选中索引。
        for label_txt, val in (
                (I18N.tr("composite.viewer3d_settings.hdri_none", "None"), ""),
                ("Studio", "studio.hdr"),
                (I18N.tr("composite.viewer3d_settings.hdri_sky", "Sky"), "__sky__"),
        ):
            self._hdri_preset.addItem(label_txt, val)
        self._hdri_preset.currentIndexChanged.connect(
            lambda idx: self._set_value(
                "hdri_preset", str(self._hdri_preset.itemData(idx) or "")))
        h.addWidget(self._hdri_preset, 1)
        v.addWidget(row)

        # IBL 开关 + 强度。
        row = QWidget()
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 2, 0, 2)
        h.setSpacing(6)
        self._use_ibl = QCheckBox(I18N.tr(
            "composite.viewer3d_settings.use_ibl", "IBL"))
        self._use_ibl.toggled.connect(
            lambda checked: self._set_value("use_ibl", checked))
        h.addWidget(self._use_ibl)
        h.addWidget(self._label(I18N.tr(
            "composite.viewer3d_settings.env_intensity", "Intensity")))
        self._env_intensity = self._drag("env_intensity", 0.0, 10.0, decimals=2)
        h.addWidget(self._env_intensity)
        h.addStretch(1)
        v.addWidget(row)
        return group

    def _build_display_group(self) -> QGroupBox:
        group = QGroupBox(I18N.tr("composite.viewer3d_settings.display", "Display"))
        v = QVBoxLayout(group)
        v.setContentsMargins(8, 6, 8, 6)
        v.setSpacing(4)

        row = QWidget()
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 2, 0, 2)
        h.setSpacing(6)
        h.addWidget(self._label("Background"))
        self._bg_swatch = QPushButton()
        self._bg_swatch.setFixedSize(28, 22)
        self._bg_swatch.clicked.connect(self._pick_bg)
        h.addWidget(self._bg_swatch)
        self._bg_opaque = QCheckBox(I18N.tr("composite.viewer3d_settings.opaque", "Opaque"))
        self._bg_opaque.toggled.connect(lambda checked: self._set_value("bg_opaque", checked))
        h.addWidget(self._bg_opaque)
        h.addStretch(1)
        v.addWidget(row)

        # 视口外框底色（letterbox/未填满区，与上面的 Background 渲染背景分离）。
        row = QWidget()
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 2, 0, 2)
        h.setSpacing(6)
        h.addWidget(self._label(I18N.tr(
            "composite.viewer3d_settings.viewport_bg", "Viewport")))
        self._viewport_bg_swatch = QPushButton()
        self._viewport_bg_swatch.setFixedSize(28, 22)
        self._viewport_bg_swatch.clicked.connect(self._pick_viewport_bg)
        h.addWidget(self._viewport_bg_swatch)
        h.addStretch(1)
        v.addWidget(row)

        row = QWidget()
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 2, 0, 2)
        h.setSpacing(8)
        self._show_grid = QCheckBox(I18N.tr("composite.viewer3d_settings.grid", "Grid"))
        self._show_grid.toggled.connect(lambda checked: self._set_value("show_grid", checked))
        self._show_axes = QCheckBox(I18N.tr("composite.viewer3d_settings.axes", "Axes"))
        self._show_axes.toggled.connect(lambda checked: self._set_value("show_axes", checked))
        h.addWidget(self._show_grid)
        h.addWidget(self._show_axes)
        h.addStretch(1)
        v.addWidget(row)

        # 地面网格视觉参数（show_grid=True 时生效）。
        row = QWidget()
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 2, 0, 2)
        h.setSpacing(4)
        h.addWidget(self._label(I18N.tr(
            "composite.viewer3d_settings.grid_cell_size", "Cell Size")))
        self._grid_cell_size = self._drag("grid_cell_size", 0.1, 5.0, decimals=2)
        h.addWidget(self._grid_cell_size)
        h.addWidget(self._label(I18N.tr(
            "composite.viewer3d_settings.grid_opacity", "Opacity")))
        self._grid_opacity = self._drag("grid_opacity", 0.0, 2.0, decimals=2)
        h.addWidget(self._grid_opacity)
        h.addWidget(self._label(I18N.tr(
            "composite.viewer3d_settings.grid_line_width", "Line Width")))
        self._grid_line_width = self._drag("grid_line_width", 0.01, 2.0, decimals=2)
        h.addWidget(self._grid_line_width)
        h.addStretch(1)
        v.addWidget(row)

        row = QWidget()
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 2, 0, 2)
        h.setSpacing(6)
        h.addWidget(self._label(I18N.tr(
            "composite.viewer3d_settings.grid_colors", "Grid Color")))
        h.addWidget(self._label("Minor"))
        self._grid_minor_swatch = QPushButton()
        self._grid_minor_swatch.setFixedSize(28, 22)
        self._grid_minor_swatch.clicked.connect(self._pick_grid_minor_color)
        h.addWidget(self._grid_minor_swatch)
        h.addWidget(self._label("Major"))
        self._grid_major_swatch = QPushButton()
        self._grid_major_swatch.setFixedSize(28, 22)
        self._grid_major_swatch.clicked.connect(self._pick_grid_major_color)
        h.addWidget(self._grid_major_swatch)
        h.addStretch(1)
        v.addWidget(row)

        row = QWidget()
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 2, 0, 2)
        h.setSpacing(4)
        h.addWidget(self._label("MSAA"))
        self._msaa = ChevronComboBox()
        for label, value in (("Off", 0), ("2x", 2), ("4x", 4), ("8x", 8)):
            self._msaa.addItem(label, value)
        self._msaa.currentIndexChanged.connect(
            lambda idx: self._set_value("msaa", int(self._msaa.itemData(idx) or 0)))
        h.addWidget(self._msaa, 1)
        v.addWidget(row)
        return group

    def _build_render_region_group(self) -> QGroupBox:
        group = QGroupBox(I18N.tr(
            "composite.viewer3d_settings.render_region", "Render Region"))
        v = QVBoxLayout(group)
        v.setContentsMargins(8, 6, 8, 6)
        v.setSpacing(4)

        row = QWidget()
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 2, 0, 2)
        h.setSpacing(8)
        self._render_region_enabled = QCheckBox(I18N.tr("composite.viewer3d_settings.region", "Region"))
        self._render_region_enabled.toggled.connect(
            lambda checked: self._set_value("render_region_enabled", checked))
        self._render_region_mask_enabled = QCheckBox(I18N.tr("composite.viewer3d_settings.mask", "Mask"))
        self._render_region_mask_enabled.toggled.connect(
            lambda checked: self._set_value("render_region_mask_enabled", checked))
        self._render_region_border_enabled = QCheckBox(I18N.tr("composite.viewer3d_settings.border", "Border"))
        self._render_region_border_enabled.toggled.connect(
            lambda checked: self._set_value("render_region_border_enabled", checked))
        h.addWidget(self._render_region_enabled)
        h.addWidget(self._render_region_mask_enabled)
        h.addWidget(self._render_region_border_enabled)
        h.addStretch(1)
        v.addWidget(row)

        row = QWidget()
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 2, 0, 2)
        h.setSpacing(6)
        h.addWidget(self._label("Mask"))
        self._render_region_mask_swatch = QPushButton()
        self._render_region_mask_swatch.setFixedSize(28, 22)
        self._render_region_mask_swatch.clicked.connect(
            self._pick_render_region_mask_color)
        h.addWidget(self._render_region_mask_swatch)
        h.addWidget(self._label("Opacity"))
        self._render_region_mask_opacity = self._drag(
            "render_region_mask_opacity", 0.0, 1.0, decimals=2)
        h.addWidget(self._render_region_mask_opacity)
        h.addStretch(1)
        v.addWidget(row)

        row = QWidget()
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 2, 0, 2)
        h.setSpacing(6)
        h.addWidget(self._label("Border"))
        self._render_region_border_swatch = QPushButton()
        self._render_region_border_swatch.setFixedSize(28, 22)
        self._render_region_border_swatch.clicked.connect(
            self._pick_render_region_border_color)
        h.addWidget(self._render_region_border_swatch)
        h.addWidget(self._label("Opacity"))
        self._render_region_border_opacity = self._drag(
            "render_region_border_opacity", 0.0, 1.0, decimals=2)
        h.addWidget(self._render_region_border_opacity)
        h.addWidget(self._label("Width"))
        self._render_region_border_width = self._drag(
            "render_region_border_width", 0.25, 8.0, decimals=2)
        h.addWidget(self._render_region_border_width)
        h.addStretch(1)
        v.addWidget(row)
        return group

    def _build_output_group(self) -> QGroupBox:
        """合成输出控制（与预览分离，控制输出到下游合成时的背景/网格）。

        Scene3D 输出到下游合成节点（Merge/Transform/Output）时是否带背景/网格。
        默认干净透明（output_bg_opaque=False / output_show_grid=False）。
        """
        group = QGroupBox(I18N.tr(
            "composite.viewer3d_settings.output", "Output"))
        v = QVBoxLayout(group)
        v.setContentsMargins(8, 6, 8, 6)
        v.setSpacing(4)

        row = QWidget()
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 2, 0, 2)
        h.setSpacing(8)
        self._output_bg_opaque = QCheckBox(I18N.tr(
            "composite.viewer3d_settings.output_bg_opaque", "Background Opaque"))
        self._output_bg_opaque.toggled.connect(
            lambda checked: self._set_value("output_bg_opaque", checked))
        self._output_show_grid = QCheckBox(I18N.tr(
            "composite.viewer3d_settings.output_show_grid", "Grid"))
        self._output_show_grid.toggled.connect(
            lambda checked: self._set_value("output_show_grid", checked))
        h.addWidget(self._output_bg_opaque)
        h.addWidget(self._output_show_grid)
        h.addStretch(1)
        v.addWidget(row)
        return group

    def _build_aov_group(self) -> QGroupBox:
        """几何 AOV 开关：只生成用户明确需要的辅助 pass。"""
        group = QGroupBox(I18N.tr(
            "composite.viewer3d_settings.aov", "Render Passes / AOV"))
        v = QVBoxLayout(group)
        v.setContentsMargins(8, 6, 8, 6)
        v.setSpacing(4)

        row = QWidget()
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 2, 0, 2)
        h.setSpacing(8)
        self._aov_depth = QCheckBox(I18N.tr("composite.viewer3d_settings.aov_depth", "Depth"))
        self._aov_normal = QCheckBox(I18N.tr("composite.viewer3d_settings.aov_normal", "Normal"))
        self._aov_position = QCheckBox(I18N.tr("composite.viewer3d_settings.aov_position", "Position"))
        self._aov_uv = QCheckBox(I18N.tr("composite.viewer3d_settings.aov_uv", "UV"))
        self._aov_material_id = QCheckBox(I18N.tr("composite.viewer3d_settings.aov_material_id", "Material ID"))
        self._aov_motion_vector = QCheckBox(I18N.tr("composite.viewer3d_settings.aov_motion", "Motion"))
        self._aov_object_id = QCheckBox(I18N.tr("composite.viewer3d_settings.aov_object_id", "Object ID"))
        for key, checkbox in (
                ("aov_depth", self._aov_depth),
                ("aov_normal", self._aov_normal),
                ("aov_position", self._aov_position),
                ("aov_uv", self._aov_uv),
                ("aov_material_id", self._aov_material_id),
                ("aov_motion_vector", self._aov_motion_vector),
                ("aov_object_id", self._aov_object_id)):
            checkbox.toggled.connect(
                lambda checked, k=key: self._set_value(k, checked))
            h.addWidget(checkbox)
        h.addStretch(1)
        v.addWidget(row)
        return group

    def _build_performance_group(self) -> QGroupBox:
        """Milestone B/C：Navigation LOD + 四视图调度（§15.1 用户设置）。

        交互降载开关、恢复延迟、四视图质量策略、非活动视图降频开关。
        这些是性能偏好，不影响合成输出质量（合成输出始终 FINAL profile）。
        """
        group = QGroupBox(I18N.tr(
            "composite.viewer3d_settings.performance", "Performance / LOD"))
        v = QVBoxLayout(group)
        v.setContentsMargins(8, 6, 8, 6)
        v.setSpacing(4)

        # Interactive LOD 开关
        row = QWidget()
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 2, 0, 2)
        h.setSpacing(8)
        self._interactive_lod = QCheckBox(I18N.tr(
            "composite.viewer3d_settings.interactive_lod", "Interactive LOD"))
        self._interactive_lod.setToolTip(
            I18N.tr(
                "composite.viewer3d_settings.interactive_lod_tooltip",
                "拖动视图时自动降质（关 MSAA/IBL）保流畅；停止后恢复高质量"))
        self._interactive_lod.toggled.connect(
            lambda checked: self._set_value("interactive_lod", checked))
        h.addWidget(self._interactive_lod)
        h.addStretch(1)
        v.addWidget(row)

        # Nav restore delay
        row = QWidget()
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 2, 0, 2)
        h.setSpacing(4)
        h.addWidget(self._label("Restore Delay"))
        self._nav_restore_delay = self._drag("nav_restore_delay_ms", 0, 1000, decimals=0)
        h.addWidget(self._nav_restore_delay)
        h.addWidget(self._label("ms"))
        h.addStretch(1)
        v.addWidget(row)

        # Quad view quality 策略
        row = QWidget()
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 2, 0, 2)
        h.setSpacing(4)
        h.addWidget(self._label("Quad View Quality"))
        self._quad_view_quality = ChevronComboBox()
        for qlabel, qval in (("Active Textured", "active_textured"),
                             ("All Textured", "all_textured"),
                             ("All Solid", "all_solid")):
            self._quad_view_quality.addItem(qlabel, qval)
        self._quad_view_quality.currentIndexChanged.connect(
            lambda idx: self._set_value(
                "quad_view_quality", str(self._quad_view_quality.itemData(idx) or "all_textured")))
        h.addWidget(self._quad_view_quality, 1)
        v.addWidget(row)

        # Inactive view skip in nav + update rate
        row = QWidget()
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 2, 0, 2)
        h.setSpacing(8)
        self._inactive_view_skip = QCheckBox(I18N.tr(
            "composite.viewer3d_settings.inactive_view_skip",
            "Throttle Inactive Views in Nav"))
        self._inactive_view_skip.setToolTip(
            I18N.tr(
                "composite.viewer3d_settings.inactive_view_skip_tooltip",
                "导航时对非活动正交视图降频（按下方 FPS 刷新，保留上一帧），保活动视图流畅"))
        self._inactive_view_skip.toggled.connect(
            lambda checked: self._set_value("inactive_view_skip_in_nav", checked))
        h.addWidget(self._inactive_view_skip)
        h.addStretch(1)
        v.addWidget(row)

        # Inactive view update rate (FPS)
        row = QWidget()
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 2, 0, 2)
        h.setSpacing(4)
        h.addWidget(self._label("Inactive View Rate"))
        self._inactive_view_rate = self._drag("inactive_view_update_rate", 1, 30, decimals=0)
        h.addWidget(self._inactive_view_rate)
        h.addWidget(self._label("FPS"))
        h.addStretch(1)
        v.addWidget(row)
        return group

    def load_from_graph(self, node_graph: Any) -> None:
        viewer_state = getattr(node_graph, "_viewer_state", {}) or {}
        self._settings = merged_viewer3d_scene_settings(
            viewer_state.get("default_3d_scene", {}))
        self._sync_widgets()

    def reset_to_defaults(self) -> None:
        """Reset all scene settings to DEFAULT_VIEWER3D_SCENE_SETTINGS.

        _sync_widgets 用 _block_emit 抑制控件 setValue 触发的递归 emit，
        所以重置后必须显式 emit 一次 settings_changed，否则只改 UI 不回写
        viewer_state（page 的 _apply_viewer3d_settings 收不到信号）。
        """
        self._settings = dict(DEFAULT_VIEWER3D_SCENE_SETTINGS)
        self._sync_widgets()
        self.settings_changed.emit(dict(self._settings))

    def _sync_widgets(self) -> None:
        self._block_emit = True
        try:
            mapping = {
                "cam_fov": self._cam_fov,
                "cam_dist": self._cam_dist,
                "cam_orbit_yaw": self._cam_yaw,
                "cam_orbit_pitch": self._cam_pitch,
                "cam_target_x": self._cam_tx,
                "cam_target_y": self._cam_ty,
                "cam_target_z": self._cam_tz,
                "light_dir_x": self._light_dx,
                "light_dir_y": self._light_dy,
                "light_dir_z": self._light_dz,
                "light_intensity": self._light_intensity,
                "env_intensity": self._env_intensity,
                "grid_cell_size": self._grid_cell_size,
                "grid_opacity": self._grid_opacity,
                "grid_line_width": self._grid_line_width,
                "render_region_mask_opacity": self._render_region_mask_opacity,
                "render_region_border_opacity": self._render_region_border_opacity,
                "render_region_border_width": self._render_region_border_width,
            }
            for key, widget in mapping.items():
                widget.setValue(float(self._settings[key]))
            self._bg_opaque.setChecked(bool(self._settings["bg_opaque"]))
            self._show_grid.setChecked(bool(self._settings["show_grid"]))
            self._show_axes.setChecked(bool(self._settings["show_axes"]))
            self._use_ibl.setChecked(bool(self._settings["use_ibl"]))
            self._output_bg_opaque.setChecked(bool(self._settings["output_bg_opaque"]))
            self._output_show_grid.setChecked(bool(self._settings["output_show_grid"]))
            self._aov_depth.setChecked(bool(self._settings.get("aov_depth", True)))
            self._aov_normal.setChecked(bool(self._settings.get("aov_normal", False)))
            self._aov_position.setChecked(bool(self._settings.get("aov_position", False)))
            self._aov_uv.setChecked(bool(self._settings.get("aov_uv", False)))
            self._aov_material_id.setChecked(bool(
                self._settings.get("aov_material_id", False)))
            self._aov_motion_vector.setChecked(bool(
                self._settings.get("aov_motion_vector", False)))
            self._aov_object_id.setChecked(bool(
                self._settings.get("aov_object_id", True)))
            self._render_region_enabled.setChecked(bool(
                self._settings["render_region_enabled"]))
            self._render_region_mask_enabled.setChecked(bool(
                self._settings["render_region_mask_enabled"]))
            self._render_region_border_enabled.setChecked(bool(
                self._settings["render_region_border_enabled"]))
            # 背景色 swatch
            rgb = (
                float(self._settings["bg_r"]),
                float(self._settings["bg_g"]),
                float(self._settings["bg_b"]),
            )
            self._bg_swatch.setStyleSheet(_swatch_style(rgb))
            # 视口外框底色 swatch
            vrgb = (
                float(self._settings["viewport_bg_r"]),
                float(self._settings["viewport_bg_g"]),
                float(self._settings["viewport_bg_b"]),
            )
            self._viewport_bg_swatch.setStyleSheet(_swatch_style(vrgb))
            # 灯光颜色 swatch
            lrgb = (
                float(self._settings["light_color_r"]),
                float(self._settings["light_color_g"]),
                float(self._settings["light_color_b"]),
            )
            self._light_swatch.setStyleSheet(_swatch_style(lrgb))
            minor_rgb = (
                float(self._settings["grid_minor_color_r"]),
                float(self._settings["grid_minor_color_g"]),
                float(self._settings["grid_minor_color_b"]),
            )
            self._grid_minor_swatch.setStyleSheet(_swatch_style(minor_rgb))
            major_rgb = (
                float(self._settings["grid_major_color_r"]),
                float(self._settings["grid_major_color_g"]),
                float(self._settings["grid_major_color_b"]),
            )
            self._grid_major_swatch.setStyleSheet(_swatch_style(major_rgb))
            mask_rgb = (
                float(self._settings["render_region_mask_r"]),
                float(self._settings["render_region_mask_g"]),
                float(self._settings["render_region_mask_b"]),
            )
            self._render_region_mask_swatch.setStyleSheet(
                _swatch_style(mask_rgb))
            border_rgb = (
                float(self._settings["render_region_border_r"]),
                float(self._settings["render_region_border_g"]),
                float(self._settings["render_region_border_b"]),
            )
            self._render_region_border_swatch.setStyleSheet(
                _swatch_style(border_rgb))
            # MSAA 下拉
            # AUD-11200: values outside the supported set (e.g. msaa=16 from
            # an exotic project) previously left the combo showing a stale
            # item; fall back to the default (4x) instead.
            msaa_value = int(self._settings["msaa"])
            for i in range(self._msaa.count()):
                if int(self._msaa.itemData(i) or 0) == msaa_value:
                    self._msaa.setCurrentIndex(i)
                    break
            else:
                for i in range(self._msaa.count()):
                    if int(self._msaa.itemData(i) or 0) == int(
                            DEFAULT_VIEWER3D_SCENE_SETTINGS["msaa"]):
                        self._msaa.setCurrentIndex(i)
                        break
            # HDRI Preset 下拉
            # AUD-11200: hdri_preset accepts arbitrary file paths (see the
            # key's doc above), but the combo only lists the built-ins. A
            # custom path previously left the combo on a stale item (the
            # "None" row) while the scene actually rendered the custom HDRI;
            # insert a dynamic row showing the real value instead.
            hdri_value = str(self._settings["hdri_preset"])
            if self._hdri_custom_index is not None:
                self._hdri_preset.removeItem(self._hdri_custom_index)
                self._hdri_custom_index = None
            for i in range(self._hdri_preset.count()):
                if str(self._hdri_preset.itemData(i) or "") == hdri_value:
                    self._hdri_preset.setCurrentIndex(i)
                    break
            else:
                self._hdri_preset.addItem(hdri_value, hdri_value)
                self._hdri_custom_index = self._hdri_preset.count() - 1
                self._hdri_preset.setCurrentIndex(self._hdri_custom_index)
            # Milestone B/C：Performance/LOD 控件同步
            self._interactive_lod.setChecked(bool(self._settings.get("interactive_lod", True)))
            self._nav_restore_delay.setValue(float(self._settings.get("nav_restore_delay_ms", 150)))
            self._inactive_view_skip.setChecked(bool(
                self._settings.get("inactive_view_skip_in_nav", True)))
            self._inactive_view_rate.setValue(float(
                self._settings.get("inactive_view_update_rate", 8)))
            qq = str(self._settings.get("quad_view_quality", "all_textured"))
            for i in range(self._quad_view_quality.count()):
                if str(self._quad_view_quality.itemData(i) or "") == qq:
                    self._quad_view_quality.setCurrentIndex(i)
                    break
            else:
                # AUD-11200: unknown strategy → fall back to the default
                # instead of leaving a stale item selected.
                for i in range(self._quad_view_quality.count()):
                    if str(self._quad_view_quality.itemData(i) or "") == \
                            str(DEFAULT_VIEWER3D_SCENE_SETTINGS["quad_view_quality"]):
                        self._quad_view_quality.setCurrentIndex(i)
                        break
        finally:
            self._block_emit = False

    def _set_value(self, key: str, value) -> None:
        if self._block_emit:
            return
        self._settings[key] = value
        self.settings_changed.emit(dict(self._settings))

    def _pick_bg(self) -> None:
        initial = QColor.fromRgbF(
            float(self._settings["bg_r"]),
            float(self._settings["bg_g"]),
            float(self._settings["bg_b"]),
            1.0,
        )
        result = get_color(parent=self, initial=initial)
        if result is None:
            return
        self._settings["bg_r"] = float(result.redF())
        self._settings["bg_g"] = float(result.greenF())
        self._settings["bg_b"] = float(result.blueF())
        self._bg_swatch.setStyleSheet(_swatch_style((
            self._settings["bg_r"],
            self._settings["bg_g"],
            self._settings["bg_b"],
        )))
        self.settings_changed.emit(dict(self._settings))

    def _pick_viewport_bg(self) -> None:
        initial = QColor.fromRgbF(
            float(self._settings["viewport_bg_r"]),
            float(self._settings["viewport_bg_g"]),
            float(self._settings["viewport_bg_b"]),
            1.0,
        )
        result = get_color(parent=self, initial=initial)
        if result is None:
            return
        self._settings["viewport_bg_r"] = float(result.redF())
        self._settings["viewport_bg_g"] = float(result.greenF())
        self._settings["viewport_bg_b"] = float(result.blueF())
        self._viewport_bg_swatch.setStyleSheet(_swatch_style((
            self._settings["viewport_bg_r"],
            self._settings["viewport_bg_g"],
            self._settings["viewport_bg_b"],
        )))
        self.settings_changed.emit(dict(self._settings))

    def _pick_light_color(self) -> None:
        initial = QColor.fromRgbF(
            float(self._settings["light_color_r"]),
            float(self._settings["light_color_g"]),
            float(self._settings["light_color_b"]),
            1.0,
        )
        result = get_color(parent=self, initial=initial)
        if result is None:
            return
        self._settings["light_color_r"] = float(result.redF())
        self._settings["light_color_g"] = float(result.greenF())
        self._settings["light_color_b"] = float(result.blueF())
        self._light_swatch.setStyleSheet(_swatch_style((
            self._settings["light_color_r"],
            self._settings["light_color_g"],
            self._settings["light_color_b"],
        )))
        self.settings_changed.emit(dict(self._settings))

    def _pick_grid_minor_color(self) -> None:
        initial = QColor.fromRgbF(
            float(self._settings["grid_minor_color_r"]),
            float(self._settings["grid_minor_color_g"]),
            float(self._settings["grid_minor_color_b"]),
            1.0,
        )
        result = get_color(parent=self, initial=initial)
        if result is None:
            return
        self._settings["grid_minor_color_r"] = float(result.redF())
        self._settings["grid_minor_color_g"] = float(result.greenF())
        self._settings["grid_minor_color_b"] = float(result.blueF())
        self._grid_minor_swatch.setStyleSheet(_swatch_style((
            self._settings["grid_minor_color_r"],
            self._settings["grid_minor_color_g"],
            self._settings["grid_minor_color_b"],
        )))
        self.settings_changed.emit(dict(self._settings))

    def _pick_grid_major_color(self) -> None:
        initial = QColor.fromRgbF(
            float(self._settings["grid_major_color_r"]),
            float(self._settings["grid_major_color_g"]),
            float(self._settings["grid_major_color_b"]),
            1.0,
        )
        result = get_color(parent=self, initial=initial)
        if result is None:
            return
        self._settings["grid_major_color_r"] = float(result.redF())
        self._settings["grid_major_color_g"] = float(result.greenF())
        self._settings["grid_major_color_b"] = float(result.blueF())
        self._grid_major_swatch.setStyleSheet(_swatch_style((
            self._settings["grid_major_color_r"],
            self._settings["grid_major_color_g"],
            self._settings["grid_major_color_b"],
        )))
        self.settings_changed.emit(dict(self._settings))

    def _pick_render_region_mask_color(self) -> None:
        initial = QColor.fromRgbF(
            float(self._settings["render_region_mask_r"]),
            float(self._settings["render_region_mask_g"]),
            float(self._settings["render_region_mask_b"]),
            1.0,
        )
        result = get_color(parent=self, initial=initial)
        if result is None:
            return
        self._settings["render_region_mask_r"] = float(result.redF())
        self._settings["render_region_mask_g"] = float(result.greenF())
        self._settings["render_region_mask_b"] = float(result.blueF())
        self._render_region_mask_swatch.setStyleSheet(_swatch_style((
            self._settings["render_region_mask_r"],
            self._settings["render_region_mask_g"],
            self._settings["render_region_mask_b"],
        )))
        self.settings_changed.emit(dict(self._settings))

    def _pick_render_region_border_color(self) -> None:
        initial = QColor.fromRgbF(
            float(self._settings["render_region_border_r"]),
            float(self._settings["render_region_border_g"]),
            float(self._settings["render_region_border_b"]),
            1.0,
        )
        result = get_color(parent=self, initial=initial)
        if result is None:
            return
        self._settings["render_region_border_r"] = float(result.redF())
        self._settings["render_region_border_g"] = float(result.greenF())
        self._settings["render_region_border_b"] = float(result.blueF())
        self._render_region_border_swatch.setStyleSheet(_swatch_style((
            self._settings["render_region_border_r"],
            self._settings["render_region_border_g"],
            self._settings["render_region_border_b"],
        )))
        self.settings_changed.emit(dict(self._settings))


class Viewer3DSettingsSection(QFrame):
    """Collapsible section for default 3D Viewer scene settings."""

    pin_toggled = pyqtSignal(bool)
    settings_changed = pyqtSignal(dict)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._is_collapsed = False
        self._is_pinned = False
        # 外框样式：与 NodeSectionWidget 保持一致（1px 边框 + 4px 圆角 + 面板背景）。
        # NODE_SECTION_STYLE 选择器限定在 NodeSectionWidget，对本类不生效，故用
        # 不限定选择器的内联样式显式呈现相同外框。
        self.setStyleSheet(SETTINGS_SECTION_FRAME_STYLE)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self._build_header())
        self._content_widget = QWidget()
        self._content_widget.setStyleSheet("background-color: #27272b; border: none;")
        content_layout = QVBoxLayout(self._content_widget)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(0)
        self._settings_widget = Viewer3DSettingsWidget()
        self._settings_widget.settings_changed.connect(self.settings_changed.emit)
        content_layout.addWidget(self._settings_widget)
        layout.addWidget(self._content_widget)

    def _build_header(self) -> QWidget:
        header = QWidget()
        header.setFixedHeight(28)
        header.setStyleSheet(SETTINGS_SECTION_HEADER_STYLE)
        layout = QHBoxLayout(header)
        layout.setContentsMargins(6, 0, 2, 0)
        layout.setSpacing(2)

        self._collapse_btn = QToolButton()
        self._collapse_btn.setText("▼")
        self._collapse_btn.setFixedSize(16, 16)
        self._collapse_btn.setStyleSheet(SETTINGS_SECTION_COLLAPSE_BTN_STYLE)
        self._collapse_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._collapse_btn.clicked.connect(self.toggle_collapse)
        layout.addWidget(self._collapse_btn)

        title = QLabel(I18N.tr("composite.viewer3d_settings.title", "3D Scene Settings"))
        title.setStyleSheet(SETTINGS_SECTION_TITLE_STYLE)
        layout.addWidget(title)
        layout.addStretch()

        # 重置全部场景参数到默认值（emit settings_changed 让 page 回写 viewer_state）。
        self._reset_btn = QToolButton()
        self._reset_btn.setIcon(get_icon("fa5s.undo", IconColors.WHITE))
        self._reset_btn.setFixedSize(24, 24)
        self._reset_btn.setStyleSheet(TOOLBAR_FLAT_BTN_STYLE)
        self._reset_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._reset_btn.setToolTip(I18N.tr(
            "composite.viewer3d_settings.reset_tooltip", "Reset to defaults"))
        self._reset_btn.clicked.connect(self._on_reset)
        layout.addWidget(self._reset_btn)

        self._pin_btn = QToolButton()
        self._pin_btn.setIcon(get_icon("fa5s.thumbtack", IconColors.WHITE))
        self._pin_btn.setFixedSize(24, 24)
        self._pin_btn.setCheckable(True)
        self._pin_btn.setStyleSheet(TOOLBAR_FLAT_BTN_STYLE)
        self._pin_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._pin_btn.clicked.connect(self._on_pin_toggled)
        layout.addWidget(self._pin_btn)
        return header

    def toggle_collapse(self) -> None:
        self._is_collapsed = not self._is_collapsed
        self._content_widget.setVisible(not self._is_collapsed)
        self._collapse_btn.setText("▶" if self._is_collapsed else "▼")

    def collapse(self) -> None:
        """折叠 section（幂等：已折叠则 no-op）。

        选中其他节点时由 NodePropertiesPanel.set_nodes 调用，避免占用过多空间。
        """
        if not self._is_collapsed:
            self.toggle_collapse()

    def _on_pin_toggled(self) -> None:
        self._is_pinned = self._pin_btn.isChecked()
        color = IconColors.ORANGE if self._is_pinned else IconColors.WHITE
        self._pin_btn.setIcon(get_icon("fa5s.thumbtack", color))
        self.pin_toggled.emit(self._is_pinned)

    def _on_reset(self) -> None:
        """Reset all 3D scene settings to defaults and propagate to the page."""
        self._settings_widget.reset_to_defaults()

    def load_from_graph(self, node_graph: Any) -> None:
        self._settings_widget.load_from_graph(node_graph)
