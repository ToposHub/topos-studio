"""
Reformat Node - Resizes an image to a target format.

Supports:
- target_format via preset formats (HD 720, HD 1080, UHD 4K, etc.) or custom width/height
- Resize types: no_resize, fit (letterbox), fill (crop excess), distort (stretch),
                center_crop (scale to fill then center-crop)
- Filters: nearest, linear, cubic, lanczos
- preserve_bbox: when True, the input bbox is passed through unchanged

Reformat is the ONLY node that should change the output format/canvas size.
Merge does NOT auto-fit images — use Reformat for that.
"""

from typing import Dict, Optional, Any, Tuple
from .base_node import Node, Port, TextureResult, ParamDef
from src.features.composite.core.format import ImageFormat, BBox, FORMAT_PRESETS

# Resize type indices — must match shader u_resize_mode values
RESIZE_TYPES = ["no_resize", "fit", "fill", "distort", "center_crop"]

# Backward compatibility alias — old code may reference RESIZE_MODES
RESIZE_MODES = RESIZE_TYPES

# Legacy value mapping for deserialization of old projects
_LEGACY_RESIZE_ALIASES = {
    "none": "no_resize",
}

# Filter mode indices — must match shader u_filter values
FILTER_MODES = ["nearest", "linear", "cubic", "lanczos"]

# Common preset names for the UI
PRESET_OPTIONS = list(FORMAT_PRESETS.keys()) + ["Custom"]


