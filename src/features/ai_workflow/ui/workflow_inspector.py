"""P10.2c WorkflowInspector —— 节点参数编辑面板（plan §10 P10.2）。

> 选中节点 → Inspector 从 :class:`WorkflowNodeSpec.params_schema` 渲染参数控件；
> 改控件 → emit ``parameter_changed(node_id, key, value)`` → host 经
> :class:`WorkflowGraphEdit.set_node_param` 更新 graph（冻结 replace）。

借鉴 Composite ``NodePropertiesPanel``（6129 行）但**极简**：只渲染基础类型
（bool/string/number/integer + enum），无 slider/多选/group/preview。

JSON-Schema → 控件映射：

- ``{"type": "boolean"}`` → :class:`QCheckBox`
- ``{"type": "string", "enum": [...]}`` → :class:`QComboBox`
- ``{"type": "string"}`` → :class:`QLineEdit`
- ``{"type": "number", "minimum":.., "maximum":..}`` → :class:`QDoubleSpinBox`
- ``{"type": "integer", ...}`` → :class:`QSpinBox`
- 其它/未知 → :class:`QLineEdit`（toString 兜底）

红线（plan §1.4.1 / §2.2）：

- 不引入 ``moderngl`` / ``composite.core``（纯 Qt 面板）。
- 参数值 JSON-safe（host 经 WorkflowGraphEdit.set_node_param 校验）。
"""
from __future__ import annotations

from typing import Any, Dict

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QDoubleSpinBox, QFormLayout, QLabel, QLineEdit,
    QSpinBox, QVBoxLayout, QWidget,
)

from src.shared.i18n.i18n import I18N


