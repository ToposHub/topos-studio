"""Smart Bin 规则引擎（P1-1）——嵌套 AND/OR、比较操作符、日期区间。

规则树存于 ``SmartBin.filter_params["rules"]``（v2 schema，
``rule_schema_version: 2``），与既有平铺 filter_params 键（P1-12）共存：
有 ``rules`` 键走规则树求值，否则走平铺路径——旧工程零迁移。

规则树结构::

    {
      "op": "and" | "or",          # 组内组合方式
      "rules": [                    # 叶子或子组，可任意嵌套
        {"field": "rating", "cmp": ">=", "value": 3},
        {"field": "fps", "cmp": "==", "value": 25.0},
        {"field": "created_at", "cmp": "between", "value": [start, end]},
        {"op": "or", "rules": [ ... ]},
      ]
    }

语义约定（诚实匹配，不产生假阳性）：
- 字段值缺失（None）：一切比较均为 False（含 ``!=``）；``exists`` 操作符
  显式判断有无（``{"field": "fps", "cmp": "exists", "value": true}``）。
- 媒体类型不适用的字段（如图片无 fps）按缺失处理，不会错误匹配。
- ``contains``：字符串子串（大小写不敏感）或关键词集合包含。
- ``between``：[下界, 上界] 闭区间（数值/时间戳）。
- ``in``：值属于候选集合。

规则能力清单（``smart_bin_rule_capabilities``）供前端规则编辑器渲染
控件；UI 不应硬编码字段列表。
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Mapping, Optional

from src.features.edit.core.project import MediaAsset

__all__ = [
    "RULE_SCHEMA_VERSION",
    "RULES_KEY",
    "SCHEMA_VERSION_KEY",
    "GROUP_OPS",
    "COMPARATORS",
    "SmartBinRuleError",
    "RuleFieldCapability",
    "smart_bin_rule_capabilities",
    "validate_rule_tree",
    "matches_rule_tree",
    "evaluate_smart_bin_rules",
    "has_rule_tree",
    "rule_tree_from_filter_params",
]

RULE_SCHEMA_VERSION = 2
RULES_KEY = "rules"
SCHEMA_VERSION_KEY = "rule_schema_version"
GROUP_OPS = ("and", "or")
COMPARATORS = ("==", "!=", ">", ">=", "<", "<=", "contains",
               "between", "in", "exists")


class SmartBinRuleError(ValueError):
    """规则树结构不合法（保存前校验，fail-loud）。"""


# ================================================================
# 字段定义与能力清单
# ================================================================

@dataclass(frozen=True)
class RuleFieldCapability:
    """单个规则字段的能力声明（前端规则编辑器渲染依据）。"""

    field: str
    label: str
    value_type: str            # "number" | "string" | "enum" | "boolean" | "timestamp" | "string_list"
    ops: tuple[str, ...]
    applies_to: tuple[str, ...] = ()   # 空 = 全部媒体类型；否则限定
    enum_values: tuple[str, ...] = ()  # value_type=enum 时的合法值


def _asset_field_value(asset: MediaAsset, field: str) -> tuple[bool, Any]:
    """字段提取。返回 (有值, 值)；不适用/缺失 → (False, None)。

    技术字段取自探测 metadata（未知为 None）；日志字段取自 MediaAsset
    列字段；媒体类型不适用按缺失处理。
    """
    media = asset.media_type
    log_fields = {
        "rating": lambda a: a.rating,
        "flag": lambda a: a.flag or None,
        "color_label": lambda a: a.color_label or None,
        "comment": lambda a: a.comment or None,
        "keywords": lambda a: tuple(a.keywords) or None,
    }
    if field in log_fields:
        value = log_fields[field](asset)
        return (value is not None, value)

    if field == "media_type":
        return (True, media)
    if field == "path":
        return (bool(asset.path), asset.path)
    if field == "created_at":
        return (asset.created_at is not None, asset.created_at)

    # 技术字段：静态图无帧域概念（不会错误匹配 fps/total_frames）
    frame_fields = {"fps", "total_frames", "duration_seconds"}
    if field in frame_fields and media == "image":
        return (False, None)

    metadata = asset.metadata or {}
    if field == "width":
        value = asset.width
    elif field == "height":
        value = asset.height
    elif field == "codec":
        value = asset.codec or None
    elif field == "bit_depth":
        value = metadata.get("bit_depth")
    elif field == "audio_channels":
        info = metadata.get("audio_info")
        value = info.get("channels") if isinstance(info, dict) else None
    elif field == "has_audio":
        info = metadata.get("audio_info")
        value = bool(info) if isinstance(info, dict) else None
        return (value is not None, value)
    elif field == "is_hdr":
        value = metadata.get("is_hdr")
        return (value is not None, bool(value))
    elif field in ("fps", "total_frames", "duration_seconds"):
        value = getattr(asset, field, None)
    else:
        raise SmartBinRuleError(f"未知规则字段: {field}")
    if value is None or (isinstance(value, bool) and field not in ("has_audio", "is_hdr")):
        return (False, None)
    return (True, value)


#: 规则能力清单（前端渲染控件的单一定义点）
_RULE_FIELDS: tuple[RuleFieldCapability, ...] = (
    RuleFieldCapability("media_type", "Media Type", "enum", ("==", "!="),
                         enum_values=("video", "audio", "image", "image_sequence",
                                      "subtitle", "model")),
    RuleFieldCapability("path", "File Path", "string", ("contains", "==", "!=")),
    RuleFieldCapability("width", "Width", "number",
                         ("==", "!=", ">", ">=", "<", "<=", "between", "exists")),
    RuleFieldCapability("height", "Height", "number",
                         ("==", "!=", ">", ">=", "<", "<=", "between", "exists")),
    RuleFieldCapability("fps", "FPS", "number",
                         ("==", "!=", ">", ">=", "<", "<=", "between", "exists"),
                         applies_to=("video", "image_sequence")),
    RuleFieldCapability("duration_seconds", "Duration (s)", "number",
                         ("==", "!=", ">", ">=", "<", "<=", "between", "exists"),
                         applies_to=("video", "audio", "image_sequence")),
    RuleFieldCapability("total_frames", "Total Frames", "number",
                         ("==", "!=", ">", ">=", "<", "<=", "between", "exists"),
                         applies_to=("video", "image_sequence")),
    RuleFieldCapability("bit_depth", "Bit Depth", "number", ("==", ">=", "<=")),
    RuleFieldCapability("codec", "Codec", "string", ("==", "!=", "contains")),
    RuleFieldCapability("audio_channels", "Audio Channels", "number",
                         ("==", "!=", ">=", "<=")),
    RuleFieldCapability("has_audio", "Has Audio", "boolean", ("==",)),
    RuleFieldCapability("is_hdr", "HDR", "boolean", ("==",)),
    RuleFieldCapability("created_at", "Imported At", "timestamp",
                         (">", ">=", "<", "<=", "between")),
    RuleFieldCapability("keywords", "Keywords", "string_list", ("contains", "in")),
    RuleFieldCapability("rating", "Rating", "number", ("==", "!=", ">=", "<=")),
    RuleFieldCapability("flag", "Flag", "enum", ("==", "!="),
                         enum_values=("none", "reject")),
    RuleFieldCapability("color_label", "Clip Color", "enum", ("==", "!=")),
    RuleFieldCapability("comment", "Comment", "string", ("contains",)),
)


def smart_bin_rule_capabilities() -> tuple[RuleFieldCapability, ...]:
    """规则能力清单（前端 Smart Bin 编辑器渲染依据，勿在 UI 硬编码）。"""
    return _RULE_FIELDS


# ================================================================
# 校验
# ================================================================

_FIELD_NAME_TO_CAP = {cap.field: cap for cap in _RULE_FIELDS}


def validate_rule_tree(node: Any, _path: str = "rules") -> None:
    """递归校验规则树结构（保存前调用；非法即抛 SmartBinRuleError）。"""
    if not isinstance(node, Mapping):
        raise SmartBinRuleError(f"{_path}: 规则节点必须是 dict")
    op = node.get("op")
    if op is not None and op not in GROUP_OPS:
        raise SmartBinRuleError(f"{_path}: 未知组合操作符 {op!r}（可用: and/or）")
    if op in GROUP_OPS:
        children = node.get(RULES_KEY)
        if not isinstance(children, (list, tuple)) or not children:
            raise SmartBinRuleError(f"{_path}: 组节点 '{op}' 需要非空 rules 列表")
        for index, child in enumerate(children):
            validate_rule_tree(child, f"{_path}[{index}]")
        return
    # 叶子
    field = node.get("field")
    if not isinstance(field, str) or field not in _FIELD_NAME_TO_CAP:
        raise SmartBinRuleError(f"{_path}: 未知规则字段 {field!r}")
    cmp_op = node.get("cmp")
    cap = _FIELD_NAME_TO_CAP[field]
    if cmp_op not in cap.ops:
        raise SmartBinRuleError(
            f"{_path}: 字段 {field} 不支持操作符 {cmp_op!r}（可用: {cap.ops}）")
    value = node.get("value")
    if cmp_op == "between":
        if (not isinstance(value, (list, tuple)) or len(value) != 2
                or not all(isinstance(v, (int, float)) and not isinstance(v, bool)
                           for v in value)):
            raise SmartBinRuleError(f"{_path}: between 需要 [下界, 上界] 数字对")
    elif cmp_op == "exists":
        if not isinstance(value, bool):
            raise SmartBinRuleError(f"{_path}: exists 需要 boolean 值")
    elif cmp_op == "in":
        if not isinstance(value, (list, tuple)) or not value:
            raise SmartBinRuleError(f"{_path}: in 需要非空候选列表")
    elif value is None:
        raise SmartBinRuleError(f"{_path}: 操作符 {cmp_op} 需要 value")


def has_rule_tree(filter_params: Optional[Mapping[str, Any]]) -> bool:
    """filter_params 是否携带 v2 规则树。"""
    if not isinstance(filter_params, Mapping):
        return False
    return isinstance(filter_params.get(RULES_KEY), Mapping)


def rule_tree_from_filter_params(
    filter_params: Optional[Mapping[str, Any]],
) -> Optional[Mapping[str, Any]]:
    """取出规则树（无树返回 None）。"""
    if not has_rule_tree(filter_params):
        return None
    tree = filter_params.get(RULES_KEY)
    return tree if isinstance(tree, Mapping) else None


# ================================================================
# 求值
# ================================================================

def _as_number(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _match_leaf(asset: MediaAsset, node: Mapping[str, Any]) -> bool:
    field = node["field"]
    cmp_op = node["cmp"]
    value = node.get("value")
    exists, actual = _asset_field_value(asset, field)

    if cmp_op == "exists":
        return exists is bool(value)

    # 缺失值不匹配任何比较（诚实语义：不产生假阳性）
    if not exists or actual is None:
        return False

    if cmp_op == "contains":
        if isinstance(actual, tuple):
            return any(str(value).casefold() in str(k).casefold() for k in actual)
        return str(value).casefold() in str(actual).casefold()
    if cmp_op == "in":
        return actual in value or str(actual).casefold() in {
            str(v).casefold() for v in value}
    if cmp_op == "between":
        low, high = value
        number = _as_number(actual)
        return number is not None and float(low) <= number <= float(high)

    number = _as_number(actual)
    target = _as_number(value)
    if number is None or target is None:
        # 枚举/字符串相等族
        if cmp_op == "==":
            return str(actual).casefold() == str(value).casefold()
        if cmp_op == "!=":
            return str(actual).casefold() != str(value).casefold()
        return False
    if cmp_op == "==":
        return abs(number - target) < 1e-9
    if cmp_op == "!=":
        return abs(number - target) >= 1e-9
    if cmp_op == ">":
        return number > target
    if cmp_op == ">=":
        return number >= target
    if cmp_op == "<":
        return number < target
    if cmp_op == "<=":
        return number <= target
    return False


def matches_rule_tree(asset: MediaAsset, tree: Mapping[str, Any]) -> bool:
    """求值（假设已 validate；未校验输入的叶子字段会 fail-loud 抛错）。"""
    op = tree.get("op")
    if op in GROUP_OPS:
        results = (matches_rule_tree(asset, child) for child in tree[RULES_KEY])
        return all(results) if op == "and" else any(results)
    return _match_leaf(asset, tree)


def evaluate_smart_bin_rules(
    assets: list[MediaAsset],
    filter_params: Mapping[str, Any],
) -> list[MediaAsset]:
    """SmartBin 规则树求值入口（校验一次后逐素材匹配）。"""
    tree = rule_tree_from_filter_params(filter_params)
    if tree is None:
        return list(assets)
    validate_rule_tree(tree)
    return [asset for asset in assets if matches_rule_tree(asset, tree)]
