"""Raw GL texture binding that works on any current context.

Production historically reached for ``QOpenGLFunctions_4_1_Core`` to bind
textures by raw GL name. That class segfaults *inside*
``initializeOpenGLFunctions()`` when no Qt GL context is current on the
calling thread — exactly the situation of headless renders
(``scripts/topos_render.py`` drives the export renderer on a standalone
ModernGL context). PyOpenGL dispatches against whatever context is
current on the calling thread, Qt-backed or standalone alike, so it is
the correct primitive for name-based binding on the render thread.

All GL operations must still run on the rendering thread (see
.clinerules); this module performs no context management of its own.
"""

from OpenGL import GL as _gl
from OpenGL.GL import (  # noqa: F401  (re-export for call sites)
    GL_TEXTURE0,
    GL_TEXTURE_2D,
    glBindTexture,
    glActiveTexture,
)


def bind_texture_unit(texture_id: int, unit: int) -> None:
    """Bind a 2D texture name to a texture unit on the current context.

    Args:
        texture_id: raw GL texture name (``Texture.glo``).
        unit: texture unit index (0-based).

    When this process has never had a GL context current (pure-mock test
    runs), PyOpenGL cannot resolve the platform entry points and the
    imported callables stay null — calling them raises NullFunctionError.
    When a context was created and released earlier in the process, the
    entry points resolve but dispatch into a dead context raises
    GL 1282. Both cases mean "no usable current context" — there is no
    binding to change, so the call is a no-op. Resolution is re-checked
    per call via the module attribute so a context created later in the
    process still works.
    """
    active = _gl.glActiveTexture
    if not active:
        return
    get_current = getattr(_gl, "glGetCurrentContext", None)
    if get_current is not None:
        try:
            current = get_current()
        except Exception:
            current = None
        if not current:
            return
    bind = _gl.glBindTexture
    if not bind:
        return
    active(_gl.GL_TEXTURE0 + int(unit))
    bind(_gl.GL_TEXTURE_2D, int(texture_id))
