"""T15 · 两份资料快照的变化对照（保存/读取入口 + 内容级 diff）。

纪律（计划 T15）：

- 保存/读取走 T01 唯一 codec（本模块只加文件入口；schema/hash 校验、
  严格拒绝语义全部复用 codec）；
- 只比较**已收集的逻辑字段**：片段新增/移除/位置/时长、字幕/marker 改
  动、素材改名/日志字段、统计差值；捕获时间戳/ID、revision 计数、导航
  状态**不参与**（避免"所有行都变了"的假阳性）；
- 按稳定 ID 配对；重建工程 project_id 不同 → ``same_project=False`` 提示
  不可直接一一对应（仍按 ID 交集给出结果但标记不可靠）；
- 任一侧未加载的序列进 ``not_compared``——完整性范围不同不误判大量删除；
- 结果只读可导出（JSON/Markdown）；不恢复/不合并工程，不替代 M02 快照
  /版本服务或 M15 评论重定位，不声明像素/音频 QC 一致。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.features.edit.insights.codec import (
    SnapshotParseResult,
    parse_snapshot,
    serialize_snapshot,
)
from src.features.edit.insights.dto import (
    LOAD_STATE_LOADED,
    InsightSnapshot,
)
from src.features.edit.insights.stats import compute_coverage

DIFF_SCHEMA = "insights-snapshot-diff/1"


# ── 保存 / 读取入口（T01 codec 之上的文件薄封装）────────────────


def save_snapshot_file(snapshot: InsightSnapshot, path: str | Path) -> None:
    Path(path).write_text(serialize_snapshot(snapshot), encoding="utf-8")


def load_snapshot_file(path: str | Path) -> SnapshotParseResult:
    return parse_snapshot(Path(path).read_text(encoding="utf-8"))


# ── diff 数据结构 ────────────────────────────────────────────────


@dataclass(frozen=True)
class FieldChange:
    field: str
    old: Any
    new: Any


@dataclass(frozen=True)
class EntityChange:
    sequence_id: str | None   # asset 级为 None
    entity_id: str
    kind: str                 # added / removed / changed
    changes: tuple[FieldChange, ...] = ()
    label: str = ""           # 显示辅助（素材名/cue 文本摘要）


@dataclass(frozen=True)
class SequenceStatDelta:
    sequence_id: str
    clip_count_old: int | None
    clip_count_new: int | None
    coverage_union_old: int | None
    coverage_union_new: int | None


@dataclass(frozen=True)
class SnapshotDiff:
    schema: str
    same_project: bool
    old_capture_id: str
    new_capture_id: str
    old_project_id: str
    new_project_id: str
    sequences_added: tuple[str, ...]
    sequences_removed: tuple[str, ...]
    sequence_meta_changes: tuple[EntityChange, ...]   # 名称/fps 等序列级
    clip_changes: tuple[EntityChange, ...]
    cue_changes: tuple[EntityChange, ...]
    marker_changes: tuple[EntityChange, ...]
    asset_changes: tuple[EntityChange, ...]
    not_compared: tuple[str, ...]   # 任一侧未加载的序列（如实列出未比较范围）
    stats_delta: tuple[SequenceStatDelta, ...]

    @property
    def has_changes(self) -> bool:
        return bool(
            self.sequences_added or self.sequences_removed
            or self.sequence_meta_changes or self.clip_changes
            or self.cue_changes or self.marker_changes or self.asset_changes
        )


# ── 字段级比较 ───────────────────────────────────────────────────

_CLIP_FIELDS = (
    "timeline_start", "timeline_end", "source_start", "source_end",
    "asset_id", "is_enabled", "source_mapping", "reverse",
    "constant_speed", "linked_clip_id", "label_color", "track_id",
)
_CUE_FIELDS = ("text", "timeline_start", "timeline_end", "is_enabled", "track_id")
_MARKER_FIELDS = ("frame", "name", "color", "comment", "kind")
_ASSET_FIELDS = (
    "display_name", "media_type", "is_offline", "file_basename",
    "reel", "scene", "shot", "take", "keywords", "rating",
    "flag", "color_label", "comment",
)


def _field_changes(old: Any, new: Any, fields: tuple[str, ...]) -> tuple[FieldChange, ...]:
    out: list[FieldChange] = []
    for name in fields:
        old_value = getattr(old, name)
        new_value = getattr(new, name)
        if old_value != new_value:
            out.append(FieldChange(field=name, old=old_value, new=new_value))
    return tuple(out)


def _diff_entities(
    old_by_id: dict[str, Any],
    new_by_id: dict[str, Any],
    fields: tuple[str, ...],
    sequence_id: str | None,
    label_of: Any = None,
) -> tuple[EntityChange, ...]:
    changes: list[EntityChange] = []
    for entity_id in sorted(set(old_by_id) - set(new_by_id)):
        changes.append(EntityChange(
            sequence_id=sequence_id, entity_id=entity_id, kind="removed",
            label=str(label_of(old_by_id[entity_id])) if label_of else "",
        ))
    for entity_id in sorted(set(new_by_id) - set(old_by_id)):
        changes.append(EntityChange(
            sequence_id=sequence_id, entity_id=entity_id, kind="added",
            label=str(label_of(new_by_id[entity_id])) if label_of else "",
        ))
    for entity_id in sorted(set(old_by_id) & set(new_by_id)):
        fc = _field_changes(old_by_id[entity_id], new_by_id[entity_id], fields)
        if fc:
            changes.append(EntityChange(
                sequence_id=sequence_id, entity_id=entity_id, kind="changed",
                changes=fc,
                label=str(label_of(new_by_id[entity_id])) if label_of else "",
            ))
    return tuple(changes)


def compare_snapshots(old: InsightSnapshot, new: InsightSnapshot) -> SnapshotDiff:
    """内容级对照（revision/捕获元数据/导航状态不参与）。"""
    old_seqs = {s.sequence_id: s for s in old.sequences}
    new_seqs = {s.sequence_id: s for s in new.sequences}
    added = tuple(sorted(set(new_seqs) - set(old_seqs)))
    removed = tuple(sorted(set(old_seqs) - set(new_seqs)))

    not_compared = tuple(sorted(
        seq_id for seq_id in set(old_seqs) & set(new_seqs)
        if old_seqs[seq_id].load_state != LOAD_STATE_LOADED
        or new_seqs[seq_id].load_state != LOAD_STATE_LOADED
    ))

    meta_changes: list[EntityChange] = []
    clip_changes: list[EntityChange] = []
    cue_changes: list[EntityChange] = []
    marker_changes: list[EntityChange] = []
    stats: list[SequenceStatDelta] = []

    for seq_id in sorted(set(old_seqs) & set(new_seqs)):
        old_seq, new_seq = old_seqs[seq_id], new_seqs[seq_id]
        if seq_id in not_compared:
            continue
        # 序列级逻辑字段：名称与时间基（revision 不参与内容比较）
        seq_fc = _field_changes(old_seq, new_seq, ("name",))
        if old_seq.fps != new_seq.fps:
            seq_fc += (FieldChange(
                field="fps",
                old=f"{old_seq.fps.num}/{old_seq.fps.den}",
                new=f"{new_seq.fps.num}/{new_seq.fps.den}",
            ),)
        if seq_fc:
            meta_changes.append(EntityChange(
                sequence_id=seq_id, entity_id=seq_id, kind="changed",
                changes=seq_fc, label=new_seq.name,
            ))

        old_clips = {c.clip_id: c for t in old_seq.tracks for c in t.clips}
        new_clips = {c.clip_id: c for t in new_seq.tracks for c in t.clips}
        clip_changes.extend(_diff_entities(
            old_clips, new_clips, _CLIP_FIELDS, seq_id,
        ))
        old_cues = {c.clip_id: c for t in old_seq.tracks for c in t.cues}
        new_cues = {c.clip_id: c for t in new_seq.tracks for c in t.cues}
        cue_changes.extend(_diff_entities(
            old_cues, new_cues, _CUE_FIELDS, seq_id,
            label_of=lambda c: c.text.replace("\n", " ⏎ "),
        ))
        old_markers = {m.marker_id: m for m in old_seq.markers}
        new_markers = {m.marker_id: m for m in new_seq.markers}
        marker_changes.extend(_diff_entities(
            old_markers, new_markers, _MARKER_FIELDS, seq_id,
        ))

        stats.append(SequenceStatDelta(
            sequence_id=seq_id,
            clip_count_old=len(old_clips),
            clip_count_new=len(new_clips),
            coverage_union_old=compute_coverage(
                (c.timeline_start, c.timeline_end) for c in old_clips.values()
            ).union_frames,
            coverage_union_new=compute_coverage(
                (c.timeline_start, c.timeline_end) for c in new_clips.values()
            ).union_frames,
        ))

    old_assets = {a.asset_id: a for a in old.assets}
    new_assets = {a.asset_id: a for a in new.assets}
    asset_changes = _diff_entities(
        old_assets, new_assets, _ASSET_FIELDS, None,
        label_of=lambda a: a.display_name,
    )

    return SnapshotDiff(
        schema=DIFF_SCHEMA,
        same_project=old.project.project_id == new.project.project_id,
        old_capture_id=old.capture_id,
        new_capture_id=new.capture_id,
        old_project_id=old.project.project_id,
        new_project_id=new.project.project_id,
        sequences_added=added,
        sequences_removed=removed,
        sequence_meta_changes=tuple(meta_changes),
        clip_changes=tuple(clip_changes),
        cue_changes=tuple(cue_changes),
        marker_changes=tuple(marker_changes),
        asset_changes=asset_changes,
        not_compared=not_compared,
        stats_delta=tuple(stats),
    )


# ── 导出（只读）─────────────────────────────────────────────────


def diff_to_dict(diff: SnapshotDiff) -> dict[str, Any]:
    def entity(e: EntityChange) -> dict:
        return {
            "sequence_id": e.sequence_id, "entity_id": e.entity_id,
            "kind": e.kind, "label": e.label,
            "changes": [
                {"field": c.field, "old": c.old, "new": c.new} for c in e.changes
            ],
        }

    return {
        "schema": diff.schema,
        "same_project": diff.same_project,
        "old_capture_id": diff.old_capture_id,
        "new_capture_id": diff.new_capture_id,
        "not_compared_unloaded": list(diff.not_compared),
        "sequences_added": list(diff.sequences_added),
        "sequences_removed": list(diff.sequences_removed),
        "sequence_meta_changes": [entity(e) for e in diff.sequence_meta_changes],
        "clip_changes": [entity(e) for e in diff.clip_changes],
        "cue_changes": [entity(e) for e in diff.cue_changes],
        "marker_changes": [entity(e) for e in diff.marker_changes],
        "asset_changes": [entity(e) for e in diff.asset_changes],
        "stats_delta": [
            {
                "sequence_id": s.sequence_id,
                "clip_count": [s.clip_count_old, s.clip_count_new],
                "coverage_union": [s.coverage_union_old, s.coverage_union_new],
            }
            for s in diff.stats_delta
        ],
        "note": "read-only comparison; revisions/capture metadata/navigation "
                "state are excluded; no project restore/merge",
    }


def export_diff_json(diff: SnapshotDiff, path: str | Path) -> None:
    Path(path).write_text(
        json.dumps(diff_to_dict(diff), ensure_ascii=False, indent=1),
        encoding="utf-8",
    )


def render_diff_markdown(diff: SnapshotDiff) -> str:
    lines: list[str] = [
        "# Snapshot Diff",
        f"- old capture: `{diff.old_capture_id}` → new capture: `{diff.new_capture_id}`",
        f"- same project: {diff.same_project}"
        + ("" if diff.same_project else "（重建工程：ID 不可直接一一对应，结果仅供参考）"),
    ]
    if diff.not_compared:
        lines.append(f"- 未比较（含未加载序列）: {', '.join(diff.not_compared)}")
    if not diff.has_changes:
        lines.append("\n（编辑内容无变化）")
        return "\n".join(lines)

    def section(title: str, changes: tuple[EntityChange, ...]) -> None:
        if not changes:
            return
        lines.append(f"\n## {title}")
        for e in changes:
            where = f"{e.sequence_id}/" if e.sequence_id else ""
            head = f"- **{e.kind}** `{where}{e.entity_id}`"
            if e.label:
                head += f" · {e.label}"
            lines.append(head)
            for c in e.changes:
                lines.append(f"  - {c.field}: {c.old!r} → {c.new!r}")

    section("序列", tuple(
        EntityChange(sequence_id=s, entity_id=s, kind="added")
        for s in diff.sequences_added
    ) + tuple(
        EntityChange(sequence_id=s, entity_id=s, kind="removed")
        for s in diff.sequences_removed
    ) + diff.sequence_meta_changes)
    section("片段", diff.clip_changes)
    section("字幕/对白", diff.cue_changes)
    section("Marker", diff.marker_changes)
    section("素材", diff.asset_changes)
    changed_stats = [
        s for s in diff.stats_delta
        if s.clip_count_old != s.clip_count_new
        or s.coverage_union_old != s.coverage_union_new
    ]
    if changed_stats:
        lines.append("\n## 统计差值")
        for s in changed_stats:
            lines.append(
                f"- {s.sequence_id}: clips {s.clip_count_old}→{s.clip_count_new}，"
                f"覆盖并集 {s.coverage_union_old}→{s.coverage_union_new}"
            )
    lines.append("\n> 只读对照：不恢复/不合并工程；revision/捕获元数据不参与比较。")
    return "\n".join(lines)


def export_diff_markdown(diff: SnapshotDiff, path: str | Path) -> None:
    Path(path).write_text(render_diff_markdown(diff), encoding="utf-8")
