"""
GL Context Hub — centralized GL context management and sharing detection.

Responsibilities:
1. Register GL contexts by name (viewer, renderer, preview, etc.)
2. Determine whether two contexts share texture objects
3. Cache sharing results to avoid redundant probes
4. Provide a single source of truth for direct-texture-presentation decisions

Thread boundary (AUD-1214/1215): every registry/cache access is guarded by
an internal lock, and the singleton build uses a double-checked lock like
``GLContextManager``.  ``probe_sharing`` itself issues **no GL calls**:
sharing is answered from context identity and (for Qt contexts) the
``QOpenGLContext.areSharing`` share-group check, so it may be called from
any thread.  The old 2x2 "pixel probe" issued raw PyOpenGL calls that act
on the *calling thread's current context* — ctx_b never participated, a
passing probe was a tautology (false positives enabling direct texture
presentation on non-sharing contexts) and the probe texture could be
deleted on an unrelated context (data corruption); it was removed.
"""

import logging
import threading
from typing import Any

logger = logging.getLogger(__name__)


class GLContextHub:
    """Singleton that tracks GL contexts and tests sharing between them."""

    _instance = None  # type: "GLContextHub | None"
    _instance_lock = threading.Lock()

    def __init__(self) -> None:
        self._contexts: dict[str, Any] = {}
        self._sharing_cache: dict[tuple[str, str], bool] = {}
        # AUD-1215: registry/cache mutations may race between the render
        # thread (register/probe) and arbitrary probe callers; an
        # unguarded interleaving with _invalidate_cache_for could leave a
        # stale True verdict behind and enable direct presentation on
        # non-sharing contexts.
        self._lock = threading.RLock()

    @classmethod
    def instance(cls) -> "GLContextHub":
        """Process-wide singleton (double-checked lock, AUD-1215)."""
        if cls._instance is None:
            with cls._instance_lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    def register_context(self, name: str, ctx: Any) -> None:
        """Register a GL context with a human-readable name."""
        with self._lock:
            old = self._contexts.get(name)
            if old is not None and old is not ctx:
                # Context changed — invalidate cache entries involving this name
                self._invalidate_cache_for(name)
            self._contexts[name] = ctx
        logger.info(f"[GL-HUB] Registered context: {name}")

    def get_context(self, name: str) -> Any:
        """Retrieve a registered context by name."""
        with self._lock:
            return self._contexts.get(name)

    def probe_sharing(self, name_a: str, name_b: str) -> bool:
        """
        Test whether two registered contexts share texture objects.

        Results are cached (keyed by the unordered name pair) so repeated
        queries are free.  Unknown names and unprovable pairs both cache
        and return False (fail-closed for direct presentation decisions).

        Thread note: performs no GL calls, so any thread may call it.
        """
        key = tuple(sorted([name_a, name_b]))
        with self._lock:
            if key in self._sharing_cache:
                return self._sharing_cache[key]
            ctx_a = self._contexts.get(name_a)
            ctx_b = self._contexts.get(name_b)
        if ctx_a is None or ctx_b is None:
            with self._lock:
                self._sharing_cache[key] = False
            return False

        # Probe outside the lock: it does no GL work and no dict access,
        # but keeping it out preserves that shape for future callers.
        result = self._probe_sharing(ctx_a, ctx_b)

        with self._lock:
            # Only cache when both names still map to the objects we
            # probed; a concurrent register_context invalidation must not
            # be overwritten by a stale verdict.
            if (self._contexts.get(name_a) is ctx_a
                    and self._contexts.get(name_b) is ctx_b):
                self._sharing_cache[key] = result
        level = logging.INFO if result else logging.WARNING
        logger.log(
            level,
            f"[GL-HUB] Probe {name_a} <-> {name_b}: "
            f"{'PASS' if result else 'FAIL'}",
        )
        return result

    @staticmethod
    def _probe_sharing(ctx_a: Any, ctx_b: Any) -> bool:
        """Determine whether ctx_a/ctx_b share GL objects — without GL calls.

        A genuine cross-context probe needs the owning thread of each
        context (create on ctx_a's thread, query on ctx_b's thread,
        delete back on ctx_a's thread); the hub cannot reach those
        threads from an arbitrary caller, so sharing is answered from
        metadata only:

        1. Same context object → trivially "shared" (single-context
           presentation path).
        2. Both Qt ``QOpenGLContext`` → ``QOpenGLContext.areSharing``
           (share-group comparison; no GL calls, no currency required).
        3. Anything else (e.g. moderngl contexts) → fail-closed False:
           direct texture presentation must never be enabled on an
           unproven pair.
        """
        if ctx_a is ctx_b:
            return True
        try:
            from PyQt6.QtGui import QOpenGLContext
        except ImportError:
            logger.warning(
                "[GL-HUB] sharing probe requires Qt contexts "
                "(PyQt6.QtGui unavailable); failing closed"
            )
            return False
        if isinstance(ctx_a, QOpenGLContext) and isinstance(ctx_b, QOpenGLContext):
            return bool(QOpenGLContext.areSharing(ctx_a, ctx_b))
        logger.warning(
            "[GL-HUB] sharing probe only supports QOpenGLContext "
            f"registrations (got {type(ctx_a).__name__} / "
            f"{type(ctx_b).__name__}); failing closed"
        )
        return False

    def is_direct_texture_enabled(self, renderer: str, viewer: str) -> bool:
        """Convenience: check if direct texture presentation is possible.

        True only when the renderer and viewer contexts are proven to
        share texture objects (probe-backed, not assumed).
        """
        return self.probe_sharing(renderer, viewer)

    def _invalidate_cache_for(self, name: str) -> None:
        """Remove cached sharing results involving a context name.

        Caller must hold ``self._lock``.
        """
        keys_to_remove = [k for k in self._sharing_cache if name in k]
        for k in keys_to_remove:
            del self._sharing_cache[k]

    def reset(self) -> None:
        """Clear all registrations and cache (for testing or context teardown)."""
        with self._lock:
            self._contexts.clear()
            self._sharing_cache.clear()
