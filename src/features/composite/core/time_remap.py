"""Frame time-remapping for the composite graph (plan P0-04 / P0-05).

Two independent mechanisms live here:

1. **Time node remapping** (P0-04).  ``time_offset`` / ``frame_hold`` /
   ``retime`` nodes used to be pure pass-throughs: their parameters were
   stored as metadata and never changed which source frame was read.  The
   evaluation model is pull-based — a source node decides which frame to
   decode at the moment it is evaluated — so a *downstream* time node can
   only influence its *upstream* sources through a pre-evaluation pass.
   :func:`compute_frame_overrides` performs that pass: it walks the graph
   in reverse topological order and composes every time node's mapping
   into a per-node :class:`TimeRemap`.  Sources then apply their remap to
   the composition playhead before resolving a frame.

2. **Layer frame gates** (P0-05).  ``Layer.in_frame`` / ``out_frame`` used
   to be persisted and validated but never read by any render path.
   ``NodeGraph.snapshot()`` now derives per-node gates from the layer
   stacks (see :func:`compute_layer_frame_gates`); the evaluator hides a
   gated node's output while the playhead is outside ``[in, out]``.

Both results ride on :class:`GraphSnapshot` so the scheduler, the
evaluator and the cache all agree without extra locking.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, Optional, Tuple

# Node types whose parameters define an output-frame → input-frame mapping.
TIME_REMAP_NODE_TYPES = frozenset({
    "time_offset", "frame_hold", "retime", "precomp",
})


@dataclass(frozen=True)
class TimeRemap:
    """Maps a composition frame to the frame a node should be evaluated at.

    The closed form is ``hold`` when set, otherwise
    ``round(frame * speed) + offset``.  This covers the three time nodes:

    * ``time_offset``  → ``TimeRemap(speed=1.0, offset=frame_offset)``
    * ``frame_hold``   → ``TimeRemap(hold=hold_frame)``
    * ``retime``       → ``TimeRemap(speed=speed)``

    B5 adds two post-affine, non-affine modes (both anchored on an
    inclusive source range ``(in, out)``):

    * ``reverse``  → mirror: ``x = (out + in) - x`` (equivalent to negative
      speed while ``x`` stays inside the range, but keeps ``speed``
      semantics clean).  Without a range the mirror anchors at 0
      (``x = -x``) — deterministic, but sources clamp negative frames, so
      a range is strongly recommended.
    * ``loop``     → wrap: ``x = in + (x - in) % max(1, out - in + 1)``.

    Per-node order is affine → reverse → loop.

    The family was closed under composition for pure affine remaps; the
    non-affine modes break that closure.  :meth:`pushed_upstream` keeps
    composition EXACT by chaining: when folding is impossible the result
    keeps the downstream transform and stores the upstream remap as
    ``inner`` — ``apply`` evaluates inner first, then the own transform.
    Chains are bounded by the number of time nodes between source and
    consumer, so per-frame cost stays trivial.
    """

    speed: float = 1.0
    offset: int = 0
    hold: Optional[int] = None
    reverse: bool = False
    loop: Optional[Tuple[int, int]] = None
    # Mirror anchor for reverse when looping is OFF: without it the mirror
    # anchors at 0 and every positive frame maps negative — the source
    # clamps and playback freezes on frame 0 (B5).
    reverse_range: Optional[Tuple[int, int]] = None
    inner: Optional["TimeRemap"] = None

    # ── Queries ──

    def is_identity(self) -> bool:
        if (self.hold is not None or self.offset != 0 or self.speed != 1.0
                or self.reverse or self.loop is not None
                or self.reverse_range is not None):
            return False
        return self.inner is None or self.inner.is_identity()

    def apply(self, frame: int) -> int:
        """Map a composition frame to this node's evaluation frame."""
        if self.hold is not None:
            return int(self.hold)
        if self.inner is not None:
            frame = self.inner.apply(frame)
        try:
            x = int(round(int(frame) * float(self.speed))) + int(self.offset)
        except (TypeError, ValueError, OverflowError):
            x = int(frame)
        if self.reverse:
            anchor = (self.reverse_range if self.reverse_range is not None
                      else self.loop)
            if anchor is not None:
                lo, hi = int(anchor[0]), int(anchor[1])
                x = (hi + lo) - x
            else:
                x = -x
        if self.loop is not None:
            lo, hi = int(self.loop[0]), int(self.loop[1])
            span = max(1, hi - lo + 1)
            x = lo + (x - lo) % span
        return x

    def signature(self) -> str:
        """Stable cache dimension — distinct remaps must not share cache keys."""
        sig = (f"ts{self.speed:.6f}|{self.offset}|{self.hold}"
               f"|r{int(self.reverse)}|L{self.loop}|R{self.reverse_range}")
        if self.inner is not None:
            sig += "|>" + self.inner.signature()
        return sig

    # ── Composition ──

    @staticmethod
    def _is_pure_affine(r: "TimeRemap") -> bool:
        return not (r.reverse or r.loop is not None
                    or r.reverse_range is not None or r.inner is not None)

    def pushed_upstream(self, downstream_g: "TimeRemap") -> "TimeRemap":
        """Compose a downstream time node's mapping around this remap.

        ``self`` maps a node N's evaluation frame: ``x = self(F)``.  When N
        is a time node with its own output→input mapping ``g``, N's inputs
        are evaluated at ``g(x)`` — i.e. the upstream's remap is
        ``g ∘ self``.
        """
        # Constant input collapses to a single constant regardless of g's
        # affine part (a held frame stays held; a hold node freezes
        # whatever arrives).
        if self.hold is not None:
            if downstream_g.hold is not None:
                return TimeRemap(hold=downstream_g.hold)
            return TimeRemap(hold=downstream_g.apply(self.hold))
        if downstream_g.hold is not None:
            return TimeRemap(hold=downstream_g.hold)
        # affine ∘ affine folds only when both speeds are integers (N-08):
        # round(F*整数) 没有小数损失，折叠是精确的。非整数速度（0.5、
        # 1.25…）的中间 round 不满足仿射闭包——旧实现一律折叠成
        # round(F*ts*gs)+…，连续 Retime(0.5)→(2.0) 合成 1.0 后帧 1/3/5
        # 分别错成 1/3/5（逐级顺序是 0/4/4）。保留 inner 链逐级 apply，
        # 精度与顺序语义一致；链长受时间节点数约束，成本可忽略。
        if (self._is_pure_affine(self) and self._is_pure_affine(downstream_g)
                and float(self.speed).is_integer()
                and float(downstream_g.speed).is_integer()):
            new_speed = float(self.speed) * float(downstream_g.speed)
            new_offset = (int(round(int(self.offset) * float(downstream_g.speed)))
                          + int(downstream_g.offset))
            return TimeRemap(speed=new_speed, offset=new_offset)
        # Non-affine modes present: keep the downstream transform as-is and
        # chain the upstream remap as inner.  apply() evaluates inner first,
        # so the result is exactly g ∘ self (no rounding drift).
        return TimeRemap(
            speed=downstream_g.speed,
            offset=downstream_g.offset,
            reverse=downstream_g.reverse,
            loop=downstream_g.loop,
            reverse_range=downstream_g.reverse_range,
            inner=self,
        )


