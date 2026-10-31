"""Stable error codes and sanitized audit events for Motion Graphic AI flows.

This module gives the Motion Graphic pipeline a single, named vocabulary of
outcomes so that:

* the UI never has to parse free-form exception text to decide what to show;
* providers, fallbacks, validators and approvals can be compared by a stable
  ``code`` string instead of by message wording;
* failures always carry a documented *next step* suggestion, so users know how
  to recover (retry / edit brief / pick a template / cancel).

Design rules (MG-23):

1. Codes are SCREAMING_SNAKE_CASE constants, grouped by domain prefix
   (``MOTION_GRAPHIC_*``, ``MOTION_GRAPHIC_LLM_*``, ``MOTION_GRAPHIC_SCENE_*``,
   ``MOTION_GRAPHIC_APPROVAL_*``).  They never include dynamic values.
2. Every :class:`MotionGraphicFailure` carries one code plus a short,
   user-facing suggestion.  It never embeds the raw LLM prompt, an API key, an
   absolute path or a full asset payload.
3. :class:`MotionGraphicAuditEvent` is the sanitized record persisted to the
   assistant session / run diagnostics.  ``redact_for_storage`` strips anything
   that violates the project ``.topos`` payload rules.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
import hashlib
import json
import re
import time
from typing import Any, Mapping


# --------------------------------------------------------------------------- #
# Stable error codes
# --------------------------------------------------------------------------- #
#
# The codes are intentionally grouped so that UI can map prefix → icon / retry
# behaviour without parsing the rest of the string.  When adding a code, also
# document the user-visible next step in :data:`_SUGGESTED_NEXT_STEPS`.

# Generation source / provenance.  MG-05 requires that a model failure can
# never be silently presented as a custom result, so the source is explicit.
MOTION_GRAPHIC_LLM_BUILD_FAILED = "MOTION_GRAPHIC_LLM_BUILD_FAILED"
MOTION_GRAPHIC_LLM_PARSE_FAILED = "MOTION_GRAPHIC_LLM_PARSE_FAILED"
MOTION_GRAPHIC_LLM_TIMEOUT = "MOTION_GRAPHIC_LLM_TIMEOUT"
MOTION_GRAPHIC_LLM_CANCELLED = "MOTION_GRAPHIC_LLM_CANCELLED"
MOTION_GRAPHIC_PROVIDER_UNAVAILABLE = "MOTION_GRAPHIC_PROVIDER_UNAVAILABLE"
MOTION_GRAPHIC_INVALID_SCENE_PLAN = "MOTION_GRAPHIC_INVALID_SCENE_PLAN"
MOTION_GRAPHIC_OPTIONAL_TEMPLATE_FALLBACK = "MOTION_GRAPHIC_OPTIONAL_TEMPLATE_FALLBACK"

# Scene / validation layer.  MG-10 expands the validator; each blocking finding
# surfaces here so the user can fix it before paying for a render.
MOTION_GRAPHIC_SCENE_STALE = "MOTION_GRAPHIC_SCENE_STALE"
MOTION_GRAPHIC_SCENE_INVALID = "MOTION_GRAPHIC_SCENE_INVALID"
MOTION_GRAPHIC_VALIDATION_FAILED = "MOTION_GRAPHIC_VALIDATION_FAILED"
MOTION_GRAPHIC_RENDER_FAILED = "MOTION_GRAPHIC_RENDER_FAILED"
MOTION_GRAPHIC_CANVAS_OUT_OF_BOUNDS = "MOTION_GRAPHIC_CANVAS_OUT_OF_BOUNDS"
MOTION_GRAPHIC_TEXT_OVERFLOW = "MOTION_GRAPHIC_TEXT_OVERFLOW"
MOTION_GRAPHIC_LOW_CONTRAST = "MOTION_GRAPHIC_LOW_CONTRAST"
MOTION_GRAPHIC_DUPLICATE_ELEMENT = "MOTION_GRAPHIC_DUPLICATE_ELEMENT"
MOTION_GRAPHIC_COMPLEXITY_LIMIT = "MOTION_GRAPHIC_COMPLEXITY_LIMIT"

# Timeline insertion / clip references.  MG-06 binds approval to the actual
# write, so a mismatched track / frame / scene hash is a stable failure rather
# than a generic RuntimeError.
MOTION_GRAPHIC_CLIP_INSERT_FAILED = "MOTION_GRAPHIC_CLIP_INSERT_FAILED"
MOTION_GRAPHIC_TIMELINE_NOT_FOUND = "MOTION_GRAPHIC_TIMELINE_NOT_FOUND"
MOTION_GRAPHIC_TRACK_NOT_FOUND = "MOTION_GRAPHIC_TRACK_NOT_FOUND"
MOTION_GRAPHIC_TIMELINE_STALE = "MOTION_GRAPHIC_TIMELINE_STALE"
MOTION_GRAPHIC_FRAME_OUT_OF_RANGE = "MOTION_GRAPHIC_FRAME_OUT_OF_RANGE"

# Approval scope / grant.  These mirror the TAEP ``TAEP_PERMISSION_DENIED``
# family but carry the Motion Graphic specific reason.
MOTION_GRAPHIC_APPROVAL_REQUIRED = "MOTION_GRAPHIC_APPROVAL_REQUIRED"
MOTION_GRAPHIC_APPROVAL_EXPIRED = "MOTION_GRAPHIC_APPROVAL_EXPIRED"
MOTION_GRAPHIC_APPROVAL_STALE = "MOTION_GRAPHIC_APPROVAL_STALE"
MOTION_GRAPHIC_APPROVAL_SCOPE_MISMATCH = "MOTION_GRAPHIC_APPROVAL_SCOPE_MISMATCH"
MOTION_GRAPHIC_PERMISSION_DENIED = "MOTION_GRAPHIC_PERMISSION_DENIED"

# Brief / revision lifecycle (MG-01 / MG-08).  Used when the user edits a
# brief after a proposal was generated or when a revision lineage is broken.
MOTION_GRAPHIC_BRIEF_INCOMPLETE = "MOTION_GRAPHIC_BRIEF_INCOMPLETE"
MOTION_GRAPHIC_BRIEF_STALE = "MOTION_GRAPHIC_BRIEF_STALE"
MOTION_GRAPHIC_REVISION_CONFLICT = "MOTION_GRAPHIC_REVISION_CONFLICT"

# Asset / media provenance (forward-looking; used by MG-14 adapters later).
MOTION_GRAPHIC_ASSET_UNRESOLVED = "MOTION_GRAPHIC_ASSET_UNRESOLVED"
MOTION_GRAPHIC_ASSET_LICENSE_MISSING = "MOTION_GRAPHIC_ASSET_LICENSE_MISSING"


_SUGGESTED_NEXT_STEPS: dict[str, str] = {
    MOTION_GRAPHIC_LLM_BUILD_FAILED: "重试生成、修改需求描述，或选择“改用内置模板”。",
    MOTION_GRAPHIC_LLM_PARSE_FAILED: "重新生成或简化需求；系统不会以样板冒充定制结果。",
    MOTION_GRAPHIC_LLM_TIMEOUT: "检查网络或稍后重试；仍失败可改用模板。",
    MOTION_GRAPHIC_LLM_CANCELLED: "已取消生成；可重新发起或修改需求。",
    MOTION_GRAPHIC_PROVIDER_UNAVAILABLE: "确认 AI 提供方已配置并授权，或改用模板。",
    MOTION_GRAPHIC_INVALID_SCENE_PLAN: "修改需求或换一个方案；可在确认前查看差异。",
    MOTION_GRAPHIC_OPTIONAL_TEMPLATE_FALLBACK: "这是显式内置模板，来源已在卡片标注。",
    MOTION_GRAPHIC_SCENE_STALE: "场景在预览后被修改；请刷新差异后再确认。",
    MOTION_GRAPHIC_SCENE_INVALID: "修正场景问题后重新预览；不会写入不合法场景。",
    MOTION_GRAPHIC_VALIDATION_FAILED: "按报告修正后再确认；阻断项不会进入时间线。",
    MOTION_GRAPHIC_RENDER_FAILED: "简化场景或减少元素后重试预览。",
    MOTION_GRAPHIC_CANVAS_OUT_OF_BOUNDS: "将元素移回安全区后重新预览。",
    MOTION_GRAPHIC_TEXT_OVERFLOW: "缩短文案、调小字号或加大文本框后重试。",
    MOTION_GRAPHIC_LOW_CONTRAST: "调整前景/背景颜色以满足可读性后重试。",
    MOTION_GRAPHIC_DUPLICATE_ELEMENT: "为元素使用唯一 ID 后重试。",
    MOTION_GRAPHIC_COMPLEXITY_LIMIT: "减少元素或动画数量后重试。",
    MOTION_GRAPHIC_CLIP_INSERT_FAILED: "检查时间线/轨道状态后重新确认。",
    MOTION_GRAPHIC_TIMELINE_NOT_FOUND: "打开目标时间线后重新生成。",
    MOTION_GRAPHIC_TRACK_NOT_FOUND: "选择或新建 Graphics 轨道后重试。",
    MOTION_GRAPHIC_TIMELINE_STALE: "时间线已变化；请基于当前状态重新确认。",
    MOTION_GRAPHIC_FRAME_OUT_OF_RANGE: "调整播放头位置后重试。",
    MOTION_GRAPHIC_APPROVAL_REQUIRED: "此操作需要显式确认；不会自动执行。",
    MOTION_GRAPHIC_APPROVAL_EXPIRED: "确认已过期；请重新查看方案并确认。",
    MOTION_GRAPHIC_APPROVAL_STALE: "方案/场景/时间线已变更；旧确认已失效。",
    MOTION_GRAPHIC_APPROVAL_SCOPE_MISMATCH: "确认作用域不匹配；请重新确认当前方案。",
    MOTION_GRAPHIC_PERMISSION_DENIED: "当前策略不允许此操作；可在设置中调整权限。",
    MOTION_GRAPHIC_BRIEF_INCOMPLETE: "补充时长、画幅或插入位置后继续。",
    MOTION_GRAPHIC_BRIEF_STALE: "需求已更新；请基于新版本重新生成方案。",
    MOTION_GRAPHIC_REVISION_CONFLICT: "版本历史冲突；请从最新版本继续修改。",
    MOTION_GRAPHIC_ASSET_UNRESOLVED: "补齐缺失素材或改为内置素材后重试。",
    MOTION_GRAPHIC_ASSET_LICENSE_MISSING: "补充素材许可信息后才能用于导出。",
}


def suggested_next_step(code: str) -> str:
    """Return the user-facing recovery hint for an error code.

    Unknown codes fall back to a generic "contact / retry" message rather than
    an empty string, so the UI always has something to render.
    """

    return _SUGGESTED_NEXT_STEPS.get(
        code,
        "请重试；如问题持续，请查看诊断详情。",
    )


# --------------------------------------------------------------------------- #
# Failure exception
# --------------------------------------------------------------------------- #


class MotionGraphicFailure(Exception):
    """A Motion Graphic pipeline failure carrying a stable ``code``.

    ``code`` is the canonical identifier persisted in diagnostics and shown to
    the UI; ``message`` is the short, non-sensitive detail.  ``details`` may
    carry structured, already-sanitized fields (element ids, counts) but must
    not contain raw prompts, secrets or absolute paths — use
    :func:`redact_for_storage` before persisting anything uncertain.
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details: dict[str, Any] = dict(details or {})

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "next_step": suggested_next_step(self.code),
            "details": _copy_json(self.details),
        }


