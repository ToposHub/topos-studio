# DORMANT(2026-08-19 审计): 已实现且有测试，但暂无生产入口（有意休眠）。
# 接线前请勿在生产依赖；若半年内无接线计划建议删除。
"""
Viewer Handle Drawing - Helper functions for drawing tool handles in CompositeViewer.

S5-12: Crop/Reformat/Format handle rendering.

This module provides pure functions for handle drawing that can be imported
and called from composite_viewport.py without modifying its complex structure.
"""

import ctypes
import numpy as np
from typing import Any, Optional
from PyQt6.QtCore import QPointF

from .viewer_tool_system import (
    view_pos_to_ndc,
    compute_crop_handles,
    compute_reformat_handles,
)


# ── GL Constants (must match composite_viewport.py) ────────────────────────────

GL_ARRAY_BUFFER = 0x8892
GL_FLOAT = 0x1406
GL_FALSE = 0
GL_BLEND = 0x0BE2
GL_SRC_ALPHA = 0x0302
GL_ONE_MINUS_SRC_ALPHA = 0x0303
GL_TRIANGLE_STRIP = 0x0005
GL_LINE_LOOP = 0x0002
GL_LINES = 0x0001


# ── Handle Drawing Functions ──────────────────────────────────────────────────

def draw_crop_reformat_handles(
    gl_api: Any,
    overlay_prog: int,
    quad_vbo: int,
    vao: int,
    node_type: str,
    params: dict[str, Any],
    tex_width: float,
    tex_height: float,
    ndc_rect: tuple[float, float, float, float],
    viewport_width: float,
    viewport_height: float,
    dpr: float,
    hover_handle: Optional[str],
    selected_node_id: Optional[str],
) -> dict[str, QPointF]:
    """Draw Crop/Reformat handles and return computed handle positions.

    Args:
        gl_api: ctypes GL API object (from composite_viewport).
        overlay_prog: OpenGL shader program ID.
        quad_vbo: VBO ID for vertex data.
        vao: VAO ID.
        node_type: "crop" or "reformat".
        params: Node parameters dict.
        tex_width/height: Texture dimensions.
        ndc_rect: (x0, y0, x1, y1) image NDC rect.
        viewport_width/height: Widget dimensions.
        dpr: Device pixel ratio.
        hover_handle: Currently hovered handle ID.
        selected_node_id: Selected node ID (for context).

    Returns:
        Dict mapping handle IDs to view pixel positions.
    """
    if not params or not node_type:
        return {}

    # Compute handles
    if node_type == "crop":
        handles = compute_crop_handles(params, tex_width, tex_height, ndc_rect,
                                       viewport_width, viewport_height, dpr)
        rect_color = (1.0, 0.9, 0.2, 0.8)  # Yellow
        handle_color = (1.0, 1.0, 1.0, 1.0)
        hover_color = (0.2, 1.0, 0.2, 1.0)
    elif node_type == "reformat":
        handles = compute_reformat_handles(params, tex_width, tex_height, ndc_rect,
                                           viewport_width, viewport_height, dpr)
        rect_color = (0.0, 0.8, 1.0, 0.7)  # Cyan
        handle_color = (1.0, 1.0, 1.0, 1.0)
        hover_color = (0.2, 1.0, 0.2, 1.0)
    else:
        return {}

    # Setup GL state
    gl_api.glUseProgram(overlay_prog)
    color_loc = gl_api.glGetUniformLocation(overlay_prog, b"u_color")
    pos_loc = gl_api.glGetAttribLocation(overlay_prog, b"in_vert")

    gl_api.glEnable(GL_BLEND)
    gl_api.glBlendFunc(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA)

    # Draw rect outline from corners
    corner_keys = [f"{node_type}_top_left", f"{node_type}_top_right",
                   f"{node_type}_bottom_left", f"{node_type}_bottom_right"]

    if all(k in handles for k in corner_keys):
        tl, tr, bl, br = [handles[k] for k in corner_keys]

        # Convert view to NDC for rect vertices
        ndc_tl = view_pos_to_ndc(tl, viewport_width, viewport_height, dpr)
        ndc_tr = view_pos_to_ndc(tr, viewport_width, viewport_height, dpr)
        ndc_bl = view_pos_to_ndc(bl, viewport_width, viewport_height, dpr)
        ndc_br = view_pos_to_ndc(br, viewport_width, viewport_height, dpr)

        rect_verts = np.array([
            ndc_tl.x(), ndc_tl.y(), 0.0, 0.0,
            ndc_tr.x(), ndc_tr.y(), 0.0, 0.0,
            ndc_br.x(), ndc_br.y(), 0.0, 0.0,
            ndc_bl.x(), ndc_bl.y(), 0.0, 0.0,
        ], dtype=np.float32)

        r, g, b, a = rect_color
        gl_api.glUniform4f(color_loc, ctypes.c_float(r), ctypes.c_float(g),
                           ctypes.c_float(b), ctypes.c_float(a))
        gl_api.glBindBuffer(GL_ARRAY_BUFFER, quad_vbo)
        gl_api.glBufferSubData(GL_ARRAY_BUFFER, 0, rect_verts.nbytes, rect_verts.ctypes.data)
        gl_api.glBindVertexArray(vao)
        gl_api.glEnableVertexAttribArray(pos_loc)
        gl_api.glVertexAttribPointer(pos_loc, 2, GL_FLOAT, GL_FALSE, 16, None)
        gl_api.glLineWidth(ctypes.c_float(2.0))
        gl_api.glDrawArrays(GL_LINE_LOOP, 0, 4)

    # Draw handles (fixed screen size)
    handle_size = 8.0

    for handle_id, view_pos in handles.items():
        h_color = hover_color if handle_id == hover_handle else handle_color
        # AUD-11204: corner ids ("crop_top_left", …) end with "_left"/
        # "_right" too, so they must be matched BEFORE the edge suffixes —
        # the old order drew corners as thin vertical bars (edge style) and
        # _draw_handle_square was unreachable for crop/reformat.
        if handle_id.endswith(
                ("_top_left", "_top_right", "_bottom_left", "_bottom_right")):
            _draw_handle_square(gl_api, view_pos, handle_size, color_loc, pos_loc,
                                quad_vbo, vao, viewport_width, viewport_height, dpr, h_color)
        elif handle_id.endswith("_center"):
            _draw_handle_cross(gl_api, view_pos, handle_size, color_loc, pos_loc,
                               quad_vbo, vao, viewport_width, viewport_height, dpr, h_color)
        elif any(handle_id.endswith(e) for e in ["_left", "_right", "_top", "_bottom"]):
            _draw_handle_edge(gl_api, view_pos, handle_id, handle_size, color_loc, pos_loc,
                              quad_vbo, vao, viewport_width, viewport_height, dpr, h_color)
        else:
            _draw_handle_square(gl_api, view_pos, handle_size, color_loc, pos_loc,
                                quad_vbo, vao, viewport_width, viewport_height, dpr, h_color)

    gl_api.glDisable(GL_BLEND)
    gl_api.glUseProgram(0)

    return handles


