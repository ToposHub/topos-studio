"""result_card_builder —— RunRecord → ResultCardState 聚合层（P1.5 渲染骨架）。

把 :class:`RunRecord` + 可选 ``artifact_resolver`` 聚合成 :class:`ResultCardState`，
供 controller 在 run 终态后 emit 到 panel 渲染。

设计要点（红线）：
- **纯函数，无 Qt 依赖**（与 ``result_card.py`` 同层）。
- **不读原文/路径/secret**：只用 RunRecord 公开字段 + typed artifact 的
  ``evidence_refs``/``citations``/``trust``/``findings``/``summary``。
- **降级优先**：任何异常 → ``error_result``，绝不抛出（聚合失败不应阻断对话流）。
- **trust 只降级不升级**：EvidenceSet 有 ``untrusted_count>0`` → 整体 ``unverified``；
  其他默认 ``trusted``。
- **ACTIVE 状态 → None**：运行中不渲染终态卡（避免误导）。
"""
from __future__ import annotations

import logging
from typing import Any, Callable

from src.features.ai_harness.invocation_models import RunRecord, RunStatus
from src.features.ai_harness.result_card import (
    ResultCardState,
    ResultEvidenceRef,
    TrustLevel,
    error_result,
    evidence_result,
    qa_result,
    generated_media_result,
    variant_result,
)

logger = logging.getLogger(__name__)

#: typed artifact 类名 → ResultKind 映射（首版按 task_artifacts 的类名匹配）。
#: 未命中 → variant 通用兜底。
_KIND_BY_TYPE_NAME: dict[str, str] = {
    "ScriptDraft": "variant",      # 创意方向（Brief/hook），非 timeline 变更
    "StoryOutline": "variant",     # 故事大纲归 variant
    "EvidenceSet": "evidence",
    "QaReport": "qa_report",
}


def build_result_card_from_run(
    run: RunRecord,
    *,
    skill_id: str = "",
    artifact_resolver: Callable[[str], Any] | None = None,
) -> ResultCardState | None:
    """RunRecord → ResultCardState 聚合（P1.5 渲染骨架）。

    Args:
        run: 终态 RunRecord（ACTIVE 状态返回 None）。
        skill_id: 触发本次 run 的 skill（用于标题可追溯）。
        artifact_resolver: ``artifact_id → typed artifact | None`` 可调用对象。
            非 None 时用它取 typed artifact 推断 kind/trust/evidence_refs；
            None 时只用 :class:`ArtifactRef.summary` 兜底。

    Returns:
        :class:`ResultCardState`；ACTIVE/SKIPPED 返回 None；聚合异常降级 error_result。
    """
    try:
        # ACTIVE / SKIPPED 不渲染终态卡（避免误导用户以为已完成）。
        if not run.is_terminal:
            return None
        if run.status == RunStatus.SKIPPED:
            return None

        # FAILED / CANCELLED → error_result
        if run.status == RunStatus.FAILED:
            return _build_error_from_run(run, skill_id)
        if run.status == RunStatus.CANCELLED:
            return error_result(
                result_id=run.run_id,
                title=_title(skill_id, "已取消"),
                error_code="CANCELLED",
                error_message=run.cancel_reason or "运行已取消",
                diagnostics=run.diagnostics,
            )

        # SUCCEEDED / SUCCEEDED_WITH_WARNINGS → 按 artifact 推断
        return _build_success_card(run, skill_id, artifact_resolver)
    except Exception as exc:  # noqa: BLE001 — 聚合失败降级，不阻断对话流
        logger.debug("build_result_card_from_run failed: %s", exc, exc_info=True)
        return error_result(
            result_id=getattr(run, "run_id", ""),
            title=_title(skill_id, "渲染失败"),
            error_code="RENDER_FAILED",
            error_message=str(exc)[:200],
        )


# ---------------------------------------------------------------------- #
# 内部：FAILED / SUCCEEDED 分支
# ---------------------------------------------------------------------- #


def _build_error_from_run(run: RunRecord, skill_id: str) -> ResultCardState:
    """FAILED run → error_result（含 error_code/error_message/diagnostics）。"""
    err_code = run.error_code or "RUN_FAILED"
    err_msg = run.error_message or f"运行失败（{run.status}）"
    return error_result(
        result_id=run.run_id,
        title=_title(skill_id, "运行失败"),
        error_code=err_code,
        error_message=err_msg,
        diagnostics=run.diagnostics,
    )


