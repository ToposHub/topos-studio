"""Paint node — clone / erase / solid brush strokes on the input image.

C2 from the composite optimization plan (Nuke RotoPaint / AE Clone Stamp
counterpart).  The node owns a serializable **stroke list** (JSON param
``strokes``); the evaluator rasterizes strokes on the CPU (QPainter paths
+ numpy feathering), samples the clone source from the node's input
image, and produces BOTH the painted result and a stroke-union matte on
the ``mask`` output (white = painted area, suitable for ai_remove /
keyers — the matte is written into RGB, not only alpha).

Each stroke (normalized coordinates, origin top-left):

    {
      "id": "<stable id>",
      "type": "clone" | "erase" | "solid",
      "points": [[x, y], ...],     # normalized 0..1 polyline
      "size": 0.02,                # normalized brush radius (× canvas width)
      "feather": 0.5,              # 0 = hard edge, 1 = fully soft
      "opacity": 1.0,
      "src_offset": [dx, dy],      # clone only: source = dst - offset
      "color": [r, g, b, a],       # solid only (linear float, HDR allowed)
      "frame_range": null,         # null = always on, else [start, end]
                                     inclusive playhead gates (CP-003)
    }

    Frame gating (CP-003): ``_do_evaluate`` filters strokes by the
    evaluation context's ``frame_number`` before handing them to the
    evaluator; strokes with a ``frame_range`` only paint while the
    playhead is inside ``[start, end]`` (inclusive).  The node also
    reports itself as frame-dependent via ``params_require_frame_cache``
    whenever any stroke carries a range, so the graph cache cannot reuse
    a painted frame outside its range.
"""

from __future__ import annotations

import json
import uuid
from typing import Any, Dict, List, Optional

from .base_node import Node, ParamDef, Port, TextureResult


STROKE_TYPES = ("clone", "erase", "solid")

_DEFAULT_PARAMS = {
    "strokes": "[]",
    "output_width": 1920,
    "output_height": 1080,
}


