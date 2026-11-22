"""Bidirectional layer/node provenance for the Composite graph.

Layer metadata and the free-form node graph intentionally remain separate.
This resolver is the read-only bridge between them: UI code can locate the
nodes owned by a layer, while node diagnostics can explain which layer (if
any) a node contributes to. Auto-generated Merge nodes are explicitly marked
read-only so callers do not mistake them for user-authored effects.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, Iterator, List, Optional, Set, Tuple

from .layer_model import Layer


@dataclass(frozen=True)
class NodeProvenance:
    """One node's role inside a layer or managed stack boundary."""

    node_id: str
    layer_id: str
    stack_id: str
    role: str  # source | root | effect | generated_merge | stack_output
    managed: bool
    read_only: bool = False
    origin: str = "layer"  # layer | generated | manual


@dataclass(frozen=True)
class LayerRoles:
    """Stable role contract for one layer's managed image chain (B01).

    ``transform`` is the layer's registered main Transform (never a second
    copy of the values); ``effects`` lists every other node on the chain in
    upstream→downstream order (the chain head included when it is an
    effect); ``foreground`` is the final image feed ``(node_id, port)``
    that plugs into the managed Merge / Output chain.
    """

    source: Optional[str]
    transform: Optional[str]
    effects: Tuple[str, ...]
    foreground: Tuple[str, str]
    managed_merge: Optional[str]
    # The registered transform role points at a node that no longer exists
    # (deleted main Transform) — an identifiable, repairable state.
    transform_missing: bool = False
    # A legacy chain without a registered role offered several transform
    # candidates; no stable reference was written.
    transform_ambiguous: bool = False


def choose_legacy_transform(
        main_chain: Tuple[str, ...],
        transform_ids: "frozenset[str]") -> Optional[str]:
    """Pick the unique legacy Transform on an explicit image main chain.

    Legacy projects predating the stable role field identified the layer
    Transform implicitly.  Only a chain with exactly one Transform node
    yields a stable reference; ambiguous chains stay unresolved so callers
    can surface a repair diagnostic instead of guessing.
    """
    candidates = [node_id for node_id in main_chain if node_id in transform_ids]
    return candidates[0] if len(candidates) == 1 else None


@dataclass(frozen=True)
class LayerProvenance:
    """Complete, immutable description of one layer's graph ownership."""

    layer_id: str
    stack_id: str
    name: str
    root_node_id: str
    source_node_id: Optional[str]
    managed: bool
    chain_node_ids: Tuple[str, ...] = ()
    generated_node_ids: Tuple[str, ...] = ()
    output_node_id: str = ""
    conflict: bool = False


