"""
CornerPin Node - Applies perspective transform via four corner control points.

Motion graphics and compositing standard tool for match-moving, screen replacement,
and perspective corrections. Supports both source and destination corner mapping.

Parameters use normalized coordinates (0-1 range) for resolution independence.
The interpolation parameter controls the filtering quality during sampling.
"""

from typing import Dict, Optional, Any, Tuple, List
from .base_node import Node, Port, TextureResult, ParamDef

if __name__ != "__main__":
    from src.features.composite.core.format import BBox

# Interpolation mode indices — must match shader u_interpolation values
INTERPOLATION_MODES = ["bilinear", "bicubic"]


class CornerPinNode(Node):
    """Perspective transform via four corner control points.

    The node maps four source corners (from1-4) to four destination corners (to1-4).
    Default values correspond to identity transform (no change).

    Corner ordering convention:
    - Corner 1: top-left (0, 0)
    - Corner 2: top-right (1, 0)
    - Corner 3: bottom-right (1, 1)
    - Corner 4: bottom-left (0, 1)
    """

    node_type = "cornerpin"
    node_title = "CornerPin"
    header_color = "#7a6a9a"  # Lavender — same family as Transform

    # i18n title key
    i18n_title_key = "nodes.cornerpin.title"

    # Metadata
    SEARCH_ALIASES = ["cornerpin", "corner", "pin", "perspective", "warp",
                      "四角", "透视", "变形", "角点", "匹配"]
    DESCRIPTION = "四角透视变形节点（match-move、屏幕替换）"
    TAGS = ["transform", "geometry", "warp"]

    PARAM_DEFS = [
        # Destination corners (where the source corners map to)
        ParamDef("to1_x", "float", 0.0, min_val=-8.0, max_val=8.0, step=0.01),
        ParamDef("to1_y", "float", 0.0, min_val=-8.0, max_val=8.0, step=0.01),
        ParamDef("to2_x", "float", 1.0, min_val=-8.0, max_val=8.0, step=0.01),
        ParamDef("to2_y", "float", 0.0, min_val=-8.0, max_val=8.0, step=0.01),
        ParamDef("to3_x", "float", 1.0, min_val=-8.0, max_val=8.0, step=0.01),
        ParamDef("to3_y", "float", 1.0, min_val=-8.0, max_val=8.0, step=0.01),
        ParamDef("to4_x", "float", 0.0, min_val=-8.0, max_val=8.0, step=0.01),
        ParamDef("to4_y", "float", 1.0, min_val=-8.0, max_val=8.0, step=0.01),
        # Source corners (defaults to full image corners for identity)
        ParamDef("from1_x", "float", 0.0, min_val=-8.0, max_val=8.0, step=0.01),
        ParamDef("from1_y", "float", 0.0, min_val=-8.0, max_val=8.0, step=0.01),
        ParamDef("from2_x", "float", 1.0, min_val=-8.0, max_val=8.0, step=0.01),
        ParamDef("from2_y", "float", 0.0, min_val=-8.0, max_val=8.0, step=0.01),
        ParamDef("from3_x", "float", 1.0, min_val=-8.0, max_val=8.0, step=0.01),
        ParamDef("from3_y", "float", 1.0, min_val=-8.0, max_val=8.0, step=0.01),
        ParamDef("from4_x", "float", 0.0, min_val=-8.0, max_val=8.0, step=0.01),
        ParamDef("from4_y", "float", 1.0, min_val=-8.0, max_val=8.0, step=0.01),
        # Interpolation quality
        ParamDef("interpolation", "option", "bilinear", labels=INTERPOLATION_MODES),
    ]

    def __init__(self, node_id: Optional[str] = None) -> None:
        super().__init__(node_id)
        self.inputs = [
            Port("image", "input", "image"),
        ]
        self.outputs = [
            Port("result", "output", "image")
        ]

    @staticmethod
    def _compute_homography(src_points: List[Tuple[float, float]],
                            dst_points: List[Tuple[float, float]]) -> Tuple[float, ...]:
        """Compute 3x3 homography matrix mapping src → dst.

        Uses Direct Linear Transform (DLT) algorithm.
        Returns matrix as (h00, h01, h02, h10, h11, h12, h20, h21) tuple.
        The last element h22 is normalized to 1.0.

        Args:
            src_points: Four source corner coordinates [(x1,y1), ...]
            dst_points: Four destination corner coordinates [(x1,y1), ...]

        Returns:
            8-element tuple representing the 3x3 homography (h22=1).
        """
        # Build the DLT matrix A from point correspondences
        # Each correspondence gives two equations
        # For point (x,y) → (x',y'), we have:
        #   -x*h00 - y*h01 + x'*h20 = x'*h22 (but h22=1)
        #   -x*h10 - y*h11 + y'*h20 = y'*h22
        # Rearranged: -x*h00 - y*h01 - 1*h02 + x*x'*h20 + y*x'*h21 + x'*h22 = 0
        #              -x*h10 - y*h11 - 1*h12 + x*y'*h20 + y*y'*h21 + y'*h22 = 0

        # We solve for h00,h01,h02,h10,h11,h12,h20,h21 with h22=1

        # Build 8x8 system (4 points × 2 equations = 8 equations)
        A = []
        b = []

        for (sx, sy), (dx, dy) in zip(src_points, dst_points):
            # First equation: -sx*h00 - sy*h01 - h02 + sx*dx*h20 + sy*dx*h21 = -dx
            A.append([-sx, -sy, -1.0, 0.0, 0.0, 0.0, sx*dx, sy*dx])
            b.append(-dx)
            # Second equation: 0.0, 0.0, 0.0, -sx, -sy, -1.0, sx*dy, sy*dy = -dy
            A.append([0.0, 0.0, 0.0, -sx, -sy, -1.0, sx*dy, sy*dy])
            b.append(-dy)

        # Solve linear system using numpy-style manual calculation
        # For 8x8 system, we use Gaussian elimination
        h = _solve_linear_system_8x8(A, b)

        if h is None:
            # Degenerate case (collinear points) — return identity
            return (1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0)

        return tuple(h)

    @staticmethod
    def _compute_inverse_homography(src_points: List[Tuple[float, float]],
                                    dst_points: List[Tuple[float, float]]) -> Tuple[float, ...]:
        """Compute inverse homography (dst → src) for ROI propagation."""
        return CornerPinNode._compute_homography(dst_points, src_points)

    def _get_corner_points(self) -> Tuple[List[Tuple[float, float]], List[Tuple[float, float]]]:
        """Extract corner points from parameters.

        Returns:
            (src_points, dst_points) as lists of (x, y) tuples.
        """
        params = self.parameters

        # Destination corners (output positions)
        dst_points = [
            (params.get("to1_x", 0.0), params.get("to1_y", 0.0)),
            (params.get("to2_x", 1.0), params.get("to2_y", 0.0)),
            (params.get("to3_x", 1.0), params.get("to3_y", 1.0)),
            (params.get("to4_x", 0.0), params.get("to4_y", 1.0)),
        ]

        # Source corners (input positions)
        src_points = [
            (params.get("from1_x", 0.0), params.get("from1_y", 0.0)),
            (params.get("from2_x", 1.0), params.get("from2_y", 0.0)),
            (params.get("from3_x", 1.0), params.get("from3_y", 1.0)),
            (params.get("from4_x", 0.0), params.get("from4_y", 1.0)),
        ]

        return src_points, dst_points

    def _is_identity(self) -> bool:
        """Check if transform is identity (no change)."""
        src, dst = self._get_corner_points()
        for (sx, sy), (dx, dy) in zip(src, dst):
            if abs(sx - dx) > 0.001 or abs(sy - dy) > 0.001:
                return False
        return True

    def compute_output_bbox(self, input_bboxes: Dict[str, "BBox"],
                            params: Dict[str, Any]) -> Optional["BBox"]:
        """Fixed-canvas bbox policy (NUKE "black outside" semantics).

        CornerPin warps the image *within* a fixed canvas of the input size;
        pixels warped outside the canvas are clipped to transparent. Therefore
        the output bbox is identical to the input bbox and does NOT follow the
        warped corners. (See TransformNode.compute_output_bbox for the same
        rationale.)
        """
        if not input_bboxes:
            return None
        input_bbox = input_bboxes.get("image")
        if input_bbox is None or input_bbox.is_empty:
            return None
        # Fixed canvas: bbox unchanged.
        return input_bbox

    def compute_input_roi(self, output_roi: "BBox",
                          params: Dict[str, Any],
                          input_bboxes: Optional[Dict[str, "BBox"]] = None) -> Dict[str, "BBox"]:
        """Compute input ROI under the fixed-canvas model.

        CornerPin warps within a fixed canvas (bbox unchanged), so output and
        input share the same canvas coordinate system. The input ROI for a
        given output ROI is the output ROI itself, expanded by the tile
        padding for the interpolation kernel. (The old inverse-homography
        mapping was a leftover from the abandoned "bbox follows transform"
        model and is no longer applicable.)
        """
        input_roi = output_roi
        pad = self._tile_padding(params)
        if pad > 0:
            input_roi = input_roi.expanded(pad)
        return {"image": input_roi}

    def _tile_padding(self, params: Dict[str, Any]) -> int:
        """Declare tile padding requirement for seamless boundaries.

        CornerPin is a perspective transform, requiring interpolation.
        Padding depends on interpolation mode:
        - Bilinear: 1 pixel
        - Bicubic: 2 pixels

        Args:
            params: Node parameters dictionary.

        Returns:
            Padding in pixels.
        """
        interp = params.get("interpolation", "bilinear")
        if interp == "bicubic":
            return 2
        return 1

    def requires_roi_expansion(self, params: Dict[str, Any]) -> int:
        """Legacy API - delegates to _tile_padding()."""
        return self._tile_padding(params)

    def _do_evaluate(self, inputs: Dict[str, TextureResult], ctx: Any, fb_manager: Any,
                      resource_pool: Any = None, render_mode: str = "preview") -> Dict[str, TextureResult]:
        """Evaluate the perspective transform.

        Returns an effect marker for deferred rendering by GraphEvaluatorMixin.
        """
        image = inputs.get("image")
        if not image or not image.is_valid:
            return {}

        # Check identity transform — pass through
        if self._is_identity():
            return {"result": image}

        # Get corner points
        src_points, dst_points = self._get_corner_points()

        # Corner params use image convention (Y-down: y=0 = top edge), but the
        # shader operates in texture UV space where Y increases upward (same
        # conversion Transform does in _pixel_matrix_to_uv_matrix). Flip both
        # corner sets so the top handles warp the image's top corners —
        # without this the warp acts vertically mirrored.
        src_points = [(x, 1.0 - y) for (x, y) in src_points]
        dst_points = [(x, 1.0 - y) for (x, y) in dst_points]

        # Compute homography matrix
        homography = self._compute_homography(src_points, dst_points)

        # Get interpolation mode index
        interp_name = self.parameters.get("interpolation", "bilinear")
        interp_idx = INTERPOLATION_MODES.index(interp_name) if interp_name in INTERPOLATION_MODES else 0

        return {
            "result": TextureResult(
                texture_id=-1,
                width=image.width,
                height=image.height,
            ),
            "_effect_type": "cornerpin",
            "_effect_input": image,
            "_effect_params": {
                "homography": homography,
                "interpolation": interp_idx,
            },
        }


