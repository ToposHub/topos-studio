"""P8.4 WorkflowCanvasAdapter —— NodeCanvasBase 子类（实现 GraphUiProtocol）。

> AI Workflow 的 ``WorkflowCanvasAdapter`` 也可继承本类（graph_ui_protocol.py:254）。

桥接 :class:`WorkflowGraph` 数据模型与 :class:`WorkflowCanvasView` 视觉层：

- 组合一个 :class:`WorkflowCanvasView`（has-a；Adapter 本身不是 QWidget）。
- 持有 :class:`NodeSpecRegistry`（查 spec → category → visual token）。
- 实现 :class:`GraphUiProtocol` 全部 9 方法（``build_from_graph`` / ``clear_all`` /
  ``add_node_visual`` / ``remove_node_visual`` / ``add_connection_visual`` /
  ``remove_connection_visual`` / ``set_node_status`` / ``selected_node_ids`` /
  ``zoom_to``）。

红线（plan §1.4.1 / §1.4.3 / §0 / graph_ui_protocol.py）：

- **不引入** ``moderngl`` / ``composite.core``。
- **不传 GPU texture**（端口 artifact_type 是字符串）。
- **不直接写 timeline**（adapter 只渲染 + 转发信号；运行经 WorkflowCompiler）。
- spec_id 不在 registry → 用默认 control token 渲染（不崩溃）。
"""
from __future__ import annotations

from typing import Any, Optional, Tuple

from src.shared.node_canvas.graph_ui_protocol import (
    NodeCanvasBase,
    VisualTokens,
)
from src.features.ai_workflow.ui.workflow_canvas_view import WorkflowCanvasView
from src.features.ai_workflow.ui.workflow_node_item import WorkflowNodeItem
from src.features.ai_workflow.ui.workflow_connection_item import (
    WorkflowConnectionItem,
)


