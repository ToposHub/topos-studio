# DORMANT(2026-08-19 审计): 已实现且有测试，但暂无生产入口（有意休眠）。
# 接线前请勿在生产依赖；若半年内无接线计划建议删除。
"""
Node Graph Commands - Unified command/undo system for node graph editing.

All node graph edit operations should go through these commands to ensure
consistent undo/redo behavior.

Usage:
    from .node_graph_commands import AddNodeCommand, DeleteNodeCommand, ConnectCommand

    # Single operation
    cmd = AddNodeCommand(node_graph, node_type, position)
    undo_stack.push(cmd)

    # Grouped operations
    undo_stack.begin_group()
    # ... multiple operations ...
    undo_stack.end_group("Delete multiple nodes")
"""

from __future__ import annotations

from typing import Optional, List, Dict, Any, Tuple, Callable, TYPE_CHECKING
from dataclasses import dataclass
import uuid

if TYPE_CHECKING:
    from src.features.composite.core.node_engine import NodeGraph


@dataclass
class AddNodeCommand:
    """Command to add a node to the graph."""

    graph: 'NodeGraph'
    node_type: str
    node_id: str
    position: Tuple[float, float]
    parameters: Optional[Dict[str, Any]] = None

    def __post_init__(self) -> None:
        self._node = None
        self.description = f"Add {self.node_type}"

    def redo(self):
        """Add the node."""
        from src.features.composite.core.node_engine import NODE_REGISTRY

        # Create node instance from registry
        node_class = NODE_REGISTRY.get(self.node_type)
        if not node_class:
            raise ValueError(f"Unknown node type: {self.node_type}")

        node = node_class(node_id=self.node_id)
        node.position = list(self.position)
        if self.parameters:
            for k, v in self.parameters.items():
                node.set_parameter(k, v)

        # Add node to graph
        self.graph.add_node(node)
        self._node = node
        return node

    def undo(self) -> None:
        """Remove the node."""
        if self._node:
            self.graph.remove_node(self._node.id)


@dataclass
class DeleteNodeCommand:
    """Command to delete a node (or multiple nodes) from the graph.

    Uses remove_node_and_reconnect for auto-reconnection on single-input/output nodes.
    Undo restores the original topology including removing reconnection links.
    """

    graph: NodeGraph
    node_ids: List[str]  # List of node IDs to delete

    def __post_init__(self) -> None:
        self._deleted_data: List[Dict] = []  # Store node data for undo
        self._reconnected_links: List[Dict] = []  # Store auto-reconnected links
        self.description = f"Delete {len(self.node_ids)} node(s)"

    def redo(self) -> None:
        """Delete the nodes with auto-reconnection.

        AUD-10602: the auto-reconnected links are MEASURED, not predicted.
        The old prediction judged eligibility by port count while the engine
        (``NodeGraph.remove_node_and_reconnect``) judges by connection count
        and re-validates each link — the two could disagree (e.g. a Blend
        node with two image input ports but only one connected), leaving
        real reconnection links unrecorded so undo leaked them into the
        graph. Snapshotting the connection set before the removal and
        diffing afterwards captures exactly what the engine created.
        """
        self._deleted_data = []
        self._reconnected_links = []

        for node_id in self.node_ids:
            node = self.graph.get_node(node_id)
            if node:
                # Store connections before deletion
                connections = []
                for conn in self.graph.get_connections_for_node(node_id):
                    connections.append({
                        'from_node': conn.src_node_id,
                        'from_port': conn.src_port_name,
                        'to_node': conn.dst_node_id,
                        'to_port': conn.dst_port_name
                    })

                conns_before = {
                    (c.src_node_id, c.src_port_name,
                     c.dst_node_id, c.dst_port_name)
                    for c in self.graph.connections
                }

                self._deleted_data.append({
                    'node': node,
                    'connections': connections
                })

                # Use remove_node_and_reconnect for auto-reconnection
                self.graph.remove_node_and_reconnect(node_id)

                # Diff: the links the engine actually created while removing
                # this node are exactly what undo must tear down again.
                conns_after = {
                    (c.src_node_id, c.src_port_name,
                     c.dst_node_id, c.dst_port_name)
                    for c in self.graph.connections
                }
                for src_nid, src_port, dst_nid, dst_port in (conns_after - conns_before):
                    self._reconnected_links.append({
                        'from_node': src_nid,
                        'from_port': src_port,
                        'to_node': dst_nid,
                        'to_port': dst_port,
                    })

    def undo(self) -> None:
        """Restore the nodes and original topology."""
        # First, remove any auto-reconnected links. Route the removal
        # through NodeGraph.disconnect (same as ConnectCommand.undo) so the
        # mutation happens under the graph lock and bumps the topology
        # revision / dirty flags instead of editing the connections list
        # directly (AUD-10601).
        for link in self._reconnected_links:
            try:
                self.graph.disconnect(
                    link['from_node'],
                    link['from_port'],
                    link['to_node'],
                    link['to_port']
                )
            except Exception:
                pass  # Link may already be gone

        # Then restore nodes and their original connections
        for data in reversed(self._deleted_data):
            node = data['node']
            self.graph.add_node(node)
            # Restore original connections
            for conn_data in data['connections']:
                if conn_data['from_node'] and conn_data['to_node']:
                    try:
                        self.graph.connect(
                            conn_data['from_node'],
                            conn_data['from_port'],
                            conn_data['to_node'],
                            conn_data['to_port']
                        )
                    except Exception:
                        pass  # Connection may not be valid anymore


