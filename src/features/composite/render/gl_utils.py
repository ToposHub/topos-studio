# DORMANT(2026-08-19 审计): 已实现且有测试，但暂无生产入口（有意休眠）。
# 接线前请勿在生产依赖；若半年内无接线计划建议删除。
"""
GL Utilities for AI Nodes - Safe GPU texture operations.

Provides minimal stable API for GPU texture readback and upload,
with safe fallback for mock/test contexts.

Architecture:
    - download_texture: GPU → CPU readback (for AI inference input)
    - upload_numpy_texture: CPU → GPU upload (for AI inference output)

Safety:
    - Detects mock context (no real GL) and returns placeholder
    - Test environments can run without OpenGL dependency
    - Production code runs in rendering thread with real ModernGL

Note:
    All GL operations must be in rendering thread (.clinerules constraint).
    Test path uses mock context which bypasses real GL calls.
"""

import logging
import numpy as np
from typing import Optional, Any

logger = logging.getLogger(__name__)

# Raw GL enums used by the framebuffer save/restore helpers (no PyOpenGL
# dependency: mirrors GPUResourcePool's ctypes pattern).
_GL_FRAMEBUFFER_BINDING = 0x8CA6  # GL_FRAMEBUFFER_BINDING
_GL_FRAMEBUFFER = 0x8D40          # GL_FRAMEBUFFER

# ── Passthrough shaders for texture blit ─────────────────────────────────────

_PASSTHROUGH_VERT = """
#version 410 core
in vec2 in_vert;
in vec2 in_uv;
out vec2 v_uv;
void main() {
    gl_Position = vec4(in_vert, 0.0, 1.0);
    v_uv = in_uv;
}
"""

_PASSTHROUGH_FRAG = """
#version 410 core
uniform sampler2D u_texture;
in vec2 v_uv;
out vec4 frag_color;
void main() {
    frag_color = texture(u_texture, v_uv);
}
"""


def _is_mock_context(ctx: Any) -> bool:
    """Check if context is a mock (no real OpenGL).

    Args:
        ctx: Context object (ModernGL or mock)

    Returns:
        True if mock context, False if real ModernGL
    """
    if ctx is None:
        return True
    # Check for mock context class name or missing attributes
    class_name = type(ctx).__name__
    if class_name in ('MockModernGLContext', 'MagicMock', 'Mock'):
        return True
    # Real ModernGL context has 'version' attribute
    if not hasattr(ctx, 'version'):
        return True
    return False


def _get_framebuffer_binding() -> Optional[int]:
    """Return the currently bound GL framebuffer (0 = default/screen).

    AUD-8307: download_texture must restore the CALLER's framebuffer
    binding, not blindly rebind the screen — composite pipelines routinely
    call it mid-offscreen-pass. Uses the process-global GL library (same
    pattern as GPUResourcePool's fence helpers); returns None when the
    binding cannot be queried (no context current / symbol unavailable).
    """
    import ctypes
    try:
        libgl = ctypes.CDLL(None)
        libgl.glGetIntegerv.argtypes = [ctypes.c_uint, ctypes.c_void_p]
        libgl.glGetIntegerv.restype = None
        buf = ctypes.c_int(0)
        libgl.glGetIntegerv(_GL_FRAMEBUFFER_BINDING, ctypes.byref(buf))
        return int(buf.value)
    except Exception as exc:
        logger.debug(f"glGetFramebufferBinding failed: {exc}")
        return None


def _restore_framebuffer_binding(fbo: int) -> None:
    """Rebind a framebuffer previously returned by _get_framebuffer_binding."""
    import ctypes
    try:
        libgl = ctypes.CDLL(None)
        libgl.glBindFramebuffer.argtypes = [ctypes.c_uint, ctypes.c_uint]
        libgl.glBindFramebuffer.restype = None
        libgl.glBindFramebuffer(_GL_FRAMEBUFFER, ctypes.c_uint(fbo))
    except Exception as exc:
        logger.debug(f"glBindFramebuffer restore failed: {exc}")