# --------------------------------------------------------------------------- #
# Sanitization helpers
# --------------------------------------------------------------------------- #
#
# The assistant session store and the run diagnostics both follow the project
# ``.topos`` rule: never persist API keys, absolute paths, raw user prompts or
# large media payloads.  ``redact_for_storage`` enforces that boundary in one
# place so adapters do not have to re-implement it.

_ABSOLUTE_PATH_PATTERN = re.compile(r"(?<![\w./-])(/[^\s\"']*)")
_KEY_LIKE_PATTERN = re.compile(r"(?i)(sk-|api[_-]?key|token|secret|password|bearer)")
_MAX_REDACTED_TEXT = 500
_SENSITIVE_KEY_NAMES = frozenset(
    {
        "api_key", "apikey", "secret", "token", "password", "authorization",
        "access_token", "refresh_token", "credential", "private_key",
        "raw_prompt", "prompt", "system_prompt", "user_prompt",
    }
)


def _copy_json(value: Any) -> Any:
    """Deep copy JSON-compatible values without sharing mutable state."""
    if isinstance(value, Mapping):
        return {str(k): _copy_json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_copy_json(item) for item in value]
    return value


def _redact_text(value: str) -> str:
    """Mask API-key-like substrings and absolute paths inside free-form text.

    The text is also truncated to ``_MAX_REDACTED_TEXT`` characters to keep the
    audit payload bounded; longer prompts are identified by their hash instead.
    """

    if not isinstance(value, str):
        return value
    redacted = _KEY_LIKE_PATTERN.sub("[redacted]", value)
    redacted = _ABSOLUTE_PATH_PATTERN.sub("[path]", redacted)
    if len(redacted) > _MAX_REDACTED_TEXT:
        digest = hashlib.sha256(redacted.encode("utf-8")).hexdigest()[:16]
        redacted = redacted[:_MAX_REDACTED_TEXT] + f"…[truncated:{digest}]"
    return redacted


