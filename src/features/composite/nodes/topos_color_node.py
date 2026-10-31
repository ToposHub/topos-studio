"""
Topos Color Node - Applies a ColorStack (color grading) to input image.

This node replaces the old COLOR node's dependency on the Color page's
current output texture. Instead, it applies color grading from:
- clip_stack: the timeline clip's own color_stack_id
- preset: a project-level color preset
- embedded: a self-contained ColorStack snapshot

Unlike the old COLOR node:
- Has an image INPUT port (not source-only)
- Reads ColorStack data, not a cached texture
- Works without the Color page being open
"""

from typing import Any, Dict, Optional
from .base_node import Node, Port, TextureResult


class ToposColorNode(Node):
    """Applies a ColorStack color grading to the input image."""

    node_type = "topos_color"
    node_title = "Topos Color"
    header_color = "#cc6600"  # Orange to match app accent

    # Metadata
    SEARCH_ALIASES = [
        "toposcolor", "topos_color", "colorstack", "color_stack",
        "调色栈", "调色", "colorgrade", "color_grade", "grade",
        "cdl", "colordecisionlist",
    ]
    DESCRIPTION = "应用调色栈（ColorStack）到输入画面"
    TAGS = ["color", "grading", "stack"]

    def __init__(self, node_id: Optional[str] = None) -> None:
        super().__init__(node_id)
        self.inputs = [
            Port("image", "input", "image"),
        ]
        self.outputs = [
            Port("result", "output", "image"),
        ]
        self.parameters = {
            "stack_source": "clip_stack",   # clip_stack | preset | embedded
            "color_stack_id": "",            # For clip_stack mode
            # Graph factories replace this with primary_clip_grade. A node
            # created manually in Composite is marked composite_final_grade
            # so timeline Color synchronization never rebinds it.
            "role": "composite_final_grade",
            "source_clip_id": "",
            "source_timeline_id": "",
            "linked": False,
            "preset_id": "",                 # For preset mode
            "embedded_stack": None,          # For embedded mode (full stack snapshot)
            "bypass": False,
            "intensity": 1.0,
        }

    def _do_evaluate(self, inputs: Dict[str, TextureResult], ctx: Any, fb_manager: Any,
                      resource_pool: Any = None, render_mode: str = "preview") -> Dict[str, TextureResult]:
        image = inputs.get("image")
        if not image or not image.is_valid:
            return {}

        # Bypass: pass through input unchanged
        if self.parameters.get("bypass", False):
            return {"result": image}

        # Intensity at 0.0: also pass through (no grading)
        if self.parameters.get("intensity", 1.0) <= 0.0:
            return {"result": image}

        # Return effect marker for the evaluator to dispatch
        # The actual ColorStack rendering will be handled by a ColorStackRenderService
        # in the evaluator (Phase 3). For now, return a marker that signals
        # the composite renderer to apply color grading.
        return {
            "result": TextureResult(
                texture_id=-1,
                width=image.width,
                height=image.height,
            ),
            "_effect_type": "topos_color",
            "_effect_input": image,
            "_effect_params": {
                "stack_source": self.parameters.get("stack_source", "clip_stack"),
                "color_stack_id": self.parameters.get("color_stack_id", ""),
                "preset_id": self.parameters.get("preset_id", ""),
                "embedded_stack": self.parameters.get("embedded_stack"),
                "intensity": self.parameters.get("intensity", 1.0),
            },
        }
