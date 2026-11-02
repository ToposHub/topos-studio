"""Undoable, UI-only node-editor drafts.

This module deliberately has no engine, project, renderer or Qt dependency.
Draft edits describe the interaction prototype; they never change a grade in
the running image engine.  Production graph commands are a separate integration
step documented in ``docs/color_nodes_backend_implementation_plan.md``.
"""

from __future__ import annotations

import copy
import math
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable, Mapping


class NodeKind(str, Enum):
    SOURCE = "source"
    OUTPUT = "output"
    ALPHA_OUTPUT = "alpha_output"
    CORRECTOR = "corrector"
    PARALLEL_MIXER = "parallel_mixer"
    LAYER_MIXER = "layer_mixer"
    KEY_MIXER = "key_mixer"
    SPLITTER = "splitter"
    COMBINER = "combiner"
    COMPOUND = "compound"


@dataclass
class NodeDraft:
    id: str
    kind: NodeKind
    label: str = ""
    x: float = 0.0
    y: float = 0.0
    number: int = 0
    enabled: bool = True
    locked: bool = False
    color: str = ""
    input_count: int = 2
    grade: dict[str, Any] = field(default_factory=dict)
    source_layer_id: str = ""
    cache_mode: str = "auto"
    shared_id: str = ""
    children: GraphDraft | None = None
    input_mappings: dict[str, tuple[str, str]] = field(default_factory=dict)
    output_mappings: dict[str, tuple[str, str]] = field(default_factory=dict)


@dataclass(frozen=True)
class ConnectionDraft:
    src_node: str
    src_port: str
    dst_node: str
    dst_port: str


@dataclass
class GraphDraft:
    nodes: dict[str, NodeDraft] = field(default_factory=dict)
    connections: list[ConnectionDraft] = field(default_factory=list)
    active_id: str = ""
    selected_ids: set[str] = field(default_factory=set)
    next_number: int = 1


_TERMINALS = {NodeKind.SOURCE, NodeKind.OUTPUT, NodeKind.ALPHA_OUTPUT}
_NUMBERED = {NodeKind.CORRECTOR, NodeKind.COMPOUND}
_IMAGE_MIXERS = {NodeKind.PARALLEL_MIXER, NodeKind.LAYER_MIXER}
_MIXERS = _IMAGE_MIXERS | {NodeKind.KEY_MIXER}
_PASSTHROUGH = {NodeKind.CORRECTOR, NodeKind.COMPOUND}


def node_ports(node: NodeDraft) -> dict[str, tuple[str, str]]:
    """Return presentation port names mapped to (direction, signal type)."""
    if node.kind == NodeKind.SOURCE:
        return {"image_out": ("out", "image")}
    if node.kind == NodeKind.OUTPUT:
        return {"image_in": ("in", "image")}
    if node.kind == NodeKind.ALPHA_OUTPUT:
        return {"key_in": ("in", "key")}
    if node.kind in _MIXERS:
        signal = "key" if node.kind == NodeKind.KEY_MIXER else "image"
        return {
            **{f"{signal}_in_{i}": ("in", signal)
               for i in range(node.input_count)},
            f"{signal}_out": ("out", signal),
        }
    if node.kind == NodeKind.SPLITTER:
        return {"image_in": ("in", "image"),
                **{channel: ("out", "image") for channel in ("red", "green", "blue")}}
    if node.kind == NodeKind.COMBINER:
        return {**{channel: ("in", "image") for channel in ("red", "green", "blue")},
                "image_out": ("out", "image")}
    ports = {"image_in": ("in", "image"), "image_out": ("out", "image"),
             "key_in": ("in", "key"), "key_out": ("out", "key")}
    if node.kind == NodeKind.COMPOUND:
        for name in node.input_mappings:
            ports[name] = ("in", "key" if name.startswith("key") else "image")
        for name in node.output_mappings:
            ports[name] = ("out", "key" if name.startswith("key") else "image")
    return ports


