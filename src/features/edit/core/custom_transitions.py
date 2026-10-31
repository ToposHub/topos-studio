"""C2: 用户自定义转场（参数化模板实例，阶段 1）。

用户在 ``Presets/User/transitions/*.json`` 声明转场变体：

    {
      "id": "my_wipe",
      "label": "我的斜擦",
      "base": "wipe",
      "params": {"direction": "left", "softness": 0.2}
    }

- ``base`` 必须是内置可参数化转场（wipe / slide_* / iris_* / dip_to_color）；
- ``params`` 与实例级 params 合并（实例优先）传给既有参数化渲染入口
  （``compute_transition_mask`` / ``compute_wipe_mask``）；
- 坏 JSON / 缺字段 / 重复 id / 未知 base 跳过并记日志，绝不阻断启动。

注册后类型 id 为 ``user_<id>``：``resolve_user_transition`` 反查
(base, params)，``is_spatial_transition`` 透传 base 的空间属性。
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Optional

logger = logging.getLogger(__name__)

__all__ = [
    "USER_TRANSITION_PREFIX",
    "CustomTransition",
    "reload_user_transitions",
    "user_transition_registry",
    "resolve_user_transition",
    "user_transitions_dir",
    "user_to_type_id",
]

USER_TRANSITION_PREFIX = "user_"

#: 允许作为 base 的内置可参数化转场
_BASE_TYPES = frozenset({
    "wipe",
    "slide_left", "slide_right", "slide_up", "slide_down",
    "iris_circle", "iris_ellipse", "iris_hexagon", "iris_plus",
    "iris_square", "iris_triangle", "iris_arrow", "iris_star", "iris_heart",
    "dip_to_color",
    # LIB1（计划 L / L05）：X01 时钟极角 / X09 双源平移（L03 注册）。
    # 两者参数均为数值白名单可校验（_safe_float 家族），可作配方 base。
    "lib1_clock_wipe",
    "lib1_true_push",
})

_SPATIAL_BASES = frozenset({
    "wipe",
    "slide_left", "slide_right", "slide_up", "slide_down",
    "iris_circle", "iris_ellipse", "iris_hexagon", "iris_plus",
    "iris_square", "iris_triangle", "iris_arrow", "iris_star", "iris_heart",
    # LIB1：两类型都属双源合成家族（user_* 继承该判定进入 GPU 门）；
    # 实际合成方式（mask vs 双源重采样）由 base 分派决定。
    "lib1_clock_wipe",
    "lib1_true_push",
})


@dataclass(frozen=True)
class CustomTransition:
    """一个用户声明的转场变体。"""

    type_id: str
    label: str
    base: str
    params: Dict[str, Any] = field(default_factory=dict)
    spatial: bool = True

    def resolve_params(
        self, instance_params: Mapping[str, Any] | None,
    ) -> Dict[str, Any]:
        """定义级 params 与实例级 params 合并（实例优先）。"""
        merged: Dict[str, Any] = dict(self.params)
        if instance_params:
            merged.update(instance_params)
        return merged


_REGISTRY: Dict[str, CustomTransition] = {}


def user_transitions_dir() -> str:
    """用户转场声明目录（project-root Presets 约定）。"""
    return os.path.join(os.getcwd(), "Presets", "User", "transitions")


def user_to_type_id(user_id: str) -> str:
    return f"{USER_TRANSITION_PREFIX}{user_id}"


def user_transition_registry() -> Dict[str, CustomTransition]:
    """当前注册表（只读视图；启动时与 reload 后填充）。"""
    return dict(_REGISTRY)


def resolve_user_transition(
    type_id: str,
) -> Optional[tuple[CustomTransition, str]]:
    """反查用户转场 → (定义, base 类型)；非用户类型返回 None。"""
    if not type_id.startswith(USER_TRANSITION_PREFIX):
        return None
    entry = _REGISTRY.get(type_id)
    if entry is None:
        return None
    return entry, entry.base


def reload_user_transitions() -> Dict[str, CustomTransition]:
    """扫描声明目录并重建注册表（应用启动 / 目录变化时调用）。"""
    _REGISTRY.clear()
    directory = user_transitions_dir()
    try:
        filenames = sorted(os.listdir(directory))
    except OSError:
        return _REGISTRY
    for filename in filenames:
        if not filename.endswith(".json"):
            continue
        path = os.path.join(directory, filename)
        try:
            with open(path, encoding="utf-8") as handle:
                data = json.load(handle)
            if not isinstance(data, dict):
                raise ValueError("not a JSON object")
        except (OSError, ValueError) as exc:
            logger.warning("Skipping bad user transition %s: %s", path, exc)
            continue

        user_id = str(data.get("id") or "").strip()
        label = str(data.get("label") or "").strip()
        base = str(data.get("base") or "").strip()
        params = data.get("params") or {}
        if not user_id or not label or not base:
            logger.warning(
                "Skipping incomplete user transition %s (id/label/base required)",
                path,
            )
            continue
        if base not in _BASE_TYPES:
            logger.warning(
                "Skipping user transition %s: unknown base %r", path, base)
            continue
        if not isinstance(params, dict):
            logger.warning(
                "Skipping user transition %s: params must be an object", path)
            continue
        type_id = user_to_type_id(user_id)
        if type_id in _REGISTRY:
            logger.warning(
                "Duplicate user transition id %r (%s) skipped", user_id, path)
            continue
        _REGISTRY[type_id] = CustomTransition(
            type_id=type_id,
            label=label,
            base=base,
            params=dict(params),
            spatial=base in _SPATIAL_BASES,
        )
        logger.debug("Loaded user transition %r → base %s", type_id, base)
    return _REGISTRY


# ── 渲染/UI 查询钩子 ──

def is_user_transition_spatial(type_id: str) -> Optional[bool]:
    """用户转场的空间属性；非用户类型返回 None。"""
    resolved = resolve_user_transition(type_id)
    if resolved is None:
        return None
    return resolved[0].spatial


def merge_user_transition_params(
    type_id: str, instance_params: Mapping[str, Any] | None,
) -> Optional[tuple[str, Dict[str, Any]]]:
    """用户转场 → (base, 合并后 params)；非用户类型返回 None。"""
    resolved = resolve_user_transition(type_id)
    if resolved is None:
        return None
    entry, base = resolved
    return base, entry.resolve_params(instance_params)

# 启动时懒加载一次（目录缺失/为空安全）
reload_user_transitions()

