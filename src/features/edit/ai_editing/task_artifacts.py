"""TAEP M9 — 受控子智能体的 typed artifact（§6.2 / §6.3 / §10.1）。

子智能体（``TaskRole`` worker）**只能**产出 typed artifact，绝不能直接写
timeline / 调 commit / 拿独立 approval（§6.2 规则 4 + §11.1 M9 红线："子智能体
永远没有独立 approval"）。本模块定义子任务可交付的四类结构化产物：

- ``ScriptDraft``   — 创意总监（``creative_director``）：受众 / hook / 行动号召。
- ``EvidenceSet``   — 媒体管理员（``media_librarian``）：引用 + 缺口 + 授权状态。
- ``StoryOutline``  — 故事编辑（``story_editor``）：beat 序列 + 分镜建议。
- ``QaReport``      — QA 审查（``qa_reviewer``）：约束 / 时长 / 字幕 / 能力 / 安全区。

设计（遵循 .clinerules / §0 / §1.2 不变量 #2）：

- **不可变 + 版本化 + 来源可追溯**：frozen dataclass，每条带 ``produced_by``
  （角色）+ ``skill_id``/``skill_version``（溯源）+ ``evidence_refs``（强制可追溯，
  与 M7 ``EditDecision.evidence_refs`` 同语义）。
- **不含 approval / commit / timeline 写入**：artifact 是结构化数据，落地需经
  SkillRuntime 收集 → 宿主 ``AssistantService`` 的既有 preview/approve/commit 闭环
  （保持 §11.1 M7 "应用仍只有一个 Undo 单元" 红线）。
- **不可信内容隔离**：artifact 内若含 transcript/OCR 文本，由产出方自行标注；不解析
  为指令（§1.2 不变量 #2："模型与外部内容不可信"）。
- **取消语义**（§6.2 规则 6）：artifact 是已完成子任务的快照；父任务取消不会回滚
  已产出的 artifact，但 SkillRuntime 会停止派发后续子任务，且子任务 artifact 不会
  自动落地为 timeline 写入。

与 M6 ``artifact_models.py`` 的关系：本模块是子智能体产物专用，**不复用**
``ArtifactKind``（那是用户可编辑的创作文档体系）；子智能体产物由 SkillRuntime 聚合
后，需要落地的部分（如脚本文案）再转成 M6 ``ScriptArtifact`` 进 ``ArtifactStore``
（用户优先于模型红线自动继承）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


# ---------------------------------------------------------------------- #
# 公共溯源字段（每个子智能体产物都带）
# ---------------------------------------------------------------------- #


def _norm_refs(refs: tuple[str, ...] | list[str] | None) -> tuple[str, ...]:
    """规范化 evidence_refs：去重保序，去掉空串。"""
    if not refs:
        return ()
    seen: set[str] = set()
    out: list[str] = []
    for r in refs:
        r = str(r).strip()
        if r and r not in seen:
            seen.add(r)
            out.append(r)
    return tuple(out)


# ---------------------------------------------------------------------- #
# ScriptDraft — 创意总监产物
# ---------------------------------------------------------------------- #


@dataclass(frozen=True)
class HookIdea:
    """开场 hook 候选（创意总监产物）。"""

    hook_id: str
    angle: str = ""            # 切入角度（如"痛点前置"）
    draft_text: str = ""      # 草稿文案（可含转录引用，标注 untrusted）
    rationale: str = ""        # 为什么这个 hook 适合受众

    def to_dict(self) -> dict[str, Any]:
        return {
            "hook_id": self.hook_id, "angle": self.angle,
            "draft_text": self.draft_text, "rationale": self.rationale,
        }


@dataclass(frozen=True)
class ScriptDraft:
    """创意总监（``creative_director``）产物：Brief/受众/hook（§6.3）。

    这是结构化的创作方向，不是 timeline 变更；宿主据此更新 ``EditorialBrief`` 或
    作为后续 ``story_editor`` 子任务的输入。
    """

    draft_id: str
    audience: str = ""                 # 目标受众
    tone: str = ""                     # 语气/风格
    hooks: tuple[HookIdea, ...] = ()   # 开场 hook 候选（>=1）
    call_to_action: str = ""           # 行动号召
    produced_by: str = "creative_director"
    skill_id: str = ""
    skill_version: str = ""
    evidence_refs: tuple[str, ...] = ()  # SemanticSegment.segment_id（可空，创意阶段）

    def __post_init__(self) -> None:
        if not self.draft_id:
            raise ValueError("draft_id cannot be empty")
        object.__setattr__(self, "evidence_refs", _norm_refs(self.evidence_refs))

    def to_dict(self) -> dict[str, Any]:
        return {
            "draft_id": self.draft_id, "audience": self.audience, "tone": self.tone,
            "hooks": [h.to_dict() for h in self.hooks],
            "call_to_action": self.call_to_action,
            "produced_by": self.produced_by,
            "skill_id": self.skill_id, "skill_version": self.skill_version,
            "evidence_refs": list(self.evidence_refs),
        }


# ---------------------------------------------------------------------- #
# EvidenceSet — 媒体管理员产物
# ---------------------------------------------------------------------- #


@dataclass(frozen=True)
class EvidenceCitation:
    """单条证据引用（媒体管理员产物）。

    ``segment_id`` 指向 ``SemanticSegment.segment_id``；``trust`` 标记是否可信
    （OCR/网页内容 = "untrusted"，转录/人工标注 = "trusted"）。
    """

    segment_id: str
    asset_id: str = ""
    kind: str = ""            # SegmentKind value（transcript/shot/tag/ocr/...）
    trust: str = "trusted"    # trusted / untrusted（§1.2 不变量 #2）
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "segment_id": self.segment_id, "asset_id": self.asset_id,
            "kind": self.kind, "trust": self.trust, "note": self.note,
        }


@dataclass(frozen=True)
class EvidenceGap:
    """证据缺口（媒体管理员产物：缺什么素材/分析）。"""

    gap_id: str
    description: str = ""     # 缺什么（如"缺少产品特写镜头"）
    severity: str = "info"    # info / warning / blocking
    suggested_action: str = ""  # 建议补救（如"补拍特写"）

    def to_dict(self) -> dict[str, Any]:
        return {
            "gap_id": self.gap_id, "description": self.description,
            "severity": self.severity, "suggested_action": self.suggested_action,
        }


@dataclass(frozen=True)
class EvidenceSet:
    """媒体管理员（``media_librarian``）产物：引用 + 缺口 + 授权（§6.3）。

    红线：``citations`` 内的 untrusted 文本（OCR/网页）保持 ``trust="untrusted"`` 标记，
    不被后续角色当作可信指令。
    """

    set_id: str
    citations: tuple[EvidenceCitation, ...] = ()
    gaps: tuple[EvidenceGap, ...] = ()
    licensing_notes: str = ""          # 授权/版权备注（§6.3 QA 检查项之一）
    produced_by: str = "media_librarian"
    skill_id: str = ""
    skill_version: str = ""
    evidence_refs: tuple[str, ...] = ()  # = citations 的 segment_id 集合（便捷溯源）

    def __post_init__(self) -> None:
        if not self.set_id:
            raise ValueError("set_id cannot be empty")
        object.__setattr__(self, "evidence_refs", _norm_refs(self.evidence_refs))

    @property
    def untrusted_count(self) -> int:
        """不可信引用数（监控用：untrusted 比例高时 QA 应提示）。"""
        return sum(1 for c in self.citations if c.trust == "untrusted")

    def to_dict(self) -> dict[str, Any]:
        return {
            "set_id": self.set_id,
            "citations": [c.to_dict() for c in self.citations],
            "gaps": [g.to_dict() for g in self.gaps],
            "licensing_notes": self.licensing_notes,
            "produced_by": self.produced_by,
            "skill_id": self.skill_id, "skill_version": self.skill_version,
            "evidence_refs": list(self.evidence_refs),
            "untrusted_count": self.untrusted_count,
        }


# ---------------------------------------------------------------------- #
# StoryOutline — 故事编辑产物
# ---------------------------------------------------------------------- #


@dataclass(frozen=True)
class StoryBeat:
    """故事 beat（故事编辑产物）。

    ``evidence_ref`` 强制可追溯：每个 beat 必须指向一条证据（M7 红线在叙事层的延伸）；
    创意型 beat（纯文案）允许空 ref，但 ``confidence`` 相应降低。
    """

    beat_id: str
    label: str = ""           # hook / problem / product_value / call_to_action
    description: str = ""     # 该 beat 讲什么
    evidence_ref: str = ""    # SemanticSegment.segment_id（可空）
    confidence: float = 1.0   # 有证据=1.0；纯文案<1.0
    suggested_range: dict[str, int] | None = None  # {"start":..,"end":..}（帧）

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "beat_id": self.beat_id, "label": self.label,
            "description": self.description, "confidence": self.confidence,
        }
        if self.evidence_ref:
            d["evidence_ref"] = self.evidence_ref
        if self.suggested_range is not None:
            d["suggested_range"] = dict(self.suggested_range)
        return d


@dataclass(frozen=True)
class StoryOutline:
    """故事编辑（``story_editor``）产物：beat 序列 + 分镜建议（§6.3）。

    ``beats`` 按叙事顺序排列；``storyboard_hints`` 是分镜建议（供 rough_cut_planner /
    后续 ``ShotListArtifact`` 参考，本身不写 timeline）。
    """

    outline_id: str
    beats: tuple[StoryBeat, ...] = ()
    storyboard_hints: tuple[str, ...] = ()  # 分镜建议（每条一段描述）
    produced_by: str = "story_editor"
    skill_id: str = ""
    skill_version: str = ""
    evidence_refs: tuple[str, ...] = ()  # beats 的 evidence_ref 集合

    def __post_init__(self) -> None:
        if not self.outline_id:
            raise ValueError("outline_id cannot be empty")
        object.__setattr__(self, "evidence_refs", _norm_refs(self.evidence_refs))

    @property
    def low_confidence_beats(self) -> tuple[str, ...]:
        """缺证据的 beat id（监控用：低置信 beat 多时标记降级）。"""
        return tuple(b.beat_id for b in self.beats if not b.evidence_ref)

    def to_dict(self) -> dict[str, Any]:
        return {
            "outline_id": self.outline_id,
            "beats": [b.to_dict() for b in self.beats],
            "storyboard_hints": list(self.storyboard_hints),
            "produced_by": self.produced_by,
            "skill_id": self.skill_id, "skill_version": self.skill_version,
            "evidence_refs": list(self.evidence_refs),
            "low_confidence_beats": list(self.low_confidence_beats),
        }


# ---------------------------------------------------------------------- #
# QaReport — QA 审查产物
# ---------------------------------------------------------------------- #


@dataclass(frozen=True)
class QaFinding:
    """单条 QA 发现（§6.3：约束/版权/时长/字幕/安全区/能力）。"""

    finding_id: str
    category: str = ""       # constraint / licensing / duration / subtitle / safe_area / capability
    severity: str = "info"   # info / warning / blocking
    message: str = ""
    suggested_action: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "finding_id": self.finding_id, "category": self.category,
            "severity": self.severity, "message": self.message,
            "suggested_action": self.suggested_action,
        }


@dataclass(frozen=True)
class QaReport:
    """QA 审查（``qa_reviewer``）产物（§6.3：QA 审查节点）。

    QA 是只读审查：只产 findings，不批准 / 不写 timeline（§6.1："子智能体是只读
    worker，不是第二个用户"）。``approved`` 表示 QA 视角通过（仍有 blocking finding
    时为 False）；最终落地仍由宿主经用户批准。
    """

    report_id: str
    findings: tuple[QaFinding, ...] = ()
    approved: bool = False           # 无 blocking finding = True（仅 QA 视角）
    summary: str = ""
    produced_by: str = "qa_reviewer"
    skill_id: str = ""
    skill_version: str = ""
    evidence_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.report_id:
            raise ValueError("report_id cannot be empty")
        object.__setattr__(self, "evidence_refs", _norm_refs(self.evidence_refs))
        # 不变量（AUD-14508）：``approved`` 与 ``blocking_findings``/``passes`` 必须
        # 一致——仍有 blocking finding 时 approved 收敛为 False（fail-closed）。
        # 生产方（LLM 路径）可能透传模型原始值，契约层强制对齐：宿主读 ``approved``
        # 不会把未通过 QA 闸门的计划当作已过。
        if self.approved and any(f.severity == "blocking" for f in self.findings):
            object.__setattr__(self, "approved", False)

    @property
    def blocking_findings(self) -> tuple[QaFinding, ...]:
        """blocking 级别的发现（导致 approved=False）。"""
        return tuple(f for f in self.findings if f.severity == "blocking")

    @property
    def passes(self) -> bool:
        """QA 是否通过（无 blocking finding）。"""
        return not self.blocking_findings

    def to_dict(self) -> dict[str, Any]:
        return {
            "report_id": self.report_id,
            "findings": [f.to_dict() for f in self.findings],
            "approved": self.approved,
            "summary": self.summary,
            "produced_by": self.produced_by,
            "skill_id": self.skill_id, "skill_version": self.skill_version,
            "evidence_refs": list(self.evidence_refs),
            "blocking_count": len(self.blocking_findings),
            "passes": self.passes,
        }


# ---------------------------------------------------------------------- #
# 统一类型别名（供 SkillRuntime / TaskGraph 类型注解）
# ---------------------------------------------------------------------- #


# 子任务可交付的 typed artifact 联合类型。
SubagentArtifact = ScriptDraft | EvidenceSet | StoryOutline | QaReport


__all__ = [
    "HookIdea",
    "ScriptDraft",
    "EvidenceCitation",
    "EvidenceGap",
    "EvidenceSet",
    "StoryBeat",
    "StoryOutline",
    "QaFinding",
    "QaReport",
    "SubagentArtifact",
]
