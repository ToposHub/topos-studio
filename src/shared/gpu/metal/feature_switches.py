"""Kill switches and the rollout ledger of the Metal migration (plan 8.5 / 8.6).

Every Metal capability can be switched off on its own, whatever its own
opt-in says::

    TOPOS_METAL_DISABLE=native_viewport,edit_metal_composite

That is the first move when a regression shows up: switch the capability
off (a whole session stays on the compatible path), then fix, re-test and
remove the switch again.  A disabled capability reports
``disabled_by_switch`` as its reason, so logs and traces tell a deliberate
switch-off from a device, format, shader or drawable failure.

:class:`Rollout` is the ledger of which capability is on by default and
under which compatibility condition it may move on (plan 8.5: "记录每次
默认切换和兼容条件").  Today every entry is explicit opt-in only.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum
from typing import Optional, Tuple


ENV = "TOPOS_METAL_DISABLE"

DISABLED_REASON = "disabled_by_switch"


class MetalFeature(str, Enum):
    """A Metal capability that can be switched off on its own."""

    EDIT_PREVIEW = "edit_metal_preview"         # NV12 (and a LUT) converted on Metal, shown by the GL viewer
    EDIT_COMPOSITE = "edit_metal_composite"     # multi-layer Edit preview composite (plan 2.6)
    NATIVE_VIEWPORT = "native_viewport"         # frames presented by a CAMetalLayer (plan 2.1–2.3, 2.5)
    COMPOSITE_GRAPH = "composite_metal"         # 2D composite graph on Metal (plan 5)
    COLOR_GRAPH_TEXTURE = "color_graph_metal"   # colour graph on Metal textures (plan 4): node thumbnails follow it
    EXPORT_NATIVE_VT = "export_native_vt"       # Metal → VideoToolbox H.264 export (plan 3)


@dataclass(frozen=True)
class Rollout:
    """How one capability is enabled, and what it takes to change that."""

    feature: MetalFeature
    #: ``"opt-in"``: off unless asked for; ``"default"``: on when the machine can.
    default: str
    #: How to ask for it while it is opt-in.
    opt_in: str
    #: The plan step that set this state.
    since: str
    #: The compatibility condition for moving it to ``"default"``.
    condition: str


ROLLOUT: Tuple[Rollout, ...] = (
    Rollout(
        MetalFeature.EDIT_PREVIEW, "opt-in",
        "TOPOS_EDIT_METAL_PREVIEW=1, or TOPOS_BACKEND_GRAPHICS=metal",
        "plan 2 (before this plan)",
        "UHD playback frame counts and seek p95 held on Intel discrete and Apple Silicon",
    ),
    Rollout(
        MetalFeature.EDIT_COMPOSITE, "opt-in",
        "the same switch as edit_metal_preview, in its 'all' mode",
        "plan 2.6",
        "multi-layer project playback on a real machine; playback-time cost measured",
    ),
    Rollout(
        MetalFeature.NATIVE_VIEWPORT, "opt-in",
        "TOPOS_EDIT_METAL_VIEWPORT=1",
        "plan 2.1–2.3",
        "resize, hide, fullscreen, screen change and close verified with the display awake",
    ),
    Rollout(
        MetalFeature.COMPOSITE_GRAPH, "opt-in",
        "TOPOS_COMPOSITE_METAL=1",
        "plan 5.1–5.3",
        "a production caller (comp clip, headless export) matches the CPU/GL frame",
    ),
    Rollout(
        MetalFeature.COLOR_GRAPH_TEXTURE, "opt-in",
        "TOPOS_COLOR_METAL=1: node thumbnails (plan 4.6); the Color Viewer presenter "
        "follows the same switch and is not wired yet",
        "plan 4.2–4.3",
        "edit renderer and delivery take their colour graph from the shared entry",
    ),
    Rollout(
        MetalFeature.EXPORT_NATIVE_VT, "default",
        "on with graphics auto or metal on macOS; TOPOS_EXPORT_NATIVE_VT=0 turns it off",
        "plan 3.1–3.2",
        "audio, fractional frame rates and cancellation verified on exported files",
    ),
)


def parse_disabled(raw: str) -> Tuple[Tuple[str, ...], Tuple[str, ...]]:
    """``(disabled, unknown)`` feature names of a ``TOPOS_METAL_DISABLE`` value."""
    disabled: list = []
    unknown: list = []
    for item in raw.replace(";", ",").split(","):
        name = item.strip().lower()
        if not name:
            continue
        if name in MetalFeature._value2member_map_:
            if name not in disabled:
                disabled.append(name)
        elif name not in unknown:
            unknown.append(name)
    return tuple(disabled), tuple(unknown)


#: The opt-in variable of each capability that is asked for by a plain
#: true/false variable of its own.  The others have more than one way in
#: (``edit_metal_preview`` also follows the graphics backend setting).
OPT_IN_ENV = {
    MetalFeature.COLOR_GRAPH_TEXTURE: "TOPOS_COLOR_METAL",
}

_TRUE = ("1", "true", "yes", "on")


def opt_in_enabled(feature: MetalFeature) -> Tuple[bool, str]:
    """``(enabled, reason)``: asked for by its opt-in variable and not switched off.

    ``reason`` is a stable key when off, so a log or a trace tells a session
    that never asked for the capability from one where the kill switch
    turned it off.
    """
    variable = OPT_IN_ENV[feature]
    if os.environ.get(variable, "").strip().lower() not in _TRUE:
        return False, "opt_in_missing"
    if feature_disabled(feature):
        return False, f"{DISABLED_REASON}:{feature.value}"
    return True, ""


def disabled_features() -> frozenset:
    """The features switched off in this process."""
    disabled, _unknown = parse_disabled(os.environ.get(ENV, ""))
    return frozenset(disabled)


def feature_disabled(feature: MetalFeature) -> bool:
    return feature.value in disabled_features()


def switch_refusal(feature: MetalFeature) -> Optional[str]:
    """``"disabled_by_switch:<feature>"`` when the capability is switched off."""
    if feature_disabled(feature):
        return f"{DISABLED_REASON}:{feature.value}"
    return None


def rollout_of(feature: MetalFeature) -> Rollout:
    for entry in ROLLOUT:
        if entry.feature is feature:
            return entry
    raise KeyError(feature)


__all__ = [
    "DISABLED_REASON",
    "ENV",
    "ROLLOUT",
    "MetalFeature",
    "OPT_IN_ENV",
    "Rollout",
    "disabled_features",
    "feature_disabled",
    "opt_in_enabled",
    "parse_disabled",
    "rollout_of",
    "switch_refusal",
]
