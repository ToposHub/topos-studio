"""P30 海报量化配方（L18 批次：源配方 → 节点参数快照转换）。

依赖：F18 posterize_shared——复用生产节点插件 posterize
（plugins/node_plugins/posterize_plugin.py，NodePluginManager 可发现、
create_node 可实例化），不另造第二个 Posterize 实现（F18 差量纪律）。
载荷为 Composite 节点参数快照（node_type + parameters 子集），节点级
真实 schema 校验见 tests/content_library_v1/test_l18_p01_p40.py。
"""

from __future__ import annotations

import sys
from pathlib import Path

_DIR = Path(__file__).resolve().parent
if str(_DIR.parents[3]) not in sys.path:
    sys.path.insert(0, str(_DIR.parents[3]))

from content_staging.library_v1.presets._shared_convert import (
    load_recipe,
    node_params_snapshot,
)

NODE_TYPE = "posterize"
COMPONENT_ID = "F18"
RECIPE = load_recipe(_DIR / "recipe.json")

def to_node_params_snapshot() -> dict:
    """Composite 节点参数快照载荷（node_type + parameters）。"""
    return node_params_snapshot(RECIPE)
