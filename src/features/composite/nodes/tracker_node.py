"""Tracker Node — 2D/planar tracking on the Composite page (A1).

Consumes the shared tracking algorithms (``src.shared.tracking``) that also
power the Color page.  The node itself is a pass-through in the render
graph: it holds tracked data as a serialized JSON parameter (``track_data``)
which the Analyze worker (page-driven QThread) fills in, and which the
page's bake actions convert into keyframes on downstream Transform /
CornerPin nodes ("match-move").

Track data JSON schema (versioned, keep backwards compatible)::

    {
        "version": 1,
        "media_path": str,            # source file that was analyzed
        "width": int, "height": int,  # source resolution
        "reference_frame": int,
        "mode": "translation" | "trs" | "planar",
        "transforms": {               # frame (str) -> transform
            "101": {"tx": 12.3, "ty": -4.2, "rotation": 0.5,
                    "scale_x": 1.0, "scale_y": 1.0, "status": "valid",
                    "matrix": [[...3x3...]]}   # planar only
        }
    }

``tx/ty`` are in source pixels; ``rotation`` in degrees; ``scale_*``
multiplicative.  Frames with ``status != "valid"`` are skipped by bake.
"""

import json
import logging
from typing import Any, Dict, Optional

from .base_node import Node, Port, TextureResult, ParamDef

if __name__ != "__main__":
    from src.features.composite.core.format import BBox

logger = logging.getLogger(__name__)

TRACK_DATA_VERSION = 1

TRACKER_MODES = ["translation", "trs", "planar"]


def encode_track_data(data: Dict[str, Any]) -> str:
    payload = dict(data)
    payload.setdefault("version", TRACK_DATA_VERSION)
    return json.dumps(payload, ensure_ascii=False)


def decode_track_data(raw: Any) -> Optional[Dict[str, Any]]:
    """Parse stored track_data; tolerates corruption (returns None)."""
    if not raw:
        return None
    if isinstance(raw, dict):
        payload = dict(raw)  # AUD-7503 note: copy so normalization can't mutate the caller's dict
    else:
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            return None
        if not isinstance(payload, dict):
            return None
    # Consumers index transforms with str(int(frame)); hand-edited or
    # foreign JSON may carry "010"/"12.0" style keys — normalize once at
    # the entry point so lookups can't KeyError. AUD-7501 fix: a single
    # non-numeric key ("abc"/"nan"/"inf") must skip that entry, not raise
    # out of this "tolerates corruption" helper and kill the caller's
    # UI slot (panel refresh / bake / import all call this on load).
    transforms = payload.get("transforms")
    if isinstance(transforms, dict):
        normalized: Dict[str, Any] = {}
        for key, value in transforms.items():
            try:
                frame_key = str(int(float(key)))
            except (TypeError, ValueError, OverflowError):
                continue
            if isinstance(value, dict):
                normalized[frame_key] = value
        payload["transforms"] = normalized
    return payload


class TrackerNode(Node):
    """Track a pattern region across the input and expose the result for
    match-move baking.  Evaluation is a pass-through."""

    node_type = "tracker"
    node_title = "Tracker"
    header_color = "#3aa0a0"

    DESCRIPTION = "跟踪：分析图像中的图案区域运动，可烘焙到 Transform/CornerPin 做匹配移动"
    TAGS = ["transform", "tracking", "matchmove"]
    SEARCH_ALIASES = ["tracker", "track", "matchmove", "跟踪", "追踪", "匹配移动"]

    PARAM_DEFS = [
        ParamDef("mode", "option", "translation", labels=TRACKER_MODES,
                 group="Tracker"),
        ParamDef("reference_frame", "int", 0, min_val=0, max_val=1000000,
                 step=1, group="Tracker"),
        ParamDef("center_x", "float", 0.5, min_val=0.0, max_val=1.0, step=0.01,
                 group="Pattern"),
        ParamDef("center_y", "float", 0.5, min_val=0.0, max_val=1.0, step=0.01,
                 group="Pattern"),
        ParamDef("size_x", "float", 0.12, min_val=0.01, max_val=1.0, step=0.01,
                 group="Pattern"),
        ParamDef("size_y", "float", 0.12, min_val=0.01, max_val=1.0, step=0.01,
                 group="Pattern"),
        # Serialized analysis result (see module docstring) — not animated.
        ParamDef("track_data", "str", "", hidden=True),
        # A2: smoothing window (frames) used by Bake Stabilize.
        ParamDef("stabilize_window", "int", 15, min_val=1, max_val=101,
                 step=2, group="Stabilize"),
    ]

    def __init__(self, node_id: Optional[str] = None) -> None:
        super().__init__(node_id)
        self.inputs = [
            Port("image", "input", "image"),
        ]
        self.outputs = [
            Port("image", "output", "image"),
        ]
        self.parameters = {
            "mode": "translation",
            "reference_frame": 0,
            "center_x": 0.5,
            "center_y": 0.5,
            "size_x": 0.12,
            "size_y": 0.12,
            "track_data": "",
            "stabilize_window": 15,
        }
        self._init_port_revisions()

    def compute_output_bbox(self, input_bboxes: Dict[str, "BBox"],
                            params: Dict[str, Any]) -> Optional["BBox"]:
        return input_bboxes.get("image")

    def compute_input_roi(self, output_roi: "BBox",
                          params: Dict[str, Any],
                          input_bboxes: Optional[Dict[str, "BBox"]] = None) -> Dict[str, "BBox"]:
        return {"image": output_roi}

    def _do_evaluate(self, inputs: Dict[str, TextureResult], ctx: Any, fb_manager: Any,
                     resource_pool: Any = None, render_mode: str = "preview") -> Dict[str, TextureResult]:
        """Pass-through: the tracker produces data, not pixels."""
        image = inputs.get("image")
        if image is None or not image.is_valid:
            return {}
        return {"image": image}

    # ── track data helpers ──

    def set_track_data(self, data: Dict[str, Any]) -> None:
        """Store analysis results (bumps param revision → cache invalidation)."""
        self.set_parameter("track_data", encode_track_data(data))

    def get_track_data(self) -> Optional[Dict[str, Any]]:
        return decode_track_data(self.parameters.get("track_data"))

    # ── M23.1（R08）：手工修正/增量重跟领域委托 ──

    def apply_manual_correction(self, frame: int,
                                transform: Dict[str, Any]) -> None:
        """记录一帧手工修正并写回节点（revision 自增 → 缓存失效）。"""
        from src.features.composite.core.tracker_corrections import (
            set_manual_correction,
        )
        data = self.get_track_data() or {"version": TRACK_DATA_VERSION,
                                         "transforms": {}}
        self.set_track_data(set_manual_correction(data, frame, transform))

    def get_manual_corrections(self) -> Dict[str, Dict[str, Any]]:
        from src.features.composite.core.tracker_corrections import (
            get_manual_corrections,
        )
        return get_manual_corrections(self.get_track_data())

    def plan_incremental_retrack(self):
        """失败段增量重跟计划（空段 = 无需重跟）。"""
        from src.features.composite.core.tracker_corrections import (
            plan_incremental_retrack,
        )
        return plan_incremental_retrack(self.get_track_data())

    def merge_retrack_results(self, new_transforms) -> None:
        """增量结果合并回节点（段外与手工修正保留）。"""
        from src.features.composite.core.tracker_corrections import (
            apply_track_update, plan_incremental_retrack,
        )
        data = self.get_track_data()
        if data is None:
            return
        plan = plan_incremental_retrack(data)
        self.set_track_data(apply_track_update(data, new_transforms, plan))
