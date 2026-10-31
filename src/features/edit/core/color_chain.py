"""F4.1 · 调色链解析单点——组前 → 片段主栈 → 片段微调 → 组后。

三个渲染消费点（Edit 预览 GPU/CPU、成片导出 CPU、Metal 导出 pass2）
全部经本模块取链，防止各路对"组处理顺序"的漂移（调研确认的现状：
preview/export 两份 `_get_*_color_grade` 靠约定保持一致，链语义必须
单点化）。

顺序语义（计划 F4.1"组前/片段/组后"）：
1. ``pre_group`` —— 组前处理（组 look 的前段）；
2. clip 主栈（``color_stack_id``，含共享栈语义）；
3. clip 微调栈（``color_trim_stack_id``，F4.1 新增）——各镜头微调独立
   于组栈存在：改组 look 不触碰 trim，改 trim 不影响组内其他镜头；
4. ``post_group`` —— 组后处理。

变换不重复：组栈/微调栈只含调色参数；IDT/ODT 是管线级语义（F5.1
收口），不进链。链应用 = 逐段顺序执行（非线性参数无代数合并，顺序
应用即精确组合）。
"""

from __future__ import annotations

import logging
from typing import Any, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

#: 与 _get_*_color_grade 的默认判定同源的标量/色轮零值集合
_IDENTITY_SCALARS = (
    "exposure", "temp", "tint", "contrast", "shadow", "highlights",
    "saturation", "vibrance", "mid_detail", "sharpness", "hue_shift",
)
_IDENTITY_WHEELS = ("lift", "gamma", "gain")


def _params_active(params: dict) -> bool:
    """基础调色参数是否偏离恒等（与快照 basic_active 判定同口径）。"""
    for name in _IDENTITY_SCALARS:
        try:
            if abs(float(params.get(name, 0.0) or 0.0)) > 1e-6:
                return True
        except (TypeError, ValueError):
            continue
    for name in _IDENTITY_WHEELS:
        wheel = params.get(name, (0.0, 0.0, 0.0))
        try:
            if any(abs(float(v)) > 1e-6 for v in wheel):
                return True
        except (TypeError, ValueError):
            continue
    return False


def resolve_color_chain(
    project: Any,
    timeline_id: str,
    clip: Any,
) -> Tuple[Optional[Any], Optional[Any], Optional[Any]]:
    """解析 clip 的组前/微调/组后三段栈（主栈由调用方按现状解析）。

    Returns:
        (pre_group_stack, trim_stack, post_group_stack)；无组/无微调时
        对应段为 None。组按 (timeline_id, clip_id) 成员唯一归属查找。
    """
    clip = clip if clip is not None else None
    trim_id = getattr(clip, "color_trim_stack_id", None) if clip else None
    trim_stack = None
    if trim_id:
        trim_stack = (getattr(project, "color_stacks", {}) or {}).get(trim_id)

    pre_stack = post_stack = None
    if project is not None and clip is not None:
        from src.features.edit.core.color_group import group_for_clip
        group = group_for_clip(
            project, timeline_id or "", getattr(clip, "clip_id", ""))
        if group is not None:
            stacks = getattr(project, "color_stacks", {}) or {}
            if group.pre_group_stack_id:
                pre_stack = stacks.get(group.pre_group_stack_id)
            if group.post_group_stack_id:
                post_stack = stacks.get(group.post_group_stack_id)
    return pre_stack, trim_stack, post_stack


