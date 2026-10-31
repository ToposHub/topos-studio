"""P26 彩色套印配方（L18 批次：源配方 → 组件载荷转换）。

依赖：F13 print_registration（暂存组件
content_staging/library_v1/filters/print_registration/，status=component_ready，
尚未接 L05 白名单/L10–L12 宿主接缝）。载荷为暂存组件参数载荷
（pack_id + component_id + slug + kind + params），**不是已可应用的宿主载荷**；
参数名/类型/范围按 component_manifest.json 权威强转，未知参数名 raise（防悬空引用）。
"""

from __future__ import annotations

import sys
from pathlib import Path

_DIR = Path(__file__).resolve().parent
if str(_DIR.parents[3]) not in sys.path:
    sys.path.insert(0, str(_DIR.parents[3]))

from content_staging.library_v1.presets._shared_convert import (
    component_payload,
    load_recipe,
)

COMPONENT_ID = "F13"
COMPONENT_SLUG = "print_registration"
RECIPE = load_recipe(_DIR / "recipe.json")

def to_component_payload() -> dict:
    """暂存组件参数载荷（组件接入宿主前的转换形态）。"""
    return component_payload(RECIPE)
