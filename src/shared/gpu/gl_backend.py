"""ModernGL bootstrap + pooled FBO/temp-texture management (shared GPU layer).

Thread contract (the important boundary):

- ModernGL contexts are thread-affine: every context created here is
  bound to the thread that called ``create_context()`` (in this project
  the render / GL producer thread).  ``GLContextManager.initialize``,
  ``create_shared_context``, ``create_texture`` and all
  ``FramebufferManager`` methods therefore MUST be called from the
  thread that owns the target context — never from the UI thread.
- ``GLContextManager.mutex`` (QMutex) is the shared GPU command lane:
  cross-thread GPU work serializes on it; ``GLContextManager.instance()``
  is the only genuinely thread-safe entry point (double-checked lock).
- ``FramebufferManager`` state (pools/caches) is plain dict/set state
  with no internal locking — a single GL thread owns one instance.

Resource rules: textures/FBOs handed out by ``FramebufferManager`` are
pooled, not owned by callers — return them via ``release_temp_texture``
and call ``cleanup()`` on the GL thread before the context is destroyed,
otherwise pooled GL objects leak.
"""

import logging
import threading

import moderngl
from PyQt6.QtCore import QObject, QMutex

logger = logging.getLogger(__name__)

class GLContextManager(QObject):
    """
    Manages the ModernGL context and coordinates between PyQt6 and ModernGL.
    Singleton-like access for global resource sharing.

    Attributes:
        ctx: The ModernGL context (or ``None`` before ``initialize``);
            only valid on the thread that created it.
        initialized: True once ``initialize`` succeeded.
        mutex: Global QMutex serializing GPU access across threads.
    """
    _instance = None
    _instance_lock = threading.Lock()

    def __init__(self):
        super().__init__()
        self.ctx = None
        self.initialized = False
        self.mutex = QMutex() # Global lock for GPU access
        # AUD-1210: thread that owns ``ctx`` (recorded at initialize time).
        # ModernGL contexts are thread-affine; cross-thread use is a
        # .clinerules violation and gets a warning (not an error, to keep
        # legacy call sites alive while they are migrated).
        self._owner_thread_id: int | None = None

    @classmethod
    def instance(cls) -> "GLContextManager":
        """Process-wide singleton (thread-safe, double-checked lock).

        The render thread and UI/GL producer threads may reach this on first
        use concurrently; an unguarded lazy build could create two managers
        and split the shared GPU command lane into two mutexes.
        """
        if cls._instance is None:
            with cls._instance_lock:
                if cls._instance is None:
                    cls._instance = GLContextManager()
        return cls._instance

    def initialize(self, pyqt_context=None) -> None:
        """
        Initializes the ModernGL context using an existing PyQt OpenGL context.

        ModernGL has no API to build a context *from* a QOpenGLContext
        object; ``moderngl.create_context()`` always adopts the calling
        thread's *current* GL context.  Therefore the ``pyqt_context``
        argument is validated instead of ignored (AUD-1210): when provided,
        it must be current on the calling thread — otherwise
        ``create_context()`` would silently bind whatever context happens
        to be current, which is exactly how a UI-thread caller ends up
        owning the render context (``.clinerules`` GL-affinity violation).
        Re-calls after a successful init are no-ops.
        """
        if self.initialized and self.ctx:
            return

        if pyqt_context is not None and not self._is_current_on_calling_thread(pyqt_context):
            raise RuntimeError(
                "GLContextManager.initialize: the provided pyqt_context is "
                "not current on the calling thread — ModernGL would bind "
                "the calling thread's current context instead. Call "
                "makeCurrent() on the owning (GL) thread before initialize."
            )

        # Create ModernGL context from existing context
        # (adopts the calling thread's current context)
        self.ctx = moderngl.create_context()
        self._owner_thread_id = threading.get_ident()
        self.initialized = True

    @staticmethod
    def _is_current_on_calling_thread(pyqt_context) -> bool:
        """True when ``pyqt_context`` is the calling thread's current context.

        No-ops to True when PyQt6 is unavailable (headless test hosts) —
        there is nothing to validate against in that case.
        """
        try:
            from PyQt6.QtGui import QOpenGLContext
        except ImportError:
            return True
        current = QOpenGLContext.currentContext()
        if current is None:
            return False
        return current is pyqt_context or current == pyqt_context

    def _warn_if_wrong_thread(self) -> None:
        """Warn when the context is used from a thread other than its owner."""
        owner = self._owner_thread_id
        if owner is not None and threading.get_ident() != owner:
            logger.warning(
                "GLContextManager: GL context created on thread %s is used "
                "from thread %s — ModernGL contexts are thread-affine "
                "(.clinerules: the UI thread must not touch GL).",
                owner,
                threading.get_ident(),
            )

    def create_shared_context(self) -> "moderngl.Context":
        """Return a NEW ModernGL context sharing objects with ``self.ctx``.

        ModernGL 5.12 has no ``Context.create_shared_context`` method; the
        supported route is ``moderngl.create_context(share=True)``, which
        asks the glcontext backend for a context in "share" mode (sharing
        object namespaces with the calling thread's current context).
        A plain ``create_context()`` call would just hand back the already
        attached context — not a shared one (AUD-1211).  ModernGL's own
        docs warn that context sharing is fragile; verify with a sharing
        probe (GLContextHub) before relying on cross-context presentation.

        Thread-affine: valid only on the calling (GL) thread, which must
        already have ``self.ctx`` current.
        """
        if not self.ctx:
            raise RuntimeError(
                "GLContextManager.create_shared_context: no context "
                "initialized — call initialize() on the GL thread first."
            )
        self._warn_if_wrong_thread()
        return moderngl.create_context(share=True)

    def create_texture(self, width, height, data=None, components=3, dtype='f1') -> "moderngl.Texture | None":
        """Helper to create a texture in the current context.

        GL thread only. Returns ``None`` when the manager has no context.
        Filtering is LINEAR and repeat is clamped to edge (no tile seams).
        """
        if not self.ctx:
            return None

        tex = self.ctx.texture((width, height), components, data=data, dtype=dtype)
        # Use LINEAR filtering for smooth scaling
        tex.filter = (moderngl.LINEAR, moderngl.LINEAR)
        # Disable repeat to avoid edge artifacts between tiles (CLAMP_TO_EDGE)
        tex.repeat_x = False
        tex.repeat_y = False
        return tex

    def create_framebuffer(self, color_attachments) -> "moderngl.Framebuffer | None":
        """Helper to create a framebuffer (GL thread only).

        Returns ``None`` when the manager has no context.
        """
        if not self.ctx:
            return None
        return self.ctx.framebuffer(color_attachments=color_attachments)

