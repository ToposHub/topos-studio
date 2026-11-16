"""Focused contract tests for the native Files plugin adapter."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from src.app.integrations.file_explorer.plugins import FileExplorerPluginCatalog


def _plugin(root: Path, plugin_id: str, source: str = "NAME = 'Example'\n") -> Path:
    folder = root / plugin_id
    folder.mkdir(parents=True)
    (folder / "__init__.py").write_text(source, encoding="utf-8")
    return folder


def test_inventory_does_not_execute_and_preserves_disabled(tmp_path: Path) -> None:
    marker = tmp_path / "executed"
    _plugin(tmp_path, "disabled", f"NAME = 'Disabled'\nVERSION = '2.0.0'\nopen({str(marker)!r}, 'w').write('ran')\n")
    _plugin(tmp_path, "failed")
    manager = SimpleNamespace(plugins=[], failed_plugins=[{"name": "failed", "error": "broken dependency"}])
    rows = {row.plugin_id: row for row in FileExplorerPluginCatalog(
        tmp_path, manager, lambda: {"disabled_plugins": ["disabled"]},
    ).snapshot()}
    assert not marker.exists()
    assert rows["disabled"].name == "Disabled"
    assert rows["disabled"].version == "2.0.0"
    assert not rows["disabled"].desired_enabled
    assert not rows["disabled"].runtime_loaded
    assert rows["failed"].error == "broken dependency"
    assert not rows["failed"].can_launch


def test_enable_disable_preserves_unknown_fields_and_runtime(tmp_path: Path) -> None:
    _plugin(tmp_path, "image_factory")
    _plugin(tmp_path, "other")
    config: dict[str, object] = {"disabled_plugins": ["other", "missing"], "libraries": ["keep"], "unknown": {"nested": True}}
    manager = SimpleNamespace(plugins=[{"name": "image_factory", "info": {"name": "Runtime name"}}], failed_plugins=[])
    catalog = FileExplorerPluginCatalog(tmp_path, manager, lambda: dict(config), lambda patch: config.update(patch))
    initial = next(row for row in catalog.snapshot() if row.plugin_id == "image_factory")
    assert initial.can_launch
    assert initial.name == "Runtime name"
    disabled = catalog.set_enabled("image_factory", False)
    assert disabled.runtime_loaded and not disabled.desired_enabled and disabled.needs_restart
    assert not disabled.can_launch
    assert config["disabled_plugins"] == ["image_factory", "missing", "other"]
    assert config["unknown"] == {"nested": True}
    assert config["libraries"] == ["keep"]
    enabled = catalog.set_enabled("image_factory", True)
    assert enabled.can_launch and not enabled.needs_restart
    assert manager.plugins == [{"name": "image_factory", "info": {"name": "Runtime name"}}]


def test_self_disabled_cannot_be_overridden(tmp_path: Path) -> None:
    folder = _plugin(tmp_path, "image_factory")
    (folder / "config.json").write_text('{"disabled": true}', encoding="utf-8")
    writes: list[dict[str, object]] = []
    catalog = FileExplorerPluginCatalog(tmp_path, None, lambda: {}, writes.append)
    assert catalog.snapshot()[0].self_disabled
    with pytest.raises(ValueError, match="自身"):
        catalog.set_enabled("image_factory", True)
    assert writes == []


def test_launch_routes_are_allowlisted_and_api_incompatibility_blocks(tmp_path: Path) -> None:
    ids = ("format_factory", "image_factory", "shutdown_timer", "decode_benchmark", "unknown", "code_editor")
    for plugin_id in ids:
        _plugin(tmp_path, plugin_id)
    _plugin(tmp_path, "audio_pro", "API_VERSION = 99\n")
    manager = SimpleNamespace(plugins=[{"name": plugin_id} for plugin_id in (*ids, "audio_pro")], failed_plugins=[])
    rows = {row.plugin_id: row for row in FileExplorerPluginCatalog(tmp_path, manager).snapshot()}
    for plugin_id, kind in {"format_factory": "path", "image_factory": "none", "shutdown_timer": "none", "decode_benchmark": "paths"}.items():
        assert rows[plugin_id].can_launch
        assert rows[plugin_id].launch_path == f"/api/plugin/{plugin_id}/launch"
        assert rows[plugin_id].launch_payload_kind == kind
    assert not rows["unknown"].can_launch and not rows["unknown"].launch_path
    assert not rows["code_editor"].can_launch
    assert not rows["audio_pro"].can_launch
    assert "不兼容" in rows["audio_pro"].launch_unavailable_reason


def test_invalid_metadata_is_reported_without_execution(tmp_path: Path) -> None:
    folder = _plugin(tmp_path, "invalid", "NAME = make_name()\nAPI_VERSION = 'future'\n")
    (folder / "config.json").write_text("[]", encoding="utf-8")
    row = FileExplorerPluginCatalog(tmp_path).snapshot()[0]
    assert row.name == "invalid"
    assert row.api_version is None
    assert len(row.diagnostics) == 3
    assert not row.can_launch


def test_failed_persistence_is_not_reported_as_success(tmp_path: Path) -> None:
    _plugin(tmp_path, "image_factory")
    catalog = FileExplorerPluginCatalog(tmp_path, None, lambda: {}, lambda patch: None)
    with pytest.raises(OSError, match="未能保存"):
        catalog.set_enabled("image_factory", False)
    with pytest.raises(ValueError, match="未知插件"):
        catalog.set_enabled("../escape", False)


def test_invalid_host_disabled_list_does_not_get_overwritten(tmp_path: Path) -> None:
    _plugin(tmp_path, "image_factory")
    writes: list[dict[str, object]] = []
    catalog = FileExplorerPluginCatalog(tmp_path, None, lambda: {"disabled_plugins": "invalid"}, writes.append)
    with pytest.raises(ValueError, match="字符串列表"):
        catalog.set_enabled("image_factory", False)
    assert writes == []


def test_symlinked_external_plugin_is_not_scanned(tmp_path: Path) -> None:
    plugins = tmp_path / "plugins"
    plugins.mkdir()
    outside = _plugin(tmp_path / "outside", "external")
    (plugins / "external").symlink_to(outside, target_is_directory=True)
    assert FileExplorerPluginCatalog(plugins).snapshot() == ()


def test_launch_payload_keeps_selection_and_rechecks_enabled_state(tmp_path: Path) -> None:
    ids = ("format_factory", "image_factory", "decode_benchmark")
    for plugin_id in ids:
        _plugin(tmp_path, plugin_id)
    config: dict[str, object] = {}
    manager = SimpleNamespace(plugins=[{"name": plugin_id} for plugin_id in ids], failed_plugins=[])
    catalog = FileExplorerPluginCatalog(tmp_path, manager, lambda: dict(config), lambda patch: config.update(patch))
    assert catalog.launch_request("format_factory", []) == ("/api/plugin/format_factory/launch", {"path": ""})
    assert catalog.launch_request("format_factory", ["/tmp/one.mov"])[1] == {"path": "/tmp/one.mov"}
    with pytest.raises(ValueError, match="只能接收一个文件"):
        catalog.launch_request("format_factory", ["/tmp/one.mov", "/tmp/two.mov"])
    paths = ["/tmp/one.mov", "/tmp/two.mov"]
    assert catalog.launch_request("decode_benchmark", paths)[1] == {"paths": paths}
    assert catalog.launch_request("image_factory", paths)[1] == {}
    catalog.set_enabled("decode_benchmark", False)
    with pytest.raises(ValueError, match="已停用"):
        catalog.launch_request("decode_benchmark", paths)
