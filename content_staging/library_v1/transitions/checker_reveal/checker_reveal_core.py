"""X03 checker_reveal CPU 核（暂存组件入口）。

L13 集成起本模块**再导出**生产实现（src/features/edit/transition_packs/
library_v1/cores.py）——组件测试与生产行为同一实现，禁双份漂移。
语义修正记录/推导/手算向量见本目录 README.md 与 hand_vectors.json。
"""

from __future__ import annotations

from typing import Any, Mapping

from numpy import ndarray

from src.features.edit.transition_packs.library_v1.cores import (
    CHECKER_DEFAULTS as DEFAULTS,
    checker_reveal_mask,
)


def evaluate_mask(width: int, height: int, progress: float,
                  params: Mapping[str, Any] | None = None) -> ndarray:
    return checker_reveal_mask(width, height, progress, params)
