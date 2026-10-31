"""TAEP Plan Validator（Phase 1）。

两阶段校验的第一层完整实现（TAEP §6.1 步骤 1-5）：

1. **结构校验**（``schema.validate_plan_structure``）：JSON Schema + protocol
   version + operation 白名单 + 必填字段 + 未知字段策略。
2. **domain 校验**（本模块）：引用完整性、source range 越界、track 类型匹配、
   transition 邻接、effect registry 存在性、effect 参数范围、revision 乐观锁、
   素材离线、操作配额。

本模块**不**编译 plan 到 command（那是 P2 ``plan_compiler`` 的职责），也不提交。
它只产生 ``ValidationReport``，供 transaction_service / UI 决定是否批准。

设计要点：

- 校验需要 ``ProjectSequences``（canonical domain 上下文）与
  ``CapabilityManifest``（能力边界）。二者都可为 None（降级为纯结构校验）。
- 诊断按 op_id 定位，便于 LLM 精确修复。
- revision 冲突产生 ``REJECTED``（非异常），因为这是预期的“过期”状态。
"""

from __future__ import annotations

from typing import Any, TYPE_CHECKING

from src.features.edit.ai_editing.capability_manifest import (
    get_effect_capability,
    is_effect_known,
    is_operation_supported,
)
from src.features.edit.ai_editing.diagnostics import (
    Diagnostic,
    DiagnosticSeverity,
    TaepDiagnosticCode,
    make_diagnostic,
)
from src.features.edit.ai_editing.edit_project_service import ProjectSequences
from src.features.edit.ai_editing.editorial_models import FrameRange
from src.features.edit.ai_editing.models import (
    CapabilityManifest,
    EditOperation,
    EditPlan,
    OperationKind,
    PlanStatus,
    ValidationReport,
)
from src.features.edit.ai_editing.schema import findings_to_diagnostics, validate_plan_structure

if TYPE_CHECKING:
    from src.features.edit.core.project import MediaAsset
    from src.features.edit.core.timeline_v2 import EditTrack, TimelineSequence


