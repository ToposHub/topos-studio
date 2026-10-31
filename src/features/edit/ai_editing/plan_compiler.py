"""TAEP Plan Compiler（Phase 2）。

把每个白名单 ``EditOperation`` 编译为现有 ``EditCommand``，再用 ``MacroCommand``
聚合成单个命名 undo group（TAEP §6.1 步骤 3-4, §6.1 步骤 8）。

**关键不变量**：

- compiler **只**产出 command，**不** apply、**不** commit、**不** bump revision。
  apply/commit 由 ``EditProjectService.commit_command`` 在提交边界统一做。
- 缺少领域 command 的 operation（如 set_marker）先补纯数据 command，再开放
  operation（TAEP §5.7）。首版所有白名单 operation 都有对应 command。
- ``simulate`` 在副本上用 MacroCommand.apply 得到 proposed sequence，与正式提交
  复用同一 compiler（保证 preview 与 commit byte-equal，TAEP §12 P2 验收）。
- 任一 operation 无法编译则整 plan 失败（CompileError），不产生半套 command。

参考：docs/ai_editing_protocol_and_implementation_plan_2026-07-31.md §5.7, §6.1
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, TYPE_CHECKING

from src.features.edit.ai_editing.editorial_models import FrameRange
from src.features.edit.ai_editing.models import EditOperation, EditPlan, OperationKind

if TYPE_CHECKING:
    from src.features.edit.core.commands import (
        AddClipCommand,
        ChangeClipFadeCommand,
        SetTrackLockCommand,
        SetTrackMuteCommand,
        SetTrackSoloCommand,
        TrimClipCommand,
        UpdateClipParamCommand,
        UpdateClipSourceCommand,
    )


class CompileError(Exception):
    """单个 operation 无法编译为 command。携带 op_id 便于诊断定位。"""

    def __init__(self, op_id: str, kind: str, reason: str) -> None:
        self.op_id = op_id
        self.kind = kind
        self.reason = reason
        super().__init__(f"Cannot compile operation {op_id!r} ({kind}): {reason}")


# ---------------------------------------------------------------------- #
# 能力真相源（M3.1）
# ---------------------------------------------------------------------- #


#: compiler 当前真正能编译为 command 的 OperationKind 集合。
#:
#: 审查 M3.1 发现：build_manifest 无条件声明全部 28 个 OperationKind，但 compiler
#: 对其中 9 个抛 CompileError（add_subtitle / create_timeline / request_export / …）。
#: 模型据此生成 plan，必然在 compile 阶段失败。本集合是"可声明 = 可编译"的唯一真相：
#: manifest 的 operations 必须从此派生，validator 的 allowlist 也据此判定。
#:
#: 维护规则：新增 compiler 分支时，把对应 OperationKind 加到这里，manifest 自动
#: 暴露给模型；反之未在此集合的 kind 一律对模型隐藏，并在 plan_validator 给出
#: 明确 UNSUPPORTED_OPERATION 诊断（而不是诱导模型后失败）。
COMPILABLE_OPERATIONS: frozenset[OperationKind] = frozenset({
    OperationKind.ADD_CLIP,
    OperationKind.DELETE_CLIP,
    OperationKind.MOVE_CLIP,
    OperationKind.TRIM_CLIP,
    OperationKind.SPLIT_CLIP,
    OperationKind.REPLACE_CLIP,
    OperationKind.ADD_TRANSITION,
    OperationKind.SET_CLIP_FADE,
    OperationKind.SET_TRANSFORM,
    OperationKind.SET_CROP,
    OperationKind.SET_OPACITY,
    OperationKind.SET_SPEED,
    OperationKind.SET_EFFECT,
    OperationKind.REMOVE_EFFECT,
    OperationKind.ENSURE_TRACK,
    OperationKind.RENAME_TRACK,
    OperationKind.SET_TRACK_STATE,
    OperationKind.SET_MARKER,
    OperationKind.ENSURE_SUBTITLE_TRACK,
    OperationKind.UPDATE_SUBTITLE_TRACK,
    OperationKind.ADD_SUBTITLE,
    OperationKind.UPDATE_SUBTITLE,
    OperationKind.DELETE_SUBTITLE,
    OperationKind.PROMOTE_SUBTITLE_TO_GRAPHIC,
    OperationKind.APPLY_CAPTION_GRAPHIC_TEMPLATE,
    OperationKind.UPDATE_CAPTION_GRAPHIC_PARAMETERS,
    OperationKind.DETACH_CAPTION_GRAPHIC,
    OperationKind.DEMOTE_GRAPHIC_TO_SUBTITLE,
})


def is_compilable(kind: OperationKind) -> bool:
    """某 OperationKind 当前是否可编译（manifest 据此暴露给模型）。"""
    return kind in COMPILABLE_OPERATIONS


@dataclass(frozen=True)
class CompiledPlan:
    """编译结果：一个 MacroCommand + 元信息。

    ``macro`` 的 ``label`` 来自 plan.title（或默认），作为单个 undo group 名。
    ``decision_to_clip`` 提供 decision → clip/source range 的可追溯映射（TAEP §6.1
    步骤 8 / P2 TimelineDiff）。
    """

    macro: Any  # MacroCommand
    plan_id: str
    undo_label: str
    operation_count: int
    # op_id -> 编译摘要（用于 diff / 审计），不含 command 对象本身
    operation_summaries: dict[str, dict[str, Any]]

    @property
    def label(self) -> str:
        return self.undo_label


def compile_plan(plan: EditPlan) -> CompiledPlan:
    """把整个 EditPlan 编译为单个 MacroCommand。

    任一 operation 编译失败抛 ``CompileError``（调用方转为 Diagnostic）。
    不读取 canonical domain —— 编译只依赖 operation params 与 clip 模型构造。
    """
    from src.features.edit.core.commands import MacroCommand

    commands: list[Any] = []
    summaries: dict[str, dict[str, Any]] = {}

    for op in plan.operations:
        cmd, summary = _compile_operation(op, plan_id=plan.plan_id)
        commands.append(cmd)
        summaries[op.op_id] = summary

    label = plan.title or f"TAEP plan {plan.plan_id[:8]}"
    macro = MacroCommand.create(label, commands)
    return CompiledPlan(
        macro=macro,
        plan_id=plan.plan_id,
        undo_label=label,
        operation_count=len(commands),
        operation_summaries=summaries,
    )


def _compile_operation(op: EditOperation, *, plan_id: str = "") -> tuple[Any, dict[str, Any]]:
    """单个 operation → (EditCommand, summary)。

    ``plan_id`` 参与 add_clip 的确定性 clip_id 推导（见 ``_compile_add_clip``），
    使同一份 plan 的多次 compile 产生相同的 clip_id —— 这是 ``diff_hash`` 绑定
    （总体不变量 §1.4：预览即提交）的前提。
    """
    kind = op.kind
    p = op.params
    summary: dict[str, Any] = {"kind": kind.value, "intent": dict(op.intent)}

    try:
        if kind == OperationKind.ADD_CLIP:
            cmd = _compile_add_clip(p, op_id=op.op_id, plan_id=plan_id)
            summary.update({"track_id": p.get("track_id"), "asset_id": p.get("asset_id")})
        elif kind == OperationKind.DELETE_CLIP:
            # M11（S02）：可选区间语义——携带 start_frame/end_frame 时
            # 编译为 RangeRippleDeleteCommand（split×2 + ripple 中段，
            # 词级删除），否则整段删除（既有语义不变）。
            if p.get("start_frame") is not None \
                    and p.get("end_frame") is not None:
                from src.features.edit.core.commands import (
                    RangeRippleDeleteCommand,
                )
                cmd = RangeRippleDeleteCommand.create(
                    p["track_id"], p["clip_id"],
                    int(p["start_frame"]), int(p["end_frame"]),
                    label=str(p.get("reason") or "Range Ripple Delete"))
                summary.update({
                    "clip_id": p.get("clip_id"),
                    "start_frame": int(p["start_frame"]),
                    "end_frame": int(p["end_frame"]),
                })
            else:
                from src.features.edit.core.commands import DeleteClipCommand

                cmd = DeleteClipCommand.create(p["track_id"], p["clip_id"])
                summary.update({"clip_id": p.get("clip_id")})
        elif kind == OperationKind.MOVE_CLIP:
            from src.features.edit.core.commands import MoveClipCommand

            tl = _frame_range(p, "timeline")
            cmd = MoveClipCommand.create(p["track_id"], p["clip_id"], tl.start)
            summary.update({"clip_id": p.get("clip_id"), "new_start": tl.start})
        elif kind == OperationKind.TRIM_CLIP:
            cmd = _compile_trim(p)
            summary.update({"clip_id": p.get("clip_id")})
        elif kind == OperationKind.SPLIT_CLIP:
            from src.features.edit.core.commands import SplitClipCommand

            frame = int(p.get("split_frame", p.get("frame", 0)))
            cmd = SplitClipCommand.create(p["track_id"], p["clip_id"], frame)
            summary.update({"clip_id": p.get("clip_id"), "split_frame": frame})
        elif kind == OperationKind.REPLACE_CLIP:
            cmd = _compile_replace_clip(p)
            summary.update({"clip_id": p.get("clip_id")})
        elif kind == OperationKind.ADD_TRANSITION:
            from src.features.edit.core.commands import AddTransitionCommand

            duration = int(p.get("duration", 12))
            cmd = AddTransitionCommand.create(
                track_id=p["track_id"],
                left_clip_id=p["left_clip_id"],
                right_clip_id=p["right_clip_id"],
                duration=duration,
                transition_type=p.get("transition_type", "cross_dissolve"),
                alignment=p.get("alignment", "centered"),
            )
            summary.update({"transition_type": p.get("transition_type"), "duration": duration})
        elif kind == OperationKind.REMOVE_TRANSITION:

            # RemoveTransitionCommand.create 需要 timeline —— compiler 无 timeline，
            # 故要求调用方在 apply 时提供。这里用一个占位 None，transaction_service
            # 在 apply 前会用真实 sequence 替换。首版更稳妥的做法：标记为需要 timeline。
            raise CompileError(
                op.op_id, kind.value,
                "remove_transition requires a live TimelineSequence; use the "
                "transaction_service.apply path which injects the sequence.",
            )
        elif kind == OperationKind.SET_CLIP_FADE:
            cmd = _compile_set_fade(op, p)
            summary.update({"clip_id": p.get("clip_id")})
        elif kind in (OperationKind.SET_TRANSFORM, OperationKind.SET_CROP,
                      OperationKind.SET_OPACITY, OperationKind.SET_SPEED):
            cmd = _compile_set_param(op, p)
            summary.update({"clip_id": p.get("clip_id"), "param": kind.value})
        elif kind == OperationKind.SET_EFFECT:
            cmd = _compile_set_effect(op, p)
            summary.update({"clip_id": p.get("clip_id"), "effect_type": p.get("effect_type")})
        elif kind == OperationKind.REMOVE_EFFECT:
            cmd = _compile_remove_effect(op, p)
            summary.update({"clip_id": p.get("clip_id"), "effect_type": p.get("effect_type")})
        elif kind == OperationKind.ENSURE_TRACK:
            from src.features.edit.core.commands import AddTrackCommand

            track_id = str(p.get("track_id") or "") or None
            cmd = AddTrackCommand.create(
                p["track_type"], p.get("name"), track_id=track_id,
            )
            summary.update({
                "track_id": track_id,
                "track_type": p.get("track_type"),
                "name": p.get("name"),
            })
        elif kind == OperationKind.RENAME_TRACK:
            from src.features.edit.core.commands import RenameTrackCommand

            cmd = RenameTrackCommand.create(p["track_id"], p["name"])
            summary.update({"track_id": p.get("track_id"), "name": p.get("name")})
        elif kind == OperationKind.SET_TRACK_STATE:
            cmd = _compile_set_track_state(p)
            summary.update({"track_id": p.get("track_id")})
        elif kind == OperationKind.SET_MARKER:
            from src.features.edit.core.commands import AddMarkerCommand
            from src.features.edit.core.timeline_v2 import TimelineMarker

            marker = TimelineMarker(
                marker_id=p.get("marker_id", str(uuid.uuid4())),
                frame=int(p["frame"]),
                name=p.get("name", ""),
                color=p.get("color", "#ffcc00"),
                comment=p.get("comment", ""),
            )
            cmd = AddMarkerCommand.create(marker)
            summary.update({"frame": int(p["frame"])})
        elif kind == OperationKind.ENSURE_SUBTITLE_TRACK:
            cmd = _compile_ensure_subtitle_track(p, plan_id=plan_id)
            summary.update({
                "track_id": p.get("track_id") or f"ai-subtitle-{plan_id[:12]}",
                "track_name": p.get("track_name") or p.get("name"),
            })
        elif kind == OperationKind.UPDATE_SUBTITLE_TRACK:
            cmd = _compile_update_subtitle_track(p, op_id=op.op_id)
            summary.update({"track_id": p.get("track_id")})
        elif kind == OperationKind.ADD_SUBTITLE:
            cmd = _compile_add_subtitle(p, op_id=op.op_id, plan_id=plan_id)
            summary.update({
                "track_id": p.get("track_id"),
                "timeline_start": _subtitle_start(p),
                "duration": _subtitle_duration(p),
            })
        elif kind == OperationKind.UPDATE_SUBTITLE:
            cmd = _compile_update_subtitle(p, op_id=op.op_id)
            summary.update({"clip_id": p.get("clip_id")})
        elif kind == OperationKind.DELETE_SUBTITLE:
            from src.features.edit.core.commands import DeleteSubtitleClipCommand

            cmd = DeleteSubtitleClipCommand.create(str(p["clip_id"]))
            summary.update({"clip_id": p.get("clip_id")})
        elif kind == OperationKind.PROMOTE_SUBTITLE_TO_GRAPHIC:
            from src.features.edit.core.captions.commands import PromoteSubtitleToGraphicCommand
            cmd = PromoteSubtitleToGraphicCommand.create(
                str(p["clip_id"]),
                template_id=str(p.get("template_id") or "fade"),
                template_version=str(p.get("template_version") or "1"),
                composite_id=p.get("composite_id"),
                sync_text=bool(p.get("sync_text", True)),
                sync_timing=bool(p.get("sync_timing", True)),
                sync_style=bool(p.get("sync_style", False)),
            )
            summary.update({"clip_id": p.get("clip_id"), "template_id": p.get("template_id", "fade")})
        elif kind == OperationKind.APPLY_CAPTION_GRAPHIC_TEMPLATE:
            from src.features.edit.core.captions.commands import (
                ApplyCaptionTrackTemplateBatchCommand,
                ApplyCaptionTrackTemplateCommand,
            )
            template_id = str(p.get("template_id") or "") or None
            parameters = p.get("parameters") if isinstance(p.get("parameters"), dict) else None
            track_ids = p.get("track_ids")
            if isinstance(track_ids, list) and track_ids:
                cmd = ApplyCaptionTrackTemplateBatchCommand.create(
                    [str(track_id) for track_id in track_ids], template_id,
                    parameters=parameters,
                )
            else:
                cmd = ApplyCaptionTrackTemplateCommand.create(
                    str(p["track_id"]), template_id, parameters=parameters,
                )
            summary.update({"track_id": p.get("track_id"), "track_ids": track_ids,
                           "template_id": p.get("template_id")})
        elif kind == OperationKind.UPDATE_CAPTION_GRAPHIC_PARAMETERS:
            from src.features.edit.core.captions.commands import UpdateCaptionGraphicBindingCommand
            cmd = UpdateCaptionGraphicBindingCommand.create(
                str(p["clip_id"]),
                p.get("parameters") if isinstance(p.get("parameters"), dict) else {},
                sync_text=p.get("sync_text"),
                sync_timing=p.get("sync_timing"),
                sync_style=p.get("sync_style"),
            )
            summary.update({"clip_id": p.get("clip_id")})
        elif kind == OperationKind.DETACH_CAPTION_GRAPHIC:
            from src.features.edit.core.captions.commands import DetachCaptionGraphicCommand
            cmd = DetachCaptionGraphicCommand.create(str(p["clip_id"]))
            summary.update({"clip_id": p.get("clip_id")})
        elif kind == OperationKind.DEMOTE_GRAPHIC_TO_SUBTITLE:
            from src.features.edit.core.captions.commands import DemoteGraphicToSubtitleCommand
            cmd = DemoteGraphicToSubtitleCommand.create(str(p["clip_id"]))
            summary.update({"clip_id": p.get("clip_id")})
        elif kind in (OperationKind.CREATE_TIMELINE, OperationKind.SET_RANGE,
                      OperationKind.ADD_REVIEW_NOTE, OperationKind.REQUEST_EXPORT,
                      OperationKind.REMOVE_TRANSITION):
            # 这些 operation 首版需要领域上下文或专用 command，标记为 pending。
            raise CompileError(
                op.op_id, kind.value,
                f"operation {kind.value} requires domain context or a dedicated "
                f"command not yet wired in the compiler (track via plan_validator).",
            )
        else:
            raise CompileError(op.op_id, kind.value, "unknown operation kind")
    except KeyError as exc:
        raise CompileError(op.op_id, kind.value, f"missing required param {exc}") from exc
    except (ValueError, TypeError) as exc:
        # AUD-14101：params 来自 LLM 生成的外部数据，int()/字符串索引等
        # 强转产生的 ValueError/TypeError（duration="abc"、state="mute"、
        # start=None 等）同样收敛为 CompileError——compile 失败 → 诊断的
        # 错误协议不再被未分类异常穿透（preview/commit 边界只捕 CompileError）。
        raise CompileError(
            op.op_id, kind.value,
            f"invalid param value/type ({type(exc).__name__}): {exc}",
        ) from exc

    return cmd, summary


def _compile_ensure_subtitle_track(
    params: dict[str, Any], *, plan_id: str,
) -> Any:
    """Compile an idempotent subtitle-track ensure operation.

    A deterministic track ID is required because ``preview`` and ``commit``
    compile the same plan independently. The command itself is idempotent when
    that track already exists, so reapplying a plan cannot delete a user track.
    """
    from src.features.edit.core.commands import AddTrackCommand

    track_id = str(params.get("track_id") or f"ai-subtitle-{plan_id[:12]}")
    track_name = str(params.get("track_name") or params.get("name") or "AI 字幕")
    position = params.get("position")
    return AddTrackCommand.create(
        "subtitle", track_name,
        int(position) if position is not None else None,
        track_id=track_id,
    )


def _subtitle_start(params: dict[str, Any]) -> int:
    raw = params.get("timeline")
    if isinstance(raw, dict) and "start" in raw:
        return int(raw["start"])
    return int(params.get("start_frame", 0))


def _subtitle_duration(params: dict[str, Any]) -> int:
    raw = params.get("timeline")
    if isinstance(raw, dict) and "duration" in raw:
        return int(raw["duration"])
    return int(params.get("duration_frames", 0))


def _compile_add_subtitle(
    params: dict[str, Any], *, op_id: str, plan_id: str,
) -> Any:
    """Compile a deterministic subtitle cue insertion command."""
    from src.features.edit.core.commands import AddSubtitleClipCommand, CaptionEditMode
    from src.features.edit.core.captions.model import CaptionWordTiming

    track_id = str(params.get("track_id") or "")
    if not track_id:
        raise CompileError(op_id, OperationKind.ADD_SUBTITLE.value,
                           "add_subtitle requires a resolved track_id")
    start = _subtitle_start(params)
    duration = _subtitle_duration(params)
    if duration <= 0:
        raise CompileError(op_id, OperationKind.ADD_SUBTITLE.value,
                           "subtitle duration must be positive")
    mode = str(params.get("mode", CaptionEditMode.OVERWRITE))
    clip_id = str(params.get("clip_id") or f"{plan_id}:subtitle:{op_id}")
    return AddSubtitleClipCommand.create(
        track_id, start, duration, str(params.get("text", "")),
        mode=mode, clip_id=clip_id,
        word_timings=tuple(
            CaptionWordTiming.from_dict(item)
            for item in (params.get("word_timings") or ())
            if isinstance(item, dict)
        ),
    )


def _compile_update_subtitle(params: dict[str, Any], *, op_id: str) -> Any:
    """Compile a constrained subtitle cue patch into the domain command."""
    from src.features.edit.core.captions.model import (
        CaptionCueMetadata, CaptionStyle, STYLE_FIELDS,
    )
    from src.features.edit.core.commands import UpdateSubtitleClipCommand

    clip_id = str(params.get("clip_id") or "")
    if not clip_id:
        raise CompileError(op_id, OperationKind.UPDATE_SUBTITLE.value,
                           "update_subtitle requires clip_id")
    raw_updates = params.get("updates")
    updates: dict[str, Any] = dict(raw_updates) if isinstance(raw_updates, dict) else {}
    if "text" in params:
        updates["text"] = str(params["text"])
    timeline = params.get("timeline")
    if isinstance(timeline, dict):
        if "start" in timeline:
            updates["timeline_start"] = int(timeline["start"])
        if "duration" in timeline:
            updates["timeline_duration"] = int(timeline["duration"])
    if "style" in params:
        style = params["style"]
        if not isinstance(style, dict):
            raise CompileError(op_id, OperationKind.UPDATE_SUBTITLE.value,
                               "style must be an object")
        updates["style_override"] = CaptionStyle.from_dict(style)
    if "style_override" in params:
        raw_style = params["style_override"]
        updates["style_override"] = (
            CaptionStyle.from_dict(raw_style) if isinstance(raw_style, dict)
            else None
        )
    if "caption_metadata" in params:
        raw_metadata = params["caption_metadata"]
        if not isinstance(raw_metadata, dict):
            raise CompileError(op_id, OperationKind.UPDATE_SUBTITLE.value,
                               "caption_metadata must be an object")
        updates["caption_metadata"] = CaptionCueMetadata.from_dict(raw_metadata)
    allowed = frozenset({
        "text", "timeline_start", "timeline_duration", "is_enabled",
        "label_color", "style_override", "caption_metadata",
        *STYLE_FIELDS,
    })
    unknown = sorted(set(updates) - allowed)
    if unknown:
        raise CompileError(
            op_id, OperationKind.UPDATE_SUBTITLE.value,
            f"unsupported subtitle update fields: {unknown}",
        )
    if not updates:
        raise CompileError(op_id, OperationKind.UPDATE_SUBTITLE.value,
                           "update_subtitle requires at least one update")
    return UpdateSubtitleClipCommand.create(
        clip_id, updates, label="AI Update Subtitle",
    )


def _compile_update_subtitle_track(params: dict[str, Any], *, op_id: str) -> Any:
    """Compile global subtitle style and metadata edits."""
    from src.features.edit.core.captions.commands import (
        UpdateSubtitleTrackMetadataCommand,
        UpdateSubtitleTrackStylePatchCommand,
    )
    from src.features.edit.core.captions.model import CaptionStyle
    from src.features.edit.core.commands import MacroCommand

    track_id = str(params.get("track_id") or "")
    if not track_id:
        raise CompileError(op_id, OperationKind.UPDATE_SUBTITLE_TRACK.value,
                           "update_subtitle_track requires track_id")
    commands: list[Any] = []
    style = params.get("style")
    if style is not None:
        if not isinstance(style, dict):
            raise CompileError(op_id, OperationKind.UPDATE_SUBTITLE_TRACK.value,
                               "style must be an object")
        commands.append(UpdateSubtitleTrackStylePatchCommand.create(
            track_id, CaptionStyle.from_dict(style),
        ))
    metadata = params.get("caption_metadata", params.get("metadata"))
    if metadata is not None:
        if not isinstance(metadata, dict):
            raise CompileError(op_id, OperationKind.UPDATE_SUBTITLE_TRACK.value,
                               "metadata must be an object")
        allowed = {"language", "role", "safe_anchor", "label"}
        unknown = sorted(set(metadata) - allowed)
        if unknown:
            raise CompileError(
                op_id, OperationKind.UPDATE_SUBTITLE_TRACK.value,
                f"unsupported subtitle track metadata fields: {unknown}",
            )
        commands.append(UpdateSubtitleTrackMetadataCommand.create(track_id, metadata))
    if not commands:
        raise CompileError(op_id, OperationKind.UPDATE_SUBTITLE_TRACK.value,
                           "update_subtitle_track requires style or metadata")
    return commands[0] if len(commands) == 1 else MacroCommand.create(
        "AI Update Subtitle Track", commands,
    )


# ---------------------------------------------------------------------- #
# 各 operation 的编译器
# ---------------------------------------------------------------------- #


def _frame_range(params: dict[str, Any], key: str) -> FrameRange:
    """从 params[key] 解析 FrameRange（容忍 dict/FrameRange）。"""
    raw = params.get(key)
    if isinstance(raw, FrameRange):
        return raw
    if isinstance(raw, dict):
        return FrameRange(start=int(raw["start"]), duration=int(raw["duration"]))
    raise KeyError(key)


def _compile_add_clip(p: dict[str, Any], *, op_id: str = "", plan_id: str = "") -> AddClipCommand:
    """add_clip → AddClipCommand.create(track_id, ClipInstance)。

    当 operation 未显式提供 ``clip_id`` 时，从 ``plan_id + op_id`` 派生一个
    **确定性** clip_id（而非 ``uuid.uuid4()``）。这样同一份 plan 的多次 compile
    （preview 与 commit）产生相同的 clip_id → 相同的 diff → 相同的 ``diff_hash``
    （总体不变量 §1.4：预览即提交——用户看到的那一份 diff 必须与提交的一致）。

    显式传入 ``clip_id`` 的 operation 不受影响（仍使用调用方提供的 id）。
    """
    from src.features.edit.core.clip_params import CropParams, SpeedParams, TransformParams
    from src.features.edit.core.commands import AddClipCommand
    from src.features.edit.core.timeline_v2 import ClipInstance

    source = _frame_range(p, "source")
    timeline = _frame_range(p, "timeline")
    clip_id = p.get("clip_id")
    if not clip_id:
        clip_id = _derive_clip_id(plan_id, op_id)
    clip = ClipInstance(
        clip_id=clip_id,
        asset_id=p["asset_id"],
        track_id=p["track_id"],
        timeline_start=timeline.start,
        timeline_duration=timeline.duration,
        source_start=source.start,
        source_duration=source.duration,
        speed=SpeedParams(),
        transform=TransformParams(),
        crop=CropParams(),
    )
    return AddClipCommand.create(p["track_id"], clip)


def _derive_clip_id(plan_id: str, op_id: str) -> str:
    """从 plan_id + op_id 派生确定性 clip_id（同一 plan 同一 op 总产生同一 id）。

    用 sha256 取前 16 hex 作为后缀，前缀 ``taep-`` 标明这是 AI plan 生成的 clip，
    便于审计与与用户手工创建的 clip 区分。
    """
    import hashlib

    raw = f"{plan_id}|{op_id}".encode("utf-8")
    digest = hashlib.sha256(raw).hexdigest()[:16]
    return f"taep-{digest}"


def _compile_trim(p: dict[str, Any]) -> TrimClipCommand:
    """trim_clip → TrimClipCommand。

    params 支持：
    - side: "left"/"right"（默认 right）
    - new_start / new_duration（timeline）；若提供 source 也一并传。
    """
    from src.features.edit.core.commands import TrimClipCommand

    side = p.get("side", p.get("trim_side", "right"))
    return TrimClipCommand(
        command_id=str(uuid.uuid4()),
        label=f"Trim {p['clip_id']}",
        track_id=p["track_id"],
        clip_id=p["clip_id"],
        trim_side=side,
        new_timeline_start=p.get("new_start"),
        new_timeline_duration=p.get("new_duration"),
    )


def _compile_replace_clip(p: dict[str, Any]) -> UpdateClipSourceCommand:
    """replace_clip → UpdateClipSourceCommand。"""
    from src.features.edit.core.commands import UpdateClipSourceCommand

    source = _frame_range(p, "source")
    return UpdateClipSourceCommand.create(
        track_id=p["track_id"],
        clip_id=p["clip_id"],
        new_asset_id=p["asset_id"],
        new_source_start=source.start,
    )


def _compile_set_fade(op: EditOperation, p: dict[str, Any]) -> ChangeClipFadeCommand:
    """set_clip_fade → ChangeClipFadeCommand（T-P2-20/W01 回放修复）。

    旧实现发 UpdateClipParamCommand(param_type="fade_in_duration")，
    而 UpdateClipParamCommand 不支持该 param_type——模拟/执行层一律
    "Unknown param_type" 拒绝，可编译却永不可应用的契约断裂。
    ChangeClipFadeCommand 是 UI 淡入手柄同款命令（apply 返回
    (timeline, updated_cmd)，MacroCommand 已处理该形状）。

    AUD-14100：缺 fade 值时抛 ``CompileError``（携带 op_id/kind，可被
    ``TransactionService.preview`` 的 ``except CompileError`` 边界捕获并
    转为 op 级诊断），不再抛裸 ``ValueError`` 逃逸。schema 层
    （``validate_plan_structure``）已同步要求 fade 字段存在性，正常
    流程在 validate 阶段即产出可修复诊断。
    """
    from src.features.edit.core.commands import ChangeClipFadeCommand

    fade_in = p.get("fade_in_duration")
    fade_out = p.get("fade_out_duration")
    if fade_in is not None:
        fade_type, new_in, new_out = "in", int(fade_in), 0
    elif fade_out is not None:
        fade_type, new_in, new_out = "out", 0, int(fade_out)
    else:
        raise CompileError(
            op.op_id, OperationKind.SET_CLIP_FADE.value,
            "set_clip_fade requires fade_in_duration or fade_out_duration",
        )
    return ChangeClipFadeCommand(
        command_id=str(uuid.uuid4()),
        label=f"Set fade {fade_type}={new_in if fade_type == 'in' else new_out}",
        track_id=str(p.get("track_id", "")),
        clip_id=str(p["clip_id"]),
        fade_type=fade_type,
        new_fade_in=new_in,
        new_fade_out=new_out,
    )


def _coerce_clip_param_value(param_type: str, value: Any, op: EditOperation) -> Any:
    """把 AI plan 的 JSON 值转换为 UpdateClipParamCommand 期望的类型。"""
    if param_type == "opacity":
        try:
            return float(value)
        except (TypeError, ValueError):
            raise CompileError(
                op.op_id, op.kind.value,
                f"opacity value must be a number, got {value!r}")
    if param_type in ("transform", "crop", "speed"):
        from src.features.edit.core.clip_params import (
            CropParams, SpeedParams, TransformParams,
        )

        cls = {"transform": TransformParams, "crop": CropParams,
               "speed": SpeedParams}[param_type]
        if isinstance(value, cls):
            return value
        if isinstance(value, dict):
            try:
                return cls.from_dict(dict(value))
            except Exception as exc:
                raise CompileError(
                    op.op_id, op.kind.value,
                    f"invalid {param_type} payload: {exc}")
        if param_type == "speed" and isinstance(value, (int, float)):
            # 便捷形态：SET_SPEED 直接给数值 → SpeedParams(speed=n)
            return SpeedParams(speed=float(value))
        raise CompileError(
            op.op_id, op.kind.value,
            f"{param_type} value must be a dict matching {cls.__name__}, "
            f"got {type(value).__name__}")
    return value


def _compile_set_param(op: EditOperation, p: dict[str, Any]) -> UpdateClipParamCommand:
    """set_transform/crop/opacity/speed → UpdateClipParamCommand。

    这些 operation 的 params 里直接是要设置的 param_type -> value 映射。
    需要从 sequence 查 track_id（compiler 无 sequence），故要求 params 带 track_id
    或在 transaction_service 注入。首版要求 params 带 clip_id + 至少一个值字段。
    """
    from src.features.edit.core.commands import UpdateClipParamCommand

    # 把 OperationKind 映射到默认 param_type；也接受 params 里显式 param/value。
    default_param = {
        OperationKind.SET_OPACITY: "opacity",
        OperationKind.SET_SPEED: "speed",
        # T-P1-22：此前缺失——param_type 回落到 kind.value（"set_transform"/
        # "set_crop"），命令层 apply 只认 "transform"/"crop"，plan 编译通过
        # 但应用必炸。
        OperationKind.SET_TRANSFORM: "transform",
        OperationKind.SET_CROP: "crop",
    }.get(op.kind)
    param_type = p.get("param", p.get("param_type", default_param or op.kind.value))
    value = p.get("value", p.get(param_type))
    if value is None:
        raise CompileError(op.op_id, op.kind.value, f"no value provided for {param_type}")
    # T-P1-22：JSON dict → clip 参数 dataclass（命令 apply 要求实例类型）。
    # 结构非法在 compile 期显式失败，不拖到 transaction apply。
    value = _coerce_clip_param_value(param_type, value, op)
    return UpdateClipParamCommand(
        command_id=str(uuid.uuid4()),
        label=f"Set {param_type}",
        track_id=p.get("track_id", ""),
        clip_id=p["clip_id"],
        param_type=param_type,
        new_value=value,
    )


def _compile_set_effect(op: EditOperation, p: dict[str, Any]) -> UpdateClipParamCommand:
    """set_effect → UpdateClipParamCommand(effects)。

    首版把 effect 作为 effects 列表追加。完整 effect instance 构造需要 registry，
    P2 先支持“设置/追加一个 effect_type + parameters”的最简形式；复杂 effect
    graph 编辑在 P9。
    """
    from src.features.edit.core.commands import UpdateClipParamCommand

    effect_type = p["effect_type"]
    parameters = p.get("parameters", {})
    # 以 {effect_type: parameters} 形式存入 effects（与 clip_params 对齐由 renderer 解释）
    return UpdateClipParamCommand(
        command_id=str(uuid.uuid4()),
        label=f"Set effect {effect_type}",
        track_id=p.get("track_id", ""),
        clip_id=p["clip_id"],
        param_type="effect",
        new_value={"effect_type": effect_type, "parameters": dict(parameters)},
    )


def _compile_remove_effect(op: EditOperation, p: dict[str, Any]) -> UpdateClipParamCommand:
    """remove_effect → UpdateClipParamCommand（标记移除）。"""
    from src.features.edit.core.commands import UpdateClipParamCommand

    return UpdateClipParamCommand(
        command_id=str(uuid.uuid4()),
        label=f"Remove effect {p.get('effect_type')}",
        track_id=p.get("track_id", ""),
        clip_id=p["clip_id"],
        param_type="remove_effect",
        new_value=p["effect_type"],
    )


def _compile_set_track_state(p: dict[str, Any]) -> SetTrackMuteCommand | SetTrackLockCommand | SetTrackSoloCommand:
    """set_track_state → 对应的 SetTrack*Command。"""
    state = p.get("state", {})
    track_id = p["track_id"]

    if "mute" in state:
        from src.features.edit.core.commands import SetTrackMuteCommand

        return SetTrackMuteCommand.create(track_id, bool(state["mute"]))
    if "lock" in state:
        from src.features.edit.core.commands import SetTrackLockCommand

        return SetTrackLockCommand.create(track_id, bool(state["lock"]))
    if "solo" in state:
        from src.features.edit.core.commands import SetTrackSoloCommand

        return SetTrackSoloCommand.create(track_id, bool(state["solo"]))
    raise CompileError(
        p.get("track_id", "?"), "set_track_state",
        "set_track_state requires a 'state' dict with mute/lock/solo/visibility",
    )