class WorkflowCanvasAdapter(NodeCanvasBase):
    """AI Workflow 画布 adapter（has-a WorkflowCanvasView）。

    实现全部 9 个 :class:`GraphUiProtocol` 方法。host 通过 :attr:`view` 访问
    ``pyqtSignal``（``run_requested`` / ``node_selected`` 等）。
    """

    def __init__(
        self,
        *,
        registry,
        visual_tokens: Optional[VisualTokens] = None,
    ) -> None:
        super().__init__(visual_tokens=visual_tokens)
        self._registry = registry
        self._view = WorkflowCanvasView(visual_tokens=self._tokens)

    # ---------------- 属性 ---------------- #

    @property
    def view(self) -> WorkflowCanvasView:
        """底层画布视图（host 通过它访问 pyqtSignal）。"""
        return self._view

    # ---------------- GraphUiProtocol: 批量重建 ---------------- #

    @staticmethod
    def _safe_float(value: Any, default: float = 0.0) -> float:
        """AUD-23912：容错 float 转换（持久化 JSON 的坐标可为 None/非法串）。

        ``float(None)`` 抛 TypeError、``float("abc")`` 抛 ValueError——坏值
        回退 default，不让整个图加载中断（「外部数据须验证」铁律）。
        """
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    def build_from_graph(self, graph: Any) -> None:
        """从 WorkflowGraph 重建画布（节点/连线/布局）。"""
        self.clear_all()
        if graph is None:
            return
        # 渲染节点（带位置；坐标经 _safe_float 防护坏值）
        for node in getattr(graph, "nodes", ()):
            if isinstance(node.ui, dict):
                x = self._safe_float(node.ui.get("x", 0.0))
                y = self._safe_float(node.ui.get("y", 0.0))
            else:
                x = y = 0.0
            self.add_node_visual(node.node_id, node.spec_id, x=x, y=y)
        # 渲染连线
        for edge in getattr(graph, "edges", ()):
            self.add_connection_visual(
                edge.from_port.node_id, edge.from_port.port,
                edge.to_port.node_id, edge.to_port.port,
            )
        # add_node_item 只发 node_added，不发 graph_changed；build 结束显式通知
        # 一次，让 host（panel）据此切换空态/Run 按钮使能（P9）。
        self._view.graph_changed.emit()

    def clear_all(self) -> None:
        """清空画布。"""
        self._view.clear_all()

    # ---------------- GraphUiProtocol: 节点增删 ---------------- #

    def add_node_visual(
        self, node_id: str, spec_id: str, *,
        x: float = 0.0, y: float = 0.0,
    ) -> None:
        """添加节点视觉（查 spec → category → visual token）。"""
        node_spec = self._registry.get(spec_id)
        # spec 不在 registry → 用 placeholder spec（category=control）
        if node_spec is None:
            node_spec = _PlaceholderSpec(spec_id)
        item = WorkflowNodeItem(
            node_id=node_id, spec_id=spec_id,
            node_spec=node_spec, visual_tokens=self._tokens,
        )
        item.set_position(x, y)
        self._view.add_node_item(item)

    def remove_node_visual(self, node_id: str) -> None:
        """移除节点（+关联连线，由 view 处理）。"""
        self._view.remove_node_item(node_id)

    # ---------------- GraphUiProtocol: 连线增删 ---------------- #

    def add_connection_visual(
        self, from_node: str, from_port: str,
        to_node: str, to_port: str,
    ) -> None:
        """添加连线视觉（定位端口 → 建 ConnectionItem）。"""
        src_item = self._view.get_node_item(from_node)
        dst_item = self._view.get_node_item(to_node)
        if src_item is None or dst_item is None:
            return  # 节点不存在，跳过
        # 推断 artifact_type（优先从源 output，退化从 dst input）
        artifact_type = (
            src_item.output_artifact_types.get(from_port)
            or dst_item.input_artifact_types.get(to_port)
            or "asset_ref"
        )
        conn = WorkflowConnectionItem(
            from_node_id=from_node, from_port=from_port,
            to_node_id=to_node, to_port=to_port,
            src_port=src_item, dst_port=dst_item,
            artifact_type=artifact_type, visual_tokens=self._tokens,
        )
        self._view.add_connection_item(conn)
        # P10.1c：注册连线到两端节点（拖动节点时连线跟随）。
        src_item.register_connection(conn)
        dst_item.register_connection(conn)

    def remove_connection_visual(
        self, from_node: str, from_port: str,
        to_node: str, to_port: str,
    ) -> None:
        """移除连线视觉。"""
        # P10.1c：先解除两端节点的连线注册，再从 view 移除。
        key = (from_node, from_port, to_node, to_port)
        conn = self._view.connection_items.get(key)
        if conn is not None:
            src_item = self._view.get_node_item(from_node)
            dst_item = self._view.get_node_item(to_node)
            if src_item is not None:
                src_item.unregister_connection(conn)
            if dst_item is not None:
                dst_item.unregister_connection(conn)
        self._view.remove_connection_item(key)

    # ---------------- GraphUiProtocol: 状态 / 选中 / 缩放 ---------------- #

    def set_node_status(self, node_id: str, status: str) -> None:
        item = self._view.get_node_item(node_id)
        if item is not None:
            item.set_status(status)

    def selected_node_ids(self) -> Tuple[str, ...]:
        return self._view.selected_node_ids()

    def zoom_to(self, zoom: float) -> None:
        self._view.zoom_to(zoom)

    def set_node_position(self, node_id: str, x: float, y: float) -> None:
        """设置节点视觉位置（undo/redo 内部用）。

        委托 view.set_node_position（触发 itemChange → 连线跟随）；**不 emit
        nodes_moved**（避免 undo/redo 闭包改视觉时触发新编辑 → 循环）。
        """
        self._view.set_node_position(node_id, x, y)


# ---------------------------------------------------------------------- #
# _PlaceholderSpec —— 未知 spec_id 的占位（category=control，不崩溃）
# ---------------------------------------------------------------------- #


class _PlaceholderSpec:
    """未知 spec_id 的占位 spec（让 WorkflowNodeItem 不崩溃）。"""

    def __init__(self, spec_id: str) -> None:
        self.spec_id = spec_id
        self.display_name = spec_id.split(".")[-1] if spec_id else "Unknown"
        self.category = "control"
        self.inputs: tuple = ()
        self.outputs: tuple = ()

    @property
    def input_ports(self) -> tuple:
        return self.inputs

    @property
    def output_ports(self) -> tuple:
        return self.outputs
