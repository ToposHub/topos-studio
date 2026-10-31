"""AE-style layer model for the Composite page.

This module implements the *layer interaction model* described in
``docs/composite_ae_layer_node_hybrid_implementation_plan_2026-08-07.md``
section 3.  It deliberately stores **only layer state** (order, name,
visibility, parent, time range, ...).  All render-affecting data
(parameters, keyframes, expressions, connections) still lives on the
underlying :class:`NodeGraph`.  A layer is never a replacement for a
node — one layer may reference several nodes; the layer system only
needs a stable ``root_node_id`` to identify where the layer's chain
plugs into the auto-generated Merge/Output chain.

Design rules (from the plan, §1.2 / §3):

1. ``LayerStack.layer_ids[0]`` is always the **topmost / foreground**
   layer.
2. ``Layer.parent_layer_id`` stores the AE-style parenting intent; the
   actual transform relationship is materialised on the Transform node's
   ``parent_id`` (already present on :class:`Node`).
3. Keyframe / opacity / blend-mode values are **not** duplicated here —
   they stay on the referenced node.  ``Layer.opacity`` /
   ``Layer.blend_mode`` are convenience mirrors used for projection and
   must be written back to the Merge / Transform node by the compiler.
4. Every mutation goes through the controller (see
   ``layer_stack_controller.py``); the data classes here are passive.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# ── Layer type constants (§3.3) ─────────────────────────────────────────────
# These are stable identifiers persisted to project files; do not rename
# existing values.  New layer types may be appended.

LAYER_TYPE_MEDIA = "media"
LAYER_TYPE_TEXT = "text"
LAYER_TYPE_SHAPE = "shape"
LAYER_TYPE_ADJUSTMENT = "adjustment"
LAYER_TYPE_SOLID = "solid"
LAYER_TYPE_GROUP = "group"
LAYER_TYPE_PRECOMP = "precomp"
LAYER_TYPE_EXTERNAL = "external"
LAYER_TYPE_3D = "3d"

ALL_LAYER_TYPES = frozenset({
    LAYER_TYPE_MEDIA,
    LAYER_TYPE_TEXT,
    LAYER_TYPE_SHAPE,
    LAYER_TYPE_ADJUSTMENT,
    LAYER_TYPE_SOLID,
    LAYER_TYPE_GROUP,
    LAYER_TYPE_PRECOMP,
    LAYER_TYPE_EXTERNAL,
    LAYER_TYPE_3D,
})

# ── Blend modes ─────────────────────────────────────────────────────────────
# A small canonical set for the layer model.  These map onto MergeNode's
# ``blend_mode`` parameter; the compiler translates the friendly name to the
# node-level value (which today already uses the same tokens as ``BLEND_MODES``
# in blend_node.py).  Keeping an independent list here avoids importing the UI
# layer / node module into the pure data model.

BLEND_MODE_NORMAL = "normal"
DEFAULT_BLEND_MODE = BLEND_MODE_NORMAL

LAYER_BLEND_MODES = frozenset({
    "normal", "multiply", "screen", "overlay", "add", "subtract",
    "difference", "darken", "lighten", "color_dodge", "color_burn",
    "soft_light", "hard_light",
})

# Layer names are intentionally friendly/persistable, while MergeNode uses
# the canonical shader tokens.  Keep this translation in the model boundary
# so every caller (compiler, validator, importers) agrees on the mapping.
LAYER_BLEND_MODE_TO_NODE = {
    "normal": "over",
    "multiply": "multiply",
    "screen": "screen",
    "overlay": "overlay",
    "add": "add",
    "subtract": "subtract",
    "difference": "difference",
    "darken": "min",
    "lighten": "max",
    "color_dodge": "color-dodge",
    "color_burn": "color-burn",
    "soft_light": "soft-light",
    "hard_light": "hard-light",
}

# Layer-level Track Matte modes.  These are intentionally separate from the
# Merge node's mask-channel UI so the layer model can persist AE semantics.
TRACK_MATTE_MODES = frozenset({"alpha", "luma"})


@dataclass
class Layer:
    """A single AE-style layer row.

    ``root_node_id`` points at the node whose output plugs into the
    auto-generated Merge chain (typically the bottom-most effect node of
    the layer, e.g. a ColorCorrect after the Transform).  ``source_node_id``
    optionally records the source asset node (ImageInput / VideoFrame / ...)
    so the media-drop flow and validators can locate the source quickly.
    """

    layer_id: str
    name: str
    root_node_id: str
    source_node_id: Optional[str] = None

    # ── Layer state (AE parity) ──
    enabled: bool = True
    solo: bool = False
    locked: bool = False
    shy: bool = False

    # ── Appearance ──
    # ``label_color`` is the AE-style label/swatch shown in the row.
    # ``opacity`` / ``blend_mode`` mirror the Merge node that combines this
    # layer with the one below; the compiler projects them onto that node so
    # there is a single source of truth at render time.
    label_color: str = "#777777"
    opacity: float = 1.0
    blend_mode: str = DEFAULT_BLEND_MODE

    # ── Parenting / timing ──
    parent_layer_id: Optional[str] = None
    track_matte_layer_id: Optional[str] = None
    track_matte_mode: str = "alpha"
    track_matte_invert: bool = False
    in_frame: Optional[int] = None
    out_frame: Optional[int] = None

    # ── Classification / management ──
    layer_type: str = LAYER_TYPE_MEDIA
    # When False the layer is a read-only projection of free-graph nodes
    # (plan §3.3 ``external``); the compiler must not rewrite its nodes.
    managed: bool = True
    collapsed: bool = True

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to a plain dict for project persistence.

        Field order is stable and human-readable; new optional fields may be
        appended without breaking older readers (``from_dict`` ignores
        unknown keys).
        """
        return {
            "layer_id": self.layer_id,
            "name": self.name,
            "root_node_id": self.root_node_id,
            "source_node_id": self.source_node_id,
            "enabled": self.enabled,
            "solo": self.solo,
            "locked": self.locked,
            "shy": self.shy,
            "label_color": self.label_color,
            "opacity": self.opacity,
            "blend_mode": self.blend_mode,
            "parent_layer_id": self.parent_layer_id,
            "track_matte_layer_id": self.track_matte_layer_id,
            "track_matte_mode": self.track_matte_mode,
            "track_matte_invert": self.track_matte_invert,
            "in_frame": self.in_frame,
            "out_frame": self.out_frame,
            "layer_type": self.layer_type,
            "managed": self.managed,
            "collapsed": self.collapsed,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Layer":
        """Deserialize a layer, tolerating missing/extra keys (forward/backward compat).

        Unknown keys are ignored.  Missing keys fall back to the dataclass
        defaults so that older projects (saved before a field existed) load
        without error — matching the lenient style of ``Node.from_dict``.
        """
        try:
            opacity = float(data.get("opacity", 1.0))
        except (TypeError, ValueError):
            opacity = 1.0
        # Persisted projects can come from older or hand-edited versions.
        # Clamp the scalar at the data boundary so a malformed file cannot
        # drive an invalid GPU uniform during the first compile.
        opacity = max(0.0, min(1.0, opacity))
        blend_mode = str(data.get("blend_mode", DEFAULT_BLEND_MODE))
        if blend_mode not in LAYER_BLEND_MODES:
            blend_mode = DEFAULT_BLEND_MODE
        track_matte_mode = str(data.get("track_matte_mode", "alpha"))
        if track_matte_mode not in TRACK_MATTE_MODES:
            track_matte_mode = "alpha"
        return cls(
            layer_id=data["layer_id"],
            name=data.get("name", "Layer"),
            root_node_id=data["root_node_id"],
            source_node_id=data.get("source_node_id"),
            enabled=data.get("enabled", True),
            solo=data.get("solo", False),
            locked=data.get("locked", False),
            shy=data.get("shy", False),
            label_color=data.get("label_color", "#777777"),
            opacity=opacity,
            blend_mode=blend_mode,
            parent_layer_id=data.get("parent_layer_id"),
            track_matte_layer_id=data.get("track_matte_layer_id"),
            track_matte_mode=track_matte_mode,
            track_matte_invert=bool(data.get("track_matte_invert", False)),
            in_frame=data.get("in_frame"),
            out_frame=data.get("out_frame"),
            layer_type=data.get("layer_type", LAYER_TYPE_MEDIA),
            managed=data.get("managed", True),
            collapsed=data.get("collapsed", True),
        )


@dataclass
class LayerStack:
    """An ordered collection of layers that compiles to a Merge/Output chain.

    MVP scope (plan §12) supports a single main stack per composition, but
    the data model is list-based from day one so that precomps / group
    stacks / multi-output stacks can be added later without a schema bump.
    """

    stack_id: str
    name: str = "Main Comp"
    output_node_id: str = ""
    layer_ids: List[str] = field(default_factory=list)
    # Auto-generated Merge node IDs owned and recycled by the compiler.
    # Manual/user-created Merges must never be listed here.
    generated_merge_node_ids: List[str] = field(default_factory=list)
    enabled: bool = True
    # When False the stack is a passive view: reordering/parenting/visibility
    # are surfaced in the UI but the compiler must not touch the graph.
    managed: bool = True
    version: int = 1

    def to_dict(self) -> Dict[str, Any]:
        return {
            "stack_id": self.stack_id,
            "name": self.name,
            "output_node_id": self.output_node_id,
            "layer_ids": list(self.layer_ids),
            "generated_merge_node_ids": list(self.generated_merge_node_ids),
            "enabled": self.enabled,
            "managed": self.managed,
            "version": self.version,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "LayerStack":
        return cls(
            stack_id=data["stack_id"],
            name=data.get("name", "Main Comp"),
            output_node_id=data.get("output_node_id", ""),
            layer_ids=list(data.get("layer_ids", [])),
            generated_merge_node_ids=list(data.get("generated_merge_node_ids", [])),
            enabled=data.get("enabled", True),
            managed=data.get("managed", True),
            version=_safe_int(data.get("version", 1), 1),
        )


def _safe_int(value: Any, default: int) -> int:
    """Coerce persisted integers without making project load brittle."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


# ── LayerMap: convenience container used by the controller / compiler ──────


class LayerMap:
    """Mutable index of ``layer_id -> Layer`` for a set of stacks.

    The stacks themselves store only ordered IDs; callers frequently need to
    resolve an ID to its :class:`Layer` without scanning.  ``LayerMap``
    provides O(1) lookup while keeping the authoritative layer list on the
    owning graph (see :meth:`NodeGraph.layer_stacks_flat`).  It is rebuilt
    on demand rather than persisted.
    """

    def __init__(self, layers: Optional[List[Layer]] = None) -> None:
        self._layers: Dict[str, Layer] = {}
        if layers:
            for layer in layers:
                self._layers[layer.layer_id] = layer

    def add(self, layer: Layer) -> None:
        self._layers[layer.layer_id] = layer

    def remove(self, layer_id: str) -> Optional[Layer]:
        return self._layers.pop(layer_id, None)

    def get(self, layer_id: str) -> Optional[Layer]:
        return self._layers.get(layer_id)

    def __contains__(self, layer_id: str) -> bool:
        return layer_id in self._layers

    def __getitem__(self, layer_id: str) -> Layer:
        return self._layers[layer_id]

    def __len__(self) -> int:
        return len(self._layers)

    def __iter__(self):
        return iter(self._layers.values())

    def copy(self) -> "LayerMap":
        new = LayerMap()
        new._layers = {lid: copy.deepcopy(l) for lid, l in self._layers.items()}
        return new