def validate_plan(
    plan: EditPlan,
    *,
    state: ProjectSequences | None = None,
    manifest: CapabilityManifest | None = None,
    lang: str = "zh",
) -> ValidationReport:
    """完整校验一个 EditPlan。

    Args:
        plan: 待校验的计划。
        state: 两层状态（用于 domain 校验）。None 时跳过 domain 校验。
        manifest: capability manifest（用于 operation/effect 白名单）。None 时
            从默认 registry 构建。
        lang: 诊断消息语言。

    Returns:
        ``ValidationReport``。``has_errors`` 决定是否可提交。
    """
    if manifest is None:
        from src.features.edit.ai_editing.capability_manifest import build_manifest

        manifest = build_manifest()

    diagnostics: list[Diagnostic] = []

    # ---- 1. 结构校验（含 capability 早拒绝，TAEP §4.3）---- #
    # 显式传入 COMPILABLE_OPERATIONS，让 schema 层就能拒绝"协议合法但未实现"
    # 的 operation（如 add_subtitle），避免诱导模型生成后才在 domain 层失败。
    plan_dict = plan.to_dict()
    try:
        from src.features.edit.ai_editing.plan_compiler import COMPILABLE_OPERATIONS
        compilable_set = frozenset(k.value for k in COMPILABLE_OPERATIONS)
    except Exception:  # noqa: BLE001
        compilable_set = frozenset()
    findings = validate_plan_structure(
        plan_dict, compilable_operations=compilable_set or None,
    )
    diagnostics.extend(findings_to_diagnostics(findings, lang=lang))

    # ---- 2. 操作配额 ---- #
    max_ops = manifest.limits.get("max_operations_per_plan", 500)
    if len(plan.operations) > max_ops:
        diagnostics.append(make_diagnostic(
            DiagnosticSeverity.ERROR, TaepDiagnosticCode.PLAN_TOO_LARGE,
            path="/operations", lang=lang,
            count=len(plan.operations), limit=max_ops,
        ))

    # ---- 3. operation 白名单（manifest 级）---- #
    # 当 schema 层已启用 compilable 早拒绝（compilable_set 非空）时，未实现 kind
    # 已在 §1 报过 UNSUPPORTED_OPERATION，此处跳过避免重复诊断；
    # compilable_set 为空（compiler 不可用）时仍保留 manifest 级独立检查。
    if not compilable_set:
        for op in plan.operations:
            if not is_operation_supported(manifest, op.kind.value):
                diagnostics.append(make_diagnostic(
                    DiagnosticSeverity.ERROR, TaepDiagnosticCode.UNSUPPORTED_OPERATION,
                    op_id=op.op_id, path=f"/operations/{op.op_id}/kind", lang=lang,
                    op_kind=op.kind.value,
                ))

    # ---- 4. domain 校验（需要 state）---- #
    current_revision: int | None = None
    if state is not None:
        try:
            from src.features.edit.ai_editing.edit_project_service import EditProjectService

            EditProjectService.assert_sequences_consistent(state)
        except ValueError as exc:
            diagnostics.append(Diagnostic(
                severity=DiagnosticSeverity.ERROR,
                code=TaepDiagnosticCode.TIMELINE_NOT_FOUND,
                message=str(exc),
                path="/target",
            ))
        else:
            if plan.target.project_id != state.project.project_id:
                diagnostics.append(make_diagnostic(
                    DiagnosticSeverity.ERROR, TaepDiagnosticCode.PROJECT_NOT_FOUND,
                    path="/target/project_id", lang=lang,
                    project_id=plan.target.project_id,
                ))
                sequence = None
            else:
                sequence = state.sequences.get(plan.target.timeline_id)
            if sequence is None and plan.target.project_id == state.project.project_id:
                diagnostics.append(make_diagnostic(
                    DiagnosticSeverity.ERROR, TaepDiagnosticCode.TIMELINE_NOT_FOUND,
                    path="/target/timeline_id", lang=lang,
                    timeline_id=plan.target.timeline_id,
                ))
            if sequence is not None:
                current_revision = sequence.revision
                # revision 乐观锁
                if plan.base_revision != sequence.revision:
                    diagnostics.append(make_diagnostic(
                        DiagnosticSeverity.ERROR, TaepDiagnosticCode.REVISION_CONFLICT,
                        path="/base_revision", suggested_action="refresh_snapshot",
                        lang=lang, base=plan.base_revision, current=sequence.revision,
                    ))
                # 逐 operation domain 校验（M3.3：顺序模拟）
                # 旧实现对每个 op 独立查 base sequence，导致 add_clip(c1) → set_effect(c1)
                # 被误判 c1 不存在。现在维护一个"投影 sequence"：验证第 i 个 op 前，
                # 已把 0..i-1 的结构性 op（编译+apply）叠加到 sequence 上，使后续 op 能
                # 看到先前 op 产生的 clip/track（与 compile→MacroCommand.apply 语义一致）。
                projection = sequence  # 起始 = base
                for op in plan.operations:
                    diagnostics.extend(
                        _validate_operation_domain(op, state, projection, manifest, lang=lang)
                    )
                    # 把本 op 叠加到投影，供下一个 op 校验时引用。
                    projection = _advance_projection(projection, op)

    # ---- 汇总状态 ---- #
    has_errors = any(d.is_error for d in diagnostics)
    # revision 冲突优先标 stale（供 UI 提示重新读取）
    has_revision_conflict = any(d.code == TaepDiagnosticCode.REVISION_CONFLICT for d in diagnostics)
    if has_errors:
        status = PlanStatus.STALE if has_revision_conflict else PlanStatus.REJECTED
    else:
        status = PlanStatus.VALID

    return ValidationReport(
        plan_id=plan.plan_id,
        status=status,
        base_revision=plan.base_revision,
        current_revision=current_revision,
        diagnostics=tuple(diagnostics),
    )


