"""
Transform Node - Applies translation, scale, and rotation to an image.

Parameters are in pixel space (matching NUKE/Fusion convention):
- translate: pixels (positive = right/down)
- pivot_mode: "center" (default) uses image center, "custom" uses pivot_x/y
- pivot: pixels (when pivot_mode="custom")
- scale: multiplier (1.0 = no change)
- rotation: degrees
"""

import math
from typing import Dict, Optional, Any, Tuple
from .base_node import Node, Port, TextureResult, ParamDef

if __name__ != "__main__":
    from src.features.composite.core.format import BBox

# Filter mode indices — must match shader u_filter values
FILTER_MODES = ["linear", "nearest"]
PIVOT_MODES = ["center", "custom"]


class TransformNode(Node):
    """Transforms an image (translate, scale, rotate) with filter selection."""

    node_type = "transform"
    supports_parent_transform = True
    # Standard 2D layer root (plan §3.5): a media layer's chain ends in a
    # Transform whose output feeds the auto Merge.
    supports_layer_root = True
    node_title = "Transform"
    i18n_title_key = "nodes.transform.title"
    header_color = "#7a6a9a"  # Lavender

    # Metadata
    SEARCH_ALIASES = ["transform", "move", "scale", "rotate", "变换", "移动", "缩放", "旋转", "位移"]
    DESCRIPTION = "图像变换（平移、缩放、旋转）"
    TAGS = ["transform", "geometry"]

    PARAM_DEFS = [
        ParamDef("translate_x", "float", 0.0, min_val=-8192.0, max_val=8192.0, step=1.0),
        ParamDef("translate_y", "float", 0.0, min_val=-8192.0, max_val=8192.0, step=1.0),
        # max 100: baked tracker scales (zoom-out shots) exceed 5x
        ParamDef("scale_x", "float", 1.0, min_val=0.01, max_val=100.0, step=0.01),
        ParamDef("scale_y", "float", 1.0, min_val=0.01, max_val=100.0, step=0.01),
        # ±3600°: tracked rotations cross the ±180 wrap on real shots
        ParamDef("rotation", "float", 0.0, min_val=-3600.0, max_val=3600.0, step=0.5),
        # A5: motion blur (closed-form sub-frame sampling of animated params)
        ParamDef("motion_blur", "bool", False, group="Motion Blur"),
        ParamDef("shutter", "float", 0.5, min_val=0.0, max_val=1.0, step=0.05,
                 group="Motion Blur"),
        ParamDef("samples", "int", 8, min_val=2, max_val=16, step=1,
                 group="Motion Blur"),
        ParamDef("shutter_offset", "float", 0.0, min_val=-0.5, max_val=0.5,
                 step=0.05, group="Motion Blur"),
        ParamDef("pivot_mode", "option", "center", labels=PIVOT_MODES),
        ParamDef("pivot_x", "float", 0.0, min_val=-8192.0, max_val=8192.0, step=1.0),
        ParamDef("pivot_y", "float", 0.0, min_val=-8192.0, max_val=8192.0, step=1.0),
        ParamDef("filter", "option", "linear", labels=FILTER_MODES),
    ]

    def __init__(self, node_id: Optional[str] = None) -> None:
        super().__init__(node_id)
        self.inputs = [
            Port("image", "input", "image"),
        ]
        self.outputs = [
            Port("result", "output", "image")
        ]
        self.parameters = {
            "translate_x": 0.0,
            "translate_y": 0.0,
            "scale_x": 1.0,
            "scale_y": 1.0,
            "rotation": 0.0,
            "motion_blur": False,
            "shutter": 0.5,
            "samples": 8,
            "shutter_offset": 0.0,
            "pivot_mode": "center",
            "pivot_x": 0.0,
            "pivot_y": 0.0,
            "filter": "linear",
        }

    def _build_transform_matrix(self, params: Dict[str, Any],
                                image_width: int, image_height: int) -> Tuple[float, ...]:
        """Build affine transformation matrix (forward).

        Matrix layout: (m00, m10, m01, m11, m02, m12)
        Transform: x' = m00*x + m01*y + m02, y' = m10*x + m11*y + m12

        Order: translate -> rotate (around pivot) -> scale
        """
        tx = params.get("translate_x", 0.0)
        ty = params.get("translate_y", 0.0)
        sx = params.get("scale_x", 1.0)
        sy = params.get("scale_y", 1.0)
        rot = params.get("rotation", 0.0)

        # Resolve pivot
        pivot_mode = params.get("pivot_mode", "center")
        if pivot_mode == "center":
            piv_x = image_width / 2.0
            piv_y = image_height / 2.0
        else:
            piv_x = params.get("pivot_x", 0.0)
            piv_y = params.get("pivot_y", 0.0)

        rad = math.radians(rot)
        cos_r = math.cos(rad)
        sin_r = math.sin(rad)

        # Build matrix: pivot translation -> rotate -> scale -> translate
        # T_total = T_final * S * R * T_pivot_inv
        # For rotation around pivot:
        #   1. Translate to pivot origin: x - piv_x, y - piv_y
        #   2. Apply scale and rotation
        #   3. Translate back + final translation

        # Combined matrix elements:
        # m00 = sx * cos_r, m01 = -sy * sin_r
        # m10 = sx * sin_r, m11 = sy * cos_r
        # m02 = piv_x - sx*cos_r*piv_x + sy*sin_r*piv_y + tx
        # m12 = piv_y - sx*sin_r*piv_x - sy*cos_r*piv_y + ty

        m00 = sx * cos_r
        m01 = -sy * sin_r
        m10 = sx * sin_r
        m11 = sy * cos_r
        m02 = piv_x - sx * cos_r * piv_x + sy * sin_r * piv_y + tx
        m12 = piv_y - sx * sin_r * piv_x - sy * cos_r * piv_y + ty

        return (m00, m10, m01, m11, m02, m12)

    def _build_inverse_matrix(self, params: Dict[str, Any],
                              image_width: int, image_height: int) -> Tuple[float, ...]:
        """Build inverse affine transformation matrix.

        Used for ROI propagation: given output ROI, find input ROI needed.

        AUD-7504 fix: the previous hand-derived signs applied the rotation
        forward again instead of undoing it (m01/m10 sign flip — with
        ``sin_r = sin(-rot)`` the code produced ``m01 = -s/sy`` and
        ``m10 = +s/sx`` where the true inverse needs ``+s/sy`` / ``-s/sx``,
        so any rotation != 0 mapped ROIs through the *forward* rotation).
        The inverse is now derived analytically from the exact forward
        matrix, which keeps ``forward ∘ inverse == identity`` for every
        rotation/scale/pivot/translate combination by construction.

        Forward affine (from ``_build_transform_matrix``)::
            x' = m00*x + m01*y + m02
            y' = m10*x + m11*y + m12
        Its inverse is the closed-form 2x2 affine inverse:
            det = m00*m11 - m01*m10
            i00 =  m11/det, i01 = -m01/det, i02 = -(i00*m02 + i01*m12)
            i10 = -m10/det, i11 =  m00/det, i12 = -(i10*m02 + i11*m12)
        """
        m00, m10, m01, m11, m02, m12 = self._build_transform_matrix(
            params, image_width, image_height)

        det = m00 * m11 - m01 * m10
        if abs(det) < 1e-12:
            # Degenerate (zero-area) linear part — fall back to identity so
            # ROI propagation stays finite. Unreachable with the parameter
            # limits (scale >= 0.01) but kept as a numerical guard.
            return (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)

        i00 = m11 / det
        i01 = -m01 / det
        i10 = -m10 / det
        i11 = m00 / det
        i02 = -(i00 * m02 + i01 * m12)
        i12 = -(i10 * m02 + i11 * m12)
        return (i00, i10, i01, i11, i02, i12)

    def _transform_bbox(self, bbox: "BBox",
                        matrix: Tuple[float, ...], image_height: int) -> "BBox":
        """Apply a Y-down pixel-space affine matrix to a Y-up BBox.

        ``_build_transform_matrix`` / ``_build_inverse_matrix`` work in
        top-left-origin (Y-down) pixel space — positive translate_y moves
        content *down*, matching the UI and the GPU evaluator. ``BBox`` is
        bottom-left-origin (Y-up, OpenGL convention). Naively feeding Y-up
        corners through the Y-down matrix flips the vertical transform, so
        vertical translation/scale clip in the wrong direction.

        We mirror the bbox through the image-height axis, transform in Y-down
        space, then mirror back. The horizontal axis is unaffected.
        """
        # Y-up -> Y-down: flip the y of every corner around image_height.
        ys_down = [image_height - bbox.y, image_height - bbox.top]
        xs = [bbox.x, bbox.right]
        corners_down = [(x, y) for x in xs for y in ys_down]
        transformed = [(m00 * x + m01 * y + m02,
                        m10 * x + m11 * y + m12)
                       for (x, y) in corners_down
                       for (m00, m10, m01, m11, m02, m12) in [matrix]]
        # Y-down -> Y-up: flip y back, then build the enclosing bbox.
        ys_up = [image_height - ty for (_, ty) in transformed]
        xs_out = [tx for (tx, _) in transformed]
        x0 = int(math.floor(min(xs_out)))
        y0 = int(math.floor(min(ys_up)))
        x1 = int(math.ceil(max(xs_out)))
        y1 = int(math.ceil(max(ys_up)))
        if x1 <= x0 or y1 <= y0:
            return BBox(x=0, y=0, width=0, height=0)
        return BBox(x=x0, y=y0, width=x1 - x0, height=y1 - y0)

    @staticmethod
    def _scheduler_reference_size(
            input_bboxes: Optional[Dict[str, "BBox"]],
            fallback_roi: Optional["BBox"]) -> Tuple[int, int]:
        """Canvas size estimate shared by the scheduler-side bbox/ROI methods.

        AUD-7505 fix (contract): ``compute_output_bbox`` and
        ``compute_input_roi`` previously each picked their own reference
        (input bbox dims vs. output ROI dims), and their docstrings claimed
        the result "matches the forward matrix in ``_build_transform_matrix``".
        That claim is only true when the input's data window covers its whole
        texture — full-frame sources and packed effect outputs, where
        ``bbox.width/height == texture width/height``. In that standard case
        this estimate is EXACTLY the reference the evaluator/GPU path uses
        (``_do_evaluate`` builds its matrix with ``image.width/height``).

        For a strict subwindow input (bbox smaller than the texture) the
        scheduler cannot see the texture dims, so the data window itself is
        the best available canvas estimate; the evaluator then pivots at the
        true texture centre while this estimate pivots at the window centre —
        a documented approximation, not a silent "match".

        Both scheduler-side methods now route through this single helper so
        the forward estimate and the ROI inverse can never diverge from each
        other by picking different references.
        """
        if input_bboxes:
            input_bbox = input_bboxes.get("image")
            if input_bbox is not None and not input_bbox.is_empty:
                return int(input_bbox.width), int(input_bbox.height)
        if fallback_roi is not None and not fallback_roi.is_empty:
            return int(fallback_roi.width), int(fallback_roi.height)
        return 1, 1

    def compute_output_bbox(self, input_bboxes: Dict[str, "BBox"],
                            params: Dict[str, Any]) -> Optional["BBox"]:
        """Bbox follows the transformed content (NUKE "bbox follows content").

        The output canvas grows to enclose the input bbox after the affine
        transform, so content that moves outside the original canvas stays
        visible (instead of being clipped to the input frame). Downstream
        nodes (e.g. merge's sampleLayer) sample this larger output using its
        bbox, so the moved content lands at the correct canvas position.

        This is the inverse of the earlier "fixed canvas" policy: that policy
        kept the bbox pinned to the input frame, which clipped any content
        moved beyond the input frame (the 1500×800 foreground could not reach
        the 1920×1080 merge canvas). sampleLayer has since been hardened to
        map inputs by their bbox origin (base_evaluator bbox_a_in_output
        offset + u_use_bbox_mapping), so following the corners is now safe.

        Pivot/flip reference: the input bbox dimensions (see
        ``_scheduler_reference_size``). This equals the evaluator's
        ``image.width/height`` reference whenever the data window covers the
        texture (sources and packed outputs); for strict subwindow textures
        it is the scheduler's best canvas estimate.
        """
        if not input_bboxes:
            return None
        input_bbox = input_bboxes.get("image")
        if input_bbox is None or input_bbox.is_empty:
            return None
        ref_w, ref_h = self._scheduler_reference_size(input_bboxes, None)
        # Transform in Y-down pixel space (the matrix's convention), flipping
        # the Y axis before/after via _transform_bbox so positive translate_y
        # moves content down on screen as the UI promises.
        matrix = self._build_transform_matrix(params, ref_w, ref_h)
        return self._transform_bbox(input_bbox, matrix, ref_h)

    def compute_input_roi(self, output_roi: "BBox",
                          params: Dict[str, Any],
                          input_bboxes: Optional[Dict[str, "BBox"]] = None) -> Dict[str, "BBox"]:
        """Inverse-transform the output ROI to find the input region needed.

        Because the output bbox follows the transformed content (see
        ``compute_output_bbox``), output and input no longer share the same
        coordinate system. Given an output ROI, we apply the *inverse* affine
        matrix to its four corners to recover the input pixel region the
        shader must sample. The result is expanded by the filter padding so
        the interpolation kernel has enough neighboring pixels at the ROI
        boundary (linear/bicubic sampling).

        Pivot/flip reference: the same ``_scheduler_reference_size`` estimate
        as ``compute_output_bbox`` (input bbox dims when known, output ROI
        dims otherwise), so this inverse is consistent with the forward
        estimate above. See that helper for when the estimate is exact vs.
        approximate relative to the GPU path.
        """
        ref_w, ref_h = self._scheduler_reference_size(input_bboxes, output_roi)

        # Build inverse matrix and map the output ROI back to input space,
        # using the same Y-down pixel-space convention as the forward matrix
        # (see _transform_bbox) so the inverse stays consistent with the
        # forward transform in compute_output_bbox.
        inv_matrix = self._build_inverse_matrix(params, ref_w, ref_h)
        input_roi = self._transform_bbox(output_roi, inv_matrix, ref_h)

        pad = self._tile_padding(params)
        if pad > 0:
            input_roi = input_roi.expanded(pad)
        return {"image": input_roi}

    def _tile_padding(self, params: Dict[str, Any]) -> int:
        """Declare tile padding requirement for seamless boundaries.

        Transform samples neighboring pixels during interpolation:
        - Nearest neighbor: no padding needed (0)
        - Linear/bilinear: 1 pixel padding
        - Future bicubic: 2 pixels padding

        Note: Rotation and scale can cause additional boundary issues,
        but the main concern is the filter footprint.

        Args:
            params: Node parameters dictionary.

        Returns:
            Padding in pixels.
        """
        filter_name = params.get("filter", "linear")
        if filter_name == "nearest":
            return 0
        return 1  # Linear filter may sample neighboring pixels

    def requires_roi_expansion(self, params: Dict[str, Any]) -> int:
        """Legacy API - delegates to _tile_padding()."""
        return self._tile_padding(params)

    def _do_evaluate(self, inputs: Dict[str, TextureResult], ctx: Any, fb_manager: Any,
                      resource_pool: Any = None, render_mode: str = "preview") -> Dict[str, TextureResult]:
        image = inputs.get("image")
        if not image or not image.is_valid:
            return {}

        # Check if identity transform
        is_identity = (
            self.parameters.get("translate_x", 0.0) == 0.0
            and self.parameters.get("translate_y", 0.0) == 0.0
            and self.parameters.get("scale_x", 1.0) == 1.0
            and self.parameters.get("scale_y", 1.0) == 1.0
            and self.parameters.get("rotation", 0.0) == 0.0
        )

        if is_identity:
            return {"result": image}

        filter_name = self.parameters.get("filter", "linear")
        filter_idx = FILTER_MODES.index(filter_name) if filter_name in FILTER_MODES else 0

        # Resolve pivot: "center" mode uses image center
        pivot_mode = self.parameters.get("pivot_mode", "center")
        if pivot_mode == "center":
            piv_x = image.width / 2.0
            piv_y = image.height / 2.0
        else:
            piv_x = self.parameters.get("pivot_x", 0.0)
            piv_y = self.parameters.get("pivot_y", 0.0)

        # Compute the output bbox = enclosing box of the transformed input
        # corners. This must mirror compute_output_bbox() so the evaluator
        # allocates an output texture that actually contains the moved
        # content (otherwise pixels shifted outside the input frame are
        # clipped — the original "fixed canvas" bug). Uses _transform_bbox
        # for the same Y-up/Y-down handling as compute_output_bbox.
        # AUD-7505 note: this path's reference is the FULL TEXTURE
        # (image.width/height — the space the GPU pivot and the packed-FBO
        # rebase in _apply_transform live in). The scheduler-side methods
        # use _scheduler_reference_size (the input bbox), which equals this
        # reference whenever the data window covers the texture.
        in_bbox = image.effective_bbox()
        fwd_matrix = self._build_transform_matrix(
            self.parameters, image.width, image.height
        )
        out_bbox = self._transform_bbox(in_bbox, fwd_matrix, image.height)
        out_w = max(1, out_bbox.width)
        out_h = max(1, out_bbox.height)

        return {
            "result": TextureResult(
                texture_id=-1,
                width=image.width,
                height=image.height,
            ),
            "_effect_type": "transform",
            "_effect_input": image,
            "_effect_params": {
                "translate": [
                    self.parameters.get("translate_x", 0.0),
                    self.parameters.get("translate_y", 0.0),
                ],
                "scale": [
                    self.parameters.get("scale_x", 1.0),
                    self.parameters.get("scale_y", 1.0),
                ],
                "rotation": self.parameters.get("rotation", 0.0),
                "pivot": [piv_x, piv_y],
                "filter": filter_idx,
                # Output canvas size + bbox. _apply_transform uses these to
                # allocate an output texture large enough to hold the moved
                # content (out_w/out_h) and tags the result with the moved
                # bbox so downstream nodes sample at the correct position.
                "out_w": out_w,
                "out_h": out_h,
                "out_bbox": out_bbox,
                # A5 motion blur gate reads these from _effect_params —
                # without them the evaluator's motion-blur path can never
                # engage (the UI toggle becomes a no-op).
                "motion_blur": bool(self.parameters.get("motion_blur", False)),
                "shutter": self.parameters.get("shutter", 0.5),
                "samples": self.parameters.get("samples", 8),
                "shutter_offset": self.parameters.get("shutter_offset", 0.0),
            },
        }
