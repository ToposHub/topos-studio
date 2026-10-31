"""Lens Distortion Node — Brown-Conrady undistort / distort (A4).

OpenCV convention (interoperable with cv2.undistort / calibration):
radial k1, k2, k3 + tangential p1, p2, principal point offset.

* ``undistort`` removes lens distortion: for each output pixel it solves
  for the source location (iterative inverse of the distortion model).
* ``distort`` applies the model forward: closed form per pixel.

Evaluation runs on the GPU (fragment shader evaluates the model per
pixel against a bilinear tap — no precomputed map needed at these
iteration counts); the model math here in the node mirrors the shader so
CPU tests exercise the same formulas.

Tile safety: padding estimates the maximum radial displacement as
``max(|k1|,|k2|,|k3|) * 0.25 * max(w, h)`` which bounds the Brown-Conrady
radius term for typical lenses.
"""

import math
from typing import Any, Dict, Optional

from .base_node import Node, Port, TextureResult, ParamDef

if __name__ != "__main__":
    from src.features.composite.core.format import BBox

LENS_MODES = ["undistort", "distort"]
EDGE_MODES = ["black", "clamp", "mirror"]


def distortion_delta(x: float, y: float, k1: float, k2: float, k3: float,
                     p1: float, p2: float) -> tuple:
    """Brown-Conrady displacement for normalized coords (OpenCV model).

    Args:
        x, y: normalized coordinates relative to the principal point,
            scaled by the focal length (unitless, cv2 convention).
    Returns:
        (dx, dy): the displacement (distorted minus undistorted position).
    """
    r2 = x * x + y * y
    radial = 1.0 + k1 * r2 + k2 * r2 * r2 + k3 * r2 * r2 * r2
    dx = x * (radial - 1.0) + 2.0 * p1 * x * y + p2 * (r2 + 2.0 * x * x)
    dy = y * (radial - 1.0) + p1 * (r2 + 2.0 * y * y) + 2.0 * p2 * x * y
    return dx, dy


def map_point(px: float, py: float, w: float, h: float,
              k1: float, k2: float, k3: float,
              p1: float, p2: float,
              cx: float = 0.5, cy: float = 0.5,
              mode: str = "undistort",
              iterations: int = 12) -> tuple:
    """Map output pixel (px, py) to the source pixel to sample.

    ``undistort`` mode (REMOVE distortion): the output is in ideal
    coordinates, so each output pixel samples the distorted input at the
    FORWARD Brown-Conrady position — closed form, exactly the map
    cv2.initUndistortRectifyMap builds.
    ``distort`` mode (ADD distortion): output pixel samples the input at
    D⁻¹(p) — fixed-point iteration of the inverse.
    Returns the sampled source pixel (may fall outside the frame).
    """
    # normalized coordinates — BOTH axes are divided by the frame HEIGHT,
    # matching the GPU shader's `(px - c) / u_resolution.y` so circles stay
    # circular and the CPU reference model stays in lockstep with the GPU.
    ax = (px - cx * w) / h
    ay = (py - cy * h) / h

    if k1 == 0.0 and k2 == 0.0 and k3 == 0.0 and p1 == 0.0 and p2 == 0.0:
        return px, py

    if mode == "undistort":
        dx, dy = distortion_delta(ax, ay, k1, k2, k3, p1, p2)
        nx, ny = ax + dx, ay + dy
    else:
        # Inverse iteration: divide out the radial factor and subtract the
        # tangential term at the current estimate. Converges quickly for
        # typical lens coefficients (stable where a naive fixed-point
        # q = p - delta(q) oscillates at large radii).
        nx, ny = ax, ay
        for _ in range(iterations):
            r2 = nx * nx + ny * ny
            radial = 1.0 + k1 * r2 + k2 * r2 * r2 + k3 * r2 * r2 * r2
            radial = radial if abs(radial) > 1e-9 else 1e-9
            tx = 2.0 * p1 * nx * ny + p2 * (r2 + 2.0 * nx * nx)
            ty = p1 * (r2 + 2.0 * ny * ny) + 2.0 * p2 * nx * ny
            nx = (ax - tx) / radial
            ny = (ay - ty) / radial

    return (nx * h + cx * w, ny * h + cy * h)


