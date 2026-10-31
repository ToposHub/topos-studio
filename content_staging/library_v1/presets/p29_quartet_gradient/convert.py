"""P29 四色渐变配方（L 计划 L05 首个滤镜类真预设：源配方 → 宿主载荷转换）。

源配方 manifest（recipe.json）不冒充宿主格式（specs/spec_schema.md §6）；
本模块把它转换为两类宿主载荷：
- Edit：clip EffectInstance 的 params dict（effect_type=plugin:LIB1_Gradient_Map）
- Color：.topos 层的 plugins 实例条目（layer["plugins"] 形态：
  {"instance_id", "plugin_id", "params", "enabled"}，layer.py:566）
应用走既有命令/入口（AddEffectCommand / 层插件实例机制），版本/内容 hash
由 M07.1 plugin_identity 链在应用时快照——本模块只做无副作用的载荷构造。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

_DIR = Path(__file__).resolve().parent
PLUGIN_ID = "LIB1_Gradient_Map"


def load_recipe() -> Dict[str, Any]:
    """读取源配方 manifest（参数即 LIB1_Gradient_Map 的 @param 语义）。"""
    return json.loads((_DIR / "recipe.json").read_text(encoding="utf-8"))


def to_edit_effect_params(recipe: Dict[str, Any] | None = None) -> Dict[str, Any]:
    """Edit clip 效果载荷：EffectInstance.params（param 名= @param var_name，
    select 用 label 字符串——catalog ParamDef 口径）。"""
    data = recipe if recipe is not None else load_recipe()
    params: Dict[str, Any] = {}
    for key, value in data["params"].items():
        if key in {"stop_0", "stop_1", "stop_2", "stop_3"}:
            params[key] = tuple(float(v) for v in value)
        elif key == "interp_mode":
            params[key] = str(value)
        else:
            params[key] = float(value)
    return params


def to_color_layer_plugin(instance_id: str,
                          recipe: Dict[str, Any] | None = None) -> Dict[str, Any]:
    """Color 层插件实例载荷（layer["plugins"] 列表元素形态）。"""
    data = recipe if recipe is not None else load_recipe()
    return {
        "instance_id": str(instance_id),
        "plugin_id": PLUGIN_ID,
        "params": {k: (list(v) if isinstance(v, (list, tuple)) else v)
                   for k, v in to_edit_effect_params(data).items()},
        "enabled": True,
    }
