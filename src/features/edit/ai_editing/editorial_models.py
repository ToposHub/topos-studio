"""TAEP 创作意图与方案决策模型（Phase 1）。

这些是“为什么这么剪”的可版本化、可验证对象，叠加在 canonical timeline
**之上**，不参与逐帧渲染真相。它们必须能引用到确定的 ``asset_id``、source range
和 ``clip_id``，但绝不直接改变工程（TAEP §3.3 四类创作状态）。

四层单向落地：

    创作意图 (EditorialBrief)        ← 用户为主，AI 只能提议 patch
    素材事实 (AssetAnalysis/SemanticSegment)  ← provider/用户写入，AI 只读
    方案与决策 (StoryOutline/EditVariant/EditDecision)  ← AI 提议，用户可锁定/否决
    确定性交付 (EditPlan/TimelineSequence)    ← 仅经 validate/approval/command 提交

所有 source/timeline range 复用第 4 章的整帧合同：非负 int 帧号、``[start, start+dur)``
半开区间、duration 正整数。

参考：docs/ai_editing_protocol_and_implementation_plan_2026-07-31.md §3.3, §5.3, §5.5
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


# ---------------------------------------------------------------------- #
# 时间/范围原语（整帧合同）
# ---------------------------------------------------------------------- #


@dataclass(frozen=True)
class FrameRange:
    """整帧范围：``[start, start+duration)`` 半开区间。

    - ``start`` 非负；``duration`` 正整数。
    - 不存 ``end``，避免与 ``start+duration`` 矛盾（TAEP §4.1）。
    """

    start: int
    duration: int

    def __post_init__(self) -> None:
        if self.start < 0:
            raise ValueError(f"FrameRange.start must be >= 0, got {self.start}")
        if self.duration <= 0:
            raise ValueError(f"FrameRange.duration must be > 0, got {self.duration}")

    @property
    def end(self) -> int:
        """右端点（不含）。"""
        return self.start + self.duration

    def contains(self, frame: int) -> bool:
        return self.start <= frame < self.end

    def overlaps(self, other: "FrameRange") -> bool:
        return self.start < other.end and other.start < self.end

    def to_dict(self) -> dict[str, int]:
        return {"start": self.start, "duration": self.duration}

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "FrameRange":
        return FrameRange(start=int(data["start"]), duration=int(data["duration"]))


@dataclass(frozen=True)
class SourceRef:
    """对某素材某源范围的引用。

    AI 选择片段时用它精确说出“选择 a-interview 的 1250–1425 帧”。
    """

    asset_id: str
    source: FrameRange

    def to_dict(self) -> dict[str, Any]:
        return {"asset_id": self.asset_id, "source": self.source.to_dict()}

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "SourceRef":
        return SourceRef(
            asset_id=str(data["asset_id"]),
            source=FrameRange.from_dict(data["source"]),
        )


# ---------------------------------------------------------------------- #
# 创作意图：EditorialBrief
# ---------------------------------------------------------------------- #


class ConstraintKind(str, Enum):
    """约束类型。``hard`` 违反即拒绝；``soft`` 仅作排序/解释依据（TAEP §5.3）。"""

    HARD = "hard"
    SOFT = "soft"


class Platform(str, Enum):
    """目标平台 preset（影响画幅/safe-area/时长）。可扩展。"""

    VERTICAL_SOCIAL = "vertical_social"  # 9:16 竖屏社媒
    HORIZONTAL = "horizontal"  # 16:9 横屏
    SQUARE = "square"  # 1:1
    CUSTOM = "custom"


class SubtitlePolicy(str, Enum):
    """字幕策略。"""

    NONE = "none"
    BURN_IN_ZH_CN = "burn_in_zh_cn"
    BURN_IN_EN = "burn_in_en"
    SIDECAR_SRT = "sidecar_srt"
    OPTIONAL = "optional"


@dataclass(frozen=True)
class DurationTarget:
    """目标时长（帧）。``ideal`` 在 ``[min, max]`` 内。"""

    ideal: int
    min: int
    max: int

    def __post_init__(self) -> None:
        if self.ideal <= 0 or self.min <= 0 or self.max <= 0:
            raise ValueError("DurationTarget values must be positive")
        if self.min > self.ideal or self.ideal > self.max:
            raise ValueError(
                f"DurationTarget must satisfy min({self.min}) <= ideal({self.ideal}) <= max({self.max})"
            )

    def to_dict(self) -> dict[str, int]:
        return {"ideal": self.ideal, "min": self.min, "max": self.max}

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "DurationTarget":
        return DurationTarget(
            ideal=int(data["ideal"]), min=int(data["min"]), max=int(data["max"])
        )


@dataclass(frozen=True)
class HardConstraint:
    """硬约束：违反即拒绝提交/导出（TAEP §5.3 ``constraints.locked_*``）。"""

    kind: str  # 如 "locked_source_range" / "forbidden_labels" / "must_include_claim_ids"
    spec: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "spec": dict(self.spec)}

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "HardConstraint":
        return HardConstraint(kind=str(data["kind"]), spec=dict(data.get("spec", {})))


@dataclass(frozen=True)
class SoftPreference:
    """软偏好：仅影响 variant 排序与评分解释，不阻止提交。"""

    key: str  # 如 "pace" / "tone" / "priority_order"
    weight: float = 1.0  # 排序权重
    spec: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"key": self.key, "weight": self.weight, "spec": dict(self.spec)}

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "SoftPreference":
        return SoftPreference(
            key=str(data["key"]),
            weight=float(data.get("weight", 1.0)),
            spec=dict(data.get("spec", {})),
        )


@dataclass(frozen=True)
class Deliverable:
    """单个交付目标（可多版本，如主线 + teaser）。"""

    deliverable_id: str
    aspect_ratio: str  # 如 "9:16"
    duration_target: int  # 帧
    export_preset: str  # 引用现有 deliver preset

    def to_dict(self) -> dict[str, Any]:
        return {
            "deliverable_id": self.deliverable_id,
            "aspect_ratio": self.aspect_ratio,
            "duration_target": self.duration_target,
            "export_preset": self.export_preset,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "Deliverable":
        return Deliverable(
            deliverable_id=str(data["deliverable_id"]),
            aspect_ratio=str(data["aspect_ratio"]),
            duration_target=int(data["duration_target"]),
            export_preset=str(data["export_preset"]),
        )


@dataclass(frozen=True)
class EditorialBrief:
    """创作合同：用户可编辑、可版本化、可验证（TAEP §5.3）。

    没有它，模型只能按“好看”这一主观默认目标工作。它是 AI-native 剪辑的核心。
    每次变更产生新 revision，并使受影响的 variant 标为 ``needs_replan``，但不会
    删除旧方案。
    """

    brief_id: str
    revision: int = 0
    # 目标
    primary_goal: str = ""
    audience: tuple[str, ...] = ()
    platform: Platform = Platform.CUSTOM
    target_duration: DurationTarget | None = None
    language: str = "zh-CN"
    # 叙事
    required_beats: tuple[str, ...] = ()  # 如 ("hook", "problem", "product_value", "call_to_action")
    priority_order: tuple[str, ...] = ()  # 如 ("clarity", "pace", "brand_safety")
    tone: tuple[str, ...] = ()  # 如 ("confident", "warm", "fast_paced")
    # 约束
    hard_constraints: tuple[HardConstraint, ...] = ()
    soft_preferences: tuple[SoftPreference, ...] = ()
    subtitle_policy: SubtitlePolicy = SubtitlePolicy.NONE
    safe_area_profile: str | None = None
    approval_required_for: tuple[str, ...] = ()  # 如 ("copyright_risk", "generated_media")
    # 交付
    deliverables: tuple[Deliverable, ...] = ()
    # 品牌元素交付要求（JSON-safe；实测 bbox/safe_area 在导出阶段注入）
    brand_requirements: tuple[dict[str, Any], ...] = ()

    def __post_init__(self) -> None:
        if not self.brief_id:
            raise ValueError("brief_id cannot be empty")
        if self.revision < 0:
            raise ValueError(f"revision must be >= 0, got {self.revision}")
        from collections.abc import Mapping

        from src.features.edit.ai_editing.delivery_contracts import (
            BrandElementRequirement,
        )

        for requirement in self.brand_requirements:
            if not isinstance(requirement, Mapping):
                raise TypeError("brand_requirements must contain JSON objects")
            BrandElementRequirement.from_dict(dict(requirement))

    def with_revision_bumped(self) -> "EditorialBrief":
        """产生新 revision 的 brief（用户批准 patch 后调用）。"""
        from dataclasses import replace

        return replace(self, revision=self.revision + 1)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "brief_id": self.brief_id,
            "revision": self.revision,
            "primary_goal": self.primary_goal,
            "audience": list(self.audience),
            "platform": self.platform.value,
            "language": self.language,
            "required_beats": list(self.required_beats),
            "priority_order": list(self.priority_order),
            "tone": list(self.tone),
            "hard_constraints": [c.to_dict() for c in self.hard_constraints],
            "soft_preferences": [p.to_dict() for p in self.soft_preferences],
            "subtitle_policy": self.subtitle_policy.value,
            "approval_required_for": list(self.approval_required_for),
            "deliverables": [d.to_dict() for d in self.deliverables],
        }
        if self.brand_requirements:
            d["brand_requirements"] = [dict(item) for item in self.brand_requirements]
        if self.target_duration is not None:
            d["target_duration"] = self.target_duration.to_dict()
        if self.safe_area_profile is not None:
            d["safe_area_profile"] = self.safe_area_profile
        return d

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "EditorialBrief":
        td = data.get("target_duration")
        return EditorialBrief(
            brief_id=str(data["brief_id"]),
            revision=int(data.get("revision", 0)),
            primary_goal=str(data.get("primary_goal", "")),
            audience=tuple(data.get("audience", [])),
            platform=Platform(data.get("platform", "custom")),
            target_duration=DurationTarget.from_dict(td) if td else None,
            language=str(data.get("language", "zh-CN")),
            required_beats=tuple(data.get("required_beats", [])),
            priority_order=tuple(data.get("priority_order", [])),
            tone=tuple(data.get("tone", [])),
            hard_constraints=tuple(
                HardConstraint.from_dict(c) for c in data.get("hard_constraints", [])
            ),
            soft_preferences=tuple(
                SoftPreference.from_dict(p) for p in data.get("soft_preferences", [])
            ),
            subtitle_policy=SubtitlePolicy(data.get("subtitle_policy", "none")),
            safe_area_profile=data.get("safe_area_profile"),
            approval_required_for=tuple(data.get("approval_required_for", [])),
            deliverables=tuple(
                Deliverable.from_dict(d) for d in data.get("deliverables", [])
            ),
            brand_requirements=tuple(
                dict(item) for item in data.get("brand_requirements", [])
            ),
        )


# ---------------------------------------------------------------------- #
# 方案与决策：StoryOutline / EditVariant / EditDecision / UserOverride
# ---------------------------------------------------------------------- #


@dataclass(frozen=True)
class StoryBeat:
    """叙事槽位：某 beat 的目标时长与证据引用（不是时间线）。"""

    beat_id: str  # 如 "hook" / "product_value" / "call_to_action"
    target_range: FrameRange  # 在目标成片中的大致位置
    evidence: tuple[str, ...] = ()  # SemanticSegment.segment_id 引用
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "beat_id": self.beat_id,
            "target_range": self.target_range.to_dict(),
            "evidence": list(self.evidence),
            "notes": self.notes,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "StoryBeat":
        return StoryBeat(
            beat_id=str(data["beat_id"]),
            target_range=FrameRange.from_dict(data["target_range"]),
            evidence=tuple(data.get("evidence", [])),
            notes=str(data.get("notes", "")),
        )


class VariantStatus(str, Enum):
    """EditVariant 生命周期状态。"""

    PROPOSED = "proposed"
    SELECTED = "selected"  # 用户选为候选
    ACCEPTED = "accepted"  # 已提交为 canonical timeline revision
    STALE = "stale"  # brief/analysis 变更后过期，仍可比较
    FAILED = "failed"
    SUPERSEDED = "superseded"


class DecisionState(str, Enum):
    """EditDecision 状态机（TAEP §5.5）。"""

    PROPOSED = "proposed"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    USER_OVERRIDE = "user_override"
    SUPERSEDED = "superseded"
    LOCKED = "locked"  # 用户锁定，下一轮规划必须保留


@dataclass(frozen=True)
class EditDecision:
    """单条可审计的编辑决策：选择/排除某片段，附证据与备选项。

    关键规则（TAEP §5.5）：用户拒绝“删口误”后，该拒绝成为下一轮规划的约束。
    """

    decision_id: str
    action: str  # 如 "select_source_range" / "exclude" / "add_transition" / "set_music"
    subject: SourceRef | None = None  # 涉及的素材范围（可能为空，如 set_music）
    supports: tuple[str, ...] = ()  # 支持的 beat / claim id
    evidence_refs: tuple[str, ...] = ()  # SemanticSegment.segment_id
    alternatives_considered: tuple[str, ...] = ()  # 其他候选 segment/asset
    confidence: float = 0.0  # 仅排序用，不替代人工判断
    explanation: str = ""
    state: DecisionState = DecisionState.PROPOSED

    def __post_init__(self) -> None:
        if not self.decision_id:
            raise ValueError("decision_id cannot be empty")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError(f"confidence must be in [0,1], got {self.confidence}")

    def with_state(self, state: DecisionState) -> "EditDecision":
        from dataclasses import replace

        return replace(self, state=state)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "decision_id": self.decision_id,
            "action": self.action,
            "supports": list(self.supports),
            "evidence_refs": list(self.evidence_refs),
            "alternatives_considered": list(self.alternatives_considered),
            "confidence": self.confidence,
            "explanation": self.explanation,
            "state": self.state.value,
        }
        if self.subject is not None:
            d["subject"] = self.subject.to_dict()
        return d

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "EditDecision":
        subj = data.get("subject")
        return EditDecision(
            decision_id=str(data["decision_id"]),
            action=str(data["action"]),
            subject=SourceRef.from_dict(subj) if subj else None,
            supports=tuple(data.get("supports", [])),
            evidence_refs=tuple(data.get("evidence_refs", [])),
            alternatives_considered=tuple(data.get("alternatives_considered", [])),
            confidence=float(data.get("confidence", 0.0)),
            explanation=str(data.get("explanation", "")),
            state=DecisionState(data.get("state", "proposed")),
        )


@dataclass(frozen=True)
class VariantScores:
    """仅用于排序的评分（hard constraint 必须 pass/fail，不参与这里）。

    AI 分数只能帮助排序；必须同时返回 evidence 与备选项（见 EditDecision）。
    用户不能被迫相信单一分数（TAEP §5.5 关键规则）。
    """

    hard_constraints_passed: bool = True
    duration_fit: float = 0.0  # 0..1
    beat_coverage: float = 0.0  # 0..1
    pace_estimate: float = 0.0  # 0..1
    explanation: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "hard_constraints_passed": self.hard_constraints_passed,
            "duration_fit": self.duration_fit,
            "beat_coverage": self.beat_coverage,
            "pace_estimate": self.pace_estimate,
            "explanation": self.explanation,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "VariantScores":
        return VariantScores(
            hard_constraints_passed=bool(data.get("hard_constraints_passed", True)),
            duration_fit=float(data.get("duration_fit", 0.0)),
            beat_coverage=float(data.get("beat_coverage", 0.0)),
            pace_estimate=float(data.get("pace_estimate", 0.0)),
            explanation=str(data.get("explanation", "")),
        )


@dataclass(frozen=True)
class EditVariant:
    """一个可命名的剪辑方案（不可变 snapshot）。

    可有多个并存版本（A 口播优先 / B B-roll 优先 / C 节奏更快）。接受一个 variant
    会产生一个新的 canonical timeline revision，而不是覆写其他备选方案（TAEP §5.5）。
    """

    variant_id: str
    brief_id: str
    brief_revision: int
    analysis_snapshot_hash: str = ""  # 规划时所基于的分析快照
    status: VariantStatus = VariantStatus.PROPOSED
    label: str = ""  # 人类可读名，如 "A 口播优先"
    outline: tuple[StoryBeat, ...] = ()
    decisions: tuple[EditDecision, ...] = ()
    scores: VariantScores = field(default_factory=VariantScores)
    edit_plan_ref: str | None = None  # 关联的 EditPlan.plan_id

    def __post_init__(self) -> None:
        if not self.variant_id:
            raise ValueError("variant_id cannot be empty")
        if not self.brief_id:
            raise ValueError("brief_id cannot be empty")

    def with_status(self, status: VariantStatus) -> "EditVariant":
        from dataclasses import replace

        return replace(self, status=status)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "variant_id": self.variant_id,
            "brief_id": self.brief_id,
            "brief_revision": self.brief_revision,
            "analysis_snapshot_hash": self.analysis_snapshot_hash,
            "status": self.status.value,
            "label": self.label,
            "outline": [b.to_dict() for b in self.outline],
            "decisions": [d.to_dict() for d in self.decisions],
            "scores": self.scores.to_dict(),
        }
        if self.edit_plan_ref is not None:
            d["edit_plan_ref"] = self.edit_plan_ref
        return d

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "EditVariant":
        return EditVariant(
            variant_id=str(data["variant_id"]),
            brief_id=str(data["brief_id"]),
            brief_revision=int(data.get("brief_revision", 0)),
            analysis_snapshot_hash=str(data.get("analysis_snapshot_hash", "")),
            status=VariantStatus(data.get("status", "proposed")),
            label=str(data.get("label", "")),
            outline=tuple(StoryBeat.from_dict(b) for b in data.get("outline", [])),
            decisions=tuple(EditDecision.from_dict(d) for d in data.get("decisions", [])),
            scores=VariantScores.from_dict(data.get("scores", {})),
            edit_plan_ref=data.get("edit_plan_ref"),
        )


@dataclass(frozen=True)
class UserOverride:
    """用户在时间线上的手工修改，回灌为下一轮规划的约束（TAEP §5.5 / §6）。

    人工精修必须能以最小粒度映射为 user_override，并标出原 AI decision 是否仍有效。
    重规划默认保留锁定/已接受的 override，不被抹掉。
    """

    override_id: str
    target_clip_id: str | None = None  # 修改的 clip（None=结构性，如新增轨）
    related_decision_id: str | None = None  # 关联的原 AI decision
    kind: str = ""  # 如 "trim" / "move" / "delete" / "replace_source" / "lock"
    detail: dict[str, Any] = field(default_factory=dict)
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "override_id": self.override_id,
            "target_clip_id": self.target_clip_id,
            "related_decision_id": self.related_decision_id,
            "kind": self.kind,
            "detail": dict(self.detail),
            "note": self.note,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "UserOverride":
        return UserOverride(
            override_id=str(data["override_id"]),
            target_clip_id=data.get("target_clip_id"),
            related_decision_id=data.get("related_decision_id"),
            kind=str(data.get("kind", "")),
            detail=dict(data.get("detail", {})),
            note=str(data.get("note", "")),
        )


# ---------------------------------------------------------------------- #
# 素材语义：SemanticSegment（AssetAnalysis 在 P5 详细实现，此处先定义 segment）
# ---------------------------------------------------------------------- #


class SegmentKind(str, Enum):
    """SemanticSegment 类型（TAEP §5.4）。"""

    TRANSCRIPT = "transcript"
    SHOT = "shot"
    TAG = "tag"
    OCR = "ocr"
    AUDIO = "audio"
    USER_NOTE = "user_note"
    # vault 桥接新增（P1-6）：把 vault 的画面/人脸/角色检索结果映射成 segment，
    # 让 AI 剪辑 Executor 能消费 vault 的打标数据。
    VISUAL = "visual"        # 画面 caption/向量命中的镜头片段
    FACE = "face"            # 人脸检测片段（含 bbox/角度）
    CHARACTER = "character"  # 角色出场片段（聚类后的同一角色）


@dataclass(frozen=True)
class SemanticSegment:
    """归一化的素材语义片段（TAEP §5.4 最小字段 + M2 PTS/时间基）。

    允许 AI 精确说出“选择 a-interview 的 1250–1425 帧，因为这段覆盖
    claim-fast-charge，且用户已确认转录”。

    M2 扩展（§18.2）：保留 provider 原始 PTS / 时间基，使证据可精确回到 source frame。
    ``source_pts_*`` / ``source_timebase`` 为可选（默认 None）——旧 segment 不携带，
    新 provider（WhisperX/OCR）经 ``TimebaseService`` 转换时填入，便于审计与失效判定。
    """

    segment_id: str
    asset_id: str
    source_range: FrameRange
    kind: SegmentKind
    payload: dict[str, Any] = field(default_factory=dict)
    confidence: float = 1.0
    evidence_ref: str | None = None
    provenance: dict[str, Any] = field(default_factory=dict)  # provider/model/version
    user_verified: bool = False
    # M2: provider 原始时间坐标（可选；None = 旧 segment 或 provider 未给出 PTS）
    source_pts_start: int | None = None
    source_pts_end: int | None = None
    source_timebase: str | None = None  # 如 "1/90000" 或 "sample_rate=48000"

    def __post_init__(self) -> None:
        if not self.segment_id:
            raise ValueError("segment_id cannot be empty")
        if not self.asset_id:
            raise ValueError("asset_id cannot be empty")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError(f"confidence must be in [0,1], got {self.confidence}")

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "segment_id": self.segment_id,
            "asset_id": self.asset_id,
            "source_range": self.source_range.to_dict(),
            "kind": self.kind.value,
            "payload": dict(self.payload),
            "confidence": self.confidence,
            "user_verified": self.user_verified,
        }
        if self.evidence_ref is not None:
            d["evidence_ref"] = self.evidence_ref
        if self.provenance:
            d["provenance"] = dict(self.provenance)
        # M2: 仅在携带时写入（保持旧 payload 兼容）
        if self.source_pts_start is not None:
            d["source_pts_start"] = self.source_pts_start
        if self.source_pts_end is not None:
            d["source_pts_end"] = self.source_pts_end
        if self.source_timebase is not None:
            d["source_timebase"] = self.source_timebase
        return d

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "SemanticSegment":
        return SemanticSegment(
            segment_id=str(data["segment_id"]),
            asset_id=str(data["asset_id"]),
            source_range=FrameRange.from_dict(data["source_range"]),
            kind=SegmentKind(data["kind"]),
            payload=dict(data.get("payload", {})),
            confidence=float(data.get("confidence", 1.0)),
            evidence_ref=data.get("evidence_ref"),
            provenance=dict(data.get("provenance", {})),
            user_verified=bool(data.get("user_verified", False)),
            source_pts_start=_opt_int(data.get("source_pts_start")),
            source_pts_end=_opt_int(data.get("source_pts_end")),
            source_timebase=data.get("source_timebase"),
        )


def _opt_int(v: Any) -> int | None:
    """可选整数读取（None 透传；M2 SemanticSegment PTS 字段用）。"""
    if v is None:
        return None
    return int(v)