def chain_extra_params(
    project: Any,
    timeline_id: str,
    clip: Any,
) -> List[dict]:
    """主栈之外的链段参数（pre → trim → post 顺序，只含有效段）。

    M05（S08）：每段携带 ``_chain_segment`` 标签（"pre"/"trim"/"post"）。
    此前是无标签位置列表——消费方把整列应用在主栈**之后**（实际
    main→pre→trim→post，正确顺序是 pre→main→trim→post；非线性参数
    非交换，顺序错误即像素错误）。消费方按标签把 pre 段放主栈之前、
    trim/post 段放主栈之后。

    只透传**基础调色参数**（与 GPU primary grade 快照同域）；链段栈若
    含 curves/LUT 等高级操作，属未支持组合——调用方不静默丢弃，本
    函数在返回列表对应位置放入带 ``_unsupported_advanced`` 标记的项，
    由渲染端决定降级路径（与 has_advanced_operations 同语义）。
    """
    extras: List[dict] = []
    pre, trim, post = resolve_color_chain(project, timeline_id, clip)
    for segment, stack in (
            ("pre", pre), ("trim", trim), ("post", post)):
        if stack is None:
            continue
        entry = dict(getattr(stack, "grading_params", {}) or {})
        entry["_chain_segment"] = segment
        if bool(getattr(stack, "curves_data", None)) \
                or bool(getattr(stack, "hsl_curve_data", None)) \
                or bool(getattr(stack, "mixer_params", None)) \
                or getattr(stack, "lut_path", None) \
                or bool((getattr(stack, "soft_clip_params", {}) or {}).get(
                    "enabled", False)):
            entry["_unsupported_advanced"] = True
        # N03：层内高级字段标记补齐——layer 快照（权威状态 dict）携带
        # 的 LUT/曲线/qualifier/插件键此前完全不标记，`extras_active`
        # 可为 False 令整段静默失效（审计 §2 N03 首条）。
        for layer in (getattr(stack, "layers", None) or ()):
            if not isinstance(layer, dict):
                continue
            layer_advanced = (
                layer.get("lut_path") or layer.get("active_lut_path")
                or layer.get("curves") or layer.get("curves_data")
                or layer.get("qualifier_params") or layer.get("qualifier")
                or layer.get("plugins") or layer.get("plugin"))
            if layer_advanced:
                entry["_unsupported_advanced"] = True
                break
        extras.append(entry)
    return extras


def chain_for_stack(project: Any, stack_id: str) -> List[dict]:
    """M05 · Composite 链段消费生产侧：按主栈 id 定位所属 clip 解析链段。

    经 ``color_versions._iter_clips`` 找到引用该主栈的首个 clip 并返回
    其 ``chain_extra_params``（带 ``_chain_segment`` 标签；共享栈多 clip
    引用时组链按组表统一，归属一致取首个）。无归属（栈不在任何时间线
    clip 上——独立/嵌入栈）/解析失败返回空列表——**不静默应用错链**，
    Composite 侧按无链段处理（与主栈独立的既有行为一致）。
    """
    if not stack_id or project is None:
        return []
    try:
        from src.features.edit.core.color_versions import _iter_clips
        for timeline_id, clip in _iter_clips(project):
            if str(getattr(clip, "color_stack_id", "") or "") == str(stack_id):
                return chain_extra_params(project, timeline_id, clip)
    except Exception:  # noqa: BLE001 — 解析失败按无链段（消费方显式 debug）
        logger.exception("chain_for_stack resolution failed for %s", stack_id)
    return []


def chain_stack_ids(
    project: Any,
    timeline_id: str,
    clip: Any,
) -> Tuple[str, ...]:
    """该 clip 链上全部组/微调栈 id（render_cache 指纹输入）。

    主栈 id 由指纹函数既有路径收集；本函数只补链段（漏收即"绿缓存"，
    见 build_render_cache_fingerprint 的注释）。
    """
    ids: List[str] = []
    pre, trim, post = resolve_color_chain(project, timeline_id, clip)
    for stack in (pre, trim, post):
        if stack is not None:
            ids.append(str(getattr(stack, "stack_id", "")))
    return tuple(i for i in ids if i)


def extras_active(extras: Sequence[dict]) -> bool:
    """链段是否存在需要渲染的动作（参数非恒等或含高级标记）。"""
    for entry in extras:
        if entry.get("_unsupported_advanced") or _params_active(entry):
            return True
    return False


__all__ = [
    "resolve_color_chain",
    "chain_extra_params",
    "chain_for_stack",
    "chain_stack_ids",
    "extras_active",
]
