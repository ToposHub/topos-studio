"""Shared in-process scene store for Edit, Composite and the editor window.

The scene document is the single source of truth.  Host objects only retain a
stable ``scene_id`` and subscribe to changes through this small, Qt-free
store.  Project IO can replace the store contents when a project is opened.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import count
from threading import Lock, RLock
from typing import Callable, Iterable

from .core.model import TeachingSceneDocument


SceneListener = Callable[[TeachingSceneDocument], None]

#: AUD-30504: revisions come from a process-wide monotonic sequence shared by
#: every store.  ``replace_all``/``clear`` used to reset a scene id's counter
#: back to 1, so reopening a project that reuses a scene id re-issued revision
#: numbers an earlier project had already used — cache keys like
#: ``scene:<id>:r1`` could then hit stale frames from the previous project.
#: A monotonic sequence makes every issued revision unique for the lifetime
#: of the process, across project switches and across stores.
_REVISION_SEQUENCE = count(1)
_REVISION_SEQUENCE_LOCK = Lock()


def _next_revision() -> int:
    with _REVISION_SEQUENCE_LOCK:
        return next(_REVISION_SEQUENCE)


@dataclass(frozen=True)
class SceneChange:
    scene: TeachingSceneDocument
    revision: int


class TeachingSceneStore:
    """Thread-safe scene repository scoped to the current application project."""

    def __init__(self) -> None:
        self._scenes: dict[str, TeachingSceneDocument] = {}
        self._revisions: dict[str, int] = {}
        self._listeners: list[SceneListener] = []
        self._lock = RLock()

    def get(self, scene_id: str) -> TeachingSceneDocument | None:
        with self._lock:
            return self._scenes.get(scene_id)

    def scenes(self) -> tuple[TeachingSceneDocument, ...]:
        with self._lock:
            return tuple(self._scenes.values())

    def revision(self, scene_id: str) -> int:
        with self._lock:
            return self._revisions.get(scene_id, 0)

    def register(self, scene: TeachingSceneDocument) -> TeachingSceneDocument:
        """Add or replace a scene and notify all host consumers."""
        with self._lock:
            previous = self._scenes.get(scene.scene_id)
            if previous == scene:
                return scene
            self._scenes[scene.scene_id] = scene
            # Monotonic process-wide sequence (AUD-30504): a re-registered
            # scene id can never re-use a revision number an earlier project
            # already issued.
            self._revisions[scene.scene_id] = _next_revision()
            listeners = tuple(self._listeners)
        for listener in listeners:
            listener(scene)
        return scene

    def remove(self, scene_id: str) -> TeachingSceneDocument | None:
        with self._lock:
            removed = self._scenes.pop(scene_id, None)
            self._revisions.pop(scene_id, None)
            return removed

    def replace_all(self, scenes: Iterable[TeachingSceneDocument]) -> None:
        with self._lock:
            self._scenes.clear()
            self._revisions.clear()
        for scene in scenes:
            self.register(scene)

    def subscribe(self, listener: SceneListener) -> Callable[[], None]:
        with self._lock:
            self._listeners.append(listener)

        def unsubscribe() -> None:
            with self._lock:
                if listener in self._listeners:
                    self._listeners.remove(listener)

        return unsubscribe

    def clear(self) -> None:
        with self._lock:
            self._scenes.clear()
            self._revisions.clear()


_SHARED_STORE = TeachingSceneStore()


def get_shared_scene_store() -> TeachingSceneStore:
    return _SHARED_STORE


__all__ = ["SceneChange", "TeachingSceneStore", "get_shared_scene_store"]
