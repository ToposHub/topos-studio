"""FX 版本兼容、未知数据保留与能力声明（R01，复审计划 2026-11-15）。

背景（复审报告 §5 R01）：
- ``audio_dsp.ALGORITHM_VERSION`` 已到 3，但工程/预设里的 ``algorithm_version``
  只写不读——777 的预设存盘后会变成 3（无损往返被破坏）。
- 普通轨遇到未知 ``effect_type`` 时 ``TrackEffectInstance.__post_init__``
  直接抛 ValueError，导致整个 EditTrack/TimelineSequence 反序列化失败；
  Master 侧的 try/except 则把整条链降级为 None（已知实例也被丢弃）。
- v3 改变了 EQ 的声音（真 N 阶 Butterworth、Shelf Q、增益-Q 耦合），
  旧工程默认加载后 HPF 稳态 RMS 相差可达 ~24dB，没有任何兼容路径。

本模块定义的契约（同一字段和时序契约只定义一次，R05/R06 直接复用）：

1. **schema_version**：FX 序列化结构版本，写在序列级
   （``TimelineSequence.to_dict`` 的 ``audio_fx_schema_version``）。
   v1 = 无版本键的历史工程；v2 = 实例携带 ``algorithm_version``、
   未知实例以 opaque payload 原样保留。
2. **algorithm_version**：单个效果实例创建/最后编辑时的 DSP 算法版本。
   0 = 未标注（早于版本标注的工程；era 有历史歧义）。DSP 按
   ``algorithm_compat_bucket(version, effect_type)`` 选择系数路径（H01
   起逐效果精确分发：EQ v≤1/v2/v3+ 三桶、Delay v≤3/v4 两桶）；> 当前
   版本的未来数据原样保留，渲染按当前算法尽力并在诊断中标记 degraded。
3. **能力声明**：``effect_type_support`` / ``collect_chain_diagnostics``
   供预览与导出 preflight 使用同一判断——supported / degraded /
   unsupported 三态及原因，不静默吞掉。

迁移策略：不自动改写旧工程的 algorithm_version；显式升级走 command
（``upgrade_chain_algorithm_version`` 生成新链，可 undo），保存后可重开。
"""

from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

#: FX 序列化 schema 版本（序列级键 ``audio_fx_schema_version``）。
#: v1：历史工程（无版本键）。v2：实例带 algorithm_version + 未知类型保留。
AUDIO_FX_SCHEMA_VERSION = 2

#: ``algorithm_version == 0`` 的语义：未标注（早于版本标注的工程数据，
#: era 跨 43dfb2e3..1016a3cd，子时代不可由标注区分）→ 按最旧可复现
#: 语义渲染并在诊断中说明；显式升级走 command。
UNSPECIFIED_ALGORITHM_VERSION = 0

#: legacy 兼容桶的默认边界（未列入逐效果表的效果）：v ≤ 2 = 302b88b6
#: 定型的 v2 语义（这些效果的最后一次改声在 v2 定义内）；v0/v1 数据
#: 同按 v2 语义渲染——pre-302b88b6 子时代声音不承诺复现，诊断说明。
LEGACY_ALGORITHM_MAX = 2

#: H01 逐效果版本边界（真历史提交核对，见
#: docs/audit/2026-11-15-audio-fx-post-integration/h01/baseline-historical.json）：
#: - strip_eq：v≤1 = v1 legacy 级联；v2 = Butterworth 无 EQ-02（302b88b6）；
#:   v3 = +EQ-02 耦合。EQ-02 代码曾于 76ad5657 在 v2 印章期内落地，但
#:   版本化声明（e2131e65 changelog v3）把 EQ-02 归为 v3 变更——按声明边界。
#: - delay：v≤3 = 直接读头切换；v4 = 15ms 双读头交叉淡化（R07）。
#: 值 = 该效果「精确历史渲染桶」的最大版本；诊断用它生成 reason。
EFFECT_VERSION_BOUNDARIES: dict[str, int] = {
    "strip_eq": 2,   # v2 有独立精确渲染路径（区别于 v≤1 legacy）
    "delay": 3,      # v≤3 直接切换（legacy 渲染路径）
    "convolution_reverb": 4,
    "true_peak_limiter": 4,
    "chorus_flanger": 4,
    "phaser": 4,
}

