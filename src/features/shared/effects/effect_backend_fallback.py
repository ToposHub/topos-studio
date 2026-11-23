"""预览 / 导出的效果后端能力预检与回退规划（阶段 6.5）。

会话请求某个图形后端（例如 ``metal``）时，逐个效果回答三个问题：

1. 请求的后端能不能跑它？（走既有 ``check_backend_support`` 能力预检）
2. 不能时，有没有**完整**的兼容链路可以回退？「完整」= 回退后端既在效果
   的能力声明里，又确实覆盖当前阶段（预览要有该后端的预览实现；导出要有
   该后端的导出实现）。链路不完整就不回退——返回 ``compatible=False``，
   由调用方阻断并在元素上标示原因，绝不静默丢效果。
3. 原因是什么、属于哪个效果实例？（``effect_id`` + ``reason_code`` + ``detail``）

预览与导出分别规划：同一个插件可能预览能回退到 OpenGL，而导出没有任何后端
（仅预览），两份结论必须各自成立。

线程边界：纯数据计算（读 catalog），无 GPU / Qt。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

from src.features.shared.effects.effect_capability import (
    EffectCapability,
    EffectDiagnostic,
    EffectDiagnosticState,
    check_backend_support,
    resolve_capability,
    resolve_execution_capability,
)

__all__ = [
    "FALLBACK_CHAINS",
    "STAGES",
    "STAGE_EXPORT",
    "STAGE_PREVIEW",
    "EffectFallbackDecision",
    "fallback_reasons",
    "plan_backend_fallback",
    "stage_backend_support",
]

STAGE_PREVIEW = "preview"
STAGE_EXPORT = "export"
STAGES: Tuple[str, ...] = (STAGE_PREVIEW, STAGE_EXPORT)

#: 请求后端 → 依次尝试的兼容回退链（顺序即优先级）。CPU 是最后一道兼容路径。
FALLBACK_CHAINS: Dict[str, Tuple[str, ...]] = {
    "metal": ("opengl", "cpu"),
    "opengl": ("cpu",),
    "cpu": (),
}

REASON_NO_FALLBACK = ""
REASON_REQUESTED_UNAVAILABLE = "requested_backend_unavailable"
REASON_NO_COMPATIBLE_FALLBACK = "no_compatible_fallback"
REASON_DISABLED = "disabled"
REASON_RUNTIME_FAILURE = "runtime_backend_failure"


@dataclass(frozen=True)
class EffectFallbackDecision:
    """一个效果实例在一个阶段上的后端决策。"""

    effect_type: str
    effect_id: str
    stage: str
    requested_backend: str
    selected_backend: Optional[str]    # None = 没有任何可用后端
    fell_back: bool
    compatible: bool                   # True = 所选后端的链路完整
    reason_code: str
    detail: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "effect_type": self.effect_type, "effect_id": self.effect_id, "stage": self.stage,
            "requested_backend": self.requested_backend, "selected_backend": self.selected_backend,
            "fell_back": self.fell_back, "compatible": self.compatible,
            "reason_code": self.reason_code, "detail": self.detail,
        }


def _effect_fields(item: Any) -> Tuple[str, str, bool]:
    if isinstance(item, Mapping):
        return (str(item.get("effect_type", "")), str(item.get("effect_id", "")),
                bool(item.get("is_enabled", True)))
    if isinstance(item, (tuple, list)):
        effect_type, effect_id = str(item[0]), str(item[1])
        enabled = bool(item[2]) if len(item) > 2 else True
        return effect_type, effect_id, enabled
    return (str(getattr(item, "effect_type", "")), str(getattr(item, "effect_id", "")),
            bool(getattr(item, "is_enabled", True)))


def _catalog_known(effect_type: str, catalog: Any) -> bool:
    if catalog is None:
        from src.features.shared.effects.effect_catalog import get_effect_catalog

        catalog = get_effect_catalog()
    return bool(catalog.is_known(effect_type))


def stage_backend_support(effect_type: str, *, stage: str, backend: str,
                          capability: Optional[EffectCapability] = None,
                          catalog: Any = None, compute: str = "") -> Optional[EffectDiagnostic]:
    """该效果在 ``stage`` 阶段能否由 ``backend`` 渲染；能则 None，否则给出原因。"""
    if stage not in STAGES:
        raise ValueError(f"未知阶段 {stage!r}（支持: {', '.join(STAGES)}）")
    known = _catalog_known(effect_type, catalog)
    cap = capability if capability is not None else resolve_capability(
        effect_type, catalog_known=known)
    if backend != "cpu":
        diagnostic = check_backend_support(effect_type, cap, graphics=backend, compute=compute)
        if diagnostic is not None:
            return diagnostic
        if stage == STAGE_PREVIEW and not cap.gpu_preview:
            return EffectDiagnostic(effect_type, effect_type, EffectDiagnosticState.UNSUPPORTED,
                                   f"无 {backend} 预览实现")
    elif stage == STAGE_PREVIEW and not cap.cpu_preview:
        return EffectDiagnostic(effect_type, effect_type, EffectDiagnosticState.UNSUPPORTED,
                               "无 CPU 预览实现")
    if stage == STAGE_EXPORT:
        execution = resolve_execution_capability(effect_type, catalog_known=known, capability=cap)
        exportable = {"metal": execution.metal_export, "opengl": execution.opengl_export,
                      "cpu": execution.cpu_export}.get(backend, False)
        if not exportable:
            return EffectDiagnostic(effect_type, effect_type, EffectDiagnosticState.UNSUPPORTED,
                                   f"无 {backend} 导出实现")
    return None


def plan_backend_fallback(effects: Sequence[Any], *, stage: str,
                          requested_backend: str = "metal", catalog: Any = None,
                          compute: str = "",
                          backend_failures: Optional[Mapping[Tuple[str, str], str]] = None,
                          ) -> Tuple[EffectFallbackDecision, ...]:
    """为一个阶段上的每个效果实例规划后端。

    Args:
        effects: ``EffectInstance`` 或 ``(effect_type, effect_id[, enabled])`` 序列。
        stage: ``"preview"`` 或 ``"export"``。
        requested_backend: 会话请求的图形后端。
        backend_failures: 运行期已知不可用的后端——``{(effect_type, backend): 诊断消息}``。
            例如 Metal 编译失败后把 ``("plugin:X", "metal")`` 映射到精简编译消息；
            该效果在 ``metal`` 上即视为不可用并进入回退。

    Returns:
        与输入同序的决策。``compatible=False`` 表示既不能在请求后端运行、也没有
        完整的兼容回退链路——调用方必须阻断或标示，不得静默继续。
    """
    if stage not in STAGES:
        raise ValueError(f"未知阶段 {stage!r}（支持: {', '.join(STAGES)}）")
    if requested_backend not in FALLBACK_CHAINS:
        raise ValueError(f"未知后端 {requested_backend!r}（支持: {', '.join(sorted(FALLBACK_CHAINS))}）")
    failures: Mapping[Tuple[str, str], str] = backend_failures or {}
    decisions: list[EffectFallbackDecision] = []
    for item in effects:
        effect_type, effect_id, enabled = _effect_fields(item)
        if not enabled:
            decisions.append(EffectFallbackDecision(
                effect_type=effect_type, effect_id=effect_id, stage=stage,
                requested_backend=requested_backend, selected_backend=None, fell_back=False,
                compatible=False, reason_code=REASON_DISABLED, detail="效果已禁用，不参与渲染"))
            continue
        capability = resolve_capability(effect_type, catalog_known=_catalog_known(effect_type, catalog))
        runtime_failure = failures.get((effect_type, requested_backend))
        if runtime_failure is not None:
            primary = EffectDiagnostic(effect_type, effect_type, EffectDiagnosticState.UNSUPPORTED,
                                       runtime_failure)
            primary_code = REASON_RUNTIME_FAILURE
        else:
            primary = stage_backend_support(effect_type, stage=stage, backend=requested_backend,
                                            capability=capability, catalog=catalog, compute=compute)
            primary_code = REASON_REQUESTED_UNAVAILABLE
        if primary is None:
            decisions.append(EffectFallbackDecision(
                effect_type=effect_type, effect_id=effect_id, stage=stage,
                requested_backend=requested_backend, selected_backend=requested_backend,
                fell_back=False, compatible=True, reason_code=REASON_NO_FALLBACK))
            continue
        chosen: Optional[str] = None
        for candidate in FALLBACK_CHAINS[requested_backend]:
            if stage_backend_support(effect_type, stage=stage, backend=candidate,
                                     capability=capability, catalog=catalog) is None:
                chosen = candidate
                break
        if chosen is not None:
            decisions.append(EffectFallbackDecision(
                effect_type=effect_type, effect_id=effect_id, stage=stage,
                requested_backend=requested_backend, selected_backend=chosen, fell_back=True,
                compatible=True, reason_code=primary_code, detail=primary.detail))
        else:
            decisions.append(EffectFallbackDecision(
                effect_type=effect_type, effect_id=effect_id, stage=stage,
                requested_backend=requested_backend, selected_backend=None, fell_back=False,
                compatible=False, reason_code=REASON_NO_COMPATIBLE_FALLBACK,
                detail=(f"{primary.detail}；且在 {stage} 阶段没有完整的兼容回退链路"
                        f"（{' → '.join(FALLBACK_CHAINS[requested_backend]) or '无'}）")))
    return tuple(decisions)


def fallback_reasons(decisions: Sequence[EffectFallbackDecision]) -> Dict[str, str]:
    """``effect_id → 可标示在界面元素上的原因``；无需标示的实例不出现在结果里。"""
    return {d.effect_id: f"{d.reason_code}: {d.detail}" for d in decisions
            if d.fell_back or not d.compatible}