def redact_for_storage(value: Any) -> Any:
    """Return a sanitized copy of ``value`` suitable for ``.topos`` persistence.

    Rules:

    * Mapping keys matching :data:`_SENSITIVE_KEY_NAMES` are replaced with the
      literal ``"[redacted]"`` (the key name is preserved so the structure is
      debuggable, but the value never lands on disk).
    * Nested strings have API-key-like substrings and absolute paths masked.
    * Non-string scalars are returned as-is.
    """

    if isinstance(value, Mapping):
        sanitized: dict[str, Any] = {}
        for key, item in value.items():
            key_name = str(key).lower().strip()
            if key_name in _SENSITIVE_KEY_NAMES:
                sanitized[str(key)] = "[redacted]"
            else:
                sanitized[str(key)] = redact_for_storage(item)
        return sanitized
    if isinstance(value, (list, tuple)):
        return [redact_for_storage(item) for item in value]
    if isinstance(value, str):
        return _redact_text(value)
    return value


# --------------------------------------------------------------------------- #
# Audit event
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class MotionGraphicAuditEvent:
    """Sanitized, persistable record of one Motion Graphic pipeline step.

    The event is the single source of truth for "what happened" shown in the
    Assistant / Run Drawer.  It deliberately stores *hashes* of the brief /
    plan / preview rather than their full content, so the diagnostics stay
    compact and never leak the raw prompt.
    """

    event_id: str
    code: str
    stage: str
    timestamp: float
    brief_revision: int | None = None
    brief_hash: str | None = None
    plan_hash: str | None = None
    scene_hash: str | None = None
    diff_hash: str | None = None
    preview_hash: str | None = None
    provider: str | None = None
    model_id: str | None = None
    execution_mode: str | None = None
    cost_units: float = 0.0
    duration_seconds: float = 0.0
    policy_decision: str | None = None
    message: str = ""
    details: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "code": self.code,
            "stage": self.stage,
            "timestamp": self.timestamp,
            "brief_revision": self.brief_revision,
            "brief_hash": self.brief_hash,
            "plan_hash": self.plan_hash,
            "scene_hash": self.scene_hash,
            "diff_hash": self.diff_hash,
            "preview_hash": self.preview_hash,
            "provider": self.provider,
            "model_id": self.model_id,
            "execution_mode": self.execution_mode,
            "cost_units": float(self.cost_units),
            "duration_seconds": float(self.duration_seconds),
            "policy_decision": self.policy_decision,
            "message": self.message,
            "details": _copy_json(self.details),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "MotionGraphicAuditEvent":
        # Numeric fields converge through the same `or`-guarded coercion so a
        # polluted/persisted None cannot escape as TypeError (AUD-26812);
        # brief_revision is narrowed to the declared `int | None`.
        brief_revision = data.get("brief_revision")
        return cls(
            event_id=str(data.get("event_id", "")),
            code=str(data.get("code", "")),
            stage=str(data.get("stage", "")),
            timestamp=float(data.get("timestamp", 0.0) or 0.0),
            brief_revision=(
                int(brief_revision) if brief_revision is not None else None
            ),
            plan_hash=data.get("plan_hash"),
            scene_hash=data.get("scene_hash"),
            diff_hash=data.get("diff_hash"),
            preview_hash=data.get("preview_hash"),
            brief_hash=data.get("brief_hash"),
            provider=data.get("provider"),
            model_id=data.get("model_id"),
            execution_mode=data.get("execution_mode"),
            cost_units=float(data.get("cost_units", 0.0) or 0.0),
            duration_seconds=float(data.get("duration_seconds", 0.0) or 0.0),
            policy_decision=data.get("policy_decision"),
            message=str(data.get("message", "")),
            details=_copy_json(data.get("details", {})),
        )

    def sanitized(self) -> "MotionGraphicAuditEvent":
        """Return a copy with all free-form fields run through redaction."""
        return replace(
            self,
            message=_redact_text(self.message),
            details=redact_for_storage(self.details),
        )


