"""Unified graph-edit commands with reason-coded preflight (B02).

Every cable edit funnels through :meth:`NodeGraph.apply_edit`, which stages
the candidate edge list, validates it completely (port existence, type
compatibility, input capacity, directed cycles) and publishes it in one
assignment under the graph lock.  Failures return machine-readable reason
codes instead of a bare ``False`` so snapping, diagnostics and tests can
explain rejections; the graph is byte-identical before and after a rejected
edit.

The command also carries an optimistic ``expected_revision`` (the graph
generation captured when the gesture started) and an optional stable
``command_id`` used to dedupe double-committed gestures.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Tuple

# Dotted reason codes, following the ``graph_preflight`` naming style.
EDIT_OK = "ok"
EDIT_STALE_REVISION = "edit.stale_revision"
EDIT_DUPLICATE_COMMAND = "edit.duplicate_command"
EDIT_EDGE_MISSING = "edit.edge_missing"
EDIT_NODE_MISSING = "edit.node_missing"
EDIT_PORT_MISSING = "edit.port_missing"
EDIT_TYPE_MISMATCH = "edit.type_mismatch"
EDIT_CAPACITY = "edit.capacity"
EDIT_CYCLE = "edit.cycle"
EDIT_LAYER_CONFLICT = "edit.layer_conflict"


@dataclass(frozen=True)
class GraphEditIssue:
    """One rejection reason with a machine-readable code (B02 §3)."""

    code: str
    message: str
    node_id: str = ""
    port: str = ""

    def to_dict(self) -> Dict[str, str]:
        return {"code": self.code, "message": self.message,
                "node_id": self.node_id, "port": self.port}


@dataclass(frozen=True)
class GraphEditResult:
    """Outcome of one :meth:`NodeGraph.apply_edit` invocation."""

    ok: bool
    issues: Tuple[GraphEditIssue, ...] = ()
    revision: int = -1
    command_id: str = ""

    @property
    def primary_code(self) -> str:
        return self.issues[0].code if self.issues else EDIT_OK

    @classmethod
    def rejected(cls, issue: GraphEditIssue, revision: int = -1,
                 command_id: str = "") -> "GraphEditResult":
        return cls(ok=False, issues=(issue,), revision=revision,
                   command_id=command_id)

    @classmethod
    def applied(cls, revision: int, command_id: str = "") -> "GraphEditResult":
        return cls(ok=True, issues=(), revision=revision, command_id=command_id)


@dataclass(frozen=True)
class GraphEditCommand:
    """One atomic cable edit: staging validation, single publication.

    ``expected_revision`` is the graph generation observed when the gesture
    began; ``-1`` skips the optimistic check (non-gesture callers).  A
    non-empty ``command_id`` is remembered and replaying the same id is
    rejected, which dedupes double-commit of one gesture (B02 §4).
    """

    removed: Tuple[Any, ...] = ()
    added: Tuple[Any, ...] = ()
    # Optional insertion index per added edge (undo replays that restore a
    # removed edge back to its original position); ``None`` appends.
    positions: Tuple[int, ...] | None = None
    command_id: str = ""
    expected_revision: int = -1
    replace_single_inputs: bool = False