def remap_for_parameters(node_type: str,
                         parameters: Dict[str, object]) -> Optional[TimeRemap]:
    """Build a time node's own output→input mapping from its parameters.

    Returns None for non-time node types or unusable parameter values
    (the caller then treats the node as a plain pass-through).
    """
    if node_type not in TIME_REMAP_NODE_TYPES:
        return None
    parameters = parameters or {}
    try:
        if node_type == "time_offset":
            offset = int(parameters.get("frame_offset", 0) or 0)
            return TimeRemap(speed=1.0, offset=offset)
        if node_type == "frame_hold":
            hold = int(parameters.get("hold_frame", 0) or 0)
            return TimeRemap(hold=max(0, hold))
        if node_type == "retime":
            speed = float(parameters.get("speed", 1.0) or 1.0)
            if speed <= 0.0:
                speed = 1.0
            reverse = bool(parameters.get("reverse", False))
            loop = _parse_loop_range(parameters)
            reverse_range = None
            if reverse:
                # Reverse needs an anchor range: mirroring at 0 maps every
                # positive frame negative and the source clamps to a frozen
                # frame 0. Anchor on the loop range even when loop_mode is
                # "off" — loop_mode only gates the wrap, not the mirror.
                reverse_range = (loop if loop is not None
                                 else _parse_loop_range(parameters, force=True))
            return TimeRemap(speed=speed, reverse=reverse, loop=loop,
                             reverse_range=reverse_range)
        if node_type == "precomp":
            speed = float(parameters.get("speed", 1.0) or 1.0)
            if speed <= 0.0:
                speed = 1.0
            offset = int(parameters.get("frame_offset", 0) or 0)
            hold_raw = parameters.get("hold_frame", -1)
            hold = None
            if hold_raw is not None and int(hold_raw) >= 0:
                hold = int(hold_raw)
            return TimeRemap(speed=speed, offset=offset, hold=hold)
    except (TypeError, ValueError, OverflowError):
        return None
    return None


