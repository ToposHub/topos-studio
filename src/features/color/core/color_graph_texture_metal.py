"""ColorGraphMetalTextureExecutor —— 调色图的 Metal 纹理原生执行器（阶段 4.2/4.3）。

与 ``color_graph_metal.ColorGraphMetalExecutor``（NumPy RGBA16F staging
桥，计划定位为**算法与测试参考**）共享同一 :class:`ColorGraphRenderPlan`
与同一套调色数学（``METAL_POINTWISE_GRADING_MSL`` + ``shader_nodes_msl``
的 msl_verified 片段），但 pass 间的中间产物全程留在 Metal 纹理上：

- **纹理租约与池**：scratch ``MTLTexture``（RGBA16Float 读写 / R16Float
  matte）按 (格式, 尺寸) 池化，相邻同尺寸 pass 复用；按 plan 的
  ``release_after`` liveness 归还（分叉输入在所有消费者结束后归还），
  多输入节点各持独立租约；失败/取消统一退栈；发布结果持有最终纹理直到
  :meth:`release_revision`。
- **能力 fail-closed**：``METAL_TEXTURE_CAPABILITY_CATALOG`` 预检 +
  ``texture_layer_domain_errors`` 逐 Corrector 参数域检查——不支持的效果
  显式拒绝（不跳节点、不把失败节点输出替换为原图）。
- **资源版本缓存**：LUT3D 纹理按 (解析路径, st_dev, st_ino, size,
  mtime_ns, ctime_ns) 键控（文件热更/换 LUT 自然失效）；曲线 768-float
  buffer、HSL 曲线纹理、Warper 网格纹理、窗口 mask 纹理按内容哈希缓存。
- **参数绑定**：沿用 ``composition_uniforms.composition_uniform_values``
  （与 GL 同一提取器）→ ``graph_grading_params_for_layer``（基础域）+
  ``make_tex_extra_params``（纹理域扩展位）。
- **4.3 key 通道**：qualifier（keyer 的 MSL 翻译）与窗口（CPU 光栅化 →
  R16Float 上传）在本节点组合为 ``key_out`` 产物；key_mixer /
  alpha_output / outside / parallel key 槽从这里解析真实 matte；引用无
  产物显式拒绝（与 GL/CPU 执行器同语义）。关键帧由调用方经
  ``graph_node_layer_provider(graph, frame)`` 在**同一帧时刻**求值（执行
  器逐请求无状态——切片/seek/并行导出无时域历史可重置）。

线程边界：无 Qt/无 GL；与 ``MetalComputeContext`` 的既有约定一致——
单线程（渲染 worker）独占使用；全部 pass 编码进同一 command buffer 后
``finish()`` 一次（与 NumPy 桥执行器/同步 GL 相同的单同步点语义）。
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

import numpy as np

from src.features.color.core.color_graph_compiler import (
    ColorGraphRenderPlan,
    RenderPass,
)
from src.features.color.core.color_graph_metal import (
    MetalGraphCancelled,
    graph_grading_params_for_layer,
)
from src.features.color.core.metal_texture_kernels import (
    METAL_TEX_GRAPH_OPS_MSL,
    make_tex_extra_params,
    make_tex_qualifier_params,
)
from src.shared.gpu.metal.context import MetalComputeContext
from src.shared.gpu.metal.kernels_color import curves_lut_to_flat_buffer

from src.features.color.shaders.node_ops_gl import (
    KEY_MODE_INDEX,
    LAYER_MODE_INDEX,
)

#: 纹理执行器能力目录（4.2/4.3 接线后 key 通道族从 unsupported → exact）
METAL_TEXTURE_CAPABILITY_CATALOG: Mapping[str, str] = {
    "source": "exact",
    "output": "exact",
    "corrector": "fallback",        # 扩展域见 texture_layer_domain_errors
    "shared_corrector": "fallback",
    "parallel_mixer": "exact",
    "layer_mixer": "exact",
    "key_mixer": "exact",           # 4.3：key 通道已接线
    "outside": "exact",             # 4.3：key 补集 + grading
    "splitter": "exact",
    "combiner": "exact",
    "alpha_output": "exact",        # 4.3：key → RGBA passthrough
    "compound": "unsupported",      # 由编译器展开消除；未展开=数据异常
}


class MetalTextureGraphFailure(Exception):
    """纹理执行失败（能力/参数域/编译），带稳定 code。"""

    def __init__(self, code: str, message: str,
                 pass_index: int = -1) -> None:
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.pass_index = pass_index


@dataclass
class MetalTextureGraphResult:
    """一次纹理执行的结果（published 仅在成功终态为 True）。"""

    request_id: str
    published: bool
    output_texture: Any = None            # RGBA16Float（lease，随 release_revision 归池）
    output_size: tuple[int, int] = (0, 0)
    executed_passes: int = 0
    released_textures: int = 0
    diagnostic_code: str | None = None
    diagnostic_message: str | None = None
    device: str = ""


def texture_layer_domain_errors(layer: Mapping[str, Any]) -> list[str]:
    """纹理执行器的 Corrector 参数域检查（fail-closed）。

    与 ``production_layer_domain_errors`` 同一判据风格，但允许纹理域已
    接线的效果：HSL 曲线 / LUT3D / mixer / warper（legacy HSV 空间）/
    soft_clip / curves_soft_clip / 非 Normal blend / qualifier（无后处理）
    / 窗口 mask_shapes。仍拒绝：插件、fx/blur（邻域）、mid_detail /
    sharpness（邻域）、qualifier 后处理（clean/blur/denoise）、warper 多
    空间变体（space > 0）、未预构建的 curves_data。
    """
    from src.features.color.core.color_graph_cpu import (
        _CPU_DEAD_GRADING_KEYS,
        CPU_GRADING_BASIC_KEYS,
        _fx_params_neutral,
    )
    errors: list[str] = []
    grading = layer.get("grading_params")
    grading = grading if isinstance(grading, Mapping) else {}
    unknown = sorted(set(grading) - CPU_GRADING_BASIC_KEYS
                     - _CPU_DEAD_GRADING_KEYS)
    if unknown:
        errors.append(f"grading 参数未支持: {unknown}")
    if float(grading.get("mid_detail") or 0.0) != 0.0 or float(
            grading.get("sharpness") or 0.0) != 0.0:
        errors.append("mid_detail/sharpness 为邻域采样（未接线）")
    if layer.get("plugins"):
        errors.append("插件无 Metal 纹理实现（显式 unsupported）")
    if layer.get("_plugin_bindings"):
        errors.append("执行器级插件绑定仅 GL 路径支持")
    fx = layer.get("effects_params")
    if isinstance(fx, Mapping) and fx and not _fx_params_neutral(fx):
        errors.append("fx/blur 组非中性（邻域效果未接线）")
    if layer.get("curves_data") and layer.get("curves_lut") is None:
        errors.append("curves_data 未预构建 curves_lut")
    qualifier = layer.get("qualifier_params")
    if isinstance(qualifier, Mapping) and qualifier.get("enabled"):
        from src.features.color.core.qualifier_cpu import (
            qualifier_postprocess_active,
        )
        if qualifier_postprocess_active(qualifier):
            errors.append("qualifier 后处理（clean/blur/denoise）未接线")
    warper = layer.get("warper_params")
    if warper is not None and int(
            layer.get("warper_space") or 0) not in (0,):
        errors.append("warper 多空间变体（space>0）未接线")
    return errors


class _SplitTextures:
    """Splitter 输出载体：R/G/B 三个灰度平面纹理（combiner 消费）。"""

    def __init__(self, r: Any, g: Any, b: Any) -> None:
        self.r, self.g, self.b = r, g, b

    def plane(self, port: str) -> Any:
        return getattr(self, port if port in ("r", "g", "b") else "r")


@dataclass
class _TexLease:
    """scratch 纹理租约（请求私有；失败/取消统一退栈）。"""

    texture: Any
    node_id: str
    release_after: int


class _TexturePool:
    """按 (像素格式, 宽, 高) 池化的 scratch MTLTexture 租借器。"""

    _MAX_PER_KEY = 8

    def __init__(self, ctx: MetalComputeContext) -> None:
        self._ctx = ctx
        self._free: dict[tuple, list] = {}
        self._retained_sizes: frozenset[tuple[int, int]] | None = None
        self.allocated = 0

    def acquire(self, pixel_format: Any, width: int, height: int) -> Any:
        key = (pixel_format, int(width), int(height))
        slot = self._free.get(key)
        if slot:
            return slot.pop()
        self.allocated += 1
        return self._ctx.create_texture_2d(
            pixel_format, key[1], key[2], shader_write=True)

    def release(self, texture: Any) -> int:
        if texture is None:
            return 0
        key = (int(texture.pixelFormat()), int(texture.width()),
               int(texture.height()))
        if self._retained_sizes is not None and key[1:] not in self._retained_sizes:
            return 1
        slot = self._free.setdefault(key, [])
        if len(slot) < self._MAX_PER_KEY:
            slot.append(texture)
        # 池满：Python 引用离场即释放 MTLTexture（无 deferred GC 需求）
        return 1

    def retain_sizes(self, sizes: frozenset[tuple[int, int]]) -> None:
        if self._retained_sizes != sizes:
            self._retained_sizes = sizes
            self._free = {key: values for key, values in self._free.items()
                          if key[1:] in sizes}

    def release_all(self) -> int:
        released = sum(len(v) for v in self._free.values())
        self._free.clear()
        return released


class ColorGraphMetalTextureExecutor:
    """按 render plan 在 Metal 纹理上执行（无 Qt / 无 GL / 无 NumPy 暂存）。"""

    def __init__(self, ctx: MetalComputeContext) -> None:
        import Metal  # noqa: F401 —— 像素格式常量（macOS only）

        self._metal = Metal
        self.ctx = ctx
        self._pool = _TexturePool(ctx)
        self._kernels: dict[str, Any] = {}
        # ── 资源版本缓存（内容/文件身份键控） ──
        self._curves_buffers: dict[str, Any] = {}
        self._zero_curves: Any = None
        self._white_mattes: dict[tuple[int, int], Any] = {}
        self._hsl_textures: dict[str, Any] = {}
        self._hsl_default: Any = None
        self._warper_textures: dict[str, Any] = {}
        self._warper_zero: Any = None
        self._lut_textures: dict[tuple, Any] = {}
        self._lut_identity: Any = None
        self._window_textures: dict[tuple, Any] = {}
        self._published: dict[str, MetalTextureGraphResult] = {}

    # ── 像素格式助手 ──

    @property
    def _rgba16f(self) -> Any:
        return self._metal.MTLPixelFormatRGBA16Float

    @property
    def _r16f(self) -> Any:
        return self._metal.MTLPixelFormatR16Float

    @property
    def rgba16f_format(self) -> Any:
        """工作位深像素格式常量（宿主上传源纹理用；MTLPixelFormat 枚举）。"""
        return self._rgba16f

    # ── 主入口 ──

    def execute_plan(
        self,
        plan: ColorGraphRenderPlan,
        source_texture: Any,
        *,
        request_id: str | None = None,
        source_size: tuple[int, int] = (0, 0),
        cancel_check: Callable[[str], bool] | None = None,
        layer_param_provider: Callable[
            [str], Mapping[str, Any] | None] | None = None,
    ) -> MetalTextureGraphResult:
        """执行 plan；``source_texture`` 为同设备 RGBA16Float 输入纹理。

        ``source_texture`` 归调用方所有（source pass 拷入租约纹理，租约
        纹理与本请求中间体一样按 liveness 归还池）。成功时返回持有最终
        纹理租约的结果，消费完成后调用 :meth:`release_revision`。
        """
        request_id = request_id or str(uuid.uuid4())
        try:
            width = int(source_texture.width())
            height = int(source_texture.height())
        except (AttributeError, TypeError) as exc:
            raise MetalTextureGraphFailure(
                "bad_source_texture", f"source 纹理不可读尺寸: {exc}") from exc
        if int(source_texture.pixelFormat()) != int(self._rgba16f):
            return MetalTextureGraphResult(
                request_id=request_id, published=False,
                diagnostic_code="bad_source_format",
                diagnostic_message="source 必须是 RGBA16Float 纹理")
        if source_size not in (None, (0, 0), (width, height)):
            return MetalTextureGraphResult(
                request_id=request_id, published=False,
                diagnostic_code="size_mismatch",
                diagnostic_message=f"source_size {source_size} != 纹理 "
                                   f"{(width, height)}")

        report = plan.validate_capabilities(METAL_TEXTURE_CAPABILITY_CATALOG)
        if report.unsupported:
            label, reason = report.unsupported[0]
            return MetalTextureGraphResult(
                request_id=request_id, published=False,
                diagnostic_code="capability_unsupported",
                diagnostic_message=f"{label}: {reason}（fail-closed，"
                                   "不产出少效果结果）",
                device=self._device_name())

        cancel = cancel_check or (lambda _rid: False)
        if cancel(request_id):
            raise MetalGraphCancelled(request_id, -1)

        pool = self._pool
        leases: list[_TexLease] = []
        node_outputs: dict[str, Any] = {}
        key_outputs: dict[tuple[str, str], Any] = {}
        # 请求内 key 产物（R16F matte）——请求末（finish 后）归池；请求内
        # 由 key_outputs 强引用保活
        key_product_textures: list[Any] = []

        def _release_all() -> int:
            released = 0
            for lease in leases:
                texture = lease.texture
                if isinstance(texture, _SplitTextures):
                    released += (pool.release(texture.r)
                                 + pool.release(texture.g)
                                 + pool.release(texture.b))
                else:
                    released += pool.release(texture)
            leases.clear()
            node_outputs.clear()
            key_outputs.clear()
            for texture in key_product_textures:
                released += pool.release(texture)
            key_product_textures.clear()
            return released

        try:
            executed = 0
            for render_pass in plan.passes:
                if cancel(request_id):
                    raise MetalGraphCancelled(
                        request_id, render_pass.index)
                out = self._execute_pass(
                    render_pass, source_texture, node_outputs, key_outputs,
                    layer_param_provider, width, height, leases,
                    key_product_textures)
                leases.append(_TexLease(out, render_pass.node_id,
                                         render_pass.release_after))
                node_outputs[render_pass.node_id] = out
                executed += 1
                final_id = plan.passes[-1].node_id
                due = [lease for lease in leases
                       if lease.release_after == render_pass.index
                       and lease.node_id != final_id]
                for lease in due:
                    leases.remove(lease)
                    if isinstance(lease.texture, _SplitTextures):
                        for plane in (lease.texture.r, lease.texture.g,
                                      lease.texture.b):
                            pool.release(plane)
                    else:
                        pool.release(lease.texture)

            # 单一同步点：全部 pass 编码后提交等待（此后纹理可安全复用/
            # 读回——与 NumPy 桥执行器/同步 GL 相同语义）
            self.ctx.finish()

            final = plan.passes[-1]
            final_texture = node_outputs[final.node_id]
            if isinstance(final_texture, _SplitTextures):
                raise MetalTextureGraphFailure(
                    "plan_invalid", "最终 output 不能是 splitter 载体")
            # 资源卫生：GPU 完成后归还残余 scratch lease（发布纹理除外）
            # 与全部 key 产物 matte——后续请求可复用（池按尺寸匹配）
            for lease in [l for l in leases
                          if l.node_id != final.node_id]:
                if isinstance(lease.texture, _SplitTextures):
                    for plane in (lease.texture.r, lease.texture.g,
                                  lease.texture.b):
                        pool.release(plane)
                else:
                    pool.release(lease.texture)
                leases.remove(lease)
            for texture in key_product_textures:
                pool.release(texture)
            key_product_textures.clear()
            key_outputs.clear()
            result = MetalTextureGraphResult(
                request_id=request_id, published=True,
                output_texture=final_texture,
                output_size=(width, height),
                executed_passes=executed,
                released_textures=len(plan.passes) - 1,
                device=self._device_name())
            self._published[request_id] = result
            return result
        except MetalGraphCancelled:
            self.ctx.finish()   # 冲 flush 已编码 scratch（无人读）
            released = _release_all()
            return MetalTextureGraphResult(
                request_id=request_id, published=False,
                released_textures=released,
                diagnostic_code="cancelled",
                diagnostic_message="执行取消：全部 scratch lease 已释放，"
                                   "未发布半帧",
                device=self._device_name())
        except MetalTextureGraphFailure as exc:
            self.ctx.finish()
            released = _release_all()
            return MetalTextureGraphResult(
                request_id=request_id, published=False,
                released_textures=released,
                diagnostic_code=exc.code,
                diagnostic_message=str(exc),
                device=self._device_name())
        except Exception as exc:  # noqa: BLE001 —— Metal 分配/编码失败等
            self.ctx.finish()
            released = _release_all()
            return MetalTextureGraphResult(
                request_id=request_id, published=False,
                released_textures=released,
                diagnostic_code="execution_error",
                diagnostic_message=str(exc),
                device=self._device_name())

    # ── 单 pass 分派 ──

    def _execute_pass(
        self,
        render_pass: RenderPass,
        source_texture: Any,
        node_outputs: Mapping[str, Any],
        key_outputs: dict[tuple[str, str], Any],
        layer_param_provider,
        width: int,
        height: int,
        leases: list[_TexLease],
        key_product_textures: list | None = None,
    ) -> Any:
        pool = self._pool
        key_products = key_product_textures if key_product_textures \
            is not None else []
        if render_pass.op == "source":
            out = pool.acquire(self._rgba16f, width, height)
            self._dispatch("tg_copy", textures={0: source_texture, 1: out},
                           buffers={}, size=(width, height))
            return out

        if render_pass.op in ("corrector", "shared_corrector"):
            layer = (layer_param_provider(render_pass.node_id)
                     if layer_param_provider else None) or dict(
                        render_pass.params)
            if not isinstance(layer, Mapping):
                raise MetalTextureGraphFailure(
                    "param_shape_unsupported",
                    f"pass {render_pass.index}: Metal 纹理路径 Corrector "
                    "参数需 dict 形态（Layer DTO 请先投影）",
                    pass_index=render_pass.index)
            input_tex = self._input_for(render_pass, "image", node_outputs,
                                        source_texture)
            if layer.get("visible") is False:
                out = pool.acquire(self._rgba16f, width, height)
                self._dispatch("tg_copy",
                               textures={0: input_tex, 1: out}, buffers={},
                               size=(width, height))
                return out
            # N05：节点 Color Space/Gamma 变换 Metal 未接线——显式拒绝
            color_domain = (render_pass.params.get("color")
                            if isinstance(render_pass.params, Mapping)
                            else None)
            if isinstance(color_domain, Mapping) and (
                    str(color_domain.get("color_space", "Use Timeline"))
                    != "Use Timeline"
                    or str(color_domain.get("gamma", "Use Timeline"))
                    != "Use Timeline"):
                raise MetalTextureGraphFailure(
                    "capability_unsupported",
                    f"pass {render_pass.index}: 节点 Color Space/Gamma "
                    "Metal 未接线（fail-closed，不错色产出）",
                    pass_index=render_pass.index)
            errors = texture_layer_domain_errors(layer)
            if errors:
                raise MetalTextureGraphFailure(
                    "param_domain_unsupported",
                    f"pass {render_pass.index}: {'; '.join(errors)}"
                    "（fail-closed，不产出少效果结果）",
                    pass_index=render_pass.index)
            # N02/4.3：本地 matte（qualifier → 窗口 → 外部 key）与
            # key_out 产物登记（与 GL/CPU 执行器同一组合顺序）
            matte = self._node_matte(layer, input_tex, render_pass,
                                     key_outputs, width, height,
                                     key_products)
            return self._corrector_pass(render_pass, layer, input_tex,
                                        matte, width, height)

        if render_pass.op == "parallel_mixer":
            base = self._input_for(render_pass, "base", node_outputs,
                                   source_texture)
            key_refs = {ref.slot_id: ref for port, ref in render_pass.inputs
                        if port == "key" and ref is not None}
            current = base
            folded = False
            for port, ref in render_pass.inputs:
                if port != "branch" or ref is None:
                    continue
                branch = node_outputs.get(ref.node_id)
                key_ref = key_refs.get(ref.slot_id) if ref.slot_id else None
                if key_ref is not None:
                    key_tex = self._resolve_key_output(
                        key_ref, render_pass, key_outputs)
                else:
                    key_tex = self._white_matte(width, height)
                out = pool.acquire(self._rgba16f, width, height)
                self._dispatch(
                    "tg_parallel_mix",
                    textures={0: current, 1: branch, 2: key_tex, 3: out},
                    buffers={}, size=(width, height))
                if folded:
                    pool.release(current)
                current = out
                folded = True
            # A mixer with no connected branches leaves `current` owned by
            # its input lease. Give the mixer output its own texture.
            return current if folded else self._copy(current, width, height)

        if render_pass.op == "layer_mixer":
            slots = [self._input_for_slot(render_pass, node_outputs,
                                          source_texture, ref)
                     for _port, ref in sorted(
                         render_pass.inputs,
                         key=lambda pr: (pr[1].slot_id if pr[1] else ""))
                     if ref is not None]
            if not slots:
                return self._copy(source_texture, width, height)
            from src.features.color.core.color_graph_param_source import (
                mixer_composite_params,
            )
            blend_mode, opacity = mixer_composite_params(render_pass.params)
            mode_index = LAYER_MODE_INDEX.get(blend_mode, 0)
            opacity_buf = self._scalar_buffer(np.float32(opacity))
            mode_buf = self._scalar_buffer(np.int32(mode_index))
            current = slots[0]
            folded = False
            for top in slots[1:]:
                out = pool.acquire(self._rgba16f, width, height)
                self._dispatch(
                    "tg_layer_mix",
                    textures={0: current, 1: top, 2: out},
                    buffers={0: opacity_buf, 1: mode_buf},
                    size=(width, height))
                if folded:
                    pool.release(current)
                current = out
                folded = True
            return current if folded else self._copy(current, width, height)

        if render_pass.op == "key_mixer":
            refs = [ref for _port, ref in sorted(
                        render_pass.inputs,
                        key=lambda pr: (pr[1].slot_id if pr[1] else ""))
                    if ref is not None]
            if not refs:
                raise MetalTextureGraphFailure(
                    "key_input_missing",
                    f"pass {render_pass.index}: KeyMixer 无 key 输入"
                    "（fail-closed）", pass_index=render_pass.index)
            keys = [self._resolve_key_output(ref, render_pass, key_outputs)
                    for ref in refs]
            mode_index = KEY_MODE_INDEX.get(
                str(render_pass.params.get("mode", "add")), 0)
            mode_buf = self._scalar_buffer(np.int32(mode_index))
            current = keys[0]
            folded = False
            for other in keys[1:]:
                out = pool.acquire(self._r16f, width, height)
                self._dispatch(
                    "tg_key_mix", textures={0: current, 1: other, 2: out},
                    buffers={0: mode_buf}, size=(width, height))
                if folded:
                    pool.release(current)
                current = out
                folded = True
            mixed = current
            key_outputs[(render_pass.node_id, "key")] = mixed
            key_products.append(mixed)
            # 最终图像载体 = key 的 RGBA passthrough（k,k,k,1——与 GL
            # 执行器 key_passthrough/CPU key_mix 同形；R16F matte 另存
            # 于 key_outputs 供下游 key 消费者引用）
            out = pool.acquire(self._rgba16f, width, height)
            self._dispatch("tg_key_passthrough",
                           textures={0: mixed, 1: out}, buffers={},
                           size=(width, height))
            return out

        if render_pass.op == "outside":
            layer = (layer_param_provider(render_pass.node_id)
                     if layer_param_provider else None) or dict(
                        render_pass.params)
            image_in = self._input_for(render_pass, "image", node_outputs,
                                       source_texture)
            key_in = self._key_input_for(render_pass, "key_in", key_outputs)
            if key_in is None:
                raise MetalTextureGraphFailure(
                    "key_input_missing",
                    f"pass {render_pass.index}: Outside 无 key 输入"
                    "（fail-closed）", pass_index=render_pass.index)
            complement = pool.acquire(self._r16f, width, height)
            self._dispatch("tg_key_complement",
                           textures={0: key_in, 1: complement}, buffers={},
                           size=(width, height))
            key_outputs[(render_pass.node_id, "key_out")] = complement
            key_products.append(complement)
            errors = texture_layer_domain_errors(layer)
            if errors:
                raise MetalTextureGraphFailure(
                    "param_domain_unsupported",
                    f"pass {render_pass.index}: {'; '.join(errors)}"
                    "（fail-closed）", pass_index=render_pass.index)
            return self._corrector_pass(render_pass, layer, image_in,
                                        complement, width, height)

        if render_pass.op == "splitter":
            base = self._input_for(render_pass, "image", node_outputs,
                                   source_texture)
            planes = []
            for channel in range(3):
                out = pool.acquire(self._rgba16f, width, height)
                channel_buf = self._scalar_buffer(np.int32(channel))
                self._dispatch("tg_splitter",
                               textures={0: base, 1: out},
                               buffers={0: channel_buf},
                               size=(width, height))
                planes.append(out)
            return _SplitTextures(*planes)

        if render_pass.op == "combiner":
            r = self._input_for(render_pass, "r", node_outputs, None)
            g = self._input_for(render_pass, "g", node_outputs, None)
            b = self._input_for(render_pass, "b", node_outputs, None)
            if r is None or g is None or b is None:
                return self._copy(source_texture, width, height)
            out = pool.acquire(self._rgba16f, width, height)
            self._dispatch("tg_combiner",
                           textures={0: r, 1: g, 2: b, 3: out}, buffers={},
                           size=(width, height))
            return out

        if render_pass.op == "alpha_output":
            key = self._key_input_for(render_pass, "key", key_outputs)
            if key is None:
                raise MetalTextureGraphFailure(
                    "key_input_missing",
                    f"pass {render_pass.index}: Alpha Output 无 key 输入"
                    "（fail-closed）", pass_index=render_pass.index)
            # 最终输出 = key 的 RGBA passthrough（R 通道即 matte，与 GL
            # 执行器 key_passthrough 同形态；消费方按 R 通道取 alpha）
            out = pool.acquire(self._rgba16f, width, height)
            self._dispatch("tg_key_passthrough",
                           textures={0: key, 1: out}, buffers={},
                           size=(width, height))
            return out

        if render_pass.op == "output":
            input_tex = source_texture
            for _port, ref in render_pass.inputs:
                if ref is not None:
                    input_tex = node_outputs.get(ref.node_id, source_texture)
            if isinstance(input_tex, _SplitTextures):
                input_tex = source_texture
            return self._copy(input_tex, width, height)

        raise MetalTextureGraphFailure(
            "capability_unsupported",
            f"pass {render_pass.index} 算子 {render_pass.op} 无 Metal 纹理实现",
            pass_index=render_pass.index)

    # ── Corrector 单 pass ──

    def _corrector_pass(self, render_pass: RenderPass, layer: Mapping,
                        input_tex: Any, matte: Any | None,
                        width: int, height: int) -> Any:
        """tg_corrector：grading → curves → hsl → sc → csc → mixer →
        warper → lut3d → blend/matte epilogue（GL composition main 同序）。"""
        pool = self._pool
        from src.features.color.core.composition_uniforms import (
            composition_uniform_values,
        )
        values = composition_uniform_values(
            dict(layer), width=width, height=height)
        hsl_lut = layer.get("hsl_curve_lut")
        if hsl_lut is None:
            hsl_data = layer.get("hsl_curve_data")
            if isinstance(hsl_data, Mapping) and hsl_data:
                from src.features.color.core.layer import (
                    build_hsl_curve_lut_from_data,
                )
                try:
                    hsl_lut = build_hsl_curve_lut_from_data(dict(hsl_data))
                except (TypeError, ValueError):
                    hsl_lut = None
        mixer = layer.get("mixer_params")
        has_mixer = isinstance(mixer, Mapping) and bool(mixer)
        sc = layer.get("soft_clip_params")
        has_soft_clip = bool(isinstance(sc, Mapping) and sc
                             and sc.get("enabled"))
        csc = layer.get("curves_soft_clip")
        has_csc = isinstance(csc, Mapping) and bool(csc)
        lut_path = layer.get("active_lut_path")
        grading = graph_grading_params_for_layer(
            layer, width, height, has_mask=matte is not None)
        extra = make_tex_extra_params(
            values,
            has_hsl=hsl_lut is not None,
            has_lut3d=bool(lut_path),
            has_mixer=has_mixer,
            has_warper=layer.get("warper_params") is not None,
            has_soft_clip=has_soft_clip,
            has_curves_soft_clip=has_csc,
        )
        out = pool.acquire(self._rgba16f, width, height)
        matte_tex = (matte if matte is not None
                     else self._white_matte(width, height))
        self._dispatch(
            "tg_corrector",
            textures={
                0: input_tex, 1: matte_tex, 2: out,
                3: self._hsl_texture(hsl_lut),
                4: (self._warper_texture(layer.get("warper_params"))
                    if layer.get("warper_params") is not None
                    else self._warper_zero_texture()),
                5: (self._lut_texture(str(lut_path))
                    if lut_path else self._lut_identity_texture()),
            },
            buffers={
                0: self._curves_buffer(layer.get("curves_lut")),
                1: self._param_buffer(grading),
                2: self._param_buffer(extra),
            },
            size=(width, height))
        return out

    # ── matte 生成（qualifier / 窗口 / 外部 key） ──

    def _node_matte(self, layer: Mapping, input_tex: Any,
                    render_pass: RenderPass,
                    key_outputs: dict[tuple[str, str], Any],
                    width: int, height: int,
                    key_products: list) -> Any | None:
        """corrector 的本地 matte（与 GL/CPU 执行器同一组合顺序）。

        qualifier（tg_qualifier）→ 窗口（CPU 光栅化 → R16Float）→ 外部
        key（invert_key_in 先取反，再 intersect）。任一存在即登记本节点
        ``key_out`` 产物并返回 R16Float matte；全无返回 None（blend 全效
        果语义）。
        """
        pool = self._pool
        matte: Any | None = None
        # 本节点自有的池化中间体（最终 matte 之外的都在组合后归还——
        # 同一 command buffer 内编码序保证请求内复用安全）
        owned: list[Any] = []
        qualifier = layer.get("qualifier_params") or {}
        if qualifier.get("enabled"):
            params = make_tex_qualifier_params(qualifier)
            q_tex = pool.acquire(self._r16f, width, height)
            self._dispatch("tg_qualifier",
                           textures={0: input_tex, 1: q_tex},
                           buffers={0: self._param_buffer(params)},
                           size=(width, height))
            matte = q_tex
            owned.append(q_tex)
        if layer.get("mask_shapes"):
            window = self._window_texture(layer["mask_shapes"], width, height)
            if matte is None:
                matte = self._copy_mask(window, width, height)
                owned.append(matte)
            else:
                combined = self._combine_mask(matte, window, width, height)
                owned.append(combined)
                matte = combined
        external_key = self._key_input_for(render_pass, "key_in", key_outputs)
        if external_key is not None:
            key_tex = external_key
            settings = (render_pass.params.get("key_settings")
                        if isinstance(render_pass.params, Mapping) else None)
            if isinstance(settings, Mapping) and settings.get("invert_key_in"):
                inverted = pool.acquire(self._r16f, width, height)
                self._dispatch("tg_key_complement",
                               textures={0: key_tex, 1: inverted},
                               buffers={}, size=(width, height))
                key_tex = inverted
                owned.append(inverted)
            if matte is None:
                matte = self._copy_mask(key_tex, width, height)
                owned.append(matte)
            else:
                combined = self._combine_mask(matte, key_tex, width, height)
                owned.append(combined)
                matte = combined
        if matte is not None:
            key_outputs[(render_pass.node_id, "key_out")] = matte
            key_products.append(matte)
            # 被组合替代的中间体归还（最终 matte 除外）
            for texture in owned:
                if texture is not matte:
                    pool.release(texture)
        return matte

    def _copy_mask(self, mask: Any, width: int, height: int) -> Any:
        out = self._pool.acquire(self._r16f, width, height)
        self._dispatch("tg_copy", textures={0: mask, 1: out}, buffers={},
                       size=(width, height))
        return out

    def _combine_mask(self, a: Any, b: Any, width: int, height: int) -> Any:
        """两个 R16Float matte 的 intersect（key_mix mode=2）。"""
        out = self._pool.acquire(self._r16f, width, height)
        mode_buf = self._scalar_buffer(np.int32(2))
        self._dispatch("tg_key_mix", textures={0: a, 1: b, 2: out},
                       buffers={0: mode_buf}, size=(width, height))
        return out

    # ── key 产物解析 ──

    def _resolve_key_output(self, ref, render_pass: RenderPass,
                            key_outputs: Mapping[tuple[str, str], Any]) -> Any:
        tex = key_outputs.get((ref.node_id, ref.port or "key_out"))
        if tex is None:
            raise MetalTextureGraphFailure(
                "key_output_missing",
                f"pass {render_pass.index}: {ref.node_id}.{ref.port} 无 key "
                "产物（上游节点未产生 qualifier/窗口/外部 key matte——"
                "fail-closed）", pass_index=render_pass.index)
        return tex

    def _key_input_for(self, render_pass: RenderPass, port: str,
                       key_outputs: Mapping[tuple[str, str], Any]) -> Any:
        for input_port, ref in render_pass.inputs:
            if input_port == port and ref is not None:
                return self._resolve_key_output(ref, render_pass, key_outputs)
        return None

    # ── 工具 ──

    def _input_for(self, render_pass: RenderPass, port: str,
                   node_outputs: Mapping[str, Any], default: Any) -> Any:
        for input_port, ref in render_pass.inputs:
            if input_port == port and ref is not None:
                value = node_outputs.get(ref.node_id, default)
                if isinstance(value, _SplitTextures):
                    return value.plane(ref.port)
                return value
        return default

    def _input_for_slot(self, render_pass: RenderPass,
                        node_outputs: Mapping[str, Any], default: Any,
                        ref: Any) -> Any:
        value = node_outputs.get(ref.node_id, default)
        if isinstance(value, _SplitTextures):
            return value.plane(ref.port)
        return value

    def _copy(self, texture: Any, width: int, height: int) -> Any:
        out = self._pool.acquire(self._rgba16f, width, height)
        self._dispatch("tg_copy", textures={0: texture, 1: out}, buffers={},
                       size=(width, height))
        return out

    def _dispatch(self, kernel_name: str, *, textures: dict, buffers: dict,
                  size: tuple[int, int]) -> None:
        kernel = self._kernels.get(kernel_name)
        if kernel is None:
            kernel = self.ctx.compile_kernel(kernel_name,
                                             METAL_TEX_GRAPH_OPS_MSL)
            self._kernels[kernel_name] = kernel
        self.ctx.dispatch(kernel, buffers=buffers, textures=textures,
                          threads=(size[0], size[1], 1))

    def _param_buffer(self, params: np.ndarray) -> Any:
        return self.ctx.create_buffer_from_numpy(params)

    def _scalar_buffer(self, value: np.generic) -> Any:
        return self.ctx.create_buffer_from_numpy(
            np.array(value, dtype=value.dtype))

    def _white_matte(self, width: int, height: int) -> Any:
        key = (width, height)
        if key not in self._white_mattes:
            data = np.ones((height, width), dtype=np.float16)
            self._white_mattes[key] = self.ctx.create_texture_from_numpy(
                data, self._r16f, width, height)
        return self._white_mattes[key]

    def _curves_buffer(self, lut) -> Any:
        if lut is None:
            if self._zero_curves is None:
                self._zero_curves = self.ctx.create_buffer_from_numpy(
                    np.zeros(768, dtype=np.float32))
            return self._zero_curves
        flat = curves_lut_to_flat_buffer(lut)
        key = hashlib.sha256(flat.tobytes()).hexdigest()
        if key not in self._curves_buffers:
            if len(self._curves_buffers) >= 16:
                self._curves_buffers.pop(next(iter(self._curves_buffers)))
            self._curves_buffers[key] = \
                self.ctx.create_buffer_from_numpy(flat)
        return self._curves_buffers[key]

    def _hsl_texture(self, hsl_lut) -> Any:
        """(6, 256) HSL 曲线 → R32Float 6×256 纹理（宽=曲线类型）。"""
        if hsl_lut is None:
            if self._hsl_default is None:
                default = np.ones((6, 256), dtype=np.float32)
                default[1] = 0.0
                self._hsl_default = self._upload_r32(
                    np.ascontiguousarray(default.T), 6, 256)
            return self._hsl_default
        arr = np.ascontiguousarray(
            np.asarray(hsl_lut, dtype=np.float32).reshape(6, 256).T)
        key = hashlib.sha256(arr.tobytes()).hexdigest()
        if key not in self._hsl_textures:
            if len(self._hsl_textures) >= 16:
                self._hsl_textures.pop(next(iter(self._hsl_textures)))
            self._hsl_textures[key] = self._upload_r32(arr, 6, 256)
        return self._hsl_textures[key]

    def _upload_r32(self, arr: np.ndarray, width: int, height: int) -> Any:
        tex = self.ctx.create_texture_2d(self._metal.MTLPixelFormatR32Float,
                                         width, height, shader_write=False)
        tex.replaceRegion_mipmapLevel_withBytes_bytesPerRow_(
            self._metal.MTLRegionMake2D(0, 0, width, height), 0,
            arr.tobytes(), arr.strides[0])
        return tex

    def _warper_zero_texture(self) -> Any:
        if self._warper_zero is None:
            zeros = np.zeros((2, 2, 2), dtype=np.float32)
            self._warper_zero = self._warper_texture(zeros)
        return self._warper_zero

    def _warper_texture(self, warper_params) -> Any:
        """(h, w, 2) (delta_hue, delta_sat) 网格 → RG32Float 纹理。"""
        array = np.ascontiguousarray(warper_params, dtype=np.float32)
        if array.ndim != 3 or array.shape[-1] != 2:
            raise MetalTextureGraphFailure(
                "param_shape_unsupported",
                f"Warper 网格形状非法: {array.shape}（期望 (h, w, 2)）")
        height, width = array.shape[0], array.shape[1]
        key = hashlib.sha256(array.tobytes()).hexdigest()
        if key not in self._warper_textures:
            tex = self.ctx.create_texture_2d(
                self._metal.MTLPixelFormatRG32Float, width, height,
                shader_write=False)
            tex.replaceRegion_mipmapLevel_withBytes_bytesPerRow_(
                self._metal.MTLRegionMake2D(0, 0, width, height), 0,
                array.tobytes(), array.strides[0])
            if len(self._warper_textures) >= 16:
                self._warper_textures.pop(next(iter(self._warper_textures)))
            self._warper_textures[key] = tex
        return self._warper_textures[key]

    def _lut_identity_texture(self) -> Any:
        if self._lut_identity is None:
            axis = np.array([0.0, 1.0], dtype=np.float32)
            table = np.stack(np.meshgrid(axis, axis, axis, indexing="ij"),
                             axis=-1)
            self._lut_identity = self.ctx.create_rgb_lut_texture(table)
        return self._lut_identity

    def _lut_texture(self, reference: str) -> Any:
        """LUT3D 纹理按资源版本缓存（解析路径 + 文件 stat 身份键控）。"""
        from src.shared.color.lut_loader import (
            load_lut,
            resample_to_unit_domain,
            resolve_lut_path,
        )
        path = Path(resolve_lut_path(reference)).absolute()
        info = path.stat()
        key = (str(path), info.st_dev, info.st_ino, info.st_size,
               info.st_mtime_ns, info.st_ctime_ns)
        cached = self._lut_textures.get(key)
        if cached is not None:
            return cached
        lut = resample_to_unit_domain(load_lut(str(path)))
        texture = self.ctx.create_rgb_lut_texture(lut.table)
        # 有界缓存（FIFO 淘汰；纹理由 Python 引用离场释放）
        if len(self._lut_textures) >= 8:
            self._lut_textures.pop(next(iter(self._lut_textures)))
        self._lut_textures[key] = texture
        return texture

    def _window_texture(self, shapes: Any, width: int, height: int) -> Any:
        """窗口 mask：CPU 光栅化（WindowRasterizer，与 GL/CPU 同实现）→
        R16Float 纹理，按 (形状内容哈希, 尺寸) 缓存。"""
        from src.features.color.core.layer import MaskGroup, MaskShape
        from src.features.color.core.window_rasterizer import WindowRasterizer
        blob = hashlib.sha256(repr((tuple(map(str, shapes)), width, height)
                                   ).encode("utf-8")).hexdigest()
        key = (blob, width, height)
        cached = self._window_textures.get(key)
        if cached is not None:
            return cached
        items = []
        for shape in shapes or ():
            data = dict(shape) if isinstance(shape, Mapping) else None
            if data is None:
                continue
            cls = MaskGroup if data.get("type") == "group" else MaskShape
            items.append(cls.from_dict(data))
        if items:
            try:
                array = WindowRasterizer().render_items(items, width, height)
            except ValueError as exc:
                raise MetalTextureGraphFailure(
                    "window_raster_failed", str(exc)) from exc
        else:
            array = np.zeros((height, width), dtype=np.float32)
        data16 = np.ascontiguousarray(array, dtype=np.float16)
        texture = self.ctx.create_texture_from_numpy(
            data16, self._r16f, width, height)
        if len(self._window_textures) >= 16:
            self._window_textures.pop(next(iter(self._window_textures)))
        self._window_textures[key] = texture
        return texture

    def _device_name(self) -> str:
        try:
            return self.ctx.device_name
        except Exception:  # noqa: BLE001 —— 诊断字段不抛
            return ""

    # ── 发布生命周期 ──

    def release_revision(self, request_id: str) -> int:
        """释放已发布结果的持有（消费完成后调用；纹理归池）。"""
        result = self._published.pop(request_id, None)
        if result is None:
            return 0
        released = self._pool.release(result.output_texture)
        result.output_texture = None
        return released

    def read_output_rgba16f(self, result: MetalTextureGraphResult,
                            ) -> np.ndarray:
        """读回已发布输出为 (H, W, 4) float32（验证/诊断路径专用）。"""
        if not result.published or result.output_texture is None:
            raise MetalTextureGraphFailure(
                "not_published", "结果未发布或已释放，无法读回")
        width, height = result.output_size
        raw = self.ctx.read_texture(result.output_texture, width * 4,
                                    height, dtype=np.float16,
                                    region_width=width)
        return raw.reshape(height, width, 4).astype(np.float32)

    def read_key_r16f(self, texture: Any, width: int, height: int,
                      ) -> np.ndarray:
        """读回 R16Float matte 为 (H, W) float32（验证/诊断路径专用）。"""
        raw = self.ctx.read_texture(texture, width, height,
                                    dtype=np.float16, region_width=width)
        return raw.reshape(height, width).astype(np.float32)

    def pool_allocated(self) -> int:
        """诊断：本执行器累计分配的 scratch 纹理数（池增长审计）。"""
        return self._pool.allocated

    def retain_preview_sizes(self, sizes: frozenset[tuple[int, int]]) -> None:
        """Keep current viewer and scope sizes across live viewport resizes."""
        self._pool.retain_sizes(sizes)
        self._white_mattes = {size: tex for size, tex in self._white_mattes.items()
                              if size in sizes}

    def release_pool(self) -> int:
        """释放池与全部资源缓存（宿主清理时调用）。"""
        released = self._pool.release_all()
        self._curves_buffers.clear()
        self._zero_curves = None
        self._white_mattes.clear()
        self._hsl_textures.clear()
        self._hsl_default = None
        self._warper_textures.clear()
        self._warper_zero = None
        self._lut_textures.clear()
        self._lut_identity = None
        self._window_textures.clear()
        return released