def _build_success_card(
    run: RunRecord,
    skill_id: str,
    artifact_resolver: Callable[[str], Any] | None,
) -> ResultCardState:
    """SUCCEEDED run → 按 artifact 推断 kind；无 artifact → 通用 info 卡。"""
    # 取第一个 artifact（首版只渲染主产物；多产物后续批扩展）。
    if not run.artifacts:
        # 无 artifact → 通用完成卡（info 性质，用 variant_result 通用兜底 + 无动作语境）
        title = _title(skill_id, "运行完成")
        summary = "本次运行未产出可展示的 artifact。"
        return variant_result(
            result_id=run.run_id, title=title, summary=summary,
            trust="trusted",
        )

    art = run.artifacts[0]
    typed = _resolve_artifact(art.artifact_id, artifact_resolver)
    if str(art.artifact_type).startswith("generated_"):
        return generated_media_result(
            result_id=run.run_id,
            title=_title(skill_id, "生成素材已入箱"),
            summary=art.summary or f"候选 {art.artifact_id} 已进入生成素材箱，等待预览。",
            detail=_generated_detail(typed),
            trust="unverified",
        )
    if typed is None:
        # resolver 未取到 → 用 ArtifactRef.summary 兜底（不崩）
        return variant_result(
            result_id=run.run_id,
            title=_title(skill_id, "运行完成"),
            summary=art.summary or f"产物 {art.artifact_id}",
            trust="trusted",
        )

    return _build_card_from_typed(run, skill_id, typed, art.summary)


def _build_card_from_typed(
    run: RunRecord,
    skill_id: str,
    typed: Any,
    fallback_summary: str,
) -> ResultCardState:
    """typed artifact → ResultCardState（按类名映射 kind）。"""
    kind = _kind_for_typed(typed)
    title = _title(skill_id, _default_title(kind))
    summary = _typed_summary(typed) or fallback_summary or "运行完成"

    if kind == "evidence":
        evidence_refs, trust = _evidence_refs_and_trust(typed)
        return evidence_result(
            result_id=run.run_id, title=title, summary=summary,
            trust=trust, evidence_refs=evidence_refs,
        )
    if kind == "qa_report":
        diagnostics = _qa_diagnostics(typed)
        return qa_result(
            result_id=run.run_id, title=title, summary=summary,
            diagnostics=diagnostics,
        )
    # variant / 其他 → 通用兜底
    evidence_refs = _evidence_refs_simple(typed)
    return variant_result(
        result_id=run.run_id, title=title, summary=summary,
        trust="trusted", evidence_refs=evidence_refs,
    )


# ---------------------------------------------------------------------- #
# 内部：typed artifact 字段提取（防御性 getattr，跨多种 artifact 形状）
# ---------------------------------------------------------------------- #


def _resolve_artifact(
    artifact_id: str,
    resolver: Callable[[str], Any] | None,
) -> Any:
    """调 resolver 取 typed artifact；异常/None 返回 None。"""
    if resolver is None:
        return None
    try:
        return resolver(artifact_id)
    except Exception:  # noqa: BLE001
        logger.debug("artifact resolver failed for %s", artifact_id, exc_info=True)
        return None


def _kind_for_typed(typed: Any) -> str:
    """typed artifact → ResultKind（双路探测，鲁棒匹配 fake/真实类名）。

    1. 类名精确匹配 / 后缀匹配（处理测试 fake 的 ``_Fake`` 前缀）；
    2. 结构特征探测（无 citations 但有 findings → qa_report；有 citations → evidence）。
    未命中 → variant 通用兜底。
    """
    type_name = type(typed).__name__
    # 精确匹配
    if type_name in _KIND_BY_TYPE_NAME:
        return _KIND_BY_TYPE_NAME[type_name]
    # 后缀匹配（_FakeEvidenceSet → EvidenceSet）
    for real_name, kind in _KIND_BY_TYPE_NAME.items():
        if type_name.endswith(real_name):
            return kind
    # 结构特征探测（无类名信息时）
    if hasattr(typed, "findings") and not hasattr(typed, "citations"):
        return "qa_report"
    if hasattr(typed, "citations"):
        return "evidence"
    return "variant"


def _evidence_refs_simple(typed: Any) -> tuple[ResultEvidenceRef, ...]:
    """从 typed artifact.evidence_refs（segment_id 列表）构造简单 evidence_refs。

    适用于 ScriptDraft / StoryOutline 等（evidence_refs 是 tuple[str,...]）。
    每条标 trust=trusted（首版不深挖 segment 源）。
    """
    refs = getattr(typed, "evidence_refs", ()) or ()
    out: list[ResultEvidenceRef] = []
    for seg_id in refs:
        if not seg_id:
            continue
        out.append(ResultEvidenceRef(
            evidence_id=str(seg_id), trust="trusted",
        ))
    return tuple(out)


