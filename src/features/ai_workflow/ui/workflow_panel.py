"""P8.5 WorkflowPanel —— AI Workflow dock 容器面板（plan §7.1 / §8）。

> Run Drawer：队列、进度、artifact 预览、diff、审批、日志。

提供 :class:`WorkflowPanel`（``QWidget``），AI Workflow dock 的内容容器：

- **顶部工具栏**：模板下拉 + Load/Clear 按钮 + 缩放 label + Run/Cancel（P9 接线）。
- **主区**：上画布（:attr:`adapter.view`）下 Run Drawer（:attr:`run_drawer`），
  ``QSplitter`` 上下分。
- **空态**：无 graph 时画布区显示提示（QLabel）。

P9 增强（plan §10.7 P9）：

- 持有当前 :attr:`current_graph`（controller 据此编译）。
- Run 按钮随 ``node_count`` 使能（无 graph 禁用）。
- ``run_requested`` / ``cancel_requested`` signal（controller 连接）。
- 嵌 :class:`AiRunDrawer`（复用 edit feature 现成 widget）；:meth:`set_run_drawer`
  带 ``_updating`` 锁防递归（仿 ``ai_edit_assistant_panel.py:795``）。

红线（plan §1.4.1 / §1.4.3 / §0）：

- **不引入** ``moderngl`` / ``composite.core``。
- **不直接写 timeline**（panel 只装配 + 转发信号；运行经 WorkflowCompiler）。
"""
from __future__ import annotations

import json
import logging
from dataclasses import replace
from typing import Any, Optional

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QStackedLayout,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from src.shared.ui.panel_splitter import PanelSplitter
from src.shared.i18n.i18n import tr
from src.features.ai_workflow.ui.workflow_canvas_adapter import WorkflowCanvasAdapter

logger = logging.getLogger(__name__)