def _parse_loop_range(parameters: Dict[str, object],
                      force: bool = False) -> Optional[Tuple[int, int]]:
    """Parse retime loop parameters into an inclusive (in, out) range.

    ``loop_mode`` "off" (or missing / unknown) disables looping — unless
    ``force`` is set (used for the reverse mirror anchor, which applies
    regardless of the wrap mode).  A malformed range (out < in,
    non-numeric) is treated as off — the node degrades to a plain
    forward/reverse remap rather than failing.
    """
    if not force:
        mode = str(parameters.get("loop_mode", "off") or "off")
        if mode != "loop":
            return None
    try:
        lo = int(parameters.get("loop_in", 0) or 0)
        hi = int(parameters.get("loop_out", 0) or 0)
    except (TypeError, ValueError, OverflowError):
        return None
    if hi < lo:
        return None
    return (lo, hi)


def compute_frame_overrides(nodes: Dict[str, object],
                            get_input_connections,
                            eval_order: Iterable[str],
                            conflict_logger=None,
                            frame_number: int = 0) -> Dict[str, TimeRemap]:
    """Pre-evaluation pass: resolve per-node frame remaps (P0-04).

    Walks ``eval_order`` in reverse (downstream first).  Each node carries
    an accumulated remap describing the frame it is evaluated at; time
    nodes wrap that remap with their own mapping before pushing it to
    their direct upstream nodes.  Only non-identity remaps are recorded.

    Bypassed time nodes do not remap: a ``disabled`` node (user bypass)
    and a node whose own ``time_range`` is inactive at ``frame_number``
    are treated as plain pass-throughs, matching the evaluator's
    ephemeral-node disable semantics.

    Limitations (deliberate, v1):

    * When one upstream node feeds several time chains that disagree on
      the remap, the first assignment wins (reverse topological order is
      deterministic).  True per-branch evaluation is not expressible in
      the single-eval-per-frame model and would require duplicate source
      evaluation.
    * Overrides are consumed by frame-reading sources (``video_frame``).
      Static image sources have no frame axis to remap.

    Args:
        nodes: ``{node_id: NodeSnapshot}``.
        get_input_connections: callable(node_id) → iterable of connection
            snapshots with ``src_node_id``.
        eval_order: topological order (sources first).
        conflict_logger: optional callable(str) for branch conflicts.
        frame_number: composition frame used for time_range activity.
    """
    pending: Dict[str, TimeRemap] = {}
    for node_id in reversed(list(eval_order)):
        ns = nodes.get(node_id)
        if ns is None:
            continue
        node_type = getattr(ns, "node_type", "")
        t = pending.get(node_id)
        t_up: Optional[TimeRemap] = t
        if (node_type in TIME_REMAP_NODE_TYPES
                and not getattr(ns, "disabled", False)
                and _own_range_active(ns, frame_number)):
            g = remap_for_parameters(
                node_type, getattr(ns, "parameters", {}) or {})
            if g is not None and not g.is_identity():
                # upstream remap = g ∘ t: the time node's own mapping g
                # applies to the frame this node is evaluated at (t).
                t_up = t.pushed_upstream(g) if t is not None else g
        if t_up is None or t_up.is_identity():
            continue
        for conn in get_input_connections(node_id):
            upstream_id = getattr(conn, "src_node_id", "")
            if not upstream_id or upstream_id not in nodes:
                continue
            existing = pending.get(upstream_id)
            if existing is not None and existing != t_up:
                if conflict_logger is not None:
                    conflict_logger(
                        f"node {upstream_id!r} is remapped differently by "
                        f"multiple time chains; using the first "
                        f"({existing.signature()})")
                continue
            pending[upstream_id] = t_up
    return pending


