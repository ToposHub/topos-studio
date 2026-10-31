"""Render externally owned NV12/P010 planes into a GPU RGB target.

This module is the render-thread counterpart of the VideoToolbox deferred
frame contract.  ``IOSurfaceGLBridge`` makes the planes visible in the
current OpenGL context; this pass converts them to an ordinary RGBA texture
without downloading the frame to NumPy or uploading it again from the CPU.

The pass deliberately accepts only two-plane OpenGL frames.  Other formats
must continue through the existing planar CPU-frame path until a matching
external shader and lifetime contract are implemented.
"""

from __future__ import annotations

import logging
from typing import Any, Sequence, Tuple

from src.shared.media.gpu_frame import GpuFramePlane

logger = logging.getLogger(__name__)


_VERTEX_SHADER = """
#version 330
in vec2 in_vert;
in vec2 in_texcoord;
out vec2 v_texcoord;
void main() {
    gl_Position = vec4(in_vert, 0.0, 1.0);
    v_texcoord = in_texcoord;
}
"""

_YUV_BODY = """
float range_mul = u_color_range == 1 ? 1.0 : (219.0 / 255.0);
float range_off = u_color_range == 1 ? 0.0 : (16.0 / 255.0);
y = max((y - range_off) / range_mul, 0.0);
float c_off = u_color_range == 1 ? 0.5 : (128.0 / 255.0);
float c_scale = u_color_range == 1 ? 1.0 : (255.0 / 224.0);
cb = (cb - c_off) * c_scale;
cr = (cr - c_off) * c_scale;
float r;
float g;
float b;
if (u_color_matrix == 0) {
    r = y + 1.402 * cr;
    g = y - 0.344136 * cb - 0.714136 * cr;
    b = y + 1.772 * cb;
} else if (u_color_matrix == 1) {
    r = y + 1.5748 * cr;
    g = y - 0.1873 * cb - 0.4681 * cr;
    b = y + 1.8556 * cb;
} else {
    r = y + 1.4746 * cr;
    g = y - 0.1646 * cb - 0.5714 * cr;
    b = y + 1.8814 * cb;
}
f_color = vec4(clamp(r, 0.0, 1.0), clamp(g, 0.0, 1.0),
               clamp(b, 0.0, 1.0), 1.0);
"""

_YUV_2D_FRAGMENT_SHADER = f"""
#version 330
uniform sampler2D u_y_plane;
uniform sampler2D u_uv_plane;
uniform int u_color_range;
uniform int u_color_matrix;
uniform int u_uv_component_mode;
in vec2 v_texcoord;
out vec4 f_color;
void main() {{
    float y = texture(u_y_plane, v_texcoord).r;
    vec4 uv_sample = texture(u_uv_plane, v_texcoord);
    vec2 uv = u_uv_component_mode == 1 ? uv_sample.ra : uv_sample.rg;
    float cb = uv.r;
    float cr = uv.g;
    {_YUV_BODY}
}}
"""

_YUV_RECT_FRAGMENT_SHADER = f"""
#version 330
#extension GL_ARB_texture_rectangle : enable
uniform sampler2DRect u_y_plane;
uniform sampler2DRect u_uv_plane;
uniform vec2 u_y_size;
uniform vec2 u_uv_size;
uniform int u_color_range;
uniform int u_color_matrix;
uniform int u_uv_component_mode;
in vec2 v_texcoord;
out vec4 f_color;
void main() {{
    float y = texture(u_y_plane, v_texcoord * u_y_size).r;
    vec4 uv_sample = texture(u_uv_plane, v_texcoord * u_uv_size);
    vec2 uv = u_uv_component_mode == 1 ? uv_sample.ra : uv_sample.rg;
    float cb = uv.r;
    float cr = uv.g;
    {_YUV_BODY}
}}
"""