class WorkflowPanel(QWidget):
    """AI Workflow dock 容器面板。

    Attributes:
        adapter: :class:`WorkflowCanvasAdapter`（host 通过它接 run 信号）。
        run_drawer: :class:`AiRunDrawer`（运行活动流，复用 edit feature）。
        current_graph: 当前画布渲染的 :class:`WorkflowGraph`（无则 None）。
    """

    # P9：向外通知运行意图（controller 连接）
    run_requested = pyqtSignal()
    cancel_requested = pyqtSignal()
    preview_plan_requested = pyqtSignal()
    approval_requested = pyqtSignal()
    apply_plan_requested = pyqtSignal(str)
    artifact_requested = pyqtSignal(str)

    def __init__(
        self,
        *,
        registry,
        template_registry,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._registry = registry
        self._template_registry = template_registry
        self._current_graph = None
        self._current_skill_id = ""
        self._current_template_id = ""
        self._approval_grant_id = ""
        self._updating = False  # set_run_drawer 防递归锁
        self.adapter = WorkflowCanvasAdapter(registry=registry)

        # P10.1d：节点移动变更层 + 撤销栈
        from src.features.ai_workflow.core.workflow_graph_edit import WorkflowGraphEdit
        from src.shared.node_canvas.undo_stack import UndoStack
        self._edit = WorkflowGraphEdit()
        self.undo_stack = UndoStack(parent=self)
        # P10.2b：节点搜索弹窗（懒建）
        self._node_search_popup = None
        # 视图模式（gallery / form / canvas）：默认模板库；load_template/load_graph
        # 落表单页；「高级编辑」才进画布（plan §10.7.4「先官方模板」的产品化）。
        self._view_mode = "gallery"

        self._build_ui()
        self._connect_signals()
        self._populate_template_combo()

    # ---------------- UI 构建 ---------------- #

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)

        # 顶部工具栏
        toolbar = QHBoxLayout()
        toolbar.setSpacing(6)
        self.template_combo = QComboBox(self)
        self.template_combo.setMinimumWidth(180)
        toolbar.addWidget(self.template_combo)

        self.load_button = QPushButton(tr("workflow.toolbar.load", "Load"), self)
        self.load_button.clicked.connect(self._on_load_clicked)
        toolbar.addWidget(self.load_button)

        self.clear_button = QPushButton(tr("workflow.toolbar.clear", "Clear"), self)
        self.clear_button.clicked.connect(self.clear_canvas)
        toolbar.addWidget(self.clear_button)

        # 视图切换（模板库默认 → 表单 → 画布高级编辑）
        self.form_view_button = QPushButton(tr("workflow.toolbar.form", "表单"), self)
        self.form_view_button.clicked.connect(self.show_form_view)
        toolbar.addWidget(self.form_view_button)
        self.canvas_view_button = QPushButton(
            tr("workflow.toolbar.canvas", "画布"), self,
        )
        self.canvas_view_button.clicked.connect(self.show_canvas_view)
        toolbar.addWidget(self.canvas_view_button)

        toolbar.addStretch()

        self.zoom_label = QLabel("100%", self)
        self.zoom_label.setMinimumWidth(60)
        toolbar.addWidget(self.zoom_label)

        # Run / Cancel（P9：Run 随 graph 状态使能；Cancel 默认禁用，run 进行时使能）
        self.run_button = QPushButton(tr("workflow.toolbar.run", "Run"), self)
        self.run_button.setEnabled(False)
        self.run_button.clicked.connect(self.run_requested.emit)
        toolbar.addWidget(self.run_button)

        self.cancel_button = QPushButton(
            tr("workflow.toolbar.cancel", "Cancel"), self,
        )
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self.cancel_requested.emit)
        toolbar.addWidget(self.cancel_button)

        layout.addLayout(toolbar)

        # Context Bar：只展示宿主提供的公开作用域，不在面板内猜工程状态。
        context_bar = QFrame(self)
        context_bar.setFrameShape(QFrame.Shape.StyledPanel)
        context_layout = QHBoxLayout(context_bar)
        context_layout.setContentsMargins(6, 3, 6, 3)
        context_layout.setSpacing(10)
        self.context_labels: dict[str, QLabel] = {}
        for key, label in (
            ("project", "工程"), ("timeline", "时间线"),
            ("brief", "Brief"), ("revision", "Revision"),
            ("assets", "素材"), ("provider", "Provider"),
            ("permission", "权限"),
        ):
            value = QLabel(f"{label}: —", context_bar)
            value.setStyleSheet("color: #666;")
            context_layout.addWidget(value)
            self.context_labels[key] = value
        context_layout.addStretch(1)
        self.context_status_label = QLabel(tr("workflow.status.not_assembled", "状态: 未装配"), context_bar)
        context_layout.addWidget(self.context_status_label)
        layout.addWidget(context_bar)

        # Plan/Diff/Evidence/Approval/Release Gate 工作区。
        action_bar = QHBoxLayout()
        action_bar.setSpacing(6)
        self.preview_plan_button = QPushButton(tr("workflow.actions.preview_plan", "预览计划"), self)
        self.preview_plan_button.setEnabled(False)
        self.preview_plan_button.clicked.connect(self.preview_plan_requested.emit)
        action_bar.addWidget(self.preview_plan_button)
        self.approval_button = QPushButton(tr("workflow.actions.request_approval", "请求审批"), self)
        self.approval_button.setEnabled(False)
        self.approval_button.clicked.connect(self.approval_requested.emit)
        action_bar.addWidget(self.approval_button)
        self.apply_plan_button = QPushButton(tr("workflow.actions.apply_approved_plan", "应用已批准计划"), self)
        self.apply_plan_button.setEnabled(False)
        self.apply_plan_button.clicked.connect(
            lambda: self.apply_plan_requested.emit(self._approval_grant_id)
        )
        action_bar.addWidget(self.apply_plan_button)
        action_bar.addStretch(1)
        self.plan_status_label = QLabel(tr("workflow.status.no_plan", "暂无计划"), self)
        self.plan_status_label.setStyleSheet("color: #666;")
        action_bar.addWidget(self.plan_status_label)
        layout.addLayout(action_bar)

        self.plan_tabs = QTabWidget(self)
        self.plan_text = self._make_readonly_text()
        self.diff_text = self._make_readonly_text()
        self.evidence_text = self._make_readonly_text()
        self.approval_text = self._make_readonly_text()
        self.release_gate_text = self._make_readonly_text()
        self.metrics_text = self._make_readonly_text()
        self.plan_tabs.addTab(self.plan_text, "Plan")
        self.plan_tabs.addTab(self.diff_text, "Diff")
        self.plan_tabs.addTab(self.evidence_text, "Evidence")
        self.plan_tabs.addTab(self.approval_text, "Approval")
        self.plan_tabs.addTab(self.release_gate_text, "Release Gate")
        self.plan_tabs.addTab(self.metrics_text, "Metrics")
        self.plan_tabs.setMaximumHeight(190)
        layout.addWidget(self.plan_tabs)

        # 主区：左右分（左=画布+Run Drawer 上下分，右=Inspector）
        outer_splitter = PanelSplitter(Qt.Orientation.Horizontal, self)
        outer_splitter.setChildrenCollapsible(False)

        # 左：上下分（画布 / Run Drawer）
        main_splitter = PanelSplitter(Qt.Orientation.Vertical, outer_splitter)
        main_splitter.setChildrenCollapsible(False)

        # 画布区：stacked（模板库 / 空态 / 参数表单 / 画布）
        canvas_host = QWidget()
        self._stack = QStackedLayout(canvas_host)
        from src.features.ai_workflow.ui.workflow_gallery_page import (
            TemplateGalleryPage,
        )
        self.gallery_page = TemplateGalleryPage(self._template_registry, self)
        self._stack.addWidget(self.gallery_page)
        self.empty_hint_widget = QLabel(
            tr("workflow.canvas.empty_hint",
               "Select a template above or drop assets here"),
            self,
        )
        self.empty_hint_widget.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_hint_widget.setStyleSheet("color: #888; font-size: 13px;")
        self._stack.addWidget(self.empty_hint_widget)
        from src.features.ai_workflow.ui.workflow_form_page import WorkflowFormPage
        self.form_page = WorkflowFormPage(self._registry, self)
        self._stack.addWidget(self.form_page)
        self._stack.addWidget(self.adapter.view)
        self._stack.setCurrentWidget(self.gallery_page)
        main_splitter.addWidget(canvas_host)

        # Run Drawer（复用 edit feature 现成 widget）
        from src.features.edit.ui.ai_assistant_composer import (
            AiRunDrawer, RunDrawerState,
        )
        self.run_drawer = AiRunDrawer()
        self.run_drawer.set_state(RunDrawerState())
        main_splitter.addWidget(self.run_drawer)
        main_splitter.setStretchFactor(0, 3)
        main_splitter.setStretchFactor(1, 1)
        outer_splitter.addWidget(main_splitter)

        # 右：Inspector（P10.2c）
        from src.features.ai_workflow.ui.workflow_inspector import WorkflowInspector
        self.inspector = WorkflowInspector(outer_splitter)
        outer_splitter.addWidget(self.inspector)
        outer_splitter.setStretchFactor(0, 4)
        outer_splitter.setStretchFactor(1, 1)

        layout.addWidget(outer_splitter, 1)

    def _connect_signals(self) -> None:
        self.adapter.view.zoom_changed.connect(self._on_zoom_changed)
        self.adapter.view.graph_changed.connect(self._on_graph_changed)
        # P10.1d：节点移动 → 变更层 + 撤销栈
        self.adapter.view.nodes_moved.connect(self._on_nodes_moved)
        self.adapter.view.undo_requested.connect(self.undo_stack.undo)
        self.adapter.view.redo_requested.connect(self.undo_stack.redo)
        # P10.2c：连线创建 → graph_edit.add_edge
        self.adapter.view.node_connected.connect(self._on_node_connected)
        # P10.2c：选中节点 → Inspector
        self.adapter.view.node_selected.connect(self._on_node_selected)
        self.adapter.view.node_double_clicked.connect(self._on_node_selected)
        self.inspector.parameter_changed.connect(self._on_parameter_changed)
        # P10.2b：Tab 加节点 → 弹搜索窗
        self.adapter.view.add_node_requested.connect(self._on_add_node_requested)
        self.run_drawer.artifact_clicked.connect(self._on_artifact_clicked)
        # 模板库 / 表单页
        self.gallery_page.template_selected.connect(self.load_template)
        self.form_page.parameter_changed.connect(self._on_parameter_changed)
        self.form_page.advanced_edit_requested.connect(self.show_canvas_view)
        # 返回模板库：仅切视图，保留当前图（清空才是显式丢弃）
        self.form_page.gallery_requested.connect(self.show_gallery_view)

    def _populate_template_combo(self) -> None:
        self.template_combo.clear()
        # 占位项
        self.template_combo.addItem(
            tr("workflow.toolbar.template", "Template") + "...", "",
        )
        for entry in self._template_registry.all_templates():
            self.template_combo.addItem(entry.display_name, entry.template_id)

    # ---------------- 槽 ---------------- #

    def _on_load_clicked(self) -> None:
        idx = self.template_combo.currentIndex()
        if idx <= 0:
            return
        template_id = self.template_combo.itemData(idx)
        if template_id:
            self.load_template(template_id)

    def _on_zoom_changed(self, zoom: float) -> None:
        percent = int(zoom * 100)
        self.zoom_label.setText(f"{percent}%")

    def _on_graph_changed(self) -> None:
        has_nodes = self.adapter.view.node_count > 0
        # 只在画布模式下跟随 graph 切换空态/画布；表单/模板库模式不被
        # 静默的 adapter.build_from_graph 打断。
        if self._view_mode == "canvas":
            if has_nodes:
                self._stack.setCurrentWidget(self.adapter.view)
            else:
                self._stack.setCurrentWidget(self.empty_hint_widget)
        # P9：Run 按钮随 graph 状态使能（Cancel 由 controller 在 run 进行时控制）
        self.run_button.setEnabled(has_nodes)
        self.form_view_button.setEnabled(has_nodes)
        self.canvas_view_button.setEnabled(has_nodes)

    def _on_nodes_moved(self, node_ids: list, old_positions: list) -> None:
        """节点拖拽结束（view.nodes_moved）：更新 current_graph + push undo。

        node_ids / old_positions 是 view 在 release 时比对出的移动节点与旧位置。
        新位置从 item.pos() 读（Qt 原生拖拽已移动 item）。
        """
        if not node_ids or self._current_graph is None:
            return
        # 旧位置快照（id→(x,y)），用于 undo 闭包
        old_map = {
            nid: (ox, oy) for nid, (ox, oy) in zip(node_ids, old_positions)
        }
        # 新位置（从视觉读）
        new_map = {}
        for nid in node_ids:
            item = self.adapter.view.get_node_item(nid)
            if item is not None:
                new_map[nid] = (item.x(), item.y())
        if not new_map:
            return
        # 应用到 graph（冻结 replace）
        graph_before = self._current_graph
        self._current_graph = self._edit.move_nodes(graph_before, new_map)

        # push undo（闭包同时改 graph + 视觉）
        from src.shared.node_canvas.undo_stack import EditAction

        def _apply(moves):
            # redo：设到 moves（新位置）
            if self._current_graph is not None:
                self._current_graph = self._edit.move_nodes(self._current_graph, moves)
            for nid, (nx, ny) in moves.items():
                self.adapter.set_node_position(nid, nx, ny)

        def _undo():
            _apply(old_map)
        def _redo():
            _apply(new_map)

        self.undo_stack.push(EditAction(
            description="move nodes",
            undo_fn=_undo, redo_fn=_redo,
        ))

    def _on_node_connected(
        self, from_node: str, from_port: str, to_node: str, to_port: str,
    ) -> None:
        """P10.2c：view.node_connected → graph_edit.add_edge + adapter 加连线视觉。"""
        if self._current_graph is None:
            return
        try:
            self._current_graph = self._edit.add_edge(
                self._current_graph, from_node, from_port, to_node, to_port,
            )
        except ValueError:
            # 重复边 / 类型不符 / ghost：忽略（view 已做类型校验，这里兜底）
            return
        self.adapter.add_connection_visual(from_node, from_port, to_node, to_port)
        # AUD-24004：加边也产生 undo 动作（此前只有移动入栈，Ctrl+Z 会错误
        # 回退更早的移动）。
        self._push_edge_action(from_node, from_port, to_node, to_port)

    def _push_edge_action(
        self, from_node: str, from_port: str, to_node: str, to_port: str,
    ) -> None:
        """AUD-24004：加边动作入撤销栈（undo=删边+删视觉；redo=重建）。"""
        from src.shared.node_canvas.undo_stack import EditAction

        def _add() -> None:
            if self._current_graph is None:
                return
            try:
                self._current_graph = self._edit.add_edge(
                    self._current_graph, from_node, from_port, to_node, to_port,
                )
            except ValueError:
                return  # 重复边（重复 redo）：保持一致即可
            self.adapter.add_connection_visual(
                from_node, from_port, to_node, to_port,
            )

        def _remove() -> None:
            if self._current_graph is None:
                return
            self._current_graph = self._edit.remove_edge(
                self._current_graph, from_node, from_port, to_node, to_port,
            )
            self.adapter.remove_connection_visual(
                from_node, from_port, to_node, to_port,
            )

        self.undo_stack.push(EditAction(
            description="connect nodes",
            undo_fn=_remove, redo_fn=_add,
        ))

    def _on_node_selected(self, node_id: str) -> None:
        """P10.2c：选中节点 → Inspector 加载参数。"""
        if self._current_graph is None:
            return
        node = self._current_graph.node(node_id)
        if node is None:
            return
        spec = self._registry.get(node.spec_id)
        if spec is None:
            return
        self.inspector.set_node(node_id, spec, dict(node.params))

    def _on_parameter_changed(self, node_id: str, key: str, value: object) -> None:
        """P10.2c：Inspector/表单改参数 → graph_edit.set_node_param + 跨视图同步。"""
        if self._current_graph is None:
            return
        node = self._current_graph.node(node_id)
        if node is None:
            return
        old_value = node.params.get(key)
        try:
            new_graph = self._edit.set_node_param(
                self._current_graph, node_id, key, value,
            )
        except (ValueError, KeyError):
            return
        self._current_graph = new_graph
        # AUD-24005：入图成功后向另一视图广播刷新（画布 Inspector 与表单页
        # 各持控件快照，不同步会出现显示值 ≠ Run 执行值）。
        self._broadcast_param_refresh(node_id, key, value)
        # AUD-24004：改参数也产生 undo 动作。
        self._push_param_action(node_id, key, old_value, value)

    def _broadcast_param_refresh(
        self, node_id: str, key: str, value: object,
    ) -> None:
        """AUD-24005：把最新参数值刷进画布 Inspector 与表单页各节控件。

        :class:`WorkflowInspector.refresh_param` 内部带 ``_refreshing``
        状态锁，刷新期间不会再回发 ``parameter_changed``（防递归信号）。
        """
        for inspector in (self.inspector, *self.form_page._inspectors):
            inspector.refresh_param(node_id, key, value)

    def _push_param_action(
        self, node_id: str, key: str, old_value: object, new_value: object,
    ) -> None:
        """AUD-24004：参数修改入撤销栈（undo/redo 同步 graph + 两视图控件）。"""
        from src.shared.node_canvas.undo_stack import EditAction

        def _apply(val: object) -> None:
            if self._current_graph is None:
                return
            try:
                self._current_graph = self._edit.set_node_param(
                    self._current_graph, node_id, key, val,
                )
            except (ValueError, KeyError):
                return
            self._broadcast_param_refresh(node_id, key, val)

        self.undo_stack.push(EditAction(
            description="set node param",
            undo_fn=lambda: _apply(old_value),
            redo_fn=lambda: _apply(new_value),
        ))

    def _on_add_node_requested(self) -> None:
        """P10.2b：Tab → 弹节点搜索窗；选中 → 在画布中心加节点。"""
        if self._node_search_popup is None:
            from src.features.ai_workflow.ui.workflow_node_search import (
                WorkflowNodeSearchPopup,
            )
            self._node_search_popup = WorkflowNodeSearchPopup(
                self._registry, parent=self,
            )
            self._node_search_popup.node_selected.connect(self._add_node_from_search)
        self._node_search_popup.exec()

    def _add_node_from_search(self, spec_id: str) -> None:
        """搜索窗选中 → 在视口中心加一个新节点（graph + 视觉）。"""
        if self._current_graph is None:
            return
        spec = self._registry.get(spec_id)
        if spec is None:
            return
        import uuid
        from src.features.ai_workflow.core.workflow_models import WorkflowNode
        # 视口中心（scene 坐标）
        center = self.adapter.view.mapToScene(
            self.adapter.view.viewport().rect().center()
        )
        node = WorkflowNode(
            node_id=f"{spec_id.split('.')[-1]}-{uuid.uuid4().hex[:6]}",
            spec_id=spec_id,
            ui={"x": center.x(), "y": center.y()},
            params=dict(getattr(spec, "default_params", {}) or {}),
        )
        try:
            self._current_graph = self._edit.add_node(self._current_graph, node)
        except ValueError:
            return  # 重复 id（极罕见）
        self.adapter.add_node_visual(
            node.node_id, node.spec_id, x=center.x(), y=center.y(),
        )
        # AUD-24005：加节点后重建表单页（画布新节点须出现在参数表单中）。
        self.form_page.set_graph(self._current_graph)
        # AUD-24004：加节点也产生 undo 动作。
        self._push_add_node_action(node)

    def _push_add_node_action(self, node: Any) -> None:
        """AUD-24004：加节点入撤销栈（undo=删节点+视觉；redo=重建）。"""
        from src.shared.node_canvas.undo_stack import EditAction

        def _add() -> None:
            if self._current_graph is None:
                return
            try:
                self._current_graph = self._edit.add_node(self._current_graph, node)
            except ValueError:
                return  # 已存在（重复 redo）
            ui = node.ui if isinstance(node.ui, dict) else {}
            self.adapter.add_node_visual(
                node.node_id, node.spec_id,
                x=float(ui.get("x", 0.0) or 0.0), y=float(ui.get("y", 0.0) or 0.0),
            )
            self.form_page.set_graph(self._current_graph)

        def _remove() -> None:
            if self._current_graph is None:
                return
            self._current_graph = self._graph_without_node(
                self._current_graph, node.node_id,
            )
            self.adapter.remove_node_visual(node.node_id)
            self.form_page.set_graph(self._current_graph)

        self.undo_stack.push(EditAction(
            description="add node",
            undo_fn=_remove, redo_fn=_add,
        ))

    @staticmethod
    def _graph_without_node(graph: Any, node_id: str) -> Any:
        """从 graph 移除节点及其关联边（``add_node`` 的逆操作；冻结 replace）。"""
        edges = tuple(
            e for e in graph.edges
            if e.from_port.node_id != node_id and e.to_port.node_id != node_id
        )
        nodes = tuple(n for n in graph.nodes if n.node_id != node_id)
        return replace(graph, nodes=nodes, edges=edges)

    # ---------------- 公共 API ---------------- #

    @staticmethod
    def _make_readonly_text() -> QPlainTextEdit:
        text = QPlainTextEdit()
        text.setReadOnly(True)
        text.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        return text

    def set_context_snapshot(
        self, context: object | None, *, provider_label: str = "", status: str = "已读取"
    ) -> None:
        """Render a sanitized live context snapshot supplied by the controller."""
        getter = getattr(context, "to_public_dict", None)
        data = context if isinstance(context, dict) else (getter() if callable(getter) else {})
        labels = {
            "project": "工程", "timeline": "时间线", "brief": "Brief",
            "revision": "Revision", "assets": "素材", "provider": "Provider",
            "permission": "权限",
        }
        values = {
            "project": data.get("project_id", "—"),
            "timeline": data.get("timeline_id", "—"),
            "brief": f"{data.get('brief_id', '—')} / r{data.get('brief_revision', 0)}",
            "revision": data.get("base_revision", "—"),
            "assets": len(data.get("selected_asset_ids", ()) or ()),
            "provider": provider_label or data.get("profile_id", "—") or "—",
            "permission": data.get("permission", "—"),
        }
        for key, value in values.items():
            self.context_labels[key].setText(f"{labels[key]}: {value}")
        self.context_status_label.setText(f"状态: {status}")

    def set_plan_preview(
        self, preview: object | None, *, plan_hash: str = "",
        status: str = "preview_ready", can_approve: bool = False,
    ) -> None:
        """Show deterministic plan metadata without executing a workflow."""
        data = preview if isinstance(preview, dict) else {}
        plan_hash = plan_hash or str(data.get("plan_hash", "") or "")
        display = dict(data)
        if plan_hash:
            display["plan_hash"] = plan_hash
        self.plan_text.setPlainText(json.dumps(display, ensure_ascii=False, indent=2, default=str))
        nodes = data.get("nodes", [])
        write_key = "timeline" + "_write_requires_approval"
        diff = {
            "node_count": len(nodes),
            "edge_count": data.get("edge_count", 0),
            "write_requires_approval": bool(data.get(write_key, False)),
            "estimated_cost": data.get("estimated_cost", 0.0),
        }
        self.diff_text.setPlainText(json.dumps(diff, ensure_ascii=False, indent=2, default=str))
        evidence = data.get("scope", {})
        self.evidence_text.setPlainText(json.dumps({
            "analysis_snapshot_hash": evidence.get("analysis_snapshot_hash", ""),
            "selected_asset_ids": evidence.get("selected_asset_ids", []),
            "brief_id": evidence.get("brief_id", ""),
        }, ensure_ascii=False, indent=2, default=str))
        self.release_gate_text.setPlainText(
            "✓ 计划仅预览，尚未写入时间线\n"
            + ("⚠ 时间线写入需要独立审批\n" if diff["write_requires_approval"] else "✓ 当前节点无时间线写入\n")
            + f"预计成本: {diff['estimated_cost']}"
        )
        self.plan_status_label.setText(
            f"{status} · Plan {plan_hash[:12]}" if plan_hash else status
        )
        self.preview_plan_button.setEnabled(bool(self._current_graph))
        self.approval_button.setEnabled(bool(can_approve))
        if not can_approve:
            self.set_approval_state("")

    def set_approval_state(self, grant_id: str = "", *, status: str = "等待审批") -> None:
        """Update approval tab while keeping the grant opaque in UI text."""
        self._approval_grant_id = str(grant_id or "")
        self.approval_button.setEnabled(bool(grant_id))
        self.apply_plan_button.setEnabled(bool(grant_id))
        self.approval_text.setPlainText(
            f"{status}\n" + ("已获得宿主授权，可应用当前 plan。" if grant_id else "尚未获得宿主授权。")
        )

    def set_diagnostics(self, diagnostics: object) -> None:
        """Show sanitized diagnostics and focus the Approval tab."""
        rows = list(diagnostics or ()) if not isinstance(diagnostics, str) else [diagnostics]
        self.approval_text.setPlainText(json.dumps(rows, ensure_ascii=False, indent=2, default=str))
        self.plan_tabs.setCurrentWidget(self.approval_text)

    def set_artifact_details(self, details: object) -> None:
        """Show a safe artifact reference and focus the evidence workspace."""
        payload = details if isinstance(details, dict) else {"summary": str(details)}
        self.evidence_text.setPlainText(
            json.dumps(payload, ensure_ascii=False, indent=2, default=str)
        )
        self.plan_tabs.setCurrentWidget(self.evidence_text)

    def set_release_gate(self, checks: object) -> None:
        """Render verification facts; never infer export success from run success."""
        rows = checks if isinstance(checks, list) else [checks]
        self.release_gate_text.setPlainText(
            json.dumps(rows, ensure_ascii=False, indent=2, default=str)
        )

    def set_metrics(self, metrics: object) -> None:
        """Render runtime-derived rollout metrics without raw logs or prompts."""
        payload = (
            metrics.to_public_dict()
            if hasattr(metrics, "to_public_dict")
            else metrics if isinstance(metrics, dict) else {"summary": str(metrics)}
        )
        self.metrics_text.setPlainText(
            json.dumps(payload, ensure_ascii=False, indent=2, default=str)
        )

    @property
    def current_graph(self):
        """当前画布渲染的 WorkflowGraph（无则 None）。controller 据此编译。"""
        return self._current_graph

    def load_template(self, template_id: str) -> None:
        """加载模板 → 实例化 WorkflowGraph → 渲染画布 → 持有 graph。"""
        entry = self._template_registry.get(template_id)
        if entry is None:
            return
        graph = self._template_registry.instantiate(template_id)
        self._current_template_id = str(template_id)
        self._set_current_graph(graph, skill_id=str(entry.skill_id or ""))

    def load_graph(self, graph, skill_id: str = "") -> None:
        """恢复一个已保存的 WorkflowGraph（工程加载 / 持久化还原入口）。

        与 ``load_template`` 同构但不经模板实例化；画布渲染 + Run 使能。
        """
        self._current_template_id = ""
        self._set_current_graph(graph, skill_id=skill_id)

    def _set_current_graph(self, graph, *, skill_id: str) -> None:
        self._current_graph = graph
        self._current_skill_id = skill_id
        self._approval_grant_id = ""
        self.adapter.build_from_graph(graph)
        self.preview_plan_button.setEnabled(True)
        self.undo_stack.clear()
        # 默认落参数表单页（画布保留为高级编辑视图，已同步渲染可随时切换）
        self.form_page.set_graph(graph)
        self.show_form_view()

    def clear_canvas(self) -> None:
        """清空画布 + 重置 current_graph（返回模板库）。"""
        self._current_graph = None
        self._current_skill_id = ""
        self._current_template_id = ""
        self._approval_grant_id = ""
        self.undo_stack.clear()
        self.adapter.clear_all()
        self.form_page.set_graph(None)
        self.show_gallery_view()
        self.set_plan_preview({}, status="暂无计划", can_approve=False)
        # clear_all 触发 graph_changed → run_button 禁用

    # ---------------- 视图模式 ---------------- #

    @property
    def view_mode(self) -> str:
        """当前视图模式：gallery / form / canvas。"""
        return self._view_mode

    def show_gallery_view(self) -> None:
        self._view_mode = "gallery"
        self._stack.setCurrentWidget(self.gallery_page)

    def show_form_view(self) -> None:
        if self._current_graph is None:
            self.show_gallery_view()
            return
        self._view_mode = "form"
        self._stack.setCurrentWidget(self.form_page)

    def show_canvas_view(self) -> None:
        if self._current_graph is None:
            self.show_gallery_view()
            return
        self._view_mode = "canvas"
        self._stack.setCurrentWidget(self.adapter.view)

    @property
    def current_skill_id(self) -> str:
        """Skill declared by the loaded official template."""
        return self._current_skill_id

    @property
    def current_template_id(self) -> str:
        """模板来源标记（load_template 时记录；load_graph 恢复时为空）。"""
        return self._current_template_id

    def set_run_drawer(self, state) -> None:
        """把运行活动流状态灌进 Run Drawer（带 ``_updating`` 锁防递归）。

        仿 ``ai_edit_assistant_panel.py:795`` 的 setter 模式。controller 聚合
        ``RunRecord`` → ``RunDrawerState`` 后调用本方法。AUD-24006：``_updating``
        在此被真实读取——嵌套的 ``set_run_drawer``（通常由 drawer 状态更新
        反向触发 controller 再灌回）直接忽略；``set_state`` 期间程序性触发的
        ``artifact_clicked`` 也被 ``_on_artifact_clicked`` 拦截。
        """
        if self._updating:
            logger.debug("set_run_drawer 重入被 _updating 锁拦截")
            return
        self._updating = True
        try:
            self.run_drawer.set_state(state)
        finally:
            self._updating = False

    def _on_artifact_clicked(self, artifact_id: str) -> None:
        """artifact 点击转发（AUD-24006：``_updating`` 期间丢弃）。

        真实用户点击只能在事件循环轮转后到达（``_updating`` 已复位）；
        ``set_state`` 重绘期间程序性触发的信号一律丢弃，阻断
        drawer → panel → drawer 的信号递归环。
        """
        if self._updating:
            logger.debug("artifact_clicked 在 _updating 期间被丢弃: %r", artifact_id)
            return
        self.artifact_requested.emit(artifact_id)

    def set_running(self, running: bool) -> None:
        """run 进行 / 结束时切换 Run / Cancel 按钮可用性。

        - ``running=True``：Run 禁用，Cancel 使能（用户可中断）。
        - ``running=False``：恢复据 graph 状态的 Run 使能，Cancel 禁用。
        """
        self.cancel_button.setEnabled(running)
        if running:
            self.run_button.setEnabled(False)
        else:
            self.run_button.setEnabled(self.adapter.view.node_count > 0)
