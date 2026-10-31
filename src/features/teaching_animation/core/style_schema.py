"""SG-06: versioned style schema — fill/stroke/gradient/shadow/blend/matte.

Element styles today are a loose mapping; this gives them an explicit,
versioned contract so the same style renders without black-edge or colour-
conversion surprises across SVG / GPU / Composite backends.  Key pieces:

* explicit ``BlendMode``;
* an ``AlphaCompositing`` flag declaring whether the layer's pixels are
  straight or premultiplied (the plan §SG-08 contract: no double conversion);
* structured fill / stroke / gradient / shadow that resolve design tokens.

Styles remain backward-compatible: the loose ``style`` mapping is preserved and
``StyleSchema.from_style`` parses the known keys, so existing scenes load
unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping

from .design_tokens import DesignTokenSet, resolve_style


class BlendMode(str, Enum):
    NORMAL = "normal"
    MULTIPLY = "multiply"
    SCREEN = "screen"
    OVERLAY = "overlay"
    DARKEN = "darken"
    LIGHTEN = "lighten"
    COLOR_DODGE = "color_dodge"
    COLOR_BURN = "color_burn"
    HARD_LIGHT = "hard_light"
    SOFT_LIGHT = "soft_light"
    DIFFERENCE = "difference"
    EXCLUSION = "exclusion"


class AlphaCompositing(str, Enum):
    """How a layer's alpha is encoded when it reaches the compositor."""

    STRAIGHT = "straight"
    PREMULTIPLIED = "premultiplied"


@dataclass(frozen=True)
class GradientStop:
    position: float          # 0..1
    color: str               # hex or token

    def __post_init__(self) -> None:
        if not 0.0 <= self.position <= 1.0:
            raise ValueError("gradient stop position must be in [0,1]")


@dataclass(frozen=True)
class Gradient:
    kind: str = "linear"     # linear | radial
    angle: float = 0.0       # degrees, for linear
    stops: tuple[GradientStop, ...] = ()

    def __post_init__(self) -> None:
        if self.kind not in {"linear", "radial"}:
            raise ValueError(f"unknown gradient kind: {self.kind}")
        if len(self.stops) < 2:
            raise ValueError("gradient needs at least 2 stops")

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "angle": self.angle,
                "stops": [{"position": s.position, "color": s.color} for s in self.stops]}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Gradient":
        stops = tuple(GradientStop(position=float(s.get("position", 0.0)), color=str(s.get("color", "#000")))
                      for s in data.get("stops", []) if isinstance(s, Mapping))
        return cls(kind=str(data.get("kind", "linear")), angle=float(data.get("angle", 0.0)), stops=stops)


@dataclass(frozen=True)
class Shadow:
    color: str = "#000000"
    blur: float = 8.0
    offset_x: float = 0.0
    offset_y: float = 4.0
    opacity: float = 0.5

    def __post_init__(self) -> None:
        if self.blur < 0.0:
            raise ValueError("shadow blur must be non-negative")
        if not 0.0 <= self.opacity <= 1.0:
            raise ValueError("shadow opacity must be in [0,1]")


@dataclass(frozen=True)
class Stroke:
    color: str = "#000000"
    width: float = 0.0
    opacity: float = 1.0

    def __post_init__(self) -> None:
        if self.width < 0.0:
            raise ValueError("stroke width must be non-negative")
        if not 0.0 <= self.opacity <= 1.0:
            raise ValueError("stroke opacity must be in [0,1]")


