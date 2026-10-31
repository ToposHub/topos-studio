"""P58 去黑边前景宏配方（L18 批次：源配方 → 合成宏模板转换）。

依赖：C17 edge_extend——暂存组件 content_staging/library_v1/nodes/edge_extend/（manifest status=component_ready；lib1_edge_extend 已注册生产 NODE_REGISTRY）；辅助生产节点 premult（输出端预乘收口）。载荷三类：
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

NODE_TYPE = "lib1_edge_extend"
COMPONENT_ID = "C17"
RECIPE = load_recipe(_DIR / "recipe.json")

# 宏图模板（step 为本地 id；外部素材一律走显式插槽，不引用作者机器路径）。
GRAPH = {'slots': [{'slot': 'foreground', 'kind': 'image', 'description': '直通 alpha 的前景（去黑边对象）'}], 'steps': [{'step': 'extend', 'node_type': 'lib1_edge_extend'}, {'step': 'premult', 'node_type': 'premult'}], 'connections': [{'from_step': 'extend', 'from_port': 'image', 'to_step': 'premult', 'to_port': 'image'}], 'output_step': 'premult'}

def to_node_params_snapshot() -> dict:
    """主节点参数快照载荷（node_type + parameters）。"""
    return node_params_snapshot(RECIPE)


def to_macro_payload() -> dict:
    """暂存级宏图模板载荷（slots/steps/connections/output_step）。"""
    return macro_payload(RECIPE, GRAPH)
