"""曲线面板四参数 Soft Clip 与 Edit 强度的正式模型（权威参考：移动版）。

语义裁决（计划 docs/plans/2026-11-01-curves-ui/ 任务 1）以移动版
``topos_edit_mobile`` 为准——桌面/移动共用同一套参数名、范围、默认值与
算法，是"移动工程可在桌面打开"的基础：

- 参数（每通道 R/G/B 独立 + 链接）：``low_clip``（默认 0，[-1, min(1, high)]）、
  ``low_soft``（默认 0）、``high_clip``（默认 1，[max(0, low), 2]）、
  ``high_soft``（默认 0）；两个膝宽互斥共享 ``gap = max(high - low, 0)``。
- 算法 = 指数软膝（膝内斜率从 1 无缝衔接、渐近衰减到 0）；默认值即恒等
  映射，超白/负 RGB 原样保留给后续节点。GLSL/MSL/CPU 端逐式对齐本模块
  的 :func:`apply_soft_clip_channel`，修改时必须三端同步。
- 旧 8 参数 ``soft_clip_params``（threshold/shoulder 等）是独立的旧工程
  兼容路径，本模块不读不写、不做等价转换。
"""

from __future__ import annotations

import copy
import math
from typing import Any, Dict

SOFT_CLIP_PARAM_KEYS = ("low_clip", "low_soft", "high_clip", "high_soft")

# 每通道四参数默认值（恒等映射）。dict 而非常量共享：调用方一律经
# default_curves_soft_clip() 取全新副本，避免活对象与默认值互串。
_CHANNEL_DEFAULTS: Dict[str, float] = {
    "low_clip": 0.0,
    "low_soft": 0.0,
    "high_clip": 1.0,
    "high_soft": 0.0,
}

# 移动版工程 JSON 的 camelCase 键（fromJson 兼容口径），未来移动工程
# 导入桌面时直接复用本归一化。
_MOBILE_KEY_ALIASES = {
    "low_clip": ("low_clip", "lowClip"),
    "low_soft": ("low_soft", "lowSoft"),
    "high_clip": ("high_clip", "highClip"),
    "high_soft": ("high_soft", "highSoft"),
}

_CHANNEL_ALIASES = {"R": ("R", "red"), "G": ("G", "green"), "B": ("B", "blue")}


def default_curves_soft_clip() -> Dict[str, Any]:
    """返回全新的默认四参数 Soft Clip 状态（链接开启，恒等映射）。"""
    return {
        "linked": True,
        "channels": {ch: dict(_CHANNEL_DEFAULTS) for ch in "RGB"},
    }


def default_curves_edit() -> Dict[str, Any]:
    """返回全新的默认 Edit 强度状态（全通道 100 = 曲线全额生效）。"""
    return {
        "linked": True,
        "amounts": {"Y": 100.0, "R": 100.0, "G": 100.0, "B": 100.0},
    }


def _read_number(raw: Any, key: str, fallback: float) -> float:
    """从 raw dict 读取数值键（移动版 snake/camel 双口径），非法回退。"""
    if not isinstance(raw, dict):
        return fallback
    for name in _MOBILE_KEY_ALIASES[key]:
        value = raw.get(name)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            value = float(value)
            return value if math.isfinite(value) else fallback
    return fallback


def _normalize_channel(raw: Any) -> Dict[str, float]:
    """归一化单通道四参数（移动版 fromJson 的钳制顺序）。

    clip 点先互相约束（low ∈ [-1,1] 且 ≤ high；high ∈ [max(0,low), 2]），
    再把两个膝宽依次压进剩余 gap（low_soft 优先占位，high_soft ≤ 剩余）。
    非有限值回退默认——NaN 会污染钳制链。
    """
    low = min(max(_read_number(raw, "low_clip", 0.0), -1.0), 1.0)
    high = min(max(_read_number(raw, "high_clip", 1.0), max(0.0, low)), 2.0)
    low_soft = min(max(_read_number(raw, "low_soft", 0.0), 0.0), max(high - low, 0.0))
    high_soft = min(
        max(_read_number(raw, "high_soft", 0.0), 0.0),
        max(high - low - low_soft, 0.0),
    )
    return {
        "low_clip": low,
        "low_soft": low_soft,
        "high_clip": high,
        "high_soft": high_soft,
    }


def normalize_curves_soft_clip(data: Any) -> Dict[str, Any]:
    """把外部数据（工程 JSON / UI 草稿 / 移动版 payload）归一化为正式模型。

    未知通道名（red/green/blue 别名可识别）忽略；结构非法时逐项回退
    默认值。恒返回全新 dict，不与输入共享容器。
    """
    result = default_curves_soft_clip()
    if not isinstance(data, dict):
        return result
    linked = data.get("linked", True)
    result["linked"] = bool(linked) if isinstance(linked, bool) else True
    channels = data.get("channels")
    if not isinstance(channels, dict):
        return result
    for canonical, aliases in _CHANNEL_ALIASES.items():
        for alias in aliases:
            if alias in channels:
                result["channels"][canonical] = _normalize_channel(channels[alias])
                break
    return result