@dataclass
class MoveNodeCommand:
    """Command to move a node (or multiple nodes) to a new position.

    Updates both the data model (node.position) and optionally the UI
    through a callback for position synchronization.

    Args:
        graph: The NodeGraph instance
        node_ids: List of node IDs to move
        new_positions: Dict mapping node_id to new (x, y) position
        ui_update_callback: Optional callback(node_id, position) to update UI
    """

    graph: NodeGraph
    node_ids: List[str]
    new_positions: Dict[str, Tuple[float, float]]
    ui_update_callback: Optional[Callable[[str, Tuple[float, float]], None]] = None

    def __post_init__(self) -> None:
        self._old_positions: Dict[str, Tuple[float, float]] = {}
        self.description = f"Move {len(self.node_ids)} node(s)"

    def redo(self) -> None:
        """Move nodes to new positions."""
        # Store old positions first time
        if not self._old_positions:
            for node_id in self.node_ids:
                node = self.graph.get_node(node_id)
                if node:
                    self._old_positions[node_id] = tuple(node.position)

        # Apply new positions
        for node_id, pos in self.new_positions.items():
            node = self.graph.get_node(node_id)
            if node:
                node.position = list(pos)
                # Update UI if callback provided
                if self.ui_update_callback:
                    self.ui_update_callback(node_id, pos)

    def undo(self) -> None:
        """Restore old positions."""
        for node_id, pos in self._old_positions.items():
            node = self.graph.get_node(node_id)
            if node:
                node.position = list(pos)
                # Update UI if callback provided
                if self.ui_update_callback:
                    self.ui_update_callback(node_id, pos)


@dataclass
class ConnectCommand:
    """Command to connect two ports."""

    graph: NodeGraph
    from_node_id: str
    from_port: str
    to_node_id: str
    to_port: str

    def __post_init__(self) -> None:
        self._connected: bool = False
        self.description = "Connect ports"

    def redo(self):
        """Create the connection."""
        self._connected = self.graph.connect(
            self.from_node_id,
            self.from_port,
            self.to_node_id,
            self.to_port
        )
        return self._connected

    def undo(self) -> None:
        """Remove the connection."""
        if self._connected:
            try:
                self.graph.disconnect(
                    self.from_node_id,
                    self.from_port,
                    self.to_node_id,
                    self.to_port
                )
            except Exception:
                pass


