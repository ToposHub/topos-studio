"""Placeholder AI inpaint node."""

from typing import Optional
from .base_node import ParamDef, Port
from .placeholder_node import PlaceholderNode


class AIInpaintNode(PlaceholderNode):
    """AI inpaint placeholder."""

    node_type = "ai_inpaint"
    node_title = "AI Inpaint"
    header_color = "#9B59B6"

    PARAM_DEFS = [
        ParamDef("prompt", "str", ""),
        ParamDef("negative_prompt", "str", ""),
        ParamDef("strength", "float", 0.75, min_val=0.0, max_val=1.0),
        ParamDef("model", "str", "default"),
    ]

    def __init__(self, node_id: Optional[str] = None) -> None:
        super().__init__(node_id)
        self.inputs = [
            Port("image", "input", "image"),
            Port("mask", "input", "image"),
        ]
        self.outputs = [Port("image", "output", "image")]
        self._init_port_revisions()

