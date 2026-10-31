"""C01 lib1_gradient_map 节点（暂存组件入口）。

L04 起本模块**再导出**生产实现（src/features/composite/nodes/lib1_nodes.py）
——组件测试与生产行为同一实现，禁双份漂移。dispatch/GLSL 已随 L04 接线
（base_evaluator lib1_ 分支 + shaders _LIB1_GRADIENT_MAP_FRAGMENT）。
"""

from __future__ import annotations

from src.features.composite.nodes.lib1_nodes import (
    INTERP_MODES, Lib1GradientMapNode,
)

__all__ = ["INTERP_MODES", "Lib1GradientMapNode"]
