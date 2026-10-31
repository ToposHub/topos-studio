"""SUB-P2-06: Caption style presets — named, reusable :class:`CaptionStyle` bundles.

The plan (SUB-P2-06) calls for "style preset + track default style + cue
override + safe-area anchor" as Phase 2A's first style-management step. This
module provides the preset half: a registry of named styles (broadcast,
social, lower-third, …) that a track can reference by id instead of copying
34 fields by hand.

Design:
* :class:`CaptionStylePreset` — a named, immutable (id, label, style, optional
  description) triple. Presets are *values*, not live references: applying a
  preset copies its style into the track/cue, so later preset edits do not
  retroactively change cues that already applied it (unless the caller uses
  :func:`propagate_preset_change`, which is an explicit, auditable operation).
* :class:`StylePresetRegistry` — the lookup table. Ships with a few built-ins
  (:data:`BUILTIN_PRESETS`) and accepts user/project presets at runtime. The
  registry is a plain dict; no singleton magic, so tests can construct fresh.
* :func:`apply_preset_to_track` / :func:`apply_preset_to_cue` — the one-shot
  helpers that resolve a preset id to a style and write it.
* :func:`propagate_preset_change` — when a preset is edited, re-apply it to
  every track/cue that was *derived from* it and has not since been overridden
  (the SUB-P2-06 "propagation" requirement). Non-overriding = the cue's style
  fields that the preset sets are all still equal to the preset's values.

This module never touches ``SubtitleClip`` directly; it works in terms of
:class:`CaptionStyle` (from model.py) and returns styles for the caller to
store via the existing command layer.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple

from src.features.edit.core.captions.model import (
    CaptionStyle,
    STYLE_FIELDS,
)


# ──────────────────────────────────────────────────────────────────────────
# Preset definition
# ──────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class CaptionStylePreset:
    """A named, reusable style bundle.

    Attributes:
        preset_id: stable unique id (e.g. ``"builtin.broadcast"``). Used as the
            reference key on tracks/cues. Never localized.
        label: human-readable name for UI (may be localized by caller).
        style: the :class:`CaptionStyle` payload. May be partial (some fields
            ``None``) — a partial preset only overrides the fields it sets,
            leaving the rest to whatever the target already had.
        description: optional long-form description for tooltips.
        is_builtin: True for presets shipped with the app (not deletable).
        category: C1 标题模板类别（"title" | "lower_third" | "callout" |
            "credit" | "social" | None=通用字幕样式）。Titles tab 按此分组。
    """
    preset_id: str
    label: str
    style: CaptionStyle
    description: str = ""
    is_builtin: bool = False
    category: str | None = None

    def __post_init__(self) -> None:
        if not self.preset_id:
            raise ValueError("preset_id cannot be empty")
        if not isinstance(self.style, CaptionStyle):
            raise TypeError("style must be a CaptionStyle")


# ──────────────────────────────────────────────────────────────────────────
# Built-in presets
# ──────────────────────────────────────────────────────────────────────────

#: Built-in presets shipped with the app. Conservative defaults that render
#: well at 1080p/4K; users can override per-project.
BUILTIN_PRESETS: tuple[CaptionStylePreset, ...] = (
    CaptionStylePreset(
        preset_id="builtin.default",
        label="Default",
        style=CaptionStyle(),  # empty = inherit everything / use legacy defaults
        description="No overrides; uses track and legacy defaults.",
        is_builtin=True,
    ),
    CaptionStylePreset(
        preset_id="builtin.broadcast",
        label="Broadcast",
        style=CaptionStyle(
            font_family="Arial",
            font_size=120,
            font_weight="normal",
            halign="center",
            valign="bottom",
            fill_r=1.0, fill_g=1.0, fill_b=1.0, fill_a=1.0,
            outline_enable=True,
            outline_width=4.0,
            outline_r=0.0, outline_g=0.0, outline_b=0.0, outline_a=1.0,
            shadow_enable=False,
        ),
        description="Classic broadcast subtitle: white sans, black outline.",
        is_builtin=True,
        category="subtitle",
    ),
    CaptionStylePreset(
        preset_id="builtin.social",
        label="Social",
        style=CaptionStyle(
            font_family="Arial",
            font_size=160,
            font_weight="bold",
            halign="center",
            valign="middle",
            fill_r=1.0, fill_g=1.0, fill_b=1.0, fill_a=1.0,
            outline_enable=True,
            outline_width=6.0,
            outline_r=0.0, outline_g=0.0, outline_b=0.0, outline_a=1.0,
            shadow_enable=True,
            shadow_offset_x=4.0,
            shadow_offset_y=4.0,
            shadow_blur=6.0,
            shadow_r=0.0, shadow_g=0.0, shadow_b=0.0, shadow_a=0.8,
        ),
        description="Large bold caption for vertical social video.",
        is_builtin=True,
        category="social",
    ),
    CaptionStylePreset(
        preset_id="builtin.lower_third",
        label="Lower Third",
        style=CaptionStyle(
            font_family="Arial",
            font_size=100,
            font_weight="demibold",
            halign="left",
            valign="bottom",
            fill_r=1.0, fill_g=1.0, fill_b=1.0, fill_a=1.0,
            outline_enable=False,
            shadow_enable=True,
            shadow_offset_x=3.0,
            shadow_offset_y=3.0,
            shadow_blur=4.0,
            shadow_r=0.0, shadow_g=0.0, shadow_b=0.0, shadow_a=0.7,
        ),
        description="Speaker lower-third: left-aligned, soft shadow.",
        is_builtin=True,
        category="lower_third",
    ),
    CaptionStylePreset(
        preset_id="builtin.karaoke",
        label="Karaoke",
        style=CaptionStyle(
            font_family="Arial",
            font_size=140,
            font_weight="bold",
            italic=True,
            halign="center",
            valign="middle",
            fill_r=1.0, fill_g=0.93, fill_b=0.0, fill_a=1.0,  # yellow
            outline_enable=True,
            outline_width=5.0,
            outline_r=0.0, outline_g=0.0, outline_b=0.0, outline_a=1.0,
            shadow_enable=True,
            shadow_offset_x=5.0,
            shadow_offset_y=5.0,
            shadow_blur=4.0,
            shadow_r=0.0, shadow_g=0.0, shadow_b=0.0, shadow_a=0.6,
        ),
        description="Karaoke-style: yellow italic bold with heavy outline.",
        is_builtin=True,
        category="subtitle",
    ),
)

# ──────────────────────────────────────────────────────────────────────────
# C1: 标题模板库内置预设（title / callout / credit / social / lower_third）
# ──────────────────────────────────────────────────────────────────────────

TITLE_CATEGORY_PRESETS: tuple[CaptionStylePreset, ...] = (
    CaptionStylePreset(
        preset_id="builtin.title_main",
        label="Main Title",
        style=CaptionStyle(
            font_family="Arial", font_size=200, font_weight="bold",
            halign="center", valign="middle",
            fill_r=1.0, fill_g=1.0, fill_b=1.0,
            outline_enable=True, outline_width=6.0,
            outline_r=0.0, outline_g=0.0, outline_b=0.0, outline_a=1.0,
            shadow_enable=True, shadow_offset_x=0.0, shadow_offset_y=6.0,
            shadow_blur=10.0, shadow_r=0.0, shadow_g=0.0, shadow_b=0.0, shadow_a=0.75,
        ),
        description="影片主标题：大号加粗居中，柔影。",
        is_builtin=True, category="title",
    ),
    CaptionStylePreset(
        preset_id="builtin.title_section",
        label="Section Title",
        style=CaptionStyle(
            font_family="Arial", font_size=140, font_weight="demibold",
            halign="left", valign="top",
            fill_r=1.0, fill_g=1.0, fill_b=1.0,
            outline_enable=False,
            shadow_enable=True, shadow_offset_x=2.0, shadow_offset_y=4.0,
            shadow_blur=8.0, shadow_r=0.0, shadow_g=0.0, shadow_b=0.0, shadow_a=0.7,
        ),
        description="章节标题：左上对齐。",
        is_builtin=True, category="title",
    ),
    CaptionStylePreset(
        preset_id="builtin.title_accent",
        label="Accent Title",
        style=CaptionStyle(
            font_family="Arial", font_size=170, font_weight="black",
            halign="center", valign="middle",
            fill_r=0.89, fill_g=0.57, fill_b=0.15,
            outline_enable=True, outline_width=4.0,
            outline_r=0.12, outline_g=0.08, outline_b=0.02, outline_a=1.0,
            shadow_enable=False,
        ),
        description="强调色标题：品牌橙。",
        is_builtin=True, category="title",
    ),
    CaptionStylePreset(
        preset_id="builtin.callout_note",
        label="Callout Note",
        style=CaptionStyle(
            font_family="Arial", font_size=90, font_weight="normal",
            halign="right", valign="top",
            fill_r=1.0, fill_g=0.98, fill_b=0.92,
            outline_enable=True, outline_width=3.0,
            outline_r=0.1, outline_g=0.1, outline_b=0.1, outline_a=0.9,
            shadow_enable=False,
        ),
        description="标注说明：右上小字带描边。",
        is_builtin=True, category="callout",
    ),
    CaptionStylePreset(
        preset_id="builtin.callout_warning",
        label="Callout Warning",
        style=CaptionStyle(
            font_family="Arial", font_size=100, font_weight="bold",
            halign="center", valign="top",
            fill_r=1.0, fill_g=0.25, fill_b=0.15,
            outline_enable=False,
            shadow_enable=True, shadow_offset_x=0.0, shadow_offset_y=3.0,
            shadow_blur=6.0, shadow_r=0.0, shadow_g=0.0, shadow_b=0.0, shadow_a=0.8,
        ),
        description="警示标注：红色居中。",
        is_builtin=True, category="callout",
    ),
    CaptionStylePreset(
        preset_id="builtin.credit_roll",
        label="Credit Roll",
        style=CaptionStyle(
            font_family="Arial", font_size=80, font_weight="normal",
            halign="center", valign="middle",
            fill_r=0.92, fill_g=0.92, fill_b=0.92,
            outline_enable=False, shadow_enable=False,
        ),
        description="片尾字幕：小号浅灰居中。",
        is_builtin=True, category="credit",
    ),
)

BUILTIN_PRESETS = BUILTIN_PRESETS + TITLE_CATEGORY_PRESETS

# Quick lookup of builtin preset ids (for the "is_builtin → not deletable" rule).
_BUILTIN_IDS: frozenset = frozenset(p.preset_id for p in BUILTIN_PRESETS)


# ──────────────────────────────────────────────────────────────────────────
# Registry
# ──────────────────────────────────────────────────────────────────────────

class StylePresetRegistry:
    """Lookup table for :class:`CaptionStylePreset`.

    Construct with :data:`BUILTIN_PRESETS` preloaded, then add project/user
    presets via :meth:`register`. Presets are values; the registry is a plain
    dict keyed by ``preset_id``. Built-ins cannot be overwritten or removed
    (they may be *shadowed* by a user preset of a different id).
    """

    def __init__(self, include_builtins: bool = True) -> None:
        self._presets: dict[str, CaptionStylePreset] = {}
        if include_builtins:
            for p in BUILTIN_PRESETS:
                self._presets[p.preset_id] = p

    # ── Core ops ──

    def register(self, preset: CaptionStylePreset) -> None:
        """Add or replace a preset. Built-in ids cannot be overwritten."""
        if not preset.preset_id:
            raise ValueError("preset_id cannot be empty")
        # Protect built-in ids: only the original builtin object may occupy
        # a builtin id. A user preset (is_builtin=False) trying to claim a
        # builtin id, or a different builtin object, both raise.
        if preset.preset_id in _BUILTIN_IDS:
            existing = self._presets.get(preset.preset_id)
            if existing is None:
                # Shouldn't happen (builtins preloaded) but be safe.
                self._presets[preset.preset_id] = preset
                return
            if existing is preset:
                return  # idempotent re-register
            raise ValueError(
                f"Cannot overwrite built-in preset: {preset.preset_id}"
            )
        self._presets[preset.preset_id] = preset

    def remove(self, preset_id: str) -> bool:
        """Remove a user preset. Returns True if removed. Built-ins refuse."""
        if preset_id in _BUILTIN_IDS:
            return False
        return self._presets.pop(preset_id, None) is not None

    def get(self, preset_id: str) -> Optional[CaptionStylePreset]:
        return self._presets.get(preset_id)

    def __contains__(self, preset_id: str) -> bool:
        return preset_id in self._presets

    def all(self) -> List[CaptionStylePreset]:
        """All presets, built-ins first then user, sorted by label."""
        items = list(self._presets.values())
        items.sort(key=lambda p: (not p.is_builtin, p.label, p.preset_id))
        return items

    def builtin_ids(self) -> frozenset:
        return _BUILTIN_IDS

    def to_dict(self) -> List[dict[str, Any]]:
        """Serialize user presets (built-ins are implicit; only non-builtin
        presets are written so project files stay small).

        AUD-16001: 持久化包含 ``category``——旧实现往返丢失该字段，
        Titles tab 按 category 分组对用户预设失效。
        """
        return [
            {
                "preset_id": p.preset_id,
                "label": p.label,
                "style": p.style.to_dict(),
                "description": p.description,
                "is_builtin": False,
                "category": p.category,
            }
            for p in self._presets.values()
            if not p.is_builtin
        ]

    @staticmethod
    def from_dict(data: Optional[Iterable[MappingLike]]) -> "StylePresetRegistry":
        """Deserialize. Built-ins are auto-loaded; only user presets read."""
        reg = StylePresetRegistry(include_builtins=True)
        if not data:
            return reg
        for entry in data:
            if not isinstance(entry, dict):
                continue
            if entry.get("is_builtin", False):
                continue  # built-ins already loaded
            try:
                # AUD-16001: 读取 category（与 title_templates.py 的清洗
                # 方式一致：空值归一为 None）。
                category = entry.get("category")
                preset = CaptionStylePreset(
                    preset_id=str(entry["preset_id"]),
                    label=str(entry.get("label", entry["preset_id"])),
                    style=CaptionStyle.from_dict(entry.get("style")),
                    description=str(entry.get("description", "")),
                    is_builtin=False,
                    category=str(category) if category else None,
                )
                reg.register(preset)
            except (KeyError, ValueError, TypeError):
                continue  # skip malformed entry, don't crash load
        return reg


# Typing helper alias: a dict-like with .get() (kept loose to avoid importing
# collections.abc.Mapping just for one annotation site; this module never
# relies on Mapping-only behaviour).
MappingLike = Any


# ──────────────────────────────────────────────────────────────────────────
# Apply / propagate helpers
# ──────────────────────────────────────────────────────────────────────────

def apply_preset_to_track(
    registry: StylePresetRegistry,
    preset_id: str,
) -> CaptionStyle:
    """Resolve ``preset_id`` to its style for writing onto a track's default.

    Returns the preset's style (possibly partial). The caller writes it onto
    ``EditTrack.caption_metadata.default_style`` via a command. Raises
    ``KeyError`` if the preset is unknown.
    """
    preset = registry.get(preset_id)
    if preset is None:
        raise KeyError(f"Unknown caption style preset: {preset_id!r}")
    return preset.style


def apply_preset_to_cue(
    registry: StylePresetRegistry,
    preset_id: str,
    merge_with: Optional[CaptionStyle] = None,
) -> CaptionStyle:
    """Resolve a preset for a cue override, optionally preserving existing
    cue-specific fields.

    If ``merge_with`` is given (the cue's current override style), the preset's
    non-``None`` fields win, but fields the preset leaves ``None`` fall through
    to whatever the cue already had — so applying a "bold only" preset keeps
    the cue's existing colour rather than blanking it.
    """
    style = apply_preset_to_track(registry, preset_id)
    if merge_with is None:
        return style
    # merge_with overrides on top of preset? No — preset should win for fields
    # it explicitly sets. So: take preset's set fields, else merge_with's.
    kwargs: dict[str, Any] = {}
    for f in STYLE_FIELDS:
        pv = getattr(style, f)
        kwargs[f] = pv if pv is not None else getattr(merge_with, f)
    return CaptionStyle(**kwargs)


def _style_field_match(a: CaptionStyle, b: CaptionStyle, field_name: str) -> bool:
    """True if ``a`` and ``b`` agree on ``field_name`` (both None counts)."""
    return getattr(a, field_name) == getattr(b, field_name)


def cue_matches_preset(
    cue_override: CaptionStyle,
    preset_style: CaptionStyle,
) -> bool:
    """True if ``cue_override`` was derived from ``preset_style`` and not since
    overridden on any field the preset sets.

    Used by :func:`propagate_preset_change` to decide which cues to update when
    a preset is edited. A cue that has manually diverged on a preset field is
    left alone (the user's manual edit wins).
    """
    for f in STYLE_FIELDS:
        pv = getattr(preset_style, f)
        if pv is None:
            continue  # preset doesn't care about this field
        if not _style_field_match(cue_override, preset_style, f):
            return False
    return True


def propagate_preset_change(
    targets: Iterable[Tuple[str, CaptionStyle]],
    old_preset_style: CaptionStyle,
    new_preset_style: CaptionStyle,
) -> Dict[str, CaptionStyle]:
    """Compute updated styles for targets that should follow a preset change.

    Args:
        targets: ``[(target_id, current_style), ...]`` — e.g. cue overrides or
            track defaults that may have been derived from the preset.
        old_preset_style: the preset's style *before* the edit.
        new_preset_style: the preset's style *after* the edit.

    Returns:
        ``{target_id: new_style}`` for every target whose current style matches
        ``old_preset_style`` (i.e. hasn't been manually overridden since).
        Targets that diverged are omitted from the result (caller leaves them).

    This is the SUB-P2-06 propagation primitive: editing a preset updates all
    non-overridden consumers, preserving manual per-target edits.
    """
    out: dict[str, CaptionStyle] = {}
    for target_id, current in targets:
        if not cue_matches_preset(current, old_preset_style):
            continue
        # Merge new preset onto current: new preset's set fields replace;
        # fields the new preset leaves None keep current's value (so a preset
        # edit that drops a field doesn't blank consumers).
        kwargs: dict[str, Any] = {}
        for f in STYLE_FIELDS:
            nv = getattr(new_preset_style, f)
            kwargs[f] = nv if nv is not None else getattr(current, f)
        out[target_id] = CaptionStyle(**kwargs)
    return out
