"""F8.1 · 命名工程快照与版本比较——纯模型层。

语义（对齐计划）：
- **区分**自动备份 / 命名项目版本 / 调色版本（F4.2 已交付栈级命名
  版本；本模块是**工程级**命名快照）；
- 快照记录**来源 revision + 依赖清单**（素材路径/调色栈/LUT 外部
  依赖），采用内容寻址（工程 payload SHA256）；
- **比较**：片段级移动/修剪/替换、效果/调色/音频变化的结构化 diff
  （初版只读比较——不做三方合并，计划原文）；
- **恢复**：命名版本可重开/复制分支/回退；恢复**不污染当前 Undo**
  （快照/恢复经领域命令，undo 恢复到恢复前状态）；
- 不复制巨大临时缓存（快照只存工程 payload 引用 + 内容哈希，媒体/
  代理/缓存不进快照）；不存 secret（工程 payload 本身无凭据——
  序列化时断言无 secret 键）。

存储：快照表挂工程 ``project_snapshots`` 字段（{name: {payload_hash,
payload, source_revision, created_at, dependencies}}）——与 F4.2
版本表同 settings 键模式。
"""

from __future__ import annotations

import copy
import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

_SECRET_KEY_MARKERS = ("token", "secret", "password", "api_key", "credential")


def _payload_hash(payload: Dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str,
                   ensure_ascii=False).encode("utf-8")).hexdigest()


def _assert_no_secrets(payload: Dict[str, Any]) -> None:
    """隐私清理守卫：payload 键名含 secret 标记即拒绝（不入快照）。"""
    def _scan(obj: Any, path: str = "") -> None:
        if isinstance(obj, dict):
            for key, value in obj.items():
                key_lower = str(key).lower()
                if any(marker in key_lower for marker in _SECRET_KEY_MARKERS):
                    raise ValueError(
                        f"快照 payload 含疑似 secret 键：{path}{key}——拒绝")
                _scan(value, f"{path}{key}.")
        elif isinstance(obj, (list, tuple)):
            for i, value in enumerate(obj):
                _scan(value, f"{path}[{i}].")
    _scan(payload)


@dataclass(frozen=True)
class ProjectSnapshotMeta:
    """一条命名工程快照的元数据（payload 存快照表）。"""

    name: str
    payload_hash: str
    source_revision: int
    created_at: float
    dependencies: Dict[str, List[str]] = field(default_factory=dict)
    auto_backup: bool = False     # 区分自动备份与命名版本

    def to_dict(self) -> dict:
        return {
            "name": self.name, "payload_hash": self.payload_hash,
            "source_revision": self.source_revision,
            "created_at": self.created_at,
            "dependencies": dict(self.dependencies),
            "auto_backup": self.auto_backup,
        }


@dataclass(frozen=True)
class SnapshotDiffEntry:
    """单项差异（结构化，只读比较）。"""

    kind: str        # clip_moved / clip_trimmed / clip_added / clip_removed /
                     # clip_replaced / effect_changed / grade_changed /
                     # audio_changed / stack_meta_changed
    timeline_id: str
    detail: str


@dataclass(frozen=True)
class ProjectSnapshotDiff:
    """两快照（或快照↔当前）的结构化比较。"""

    from_name: str
    to_name: str
    entries: Tuple[SnapshotDiffEntry, ...]

    @property
    def summary(self) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for entry in self.entries:
            counts[entry.kind] = counts.get(entry.kind, 0) + 1
        return counts


