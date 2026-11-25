"""ColorGraphMetalExecutor —— render plan 的 Metal compute 执行器（N10，B12.3）。

与 GL 执行器（N07）共享同一 :class:`ColorGraphRenderPlan` 语义：

- **能力 fail-closed**：``METAL_CAPABILITY_CATALOG`` 预检，pending/
  unsupported 拒绝执行（不跳节点）；Corrector 仅生产基础点态域
  （grading、曲线、709 Hue–Saturation Warper、Normal/opacity），
  插件/邻域/mixer/旧 Warper/LUT3D/fx 显式拒绝（与 CPU oracle 的
  ``production_layer_domain_errors`` 同一判据）。
- **buffer lease**：scratch MTLBuffer 按尺寸池化；按 plan 的
  ``release_after`` liveness 归还（分叉输入在所有消费者结束后归还），
  失败/取消统一退栈；发布结果持有最终 buffer 直到
  :meth:`release_revision`。
- **逐 pass 取消**：每 pass 前回调 ``cancel_check``；命中则冲刷已编码
  命令、归还全部 lease、不发布半帧。
- **HDR 语义**：RGBA16F 半精度线性 buffer（half4），RGB 路径不 clamp
  （kernel 侧无 clamp，spec 与 GLSL 同序）；key 域按 spec 有界。

输入输出均为 numpy RGBA16F（``(H, W, 4)`` float16）——CPU staging 桥
（macOS GL 4.1 与 Metal 无纹理共享通道，见 ``kernels.py`` 模块文档）。
MSL kernels 定义在 E23（``kernels_color.METAL_GRAPH_OPS_MSL``）。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Callable, Mapping

import numpy as np

from src.features.color.core.base_grade_runtime import (
    BASE_GRADE_SIGNAL_VECTORS, BASE_GRADE_UNIFORMS, metal_graph_base_grade_source,
)
from src.features.color.core.color_graph_compiler import (
    ColorGraphRenderPlan,
    RenderPass,
)
from src.features.color.core.color_graph_cpu import (
    calibrated_warper_offsets,
    production_layer_domain_errors,
)
from src.features.color.core.curves_calibration import is_calibrated_curves_lut
from src.shared.gpu.metal.context import MetalComputeContext
from src.shared.gpu.metal.kernels_color import (
    build_metal_graph_ops_msl,
    build_metal_graph_warper_msl,
    curves_lut_to_flat_buffer,
    make_graph_grading_params,
    make_graph_size_params,
)

from src.features.color.shaders.node_ops_gl import (
    KEY_MODE_INDEX,
    LAYER_MODE_INDEX,
)
from src.shared.project.color_graph import ColorGraphData

#: Metal 后端能力目录（B12.1 矩阵数据源；op → exact/fallback/unsupported）
METAL_CAPABILITY_CATALOG: Mapping[str, str] = {
    "source": "exact",
    "output": "exact",
    "corrector": "fallback",   # 基础点态域 exact；插件/邻域/LUT 显式拒绝
    "shared_corrector": "fallback",
    "parallel_mixer": "exact",
    "layer_mixer": "exact",
    # N02：key 通道（key_out 产物/key_in 消费）Metal 未接线——按
    # 「不支持的显式禁用」置 unsupported（N14 跨后端域扩展时恢复）。
    "key_mixer": "unsupported",
    "outside": "unsupported",
    "splitter": "exact",
    "combiner": "exact",
    "alpha_output": "unsupported",
    "compound": "unsupported",
}


def graph_grading_params_for_layer(
    layer: Mapping[str, Any], width: int, height: int, *, has_mask: bool = False,
    grading_params_override: Mapping[str, Any] | None = None,
) -> np.ndarray:
    """Pack primary-grading uniforms for a layer with a supported parameter domain."""
    from src.features.color.core.composition_uniforms import (
        composition_uniform_values,
    )
    from src.features.color.core.primary_calibration import grade_uses_calibration

    if grade_uses_calibration(layer) and grading_params_override is None:
        raise ValueError("Measured primary grading needs a calibration grid")
    values = composition_uniform_values(
        dict(layer), width=width, height=height,
        grading_params_override=grading_params_override,
    )
    curves = layer.get("curves_lut")
    return make_graph_grading_params(
        exposure=float(values["u_exposure"]),
        temp=float(values["u_temp"]),
        tint=float(values["u_tint"]),
        contrast=float(values["u_contrast"]),
        saturation=float(values["u_saturation"]),
        vibrance=float(values["u_vibrance"]),
        shadow=float(values["u_shadow"]),
        highlights=float(values["u_highlights"]),
        hue_shift=float(values["u_hue_shift"]),
        lift_saturation=float(values["u_lift_saturation"]),
        gamma_saturation=float(values["u_gamma_saturation"]),
        gain_saturation=float(values["u_gain_saturation"]),
        lift=tuple(values["u_lift"]),
        gamma=tuple(values["u_gamma"]),
        gain=tuple(values["u_gain"]),
        pivot=float(values["u_pivot"]),
        offset=tuple(values["u_offset"]),
        log_shadow=tuple(values["u_log_shadow"]),
        log_midtone=tuple(values["u_log_midtone"]),
        log_highlight=tuple(values["u_log_highlight"]),
        log_offset=tuple(values["u_log_offset"]),
        log_low_range=float(values["u_log_low_range"]),
        log_high_range=float(values["u_log_high_range"]),
        lum_mix=float(values["u_lum_mix"]),
        opacity=float(values["u_opacity"]),
        mask_mode=1 if has_mask else 0,
        has_curves=curves is not None,
        calibrated_curves=curves is not None and is_calibrated_curves_lut(np.asarray(curves)),
        base_grade=tuple(component for name, _ in BASE_GRADE_UNIFORMS for component in values.get(name, ())),
        base_grade_encoded=values["u_rec709_calibration_encoded"],
        base_grade_transfer=values.get("u_base_grade_transfer", -1),
        base_grade_signal=tuple(component for name in BASE_GRADE_SIGNAL_VECTORS for component in values.get(name, ())),
    )


@dataclass(frozen=True)
class MetalDeliveryCorrector:
    """A static primary grade and/or LUT in one serial graph node."""

    lut: tuple[str, float] | None
    primary: np.ndarray | None = None

    @property
    def operation(self) -> str:
        if self.primary is None:
            return "lut"
        return "primary_lut" if self.lut is not None else "primary"


def serial_metal_delivery_grade(
    graph: ColorGraphData, width: int, height: int, *, rec709_calibration_encoded: bool = False,
) -> tuple[MetalDeliveryCorrector, ...] | None:
    """Resolve static legacy primary and LUT nodes for the direct pixel path.

    These kernels bind neither the measured primary grid nor curve samples.
    """
    from src.features.color.core.color_graph_param_source import node_layer_payload
    from src.features.color.core.color_graph_texture import corrector_lut_only, serial_correctors
    from src.features.color.core.color_node_lut_spec import node_lut_projection

    correctors = serial_correctors(graph)
    if not correctors:
        return None
    operations = []
    for node in correctors:
        lut = corrector_lut_only(node)
        if lut is not None:
            operations.append(MetalDeliveryCorrector(lut))
            continue
        if (node.legacy_payload or node.params.keys() - {
                "grade", "opacity", "blend_mode", "resources", "key_source_mode"}
                or node.params.get("key_source_mode", "node_input") != "node_input"):
            return None
        resources = node.params.get("resources", {})
        if not isinstance(resources, Mapping) or resources.keys() - {"lut_ref", "lut_strength"}:
            return None
        layer = node_layer_payload(node, include_lut_cache=False)
        if (layer is None or layer.get("curves_data") or layer.get("curves_lut") is not None
                or production_layer_domain_errors(layer, backend="metal")):
            return None
        reference, strength = node_lut_projection(resources)
        if rec709_calibration_encoded:
            layer = {**layer, "_rec709_calibration_encoded": True}
        operations.append(MetalDeliveryCorrector(
            (reference, strength) if reference is not None else None,
            graph_grading_params_for_layer(layer, width, height),
        ))
    return tuple(operations)


class MetalGraphCancelled(Exception):
    """请求在执行中被取消（不发布半帧）。"""

    def __init__(self, request_id: str, pass_index: int) -> None:
        super().__init__(
            f"request {request_id} cancelled before pass {pass_index}")
        self.request_id = request_id
        self.pass_index = pass_index


class MetalGraphFailure(Exception):
    """Metal 执行失败（能力/参数域/编译），带稳定 code。"""

    def __init__(self, code: str, message: str,
                 pass_index: int = -1) -> None:
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.pass_index = pass_index


@dataclass
class MetalGraphExecutionResult:
    """一次 Metal 执行的结果（published 仅在成功终态为 True）。"""

    request_id: str
    published: bool
    output: np.ndarray | None = None          # (H, W, 4) float16 视图
    output_buffer: Any = None                 # 持有的 MTLBuffer（lease）
    output_size: tuple[int, int] = (0, 0)
    executed_passes: int = 0
    released_buffers: int = 0
    diagnostic_code: str | None = None
    diagnostic_message: str | None = None
    device: str = ""


class _SplitBuffers:
    """Splitter 输出载体：R/G/B 三个灰度平面 buffer（combiner 消费）。"""

    def __init__(self, r, g, b) -> None:
        self.r, self.g, self.b = r, g, b

    def release(self, pool: "_BufferPool") -> int:
        released = 0
        for buf in (self.r, self.g, self.b):
            released += pool.release(buf)
        return released


@dataclass
class _Lease:
    """scratch buffer 租约（请求私有；失败/取消统一退栈）。"""

    buffer: Any
    node_id: str
    release_after: int


class _BufferPool:
    """按字节尺寸池化的 scratch MTLBuffer 租借器。"""

    def __init__(self, ctx: MetalComputeContext) -> None:
        self._ctx = ctx
        self._free: dict[int, list] = {}
        self.allocated = 0

    def acquire(self, byte_length: int) -> Any:
        slot = self._free.get(int(byte_length))
        if slot:
            return slot.pop()
        self.allocated += 1
        return self._ctx.create_buffer(int(byte_length))

    def release(self, buffer: Any) -> int:
        if buffer is None:
            return 0
        self._free.setdefault(int(buffer.length()), []).append(buffer)
        return 1


class ColorGraphMetalExecutor:
    """按同一 render plan 在 Metal compute 上执行（无 Qt / 无 GL）。"""

    def __init__(self, ctx: MetalComputeContext) -> None:
        self.ctx = ctx
        self._pool = _BufferPool(ctx)
        self._kernels: dict[str, Any] = {}
        self._white: dict[tuple[int, int], Any] = {}
        self._curves_buffers: dict[int, Any] = {}
        self._zero_curves: Any = None
        # request_id → 已发布结果（消费者经 release_revision 释放）
        self._published: dict[str, MetalGraphExecutionResult] = {}

    # ── 主入口 ──

    def execute_plan(
        self,
        plan: ColorGraphRenderPlan,
        source_rgba16f: np.ndarray,
        *,
        request_id: str | None = None,
        source_size: tuple[int, int] = (0, 0),
        cancel_check: Callable[[str], bool] | None = None,
        layer_param_provider: Callable[
            [str], Mapping[str, Any] | None] | None = None,
    ) -> MetalGraphExecutionResult:
        """执行 plan；``source_rgba16f`` 为 (H, W, 4) float16 输入。

        ``layer_param_provider``：node_id → Corrector 参数（dict 形态；
        生产域判据与 CPU oracle 共用 ``production_layer_domain_errors``）。
        """
        request_id = request_id or str(uuid.uuid4())
        height, width = source_rgba16f.shape[:2]
        if source_rgba16f.dtype != np.float16 or source_rgba16f.ndim != 3 \
                or source_rgba16f.shape[2] != 4:
            return MetalGraphExecutionResult(
                request_id=request_id, published=False,
                diagnostic_code="bad_source_format",
                diagnostic_message="source 必须是 (H, W, 4) float16 "
                                   f"RGBA16F（got {source_rgba16f.shape} "
                                   f"{source_rgba16f.dtype}）")
        if source_size not in (None, (0, 0), (width, height)):
            return MetalGraphExecutionResult(
                request_id=request_id, published=False,
                diagnostic_code="size_mismatch",
                diagnostic_message=f"source_size {source_size} != 数组 "
                                   f"{(width, height)}")

        # 能力预检：fail-closed（不跳节点）
        report = plan.validate_capabilities(METAL_CAPABILITY_CATALOG)
        if report.unsupported:
            label, reason = report.unsupported[0]
            return MetalGraphExecutionResult(
                request_id=request_id, published=False,
                diagnostic_code="capability_unsupported",
                diagnostic_message=f"{label}: {reason}（fail-closed，"
                                   "不产出少效果结果）",
                device=self._device_name())

        cancel = cancel_check or (lambda _rid: False)
        if cancel(request_id):
            raise MetalGraphCancelled(request_id, -1)

        source_buffer = self.ctx.create_buffer_from_numpy(
            np.ascontiguousarray(source_rgba16f))
        leases: list[_Lease] = []
        node_outputs: dict[str, Any] = {}
        pool = self._pool

        def _release_all() -> int:
            released = 0
            for lease in leases:
                try:
                    if isinstance(lease.buffer, _SplitBuffers):
                        released += lease.buffer.release(pool)
                    else:
                        released += pool.release(lease.buffer)
                except Exception:  # noqa: BLE001 —— 释放路径不二次抛
                    pass
            leases.clear()
            node_outputs.clear()
            released += pool.release(source_buffer)
            return released

        try:
            executed = 0
            for render_pass in plan.passes:
                if cancel(request_id):
                    raise MetalGraphCancelled(
                        request_id, render_pass.index)
                out = self._execute_pass(
                    render_pass, source_buffer, node_outputs,
                    layer_param_provider, width, height)
                leases.append(_Lease(out, render_pass.node_id,
                                     render_pass.release_after))
                node_outputs[render_pass.node_id] = out
                executed += 1
                # liveness：release_after == 当前 index 的中间 buffer 归还
                final_id = plan.passes[-1].node_id
                due = [l for l in leases
                       if l.release_after == render_pass.index
                       and l.node_id != final_id]
                for lease in due:
                    leases.remove(lease)
                    if isinstance(lease.buffer, _SplitBuffers):
                        lease.buffer.release(pool)
                    else:
                        pool.release(lease.buffer)

            self.ctx.finish()   # 单一同步点：全部 pass 编码后提交等待
            final = plan.passes[-1]
            final_buffer = node_outputs[final.node_id]
            if isinstance(final_buffer, _SplitBuffers):
                raise MetalGraphFailure(
                    "plan_invalid", "最终 output 不能是 splitter 载体")
            readback = self.ctx.read_buffer(final_buffer)
            output = readback.view(np.float16).reshape(height, width, 4)
            result = MetalGraphExecutionResult(
                request_id=request_id, published=True,
                output=output, output_buffer=final_buffer,
                output_size=(width, height),
                executed_passes=executed,
                released_buffers=len(plan.passes) - 1,
                device=self._device_name())
            self._published[request_id] = result
            # 发布 buffer 不入池；source buffer 已被消费可归还
            pool.release(source_buffer)
            return result
        except MetalGraphCancelled:
            self.ctx.finish()   # 冲 flush 已编码命令（scratch，无人读）
            released = _release_all()
            return MetalGraphExecutionResult(
                request_id=request_id, published=False,
                released_buffers=released,
                diagnostic_code="cancelled",
                diagnostic_message="执行取消：全部 scratch lease 已释放，"
                                   "未发布半帧",
                device=self._device_name())
        except MetalGraphFailure as exc:
            self.ctx.finish()
            released = _release_all()
            return MetalGraphExecutionResult(
                request_id=request_id, published=False,
                released_buffers=released,
                diagnostic_code=exc.code,
                diagnostic_message=str(exc),
                device=self._device_name())
        except Exception as exc:  # noqa: BLE001 —— Metal 分配/编码失败等
            self.ctx.finish()
            released = _release_all()
            return MetalGraphExecutionResult(
                request_id=request_id, published=False,
                released_buffers=released,
                diagnostic_code="execution_error",
                diagnostic_message=str(exc),
                device=self._device_name())

    # ── 单 pass 分派 ──

    def _execute_pass(self, render_pass: RenderPass, source_buffer,
                      node_outputs: Mapping[str, Any], layer_param_provider,
                      width: int, height: int) -> Any:
        pool = self._pool
        size = (width, height)
        if render_pass.op == "source":
            out = pool.acquire(width * height * 8)
            self._dispatch("graph_copy",
                           {0: source_buffer, 1: out,
                            2: self._params_buffer(
                                make_graph_size_params(width, height))},
                           size)
            return out

        if render_pass.op in ("corrector", "shared_corrector"):
            layer = (layer_param_provider(render_pass.node_id)
                     if layer_param_provider else None) or dict(
                        render_pass.params)
            if not isinstance(layer, Mapping):
                raise MetalGraphFailure(
                    "param_shape_unsupported",
                    f"pass {render_pass.index}: Metal 路径 Corrector 参数"
                    "需 dict 形态（Layer DTO 请先投影）",
                    pass_index=render_pass.index)
            input_buf = self._input_for(render_pass, "image", node_outputs,
                                        source_buffer)
            # N05：节点 Color Space/Gamma 变换 Metal 未接线——显式
            # 拒绝（不静默跳过变换=错色；N14 恢复）
            color_domain_m = (render_pass.params.get("color")
                              if isinstance(render_pass.params, Mapping)
                              else None)
            if isinstance(color_domain_m, Mapping) and (
                    str(color_domain_m.get("color_space",
                                           "Use Timeline"))
                    != "Use Timeline"
                    or str(color_domain_m.get("gamma", "Use Timeline"))
                    != "Use Timeline"):
                raise MetalGraphFailure(
                    "capability_unsupported",
                    f"pass {render_pass.index}: 节点 Color Space/Gamma "
                    "Metal 未接线（N05 v1 GL/CPU；N14 恢复）",
                    pass_index=render_pass.index)
            # N02：key_in 边（外部 key 消费）Metal 未接线——显式拒绝，
            # 不用白 matte 顶替（key 通道接线归 N14）
            if any(port == "key_in" and ref is not None
                   for port, ref in render_pass.inputs):
                raise MetalGraphFailure(
                    "capability_unsupported",
                    f"pass {render_pass.index}: Corrector 的 key_in 消费"
                    "Metal 未接线（N14）——fail-closed",
                    pass_index=render_pass.index)
            if layer.get("visible") is False:
                out = pool.acquire(width * height * 8)
                self._dispatch("graph_copy",
                               {0: input_buf, 1: out,
                                2: self._params_buffer(
                                    make_graph_size_params(width, height))},
                               size)
                return out
            graded = self._grading_pass(render_pass, layer, input_buf,
                                        None, width, height)
            return graded

        if render_pass.op == "parallel_mixer":
            base = self._input_for(render_pass, "base", node_outputs,
                                   source_buffer)
            # N02：声明的 key 槽（multi key 边）Metal 未接线——显式拒绝
            # （未声明槽的白 key 是 spec §1 语义，保留）
            if any(port == "key" and ref is not None
                   for port, ref in render_pass.inputs):
                raise MetalGraphFailure(
                    "capability_unsupported",
                    f"pass {render_pass.index}: Parallel Mixer 声明的 "
                    "key 槽 Metal 未接线（N14）——fail-closed",
                    pass_index=render_pass.index)
            white = self._white_buffer(width, height)
            current = base
            folded = False
            for port, ref in render_pass.inputs:
                if port != "branch" or ref is None:
                    continue
                branch = node_outputs.get(ref.node_id)
                out = pool.acquire(width * height * 8)
                self._dispatch("graph_parallel_mix",
                               {0: current, 1: branch, 2: white, 3: out,
                                4: self._params_buffer(
                                    make_graph_size_params(width, height))},
                               size)
                if folded:
                    pool.release(current)   # 上一轮折叠中间量归还
                current = out
                folded = True
            return current

        if render_pass.op == "layer_mixer":
            slots = [node_outputs[ref.node_id]
                     for _port, ref in sorted(
                         render_pass.inputs,
                         key=lambda pr: (pr[1].slot_id if pr[1] else ""))
                     if ref is not None]
            if not slots:
                return self._copy(source_buffer, width, height)
            from src.features.color.core.color_graph_param_source import (
                mixer_composite_params,
            )
            blend_mode, opacity = mixer_composite_params(render_pass.params)
            mode_index = LAYER_MODE_INDEX.get(blend_mode, 0)
            current = slots[0]
            folded = False
            for top in slots[1:]:
                out = pool.acquire(width * height * 8)
                self._dispatch("graph_layer_mix",
                               {0: current, 1: top, 2: out,
                                3: self._params_buffer(
                                    make_graph_size_params(
                                        width, height, a=opacity,
                                        b=mode_index))},
                               size)
                if folded:
                    pool.release(current)
                current = out
                folded = True
            return current

        if render_pass.op == "key_mixer":
            keys = [node_outputs[ref.node_id]
                    for _port, ref in sorted(
                        render_pass.inputs,
                        key=lambda pr: (pr[1].slot_id if pr[1] else ""))
                    if ref is not None]
            if not keys:
                # N02：key_mixer 无 key 输入显式拒绝（不再白 matte）
                raise MetalGraphFailure(
                    "key_input_missing",
                    f"pass {render_pass.index}: KeyMixer 无 key 输入"
                    "（fail-closed）",
                    pass_index=render_pass.index)
            mode_index = KEY_MODE_INDEX.get(
                str(render_pass.params.get("mode", "add")), 0)
            current = keys[0]
            folded = False
            for other in keys[1:]:
                out = pool.acquire(width * height * 8)
                self._dispatch("graph_key_mix",
                               {0: current, 1: other, 2: out,
                                3: self._params_buffer(
                                    make_graph_size_params(
                                        width, height, b=mode_index))},
                               size)
                if folded:
                    pool.release(current)
                current = out
                folded = True
            return current

        if render_pass.op == "outside":
            layer = (layer_param_provider(render_pass.node_id)
                     if layer_param_provider else None) or dict(
                        render_pass.params)
            image_in = self._input_for(render_pass, "image", node_outputs,
                                       source_buffer)
            key_in = self._input_for(render_pass, "key_in", node_outputs,
                                     None)
            # N02：outside 的 key 通道（key_out 产物消费 + 补集）Metal
            # 未接线（N14）；能力目录已 unsupported——此处防御性同语义
            raise MetalGraphFailure(
                "capability_unsupported",
                f"pass {render_pass.index}: Outside 的 key 通道 Metal "
                "未接线（N14）——fail-closed",
                pass_index=render_pass.index)

        if render_pass.op == "splitter":
            base = self._input_for(render_pass, "image", node_outputs,
                                   source_buffer)
            planes = []
            for ch in range(3):
                out = pool.acquire(width * height * 8)
                self._dispatch("graph_splitter",
                               {0: base, 1: out,
                                2: self._params_buffer(
                                    make_graph_size_params(width, height,
                                                           b=ch))},
                               size)
                planes.append(out)
            return _SplitBuffers(*planes)

        if render_pass.op == "combiner":
            r = self._input_for(render_pass, "r", node_outputs, None)
            g = self._input_for(render_pass, "g", node_outputs, None)
            b = self._input_for(render_pass, "b", node_outputs, None)
            if r is None or g is None or b is None:
                return self._copy(source_buffer, width, height)
            out = pool.acquire(width * height * 8)
            self._dispatch("graph_combiner",
                           {0: r, 1: g, 2: b, 3: out,
                            4: self._params_buffer(
                                make_graph_size_params(width, height))},
                           size)
            return out

        if render_pass.op == "alpha_output":
            key = self._input_for(render_pass, "key", node_outputs, None)
            if key is None:
                # N02：无 key 边显式拒绝（不再白 matte）
                raise MetalGraphFailure(
                    "key_input_missing",
                    f"pass {render_pass.index}: Alpha Output 无 key 输入"
                    "（fail-closed）",
                    pass_index=render_pass.index)
            out = pool.acquire(width * height * 8)
            self._dispatch("graph_key_passthrough",
                           {0: key, 1: out,
                            2: self._params_buffer(
                                make_graph_size_params(width, height))},
                           size)
            return out

        if render_pass.op == "output":
            input_buf = source_buffer
            for port, ref in render_pass.inputs:
                if ref is not None:
                    input_buf = node_outputs.get(ref.node_id, source_buffer)
            if isinstance(input_buf, _SplitBuffers):
                input_buf = source_buffer
            return self._copy(input_buf, width, height)

        raise MetalGraphFailure(
            "capability_unsupported",
            f"pass {render_pass.index} 算子 {render_pass.op} 无 Metal 实现",
            pass_index=render_pass.index)

    # ── Corrector 单 pass（grading + curves + epilogue） ──

    def _grading_pass(self, render_pass: RenderPass, layer: Mapping,
                      input_buf, key_buf, width: int, height: int):
        # N14-B：显式 metal 后端口径（HSL/soft_clip/blend CPU 已接线，
        # Metal kernel 参数位未扩——维持逐域 fail-closed）
        errors = production_layer_domain_errors(layer, backend="metal")
        if errors:
            raise MetalGraphFailure(
                "param_domain_unsupported",
                f"pass {render_pass.index}: {'; '.join(errors)}"
                "（fail-closed，不产出少效果结果）",
                pass_index=render_pass.index)
        params = graph_grading_params_for_layer(
            layer, width, height, has_mask=key_buf is not None)
        lut = layer.get("curves_lut")
        curves_buf = self._curves_buffer(lut)
        matte = key_buf if key_buf is not None else self._white_buffer(
            width, height)
        offsets = calibrated_warper_offsets(layer)
        warper_tex = None
        kernel_name = "graph_grading"
        if offsets is not None:
            import Metal

            from src.features.color.core.warper_color import (
                RESOLVE_HS709_RESPONSE_SPACES, WARPER_CHROMA709_SPACE, WARPER_X_GRADE_SPACE,
                prepare_resolve_hs709_texture,
            )

            space = layer["warper_space"]
            sampled = prepare_resolve_hs709_texture(offsets, space)
            rows, columns, _ = sampled.shape
            rgba = np.zeros((rows, columns, 4), dtype=np.float32)
            rgba[..., :3] = sampled
            warper_tex = self.ctx.create_texture_from_numpy(
                rgba, Metal.MTLPixelFormatRGBA32Float, columns, rows)
            if space == WARPER_X_GRADE_SPACE:
                kernel_name = "graph_grading_x_grade"
            elif space == WARPER_CHROMA709_SPACE:
                kernel_name = "graph_grading_chroma709"
            else:
                kernel_name = "graph_grading_709_v2" if space in RESOLVE_HS709_RESPONSE_SPACES else "graph_grading_709"
        out = self._pool.acquire(width * height * 8)
        dispatched = False
        try:
            self._dispatch(
                kernel_name,
                {0: input_buf, 1: matte, 2: out, 3: curves_buf,
                 4: self._params_buffer(params),
                 5: self._params_buffer(make_graph_size_params(width, height))},
                (width, height), textures=None if warper_tex is None else {0: warper_tex},
            )
            dispatched = True
        finally:
            if not dispatched:
                self._pool.release(out)
        return out

    # ── 工具 ──

    def _input_for(self, render_pass: RenderPass, port: str,
                   node_outputs: Mapping[str, Any], default: Any) -> Any:
        for input_port, ref in render_pass.inputs:
            if input_port == port and ref is not None:
                value = node_outputs.get(ref.node_id, default)
                if isinstance(value, _SplitBuffers):
                    return getattr(value,
                                   ref.port if ref.port in ("r", "g", "b")
                                   else "r")
                return value
        return default

    def _copy(self, buf, width: int, height: int):
        out = self._pool.acquire(width * height * 8)
        self._dispatch("graph_copy",
                       {0: buf, 1: out,
                        2: self._params_buffer(
                            make_graph_size_params(width, height))},
                       (width, height))
        return out

    def _dispatch(self, kernel_name: str, buffers: dict,
                  size: tuple[int, int], *, textures: dict[int, Any] | None = None) -> None:
        kernel = self._kernels.get(kernel_name)
        if kernel is None:
            extension = metal_graph_base_grade_source()
            source = build_metal_graph_ops_msl(extension)
            if kernel_name in ("graph_grading_709", "graph_grading_709_v2", "graph_grading_chroma709",
                               "graph_grading_x_grade"):
                from src.features.color.core.shader_nodes_msl import (
                    MSL_HSV_FUNCS, MSL_RESOLVE_HS709, MSL_RESOLVE_HS709_V2,
                    MSL_WARPER_CHROMA709,
                )

                v2 = kernel_name == "graph_grading_709_v2"
                chroma = kernel_name == "graph_grading_chroma709"
                x_grade = kernel_name == "graph_grading_x_grade"
                warper_code = {
                    "graph_grading_709": MSL_RESOLVE_HS709,
                    "graph_grading_709_v2": MSL_RESOLVE_HS709_V2,
                    "graph_grading_chroma709": MSL_WARPER_CHROMA709,
                }
                if x_grade:
                    from src.features.color.core.x_grade_shader import MSL_X_GRADE

                    warper_code[kernel_name] = (MSL_WARPER_CHROMA709 + MSL_RESOLVE_HS709
                                                + MSL_RESOLVE_HS709_V2 + MSL_X_GRADE)
                source = build_metal_graph_warper_msl(MSL_HSV_FUNCS + warper_code[kernel_name], v2=v2,
                                                      chroma=chroma, x_grade=x_grade,
                                                      grading_extension=extension)
            kernel = self.ctx.compile_kernel(kernel_name, source)
            self._kernels[kernel_name] = kernel
        self.ctx.dispatch(kernel, buffers=buffers,
                          textures=textures,
                          threads=(size[0], size[1], 1))

    def _params_buffer(self, params: np.ndarray):
        return self.ctx.create_buffer_from_numpy(params)

    def _white_buffer(self, width: int, height: int):
        key = (width, height)
        if key not in self._white:
            data = np.ones((height, width, 4), dtype=np.float16)
            self._white[key] = self.ctx.create_buffer_from_numpy(data)
        return self._white[key]

    def _curves_buffer(self, lut):
        if lut is None:
            if self._zero_curves is None:
                self._zero_curves = self.ctx.create_buffer_from_numpy(
                    np.zeros(768, dtype=np.float32))
            return self._zero_curves
        flat = curves_lut_to_flat_buffer(lut)
        key = hash(flat.tobytes())
        if key not in self._curves_buffers:
            self._curves_buffers[key] = self.ctx.create_buffer_from_numpy(
                flat)
        return self._curves_buffers[key]

    def _device_name(self) -> str:
        try:
            return self.ctx.device_name
        except Exception:  # noqa: BLE001 —— 诊断字段不抛
            return ""

    # ── 发布生命周期（镜像 GL 执行器） ──

    def release_revision(self, request_id: str) -> int:
        """释放已发布结果的持有（消费完成后调用；buffer 归还池）。"""
        result = self._published.pop(request_id, None)
        if result is None:
            return 0
        released = self._pool.release(result.output_buffer)
        result.output_buffer = None
        result.output = None
        return released

    def pool_allocated(self) -> int:
        """诊断：本执行器累计分配的 scratch buffer 数（池增长审计）。"""
        return self._pool.allocated