class WorkflowInspector(QWidget):
    """节点参数编辑面板（极简版）。

    Signals:
        parameter_changed(str, str, object): (node_id, param_name, value)。
    """

    parameter_changed = pyqtSignal(str, str, object)

    def __init__(self, parent: Any = None) -> None:
        super().__init__(parent)
        self._node_id: str = ""
        self._param_widgets: Dict[str, QWidget] = {}
        # AUD-24005：程序化刷新（refresh_param）期间抑制 parameter_changed
        # 回发（状态锁防递归信号，.clinerules）。
        self._refreshing: bool = False
        self._build_ui()

    # ---------------- 构建 ---------------- #

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(4)

        self._title = QLabel(I18N.tr("workflow.inspector.no_selection", "<b>(no selection)</b>"))
        self._title.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(self._title)

        self._form_host = QWidget()
        self._form = QFormLayout(self._form_host)
        self._form.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._form_host, 1)

    # ---------------- 公共 API ---------------- #

    @property
    def current_node_id(self) -> str:
        return self._node_id

    @property
    def param_widgets(self) -> Dict[str, QWidget]:
        """参数名 → 控件（测试/外部访问用）。"""
        return self._param_widgets

    def set_node(
        self, node_id: str, node_spec: Any, params: Dict[str, Any],
    ) -> None:
        """加载节点：从 spec.params_schema 渲染控件，初值取 params。"""
        self.clear_node()
        self._node_id = node_id
        display = getattr(node_spec, "display_name", "") or node_id
        self._title.setText(f"<b>{display}</b>")

        schema = getattr(node_spec, "params_schema", {}) or {}
        properties = schema.get("properties", {}) if isinstance(schema, dict) else {}
        for key, definition in properties.items():
            value = params.get(key)
            widget = self._build_param_widget(key, definition, value)
            if widget is not None:
                self._param_widgets[key] = widget
                self._form.addRow(key, widget)

    def clear_node(self) -> None:
        """清空（无选中）。

        QFormLayout.removeRow 会 deleteLater 掉行内控件，故此处不再手动
        deleteLater（避免对已删除的 C++ 对象操作）。
        """
        self._node_id = ""
        self._title.setText(I18N.tr("workflow.inspector.no_selection", "<b>(no selection)</b>"))
        # 清空 form（removeRow 自动 deleteLater 行内 widget）
        while self._form.rowCount() > 0:
            self._form.removeRow(0)
        self._param_widgets = {}

    def refresh_param(self, node_id: str, key: str, value: Any) -> None:
        """跨视图参数同步（AUD-24005）：host 在参数成功入图后调用。

        只刷新本 Inspector 当前节点且已有控件的参数；带 ``_refreshing``
        状态锁，控件信号触发的 ``_emit_param`` 在刷新期间不回发，防递归。
        """
        if node_id != self._node_id:
            return
        widget = self._param_widgets.get(key)
        if widget is None:
            return
        self._refreshing = True
        try:
            if isinstance(widget, QCheckBox):
                widget.setChecked(bool(value))
            elif isinstance(widget, QComboBox):
                idx = widget.findText(str(value))
                if idx >= 0:
                    widget.setCurrentIndex(idx)
            elif isinstance(widget, QDoubleSpinBox):
                widget.setValue(self._to_float(value, widget.minimum()))
            elif isinstance(widget, QSpinBox):
                widget.setValue(self._to_int(value, widget.minimum()))
            elif isinstance(widget, QLineEdit):
                widget.setText("" if value is None else str(value))
        finally:
            self._refreshing = False

    def _emit_param(self, node_id: str, key: str, value: Any) -> None:
        """控件信号 → parameter_changed（程序化刷新期间抑制，防递归）。"""
        if self._refreshing:
            return
        self.parameter_changed.emit(node_id, key, value)

    @staticmethod
    def _to_float(value: Any, default: float) -> float:
        """AUD-23920：容错 float 转换（外部持久化值可为字符串/嵌套对象）。"""
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _to_int(value: Any, default: int) -> int:
        """AUD-23920：容错 int 转换。"""
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    # ---------------- 控件工厂 ---------------- #

    def _build_param_widget(
        self, key: str, definition: Dict[str, Any], value: Any,
    ) -> QWidget:
        """据 JSON-Schema definition 建 widget，连 changed → emit parameter_changed。

        AUD-23920：value / minimum / maximum / multipleOf 来自外部持久化
        JSON，类型可能漂移——数值一律经 ``_to_float`` / ``_to_int`` 容错转换
        （坏值回退边界/默认），不让选中/加载路径崩溃。
        """
        ptype = definition.get("type", "string") if isinstance(definition, dict) else "string"
        node_id = self._node_id

        if ptype == "boolean":
            cb = QCheckBox()
            cb.setChecked(bool(value))
            cb.toggled.connect(
                lambda v, nid=node_id, k=key: self._emit_param(nid, k, v)
            )
            return cb

        if ptype == "string" and "enum" in definition:
            combo = QComboBox()
            for opt in definition["enum"]:
                combo.addItem(str(opt))
            if value is not None:
                idx = combo.findText(str(value))
                if idx >= 0:
                    combo.setCurrentIndex(idx)
            combo.currentTextChanged.connect(
                lambda v, nid=node_id, k=key: self._emit_param(nid, k, v)
            )
            return combo

        if ptype == "string":
            le = QLineEdit("" if value is None else str(value))
            le.editingFinished.connect(
                lambda *_a, nid=node_id, k=key, w=le:
                    self._emit_param(nid, k, w.text())
            )
            return le

        if ptype == "number":
            sb = QDoubleSpinBox()
            mn = self._to_float(definition.get("minimum", 0.0), 0.0)
            mx = self._to_float(definition.get("maximum", 1.0), 1.0)
            if mx < mn:
                mx = mn  # 坏 schema（min>max）兜底，避免 setRange 倒挂
            sb.setRange(mn, mx)
            sb.setValue(self._to_float(value, mn))
            if "multipleOf" in definition:
                sb.setSingleStep(
                    self._to_float(definition["multipleOf"], 1.0) or 1.0,
                )
            sb.valueChanged.connect(
                lambda v, nid=node_id, k=key: self._emit_param(nid, k, v)
            )
            return sb

        if ptype == "integer":
            sb = QSpinBox()
            mn = self._to_int(definition.get("minimum", 0), 0)
            mx = self._to_int(definition.get("maximum", 100), 100)
            if mx < mn:
                mx = mn
            sb.setRange(mn, mx)
            sb.setValue(self._to_int(value, mn))
            sb.valueChanged.connect(
                lambda v, nid=node_id, k=key: self._emit_param(nid, k, v)
            )
            return sb

        # 兜底：LineEdit（toString）
        le = QLineEdit("" if value is None else str(value))
        le.editingFinished.connect(
            lambda *_a, nid=node_id, k=key, w=le:
                self._emit_param(nid, k, w.text())
        )
        return le


__all__ = ["WorkflowInspector"]