# ── Helper Functions ──

def _solve_linear_system_8x8(A: List[List[float]], b: List[float]) -> Optional[List[float]]:
    """Solve 8x8 linear system Ax = b using Gaussian elimination.

    Returns solution vector x, or None if system is singular.
    """
    n = 8
    # Create augmented matrix
    M = [row + [b[i]] for i, row in enumerate(A)]

    # Forward elimination with partial pivoting
    for col in range(n):
        # Find pivot
        max_row = col
        max_val = abs(M[col][col])
        for row in range(col + 1, n):
            if abs(M[row][col]) > max_val:
                max_val = abs(M[row][col])
                max_row = row

        # Swap rows
        if max_row != col:
            M[col], M[max_row] = M[max_row], M[col]

        # Check for singular matrix
        if abs(M[col][col]) < 1e-10:
            return None

        # Eliminate
        pivot = M[col][col]
        for row in range(col + 1, n):
            factor = M[row][col] / pivot
            for j in range(col, n + 1):
                M[row][j] -= factor * M[col][j]

    # Back substitution
    x = [0.0] * n
    for row in range(n - 1, -1, -1):
        x[row] = M[row][n]
        for col in range(row + 1, n):
            x[row] -= M[row][col] * x[col]
        x[row] /= M[row][row]

    return x


def _apply_homography_point(h: Tuple[float, ...], x: float, y: float) -> Tuple[float, float]:
    """Apply homography to a point (x, y).

    Args:
        h: 8-element homography tuple (h00, h01, h02, h10, h11, h12, h20, h21)
        x, y: Input coordinates

    Returns:
        (x', y') transformed coordinates
    """
    h00, h01, h02, h10, h11, h12, h20, h21 = h

    # Homogeneous coordinate transformation
    # [x' y' w'] = H * [x y 1]
    # Final: (x'/w', y'/w')
    w = h20 * x + h21 * y + 1.0
    if abs(w) < 1e-10:
        return (x, y)  # Degenerate case

    x_out = (h00 * x + h01 * y + h02) / w
    y_out = (h10 * x + h11 * y + h12) / w

    return (x_out, y_out)