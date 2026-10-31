"""Bezier/roto mask node used by both Layer and Node views."""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from .base_node import Node, ParamDef, Port, TextureResult

logger = logging.getLogger(__name__)


_DEFAULT_POINTS = json.dumps([
    {"pos": [0.25, 0.25], "c1": [0.25, 0.25], "c2": [0.25, 0.25]},
    {"pos": [0.75, 0.25], "c1": [0.75, 0.25], "c2": [0.75, 0.25]},
    {"pos": [0.75, 0.75], "c1": [0.75, 0.75], "c2": [0.75, 0.75]},
    {"pos": [0.25, 0.75], "c1": [0.25, 0.75], "c2": [0.25, 0.75]},
])


class RotoShapeNode(Node):
    """Render a free-form roto path or a quick primitive shape.

    ``points`` remains JSON for project compatibility.  Each point may have
    ``pos``, incoming ``c1`` and outgoing ``c2`` Bezier handles in normalized
    image coordinates.  The evaluator flattens the curve for the GPU fill.
    """

    node_type = "roto_shape"
    node_title = "Roto Shape"
    header_color = "#8E44AD"
    IS_PLACEHOLDER = False

    PARAM_DEFS = [
        ParamDef("shape_type", "option", "bezier",
                 labels=["rectangle", "ellipse", "polygon", "bezier"],
                 group="Shape"),
        ParamDef("points", "str", _DEFAULT_POINTS, group="Shape"),
        ParamDef("center_x", "float", 0.5, min_val=0.0, max_val=1.0,
                 step=0.01, group="Transform", animated=True),
        ParamDef("center_y", "float", 0.5, min_val=0.0, max_val=1.0,
                 step=0.01, group="Transform", animated=True),
        ParamDef("width", "float", 0.5, min_val=0.0, max_val=2.0,
                 step=0.01, group="Transform", animated=True),
        ParamDef("height", "float", 0.5, min_val=0.0, max_val=2.0,
                 step=0.01, group="Transform", animated=True),
        ParamDef("rotation", "float", 0.0, min_val=-180.0, max_val=180.0,
                 step=1.0, group="Transform", animated=True),
        ParamDef("feather", "float", 0.0, min_val=0.0, max_val=0.5,
                 step=0.01, group="Edge"),
        ParamDef("opacity", "float", 1.0, min_val=0.0, max_val=1.0,
                 step=0.01, group="Edge"),
        ParamDef("invert", "bool", False, group="Edge"),
        ParamDef("output_mode", "option", "result",
                 labels=["matte", "result"], group="Output"),
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
            Port("matte", "output", "image"),
        ]
        self.parameters = {
            "shape_type": "bezier",
            "points": _DEFAULT_POINTS,
            "center_x": 0.5,
            "center_y": 0.5,
            "width": 0.5,
            "height": 0.5,
            "rotation": 0.0,
            "feather": 0.0,
            "opacity": 1.0,
            "invert": False,
            "output_mode": "result",
            "output_width": 1920,
            "output_height": 1080,
        }
        self._sync_point_parameters(self.parse_points(self.parameters["points"]))
        self._init_port_revisions()

    @staticmethod
    def _point_param_names(index: int) -> Dict[str, str]:
        prefix = f"roto_point_{index}"
        return {
            "x": f"{prefix}_x", "y": f"{prefix}_y",
            "c1_x": f"{prefix}_c1_x", "c1_y": f"{prefix}_c1_y",
            "c2_x": f"{prefix}_c2_x", "c2_y": f"{prefix}_c2_y",
            "feather": f"{prefix}_feather",
        }

    def _sync_point_parameters(self, points: List[Dict[str, List[float]]]) -> None:
        """Mirror path coordinates into numeric, independently animatable params."""
        for index, point in enumerate(points[:16]):
            names = self._point_param_names(index)
            self.parameters[names["x"]] = float(point["pos"][0])
            self.parameters[names["y"]] = float(point["pos"][1])
            self.parameters[names["c1_x"]] = float(point["c1"][0])
            self.parameters[names["c1_y"]] = float(point["c1"][1])
            self.parameters[names["c2_x"]] = float(point["c2"][0])
            self.parameters[names["c2_y"]] = float(point["c2"][1])

    def get_param_defs(self) -> Dict[str, ParamDef]:
        """Return static definitions plus the current path point channels.

        ``points`` remains JSON for project compatibility, while the viewer
        and timeline edit each coordinate independently.  Generated
        definitions put those channels on the same contract used by
        Properties, the edit service, snapshots, and render interpolation.
        """
        definitions = super().get_param_defs()
        points = self.parse_points(self.parameters.get("points", "[]"))
        for index, point in enumerate(points[:16]):
            names = self._point_param_names(index)
            values = {
                names["x"]: point["pos"][0],
                names["y"]: point["pos"][1],
                names["c1_x"]: point["c1"][0],
                names["c1_y"]: point["c1"][1],
                names["c2_x"]: point["c2"][0],
                names["c2_y"]: point["c2"][1],
            }
            for name, default in values.items():
                definitions[name] = ParamDef(
                    name,
                    "float",
                    float(default),
                    min_val=0.0,
                    max_val=1.0,
                    step=0.01,
                    decimals=3,
                    animated=True,
                    group="Roto Points",
                )
            definitions[names["feather"]] = ParamDef(
                names["feather"],
                "float",
                float(point.get("feather", self.parameters.get("feather", 0.0))),
                min_val=0.0,
                max_val=0.5,
                step=0.01,
                decimals=3,
                animated=True,
                group="Roto Points",
            )
        return definitions

    @classmethod
    def apply_point_parameter_overrides(
            cls, points: List[Dict[str, Any]],
            parameters: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Apply static or interpolated point channels to a parsed path.

        AUD-7210: channel values can originate from keyframe interpolation
        or programmatic writes, so each ``float()`` is guarded — a malformed
        value keeps the JSON coordinate instead of raising out of
        ``_do_evaluate`` (which the evaluator would turn into an empty
        output and a single warning).
        """
        result = [
            {
                key: list(value) if isinstance(value, (list, tuple)) else value
                for key, value in point.items()
            }
            for point in points
        ]
        for index, point in enumerate(result[:16]):
            names = cls._point_param_names(index)
            for axis, key in (("x", "pos"), ("y", "pos")):
                if names[axis] in parameters:
                    try:
                        point[key][0 if axis == "x" else 1] = float(
                            parameters[names[axis]])
                    except (TypeError, ValueError, IndexError):
                        logger.debug(
                            "RotoShape channel %s has non-numeric value %r; "
                            "keeping JSON coordinate",
                            names[axis], parameters[names[axis]])
            for channel, key in (("c1_x", "c1"), ("c1_y", "c1"),
                                 ("c2_x", "c2"), ("c2_y", "c2")):
                if channel in names and names[channel] in parameters:
                    try:
                        point[key][0 if channel.endswith("_x") else 1] = float(
                            parameters[names[channel]])
                    except (TypeError, ValueError, IndexError):
                        logger.debug(
                            "RotoShape channel %s has non-numeric value %r; "
                            "keeping JSON coordinate",
                            names[channel], parameters[names[channel]])
            if names["feather"] in parameters:
                try:
                    point["feather"] = max(
                        0.0, min(0.5, float(parameters[names["feather"]])))
                except (TypeError, ValueError):
                    logger.debug(
                        "RotoShape channel %s has non-numeric value %r; "
                        "keeping JSON feather",
                        names["feather"], parameters[names["feather"]])
        return result

    def _validate_point_parameter(self, key: str,
                                  value: Any) -> Optional[float]:
        """Validate a ``roto_point_*`` write before it is committed.

        AUD-7210: returns the normalised numeric value, or ``None`` when the
        write must be rejected.  A rejected write never reaches
        ``parameters`` — the old commit-first order had ``super()`` bump the
        revision and record the value for undo, then a silent ``except``
        dropped the JSON sync, leaving the two representations diverged (and
        the bad value re-raising inside ``_do_evaluate``).
        """
        tokens = key.split("_")
        if len(tokens) < 4:
            return None
        try:
            index = int(tokens[2])
        except (TypeError, ValueError):
            return None
        channel = "_".join(tokens[3:])
        if channel not in ("x", "y", "c1_x", "c1_y", "c2_x", "c2_y",
                           "feather"):
            return None
        points = self.parse_points(self.parameters.get("points", "[]"))
        if not (0 <= index < len(points)):
            return None
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        if channel == "feather":
            number = max(0.0, min(0.5, number))
        return number

    def set_parameter(self, key: str, value: Any) -> None:
        """Keep JSON and numeric point channels synchronized for static edits."""
        if key.startswith("roto_point_"):
            validated = self._validate_point_parameter(key, value)
            if validated is None:
                logger.warning(
                    "RotoShapeNode %s: rejected invalid point parameter "
                    "write %s=%r (JSON and numeric channels stay in sync)",
                    self.id, key, value)
                return
            value = validated
        super().set_parameter(key, value)
        if key == "points":
            self._sync_point_parameters(self.parse_points(value))
        elif key.startswith("roto_point_"):
            try:
                points = self.parse_points(self.parameters.get("points", "[]"))
                tokens = key.split("_")
                index = int(tokens[2])
                channel = "_".join(tokens[3:])
                if 0 <= index < len(points):
                    point = points[index]
                    changed = False
                    if channel == "x":
                        changed = point["pos"][0] != float(value)
                        point["pos"][0] = float(value)
                    elif channel == "y":
                        changed = point["pos"][1] != float(value)
                        point["pos"][1] = float(value)
                    elif channel in ("c1_x", "c1_y", "c2_x", "c2_y"):
                        tangent = point[channel[:2]]
                        axis = 0 if channel.endswith("_x") else 1
                        changed = tangent[axis] != float(value)
                        tangent[axis] = float(value)
                    elif channel == "feather":
                        feather = max(0.0, min(0.5, float(value)))
                        changed = point.get("feather") != feather
                        point["feather"] = feather
                    # During deserialization the generated channels are
                    # restored to the same values already present in points.
                    # Avoid re-encoding the JSON in that no-op case so older
                    # projects retain their exact serialized representation.
                    if changed:
                        self.parameters["points"] = json.dumps(
                            points, separators=(",", ":"))
            except (TypeError, ValueError, IndexError, KeyError) as exc:
                # AUD-7210: no longer a silent pass — the numeric channel was
                # committed above, so a sync failure MUST be visible or the
                # two representations diverge without a trace.
                logger.warning(
                    "RotoShapeNode %s: point channel sync failed for %s: %s",
                    self.id, key, exc)

    @staticmethod
    def parse_points(value: Any) -> List[Dict[str, Any]]:
        """Parse persisted point JSON, tolerating old list-of-pairs data."""
        try:
            raw = json.loads(value) if isinstance(value, str) else value
        except (TypeError, ValueError, json.JSONDecodeError):
            raw = []
        points: List[Dict[str, Any]] = []
        for item in raw if isinstance(raw, list) else []:
            explicit_feather: Optional[float] = None
            if isinstance(item, dict):
                pos = item.get("pos", item.get("p", ()))
                c1 = item.get("c1", pos)
                c2 = item.get("c2", pos)
                if "feather" in item:
                    try:
                        explicit_feather = max(
                            0.0, min(0.5, float(item["feather"])))
                    except (TypeError, ValueError):
                        explicit_feather = None
            else:
                pos = item
                c1 = pos
                c2 = pos
            try:
                p = [float(pos[0]), float(pos[1])]
                h1 = [float(c1[0]), float(c1[1])]
                h2 = [float(c2[0]), float(c2[1])]
            except (TypeError, IndexError, ValueError):
                continue
            point: Dict[str, Any] = {"pos": p, "c1": h1, "c2": h2}
            if explicit_feather is not None:
                point["feather"] = explicit_feather
            points.append(point)
        return points

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
        points = self.apply_point_parameter_overrides(
            self.parse_points(self.parameters.get("points", "")),
            self.parameters)
        return {
            "result": TextureResult(
                texture_id=-1, width=width, height=height,
                alpha_premultiplied=has_image, color_space="linear"),
            "matte": TextureResult(
                texture_id=-1, width=width, height=height,
                alpha_premultiplied=False, color_space="linear"),
            "_effect_type": "roto",
            "_effect_input": image_input if has_image else None,
            "_effect_params": {
                "shape_type": self.parameters.get("shape_type", "bezier"),
                "points": points,
                "center_x": float(self.parameters.get("center_x", 0.5)),
                "center_y": float(self.parameters.get("center_y", 0.5)),
                "width": float(self.parameters.get("width", 0.5)),
                "height": float(self.parameters.get("height", 0.5)),
                "rotation": float(self.parameters.get("rotation", 0.0)),
                "feather": float(self.parameters.get("feather", 0.0)),
                "opacity": float(self.parameters.get("opacity", 1.0)),
                "invert": bool(self.parameters.get("invert", False)),
                "output_mode": self.parameters.get("output_mode", "result"),
                "output_width": width,
                "output_height": height,
            },
        }
