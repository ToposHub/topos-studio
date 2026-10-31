"""
PlaceholderNode - Base class for nodes that compile but have no real implementation.

Placeholder nodes:
- Can be added to the graph, connected, and serialized
- Show "NOT IMPLEMENTED" state in the UI
- Produce no output during evaluation (empty dict)
- Parameters are preserved for when the implementation is added
"""

from .base_node import Node, TextureResult
from .node_state import NodeState
from typing import Any, Dict, Optional


class PlaceholderNode(Node):
    """Base class for placeholder nodes."""

    IS_PLACEHOLDER = True
    # CP-044: 占位节点不参与通用 mask/mix（无真实效果可门控）。
    GENERIC_MASK_MIX = False

    def __init__(self, node_id: Optional[str] = None) -> None:
        super().__init__(node_id)
        self._state = NodeState.PLACEHOLDER

    def _do_evaluate(self, inputs: Dict[str, TextureResult], ctx: Any, fb_manager: Any,
                      resource_pool: Any = None, render_mode: str = "preview") -> Dict[str, TextureResult]:
        """Placeholder evaluation — returns empty dict (no output)."""
        return {}
