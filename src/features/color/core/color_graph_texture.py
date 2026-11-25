"""Render a frozen Color graph on a caller-owned GL render context."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Iterator, Mapping
import uuid

import numpy as np

from src.features.color.core.cache_manager import CacheTier, LRUCache
from src.features.color.core.color_graph_compiler import compile_graph
from src.features.color.core.color_graph_executor import ColorGraphExecutor
from src.features.color.core.color_node_lut_spec import node_lut_projection
from src.features.color.core.color_graph_param_source import graph_node_layer_provider
from src.features.color.core.color_graph_snapshot import freeze_graph
from src.features.color.core.mist import mist_is_active
from src.features.color.core.shader_compiler import ShaderPipeline
from src.features.color.shaders.shaders import PASSTHROUGH_VERTEX
from src.shared.color.lut_loader import load_lut, resample_to_unit_domain, resolve_lut_path
from src.shared.gpu.framebuffer_scope import preserve_framebuffer, temporary_framebuffer
from src.shared.project.color_graph import ColorGraphData, ColorNodeData


_LUT_CACHE_BYTES = 32 * 1024 * 1024


def serial_lut_only(graph: ColorGraphData) -> tuple[str, float] | None:
    """Recognize a fresh, enabled Corrector whose only operation is a LUT."""
    luts = serial_luts_only(graph, max_luts=1)
    return luts[0] if luts else None


def serial_luts_only(
    graph: ColorGraphData, *, max_luts: int,
) -> tuple[tuple[str, float], ...] | None:
    """Recognize a bounded sequence of LUT-only Correctors in image-edge order."""
    if not 1 <= len(graph.nodes) - 2 <= max_luts:
        return None
    correctors = serial_correctors(graph)
    if correctors is None:
        return None
    luts = []
    for corrector in correctors:
        lut = corrector_lut_only(corrector)
        if lut is None:
            return None
        luts.append(lut)
    return tuple(luts)


def serial_correctors(graph: ColorGraphData) -> tuple[ColorNodeData, ...] | None:
    """Find enabled Correctors in image-edge order for a simple serial graph."""
    nodes = {node.node_id: node for node in graph.nodes}
    source = nodes.get(graph.source_node_id)
    output = nodes.get(graph.output_node_id)
    if (len(nodes) < 3 or len(nodes) != len(graph.nodes) or len(graph.edges) != len(nodes) - 1
            or source is None or source.node_type != "source"
            or output is None or output.node_type != "output"
            or source.params or output.params or graph.alpha_output_node_id is not None
            or graph.shared_definitions or graph.working_color_context):
        return None
    if any(not node.enabled or node.compound_children
           or node.shared_definition_id is not None for node in nodes.values()):
        return None
    if source.legacy_payload or output.legacy_payload:
        return None
    if any(edge.src_port != "image" or edge.dst_port != "image" or edge.slot_id
           for edge in graph.edges):
        return None
    successors = {edge.src_node: edge.dst_node for edge in graph.edges}
    correctors = []
    next_id = successors.get(source.node_id)
    for _ in range(len(nodes) - 2):
        node = nodes.get(next_id)
        if node is None or node.node_type != "corrector":
            return None
        correctors.append(node)
        next_id = successors.get(node.node_id)
    return tuple(correctors) if next_id == output.node_id else None


def corrector_lut_only(node: ColorNodeData) -> tuple[str, float] | None:
    """Recognize a fresh Corrector whose only operation is a LUT."""
    if (node.legacy_payload or node.params.keys() - {"key_source_mode", "resources"}
            or node.params.get("key_source_mode", "node_input") != "node_input"):
        return None
    resources = node.params.get("resources")
    if not isinstance(resources, Mapping) or resources.keys() - {"lut_ref", "lut_strength"}:
        return None
    reference, strength = node_lut_projection(resources)
    return (reference, strength) if reference is not None else None


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
        self._resources: list[Any] = []
        self._vaos: dict[int, Any] = {}
        self._programs: dict[str, Any] = {}
        self._outputs: dict[str, tuple[str, Any]] = {}
        self._normalized_source: Any = None
        # This renderer and its cache are used only on the owning GL thread.
        self._lut_textures: LRUCache[Any] = LRUCache(
            16, CacheTier.GPU_TEXTURE, max_bytes=_LUT_CACHE_BYTES,
            on_evict=lambda texture: texture.release(),
        )
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
                fragment_shader=fragment if fragment is not None else path.read_text(encoding="utf-8"))
        return self._programs[name]

    def _texture(self, size: tuple[int, int], components: int, data: Any = None,
                 *, transient: bool = True) -> Any:
        tex = self.ctx.texture(size, components, data=data, dtype="f4")
        import moderngl
        tex.filter = (moderngl.LINEAR, moderngl.LINEAR)
        tex.repeat_x = tex.repeat_y = False
        if transient:
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

    def _lut_texture(self, reference: str) -> Any:
        import moderngl

        # Resolve before looking up the cache so project switches, missing
        # resources and edits to external LUT files still take effect.
        path = Path(resolve_lut_path(reference)).absolute()
        info = path.stat()
        key = (path, info.st_dev, info.st_ino, info.st_size,
               info.st_mtime_ns, info.st_ctime_ns)
        texture = self._lut_textures.get(key)
        if texture is not None:
            return texture

        lut = resample_to_unit_domain(load_lut(str(path)))
        data = np.ascontiguousarray(lut.table, dtype="f4")
        texture = self.ctx.texture3d((lut.size,) * 3, 3, data, dtype="f4")
        try:
            texture.filter = (moderngl.LINEAR, moderngl.LINEAR)
            texture.repeat_x = texture.repeat_y = texture.repeat_z = False
        except Exception:
            texture.release()
            raise
        if data.nbytes <= _LUT_CACHE_BYTES:
            self._lut_textures.put(key, texture, estimated_bytes=data.nbytes)
        else:
            self._resources.append(texture)
        return texture

    def _aux(self, prog: Any, layer: Mapping[str, Any]) -> None:
        hsl_default = np.ones((6, 256), dtype="f4")
        hsl_default[1] = 0.0
        hsl = layer.get("hsl_curve_lut")
        if hsl is None and layer.get("hsl_curve_data"):
            from src.features.color.core.curves_calibration import curves_calibration_version
            from src.features.color.core.layer import build_hsl_curve_lut_from_data
            hsl = build_hsl_curve_lut_from_data(
                dict(layer["hsl_curve_data"]),
                calibration_version=curves_calibration_version(layer.get("curves_edit")),
            )
        for uniform, key, size, components, default in (
            ("u_curves_tex", "curves_lut", (256, 1), 3, np.linspace(0,1,256)[:,None].repeat(3,1)),
            ("u_hsl_curve_tex", "hsl_curve_lut", (6,256), 1, hsl_default),
        ):
            if uniform not in prog:
                continue
            value = hsl if key == "hsl_curve_lut" else layer.get(key)
            array = np.asarray(value if value is not None else default, dtype="f4")
            if key == "hsl_curve_lut":
                size = array.shape
                array = array.T.copy()
            else:
                size = (array.shape[0], 1)
            tex = self._texture(size, components, array.tobytes())
            unit = 2 if uniform == "u_curves_tex" else 3
            tex.use(unit)
            prog[uniform].value = unit
        if "u_lut_tex" in prog and layer.get("active_lut_path"):
            self._lut_texture(layer["active_lut_path"]).use(4)
            prog["u_lut_tex"].value = 4
        if "u_warper_tex" in prog and layer.get("warper_params") is not None:
            from src.features.color.core.warper_color import (
                WARPER_PREPARED_SPACES, prepare_resolve_hs709_texture,
            )

            array = np.ascontiguousarray(layer["warper_params"], dtype="f4")
            space = int(layer.get("warper_space", 0) or 0)
            components = 3 if space in WARPER_PREPARED_SPACES else 2
            if array.ndim != 3 or array.shape[-1] != components:
                raise ValueError("Invalid Color Warper grid")
            if space in WARPER_PREPARED_SPACES:
                array = prepare_resolve_hs709_texture(array, space)
            tex = self._texture((array.shape[1], array.shape[0]), components, array.tobytes())
            tex.use(5)
            prog["u_warper_tex"].value = 5

    def delivery_lut_texture(self, reference: str) -> Any:
        """Resolve a LUT for a caller's stand-alone delivery pass."""
        self._release_frame()
        return self._lut_texture(reference)

    def render(self, source: Any, graph: Any, frame: int, *,
               output_key: str = "main", integer_source: bool = False,
               rec709_calibration_encoded: bool = False) -> Any:
        with preserve_framebuffer(self.ctx):
            return self._render(
                source, graph, frame, output_key=output_key, integer_source=integer_source,
                rec709_calibration_encoded=rec709_calibration_encoded)

    def _render(self, source: Any, graph: Any, frame: int, *,
                output_key: str, integer_source: bool, rec709_calibration_encoded: bool) -> Any:
        self._release_frame()
        if output_key not in self._outputs and len(self._outputs) >= 64:
            raise RuntimeError("Too many simultaneous Color graph outputs")
        reason = graph_texture_fallback_reason(graph, frame)
        if reason is not None:
            raise RuntimeError(reason)
        if integer_source:
            normalized = self._normalized_source
            if normalized is None or normalized.size != source.size:
                if normalized is not None:
                    normalized.release()
                self._normalized_source = None
                normalized = self._texture(source.size, 4, transient=False)
                self._normalized_source = normalized
            with temporary_framebuffer(self.ctx, [normalized]):
                self.ctx.viewport = (0, 0, *source.size)
                prog = self._program("normalize_u16")
                source.use(0)
                prog["u_input_tex"].value = 0
                self._draw(prog)
            source = normalized
        request = str(uuid.uuid4())
        # Frame belongs to parameter evaluation and executor cache identity,
        # not the structural plan: playback must reuse the same compiled plan.
        snapshot = freeze_graph(graph, session_id="timeline-gl", source_frame=0)
        plan = compile_graph(snapshot, size=source.size, cache=self.plan_cache)
        provider = graph_node_layer_provider(
            snapshot.graph, frame=frame, rec709_calibration_encoded=rec709_calibration_encoded)
        simple = False
        if len(plan.passes) == 3 and plan.passes[1].op == "corrector":
            layer = provider(plan.passes[1].node_id) or {}
            effects = layer.get("effects_params") or {}
            grade = layer.get("grading_params") or {}
            simple = bool(
                layer.get("active_lut_path")
                and not layer.get("plugins")
                and not effects.get("enabled")
                and not effects.get("blur_enabled")
                and not mist_is_active(effects)
                and not grade.get("mid_detail")
                and not grade.get("sharpness")
                and not layer.get("mask_shapes")
                and not (layer.get("qualifier_params") or {}).get("enabled")
                and not plan.passes[1].key_pipeline
            )
        result = self.executor.execute_plan(
            plan, source, request_id=request, source_size=source.size,
            layer_param_provider=provider,
            render_quad=self._draw, bind_aux=self._aux, matte_provider=self._matte,
            dtype="f4", cache_context=(frame,), fold_single_corrector_io=simple)
        if not result.published:
            raise RuntimeError(result.diagnostic_message or "Color graph GL execution failed")
        # Hold the executor's published lease for each output key. An
        # unsuccessful frame leaves the previous output available.
        texture = result.output_texture
        try:
            texture.repeat_x = texture.repeat_y = False
        except Exception:
            self.executor.release_revision(request)
            raise
        previous = self._outputs.get(output_key)
        self._outputs[output_key] = (request, texture)
        if previous is not None:
            old_request, old_texture = previous
            old_texture.repeat_x = old_texture.repeat_y = True
            self.executor.release_revision(old_request)
        if len(self.plan_cache) > 16:
            self.plan_cache.clear()
        return texture

    def _release_frame(self) -> None:
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
        if self._normalized_source is not None:
            self._normalized_source.release()
            self._normalized_source = None
        self._lut_textures.clear()
        for request, _ in self._outputs.values():
            self.executor.release_revision(request)
        self._outputs.clear()
        self.executor.release_pool()
        for vao in self._vaos.values():
            vao.release()
        for program in self._programs.values():
            program.release()
        self.pipeline.release()
        self._vbo.release()
