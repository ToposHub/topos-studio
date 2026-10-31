"""M12/B5 发布证据：脱敏遥测、灰度 cohort、无障碍与回滚门。"""

from __future__ import annotations

import hashlib
import re
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Mapping

from src.features.edit.ai_editing.kill_switch import Capability, KillSwitch
from src.features.edit.ai_editing.privacy_guard import redact_text


_BLOCKED_METADATA_KEYS = frozenset({
    "prompt", "raw_prompt", "content", "source_path", "absolute_path", "path",
    "secret", "token", "api_key", "credential", "media_bytes",
})
_ABSOLUTE_PATH = re.compile(r"^(?:/|[A-Za-z]:[\\/]|~[\\/])")


def _safe_metadata(metadata: Mapping[str, Any]) -> dict[str, Any]:
    """只保留低基数、非内容 telemetry；未知敏感键丢弃。

    AUD-14205：字符串值先复用同包 ``privacy_guard.redact_text`` 做值级脱敏
    ——此前只做 key 黑名单 + 值前缀路径过滤，值中段包含的 secret/绝对路径
    原样进入 TelemetryEvent 并随 ``export()`` 导出。
    """

    safe: dict[str, Any] = {}
    for raw_key, value in metadata.items():
        key = str(raw_key).strip().lower()
        if not key or key in _BLOCKED_METADATA_KEYS or any(
            blocked in key for blocked in ("prompt", "secret", "token", "path", "content")
        ):
            continue
        if isinstance(value, (str, int, float, bool)) or value is None:
            if isinstance(value, str):
                if _ABSOLUTE_PATH.match(value):
                    continue
                safe[key[:64]] = redact_text(value)[:200]
            else:
                safe[key[:64]] = value
    return safe


@dataclass(frozen=True)
class TelemetryEvent:
    """单条 opt-in telemetry；不含用户标识、prompt、素材或路径。"""

    name: str
    status: str = ""
    channel: str = ""
    duration_ms: int = 0
    error_code: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata", _safe_metadata(self.metadata))

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name[:80], "status": self.status[:40],
            "channel": self.channel[:40], "duration_ms": max(0, int(self.duration_ms)),
            "error_code": self.error_code[:100], "metadata": dict(self.metadata),
            "timestamp": self.timestamp,
        }


class TelemetryCollector:
    """默认关闭的、可删除的 telemetry collector。

    AUD-14206/14207：事件队列用 ``deque(maxlen=...)``（饱和后 O(1) 淘汰最旧，
    取代 ``list.pop(0)`` 的 O(n) 搬移），并持有一把 ``threading.Lock`` 保护
    ``_opt_in`` / ``_events``——``set_opt_in`` 的清空、``emit`` 的 append、
    ``export`` 的迭代不再有并发竞态（"RuntimeError: deque mutated during
    iteration" 一类）。
    """

    def __init__(self, *, opt_in: bool = False, max_events: int = 1000) -> None:
        self._opt_in = bool(opt_in)
        self._max_events = max(1, int(max_events))
        self._events: deque[TelemetryEvent] = deque(maxlen=self._max_events)
        self._lock = threading.Lock()

    @property
    def opted_in(self) -> bool:
        with self._lock:
            return self._opt_in

    def set_opt_in(self, enabled: bool) -> None:
        with self._lock:
            self._opt_in = bool(enabled)
            if not self._opt_in:
                self._events.clear()

    def emit(
        self,
        name: str,
        *,
        status: str = "",
        channel: str = "",
        duration_ms: int = 0,
        error_code: str = "",
        metadata: Mapping[str, Any] | None = None,
    ) -> bool:
        with self._lock:
            if not self._opt_in:
                return False
            # deque(maxlen) 满时自动淘汰最旧一条（O(1)），无需手工 pop(0)。
            self._events.append(TelemetryEvent(
                name=name, status=status, channel=channel, duration_ms=duration_ms,
                error_code=error_code, metadata=metadata or {},
            ))
            return True

    def events(self) -> tuple[TelemetryEvent, ...]:
        with self._lock:
            return tuple(self._events)

    def export(self) -> dict[str, Any]:
        with self._lock:
            return {
                "opted_in": self._opt_in,
                "events": [event.to_dict() for event in self._events],
            }

    def clear(self) -> None:
        with self._lock:
            self._events.clear()


