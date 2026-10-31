"""Provider-safe resume: request fingerprint + reconcile contract (OS-09).

OS-09 requires that after a restart, the host can *prove* what has already
happened with external providers rather than blindly re-sending network
calls (which double-charges, double-inserts, or produces inconsistent
state).  The audit found that checkpoints exist at job / agent / goal layers
but lack a normalized ``request_fingerprint`` for provider-level idempotency
and a cross-provider reconcile flow.

This module defines the **resume contract**:
* :class:`ProviderRequestFingerprint` — stable hash of one provider call;
* :class:`ExternalJobRecord` — what the host knows about a remote job;
* :class:`ReconcileDecision` — what to do on restart (resume / skip / ask);
* :meth:`reconcile_inflight` — the orchestrator that inspects all in-flight
  external jobs and produces one decision per job.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import datetime as _datetime
import hashlib
import json
import os
from typing import Any, Iterable, Mapping


# --------------------------------------------------------------------------- #
# Reconcile decisions
# --------------------------------------------------------------------------- #

RECONCILE_RESUME = "resume"           # safe to continue; provider confirms progress
RECONCILE_SKIP = "skip"               # already completed; do not re-call
RECONCILE_ASK_USER = "ask_user"       # cannot prove state; request human input
RECONCILE_RETRY = "retry"             # provider lost the job; safe to re-send
RECONCILE_FAIL = "fail"               # unrecoverable; mark as failed

ALL_RECONCILE_DECISIONS: frozenset[str] = frozenset({
    RECONCILE_RESUME, RECONCILE_SKIP, RECONCILE_ASK_USER, RECONCILE_RETRY, RECONCILE_FAIL,
})


# --------------------------------------------------------------------------- #
# Fingerprint
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ProviderRequestFingerprint:
    """Stable identity for one provider call so it can be deduped on restart.

    The fingerprint covers everything that determines the provider's output:
    provider id, model id, input hash, parameters.  It excludes timestamps
    and transient ids so the same logical request always produces the same
    fingerprint.
    """

    provider_id: str
    model_id: str
    input_hash: str           # sha256 of the canonical input payload
    parameters_hash: str      # sha256 of the parameters dict

    @property
    def fingerprint(self) -> str:
        payload = {
            "provider_id": self.provider_id,
            "model_id": self.model_id,
            "input_hash": self.input_hash,
            "parameters_hash": self.parameters_hash,
        }
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "model_id": self.model_id,
            "input_hash": self.input_hash,
            "parameters_hash": self.parameters_hash,
            "fingerprint": self.fingerprint,
        }


def _canonicalize(value: Any) -> Any:
    """Recursively normalize *value* into a JSON-native, order-stable shape.

    AUD-14202：``json.dumps(..., default=str)`` 会把非 JSON 原生类型按
    ``repr`` 序列化——无自定义 ``__repr__`` 的对象带内存地址、set/dict 视图
    的迭代顺序随 PYTHONHASHSEED 变化，重启后同一逻辑请求算出不同
    fingerprint，破坏 OS-09 幂等去重。这里显式规范化：

    - set / frozenset → 按元素规范化 JSON 排序的 list（顺序稳定）；
    - tuple → list；
    - os.PathLike → 规范字符串路径；
    - datetime / date → ISO-8601 字符串；
    - 其余不可 JSON 化的类型 → ``ValueError``（宁可显式失败也不静默 repr）。
    """

    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return value
    if isinstance(value, Mapping):
        return {str(key): _canonicalize(item) for key, item in value.items()}
    if isinstance(value, (set, frozenset)):
        normalized = [_canonicalize(item) for item in value]
        return sorted(
            normalized, key=lambda item: json.dumps(item, sort_keys=True, ensure_ascii=False)
        )
    if isinstance(value, (list, tuple)):
        return [_canonicalize(item) for item in value]
    if isinstance(value, os.PathLike):
        # W07 Windows：Path 的 fspath 是原生分隔符（\），而等价的
        # str 输入可能是 / 风格——同一路径两种写法必须同指纹。统一
        # 规范化为 / 风格（每平台内稳定，Path 与 str 输入一致）。
        return _canonicalize(os.fspath(value).replace(os.sep, '/'))
    if isinstance(value, (_datetime.datetime, _datetime.date)):
        return value.isoformat()
    raise ValueError(
        f"make_fingerprint 输入包含不可 JSON 规范化的类型 "
        f"{type(value).__name__!r}，拒绝为其生成不稳定指纹"
    )


def _canonical_payload_hash(payload: Mapping[str, Any]) -> str:
    try:
        canonical = json.dumps(
            _canonicalize(payload), sort_keys=True, separators=(",", ":"),
            ensure_ascii=False, allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(f"make_fingerprint 输入无法规范化: {exc}") from exc
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def make_fingerprint(
    provider_id: str,
    model_id: str,
    input_payload: Mapping[str, Any],
    parameters: Mapping[str, Any] | None = None,
) -> ProviderRequestFingerprint:
    """Build a :class:`ProviderRequestFingerprint` from raw payloads.

    输入先经 :func:`_canonicalize` 规范化（set 排序、路径/时间转规范字符串），
    不可 JSON 化的类型显式抛 ``ValueError`` 而不是按 ``repr`` 静默入哈希——
    同一逻辑请求跨进程/跨重启得到稳定指纹（OS-09 幂等去重的前提）。
    """

    if not str(provider_id or "").strip():
        raise ValueError("make_fingerprint requires a non-empty provider_id")
    if not str(model_id or "").strip():
        raise ValueError("make_fingerprint requires a non-empty model_id")
    input_hash = _canonical_payload_hash(input_payload)
    params_hash = _canonical_payload_hash(parameters or {})
    return ProviderRequestFingerprint(
        provider_id=provider_id,
        model_id=model_id,
        input_hash=input_hash,
        parameters_hash=params_hash,
    )


# --------------------------------------------------------------------------- #
# External job record
# --------------------------------------------------------------------------- #


class ExternalJobState(str):
    pass


EXT_PENDING = "pending"               # sent but no result yet
EXT_IN_PROGRESS = "in_progress"       # provider confirmed it's running
EXT_SUCCEEDED = "succeeded"
EXT_FAILED = "failed"
EXT_UNKNOWN = "unknown"               # provider unreachable / status query failed

ALL_EXTERNAL_STATES: frozenset[str] = frozenset({
    EXT_PENDING, EXT_IN_PROGRESS, EXT_SUCCEEDED, EXT_FAILED, EXT_UNKNOWN,
})


@dataclass(frozen=True)
class ExternalJobRecord:
    """What the host knows about one remote provider job (OS-09)."""

    job_id: str
    fingerprint: ProviderRequestFingerprint
    provider_job_ref: str | None = None    # provider's own id for status query
    state: str = EXT_PENDING
    completed_steps: tuple[str, ...] = ()
    artifact_hashes: tuple[str, ...] = ()
    cost_incurred: float = 0.0
    last_checked_at: float = 0.0
    error_code: str = ""
    error_message: str = ""

    def __post_init__(self) -> None:
        if not self.job_id:
            raise ValueError("ExternalJobRecord.job_id is required")
        if self.state not in ALL_EXTERNAL_STATES:
            raise ValueError(f"Unknown external job state: {self.state!r}")

    @property
    def is_terminal(self) -> bool:
        return self.state in {EXT_SUCCEEDED, EXT_FAILED}

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "fingerprint": self.fingerprint.to_dict(),
            "provider_job_ref": self.provider_job_ref,
            "state": self.state,
            "completed_steps": list(self.completed_steps),
            "artifact_hashes": list(self.artifact_hashes),
            "cost_incurred": self.cost_incurred,
            "last_checked_at": self.last_checked_at,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "is_terminal": self.is_terminal,
        }


# --------------------------------------------------------------------------- #
# Reconcile
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ReconcileDecision:
    """One job's restart decision."""

    job_id: str
    decision: str
    reason: str = ""
    artifact_refs_to_reuse: tuple[str, ...] = ()
    details: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.decision not in ALL_RECONCILE_DECISIONS:
            raise ValueError(f"Unknown reconcile decision: {self.decision!r}")


