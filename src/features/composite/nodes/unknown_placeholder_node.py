"""
UnknownPlaceholderNode - Placeholder for unregistered/unknown node types.

This node is created when deserializing a graph that contains nodes
with types not registered in NODE_REGISTRY (e.g., 'cornerpin', 'remove_channels'
which are None placeholders, or completely unknown types).

The node preserves the original type name and parameters so they can
be restored when the actual implementation is added.
"""

import copy
import logging
from typing import Any, Dict, Optional
from .base_node import Node, Port, TextureResult
from .node_state import NodeState

logger = logging.getLogger(__name__)


class UnknownPlaceholderNode(Node):
    """Placeholder for unknown/unregistered node types.

    Created during graph deserialization when:
    - NODE_REGISTRY[type] is None (unimplemented placeholder like 'cornerpin')
    - NODE_REGISTRY[type] is missing (unknown type from newer version)

    The node preserves the original type in '_original_type' parameter
    for potential future recovery.
    """

    node_type = "unknown_placeholder"
    # CP-044: 未知负载必须原样往返，不注入通用 mask/mix 默认值。
    GENERIC_MASK_MIX = False
    node_title = "Unknown Node"
    header_color = "#8B0000"  # Dark red for visibility
    IS_PLACEHOLDER = True

    def __init__(self, node_id: Optional[str] = None, original_type: Optional[str] = None) -> None:
        super().__init__(node_id)
        self._state = NodeState.PLACEHOLDER
        self._original_type = original_type or "unknown"

        # Store original type for potential recovery
        self.parameters["_original_type"] = self._original_type
        # AUD-7509: warn once per instance — the docstring has always
        # claimed a warning is logged; make the code match the doc so
        # "unknown type silently skipped" leaves a debugging trail.
        self._unknown_warned = False

        # Default ports - most nodes have at least one input/output
        self.inputs = [
            Port("image", "input", "image"),
        ]
        self.outputs = [
            Port("image", "output", "image")
        ]

    def _do_evaluate(self, inputs: Dict[str, TextureResult], ctx: Any, fb_manager: Any,
                      resource_pool: Any = None, render_mode: str = "preview") -> Dict[str, TextureResult]:
        """Placeholder evaluation — returns empty dict (no output).

        Logs a warning about the unknown node type for debugging
        (once per instance, AUD-7509: the docstring promised this log;
        previously no logging call existed at all).
        """
        if not self._unknown_warned:
            self._unknown_warned = True
            logger.warning(
                "UnknownPlaceholderNode: skipping evaluation of unknown "
                "node type %r (id=%s) — no output produced",
                self._original_type, self.id)
        return {}

    @classmethod
    def from_dict(cls, data: dict) -> 'UnknownPlaceholderNode':
        """Deserialize from dict, preserving original type."""
        original_type = data.get("type", "unknown")
        node = cls(
            node_id=data.get("id"),
            original_type=original_type
        )
        node.position = tuple(data.get("position", [0, 0]))

        # Preserve original parameters (excluding internal ones)
        loaded_params = data.get("parameters", {})
        for key, val in loaded_params.items():
            if not key.startswith("_"):  # Keep non-internal params
                node.parameters[key] = val

        node.disabled = data.get("disabled", False)
        node.parent_id = data.get("parent_id")
        bindings = data.get("parameter_bindings", {})
        if isinstance(bindings, dict):
            node.parameter_bindings = copy.deepcopy(bindings)
        expressions = data.get("expressions", {})
        if isinstance(expressions, dict):
            # Unknown nodes cannot validate expressions against a missing
            # schema, but the raw strings must survive until the real plugin
            # is installed and can interpret them.
            node._expressions = copy.deepcopy(expressions)
        keyframe_tracks = data.get("keyframe_tracks", {})
        if isinstance(keyframe_tracks, dict):
            node.keyframe_tracks = copy.deepcopy(keyframe_tracks)
        time_range = data.get("time_range")
        if isinstance(time_range, dict):
            node.time_range = copy.deepcopy(time_range)
        node_color = data.get("node_color")
        if isinstance(node_color, str) and node_color:
            node.custom_header_color = node_color
            node.header_color = node_color
        known_fields = {
            "id", "type", "position", "parameters", "disabled",
            "expressions", "keyframe_tracks", "parameter_bindings",
            "parent_id", "time_range", "node_color",
        }
        node._unknown_fields = {
            str(key): copy.deepcopy(value)
            for key, value in data.items()
            if key not in known_fields
        }
        return node

    def to_dict(self) -> dict:
        """Serialize, restoring original type for round-trip compatibility."""
        data = super().to_dict()
        # Filter out internal params for serialization while retaining all
        # standard animation/timing fields emitted by Node.to_dict().
        data["type"] = self._original_type  # Restore original type
        data["parameters"] = {
            key: value for key, value in data.get("parameters", {}).items()
            if not str(key).startswith("_")
        }
        return data

    def __repr__(self) -> str:
        return f"UnknownPlaceholderNode(id={self.id}, original_type={self._original_type})"
