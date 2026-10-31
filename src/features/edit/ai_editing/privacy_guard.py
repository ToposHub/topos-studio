"""Prompt/upload privacy guard used by model transports and audit adapters."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping


_SECRET_PATTERNS = (
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"\b(?:api[_-]?key|token|secret|password)\s*[:=]\s*[^\s,;]+", re.I),
)
_ABSOLUTE_PATH = re.compile(r"(?<![A-Za-z0-9])/(?:Users|home|private|tmp)/[^\s,;]+")


class PrivacyViolation(ValueError):
    """A caller attempted an upload or retention action without consent."""


@dataclass(frozen=True)
class UploadDecision:
    allowed: bool
    reason: str
    asset_ids: tuple[str, ...] = ()


def redact_text(text: str) -> str:
    """Redact common secrets and machine paths without storing raw prompt text."""
    value = str(text)
    for pattern in _SECRET_PATTERNS:
        value = pattern.sub(lambda match: f"{match.group(1) if match.lastindex else ''}[REDACTED]", value)
    return _ABSOLUTE_PATH.sub("[PATH_REDACTED]", value)


def redact_mapping(value: Mapping[str, Any]) -> dict[str, Any]:
    """Return a shallow redacted audit payload; nested mappings are handled too."""
    result: dict[str, Any] = {}
    secret_keys = {"api_key", "apikey", "token", "authorization", "secret", "password"}
    for key, item in value.items():
        if str(key).lower() in secret_keys:
            result[str(key)] = "[REDACTED]"
        elif isinstance(item, Mapping):
            result[str(key)] = redact_mapping(item)
        elif isinstance(item, str):
            result[str(key)] = redact_text(item)
        else:
            result[str(key)] = item
    return result


def check_media_upload(
    *,
    profile: Any,
    asset_ids: tuple[str, ...],
    user_confirmed: bool,
) -> UploadDecision:
    """Apply modality + profile policy + per-request consent as one gate."""
    if not asset_ids:
        return UploadDecision(False, "没有可上传的素材", ())
    if not user_confirmed:
        return UploadDecision(False, "素材上传需要用户明确确认", asset_ids)
    if not bool(getattr(profile, "allows_media_upload", False)):
        return UploadDecision(False, "当前 profile 未允许 media_upload", asset_ids)
    return UploadDecision(True, "允许上传", asset_ids)


__all__ = [
    "PrivacyViolation",
    "UploadDecision",
    "check_media_upload",
    "redact_mapping",
    "redact_text",
]
