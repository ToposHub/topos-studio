"""Render a frozen Color graph on a caller-owned GL render context."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Iterator, Mapping
import uuid

import numpy as np

from src.features.color.core.color_graph_compiler import compile_graph
from src.features.color.core.color_graph_executor import ColorGraphExecutor
from src.features.color.core.color_graph_param_source import graph_node_layer_provider
from src.features.color.core.color_graph_snapshot import freeze_graph
from src.features.color.core.shader_compiler import ShaderPipeline
from src.features.color.shaders.shaders import PASSTHROUGH_VERTEX
from src.shared.gpu.framebuffer_scope import preserve_framebuffer, temporary_framebuffer


def graph_texture_fallback_reason(graph: Any, frame: int) -> str | None:
    """Capability check before the timeline worker decodes GPU-only sources."""
    from src.features.color.core.qualifier_cpu import qualifier_postprocess_active
    provider = graph_node_layer_provider(graph, frame=frame)

    def nodes(current: Any) -> Iterator[Any]:
        for node in current.nodes:
            yield node
            if node.compound_children is not None:
                yield from nodes(node.compound_children)

    for node in nodes(graph):
        payload = provider(node.node_id)
        if not payload or not payload.get("visible", True):
            continue
        qp = payload.get("qualifier_params") or {}
        if qp.get("enabled") and qualifier_postprocess_active(qp):
            return "color_graph_qualifier_postprocess_cpu"
        for plugin in payload.get("plugins") or ():
            if plugin.get("enabled", True):
                from src.features.color.core.shader_compiler import plugin_binding_from_instance
                if plugin_binding_from_instance(dict(plugin)) is None:
                    return "color_graph_plugin_unavailable"
    return None


class ColorGraphTextureRenderer:
    """Render-thread resources; outputs remain valid until their key is reused."""

    def __init__(self, ctx: Any) -> None:
        self.ctx = ctx
        self.pipeline = ShaderPipeline(ctx)
        self.executor = ColorGraphExecutor(ctx, self.pipeline, node_cache_budget_bytes=0)
        self.plan_cache: dict = {}
        self._request: str | None = None
        self._resources: list[Any] = []
        self._vaos: dict[int, Any] = {}
        self._programs: dict[str, Any] = {}
        self._outputs: dict[str, tuple[Any, Any]] = {}
        quad = np.array([-1,-1,0,0, 1,-1,1,0, -1,1,0,1, 1,1,1,1], dtype="f4")
        self._vbo = ctx.buffer(quad.tobytes())

    def _draw(self, program: Any) -> None:
        vao = self._vaos.get(program.glo)
        if vao is None:
            vao = self.ctx.vertex_array(program, [(self._vbo, "2f 2f", "in_vert", "in_texcoord")])
            self._vaos[program.glo] = vao
        import moderngl
        vao.render(mode=moderngl.TRIANGLE_STRIP)

    def _program(self, name: str) -> Any:
        if name not in self._programs:
            path = Path(__file__).resolve().parents[1] / "shaders" / f"{name}.glsl"
            fragment = {
                "mask_combine": """#version 330
uniform sampler2D u_matte_a, u_matte_b;
uniform vec4 u_mask_uv_rect;
in vec2 v_texcoord; out float f_result;
void main() { vec2 uv=u_mask_uv_rect.xy+v_texcoord*u_mask_uv_rect.zw;
f_result=texture(u_matte_a,v_texcoord).r*texture(u_matte_b,uv).r; }
""",
                "normalize_u16": """#version 330
