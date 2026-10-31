"""ProjectSettingsWidget - 合成项目设置嵌入式视图。

嵌入 Composite 页参数面板(NodePropertiesPanel),按 S 快捷键切换显示。
字段与 CompClipSettingsDialog 一致(分辨率/帧率/时长/背景色),风格统一,
但作为嵌入式 widget:改参数实时 emit settings_changed,由 CompositePage
写回 output 节点(分辨率/fps)和 constant 节点(背景色)。
"""

from __future__ import annotations

from typing import Any, Optional

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QSizePolicy,
)

from src.shared.i18n.i18n import I18N
from src.shared.assets.icon_manager import make_link_button
from src.shared.ui.widgets.drag_value import DragValue
from src.shared.ui.widgets.chevron_combo import ChevronComboBox
from src.shared.ui.widgets.color_picker_control import ColorPickerControl
from src.features.edit.ui.comp_clip_settings_dialog import (
    _RESOLUTION_PRESETS,
    _FPS_PRESETS,
    _CUSTOM_KEY,
    _DEFAULT_BACKGROUND_COLOR,
    _qcolor_to_rgba,
)
from src.features.edit.core.composite_graph_data import (
    PROJECT_BACKGROUND_PARAM,
    CONSTANT_COLOR_SPACE_PARAM,
)
from src.features.composite.nodes.constant_node import ConstantNode


