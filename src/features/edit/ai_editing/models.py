"""TAEP 数据合同：EditPlan / operation union / ValidationReport / ProjectSnapshot /
CapabilityManifest（Phase 1）。

这些是 LLM/MCP 客户端与 Topos 之间的“稳定合同”。JSON 对 LLM 工具调用、
JSON Schema 校验、diff、审计最友好（TAEP §5）。XML adapter 从同一 canonical
model 生成，不要求模型手写 XML。

设计要点：

- 所有 operation 是 frozen dataclass，带 ``op_id`` 与 ``intent``（reason），便于审计。
- ``EditPlan`` 携带 ``base_revision``（乐观锁）、``mode``（preview/commit/dry_run_export）
  以及对 brief/variant/decision 的可追溯引用。
- ``ProjectSnapshot`` 是脱敏的只读世界状态（不含本机绝对路径/secret/二进制）。
- ``CapabilityManifest`` 从实际 registry 与 feature flag 生成，不手工复制效果表
  （这样未实现的参数不会被模型调用）。

参考：docs/ai_editing_protocol_and_implementation_plan_2026-07-31.md §5.1, §5.2, §5.6, §5.8
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from src.features.edit.ai_editing.diagnostics import Diagnostic
from src.features.edit.ai_editing.editorial_models import FrameRange
from src.features.edit.core.transition_catalog import TRANSITION_TYPE_OPTIONS


TAEP_PROTOCOL_VERSION = "taep/1.0"


# ---------------------------------------------------------------------- #
# Operation union（首版白名单，TAEP §5.7）
# ---------------------------------------------------------------------- #


class OperationKind(str, Enum):
    """首版 operation 白名单（TAEP §5.7）。

    未在白名单内的能力（ripple_delete、多机位、嵌套、完整关键帧、生成式视频、
    复杂混音、合成图编辑、多人协作合并）必须在 manifest 中显示为未支持，直到
    各自有明确的 domain command 与测试。
    """

    # 结构
    CREATE_TIMELINE = "create_timeline"
    ENSURE_TRACK = "ensure_track"
    RENAME_TRACK = "rename_track"
    SET_TRACK_STATE = "set_track_state"
    # 基础剪辑
    ADD_CLIP = "add_clip"
    MOVE_CLIP = "move_clip"
    TRIM_CLIP = "trim_clip"
    SPLIT_CLIP = "split_clip"
    DELETE_CLIP = "delete_clip"
    REPLACE_CLIP = "replace_clip"
    # 转场/淡化
    ADD_TRANSITION = "add_transition"
    REMOVE_TRANSITION = "remove_transition"
    SET_CLIP_FADE = "set_clip_fade"
    # 图像处理
    SET_TRANSFORM = "set_transform"
    SET_CROP = "set_crop"
    SET_OPACITY = "set_opacity"
    SET_SPEED = "set_speed"
    SET_EFFECT = "set_effect"
    REMOVE_EFFECT = "remove_effect"
    # 字幕
    ENSURE_SUBTITLE_TRACK = "ensure_subtitle_track"
    UPDATE_SUBTITLE_TRACK = "update_subtitle_track"
    ADD_SUBTITLE = "add_subtitle"
    UPDATE_SUBTITLE = "update_subtitle"
    DELETE_SUBTITLE = "delete_subtitle"
    # 字幕图形片段（仍然通过 EditPlan → MacroCommand 提交）
    PROMOTE_SUBTITLE_TO_GRAPHIC = "promote_subtitle_to_graphic"
    APPLY_CAPTION_GRAPHIC_TEMPLATE = "apply_caption_graphic_template"
    UPDATE_CAPTION_GRAPHIC_PARAMETERS = "update_caption_graphic_parameters"
    DETACH_CAPTION_GRAPHIC = "detach_caption_graphic"
    DEMOTE_GRAPHIC_TO_SUBTITLE = "demote_graphic_to_subtitle"
    # 标记/审核
    SET_MARKER = "set_marker"
    SET_RANGE = "set_range"
    ADD_REVIEW_NOTE = "add_review_note"
    # 导出请求
    REQUEST_EXPORT = "request_export"


# 只读工具（非 EditPlan operation，TAEP §5.7）
READONLY_TOOLS: tuple[str, ...] = (
    "get_snapshot",
    "find_assets",
    "get_analysis",
    "get_capabilities",
)


@dataclass(frozen=True)
class EditOperation:
    """单个 TAEP 声明式操作。

    采用扁平字段而非 polymorphic 子类：对 LLM 工具调用与 JSON Schema 更友好，
    校验也更直接。``kind`` 决定哪些字段是必填。

    Attributes:
        op_id: 操作 id（计划内唯一），便于诊断定位与审计。
        kind: ``OperationKind``。
        params: 该 kind 的参数（见 schema.py 的 per-kind 校验）。
        intent: 人类可读 reason（审计用，不影响执行）。
    """

    op_id: str
    kind: OperationKind
    params: dict[str, Any] = field(default_factory=dict)
    intent: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.op_id:
            raise ValueError("op_id cannot be empty")

    def to_dict(self) -> dict[str, Any]:
        return {
            "op_id": self.op_id,
            "kind": self.kind.value,
            "params": dict(self.params),
            "intent": dict(self.intent),
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "EditOperation":
        return EditOperation(
            op_id=str(data["op_id"]),
            kind=OperationKind(data["kind"]),
            params=dict(data.get("params", {})),
            intent=dict(data.get("intent", {})),
        )


# ---------------------------------------------------------------------- #
# EditPlan
# ---------------------------------------------------------------------- #


class PlanMode(str, Enum):
    """EditPlan 执行模式（TAEP §5.6）。

    外部 LLM 初始权限只能提交 ``PREVIEW``；``COMMIT`` 和最终 ``export`` 必须由
    UI 或受信任自动化策略批准。
    """

    PREVIEW = "preview"  # 编译 + 校验 + diff，不写工程
    COMMIT = "commit"  # 原子提交为 MacroCommand（需 approval token）
    DRY_RUN_EXPORT = "dry_run_export"  # 跑导出 preflight 但不写文件


@dataclass(frozen=True)
class PlanTarget:
    """计划作用目标。"""

    project_id: str
    timeline_id: str


@dataclass(frozen=True)
class RequestedOutput:
    """计划请求的产物（预览/导出）。"""

    kind: str  # "preview" / "export"
    range: FrameRange | None = None
    preset: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"kind": self.kind}
        if self.range is not None:
            d["range"] = self.range.to_dict()
        if self.preset is not None:
            d["preset"] = self.preset
        return d


@dataclass(frozen=True)
class EditPlan:
    """AI 的写入单位（TAEP §5.6）。

    支持预览、显示 diff、原子提交、撤销与审计。携带对 brief/variant/decision 的
    可追溯引用，使“为什么这么剪”可审计。
    """

    plan_id: str
    base_revision: int
    target: PlanTarget
    mode: PlanMode = PlanMode.PREVIEW
    protocol: str = TAEP_PROTOCOL_VERSION
    title: str = ""
    rationale: str = ""
    operations: tuple[EditOperation, ...] = ()
    requested_outputs: tuple[RequestedOutput, ...] = ()
    # 可追溯引用（TAEP §3.4 ID 规则）
    brief_id: str | None = None
    variant_id: str | None = None
    decision_refs: tuple[str, ...] = ()
    analysis_snapshot_hash: str | None = None

    def __post_init__(self) -> None:
        if not self.plan_id:
            raise ValueError("plan_id cannot be empty")
        if self.base_revision < 0:
            raise ValueError(f"base_revision must be >= 0, got {self.base_revision}")

    @staticmethod
    def create(
        target: PlanTarget,
        base_revision: int,
        operations: tuple[EditOperation, ...] = (),
        *,
        mode: PlanMode = PlanMode.PREVIEW,
        title: str = "",
        rationale: str = "",
        plan_id: str | None = None,
        brief_id: str | None = None,
        variant_id: str | None = None,
        decision_refs: tuple[str, ...] = (),
        analysis_snapshot_hash: str | None = None,
        requested_outputs: tuple[RequestedOutput, ...] = (),
    ) -> "EditPlan":
        return EditPlan(
            plan_id=plan_id or str(uuid.uuid4()),
            base_revision=base_revision,
            target=target,
            mode=mode,
            title=title,
            rationale=rationale,
            operations=operations,
            requested_outputs=requested_outputs,
            brief_id=brief_id,
            variant_id=variant_id,
            decision_refs=decision_refs,
            analysis_snapshot_hash=analysis_snapshot_hash,
        )

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "protocol": self.protocol,
            "plan_id": self.plan_id,
            "base_revision": self.base_revision,
            "target": {"project_id": self.target.project_id, "timeline_id": self.target.timeline_id},
            "mode": self.mode.value,
            "title": self.title,
            "rationale": self.rationale,
            "operations": [o.to_dict() for o in self.operations],
            "requested_outputs": [o.to_dict() for o in self.requested_outputs],
            "decision_refs": list(self.decision_refs),
        }
        if self.brief_id is not None:
            d["brief_id"] = self.brief_id
        if self.variant_id is not None:
            d["variant_id"] = self.variant_id
        if self.analysis_snapshot_hash is not None:
            d["analysis_snapshot_hash"] = self.analysis_snapshot_hash
        return d

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "EditPlan":
        tgt = data["target"]
        return EditPlan(
            plan_id=str(data["plan_id"]),
            base_revision=int(data["base_revision"]),
            target=PlanTarget(
                project_id=str(tgt["project_id"]), timeline_id=str(tgt["timeline_id"])
            ),
            mode=PlanMode(data.get("mode", "preview")),
            protocol=str(data.get("protocol", TAEP_PROTOCOL_VERSION)),
            title=str(data.get("title", "")),
            rationale=str(data.get("rationale", "")),
            operations=tuple(EditOperation.from_dict(o) for o in data.get("operations", [])),
            requested_outputs=tuple(
                RequestedOutput(
                    kind=str(ro["kind"]),
                    range=FrameRange.from_dict(ro["range"]) if ro.get("range") else None,
                    preset=ro.get("preset"),
                )
                for ro in data.get("requested_outputs", [])
            ),
            brief_id=data.get("brief_id"),
            variant_id=data.get("variant_id"),
            decision_refs=tuple(data.get("decision_refs", [])),
            analysis_snapshot_hash=data.get("analysis_snapshot_hash"),
        )


# ---------------------------------------------------------------------- #
# ValidationReport
# ---------------------------------------------------------------------- #


class PlanStatus(str, Enum):
    """EditPlan 校验/提交后的状态。"""

    VALID = "valid"  # 可提交/可预览
    REJECTED = "rejected"  # 有 error，不可提交
    APPLIED = "applied"  # 已原子提交
    STALE = "stale"  # base_revision 已过期
    # AUD-14000：持久化数据含未知/缺失 status 字符串时的降级落点。此前
    # ``ValidationReport.from_dict`` 的容错分支引用了不存在的
    # ``PlanStatus.INVALID``，坏数据反而让回看路径抛 AttributeError。
    INVALID = "invalid"  # 历史/损坏数据：status 未知，不可提交


@dataclass(frozen=True)
class ValidationReport:
    """``validate_edit_plan`` 的返回（TAEP §5.8）。

    诊断按 severity 分组；``has_errors`` 决定是否可提交。
    """

    plan_id: str
    status: PlanStatus
    base_revision: int
    current_revision: int | None = None
    diagnostics: tuple[Diagnostic, ...] = ()
    new_revision: int | None = None  # 仅 COMMIT 成功后
    undo_label: str | None = None

    @property
    def has_errors(self) -> bool:
        return any(d.is_error for d in self.diagnostics)

    @property
    def errors(self) -> tuple[Diagnostic, ...]:
        return tuple(d for d in self.diagnostics if d.is_error)

    @property
    def warnings(self) -> tuple[Diagnostic, ...]:
        from src.features.edit.ai_editing.diagnostics import DiagnosticSeverity

        return tuple(d for d in self.diagnostics if d.severity == DiagnosticSeverity.WARNING)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "plan_id": self.plan_id,
            "status": self.status.value,
            "base_revision": self.base_revision,
            "diagnostics": [d.to_dict() for d in self.diagnostics],
        }
        if self.current_revision is not None:
            d["current_revision"] = self.current_revision
        if self.new_revision is not None:
            d["new_revision"] = self.new_revision
        if self.undo_label is not None:
            d["undo_label"] = self.undo_label
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ValidationReport":
        """从 ``to_dict`` 输出重建（工程重开后回看历史校验结果用）。"""
        from src.features.edit.ai_editing.diagnostics import Diagnostic
        try:
            status = PlanStatus(data.get("status"))
        except ValueError:
            status = PlanStatus.INVALID
        return cls(
            plan_id=str(data.get("plan_id", "")),
            status=status,
            base_revision=int(data.get("base_revision", 0)),
            current_revision=(
                int(data["current_revision"]) if data.get("current_revision") is not None else None
            ),
            diagnostics=tuple(
                Diagnostic.from_dict(d) for d in data.get("diagnostics", [])
            ),
            new_revision=(
                int(data["new_revision"]) if data.get("new_revision") is not None else None
            ),
            undo_label=data.get("undo_label"),
        )


# ---------------------------------------------------------------------- #
# ProjectSnapshot（脱敏只读世界状态，TAEP §5.2）
# ---------------------------------------------------------------------- #


@dataclass(frozen=True)
class SnapshotTrack:
    """snapshot 中的轨道摘要。"""

    track_id: str
    kind: str  # video/audio/subtitle
    index: int
    name: str = ""
    locked: bool = False
    clips: tuple[dict[str, Any], ...] = ()
    caption_metadata: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "track_id": self.track_id,
            "kind": self.kind,
            "index": self.index,
            "name": self.name,
            "locked": self.locked,
            "clips": [dict(c) for c in self.clips],
        }
        if self.caption_metadata is not None:
            d["caption_metadata"] = dict(self.caption_metadata)
        return d


@dataclass(frozen=True)
class SnapshotAsset:
    """snapshot 中的素材摘要（脱敏：不含本机绝对路径）。"""

    asset_id: str
    name: str
    kind: str  # video/audio/image/...
    duration_frames: int | None = None
    analysis_status: str = "unknown"  # unknown/pending/ready
    offline: bool = False

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "asset_id": self.asset_id,
            "name": self.name,
            "kind": self.kind,
            "analysis_status": self.analysis_status,
            "offline": self.offline,
        }
        if self.duration_frames is not None:
            d["duration_frames"] = self.duration_frames
        return d


class DetailLevel(str, Enum):
    """snapshot 详细程度（控制 token 量）。"""

    MINIMAL = "minimal"  # 仅 project/active timeline/asset 计数
    STRUCTURE = "structure"  # + tracks/clips 摘要
    FULL = "full"  # + clip params/marker/字幕摘要


@dataclass(frozen=True)
class ProjectSnapshot:
    """给模型的只读世界状态（TAEP §5.2）。

    snapshot **不是** ``.topos`` 原文件，也不含帧/缩略图/本机绝对路径/secret。
    按 ``detail_level`` 与分页返回。
    """

    protocol: str = TAEP_PROTOCOL_VERSION
    project_id: str = ""
    name: str = ""
    revision: int = 0
    active_timeline: dict[str, Any] | None = None  # 含 tracks
    assets: tuple[SnapshotAsset, ...] = ()
    detail_level: DetailLevel = DetailLevel.STRUCTURE

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "protocol": self.protocol,
            "project_id": self.project_id,
            "name": self.name,
            "revision": self.revision,
            "detail_level": self.detail_level.value,
            "assets": [a.to_dict() for a in self.assets],
        }
        if self.active_timeline is not None:
            d["active_timeline"] = dict(self.active_timeline)
        return d


# ---------------------------------------------------------------------- #
# CapabilityManifest（TAEP §5.1）
# ---------------------------------------------------------------------- #


@dataclass(frozen=True)
class EffectCapability:
    """manifest 中的单个效果能力（从 EffectRegistry 生成）。

    T-P1-20：除参数 schema 外显式携带导出语义（统一
    ``EffectExecutionCapability`` 契约的投影）——AI 规划器据此不会生成
    必然被 Deliver preflight 阻断的 plan；``preview_only`` 效果仅可入
    预览链，``delivery_restriction_code`` 与 preflight/UI 逐字一致。
    """

    effect_type: str
    target: str  # video_clip / audio_clip / track
    parameters: tuple[dict[str, Any], ...] = ()
    exportable: bool = True
    preview_only: bool = False
    unsupported: bool = False
    delivery_restriction_code: str = ""  # 空 = 可交付
    delivery_restriction_message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "effect_type": self.effect_type,
            "target": self.target,
            "parameters": [dict(p) for p in self.parameters],
            "exportable": self.exportable,
            "preview_only": self.preview_only,
            "unsupported": self.unsupported,
            "delivery_restriction": (
                {
                    "code": self.delivery_restriction_code,
                    "message": self.delivery_restriction_message,
                }
                if self.delivery_restriction_code else None
            ),
        }


@dataclass(frozen=True)
class CapabilityManifest:
    """模型每个会话先调 ``get_capabilities()`` 获得的能力表（TAEP §5.1）。

    **必须从实际 registry 与 feature flags 生成**，不能手工复制一份效果表。
    这样未实现的参数不会被模型调用。
    """

    protocol: str = TAEP_PROTOCOL_VERSION
    project_revision: str = ""  # sha256 或 revision 标识
    timebase_support: dict[str, Any] = field(
        default_factory=lambda: {"frame_only_writes": True, "accepted_fps": ["24/1", "25/1", "30/1"]}
    )
    track_types: tuple[str, ...] = ("video", "audio", "subtitle")
    operations: tuple[str, ...] = ()
    transitions: tuple[str, ...] = TRANSITION_TYPE_OPTIONS
    effects: tuple[EffectCapability, ...] = ()
    export: dict[str, Any] = field(
        default_factory=lambda: {"codecs": [], "requires_confirmation": True}
    )
    limits: dict[str, int] = field(
        default_factory=lambda: {"max_operations_per_plan": 500, "max_preview_frames": 300}
    )
    # Composite uses a separate graph plan protocol because its state is not a
    # timeline sequence. Keeping this capability block beside the editorial
    # manifest makes the AI discover the two domains in one call.
    composite: dict[str, Any] = field(default_factory=dict)
    # P0 capability matrix.  Entries are derived from live profile/provider
    # registries by the capability manifest builder; they are informational and
    # never grant permission to execute an operation.
    capabilities: tuple[dict[str, Any], ...] = ()
    # T-P0-00：RuntimeCapabilitySnapshot.to_dict()（四态运行时能力事实）。
    # 空 dict 表示宿主未注入快照（离线/降级路径）——消费方不得把空当成可用。
    runtime: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol": self.protocol,
            "project_revision": self.project_revision,
            "timebase_support": dict(self.timebase_support),
            "track_types": list(self.track_types),
            "operations": list(self.operations),
            "transitions": list(self.transitions),
            "effects": [e.to_dict() for e in self.effects],
            "export": dict(self.export),
            "limits": dict(self.limits),
            "composite": dict(self.composite),
            "capabilities": [dict(item) for item in self.capabilities],
            "runtime": dict(self.runtime),
        }
