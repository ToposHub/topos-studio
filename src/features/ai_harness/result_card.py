"""运行结果卡状态模型（plan §10.3 P1.5）。

统一渲染 evidence / 脚本 / 字幕草稿 / Variant / 生成素材 / QA / 错误，并声明
每个结果可执行的操作（查看依据 / 重试 / 调整参数 / 预览 diff / 审批）。

核心约束（plan §10.3 P1.5）：

- **不把未审批 proposal 显示成已应用**：``ResultCardState.applied`` 严格区分；
  审批前所有"应用/导出"类按钮禁用。
- **不将 OCR / 网页不可信文本当作字幕事实**：``trust_level`` 标注来源可信度
  （trusted / unverified / untrusted）；不可信结果明示需人工核对。
- **每个结果都能"查看依据 / 重试 / 调整参数 / 预览 diff / 审批"**：用
  ``available_actions`` 显式声明哪些操作可用（按状态 / 权限决定）。

本模块纯 Python（不依赖 Qt）；UI 渲染由 ``ai_result_card.py`` 负责。controller
从 RunRecord + artifact + approval 状态聚合成本模型的实例后喂给 UI。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal


ResultKind = Literal[
    "variant",          # 粗剪 / 广告 A/B 方案
    "subtitle_draft",   # 字幕草稿
    "script",           # 创意脚本 / 分镜
    "evidence",         # 分析证据（转录 / OCR / 镜头）
    "generated_media",  # 生成图片 / 视频 / 音乐 / TTS
    "qa_report",        # QA 报告
    "error",            # 错误
    "info",             # 普通信息（如降级提示）
]

TrustLevel = Literal["trusted", "unverified", "untrusted"]

ActionKind = Literal[
    "view_evidence",    # 查看依据（evidence_refs）
    "retry",            # 重试
    "adjust_params",    # 调整参数（重新打开输入表单）
    "preview_diff",     # 预览 diff
    "approve",          # 审批
    "apply",            # 应用（审批后）
    "discard",          # 丢弃
    "view_diagnostics", # 查看诊断
    "locate_in_pool",   # 预览 / 查看生成素材
    "import_to_pool",   # 导入媒体池
    "place_on_timeline",  # 提出时间线放置方案（需 Preview/Approval）
    "open_in_editor",   # 在编辑器打开（字幕草稿）
]


@dataclass(frozen=True)
class ResultAction:
    """一个可执行操作。"""

    kind: ActionKind
    label: str
    enabled: bool = True
    #: 操作所需权限（用于 UI 提示为何禁用）。
    requires_permission: str = ""
    #: 禁用原因（enabled=False 时填，供 tooltip）。
    disabled_reason: str = ""


@dataclass(frozen=True)
class ResultEvidenceRef:
    """结果引用的证据（plan §10.3 P1.5"查看依据"）。"""

    evidence_id: str
    kind: str = ""            # transcript / ocr / shot / tag
    summary: str = ""
    trust: TrustLevel = "trusted"


@dataclass(frozen=True)
class ResultCardState:
    """一个运行结果的统一渲染状态。

    controller 从 RunRecord + artifact + approval 聚合；UI 只读渲染。
    """

    result_id: str                       # 关联 run_id 或独立 id
    kind: ResultKind
    title: str
    summary: str = ""
    detail: str = ""                     # 富文本摘要（脱敏）
    #: 信任等级：trusted=可信证据；unverified=模型生成待核；untrusted=OCR/网页不可信。
    trust: TrustLevel = "trusted"
    #: 是否已审批（applied=True 隐含 approved；approved 不一定 applied）。
    approved: bool = False
    #: 是否已应用（写入时间线 / 导入媒体池）。
    applied: bool = False
    evidence_refs: tuple[ResultEvidenceRef, ...] = ()
    available_actions: tuple[ResultAction, ...] = ()
    diagnostics: tuple[dict[str, Any], ...] = ()
    #: 错误码（kind=error 时）。
    error_code: str = ""

    @property
    def trust_label(self) -> str:
        return {
            "trusted": "可信",
            "unverified": "待核",
            "untrusted": "不可信（需人工核对）",
        }.get(self.trust, self.trust)

    @property
    def status_label(self) -> str:
        if self.kind == "error":
            return "失败"
        if self.applied:
            return "已应用"
        if self.approved:
            return "已审批（待应用）"
        return "提议（未审批）"


# ---------------------------------------------------------------------- #
# 工厂：按 kind + 状态构造默认可用操作
# ---------------------------------------------------------------------- #


def default_actions(
    kind: ResultKind,
    *,
    approved: bool = False,
    applied: bool = False,
    has_evidence: bool = True,
    can_approve: bool = True,
    trust: TrustLevel = "trusted",
    has_diagnostics: bool = False,
) -> tuple[ResultAction, ...]:
    """按结果类型 + 状态构造默认可用操作集（plan §10.3 P1.5）。

    规则：

    - ``apply`` 仅在 approved 后启用（红线：不把未审批 proposal 显示成已应用）。
    - ``approve`` 在未 approved 且可审批时启用。
    - ``retry`` 永远启用（除非已 applied）。
    - ``view_evidence`` 在有证据时启用。
    - 不可信结果（untrusted）始终允许"查看依据 / 调整"，但禁用直接 apply（需先核）。
    """
    actions: list[ResultAction] = []

    if kind == "error":
        actions.append(ResultAction(kind="retry", label="重试", enabled=True))
        if has_diagnostics:
            actions.append(ResultAction(kind="view_diagnostics", label="查看诊断", enabled=True))
        return tuple(actions)

    # 查看依据（有证据时）
    if has_evidence:
        actions.append(ResultAction(kind="view_evidence", label="查看依据", enabled=True))

    # 调整参数
    actions.append(ResultAction(kind="adjust_params", label="调整参数", enabled=not applied))

    # 重试
    actions.append(ResultAction(kind="retry", label="重试", enabled=not applied))

    # 编辑类结果（variant / subtitle_draft / script）
    if kind in ("variant", "subtitle_draft", "script"):
        if not applied:
            actions.append(ResultAction(
                kind="preview_diff", label="预览 diff",
                enabled=not applied, disabled_reason="已应用，无需预览" if applied else "",
            ))
        # 审批
        if not approved and can_approve:
            actions.append(ResultAction(
                kind="approve", label="审批", enabled=True,
                requires_permission="apply_with_each_approval",
            ))
        # 应用（仅 approved 后）
        actions.append(ResultAction(
            kind="apply", label="应用",
            enabled=approved and not applied and trust != "untrusted",
            disabled_reason=(
                "需先审批" if not approved
                else ("不可信结果需人工核对" if trust == "untrusted"
                      else ("已应用" if applied else ""))
            ),
        ))
        if applied:
            actions.append(ResultAction(kind="discard", label="撤销应用", enabled=True))

    # 生成素材
    if kind == "generated_media":
        actions.append(ResultAction(
            kind="locate_in_pool", label="查看生成素材箱", enabled=True,
        ))
        if not applied:
            # 红线（模块 docstring 8-9 行）："导入媒体池"是应用类写操作，审批前
            # 必须禁用（AUD-22210；控制器侧 import_to_pool 直接写媒体池）。
            actions.append(ResultAction(
                kind="import_to_pool", label="导入媒体池",
                enabled=approved,
                disabled_reason="" if approved else "需先审批",
            ))
            # place_on_timeline 是"提出放置方案"类动作：控制器侧只会产出
            # 待审批 add_clip plan 并走统一审批/CommandStack，不直接写时间线，
            # 因此允许未审批时点击（提案语义，与"应用/导出"类按钮不同）。
            actions.append(ResultAction(
                kind="place_on_timeline", label="放入时间线（需审批）", enabled=True,
            ))

    # 字幕草稿：在编辑器打开
    if kind == "subtitle_draft":
        actions.append(ResultAction(kind="open_in_editor", label="在字幕编辑器打开", enabled=True))

    # 诊断
    if has_diagnostics:
        actions.append(ResultAction(kind="view_diagnostics", label="查看诊断", enabled=True))

    return tuple(actions)


# ---------------------------------------------------------------------- #
# 便捷构造器（controller 用）
# ---------------------------------------------------------------------- #


def variant_result(
    result_id: str, title: str, summary: str,
    *, evidence_refs: tuple[ResultEvidenceRef, ...] = (),
    approved: bool = False, applied: bool = False, trust: TrustLevel = "trusted",
    diagnostics: tuple[dict[str, Any], ...] = (),
) -> ResultCardState:
    return ResultCardState(
        result_id=result_id, kind="variant", title=title, summary=summary,
        evidence_refs=evidence_refs, approved=approved, applied=applied,
        trust=trust, diagnostics=diagnostics,
        available_actions=default_actions(
            "variant", approved=approved, applied=applied,
            has_evidence=bool(evidence_refs), trust=trust,
            has_diagnostics=bool(diagnostics),
        ),
    )


def subtitle_draft_result(
    result_id: str, title: str, summary: str, *,
    trust: TrustLevel = "unverified",   # 字幕草稿默认待核
    evidence_refs: tuple[ResultEvidenceRef, ...] = (),
    approved: bool = False, applied: bool = False,
) -> ResultCardState:
    return ResultCardState(
        result_id=result_id, kind="subtitle_draft", title=title, summary=summary,
        trust=trust, evidence_refs=evidence_refs, approved=approved, applied=applied,
        available_actions=default_actions(
            "subtitle_draft", approved=approved, applied=applied,
            has_evidence=bool(evidence_refs), trust=trust,
        ),
    )


def evidence_result(
    result_id: str, title: str, summary: str, *,
    trust: TrustLevel = "trusted",
    evidence_refs: tuple[ResultEvidenceRef, ...] = (),
) -> ResultCardState:
    return ResultCardState(
        result_id=result_id, kind="evidence", title=title, summary=summary,
        trust=trust, evidence_refs=evidence_refs,
        available_actions=default_actions(
            "evidence", has_evidence=bool(evidence_refs), trust=trust,
        ),
    )


def generated_media_result(
    result_id: str, title: str, summary: str, *,
    trust: TrustLevel = "unverified", detail: str = "",
    approved: bool = False, applied: bool = False,
) -> ResultCardState:
    return ResultCardState(
        result_id=result_id, kind="generated_media", title=title, summary=summary,
        detail=detail, trust=trust, approved=approved, applied=applied,
        available_actions=default_actions(
            "generated_media", approved=approved, applied=applied, trust=trust,
        ),
    )


def qa_result(
    result_id: str, title: str, summary: str, *,
    diagnostics: tuple[dict[str, Any], ...] = (),
) -> ResultCardState:
    return ResultCardState(
        result_id=result_id, kind="qa_report", title=title, summary=summary,
        diagnostics=diagnostics,
        available_actions=default_actions(
            "qa_report", has_diagnostics=bool(diagnostics), has_evidence=False,
        ),
    )


def error_result(
    result_id: str, title: str, error_code: str, error_message: str, *,
    diagnostics: tuple[dict[str, Any], ...] = (),
) -> ResultCardState:
    return ResultCardState(
        result_id=result_id, kind="error", title=title, summary=error_message,
        detail=error_message, error_code=error_code, diagnostics=diagnostics,
        available_actions=default_actions(
            "error", has_diagnostics=bool(diagnostics), has_evidence=False,
        ),
    )


__all__ = [
    "ActionKind",
    "ResultAction",
    "ResultCardState",
    "ResultEvidenceRef",
    "ResultKind",
    "TrustLevel",
    "default_actions",
    "error_result",
    "evidence_result",
    "generated_media_result",
    "qa_result",
    "subtitle_draft_result",
    "variant_result",
]