class LayerProvenanceResolver:
    """Resolve layer/node provenance without mutating the graph."""

    _BOUNDARY_TYPES = frozenset({"group", "precomp"})

    def __init__(self, graph: Any) -> None:
        self.graph = graph

    def main_image_chains(self, layers: Iterable[Layer]) -> Iterator[tuple[Layer, list[str]]]:
        """Walk each layer's image chain from its root towards its source."""
        owners: dict[str, set[str]] = {}
        for layer in self.graph.get_all_layers():
            for node_id in (layer.root_node_id, layer.source_node_id,
                            getattr(layer, "transform_node_id", None)):
                if node_id:
                    owners.setdefault(node_id, set()).add(layer.layer_id)
        inputs = {(edge.dst_node_id, edge.dst_port_name): edge.src_node_id
                  for edge in self.graph.connections}
        for layer in layers:
            chain = [layer.root_node_id]
            own_layer = {layer.layer_id}
            while layer.source_node_id and chain[-1] != layer.source_node_id:
                node = self.graph.get_node(chain[-1])
                if node is None or getattr(node, "node_type", "") in self._BOUNDARY_TYPES:
                    break
                port = next((port for port in node.inputs if port.data_type == "image"), None)
                previous_id = inputs.get((node.id, port.name)) if port is not None else None
                if (not previous_id or previous_id in chain
                        or owners.get(previous_id, own_layer) != own_layer):
                    break
                chain.append(previous_id)
            yield layer, chain

    def main_transform(self, layer: Layer, chain: list[str]) -> tuple[str | None, bool, bool]:
        """Return the main Transform ID and missing/ambiguous role indicators."""
        registered = getattr(layer, "transform_node_id", None)
        if registered:
            node = self.graph.get_node(registered)
            if node is not None and node.node_type == "transform" and registered in chain:
                return registered, False, False
            return None, True, False
        candidates = frozenset(node_id for node_id in chain
                               if getattr(self.graph.get_node(node_id), "node_type", "") == "transform")
        candidate = choose_legacy_transform(tuple(chain), candidates)
        return candidate, False, candidate is None and bool(candidates)

    def for_layer(self, layer_id: str,
                  stack_id: Optional[str] = None) -> Optional[LayerProvenance]:
        """Return ownership and the upstream node chain for ``layer_id``."""
        layer = self.graph.get_layer(layer_id)
        if layer is None:
            return None
        stack = self._stack_for_layer(layer_id, stack_id)
        if stack is None:
            return None
        root_id = str(getattr(layer, "root_node_id", "") or "")
        source_id = getattr(layer, "source_node_id", None)
        chain = self._trace_chain(root_id, str(source_id) if source_id else None,
                                  stack)
        generated = tuple(str(value) for value in
                          (getattr(stack, "generated_merge_node_ids", []) or []))
        managed = bool(getattr(layer, "managed", True)
                       and getattr(stack, "managed", True))
        return LayerProvenance(
            layer_id=str(layer.layer_id),
            stack_id=str(stack.stack_id),
            name=str(getattr(layer, "name", "Layer") or "Layer"),
            root_node_id=root_id,
            source_node_id=str(source_id) if source_id else None,
            managed=managed,
            chain_node_ids=tuple(chain),
            generated_node_ids=generated,
            output_node_id=str(getattr(stack, "output_node_id", "") or ""),
            conflict=self._stack_has_conflict(stack),
        )

    def for_node(self, node_id: str) -> List[NodeProvenance]:
        """Return all layer/stack roles associated with ``node_id``."""
        target = str(node_id or "")
        if not target:
            return []
        result: List[NodeProvenance] = []
        for stack in self.graph.get_layer_stacks().values():
            generated = {
                str(value) for value in
                (getattr(stack, "generated_merge_node_ids", []) or [])
            }
            if target in generated:
                for layer_id in getattr(stack, "layer_ids", []) or []:
                    layer = self.graph.get_layer(layer_id)
                    if layer is None:
                        continue
                    result.append(NodeProvenance(
                        node_id=target,
                        layer_id=str(layer_id),
                        stack_id=str(stack.stack_id),
                        role="generated_merge",
                        managed=bool(getattr(layer, "managed", True)
                                     and getattr(stack, "managed", True)),
                        read_only=True,
                        origin="generated",
                    ))
                continue

            output_id = str(getattr(stack, "output_node_id", "") or "")
            if target == output_id:
                result.append(NodeProvenance(
                    node_id=target, layer_id="", stack_id=str(stack.stack_id),
                    role="stack_output", managed=bool(getattr(stack, "managed", True)),
                    read_only=True, origin="generated"))

            for layer_id in getattr(stack, "layer_ids", []) or []:
                provenance = self.for_layer(str(layer_id), str(stack.stack_id))
                if provenance is None or target not in provenance.chain_node_ids:
                    continue
                layer = self.graph.get_layer(layer_id)
                if layer is None:
                    continue
                if target == provenance.source_node_id:
                    role = "source"
                elif target == provenance.root_node_id:
                    role = "root"
                elif target == getattr(layer, "transform_node_id", None):
                    # B01: a registered main Transform that is not the chain
                    # head used to be misreported as a plain effect.
                    role = "transform"
                else:
                    role = "effect"
                result.append(NodeProvenance(
                    node_id=target,
                    layer_id=str(layer_id),
                    stack_id=str(stack.stack_id),
                    role=role,
                    managed=bool(getattr(layer, "managed", True)
                                 and getattr(stack, "managed", True)),
                    read_only=False,
                    origin="manual" if not getattr(layer, "managed", True)
                    else "layer",
                ))
        return result

    def node_ids_for_layer(self, layer_id: str,
                           stack_id: Optional[str] = None) -> List[str]:
        """Return the ordered layer chain, root first and source last."""
        provenance = self.for_layer(layer_id, stack_id)
        return list(provenance.chain_node_ids) if provenance else []

    def layer_ids_for_node(self, node_id: str) -> List[str]:
        """Return unique layer IDs that contain or own ``node_id``."""
        seen: Set[str] = set()
        result: List[str] = []
        for item in self.for_node(node_id):
            if not item.layer_id or item.layer_id in seen:
                continue
            seen.add(item.layer_id)
            result.append(item.layer_id)
        return result

    def _stack_for_layer(self, layer_id: str,
                         stack_id: Optional[str]) -> Optional[Any]:
        if stack_id:
            stack = self.graph.get_layer_stack(stack_id)
            if stack is not None and layer_id in (getattr(stack, "layer_ids", []) or []):
                return stack
        for stack in self.graph.get_layer_stacks().values():
            if layer_id in (getattr(stack, "layer_ids", []) or []):
                return stack
        return None

    def _trace_chain(self, root_id: str, source_id: Optional[str],
                     stack: Any) -> List[str]:
        if not root_id or self.graph.get_node(root_id) is None:
            return []
        stop_ids = {
            str(value) for value in
            (getattr(stack, "generated_merge_node_ids", []) or [])
        }
        output_id = str(getattr(stack, "output_node_id", "") or "")
        if output_id:
            stop_ids.add(output_id)

        incoming: Dict[str, List[str]] = {}
        for connection in getattr(self.graph, "connections", []) or []:
            incoming.setdefault(str(connection.dst_node_id), []).append(
                str(connection.src_node_id))

        result: List[str] = []
        visited: Set[str] = set()

        # AUD-48015: iterative pre-order DFS.  The previous nested-recursive
        # ``visit`` blew Python's recursion limit on pathological long
        # upstream chains (a linear 2k-node chain raises RecursionError).
        # Pushing upstream ids in reverse keeps the exact visit order of
        # the recursive version.
        pending: List[str] = [str(root_id)]
        while pending:
            node_id = pending.pop()
            if node_id in visited or node_id in stop_ids:
                continue
            node = self.graph.get_node(node_id)
            if node is None:
                continue
            visited.add(node_id)
            result.append(node_id)
            if node_id == source_id:
                continue
            if getattr(node, "node_type", "") in self._BOUNDARY_TYPES:
                continue
            for upstream_id in reversed(incoming.get(node_id, [])):
                pending.append(upstream_id)

        return result

    def _stack_has_conflict(self, stack: Any) -> bool:
        """Detect an external source on a managed Merge input."""
        if not getattr(stack, "managed", True):
            return False
        roots = {
            str(self.graph.get_layer(layer_id).root_node_id)
            for layer_id in getattr(stack, "layer_ids", []) or []
            if self.graph.get_layer(layer_id) is not None
        }
        allowed = roots | {
            str(value) for value in
            (getattr(stack, "generated_merge_node_ids", []) or [])
        }
        merge_ids = set(getattr(stack, "generated_merge_node_ids", []) or [])
        for connection in getattr(self.graph, "connections", []) or []:
            if (connection.dst_node_id in merge_ids
                    and connection.dst_port_name in {"A", "B"}
                    and connection.src_node_id not in allowed):
                return True
        return False