class FramebufferManager:
    """
    Manages temporary textures and framebuffers for multi-pass rendering.
    Uses an object pool to avoid frequent allocation overhead.

    Ownership: textures obtained via ``get_temp_texture`` are *leased* —
    return them with ``release_temp_texture``; ``cleanup`` releases
    everything still held. Single-threaded by contract: all methods must
    run on the GL thread that owns ``ctx``.
    """
    def __init__(self, ctx: "moderngl.Context"):
        self.ctx = ctx
        self._texture_pool = {} # (w, h, components, dtype) -> list of textures
        self._fbo_cache = {}    # texture -> framebuffer
        self._active_textures = set()

    def get_temp_texture(self, width, height, components=4, dtype='f2') -> "moderngl.Texture":
        """
        Retrieves a texture from the pool or creates a new one.

        dtype: ``'f2'`` is 16-bit half-float per channel (RGBA16F with the
        default components=4), ``'f4'`` is 32-bit float (RGBA32F).
        """
        key = (width, height, components, dtype)
        if key in self._texture_pool and self._texture_pool[key]:
            tex = self._texture_pool[key].pop()
        else:
            tex = self.ctx.texture((width, height), components, dtype=dtype)
            tex.filter = (moderngl.LINEAR, moderngl.LINEAR)
            tex.repeat_x = False
            tex.repeat_y = False
        
        self._active_textures.add(tex)
        return tex

    def get_fbo(self, texture) -> "moderngl.Framebuffer":
        """Returns a framebuffer object for the given texture, caching it if necessary."""
        if texture in self._fbo_cache:
            return self._fbo_cache[texture]
        
        fbo = self.ctx.framebuffer(color_attachments=[texture])
        self._fbo_cache[texture] = fbo
        return fbo

    def release_temp_texture(self, tex) -> None:
        """Returns a texture to the pool for reuse (idempotent per texture)."""
        if tex in self._active_textures:
            self._active_textures.remove(tex)
            key = (tex.width, tex.height, tex.components, tex.dtype)
            if key not in self._texture_pool:
                self._texture_pool[key] = []
            self._texture_pool[key].append(tex)

    def cleanup(self) -> None:
        """Releases all pooled OpenGL resources.

        GL thread only; must run before the owning context is destroyed.
        Individual release failures are logged and swallowed so cleanup
        always clears the pools.
        """
        for fbo in self._fbo_cache.values():
            try: fbo.release()
            except Exception: logger.debug("Failed to release FBO in cleanup")
        self._fbo_cache.clear()

        for tex_list in self._texture_pool.values():
            for tex in tex_list:
                try: tex.release()
                except Exception: logger.debug("Failed to release pooled texture in cleanup")
        self._texture_pool.clear()

        for tex in self._active_textures:
            try: tex.release()
            except Exception: logger.debug("Failed to release active texture in cleanup")
        self._active_textures.clear()
