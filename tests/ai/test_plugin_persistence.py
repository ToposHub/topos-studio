"""P6 plugin snapshot migration and fail-closed restore tests."""

from __future__ import annotations

from src.features.ai_harness.plugin_persistence import (
    PLUGIN_PROJECT_SCHEMA,
    PluginMigrationRegistry,
    PluginSnapshotJournal,
    PluginSnapshotStore,
)
from src.features.ai_harness.plugin_runtime import (
    PluginCapability,
    PluginExecutionPolicy,
    PluginHealth,
    PluginManifest,
    PluginRegistry,
    PluginSource,
    PluginState,
)


def _manifest(plugin_id: str = "topos.persisted", *, version: str = "1.0.0") -> PluginManifest:
    return PluginManifest(
        plugin_id=plugin_id,
        display_name="Persisted plugin",
        version=version,
        source=PluginSource.LOCAL_DEV,
        capabilities=(PluginCapability(
            capability_id="persisted.inspect",
            tool_names=("topos.persisted.inspect",),
            metadata={"api_key": "api_key=sk-1234567890123456"},
        ),),
    )


class _Plugin:
    def __init__(self) -> None:
        self.active = False

    def activate(self, context) -> None:
        self.active = True

    def deactivate(self) -> None:
        self.active = False

    def health(self) -> PluginHealth:
        return PluginHealth("healthy" if self.active else "inactive")

    def invoke(self, tool_name, args, *, approval=None):
        return {"ok": True, "tool_name": tool_name}


def test_export_redacts_metadata_and_restore_does_not_reactivate_or_restore_auth() -> None:
    source = PluginRegistry()
    plugin = _Plugin()
    manifest = _manifest()
    policy = PluginExecutionPolicy(max_concurrent_calls=2)
    assert source.register(manifest, plugin, policy=policy)
    assert source.activate(manifest.plugin_id)
    snapshot = PluginSnapshotStore.export(source, project_id="project-1")

    serialized = str(snapshot)
    assert "sk-1234567890123456" not in serialized
    assert snapshot["schema_version"] == PLUGIN_PROJECT_SCHEMA
    assert snapshot["authorization"]["approval_grants"] == []

    restored = PluginRegistry()
    report = PluginSnapshotStore.restore(
        snapshot,
        restored,
        resolver={manifest.plugin_id: _Plugin()},
    )
    assert report.ok
    assert report.authorization_reset
    assert report.reactivation_required == (manifest.plugin_id,)
    record = restored.get(manifest.plugin_id)
    assert record is not None
    assert record.state == PluginState.ENABLED
    assert record.policy.max_concurrent_calls == 2
    assert restored.invoke_tool(manifest.plugin_id, "topos.persisted.inspect")["diagnostics"][0]["code"] == "PLUGIN_NOT_ACTIVE"
    assert restored.activate(manifest.plugin_id)


def test_project_state_restore_downgrades_active_without_reactivation() -> None:
    source = PluginRegistry()
    source_plugin = _Plugin()
    manifest = _manifest("topos.project-state")
    assert source.register(manifest, source_plugin)
    assert source.activate(manifest.plugin_id)
    snapshot = PluginSnapshotStore.export(source)

    target = PluginRegistry()
    target_plugin = _Plugin()
    assert target.register(manifest, target_plugin)
    assert target.activate(manifest.plugin_id)
    report = PluginSnapshotStore.restore_project_state(snapshot, target)

    assert report.ok
    assert report.restored_plugins == (manifest.plugin_id,)
    assert report.reactivation_required == (manifest.plugin_id,)
    assert target.get(manifest.plugin_id).state == PluginState.ENABLED
    assert target_plugin.active is False
    assert target.invoke_tool(manifest.plugin_id, "topos.persisted.inspect")["diagnostics"][0]["code"] == "PLUGIN_NOT_ACTIVE"


def test_project_state_restore_keeps_disabled_plugin_disabled() -> None:
    source = PluginRegistry()
    manifest = _manifest("topos.project-disabled")
    assert source.register(manifest, enabled=False)
    snapshot = PluginSnapshotStore.export(source)

    target = PluginRegistry()
    target_plugin = _Plugin()
    assert target.register(manifest, target_plugin)
    assert target.activate(manifest.plugin_id)
    report = PluginSnapshotStore.restore_project_state(snapshot, target)

    assert report.ok
    assert target.get(manifest.plugin_id).state == PluginState.DISABLED
    assert target_plugin.active is False


def test_restore_rejects_tampered_manifest_content_hash() -> None:
    source = PluginRegistry()
    manifest = _manifest("topos.tampered-manifest")
    assert source.register(manifest)
    snapshot = PluginSnapshotStore.export(source)
    snapshot["plugins"][0]["manifest"]["content_hash"] = "0" * 64

    report = PluginSnapshotStore.restore(snapshot, PluginRegistry())

    assert not report.ok
    assert "content hash mismatch" in report.skipped_entries[0]


