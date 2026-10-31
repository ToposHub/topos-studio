"""Validate a :class:`LayerStack` against its :class:`NodeGraph`.

The validator runs the full check list from plan §9.4 / §13.1 / §13.4:

* every layer root / source node exists in the graph;
* layer IDs are unique across the stack;
* no parent cycle among layers (and the parent node supports parenting);
* the Output node exists and the chain feeding it is acyclic;
* Merge ports used by auto-merges are valid (A/B);
* blend-mode / opacity values are within their accepted ranges;
* stale layers (missing root node) are flagged so the controller can
  offer a repair action rather than silently dropping user data.

It produces a :class:`ValidationReport` — problems are categorised by
severity so the controller/UI can decide what to surface.  The validator
never mutates the graph.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set

from .layer_model import (
    Layer, LayerStack, ALL_LAYER_TYPES, LAYER_BLEND_MODES, DEFAULT_BLEND_MODE,
    TRACK_MATTE_MODES,
)


SEVERITY_ERROR = "error"
SEVERITY_WARNING = "warning"
SEVERITY_INFO = "info"


@dataclass
class ValidationIssue:
    """A single problem found by the validator."""

    severity: str
    code: str           # stable machine id, e.g. "missing_root_node"
    message: str
    layer_id: Optional[str] = None
    node_id: Optional[str] = None
    stack_id: Optional[str] = None


@dataclass
class ValidationReport:
    """Aggregated validation result."""

    issues: List[ValidationIssue] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """True when there are no error-severity issues."""
        return not any(i.severity == SEVERITY_ERROR for i in self.issues)

    @property
    def errors(self) -> List[ValidationIssue]:
        return [i for i in self.issues if i.severity == SEVERITY_ERROR]

    @property
    def warnings(self) -> List[ValidationIssue]:
        return [i for i in self.issues if i.severity == SEVERITY_WARNING]

    def has(self, code: str) -> bool:
        return any(i.code == code for i in self.issues)


class LayerStackValidator:
    """Validate layer stacks against the owning :class:`NodeGraph`."""

    def __init__(self, graph: Any) -> None:
        self.graph = graph

    # ── Public API ──────────────────────────────────────────────────────

    def validate(self, stack: LayerStack,
                 layers: Dict[str, Layer]) -> ValidationReport:
        """Run all checks; return the aggregated :class:`ValidationReport`."""
        report = ValidationReport()
        if stack is None:
            report.issues.append(ValidationIssue(
                severity=SEVERITY_ERROR, code="null_stack",
                message="No layer stack provided."))
            return report

        self._check_unique_ids(stack, report)
        self._check_referenced_nodes(stack, layers, report)
        self._check_shared_roots(stack, layers, report)
        self._check_parenting(stack, layers, report)
        self._check_output(stack, report)
        self._check_merge_ports(stack, report)
        self._check_layer_fields(stack, layers, report)
        self._check_acyclic_chain(stack, layers, report)
        return report

    # ── Individual checks ───────────────────────────────────────────────

    def _check_unique_ids(self, stack: LayerStack,
                          report: ValidationReport) -> None:
        seen: Set[str] = set()
        for lid in stack.layer_ids:
            if lid in seen:
                report.issues.append(ValidationIssue(
                    severity=SEVERITY_ERROR, code="duplicate_layer_id",
                    message=f"Layer id {lid!r} appears more than once.",
                    layer_id=lid, stack_id=stack.stack_id))
            seen.add(lid)

    def _check_referenced_nodes(self, stack: LayerStack,
                                layers: Dict[str, Layer],
                                report: ValidationReport) -> None:
        for lid in stack.layer_ids:
            layer = layers.get(lid)
            if layer is None:
                report.issues.append(ValidationIssue(
                    severity=SEVERITY_ERROR, code="missing_layer",
                    message=(f"Stack references layer {lid!r} which is not "
                             f"in the layer map."),
                    layer_id=lid, stack_id=stack.stack_id))
                continue
            if not self.graph.get_node(layer.root_node_id):
                report.issues.append(ValidationIssue(
                    severity=SEVERITY_ERROR, code="missing_root_node",
                    message=(f"Layer {lid!r} root node "
                             f"{layer.root_node_id!r} does not exist."),
                    layer_id=lid, node_id=layer.root_node_id,
                    stack_id=stack.stack_id))
            if (layer.source_node_id is not None
                    and not self.graph.get_node(layer.source_node_id)):
                report.issues.append(ValidationIssue(
                    severity=SEVERITY_WARNING, code="missing_source_node",
                    message=(f"Layer {lid!r} source node "
                             f"{layer.source_node_id!r} does not exist "
                             f"(the layer may still render via its root)."),
                    layer_id=lid, node_id=layer.source_node_id,
                    stack_id=stack.stack_id))

    def _check_shared_roots(self, stack: LayerStack,
                            layers: Dict[str, Layer],
                            report: ValidationReport) -> None:
        """Flag layers that share the same root node (review BUG-4).

        Two layers sharing a root produces an ambiguous Merge chain (the
        root feeds two different auto-merges) and makes remove_layer's
        node deletion dangerous (deleting one layer's root orphans the
        other).  We emit one warning per collision rather than erroring —
        instance-style sharing may be a legitimate advanced config, but
        the UI should at least know about it.
        """
        seen: Dict[str, str] = {}  # root_node_id -> first layer_id using it
        for lid in stack.layer_ids:
            layer = layers.get(lid)
            if layer is None:
                continue
            rid = layer.root_node_id
            first = seen.get(rid)
            if first is None:
                seen[rid] = lid
            else:
                report.issues.append(ValidationIssue(
                    severity=SEVERITY_WARNING, code="shared_root_node",
                    message=(f"Layers {first!r} and {lid!r} share root node "
                             f"{rid!r}; removing either may orphan the other."),
                    layer_id=lid, node_id=rid, stack_id=stack.stack_id))

    def _check_parenting(self, stack: LayerStack,
                         layers: Dict[str, Layer],
                         report: ValidationReport) -> None:
        # 1. Capability: parent root node must support_parent_transform.
        # 2. No cycles in the layer parent chain.
        # 3. Parent must be a known layer.
        for lid in stack.layer_ids:
            layer = layers.get(lid)
            if layer is None or layer.parent_layer_id is None:
                continue
            if layer.parent_layer_id not in layers:
                report.issues.append(ValidationIssue(
                    severity=SEVERITY_WARNING, code="dangling_parent",
                    message=(f"Layer {lid!r} parents to unknown layer "
                             f"{layer.parent_layer_id!r}."),
                    layer_id=lid, stack_id=stack.stack_id))
                continue
            # Capability check on the root node.
            root = self.graph.get_node(layer.root_node_id)
            if root is not None and not getattr(
                    root, "supports_parent_transform", False):
                report.issues.append(ValidationIssue(
                    severity=SEVERITY_WARNING, code="parent_unsupported_node",
                    message=(f"Layer {lid!r} root node "
                             f"{layer.root_node_id!r} does not support "
                             f"parenting."),
                    layer_id=lid, node_id=layer.root_node_id,
                    stack_id=stack.stack_id))

        # Cycle detection over layer parents.
        for start in stack.layer_ids:
            if self._layer_has_parent_cycle(start, layers):
                report.issues.append(ValidationIssue(
                    severity=SEVERITY_ERROR, code="parent_cycle",
                    message=(f"Layer {start!r} participates in a parent "
                             f"cycle."),
                    layer_id=start, stack_id=stack.stack_id))
                break  # one report is enough

    def _layer_has_parent_cycle(self, start: str,
                                layers: Dict[str, Layer]) -> bool:
        """Return True if ``start`` reaches itself via parent links.

        Uses an explicit visited-set walk rather than Floyd's tortoise/
        hare: Floyd silently mis-detects short cycles and chains that
        converge to a common ancestor, so a plain DFS with a visited set
        is the robust choice for a validator.
        """
        seen: Set[str] = set()
        cur = start
        # Guard against pathological depth.
        for _ in range(len(layers) + 2):
            if cur in seen:
                return True
            seen.add(cur)
            layer = layers.get(cur)
            if layer is None or layer.parent_layer_id is None:
                return False
            cur = layer.parent_layer_id
        return cur in seen

    def _check_output(self, stack: LayerStack,
                      report: ValidationReport) -> None:
        if not stack.output_node_id:
            report.issues.append(ValidationIssue(
                severity=SEVERITY_WARNING, code="missing_output_node_id",
                message="Stack has no output_node_id set.",
                stack_id=stack.stack_id))
            return
        out = self.graph.get_node(stack.output_node_id)
        if out is None:
            report.issues.append(ValidationIssue(
                severity=SEVERITY_ERROR, code="missing_output_node",
                message=(f"Output node {stack.output_node_id!r} does not "
                         f"exist."),
                node_id=stack.output_node_id, stack_id=stack.stack_id))

    def _check_merge_ports(self, stack: LayerStack,
                           report: ValidationReport) -> None:
        # Auto-merges must still be Merge nodes with A/B inputs.
        for mid in stack.generated_merge_node_ids:
            node = self.graph.get_node(mid)
            if node is None:
                report.issues.append(ValidationIssue(
                    severity=SEVERITY_ERROR, code="missing_merge_node",
                    message=(f"Auto-merge {mid!r} is registered but absent "
                             f"from the graph."),
                    node_id=mid, stack_id=stack.stack_id))
                continue
            port_names = {p.name for p in getattr(node, "inputs", [])}
            if "A" not in port_names or "B" not in port_names:
                report.issues.append(ValidationIssue(
                    severity=SEVERITY_ERROR, code="invalid_merge_ports",
                    message=(f"Auto-merge {mid!r} is not a valid Merge node "
                             f"(missing A/B inputs)."),
                    node_id=mid, stack_id=stack.stack_id))

    def _check_layer_fields(self, stack: LayerStack,
                            layers: Dict[str, Layer],
                            report: ValidationReport) -> None:
        # AUD-5002: ``stack.layer_ids`` is a list — the per-layer track-matte
        # membership test below is O(n) against it, making the whole loop
        # O(n²) on large stacks.  Build the set once.
        stack_layer_id_set: Set[str] = set(stack.layer_ids)
        for lid in stack.layer_ids:
            layer = layers.get(lid)
            if layer is None:
                continue
            if layer.layer_type not in ALL_LAYER_TYPES:
                report.issues.append(ValidationIssue(
                    severity=SEVERITY_WARNING, code="unknown_layer_type",
                    message=(f"Layer {lid!r} has unknown layer_type "
                             f"{layer.layer_type!r}."),
                    layer_id=lid, stack_id=stack.stack_id))
            if layer.track_matte_layer_id is not None:
                matte = layers.get(layer.track_matte_layer_id)
                if matte is None or layer.track_matte_layer_id not in stack_layer_id_set:
                    report.issues.append(ValidationIssue(
                        severity=SEVERITY_WARNING,
                        code="dangling_track_matte",
                        message=(f"Layer {lid!r} references unknown or external "
                                 f"Track Matte {layer.track_matte_layer_id!r}."),
                        layer_id=lid, stack_id=stack.stack_id))
                if layer.track_matte_layer_id == lid:
                    report.issues.append(ValidationIssue(
                        severity=SEVERITY_ERROR,
                        code="self_track_matte",
                        message=f"Layer {lid!r} cannot matte itself.",
                        layer_id=lid, stack_id=stack.stack_id))
            if layer.track_matte_mode not in TRACK_MATTE_MODES:
                report.issues.append(ValidationIssue(
                    severity=SEVERITY_WARNING,
                    code="unknown_track_matte_mode",
                    message=(f"Layer {lid!r} has unsupported Track Matte mode "
                             f"{layer.track_matte_mode!r}."),
                    layer_id=lid, stack_id=stack.stack_id))
            if layer.blend_mode not in LAYER_BLEND_MODES:
                report.issues.append(ValidationIssue(
                    severity=SEVERITY_WARNING, code="unknown_blend_mode",
                    message=(f"Layer {lid!r} has blend_mode "
                             f"{layer.blend_mode!r}; reset to "
                             f"{DEFAULT_BLEND_MODE!r}."),
                    layer_id=lid, stack_id=stack.stack_id))
            if not (0.0 <= layer.opacity <= 1.0):
                report.issues.append(ValidationIssue(
                    severity=SEVERITY_WARNING, code="opacity_out_of_range",
                    message=(f"Layer {lid!r} opacity {layer.opacity!r} is "
                             f"out of [0,1]."),
                    layer_id=lid, stack_id=stack.stack_id))
            # Time range sanity: in_frame <= out_frame when both set.
            if (layer.in_frame is not None
                    and layer.out_frame is not None
                    and layer.in_frame > layer.out_frame):
                report.issues.append(ValidationIssue(
                    severity=SEVERITY_WARNING, code="time_range_inverted",
                    message=(f"Layer {lid!r} in_frame {layer.in_frame} > "
                             f"out_frame {layer.out_frame}."),
                    layer_id=lid, stack_id=stack.stack_id))

    def _check_acyclic_chain(self, stack: LayerStack,
                             layers: Dict[str, Layer],
                             report: ValidationReport) -> None:
        # The compiler guarantees acyclicity, but a user could rewire the
        # node graph manually.  A full topological sort failure is the
        # authoritative signal — surface it as a stack-level error.
        #
        # AUD-5000: ``topological_sort()`` is Kahn's algorithm — it RAISES
        # ValueError("Graph contains a cycle") on a cycle and otherwise
        # returns every node; it never returns a partial list.  The old
        # broad-except + ``order is not None`` pattern therefore swallowed
        # the authoritative cycle signal (node_graph_cycle was dead code)
        # and masked non-cycle API failures as well.
        try:
            order = self.graph.topological_sort()
        except ValueError:
            report.issues.append(ValidationIssue(
                severity=SEVERITY_ERROR, code="node_graph_cycle",
                message=("The node graph contains a cycle; the layer stack "
                         "cannot be reliably compiled."),
                stack_id=stack.stack_id))
            return
        except Exception as exc:
            # A genuine API failure must stay diagnosable, not silently
            # turn the cycle check into a no-op.
            report.issues.append(ValidationIssue(
                severity=SEVERITY_WARNING, code="topology_check_failed",
                message=(f"Topological sort failed unexpectedly: {exc!r}; "
                         "the acyclicity check could not run."),
                stack_id=stack.stack_id))
            return
        # AUD-5001: ``NodeGraph.nodes`` is guarded by the graph's RLock —
        # take the lock for this count read instead of touching the raw
        # dict unlocked.  Graph test doubles without a lock fall back to a
        # direct read.
        graph_lock = getattr(self.graph, "_lock", None)
        if graph_lock is not None:
            with graph_lock:
                node_count = len(self.graph.nodes)
        else:
            node_count = len(self.graph.nodes)
        if order is not None and len(order) != node_count:
            # Defensive belt-and-braces: Kahn's implementation guarantees
            # all-or-raise today; keep the partial-list report in case that
            # contract ever changes.
            report.issues.append(ValidationIssue(
                severity=SEVERITY_ERROR, code="node_graph_cycle",
                message=("The node graph contains a cycle; the layer stack "
                         "cannot be reliably compiled."),
                stack_id=stack.stack_id))
