"""TAEP JSON Schema 校验与协议版本协商（Phase 1）。

不引入 ``jsonschema`` 依赖：实现一个**专注**于 EditPlan 结构的轻量校验器，
覆盖 LLM 最容易犯的错（缺字段/类型错/未知 operation/参数结构可疑）。
完整 JSON Schema 文件由 ``build_plan_json_schema()`` 生成，供外部 MCP/host
做严格校验或文档发布。

策略（TAEP §5.6 / §5.8）：

- **未知 core 字段透传、不拒绝**（AUD-14317 修正文档）：结构层刻意保持
  宽容——每种 operation 的可选参数清单由 ``plan_compiler`` 的编译目标持续
  演进，在 schema 层穷举会误伤合法可选字段。未知 core 字段随 plan 流转但
  编译器忽略之（不产生未定义行为）；仅对明显可疑的纯数字键给 WARNING。
  ``extension`` 字段（``x_*`` 前缀）按 TAEP §3.2 保留透传，同样不参与
  编译。需要严格白名单的外部 host 请用 ``build_plan_json_schema()`` 自行
  收紧（``additionalProperties``）。
- 协议版本协商：``taep/1.0``；不匹配返回 ``TAEP_PROTOCOL_VERSION_MISMATCH``。
- 校验产生 ``Diagnostic`` 列表，与 domain 校验共用同一返回结构。
"""

from __future__ import annotations

from typing import Any

from src.features.edit.ai_editing.diagnostics import (
    DiagnosticSeverity,
    TaepDiagnosticCode,
    make_diagnostic,
)
from src.features.edit.ai_editing.models import (
    READONLY_TOOLS,
    TAEP_PROTOCOL_VERSION,
    OperationKind,
)

# 允许透传的 extension 字段前缀（TAEP §3.2）。不参与编译，但保留信息。
EXTENSION_PREFIX = "x_"


# ---------------------------------------------------------------------- #
# 协议版本协商
# ---------------------------------------------------------------------- #


def negotiate_protocol(received: str, *, expected: str = TAEP_PROTOCOL_VERSION) -> str | None:
    """返回兼容的协议版本，或 None 表示不兼容。

    首版只支持 ``taep/1.0``。未来 minor 版本可向前兼容（``taep/1.x``），
    major 变更需显式迁移。
    """
    if received == expected:
        return expected
    # 容忍缺失前缀
    if received == "1.0":
        return expected
    return None


# ---------------------------------------------------------------------- #
# 每种 operation 的必填字段定义（TAEP §5.7 编译目标的参数契约）
# ---------------------------------------------------------------------- #
# key = OperationKind, value = (必填字段, 数值字段范围检查)
# 这里只定义结构性必填；语义校验（overlap/source range 越界）由 plan_validator 做。

