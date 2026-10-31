"""Optional, dependency-free advanced SVG capabilities."""

from __future__ import annotations

from dataclasses import dataclass
import csv
from html import escape
import io
import json
import math
import re
from typing import Any, Iterable, Mapping


def resolve_localized_text(value: Any, locale: str = "en-US", fallback_locale: str = "en-US") -> str:
    if not isinstance(value, Mapping):
        return str(value)
    locales = value.get("locales", value)
    if not isinstance(locales, Mapping):
        return str(value)
    for key in (locale, locale.replace("_", "-"), locale.split("-")[0], fallback_locale, "en-US", "en", "default"):
        if key in locales:
            return str(locales[key])
    return next((str(item) for item in locales.values()), "")


def formula_to_plain_text(value: Any) -> str:
    """Offline-safe formula fallback for common LaTeX-like input."""
    text = resolve_localized_text(value)
    text = re.sub(r"\\frac\{([^{}]*)\}\{([^{}]*)\}", r"(\1)/\2", text)
    return text.replace("\\times", "×").replace("\\cdot", "·")


@dataclass(frozen=True)
class ChartData:
    labels: tuple[str, ...]
    values: tuple[float, ...]

    def __post_init__(self) -> None:
        if len(self.labels) != len(self.values) or not self.labels:
            raise ValueError("chart labels and values must have equal non-zero length")
        if not all(math.isfinite(value) for value in self.values):
            raise ValueError("chart values must be finite")

    @classmethod
    def from_records(cls, records: Iterable[Mapping[str, Any]], *, label_key: str = "label", value_key: str = "value") -> "ChartData":
        labels: list[str] = []
        values: list[float] = []
        for record in records:
            labels.append(str(record.get(label_key, "")))
            values.append(float(record.get(value_key, 0.0)))
        return cls(tuple(labels), tuple(values))

    @classmethod
    def from_json(cls, value: str | bytes) -> "ChartData":
        parsed = json.loads(value)
        if isinstance(parsed, Mapping):
            return cls(tuple(str(item) for item in parsed.get("labels", [])), tuple(float(item) for item in parsed.get("values", [])))
        if isinstance(parsed, list):
            return cls.from_records(parsed)
        raise ValueError("chart JSON must be an object or records array")

    @classmethod
    def from_csv(cls, value: str, *, label_key: str = "label", value_key: str = "value") -> "ChartData":
        return cls.from_records(csv.DictReader(io.StringIO(value)), label_key=label_key, value_key=value_key)


def render_chart_svg(data: ChartData, width: float, height: float, *, chart_type: str = "bar", color: str = "#3b82f6") -> str:
    maximum = max(max(data.values), 0.0) or 1.0
    count = len(data.values)
    fragments: list[str] = []
    if chart_type == "line":
        points = " ".join(f"{((index + 0.5) * width / count):.6f},{(height - value / maximum * height):.6f}" for index, value in enumerate(data.values))
        fragments.append(f'<polyline points="{points}" fill="none" stroke="{escape(color, quote=True)}" stroke-width="2"/>')
    else:
        bar_width = width / count
        for index, value in enumerate(data.values):
            bar_height = max(0.0, value / maximum * height)
            fragments.append(f'<rect x="{index * bar_width:.6f}" y="{height - bar_height:.6f}" width="{bar_width * 0.8:.6f}" height="{bar_height:.6f}" fill="{escape(color, quote=True)}"/>')
    return "".join(fragments)


_DEFAULT_STROKE_WIDTH = 3.0


def _stroke_width(content: Mapping[str, Any], default: float = _DEFAULT_STROKE_WIDTH) -> float:
    """Coerce the ``stroke_width`` scene attribute with a safe default.

    AUD-29801: scene content is untrusted data; a non-numeric ``stroke_width``
    (``"thick"``, ``None``, a list…) used to leak a raw ``ValueError``/
    ``TypeError`` out of :func:`render_draw_svg` into the render thread.
    Degrade to the default instead; non-finite results are equally useless as
    an SVG attribute and take the same path.
    """
    try:
        value = float(content.get("stroke_width", default))
    except (TypeError, ValueError):
        return default
    return value if math.isfinite(value) else default


def render_draw_svg(content: Mapping[str, Any], width: float, height: float, *, color: str = "#f59e0b") -> str:
    raw_points = content.get("points", [])
    if not isinstance(raw_points, list):
        return ""
    points: list[str] = []
    for point in raw_points:
        if isinstance(point, (list, tuple)) and len(point) >= 2:
            try:
                points.append(f"{float(point[0]):.6f},{float(point[1]):.6f}")
            except (TypeError, ValueError):
                continue
    if len(points) < 2:
        return ""
    return f'<polyline points="{" ".join(points)}" fill="none" stroke="{escape(color, quote=True)}" stroke-width="{_stroke_width(content):.6f}" stroke-linecap="round" stroke-linejoin="round"/>'


@dataclass(frozen=True)
class AdvancedBackendCapabilities:
    canvas: bool = True
    webgl: bool = False
    moderngl: bool = False
    fallback_backend: str = "svg-cpu"


class WebGLPreviewRenderer:
    """Optional WebGL capability surface with deterministic HTML fallback."""

    def __init__(self, *, webgl_available: bool = False) -> None:
        self.capabilities = AdvancedBackendCapabilities(webgl=webgl_available)
        self.fallback_used = not webgl_available

    def backend_for_preview(self) -> str:
        return "webgl" if self.capabilities.webgl else self.capabilities.fallback_backend


class ModernGLEffectRegistry:
    """Descriptor-only registry; actual GL effects remain optional host nodes."""

    def __init__(self) -> None:
        self._effects: dict[str, Mapping[str, Any]] = {}

    def register(self, effect_id: str, *, parameters: Iterable[str] = ()) -> None:
        self._effects[effect_id] = {"effect_id": effect_id, "parameters": tuple(parameters), "backend": "moderngl-optional"}

    def list_effects(self) -> tuple[Mapping[str, Any], ...]:
        return tuple(self._effects[key] for key in sorted(self._effects))