def normalize_curves_edit(data: Any) -> Dict[str, Any]:
    """归一化 Edit 强度状态（amounts 钳制到 0-100，非法回退 100）。"""
    result = default_curves_edit()
    if not isinstance(data, dict):
        return result
    linked = data.get("linked", True)
    result["linked"] = bool(linked) if isinstance(linked, bool) else True
    amounts = data.get("amounts")
    if not isinstance(amounts, dict):
        return result
    for channel in ("Y", "R", "G", "B"):
        value = amounts.get(channel, 100.0)
        if isinstance(value, (int, float)) and not isinstance(value, bool) \
                and math.isfinite(float(value)):
            result["amounts"][channel] = min(max(float(value), 0.0), 100.0)
    return result


def curves_soft_clip_is_identity(params: Any) -> bool:
    """是否处于恒等映射（默认值）——渲染管线据此跳过整个阶段。

    默认值直通是移动版明确的语义（保留超白/负值），identity 判定必须
    与逐通道默认值精确一致。
    """
    if not isinstance(params, dict):
        return True
    channels = params.get("channels")
    if not isinstance(channels, dict):
        return True
    for ch in "RGB":
        values = channels.get(ch)
        if not isinstance(values, dict):
            return False
        for key, default in _CHANNEL_DEFAULTS.items():
            value = values.get(key, default)
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                return False
            if float(value) != default:
                return False
    return True


def set_soft_clip_control(
    params: Dict[str, Any], channel: str, key: str, value: float,
) -> Dict[str, Any]:
    """对指定通道应用一个控件更新（移动版 ``withControl`` 约束的移植）。

    clip 点保持有序并约束在 [-1, 2]；两个膝宽互斥共享 gap，移动 clip 点
    时把越界膝宽重新压回。恒返回全新 dict（快照冻结契约：调用方拿到的
    状态与活对象脱钩）。

    Raises:
        ValueError: channel 不在 R/G/B 或 key 不是四参数之一。
    """
    if channel not in _CHANNEL_ALIASES:
        raise ValueError(f"未知 Soft Clip 通道: {channel!r}")
    if key not in SOFT_CLIP_PARAM_KEYS:
        raise ValueError(f"未知 Soft Clip 参数: {key!r}")
    if not isinstance(value, (int, float)) or isinstance(value, bool) \
            or not math.isfinite(float(value)):
        raise ValueError(f"Soft Clip 参数值非法: {value!r}")
    value = float(value)

    result = normalize_curves_soft_clip(params)
    values = dict(result["channels"][channel])
    gap = max(values["high_clip"] - values["low_clip"], 0.0)
    if key == "low_clip":
        values["low_clip"] = min(max(value, -1.0), min(1.0, values["high_clip"]))
        new_gap = max(values["high_clip"] - values["low_clip"], 0.0)
        values["low_soft"] = min(max(values["low_soft"], 0.0), new_gap)
        values["high_soft"] = min(
            max(values["high_soft"], 0.0), max(new_gap - values["low_soft"], 0.0))
    elif key == "low_soft":
        values["low_soft"] = min(max(value, 0.0), max(gap - values["high_soft"], 0.0))
    elif key == "high_clip":
        values["high_clip"] = min(max(value, max(0.0, values["low_clip"])), 2.0)
        new_gap = max(values["high_clip"] - values["low_clip"], 0.0)
        values["low_soft"] = min(max(values["low_soft"], 0.0), new_gap)
        values["high_soft"] = min(
            max(values["high_soft"], 0.0), max(new_gap - values["low_soft"], 0.0))
    else:  # high_soft
        values["high_soft"] = min(max(value, 0.0), max(gap - values["low_soft"], 0.0))
    result["channels"][channel] = values
    return result


def apply_soft_clip_channel(
    value: float,
    low_clip: float,
    low_soft: float,
    high_clip: float,
    high_soft: float,
) -> float:
    """单通道指数软膝映射——CPU 参考实现，GLSL/MSL 逐式对齐移动版。

    与移动版 ``SoftClipChannelParams.map`` / ``shaders/soft_clip.frag``
    的 ``apply_soft_clip_channel`` 保持同一数学；默认参数下严格恒等
    （含超白/负值直通）。膝宽按移动版口径先钳进 gap。
    """
    gap = max(high_clip - low_clip, 0.0)
    if gap == 0.0:
        return low_clip
    low_knee = min(max(low_soft, 0.0), gap)
    high_knee = min(max(high_soft, 0.0), max(gap - low_knee, 0.0))

    result = value
    if low_knee > 0.0:
        low_start = low_clip + low_knee
        if result < low_start:
            result = low_clip + low_knee * math.exp((result - low_start) / low_knee)
    elif low_clip != 0.0 and result < low_clip:
        result = low_clip

    if high_knee > 0.0:
        high_start = high_clip - high_knee
        if result > high_start:
            result = high_clip - high_knee * math.exp(-(result - high_start) / high_knee)
    elif high_clip != 1.0 and result > high_clip:
        result = high_clip
    return result


def sanitize_payload(payload: Any) -> Dict[str, Any]:
    """深拷贝一份可安全入档/入快照的模型状态（防御外部可变容器）。"""
    return copy.deepcopy(normalize_curves_soft_clip(payload))


__all__ = [
    "SOFT_CLIP_PARAM_KEYS",
    "apply_soft_clip_channel",
    "curves_soft_clip_is_identity",
    "default_curves_edit",
    "default_curves_soft_clip",
    "normalize_curves_edit",
    "normalize_curves_soft_clip",
    "set_soft_clip_control",
    "sanitize_payload",
]