class NodeEditorState:
    """Mutable presentation draft with whole-gesture undo/redo snapshots.

    Selection does not enter history.  ``move_nodes`` is a final drag commit:
    the view should move its graphics items freely and call it once on release.
    Every failed connection preserves both topology and the undo stack.
    """

    def __init__(self, graph: GraphDraft | None = None) -> None:
        self._undo: list[GraphDraft] = []
        self._redo: list[GraphDraft] = []
        self.graph = self.default_graph()
        if graph is not None:
            self.load_graph(graph)

    @staticmethod
    def default_graph() -> GraphDraft:
        source = NodeDraft("source", NodeKind.SOURCE, x=-180.0, y=110.0)
        corrector = NodeDraft(str(uuid.uuid4()), NodeKind.CORRECTOR, number=1)
        output = NodeDraft("output", NodeKind.OUTPUT, x=480.0, y=110.0)
        return GraphDraft(
            nodes={node.id: node for node in (source, corrector, output)},
            connections=[ConnectionDraft(source.id, "image_out", corrector.id, "image_in"),
                         ConnectionDraft(corrector.id, "image_out", output.id, "image_in")],
            active_id=corrector.id, selected_ids={corrector.id}, next_number=2,
        )

    @property
    def can_undo(self) -> bool:
        return bool(self._undo)

    @property
    def can_redo(self) -> bool:
        return bool(self._redo)

    def snapshot(self) -> GraphDraft:
        return copy.deepcopy(self.graph)

    def load_graph(self, graph: GraphDraft) -> None:
        """Replace a draft without emitting an edit or preserving history."""
        if not isinstance(graph, GraphDraft):
            raise TypeError("graph must be a GraphDraft")
        self._check_graph_bounds(graph)
        candidate = copy.deepcopy(graph)
        self._validate_draft(candidate)
        self.graph = candidate
        self._undo.clear()
        self._redo.clear()

    @staticmethod
    def _check_graph_bounds(graph: GraphDraft) -> None:
        """Reject recursive/deep external compound payloads before deepcopy."""
        total_nodes, total_edges = 0, 0

        def visit(current: GraphDraft, ancestors: set[int], depth: int) -> None:
            nonlocal total_nodes, total_edges
            if not isinstance(current, GraphDraft):
                raise TypeError("compound children must be a GraphDraft")
            if id(current) in ancestors or depth > 16:
                raise ValueError("compound graph nesting is recursive or exceeds 16 levels")
            if not isinstance(current.nodes, dict) or not isinstance(current.connections, list):
                raise TypeError("invalid graph collections")
            total_nodes += len(current.nodes)
            total_edges += len(current.connections)
            if total_nodes > 4096 or total_edges > 32768:
                raise ValueError("presentation graph exceeds size limits")
            for node in current.nodes.values():
                if not isinstance(node, NodeDraft):
                    raise TypeError("graph nodes must be NodeDraft instances")
                if node.children is not None:
                    visit(node.children, ancestors | {id(current)}, depth + 1)

        visit(graph, set(), 0)

    @staticmethod
    def _validate_draft(candidate: GraphDraft) -> None:
        if not isinstance(candidate.next_number, int) or candidate.next_number < 1:
            raise ValueError("next_number must be a positive integer")
        if not isinstance(candidate.selected_ids, set):
            raise TypeError("selected_ids must be a set")
        if any(not isinstance(edge, ConnectionDraft) for edge in candidate.connections):
            raise TypeError("graph edges must be ConnectionDraft instances")
        if len(candidate.connections) != len(set(candidate.connections)):
            raise ValueError("duplicate connections")
        for node_id, node in candidate.nodes.items():
            if not isinstance(node, NodeDraft) or node_id != node.id or not node_id:
                raise ValueError("invalid node identity")
            node.kind = NodeKind(node.kind)
            if not math.isfinite(node.x) or not math.isfinite(node.y):
                raise ValueError("invalid node position")
            if not 1 <= node.input_count <= 64:
                raise ValueError("input_count must be between 1 and 64")
            if not isinstance(node.number, int) or node.number < 0:
                raise ValueError("node number must be a nonnegative integer")
            if node.children is not None:
                NodeEditorState._validate_draft(node.children)
            for mappings, direction in ((node.input_mappings, "in"),
                                        (node.output_mappings, "out")):
                if not isinstance(mappings, dict):
                    raise TypeError("compound boundary mappings must be dictionaries")
                for outer_port, endpoint in mappings.items():
                    if (node.kind != NodeKind.COMPOUND or node.children is None
                            or not isinstance(outer_port, str)
                            or not isinstance(endpoint, (tuple, list)) or len(endpoint) != 2
                            or not all(isinstance(part, str) for part in endpoint)):
                        raise ValueError("invalid compound boundary mapping")
                    member = node.children.nodes.get(endpoint[0])
                    signal = "key" if outer_port.startswith("key") else "image"
                    if member is None or node_ports(member).get(endpoint[1]) != (direction, signal):
                        raise ValueError("dangling or incompatible compound boundary")
        for connection in candidate.connections:
            rest = [edge for edge in candidate.connections if edge != connection]
            if not NodeEditorState._connection_valid(candidate, connection, rest):
                raise ValueError("invalid graph connection")
        candidate.selected_ids.intersection_update(candidate.nodes)
        if candidate.active_id not in candidate.nodes:
            candidate.active_id = ""
        candidate.next_number = max(candidate.next_number,
                                    max((n.number for n in candidate.nodes.values()), default=0) + 1)

    def ports(self, node_id: str) -> dict[str, tuple[str, str]]:
        node = self.graph.nodes.get(node_id)
        return node_ports(node) if node is not None else {}

    def select(self, ids: Iterable[str], active_id: str = "") -> None:
        selected = set(ids) & self.graph.nodes.keys()
        self.graph.selected_ids = selected
        if active_id in selected:
            self.graph.active_id = active_id
        elif self.graph.active_id not in selected:
            self.graph.active_id = next((key for key in self.graph.nodes if key in selected), "")

    def _commit(self, graph: GraphDraft) -> None:
        self._undo.append(self.snapshot())
        if len(self._undo) > 100:
            del self._undo[0]
        self._redo.clear()
        self.graph = graph

    @staticmethod
    def _new_node(graph: GraphDraft, kind: NodeKind, x: float, y: float) -> NodeDraft:
        node = NodeDraft(str(uuid.uuid4()), kind, x=x, y=y,
                         number=graph.next_number if kind in _NUMBERED else 0)
        if kind in _NUMBERED:
            graph.next_number += 1
        graph.nodes[node.id] = node
        return node

    @staticmethod
    def _activate(graph: GraphDraft, node_id: str) -> None:
        graph.active_id = node_id
        graph.selected_ids = {node_id} if node_id else set()

    @staticmethod
    def _connection_valid(graph: GraphDraft, edge: ConnectionDraft,
                          connections: list[ConnectionDraft]) -> bool:
        if edge.src_node == edge.dst_node:
            return False
        source, target = graph.nodes.get(edge.src_node), graph.nodes.get(edge.dst_node)
        if source is None or target is None:
            return False
        source_port, target_port = node_ports(source).get(edge.src_port), node_ports(target).get(edge.dst_port)
        if (source_port is None or target_port is None or source_port[0] != "out"
                or target_port[0] != "in" or source_port[1] != target_port[1]):
            return False
        if any(c.dst_node == edge.dst_node and c.dst_port == edge.dst_port for c in connections):
            return False
        # A new A→B edge is cyclic if B already reaches A, across either signal type.
        stack, visited = [edge.dst_node], set()
        while stack:
            current = stack.pop()
            if current == edge.src_node:
                return False
            if current not in visited:
                visited.add(current)
                stack.extend(c.dst_node for c in connections if c.src_node == current)
        return True

    def move_nodes(self, positions: Mapping[str, tuple[float, float]]) -> bool:
        graph = self.snapshot()
        if not self._move_positions(graph, positions):
            return False
        self._commit(graph)
        return True

    @staticmethod
    def _move_positions(graph: GraphDraft,
                        positions: Mapping[str, tuple[float, float]]) -> bool:
        changes: dict[str, tuple[float, float]] = {}
        for key, position in positions.items():
            if key not in graph.nodes or graph.nodes[key].locked:
                continue
            x, y = float(position[0]), float(position[1])
            if not math.isfinite(x) or not math.isfinite(y):
                raise ValueError("node position must be finite")
            if (x, y) != (graph.nodes[key].x, graph.nodes[key].y):
                changes[key] = (x, y)
        if not changes:
            return False
        for key, (x, y) in changes.items():
            graph.nodes[key].x, graph.nodes[key].y = x, y
        return True

    def move_nodes_and_insert(self, positions: Mapping[str, tuple[float, float]],
                              node_id: str, edge: ConnectionDraft) -> bool:
        """Commit one drag gesture; return whether the node entered the wire.

        A refused insertion still commits valid movement, with one undo step.
        Existing connections of an attached node are never discarded to make
        it fit the target wire.
        """
        graph = self.snapshot()
        moved = self._move_positions(graph, positions)
        inserted = self._insert(graph, node_id, edge)
        if moved or inserted:
            self._commit(graph)
        return inserted

    def add_node(self, kind: NodeKind, x: float = 0.0, y: float = 0.0) -> str:
        kind = NodeKind(kind)
        if kind in {NodeKind.SOURCE, NodeKind.OUTPUT}:
            return ""
        if not math.isfinite(x) or not math.isfinite(y):
            raise ValueError("node position must be finite")
        graph = self.snapshot()
        node = self._new_node(graph, kind, x, y)
        self._activate(graph, node.id)
        self._commit(graph)
        return node.id

    @staticmethod
    def _serial(graph: GraphDraft, before: bool = False) -> NodeDraft:
        active = graph.nodes.get(graph.active_id)
        if active is None:
            active = next((n for n in graph.nodes.values() if n.kind == NodeKind.SOURCE), None)
        x, y = (active.x, active.y) if active is not None else (0.0, 0.0)
        node = NodeEditorState._new_node(graph, NodeKind.CORRECTOR,
                                        x - 130.0 if before else x + 130.0, y)
        if active is not None:
            # Output terminals accept insertion on their input; source terminals on output.
            incoming = before or active.kind in {NodeKind.OUTPUT, NodeKind.ALPHA_OUTPUT}
            if incoming and "image_in" in node_ports(active):
                edges = [c for c in graph.connections if c.dst_node == active.id and c.dst_port == "image_in"]
                graph.connections = [c for c in graph.connections if c not in edges]
                graph.connections.extend(ConnectionDraft(c.src_node, c.src_port, node.id, "image_in") for c in edges)
                graph.connections.append(ConnectionDraft(node.id, "image_out", active.id, "image_in"))
            elif "image_out" in node_ports(active):
                edges = [c for c in graph.connections if c.src_node == active.id and c.src_port == "image_out"]
                graph.connections = [c for c in graph.connections if c not in edges]
                graph.connections.append(ConnectionDraft(active.id, "image_out", node.id, "image_in"))
                graph.connections.extend(ConnectionDraft(node.id, "image_out", c.dst_node, c.dst_port) for c in edges)
        NodeEditorState._activate(graph, node.id)
        return node

    def add_serial(self, before: bool = False) -> str:
        graph = self.snapshot()
        node = self._serial(graph, before)
        self._commit(graph)
        return node.id

    def add_branch(self, kind: NodeKind) -> str:
        kind = NodeKind(kind)
        if kind not in _IMAGE_MIXERS:
            raise ValueError("branch kind must be a parallel or layer mixer")
        graph = self.snapshot()
        active = graph.nodes.get(graph.active_id)
        if active is None or active.kind not in _PASSTHROUGH:
            return ""
        upstream = next((c for c in graph.connections if c.dst_node == active.id and c.dst_port == "image_in"), None)
        if upstream is None:
            return ""
        branch_edges = [c for c in graph.connections if c.src_node == active.id and c.src_port == "image_out"]
        mixer = next((graph.nodes[c.dst_node] for c in branch_edges
                      if graph.nodes[c.dst_node].kind == kind), None)
        sibling_y = active.y + 95.0
        while any(abs(n.x - active.x) < 70.0 and abs(n.y - sibling_y) < 60.0 for n in graph.nodes.values()):
            sibling_y += 95.0
        node = self._new_node(graph, NodeKind.CORRECTOR, active.x, sibling_y)
        graph.connections.append(ConnectionDraft(upstream.src_node, upstream.src_port, node.id, "image_in"))
        if mixer is None:
            mixer = self._new_node(graph, kind, active.x + 150.0, active.y + 40.0)
            graph.connections = [c for c in graph.connections if c not in branch_edges]
            graph.connections.append(ConnectionDraft(active.id, "image_out", mixer.id, "image_in_0"))
            graph.connections.extend(ConnectionDraft(mixer.id, "image_out", c.dst_node, c.dst_port) for c in branch_edges)
            index = 1
        else:
            occupied = {c.dst_port for c in graph.connections if c.dst_node == mixer.id}
            index = next((i for i in range(mixer.input_count) if f"image_in_{i}" not in occupied), mixer.input_count)
            if index >= 64:
                return ""
            mixer.input_count = max(mixer.input_count, index + 1)
        graph.connections.append(ConnectionDraft(node.id, "image_out", mixer.id, f"image_in_{index}"))
        self._activate(graph, node.id)
        self._commit(graph)
        return node.id

    def add_outside(self) -> str:
        graph = self.snapshot()
        previous = graph.nodes.get(graph.active_id)
        if previous is None or "key_out" not in node_ports(previous):
            return ""
        node = self._serial(graph)
        node.grade["key_invert"] = True
        graph.connections.append(ConnectionDraft(previous.id, "key_out", node.id, "key_in"))
        self._commit(graph)
        return node.id

    def add_splitter_combiner(self) -> tuple[str, str]:
        graph = self.snapshot()
        active = graph.nodes.get(graph.active_id)
        if active is None or "image_out" not in node_ports(active):
            return ("", "")
        edges = [c for c in graph.connections if c.src_node == active.id and c.src_port == "image_out"]
        graph.connections = [c for c in graph.connections if c not in edges]
        splitter = self._new_node(graph, NodeKind.SPLITTER, active.x + 130.0, active.y)
        combiner = self._new_node(graph, NodeKind.COMBINER, active.x + 400.0, active.y)
        graph.connections.append(ConnectionDraft(active.id, "image_out", splitter.id, "image_in"))
        for index, channel in enumerate(("red", "green", "blue")):
            node = self._new_node(graph, NodeKind.CORRECTOR, active.x + 260.0, active.y + (index - 1) * 95.0)
            node.label = channel.title()
            graph.connections.extend((ConnectionDraft(splitter.id, channel, node.id, "image_in"),
                                      ConnectionDraft(node.id, "image_out", combiner.id, channel)))
        graph.connections.extend(ConnectionDraft(combiner.id, "image_out", c.dst_node, c.dst_port) for c in edges)
        self._activate(graph, splitter.id)
        self._commit(graph)
        return splitter.id, combiner.id

    def add_alpha_output(self) -> str:
        existing = next((n.id for n in self.graph.nodes.values() if n.kind == NodeKind.ALPHA_OUTPUT), "")
        if existing:
            self.select({existing}, existing)
            return existing
        output = next((n for n in self.graph.nodes.values() if n.kind == NodeKind.OUTPUT), None)
        return self.add_node(NodeKind.ALPHA_OUTPUT, output.x if output else 480.0,
                             output.y + 100.0 if output else 210.0)

    def add_key_mixer(self) -> str:
        """Add a matte-only mixer fed by the selected node's key output."""
        graph = self.snapshot()
        active = graph.nodes.get(graph.active_id)
        if active is None or node_ports(active).get("key_out") != ("out", "key"):
            return ""
        mixer = self._new_node(graph, NodeKind.KEY_MIXER, active.x + 150.0, active.y + 95.0)
        graph.connections.append(ConnectionDraft(active.id, "key_out", mixer.id, "key_in_0"))
        self._activate(graph, mixer.id)
        self._commit(graph)
        return mixer.id

    def remove_alpha_output(self, node_id: str) -> bool:
        """Remove only an alpha-output terminal and all of its Key wires."""
        node = self.graph.nodes.get(node_id)
        if node is None or node.kind != NodeKind.ALPHA_OUTPUT:
            return False
        return self.delete_nodes([node_id], reconnect=False)

    def morph_mixer(self, node_id: str) -> bool:
        """Switch Parallel↔Layer without replacing node identity or ports."""
        node = self.graph.nodes.get(node_id)
        if node is None or node.kind not in _IMAGE_MIXERS or node.locked:
            return False
        graph = self.snapshot()
        graph.nodes[node_id].kind = (NodeKind.LAYER_MIXER if node.kind == NodeKind.PARALLEL_MIXER
                                     else NodeKind.PARALLEL_MIXER)
        self._commit(graph)
        return True

    def connect(self, edge: ConnectionDraft) -> bool:
        if edge in self.graph.connections:
            return False
        graph = self.snapshot()
        remaining = [c for c in graph.connections if not (c.dst_node == edge.dst_node and c.dst_port == edge.dst_port)]
        if not self._connection_valid(graph, edge, remaining):
            return False
        graph.connections = remaining + [edge]
        self._commit(graph)
        return True

    def disconnect(self, edge: ConnectionDraft) -> bool:
        return self.disconnect_many([edge])

    def disconnect_many(self, edges: Iterable[ConnectionDraft]) -> bool:
        """Delete the existing selected wires in one reversible gesture."""
        requested = set(edges)
        if not requested.intersection(self.graph.connections):
            return False
        graph = self.snapshot()
        graph.connections = [edge for edge in graph.connections if edge not in requested]
        self._commit(graph)
        return True

    def insert_on_connection(self, node_id: str, edge: ConnectionDraft) -> bool:
        graph = self.snapshot()
        if not self._insert(graph, node_id, edge):
            return False
        self._commit(graph)
        return True

    @staticmethod
    def _insert(graph: GraphDraft, node_id: str, edge: ConnectionDraft) -> bool:
        node = graph.nodes.get(node_id)
        if (node is None or node.kind != NodeKind.CORRECTOR or node.locked
                or edge not in graph.connections):
            return False
        if node_id in {edge.src_node, edge.dst_node}:
            return False
        # A connected node must first be extracted; insertion never discards branches.
        if any(c.src_node == node_id or c.dst_node == node_id for c in graph.connections):
            return False
        rest = [c for c in graph.connections if c != edge]
        first = ConnectionDraft(edge.src_node, edge.src_port, node_id, "image_in")
        second = ConnectionDraft(node_id, "image_out", edge.dst_node, edge.dst_port)
        if (not NodeEditorState._connection_valid(graph, first, rest)
                or not NodeEditorState._connection_valid(graph, second, rest + [first])):
            return False
        graph.connections = rest + [first, second]
        return True

    @staticmethod
    def _remove(graph: GraphDraft, ids: set[str], reconnect: bool,
                delete: bool) -> None:
        old_connections = list(graph.connections)
        remaining = [c for c in old_connections if c.src_node not in ids and c.dst_node not in ids]
        if reconnect:
            # Trace through selected correctors only. Never choose one arbitrary
            # mixer input, or turn a matte/key connection into an image connection.
            for edge in old_connections:
                if edge.src_node not in ids or edge.dst_node in ids or edge.src_port != "image_out":
                    continue
                current = edge.src_node
                visited: set[str] = set()
                replacement: ConnectionDraft | None = None
                while current in ids and current not in visited:
                    visited.add(current)
                    node = graph.nodes[current]
                    if node.kind != NodeKind.CORRECTOR:
                        break
                    incoming = [c for c in old_connections if c.dst_node == current and c.dst_port == "image_in"]
                    if len(incoming) != 1:
                        break
                    source = incoming[0]
                    if source.src_node not in ids:
                        replacement = ConnectionDraft(source.src_node, source.src_port, edge.dst_node, edge.dst_port)
                        break
                    if source.src_port != "image_out":
                        break
                    current = source.src_node
                if replacement is not None and NodeEditorState._connection_valid(graph, replacement, remaining):
                    remaining.append(replacement)
        graph.connections = remaining
        if delete:
            for node_id in ids:
                del graph.nodes[node_id]
            graph.selected_ids.difference_update(ids)
            if graph.active_id in ids:
                next_active = next((n.id for n in graph.nodes.values() if n.kind == NodeKind.CORRECTOR), "")
                NodeEditorState._activate(graph, next_active)

    def delete_nodes(self, ids: Iterable[str], reconnect: bool = True) -> bool:
        selected = {key for key in ids if key in self.graph.nodes
                    and self.graph.nodes[key].kind not in {NodeKind.SOURCE, NodeKind.OUTPUT}
                    and not self.graph.nodes[key].locked}
        if not selected:
            return False
        graph = self.snapshot()
        self._remove(graph, selected, reconnect, True)
        self._commit(graph)
        return True

    def extract_nodes(self, ids: Iterable[str]) -> bool:
        selected = {key for key in ids if key in self.graph.nodes and self.graph.nodes[key].kind in _PASSTHROUGH
                    and not self.graph.nodes[key].locked}
        if not selected or not any(c.src_node in selected or c.dst_node in selected for c in self.graph.connections):
            return False
        graph = self.snapshot()
        self._remove(graph, selected, True, False)
        self._commit(graph)
        return True

    def duplicate_nodes(self, ids: Iterable[str]) -> list[str]:
        return self.paste_nodes(self.graph, ids, (30.0, 80.0))

    def paste_nodes(self, graph: GraphDraft, ids: Iterable[str],
                    offset: tuple[float, float] = (80.0, 80.0)) -> list[str]:
        """Paste a selected subgraph with independent identities and one undo.

        External source/output connections are intentionally excluded. The
        caller may then insert the pasted nodes into a wire as another gesture.
        """
        source = NodeEditorState(graph).graph
        dx, dy = float(offset[0]), float(offset[1])
        if not math.isfinite(dx) or not math.isfinite(dy):
            raise ValueError("paste offset must be finite")
        selected = set(ids)
        originals = [n for n in source.nodes.values() if n.id in selected and n.kind not in _TERMINALS]
        if not originals:
            return []
        candidate = self.snapshot()
        mapping: dict[str, str] = {}
        for original in originals:
            node = copy.deepcopy(original)
            node.id = str(uuid.uuid4())
            node.number = candidate.next_number if node.kind in _NUMBERED else 0
            if node.kind in _NUMBERED:
                candidate.next_number += 1
            node.x += dx
            node.y += dy
            # A duplicate is an independent grade; explicit shared-node UI may
            # restore shared_id after the duplicate transaction.
            node.shared_id = ""
            if node.children is not None:
                self._remap_compound_children(node)
            mapping[original.id] = node.id
            candidate.nodes[node.id] = node
        candidate.connections.extend(ConnectionDraft(mapping[c.src_node], c.src_port, mapping[c.dst_node], c.dst_port)
                                     for c in source.connections if c.src_node in mapping and c.dst_node in mapping)
        candidate.selected_ids = set(mapping.values())
        candidate.active_id = next(iter(mapping.values()))
        self._commit(candidate)
        return list(mapping.values())

    @staticmethod
    def _remap_compound_children(compound: NodeDraft) -> None:
        """Give a copied compound independent identities at every depth."""
        children = compound.children
        if children is None:
            return
        identities = {key: str(uuid.uuid4()) for key in children.nodes}
        nodes: dict[str, NodeDraft] = {}
        for old_id, child in children.nodes.items():
            child.id = identities[old_id]
            child.shared_id = ""
            NodeEditorState._remap_compound_children(child)
            nodes[child.id] = child
        children.nodes = nodes
        children.connections = [ConnectionDraft(identities[c.src_node], c.src_port,
                                                identities[c.dst_node], c.dst_port)
                                for c in children.connections]
        children.active_id = identities.get(children.active_id, "")
        children.selected_ids = {identities[key] for key in children.selected_ids if key in identities}
        compound.input_mappings = {name: (identities[key], port)
                                   for name, (key, port) in compound.input_mappings.items()}
        compound.output_mappings = {name: (identities[key], port)
                                    for name, (key, port) in compound.output_mappings.items()}

    def set_node_fields(self, ids: Iterable[str], **fields: Any) -> bool:
        allowed = {"label", "enabled", "locked", "color", "input_count", "grade",
                   "source_layer_id", "cache_mode", "shared_id"}
        if fields.keys() - allowed:
            raise ValueError("unsupported node field")
        if "input_count" in fields and (not isinstance(fields["input_count"], int) or not 1 <= fields["input_count"] <= 64):
            raise ValueError("input_count must be between 1 and 64")
        if "grade" in fields and not isinstance(fields["grade"], dict):
            raise TypeError("grade must be a dict")
        if "cache_mode" in fields and fields["cache_mode"] not in {"auto", "on", "off"}:
            raise ValueError("unknown cache mode")
        for name in {"label", "color", "source_layer_id", "shared_id"} & fields.keys():
            if not isinstance(fields[name], str):
                raise TypeError(f"{name} must be a string")
        for name in {"enabled", "locked"} & fields.keys():
            if not isinstance(fields[name], bool):
                raise TypeError(f"{name} must be a boolean")
        selected = set(ids) & self.graph.nodes.keys()
        if not selected or not fields:
            return False
        graph = self.snapshot()
        changed = False
        for key in selected:
            node = graph.nodes[key]
            if node.kind in _TERMINALS:
                continue
            if node.locked and fields.keys() != {"locked"}:
                continue
            for name, value in fields.items():
                # Grade may contain numpy-backed legacy previews. Replacing a
                # grade is an explicit operation, without ambiguous array ==.
                if name == "grade" or getattr(node, name) != value:
                    setattr(node, name, copy.deepcopy(value))
                    changed = True
            if "input_count" in fields and node.kind in _MIXERS:
                ports = node_ports(node)
                graph.connections = [c for c in graph.connections if c.dst_node != key or c.dst_port in ports]
        if changed:
            self._commit(graph)
        return changed

    def reset_grades(self, ids: Iterable[str]) -> bool:
        selected = {key for key in ids if key in self.graph.nodes and bool(self.graph.nodes[key].grade)
                    and not self.graph.nodes[key].locked}
        return self.set_node_fields(selected, grade={}) if selected else False

    def make_local(self, ids: Iterable[str]) -> bool:
        """Detach shared drafts and unlock them atomically, leaving other locks."""
        selected = {key for key in ids if key in self.graph.nodes
                    and self.graph.nodes[key].kind not in _TERMINALS
                    and bool(self.graph.nodes[key].shared_id)}
        if not selected:
            return False
        graph = self.snapshot()
        for key in selected:
            graph.nodes[key].shared_id = ""
            graph.nodes[key].locked = False
        self._commit(graph)
        return True

    def reset_all(self) -> None:
        self._commit(self.default_graph())

    def make_compound(self, ids: Iterable[str]) -> str:
        selected = {key for key in ids if key in self.graph.nodes and self.graph.nodes[key].kind not in _TERMINALS
                    and not self.graph.nodes[key].locked}
        if not selected:
            return ""
        graph = self.snapshot()
        members = [n for n in graph.nodes.values() if n.id in selected]
        incoming = [c for c in graph.connections if c.src_node not in selected and c.dst_node in selected]
        outgoing = [c for c in graph.connections if c.src_node in selected and c.dst_node not in selected]
        # Collapsing a non-convex selection can introduce a cycle at the boundary.
        external_paths = {c.dst_node for c in outgoing}
        seen: set[str] = set()
        while external_paths:
            current = external_paths.pop()
            if current in selected:
                return ""
            if current not in seen:
                seen.add(current)
                external_paths.update(c.dst_node for c in graph.connections if c.src_node == current)
        compound = self._new_node(graph, NodeKind.COMPOUND,
                                  sum(n.x for n in members) / len(members),
                                  sum(n.y for n in members) / len(members))
        compound.label = "Compound"
        compound.children = GraphDraft(
            nodes={n.id: copy.deepcopy(n) for n in members},
            connections=[c for c in graph.connections if c.src_node in selected and c.dst_node in selected],
            active_id=graph.active_id if graph.active_id in selected else members[0].id,
            selected_ids=set(selected), next_number=graph.next_number,
        )
        outer = [c for c in graph.connections if c.src_node not in selected and c.dst_node not in selected]
        for edge in incoming:
            signal = node_ports(graph.nodes[edge.dst_node])[edge.dst_port][1]
            prefix = f"{signal}_in"
            name = prefix if prefix not in compound.input_mappings else f"{prefix}_{len(compound.input_mappings)}"
            compound.input_mappings[name] = (edge.dst_node, edge.dst_port)
            outer.append(ConnectionDraft(edge.src_node, edge.src_port, compound.id, name))
        # Multiple outgoing consumers share the same compound output port.
        outputs: dict[tuple[str, str], str] = {}
        for edge in outgoing:
            endpoint = (edge.src_node, edge.src_port)
            name = outputs.get(endpoint)
            if name is None:
                signal = node_ports(graph.nodes[edge.src_node])[edge.src_port][1]
                prefix = f"{signal}_out"
                name = prefix if prefix not in compound.output_mappings else f"{prefix}_{len(compound.output_mappings)}"
                compound.output_mappings[name] = endpoint
                outputs[endpoint] = name
            outer.append(ConnectionDraft(compound.id, name, edge.dst_node, edge.dst_port))
        # A detached selection still needs useful ports for a subsequent drag
        # connection. Their anchors are retained when expanding the compound.
        image_inputs = [n for n in members if "image_in" in node_ports(n)]
        image_outputs = [n for n in members if "image_out" in node_ports(n)]
        key_inputs = [n for n in members if "key_in" in node_ports(n)]
        key_outputs = [n for n in members if "key_out" in node_ports(n)]
        if image_inputs:
            compound.input_mappings.setdefault("image_in", (image_inputs[0].id, "image_in"))
        if image_outputs:
            compound.output_mappings.setdefault("image_out", (image_outputs[-1].id, "image_out"))
        if key_inputs:
            compound.input_mappings.setdefault("key_in", (key_inputs[0].id, "key_in"))
        if key_outputs:
            compound.output_mappings.setdefault("key_out", (key_outputs[-1].id, "key_out"))
        for key in selected:
            del graph.nodes[key]
        graph.connections = outer
        self._activate(graph, compound.id)
        self._commit(graph)
        return compound.id

    def expand_compound(self, node_id: str) -> list[str]:
        original = self.graph.nodes.get(node_id)
        if original is None or original.kind != NodeKind.COMPOUND or original.children is None or original.locked:
            return []
        if set(original.children.nodes) & self.graph.nodes.keys():
            return []
        graph = self.snapshot()
        compound = graph.nodes[node_id]
        children = compound.children
        assert children is not None
        if children.nodes:
            offset_x = compound.x - sum(n.x for n in children.nodes.values()) / len(children.nodes)
            offset_y = compound.y - sum(n.y for n in children.nodes.values()) / len(children.nodes)
            for member in children.nodes.values():
                member.x += offset_x
                member.y += offset_y
        outer = [c for c in graph.connections if c.src_node != node_id and c.dst_node != node_id]
        outer.extend(children.connections)
        for edge in graph.connections:
            if edge.dst_node == node_id:
                endpoint = compound.input_mappings.get(edge.dst_port)
                if endpoint is not None:
                    outer.append(ConnectionDraft(edge.src_node, edge.src_port, *endpoint))
            elif edge.src_node == node_id:
                endpoint = compound.output_mappings.get(edge.src_port)
                if endpoint is not None:
                    outer.append(ConnectionDraft(*endpoint, edge.dst_node, edge.dst_port))
        graph.nodes.update(copy.deepcopy(children.nodes))
        del graph.nodes[node_id]
        graph.connections = outer
        graph.selected_ids = set(children.nodes)
        graph.active_id = children.active_id
        self._commit(graph)
        return list(children.nodes)

    def replace_compound_children(self, node_id: str, children: GraphDraft) -> bool:
        """Commit the child-editor draft while preserving all boundary ports.

        Deleting a mapped boundary node or changing its mapped port type is
        rejected atomically. This prevents returning from a compound editor
        with dangling external RGB or Key wires.
        """
        compound = self.graph.nodes.get(node_id)
        if compound is None or compound.kind != NodeKind.COMPOUND or compound.locked:
            return False
        try:
            replacement = NodeEditorState(children).graph
        except (TypeError, ValueError):
            return False
        for mappings, direction in ((compound.input_mappings, "in"),
                                    (compound.output_mappings, "out")):
            for outer_port, (member_id, member_port) in mappings.items():
                member = replacement.nodes.get(member_id)
                expected_signal = "key" if outer_port.startswith("key") else "image"
                if member is None or node_ports(member).get(member_port) != (direction, expected_signal):
                    return False
        graph = self.snapshot()
        graph.nodes[node_id].children = replacement
        self._commit(graph)
        return True

    def auto_layout(self) -> None:
        graph = self.snapshot()
        indegree = {key: 0 for key in graph.nodes}
        for edge in graph.connections:
            indegree[edge.dst_node] += 1
        frontier = [key for key in graph.nodes if indegree[key] == 0]
        level = {key: 0 for key in frontier}
        while frontier:
            key = frontier.pop(0)
            for edge in graph.connections:
                if edge.src_node == key:
                    level[edge.dst_node] = max(level.get(edge.dst_node, 0), level[key] + 1)
                    indegree[edge.dst_node] -= 1
                    if indegree[edge.dst_node] == 0:
                        frontier.append(edge.dst_node)
        rows: dict[int, int] = {}
        changed = False
        for key, node in graph.nodes.items():
            column = level.get(key, 0)
            row = rows.get(column, 0)
            rows[column] = row + 1
            if not node.locked:
                x, y = column * 140.0, row * 95.0
                changed = changed or (node.x, node.y) != (x, y)
                node.x, node.y = x, y
        if changed:
            self._commit(graph)

    def undo(self) -> bool:
        if not self._undo:
            return False
        self._redo.append(self.snapshot())
        self.graph = self._undo.pop()
        return True

    def redo(self) -> bool:
        if not self._redo:
            return False
        self._undo.append(self.snapshot())
        self.graph = self._redo.pop()
        return True
