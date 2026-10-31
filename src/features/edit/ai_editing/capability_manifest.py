"""CapabilityManifest 构建器（Phase 1）。

**必须从实际 registry 与 feature flags 生成**，不能手工复制一份效果表
（TAEP §5.1）。这样未实现的参数不会被模型调用。

数据源：

- ``EffectRegistry.get_all_definitions()`` → effects（params 从 ``params_schema`` 提取）
- ``TransitionInstance`` 的 transition_type 白名单（来自统一 transition catalogue）
- ``ExportCodec`` 枚举 → export.codecs
- ``Rational`` 接受的 fps 列表（timebase）
- ``OperationKind`` 白名单 → operations
- 默认 limits（可被 feature flag 覆盖）
"""

from __future__ import annotations

import logging
from typing import Any

from src.features.edit.ai_editing.models import (
    TAEP_PROTOCOL_VERSION,
    CapabilityManifest,
    EffectCapability,
)
from src.features.edit.core.transition_catalog import TRANSITION_TYPE_OPTIONS

logger = logging.getLogger(__name__)

#: manifest schema 版本（toposctl `_version_info` 的真相源；此前 CLI 侧引用了
#: 不存在的名字靠 except 兜底——T-P0-00 收口为单一常量）。
CAPABILITY_MANIFEST_VERSION = "2"


# 接受的 fps（Rational 字符串形式）。与计划文档 §5.1 一致。
_ACCEPTED_FPS: tuple[str, ...] = (
    "24/1",
    "24000/1001",
    "25/1",
    "30000/1001",
    "30/1",
    "60000/1001",
    "60/1",
)

# 首版支持的 transition（与 TransitionInstance.__post_init__ 白名单一致）。
_SUPPORTED_TRANSITIONS: tuple[str, ...] = TRANSITION_TYPE_OPTIONS


def _effect_target_from_category(category: str) -> str:
    """从 effect category 推断 target（video_clip / audio_clip）。

    registry 的 ``audio`` category → audio_clip；其余（transform/filter/stylize/
    generator/plugin）→ video_clip。
    """
    if category == "audio":
        return "audio_clip"
    return "video_clip"


def _params_schema_to_capabilities(params_schema: dict[str, Any]) -> tuple[dict[str, Any], ...]:
    """把 registry 的 ``params_schema``（dict[param_name, ParamSchema]）转为
    manifest 的参数描述列表。

    ParamSchema 字段：type/default/min_value/max_value/step/label/choices/unit。
    """
    out: list[dict[str, Any]] = []
    for name, schema in (params_schema or {}).items():
        # ParamSchema 是 dataclass；用 getattr 兼容 dict 与对象。
        g = lambda attr, default=None: getattr(schema, attr, default) if not isinstance(schema, dict) else schema.get(attr, default)
        entry: dict[str, Any] = {
            "name": name,
            "type": g("type", "float"),
            "default": g("default"),
        }
        # min/max（命名空间里是 min_value/max_value）
        mn = g("min_value")
        mx = g("max_value")
        if mn is not None:
            entry["min"] = mn
        if mx is not None:
            entry["max"] = mx
        step = g("step")
        if step is not None:
            entry["step"] = step
        choices = g("choices")
        if choices:
            entry["choices"] = list(choices)
        label = g("label")
        if label:
            entry["label"] = label
        unit = g("unit")
        if unit:
            entry["unit"] = unit
        out.append(entry)
    return tuple(out)


