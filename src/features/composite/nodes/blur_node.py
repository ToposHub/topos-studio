"""
Blur Node - Multi-type GPU blur effect.

Supports five blur types, all GPU-accelerated for smooth real-time performance:

  * box       — separable uniform box blur (default)
  * gaussian  — separable Gaussian blur
  * bilateral — edge-preserving bilateral filter (single pass)
  * motion    — motion blur from an angle or an optional velocity image
  * bokeh     — lens defocus with aperture blades / anamorphic / highlight gain
                (single pass)

Box and Gaussian are separable two-pass filters and expose independent X/Y
radii (anisotropic — can produce horizontal streaks when Y is collapsed).
Bilateral / Motion / Bokeh are single-pass and use a single radius.

Every type supports per-channel masking (R/G/B/A independent toggles) and a
``mix`` slider that blends the blurred result with the original. The legacy
``directional`` type is accepted on load and transparently mapped to ``motion``
for backward compatibility with older projects.
"""

from typing import Dict, Optional, Any, Set
from .base_node import Node, Port, TextureResult, ParamDef

if __name__ != "__main__":
    from src.features.composite.core.format import BBox


# Blur kernel types — order defines the combo dropdown order.
# "box" is first and therefore the default.
BLUR_TYPES = ["box", "gaussian", "bilateral", "motion", "bokeh"]

# Edge policies
EDGE_POLICIES = ["clamp", "wrap", "mirror"]

# Quality levels (drive sample count for single-pass shaders).
QUALITY_LEVELS = ["fast", "normal", "high"]

# Bokeh aperture blade options. "circle" maps to 0 in the shader; numeric
# strings map to their int value. Kept as strings so the combo stays stable.
BOKEH_BLADES = ["circle", "5", "6", "8"]

# Motion vector sources. ``angle`` preserves the legacy directional blur
# behaviour; ``velocity`` consumes signed, normalized UV displacement from the
# optional ``velocity`` image input (one frame at shutter=1.0).
MOTION_SOURCES = ["angle", "velocity"]

# AUD-64013: safety caps for externally-supplied (unvalidated) radius
# values.  ParamDef max_val only constrains the UI — ``from_dict`` /
# hand-edited project files can carry arbitrary values, and both
# ``compute_input_roi`` and ``_tile_padding`` expand by the raw radius
# (a 1e9 radius would blow up the scheduler's ROI / tile geometry).
# Values mirror PARAM_DEFS: radius_x/radius_y max 200, scalar radius max 100.
_RADIUS_XY_MAX = 200.0
_RADIUS_MAX = 100.0


