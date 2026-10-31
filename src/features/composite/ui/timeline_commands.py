"""UI-side Timeline commands (P2, T15-T23).

This module is the AE-style timeline's single edit entry point. It sits
*above* :class:`CompositeEditService` and gives the Keyframes / Spline /
Graph Editor views, the properties panel, AI and script a uniform way to:

* build a ``CompositeEditPlan`` with the right operation vocabulary;
* run the press/move/release/cancel lifecycle so a 20-keyframe drag is one
  undo unit (T17, plan §6.2);
* copy/paste with playhead-anchored clipboard semantics (T18, plan §6.3 /
  §19.10);
* mutate node ranges and per-track lock state (T21, T65) — neither of which
  is in the v1 ``KEYFRAME_OPS`` vocabulary, so they are handled here on the
  host side and surfaced to the host via a small result object the host
  already knows how to persist + undo.

Design rules:

1. **GUI-free.** No QWidget/QPainter. Everything is plain data + a host
   callback interface (``TimelineHost``) — so it is unit-testable headlessly
   and reusable by AI/script.
2. **One path.** Every keyframe / expression / interpolation / tangent /
   handle edit goes through :meth:`TimelineCommandBuilder.apply`, which
   builds a ``CompositeEditPlan`` and calls ``CompositeEditService.apply``.
   The result is a stable ``TimelineCommandResult`` with an ``error_code``
   string (T15) instead of an opaque message, so AI/script can branch on it.
3. **Before/after for undo.** :meth:`apply` returns serialized before/after
   track snapshots in the shape the host's ``_push_keyframe_track_undo``
   already consumes, so a host can record one undo group per command
   regardless of how many operations it contains.
4. **Lock is enforced at the service layer (T65).** ``set_track_lock`` /
   ``move_keyframes`` / ``paste_keyframes`` consult the lock registry
   before touching data; AI, script, mouse and keyboard all hit the same
   check.

The module deliberately re-uses the v1 protocol's op names
(``add_keyframe``, ``set_interpolation`` …) so a timeline command and a
raw script plan produce byte-identical edits.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Protocol, Tuple

from src.features.composite.edit_service import (
    CompositeEditError,
    CompositeEditPlan,
    CompositeEditService,
)


# ── error codes (T15) ───────────────────────────────────────────────────
# Stable strings the UI/AI/script can branch on. Never reuse a code for a
# different meaning; add a new one instead.
ERR_OK = "ok"
ERR_NODE_NOT_FOUND = "node_not_found"
ERR_PARAM_NOT_FOUND = "param_not_found"
ERR_NOT_ANIMATABLE = "param_not_animatable"
ERR_KEYFRAME_NOT_FOUND = "keyframe_not_found"
ERR_FRAME_INVALID = "frame_invalid"
ERR_VALUE_OUT_OF_RANGE = "value_out_of_range"
ERR_INTERP_INVALID = "interpolation_invalid"
ERR_TANGENT_INVALID = "tangent_invalid"
ERR_EXTRAP_INVALID = "extrap_invalid"
ERR_HANDLE_INVALID = "handle_invalid"
ERR_LOCKED = "track_locked"
ERR_EXPR_INVALID = "expression_invalid"
ERR_EXPR_NOT_ALLOWED = "expression_not_allowed"
ERR_PASTE_TYPE_MISMATCH = "paste_type_mismatch"
ERR_PLAN_FAILED = "plan_failed"
ERR_NO_TARGET = "no_target"


@dataclass(frozen=True)
class TimelineCommandResult:
    """Outcome of a timeline command.

    ``error_code`` is a stable string (one of ``ERR_*``). ``applied`` is
    True only when ``error_code == ERR_OK`` *and* the plan was actually
    committed. ``affected_tracks`` lists the (node_id, param_name) pairs
    the host needs to refresh / record undo for; each carries a serialized
    before/after track dict (None when the track did not / does not exist),
    matching the host's ``_push_keyframe_track_undo`` shape.
    """

    applied: bool
    error_code: str
    error_message: str = ""
    plan_id: str = ""
    affected_tracks: Tuple["TrackChange", ...] = field(default_factory=tuple)
    # Node-range changes (set_node_range) — host records as time-range undo.
    node_range_changes: Tuple["NodeRangeChange", ...] = field(default_factory=tuple)
    # Expression changes (set/remove_expression) — host records expression
    # text undo via these (AUD-11011).
    expression_changes: Tuple["ExpressionChange", ...] = field(
        default_factory=tuple)

    @property
    def ok(self) -> bool:
        return self.applied and self.error_code == ERR_OK

    def to_dict(self) -> Dict[str, Any]:
        return {
            "applied": self.applied,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "plan_id": self.plan_id,
            "affected_tracks": [t.to_dict() for t in self.affected_tracks],
            "node_range_changes": [
                n.to_dict() for n in self.node_range_changes
            ],
            "expression_changes": [
                e.to_dict() for e in self.expression_changes
            ],
        }


@dataclass(frozen=True)
class TrackChange:
    """Before/after serialized track for one (node, param).

    ``before`` / ``after`` are the dicts stored in
    ``node.keyframe_tracks[param]`` (or None). The host pushes one undo
    record per entry so a multi-track move = one logical edit but N undo
    restores — the plan calls this out as acceptable since each track's
    restore is independent.
    """

    node_id: str
    param_name: str
    before: Optional[Dict[str, Any]]
    after: Optional[Dict[str, Any]]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "node_id": self.node_id,
            "param_name": self.param_name,
            "before": self.before,
            "after": self.after,
        }


@dataclass(frozen=True)
class NodeRangeChange:
    node_id: str
    before: Optional[Dict[str, Any]]
    after: Optional[Dict[str, Any]]

    def to_dict(self) -> Dict[str, Any]:
        return {"node_id": self.node_id, "before": self.before,
                "after": self.after}


@dataclass(frozen=True)
class ExpressionChange:
    """Before/after expression text for one (node, param) — AUD-11011.

    ``set_expression`` / ``remove_expression`` mutate ``node`` expression
    storage, which the keyframe :class:`TrackChange` records cannot
    represent: without this record a host relying on the builder's undo
    hooks cannot restore the expression text on undo (and
    ``remove_expression`` previously produced NO record at all).
    ``before_track`` / ``after_track`` snapshot the keyframe track around the
    A-15 mutex flip so one record suffices to undo both dimensions.
    """

    node_id: str
    param_name: str
    before_text: Optional[str]
    after_text: Optional[str]
    before_track: Optional[Dict[str, Any]] = None
    after_track: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "node_id": self.node_id,
            "param_name": self.param_name,
            "before_text": self.before_text,
            "after_text": self.after_text,
            "before_track": self.before_track,
            "after_track": self.after_track,
        }


# ── host interface ──────────────────────────────────────────────────────


class TimelineHost(Protocol):
    """What :class:`TimelineCommandBuilder` needs from the host.

    ``get_node_graph`` returns the live NodeGraph the
    :class:`CompositeEditService` will mutate. ``record_track_change`` and
    ``record_node_range_change`` are undo hooks — the host already has
    equivalents (``_push_keyframe_track_undo`` /
    ``node_time_range_change``); this layer calls them so the command
    builder stays host-agnostic.
    """

    def get_node_graph(self) -> Any: ...
    def record_track_change(self, change: TrackChange) -> None: ...
    def record_node_range_change(self, change: NodeRangeChange) -> None: ...


# ── in-process clipboard (T18) ──────────────────────────────────────────


@dataclass
class ClipboardEntry:
    """One copied keyframe, anchored relative to its source playhead.

    Per plan §19.10: pasting anchors the *earliest* copied frame at the
    target frame and keeps every other frame's relative offset. We also
    keep the source's interpolation / handles / tangent so a paste preserves
    the full curve shape, and the source node-type + param ptype so a paste
    to a different-typed target can be rejected (``ERR_PASTE_TYPE_MISMATCH``)
    instead of silently truncating.
    """

    relative_frame: int  # frame - anchor_frame (>= 0, anchor = earliest)
    value: float
    interpolation: str = "linear"
    tangent_mode: str = "auto"
    handle_left: Optional[Tuple[float, float]] = None
    handle_right: Optional[Tuple[float, float]] = None
    extra: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Clipboard:
    """Process-wide keyframe clipboard.

    Lives in the builder so the timeline, spline and Graph Editor share one
    paste source. Does not persist to project files (plan §12).
    """

    entries: List[ClipboardEntry] = field(default_factory=list)
    source_node_type: Optional[str] = None
    source_ptype: Optional[str] = None

    def is_empty(self) -> bool:
        return not self.entries

    def clear(self) -> None:
        self.entries.clear()
        self.source_node_type = None
        self.source_ptype = None


# ── lock registry (T65) ─────────────────────────────────────────────────


class TrackLockRegistry:
    """Per-(node, param) edit-lock state, enforced at the service layer.

    The plan (§3.4 / §5.4 / §19.2) requires Lock to block mouse, keyboard,
    AI and script edits alike. This registry is the single authority; the
    AE-style "lock column" UI flips these bits and every command checks
    ``is_locked`` before applying. State is session-only — locks do not
    persist to project files (a project should render the same regardless
    of UI lock state).
    """

    def __init__(self) -> None:
        self._locked: set[str] = set()

    @staticmethod
    def _key(node_id: str, param_name: str) -> str:
        return f"{node_id}\x1f{param_name}"

    def is_locked(self, node_id: str, param_name: str) -> bool:
        return self._key(node_id, param_name) in self._locked

    def set_locked(self, node_id: str, param_name: str,
                   locked: bool) -> None:
        key = self._key(node_id, param_name)
        if locked:
            self._locked.add(key)
        else:
            self._locked.discard(key)

    def clear(self) -> None:
        self._locked.clear()


# ── drag transaction (T17) ──────────────────────────────────────────────


@dataclass
class DragTransaction:
    """press/move/release state for a multi-keyframe drag (T17).

    Captures the serialized *before* state of every affected track on press
    plus each selected keyframe's source frame/payload; ``move`` only updates
    a lightweight preview (the view paints it); ``release`` builds one
    ``CompositeEditPlan`` containing all the new keyframes and applies it,
    producing a single undo unit. ``cancel`` discards the preview and touches
    nothing.
    """

    affected: List[Tuple[str, str]]  # (node_id, param_name) per track
    before: Dict[Tuple[str, str], Optional[Dict[str, Any]]]
    # AUD-11010: per-track source keyframes of the drag — src_frame ->
    # serialized keyframe dict. commit_move DROPS these source frames (a
    # drag is a move, not a copy) and carries each payload's
    # interpolation/tangent/handles to the new frame.
    sources: Dict[Tuple[str, str], Dict[int, Dict[str, Any]]] = field(
        default_factory=dict)
    # (node_id, param_name) -> {src_frame: (new_frame, value)}. Keyed by the
    # dragged key's SOURCE frame so repeated updates overwrite the same
    # entry (AUD-11010) instead of accumulating every intermediate position.
    preview: Dict[Tuple[str, str], Dict[int, Tuple[int, float]]] = field(
        default_factory=dict)
    active: bool = False

    def reset(self) -> None:
        self.affected.clear()
        self.before.clear()
        self.sources.clear()
        self.preview.clear()
        self.active = False


# ── command builder ─────────────────────────────────────────────────────


class TimelineCommandBuilder:
    """Build + apply timeline commands through the unified protocol.

    One instance per CompositePage (shared by Keyframes, Spline, Graph
    Editor, properties panel, AI and script). Owns the clipboard and lock
    registry so every surface agrees.
    """

    def __init__(self, host: TimelineHost) -> None:
        self._host = host
        self.clipboard = Clipboard()
        self.locks = TrackLockRegistry()
        self._drag = DragTransaction(affected=[], before={})

    # ── service access ──
    def _service(self) -> CompositeEditService:
        return CompositeEditService(self._host.get_node_graph())

    def _node(self, node_id: str) -> Tuple[Any, str]:
        graph = self._host.get_node_graph()
        node = graph.get_node(node_id) if graph is not None else None
        if node is None:
            return None, ERR_NODE_NOT_FOUND
        return node, ERR_OK

    def _snapshot_track(self, node: Any,
                        param_name: str) -> Optional[Dict[str, Any]]:
        data = node.keyframe_tracks.get(param_name)
        return copy.deepcopy(data) if data is not None else None

    @staticmethod
    def _ptype_of(node: Any, param_name: str) -> Optional[str]:
        pdef = node.get_param_defs().get(param_name)
        return pdef.ptype if pdef is not None else None

    # ── core apply ──
    def _apply(self, plan_id: str,
               operations: List[Mapping[str, Any]],
               affected: List[Tuple[str, str]]) -> TimelineCommandResult:
        """Apply a plan and capture before/after for the affected tracks."""
        # Capture BEFORE snapshots from the live graph (so the host can undo
        # exactly what changed, even if the plan partially touched the same
        # param twice).
        graph = self._host.get_node_graph()
        before_snapshots: Dict[Tuple[str, str], Optional[Dict[str, Any]]] = {}
        for node_id, param_name in affected:
            node = graph.get_node(node_id) if graph is not None else None
            if node is None:
                continue
            before_snapshots[(node_id, param_name)] = \
                self._snapshot_track(node, param_name)

        plan = CompositeEditPlan(plan_id=plan_id,
                                 operations=tuple(dict(o) for o in operations))
        try:
            result = self._service().apply(plan)
        except CompositeEditError as exc:
            return TimelineCommandResult(
                False, _classify_error(str(exc)), str(exc), plan_id=plan_id)
        except Exception as exc:  # host / node defect — never crash the UI
            return TimelineCommandResult(
                False, ERR_PLAN_FAILED, str(exc), plan_id=plan_id)

        if not result.applied:
            msg = (result.report.errors[0].message
                   if result.report.errors else (result.error or ""))
            return TimelineCommandResult(
                False, _classify_error(msg), msg, plan_id=plan_id)

        # Capture AFTER and emit TrackChange records the host will undo.
        changes: List[TrackChange] = []
        for node_id, param_name in affected:
            node = graph.get_node(node_id) if graph is not None else None
            if node is None:
                continue
            after = self._snapshot_track(node, param_name)
            changes.append(TrackChange(
                node_id=node_id, param_name=param_name,
                before=before_snapshots.get((node_id, param_name)),
                after=after))
        for change in changes:
            self._host.record_track_change(change)
        return TimelineCommandResult(
            True, ERR_OK, "", plan_id=plan_id,
            affected_tracks=tuple(changes))

    # ── T16: add / remove keyframe ──
    def add_keyframe(self, node_id: str, param_name: str, frame: int,
                     value: float, *,
                     interpolation: str = "linear",
                     tangent_mode: str = "auto",
                     handle_left: Optional[Tuple[float, float]] = None,
                     handle_right: Optional[Tuple[float, float]] = None,
                     plan_id: str = "timeline-add-keyframe"
                     ) -> TimelineCommandResult:
        if self.locks.is_locked(node_id, param_name):
            return _locked(node_id, param_name, plan_id)
        node, code = self._node(node_id)
        if code != ERR_OK:
            return TimelineCommandResult(False, code, "node not found",
                                         plan_id=plan_id)
        return self._apply(plan_id, [{
            "op": "add_keyframe", "node_id": node_id,
            "param_name": param_name, "frame": frame, "value": value,
            "interpolation": interpolation, "tangent_mode": tangent_mode,
            "handle_left": handle_left, "handle_right": handle_right,
        }], [(node_id, param_name)])

    def remove_keyframe(self, node_id: str, param_name: str, frame: int,
                        plan_id: str = "timeline-remove-keyframe"
                        ) -> TimelineCommandResult:
        if self.locks.is_locked(node_id, param_name):
            return _locked(node_id, param_name, plan_id)
        return self._apply(plan_id, [{
            "op": "remove_keyframe", "node_id": node_id,
            "param_name": param_name, "frame": frame,
        }], [(node_id, param_name)])

    def clear_track(self, node_id: str, param_name: str,
                    plan_id: str = "timeline-clear-track"
                    ) -> TimelineCommandResult:
        if self.locks.is_locked(node_id, param_name):
            return _locked(node_id, param_name, plan_id)
        return self._apply(plan_id, [{
            "op": "clear_keyframes", "node_id": node_id,
            "param_name": param_name,
        }], [(node_id, param_name)])

    # ── T17: move keyframes transaction ──
    def begin_move(self, selection: List[Tuple[str, str, int]]
                   ) -> TimelineCommandResult:
        """Capture before-state for a drag.

        ``selection`` is a list of ``(node_id, param_name, frame)`` triples
        covering every keyframe the drag will move. Locks are checked once
        up front so a locked track aborts the whole drag (no partial move).
        Returns a non-OK result if any target is locked or missing; the
        caller should NOT start the drag in that case.
        """
        affected: List[Tuple[str, str]] = []
        before: Dict[Tuple[str, str], Optional[Dict[str, Any]]] = {}
        graph = self._host.get_node_graph()
        seen_tracks: set = set()
        for node_id, param_name, frame in selection:
            if self.locks.is_locked(node_id, param_name):
                self._drag.reset()
                return _locked(node_id, param_name, "timeline-move-keyframes")
            key = (node_id, param_name)
            if key in seen_tracks:
                continue
            seen_tracks.add(key)
            node = graph.get_node(node_id) if graph is not None else None
            if node is None:
                self._drag.reset()
                return TimelineCommandResult(
                    False, ERR_NODE_NOT_FOUND, "node not found",
                    plan_id="timeline-move-keyframes")
            before[key] = self._snapshot_track(node, param_name)
            affected.append(key)
        # AUD-11010: remember each dragged keyframe's source payload so
        # commit_move can drop the source frames (a drag is a MOVE, not a
        # copy) and carry the original interpolation/tangent/handles to the
        # new frame.
        sources: Dict[Tuple[str, str], Dict[int, Dict[str, Any]]] = {}
        for node_id, param_name, frame in selection:
            key = (node_id, param_name)
            src_map = sources.setdefault(key, {})
            if int(frame) in src_map:
                continue
            for kf in ((before.get(key) or {}).get("keyframes") or []):
                if isinstance(kf, Mapping) and int(kf.get("frame", -1)) == int(frame):
                    src_map[int(frame)] = dict(kf)
                    break
        self._drag = DragTransaction(affected=affected, before=before,
                                     sources=sources, preview={}, active=True)
        return TimelineCommandResult(True, ERR_OK, "",
                                     plan_id="timeline-move-begin")

    def update_move_preview(self, moves: List[Tuple[str, str, int, int,
                                                    float]]) -> None:
        """Record the latest position of each dragged keyframe.

        ``moves`` are ``(node_id, param_name, src_frame, new_frame, value)``
        tuples — the *source* frame of the dragged key plus its current new
        frame/value (the view computes them from the cursor delta + snap).
        Safe to call many times during one drag: each update OVERWRITES the
        previous entry for the same dragged key (AUD-11010 — the old
        frame-keyed accumulation turned every intermediate position into a
        permanent keyframe at commit). Does not touch the graph.
        """
        if not self._drag.active:
            return
        for node_id, param_name, src_frame, new_frame, value in moves:
            self._drag.preview.setdefault(
                (node_id, param_name), {})[int(src_frame)] = (
                int(new_frame), float(value))

    def commit_move(self, plan_id: str = "timeline-move-keyframes"
                    ) -> TimelineCommandResult:
        """Apply the accumulated move as one CompositeEditPlan.

        AUD-11010: a drag is a MOVE. The source frame of every dragged
        keyframe is dropped from the track and re-added at its new frame
        carrying the source keyframe's interpolation/tangent/handles. (The
        previous implementation kept the source frames — every drag silently
        DUPLICATED the keyframes — and re-added moved keys with
        linear/auto defaults, losing their interpolation/handles.)
        Untouched keyframes keep their frame/value/interpolation/handles.
        Produces exactly one undo unit per track via :meth:`_apply`.
        """
        if not self._drag.active:
            return TimelineCommandResult(False, ERR_NO_TARGET,
                                         "no active drag", plan_id=plan_id)
        before = self._drag.before
        sources = self._drag.sources
        preview = self._drag.preview
        if not before or not preview:
            self._drag.reset()
            return TimelineCommandResult(False, ERR_NO_TARGET,
                                         "empty drag", plan_id=plan_id)
        operations: List[Mapping[str, Any]] = []
        affected: List[Tuple[str, str]] = []
        for (node_id, param_name), track_before in before.items():
            src_map = sources.get((node_id, param_name), {})
            preview_map = preview.get((node_id, param_name), {})
            # Keep the unmoved keyframes; drop each moved key's SOURCE frame
            # so the drag doesn't duplicate it.
            moved_srcs = set(preview_map.keys())
            kfs: List[Dict[str, Any]] = [
                dict(kf) for kf in (track_before or {}).get("keyframes", [])
                if int(kf.get("frame", -1)) not in moved_srcs]
            # Re-add the moved keys at their new frames with their own
            # payloads (interpolation/tangent/handles survive the move).
            for src_frame in sorted(preview_map.keys()):
                new_frame, value = preview_map[src_frame]
                payload = dict(src_map.get(
                    src_frame, {"frame": src_frame, "value": value,
                                "interpolation": "linear",
                                "tangent_mode": "auto"}))
                payload["frame"] = new_frame
                payload["value"] = value
                kfs.append(payload)
            new_kfs = sorted(kfs, key=lambda d: int(d["frame"]))
            operations.append({
                "op": "set_keyframes", "node_id": node_id,
                "param_name": param_name, "keyframes": new_kfs,
                "pre_extrap": (track_before or {}).get("pre_extrap",
                                                       "constant"),
                "post_extrap": (track_before or {}).get("post_extrap",
                                                        "constant"),
            })
            affected.append((node_id, param_name))
        self._drag.reset()
        return self._apply(plan_id, operations, affected)

    def cancel_move(self) -> None:
        """Drop the in-flight drag without touching the graph (Esc)."""
        self._drag.reset()

    # ── T18: copy / paste ──
    def copy_keyframes(self, source: List[Tuple[str, str, int, float, str]]
                       ) -> int:
        """Copy selected keyframes into the shared clipboard.

        ``source`` is a list of ``(node_id, param_name, frame, value,
        interpolation)`` from one source node/param (multi-track copy is
        out of scope for the v1 clipboard). Stores relative frames anchored
        on the earliest copied frame, plus interpolation. The host is
        expected to also pass handles via :class:`ClipboardEntry` extras if
        it has them — but the minimal 5-tuple is enough for the basic path.
        """
        if not source:
            self.clipboard.clear()
            return 0
        # All entries must share a source track (plan §6.3 v1 clipboard).
        node_id = source[0][0]
        param_name = source[0][1]
        graph = self._host.get_node_graph()
        node = graph.get_node(node_id) if graph is not None else None
        if node is None:
            return 0
        # Pull the full keyframe records so we keep handles/tangent.
        track_data = node.keyframe_tracks.get(param_name) or {}
        by_frame: Dict[int, Dict[str, Any]] = {
            int(kf.get("frame")): kf
            for kf in track_data.get("keyframes", [])
            if isinstance(kf, Mapping)}
        anchor = min(s[2] for s in source)
        entries: List[ClipboardEntry] = []
        for src_node_id, src_param, frame, value, interp in source:
            if src_node_id != node_id or src_param != param_name:
                continue  # clipboard is single-track in v1
            full = by_frame.get(int(frame), {})
            entries.append(ClipboardEntry(
                relative_frame=int(frame) - anchor,
                value=float(value),
                interpolation=str(full.get("interpolation", interp)),
                tangent_mode=str(full.get("tangent_mode", "auto")),
                handle_left=_tuple_or_none(full.get("handle_left")),
                handle_right=_tuple_or_none(full.get("handle_right")),
                extra={k: v for k, v in full.items()
                       if k not in {"frame", "value", "interpolation",
                                     "tangent_mode", "handle_left",
                                     "handle_right"}},
            ))
        self.clipboard.entries = entries
        self.clipboard.source_node_type = node.node_type
        self.clipboard.source_ptype = self._ptype_of(node, param_name)
        return len(entries)

    def paste_keyframes(self, target_node_id: str, target_param: str,
                        anchor_frame: int,
                        plan_id: str = "timeline-paste-keyframes"
                        ) -> TimelineCommandResult:
        """Paste the clipboard at ``anchor_frame`` (the playhead).

        The earliest clipboard entry lands at ``anchor_frame``; later
        entries keep their relative offset (plan §19.10). The target param
        must accept the source's ptype (scalar→scalar) or the paste is
        rejected with ``ERR_PASTE_TYPE_MISMATCH`` rather than silently
        truncating. Resulting keyframes become the new selection (the host
        updates the shared selection model).
        """
        if self.clipboard.is_empty():
            return TimelineCommandResult(False, ERR_NO_TARGET,
                                         "clipboard empty", plan_id=plan_id)
        if self.locks.is_locked(target_node_id, target_param):
            return _locked(target_node_id, target_param, plan_id)
        node, code = self._node(target_node_id)
        if code != ERR_OK:
            return TimelineCommandResult(False, code, "node not found",
                                         plan_id=plan_id)
        target_ptype = self._ptype_of(node, target_param)
        if (self.clipboard.source_ptype is not None
                and target_ptype is not None
                and target_ptype != self.clipboard.source_ptype):
            return TimelineCommandResult(
                False, ERR_PASTE_TYPE_MISMATCH,
                f"type mismatch: source={self.clipboard.source_ptype} "
                f"target={target_ptype}", plan_id=plan_id)
        # Merge paste into existing track so we don't clobber untouched kfs.
        existing = self._snapshot_track(node, target_param) or {}
        by_frame: Dict[int, Dict[str, Any]] = {
            int(kf.get("frame")): dict(kf)
            for kf in existing.get("keyframes", [])}
        for entry in self.clipboard.entries:
            frame = int(anchor_frame) + entry.relative_frame
            by_frame[frame] = {
                "frame": frame, "value": entry.value,
                "interpolation": entry.interpolation,
                "tangent_mode": entry.tangent_mode,
                "handle_left": list(entry.handle_left)
                               if entry.handle_left else None,
                "handle_right": list(entry.handle_right)
                                if entry.handle_right else None,
            }
        new_kfs = sorted(by_frame.values(), key=lambda d: d["frame"])
        return self._apply(plan_id, [{
            "op": "set_keyframes", "node_id": target_node_id,
            "param_name": target_param, "keyframes": new_kfs,
            "pre_extrap": existing.get("pre_extrap", "constant"),
            "post_extrap": existing.get("post_extrap", "constant"),
        }], [(target_node_id, target_param)])

    # ── T19: interpolation / tangent ──
    def set_interpolation(self, node_id: str, param_name: str, frame: int,
                          interpolation: str,
                          plan_id: str = "timeline-set-interpolation"
                          ) -> TimelineCommandResult:
        if self.locks.is_locked(node_id, param_name):
            return _locked(node_id, param_name, plan_id)
        return self._apply(plan_id, [{
            "op": "set_interpolation", "node_id": node_id,
            "param_name": param_name, "frame": frame,
            "interpolation": interpolation,
        }], [(node_id, param_name)])

    def set_tangent_mode(self, node_id: str, param_name: str, frame: int,
                         tangent_mode: str,
                         plan_id: str = "timeline-set-tangent"
                         ) -> TimelineCommandResult:
        if self.locks.is_locked(node_id, param_name):
            return _locked(node_id, param_name, plan_id)
        return self._apply(plan_id, [{
            "op": "set_tangent_mode", "node_id": node_id,
            "param_name": param_name, "frame": frame,
            "tangent_mode": tangent_mode,
        }], [(node_id, param_name)])

    def set_bezier_handles(self, node_id: str, param_name: str, frame: int, *,
                           handle_left: Optional[Tuple[float, float]] = None,
                           handle_right: Optional[Tuple[float, float]] = None,
                           plan_id: str = "timeline-set-handles"
                           ) -> TimelineCommandResult:
        if self.locks.is_locked(node_id, param_name):
            return _locked(node_id, param_name, plan_id)
        return self._apply(plan_id, [{
            "op": "set_bezier_handles", "node_id": node_id,
            "param_name": param_name, "frame": frame,
            "handle_left": handle_left, "handle_right": handle_right,
        }], [(node_id, param_name)])

    def set_extrapolation(self, node_id: str, param_name: str, *,
                          pre_extrap: str = "constant",
                          post_extrap: str = "constant",
                          plan_id: str = "timeline-set-extrap"
                          ) -> TimelineCommandResult:
        if self.locks.is_locked(node_id, param_name):
            return _locked(node_id, param_name, plan_id)
        return self._apply(plan_id, [{
            "op": "set_extrapolation", "node_id": node_id,
            "param_name": param_name, "pre_extrap": pre_extrap,
            "post_extrap": post_extrap,
        }], [(node_id, param_name)])

    # ── T20: expression ──
    def _emit_expression_change(self, change: ExpressionChange) -> None:
        """Forward an ExpressionChange to the host's undo hook, if any.

        AUD-11011: the v1 ``TimelineHost`` protocol only defines track/range
        hooks. Hosts that want expression undo implement
        ``record_expression_change`` (structural typing — absence is not an
        error, the change is still carried on the command result).
        """
        recorder = getattr(self._host, "record_expression_change", None)
        if callable(recorder):
            recorder(change)

    def set_expression(self, node_id: str, param_name: str,
                       expression: str,
                       plan_id: str = "timeline-set-expression"
                       ) -> TimelineCommandResult:
        if self.locks.is_locked(node_id, param_name):
            return _locked(node_id, param_name, plan_id)
        # Expression edits don't go through keyframe tracks, but they DO
        # remove the keyframe track (A-15 mutex, P0 §5). Capture the track
        # before so the host can restore it on undo.
        node, code = self._node(node_id)
        if code != ERR_OK:
            return TimelineCommandResult(False, code, "node not found",
                                         plan_id=plan_id)
        before_track = self._snapshot_track(node, param_name)
        # AUD-11011: capture the previous expression text so undo can
        # restore it (the track record alone cannot).
        before_text = node.get_expression(param_name)
        # Pass an empty affected list: the expression op mutates the node's
        # expression storage, not node.keyframe_tracks, so _apply would
        # otherwise emit a misleading TrackChange. We capture + emit the
        # before/after track ourselves below (so undo can restore the
        # keyframe track the A-15 mutex just removed).
        result = self._apply(plan_id, [{
            "op": "set_expression", "node_id": node_id,
            "param_name": param_name, "expression": expression,
        }], [])
        if result.ok:
            after_track = self._snapshot_track(node, param_name)
            track_change = None
            if before_track is not None:
                track_change = TrackChange(node_id=node_id,
                                           param_name=param_name,
                                           before=before_track,
                                           after=after_track)
            expr_change = ExpressionChange(
                node_id=node_id, param_name=param_name,
                before_text=before_text, after_text=expression,
                before_track=before_track, after_track=after_track)
            result = TimelineCommandResult(
                True, ERR_OK, "", plan_id=plan_id,
                affected_tracks=((track_change,)
                                 if track_change is not None else ()),
                expression_changes=(expr_change,))
            if track_change is not None:
                self._host.record_track_change(track_change)
            self._emit_expression_change(expr_change)
        return result

    def remove_expression(self, node_id: str, param_name: str,
                          plan_id: str = "timeline-remove-expression"
                          ) -> TimelineCommandResult:
        if self.locks.is_locked(node_id, param_name):
            return _locked(node_id, param_name, plan_id)
        node, code = self._node(node_id)
        if code != ERR_OK:
            return TimelineCommandResult(False, code, "node not found",
                                         plan_id=plan_id)
        # AUD-11011: remove_expression previously produced NO undo record —
        # a host relying on this layer could not undo the removal. Capture
        # the expression text (and the track state) before applying.
        before_text = node.get_expression(param_name)
        before_track = self._snapshot_track(node, param_name)
        result = self._apply(plan_id, [{
            "op": "remove_expression", "node_id": node_id,
            "param_name": param_name,
        }], [])
        if result.ok:
            expr_change = ExpressionChange(
                node_id=node_id, param_name=param_name,
                before_text=before_text, after_text=None,
                before_track=before_track,
                after_track=self._snapshot_track(node, param_name))
            result = TimelineCommandResult(
                True, ERR_OK, "", plan_id=plan_id,
                expression_changes=(expr_change,))
            self._emit_expression_change(expr_change)
        return result

    # ── T21: node range ──
    def set_node_range(self, node_id: str, *, start: Optional[int] = None,
                       end: Optional[int] = None,
                       plan_id: str = "timeline-set-node-range"
                       ) -> TimelineCommandResult:
        """Trim a node's effective time range.

        Host-side operation (not in v1 KEYFRAME_OPS). Mutates
        ``node.time_range`` and emits a :class:`NodeRangeChange` the host
        records as a ``node_time_range_change`` undo.
        """
        node, code = self._node(node_id)
        if code != ERR_OK:
            return TimelineCommandResult(False, code, "node not found",
                                         plan_id=plan_id)
        before = copy.deepcopy(getattr(node, "time_range", None))
        tr = dict(before) if isinstance(before, dict) else {}
        tr["enabled"] = True
        if start is not None:
            tr["comp_start"] = int(start)
        if end is not None:
            tr["comp_end"] = int(end)
        if "comp_start" in tr and "comp_end" in tr \
                and tr["comp_end"] < tr["comp_start"]:
            return TimelineCommandResult(
                False, ERR_VALUE_OUT_OF_RANGE,
                "range end before start", plan_id=plan_id)
        node.time_range = tr
        after = copy.deepcopy(tr)
        change = NodeRangeChange(node_id=node_id, before=before, after=after)
        self._host.record_node_range_change(change)
        return TimelineCommandResult(
            True, ERR_OK, "", plan_id=plan_id,
            node_range_changes=(change,))

    # ── T65: track lock (service-layer enforcement) ──
    def set_track_lock(self, node_id: str, param_name: str,
                       locked: bool) -> TimelineCommandResult:
        # No undo — lock state is session-only by design (plan §11.3 / §12).
        self.locks.set_locked(node_id, param_name, locked)
        return TimelineCommandResult(
            True, ERR_OK, "", plan_id="timeline-set-lock")


# ── helpers ─────────────────────────────────────────────────────────────


def _locked(node_id: str, param_name: str, plan_id: str
            ) -> TimelineCommandResult:
    return TimelineCommandResult(
        False, ERR_LOCKED,
        f"track locked: {node_id}.{param_name}", plan_id=plan_id)


def _tuple_or_none(value: Any) -> Optional[Tuple[float, float]]:
    if value is None:
        return None
    if isinstance(value, (list, tuple)) and len(value) == 2:
        try:
            return (float(value[0]), float(value[1]))
        except (TypeError, ValueError):
            return None
    return None


def _classify_error(message: str) -> str:
    """Map a CompositeEditError message to a stable code (T15)."""
    m = message.lower()
    if m.startswith("node not found"):
        return ERR_NODE_NOT_FOUND
    if m.startswith("parameter not found"):
        return ERR_PARAM_NOT_FOUND
    if "not animatable" in m:
        return ERR_NOT_ANIMATABLE
    if "keyframe not found" in m:
        return ERR_KEYFRAME_NOT_FOUND
    if m.startswith("frame must be"):
        return ERR_FRAME_INVALID
    if "outside parameter range" in m:
        return ERR_VALUE_OUT_OF_RANGE
    if m.startswith("invalid interpolation"):
        return ERR_INTERP_INVALID
    if m.startswith("invalid tangent"):
        return ERR_TANGENT_INVALID
    if m.startswith("invalid ") and "extrap" in m:
        return ERR_EXTRAP_INVALID
    if "_handle" in m or "handle_" in m:
        return ERR_HANDLE_INVALID
    if "expression" in m and "must be" in m:
        return ERR_EXPR_INVALID
    if "does not accept expressions" in m:
        return ERR_EXPR_NOT_ALLOWED
    return ERR_PLAN_FAILED


# ── video media range linkage ───────────────────────────────────────────


def _video_source_node_for(target_node: Any, graph: Any,
                           layers_map: Optional[Mapping[str, Any]]) -> Any:
    """Resolve the video_frame node a range edit applies to.

    Layer rows carry their bar on the root (Transform) node; the playback
    range lives on the layer's video source node. Node-mode rows on a
    video_frame node map to themselves.
    """
    if getattr(target_node, "node_type", "") == "video_frame":
        return target_node
    if not layers_map or graph is None:
        return None
    for layer in layers_map.values():
        if getattr(layer, "root_node_id", None) != getattr(target_node, "id", None):
            continue
        source_id = getattr(layer, "source_node_id", None)
        if not source_id:
            continue
        try:
            source = graph.get_node(source_id)
        except Exception:
            source = None
        if source is not None and getattr(source, "node_type", "") == "video_frame":
            return source
    return None


def apply_video_range_edit(target_node: Any, graph: Any,
                           layers_map: Optional[Mapping[str, Any]],
                           new_range: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    """Link a Keyframes-panel bar edit to the video source's playback range.

    NLE trim semantics (content stays anchored to the composition):
      * ``trim_in``  — head trim: comp_start and source_in advance together.
      * ``trim_out`` — tail trim: duration shrinks; source_out = derived.
      * ``slide``    — body drag: the clip moves with its material, the
        source range is untouched.

    Args:
        target_node: Node whose bar was dragged (root node for layer rows).
        graph: NodeGraph used to resolve layer source nodes.
        layers_map: layer_id -> Layer mapping (layer stack projection).
        new_range: The ``range_changed`` payload: ``comp_start``/``comp_end``
            plus an ``edit`` key (``trim_in`` | ``trim_out`` | ``slide``).

    Returns:
        ``{"node_id", "before": {...}, "after": {...}}`` parameter snapshot
        for undo when the video source params changed, else ``None``.
    """
    video_node = _video_source_node_for(target_node, graph, layers_map)
    if video_node is None:
        return None
    edit = str(new_range.get("edit", "") or "")
    if edit not in ("trim_in", "trim_out"):
        # slide (and legacy payloads without an edit kind) keep the
        # material anchored to the moving bar.
        return None

    try:
        new_start = int(new_range.get("comp_start"))
        new_end = int(new_range.get("comp_end"))
    except (TypeError, ValueError):
        return None

    tr = getattr(target_node, "time_range", None)
    old_start = 0
    if isinstance(tr, dict):
        try:
            old_start = int(tr.get("comp_start", 0) or 0)
        except (TypeError, ValueError):
            old_start = 0

    params = getattr(video_node, "parameters", {})
    before: Dict[str, Any] = {}
    after: Dict[str, Any] = {}

    if edit == "trim_in":
        old_in = int(params.get("source_in", 0) or 0)
        new_in = max(0, old_in + (new_start - old_start))
        if new_in != old_in:
            before["source_in"] = old_in
            after["source_in"] = new_in
    else:  # trim_out
        old_in = int(params.get("source_in", 0) or 0)
        old_out = int(params.get("source_out", -1))
        new_out = old_in + (new_end - new_start)
        if new_out != old_out:
            before["source_out"] = old_out
            after["source_out"] = new_out

    if not after:
        return None
    for key, value in after.items():
        video_node.set_parameter(key, value)
    return {"node_id": video_node.id, "before": before, "after": after}


def video_source_node_for(target_node: Any, graph: Any,
                          layers_map: Optional[Mapping[str, Any]]) -> Any:
    """Resolve the video source behind a node/layer range bar.

    Unlike :func:`apply_video_range_edit`, this helper also returns a source
    for a slide edit where source_in/source_out stay unchanged but the source
    still needs the layer's composition visibility range.
    """
    return _video_source_node_for(target_node, graph, layers_map)


__all__ = [
    # error codes
    "ERR_OK", "ERR_NODE_NOT_FOUND", "ERR_PARAM_NOT_FOUND",
    "ERR_NOT_ANIMATABLE", "ERR_KEYFRAME_NOT_FOUND", "ERR_FRAME_INVALID",
    "ERR_VALUE_OUT_OF_RANGE", "ERR_INTERP_INVALID", "ERR_TANGENT_INVALID",
    "ERR_EXTRAP_INVALID", "ERR_HANDLE_INVALID", "ERR_LOCKED",
    "ERR_EXPR_INVALID", "ERR_EXPR_NOT_ALLOWED",
    "ERR_PASTE_TYPE_MISMATCH", "ERR_PLAN_FAILED", "ERR_NO_TARGET",
    # data
    "TimelineCommandResult", "TrackChange", "NodeRangeChange",
    "ExpressionChange",
    "ClipboardEntry", "Clipboard", "TrackLockRegistry", "DragTransaction",
    "TimelineHost",
    # the builder
    "TimelineCommandBuilder",
    # video media range linkage
    "apply_video_range_edit", "video_source_node_for",
]
