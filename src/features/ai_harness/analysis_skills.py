"""分析 Skill（plan §10.4 P2.1）。

三个分析 Skill（``analyze_selection`` / ``analyze_bin`` / ``analyze_all_assets``）
共享**同一分析任务图**：

- 素材探测 / 代理与音轨提取 → ASR（AdaptiveTranscriptProvider）→ 低质量时
  Demucs → WhisperX fallback → diarization / 镜头 / OCR / 节拍 / 响度 →
  evidence normalize → 索引与 freshness 更新（plan §10.4 第 2 条）。
- 每一阶段都产**版本化** ``EvidenceRef``，不能把模型原始文本直接塞给 LLM
  （plan §10.4 红线）。

本模块纯 Python（不依赖 Qt / 不执行真实 provider —— 那是 host 装配的事）。
它做三件事：

1. :func:`resolve_analysis_scope` —— 把 chat / bin / all 三类范围解析成
   ``AnalysisRequest`` 用的 ``asset_ids`` 元组 + 范围确认提示（plan §0.3 第 6 条：
   "分析所有素材"必须显示素材数量 / 预计耗时，用户确认写进 invocation）。
2. :func:`build_analysis_request` —— 构造 ``AnalysisRequest``（propose→confirm→
   execute 三步流的第 1 步），含 recipe / budget / data_policy / requested_by。
3. :func:`collect_transcript_trace` —— 从 ``AssetAnalysis`` 收集完整的转录路由
   追踪（Qwen 质量 / 是否进 Demucs / 是否回退 WhisperX / 最终证据版本），满足
   plan §10.4 验收"RunRecord 明确显示 Qwen 质量、是否进入 Demucs、是否回退
   WhisperX、最终证据版本"。

红线（plan §10.4 / §1.3）：

- 默认只分析当前选择；"分析所有素材"必须显式确认。
- 媒体池的"AI 分析"开关决定是否允许主动分析（这里只读开关，由 host 注入）。
- evidence 是版本化引用；不传模型原始文本。

注意：``analyze_selection`` / ``analyze_bin`` / ``analyze_all_assets`` 不是
SkillRegistry 里的 ``SkillManifest``（它们产 evidence，不产 plan/artifact），
而是"分析任务"的参数化形式。本模块提供构造它们的统一入口。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Literal

from src.features.edit.ai_editing.analysis_request import (
    AnalysisBudget,
    AnalysisDataPolicy,
    AnalysisRecipe,
    AnalysisRequest,
)


AnalysisScopeKind = Literal["selection", "bin", "all"]


# ---------------------------------------------------------------------- #
# 范围解析
# ---------------------------------------------------------------------- #


@dataclass(frozen=True)
class AnalysisScopeResolution:
    """范围解析结果（plan §0.3 第 6 条）。"""

    kind: AnalysisScopeKind
    asset_ids: tuple[str, ...]
    count: int
    #: 范围确认提示（必须显示给用户；analyze_all 必含数量 + 预计）。
    confirm_prompt: str
    #: 是否需要用户二次确认（analyze_all 永远 True）。
    requires_confirmation: bool = False


def resolve_analysis_scope(
    *,
    kind: AnalysisScopeKind,
    selected_asset_ids: Iterable[str] = (),
    bin_asset_ids: Iterable[str] | None = None,
    all_asset_ids: Iterable[str] | None = None,
    pool_analysis_enabled: bool = True,
) -> AnalysisScopeResolution:
    """把范围意图解析成 asset_ids 元组 + 确认提示。

    Args:
        kind: selection / bin / all。
        selected_asset_ids: 当前用户选择的素材 ID（analyze_selection 用）。
        bin_asset_ids: 当前 bin 的素材 ID（analyze_bin 用）。
        all_asset_ids: 整个媒体池的素材 ID（analyze_all 用）。
        pool_analysis_enabled: 媒体池的"AI 分析"开关（plan §0.3 第 6 条：
            不允许主动分析全库时 analyze_all 被拒）。

    红线：analyze_all 默认需要二次确认；pool_analysis_enabled=False 时
    analyze_all 直接拒绝（返回空 asset_ids + 警告）。
    """
    if kind == "selection":
        ids = tuple(selected_asset_ids)
        if not ids:
            return AnalysisScopeResolution(
                kind="selection", asset_ids=(), count=0,
                confirm_prompt="未选择素材。请先选择素材再分析。",
                requires_confirmation=False,
            )
        return AnalysisScopeResolution(
            kind="selection", asset_ids=ids, count=len(ids),
            confirm_prompt=f"将分析已选的 {len(ids)} 个素材。",
            requires_confirmation=False,
        )

    if kind == "bin":
        ids = tuple(bin_asset_ids or ())
        if not ids:
            return AnalysisScopeResolution(
                kind="bin", asset_ids=(), count=0,
                confirm_prompt="当前 bin 无素材。",
                requires_confirmation=False,
            )
        return AnalysisScopeResolution(
            kind="bin", asset_ids=ids, count=len(ids),
            confirm_prompt=f"将分析当前 bin 的 {len(ids)} 个素材。",
            requires_confirmation=True,
        )

    # kind == "all"
    if not pool_analysis_enabled:
        return AnalysisScopeResolution(
            kind="all", asset_ids=(), count=0,
            confirm_prompt="媒体池的「AI 分析」开关未开启，不允许分析全部素材。",
            requires_confirmation=False,
        )
    ids = tuple(all_asset_ids or ())
    if not ids:
        return AnalysisScopeResolution(
            kind="all", asset_ids=(), count=0,
            confirm_prompt="媒体池为空。",
            requires_confirmation=False,
        )
    # 估算耗时（粗略：每素材约 15–40 秒 ASR + 分析）。
    est_min = max(1, len(ids) * 15 // 60)
    est_max = max(est_min, len(ids) * 40 // 60)
    return AnalysisScopeResolution(
        kind="all", asset_ids=ids, count=len(ids),
        confirm_prompt=(
            f"将分析媒体池中的全部 {len(ids)} 个素材，"
            f"预计耗时 {est_min}–{est_max} 分钟，"
            f"将占用本地 ASR 资源。确认后开始。"
        ),
        requires_confirmation=True,
    )


# ---------------------------------------------------------------------- #
# AnalysisRequest 构造
# ---------------------------------------------------------------------- #


def build_analysis_request(
    *,
    project_id: str,
    scope: AnalysisScopeResolution,
    recipe_id: AnalysisRecipe = AnalysisRecipe.EDITORIAL_LOCAL,
    data_policy: AnalysisDataPolicy = AnalysisDataPolicy.LOCAL_ONLY,
    requested_by: str = "skill:analyze",
    budget: AnalysisBudget | None = None,
    project_revision: int | None = None,
) -> AnalysisRequest | None:
    """构造 ``AnalysisRequest``（plan §10.4 三步流第 1 步）。

    scope.asset_ids 为空时返回 None（调用方应据此拒绝 / 提示）。

    红线：

    - ``max_assets`` 预算守门（analyze_all 大项目不爆）。
    - CLOUD_ENHANCED recipe 必须配 APPROVED_CLOUD policy（AnalysisRequest 已强制）。
    """
    if not scope.asset_ids:
        return None
    # analyze_all 时设 max_assets 预算守门（默认 200，防全库爆）。
    effective_budget = budget
    if effective_budget is None and scope.kind == "all":
        effective_budget = AnalysisBudget(max_assets=200)
    return AnalysisRequest.create(
        project_id=project_id,
        asset_ids=scope.asset_ids,
        recipe_id=recipe_id,
        provider_ids=(),  # 由 recipe 默认填充
        data_policy=data_policy,
        budget=effective_budget,
        requested_by=requested_by,
        project_revision=project_revision,
        title=f"分析（{scope.kind}，{scope.count} 素材）",
    )


# ---------------------------------------------------------------------- #
# 转录路由追踪（plan §10.4 验收：完整路径可见）
# ---------------------------------------------------------------------- #


@dataclass(frozen=True)
class TranscriptRouteStep:
    """转录路由的单步追踪（plan §10.4：完整路径，不只终态）。

    ``quality`` 为 ``None`` 表示该步的真实质量**未持久化**、无法从终态反推
    （AUD-21901：只有最终选用路径的质量等于 ``final_quality``；未选中的
    中间步骤不得冒用终态值）。provider 未来在
    ``provenance.extra["route_steps"]`` 持久化逐步质量后此值不再是 None。
    """

    provider: str                  # "qwen3_asr" / "demucs_v4" / "whisperx"
    quality: float | None          # 该步的 quality 分（0–1）；None=未持久化
    attempted: bool                # 是否真的运行了
    selected: bool                 # 是否最终选用
    note: str = ""                 # 如"质量低于阈值 0.65，进入 Demucs"


@dataclass(frozen=True)
class TranscriptTrace:
    """一次转录的完整路由追踪（plan §10.4 验收）。"""

    asset_id: str
    steps: tuple[TranscriptRouteStep, ...] = ()
    final_selection: str = ""     # AdaptiveTranscriptProvider 的 router_selection
    final_quality: float = 0.0
    has_word_alignment: bool = False
    evidence_version: str = ""    # field_provenance 的版本摘要

    @property
    def used_demucs(self) -> bool:
        return any(s.provider == "demucs_v4" and s.attempted for s in self.steps)

    @property
    def used_whisperx_fallback(self) -> bool:
        return any(s.provider == "whisperx" and s.attempted for s in self.steps)

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "asset_id": self.asset_id,
            "final_selection": self.final_selection,
            "final_quality": round(self.final_quality, 3),
            "used_demucs": self.used_demucs,
            "used_whisperx_fallback": self.used_whisperx_fallback,
            "has_word_alignment": self.has_word_alignment,
            "evidence_version": self.evidence_version,
            "steps": [
                {"provider": s.provider,
                 "quality": (round(s.quality, 3) if s.quality is not None else None),
                 "attempted": s.attempted, "selected": s.selected, "note": s.note}
                for s in self.steps
            ],
        }


def collect_transcript_trace(
    asset_id: str, analysis: Any, *, quality_threshold: float = 0.65,
) -> TranscriptTrace:
    """从 ``AssetAnalysis`` 收集完整的转录路由追踪。

    现有 ``AdaptiveTranscriptProvider`` 只在 ``provenance.extra["router_selection"]``
    记录**终态**选择（audit 结论）。本函数把它扩展成完整的多步追踪——依据
    router_selection 字符串反推路径（满足 plan §10.4 验收："明确显示 Qwen 质量、
    是否进入 Demucs、是否回退 WhisperX"）。

    若未来 provider 直接持久化中间步骤（``provenance.extra["route_steps"]``），
    本函数优先使用它。
    """
    provenance = getattr(analysis, "provenance", None)
    extra: dict[str, Any] = {}
    if provenance is not None:
        extra = dict(getattr(provenance, "extra", None) or {})

    # 优先用 provider 持久化的完整 steps（未来扩展）
    raw_steps = extra.get("route_steps")
    if isinstance(raw_steps, list):
        parsed: list[TranscriptRouteStep] = []
        for s in raw_steps:
            if not isinstance(s, dict):
                continue
            raw_quality = s.get("quality")
            try:
                step_quality = None if raw_quality is None else float(raw_quality)
            except (TypeError, ValueError):
                step_quality = None
            parsed.append(TranscriptRouteStep(
                provider=str(s.get("provider", "")),
                quality=step_quality,
                attempted=bool(s.get("attempted", True)),
                selected=bool(s.get("selected", False)),
                note=str(s.get("note", "")),
            ))
        steps = tuple(parsed)
    else:
        steps = _infer_steps_from_selection(
            asset_id, analysis, extra, quality_threshold,
        )

    final_selection = str(extra.get("router_selection", ""))
    final_quality = _analysis_quality(analysis)
    has_word_alignment = _has_word_alignment(analysis)

    # evidence 版本摘要（field_provenance 的 provider + created_at）
    field_prov = getattr(analysis, "field_provenance", None)
    transcript_prov = None
    if field_prov is not None:
        transcript_prov = getattr(field_prov, "transcript", None)
    ev_version = ""
    if transcript_prov is not None:
        ev_version = f"{getattr(transcript_prov, 'provider', '')}@{getattr(transcript_prov, 'model', '')}"

    return TranscriptTrace(
        asset_id=asset_id,
        steps=steps,
        final_selection=final_selection,
        final_quality=final_quality,
        has_word_alignment=has_word_alignment,
        evidence_version=ev_version,
    )


def _infer_steps_from_selection(
    asset_id: str, analysis: Any, extra: dict[str, Any],
    quality_threshold: float,
) -> tuple[TranscriptRouteStep, ...]:
    """依据 router_selection 字符串反推完整路径（plan §10.4 验收）。

    AUD-21901：持久化数据只有**终态**质量（最终入库的 transcript）。反推时
    只有"最终选用"的那一步才允许使用该值；未选中的中间步骤质量未持久化，
    一律标 ``quality=None`` 并在 note 里说明，不得把终态质量冒充为该步质量、
    也不得编造 0.0 之类的假值。
    """
    selection = str(extra.get("router_selection", ""))
    quality = _analysis_quality(analysis)

    steps: list[TranscriptRouteStep] = []
    # Qwen 原音频：永远先尝试
    qwen_selected = selection in ("qwen3_asr_original",
                                  "qwen3_asr_original_low_confidence")
    if qwen_selected:
        qwen_quality: float | None = quality
        qwen_note = (
            "质量达标" if quality >= quality_threshold
            else f"质量 {quality:.2f} 低于阈值 {quality_threshold:.2f}"
        )
    else:
        # Demucs 胜出 / WhisperX 回退时，Qwen 原音频步的真实质量未持久化；
        # 已知事实只有"低于路由要求才转入后续路径"。
        qwen_quality = None
        qwen_note = "质量未持久化（低于路由要求才转入后续路径）"
    steps.append(TranscriptRouteStep(
        provider="qwen3_asr", quality=qwen_quality, attempted=True,
        selected=qwen_selected,
        note=qwen_note,
    ))

    # Demucs：当 selection 是 demucs，或 qwen 低置信时（auto 路径会试 Demucs）
    used_demucs = selection == "qwen3_asr_demucs_v4"
    if used_demucs or (selection == "qwen3_asr_original_low_confidence"):
        # 只有 Demucs 胜出时终态质量才是 Demucs 路径的质量；未胜出的尝试
        # 分数未持久化（AUD-21901：不得编造 0.0）。
        demucs_quality = quality if used_demucs else None
        steps.append(TranscriptRouteStep(
            provider="demucs_v4", quality=demucs_quality, attempted=True,
            selected=used_demucs,
            note=("Demucs 分离人声后 Qwen 质量提升" if used_demucs
                  else "Demucs 未超过 Qwen 原始（该步质量未持久化）"),
        ))

    # WhisperX：selection 是 whisperx_fallback 时
    if selection == "whisperx_fallback":
        steps.append(TranscriptRouteStep(
            provider="whisperx", quality=quality, attempted=True,
            selected=True, note="Qwen/Demucs 均不足，回退 WhisperX",
        ))

    return tuple(steps)


def _analysis_quality(analysis: Any) -> float:
    """从 AssetAnalysis 估算 transcript 质量（与 router._quality 一致口径）。"""
    transcript = getattr(analysis, "transcript", ()) or ()
    if not transcript:
        return 0.0
    confs = [float(getattr(t, "confidence", 0.0) or 0.0) for t in transcript]
    avg = sum(confs) / len(confs) if confs else 0.0
    has_words = any(getattr(t, "words", ()) for t in transcript)
    if not has_words:
        avg = max(0.0, avg - 0.05)
    return max(0.0, min(1.0, avg))


def _has_word_alignment(analysis: Any) -> bool:
    transcript = getattr(analysis, "transcript", ()) or ()
    return any(getattr(t, "words", ()) for t in transcript)


# ---------------------------------------------------------------------- #
# Evidence 收集（版本化引用）
# ---------------------------------------------------------------------- #


@dataclass(frozen=True)
class EvidenceRef:
    """版本化的 evidence 引用（plan §10.4：每一阶段都产版本化引用）。"""

    asset_id: str
    kind: str             # transcript / shot / ocr / tag / audio
    segment_count: int
    provider: str
    evidence_version: str
    trust: str = "trusted"   # trusted / unverified / untrusted
    router_trace: TranscriptTrace | None = None  # transcript 才有


def collect_evidence_refs(
    asset_ids: Iterable[str], analysis_service: Any,
) -> list[EvidenceRef]:
    """从 AnalysisService 收集版本化 evidence 引用（每个 asset 的每个 field）。

    红线：返回的是引用（asset_id + kind + 版本），不是模型原始文本。
    """
    refs: list[EvidenceRef] = []
    for asset_id in asset_ids:
        analysis = analysis_service.get_analysis(asset_id)
        if analysis is None:
            continue
        field_prov = getattr(analysis, "field_provenance", None)
        for field in ("transcript", "scene_cuts", "tags", "ocr", "audio"):
            value = getattr(analysis, field, None)
            if not value:
                continue
            kind = {"scene_cuts": "shot", "tags": "tag"}.get(field, field)
            provider = ""
            ev_version = ""
            if field_prov is not None:
                fp = getattr(field_prov, field, None)
                if fp is not None:
                    provider = getattr(fp, "provider", "") or getattr(fp, "model", "")
                    ev_version = f"{getattr(fp, 'provider', '')}@{getattr(fp, 'model', '')}"
            trace = None
            trust = "trusted"
            if field == "transcript":
                trace = collect_transcript_trace(asset_id, analysis)
                provider = trace.final_selection or provider
                ev_version = trace.evidence_version or ev_version
            elif field == "ocr":
                trust = "untrusted"   # OCR 文本不可信（plan §1.3）
            refs.append(EvidenceRef(
                asset_id=asset_id, kind=kind,
                segment_count=len(value) if hasattr(value, "__len__") else 0,
                provider=provider, evidence_version=ev_version,
                trust=trust, router_trace=trace,
            ))
    return refs


__all__ = [
    "AnalysisScopeKind",
    "AnalysisScopeResolution",
    "EvidenceRef",
    "TranscriptRouteStep",
    "TranscriptTrace",
    "build_analysis_request",
    "collect_evidence_refs",
    "collect_transcript_trace",
    "resolve_analysis_scope",
]
