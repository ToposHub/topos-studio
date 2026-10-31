"""Bounded parent/child Agent runtime.

Subagents are intentionally provider-neutral. A host supplies a runner for a
named child preset; this module enforces lineage, depth, round and child-count
authority before the runner is called. It never inherits parent approval or
write authority implicitly.
"""

from __future__ import annotations

import uuid
from collections import OrderedDict
from dataclasses import dataclass
import hashlib
import threading
from typing import Any, Callable


class SubagentError(RuntimeError):
    """Fail-closed subagent admission error."""


#: Bounded bookkeeping caps (AUD-22414): supervisor state must not grow without
#: bound across long sessions.  Oldest records are evicted LRU-style; a later
#: continue of an evicted child fails closed with "unknown child run".
_MAX_TRACKED_PARENTS = 1024
_MAX_TRACKED_CHILDREN = 512


@dataclass(frozen=True)
class SubagentPolicy:
    max_depth: int = 2
    max_children_per_parent: int = 4
    max_rounds: int = 4
    allow_write_tools: bool = False

    def __post_init__(self) -> None:
        if self.max_depth < 0 or self.max_children_per_parent <= 0 or self.max_rounds <= 0:
            raise ValueError("invalid subagent policy")


@dataclass(frozen=True)
class SubagentRequest:
    child_run_id: str
    parent_run_id: str
    session_id: str
    agent_id: str
    prompt: str
    depth: int
    lineage: tuple[str, ...]
    allow_write_tools: bool = False
    round: int = 1

    def to_trace_dict(self) -> dict[str, Any]:
        """Return bounded metadata without persisting the child prompt."""

        return {
            "child_run_id": self.child_run_id,
            "parent_run_id": self.parent_run_id,
            "session_id": self.session_id,
            "agent_id": self.agent_id,
            "depth": self.depth,
            "lineage": list(self.lineage),
            "allow_write_tools": self.allow_write_tools,
            "round": self.round,
            "prompt_len": len(self.prompt),
            "prompt_hash": hashlib.sha256(self.prompt.encode("utf-8")).hexdigest()[:16],
        }


@dataclass(frozen=True)
class SubagentResult:
    child_run_id: str
    agent_id: str
    status: str
    text: str = ""
    round: int = 1
    error: str = ""
    lineage: tuple[str, ...] = ()

    def to_trace_dict(self) -> dict[str, Any]:
        """Return bounded completion metadata for the parent trace."""

        return {
            "child_run_id": self.child_run_id,
            "agent_id": self.agent_id,
            "status": self.status,
            "round": self.round,
            "lineage": list(self.lineage),
            "text_len": len(self.text),
            "text_hash": hashlib.sha256(self.text.encode("utf-8")).hexdigest()[:16],
            "error": self.error[:300],
        }


SubagentRunner = Callable[[SubagentRequest], SubagentResult | str]
EventSink = Callable[[str, str, dict[str, Any]], None]


@dataclass(frozen=True)
class SubagentDefinition:
    agent_id: str
    display_name: str
    runner: SubagentRunner
    continuable: bool = False


class SubagentRegistry:
    def __init__(self) -> None:
        self._definitions: dict[str, SubagentDefinition] = {}

    def register(self, definition: SubagentDefinition, *, replace: bool = False) -> bool:
        if definition.agent_id in self._definitions and not replace:
            return False
        self._definitions[definition.agent_id] = definition
        return True

    def get(self, agent_id: str) -> SubagentDefinition | None:
        return self._definitions.get(str(agent_id))

    def remove(self, agent_id: str) -> bool:
        return self._definitions.pop(str(agent_id), None) is not None


@dataclass(frozen=True)
class _ChildContinuation:
    """Bounded per-child bookkeeping kept while the child stays continuable.

    Deliberately excludes the child prompt: after a round finishes the prompt
    text is dead weight (and a privacy/retention liability), and
    ``continue_child`` never reads it (AUD-22414).  ``round`` doubles as the
    authoritative round counter.
    """

    child_run_id: str
    parent_run_id: str
    session_id: str
    agent_id: str
    depth: int
    lineage: tuple[str, ...]
    allow_write_tools: bool
    round: int


