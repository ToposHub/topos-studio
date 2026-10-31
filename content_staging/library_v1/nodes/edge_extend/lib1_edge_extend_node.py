"""C17 lib1_edge_extend 节点（暂存组件入口）。

L04 起本模块**再导出**生产实现（src/features/composite/nodes/lib1_nodes.py）
——组件测试与生产行为同一实现，禁双份漂移。
"""

from __future__ import annotations

from src.features.composite.nodes.lib1_nodes import (
    OUTPUT_MODES, Lib1EdgeExtendNode,
)

__all__ = ["OUTPUT_MODES", "Lib1EdgeExtendNode"]
