"""X06 noise_dissolve CPU 核（暂存组件入口）。

L13 集成起本模块**再导出**生产实现（src/features/edit/transition_packs/
library_v1/cores.py）——组件测试与生产行为同一实现，禁双份漂移。
语义修正记录/推导/手算向量见本目录 README.md 与 hand_vectors.json。
"""

from __future__ import annotations

from typing import Any, Mapping

from numpy import ndarray

from src.features.edit.transition_packs.library_v1.cores import (
    NOISE_DISSOLVE_DEFAULTS as DEFAULTS,
    noise_dissolve_mask,
    noise_lattice_hash_u32,
)


# 暂存期公开名（测试/文档引用）：生产实现见 cores.noise_lattice_hash_u32
lattice_hash_u32 = noise_lattice_hash_u32


def evaluate_mask(width: int, height: int, progress: float,
                  params: Mapping[str, Any] | None = None) -> ndarray:
    return noise_dissolve_mask(width, height, progress, params)
