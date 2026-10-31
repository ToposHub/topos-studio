"""FH-04 / RD-11: a pooled, lifecycle-managed HTML renderer service.

The pinned capture backend (:class:`PlaywrightChromiumBackend`) launches a
fresh, isolated browser per capture — strong crash isolation, but one frame at
a time.  Export needs throughput, so this service wraps one or more backends in
a **worker pool**: up to ``pool_size`` captures run concurrently (one browser
per in-flight capture, preserving the crash-isolation invariant), and a worker
that crashes is discarded and replaced by a fresh one rather than poisoning the
next job.

The service is the HTML half of RD-11's "renderer session/pool, page/process
ownership, timeout/cancel/crash recovery".  It is renderer-agnostic: it pools
any :class:`FrameCaptureBackend` produced by ``backend_factory`` (the factory
encapsulates the pinned build, the asset resolver, launch args).  A
:class:`~src.features.teaching_animation.render.html_scene_renderer_v2.HtmlSceneRendererV2`
adapts this pool to the frozen ``SceneRendererV2`` Protocol so an HTML scene
renders through the same contract as the SVG/GPU backends.

Design rules (matching the plan):
* fail-closed — ``render_frame`` returns the structured :class:`CaptureFailure`
  on timeout/cancel/crash, never a stale or blank frame;
* one process per in-flight capture — the pool bounds concurrency to
  ``pool_size``; a queue hands out workers, blocking (or reporting busy) when
  exhausted;
* crash recovery — a worker that returns ``CaptureFailureCode.CRASH`` (or
  raises) is not returned to the pool; the factory supplies a replacement.
"""

from __future__ import annotations

import queue
import threading
import time
import inspect
from typing import Callable, Mapping

from .html_renderer_spike import (
    CancelToken,
    CaptureFailure,
    CaptureFailureCode,
    CaptureSurface,
    CapturedFrame,
    FrameCaptureBackend,
    UrlPolicy,
)