def capture_snapshot_payload(
    project: Any,
    timeline_sequences: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """捕获工程 payload（M02：与正式保存链同一 serializer）。

    payload 结构：
    - 工程核心 = convert_edit_project_to_topos_project(project).to_dict()
      （project.json 同构：素材引用/时间线引用/settings 全量表）；
    - ``full_data`` = 与 EditProjectIO.save_project 逐字段相同的完整内容
      （素材 21 字段/bins/timeline refs/真实 sequence dict/调色栈/
      compositions/comp_clips/smart_bins）——恢复侧据此重建全保真工程；
    - **非递归引用**：settings.project_snapshots 从 payload 剥离——快照
      不嵌套其他快照的 payload（否则快照套快照指数膨胀）。

    Args:
        project: EditProject。
        timeline_sequences: {timeline_id: TimelineSequence}——真实 sequence
            内容（轨道/片段/效果/字幕）。未传时只存引用（模型层调用方
            可后续补）；正式入口应传当前 sequences（与保存一致）。
    """
    from src.features.edit.core.edit_project_io import (
        convert_edit_project_to_topos_project,
    )
    from src.features.edit.core.serialization import EditProjectSerializer
    topos = convert_edit_project_to_topos_project(project)
    payload = topos.to_dict()
    # 非递归引用：快照 payload 不携带快照表本身
    (payload.get("settings") or {}).pop("project_snapshots", None)

    # 完整内容数据（EditProjectIO.save_project 同一 serializer 语义）
    seq_data: Dict[str, Any] = {}
    if timeline_sequences:
        seq_data = {
            seq_id: (seq.to_dict() if hasattr(seq, "to_dict") else dict(seq))
            for seq_id, seq in timeline_sequences.items()
        }
    payload["full_data"] = {
        "assets": {
            asset_id: EditProjectSerializer._asset_to_dict(asset)
            for asset_id, asset in (getattr(project, "assets", None) or {}).items()
        },
        "bins": {
            bin_id: EditProjectSerializer._bin_to_dict(bin_obj)
            for bin_id, bin_obj in (getattr(project, "bins", None) or {}).items()
        },
        "timelines": {
            tl_id: EditProjectSerializer._timeline_ref_to_dict(tl)
            for tl_id, tl in (getattr(project, "timelines", None) or {}).items()
        },
        "timeline_sequences": seq_data,
        "color_stacks": {
            stack_id: (stack.to_dict() if hasattr(stack, "to_dict") else dict(stack))
            for stack_id, stack in (getattr(project, "color_stacks", None) or {}).items()
        },
        "compositions": dict(getattr(project, "compositions", None) or {}),
        "comp_clips": dict(getattr(project, "comp_clips", None) or {}),
        "smart_bins": {
            bin_id: EditProjectSerializer._smart_bin_to_dict(sb)
            for bin_id, sb in (getattr(project, "smart_bins", None) or {}).items()
        },
    }
    _assert_no_secrets(payload)
    return payload


def collect_dependencies(project: Any) -> Dict[str, List[str]]:
    """快照依赖清单：素材路径 + 调色栈 LUT 外部依赖（缺失可报告）。"""
    assets: List[str] = []
    luts: List[str] = []
    for asset in (getattr(project, "assets", None) or {}).values():
        path = getattr(asset, "path", None)
        if path:
            assets.append(str(path))
    for stack in (getattr(project, "color_stacks", None) or {}).values():
        lut = getattr(stack, "lut_path", None)
        if lut:
            luts.append(str(lut))
    return {"media_paths": sorted(set(assets)), "luts": sorted(set(luts))}


def take_snapshot(
    project: Any,
    name: str,
    *,
    auto_backup: bool = False,
    payload: Optional[Dict[str, Any]] = None,
    timeline_sequences: Optional[Dict[str, Any]] = None,
) -> ProjectSnapshotMeta:
    """创建命名快照（写入工程快照表；工程不可变——返回带快照的新工程）。

    Args:
        timeline_sequences: 真实 sequence 内容（同保存链；None=只存引用）。

    Returns:
        (new_project, meta) 元组。
    """
    name = str(name).strip()
    if not name:
        raise ValueError("快照名不能为空。")
    table = dict(getattr(project, "project_snapshots", None) or {})
    if name in table and not auto_backup:
        raise ValueError(f"快照「{name}」已存在（自动备份可覆盖同名）。")
    payload = payload if payload is not None else capture_snapshot_payload(
        project, timeline_sequences=timeline_sequences)
    _assert_no_secrets(payload)
    meta = ProjectSnapshotMeta(
        name=name, payload_hash=_payload_hash(payload),
        source_revision=int(getattr(project, "revision", 0) or 0),
        created_at=time.time(),
        dependencies=collect_dependencies(project),
        auto_backup=auto_backup)
    table[name] = {
        "meta": meta.to_dict(),
        "payload": payload,   # 内容寻址：payload_hash 即身份
    }
    return project.with_project_snapshots(table), meta


def restore_snapshot(
    project: Any,
    name: str,
    *,
    payload_loader=None,
) -> Tuple[Any, Dict[str, Any]]:
    """恢复命名快照（重开/回退）——返回 (恢复后的工程, 恢复报告)。

    恢复报告含依赖缺失清单（原片移动/插件缺失仍保留版本元数据并
    报告依赖）；恢复不触碰当前工程的 undo 栈（快照恢复是模型级替换，
    undo 语义由调用方命令层包裹）。

    M02（S01）：payload 带 ``full_data`` 时按正式加载链重建——素材
    metadata/评级/调色元数据/真实 sequence 全保真；报告附
    ``timeline_sequences``（{timeline_id: TimelineSequence}，旧 payload
    无 full_data 时为空 dict——序列保持调用方现状）。
    """
    table = dict(getattr(project, "project_snapshots", None) or {})
    entry = table.get(name)
    if entry is None:
        raise ValueError(f"快照不存在：{name}")
    payload = entry["payload"]
    if _payload_hash(payload) != entry["meta"].get("payload_hash"):
        raise ValueError(f"快照「{name}」payload 哈希校验失败——拒绝恢复")
    report: Dict[str, Any] = {
        "restored_from": name,
        "source_revision": entry["meta"].get("source_revision"),
        "missing_dependencies": [],
        "timeline_sequences": {},
    }
    import os
    for dep in entry["meta"].get("dependencies", {}).get("media_paths", []):
        if not os.path.exists(dep):
            report["missing_dependencies"].append(dep)
    for lut in entry["meta"].get("dependencies", {}).get("luts", []):
        if not os.path.exists(lut):
            report["missing_dependencies"].append(lut)
    from src.features.edit.core.edit_project_io import (
        convert_topos_project_to_edit_project,
    )
    from src.shared.project.topos_project import ToposProject
    full_data = payload.get("full_data") if isinstance(payload, dict) else None
    restored = convert_topos_project_to_edit_project(
        ToposProject.from_dict(copy.deepcopy(payload)),
        full_data=copy.deepcopy(full_data) or {})
    # 真实 sequence 内容重建（与 EditProjectIO.load_project 同链）
    if full_data and full_data.get("timeline_sequences"):
        from src.features.edit.core.timeline_v2 import TimelineSequence
        sequences: Dict[str, Any] = {}
        for seq_id, seq_dict in full_data["timeline_sequences"].items():
            try:
                sequences[str(seq_id)] = TimelineSequence.from_dict(
                    copy.deepcopy(seq_dict))
            except (KeyError, TypeError, ValueError) as exc:
                report.setdefault("sequence_load_errors", []).append(
                    {"timeline_id": str(seq_id), "error": str(exc)})
        report["timeline_sequences"] = sequences
    # 快照表随工程走（恢复后的工程仍持有全部快照——可再回退）
    return restored.with_project_snapshots(table), report


def _clip_signature(clip: Any) -> Tuple:
    return (
        getattr(clip, "asset_id", ""), int(getattr(clip, "timeline_start", 0)),
        int(getattr(clip, "timeline_duration", 0)),
        int(getattr(clip, "source_start", 0)),
        getattr(clip, "color_stack_id", None),
        getattr(clip, "audio_gain_db", 0.0),
        len(getattr(clip, "effects", ()) or ()),
    )


def _payload_timelines(payload: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """payload 中的时间线引用表——真实 schema 为 {timeline_id: ref_dict}
    （ToposProject.to_dict() 形态）。M02 前 compare 按"list of dicts"迭代
    真实 payload 直接 AttributeError——真实 dict 形态从未跑通。"""
    raw = payload.get("timelines") or {}
    if isinstance(raw, dict):
        return {str(tid): t for tid, t in raw.items() if isinstance(t, dict)}
    # 兼容历史/手工构造的 list 形态
    if isinstance(raw, (list, tuple)):
        return {
            str(t.get("timeline_id", "")): t
            for t in raw if isinstance(t, dict)
        }
    return {}


def _payload_clips(
    payload: Dict[str, Any],
    timeline_id: str,
    tl_ref: Dict[str, Any],
) -> Dict[Any, Dict[str, Any]]:
    """时间线的片段表。真实 schema：内容在 full_data.timeline_sequences
    的 tracks[].clips（时间线引用本身无片段）；兼容手工/历史构造的
    list 形态（tracks 内联在时间线 dict 里）。"""
    seqs = ((payload.get("full_data") or {}).get("timeline_sequences")) or {}
    seq = seqs.get(timeline_id) or {}
    # TimelineSequence.to_dict() 的轨族：video/audio/subtitle_tracks；
    # 兼容手工/历史形态的单 "tracks" 键
    track_lists = [
        seq.get("video_tracks"), seq.get("audio_tracks"),
        seq.get("subtitle_tracks"), seq.get("tracks"),
        tl_ref.get("tracks"),
    ]
    clips: Dict[Any, Dict[str, Any]] = {}
    for tracks in track_lists:
        for track in (tracks or []):
            if not isinstance(track, dict):
                continue
            for clip in (track.get("clips") or []):
                if isinstance(clip, dict):
                    clips[clip.get("clip_id")] = clip
    return clips


def compare_snapshots(
    from_payload: Dict[str, Any],
    to_payload: Dict[str, Any],
    *,
    from_name: str = "from",
    to_name: str = "to",
) -> ProjectSnapshotDiff:
    """两工程 payload 的结构化只读比较（不做三方合并）。

    M02：按真实 payload schema 比较——时间线引用表（{id: ref}）判
    增删；片段级 diff 取 full_data.timeline_sequences 的 tracks[].clips。
    """
    entries: List[SnapshotDiffEntry] = []
    from_tl = _payload_timelines(from_payload)
    to_tl = _payload_timelines(to_payload)
    for tl_id in sorted(set(from_tl) | set(to_tl)):
        if tl_id not in to_tl:
            entries.append(SnapshotDiffEntry(
                "timeline_removed", tl_id, "时间线被删除"))
            continue
        if tl_id not in from_tl:
            entries.append(SnapshotDiffEntry(
                "timeline_added", tl_id, "新增时间线"))
            continue
        f_clips = _payload_clips(from_payload, tl_id, from_tl[tl_id])
        t_clips = _payload_clips(to_payload, tl_id, to_tl[tl_id])
        for clip_id in sorted(set(f_clips) | set(t_clips), key=str):
            f, t = f_clips.get(clip_id), t_clips.get(clip_id)
            if f is None:
                entries.append(SnapshotDiffEntry(
                    "clip_added", tl_id, f"新增片段 {clip_id}"))
            elif t is None:
                entries.append(SnapshotDiffEntry(
                    "clip_removed", tl_id, f"删除片段 {clip_id}"))
            else:
                f_track = f.get("track_id", "")
                t_track = t.get("track_id", "")
                if f_track != t_track:
                    entries.append(SnapshotDiffEntry(
                        "clip_moved", tl_id,
                        f"片段 {clip_id} 轨道 {f_track}→{t_track}"))
                if (f.get("timeline_start") != t.get("timeline_start")
                        or f.get("timeline_duration")
                        != t.get("timeline_duration")):
                    entries.append(SnapshotDiffEntry(
                        "clip_trimmed", tl_id,
                        f"片段 {clip_id} 位置/时长 "
                        f"{f.get('timeline_start')}+{f.get('timeline_duration')}"
                        f" → {t.get('timeline_start')}+"
                        f"{t.get('timeline_duration')}"))
                if f.get("asset_id") != t.get("asset_id"):
                    entries.append(SnapshotDiffEntry(
                        "clip_replaced", tl_id,
                        f"片段 {clip_id} 换源 "
                        f"{f.get('asset_id')}→{t.get('asset_id')}"))
                if len(f.get("effects", ()) or ()) != len(
                        t.get("effects", ()) or ()):
                    entries.append(SnapshotDiffEntry(
                        "effect_changed", tl_id,
                        f"片段 {clip_id} 效果数 "
                        f"{len(f.get('effects') or ())}"
                        f"→{len(t.get('effects') or ())}"))
                if f.get("color_stack_id") != t.get("color_stack_id"):
                    entries.append(SnapshotDiffEntry(
                        "grade_changed", tl_id,
                        f"片段 {clip_id} 调色栈 "
                        f"{f.get('color_stack_id')}→{t.get('color_stack_id')}"))
                if f.get("audio_gain_db", 0.0) != t.get("audio_gain_db", 0.0):
                    entries.append(SnapshotDiffEntry(
                        "audio_changed", tl_id,
                        f"片段 {clip_id} 音频增益 "
                        f"{f.get('audio_gain_db')}→{t.get('audio_gain_db')}"))
    return ProjectSnapshotDiff(
        from_name=from_name, to_name=to_name, entries=tuple(entries))


def _rebuild_from_payload(payload: Dict[str, Any]) -> Tuple[Any, Dict[str, Any]]:
    """payload → (工程, sequences)；restore_snapshot 的共用重建路径。"""
    from src.features.edit.core.edit_project_io import (
        convert_topos_project_to_edit_project,
    )
    from src.shared.project.topos_project import ToposProject
    full_data = payload.get("full_data") if isinstance(payload, dict) else None
    restored = convert_topos_project_to_edit_project(
        ToposProject.from_dict(copy.deepcopy(payload)),
        full_data=copy.deepcopy(full_data) or {})
    sequences: Dict[str, Any] = {}
    if full_data and full_data.get("timeline_sequences"):
        from src.features.edit.core.timeline_v2 import TimelineSequence
        for seq_id, seq_dict in full_data["timeline_sequences"].items():
            sequences[str(seq_id)] = TimelineSequence.from_dict(
                copy.deepcopy(seq_dict))
    return restored, sequences


@dataclass(frozen=True)
class RestoreSnapshotCommand:
    """恢复命名快照的事务命令（M02：恢复经命令可 Undo）。

    apply：目标快照必须存在且 payload 哈希一致；整体替换为恢复工程，
    快照表随恢复工程携带（可再回退）。undo：对称回到恢复前工程——
    before_payload 在 apply 时捕获（含恢复前 sequences），undo 重建。
    sequences 不入命令栈序列化载荷（体积大），只随执行结果返回，由
    调用方栈管理序列状态的换入/换回。
    """

    command_id: str
    label: str
    snapshot_name: str
    before_payload: Optional[Dict[str, Any]] = None  # apply 时捕获

    def apply(self, project: Any) -> Tuple[Any, Dict[str, Any]]:
        table = dict(getattr(project, "project_snapshots", None) or {})
        entry = table.get(self.snapshot_name)
        if entry is None:
            raise ValueError(f"快照不存在：{self.snapshot_name}")
        if _payload_hash(entry["payload"]) != entry["meta"].get("payload_hash"):
            raise ValueError(
                f"快照「{self.snapshot_name}」payload 哈希校验失败——拒绝恢复")
        object.__setattr__(
            self, "before_payload",
            capture_snapshot_payload(
                project, timeline_sequences=None))
        restored, report = restore_snapshot(project, self.snapshot_name)
        return restored, report

    def undo(self, project: Any) -> Tuple[Any, Dict[str, Any]]:
        if not self.before_payload:
            raise ValueError("命令未执行或 before 状态缺失，无法撤销。")
        restored, sequences = _rebuild_from_payload(self.before_payload)
        # 恢复前工程的快照表整体回挂（before_payload 不含快照表——非递归）
        table = dict(getattr(project, "project_snapshots", None) or {})
        return restored.with_project_snapshots(table), {
            "restored_from": f"undo:{self.snapshot_name}",
            "timeline_sequences": sequences,
            "missing_dependencies": [],
        }

    def to_dict(self) -> dict:
        return {
            "command_id": self.command_id,
            "command_type": "RestoreSnapshotCommand",
            "label": self.label,
            "snapshot_name": self.snapshot_name,
            # before_payload 体积大（整工程）——命令栈序列化只存身份，
            # 跨会话 undo 由调用方按快照表重建命令（工程重开后恢复前
            # 状态不可跨会话保证，与领域命令栈其余部分语义一致）
        }

    @staticmethod
    def from_dict(data: dict) -> "RestoreSnapshotCommand":
        return RestoreSnapshotCommand(
            command_id=data.get("command_id", str(time.time())),
            label=data.get("label", "恢复快照"),
            snapshot_name=str(data.get("snapshot_name", "")),
        )


__all__ = [
    "ProjectSnapshotMeta",
    "ProjectSnapshotDiff",
    "SnapshotDiffEntry",
    "RestoreSnapshotCommand",
    "capture_snapshot_payload",
    "collect_dependencies",
    "take_snapshot",
    "restore_snapshot",
    "compare_snapshots",
]