def _advance_projection(projection: TimelineSequence, op: EditOperation) -> TimelineSequence:
    """把单个 op 叠加到投影 sequence 上，返回新投影（M3.3 顺序模拟）。

    供 ``validate_plan`` 在校验第 i 个 op 后，把结构性变更（add_clip / delete_clip
    / move / trim / split / replace / ensure_track 等）反映到投影，使第 i+1 个 op
    的引用检查能看到先前 op 产生的对象——与 compile→MacroCommand.apply 的顺序语义
    一致（审查 M3.3）。

    本函数**不**产生诊断（诊断由 ``_validate_operation_domain`` 负责）；任何编译/
    apply 失败都静默返回原 projection，让后续 op 的引用检查按现状进行（编译失败的
    op 已在结构/能力校验中报错）。
    """
    try:
        from src.features.edit.ai_editing.plan_compiler import compile_plan
    except Exception:
        return projection
    # 用只含该 op 的临时 plan 编译（compiler 是无状态的、按 op 编译）。
    from src.features.edit.ai_editing.models import EditPlan, PlanTarget

    try:
        tmp_plan = EditPlan(
            plan_id="_projection",
            base_revision=0,
            target=PlanTarget(project_id="", timeline_id=""),
            operations=(op,),
        )
        compiled = compile_plan(tmp_plan)
        result = compiled.macro.apply(projection)
        if isinstance(result, tuple):
            result = result[0]
        return result
    except Exception:
        # 编译/apply 失败：保持投影不变（诊断由 domain 校验产生）。
        return projection


# ---------------------------------------------------------------------- #
# 逐 operation domain 校验
# ---------------------------------------------------------------------- #


