"""Project archive roundtrip for plugin facts and checkpoint journal."""

from __future__ import annotations

from src.features.ai_harness.plugin_persistence import (
    PluginSnapshotJournal,
    PluginSnapshotStore,
)
from src.features.ai_harness.plugin_runtime import PluginManifest, PluginRegistry
from src.shared.project.topos_project import ToposProject
from src.shared.project.topos_project_io import ToposProjectIO


def test_topos_project_roundtrip_preserves_plugin_journal_payload(tmp_path) -> None:
    registry = PluginRegistry()
    manifest = PluginManifest(
        plugin_id="topos.project.plugin",
        display_name="Project plugin",
    )
    assert registry.register(manifest)
    journal = PluginSnapshotJournal(max_checkpoints=2)
    snapshot = PluginSnapshotStore.export(registry, project_id="project-1")
    checkpoint = journal.checkpoint(snapshot)
    plugin_payload = {
        "schema_version": "topos-plugin-state/1.0",
        "snapshot": snapshot,
        "journal": journal.to_dict(),
        "current_checkpoint_id": checkpoint.checkpoint_id,
    }

    path = str(tmp_path / "plugins.topos")
    project = ToposProject.create_single_asset(
        name="Plugin payload",
        asset_path="/tmp/plugin.jpg",
        media_type="image",
    )
    assert ToposProjectIO.save(project, path, ai_plugins_data=plugin_payload)

    _, full_data = ToposProjectIO.load(path, load_full_data=True)
    assert full_data is not None
    loaded = full_data["ai_plugins"]
    assert loaded["snapshot"]["project_id"] == "project-1"
    restored, report = PluginSnapshotJournal.from_dict(loaded["journal"])
    assert report.ok
    assert restored.current_id == checkpoint.checkpoint_id