@dataclass(frozen=True)
class GrayRolloutConfig:
    capability: Capability
    channel: str = "alpha"
    percentage: float = 0.0
    salt: str = "topos-gray-v1"

    def __post_init__(self) -> None:
        if not 0.0 <= self.percentage <= 100.0:
            raise ValueError("gray rollout percentage must be between 0 and 100")
        if self.channel not in {"internal", "alpha", "beta", "stable"}:
            raise ValueError("unsupported rollout channel")

    def enabled_for(self, cohort_key: str, *, kill_switch: KillSwitch) -> bool:
        """确定性 cohort 分桶；只在当前 capability 未被 kill 时生效。"""
        if not cohort_key or not kill_switch.is_enabled(self.capability):
            return False
        if self.percentage >= 100.0:
            return True
        bucket = int.from_bytes(
            hashlib.sha256(f"{self.salt}:{cohort_key}".encode("utf-8")).digest()[:8],
            "big",
        ) % 10_000
        return bucket < int(self.percentage * 100)

    def to_dict(self) -> dict[str, Any]:
        return {
            "capability": self.capability.value,
            "channel": self.channel,
            "percentage": self.percentage,
        }


@dataclass(frozen=True)
class AccessibilityEvidence:
    keyboard_navigation: bool = False
    screen_reader_labels: bool = False
    contrast_checked: bool = False
    focus_order_checked: bool = False
    notes: tuple[str, ...] = ()

    @property
    def passed(self) -> bool:
        return all((self.keyboard_navigation, self.screen_reader_labels,
                    self.contrast_checked, self.focus_order_checked))

    def to_dict(self) -> dict[str, Any]:
        return {
            "keyboard_navigation": self.keyboard_navigation,
            "screen_reader_labels": self.screen_reader_labels,
            "contrast_checked": self.contrast_checked,
            "focus_order_checked": self.focus_order_checked,
            "notes": list(self.notes), "passed": self.passed,
        }


@dataclass(frozen=True)
class ReleaseEvidenceBundle:
    """B5 发布门的最小证据集合；缺任一硬项即 fail closed。"""

    version: str
    channel: str
    eval_report_id: str = ""
    eval_passed: bool = False
    eval_corpus_version: str = ""
    rubric_report_id: str = ""
    rubric_passed: bool = False
    performance_report_id: str = ""
    security_report_id: str = ""
    privacy_audit_passed: bool = False
    kill_switch_verified: bool = False
    gray_rollout: GrayRolloutConfig | None = None
    telemetry_opt_in_documented: bool = False
    accessibility: AccessibilityEvidence = field(default_factory=AccessibilityEvidence)
    provider_smoke_passed: bool = False
    provider_cost_report_id: str = ""
    cancellation_recovery_passed: bool = False
    rollback_runbook_ref: str = ""
    known_limitations: tuple[str, ...] = ()

    @property
    def blockers(self) -> tuple[str, ...]:
        blockers: list[str] = []
        for condition, reason in (
            (bool(self.version), "version_missing"),
            (bool(self.eval_report_id) and self.eval_passed, "eval_missing_or_failed"),
            (bool(self.eval_corpus_version), "eval_corpus_version_missing"),
            (bool(self.rubric_report_id) and self.rubric_passed, "rubric_missing_or_failed"),
            (bool(self.performance_report_id), "performance_report_missing"),
            (bool(self.security_report_id) and self.privacy_audit_passed, "security_or_privacy_failed"),
            (self.kill_switch_verified, "kill_switch_not_verified"),
            (self.telemetry_opt_in_documented, "telemetry_opt_in_not_documented"),
            (self.gray_rollout is not None, "gray_rollout_missing"),
            (self.accessibility.passed, "accessibility_check_failed"),
            (self.provider_smoke_passed and bool(self.provider_cost_report_id), "provider_evidence_missing"),
            (self.cancellation_recovery_passed, "cancellation_recovery_missing"),
            (bool(self.rollback_runbook_ref), "rollback_runbook_missing"),
        ):
            if not condition:
                blockers.append(reason)
        return tuple(blockers)

    @property
    def releasable(self) -> bool:
        return not self.blockers

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version, "channel": self.channel,
            "eval_report_id": self.eval_report_id, "eval_passed": self.eval_passed,
            "eval_corpus_version": self.eval_corpus_version,
            "rubric_report_id": self.rubric_report_id, "rubric_passed": self.rubric_passed,
            "performance_report_id": self.performance_report_id,
            "security_report_id": self.security_report_id,
            "privacy_audit_passed": self.privacy_audit_passed,
            "kill_switch_verified": self.kill_switch_verified,
            "gray_rollout": self.gray_rollout.to_dict() if self.gray_rollout else None,
            "telemetry_opt_in_documented": self.telemetry_opt_in_documented,
            "accessibility": self.accessibility.to_dict(),
            "provider_smoke_passed": self.provider_smoke_passed,
            "provider_cost_report_id": self.provider_cost_report_id,
            "cancellation_recovery_passed": self.cancellation_recovery_passed,
            "rollback_runbook_ref": self.rollback_runbook_ref,
            "known_limitations": list(self.known_limitations),
            "blockers": list(self.blockers), "releasable": self.releasable,
        }


__all__ = [
    "TelemetryEvent", "TelemetryCollector", "GrayRolloutConfig",
    "AccessibilityEvidence", "ReleaseEvidenceBundle",
]