class PaintNode(Node):
    """Paint strokes (clone/erase/solid) onto the upstream image.

    ``strokes`` stays JSON for project compatibility — the viewer's PAINT
    tool appends/removes whole strokes and issues one undo per stroke.
    The evaluator (filter_evaluator._apply_paint) does the CPU raster.
    """

    node_type = "paint"
    node_title = "Paint"
    i18n_title_key = "nodes.paint.title"
    header_color = "#D35400"
    IS_PLACEHOLDER = False
    supports_layer_root = True

    SEARCH_ALIASES = [
        "paint", "clone", "clone stamp", "erase", "retouch",
        "绘画", "克隆", "仿制图章", "擦除", "修补",
    ]
    DESCRIPTION = "绘画/克隆/擦除笔划图层（克隆修复、遮挡擦除、纯色涂抹）"
    TAGS = ["effect", "paint", "cleanup"]

    PARAM_DEFS = [
        ParamDef("strokes", "str", "[]", group="Strokes"),
        # ── Brush defaults for the viewer PAINT tool (C2-3). The viewport
        # reads these when committing a stroke; [ / ] adjust brush_size.
        ParamDef("brush_type", "option", "clone",
                 labels=list(STROKE_TYPES), group="Brush"),
        ParamDef("brush_size", "float", 40.0, min_val=1.0, max_val=1024.0,
                 step=1.0, group="Brush"),
        ParamDef("brush_feather", "float", 0.5, min_val=0.0, max_val=1.0,
                 step=0.05, group="Brush"),
        ParamDef("brush_opacity", "float", 1.0, min_val=0.0, max_val=1.0,
                 step=0.05, group="Brush"),
        ParamDef("brush_color_r", "float", 0.0, min_val=0.0, max_val=65535.0,
                 step=0.01, group="Brush"),
        ParamDef("brush_color_g", "float", 0.0, min_val=0.0, max_val=65535.0,
                 step=0.01, group="Brush"),
        ParamDef("brush_color_b", "float", 0.0, min_val=0.0, max_val=65535.0,
                 step=0.01, group="Brush"),
        ParamDef("brush_color_a", "float", 1.0, min_val=0.0, max_val=1.0,
                 step=0.05, group="Brush"),
        ParamDef("output_width", "int", 1920, min_val=1, max_val=16384,
                 step=1, hidden=True),
        ParamDef("output_height", "int", 1080, min_val=1, max_val=16384,
                 step=1, hidden=True),
    ]

    def __init__(self, node_id: Optional[str] = None) -> None:
        super().__init__(node_id)
        self.inputs = [Port("image", "input", "image")]
        self.outputs = [
            Port("result", "output", "image"),
            Port("mask", "output", "image"),
        ]
        self.parameters = {
            **_DEFAULT_PARAMS,
            "brush_type": "clone",
            "brush_size": 40.0,
            "brush_feather": 0.5,
            "brush_opacity": 1.0,
            "brush_color_r": 0.0,
            "brush_color_g": 0.0,
            "brush_color_b": 0.0,
            "brush_color_a": 1.0,
        }
        self._init_port_revisions()

    # ── Stroke list access ──────────────────────────────────────────────

    @staticmethod
    def parse_strokes(value: Any) -> List[Dict[str, Any]]:
        """Parse the persisted strokes JSON with per-field coercion.

        Unknown types / malformed entries are dropped (never raise) so a
        hand-edited project file cannot break evaluation.  Missing ids get
        a stable-per-parse fallback (empty string) — callers that need a
        persistent id should assign one when appending the stroke.
        """
        try:
            raw = json.loads(value) if isinstance(value, str) else value
        except (TypeError, ValueError, json.JSONDecodeError):
            raw = []
        strokes: List[Dict[str, Any]] = []
        for item in raw if isinstance(raw, list) else []:
            if not isinstance(item, dict):
                continue
            stroke_type = str(item.get("type", "")).lower()
            if stroke_type not in STROKE_TYPES:
                continue
            points: List[List[float]] = []
            for pt in item.get("points", []) or []:
                try:
                    x = max(0.0, min(1.0, float(pt[0])))
                    y = max(0.0, min(1.0, float(pt[1])))
                    points.append([x, y])
                except (TypeError, IndexError, ValueError):
                    continue
            if not points:
                continue
            try:
                size = max(1e-4, min(0.5, float(item.get("size", 0.02))))
            except (TypeError, ValueError):
                size = 0.02
            try:
                feather = max(0.0, min(1.0, float(item.get("feather", 0.5))))
            except (TypeError, ValueError):
                feather = 0.5
            try:
                opacity = max(0.0, min(1.0, float(item.get("opacity", 1.0))))
            except (TypeError, ValueError):
                opacity = 1.0
            src_offset = item.get("src_offset") or [0.0, 0.0]
            try:
                offset = [float(src_offset[0]), float(src_offset[1])]
            except (TypeError, IndexError, ValueError):
                offset = [0.0, 0.0]
            color = item.get("color") or [0.0, 0.0, 0.0, 1.0]
            try:
                rgba = [max(0.0, float(color[0])), max(0.0, float(color[1])),
                        max(0.0, float(color[2])), max(0.0, min(1.0, float(color[3])))]
            except (TypeError, IndexError, ValueError):
                rgba = [0.0, 0.0, 0.0, 1.0]
            frame_range = item.get("frame_range")
            if isinstance(frame_range, (list, tuple)) and len(frame_range) == 2:
                try:
                    frame_range = [int(frame_range[0]), int(frame_range[1])]
                except (TypeError, ValueError):
                    frame_range = None
            else:
                frame_range = None
            strokes.append({
                "id": str(item.get("id", "")),
                "type": stroke_type,
                "points": points,
                "size": size,
                "feather": feather,
                "opacity": opacity,
                "src_offset": offset,
                "color": rgba,
                "frame_range": frame_range,
            })
        return strokes

    @staticmethod
    def make_stroke(stroke_type: str, points: List[List[float]], *,
                    size: float = 0.02, feather: float = 0.5,
                    opacity: float = 1.0,
                    src_offset: Optional[List[float]] = None,
                    color: Optional[List[float]] = None) -> Dict[str, Any]:
        """Build one stroke dict with a fresh stable id (viewer tool uses
        this so undo entries can reference ``id``)."""
        return {
            "id": uuid.uuid4().hex,
            "type": stroke_type if stroke_type in STROKE_TYPES else "solid",
            "points": [list(pt) for pt in points],
            "size": max(1e-4, min(0.5, float(size))),
            "feather": max(0.0, min(1.0, float(feather))),
            "opacity": max(0.0, min(1.0, float(opacity))),
            "src_offset": list(src_offset or [0.0, 0.0]),
            "color": list(color or [0.0, 0.0, 0.0, 1.0]),
            "frame_range": None,
        }

    def get_strokes(self) -> List[Dict[str, Any]]:
        return self.parse_strokes(self.parameters.get("strokes", "[]"))

    @staticmethod
    def strokes_active_at(strokes: List[Dict[str, Any]],
                          frame_number: int) -> List[Dict[str, Any]]:
        """Filter strokes by inclusive ``frame_range`` gates (CP-003).

        ``frame_range`` of None means the stroke is always active.  A
        malformed range (start > end) never activates — the safer default
        for hand-edited project files is "no paint" over "surprise paint".
        """
        active: List[Dict[str, Any]] = []
        for stroke in strokes:
            gate = stroke.get("frame_range")
            if gate is None:
                active.append(stroke)
                continue
            try:
                start, end = int(gate[0]), int(gate[1])
            except (TypeError, ValueError, IndexError):
                active.append(stroke)
                continue
            if start <= frame_number <= end:
                active.append(stroke)
        return active

    @classmethod
    def params_require_frame_cache(cls, params: Dict[str, object]) -> bool:
        """Frame-dependent whenever any stroke is range-gated (CP-003).

        Keeps the graph cache key frame-scoped so a painted frame cannot
        leak outside its ``frame_range`` via a stale cache entry.
        """
        strokes = cls.parse_strokes(params.get("strokes", "[]"))
        return any(s.get("frame_range") is not None for s in strokes)

    def set_strokes(self, strokes: List[Dict[str, Any]]) -> None:
        """Replace the whole stroke list (single serialized param → single
        undo entry per stroke edit on the viewer side)."""
        self.set_parameter("strokes", json.dumps(
            strokes, separators=(",", ":"), ensure_ascii=False))

    # ── Evaluation (marker; raster happens in filter_evaluator) ─────────

    def _do_evaluate(self, inputs: Dict[str, TextureResult], ctx: Any,
                     fb_manager: Any, resource_pool: Any = None,
                     render_mode: str = "preview") -> Dict[str, TextureResult]:
        image_input = inputs.get("image")
        has_image = image_input is not None and image_input.is_valid
        width = int(image_input.width) if has_image else int(
            self.parameters.get("output_width", 1920))
        height = int(image_input.height) if has_image else int(
            self.parameters.get("output_height", 1080))
        width = max(1, width)
        height = max(1, height)
        strokes = self.parse_strokes(self.parameters.get("strokes", "[]"))
        # CP-003: apply the inclusive frame_range gates at the playhead.
        # An empty active list keeps the two-port contract via the
        # evaluator's no-stroke passthrough (zeroed mask output).
        frame_number = int(getattr(ctx, "frame_number", 0) or 0)
        strokes = self.strokes_active_at(strokes, frame_number)
        # The mask output must carry the matte in RGB: ai_remove and the
        # keyers read back only the first three channels (white = painted).
        return {
            "result": TextureResult(
                texture_id=-1, width=width, height=height,
                alpha_premultiplied=has_image, color_space="linear"),
            "mask": TextureResult(
                texture_id=-1, width=width, height=height,
                alpha_premultiplied=False, color_space="linear"),
            "_effect_type": "paint",
            "_effect_input": image_input if has_image else None,
            "_effect_params": {
                "strokes": strokes,
                "output_width": width,
                "output_height": height,
            },
        }