def _draw_handle_square(gl_api, view_pos, size, color_loc, pos_loc,
                        quad_vbo, vao, vp_w, vp_h, dpr, color) -> None:
    """Draw a square handle at view position."""
    half = size / 2.0
    ndc0 = view_pos_to_ndc(QPointF(view_pos.x() - half, view_pos.y() - half), vp_w, vp_h, dpr)
    ndc1 = view_pos_to_ndc(QPointF(view_pos.x() + half, view_pos.y() + half), vp_w, vp_h, dpr)

    verts = np.array([
        ndc0.x(), ndc0.y(), 0.0, 0.0,
        ndc1.x(), ndc0.y(), 0.0, 0.0,
        ndc0.x(), ndc1.y(), 0.0, 0.0,
        ndc1.x(), ndc1.y(), 0.0, 0.0,
    ], dtype=np.float32)

    r, g, b, a = color
    gl_api.glUniform4f(color_loc, ctypes.c_float(r), ctypes.c_float(g),
                       ctypes.c_float(b), ctypes.c_float(a))
    gl_api.glBindBuffer(GL_ARRAY_BUFFER, quad_vbo)
    gl_api.glBufferSubData(GL_ARRAY_BUFFER, 0, verts.nbytes, verts.ctypes.data)
    gl_api.glBindVertexArray(vao)
    gl_api.glEnableVertexAttribArray(pos_loc)
    gl_api.glVertexAttribPointer(pos_loc, 2, GL_FLOAT, GL_FALSE, 16, None)
    gl_api.glDrawArrays(GL_TRIANGLE_STRIP, 0, 4)