def _build_timeline_capability() -> dict[str, Any]:
    """Assemble the AE-style timeline capability block (T62-T65).

    Every value here is pulled from the modules the UI actually uses, so
    the manifest is the single source of truth an AI/script client reads
    to discover:

    * the command vocabulary + stable error codes (timeline_commands.py);
    * the keyboard shortcut commands (timeline_shortcuts.py);
    * the row/selection model + row_id scheme (timeline_view_model.py);
    * the four-state expression status + stable expression error codes
      (timeline_expression.py).

    A test (tests/edit/ai_editing/test_timeline_capabilities.py) asserts
    the block matches those modules exactly, so adding a command/error/
    shortcut anywhere else without updating it here fails CI.
    """
    from src.features.composite.ui import (
        timeline_commands as tc,
        timeline_expression as te,
        timeline_shortcuts as ts,
        timeline_view_model as tvm,
    )

    # Stable command error codes (T15/T63). Only codes starting with ERR_
    # are part of the public contract; internal helpers stay private.
    error_codes = sorted(
        name for name in dir(tc)
        if name.startswith("ERR_") and getattr(tc, name)
    )
    # Stable expression error codes (T53).
    expression_error_codes = sorted(
        name for name in dir(te)
        if name.startswith("EXPR_ERR_") and getattr(te, name)
    )
    # Keyboard commands the dispatcher can fire (T62 prep / T062).
    shortcut_commands = sorted(ts.DEFAULT_AE_BINDINGS.values())
    # Row types the timeline paints (T04).
    row_types = [tvm.ROW_NODE, tvm.ROW_GROUP, tvm.ROW_PARAM,
                 tvm.ROW_CHANNEL, tvm.ROW_EXPRESSION]
    # Four expression states the UI shows (T49).
    expression_states = [te.EXPRESSION_NONE, te.EXPRESSION_VALID,
                         te.EXPRESSION_ERROR, te.EXPRESSION_DISABLED]

    return {
        # T63 modification commands: each maps 1:1 to a TimelineCommandBuilder
        # method. AI/script build a CompositeEditPlan with these op names OR
        # call the timeline API; both land at the same validator+undo path.
        "commands": [
            "add_keyframe", "remove_keyframe", "clear_track",
            "move_keyframes", "copy_keyframes", "paste_keyframes",
            "set_interpolation", "set_tangent_mode", "set_bezier_handles",
            "set_extrapolation", "set_expression", "remove_expression",
            "set_node_range", "set_track_lock",
        ],
        # Stable strings commands return on failure (T15). AI can branch on
        # these instead of parsing free-form messages.
        "error_codes": error_codes,
        # T62 query interface: the read-only surface AI uses to inspect the
        # timeline before editing. Each entry is a method on the timeline
        # API/viewmodel; the host exposes them via the same composite bridge.
        "queries": [
            "list_nodes", "list_properties", "get_track",
            "get_selection", "get_expression_status", "query_at_frame",
            "list_animatable_params",
        ],
        # Row types + the stable row_id grammar (T04).
        "row_types": row_types,
        "row_id_grammar": {
            tvm.ROW_NODE: "node:{node_id}",
            tvm.ROW_GROUP: "group:{node_id}:{group_name}",
            tvm.ROW_PARAM: "param:{node_id}:{param_name}",
            tvm.ROW_CHANNEL: "channel:{node_id}:{param_name}:{channel}",
            tvm.ROW_EXPRESSION: "expr:{node_id}:{param_name}",
        },
        # Selection model: cross-track keyframe + row selection, primary
        # anchor for J/K navigation, coalesced batch notifications.
        "selection": {
            "shared_across_panels": True,
            "supports_cross_track_keyframes": True,
            "supports_primary_anchor": True,
        },
        # Keyboard command vocabulary (T062). AI can map a user intent
        # ("go to next keyframe") to the exact command the panel would fire.
        "shortcut_commands": shortcut_commands,
        # Expression model: four-state status + stable error codes (T49/T53).
        "expression_states": expression_states,
        "expression_error_codes": expression_error_codes,
        "expression_precedence": [
            "keyframe_or_static_value",
            "expression_result_if_valid",
            "fallback_to_keyframe_or_static_on_error",
        ],
        # Undo discipline: one drag/batch = one undo unit (T17/T64).
        "undo": {
            "one_unit_per_drag": True,
            "one_unit_per_paste": True,
            "esc_cancels_drag": True,
            "batch_transaction_supported": True,
        },
        # Lock is enforced at the service layer — AI/script cannot bypass it
        # any more than the mouse can (T65).
        "lock_enforcement": "service_layer",
    }