@dataclass
class DisconnectCommand:
    """Command to disconnect two ports."""

    graph: NodeGraph
    from_node_id: str
    from_port: str
    to_node_id: str
    to_port: str

    def __post_init__(self) -> None:
        self._disconnected: bool = False
        self.description = "Disconnect ports"

    def redo(self) -> None:
        """Disconnect the ports."""
        self._disconnected = self.graph.disconnect(
            self.from_node_id,
            self.from_port,
            self.to_node_id,
            self.to_port
        )

    def undo(self) -> None:
        """Reconnect the ports."""
        if self._disconnected:
            try:
                self.graph.connect(
                    self.from_node_id,
                    self.from_port,
                    self.to_node_id,
                    self.to_port
                )
            except Exception:
                pass


@dataclass
class ReconnectCommand:
    """Command to reconnect a port to a different source.

    Atomically reconnects by leveraging NodeGraph.connect's built-in
    validation and safe disconnect logic. If the new connection fails
    validation (type mismatch, cycle, etc.), the old connection is preserved.

    Note: NodeGraph.connect automatically disconnects the old connection
    on the input port when max_connections limit is reached (typically 1).
    """

    graph: NodeGraph
    to_node_id: str
    to_port: str
    old_from_node_id: str
    old_from_port: str
    new_from_node_id: str
    new_from_port: str

    def __post_init__(self) -> None:
        self._reconnected: bool = False
        self.description = "Reconnect ports"

    def redo(self) -> None:
        """Atomically reconnect to new source.

        NodeGraph.connect handles validation and safe disconnect of old connection.
        If validation fails, the old connection is preserved without modification.
        """
        # Connect to new source - this atomically:
        # 1. Validates the new connection (type, cycle check)
        # 2. Disconnects the old connection if validation passes
        # 3. Creates the new connection
        # Returns False if validation fails, leaving old connection intact
        self._reconnected = self.graph.connect(
            self.new_from_node_id,
            self.new_from_port,
            self.to_node_id,
            self.to_port
        )

    def undo(self) -> None:
        """Restore old connection if reconnection was successful."""
        if not self._reconnected:
            return  # Nothing was changed

        # Remove new connection
        self.graph.disconnect(
            self.new_from_node_id,
            self.new_from_port,
            self.to_node_id,
            self.to_port
        )

        # Restore old connection
        self.graph.connect(
            self.old_from_node_id,
            self.old_from_port,
            self.to_node_id,
            self.to_port
        )

    def was_successful(self) -> bool:
        """Return True if the reconnection was successful."""
        return self._reconnected


@dataclass
class SetParameterCommand:
    """Command to change a node parameter."""
    
    graph: NodeGraph
    node_id: str
    param_name: str
    new_value: Any
    
    def __post_init__(self) -> None:
        # AUD-10603: ``None`` is a legitimate old value (unset parameter), so
        # "not captured yet" must be tracked by an explicit flag (mirrors
        # RenameNodeCommand) instead of overloading ``_old_value is None``.
        self._old_value: Any = None
        self._captured: bool = False
        self.description = f"Set {self.param_name}"

    def redo(self) -> None:
        """Set the new parameter value."""
        node = self.graph.get_node(self.node_id)
        if node and not self._captured:
            self._old_value = node.parameters.get(self.param_name)
            self._captured = True
        if node:
            node.set_parameter(self.param_name, self.new_value)

    def undo(self) -> None:
        """Restore the old parameter value.

        ``_old_value`` of ``None`` means the parameter was previously unset;
        writing ``None`` back restores that state (readers use ``.get``), so
        undo works for previously-unset parameters too.
        """
        node = self.graph.get_node(self.node_id)
        if node and self._captured:
            node.set_parameter(self.param_name, self._old_value)