class ReformatNode(Node):
    """Resizes the image to a target format (canvas size)."""

    node_type = "reformat"
    node_title = "Reformat"
    header_color = "#6a8a7a"  # Sage green

    # i18n title key
    i18n_title_key = "nodes.reformat.title"

    # Metadata
    SEARCH_ALIASES = ["reformat", "resize", "format", "重设", "调整大小", "画布", "分辨率", "fit"]
    DESCRIPTION = "调整图像到目标格式/分辨率"
    TAGS = ["transform", "format"]

    PARAM_DEFS = [
        ParamDef("format_mode", "option", "preset", labels=["preset", "custom"]),
        ParamDef("preset", "option", "HD 1080", labels=list(FORMAT_PRESETS.keys())),
        ParamDef("width", "int", 1920, min_val=1, max_val=16384, step=1),
        ParamDef("height", "int", 1080, min_val=1, max_val=16384, step=1),
        ParamDef("resize_mode", "option", "fit", labels=RESIZE_TYPES),
        ParamDef("filter", "option", "linear", labels=FILTER_MODES),
        ParamDef("preserve_bbox", "bool", False),
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
            "format_mode": "preset",
            "preset": "HD 1080",
            "width": 1920,
            "height": 1080,
            "resize_mode": "fit",
            "filter": "linear",
            "preserve_bbox": False,
        }

    # ── helpers ──

    @staticmethod
    def _normalise_resize_mode(value: str) -> str:
        """Map legacy values to their modern equivalents."""
        return _LEGACY_RESIZE_ALIASES.get(value, value)

    def _get_target_dimensions(self, params: Dict[str, Any],
                               input_width: int, input_height: int) -> Tuple[int, int]:
        """Helper to compute target dimensions from parameters."""
        fmt_mode = params.get("format_mode", "preset")
        if fmt_mode == "preset":
            preset_name = params.get("preset", "HD 1080")
            preset = FORMAT_PRESETS.get(preset_name)
            if preset:
                return preset.width, preset.height
            return input_width, input_height
        else:
            return max(1, int(params.get("width", 1920))), max(1, int(params.get("height", 1080)))

    def _compute_scale_factor(self, params: Dict[str, Any],
                               input_width: int, input_height: int) -> float:
        """Compute the scale factor based on resize_mode."""
        target_w, target_h = self._get_target_dimensions(params, input_width, input_height)
        resize_mode = self._normalise_resize_mode(params.get("resize_mode", "fit"))

        if resize_mode == "no_resize":
            return 1.0  # No scaling
        elif resize_mode == "distort":
            # Different scale for x and y - use average for ROI estimation
            scale_x = target_w / input_width if input_width > 0 else 1.0
            scale_y = target_h / input_height if input_height > 0 else 1.0
            return (scale_x + scale_y) / 2.0
        elif resize_mode == "fit":
            # Fit preserves aspect ratio, scale to fit within target
            scale_x = target_w / input_width if input_width > 0 else 1.0
            scale_y = target_h / input_height if input_height > 0 else 1.0
            return min(scale_x, scale_y)
        elif resize_mode in ("fill", "center_crop"):
            # Fill / center_crop both scale to cover target (may crop)
            scale_x = target_w / input_width if input_width > 0 else 1.0
            scale_y = target_h / input_height if input_height > 0 else 1.0
            return max(scale_x, scale_y)
        return 1.0

    # ── bbox / ROI ──

    def compute_output_bbox(self, input_bboxes: Dict[str, "BBox"],
                            params: Dict[str, Any]) -> Optional["BBox"]:
        """Reformat maps the input bbox onto the target canvas.

        AUD-7203: the forward mapping now mirrors ``_apply_reformat``
        (transform_evaluator) exactly — the input bbox position is scaled
        and shifted by the float centre offset ``(target - scaled) * 0.5``
        (the old ``// 2`` on pre-truncated integers could differ by a pixel
        and the bbox origin was dropped entirely, so sub-window inputs
        declared the wrong output region for ROI planning).  Residual
        approximation: the scheduler only provides the upstream *bbox*,
        while the renderer scales by the full texture size; with the common
        full-frame bbox the two bases coincide.
        """
        if not input_bboxes:
            return None
        input_bbox = input_bboxes.get("image")
        if input_bbox is None or input_bbox.is_empty:
            return None

        resize_mode = self._normalise_resize_mode(params.get("resize_mode", "fit"))

        # preserve_bbox: pass through the input bbox unchanged
        if params.get("preserve_bbox", False):
            return input_bbox

        # For "no_resize" mode, bbox stays same position
        if resize_mode == "no_resize":
            return input_bbox

        # For other modes, bbox scales
        target_w, target_h = self._get_target_dimensions(params, input_bbox.width, input_bbox.height)
        scale = self._compute_scale_factor(params, input_bbox.width, input_bbox.height)

        if resize_mode in ("fit", "fill", "center_crop"):
            # Uniform scaling — same formula as the renderer: float centre
            # offset, then int() truncation on the final bbox edges.
            scaled_w = input_bbox.width * scale
            scaled_h = input_bbox.height * scale
            offset_x = (target_w - scaled_w) * 0.5
            offset_y = (target_h - scaled_h) * 0.5

            result = BBox(
                x=int(input_bbox.x * scale + offset_x),
                y=int(input_bbox.y * scale + offset_y),
                width=max(1, int(scaled_w)),
                height=max(1, int(scaled_h)),
            )
            return result.clamped(ImageFormat(width=target_w, height=target_h))

        # distort mode — non-uniform scaling (separate x/y)
        scale_x = target_w / input_bbox.width if input_bbox.width > 0 else 1.0
        scale_y = target_h / input_bbox.height if input_bbox.height > 0 else 1.0
        scaled_x = int(input_bbox.x * scale_x)
        scaled_y = int(input_bbox.y * scale_y)
        scaled_w = max(1, int(input_bbox.width * scale_x))
        scaled_h = max(1, int(input_bbox.height * scale_y))

        result = BBox(x=scaled_x, y=scaled_y, width=scaled_w, height=scaled_h)
        return result.clamped(ImageFormat(width=target_w, height=target_h))

    def compute_input_roi(self, output_roi: "BBox",
                          params: Dict[str, Any],
                          input_bboxes: Optional[Dict[str, "BBox"]] = None) -> Dict[str, "BBox"]:
        """Inverse-map the output ROI through the reformat transform.

        AUD-7204: the inverse now undoes the *actual* forward mapping:
        (1) the centring offset ``p_in = (p_out - offset) / scale`` — the
        old ``p_out / scale`` mislocated letterboxed inputs;
        (2) distort uses per-axis scales instead of the average;
        (3) the filter-footprint padding from ``requires_roi_expansion``
        is applied (the scheduler explicitly relies on this override owning
        the expansion, matching the base-class default).
        """
        resize_mode = self._normalise_resize_mode(params.get("resize_mode", "no_resize"))
        padding = self.requires_roi_expansion(params)

        if resize_mode == "no_resize":
            # No scaling, input ROI = output ROI (+ filter padding)
            roi = output_roi.expanded(padding) if padding > 0 else output_roi
            return {"image": roi}

        # Get input dimensions (from input_bboxes if available)
        input_w = output_roi.width
        input_h = output_roi.height
        if input_bboxes:
            input_bbox = input_bboxes.get("image")
            if input_bbox:
                input_w = input_bbox.width
                input_h = input_bbox.height
        if input_w <= 0 or input_h <= 0:
            roi = output_roi.expanded(padding) if padding > 0 else output_roi
            return {"image": roi}

        target_w, target_h = self._get_target_dimensions(params, input_w, input_h)
        corners = [
            (output_roi.x, output_roi.y),
            (output_roi.right, output_roi.y),
            (output_roi.x, output_roi.top),
            (output_roi.right, output_roi.top),
        ]

        if resize_mode == "distort":
            # Per-axis inverse scales
            scale_x = target_w / input_w if input_w > 0 else 1.0
            scale_y = target_h / input_h if input_h > 0 else 1.0
            inv_x = 1.0 / scale_x if scale_x > 0 else 1.0
            inv_y = 1.0 / scale_y if scale_y > 0 else 1.0
            inverse_corners = [(c[0] * inv_x, c[1] * inv_y) for c in corners]
        else:
            # fit / fill / center_crop: undo scale AND centring offset
            scale = self._compute_scale_factor(params, input_w, input_h)
            inv_scale = 1.0 / scale if scale > 0 else 1.0
            scaled_w = input_w * scale
            scaled_h = input_h * scale
            offset_x = (target_w - scaled_w) * 0.5
            offset_y = (target_h - scaled_h) * 0.5
            inverse_corners = [
                ((c[0] - offset_x) * inv_scale, (c[1] - offset_y) * inv_scale)
                for c in corners
            ]

        input_roi = BBox.from_corners(inverse_corners)
        if padding > 0:
            input_roi = input_roi.expanded(padding)
        return {"image": input_roi}

    def requires_roi_expansion(self, params: Dict[str, Any]) -> int:
        """Reformat may need extra pixels for filter footprint."""
        filter_name = params.get("filter", "linear")
        if filter_name == "nearest":
            return 0
        if filter_name in ("cubic", "lanczos"):
            return 2  # Higher-quality filters have larger footprints
        return 1  # Linear filter may sample neighboring pixels

    # ── evaluation ──

    def _do_evaluate(self, inputs: Dict[str, TextureResult], ctx: Any, fb_manager: Any,
                      resource_pool: Any = None, render_mode: str = "preview") -> Dict[str, TextureResult]:
        image = inputs.get("image")
        if not image or not image.is_valid:
            return {}

        # Determine target dimensions
        fmt_mode = self.parameters.get("format_mode", "preset")
        if fmt_mode == "preset":
            preset_name = self.parameters.get("preset", "HD 1080")
            preset = FORMAT_PRESETS.get(preset_name)
            if preset:
                target_w, target_h = preset.width, preset.height
            else:
                target_w, target_h = image.width, image.height
        else:
            target_w = max(1, int(self.parameters.get("width", 1920)))
            target_h = max(1, int(self.parameters.get("height", 1080)))

        resize_mode = self._normalise_resize_mode(self.parameters.get("resize_mode", "fit"))

        # If same dimensions and no_resize, pass through
        if resize_mode == "no_resize" and image.width == target_w and image.height == target_h:
            new_fmt = ImageFormat(width=target_w, height=target_h, name="Reformatted")
            # AUD-7202: with_updates preserves revision/bit_depth/
            # frame_metadata/depth/AOV/diagnostic metadata.  A fresh
            # TextureResult reset revision to 0 and bit_depth to 8, freezing
            # downstream cache keys and degrading HDR passthrough.
            return {
                "result": image.with_updates(
                    width=image.width,
                    height=image.height,
                    format=new_fmt,
                    bbox=image.bbox,
                    alpha_premultiplied=image.alpha_premultiplied,
                    color_space=image.color_space,
                )
            }

        # If same dimensions and non-distort, also pass through
        if resize_mode != "distort" and resize_mode != "no_resize" and image.width == target_w and image.height == target_h:
            new_fmt = ImageFormat(width=target_w, height=target_h, name="Reformatted")
            return {
                "result": image.with_updates(
                    width=image.width,
                    height=image.height,
                    format=new_fmt,
                    bbox=image.bbox,
                    alpha_premultiplied=image.alpha_premultiplied,
                    color_space=image.color_space,
                )
            }

        filter_name = self.parameters.get("filter", "linear")
        filter_idx = FILTER_MODES.index(filter_name) if filter_name in FILTER_MODES else 1
        resize_idx = RESIZE_TYPES.index(resize_mode) if resize_mode in RESIZE_TYPES else 1

        return {
            "result": TextureResult(
                texture_id=-1,
                width=target_w,
                height=target_h,
            ),
            "_effect_type": "reformat",
            "_effect_input": image,
            "_effect_params": {
                "target_width": target_w,
                "target_height": target_h,
                "resize_mode": resize_idx,
                "filter": filter_idx,
                "preserve_bbox": self.parameters.get("preserve_bbox", False),
            },
        }