def _draw_handle_edge(gl_api, view_pos, handle_id, size, color_loc, pos_loc,
                      quad_vbo, vao, vp_w, vp_h, dpr, color) -> None:
    """Draw an edge handle (horizontal/vertical bar)."""
    bar_len = size * 2.0
    bar_w = size * 0.6

    is_horiz = handle_id.endswith("_top") or handle_id.endswith("_bottom")
    if is_horiz:
        x0, y0 = view_pos.x() - bar_len / 2.0, view_pos.y() - bar_w / 2.0
        x1, y1 = view_pos.x() + bar_len / 2.0, view_pos.y() + bar_w / 2.0
    else:
        x0, y0 = view_pos.x() - bar_w / 2.0, view_pos.y() - bar_len / 2.0
        x1, y1 = view_pos.x() + bar_w / 2.0, view_pos.y() + bar_len / 2.0

    ndc0 = view_pos_to_ndc(QPointF(x0, y0), vp_w, vp_h, dpr)
    ndc1 = view_pos_to_ndc(QPointF(x1, y1), vp_w, vp_h, dpr)

    verts = np.array([
        ndc0.x(), ndc0.y(), 0.0, 0.0,
        ndc1.x(), ndc0.y(), 0.0, 0.0,
        ndc0.x(), ndc1.y(), 0.0, 0.0,
        ndc1.x(), ndc1.y(), 0.0, 0.0,
    ], dtype=np.float32)

    r, g, b, a = color
    gl_api.glUniform4f(color_loc, ctypes.c_float(r), ctypes.c_float(g),
                       ctypes.c_float(b), ctypes.c_float(a))
    gl_api.glBindBuffer(GL_ARRAY_BUFFER, quad_vbo)
    gl_api.glBufferSubData(GL_ARRAY_BUFFER, 0, verts.nbytes, verts.ctypes.data)
    gl_api.glBindVertexArray(vao)
    gl_api.glEnableVertexAttribArray(pos_loc)
    gl_api.glVertexAttribPointer(pos_loc, 2, GL_FLOAT, GL_FALSE, 16, None)
    gl_api.glDrawArrays(GL_TRIANGLE_STRIP, 0, 4)


def _draw_handle_cross(gl_api, view_pos, size, color_loc, pos_loc,
                       quad_vbo, vao, vp_w, vp_h, dpr, color) -> None:
    """Draw a cross handle at center position."""
    half = size

    h0 = view_pos_to_ndc(QPointF(view_pos.x() - half, view_pos.y()), vp_w, vp_h, dpr)
    h1 = view_pos_to_ndc(QPointF(view_pos.x() + half, view_pos.y()), vp_w, vp_h, dpr)
    v0 = view_pos_to_ndc(QPointF(view_pos.x(), view_pos.y() - half), vp_w, vp_h, dpr)
    v1 = view_pos_to_ndc(QPointF(view_pos.x(), view_pos.y() + half), vp_w, vp_h, dpr)

    r, g, b, a = color
    gl_api.glUniform4f(color_loc, ctypes.c_float(r), ctypes.c_float(g),
                       ctypes.c_float(b), ctypes.c_float(a))
    gl_api.glLineWidth(ctypes.c_float(2.0))

    verts = np.array([
        h0.x(), h0.y(), 0.0, 0.0, h1.x(), h1.y(), 0.0, 0.0,
        v0.x(), v0.y(), 0.0, 0.0, v1.x(), v1.y(), 0.0, 0.0,
    ], dtype=np.float32)

    gl_api.glBindBuffer(GL_ARRAY_BUFFER, quad_vbo)
    gl_api.glBufferSubData(GL_ARRAY_BUFFER, 0, verts.nbytes, verts.ctypes.data)
    gl_api.glBindVertexArray(vao)
    gl_api.glEnableVertexAttribArray(pos_loc)
    gl_api.glVertexAttribPointer(pos_loc, 2, GL_FLOAT, GL_FALSE, 16, None)
    gl_api.glDrawArrays(GL_LINES, 0, 4)