@dataclass
class RenameNodeCommand:
    """Command to rename a node (B4: set/clear its ``user_label``).

    ``new_label`` of ``None`` or a blank string clears the custom label so
    display falls back to the auto number — matching the canvas rename UI.
    """
    
    graph: NodeGraph
    node_id: str
    new_label: Optional[str]
    
    def __post_init__(self) -> None:
        self._old_label: Optional[str] = None
        self._captured = False
        self.description = "Rename node"

    @staticmethod
    def _normalize(label: Optional[str]) -> Optional[str]:
        if label is None:
            return None
        label = str(label).strip()
        return label or None

    def redo(self) -> None:
        node = self.graph.get_node(self.node_id)
        if node is None:
            return
        if not self._captured:
            self._old_label = getattr(node, "user_label", None)
            self._captured = True
        node.user_label = self._normalize(self.new_label)

    def undo(self) -> None:
        node = self.graph.get_node(self.node_id)
        if node is not None:
            node.user_label = self._old_label


@dataclass
class CopyNodesCommand:
    """Command to copy nodes to clipboard.

    Stores both node data and internal connections among selected nodes.
    The clipboard data format:
    {
        'nodes': [{'id': str, 'type': str, 'parameters': dict, 'position': [x, y]}],
        'connections': [{'src_idx': int, 'src_port': str, 'dst_idx': int, 'dst_port': str}]
    }
    Connections use indices into the nodes list to maintain references.
    """

    graph: NodeGraph
    node_ids: List[str]

    def __post_init__(self) -> None:
        self._clipboard_data: Dict[str, Any] = {}
        self.description = f"Copy {len(self.node_ids)} node(s)"

    def redo(self):
        """Copy node data and internal connections to clipboard."""
        nodes_data: List[Dict] = []
        id_to_idx: Dict[str, int] = {}

        # Collect node data. AUD-10604: the clipboard contract is "indices
        # point into the nodes list"; missing nodes must NOT shift the
        # mapping, so index by the appended length instead of the input
        # position — otherwise pasted internal connections resolve to the
        # wrong (or missing) nodes.
        for node_id in self.node_ids:
            node = self.graph.get_node(node_id)
            if node:
                id_to_idx[node_id] = len(nodes_data)
                nodes_data.append({
                    'id': node_id,
                    'type': node.node_type,
                    'parameters': dict(node.parameters),
                    'position': list(node.position),
                })

        # Collect internal connections among selected nodes
        connections: List[Dict] = []
        for conn in self.graph.connections:
            if conn.src_node_id in id_to_idx and conn.dst_node_id in id_to_idx:
                connections.append({
                    'src_idx': id_to_idx[conn.src_node_id],
                    'src_port': conn.src_port_name,
                    'dst_idx': id_to_idx[conn.dst_node_id],
                    'dst_port': conn.dst_port_name,
                })

        self._clipboard_data = {
            'nodes': nodes_data,
            'connections': connections,
        }
        return self._clipboard_data

    def undo(self) -> None:
        """No-op for copy."""
        pass

    def get_clipboard_data(self) -> Dict[str, Any]:
        """Return clipboard data for paste."""
        return self._clipboard_data


