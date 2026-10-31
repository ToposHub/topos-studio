# DORMANT(2026-08-19 审计): 已实现且有测试，但暂无生产入口（有意休眠）。
# 接线前请勿在生产依赖；若半年内无接线计划建议删除。
"""Motion Graphic action permission matrix (MG-22).

The general-purpose :mod:`src.features.edit.ai_editing.execution_policy`
defines a three-mode model (SUGGESTION / SEMI_AUTOMATIC / RESTRICTED_AUTOMATIC)
and ten cross-domain actions.  MG-22 asks for a Motion Graphic specific action
matrix that:

* names the actions a Motion Graphic clip lifecycle actually performs;
* declares, for each action, whether it is read-only, costs money, writes the
  timeline, replaces existing media, uploads to the cloud, or publishes — and
  therefore whether it is *auto-allowed* by default, allowed after one batch
  approval, or **always** requires a fresh single-action confirmation;
* fails closed: any action not present in the matrix is denied by default
  rather than silently allowed.

The matrix is intentionally separate from ``ExecutionPolicy`` so that the
Motion Graphic feature can evolve without leaking new actions into the cross-
domain policy, while still being *consumable* by it via
:class:`MotionGraphicPolicyAdapter`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable, Mapping


# --------------------------------------------------------------------------- #
# Action vocabulary
# --------------------------------------------------------------------------- #


class MotionGraphicAction(str, Enum):
    """Every action the Motion Graphic pipeline can perform.

    The closed vocabulary is the contract between the Assistant UI, the
    planner and the approval scope.  Adding an action here forces a deliberate
    decision about its default permission — which is the whole point of MG-22.
    """

    # read-only / planning — safe to auto-run
    READ_PROJECT = "read_project"
    ANALYZE_ASSETS = "analyze_assets"
    PROPOSE_BRIEF = "propose_brief"
    PLAN_CREATIVE = "plan_creative"

    # local generation — runs on local CPU/GPU, no network, no write
    RENDER_LOWRES_PREVIEW = "render_lowres_preview"
    RENDER_STORYBOARD = "render_storyboard"
    RENDER_KEYFRAME_STILLS = "render_keyframe_stills"

    # LLM generation — network, cost, but no project write
    LLM_GENERATE_PROPOSAL = "llm_generate_proposal"
    LLM_GENERATE_SCENE_PLAN = "llm_generate_scene_plan"
    LLM_REFINE_COPY = "llm_refine_copy"

    # paid / cloud media generation — money + external provider
    GENERATE_PAID_IMAGE = "generate_paid_image"
    GENERATE_PAID_VIDEO = "generate_paid_video"
    GENERATE_PAID_AUDIO = "generate_paid_audio"
    GENERATE_TTS = "generate_tts"

    # network asset acquisition — download fonts/stock, may relicense content
    DOWNLOAD_FONT = "download_font"
    DOWNLOAD_STOCK_ASSET = "download_stock_asset"

    # project writes — touch the scene store / timeline
    CREATE_SCENE = "create_scene"
    UPDATE_SCENE_ELEMENTS = "update_scene_elements"
    INSERT_TIMELINE_CLIP = "insert_timeline_clip"
    REPLACE_EXISTING_CLIP = "replace_existing_clip"

    # delivery — final, outward-facing
    EXPORT_FILE = "export_file"
    PUBLISH_TO_PLATFORM = "publish_to_platform"
    UPLOAD_TO_CLOUD = "upload_to_cloud"


# --------------------------------------------------------------------------- #
# Risk profile
# --------------------------------------------------------------------------- #


class MotionGraphicActionKind(str, Enum):
    """What kind of consequence an action has — drives the default rule."""

    READ_ONLY = "read_only"                 # no side effects
    LOCAL_COMPUTE = "local_compute"         # CPU/GPU work, no network, no write
    LLM_CALL = "llm_call"                   # network + cost, no project write
    PAID_GENERATION = "paid_generation"     # external provider + money
    NETWORK_FETCH = "network_fetch"         # downloads external assets
    PROJECT_WRITE = "project_write"         # mutates scene / timeline
    DESTRUCTIVE_WRITE = "destructive_write"  # replaces existing user content
    DELIVERY = "delivery"                   # outward-facing export / publish


class MotionGraphicPermissionTier(str, Enum):
    """How much approval an action needs by default."""

    AUTO = "auto"                 # may run whenever the policy allows
    APPROVED_ONCE = "approved_once"  # covered by a single session/batch approval
    ALWAYS_CONFIRM = "always_confirm"  # never batched; needs its own confirmation


@dataclass(frozen=True)
class MotionGraphicActionProfile:
    """The risk profile of one :class:`MotionGraphicAction`."""

    action: MotionGraphicAction
    kind: MotionGraphicActionKind
    default_permission: MotionGraphicPermissionTier
    writes_timeline: bool = False
    costs_money: bool = False
    uses_network: bool = False
    uploads_content: bool = False
    description: str = ""


# --------------------------------------------------------------------------- #
# Matrix definition
# --------------------------------------------------------------------------- #
#
# The matrix is the single source of truth: if an action is not listed here,
# :meth:`MotionGraphicActionMatrix.profile` raises — there is no implicit
# "allow" path.  This implements the MG-22 requirement that "未分类动作默认拒绝".

_ACTION_PROFILES: dict[MotionGraphicAction, MotionGraphicActionProfile] = {
    MotionGraphicAction.READ_PROJECT: MotionGraphicActionProfile(
        MotionGraphicAction.READ_PROJECT,
        MotionGraphicActionKind.READ_ONLY,
        MotionGraphicPermissionTier.AUTO,
        description="读取工程、时间线、素材、品牌包等上下文。",
    ),
    MotionGraphicAction.ANALYZE_ASSETS: MotionGraphicActionProfile(
        MotionGraphicAction.ANALYZE_ASSETS,
        MotionGraphicActionKind.LOCAL_COMPUTE,
        MotionGraphicPermissionTier.AUTO,
        description="本地分析素材时长、分辨率、可用区间等。",
    ),
    MotionGraphicAction.PROPOSE_BRIEF: MotionGraphicActionProfile(
        MotionGraphicAction.PROPOSE_BRIEF,
        MotionGraphicActionKind.READ_ONLY,
        MotionGraphicPermissionTier.AUTO,
        description="从用户输入整理可编辑的 Motion Graphic Brief。",
    ),
    MotionGraphicAction.PLAN_CREATIVE: MotionGraphicActionProfile(
        MotionGraphicAction.PLAN_CREATIVE,
        MotionGraphicActionKind.LLM_CALL,
        MotionGraphicPermissionTier.AUTO,
        costs_money=True,
        uses_network=True,
        description="调用 LLM 生成 1–3 个低成本创意方案；不写工程。",
    ),
    MotionGraphicAction.RENDER_LOWRES_PREVIEW: MotionGraphicActionProfile(
        MotionGraphicAction.RENDER_LOWRES_PREVIEW,
        MotionGraphicActionKind.LOCAL_COMPUTE,
        MotionGraphicPermissionTier.AUTO,
        description="渲染低清可播放预览供确认前查看。",
    ),
    MotionGraphicAction.RENDER_STORYBOARD: MotionGraphicActionProfile(
        MotionGraphicAction.RENDER_STORYBOARD,
        MotionGraphicActionKind.LOCAL_COMPUTE,
        MotionGraphicPermissionTier.AUTO,
        description="渲染首/中/尾帧与分镜缩略图。",
    ),
    MotionGraphicAction.RENDER_KEYFRAME_STILLS: MotionGraphicActionProfile(
        MotionGraphicAction.RENDER_KEYFRAME_STILLS,
        MotionGraphicActionKind.LOCAL_COMPUTE,
        MotionGraphicPermissionTier.AUTO,
        description="渲染关键帧静帧用于质量检查。",
    ),
    MotionGraphicAction.LLM_GENERATE_PROPOSAL: MotionGraphicActionProfile(
        MotionGraphicAction.LLM_GENERATE_PROPOSAL,
        MotionGraphicActionKind.LLM_CALL,
        MotionGraphicPermissionTier.AUTO,
        costs_money=True,
        uses_network=True,
        description="LLM 输出 CreativeProposal（叙事/文案/节奏/版式）。",
    ),
    MotionGraphicAction.LLM_GENERATE_SCENE_PLAN: MotionGraphicActionProfile(
        MotionGraphicAction.LLM_GENERATE_SCENE_PLAN,
        MotionGraphicActionKind.LLM_CALL,
        MotionGraphicPermissionTier.AUTO,
        costs_money=True,
        uses_network=True,
        description="LLM 把已选方案编译为受限场景命令序列。",
    ),
    MotionGraphicAction.LLM_REFINE_COPY: MotionGraphicActionProfile(
        MotionGraphicAction.LLM_REFINE_COPY,
        MotionGraphicActionKind.LLM_CALL,
        MotionGraphicPermissionTier.AUTO,
        costs_money=True,
        uses_network=True,
        description="LLM 仅修改文案、配色、节奏等局部字段。",
    ),
    MotionGraphicAction.GENERATE_PAID_IMAGE: MotionGraphicActionProfile(
        MotionGraphicAction.GENERATE_PAID_IMAGE,
        MotionGraphicActionKind.PAID_GENERATION,
        MotionGraphicPermissionTier.ALWAYS_CONFIRM,
        costs_money=True,
        uses_network=True,
        description="付费调用图像生成 provider；需要单独确认与预算。",
    ),
    MotionGraphicAction.GENERATE_PAID_VIDEO: MotionGraphicActionProfile(
        MotionGraphicAction.GENERATE_PAID_VIDEO,
        MotionGraphicActionKind.PAID_GENERATION,
        MotionGraphicPermissionTier.ALWAYS_CONFIRM,
        costs_money=True,
        uses_network=True,
        description="付费调用视频生成 provider；需要单独确认与预算。",
    ),
    MotionGraphicAction.GENERATE_PAID_AUDIO: MotionGraphicActionProfile(
        MotionGraphicAction.GENERATE_PAID_AUDIO,
        MotionGraphicActionKind.PAID_GENERATION,
        MotionGraphicPermissionTier.ALWAYS_CONFIRM,
        costs_money=True,
        uses_network=True,
        description="付费调用音乐/音效生成 provider；需要单独确认与预算。",
    ),
    MotionGraphicAction.GENERATE_TTS: MotionGraphicActionProfile(
        MotionGraphicAction.GENERATE_TTS,
        MotionGraphicActionKind.PAID_GENERATION,
        MotionGraphicPermissionTier.ALWAYS_CONFIRM,
        costs_money=True,
        uses_network=True,
        description="付费调用 TTS provider 生成配音；需要单独确认。",
    ),
    MotionGraphicAction.DOWNLOAD_FONT: MotionGraphicActionProfile(
        MotionGraphicAction.DOWNLOAD_FONT,
        MotionGraphicActionKind.NETWORK_FETCH,
        MotionGraphicPermissionTier.APPROVED_ONCE,
        uses_network=True,
        description="下载字体到本地；涉及许可，需要确认。",
    ),
    MotionGraphicAction.DOWNLOAD_STOCK_ASSET: MotionGraphicActionProfile(
        MotionGraphicAction.DOWNLOAD_STOCK_ASSET,
        MotionGraphicActionKind.NETWORK_FETCH,
        MotionGraphicPermissionTier.APPROVED_ONCE,
        uses_network=True,
        description="下载图库/视频素材；涉及许可，需要确认。",
    ),
    MotionGraphicAction.CREATE_SCENE: MotionGraphicActionProfile(
        MotionGraphicAction.CREATE_SCENE,
        MotionGraphicActionKind.PROJECT_WRITE,
        MotionGraphicPermissionTier.APPROVED_ONCE,
        description="在场景存储中创建新场景；尚未写入时间线。",
    ),
    MotionGraphicAction.UPDATE_SCENE_ELEMENTS: MotionGraphicActionProfile(
        MotionGraphicAction.UPDATE_SCENE_ELEMENTS,
        MotionGraphicActionKind.PROJECT_WRITE,
        MotionGraphicPermissionTier.APPROVED_ONCE,
        description="修改已存在场景的元素/动画/文案。",
    ),
    MotionGraphicAction.INSERT_TIMELINE_CLIP: MotionGraphicActionProfile(
        MotionGraphicAction.INSERT_TIMELINE_CLIP,
        MotionGraphicActionKind.PROJECT_WRITE,
        MotionGraphicPermissionTier.ALWAYS_CONFIRM,
        writes_timeline=True,
        description="把场景作为 clip 插入时间线 Graphics 轨；写入工程。",
    ),
    MotionGraphicAction.REPLACE_EXISTING_CLIP: MotionGraphicActionProfile(
        MotionGraphicAction.REPLACE_EXISTING_CLIP,
        MotionGraphicActionKind.DESTRUCTIVE_WRITE,
        MotionGraphicPermissionTier.ALWAYS_CONFIRM,
        writes_timeline=True,
        description="替换时间线上已有的 Motion Graphic clip；破坏性操作。",
    ),
    MotionGraphicAction.EXPORT_FILE: MotionGraphicActionProfile(
        MotionGraphicAction.EXPORT_FILE,
        MotionGraphicActionKind.DELIVERY,
        MotionGraphicPermissionTier.ALWAYS_CONFIRM,
        description="导出最终文件；面向交付，必须单独确认。",
    ),
    MotionGraphicAction.PUBLISH_TO_PLATFORM: MotionGraphicActionProfile(
        MotionGraphicAction.PUBLISH_TO_PLATFORM,
        MotionGraphicActionKind.DELIVERY,
        MotionGraphicPermissionTier.ALWAYS_CONFIRM,
        uses_network=True,
        uploads_content=True,
        description="发布到外部平台；涉及上传，必须单独确认。",
    ),
    MotionGraphicAction.UPLOAD_TO_CLOUD: MotionGraphicActionProfile(
        MotionGraphicAction.UPLOAD_TO_CLOUD,
        MotionGraphicActionKind.DELIVERY,
        MotionGraphicPermissionTier.ALWAYS_CONFIRM,
        uses_network=True,
        uploads_content=True,
        description="上传内容到云存储；涉及上传，必须单独确认。",
    ),
}


# --------------------------------------------------------------------------- #
# Policy decision
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class MotionGraphicPolicyDecision:
    """Outcome of evaluating one action against the matrix and current grants.

    Mirrors the shape of :class:`ExecutionPolicy`'s ``PolicyDecision`` so the
    UI can treat both sources uniformly.  ``needs_user_input`` is set when the
    denial is recoverable by asking the user (approval / clarification) rather
    than a hard policy mismatch.
    """

    allowed: bool
    action: MotionGraphicAction
    permission_tier: MotionGraphicPermissionTier
    needs_approval: bool = False
    needs_user_input: bool = False
    reason: str = ""
    diagnostic_code: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "action": self.action.value,
            "permission_tier": self.permission_tier.value,
            "needs_approval": self.needs_approval,
            "needs_user_input": self.needs_user_input,
            "reason": self.reason,
            "diagnostic_code": self.diagnostic_code,
        }


# --------------------------------------------------------------------------- #
# Matrix
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class MotionGraphicActionMatrix:
    """Lookup table of action → risk profile plus default-mode rules.

    The matrix is pure data; the live decision logic lives in
    :class:`MotionGraphicPermissionPolicy`, which combines the matrix with the
    user's grants (one-batch approval, explicit confirmations).
    """

    profiles: Mapping[MotionGraphicAction, MotionGraphicActionProfile] = field(
        default_factory=lambda: dict(_ACTION_PROFILES)
    )

    def profile(self, action: MotionGraphicAction) -> MotionGraphicActionProfile:
        try:
            return self.profiles[action]
        except KeyError as exc:  # pragma: no cover - defensive
            raise KeyError(
                f"Motion Graphic action {action!r} is not classified; "
                "the permission matrix fails closed. Register the action before use."
            ) from exc

    def known_actions(self) -> tuple[MotionGraphicAction, ...]:
        return tuple(self.profiles.keys())

    def actions_requiring_confirmation(self) -> tuple[MotionGraphicAction, ...]:
        return tuple(
            action for action, profile in self.profiles.items()
            if profile.default_permission == MotionGraphicPermissionTier.ALWAYS_CONFIRM
        )

    def actions_writing_timeline(self) -> tuple[MotionGraphicAction, ...]:
        return tuple(
            action for action, profile in self.profiles.items()
            if profile.writes_timeline
        )


_DEFAULT_MATRIX = MotionGraphicActionMatrix()


def default_action_matrix() -> MotionGraphicActionMatrix:
    """Return the shared default matrix instance (immutable)."""
    return _DEFAULT_MATRIX


# --------------------------------------------------------------------------- #
# Policy
# --------------------------------------------------------------------------- #


class MotionGraphicPermissionPolicy:
    """Decide whether a Motion Graphic action may run right now.

    The policy is *additive* on top of the cross-domain ``ExecutionPolicy``:
    it knows the Motion Graphic specific actions and grants, while feature
    flags (auto mode, cloud upload, paid generation, auto export) gate the
    same capabilities the cross-domain policy already gates.  Both must allow
    an action for it to run.

    Grants are tracked in three buckets:

    - ``auto_allowed``: actions the matrix says may run automatically;
    - ``batch_granted``: a single user approval that covers all
      ``APPROVED_ONCE`` actions in the current mission;
    - ``explicit_grants``: per-action confirmations, the only thing that can
      unlock ``ALWAYS_CONFIRM`` actions.

    Any action absent from the matrix is denied with a stable diagnostic code
    so the UI can show "未分类动作默认拒绝".
    """

    def __init__(
        self,
        matrix: MotionGraphicActionMatrix | None = None,
        *,
        cloud_upload_enabled: bool = False,
        paid_generation_enabled: bool = False,
        auto_export_enabled: bool = False,
        batch_granted: bool = False,
        explicit_grants: Iterable[MotionGraphicAction] | None = None,
        budget_units: float | None = None,
    ) -> None:
        self._matrix = matrix or default_action_matrix()
        self.cloud_upload_enabled = cloud_upload_enabled
        self.paid_generation_enabled = paid_generation_enabled
        self.auto_export_enabled = auto_export_enabled
        self.batch_granted = batch_granted
        self._explicit_grants: set[MotionGraphicAction] = set(explicit_grants or ())
        self.budget_units = budget_units

    # ---------------- grant management ---------------- #

    def grant_explicit(self, action: MotionGraphicAction) -> None:
        """Record that the user has confirmed this specific action once."""
        self._explicit_grants.add(action)

    def revoke_explicit(self, action: MotionGraphicAction) -> None:
        self._explicit_grants.discard(action)

    def revoke_all_explicit(self) -> None:
        self._explicit_grants.clear()

    def set_batch_granted(self, granted: bool) -> None:
        self.batch_granted = granted

    def has_explicit_grant(self, action: MotionGraphicAction) -> bool:
        return action in self._explicit_grants

    # ---------------- evaluation ---------------- #

    def evaluate(
        self,
        action: MotionGraphicAction,
        *,
        cost_units: float = 0.0,
    ) -> MotionGraphicPolicyDecision:
        if action not in self._matrix.profiles:
            return MotionGraphicPolicyDecision(
                allowed=False,
                action=action,
                permission_tier=MotionGraphicPermissionTier.ALWAYS_CONFIRM,
                needs_user_input=True,
                reason="未分类动作默认拒绝；请先在权限矩阵中登记该动作。",
                diagnostic_code="MOTION_GRAPHIC_PERMISSION_DENIED",
            )
        profile = self._matrix.profile(action)

        if profile.uploads_content and not self.cloud_upload_enabled:
            return MotionGraphicPolicyDecision(
                allowed=False,
                action=action,
                permission_tier=profile.default_permission,
                reason="云上传已在设置中关闭。",
                diagnostic_code="MOTION_GRAPHIC_PERMISSION_DENIED",
            )
        if profile.costs_money and not self.paid_generation_enabled:
            return MotionGraphicPolicyDecision(
                allowed=False,
                action=action,
                permission_tier=profile.default_permission,
                needs_user_input=True,
                reason="付费生成未启用；请在设置中开启或选择非付费方案。",
                diagnostic_code="MOTION_GRAPHIC_PERMISSION_DENIED",
            )
        if (
            self.budget_units is not None
            and profile.costs_money
            and cost_units > self.budget_units
        ):
            return MotionGraphicPolicyDecision(
                allowed=False,
                action=action,
                permission_tier=profile.default_permission,
                needs_user_input=True,
                reason="该动作的花费超过当前预算上限。",
                diagnostic_code="MOTION_GRAPHIC_PERMISSION_DENIED",
            )

        if profile.default_permission == MotionGraphicPermissionTier.AUTO:
            return MotionGraphicPolicyDecision(
                allowed=True,
                action=action,
                permission_tier=profile.default_permission,
            )
        if profile.default_permission == MotionGraphicPermissionTier.APPROVED_ONCE:
            if self.batch_granted or action in self._explicit_grants:
                return MotionGraphicPolicyDecision(
                    allowed=True,
                    action=action,
                    permission_tier=profile.default_permission,
                )
            return MotionGraphicPolicyDecision(
                allowed=False,
                action=action,
                permission_tier=profile.default_permission,
                needs_approval=True,
                needs_user_input=True,
                reason="该动作需要一次性批量授权后才能执行。",
                diagnostic_code="MOTION_GRAPHIC_APPROVAL_REQUIRED",
            )
        # ALWAYS_CONFIRM: even a batch grant is not enough.
        if action in self._explicit_grants:
            return MotionGraphicPolicyDecision(
                allowed=True,
                action=action,
                permission_tier=profile.default_permission,
            )
        return MotionGraphicPolicyDecision(
            allowed=False,
            action=action,
            permission_tier=profile.default_permission,
            needs_approval=True,
            needs_user_input=True,
            reason="该动作始终需要单独确认；不会被批量授权覆盖。",
            diagnostic_code="MOTION_GRAPHIC_APPROVAL_REQUIRED",
        )

    def evaluate_plan(
        self,
        actions: Iterable[MotionGraphicAction],
    ) -> MotionGraphicPolicyDecision:
        """Evaluate an ordered plan; return the first denial or an overall allow."""
        for action in actions:
            decision = self.evaluate(action)
            if not decision.allowed:
                return decision
        last_action = next(reversed(list(actions)), None)
        if last_action is None:
            return MotionGraphicPolicyDecision(
                allowed=True,
                action=MotionGraphicAction.READ_PROJECT,
                permission_tier=MotionGraphicPermissionTier.AUTO,
            )
        return MotionGraphicPolicyDecision(
            allowed=True,
            action=last_action,
            permission_tier=self._matrix.profile(last_action).default_permission,
        )


__all__ = [
    "MotionGraphicAction",
    "MotionGraphicActionKind",
    "MotionGraphicPermissionTier",
    "MotionGraphicActionProfile",
    "MotionGraphicActionMatrix",
    "MotionGraphicPolicyDecision",
    "MotionGraphicPermissionPolicy",
    "default_action_matrix",
]
