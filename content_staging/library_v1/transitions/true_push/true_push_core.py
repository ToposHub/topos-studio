"""X09 true_push CPU 核（暂存组件入口）。

L03 起本模块**再导出**生产实现（src/features/edit/transition_packs/
library_v1/cores.py::push_frame）；float32 多通道接口语义与试点 README
一致（双线性、端点比特级、恒界内采样）。
"""

from __future__ import annotations

from typing import Any, Mapping

import numpy as np
from numpy import ndarray

from src.features.edit.transition_packs.library_v1.cores import (
    PUSH_DEFAULTS as DEFAULTS,
    PUSH_DIRECTIONS as _DIRS,
    push_frame,
)

_DIRS_SET = frozenset(_DIRS)


def evaluate(a_img: ndarray, b_img: ndarray, progress: float,
             params: Mapping[str, Any] | None = None) -> ndarray:
    p = dict(DEFAULTS)
    if params:
        p.update(params)
    direction = str(p.get("direction", "left"))
    if direction not in _DIRS_SET:
        raise ValueError(f"direction 必须是 {sorted(_DIRS_SET)}，收到 {direction!r}")
    if np.asarray(a_img).shape != np.asarray(b_img).shape:
        raise ValueError(
            f"双源尺寸不一致: {np.asarray(a_img).shape} vs {np.asarray(b_img).shape}")
    return push_frame(a_img, b_img, progress, direction,
                      float(p.get("seam_softness", 0.0)))
