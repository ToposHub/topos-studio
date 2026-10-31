"""P65 双列画中画宏配方（L18 批次：源配方 → 合成宏模板转换）。

依赖：C14 tile_array——暂存组件 content_staging/library_v1/nodes/tile_array/（manifest status=component_ready；未注册生产 NODE_REGISTRY，属 L17 在途批次）；辅助生产节点 transform（源适配）。载荷三类：
- to_node_params_snapshot：主节点参数快照（node_type + parameters 子集）；
- to_macro_payload：暂存级宏图模板（slots 显式插槽 + steps + connections
  + output_step；主步骤参数 = 暂存组件 manifest DEFAULTS 叠加配方 params）；
- 图导入（节点 ID/连接/表达式重映射为宿主 Composite 图）是 L05 转换入口的
  事——本模块只做无副作用构造（spec_schema.md §6，不冒充宿主图 JSON）。

能力如实：status=component_ready（暂存，未接 L05 白名单/宿主图导入未接线）。
"""

from __future__ import annotations

import sys
from pathlib import Path

_DIR = Path(__file__).resolve().parent
if str(_DIR.parents[3]) not in sys.path:
    sys.path.insert(0, str(_DIR.parents[3]))

from content_staging.library_v1.presets._shared_convert import (
    load_recipe,
    macro_payload,
    node_params_snapshot,
)

NODE_TYPE = "lib1_tile_array"
COMPONENT_ID = "C14"
RECIPE = load_recipe(_DIR / "recipe.json")

# 宏图模板（step 为本地 id；外部素材一律走显式插槽，不引用作者机器路径）。
GRAPH = {'slots': [{'slot': 'source', 'kind': 'image', 'description': '阵列单元源'}], 'steps': [{'step': 'fit', 'node_type': 'transform'}, {'step': 'array', 'node_type': 'lib1_tile_array'}], 'connections': [{'from_step': 'fit', 'from_port': 'result', 'to_step': 'array', 'to_port': 'image'}], 'output_step': 'array'}

def to_node_params_snapshot() -> dict:
    """主节点参数快照载荷（node_type + parameters）。"""
    return node_params_snapshot(RECIPE)


def to_macro_payload() -> dict:
    """暂存级宏图模板载荷（slots/steps/connections/output_step）。"""
    return macro_payload(RECIPE, GRAPH)
