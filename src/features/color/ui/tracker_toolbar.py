"""Tracker 模式工具栏（阶段 5，计划 §5.3）。

固定排列：点跟踪 / 区域跟踪 / 平面透视 / 3D 摄像机（禁用）/
目标选择下拉框 / 2D-3D 模式切换。
"""

from __future__ import annotations

from PyQt6.QtCore import QSize, Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QHBoxLayout,
    QToolButton,
    QWidget,
)

from src.shared.i18n.i18n import tr
from src.shared.ui.widgets import ChevronComboBox

BTN_SIZE = 30          # 每个按钮 30x30 px
ICON_SIZE = 18         # 图标 18x18 px

TOOL_IDS = ("point", "region", "planar", "camera3d")

TOOL_TIPS = {
    "point": ("tracker.tool_point", "点跟踪"),
    "region": ("tracker.tool_region", "区域跟踪"),
    "planar": ("tracker.tool_planar", "平面/透视跟踪"),
    "camera3d": ("tracker.tool_camera3d", "3D 摄像机跟踪（暂未开放）"),
}

_SELECTED_STYLE = (
    "QToolButton { background: rgba(255, 136, 0, 35); border: 1px solid transparent;"
    " border-bottom: 2px solid #FF8800; border-radius: 3px; color: #FFFFFF; }"
)
_NORMAL_STYLE = (
    "QToolButton { background: transparent; border: 1px solid transparent;"
    " border-radius: 3px; color: #A9A9AE; }"
    "QToolButton:hover { background: #2C2D31; border-color: #505158; }"
)

TOOL_GLYPHS = {
    "point": "•",
    "region": "□",
    "planar": "◇",
    "camera3d": "3D",
}


class TrackerToolBar(QWidget):
    """模式工具栏。3D 摄像机按钮按范围约定显示禁用态。"""

    tool_changed = pyqtSignal(str)        # tool id
    target_changed = pyqtSignal(str)      # 目标选择
    dimension_changed = pyqtSignal(str)   # "2d" / "3d"

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        # 作为面板标题下的上下文工具栏，保持 DaVinci 式紧凑高度；
        # 工具按钮尺寸保留 30 px，方便鼠标操作和现有调用方复用。
        self.setFixedHeight(34)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 2, 8, 2)
        layout.setSpacing(2)

        self._tool_buttons: dict[str, QToolButton] = {}
        for tool_id in TOOL_IDS:
            btn = QToolButton(self)
            btn.setObjectName(f"tracker_tool_{tool_id}")
            btn.setFixedSize(BTN_SIZE, BTN_SIZE)
            btn.setIconSize(QSize(ICON_SIZE, ICON_SIZE))
            btn.setCheckable(True)
            btn.setFocusPolicy(Qt.FocusPolicy.TabFocus)
            label_key, label_default = TOOL_TIPS[tool_id]
            btn.setToolTip(tr(label_key, label_default))
            btn.setText(TOOL_GLYPHS[tool_id])
            btn.setFont(btn.font())
            btn.clicked.connect(
                lambda _checked=False, tid=tool_id: self._on_tool_clicked(tid)
            )
            self._tool_buttons[tool_id] = btn
            layout.addWidget(btn)
        # 阶段 9/10 不在范围：3D 摄像机禁用态
        self._tool_buttons["camera3d"].setEnabled(False)
        self._tool_buttons["region"].setChecked(True)
        self._refresh_tool_styles()

        # 目标选择下拉框
        self.target_combo = ChevronComboBox(self)
        self.target_combo.setObjectName("tracker_target_combo")
        self.target_combo.setToolTip(tr("tracker.target_tooltip", "跟踪目标"))
        self.target_combo.addItem(tr("tracker.window_default", "Window 1"), "")
        self.target_combo.setMinimumWidth(92)
        self.target_combo.setFixedHeight(26)
        # 发射 itemData（shape_id）而非文本：同名窗口也能区分
        self.target_combo.currentIndexChanged.connect(
            lambda _index: self.target_changed.emit(self.current_target_id()))
        layout.addWidget(self.target_combo)

        # 2D / 3D 模式切换
        self.dim_combo = ChevronComboBox(self)
        self.dim_combo.setObjectName("tracker_dim_combo")
        self.dim_combo.setToolTip(tr("tracker.dimension_tooltip", "跟踪维度"))
        self.dim_combo.addItems([
            tr("tracker.dimension_2d", "2D"),
            tr("tracker.dimension_3d", "3D"),
        ])
        self.dim_combo.setMinimumWidth(48)
        self.dim_combo.setFixedHeight(26)
        self.dim_combo.setCurrentIndex(0)
        # 3D 尚未实现：下拉中保留但选中时回退
        self.dim_combo.currentIndexChanged.connect(self._on_dim_changed)
        layout.addWidget(self.dim_combo)

        layout.addStretch(1)

    # ----------------------------------------------------------------

    def _refresh_tool_styles(self) -> None:
        for btn in self._tool_buttons.values():
            btn.setStyleSheet(_SELECTED_STYLE if btn.isChecked() else _NORMAL_STYLE)

    def _on_tool_clicked(self, tool_id: str) -> None:
        for tid, btn in self._tool_buttons.items():
            btn.setChecked(tid == tool_id)
        self._refresh_tool_styles()
        self.tool_changed.emit(tool_id)

    def _on_dim_changed(self, index: int) -> None:
        text = self.dim_combo.itemText(index).lower()
        if text == "3d":
            # 3D 不在当前范围：提示并回退 2D
            self.dim_combo.blockSignals(True)
            self.dim_combo.setCurrentIndex(0)
            self.dim_combo.blockSignals(False)
            text = "2d"
        self.dimension_changed.emit(text)

    # ----------------------------------------------------------------

    def current_tool(self) -> str:
        for tid, btn in self._tool_buttons.items():
            if btn.isChecked():
                return tid
        return "region"

    def set_target_names(self, names: list[str] | None) -> None:
        """按显示名填充目标下拉并尽量保留当前选择。

        AUD-25810：此前 ``addItems(names or [...])`` 默认 names 可为
        None/空，但随后 ``current in names`` 在 None 时抛 TypeError，且
        异常逃逸时 blockSignals(True) 未释放，下拉后续程序化更新全部
        静默失效。先归一化为 list，并用 try/finally 保证信号锁配对。
        """
        items = list(names) if names else [tr("tracker.window_default", "Window 1")]
        current = self.target_combo.currentText()
        self.target_combo.blockSignals(True)
        try:
            self.target_combo.clear()
            self.target_combo.addItems(items)
            if current in items:
                self.target_combo.setCurrentText(current)
        finally:
            self.target_combo.blockSignals(False)

    def set_targets(self, pairs: list, selected_id: str | None = None) -> None:
        """按 (shape_id, 显示名) 填充目标下拉并选中当前绑定。

        target_changed 随后发射的是 shape_id（itemData），控制器据此
        直接重绑，避免同名窗口无法区分。
        """
        self.target_combo.blockSignals(True)
        self.target_combo.clear()
        for shape_id, name in (pairs or []):
            self.target_combo.addItem(str(name), str(shape_id))
        if not pairs:
            self.target_combo.addItem(tr("tracker.window_default", "Window 1"), "")
        if selected_id is not None:
            index = self.target_combo.findData(str(selected_id))
            if index >= 0:
                self.target_combo.setCurrentIndex(index)
        self.target_combo.blockSignals(False)

    def current_target_id(self) -> str:
        data = self.target_combo.currentData()
        return str(data) if data is not None else ""

    def tool_button(self, tool_id: str) -> QToolButton:
        return self._tool_buttons[tool_id]