uniform usampler2D u_input_tex;
in vec2 v_texcoord; out vec4 f_result;
void main() { f_result=vec4(vec3(texture(u_input_tex,v_texcoord).rgb)/65535.0,1.0); }
""",
            }.get(name)
            self._programs[name] = self.ctx.program(
                vertex_shader=PASSTHROUGH_VERTEX,
                fragment_shader=fragment if fragment is not None else path.read_text())
        return self._programs[name]

    def _texture(self, size: tuple[int, int], components: int, data: Any = None) -> Any:
        tex = self.ctx.texture(size, components, data=data, dtype="f4")
        import moderngl
        tex.filter = (moderngl.LINEAR, moderngl.LINEAR)
        tex.repeat_x = tex.repeat_y = False
        self._resources.append(tex)
        return tex

    def _matte(self, layer: Mapping[str, Any], source: Any) -> Any:
        from src.features.color.core.color_graph_cpu import UnsupportedNode
        qp = layer.get("qualifier_params") or {}
        from src.features.color.core.qualifier_cpu import qualifier_postprocess_active
        if qp.get("enabled") and qualifier_postprocess_active(qp):
            raise UnsupportedNode("Qualifier postprocess requires the CPU fallback")
        size = source.size
        mask = None
        if layer.get("mask_shapes"):
            from src.features.color.core.layer import MaskGroup, MaskShape
            from src.features.color.core.window_rasterizer import WindowRasterizer
            shapes = [(MaskGroup if item.get("type") == "group" else MaskShape).from_dict(dict(item))
                      for item in layer["mask_shapes"]]
            array = WindowRasterizer().render_items(shapes, *size)
            mask = self._texture(size, 1, np.ascontiguousarray(array, dtype="f4").tobytes())
        if not qp.get("enabled"):
            return mask
        matte = self._texture(size, 1)
        with temporary_framebuffer(self.ctx, [matte]):
            self.ctx.viewport = (0, 0, *size)
            prog = self._program("keyer")
            source.use(0)
            prog["u_input_tex"].value = 0
            fields = {
                "h_enabled": ("hue_enabled", True), "h_center": ("hue_center", .5),
                "h_width": ("hue_width", 1.0), "h_soft": ("hue_soft", .1),
                "s_enabled": ("sat_enabled", True), "s_low": ("sat_low", 0.0),
                "s_high": ("sat_high", 1.0), "s_s_low": ("sat_soft_low", 0.0),
                "s_s_high": ("sat_soft_high", 0.0), "l_enabled": ("lum_enabled", True),
                "l_low": ("lum_low", 0.0), "l_high": ("lum_high", 1.0),
                "l_s_low": ("lum_soft_low", 0.0), "l_s_high": ("lum_soft_high", 0.0),
                "invert": ("invert", False),
            }
            for uniform, (field, default) in fields.items():
                prog["u_" + uniform].value = qp.get(field, default)
            self._draw(prog)
        if mask is None:
            return matte
        combined = self._texture(size, 1)
        with temporary_framebuffer(self.ctx, [combined]):
            prog = self._program("mask_combine")
            matte.use(0)
            mask.use(1)
            prog["u_matte_a"].value, prog["u_matte_b"].value = 0, 1
            prog["u_mask_uv_rect"].value = (0.0, 0.0, 1.0, 1.0)
            self._draw(prog)
        return combined

    def _aux(self, prog: Any, layer: Mapping[str, Any]) -> None:
        hsl_default = np.ones((6, 256), dtype="f4")
        hsl_default[1] = 0.0
        hsl = layer.get("hsl_curve_lut")
        if hsl is None and layer.get("hsl_curve_data"):
            from src.features.color.core.layer import build_hsl_curve_lut_from_data
            hsl = build_hsl_curve_lut_from_data(dict(layer["hsl_curve_data"]))
        for uniform, key, size, components, default in (
            ("u_curves_tex", "curves_lut", (256, 1), 3, np.linspace(0,1,256)[:,None].repeat(3,1)),
            ("u_hsl_curve_tex", "hsl_curve_lut", (6,256), 1, hsl_default),
        ):
            if uniform not in prog:
                continue
            value = hsl if key == "hsl_curve_lut" else layer.get(key)
            array = np.asarray(value if value is not None else default, dtype="f4")
            if key == "hsl_curve_lut":
                array = array.T.copy()
            tex = self._texture(size, components, array.tobytes())
            unit = 2 if uniform == "u_curves_tex" else 3
            tex.use(unit)
            prog[uniform].value = unit
        if "u_lut_tex" in prog and layer.get("active_lut_path"):
            from src.shared.color.lut_loader import load_lut, resample_to_unit_domain
            lut = resample_to_unit_domain(load_lut(layer["active_lut_path"]))
            tex = self.ctx.texture3d((lut.size,)*3, 3, np.ascontiguousarray(lut.table,dtype="f4").tobytes(),dtype="f4")
            self._resources.append(tex)
            import moderngl
            tex.filter = (moderngl.LINEAR, moderngl.LINEAR)
            tex.repeat_x = tex.repeat_y = tex.repeat_z = False
            tex.use(4)
            prog["u_lut_tex"].value = 4
        if "u_warper_tex" in prog and layer.get("warper_params") is not None:
            array = np.ascontiguousarray(layer["warper_params"], dtype="f4")
            if array.ndim != 3 or array.shape[-1] != 2:
                raise ValueError("Invalid Color Warper grid")
            tex = self._texture((array.shape[1], array.shape[0]), 2, array.tobytes())
            tex.use(5)
            prog["u_warper_tex"].value = 5

    def render(self, source: Any, graph: Any, frame: int, *,
               output_key: str = "main", integer_source: bool = False) -> Any:
        with preserve_framebuffer(self.ctx):
            return self._render(source, graph, frame, output_key=output_key, integer_source=integer_source)

    def _render(self, source: Any, graph: Any, frame: int, *,
                output_key: str, integer_source: bool) -> Any:
        self._release_frame()
        reason = graph_texture_fallback_reason(graph, frame)
        if reason is not None:
            raise RuntimeError(reason)
        if integer_source:
            normalized = self._texture(source.size, 4)
            with temporary_framebuffer(self.ctx, [normalized]):
                self.ctx.viewport = (0, 0, *source.size)
                prog = self._program("normalize_u16")
                source.use(0)
                prog["u_input_tex"].value = 0
                self._draw(prog)
            source = normalized
        self._request = str(uuid.uuid4())
        # Frame belongs to parameter evaluation and executor cache identity,
        # not the structural plan: playback must reuse the same compiled plan.
        snapshot = freeze_graph(graph, session_id="timeline-gl", source_frame=0)
        plan = compile_graph(snapshot, size=source.size, cache=self.plan_cache)
        result = self.executor.execute_plan(
            plan, source, request_id=self._request, source_size=source.size,
            layer_param_provider=graph_node_layer_provider(snapshot.graph, frame=frame),
            render_quad=self._draw, bind_aux=self._aux, matte_provider=self._matte,
            dtype="f4", cache_context=(frame,))
        if not result.published:
            raise RuntimeError(result.diagnostic_message or "Color graph GL execution failed")
        # Callers may retain two transition sources or several track outputs.
        # Keep a separate owned copy, never expose an executor scratch lease.
        target = self._outputs.get(output_key)
        if target is not None and target[0].size != source.size:
            target[1].release()
            target[0].release()
            target = None
        if target is None:
            if output_key not in self._outputs and len(self._outputs) >= 64:
                raise RuntimeError("Too many simultaneous Color graph outputs")
            tex = self.ctx.texture(source.size, 4, dtype="f4")
            import moderngl
            tex.filter = (moderngl.LINEAR, moderngl.LINEAR)
            tex.repeat_x = tex.repeat_y = False
            target = (tex, self.ctx.framebuffer([tex]))
            self._outputs[output_key] = target
        with temporary_framebuffer(self.ctx, [result.output_texture]) as read_fbo:
            self.ctx.copy_framebuffer(target[1], read_fbo)
        if len(self.plan_cache) > 16:
            self.plan_cache.clear()
        return target[0]

    def _release_frame(self) -> None:
        if self._request is not None:
            self.executor.release_revision(self._request)
            self._request = None
        for resource in reversed(self._resources):
            resource.release()
        self._resources.clear()
        for vao in self._vaos.values():
            vao.release()
        self._vaos.clear()

    def release(self) -> None:
        with preserve_framebuffer(self.ctx):
            self._release()

    def _release(self) -> None:
        self._release_frame()
        self.executor.release_pool()
        for tex, fbo in self._outputs.values():
            fbo.release()
            tex.release()
        self._outputs.clear()
        for vao in self._vaos.values():
            vao.release()
        for program in self._programs.values():
            program.release()
        self.pipeline.release()
        self._vbo.release()