class LensDistortionNode(Node):
    """Apply or remove Brown-Conrady lens distortion (OpenCV convention)."""

    node_type = "lens_distortion"
    node_title = "Lens Distortion"
    header_color = "#5a6a8a"

    DESCRIPTION = ("镜头畸变：Brown-Conrady 模型去畸变/加畸变（OpenCV 约定，"
                   "k1-k3 径向 + p1/p2 切向），配合跟踪做 matchmove 工作流")
    TAGS = ["filter", "distort", "lens", "matchmove"]
    SEARCH_ALIASES = ["lens", "distortion", "undistort", "镜头畸变", "去畸变",
                      "畸变"]

    PARAM_DEFS = [
        ParamDef("mode", "option", "undistort", labels=LENS_MODES),
        ParamDef("k1", "float", 0.0, min_val=-1.0, max_val=1.0, step=0.001),
        ParamDef("k2", "float", 0.0, min_val=-1.0, max_val=1.0, step=0.001),
        ParamDef("k3", "float", 0.0, min_val=-1.0, max_val=1.0, step=0.001),
        ParamDef("p1", "float", 0.0, min_val=-0.5, max_val=0.5, step=0.001),
        ParamDef("p2", "float", 0.0, min_val=-0.5, max_val=0.5, step=0.001),
        ParamDef("center_x", "float", 0.5, min_val=0.0, max_val=1.0, step=0.005),
        ParamDef("center_y", "float", 0.5, min_val=0.0, max_val=1.0, step=0.005),
        ParamDef("edge_mode", "option", "black", labels=EDGE_MODES),
        ParamDef("filter_enabled", "bool", True),
    ]

    BYPASS_MAPPING = {"result": "image"}

    def __init__(self, node_id: Optional[str] = None) -> None:
        super().__init__(node_id)
        self.inputs = [
            Port("image", "input", "image"),
        ]
        self.outputs = [
            Port("result", "output", "image"),
        ]
        self.parameters = {
            "mode": "undistort",
            "k1": 0.0,
            "k2": 0.0,
            "k3": 0.0,
            "p1": 0.0,
            "p2": 0.0,
            "center_x": 0.5,
            "center_y": 0.5,
            "edge_mode": "black",
            "filter_enabled": True,
        }

    def compute_output_bbox(self, input_bboxes: Dict[str, "BBox"],
                            params: Dict[str, Any]) -> Optional["BBox"]:
        return input_bboxes.get("image")

    def compute_input_roi(self, output_roi: "BBox",
                          params: Dict[str, Any],
                          input_bboxes: Optional[Dict[str, "BBox"]] = None) -> Dict[str, "BBox"]:
        pad = self._tile_padding(params)
        if pad > 0 and output_roi is not None:
            # Scale the bound to the actual input frame when it is known
            # (ROI propagation passes the upstream bbox).
            ref = (input_bboxes or {}).get("image")
            if ref is not None and getattr(ref, "height", 0) > 0:
                pad = self._tile_padding(dict(params, height=ref.height))
            return {"image": output_roi.expanded(pad)}
        return {"image": output_roi}

    # Displacement bound: |delta| ≈ |p|·|radial−1| + tangential in
    # height-normalised units; |p| ≤ r and r stays ≲1.3 for common frame
    # aspects, so ~2.5·kmax covers the radial chain and ~3·pmax the
    # tangential pair.
    _RADIAL_BOUND = 2.5
    _TANGENT_BOUND = 3.0
    _REF_HEIGHT = 2160.0  # conservative frame height when unknown

    def _tile_padding(self, params: Dict[str, Any]) -> int:
        """Pixel padding so spatial sampling stays inside the input."""
        kmax = max(abs(float(params.get(k, 0.0) or 0.0))
                   for k in ("k1", "k2", "k3"))
        tmax = max(abs(float(params.get(k, 0.0) or 0.0))
                   for k in ("p1", "p2"))
        if kmax <= 0.0 and tmax <= 0.0:
            return 0
        height = float(params.get("height") or self._REF_HEIGHT)
        pad = height * (self._RADIAL_BOUND * kmax + self._TANGENT_BOUND * tmax)
        return max(1, int(math.ceil(pad)))

    def requires_roi_expansion(self, params: Dict[str, Any]) -> int:
        return self._tile_padding(params)

    def _do_evaluate(self, inputs: Dict[str, TextureResult], ctx: Any, fb_manager: Any,
                     resource_pool: Any = None, render_mode: str = "preview") -> Dict[str, TextureResult]:
        """Marker dict → renderer dispatches to _apply_lens_distortion."""
        image = inputs.get("image")
        if not image or not image.is_valid:
            return {}
        if not self.parameters.get("filter_enabled", True):
            return {"result": image}
        return {
            "result": TextureResult(
                texture_id=-1,
                width=image.width,
                height=image.height,
            ),
            "_effect_type": "lens_distortion",
            "_effect_input": image,
            "_effect_params": dict(self.parameters),
        }
