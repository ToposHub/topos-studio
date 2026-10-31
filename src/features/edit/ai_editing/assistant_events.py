"""TAEP AI 助手强类型状态/事件（Phase 9 UI-0）。

UI 与 service 之间**禁止**用 ``dict[str, Any]`` 无限传播（TAEP §11.9 UI-0）。
本模块定义所有跨层传递的 typed state / event / result，且**不传 Qt object**
（service 层无 PyQt 依赖）。

会话状态机（TAEP §11.5）：

    IDLE → GATHERING_CONTEXT → AWAITING_BRIEF_APPROVAL → PLANNING
         → VARIANTS_READY → AWAITING_VARIANT_APPROVAL → VALIDATING
         → AWAITING_APPLY_APPROVAL → APPLYING → PREVIEW_RENDERING → IDLE
                         ↘ STALE / FAILED / CANCELLED
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from src.features.edit.ai_editing.image_attachment import (
    ChatAttachment,
)


class AssistantState(str, Enum):
    """AI 助手会话状态机（TAEP §11.5）。"""

    IDLE = "idle"
    GATHERING_CONTEXT = "gathering_context"
    AWAITING_BRIEF_APPROVAL = "awaiting_brief_approval"
    PLANNING = "planning"
    VARIANTS_READY = "variants_ready"
    AWAITING_VARIANT_APPROVAL = "awaiting_variant_approval"
    VALIDATING = "validating"
    AWAITING_APPLY_APPROVAL = "awaiting_apply_approval"
    APPLYING = "applying"
    PREVIEW_RENDERING = "preview_rendering"
    STALE = "stale"
    FAILED = "failed"
    CANCELLED = "cancelled"
    # 边界状态
    NO_PROJECT = "no_project"  # 无打开工程
    NO_PROFILE = "no_profile"  # 未配置模型 profile

    @property
    def is_busy(self) -> bool:
        """是否在异步任务中（UI 显示 spinner / 禁用输入）。"""
        return self in (
            AssistantState.GATHERING_CONTEXT, AssistantState.PLANNING,
            AssistantState.VALIDATING, AssistantState.APPLYING,
            AssistantState.PREVIEW_RENDERING,
        )

    @property
    def is_awaiting_user(self) -> bool:
        """是否在等待用户决策（UI 高亮待确认区域）。"""
        return self in (
            AssistantState.AWAITING_BRIEF_APPROVAL,
            AssistantState.AWAITING_VARIANT_APPROVAL,
            AssistantState.AWAITING_APPLY_APPROVAL,
        )


@dataclass(frozen=True)
class AssistantRequest:
    """用户的一条自然语言需求（不可变，TAEP §11.6 步骤 2）。

    不含绝对路径/secret；含输入文本、用户选择的素材范围、brief revision、profile ID。
    """

    request_id: str
    text: str
    selected_asset_ids: tuple[str, ...] = ()
    selected_range: tuple[int, int] | None = None  # (start_frame, end_frame)
    brief_revision: int = 0
    profile_id: str = ""
    # Composer routing mode.  ``chat`` preserves the historical behavior;
    # feature-specific modes are handled by the controller's existing, gated
    # pipelines rather than by the UI.
    mode: str = "chat"
    # 多模态图片附件（粘贴的截图或抓取的视口帧）。仅内存中携带，不持久化为字节。
    attachments: tuple[ChatAttachment, ...] = ()
    # 用户从媒体池明确拖入/选择的目标资产。与 selected_asset_ids（当前上下文范围）
    # 不同，这是本次请求必须修改的稳定资产引用；Motion Graphic 使用
    # ``teaching_animation:<scene_id>``，不依赖旧对话或当前时间线选择。
    target_asset_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.request_id:
            raise ValueError("request_id cannot be empty")


@dataclass(frozen=True)
class AssistantMessage:
    """对话区的一条消息（TAEP §11.2）。

    每条 assistant 消息必须关联 brief_patch/analysis_ref/variant_id/decision_id/
    job_id 中至少一个；纯文本不能自行触发 commit。
    """

    message_id: str
    role: str  # "user" / "assistant" / "system"
    text: str = ""
    # 结构化引用（至少一个非空 for assistant 消息）
    brief_patch_ref: str | None = None
    analysis_ref: str | None = None
    variant_id: str | None = None
    decision_id: str | None = None
    job_id: str | None = None
    # 若为错误消息
    error_code: str | None = None
    # 多模态图片附件（仅内存会话历史携带；持久化时落为标记而非字节）。
    attachments: tuple[ChatAttachment, ...] = ()


@dataclass(frozen=True)
class ContextSummary:
    """上下文摘要（TAEP §11.2 上下文摘要区）。"""

    asset_count: int = 0
    analyzed_count: int = 0
    music_count: int = 0
    locked_count: int = 0
    snapshot_revision: int = 0
    analysis_fresh: bool = False


@dataclass(frozen=True)
class VariantCard:
    """variant 对比卡（TAEP §11.3 VariantListPanel）。"""

    variant_id: str
    label: str
    duration_frames: int = 0
    hard_constraints_passed: bool = True
    beat_coverage: float = 0.0
    pace_estimate: float = 0.0
    has_music: bool = False
    has_subtitle: bool = False
    status: str = "proposed"


@dataclass(frozen=True)
class DecisionCard:
    """decision 检查器条目（TAEP §11.3 DecisionInspector）。"""

    decision_id: str
    beat: str = ""
    description: str = ""
    evidence_summary: str = ""
    state: str = "proposed"  # proposed/accepted/rejected/locked
    is_music: bool = False
    is_subtitle: bool = False


@dataclass(frozen=True)
class JobCard:
    """job 进度卡（TAEP §11.3 AiJobPanel）。"""

    job_id: str
    kind: str  # preview/export/analysis
    status: str  # queued/running/...
    progress: float = 0.0
    label: str = ""


@dataclass(frozen=True)
class AssistantStateSnapshot:
    """完整的助手状态快照（供 UI 一次性刷新，避免多次信号）。

    UI 订阅 service 的 ``state_changed`` 信号，收到此快照后用 ``_updating`` 锁
    填充各子组件（TAEP §11.3：状态回填使用锁避免信号递归）。
    """

    state: AssistantState = AssistantState.IDLE
    active_profile_id: str = ""
    profile_connected: bool = False
    allow_media_upload: bool = False
    context: ContextSummary = field(default_factory=ContextSummary)
    variants: tuple[VariantCard, ...] = ()
    decisions: tuple[DecisionCard, ...] = ()
    jobs: tuple[JobCard, ...] = ()
    messages: tuple[AssistantMessage, ...] = ()
    error_message: str | None = None
    # 可选：当前 diff 摘要（AWAITING_APPLY_APPROVAL 时）
    diff_summary: dict[str, Any] | None = None