_OPERATION_REQUIRED_FIELDS: dict[OperationKind, tuple[str, ...]] = {
    OperationKind.CREATE_TIMELINE: ("name",),
    OperationKind.ENSURE_TRACK: ("track_type",),
    OperationKind.RENAME_TRACK: ("track_id", "name"),
    OperationKind.SET_TRACK_STATE: ("track_id",),
    OperationKind.ADD_CLIP: ("track_id", "asset_id", "source", "timeline"),
    OperationKind.MOVE_CLIP: ("clip_id", "timeline"),
    OperationKind.TRIM_CLIP: ("clip_id",),
    OperationKind.SPLIT_CLIP: ("clip_id",),
    OperationKind.DELETE_CLIP: ("clip_id",),
    OperationKind.REPLACE_CLIP: ("clip_id", "asset_id", "source"),
    OperationKind.ADD_TRANSITION: ("track_id", "left_clip_id", "right_clip_id", "transition_type"),
    OperationKind.REMOVE_TRANSITION: ("transition_id",),
    OperationKind.SET_CLIP_FADE: ("clip_id",),
    OperationKind.SET_TRANSFORM: ("clip_id",),
    OperationKind.SET_CROP: ("clip_id",),
    OperationKind.SET_OPACITY: ("clip_id",),
    OperationKind.SET_SPEED: ("clip_id",),
    OperationKind.SET_EFFECT: ("clip_id", "effect_type"),
    OperationKind.REMOVE_EFFECT: ("clip_id", "effect_type"),
    OperationKind.ENSURE_SUBTITLE_TRACK: (),
    OperationKind.UPDATE_SUBTITLE_TRACK: ("track_id",),
    OperationKind.ADD_SUBTITLE: ("track_id", "timeline", "text"),
    OperationKind.UPDATE_SUBTITLE: ("clip_id",),
    OperationKind.DELETE_SUBTITLE: ("clip_id",),
    OperationKind.PROMOTE_SUBTITLE_TO_GRAPHIC: ("clip_id",),
    OperationKind.APPLY_CAPTION_GRAPHIC_TEMPLATE: ("template_id",),
    OperationKind.UPDATE_CAPTION_GRAPHIC_PARAMETERS: ("clip_id", "parameters"),
    OperationKind.DETACH_CAPTION_GRAPHIC: ("clip_id",),
    OperationKind.DEMOTE_GRAPHIC_TO_SUBTITLE: ("clip_id",),
    OperationKind.SET_MARKER: ("frame",),
    OperationKind.SET_RANGE: (),
    OperationKind.ADD_REVIEW_NOTE: (),
    OperationKind.REQUEST_EXPORT: (),
}

# 未知 operation kind 的白名单兜底：白名单在 OperationKind 枚举里，校验时查枚举。


# ---------------------------------------------------------------------- #
# 轻量结构校验器
# ---------------------------------------------------------------------- #


