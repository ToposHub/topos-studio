"""Application-scoped background cache coordinator for saved scenes."""

from __future__ import annotations

from threading import Lock, RLock

from .core.contracts import RenderContext
from .core.model import TeachingSceneDocument
from .render.render_worker import RenderHandle, RenderWorker
from .render.cache import SceneRenderCache


class TeachingSceneCacheCoordinator:
    def __init__(self) -> None:
        self.cache = SceneRenderCache()
        self.worker = RenderWorker(cache=self.cache)
        self._handles: dict[str, RenderHandle] = {}
        self._lock = RLock()
        # AUD-30502: set under ``_lock`` by close(); schedule() refuses new
        # work afterwards (submitting to a shut-down executor would park the
        # handle as silently never-running work).
        self._closed: bool = False

    def schedule(self, scene: TeachingSceneDocument, *, on_progress=None) -> RenderHandle:
        """Render all scene frames in the worker and replace prior cache work."""
        with self._lock:
            if self._closed:
                raise RuntimeError(
                    "TeachingSceneCacheCoordinator is closed; "
                    "cannot schedule new cache renders")
            previous = self._handles.get(scene.scene_id)
            if previous is not None and not previous.done:
                previous.cancel()
            from .render.scene_renderer_router import backend_for_scene
            backend = backend_for_scene(scene)
            handle = self.worker.submit(
                scene,
                range(scene.duration_frames),
                RenderContext(
                    fps=scene.fps, width=scene.width, height=scene.height,
                    backend=backend,
                    # Cache is non-authoritative delivery work. A legitimate
                    # uniform intro frame must not abort the whole background
                    # job; export and explicit validation retain their own
                    # strict renderability gate.
                    allow_uniform_frames=backend == "html",
                ),
                on_progress,
            )
            self._handles[scene.scene_id] = handle
            return handle

    def invalidate(self, scene_id: str) -> int:
        with self._lock:
            return self.cache.invalidate_scene(scene_id)

    def close(self) -> None:
        """Shut the worker down and drop pending handles.

        AUD-30502: ``worker.shutdown()`` 与 ``_handles`` 清理必须与并发的
        ``schedule()`` 互斥——旧实现 shutdown 在锁外执行，窗口内并发的
        schedule() 可以往正在关闭的 executor 里提交新任务（句柄从此
        永远不会完成）。shutdown 本身持有 worker 内部锁、不取本对象
        的 ``_lock``（schedule 持锁调 submit 已证明可重入安全），故
        全程持 ``_lock`` 无死锁风险。重复 close 幂等。
        """
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self.worker.shutdown()
            self._handles.clear()


_SHARED_COORDINATOR: TeachingSceneCacheCoordinator | None = None
#: AUD-30501: the lazy init is a check-then-act race — two threads first
#: calling in together each built a coordinator (two RenderWorker thread
#: pools), whose in-flight jobs/invalidate could then miss each other.
_SHARED_COORDINATOR_LOCK = Lock()


def get_shared_scene_cache_coordinator() -> TeachingSceneCacheCoordinator:
    """Process-wide coordinator, created exactly once under a lock (AUD-30501)."""
    global _SHARED_COORDINATOR
    if _SHARED_COORDINATOR is None:
        with _SHARED_COORDINATOR_LOCK:
            if _SHARED_COORDINATOR is None:
                _SHARED_COORDINATOR = TeachingSceneCacheCoordinator()
    return _SHARED_COORDINATOR


def get_existing_scene_cache_coordinator() -> TeachingSceneCacheCoordinator | None:
    """Return the application cache coordinator without creating a worker."""
    return _SHARED_COORDINATOR


__all__ = [
    "TeachingSceneCacheCoordinator",
    "get_shared_scene_cache_coordinator",
    "get_existing_scene_cache_coordinator",
]
