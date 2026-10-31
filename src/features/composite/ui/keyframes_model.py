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
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from src.features.composite.core.layer_model import LAYER_TYPE_3D

logger = logging.getLogger(__name__)


# Both views use the same row geometry. Keeping these values in one place is
# important: the tree side has to reserve the ruler's header row or its first
# item will be painted on top of the timeline ruler.
KEYFRAMES_RULER_H = 30
KEYFRAMES_ROW_H = 24


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
    # Video media length in frames behind this row's bar (0 = not a video
    # media row). The timeline view clamps bar drags to [0, source_total-1].
    source_total: int = 0


@dataclass
class KeyframesRow:
    """One row in the left tree (node, parameter, or layer)."""
    row_id: str
    node_id: str
    param_name: Optional[str]        # None for node rows
    label: str
    row_type: str                    # "node" | "param" | "layer"
    depth: int                       # 0 = node/layer, 1 = param
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
    # Populated only for ``row_type == "layer"``.  ``layer_id`` keys back to
    # the LayerStack's layer; ``layer_index`` is the 1-based display number
    # shown in the row (top layer = 1).  These stay None for node/param rows
    # so the existing drawing code is unaffected.
    layer_id: Optional[str] = None
    layer_index: Optional[int] = None
    layer_enabled: bool = True        # AE eye toggle state
    layer_solo: bool = False
    layer_shy: bool = False
    layer_blended: bool = True        # False for layers w/o owning merge
    layer_type: str = "media"
    # ── Layer relation diagnostics (P1-04) ──
    relation_badge: str = ""
    relation_badge_state: str = "normal"
    relation_tooltip: str = ""


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

    def set_zoom(self, pixels_per_frame: float) -> None:
        # Clamp both ends: 0.05 px/frame keeps huge comps navigable,
        # 256 px/frame gives comfortable frame-by-frame scrubbing.
        self.pixels_per_frame = max(0.05, min(256.0, float(pixels_per_frame)))

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
        self._filter: str = "all"           # all | animated | modified | expression | selected
        self._text_filter: str = ""
        self._selected_node_id: Optional[str] = None
        # Composition frame range — used for AE-style default layer bars
        # (nodes without an explicit time_range span the whole comp).
        self._comp_range: Tuple[int, int] = (0, 0)
        # AE/Nuke hybrid view mode (plan §6.1).  "nodes" = legacy behaviour;
        # "layers" = AE layer stack with one row per layer.
        self._view_mode: str = "nodes"

    # ── view mode (AE/Nuke hybrid, plan §6.1) ──

    VIEW_MODE_NODES = "nodes"
    VIEW_MODE_LAYERS = "layers"

    @property
    def view_mode(self) -> str:
        return self._view_mode

    def set_view_mode(self, mode: str) -> None:
        if mode in (self.VIEW_MODE_NODES, self.VIEW_MODE_LAYERS):
            self._view_mode = mode

    def rebuild_layers(self, controller: Any,
                       selected_layer_id: Optional[str] = None) -> None:
        """Regenerate rows from a :class:`LayerStackController`.

        Produces one ``layer`` row per layer (foreground-first, i.e. the
        stack's ``layer_ids[0]`` is drawn at the top), followed by that
        layer's root-node parameter rows indented underneath — mirroring
        AE's layer/property tree.  Layer ordering is authoritative here;
        node canvas position is ignored.  Track visuals reuse the existing
        :meth:`_build_node_track` so the AE-style default bar still appears.
        """
        self._rows.clear()
        self._tracks.clear()
        self._selected_node_id = None
        if controller is None:
            return
        stack = controller.main_stack()
        if stack is None:
            return
        layers_map = controller.layers_map()
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
            relation = relation_view(layer_id)
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
                    layer_enabled=layer.enabled,
                    layer_solo=layer.solo,
                    layer_shy=layer.shy,
                    layer_blended=layer_id in merge_owner_index,
                    layer_type=layer.layer_type,
                    **relation,
                ))
                continue
            color = layer.label_color or self._node_color(node)
            effect_nodes = self._layer_effect_nodes(controller, layer)
            if self._filter in ("animated", "modified", "expression"):
                effect_nodes = [
                    effect_node for effect_node in effect_nodes
                    if self._node_has_matching_params(effect_node)
                ]
                if (not self._node_has_matching_params(node)
                        and not effect_nodes):
                    continue
            expanded = (layer.root_node_id in self._expanded) if \
                self._expanded else True
            layer_row = KeyframesRow(
                row_id=f"layer:{layer_id}",
                node_id=layer.root_node_id,
                param_name=None,
                label=layer.name or self._node_label(node),
                row_type="layer",
                depth=0,
                expanded=expanded,
                selected=(layer_id == selected_layer_id),
                node_color=color,
                layer_id=layer_id,
                layer_index=display_idx,
                layer_enabled=layer.enabled,
                layer_solo=layer.solo,
                layer_shy=layer.shy,
                layer_blended=layer_id in merge_owner_index,
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
                node, color, source_node=source_node)
            # The root's properties remain directly under the Layer row.
            # Additional upstream/side-chain nodes (including Mask/Roto) are
            # projected as child node rows.  Every row points at the live
            # node; no Layer-owned parameter copy is created.
            self._append_layer_node_params(
                node, color, parent_row_id=layer_row.row_id,
                depth=1, visible=expanded)
            for effect_node in effect_nodes:
                if effect_node.id == layer.root_node_id:
                    continue
                effect_color = layer.label_color or self._node_color(effect_node)
                effect_row = KeyframesRow(
                    row_id=f"node:{effect_node.id}",
                    node_id=effect_node.id,
                    param_name=None,
                    label=self._node_label(effect_node),
                    row_type="node",
                    depth=1,
                    expanded=(effect_node.id in self._expanded)
                    if self._expanded else True,
                    selected=(effect_node.id == self._selected_node_id),
                    node_color=effect_color,
                    parent_row_id=layer_row.row_id,
                )
                self._rows.append(effect_row)
                self._tracks[effect_row.row_id] = self._build_node_track(
                    effect_node, effect_color)
                self._append_layer_node_params(
                    effect_node, effect_color,
                    parent_row_id=effect_row.row_id,
                    depth=2, visible=effect_row.expanded)

    def _append_layer_node_params(self, node: Any, color: str,
                                  parent_row_id: str, depth: int,
                                  visible: bool) -> None:
        """Append the live animatable parameters of one projected node."""
        for pname, pdef in self._filtered_params(node):
            animatable = bool(getattr(
                pdef, "is_animatable", lambda: False)())
            has_track = self._has_param_track(node, pname, pdef)
            if not animatable and not has_track:
                continue
            param_row = KeyframesRow(
                row_id=f"param:{node.id}:{pname}",
                node_id=node.id,
                param_name=pname,
                label=self._param_label(node, pname, pdef),
                row_type="param",
                depth=depth,
                expanded=True,
                selected=False,
                visible=visible,
                node_color=color,
                parent_row_id=parent_row_id,
            )
            self._rows.append(param_row)
            self._tracks[param_row.row_id] = self._build_param_track(
                node, pname, color)

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
        # A 3D layer's source is the Model3D node that owns the animation
        # parameters. It is intentionally projected as a child row so the
        # layer exposes AE-style animation controls instead of hiding the
        # very node that drives its Scene3D root.
        include_source = getattr(layer, "layer_type", "") == LAYER_TYPE_3D
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
            if ((node_id != source_id or include_source) and node_id not in generated
                    and getattr(node, "node_type", "") not in excluded_types
                    and node_id != getattr(layer, "root_node_id", None)):
                ordered.append(node)

        visit(getattr(layer, "root_node_id", ""))
        return ordered

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
        expanded: Dict[str, bool] = {
            row.row_id: row.expanded
            for row in self._rows
            if row.row_type in ("node", "layer")
        }
        for row in self._rows:
            if row.parent_row_id and not expanded.get(row.parent_row_id, True):
                continue
            if row.row_type == "param" and not row.visible:
                continue
            out.append(row)
        return out

    def track_for(self, row_id: str) -> Optional[KeyframesTrackVisual]:
        return self._tracks.get(row_id)

    def tracks(self) -> Dict[str, KeyframesTrackVisual]:
        return dict(self._tracks)

    # ── composition range (default layer bars) ──

    def set_comp_range(self, start_frame: int, end_frame: int) -> None:
        """Store the comp frame range and refit all default layer bars."""
        self._comp_range = (int(start_frame), int(end_frame))
        for tr in self._tracks.values():
            if tr.range_bar is not None and tr.range_bar.default:
                tr.range_bar.start_frame = int(start_frame)
                tr.range_bar.end_frame = int(end_frame)

    # ── expansion / filtering / selection ──

    def set_expanded(self, node_id: str, expanded: bool) -> None:
        if expanded:
            self._expanded.add(node_id)
        else:
            self._expanded.discard(node_id)
        for row in self._rows:
            if row.row_type in ("node", "layer") and row.node_id == node_id:
                row.expanded = expanded
            elif row.row_type == "param" and row.node_id == node_id:
                row.visible = expanded

    def expand_all(self, expanded: bool = True) -> None:
        for row in self._rows:
            if row.row_type in ("node", "layer"):
                row.expanded = expanded
                if expanded:
                    self._expanded.add(row.node_id)
                else:
                    self._expanded.discard(row.node_id)
        for row in self._rows:
            if row.row_type == "param":
                row.visible = expanded

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
                              pdef: Any) -> bool:
        has_track = self._has_param_track(node, param_name, pdef)
        if self._filter == "animated":
            return has_track
        if self._filter == "modified":
            return self._is_modified_param(node, param_name, pdef, has_track)
        if self._filter == "expression":
            return self._has_expression(node, param_name)
        if self._filter == "selected":
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

    def _node_has_matching_params(self, node: Any) -> bool:
        return bool(self._filtered_params(node))

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
                          source_node: Any = None) -> KeyframesTrackVisual:
        tr = getattr(node, "time_range", None)
        range_bar: Optional[TimeRangeBar] = None
        # Layer rows carry their bar on the root (Transform) node, but the
        # media length lives on the layer's video source node.
        media_node = source_node if source_node is not None else node
        source_total = self._media_total_frames(media_node)
        if isinstance(tr, dict) and tr.get("enabled", True):
            start = int(tr.get("comp_start", tr.get("source_in", 0)) or 0)
            end = int(tr.get("comp_end", tr.get("source_out", 0)) or 0)
            range_bar = TimeRangeBar(
                start_frame=start, end_frame=end,
                label=self._node_label(node), color=color)
        else:
            # AE-style layer bar: every material/node row shows a bar
            # spanning the whole composition until the user trims it.
            # Video media shows its full source length instead, so material
            # longer than the composition stays visible and trimmable.
            if source_total > 0:
                cs, ce = 0, source_total - 1
            else:
                cs, ce = self._comp_range
            if ce >= cs:
                range_bar = TimeRangeBar(
                    start_frame=cs, end_frame=ce,
                    label=self._node_label(node), color=color, default=True)
        return KeyframesTrackVisual(
            row_id=f"node:{node.id}",
            range_bar=range_bar, color=color, node_color=color,
            source_total=source_total)

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