def download_texture(texture_id: int, width: int, height: int,
                     ctx: Optional[Any] = None) -> np.ndarray:
    """Download GPU texture data to CPU as numpy array.

    For AI inference: read texture from GPU to CPU memory.

    Args:
        texture_id: OpenGL texture ID (from TextureResult.texture_id)
        width: Texture width in pixels
        height: Texture height in pixels
        ctx: ModernGL context (optional, for production use)

    Returns:
        RGB float32 numpy array (h, w, 3), vertically flipped to image
        top-left order (AUD-8308: the passthrough shader performs no channel
        swap, so the data keeps the pipeline's RGBA/RGB channel order —
        pair with upload_numpy_texture, which writes without swapping).
        For mock context or missing ctx: returns zero array.

    Raises:
        RuntimeError: on production-path GL failure (context loss, shader
            compile error, readback failure). Errors are never masked with
            a black image (AUD-8309) — only the explicit mock/test path
            returns a zero array.
    """
    # Clamp dimensions
    w = max(1, width)
    h = max(1, height)

    # Safe fallback for mock/test context
    if ctx is None or _is_mock_context(ctx):
        logger.debug(f"download_texture: mock/test path, returning zero array ({h}x{w})")
        return np.zeros((h, w, 3), dtype=np.float32)

    # Production path: real GPU readback
    import moderngl

    # Save original GL state for restoration
    original_viewport = ctx.viewport
    # AUD-8307: actually track the caller's framebuffer binding so the
    # finally-block can restore it (the old code declared this variable and
    # then unconditionally bound ctx.screen, corrupting offscreen callers).
    original_fbo = _get_framebuffer_binding()

    readback_tex = None
    readback_fbo = None
    vbo = None
    vao = None
    prog = None

    try:
        # Create temporary texture + FBO for readback
        readback_tex = ctx.texture((w, h), 4, dtype='f4')
        readback_tex.filter = (moderngl.LINEAR, moderngl.LINEAR)
        readback_fbo = ctx.framebuffer(color_attachments=[readback_tex])
        readback_fbo.use()
        ctx.viewport = (0, 0, w, h)
        ctx.clear(0.0, 0.0, 0.0, 0.0)

        # Create passthrough shader program
        prog = ctx.program(
            vertex_shader=_PASSTHROUGH_VERT,
            fragment_shader=_PASSTHROUGH_FRAG,
        )

        # Bind source texture using raw OpenGL
        from PyQt6.QtOpenGL import QOpenGLFunctions_4_1_Core
        gl = QOpenGLFunctions_4_1_Core()
        gl.initializeOpenGLFunctions()
        gl.glActiveTexture(0x84C0)  # GL_TEXTURE0
        gl.glBindTexture(0x0DE1, texture_id)  # GL_TEXTURE_2D

        prog['u_texture'].value = 0

        # Full-screen quad
        vbo = ctx.buffer(np.array([
            -1.0, -1.0, 0.0, 0.0,
             1.0, -1.0, 1.0, 0.0,
            -1.0,  1.0, 0.0, 1.0,
             1.0,  1.0, 1.0, 1.0,
        ], dtype='f4'))
        vao = ctx.vertex_array(prog, [(vbo, '2f 2f', 'in_vert', 'in_uv')])
        vao.render(moderngl.TRIANGLE_STRIP)

        # Cleanup GL bindings
        gl.glActiveTexture(0x84C0)
        gl.glBindTexture(0x0DE1, 0)

        # Read pixels
        ctx.finish()
        raw = readback_fbo.read(components=4, dtype='f4')
        arr = np.frombuffer(raw, dtype=np.float32).reshape(h, w, 4)

        # Flip vertically (OpenGL bottom-left origin → image top-left)
        arr = np.flipud(arr).copy()

        # Drop alpha; channels stay in the source's RGBA order (AUD-8308).
        return arr[:, :, :3]  # RGB

    except Exception as e:
        # AUD-8309: never mask a production GL failure with a zero (black)
        # array — AI inference input would be silently replaced by a black
        # frame. Surface the error to the caller instead.
        raise RuntimeError(f"download_texture failed: {e}") from e
    finally:
        # Release all temporary resources
        if vao is not None:
            try:
                vao.release()
            except Exception:
                pass
        if vbo is not None:
            try:
                vbo.release()
            except Exception:
                pass
        if prog is not None:
            try:
                prog.release()
            except Exception:
                pass
        if readback_fbo is not None:
            try:
                readback_fbo.release()
            except Exception:
                pass
        if readback_tex is not None:
            try:
                readback_tex.release()
            except Exception:
                pass

        # Restore original GL state
        try:
            ctx.viewport = original_viewport
        except Exception:
            pass
        if original_fbo is not None:
            # Restore the binding the caller had (may be an offscreen FBO).
            _restore_framebuffer_binding(original_fbo)


