"""TAEP M7 — 首个剪辑工作流闭环（口播精剪 / 30秒产品广告 / 字幕初稿）。

本模块是把通用 ``VariantPlanner`` 包装成三个**确定性、固定输入 schema、
带降级策略**的具体用例的薄编排层（产品计划 §11.1 M7 / §5.2）。

设计边界（红线）：

- runner **只做**：输入校验 → 查证据 → 调既有 ``VariantPlanner``（产 A/B variant）
  / 产字幕 artifact → 收集 ``WorkflowResult``。
- runner **不做**：自己实现 plan/commit/undo 逻辑（那是 ``TransactionService`` /
  ``ApprovalService`` / ``AiEditHostBridge`` 的职责）；variant 产出的 EditPlan
  经 ``on_plan_compiled`` 回调进入既有 ``PlanRepository``，与 ``AssistantService``
  的 submit→plan 路径共用同一个 approval/commit/undo 闭环（"应用仍只有一个 Undo
  单元" §11.1 M7）。
- **降级红线**（§11.1 M7 / §17.3）：缺分析证据时不强行产 plan，返回 ``degradation``
  说明 + 空 variants；绝不能把未分析素材伪装为已理解。
- **证据强制引用**（§11.1 M7）：variant 的每条 decision 必须带非空
  ``evidence_refs``（由 ``VariantPlanner`` 保证）；runner 额外校验并标记降级。
- **字幕只来自转录**（M8 边界预演）：OCR/字幕文本是 untrusted data（§9 / §18.1），
  首版字幕初稿只从 ``SegmentKind.TRANSCRIPT`` 生成。

不引入 Skill manifest/registry/version/signature（那是 M9）；不引入子智能体 /
TaskGraph（M9）；不为 ``ADD_SUBTITLE`` 接 compiler（字幕走 typed artifact，编译器
接通是后续批次）。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Protocol

from src.features.edit.ai_editing.artifact_models import (
    ArtifactKind,
    ArtifactSource,
    ScriptArtifact,
    ScriptLine,
)
from src.features.edit.ai_editing.artifact_store import ArtifactOverrideError, ArtifactStore
from src.features.edit.ai_editing.diagnostics import (
    Diagnostic,
    DiagnosticSeverity,
    TaepDiagnosticCode,
    make_diagnostic,
)
from src.features.edit.ai_editing.editorial_models import (
    EditVariant,
    SegmentKind,
    SemanticSegment,
)
from src.features.edit.ai_editing.semantic_index import SemanticIndex, build_index_from_analysis
from src.features.edit.ai_editing.subtitle_draft import (
    SubtitleDraftResult,
    build_subtitle_draft,
)
from src.features.edit.ai_editing.variant_planner import (
    PlannerInput,
    STRATEGY_A_TALKING_HEAD,
    STRATEGY_B_BROLL,
    VariantPlanner,
)


# ---------------------------------------------------------------------- #
# 工作流标识与固定定义
# ---------------------------------------------------------------------- #


class WorkflowId(str, Enum):
    """内置商业剪辑工作流（§11.1 M7 / §5.2 / P5）。"""

    VOICEOVER_POLISH = "voiceover_polish"  # 口播精剪
    PRODUCT_AD_30S = "product_ad_30s"  # 30 秒产品广告
    INTERVIEW_POLISH = "interview_polish"  # 访谈精剪
    PODCAST_CLIP = "podcast_clip"  # 播客切片
    SUBTITLE_DRAFT = "subtitle_draft"  # 字幕初稿
    TUTORIAL_COURSE = "tutorial_course"  # 教程课程
    MULTILINGUAL_VERSION = "multilingual_version"  # 多语言版本


@dataclass(frozen=True)
class WorkflowSpec:
    """单个工作流的固定定义（确定性，无模型）。

    ``required_evidence_kinds`` 是**必需**证据类型——任一缺失即走降级分支。
    ``produces_plan`` 区分口播/广告（产 EditPlan variant）与字幕（产 artifact，
    首版不落 CommandStack——字幕 operation 尚未在编译器接通，强提交会
    ``CompileError``）。
    """

    workflow_id: WorkflowId
    title: str
    description: str
    required_evidence_kinds: tuple[SegmentKind, ...]
    produces_plan: bool
    produces_artifact_kind: ArtifactKind | None
    target_beats: tuple[str, ...]  # 喂给 planner 的故事 beat
    strategies: tuple[str, ...]  # 喂给 planner 的 A/B 策略
    degrade_to: str  # 缺证据时的降级说明
    brief_template: dict[str, Any]  # brief 模板参数（覆盖默认）


# 三个内置 spec 实例（确定性，首版固定）。
#
# 口播精剪：以转录段为主，hook→产品价值→行动号召，30s 竖版。
#   "去停顿" 在首版用 transcript 覆盖率替代（静音/beat 证据扩展是 M8 子任务），
#   在 degrade_to 里明确标注。
VOICEOVER_POLISH_SPEC = WorkflowSpec(
    workflow_id=WorkflowId.VOICEOVER_POLISH,
    title="口播精剪",
    description="基于转录证据生成 A/B 粗剪方案（口播优先 / B-roll 优先）。",
    required_evidence_kinds=(SegmentKind.TRANSCRIPT,),
    produces_plan=True,
    produces_artifact_kind=None,
    target_beats=("hook", "product_value", "call_to_action"),
    strategies=(STRATEGY_A_TALKING_HEAD, STRATEGY_B_BROLL),
    degrade_to=(
        "缺少转录证据：只能生成低置信度草稿。建议先对所选素材运行 "
        "editorial_local 分析（含转录）。首版'去停顿'依赖转录覆盖率；"
        "静音/beat 级精剪在 M8 音频证据扩展后提供。"
    ),
    brief_template={
        "required_beats": ("hook", "product_value", "call_to_action"),
        "target_duration": {"ideal": 720, "min": 648, "max": 792},
        "platform": "vertical_social",
    },
)

# 30 秒产品广告：beat 增加 "problem"（问题→产品价值→行动），可用 shot/tag 作 B-roll。
PRODUCT_AD_30S_SPEC = WorkflowSpec(
    workflow_id=WorkflowId.PRODUCT_AD_30S,
    title="30 秒产品广告",
    description="产品 claim + 已选素材生成 A/B 广告方案（突出卖点与行动号召）。",
    required_evidence_kinds=(SegmentKind.TRANSCRIPT, SegmentKind.SHOT, SegmentKind.TAG),
    produces_plan=True,
    produces_artifact_kind=None,
    target_beats=("hook", "problem", "product_value", "call_to_action"),
    strategies=(STRATEGY_A_TALKING_HEAD, STRATEGY_B_BROLL),
    degrade_to=(
        "缺少必需证据（转录/镜头/标签）：只能生成低置信度草稿。建议先运行 "
        "editorial_local 或 visual_semantic_local 分析补齐证据。"
    ),
    brief_template={
        "required_beats": ("hook", "problem", "product_value", "call_to_action"),
        "target_duration": {"ideal": 720, "min": 648, "max": 792},
        "platform": "vertical_social",
    },
)

# 访谈精剪：保留问题/洞察/结论，证据不足时明确降级。
INTERVIEW_POLISH_SPEC = WorkflowSpec(
    workflow_id=WorkflowId.INTERVIEW_POLISH,
    title="访谈精剪",
    description="基于转录与镜头证据生成访谈精华 A/B 粗剪方案。",
    required_evidence_kinds=(SegmentKind.TRANSCRIPT, SegmentKind.SHOT),
    produces_plan=True,
    produces_artifact_kind=None,
    target_beats=("hook", "key_insight", "context", "call_to_action"),
    strategies=(STRATEGY_A_TALKING_HEAD, STRATEGY_B_BROLL),
    degrade_to=(
        "访谈精剪需要转录和镜头证据；缺失时不会猜测内容，建议先运行 editorial_local。"
    ),
    brief_template={
        "required_beats": ("hook", "key_insight", "context", "call_to_action"),
        "target_duration": {"ideal": 1440, "min": 960, "max": 2160},
        "platform": "vertical_social",
    },
)

# 播客切片：把人声证据和响度/节拍证据一起纳入工作流门禁。
PODCAST_CLIP_SPEC = WorkflowSpec(
    workflow_id=WorkflowId.PODCAST_CLIP,
    title="播客切片",
    description="基于转录和音频证据生成社媒播客切片方案。",
    required_evidence_kinds=(SegmentKind.TRANSCRIPT, SegmentKind.AUDIO),
    produces_plan=True,
    produces_artifact_kind=None,
    target_beats=("hook", "argument", "payoff"),
    strategies=(STRATEGY_A_TALKING_HEAD, STRATEGY_B_BROLL),
    degrade_to=(
        "播客切片需要转录和音频证据；缺失时不会伪装完成，建议先运行 editorial_local。"
    ),
    brief_template={
        "required_beats": ("hook", "argument", "payoff"),
        "target_duration": {"ideal": 1440, "min": 720, "max": 2160},
        "platform": "vertical_social",
    },
)

# 字幕初稿：只产 typed artifact，不产 EditPlan（字幕 op 不可编译）。
SUBTITLE_DRAFT_SPEC = WorkflowSpec(
    workflow_id=WorkflowId.SUBTITLE_DRAFT,
    title="字幕初稿",
    description="从转录证据生成可编辑的句级字幕草稿（typed artifact）。",
    required_evidence_kinds=(SegmentKind.TRANSCRIPT,),
    produces_plan=False,
    produces_artifact_kind=ArtifactKind.SCRIPT,
    target_beats=(),
    strategies=(),
    degrade_to=(
        "缺少转录证据：无法生成字幕草稿。建议先对所选素材运行 "
        "editorial_local 分析（含转录）。"
    ),
    brief_template={
        "required_beats": (),
        "target_duration": {"ideal": 720, "min": 648, "max": 792},
        "platform": "vertical_social",
    },
)

# 教程课程：章节目标由 brief 提供，首版沿用证据约束的 A/B 粗剪输出。
TUTORIAL_COURSE_SPEC = WorkflowSpec(
    workflow_id=WorkflowId.TUTORIAL_COURSE,
    title="教程课程",
    description="基于讲解转录与镜头证据生成章节化课程粗剪方案。",
    required_evidence_kinds=(SegmentKind.TRANSCRIPT, SegmentKind.SHOT),
    produces_plan=True,
    produces_artifact_kind=None,
    target_beats=("learning_goal", "explanation", "example", "recap"),
    strategies=(STRATEGY_A_TALKING_HEAD, STRATEGY_B_BROLL),
    degrade_to=(
        "教程课程需要转录和镜头证据；缺失时不会猜测章节内容，建议先运行 "
        "editorial_local 分析。"
    ),
    brief_template={
        "required_beats": ("learning_goal", "explanation", "example", "recap"),
        "target_duration": {"ideal": 2880, "min": 1440, "max": 7200},
        "platform": "horizontal",
    },
)

# 多语言版本的模板先落地输入/证据/策略合同；真正翻译、配音与时间线版本
# 需要显式 translation provider，runner 在未注入时返回可见降级，而不是复制原文
# 冒充已完成多语言交付。
MULTILINGUAL_VERSION_SPEC = WorkflowSpec(
    workflow_id=WorkflowId.MULTILINGUAL_VERSION,
    title="多语言版本",
    description="基于原片转录证据准备翻译字幕/配音版本的输入合同。",
    required_evidence_kinds=(SegmentKind.TRANSCRIPT,),
    produces_plan=False,
    produces_artifact_kind=ArtifactKind.SCRIPT,
    target_beats=(),
    strategies=(),
    degrade_to=(
        "多语言版本需要已配置的翻译/TTS provider；当前只完成原片证据检查，"
        "不会把原文复制为目标语言成片。"
    ),
    brief_template={
        "required_beats": (),
        "target_duration": {"ideal": 720, "min": 648, "max": 792},
        "platform": "vertical_social",
    },
)

# 内置 spec 注册表（供 runner 与 UI 查询）。
BUILTIN_SPECS: dict[WorkflowId, WorkflowSpec] = {
    WorkflowId.VOICEOVER_POLISH: VOICEOVER_POLISH_SPEC,
    WorkflowId.PRODUCT_AD_30S: PRODUCT_AD_30S_SPEC,
    WorkflowId.INTERVIEW_POLISH: INTERVIEW_POLISH_SPEC,
    WorkflowId.PODCAST_CLIP: PODCAST_CLIP_SPEC,
    WorkflowId.SUBTITLE_DRAFT: SUBTITLE_DRAFT_SPEC,
    WorkflowId.TUTORIAL_COURSE: TUTORIAL_COURSE_SPEC,
    WorkflowId.MULTILINGUAL_VERSION: MULTILINGUAL_VERSION_SPEC,
}


# ---------------------------------------------------------------------- #
# 工作流输入与产物
# ---------------------------------------------------------------------- #


@dataclass(frozen=True)
class WorkflowInput:
    """工作流统一输入（固定 schema，§11.1 M7）。

    ``asset_ids`` 是工作流作用域——红线：必须来自用户确认的 selection，不是
    "所有素材"（§0 红线：未经确认不得分析整批素材）。planner 首版按主素材
    （``asset_ids[0]``）规划；多素材 B-roll 组合是 M9 子智能体能力的范畴。
    """

    workflow_id: WorkflowId
    asset_ids: tuple[str, ...]
    timeline_id: str
    project_id: str
    base_revision: int
    brief_id: str
    analysis_snapshot_hash: str = ""
    target_duration: int = 720  # 帧（默认 30s@24fps）
    target_language: str = ""  # 多语言版本目标语言；其它 workflow 忽略
    # 节点画布入口（plan §10.7 P9）传入的节点 id 快照（有序全量），供
    # FakeWorkflowService 等发逐节点事件；聊天入口不填 → 退化为单节点。
    node_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class WorkflowResult:
    """工作流产物（只读）。

    不含 approval/grant——那是宿主（``AssistantService`` + ``AiEditHostBridge``）
    的职责。``variants`` 里的 EditPlan 已通过 runner 的 ``on_plan_compiled`` 回调
    进入 ``PlanRepository``，``edit_plan_ref`` 可解析。

    ``degradation`` 非空 = 走了降级分支（证据缺失）；此时 ``variants``/``artifact_id``
    按工作流类型为空或低置信草稿。绝不能在 ``degradation`` 非空时假装产了完整方案。
    """

    workflow_id: WorkflowId
    variants: tuple[EditVariant, ...] = ()
    artifact_id: str | None = None
    degradation: str | None = None
    missing_evidence: tuple[str, ...] = ()
    diagnostics: tuple[dict[str, Any], ...] = ()

    @property
    def degraded(self) -> bool:
        return self.degradation is not None


# ---------------------------------------------------------------------- #
# AnalysisProvider（duck typing，避免硬依赖 AnalysisService）
# ---------------------------------------------------------------------- #


class _AnalysisReader(Protocol):
    """runner 需要的分析读取接口（``AnalysisService`` 已满足）。"""

    def get_status(self, asset_id: str) -> str: ...
    def get_segments(self, asset_id: str) -> tuple[SemanticSegment, ...]: ...


# ---------------------------------------------------------------------- #
# WorkflowRunner
# ---------------------------------------------------------------------- #


class WorkflowRunner:
    """工作流编排器（M7 / §11.1）。

    薄编排层：把通用 ``VariantPlanner`` / ``build_subtitle_draft`` 包装成三个具
    体用例。**只协调，不自己实现 plan/commit/undo**。

    用法::

        runner = WorkflowRunner(planner, analysis_service, brief_service,
                                artifact_store, on_plan_compiled=repo.store)
        result = runner.run(WorkflowInput(...))
        # 口播/广告：result.variants → 宿主 select_variant → validate → approve → commit
        # 字幕：result.artifact_id → ArtifactStore.get(...)
    """

    def __init__(
        self,
        planner: VariantPlanner,
        analysis_service: _AnalysisReader,
        brief_service: Any | None = None,
        artifact_store: ArtifactStore | None = None,
        on_plan_compiled: "Callable[[Any, EditVariant], None] | None" = None,
        translation_provider: Callable[[tuple[SemanticSegment, ...], str], Any] | None = None,
    ) -> None:
        self._planner = planner
        self._analysis = analysis_service
        self._brief = brief_service
        self._artifact_store = artifact_store
        self._on_plan_compiled = on_plan_compiled
        self._translation_provider = translation_provider

    # ---------------- 查询 ---------------- #

    def spec(self, workflow_id: WorkflowId) -> WorkflowSpec:
        """返回内置 spec（未知 id 抛 KeyError）。"""
        return BUILTIN_SPECS[workflow_id]

    def list_specs(self) -> tuple[WorkflowSpec, ...]:
        return tuple(BUILTIN_SPECS.values())

    # ---------------- 输入校验 ---------------- #

    def validate_input(self, inp: WorkflowInput) -> list[Diagnostic]:
        """纯校验，不改状态。返回诊断列表（空 = 通过）。"""
        diags: list[Diagnostic] = []
        spec = BUILTIN_SPECS.get(inp.workflow_id)
        if spec is None:
            diags.append(make_diagnostic(
                DiagnosticSeverity.ERROR, TaepDiagnosticCode.SCHEMA_INVALID,
                details={"workflow_id": str(inp.workflow_id)},
            ))
            return diags
        if not inp.asset_ids:
            diags.append(make_diagnostic(
                DiagnosticSeverity.ERROR, TaepDiagnosticCode.SCHEMA_INVALID,
                details={"reason": "asset_ids must be non-empty"},
            ))
        if not inp.timeline_id or not inp.project_id:
            diags.append(make_diagnostic(
                DiagnosticSeverity.ERROR, TaepDiagnosticCode.SCHEMA_INVALID,
                details={"reason": "timeline_id/project_id required"},
            ))
        if inp.target_duration <= 0:
            diags.append(make_diagnostic(
                DiagnosticSeverity.ERROR, TaepDiagnosticCode.SOURCE_RANGE_INVALID,
                details={"reason": "target_duration must be > 0"},
            ))
        return diags

    # ---------------- 主入口 ---------------- #

    def run(
        self, inp: WorkflowInput, *, emit_stage: Any = None,
    ) -> WorkflowResult:
        """执行工作流（确定性）。

        ``emit_stage``（可选，``Callable[[str], None]``）在真实阶段边界触发：
        ``workflow_started`` → ``evidence_ready``/``evidence_missing`` →
        ``planning_started`` → ``variants_ready``/``artifact_saved``/``degraded``。
        只反映实际发生的事，供节点画布做诚实状态映射（不伪造进度）。
        """
        emit = emit_stage if callable(emit_stage) else (lambda _stage: None)

        diag_dicts: list[dict[str, Any]] = []
        for d in self.validate_input(inp):
            diag_dicts.append(d.to_dict())
        if diag_dicts:
            emit("validation_failed")
            return WorkflowResult(
                workflow_id=inp.workflow_id, diagnostics=tuple(diag_dicts),
            )

        spec = self.spec(inp.workflow_id)
        primary_asset = inp.asset_ids[0]
        emit("workflow_started")

        # 1. 证据就绪检查（红线：未确认/未分析素材不能伪装为已理解）。
        missing = self._collect_missing_evidence(inp.asset_ids, spec)
        if missing:
            emit("evidence_missing")
            emit("degraded")
            # 降级分支：返回说明，不强行产 plan。
            return WorkflowResult(
                workflow_id=inp.workflow_id,
                degradation=spec.degrade_to,
                missing_evidence=tuple(missing),
                diagnostics=tuple(diag_dicts),
            )
        emit("evidence_ready")

        # 2. 证据就绪 → 按工作流类型产出。
        if spec.workflow_id == WorkflowId.SUBTITLE_DRAFT:
            emit("planning_started")
            result = self._run_subtitle(inp, spec, primary_asset, diag_dicts)
        elif spec.workflow_id == WorkflowId.MULTILINGUAL_VERSION:
            emit("planning_started")
            result = self._run_multilingual(inp, primary_asset, diag_dicts)
        else:
            emit("planning_started")
            result = self._run_variant_workflow(
                inp, spec, primary_asset, diag_dicts,
            )
        if result.degradation:
            emit("degraded")
        elif result.variants:
            emit("variants_ready")
        elif result.artifact_id:
            emit("artifact_saved")
        return result

    # ---------------- 内部：variant 工作流（口播/广告） ---------------- #

    def _run_variant_workflow(
        self, inp: WorkflowInput, spec: WorkflowSpec, primary_asset: str,
        diag_dicts: list[dict[str, Any]],
    ) -> WorkflowResult:
        """构造 PlannerInput，调既有 VariantPlanner，收集 A/B variant。"""
        index = self._build_index(inp.asset_ids)
        brief_rev = self._brief_revision(inp.brief_id)
        planner_inp = PlannerInput(
            brief_id=inp.brief_id,
            brief_revision=brief_rev,
            asset_id=primary_asset,
            timeline_id=inp.timeline_id,
            project_id=inp.project_id,
            base_revision=inp.base_revision,
            analysis_snapshot_hash=inp.analysis_snapshot_hash,
            target_duration=inp.target_duration,
        )
        variants = self._planner.plan(
            planner_inp, index,
            target_beats=spec.target_beats,
            strategies=spec.strategies,
            on_plan_compiled=self._on_plan_compiled or _noop_plan_compiled,
        )
        # 证据强制引用红线：若 planner 产出空 evidence 的 decision，标降级。
        low_confidence = self._has_low_confidence_decisions(variants)
        degradation = None
        if low_confidence:
            degradation = (
                "部分剪辑选段缺少证据引用（低置信度草稿）。建议补齐转录/镜头分析后重规划。"
            )
        return WorkflowResult(
            workflow_id=inp.workflow_id,
            variants=tuple(variants),
            degradation=degradation,
            diagnostics=tuple(diag_dicts),
        )

    # ---------------- 内部：字幕工作流 ---------------- #

    def _run_subtitle(
        self, inp: WorkflowInput, spec: WorkflowSpec, primary_asset: str,
        diag_dicts: list[dict[str, Any]],
    ) -> WorkflowResult:
        """从转录证据产 SubtitleDraftResult → ScriptArtifact，存 ArtifactStore。"""
        if self._artifact_store is None:
            diag_dicts.append(make_diagnostic(
                DiagnosticSeverity.ERROR, TaepDiagnosticCode.SCHEMA_INVALID,
                details={"reason": "subtitle workflow requires an ArtifactStore"},
            ).to_dict())
            return WorkflowResult(
                workflow_id=inp.workflow_id, diagnostics=tuple(diag_dicts),
            )

        segments = self._collect_transcript_segments(inp.asset_ids)
        draft = build_subtitle_draft(segments)
        artifact = self._subtitle_draft_to_artifact(draft, inp.brief_id, primary_asset)
        # AUD-15011：同一素材+brief 重跑字幕初稿时，用户已编辑过草稿
        # （user_edited=True）会被 ArtifactStore 拒绝覆盖（§11.1 M6 红线）。
        # 该冲突必须以 WorkflowResult.degradation 表达（模块契约：失败降级
        # 而非异常终止），让用户保留自己的编辑并知道模型未覆盖。
        try:
            saved = self._artifact_store.save(artifact)
        except ArtifactOverrideError:
            return WorkflowResult(
                workflow_id=inp.workflow_id,
                degradation=(
                    "字幕草稿已被你手动编辑，模型不得覆盖（保留当前草稿）。"
                    "如需重新生成，请先在草稿面板重置或另存新 brief。"
                ),
                diagnostics=tuple(diag_dicts),
            )
        return WorkflowResult(
            workflow_id=inp.workflow_id,
            artifact_id=saved.artifact_id,
            diagnostics=tuple(diag_dicts),
        )

    def _run_multilingual(
        self, inp: WorkflowInput, primary_asset: str,
        diag_dicts: list[dict[str, Any]],
    ) -> WorkflowResult:
        """Translate only through an injected provider; never copy source text."""
        if self._translation_provider is None:
            return WorkflowResult(
                workflow_id=inp.workflow_id,
                degradation=MULTILINGUAL_VERSION_SPEC.degrade_to,
                diagnostics=tuple(diag_dicts),
            )
        if self._artifact_store is None:
            diag_dicts.append(make_diagnostic(
                DiagnosticSeverity.ERROR, TaepDiagnosticCode.SCHEMA_INVALID,
                details={"reason": "multilingual workflow requires an ArtifactStore"},
            ).to_dict())
            return WorkflowResult(workflow_id=inp.workflow_id, diagnostics=tuple(diag_dicts))
        target_language = (inp.target_language or "").strip()
        if not target_language:
            return WorkflowResult(
                workflow_id=inp.workflow_id,
                degradation="多语言版本缺少目标语言，未生成翻译产物。",
                diagnostics=tuple(diag_dicts),
            )
        segments = tuple(self._collect_transcript_segments(inp.asset_ids))
        try:
            translated = tuple(self._translation_provider(segments, target_language))
        except Exception as exc:  # noqa: BLE001 - provider failure is visible data
            return WorkflowResult(
                workflow_id=inp.workflow_id,
                degradation=f"翻译 provider 执行失败：{type(exc).__name__}",
                diagnostics=tuple(diag_dicts),
            )
        if not translated or not all(isinstance(line, ScriptLine) for line in translated):
            return WorkflowResult(
                workflow_id=inp.workflow_id,
                degradation="翻译 provider 返回的 typed artifact 无效，未保存。",
                diagnostics=tuple(diag_dicts),
            )
        artifact = ScriptArtifact(
            artifact_id=f"multilingual-{inp.brief_id}-{primary_asset}",
            title=f"多语言版本（{target_language}）",
            lines=translated,
            source=ArtifactSource.SKILL,
            brief_revision=self._brief_revision(inp.brief_id),
        )
        # AUD-15011：与 _run_subtitle 同款——用户编辑过的多语言版本拒绝被
        # 模型重跑结果覆盖，以降级结果而非异常终止。
        try:
            saved = self._artifact_store.save(artifact)
        except ArtifactOverrideError:
            return WorkflowResult(
                workflow_id=inp.workflow_id,
                degradation=(
                    f"多语言版本（{target_language}）已被你手动编辑，模型不得覆盖"
                    "（保留当前版本）。如需重新翻译，请先重置该版本或另存新 brief。"
                ),
                diagnostics=tuple(diag_dicts),
            )
        return WorkflowResult(
            workflow_id=inp.workflow_id,
            artifact_id=saved.artifact_id,
            diagnostics=tuple(diag_dicts),
        )

    # ---------------- 内部：证据收集 ---------------- #

    def _collect_missing_evidence(
        self, asset_ids: tuple[str, ...], spec: WorkflowSpec,
    ) -> list[str]:
        """检查每个 asset 是否就绪且覆盖所有必需证据类型。

        返回缺失描述列表（空 = 就绪）。"就绪" = ``get_status == "ready"`` 且
        至少有一个必需 kind 的 segment。
        """
        missing: list[str] = []
        for asset_id in asset_ids:
            status = self._analysis.get_status(asset_id)
            if status != "ready":
                missing.append(f"{asset_id}: analysis {status!r}（需 ready）")
                continue
            segs = self._analysis.get_segments(asset_id)
            present_kinds = {s.kind for s in segs}
            absent = [k.value for k in spec.required_evidence_kinds if k not in present_kinds]
            if absent:
                missing.append(f"{asset_id}: 缺少证据类型 {absent}")
        return missing

    def _build_index(self, asset_ids: tuple[str, ...]) -> SemanticIndex:
        """从 analysis_service 拉 segments 建 SemanticIndex（供 planner 查询）。

        委托给模块级共享实现 ``build_index_from_analysis``，与 host bridge
        （自由对话/legacy brief 路径 A）共用同一份证据收集逻辑（TAEP §5.4）。
        """
        return build_index_from_analysis(self._analysis, asset_ids)

    def _collect_transcript_segments(
        self, asset_ids: tuple[str, ...],
    ) -> list[SemanticSegment]:
        """只取 TRANSCRIPT kind（M8 边界：OCR 等 untrusted data 不进字幕）。"""
        result: list[SemanticSegment] = []
        for asset_id in asset_ids:
            for seg in self._analysis.get_segments(asset_id):
                if seg.kind == SegmentKind.TRANSCRIPT:
                    result.append(seg)
        # 稳定排序：按 asset → start frame
        result.sort(key=lambda s: (s.asset_id, s.source_range.start))
        return result

    def _has_low_confidence_decisions(self, variants: list[EditVariant]) -> bool:
        """证据强制引用红线：任一 decision 的 evidence_refs 为空 → 降级标记。"""
        for v in variants:
            for d in v.decisions:
                if not d.evidence_refs:
                    return True
        return False

    # ---------------- 内部：辅助 ---------------- #

    def _brief_revision(self, brief_id: str) -> int:
        if self._brief is None:
            return 0
        rev = getattr(self._brief, "current_revision", None)
        if callable(rev):
            try:
                return rev(brief_id) or 0
            except Exception:
                return 0
        return 0

    def _subtitle_draft_to_artifact(
        self, draft: SubtitleDraftResult, brief_id: str, primary_asset: str,
    ) -> ScriptArtifact:
        """把 SubtitleDraftResult 包成 ScriptArtifact（source=SKILL）。

        复用 M6 typed artifact + user-priority-override 红线：source=SKILL 表示
        模型/工作流产物，用户编辑（``with_user_edit``）后模型不能再覆盖。
        """
        lines = tuple(
            ScriptLine(
                line_id=ln.line_id,
                speaker=ln.speaker,
                text=ln.text,
                note=self._subtitle_note(ln, draft),
            )
            for ln in draft.lines
        )
        return ScriptArtifact(
            artifact_id=f"subtitle-draft-{primary_asset}-{brief_id}",
            kind=ArtifactKind.SCRIPT,
            revision=0,
            title="字幕初稿（来自转录证据）",
            lines=lines,
            source=ArtifactSource.SKILL,
            user_edited=False,
            model_id="",
            created_at="",
            brief_revision=0,
        )

    @staticmethod
    def _subtitle_note(line: Any, draft: SubtitleDraftResult) -> str:
        """把可访问性诊断（cps 过快）写进 note（只提示，不阻断）。"""
        if line.line_id in draft.over_fast_lines:
            return f"阅读速度 {line.reading_speed_cps:.1f} cps 过快（>{_DEFAULT_MAX_CPS}）"
        return ""


def _noop_plan_compiled(plan: Any, variant: EditVariant) -> None:
    """默认 on_plan_compiled：丢弃 plan（仅保留 plan_id 引用）。

    与 ``VariantPlanner.plan`` 不提供回调时的旧行为一致。
    """
    return None


# 阈值常量（避免循环导入：subtitle_draft 也定义，此处仅用于 note 文案）。
_DEFAULT_MAX_CPS = 15.0
