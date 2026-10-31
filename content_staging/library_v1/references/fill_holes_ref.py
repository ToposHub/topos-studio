"""fill_holes —— 独立 CPU 数学参考（oracle 入口）。

L17 集成起本模块**再导出**生产实现（src/features/composite/evaluators/
lib1_fullframe_compute.py）——组件测试与生产行为同一实现，禁双份漂移。
"""

from __future__ import annotations

from src.features.composite.evaluators.lib1_fullframe_compute import (  # noqa: F401
    FILL_HOLES_DEFAULTS as DEFAULTS,
    CONNECTIVITIES,
    OUTPUT_MODES,
    fill_holes_evaluate as evaluate,
)