class BlurNode(Node):
    """Multi-type GPU blur with per-channel masking."""

    node_type = "blur"
    node_title = "Blur"
    i18n_title_key = "nodes.blur.title"
    header_color = "#5a8a6a"  # Muted green

    # Metadata
    SEARCH_ALIASES = ["blur", "模糊", "虚化", "gaussian", "柔化", "box",
                      "bilateral", "motion", "bokeh", "散景", "双边", "运动模糊"]
    DESCRIPTION = "多类型模糊（Box/Gaussian/Bilateral/Motion/Bokeh），支持单通道控制"
    TAGS = ["filter", "image"]

    PARAM_DEFS = [
        # ── Type ──
        ParamDef("blur_type", "option", "box", labels=BLUR_TYPES, group="Blur"),

        # ── Radius (separable types use X/Y, single-pass types use scalar) ──
        # linked 标记让编辑页 effect 面板把 X/Y 渲染成带 ⛓ 链锁的一行
        # （默认锁住、1:1 等值联动）。合成页走自己的 _build_blur_ui，不读此字段。
        ParamDef("radius_x", "float", 5.0, min_val=0.0, max_val=200.0,
                 step=0.5, group="Blur", linked="radius_y"),
        ParamDef("radius_y", "float", 5.0, min_val=0.0, max_val=200.0,
                 step=0.5, group="Blur", linked="radius_x"),
        ParamDef("radius", "float", 8.0, min_val=0.0, max_val=100.0,
                 step=0.5, group="Blur"),

        # ── Motion ──
        ParamDef("angle", "float", 0.0, min_val=0.0, max_val=360.0,
                 step=1.0, group="Blur"),
        ParamDef("motion_source", "option", "angle", labels=MOTION_SOURCES,
                 group="Motion", animated=True),
        ParamDef("shutter", "float", 1.0, min_val=0.0, max_val=2.0,
                 step=0.01, group="Motion", animated=True),

        # ── Bilateral ──
        ParamDef("edge_preserve", "float", 0.5, min_val=0.0, max_val=1.0,
                 step=0.01, group="Blur"),

        # ── Bokeh ──
        ParamDef("aperture_blades", "option", "circle", labels=BOKEH_BLADES,
                 group="Blur"),
        ParamDef("anamorphic", "float", 0.0, min_val=-1.0, max_val=1.0,
                 step=0.01, group="Blur",
                 expression_hint="变形宽荧幕拉伸: 0=圆形, +1=水平拉伸 "
                                 "(电影变形散景), -1=垂直拉伸"),
        ParamDef("highlight_gain", "float", 0.0, min_val=0.0, max_val=4.0,
                 step=0.01, group="Blur"),

        # ── Common ──
        ParamDef("edge_policy", "option", "clamp", labels=EDGE_POLICIES,
                 group="Blur"),
        ParamDef("quality", "option", "normal", labels=QUALITY_LEVELS,
                 group="Blur"),

        # ── Channels (independent toggles, default RGB on / A off) ──
        ParamDef("channel_r", "bool", True, group="Channels"),
        ParamDef("channel_g", "bool", True, group="Channels"),
        ParamDef("channel_b", "bool", True, group="Channels"),
        ParamDef("channel_a", "bool", False, group="Channels"),

        # ── Mix ──
        ParamDef("mix", "float", 1.0, min_val=0.0, max_val=1.0,
                 step=0.01, group="Channels"),

        # Legacy: kept for backward compatibility of saved graphs. Hidden from
        # the new UI (channel_* / mix replace it). Still consumed by the
        # evaluator fallback path for old nodes.
        ParamDef("process_alpha", "bool", True, group="Blur", hidden=True),
    ]

    def __init__(self, node_id: Optional[str] = None) -> None:
        super().__init__(node_id)
        self.inputs = [
            Port("image", "input", "image"),
            # Optional RG velocity image. The graph only supplies this port
            # when connected, so old one-input Blur graphs remain unchanged.
            Port("velocity", "input", "image"),
        ]
        self.outputs = [
            Port("result", "output", "image"),
        ]
        self.parameters = {
            "blur_type": "box",
            "radius_x": 5.0,
            "radius_y": 5.0,
            "radius": 8.0,
            "angle": 0.0,
            "motion_source": "angle",
            "shutter": 1.0,
            "edge_preserve": 0.5,
            "aperture_blades": "circle",
            "anamorphic": 0.0,
            "highlight_gain": 0.0,
            "edge_policy": "clamp",
            "quality": "normal",
            "channel_r": True,
            "channel_g": True,
            "channel_b": True,
            "channel_a": False,
            "mix": 1.0,
            "process_alpha": True,
        }

    # ───────────────────────────────────────────────────────────────────────
    # Parameter visibility (drives the UI rebuild on blur_type change)
    # ───────────────────────────────────────────────────────────────────────
    @staticmethod
    def blur_type_param_visibility(blur_type: str,
                                   motion_source: str = "angle") -> Set[str]:
        """Return the set of parameter names that should be hidden for the
        given ``blur_type``.

        The UI calls this whenever the type combo changes and rebuilds the
        parameter rows. Parameters that are irrelevant to the active type are
        hidden; relevant ones are shown.
        """
        # process_alpha is legacy/hidden for every type.
        always_hidden: Set[str] = {"process_alpha"}

        # Radius model per type.
        separable_types = {"box", "gaussian"}    # use radius_x / radius_y
        single_types = {"bilateral", "motion", "bokeh"}  # use scalar radius
        non_motion_hidden = {"motion_source", "shutter"}

        if blur_type in separable_types:
            hidden = always_hidden | non_motion_hidden | {"radius", "angle", "edge_preserve",
                                      "aperture_blades", "anamorphic",
                                      "highlight_gain"}
        elif blur_type == "bilateral":
            hidden = always_hidden | non_motion_hidden | {"radius_x", "radius_y", "angle",
                                      "aperture_blades", "anamorphic",
                                      "highlight_gain"}
        elif blur_type == "motion":
            hidden = always_hidden | {"radius_x", "radius_y", "edge_preserve",
                                      "aperture_blades", "anamorphic",
                                      "highlight_gain"}
            if motion_source == "velocity":
                hidden.add("angle")
        elif blur_type == "bokeh":
            hidden = always_hidden | non_motion_hidden | {"radius_x", "radius_y", "angle",
                                      "edge_preserve"}
        else:
            hidden = always_hidden | non_motion_hidden

        # single_types that are NOT the active type hide the scalar radius too,
        # except separable types already hide it above.
        if blur_type not in single_types and blur_type in separable_types:
            # radius already in hidden set for separable types.
            pass

        return hidden

    # ───────────────────────────────────────────────────────────────────────
    # Radius helpers
    # ───────────────────────────────────────────────────────────────────────
    @staticmethod
    def _clamped_radius(raw: Any, default: float, max_val: float) -> float:
        """Radius as float clamped to ``[0, max_val]`` (AUD-64013).

        Non-numeric / NaN values from unvalidated project data fall back to
        ``default`` instead of raising TypeError inside ROI computation or
        ``_do_evaluate`` (which the evaluator's guard would blank into an
        empty render).
        """
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return default
        if value != value:  # NaN
            return default
        return min(max_val, max(0.0, value))

    def _get_effective_radius(self, params: Dict[str, Any]) -> tuple:
        """Get effective radius values for ROI/tile-padding computation.

        Separable types use (radius_x, radius_y). Single-pass types use the
        scalar ``radius`` for both axes. Legacy ``radius`` is honoured when
        radius_x/y are absent (old saved graphs).

        AUD-64013: radii are clamped here — the single choke point for
        ROI/tile padding — so externally-supplied out-of-range values can
        no longer expand the padding by the raw amount.
        """
        blur_type = params.get("blur_type", "box")
        if blur_type == "directional":
            blur_type = "motion"

        if blur_type in ("box", "gaussian"):
            if "radius" in params and "radius_x" not in params:
                r = self._clamped_radius(params.get("radius", 5.0),
                                         5.0, _RADIUS_MAX)
                return (r, r)
            rx = self._clamped_radius(params.get("radius_x", 5.0),
                                      5.0, _RADIUS_XY_MAX)
            ry = self._clamped_radius(params.get("radius_y", 5.0),
                                      5.0, _RADIUS_XY_MAX)
            return (rx, ry)
        else:
            r = self._clamped_radius(params.get("radius", params.get("radius_x", 8.0)),
                                     8.0, _RADIUS_MAX)
            return (r, r)

    def compute_output_bbox(self, input_bboxes: Dict[str, "BBox"],
                            params: Dict[str, Any]) -> Optional["BBox"]:
        """Fixed-canvas bbox policy.

        Blur renders into a texture of the input size without expanding the
        canvas, so the bbox stays aligned with the input bbox. (The blur
        feather that visually extends beyond the original content edges is
        captured inside the fixed canvas as semi-transparent pixels, not by
        moving the bbox.) Expanding the bbox would desynchronize it from the
        texture and break downstream sampling.
        """
        if not input_bboxes:
            return None
        input_bbox = input_bboxes.get("image")
        if input_bbox is None or input_bbox.is_empty:
            return None
        return input_bbox

    def compute_input_roi(self, output_roi: "BBox",
                          params: Dict[str, Any],
                          input_bboxes: Optional[Dict[str, "BBox"]] = None) -> Dict[str, "BBox"]:
        """To blur output_roi, we need input expanded by the maximum radius."""
        radius_x, radius_y = self._get_effective_radius(params)
        max_radius = int(max(radius_x, radius_y))
        expanded_roi = output_roi.expanded(max_radius)
        result = {"image": expanded_roi}
        # Velocity is sampled at the output pixel, not along the blur kernel,
        # so it does not need the image-kernel padding. Only advertise this
        # ROI when the optional port is actually connected.
        if input_bboxes and "velocity" in input_bboxes:
            result["velocity"] = output_roi
        return result

    def _tile_padding(self, params: Dict[str, Any]) -> int:
        """Declare tile padding requirement for seamless boundaries.

        Blur requires the maximum radius on all sides to avoid artifacts
        at tile edges. This is critical for large blur radii where
        neighboring pixels contribute significantly.

        Args:
            params: Node parameters dictionary.

        Returns:
            Padding in pixels (maximum of radius_x and radius_y).
        """
        radius_x, radius_y = self._get_effective_radius(params)
        return int(max(radius_x, radius_y))

    def requires_roi_expansion(self, params: Dict[str, Any]) -> int:
        """Legacy API - delegates to _tile_padding()."""
        return self._tile_padding(params)

    # ───────────────────────────────────────────────────────────────────────
    # Evaluation
    # ───────────────────────────────────────────────────────────────────────
    def _do_evaluate(self, inputs: Dict[str, TextureResult], ctx: Any, fb_manager: Any,
                      resource_pool: Any = None, render_mode: str = "preview") -> Dict[str, TextureResult]:
        image = inputs.get("image")
        if not image or not image.is_valid:
            return {}

        blur_type = self.parameters.get("blur_type", "box")
        # Backward compat: legacy "directional" -> "motion".
        if blur_type == "directional":
            blur_type = "motion"

        # AUD-64013: clamp to the ParamDef maxima (not just >= 0) so
        # unvalidated external values cannot reach the shader / ROI math.
        radius_x = self._clamped_radius(self.parameters.get("radius_x", 5.0),
                                        5.0, _RADIUS_XY_MAX)
        radius_y = self._clamped_radius(self.parameters.get("radius_y", 5.0),
                                        5.0, _RADIUS_XY_MAX)
        radius = self._clamped_radius(self.parameters.get("radius", 8.0),
                                      8.0, _RADIUS_MAX)
        angle = float(self.parameters.get("angle", 0.0))
        motion_source = self.parameters.get("motion_source", "angle")
        if motion_source not in MOTION_SOURCES:
            motion_source = "angle"
        shutter = min(2.0, max(0.0, float(
            self.parameters.get("shutter", 1.0))))
        edge_preserve = float(self.parameters.get("edge_preserve", 0.5))
        aperture_blades = self.parameters.get("aperture_blades", "circle")
        anamorphic = float(self.parameters.get("anamorphic", 0.0))
        highlight_gain = float(self.parameters.get("highlight_gain", 0.0))
        edge_policy = self.parameters.get("edge_policy", "clamp")
        quality = self.parameters.get("quality", "normal")
        mix = float(self.parameters.get("mix", 1.0))

        # Channel mask packing (RGB default on, A default off).
        channel_mask = [
            1.0 if self.parameters.get("channel_r", True) else 0.0,
            1.0 if self.parameters.get("channel_g", True) else 0.0,
            1.0 if self.parameters.get("channel_b", True) else 0.0,
            1.0 if self.parameters.get("channel_a", False) else 0.0,
        ]

        velocity = inputs.get("velocity")
        has_velocity = bool(velocity and velocity.is_valid)
        velocity_mode = blur_type == "motion" and motion_source == "velocity"

        # Skip if the active blur distance is negligible. Velocity mode uses
        # the connected vector instead of radius; with no vector it falls back
        # to the legacy angle path, which still needs a radius.
        if blur_type in ("box", "gaussian"):
            eff_radius = max(radius_x, radius_y)
        else:
            eff_radius = radius
        if shutter < 0.0001 or (eff_radius < 0.1 and not (velocity_mode and has_velocity)):
            return {"result": image}

        # Preview mode: reduce radius for faster interactive response.
        if render_mode == "preview":
            if eff_radius > 15.0:
                scale = max(5.0, eff_radius * 0.3) / eff_radius
                radius_x *= scale
                radius_y *= scale
                radius *= scale

        # Quality-based sample count adjustment (for renderer guidance).
        quality_samples = {"fast": 8, "normal": 16, "high": 32}
        sample_count = quality_samples.get(quality, 16)

        return {
            "result": TextureResult(
                texture_id=-1,  # Will be replaced by actual GPU result
                width=image.width,
                height=image.height,
                alpha_premultiplied=image.alpha_premultiplied,
                color_space=image.color_space,
            ),
            "_effect_type": "blur",
            "_effect_input": image,
            "_effect_params": {
                "blur_type": blur_type,
                "radius_x": radius_x,
                "radius_y": radius_y,
                "radius": radius,
                "angle": angle,
                "motion_source": motion_source,
                "shutter": shutter,
                "edge_preserve": edge_preserve,
                "aperture_blades": aperture_blades,
                "anamorphic": anamorphic,
                "highlight_gain": highlight_gain,
                "edge_policy": edge_policy,
                "sample_count": sample_count,
                "quality": quality,
                "channel_r": self.parameters.get("channel_r", True),
                "channel_g": self.parameters.get("channel_g", True),
                "channel_b": self.parameters.get("channel_b", True),
                "channel_a": self.parameters.get("channel_a", False),
                "channel_mask": channel_mask,
                "mix": mix,
                # Legacy param kept for old evaluator fallback paths.
                "process_alpha": bool(channel_mask[3] > 0.5),
            },
            **({"_effect_velocity": velocity} if velocity_mode and has_velocity else {}),
        }
