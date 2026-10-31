"""P56 柔影合页配方（L18 批次：源配方 → 转场载荷转换）。

依赖：X10 hinged_doors——生产 base lib1_hinged_doors（transition_catalog 已注册，
生产核 resample_cores/x10_hinged_doors.py；暂存组件 content_staging/library_v1/transitions/hinged_doors/）。
载荷一类：
- to_transition_instance：TransitionInstance(transition_type, params) 直填形态
  （生产 catalog 类型 + 生产核参数域，测试逐项校验并真实构造实例）。

白名单纪律：base lib1_hinged_doors 不在 custom_transitions._BASE_TYPES（当前仅
lib1_clock_wipe / lib1_true_push），不产出用户转场 JSON，也不提供该转换——
未接入 = 不可声明（防悬空引用，L05 口径）。
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
)

TRANSITION_TYPE = "lib1_hinged_doors"
COMPONENT_ID = "X10"
RECIPE = load_recipe(_DIR / "recipe.json")

def to_transition_instance() -> dict:
    """Edit 转场实例载荷（TransitionInstance 直填形态）。"""
    return transition_instance(RECIPE)
