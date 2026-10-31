"""参数表单页 —— 模板的「只改参数不画图」执行入口。

> 用户选模板 → 本页按节点分组渲染参数控件（复用 :class:`WorkflowInspector`
> 的 JSON-Schema→控件工厂，每节点一节）→ Run；「高级编辑」才进画布。

- 控件改动 → ``parameter_changed(node_id, key, value)``（与画布 Inspector
  同一信号约定，host 走既有 ``WorkflowGraphEdit.set_node_param`` 路径入图）。
- 无 params_schema 的节点（纯输入/输出节点）不渲染节，保持表单精简。
- 纯 Qt；不依赖画布 / moderngl。
"""
from __future__ import annotations

from typing import Any

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import (
    QHBoxLayout, QLabel, QPushButton, QScrollArea, QFrame,
    QVBoxLayout, QWidget,
)

from src.shared.i18n.i18n import tr
from src.features.ai_workflow.ui.workflow_inspector import WorkflowInspector


class WorkflowFormPage(QWidget):
    """模板参数表单（分节点分节）。

    Signals:
        parameter_changed(str, str, object): (node_id, param, value)。
        advanced_edit_requested(): 用户点「高级编辑」→ host 切画布页。
        gallery_requested(): 用户点「返回模板库」→ host 切模板库页
            （仅切视图，保留当前图；「清空」才是放弃图的显式操作）。
    """

    parameter_changed = pyqtSignal(str, str, object)
    advanced_edit_requested = pyqtSignal()
    gallery_requested = pyqtSignal()

    def __init__(
        self,
        node_registry: Any,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._node_registry = node_registry
        self._inspectors: list[WorkflowInspector] = []
        self._build_ui()

    # ---------------- 构建 ---------------- #

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        header_row = QHBoxLayout()
        back_button = QPushButton(
            "‹ " + tr("workflow.form.back_to_gallery", "返回模板库"),
        )
        back_button.setToolTip(tr(
            "workflow.form.back_to_gallery_tooltip",
            "回到模板库选择其他模板；当前工作流会保留（清空才会丢弃）",
        ))
        back_button.clicked.connect(self.gallery_requested.emit)
        header_row.addWidget(back_button)
        self._title = QLabel(tr("workflow.form.title", "参数"))
        self._title.setStyleSheet("font-size: 15px; font-weight: 600;")
        header_row.addWidget(self._title, 1)
        advanced_button = QPushButton(
            tr("workflow.form.advanced_edit", "高级编辑（画布）"),
        )
        advanced_button.clicked.connect(self.advanced_edit_requested.emit)
        header_row.addWidget(advanced_button)
        layout.addLayout(header_row)

        self._scroll = QScrollArea(self)
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._host = QWidget()
        self._sections = QVBoxLayout(self._host)
        self._sections.setContentsMargins(4, 4, 4, 4)
        self._sections.setSpacing(8)
        self._sections.addStretch(1)
        self._scroll.setWidget(self._host)
        layout.addWidget(self._scroll, 1)

    # ---------------- 公共 API ---------------- #

    def set_graph(self, graph: Any) -> None:
        """按 graph 节点重建表单（每节点一节 WorkflowInspector）。"""
        self._clear_sections()
        if graph is None:
            self._title.setText(tr("workflow.form.title", "参数"))
            return
        self._title.setText(tr(
            "workflow.form.title_with_name", "{name} · 参数",
        ).format(name=graph.name or graph.workflow_id))

        shown = 0
        for node in graph.nodes:
            spec = self._node_registry.get(node.spec_id)
            if spec is None:
                continue
            schema = getattr(spec, "params_schema", {}) or {}
            properties = (
                schema.get("properties", {}) if isinstance(schema, dict) else {}
            )
            if not properties:
                continue  # 纯输入/输出节点不占表单空间
            section_host = QWidget()
            section = QVBoxLayout(section_host)
            section.setContentsMargins(0, 0, 0, 0)
            section.setSpacing(2)
            caption = QLabel(tr(
                "workflow.form.node_section", "节点：{node}",
            ).format(node=getattr(spec, "display_name", "") or node.node_id))
            caption.setStyleSheet("color: #9ca3af; font-weight: 600;")
            section.addWidget(caption)
            inspector = WorkflowInspector(section_host)
            inspector.set_node(node.node_id, spec, dict(node.params))
            inspector.parameter_changed.connect(self.parameter_changed.emit)
            section.addWidget(inspector)
            self._sections.insertWidget(self._sections.count() - 1, section_host)
            self._inspectors.append(inspector)
            shown += 1

        if not shown:
            hint = QLabel(tr(
                "workflow.form.no_params",
                "该模板没有可调参数，直接 Run 即可",
            ))
            hint.setStyleSheet("color: #9ca3af;")
            self._sections.insertWidget(self._sections.count() - 1, hint)

    # ---------------- 内部 ---------------- #

    def _clear_sections(self) -> None:
        # 保留末尾 stretch，移除其余节
        while self._sections.count() > 1:
            item = self._sections.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self._inspectors = []


__all__ = ["WorkflowFormPage"]