@dataclass
class PasteNodesCommand:
    """Command to paste nodes from clipboard.

    Accepts clipboard data in the format:
    {
        'nodes': [{'id': str, 'type': str, 'parameters': dict, 'position': [x, y]}],
        'connections': [{'src_idx': int, 'src_port': str, 'dst_idx': int, 'dst_port': str}]
    }

    Creates new nodes with new IDs and restores internal connections
    using index mapping.
    """

    graph: NodeGraph
    clipboard_data: Dict[str, Any]  # Contains 'nodes' and 'connections'
    offset: Tuple[float, float] = (50, 50)

    def __post_init__(self) -> None:
        self._pasted_node_ids: List[str] = []
        self._created_connections: List[Tuple[str, str, str, str]] = []  # For undo
        # AUD-10605: clipboard data is foreign-shaped; tolerate anything so
        # command construction itself cannot raise.
        nodes_for_count = (self.clipboard_data.get('nodes', [])
                           if isinstance(self.clipboard_data, dict) else [])
        if not isinstance(nodes_for_count, (list, tuple)):
            nodes_for_count = []
        self.description = f"Paste {len(nodes_for_count)} node(s)"

    def redo(self):
        """Paste nodes with offset and restore connections."""
        from src.features.composite.core.node_engine import NODE_REGISTRY

        self._pasted_node_ids = []
        self._created_connections = []
        idx_to_new_id: Dict[int, str] = {}  # clipboard index -> new node ID

        # AUD-10605: clipboard payloads may come from external JSON — validate
        # every field before use instead of hard-indexing, so a malformed
        # entry is skipped rather than aborting the whole redo with half of
        # the nodes already added to the graph.
        nodes_data = (self.clipboard_data.get('nodes', [])
                      if isinstance(self.clipboard_data, dict) else [])
        connections_data = (self.clipboard_data.get('connections', [])
                            if isinstance(self.clipboard_data, dict) else [])
        if not isinstance(nodes_data, (list, tuple)):
            nodes_data = []
        if not isinstance(connections_data, (list, tuple)):
            connections_data = []

        # First pass: create nodes
        for i, data in enumerate(nodes_data):
            if not isinstance(data, dict):
                continue
            node_type = data.get('type')
            node_class = (NODE_REGISTRY.get(node_type)
                          if isinstance(node_type, str) else None)
            if not node_class:
                continue

            new_id = str(uuid.uuid4())[:8]
            node = node_class(node_id=new_id)

            # Restore parameters through set_parameter so per-node validation,
            # dirty flags and revisions apply (same as AddNodeCommand) instead
            # of bypassing them with a wholesale dict assignment.
            params = data.get('parameters')
            if isinstance(params, dict):
                for k, v in params.items():
                    node.set_parameter(k, v)

            pos = data.get('position')
            if (isinstance(pos, (list, tuple)) and len(pos) == 2
                    and isinstance(pos[0], (int, float))
                    and isinstance(pos[1], (int, float))):
                node.position = [
                    float(pos[0]) + self.offset[0],
                    float(pos[1]) + self.offset[1]
                ]
            else:
                node.position = [float(self.offset[0]), float(self.offset[1])]

            self.graph.add_node(node)
            self._pasted_node_ids.append(new_id)
            idx_to_new_id[i] = new_id

        # Second pass: restore internal connections
        for conn in connections_data:
            if not isinstance(conn, dict):
                continue
            src_idx = conn.get('src_idx')
            dst_idx = conn.get('dst_idx')
            src_id = (idx_to_new_id.get(src_idx)
                      if isinstance(src_idx, int) else None)
            dst_id = (idx_to_new_id.get(dst_idx)
                      if isinstance(dst_idx, int) else None)
            src_port = conn.get('src_port')
            dst_port = conn.get('dst_port')
            if (src_id and dst_id
                    and isinstance(src_port, str) and isinstance(dst_port, str)):
                success = self.graph.connect(
                    src_id, src_port,
                    dst_id, dst_port
                )
                if success:
                    self._created_connections.append(
                        (src_id, src_port, dst_id, dst_port)
                    )

        return self._pasted_node_ids

    def undo(self) -> None:
        """Remove pasted nodes and their connections."""
        # Remove connections first (they will be auto-removed when nodes are deleted,
        # but we track them explicitly for clarity)
        for src_id, src_port, dst_id, dst_port in self._created_connections:
            self.graph.disconnect(src_id, src_port, dst_id, dst_port)

        # Remove nodes
        for node_id in self._pasted_node_ids:
            self.graph.remove_node(node_id)

    def get_pasted_ids(self) -> List[str]:
        """Return IDs of pasted nodes."""
        return self._pasted_node_ids


