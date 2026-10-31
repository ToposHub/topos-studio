"""
Crop Node - Crops an image to a specified region.

Supports:
- Crop region (x, y, width, height) in pixels
- affect_format: controls whether the output format changes to match the crop region
  - True:  output texture = crop size, format updated to crop dimensions (GPU render)
  - False: output texture = original size, only bbox updated (no GPU render)
- outside_policy: how to fill pixels outside the source image
  - "black":        fill with (0, 0, 0, 1)
  - "transparent":  fill with (0, 0, 0, 0)
  - "repeat":       tile/wrap the source image

Crop changes the data window (bbox). Use Reformat to change the canvas size.
"""

from typing import Dict, Optional, Any
from .base_node import Node, Port, TextureResult, ParamDef
from src.features.composite.core.format import BBox

# Outside policy indices — must match shader u_outside values
OUTSIDE_POLICIES = ["black", "transparent", "repeat", "mirror"]
# Alias for backward compatibility with tests and older code
OUTSIDE_MODES = ["black", "transparent"]


class CropNode(Node):
    """Crops the image to a specified pixel region."""

    node_type = "crop"
    node_title = "Crop"
    header_color = "#8a7a6a"  # Warm tan

    # i18n title key
    i18n_title_key = "nodes.crop.title"

    # Metadata
    SEARCH_ALIASES = ["crop", "裁剪", "剪切", "region", "区域", "cut"]
    DESCRIPTION = "裁剪图像到指定区域"
    TAGS = ["transform", "region"]

    PARAM_DEFS = [
        ParamDef("x", "int", 0, min_val=0, max_val=16384, step=1),
        ParamDef("y", "int", 0, min_val=0, max_val=16384, step=1),
        ParamDef("width", "int", 512, min_val=1, max_val=16384, step=1),
        ParamDef("height", "int", 512, min_val=1, max_val=16384, step=1),
        ParamDef("reformat", "bool", True),
        ParamDef("outside", "option", "transparent", labels=OUTSIDE_POLICIES),
    ]

    def __init__(self, node_id: Optional[str] = None) -> None:
        super().__init__(node_id)
        self.inputs = [
            Port("image", "input", "image"),
        ]
        self.outputs = [
            Port("result", "output", "image"),
        ]
        self.parameters = {
            "x": 0,
            "y": 0,
            "width": 512,
            "height": 512,
            "reformat": True,
            "outside": "transparent",
        }

    def _get_crop_bbox(self, params: Dict[str, Any]) -> BBox:
        """Helper to compute the crop region from parameters."""
        crop_x = max(0, int(params.get("x", 0)))
        crop_y = max(0, int(params.get("y", 0)))
        crop_w = max(1, int(params.get("width", 512)))
        crop_h = max(1, int(params.get("height", 512)))
        return BBox(x=crop_x, y=crop_y, width=crop_w, height=crop_h)

    def compute_output_bbox(self, input_bboxes: Dict[str, "BBox"],
                            params: Dict[str, Any]) -> Optional["BBox"]:
        """Crop output bbox is the crop region intersected with input bbox.

        When reformat is True the output is the full crop region
        (because pixels outside the source may be filled by the outside policy).
        When reformat is False the output is the intersection, since no
        GPU render happens and only metadata changes.
        """
        if not input_bboxes:
            return None
        input_bbox = input_bboxes.get("image")
        if input_bbox is None or input_bbox.is_empty:
            return None

        crop_bbox = self._get_crop_bbox(params)
        reformat = params.get("reformat", params.get("affect_format", True))

        if reformat:
            # With GPU render, output is the full crop region
            return crop_bbox
        else:
            # Without GPU render, only the intersection contains data
            return crop_bbox.intersection(input_bbox)

    def compute_input_roi(self, output_roi: "BBox",
                          params: Dict[str, Any],
                          input_bboxes: Optional[Dict[str, "BBox"]] = None) -> Dict[str, "BBox"]:
        """To produce output_roi, we need that region from the input.

        For crop, the output ROI directly maps to the input region
        (no expansion needed, crop just reads that region).
        """
        crop_bbox = self._get_crop_bbox(params)

        # If output_roi is outside crop region, we don't need any input
        if not output_roi.intersects(crop_bbox):
            return {"image": BBox(x=0, y=0, width=0, height=0)}

        # Clamp output_roi to the crop region
        needed_region = output_roi.intersection(crop_bbox)
        return {"image": needed_region}

    def _do_evaluate(self, inputs: Dict[str, TextureResult], ctx: Any, fb_manager: Any,
                      resource_pool: Any = None, render_mode: str = "preview") -> Dict[str, TextureResult]:
        image = inputs.get("image")
        if not image or not image.is_valid:
            return {}

        crop_x = max(0, int(self.parameters.get("x", 0)))
        crop_y = max(0, int(self.parameters.get("y", 0)))
        crop_w = max(1, int(self.parameters.get("width", 512)))
        crop_h = max(1, int(self.parameters.get("height", 512)))
        reformat = self.parameters.get("reformat", self.parameters.get("affect_format", True))

        if reformat:
            # GPU render: produce new texture of crop size
            # Use 'outside' as primary param name, fallback to 'outside_policy' for legacy
            outside_val = self.parameters.get("outside",
                                              self.parameters.get("outside_policy", "transparent"))
            outside_idx = (OUTSIDE_POLICIES.index(outside_val)
                           if outside_val in OUTSIDE_POLICIES else 1)

            return {
                "result": TextureResult(
                    texture_id=-1,
                    width=crop_w,
                    height=crop_h,
                ),
                "_effect_type": "crop",
                "_effect_input": image,
                "_effect_params": {
                    "crop_x": crop_x,
                    "crop_y": crop_y,
                    "crop_w": crop_w,
                    "crop_h": crop_h,
                    "outside": outside_idx,
                },
            }
        else:
            # No GPU render: just update bbox metadata
            new_bbox = BBox(x=crop_x, y=crop_y, width=crop_w, height=crop_h)
            # Intersect with input bbox to get actual data region
            in_bbox = image.effective_bbox()
            clipped_bbox = new_bbox.intersection(in_bbox)
            # Clamp to input format if available
            if image.format:
                clipped_bbox = clipped_bbox.clamped(image.format)

            return {
                "result": TextureResult(
                    texture_id=image.texture_id,
                    width=image.width,
                    height=image.height,
                    format=image.format,
                    bbox=clipped_bbox if not clipped_bbox.is_empty else new_bbox,
                    alpha_premultiplied=image.alpha_premultiplied,
                    color_space=image.color_space,
                )
            }