def validate_plan_structure(
    plan_dict: dict[str, Any],
    *,
    compilable_operations: frozenset[str] | None = None,
) -> list[tuple[DiagnosticSeverity, TaepDiagnosticCode, str, str | None, str | None, dict[str, Any]]]:
    """校验 plan 字典的结构（不涉及 canonical domain）。

    AUD-14318：原先函数体内有两个连续 docstring 字面量（第二个是死代码），
    合并为一个。

    Args:
        plan_dict: 待校验的 plan 字典。
        compilable_operations: 当前运行时真正可编译的 operation kind 字符串集合。
            默认从 ``plan_compiler.COMPILABLE_OPERATIONS`` 派生（TAEP §4.3 单一真相）。
            传入时让结构校验阶段就能拒绝"协议合法但未实现"的 operation（如
            ``add_subtitle``），避免诱导模型生成后才在 domain 层失败。传入空集合
            可禁用该检查（兼容旧行为/测试）。

    Returns:
        诊断元组列表（severity, code, message, op_id, path, vars），由调用方
        转为 ``Diagnostic``。结构上合法时返回空列表。

    注意：本函数**不**校验 source range 是否越界、clip 是否重叠等 domain 语义
    —— 那些需要 ``EditProject``/``TimelineSequence`` 上下文，由 ``plan_validator``
    负责。
    """
    findings: list[tuple[DiagnosticSeverity, TaepDiagnosticCode, str, str | None, str | None, dict[str, Any]]] = []

    # TAEP §4.3：结构校验阶段就拒绝"协议合法但 compiler 未实现"的 operation，
    # 避免模型生成的 plan 过了 schema 关却在 domain 层才失败（诱导模型）。
    # 默认不启用（保持 schema 层纯结构校验、不耦合 compiler，向后兼容）；
    # 由调用方（plan_validator）显式传入 COMPILABLE_OPERATIONS 实现早拒绝。
    effective_compilable: frozenset[str] | None = compilable_operations

    # 顶层必填
    for key in ("plan_id", "base_revision", "target", "operations"):
        if key not in plan_dict:
            findings.append((
                DiagnosticSeverity.ERROR, TaepDiagnosticCode.SCHEMA_INVALID,
                f"plan missing required field {key!r}", None, f"/{key}", {},
            ))

    # 协议版本
    proto = plan_dict.get("protocol")
    if proto is not None and negotiate_protocol(str(proto)) is None:
        findings.append((
            DiagnosticSeverity.ERROR, TaepDiagnosticCode.PROTOCOL_VERSION_MISMATCH,
            "protocol version", None, "/protocol",
            {"expected": TAEP_PROTOCOL_VERSION, "received": str(proto)},
        ))

    # base_revision 类型
    br = plan_dict.get("base_revision")
    if br is not None and (not isinstance(br, int) or isinstance(br, bool) or br < 0):
        findings.append((
            DiagnosticSeverity.ERROR, TaepDiagnosticCode.SCHEMA_INVALID,
            f"base_revision must be a non-negative int, got {br!r}", None, "/base_revision", {},
        ))

    # target 结构
    target = plan_dict.get("target")
    if isinstance(target, dict):
        for k in ("project_id", "timeline_id"):
            if not target.get(k):
                findings.append((
                    DiagnosticSeverity.ERROR, TaepDiagnosticCode.SCHEMA_INVALID,
                    f"target.{k} is required", None, f"/target/{k}", {},
                ))

    # operations
    ops = plan_dict.get("operations")
    if ops is not None:
        if not isinstance(ops, list):
            findings.append((
                DiagnosticSeverity.ERROR, TaepDiagnosticCode.SCHEMA_INVALID,
                "operations must be a list", None, "/operations", {},
            ))
        else:
            seen_op_ids: set[str] = set()
            for i, op in enumerate(ops):
                if not isinstance(op, dict):
                    findings.append((
                        DiagnosticSeverity.ERROR, TaepDiagnosticCode.SCHEMA_INVALID,
                        f"operation #{i} must be an object", None, f"/operations/{i}", {},
                    ))
                    continue
                op_id = op.get("op_id")
                op_path = f"/operations/{i}"
                # op_id 必填且唯一
                if not op_id:
                    findings.append((
                        DiagnosticSeverity.ERROR, TaepDiagnosticCode.SCHEMA_INVALID,
                        f"operation #{i} missing op_id", None, f"{op_path}/op_id", {},
                    ))
                elif op_id in seen_op_ids:
                    findings.append((
                        DiagnosticSeverity.ERROR, TaepDiagnosticCode.SCHEMA_INVALID,
                        f"duplicate op_id {op_id!r}", op_id, f"{op_path}/op_id", {},
                    ))
                else:
                    seen_op_ids.add(op_id)

                # kind 必填、必须是字符串且在白名单。
                # AUD-14316：``OperationKind(kind_str)`` 对不可哈希对象
                # （list/dict）抛 TypeError 而非 ValueError，``except
                # ValueError`` 捕不到 → 校验器自身被畸形输入击穿。先做
                # isinstance 守卫，再同时捕获 TypeError 兜底。
                kind_str = op.get("kind")
                if not isinstance(kind_str, str) or not kind_str:
                    findings.append((
                        DiagnosticSeverity.ERROR, TaepDiagnosticCode.SCHEMA_INVALID,
                        f"operation {op_id or i} kind must be a non-empty string, "
                        f"got {type(kind_str).__name__}",
                        op_id, f"{op_path}/kind", {},
                    ))
                    continue
                try:
                    kind = OperationKind(kind_str)
                except (ValueError, TypeError):
                    if kind_str in READONLY_TOOLS:
                        findings.append((
                            DiagnosticSeverity.ERROR, TaepDiagnosticCode.UNSUPPORTED_OPERATION,
                            f"read-only tool {kind_str!r} cannot appear in operations",
                            op_id, f"{op_path}/kind", {"op_kind": kind_str},
                        ))
                    else:
                        findings.append((
                            DiagnosticSeverity.ERROR, TaepDiagnosticCode.UNSUPPORTED_OPERATION,
                            f"unsupported operation kind {kind_str!r}",
                            op_id, f"{op_path}/kind", {"op_kind": kind_str},
                        ))
                    continue

                # TAEP §4.3：kind 在枚举里（协议合法）但 compiler 未实现 → 直接
                # 产 UNSUPPORTED_OPERATION，不继续校验其参数（参数再合法也无意义）。
                # 这样模型在 schema 阶段就被拒，而非诱导其生成后才在 domain 层失败。
                # None 或空集合 = 禁用早拒绝（兼容旧行为/compiler 不可用）。
                if effective_compilable and kind.value not in effective_compilable:
                    findings.append((
                        DiagnosticSeverity.ERROR, TaepDiagnosticCode.UNSUPPORTED_OPERATION,
                        f"operation kind {kind.value!r} is declared but not yet implemented",
                        op_id, f"{op_path}/kind", {"op_kind": kind.value},
                    ))
                    continue

                # 必填字段
                required = _OPERATION_REQUIRED_FIELDS.get(kind, ())
                params = op.get("params", {})
                if not isinstance(params, dict):
                    findings.append((
                        DiagnosticSeverity.ERROR, TaepDiagnosticCode.SCHEMA_INVALID,
                        f"operation {op_id} params must be an object", op_id, f"{op_path}/params", {},
                    ))
                    params = {}
                for req in required:
                    if req not in params:
                        findings.append((
                            DiagnosticSeverity.ERROR, TaepDiagnosticCode.SCHEMA_INVALID,
                            f"operation {op_id} ({kind.value}) missing required param {req!r}",
                            op_id, f"{op_path}/params/{req}", {},
                        ))
                if kind == OperationKind.APPLY_CAPTION_GRAPHIC_TEMPLATE:
                    if not params.get("track_id") and not params.get("track_ids"):
                        findings.append((
                            DiagnosticSeverity.ERROR, TaepDiagnosticCode.SCHEMA_INVALID,
                            f"operation {op_id} ({kind.value}) requires track_id or track_ids",
                            op_id, f"{op_path}/params", {},
                        ))
                # AUD-14100：set_clip_fade 的 fade 字段是"至少其一"的
                # 二选一约束，无法用 _OPERATION_REQUIRED_FIELDS 表达——
                # 缺失时模型生成的 plan 过了必填字段检查却在 compile 阶段
                # 失败（且修复前以裸 ValueError 逃逸 preview 边界）。
                # 在结构层即拒绝，产出可修复的 op 级诊断。
                if kind == OperationKind.SET_CLIP_FADE:
                    if (params.get("fade_in_duration") is None
                            and params.get("fade_out_duration") is None):
                        findings.append((
                            DiagnosticSeverity.ERROR, TaepDiagnosticCode.SCHEMA_INVALID,
                            f"operation {op_id} ({kind.value}) requires "
                            f"fade_in_duration or fade_out_duration",
                            op_id, f"{op_path}/params", {},
                        ))

                # 未知 core 字段（AUD-14317：透传、不拒绝——见模块 docstring）
                for pkey in params:
                    # 非 str 键（编程调用方）直接结构错误，避免后续
                    # startswith 抛 AttributeError（守卫先于使用）。
                    if not isinstance(pkey, str):
                        findings.append((
                            DiagnosticSeverity.ERROR, TaepDiagnosticCode.SCHEMA_INVALID,
                            f"operation {op_id} has non-string param key "
                            f"{type(pkey).__name__}",
                            op_id, f"{op_path}/params", {},
                        ))
                        continue
                    if pkey.startswith(EXTENSION_PREFIX):
                        continue
                    # 允许的参数名集合 = 该 kind 的必填 + 常见可选（宽容，不在此穷举）
                    # 这里只对明显可疑的纯数字键做提示，避免误伤合法可选字段。
                    if pkey.isdigit():
                        findings.append((
                            DiagnosticSeverity.WARNING, TaepDiagnosticCode.SCHEMA_INVALID,
                            f"operation {op_id} has suspicious numeric param key {pkey!r}",
                            op_id, f"{op_path}/params/{pkey}", {},
                        ))

    return findings


