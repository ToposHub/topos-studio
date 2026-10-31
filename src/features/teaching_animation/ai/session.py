"""One shared Assistant session per project, regardless of host surface."""

from __future__ import annotations

from dataclasses import dataclass, field
import threading
import uuid

from .context_provider import TeachingSceneContextProvider


@dataclass
class TeachingAssistantSession:
    project_id: str
    context_provider: TeachingSceneContextProvider | None = None
    session_id: str = field(default_factory=lambda: f"teaching-ai-{uuid.uuid4().hex}")
    attached_surfaces: set[str] = field(default_factory=set)
    # AUD-27100: ``attached_surfaces`` is mutated from whichever thread the
    # host surface runs on; guard the set with a per-session lock.
    _surface_lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    def attach(self, surface_id: str) -> None:
        with self._surface_lock:
            self.attached_surfaces.add(surface_id)

    def detach(self, surface_id: str) -> None:
        with self._surface_lock:
            self.attached_surfaces.discard(surface_id)

    def attached_surface_ids(self) -> tuple[str, ...]:
        """Snapshot of the attached surface ids (thread-safe read)."""
        with self._surface_lock:
            return tuple(self.attached_surfaces)


class TeachingAssistantSessionRegistry:
    def __init__(self) -> None:
        self._sessions: dict[str, TeachingAssistantSession] = {}
        # AUD-27100: the registry is documented as "one session per project
        # across host surfaces"; different surfaces may call in from the UI
        # thread and background threads.  ``get_or_create`` used to be a
        # check-then-act without a lock — two racing callers each built a
        # session and the later write silently dropped the first session's
        # attached surfaces.  All registry access now goes through this lock.
        self._lock = threading.RLock()

    def get_or_create(self, project_id: str, *, context_provider: TeachingSceneContextProvider | None = None) -> TeachingAssistantSession:
        with self._lock:
            session = self._sessions.get(project_id)
            if session is None:
                session = TeachingAssistantSession(project_id, context_provider)
                self._sessions[project_id] = session
            elif context_provider is not None:
                session.context_provider = context_provider
            return session

    def get(self, project_id: str) -> TeachingAssistantSession | None:
        with self._lock:
            return self._sessions.get(project_id)

    def close_project(self, project_id: str) -> None:
        with self._lock:
            self._sessions.pop(project_id, None)


_GLOBAL_SESSION_REGISTRY = TeachingAssistantSessionRegistry()


def get_shared_teaching_assistant_session(project_id: str, *, context_provider: TeachingSceneContextProvider | None = None) -> TeachingAssistantSession:
    return _GLOBAL_SESSION_REGISTRY.get_or_create(project_id, context_provider=context_provider)


__all__ = ["TeachingAssistantSession", "TeachingAssistantSessionRegistry", "get_shared_teaching_assistant_session"]
