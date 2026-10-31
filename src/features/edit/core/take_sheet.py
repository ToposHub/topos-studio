"""ADR Take Sheet —— 录音 take 列表与最佳 take 标记（P1-10）。

专业 ADR 工作流的核心对象：一场录音会话产生的所有 take 汇总成一张
take sheet，支持标记最佳 take（同场次互斥）、按 asset 反查、以及
工程重开后的重建。

持久化策略：take sheet 不单独落盘，而是随 asset metadata 持久化——
导入 take 时把 take 字段写进 asset.metadata（见 edit_controller
``_on_take_recorded``），重开工程后用
:func:`build_take_sheet_from_project` 从素材元数据完整重建。
这保证 take sheet 与工程包（Collect/Archive）天然一致，不会出现
两张真值。

模块边界：纯核心，不依赖 Qt。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from src.features.edit.core.recording_manager import TakeInfo

logger = logging.getLogger(__name__)

#: asset.metadata 中标记"这是一个录音 take"的键
KEY_IS_RECORDING = "is_recording"
#: asset.metadata 中标记最佳 take 的键
KEY_IS_BEST = "is_best_take"


@dataclass
class TakeSheetEntry:
    """take sheet 中的一行：一个已完成的录音 take。"""

    take_id: str = ""
    take_number: int = 0
    name: str = ""
    file_path: str = ""
    asset_id: str = ""
    track_id: str = ""
    scene_name: str = ""
    talent_name: str = ""
    duration_seconds: float = 0.0
    peak_dbfs: float = float("-inf")
    status: str = ""
    start_timecode: str = ""
    time_reference_samples: int = 0
    in_point_frame: int = -1
    is_best: bool = False
    notes: str = ""
    created_at: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "take_id": self.take_id, "take_number": self.take_number,
            "name": self.name, "file_path": self.file_path,
            "asset_id": self.asset_id, "track_id": self.track_id,
            "scene_name": self.scene_name, "talent_name": self.talent_name,
            "duration_seconds": self.duration_seconds,
            "peak_dbfs": (None if self.peak_dbfs == float("-inf")
                          else float(self.peak_dbfs)),
            "status": self.status, "start_timecode": self.start_timecode,
            "time_reference_samples": self.time_reference_samples,
            "in_point_frame": self.in_point_frame,
            "is_best": self.is_best, "notes": self.notes,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TakeSheetEntry":
        peak = data.get("peak_dbfs")
        return cls(
            take_id=str(data.get("take_id", "")),
            take_number=int(data.get("take_number", 0) or 0),
            name=str(data.get("name", "")),
            file_path=str(data.get("file_path", "")),
            asset_id=str(data.get("asset_id", "")),
            track_id=str(data.get("track_id", "")),
            scene_name=str(data.get("scene_name", "")),
            talent_name=str(data.get("talent_name", "")),
            duration_seconds=float(data.get("duration_seconds", 0.0) or 0.0),
            peak_dbfs=(float("-inf") if peak is None else float(peak)),
            status=str(data.get("status", "")),
            start_timecode=str(data.get("start_timecode", "")),
            time_reference_samples=int(data.get("time_reference_samples", 0) or 0),
            in_point_frame=int(data.get("in_point_frame", -1)),
            is_best=bool(data.get("is_best", False)),
            notes=str(data.get("notes", "")),
            created_at=str(data.get("created_at", "")),
        )


class TakeSheet:
    """一场 ADR 会话的 take 列表（内存真值，持久化随 asset metadata）。

    最佳 take 标记按场次（scene_name）互斥：同一场次只有一个 ★。
    """

    def __init__(self) -> None:
        self._entries: List[TakeSheetEntry] = []

    # ── 查询 ──

    @property
    def entries(self) -> List[TakeSheetEntry]:
        return list(self._entries)

    def __len__(self) -> int:
        return len(self._entries)

    def entry_for_take(self, take_id: str) -> Optional[TakeSheetEntry]:
        for entry in self._entries:
            if entry.take_id == take_id:
                return entry
        return None

    def entry_for_asset(self, asset_id: str) -> Optional[TakeSheetEntry]:
        for entry in self._entries:
            if entry.asset_id == asset_id:
                return entry
        return None

    def best_take(self, scene_name: str = "") -> Optional[TakeSheetEntry]:
        """指定场次（空=全局）的最佳 take。"""
        for entry in self._entries:
            if not entry.is_best:
                continue
            if not scene_name or entry.scene_name == scene_name:
                return entry
        return None

    # ── 变更 ──

    def add_take(self, take: TakeInfo, asset_id: str = "",
                 track_id: str = "") -> TakeSheetEntry:
        """把一个完成的 take 追加进 sheet（take_id 幂等：已存在则更新）。"""
        existing = self.entry_for_take(take.take_id)
        if existing is not None:
            existing.asset_id = asset_id or existing.asset_id
            existing.status = take.status
            existing.duration_seconds = take.duration_seconds
            existing.peak_dbfs = take.peak_dbfs
            existing.start_timecode = take.start_timecode
            existing.time_reference_samples = take.time_reference_samples
            existing.in_point_frame = take.in_point_frame
            return existing
        entry = TakeSheetEntry(
            take_id=take.take_id,
            take_number=take.take_number,
            name=take.name,
            file_path=take.file_path,
            asset_id=asset_id,
            track_id=track_id,
            scene_name=take.scene_name,
            talent_name=take.talent_name,
            duration_seconds=take.duration_seconds,
            peak_dbfs=take.peak_dbfs,
            status=take.status,
            start_timecode=take.start_timecode,
            time_reference_samples=take.time_reference_samples,
            in_point_frame=take.in_point_frame,
            created_at=take.start_timestamp,
        )
        self._entries.append(entry)
        return entry

    def mark_best(self, take_id: str) -> bool:
        """标记最佳 take（同场次互斥）。返回是否找到该 take。"""
        target = self.entry_for_take(take_id)
        if target is None:
            return False
        for entry in self._entries:
            if entry.scene_name == target.scene_name:
                entry.is_best = entry.take_id == take_id
        return True

    def clear(self) -> None:
        self._entries = []

    # ── 序列化 ──

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema": "topos.adr_take_sheet.v1",
            "entries": [e.to_dict() for e in self._entries],
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TakeSheet":
        sheet = cls()
        for item in data.get("entries", ()):
            try:
                sheet._entries.append(TakeSheetEntry.from_dict(item))
            except (TypeError, ValueError) as e:
                logger.warning("Skip malformed take sheet entry: %s", e)
        return sheet


def take_metadata_for_asset(take: TakeInfo) -> Dict[str, Any]:
    """生成写入 asset.metadata 的 take 字段（导入时调用）。"""
    return {
        KEY_IS_RECORDING: True,
        "take_id": take.take_id,
        "take_number": take.take_number,
        "name": take.name,
        "scene": take.scene_name,
        "talent": take.talent_name,
        "duration": take.duration_seconds,
        "peak_dbfs": (None if take.peak_dbfs == float("-inf")
                      else float(take.peak_dbfs)),
        "start_timecode": take.start_timecode,
        "time_reference_samples": take.time_reference_samples,
        "in_point_frame": take.in_point_frame,
        "notes": take.notes,
    }


def build_take_sheet_from_project(project: Any) -> TakeSheet:
    """从工程素材元数据重建 take sheet（重开工程后恢复）。

    扫描所有 ``metadata['is_recording']`` 为真的素材，按 take 字段还原
    条目；最佳 take 标记来自 ``metadata['is_best_take']``。
    """
    sheet = TakeSheet()
    assets = getattr(project, "assets", None) or {}
    for asset_id, asset in assets.items():
        metadata = getattr(asset, "metadata", None) or {}
        if not metadata.get(KEY_IS_RECORDING):
            continue
        peak = metadata.get("peak_dbfs")
        entry = TakeSheetEntry(
            take_id=str(metadata.get("take_id", asset_id)),
            take_number=int(metadata.get("take_number", 0) or 0),
            name=str(metadata.get("name", "") or getattr(asset, "display_name", "")),
            file_path=str(getattr(asset, "path", "")),
            asset_id=asset_id,
            scene_name=str(metadata.get("scene", "")),
            talent_name=str(metadata.get("talent", "")),
            duration_seconds=float(metadata.get("duration", 0.0) or 0.0),
            peak_dbfs=(float("-inf") if peak is None else float(peak)),
            status="completed",
            start_timecode=str(metadata.get("start_timecode", "")),
            time_reference_samples=int(
                metadata.get("time_reference_samples", 0) or 0),
            in_point_frame=int(metadata.get("in_point_frame", -1)),
            is_best=bool(metadata.get(KEY_IS_BEST, False)),
            notes=str(metadata.get("notes", "")),
        )
        sheet._entries.append(entry)
    sheet._entries.sort(key=lambda e: (e.scene_name, e.take_number, e.take_id))
    return sheet


__all__ = [
    "KEY_IS_BEST",
    "KEY_IS_RECORDING",
    "TakeSheet",
    "TakeSheetEntry",
    "build_take_sheet_from_project",
    "take_metadata_for_asset",
]
