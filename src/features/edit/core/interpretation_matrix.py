"""M04.3 · 解释组合的消费者支持矩阵（按真实消费者定义，不静默）。

旋转/SAR/隔行/帧率四个解释维度的**声明域 vs 消费状态**分开登记：

- ``applied``：真实消费者已应用（fps 覆盖 → 帧率解释链：媒体池/时间
  线插入/重锚命令）；
- ``declared_unapplied``：字段可声明（探测/覆盖/持久化往返），渲染侧
  当前**未应用校正**（按探测原样渲染）——矩阵明示，不装作已生效。

矩阵是纯数据（UI/诊断/预检消费）；消费状态变化时同步本表，禁止凭
感觉改（与 delivery_matrix 同纪律）。
"""

from __future__ import annotations

from typing import Any, Dict

_APPLIED = "applied"
_DECLARED_UNAPPLIED = "declared_unapplied"

#: 维度 → 消费者状态矩阵（consumer → status）
_DIMENSION_CONSUMERS: Dict[str, Dict[str, str]] = {
    "fps": {
        "media_pool_insert": _APPLIED,      # 新插入走生效值
        "timeline_view": _APPLIED,          # 帧率显示/解释
        "reanchor_command": _APPLIED,       # UpdateClipFpsInterpretationCommand
    },
    "rotation_deg": {
        "preview_render": _DECLARED_UNAPPLIED,   # 按探测原样，未旋转校正
        "export_render": _DECLARED_UNAPPLIED,
    },
    "sar": {
        "preview_render": _DECLARED_UNAPPLIED,   # 像素宽高比未参与缩放
        "export_render": _DECLARED_UNAPPLIED,
    },
    "field_order": {
        "preview_render": _DECLARED_UNAPPLIED,   # 未去隔行/场序处理
        "export_render": _DECLARED_UNAPPLIED,
    },
}


def consumer_support_matrix() -> Dict[str, Dict[str, str]]:
    """全量矩阵快照（诊断/预检消费；副本）。"""
    return {dim: dict(consumers)
            for dim, consumers in _DIMENSION_CONSUMERS.items()}


def unapplied_dimensions(override: Any) -> list:
    """该覆盖中声明了但当前渲染侧未应用的维度（预检明示清单）。

    覆盖为空/None → 空列表。仅含 override 实际设置了值的维度——
    用户设置了才提示，不泛化告警。
    """
    if override is None:
        return []
    return [
        dim for dim in ("rotation_deg", "sar", "field_order")
        if getattr(override, dim, None) is not None
    ]


def dimension_status(dimension: str, consumer: str) -> str:
    """单点查询（未知维度/消费者 → declared_unapplied 保守值）。"""
    return _DIMENSION_CONSUMERS.get(dimension, {}).get(
        consumer, _DECLARED_UNAPPLIED)


__all__ = [
    "consumer_support_matrix",
    "dimension_status",
    "unapplied_dimensions",
]
