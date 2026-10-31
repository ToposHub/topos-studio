"""Cancellable background renderer for deterministic scene frames."""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
import threading
import uuid
from typing import Callable, Iterable

from ..core.contracts import AnimationFrame, RenderContext, RenderedFrame
from ..core.model import TeachingSceneDocument
from .cache import SceneRenderCache, build_cache_manifest
from .scene_renderer import CancellationToken


@dataclass(frozen=True)
class RenderProgress:
    job_id: str
    completed: int
    total: int
    frame: int | None = None


@dataclass(frozen=True)
class RenderResult:
    job_id: str
    frames: tuple[RenderedFrame, ...]
    cancelled: bool = False


class RenderHandle:
    def __init__(self, job_id: str, future: Future[RenderResult], token: CancellationToken) -> None:
        self.job_id = job_id
        self._future = future
        self._token = token

    def cancel(self) -> None:
        self._token.cancel()
        self._future.cancel()

    @property
    def done(self) -> bool:
        return self._future.done()

    def result(self, timeout: float | None = None) -> RenderResult:
        return self._future.result(timeout=timeout)


class RenderWorker:
    def __init__(self, *, cache: SceneRenderCache | None = None, max_workers: int = 1) -> None:
        self.cache = cache or SceneRenderCache()
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="teaching-render")
        # Running-job registry only: entries are evicted when their future
        # reaches a terminal state (see submit), so completed frames are not
        # pinned by the worker.  AUD-30310: submit (any thread), the
        # done-callback (executor thread) and shutdown can race on this dict —
        # guard it with a lock instead of relying on GIL atomicity.
        self._handles: dict[str, RenderHandle] = {}
        self._handles_lock = threading.Lock()

    def submit(self, scene: TeachingSceneDocument, frames: Iterable[int], context: RenderContext | None = None,
               on_progress: Callable[[RenderProgress], bool | None] | None = None) -> RenderHandle:
        frame_numbers = tuple(int(frame) for frame in frames)
        if context is None:
            from .scene_renderer_router import backend_for_scene
            context = RenderContext(
                fps=scene.fps, width=scene.width, height=scene.height,
                backend=backend_for_scene(scene),
            )
        job_id = f"render-{uuid.uuid4().hex}"
        token = CancellationToken()
        future = self._executor.submit(self._render, job_id, token, scene, frame_numbers, context, on_progress)
        handle = RenderHandle(job_id, future, token)
        with self._handles_lock:
            self._handles[job_id] = handle

        def _release_when_done(_future: Future[RenderResult]) -> None:
            # A finished Future pins the whole RenderResult — every RGBA frame
            # of the job — so the worker-side registry must not keep completed
            # jobs alive: a long-lived application worker would accumulate each
            # job's full frame set until OOM.  Callers hold their own
            # RenderHandle, so eviction never disturbs result()/cancel()/done.
            with self._handles_lock:
                self._handles.pop(job_id, None)

        future.add_done_callback(_release_when_done)
        return handle

    def render_sync(self, scene: TeachingSceneDocument, frames: Iterable[int], context: RenderContext | None = None,
                    on_progress: Callable[[RenderProgress], bool | None] | None = None) -> RenderResult:
        # AUD-30310: the callback annotation now matches submit() — a truthy
        # return cancels the job here as well, so one callback cannot carry
        # two different contracts depending on the entry point used.
        return self.submit(scene, frames, context, on_progress).result()

    def shutdown(self, wait: bool = True) -> None:
        with self._handles_lock:
            handles = tuple(self._handles.values())
        for handle in handles:
            if not handle.done:
                handle.cancel()
        self._executor.shutdown(wait=wait, cancel_futures=True)
        with self._handles_lock:
            self._handles.clear()

    def _render(self, job_id: str, token: CancellationToken, scene: TeachingSceneDocument,
                frames: tuple[int, ...], context: RenderContext,
                    on_progress: Callable[[RenderProgress], bool | None] | None) -> RenderResult:
        from .scene_renderer_router import select_scene_renderer
        renderer = select_scene_renderer(scene, cancellation=token)
        rendered: list[RenderedFrame] = []
        try:
            # Keep cleanup in the same finally block as prepare: asset
            # preflight or browser session setup can fail before the first
            # frame, and a renderer must still release any partial resources.
            renderer.prepare(scene, context)
            for index, frame_number in enumerate(frames):
                if token.is_cancelled:
                    return RenderResult(job_id, tuple(rendered), cancelled=True)
                # The pinned renderer build (chromium revision for HTML) takes
                # part in the cache key so a renderer upgrade never serves a
                # stale frame.  Read per frame (cheap): before the first capture
                # it is the pre-capture default, the first capture confirms the
                # real build — so a freshly rendered frame is re-keyed under the
                # confirmed build and stays reusable on subsequent renders.
                renderer_build = getattr(renderer, "renderer_build", "") or ""
                manifest = build_cache_manifest(
                    scene, frame_number, context, renderer_build=renderer_build)
                frame = self.cache.get(manifest)
                if frame is None:
                    frame = renderer.render_frame(AnimationFrame(frame_number, context.fps))
                    confirmed = getattr(renderer, "renderer_build", "") or ""
                    if confirmed != manifest.renderer_build:
                        manifest = build_cache_manifest(
                            scene, frame_number, context, renderer_build=confirmed)
                    self.cache.put(manifest, frame)
                rendered.append(frame)
                if on_progress is not None:
                    if on_progress(RenderProgress(job_id, index + 1, len(frames), frame_number)):
                        token.cancel()
            return RenderResult(job_id, tuple(rendered))
        finally:
            renderer.cleanup()


__all__ = ["RenderHandle", "RenderProgress", "RenderResult", "RenderWorker"]
