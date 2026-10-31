"""Compile a :class:`LayerStack` ordering into a Merge/Output chain.

The compiler is the **only** component that translates layer state into
node connections (plan §1.2.1).  It reads the stack ordering, computes the
required Merge-node topology, recycles the stack's existing auto-merge
nodes, and writes the resulting connections back onto the
:class:`NodeGraph`.  Layer parenting, visibility and opacity are also
projected onto the matching Transform/Merge nodes here so a single render
pass sees a consistent picture.

Key invariants (plan §4):

* ``layer_ids[0]`` is the foreground.  For ``[A, B, C]`` the chain is
  ``Merge1(A, B) → Merge2(Merge1, C) → Output``.
* Merge input port names are ``A`` (foreground) and ``B`` (background) —
  the actual :class:`MergeNode` port names, **not** the
  "Foreground"/"Background" labels used in the plan doc (those are
  illustrative only; locked by ``tests/composite/test_merge_node.py``).
* Re-running the compiler must never create duplicate Merge nodes:
  existing entries in ``stack.generated_merge_node_ids`` are reused, only
  the shortfall is created, and only the surplus is removed.
* Connection changes are batched into a single undo command by the caller
  (the controller), not here.  The compiler returns a structured
  :class:`CompileResult` describing what changed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .layer_model import (
    Layer,
    LayerStack,
    LAYER_BLEND_MODE_TO_NODE,
)

logger = logging.getLogger(__name__)

# Merge node port names (the real ones on MergeNode — see
# tests/composite/test_merge_node.py for the lock test).
MERGE_PORT_FOREGROUND = "A"
MERGE_PORT_BACKGROUND = "B"
MERGE_OUTPUT_PORT = "result"
OUTPUT_INPUT_PORT = "image"
MERGE_NODE_TYPE = "merge"


@dataclass
class _CompileCtx:
    """Per-compile caches to avoid repeated full-graph scans (perf finding #8).

    Built once at the top of :meth:`LayerStackCompiler.compile` and threaded
    through the helpers that previously each rebuilt these structures.  The
    compiler stays stateless: the context is a local, not instance, field.
    """

    # Nodes the compiler is allowed to rewire: auto-merge ids + output id.
    managed_node_ids: set = field(default_factory=set)
    # (dst_node_id, dst_port_name) -> src_node_id, for O(1) input-source
    # lookups during conflict detection (replaces _input_source's scan).
    input_index: Dict[Tuple[str, str], str] = field(default_factory=dict)
    # The stack being compiled (for output-id checks in _is_managed_connection).
    output_node_id: str = ""


@dataclass
class CompileChange:
    """A single connection-level change produced by the compiler.

    Describing changes as ``Connect`` / ``Disconnect`` records lets the
    controller wrap them in one undo command (plan §10) and lets the
    validator diff "intended" vs "actual" connections to detect when the
    user has manually rewired an auto-Merge (plan §4.5).
    """

    kind: str  # "connect" | "disconnect" | "create_node" | "remove_node"
    # For connect/disconnect:
    src_node_id: Optional[str] = None
    src_port_name: Optional[str] = None
    dst_node_id: Optional[str] = None
    dst_port_name: Optional[str] = None
    # For create_node/remove_node:
    node_id: Optional[str] = None
    node_type: Optional[str] = None
    # For parameter projection changes (AUD-4900: projection is always
    # recorded — for existing nodes as well as newly-created ones — and
    # applied by the controller inside its undo transaction).
    param_name: Optional[str] = None
    value: Any = None


@dataclass
class CompileResult:
    """Structured outcome of :meth:`LayerStackCompiler.compile`.

    ``merge_node_ids`` is the post-compile ordered list of auto-merge node
    IDs (foreground-first, i.e. ``merge_node_ids[0]`` merged the topmost
    two layers).  The controller writes this back into
    ``stack.generated_merge_node_ids``.
    """

    ok: bool
    merge_node_ids: List[str] = field(default_factory=list)
    changes: List[CompileChange] = field(default_factory=list)
    # Non-fatal issues (e.g. a layer's root node vanished) — surfaced to
    # the controller so it can mark the layer as broken without aborting.
    warnings: List[str] = field(default_factory=list)
    # When the compiler detected a topology it cannot safely rewrite
    # (plan §4.5 / §8.3) it sets ``conflict=True`` and leaves the graph
    # untouched; the controller asks the user how to proceed.
    conflict: bool = False
    conflict_reason: Optional[str] = None


class LayerStackCompiler:
    """Compile layer ordering → Merge/Output connections.

    Stateless apart from the graph reference; safe to keep one instance
    per composite page.  All mutations go through the public :meth:`compile`
    entry point, which returns a :class:`CompileResult` instead of raising
    on recoverable issues (missing layer, missing node, ...).
    """

    def __init__(self, graph: Any) -> None:
        self.graph = graph

    # ── Public API ──────────────────────────────────────────────────────

    def compile(self, stack: LayerStack,
                layers: Dict[str, Layer]) -> CompileResult:
        """Rebuild the Merge/Output chain for ``stack``.

        Args:
            stack: the LayerStack to compile (mutated in place only through
                the returned result — the caller applies changes).
            layers: ``layer_id -> Layer`` map for resolving root nodes.
                Layers not present in this map are skipped with a warning.

        Returns:
            :class:`CompileResult`.  When ``conflict`` is True the graph is
            unchanged and the controller must surface the conflict.
        """
        result = CompileResult(ok=True)
        layer_ids = [lid for lid in stack.layer_ids
                     if self._layer_is_live(lid, layers, result)]
        # A Track Matte source is a control layer, not a visible layer in
        # the final stack.  Keep it in ``layers`` for mask resolution, but
        # remove it from the foreground/background chain itself.
        matte_source_ids = {
            layer.track_matte_layer_id
            for layer in (layers.get(lid) for lid in stack.layer_ids)
            if layer is not None and layer.track_matte_layer_id
        }
        if matte_source_ids:
            layer_ids = [lid for lid in layer_ids
                         if lid not in matte_source_ids]

        if not stack.managed:
            # Free-graph mode (plan §8.2): compiler is a no-op.  The stack
            # is purely a reference view; reordering does not reconnect.
            result.merge_node_ids = list(stack.generated_merge_node_ids)
            return result

        # Build per-compile caches once (perf finding #8): the managed-node
        # id set and a (dst_node, dst_port) -> src_node input index.  These
        # used to be rebuilt inside _is_managed_connection / _input_source
        # on every call, turning reconcile into O(connections × merges).
        ctx = _CompileCtx(output_node_id=stack.output_node_id)
        ctx.managed_node_ids.update(stack.generated_merge_node_ids)
        if stack.output_node_id:
            ctx.managed_node_ids.add(stack.output_node_id)
        for conn in self.graph.connections:
            ctx.input_index[(conn.dst_node_id, conn.dst_port_name)] = \
                conn.src_node_id

        # Resolve root node ids in stack order.  Skip layers whose root is
        # gone — but record the skip so the controller can mark them.
        root_ids: List[str] = []
        # Keep the layer ids paired with root_ids.  Using the unfiltered
        # stack list here makes a disabled/solo layer shift opacity/blend
        # projection onto the wrong Merge node.
        live_layer_ids: List[str] = []
        for lid in layer_ids:
            layer = layers.get(lid)
            if layer is None or not self.graph.get_node(layer.root_node_id):
                result.warnings.append(
                    f"Layer {lid!r} has a missing root node; skipped.")
                continue
            root_ids.append(layer.root_node_id)
            live_layer_ids.append(lid)

        # Detect manual rewiring of an existing auto-Merge before we touch
        # anything (plan §4.5).  If the user broke the chain we bail out.
        conflict = self._detect_chain_conflict(stack, root_ids, result, ctx)
        if conflict:
            return result

        # Ensure the right number of auto-merge nodes exist, recycling the
        # stack's existing ones.  Only the diff is recorded as changes so
        # the controller can undo precisely.
        merge_ids = self._ensure_merge_nodes(stack, root_ids, result,
                                             live_layer_ids)
        result.merge_node_ids = merge_ids

        # Compute the intended connection set and reconcile it with the
        # current graph, emitting connect/disconnect changes.
        self._apply_connections(stack, root_ids, merge_ids, result, ctx,
                                 live_layer_ids, layers)

        # Project layer-level appearance (opacity/blend/visibility/parent)
        # onto the matching nodes.  Failures here are non-fatal warnings.
        self._project_layer_state(live_layer_ids, layers, merge_ids, result)

        return result

    # ── Helpers ─────────────────────────────────────────────────────────

    def _layer_is_live(self, layer_id: str, layers: Dict[str, Layer],
                       result: CompileResult) -> bool:
        """Return True if the layer should participate in the chain.

        ``solo`` overrides visibility (plan §6.3): when any layer is soloed,
        only soloed layers render.  ``enabled=False`` layers drop out.
        """
        layer = layers.get(layer_id)
        if layer is None:
            result.warnings.append(
                f"Layer {layer_id!r} is referenced by the stack but not "
                f"present in the layer map; skipped.")
            return False
        if not layer.enabled:
            return False
        # Solo handling: if any layer is soloed, non-solo layers drop out.
        any_solo = any(l.solo for l in layers.values())
        if any_solo and not layer.solo:
            return False
        return True

    def _ensure_merge_nodes(self, stack: LayerStack,
                            root_ids: List[str],
                            result: CompileResult,
                            layer_ids: Optional[List[str]] = None) -> List[str]:
        """Create/recycle auto-merge nodes to match the layer count.

        Plan §4.3:
          * 0 layers → no merge.
          * 1 layer  → no merge (root → Output directly).
          * N layers → N-1 merges.

        Existing entries in ``stack.generated_merge_node_ids`` are reused;
        surplus nodes are removed (and recorded), missing nodes are created.
        """
        # A single layer normally bypasses Merge entirely.  If it has a
        # track matte, however, one managed Merge is needed as a masked copy
        # (A=layer, B/mask=matte).  Supplying the matte on the explicit mask
        # port is important for luma mattes; the ``mask`` blend mode only
        # reads B.alpha.
        single_layer_matte = False
        if len(root_ids) == 1:
            layer = self.graph.get_layer((layer_ids or stack.layer_ids)[0])
            matte = (self.graph.get_layer(layer.track_matte_layer_id)
                     if layer is not None and
                     getattr(layer, "track_matte_layer_id", None)
                     else None)
            single_layer_matte = bool(
                matte is not None and self.graph.get_node(matte.root_node_id))
        needed = max(0, len(root_ids) - 1)
        if single_layer_matte:
            needed = 1
        existing = [mid for mid in stack.generated_merge_node_ids
                    if self.graph.get_node(mid) is not None]
        # Prune dead ids from the caller's view as we go (recorded via the
        # returned list; the controller updates the stack).

        if len(existing) > needed:
            # Remove surplus auto-merges.  We never remove nodes the user
            # created — only those the stack itself owns.
            surplus = existing[needed:]
            for mid in surplus:
                result.changes.append(CompileChange(
                    kind="remove_node", node_id=mid, node_type=MERGE_NODE_TYPE))
            existing = existing[:needed]
        elif len(existing) < needed:
            # Create the shortfall.  Position them in a column to the left
            # of the output for readability.
            base_x, base_y = self._merge_placement_hint(stack)
            for i in range(len(existing), needed):
                mid = self._new_node_id(prefix="layer_merge")
                result.changes.append(CompileChange(
                    kind="create_node", node_id=mid,
                    node_type=MERGE_NODE_TYPE))
                existing.append(mid)

        return existing

    def _merge_placement_hint(self, stack: LayerStack) -> Tuple[float, float]:
        """Pick an origin for newly created Merge nodes.

        Anchors on the Output node's position when known, else (0, 0).  Each
        new merge is stacked vertically by the caller via index, so we only
        need the base point here.
        """
        out = self.graph.get_node(stack.output_node_id)
        if out is not None:
            x, y = out.position
            return (x - 280.0, y)
        return (0.0, 0.0)

    def _new_node_id(self, prefix: str = "n") -> str:
        """Generate a unique node id not present in the graph."""
        import uuid
        for _ in range(32):
            cand = f"{prefix}_{uuid.uuid4().hex[:6]}"
            if self.graph.get_node(cand) is None:
                return cand
        # Extremely unlikely fallback.
        return f"{prefix}_{uuid.uuid4().hex[:8]}"

    def _apply_connections(self, stack: LayerStack, root_ids: List[str],
                           merge_ids: List[str],
                           result: CompileResult,
                           ctx: Optional[_CompileCtx] = None,
                           layer_ids: Optional[List[str]] = None,
                           layers: Optional[Dict[str, Layer]] = None) -> None:
        """Compute intended connections and reconcile with the graph.

        Emits Connect/Disconnect CompileChanges for every divergence.  The
        controller is responsible for actually mutating the graph (and
        batching into one undo command).
        """
        intended: List[Tuple[str, str, str, str]] = []  # (src,src_port,dst,dst_port)

        n = len(root_ids)
        layer_ids = layer_ids or []
        layers = layers or {}
        if n == 0:
            # Nothing feeds the Output.  Caller may want to clear Output's
            # input; we record a disconnect of whatever currently feeds it.
            if stack.output_node_id:
                self._record_disconnects_to_port(
                    stack.output_node_id, OUTPUT_INPUT_PORT, result)
            return

        if n == 1 and merge_ids:
            # A single-layer Track Matte uses Merge's masked "copy"
            # operation: A is the layer, B is ignored by the blend mode, and
            # the same matte is also connected to the explicit mask port.
            layer = layers.get(layer_ids[0]) if layer_ids else None
            matte = layers.get(getattr(layer, "track_matte_layer_id", None)) \
                if layer is not None else None
            mid = merge_ids[0]
            if matte is None or not self.graph.get_node(matte.root_node_id):
                result.warnings.append(
                    f"Layer {layer_ids[0]!r} references a missing Track Matte; "
                    "the layer will render without the matte.")
                if stack.output_node_id:
                    intended.append((root_ids[0], _output_port_of(self.graph, root_ids[0]),
                                     stack.output_node_id, OUTPUT_INPUT_PORT))
            else:
                intended.append((root_ids[0], _output_port_of(self.graph, root_ids[0]),
                                 mid, MERGE_PORT_FOREGROUND))
                intended.append((matte.root_node_id,
                                 _output_port_of(self.graph, matte.root_node_id,
                                                 role="matte"),
                                 mid, MERGE_PORT_BACKGROUND))
                intended.append((matte.root_node_id,
                                 _output_port_of(self.graph, matte.root_node_id,
                                                 role="matte"),
                                 mid, "mask"))
                if stack.output_node_id:
                    intended.append((mid, MERGE_OUTPUT_PORT,
                                     stack.output_node_id, OUTPUT_INPUT_PORT))
        elif n == 1:
            # Single layer → straight to Output.
            if stack.output_node_id:
                intended.append((root_ids[0], _output_port_of(self.graph, root_ids[0]),
                                 stack.output_node_id, OUTPUT_INPUT_PORT))
        else:
            # N layers, N-1 merges.  Merge i combines:
            #   foreground = root_ids[i]                    (for i == 0)
            #             OR merge_ids[i-1].result          (for i > 0)
            #   background = root_ids[i+1]
            # The last merge feeds the Output.
            for i in range(len(merge_ids)):
                mid = merge_ids[i]
                if i == 0:
                    fg_src = root_ids[0]
                    fg_port = _output_port_of(self.graph, root_ids[0])
                else:
                    fg_src = merge_ids[i - 1]
                    fg_port = MERGE_OUTPUT_PORT
                bg_src = root_ids[i + 1]
                bg_port = _output_port_of(self.graph, root_ids[i + 1])
                intended.append((fg_src, fg_port, mid, MERGE_PORT_FOREGROUND))
                intended.append((bg_src, bg_port, mid, MERGE_PORT_BACKGROUND))
                # Layer i owns the merge that composites it over the layer
                # below.  Its Track Matte is a third input to that managed
                # Merge, so the shader applies alpha/luma before compositing.
                if i < len(layer_ids):
                    layer = layers.get(layer_ids[i])
                    matte = (layers.get(layer.track_matte_layer_id)
                             if layer is not None and
                             layer.track_matte_layer_id else None)
                    if matte is not None and self.graph.get_node(matte.root_node_id):
                        intended.append((
                            matte.root_node_id,
                            _output_port_of(self.graph, matte.root_node_id,
                                            role="matte"),
                            mid, "mask"))
                    elif layer is not None and layer.track_matte_layer_id:
                        result.warnings.append(
                            f"Layer {layer.layer_id!r} references a missing "
                            "Track Matte; the matte connection was skipped.")
            # Final merge → Output
            if stack.output_node_id:
                intended.append((merge_ids[-1], MERGE_OUTPUT_PORT,
                                 stack.output_node_id, OUTPUT_INPUT_PORT))

        self._reconcile_connections(intended, result, ctx)

    def _reconcile_connections(self,
                               intended: List[Tuple[str, str, str, str]],
                               result: CompileResult,
                               ctx: Optional[_CompileCtx] = None) -> None:
        """Diff intended connections vs current graph; emit changes.

        Operates on the *auto-managed subgraph* only: connections that
        involve an auto-merge node or the Output's ``image`` port.  User
        connections between effect nodes (Blur → Transform, etc.) are never
        touched.
        """
        intended_set = set(intended)
        # Current connections restricted to the managed surface.
        current: Dict[Tuple[str, str, str, str], None] = {}
        for conn in list(self.graph.connections):
            key = (conn.src_node_id, conn.src_port_name,
                   conn.dst_node_id, conn.dst_port_name)
            current[key] = None

        # Disconnects: present now, not intended.
        for key in current:
            if key not in intended_set and self._is_managed_connection(key, ctx):
                result.changes.append(CompileChange(
                    kind="disconnect",
                    src_node_id=key[0], src_port_name=key[1],
                    dst_node_id=key[2], dst_port_name=key[3]))

        # Connects: intended, not present.
        for key in intended:
            if key not in current:
                result.changes.append(CompileChange(
                    kind="connect",
                    src_node_id=key[0], src_port_name=key[1],
                    dst_node_id=key[2], dst_port_name=key[3]))

    def _is_managed_connection(self, key: Tuple[str, str, str, str],
                               ctx: Optional[_CompileCtx] = None) -> bool:
        """Return True if a connection is part of the auto Merge/Output chain.

        A connection is "managed" if it touches an auto-merge node (by id —
        tracked on the stack) or lands on the Output's ``image`` input.
        Everything else (effect → effect wiring the user authored) is left
        alone.

        When ``ctx`` is supplied (the normal compile path) the managed-id
        set is reused; otherwise we fall back to a stack lookup for any
        callers outside compile (kept for backwards compatibility).
        """
        src_id, _src_port, dst_id, dst_port = key
        if ctx is not None:
            managed = ctx.managed_node_ids
            out_id = ctx.output_node_id
        else:
            stack = self.graph.get_main_layer_stack()
            if stack is None:
                return False
            managed = set(stack.generated_merge_node_ids)
            managed.add(stack.output_node_id)
            out_id = stack.output_node_id
        # A nested stack's output target (the Precomp node) is also the
        # source of its parent-layer connection.  That downstream edge is
        # owned by the parent stack, so the child compiler must not remove
        # it merely because the target node is in the child's managed set.
        if (src_id in managed and src_id != out_id) or dst_id in managed:
            return True
        # Output image input regardless of node-id membership.
        if dst_id == out_id and dst_port == OUTPUT_INPUT_PORT:
            return True
        return False

    def _record_disconnects_to_port(self, node_id: str, port_name: str,
                                    result: CompileResult) -> None:
        for conn in list(self.graph.connections):
            if conn.dst_node_id == node_id and conn.dst_port_name == port_name:
                result.changes.append(CompileChange(
                    kind="disconnect",
                    src_node_id=conn.src_node_id, src_port_name=conn.src_port_name,
                    dst_node_id=conn.dst_node_id, dst_port_name=conn.dst_port_name))

    def _detect_chain_conflict(self, stack: LayerStack, root_ids: List[str],
                               result: CompileResult,
                               ctx: Optional[_CompileCtx] = None) -> bool:
        """Detect when the user manually rewired an auto-Merge (plan §4.5).

        We must distinguish two cases that look identical at the connection
        level:

        1. The previous compile produced the current connections (a normal
           reorder should rewrite them — NOT a conflict).
        2. The user attached nodes that the layer system did not author
           (a real conflict — refuse to overwrite).

        Because :class:`Connection` carries no provenance flag, we use the
        following heuristic: an auto-Merge's A/B inputs may legitimately be
        driven by any *layer root* or any other *auto-Merge* (those are the
        nodes the chain is allowed to wire).  If either input is fed by a
        node outside that allow-list, the user has grafted external wiring
        and we flag a conflict.
        """
        if len(root_ids) < 2:
            return False
        existing = [mid for mid in stack.generated_merge_node_ids
                    if self.graph.get_node(mid) is not None]
        if not existing:
            return False

        allow = set(root_ids)
        allow.update(existing)
        # Include every layer root/source the graph knows about — not just
        # the ones currently in this stack.  When a layer was just removed
        # (its root node still exists in the graph until the controller
        # garbage-collects it), its outgoing connections to auto-merges are
        # *stale*, not *user-rewired*, and must not be mistaken for a
        # conflict.  The reconcile pass will disconnect them cleanly.
        for layer in self.graph.get_all_layers():
            allow.add(layer.root_node_id)
            if layer.source_node_id:
                allow.add(layer.source_node_id)

        for i, mid in enumerate(existing[:max(0, len(root_ids) - 1)]):
            cur_a = self._input_source(mid, MERGE_PORT_FOREGROUND, ctx)
            cur_b = self._input_source(mid, MERGE_PORT_BACKGROUND, ctx)
            # An empty input is fine (compiler will fill it).  An input fed
            # by something we never author is the conflict signal.
            bad_a = cur_a is not None and cur_a not in allow
            bad_b = cur_b is not None and cur_b not in allow
            if bad_a or bad_b:
                result.conflict = True
                result.ok = False
                result.conflict_reason = (
                    f"Auto-Merge {mid!r} has externally authored inputs "
                    f"(A={cur_a!r}, B={cur_b!r}); the layer stack cannot "
                    f"overwrite them without confirmation.")
                return True
        return False

    def _input_source(self, node_id: str, port_name: str,
                      ctx: Optional[_CompileCtx] = None) -> Optional[str]:
        """Return the src node id feeding ``node_id:port_name``, or None.

        Uses the cached ``ctx.input_index`` when available (O(1)); otherwise
        falls back to a full-connections scan for out-of-compile callers.
        """
        if ctx is not None:
            return ctx.input_index.get((node_id, port_name))
        for conn in self.graph.connections:
            if (conn.dst_node_id == node_id
                    and conn.dst_port_name == port_name):
                return conn.src_node_id
        return None

    def _project_layer_state(self, layer_ids: List[str],
                             layers: Dict[str, Layer],
                             merge_ids: List[str],
                             result: CompileResult) -> None:
        """Project opacity/blend/visibility/parent onto nodes (plan §7/§8).

        This is a best-effort projection — failures are recorded as warnings
        rather than aborting the compile.  Only managed layers are touched;
        ``managed=False`` (external) layers keep their node state intact.

        AUD-4900: projection is *record-only*.  Every value is emitted as a
        ``set_parameter`` :class:`CompileChange` (skipped when the live node
        already holds it) and the controller applies them inside its undo
        transaction.  The compiler itself never mutates live nodes, so a
        dry-run compile (``sync_from_node_graph(repair=False)``) is a true
        dry-run and eager parameter writes can no longer escape undo.

        Opacity mapping (AUD-4901).  The managed chain is
        ``m0(fg=root(l0), bg=root(l1)) → m1(fg=m0, bg=root(l2)) → …`` and a
        Merge's ``opacity`` modulates its *foreground* only (shaders.py
        MERGE comment).  Therefore:

        * l0 (top of the live chain): is the fg of m0 — its blend/opacity
          (and matte) project onto m0.
        * every layer below the top: enters the chain only as a merge
          *background*, where opacity does not apply.  Writing its opacity
          onto the merge at its index would dim the whole group above it
          (the audited defect).  Blend mode and matte still project to that
          merge (layer_model contract: "the Merge node that combines this
          layer with the one below"); opacity falls back to the layer's own
          root node when it declares opacity support (see
          :meth:`_project_root_opacity`), which also covers single-layer
          stacks and the bottom layer that own no merge at all.
        """
        for i, lid in enumerate(layer_ids):
            layer = layers.get(lid)
            if layer is None or not layer.managed:
                continue
            owns_foreground_merge = i == 0 and i < len(merge_ids)
            if i < len(merge_ids):
                merge_id = merge_ids[i]
                node = self.graph.get_node(merge_id)
                # Map the persisted layer token to the MergeNode/shader
                # token.  Passing friendly underscore names through silently
                # falls back to ``over`` in the evaluator.
                has_track_matte = bool(layer.track_matte_layer_id)
                # The single-layer matte merge is a masked copy; regular
                # layer stacks retain the user's blend mode and use the
                # Merge mask port for alpha/luma modulation.
                bm = ("copy" if len(layer_ids) == 1 and has_track_matte
                      else LAYER_BLEND_MODE_TO_NODE.get(layer.blend_mode, "over"))
                _project_param(node, "blend_mode", bm, result, merge_id)
                if owns_foreground_merge:
                    # m0's foreground is root(l0): merge opacity modulates
                    # exactly this layer's contribution.
                    _project_param(node, "opacity", layer.opacity,
                                   result, merge_id)
                _project_param(
                    node, "mask_channel",
                    "alpha" if layer.track_matte_mode == "alpha" else "luma",
                    result, merge_id)
                _project_param(node, "mask_invert", bool(layer.track_matte_invert),
                               result, merge_id)
            if not owns_foreground_merge:
                # AUD-4901: no managed merge modulates this layer alone —
                # use the root-node opacity fallback (never the group
                # merge at this index, which would dim everything above).
                self._project_root_opacity(layer, result)
            # Parenting is projected onto the layer root (Transform/Text),
            # not the merge — the runtime world-transform resolver reads
            # ``parent_id`` on that node.
            root = self.graph.get_node(layer.root_node_id)
            if root is not None and getattr(root, "supports_parent_transform",
                                            False):
                parent_layer = (layers.get(layer.parent_layer_id)
                                if layer.parent_layer_id else None)
                wanted_parent = (parent_layer.root_node_id
                                 if parent_layer is not None else None)
                if getattr(root, "parent_id", None) != wanted_parent:
                    # Record as a node param-style change so the controller
                    # can wrap it in the same undo group.
                    result.changes.append(CompileChange(
                        kind="connect",  # reuse connect change for parent set
                        src_node_id=layer.root_node_id,
                        dst_node_id=layer.root_node_id,
                        src_port_name="__parent__",
                        dst_port_name=wanted_parent or ""))

    def _project_root_opacity(self, layer: Layer,
                              result: CompileResult) -> None:
        """AUD-4901 fallback: project opacity onto the layer's own root.

        Used for layers no managed merge modulates individually (everything
        below the top layer, plus single-layer stacks).  The root must
        declare opacity support — the ``supports_layer_opacity`` ClassVar,
        an ``opacity`` entry in ``PARAM_DEFS``, or an existing ``opacity``
        parameter.  When it does not, the value is unrepresentable in the
        locked Merge topology; a layer whose opacity differs from 1.0 is
        surfaced as a warning instead of silently re-targeting the merge
        that dims the whole group above it.
        """
        root = self.graph.get_node(layer.root_node_id)
        if root is None:
            return
        params = getattr(root, "parameters", None)
        declares_opacity = bool(
            getattr(root, "supports_layer_opacity", False)
            or (isinstance(params, dict) and "opacity" in params)
            or any(getattr(p, "name", None) == "opacity"
                   for p in getattr(root, "PARAM_DEFS", ()) or ()))
        if declares_opacity:
            _project_param(root, "opacity", layer.opacity, result,
                           layer.root_node_id)
        elif layer.opacity != 1.0:
            result.warnings.append(
                f"Layer {layer.layer_id!r} opacity {layer.opacity!r} cannot "
                "be projected: its root node declares no opacity parameter "
                "and the managed Merge chain cannot modulate a background "
                "layer individually.")


def _output_port_of(graph: Any, node_id: str,
                    role: str = "result") -> str:
    """Return the image output port for a graph role.

    Ordinary layer/effect connections use the regular result output. Track
    Matte connections prefer a dedicated ``matte``/``mask``/``alpha`` output
    when available, while older nodes still fall back to their first image
    output.
    """
    node = graph.get_node(node_id)
    if node is None:
        return MERGE_OUTPUT_PORT
    outputs = list(getattr(node, "outputs", []))
    if role == "matte":
        for p in outputs:
            if (getattr(p, "name", "").casefold() in
                    {"matte", "mask", "alpha"}
                    and getattr(p, "data_type", "") == "image"):
                return p.name
    for p in outputs:
        if getattr(p, "data_type", "") == "image":
            return p.name
    if outputs:
        return outputs[0].name
    return "result"


def _project_param(node: Any, key: str, value: Any,
                   result: CompileResult, node_id: str) -> None:
    """Record a ``set_parameter`` change for the controller to apply.

    AUD-4900: the compiler never writes live nodes.  Even when ``node``
    already exists the projection is emitted as a :class:`CompileChange`
    and applied by the controller inside its undo transaction, so a
    dry-run compile (``sync_from_node_graph(repair=False)``) cannot mutate
    the graph and eager writes cannot escape undo snapshots.

    When the live node already holds the value the change is skipped: a
    steady-state recompile produces no set_parameter records (keeping the
    sync dry-run report quiet) and applying the batch stays idempotent.
    """
    params = getattr(node, "parameters", None)
    if isinstance(params, dict) and params.get(key) == value:
        return
    result.changes.append(CompileChange(
        kind="set_parameter", node_id=node_id,
        param_name=key, value=value))
