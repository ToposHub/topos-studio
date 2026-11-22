"""KeyframesModel — pure-data layer for the Composite Keyframes panel.

This module turns a NodeGraph into the row/track/keyframe structures the
Keyframes panel paints. It is deliberately GUI-free (no QWidget, no QPainter)
so it can be unit-tested headlessly and reused by both the timeline view and
the (phase-3) Spline curve view.

Design (per docs/composite_davinci_keyframes_tool_plan_2026-06-28.md §5):

- The authoritative animation source is each Node instance's
  ``keyframe_tracks`` (set up in P1). This model never reads the class-level
  PARAM_DEFS for keyframe data.
- Rows form a flat ordered list mixing node rows and their child parameter
  rows. Expansion / filtering produce a *visible* row subsequence.
- Range bars derive from per-node ``time_range`` (None = whole composition).
- Colors come from ``node.effective_header_color()`` so user overrides and
  NodeGraph/Properties color changes flow through automatically.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from fractions import Fraction
from typing import Any, Dict, List, Optional, Tuple

from src.features.composite.core.layer_model import (
    LAYER_TYPE_AUDIO,
    BOTTOM_UP_LAYER_STACK_VERSION, LAYER_MERGE_OWNER_PARAM,
    LAYER_TYPE_3D, LAYER_TYPE_SOLID, LAYER_TYPE_TEXT, LEGACY_LAYER_STACK_VERSION,
)
from src.features.composite.core.layer_time import LayerTiming, layer_timing_from_video
from src.shared.i18n.i18n import I18N

logger = logging.getLogger(__name__)


# Both views use the same row geometry. Keeping these values in one place is
# important: the tree side has to reserve the ruler's header row or its first
# item will be painted on top of the timeline ruler.
KEYFRAMES_RULER_H = 30
KEYFRAMES_ROW_H = 24

_LAYER_GROUPS = (
    ("masks", "Masks"), ("effects", "Effects"), ("transform", "Transform"),
    ("content", "Content"), ("advanced", "Advanced"),
)
_COMPONENT_GROUPS = (
    ("anchor", "Anchor Point", ("pivot_x", "pivot_y")),
    ("position", "Position", ("translate_x", "translate_y")),
    ("scale", "Scale", ("scale_x", "scale_y")),
)
LAYER_TRANSFORM_PROPERTIES = {group: names for group, _, names in _COMPONENT_GROUPS}
LAYER_TRANSFORM_PROPERTIES.update(rotation=("rotation",), opacity=("opacity",))
_TRANSFORM_PRIORITY = tuple(name for _, _, names in _COMPONENT_GROUPS for name in names) + (
    "rotation", "opacity",
)
_TRANSFORM_SAMPLING = frozenset({
    "filter", "motion_blur", "pivot_mode", "samples", "shutter", "shutter_offset",
    "mask_channel", "mask_invert", "mix",
})
_LAYER_PROPERTY_LABELS = {
    "rotation": ("keyframes.property_rotation", "Rotation"),
    "opacity": ("keyframes.property_opacity", "Opacity"),
}
_MASK_NODE_TYPES = frozenset({"mask", "matte", "mask_merge", "roto_shape"})
_MEDIA_NODE_TYPES = frozenset({"image_input", "video_frame", "timeline_clip_input"})


# ── Shared timeline state (§5.4) ─────────────────────────────────────────


@dataclass
class CompositeTimelineState:
    """Lightweight timeline state shared by the Keyframes panel and the
    properties-panel keyframe editor.

    Precedence for current_frame is resolved by the caller (CompositePage);
    this dataclass just carries the resolved values.
    """
    start_frame: int = 0
    end_frame: int = 100
    current_frame: int = 0
    fps: float = 24.0
    display_mode: str = "time"  # time | frame | seconds | timecode


# ── Row + visual structures (§P2) ───────────────────────────────────────


@dataclass
class KeyframePointVisual:
    """One keyframe point on a parameter track.

    P1-1: ``extra`` carries through any fields the timeline doesn't interpret
    directly (handle_left/right, tangent_mode, bezier_in/out...) so that moving
    a keyframe in the Keyframes panel does not silently drop the user's bezier
    handles. ``to_keyframe_dict()`` re-emits frame/value/interpolation + extra.
    """
    frame: int
    value: Any
    interpolation: str = "linear"
    selected: bool = False
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_keyframe_dict(self) -> Dict[str, Any]:
        """Serialize back, preserving unknown handle/tangent fields (P1-1)."""
        d = {"frame": self.frame, "value": self.value,
             "interpolation": self.interpolation}
        d.update(self.extra)
        return d


@dataclass
class TimeRangeBar:
    """A node's effective time range bar on the timeline.

    ``default=True`` marks AE-style layer bars: the node has no explicit
    time_range yet, so the bar spans the whole composition. Trimming such a
    bar writes the first explicit time_range into the node. For video media
    the default bar instead spans the clip's full source length (which may
    exceed the composition), so users can see and trim the whole material.
    """
    start_frame: int
    end_frame: int
    label: str = ""
    color: str = "#555555"
    default: bool = False


@dataclass
class KeyframesTrackVisual:
    """The right-hand-side visuals for one row."""
    row_id: str
    range_bar: Optional[TimeRangeBar] = None
    keyframes: List[KeyframePointVisual] = field(default_factory=list)
    color: str = "#bdbdbd"        # curve / keyframe point color
    node_color: str = "#555555"   # range bar / node-row color
    # Source frames are independent of the bar's composition frame numbers.
    source_total: int = 0
    source_in: int = 0
    source_step: Fraction = Fraction(1)
    source_phase: Fraction = Fraction(0)
    media_timing: LayerTiming | None = None
    layer_managed: bool = True


@dataclass
class KeyframesRow:
    """One node, parameter, layer or property group in the left tree."""
    row_id: str
    node_id: str
    param_name: Optional[str]        # None for node rows
    label: str
    row_type: str                    # "node" | "param" | "layer" | "group"
    depth: int
    expanded: bool = True
    locked: bool = False
    selected: bool = False
    visible: bool = True
    node_color: str = "#555555"
    # Parent group row in the layer projection.  Node-mode rows leave this
    # empty; layer-mode effect/parameter rows use it so collapsing a Layer
    # hides the complete referenced node chain without duplicating state.
    parent_row_id: Optional[str] = None
    # ── Layer-mode fields (AE/Nuke hybrid, plan §6.2) ──
    # All projected descendants carry the owning layer ID. Only the layer
    # heading shows the 1-based index (top layer = 1).
    layer_id: Optional[str] = None
    layer_index: Optional[int] = None
    layer_enabled: bool = True        # AE eye toggle state
    layer_solo: bool = False
    layer_shy: bool = False
    layer_motion_blur: Optional[bool] = None
    layer_blended: bool = True        # False for layers w/o owning merge
    layer_managed: bool = True
    layer_type: str = "media"
    # ── Layer relation diagnostics (P1-04) ──
    relation_badge: str = ""
    relation_badge_state: str = "normal"
    relation_tooltip: str = ""
    parent_layer_name: str = ""
    # A grouped value edits these independent parameters on node_id.
    component_params: tuple[str, ...] = ()


# ── Frame <-> pixel mapping (§P2.5) ─────────────────────────────────────


class FrameMapper:
    """Map between frame numbers and timeline x-pixels.

    Supports negative frames (Fusion-style composition origin), zoom
    (pixels_per_frame) and horizontal scroll (offset in pixels).
    """

    def __init__(self, pixels_per_frame: float = 4.0,
                 scroll_offset: float = 0.0) -> None:
        self.pixels_per_frame = max(0.05, float(pixels_per_frame))
        self.scroll_offset = float(scroll_offset)

    def frame_to_x(self, frame: float, origin_x: float = 0.0) -> float:
        return origin_x + self.scroll_offset + frame * self.pixels_per_frame

    def x_to_frame(self, x: float, origin_x: float = 0.0) -> float:
        return (x - origin_x - self.scroll_offset) / self.pixels_per_frame

    def set_zoom(self, pixels_per_frame: float, *, fit_scale: Optional[float] = None) -> None:
        # Clamp both ends: 0.05 px/frame keeps huge comps navigable,
        # 256 px/frame gives comfortable frame-by-frame scrubbing.
        minimum, maximum = 0.05, 256.0
        if fit_scale is not None:
            minimum = fit_scale
            maximum = max(maximum, fit_scale)
        self.pixels_per_frame = max(minimum, min(maximum, float(pixels_per_frame)))

    def set_scroll(self, offset: float) -> None:
        self.scroll_offset = float(offset)


# ── Channel color assignment (§3.4) ─────────────────────────────────────


def _channel_color_for(param_name: str, node_color: str) -> str:
    """Pick a curve color for a parameter.

    Hue/saturation/angle/strength params get fixed channel colors; everything
    else inherits the node color so a node's tracks read as one family.
    """
    from src.shared.ui.style_sheet import C
    name = (param_name or "").lower()
    if "hue" in name:
        return C.CURVE_HUE
    if "sat" in name:
        return C.CURVE_SAT
    if "angle" in name or "rotation" in name:
        return C.CURVE_ANGLE
    if "strength" in name or "intensity" in name or "opacity" in name:
        return C.CURVE_STRENGTH
    return node_color or C.CURVE_DEFAULT


# ── Model ───────────────────────────────────────────────────────────────


class KeyframesModel:
    """Build row + track data from a NodeGraph.

    The model is rebuilt on demand from the live graph (no incremental
    diffing in this phase). UI expansion / filtering / selection live on the
    panel; the model exposes helpers to apply them.
    """

    def __init__(self) -> None:
        self._rows: List[KeyframesRow] = []
        self._tracks: Dict[str, KeyframesTrackVisual] = {}
        self._expanded: set = set()         # node_ids expanded
        self._layer_expansion: dict[str, bool] = {}
        self._layer_row_expansion: dict[str, bool] = {}
        self._split_dimensions: set[str] = set()
        self._layer_property_focus: dict[str, frozenset[str]] = {}
        self._focused_layer_expansion: dict[str, dict[str, bool]] = {}
        self._filter: str = "all"           # all | animated | modified | expression | selected
        self._text_filter: str = ""
        self._selected_node_id: Optional[str] = None
        # Composition frame range — used for AE-style default layer bars
        # (nodes without an explicit time_range span the whole comp).
        self._comp_range: Tuple[int, int] = (0, 0)
        # AE/Nuke hybrid view mode (plan §6.1).  "nodes" = legacy behaviour;
        # "layers" = AE layer stack with one row per layer.
        self._view_mode: str = "nodes"
        self._hide_shy_layers = False

    # ── view mode (AE/Nuke hybrid, plan §6.1) ──

    VIEW_MODE_NODES = "nodes"
    VIEW_MODE_LAYERS = "layers"

    @property
    def view_mode(self) -> str:
        return self._view_mode

    def set_view_mode(self, mode: str) -> None:
        if mode in (self.VIEW_MODE_NODES, self.VIEW_MODE_LAYERS):
            self._view_mode = mode

    def set_hide_shy_layers(self, hide: bool) -> None:
        self._hide_shy_layers = hide

    def rebuild_layers(self, controller: Any,
                       selected_layer_id: Optional[str] = None) -> None:
        """Regenerate rows from a :class:`LayerStackController`.

        Layer order matches the stack. Property groups organize the live
        node parameters without changing their animation track IDs.
        """
        self._rows.clear()
        self._tracks.clear()
        self._selected_node_id = None
        if controller is None:
            return
        stack = controller.main_stack()
        if stack is None:
            return
        legacy_stack = getattr(stack, "version", LEGACY_LAYER_STACK_VERSION) < BOTTOM_UP_LAYER_STACK_VERSION
        layers_map = controller.layers_map()
        blur_states = controller.layer_motion_blur_states(layers_map.values())
        self._layer_property_focus = {layer_id: focus for layer_id, focus
                                      in self._layer_property_focus.items()
                                      if layer_id in layers_map}
        self._focused_layer_expansion = {layer_id: expansion for layer_id, expansion
                                         in self._focused_layer_expansion.items()
                                         if layer_id in layers_map}
        active_layer_ids = None
        active_layers_fn = getattr(controller, "active_layer_ids_for_output", None)
        if callable(active_layers_fn):
            try:
                active_layer_ids = set(active_layers_fn())
            except (AttributeError, KeyError, TypeError, ValueError):
                logger.debug("active_layer_ids_for_output failed; "
                             "showing all layers", exc_info=True)
                active_layer_ids = None
        # Index merges so we can flag layers that own a blend node.
        merge_owner_index = self._build_merge_owner_index(stack, layers_map,
                                                          controller.graph,
                                                          active_layer_ids)
        relations_by_id = {}
        relation_fn = getattr(controller, "layer_relationships", None)
        if callable(relation_fn):
            try:
                relations_by_id = {
                    str(item["layer_id"]): item
                    for item in relation_fn(stack.stack_id)
                    if isinstance(item, dict) and item.get("layer_id")
                }
            except (AttributeError, TypeError, ValueError):
                relations_by_id = {}

        def relation_view(layer_id: str) -> Dict[str, Any]:
            relation = relations_by_id.get(str(layer_id), {})
            badges = []
            parent_name = str(relation.get("parent_layer_name", "") or "")
            if relation.get("parent_layer_id"):
                badges.append("P")
            matte_id = relation.get("track_matte_layer_id")
            matte_name = str(
                relation.get("track_matte_layer_name", "") or "")
            if matte_id:
                mode = str(relation.get("track_matte_mode", "alpha"))
                mode_badge = "α" if mode == "alpha" else "L"
                badges.append("M" + mode_badge
                              + ("!" if relation.get("track_matte_invert")
                                 else ""))
            target_ids = tuple(
                relation.get("track_matte_target_layer_ids", ()) or ())
            if target_ids:
                badges.append("S" + str(len(target_ids)))
            valid = bool(relation.get("parent_valid", True)) \
                and bool(relation.get("track_matte_valid", True))
            tooltip_parts = []
            if parent_name:
                tooltip_parts.append("Parent: " + parent_name)
            if matte_id:
                matte_text = matte_name or str(matte_id)
                matte_text += " (" + str(
                    relation.get("track_matte_mode", "alpha")) + ")"
                if relation.get("track_matte_invert"):
                    matte_text += ", inverted"
                tooltip_parts.append("Track matte: " + matte_text)
            if target_ids:
                tooltip_parts.append(
                    "Matte source for " + str(len(target_ids)) + " layer(s)")
            return {
                "relation_badge": " ".join(badges),
                "relation_badge_state": "error" if not valid else (
                    "source" if target_ids and not matte_id else "normal"),
                "relation_tooltip": "; ".join(tooltip_parts),
                "parent_layer_name": parent_name,
            }

        display_idx = 0
        for layer_id in stack.layer_ids:
            if (active_layer_ids is not None
                    and layer_id not in active_layer_ids):
                continue
            layer = layers_map.get(layer_id)
            if layer is None:
                continue
            display_idx += 1
            if self._hide_shy_layers and layer.shy:
                continue
            relation = relation_view(layer_id)
            audio = layer.audio if layer.layer_type == LAYER_TYPE_AUDIO else None
            node = controller.graph.get_node(layer.root_node_id)
            if node is None:
                # Stale layer — still show it (broken state) so the UI can
                # offer repair; track is omitted.
                self._rows.append(KeyframesRow(
                    row_id=f"layer:{layer_id}",
                    node_id=layer.root_node_id,
                    param_name=None,
                    label=layer.name or layer_id,
                    row_type="layer",
                    depth=0,
                    expanded=True,
                    selected=(layer_id == selected_layer_id),
                    node_color=layer.label_color or "#555555",
                    layer_id=layer_id,
                    layer_index=display_idx,
                    layer_enabled=not audio.mute if audio is not None else layer.enabled,
                    layer_solo=audio.solo if audio is not None else layer.solo,
                    layer_shy=layer.shy,
                    layer_blended=layer_id in merge_owner_index,
                    layer_managed=layer.managed,
                    layer_type=layer.layer_type,
                    **relation,
                ))
                continue
            color = layer.label_color or self._node_color(node)
            expanded = self._layer_expansion.get(
                layer_id, legacy_stack or not layer.collapsed)
            layer_row = KeyframesRow(
                row_id=f"layer:{layer_id}",
                node_id=layer.root_node_id,
                param_name=None,
                label=layer.name or self._node_label(node),
                row_type="layer",
                depth=0,
                expanded=expanded,
                locked=layer.locked,
                selected=(layer_id == selected_layer_id),
                node_color=color,
                layer_id=layer_id,
                layer_index=display_idx,
                layer_enabled=not audio.mute if audio is not None else layer.enabled,
                layer_solo=audio.solo if audio is not None else layer.solo,
                layer_shy=layer.shy,
                layer_motion_blur=blur_states.get(layer_id),
                layer_blended=layer_id in merge_owner_index,
                layer_managed=layer.managed,
                layer_type=layer.layer_type,
                **relation,
            )
            self._rows.append(layer_row)
            # The layer bar's media length comes from the layer's video
            # source node (the bar itself lives on the root node).
            source_node = None
            if layer.source_node_id:
                try:
                    source_node = controller.graph.get_node(layer.source_node_id)
                except (AttributeError, KeyError, TypeError):
                    logger.debug("layer source node lookup failed for %s",
                                 layer.source_node_id, exc_info=True)
                    source_node = None
            self._tracks[layer_row.row_id] = self._build_node_track(
                node, color, source_node=source_node, layer=layer,
                comp_fps=getattr(controller.graph, "fps", 0))
            merge_for_layer = getattr(controller, "layer_merge_node", None)
            merge = merge_for_layer(layer_id) if callable(merge_for_layer) else None
            self._append_layer_groups(layer_row, layer, node, merge,
                                      self._layer_effect_nodes(controller, layer))
        self._filter_layer_rows()
        self._project_layer_property_focus(controller.graph)

    def focus_layer_properties(self, layer_ids: list[str], token: str, *,
                               additive: bool = False, force: bool = False) -> None:
        """Apply a temporary shortcut view to the specified layers."""
        requested = frozenset((token,))
        for layer_id in layer_ids:
            previous = self._layer_property_focus.get(layer_id, frozenset())
            if additive:
                focus = previous | requested if force else previous ^ requested
            else:
                focus = requested if force or previous != requested else frozenset()
            self._layer_property_focus[layer_id] = focus
            if previous != focus:
                self._focused_layer_expansion.pop(layer_id, None)

    def clear_layer_property_focus(self, layer_id: Optional[str] = None) -> bool:
        if layer_id is not None:
            self._focused_layer_expansion.pop(layer_id, None)
            return self._layer_property_focus.pop(layer_id, None) is not None
        had_focus = bool(self._layer_property_focus)
        self._layer_property_focus.clear()
        self._focused_layer_expansion.clear()
        return had_focus

    def _project_layer_property_focus(self, graph: Any) -> None:
        if not self._layer_property_focus:
            return
        headings = {row.row_id: row for row in self._rows if row.row_type != "param"}
        retained: set[tuple[str, Optional[str]]] = set()
        expanded: set[str] = set()
        flat_transform_layers = {layer_id for layer_id, tokens in self._layer_property_focus.items()
                                 if tokens and tokens <= LAYER_TRANSFORM_PROPERTIES.keys()}
        for row in self._rows:
            tokens = self._layer_property_focus.get(row.layer_id)
            if tokens is None or row.row_type == "layer":
                retained.add((row.row_id, row.parent_row_id))
                continue
            if not tokens:
                continue
            parents = []
            parent_id = row.parent_row_id
            while parent_id is not None:
                parent = headings[parent_id]
                parents.append(parent)
                parent_id = parent.parent_row_id
            effects = "effects" in tokens and any(
                item.row_type == "group" and item.row_id.endswith(":group:effects")
                for item in (row, *parents))
            layer_row_id = f"layer:{row.layer_id}"
            transform_row_id = f"{layer_row_id}:group:transform"
            layer_root = headings[layer_row_id].node_id
            focus_layer = graph.get_layer(row.layer_id) \
                if callable(getattr(graph, "get_layer", None)) else None
            layer_transform = getattr(focus_layer, "transform_node_id", None)
            # B01: transform properties live on the registered main
            # Transform, which may differ from the chain head.
            transform_property = (row.node_id == layer_root
                                  or row.node_id == layer_transform
                                  or (row.param_name == "opacity"
                                      and row.parent_row_id == transform_row_id))
            if not effects and not self._matches_layer_property_focus(
                    graph, row, tokens, transform_property=transform_property):
                continue
            if row.layer_id in flat_transform_layers and any(parent.row_id == transform_row_id for parent in parents):
                row.depth -= 1
                headings[transform_row_id].visible = False
            retained.add((row.row_id, row.parent_row_id))
            for parent in parents:
                retained.add((parent.row_id, parent.parent_row_id))
                # Paired values are readable without opening their axis rows.
                if not parent.component_params and (not effects or parent.row_type != "node"):
                    expanded.add(parent.row_id)
            if effects and row.row_id.endswith(":group:effects"):
                expanded.add(row.row_id)

        self._rows = [row for row in self._rows if (row.row_id, row.parent_row_id) in retained]
        for row in self._rows:
            if row.row_id in expanded:
                row.expanded = True
            elif row.row_type == "layer" and row.layer_id in self._layer_property_focus:
                row.expanded = False
            if row.layer_id in self._focused_layer_expansion:
                row.expanded = self._focused_layer_expansion[row.layer_id].get(row.row_id, row.expanded)
        retained_ids = {row.row_id for row in self._rows}
        self._tracks = {row_id: track for row_id, track in self._tracks.items()
                        if row_id in retained_ids}

    def _matches_layer_property_focus(self, graph: Any, row: KeyframesRow,
                                      tokens: frozenset[str], *, transform_property: bool) -> bool:
        if row.param_name is None:
            return False
        if transform_property and any(
                row.param_name in LAYER_TRANSFORM_PROPERTIES.get(token, ()) for token in tokens):
            return True
        modes = tokens.intersection(("animated", "modified", "expression"))
        if not modes:
            return False
        node = graph.get_node(row.node_id)
        if node is None:
            return False
        pdef = node.get_param_defs().get(row.param_name)
        return any(self._param_matches_filter(node, row.param_name, pdef, mode=mode)
                   for mode in modes)

    def _append_layer_groups(self, layer_row: KeyframesRow, layer: Any, root: Any,
                             merge: Any, effect_nodes: List[Any]) -> None:
        root_groups: Dict[str, List[Tuple[str, Any]]] = {}
        root_group = self._layer_node_group(root, layer)
        for name, pdef in self._filtered_params(root):
            group = root_group
            if group == "transform" or getattr(root, "supports_parent_transform", False):
                if name in _TRANSFORM_SAMPLING:
                    group = "advanced"
                elif (name in _TRANSFORM_PRIORITY or name.startswith(
                        ("translate_", "rotation_", "rotate_", "scale_", "pivot_"))):
                    group = "transform"
            root_groups.setdefault(group, []).append((name, pdef))
        opacity = next((item for item in self._filtered_params(merge) if item[0] == "opacity"),
                       None) if merge is not None else None

        node_groups: Dict[str, List[Tuple[Any, List[Tuple[str, Any]]]]] = {}
        for node in effect_nodes:
            params = self._filtered_params(node)
            if params:
                group = self._layer_node_group(node, layer)
                node_groups.setdefault(group, []).append((node, params))

        for group, title in _LAYER_GROUPS:
            root_params = root_groups.get(group, [])
            nodes = node_groups.get(group, [])
            merge_opacity = opacity if group == "transform" else None
            if not (root_params or nodes or merge_opacity):
                continue
            group_row = self._append_property_group(layer_row, root.id, group, title,
                                                     expanded=(group == "transform"))
            if group == "transform":
                self._append_transform_params(group_row, root, root_params)
            else:
                for name, pdef in root_params:
                    self._append_layer_param(group_row, root, name, pdef)
            if merge_opacity is not None:
                self._append_layer_param(group_row, merge, *merge_opacity)
            for node, params in nodes:
                row_id = f"{layer_row.row_id}:node:{node.id}"
                node_row = KeyframesRow(
                    row_id=row_id, node_id=node.id, param_name=None,
                    label=self._node_label(node), row_type="node", depth=group_row.depth + 1,
                    expanded=self._layer_row_expansion.get(row_id, False),
                    locked=group_row.locked,
                    node_color=layer.label_color or self._node_color(node),
                    parent_row_id=group_row.row_id, layer_id=layer_row.layer_id,
                )
                self._rows.append(node_row)
                track = self._build_node_track(node, node_row.node_color)
                if track.range_bar is not None and track.range_bar.default:
                    track.range_bar = None
                track.row_id = row_id
                self._tracks[row_id] = track
                if group == "transform":
                    self._append_transform_params(node_row, node, params)
                else:
                    for name, pdef in params:
                        self._append_layer_param(node_row, node, name, pdef)

    @staticmethod
    def _layer_node_group(node: Any, layer: Any) -> str:
        node_type = getattr(node, "node_type", "")
        if node_type in _MASK_NODE_TYPES:
            return "masks"
        if node.id == getattr(layer, "transform_node_id", None):
            # B01: the registered main Transform belongs to the transform
            # group even when it is not the chain head.
            return "transform"
        if node_type == "transform" or node_type == "transform3d":
            return "transform"
        if (node.id == layer.source_node_id or node_type in _MEDIA_NODE_TYPES
                or node_type in {"text", "constant", "model3d"}):
            return "content"
        return "effects"

    def _append_property_group(self, parent: KeyframesRow, node_id: str,
                               group: str, title: str, *, expanded: bool = False,
                               components: tuple[str, ...] = ()) -> KeyframesRow:
        row_id = f"{parent.row_id}:{node_id}:{group}" if components else f"{parent.row_id}:group:{group}"
        row = KeyframesRow(
            row_id=row_id, node_id=node_id, param_name=None,
            label=I18N.tr(f"keyframes.group_{group}", title), row_type="group",
            depth=parent.depth + 1, expanded=self._layer_row_expansion.get(row_id, expanded),
            locked=parent.locked, node_color=parent.node_color, parent_row_id=parent.row_id,
            layer_id=parent.layer_id, component_params=components,
        )
        if components:
            row.expanded = row_id in self._split_dimensions
            row.visible = not row.expanded
        self._rows.append(row)
        return row

    def _append_transform_params(self, parent: KeyframesRow, node: Any,
                                 params: List[Tuple[str, Any]]) -> None:
        remaining = dict(params)
        definitions = node.get_param_defs()
        for group, title, pair in _COMPONENT_GROUPS:
            names = [name for name in pair if name in remaining]
            if not names:
                continue
            pair_parent = parent
            if all(name in definitions and definitions[name].is_animatable() for name in pair):
                pair_parent = self._append_property_group(parent, node.id, group, title,
                                                          components=pair)
            for name in names:
                self._append_layer_param(pair_parent, node, name, remaining.pop(name))
        for name in _TRANSFORM_PRIORITY:
            if name in remaining:
                self._append_layer_param(parent, node, name, remaining.pop(name))
        for name, pdef in remaining.items():
            self._append_layer_param(parent, node, name, pdef)

    def _append_layer_param(self, parent: KeyframesRow, node: Any,
                            name: str, pdef: Any) -> None:
        label = self._param_label(node, name, pdef)
        translated = _LAYER_PROPERTY_LABELS.get(name)
        if parent.component_params:
            axis = name.rsplit("_", 1)[-1].upper()
            label = f"{parent.label} {axis}"
        elif translated:
            label = I18N.tr(*translated)
        row = KeyframesRow(
            row_id=f"param:{node.id}:{name}", node_id=node.id, param_name=name,
            label=label, row_type="param",
            depth=parent.depth + (0 if parent.component_params else 1),
            locked=parent.locked, node_color=parent.node_color,
            parent_row_id=parent.row_id, layer_id=parent.layer_id,
        )
        self._rows.append(row)
        self._tracks[row.row_id] = self._build_param_track(node, name, row.node_color)

    def _filter_layer_rows(self) -> None:
        query = self._text_filter.strip().casefold()
        filters_properties = self._filter in ("animated", "modified", "expression")
        if not query and not filters_properties:
            return
        headings = {row.row_id: row for row in self._rows if row.row_type != "param"}
        text_matches: Dict[str, bool] = {}
        retained_headings: set[str] = set()
        visible_parents: set[str] = set()
        retained_params: set[tuple[str, Optional[str]]] = set()
        for row in self._rows:
            matches = not query or query in row.label.casefold()
            if row.param_name is not None:
                matches = matches or query in row.param_name.casefold()
            elif row.row_type == "group":
                matches = matches or query in row.row_id.rsplit(":", 1)[-1]
            matches = matches or text_matches.get(row.parent_row_id or "", False)
            if row.row_type != "param":
                text_matches[row.row_id] = matches
                if filters_properties or not matches:
                    continue
                retained_headings.add(row.row_id)
            elif matches:
                retained_params.add((row.row_id, row.parent_row_id))
            else:
                continue
            parent_id = row.parent_row_id
            while parent_id and parent_id not in visible_parents:
                retained_headings.add(parent_id)
                visible_parents.add(parent_id)
                parent_id = headings[parent_id].parent_row_id

        self._rows = [row for row in self._rows if row.row_id in retained_headings
                      or (row.row_id, row.parent_row_id) in retained_params]
        for row in self._rows:
            if row.row_id in visible_parents and not row.component_params:
                row.expanded = True
        retained_ids = {row.row_id for row in self._rows}
        self._tracks = {row_id: track for row_id, track in self._tracks.items()
                        if row_id in retained_ids}

    @staticmethod
    def _layer_effect_nodes(controller: Any, layer: Any) -> List[Any]:
        """Return live nodes upstream of a layer root for Layer mode.

        This is intentionally a projection, not a second graph model.  It
        follows every image input so a Mask/Roto connected to a side mask
        port is still visible under the layer.  Generated Merge nodes and a
        layer's declared source are omitted because they are structural or
        already represented by the Layer row.
        """
        graph = getattr(controller, "graph", None)
        if graph is None or layer is None:
            return []
        generated = set()
        stack = None
        try:
            stack = controller.main_stack()
            generated = set(getattr(stack, "generated_merge_node_ids", []))
        except (AttributeError, TypeError):
            logger.debug("main_stack/generated merge lookup failed",
                         exc_info=True)
        source_id = getattr(layer, "source_node_id", None)
        # Generated content and 3D sources own editable properties separate
        # from the layer Transform or Scene3D root.
        include_source = getattr(layer, "layer_type", "") in {
            LAYER_TYPE_3D, LAYER_TYPE_SOLID, LAYER_TYPE_TEXT,
        }
        excluded_types = {"output", "merge"}
        visited: set[str] = set()
        ordered: List[Any] = []

        def visit(node_id: str) -> None:
            if not node_id or node_id in visited:
                return
            visited.add(node_id)
            node = graph.get_node(node_id)
            if node is None:
                return
            incoming = [c for c in getattr(graph, "connections", [])
                        if c.dst_node_id == node_id]
            incoming.sort(key=lambda c: (str(c.dst_port_name),
                                         str(c.src_node_id)))
            for conn in incoming:
                visit(conn.src_node_id)
            node_type = getattr(node, "node_type", "")
            if ((node_id != source_id or include_source or node_type in _MEDIA_NODE_TYPES)
                    and node_id not in generated
                    and node_type not in excluded_types
                    and node_id != getattr(layer, "root_node_id", None)):
                ordered.append(node)

        visit(getattr(layer, "root_node_id", ""))
        return ordered

    def layer_keyframe_frames(self, controller: Any, layer: Any) -> set[int]:
        """Return authored points across one layer, independent of disclosure."""
        root = controller.graph.get_node(layer.root_node_id)
        merge = controller.layer_merge_node(layer.layer_id)
        nodes = (root, merge, *self._layer_effect_nodes(controller, layer))
        return {point.frame for node in nodes if node is not None
                for name in node.keyframe_tracks
                for point in self.track_for_property_edit(node, name).keyframes}

    def _build_merge_owner_index(self, stack: Any, layers_map: dict,
                                 graph: Any,
                                 active_layer_ids: Optional[set] = None) -> set:
        """Return the set of layer_ids that own an auto-merge.

        A layer "owns" the merge whose foreground input is the layer's
        contribution.  For ``[A, B, C]`` with merges ``[M0, M1]``: A owns
        M0, B owns M1, C owns none.  See layer_stack_compiler for the full
        mapping; here we reconstruct it from the live connections so the
        UI stays correct even after manual rewires.
        """
        merges = stack.generated_merge_node_ids if stack else []
        if stack and getattr(stack, "version", LEGACY_LAYER_STACK_VERSION) >= BOTTOM_UP_LAYER_STACK_VERSION:
            return {node.parameters[LAYER_MERGE_OWNER_PARAM]
                    for merge_id in merges
                    if (node := graph.get_node(merge_id)) is not None
                    and node.parameters.get(LAYER_MERGE_OWNER_PARAM)}
        # Merge ids are ordered against the *live* layer list.  Reconstructing
        # ownership from the A connection fails for M1+ because its A input
        # is the previous Merge, not a layer root.  The ordered layer list is
        # the authoritative mapping and also handles disabled/solo layers.
        live_layers = [layers_map.get(lid)
                       for lid in (stack.layer_ids if stack else [])]
        live_layers = [layer for layer in live_layers
                       if (layer is not None and layer.enabled
                           and (active_layer_ids is None
                                or layer.layer_id in active_layer_ids))]
        if any(layer.solo for layer in live_layers):
            live_layers = [layer for layer in live_layers if layer.solo]
        return {
            layer.layer_id
            for index, layer in enumerate(live_layers[:len(merges)])
            if graph.get_node(merges[index]) is not None
        }

    # ── rebuild from graph ──

    def rebuild(self, node_graph: Any,
                selected_node_id: Optional[str] = None) -> None:
        """Regenerate rows + tracks from ``node_graph``.

        Node ordering follows the graph's editor position (top-to-bottom,
        left-to-right) to match what the user sees on canvas (§P2.1).
        """
        self._rows.clear()
        self._tracks.clear()
        self._selected_node_id = selected_node_id

        nodes = self._ordered_nodes(node_graph)
        for node in nodes:
            color = self._node_color(node)
            node_id = node.id
            node_label = self._node_label(node)
            query = self._text_filter.strip().casefold()
            params = self._filtered_params(node)
            if query and query not in node_label.casefold():
                params = self._filtered_params(node, query=query)
                if not params:
                    continue
            if (self._filter in ("animated", "modified", "expression")
                    and not params):
                continue
            if self._filter == "selected":
                has_selected_track = any(
                    any(bool(getattr(kf, "selected", False))
                        for kf in getattr(track, "keyframes", []))
                    for track in getattr(node, "keyframe_tracks", {}).values()
                )
                if node_id != selected_node_id and not has_selected_track:
                    continue
            expanded = (node_id in self._expanded) if self._expanded else True
            node_row = KeyframesRow(
                row_id=f"node:{node_id}",
                node_id=node_id,
                param_name=None,
                label=node_label,
                row_type="node",
                depth=0,
                expanded=expanded,
                selected=(node_id == selected_node_id),
                node_color=color,
            )
            self._rows.append(node_row)

            # node-level range bar + track visuals
            self._tracks[node_row.row_id] = self._build_node_track(node, color)

            # child parameter rows (only animatable + existing tracks)
            for pname, pdef in params:
                animatable = bool(getattr(pdef, "is_animatable", lambda: False)())
                has_track = self._has_param_track(node, pname, pdef)
                if self._filter == "selected":
                    tracks = self._tracks_for_param(node, pname, pdef)
                    if not any(
                            bool(getattr(kf, "selected", False))
                            for track in tracks
                            for kf in getattr(track, "keyframes", [])):
                        continue
                if not animatable and not has_track:
                    continue
                param_row = KeyframesRow(
                    row_id=f"param:{node_id}:{pname}",
                    node_id=node_id,
                    param_name=pname,
                    label=self._param_label(node, pname, pdef),
                    row_type="param",
                    depth=1,
                    expanded=True,
                    selected=False,
                    visible=expanded,
                    node_color=color,
                )
                self._rows.append(param_row)
                self._tracks[param_row.row_id] = self._build_param_track(
                    node, pname, color)

    # ── accessors ──

    @property
    def rows(self) -> List[KeyframesRow]:
        return self._rows

    def visible_rows(self) -> List[KeyframesRow]:
        """Rows that should be painted given expansion + visibility."""
        out: List[KeyframesRow] = []
        expanded: Dict[str, bool] = {}
        for row in self._rows:
            parent_visible = expanded.get(row.parent_row_id, True) if row.parent_row_id else True
            if row.row_type in ("node", "layer", "group"):
                expanded[row.row_id] = parent_visible and row.expanded
            if not parent_visible:
                continue
            if not row.visible:
                continue
            out.append(row)
        return out

    def track_for(self, row_id: str) -> Optional[KeyframesTrackVisual]:
        return self._tracks.get(row_id)

    def track_for_property_edit(self, node: Any, param_name: str) -> KeyframesTrackVisual:
        """Provide data for direct property edits, including filtered-out tracks."""
        track = self.track_for(f"param:{node.id}:{param_name}")
        return track if track is not None else self._build_param_track(
            node, param_name, self._node_color(node))

    def tracks(self) -> Dict[str, KeyframesTrackVisual]:
        return dict(self._tracks)

    # ── composition range (default layer bars) ──

    def set_comp_range(self, start_frame: int, end_frame: int) -> None:
        """Store the comp frame range and refit all default layer bars."""
        self._comp_range = (int(start_frame), int(end_frame))
        for tr in self._tracks.values():
            if (tr.range_bar is not None and tr.range_bar.default
                    and not tr.source_total):
                tr.range_bar.start_frame = int(start_frame)
                tr.range_bar.end_frame = int(end_frame)

    # ── expansion / filtering / selection ──

    def set_expanded(self, node_id: str, expanded: bool) -> None:
        layer_rows = [row for row in self._rows if row.layer_id is not None
                      and row.row_type in ("node", "layer") and row.node_id == node_id]
        if layer_rows:
            for row in layer_rows:
                self._set_projected_expansion(row, expanded)
            return
        if expanded:
            self._expanded.add(node_id)
        else:
            self._expanded.discard(node_id)
        for row in self._rows:
            if row.row_type == "node" and row.node_id == node_id:
                row.expanded = expanded
            elif row.row_type == "param" and row.node_id == node_id and row.layer_id is None:
                row.visible = expanded

    def _set_projected_expansion(self, row: KeyframesRow, expanded: bool) -> None:
        row.expanded = expanded
        if row.layer_id is not None and row.layer_id in self._layer_property_focus:
            self._focused_layer_expansion.setdefault(row.layer_id, {})[row.row_id] = expanded
        elif row.row_type == "layer" and row.layer_id is not None:
            self._layer_expansion[row.layer_id] = expanded
        else:
            self._layer_row_expansion[row.row_id] = expanded

    def set_row_expanded(self, row_id: str, expanded: bool, *,
                         recursive: bool = False) -> bool:
        """Expand a heading by its view identity, optionally including descendants."""
        row = next((item for item in self._rows if item.row_id == row_id), None)
        if row is None or row.component_params or row.row_type not in ("node", "layer", "group"):
            return False
        if row.layer_id is None:
            self.set_expanded(row.node_id, expanded)
            return True
        self._set_projected_expansion(row, expanded)
        if recursive:
            # The layer projection emits parents before descendants, including
            # rows currently hidden by collapsed headings.
            headings = {row_id}
            for child in self._rows:
                if (child.parent_row_id in headings and child.row_type != "param"
                        and not child.component_params):
                    self._set_projected_expansion(child, expanded)
                    headings.add(child.row_id)
        return True

    def reveal_row(self, row_id: str) -> bool:
        """Expand the target's ancestors so the row can be selected or edited."""
        layer_id = next((row.layer_id for row in self._rows
                         if row.row_type == "layer" and row.selected), None)
        candidates = [row for row in self._rows if row.row_id == row_id]
        if not candidates:
            return False
        row = next((item for item in candidates if item.layer_id == layer_id), candidates[0])
        if row.layer_id is None and row.row_type == "param":
            self.set_expanded(row.node_id, True)
            return True
        headings = {item.row_id: item for item in self._rows if item.row_type != "param"}
        parent_id = row.parent_row_id
        while parent_id:
            parent = headings[parent_id]
            if not parent.component_params:
                self._set_projected_expansion(parent, True)
            parent_id = parent.parent_row_id
        return True

    def expand_all(self, expanded: bool = True) -> None:
        for row in self._rows:
            if row.layer_id is not None:
                if row.row_type != "param" and not row.component_params:
                    self._set_projected_expansion(row, expanded)
            elif row.row_type == "node":
                row.expanded = expanded
                if expanded:
                    self._expanded.add(row.node_id)
                else:
                    self._expanded.discard(row.node_id)
            elif row.row_type == "param":
                row.visible = expanded

    def dimension_group(self, row: KeyframesRow) -> Optional[KeyframesRow]:
        """Find the vector summary for a combined row or one of its axes."""
        if row.component_params:
            return row
        if row.row_type != "param" or row.parent_row_id is None:
            return None
        return next((group for group in self._rows
                     if group.row_id == row.parent_row_id and group.component_params), None)

    def toggle_dimensions(self, row_id: str) -> None:
        """Toggle vector presentation without altering the component tracks."""
        if row_id in self._split_dimensions:
            self._split_dimensions.remove(row_id)
        else:
            self._split_dimensions.add(row_id)

    def set_filter(self, mode: str) -> None:
        """Set the property filter used by both node and layer projections.

        ``modified`` includes authored values, keyframe tracks and
        expressions; ``expression`` narrows the result to properties with an
        expression, while ``animated`` remains track-only.
        """
        allowed = ("all", "animated", "modified", "expression", "selected")
        self._filter = mode if mode in allowed else "all"

    @property
    def filter_mode(self) -> str:
        return self._filter

    def set_text_filter(self, text: str) -> None:
        """Set a case-insensitive property search string.

        Rebuilding is intentionally left to the panel, which owns the live
        graph reference. Keeping the query in the model means the same
        filter can later be serialized or shared with the Graph Editor.
        """
        self._text_filter = str(text or "")

    @property
    def text_filter(self) -> str:
        return self._text_filter

    def set_selected_node(self, node_id: Optional[str]) -> None:
        self._selected_node_id = node_id
        for row in self._rows:
            if row.row_type in ("node", "layer"):
                row.selected = (row.node_id == node_id)

    # ── helpers ──

    @classmethod
    def _has_expression(cls, node: Any, param_name: str) -> bool:
        """Return whether a node has an expression on ``param_name``."""
        getter = getattr(node, "has_expression", None)
        if callable(getter):
            try:
                return bool(getter(param_name))
            except (AttributeError, KeyError, TypeError, ValueError):
                pass
        getter = getattr(node, "get_expression", None)
        if callable(getter):
            try:
                return bool(getter(param_name))
            except (AttributeError, KeyError, TypeError, ValueError):
                pass
        expressions = getattr(node, "_expressions", {}) or {}
        value = expressions.get(param_name)
        if isinstance(value, dict):
            return bool(value.get("text") or value.get("expression"))
        return bool(value)

    @staticmethod
    def _values_equal(left: Any, right: Any) -> bool:
        """Compare scalar/vector parameter values without numpy truth errors."""
        try:
            result = left == right
        except (TypeError, ValueError):
            return False
        if isinstance(result, bool):
            return result
        try:
            return bool(result.all())
        except (AttributeError, TypeError, ValueError):
            try:
                return bool(result)
            except (TypeError, ValueError):
                return False

    @classmethod
    def _is_modified_param(cls, node: Any, param_name: str,
                           pdef: Any, has_track: bool) -> bool:
        if has_track or cls._has_expression(node, param_name):
            return True
        if pdef is None:
            return False
        params = getattr(node, "parameters", {}) or {}
        if param_name not in params:
            return False
        return not cls._values_equal(params.get(param_name),
                                     getattr(pdef, "default", None))

    def _param_matches_filter(self, node: Any, param_name: str,
                              pdef: Any, *, mode: Optional[str] = None) -> bool:
        mode = self._filter if mode is None else mode
        has_track = self._has_param_track(node, param_name, pdef)
        if mode == "animated":
            return has_track
        if mode == "modified":
            return self._is_modified_param(node, param_name, pdef, has_track)
        if mode == "expression":
            return self._has_expression(node, param_name)
        if mode == "selected":
            return any(
                bool(getattr(kf, "selected", False))
                for track in self._tracks_for_param(node, param_name, pdef)
                for kf in getattr(track, "keyframes", [])
            )
        return True

    def _filtered_params(self, node: Any, query: str = "") -> List[Tuple[str, Any]]:
        """Return animatable/legacy parameters matching current filters."""
        result = []
        node_label = self._node_label(node).casefold()
        for pname, pdef in self._animatable_params(node):
            if query and query not in node_label and not (
                    query in self._param_label(node, pname, pdef).casefold()
                    or query in pname.casefold()):
                continue
            animatable = bool(getattr(
                pdef, "is_animatable", lambda: False)())
            has_track = self._has_param_track(node, pname, pdef)
            if not animatable and not has_track:
                continue
            if not self._param_matches_filter(node, pname, pdef):
                continue
            result.append((pname, pdef))
        return result

    @staticmethod
    def _ordered_nodes(node_graph: Any) -> List[Any]:
        try:
            items = list(node_graph.nodes.values())
        except (AttributeError, TypeError):
            logger.debug("node graph has no readable nodes mapping",
                         exc_info=True)
            return []
        # Sort by canvas position: y then x (top-to-bottom, left-to-right).
        def _key(n):
            x, y = getattr(n, "position", (0.0, 0.0)) or (0.0, 0.0)
            return (float(y), float(x))
        try:
            items.sort(key=_key)
        except (AttributeError, TypeError, ValueError):
            logger.debug("node ordering by canvas position failed; keeping "
                         "graph order", exc_info=True)
        return items

    @staticmethod
    def _node_color(node: Any) -> str:
        try:
            return node.effective_header_color()
        except (AttributeError, TypeError, ValueError):
            return getattr(node, "header_color", "#555555") or "#555555"

    @staticmethod
    def _node_label(node: Any) -> str:
        # The UUID-like node id is an internal graph key, not useful content
        # for a normal user. Keep it in row_id/node_id for data operations,
        # but show only the friendly node title in the panel.
        try:
            title = node.display_title
        except (AttributeError, TypeError, ValueError):
            title = getattr(node, "node_title", node.node_type)
        return str(title or getattr(node, "node_type", "Node"))

    @staticmethod
    def _animatable_params(node: Any):
        # Constant owns mode-dependent controls.  Use the same source of
        # truth as the Properties panel, including its hidden atomic color
        # parameters, while omitting inactive mode controls.
        if getattr(node, "node_type", "") == "constant":
            try:
                from src.features.composite.nodes.constant_node import ConstantNode
                param_defs = node.get_param_defs()
                result = []
                mode = str(getattr(node, "parameters", {}).get("mode", "solid"))
                for pname, _ in ConstantNode.keyframe_parameter_specs(mode):
                    pdef = param_defs.get(pname)
                    if pdef is not None:
                        result.append((pname, pdef))
                return result
            except (AttributeError, ImportError, KeyError, TypeError,
                    ValueError):
                logger.debug("constant node param specs failed; falling back "
                             "to generic params", exc_info=True)

        out = []
        existing = set()
        try:
            for pdef in node.get_param_defs().values():
                # Hidden numeric parameters are implementation metadata (for
                # example a video's decoded width/fps), not user animation
                # controls. Keep them out of the Keyframes panel even though
                # their storage type is numeric.
                if (not getattr(pdef, "hidden", False)
                        and getattr(pdef, "is_animatable", lambda: False)()):
                    out.append((pdef.name, pdef))
                    existing.add(pdef.name)
        except (AttributeError, TypeError, ValueError):
            logger.debug("node param defs unreadable for %s",
                         getattr(node, "id", "?"), exc_info=True)

        # Also include params that have tracks but aren't in the node's
        # instance-aware definitions (for example an orphaned legacy track).
        try:
            existing.update(node.get_param_defs())
        except (AttributeError, TypeError):
            logger.debug("node param defs unavailable for existing-name "
                         "index", exc_info=True)
        for pname in getattr(node, "keyframe_tracks", {}):
            if pname not in existing:
                out.append((pname, None))
        return out

    @staticmethod
    def _param_label(node: Any, param_name: str, pdef: Any) -> str:
        if getattr(node, "node_type", "") == "constant":
            if param_name == "color":
                return "Color"
            if param_name == "color2":
                return "Color 2"
        # Keep the Layer view in the same language as the Properties panel.
        # Parameter names are persisted in English, so localization belongs
        # at this display boundary rather than in the graph/model data.
        try:
            from src.shared.i18n.i18n import tr_or_none
            node_type = str(getattr(node, "node_type", "") or "")
            translated = tr_or_none(
                f"nodes.{node_type}.params.{param_name}")
            if translated:
                return translated
        except (AttributeError, ImportError, KeyError, TypeError,
                ValueError):
            logger.debug("param label localization failed for %s/%s",
                         getattr(node, "node_type", "?"), param_name,
                         exc_info=True)
        if pdef is not None:
            # Use the param name directly; the panel can localize later.
            return param_name.replace("_", " ").title()
        return param_name.replace("_", " ").title()

    @staticmethod
    def _tracks_for_param(node: Any, param_name: str,
                          pdef: Any = None) -> List[Any]:
        """Return the single authoritative track for one visible property."""
        track = getattr(node, "keyframe_tracks", {}).get(param_name)
        return [track] if track is not None else []

    @classmethod
    def _has_param_track(cls, node: Any, param_name: str,
                         pdef: Any = None) -> bool:
        return any(
            isinstance(track, dict) and bool(track.get("keyframes"))
            for track in cls._tracks_for_param(node, param_name, pdef)
        )

    @staticmethod
    def _media_total_frames(node: Any) -> int:
        """Source length (frames) when ``node`` is a video media node."""
        if getattr(node, "node_type", "") != "video_frame":
            return 0
        try:
            total = int(node.parameters.get("total_frames", 0) or 0)
        except (TypeError, ValueError):
            return 0
        return max(0, total)

    def _build_node_track(self, node: Any, color: str,
                          source_node: Any = None,
                          layer: Any = None, comp_fps: Any = 0) -> KeyframesTrackVisual:
        tr = getattr(node, "time_range", None)
        range_bar: Optional[TimeRangeBar] = None
        label = layer.name if layer is not None else self._node_label(node)
        # Layer rows carry their bar on the root (Transform) node, but the
        # media length lives on the layer's video source node.
        media_node = source_node if source_node is not None else node
        source_total = self._media_total_frames(media_node)
        source_in = 0
        source_out = source_total - 1
        media_timing = None
        if layer is not None and getattr(media_node, "node_type", "") == "video_frame":
            from src.shared.media.presentation_time import ready_presentation_index

            media_range = getattr(media_node, "time_range", None) or {}
            params = media_node.parameters
            index = ready_presentation_index(str(params.get("file_path") or ""))
            if index is not None:
                source_total = len(index.pts)
                source_out = source_total - 1
            media_timing = layer_timing_from_video((0, 0), params, media_range, comp_fps, index)
        if source_total:
            try:
                source_in = max(0, min(source_out, int(
                    media_node.parameters.get("source_in", 0))))
                media_out = int(media_node.parameters.get("source_out", -1))
                if media_out >= 0:
                    source_out = max(source_in, min(source_out, media_out))
            except (TypeError, ValueError):
                pass
            if (layer is not None and not isinstance(tr, dict)
                    and media_node is not node):
                tr = getattr(media_node, "time_range", None)
        if isinstance(tr, dict) and tr.get("enabled", True):
            start = int(tr.get("comp_start", tr.get("source_in", 0)) or 0)
            end = int(tr.get("comp_end", tr.get("source_out", 0)) or 0)
            range_bar = TimeRangeBar(
                start_frame=start, end_frame=end,
                label=label, color=color)
        else:
            # AE-style layer bar: every material/node row shows a bar
            # spanning the whole composition until the user trims it.
            # Video media shows its full source length instead, so material
            # longer than the composition stays visible and trimmable.
            if source_total > 0:
                duration = (media_timing.window_duration(source_out) if media_timing is not None
                            else source_out - source_in + 1)
                cs, ce = 0, duration - 1
            else:
                cs, ce = self._comp_range
            if ce >= cs:
                range_bar = TimeRangeBar(
                    start_frame=cs, end_frame=ce,
                    label=label, color=color, default=True)
        if (layer is not None and
                (layer.in_frame is not None or layer.out_frame is not None)):
            base_start, base_end = self._comp_range
            if range_bar is not None:
                base_start, base_end = range_bar.start_frame, range_bar.end_frame
            start = layer.in_frame if layer.in_frame is not None else base_start
            end = layer.out_frame if layer.out_frame is not None else base_end
            range_bar = TimeRangeBar(
                start_frame=start, end_frame=max(start, end),
                label=layer.name, color=color)
        if media_timing is not None and range_bar is not None:
            media_timing = replace(media_timing, in_frame=range_bar.start_frame,
                                   out_frame=range_bar.end_frame)
        return KeyframesTrackVisual(
            row_id=f"node:{node.id}",
            range_bar=range_bar, color=color, node_color=color,
            source_total=source_total, source_in=source_in, media_timing=media_timing,
            source_step=media_timing.source_step if media_timing else Fraction(1),
            source_phase=media_timing.source_phase if media_timing else Fraction(0),
            layer_managed=layer is None or layer.managed)

    def _build_param_track(self, node: Any, param_name: str,
                           node_color: str) -> KeyframesTrackVisual:
        track_data = node.keyframe_tracks.get(param_name)
        points: List[KeyframePointVisual] = []
        if isinstance(track_data, dict):
            for kf in track_data.get("keyframes", []):
                try:
                    # P1-1: preserve unknown fields (handle_left/right,
                    # tangent_mode, bezier_in/out) so moving a keyframe in the
                    # Keyframes panel doesn't drop the user's bezier handles.
                    extra = {k: v for k, v in kf.items()
                             if k not in ("frame", "value", "interpolation")}
                    raw_value = kf.get("value", 0.0)
                    # Keep the declared value domain intact. The timeline
                    # only needs the frame/selection for drawing; coercing
                    # option/bool/vector values to float loses edit fidelity.
                    value = raw_value
                    points.append(KeyframePointVisual(
                        frame=int(kf.get("frame", 0)),
                        value=value,
                        interpolation=kf.get("interpolation", "linear"),
                        extra=extra,
                    ))
                except (TypeError, ValueError):
                    continue
        curve_color = _channel_color_for(param_name, node_color)
        return KeyframesTrackVisual(
            row_id=f"param:{node.id}:{param_name}",
            range_bar=None,
            keyframes=points,
            color=curve_color,
            node_color=node_color)
