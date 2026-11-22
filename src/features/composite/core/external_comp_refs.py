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

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import PurePosixPath, PureWindowsPath
from threading import Lock
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
REF_REVISION_MISMATCH = "ref.revision_mismatch"
REF_PROJECT_MISMATCH = "ref.project_mismatch"
REF_INVALID = "ref.invalid"
REF_UNSUPPORTED_CONTEXT = "ref.unsupported_context"

DEFAULT_MAX_DEPTH = 64
DEFAULT_MAX_NODES = 100_000
_HOST_LOCK = Lock()
_HOST_ARCHIVE = ""


def set_reference_host_archive(path: str | None) -> None:
    """Set the app's archive for newly captured composition snapshots."""
    global _HOST_ARCHIVE
    with _HOST_LOCK:
        _HOST_ARCHIVE = path or ""


def active_reference_host_archive() -> str:
    with _HOST_LOCK:
        return _HOST_ARCHIVE


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
        if any(not isinstance(value, str) or not value.strip()
               for value in (self.project_id, self.comp_id)):
            raise ValueError("External references require project and composition IDs")
        path = self.relative_path
        if (not isinstance(path, str) or "\0" in path
                or PurePosixPath(path).is_absolute() or PureWindowsPath(path).anchor
                or path.lower().startswith("file:")):
            raise ValueError("External references require a project-relative path")

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
        relative_path = data.get("relative_path", "")
        if relative_path is not None and not isinstance(relative_path, str):
            return None
        try:
            return cls(
                project_id=project_id, comp_id=comp_id,
                revision_fingerprint=str(data.get("revision_fingerprint", "")
                                         or ""),
                relative_path=relative_path or "",
                update_policy=str(data.get("update_policy",
                                           UPDATE_POLICY_PINNED)
                                  or UPDATE_POLICY_PINNED),
            )
        except ValueError:
            return None


def read_comp_ref(parameters: dict[str, Any]) -> CompRef | None:
    """Read the structured contract or the editable Precomp parameter fields."""
    data = parameters.get("external_comp_ref")
    if data == {}:
        return None
    if data is None and any(parameters.get(f"ref_{key}") for key in (
        "project_id", "comp_id", "relative_path", "revision_fingerprint",
    )):
        data = {key: parameters[f"ref_{key}"] for key in (
            "project_id", "comp_id", "relative_path", "revision_fingerprint",
            "update_policy",
        ) if f"ref_{key}" in parameters}
    if data is None:
        return None
    ref = CompRef.from_dict(data)
    if ref is None:
        raise ValueError("Invalid external composition reference")
    return ref


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
    _, problem = _dependency_order(start, dependencies, max_depth=None, max_nodes=None)
    return list(problem.path) if problem is not None else []


def _dependency_order(
    start: CompKey,
    dependencies: Dict[CompKey, Tuple[CompKey, ...]],
    *,
    max_depth: Optional[int],
    max_nodes: Optional[int],
) -> Tuple[List[CompKey], Optional[RefDiagnostic]]:
    """Preflight a reference graph before loading or validating any payloads."""
    if max_depth is not None and max_depth < 1:
        return [], RefDiagnostic(REF_DEPTH_EXCEEDED, start,
                                 f"Reference chain deeper than {max_depth}")
    if max_nodes is not None and max_nodes < 1:
        return [], RefDiagnostic(REF_SIZE_EXCEEDED, start,
                                 f"Reference graph exceeds {max_nodes} compositions")

    active = {start: 0}
    stack: List[Tuple[CompKey, Iterator[CompKey]]] = [(start, iter(dependencies.get(start, ())))]
    heights = {start: 1}
    order: List[CompKey] = []
    while stack:
        key, children = stack[-1]
        child = next(children, None)
        if child is None:
            order.append(key)
            del active[key]
            stack.pop()
            if stack:
                parent = stack[-1][0]
                heights[parent] = max(heights[parent], 1 + heights[key])
            continue
        if child in active:
            cycle = tuple(key for key, _ in stack[active[child]:]) + (child,)
            return [], RefDiagnostic(REF_CYCLE, child,
                                     " → ".join(f"{p}/{c}" for p, c in cycle), cycle)
        if max_depth is not None and len(stack) + heights.get(child, 1) > max_depth:
            return [], RefDiagnostic(REF_DEPTH_EXCEEDED, child,
                                     f"Reference chain deeper than {max_depth}")
        if child in heights:
            heights[key] = max(heights[key], 1 + heights[child])
            continue
        if max_nodes is not None and len(heights) >= max_nodes:
            return [], RefDiagnostic(REF_SIZE_EXCEEDED, child,
                                     f"Reference graph exceeds {max_nodes} compositions")
        active[child] = len(stack)
        heights[child] = 1
        stack.append((child, iter(dependencies.get(child, ()))))
    return order, None


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
        max_nodes: int = DEFAULT_MAX_NODES,
        validate: Optional[Callable[[CompKey], Optional[RefDiagnostic]]] = None,
        done: Optional[set[CompKey]] = None,
) -> ReferenceResolution:
    """Resolve a reference DAG dependencies-first with bounded depth and size.

    ``validate`` reports per-key diagnostics (missing file / missing
    output / plugin mismatch); resolution stops at the first failure so
    callers never publish a partially validated graph.  ``done`` seeds the
    validated set so shared subgraphs (two references to the same comp)
    are validated once and reported as reused.
    """
    order, problem = _dependency_order(
        start, dependencies, max_depth=max_depth, max_nodes=max_nodes,
    )
    if problem is not None:
        return ReferenceResolution(ok=False, diagnostics=(problem,))

    # Cached payloads still count toward the structural limits. Skip their
    # payload validation and any descendants reached only through those keys.
    done_set = set(done or ())
    needed: set[CompKey] = set()
    reused: dict[CompKey, None] = {}
    pending = [iter((start,))]
    while pending:
        key = next(pending[-1], None)
        if key is None:
            pending.pop()
            continue
        if key in done_set or key in needed:
            reused[key] = None
            continue
        needed.add(key)
        pending.append(iter(dependencies.get(key, ())))

    load_order = tuple(key for key in order if key in needed)
    if validate is not None:
        for key in load_order:
            problem = validate(key)
            if problem is not None:
                return ReferenceResolution(ok=False, diagnostics=(problem,))
    return ReferenceResolution(
        ok=True, order=load_order,
        reused=tuple(reused))


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
