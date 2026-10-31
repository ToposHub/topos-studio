"""
Node Editor Actions - Unified action entry point for node graph operations.

All modifications to the node graph should go through this module to ensure:
- Consistent undo/redo behavior
- Signal-driven architecture compatibility
- Single source of truth for operation logic

Usage:
    from .node_editor_actions import NodeEditorActions

    # In CompositePage or NodeEditorWidget initialization:
    actions = NodeEditorActions(node_editor, node_graph)

    # Add a node:
    actions.add_node("blur", x=100, y=200)

    # Delete selected nodes:
    actions.delete_selected_nodes()

    # Connect ports:
    actions.connect(src_node_id, "output", dst_node_id, "input")
"""

from __future__ import annotations

from typing import TYPE_CHECKING, List, Optional, Dict, Any, Tuple
import logging
import uuid
import copy

if TYPE_CHECKING:
    from src.features.composite.core.node_engine import NodeGraph
    from src.features.composite.nodes.base_node import Node
    from src.features.composite.utils.preset_manager import PresetManager
    from .node_editor_widget import NodeEditorWidget

logger = logging.getLogger(__name__)


class NodeEditorActions:
    """Unified entry point for node editor operations.

    This class wraps NodeEditorWidget operations and ensures all actions
    emit appropriate signals for undo/redo tracking in CompositePage.

    Architecture:
    - NodeEditorWidget handles visual interactions and emits signals
    - NodeEditorActions provides explicit API for programmatic operations
    - CompositePage listens to signals and pushes UndoActions

    This design maintains the signal-driven architecture while providing
    a clean, documented API for all operations.
    """

    # Node types that canvas delete/cut/duplicate operations must never
    # touch (AUD-10408/AUD-10500): identity comes from the graph's
    # node_type, never from the user-editable display title.
    _PROTECTED_NODE_TYPES = ("color_input", "output")

    def __init__(self, editor: "NodeEditorWidget", graph: "NodeGraph") -> None:
        """Initialize with references to editor and graph.

        Args:
            editor: The NodeEditorWidget instance
            graph: The NodeGraph data model
        """
        self._editor = editor
        self._graph = graph

    def _is_protected_node(self, node_id: str) -> bool:
        """Return True if ``node_id`` is a COLOR/Output node.

        AUD-10408: protection is judged by ``node.node_type`` from the graph.
        The delete/cut/duplicate filters used to compare the display title
        (``node_title not in ("COLOR", "Output")``), which a user rename
        could bypass — deleting the Output node breaks the render chain.
        """
        node = self._graph.get_node(node_id)
        return bool(node) and node.node_type in self._PROTECTED_NODE_TYPES

    # ── Node Operations ──

    def add_node(self, node_type: str, x: float, y: float,
                 parameters: Optional[Dict[str, Any]] = None) -> Optional["Node"]:
        """Add a new node at the specified position.

        Emits: node_added(node_id, node_type)

        Args:
            node_type: Type of node to create (e.g., "blur", "grade")
            x: X position in scene coordinates
            y: Y position in scene coordinates
            parameters: Optional initial parameters

        Returns:
            The created Node instance, or None if type not found
        """
        from src.features.composite.core.node_engine import NODE_REGISTRY

        node_cls = NODE_REGISTRY.get(node_type)
        if not node_cls:
            return None

        node = node_cls()
        node.position = (x, y)
        if parameters:
            for k, v in parameters.items():
                node.set_parameter(k, v)

        # 命名升级（P1）：分配实例序号（Camera1/Light2…），需在 add_node 前查同类计数
        from src.features.composite.core.node_engine import assign_instance_index
        assign_instance_index(node, self._graph)

        self._graph.add_node(node)
        self._editor.add_node_visual(node)
        self._editor.node_added.emit(node.id, node_type)
        self._editor.graph_changed.emit()

        return node

    def create_3d_scene_with_children(self, x: float, y: float,
                                      model_path: Optional[str] = None,
                                      base_id: Optional[str] = None) -> Optional["Node"]:
        """创建 Scene3D + 子节点分裂图（P2：与迁移路径一致）。

        拖拽 3D 模型时调用：建 Scene3D 主节点，并 spawn Model3D 连接到 scene
        输入。默认 Camera/Light 由 Scene3D fallback 提供；默认 Geo3D 不创建，避免
        primitive 与导入模型混在一起。

        AUD-10407: 整个拖拽是一个事务。旧实现为每个子节点 emit ``node_added``，
        页面逐个推 undo 并整图求值——一次拖拽产生 N 步 undo。现在所有节点
        （含 Scene3D 主节点）静默创建，连接直接写入图模型，最后一次性 emit
        ``nodes_pasted``：页面 begin_group 成组推 undo、末尾统一求值，
        Ctrl+Z 一步即可撤销整个分裂图。

        Args:
            x, y: Scene3D 主节点位置（子节点据此左侧偏移）。
            model_path: 可选模型路径；非空时同时建 Model3D 并连接 geometry_in。
            base_id: 可选主节点 id；None 自动生成。

        Returns:
            创建的 Scene3D Node（失败返回 None）。
        """
        from src.features.composite.core.graph_migration import spawn_3d_scene_children
        from src.features.composite.core.node_engine import (
            NODE_REGISTRY, assign_instance_index,
        )

        scene3d_cls = NODE_REGISTRY.get("scene3d")
        if scene3d_cls is None:
            return None

        # 1. 静默建 Scene3D 主节点（不 emit node_added —— 见 docstring）
        scene3d = scene3d_cls()
        scene3d.position = (x, y)
        scene3d_id = scene3d.id
        if base_id and self._rename_node_safe(scene3d.id, base_id):
            scene3d_id = base_id
        # 页面在 node_added 路径会用工程输出尺寸初始化 Scene3D（
        # CompositePage._initialize_new_3d_helper_node）；走汇总信号时在这里
        # 用同一来源（图的 Output 节点）补齐 width/height。
        output_size = self._graph_output_size()
        if output_size is not None:
            try:
                scene3d.set_parameter("width", output_size[0])
                scene3d.set_parameter("height", output_size[1])
            except Exception:
                params = getattr(scene3d, "parameters", None)
                if isinstance(params, dict):
                    params["width"] = output_size[0]
                    params["height"] = output_size[1]

        # 2. 准备 scene3d 参数（拖拽模型时带 model_path，触发工厂 spawn Model3D）
        scene3d_params: Dict[str, Any] = {}
        if model_path:
            scene3d_params["model_path"] = model_path
            scene3d_params["use_imported_model"] = True

        # 3. 用工厂算子节点 dict + 连接 dict（与迁移完全一致的拓扑）
        existing_ids = set(self._graph.nodes.keys())
        child_nodes, child_conns = spawn_3d_scene_children(
            scene3d_id=scene3d_id,
            scene3d_position=(x, y),
            scene3d_params=scene3d_params,
            existing_ids=existing_ids,
            include_model=bool(model_path),
            include_camera=not bool(model_path),
            include_light=not bool(model_path),
            include_geo=not bool(model_path),
        )

        # 4. 批量静默实例化全部节点 + 直连（无逐节点/逐线信号）
        created_data: List[Dict[str, Any]] = []
        self._editor._batch_mode = True
        try:
            assign_instance_index(scene3d, self._graph)
            self._graph.add_node(scene3d)
            self._editor.add_node_visual(scene3d)
            created_data.append(scene3d.to_dict())

            for cn in child_nodes:
                child_cls = NODE_REGISTRY.get(cn["type"])
                if child_cls is None:
                    continue
                child = child_cls(cn["id"])
                child.position = tuple(cn["position"])
                for k, v in cn["parameters"].items():
                    child.set_parameter(k, v)
                # 命名升级（P1）：spawn 子节点也分配序号
                assign_instance_index(child, self._graph)
                self._graph.add_node(child)
                self._editor.add_node_visual(child)
                created_data.append(child.to_dict())

            # 直连（不 emit node_connected）：页面的 nodes_pasted 处理器会
            # 读取图模型，把 pasted 集合内部的连接记入同一个 undo 组。
            for cc in child_conns:
                if self._graph.connect(cc["src_node_id"], cc["src_port_name"],
                                       cc["dst_node_id"], cc["dst_port_name"]):
                    self._editor.add_connection_visual(
                        cc["src_node_id"], cc["src_port_name"],
                        cc["dst_node_id"], cc["dst_port_name"])
        finally:
            self._editor._batch_mode = False

        # 5. 一次性汇总信号 → 一组 undo + 一次整图求值
        if created_data:
            self._editor.nodes_pasted.emit(created_data)
        self._editor.graph_changed.emit()
        return scene3d

    def _graph_output_size(self) -> Optional[Tuple[int, int]]:
        """从图的 Output 节点读取工程画布尺寸（width/height 参数）。

        与 CompositePage._project_output_size 的图内来源一致：Output 节点
        拥有合成工程分辨率，持久化 Scene3D 用它对齐渲染输出。
        """
        for node in list(self._graph.nodes.values()):
            if getattr(node, "node_type", "") != "output":
                continue
            params = getattr(node, "parameters", None) or {}
            try:
                width = int(params.get("width", 0) or 0)
                height = int(params.get("height", 0) or 0)
            except (TypeError, ValueError):
                continue
            if width > 0 and height > 0:
                return (width, height)
        return None

    def _rename_node_safe(self, old_id: str, new_id: str) -> bool:
        """重命名节点 id（若 new_id 不冲突），并同步所有引用。

        AUD-10404: 旧实现写入 ``node._id`` —— Node 上不存在该属性（``id`` 是
        普通属性），导致 graph 字典/连接已指向 new_id 而节点自身与其序列化
        仍是 old_id，且编辑器 ``_node_items`` 未重键，图/序列化/视觉全面脱节。
        现在统一改 ``node.id``、graph 字典 key、连接 src/dst、编辑器条目键、
        锁定集合与选中态。

        Args:
            old_id: 现有节点 id。
            new_id: 目标 id（冲突时跳过并返回 False）。

        Returns:
            True 表示已重命名；False 表示跳过（节点缺失/冲突/相同 id）。
        """
        if not new_id or new_id == old_id:
            return False
        if new_id in self._graph.nodes:
            return False  # 冲突则保留旧 id
        node = self._graph.get_node(old_id)
        if node is None:
            return False

        # 更新 graph 内部字典 key（与既有实现一致：直改 nodes 字典）
        nodes_map = self._graph.nodes
        nodes_map[new_id] = nodes_map.pop(old_id)
        node.id = new_id
        # 更新涉及该节点的连接（src/dst）
        for conn in self._graph.connections:
            if conn.src_node_id == old_id:
                conn.src_node_id = new_id
            if conn.dst_node_id == old_id:
                conn.dst_node_id = new_id

        # 视觉层同步重键（connect/add_connection_visual 均按 id 查 item）
        items = getattr(self._editor, "_node_items", None)
        if isinstance(items, dict) and old_id in items:
            items[new_id] = items.pop(old_id)
        locked = getattr(self._editor, "_locked_node_ids", None)
        if isinstance(locked, set) and old_id in locked:
            locked.discard(old_id)
            locked.add(new_id)
        if getattr(self._editor, "_selected_node_id", None) == old_id:
            self._editor._selected_node_id = new_id
        return True

    def delete_node(self, node_id: str) -> bool:
        """Delete a single node; upstream→downstream stays connected.

        AUD-10403: this method used to reconnect around the node (via
        ``_reconnect_around_node``) BEFORE notifying the page, so the page's
        removal-undo handler captured an already-empty connection list — one
        Ctrl+Z restored a bare node and the original cables were lost. The
        removal is now routed through the page's grouped batch-removal
        handler (the same transaction ``cut`` and ``delete_selected_nodes``
        use): CompositePage captures the still-intact connections first,
        then performs the engine-level ``remove_node_and_reconnect`` in the
        same undo group.

        Emits: nodes_about_to_be_removed([node_id]), nodes_batch_removed([node_id])

        Args:
            node_id: ID of the node to delete

        Returns:
            True if the deletion was submitted, False if not found/protected
        """
        node = self._graph.get_node(node_id)
        if not node:
            return False

        # Cannot delete COLOR or Output nodes
        if node.node_type in self._PROTECTED_NODE_TYPES:
            return False

        self._editor.nodes_about_to_be_removed.emit([node_id])
        item = self._editor._node_items.get(node_id)
        if item is not None:
            item.set_selected(False)
        if self._editor._selected_node_id == node_id:
            self._editor._selected_node_id = None

        # The node stays in the graph model here — the page's
        # nodes_batch_removed handler captures its connections for undo and
        # removes it (with engine-level reconnection) as one group.
        self._editor.remove_node_visual(node_id)
        self._editor.nodes_batch_removed.emit([node_id])
        self._editor.graph_changed.emit()

        return True

    def delete_selected_nodes(self) -> List[str]:
        """Delete all selected nodes, reconnecting chains around each.

        For each deleted node, upstream→downstream connections are preserved.

        Emits: nodes_batch_removed(node_ids)

        Returns:
            List of deleted node IDs
        """
        selected_ids = [
            nid for nid, item in self._editor._node_items.items()
            if item.isSelected() and not self._is_protected_node(nid)
        ]

        if not selected_ids:
            return []

        self._editor.nodes_about_to_be_removed.emit(selected_ids)
        selected_set = set(selected_ids)
        for nid in selected_ids:
            item = self._editor._node_items.get(nid)
            if item is not None:
                item.set_selected(False)
        if self._editor._selected_node_id in selected_set:
            self._editor._selected_node_id = None

        # AUD-10403: no reconnection here. The page's nodes_batch_removed
        # handler captures each node's connections (still intact at this
        # point) for a single grouped undo and then performs the
        # engine-level remove_node_and_reconnect. Reconnecting here first
        # emptied the undo capture and scattered per-cable undo entries.
        for nid in selected_ids:
            self._editor.remove_node_visual(nid)

        self._editor._selected_node_id = None
        self._editor.nodes_batch_removed.emit(selected_ids)
        self._editor.graph_changed.emit()

        return selected_ids

    def delete_selected_connections(self) -> List[Tuple[str, str, str, str]]:
        """Delete all selected connections.

        Emits: node_disconnected for each connection that exists in the
        graph model. A selected visual without a model connection (ghost
        cable) is removed directly — see :meth:`disconnect` (AUD-10402).

        Returns:
            List of (src_node, src_port, dst_node, dst_port) tuples
        """
        deleted = []
        is_locked = getattr(self._editor, "is_node_locked", lambda _id: False)
        for conn_data in list(self._editor._connection_items):
            conn_item = conn_data[0]
            if (conn_item.isSelected()
                    and not is_locked(conn_data[1])
                    and not is_locked(conn_data[3])):
                _, src_nid, src_pname, dst_nid, dst_pname = conn_data
                in_model = any(
                    conn.src_node_id == src_nid
                    and conn.src_port_name == src_pname
                    and conn.dst_node_id == dst_nid
                    and conn.dst_port_name == dst_pname
                    for conn in getattr(self._graph, "connections", []))
                if in_model:
                    # AUD-10402: signal contract — the page owns the graph
                    # disconnect and the visual removal. Removing the visual
                    # here as well made the page delete a second, innocent
                    # cable on multi-input ports.
                    self._editor.node_disconnected.emit(
                        src_nid, src_pname, dst_nid, dst_pname)
                else:
                    # Ghost cable: model connection already gone; nothing to
                    # signal, just drop the stale visual.
                    self._editor._remove_connection_item(conn_item)
                deleted.append((src_nid, src_pname, dst_nid, dst_pname))

        if deleted:
            self._editor.graph_changed.emit()

        return deleted

    # ── Move Operations ──

    def move_node(self, node_id: str, x: float, y: float) -> bool:
        """Move a single node to a new position.

        Emits: nodes_moved([node_id], [old_position])

        Args:
            node_id: ID of the node to move
            x: New X position
            y: New Y position

        Returns:
            True if moved, False if node not found
        """
        item = self._editor._node_items.get(node_id)
        if not item:
            return False

        old_pos = (item.pos().x(), item.pos().y())
        item.setPos(x, y)

        # Update graph model
        node = self._graph.get_node(node_id)
        if node:
            node.position = (x, y)

        self._editor.nodes_moved.emit([node_id], [old_pos])
        self._editor.graph_changed.emit()

        return True

    def move_nodes(self, node_ids: List[str], positions: List[Tuple[float, float]]) -> bool:
        """Move multiple nodes to new positions.

        Emits: nodes_moved(node_ids, old_positions)

        Args:
            node_ids: List of node IDs to move
            positions: List of (x, y) positions

        Returns:
            True if any nodes moved, False otherwise
        """
        if len(node_ids) != len(positions):
            return False

        old_positions = []
        moved_ids = []

        for nid, (x, y) in zip(node_ids, positions):
            item = self._editor._node_items.get(nid)
            if item:
                old_pos = (item.pos().x(), item.pos().y())
                item.setPos(x, y)
                old_positions.append(old_pos)
                moved_ids.append(nid)

                # Update graph model
                node = self._graph.get_node(nid)
                if node:
                    node.position = (x, y)

        if moved_ids:
            self._editor.nodes_moved.emit(moved_ids, old_positions)
            self._editor.graph_changed.emit()

        return len(moved_ids) > 0

    def move_nodes_by_delta(self, node_ids: List[str], dx: float, dy: float) -> bool:
        """Move multiple nodes by a relative offset.

        Emits: nodes_moved(node_ids, old_positions)

        Args:
            node_ids: List of node IDs to move
            dx: X offset
            dy: Y offset

        Returns:
            True if any nodes moved
        """
        old_positions = []
        moved_ids = []

        for nid in node_ids:
            item = self._editor._node_items.get(nid)
            if item:
                old_pos = (item.pos().x(), item.pos().y())
                new_pos = (old_pos[0] + dx, old_pos[1] + dy)
                item.setPos(new_pos[0], new_pos[1])
                old_positions.append(old_pos)
                moved_ids.append(nid)

                # Update graph model
                node = self._graph.get_node(nid)
                if node:
                    node.position = new_pos

        if moved_ids:
            self._editor.nodes_moved.emit(moved_ids, old_positions)
            self._editor.graph_changed.emit()

        return len(moved_ids) > 0

    # ── Connection Operations ──

    def connect(self, src_node_id: str, src_port: str,
                dst_node_id: str, dst_port: str) -> bool:
        """Connect two ports.

        Emits: node_connected(src_node, src_port, dst_node, dst_port)

        AUD-10401: single submit. Validation, the graph write, the visual
        cable and the undo record all belong to CompositePage's
        ``node_connected`` handler (composite_page._on_nodes_connected —
        "NodeEditorWidget no longer does this"). This method used to do all
        four AND emit, so the page redid them: multi-input ports (whose
        ``NodeGraph.connect`` appends without exact-quad dedupe) gained
        duplicate model connections and duplicate visual lines. Now this
        method only guards against duplicate requests and emits.

        Args:
            src_node_id: Source node ID
            src_port: Source output port name
            dst_node_id: Destination node ID
            dst_port: Destination input port name

        Returns:
            True if the connection was created (the page handler runs
            synchronously on emit, so the model already reflects the result)
        """
        # Duplicate request guard: an exact quad already in the model must
        # not be re-submitted — the page's multi-input append path has no
        # exact dedupe and would append a second copy.
        for conn in getattr(self._graph, "connections", []):
            if (conn.src_node_id == src_node_id
                    and conn.src_port_name == src_port
                    and conn.dst_node_id == dst_node_id
                    and conn.dst_port_name == dst_port):
                return False  # Already connected in the model
        # Visual-level guard (a ghost cable without a model connection).
        for conn_data in self._editor._connection_items:
            _, sn, sp, dn, dp = conn_data
            if sn == src_node_id and sp == src_port and dn == dst_node_id and dp == dst_port:
                return False  # Already connected visually

        self._editor.node_connected.emit(src_node_id, src_port, dst_node_id, dst_port)
        if not self._editor._batch_mode:
            self._editor.graph_changed.emit()

        # Same-thread (direct) connections mean the page handler has already
        # run; report whether the connection actually landed in the model
        # (it may have been refused, e.g. a multi-input port at capacity).
        return any(
            conn.src_node_id == src_node_id
            and conn.src_port_name == src_port
            and conn.dst_node_id == dst_node_id
            and conn.dst_port_name == dst_port
            for conn in getattr(self._graph, "connections", []))

    def disconnect(self, src_node_id: str, src_port: str,
                   dst_node_id: str, dst_port: str) -> bool:
        """Disconnect two ports.

        Emits: node_disconnected(src_node, src_port, dst_node, dst_port)

        AUD-10402: visual removal is the page handler's job (signal
        contract). This method used to remove the cable visual itself and
        then emit, so the page removed a second visual matching the same
        destination port — on multi-input ports that deleted an innocent
        cable while leaving the disconnected one painted. Now it only
        emits. A selected visual whose model connection is already gone
        (ghost cable) is cleaned up directly, because the page's by-dst
        cleanup cannot target it precisely and there is no model state left
        to undo.

        Args:
            src_node_id: Source node ID
            src_port: Source output port name
            dst_node_id: Destination node ID
            dst_port: Destination input port name

        Returns:
            True if connection was removed
        """
        # Authoritative store first: the model connection drives the page
        # handler (graph disconnect + visual cleanup + undo).
        in_model = any(
            conn.src_node_id == src_node_id
            and conn.src_port_name == src_port
            and conn.dst_node_id == dst_node_id
            and conn.dst_port_name == dst_port
            for conn in getattr(self._graph, "connections", []))
        if in_model:
            self._editor.node_disconnected.emit(src_node_id, src_port, dst_node_id, dst_port)
            if not self._editor._batch_mode:
                self._editor.graph_changed.emit()
            return True

        # Ghost cable: visual exists but the model connection is gone. No
        # signal — emitting would make the page remove a by-dst visual that
        # may belong to another source and push undo for a connection that
        # does not exist.
        for conn_data in list(self._editor._connection_items):
            conn_item, sn, sp, dn, dp = conn_data
            if sn == src_node_id and sp == src_port and dn == dst_node_id and dp == dst_port:
                self._editor._remove_connection_item(conn_item)
                if not self._editor._batch_mode:
                    self._editor.graph_changed.emit()
                return True

        return False

    def disconnect_by_dst(self, dst_node_id: str, dst_port: str) -> Optional[Tuple[str, str]]:
        """Disconnect an input port and return the source info.

        Emits: node_disconnected(src_node, src_port, dst_node, dst_port)

        U-15：本方法不再直接删除视觉线。旧实现先删第一条 visual 再发
        信号，页面 slot 又按 dst 删下一条——多输入端口上两条线会被连环
        误删。页面是视觉删除的唯一所有者：它按信号携带的精确 quad 删
        除模型与视觉。多条线时选择当前遍历序的第一条（多输入反拖的
        显式选择 UX 另行设计）。

        Args:
            dst_node_id: Destination node ID
            dst_port: Destination input port name

        Returns:
            (src_node_id, src_port) if disconnected, None otherwise
        """
        for conn_data in list(self._editor._connection_items):
            conn_item, sn, sp, dn, dp = conn_data
            if dn == dst_node_id and dp == dst_port:
                self._editor.node_disconnected.emit(sn, sp, dn, dp)
                if not self._editor._batch_mode:
                    self._editor.graph_changed.emit()
                return (sn, sp)

        return None

    def reconnect(self, old_src_node: str, old_src_port: str,
                  new_src_node: str, new_src_port: str,
                  dst_node: str, dst_port: str) -> bool:
        """Reconnect an input port to a different source.

        Emits: node_disconnected (old), node_connected (new)

        Args:
            old_src_node: Current source node ID
            old_src_port: Current source port name
            new_src_node: New source node ID
            new_src_port: New source port name
            dst_node: Destination node ID
            dst_port: Destination input port name

        Returns:
            True if reconnected successfully
        """
        # Disconnect old
        self.disconnect(old_src_node, old_src_port, dst_node, dst_port)
        # Connect new
        return self.connect(new_src_node, new_src_port, dst_node, dst_port)

    def insert_dot_on_connection(
        self,
        old_src_node: str, old_src_port: str,
        old_dst_node: str, old_dst_port: str,
        new_src_node: Optional[str] = None, new_src_port: Optional[str] = None,
        new_dst_node: Optional[str] = None, new_dst_port: Optional[str] = None,
        insert_x: float = 0.0, insert_y: float = 0.0,
    ) -> Optional[str]:
        """Insert a Dot node into an existing connection.

        This operation splits an existing connection and inserts a Dot node:
        - Original: A.output -> B.input
        - After insert: A.output -> Dot.input -> Dot.output -> B.input

        Optionally, a new connection can replace one side of the split:
        - Forward replace: new_source -> Dot.input -> Dot.output -> B.input
          (A.output is disconnected)
        - Reverse replace: A.output -> Dot.input -> Dot.output -> new_destination
          (B.input is disconnected)

        Args:
            old_src_node: Original connection source node ID
            old_src_port: Original connection source port name
            old_dst_node: Original connection destination node ID
            old_dst_port: Original connection destination port name
            new_src_node: Optional new source node ID (replaces original source)
            new_src_port: Optional new source port name
            new_dst_node: Optional new destination node ID (replaces original destination)
            new_dst_port: Optional new destination port name
            insert_x: X position for the Dot node
            insert_y: Y position for the Dot node

        Returns:
            The created Dot node ID, or None if failed
        """
        # Disconnect the original connection
        self.disconnect(old_src_node, old_src_port, old_dst_node, old_dst_port)

        # Create Dot node at insertion point
        dot_node = self.add_node("dot", insert_x, insert_y)
        if not dot_node:
            # Restore original connection if Dot creation failed
            self.connect(old_src_node, old_src_port, old_dst_node, old_dst_port)
            return None

        # Connect: source -> Dot.input
        actual_src_node = new_src_node if new_src_node else old_src_node
        actual_src_port = new_src_port if new_src_port else old_src_port
        src_ok = self.connect(actual_src_node, actual_src_port, dot_node.id, "input")

        # Connect: Dot.output -> destination
        actual_dst_node = new_dst_node if new_dst_node else old_dst_node
        actual_dst_port = new_dst_port if new_dst_port else old_dst_port
        dst_ok = self.connect(dot_node.id, "output", actual_dst_node, actual_dst_port)

        if not (src_ok and dst_ok):
            # AUD-10409: a Dot's ports are "image" only — splitting a
            # scene/geometry (or otherwise incompatible) cable makes one of
            # the two connect() calls fail. The original edge is already
            # gone at this point, so roll the whole operation back instead
            # of leaving the chain broken with a dangling Dot.
            logger.warning(
                "insert_dot_on_connection: split failed (src_ok=%s, dst_ok=%s) "
                "for %s.%s -> %s.%s; rolling back",
                src_ok, dst_ok, old_src_node, old_src_port, old_dst_node,
                old_dst_port,
            )
            self.disconnect(actual_src_node, actual_src_port, dot_node.id, "input")
            self.disconnect(dot_node.id, "output", actual_dst_node, actual_dst_port)
            self.delete_node(dot_node.id)
            if not self.connect(old_src_node, old_src_port,
                                old_dst_node, old_dst_port):
                logger.error(
                    "insert_dot_on_connection: failed to restore original "
                    "connection %s.%s -> %s.%s after rollback",
                    old_src_node, old_src_port, old_dst_node, old_dst_port,
                )
            return None

        self._editor.graph_changed.emit()
        return dot_node.id

    # ── Clipboard Operations ──

    def copy_selected_nodes(self) -> Optional[Dict]:
        """Copy selected nodes to clipboard.

        Returns clipboard data with nodes and internal connections.
        Does NOT emit undo signals (copy is not undoable).

        Returns:
            Clipboard dict with 'nodes' and 'connections' lists
        """
        clipboard = self._serialize_selected_nodes()
        if clipboard is None:
            return None
        self._editor._clipboard = clipboard
        return clipboard

    def _serialize_selected_nodes(self) -> Optional[Dict]:
        """Serialize the selection into a clipboard-shaped dict (pure).

        AUD-10405: unlike :meth:`copy_selected_nodes` this writes nothing to
        the editor clipboard, so callers such as
        :meth:`save_selected_as_preset` cannot silently replace a user's
        pending Ctrl+C content with the preset's source nodes.

        Returns:
            Clipboard dict with 'nodes' and 'connections' lists, or None
            when nothing (valid) is selected.
        """
        selected = [
            (nid, item) for nid, item in self._editor._node_items.items()
            if item.isSelected()
        ]

        if not selected:
            return None

        # Calculate reference point (top-left of selection)
        ref_x = min(item.pos().x() for _, item in selected)
        ref_y = min(item.pos().y() for _, item in selected)

        nodes_data = []
        id_to_idx = {}

        for i, (nid, item) in enumerate(selected):
            node = self._graph.get_node(nid)
            if not node:
                continue

            data = node.to_dict()
            data["position"] = [
                item.pos().x() - ref_x,
                item.pos().y() - ref_y,
            ]
            nodes_data.append(data)
            id_to_idx[nid] = i

        # Save internal connections among selected nodes
        connections = []
        for conn in self._graph.connections:
            if conn.src_node_id in id_to_idx and conn.dst_node_id in id_to_idx:
                connections.append({
                    "src_idx": id_to_idx[conn.src_node_id],
                    "src_port": conn.src_port_name,
                    "dst_idx": id_to_idx[conn.dst_node_id],
                    "dst_port": conn.dst_port_name,
                })

        return {
            "nodes": nodes_data,
            "connections": connections,
        }

    def paste_nodes(self, offset_x: float = 50.0, offset_y: float = 50.0) -> List[str]:
        """Paste nodes from clipboard at viewport center with offset.

        Phase 6 (AUD-10406): when pasting a Topos Color node, the source
        stack's grading is deep-copied into the node's ``embedded_stack`` so
        the copy renders identically without registering anything in the
        project (see :meth:`_localize_topos_color_params`).

        Emits: nodes_pasted(pasted_data)

        Args:
            offset_x: X offset from reference position
            offset_y: Y offset from reference position

        Returns:
            List of new node IDs
        """
        clipboard = self._editor._clipboard
        if not clipboard:
            return []

        from src.features.composite.core.node_engine import NODE_REGISTRY

        # Calculate paste position (center of viewport)
        center = self._editor.mapToScene(self._editor.viewport().rect().center())

        # Deselect all current nodes
        for item in self._editor._node_items.values():
            item.set_selected(False)

        # Map from clipboard index to new node IDs
        idx_to_new_id = {}
        pasted_data = []

        for i, data in enumerate(clipboard["nodes"]):
            node_cls = NODE_REGISTRY.get(data["type"])
            if not node_cls:
                continue

            # Create new ID and offset position
            new_id = str(uuid.uuid4())
            data_copy = copy.deepcopy(data)
            data_copy["id"] = new_id
            data_copy["position"] = [
                center.x() + data["position"][0] + offset_x - 50,
                center.y() + data["position"][1] + offset_y,
            ]

            # Phase 6: Topos Color paste = self-contained local grade copy
            # (AUD-10406 — see _localize_topos_color_params).
            if data["type"] == "topos_color":
                params = data_copy.get("parameters", {})
                self._localize_topos_color_params(params)

            new_node = node_cls.from_dict(data_copy)
            self._graph.add_node(new_node)
            self._editor.add_node_visual(new_node)
            idx_to_new_id[i] = new_id
            pasted_data.append(data_copy)

            # Select the pasted node
            item = self._editor._node_items.get(new_id)
            if item:
                item.set_selected(True)

        # U-16：装配期先通知页面进入静默事务——内部连线经 actions.connect
        # 逐条提交时页面不再压独立 undo/求值，等 nodes_pasted 统一成组，
        # 否则历史里会残留 N 条游离的 "Connect" 记录。
        # （测试桩编辑器可能没有该信号，防御性获取。）
        if pasted_data:
            emit_pasting = getattr(self._editor, "nodes_pasting", None)
            if emit_pasting is not None:
                emit_pasting.emit(pasted_data)

        # Restore internal connections
        for conn in clipboard["connections"]:
            src_id = idx_to_new_id.get(conn["src_idx"])
            dst_id = idx_to_new_id.get(conn["dst_idx"])
            if src_id and dst_id:
                self.connect(src_id, conn["src_port"], dst_id, conn["dst_port"])

        # Emit signal with pasted data for undo support
        if pasted_data:
            self._editor.nodes_pasted.emit(pasted_data)
            self._editor.graph_changed.emit()

        return list(idx_to_new_id.values())

    def _find_host_composite_page(self) -> Optional[Any]:
        """Walk up the widget hierarchy to the hosting CompositePage, if any."""
        widget = self._editor.parent()
        while widget is not None:
            if hasattr(widget, "_runtime_context"):
                return widget
            widget = widget.parent()
        return None

    def _localize_topos_color_params(self, params: Dict[str, Any]) -> None:
        """Make a pasted Topos Color node carry its own grade (AUD-10406).

        The old paste fabricated a ``color_stack_id`` that was never
        registered in the project and set ``stack_source="preset_copy"`` —
        a value the ColorStackRenderService does not resolve, so the pasted
        node silently rendered with default (no) grading while the user
        believed the grade was copied. Now, when the source stack can be
        resolved through the hosting CompositePage's project, it is
        deep-copied into ``embedded_stack`` (``stack_source="embedded"``,
        resolved by the render service without any registry). When it cannot
        be resolved the node keeps referencing the original stack id instead
        of a fabricated one, so the reference at least round-trips within
        the same project.

        Args:
            params: The pasted node's parameter dict (mutated in place).
        """
        source_stack_id = str(params.get("color_stack_id", "") or "")
        params.pop("source_color_stack_id", None)
        params["linked"] = False
        if params.get("role") == "primary_clip_grade":
            # A pasted node is an independent final-stage grade; timeline
            # Color synchronization must not rebind it to the source clip.
            params["role"] = "composite_final_grade"

        composite_page = self._find_host_composite_page()
        project = getattr(composite_page, "_project", None)
        color_stacks = getattr(project, "color_stacks", None)
        source_stack = (
            color_stacks.get(source_stack_id)
            if isinstance(color_stacks, dict) and source_stack_id else None)
        to_dict = getattr(source_stack, "to_dict", None)

        if source_stack is not None and callable(to_dict):
            try:
                snapshot = to_dict()
            except Exception:
                logger.exception(
                    "[Phase6] Failed to snapshot ColorStack %r for paste; "
                    "keeping a reference to the source stack",
                    source_stack_id,
                )
            else:
                params["stack_source"] = "embedded"
                params["embedded_stack"] = copy.deepcopy(snapshot)
                params["color_stack_id"] = ""
                logger.info(
                    "[Phase6] Topos Color paste embedded source stack %r "
                    "into the node", source_stack_id,
                )
                return

        if source_stack_id:
            logger.info(
                "[Phase6] Topos Color paste: source stack %r not resolvable; "
                "keeping the clip_stack reference", source_stack_id,
            )

    def cut_selected_nodes(self) -> Tuple[Optional[Dict], List[str]]:
        """Cut (copy + delete) selected nodes.

        Emits: nodes_batch_removed(node_ids)

        Returns:
            (clipboard_data, deleted_ids)
        """
        clipboard = self.copy_selected_nodes()

        # AUD-10408: protection by node_type (rename-proof), not display title.
        selected_ids = [
            nid for nid, item in self._editor._node_items.items()
            if item.isSelected() and not self._is_protected_node(nid)
        ]

        for nid in selected_ids:
            self._editor.remove_node_visual(nid)

        self._editor._selected_node_id = None

        if selected_ids:
            self._editor.nodes_batch_removed.emit(selected_ids)
            self._editor.graph_changed.emit()

        return (clipboard, selected_ids)

    def duplicate_selected_nodes(self, offset_x: float = 40.0, offset_y: float = 40.0) -> List[str]:
        """Duplicate selected nodes with offset.

        Emits: nodes_duplicated(new_ids)

        Args:
            offset_x: X offset for duplicates
            offset_y: Y offset for duplicates

        Returns:
            List of new node IDs
        """
        from src.features.composite.core.node_engine import NODE_REGISTRY

        # AUD-10408: protection by node_type (rename-proof), not display title.
        selected = [
            (nid, item) for nid, item in self._editor._node_items.items()
            if item.isSelected() and not self._is_protected_node(nid)
        ]

        if not selected:
            return []

        new_ids = []
        for nid, item in selected:
            node = self._graph.get_node(nid)
            if not node:
                continue

            # Serialize current node state and create a copy with new ID
            node_data = node.to_dict()
            node_data["id"] = str(uuid.uuid4())
            node_data["position"] = [
                item.pos().x() + offset_x,
                item.pos().y() + offset_y,
            ]

            node_cls = NODE_REGISTRY.get(node_data["type"])
            if node_cls:
                new_node = node_cls.from_dict(node_data)
                self._graph.add_node(new_node)
                self._editor.add_node_visual(new_node)
                new_ids.append(new_node.id)

        if new_ids:
            self._editor.nodes_duplicated.emit(new_ids)
            self._editor.graph_changed.emit()

        return new_ids

    # ── Bypass/Disable Operations ──

    def toggle_bypass(self, node_id: str) -> bool:
        """Toggle bypass/disable state for a node.

        Emits: node_bypass_toggled(node_id)

        Args:
            node_id: ID of the node to toggle

        Returns:
            New bypass state (True = disabled)
        """
        item = self._editor._node_items.get(node_id)
        if not item:
            return False

        new_state = not item._disabled
        item.set_disabled(new_state)

        # Update graph model
        self._graph.set_node_disabled(node_id, new_state)

        self._editor.node_bypass_toggled.emit(node_id)
        self._editor.graph_changed.emit()

        return new_state

    def toggle_bypass_selected(self) -> List[str]:
        """Toggle bypass for all selected nodes.

        Emits: nodes_bypass_batch_toggled({node_id: new_state}) once so the
        page records a single grouped undo entry (U-07).

        Returns:
            List of toggled node IDs
        """
        toggled_ids = []
        batch_changes: dict = {}
        for nid, item in self._editor._node_items.items():
            if item.isSelected():
                new_state = not item._disabled
                item.set_disabled(new_state)
                self._graph.set_node_disabled(nid, new_state)
                batch_changes[nid] = new_state
                toggled_ids.append(nid)

        if toggled_ids:
            self._editor.nodes_bypass_batch_toggled.emit(batch_changes)
            self._editor.graph_changed.emit()

        return toggled_ids

    def set_bypass(self, node_id: str, disabled: bool) -> bool:
        """Set bypass state explicitly.

        Emits: node_bypass_toggled(node_id)

        Args:
            node_id: ID of the node
            disabled: True to disable, False to enable

        Returns:
            True if state was changed
        """
        item = self._editor._node_items.get(node_id)
        if not item:
            return False

        if item._disabled == disabled:
            return False  # No change

        item.set_disabled(disabled)
        self._graph.set_node_disabled(node_id, disabled)

        self._editor.node_bypass_toggled.emit(node_id)
        self._editor.graph_changed.emit()

        return True

    # ── Parameter Operations ──

    def set_parameter(self, node_id: str, param_name: str, value: Any) -> bool:
        """Set a node parameter.

        Note: This is a convenience method. For undo support,
        use NodePropertiesPanel which has debouncing.

        Args:
            node_id: ID of the node
            param_name: Parameter name
            value: New value

        Returns:
            True if parameter was set
        """
        node = self._graph.get_node(node_id)
        if not node:
            return False

        self._graph.set_node_parameter(node_id, param_name, value)
        self._editor.graph_changed.emit()

        return True

    # ── Group Operations ──

    def group_selected_nodes(self, title: str = "Group",
                              color: str = "#4A90D9") -> Optional[str]:
        """Create a group from selected nodes.

        Uses group_commands.CreateGroupCommand internally.

        AUD-9920: the applied command is emitted via
        ``group_commands_applied`` so CompositePage registers it on its
        UndoStack — the command object must not be discarded after redo().

        Args:
            title: Group title
            color: Group color

        Returns:
            Group ID if created, None otherwise
        """
        from .group_commands import build_group_command_from_selection

        selected_ids = [
            nid for nid, item in self._editor._node_items.items()
            if item.isSelected()
        ]

        if len(selected_ids) < 2:
            return None

        cmd = build_group_command_from_selection(
            self._graph, selected_ids, title=title, color=color
        )
        cmd.redo()
        self._editor.group_commands_applied.emit(cmd.description, [cmd])

        self._editor.graph_changed.emit()
        return cmd.group_id

    def ungroup_selected(self) -> List[str]:
        """Dissolve groups containing selected nodes.

        Uses group_commands.UngroupCommand internally.

        AUD-9920: all applied commands are emitted in ONE
        ``group_commands_applied`` batch so the whole dissolve is a single
        undo step (same batching precedent as backdrops_deleted).

        Returns:
            List of dissolved group IDs
        """
        from .group_commands import get_group_for_node, UngroupCommand

        dissolved_ids: List[str] = []
        applied: List[UngroupCommand] = []
        selected_ids = [
            nid for nid, item in self._editor._node_items.items()
            if item.isSelected()
        ]

        for nid in selected_ids:
            group = get_group_for_node(self._graph, nid)
            if group and group['id'] not in dissolved_ids:
                cmd = UngroupCommand(self._graph, group['id'])
                cmd.redo()
                applied.append(cmd)
                dissolved_ids.append(group['id'])

        if dissolved_ids:
            self._editor.group_commands_applied.emit("Ungroup nodes", applied)
            self._editor.graph_changed.emit()

        return dissolved_ids

    def add_to_group(self, group_id: str, node_ids: List[str]) -> bool:
        """Add nodes to an existing group.

        AUD-9920: the applied command is emitted via
        ``group_commands_applied`` for the page's undo record.

        Args:
            group_id: Target group ID
            node_ids: Node IDs to add

        Returns:
            True if any nodes were added
        """
        from .group_commands import AddToGroupCommand

        cmd = AddToGroupCommand(self._graph, group_id, node_ids)
        cmd.redo()
        self._editor.group_commands_applied.emit(cmd.description, [cmd])

        self._editor.graph_changed.emit()
        return True

    def remove_from_group(self, group_id: str, node_ids: List[str]) -> bool:
        """Remove nodes from a group.

        AUD-9920: the applied command is emitted via
        ``group_commands_applied`` for the page's undo record.

        Args:
            group_id: Group ID
            node_ids: Node IDs to remove

        Returns:
            True if any nodes were removed
        """
        from .group_commands import RemoveFromGroupCommand

        cmd = RemoveFromGroupCommand(self._graph, group_id, node_ids)
        cmd.redo()
        self._editor.group_commands_applied.emit(cmd.description, [cmd])

        self._editor.graph_changed.emit()
        return True

    # ── Selection Operations ──

    def select_node(self, node_id: str, add_to_selection: bool = False) -> bool:
        """Select a node.

        Emits: node_selected(node_id) or selection_cleared

        Args:
            node_id: ID of the node to select
            add_to_selection: If True, add to current selection

        Returns:
            True if node was selected
        """
        item = self._editor._node_items.get(node_id)
        if not item:
            return False

        if not add_to_selection:
            # Clear other selections first
            for nid, other in self._editor._node_items.items():
                other.set_selected(nid == node_id)

        item.set_selected(True)
        self._editor._selected_node_id = node_id
        self._editor.node_selected.emit(node_id)

        return True

    def select_nodes(self, node_ids: List[str]) -> bool:
        """Select multiple nodes.

        Emits: node_selected(first_node_id)

        Args:
            node_ids: List of node IDs to select

        Returns:
            True if any nodes were selected
        """
        if not node_ids:
            return False

        # Clear all first
        for item in self._editor._node_items.values():
            item.set_selected(False)

        # Select specified
        for nid in node_ids:
            item = self._editor._node_items.get(nid)
            if item:
                item.set_selected(True)

        self._editor._selected_node_id = node_ids[0]
        self._editor.node_selected.emit(node_ids[0])

        return True

    def select_all_nodes(self) -> int:
        """Select all nodes.

        Returns:
            Number of nodes selected
        """
        count = 0
        for item in self._editor._node_items.values():
            item.set_selected(True)
            count += 1

        return count

    def clear_selection(self) -> None:
        """Clear all selections.

        Emits: selection_cleared
        """
        for item in self._editor._node_items.values():
            item.set_selected(False)

        self._editor._selected_node_id = None
        self._editor.selection_cleared.emit()

    def get_selected_node_ids(self) -> List[str]:
        """Get all selected node IDs.

        Returns:
            List of selected node IDs
        """
        return [
            nid for nid, item in self._editor._node_items.items()
            if item.isSelected()
        ]

    def select_upstream(self) -> List[str]:
        """Select all upstream connected nodes from current selection.

        Returns:
            List of selected node IDs
        """
        selected_ids = self.get_selected_node_ids()
        if not selected_ids:
            return []

        upstream = set()
        for nid in selected_ids:
            upstream.update(self._editor._get_upstream_nodes(nid))

        # Add upstream nodes to selection
        for nid in upstream:
            item = self._editor._node_items.get(nid)
            if item:
                item.set_selected(True)

        return list(upstream)

    def select_downstream(self) -> List[str]:
        """Select all downstream connected nodes from current selection.

        Returns:
            List of selected node IDs
        """
        selected_ids = self.get_selected_node_ids()
        if not selected_ids:
            return []

        downstream = set()
        for nid in selected_ids:
            downstream.update(self._editor._get_downstream_nodes(nid))

        # Add downstream nodes to selection
        for nid in downstream:
            item = self._editor._node_items.get(nid)
            if item:
                item.set_selected(True)

        return list(downstream)

    # ── View Operations ──

    def fit_all_nodes(self) -> None:
        """Fit all nodes in the viewport."""
        self._editor.fit_all_nodes()

    def fit_selected_nodes(self) -> None:
        """Fit selected nodes in the viewport."""
        self._editor.fit_selected_nodes()

    def set_zoom(self, level: float) -> None:
        """Set zoom level.

        Args:
            level: Zoom level (0.1 - 5.0)
        """
        from PyQt6.QtGui import QTransform

        level = max(0.1, min(5.0, level))
        self._editor._current_zoom = level
        self._editor.setTransform(QTransform().scale(level, level))
        self._editor.zoom_changed.emit(level)

    def get_zoom(self) -> float:
        """Get current zoom level."""
        return self._editor._current_zoom

    # ── Utility Methods ──

    def get_node_at_position(self, x: float, y: float) -> Optional[str]:
        """Get node ID at a scene position.

        Args:
            x: X position in scene coordinates
            y: Y position in scene coordinates

        Returns:
            Node ID if found, None otherwise
        """
        from PyQt6.QtCore import QPointF

        scene_pos = QPointF(x, y)
        item = self._editor._find_node_at(scene_pos)
        if item:
            return item.node_id
        return None

    def get_port_at_position(self, x: float, y: float) -> Optional[Tuple[str, str, str]]:
        """Get port info at a scene position.

        Args:
            x: X position in scene coordinates
            y: Y position in scene coordinates

        Returns:
            (node_id, port_name, direction) if found, None otherwise
        """
        from PyQt6.QtCore import QPointF

        scene_pos = QPointF(x, y)
        port = self._editor._find_port_at(scene_pos)
        if port:
            parent = port.parentItem()
            if hasattr(parent, 'node_id'):
                return (parent.node_id, port.port_name, port.direction)
        return None

    # ── Preset Operations ──

    def save_selected_as_preset(self, name: str, description: str = "",
                                author: str = "") -> bool:
        """保存选中节点为预设模板。

        Args:
            name: 预设名称
            description: 预设描述
            author: 作者名（可选）

        Returns:
            True 如果保存成功
        """
        from src.features.composite.utils.preset_manager import PresetManager

        # 先序列化选中节点（获取节点数据）。AUD-10405: 必须走无副作用的
        # 序列化——借道 copy_selected_nodes 会覆写编辑器剪贴板，保存预设后
        # Ctrl+V 粘贴出的是预设源节点而不是用户先前复制的内容。
        nodes_data = self._serialize_selected_nodes()
        if not nodes_data or not nodes_data.get("nodes"):
            return False

        # 获取 PresetManager 实例（从编辑器或创建新的）
        preset_manager = getattr(self._editor, '_preset_manager', None)
        if preset_manager is None:
            preset_manager = PresetManager()
            self._editor._preset_manager = preset_manager

        return preset_manager.save_preset(name, nodes_data, description, author)

    def load_preset(self, name: str, position: Optional[Tuple[float, float]] = None,
                    select_loaded: bool = True) -> List[str]:
        """加载预设模板并创建节点。

        Args:
            name: 预设名称
            position: 放置位置（场景坐标），默认为视图中心
            select_loaded: 是否选中新创建的节点

        Returns:
            新创建的节点 ID 列表
        """
        from src.features.composite.utils.preset_manager import PresetManager
        from src.features.composite.core.node_engine import NODE_REGISTRY

        # 获取 PresetManager 实例
        preset_manager = getattr(self._editor, '_preset_manager', None)
        if preset_manager is None:
            preset_manager = PresetManager()
            self._editor._preset_manager = preset_manager

        # 加载预设数据
        preset_data = preset_manager.load_preset(name)
        if not preset_data:
            return []

        nodes_template = preset_data.get("nodes", [])
        connections_template = preset_data.get("connections", [])

        if not nodes_template:
            return []

        # 计算放置位置
        if position is None:
            center = self._editor.mapToScene(self._editor.viewport().rect().center())
            position = (center.x(), center.y())

        # 计算模板中的参考点（左上角）。AUD-10410: 预设是磁盘 JSON（可手改/
        # 第三方导入），position 可能是标量/null/短列表——逐节点校验形状，
        # 非法回退 [0, 0] 并告警，避免 IndexError/TypeError 从槽内抛出。
        positions: List[List[float]] = []
        for idx, n in enumerate(nodes_template):
            pos = self._safe_preset_position(
                n.get("position"), label=f"nodes[{idx}]")
            positions.append(pos)
        ref_x = min(p[0] for p in positions)
        ref_y = min(p[1] for p in positions)

        # 清除当前选择
        if select_loaded:
            for item in self._editor._node_items.values():
                item.set_selected(False)

        # 创建节点
        idx_to_new_id: Dict[int, str] = {}
        new_ids: List[str] = []
        pasted_data: List[dict] = []

        for i, node_data in enumerate(nodes_template):
            node_cls = NODE_REGISTRY.get(node_data.get("type"))
            if not node_cls:
                continue

            # 创建新 ID 并偏移位置
            new_id = str(uuid.uuid4())
            node_data_copy = copy.deepcopy(node_data)
            node_data_copy["id"] = new_id

            # 应用位置偏移（AUD-10410: 同样过形状校验）
            orig_pos = self._safe_preset_position(
                node_data.get("position"), label=f"nodes[{i}]")
            node_data_copy["position"] = [
                position[0] + orig_pos[0] - ref_x,
                position[1] + orig_pos[1] - ref_y,
            ]

            # 创建节点
            new_node = node_cls.from_dict(node_data_copy)
            self._graph.add_node(new_node)
            self._editor.add_node_visual(new_node)

            idx_to_new_id[i] = new_id
            new_ids.append(new_node.id)
            pasted_data.append(node_data_copy)

            # 选中新节点
            if select_loaded:
                item = self._editor._node_items.get(new_id)
                if item:
                    item.set_selected(True)

        # U-16：与 paste_nodes 同一静默装配事务（见该方法注释；测试桩
        # 编辑器可能没有该信号，防御性获取）。
        if pasted_data:
            emit_pasting = getattr(self._editor, "nodes_pasting", None)
            if emit_pasting is not None:
                emit_pasting.emit(pasted_data)

        # 恢复内部连接
        for conn in connections_template:
            src_idx = conn.get("src_idx")
            dst_idx = conn.get("dst_idx")
            src_port = conn.get("src_port")
            dst_port = conn.get("dst_port")

            if src_idx is not None and dst_idx is not None:
                src_id = idx_to_new_id.get(src_idx)
                dst_id = idx_to_new_id.get(dst_idx)
                if src_id and dst_id:
                    self.connect(src_id, src_port, dst_id, dst_port)

        # 发送信号
        if pasted_data:
            self._editor.nodes_pasted.emit(pasted_data)
            self._editor.graph_changed.emit()

        return new_ids

    @staticmethod
    def _safe_preset_position(value: Any, label: str = "node") -> List[float]:
        """Validate a node position coming from external preset JSON.

        AUD-10410: ``PresetManager._validate_preset`` only checks that
        ``nodes``/``connections`` are lists, so a hand-edited or third-party
        preset can carry ``position: []``, ``position: 100`` or
        ``position: null``. Indexing such values raised IndexError/
        TypeError straight out of the load slot. Invalid shapes fall back
        to ``[0.0, 0.0]`` with a warning.

        Args:
            value: The raw ``position`` value from the preset dict.
            label: Context label for the warning message.

        Returns:
            A validated ``[x, y]`` list.
        """
        if isinstance(value, (list, tuple)) and len(value) >= 2:
            x, y = value[0], value[1]
            if (isinstance(x, (int, float)) and not isinstance(x, bool)
                    and isinstance(y, (int, float)) and not isinstance(y, bool)):
                return [float(x), float(y)]
        logger.warning(
            "load_preset: %s has invalid position %r; falling back to [0, 0]",
            label, value,
        )
        return [0.0, 0.0]

    def get_preset_manager(self) -> "PresetManager":
        """获取 PresetManager 实例。

        Returns:
            PresetManager 实例
        """
        from src.features.composite.utils.preset_manager import PresetManager

        preset_manager = getattr(self._editor, '_preset_manager', None)
        if preset_manager is None:
            preset_manager = PresetManager()
            self._editor._preset_manager = preset_manager

        return preset_manager

    def list_presets(self) -> List[str]:
        """列出所有可用预设。

        Returns:
            预设名称列表
        """
        return self.get_preset_manager().list_presets()

    def delete_preset(self, name: str) -> bool:
        """删除预设。

        Args:
            name: 预设名称

        Returns:
            True 如果删除成功
        """
        return self.get_preset_manager().delete_preset(name)

    def import_preset(self, file_path: str) -> Optional[str]:
        """从文件导入预设。

        Args:
            file_path: JSON 文件路径

        Returns:
            导入后的预设名称，或 None 如果失败
        """
        return self.get_preset_manager().import_preset(file_path)

    def export_preset(self, name: str, file_path: str) -> bool:
        """导出预设到文件。

        Args:
            name: 预设名称
            file_path: 目标文件路径

        Returns:
            True 如果导出成功
        """
        return self.get_preset_manager().export_preset(name, file_path)