@dataclass
class DuplicateNodesCommand:
    """Command to duplicate nodes with their internal connections.

    Creates new nodes with new IDs and restores internal connections
    among the duplicated nodes using ID mapping.
    """

    graph: NodeGraph
    node_ids: List[str]
    offset: Tuple[float, float] = (50, 50)

    def __post_init__(self) -> None:
        self._duplicated_ids: List[str] = []
        self._id_mapping: Dict[str, str] = {}  # original_id -> duplicated_id
        self._created_connections: List[Tuple[str, str, str, str]] = []  # For undo
        self.description = f"Duplicate {len(self.node_ids)} node(s)"

    def redo(self):
        """Duplicate nodes with offset and restore internal connections."""
        from src.features.composite.core.node_engine import NODE_REGISTRY

        self._duplicated_ids = []
        self._id_mapping = {}
        self._created_connections = []

        # First pass: create duplicated nodes
        for node_id in self.node_ids:
            node = self.graph.get_node(node_id)
            if node:
                new_id = str(uuid.uuid4())[:8]

                # Create node instance from registry
                node_class = NODE_REGISTRY.get(node.node_type)
                if not node_class:
                    continue

                new_node = node_class(node_id=new_id)
                new_node.parameters = dict(node.parameters)
                new_node.position = [
                    node.position[0] + self.offset[0],
                    node.position[1] + self.offset[1]
                ]

                self.graph.add_node(new_node)
                self._duplicated_ids.append(new_id)
                self._id_mapping[node_id] = new_id

        # Second pass: restore internal connections among duplicated nodes
        for conn in self.graph.connections:
            # Only restore connections where both ends are in the duplicated set
            if conn.src_node_id in self._id_mapping and conn.dst_node_id in self._id_mapping:
                new_src_id = self._id_mapping[conn.src_node_id]
                new_dst_id = self._id_mapping[conn.dst_node_id]
                success = self.graph.connect(
                    new_src_id, conn.src_port_name,
                    new_dst_id, conn.dst_port_name
                )
                if success:
                    self._created_connections.append(
                        (new_src_id, conn.src_port_name, new_dst_id, conn.dst_port_name)
                    )

        return self._duplicated_ids

    def undo(self) -> None:
        """Remove duplicated nodes and their connections."""
        # Remove connections first (they will be auto-removed when nodes are deleted,
        # but we track them explicitly for clarity)
        for src_id, src_port, dst_id, dst_port in self._created_connections:
            self.graph.disconnect(src_id, src_port, dst_id, dst_port)

        # Remove nodes
        for node_id in self._duplicated_ids:
            self.graph.remove_node(node_id)

    def get_duplicated_ids(self) -> List[str]:
        """Return IDs of duplicated nodes."""
        return self._duplicated_ids

    def get_id_mapping(self) -> Dict[str, str]:
        """Return mapping from original IDs to duplicated IDs."""
        return self._id_mapping


# ── Backdrop Commands ──
# Backdrops are UI-only elements that don't affect rendering.
# They are stored in NodeGraph.backdrops dict for serialization.


@dataclass
class CreateBackdropCommand:
    """Command to create a backdrop."""

    graph: NodeGraph
    backdrop_id: str
    backdrop_data: Dict[str, Any]  # {title, color, note, position, size}

    def __post_init__(self) -> None:
        self.description = f"Create backdrop '{self.backdrop_data.get('title', 'Backdrop')}'"

    def redo(self):
        """Create the backdrop."""
        data = dict(self.backdrop_data)
        data['id'] = self.backdrop_id
        self.graph.set_backdrop(self.backdrop_id, data)
        return self.backdrop_id

    def undo(self) -> None:
        """Remove the backdrop."""
        self.graph.remove_backdrop(self.backdrop_id)


@dataclass
class DeleteBackdropCommand:
    """Command to delete a backdrop."""

    graph: NodeGraph
    backdrop_id: str

    def __post_init__(self) -> None:
        self._backdrop_data: Optional[Dict[str, Any]] = None
        self.description = "Delete backdrop"

    def redo(self) -> None:
        """Delete the backdrop, storing data for undo."""
        backdrop_data = self.graph.backdrops.get(self.backdrop_id)
        if backdrop_data:
            self._backdrop_data = dict(backdrop_data)
        self.graph.remove_backdrop(self.backdrop_id)

    def undo(self) -> None:
        """Restore the backdrop."""
        if self._backdrop_data:
            self.graph.set_backdrop(self.backdrop_id, self._backdrop_data)