def _validate_operation_domain(
    op: EditOperation,
    state: ProjectSequences,
    sequence: TimelineSequence,
    manifest: CapabilityManifest,
    *,
    lang: str = "zh",
) -> list[Diagnostic]:
    """单个 operation 的 domain 校验（引用完整性、range、type、effect）。"""
    diags: list[Diagnostic] = []
    params = op.params
    project = state.project

    def _frame_range_from(d: dict[str, Any] | None, key: str) -> FrameRange | None:
        """从 params[key] 解析 FrameRange（容忍 {start,duration}）。"""
        raw = params.get(key) if isinstance(params, dict) else None
        if raw is None:
            return None
        if isinstance(raw, FrameRange):
            return raw
        if isinstance(raw, dict):
            try:
                return FrameRange(start=int(raw["start"]), duration=int(raw["duration"]))
            except (KeyError, TypeError, ValueError):
                return None
        return None

    kind = op.kind

    # --- ADD_CLIP / REPLACE_CLIP: source range 越界 + asset 存在 + track 类型 ---
    if kind in (OperationKind.ADD_CLIP, OperationKind.REPLACE_CLIP):
        asset_id = params.get("asset_id")
        asset = project.assets.get(asset_id) if asset_id else None
        if asset_id and asset is None:
            diags.append(make_diagnostic(
                DiagnosticSeverity.ERROR, TaepDiagnosticCode.ASSET_NOT_FOUND,
                op_id=op.op_id, path=f"/operations/{op.op_id}/params/asset_id",
                lang=lang, asset_id=asset_id,
            ))
        elif asset is not None and bool(getattr(asset, "is_offline", False)):
            diags.append(make_diagnostic(
                DiagnosticSeverity.ERROR, TaepDiagnosticCode.ASSET_OFFLINE,
                op_id=op.op_id, path=f"/operations/{op.op_id}/params/asset_id",
                lang=lang, asset_id=asset_id,
            ))
        # source range 越界
        source = _frame_range_from(params, "source")
        if asset is not None and source is not None:
            asset_duration = _asset_duration_frames(asset)
            if asset_duration is not None and source.end > asset_duration:
                diags.append(make_diagnostic(
                    DiagnosticSeverity.ERROR, TaepDiagnosticCode.SOURCE_RANGE_INVALID,
                    op_id=op.op_id, path=f"/operations/{op.op_id}/params/source",
                    lang=lang, asset_id=asset_id, duration=asset_duration,
                    start=source.start, dur=source.duration,
                ))
        # track 类型匹配（video clip → video track）
        if kind == OperationKind.ADD_CLIP:
            track_id = params.get("track_id")
            track = sequence.get_track_by_id(track_id) if track_id else None
            if track is None and track_id:
                diags.append(make_diagnostic(
                    DiagnosticSeverity.ERROR, TaepDiagnosticCode.TRACK_NOT_FOUND,
                    op_id=op.op_id, path=f"/operations/{op.op_id}/params/track_id",
                    lang=lang, track_id=track_id,
                ))
            elif asset is not None and track is not None:
                media_type = getattr(asset, "media_type", "")
                expected_track = "video" if media_type in ("video", "image") else (
                    "audio" if media_type == "audio" else None
                )
                if expected_track and track.track_type != expected_track:
                    diags.append(make_diagnostic(
                        DiagnosticSeverity.ERROR, TaepDiagnosticCode.TRACK_TYPE_MISMATCH,
                        op_id=op.op_id, path=f"/operations/{op.op_id}/params/track_id",
                        lang=lang, track_id=track_id, actual=track.track_type,
                        expected=expected_track,
                    ))

    # --- MOVE_CLIP / TRIM / SPLIT / DELETE: clip 存在 ---
    if kind in (OperationKind.MOVE_CLIP, OperationKind.TRIM_CLIP,
                OperationKind.SPLIT_CLIP, OperationKind.DELETE_CLIP,
                OperationKind.REPLACE_CLIP, OperationKind.SET_TRANSFORM,
                OperationKind.SET_CROP, OperationKind.SET_OPACITY,
                OperationKind.SET_SPEED, OperationKind.SET_CLIP_FADE,
                OperationKind.SET_EFFECT, OperationKind.REMOVE_EFFECT,
                OperationKind.UPDATE_SUBTITLE, OperationKind.DELETE_SUBTITLE):
        clip_id = params.get("clip_id")
        if clip_id:
            clip = sequence.get_clip_by_id(clip_id)
            if clip is None and kind != OperationKind.DELETE_SUBTITLE:
                # subtitle clip 在不同集合，宽松处理：只在 video/audio 找不到时报
                if sequence.get_subtitle_clip_by_id(clip_id) is None:
                    diags.append(_clip_not_found_diag(op, clip_id, lang))

    if kind in (OperationKind.UPDATE_SUBTITLE, OperationKind.DELETE_SUBTITLE):
        clip_id = params.get("clip_id")
        subtitle_clip = sequence.get_subtitle_clip_by_id(clip_id) if clip_id else None
        if subtitle_clip is not None:
            owner = sequence.find_subtitle_clip_owner_track(clip_id)
            if owner is not None and getattr(owner, "is_locked", False):
                diags.append(make_diagnostic(
                    DiagnosticSeverity.ERROR, TaepDiagnosticCode.SUBTITLE_LOCKED,
                    op_id=op.op_id,
                    path=f"/operations/{op.op_id}/params/clip_id",
                    lang=lang, clip_id=clip_id,
                ))

    # --- 字幕：目标必须是 subtitle track，且时间范围有效 ---
    if kind == OperationKind.UPDATE_SUBTITLE_TRACK:
        track_id = params.get("track_id")
        track = sequence.get_track_by_id(track_id) if track_id else None
        if track is None:
            diags.append(make_diagnostic(
                DiagnosticSeverity.ERROR, TaepDiagnosticCode.TRACK_NOT_FOUND,
                op_id=op.op_id,
                path=f"/operations/{op.op_id}/params/track_id",
                lang=lang, track_id=track_id or "",
            ))
        elif track.track_type != "subtitle":
            diags.append(make_diagnostic(
                DiagnosticSeverity.ERROR, TaepDiagnosticCode.TRACK_TYPE_MISMATCH,
                op_id=op.op_id,
                path=f"/operations/{op.op_id}/params/track_id",
                lang=lang, track_id=track_id, actual=track.track_type,
                expected="subtitle",
            ))
        elif track.is_locked:
            diags.append(make_diagnostic(
                DiagnosticSeverity.ERROR, TaepDiagnosticCode.SUBTITLE_LOCKED,
                op_id=op.op_id,
                path=f"/operations/{op.op_id}/params/track_id",
                lang=lang, track_id=track_id,
            ))

    if kind == OperationKind.ADD_SUBTITLE:
        track_id = params.get("track_id")
        track = sequence.get_track_by_id(track_id) if track_id else None
        if track is None:
            diags.append(make_diagnostic(
                DiagnosticSeverity.ERROR, TaepDiagnosticCode.TRACK_NOT_FOUND,
                op_id=op.op_id, path=f"/operations/{op.op_id}/params/track_id",
                lang=lang, track_id=track_id or "",
            ))
        elif track.track_type != "subtitle":
            diags.append(make_diagnostic(
                DiagnosticSeverity.ERROR, TaepDiagnosticCode.TRACK_TYPE_MISMATCH,
                op_id=op.op_id, path=f"/operations/{op.op_id}/params/track_id",
                lang=lang, track_id=track_id, actual=track.track_type,
                expected="subtitle",
            ))
        elif track.is_locked:
            diags.append(make_diagnostic(
                DiagnosticSeverity.ERROR, TaepDiagnosticCode.SUBTITLE_LOCKED,
                op_id=op.op_id, path=f"/operations/{op.op_id}/params/track_id",
                lang=lang, track_id=track_id,
            ))
        raw_timeline = params.get("timeline")
        start = raw_timeline.get("start") if isinstance(raw_timeline, dict) else params.get("start_frame")
        duration = raw_timeline.get("duration") if isinstance(raw_timeline, dict) else params.get("duration_frames")
        if not isinstance(start, int) or isinstance(start, bool) or start < 0:
            diags.append(make_diagnostic(
                DiagnosticSeverity.ERROR, TaepDiagnosticCode.SCHEMA_INVALID,
                op_id=op.op_id, path=f"/operations/{op.op_id}/params/timeline/start",
                lang=lang, detail="subtitle start must be a non-negative integer",
            ))
        if not isinstance(duration, int) or isinstance(duration, bool) or duration <= 0:
            diags.append(make_diagnostic(
                DiagnosticSeverity.ERROR, TaepDiagnosticCode.SCHEMA_INVALID,
                op_id=op.op_id, path=f"/operations/{op.op_id}/params/timeline/duration",
                lang=lang, detail="subtitle duration must be a positive integer",
            ))

    # --- SET_CLIP_FADE: fade 值约束（AUD-14106，此前仅查 clip 存在性）---
    if kind == OperationKind.SET_CLIP_FADE:
        fade_in = params.get("fade_in_duration")
        fade_out = params.get("fade_out_duration")
        if fade_in is None and fade_out is None:
            diags.append(make_diagnostic(
                DiagnosticSeverity.ERROR, TaepDiagnosticCode.SCHEMA_INVALID,
                op_id=op.op_id,
                path=f"/operations/{op.op_id}/params/fade_in_duration",
                lang=lang,
                detail=(
                    "set_clip_fade requires fade_in_duration or "
                    "fade_out_duration (non-negative integer frames)"
                ),
            ))
        else:
            for fade_name, fade_value in (
                ("fade_in_duration", fade_in),
                ("fade_out_duration", fade_out),
            ):
                if fade_value is None:
                    continue
                if not isinstance(fade_value, int) or isinstance(fade_value, bool) \
                        or fade_value < 0:
                    diags.append(make_diagnostic(
                        DiagnosticSeverity.ERROR,
                        TaepDiagnosticCode.SCHEMA_INVALID,
                        op_id=op.op_id,
                        path=f"/operations/{op.op_id}/params/{fade_name}",
                        lang=lang,
                        detail=f"{fade_name} must be a non-negative integer, "
                               f"got {fade_value!r}",
                    ))

    # --- ADD_TRANSITION: 引用存在 + 邻接 + duration 值约束 ---
    if kind == OperationKind.ADD_TRANSITION:
        ttype = params.get("transition_type")
        if ttype and ttype not in manifest.transitions:
            diags.append(make_diagnostic(
                DiagnosticSeverity.ERROR, TaepDiagnosticCode.TRANSITION_INVALID,
                op_id=op.op_id, path=f"/operations/{op.op_id}/params/transition_type",
                lang=lang, detail=f"transition {ttype!r} not supported",
            ))
        # AUD-14106：duration 缺省时 compiler 取默认 12，但显式提供的值
        # 必须是正整数帧数（"abc"/0/-5/12.0 之前以 VALID 放行后在 compile
        # 期炸裸 ValueError/TypeError）。
        duration = params.get("duration")
        if duration is not None and (
            not isinstance(duration, int) or isinstance(duration, bool)
            or duration <= 0
        ):
            diags.append(make_diagnostic(
                DiagnosticSeverity.ERROR, TaepDiagnosticCode.TRANSITION_INVALID,
                op_id=op.op_id,
                path=f"/operations/{op.op_id}/params/duration",
                lang=lang,
                detail=f"transition duration must be a positive integer, "
                       f"got {duration!r}",
            ))
        left_id = params.get("left_clip_id")
        right_id = params.get("right_clip_id")
        track_id = params.get("track_id")
        track = sequence.get_track_by_id(track_id) if track_id else None
        if track is None and track_id:
            diags.append(make_diagnostic(
                DiagnosticSeverity.ERROR, TaepDiagnosticCode.TRACK_NOT_FOUND,
                op_id=op.op_id, lang=lang, track_id=track_id,
            ))
        elif left_id and right_id:
            _check_transition_adjacency(diags, op, track, left_id, right_id, lang)

    # --- SET_EFFECT: effect registry + 参数范围 ---
    if kind == OperationKind.SET_EFFECT:
        etype = params.get("effect_type")
        if etype and not is_effect_known(manifest, etype):
            diags.append(make_diagnostic(
                DiagnosticSeverity.ERROR, TaepDiagnosticCode.EFFECT_INVALID,
                op_id=op.op_id, path=f"/operations/{op.op_id}/params/effect_type",
                lang=lang, effect_type=etype,
            ))
        elif etype:
            # T-P2-22 条目 2：preview_only / unsupported 效果不得进入计划
            # ——它会被 Deliver preflight 阻断；计划期即给出可解释拒绝与
            # 限制码，供规划器改提替代方案。
            capability = get_effect_capability(manifest, etype)
            if capability is not None and not capability.exportable:
                restriction = capability.delivery_restriction_code or                     ("unsupported" if capability.unsupported else "preview_only")
                diags.append(make_diagnostic(
                    DiagnosticSeverity.ERROR,
                    TaepDiagnosticCode.EFFECT_NOT_DELIVERABLE,
                    op_id=op.op_id,
                    path=f"/operations/{op.op_id}/params/effect_type",
                    lang=lang, effect_type=etype,
                    details={
                        "effect_type": etype,
                        "restriction": restriction,
                        "reason": (capability.delivery_restriction_message
                                   or "无导出后端"),
                        "suggestion": "请改用可交付效果或移除该交付步骤。",
                    },
                ))
            _check_effect_params(diags, op, manifest, etype, params.get("parameters", {}), lang)

    # --- SET_MARKER: frame 非负 ---
    if kind == OperationKind.SET_MARKER:
        frame = params.get("frame")
        if frame is not None and (not isinstance(frame, int) or isinstance(frame, bool) or frame < 0):
            diags.append(make_diagnostic(
                DiagnosticSeverity.ERROR, TaepDiagnosticCode.SCHEMA_INVALID,
                op_id=op.op_id, path=f"/operations/{op.op_id}/params/frame",
                lang=lang, detail=f"frame must be a non-negative int, got {frame!r}",
            ))

    return diags


