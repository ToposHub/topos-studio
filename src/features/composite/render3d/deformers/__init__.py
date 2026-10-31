"""Deformer package — F13 非破坏变形器（纯 numpy，无 GL/PyQt）。

公开接口（与 ``__all__`` 一一对应，AUD-8710）：
    DeformerSpec / DeformerStack / DeformerResult — 数据结构（base.py）
    evaluate_deformer — CPU 变形分派（cpu.py）

类型常量（已实现的变形器类型）：
    DEFORMER_BEND / TWIST / BULGE / TAPER / STRETCH / NOISE

注意：
    - Falloff 控制（fields.py 的 FieldSpec / linear_field / radial_field /
      box_field）不属本包导出——需要时从 ``render3d.fields`` import。
    - ``DEFORMER_SHEAR`` / ``DEFORMER_SPHERIFY`` 仍在 base.py 定义（占位类型
      名），但 cpu.py 无实现、evaluate_deformer 对其抛 ValueError
      （AUD-8703），故不再从本包导出（导出即承诺、承诺不兑现）。

参考 master_plan §16 / §21.16。
"""

from .base import (
    DeformerSpec,
    DeformerStack,
    DeformerResult,
    DEFORMER_BEND,
    DEFORMER_TWIST,
    DEFORMER_BULGE,
    DEFORMER_TAPER,
    DEFORMER_STRETCH,
    DEFORMER_NOISE,
)
from .cpu import evaluate_deformer

__all__ = [
    "DeformerSpec",
    "DeformerStack",
    "DeformerResult",
    "evaluate_deformer",
    "DEFORMER_BEND",
    "DEFORMER_TWIST",
    "DEFORMER_BULGE",
    "DEFORMER_TAPER",
    "DEFORMER_STRETCH",
    "DEFORMER_NOISE",
]
