# DORMANT(2026-08-19 审计): 已实现且有测试，但暂无生产入口（有意休眠）。
# 接线前请勿在生产依赖；若半年内无接线计划建议删除。
"""FX-H01 / FX-M03: effect capability declaration & diagnostics.

Audit FX-H01: every effect that appears in the attach UI must declare
which backends can render it (GPU preview / CPU preview / export), so
the panel can tell the user "this effect won't appear in the export"
instead of silently dropping it. Audit FX-M03: when a shader fails to
compile or a parameter can't be sampled, the user sees "effect invalid"
with no status — we need a per-instance diagnostic state.

This module provides the pure-data ``EffectCapability`` (audit §3.2
target dataclass) and a resolver that computes it from the EFFECTIVE
backend coverage rather than a hand-maintained table, so adding a new
CPU/export handler automatically updates the capability.

It also defines the diagnostic enum (FX-M03) and a tiny aggregator that
maps per-effect render outcomes (ran / degraded / bypassed / error) into
a status the UI can badge.

This is intentionally side-effect free and has no GL/Qt deps, so it is
unit-testable in pure data space — the same constraint audit §7 places
on the P0 test matrix.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import FrozenSet, Optional, Tuple

from src.shared.gpu.backend_types import ComputeBackendKind, GraphicsBackendKind

# 默认后端轴（Phase 7 P7-06）：既有效力声明未扩展的旧效果/插件按
# OpenGL-only 处理（P7-07 契约）——与计划 v2 之前的实际行为一致。
_DEFAULT_GRAPHICS: FrozenSet[str] = frozenset({GraphicsBackendKind.OPENGL.value})
_DEFAULT_COMPUTE: FrozenSet[str] = frozenset({ComputeBackendKind.GLSL_FRAGMENT.value})


# ── Backend coverage source of truth ─────────────────────────────────
# These frozensets mirror the actual dispatch tables in the renderers:
#   - GPU preview: every EffectCatalog spec ships a fragment shader.
#   - CPU preview: timeline_preview_renderer._apply_clip_effects_cpu
#     (blur / sharpen).
#   - Export:      timeline_export._CPU_EFFECT_HANDLERS.
# Update these when a renderer gains/loses a handler; the capability
# resolver reads them, so the UI status stays in sync automatically.

CPU_PREVIEW_EFFECT_TYPES: FrozenSet[str] = frozenset({"blur", "sharpen"})
EXPORT_EFFECT_TYPES: FrozenSet[str] = frozenset({
    "blur", "sharpen", "glow", "erode_dilate", "edge_detect",
    "denoise", "defocus",
})
# Native Metal timeline export accelerates bounded parameter subsets of Blur
# (Box/Gaussian/Bilateral), Sharpen, Glow, Erode/Dilate, Edge Detect,
# Denoise, and Defocus. Runtime parameter gating keeps unsupported variants
# on the established CPU export path; resolve_capability declares Metal only
# for effects with an implementation-backed parameter subset.
GPU_EXPORT_EFFECT_TYPES: FrozenSet[str] = frozenset({
    "blur", "sharpen", "glow", "erode_dilate", "edge_detect",
    "denoise", "defocus",
})


# ── EffectCapability (audit §3.2) ────────────────────────────────────

@dataclass(frozen=True)
class EffectCapability:
    """Which backends can render an effect, plus key property flags.

    Fields mirror audit §3.2's target dataclass. ``gpu_export`` is kept
    separate from ``gpu_preview`` because timeline export has its own
    offscreen renderer. It means at least one parameter subset can run there;
    the exporter still checks each clip's actual parameters before dispatch.
    """

    gpu_preview: bool
    cpu_preview: bool
    gpu_export: bool
    cpu_export: bool
    supports_hdr: bool = True
    supports_alpha: bool = True
    deterministic: bool = True
    # Phase 7 P7-06：后端轴（值即 backend_types 四维枚举的 value 字符串）。
    # 语义：该效果**能跑**的 graphics/compute 后端集合；空集 = 无后端。
    # 默认 {opengl}/{glsl_fragment} 保持旧调用方与旧插件行为不变。
    graphics_backends: FrozenSet[str] = field(default=_DEFAULT_GRAPHICS)
    compute_backends: FrozenSet[str] = field(default=_DEFAULT_COMPUTE)

    @property
    def exportable(self) -> bool:
        """Whether the effect can reach the deliverable on ANY export path."""
        return self.cpu_export or self.gpu_export

    @property
    def previewable(self) -> bool:
        """Whether the effect can be seen in ANY preview path."""
        return self.gpu_preview or self.cpu_preview

    def supports_graphics(self, api: str) -> bool:
        """该效果是否支持指定 graphics 后端（如 'opengl' / 'metal'）。"""
        return (api or "").strip().lower() in self.graphics_backends

    def supports_compute(self, kind: str) -> bool:
        """该效果是否支持指定 compute 后端（如 'glsl_fragment' /
        'metal_compute'）。CPU 片段回退一律视为可用（fragment 回退是
        compute 效果的既有降级通道，§4.3）。"""
        k = (kind or "").strip().lower()
        if k in self.compute_backends:
            return True
        if k == ComputeBackendKind.CPU.value:
            return True
        # compute 效果声明了 glsl_compute（GL 4.3+ SSBO 路径）时，
        # glsl_fragment 视为其已存在的自动回退（§0.4：compute→fragment
        # 自动回退是项目既有机制）。
        if (
            k == ComputeBackendKind.GLSL_FRAGMENT.value
            and ComputeBackendKind.GLSL_COMPUTE.value in self.compute_backends
        ):
            return True
        return False


def resolve_capability(
    effect_type: str,
    *,
    catalog_known: bool,
    cpu_preview_types: FrozenSet[str] = CPU_PREVIEW_EFFECT_TYPES,
    export_types: FrozenSet[str] = EXPORT_EFFECT_TYPES,
    gpu_export_types: FrozenSet[str] = GPU_EXPORT_EFFECT_TYPES,
    graphics_backends: Optional[FrozenSet[str]] = None,
    compute_backends: Optional[FrozenSet[str]] = None,
) -> EffectCapability:
    """Compute an effect's capability from the effective backend coverage.

    Args:
        effect_type: the EffectInstance.effect_type / EffectSpec.effect_type.
        catalog_known: whether the effect is in the EffectCatalog (i.e. has
            a GPU fragment shader). Pass ``catalog.is_known(effect_type)``.
        cpu_preview_types: effect types with a CPU preview implementation.
        export_types: effect types with a CPU export implementation.
        gpu_export_types: effects with at least one GPU-exportable parameter subset.
        graphics_backends: 该效果可用的 graphics 后端（P7-06）；None 时按
            实际实现表推导，显式集合则尊重 EffectSpec/插件声明。
        compute_backends: 该效果可用的 compute 后端；None 时按实际实现表推导。

    Returns:
        ``EffectCapability``. An unknown effect has no backend at all
        (all False) so the UI can flag it as unsupported rather than
        silently no-op'ing.
    """
    has_gpu = catalog_known
    has_cpu_preview = effect_type in cpu_preview_types
    has_cpu_export = effect_type in export_types
    if graphics_backends is None:
        inferred_graphics = set(_DEFAULT_GRAPHICS) if has_gpu else set()
        if has_gpu and effect_type in gpu_export_types:
            inferred_graphics.add(GraphicsBackendKind.METAL.value)
        resolved_graphics = frozenset(inferred_graphics)
    else:
        resolved_graphics = graphics_backends if has_gpu else frozenset()
    if compute_backends is None:
        inferred_compute = set(_DEFAULT_COMPUTE) if has_gpu else set()
        if has_gpu and effect_type in gpu_export_types:
            inferred_compute.add(ComputeBackendKind.METAL_COMPUTE.value)
        resolved_compute = frozenset(inferred_compute)
    else:
        resolved_compute = compute_backends if has_gpu else frozenset()
    return EffectCapability(
        gpu_preview=has_gpu,
        cpu_preview=has_cpu_preview,
        gpu_export=has_gpu and effect_type in gpu_export_types,
        cpu_export=has_cpu_export,
        graphics_backends=resolved_graphics,
        compute_backends=resolved_compute,
    )


# ── Phase 7 P7-08：按会话后端的可用性诊断 ─────────────────────────────

def check_backend_support(
    effect_type: str,
    capability: EffectCapability,
    *,
    graphics: str,
    compute: str = "",
) -> Optional[EffectDiagnostic]:
    """按当前会话的后端选择检查效果是否可用（P7-08）。

    不可用时返回带明确原因的 ``UNSUPPORTED`` 诊断（UI 可 badge）——
    **绝不静默丢效果**（.clinerules:11）；可用返回 None。

    Args:
        effect_type: 效果类型标识（诊断里携带）。
        capability: 该效果已解析的能力。
        graphics: 当前会话 graphics 后端（'opengl' / 'metal' / ...）。
        compute: 当前会话 compute 后端；空串表示仅按 graphics 判定
            （纯 fragment 效果没有独立 compute 选择）。
    """
    if not capability.graphics_backends and not capability.compute_backends:
        return EffectDiagnostic(
            effect_id=effect_type, effect_type=effect_type,
            state=EffectDiagnosticState.UNSUPPORTED,
            detail="no render backend declared",
        )
    if not capability.supports_graphics(graphics):
        declared = ", ".join(sorted(capability.graphics_backends)) or "none"
        return EffectDiagnostic(
            effect_id=effect_type, effect_type=effect_type,
            state=EffectDiagnosticState.UNSUPPORTED,
            detail=f"graphics backend {graphics!r} not supported (declares: {declared})",
        )
    if compute and not capability.supports_compute(compute):
        declared = ", ".join(sorted(capability.compute_backends)) or "none"
        return EffectDiagnostic(
            effect_id=effect_type, effect_type=effect_type,
            state=EffectDiagnosticState.UNSUPPORTED,
            detail=f"compute backend {compute!r} not supported (declares: {declared})",
        )
    return None


# ── Phase 7 P7-07：manifest 后端声明解析 ─────────────────────────────

def parse_backend_declaration(raw: str, *, kind: str) -> FrozenSet[str]:
    """``.tglsl`` manifest 的后端声明（逗号分隔）→ 后端值集合（P7-07）。

    ``'opengl, metal'`` → ``frozenset({'opengl','metal'})``。未知值显式
    ValueError（诊断而非静默忽略——静默丢声明会让插件在后端选择时得到
    与作者意图相反的行为）。'auto' 不是合法声明（能力必须具体）。

    Args:
        raw: 声明原文（如 ``"opengl,metal"``）；空串 → 空集。
        kind: ``'graphics'`` 或 ``'compute'``。
    """
    vocab = GraphicsBackendKind if kind == "graphics" else ComputeBackendKind
    valid = {m.value for m in vocab} - {"auto"}
    out: set[str] = set()
    for token in (raw or "").split(","):
        t = token.strip().lower()
        if not t:
            continue
        if t not in valid:
            raise ValueError(
                f"未知 {kind} 后端声明 {t!r}（支持: {', '.join(sorted(valid))}）"
            )
        out.add(t)
    return frozenset(out)


# ── FX-M03: per-instance diagnostic state ────────────────────────────

class EffectDiagnosticState(str, Enum):
    """Status of an effect instance at render time (FX-M03).

    Values are strings so they serialise cleanly and the UI can badge
    without importing the enum.
    """

    ACTIVE = "active"                  # ran normally on the active backend
    PREVIEW_DEGRADED = "preview_degraded"  # ran, but at reduced quality
    BYPASSED = "bypassed"              # explicitly disabled / mix=0
    UNSUPPORTED = "unsupported"        # no backend can render it
    COMPILE_ERROR = "compile_error"    # shader failed to compile
    PARAM_ERROR = "param_error"        # parameter sampling threw
    EXPORT_BLOCKED = "export_blocked"  # fine in preview, missing on export
    # FX-M07: plugin identity states
    PLUGIN_MISSING = "plugin_missing"      # plugin file not found on this machine
    VERSION_MISMATCH = "version_mismatch"  # plugin version differs from stored
    SCHEMA_MISMATCH = "schema_mismatch"    # parameter schema changed


@dataclass(frozen=True)
class EffectDiagnostic:
    """One effect instance's render status (FX-M03).

    The renderer fills one of these per effect per frame (cheaply, off
    the decisions it already makes); the Effects panel aggregates them
    into a badge so 'effect invalid' is never silent.
    """

    effect_id: str
    effect_type: str
    state: EffectDiagnosticState
    detail: str = ""

    @property
    def ok(self) -> bool:
        """Whether the effect is contributing to the frame as intended."""
        return self.state in (EffectDiagnosticState.ACTIVE,
                              EffectDiagnosticState.PREVIEW_DEGRADED)


def classify_for_export(
    effect_type: str,
    capability: EffectCapability,
) -> Optional[EffectDiagnosticState]:
    """Pre-export check: is this effect going to survive the export?

    Returns ``EXPORT_BLOCKED`` when the effect is previewable (so the user
    sees it while editing) but has no export backend — the audit's
    'silently disappears from the deliverable' case. Returns None when the
    effect is fine for export (or is already unsupported in preview).
    """
    if not capability.previewable:
        return EffectDiagnosticState.UNSUPPORTED
    if not capability.exportable:
        return EffectDiagnosticState.EXPORT_BLOCKED
    return None


# ── FX-M03: per-effect UI status (badge source) ──────────────────────

def compute_effect_status(
    effect_type: str,
    capability: EffectCapability,
) -> EffectDiagnostic:
    """The status badge text for an effect in the Inspector (FX-M03).

    Pure-data: given an effect's resolved capability, return the
    ``EffectDiagnostic`` the panel badges from. This is the value the
    renderer-side diagnostic (FX-M03) will eventually corroborate with
    per-frame compile/param outcomes; for now it reflects the static
    capability (which backend can render it), so the user can already see
    'Preview-only' / 'Unsupported for export' instead of a silent no-op.
    """
    if not capability.previewable:
        return EffectDiagnostic(
            effect_id=effect_type, effect_type=effect_type,
            state=EffectDiagnosticState.UNSUPPORTED,
            detail="no render backend",
        )
    if not capability.exportable:
        return EffectDiagnostic(
            effect_id=effect_type, effect_type=effect_type,
            state=EffectDiagnosticState.EXPORT_BLOCKED,
            detail="not in export — drops from deliverable",
        )
    if capability.gpu_preview and not capability.cpu_preview:
        return EffectDiagnostic(
            effect_id=effect_type, effect_type=effect_type,
            state=EffectDiagnosticState.PREVIEW_DEGRADED,
            detail="GPU-only — CPU fallback unavailable",
        )
    return EffectDiagnostic(
        effect_id=effect_type, effect_type=effect_type,
        state=EffectDiagnosticState.ACTIVE,
    )


# ── FX-M05: unified failure policy ───────────────────────────────────

def decide_preview_policy(
    capability: EffectCapability,
    *,
    gpu_available: bool,
) -> EffectDiagnosticState:
    """Interactive preview failure policy (audit FX-M05).

    Preview may DEGRADE gracefully (the user keeps editing) but must never
    silently change the picture: a preview-only effect on a GPU-less
    machine returns ``UNSUPPORTED`` so the panel can badge it, rather than
    rendering nothing with no explanation.

    Args:
        capability: the effect's resolved capability.
        gpu_available: whether the GPU preview pipeline initialised.

    Returns:
        ``ACTIVE`` when the effect runs normally; ``PREVIEW_DEGRADED`` when
        it can run on CPU but the GPU path was expected (so the user knows
        quality may differ from export); ``UNSUPPORTED`` when no backend
        can render it at all.
    """
    if capability.gpu_preview and gpu_available:
        return EffectDiagnosticState.ACTIVE
    if capability.cpu_preview:
        # GPU unavailable but a CPU handler exists — degrade, don't drop.
        return EffectDiagnosticState.PREVIEW_DEGRADED
    # GPU-only effect on a GPU-less machine — can't render at all.
    return EffectDiagnosticState.UNSUPPORTED


def decide_export_policy(capability: EffectCapability) -> EffectDiagnosticState:
    """Export failure policy (audit FX-M05).

    Unlike preview, export must NOT silently degrade: a preview-only effect
    that has no export backend returns ``EXPORT_BLOCKED`` so the preflight
    can either block the export or require an explicit 'bypass' decision —
    never silently omit the effect from the deliverable.

    Returns ``ACTIVE`` when the effect has an export backend;
    ``EXPORT_BLOCKED`` otherwise.
    """
    if capability.exportable:
        return EffectDiagnosticState.ACTIVE
    return EffectDiagnosticState.EXPORT_BLOCKED


# ── T-P1-20：统一 EffectExecutionCapability ──────────────────────────

@dataclass(frozen=True)
class EffectExecutionCapability:
    """单一效果执行能力契约（T-P1-20 步骤 1）。

    取代散落 bool：七个显式轴 + 每轴 reason（机器可读 code 与人类可读
    消息）+ 质量约束。UI、AI manifest、Deliver preflight、CLI 一律从
    :func:`resolve_execution_capability` 取值，不再各自维护真值表。

    轴语义：
    - ``cpu_preview`` / ``gpu_preview``：预览后端；
    - ``cpu_export`` / ``metal_export`` / ``opengl_export``：导出后端
      （GPU 导出按实际分派链拆分为 Metal / OpenGL 两条轴）；
    - ``preview_only``：可预览但无任何导出后端（Deliver 必须拒绝）；
    - ``unsupported``：预览/导出均无后端（attach 后零视觉效果）。
    """

    effect_type: str
    cpu_preview: bool
    gpu_preview: bool
    cpu_export: bool
    metal_export: bool
    opengl_export: bool
    preview_only: bool
    unsupported: bool
    reasons: FrozenSet[Tuple[str, str, str]] = frozenset()  # {(axis, code, message)}
    quality_constraints: tuple[str, ...] = ()     # e.g. metal 参数子集门控

    @property
    def exportable(self) -> bool:
        """是否有任何导出后端（CPU / Metal / OpenGL 任一）。"""
        return self.cpu_export or self.metal_export or self.opengl_export

    @property
    def previewable(self) -> bool:
        """是否有任何预览后端（GPU / CPU 任一）。"""
        return self.gpu_preview or self.cpu_preview

    def reason_for(self, axis: str) -> tuple:
        """该轴的 (code, message)；可用轴返回 ("", "")。"""
        for a, code, message in self.reasons:
            if a == axis:
                return (code, message)
        return ("", "")

    def delivery_restriction(self) -> tuple:
        """交付限制 (code, message)；可交付返回 ("", "")。

        UI badge、AI manifest、Deliver preflight 共用此出口，保证三处
        拒绝原因逐字一致（T-P1-20 步骤 2）。
        """
        if self.exportable:
            return ("", "")
        if self.unsupported:
            code, message = self.reason_for("unsupported")
            return (code or "effect_unsupported", message)
        code, message = self.reason_for("preview_only")
        return (code or "preview_only", message)

    def to_dict(self) -> dict:
        """序列化为可 JSON 化的 dict（AI manifest / CLI / 遥测用）。

        除七个布尔轴外，附带派生属性 ``exportable``/``previewable``、
        排序后的 reasons 列表、质量约束列表与交付限制摘要。
        """
        return {
            "effect_type": self.effect_type,
            "cpu_preview": self.cpu_preview,
            "gpu_preview": self.gpu_preview,
            "cpu_export": self.cpu_export,
            "metal_export": self.metal_export,
            "opengl_export": self.opengl_export,
            "preview_only": self.preview_only,
            "unsupported": self.unsupported,
            "exportable": self.exportable,
            "previewable": self.previewable,
            "reasons": [
                {"axis": a, "code": c, "message": m}
                for a, c, m in sorted(self.reasons)
            ],
            "quality_constraints": list(self.quality_constraints),
            "delivery_restriction": {
                "code": self.delivery_restriction()[0],
                "message": self.delivery_restriction()[1],
            } if self.delivery_restriction()[0] else None,
        }


#: Metal 导出链按逐参数门控：不支持变体在运行时回落 CPU 导出（见
#: timeline_export._gpu_metal_effect_instance_uniforms）——能力声明为
#: ``metal_export=True`` 时必须同时携带该质量约束，禁止宣传成全参数等价。
_METAL_PARAM_SUBSET = "metal_export_parameter_subset_gated_fallback_cpu"

#: 插件 GL-only 导出的质量约束：CPU 回退链无插件实现，回退时该效果
#: 不参与渲染（导出遥测可见，UI 必须可解释）。
_PLUGIN_GL_ONLY_CONSTRAINT = "plugin_opengl_export_only_cpu_fallback_skips_effect"


def resolve_execution_capability(
    effect_type: str,
    *,
    catalog_known: bool,
    capability: Optional[EffectCapability] = None,
    plugin_export_block_reason: str = "",
) -> EffectExecutionCapability:
    """从有效后端覆盖解析统一执行能力（T-P1-20 单一真相源）。

    Args:
        effect_type: 效果类型（``EffectInstance.effect_type``）。
        catalog_known: 是否在 EffectCatalog 中（有 GPU fragment shader）。
        capability: 预解析的 :class:`EffectCapability`；None 时按默认
            实现表自行解析。
        plugin_export_block_reason: 插件身份/导出阻断原因（非空 = 插件
            不可导出，写入 preview_only reason）。
    """
    cap = capability if capability is not None else resolve_capability(
        effect_type, catalog_known=catalog_known)
    # GPU 导出双链（GL offscreen 链 + Metal uniforms 链）覆盖同一批效果；
    # Metal 链按参数子集门控（质量约束显式携带）。
    metal_export = cap.gpu_export
    opengl_export = cap.gpu_export
    # T-P2-20/W06：插件可在头部声明 ``// @export_support opengl``（catalog
    # 落为 plugin_capabilities 的 "export_opengl"）。此类插件经 GL 导出
    # 效果链真实参与交付；CPU 回退链没有插件实现——该效果会被跳过，
    # 因此质量约束显式携带该事实（禁止宣传成全后端等价）。
    plugin_gl_only = False
    if (not cap.exportable and effect_type.startswith("plugin:")):
        try:
            from src.features.shared.effects.effect_catalog import (
                get_effect_catalog,
            )
            spec = get_effect_catalog().get_spec(effect_type)
            if spec is not None and "export_opengl" in spec.plugin_capabilities:
                opengl_export = True
                plugin_gl_only = True
        except Exception:  # noqa: BLE001 — 能力求证失败按不可交付处理
            plugin_gl_only = False
    preview_only = bool(cap.previewable and not (cap.cpu_export or metal_export
                                                 or opengl_export))
    unsupported = bool(not cap.previewable and not cap.exportable
                       and not opengl_export)

    reasons: set[Tuple[str, str, str]] = set()
    if not cap.cpu_export:
        code = "plugin_preview_only" if preview_only else "no_cpu_implementation"
        msg = ("插件仅声明 GPU 预览后端，无导出实现"
               if preview_only else "无 CPU 导出实现")
        if plugin_export_block_reason:
            code = f"plugin_export_blocked:{plugin_export_block_reason}"
        reasons.add(("cpu_export", code, msg))
    if not metal_export:
        reasons.add(("metal_export", "no_metal_export_path",
                     "无 Metal 导出实现"))
    if not opengl_export:
        reasons.add(("opengl_export", "no_opengl_export_path",
                     "无 OpenGL 导出实现"))
    if preview_only:
        reasons.add(("preview_only", "plugin_preview_only",
                     "该效果仅可在预览中呈现；导出将拒绝包含它的时间线"))
    if unsupported:
        code = "effect_unknown" if not catalog_known else "no_backend_declared"
        reasons.add(("unsupported", code,
                     "无任何预览/导出后端（效果不可渲染）"))

    constraints = ()
    if metal_export:
        constraints += (_METAL_PARAM_SUBSET,)
    if plugin_gl_only:
        constraints += (_PLUGIN_GL_ONLY_CONSTRAINT,)
    return EffectExecutionCapability(
        effect_type=effect_type,
        cpu_preview=cap.cpu_preview,
        gpu_preview=cap.gpu_preview,
        cpu_export=cap.cpu_export,
        metal_export=metal_export,
        opengl_export=opengl_export,
        preview_only=preview_only,
        unsupported=unsupported,
        reasons=frozenset(reasons),
        quality_constraints=constraints,
    )
