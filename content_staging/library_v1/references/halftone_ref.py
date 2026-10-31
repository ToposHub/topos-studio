"""C06 halftone —— F13 print_registration 共享核再导出（单一事实源）。

C06 复用 F13 的网点/套印算法（L00 台账 C06 行：无 halftone 节点，
复用 Halftone_Print 几何 + F13 套印差量）。**禁双份漂移**：本模块只
再导出 references/print_registration_ref.py 的 evaluate/DEFAULTS，
节点参数表与 F13 oracle DEFAULTS 一致；语义/手算向量见
filters/print_registration/hand_vectors.json 与 test_f13。
"""

from __future__ import annotations

from content_staging.library_v1.references.print_registration_ref import (
    DEFAULTS, evaluate,
)

SOURCE_REF = "print_registration_ref"

__all__ = ["DEFAULTS", "evaluate", "SOURCE_REF"]