class HtmlRendererService:
    """A pool of capture backends with crash recovery and bounded concurrency.

    ``backend_factory`` is called to create each pooled worker (and to replace a
    crashed one).  Callers may drive concurrency themselves by invoking
    :meth:`render_frame` from multiple threads; the internal queue bounds the
    number of in-flight captures to ``pool_size``.
    """

    def __init__(
        self,
        backend_factory: Callable[[], FrameCaptureBackend],
        *,
        pool_size: int = 1,
        default_timeout_ms: int = 10000,
    ) -> None:
        if pool_size <= 0:
            raise ValueError("pool_size must be positive")
        if default_timeout_ms <= 0:
            raise ValueError("default_timeout_ms must be positive")
        self._factory = backend_factory
        self._pool_size = pool_size
        self._default_timeout_ms = default_timeout_ms
        self._idle: queue.Queue[FrameCaptureBackend] = queue.Queue()
        self._lock = threading.Lock()
        self._closed = False
        # AUD-30130: replacements the pool still owes after a failed factory
        # rebuild (release must never raise, so the rebuild is retried lazily
        # at the next acquire).  ``_factory_errors`` keeps the last failure for
        # diagnostics instead of silently swallowing it.
        self._deficit = 0
        self._factory_errors: list[Exception] = []
        # AUD-30133: the pinned build is captured once from the first worker
        # created so the ``backend_build`` diagnostics property never has to
        # acquire() a worker (which blocks unboundedly while the pool is busy).
        self._backend_build = ""
        workers: list[FrameCaptureBackend] = []
        try:
            for _ in range(pool_size):
                workers.append(self._factory())
        except BaseException:
            # AUD-30134: a mid-loop factory failure used to leak every worker
            # created before it (each one is a live browser backend). Close
            # them before propagating so the process does not leak on a
            # partially-failed construction.
            for worker in workers:
                _close_quietly(worker)
            raise
        for worker in workers:
            self._idle.put(worker)
        if workers:
            self._backend_build = str(getattr(workers[0], "backend_build", ""))

    # ------------------------------------------------------------------
    # Pool access
    # ------------------------------------------------------------------
    @property
    def pool_size(self) -> int:
        return self._pool_size

    @property
    def pending_rebuilds(self) -> int:
        """Workers the pool still owes after failed replacement attempts."""
        return self._deficit

    @property
    def last_factory_error(self) -> Exception | None:
        """The most recent replacement-factory failure, for diagnostics."""
        with self._lock:
            return self._factory_errors[-1] if self._factory_errors else None

    @property
    def backend_build(self) -> str:
        """The pinned build of the pooled workers (stable across the pool).

        AUD-30133: served from the build captured when the first worker was
        created instead of acquiring a worker here — the old unbounded
        ``acquire()`` made this diagnostics property block forever whenever
        every pool worker was busy with a concurrent export.
        """
        # All workers come from the same factory, so any one reports the build.
        return self._backend_build

    def acquire(self, timeout: float | None = None) -> FrameCaptureBackend:
        """Take an idle worker, blocking up to ``timeout`` if the pool is empty."""
        if self._closed:
            raise RuntimeError("HtmlRendererService is closed")
        try:
            return self._idle.get(timeout=timeout)
        except queue.Empty as exc:
            # A worker the pool still owes (failed replacement) is rebuilt
            # here, at the point a caller is actually waiting for capacity.
            rebuilt = self._rebuild_owed_worker()
            if rebuilt is not None:
                return rebuilt
            raise TimeoutError("no HTML renderer worker available within timeout") from exc

    def release(self, worker: FrameCaptureBackend, *, healthy: bool = True) -> None:
        """Return a worker to the pool, or replace it if it crashed.

        Never raises: ``release`` runs in :meth:`render_frame`'s ``finally``,
        so an exception here would replace the capture result
        (AUD-30130) — a failed replacement factory call is recorded as a
        rebuild debt instead and retried at the next :meth:`acquire`.  The
        closed-check and the idle-queue put happen atomically under the same
        lock :meth:`close` uses to set ``_closed`` (AUD-30131): a worker
        released while the service is closing is either drained by ``close``
        or discarded here, never parked in the idle queue with nobody left to
        close it.
        """
        _clear_asset_resolver(worker)
        if healthy:
            with self._lock:
                if self._closed:
                    healthy = False  # service retired; discard below
                else:
                    self._idle.put(worker)
                    return
        # Discard the poisoned (or retired) worker; the factory supplies a
        # fresh one so the pool size stays constant across crashes.
        _close_quietly(worker)
        try:
            fresh = self._factory()
        except Exception as exc:
            # AUD-30130: the factory failing here (e.g. browser launch) must
            # not escape the finally in render_frame. Record the debt; the
            # next acquire() retries the rebuild so the pool does not
            # permanently shrink.
            with self._lock:
                self._deficit += 1
                self._factory_errors.clear()
                self._factory_errors.append(exc)
            return
        with self._lock:
            if self._closed:
                discard = True
            else:
                self._idle.put(fresh)
                discard = False
        if discard:
            _close_quietly(fresh)

    def _rebuild_owed_worker(self) -> FrameCaptureBackend | None:
        """Lazily rebuild one worker the pool still owes (AUD-30130).

        Returns ``None`` when nothing is owed or the rebuild failed again;
        the debt is restored on failure so a later acquire retries it.
        """
        with self._lock:
            if self._closed or self._deficit <= 0:
                return None
            self._deficit -= 1  # claimed; restored below on failure
        try:
            return self._factory()
        except Exception as exc:
            with self._lock:
                self._deficit += 1
                self._factory_errors.clear()
                self._factory_errors.append(exc)
            return None

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------
    def render_frame(
        self,
        html: str,
        surface: CaptureSurface,
        *,
        frame: int,
        fps_num: int,
        fps_den: int,
        csp: str,
        url_policy: UrlPolicy,
        asset_resolver: Mapping[str, bytes] | None = None,
        timeout_ms: int | None = None,
        cancel_token: CancelToken | None = None,
        transparent: bool = False,
        allow_uniform: bool = False,
    ) -> CapturedFrame | CaptureFailure:
        """Capture one frame on a pooled worker (fail-closed).

        Timeout and cancellation cover the **queue stage** as well as the
        capture itself: when the pool is exhausted, waiting for a worker is
        bounded by the remaining timeout budget and aborts immediately when
        the cancel token fires.  A crash-class failure (or an exception from
        the backend) causes the worker to be replaced; any other result
        returns the worker to the pool.
        """
        backend_id = "html"
        budget_ms = int(timeout_ms or self._default_timeout_ms)
        deadline = time.monotonic() + budget_ms / 1000.0

        # ---- Queue stage: bounded, cancel-aware worker acquisition -------
        worker: FrameCaptureBackend | None = None
        while worker is None:
            if self._closed:
                return CaptureFailure(CaptureFailureCode.CRASH, "service closed", backend_id)
            if cancel_token is not None and cancel_token.is_cancelled:
                return CaptureFailure(
                    CaptureFailureCode.CANCELLED, "cancelled while waiting for worker", backend_id)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return CaptureFailure(
                    CaptureFailureCode.TIMEOUT, "timed out waiting for a worker", backend_id)
            # Poll in short slices so cancellation is honoured promptly even
            # while blocked on a full pool, not just after the full budget.
            try:
                worker = self.acquire(timeout=min(remaining, _QUEUE_POLL_S))
            except TimeoutError:
                continue
        backend_id = getattr(worker, "backend_id", backend_id)

        # ---- Capture stage ----------------------------------------------
        healthy = True
        try:
            _bind_asset_resolver(worker, asset_resolver)
            remaining_ms = max(1, int((deadline - time.monotonic()) * 1000))
            capture_kwargs = dict(
                frame=frame, fps_num=fps_num, fps_den=fps_den,
                csp=csp, url_policy=url_policy,
                timeout_ms=remaining_ms,
                cancel_token=cancel_token,
                transparent=transparent,
            )
            # Keep third-party/test backends implementing the pre-flag
            # protocol usable while the built-in browser backend opts into
            # uniform-frame delivery for validated final renders.
            try:
                params = inspect.signature(worker.capture).parameters
            except (TypeError, ValueError):
                params = {}
            if "allow_uniform" in params or any(
                p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()
            ):
                capture_kwargs["allow_uniform"] = bool(allow_uniform)
            result = worker.capture(html, surface, **capture_kwargs)
            if isinstance(result, CaptureFailure) and result.code == CaptureFailureCode.CRASH:
                healthy = False
            return result
        except Exception:
            # Backend raised (process gone) — treat as crash, replace worker.
            healthy = False
            return CaptureFailure(
                CaptureFailureCode.CRASH, "worker raised during capture", backend_id)
        finally:
            self.release(worker, healthy=healthy)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def close(self) -> None:
        """Drain the pool and release every worker.  Idempotent."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
        drained: list[FrameCaptureBackend] = []
        while True:
            try:
                drained.append(self._idle.get_nowait())
            except queue.Empty:
                break
        for worker in drained:
            _close_quietly(worker)

    def __enter__(self) -> "HtmlRendererService":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


# Maximum time spent blocked on a full pool per poll iteration.  Kept small so
# the queue-stage cancel/timeout check runs promptly without busy-spinning.
_QUEUE_POLL_S = 0.2


def _bind_asset_resolver(worker: FrameCaptureBackend,
                         resolver: Mapping[str, bytes] | None) -> None:
    """Bind (or clear) the per-call asset resolver on a pooled worker.

    Always assigns the attribute — ``resolver is None`` clears any previous
    binding — so a reused pooled worker can never serve a later capture the
    bound assets of an earlier one (cross-request leak).  Backends that do not
    expose a settable ``_asset_resolver`` are left untouched.
    """
    payload = dict(resolver) if resolver else {}
    try:
        object.__setattr__(worker, "_asset_resolver", payload)
    except (AttributeError, TypeError):
        pass  # backend does not support per-call resolver override


def _clear_asset_resolver(worker: FrameCaptureBackend) -> None:
    """Reset a worker's resolver to empty (called on release)."""
    try:
        object.__setattr__(worker, "_asset_resolver", {})
    except (AttributeError, TypeError):
        pass


def _close_quietly(worker: FrameCaptureBackend) -> None:
    close = getattr(worker, "close", None)
    if callable(close):
        try:
            close()
        except Exception:
            pass


__all__ = ["HtmlRendererService"]
