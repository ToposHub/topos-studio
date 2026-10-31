"""Mesh (grid) warp node — AE Mesh Warp / Fusion Grid Warp counterpart.

C7 from the composite optimization plan.  A regular (grid_size+1)²
control-point lattice covers the frame; each point carries a normalized
displacement (``offsets`` JSON mirrored into per-point ``warp_point_{i}_
dx/dy`` animation channels, roto_shape's dynamic-parameter pattern so
the keyframes tree can edit each component independently).  The GPU
evaluator uploads the lattice as a small RG32F texture and the fragment
shader bilinearly blends the four surrounding corner displacements to
warp the sampling UV.

Tile safety: padding is the lattice's maximum displacement norm in
pixels (evaluated dynamically, zero when the grid is identity —
identity passes through with no expansion at all).
"""

from __future__ import annotations

import json
import math
from typing import Any, Dict, List, Optional

from .base_node import Node, ParamDef, Port, TextureResult
from src.features.composite.core.format import BBox

DEFAULT_GRID = 4


def _point_param_names(index: int) -> Dict[str, str]:
    return {
        "dx": f"warp_point_{index}_dx",
        "dy": f"warp_point_{index}_dy",
    }


class WarpNode(Node):
    """Grid/mesh warp driven by per-point displacement channels."""

    node_type = "mesh_warp"
    node_title = "Mesh Warp"
    i18n_title_key = "nodes.mesh_warp.title"
    header_color = "#27AE60"
    IS_PLACEHOLDER = False

    SEARCH_ALIASES = [
        "warp", "mesh warp", "grid warp", "distort grid",
        "网格变形", "变形", "网格扭曲",
    ]
    DESCRIPTION = "网格变形：拖拽格点做局部位移（AE Mesh Warp / Fusion Grid Warp）"
    TAGS = ["filter", "distort", "mesh", "warp"]

    PARAM_DEFS = [
        # Grid cells per axis; points per axis = grid_size + 1. Static by
        # design — changing it rebuilds the point-channel layout.
        ParamDef("grid_size", "int", DEFAULT_GRID, min_val=4, max_val=16, step=1),
        ParamDef("edge_mode", "option", "black", labels=["black", "clamp", "mirror"]),
    ]

    BYPASS_MAPPING = {"result": "image"}

    def __init__(self, node_id: Optional[str] = None) -> None:
        super().__init__(node_id)
        self.inputs = [Port("image", "input", "image")]
        self.outputs = [Port("result", "output", "image")]
        grid = DEFAULT_GRID
        self.parameters = {
            "grid_size": grid,
            "edge_mode": "black",
            "offsets": json.dumps(
                [[0.0, 0.0]] * self.point_count(grid), separators=(",", ":")),
        }
        self._sync_point_parameters()
        self._init_port_revisions()

    # ── Lattice helpers ──────────────────────────────────────────────────

    @staticmethod
    def point_count(grid: int) -> int:
        return (grid + 1) * (grid + 1)

    def point_index(self, row: int, col: int) -> int:
        grid = self.current_grid()
        return row * (grid + 1) + col

    def current_grid(self) -> int:
        return max(4, min(16, int(self.parameters.get("grid_size", DEFAULT_GRID))))

    @staticmethod
    def parse_offsets(value: Any, count: int) -> List[List[float]]:
        """Parse the persisted offsets JSON, clamped and padded to count."""
        try:
            raw = json.loads(value) if isinstance(value, str) else value
        except (TypeError, ValueError, json.JSONDecodeError):
            raw = []
        offsets: List[List[float]] = []
        for item in raw if isinstance(raw, list) else []:
            try:
                offsets.append([max(-1.0, min(1.0, float(item[0]))),
                                max(-1.0, min(1.0, float(item[1])))])
            except (TypeError, IndexError, ValueError):
                continue
        while len(offsets) < count:
            offsets.append([0.0, 0.0])
        return offsets[:count]

    @classmethod
    def from_dict(cls, data: dict) -> "WarpNode":
        """Deserialize; guarantee the point channels match the JSON lattice.

        base from_dict writes parameters directly (no set_parameter), so a
        project that carries an offsets JSON but no warp_point_* channels
        (legacy/hand-edited data) would otherwise keep the all-zero
        channels written by __init__ — and apply_point_parameter_overrides
        would silently zero the whole lattice back to identity.
        """
        node = super().from_dict(data)
        try:
            node._sync_point_parameters()
        except Exception:
            pass
        return node

    def get_offsets(self) -> List[List[float]]:
        return self.parse_offsets(self.parameters.get("offsets", "[]"),
                                  self.point_count(self.current_grid()))

    def set_offsets(self, offsets: List[List[float]]) -> None:
        self.set_parameter("offsets", json.dumps(
            offsets, separators=(",", ":")))

    def _sync_point_parameters(self) -> None:
        """Mirror offsets into numeric animation channels (roto pattern)."""
        offsets = self.get_offsets()
        for i, (dx, dy) in enumerate(offsets):
            names = _point_param_names(i)
            self.parameters[names["dx"]] = float(dx)
            self.parameters[names["dy"]] = float(dy)

    def get_param_defs(self) -> Dict[str, ParamDef]:
        """Static defs + one dx/dy pair per lattice point (roto pattern)."""
        definitions = super().get_param_defs()
        grid = self.current_grid()
        for i in range(self.point_count(grid)):
            names = _point_param_names(i)
            for key in ("dx", "dy"):
                definitions[names[key]] = ParamDef(
                    names[key], "float",
                    float(self.parameters.get(names[key], 0.0)),
                    min_val=-1.0, max_val=1.0, step=0.001, decimals=4,
                    animated=True, group="Warp Points",
                )
        return definitions

    @staticmethod
    def _resample_lattice(offsets: List[List[float]], old_grid: int,
                          new_grid: int) -> List[List[float]]:
        """Bilinearly resample a lattice between grid resolutions (AUD-7710).

        Lattice points are addressed by ``(row, col)`` with flat index
        ``row * (grid + 1) + col``. A ``grid_size`` change must move each
        displacement to the same *normalized* lattice position — flat
        truncation/padding rebinds every point to a different (row, col)
        (e.g. flat index 7 is (1, 2) on a 4-grid but (0, 7) on an 8-grid),
        which made the whole deformation jump to wrong points. The
        displacement field is resampled bilinearly on normalized (row, col)
        coordinates so the visual deformation is preserved; the four
        lattice corners always map exactly.
        """
        old_pts = old_grid + 1
        new_pts = new_grid + 1

        def at(row: int, col: int) -> List[float]:
            idx = row * old_pts + col
            if 0 <= idx < len(offsets):
                return offsets[idx]
            return [0.0, 0.0]

        result: List[List[float]] = []
        for r in range(new_pts):
            fy = r / new_grid * old_grid
            y0 = int(math.floor(fy))
            y1 = min(old_pts - 1, y0 + 1)
            ty = fy - y0
            for c in range(new_pts):
                fx = c / new_grid * old_grid
                x0 = int(math.floor(fx))
                x1 = min(old_pts - 1, x0 + 1)
                tx = fx - x0
                p00 = at(y0, x0)
                p10 = at(y0, x1)
                p01 = at(y1, x0)
                p11 = at(y1, x1)
                dx = ((p00[0] * (1.0 - tx) + p10[0] * tx) * (1.0 - ty)
                      + (p01[0] * (1.0 - tx) + p11[0] * tx) * ty)
                dy = ((p00[1] * (1.0 - tx) + p10[1] * tx) * (1.0 - ty)
                      + (p01[1] * (1.0 - tx) + p11[1] * tx) * ty)
                result.append([max(-1.0, min(1.0, dx)),
                               max(-1.0, min(1.0, dy))])
        return result

    def set_parameter(self, key: str, value: Any) -> None:
        # AUD-7710: capture the OLD lattice (parsed under the OLD grid)
        # before super() writes the new grid_size — get_offsets() after the
        # write would interpret the old JSON against the new point count.
        old_grid = self.current_grid()
        old_offsets = self.get_offsets() if key == "grid_size" else None
        super().set_parameter(key, value)
        if key == "offsets":
            self._sync_point_parameters()
        elif key == "grid_size":
            new_grid = self.current_grid()
            if new_grid != old_grid and old_offsets is not None:
                # Resample by normalized (row, col) so each displacement
                # keeps its position on the frame (see _resample_lattice).
                self.set_offsets(self._resample_lattice(
                    old_offsets, old_grid, new_grid))
            else:
                # Same grid (rejected/clamped write): re-encode so stale
                # extra points from legacy data don't linger.
                self.set_offsets(self.get_offsets())
        elif key.startswith("warp_point_"):
            try:
                tokens = key.split("_")
                index = int(tokens[2])
                channel = tokens[3]
                offsets = self.get_offsets()
                if 0 <= index < len(offsets):
                    axis = 0 if channel == "dx" else 1
                    new = max(-1.0, min(1.0, float(value)))
                    changed = offsets[index][axis] != new
                    offsets[index][axis] = new
                    # Deserialization no-op must not re-encode the JSON
                    # (keeps old projects byte-identical, roto pattern).
                    if changed:
                        self.parameters["offsets"] = json.dumps(
                            offsets, separators=(",", ":"))
            except (TypeError, ValueError, IndexError):
                pass

    @classmethod
    def apply_point_parameter_overrides(
            cls, offsets: List[List[float]],
            parameters: Dict[str, Any]) -> List[List[float]]:
        """Apply static/interpolated point channels to a parsed lattice."""
        for i, offset in enumerate(offsets):
            names = _point_param_names(i)
            for key, axis in (("dx", 0), ("dy", 1)):
                if names[key] in parameters:
                    offset[axis] = max(
                        -1.0, min(1.0, float(parameters[names[key]])))
        return offsets

    # ── Tile safety (lens_distortion pattern) ────────────────────────────

    _REF_WIDTH = 3840.0  # conservative frame width when unknown

    def _tile_padding(self, params: Dict[str, Any]) -> int:
        """Padding bound: the lattice's maximum displacement norm in px.

        Identity grid (all offsets zero) needs no padding at all.
        """
        grid = max(4, min(16, int(params.get("grid_size", DEFAULT_GRID))))
        offsets = self.parse_offsets(params.get("offsets", "[]"),
                                     self.point_count(grid))
        width = float(params.get("width") or self._REF_WIDTH)
        # dy is normalized by frame HEIGHT (viewer/drag convention), dx by
        # width — take each point's max per-axis pixel displacement. Height
        # unknown → reuse width (conservative: width ≥ height).
        height = float(params.get("height") or width)
        max_px = max(
            (max(abs(dx) * width, abs(dy) * height) for dx, dy in offsets),
            default=0.0)
        if max_px <= 0.0:
            return 0
        return max(1, int(math.ceil(max_px)))

    def compute_output_bbox(self, input_bboxes: Dict[str, "BBox"],
                            params: Dict[str, Any]) -> Optional["BBox"]:
        return input_bboxes.get("image")

    def compute_input_roi(self, output_roi: "BBox",
                          params: Dict[str, Any],
                          input_bboxes: Optional[Dict[str, "BBox"]] = None
                          ) -> Dict[str, "BBox"]:
        pad = self._tile_padding(params)
        if pad > 0 and output_roi is not None:
            ref = (input_bboxes or {}).get("image")
            if ref is not None and getattr(ref, "width", 0) > 0:
                pad = self._tile_padding(dict(
                    params, width=ref.width,
                    height=float(getattr(ref, "height", 0)) or ref.width))
            return {"image": output_roi.expanded(pad)}
        return {"image": output_roi}

    def requires_roi_expansion(self, params: Dict[str, Any]) -> int:
        return self._tile_padding(params)

    # ── Evaluation (marker → renderer dispatch) ──────────────────────────

    def _do_evaluate(self, inputs: Dict[str, TextureResult], ctx: Any,
                     fb_manager: Any, resource_pool: Any = None,
                     render_mode: str = "preview") -> Dict[str, TextureResult]:
        image = inputs.get("image")
        if not image or not image.is_valid:
            return {}
        params = dict(self.parameters)
        params["width"] = float(image.width)
        params["height"] = float(image.height)
        offsets = self.apply_point_parameter_overrides(
            self.get_offsets(), self.parameters)
        params["offsets"] = offsets
        return {
            "result": TextureResult(
                texture_id=-1,
                width=image.width,
                height=image.height,
            ),
            "_effect_type": "mesh_warp",
            "_effect_input": image,
            "_effect_params": params,
        }