def _own_range_active(ns: object, frame_number: int) -> bool:
    """Time node's own time_range gate (mirrors evaluator semantics)."""
    tr = getattr(ns, "time_range", None)
    if not isinstance(tr, dict) or not tr.get("enabled", True):
        return True
    current = int(frame_number)
    try:
        start = int(tr.get("comp_start", current))
        end = int(tr.get("comp_end", current))
    except (TypeError, ValueError):
        return True
    return start <= current <= end


# ── Layer frame gates (P0-05) ──────────────────────────────────────────────

def gate_active(gate: Tuple[Optional[int], Optional[int]],
                frame: int) -> bool:
    """Inclusive in/out bounds check; None on a side means unbounded."""
    lo, hi = gate
    current = int(frame)
    if lo is not None and current < int(lo):
        return False
    if hi is not None and current > int(hi):
        return False
    return True


def _intersect_gates(a: Tuple[Optional[int], Optional[int]],
                     b: Tuple[Optional[int], Optional[int]]
                     ) -> Tuple[Optional[int], Optional[int]]:
    """Most restrictive range wins — a node claimed by two layers is only
    visible while both layers are."""
    lo = max(a[0], b[0]) if a[0] is not None and b[0] is not None else (
        a[0] if a[0] is not None else b[0])
    hi = min(a[1], b[1]) if a[1] is not None and b[1] is not None else (
        a[1] if a[1] is not None else b[1])
    return (lo, hi)


def compute_layer_frame_gates(layer_stacks: Iterable[object],
                              get_layer) -> Dict[str, Tuple[Optional[int],
                                                            Optional[int]]]:
    """Derive ``{node_id: (in_frame, out_frame)}`` from all layer stacks.

    A layer's range gates both its ``root_node_id`` (where the layer plugs
    into the merge chain — hides the layer) and its ``source_node_id``
    (the media source — prevents a stale decoded frame from leaking past
    the gate).  Ranges from multiple layers claiming the same node
    intersect.

    Args:
        layer_stacks: iterable of ``LayerStack``.
        get_layer: callable(layer_id) → ``Layer`` or None.
    """
    gates: Dict[str, Tuple[Optional[int], Optional[int]]] = {}
    for stack in layer_stacks:
        for layer_id in getattr(stack, "layer_ids", []) or []:
            layer = get_layer(layer_id)
            if layer is None:
                continue
            in_f = getattr(layer, "in_frame", None)
            out_f = getattr(layer, "out_frame", None)
            if in_f is None and out_f is None:
                continue
            try:
                in_f = int(in_f) if in_f is not None else None
                out_f = int(out_f) if out_f is not None else None
            except (TypeError, ValueError):
                continue
            for node_id in (getattr(layer, "source_node_id", None),
                            getattr(layer, "root_node_id", None)):
                if not node_id:
                    continue
                gate = (in_f, out_f)
                if node_id in gates:
                    gate = _intersect_gates(gates[node_id], gate)
                gates[node_id] = gate
    return gates
