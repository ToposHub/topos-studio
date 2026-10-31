"""X01 clock_wipe CPU 核（暂存组件入口）。

L03 起本模块**再导出**生产实现（src/features/edit/transition_packs/
library_v1/cores.py）——组件测试与生产行为同一实现，禁双份漂移。
几何推导/手算向量见本目录 README.md 与 hand_vectors.json。
"""

from __future__ import annotations

from typing import Any, Mapping

from numpy import ndarray

from src.features.edit.transition_packs.library_v1.cores import (
    CLOCK_WIPE_DEFAULTS as DEFAULTS,
    clock_wipe_mask,
)


def evaluate_mask(width: int, height: int, progress: float,
                  params: Mapping[str, Any] | None = None) -> ndarray:
    return clock_wipe_mask(width, height, progress, params)