#: schema 变更记录（新版本追加条目，不回填改写历史条目）。
FX_SCHEMA_CHANGELOG: dict[int, str] = {
    1: "legacy：无版本键；algorithm_version 只写不读，未知 effect_type 抛错。",
    2: (
        "实例携带 algorithm_version（0=未标注→legacy 桶）；未知 effect_type "
        "以 opaque payload 保留并诊断，不再抛错/清链；序列写 audio_fx_schema_version。"
    ),
}


def algorithm_compat_bucket(algorithm_version: Any,
                            effect_type: Optional[str] = None) -> str:
    """把实例的 algorithm_version 映射到 DSP 兼容桶（H01 逐效果边界）。

    Args:
        algorithm_version: 实例标注的算法版本（0 = 未标注）。
        effect_type: 可选效果类型；未给出的调用维持旧全局语义。

    Returns:
        ``"legacy"``（该效果精确历史渲染桶内：EQ v≤1 / Delay v≤3 /
        其余 v≤2 或未标注）/ ``"v2"``（仅 strip_eq v2：Butterworth 无
        EQ-02，302b88b6 精确语义）/ ``"current"`` / ``"future"``（> 本地
        版本：数据保留，渲染按当前算法降级并诊断）。
    """
    from src.features.edit.core.audio_dsp import ALGORITHM_VERSION

    try:
        v = int(algorithm_version or 0)
    except (TypeError, ValueError):
        v = 0
    cur = int(ALGORITHM_VERSION)
    if v > cur:
        return "future"
    if effect_type == "strip_eq":
        if v <= 1:
            return "legacy"
        if v == 2:
            return "v2"
        return "current"
    if effect_type == "delay":
        return "legacy" if v <= 3 else "current"
    if effect_type in {"convolution_reverb", "true_peak_limiter",
                       "chorus_flanger", "phaser"}:
        return "legacy" if v <= 4 else "current"
    return "legacy" if v <= LEGACY_ALGORITHM_MAX else "current"


def _legacy_bucket_reason(effect_type: str, v: int) -> str:
    """legacy/v2 桶的诊断说明（渲染语义 + 历史边界，不静默）。"""
    if effect_type in {"convolution_reverb", "true_peak_limiter",
                       "chorus_flanger", "phaser"}:
        warning = {
            "convolution_reverb": "historical convolution timing and IR mapping",
            "true_peak_limiter": "historical detector/linking; true-peak ceiling is not guaranteed",
            "chorus_flanger": "historical modulation read-head timing",
            "phaser": "historical block-local feedback",
        }[effect_type]
        return (f"v{v} preserves pre-v5 PCM: {warning}; "
                "explicitly upgrade the instance to v5 for corrected DSP")
    if effect_type == "strip_eq":
        if v <= 1:
            if v == 0:
                return ("unstamped pre-versioning data: rendered with oldest "
                        "reproducible (v1) EQ coefficients; era is ambiguous "
                        "across 43dfb2e3..1016a3cd")
            return "v1 EQ coefficients (cascaded Q=0.7071 cuts, S=1 shelves)"
        return ("v2 EQ (302b88b6): Butterworth cuts + Q shelves, no EQ-02 "
                "gain-Q coupling (EQ-02 is a v3 change)")
    if effect_type == "delay":
        if v == 0:
            return ("unstamped pre-versioning data: rendered with v2-era "
                    "delay; direct read-head switch (crossfade is v4)")
        return (f"v{v} delay: direct read-head switch on delay-time change "
                "(15ms crossfade is a v4 change)")
    if v == 0:
        return ("unstamped pre-versioning data: era ambiguous; rendered with "
                "v2-era (302b88b6) algorithm")
    return f"v{v} data: rendered with v2-era (302b88b6) algorithm"


def _current_algorithm_version() -> int:
    from src.features.edit.core.audio_dsp import ALGORITHM_VERSION

    return int(ALGORITHM_VERSION)


