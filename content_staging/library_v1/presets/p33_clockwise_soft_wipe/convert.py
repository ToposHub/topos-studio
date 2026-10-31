"""P33 顺时钟轻擦配方（L18 批次：源配方 → 转场载荷转换）。

依赖：X01 clock_wipe——生产 base lib1_clock_wipe（transition_catalog 已注册，
生产核 src/features/edit/transition_packs/library_v1/cores.py；暂存组件
content_staging/library_v1/transitions/clock_wipe/）。载荷两类：
- to_transition_instance：TransitionInstance(transition_type, params) 直填形态
  （生产 catalog 类型 + 生产核参数域，测试逐项校验并真实构造实例）；
- to_user_transition_json：base 在 custom_transitions._BASE_TYPES 白名单
  （L05 口径），可产出用户转场声明 JSON。
"""

from __future__ import annotations

import sys
from pathlib import Path

_DIR = Path(__file__).resolve().parent
if str(_DIR.parents[3]) not in sys.path:
    sys.path.insert(0, str(_DIR.parents[3]))

from content_staging.library_v1.presets._shared_convert import (
    load_recipe,
    transition_instance,
    user_transition_json,
)

TRANSITION_TYPE = "lib1_clock_wipe"
COMPONENT_ID = "X01"
RECIPE = load_recipe(_DIR / "recipe.json")

def to_transition_instance() -> dict:
    """Edit 转场实例载荷（TransitionInstance 直填形态）。"""
    return transition_instance(RECIPE)


def to_user_transition_json(user_id: str, label: str | None = None) -> dict:
    """用户转场声明 JSON（base 须在 custom_transitions._BASE_TYPES 白名单）。"""
    return user_transition_json(RECIPE, user_id, label)