def findings_to_diagnostics(
    findings: list[tuple[DiagnosticSeverity, TaepDiagnosticCode, str, str | None, str | None, dict[str, Any]]],
    *,
    lang: str = "zh",
) -> list:
    """把结构校验的原始 findings 转为 ``Diagnostic``。

    SCHEMA_INVALID / UNSUPPORTED_OPERATION 的消息直接用 findings 里预置的英文
    detail（更精确），不强制走模板。
    """
    from src.features.edit.ai_editing.diagnostics import Diagnostic

    diags: list[Diagnostic] = []
    for severity, code, detail, op_id, path, vars_ in findings:
        if code == TaepDiagnosticCode.SCHEMA_INVALID:
            msg = detail  # 已含上下文
        elif code == TaepDiagnosticCode.UNSUPPORTED_OPERATION:
            msg = make_diagnostic(severity, code, op_id=op_id, path=path, lang=lang, **vars_).message
        elif code == TaepDiagnosticCode.PROTOCOL_VERSION_MISMATCH:
            msg = make_diagnostic(severity, code, op_id=op_id, path=path, lang=lang, **vars_).message
        else:
            msg = detail
        diags.append(Diagnostic(
            severity=severity, code=code, message=msg, op_id=op_id, path=path,
        ))
    return diags