def reconcile_inflight(
    records: Iterable[ExternalJobRecord],
    *,
    known_completed_fingerprints: Iterable[str] = (),
) -> tuple[ReconcileDecision, ...]:
    """Inspect in-flight external jobs on restart and decide per job.

    The host calls this after recovering from a crash / restart.  For each
    in-flight record:

    * if the fingerprint is already in ``known_completed_fingerprints`` → SKIP
      (the artifact already exists locally, do not re-call);
    * if the provider confirmed SUCCEEDED but we have no local artifact →
      RESUME (fetch the artifact from the provider);
    * if the provider says IN_PROGRESS → RESUME (poll until done);
    * if the provider says FAILED → RETRY (safe to re-send because the
      fingerprint is idempotent);
    * if the provider is unreachable (UNKNOWN) → ASK_USER (cannot prove
      state, do not guess).
    """

    completed_set = set(known_completed_fingerprints)
    decisions: list[ReconcileDecision] = []
    for record in records:
        if record.fingerprint.fingerprint in completed_set:
            decisions.append(ReconcileDecision(
                job_id=record.job_id,
                decision=RECONCILE_SKIP,
                reason="该请求的产物已存在，无需重新调用。",
                artifact_refs_to_reuse=record.artifact_hashes,
            ))
            continue
        if record.state == EXT_SUCCEEDED:
            decisions.append(ReconcileDecision(
                job_id=record.job_id,
                decision=RECONCILE_RESUME,
                reason="provider 已完成，但本地缺失产物，需要拉取。",
                artifact_refs_to_reuse=record.artifact_hashes,
            ))
        elif record.state == EXT_IN_PROGRESS:
            decisions.append(ReconcileDecision(
                job_id=record.job_id,
                decision=RECONCILE_RESUME,
                reason="provider 仍在处理，继续轮询。",
            ))
        elif record.state == EXT_FAILED:
            decisions.append(ReconcileDecision(
                job_id=record.job_id,
                decision=RECONCILE_RETRY,
                reason="provider 报告失败，安全重试（幂等指纹）。",
                details={"error_code": record.error_code},
            ))
        elif record.state == EXT_UNKNOWN:
            decisions.append(ReconcileDecision(
                job_id=record.job_id,
                decision=RECONCILE_ASK_USER,
                reason="无法确认 provider 状态，需用户决定是否重试。",
            ))
        else:
            decisions.append(ReconcileDecision(
                job_id=record.job_id,
                decision=RECONCILE_ASK_USER,
                reason=f"未知状态 {record.state!r}，需用户确认。",
            ))
    return tuple(decisions)


__all__ = [
    "ProviderRequestFingerprint",
    "ExternalJobRecord",
    "ExternalJobState",
    "ReconcileDecision",
    "ALL_RECONCILE_DECISIONS",
    "ALL_EXTERNAL_STATES",
    "EXT_PENDING",
    "EXT_IN_PROGRESS",
    "EXT_SUCCEEDED",
    "EXT_FAILED",
    "EXT_UNKNOWN",
    "RECONCILE_RESUME",
    "RECONCILE_SKIP",
    "RECONCILE_ASK_USER",
    "RECONCILE_RETRY",
    "RECONCILE_FAIL",
    "make_fingerprint",
    "reconcile_inflight",
]