class ProjectSettingsWidget(QWidget):
    """嵌入式项目设置视图。

    Signals:
        settings_changed(dict): 任一参数变化时发射,含
            width/height/fps/duration_seconds/background_color。
    """

    settings_changed = pyqtSignal(dict)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._block_emit = False
        self._aspect_locked = False
        self._aspect_ratio = 16.0 / 9.0
        self._bg_color_value = _DEFAULT_BACKGROUND_COLOR

        self.setStyleSheet("background-color: transparent;")
        self._build_ui()

    # ------------------------------------------------------------------
    # UI 构建
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        # 去掉 QGroupBox 分类框，所有参数融合成统一的紧凑行（与节点参数行
        # 同风格：左侧固定宽度标签 + 右侧控件，行间距 4px）。外层边距与
        # NodeSectionWidget 内容区（8,6,8,6）对齐，视觉上无缝融入参数面板。
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(4)

        layout.addWidget(self._build_preset_row())
        layout.addWidget(self._build_size_row())
        layout.addWidget(self._build_fps_row())
        layout.addWidget(self._build_duration_row())
        layout.addWidget(self._build_background_row())
        layout.addStretch(1)

    # ------------------------------------------------------------------
    # 行容器 / 标签工厂（与 NodeSectionWidget 参数行规范一致）
    # ------------------------------------------------------------------

    # 标签固定宽度：让所有行左侧对齐，右侧控件左边缘齐整。
    _LABEL_MIN_WIDTH = 80

    def _make_label(self, text: str) -> QLabel:
        """参数行标签：10px / #bbb / 固定最小宽度 + Maximum 尺寸策略，
        与 NodeSectionWidget._make_param_label 规范一致。"""
        lbl = QLabel(text)
        lbl.setStyleSheet("color: #bbb; font-size: 10px;")
        lbl.setMinimumWidth(self._LABEL_MIN_WIDTH)
        lbl.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Preferred)
        return lbl

    def _make_row(self) -> tuple[QWidget, QHBoxLayout]:
        """参数行容器：水平 0 / 垂直 2px 边距，间距 4px（同节点参数行）。"""
        row = QWidget()
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 2, 0, 2)
        h.setSpacing(4)
        return row, h

    # ------------------------------------------------------------------
    # 各参数行
    # ------------------------------------------------------------------

    def _build_preset_row(self) -> QWidget:
        row, h = self._make_row()
        h.addWidget(self._make_label(
            I18N.tr("composite.project_settings.preset", "Preset")))
        self._resolution_combo = ChevronComboBox()
        for name, w, hh in _RESOLUTION_PRESETS:
            self._resolution_combo.addItem(f"{name}  ({w}x{hh})", (w, hh))
        self._resolution_combo.addItem(
            I18N.tr("composite.project_settings.custom", "Custom"), _CUSTOM_KEY)
        h.addWidget(self._resolution_combo, 1)
        self._resolution_combo.currentIndexChanged.connect(
            self._on_resolution_preset_changed)
        return row

    def _build_size_row(self) -> QWidget:
        # W [width] [锁] H [height] —— 宽高合并到同一行，更省空间。
        row, h = self._make_row()
        h.addWidget(self._make_label(
            I18N.tr("composite.project_settings.size", "Size")))

        wh_w = QLabel(I18N.tr("composite.project_settings.w_label", "W"))
        wh_w.setStyleSheet("color: #888; font-size: 10px;")
        h.addWidget(wh_w)
        self._width_drag = DragValue("", 64, 16384, 1920.0, decimals=0)
        self._width_drag.setFixedValueWidth(64)
        h.addWidget(self._width_drag)

        # 统一小锁图标（锁定=橙 / 解锁=灰），与全项目绑定图标一致。
        self._aspect_lock_btn = make_link_button(
            initial_linked=False,
            tooltip=I18N.tr("composite.project_settings.aspect_lock",
                            "Lock aspect ratio"),
        )
        self._aspect_lock_btn.toggled.connect(self._on_aspect_lock_toggled)
        h.addWidget(self._aspect_lock_btn)

        wh_h = QLabel(I18N.tr("composite.project_settings.h_label", "H"))
        wh_h.setStyleSheet("color: #888; font-size: 10px;")
        h.addWidget(wh_h)
        self._height_drag = DragValue("", 64, 16384, 1080.0, decimals=0)
        self._height_drag.setFixedValueWidth(64)
        h.addWidget(self._height_drag)

        h.addStretch(1)
        self._width_drag.valueChanged.connect(self._on_width_changed)
        self._height_drag.valueChanged.connect(self._on_height_changed)
        return row

    def _build_fps_row(self) -> QWidget:
        row, h = self._make_row()
        h.addWidget(self._make_label(
            I18N.tr("composite.project_settings.fps", "Frame Rate")))
        self._fps_combo = ChevronComboBox()
        for label, fps in _FPS_PRESETS:
            self._fps_combo.addItem(f"{label} fps", fps)
        self._fps_combo.addItem(
            I18N.tr("composite.project_settings.custom", "Custom"), _CUSTOM_KEY)
        h.addWidget(self._fps_combo, 1)
        # AUD-10814: route through _on_fps_changed — the trailing "Custom"
        # entry carries the non-numeric ``_CUSTOM_KEY`` sentinel until a
        # concrete value is stored, and _emit_changed used to call
        # float("__custom__") on it (ValueError inside the slot, signal lost).
        self._fps_combo.currentIndexChanged.connect(self._on_fps_changed)
        return row

    def _build_duration_row(self) -> QWidget:
        row, h = self._make_row()
        h.addWidget(self._make_label(
            I18N.tr("composite.project_settings.duration", "Duration")))
        self._duration_drag = DragValue("", 0.1, 3600.0, 3.0, decimals=1,
                                        suffix="s")
        self._duration_drag.setSingleStep(0.1)
        self._duration_drag.setFixedValueWidth(64)
        h.addWidget(self._duration_drag)
        unit = QLabel(I18N.tr("composite.project_settings.s_label", "s"))
        unit.setStyleSheet("color: #888; font-size: 10px;")
        h.addWidget(unit)
        h.addStretch(1)
        self._duration_drag.valueChanged.connect(self._emit_changed)
        return row

    def _build_background_row(self) -> QWidget:
        row, h = self._make_row()
        h.addWidget(self._make_label(
            I18N.tr("composite.project_settings.background_color",
                    "Background")))

        self._bg_color_control = ColorPickerControl(
            self._bg_color_value, parent=row)
        self._bg_color_control.color_changed.connect(
            self._on_background_color_changed)
        # 保留旧属性别名，兼容已有调用方读取 swatch/HEX 标签的代码。
        self._bg_swatch = self._bg_color_control.swatch
        self._bg_hex_label = self._bg_color_control.hex_label
        h.addWidget(self._bg_color_control, 1)
        return row

    # ------------------------------------------------------------------
    # 加载当前值
    # ------------------------------------------------------------------

    def load_from_graph(
        self,
        node_graph: Any,
        *,
        width: int | None = None,
        height: int | None = None,
        fps: float | None = None,
        duration_frames: int | None = None,
        timeline_bound: bool = False,
    ) -> None:
        """从 NodeGraph 读取 output 节点(分辨率/fps)+ constant 节点(背景色)。

        Args:
            node_graph: NodeGraph 实例(含 .nodes dict)。
        """
        self._block_emit = True
        try:
            graph_width = graph_height = None
            graph_fps = float(getattr(node_graph, "fps", 24.0) or 24.0)
            graph_duration = int(
                getattr(node_graph, "duration_frames", 0) or 0
            )
            constant_nodes = []
            for node in node_graph.nodes.values():
                if node.node_type == "output":
                    graph_width = int(node.parameters.get("width", 1920))
                    graph_height = int(node.parameters.get("height", 1080))
                    # output 不存 fps;读 graph 级别 fps（由上层传入更好，
                    # 这里从参数兜底）
                elif node.node_type == "constant":
                    constant_nodes.append(node)

            # Empty Comp Clips mark their default background Constant.  Keep
            # Project Settings scoped to that node; user-added Constant nodes
            # are independent generators and must retain their own colors.
            marked_background_nodes = [
                node for node in constant_nodes
                if bool(node.parameters.get(PROJECT_BACKGROUND_PARAM, False))
            ]
            background_nodes = marked_background_nodes or constant_nodes
            if background_nodes:
                # Preserve the legacy last-Constant fallback for graphs that
                # predate the explicit background marker.
                params = (marked_background_nodes[0]
                          if marked_background_nodes
                          else background_nodes[-1]).parameters
                legacy_srgb = params.get(CONSTANT_COLOR_SPACE_PARAM) != "linear"
                params = ConstantNode.normalize_color_parameters(
                    params, legacy_srgb=legacy_srgb)
                self._bg_color_value = (
                    float(params.get("color_r", 1.0)),
                    float(params.get("color_g", 1.0)),
                    float(params.get("color_b", 1.0)),
                    float(params.get("color_a", 1.0)),
                )

            effective_width = (
                int(width) if width is not None else graph_width
            )
            effective_height = (
                int(height) if height is not None else graph_height
            )
            if effective_width and effective_height:
                self._width_drag.setValue(float(effective_width))
                self._height_drag.setValue(float(effective_height))
                self._sync_resolution_combo()

            effective_fps = float(fps if fps is not None else graph_fps)
            self._set_fps_value(effective_fps)
            effective_duration = (
                int(duration_frames)
                if duration_frames is not None
                else graph_duration
            )
            if effective_duration > 0 and effective_fps > 0:
                self._duration_drag.setValue(
                    float(effective_duration) / effective_fps
                )

            self.set_timeline_bound(timeline_bound)

            self._bg_color_control.set_rgba(self._bg_color_value)
        finally:
            self._block_emit = False

    def _set_fps_value(self, fps: float) -> None:
        """Select a preset FPS, or keep a custom value visible if needed."""
        for index in range(self._fps_combo.count()):
            value = self._fps_combo.itemData(index)
            if isinstance(value, (int, float)) and abs(float(value) - fps) < 1e-6:
                self._fps_combo.setCurrentIndex(index)
                return
        custom_index = self._fps_combo.count() - 1
        self._fps_combo.setItemText(custom_index, f"{fps:g} fps")
        self._fps_combo.setItemData(custom_index, float(fps))
        self._fps_combo.setCurrentIndex(custom_index)

    def set_timeline_bound(self, bound: bool) -> None:
        """Lock project timing/size controls when Edit owns the clip metadata."""
        for widget in (
            self._resolution_combo,
            self._width_drag,
            self._height_drag,
            self._aspect_lock_btn,
            self._fps_combo,
            self._duration_drag,
        ):
            widget.setEnabled(not bound)
        tooltip = (
            I18N.tr(
                "composite.project_settings.timeline_bound_tooltip",
                "Controlled by the source clip in Edit",
            )
            if bound else ""
        )
        for widget in (
            self._resolution_combo,
            self._width_drag,
            self._height_drag,
            self._fps_combo,
            self._duration_drag,
        ):
            widget.setToolTip(tooltip)

    # ------------------------------------------------------------------
    # 交互逻辑
    # ------------------------------------------------------------------

    def _on_aspect_lock_toggled(self, checked: bool) -> None:
        self._aspect_locked = checked
        if checked and self._height_drag.value() > 0:
            self._aspect_ratio = (
                self._width_drag.value() / self._height_drag.value())

    def _on_width_changed(self, value: float) -> None:
        if self._aspect_locked and value > 0:
            new_h = max(64, min(16384, int(round(value / self._aspect_ratio))))
            self._height_drag.blockSignals(True)
            self._height_drag.setValue(new_h)
            self._height_drag.blockSignals(False)
        self._sync_resolution_combo()
        self._emit_changed()

    def _on_height_changed(self, value: float) -> None:
        if self._aspect_locked and value > 0:
            new_w = max(64, min(16384, int(round(value * self._aspect_ratio))))
            self._width_drag.blockSignals(True)
            self._width_drag.setValue(new_w)
            self._width_drag.blockSignals(False)
        self._sync_resolution_combo()
        self._emit_changed()

    def _on_resolution_preset_changed(self, index: int) -> None:
        if self._block_emit:
            return
        data = self._resolution_combo.itemData(index)
        if data == _CUSTOM_KEY or data is None:
            return
        w, h = data
        self._width_drag.blockSignals(True)
        self._height_drag.blockSignals(True)
        self._width_drag.setValue(float(w))
        self._height_drag.setValue(float(h))
        self._height_drag.blockSignals(False)
        self._width_drag.blockSignals(False)
        if self._aspect_locked and h > 0:
            self._aspect_ratio = w / h
        self._emit_changed()

    def _on_fps_changed(self, index: int) -> None:
        """Emit settings on FPS preset change (AUD-10814).

        Mirrors ``_on_resolution_preset_changed``: selecting the "Custom"
        entry is a placeholder (no concrete fps stored in its itemData yet),
        so there is nothing to emit — the sentinel string must never reach
        ``float()``.
        """
        if self._block_emit:
            return
        data = self._fps_combo.itemData(index)
        if data == _CUSTOM_KEY or data is None:
            return
        self._emit_changed()

    def _current_fps(self) -> float:
        """Resolve the FPS combo's current value with a safe fallback.

        The Custom entry keeps the ``_CUSTOM_KEY`` sentinel until
        ``_set_fps_value`` stores a concrete float into it; other emissions
        (width/height/duration/background) may fire while the combo sits on
        that entry, so parsing must tolerate the sentinel.
        """
        data = self._fps_combo.currentData()
        if isinstance(data, (int, float)) and float(data) > 0:
            return float(data)
        return 24.0

    def _sync_resolution_combo(self) -> None:
        cur_w = int(self._width_drag.value())
        cur_h = int(self._height_drag.value())
        for i in range(self._resolution_combo.count()):
            data = self._resolution_combo.itemData(i)
            if data == _CUSTOM_KEY:
                continue
            if data == (cur_w, cur_h):
                self._resolution_combo.blockSignals(True)
                self._resolution_combo.setCurrentIndex(i)
                self._resolution_combo.blockSignals(False)
                return
        self._resolution_combo.blockSignals(True)
        self._resolution_combo.setCurrentIndex(
            self._resolution_combo.count() - 1)
        self._resolution_combo.blockSignals(False)

    def _on_background_color_changed(self, color: QColor) -> None:
        """接收共享颜色控件的已确认颜色。"""
        self._bg_color_value = _qcolor_to_rgba(color)
        self._emit_changed()

    def _emit_changed(self) -> None:
        if self._block_emit:
            return
        self.settings_changed.emit({
            "width": int(self._width_drag.value()),
            "height": int(self._height_drag.value()),
            # AUD-10814: sentinel-safe fps resolution (see _current_fps).
            "fps": self._current_fps(),
            "duration_seconds": float(self._duration_drag.value()),
            "background_color": self._bg_color_value,
        })
