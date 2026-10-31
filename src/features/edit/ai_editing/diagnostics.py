"""TAEP 诊断码与消息模板（Phase 1）。

诊断码一经发布即**稳定**（TAEP §5.8），外部客户端可用它们做分支与本地化。
本模块只定义枚举、严重级与中英文文案模板；具体诊断由 ``plan_validator`` /
``transaction_service`` / adapter 产生。

设计要点：

- 诊断码是**机器可读**的稳定字符串（``TAEP_*``），不是异常类型。
- ``Diagnostic`` 同时携带机器字段（``code`` / ``op_id`` / ``path``）和人类可读
  ``message`` / ``suggested_action``，便于 UI 直接展示与本地化。
- 严重级 ``error`` 阻止提交/导出；``warning`` 提示但不阻塞；``info`` 仅记录。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class DiagnosticSeverity(str, Enum):
    """诊断严重级。``error`` 阻止提交；``warning`` 提示；``info`` 记录。"""

    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


class TaepDiagnosticCode(str, Enum):
    """TAEP 稳定诊断码（TAEP §5.8）。

    一经发布不可改名/复用；新增码追加到末尾。外部 LLM/MCP 客户端可硬编码引用。
    """

    # Schema / protocol
    SCHEMA_INVALID = "TAEP_SCHEMA_INVALID"
    UNSUPPORTED_OPERATION = "TAEP_UNSUPPORTED_OPERATION"
    PERMISSION_DENIED = "TAEP_PERMISSION_DENIED"
    PROTOCOL_VERSION_MISMATCH = "TAEP_PROTOCOL_VERSION_MISMATCH"

    # Revision / identity
    REVISION_CONFLICT = "TAEP_REVISION_CONFLICT"
    TIMELINE_NOT_FOUND = "TAEP_TIMELINE_NOT_FOUND"
    TRACK_NOT_FOUND = "TAEP_TRACK_NOT_FOUND"
    PROJECT_NOT_FOUND = "TAEP_PROJECT_NOT_FOUND"

    # Asset / range
    ASSET_OFFLINE = "TAEP_ASSET_OFFLINE"
    ASSET_NOT_FOUND = "TAEP_ASSET_NOT_FOUND"
    SOURCE_RANGE_INVALID = "TAEP_SOURCE_RANGE_INVALID"

    # Timeline semantics
    TRACK_OVERLAP = "TAEP_TRACK_OVERLAP"
    TRACK_TYPE_MISMATCH = "TAEP_TRACK_TYPE_MISMATCH"
    TRANSITION_INVALID = "TAEP_TRANSITION_INVALID"
    SUBTITLE_LOCKED = "TAEP_SUBTITLE_LOCKED"

    # Effect / capability
    EFFECT_INVALID = "TAEP_EFFECT_INVALID"
    EFFECT_PARAMETER_INVALID = "TAEP_EFFECT_PARAMETER_INVALID"
    # T-P2-22：preview_only/unsupported 效果不得作为交付步骤进入计划
    EFFECT_NOT_DELIVERABLE = "TAEP_EFFECT_NOT_DELIVERABLE"

    # Export / interchange
    EXPORT_PREFLIGHT_FAILED = "TAEP_EXPORT_PREFLIGHT_FAILED"
    INTERCHANGE_LOSSY = "TAEP_INTERCHANGE_LOSSY"

    # Plan-level
    PLAN_TOO_LARGE = "TAEP_PLAN_TOO_LARGE"
    QUOTA_EXCEEDED = "TAEP_QUOTA_EXCEEDED"

    # Quality gate（M12 / §11.2 M12 / §12.2：安全硬门槛）
    QUALITY_GATE_FAILED = "TAEP_QUALITY_GATE_FAILED"

    # 运行时（可重试的内部失败；不泄露 traceback/绝对路径）
    INTERNAL_ERROR = "TAEP_INTERNAL_ERROR"


@dataclass(frozen=True)
class Diagnostic:
    """单条诊断。

    Attributes:
        severity: 严重级。
        code: 稳定诊断码（``TaepDiagnosticCode`` 值）。
        message: 人类可读消息（默认中文，可被 i18n 替换）。
        op_id: 关联的 operation id（``None`` 表示计划级问题）。
        path: JSON Pointer 路径，定位到出错的字段（如 ``/operations/2/source``）。
        suggested_action: 建议的下一步动作（机器可读，如 ``refresh_snapshot``）。
        details: 额外机器可读细节（不放入 message，供调试/审计）。
    """

    severity: DiagnosticSeverity
    code: TaepDiagnosticCode
    message: str
    op_id: str | None = None
    path: str | None = None
    suggested_action: str | None = None
    details: dict[str, Any] | None = None

    @property
    def is_error(self) -> bool:
        return self.severity == DiagnosticSeverity.ERROR

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            # from_dict 对未知枚举值回落到原始字符串（见下），序列化时同样
            # 兼容原始字符串（AUD-13400）。
            "severity": getattr(self.severity, "value", self.severity),
            "code": getattr(self.code, "value", self.code),
            "message": self.message,
        }
        if self.op_id is not None:
            d["op_id"] = self.op_id
        if self.path is not None:
            d["path"] = self.path
        if self.suggested_action is not None:
            d["suggested_action"] = self.suggested_action
        if self.details:
            d["details"] = self.details
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Diagnostic":
        """从 ``to_dict`` 输出重建（工程重开后回看历史诊断用）。

        ``severity``/``code`` 用枚举值字符串安全重建；未知值回落到原始字符串
        （``DiagnosticSeverity``/``TaepDiagnosticCode`` 均为 ``str`` 枚举，兼容）。
        """
        sev_raw = data.get("severity")
        code_raw = data.get("code")
        details = dict(data["details"]) if data.get("details") else None
        try:
            severity: DiagnosticSeverity | str = DiagnosticSeverity(sev_raw)
        except ValueError:
            # 契约（上方 docstring）：未知值回落到原始字符串——两个枚举均为
            # ``str`` 枚举，运行时兼容；原始值同时写入 details 便于定位，
            # 不再静默降级为 INFO（否则原本 error 级的诊断会丢失且 is_error
            # 变 False，重载后的历史诊断可能解除提交/导出门控）（AUD-13400）。
            if isinstance(sev_raw, str) and sev_raw:
                severity = sev_raw
                details = dict(details or {})
                details["severity_raw"] = sev_raw
            else:
                severity = DiagnosticSeverity.INFO
        try:
            code: TaepDiagnosticCode | str = TaepDiagnosticCode(code_raw)
        except ValueError:
            if isinstance(code_raw, str) and code_raw:
                code = code_raw
                details = dict(details or {})
                details["code_raw"] = code_raw
            else:
                code = TaepDiagnosticCode.INTERNAL_ERROR
        return cls(
            severity=severity,  # type: ignore[arg-type]
            code=code,  # type: ignore[arg-type]
            message=str(data.get("message", "")),
            op_id=data.get("op_id"),
            path=data.get("path"),
            suggested_action=data.get("suggested_action"),
            details=details,
        )


# ---------------------------------------------------------------------- #
# 消息模板（中英文）。validator 只填入变量，文案集中在此便于审校与本地化。
# ---------------------------------------------------------------------- #

MESSAGE_TEMPLATES: dict[TaepDiagnosticCode, dict[str, str]] = {
    TaepDiagnosticCode.SCHEMA_INVALID: {
        "zh": "计划不符合 TAEP JSON Schema：{detail}",
        "en": "Plan does not conform to the TAEP JSON Schema: {detail}",
    },
    TaepDiagnosticCode.UNSUPPORTED_OPERATION: {
        "zh": "操作类型 {op_kind!r} 不在当前 capability manifest 支持列表内",
        "en": "Operation kind {op_kind!r} is not in the supported list of the current capability manifest",
    },
    TaepDiagnosticCode.PERMISSION_DENIED: {
        "zh": "当前会话无权限执行该操作（{reason}）",
        "en": "The current session is not permitted to perform this operation ({reason})",
    },
    TaepDiagnosticCode.PROTOCOL_VERSION_MISMATCH: {
        "zh": "协议版本不匹配：期望 {expected}，收到 {received}",
        "en": "Protocol version mismatch: expected {expected}, got {received}",
    },
    TaepDiagnosticCode.REVISION_CONFLICT: {
        "zh": "项目已被修改（base={base}，current={current}）；请重新读取快照后再生成计划",
        "en": "Project was modified (base={base}, current={current}); refresh the snapshot and regenerate the plan",
    },
    TaepDiagnosticCode.TIMELINE_NOT_FOUND: {
        "zh": "找不到时间线 {timeline_id!r}",
        "en": "Timeline not found: {timeline_id!r}",
    },
    TaepDiagnosticCode.TRACK_NOT_FOUND: {
        "zh": "在时间线中找不到轨道 {track_id!r}",
        "en": "Track not found in timeline: {track_id!r}",
    },
    TaepDiagnosticCode.PROJECT_NOT_FOUND: {
        "zh": "找不到项目 {project_id!r}",
        "en": "Project not found: {project_id!r}",
    },
    TaepDiagnosticCode.ASSET_OFFLINE: {
        "zh": "素材 {asset_id!r} 处于离线状态，无法探帧/预览/导出",
        "en": "Asset {asset_id!r} is offline; cannot probe/preview/export",
    },
    TaepDiagnosticCode.ASSET_NOT_FOUND: {
        "zh": "找不到素材 {asset_id!r}",
        "en": "Asset not found: {asset_id!r}",
    },
    TaepDiagnosticCode.SOURCE_RANGE_INVALID: {
        "zh": "源范围无效：素材 {asset_id!r} 时长 {duration} 帧，请求范围 [{start}, {start}+{dur}) 越界",
        "en": "Invalid source range: asset {asset_id!r} has {duration} frames, requested [{start}, {start}+{dur}) is out of bounds",
    },
    TaepDiagnosticCode.TRACK_OVERLAP: {
        "zh": "轨道 {track_id!r} 上 clips 重叠（{detail}）",
        "en": "Clips overlap on track {track_id!r} ({detail})",
    },
    TaepDiagnosticCode.TRACK_TYPE_MISMATCH: {
        "zh": "轨道 {track_id!r} 类型为 {actual}，不能放置 {expected} 类型的 clip",
        "en": "Track {track_id!r} is of type {actual}; cannot place a {expected} clip",
    },
    TaepDiagnosticCode.TRANSITION_INVALID: {
        "zh": "转场无效：{detail}",
        "en": "Transition invalid: {detail}",
    },
    TaepDiagnosticCode.SUBTITLE_LOCKED: {
        "zh": "字幕轨/字幕片段已锁定，无法修改",
        "en": "Subtitle track/clip is locked and cannot be modified",
    },
    TaepDiagnosticCode.EFFECT_INVALID: {
        "zh": "效果 {effect_type!r} 不在 registry 中",
        "en": "Effect {effect_type!r} is not in the registry",
    },
    TaepDiagnosticCode.EFFECT_PARAMETER_INVALID: {
        "zh": "效果参数无效：{param}={value} 不在允许范围 [{min}, {max}]",
        "en": "Invalid effect parameter: {param}={value} outside [{min}, {max}]",
    },
    TaepDiagnosticCode.EXPORT_PREFLIGHT_FAILED: {
        "zh": "导出预检失败：{detail}",
        "en": "Export preflight failed: {detail}",
    },
    TaepDiagnosticCode.INTERCHANGE_LOSSY: {
        "zh": "交换格式存在信息损失：{detail}",
        "en": "Interchange is lossy: {detail}",
    },
    TaepDiagnosticCode.PLAN_TOO_LARGE: {
        "zh": "计划过大：{count} 个操作超过上限 {limit}",
        "en": "Plan too large: {count} operations exceed the limit {limit}",
    },
    TaepDiagnosticCode.QUOTA_EXCEEDED: {
        "zh": "超出配额：{detail}",
        "en": "Quota exceeded: {detail}",
    },
    TaepDiagnosticCode.QUALITY_GATE_FAILED: {
        "zh": "质量门未通过：{detail}",
        "en": "Quality gate failed: {detail}",
    },
}


def format_message(
    code: TaepDiagnosticCode, lang: str = "zh", **kwargs: Any
) -> str:
    """按诊断码与语言生成消息（变量通过 kwargs 填入模板）。"""
    templates = MESSAGE_TEMPLATES.get(code, {})
    template = templates.get(lang, templates.get("zh", code.value))
    try:
        return template.format(**kwargs)
    except (KeyError, IndexError):
        return template


def make_diagnostic(
    severity: DiagnosticSeverity,
    code: TaepDiagnosticCode,
    *,
    op_id: str | None = None,
    path: str | None = None,
    suggested_action: str | None = None,
    lang: str = "zh",
    details: dict[str, Any] | None = None,
    **message_vars: Any,
) -> Diagnostic:
    """便捷构造：自动从模板生成 message。"""
    return Diagnostic(
        severity=severity,
        code=code,
        message=format_message(code, lang=lang, **message_vars),
        op_id=op_id,
        path=path,
        suggested_action=suggested_action,
        details=dict(details) if details else None,
    )