def effect_type_support(effect_type: str) -> tuple[str, str]:
    """效果类型能力三态判定（预览与导出 preflight 共用）。

    Returns:
        (status, reason)。status ∈ {"supported", "degraded", "unsupported"}：
        - supported：参数 registry + DSP 后端类均存在。
        - degraded：参数 registry 存在但 DSP 后端缺失（预览/导出会无声跳过）。
        - unsupported：未知类型（数据保留为占位，不参与渲染）。
    """
    if not effect_type:
        return "unsupported", "empty effect_type"
    try:
        from src.features.edit.core.timeline_v2 import _ensure_effect_param_registry
        from src.features.edit.core.audio_dsp import _ensure_effect_registry

        if effect_type not in _ensure_effect_param_registry():
            return ("unsupported",
                    f"unknown effect_type {effect_type!r} (kept as opaque payload)")
        if effect_type not in _ensure_effect_registry():
            return ("degraded",
                    f"effect_type {effect_type!r} has no DSP backend "
                    "(silently skipped in preview/export)")
    except Exception as exc:  # pragma: no cover - 防御：registry 初始化失败
        return "degraded", f"registry unavailable: {exc!r}"
    return "supported", ""


def collect_chain_diagnostics(chain: Any) -> list[dict[str, Any]]:
    """对一条 EffectChainParams 收集逐实例诊断（版本 + 能力）。

    诊断面向导出 preflight / 健康面板；不做任何修改。`chain` 也接受
    TrackEffectInstance 元组（Bus/Master 复用）。

    H01：future 版本不再伪装 supported——status=degraded 且 reason 说明
    数据保留/按当前算法渲染；legacy/v2 桶给出渲染语义说明。
    """
    effects = getattr(chain, "effects", None)
    if effects is None and isinstance(chain, (tuple, list)):
        effects = chain
    out: list[dict[str, Any]] = []
    for e in effects or ():
        etype = getattr(e, "effect_type", "")
        status, reason = effect_type_support(etype)
        if etype == "vst3_hosted" and getattr(e, "enabled", True):
            from src.features.edit.core.hosted_vst3 import params_issue

            params = getattr(e, "params", {})
            if params.get("enabled", True) and (issue := params_issue(params)):
                status, reason = "unsupported", issue
        v = int(getattr(e, "algorithm_version", 0) or 0)
        bucket = algorithm_compat_bucket(v, effect_type=etype)
        if bucket == "future":
            status = "degraded"
            reason = (f"algorithm_version {v} > local "
                      f"{_current_algorithm_version()}: payload preserved; "
                      "rendered with current algorithm (sound may differ "
                      "from authoring)")
        elif bucket in ("legacy", "v2") and status == "supported":
            reason = _legacy_bucket_reason(etype, v)
        out.append({
            "instance_id": getattr(e, "instance_id", ""),
            "effect_type": etype,
            "algorithm_version": v,
            "compat_bucket": bucket,
            "status": status,
            "reason": reason,
        })
    return out


def chain_has_diagnostics(chain: Any) -> bool:
    """链内是否存在非 supported 实例（preflight 快速判断）。"""
    return any(d["status"] != "supported" for d in collect_chain_diagnostics(chain))


def upgrade_chain_algorithm_version(chain: Any,
                                    target: Optional[int] = None) -> Any:
    """生成 algorithm_version 升级到 target 的链副本（显式升级，供 command 使用）。

    - 只改 ``algorithm_version`` 与实例 extras 中的诊断元数据，不动声音参数；
      升级后的实例按当前算法渲染（声音可能改变——这正是"显式升级"的语义）。
    - 未来版本（> 当前）实例不升级，保持原样（不把未来数据伪装成本地版本）。
    - 返回新 EffectChainParams；调用方通过 SetTrackEffectChain 等命令提交，
      天然支持 undo / 保存重开。
    """
    from dataclasses import replace
    from src.features.edit.core.timeline_v2 import EffectChainParams

    if target is None:
        target = _current_algorithm_version()
    target = int(target)
    upgraded = []
    for e in chain.effects:
        v = int(getattr(e, "algorithm_version", 0) or 0)
        if v <= _current_algorithm_version():
            upgraded.append(replace(e, algorithm_version=target))
        else:
            upgraded.append(e)
    return EffectChainParams(effects=tuple(upgraded))