class ExternalYuvToRgbPass:
    """GPU-only two-plane external YUV conversion pass.

    The class owns only the shader/VAO resources.  The input planes remain
    owned by the decoder/IOSurface bridge and are released by the caller
    after a GPU fence signals.
    """

    def __init__(self, ctx: Any) -> None:
        self._ctx = ctx
        self._program_2d: Any | None = None
        self._program_rect: Any | None = None
        self._vbo: Any | None = None
        self._vao_2d: Any | None = None
        self._vao_rect: Any | None = None

    def _ensure_resources(self) -> bool:
        if self._program_2d is not None and self._program_rect is not None:
            return self._vao_2d is not None and self._vao_rect is not None
        try:
            import moderngl
            import numpy as np
        except ImportError as error:
            # AUD-39000：领域错误必须留痕，不再静默吞掉
            logger.warning(
                "ExternalYuvToRgbPass: moderngl/numpy unavailable: %s", error
            )
            return False
        try:
            self._program_2d = self._ctx.program(
                vertex_shader=_VERTEX_SHADER,
                fragment_shader=_YUV_2D_FRAGMENT_SHADER,
            )
            self._program_rect = self._ctx.program(
                vertex_shader=_VERTEX_SHADER,
                fragment_shader=_YUV_RECT_FRAGMENT_SHADER,
            )
            # Match the edit renderer's non-flipped track-FBO convention:
            # final presentation performs the one display-orientation flip.
            vertices = np.array([
                -1.0, -1.0, 0.0, 0.0,
                 1.0, -1.0, 1.0, 0.0,
                -1.0,  1.0, 0.0, 1.0,
                 1.0,  1.0, 1.0, 1.0,
            ], dtype=np.float32)
            self._vbo = self._ctx.buffer(vertices)
            self._vao_2d = self._ctx.vertex_array(
                self._program_2d,
                [(self._vbo, "2f 2f", "in_vert", "in_texcoord")],
            )
            self._vao_rect = self._ctx.vertex_array(
                self._program_rect,
                [(self._vbo, "2f 2f", "in_vert", "in_texcoord")],
            )
            return True
        except moderngl.Error as error:
            # 着色器编译/GL 资源创建失败：记录领域错误并释放半建资源
            logger.warning(
                "ExternalYuvToRgbPass: shader/VAO creation failed: %s", error
            )
            self.release()
            return False
        except Exception:
            logger.exception("ExternalYuvToRgbPass: resource creation failed")
            self.release()
            return False

    def initialize(self) -> bool:
        """Compile the pass and allocate its fullscreen draw resources."""
        return self._ensure_resources()

    @staticmethod
    def _matrix_code(matrix: object) -> int:
        value = str(matrix or "bt709").lower()
        if value in {"bt601", "smpte170m", "bt470bg", "fcc"}:
            return 0
        if value in {"bt2020", "bt2020nc"}:
            return 2
        return 1

    def render(
        self,
        planes: Sequence[GpuFramePlane],
        gpu_frame: Any,
        output_fbo: Any,
        width: int,
        height: int,
    ) -> bool:
        """Render two external planes into ``output_fbo``.

        Returns ``False`` before issuing a draw when the frame does not meet
        the external OpenGL contract.  The caller can then release the frame
        and use its bounded CPU/planar fallback.
        """
        if len(planes) < 2:
            return False
        if any(not isinstance(plane, GpuFramePlane) for plane in planes[:2]):
            return False
        y_plane, uv_plane = planes[0], planes[1]
        if y_plane.api != "opengl" or uv_plane.api != "opengl":
            return False
        if not y_plane.texture_id or not uv_plane.texture_id:
            return False
        if y_plane.target not in {"2d", "rectangle"}:
            return False
        if uv_plane.target != y_plane.target:
            return False
        if width <= 0 or height <= 0:
            return False
        if not self._ensure_resources():
            return False

        try:
            import moderngl
            from OpenGL.GL import (
                GL_TEXTURE_2D,
                GL_TEXTURE_RECTANGLE,
                GL_TEXTURE0,
                glActiveTexture,
                glBindTexture,
                glGetUniformLocation,
                glUniform1i,
                glUseProgram,
            )
            try:
                from OpenGL.error import GLError
            except ImportError:  # 旧版 PyOpenGL 模块名
                from OpenGL.errors import GLError
        except ImportError as error:
            # AUD-39000：领域错误必须留痕，不再静默吞掉
            logger.warning(
                "ExternalYuvToRgbPass: OpenGL bindings unavailable: %s", error
            )
            return False

        use_rectangle = y_plane.target == "rectangle"
        program = self._program_rect if use_rectangle else self._program_2d
        vao = self._vao_rect if use_rectangle else self._vao_2d
        texture_target = GL_TEXTURE_RECTANGLE if use_rectangle else GL_TEXTURE_2D
        prev_viewport = self._ctx.viewport

        try:
            output_fbo.use()
            self._ctx.viewport = (0, 0, int(width), int(height))
            self._ctx.clear(0.0, 0.0, 0.0, 0.0)

            # Samplers default to texture unit 0: both must be pinned to their
            # units or the UV plane samples the Y texture and the conversion
            # emits wrong chroma while "succeeding". Raw glUniform1i like the
            # Edit renderer: moderngl never writes sampler2DRect uniform
            # values to GL, and item assignment is not reliable for samplers.
            glUseProgram(program.glo)
            glUniform1i(glGetUniformLocation(program.glo, "u_y_plane"), 0)
            glUniform1i(glGetUniformLocation(program.glo, "u_uv_plane"), 1)

            glActiveTexture(GL_TEXTURE0)
            glBindTexture(texture_target, int(y_plane.texture_id))
            glActiveTexture(GL_TEXTURE0 + 1)
            glBindTexture(texture_target, int(uv_plane.texture_id))

            program["u_color_range"] = (
                0 if str(getattr(gpu_frame, "color_range", "limited"))
                == "limited" else 1
            )
            program["u_color_matrix"] = self._matrix_code(
                getattr(gpu_frame, "color_matrix", "bt709")
            )
            # IOSurface's legacy luminance-alpha profile stores UV in R/A of
            # the *UV* plane; the Y plane is plain GL_LUMINANCE (see the
            # bridge's plane format table). Mirror video_frame_node's check.
            program["u_uv_component_mode"] = int(
                uv_plane.internal_format == "GL_LUMINANCE_ALPHA"
            )
            if use_rectangle:
                program["u_y_size"] = (
                    float(y_plane.width), float(y_plane.height)
                )
                program["u_uv_size"] = (
                    float(uv_plane.width), float(uv_plane.height)
                )
            vao.render(mode=moderngl.TRIANGLE_STRIP, vertices=4)
        except (moderngl.Error, GLError, KeyError) as error:
            # AUD-39000：记录 GL/着色器领域错误，并恢复本 pass 触碰过
            # 的 GL 状态（program/纹理绑定/活动纹理单元/viewport），
            # 不把脏状态泄漏给后续渲染。
            logger.warning("ExternalYuvToRgbPass: render failed: %s", error)
            self._restore_gl_state(
                glUseProgram=glUseProgram,
                glActiveTexture=glActiveTexture,
                glBindTexture=glBindTexture,
                texture_unit0=GL_TEXTURE0,
                texture_target=texture_target,
                prev_viewport=prev_viewport,
            )
            return False
        except Exception:
            logger.exception("ExternalYuvToRgbPass: render failed")
            self._restore_gl_state(
                glUseProgram=glUseProgram,
                glActiveTexture=glActiveTexture,
                glBindTexture=glBindTexture,
                texture_unit0=GL_TEXTURE0,
                texture_target=texture_target,
                prev_viewport=prev_viewport,
            )
            return False
        return True

    def _restore_gl_state(
        self,
        *,
        glUseProgram: Any,
        glActiveTexture: Any,
        glBindTexture: Any,
        texture_unit0: int,
        texture_target: int,
        prev_viewport: Tuple[int, ...],
    ) -> None:
        """render 失败路径的 GL 状态恢复（AUD-39000）。

        恢复本 pass 独占触碰的状态：program 绑定、纹理单元 0/1 上的
        纹理绑定、活动纹理单元、context viewport。恢复动作自身的失败
        仅记 debug 日志——它发生在已失败的渲染路径上，不得再抛出。
        """
        try:
            glUseProgram(0)
            glActiveTexture(texture_unit0 + 1)
            glBindTexture(texture_target, 0)
            glActiveTexture(texture_unit0)
            glBindTexture(texture_target, 0)
        except Exception:
            logger.debug(
                "ExternalYuvToRgbPass: GL binding restore failed",
                exc_info=True,
            )
        try:
            self._ctx.viewport = prev_viewport
        except Exception:
            logger.debug(
                "ExternalYuvToRgbPass: viewport restore failed",
                exc_info=True,
            )

    def release(self) -> None:
        """Release GL resources while the owning context is current."""
        for attr in ("_vao_2d", "_vao_rect", "_vbo", "_program_2d", "_program_rect"):
            resource = getattr(self, attr, None)
            if resource is not None:
                try:
                    resource.release()
                except Exception:
                    pass
                setattr(self, attr, None)
