"""ColorGraphExecutor —— render plan 的 GL 执行器（N07，B09）。

只渲染线程调用（GL 上下文持有中）。核心语义（计划 §5.2/§5.5）：

- **逐 pass 取消/代数检查**：每个 pass 前回调 ``cancel_check``；命中即
  抛 :class:`GraphExecutionCancelled`，统一退栈释放本请求全部 scratch
  lease（缓存/front 资源归 owner，不随 scratch 归还）。
- **成功终态才发布**：全部 pass 完成后把最终输出复制到发布纹理并返回
  :class:`GraphExecutionResult(published=True)`；中途取消/失败**不发布
  半帧**，front 输出 lease 保持有效（取消前画面仍可显示）。
- **liveness 释放**：按 plan 的 ``release_after`` 归还中间纹理——分叉
  输入在所有消费者结束后才归还（§5.4）。
- **能力 fail-closed**：plan 含 pending/unsupported 算子（mixer 族待
  B10）时拒绝执行并返回诊断，绝不跳过节点产出"成功但少效果"。
- **串行等价**：串行链（Source→C…→Output）经本执行器的输出与旧
  layers ping-pong 路径逐位一致（B09.5 对照，golden 测试锁定）。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

import numpy as np

from src.features.color.core.color_graph_compiler import (
    ColorGraphRenderPlan,
    RenderPass,
)
from src.features.color.core.corrector_render_service import (
    CorrectorDiagnostic,
    prepare_corrector,
    render_corrector_gl,
)
from src.features.color.core.shader_compiler import ShaderPipeline


class GraphExecutionCancelled(Exception):
    """请求在执行中被取消（不发布半帧）。"""

    def __init__(self, request_id: str, pass_index: int) -> None:
        super().__init__(
            f"request {request_id} cancelled before pass {pass_index}")
        self.request_id = request_id
        self.pass_index = pass_index


class GraphExecutionFailure(Exception):
    """执行失败（编译/分配/能力），带稳定 code 与诊断。"""

    def __init__(self, code: str, message: str,
                 pass_index: int = -1) -> None:
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.pass_index = pass_index


@dataclass
class GraphExecutionResult:
    """一次执行的结果（published 仅在成功终态为 True）。"""

    request_id: str
    published: bool
    output_texture: Any = None
    output_size: tuple[int, int] = (0, 0)
    graph_revision: int = -1
    executed_passes: int = 0
    released_textures: int = 0
    diagnostic_code: str | None = None
    diagnostic_message: str | None = None


@dataclass
class _Lease:
    """scratch 纹理租约（请求私有；失败/取消统一退栈）。"""

    texture: Any
    node_id: str
    release_after: int


class ColorGraphExecutor:
    """按 plan 执行（每实例一个 GL 上下文线程；无 Qt）。"""

    def __init__(self, ctx: Any, pipeline: ShaderPipeline | None = None,
                 *, backend_catalog: Mapping[str, str] | None = None) -> None:
        self.ctx = ctx
        self.pipeline = pipeline or ShaderPipeline(ctx)
        self.backend_catalog = backend_catalog
        # revision → 已发布结果（scopes/Composite 消费者经 release_revision 释放）
        self._published: dict[str, GraphExecutionResult] = {}

    # ── 主入口 ──

    def execute_plan(
        self,
        plan: ColorGraphRenderPlan,
        source_texture: Any,
        *,
        request_id: str | None = None,
        source_size: tuple[int, int] = (0, 0),
        cancel_check: Callable[[str], bool] | None = None,
        layer_param_provider: Callable[[str], Mapping[str, Any] | None] | None = None,
        render_quad: Callable[[Any], None] | None = None,
    ) -> GraphExecutionResult:
        """执行 plan；source pass 消费 ``source_texture``（租借不持有）。

        ``layer_param_provider``：node_id → Corrector 的 Layer DTO（迁移图
        里参数在 legacy_payload；执行器按节点取参数交给 N06）。
        """
        request_id = request_id or str(uuid.uuid4())
        width, height = (int(source_size[0]) or source_texture.size[0],
                         int(source_size[1]) or source_texture.size[1])

        # 能力预检：pending/unsupported 一律 fail-closed（不跳节点）
        report = plan.validate_capabilities(self.backend_catalog)
        if report.unsupported:
            label, reason = report.unsupported[0]
            return GraphExecutionResult(
                request_id=request_id, published=False,
                graph_revision=-1,
                diagnostic_code="capability_unsupported",
                diagnostic_message=f"{label}: {reason}（fail-closed，"
                                   "不产出少效果结果）")

        cancel = cancel_check or (lambda _rid: False)
        if cancel(request_id):
            raise GraphExecutionCancelled(request_id, -1)

        leases: list[_Lease] = []
        node_outputs: dict[str, Any] = {}

        def _release_lease(lease: _Lease) -> int:
            lease.texture.release()
            return 1

        def _release_all() -> int:
            released = 0
            for lease in leases:
                try:
                    released += _release_lease(lease)
                except Exception:  # noqa: BLE001 —— 释放路径不二次抛
                    pass
            leases.clear()
            node_outputs.clear()
            return released

        try:
            executed = 0
            for render_pass in plan.passes:
                if cancel(request_id):
                    raise GraphExecutionCancelled(
                        request_id, render_pass.index)

                out_tex = self._execute_pass(
                    render_pass, source_texture, node_outputs,
                    layer_param_provider, render_quad, width, height)
                leases.append(_Lease(out_tex, render_pass.node_id,
                                     render_pass.release_after))
                node_outputs[render_pass.node_id] = out_tex
                executed += 1

                # liveness：release_after == 当前 index 的中间纹理归还
                due = [l for l in leases
                       if l.release_after == render_pass.index
                       and l.node_id != plan.passes[-1].node_id]
                for lease in due:
                    leases.remove(lease)
                    _release_lease(lease)

            final = plan.passes[-1]
            output_texture = node_outputs[final.node_id]
            result = GraphExecutionResult(
                request_id=request_id,
                published=True,
                output_texture=output_texture,
                output_size=(width, height),
                graph_revision=0,
                executed_passes=executed,
                released_textures=len(plan.passes) - 1)
            self._published[request_id] = result
            return result
        except GraphExecutionCancelled:
            released = _release_all()
            return GraphExecutionResult(
                request_id=request_id, published=False,
                released_textures=released,
                diagnostic_code="cancelled",
                diagnostic_message="执行取消：全部 scratch 已释放，"
                                   "front 输出未受影响")
        except GraphExecutionFailure as exc:
            released = _release_all()
            return GraphExecutionResult(
                request_id=request_id, published=False,
                released_textures=released,
                diagnostic_code=exc.code,
                diagnostic_message=str(exc))
        except Exception as exc:  # noqa: BLE001 —— GL 分配失败等
            released = _release_all()
            return GraphExecutionResult(
                request_id=request_id, published=False,
                released_textures=released,
                diagnostic_code="execution_error",
                diagnostic_message=str(exc))

    # ── 单 pass 分派 ──

    def _execute_pass(self, render_pass: RenderPass,
                      source_texture: Any,
                      node_outputs: Mapping[str, Any],
                      layer_param_provider, render_quad,
                      width: int, height: int) -> Any:
        if render_pass.op == "source":
            # source = 输入纹理直通（新租约纹理承载，源纹理归调用方）
            return self._copy_texture(source_texture, width, height)

        if render_pass.op in ("corrector", "shared_corrector"):
            layer = (layer_param_provider(render_pass.node_id)
                     if layer_param_provider else None) or dict(render_pass.params)
            # bypass（enabled=False）：输入直通——不执行 grade（§6.3
            # set_bypass 旁路映射；hidden 层在 legacy 循环是整层跳过，
            # 串行链上等价于直通）
            if isinstance(layer, Mapping) and layer.get("visible") is False:
                input_tex = source_texture
                for port, ref in render_pass.inputs:
                    if port == "image" and ref is not None:
                        input_tex = node_outputs.get(ref.node_id,
                                                     source_texture)
                return self._copy_texture(input_tex, width, height)
            # ``_plugin_bindings``：执行器级显式 PluginBinding 注入（真实
            # 插件经注册表由 prepare_corrector 从 payload.plugins 解析；
            # stub/测试绑定走此通道，避免伪造注册表插件）
            override = layer.get("_plugin_bindings") if isinstance(
                layer, Mapping) else None
            prepared = prepare_corrector(
                self.pipeline, layer, layer_id=render_pass.node_id,
                bindings_override=override or None)
            if isinstance(prepared, CorrectorDiagnostic):
                raise GraphExecutionFailure(
                    "shader_compile_failed",
                    f"pass {render_pass.index}: {prepared.message}",
                    pass_index=render_pass.index)
            # 输入：image 端口引用上游输出
            input_tex = source_texture
            for port, ref in render_pass.inputs:
                if port == "image" and ref is not None:
                    input_tex = node_outputs.get(ref.node_id, source_texture)
            out_tex = self.ctx.texture((width, height), components=4,
                                       dtype="f2")
            fbo = self.ctx.framebuffer(color_attachments=[out_tex])
            fbo.use()
            matte = self._white_matte(width, height)
            quad_fn = render_quad or (
                lambda prog: self._default_quad(prog).render(_TRIANGLE_STRIP()))
            aux = self._layer_aux_textures(layer, render_pass.node_id)
            render_corrector_gl(
                self.ctx, prepared, layer=layer,
                grade_input_tex=input_tex, matte_tex=matte,
                viewport=(width, height),
                update_uniforms=self._make_uniform_uploader(
                    layer, width, height, prepared.bindings),
                bind_aux=aux,
                render_quad=quad_fn)
            matte.release()
            fbo.release()
            return out_tex

        if render_pass.op == "output":
            input_tex = source_texture
            for port, ref in render_pass.inputs:
                if ref is not None:
                    input_tex = node_outputs.get(ref.node_id, source_texture)
            return self._copy_texture(input_tex, width, height)

        raise GraphExecutionFailure(
            "capability_unsupported",
            f"pass {render_pass.index} 算子 {render_pass.op} 无执行实现",
            pass_index=render_pass.index)

    # ── 工具 ──

    def _copy_texture(self, tex: Any, width: int, height: int) -> Any:
        out = self.ctx.texture((width, height), components=4, dtype="f2")
        fbo = self.ctx.framebuffer(color_attachments=[out])
        fbo.use()
        prog = self._passthrough_program()
        tex.use(0)
        prog["u_texture"].value = 0
        self._default_quad(prog).render(_TRIANGLE_STRIP())  # noqa: direct VAO
        fbo.release()
        return out

    def _layer_aux_textures(self, layer, node_id: str):
        """per-layer aux 纹理绑定（curves unit 2 / hsl unit 3；与生产
        ``_bind_aux_textures_single_layer`` 同构——identity 默认兜底）。"""
        base_unit = 2

        def bind(prog: Any) -> None:
            unit = base_unit
            if "u_curves_tex" in prog:
                tex = self._curves_texture(layer, node_id)
                tex.use(unit)
                prog["u_curves_tex"].value = unit
                unit += 1
            if "u_hsl_curve_tex" in prog:
                tex = self._default_hsl_texture()
                tex.use(unit)
                prog["u_hsl_curve_tex"].value = unit
        return bind

    def _curves_texture(self, layer, node_id: str) -> Any:
        lut = layer.get("curves_lut") if isinstance(layer, Mapping) else None
        cache = getattr(self, "_curves_tex_cache", None)
        if cache is None:
            cache = {}
            self._curves_tex_cache = cache
        if lut is None:
            if "identity" not in cache:
                ramp = np.linspace(0.0, 1.0, 256, dtype=np.float32)
                data = np.stack([ramp] * 3, axis=-1).reshape(256, 1, 3)
                tex = self.ctx.texture((256, 1), components=3, dtype="f4",
                                       data=np.ascontiguousarray(data).tobytes())
                tex.filter = (self.ctx.LINEAR, self.ctx.LINEAR)
                cache["identity"] = tex
            return cache["identity"]
        arr = np.ascontiguousarray(
            np.asarray(lut, dtype=np.float32).reshape(256, 1, 3)).tobytes()
        key = hash(arr)
        if key not in cache:
            tex = self.ctx.texture((256, 1), components=3, dtype="f4",
                                   data=arr)
            tex.filter = (self.ctx.LINEAR, self.ctx.LINEAR)
            cache[key] = tex
        return cache[key]

    def _default_hsl_texture(self) -> Any:
        if getattr(self, "_hsl_tex", None) is None:
            ramp = np.linspace(0.0, 1.0, 256, dtype=np.float32)
            data = np.tile(ramp, (6, 1)).astype(np.float32)
            tex = self.ctx.texture((6, 256), components=1, dtype="f4",
                                   data=np.ascontiguousarray(data).tobytes())
            tex.filter = (self.ctx.LINEAR, self.ctx.LINEAR)
            self._hsl_tex = tex
        return self._hsl_tex

    def _white_matte(self, width: int, height: int) -> Any:
        data = np.ones((height, width, 4), dtype=np.float16)
        return self.ctx.texture((width, height), components=4,
                                dtype="f2", data=data)

    def _passthrough_program(self) -> Any:
        import moderngl
        if getattr(self, "_pass_prog", None) is None:
            self._pass_prog = self.ctx.program(
                vertex_shader="""
                #version 330
                in vec2 in_vert; in vec2 in_texcoord;
                out vec2 v_uv;
                void main(){ gl_Position = vec4(in_vert,0.,1.); v_uv = in_texcoord; }
                """,
                fragment_shader="""
                #version 330
                uniform sampler2D u_texture;
                in vec2 v_uv; out vec4 f_color;
                void main(){ f_color = texture(u_texture, v_uv); }
                """)
        return self._pass_prog

    def _make_uniform_uploader(self, layer, width, height, bindings):
        from src.features.color.core.composition_uniforms import (
            composition_uniform_values,
            upload_composition_uniforms,
        )

        def _upload(prog: Any) -> None:
            values = composition_uniform_values(
                layer, bindings=tuple(bindings), width=width, height=height)
            upload_composition_uniforms(prog, values)
        return _upload

    def _default_quad(self, prog):
        import numpy as np
        if getattr(self, "_quad_vao_prog", None) is not prog:
            self._quad_vao = self.ctx.vertex_array(
                prog,
                [(self.ctx.buffer(np.array([
                    -1.0, -1.0, 0.0, 0.0, 1.0, -1.0, 1.0, 0.0,
                    -1.0, 1.0, 0.0, 1.0, 1.0, 1.0, 1.0, 1.0],
                    dtype="f4").tobytes()),
                  "2f 2f", "in_vert", "in_texcoord")])
            self._quad_vao_prog = prog
        return self._quad_vao

    # ── 取消 / 发布生命周期（宿主调度接线点） ──

    def cancel_request(self, request_id: str) -> None:
        """登记取消（执行中的 plan 在下一 pass 边界生效）。"""
        self._cancel_flags = getattr(self, "_cancel_flags", set())
        self._cancel_flags.add(request_id)

    def is_cancelled(self, request_id: str) -> bool:
        return request_id in getattr(self, "_cancel_flags", set())

    def clear_cancel(self, request_id: str) -> None:
        getattr(self, "_cancel_flags", set()).discard(request_id)

    def release_revision(self, request_id: str) -> int:
        """释放已发布结果的持有（scopes/Composite 消费完成后调用）。"""
        result = self._published.pop(request_id, None)
        if result is None:
            return 0
        try:
            result.output_texture.release()
            return 1
        except Exception:  # noqa: BLE001
            return 0


def _TRIANGLE_STRIP():
    import moderngl
    return moderngl.TRIANGLE_STRIP