def test_unknown_plugin_is_retained_as_degraded_and_legacy_snapshot_migrates() -> None:
    source = PluginRegistry()
    manifest = _manifest("topos.unknown")
    assert source.register(manifest)
    assert source.activate(manifest.plugin_id)
    legacy = source.snapshot()

    restored = PluginRegistry()
    report = PluginSnapshotStore.restore(legacy, restored)
    assert report.migrated_from == "topos-plugin-registry/1.0"
    assert report.unknown_plugins == (manifest.plugin_id,)
    assert restored.get(manifest.plugin_id).state == PluginState.DEGRADED


def test_disabled_unknown_plugin_stays_disabled_and_bad_schema_is_skipped() -> None:
    source = PluginRegistry()
    manifest = _manifest("topos.disabled")
    assert source.register(manifest, enabled=False)
    snapshot = PluginSnapshotStore.export(source)
    restored = PluginRegistry()
    report = PluginSnapshotStore.restore(snapshot, restored)
    assert report.ok
    assert restored.get(manifest.plugin_id).state == PluginState.DISABLED

    bad = PluginSnapshotStore.restore({"schema_version": "future/9"}, PluginRegistry())
    assert not bad.ok
    assert "unsupported plugin snapshot schema" in bad.skipped_entries[0]


def test_restore_rejects_invalid_plugin_entry_without_partial_activation() -> None:
    snapshot = {
        "schema_version": PLUGIN_PROJECT_SCHEMA,
        "plugins": [{"manifest": {"plugin_id": "bad id"}, "state": "active"}],
    }
    registry = PluginRegistry()
    report = PluginSnapshotStore.restore(snapshot, registry)
    assert not report.ok
    assert registry.list() == ()


def test_manifest_migration_chain_restores_only_target_version() -> None:
    source = PluginRegistry()
    manifest = _manifest(version="1.0.0")
    assert source.register(manifest, policy=PluginExecutionPolicy(max_output_chars=1234))
    snapshot = PluginSnapshotStore.export(source)

    migrations = PluginMigrationRegistry()
    assert migrations.register(
        manifest.plugin_id,
        "1.0.0",
        "1.1.0",
        lambda raw: {
            **raw,
            "metadata": {**dict(raw.get("metadata", {})), "migrated": True},
        },
    )
    assert migrations.register(
        manifest.plugin_id,
        "1.1.0",
        "2.0.0",
        lambda raw: raw,
    )

    restored = PluginRegistry()
    report = PluginSnapshotStore.restore(
        snapshot,
        restored,
        resolver={manifest.plugin_id: _Plugin()},
        migrations=migrations,
        target_versions={manifest.plugin_id: "2.0.0"},
    )

    assert report.ok
    assert report.migrated_plugins == (manifest.plugin_id,)
    assert restored.get(manifest.plugin_id).manifest.version == "2.0.0"
    assert restored.get(manifest.plugin_id).manifest.metadata["migrated"] is True
    assert restored.get(manifest.plugin_id).state == PluginState.ENABLED


def test_missing_manifest_migration_blocks_restore_without_loading_old_schema() -> None:
    source = PluginRegistry()
    manifest = _manifest("topos.old", version="1.0.0")
    assert source.register(manifest)
    snapshot = PluginSnapshotStore.export(source)

    restored = PluginRegistry()
    report = PluginSnapshotStore.restore(
        snapshot,
        restored,
        migrations=PluginMigrationRegistry(),
        target_versions={manifest.plugin_id: "2.0.0"},
    )

    assert not report.ok
    assert report.migration_errors == (
        "topos.old:1.0.0->2.0.0:migration_missing",
    )
    assert restored.list() == ()


def test_snapshot_journal_is_bounded_and_rolls_back_to_a_checkpoint() -> None:
    journal = PluginSnapshotJournal(max_checkpoints=2)
    first = journal.checkpoint(PluginSnapshotStore.export(PluginRegistry(), project_id="one"))
    second = journal.checkpoint(PluginSnapshotStore.export(PluginRegistry(), project_id="two"))
    third = journal.checkpoint(PluginSnapshotStore.export(PluginRegistry(), project_id="three"))

    assert len(journal.list()) == 2
    assert first.checkpoint_id not in {item.checkpoint_id for item in journal.list()}
    assert len(third.snapshot_hash) == 64
    rolled_back = journal.rollback(second.checkpoint_id)
    assert rolled_back is not None
    assert rolled_back["project_id"] == "two"
    assert journal.current_id == second.checkpoint_id
    assert journal.to_dict()["schema_version"] == "topos-plugin-journal/1.0"


def test_snapshot_journal_roundtrips_and_rejects_tampered_checkpoint() -> None:
    journal = PluginSnapshotJournal(max_checkpoints=3)
    checkpoint = journal.checkpoint(PluginSnapshotStore.export(PluginRegistry(), project_id="safe"))
    payload = journal.to_dict()

    restored, report = PluginSnapshotJournal.from_dict(payload)
    assert report.ok
    assert restored.current_id == checkpoint.checkpoint_id
    assert restored.rollback(checkpoint.checkpoint_id)["project_id"] == "safe"

    tampered = journal.to_dict()
    tampered["checkpoints"][0]["snapshot"]["project_id"] = "tampered"
    _, tampered_report = PluginSnapshotJournal.from_dict(tampered)
    assert not tampered_report.ok
    assert "hash_mismatch" in tampered_report.skipped_entries[0]
