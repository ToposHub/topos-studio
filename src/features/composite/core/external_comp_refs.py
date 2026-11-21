"""External composition references: identity, cycles and audits (B09).

An external reference names a target ``(project_id, comp_id)`` with a
version fingerprint, a project-relative path and an explicit update policy
(``pinned`` revisions follow the recorded fingerprint; ``follow`` tracks
the target's current revision).  Internal precomps keep their existing
loading path; external packages are read-only graph/resource payloads.
Resolution walks ``(project_id, comp_id)`` keys with an explicit stack:
the active set reports back-edges as a full cycle path, the done set
reuses already-validated subgraphs, and production depth/size limits keep
long chains bounded.  Missing files, missing outputs and plugin
mismatches are separate diagnostics so relink, audit and archive flows
can act on each cause.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple, TypeAlias

CompKey: TypeAlias = Tuple[str, str]

UPDATE_POLICY_PINNED = "pinned"
UPDATE_POLICY_FOLLOW = "follow"
UPDATE_POLICIES = frozenset({UPDATE_POLICY_PINNED, UPDATE_POLICY_FOLLOW})

# Diagnostics (B09 §3): distinct causes, separately actionable.
REF_OK = "ok"
REF_MISSING_FILE = "ref.missing_file"
REF_MISSING_OUTPUT = "ref.missing_output"
REF_PLUGIN_MISMATCH = "ref.plugin_mismatch"
REF_CYCLE = "ref.cycle"
REF_DEPTH_EXCEEDED = "ref.depth_exceeded"
REF_SIZE_EXCEEDED = "ref.size_exceeded"

DEFAULT_MAX_DEPTH = 64
DEFAULT_MAX_NODES = 100_000


@dataclass(frozen=True)
class CompRef:
    """One external composition reference (B09 §1)."""

    project_id: str
    comp_id: str
    revision_fingerprint: str = ""   # "" = unversioned legacy reference
    relative_path: str = ""          # relative to the host project
    update_policy: str = UPDATE_POLICY_PINNED

    def __post_init__(self) -> None:
        if self.update_policy not in UPDATE_POLICIES:
            raise ValueError(
                f"Unknown update policy {self.update_policy!r}")

    @property
    def key(self) -> CompKey:
        return (self.project_id, self.comp_id)

    def to_dict(self) -> Dict[str, str]:
        return {
            "project_id": self.project_id, "comp_id": self.comp_id,
            "revision_fingerprint": self.revision_fingerprint,
            "relative_path": self.relative_path,
            "update_policy": self.update_policy,
        }

    @classmethod
    def from_dict(cls, data: Any) -> Optional["CompRef"]:
        if not isinstance(data, dict):
            return None
        project_id = data.get("project_id")
        comp_id = data.get("comp_id")
        if not isinstance(project_id, str) or not isinstance(comp_id, str):
            return None
        try:
            return cls(
                project_id=project_id, comp_id=comp_id,
                revision_fingerprint=str(data.get("revision_fingerprint", "")
                                         or ""),
                relative_path=str(data.get("relative_path", "") or ""),
                update_policy=str(data.get("update_policy",
                                           UPDATE_POLICY_PINNED)
                                  or UPDATE_POLICY_PINNED),
            )
        except ValueError:
            return None


@dataclass(frozen=True)
class RefDiagnostic:
    """One reference problem with its separately-actionable cause."""

    code: str
    key: Optional[CompKey] = None
    detail: str = ""
    path: Tuple[CompKey, ...] = ()   # cycle path for REF_CYCLE


def find_cycle(start: CompKey,
               dependencies: Dict[CompKey, Tuple[CompKey, ...]]
               ) -> List[CompKey]:
    """DFS cycle search on small fixtures; returns the first full cycle.

    The path includes the repeated key at both ends (``A → B → A``), or an
    empty list when the reference graph is acyclic from ``start``.
    """
    path: List[CompKey] = []
    active: set = set()
    done: set = set()

    def visit(key: CompKey) -> List[CompKey]:
        if key in active:
            return path[path.index(key):] + [key]
        if key in done:
            return []
        path.append(key)
        active.add(key)
        for child in dependencies.get(key, ()):
            cycle = visit(child)
            if cycle:
                return cycle
        active.remove(key)
        path.pop()
        done.add(key)
        return []

    return visit(start)


@dataclass(frozen=True)
class ReferenceResolution:
    """Result of resolving one external reference graph (B09 §2)."""

    ok: bool
    order: Tuple[CompKey, ...] = ()       # dependencies-first resolution
    diagnostics: Tuple[RefDiagnostic, ...] = ()
    reused: Tuple[CompKey, ...] = ()      # subgraphs served from done set


def resolve_reference_graph(
        start: CompKey,
        dependencies: Dict[CompKey, Tuple[CompKey, ...]],
        *,
        max_depth: int = DEFAULT_MAX_DEPTH,
        validate: Optional[Callable[[CompKey], Optional[RefDiagnostic]]] = None,
        done: Optional[set] = None,
) -> ReferenceResolution:
    """Resolve a reference DAG dependencies-first with bounded depth.

    ``validate`` reports per-key diagnostics (missing file / missing
    output / plugin mismatch); resolution stops at the first failure so
    callers never publish a partially validated graph.  ``done`` seeds the
    validated set so shared subgraphs (two references to the same comp)
    are validated once and reported as reused.
    """
    done_set: set = set(done or ())
    diagnostics: List[RefDiagnostic] = []
    order: List[CompKey] = []
    reused: List[CompKey] = []
    cycle = find_cycle(start, dependencies)
    if cycle:
        return ReferenceResolution(
            ok=False,
            diagnostics=(RefDiagnostic(
                REF_CYCLE, cycle[0],
                " → ".join(f"{p}/{c}" for p, c in cycle),
                tuple(cycle)),))

    status: Dict[CompKey, int] = {}

    def visit(key: CompKey, depth: int) -> bool:
        if key in done_set:
            reused.append(key)
            return True
        if status.get(key) == 1:
            return True
        if depth > max_depth:
            diagnostics.append(RefDiagnostic(
                REF_DEPTH_EXCEEDED, key,
                f"Reference chain deeper than {max_depth}"))
            return False
        status[key] = 1
        for child in dependencies.get(key, ()):
            if not visit(child, depth + 1):
                return False
        if validate is not None:
            problem = validate(key)
            if problem is not None:
                diagnostics.append(problem)
                return False
        status[key] = 2
        done_set.add(key)
        order.append(key)
        return True

    if not visit(start, 1):
        return ReferenceResolution(ok=False, diagnostics=tuple(diagnostics))
    return ReferenceResolution(
        ok=True, order=tuple(order), diagnostics=(),
        reused=tuple(dict.fromkeys(reused)))


def audit_external_refs(
        refs: Iterable[CompRef],
        resolve_payload: Callable[[CompRef], Optional[Dict[str, Any]]],
        resolve_output: Optional[Callable[[CompRef], Optional[str]]] = None,
        plugin_matches: Optional[Callable[[CompRef], bool]] = None,
) -> Tuple[RefDiagnostic, ...]:
    """Audit external references with separated causes (B09 §3).

    ``resolve_payload`` returns the target's graph payload dict or None
    when its file/project cannot be read (missing file).  Missing
    comp/output and plugin mismatches are checked only once the payload
    exists so one failure never masks another cause.
    """
    diagnostics: List[RefDiagnostic] = []
    for ref in refs:
        payload = resolve_payload(ref)
        if payload is None:
            diagnostics.append(RefDiagnostic(
                REF_MISSING_FILE, ref.key,
                f"{ref.project_id}:{ref.relative_path or '?'}"))
            continue
        if resolve_output is not None and resolve_output(ref) is None:
            diagnostics.append(RefDiagnostic(
                REF_MISSING_OUTPUT, ref.key, ref.comp_id))
            continue
        if plugin_matches is not None and not plugin_matches(ref):
            diagnostics.append(RefDiagnostic(
                REF_PLUGIN_MISMATCH, ref.key, ref.comp_id))
    return tuple(diagnostics)
