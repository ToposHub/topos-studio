"""C20 halation —— F06 multiscale_halation 共享核再导出（单一事实源）。

C20 复用 F06 的高光扩散算法（L00 台账 C20 行：与普通 Glow 差量一致）。
GPU 单 pass 等价口径：本组件的 evaluate 别名指向 F06 的
``evaluate_degraded_single_scale``（单尺度 12-tap 黄金角环 + 逐 tap
软膝门控）——与暂存 .tglsl 草稿逐式同构；**真多尺度版（E 场提取 +
双尺度扩散的天然多 pass 结构）待原生 renderer 多 pass 接线后启用**
（README/manifest known_limits 声明）。**禁双份漂移**：不复制实现，
只再导出；节点参数表与 F06 oracle DEFAULTS 一致。
"""

from __future__ import annotations

from content_staging.library_v1.references.multiscale_halation_ref import (
    DEFAULTS, evaluate_degraded_single_scale, evaluate as evaluate_multiscale,
)

# C20 的 GPU 单 pass 等价入口（多 pass 版留 evaluate_multiscale 备查）
evaluate = evaluate_degraded_single_scale

SOURCE_REF = "multiscale_halation_ref"
SOURCE_ENTRY = "evaluate_degraded_single_scale"

__all__ = ["DEFAULTS", "evaluate", "evaluate_multiscale", "SOURCE_REF", "SOURCE_ENTRY"]