def build_manifest(
    *,
    effect_registry: Any | None = None,
    feature_flags: Any | None = None,
    project_revision: str = "",
    extra_operations: tuple[str, ...] = (),
    profile: Any | None = None,
    credential_available: bool = False,
    generation_providers: tuple[Any, ...] = (),
    analysis_provider_ids: tuple[str, ...] = (),
    runtime_snapshot: Any | None = None,
) -> CapabilityManifest:
    """从实际 registry / feature flags 构建 CapabilityManifest。

    Args:
        effect_registry: ``EffectRegistry`` 实例。None 时延迟导入默认实例。
        feature_flags: 可选的 feature flags 对象（用于按 flag 启用/禁用 operation）。
            首版未消费具体 flag，预留接口。
        project_revision: 当前工程 revision 标识（用于让模型判断 manifest 新鲜度）。
        extra_operations: 额外允许的 operation（如实验性 flag 启用的）。
        profile / credential_available / generation_providers: P0 能力状态输入；
            不触发网络请求。
        runtime_snapshot: T-P0-00 ``RuntimeCapabilitySnapshot``（可选）。注入时
            manifest 嵌入 ``runtime`` 块；且当 ``codec.topos`` 非 AVAILABLE 时，
            "topos" 从可导出 codecs 中移除并记录原因——无能力的交付目标不得
            被 AI 计划器选为可执行操作（fail-closed）。
    """
    # --- effects ---
    effects: list[EffectCapability] = []
    if effect_registry is None:
        from src.features.edit.core.effect_registry import EffectRegistry

        effect_registry = EffectRegistry()
    try:
        definitions = effect_registry.get_all_definitions()
    except Exception:  # noqa: BLE001 — registry 读取失败按空效果面降级（AUD-13114）
        # fail-closed 方向正确，但降级必须留痕：静默失去整个效果能力面会让
        # AI/脚本客户端以为"该工程不支持任何效果"且无法诊断。
        logger.warning(
            "build_manifest: 效果注册表读取失败，manifest 效果能力面降级为空",
            exc_info=True,
        )
        definitions = []
    for definition in definitions:
        et = getattr(definition, "effect_type", None)
        if not et:
            continue
        target = _effect_target_from_category(getattr(definition, "category", ""))
        params = _params_schema_to_capabilities(getattr(definition, "params_schema", {}) or {})
        # T-P1-20：导出语义来自统一 EffectExecutionCapability（与 Deliver
        # preflight / UI 同一真相源）。音频类效果走 CPU DSP 导出链，视为
        # 可交付；视频效果按能力解析；未知类型按 unsupported 处理。
        exportable = True
        preview_only = False
        unsupported = False
        restriction_code = ""
        restriction_message = ""
        if target == "video_clip" and not et.startswith("plugin:"):
            try:
                from src.features.shared.effects import (
                    get_effect_catalog, resolve_execution_capability,
                )

                execution = resolve_execution_capability(
                    et, catalog_known=get_effect_catalog().is_known(et))
                exportable = execution.exportable
                preview_only = execution.preview_only
                unsupported = execution.unsupported
                restriction_code, restriction_message = \
                    execution.delivery_restriction()
            except Exception:  # noqa: BLE001 — 能力求证失败按不可交付处理
                exportable = False
                unsupported = True
                restriction_code = "capability_resolution_unavailable"
                restriction_message = "效果能力解析失败，按不可交付处理"
        elif target == "video_clip":
            # plugin:* 经同一 resolve_execution_capability 解析（T-P2-20/W06）：
            # 头部声明 @export_support opengl 的插件可交付（GL 导出链），
            # 未声明的维持 preview_only——拒绝原因与预检/UI 逐字一致。
            try:
                from src.features.shared.effects import (
                    get_effect_catalog, resolve_execution_capability,
                )
                execution = resolve_execution_capability(
                    et, catalog_known=get_effect_catalog().is_known(et))
                exportable = execution.exportable
                preview_only = execution.preview_only
                unsupported = execution.unsupported
                restriction_code, restriction_message = \
                    execution.delivery_restriction()
            except Exception:  # noqa: BLE001 — 能力求证失败按不可交付处理
                exportable = False
                preview_only = True
                restriction_code = "plugin_preview_only"
                restriction_message = "插件仅声明 GPU 预览后端，无导出实现"
        effects.append(EffectCapability(
            effect_type=et, target=target, parameters=params,
            exportable=exportable, preview_only=preview_only,
            unsupported=unsupported,
            delivery_restriction_code=restriction_code,
            delivery_restriction_message=restriction_message,
        ))

    # --- operations（M3.1：能力真相源 = compiler 真正能编译的 OperationKind）---
    # 不再无条件声明全部 OperationKind：manifest 与 compiler 必须同一真相，否则
    # 模型会生成 compile 阶段必然失败的 plan（审查 M3.1）。只有出现在
    # COMPILABLE_OPERATIONS 中的 kind 才对模型暴露；extra_operations 允许显式追加
    # （如实验性 flag 启用、且已补 compiler 分支的 operation）。
    from src.features.edit.ai_editing.plan_compiler import COMPILABLE_OPERATIONS

    operations = tuple(sorted(k.value for k in COMPILABLE_OPERATIONS)) + tuple(extra_operations)

    # Expose the expression surface from the same parser used by the runtime.
    # This keeps AI/script clients from inventing functions that the renderer
    # will reject, and makes the expression editor discoverable through the
    # existing composite capability manifest.
    from src.features.composite.utils.expression_parser import (
        RUNTIME_FUNCTION_NAMES,
        SAFE_MATH_FUNCTIONS,
    )
    expression_functions = sorted(
        set(SAFE_MATH_FUNCTIONS) | set(RUNTIME_FUNCTION_NAMES)
    )

    # Timeline capability surface (T62-T65): the AI/script vocabulary for the
    # AE-style timeline. Sourced from the same modules the UI uses so the
    # manifest can never drift from what actually executes — every command
    # name, error code, row type and shortcut here is asserted by tests.
    timeline_capability = _build_timeline_capability()

    # --- export codecs ---
    codecs: list[str] = []
    try:
        from src.features.edit.core.timeline_export import ExportCodec

        codecs = [c.value for c in ExportCodec]
    except Exception:  # noqa: BLE001 — 编解码器读取失败按空列表降级（AUD-13114）
        # 同上：可导出格式清空属于重大能力面损失，必须留痕可诊断。
        logger.warning(
            "build_manifest: ExportCodec 读取失败，manifest 可导出编解码器降级为空",
            exc_info=True,
        )
        codecs = []

    # --- T-P0-00：注入运行时能力快照 + 不可用交付目标的 fail-closed 门禁 ---
    runtime: dict[str, Any] = {}
    export_extra: dict[str, Any] = {}
    if runtime_snapshot is not None:
        runtime = runtime_snapshot.to_dict()
        topos_entry = runtime_snapshot.entry("codec.topos")
        if topos_entry is not None and topos_entry.state.value != "AVAILABLE":
            codecs = [c for c in codecs if not c.startswith("topos")]
            export_extra = {
                "topos_removed_reason_code": topos_entry.reason_code
                or "CODEC_TOPOS_NOT_AVAILABLE",
                "topos_removed_detail": topos_entry.detail,
            }

    from src.features.edit.ai_editing.capability_status import build_capability_matrix

    capabilities = build_capability_matrix(
        feature_flags=feature_flags,
        profile=profile,
        credential_available=credential_available,
        generation_providers=generation_providers,
        analysis_provider_ids=analysis_provider_ids,
    )

    return CapabilityManifest(
        protocol=TAEP_PROTOCOL_VERSION,
        project_revision=project_revision,
        timebase_support={
            "frame_only_writes": True,
            "accepted_fps": list(_ACCEPTED_FPS),
        },
        track_types=("video", "audio", "subtitle"),
        operations=operations,
        transitions=_SUPPORTED_TRANSITIONS,
        effects=tuple(effects),
        export={"codecs": codecs, "requires_confirmation": True, **export_extra},
        limits={"max_operations_per_plan": 500, "max_preview_frames": 300},
        composite={
            "protocol": "topos.composite/v1",
            "editable_domains": ["parameters", "keyframes", "bezier_handles", "expressions"],
            "operations": [
                "add_keyframe", "set_keyframes", "remove_keyframe", "clear_keyframes",
                "set_interpolation", "set_bezier_handles", "set_tangent_mode",
                "set_extrapolation", "set_parameter", "set_expression", "remove_expression",
            ],
            "interpolation": ["linear", "bezier", "hold", "ease_in", "ease_out", "ease_in_out"],
            "tangent_modes": ["auto", "smooth", "broken", "linear", "flat", "step"],
            "extrapolation": ["constant", "linear"],
            "expression_functions": expression_functions,
            "expression_context": [
                "frame", "time", "fps", "value", "valueAtTime",
                "comp.width", "comp.height", "thisComp.width", "thisComp.height",
            ],
            "requires_approval_for_apply": True,
            "timeline": timeline_capability,
        },
        capabilities=tuple(item.to_dict() for item in capabilities),
        runtime=runtime,
    )


def is_operation_supported(manifest: CapabilityManifest, kind: str) -> bool:
    """检查某 operation kind 是否在 manifest 支持列表内（给 validator 用）。"""
    return kind in manifest.operations


def is_effect_known(manifest: CapabilityManifest, effect_type: str) -> bool:
    """检查某 effect 是否在 manifest 中（给 validator 用）。"""
    return any(e.effect_type == effect_type for e in manifest.effects)


def get_effect_capability(
    manifest: CapabilityManifest, effect_type: str
) -> EffectCapability | None:
    """取某 effect 的 capability 描述（含参数 schema）。"""
    for e in manifest.effects:
        if e.effect_type == effect_type:
            return e
    return None
