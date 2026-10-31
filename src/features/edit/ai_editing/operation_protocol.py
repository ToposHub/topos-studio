"""T-P2-20: 统一产品操作协议 —— 全域操作 schema 矩阵（字段级契约）.

卡要求：每个操作的 schema 至少包含 ``operation_id``、``domain``、
``permission``、``scope``、``preconditions``、``idempotency_key``、
``preview_diff``、``apply``、``verify``、``undo_or_compensation``、
``error_codes``、``required_capabilities``。

设计原则（对应 §11.2 诚实条款）：
* ``apply`` 只指向**真实存在**的入口（ToolFacade 工具名 / EditPlan
  OperationKind / CommandStack 命令 / service 方法），不造"计划中"的条目；
* ``error_codes`` 优先引用 TaepDiagnosticCode / ERR_* 真实词汇表；
* ``required_capabilities`` 引用 get_capabilities 的真实 capability_id
  （如 offline_help / automatic_export）或编码能力（codec 名）；
* ``idempotency_key`` 是带命名占位符的模板（``{clip_id}`` 等），供
  WorkflowTransaction 去重。

消费方：WorkflowTransaction（T-P2-21）按 operation_id 取契约；测试
``test_operation_protocol_matrix.py`` 固定字段完整性 + 与真实 manifest/
NODE_OPS 的一致性；``to_json()`` 输出机器可读矩阵供审计与文档。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


#: 卡点要求的 12 个必备字段——少一字段即架构缺陷（测试强制）。
REQUIRED_FIELDS = (
    "operation_id", "domain", "permission", "scope", "preconditions",
    "idempotency_key", "preview_diff", "apply", "verify",
    "undo_or_compensation", "error_codes", "required_capabilities",
)

#: 卡点要求的域清单（顺序即文档展示顺序）。
DOMAINS = ("Project", "Media", "Edit", "Color", "Composite", "Deliver",
           "AI/Models")


@dataclass(frozen=True)
class OperationContract:
    """单个产品操作的字段级契约（12 必备字段全部存在）。"""

    operation_id: str
    domain: str
    permission: str            # ToolPermission 值：read/preview/write/expensive/file_write
    scope: str                 # 作用对象（project/timeline/clip/node/…）+ 定位键
    preconditions: tuple[str, ...]
    idempotency_key: str       # 模板："{clip_id}:{plan_hash}" 等
    preview_diff: bool         # apply 前是否产出 preview/diff
    apply: str                 # 真实入口（工具名 / kind / service 方法）
    verify: str                # 验证证据（probe/telemetry/revision/…）
    undo_or_compensation: str  # 撤销或补偿路径
    error_codes: tuple[str, ...]
    required_capabilities: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "operation_id": self.operation_id,
            "domain": self.domain,
            "permission": self.permission,
            "scope": self.scope,
            "preconditions": list(self.preconditions),
            "idempotency_key": self.idempotency_key,
            "preview_diff": self.preview_diff,
            "apply": self.apply,
            "verify": self.verify,
            "undo_or_compensation": self.undo_or_compensation,
            "error_codes": list(self.error_codes),
            "required_capabilities": list(self.required_capabilities),
        }


def _op(operation_id: str, domain: str, **kw: Any) -> OperationContract:
    """紧凑构造器：preview_diff 缺省 True；permission 缺省 "write"
    （保守值——写操作必须经宿主 approval 才能生效）。"""
    kw.setdefault("preview_diff", True)
    kw.setdefault("permission", "write")
    return OperationContract(
        operation_id=operation_id, domain=domain,
        permission=kw["permission"], scope=kw["scope"],
        preconditions=tuple(kw.get("preconditions", ())),
        idempotency_key=kw["idempotency_key"],
        preview_diff=bool(kw["preview_diff"]),
        apply=kw["apply"], verify=kw["verify"],
        undo_or_compensation=kw["undo"],
        error_codes=tuple(kw.get("codes", ())),
        required_capabilities=tuple(kw.get("caps", ())),
    )


def _build_registry() -> tuple[OperationContract, ...]:
    """全域操作矩阵。

    每个条目的 apply 都引用当前代码库真实存在的入口；error_codes 优先
    取 TaepDiagnosticCode（TAEP_*）/ composite ERR_* / TAEP_EXPORT_*。
    """
    R: list[OperationContract] = []

    # ── Project ──
    R += [
        _op("project.create", "Project", permission="write",
            scope="project", preconditions=("no active project lock",),
            idempotency_key="project:{name}:{created_at}",
            apply="EditProject.create", verify="project_id + revision>0",
            undo="close without save", codes=("TAEP_INTERNAL_ERROR",)),
        _op("project.open", "Project", permission="read",
            scope="project:{project_id}", preconditions=("file exists",),
            idempotency_key="project.open:{project_id}",
            apply="persistence load", verify="revision + schema version",
            undo="n/a (read)", codes=("TAEP_ASSET_NOT_FOUND",)),
        _op("project.save", "Project", permission="file_write",
            scope="project:{project_id}", preconditions=("revision advanced",),
            idempotency_key="project.save:{project_id}:{base_revision}",
            apply="persistence save", verify="file mtime + reload parity",
            undo="previous save file retained", codes=("TAEP_INTERNAL_ERROR",),
            caps=("automatic_export",)),
        _op("project.save_as", "Project", permission="file_write",
            scope="project:{project_id}:path:{path}",
            preconditions=("path grant issued",),
            idempotency_key="project.save_as:{path}",
            apply="persistence save-as (PathGrant bound)",
            verify="new file readable + project_id preserved",
            undo="delete created file", codes=("TAEP_INTERNAL_ERROR",)),
        _op("project.undo", "Project", permission="write",
            scope="timeline:{timeline_id}",
            preconditions=("command stack non-empty",),
            idempotency_key="project.undo:{stack_depth}",
            apply="CommandStack.undo", verify="revision delta -1",
            undo="redo", codes=("TAEP_INTERNAL_ERROR",)),
        _op("project.redo", "Project", permission="write",
            scope="timeline:{timeline_id}",
            preconditions=("redo stack non-empty",),
            idempotency_key="project.redo:{stack_depth}",
            apply="CommandStack.redo", verify="revision delta +1",
            undo="undo", codes=("TAEP_INTERNAL_ERROR",)),
        _op("project.revision.read", "Project", permission="read",
            scope="project:{project_id}", idempotency_key="revision:{project_id}",
            apply="ProjectSequences.revision", verify="monotonic revision",
            undo="n/a (read)"),
        _op("project.migrate", "Project", permission="write",
            scope="project:{project_id}:schema:{version}",
            preconditions=("older schema version detected",),
            idempotency_key="project.migrate:{project_id}:{version}",
            apply="persistence migration", verify="schema version + probe",
            undo="backup file retained", codes=("TAEP_INTERCHANGE_LOSSY",)),
        _op("project.audit.read", "Project", permission="read",
            scope="run:{run_id}", idempotency_key="audit:{run_id}",
            apply="AuditStore.read", verify="entries json-safe",
            undo="n/a (read)"),
    ]

    # ── Media ──
    R += [
        _op("media.import", "Media", permission="write",
            scope="project:{project_id}:path:{path}",
            preconditions=("path readable", "codec probe ok"),
            idempotency_key="media.import:{path}:{size_bytes}",
            apply="EditProject.with_media_asset_added",
            verify="asset probe + duration>0",
            undo="with_media_asset_removed",
            codes=("TAEP_ASSET_NOT_FOUND",)),
        _op("media.remove", "Media", permission="write",
            scope="asset:{asset_id}", preconditions=("no clip references",),
            idempotency_key="media.remove:{asset_id}",
            apply="DeleteAssetCommand", verify="references == 0",
            undo="CommandStack.undo",
            codes=("TAEP_ASSET_NOT_FOUND",)),
        _op("media.relink", "Media", permission="write",
            scope="asset:{asset_id}:path:{path}",
            preconditions=("new path probes to same stream layout",),
            idempotency_key="media.relink:{asset_id}:{path}",
            apply="UpdateClipSourceCommand", verify="probe parity",
            undo="relink back", codes=("TAEP_ASSET_NOT_FOUND",)),
        _op("media.probe", "Media", permission="read",
            scope="asset:{asset_id}", idempotency_key="probe:{asset_id}",
            apply="media factory probe", verify="stream layout json-safe",
            undo="n/a (read)"),
        _op("media.proxy", "Media", permission="expensive",
            scope="asset:{asset_id}",
            preconditions=("source decodable",),
            idempotency_key="media.proxy:{asset_id}:{preset}",
            apply="proxy policy build job", verify="proxy probe + fps match",
            undo="delete proxy cache", caps=("automatic_export",),
            codes=("TAEP_ASSET_NOT_FOUND", "TAEP_INTERNAL_ERROR")),
        _op("media.search", "Media", permission="read",
            scope="project:{project_id}:query:{query}",
            idempotency_key="media.search:{query}:{window}",
            apply="topos.search_assets", verify="segments json-safe",
            undo="n/a (read)"),
        _op("media.analysis", "Media", permission="expensive",
            scope="asset:{asset_id}",
            preconditions=("analysis provider installed",),
            idempotency_key="media.analysis:{asset_id}:{provider}",
            apply="AnalysisService request", verify="analysis record stored",
            undo="delete analysis record", caps=("local_media_analysis",),
            codes=("TAEP_INTERNAL_ERROR", "TAEP_QUOTA_EXCEEDED")),
        _op("media.offline_recover", "Media", permission="write",
            scope="asset:{asset_id}",
            preconditions=("asset offline",),
            idempotency_key="media.recover:{asset_id}",
            apply="offline/recover flow", verify="probe restored",
            undo="mark offline again",
            codes=("TAEP_ASSET_NOT_FOUND",)),
    ]

    # ── Edit（与 EditPlan OperationKind / manifest.operations 同源）──
    edit_common = dict(
        permission="write",
        idempotency_key="{op_id}:{plan_hash}",
        apply="topos.apply_edit_plan (EditPlan OperationKind)",
        verify="commit revision + preview diff clean",
        undo="MacroCommand undo (single undo group)",
        codes=("TAEP_SCHEMA_INVALID", "TAEP_TRACK_OVERLAP",
               "TAEP_SOURCE_RANGE_INVALID"),
    )
    R += [
        _op("edit.add_clip", "Edit", scope="clip:{clip_id}@track:{track_id}",
            preconditions=("asset exists", "revision fresh"), **edit_common),
        _op("edit.delete_clip", "Edit", scope="clip:{clip_id}", **edit_common),
        _op("edit.move_clip", "Edit", scope="clip:{clip_id}", **edit_common),
        _op("edit.trim_clip", "Edit", scope="clip:{clip_id}", **edit_common),
        _op("edit.split_clip", "Edit", scope="clip:{clip_id}@frame:{frame}",
            **edit_common),
        _op("edit.replace_clip", "Edit", scope="clip:{clip_id}", **edit_common),
        _op("edit.set_transform", "Edit", scope="clip:{clip_id}",
            preview_diff=False, **edit_common),
        _op("edit.set_crop", "Edit", scope="clip:{clip_id}",
            preview_diff=False, **edit_common),
        _op("edit.set_opacity", "Edit", scope="clip:{clip_id}",
            preview_diff=False, **edit_common),
        _op("edit.set_speed", "Edit", scope="clip:{clip_id}",
            preview_diff=False, **edit_common),
        _op("edit.set_effect", "Edit", scope="clip:{clip_id}:effect:{effect_type}",
            preview_diff=False,
            caps=("effect:{effect_type}:exportable",),
            **{**edit_common,
               "codes": ("TAEP_EFFECT_INVALID", "TAEP_EFFECT_NOT_DELIVERABLE",
                         "TAEP_EFFECT_PARAMETER_INVALID")}),
        _op("edit.remove_effect", "Edit", scope="clip:{clip_id}:effect:{effect_id}",
            **edit_common),
        _op("edit.set_marker", "Edit", scope="timeline:{timeline_id}@frame:{frame}",
            **edit_common),
        _op("edit.add_subtitle", "Edit", scope="subtitle:{cue_id}", **edit_common),
        _op("edit.update_subtitle", "Edit", scope="subtitle:{cue_id}",
            **edit_common),
        _op("edit.ensure_track", "Edit", scope="track:{track_id}", **edit_common),
        _op("edit.set_track_state", "Edit", scope="track:{track_id}",
            preview_diff=False, **edit_common),
        _op("edit.rename_track", "Edit", scope="track:{track_id}",
            preview_diff=False, **edit_common),
        _op("edit.add_transition", "Edit",
            scope="transition:{transition_id}:clip:{clip_id}", **edit_common),
        _op("edit.ensure_subtitle_track", "Edit", scope="track:{track_id}",
            **edit_common),
        _op("edit.keyframe.add", "Edit", permission="write",
            scope="clip:{clip_id}:param:{param_name}",
            preconditions=("param in catalog ParamDef range",),
            idempotency_key="kf.add:{effect_id}:{param}:{frame}",
            apply="AddKeyframeCommand", verify="has_keyframe_at",
            undo="RemoveKeyframeCommand (undo stores _old_value)",
            codes=("TAEP_EFFECT_PARAMETER_INVALID",)),
        _op("edit.set_clip_fade", "Edit", scope="clip:{clip_id}",
            preview_diff=False, **edit_common),
        _op("edit.delete_subtitle", "Edit", scope="subtitle:{cue_id}",
            **edit_common),
        _op("edit.update_subtitle_track", "Edit", scope="track:{track_id}",
            **edit_common),
        _op("edit.apply_caption_graphic_template", "Edit",
            scope="clip:{clip_id}:template:{template_id}", **edit_common),
        _op("edit.update_caption_graphic_parameters", "Edit",
            scope="clip:{clip_id}:graphic:{graphic_id}",
            preview_diff=False, **edit_common),
        _op("edit.detach_caption_graphic", "Edit",
            scope="clip:{clip_id}:graphic:{graphic_id}", **edit_common),
        _op("edit.promote_subtitle_to_graphic", "Edit",
            scope="subtitle:{cue_id}", **edit_common),
        _op("edit.demote_graphic_to_subtitle", "Edit",
            scope="clip:{clip_id}:graphic:{graphic_id}", **edit_common),
    ]

    # ── Color ──
    color_common = dict(
        permission="write",
        apply="ColorStack primary grade (preview/export/GPU 三路同源)",
        verify="10-bit 对拍 Δ≤1 LSB（T-P1-21 量化）",
        codes=("TAEP_INTERNAL_ERROR",),
    )
    R += [
        _op("color.set_primary", "Color", scope="clip:{clip_id}:colorstack",
            idempotency_key="color:{clip_id}:{param_hash}",
            undo="grade snapshot restore", **color_common),
        _op("color.set_param_keyframe", "Color", permission="write",
            scope="clip:{clip_id}:tool:{tool}:param:{param}",
            idempotency_key="color.kf:{clip_id}:{param}:{frame}",
            apply="composite timeline keyframe commands",
            verify="valueAtFrame parity", undo="remove_keyframe",
            codes=("ERR_KEYFRAME_NOT_FOUND", "ERR_VALUE_OUT_OF_RANGE")),
        _op("color.preset.apply", "Color", permission="write",
            scope="clip:{clip_id}:preset:{preset}",
            idempotency_key="color.preset:{preset}:{clip_id}",
            apply="preset load → ColorStack", verify="grade hash",
            undo="previous grade restore",
            codes=("TAEP_INTERNAL_ERROR",)),
        _op("color.scopes.read", "Color", permission="read",
            scope="timeline:{timeline_id}@frame:{frame}",
            idempotency_key="scopes:{timeline_id}:{frame}",
            apply="GPU scopes bench path", verify="json-safe histograms",
            undo="n/a (read)"),
        _op("color.auto_color", "Color", permission="expensive",
            scope="clip:{clip_id}",
            preconditions=("frame decodable",),
            idempotency_key="auto_color:{clip_id}:{frame}",
            apply="Auto Color analysis → ColorStack proposal",
            verify="grade diff + user confirm", undo="undo grade apply",
            codes=("TAEP_INTERNAL_ERROR",)),
        _op("color.mask.set", "Color", permission="write",
            scope="clip:{clip_id}:mask:{mask_id}",
            idempotency_key="mask:{mask_id}:{param_hash}",
            apply="mask/qualifier params via effect chain",
            verify="mask preview render", undo="param snapshot restore",
            codes=("TAEP_INTERNAL_ERROR",)),
    ]

    # ── Composite（与 edit_service NODE_OPS / composite plan 同源）──
    node_common = dict(
        permission="write",
        apply="topos.apply_composite_edit (CompositeEditPlan NODE_OP)",
        verify="generation_after + composite snapshot",
        undo="composite edit service undo group",
        codes=("ERR_NODE_NOT_FOUND", "ERR_PARAM_NOT_FOUND"),
    )
    R += [
        _op("composite.add_node", "Composite", scope="node:{node_id}:type:{node_type}",
            idempotency_key="node.add:{node_type}:{content_hash}", **node_common),
        _op("composite.remove_node", "Composite", scope="node:{node_id}",
            idempotency_key="node.rm:{node_id}:{content_hash}", **node_common),
        _op("composite.connect", "Composite",
            scope="node:{src_node}:port:{src_port}->{dst_node}:{dst_port}",
            idempotency_key="conn:{src}:{dst}:{content_hash}", **node_common),
        _op("composite.disconnect", "Composite", scope="connection:{connection_id}",
            idempotency_key="disc:{connection_id}:{content_hash}", **node_common),
        _op("composite.set_param", "Composite", scope="node:{node_id}:param:{param}",
            preview_diff=False,
            idempotency_key="node.param:{node_id}:{param}", **node_common),
        _op("composite.duplicate_node", "Composite",
            scope="node:{node_id}", idempotency_key="node.dup:{node_id}:{content_hash}",
            **node_common),
        _op("composite.set_output_target", "Composite",
            scope="output:{output_name}", preview_diff=False,
            idempotency_key="out:{output_name}:{content_hash}", **node_common),
        _op("composite.group", "Composite", scope="nodes:{node_ids}",
            idempotency_key="group:{node_ids_hash}:{content_hash}", **node_common),
        _op("composite.ungroup", "Composite", scope="group:{group_id}",
            idempotency_key="ungroup:{group_id}:{content_hash}", **node_common),
        _op("composite.keyframes", "Composite", scope="node:{node_id}:param:{param}",
            apply="composite timeline keyframe commands",
            idempotency_key="ckf:{node_id}:{param}:{frame}",
            verify="query_at_frame parity", undo="remove_keyframe",
            codes=("ERR_KEYFRAME_NOT_FOUND",)),
        _op("composite.expressions", "Composite",
            scope="node:{node_id}:param:{param}",
            idempotency_key="expr:{node_id}:{param}:{expr_hash}",
            apply="set_expression / remove_expression",
            verify="get_expression_status=valid",
            undo="remove_expression",
            codes=("ERR_EXPR_INVALID", "EXPR_ERR_SYNTAX")),
        _op("composite.render_preview", "Composite", permission="expensive",
            scope="output:{output_name}",
            preconditions=("graph has output target",),
            idempotency_key="crender:{output_name}:{generation}",
            apply="composite page evaluation", verify="node render probe",
            undo="n/a (read-only evaluation)",
            codes=("ERR_NODE_NOT_FOUND", "TAEP_INTERNAL_ERROR")),
        _op("composite.model_deps", "Composite", permission="read",
            scope="node:{node_id}:model:{model_id}",
            idempotency_key="modeldep:{node_id}",
            apply="node capability report (blocked_missing_model 可解释)",
            verify="six-state classification", undo="n/a (read)"),
    ]

    # ── Deliver ──
    deliver_common = dict(
        apply="topos.request_export / ExportService",
        verify="preflight ok/blocking/degraded + output probe + stage telemetry",
        codes=("TAEP_EXPORT_PREFLIGHT_FAILED", "TAEP_QUALITY_GATE_FAILED"),
    )
    R += [
        _op("deliver.capabilities", "Deliver", permission="read",
            scope="project:{project_id}",
            idempotency_key="dcaps:{project_id}:{revision}",
            apply="DeliveryCapability.describe / get_capabilities",
            verify="codec list json-safe", undo="n/a (read)"),
        _op("deliver.validate", "Deliver", permission="preview",
            scope="timeline:{timeline_id}:codec:{codec}",
            idempotency_key="dvalid:{plan_hash}",
            apply="topos.validate_edit_plan + export preflight",
            verify="preflight report", undo="n/a (dry run)"),
        _op("deliver.request", "Deliver", permission="file_write",
            scope="timeline:{timeline_id}:codec:{codec}:path:{path}",
            preconditions=("path grant issued", "preflight no blocking"),
            idempotency_key="export:{plan_hash}:{output_path}",
            undo="cancel_job（完成后不可逆：需特定授权+输出范围）",
            caps=("automatic_export", "codec:{codec}"), **deliver_common),
        _op("deliver.cancel", "Deliver", permission="write",
            scope="job:{job_id}", idempotency_key="cancel:{job_id}",
            apply="topos.cancel_job", verify="outcome.cancelled=True",
            undo="n/a（取消即补偿）",
            codes=("TAEP_INTERNAL_ERROR",)),
        _op("deliver.status", "Deliver", permission="read",
            scope="job:{job_id}", idempotency_key="status:{job_id}:{poll}",
            apply="topos.get_job_status", verify="job state json-safe",
            undo="n/a (read)"),
        _op("deliver.result_verify", "Deliver", permission="read",
            scope="job:{job_id}",
            idempotency_key="dverify:{job_id}",
            apply="finalize_export_with_probe（probe+telemetry 并入 AI manifest）",
            verify="output probe + frame checks + GPU trail + quality result",
            undo="n/a (read)", caps=("offline_help",)),
        _op("deliver.batch_variants", "Deliver", permission="file_write",
            scope="timeline:{timeline_id}:variants:{variant_set}",
            preconditions=("per-variant preflight ok",),
            idempotency_key="batch:{variant_set}:{plan_hash}",
            apply="batch export jobs", verify="per-variant probes",
            undo="cancel remaining jobs", caps=("automatic_export",),
            codes=("TAEP_EXPORT_PREFLIGHT_FAILED", "TAEP_QUALITY_GATE_FAILED")),
    ]

    # ── AI/Models ──
    R += [
        _op("ai.capabilities", "AI/Models", permission="read",
            scope="project:{project_id}",
            idempotency_key="aicaps:{revision}",
            apply="topos.get_capabilities", verify="manifest json-safe",
            undo="n/a (read)"),
        _op("ai.model_status", "AI/Models", permission="read",
            scope="model:{model_id}",
            idempotency_key="model:{model_id}:{poll}",
            apply="capability manifest state (not_installed/blocked_by_policy/…)",
            verify="state + next_step 可解释", undo="n/a (read)"),
        _op("ai.model_run", "AI/Models", permission="expensive",
            scope="model:{model_id}:job:{job_id}",
            preconditions=("model installed", "consent granted"),
            idempotency_key="modelrun:{job_id}",
            apply="受控 job 发起（不得绕开 job registry）",
            verify="job VERIFYING→SUCCEEDED + artifact probe",
            undo="cancel_job（running 前）",
            codes=("TAEP_QUOTA_EXCEEDED", "TAEP_INTERNAL_ERROR"),
            caps=("requires_consent",)),
        _op("ai.diagnostics.read", "AI/Models", permission="read",
            scope="run:{run_id}", idempotency_key="aidiag:{run_id}",
            apply="topos.get_render_diagnostics / explain_diagnostic",
            verify="entries json-safe + 不泄露 traceback",
            undo="n/a (read)"),
    ]
    return tuple(R)


_REGISTRY: tuple[OperationContract, ...] | None = None


def operation_registry() -> tuple[OperationContract, ...]:
    """全域操作契约（首次构建后缓存；顺序稳定）。"""
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = _build_registry()
    return _REGISTRY


def operations_by_domain(domain: str) -> tuple[OperationContract, ...]:
    return tuple(o for o in operation_registry() if o.domain == domain)


def get_operation(operation_id: str) -> OperationContract | None:
    for o in operation_registry():
        if o.operation_id == operation_id:
            return o
    return None


def registry_to_json() -> dict[str, Any]:
    """机器可读矩阵（审计/文档/测试共享同一真相）。"""
    return {
        "protocol": "topos.operation/1.0",
        "required_fields": list(REQUIRED_FIELDS),
        "domains": list(DOMAINS),
        "operations": [o.to_dict() for o in operation_registry()],
    }