# ---------------------------------------------------------------------- #
# 完整 JSON Schema 生成（供外部 host / 文档发布）
# ---------------------------------------------------------------------- #


def build_plan_json_schema() -> dict[str, Any]:
    """生成 EditPlan 的完整 JSON Schema（draft-07）。

    供 MCP/HTTP host 做严格校验，或作为协议文档发布。内部校验仍用上面的轻量
    校验器（无 jsonschema 依赖）。
    """
    operation_kinds = [k.value for k in OperationKind]
    return {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "title": "TAEP EditPlan",
        "type": "object",
        "required": ["protocol", "plan_id", "base_revision", "target", "operations"],
        "properties": {
            "protocol": {"type": "string", "const": TAEP_PROTOCOL_VERSION},
            "plan_id": {"type": "string", "minLength": 1},
            "base_revision": {"type": "integer", "minimum": 0},
            "mode": {"type": "string", "enum": ["preview", "commit", "dry_run_export"]},
            "title": {"type": "string"},
            "rationale": {"type": "string"},
            "target": {
                "type": "object",
                "required": ["project_id", "timeline_id"],
                "properties": {
                    "project_id": {"type": "string", "minLength": 1},
                    "timeline_id": {"type": "string", "minLength": 1},
                },
            },
            "operations": {
                "type": "array",
                "description": (
                    "每个 operation 的 params 透传未知 core 字段（编译器忽略，"
                    "AUD-14317）；x_* 前缀为 extension 保留。需要严格白名单的"
                    " host 请自行收紧 items.additionalProperties。"
                ),
                "items": {
                    "type": "object",
                    "required": ["op_id", "kind", "params"],
                    "properties": {
                        "op_id": {"type": "string", "minLength": 1},
                        "kind": {"type": "string", "enum": operation_kinds},
                        "params": {
                            "type": "object",
                            "description": (
                                "未知 core 字段透传不拒绝（编译器忽略）；"
                                "x_* 前缀为 extension 保留。"
                            ),
                        },
                        "intent": {"type": "object"},
                    },
                },
            },
            "brief_id": {"type": "string"},
            "variant_id": {"type": "string"},
            "decision_refs": {"type": "array", "items": {"type": "string"}},
            "analysis_snapshot_hash": {"type": "string"},
        },
        # AUD-14317：extension/未知 core 字段均透传——编译器忽略未知字段，
        # 不存在"默认拒绝"行为（文档与实现保持一致，见模块 docstring）。
        "additionalProperties": True,
    }