# ---------------------------------------------------------------------- #
# 辅助
# ---------------------------------------------------------------------- #


def _asset_duration_frames(asset: MediaAsset) -> int | None:
    """从 MediaAsset.metadata 推断时长（帧）。"""
    meta = getattr(asset, "metadata", {}) or {}
    if not isinstance(meta, dict):
        return None
    for key in ("duration_frames", "frame_count", "nb_frames"):
        val = meta.get(key)
        if val is not None:
            try:
                return int(val)
            except (TypeError, ValueError):
                return None
    return None


def _clip_not_found_diag(op: EditOperation, clip_id: str, lang: str) -> Diagnostic:
    return Diagnostic(
        severity=DiagnosticSeverity.ERROR,
        code=TaepDiagnosticCode.TRACK_NOT_FOUND,  # 复用：找不到引用对象
        message=f"clip {clip_id!r} not found in timeline (op {op.op_id})",
        op_id=op.op_id,
        path=f"/operations/{op.op_id}/params/clip_id",
    )


def _check_transition_adjacency(
    diags: list[Diagnostic],
    op: EditOperation,
    track: EditTrack,
    left_id: str,
    right_id: str,
    lang: str,
) -> None:
    """转场引用的两个 clip 必须时间上相邻（TAEP §4.2 必须拒绝跨轨/不相邻）。

    相邻 = ``left.timeline_end == right.timeline_start``（半开区间端点相接）。
    仅索引相邻但时间不连续（中间有 gap）仍判为不相邻。
    """
    if track is None:
        return
    clips = list(track.clips)
    left = next((c for c in clips if c.clip_id == left_id), None)
    right = next((c for c in clips if c.clip_id == right_id), None)
    if left is None or right is None:
        diags.append(make_diagnostic(
            DiagnosticSeverity.ERROR, TaepDiagnosticCode.TRANSITION_INVALID,
            op_id=op.op_id, lang=lang,
            detail="transition references non-existent clip(s)",
        ))
        return
    # 时间邻接：left 的右端点 == right 的左端点（按 timeline 顺序）
    first, second = (left, right) if left.timeline_start <= right.timeline_start else (right, left)
    if first.timeline_end != second.timeline_start:
        diags.append(make_diagnostic(
            DiagnosticSeverity.ERROR, TaepDiagnosticCode.TRANSITION_INVALID,
            op_id=op.op_id, lang=lang,
            detail=(
                f"transition clips must be time-adjacent: {first.clip_id} ends at "
                f"{first.timeline_end} but {second.clip_id} starts at {second.timeline_start}"
            ),
        ))


