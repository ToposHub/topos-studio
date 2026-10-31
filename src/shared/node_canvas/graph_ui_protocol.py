"""P5.6 节点画布 UI 协议（plan §10.7.1 / §2.1）。

> 先复用 Composite 的画布视觉/交互，抽取 ``NodeCanvasBase``；不复用 GPU
> ``NodeGraph``。

本模块定义 **画布 UI 的最小 Protocol**（``GraphUiProtocol``）+ 视觉令牌
（``VisualTokens``），让 AI Workflow 的画布与 Composite 的画布共享同一交互
契约，而**不**触碰 GPU ``NodeGraph``（ModernGL 渲染栈）。

设计边界（红线，plan §10.7.1 / §1.4）：

- **Protocol 而非基类**：避免现在强行重构 5000+ 行的 ``NodeEditorWidget``
  （高风险）。Protocol 定义"画布必须做什么"；Composite 与 AI Workflow 各自
  实现。Composite 后续可渐进迁移到 ``NodeCanvasBase``（plan §10.7.1 M1）。
- **不含 GPU / ModernGL**：本模块纯数据 + Protocol；不 import ``composite.core``
  / ``moderngl`` / 任何 renderer。AI Workflow 画布端口显示 artifact 类型/状态，
  不显示 GPU thumbnail。
- **信号契约**：``GraphUiSignals`` 列出画布对外必须发出的信号（节点增删、连线、
  选择、缩放、运行请求）；Composite 现有 ``pyqtSignal`` 已满足（探索结论 §1c）。

与既有模块的关系：

- AI Workflow 的 ``WorkflowCanvasAdapter``（M5+ UI 批次）实现 ``GraphUiProtocol``，
  从 :class:`WorkflowGraph`（P5.1）渲染节点/连线。
- Composite 的 ``NodeEditorWidget`` 后续实现本 Protocol（渐进迁移，不破坏现状）。
- ``VisualTokens`` 统一节点/端口/连线的视觉常量（颜色/圆角/端口大小/状态色）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional, Protocol, Tuple, runtime_checkable


# ---------------------------------------------------------------------- #
# VisualTokens —— 视觉令牌（节点/端口/连线/状态色）
# ---------------------------------------------------------------------- #


@dataclass(frozen=True)
class NodeVisualToken:
    """单个节点分类的视觉令牌。"""

    category: str           # NodeCategory value
    color: str = "#6b7280"  # 节点标题色
    icon: str = ""          # 图标 key


@dataclass(frozen=True)
class PortVisualToken:
    """端口视觉令牌（按 artifact type 着色）。"""

    artifact_type: str
    color: str = "#9ca3af"
    shape: str = "circle"   # circle / square / diamond
    radius: int = 6


@dataclass(frozen=True)
class StatusColor:
    """节点运行状态色（plan §6.1 节点状态，含 queued/skipped）。"""

    idle: str = "#9ca3af"           # 灰
    queued: str = "#64748b"         # 深灰蓝（已排队未执行）
    running: str = "#3b82f6"        # 蓝
    cached: str = "#10b981"         # 绿
    succeeded: str = "#22c55e"      # 绿
    failed: str = "#ef4444"         # 红
    cancelled: str = "#f59e0b"      # 橙
    waiting_input: str = "#a855f7"  # 紫
    skipped: str = "#cbd5e1"        # 浅灰（本轮未执行）


@dataclass(frozen=True)
class VisualTokens:
    """画布视觉常量集合（AI Workflow 与 Composite 共享）。

    所有值是数据（字符串/数字），不含 Qt 对象；UI 层据此创建 QPen/QBrush。
    """

    node_corner_radius: int = 6
    node_min_width: int = 140
    node_header_height: int = 28
    port_radius: int = 6
    port_spacing: int = 20
    connection_width: int = 2
    grid_size: int = 20
    zoom_min: float = 0.2
    zoom_max: float = 3.0

    status: StatusColor = field(default_factory=StatusColor)

    # 节点分类 → 视觉令牌
    node_tokens: Tuple[NodeVisualToken, ...] = (
        NodeVisualToken("input", "#3b82f6", "input"),
        NodeVisualToken("analysis", "#8b5cf6", "analysis"),
        NodeVisualToken("edit", "#ec4899", "edit"),
        NodeVisualToken("text", "#06b6d4", "text"),
        NodeVisualToken("generation", "#f59e0b", "generation"),
        NodeVisualToken("agent", "#10b981", "agent"),
        NodeVisualToken("approval", "#ef4444", "approval"),
        NodeVisualToken("output", "#6366f1", "output"),
        NodeVisualToken("control", "#6b7280", "control"),
    )

    # artifact type → 端口视觉令牌
    port_tokens: Tuple[PortVisualToken, ...] = (
        PortVisualToken("asset_ref", "#3b82f6"),
        PortVisualToken("asset_set", "#3b82f6"),
        PortVisualToken("timeline_ref", "#6366f1"),
        PortVisualToken("transcript_ref", "#8b5cf6"),
        PortVisualToken("analysis_ref", "#a855f7"),
        PortVisualToken("subtitle_track_ref", "#ec4899"),
        PortVisualToken("text_document", "#06b6d4"),
        PortVisualToken("edit_plan_ref", "#f59e0b"),
        PortVisualToken("media_generation_ref", "#10b981"),
        PortVisualToken("report", "#6b7280"),
        PortVisualToken("approval_token", "#ef4444", "diamond"),
        PortVisualToken("selection_set", "#14b8a6"),
    )

    def node_token(self, category: str) -> NodeVisualToken:
        for t in self.node_tokens:
            if t.category == category:
                return t
        return NodeVisualToken(category=category)

    def port_token(self, artifact_type: str) -> PortVisualToken:
        for t in self.port_tokens:
            if t.artifact_type == artifact_type:
                return t
        return PortVisualToken(artifact_type=artifact_type)

    def status_color(self, status: str) -> str:
        mapping = {
            "idle": self.status.idle, "queued": self.status.queued,
            "running": self.status.running,
            "cached": self.status.cached, "succeeded": self.status.succeeded,
            "failed": self.status.failed, "cancelled": self.status.cancelled,
            "waiting_input": self.status.waiting_input,
            "skipped": self.status.skipped,
        }
        return mapping.get(status, self.status.idle)


# 默认视觉令牌（单例）
DEFAULT_VISUAL_TOKENS = VisualTokens()


# ---------------------------------------------------------------------- #
# GraphUiSignals —— 画布对外信号契约（plan §1c）
# ---------------------------------------------------------------------- #


@dataclass(frozen=True)
class GraphUiSignals:
    """画布必须发出的信号名集合（Protocol 契约文档）。

    Composite 的 ``NodeEditorWidget`` 已有同名 ``pyqtSignal``（探索 §1c）。
    AI Workflow 的 ``WorkflowCanvasAdapter`` 必须实现等价信号。
    """

    NODE_ADDED = "node_added"
    NODE_REMOVED = "node_removed"
    NODE_CONNECTED = "node_connected"          # 连线建立
    NODE_DISCONNECTED = "node_disconnected"    # 连线断开
    NODE_SELECTED = "node_selected"
    NODES_SELECTED = "nodes_selected"          # 多选
    SELECTION_CLEARED = "selection_cleared"
    NODE_DOUBLE_CLICKED = "node_double_clicked"
    NODES_MOVED = "nodes_moved"
    GRAPH_CHANGED = "graph_changed"
    ZOOM_CHANGED = "zoom_changed"
    UNDO_REQUESTED = "undo_requested"
    REDO_REQUESTED = "redo_requested"
    RUN_REQUESTED = "run_requested"            # 点击 Run → WorkflowCompiler
    CANCEL_REQUESTED = "cancel_requested"
    ASSETS_DROPPED = "assets_dropped"          # 拖放素材到画布

    ALL: Tuple[str, ...] = (
        NODE_ADDED, NODE_REMOVED, NODE_CONNECTED, NODE_DISCONNECTED,
        NODE_SELECTED, NODES_SELECTED, SELECTION_CLEARED,
        NODE_DOUBLE_CLICKED, NODES_MOVED, GRAPH_CHANGED, ZOOM_CHANGED,
        UNDO_REQUESTED, REDO_REQUESTED, RUN_REQUESTED, CANCEL_REQUESTED,
        ASSETS_DROPPED,
    )


# ---------------------------------------------------------------------- #
# GraphUiProtocol —— 画布最小 Protocol
# ---------------------------------------------------------------------- #


@runtime_checkable
class GraphUiProtocol(Protocol):
    """节点画布的最小 Protocol（plan §10.7.1）。

    AI Workflow 的 ``WorkflowCanvasAdapter`` 与 Composite 的 ``NodeEditorWidget``
    都应满足本 Protocol。本 Protocol **不含** GPU 渲染方法（``preview_requested`` /
    ``thumbnail`` 等）—— 那是 Composite 私有，AI Workflow 不复用。

    实现注意：本 Protocol 是 ``runtime_checkable``，但只检查方法存在性，不检查
    签名（Python Protocol 限制）。host 装配侧负责完整接线。
    """

    def build_from_graph(self, graph: Any) -> None:
        """从 graph 数据模型重建画布视觉（节点/连线/布局）。

        Args:
            graph: :class:`WorkflowGraph`（AI Workflow）或 Composite 的 ``NodeGraph``。
        """
        ...

    def clear_all(self) -> None:
        """清空画布（节点/连线/backdrop/sticky）。"""
        ...

    def add_node_visual(self, node_id: str, spec_id: str, *,
                        x: float = 0.0, y: float = 0.0) -> None:
        """添加节点视觉。"""
        ...

    def remove_node_visual(self, node_id: str) -> None:
        """移除节点视觉。"""
        ...

    def add_connection_visual(self, from_node: str, from_port: str,
                              to_node: str, to_port: str) -> None:
        """添加连线视觉。"""
        ...

    def remove_connection_visual(self, from_node: str, from_port: str,
                                 to_node: str, to_port: str) -> None:
        """移除连线视觉。"""
        ...

    def set_node_status(self, node_id: str, status: str) -> None:
        """设置节点运行状态色（plan §6.1 节点状态）。"""
        ...

    def selected_node_ids(self) -> Tuple[str, ...]:
        """返回当前选中的节点 ID。"""
        ...

    def zoom_to(self, zoom: float) -> None:
        """缩放到指定倍率。"""
        ...


# ---------------------------------------------------------------------- #
# NodeCanvasBase —— 抽象基类（可选实现路径）
# ---------------------------------------------------------------------- #


class NodeCanvasBase:
    """节点画布抽象基类（plan §10.7.1 ``NodeCanvasBase``）。

    本类是**可选**的实现路径：Composite 后续可渐进迁移 ``NodeEditorWidget`` 继承
    本类；AI Workflow 的 ``WorkflowCanvasAdapter`` 也可继承本类。当前阶段
    （P5.6）只定义契约 + 共享视觉令牌，**不**强制重构 Composite。

    红线：本类不持有 GPU 资源；不 import ``moderngl`` / ``composite.core``。
    """

    def __init__(self, *, visual_tokens: Optional[VisualTokens] = None) -> None:
        self._tokens = visual_tokens or DEFAULT_VISUAL_TOKENS

    @property
    def visual_tokens(self) -> VisualTokens:
        return self._tokens

    # 子类必须实现 GraphUiProtocol 方法（本基类不提供默认实现，避免误导）
    def build_from_graph(self, graph: Any) -> None:
        raise NotImplementedError

    def clear_all(self) -> None:
        raise NotImplementedError

    def add_node_visual(self, node_id: str, spec_id: str, *,
                        x: float = 0.0, y: float = 0.0) -> None:
        raise NotImplementedError

    def remove_node_visual(self, node_id: str) -> None:
        raise NotImplementedError

    def add_connection_visual(self, from_node: str, from_port: str,
                              to_node: str, to_port: str) -> None:
        raise NotImplementedError

    def remove_connection_visual(self, from_node: str, from_port: str,
                                 to_node: str, to_port: str) -> None:
        raise NotImplementedError

    def set_node_status(self, node_id: str, status: str) -> None:
        raise NotImplementedError

    def selected_node_ids(self) -> Tuple[str, ...]:
        raise NotImplementedError

    def zoom_to(self, zoom: float) -> None:
        raise NotImplementedError


__all__ = [
    "NodeVisualToken",
    "PortVisualToken",
    "StatusColor",
    "VisualTokens",
    "DEFAULT_VISUAL_TOKENS",
    "GraphUiSignals",
    "GraphUiProtocol",
    "NodeCanvasBase",
]
