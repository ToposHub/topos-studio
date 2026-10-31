"""P20 霓虹扩散配方（L18 批次：源配方 → 宿主载荷转换）。

依赖：Soft_Glow_Bloom（生产插件 plugins/Soft_Glow_Bloom.tglsl，@version 1.0）。载荷形态与 P29 试点一致：
- Edit：clip EffectInstance.params（effect_type=plugin:Soft_Glow_Bloom，
  select 用 label 字符串、checkbox 用 bool——catalog ParamDef 口径）
- Color：.topos 层 plugins 实例条目（layer["plugins"] 四字段形态）
参数名/类型/范围由 tests/content_library_v1/test_l18_p01_p40.py 对真实插件
schema 校验；应用走既有 AddEffectCommand / 层插件实例机制，本模块无副作用。
"""

from __future__ import annotations

import sys
from pathlib import Path

_DIR = Path(__file__).resolve().parent
if str(_DIR.parents[3]) not in sys.path:
    sys.path.insert(0, str(_DIR.parents[3]))

from content_staging.library_v1.presets._shared_convert import (
    color_layer_plugin,
    edit_effect_params,
    load_recipe,
)

PLUGIN_ID = "Soft_Glow_Bloom"
RECIPE = load_recipe(_DIR / "recipe.json")

def to_edit_effect_params() -> dict:
    """Edit clip 效果载荷参数（EffectInstance.params）。"""
    return edit_effect_params(RECIPE)


def to_color_layer_plugin(instance_id: str) -> dict:
    """Color 层插件实例载荷（layer["plugins"] 列表元素）。"""
    return color_layer_plugin(RECIPE, instance_id)