def make_audit_event(
    code: str,
    *,
    stage: str,
    timestamp: float | None = None,
    **fields: Any,
) -> MotionGraphicAuditEvent:
    """Build a sanitized audit event with a generated id and clock default."""

    import uuid

    event = MotionGraphicAuditEvent(
        event_id=f"mg-audit-{uuid.uuid4().hex}",
        code=code,
        stage=stage,
        timestamp=timestamp if timestamp is not None else time.time(),
        **fields,
    )
    return event.sanitized()


def canonical_json_hash(payload: Any) -> str:
    """Stable sha256 hex for a JSON-serializable payload.

    Used by brief / plan / preview adapters so that an audit event can refer to
    "the brief the user signed off on" without persisting the brief itself.
    """

    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


__all__ = [
    "MotionGraphicFailure",
    "MotionGraphicAuditEvent",
    "redact_for_storage",
    "suggested_next_step",
    "canonical_json_hash",
    "make_audit_event",
    # generation / provenance
    "MOTION_GRAPHIC_LLM_BUILD_FAILED",
    "MOTION_GRAPHIC_LLM_PARSE_FAILED",
    "MOTION_GRAPHIC_LLM_TIMEOUT",
    "MOTION_GRAPHIC_LLM_CANCELLED",
    "MOTION_GRAPHIC_PROVIDER_UNAVAILABLE",
    "MOTION_GRAPHIC_INVALID_SCENE_PLAN",
    "MOTION_GRAPHIC_OPTIONAL_TEMPLATE_FALLBACK",
    # scene / validation
    "MOTION_GRAPHIC_SCENE_STALE",
    "MOTION_GRAPHIC_SCENE_INVALID",
    "MOTION_GRAPHIC_VALIDATION_FAILED",
    "MOTION_GRAPHIC_RENDER_FAILED",
    "MOTION_GRAPHIC_CANVAS_OUT_OF_BOUNDS",
    "MOTION_GRAPHIC_TEXT_OVERFLOW",
    "MOTION_GRAPHIC_LOW_CONTRAST",
    "MOTION_GRAPHIC_DUPLICATE_ELEMENT",
    "MOTION_GRAPHIC_COMPLEXITY_LIMIT",
    # timeline / clip
    "MOTION_GRAPHIC_CLIP_INSERT_FAILED",
    "MOTION_GRAPHIC_TIMELINE_NOT_FOUND",
    "MOTION_GRAPHIC_TRACK_NOT_FOUND",
    "MOTION_GRAPHIC_TIMELINE_STALE",
    "MOTION_GRAPHIC_FRAME_OUT_OF_RANGE",
    # approval
    "MOTION_GRAPHIC_APPROVAL_REQUIRED",
    "MOTION_GRAPHIC_APPROVAL_EXPIRED",
    "MOTION_GRAPHIC_APPROVAL_STALE",
    "MOTION_GRAPHIC_APPROVAL_SCOPE_MISMATCH",
    "MOTION_GRAPHIC_PERMISSION_DENIED",
    # brief / revision
    "MOTION_GRAPHIC_BRIEF_INCOMPLETE",
    "MOTION_GRAPHIC_BRIEF_STALE",
    "MOTION_GRAPHIC_REVISION_CONFLICT",
    # assets
    "MOTION_GRAPHIC_ASSET_UNRESOLVED",
    "MOTION_GRAPHIC_ASSET_LICENSE_MISSING",
]
