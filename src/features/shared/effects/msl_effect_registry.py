"""MSL 效果注册表（阶段 6.2）：已登记的 MSL 实现、内置条目与验证记录。

三个职责，彼此可分别审计：

1. **helper 库**——受控算子的 MSL 实现 + 对应的 GLSL 模式。转译器
   （``tglsl_msl_transpiler``）在插件的 ``process`` 与某个 helper 的 GLSL 模式
   同构时，改为调用这里登记的 MSL 函数。注册时即校验模式可解析，坏 helper
   进不了库。
2. **内置条目**——宿主自带效果（如 ``builtin:exposure``）。条目只声明参数
   schema 与 GLSL 源；kernel 由**同一个受控模板**生成（输入纹理、sampler、
   uniform、输出格式显式绑定），与插件走完全相同的转译 / 编译 / 执行链路。
3. **验证记录**——哪个插件 / 条目在哪台设备、哪天、以多大误差通过了
   「GL 参考值 vs Metal 读回」对拍。登记 Metal 能力的前提是有这条记录。

「能编译」不等于「已验证」：记录里的 ``status`` 只有 ``verified`` 才代表真实
运行通过。哈希噪声类（sin-hash 抖动 / 算术哈希）在两个后端之间天然不可复现，
单列为 ``noise_gated``，不计入 verified。

线程边界：纯数据容器，注册与读取都持锁；不触达 GPU。
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

from src.features.shared.effects.tglsl_front import (
    TglslFrontError,
    parse_tglsl_glsl,
)
from src.features.shared.effects.tglsl_msl_transpiler import ENTRY_NAME, MslHelper

__all__ = [
    "EXPOSURE_ENTRY",
    "EXPOSURE_GLSL",
    "EXPOSURE_HELPER",
    "MslEffectEntry",
    "MslEffectRegistry",
    "VerificationRecord",
    "get_msl_effect_registry",
]

STATUS_VERIFIED = "verified"
STATUS_UNVERIFIED = "unverified"
STATUS_NOISE_GATED = "noise_gated"


# ── 曝光（参考实现 6B）─────────────────────────────────────────────────

#: 计划「参考实现 6B」左侧的 GLSL 样本：既是内置条目的源，也是命名样本。
EXPOSURE_GLSL = """\
// --- Topos Plugin Metadata ---
// @name Migration Exposure Sample
// @input_space scene-linear
// @output_space scene-linear
// @param slider exposure "Exposure" 0.0 -5.0 5.0
// -----------------------------
vec3 process(vec3 color, vec2 uv) {
    return color * exp2(exposure);
}
"""

#: 计划「参考实现 6B」右侧的 MSL 函数，原样登记。
EXPOSURE_HELPER = MslHelper(
    helper_id="builtin:exposure",
    version="1.0.0",
    msl_name="migration_exposure",
    msl_params=("float3 linear_rgb", "float exposure"),
    msl_body="return linear_rgb * exp2(exposure);",
    glsl_pattern=("vec3 process(vec3 color, vec2 uv) {\n"
                  "    return color * exp2(exposure);\n"
                  "}\n"),
    glsl_param_bindings=("exposure",),
    param_binding_types=("float",),
    description="scene-linear 曝光（档）：linear_rgb * exp2(exposure)",
)


# ── 数据类型 ───────────────────────────────────────────────────────────


@dataclass(frozen=True)
class VerificationRecord:
    """一次「GL 参考值 vs Metal 读回」对拍的结论。"""

    status: str                     # verified / unverified / noise_gated
    device: str = ""
    date: str = ""
    reference: str = "GL: EffectPassRunner/dctl_wrapper, RGBA16F"
    max_abs_diff: float = -1.0      # < 0 = 未测量
    max_rel_diff: float = -1.0
    max_fp16_ulp: float = -1.0
    corpus: str = ""                # 对拍语料的简述
    evidence: str = ""              # 证据文件路径（相对仓库根）
    note: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status, "device": self.device, "date": self.date,
            "reference": self.reference, "max_abs_diff": self.max_abs_diff,
            "max_rel_diff": self.max_rel_diff, "max_fp16_ulp": self.max_fp16_ulp,
            "corpus": self.corpus, "evidence": self.evidence, "note": self.note,
        }


@dataclass(frozen=True)
class MslEffectEntry:
    """注册表内置条目：参数 schema + GLSL 源 + 能力声明 + 验证状态。"""

    effect_id: str
    name: str
    version: str
    description: str
    glsl_source: str
    param_defs: Tuple[Any, ...]
    helper_id: str
    input_space: str = "scene-linear"
    output_space: str = "scene-linear"
    preserves_alpha: bool = True
    deterministic: bool = True
    hdr: bool = True
    verification: Optional[VerificationRecord] = None

    @property
    def param_schema_hash(self) -> str:
        from src.features.shared.effects.plugin_identity import compute_parameter_schema_hash

        return compute_parameter_schema_hash(self.param_defs)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "effect_id": self.effect_id, "name": self.name, "version": self.version,
            "description": self.description, "helper_id": self.helper_id,
            "params": [(p.name, p.ptype, p.default, p.min_val, p.max_val) for p in self.param_defs],
            "input_space": self.input_space, "output_space": self.output_space,
            "preserves_alpha": self.preserves_alpha, "deterministic": self.deterministic,
            "hdr": self.hdr, "verification": self.verification.to_dict() if self.verification else None,
        }


def _exposure_param_defs() -> Tuple[Any, ...]:
    from src.features.composite.nodes.base_node import ParamDef

    return (ParamDef("exposure", "float", 0.0, min_val=-5.0, max_val=5.0, step=0.01, decimals=2),)


def _build_exposure_entry(verification: Optional[VerificationRecord]) -> MslEffectEntry:
    return MslEffectEntry(
        effect_id="builtin:exposure",
        name="Exposure",
        version="1.0.0",
        description="scene-linear 曝光（档）：color * exp2(exposure)；宿主保持 Alpha。",
        glsl_source=EXPOSURE_GLSL,
        param_defs=_exposure_param_defs(),
        helper_id=EXPOSURE_HELPER.helper_id,
        verification=verification,
    )


# ── 注册表 ─────────────────────────────────────────────────────────────

_BINDING_GL_TYPES = frozenset({"float", "int", "vec2", "vec3"})


class MslEffectRegistry:
    """MSL helper、内置条目与验证记录的进程级容器。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._helpers: Dict[str, MslHelper] = {}
        self._entries: Dict[str, MslEffectEntry] = {}
        self._plugin_records: Dict[str, VerificationRecord] = {}

    # ── helper ──
    def register_helper(self, helper: MslHelper) -> None:
        """登记一个 helper；模式不可解析或绑定不自洽时显式拒绝。"""
        try:
            pattern = parse_tglsl_glsl(helper.glsl_pattern)
        except TglslFrontError as exc:
            raise ValueError(f"helper {helper.helper_id!r} 的 glsl_pattern 不可解析：{exc.issue.format()}") from exc
        entries = [fn for fn in pattern.functions if fn.name == ENTRY_NAME]
        if len(entries) != 1:
            raise ValueError(f"helper {helper.helper_id!r} 的 glsl_pattern 必须恰好定义一个 process 入口")
        signature = {p.name for p in entries[0].params}
        for name, glsl_type in zip(helper.glsl_param_bindings, helper.param_binding_types):
            if glsl_type not in _BINDING_GL_TYPES:
                raise ValueError(f"helper {helper.helper_id!r} 的绑定参数 {name!r} 类型 {glsl_type!r} 不受支持")
            appears = re.search(rf"(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])", helper.glsl_pattern)
            if appears is None or name in signature:
                raise ValueError(
                    f"helper {helper.helper_id!r} 的绑定参数 {name!r} 必须作为独立标识符出现在模式中，"
                    "且不能是 process 的形参")
        with self._lock:
            self._helpers[helper.helper_id] = helper

    def helpers(self) -> Tuple[MslHelper, ...]:
        with self._lock:
            return tuple(self._helpers[h] for h in sorted(self._helpers))

    def helper(self, helper_id: str) -> Optional[MslHelper]:
        with self._lock:
            return self._helpers.get(helper_id)

    # ── 内置条目 ──
    def register_entry(self, entry: MslEffectEntry) -> None:
        with self._lock:
            if entry.helper_id not in self._helpers:
                raise ValueError(
                    f"条目 {entry.effect_id!r} 依赖的 helper {entry.helper_id!r} 尚未登记（先 register_helper）")
            self._entries[entry.effect_id] = entry

    def entry(self, effect_id: str) -> Optional[MslEffectEntry]:
        with self._lock:
            return self._entries.get(effect_id)

    def entries(self) -> Tuple[MslEffectEntry, ...]:
        with self._lock:
            return tuple(self._entries[k] for k in sorted(self._entries))

    # ── 插件验证记录 ──
    def record_plugin_verification(self, plugin_id: str, record: VerificationRecord) -> None:
        with self._lock:
            self._plugin_records[plugin_id] = record

    def plugin_verification(self, plugin_id: str) -> Optional[VerificationRecord]:
        with self._lock:
            return self._plugin_records.get(plugin_id)

    def verified_plugins(self) -> Tuple[str, ...]:
        with self._lock:
            return tuple(sorted(k for k, r in self._plugin_records.items() if r.status == STATUS_VERIFIED))

    def to_dict(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "helpers": [{"helper_id": h.helper_id, "version": h.version, "msl_name": h.msl_name}
                            for h in self.helpers()],
                "entries": [e.to_dict() for e in self.entries()],
                "plugin_verification": {k: r.to_dict() for k, r in sorted(self._plugin_records.items())},
            }


def _default_registry() -> MslEffectRegistry:
    # 验证记录是纯数据模块，延迟导入以便测试替换后 reset 重建。
    from src.features.shared.effects.msl_plugin_verification import (  # noqa: E402 — 延迟导入避免环
        EXPOSURE_VERIFICATION,
        PLUGIN_VERIFICATION_RECORDS,
    )

    registry = MslEffectRegistry()
    registry.register_helper(EXPOSURE_HELPER)
    registry.register_entry(_build_exposure_entry(EXPOSURE_VERIFICATION))
    for plugin_id, record in PLUGIN_VERIFICATION_RECORDS.items():
        registry.record_plugin_verification(plugin_id, record)
    return registry


_instance: Optional[MslEffectRegistry] = None
_instance_lock = threading.Lock()


def get_msl_effect_registry() -> MslEffectRegistry:
    """进程级单例（延迟构建；测试可通过 :func:`reset_msl_effect_registry` 重建）。"""
    global _instance
    if _instance is None:
        with _instance_lock:
            if _instance is None:
                _instance = _default_registry()
    return _instance


def reset_msl_effect_registry() -> None:
    """丢弃单例（热重载 / 测试用）。"""
    global _instance
    with _instance_lock:
        _instance = None
