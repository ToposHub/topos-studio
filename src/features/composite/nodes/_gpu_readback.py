"""Shared defensive GPU readback helper for self-evaluating nodes.

Source-action nodes (deflicker, frame_interpolate, …) evaluate on the CPU
but consume upstream GPU textures.  The evaluator resolves textures by id
through ``_get_texture_by_id``; nodes see the same holders and must
tolerate every container shape they occur in:

* ``FramebufferManager._active_textures`` — a **set** of textures
* ``FramebufferManager._texture_pool``   — dict[key -> list[texture]]
* ``GPUResourcePool._pooled_textures``   — dict[key -> list[texture]]

Everything is best-effort: any failure returns ``None`` and the caller
degrades visibly instead of crashing the render thread.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

import numpy as np

logger = logging.getLogger(__name__)


def find_texture(texture_id: int, fb_manager: Any,
                 resource_pool: Any = None) -> Optional[Any]:
    """Locate a ModernGL texture object by its OpenGL name (glo)."""
    holders = []
    for attr in ("_active_textures", "_texture_pool"):
        holder = getattr(fb_manager, attr, None) if fb_manager else None
        if holder is not None:
            holders.append(holder)
    pooled = getattr(resource_pool, "_pooled_textures", None)
    if pooled is not None:
        holders.append(pooled)

    for holder in holders:
        # A set of textures, a list of textures, or a dict of lists.
        if isinstance(holder, dict):
            candidates = (tex for lst in holder.values()
                          for tex in (lst if isinstance(lst, (list, tuple))
                                      else [lst]))
        else:
            candidates = holder
        for tex in candidates:
            if getattr(tex, "glo", None) == texture_id:
                return tex
    return None


def read_rgba(tex_result: Any, ctx: Any, fb_manager: Any,
              resource_pool: Any = None) -> Optional["np.ndarray"]:
    """Read a TextureResult's GPU texture back as a top-down RGBA array."""
    try:
        gl = getattr(ctx, "gl", ctx)
        tex = find_texture(tex_result.texture_id, fb_manager, resource_pool)
        if tex is None:
            return None
        fbo = (fb_manager.get_fbo(tex)
               if fb_manager is not None else gl.framebuffer(tex))
        fbo.use()
        gl.viewport = (0, 0, int(tex_result.width), int(tex_result.height))
        raw = fbo.read(components=4, dtype="f4")
        arr = np.frombuffer(raw, dtype=np.float32).reshape(
            int(tex_result.height), int(tex_result.width), 4)
        # GL rows start at the bottom; CPU arrays are top-down.
        return np.ascontiguousarray(np.flipud(arr))
    except Exception as exc:
        logger.warning("GPU readback failed: %s", exc)
        return None
