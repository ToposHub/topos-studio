"""M03.4 · 同步诊断持久化（模型层）。

F2.6 的同步对齐诊断（置信度/歧义/拒绝原因）此前只存在于计算当刻——
重开工程后"为什么这个绑定被接受/拒绝"无从追溯。本模块定义可持久化
诊断记录（随工程 JSON/包往返），表挂在
``EditProject.sync_diagnostics``（{asset_key: record dict}）。

记录为**审计事实**：拒绝的同步也持久化（rejection 非空）——下次打开
能看到失败原因，而不是只有成功绑定留下的痕迹。
"""

from __future__ import annotations

from dataclasses import dataclass, replace as _dc_replace
from typing import Any, Dict, Optional

_VALID_METHODS = ("waveform", "timecode", "manual")


@dataclass(frozen=True)
class SyncDiagnosticRecord:
    """一次同步对齐的诊断快照（不可变）。"""

    asset_key: str            # 被同步素材 id（多机位对可含 "a|b" 对键）
    method: str = "manual"    # waveform | timecode | manual
    offset_seconds: float = 0.0
    confidence: float = 0.0   # 负值 = 反相标记（F2.6）
    ambiguity: float = 0.0    # 次峰/主峰相关比（0..1）
    rejection: str = ""       # "" = 接受写回；否则 alignment_rejection 原因
    computed_at: float = 0.0  # epoch 秒

    def to_dict(self) -> Dict[str, Any]:
        return {
            "asset_key": self.asset_key,
            "method": self.method,
            "offset_seconds": self.offset_seconds,
            "confidence": self.confidence,
            "ambiguity": self.ambiguity,
            "rejection": self.rejection,
            "computed_at": self.computed_at,
        }

    @staticmethod
    def from_dict(data: Dict[str, Any]) -> Optional["SyncDiagnosticRecord"]:
        """形状校验解析（非法载荷返回 None——调用方丢弃该条不崩）。"""
        if not isinstance(data, dict):
            return None
        try:
            record = SyncDiagnosticRecord(
                asset_key=str(data.get("asset_key", "")),
                method=str(data.get("method", "manual")),
                offset_seconds=float(data.get("offset_seconds", 0.0) or 0.0),
                confidence=float(data.get("confidence", 0.0) or 0.0),
                ambiguity=float(data.get("ambiguity", 0.0) or 0.0),
                rejection=str(data.get("rejection", "") or ""),
                computed_at=float(data.get("computed_at", 0.0) or 0.0),
            )
        except (TypeError, ValueError):
            return None
        if not record.asset_key or record.method not in _VALID_METHODS:
            return None
        if record.offset_seconds != record.offset_seconds or \
                abs(record.offset_seconds) > 24 * 3600.0:
            return None
        return record


def record_sync_diagnostic(project: Any,
                           record: SyncDiagnosticRecord) -> Any:
    """把诊断记录进工程的 sync_diagnostics 表（不可变 replace）。

    同键覆盖（每素材/对键保留最新一条诊断——历史滚动单槽）。
    """
    table = dict(getattr(project, "sync_diagnostics", None) or {})
    table[record.asset_key] = record.to_dict()
    return _dc_replace(project, sync_diagnostics=table)


def sync_diagnostic_for(project: Any, asset_key: str) \
        -> Optional[SyncDiagnosticRecord]:
    """读回该素材/对键的最新诊断（无记录/载荷损坏 = None）。"""
    raw = (getattr(project, "sync_diagnostics", None) or {}).get(asset_key)
    return SyncDiagnosticRecord.from_dict(raw) if raw is not None else None


def sanitize_sync_diagnostics(raw: Any) -> Dict[str, Any]:
    """表级反序列化：逐条形状校验，非法条目丢弃（旧工程无键 = 空表）。"""
    if not isinstance(raw, dict):
        return {}
    out: Dict[str, Any] = {}
    for key, value in raw.items():
        record = SyncDiagnosticRecord.from_dict(value)
        if record is not None:
            out[str(key)] = record.to_dict()
    return out


__all__ = [
    "SyncDiagnosticRecord",
    "record_sync_diagnostic",
    "sync_diagnostic_for",
    "sanitize_sync_diagnostics",
]
