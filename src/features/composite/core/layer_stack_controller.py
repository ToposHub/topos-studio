"""Single entry point for all AE-style layer operations (plan §9.2).

Every layer mutation — add, remove, reorder, duplicate, rename, set
property, set parent — goes through :class:`LayerStackController`.  The
controller:

1. validates the intent (delegates to :class:`LayerStackValidator`);
2. mutates the :class:`NodeGraph` and :class:`LayerStack` together;
3. wraps the whole change in **one** undo action so a media drop or a
   reorder is a single undo step (plan §1.2.6 / §10);
4. invokes the :class:`LayerStackCompiler` to keep the Merge/Output chain
   in sync with the layer ordering;
5. emits ``layer_stack_changed`` so the Keyframes panel / NodeGraph view
   can refresh.

UI code (Keyframes panel, media pool drop handler, ...) must never touch
the graph directly for layer operations — it calls into this controller.
The free NodeGraph editor still edits the graph for non-layer concerns
(branches, Dots, Groups, Masks) without going through the controller.
"""

from __future__ import annotations

import copy
import logging
import math
from typing import Any, Dict, List, Optional

try:
    from PyQt6.QtCore import QObject, pyqtSignal
    _HAVE_QT = True
except Exception:  # pragma: no cover - allows pure-CLI tests
    _HAVE_QT = False

    class QObject:  # type: ignore
        def __init__(self, *a, **kw) -> None:
            pass

    def pyqtSignal(*a, **kw):  # type: ignore
        return None

from .layer_model import (
    Layer,
    LayerStack,
    DEFAULT_BLEND_MODE,
    LAYER_BLEND_MODES,
    LAYER_TYPE_PRECOMP,
    LAYER_TYPE_SHAPE,
    LAYER_TYPE_3D,
    TRACK_MATTE_MODES,
)
from .layer_stack_compiler import (
    LayerStackCompiler, CompileChange, CompileResult,
    MERGE_NODE_TYPE,
)
from .layer_stack_validator import LayerStackValidator
from .layer_provenance import (
    LayerProvenance,
    LayerProvenanceResolver,
    NodeProvenance,
)

logger = logging.getLogger(__name__)


# Action type strings (kept in one place so they can be audited / persisted).
ACTION_ADD_LAYER = "layer_add"
ACTION_REMOVE_LAYER = "layer_remove"
ACTION_MOVE_LAYER = "layer_move"
ACTION_DUPLICATE_LAYER = "layer_duplicate"
ACTION_RENAME_LAYER = "layer_rename"
ACTION_SET_LAYER_PROPERTY = "layer_set_property"
ACTION_SET_LAYER_PARENT = "layer_set_parent"
ACTION_REBUILD_STACK = "layer_stack_rebuild"
ACTION_ADD_MEDIA = "layer_add_media"
ACTION_ADD_PRECOMP = "layer_add_precomp"
ACTION_SET_LAYER_TRACK_MATTE = "layer_set_track_matte"
ACTION_ATTACH_NODE_TO_LAYER = "layer_attach_node"
ACTION_PROMOTE_NODE_TO_LAYER = "layer_promote_node"


