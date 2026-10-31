"""C15 median —— F19 median_rank 共享核再导出（单一事实源）。

C15 复用 F19 的小核中值/百分位算法（L00 台账 C15 行）。**禁双份漂移**：
本模块只再导出 references/median_rank_ref.py 的 evaluate/DEFAULTS，
节点参数表与 F19 oracle DEFAULTS 一致；语义/手算向量见
filters/median_rank/hand_vectors.json 与 test_f19。
"""

from __future__ import annotations

from content_staging.library_v1.references.median_rank_ref import (
    DEFAULTS, evaluate,
)

SOURCE_REF = "median_rank_ref"

__all__ = ["DEFAULTS", "evaluate", "SOURCE_REF"]
