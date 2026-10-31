"""TM-05: design tokens — palette, type scale, spacing, motion + brand theming.

Tokens are versioned, named values that styles reference symbolically (e.g.
``{"color": "$palette.primary"}``).  Resolving tokens replaces references with
concrete values, so changing one brand colour updates every reference at once —
without touching manually-locked elements.

Brand theming is a token overlay: a ``BrandTheme`` carries palette/type/motion
overrides with a priority and a diagnostic for conflicts (e.g. a token the
brand theme expects but the base set does not define).  Locked elements
(``style.__locked__``) are skipped, so brand updates never silently overwrite
hand-tuned content.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping


_TOKEN_REF = "$"

#: Token categories resolvable through ``$category.name`` references
#: (AUD-27415: category lookup is an explicit whitelist, never getattr on an
#: arbitrary attribute name — external token data must not be able to pull
#: instance attributes/methods into resolution).
_TOKEN_CATEGORIES: tuple[str, ...] = (
    "palette", "type_scale", "spacing", "radius", "shadow", "motion",
)


@dataclass(frozen=True)
class DesignTokenSet:
    """A versioned bag of named design tokens grouped by category."""

    palette: Mapping[str, str] = field(default_factory=dict)          # name -> hex colour
    type_scale: Mapping[str, float] = field(default_factory=dict)     # name -> font size
    spacing: Mapping[str, float] = field(default_factory=dict)        # name -> px
    radius: Mapping[str, float] = field(default_factory=dict)
    shadow: Mapping[str, str] = field(default_factory=dict)
    motion: Mapping[str, float] = field(default_factory=dict)         # name -> ms or frames
    safe_area: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)

    def category(self, name: str) -> Mapping[str, Any]:
        if name not in _TOKEN_CATEGORIES:
            return {}
        return getattr(self, name)

    def resolve_value(self, value: Any) -> Any:
        """Resolve a single token reference to its concrete value (recursive).

        AUD-27415: token data comes from scene metadata and brand theme
        packages (external input), so resolution is iterative with a visited
        set — a cyclic reference (``a -> b -> a``) or an overlong chain
        degrades to the unresolved pass-through value instead of a
        RecursionError, and a malformed category never yields a non-mapping
        attribute (``$to_dict.x`` resolves to itself, not a TypeError).
        """
        visited: set[str] = set()
        current = value
        while isinstance(current, str) and current.startswith(_TOKEN_REF):
            if current in visited:
                return current  # cyclic reference — treat as unresolved
            visited.add(current)
            category, _, key = current[1:].partition(".")
            cat = self.category(category) if category else {}
            if key not in cat:
                return current  # unresolved refs pass through unchanged
            current = cat[key]
        return current

    def overlay(self, theme: "BrandTheme") -> "DesignTokenSet":
        """Return a new token set with the brand theme's overrides applied."""
        if theme.priority < 0:
            raise ValueError("brand theme priority must be non-negative")
        # Merge only the categories a theme chooses to override (getattr keeps
        # overlay robust as BrandTheme grows new optional categories).
        return DesignTokenSet(
            palette={**self.palette, **getattr(theme, "palette", {})},
            type_scale={**self.type_scale, **getattr(theme, "type_scale", {})},
            spacing={**self.spacing, **getattr(theme, "spacing", {})},
            radius={**self.radius, **getattr(theme, "radius", {})},
            shadow={**self.shadow, **getattr(theme, "shadow", {})},
            motion={**self.motion, **getattr(theme, "motion", {})},
            safe_area=theme.safe_area if getattr(theme, "safe_area", ()) else self.safe_area,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "palette": dict(self.palette), "type_scale": dict(self.type_scale),
            "spacing": dict(self.spacing), "radius": dict(self.radius),
            "shadow": dict(self.shadow), "motion": dict(self.motion),
            "safe_area": list(self.safe_area),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> "DesignTokenSet":
        data = data or {}

        def _colors(values: Any) -> dict[str, str]:
            return {str(k): str(v) for k, v in dict(values or {}).items()
                    if isinstance(v, str)}

        def _numbers(values: Any) -> dict[str, float]:
            out: dict[str, float] = {}
            for key, value in dict(values or {}).items():
                try:
                    out[str(key)] = float(value)
                except (TypeError, ValueError):
                    continue
            return out

        safe = data.get("safe_area") or (0.0, 0.0, 0.0, 0.0)
        return cls(
            palette=_colors(data.get("palette")),
            type_scale=_numbers(data.get("type_scale")),
            spacing=_numbers(data.get("spacing")),
            radius=_numbers(data.get("radius")),
            shadow=_colors(data.get("shadow")),
            motion=_numbers(data.get("motion")),
            safe_area=tuple(float(v) for v in safe[:4]),
        )


@dataclass(frozen=True)
class BrandTheme:
    """An overlay of brand tokens with priority + provenance."""

    theme_id: str
    palette: Mapping[str, str] = field(default_factory=dict)
    type_scale: Mapping[str, float] = field(default_factory=dict)
    spacing: Mapping[str, float] = field(default_factory=dict)
    motion: Mapping[str, float] = field(default_factory=dict)
    safe_area: tuple[float, float, float, float] = ()
    priority: int = 0
    source: str = ""

    def __post_init__(self) -> None:
        if not self.theme_id:
            raise ValueError("theme_id cannot be empty")


@dataclass(frozen=True)
class TokenConflict:
    theme_id: str
    unresolved: tuple[str, ...]   # token names the theme references but the base lacks


def validate_theme(base: DesignTokenSet, theme: BrandTheme) -> TokenConflict:
    """Report token references in a theme that the base set does not define."""
    unresolved: list[str] = []
    for category_name, overrides in (
        ("palette", theme.palette), ("type_scale", theme.type_scale),
        ("spacing", theme.spacing), ("motion", theme.motion),
    ):
        for key, value in overrides.items():
            if isinstance(value, str) and value.startswith(_TOKEN_REF):
                if base.resolve_value(value) == value:
                    unresolved.append(f"{category_name}.{key} -> {value}")
    return TokenConflict(theme_id=theme.theme_id, unresolved=tuple(unresolved))


def resolve_style(style: Mapping[str, Any], tokens: DesignTokenSet) -> dict[str, Any]:
    """Resolve all token references in a style mapping.

    Elements marked locked (``style["__locked__"]`` is a list of property names,
    or True for the whole style) keep their literal values.
    """
    locked_all = style.get("__locked__") is True
    locked_keys = set(style.get("__locked__", []) or []) if isinstance(style.get("__locked__"), (list, tuple)) else set()
    resolved: dict[str, Any] = {}
    for key, value in style.items():
        if key == "__locked__":
            continue
        if locked_all or key in locked_keys:
            resolved[key] = value
            continue
        resolved[key] = tokens.resolve_value(value)
    return resolved


def default_brand_tokens() -> DesignTokenSet:
    """A neutral default token set used when no brand is configured."""
    return DesignTokenSet(
        palette={
            "primary": "#3b82f6", "primary_fg": "#ffffff",
            "accent": "#f59e0b", "surface": "#0b1220", "surface_fg": "#e5e7eb",
            "muted": "#94a3b8", "danger": "#ef4444",
        },
        type_scale={"display": 48.0, "title": 36.0, "heading": 28.0, "body": 20.0, "caption": 16.0},
        spacing={"xs": 4.0, "sm": 8.0, "md": 16.0, "lg": 24.0, "xl": 40.0},
        radius={"sm": 4.0, "md": 8.0, "lg": 16.0, "pill": 999.0},
        shadow={"sm": "0 1px 2px rgba(0,0,0,0.2)", "md": "0 4px 12px rgba(0,0,0,0.3)"},
        motion={"fast": 8.0, "normal": 16.0, "slow": 28.0},
        safe_area=(40.0, 40.0, 40.0, 40.0),
    )


# --------------------------------------------------------------------------- #
# Scene-level storage (WP-4): tokens and brand themes live in scene.metadata
# under versioned keys so unknown metadata fields always survive saves.
# --------------------------------------------------------------------------- #

_SCENE_TOKENS_KEY = "design_tokens"
_SCENE_THEMES_KEY = "brand_themes"
_TOKENS_SCHEMA_VERSION = 1


def read_scene_tokens(metadata: Mapping[str, Any]) -> DesignTokenSet:
    """Default tokens overlaid with scene tokens, then the active brand theme."""
    base = default_brand_tokens()
    raw = metadata.get(_SCENE_TOKENS_KEY) if isinstance(metadata, Mapping) else None
    if isinstance(raw, Mapping):
        scene_set = DesignTokenSet.from_dict(raw)
        base = DesignTokenSet(
            palette={**base.palette, **scene_set.palette},
            type_scale={**base.type_scale, **scene_set.type_scale},
            spacing={**base.spacing, **scene_set.spacing},
            radius={**base.radius, **scene_set.radius},
            shadow={**base.shadow, **scene_set.shadow},
            motion={**base.motion, **scene_set.motion},
            safe_area=scene_set.safe_area if scene_set.safe_area != (0.0, 0.0, 0.0, 0.0) else base.safe_area,
        )
    theme = read_active_brand_theme(metadata)
    if theme is not None:
        base = base.overlay(theme)
    return base


def write_scene_tokens_into(metadata: Mapping[str, Any], tokens: DesignTokenSet) -> dict[str, Any]:
    """Return a new metadata dict with tokens stored under the versioned key."""
    new = dict(metadata) if isinstance(metadata, Mapping) else {}
    payload = tokens.to_dict()
    payload["schema_version"] = _TOKENS_SCHEMA_VERSION
    new[_SCENE_TOKENS_KEY] = payload
    return new


def read_brand_themes(metadata: Mapping[str, Any]) -> tuple[BrandTheme, ...]:
    raw = metadata.get(_SCENE_THEMES_KEY) if isinstance(metadata, Mapping) else None
    if not isinstance(raw, Mapping):
        return ()
    themes: list[BrandTheme] = []
    for theme_id, payload in raw.items():
        if not isinstance(payload, Mapping):
            continue
        data = dict(payload)
        data.pop("schema_version", None)
        safe = data.pop("safe_area", ()) or ()
        try:
            themes.append(BrandTheme(
                theme_id=str(theme_id),
                palette={str(k): str(v) for k, v in dict(data.get("palette", {})).items()},
                type_scale={str(k): float(v) for k, v in dict(data.get("type_scale", {})).items()},
                spacing={str(k): float(v) for k, v in dict(data.get("spacing", {})).items()},
                motion={str(k): float(v) for k, v in dict(data.get("motion", {})).items()},
                safe_area=tuple(float(v) for v in safe[:4]),
                priority=int(data.get("priority", 0)),
                source=str(data.get("source", "")),
            ))
        except (TypeError, ValueError):
            continue
    return tuple(themes)


def read_active_brand_theme(metadata: Mapping[str, Any]) -> BrandTheme | None:
    if not isinstance(metadata, Mapping):
        return None
    active_id = metadata.get("active_brand_theme")
    if not active_id:
        return None
    for theme in read_brand_themes(metadata):
        if theme.theme_id == str(active_id):
            return theme
    return None


def set_active_brand_theme_into(metadata: Mapping[str, Any], theme_id: str | None) -> dict[str, Any]:
    """Return new metadata with the active theme id set (None clears it)."""
    new = dict(metadata) if isinstance(metadata, Mapping) else {}
    if theme_id:
        new["active_brand_theme"] = str(theme_id)
    else:
        new.pop("active_brand_theme", None)
    return new


def css_variables(tokens: DesignTokenSet) -> dict[str, str]:
    """Compile tokens into scoped ``--topos-*`` CSS custom properties."""
    values: dict[str, str] = {}
    for name, value in tokens.palette.items():
        values[f"--topos-color-{name.replace('_', '-')}"] = str(value)
    for name, value in tokens.spacing.items():
        values[f"--topos-spacing-{name.replace('_', '-')}"] = f"{float(value):g}px"
    for name, value in tokens.radius.items():
        values[f"--topos-radius-{name.replace('_', '-')}"] = f"{float(value):g}px"
    for name, value in tokens.type_scale.items():
        values[f"--topos-type-{name.replace('_', '-')}"] = f"{float(value):g}px"
    return values


__all__ = [
    "BrandTheme",
    "DesignTokenSet",
    "TokenConflict",
    "css_variables",
    "default_brand_tokens",
    "read_active_brand_theme",
    "read_brand_themes",
    "read_scene_tokens",
    "resolve_style",
    "set_active_brand_theme_into",
    "validate_theme",
    "write_scene_tokens_into",
]
