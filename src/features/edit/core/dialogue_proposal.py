"""B13 · 对白工具闭环：候选→校验→单命令应用（主计划 §20/B13）。

``DialogueProposal`` 是只读候选（RoomTone/对齐/批淡化/gap split 的组合），
经 ``validate_proposal``（工程 revision + 源指纹双重校验）后由
``ApplyDialogueProposalCommand`` 一次性落地（单步 undo）。对齐写既有
采样偏移字段（``ClipInstance.audio_sample_offset``），不改 UI 播放头伪装
对齐；置信度低于门槛的对齐不自动接受（相同周期音/静音低置信度）。

``RoomToneRef`` 只存源窗引用（PCM 不进工程）；``RecorderSourceIdentity``
扩展原素材 metadata（BWF time reference/UMID/场次镜次/通道名），缺字段
保持 unknown——不把日期缺省为今天。
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field, replace
from typing import Any, Dict, Mapping, Optional, Tuple

logger = logging.getLogger(__name__)

DIALOGUE_PROPOSAL_VERSION = 1

#: 对齐自动应用的最低置信度（峰值归一化相关）。低置信度候选仅提示。
ALIGNMENT_AUTO_APPLY_CONFIDENCE = 0.6


@dataclass(frozen=True)
class RoomToneRef:
    """Room Tone 源窗引用（不存 PCM 到工程）。"""

    asset_id: str
    source_start_sample: int
    source_end_sample: int
    channel_map: Tuple[int, ...] = ()     # 空 = 原布局直通
    crossfade_samples: int = 64
    seed: int = 0                          # fill 生成固定 seed（缓存/交付一致）

    def __post_init__(self) -> None:
        if self.source_end_sample <= self.source_start_sample:
            raise ValueError("empty room tone source window")
        if self.crossfade_samples < 0:
            raise ValueError("negative crossfade")


@dataclass(frozen=True)
class RecorderSourceIdentity:
    """外录来源身份（逐级过滤：稳定ID→卷/日期/场次镜次→时间窗→通道名）。"""

    asset_id: str
    bwf_time_reference: Optional[int] = None   # 缺字段保持 unknown
    umid: str = ""
    scene: str = ""
    take: str = ""
    reel: str = ""
    recorded_date: str = ""                     # 缺失不缺省为今天
    source_rate: int = 0
    channel_names: Tuple[str, ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "asset_id": self.asset_id,
            "bwf_time_reference": self.bwf_time_reference,
            "umid": self.umid,
            "scene": self.scene,
            "take": self.take,
            "reel": self.reel,
            "recorded_date": self.recorded_date,
            "source_rate": self.source_rate,
            "channel_names": list(self.channel_names),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "RecorderSourceIdentity":
        return cls(
            asset_id=str(data.get("asset_id", "")),
            bwf_time_reference=data.get("bwf_time_reference"),
            umid=str(data.get("umid", "")),
            scene=str(data.get("scene", "")),
            take=str(data.get("take", "")),
            reel=str(data.get("reel", "")),
            recorded_date=str(data.get("recorded_date", "")),
            source_rate=int(data.get("source_rate", 0) or 0),
            channel_names=tuple(str(c) for c in
                                (data.get("channel_names") or ())),
        )


# 操作类型（对齐 B13 手册第 1/2/5 步；全部为只读候选，应用走命令）
OPERATION_ALIGN = "align"
OPERATION_ROOM_TONE = "room_tone"
OPERATION_BATCH_CROSSFADE = "batch_crossfade"
OPERATION_GAP_SPLIT = "gap_split"
OPERATION_TYPES = (OPERATION_ALIGN, OPERATION_ROOM_TONE,
                   OPERATION_BATCH_CROSSFADE, OPERATION_GAP_SPLIT)


@dataclass(frozen=True)
class ProposalOperation:
    """一个候选操作（类型 + 定位 + 参数载荷）。"""

    op_type: str
    track_id: str
    clip_id: str = ""
    # align：take 相对参考的整样本偏移（+ = 右移）；confidence 为
    # estimate_alignment_offset 的归一化峰值相关。
    offset_samples: int = 0
    confidence: float = 0.0
    # room_tone / 其他操作的载荷（GapSplitPlan/BatchCrossfadePlan 的 dict）
    payload: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.op_type not in OPERATION_TYPES:
            raise ValueError(f"unknown op_type: {self.op_type!r}")


@dataclass(frozen=True)
class DialogueProposal:
    """对白工具只读候选（提交前 revision/指纹可能变化 → 重新计算）。"""

    proposal_id: str
    project_revision: int
    source_fingerprints: Tuple[str, ...]
    operations: Tuple[ProposalOperation, ...]
    warnings: Tuple[str, ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "version": DIALOGUE_PROPOSAL_VERSION,
            "proposal_id": self.proposal_id,
            "project_revision": self.project_revision,
            "source_fingerprints": list(self.source_fingerprints),
            "operations": [{
                "op_type": op.op_type,
                "track_id": op.track_id,
                "clip_id": op.clip_id,
                "offset_samples": op.offset_samples,
                "confidence": op.confidence,
                "payload": dict(op.payload),
            } for op in self.operations],
            "warnings": list(self.warnings),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "DialogueProposal":
        return cls(
            proposal_id=str(data.get("proposal_id", "")),
            project_revision=int(data.get("project_revision", 0) or 0),
            source_fingerprints=tuple(
                str(f) for f in (data.get("source_fingerprints") or ())),
            operations=tuple(
                ProposalOperation(
                    op_type=str(op.get("op_type", "")),
                    track_id=str(op.get("track_id", "")),
                    clip_id=str(op.get("clip_id", "")),
                    offset_samples=int(op.get("offset_samples", 0) or 0),
                    confidence=float(op.get("confidence", 0.0) or 0.0),
                    payload=dict(op.get("payload") or {}),
                )
                for op in (data.get("operations") or ())
            ),
            warnings=tuple(str(w) for w in (data.get("warnings") or ())),
        )


def validate_proposal(
    proposal: DialogueProposal,
    *,
    current_revision: int,
    current_fingerprints: Tuple[str, ...],
) -> Tuple[str, ...]:
    """候选校验：revision/指纹不匹配 → 错误列表（非空=必须重新计算）。"""
    errors: List[str] = []
    if proposal.project_revision != current_revision:
        errors.append(
            f"proposal revision {proposal.project_revision} != current "
            f"{current_revision}; recompute required")
    if set(current_fingerprints) != set(proposal.source_fingerprints):
        errors.append("source fingerprints changed since proposal")
    for op in proposal.operations:
        if op.op_type == OPERATION_ALIGN:
            if op.confidence < ALIGNMENT_AUTO_APPLY_CONFIDENCE:
                errors.append(
                    f"alignment for clip {op.clip_id} confidence "
                    f"{op.confidence:.2f} below auto-apply threshold "
                    f"{ALIGNMENT_AUTO_APPLY_CONFIDENCE:.2f}")
    return errors


def apply_alignment_to_clip(clip: Any, offset_samples: int) -> Any:
    """把对齐偏移写进 clip 既有采样偏移字段（模型语义，不改播放头）。

    模型字段是非负语义：``audio_sample_offset`` = 帧起点后延迟的样本数
    （take 右移）；``source_sample_offset`` = 入点后跳过的源样本数
    （take 左移）。正偏移走延迟字段，负偏移走源侧修剪；既有值叠加。
    """
    from dataclasses import replace as _replace

    if not hasattr(clip, "audio_sample_offset"):
        raise ValueError("clip has no audio_sample_offset field")
    offset = int(offset_samples)
    if offset == 0:
        return clip
    if offset > 0:
        return _replace(
            clip,
            audio_sample_offset=int(getattr(clip, "audio_sample_offset", 0) or 0)
            + offset)
    # 左移：源侧修剪 |offset| 个样本（内容在时间线上前移）
    return _replace(
        clip,
        source_sample_offset=int(getattr(clip, "source_sample_offset", 0) or 0)
        + (-offset))


@dataclass(frozen=True)
class ApplyDialogueProposalCommand:
    """应用对白候选（单命令单 undo；对齐仅应用置信度达标操作）。"""

    command_id: str
    label: str
    proposal_id: str
    track_id: str
    align_offsets: Tuple[Tuple[str, int], ...]   # (clip_id, offset)
    _old_offsets: Optional[Dict[str, Tuple[int, int]]] = None

    @staticmethod
    def create(proposal: DialogueProposal, track_id: str,
               *,
               include_low_confidence: bool = False,
               ) -> Tuple["ApplyDialogueProposalCommand", Tuple[str, ...]]:
        """从候选构建命令；低置信度对齐默认排除（返回跳过列表）。"""
        offsets: list = []
        skipped: list = []
        for op in proposal.operations:
            if op.op_type != OPERATION_ALIGN or not op.clip_id:
                continue
            if (op.confidence < ALIGNMENT_AUTO_APPLY_CONFIDENCE
                    and not include_low_confidence):
                skipped.append(
                    f"{op.clip_id}: confidence {op.confidence:.2f} below "
                    "auto-apply threshold")
                continue
            offsets.append((op.clip_id, int(op.offset_samples)))
        return ApplyDialogueProposalCommand(
            command_id=str(uuid.uuid4()),
            label=f"Apply dialogue proposal ({proposal.proposal_id[:8]})",
            proposal_id=proposal.proposal_id,
            track_id=track_id,
            align_offsets=tuple(offsets),
        ), tuple(skipped)

    def apply(self, timeline: Any):
        track = timeline.get_track_by_id(self.track_id)
        if track is None:
            raise ValueError(f"Track not found: {self.track_id}")
        from dataclasses import replace as _replace

        old: Dict[str, Tuple[int, int]] = {}
        new_clips = []
        targets = dict(self.align_offsets)
        hit = set()
        for clip in track.clips:
            if clip.clip_id in targets:
                old[clip.clip_id] = (
                    int(getattr(clip, "audio_sample_offset", 0) or 0),
                    int(getattr(clip, "source_sample_offset", 0) or 0))
                new_clips.append(apply_alignment_to_clip(
                    clip, targets[clip.clip_id]))
                hit.add(clip.clip_id)
            else:
                new_clips.append(clip)
        missing = set(targets) - hit
        if missing:
            raise ValueError(f"clips not found on track {self.track_id}: "
                             f"{sorted(missing)}")
        new_track = track._rebuild(clips=tuple(new_clips))
        new_timeline = timeline.with_track_updated(new_track)
        updated = replace(self, _old_offsets=old)
        return new_timeline, updated

    def undo(self, timeline: Any) -> Any:
        if self._old_offsets is None:
            return timeline
        track = timeline.get_track_by_id(self.track_id)
        if track is None:
            raise ValueError(f"Track not found: {self.track_id}")
        from dataclasses import replace as _replace

        new_clips = []
        for clip in track.clips:
            if clip.clip_id in self._old_offsets:
                audio_off, source_off = self._old_offsets[clip.clip_id]
                new_clips.append(_replace(clip,
                                          audio_sample_offset=audio_off,
                                          source_sample_offset=source_off))
            else:
                new_clips.append(clip)
        return timeline.with_track_updated(track._rebuild(clips=tuple(new_clips)))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "command_id": self.command_id,
            "command_type": "ApplyDialogueProposalCommand",
            "label": self.label,
            "proposal_id": self.proposal_id,
            "track_id": self.track_id,
            "align_offsets": [list(pair) for pair in self.align_offsets],
            "old_offsets": (
                {k: list(v) for k, v in self._old_offsets.items()}
                if self._old_offsets else None),
        }

    @staticmethod
    def from_dict(data: Mapping[str, Any]) -> "ApplyDialogueProposalCommand":
        raw_old = data.get("old_offsets")
        return ApplyDialogueProposalCommand(
            command_id=str(data.get("command_id", "")),
            label=str(data.get("label", "Apply dialogue proposal")),
            proposal_id=str(data.get("proposal_id", "")),
            track_id=str(data.get("track_id", "")),
            align_offsets=tuple(
                (str(p[0]), int(p[1])) for p in
                (data.get("align_offsets") or [])),
            _old_offsets=(
                {str(k): (int(v[0]), int(v[1])) for k, v in raw_old.items()}
                if raw_old else None),
        )


__all__ = [
    "ALIGNMENT_AUTO_APPLY_CONFIDENCE",
    "ApplyDialogueProposalCommand",
    "DialogueProposal",
    "DIALOGUE_PROPOSAL_VERSION",
    "OPERATION_TYPES",
    "ProposalOperation",
    "RecorderSourceIdentity",
    "RoomToneRef",
    "apply_alignment_to_clip",
    "validate_proposal",
]
