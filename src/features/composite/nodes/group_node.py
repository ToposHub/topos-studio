"""
Group Node - Container for grouping multiple nodes into a macro/subgraph.

This is a UI-level grouping mechanism that also supports subgraph evaluation.
The GroupNode acts as a visual container that:
- Holds a list of member node IDs
- Provides input/output ports that map to member nodes
- Can be serialized/deserialized with the graph
- Supports enter/exit for editing contained nodes
- Evaluates internal member nodes and outputs their results

Architecture note:
When set_node_graph() is called, GroupNode can evaluate its internal
subgraph by extracting results from already-evaluated member nodes.
The main graph's topological sort ensures members are evaluated before
the group when dependencies are properly set up.
"""

from typing import Any, Dict, List, Optional, Tuple
import logging

from .base_node import Node, Port, TextureResult, ParamDef

logger = logging.getLogger(__name__)


class GroupNode(Node):
    """A container node that groups other nodes together.

    This is a UI-level grouping mechanism for organizing complex node graphs.
    When set_node_graph() is called, the GroupNode can evaluate its internal
    subgraph by extracting results from member nodes that have been evaluated
    by the main graph's topological sort.

    Attributes:
        member_node_ids: List of node IDs contained in this group
        input_mappings: Dict mapping group input port name -> (member_node_id, member_port_name)
        output_mappings: Dict mapping group output port name -> (member_node_id, member_port_name)
        is_expanded: Whether the group is visually expanded in the UI
        group_color: Visual color for the group border in the UI
        _node_graph: Optional reference to the parent NodeGraph for evaluation
        _internal_eval_cache: Cache for internal member evaluation results
    """

    node_type = "group"
    # CP-044: 组织容器不参与通用 mask。
    GENERIC_MASK_MIX = False

    node_title = "Group"
    header_color = "#4A90D9"  # Blue for groups

    # A GroupNode is a renderable graph boundary. The snapshot layer rewires
    # its declared boundary ports to the member graph, so it does not need a
    # second GPU evaluator or a duplicate texture cache.
    IS_PLACEHOLDER = False

    # Metadata
    SEARCH_ALIASES = ["group", "组", "容器", "container", "macro", "subgraph", "子图"]
    # CP-009: 子图执行尚未实现（base_evaluator.PLACEHOLDER_EFFECT_TYPES
    # 透传 + warning）；描述里明示"仅组织用"，避免用户以为组内可独立求值。
    DESCRIPTION = "节点组容器，仅用于组织复杂节点图（当前不参与求值，信号透传；子图执行见 CP-031）"
    TAGS = ["utility", "organize"]

    PARAM_DEFS: List[ParamDef] = [
        ParamDef(name="group_name", ptype="str", default="Group"),
        ParamDef(name="is_expanded", ptype="bool", default=False),
        ParamDef(name="group_color", ptype="str", default="#4A90D9"),
    ]

    def __init__(self, node_id: Optional[str] = None) -> None:
        super().__init__(node_id)

        # Group contents
        self.member_node_ids: List[str] = []

        # Port mappings: external port -> internal (node_id, port_name)
        self.input_mappings: Dict[str, Tuple[str, str]] = {}
        self.output_mappings: Dict[str, Tuple[str, str]] = {}

        # Visual state
        self.is_expanded: bool = False
        self.group_color: str = "#4A90D9"

        # Reference to parent NodeGraph for subgraph evaluation
        self._node_graph: Optional[Any] = None  # Reference to NodeGraph

        # B3-2: promoted ("published") member parameters. Maps the proxy
        # parameter name on THIS node → {"member_id", "member_param",
        # "min", "max", "step", "default"}. The member's actual parameter
        # is driven by an expression binding to the proxy (set via the
        # engine's set_parameter_binding), so editing the proxy on the
        # group drives every bound member. Serialized with the node.
        self.promoted_params: Dict[str, Dict[str, Any]] = {}

        # Internal evaluation cache for member nodes
        self._internal_eval_cache: Dict[str, Dict[str, TextureResult]] = {}

        # Initialize default ports (can be modified by the group)
        self.inputs = [
            Port(name="input_1", direction="input", data_type="image"),
        ]
        self.outputs = [
            Port(name="output_1", direction="output", data_type="image"),
        ]

    def set_node_graph(self, graph: Any) -> None:
        """Set the parent NodeGraph reference for subgraph evaluation.

        This is called by NodeGraph.add_node() to allow the GroupNode
        to access member nodes and their evaluation results.

        Args:
            graph: The parent NodeGraph instance
        """
        self._node_graph = graph

    def add_member(self, node_id: str):
        """Add a node to this group."""
        if node_id not in self.member_node_ids:
            self.member_node_ids.append(node_id)
            self._dirty = True

    def remove_member(self, node_id: str):
        """Remove a node from this group."""
        if node_id in self.member_node_ids:
            self.member_node_ids.remove(node_id)
            # Clean up mappings
            self.input_mappings = {
                k: v for k, v in self.input_mappings.items()
                if v[0] != node_id
            }
            self.output_mappings = {
                k: v for k, v in self.output_mappings.items()
                if v[0] != node_id
            }
            self._dirty = True

    def add_input_port(self, name: str, member_node_id: str, member_port_name: str):
        """Add an input port to the group that maps to an internal node port."""
        self.input_mappings[name] = (member_node_id, member_port_name)
        if not any(p.name == name for p in self.inputs):
            self.inputs.append(Port(name=name, direction="input", data_type="image"))
        self._dirty = True

    def add_output_port(self, name: str, member_node_id: str, member_port_name: str):
        """Add an output port to the group that maps to an internal node port."""
        self.output_mappings[name] = (member_node_id, member_port_name)
        if not any(p.name == name for p in self.outputs):
            self.outputs.append(Port(name=name, direction="output", data_type="image"))
        self._dirty = True

    def remove_input_port(self, name: str):
        """Remove an input port from the group."""
        if name in self.input_mappings:
            del self.input_mappings[name]
            self.inputs = [p for p in self.inputs if p.name != name]
            self._dirty = True

    def remove_output_port(self, name: str):
        """Remove an output port from the group."""
        if name in self.output_mappings:
            del self.output_mappings[name]
            self.outputs = [p for p in self.outputs if p.name != name]
            self._dirty = True

    def get_member_ids(self) -> List[str]:
        """Return the list of member node IDs."""
        return list(self.member_node_ids)

    def contains_node(self, node_id: str) -> bool:
        """Check if a node is a member of this group."""
        return node_id in self.member_node_ids

    def to_dict(self) -> dict:
        """Serialize group node to dict."""
        data = super().to_dict()
        data.update({
            "member_node_ids": self.member_node_ids,
            "input_mappings": {
                k: list(v) for k, v in self.input_mappings.items()
            },
            "output_mappings": {
                k: list(v) for k, v in self.output_mappings.items()
            },
            "is_expanded": self.is_expanded,
            "group_color": self.group_color,
            # B3-2: promoted parameter registry (schema-compatible: old
            # graphs simply lack the key).
            "promoted_params": {
                name: dict(meta) for name, meta in self.promoted_params.items()
            },
        })
        return data

    @staticmethod
    def _parse_port_mappings(raw: Any, field_name: str) -> Dict[str, Tuple[str, str]]:
        """AUD-6809: validate a serialized port-mapping table.

        Malformed project files (hand-edited, or written by a tool with a
        different schema) used to crash project load inside from_dict —
        ``v[0]`` raised for scalar entries, non-dict tables raised
        AttributeError. Entries that are not ``name -> [member_id,
        port_name]`` string pairs are now dropped with a warning instead.
        """
        if raw is None:
            return {}
        if not isinstance(raw, dict):
            logger.warning(
                "GroupNode.from_dict: '%s' is %s (dict expected) — ignored",
                field_name, type(raw).__name__)
            return {}
        mappings: Dict[str, Tuple[str, str]] = {}
        for key, value in raw.items():
            if (isinstance(value, (list, tuple)) and len(value) >= 2
                    and isinstance(value[0], str)
                    and isinstance(value[1], str)):
                mappings[str(key)] = (value[0], value[1])
                continue
            logger.warning(
                "GroupNode.from_dict: dropping malformed '%s' entry %r "
                "(expected [member_node_id, port_name] strings)",
                field_name, key)
        return mappings

    @staticmethod
    def _parse_member_ids(raw: Any) -> List[str]:
        """AUD-6809: validate the serialized member_node_ids list."""
        if raw is None:
            return []
        if not isinstance(raw, (list, tuple)):
            logger.warning(
                "GroupNode.from_dict: 'member_node_ids' is %s (list "
                "expected) — ignored", type(raw).__name__)
            return []
        member_ids: List[str] = []
        for item in raw:
            if isinstance(item, str) and item:
                member_ids.append(item)
            else:
                logger.warning(
                    "GroupNode.from_dict: dropping non-string member id %r",
                    item)
        return member_ids

    @classmethod
    def from_dict(cls, data: dict) -> 'GroupNode':
        """Deserialize group node from dict."""
        node = super().from_dict(data)

        # Load group-specific data (AUD-6809: structure-validated so a
        # malformed project file degrades to a clean group instead of
        # crashing during load).
        node.member_node_ids = cls._parse_member_ids(
            data.get("member_node_ids", []))

        node.input_mappings = cls._parse_port_mappings(
            data.get("input_mappings", {}), "input_mappings")

        node.output_mappings = cls._parse_port_mappings(
            data.get("output_mappings", {}), "output_mappings")

        node.is_expanded = bool(data.get("is_expanded", False))
        node.group_color = str(data.get("group_color", "#4A90D9"))
        node.promoted_params = {
            str(name): dict(meta)
            for name, meta in (data.get("promoted_params") or {}).items()
            if isinstance(meta, dict)
        }

        return node

    # ── B3-2: promoted (published) member parameters ──────────────────────

    def get_param_defs(self) -> Dict[str, ParamDef]:
        """Static defs + one numeric proxy ParamDef per promoted parameter
        (roto_shape's dynamic-channel pattern). The panel edits the proxy
        here; bound members follow via expression bindings."""
        definitions = super().get_param_defs()
        for name, meta in self.promoted_params.items():
            definitions[name] = ParamDef(
                name=name,
                ptype="float",
                default=float(meta.get("default", 0.0)),
                min_val=float(meta.get("min", 0.0)),
                max_val=float(meta.get("max", 100.0)),
                step=float(meta.get("step", 0.01)),
                decimals=3,
                animated=True,
                allow_expression=False,
                group="Promoted",
            )
        return definitions

    def promote_param(self, member_id: str, member_param: str,
                      graph: Any, proxy_name: Optional[str] = None) -> Tuple[bool, str]:
        """Publish a member float/int parameter onto this group.

        Creates (or reuses) a numeric proxy parameter here, records the
        mapping, and binds the member parameter to it via the engine's
        cross-node parameter binding (member expression → group proxy).
        Returns (ok, proxy_name or error).
        """
        member = graph.get_node(member_id) if graph is not None else None
        if member is None:
            return False, f"member not found: {member_id}"
        if member_id not in self.member_node_ids:
            return False, f"node {member_id} is not a member of this group"
        pdef = member.get_param_defs().get(member_param)
        if pdef is None:
            return False, f"parameter not found: {member_param}"
        if not pdef.is_expression_compatible():
            return False, "only float/int parameters can be promoted"

        if not proxy_name:
            proxy_name = f"{member_param}"
            if proxy_name in self.promoted_params:
                return False, f"already promoted as {proxy_name}"
            base, n = proxy_name, 2
            while proxy_name in self.parameters or proxy_name in self.promoted_params \
                    or proxy_name in self.get_param_defs():
                proxy_name = f"{base}_{n}"
                n += 1
        proxy_name = str(proxy_name)

        self.promoted_params[proxy_name] = {
            "member_id": str(member_id),
            "member_param": str(member_param),
            "min": float(pdef.min_val) if pdef.min_val is not None else 0.0,
            "max": float(pdef.max_val) if pdef.max_val is not None else 100.0,
            "step": float(pdef.step) if pdef.step is not None else 0.01,
            "default": float(member.parameters.get(member_param,
                                                  pdef.default or 0.0)),
        }
        # Seed the proxy with the member's current value, then bind the
        # member to follow the proxy (member expression → group proxy).
        self.parameters[proxy_name] = self.promoted_params[proxy_name]["default"]
        ok, err = graph.set_parameter_binding(
            member_id, member_param, self.id, proxy_name)
        if not ok:
            self.promoted_params.pop(proxy_name, None)
            self.parameters.pop(proxy_name, None)
            return False, (err or "binding failed")
        return True, proxy_name

    def unpromote_param(self, proxy_name: str, graph: Any) -> bool:
        """Remove a promotion: drop the member binding and the proxy."""
        meta = self.promoted_params.get(proxy_name)
        if meta is None:
            return False
        if graph is not None:
            graph.remove_parameter_binding(str(meta["member_id"]),
                                           str(meta["member_param"]))
        self.promoted_params.pop(proxy_name, None)
        self.parameters.pop(proxy_name, None)
        return True

    def _do_evaluate(self, inputs: Dict[str, TextureResult], ctx: Any, fb_manager: Any,
                     resource_pool: Any = None, render_mode: str = "preview") -> Dict[str, TextureResult]:
        """Evaluate the internal subgraph and return output port results.

        This method extracts results from already-evaluated member nodes
        (evaluated by the main graph's topological sort) and maps them
        to the group's output ports.

        If _node_graph is not set or no output mappings exist, falls back
        to pass-through behavior.

        Args:
            inputs: Map of input port name -> TextureResult from upstream nodes
            ctx: ModernGL context
            fb_manager: FramebufferManager for temp texture allocation
            resource_pool: Optional GPUResourcePool for tracked texture allocation
            render_mode: "preview", "full", or "export"

        Returns:
            Map of output port name -> TextureResult
        """
        # Fallback: passthrough if no node graph reference or no output mappings
        if not self._node_graph or not self.output_mappings:
            if inputs and self.outputs:
                first_input = next(iter(inputs.values()))
                return {self.outputs[0].name: first_input}
            return {}

        outputs: Dict[str, TextureResult] = {}

        # Extract results from member nodes using output_mappings
        for port_name, mapping in self.output_mappings.items():
            member_node_id, member_port_name = mapping

            # Get the member node from the main graph
            member_node = self._node_graph.get_node(member_node_id)
            if not member_node:
                continue

            # Check if the member node has been evaluated (has cached result)
            # The main graph's topological sort ensures dependencies are evaluated first
            if hasattr(self._node_graph, '_evaluation_cache'):
                cache = self._node_graph._evaluation_cache
                if member_node_id in cache:
                    member_outputs = cache[member_node_id]
                    if member_port_name in member_outputs:
                        outputs[port_name] = member_outputs[member_port_name]
                        continue

            # Alternative: check member node's _last_result if available
            # (for compatibility with different evaluation patterns)
            if hasattr(member_node, '_last_result') and member_node._last_result:
                if member_port_name in member_node._last_result:
                    outputs[port_name] = member_node._last_result[member_port_name]

        # If no outputs were extracted, fall back to passthrough
        if not outputs and inputs and self.outputs:
            first_input = next(iter(inputs.values()))
            return {self.outputs[0].name: first_input}

        return outputs

    def _compute_cache_key(self, inputs: Dict[str, TextureResult], render_mode: str,
                           frame_number: int = 0) -> str:
        """Compute a cache key that includes internal member node states.

        This ensures the group's cache is invalidated when any member's
        parameters change.

        Args:
            inputs: Map of input port name -> TextureResult
            render_mode: Current render mode
            frame_number: Current frame number for animation

        Returns:
            A cache key string that uniquely identifies this evaluation state
        """
        import hashlib

        # Base key components
        key_parts = [
            self.node_type,
            self.id,
            render_mode,
            str(frame_number),
            str(self.disabled),
        ]

        # Add input texture info
        for port_name, tex in inputs.items():
            key_parts.append(f"{port_name}:{tex.texture_id}:{tex.revision}")

        # Add output mappings (defines which member outputs we use)
        for port_name, mapping in sorted(self.output_mappings.items()):
            key_parts.append(f"out:{port_name}:{mapping[0]}:{mapping[1]}")

        # Add member node parameters for cache invalidation
        if self._node_graph:
            for member_id in sorted(self.member_node_ids):
                member_node = self._node_graph.get_node(member_id)
                if member_node:
                    key_parts.append(f"member:{member_id}:{member_node.param_revision}")
                    key_parts.append(f"disabled:{member_id}:{member_node.disabled}")

        # Hash the combined key
        key_str = "|".join(key_parts)
        return hashlib.md5(key_str.encode()).hexdigest()
