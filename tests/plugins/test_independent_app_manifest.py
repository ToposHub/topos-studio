"""Independent apps are inert declarations until the host explicitly launches."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.shared.plugins.discovery import PluginSourceConfig, scan_sources, source_label
from src.shared.plugins.independent_apps import (
    IndependentAppError, discover_independent_apps, fingerprint_independent_app,
    load_independent_app,
)
from src.shared.plugins.inventory import PluginInventory

HOST = dict(host_api=1, topos_version="3.0.0", platform="darwin", arch="arm64")


def _package(base: Path, name: str = "word", **app_fields: object) -> Path:
    root = base / name
    root.mkdir(parents=True)
    (root / "main.py").write_text(
        "raise RuntimeError('discovery must never execute this')\n", encoding="utf-8")
    declaration: dict[str, object] = {
        "protocol": 1, "entrypoint": "main.py", "extensions": [".DOCX", ".docx"],
        "required_modules": {"docx": "python-docx"},
    }
    declaration.update(app_fields)
    (root / "topos.plugin.json").write_text(json.dumps({
        "plugin_id": "files.word", "display_name": "Word", "version": "1.0.0",
        "capabilities": [{"capability_id": "open", "kind": "command", "run_location": "process"}],
        "metadata": {"independent_app": declaration},
    }), encoding="utf-8")
    return root


def _modify_manifest(root: Path, **fields: object) -> None:
    path = root / "topos.plugin.json"
    declaration = json.loads(path.read_text(encoding="utf-8"))
    declaration.update(fields)
    path.write_text(json.dumps(declaration), encoding="utf-8")


def test_load_is_declarative_normalized_and_immutable(tmp_path: Path) -> None:
    root = _package(tmp_path)
    spec = load_independent_app(root, host_facts=HOST)
    assert spec.plugin_id == "files.word"
    assert spec.root == root.resolve()
    assert spec.entrypoint == root.resolve() / "main.py"
    assert spec.extensions == (".docx",)
    assert spec.required_modules == {"docx": "python-docx"}
    with pytest.raises(TypeError):
        spec.required_modules["bad"] = "bad"  # type: ignore[index]


@pytest.mark.parametrize("protocol", [True, False, 1.0, "1", 2, None])
def test_protocol_requires_exact_supported_integer(tmp_path: Path, protocol: object) -> None:
    root = _package(tmp_path, protocol=protocol)
    with pytest.raises(IndependentAppError, match="协议版本"):
        load_independent_app(root, host_facts=HOST)


@pytest.mark.parametrize("entrypoint", ["../main.py", "/tmp/main.py", "a/../main.py",
    "a\\main.py", "main.py/", "./main.py", "main.txt", ".venv/main.py", "missing.py"])
def test_entrypoint_must_be_an_existing_in_package_python_file(
    tmp_path: Path, entrypoint: str,
) -> None:
    root = _package(tmp_path, entrypoint=entrypoint)
    with pytest.raises(IndependentAppError, match="entrypoint"):
        load_independent_app(root, host_facts=HOST)


@pytest.mark.parametrize("field,value", [
    ("extensions", []), ("extensions", ["docx"]), ("extensions", [1]),
    ("extensions", "docx"), ("required_modules", []),
    ("required_modules", {"bad/name": "package"}),
    ("required_modules", {"valid": 1}),
    ("format_modules", {".fbx": {"assimp_py": "assimp-py"}}),
])
def test_invalid_field_shapes_rejected(tmp_path: Path, field: str, value: object) -> None:
    root = _package(tmp_path, **{field: value})
    with pytest.raises(IndependentAppError):
        load_independent_app(root, host_facts=HOST)


def test_optional_format_modules_are_deeply_frozen(tmp_path: Path) -> None:
    root = _package(tmp_path, extensions=[".fbx"],
                    format_modules={".fbx": {"assimp_py": "assimp-py"}})
    spec = load_independent_app(root, host_facts=HOST)
    assert spec.format_modules[".fbx"]["assimp_py"] == "assimp-py"
    with pytest.raises(TypeError):
        spec.format_modules[".fbx"]["bad"] = "bad"  # type: ignore[index]


def test_shared_manifest_host_compatibility_still_applies(tmp_path: Path) -> None:
    root = _package(tmp_path)
    _modify_manifest(root, host_api=[2, 2])
    with pytest.raises(IndependentAppError, match="API"):
        load_independent_app(root, host_facts=HOST)


def test_shared_manifest_error_is_not_ignored(tmp_path: Path) -> None:
    root = _package(tmp_path)
    _modify_manifest(root, schema_version="99.0")
    with pytest.raises(IndependentAppError, match="schema"):
        load_independent_app(root, host_facts=HOST)


def test_process_command_is_required(tmp_path: Path) -> None:
    root = _package(tmp_path)
    _modify_manifest(root, capabilities=[{"kind": "command", "capability_id": "open"}])
    with pytest.raises(IndependentAppError, match="run_location"):
        load_independent_app(root, host_facts=HOST)


@pytest.mark.parametrize("target", ["main.py", "topos.plugin.json", "resources"])
def test_symlinks_rejected_even_for_internal_targets(tmp_path: Path, target: str) -> None:
    root = _package(tmp_path)
    path = root / target
    if path.exists():
        path.rename(root / f"original-{target}")
        path.symlink_to(root / f"original-{target}")
    else:
        path.symlink_to(tmp_path)
    with pytest.raises(IndependentAppError, match="符号链接"):
        load_independent_app(root, host_facts=HOST)


def test_fingerprint_changes_for_source_and_resource_but_not_generated_cache(tmp_path: Path) -> None:
    root = _package(tmp_path)
    original = fingerprint_independent_app(root)
    assert fingerprint_independent_app(root) == original
    cache = root / "__pycache__"
    cache.mkdir()
    (cache / "main.cpython-312.pyc").write_bytes(b"generated")
    assert fingerprint_independent_app(root) == original
    (root / "icon.svg").write_text("<svg/>", encoding="utf-8")
    with_resource = fingerprint_independent_app(root)
    assert with_resource != original
    (root / "main.py").write_text("print('updated')", encoding="utf-8")
    assert fingerprint_independent_app(root) != with_resource


def test_fingerprint_is_independent_of_absolute_location(tmp_path: Path) -> None:
    import shutil
    first = _package(tmp_path / "first")
    second = tmp_path / "second"
    shutil.copytree(first, second)
    assert fingerprint_independent_app(first) == fingerprint_independent_app(second)


@pytest.mark.parametrize("folder", ["cache", ".cache", "venv", ".venv", "__pycache__"])
def test_cache_named_directories_cannot_hide_source_changes(tmp_path: Path, folder: str) -> None:
    root = _package(tmp_path)
    source = root / folder / "payload.py"
    source.parent.mkdir()
    source.write_text("VALUE = 1\n")
    original = fingerprint_independent_app(root)
    source.write_text("VALUE = 2\n")
    assert fingerprint_independent_app(root) != original


def test_package_size_and_count_are_bounded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import src.shared.plugins.independent_apps as apps
    root = _package(tmp_path)
    monkeypatch.setattr(apps, "MAX_PACKAGE_BYTES", 5)
    with pytest.raises(IndependentAppError, match="体积限制"):
        fingerprint_independent_app(root)
    monkeypatch.setattr(apps, "MAX_PACKAGE_BYTES", 1024 * 1024)
    monkeypatch.setattr(apps, "MAX_PACKAGE_FILES", 1)
    with pytest.raises(IndependentAppError, match="数量"):
        fingerprint_independent_app(root)


def test_discovery_preserves_invalid_apps_and_ignores_ordinary_directories(tmp_path: Path) -> None:
    valid = _package(tmp_path)
    invalid = _package(tmp_path, name="bad", protocol=2)
    _modify_manifest(invalid, plugin_id="files.bad")
    (tmp_path / "ordinary").mkdir()
    records = discover_independent_apps([tmp_path, tmp_path], host_facts=HOST)
    assert len(records) == 2
    by_id = {record.plugin_id: record for record in records}
    assert by_id["files.word"].origin_path == str(valid.resolve())
    assert by_id["files.word"].source == "local_app"
    assert by_id["files.word"].desired_enabled is True
    assert by_id["files.word"].runtime_state == "not_loaded"
    assert by_id["files.bad"].error_code == "INDEPENDENT_APP_INVALID"
    assert by_id["files.bad"].runtime_state == "rejected"


def test_discovery_never_trusts_manifest_source_or_loads_code(tmp_path: Path) -> None:
    root = _package(tmp_path)
    marker = tmp_path / "executed"
    (root / "main.py").write_text(f"open({str(marker)!r}, 'w').write('bad')", encoding="utf-8")
    _modify_manifest(root, source="builtin")
    records = discover_independent_apps([tmp_path], {"files.word": False}, host_facts=HOST)
    assert records[0].source == "local_app"
    assert records[0].desired_enabled is False
    assert not marker.exists()
    assert source_label("local_app") == "本地独立应用"


def test_scan_sources_aggregates_local_apps_and_exposes_identity_conflicts(tmp_path: Path) -> None:
    _package(tmp_path / "apps", name="first")
    _package(tmp_path / "apps", name="second")
    config = PluginSourceConfig.for_host(
        builtin_ids=(), install_root=tmp_path / "installed", app_roots=(tmp_path / "apps",))
    records = scan_sources((), config, host_facts=HOST)
    snapshot = PluginInventory(**HOST).refresh(records)
    assert len(snapshot.records) == 1
    assert any(note.code == "PLUGIN_ID_DUPLICATE" for note in snapshot.records[0].diagnostics)
