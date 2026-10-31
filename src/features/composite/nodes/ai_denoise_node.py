"""Placeholder AI denoise node."""

from typing import Optional
from .base_node import ParamDef, Port
from .placeholder_node import PlaceholderNode


class AIDenoiseNode(PlaceholderNode):
    """AI denoise placeholder."""

    node_type = "ai_denoise"
    node_title = "AI Denoise"
    header_color = "#9B59B6"

    PARAM_DEFS = [
        ParamDef("strength", "float", 0.5, min_val=0.0, max_val=1.0),
        ParamDef("model", "str", "default"),
    ]

    def __init__(self, node_id: Optional[str] = None) -> None:
        super().__init__(node_id)
        self.inputs = [Port("image", "input", "image")]
        self.outputs = [Port("image", "output", "image")]
        self._init_port_revisions()

