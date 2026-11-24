"""OpenColorIO's analytical ACES display shader and context-owned tables."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from src.shared.color.color_space_descriptor import ACESCG_SCENE, ColorSpaceDescriptor
from src.shared.color.color_transform_provider import aces_output_gpu_shader
from src.shared.color.project_video_signal import ProjectAcesSettings
from src.shared.color.video_signal_gl import (
    VIDEO_SIGNAL_GLSL, bind_video_signal_transform, video_signal_uniform_values,
)


@dataclass(frozen=True)
class AcesShaderTable:
    sampler: str
    size: tuple[int, int]
    channels: int
    values: bytes


@lru_cache(maxsize=16)
def aces_shader_source(
    version: str, target: str,
) -> tuple[str, tuple[AcesShaderTable, ...]]:
    shader = aces_output_gpu_shader(version, target)
    if tuple(shader.get3DTextures()) or tuple(shader.getUniforms()):
        raise ValueError("The pinned ACES ODT returned unsupported GPU resources")
    tables = tuple(AcesShaderTable(
        table.samplerName, (table.width, max(1, table.height)),
        table.getValues().size // (table.width * max(1, table.height)),
        table.getValues().astype("float32", copy=False).tobytes(),
    ) for table in shader.getTextures())
    source = shader.getShaderText() + """
        vec3 topos_render_aces(vec3 rgb) {
            return topos_aces_ocio(vec4(topos_convert_video_signal(rgb), 1.0)).rgb;
        }
    """
    return source, tables


def aces_shader_key(aces: ProjectAcesSettings) -> tuple[str, str]:
    return aces.version, aces.target


class AcesShaderTextures:
    """Upload and bind OCIO's immutable 2D lookup tables in a ModernGL context."""

    def __init__(self, context: Any, tables: tuple[AcesShaderTable, ...]) -> None:
        import moderngl

        self._textures: list[tuple[str, Any]] = []
        try:
            for table in tables:
                texture = context.texture(table.size, table.channels, table.values, dtype="f4")
                self._textures.append((table.sampler, texture))
                texture.filter = (moderngl.NEAREST, moderngl.NEAREST)
                texture.repeat_x = texture.repeat_y = False
        except BaseException:
            self.release()
            raise

    def bind(self, program: Any, *, first_unit: int) -> None:
        for unit, (sampler, table) in enumerate(self._textures, start=first_unit):
            table.use(unit)
            program[sampler].value = unit

    def release(self) -> None:
        for _, texture in self._textures:
            texture.release()
        self._textures.clear()


class AcesVideoGLPass:
    """Render an ACES view into the currently bound ModernGL framebuffer."""

    def __init__(self, context: Any, aces: ProjectAcesSettings) -> None:
        source, tables = aces_shader_source(*aces_shader_key(aces))
        self.program = context.program(
            vertex_shader="""#version 330
                out vec2 uv;
                void main() {
                    uv = vec2((gl_VertexID << 1) & 2, gl_VertexID & 2);
                    gl_Position = vec4(uv * 2.0 - 1.0, 0.0, 1.0);
                }
            """,
            fragment_shader="#version 330\n" + VIDEO_SIGNAL_GLSL + source + """
                uniform sampler2D u_texture;
                uniform bool u_unpremultiply;
                uniform bool u_swizzle;
                in vec2 uv;
                out vec4 fragment;
                void main() {
                    vec4 color = texture(u_texture, uv);
                    if (u_swizzle) color.rgb = color.bgr;
                    if (u_unpremultiply) {
                        color.rgb = color.a > 0.0 ? color.rgb / color.a : vec3(0.0);
                    }
                    fragment = vec4(topos_render_aces(color.rgb), color.a);
                }
            """,
        )
        self._vao = None
        self._tables = None
        try:
            self._vao = context.vertex_array(self.program, [])
            self._tables = AcesShaderTextures(context, tables)
        except BaseException:
            self.release()
            raise

    def draw(
        self, texture: Any, source: ColorSpaceDescriptor, *,
        unpremultiply: bool = False, swizzle: bool = False,
    ) -> None:
        bind_video_signal_transform(self.program, source, ACESCG_SCENE)
        texture.use(0)
        self.program["u_texture"].value = 0
        self.program["u_unpremultiply"].value = unpremultiply
        self.program["u_swizzle"].value = swizzle
        self._tables.bind(self.program, first_unit=1)
        self._vao.render(vertices=3)

    def release(self) -> None:
        if self._tables is not None:
            self._tables.release()
        if self._vao is not None:
            self._vao.release()
        self.program.release()


def aces_scene_uniforms(source: ColorSpaceDescriptor) -> tuple[int, int, tuple[float, ...]]:
    return video_signal_uniform_values(source, ACESCG_SCENE)
