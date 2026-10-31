"""ProjectSettingsSection - Project Settings 的 section 容器。

仿照 NodeSectionWidget 的 header 子集（折叠 + 标题 + pin），内嵌
ProjectSettingsWidget 作为内容区。与节点 section 共存于参数面板，
可被 pin（固定不被清除）和折叠，与 Nuke 的 Project Settings 行为一致。

不复用/继承 NodeSectionWidget：Project Settings 不是节点，没有 node_id/
ParamDef/enable 等语义，强行复用会引入节点耦合。
"""

from __future__ import annotations

from typing import Any, Optional

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QFrame, QVBoxLayout, QHBoxLayout, QWidget, QLabel, QToolButton,
)

from src.shared.i18n.i18n import I18N
from src.shared.ui.style_sheet import (
    SETTINGS_SECTION_FRAME_STYLE,
    SETTINGS_SECTION_HEADER_STYLE,
    SETTINGS_SECTION_TITLE_STYLE,
    SETTINGS_SECTION_COLLAPSE_BTN_STYLE,
    TOOLBAR_FLAT_BTN_STYLE,
)
from src.shared.assets.icon_manager import get_icon, IconColors
from src.features.composite.ui.project_settings_widget import (
    ProjectSettingsWidget,
)


class ProjectSettingsSection(QFrame):
    """可折叠/可固定的 Project Settings 容器。

    Layout::

        ┌─ header ──────────────────────────────────┐
        │ ▼  Project Settings           📌          │
        ├─ content ─────────────────────────────────┤
        │ [Resolution] [Timing] [Background Color]   │
        └────────────────────────────────────────────┘

    Signals:
        pin_toggled(bool): pin 状态变化（is_pinned）。
        settings_changed(dict): 内嵌 ProjectSettingsWidget 的设置变化透传。
    """

    pin_toggled = pyqtSignal(bool)
    settings_changed = pyqtSignal(dict)

    _BTN_STYLE = TOOLBAR_FLAT_BTN_STYLE

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._is_collapsed = False
        self._is_pinned = False

        # 外框样式：与 NodeSectionWidget 保持一致（1px 边框 + 4px 圆角 + 面板背景）。
        # 注意 NODE_SECTION_STYLE 的选择器限定在 NodeSectionWidget，对本类不生效，
        # 因此这里用不限定选择器的内联样式显式呈现相同外框。
        self.setStyleSheet(SETTINGS_SECTION_FRAME_STYLE)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        layout.addWidget(self._build_header())
        self._content_widget = self._build_content()
        layout.addWidget(self._content_widget)

    # ------------------------------------------------------------------
    # Header（NodeSectionWidget 的子集：collapse + title + pin）
    # ------------------------------------------------------------------

    def _build_header(self) -> QWidget:
        header = QWidget()
        header.setFixedHeight(28)
        header.setStyleSheet(SETTINGS_SECTION_HEADER_STYLE)
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(6, 0, 2, 0)
        header_layout.setSpacing(2)

        # Collapse button
        self._collapse_btn = QToolButton()
        self._collapse_btn.setText("▼")
        self._collapse_btn.setFixedSize(16, 16)
        self._collapse_btn.setStyleSheet(SETTINGS_SECTION_COLLAPSE_BTN_STYLE)
        self._collapse_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._collapse_btn.clicked.connect(self.toggle_collapse)
        header_layout.addWidget(self._collapse_btn)

        # Title
        self._title_label = QLabel(
            I18N.tr("composite.project_settings.title", "Project Settings"))
        self._title_label.setStyleSheet(SETTINGS_SECTION_TITLE_STYLE)
        header_layout.addWidget(self._title_label)

        header_layout.addStretch()

        # Pin button
        self._pin_btn = QToolButton()
        self._pin_btn.setIcon(get_icon("fa5s.thumbtack", IconColors.WHITE))
        self._pin_btn.setIconSize(self._pin_btn.iconSize())
        self._pin_btn.setFixedSize(24, 24)
        self._pin_btn.setCheckable(True)
        self._pin_btn.setStyleSheet(self._BTN_STYLE)
        self._pin_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._pin_btn.setToolTip(
            I18N.tr("composite.project_settings.pin_tooltip", "Pin Project Settings"))
        self._pin_btn.clicked.connect(self._on_pin_toggled)
        header_layout.addWidget(self._pin_btn)

        return header

    def _build_content(self) -> QWidget:
        wrapper = QWidget()
        wrapper.setStyleSheet("background-color: #27272b; border: none;")
        l = QVBoxLayout(wrapper)
        l.setContentsMargins(0, 0, 0, 0)
        l.setSpacing(0)
        self._settings_widget = ProjectSettingsWidget()
        self._settings_widget.settings_changed.connect(
            self.settings_changed.emit)
        l.addWidget(self._settings_widget)
        return wrapper

    # ------------------------------------------------------------------
    # 折叠 / Pin
    # ------------------------------------------------------------------

    def toggle_collapse(self) -> None:
        self._is_collapsed = not self._is_collapsed
        self._content_widget.setVisible(not self._is_collapsed)
        self._collapse_btn.setText("▶" if self._is_collapsed else "▼")

    def collapse(self) -> None:
        """折叠 section（幂等：已折叠则 no-op）。

        选中其他节点时由 NodePropertiesPanel.set_nodes 调用，避免 Project
        Settings 占用过多空间；已折叠时不重复触发。
        """
        if not self._is_collapsed:
            self.toggle_collapse()

    def _on_pin_toggled(self) -> None:
        self._is_pinned = self._pin_btn.isChecked()
        color = IconColors.ORANGE if self._is_pinned else IconColors.WHITE
        self._pin_btn.setIcon(get_icon("fa5s.thumbtack", color))
        self.pin_toggled.emit(self._is_pinned)

    @property
    def is_pinned(self) -> bool:
        return self._is_pinned

    # ------------------------------------------------------------------
    # 代理到内嵌 widget
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
        self._settings_widget.load_from_graph(
            node_graph,
            width=width,
            height=height,
            fps=fps,
            duration_frames=duration_frames,
            timeline_bound=timeline_bound,
        )

    def set_settings_callback(self, callback) -> None:
        """连接设置变化回调（透传给内嵌 ProjectSettingsWidget）。"""
        self._settings_widget.settings_changed.connect(callback)