class LayerStackController(QObject if _HAVE_QT else object):
    """Coordinator between LayerStack, NodeGraph, compiler and undo stack.

    The controller is the first *truly independent* controller in the app
    (the existing ``LayerOpsMixin`` / ``CompositeControllerMixin`` are
    MainWindow mixins).  It is intentionally QObject-based so it can emit
    signals without coupling to the page/widget layer.
    """

    # Emitted after any layer change with the affected stack_id (or "" for
    # a global refresh).  UI panels connect to this to rebuild.
    layer_stack_changed = pyqtSignal(str) if _HAVE_QT else None
    # Emitted when the compiler hit a conflict (plan §4.5).  Carries the
    # human-readable reason; the UI asks the user how to proceed.
    layer_stack_conflict = pyqtSignal(str, str) if _HAVE_QT else None

    def __init__(self, graph: Any, undo_stack: Any = None,
                 node_factory: Any = None,
                 parent: Optional[QObject] = None) -> None:
        """Create a controller bound to ``graph``.

        Args:
            graph: the :class:`NodeGraph` owning the layer stacks.
            undo_stack: optional :class:`UndoStack`.  When None, operations
                execute without undo tracking (useful for tests / scripting).
            node_factory: callable ``(node_type, node_id=None) -> Node``.
                When None, the controller falls back to the graph's
                ``create_node_by_type``.  Injected so tests can swap it.
        """
        if _HAVE_QT:
            super().__init__(parent)
        self.graph = graph
        self.undo_stack = undo_stack
        self.compiler = LayerStackCompiler(graph)
        self.validator = LayerStackValidator(graph)
        self.provenance = LayerProvenanceResolver(graph)
        self._node_factory = node_factory
        # Incremented on every successful mutation; useful for change
        # detection in tests / UI caches.
        self.generation: int = 0

    # ── Convenience accessors ───────────────────────────────────────────

    def stacks(self) -> Dict[str, LayerStack]:
        return self.graph.get_layer_stacks()

    def main_stack(self) -> Optional[LayerStack]:
        return self.graph.get_main_layer_stack()

    def ensure_main_stack(self, output_node_id: str = "") -> LayerStack:
        """Return the main stack, creating an empty one if absent.

        Used by the media-drop flow and the legacy-project projection.
        When ``output_node_id`` is empty the controller auto-discovers the
        graph's Output node so a fresh media drop wires up correctly.
        """
        if not output_node_id:
            output_node_id = self._discover_output_node_id()
        stack = self.graph.get_main_layer_stack()
        if stack is None:
            stack = LayerStack(
                stack_id="main",
                output_node_id=output_node_id,
            )
            self.graph.set_layer_stack(stack)
        elif output_node_id and not stack.output_node_id:
            stack.output_node_id = output_node_id
        return stack

    def _discover_output_node_id(self) -> str:
        """Find the (first) Output node id on the graph, or "" if none."""
        for node in self.graph.nodes.values():
            if node.node_type == "output":
                return node.id
        return ""

    def layers_map(self) -> Dict[str, Layer]:
        """Return ``layer_id -> Layer`` for the current stacks."""
        return {l.layer_id: l for l in self.graph.get_all_layers()}

    def layer_relationships(self, stack_id: Optional[str] = None
                            ) -> List[Dict[str, Any]]:
        """Return a read-only relation index for one LayerStack.

        The index is the shared contract for Layer-mode diagnostics. It
        identifies parent links, matte targets, matte sources, mode/invert
        state, and whether each reference is valid in the selected stack.
        Keeping this derived data in the controller prevents the UI from
        reimplementing layer graph rules or mutating layer state while it
        paints a relation badge.
        """
        stack = (self.graph.get_layer_stack(stack_id)
                 if stack_id else self.main_stack())
        if stack is None:
            return []
        layers = self.layers_map()
        stack_ids = {str(layer_id) for layer_id in stack.layer_ids}
        matte_targets: Dict[str, List[str]] = {}
        for layer_id in stack.layer_ids:
            layer = layers.get(layer_id)
            matte_id = getattr(layer, "track_matte_layer_id", None) \
                if layer is not None else None
            if matte_id:
                matte_targets.setdefault(str(matte_id), []).append(str(layer_id))

        relations: List[Dict[str, Any]] = []
        for layer_id in stack.layer_ids:
            layer = layers.get(layer_id)
            if layer is None:
                continue
            parent_id = (str(layer.parent_layer_id)
                         if layer.parent_layer_id else None)
            matte_id = (str(layer.track_matte_layer_id)
                        if layer.track_matte_layer_id else None)
            parent_layer = layers.get(parent_id) if parent_id else None
            matte_layer = layers.get(matte_id) if matte_id else None
            matte_valid = bool(
                matte_layer is not None and matte_id in stack_ids
                and matte_id != str(layer_id))
            parent_valid = bool(parent_layer is not None
                                 and parent_id in stack_ids
                                 and parent_id != str(layer_id))
            target_ids = tuple(matte_targets.get(str(layer_id), ()))
            relations.append({
                "layer_id": str(layer_id),
                "layer_name": str(layer.name),
                "parent_layer_id": parent_id,
                "parent_layer_name": (str(parent_layer.name)
                                       if parent_layer is not None else ""),
                "parent_valid": parent_valid,
                "track_matte_layer_id": matte_id,
                "track_matte_layer_name": (str(matte_layer.name)
                                             if matte_layer is not None else ""),
                "track_matte_mode": str(layer.track_matte_mode or "alpha"),
                "track_matte_invert": bool(layer.track_matte_invert),
                "track_matte_valid": matte_valid if matte_id else True,
                "track_matte_target_layer_ids": target_ids,
            })
        return relations

    def snapshot(self) -> Dict[str, Any]:
        """Capture a complete layer-owned graph state for a host transaction."""
        return copy.deepcopy(self._snapshot())

    def restore_snapshot(self, snapshot: Dict[str, Any]) -> None:
        """Restore a state returned by :meth:`snapshot`."""
        self._restore(copy.deepcopy(snapshot))

    def layer_ids_for_node(self, node_id: str) -> List[str]:
        """Return layers that own ``node_id`` as source or live root.

        A media Layer owns both the source node and the automatically-created
        Transform root.  Keeping this lookup in the controller gives the
        NodeGraph editor a safe reverse-sync entry point: deleting either
        owned node can remove the Layer and let the compiler reclaim its
        generated Merge chain.
        """
        node_id = str(node_id or "")
        if not node_id:
            return []
        return [
            layer.layer_id
            for layer in self.graph.get_all_layers()
            if node_id in {layer.source_node_id, layer.root_node_id}
        ]

    def provenance_for_layer(self, layer_id: str,
                             stack_id: Optional[str] = None
                             ) -> Optional[LayerProvenance]:
        """Describe a layer's root/source chain and managed boundaries."""
        return self.provenance.for_layer(layer_id, stack_id)

    def provenance_for_node(self, node_id: str) -> List[NodeProvenance]:
        """Return layer roles for a node, including generated Merge roles."""
        return self.provenance.for_node(node_id)

    def node_ids_for_layer(self, layer_id: str,
                           stack_id: Optional[str] = None) -> List[str]:
        """Return the ordered nodes contributing to a layer, root first."""
        return self.provenance.node_ids_for_layer(layer_id, stack_id)

    def trace_layer_ids_for_node(self, node_id: str) -> List[str]:
        """Reverse-locate all layers whose owned chain contains a node."""
        return self.provenance.layer_ids_for_node(node_id)

    def active_layer_ids_for_output(self,
                                    stack_id: Optional[str] = None) -> List[str]:
        """Return layers whose root is reachable from the stack Output.

        Layer metadata is intentionally retained when a user disconnects a
        material in Nodes mode, but the Keyframes Layers view represents the
        rendered composition rather than every node ever added to the graph.
        Walking upstream from Output gives that view a single, graph-owned
        definition of an active layer.  Reconnecting the root automatically
        makes the existing layer row visible again, including its keyframes.
        """
        stack = (self.graph.get_layer_stack(stack_id)
                 if stack_id else self.main_stack())
        if stack is None or not stack.output_node_id:
            return []
        if self.graph.get_node(stack.output_node_id) is None:
            return []

        incoming = {}
        for conn in getattr(self.graph, "connections", []):
            incoming.setdefault(conn.dst_node_id, []).append(conn.src_node_id)

        reachable: set[str] = set()
        pending = [stack.output_node_id]
        while pending:
            node_id = pending.pop()
            if node_id in reachable:
                continue
            if self.graph.get_node(node_id) is None:
                continue
            reachable.add(node_id)
            pending.extend(incoming.get(node_id, ()))

        result: List[str] = []
        for layer_id in stack.layer_ids:
            layer = self.graph.get_layer(layer_id)
            if layer is None:
                continue
            root_id = layer.root_node_id
            if root_id and root_id in reachable:
                result.append(layer_id)
        return result

    def _stack_for_layer(self, layer_id: str) -> Optional[LayerStack]:
        """Return the owning stack for a layer, including nested stacks."""
        for stack in self.graph.get_layer_stacks().values():
            if layer_id in stack.layer_ids:
                return stack
        return None

    def _nested_stack_ids(self, stack_id: str) -> List[str]:
        """Return a child stack and all descendant Precomp stacks."""
        result: List[str] = []
        seen = set()

        def visit(current_id: str) -> None:
            if not current_id or current_id in seen:
                return
            stack = self.graph.get_layer_stack(current_id)
            if stack is None:
                return
            seen.add(current_id)
            result.append(current_id)
            for lid in stack.layer_ids:
                child_layer = self.graph.get_layer(lid)
                if (child_layer is None or
                        child_layer.layer_type != LAYER_TYPE_PRECOMP):
                    continue
                child_node = self.graph.get_node(child_layer.root_node_id)
                child_id = (child_node.parameters.get("child_stack_id", "")
                            if child_node is not None else "")
                visit(child_id)

        visit(stack_id)
        return result

    def emit_changed(self, stack_id: str = "") -> None:
        self.generation += 1
        if _HAVE_QT and self.layer_stack_changed is not None:
            self.layer_stack_changed.emit(stack_id)

    # ── Node factory helper ─────────────────────────────────────────────

    def _create_node(self, node_type: str, node_id: Optional[str] = None):
        if self._node_factory is not None:
            return self._node_factory(node_type, node_id)
        # Fall back to the NODE_REGISTRY (module-level factory in node_engine).
        from .node_engine import NODE_REGISTRY
        node_cls = NODE_REGISTRY.get(node_type)
        if node_cls is None:
            return None
        return node_cls(node_id=node_id)

    # ── Add layer ───────────────────────────────────────────────────────

    def add_layer(self, layer: Layer, insert_index: Optional[int] = None,
                  auto_compile: bool = True,
                  description: str = "", stack_id: Optional[str] = None) -> str:
        """Append (or insert) a layer and re-compile the Merge chain.

        Returns the layer_id.  The caller is responsible for having already
        created the layer's root/source nodes on the graph.
        """
        stack = (self.graph.get_layer_stack(stack_id)
                 if stack_id else self.ensure_main_stack())
        if stack is None:
            raise ValueError(f"Layer stack {stack_id!r} does not exist.")
        if not layer.layer_id or self.graph.get_layer(layer.layer_id) is not None:
            raise ValueError(f"Layer id {layer.layer_id!r} already exists.")
        if insert_index is None:
            insert_index = 0  # new layer defaults to top (foreground)
        insert_index = max(0, min(int(insert_index), len(stack.layer_ids)))
        # Snapshot for undo.
        before = self._snapshot()
        # Validate node presence.
        if not self.graph.get_node(layer.root_node_id):
            raise ValueError(
                f"Layer root node {layer.root_node_id!r} does not exist; "
                f"create it before calling add_layer.")
        stack.layer_ids.insert(insert_index, layer.layer_id)
        if not self.graph.upsert_layer(stack.stack_id, layer):
            # Keep a failed add atomic even when a custom graph implementation
            # rejects the stack registration.
            stack.layer_ids.remove(layer.layer_id)
            raise ValueError(f"Unable to register layer {layer.layer_id!r}.")

        result = CompileResult(ok=True)
        if auto_compile:
            result = self._recompile(stack)
        after = self._snapshot()
        self._push_undo(ACTION_ADD_LAYER, before, after, description or
                        f"Add layer {layer.name!r}", stack.stack_id,
                        extra={"conflict": result.conflict,
                               "conflict_reason": result.conflict_reason})
        self._handle_conflict(result, stack.stack_id)
        self.emit_changed(stack.stack_id)
        return layer.layer_id

    def create_precomp_layer(self, name: str = "Precomp",
                             insert_index: Optional[int] = None,
                             description: str = "") -> str:
        """Create a nested LayerStack exposed through a parent layer.

        The child stack targets the new Precomp node's ``image`` input.  Both
        stacks, the boundary node, and the parent layer are captured in one
        undo snapshot so the operation is atomic from the UI's perspective.
        """
        import uuid

        parent_stack = self.ensure_main_stack()
        before = self._snapshot()
        precomp_id = self._new_node_id("precomp")
        node = self._create_node("precomp", precomp_id)
        if node is None:
            raise ValueError("Unable to create the Precomp node.")
        child_id = f"precomp_{uuid.uuid4().hex[:8]}"
        child_stack = LayerStack(
            stack_id=child_id,
            name=name or "Precomp",
            output_node_id=precomp_id,
        )
        layer_id = self._new_layer_id("precomp")
        layer = Layer(
            layer_id=layer_id,
            name=name or "Precomp",
            root_node_id=precomp_id,
            layer_type=LAYER_TYPE_PRECOMP,
        )
        try:
            node.set_parameter("child_stack_id", child_id)
            node.set_parameter("precomp_name", name or "Precomp")
            self.graph.add_node(node)
            self.graph.set_layer_stack(child_stack)
            if insert_index is None:
                insert_index = 0
            index = max(0, min(int(insert_index),
                               len(parent_stack.layer_ids)))
            parent_stack.layer_ids.insert(index, layer_id)
            if not self.graph.upsert_layer(parent_stack.stack_id, layer):
                raise ValueError(f"Unable to register layer {layer_id!r}.")
            result = self._recompile(parent_stack)
            if result.conflict:
                raise ValueError(result.conflict_reason or
                                 "Unable to compile the Precomp layer.")
        except Exception:
            self._restore(before)
            raise

        after = self._snapshot()
        self._push_undo(
            ACTION_ADD_PRECOMP, before, after,
            description or f"Add precomp layer {name!r}", parent_stack.stack_id,
            extra={"created_node_ids": [precomp_id],
                   "created_layer_ids": [layer_id],
                   "child_stack_id": child_id})
        self.emit_changed(parent_stack.stack_id)
        return layer_id

    # ── Add media (the full drop flow, plan §5.3) ───────────────────────

    def add_media_layer(self, name: str, source_node_id: str,
                        root_node_id: Optional[str] = None,
                        layer_type: str = "media",
                        insert_index: Optional[int] = None,
                        blend_mode: str = DEFAULT_BLEND_MODE,
                        opacity: float = 1.0,
                        description: str = "",
                        _before_snapshot: Optional[Dict[str, Any]] = None,
                        stack_id: Optional[str] = None) -> str:
        """High-level media-drop entry point.

        Creates a Layer whose root is ``root_node_id`` (or auto-wraps the
        source in a Transform when the source node is not layer-root
        capable, plan §3.5 / §5.4).  The created source/transform nodes
        must already exist on the graph when ``root_node_id`` is provided;
        when None, the controller creates an auto Transform wrapping the
        source.  The entire drop is one undo step (plan §1.2.6 / §10).
        """
        if blend_mode not in LAYER_BLEND_MODES:
            raise ValueError(f"Unsupported layer blend mode {blend_mode!r}.")
        try:
            opacity = float(opacity)
        except (TypeError, ValueError):
            raise ValueError("Layer opacity must be a finite number.") from None
        if not math.isfinite(opacity) or not 0.0 <= opacity <= 1.0:
            raise ValueError("Layer opacity must be within [0, 1].")
        before = _before_snapshot if _before_snapshot is not None \
            else self._snapshot()
        created_node_ids: List[str] = []

        source_node = self.graph.get_node(source_node_id)
        if source_node is None:
            raise ValueError(
                f"Media source node {source_node_id!r} does not exist.")

        stack = (self.graph.get_layer_stack(stack_id)
                 if stack_id else self.ensure_main_stack())
        if stack is None:
            raise ValueError(f"Layer stack {stack_id!r} does not exist.")

        auto_transform = None
        if root_node_id is None:
            # Auto-wrap: source → Transform.  If the source node already
            # supports layer_root (e.g. Text) we skip the wrapper.
            if getattr(source_node, "supports_layer_root", False):
                root_node_id = source_node_id
            else:
                t = self._create_node("transform")
                if t is None:
                    raise ValueError("Unable to create the media Transform node.")
                self.graph.add_node(t)
                # source → transform
                from .layer_stack_compiler import _output_port_of
                if not self.graph.connect(
                        source_node_id,
                        _output_port_of(self.graph, source_node_id),
                        t.id, "image"):
                    self.graph.remove_node(t.id)
                    raise ValueError(
                        f"Unable to connect media source {source_node_id!r} "
                        "to its Transform wrapper.")
                root_node_id = t.id
                auto_transform = t
                created_node_ids.append(t.id)
        elif self.graph.get_node(root_node_id) is None:
            raise ValueError(f"Layer root node {root_node_id!r} does not exist.")

        if auto_transform is not None:
            self._fit_new_media_transform(auto_transform, source_node, stack)
        layer_id = self._new_layer_id()
        if insert_index is None:
            insert_index = 0
        insert_index = max(0, min(int(insert_index), len(stack.layer_ids)))
        layer = Layer(
            layer_id=layer_id,
            name=name or "Layer",
            root_node_id=root_node_id,
            source_node_id=source_node_id,
            layer_type=layer_type,
            blend_mode=blend_mode,
            opacity=opacity,
        )
        stack.layer_ids.insert(insert_index, layer_id)
        if not self.graph.upsert_layer(stack.stack_id, layer):
            stack.layer_ids.remove(layer_id)
            for nid in created_node_ids:
                if self.graph.get_node(nid) is not None:
                    self.graph.remove_node(nid)
            raise ValueError(f"Unable to register layer {layer_id!r}.")

        result = self._recompile(stack)
        after = self._snapshot()
        self._push_undo(
            ACTION_ADD_MEDIA, before, after,
            description or f"Add media layer {name!r}", stack.stack_id,
            extra={"created_node_ids": created_node_ids,
                   "created_layer_ids": [layer_id],
                   "conflict": result.conflict,
                   "conflict_reason": result.conflict_reason})
        self._handle_conflict(result, stack.stack_id)
        self.emit_changed(stack.stack_id)
        return layer_id

    @staticmethod
    def _node_pixel_size(node: Any) -> tuple[int, int]:
        """Return the best-known pixel size for a source node."""
        for width_key, height_key in (
                ("_width", "_height"),
                ("_cached_width", "_cached_height"),
                ("width", "height"),
                ("video_width", "video_height")):
            width = getattr(node, width_key, None)
            height = getattr(node, height_key, None)
            if width is None or height is None:
                params = getattr(node, "parameters", {}) or {}
                width = params.get(width_key, width)
                height = params.get(height_key, height)
            try:
                width_i, height_i = int(width or 0), int(height or 0)
            except (TypeError, ValueError):
                continue
            if width_i > 0 and height_i > 0:
                return width_i, height_i
        return 0, 0

    def _fit_new_media_transform(self, transform: Any, source: Any,
                                 stack: LayerStack) -> None:
        """Downscale an oversized dropped source to the composition format.

        The source remains in its native pixel space (so Transform handles
        and pivots stay 4K-aware), while the generated Transform starts with
        a uniform fit-to-composition scale and a translation that centers the
        scaled result in the composition.  Smaller sources retain the
        historical 1.0 scale and origin and can be enlarged manually.
        """
        source_w, source_h = self._node_pixel_size(source)
        output = self.graph.get_node(stack.output_node_id) \
            if stack.output_node_id else None
        output_params = getattr(output, "parameters", {}) or {}
        try:
            comp_w = int(output_params.get("width", 0) or 0)
            comp_h = int(output_params.get("height", 0) or 0)
        except (TypeError, ValueError):
            comp_w = comp_h = 0
        if source_w <= 0 or source_h <= 0 or comp_w <= 0 or comp_h <= 0:
            return
        fit_scale = min(1.0, comp_w / source_w, comp_h / source_h)
        if fit_scale < 1.0:
            transform.set_parameter("scale_x", fit_scale)
            transform.set_parameter("scale_y", fit_scale)
            transform.set_parameter(
                "translate_x", comp_w / 2.0 - source_w / 2.0)
            transform.set_parameter(
                # Transform's Y-down interaction coordinates are converted
                # back from the compositor's bottom-left BBox, so centering
                # an oversized source uses the opposite sign on Y.
                "translate_y", source_h / 2.0 - comp_h / 2.0)

    def remove_layer_root_node(self, layer_id: str,
                               description: str = "",
                               record_undo: bool = True) -> bool:
        """Remove a layer's effect/root node while preserving the layer.

        A media layer owns both its source and its generated Transform root.
        Deleting the source means deleting the layer; deleting only the root
        means the layer continues from the root's upstream image node.  The
        LayerStack compiler then rewires the generated Merge chain.
        """
        stack = self._stack_for_layer(layer_id)
        layer = self.graph.get_layer(layer_id)
        if stack is None or layer is None or layer.locked:
            return False
        root_id = layer.root_node_id
        source_id = layer.source_node_id
        if not root_id or not source_id or root_id == source_id:
            return False
        root = self.graph.get_node(root_id)
        if root is None:
            return False
        # Do not remove a root shared by another layer.
        if any(other.layer_id != layer_id
               and other.root_node_id == root_id
               for other in self.graph.get_all_layers()):
            return False

        before = self._snapshot()
        upstream_id = None
        for conn in self.graph.connections:
            if conn.dst_node_id != root_id:
                continue
            dst_port = getattr(conn, "dst_port_name", "")
            if dst_port == "image":
                upstream_id = conn.src_node_id
                break
        upstream_id = upstream_id or source_id
        layer.root_node_id = upstream_id
        self.graph.upsert_layer(stack.stack_id, layer)
        if self.graph.get_node(root_id) is not None:
            self.graph.remove_node_and_reconnect(root_id)
        result = self._recompile(stack)
        after = self._snapshot()
        if record_undo:
            self._push_undo(
                ACTION_REMOVE_LAYER, before, after,
                description or f"Remove layer root {root_id!r}",
                stack.stack_id,
                extra={"deleted_node_ids": [root_id],
                       "deleted_layer_ids": [],
                       "conflict": result.conflict,
                       "conflict_reason": result.conflict_reason})
        self._handle_conflict(result, stack.stack_id)
        self.emit_changed(stack.stack_id)
        return True

    def add_media_payload(self, payload: Any, insert_index: Optional[int] = None,
                          description: str = "") -> str:
        """Create a source node from a resolved media-drop payload.

        The source node is created inside the same snapshot boundary as the
        layer/Transform/Merge changes.  Calling ``add_media_layer`` after
        creating the source externally would leave that source behind after a
        single Undo, which violates the media-drop atomicity contract.
        """
        before = self._snapshot()
        node_type = str(getattr(payload, "node_type", "image_input"))
        node = self._create_node(node_type, self._new_node_id(node_type))
        if node is None:
            raise ValueError(f"Unable to create media node {node_type!r}.")
        self.graph.add_node(node)
        try:
            values = {
                "file_path": getattr(payload, "path", ""),
                "asset_id": getattr(payload, "asset_id", ""),
                "width": getattr(payload, "width", 0),
                "height": getattr(payload, "height", 0),
                "fps": (getattr(payload, "extras", {}) or {}).get("fps"),
                "total_frames": getattr(payload, "duration", 0),
                "video_width": getattr(payload, "width", 0),
                "video_height": getattr(payload, "height", 0),
            }
            for key, value in values.items():
                if value is not None and key in getattr(node, "parameters", {}):
                    node.set_parameter(key, value)
            return self.add_media_layer(
                getattr(payload, "display_name", "Media") or "Media",
                node.id,
                layer_type="media",
                insert_index=insert_index,
                description=description or "Add media layer",
                _before_snapshot=before,
            )
        except Exception:
            self._restore(before)
            raise

    def add_model3d_payload(self, payload: Any,
                            insert_index: Optional[int] = None,
                            description: str = "") -> str:
        """Create one AE-style 3D layer from a resolved media payload.

        A Model3D node emits a ``scene`` payload, while a layer stack expects
        an image-producing root.  The small, owned graph chain below keeps
        the asset as one visible layer while preserving the renderer's
        existing scene-payload architecture::

            Model3D (scene) -> Scene3D (image) -> layer merge chain

        Both nodes are covered by the same snapshot, so undoing the drop
        removes the complete 3D layer rather than leaving an orphan scene
        node in the node graph.
        """
        before = self._snapshot()
        model_id = self._new_node_id("model3d")
        scene_id = self._new_node_id("scene3d")
        model = self._create_node("model3d", model_id)
        scene = self._create_node("scene3d", scene_id)
        if model is None or scene is None:
            raise ValueError("Unable to create the Model3D/Scene3D layer nodes.")

        try:
            stack = self.ensure_main_stack()
            self.graph.add_node(model)
            self.graph.add_node(scene)

            model_path = str(getattr(payload, "path", "") or "")
            model_values = {
                "model_path": model_path,
                "use_imported_model": bool(model_path),
                "anim_fps": (getattr(payload, "extras", {}) or {}).get("fps"),
            }
            for key, value in model_values.items():
                if value is not None and key in getattr(model, "parameters", {}):
                    model.set_parameter(key, value)

            # Match the Scene3D render surface to the composition when the
            # Output node exposes a valid format. Scene3D keeps its normal
            # defaults when this is a standalone/test graph.
            output = self.graph.get_node(stack.output_node_id) \
                if stack is not None and stack.output_node_id else None
            output_params = getattr(output, "parameters", {}) or {}
            for key in ("width", "height"):
                value = output_params.get(key)
                if value is not None and key in getattr(scene, "parameters", {}):
                    try:
                        scene.set_parameter(key, int(value))
                    except (TypeError, ValueError):
                        pass

            from .layer_stack_compiler import _output_port_of
            if not self.graph.connect(
                    model.id, _output_port_of(self.graph, model.id),
                    scene.id, "in"):
                raise ValueError("Unable to connect Model3D to Scene3D.")

            return self.add_media_layer(
                getattr(payload, "display_name", "3D Model") or "3D Model",
                model.id,
                root_node_id=scene.id,
                layer_type=LAYER_TYPE_3D,
                insert_index=insert_index,
                description=description or "Add 3D model layer",
                _before_snapshot=before,
            )
        except Exception:
            self._restore(before)
            raise

    def add_node_to_layer(self, layer_id: str, node_id: str,
                          description: str = "") -> bool:
        """Append an image-effect node to a managed Layer's live chain.

        This is the explicit Layer-mode operation for Mask/Roto/Grade/etc.
        The existing layer root feeds the node's first image input, then the
        node becomes the new root.  Parameters and keyframes remain on the
        node itself, so switching back to Node mode sees the same graph.
        """
        layer = self.graph.get_layer(layer_id)
        stack = self._stack_for_layer(layer_id)
        node = self.graph.get_node(node_id)
        if (layer is None or stack is None or node is None or layer.locked
                or layer.root_node_id == node_id):
            return False
        image_inputs = [p for p in getattr(node, "inputs", [])
                        if getattr(p, "data_type", "") == "image"]
        if not image_inputs:
            return False
        root = self.graph.get_node(layer.root_node_id)
        if root is None:
            return False

        before = self._snapshot()
        from .layer_stack_compiler import _output_port_of
        if not self.graph.connect(
                root.id, _output_port_of(self.graph, root.id),
                node.id, image_inputs[0].name):
            return False
        layer.root_node_id = node.id
        result = self._recompile(stack)
        if result.conflict:
            self._restore(before)
            self._handle_conflict(result, stack.stack_id)
            return False
        after = self._snapshot()
        self._push_undo(
            ACTION_ATTACH_NODE_TO_LAYER, before, after,
            description or f"Add {node.display_title} to layer",
            stack.stack_id,
            extra={"layer_id": layer_id, "node_id": node_id})
        self.emit_changed(stack.stack_id)
        return True

    def promote_node_to_layer(self, node_id: str, name: str = "",
                              insert_index: Optional[int] = None,
                              layer_type: str = LAYER_TYPE_SHAPE,
                              description: str = "") -> Optional[str]:
        """Expose an existing free-graph node as one managed Layer.

        Promotion is explicit: adding a Mask/Roto node in Node mode never
        silently creates a layer.  The node keeps its id and all animation;
        only a Layer reference is created around it.
        """
        node = self.graph.get_node(node_id)
        stack = self.ensure_main_stack()
        if node is None or stack is None:
            return None
        if any(layer.root_node_id == node_id
               for layer in self.graph.get_all_layers()):
            return None
        layer_id = self._new_layer_id()
        layer = Layer(
            layer_id=layer_id,
            name=name or getattr(node, "display_title", None)
            or getattr(node, "node_title", "Layer"),
            root_node_id=node_id,
            layer_type=layer_type,
            managed=True,
        )
        try:
            self.add_layer(
                layer, insert_index=insert_index,
                description=description or f"Promote {layer.name} to layer",
                stack_id=stack.stack_id)
        except (TypeError, ValueError):
            return None
        return layer_id

    def create_matte_layer(self, node_id: str, name: str = "Matte",
                           insert_index: Optional[int] = None) -> Optional[str]:
        """Promote a Mask/Roto node as a shape/Track Matte-capable layer."""
        node = self.graph.get_node(node_id)
        if node is None or getattr(node, "node_type", "") not in {
                "mask", "roto_shape", "mask_merge", "matte"}:
            return None
        return self.promote_node_to_layer(
            node_id, name=name, insert_index=insert_index,
            layer_type=LAYER_TYPE_SHAPE, description="Create matte layer")

    # ── Remove layer ────────────────────────────────────────────────────

    def remove_layer(self, layer_id: str, delete_nodes: bool = True,
                     description: str = "", record_undo: bool = True) -> bool:
        """Remove a layer; optionally delete its owned nodes (plan §10).

        Returns True if the layer was removed.  ``delete_nodes`` controls
        whether the layer's root/source/auto-merge are deleted from the
        graph — for "detach to free graph" set False.
        """
        stack = self._stack_for_layer(layer_id) or self.main_stack()
        if stack is None:
            return False
        layer = self.graph.get_layer(layer_id)
        if layer is None:
            return False
        if layer.locked:
            return False
        before = self._snapshot()
        node_ids_to_delete: List[str] = []
        nested_stack_ids: List[str] = []
        nested_node_ids: List[str] = []
        if delete_nodes and layer.layer_type == LAYER_TYPE_PRECOMP:
            root = self.graph.get_node(layer.root_node_id)
            child_id = (root.parameters.get("child_stack_id", "")
                        if root is not None else "")
            nested_stack_ids = self._nested_stack_ids(child_id)
            # The child output connection lands on this boundary node; treat
            # it as part of the removable subgraph for external-reference
            # checks, even though it belongs to the parent layer.
            nested_node_ids.append(layer.root_node_id)
            for sid in nested_stack_ids:
                child_stack = self.graph.get_layer_stack(sid)
                if child_stack is None:
                    continue
                nested_node_ids.extend(child_stack.generated_merge_node_ids)
                for child_layer_id in child_stack.layer_ids:
                    child_layer = self.graph.get_layer(child_layer_id)
                    if child_layer is None:
                        continue
                    for node_id in (child_layer.source_node_id,
                                    child_layer.root_node_id):
                        if node_id:
                            nested_node_ids.append(node_id)
        # External/free-graph projections do not own their nodes.  Also keep
        # shared roots/sources alive when another layer references them.
        other_layers = [l for l in self.graph.get_all_layers()
                        if l.layer_id != layer_id]
        referenced_elsewhere = {
            node_id
            for other in other_layers
            for node_id in (other.root_node_id, other.source_node_id)
            if node_id
        }
        if delete_nodes and layer.managed:
            owned_ids = {node_id for node_id in (
                layer.source_node_id, layer.root_node_id) if node_id}
            # A node can be shared by the free Nuke graph without being
            # referenced by another Layer. Keep the whole owned chain when a
            # non-stack connection touches it; deleting the source alone can
            # otherwise leave an external branch pointing at a missing node.
            allowed_ids = owned_ids | set(stack.generated_merge_node_ids)
            if stack.output_node_id:
                allowed_ids.add(stack.output_node_id)
            has_external_ref = any(
                ((conn.src_node_id in owned_ids
                  and conn.dst_node_id not in allowed_ids)
                 or (conn.dst_node_id in owned_ids
                     and conn.src_node_id not in allowed_ids))
                for conn in self.graph.connections
            )
            if not has_external_ref:
                if (layer.source_node_id
                        and layer.source_node_id not in referenced_elsewhere):
                    node_ids_to_delete.append(layer.source_node_id)
                if (layer.root_node_id != layer.source_node_id
                        and layer.root_node_id not in referenced_elsewhere):
                    node_ids_to_delete.append(layer.root_node_id)
        # Deleting a parent must not leave children pointing at a missing
        # layer; AE clears the parent relationship in this case.
        for other in other_layers:
            if other.parent_layer_id == layer_id:
                other.parent_layer_id = None
        # Detach.
        self.graph.remove_layer(layer_id)
        for nid in node_ids_to_delete:
            if self.graph.get_node(nid) is not None:
                self.graph.remove_node(nid)
        # Removing a Precomp also removes its child stack metadata.  Child
        # nodes are reclaimed when they have no connection to the remaining
        # graph; externally shared branches remain intact.
        nested_node_set = set(nested_node_ids)
        if nested_stack_ids:
            for sid in reversed(nested_stack_ids):
                self.graph.remove_layer_stack(sid)
            remaining_layer_nodes = {
                node_id
                for other in self.graph.get_all_layers()
                for node_id in (other.source_node_id, other.root_node_id)
                if node_id
            }
            for nid in nested_node_set - remaining_layer_nodes:
                if self.graph.get_node(nid) is None:
                    continue
                has_external_ref = any(
                    ((conn.src_node_id == nid and
                      conn.dst_node_id not in nested_node_set)
                     or (conn.dst_node_id == nid and
                         conn.src_node_id not in nested_node_set))
                    for conn in self.graph.connections
                )
                if not has_external_ref:
                    self.graph.remove_node(nid)
        result = self._recompile(stack)
        after = self._snapshot()
        if record_undo:
            self._push_undo(
                ACTION_REMOVE_LAYER, before, after,
                description or f"Remove layer {layer.name!r}", stack.stack_id,
                extra={"deleted_node_ids": node_ids_to_delete,
                       "deleted_layer_ids": [layer_id],
                       "conflict": result.conflict,
                       "conflict_reason": result.conflict_reason})
        self._handle_conflict(result, stack.stack_id)
        self.emit_changed(stack.stack_id)
        return True

    # ── Move / reorder ──────────────────────────────────────────────────

    def move_layer(self, layer_id: str, new_index: int,
                   description: str = "") -> bool:
        """Reorder ``layer_id`` to ``new_index`` (0 = top / foreground)."""
        # AUD-4907: nested (Precomp child stack) layers must be looked up
        # in their owning stack — anchoring every move to the main stack
        # made nested layers permanently un-reorderable.
        stack = self._stack_for_layer(layer_id) or self.main_stack()
        if stack is None or layer_id not in stack.layer_ids:
            return False
        before = self._snapshot()
        layer = self.graph.get_layer(layer_id)
        if layer is not None and layer.locked:
            return False
        old_index = stack.layer_ids.index(layer_id)
        # Clamp before the no-op check, otherwise an out-of-range request can
        # create an undo record for a move that changes nothing.
        new_index = max(0, min(int(new_index), len(stack.layer_ids) - 1))
        if old_index == new_index:
            return True
        stack.layer_ids.insert(new_index, stack.layer_ids.pop(old_index))
        result = self._recompile(stack)
        after = self._snapshot()
        self._push_undo(
            ACTION_MOVE_LAYER, before, after,
            description or f"Move layer {layer_id!r}", stack.stack_id,
            extra={"from_index": old_index, "to_index": new_index,
                   "conflict": result.conflict,
                   "conflict_reason": result.conflict_reason})
        self._handle_conflict(result, stack.stack_id)
        self.emit_changed(stack.stack_id)
        return True

    # ── Duplicate ───────────────────────────────────────────────────────

    def duplicate_layer(self, layer_id: str,
                        description: str = "") -> Optional[str]:
        """Duplicate a layer's *node chain* and register a new layer above.

        Returns the new layer_id, or None on failure.  The source + root
        nodes are deep-copied via to_dict/from_dict, so the duplicate owns
        independent parameters AND its own keyframe track copies (AE-style
        Ctrl+D semantics: the clone animates independently from the
        original).  Deeper effect chains (Blur/Color/... between source
        and root, or upstream of source) are shared by reference to keep
        the duplication lightweight — power users can duplicate those via
        the node editor.  Plan §6.1's "keyframes belong to node params"
        rule still holds: the duplicate's keyframes live on the duplicate's
        own node, never on the Layer object.
        """
        stack = self._stack_for_layer(layer_id) or self.main_stack()
        if stack is None:
            return None
        layer = self.graph.get_layer(layer_id)
        if layer is None or layer_id not in stack.layer_ids:
            return None
        before = self._snapshot()

        # Clone the source + root chain.  We only clone the immediate
        # source and root; deeper effect chains (Blur/Color/...) are shared
        # by reference to keep the duplication lightweight.  Power users can
        # duplicate the full chain via the node editor.
        created_node_ids: List[str] = []
        new_source_id = None
        if layer.source_node_id:
            new_source_id = self._clone_node(layer.source_node_id)
            if not new_source_id:
                return None
            created_node_ids.append(new_source_id)
        if layer.root_node_id == layer.source_node_id and new_source_id:
            new_root_id = new_source_id
        else:
            new_root_id = self._clone_node(layer.root_node_id)
            if not new_root_id:
                if new_source_id:
                    self.graph.remove_node(new_source_id)
                return None
            created_node_ids.append(new_root_id)
        # Re-wire source → root for the clone if both exist.
        if new_source_id and new_root_id and new_root_id != new_source_id:
            from .layer_stack_compiler import _output_port_of
            if not self.graph.connect(
                    new_source_id,
                    _output_port_of(self.graph, new_source_id),
                    new_root_id, "image"):
                for nid in created_node_ids:
                    self.graph.remove_node(nid)
                return None

        # AUD-4906: a duplicated Precomp must own an independent child
        # stack.  Sharing the original child_stack_id made the two layers
        # aliases of one composition, and deleting either removed the
        # child stack out from under the survivor.
        if layer.layer_type == LAYER_TYPE_PRECOMP and new_root_id:
            old_root = self.graph.get_node(layer.root_node_id)
            old_child_id = (old_root.parameters.get("child_stack_id", "")
                            if old_root is not None else "")
            if old_child_id:
                new_child_id = self._duplicate_layer_stack_tree(
                    old_child_id, new_root_id, created_node_ids)
                if new_child_id is None:
                    for nid in created_node_ids:
                        if self.graph.get_node(nid) is not None:
                            self.graph.remove_node(nid)
                    return None
                # Point the cloned boundary node at the copied stack (the
                # to_dict/from_dict clone carried the original stack id).
                cloned_root = self.graph.get_node(new_root_id)
                if cloned_root is not None:
                    cloned_root.set_parameter("child_stack_id", new_child_id)
            else:
                logger.warning(
                    "Duplicating Precomp layer %r whose root has no "
                    "child_stack_id; the copy will render empty like the "
                    "original.", layer_id)

        new_layer_id = self._new_layer_id(suffix=layer_id)
        new_layer = Layer(
            layer_id=new_layer_id,
            name=f"{layer.name} copy",
            root_node_id=new_root_id or layer.root_node_id,
            source_node_id=new_source_id or layer.source_node_id,
            layer_type=layer.layer_type,
            blend_mode=layer.blend_mode,
            opacity=layer.opacity,
            label_color=layer.label_color,
            enabled=layer.enabled,
            shy=layer.shy,
            in_frame=layer.in_frame,
            out_frame=layer.out_frame,
            managed=layer.managed,
        )
        # Insert directly above the original (plan §6.3: Ctrl+D).
        idx = stack.layer_ids.index(layer_id)
        stack.layer_ids.insert(idx, new_layer_id)
        self.graph.upsert_layer(stack.stack_id, new_layer)

        result = self._recompile(stack)
        after = self._snapshot()
        self._push_undo(
            ACTION_DUPLICATE_LAYER, before, after,
            description or f"Duplicate layer {layer.name!r}", stack.stack_id,
            extra={"created_node_ids": created_node_ids,
                   "created_layer_ids": [new_layer_id],
                   "source_layer_id": layer_id,
                   "conflict": result.conflict,
                   "conflict_reason": result.conflict_reason})
        self._handle_conflict(result, stack.stack_id)
        self.emit_changed(stack.stack_id)
        return new_layer_id

    def _clone_node(self, node_id: str) -> Optional[str]:
        node = self.graph.get_node(node_id)
        if node is None:
            return None
        new = node.__class__.from_dict(node.to_dict())
        # Generate a fresh id (avoid collisions).
        new.id = self._new_node_id(prefix=f"{node.node_type}_dup")
        new.position = (node.position[0] + 40.0, node.position[1] + 40.0)
        # The clone starts unconnected; the caller wires it up.
        self.graph.add_node(new)
        return new.id

    def _duplicate_layer_stack_tree(
            self, stack_id: str, new_output_node_id: str,
            created_node_ids: List[str]) -> Optional[str]:
        """Deep-copy a LayerStack tree for Precomp duplication (AUD-4906).

        Clones every node the stack's layers own (source + root), re-wires
        their internal source→root edges, copies each :class:`Layer` under a
        fresh id (remapping same-stack parent/matte references and dropping
        cross-stack ones), recurses into nested Precomp layers, and
        registers the new stack against ``new_output_node_id`` (the cloned
        boundary Precomp node).  The copy's auto-Merge chain starts empty
        and is rebuilt by the recursive recompile in :meth:`_recompile`.

        Args:
            stack_id: the child stack to duplicate.
            new_output_node_id: the cloned Precomp node that will surface
                the duplicated stack in the parent graph.
            created_node_ids: accumulator (owned by the caller) listing
                every cloned node so the caller can roll back its own
                clones; the helper also appends the nodes it creates.

        Returns:
            The new stack id, or None after removing everything the helper
            created (the caller then rolls back its own cloned nodes).
        """
        import uuid

        stack = self.graph.get_layer_stack(stack_id)
        if stack is None:
            logger.warning(
                "Cannot duplicate unknown layer stack %r (AUD-4906).",
                stack_id)
            return None
        new_stack_id = f"{stack_id}_dup_{uuid.uuid4().hex[:6]}"
        new_stack = LayerStack(
            stack_id=new_stack_id,
            name=f"{stack.name} copy",
            output_node_id=new_output_node_id,
            managed=stack.managed,
        )
        self.graph.set_layer_stack(new_stack)
        created_stacks: List[str] = [new_stack_id]
        from .layer_stack_compiler import _output_port_of

        def _rollback() -> None:
            for sid in reversed(created_stacks):
                if self.graph.get_layer_stack(sid) is not None:
                    self.graph.remove_layer_stack(sid)
            for nid in created_node_ids:
                if self.graph.get_node(nid) is not None:
                    self.graph.remove_node(nid)

        layer_id_map: Dict[str, str] = {}
        new_layers: List[Layer] = []
        for old_layer_id in stack.layer_ids:
            layer = self.graph.get_layer(old_layer_id)
            if layer is None:
                continue
            node_id_map: Dict[str, str] = {}
            for nid in (layer.source_node_id, layer.root_node_id):
                if not nid or nid in node_id_map:
                    continue
                clone_id = self._clone_node(nid)
                if clone_id is None:
                    _rollback()
                    return None
                node_id_map[nid] = clone_id
                created_node_ids.append(clone_id)
            if (layer.source_node_id and layer.root_node_id
                    and layer.root_node_id != layer.source_node_id):
                if not self.graph.connect(
                        node_id_map[layer.source_node_id],
                        _output_port_of(
                            self.graph, node_id_map[layer.source_node_id]),
                        node_id_map[layer.root_node_id], "image"):
                    _rollback()
                    return None
            new_layer = Layer.from_dict(layer.to_dict())
            new_layer.layer_id = self._new_layer_id(suffix=old_layer_id)
            new_layer.root_node_id = node_id_map.get(
                layer.root_node_id, layer.root_node_id)
            new_layer.source_node_id = (node_id_map.get(layer.source_node_id)
                                        if layer.source_node_id else None)
            new_layer.name = f"{layer.name} copy"
            layer_id_map[old_layer_id] = new_layer.layer_id
            new_layers.append(new_layer)
            new_stack.layer_ids.append(new_layer.layer_id)
            self.graph.upsert_layer(new_stack.stack_id, new_layer)
            # Nested Precomp layers recurse so the copy owns its own
            # subtree instead of aliasing the original child stack.
            if layer.layer_type == LAYER_TYPE_PRECOMP:
                cloned_root = self.graph.get_node(new_layer.root_node_id)
                nested_id = (cloned_root.parameters.get("child_stack_id", "")
                             if cloned_root is not None else "")
                if nested_id:
                    new_nested = self._duplicate_layer_stack_tree(
                        nested_id, new_layer.root_node_id, created_node_ids)
                    if new_nested is None:
                        _rollback()
                        return None
                    created_stacks.append(new_nested)
                    cloned_root.set_parameter("child_stack_id", new_nested)

        # Same-stack parent/matte references follow the copied layers;
        # references that pointed outside this stack were invalid for the
        # original as well and are dropped on the copy.
        for new_layer in new_layers:
            if new_layer.parent_layer_id is not None:
                new_layer.parent_layer_id = layer_id_map.get(
                    new_layer.parent_layer_id)
            if new_layer.track_matte_layer_id is not None:
                new_layer.track_matte_layer_id = layer_id_map.get(
                    new_layer.track_matte_layer_id)
        return new_stack_id

    # ── Rename ──────────────────────────────────────────────────────────

    def rename_layer(self, layer_id: str, new_name: str,
                     description: str = "") -> bool:
        return self.set_layer_property(layer_id, "name", new_name,
                                       description=description or "Rename layer",
                                       action_type=ACTION_RENAME_LAYER)

    # ── Set property (visibility/opacity/blend/color/...) ───────────────

    def set_layer_property(self, layer_id: str, prop: str, value: Any,
                           description: str = "",
                           action_type: str = ACTION_SET_LAYER_PROPERTY,
                           mergeable: bool = False) -> bool:
        """Set a single :class:`Layer` attribute and re-project.

        ``prop`` is the attribute name (e.g. ``"enabled"``, ``"opacity"``,
        ``"blend_mode"``, ``"label_color"``, ``"name"``).  When the property
        affects rendering (opacity/blend/enabled/parent) the Merge/Output
        chain is recompiled; pure metadata (name/color) only emits a
        refresh.
        """
        layer = self.graph.get_layer(layer_id)
        if layer is None:
            return False
        # AUD-4907: parent/matte membership checks must run against the
        # stack that actually owns the layer.  Anchoring them to the main
        # stack made legal same-child-stack parents and mattes of nested
        # (Precomp) layers fail validation.
        stack = self._stack_for_layer(layer_id) or self.main_stack()
        if stack is None:
            return False
        mutable_props = {
            "name", "enabled", "solo", "locked", "shy", "label_color",
            "opacity", "blend_mode", "parent_layer_id", "in_frame",
            "out_frame", "collapsed", "track_matte_layer_id",
            "track_matte_mode", "track_matte_invert",
        }
        if prop not in mutable_props or not hasattr(layer, prop):
            return False
        if prop == "name":
            if not isinstance(value, str) or not value.strip():
                return False
            value = value.strip()
        elif prop in {"enabled", "solo", "locked", "shy", "collapsed"}:
            if not isinstance(value, bool):
                return False
        elif prop == "opacity":
            if isinstance(value, bool):
                return False
            try:
                value = float(value)
            except (TypeError, ValueError):
                return False
            if not math.isfinite(value) or not 0.0 <= value <= 1.0:
                return False
        elif prop == "blend_mode":
            if value not in LAYER_BLEND_MODES:
                return False
        elif prop == "track_matte_layer_id":
            if value is not None:
                matte_stack = self._stack_for_layer(value)
                if (value == layer_id or self.graph.get_layer(value) is None
                        or matte_stack is not stack):
                    return False
        elif prop == "track_matte_mode":
            if value not in TRACK_MATTE_MODES:
                return False
        elif prop == "track_matte_invert":
            if not isinstance(value, bool):
                return False
        elif prop in {"in_frame", "out_frame"}:
            if value is not None:
                try:
                    value = int(value)
                except (TypeError, ValueError):
                    return False
                other = layer.out_frame if prop == "in_frame" else layer.in_frame
                if other is not None and ((prop == "in_frame" and value > other)
                                          or (prop == "out_frame" and other > value)):
                    return False
        elif prop == "parent_layer_id" and value is not None:
            if value not in stack.layer_ids or value == layer_id:
                return False
            root = self.graph.get_node(layer.root_node_id)
            parent = self.graph.get_layer(value)
            parent_root = (self.graph.get_node(parent.root_node_id)
                           if parent is not None else None)
            if (root is None or parent_root is None
                    or not getattr(root, "supports_parent_transform", False)
                    or not getattr(parent_root, "supports_parent_transform", False)
                    or self._would_create_parent_cycle(layer_id, value)):
                return False
        before = self._snapshot()
        old_value = getattr(layer, prop)
        setattr(layer, prop, value)
        # Decide whether to recompile.
        render_affecting = prop in (
            "enabled", "solo", "opacity", "blend_mode",
            "parent_layer_id", "in_frame", "out_frame",
            "track_matte_layer_id", "track_matte_mode", "track_matte_invert")
        result = CompileResult(ok=True)
        if render_affecting:
            result = self._recompile(stack)
        after = self._snapshot()
        extra = {"layer_id": layer_id, "prop": prop,
                 "old_value": old_value, "new_value": value,
                 "conflict": result.conflict,
                 "conflict_reason": result.conflict_reason}
        merge_key = None
        if mergeable:
            merge_key = f"{action_type}:{layer_id}:{prop}"
        self._push_undo(action_type, before, after,
                        description or f"Set {prop}", stack.stack_id,
                        extra=extra, merge_key=merge_key)
        self._handle_conflict(result, stack.stack_id)
        self.emit_changed(stack.stack_id)
        return True

    def set_layer_visibility(self, layer_id: str, enabled: bool) -> bool:
        return self.set_layer_property(layer_id, "enabled", enabled,
                                       description="Toggle layer visibility")

    def set_layer_locked(self, layer_id: str, locked: bool) -> bool:
        return self.set_layer_property(layer_id, "locked", locked,
                                       description="Toggle layer lock")

    def set_layer_solo(self, layer_id: str, solo: bool) -> bool:
        return self.set_layer_property(layer_id, "solo", solo,
                                       description="Toggle layer solo")

    def set_layer_opacity(self, layer_id: str, opacity: float) -> bool:
        return self.set_layer_property(
            layer_id, "opacity", opacity,
            description="Change layer opacity", mergeable=True)

    def set_layer_blend_mode(self, layer_id: str, blend_mode: str) -> bool:
        return self.set_layer_property(
            layer_id, "blend_mode", blend_mode,
            description="Change blend mode")

    def set_layer_track_matte(self, layer_id: str,
                              matte_layer_id: Optional[str],
                              mode: str = "alpha",
                              invert: bool = False,
                              description: str = "") -> bool:
        """Assign all Track Matte fields as one undoable layer operation."""
        layer = self.graph.get_layer(layer_id)
        stack = self._stack_for_layer(layer_id)
        matte = (self.graph.get_layer(matte_layer_id)
                 if matte_layer_id is not None else None)
        if (layer is None or stack is None or mode not in TRACK_MATTE_MODES
                or not isinstance(invert, bool)
                or (matte_layer_id is not None and
                    (matte is None or matte_layer_id == layer_id or
                     self._stack_for_layer(matte_layer_id) is not stack))):
            return False
        before = self._snapshot()
        layer.track_matte_layer_id = matte_layer_id
        layer.track_matte_mode = mode
        layer.track_matte_invert = invert
        result = self._recompile(stack)
        if result.conflict:
            self._restore(before)
            self._handle_conflict(result, stack.stack_id)
            return False
        after = self._snapshot()
        self._push_undo(
            ACTION_SET_LAYER_TRACK_MATTE, before, after,
            description or "Set layer Track Matte", stack.stack_id,
            extra={"layer_id": layer_id,
                   "matte_layer_id": matte_layer_id,
                   "mode": mode,
                   "invert": invert})
        self.emit_changed(stack.stack_id)
        return True

    def set_layer_label_color(self, layer_id: str, color: str) -> bool:
        return self.set_layer_property(
            layer_id, "label_color", color,
            description="Change layer label color")

    # ── Parent (plan §7) ───────────────────────────────────────────────

    def set_layer_parent(self, layer_id: str,
                         parent_layer_id: Optional[str],
                         description: str = "") -> bool:
        """Set/clear AE-style parenting with ancestor/cycle checks."""
        # AUD-4907: resolve the owning stack — a nested (Precomp child
        # stack) layer must parent inside its own stack, not the main one.
        stack = self._stack_for_layer(layer_id) or self.main_stack()
        if stack is None:
            return False
        layer = self.graph.get_layer(layer_id)
        if layer is None:
            return False
        # No-op if unchanged.
        if layer.parent_layer_id == parent_layer_id:
            return True
        # Capability check only matters when *setting* a parent — clearing
        # is always allowed (the node may have lost support after the
        # parent was set, e.g. its root was swapped to an effect node).
        if parent_layer_id is not None:
            root = self.graph.get_node(layer.root_node_id)
            if root is None or not getattr(
                    root, "supports_parent_transform", False):
                return False
            # The parent layer's root must also support parenting.
            parent_layer = self.graph.get_layer(parent_layer_id)
            if parent_layer is None or parent_layer_id not in stack.layer_ids:
                return False
            parent_root = self.graph.get_node(parent_layer.root_node_id)
            if parent_root is None or not getattr(
                    parent_root, "supports_parent_transform", False):
                return False
        # Reject self / cycle.
        if parent_layer_id is not None:
            if parent_layer_id == layer_id:
                return False
            if self._would_create_parent_cycle(layer_id, parent_layer_id):
                return False
        return self.set_layer_property(
            layer_id, "parent_layer_id", parent_layer_id,
            description=description or "Set layer parent",
            action_type=ACTION_SET_LAYER_PARENT)

    def _would_create_parent_cycle(self, layer_id: str,
                                   candidate_parent_id: str) -> bool:
        """Walk up from candidate parent; if we reach layer_id it's a cycle."""
        layers = self.layers_map()
        cur = candidate_parent_id
        seen = set()
        while cur is not None and cur in layers:
            if cur == layer_id:
                return True
            if cur in seen:  # defensive against pre-existing cycles
                return True
            seen.add(cur)
            cur = layers[cur].parent_layer_id
        return False

    # ── Rebuild (force-compile) ─────────────────────────────────────────

    def rebuild(self, description: str = "") -> CompileResult:
        """Force a full recompile of the main stack."""
        stack = self.main_stack()
        if stack is None:
            return CompileResult(ok=True)
        before = self._snapshot()
        result = self._recompile(stack)
        after = self._snapshot()
        self._push_undo(ACTION_REBUILD_STACK, before, after,
                        description or "Rebuild layer stack", stack.stack_id,
                        extra={"conflict": result.conflict,
                               "conflict_reason": result.conflict_reason})
        self._handle_conflict(result, stack.stack_id)
        self.emit_changed(stack.stack_id)
        return result

    def sync_from_node_graph(self, repair: bool = False) -> CompileResult:
        """Reconcile LayerStack awareness with the current NodeGraph.

        Node editing is intentionally not allowed to silently rewrite a
        managed layer stack.  This method therefore performs a dry-run by
        default: it reports manual rewires, missing roots and topology drift
        through the normal ``CompileResult``/conflict signal.  Callers that
        own an explicit repair transaction may pass ``repair=True`` to apply
        the compiler's safe changes without creating a second Undo command.
        """
        stack = self.main_stack()
        if stack is None:
            return CompileResult(ok=True)
        if not stack.managed:
            # Free-graph projections are references, not owners. Their
            # topology is authoritative in NodeGraph and must not be rebuilt
            # merely because the node editor changed.
            return CompileResult(ok=True,
                                 merge_node_ids=list(
                                     stack.generated_merge_node_ids))

        old_merge_ids = list(stack.generated_merge_node_ids)
        result = self.compiler.compile(stack, self.layers_map())
        if result.conflict:
            self._handle_conflict(result, stack.stack_id)
            return result
        if repair:
            failures = self._apply_changes(result.changes)
            if failures:
                result.warnings.extend(failures)
            stack.generated_merge_node_ids = list(result.merge_node_ids)
            if result.changes or old_merge_ids != result.merge_node_ids:
                self.emit_changed(stack.stack_id)
            return result

        if result.changes:
            result.warnings.append(
                "NodeGraph topology differs from the managed LayerStack; "
                "the graph edit was preserved and was not silently overwritten.")
        self._handle_conflict(result, stack.stack_id)
        return result

    # ── Legacy projection (plan §1.2.7 / §12.2) ─────────────────────────

    def project_from_node_graph(self, output_node_id: str = "") -> int:
        """Build a LayerStack from the existing free node graph.

        For projects saved before the layer system existed, this walks
        upstream from the Output node and creates one ``external`` layer
        per top-level branch root.  The result is a *projection* —
        ``managed=True`` is set only for direct Output-feeding chains the
        compiler can safely own; everything else stays ``external``.

        Returns the number of layers created.
        """
        # Locate the output node.
        out_id = output_node_id
        if not out_id:
            for node in self.graph.nodes.values():
                if node.node_type == "output":
                    out_id = node.id
                    break
        if not out_id:
            return 0
        stack = self.ensure_main_stack(out_id)
        # Projection is a migration operation, not an append operation.
        # Re-running it on an already projected graph used to duplicate every
        # layer row and made a later promotion ambiguous.
        if stack.layer_ids:
            return 0
        # A projected free graph remains non-managed: the free graph already
        # owns its Merge/Output wiring and the layer system must not rewrite
        # it. The exception is one of the pristine composition graphs created
        # by Composite itself. These graphs are deliberately simple source →
        # (optional Topos Color) → Output chains, so treating the existing
        # chain as the first managed layer is safe and is what lets a normal
        # user drop the first extra asset without having to wire anything in
        # the node editor.
        # Other free graphs remain projections and retain their Nuke-style
        # topology untouched.
        # Walk upstream: collect the direct inputs to Output, then for each
        # follow the chain back to its ultimate source.
        created = 0
        # Direct feeders of Output, in connection order.
        feeders: List[str] = []
        for conn in self.graph.connections:
            if conn.dst_node_id == out_id and conn.dst_port_name == "image":
                feeders.append(conn.src_node_id)
        manage_default_graph = self._is_default_timeline_graph(
            out_id, feeders)
        stack.managed = manage_default_graph
        # If there's a Merge chain, treat the merge feeds as the layer list.
        # Otherwise each feeder is its own (external) layer.
        for src_id in feeders:
            src_node = self.graph.get_node(src_id)
            if src_node is None:
                continue
            # Follow back to a root source for a friendlier name.
            root_id = self._trace_to_root(src_id)
            root_node = self.graph.get_node(root_id)
            layer_id = self._new_layer_id()
            layer = Layer(
                layer_id=layer_id,
                name=("Timeline Clip Input"
                      if getattr(root_node, "node_type", "") ==
                      "timeline_clip_input"
                      else self._default_layer_name(root_id)),
                root_node_id=src_id,  # what actually feeds Output
                source_node_id=root_id if root_id != src_id else None,
                layer_type=("media" if manage_default_graph else "external"),
                managed=manage_default_graph,
                collapsed=True,
            )
            stack.layer_ids.append(layer_id)
            self.graph.upsert_layer(stack.stack_id, layer)
            created += 1
        # A pristine built-in graph is now managed immediately; its one base
        # layer is already wired correctly and later drops can therefore grow
        # the Merge chain. All other projections remain managed=False and are
        # intentionally left untouched.
        # Projection is NOT pushed as a user undo step (it runs at load).
        self.emit_changed(stack.stack_id)
        return created

    def _is_default_timeline_graph(self, output_node_id: str,
                                   feeders: List[str]) -> bool:
        """Return whether the graph is a pristine built-in composition.

        This name is retained for compatibility with callers that used the
        original timeline-only helper.  Composite has three built-in simple
        graph shapes that can safely become the base of a managed LayerStack:

        * ``TimelineClipInput → ToposColor → Output``;
        * ``ImageInput → ToposColor → Output``;
        * ``Constant → Output`` for an empty comp clip.

        We intentionally match the complete node/connection topology instead
        of adopting arbitrary one-chain user graphs.  That keeps the Nuke
        free-graph workflow intact while making the normal AE-style workflow
        work for both video and image compositions.
        """
        if len(feeders) != 1:
            return False
        output = self.graph.get_node(output_node_id)
        if output is None or getattr(output, "node_type", "") != "output":
            return False
        connections = {
            (conn.src_node_id, conn.src_port_name,
             conn.dst_node_id, conn.dst_port_name)
            for conn in self.graph.connections
        }

        if len(self.graph.nodes) == 2:
            # Empty comp clip: Constant → Output.
            source = self.graph.get_node(feeders[0])
            if source is None or getattr(source, "node_type", "") != "constant":
                return False
            return connections == {
                (source.id, "result", output.id, "image"),
            }

        if len(self.graph.nodes) != 3:
            return False

        color_nodes = [
            node for node in self.graph.nodes.values()
            if getattr(node, "node_type", "") == "topos_color"
        ]
        if len(color_nodes) != 1 or feeders[0] != color_nodes[0].id:
            return False
        color = color_nodes[0]
        source_nodes = [
            node for node in self.graph.nodes.values()
            if getattr(node, "node_type", "") in {
                "timeline_clip_input", "image_input"
            }
        ]
        if len(source_nodes) != 1:
            return False
        source = source_nodes[0]
        source_port = "output" if source.node_type == "timeline_clip_input" \
            else "image"
        return connections == {
            (source.id, source_port, color.id, "image"),
            (color.id, "result", output.id, "image"),
        }

    def _trace_to_root(self, node_id: str) -> str:
        """Follow the first image-input chain back to a source node."""
        cur = node_id
        seen = set()
        while cur and cur not in seen:
            seen.add(cur)
            node = self.graph.get_node(cur)
            if node is None:
                return cur
            # If node has no image inputs, it's a root.
            image_inputs = [p for p in node.inputs
                            if getattr(p, "data_type", "") == "image"]
            if not image_inputs:
                return cur
            upstream = None
            for conn in self.graph.connections:
                if (conn.dst_node_id == cur
                        and conn.dst_port_name == image_inputs[0].name):
                    upstream = conn.src_node_id
                    break
            if upstream is None:
                return cur
            cur = upstream
        return cur

    def _default_layer_name(self, node_id: str) -> str:
        node = self.graph.get_node(node_id)
        if node is not None:
            try:
                return node.display_title or node.node_title or "Layer"
            except Exception:
                return "Layer"
        return "Layer"

    # ── Compile + apply ─────────────────────────────────────────────────

    def _recompile(self, stack: LayerStack) -> CompileResult:
        """Run the compiler and apply its changes to the graph.

        On conflict, no graph mutation happens — the caller surfaces it.
        """
        layers = self.layers_map()
        # Precomp stacks are ordinary LayerStacks whose output surface is a
        # PrecompNode input.  Compile children first so the parent sees a
        # valid nested image path in the same graph evaluation pass.
        result = self._recompile_stack_recursive(stack, layers, set())
        # Surface a missing/unknown Output node explicitly.  Without this
        # the compiler silently emits connect changes that graph.connect()
        # refuses (dst node missing), so the layer appears created but the
        # canvas stays blank with no feedback (review BUG-5).
        if stack.output_node_id and not self.graph.get_node(stack.output_node_id):
            result.warnings.append(
                f"Output node {stack.output_node_id!r} does not exist; the "
                f"layer chain cannot be wired to the composition output.")
        elif not stack.output_node_id and stack.layer_ids:
            result.warnings.append(
                "Layer stack has no output_node_id; add an Output node before "
                "dropping media.")
        if not result.ok:
            return result  # conflict — leave the graph alone
        gen_before = self.graph.generation
        # Apply changes; collect any rejections so they surface as warnings
        # rather than silently leaving the layer unwired (finding #11).
        failures = self._apply_changes(result.changes)
        if failures:
            result.warnings.extend(failures)
        # P0-05: layer state that does not alter topology (in_frame/out_frame
        # frame gates) still changes rendering — bump the graph generation so
        # the renderer's snapshot-skip check re-evaluates.
        if self.graph.generation == gen_before:
            self.graph.bump_generation()
        # Update the stack's owned merge list.
        stack.generated_merge_node_ids = list(result.merge_node_ids)
        return result

    def _recompile_stack_recursive(self, stack: LayerStack,
                                   layers: Dict[str, Layer],
                                   active: set) -> CompileResult:
        """Compile a stack and any nested Precomp stacks exactly once."""
        if stack.stack_id in active:
            result = CompileResult(ok=False, conflict=True,
                                   conflict_reason=(
                                       f"Precomp stack cycle detected at "
                                       f"{stack.stack_id!r}."))
            return result
        active = set(active)
        active.add(stack.stack_id)
        aggregate = CompileResult(ok=True)
        for lid in stack.layer_ids:
            layer = layers.get(lid)
            node = self.graph.get_node(layer.root_node_id) if layer else None
            if node is None or getattr(node, "node_type", "") != "precomp":
                continue
            child_id = node.parameters.get("child_stack_id", "")
            child = self.graph.get_layer_stack(child_id) if child_id else None
            if child is None:
                aggregate.warnings.append(
                    f"Precomp layer {lid!r} has no child stack.")
                continue
            child_result = self._recompile_stack_recursive(child, layers, active)
            aggregate.warnings.extend(child_result.warnings)
            if child_result.conflict:
                return child_result
            # AUD-4905: child changes must be aggregated (children first,
            # parents after) so _recompile() applies them in order together
            # with the parent and the whole nested compile stays one
            # undoable graph mutation.  Dropping them left the child stack
            # with a rewritten generated_merge_node_ids list pointing at
            # nodes that were never created/connected.
            aggregate.changes.extend(child_result.changes)

        parent_result = self.compiler.compile(stack, layers)
        aggregate.warnings.extend(parent_result.warnings)
        aggregate.merge_node_ids = parent_result.merge_node_ids
        # Keep child and parent topology edits in one transaction.  Applying
        # the parent here would make _recompile() apply it a second time,
        # which is especially dangerous for newly-created Merge nodes.
        aggregate.changes.extend(parent_result.changes)
        aggregate.ok = parent_result.ok
        aggregate.conflict = parent_result.conflict
        aggregate.conflict_reason = parent_result.conflict_reason
        if parent_result.ok:
            stack.generated_merge_node_ids = list(parent_result.merge_node_ids)
        return aggregate

    def _apply_changes(self, changes: List[CompileChange]) -> List[str]:
        """Apply compiler changes; return a list of failure descriptions.

        Failures (graph.connect refused, node missing, ...) used to be
        silently swallowed, leaving the layer registered but unwired with
        no feedback (review finding #11).  We now collect them so callers
        can surface warnings and the caller can decide whether to log or
        signal conflict.  Parent-projection (``__parent__``) and benign
        "already removed" cases are not treated as failures.
        """
        failures: List[str] = []
        for ch in changes:
            if ch.kind == "create_node":
                if ch.node_type == MERGE_NODE_TYPE:
                    from src.features.composite.nodes.blend_node import MergeNode
                    node = MergeNode(node_id=ch.node_id)
                else:
                    node = self._create_node(ch.node_type, ch.node_id)
                if node is None:
                    failures.append(
                        f"create_node: could not build {ch.node_type!r} "
                        f"({ch.node_id!r}).")
                    continue
                self.graph.add_node(node)
            elif ch.kind == "remove_node":
                removed = self.graph.remove_node(ch.node_id)
                if removed is None and self.graph.get_node(ch.node_id) is not None:
                    failures.append(
                        f"remove_node: {ch.node_id!r} still present after "
                        f"removal attempt.")
            elif ch.kind == "connect":
                if ch.src_port_name == "__parent__":
                    node = self.graph.get_node(ch.src_node_id)
                    if node is None:
                        failures.append(
                            f"parent set: target node {ch.src_node_id!r} "
                            f"missing.")
                    else:
                        setter = getattr(self.graph, "set_node_parent", None)
                        if callable(setter):
                            ok, error = setter(
                                ch.src_node_id, ch.dst_port_name or None)
                            if not ok:
                                failures.append(
                                    f"parent set {ch.src_node_id!r} rejected: "
                                    f"{error or 'invalid parent'}")
                        else:
                            node.parent_id = ch.dst_port_name or None
                else:
                    ok = self.graph.connect(
                        ch.src_node_id, ch.src_port_name,
                        ch.dst_node_id, ch.dst_port_name)
                    if not ok:
                        failures.append(
                            f"connect {ch.src_node_id}:{ch.src_port_name} → "
                            f"{ch.dst_node_id}:{ch.dst_port_name} was rejected "
                            f"(missing node/port, type mismatch, or cycle).")
            elif ch.kind == "set_parameter":
                if not ch.node_id or not ch.param_name:
                    failures.append("set_parameter: missing node or parameter")
                    continue
                setter = getattr(self.graph, "set_node_parameter", None)
                ok = (setter(ch.node_id, ch.param_name, ch.value)
                      if callable(setter) else False)
                if not ok:
                    node = self.graph.get_node(ch.node_id)
                    if node is None:
                        failures.append(
                            f"set_parameter: node {ch.node_id!r} missing.")
                    else:
                        try:
                            node.set_parameter(ch.param_name, ch.value)
                        except Exception as exc:
                            failures.append(
                                f"set_parameter {ch.node_id}:{ch.param_name} "
                                f"failed: {exc}")
            elif ch.kind == "disconnect":
                # Disconnect is idempotent — "already disconnected" is fine
                # and not recorded as a failure.
                self.graph.disconnect(ch.src_node_id, ch.src_port_name,
                                      ch.dst_node_id, ch.dst_port_name)
        if failures:
            logger.warning(
                "LayerStack apply had %d failure(s): %s",
                len(failures), "; ".join(failures))
        return failures

    # ── Undo plumbing ───────────────────────────────────────────────────

    def _snapshot(self) -> Dict[str, Any]:
        """Capture the layer state for undo.

        Snapshots the full set of stacks and layers as raw dicts so that
        undo/redo can restore them deterministically without aliasing.
        The complete node set is also captured (as ``to_dict`` payloads) so
        that controller-created nodes (auto-transforms, auto-merges) are
        recreated/removed on restore — a media drop's undo must delete the
        transform it created, not just the layer + connections.
        """
        return {
            "stacks": {sid: s.to_dict() for sid, s in
                       self.graph.get_layer_stacks().items()},
            "layers": {l.layer_id: l.to_dict() for l in
                       self.graph.get_all_layers() if hasattr(l, "to_dict")},
            "connections": [
                {"src_node_id": c.src_node_id, "src_port_name": c.src_port_name,
                 "dst_node_id": c.dst_node_id, "dst_port_name": c.dst_port_name}
                for c in list(self.graph.connections)
            ],
        # Full node snapshot keyed by node id.  Using to_dict lets us
        # rebuild via Node.from_dict without aliasing live objects.
        "nodes": {nid: n.to_dict() for nid, n in
                  self.graph.nodes.items()},
        # Expression manager state (graph-level, not per-node).  Expressions
        # live on NodeGraph._expression_manager, not Node._expressions, so
        # the node snapshot above does not capture them — without this an
        # undo that removes an expression-bearing node would silently drop
        # the expression dependency edges (review BUG-1).
        "expressions": {
            f"{nid}.{pname}": expr
            for (nid, pname), expr in getattr(
                getattr(self.graph, "_expression_manager", None),
                "_expressions", {}).items()
        },
        }

    def _restore(self, snapshot: Dict[str, Any]) -> None:
        """Restore layer + node + connection state from a snapshot.

        Restores stacks/layers, then reconciles the node set (adding back
        nodes that disappeared and dropping nodes the snapshot no longer
        has), and finally rewires connections.  This is an undo/redo path,
        not a hot loop, so we favour correctness over speed.
        """
        from .layer_model import LayerStack, Layer
        from .node_engine import NODE_REGISTRY
        # Stacks + layers.
        self.graph._layer_stacks.clear()
        for sid, sd in (snapshot.get("stacks") or {}).items():
            try:
                self.graph._layer_stacks[sid] = LayerStack.from_dict(sd)
            except (KeyError, TypeError):
                continue
        self.graph._layers.clear()
        for lid, ld in (snapshot.get("layers") or {}).items():
            try:
                self.graph._layers[lid] = Layer.from_dict(ld)
            except (KeyError, TypeError):
                continue

        # Reconcile node set with the snapshot.  We never touch nodes the
        # snapshot didn't capture (those are external/user-owned and were
        # not part of this controller's mutation).
        snap_nodes = snapshot.get("nodes") or {}
        if "nodes" in snapshot:
            # Remove nodes that the snapshot does not have.
            for nid in list(self.graph.nodes.keys()):
                if nid not in snap_nodes:
                    self.graph.remove_node(nid)
            # Add back / update nodes present in the snapshot.  Existing
            # nodes are rebuilt from their dict to reset parameters/ports.
            for nid, nd in snap_nodes.items():
                node_cls = NODE_REGISTRY.get(nd.get("type"))
                if node_cls is None:
                    continue
                # If it exists already, remove first so from_dict produces
                # a clean object (positions/params are part of the dict).
                if nid in self.graph.nodes:
                    # Preserve only if the live object already matches —
                    # otherwise rebuild.  Simplest correct path: rebuild.
                    self.graph.nodes.pop(nid, None)
                try:
                    rebuilt = node_cls.from_dict(nd)
                except Exception:
                    continue
                self.graph.add_node(rebuilt)

        # Connections: rebuild from snapshot.
        self.graph.connections = []
        for conn in (snapshot.get("connections") or []):
            try:
                self.graph.connect(conn["src_node_id"], conn["src_port_name"],
                                   conn["dst_node_id"], conn["dst_port_name"])
            except Exception as e:
                # tolerate missing nodes during partial restore
                logger.warning("LayerStackController: restore connect "
                               "%s.%s -> %s.%s failed: %s",
                               conn["src_node_id"], conn["src_port_name"],
                               conn["dst_node_id"], conn["dst_port_name"], e)

        # Re-hydrate expression dependencies from the snapshot.  Expressions
        # live on NodeGraph._expression_manager (graph-level), not on
        # Node._expressions, so the node rebuild above does not restore
        # them — mirror NodeGraph.from_dict by replaying each expression
        # through set_expression so dependency/cycle state is rebuilt.
        # (Plan §10: undo must restore a consistent world, not just data.)
        expr_manager = getattr(self.graph, "_expression_manager", None)
        if expr_manager is not None:
            # ExpressionManager has no public clear(); reset its internal
            # stores directly (the same three dicts __init__ populates).
            for attr in ("_expressions", "_dependencies", "_dependents"):
                store = getattr(expr_manager, attr, None)
                if isinstance(store, dict):
                    store.clear()
            for key, expr in (snapshot.get("expressions") or {}).items():
                # key format: "node_id.param_name"
                if "." not in key:
                    continue
                nid, pname = key.split(".", 1)
                if nid not in self.graph.nodes:
                    continue  # node gone in this snapshot variant
                try:
                    expr_manager.set_expression(nid, pname, expr)
                except Exception as e:
                    logger.warning("LayerStackController: restore expression "
                                   "%s.%s failed: %s", nid, pname, e)

    def _push_undo(self, action_type: str, before: Dict[str, Any],
                   after: Dict[str, Any], description: str,
                   stack_id: str, extra: Optional[Dict[str, Any]] = None,
                   merge_key: Optional[str] = None) -> None:
        if self.undo_stack is None:
            return
        try:
            from .undo_stack import UndoAction
        except Exception:
            return
        data = {
            "stack_id": stack_id,
            "before": before,
            "after": after,
            "extra": extra or {},
        }
        action = UndoAction(
            action_type,
            data,
            undo_fn=lambda d: self._restore(d["before"]),
            redo_fn=lambda d: self._restore(d["after"]),
            description=description,
            merge_key=merge_key,
        )
        # Always keep structural layer actions; never auto-compress them.
        action.is_compressed = False
        self.undo_stack.push(action)

    def _handle_conflict(self, result: CompileResult, stack_id: str) -> None:
        if result.conflict:
            logger.warning("Layer stack conflict on %s: %s",
                           stack_id, result.conflict_reason)
            if _HAVE_QT and self.layer_stack_conflict is not None:
                self.layer_stack_conflict.emit(
                    stack_id, result.conflict_reason or "")

    # ── ID generation ───────────────────────────────────────────────────

    def _new_layer_id(self, suffix: str = "") -> str:
        import uuid
        base = "layer"
        if suffix:
            # Strip non-alphanumeric to keep ids filesystem/token-safe.
            clean = "".join(c for c in suffix if c.isalnum())
            base = f"{base}_{clean}"[:24]
        cand = f"{base}_{uuid.uuid4().hex[:6]}"
        # Avoid collisions with existing layer ids.  AUD-4910: the previous
        # guard also tested cand against the full Layer object list, where a
        # str membership test was always False.  ``get_layer`` (the _layers
        # id registry) is the authoritative and strictly stronger check.
        while self.graph.get_layer(cand) is not None:
            cand = f"{base}_{uuid.uuid4().hex[:6]}"
        return cand

    def _new_node_id(self, prefix: str = "n") -> str:
        import uuid
        for _ in range(32):
            cand = f"{prefix}_{uuid.uuid4().hex[:6]}"
            if self.graph.get_node(cand) is None:
                return cand
        return f"{prefix}_{uuid.uuid4().hex[:8]}"
