""".tglsl 插件的 Metal 能力判定（阶段 6.3 / 6.4）。

插件头部声明 ≠ 能力。一个插件能在 Metal 上跑，需要同时满足：

1. **作者声明**：``@graphics_backends`` 含 ``metal``。未声明的旧插件一律按
   OpenGL 能力解析（计划 6.1 的明文要求），哪怕它在结构上可译；
2. **宿主可译**：受限转译器能把它转成 MSL（解析 + 类型检查 + 发射都成功）。
   声明了 ``metal`` 但转译失败的插件，生效后端里**不含** ``metal``，并留下
   带插件 ID / 版本 / 阶段 / 后端的诊断——它继续走 OpenGL 兼容路径，不会
   被静默丢弃；
3. **运行期可编译**：真正编译在 ``MetalEffectRuntime.compile`` 里发生；失败同样
   是显式诊断，由调用方走回退规划（``effect_backend_fallback``）。

「是否经过真实对拍」不在这里判定——那是 ``msl_plugin_verification`` 的验证
记录与 ``tests/shared/test_tglsl_metal_parity.py`` 的职责。

线程边界：纯计算；分析结果按 (插件 ID, 版本, 内容哈希, 源码指纹, schema
哈希, 声明) 缓存，``resolve_capability`` 每次调用不会重复转译。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, FrozenSet, Optional, Tuple

from src.features.shared.effects.effect_spec import EffectSpec
from src.features.shared.effects.tglsl_front import (
    TRANSPILER_VERSION,
    TranspileStage,
    TglslMetalDiagnostic,
    TglslMetalError,
)
from src.features.shared.effects.tglsl_msl_transpiler import transpile_tglsl

__all__ = [
    "TglslMetalSupport",
    "analyze_tglsl_metal",
    "clear_metal_support_cache",
    "plugin_metal_support",
]

_LEGACY_GRAPHICS: FrozenSet[str] = frozenset({"opengl"})
_LEGACY_COMPUTE: FrozenSet[str] = frozenset({"glsl_fragment"})


@dataclass(frozen=True)
class _ParamView:
    """``ParamDef`` 的可哈希投影（lru_cache 需要）；转译器只读这些字段。"""

    name: str
    ptype: str
    default: Any
    min_val: float
    max_val: float
    labels: Tuple[str, ...]


@dataclass(frozen=True)
class TglslMetalSupport:
    """一个 ``.tglsl`` 插件相对 Metal 后端的判定结果。"""

    plugin_id: str
    plugin_version: str
    declared_graphics: FrozenSet[str]
    declared_compute: FrozenSet[str]
    declared_metal: bool
    #: None = 未声明 metal（未尝试）；True / False = 转译成功 / 失败。
    transpilable: Optional[bool]
    diagnostic: Optional[TglslMetalDiagnostic] = None
    effective_graphics: FrozenSet[str] = _LEGACY_GRAPHICS
    effective_compute: FrozenSet[str] = _LEGACY_COMPUTE
    msl_fingerprint: str = ""
    features: Tuple[str, ...] = ()

    @property
    def supports_metal(self) -> bool:
        return "metal" in self.effective_graphics

    def to_dict(self) -> dict:
        return {
            "plugin_id": self.plugin_id, "plugin_version": self.plugin_version,
            "declared_graphics": sorted(self.declared_graphics),
            "declared_compute": sorted(self.declared_compute),
            "declared_metal": self.declared_metal, "transpilable": self.transpilable,
            "effective_graphics": sorted(self.effective_graphics),
            "effective_compute": sorted(self.effective_compute),
            "msl_fingerprint": self.msl_fingerprint, "features": list(self.features),
            "diagnostic": self.diagnostic.to_dict() if self.diagnostic else None,
        }


def _param_views(param_defs: Tuple[Any, ...]) -> Tuple[_ParamView, ...]:
    return tuple(
        _ParamView(name=p.name, ptype=p.ptype, default=p.default, min_val=float(p.min_val),
                   max_val=float(p.max_val), labels=tuple(p.labels or ()))
        for p in param_defs)


@lru_cache(maxsize=256)
def _analyze_cached(plugin_id: str, plugin_version: str, source: str,
                    params: Tuple[_ParamView, ...], declared_graphics: FrozenSet[str],
                    declared_compute: FrozenSet[str],
                    content_hash: str) -> TglslMetalSupport:
    declared_metal = "metal" in declared_graphics
    support = TglslMetalSupport(
        plugin_id=plugin_id, plugin_version=plugin_version,
        declared_graphics=declared_graphics, declared_compute=declared_compute,
        declared_metal=declared_metal, transpilable=None,
        effective_graphics=frozenset(declared_graphics),
        effective_compute=frozenset(declared_compute))
    if not declared_metal:
        return support
    try:
        transpiled = transpile_tglsl(source, params, plugin_id=plugin_id,
                                     plugin_version=plugin_version, content_hash=content_hash)
    except TglslMetalError as exc:
        diagnostic = exc.diagnostic
        return TglslMetalSupport(
            plugin_id=plugin_id, plugin_version=plugin_version,
            declared_graphics=declared_graphics, declared_compute=declared_compute,
            declared_metal=True, transpilable=False, diagnostic=diagnostic,
            effective_graphics=frozenset(declared_graphics - {"metal"}),
            effective_compute=frozenset(declared_compute - {"metal_compute"}))
    return TglslMetalSupport(
        plugin_id=plugin_id, plugin_version=plugin_version,
        declared_graphics=declared_graphics, declared_compute=declared_compute,
        declared_metal=True, transpilable=True, diagnostic=None,
        effective_graphics=frozenset(declared_graphics),
        effective_compute=frozenset(declared_compute),
        msl_fingerprint=transpiled.msl_fingerprint, features=transpiled.features)


def clear_metal_support_cache() -> None:
    """清空分析缓存（插件热重载 / 测试用）。"""
    _analyze_cached.cache_clear()


def plugin_metal_support(spec: EffectSpec) -> Optional[TglslMetalSupport]:
    """判定一个 catalog spec 的 Metal 支持；非 ``.tglsl`` 插件返回 None。

    空声明按旧插件处理（OpenGL-only）。本函数不编译、不触达 GPU。
    """
    if not spec.plugin_source_glsl:
        return None
    source = spec.plugin_source_glsl
    content_hash = spec.plugin_content_hash or hashlib.sha256(source.encode("utf-8")).hexdigest()
    return _analyze_cached(
        spec.plugin_id[len("plugin:"):] if spec.plugin_id.startswith("plugin:") else spec.plugin_id,
        spec.plugin_version or "", source, _param_views(tuple(spec.param_defs)),
        frozenset(spec.graphics_backends) or _LEGACY_GRAPHICS,
        frozenset(spec.compute_backends) or _LEGACY_COMPUTE, content_hash)


def analyze_tglsl_metal(plugin_id: str, plugin_version: str, source: str,
                        param_defs: Tuple[Any, ...], *, declared_graphics: FrozenSet[str],
                        declared_compute: FrozenSet[str] = _LEGACY_COMPUTE,
                        content_hash: str = "") -> TglslMetalSupport:
    """直接分析（catalog 之外的场景，如测试与工具）；语义同 :func:`plugin_metal_support`。"""
    return _analyze_cached(
        plugin_id, plugin_version, source, _param_views(tuple(param_defs)),
        frozenset(declared_graphics), frozenset(declared_compute),
        content_hash or hashlib.sha256(source.encode("utf-8")).hexdigest())


def transpiler_version() -> str:
    """当前转译器版本（进入编译缓存键）。"""
    return TRANSPILER_VERSION
