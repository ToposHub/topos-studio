# DORMANT(2026-08-19 审计): 已实现且有测试，但暂无生产入口（有意休眠）。
# 接线前请勿在生产依赖；若半年内无接线计划建议删除。
"""Persistent Motion Graphic session store (MG-20).

MG-20 turns the in-memory ``_pending_motion_graphic_requests`` dict on
``module.py`` into a real lifecycle: unconfirmed drafts, revisions, preview
artifacts and run refs survive a close/reopen of the project, while stale
grants are invalidated automatically.

The store follows the project ``.topos`` rules:

* no secret / API key / absolute path / raw prompt is persisted;
* every record is JSON-serializable with a schema version and an unknown-
  payload passthrough (so future fields do not break older builds);
* grants are stored only as ``grant_id`` + ``scope`` + ``expires_at``; the
  live ``MotionGraphicApprovalService`` is the source of truth for validity,
  the store only survives restarts.

The store is intentionally minimal: it owns briefs, proposals, revisions,
plans, previews and run snapshots.  It does not own the scene documents
themselves (those live in the scene store); it references them by id.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import logging
import os
import threading
import time
import uuid
from typing import Any, Mapping

from .action_matrix import MotionGraphicAction
from .approval_scope import MotionGraphicApprovalScope
from .error_codes import MOTION_GRAPHIC_REVISION_CONFLICT, MotionGraphicFailure


LOGGER = logging.getLogger(__name__)

SCHEMA_VERSION = 1

#: Sentinel distinguishing "caller did not pass status/note" from an explicit
#: value (AUD-27103).  The public default behaviour for create is unchanged:
#: status "draft", note "".
_UNSET: Any = object()

# Keys MotionGraphicApprovalScope actually declares.  ``.topos`` payloads are
# external data (hand-edited / newer schema); unknown scope keys must not be
# forwarded as ``MotionGraphicApprovalScope(**payload)`` kwargs (AUD-27105).
_SCOPE_KEYS: tuple[str, ...] = (
    "project_id", "timeline_id", "track_id", "insert_frame",
    "brief_id", "brief_revision", "brief_hash", "scene_hash",
    "plan_hash", "diff_hash", "preview_hash",
)


def _scope_from_mapping(scope_data: Mapping[str, Any]) -> MotionGraphicApprovalScope | None:
    """Rebuild a scope from persisted JSON, restoring real field types.

    AUD-27105/AUD-27106: the persisted grant_scope dict is external data.
    Unknown keys are dropped instead of raising TypeError, and the typed
    collections are restored (``asset_ids`` list → tuple[str, ...],
    ``allowed_actions`` value-strings → tuple[MotionGraphicAction, ...]) so a
    save/reload round-trip yields a scope that compares equal to the original
    and survives ``diff_from``.  Any structurally invalid scope yields ``None``
    instead of an exception.
    """
    payload: dict[str, Any] = {key: scope_data[key] for key in _SCOPE_KEYS if key in scope_data}
    asset_ids = scope_data.get("asset_ids")
    if isinstance(asset_ids, (list, tuple)):
        payload["asset_ids"] = tuple(str(item) for item in asset_ids)
    actions = scope_data.get("allowed_actions")
    if isinstance(actions, (list, tuple)):
        converted: list[MotionGraphicAction] = []
        for value in actions:
            if isinstance(value, MotionGraphicAction):
                converted.append(value)
                continue
            try:
                converted.append(MotionGraphicAction(str(value)))
            except ValueError:
                LOGGER.warning(
                    "dropping unknown MotionGraphicAction %r in persisted grant_scope", value)
        payload["allowed_actions"] = tuple(converted)
    try:
        return MotionGraphicApprovalScope(**payload)
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# Persistent record
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class MotionGraphicPendingRequest:
    """One restorable pending Motion Graphic request.

    This is what survives a project close/reopen.  The host rebuilds the
    in-memory :class:`~src.features.teaching_animation.ai.revision.MotionGraphicRevisionLog`
    and proposal card from one of these.
    """

    request_id: str
    project_id: str
    brief_id: str
    brief_revision: int
    created_at: float
    updated_at: float
    timeline_id: str | None = None
    lineage_id: str | None = None
    latest_revision_id: str | None = None
    plan_id: str | None = None
    preview_id: str | None = None
    run_id: str | None = None
    grant_id: str | None = None
    grant_scope: MotionGraphicApprovalScope | None = None
    grant_expires_at: float | None = None
    status: str = "draft"          # draft / waiting_approval / applied / cancelled
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "project_id": self.project_id,
            "timeline_id": self.timeline_id,
            "brief_id": self.brief_id,
            "brief_revision": self.brief_revision,
            "lineage_id": self.lineage_id,
            "latest_revision_id": self.latest_revision_id,
            "plan_id": self.plan_id,
            "preview_id": self.preview_id,
            "run_id": self.run_id,
            "grant_id": self.grant_id,
            "grant_scope": self.grant_scope.to_dict() if self.grant_scope else None,
            "grant_expires_at": self.grant_expires_at,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "status": self.status,
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "MotionGraphicPendingRequest":
        scope_data = data.get("grant_scope")
        scope = _scope_from_mapping(scope_data) if isinstance(scope_data, Mapping) else None
        return cls(
            request_id=str(data.get("request_id", "")),
            project_id=str(data.get("project_id", "")),
            timeline_id=data.get("timeline_id"),
            brief_id=str(data.get("brief_id", "")),
            brief_revision=int(data.get("brief_revision", 1)),
            lineage_id=data.get("lineage_id"),
            latest_revision_id=data.get("latest_revision_id"),
            plan_id=data.get("plan_id"),
            preview_id=data.get("preview_id"),
            run_id=data.get("run_id"),
            grant_id=data.get("grant_id"),
            grant_scope=scope,
            grant_expires_at=data.get("grant_expires_at"),
            created_at=float(data.get("created_at", 0.0) or 0.0),
            updated_at=float(data.get("updated_at", 0.0) or 0.0),
            status=str(data.get("status", "draft")),
            note=str(data.get("note", "")),
        )


@dataclass
class MotionGraphicSessionSnapshot:
    """Full snapshot persisted to ``.topos``."""

    schema_version: int = SCHEMA_VERSION
    project_id: str = ""
    requests: tuple[MotionGraphicPendingRequest, ...] = ()
    extra: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "project_id": self.project_id,
            "requests": [req.to_dict() for req in self.requests],
            "extra": dict(self.extra),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "MotionGraphicSessionSnapshot":
        raw_requests = data.get("requests", [])
        requests_list: list[MotionGraphicPendingRequest] = []
        # AUD-27105: every record is external JSON — a record with unknown
        # grant_scope keys, a non-numeric brief_revision/created_at or a
        # broken schema is dropped (with a log) instead of crashing the
        # whole reload, matching the module's unknown-payload passthrough
        # promise.
        for item in raw_requests if isinstance(raw_requests, (list, tuple)) else ():
            if not isinstance(item, Mapping):
                continue
            try:
                requests_list.append(MotionGraphicPendingRequest.from_dict(item))
            except (TypeError, ValueError):
                LOGGER.warning(
                    "dropping malformed Motion Graphic request record %r",
                    item.get("request_id", "<missing id>"),
                )
        return cls(
            schema_version=int(data.get("schema_version", SCHEMA_VERSION)),
            project_id=str(data.get("project_id", "")),
            requests=tuple(requests_list),
            extra=dict(data.get("extra", {}) or {}),
        )


# --------------------------------------------------------------------------- #
# Store
# --------------------------------------------------------------------------- #


class MotionGraphicSessionStore:
    """Thread-safe persistence for Motion Graphic pending requests.

    The store writes one JSON file per project under ``target_dir``.  Writes
    are atomic (``tempfile + os.replace``).  The host calls :meth:`flush`
    after every mutation; tests can construct the store with an in-memory
    target by passing ``target_dir=None`` and using :meth:`snapshot` /
    :meth:`restore`.
    """

    FILENAME = "motion_graphic_session.json"

    def __init__(
        self,
        *,
        target_dir: str | None,
        project_id: str,
        time_fn: Any = None,
    ) -> None:
        self._target_dir = target_dir
        self._project_id = project_id
        self._time_fn = time_fn or time.time
        self._lock = threading.RLock()
        self._requests: dict[str, MotionGraphicPendingRequest] = {}

    # ---------------- queries ---------------- #

    def get(self, request_id: str) -> MotionGraphicPendingRequest | None:
        with self._lock:
            return self._requests.get(request_id)

    def list_requests(self) -> tuple[MotionGraphicPendingRequest, ...]:
        with self._lock:
            return tuple(self._requests.values())

    def list_for_brief(self, brief_id: str) -> tuple[MotionGraphicPendingRequest, ...]:
        with self._lock:
            return tuple(
                req for req in self._requests.values()
                if req.brief_id == brief_id
            )

    # ---------------- mutations ---------------- #

    def upsert_request(
        self,
        *,
        request_id: str | None = None,
        project_id: str,
        timeline_id: str | None,
        brief_id: str,
        brief_revision: int,
        status: Any = _UNSET,
        note: Any = _UNSET,
        **updates: Any,
    ) -> MotionGraphicPendingRequest:
        """Create or update a pending request and persist.

        AUD-27103: the update branch used to fall back to the ``status``
        signature default (``"draft"``), silently resetting a stored
        waiting/applied status, and the named ``timeline_id`` / ``brief_id`` /
        ``brief_revision`` arguments were ignored on update.  Named arguments
        are now merged in as part of the updates (an explicit per-field entry
        in ``**updates`` wins over the same-named argument); omitted values
        fall back to the *existing* record on update and to the documented
        defaults on create.
        """
        with self._lock:
            now = self._time_fn()
            merged: dict[str, Any] = dict(updates)
            # Explicit named arguments count as updates; an explicitly spelled
            # per-field entry in ``updates`` keeps precedence (pre-change
            # behavior for overlapping keys).
            merged.setdefault("timeline_id", timeline_id)
            merged.setdefault("brief_id", brief_id)
            merged.setdefault("brief_revision", brief_revision)
            if status is not _UNSET:
                merged["status"] = status
            if note is not _UNSET:
                merged["note"] = note
            existing = self._requests.get(request_id) if request_id else None
            if existing is None:
                request_id = request_id or f"mg-request-{uuid.uuid4().hex}"
                record = MotionGraphicPendingRequest(
                    request_id=request_id,
                    project_id=project_id,
                    timeline_id=merged.get("timeline_id"),
                    brief_id=merged.get("brief_id", brief_id),
                    brief_revision=merged.get("brief_revision", brief_revision),
                    created_at=now,
                    updated_at=now,
                    status=merged.get("status", "draft"),
                    note=merged.get("note", ""),
                    # New records may be created with grant/lineage/plan/preview
                    # already populated (e.g. restoring from a snapshot).
                    lineage_id=merged.get("lineage_id"),
                    latest_revision_id=merged.get("latest_revision_id"),
                    plan_id=merged.get("plan_id"),
                    preview_id=merged.get("preview_id"),
                    run_id=merged.get("run_id"),
                    grant_id=merged.get("grant_id"),
                    grant_scope=merged.get("grant_scope"),
                    grant_expires_at=merged.get("grant_expires_at"),
                )
            else:
                record = MotionGraphicPendingRequest(
                    request_id=existing.request_id,
                    project_id=project_id,
                    timeline_id=merged.get("timeline_id", existing.timeline_id),
                    brief_id=merged.get("brief_id", existing.brief_id),
                    brief_revision=merged.get("brief_revision", existing.brief_revision),
                    lineage_id=merged.get("lineage_id", existing.lineage_id),
                    latest_revision_id=merged.get("latest_revision_id", existing.latest_revision_id),
                    plan_id=merged.get("plan_id", existing.plan_id),
                    preview_id=merged.get("preview_id", existing.preview_id),
                    run_id=merged.get("run_id", existing.run_id),
                    grant_id=merged.get("grant_id", existing.grant_id),
                    grant_scope=merged.get("grant_scope", existing.grant_scope),
                    grant_expires_at=merged.get("grant_expires_at", existing.grant_expires_at),
                    created_at=existing.created_at,
                    updated_at=now,
                    status=merged.get("status", existing.status),
                    note=merged.get("note", existing.note),
                )
            previous_state = dict(self._requests)
            self._requests[record.request_id] = record
            self._commit_or_rollback(previous_state)
            return record

    def discard(self, request_id: str) -> bool:
        with self._lock:
            previous_state = dict(self._requests)
            existed = self._requests.pop(request_id, None) is not None
            if existed:
                self._commit_or_rollback(previous_state)
            return existed

    def clear(self) -> int:
        with self._lock:
            count = len(self._requests)
            previous_state = dict(self._requests)
            self._requests.clear()
            self._commit_or_rollback(previous_state)
            return count

    def invalidate_stale_grants(self, *, now: float | None = None) -> int:
        """Mark expired grants as invalid (drop the grant_id)."""
        current = now if now is not None else self._time_fn()
        invalidated = 0
        with self._lock:
            previous_state = dict(self._requests)
            for rid, req in list(self._requests.items()):
                if req.grant_id is None:
                    continue
                if req.grant_expires_at is not None and current >= req.grant_expires_at:
                    self._requests[rid] = MotionGraphicPendingRequest(
                        request_id=req.request_id,
                        project_id=req.project_id,
                        timeline_id=req.timeline_id,
                        brief_id=req.brief_id,
                        brief_revision=req.brief_revision,
                        lineage_id=req.lineage_id,
                        latest_revision_id=req.latest_revision_id,
                        plan_id=req.plan_id,
                        preview_id=req.preview_id,
                        run_id=req.run_id,
                        grant_id=None,
                        grant_scope=None,
                        grant_expires_at=None,
                        created_at=req.created_at,
                        updated_at=current,
                        status=req.status,
                        note=req.note,
                    )
                    invalidated += 1
            if invalidated:
                self._commit_or_rollback(previous_state)
        return invalidated

    # ---------------- persistence ---------------- #

    def snapshot(self) -> MotionGraphicSessionSnapshot:
        with self._lock:
            return MotionGraphicSessionSnapshot(
                schema_version=SCHEMA_VERSION,
                project_id=self._project_id,
                requests=tuple(self._requests.values()),
            )

    def restore(self, snapshot: MotionGraphicSessionSnapshot) -> None:
        with self._lock:
            if snapshot.project_id and snapshot.project_id != self._project_id:
                raise MotionGraphicFailure(
                    MOTION_GRAPHIC_REVISION_CONFLICT,
                    "快照属于其他工程。",
                    details={
                        "expected": self._project_id,
                        "actual": snapshot.project_id,
                    },
                )
            previous_state = dict(self._requests)
            self._requests = {req.request_id: req for req in snapshot.requests}
            self._commit_or_rollback(previous_state)

    def reload(self) -> None:
        """Re-read the JSON file from disk if one exists."""
        with self._lock:
            data = self._read_locked()
            if data is None:
                return
            # AUD-27105: the on-disk JSON is external data — a snapshot-level
            # type/conversion error must refuse the load, not crash the host.
            try:
                snapshot = MotionGraphicSessionSnapshot.from_dict(data)
            except (TypeError, ValueError):
                LOGGER.warning("motion graphic session file is malformed; refusing reload")
                return
            if snapshot.project_id and snapshot.project_id != self._project_id:
                # Different project on disk — refuse rather than mix.
                return
            self._requests = {req.request_id: req for req in snapshot.requests}

    # ---------------- internals ---------------- #

    def _path(self) -> str | None:
        if not self._target_dir:
            return None
        return os.path.join(self._target_dir, self.FILENAME)

    def _commit_or_rollback(self, previous_state: Mapping[str, MotionGraphicPendingRequest]) -> None:
        """Persist after a memory mutation, rolling memory back on failure.

        AUD-27104: mutations used to commit to ``self._requests`` *before*
        writing; an OSError in ``_write_locked`` (disk full, read-only dir)
        left memory and disk diverging with no rollback — a later reload
        resurrected deleted records.  Now the mutation only sticks if the
        write succeeds; on failure the pre-mutation state is restored and the
        error propagates to the caller.
        """
        try:
            self._write_locked()
        except OSError:
            self._requests = dict(previous_state)
            raise

    def _write_locked(self) -> None:
        path = self._path()
        if path is None:
            return
        os.makedirs(os.path.dirname(path), exist_ok=True)
        payload = self.snapshot().to_dict()
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        os.replace(tmp, path)

    def _read_locked(self) -> Mapping[str, Any] | None:
        path = self._path()
        if path is None or not os.path.exists(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, json.JSONDecodeError):
            return None
        return data if isinstance(data, Mapping) else None


__all__ = [
    "MotionGraphicPendingRequest",
    "MotionGraphicSessionSnapshot",
    "MotionGraphicSessionStore",
    "SCHEMA_VERSION",
]