class SubagentSupervisor:
    """Admit and run named child agents under parent authority."""

    def __init__(
        self,
        registry: SubagentRegistry,
        *,
        policy: SubagentPolicy | None = None,
        event_sink: EventSink | None = None,
    ) -> None:
        self._registry = registry
        self._policy = policy or SubagentPolicy()
        self._event_sink = event_sink
        # Bounded LRU accounting (AUD-22414): parent child-counts and child
        # continuation records are capped; prompts are never retained.
        self._children_by_parent: OrderedDict[str, int] = OrderedDict()
        self._continuations: OrderedDict[str, _ChildContinuation] = OrderedDict()
        self._lock = threading.RLock()

    @property
    def registry(self) -> SubagentRegistry:
        """The definition registry exposed for host composition adapters."""

        return self._registry

    def run(
        self,
        *,
        parent_run_id: str,
        session_id: str,
        agent_id: str,
        prompt: str,
        parent_depth: int = 0,
        parent_lineage: tuple[str, ...] = (),
        parent_allows_write: bool = False,
    ) -> SubagentResult:
        definition = self._require_definition(agent_id)
        prompt = str(prompt or "").strip()
        if not prompt:
            raise SubagentError("subagent prompt cannot be empty")
        depth = int(parent_depth) + 1
        if depth > self._policy.max_depth:
            raise SubagentError("subagent maximum depth exceeded")
        with self._lock:
            children = self._children_by_parent.get(str(parent_run_id), 0)
            if children >= self._policy.max_children_per_parent:
                raise SubagentError("parent subagent child limit exceeded")
            child_run_id = uuid.uuid4().hex
            request = SubagentRequest(
                child_run_id=child_run_id,
                parent_run_id=str(parent_run_id),
                session_id=str(session_id),
                agent_id=definition.agent_id,
                prompt=prompt,
                depth=depth,
                lineage=tuple(parent_lineage) + (str(parent_run_id),),
                # Child authority can only narrow the parent, never elevate it.
                allow_write_tools=bool(parent_allows_write and self._policy.allow_write_tools),
            )
            self._children_by_parent[str(parent_run_id)] = children + 1
            self._children_by_parent.move_to_end(str(parent_run_id))
            while len(self._children_by_parent) > _MAX_TRACKED_PARENTS:
                self._children_by_parent.popitem(last=False)
            self._admit_continuation_locked(request)
        self._emit(child_run_id, "subagent.started", request)
        try:
            raw = definition.runner(request)
            result = raw if isinstance(raw, SubagentResult) else SubagentResult(
                child_run_id=child_run_id,
                agent_id=definition.agent_id,
                status="succeeded",
                text=str(raw or ""),
                lineage=request.lineage,
            )
        except Exception as exc:  # noqa: BLE001 - child failure is contained
            result = SubagentResult(
                child_run_id=child_run_id,
                agent_id=definition.agent_id,
                status="failed",
                error=f"{type(exc).__name__}: {exc}",
                lineage=request.lineage,
            )
        self._emit(child_run_id, "subagent.finished", result)
        with self._lock:
            self._touch_continuation_locked(child_run_id)
        return result

    def continue_child(self, child_run_id: str, prompt: str) -> SubagentResult:
        prompt = str(prompt or "").strip()
        # AUD-22415: admission (read rounds + limit check + write-back) runs in
        # one critical section.  The previous code checked the round in one
        # lock scope and wrote it back in another, so two concurrent follow-ups
        # could both pass max_rounds on the same stale value and run the
        # runner twice for one round.
        with self._lock:
            previous = self._continuations.get(str(child_run_id))
            if previous is None:
                raise SubagentError("unknown child run")
            definition = self._require_definition(previous.agent_id)
            if not definition.continuable:
                raise SubagentError("subagent is one-shot and cannot continue")
            if not prompt:
                raise SubagentError("subagent prompt cannot be empty")
            round_number = previous.round + 1
            if round_number > self._policy.max_rounds:
                raise SubagentError("subagent maximum rounds exceeded")
            request = SubagentRequest(
                child_run_id=previous.child_run_id,
                parent_run_id=previous.parent_run_id,
                session_id=previous.session_id,
                agent_id=previous.agent_id,
                prompt=prompt,
                depth=previous.depth,
                lineage=previous.lineage,
                allow_write_tools=previous.allow_write_tools,
                round=round_number,
            )
            # Check-and-set in the same critical section: the record already
            # carries the advanced round, so a concurrent follow-up sees it.
            self._admit_continuation_locked(request)
        self._emit(request.child_run_id, "subagent.followup", request)
        try:
            raw = definition.runner(request)
            result = raw if isinstance(raw, SubagentResult) else SubagentResult(
                child_run_id=request.child_run_id,
                agent_id=request.agent_id,
                status="succeeded",
                text=str(raw or ""),
                round=round_number,
                lineage=request.lineage,
            )
        except Exception as exc:  # noqa: BLE001
            result = SubagentResult(
                child_run_id=request.child_run_id,
                agent_id=request.agent_id,
                status="failed",
                round=round_number,
                error=f"{type(exc).__name__}: {exc}",
                lineage=request.lineage,
            )
        self._emit(request.child_run_id, "subagent.finished", result)
        with self._lock:
            self._touch_continuation_locked(request.child_run_id)
        return result

    def _admit_continuation_locked(self, request: SubagentRequest) -> None:
        """Store bounded continuation bookkeeping for an admitted round.

        Caller must hold ``self._lock``.  The prompt is intentionally not
        retained (AUD-22414); records are LRU-capped.  Records of finished
        children are deliberately kept (prompt-free) so that later
        ``continue_child`` calls fail with the same domain errors as before
        ("one-shot" / "maximum rounds exceeded") instead of "unknown child
        run"; cleanup happens via LRU eviction only.
        """

        record = _ChildContinuation(
            child_run_id=request.child_run_id,
            parent_run_id=request.parent_run_id,
            session_id=request.session_id,
            agent_id=request.agent_id,
            depth=request.depth,
            lineage=request.lineage,
            allow_write_tools=request.allow_write_tools,
            round=request.round,
        )
        self._continuations[request.child_run_id] = record
        self._continuations.move_to_end(request.child_run_id)
        while len(self._continuations) > _MAX_TRACKED_CHILDREN:
            self._continuations.popitem(last=False)

    def _touch_continuation_locked(self, child_run_id: str) -> None:
        """Refresh LRU recency of a finished child's bounded record."""

        if child_run_id in self._continuations:
            self._continuations.move_to_end(child_run_id)

    def _require_definition(self, agent_id: str) -> SubagentDefinition:
        definition = self._registry.get(agent_id)
        if definition is None:
            raise SubagentError(f"unknown subagent: {agent_id}")
        return definition

    def _emit(self, child_run_id: str, kind: str, value: Any) -> None:
        if self._event_sink is None:
            return
        payload = (
            value.to_trace_dict() if hasattr(value, "to_trace_dict")
            else value.to_dict() if hasattr(value, "to_dict") else {
            "agent_id": getattr(value, "agent_id", ""),
            "parent_run_id": getattr(value, "parent_run_id", ""),
            "depth": getattr(value, "depth", 0),
            "round": getattr(value, "round", 1),
            }
        )
        self._event_sink(child_run_id, kind, payload)


__all__ = [
    "SubagentDefinition",
    "SubagentError",
    "SubagentPolicy",
    "SubagentRegistry",
    "SubagentRequest",
    "SubagentResult",
    "SubagentSupervisor",
]