def upload_numpy_texture(ctx: Any, image: np.ndarray,
                         fb_manager: Optional[Any] = None,
                         resource_pool: Optional[Any] = None) -> int:
    """Upload numpy array to GPU texture.

    For AI inference output: write result from CPU to GPU texture.

    Args:
        ctx: ModernGL context (or mock context for tests)
        image: RGB/RGBA float32 numpy array (h, w, 3 or 4). Written as-is —
            no channel swap is performed (AUD-8308), so data produced by
            download_texture round-trips unchanged.
        fb_manager: Optional FramebufferManager for texture pooling
        resource_pool: Optional GPUResourcePool for tracked allocation.
            When given, the texture is acquired through the pool
            (acquire_texture) so it is budget-tracked and can later be
            returned via release_texture_by_id(texture_id).

    Returns:
        Texture ID (OpenGL texture name). For mock context: returns mock ID.

    Raises:
        RuntimeError: on production-path GL failure (AUD-8309). The mock
            path keeps returning a placeholder ID for tests.
    """
    # Safe fallback for mock/test context
    if ctx is None or _is_mock_context(ctx):
        logger.debug("upload_numpy_texture: mock/test path, returning mock texture ID")
        # Mock context has texture() method that returns mock with .glo
        if hasattr(ctx, 'texture'):
            h, w = image.shape[:2]
            components = image.shape[2] if image.ndim == 3 else 1
            mock_tex = ctx.texture((w, h), components, dtype='f2')
            return mock_tex.glo
        # Fallback mock ID
        return 1

    # Production path: real GPU upload
    import moderngl

    h, w = image.shape[:2]
    components = image.shape[2] if image.ndim == 3 else 1

    # Ensure float32 dtype for upload
    if image.dtype != np.float32:
        image = image.astype(np.float32)

    # Prepare data (OpenGL expects bottom-left origin, flip)
    upload_data = np.flipud(image).copy()

    # Use resource pool if available (tracked allocation)
    # AUD-8306: the pool's API is acquire_texture (GPUResourcePool has no
    # allocate_texture), so the previous hasattr guard never matched and
    # every upload fell through to the untracked standalone path, leaking
    # one VRAM-sized texture per call. Pool-acquired textures are returned
    # to the pool through the standard release_texture_by_id(texture_id).
    if resource_pool is not None and hasattr(resource_pool, 'acquire_texture'):
        try:
            tex = resource_pool.acquire_texture(w, h, components, dtype='f4')
            tex.write(upload_data.tobytes())
            return tex.glo
        except Exception as e:
            raise RuntimeError(f"upload_numpy_texture failed: {e}") from e

    # Use framebuffer manager pool if available
    if fb_manager is not None and hasattr(fb_manager, 'get_temp_texture'):
        try:
            tex = fb_manager.get_temp_texture(w, h, components=components, dtype='f4')
            tex.write(upload_data.tobytes())
            return tex.glo
        except Exception as e:
            raise RuntimeError(f"upload_numpy_texture failed: {e}") from e

    # Create standalone texture (no pooling) — NOTE: the caller receives
    # only the glo, so this texture has no release handle; prefer passing
    # resource_pool so ownership is tracked.
    try:
        tex = ctx.texture((w, h), components, data=upload_data.tobytes(), dtype='f4')
        tex.filter = (moderngl.LINEAR, moderngl.LINEAR)
        tex.repeat_x = False
        tex.repeat_y = False

        return tex.glo
    except Exception as e:
        raise RuntimeError(f"upload_numpy_texture failed: {e}") from e


# ── Convenience functions ───────────────────────────────────────────────────

def create_mock_texture_id(ctx: Any, width: int, height: int) -> int:
    """Create a mock texture ID for test environments.

    Args:
        ctx: Mock ModernGL context
        width: Texture width
        height: Texture height

    Returns:
        Mock texture ID (positive integer)
    """
    if hasattr(ctx, 'texture'):
        tex = ctx.texture((width, height), 4, dtype='f2')
        return tex.glo
    return 1