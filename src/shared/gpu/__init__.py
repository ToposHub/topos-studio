"""Shared GPU layer: cross-feature GL/backend foundation (plan P2/P3).

Re-exports the public surface of ``src.shared.gpu``:

- ``gl_backend``      : GLContextManager (ModernGL bootstrap + global GPU
  mutex) and FramebufferManager (pooled temp textures / FBOs).
- ``gl_context_hub``  : named-context registry + sharing probe.
- ``present_frame``   : present protocol (GpuPresentFrame, OutputSlotPool,
  TextureLease, SlotState).
- ``gl_resource_registry`` : GL object lifetime tracking (P3-03).
- ``graphics_backend``: backend-agnostic contract + thread-violation
  errors (P3-02).

Thread contract (GPU layer, the important boundary):

- Everything that touches a live GL context (GLContextManager creation of
  textures/FBOs, GLContextHub probes, GlPassTimer) must run on the thread
  that owns the current GL context — in this codebase the render thread.
  GLContextManager.mutex is the shared GPU command lane; UI threads never
  issue GL calls directly.
- Pure selection/capability modules re-exported here via
  ``backend_types``/``capabilities``/``backend_selector`` (import them
  directly from their own modules) are stateless functions of their
  arguments and are safe from any thread, as long as GL driver strings
  passed in were captured on the GL thread.

Module boundary: nothing in this package may import ``src.features.*``
or ``src.app.*``.
"""

from src.shared.gpu.gl_backend import GLContextManager, FramebufferManager
from src.shared.gpu.gl_context_hub import GLContextHub
from src.shared.gpu.present_frame import (
    GpuPresentFrame,
    OutputSlotPool,
    SlotState,
    TextureLease,
)
from src.shared.gpu.gl_resource_registry import (
    GLResourceRegistry,
    ResourceRecord,
)
from src.shared.gpu.graphics_backend import (
    BackendNotReadyError,
    BackendThreadViolationError,
    GraphicsBackend,
    GraphicsBackendError,
    GraphicsBackendState,
    PresentDescriptor,
    TextureDescriptor,
)

__all__ = [
    "GLContextManager",
    "FramebufferManager",
    "GLContextHub",
    # present protocol
    "GpuPresentFrame",
    "OutputSlotPool",
    "SlotState",
    "TextureLease",
    # resource registry (P3-03)
    "GLResourceRegistry",
    "ResourceRecord",
    # graphics backend contract (P3-02)
    "GraphicsBackend",
    "GraphicsBackendState",
    "GraphicsBackendError",
    "BackendNotReadyError",
    "BackendThreadViolationError",
    "TextureDescriptor",
    "PresentDescriptor",
]