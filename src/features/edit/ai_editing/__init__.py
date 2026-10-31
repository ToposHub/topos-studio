"""TAEP — Topos AI Edit Protocol.

本包实现 AI-native 剪辑的协议层、创作模型、规划器、编译器与事务服务。
它是 Edit 产品的“可审计、可重放、可预览”的 AI 创作闭环（详见
``docs/ai_editing_protocol_and_implementation_plan_2026-07-31.md``）。

设计约束（与 .clinerules / AGENTS.md 一致）：

- **唯一事实源**：canonical domain 仍是 ``EditProject`` + ``TimelineSequence`` +
  ``EditCommand``。本包只在其**之上**叠加可版本化的 Brief、语义素材、故事结构、
  方案与决策层；绝不创建第二套“AI 时间线”。
- **三层不混用**：canonical domain / TAEP AI contract / interchange（FCPXML 等）。
- **整帧合同**：所有写入时刻是非负 int 帧号；``[start_frame, end_frame)`` 半开区间；
  fps 保存为 ``Rational``（如 24000/1001）。
- **统一提交入口**：所有 AI/脚本写入经 ``EditProjectService.commit_*``，
  内部编译到 ``EditCommand`` / ``MacroCommand`` 并 bump revision，保持 ref↔sequence
  两层状态与 undo/redo 一致。
- **线程边界**：本包是纯数据/逻辑层，不实例化 Qt widget，不触碰 ModernGL。
"""

from src.features.edit.ai_editing.capability_manifest import build_manifest
from src.features.edit.ai_editing.composition_plugin import (
    AgentPresetPluginHost,
    SkillPluginHost,
    SubagentPluginHost,
)
from src.features.edit.ai_editing.diagnostics import (
    Diagnostic,
    DiagnosticSeverity,
    TaepDiagnosticCode,
)
from src.features.edit.ai_editing.edit_project_service import (
    CommitResult,
    EditProjectService,
    ProjectSequences,
    RevisionConflict,
)
from src.features.edit.ai_editing.edit_capability_plugin import (
    EditCapabilityDescriptor,
    EditCapabilityPlugin,
    EditCapabilityPluginHost,
)
from src.features.edit.ai_editing.editorial_models import (
    DecisionState,
    DurationTarget,
    EditDecision,
    EditVariant,
    EditorialBrief,
    FrameRange,
    HardConstraint,
    Platform,
    SegmentKind,
    SemanticSegment,
    SourceRef,
    StoryBeat,
    UserOverride,
    VariantScores,
    VariantStatus,
)
from src.features.edit.ai_editing.models import (
    CapabilityManifest,
    DetailLevel,
    EditOperation,
    EditPlan,
    OperationKind,
    PlanMode,
    PlanStatus,
    PlanTarget,
    ProjectSnapshot,
    TAEP_PROTOCOL_VERSION,
    ValidationReport,
)
from src.features.edit.ai_editing.plan_validator import validate_plan
from src.features.edit.ai_editing.mcp_plugin import (
    McpPluginDescriptor,
    McpPluginHost,
)
from src.features.edit.ai_editing.provider_plugin import (
    AnalysisPluginHost,
    AnalysisProviderPluginAdapter,
    ProviderDescriptor,
)
from src.features.edit.ai_editing.schema import build_plan_json_schema
from src.features.edit.ai_editing.semantic_index import SemanticIndex
from src.features.edit.ai_editing.snapshot_service import build_snapshot

__all__ = [
    # edit_project_service (P0)
    "CommitResult",
    "EditProjectService",
    "ProjectSequences",
    "RevisionConflict",
    "EditCapabilityDescriptor",
    "EditCapabilityPlugin",
    "EditCapabilityPluginHost",
    # diagnostics (P1)
    "Diagnostic",
    "DiagnosticSeverity",
    "TaepDiagnosticCode",
    # editorial_models (P1)
    "DecisionState",
    "DurationTarget",
    "EditDecision",
    "EditVariant",
    "EditorialBrief",
    "FrameRange",
    "HardConstraint",
    "Platform",
    "SegmentKind",
    "SemanticSegment",
    "SourceRef",
    "StoryBeat",
    "UserOverride",
    "VariantScores",
    "VariantStatus",
    # models (P1)
    "CapabilityManifest",
    "DetailLevel",
    "EditOperation",
    "EditPlan",
    "OperationKind",
    "PlanMode",
    "PlanStatus",
    "PlanTarget",
    "ProjectSnapshot",
    "TAEP_PROTOCOL_VERSION",
    "ValidationReport",
    # services (P1)
    "build_manifest",
    "AgentPresetPluginHost",
    "SkillPluginHost",
    "SubagentPluginHost",
    "build_plan_json_schema",
    "build_snapshot",
    "AnalysisPluginHost",
    "AnalysisProviderPluginAdapter",
    "McpPluginDescriptor",
    "McpPluginHost",
    "ProviderDescriptor",
    "validate_plan",
    "SemanticIndex",
]
