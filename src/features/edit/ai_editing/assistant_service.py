"""TAEP AI Assistant Service（Phase 9，补齐 §11.8）。

AI 编辑助手的会话状态机、request 编排、provider/tool 调用（TAEP §11.5 / §11.8）。

它是 UI（``AiEditAssistantPanel``）与底层服务（``variant_planner`` /
``transaction_service`` / ``tool_facade``）之间的编排层：

- 维护 ``AssistantState`` 状态机（IDLE→GATHERING→…→IDLE）。
- 把用户输入封装为不可变 ``AssistantRequest``（不含绝对路径/secret）。
- 编排：brief patch → 收集上下文 → 规划 variants → 用户选择 → validate → apply。
- 所有写入经 ``EditSession``/``TransactionService``，绝不绕开 command compiler。
- 长任务异步、可取消；不阻塞 UI（TAEP §11.1）。

本服务是**纯逻辑层**：不实例化 Qt widget，不触碰 ModernGL。状态变更通过
``state_changed`` 回调通知 UI（用 callable 而非 pyqtSignal，避免 QObject 依赖）。
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Any, Callable

from src.features.edit.ai_editing.assistant_events import (
    AssistantMessage,
    AssistantRequest,
    AssistantState,
    AssistantStateSnapshot,
    ContextSummary,
    DecisionCard,
    ChatAttachment,
    VariantCard,
)
from src.features.edit.ai_editing.brief_service import BriefPatch, BriefService
from src.features.edit.ai_editing.edit_api import EditSession
from src.features.edit.ai_editing.editorial_models import (
    DecisionState,
    EditDecision,
    EditVariant,
    EditorialBrief,
    VariantStatus,
)
from src.features.edit.ai_editing.plan_repository import PlanRepository
from src.features.edit.ai_editing.transaction_service import TransactionService
from src.features.edit.ai_editing.variant_planner import (
    PlannerInput,
    VariantPlanner,
    compare_variants,
)
# M7：工作流闭环（薄编排层）。延迟 TYPE_CHECKING 之外导入避免循环——workflow.py 依赖
# variant_planner / subtitle_draft / artifact_store，与本文无循环，可直接导入。
from src.features.edit.ai_editing.workflow import (
    WorkflowId,
    WorkflowInput,
    WorkflowResult,
    WorkflowRunner,
)
# M9：Skill Runtime + 受控子智能体。skill_runtime 依赖 skill_manifest / task_graph /
# task_artifacts，与本文无循环，可直接导入。官方 Skill 经 _OfficialWorkflowBackend
# 桥接到既有 WorkflowRunner（roles=() 的官方 Skill 走 M7 单路径，向后兼容）。
from src.features.edit.ai_editing.skill_runtime import (
    DefaultGraphBuilder,
    OfficialResult,
    SkillInput,
    SkillRuntime,
    SkillResult,
)
from src.features.edit.ai_editing.skill_registry import SkillRegistry
from src.features.edit.ai_editing.task_graph import SingleWriterPolicy
from src.features.edit.ai_editing.task_graph import RoleExecutor


# 官方 Skill skill_id ↔ M7 WorkflowId 映射（三个官方 Skill 对应三个内置工作流）。
_SKILL_TO_WORKFLOW: dict[str, WorkflowId] = {
    "topos.voiceover_polish": WorkflowId.VOICEOVER_POLISH,
    "topos.product_ad_30s": WorkflowId.PRODUCT_AD_30S,
    "topos.subtitle_draft": WorkflowId.SUBTITLE_DRAFT,
}

logger = logging.getLogger(__name__)


@dataclass
class AssistantConfig:
    """助手会话配置。"""

    asset_id: str = ""
    timeline_id: str = ""
    project_id: str = ""
    target_duration_frames: int = 720
    target_beats: tuple[str, ...] = ("hook", "product_value", "call_to_action")


class _NullAnalysisReader:
    """空分析读取器（无 analysis_service 注入时的兜底）。

    报告所有 asset 为 ``unknown``、无 segment。使 WorkflowRunner 走降级分支，
    而不是抛错——保证向后兼容（既有测试不注入 analysis_service）。
    """

    def get_status(self, asset_id: str) -> str:
        return "unknown"

    def get_segments(self, asset_id: str) -> tuple:
        return ()


class AssistantService:
    """AI 编辑助手会话编排。

    一个 project 同一时刻只允许一个会影响同一 timeline 的**提交会话**，但可以有
    只读分析/预览 job（TAEP §11.5）。
    """

    def __init__(
        self,
        session: EditSession,
        *,
        brief_service: BriefService | None = None,
        planner: VariantPlanner | None = None,
        plan_repository: PlanRepository | None = None,
        analysis_service: Any | None = None,
        artifact_store: Any | None = None,
        workflow_runner: WorkflowRunner | None = None,
        skill_runtime: SkillRuntime | None = None,
        role_executor: RoleExecutor | None = None,
    ) -> None:
        self._session = session
        self._brief = brief_service or BriefService()
        self._planner = planner or VariantPlanner(self._brief)
        self._txn = TransactionService()
        # M2.1：plan 仓库——planner 编译的 plan 在此存根，variant 的 edit_plan_ref
        # 据此解析。apply 路径必须取仓库里的同一份 immutable plan（预览即提交）。
        self._plans = plan_repository or PlanRepository()
        # M7：工作流闭环依赖。analysis_service 供 WorkflowRunner 查证据状态/segment；
        # artifact_store 供字幕初稿存 typed artifact。均可选（None 时 run_workflow 在
        # 缺依赖的工作流上返回诊断，向后兼容既有测试）。
        self._analysis_service = analysis_service
        self._artifact_store = artifact_store
        self._workflow_runner = workflow_runner
        self._translation_provider: Callable[[tuple[Any, ...], str], Any] | None = None
        # M9：Skill Runtime（官方 Skill 经此入口；roles=() 走 M7 闭环，roles 非空走
        # TaskGraph）。可选——None 时 run_skill 按需惰性构造。单写者 policy 跨工作流
        # 共享，保证同一 timeline 同时至多一个提交候选（§6.2 规则 2）。
        self._skill_runtime = skill_runtime
        self._role_executor = role_executor
        self._single_writer_policy = SingleWriterPolicy()
        self._state: AssistantState = AssistantState.IDLE
        self._variants: dict[str, EditVariant] = {}
        self._decisions: dict[str, EditDecision] = {}
        self._messages: list[AssistantMessage] = []
        # AUD-13009：__init__ 即初始化默认 config——start_session 之前访问
        # ``service.config`` 不再抛 AttributeError（调用方无需 except 防御）。
        # start_session 仍会用真实会话配置整体覆盖。
        self._config: AssistantConfig = AssistantConfig()
        self._active_brief_id: str = ""
        self._active_profile_id: str = ""
        self._profile_connected: bool = False
        self._error: str | None = None
        # M5：可选 profile 查询（宿主注入）。用于派生 allow_media_upload（profile.data_policy）。
        # None 时 allow_media_upload 保持 False（向后兼容）。签名：profile_id → AiProviderProfile | None。
        self._profile_lookup: Callable[[str], Any] | None = None
        # M5：可选 context 摘要查询（宿主注入）。用于派生 ContextSummary。
        self._context_provider: Callable[[str], Any] | None = None
        # P0.6：可选 job 卡片查询（宿主注入 JobRunBridge 后填 snapshot.jobs）。
        # 签名：() -> tuple[JobCard, ...]。None 时 snapshot.jobs 保持 ()（向后兼容）。
        self._job_cards_provider: Callable[[], tuple] | None = None
        # P0.6：当前 chat run 的 run_id + 关联 runtime（cancel() 透传用）。
        # 由 AiEditController.start_chat_run 后调 set_active_run_id 注入。
        self._active_run_id: str = ""
        self._active_runtime: Any = None
        # UI 状态变更回调（controller 订阅后回填面板）
        self.on_state_changed: Callable[[AssistantStateSnapshot], None] | None = None

    # ------------------------------------------------------------------ #
    # 状态机
    # ------------------------------------------------------------------ #

    @property
    def state(self) -> AssistantState:
        return self._state

    # ---- 类型化访问器（供 controller/bridge 使用，避免穿透私有字段，M1.1）---- #

    @property
    def active_brief_id(self) -> str:
        return self._active_brief_id

    @property
    def active_profile_id(self) -> str:
        """当前会话使用的非敏感 profile 标识（供统一 Invocation 审计）。"""
        return self._active_profile_id

    @property
    def config(self) -> "AssistantConfig":
        """当前会话配置。

        AUD-13009：``__init__`` 已初始化默认值，会话启动前访问返回空配置
        （project/timeline 均为空串），由调用方按"无工程"语义处理。
        """
        return self._config

    def get_brief(self, brief_id: str) -> EditorialBrief | None:
        """取当前 brief（不存在返回 None）。"""
        return self._brief.get(brief_id)

    def get_analysis_evidence(
        self, asset_ids: tuple[str, ...], kind: str,
    ) -> tuple[Any, ...]:
        """Return normalized evidence for a workflow/editorial builder.

        The workflow adapter consumes this public seam instead of reaching into
        the analysis service or falling back to an empty synthetic evidence set.
        """
        if self._analysis_service is None:
            return ()
        wanted = str(kind).lower()
        result: list[Any] = []
        for asset_id in asset_ids:
            for segment in self._analysis_service.get_segments(str(asset_id)):
                segment_kind = str(getattr(getattr(segment, "kind", None), "value", ""))
                if not wanted or wanted in segment_kind:
                    result.append(segment)
        return tuple(result)

    def get_variant(self, variant_id: str) -> EditVariant | None:
        """取 variant（不存在返回 None）。"""
        return self._variants.get(variant_id)

    @property
    def plan_repository(self) -> PlanRepository:
        """M2.1：plan 仓库（bridge/controller 经此取 immutable plan）。"""
        return self._plans

    def preview_plan(self, plan: Any, state: Any) -> Any:
        """对 plan 跑预览（validate+compile+simulate+diff），返回 PreviewResult。

        供 controller 在 apply 前生成 diff 给宿主确认框展示。
        """
        try:
            return self._txn.preview(plan, state)
        except Exception as exc:
            self._set_state(AssistantState.FAILED, error=str(exc))
            return None

    def record_error(self, message: str) -> None:
        """记录一个明确错误状态（取代 broad except: pass）。"""
        self._set_state(AssistantState.FAILED, error=message)

    def on_applied(self, variant_id: str, new_timeline: Any) -> None:
        """AI 方案成功提交到真实 CommandStack 后由 controller 调用。

        把 service 状态机回到 IDLE，并记一条 assistant 消息（"可一次撤销"）。
        实际的 timeline 数据 / UI 刷新由 ``execute_command`` → ``timeline_changed``
        链统一处理；本方法只更新会话状态与对话记录。
        """
        rev = getattr(new_timeline, "revision", None)
        msg = AssistantMessage(
            message_id=str(uuid.uuid4()), role="assistant",
            text=(
                f"方案已应用（revision {rev}）。"
                f"该编辑作为单个撤销单元，可一次 Ctrl+Z 整体恢复。"
            ),
            variant_id=variant_id,
        )
        self._messages.append(msg)
        self._set_state(AssistantState.IDLE)

    def _set_state(self, state: AssistantState, *, error: str | None = None) -> None:
        self._state = state
        if error is not None:
            self._error = error
        elif state not in (AssistantState.FAILED,):
            self._error = None
        self._emit_state()

    def _emit_state(self) -> None:
        if self.on_state_changed is None:
            return
        snap = AssistantStateSnapshot(
            state=self._state,
            active_profile_id=self._active_profile_id,
            profile_connected=self._profile_connected,
            # M5：从 profile.data_policy 派生（之前一直默认 False —— 红线 bug：UI 永远显示
            # 不允许上传，即使用户配了允许上传的 profile）。profile_lookup 缺省时保持 False。
            allow_media_upload=self._resolve_allow_media_upload(),
            # M5：派生真实 ContextSummary（之前一直是空默认值）。
            context=self._build_context_summary(),
            # 对话区本身是固定高度的滚动视图，保留完整内存历史让用户可以
            # 向上查看；请求侧的 token 压缩由 controller 单独处理，不截断 UI。
            messages=tuple(self._messages),
            variants=tuple(self._variant_cards()),
            decisions=tuple(self._decision_cards()),
            # P0.6：jobs 由注入的 job_cards_provider 派生（宿主装配 JobRunBridge
            # 后填真实 JobCard）。provider 未注入时保持 ()（向后兼容旧测试）。
            jobs=self._resolve_job_cards(),
            error_message=self._error,
        )
        self.on_state_changed(snap)

    def set_profile_lookup(
        self, lookup: Callable[[str], Any] | None,
    ) -> None:
        """注入 profile 查询 callable（M5：派生 allow_media_upload）。

        签名：profile_id → AiProviderProfile | None。None 时 allow_media_upload 回退 False。
        """
        self._profile_lookup = lookup

    def _resolve_allow_media_upload(self) -> bool:
        """从当前 profile 的 data_policy 派生是否允许上传媒体（§10 数据策略）。"""
        if not self._active_profile_id or self._profile_lookup is None:
            return False
        try:
            profile = self._profile_lookup(self._active_profile_id)
        except Exception:
            return False
        if profile is None:
            return False
        # AiProviderProfile 当前把 allows_media_upload 定义为 property；旧的
        # 注入 profile/测试桩可能仍把它定义成 method。两种契约都要兼容，不能
        # 直接调用 property 返回的 bool（否则启动阶段会抛 "bool is not callable"）。
        allows_upload = getattr(profile, "allows_media_upload", None)
        if callable(allows_upload):
            return bool(allows_upload())
        if allows_upload is not None:
            return bool(allows_upload)
        policy = getattr(profile, "data_policy", None)
        return bool(getattr(policy, "allow_media_upload", False)) if policy else False

    def _build_context_summary(self) -> ContextSummary:
        """派生 ContextSummary（asset/分析/锁定计数；之前一直是空默认值）。

        数据来源有限：service 不直接持工程引用，故依赖宿主经 set_context_provider
        注入的查询；缺省时返回最小 summary（保持 UI 不崩）。
        """
        provider = getattr(self, "_context_provider", None)
        if provider is None:
            return ContextSummary()
        try:
            return ContextSummary(
                asset_count=int(provider("asset_count") or 0),
                analyzed_count=int(provider("analyzed_count") or 0),
                music_count=int(provider("music_count") or 0),
                locked_count=int(provider("locked_count") or 0),
                snapshot_revision=int(provider("snapshot_revision") or 0),
                analysis_fresh=bool(provider("analysis_fresh")),
            )
        except Exception:
            return ContextSummary()

    def set_context_provider(
        self, provider: Callable[[str], Any] | None,
    ) -> None:
        """注入 context 摘要查询 callable（M5：派生 ContextSummary）。

        签名：field_name → 值（asset_count/analyzed_count/music_count/locked_count/
        snapshot_revision/analysis_fresh）。None 时 ContextSummary 回退默认值。
        """
        self._context_provider = provider

    def set_job_cards_provider(
        self, provider: Callable[[], tuple] | None,
    ) -> None:
        """注入 job 卡片查询 callable（P0.6：填 ``snapshot.jobs``）。

        签名：() -> tuple[JobCard, ...]。每次 ``_emit_state`` 都重读，使 snapshot
        随 JobRegistry 状态实时刷新。None 时 snapshot.jobs 保持 ()（向后兼容）。
        """
        self._job_cards_provider = provider

    def _resolve_job_cards(self) -> tuple:
        """从 job_cards_provider 派生 JobCard 元组（provider 异常时回退 ()）。"""
        if self._job_cards_provider is None:
            return ()
        try:
            cards = self._job_cards_provider()
            return tuple(cards) if cards else ()
        except Exception:  # noqa: BLE001
            return ()

    def set_active_run_id(self, run_id: str, *, runtime: Any = None) -> None:
        """注入当前 chat run 的 run_id + 关联 runtime（P0.6：cancel() 透传用）。

        由 ``AiEditController.start_chat_run`` 在 ``runtime.start`` 之后调用。
        runtime 为 None 时 ``cancel()`` 回退到纯状态翻转（向后兼容）。
        """
        self._active_run_id = run_id
        if runtime is not None:
            self._active_runtime = runtime

    def _variant_cards(self) -> list[VariantCard]:
        cards: list[VariantCard] = []
        for v in self._variants.values():
            dur = sum(b.target_range.duration for b in v.outline)
            cards.append(VariantCard(
                variant_id=v.variant_id, label=v.label, duration_frames=dur,
                hard_constraints_passed=v.scores.hard_constraints_passed,
                beat_coverage=v.scores.beat_coverage,
                pace_estimate=v.scores.pace_estimate,
                status=v.status.value,
            ))
        return cards

    def _decision_cards(self) -> list[DecisionCard]:
        return [
            DecisionCard(
                decision_id=d.decision_id,
                beat=", ".join(d.supports),
                description=d.explanation,
                evidence_summary=", ".join(d.evidence_refs),
                state=d.state.value,
            )
            for d in self._decisions.values()
        ]

    # ------------------------------------------------------------------ #
    # 会话生命周期
    # ------------------------------------------------------------------ #

    def start_session(
        self, config: AssistantConfig, *, active_profile_id: str = "",
        profile_connected: bool = False,
    ) -> None:
        """打开/创建会话（TAEP §11.6 步骤 1）。"""
        self._config = config
        self._active_profile_id = active_profile_id
        self._profile_connected = profile_connected
        if not config.project_id or not config.timeline_id:
            self._set_state(AssistantState.NO_PROJECT)
            return
        if not active_profile_id:
            self._set_state(AssistantState.NO_PROFILE)
            return
        self._set_state(AssistantState.IDLE)

    def set_profile(self, profile_id: str, connected: bool) -> None:
        """切换/连接 profile（只影响后续任务，TAEP §11.4）。"""
        self._active_profile_id = profile_id
        self._profile_connected = connected
        if self._state == AssistantState.NO_PROFILE and profile_id:
            self._set_state(AssistantState.IDLE)
        else:
            self._emit_state()

    def set_planner(self, planner: VariantPlanner | None) -> None:
        """切换后续请求使用的 planner；``None`` 回退到确定性 planner。"""
        self._planner = planner or VariantPlanner(self._brief)

    def set_translation_provider(
        self, provider: Callable[[tuple[Any, ...], str], Any] | None,
    ) -> None:
        """Set the provider used by the multilingual subtitle workflow."""
        self._translation_provider = provider

    # ------------------------------------------------------------------ #
    # 自由对话（unknown 分支 → LLM chat）
    # ------------------------------------------------------------------ #

    def append_chat_message(
        self, role: str, text: str, *, error_code: str | None = None,
        attachments: tuple[ChatAttachment, ...] = (),
    ) -> None:
        """追加一条自由对话消息（user/assistant），用于 unknown 分支的 LLM 对话。

        与 ``submit_request`` / ``run_workflow`` 等动作型方法不同：这里不触发
        brief/plan 状态机，只追加消息并刷新 UI。``error_code`` 非空时 UI 用红字
        渲染（错误提示）。``attachments`` 携带多模态图片（粘贴/抓帧），仅存于内存
        会话历史，不持久化为字节。

        契约注：``assistant_events.AssistantMessage`` 文档要求 assistant 消息至少
        关联一个结构化引用，但无 ``__post_init__`` 强制校验——自由对话的纯文本
        消息（所有 ref 为 None）可安全构造。
        """
        self._messages.append(AssistantMessage(
            message_id=str(uuid.uuid4()), role=role, text=text, error_code=error_code,
            attachments=attachments,
        ))
        self._emit_state()

    def recent_chat_messages(self, limit: int = 10) -> list[AssistantMessage]:
        """最近 ``limit`` 条 user/assistant 对话消息（供多轮对话构造上下文）。

        跳过 system 消息；保留有文本或图片附件的消息；按时间正序返回（最旧→最新）。
        """
        return [
            m for m in self._messages
            if m.role in ("user", "assistant") and (m.text or m.attachments)
        ][-limit:]

    def reset_chat_messages(self) -> None:
        """清空内存对话历史（用于新建会话或切换到不含历史的会话）。

        与 ``_messages`` 仅在构造时初始化为空不同，本方法显式清空历史，使
        多轮对话从空白重新开始——这是会话目录（``SessionCatalog``）与内存
        历史保持一致的关键。不清空 variants/decisions（那些由宿主在切换
        会话时另行处理）。
        """
        self._messages.clear()
        self._emit_state()

    def restore_chat_messages(self, messages: "tuple[AssistantMessage, ...]") -> None:
        """把已持久化的消息回灌进内存历史（工程加载 / 会话切换时调用）。

        持久化层（``MessageSummary``）只存脱敏文本 + 角色，无 ``message_id`` 之外
        的 ref（自由对话的纯文本消息契约上允许所有 ref 为 None，见
        ``append_chat_message`` 文档）。本方法据此安全重建 ``AssistantMessage``。

        Args:
            messages: 要恢复的消息元组（已按时间正序排列）。元素会被去重——
                若与现有 ``_messages`` 末尾连续相同则跳过，避免重复回灌。
        """
        if not messages:
            return
        existing = list(self._messages)
        # 仅接受 user/assistant 且非空的摘要（持久化层可能含 system，跳过）。
        candidates = [
            AssistantMessage(
                message_id=m.message_id,
                role=m.role,
                text=m.text,
            )
            for m in messages
            if m.role in ("user", "assistant") and m.text
        ]
        if not candidates:
            return
        # 去重：避免重复回灌（同一 message_id 不重复加入）。
        existing_ids = {m.message_id for m in existing}
        for msg in candidates:
            if msg.message_id and msg.message_id in existing_ids:
                continue
            self._messages.append(msg)
        self._emit_state()

    # ------------------------------------------------------------------ #
    # 提交需求 → 规划（TAEP §11.6 步骤 2-7）
    # ------------------------------------------------------------------ #

    def submit_request(
        self, text: str, *, brief_id: str = "default"
    ) -> AssistantRequest:
        """提交自然语言需求（TAEP §11.6 步骤 2-3）。

        1. 封装为不可变 AssistantRequest（不含绝对路径/secret）。
        2. 从需求生成 brief patch（AI 提议）。
        3. 转入 GATHERING_CONTEXT → PLANNING。
        """
        req = AssistantRequest(
            request_id=str(uuid.uuid4()),
            text=text,
            profile_id=self._active_profile_id,
        )
        self._messages.append(AssistantMessage(
            message_id=str(uuid.uuid4()), role="user", text=text,
        ))
        self._active_brief_id = brief_id
        self._set_state(AssistantState.GATHERING_CONTEXT)
        return req

    def propose_brief_patch(self, brief_id: str, user_text: str) -> BriefPatch:
        """从需求生成提议性 brief patch（TAEP §11.6 步骤 3）。

        用户批准后才版本化保存（``approve_brief_patch``）。
        """
        self._set_state(AssistantState.AWAITING_BRIEF_APPROVAL)
        return self._brief.propose_patch_from_text(brief_id, user_text)

    def approve_brief_patch(self, brief_id: str, patch: BriefPatch) -> EditorialBrief:
        """用户批准 brief patch（版本化保存，TAEP §11.6 步骤 3）。"""
        brief = self._brief.apply_patch(brief_id, patch)
        self._active_brief_id = brief_id
        self._messages.append(AssistantMessage(
            message_id=str(uuid.uuid4()), role="assistant",
            text=f"Brief 已更新到 revision {brief.revision}",
            brief_patch_ref=brief_id,
        ))
        return brief

    def preview_brief_patch(
        self, brief_id: str, patch: BriefPatch
    ) -> EditorialBrief:
        """把 AI 提议的 patch 合并成一个**预览** brief（不保存），供 controller 回填
        Brief 编辑器（M1.3：显式批准门禁）。

        基于 brief_id 的现有 brief（或 ``create_default`` 后的默认）+ patch.changes
        合并；返回未 bump revision 的预览（编辑器批准时会 bump）。
        """
        current = self._brief.get(brief_id)
        if current is None:
            current = self._brief.create_default(brief_id)
        # 复用 BriefService 的合并逻辑（不 save、不 bump）。
        return self._brief._merge(current, patch.changes)

    def apply_user_brief(
        self, brief_id: str, brief: EditorialBrief
    ) -> EditorialBrief:
        """用户在编辑器里批准的 brief 直接 save（M1.3：用户是真相源）。

        editor emit 的是完整 bumped EditorialBrief；直接 save，不走 patch/乐观锁。
        **不**进入 PLANNING——planning 由 controller 在批准后单独触发（职责单一）。
        """
        saved = self._brief.save(brief)
        self._active_brief_id = brief_id
        self._messages.append(AssistantMessage(
            message_id=str(uuid.uuid4()), role="assistant",
            text=f"Brief 已确认（revision {saved.revision}），开始规划方案",
            brief_patch_ref=brief_id,
        ))
        return saved

    def plan_variants(
        self, semantic_index: Any, config: AssistantConfig,
        *, analysis_snapshot_hash: str = "",
    ) -> list[EditVariant]:
        """生成多个 variant（TAEP §11.6 步骤 6-7）。

        使用确定性规则引擎 planner（无云场景）；真实模型在 P6 接入。
        """
        self._set_state(AssistantState.PLANNING)
        try:
            brief = self._brief.get(self._active_brief_id)
            brief_rev = brief.revision if brief else 0
            base_rev = self._current_timeline_revision(config.timeline_id)
            if base_rev is None:
                # AUD-13011：revision 未知（快照失败或活动时间线不匹配）时显式
                # 降级告知，而不是静默以 revision 0 编译、到提交乐观锁才失败。
                self._messages.append(AssistantMessage(
                    message_id=str(uuid.uuid4()), role="assistant",
                    text=(
                        "无法确认当前时间线 revision，方案将以基线 revision 0 规划；"
                        "应用前会重新校验，若时间线已变化需重新规划"
                    ),
                ))
            inp = PlannerInput(
                brief_id=self._active_brief_id,
                brief_revision=brief_rev,
                asset_id=config.asset_id,
                timeline_id=config.timeline_id,
                project_id=config.project_id,
                base_revision=base_rev or 0,
                analysis_snapshot_hash=analysis_snapshot_hash,
                target_duration=config.target_duration_frames,
            )
            variants = self._planner.plan(
                inp, semantic_index,
                target_beats=config.target_beats,
                # M2.1：每编译出一个 plan 就存入仓库，使 variant.edit_plan_ref 可解析。
                on_plan_compiled=lambda plan, variant: self._plans.store(
                    plan, source_variant_id=variant.variant_id,
                ),
            )
            self._variants = {v.variant_id: v for v in variants}
            # 收集 decisions
            self._decisions = {}
            for v in variants:
                for d in v.decisions:
                    self._decisions[d.decision_id] = d
            self._messages.append(AssistantMessage(
                message_id=str(uuid.uuid4()), role="assistant",
                text=f"生成了 {len(variants)} 个可比较的方案",
                variant_id=variants[0].variant_id if variants else None,
            ))
            self._set_state(AssistantState.VARIANTS_READY)
            return variants
        except Exception as exc:
            self._set_state(AssistantState.FAILED, error=str(exc))
            return []

    # ------------------------------------------------------------------ #
    # 工作流闭环（M7 / §11.1）
    # ------------------------------------------------------------------ #

    def run_workflow(
        self,
        inp: WorkflowInput,
        *,
        cancel_requested: Any = None,
        emit_node_event: Any = None,
    ) -> WorkflowResult:
        """执行一个内置工作流（口播精剪 / 30秒广告 / 字幕初稿）。

        薄入口：委托 ``WorkflowRunner``。产出的 variant 进入既有 ``self._variants``
        字典，使其后的 ``select_variant`` / ``validate_selected`` / ``apply_selected``
        路径**完全复用**（不写第二条提交路径）——满足"应用仍只有一个 Undo 单元"
        （§11.1 M7）。variant 的 EditPlan 经 runner 的 ``on_plan_compiled`` 回调进入
        ``self._plans``（与 ``plan_variants`` 同一仓库路径），``edit_plan_ref`` 可解析。

        字幕工作流产 typed artifact（``artifact_id``），不产 EditPlan（字幕 op 首版
        未在编译器接通）。

        P0.6：接受 ``cancel_requested`` / ``emit_node_event`` 关键字参数，使
        ``LegacyHarnessAdapter._service_accepts_workflow_kwargs`` 探测成功（adapter
        据此把 runtime cancel token 透传进来）。已取消时在调用前门槛短路，不执行
        WorkflowRunner——满足 plan §1.4 红线"取消会传播"。

        ``emit_node_event`` 接收 ``WorkflowRunner`` 的真实阶段事件
        （``kind="progress", node_id="", payload={"stage": ...}``）；节点画布
        controller 据此按节点类别做诚实状态映射（分析节点 cached、审批节点
        waiting_input 等），不伪造逐节点进度。
        """
        # P0.6：调用前门槛——已取消则不执行 WorkflowRunner（协作取消）。
        if cancel_requested is not None and cancel_requested():
            self._messages.append(AssistantMessage(
                message_id=str(uuid.uuid4()), role="assistant",
                text=f"工作流 {inp.workflow_id.value} 已取消",
            ))
            self._set_state(AssistantState.CANCELLED)
            return WorkflowResult(
                workflow_id=inp.workflow_id, degradation="cancelled",
            )

        def _emit_stage(stage: str) -> None:
            if emit_node_event is None:
                return
            try:
                emit_node_event("progress", "", payload={"stage": stage})
            except TypeError:
                # 兼容仅接受 (kind, node_id) 的旧 emitter——丢弃 stage 信息。
                pass

        runner = self._workflow_runner or self._build_default_workflow_runner()
        result = runner.run(inp, emit_stage=_emit_stage)
        # 把 variant 灌入既有状态机，使 select/validate/apply 路径复用。
        if result.variants:
            self._variants = {v.variant_id: v for v in result.variants}
            self._decisions = {}
            for v in result.variants:
                for d in v.decisions:
                    self._decisions[d.decision_id] = d
            self._messages.append(AssistantMessage(
                message_id=str(uuid.uuid4()), role="assistant",
                text=(
                    f"工作流 {inp.workflow_id.value} 生成了 "
                    f"{len(result.variants)} 个方案"
                    + ("（含降级提示）" if result.degraded else "")
                ),
                variant_id=result.variants[0].variant_id,
            ))
            self._set_state(AssistantState.VARIANTS_READY)
        elif result.artifact_id is not None:
            self._messages.append(AssistantMessage(
                message_id=str(uuid.uuid4()), role="assistant",
                text=f"工作流 {inp.workflow_id.value} 生成了字幕初稿 artifact",
            ))
            self._set_state(AssistantState.IDLE)
        else:
            # 降级或诊断错误：不产生 variant/artifact。
            reason = result.degradation or "工作流未产出（见 diagnostics）"
            self._messages.append(AssistantMessage(
                message_id=str(uuid.uuid4()), role="assistant",
                text=f"工作流 {inp.workflow_id.value} 降级：{reason}",
            ))
            self._set_state(AssistantState.IDLE)
        return result

    def _build_default_workflow_runner(self) -> WorkflowRunner:
        """按需用现有依赖构造 WorkflowRunner（向后兼容：无 analysis_service 时
        variant 工作流仍可跑，字幕工作流返回诊断）。"""
        return WorkflowRunner(
            planner=self._planner,
            analysis_service=self._analysis_service or _NullAnalysisReader(),
            brief_service=self._brief,
            artifact_store=self._artifact_store,
            on_plan_compiled=lambda plan, variant: self._plans.store(
                plan, source_variant_id=variant.variant_id,
            ),
            translation_provider=self._translation_provider,
        )

    # ------------------------------------------------------------------ #
    # Skill Runtime 闭环（M9 / §11.1）
    # ------------------------------------------------------------------ #

    def run_skill(
        self, inp: SkillInput,
        *,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> SkillResult:
        """执行一个已注册的 Skill（M9 / §11.1）。

        薄入口：委托 ``SkillRuntime``。官方 Skill（roles=()）经
        ``_OfficialWorkflowBackend`` 桥接到既有 ``WorkflowRunner``——行为与
        ``run_workflow`` 完全一致（向后兼容），variant 同样灌入 ``self._variants``
        复用 select/validate/apply 路径（仍只有一个 Undo 单元，§11.1 M7 红线）。
        多角色 Skill（roles 非空）走 TaskGraph，产 typed artifact（不直接写 timeline，
        §6.1 子智能体无独立 approval）。

        ``cancel_requested``：透传给 ``SkillRuntime.run``（仅多角色 TaskGraph 路径生效；
        官方 Skill 取消由宿主在 workflow 层处理）。
        """
        runtime = self._skill_runtime or self._build_default_skill_runtime()
        result = runtime.run(inp, cancel_requested=cancel_requested)
        # 官方 Skill 的 variant 灌入既有状态机（与 run_workflow 同路径）。
        if result.variants:
            self._variants = {v.variant_id: v for v in result.variants}
            self._decisions = {}
            for v in result.variants:
                for d in v.decisions:
                    self._decisions[d.decision_id] = d
            self._messages.append(AssistantMessage(
                message_id=str(uuid.uuid4()), role="assistant",
                text=(
                    f"Skill {inp.skill_id} 生成了 {len(result.variants)} 个方案"
                    + ("（含降级提示）" if result.degraded else "")
                ),
                variant_id=result.variants[0].variant_id,
            ))
            self._set_state(AssistantState.VARIANTS_READY)
        elif result.artifacts:
            # 多角色 Skill：typed artifact 聚合（不进 _variants，需宿主后续处理）。
            self._messages.append(AssistantMessage(
                message_id=str(uuid.uuid4()), role="assistant",
                text=(
                    f"Skill {inp.skill_id} 产出了 {len(result.artifacts)} 个子任务产物"
                    + (f"（{result.degradation}）" if result.degraded else "")
                ),
            ))
            self._set_state(AssistantState.IDLE)
        elif result.artifact_id is not None:
            self._messages.append(AssistantMessage(
                message_id=str(uuid.uuid4()), role="assistant",
                text=f"Skill {inp.skill_id} 生成了字幕初稿 artifact",
            ))
            self._set_state(AssistantState.IDLE)
        else:
            reason = result.degradation or "Skill 未产出（见 diagnostics）"
            self._messages.append(AssistantMessage(
                message_id=str(uuid.uuid4()), role="assistant",
                text=f"Skill {inp.skill_id} 降级：{reason}",
            ))
            self._set_state(AssistantState.IDLE)
        return result

    def _build_default_skill_runtime(self) -> SkillRuntime:
        """按需构造 SkillRuntime（向后兼容）。

        官方 Skill 经 ``_OfficialWorkflowBackend`` 桥接到既有 ``WorkflowRunner``；
        多角色 Skill 用默认 ``DefaultGraphBuilder`` + ``DeterministicRoleExecutor``。
        """
        registry = SkillRegistry()  # 默认注册三个官方 Skill
        backend = _OfficialWorkflowBackend(
            runner=self._workflow_runner or self._build_default_workflow_runner(),
        )
        return SkillRuntime(
            registry=registry,
            policy=self._single_writer_policy,
            workflow_backend=backend,
            role_executor=self._role_executor,
            graph_builder=DefaultGraphBuilder(),
        )

    def compare(self) -> dict[str, Any]:
        """比较当前 variants（TAEP §7 compare_variants）。"""
        return compare_variants(list(self._variants.values())).to_dict()

    # ------------------------------------------------------------------ #
    # 用户反馈（TAEP §11.6 步骤 9）
    # ------------------------------------------------------------------ #

    def select_variant(self, variant_id: str) -> None:
        self._set_state(AssistantState.AWAITING_VARIANT_APPROVAL)
        for vid, v in self._variants.items():
            self._variants[vid] = v.with_status(
                VariantStatus.SELECTED if vid == variant_id else VariantStatus.PROPOSED
            )
        self._emit_state()

    def accept_decision(self, decision_id: str) -> None:
        d = self._decisions.get(decision_id)
        if d is not None:
            self._decisions[decision_id] = d.with_state(DecisionState.ACCEPTED)
        self._emit_state()

    def reject_decision(self, decision_id: str) -> None:
        d = self._decisions.get(decision_id)
        if d is not None:
            self._decisions[decision_id] = d.with_state(DecisionState.REJECTED)
        self._messages.append(AssistantMessage(
            message_id=str(uuid.uuid4()), role="assistant",
            text=f"已拒绝决策 {decision_id}；下一轮规划将避开该选择",
            decision_id=decision_id,
        ))
        self._emit_state()

    def lock_decision(self, decision_id: str) -> None:
        d = self._decisions.get(decision_id)
        if d is not None:
            self._decisions[decision_id] = d.with_state(DecisionState.LOCKED)
        self._emit_state()

    # ------------------------------------------------------------------ #
    # 校验 + 应用（TAEP §11.6 步骤 10-11）
    # ------------------------------------------------------------------ #

    def validate_selected(self, variant_id: str, *, state: Any = None) -> Any:
        """对选定 variant 校验并生成 diff（TAEP §11.6 步骤 10）。

        M2.3：从 ``PlanRepository`` 取回不可变 plan，调用 ``TransactionService.preview``
        做真正的 validate+compile+simulate+diff，返回完整 ``PreviewResult``（含稳定
        diagnostics）。取代旧实现"返回 scores+plan ref 即视为校验"的占位逻辑。

        Args:
            variant_id: 选定的 variant。
            state: 可选的两层状态；None 时尝试从 session 取。无 state 时无法做
                domain 校验，返回 None 并进入 FAILED。
        """
        self._set_state(AssistantState.VALIDATING)
        v = self._variants.get(variant_id)
        if v is None or v.edit_plan_ref is None:
            self._set_state(AssistantState.FAILED, error="no variant/plan to validate")
            return None
        plan = self._plans.get_plan(v.edit_plan_ref)
        if plan is None:
            # plan 已不在仓库（工程重开 / variant 过期）→ 提示重新规划
            self._set_state(AssistantState.FAILED, error="方案对应的 plan 已过期，请重新规划")
            return None
        # 取状态：优先参数，其次 session 的 snapshot 无法直接给 domain 校验用，
        # 故要求调用方（controller/bridge）显式提供 state。
        if state is None:
            self._set_state(AssistantState.FAILED, error="缺少工程状态，无法校验方案")
            return None
        preview = self._txn.preview(plan, state)
        # 把 preview 绑定到 plan（带状态指纹，供 stale 判定，M2.1）
        self._plans.attach_preview(
            plan.plan_id, preview,
            state_fingerprint_value=self._fingerprint_for(plan, state),
        )
        if preview.has_errors:
            self._set_state(
                AssistantState.FAILED,
                error="; ".join(d.message for d in preview.report.errors) or "校验未通过",
            )
            return preview
        self._set_state(AssistantState.AWAITING_APPLY_APPROVAL)
        return preview

    @staticmethod
    def _fingerprint_for(plan: Any, state: Any) -> str:
        """构造 (project revision, timeline revision) 指纹（manifest hash 后续补）。"""
        from src.features.edit.ai_editing.plan_repository import state_fingerprint

        try:
            proj_rev = getattr(state.project, "revision", None)
            seq = state.sequences.get(plan.target.timeline_id)
            tl_rev = getattr(seq, "revision", None) if seq is not None else None
        except Exception:
            proj_rev, tl_rev = None, None
        return state_fingerprint(
            project_revision=proj_rev, manifest_hash=None, timeline_revision=tl_rev,
        )

    def apply_selected(
        self,
        variant_id: str,
        plan: Any,
        state: Any,
        *,
        approval: Any,
        approval_service: Any = None,
    ) -> Any:
        """应用选定 variant（TAEP §11.6 步骤 11）。

        授权契约（M0.1）：``approval`` 必须是宿主签发的 ``ApprovalGrant``（或经
        ``approval_service`` consume 的 grant_id 字符串）。裸字符串 token 不再被
        接受。``state`` 由调用方（controller/bridge）显式提供，本服务不再触碰
        ``EditSession`` 的私有 ``_host`` 字段（M1.1）。
        """
        self._set_state(AssistantState.APPLYING)
        try:
            result = self._txn.commit(
                plan, state,
                approval=approval,
                approval_service=approval_service,
                client="assistant",
            )
            if result.applied:
                self._messages.append(AssistantMessage(
                    message_id=str(uuid.uuid4()), role="assistant",
                    text=f"方案已应用（revision {result.new_revision}，可一次撤销）",
                    variant_id=variant_id,
                ))
                self._set_state(AssistantState.IDLE)
            else:
                self._set_state(AssistantState.FAILED, error=result.error or "apply failed")
            return result
        except Exception as exc:
            self._set_state(AssistantState.FAILED, error=str(exc))
            return None

    # ------------------------------------------------------------------ #
    # 取消（TAEP §11.5）
    # ------------------------------------------------------------------ #

    def cancel(self) -> None:
        """取消当前请求；保留已完成的诊断（TAEP §11.5 / plan §10.2 P0.6）。

        P0.6：若 ``set_active_run_id`` 注入了 runtime + active run_id，则把 cancel
        透传到 ``runtime.cancel(run_id)``（协作 cancel token 传播到下游 provider /
        executor / job）。否则回退到纯状态翻转（向后兼容，旧测试 / 未装配场景）。

        AUD-13010：取消传播失败不再被静默吞掉——记 warning（含堆栈），并通过
        snapshot 的 ``error_message`` 明示"取消请求可能未传达，任务可能仍在执行"
        （付费生成可能继续扣费，用户必须能看到这一风险）。
        """
        cancel_not_delivered = False
        if (
            self._active_runtime is not None
            and self._active_run_id
        ):
            try:
                self._active_runtime.cancel(self._active_run_id)
            except Exception:  # noqa: BLE001 — 取消传播失败必须可见，不得 pass 掉
                cancel_not_delivered = True
                logger.warning(
                    "runtime.cancel failed for run %s; the downstream run may "
                    "still be executing",
                    self._active_run_id,
                    exc_info=True,
                )
        self._set_state(
            AssistantState.CANCELLED,
            error=(
                "取消请求未能传达到底层任务，该任务可能仍在执行"
                if cancel_not_delivered else None
            ),
        )

    # ------------------------------------------------------------------ #
    # 辅助
    # ------------------------------------------------------------------ #

    def _current_timeline_revision(self, timeline_id: str) -> int | None:
        """读取当前时间线 revision；无法确认时返回 None（不伪造基线）。

        AUD-13011：快照异常不再被 ``except: pass`` 静默降级——记 warning（含
        堆栈）。返回 None 时 ``plan_variants`` 会显式提示"以基线 0 规划"的降级，
        而不是让 plan 悄悄针对 revision 0 编译、再在提交乐观锁处莫名失败。
        """
        try:
            snap = self._session.get_snapshot()
            tl = snap.active_timeline
            if tl and tl.get("timeline_id") == timeline_id:
                return int(tl.get("revision", 0))
        except Exception:  # noqa: BLE001 — 失败可见，但规划不因此崩溃
            logger.warning(
                "timeline revision snapshot failed for %s; planning will "
                "proceed with an unverified base revision",
                timeline_id,
                exc_info=True,
            )
        return None


class _OfficialWorkflowBackend:
    """把官方 Skill（roles=()）桥接到 M7 ``WorkflowRunner``（M9 / §11.1）。

    SkillRuntime 对官方 Skill 调 ``run_official``，本类把 ``SkillInput`` 转成对应的
    ``WorkflowInput``（经 ``_SKILL_TO_WORKFLOW`` 映射），调既有 runner，再把
    ``WorkflowResult`` 转成 ``OfficialResult``。保证官方 Skill 走 M7 单路径闭环，
    "应用仍只有一个 Undo 单元"（§11.1 M7 红线）。
    """

    def __init__(self, runner: WorkflowRunner) -> None:
        self._runner = runner

    def run_official(self, manifest: Any, inp: SkillInput) -> OfficialResult:
        workflow_id = _SKILL_TO_WORKFLOW.get(inp.skill_id)
        if workflow_id is None:
            # 非官方 Skill 但 roles=()：无 backend 可走，明确降级（不编造）。
            return OfficialResult(degradation=(
                f"Skill {inp.skill_id} 声明无角色但非官方工作流，无 WorkflowBackend 可用"
            ))
        wf_input = WorkflowInput(
            workflow_id=workflow_id,
            asset_ids=inp.asset_ids,
            timeline_id=inp.timeline_id,
            project_id=inp.project_id,
            base_revision=inp.base_revision,
            brief_id=inp.brief_id,
            target_duration=inp.target_duration,
        )
        wf_result = self._runner.run(wf_input)
        return OfficialResult(
            variants=wf_result.variants,
            artifact_id=wf_result.artifact_id,
            degradation=wf_result.degradation,
            missing_evidence=wf_result.missing_evidence,
        )
