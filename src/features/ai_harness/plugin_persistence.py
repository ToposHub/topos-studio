"""Versioned plugin-state persistence with fail-closed restoration.

The persisted payload records plugin facts (manifest, version, source, last
known state, health and policy) but never restores an active plugin or an
approval grant.  A process restart must re-establish the current plugin
implementation and ask for any write authorization again.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
import hashlib
import json
import time
import uuid
from typing import Any, Callable

from src.features.ai_harness.invocation_models import sanitize_for_storage
from src.features.ai_harness.plugin_runtime import PluginCapability, PluginExecutionPolicy, PluginLifecycle, PluginManifest, PluginRegistry, PluginState


PLUGIN_PROJECT_SCHEMA = "topos-plugin-project/1.0"
LEGACY_REGISTRY_SCHEMA = "topos-plugin-registry/1.0"


@dataclass(frozen=True)
class PluginRestoreReport:
    migrated_from: str = ""
    restored_plugins: tuple[str, ...] = ()
    reactivation_required: tuple[str, ...] = ()
    unknown_plugins: tuple[str, ...] = ()
    skipped_entries: tuple[str, ...] = ()
    state_conflicts: tuple[str, ...] = ()
    migrated_plugins: tuple[str, ...] = ()
    migration_errors: tuple[str, ...] = ()
    authorization_reset: bool = True

    @property
    def ok(self) -> bool:
        return not self.skipped_entries

    def to_dict(self) -> dict[str, Any]:
        return {
            "migrated_from": self.migrated_from,
            "restored_plugins": list(self.restored_plugins),
            "reactivation_required": list(self.reactivation_required),
            "unknown_plugins": list(self.unknown_plugins),
            "skipped_entries": list(self.skipped_entries),
            "state_conflicts": list(self.state_conflicts),
            "migrated_plugins": list(self.migrated_plugins),
            "migration_errors": list(self.migration_errors),
            "authorization_reset": self.authorization_reset,
            "ok": self.ok,
        }


class PluginSnapshotStore:
    """Export, migrate and restore plugin facts for project persistence."""

    @staticmethod
    def export(registry: PluginRegistry, *, project_id: str = "") -> dict[str, Any]:
        raw = registry.snapshot()
        safe_plugins = sanitize_for_storage(list(raw.get("plugins", ())))
        for row in safe_plugins:
            if not isinstance(row, Mapping) or not isinstance(row.get("manifest"), Mapping):
                continue
            safe_manifest = dict(row["manifest"])
            safe_manifest.pop("content_hash", None)
            try:
                # The persisted hash covers the sanitized manifest.  The
                # runtime hash may cover redacted metadata and is therefore
                # intentionally not copied verbatim into the project file.
                safe_manifest["content_hash"] = _manifest_from_dict(safe_manifest).content_hash
            except (TypeError, ValueError, KeyError):
                # Keep the row; restore will surface the malformed manifest as
                # a structured skipped entry instead of breaking project save.
                safe_manifest["content_hash"] = ""
            row["manifest"] = safe_manifest
        snapshot = {
            "schema_version": PLUGIN_PROJECT_SCHEMA,
            "project_id": str(project_id)[:200],
            "plugins": safe_plugins,
            # Explicitly persisted as empty: approvals are process/session
            # facts and must never survive a project reload.
            "authorization": {
                "approval_grants": [],
                "restored": False,
            },
        }
        return sanitize_for_storage(snapshot)

    @staticmethod
    def migrate(snapshot: Mapping[str, Any]) -> tuple[dict[str, Any], str]:
        """Normalize current and legacy registry snapshots to project schema."""

        if not isinstance(snapshot, Mapping):
            raise ValueError("plugin snapshot must be an object")
        version = str(snapshot.get("schema_version", ""))
        if version == PLUGIN_PROJECT_SCHEMA:
            rows = snapshot.get("plugins", ())
            if not isinstance(rows, (list, tuple)):
                raise ValueError("plugin snapshot plugins must be a list")
            return {
                "schema_version": PLUGIN_PROJECT_SCHEMA,
                "project_id": str(snapshot.get("project_id", ""))[:200],
                "plugins": list(rows),
                "authorization": {
                    "approval_grants": [],
                    "restored": False,
                },
            }, version
        if version == LEGACY_REGISTRY_SCHEMA:
            rows = snapshot.get("plugins", ())
            if not isinstance(rows, (list, tuple)):
                raise ValueError("legacy plugin snapshot plugins must be a list")
            return {
                "schema_version": PLUGIN_PROJECT_SCHEMA,
                "project_id": "",
                "plugins": list(rows),
                "authorization": {
                    "approval_grants": [],
                    "restored": False,
                },
            }, version
        # Early development snapshots had no wrapper but did carry a plugin
        # list.  Migrate them conservatively instead of rejecting old projects.
        if not version and isinstance(snapshot.get("plugins"), (list, tuple)):
            return {
                "schema_version": PLUGIN_PROJECT_SCHEMA,
                "project_id": "",
                "plugins": list(snapshot["plugins"]),
                "authorization": {
                    "approval_grants": [],
                    "restored": False,
                },
            }, "legacy-unversioned"
        raise ValueError(f"unsupported plugin snapshot schema: {version!r}")

    @classmethod
    def restore(
        cls,
        snapshot: Mapping[str, Any],
        registry: PluginRegistry,
        *,
        resolver: Mapping[str, PluginLifecycle] | Callable[[PluginManifest], PluginLifecycle | None] | None = None,
        migrations: "PluginMigrationRegistry | None" = None,
        target_versions: Mapping[str, str] | None = None,
    ) -> PluginRestoreReport:
        try:
            normalized, migrated_from = cls.migrate(snapshot)
        except (TypeError, ValueError) as exc:
            return PluginRestoreReport(skipped_entries=(str(exc)[:500],))

        migration_report = PluginMigrationReport()
        if migrations is not None:
            normalized, migration_report = migrations.migrate(
                normalized,
                target_versions=target_versions or {},
            )
        restored: list[str] = []
        reactivation: list[str] = []
        unknown: list[str] = []
        skipped: list[str] = []
        skipped.extend(migration_report.blocked_entries)
        blocked_plugin_ids = set(migration_report.blocked_plugin_ids)
        rows = normalized["plugins"]
        for index, raw in enumerate(rows):
            if not isinstance(raw, Mapping):
                skipped.append(f"entry_{index}:not_object")
                continue
            plugin_id = str(raw.get("manifest", {}).get("plugin_id", "")) if isinstance(raw.get("manifest"), Mapping) else ""
            if plugin_id in blocked_plugin_ids:
                skipped.append(f"{plugin_id}:migration_blocked")
                continue
            try:
                manifest = _manifest_from_dict(raw.get("manifest"))
                policy = _policy_from_dict(raw.get("policy", {}))
            except (TypeError, ValueError, KeyError) as exc:
                skipped.append(f"entry_{index}:{type(exc).__name__}:{exc}"[:500])
                continue
            plugin = _resolve(resolver, manifest)
            previous_state = str(raw.get("state", "enabled"))
            accepted = registry.register(
                manifest,
                plugin,
                enabled=previous_state != "disabled",
                policy=policy,
            )
            if not accepted:
                skipped.append(f"{manifest.plugin_id}:duplicate_or_invalid")
                continue
            restored.append(manifest.plugin_id)
            if previous_state == "active":
                reactivation.append(manifest.plugin_id)
            if plugin is None:
                unknown.append(manifest.plugin_id)
                if previous_state != "disabled":
                    registry.degrade(manifest.plugin_id, "restored plugin implementation is unavailable")
            elif previous_state in {"failed", "degraded"}:
                registry.degrade(
                    manifest.plugin_id,
                    f"restored previous state: {previous_state}; reactivation requires host check",
                )

        return PluginRestoreReport(
            migrated_from=migrated_from,
            restored_plugins=tuple(restored),
            reactivation_required=tuple(reactivation),
            unknown_plugins=tuple(unknown),
            skipped_entries=tuple(skipped),
            migrated_plugins=migration_report.migrated_plugins,
            migration_errors=migration_report.blocked_entries,
            authorization_reset=True,
        )

    @classmethod
    def restore_project_state(
        cls,
        snapshot: Mapping[str, Any],
        registry: PluginRegistry,
        *,
        state_restorer: Callable[[str, str], bool] | None = None,
    ) -> PluginRestoreReport:
        """Reconcile persisted lifecycle facts with already loaded plugins.

        The host composes validated implementations at startup.  This method
        only applies the saved state to those known implementations; an old
        ``active`` state becomes ``enabled`` and therefore needs explicit
        activation.  It never registers persisted code or calls ``activate``.
        ``state_restorer`` lets composition hosts synchronize their secondary
        registries (for example ``SkillRegistry``) with the kernel registry.
        """

        try:
            normalized, migrated_from = cls.migrate(snapshot)
        except (TypeError, ValueError) as exc:
            return PluginRestoreReport(skipped_entries=(str(exc)[:500],))

        restored: list[str] = []
        reactivation: list[str] = []
        unknown: list[str] = []
        skipped: list[str] = []
        conflicts: list[str] = []
        apply_state = state_restorer or registry.restore_project_state
        for index, raw in enumerate(normalized["plugins"]):
            if not isinstance(raw, Mapping):
                skipped.append(f"entry_{index}:not_object")
                continue
            try:
                manifest_raw = raw.get("manifest")
                manifest = _manifest_from_dict(manifest_raw)
                requested_state = str(raw.get("state", PluginState.ENABLED.value))
            except (TypeError, ValueError, KeyError) as exc:
                skipped.append(f"entry_{index}:{type(exc).__name__}:{exc}"[:500])
                continue
            current = registry.get(manifest.plugin_id)
            if current is None:
                unknown.append(manifest.plugin_id)
                continue
            if _stored_manifest_hash(current.manifest) != manifest.content_hash:
                conflicts.append(f"{manifest.plugin_id}:manifest_content_hash_mismatch")
                continue
            if not apply_state(manifest.plugin_id, requested_state):
                conflicts.append(f"{manifest.plugin_id}:state_restore_failed")
                continue
            restored.append(manifest.plugin_id)
            if requested_state == PluginState.ACTIVE.value:
                reactivation.append(manifest.plugin_id)

        return PluginRestoreReport(
            migrated_from=migrated_from,
            restored_plugins=tuple(restored),
            reactivation_required=tuple(reactivation),
            unknown_plugins=tuple(unknown),
            skipped_entries=tuple(skipped),
            state_conflicts=tuple(conflicts),
            authorization_reset=True,
        )


@dataclass(frozen=True)
class PluginMigrationReport:
    migrated_plugins: tuple[str, ...] = ()
    blocked_entries: tuple[str, ...] = ()
    blocked_plugin_ids: tuple[str, ...] = ()


PluginManifestMigration = Callable[[dict[str, Any]], Mapping[str, Any]]


@dataclass(frozen=True)
class _PluginMigration:
    plugin_id: str
    from_version: str
    to_version: str
    migrate: PluginManifestMigration


class PluginMigrationRegistry:
    """Deterministic manifest migration chains for plugin upgrades."""

    def __init__(self) -> None:
        self._migrations: dict[tuple[str, str], _PluginMigration] = {}

    def register(
        self,
        plugin_id: str,
        from_version: str,
        to_version: str,
        migrate: PluginManifestMigration,
    ) -> bool:
        key = (str(plugin_id), str(from_version))
        if not key[0] or not key[1] or not str(to_version) or key[1] == str(to_version):
            raise ValueError("plugin migration versions must be non-empty and different")
        if key in self._migrations:
            return False
        self._migrations[key] = _PluginMigration(
            plugin_id=key[0],
            from_version=key[1],
            to_version=str(to_version),
            migrate=migrate,
        )
        return True

    def migrate(
        self,
        snapshot: Mapping[str, Any],
        *,
        target_versions: Mapping[str, str],
    ) -> tuple[dict[str, Any], PluginMigrationReport]:
        """Apply registered one-step migrations until each target is reached."""

        normalized, _ = PluginSnapshotStore.migrate(snapshot)
        rows = [dict(row) if isinstance(row, Mapping) else row for row in normalized["plugins"]]
        migrated: list[str] = []
        blocked: list[str] = []
        blocked_plugin_ids: list[str] = []
        for index, row in enumerate(rows):
            if not isinstance(row, Mapping):
                continue
            manifest_raw = row.get("manifest")
            if not isinstance(manifest_raw, Mapping):
                continue
            plugin_id = str(manifest_raw.get("plugin_id", ""))
            target = str(target_versions.get(plugin_id, ""))
            current = str(manifest_raw.get("version", "1.0.0"))
            if not target or target == current:
                continue
            working = dict(manifest_raw)
            seen_versions: set[str] = set()
            while current != target:
                if current in seen_versions:
                    blocked.append(f"{plugin_id}:migration_cycle")
                    blocked_plugin_ids.append(plugin_id)
                    break
                seen_versions.add(current)
                migration = self._migrations.get((plugin_id, current))
                if migration is None:
                    blocked.append(f"{plugin_id}:{current}->{target}:migration_missing")
                    blocked_plugin_ids.append(plugin_id)
                    break
                try:
                    migrated_raw = migration.migrate(dict(working))
                    if not isinstance(migrated_raw, Mapping):
                        raise TypeError("migration must return a mapping")
                    working = dict(migrated_raw)
                except Exception as exc:  # noqa: BLE001 - migration is isolated
                    blocked.append(f"{plugin_id}:{current}->{migration.to_version}:{type(exc).__name__}")
                    blocked_plugin_ids.append(plugin_id)
                    break
                working["version"] = migration.to_version
                # A manifest migration changes the canonical content.  Do
                # not carry a stale hash into the next restore boundary.
                migrated_manifest = _manifest_from_dict({
                    key: value for key, value in working.items()
                    if key != "content_hash"
                })
                working["content_hash"] = migrated_manifest.content_hash
                current = migration.to_version
            else:
                row_copy = dict(row)
                row_copy["manifest"] = working
                rows[index] = row_copy
                migrated.append(plugin_id)
        normalized["plugins"] = rows
        return normalized, PluginMigrationReport(
            migrated_plugins=tuple(migrated),
            blocked_entries=tuple(blocked),
            blocked_plugin_ids=tuple(dict.fromkeys(blocked_plugin_ids)),
        )


@dataclass(frozen=True)
class PluginSnapshotCheckpoint:
    checkpoint_id: str
    snapshot_hash: str
    created_at: float
    snapshot: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "checkpoint_id": self.checkpoint_id,
            "snapshot_hash": self.snapshot_hash,
            "created_at": self.created_at,
            "snapshot": dict(self.snapshot),
        }


@dataclass(frozen=True)
class PluginJournalRestoreReport:
    restored_checkpoints: tuple[str, ...] = ()
    skipped_entries: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.skipped_entries

    def to_dict(self) -> dict[str, Any]:
        return {
            "restored_checkpoints": list(self.restored_checkpoints),
            "skipped_entries": list(self.skipped_entries),
            "ok": self.ok,
        }


class PluginSnapshotJournal:
    """Bounded, recoverable checkpoint history for plugin state."""

    def __init__(self, *, max_checkpoints: int = 5) -> None:
        if max_checkpoints <= 0:
            raise ValueError("max_checkpoints must be positive")
        self._max_checkpoints = max_checkpoints
        self._checkpoints: list[PluginSnapshotCheckpoint] = []
        self._current_id: str = ""

    def checkpoint(self, snapshot: Mapping[str, Any]) -> PluginSnapshotCheckpoint:
        normalized, _ = PluginSnapshotStore.migrate(snapshot)
        safe = sanitize_for_storage(normalized)
        encoded = json.dumps(safe, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        snapshot_hash = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        if self._checkpoints and self._checkpoints[-1].snapshot_hash == snapshot_hash:
            self._current_id = self._checkpoints[-1].checkpoint_id
            return self._checkpoints[-1]
        checkpoint = PluginSnapshotCheckpoint(
            checkpoint_id=f"plugin-checkpoint-{uuid.uuid4().hex[:12]}",
            snapshot_hash=snapshot_hash,
            created_at=time.time(),
            snapshot=safe,
        )
        self._checkpoints.append(checkpoint)
        self._checkpoints = self._checkpoints[-self._max_checkpoints:]
        self._current_id = checkpoint.checkpoint_id
        return checkpoint

    def checkpoint_registry(
        self, registry: PluginRegistry, *, project_id: str = "",
    ) -> PluginSnapshotCheckpoint:
        return self.checkpoint(PluginSnapshotStore.export(registry, project_id=project_id))

    def list(self) -> tuple[PluginSnapshotCheckpoint, ...]:
        return tuple(self._checkpoints)

    def rollback(self, checkpoint_id: str) -> Mapping[str, Any] | None:
        for checkpoint in self._checkpoints:
            if checkpoint.checkpoint_id == str(checkpoint_id):
                self._current_id = checkpoint.checkpoint_id
                return dict(checkpoint.snapshot)
        return None

    @classmethod
    def from_dict(
        cls, payload: Mapping[str, Any],
    ) -> tuple["PluginSnapshotJournal", PluginJournalRestoreReport]:
        if not isinstance(payload, Mapping):
            return cls(), PluginJournalRestoreReport(
                skipped_entries=("journal:not_object",),
            )
        try:
            max_checkpoints = int(payload.get("max_checkpoints", 5))
        except (TypeError, ValueError):
            return cls(), PluginJournalRestoreReport(
                skipped_entries=("journal:invalid_max_checkpoints",),
            )
        try:
            journal = cls(max_checkpoints=max_checkpoints)
        except ValueError:
            return cls(), PluginJournalRestoreReport(
                skipped_entries=("journal:invalid_max_checkpoints",),
            )
        report = journal.restore_dict(payload)
        return journal, report

    def restore_dict(self, payload: Mapping[str, Any]) -> PluginJournalRestoreReport:
        """Restore a journal after validating every checkpoint hash."""
        if not isinstance(payload, Mapping):
            return PluginJournalRestoreReport(skipped_entries=("journal:not_object",))
        if str(payload.get("schema_version", "")) != "topos-plugin-journal/1.0":
            return PluginJournalRestoreReport(skipped_entries=("journal:unsupported_schema",))
        rows = payload.get("checkpoints", ())
        if not isinstance(rows, (list, tuple)):
            return PluginJournalRestoreReport(skipped_entries=("journal:checkpoints_not_list",))
        restored: list[PluginSnapshotCheckpoint] = []
        skipped: list[str] = []
        seen_ids: set[str] = set()
        for index, raw in enumerate(rows):
            if not isinstance(raw, Mapping):
                skipped.append(f"checkpoint_{index}:not_object")
                continue
            checkpoint_id = str(raw.get("checkpoint_id", ""))
            snapshot_hash = str(raw.get("snapshot_hash", ""))
            snapshot = raw.get("snapshot")
            if not checkpoint_id or checkpoint_id in seen_ids or not snapshot_hash:
                skipped.append(f"checkpoint_{index}:invalid_identity")
                continue
            try:
                normalized, _ = PluginSnapshotStore.migrate(snapshot)
                safe = sanitize_for_storage(normalized)
                encoded = json.dumps(safe, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                if hashlib.sha256(encoded.encode("utf-8")).hexdigest() != snapshot_hash:
                    skipped.append(f"checkpoint_{index}:hash_mismatch")
                    continue
                created_at = float(raw.get("created_at", 0.0))
            except (TypeError, ValueError, OverflowError):
                skipped.append(f"checkpoint_{index}:invalid_snapshot")
                continue
            seen_ids.add(checkpoint_id)
            restored.append(PluginSnapshotCheckpoint(
                checkpoint_id=checkpoint_id,
                snapshot_hash=snapshot_hash,
                created_at=created_at,
                snapshot=safe,
            ))
        self._checkpoints = restored[-self._max_checkpoints:]
        available_ids = {item.checkpoint_id for item in self._checkpoints}
        requested_current = str(payload.get("current_id", ""))
        self._current_id = requested_current if requested_current in available_ids else (
            self._checkpoints[-1].checkpoint_id if self._checkpoints else ""
        )
        return PluginJournalRestoreReport(
            restored_checkpoints=tuple(item.checkpoint_id for item in self._checkpoints),
            skipped_entries=tuple(skipped),
        )

    @property
    def current_id(self) -> str:
        return self._current_id

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "topos-plugin-journal/1.0",
            "max_checkpoints": self._max_checkpoints,
            "current_id": self._current_id,
            "checkpoints": [checkpoint.to_dict() for checkpoint in self._checkpoints],
        }


def _resolve(
    resolver: Mapping[str, PluginLifecycle] | Callable[[PluginManifest], PluginLifecycle | None] | None,
    manifest: PluginManifest,
) -> PluginLifecycle | None:
    if resolver is None:
        return None
    if isinstance(resolver, Mapping):
        return resolver.get(manifest.plugin_id)
    return resolver(manifest)


def _stored_manifest_hash(manifest: PluginManifest) -> str:
    """Compute the hash used by the sanitized project representation."""

    safe = sanitize_for_storage(manifest.to_dict())
    if not isinstance(safe, Mapping):
        raise TypeError("sanitized plugin manifest must be an object")
    safe = dict(safe)
    safe.pop("content_hash", None)
    return _manifest_from_dict(safe).content_hash


def _manifest_from_dict(raw: Any) -> PluginManifest:
    if not isinstance(raw, Mapping):
        raise ValueError("plugin manifest must be an object")
    capabilities: list[PluginCapability] = []
    for item in raw.get("capabilities", ()):
        if not isinstance(item, Mapping):
            raise ValueError("plugin capability must be an object")
        capabilities.append(PluginCapability(
            capability_id=str(item.get("capability_id", "")),
            description=str(item.get("description", "")),
            tool_names=tuple(item.get("tool_names", ()) or ()),
            permission=str(item.get("permission", "read")),
            requires_approval=bool(item.get("requires_approval", False)),
            input_schema=dict(item.get("input_schema", {}) or {}),
            output_schema=dict(item.get("output_schema", {}) or {}),
            metadata=dict(item.get("metadata", {}) or {}),
        ))
    manifest = PluginManifest(
        plugin_id=str(raw.get("plugin_id", "")),
        display_name=str(raw.get("display_name", "")),
        version=str(raw.get("version", "1.0.0")),
        api_version=str(raw.get("api_version", "1.0")),
        source=str(raw.get("source", "builtin")),
        author=str(raw.get("author", "")),
        description=str(raw.get("description", "")),
        capabilities=tuple(capabilities),
        dependencies=tuple(raw.get("dependencies", ()) or ()),
        metadata=dict(raw.get("metadata", {}) or {}),
        declared_hash=str(raw.get("declared_hash", "")),
        signature=str(raw.get("signature", "")),
        signature_key_id=str(raw.get("signature_key_id", "")),
    )
    expected_content_hash = str(raw.get("content_hash", ""))
    if expected_content_hash and expected_content_hash != manifest.content_hash:
        raise ValueError("plugin manifest content hash mismatch")
    return manifest


def _policy_from_dict(raw: Any) -> PluginExecutionPolicy:
    if raw is None:
        return PluginExecutionPolicy()
    if not isinstance(raw, Mapping):
        raise ValueError("plugin policy must be an object")
    return PluginExecutionPolicy(
        max_concurrent_calls=int(raw.get("max_concurrent_calls", 1)),
        max_input_chars=int(raw.get("max_input_chars", 64_000)),
        max_output_chars=int(raw.get("max_output_chars", 256_000)),
        allow_network=bool(raw.get("allow_network", False)),
        allow_filesystem=bool(raw.get("allow_filesystem", False)),
        timeout_seconds=(
            None if raw.get("timeout_seconds") is None
            else float(raw.get("timeout_seconds"))
        ),
        isolation_mode=str(raw.get("isolation_mode", "in_process")),
        sandbox_mode=str(raw.get("sandbox_mode", "report")),
    )


__all__ = [
    "LEGACY_REGISTRY_SCHEMA",
    "PLUGIN_PROJECT_SCHEMA",
    "PluginRestoreReport",
    "PluginMigrationRegistry",
    "PluginMigrationReport",
    "PluginJournalRestoreReport",
    "PluginSnapshotCheckpoint",
    "PluginSnapshotJournal",
    "PluginSnapshotStore",
]
