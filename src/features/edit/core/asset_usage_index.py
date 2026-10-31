"""Asset Usage Index —— 素材使用关系索引（P1-13 地基）。

背景（计划表 P1-13）：有重链接和 Batch Relink，但没有
Asset→Clip→Timeline 的依赖索引。缺失素材报告无法列出"所有使用位置"，
Replace Media / Consolidate / Collect / Reveal in Timeline 都缺地基。

本模块提供纯模型索引：

- ``ClipUsage``：一次素材引用的位置（时间线/轨道/剪辑/帧范围）；
- ``AssetUsageIndex``：asset_id → 引用列表；含未引用素材清单；
- 报告格式化（离线素材报告可直接嵌入"使用位置"段落）。

后续 Replace Media / Consolidate / Archive 在此索引之上构建。
约束：纯模型 + 标准库；鸭子类型防御；索引构建 O(clips)。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple


@dataclass(frozen=True)
class ClipUsage:
    """一次素材引用的位置。"""

    timeline_id: str
    timeline_name: str
    track_id: str
    track_name: str
    track_type: str
    clip_id: str
    timeline_start: int
    timeline_end: int

    def to_dict(self) -> Dict[str, Any]:
        return {
            "timeline_id": self.timeline_id,
            "timeline_name": self.timeline_name,
            "track_id": self.track_id,
            "track_name": self.track_name,
            "track_type": self.track_type,
            "clip_id": self.clip_id,
            "timeline_start": self.timeline_start,
            "timeline_end": self.timeline_end,
        }

    def format(self) -> str:
        name = self.timeline_name or self.timeline_id
        track = self.track_name or self.track_id
        return (
            f"{name} / {track}（{self.track_type}）"
            f" clip {self.clip_id} @ 帧 {self.timeline_start}–{self.timeline_end}"
        )


@dataclass(frozen=True)
class AssetUsageIndex:
    """asset_id → 引用列表的只读索引。

    M20（R03）：``binding_only_asset_ids`` = 仅经外录绑定使用（无 clip
    引用）的素材——播放/成片/Stem 消费绑定 WAV，include_unused=False
    的归档必须带上它们。

    N11（2026-10-25）：``snapshot_only_asset_ids`` = 仅被**保留快照**
    引用的素材——快照 payload 的 full_data 携带快照时刻的完整资产表，
    恢复保真需要这些源；include_unused=False 的归档按 unused 丢弃它们
    会令快照恢复断链（审计复现的闭包缺口）。候选 take 无工程级持久
    容器（audition 模型未入 project 存储）——take 闭包随其持久化落地
    （在案未冒称）。
    """

    entries: Dict[str, Tuple[ClipUsage, ...]] = field(default_factory=dict)
    unused_asset_ids: Tuple[str, ...] = ()
    binding_only_asset_ids: Tuple[str, ...] = ()
    snapshot_only_asset_ids: Tuple[str, ...] = ()

    def locations_for(self, asset_id: str) -> Tuple[ClipUsage, ...]:
        return self.entries.get(asset_id, ())

    def is_used(self, asset_id: str) -> bool:
        return (bool(self.entries.get(asset_id))
                or asset_id in self.binding_only_asset_ids
                or asset_id in self.snapshot_only_asset_ids)

    def usage_count(self, asset_id: str) -> int:
        return len(self.entries.get(asset_id, ()))

    def used_asset_ids(self) -> Tuple[str, ...]:
        used = {a for a, u in self.entries.items() if u}
        used.update(self.binding_only_asset_ids)
        used.update(self.snapshot_only_asset_ids)
        return tuple(sorted(used))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "used_assets": {
                asset_id: [u.to_dict() for u in usages]
                for asset_id, usages in sorted(self.entries.items())
                if usages
            },
            "unused_asset_ids": list(self.unused_asset_ids),
            "binding_only_asset_ids": list(self.binding_only_asset_ids),
            "snapshot_only_asset_ids": list(self.snapshot_only_asset_ids),
        }


def _snapshot_referenced_asset_ids(project: Any) -> set:
    """从保留快照 payload 提取素材引用（防御式；N11）。

    快照 payload 两处资产表：topos 核心层 ``payload['assets']`` 与
    ``payload['full_data']['assets']``（dict[asset_id, …]）。形态变化
    时宁可少收（保持旧 unused 语义）不误收。
    """
    referenced: set = set()
    snapshots = getattr(project, "project_snapshots", None) or {}
    if not isinstance(snapshots, dict):
        return referenced
    for entry in snapshots.values():
        payload = entry.get("payload") if isinstance(entry, dict) else None
        if not isinstance(payload, dict):
            continue
        for table in (payload.get("assets"),
                      (payload.get("full_data") or {}).get("assets")):
            if isinstance(table, dict):
                referenced.update(str(k) for k in table.keys())
            elif isinstance(table, list):
                for item in table:
                    if isinstance(item, dict) and item.get("asset_id"):
                        referenced.add(str(item["asset_id"]))
    return referenced


def build_asset_usage_index(
    sequences: Iterable[Any],
    project: Optional[Any] = None,
) -> AssetUsageIndex:
    """构建素材使用索引。

    Args:
        sequences: TimelineSequence 集合（导出路径传当前序列；
            工程级报告传全部序列）。
        project: 提供时同时计算"工程中存在但无引用"的素材清单。
    """
    entries: Dict[str, List[ClipUsage]] = {}
    for sequence in sequences:
        seq_id = str(getattr(sequence, "timeline_id", "") or "")
        seq_name = str(getattr(sequence, "name", "") or "")
        for track in getattr(sequence, "all_tracks", ()):
            track_id = str(getattr(track, "track_id", "") or "")
            track_name = str(getattr(track, "name", "") or "")
            track_type = str(getattr(track, "track_type", "") or "")
            for clip in getattr(track, "clips", ()):
                asset_id = str(getattr(clip, "asset_id", "") or "")
                if not asset_id:
                    continue
                start = int(getattr(clip, "timeline_start", 0) or 0)
                end = start + int(getattr(clip, "timeline_duration", 0) or 0)
                entries.setdefault(asset_id, []).append(ClipUsage(
                    timeline_id=seq_id,
                    timeline_name=seq_name,
                    track_id=track_id,
                    track_name=track_name,
                    track_type=track_type,
                    clip_id=str(getattr(clip, "clip_id", "") or ""),
                    timeline_start=start,
                    timeline_end=end,
                ))

    unused: Tuple[str, ...] = ()
    binding_used: set = set()
    snapshot_used: set = set()
    if project is not None:
        assets = getattr(project, "assets", None)
        if isinstance(assets, dict):
            # M20（R03）：仅经外录绑定使用的素材不是 unused——绑定 WAV
            # 无 clip 引用，但播放/成片/Stem 都消费它；include_unused=False
            # 的归档若按 unused 丢弃会断音频。keep_camera_audio 的绑定也
            # 保留（工程恢复需要 WAV）。
            bindings = getattr(project, "audio_sync_bindings", None) or {}
            if isinstance(bindings, dict):
                for binding in bindings.values():
                    wav_id = getattr(binding, "wav_asset_id", None)
                    if wav_id:
                        binding_used.add(str(wav_id))
            # N11：保留快照引用的素材同样不是 unused——快照恢复保真
            # 需要其源（include_unused=False 丢弃即断快照链）。
            snapshot_used = _snapshot_referenced_asset_ids(project)
            unused = tuple(
                sorted(
                    str(a) for a in assets.keys()
                    if not entries.get(str(a))
                    and str(a) not in binding_used
                    and str(a) not in snapshot_used
                )
            )

    return AssetUsageIndex(
        entries={k: tuple(v) for k, v in entries.items()},
        unused_asset_ids=unused,
        binding_only_asset_ids=tuple(sorted(
            w for w in binding_used if not entries.get(w))),
        snapshot_only_asset_ids=tuple(sorted(
            s for s in snapshot_used
            if not entries.get(s) and s not in binding_used)),
    )