def _check_effect_params(
    diags: list[Diagnostic],
    op: EditOperation,
    manifest: CapabilityManifest,
    effect_type: str,
    given_params: dict[str, Any],
    lang: str,
) -> None:
    """effect 参数范围校验（min/max/choices）。"""
    cap = get_effect_capability(manifest, effect_type)
    if cap is None:
        return
    # 建立参数 schema 索引
    schema_by_name = {p.get("name"): p for p in cap.parameters if isinstance(p, dict)}
    for pname, pvalue in (given_params or {}).items():
        schema = schema_by_name.get(pname)
        if schema is None:
            continue  # 未知参数由结构校验兜底
        if "min" in schema and isinstance(pvalue, (int, float)) and pvalue < schema["min"]:
            diags.append(make_diagnostic(
                DiagnosticSeverity.ERROR, TaepDiagnosticCode.EFFECT_PARAMETER_INVALID,
                op_id=op.op_id, path=f"/operations/{op.op_id}/params/parameters/{pname}",
                lang=lang, param=pname, value=pvalue,
                min=schema["min"], max=schema.get("max", schema["min"]),
            ))
        elif "max" in schema and isinstance(pvalue, (int, float)) and pvalue > schema["max"]:
            diags.append(make_diagnostic(
                DiagnosticSeverity.ERROR, TaepDiagnosticCode.EFFECT_PARAMETER_INVALID,
                op_id=op.op_id, path=f"/operations/{op.op_id}/params/parameters/{pname}",
                lang=lang, param=pname, value=pvalue,
                min=schema.get("min", schema["max"]), max=schema["max"],
            ))
        elif "choices" in schema and pvalue not in schema["choices"]:
            diags.append(make_diagnostic(
                DiagnosticSeverity.ERROR, TaepDiagnosticCode.EFFECT_PARAMETER_INVALID,
                op_id=op.op_id, path=f"/operations/{op.op_id}/params/parameters/{pname}",
                lang=lang, param=pname, value=pvalue,
                min=0, max=0,
            ))