@dataclass
class DeleteBackdropsCommand:
    """Command to delete multiple backdrops."""

    graph: NodeGraph
    backdrop_ids: List[str]

    def __post_init__(self) -> None:
        self._deleted_data: Dict[str, Dict[str, Any]] = {}
        self.description = f"Delete {len(self.backdrop_ids)} backdrop(s)"

    def redo(self) -> None:
        """Delete the backdrops, storing data for undo."""
        self._deleted_data = {}
        for backdrop_id in self.backdrop_ids:
            backdrop_data = self.graph.backdrops.get(backdrop_id)
            if backdrop_data:
                self._deleted_data[backdrop_id] = dict(backdrop_data)
            self.graph.remove_backdrop(backdrop_id)

    def undo(self) -> None:
        """Restore the backdrops."""
        for backdrop_id, data in self._deleted_data.items():
            self.graph.set_backdrop(backdrop_id, data)


@dataclass
class MoveBackdropCommand:
    """Command to move a backdrop to a new position."""

    graph: NodeGraph
    backdrop_id: str
    old_position: Tuple[float, float]
    new_position: Tuple[float, float]

    def __post_init__(self) -> None:
        self.description = "Move backdrop"

    def redo(self) -> None:
        """Move backdrop to new position."""
        current = self.graph.backdrops.get(self.backdrop_id)
        if current:
            # AUD-10610: never write into the engine's live ``backdrops``
            # dict outside the graph lock — build the updated copy first and
            # hand it to set_backdrop(), which deep-copies under the lock.
            data = dict(current)
            data['position'] = list(self.new_position)
            self.graph.set_backdrop(self.backdrop_id, data)

    def undo(self) -> None:
        """Restore old position."""
        current = self.graph.backdrops.get(self.backdrop_id)
        if current:
            # AUD-10610: same copy-on-write discipline as redo().
            data = dict(current)
            data['position'] = list(self.old_position)
            self.graph.set_backdrop(self.backdrop_id, data)


@dataclass
class ResizeBackdropCommand:
    """Command to resize a backdrop."""

    graph: NodeGraph
    backdrop_id: str
    old_size: Tuple[float, float]
    new_size: Tuple[float, float]

    def __post_init__(self) -> None:
        self.description = "Resize backdrop"

    def redo(self) -> None:
        """Resize backdrop to new size."""
        current = self.graph.backdrops.get(self.backdrop_id)
        if current:
            # AUD-10610: copy-on-write — see MoveBackdropCommand.redo().
            data = dict(current)
            data['size'] = list(self.new_size)
            self.graph.set_backdrop(self.backdrop_id, data)

    def undo(self) -> None:
        """Restore old size."""
        current = self.graph.backdrops.get(self.backdrop_id)
        if current:
            # AUD-10610: copy-on-write — see MoveBackdropCommand.redo().
            data = dict(current)
            data['size'] = list(self.old_size)
            self.graph.set_backdrop(self.backdrop_id, data)


@dataclass
class ModifyBackdropCommand:
    """Command to modify backdrop properties (title, note, color, z_order)."""

    graph: NodeGraph
    backdrop_id: str
    old_data: Dict[str, Any]  # Properties before change
    new_data: Dict[str, Any]  # Properties to change

    def __post_init__(self) -> None:
        self.description = "Modify backdrop"

    def redo(self) -> None:
        """Apply new properties."""
        current = self.graph.backdrops.get(self.backdrop_id)
        if current:
            # AUD-10610: copy-on-write — see MoveBackdropCommand.redo().
            data = dict(current)
            for key, value in self.new_data.items():
                data[key] = value
            self.graph.set_backdrop(self.backdrop_id, data)

    def undo(self) -> None:
        """Restore old properties."""
        current = self.graph.backdrops.get(self.backdrop_id)
        if current:
            # AUD-10610: copy-on-write — see MoveBackdropCommand.redo().
            data = dict(current)
            for key, value in self.old_data.items():
                data[key] = value
            self.graph.set_backdrop(self.backdrop_id, data)
