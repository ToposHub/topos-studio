# DORMANT(2026-08-19 审计): 已实现且有测试，但暂无生产入口（有意休眠）。
# 接线前请勿在生产依赖；若半年内无接线计划建议删除。
"""Motion Graphic approval scope and one-shot grant service (MG-06).

MG-06 closes the hole where "the user clicked confirm once" used to be a bare
boolean and a request id.  A confirmation must be **bound** to:

* the project and timeline the user was looking at,
* the brief revision that produced the proposal,
* the scene hash and diff hash the user saw in the preview,
* the insertion point (track + frame) the preview was rendered for,
* the set of asset ids the plan is allowed to touch,
* the permission tier and a wall-clock expiry.

When any of those change, the confirmation is stale and the host must ask
again.  Repeated clicks on the same confirm button must never insert twice.

Design:

* :class:`MotionGraphicApprovalScope` is the immutable, fully-typed binding.
* :class:`MotionGraphicApprovalService` is a pure-memory, thread-safe grant
  issuer/consumer mirroring the shape of the cross-domain ``ApprovalService``
  (so the host code reads the same), but with Motion Graphic specific scope
  fields and diagnostic codes.
* Grants are one-shot by default.  ``consume`` is atomic and never marks a
  failed validation as used, so a transient stale check can be retried after
  re-confirmation.
* Nothing in this module touches Qt, the scene store or the timeline — it is
  the decision layer only.  The host wires it into the confirm handler.
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass
from threading import RLock
from typing import Any

from .action_matrix import MotionGraphicAction, MotionGraphicPermissionTier
from .error_codes import MOTION_GRAPHIC_APPROVAL_EXPIRED, MOTION_GRAPHIC_APPROVAL_REQUIRED, MOTION_GRAPHIC_APPROVAL_SCOPE_MISMATCH, MOTION_GRAPHIC_PERMISSION_DENIED, MotionGraphicFailure


# --------------------------------------------------------------------------- #
# Scope
# --------------------------------------------------------------------------- #


#: Default TTL for a Motion Graphic commit grant.  Previews can outlive a
#: single render, but a confirm should not stay valid for hours while the
#: timeline keeps moving underneath it.
DEFAULT_GRANT_TTL_SECONDS: float = 300.0


@dataclass(frozen=True)
class MotionGraphicApprovalScope:
    """The set of objects a confirmation is bound to.

    All hash fields are the *sha256 hex* form produced by the brief / scene /
    plan / preview adapters.  ``None`` means "unconstrained" for backward
    compatibility with scopes that do not care about that field, but in
    practice the host should fill every field it can — the more bindings, the
    stronger the stale detection.
    """

    project_id: str
    timeline_id: str | None
    track_id: str | None
    insert_frame: int | None
    brief_id: str
    brief_revision: int
    brief_hash: str
    scene_hash: str | None
    plan_hash: str | None
    diff_hash: str | None
    preview_hash: str | None
    asset_ids: tuple[str, ...] = ()
    allowed_actions: tuple[MotionGraphicAction, ...] = ()

    def __post_init__(self) -> None:
        if not self.project_id:
            raise ValueError("MotionGraphicApprovalScope.project_id is required")
        if not self.brief_id:
            raise ValueError("MotionGraphicApprovalScope.brief_id is required")
        if self.brief_revision < 1:
            raise ValueError("MotionGraphicApprovalScope.brief_revision must be >= 1")
        if not self.brief_hash:
            raise ValueError("MotionGraphicApprovalScope.brief_hash is required")
        if self.insert_frame is not None and self.insert_frame < 0:
            raise ValueError("MotionGraphicApprovalScope.insert_frame must be non-negative")

    def matches(self, other: "MotionGraphicApprovalScope") -> bool:
        """Return whether ``other`` satisfies every binding in this scope.

        A binding is satisfied when either side is ``None`` (unconstrained) or
        the two values are equal.  ``project_id`` / ``brief_id`` /
        ``brief_revision`` are always required to match exactly — those are the
        hard guardrails that prevent cross-project / cross-brief confusion.
        """

        if self.project_id != other.project_id:
            return False
        if self.brief_id != other.brief_id:
            return False
        if self.brief_revision != other.brief_revision:
            return False
        for field_name in (
            "timeline_id", "track_id", "insert_frame", "brief_hash",
            "scene_hash", "plan_hash", "diff_hash", "preview_hash",
        ):
            mine = getattr(self, field_name)
            theirs = getattr(other, field_name)
            if mine is not None and theirs is not None and mine != theirs:
                return False
        if self.asset_ids and other.asset_ids:
            if not set(other.asset_ids).issubset(set(self.asset_ids)):
                return False
        if self.allowed_actions and other.allowed_actions:
            if not set(other.allowed_actions).issubset(set(self.allowed_actions)):
                return False
        return True

    def diff_from(self, observed: "MotionGraphicApprovalScope") -> list[str]:
        """Return human-readable reasons why ``observed`` no longer matches.

        Used by the host to explain *why* a confirmation went stale so the UI
        can tell the user "你修改了 brief / 切换了时间线 / 场景已变更".
        """

        reasons: list[str] = []
        field_labels = {
            "project_id": "工程",
            "timeline_id": "时间线",
            "track_id": "Graphics 轨道",
            "insert_frame": "插入帧位置",
            "brief_id": "需求",
            "brief_revision": "需求版本",
            "brief_hash": "需求内容",
            "scene_hash": "场景",
            "plan_hash": "方案",
            "diff_hash": "差异",
            "preview_hash": "预览",
        }
        for field_name, label in field_labels.items():
            mine = getattr(self, field_name)
            theirs = getattr(observed, field_name)
            if mine is not None and theirs is not None and mine != theirs:
                reasons.append(f"{label}已变更")
        if self.asset_ids and observed.asset_ids:
            extra = set(observed.asset_ids) - set(self.asset_ids)
            if extra:
                reasons.append(f"出现了未经确认的素材：{', '.join(sorted(extra))}")
        if self.allowed_actions and observed.allowed_actions:
            extra = set(observed.allowed_actions) - set(self.allowed_actions)
            if extra:
                reasons.append(
                    "方案包含未授权的动作："
                    + ", ".join(sorted(action.value for action in extra))
                )
        return reasons

    def to_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "timeline_id": self.timeline_id,
            "track_id": self.track_id,
            "insert_frame": self.insert_frame,
            "brief_id": self.brief_id,
            "brief_revision": self.brief_revision,
            "brief_hash": self.brief_hash,
            "scene_hash": self.scene_hash,
            "plan_hash": self.plan_hash,
            "diff_hash": self.diff_hash,
            "preview_hash": self.preview_hash,
            "asset_ids": list(self.asset_ids),
            "allowed_actions": [action.value for action in self.allowed_actions],
        }


# --------------------------------------------------------------------------- #
# Grant
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class MotionGraphicGrant:
    """One-shot, opaque confirmation token bound to an approval scope."""

    grant_id: str
    scope: MotionGraphicApprovalScope
    issued_at: float
    expires_at: float
    single_use: bool = True
    issued_by: str = "host"
    permission_tier: MotionGraphicPermissionTier = MotionGraphicPermissionTier.ALWAYS_CONFIRM

    def is_expired_at(self, now: float) -> bool:
        return now >= self.expires_at


@dataclass
class _GrantRecord:
    grant: MotionGraphicGrant
    used: bool = False
    revoked: bool = False


# --------------------------------------------------------------------------- #
# Service
# --------------------------------------------------------------------------- #


class MotionGraphicApprovalService:
    """Thread-safe issuer + consumer of Motion Graphic confirmation grants.

    Like the cross-domain ``ApprovalService``:

    * only the host calls :meth:`issue_after_user_confirmation`;
    * model / planner / MCP clients never receive the grant object, only the
      host forwards the grant id to the commit entry;
    * grants are single-use by default so double-clicks cannot double-insert.
    """

    def __init__(self, *, time_fn: Any = None) -> None:
        self._time_fn = time_fn or time.time
        self._records: dict[str, _GrantRecord] = {}
        self._lock = RLock()

    # ---------------- issuance (host only) ---------------- #

    def issue_after_user_confirmation(
        self,
        scope: MotionGraphicApprovalScope,
        *,
        ttl_seconds: float | None = None,
        single_use: bool = True,
        issued_by: str = "host",
        permission_tier: MotionGraphicPermissionTier = MotionGraphicPermissionTier.ALWAYS_CONFIRM,
    ) -> MotionGraphicGrant:
        if ttl_seconds is None:
            ttl_seconds = DEFAULT_GRANT_TTL_SECONDS
        now = self._time_fn()
        grant = MotionGraphicGrant(
            grant_id=f"mg-grant-{secrets.token_urlsafe(32)}",
            scope=scope,
            issued_at=now,
            expires_at=now + ttl_seconds,
            single_use=single_use,
            issued_by=issued_by,
            permission_tier=permission_tier,
        )
        with self._lock:
            self._records[grant.grant_id] = _GrantRecord(grant=grant)
        return grant

    # ---------------- consumption ---------------- #

    def consume(
        self,
        grant_id: str,
        expected_scope: MotionGraphicApprovalScope,
    ) -> MotionGraphicGrant:
        """Atomically validate + consume a grant. Raises on any failure.

        Validation order: exists/unused → not revoked → not expired → scope
        matches.  A failure never marks the grant used, so a stale-check retry
        after re-confirmation still works.
        """

        with self._lock:
            record = self._records.get(grant_id)
            if record is None or (record.grant.single_use and record.used):
                raise MotionGraphicFailure(
                    MOTION_GRAPHIC_APPROVAL_REQUIRED,
                    "确认已用完或不存在；请重新查看方案并确认。",
                    details={"grant_id": grant_id},
                )
            if record.revoked:
                raise MotionGraphicFailure(
                    MOTION_GRAPHIC_PERMISSION_DENIED,
                    "确认已被撤销；请重新确认。",
                    details={"grant_id": grant_id},
                )
            now = self._time_fn()
            if record.grant.is_expired_at(now):
                self._records.pop(grant_id, None)
                raise MotionGraphicFailure(
                    MOTION_GRAPHIC_APPROVAL_EXPIRED,
                    "确认已过期；请重新查看方案并确认。",
                    details={"grant_id": grant_id},
                )
            if not record.grant.scope.matches(expected_scope):
                reasons = record.grant.scope.diff_from(expected_scope)
                detail = "; ".join(reasons) if reasons else "scope mismatch"
                raise MotionGraphicFailure(
                    MOTION_GRAPHIC_APPROVAL_SCOPE_MISMATCH,
                    f"确认作用域不匹配：{detail}。",
                    details={"grant_id": grant_id, "reasons": reasons},
                )
            if record.grant.single_use:
                record.used = True
            return record.grant

    def validate(
        self,
        grant_id: str,
        expected_scope: MotionGraphicApprovalScope,
    ) -> bool:
        try:
            with self._lock:
                record = self._records.get(grant_id)
                if record is None or record.revoked:
                    return False
                if record.grant.single_use and record.used:
                    return False
                if record.grant.is_expired_at(self._time_fn()):
                    return False
                return record.grant.scope.matches(expected_scope)
        except Exception:  # noqa: BLE001 - validate must never raise
            return False

    # ---------------- lifecycle ---------------- #

    def revoke(self, grant_id: str) -> bool:
        with self._lock:
            record = self._records.get(grant_id)
            if record is None:
                return False
            record.revoked = True
            return True

    def revoke_for_project(self, project_id: str) -> int:
        n = 0
        with self._lock:
            for record in self._records.values():
                if record.grant.scope.project_id == project_id and not record.revoked:
                    record.revoked = True
                    n += 1
        return n

    def revoke_for_brief(self, brief_id: str) -> int:
        n = 0
        with self._lock:
            for record in self._records.values():
                if record.grant.scope.brief_id == brief_id and not record.revoked:
                    record.revoked = True
                    n += 1
        return n

    def revoke_all(self) -> int:
        with self._lock:
            n = sum(1 for r in self._records.values() if not r.revoked)
            for r in self._records.values():
                r.revoked = True
            return n

    def purge_expired(self) -> int:
        now = self._time_fn()
        with self._lock:
            stale = [
                gid for gid, record in self._records.items()
                if record.grant.is_expired_at(now)
            ]
            for gid in stale:
                self._records.pop(gid, None)
            return len(stale)

    def active_count(self) -> int:
        now = self._time_fn()
        with self._lock:
            return sum(
                1 for record in self._records.values()
                if not record.revoked
                and not (record.grant.single_use and record.used)
                and not record.grant.is_expired_at(now)
            )

    # ---------------- stale detection ---------------- #

    @staticmethod
    def detect_stale(
        approved: MotionGraphicApprovalScope,
        observed: MotionGraphicApprovalScope,
    ) -> list[str]:
        """Return reasons ``observed`` would be rejected against ``approved``.

        Convenience helper for the host: after the user changes the brief or
        the timeline moves, build an *observed* scope from the current state
        and call this to render "why is the old confirmation stale" before the
        commit attempt, instead of waiting for ``consume`` to raise.
        """

        if approved.matches(observed):
            return []
        return approved.diff_from(observed)


__all__ = [
    "MotionGraphicApprovalScope",
    "MotionGraphicGrant",
    "MotionGraphicApprovalService",
    "DEFAULT_GRANT_TTL_SECONDS",
]
