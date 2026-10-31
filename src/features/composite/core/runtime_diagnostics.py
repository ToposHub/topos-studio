"""Runtime diagnostic records shared by render metadata and UI panels.

The graph preflight layer already has a structured issue contract.  Runtime
failures historically arrived as a mixture of free-form strings and lists of
codes, which made it easy for one UI surface to hide useful context.  This
module provides the smaller cross-thread contract used by Scene3D diagnostics;
it intentionally has no Qt, GL, or logging side effects.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import threading
from typing import Iterable, Optional


SEVERITY_ERROR = "error"
SEVERITY_WARNING = "warning"
SEVERITY_INFO = "info"


@dataclass(frozen=True)
class RuntimeDiagnostic:
    """One JSON-safe runtime diagnostic record."""

    code: str
    severity: str
    message: str
    source: str = "render"
    node_id: str = ""
    frame: Optional[int] = None
    suggestion: str = ""

    def to_dict(self) -> dict:
        return {
            "code": str(self.code),
            "severity": str(self.severity),
            "message": str(self.message),
            "source": str(self.source),
            "node_id": str(self.node_id),
            "frame": int(self.frame) if self.frame is not None else None,
            "suggestion": str(self.suggestion),
        }


class DiagnosticBus:
    """Bounded, thread-safe runtime record store.

    The bus is deliberately transport-agnostic.  A render worker may publish
    records, while the UI consumes a detached snapshot later; no Qt object or
    mutable render payload is retained here.
    """

    def __init__(self, max_records: int = 256) -> None:
        self._records: deque[RuntimeDiagnostic] = deque(
            maxlen=max(1, int(max_records)))
        self._lock = threading.Lock()

    def publish(self, record: RuntimeDiagnostic) -> None:
        if not isinstance(record, RuntimeDiagnostic):
            raise TypeError("record must be RuntimeDiagnostic")
        with self._lock:
            self._records.append(record)

    def publish_many(self, records: Iterable[RuntimeDiagnostic]) -> None:
        # AUD-5213: a single critical section — the previous implementation
        # called publish() per record, re-acquiring the lock for each one
        # and letting concurrent readers observe a partially published
        # batch.  Records are validated first, so an invalid batch publishes
        # nothing instead of a partial prefix.
        batch = list(records)
        for record in batch:
            if not isinstance(record, RuntimeDiagnostic):
                raise TypeError("record must be RuntimeDiagnostic")
        with self._lock:
            self._records.extend(batch)

    def snapshot(self) -> tuple[RuntimeDiagnostic, ...]:
        with self._lock:
            return tuple(self._records)

    def clear(self) -> None:
        with self._lock:
            self._records.clear()


def _code_from_message(message: str, fallback: str) -> str:
    candidate = str(message or "").split(":", 1)[0].strip()
    return candidate if "." in candidate else fallback


def records_from_scene_diagnostics(
        diagnostics: object, *, frame: Optional[int] = None
        ) -> tuple[RuntimeDiagnostic, ...]:
    """Normalize current Scene3D model fields into stable records.

    The input is intentionally the existing render-diagnostics dictionary, so
    older producers remain compatible.  Duplicate code/node pairs are removed
    while preserving first-seen order for deterministic UI and tests.
    """
    if not isinstance(diagnostics, dict):
        return ()
    node_id = str(diagnostics.get("model_node_id") or "")
    result: list[RuntimeDiagnostic] = []
    seen: set[tuple[str, str]] = set()

    def add(record: RuntimeDiagnostic) -> None:
        key = (record.code, record.node_id)
        if key not in seen:
            seen.add(key)
            result.append(record)

    model_error = str(diagnostics.get("model_error") or "").strip()
    if model_error:
        add(RuntimeDiagnostic(
            code=_code_from_message(model_error, "model.import_failed"),
            severity=SEVERITY_ERROR,
            message=model_error,
            source="model_import",
            node_id=node_id,
            frame=frame,
            suggestion="检查模型路径、格式后端和导入报告。",
        ))

    raw_codes = diagnostics.get("model_diagnostics", ())
    if isinstance(raw_codes, (list, tuple, set)):
        for raw_code in raw_codes:
            code = str(raw_code or "").strip()
            if not code:
                continue
            add(RuntimeDiagnostic(
                code=code,
                severity=SEVERITY_WARNING,
                message=code,
                source="model_render",
                node_id=node_id,
                frame=frame,
                suggestion="查看对应节点的导入/变形设置。",
            ))

    raw_import_codes = diagnostics.get("model_import_diagnostics", ())
    if isinstance(raw_import_codes, (list, tuple, set)):
        for raw_code in raw_import_codes:
            message = str(raw_code or "").strip()
            if not message:
                continue
            add(RuntimeDiagnostic(
                code=_code_from_message(message, "model.import_warning"),
                severity=(SEVERITY_ERROR
                          if diagnostics.get("model_import_status") == "error"
                          else SEVERITY_WARNING),
                message=message,
                source="model_import",
                node_id=node_id,
                frame=frame,
                suggestion="检查导入报告中的格式、路径和资源预算。",
            ))

    probe = diagnostics.get("model_import_probe")
    if isinstance(probe, dict) and not bool(probe.get("can_import", True)):
        reason_code = str(probe.get("reason_code") or "format.unavailable")
        add(RuntimeDiagnostic(
            code=reason_code,
            severity=SEVERITY_ERROR,
            message=str(probe.get("reason") or reason_code),
            source="format_probe",
            node_id=node_id,
            frame=frame,
            suggestion="改用可导入格式，或安装/接入对应 loader 后重试。",
        ))

    estimate = diagnostics.get("model_import_resource_estimate")
    if isinstance(estimate, dict) and bool(estimate.get("over_budget")):
        add(RuntimeDiagnostic(
            code="resource.budget_exceeded",
            severity=SEVERITY_ERROR,
            message=("预计模型导入资源超过当前预算。"),
            source="resource_preflight",
            node_id=node_id,
            frame=frame,
            suggestion="提高资源预算、降低模型复杂度，或使用代理模型。",
        ))

    return tuple(result)


__all__ = [
    "DiagnosticBus",
    "RuntimeDiagnostic",
    "SEVERITY_ERROR",
    "SEVERITY_WARNING",
    "SEVERITY_INFO",
    "records_from_scene_diagnostics",
]
