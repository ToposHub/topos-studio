"""端到端 Skill 产物构造（plan §10.4 P2.2）。

四个官方 Skill 的产物构造器：

1. **自动字幕**（auto_caption）：转录 evidence → 字幕草稿 → EditPlan（仅
   ``ENSURE_SUBTITLE_TRACK`` + ``ADD_SUBTITLE`` 提议，不直接写）。
2. **字幕翻译与配音**（subtitle_localization）：字幕草稿 → 翻译 text_document →
   Qwen3-TTS media_generation_ref → 音轨放置 EditPlan。生成音频先入生成素材箱。
3. **口播精剪**（voiceover_polish）：复用既有 ``WorkflowRunner`` 的 A/B variant，
   产 EditPlan（已有 plan_repository 路径）。
4. **产品短片粗剪**（product_ad_rough_cut）：Brief + 素材分析 → 选段 → variant
   → EditPlan。

红线（plan §10.4 P2.2 / §1.4）：

- **字幕、音频放置、粗剪都只产** ``EditPlan``（``mode=PREVIEW``）；不直接写时间线。
- 生成 / 已有音频先入媒体池 / 生成素材箱；只有最后的 Diff / Approval 写入。
- 字幕草稿 / 翻译是 ``text_document`` artifact，OCR/网页不可信文本标 untrusted。

本模块纯 Python；不执行真实 provider（转录 / TTS / LLM 由 host 装配注入）。
它只做"evidence → EditPlan 提议 + typed artifact"的确定性映射。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Sequence

from src.features.edit.ai_editing.models import (
    EditOperation,
    EditPlan,
    OperationKind,
    PlanMode,
    PlanTarget,
)
from src.features.edit.ai_editing.subtitle_draft import build_subtitle_draft


# ---------------------------------------------------------------------- #
# 通用产物
# ---------------------------------------------------------------------- #


@dataclass(frozen=True)
class SkillArtifact:
    """Skill 产出的 typed artifact（plan §0.4：小型可验证引用）。"""

    artifact_id: str
    artifact_type: str          # subtitle_draft / text_document / media_generation_ref / edit_plan_ref
    summary: str = ""
    trust: str = "unverified"   # 默认待核（plan §1.3）
    payload_ref: str = ""       # 指向 BriefService/ArtifactStore/GenerationBin 的 id
    # A generated candidate is not timeline-ready until the host explicitly
    # imports it into the Media Pool.  The imported id is the only value an
    # editorial plan may place on a timeline.
    media_asset_id: str = ""
    duration_frames: int = 0
    start_frame: int = 0


@dataclass(frozen=True)
class SkillOutcome:
    """一个 Skill 的最终产物：EditPlan 提议 + typed artifacts + 诊断。

    红线：EditPlan 永远 mode=PREVIEW；审批后才 commit。
    """

    plan: EditPlan | None
    artifacts: tuple[SkillArtifact, ...] = ()
    diagnostics: tuple[dict[str, Any], ...] = ()
    degradation: str = ""

    @property
    def degraded(self) -> bool:
        return bool(self.degradation)


# ---------------------------------------------------------------------- #
# 1. 自动字幕
# ---------------------------------------------------------------------- #


def build_auto_caption(
    *,
    project_id: str,
    timeline_id: str,
    base_revision: int,
    transcript_segments: Sequence[Any],
    fps: float = 24.0,
    track_name: str = "AI 字幕",
    brief_id: str | None = None,
) -> SkillOutcome:
    """自动字幕 Skill（plan §10.4：Qwen → Demucs/WhisperX fallback → 字幕提议 → 审批）。

    输入：transcript ``SemanticSegment`` 列表（来自 evidence）。
    输出：

    - ``subtitle_draft`` artifact（typed，待核）。
    - EditPlan：``ENSURE_SUBTITLE_TRACK`` + 每行一个 ``ADD_SUBTITLE`` 提议
      （mode=PREVIEW，不写时间线）。

    无 transcript → degradation，不伪造字幕。
    """
    # 1. 构造字幕草稿（复用现有 build_subtitle_draft）
    draft = build_subtitle_draft(transcript_segments, fps=fps)
    if draft.empty:
        return SkillOutcome(
            plan=None, artifacts=(),
            diagnostics=({"severity": "warning", "code": "NO_TRANSCRIPT",
                          "message": "无转录证据，无法生成字幕"},),
            degradation="无转录证据",
        )

    # 2. 字幕草稿 artifact
    draft_artifact = SkillArtifact(
        artifact_id=f"subtitle_draft:{uuid.uuid4().hex[:12]}",
        artifact_type="subtitle_draft",
        summary=f"{len(draft.lines)} 行字幕（avg {draft.average_cps:.1f} cps）"
        if draft.average_cps else f"{len(draft.lines)} 行字幕",
        trust="unverified",  # 字幕草稿默认待核
    )

    # 3. EditPlan 提议（PREVIEW）。字幕轨道与 cue 使用稳定 ID，保证
    # preview/commit 两次编译得到同一份 diff，而不是每次随机生成对象 ID。
    plan_id = str(uuid.uuid4())
    track_id = "ai-subtitle-" + uuid.uuid5(
        uuid.NAMESPACE_URL,
        f"topos-color/{project_id}/{timeline_id}/{track_name}",
    ).hex[:20]
    ops: list[EditOperation] = [
        EditOperation(
            op_id="ensure_subtitle_track",
            kind=OperationKind.ENSURE_SUBTITLE_TRACK,
            params={"track_id": track_id, "track_name": track_name},
            intent={"reason": "auto_caption: ensure subtitle track"},
        )
    ]
    for i, line in enumerate(draft.lines):
        ops.append(EditOperation(
            op_id=f"sub_{i:03d}",
            kind=OperationKind.ADD_SUBTITLE,
            params={
                "track_id": track_id,
                "track_name": track_name,
                "timeline": {
                    "start": line.source_range.start,
                    "duration": line.source_range.duration,
                },
                "start_frame": line.source_range.start,
                "duration_frames": line.source_range.duration,
                "text": line.text,
                "speaker": line.speaker,
                "evidence_ref": line.evidence_ref,
            },
            intent={"reason": "auto_caption: from transcript evidence",
                    "evidence_ref": line.evidence_ref,
                    "reading_speed_cps": line.reading_speed_cps},
        ))

    plan = EditPlan.create(
        target=PlanTarget(project_id=project_id, timeline_id=timeline_id),
        base_revision=base_revision,
        operations=tuple(ops),
        mode=PlanMode.PREVIEW,
        title="自动字幕提议",
        rationale="从转录证据生成的句级字幕草稿，待预览/审批后写入",
        plan_id=plan_id,
        brief_id=brief_id,
    )

    diagnostics: list[dict[str, Any]] = []
    if draft.over_fast_lines:
        diagnostics.append({
            "severity": "info", "code": "READING_SPEED_FAST",
            "message": f"{len(draft.over_fast_lines)} 行阅读速度过快，建议人工核对",
            "line_ids": list(draft.over_fast_lines),
        })

    return SkillOutcome(
        plan=plan, artifacts=(draft_artifact,), diagnostics=tuple(diagnostics),
    )


# ---------------------------------------------------------------------- #
# 2. 字幕翻译与配音
# ---------------------------------------------------------------------- #


@dataclass(frozen=True)
class TranslationResult:
    """翻译产物（text_document）。"""

    text_document_id: str
    target_language: str
    line_count: int
    term_warnings: tuple[str, ...] = ()


def build_subtitle_localization(
    *,
    project_id: str,
    timeline_id: str,
    base_revision: int,
    subtitle_lines: Sequence[Any],   # SubtitleLine 或 (start, dur, text) 元组
    translation: TranslationResult | None,
    tts_media_ref: SkillArtifact | None = None,
    target_language: str = "",
    audio_track_name: str = "AI 配音",
    brief_id: str | None = None,
) -> SkillOutcome:
    """字幕翻译与配音 Skill（plan §9.2：字幕 → 翻译 → TTS → 音轨提议 → 审批）。

    红线：

    - 翻译是 ``text_document`` artifact（待核）。
    - TTS 音频先入生成素材箱（``tts_media_ref``）；本函数只产**音轨放置 EditPlan
      提议**（``ADD_CLIP`` 风格的音频放置），不直接插音轨。
    - 会覆盖现有音频时默认提议新建"AI 配音"轨道。
    """
    artifacts: list[SkillArtifact] = []
    diagnostics: list[dict[str, Any]] = []

    # 翻译 artifact
    if translation is not None:
        artifacts.append(SkillArtifact(
            artifact_id=translation.text_document_id,
            artifact_type="text_document",
            summary=f"翻译为 {translation.target_language}（{translation.line_count} 行）",
            trust="unverified",
        ))
        if translation.term_warnings:
            diagnostics.append({
                "severity": "warning", "code": "TRANSLATION_TERMS",
                "message": "术语不一致，建议核对",
                "warnings": list(translation.term_warnings),
            })

    # TTS 音频引用（已入生成素材箱）
    if tts_media_ref is not None:
        artifacts.append(tts_media_ref)

    # 音轨放置 EditPlan 提议（PREVIEW）
    ops: list[EditOperation] = []
    if tts_media_ref is not None:
        # Only a Media Pool id is allowed here.  A GenerationBin id/remote URL
        # is intentionally insufficient: the host must materialize + probe +
        # import the candidate before a plan can place it on a timeline.
        media_asset_id = str(
            tts_media_ref.media_asset_id or tts_media_ref.payload_ref or ""
        ).strip()
        if not media_asset_id:
            diagnostics.append({
                "severity": "warning", "code": "TTS_MEDIA_NOT_MATERIALIZED",
                "message": "TTS 候选尚未物化并导入媒体池，暂不生成音频写入计划",
                "artifact_id": tts_media_ref.artifact_id,
                "next_step": "预览候选 → 物化/探测 → 导入媒体池 → 重新规划",
            })
        else:
            start_frame, inferred_duration = _subtitle_frame_bounds(subtitle_lines)
            start_frame = tts_media_ref.start_frame or start_frame
            duration_frames = tts_media_ref.duration_frames or inferred_duration
            if duration_frames <= 0:
                diagnostics.append({
                    "severity": "warning", "code": "TTS_MEDIA_DURATION_MISSING",
                    "message": "已导入 TTS 音频缺少有效时长，无法安全放置",
                    "media_asset_id": media_asset_id,
                })
            else:
                track_id = "ai-dub-" + uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    f"topos-color/{project_id}/{timeline_id}/{audio_track_name}",
                ).hex[:20]
                ops.extend((
                    EditOperation(
                        op_id="ensure_dub_audio_track",
                        kind=OperationKind.ENSURE_TRACK,
                        params={
                            "track_id": track_id,
                            "track_type": "audio",
                            "name": audio_track_name,
                        },
                        intent={"reason": "subtitle_localization: ensure AI dub track"},
                    ),
                    EditOperation(
                        op_id="place_dub_audio",
                        kind=OperationKind.ADD_CLIP,
                        params={
                            "track_id": track_id,
                            "asset_id": media_asset_id,
                            "source": {"start": 0, "duration": duration_frames},
                            "timeline": {"start": start_frame, "duration": duration_frames},
                        },
                        intent={
                            "reason": "subtitle_localization: place materialized TTS audio",
                            "media_asset_id": media_asset_id,
                            "target_language": target_language or (
                                translation.target_language if translation else ""
                            ),
                        },
                    ),
                ))

    plan: EditPlan | None = None
    if ops:
        plan = EditPlan.create(
            target=PlanTarget(project_id=project_id, timeline_id=timeline_id),
            base_revision=base_revision,
            operations=tuple(ops),
            mode=PlanMode.PREVIEW,
            title="字幕翻译与配音提议",
            rationale="翻译字幕 + TTS 配音放置提议，待预览/审批",
            brief_id=brief_id,
        )
    else:
        diagnostics.append({
            "severity": "warning", "code": "NO_TTS_MEDIA",
            "message": "未提供 TTS 音频引用，仅产翻译 artifact",
        })

    return SkillOutcome(plan=plan, artifacts=tuple(artifacts),
                        diagnostics=tuple(diagnostics))


def _subtitle_frame_bounds(lines: Sequence[Any]) -> tuple[int, int]:
    """Derive a conservative frame span from subtitle/evidence lines."""
    ranges: list[tuple[int, int]] = []
    for line in lines:
        source_range = getattr(line, "source_range", None)
        if source_range is not None:
            start = int(getattr(source_range, "start", 0))
            duration = int(getattr(source_range, "duration", 0))
        elif isinstance(line, (tuple, list)) and len(line) >= 2:
            start, duration = int(line[0]), int(line[1])
        else:
            continue
        if duration > 0:
            ranges.append((start, duration))
    if not ranges:
        return 0, 0
    start = min(item[0] for item in ranges)
    end = max(item[0] + item[1] for item in ranges)
    return start, max(0, end - start)


# ---------------------------------------------------------------------- #
# 3 & 4. 口播精剪 / 产品粗剪
# ---------------------------------------------------------------------- #


def build_rough_cut_plan(
    *,
    project_id: str,
    timeline_id: str,
    base_revision: int,
    variant_plan: Any,   # 既有 VariantPlanner 产的 EditPlan（已有 plan_repository 路径）
    title: str = "粗剪方案提议",
    brief_id: str | None = None,
) -> SkillOutcome:
    """口播精剪 / 产品粗剪 Skill 的 EditPlan 包装（plan §10.4）。

    复用既有 ``VariantPlanner`` 产的 ``EditPlan``（已存 plan_repository）；
    本函数确保它是 ``PREVIEW`` 模式 + 可追溯引用，并产 ``edit_plan_ref`` artifact。

    红线：粗剪只产 EditPlan；审批后才写入（既有 select/validate/apply 路径）。
    """
    if variant_plan is None:
        return SkillOutcome(
            plan=None, artifacts=(),
            diagnostics=({"severity": "warning", "code": "NO_VARIANT_PLAN",
                          "message": "无粗剪方案（可能因证据不足降级）"},),
            degradation="无粗剪方案",
        )
    # 强制 PREVIEW 模式（若已是 COMMIT 也降级为 PREVIEW —— skill 永不直接 commit）
    plan = variant_plan
    if plan.mode != PlanMode.PREVIEW:
        from dataclasses import replace
        plan = replace(plan, mode=PlanMode.PREVIEW, title=title)

    artifact = SkillArtifact(
        artifact_id=f"edit_plan:{plan.plan_id}",
        artifact_type="edit_plan_ref",
        summary=title,
        trust="unverified",
        payload_ref=plan.plan_id,
    )
    return SkillOutcome(plan=plan, artifacts=(artifact,))


# ---------------------------------------------------------------------- #
# 5. 合成渲染后导入（plan §9.5）
# ---------------------------------------------------------------------- #


def build_composite_render_import(
    *,
    project_id: str,
    timeline_id: str,
    base_revision: int,
    render_media_ref: SkillArtifact | None,
    composite_graph_id: str = "",
    brief_id: str | None = None,
) -> SkillOutcome:
    """合成渲染后导入 Skill（plan §9.5：Composite 子图 → 渲染 → 生成素材箱）。

    渲染本体由 host provider 执行（headless ``CompositeRenderOutput``，worker
    线程）；本函数把渲染候选包装为 ``media_generation_ref`` artifact。

    红线（与 TTS 流程同构）：

    - 候选先入**生成素材箱**，本函数不产时间线写入 plan；导入媒体池是用户
      在生成素材箱里的显式动作。
    - 无渲染产物 → degradation，不伪造。
    """
    if render_media_ref is None:
        return SkillOutcome(
            plan=None, artifacts=(),
            diagnostics=({
                "severity": "warning", "code": "COMPOSITE_RENDER_NO_OUTPUT",
                "message": "无合成渲染产物（渲染失败、被取消或无可用 composite 图）",
            },),
            degradation="无合成渲染产物",
        )

    diagnostics: list[dict[str, Any]] = []
    if not render_media_ref.media_asset_id:
        diagnostics.append({
            "severity": "info", "code": "COMPOSITE_RENDER_PENDING_IMPORT",
            "message": "渲染候选已入生成素材箱，预览确认后可导入媒体池",
            "artifact_id": render_media_ref.artifact_id,
            "next_step": "生成素材箱 → 预览 → 导入媒体池",
        })

    return SkillOutcome(
        plan=None,
        artifacts=(render_media_ref,),
        diagnostics=tuple(diagnostics),
    )


# ---------------------------------------------------------------------- #
# Skill manifest 扩展（P2 新增的官方 skill）
# ---------------------------------------------------------------------- #


#: P2 新增官方 skill 的元信息（供 SkillCatalogView 显示；与 P1.2 的 _SKILL_META 合并）。
P2_SKILL_META: dict[str, dict[str, Any]] = {
    "topos.auto_caption": {
        "category": "字幕",
        "produces": "字幕草稿 + EditPlan（待审批）",
        "estimated_work": "转录 + 字幕生成（约 10–30 秒/分钟素材）",
        "trigger_examples": ("生成字幕", "自动字幕", "上字幕"),
    },
    "topos.subtitle_localization": {
        "category": "配音",
        "produces": "翻译 + TTS 配音 + EditPlan（待审批）",
        "estimated_work": "翻译 + TTS 合成（依赖翻译/TTS provider）",
        "trigger_examples": ("配音", "翻译配音", "配中文"),
    },
    "topos.composite_render_import": {
        "category": "合成",
        "produces": "合成渲染候选（入生成素材箱，待导入）",
        "estimated_work": "Composite 子图渲染（取决于图复杂度与时长）",
        "trigger_examples": ("渲染合成", "合成渲染后导入"),
    },
}


def p2_official_manifests() -> tuple[Any, ...]:
    """P2 新增的官方 SkillManifest（自动字幕与字幕翻译/配音）。

    口播精剪 / 产品粗剪复用现有 OFFICIAL_VOICEOVER_POLISH / OFFICIAL_PRODUCT_AD_30S
    （已在 SkillRegistry 注册），故不重复定义；这里注册两个新增 Skill。
    """
    from src.features.edit.ai_editing.editorial_models import SegmentKind
    from src.features.edit.ai_editing.skill_manifest import (
        SkillManifest, SkillRisk, SkillSource, ToolPolicy,
    )
    return (
        SkillManifest(
            skill_id="topos.auto_caption",
            version="1.0.0",
            title="自动字幕",
            description="从转录证据生成可编辑的句级字幕草稿（EditPlan 提议，待审批）。",
            author="Topos", source=SkillSource.OFFICIAL, license="Topos-proprietary",
            required_evidence_kinds=(SegmentKind.TRANSCRIPT,),
            produces_plan=True,
            target_beats=(),
            strategies=(),
            degrade_to="无转录证据时降级",
            brief_template={},
            tool_policy=ToolPolicy(),
            risk=SkillRisk.PROPOSAL_ONLY,
            input_schema={
                "type": "object",
                # 字幕生成没有创作 brief 依赖；它的输入来自当前时间线和
                # 已选择的音频素材。此前沿用了粗剪 Skill 的 brief_id 必填项，
                # 导致自然语言请求在真正进入 ASR 前就 COMPILE_FAILED。
                "required": ["asset_ids", "timeline_id", "project_id"],
                "properties": {
                    "asset_ids": {"type": "array", "items": {"type": "string"}},
                    "timeline_id": {"type": "string"},
                    "track_name": {"type": "string"},
                },
            },
            ui_card={"form_fields": ("track_name",), "result_card": "subtitle_draft"},
            eval_version="v1",
            roles=(),
        ),
        SkillManifest(
            skill_id="topos.subtitle_localization",
            version="1.0.0",
            title="字幕翻译与配音",
            description="翻译字幕并生成 TTS 配音（先入生成素材箱，再产音轨放置 EditPlan）。",
            author="Topos", source=SkillSource.OFFICIAL, license="Topos-proprietary",
            required_evidence_kinds=(SegmentKind.TRANSCRIPT,),
            produces_plan=True,
            target_beats=(),
            strategies=(),
            degrade_to="无翻译/TTS provider 时降级",
            brief_template={},
            tool_policy=ToolPolicy(),
            risk=SkillRisk.PROPOSAL_ONLY,
            input_schema={
                "type": "object",
                "required": ["asset_ids", "timeline_id", "project_id", "brief_id"],
                "properties": {
                    "asset_ids": {"type": "array", "items": {"type": "string"}},
                    "timeline_id": {"type": "string"},
                    "brief_id": {"type": "string"},
                    "target_language": {"type": "string"},
                    "tts_media_asset_id": {"type": "string"},
                    "tts_generated_asset_id": {"type": "string"},
                    "tts_duration_frames": {"type": "integer", "minimum": 1},
                    "tts_start_frame": {"type": "integer", "minimum": 0},
                },
            },
            ui_card={"form_fields": ("target_language",), "result_card": "subtitle_localization"},
            eval_version="v1",
            roles=(),
        ),
        SkillManifest(
            skill_id="topos.composite_render_import",
            version="1.0.0",
            title="合成渲染后导入",
            description=(
                "渲染 Composite 子图并产出候选（先入生成素材箱；"
                "导入媒体池与时间线放置由用户确认）。"
            ),
            author="Topos", source=SkillSource.OFFICIAL, license="Topos-proprietary",
            required_evidence_kinds=(),
            produces_plan=False,
            target_beats=(),
            strategies=(),
            degrade_to="无可用 composite 图或渲染失败时降级",
            brief_template={},
            tool_policy=ToolPolicy(),
            risk=SkillRisk.PROPOSAL_ONLY,
            input_schema={
                "type": "object",
                "required": ["asset_ids", "timeline_id", "project_id"],
                "properties": {
                    "asset_ids": {"type": "array", "items": {"type": "string"}},
                    "timeline_id": {"type": "string"},
                    "composite_graph_id": {"type": "string"},
                    "rendered_media_asset_id": {"type": "string"},
                },
            },
            ui_card={"form_fields": ("composite_graph_id",), "result_card": "media_generation"},
            eval_version="v1",
            roles=(),
        ),
    )


def register_p2_skills(registry: Any) -> list[Any]:
    """把 P2 官方 manifest 注册进 SkillRegistry（plan §10.4 P2.3）。

    返回注册产生的诊断列表（空表示全部成功）。已注册的同 skill_id + 同版本会被
    SkillRegistry 拒绝升级（返回诊断，不抛错）。

    口播精剪 / 产品粗剪复用现有 OFFICIAL_VOICEOVER_POLISH / OFFICIAL_PRODUCT_AD_30S
    （SkillRegistry(register_official=True) 默认已注册），故这里注册两个新 P2 skill。
    """
    diags: list[Any] = []
    for manifest in p2_official_manifests():
        existing = registry.get(manifest.skill_id)
        if existing is not None:
            # 已注册（可能是同版本）→ 跳过，不重复注册
            continue
        result = registry.register(manifest, enabled=True)
        diags.extend(result or [])
    return diags


__all__ = [
    "P2_SKILL_META",
    "SkillArtifact",
    "SkillOutcome",
    "TranslationResult",
    "build_auto_caption",
    "build_composite_render_import",
    "build_rough_cut_plan",
    "build_subtitle_localization",
    "p2_official_manifests",
    "register_p2_skills",
]