@dataclass(frozen=True)
class StyleSchema:
    """Versioned, structured style for a layer."""

    schema_version: int = 1
    fill: str | None = None
    fill_opacity: float = 1.0
    stroke: Stroke | None = None
    gradient: Gradient | None = None
    shadow: Shadow | None = None
    opacity: float = 1.0
    blend_mode: BlendMode = BlendMode.NORMAL
    alpha: AlphaCompositing = AlphaCompositing.STRAIGHT
    matte_target: str | None = None          # element_id supplying a track matte
    extras: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not 0.0 <= self.opacity <= 1.0:
            raise ValueError("opacity must be in [0,1]")
        if not 0.0 <= self.fill_opacity <= 1.0:
            raise ValueError("fill_opacity must be in [0,1]")

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "schema_version": self.schema_version,
            "opacity": self.opacity, "blend_mode": self.blend_mode.value,
            "alpha": self.alpha.value, "fill_opacity": self.fill_opacity,
        }
        if self.fill is not None:
            data["fill"] = self.fill
        if self.stroke is not None:
            data["stroke"] = {"color": self.stroke.color, "width": self.stroke.width, "opacity": self.stroke.opacity}
        if self.gradient is not None:
            data["gradient"] = self.gradient.to_dict()
        if self.shadow is not None:
            data["shadow"] = {"color": self.shadow.color, "blur": self.shadow.blur,
                              "offset_x": self.shadow.offset_x, "offset_y": self.shadow.offset_y,
                              "opacity": self.shadow.opacity}
        if self.matte_target is not None:
            data["matte_target"] = self.matte_target
        if self.extras:
            data["extras"] = dict(self.extras)
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "StyleSchema":
        def _blend(v: Any) -> BlendMode:
            try:
                return BlendMode(v)
            except ValueError:
                return BlendMode.NORMAL

        def _alpha(v: Any) -> AlphaCompositing:
            try:
                return AlphaCompositing(v)
            except ValueError:
                return AlphaCompositing.STRAIGHT

        stroke_raw = data.get("stroke")
        gradient_raw = data.get("gradient")
        shadow_raw = data.get("shadow")
        return cls(
            schema_version=int(data.get("schema_version", 1)),
            fill=data.get("fill"),
            fill_opacity=float(data.get("fill_opacity", 1.0)),
            stroke=Stroke(color=str(stroke_raw.get("color", "#000")), width=float(stroke_raw.get("width", 0.0)),
                          opacity=float(stroke_raw.get("opacity", 1.0))) if isinstance(stroke_raw, Mapping) else None,
            gradient=Gradient.from_dict(gradient_raw) if isinstance(gradient_raw, Mapping) else None,
            shadow=Shadow(color=str(shadow_raw.get("color", "#000")), blur=float(shadow_raw.get("blur", 8.0)),
                          offset_x=float(shadow_raw.get("offset_x", 0.0)), offset_y=float(shadow_raw.get("offset_y", 4.0)),
                          opacity=float(shadow_raw.get("opacity", 0.5))) if isinstance(shadow_raw, Mapping) else None,
            opacity=float(data.get("opacity", 1.0)),
            blend_mode=_blend(data.get("blend_mode", "normal")),
            alpha=_alpha(data.get("alpha", "straight")),
            matte_target=data.get("matte_target"),
            extras=dict(data.get("extras", {})),
        )

    @classmethod
    def from_style(cls, style: Mapping[str, Any]) -> "StyleSchema":
        """Parse the legacy loose ``style`` mapping into a structured schema."""
        stroke = None
        if "stroke" in style or "stroke_width" in style:
            stroke = Stroke(color=str(style.get("stroke", "#000000")),
                            width=float(style.get("stroke_width", 0.0)))
        shadow = None
        if "shadow" in style or "box_shadow" in style:
            shadow = Shadow(color=str(style.get("shadow_color", "#000000")),
                            blur=float(style.get("shadow_blur", 8.0)),
                            opacity=float(style.get("shadow_opacity", 0.5)))
        return cls(
            fill=style.get("fill", style.get("color")),
            fill_opacity=float(style.get("fill_opacity", style.get("opacity", 1.0))),
            stroke=stroke, shadow=shadow,
            opacity=float(style.get("opacity", 1.0)),
        )

    def resolved(self, tokens: DesignTokenSet) -> dict[str, Any]:
        """Return a flat resolved style mapping (tokens replaced) for renderers."""
        flat = {"opacity": self.opacity, "blend_mode": self.blend_mode.value, "alpha": self.alpha.value}
        if self.fill is not None:
            flat["fill"] = tokens.resolve_value(self.fill)
            flat["fill_opacity"] = self.fill_opacity
        if self.stroke is not None:
            flat["stroke"] = tokens.resolve_value(self.stroke.color)
            flat["stroke_width"] = self.stroke.width
            flat["stroke_opacity"] = self.stroke.opacity
        if self.shadow is not None:
            flat["shadow"] = tokens.resolve_value(self.shadow.color)
            flat["shadow_blur"] = self.shadow.blur
            flat["shadow_offset_x"] = self.shadow.offset_x
            flat["shadow_offset_y"] = self.shadow.offset_y
            flat["shadow_opacity"] = self.shadow.opacity
        if self.gradient is not None:
            flat["gradient"] = self.gradient.to_dict()
        if self.matte_target is not None:
            flat["matte_target"] = self.matte_target
        # merge extras (resolved too) for forward-compat properties renderers may read.
        flat.update(resolve_style(self.extras, tokens))
        return flat


__all__ = [
    "AlphaCompositing", "BlendMode", "Gradient", "GradientStop", "Shadow", "Stroke", "StyleSchema",
]