def _evidence_refs_and_trust(
    typed: Any,
) -> tuple[tuple[ResultEvidenceRef, ...], TrustLevel]:
    """EvidenceSet 专用：从 citations 构造 evidence_refs，并按 untrusted_count 推 trust。

    trust 只降级：有任何 untrusted citation → 整体 unverified（红线：OCR 不当字幕事实）。
    """
    citations = getattr(typed, "citations", ()) or ()
    untrusted_count = getattr(typed, "untrusted_count", None)
    if untrusted_count is None:
        # 无 untrusted_count 属性时手动统计
        untrusted_count = sum(
            1 for c in citations if getattr(c, "trust", "") == "untrusted"
        )
    trust: TrustLevel = "unverified" if untrusted_count > 0 else "trusted"
    out: list[ResultEvidenceRef] = []
    for c in citations:
        seg_id = getattr(c, "segment_id", "") or getattr(c, "evidence_id", "")
        if not seg_id:
            continue
        c_trust = getattr(c, "trust", "trusted")
        # 单条 trust 映射：untrusted → untrusted；其他 → trusted（不升级）
        ref_trust: TrustLevel = "untrusted" if c_trust == "untrusted" else "trusted"
        out.append(ResultEvidenceRef(
            evidence_id=str(seg_id),
            kind=getattr(c, "kind", ""),
            trust=ref_trust,
        ))
    return tuple(out), trust


def _qa_diagnostics(typed: Any) -> tuple[dict[str, Any], ...]:
    """QaReport.findings → diagnostics dict 列表。"""
    findings = getattr(typed, "findings", ()) or ()
    out: list[dict[str, Any]] = []
    for f in findings:
        try:
            out.append({
                "finding_id": getattr(f, "finding_id", ""),
                "category": getattr(f, "category", ""),
                "severity": getattr(f, "severity", "info"),
                "message": getattr(f, "message", ""),
                "suggested_action": getattr(f, "suggested_action", ""),
            })
        except Exception:  # noqa: BLE001
            continue
    return tuple(out)


def _typed_summary(typed: Any) -> str:
    """从 typed artifact 取可读摘要（to_dict().summary 或 audience/tone 拼接）。"""
    # 优先用 summary 字段
    summary = getattr(typed, "summary", "") or ""
    if summary:
        return str(summary)
    # ScriptDraft：用 audience/tone 拼接
    audience = getattr(typed, "audience", "")
    tone = getattr(typed, "tone", "")
    parts = [p for p in (audience, tone) if p]
    if parts:
        return " / ".join(parts)
    return ""


def _generated_detail(typed: Any) -> str:
    """从 GeneratedAsset 提取不含路径/原文的可审阅元数据。"""
    if typed is None:
        return ""
    provenance = getattr(typed, "provenance", None)
    safety = getattr(typed, "safety", None)
    modality = getattr(getattr(typed, "modality", None), "value", "")
    state = getattr(getattr(typed, "state", None), "value", "")
    license_value = getattr(getattr(typed, "license", None), "value", "")
    provider = getattr(provenance, "provider_id", "")
    model = getattr(provenance, "model_id", "")
    safety_value = "通过" if not getattr(safety, "blocked", False) else "已拦截"
    parts = [
        f"类型：{modality}" if modality else "",
        f"状态：{state}" if state else "",
        f"Provider：{provider}" if provider else "",
        f"模型：{model}" if model else "",
        f"许可：{license_value}" if license_value else "",
        f"安全：{safety_value}",
    ]
    return " · ".join(p for p in parts if p)


# ---------------------------------------------------------------------- #
# 内部：标题工具
# ---------------------------------------------------------------------- #


def _title(skill_id: str, fallback: str) -> str:
    """构造可追溯标题：含 skill_id 时优先，否则 fallback。"""
    if skill_id:
        # 取 skill_id 末段（如 topos.auto_caption → auto_caption）
        short = skill_id.split(".")[-1] if "." in skill_id else skill_id
        return f"{short} · {fallback}"
    return fallback


def _default_title(kind: str) -> str:
    """按 kind 给默认副标题。"""
    return {
        "variant": "方案已生成",
        "evidence": "证据已收集",
        "qa_report": "QA 报告",
        "error": "运行失败",
        "info": "运行完成",
    }.get(kind, "运行完成")


__all__ = ["build_result_card_from_run"]
