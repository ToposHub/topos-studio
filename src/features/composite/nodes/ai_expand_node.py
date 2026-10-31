"""Placeholder AI expand/outpaint node."""

from typing import Optional
from .base_node import ParamDef, Port
from .placeholder_node import PlaceholderNode


class AIExpandNode(PlaceholderNode):
    """AI expand/outpaint placeholder."""

    node_type = "ai_expand"
    node_title = "AI Expand"
    header_color = "#9B59B6"

    PARAM_DEFS = [
        ParamDef("direction", "option", "all", labels=["left", "right", "top", "bottom", "all"]),
        ParamDef("amount", "int", 256, min_val=1, max_val=4096, step=1),
        ParamDef("prompt", "str", ""),
        ParamDef("model", "str", "default"),
    ]

    def __init__(self, node_id: Optional[str] = None) -> None:
        super().__init__(node_id)
        self.inputs = [Port("image", "input", "image")]
        self.outputs = [Port("image", "output", "image")]
        self._init_port_revisions()

