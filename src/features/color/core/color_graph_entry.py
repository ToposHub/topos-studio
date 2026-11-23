"""阶段 4.1 —— 预览与导出共享同一份调色图的统一入口。

同一工程帧在编辑、拖动、导出与静帧各处必须得到**相同的图顺序、节点
ID、参数与旁路状态**。本模块把这条契约收敛到一个入口：

1. **需求键**（:func:`color_graph_frame_demand_key`）：按「媒体、帧时刻、
   效果修订、工作色域、目标格式」生成需求身份——同一帧的同构请求键相
   等（预览与交付可互认），帧时刻/效果修订/工作色域/目标格式任一变化
   键必变。
2. **共享计划**（:func:`plan_color_graph_frame`）：沿用
   ``color_graph_snapshot.freeze_graph`` 的工程快照、
   ``color_graph_compiler.compile_graph`` 的依赖锥计划与
   ``color_graph_preflight.preflight_graph_resources`` 的资源预检——
   预览（``async_renderer``，GL 执行器）与导出/独立渲染
   （``SharedColorGraphEntry.render_frame``）经同一函数取 plan。
3. **执行与回退**（:class:`SharedColorGraphEntry`）：Metal 纹理执行器
   （``color_graph_texture_metal``，阶段 4.2/4.3 域）优先；Metal 侧
   产生**不支持诊断**（断边/未知节点/参数域/编译/取消）时保有同一帧
   时刻、工程修订与节点参数，回退到既有兼容图执行
   （``color_graph_cpu.run_plan_cpu``——Edit 预览/交付回退路径的生产
   oracle）；兼容路径同样 fail-closed（``UnsupportedNode`` 显式上抛），
   **禁止把失败节点的输出替换为原图继续产出**。

线程边界：``render_frame`` 在渲染 worker 线程调用（Metal context 由
调用方注入并独占持有）；无 Qt。回退单元为一次完整图请求（迁移计划
§阶段 4「回退单元」）。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from fractions import Fraction
from typing import Any, Callable, Mapping

import numpy as np

from src.features.color.core.color_graph_compiler import (
    BoundedPlanCache,
    CapabilityReport,
    ColorGraphRenderPlan,
    GraphCompileError,
    compile_graph,
)
from src.features.color.core.color_graph_cpu import (
    UnsupportedNode,
    run_plan_cpu,
)
from src.features.color.core.color_graph_param_source import (
    graph_node_layer_provider,
)
from src.features.color.core.color_graph_preflight import (
    ResourcePreflightIssue,
    preflight_graph_resources,
)
from src.features.color.core.color_graph_snapshot import ColorGraphSnapshot

#: 需求键的目标格式契约值（帧格式边界；4.4 色彩管理接入前的 v1 域）
TARGET_FORMATS = ("rgba16f",)

#: 编译计划缓存默认字节预算（与 B08 §5.4 的 BoundedPlanCache 同口径）
_DEFAULT_PLAN_CACHE_BYTES = 256 * 1024 * 1024


def color_graph_frame_demand_key(
    *,
    media_id: str,
    frame: int,
    effect_revision: int,
    resource_fingerprint: str = "",
    working_color_context: Mapping[str, Any] | None = None,
    target_format: str = "rgba16f",
    quality: str = "full",
    time_base: Fraction | None = None,
) -> tuple:
    """按「媒体、帧时刻、效果修订、工作色域、目标格式」生成需求键。

    同一帧的同构请求（预览 vs 导出）键相等；``frame``/
    ``effect_revision``/``resource_fingerprint``/色彩上下文/目标格式任一
    变化键必变。``time_base``（精确帧率时基）参与身份——分数帧率交付
    与预览不得互认。
    """
    if target_format not in TARGET_FORMATS:
        raise ValueError(f"未知目标格式: {target_format}")
    context_key = tuple(sorted(
        (str(k), str(v)) for k, v in (working_color_context or {}).items()))
    return (
        "color-graph-frame",
        str(media_id),
        int(frame),
        Fraction(time_base) if time_base is not None else None,
        int(effect_revision),
        str(resource_fingerprint),
        context_key,
        str(target_format),
        str(quality),
    )


@dataclass(frozen=True)
class ColorGraphPlanOutcome:
    """共享计划入口的结果（plan 缺席时诊断必非空）。"""

    plan: ColorGraphRenderPlan | None
    capability: CapabilityReport | None = None
    resource_issues: tuple[ResourcePreflightIssue, ...] = ()
    compile_error: GraphCompileError | None = None

    @property
    def diagnostics(self) -> tuple[str, ...]:
        """机器可读诊断列表（预检/编译/能力，按序）。"""
        items: list[str] = []
        if self.compile_error is not None:
            items.append(f"compile:{self.compile_error.code}:"
                         f"{self.compile_error}")
        items.extend(f"resource:{issue.code}:{issue.node_id}:{issue.detail}"
                     for issue in self.resource_issues)
        if self.capability is not None:
            items.extend(f"capability:{label}:{reason}"
                         for label, reason in self.capability.unsupported)
        return tuple(items)


#: 后端名 → 能力目录（预览 GL 目录由调用方传入 compile 时的 catalog）
def _backend_catalog(backend: str) -> Mapping[str, str] | None:
    if backend == "metal_texture":
        from src.features.color.core.color_graph_texture_metal import (
            METAL_TEXTURE_CAPABILITY_CATALOG,
        )
        return METAL_TEXTURE_CAPABILITY_CATALOG
    if backend == "gl":
        from src.features.color.core.color_graph_compiler import (
            GL_CAPABILITY_CATALOG,
        )
        return GL_CAPABILITY_CATALOG
    if backend == "cpu":
        from src.features.color.core.color_graph_cpu import (
            CPU_CAPABILITY_CATALOG,
        )
        return CPU_CAPABILITY_CATALOG
    raise ValueError(f"未知后端: {backend}")


def plan_color_graph_frame(
    snapshot: ColorGraphSnapshot,
    *,
    size: tuple[int, int],
    backend: str = "metal_texture",
    cache: dict | None = None,
    preflight: bool = True,
) -> ColorGraphPlanOutcome:
    """预览与导出共用的「快照 → 资源预检 → 依赖锥计划 → 能力判定」。

    ``backend``：``metal_texture`` / ``gl`` / ``cpu``——决定能力目录；
    计划本身后端无关（同一快照 + 尺寸 → 同一 pass 序/节点 ID/参数）。
    编译失败（断链/环/目标缺失）返回 ``compile_error`` 而不抛——调用方
    决定回退或显式失败；资源预检问题按 R06 语义列出（不代替执行期校验）。
    """
    resource_issues: tuple[ResourcePreflightIssue, ...] = ()
    if preflight:
        resource_issues = tuple(
            preflight_graph_resources(snapshot.graph))
    try:
        plan = compile_graph(snapshot, size=size, cache=cache)
    except GraphCompileError as exc:
        return ColorGraphPlanOutcome(
            plan=None, resource_issues=resource_issues, compile_error=exc)
    report = plan.validate_capabilities(_backend_catalog(backend))
    return ColorGraphPlanOutcome(
        plan=plan, capability=report, resource_issues=resource_issues)


@dataclass
class ColorGraphFrameResult:
    """一次帧渲染的结果（backend 记录实际执行路径）。"""

    request_id: str
    published: bool
    output: np.ndarray | None = None            # (H, W, 4) float32
    backend: str = ""                            # metal_texture / cpu_compat
    demand_key: tuple = ()
    executed_passes: int = 0
    diagnostics: tuple[str, ...] = ()
    #: Metal 侧不可用原因（回退发生时非空——诊断可见，不静默降级）
    fallback_reason: str = ""


class SharedColorGraphEntry:
    """统一帧入口：Metal 纹理执行优先，兼容图（CPU 生产 oracle）回退。

    生命周期：每渲染 worker 一个实例；``metal_ctx`` 由调用方注入并独占
    （本类不创建设备）；计划缓存实例内复用（BoundedPlanCache）。
    """

    def __init__(self, metal_ctx: Any = None, *,
                 plan_cache_bytes: int = _DEFAULT_PLAN_CACHE_BYTES,
                 metal_enabled: bool = True) -> None:
        self._metal_ctx = metal_ctx
        self._metal_enabled = bool(metal_enabled)
        self._texture_executor: Any = None
        self._plan_cache = BoundedPlanCache(plan_cache_bytes)

    @property
    def metal_available(self) -> bool:
        if not self._metal_enabled:
            return False
        if self._metal_ctx is not None:
            return True
        from src.shared.gpu.metal.context import MetalComputeContext
        return MetalComputeContext.is_supported()

    # ── 计划（预览侧共用；不触 GPU） ──

    def plan_frame(
        self, snapshot: ColorGraphSnapshot, *,
        size: tuple[int, int],
        backend: str = "metal_texture",
        preflight: bool = True,
    ) -> ColorGraphPlanOutcome:
        """共享计划入口（``plan_color_graph_frame`` 的实例封装）。"""
        return plan_color_graph_frame(
            snapshot, size=size, backend=backend, cache=self._plan_cache,
            preflight=preflight)

    # ── 执行（导出/独立渲染；预览走各自执行器 + plan_frame） ──

    def render_frame(
        self,
        graph: Any,
        frame: int,
        source_rgba16f: np.ndarray,
        *,
        media_id: str,
        working_color_context: Mapping[str, Any] | None = None,
        target_format: str = "rgba16f",
        quality: str = "full",
        time_base: Fraction | None = None,
        request_id: str | None = None,
        cancel_check: Callable[[str], bool] | None = None,
        size: tuple[int, int] = (0, 0),
    ) -> ColorGraphFrameResult:
        """渲染一帧：需求键 → 共享计划 → Metal 纹理执行 → 兼容图回退。

        ``source_rgba16f``：(H, W, 4) float16/float32（float32 自动量化
        到 fp16——Metal 纹理路径的工作位深）。失败终态（两条路径都拒绝）
        返回 ``published=False`` + 诊断；**不会**以原图顶替失败节点输出。
        """
        request_id = request_id or str(uuid.uuid4())
        from src.features.color.core.color_graph_snapshot import freeze_graph
        snapshot = freeze_graph(
            graph, session_id=f"entry-{media_id}", source_frame=int(frame),
            working_color_context=working_color_context)
        height, width = source_rgba16f.shape[:2]
        plan_size = size if size not in (None, (0, 0)) else (width, height)
        key = color_graph_frame_demand_key(
            media_id=media_id, frame=int(frame),
            effect_revision=snapshot.graph_revision,
            resource_fingerprint=snapshot.resource_fingerprint,
            working_color_context=working_color_context,
            target_format=target_format, quality=quality,
            time_base=time_base)
        diagnostics: list[str] = []

        outcome = self.plan_frame(snapshot, size=plan_size,
                                  backend="metal_texture")
        diagnostics.extend(outcome.diagnostics)
        if outcome.plan is None or outcome.capability is None \
                or not outcome.capability.executable:
            fallback_reason = "; ".join(diagnostics) or "metal_plan_failed"
        else:
            fallback_reason = ""
            result, metal_reason = self._execute_metal(
                outcome.plan, snapshot, int(frame), source_rgba16f,
                request_id, cancel_check, (width, height))
            if result is not None:
                result.demand_key = key
                result.diagnostics = tuple(diagnostics)
                return result
            fallback_reason = metal_reason or "metal_execution_failed"
            if fallback_reason:
                diagnostics.append(f"metal:{fallback_reason}")
        diagnostics.append(f"fallback:{fallback_reason}")
        return self._fallback_cpu(
            snapshot, int(frame), source_rgba16f, request_id, key,
            tuple(diagnostics), fallback_reason, plan_size)

    # ── 内部：Metal 纹理执行 ──

    def _execute_metal(
        self, plan: ColorGraphRenderPlan, snapshot: ColorGraphSnapshot,
        frame: int, source_rgba16f: np.ndarray, request_id: str,
        cancel_check: Callable[[str], bool] | None,
        size: tuple[int, int],
    ) -> tuple[ColorGraphFrameResult | None, str]:
        """Metal 纹理执行；返回 (结果|None, 不可用原因)。

        取消是**终态**而非回退条件：直接返回 cancelled 结果（不发布半
        帧、不产出兼容路径结果）。
        """
        if not self.metal_available:
            return None, "metal_unavailable"
        try:
            from src.features.color.core.color_graph_texture_metal import (
                ColorGraphMetalTextureExecutor,
            )
            import Metal  # noqa: F401
        except ImportError:
            return None, "metal_import_missing"
        if self._metal_ctx is None:
            from src.shared.gpu.metal.context import MetalComputeContext
            if not MetalComputeContext.is_supported():
                return None, "metal_device_missing"
            self._metal_ctx = MetalComputeContext()
        if self._texture_executor is None:
            self._texture_executor = ColorGraphMetalTextureExecutor(
                self._metal_ctx)
        data = np.ascontiguousarray(source_rgba16f, dtype=np.float16)
        source_texture = self._metal_ctx.create_texture_from_numpy(
            data, self._texture_executor.rgba16f_format,
            size[0], size[1])
        provider = graph_node_layer_provider(snapshot.graph, frame=frame)
        from src.features.color.core.color_graph_metal import (
            MetalGraphCancelled,
        )
        try:
            result = self._texture_executor.execute_plan(
                plan, source_texture, request_id=request_id,
                source_size=size, cancel_check=cancel_check,
                layer_param_provider=provider)
        except MetalGraphCancelled:
            return ColorGraphFrameResult(
                request_id=request_id, published=False,
                backend="cancelled", executed_passes=0,
                diagnostics=("cancelled", "取消：不发布半帧，不回退产出")), ""
        if result.diagnostic_code == "cancelled":
            return ColorGraphFrameResult(
                request_id=request_id, published=False,
                backend="cancelled", executed_passes=0,
                diagnostics=("cancelled", "取消：不发布半帧，不回退产出")), ""
        if not result.published:
            reason = (f"{result.diagnostic_code}: "
                      f"{result.diagnostic_message or ''}")
            return None, reason
        output = self._texture_executor.read_output_rgba16f(result)
        self._texture_executor.release_revision(request_id)
        return ColorGraphFrameResult(
            request_id=request_id, published=True, output=output,
            backend="metal_texture", executed_passes=result.executed_passes
        ), ""

    # ── 内部：兼容图回退（CPU 生产 oracle） ──

    def _fallback_cpu(
        self, snapshot: ColorGraphSnapshot, frame: int,
        source_rgba16f: np.ndarray, request_id: str, key: tuple,
        diagnostics: tuple[str, ...], fallback_reason: str,
        plan_size: tuple[int, int],
    ) -> ColorGraphFrameResult:
        """既有兼容图执行（run_plan_cpu——Edit 预览/交付回退同入口）。

        兼容路径同样 fail-closed：``UnsupportedNode`` / 编译错误显式失败
        （published=False + 诊断），不静默近似、不替换失败节点输出。
        """
        try:
            outcome = self.plan_frame(snapshot, size=plan_size,
                                      backend="cpu", preflight=False)
            if outcome.plan is None:
                return ColorGraphFrameResult(
                    request_id=request_id, published=False,
                    backend="none", demand_key=key,
                    diagnostics=diagnostics + (
                        f"cpu_compile:{outcome.compile_error}",),
                    fallback_reason=fallback_reason)
            provider = graph_node_layer_provider(
                snapshot.graph, frame=frame)
            quantized = np.ascontiguousarray(
                source_rgba16f, dtype=np.float16).astype(np.float32)
            execution = run_plan_cpu(
                outcome.plan, quantized,
                lambda render_pass: provider(render_pass.node_id))
            return ColorGraphFrameResult(
                request_id=request_id, published=True,
                output=np.asarray(execution.output, dtype=np.float32),
                backend="cpu_compat", demand_key=key,
                executed_passes=len(outcome.plan.passes),
                diagnostics=diagnostics,
                fallback_reason=fallback_reason)
        except UnsupportedNode as exc:
            return ColorGraphFrameResult(
                request_id=request_id, published=False, backend="none",
                demand_key=key,
                diagnostics=diagnostics + (f"cpu_unsupported:{exc}",),
                fallback_reason=fallback_reason)
        except GraphCompileError as exc:
            return ColorGraphFrameResult(
                request_id=request_id, published=False, backend="none",
                demand_key=key,
                diagnostics=diagnostics + (f"cpu_compile:{exc}",),
                fallback_reason=fallback_reason)
