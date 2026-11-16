"""PL-01 回归：共享 manifest schema、解析与校验（发现永不执行插件代码）。"""
from __future__ import annotations

import json

import pytest

from src.shared.plugins.manifest import (
    CONTRIBUTION_KINDS,
    PluginCapability,
    PluginManifest,
    legacy_python_manifest,
    parse_manifest_file,
    parse_manifest_mapping,
    validate_manifest_set,
)


def _base(**overrides):
    data = {
        "plugin_id": "files.format_factory",
        "display_name": "格式工厂",
        "version": "1.2.0",
        "capabilities": [
            {"capability_id": "launch", "kind": "command", "selection_mode": "single",
             "applies_to_kinds": ["video"]},
        ],
        "windows": [{"window_id": "main", "command_id": "launch"}],
    }
    data.update(overrides)
    return data


# ---------------------------------------------------------------------------
# 解析与身份
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad_id", ["Format Factory", "files", "files.", " nope.a", ".a.b", "a b.c"])
def test_plugin_id_must_be_namespaced_lowercase(bad_id):
    manifest, diagnostics = parse_manifest_mapping(_base(plugin_id=bad_id))
    assert manifest is None
    assert any(d.code == "PLUGIN_ID_INVALID" for d in diagnostics)


def test_valid_manifest_parses_with_defaults():
    manifest, diagnostics = parse_manifest_mapping(_base())
    assert diagnostics == ()
    assert manifest.plugin_id == "files.format_factory"
    assert manifest.host_api == (1, 1)
    assert manifest.capabilities[0].kind == "command"
    assert manifest.capabilities[0].available is True
    assert manifest.windows[0].command_id == "launch"


@pytest.mark.parametrize("field,value,code", [
    ("version", "1.2", "VERSION_INVALID"),
    ("display_name", "  ", "DISPLAY_NAME_MISSING"),
    ("host_api", [2], "HOST_API_INVALID"),
    ("host_api", [3, 2], "HOST_API_RANGE_INVALID"),
    ("topos_min_version", "abc", "TOPOS_MIN_VERSION_INVALID"),
    ("dependencies", "files.other", "FIELD_NOT_STRING_LIST"),
])
def test_field_validation_errors(field, value, code):
    manifest, diagnostics = parse_manifest_mapping(_base(**{field: value}))
    assert manifest is None
    assert any(d.code == code for d in diagnostics)


def test_unknown_fields_survive_round_trip():
    manifest, _ = parse_manifest_mapping(_base(metadata={"future_key": {"nested": 1}},
                                              extra_future_field="keep-me"))
    assert manifest.unknown_fields["extra_future_field"] == "keep-me"
    assert manifest.metadata["future_key"] == {"nested": 1}
    assert manifest.to_dict()["unknown_fields"]["extra_future_field"] == "keep-me"


def test_future_schema_version_is_warning_not_silent():
    manifest, diagnostics = parse_manifest_mapping(_base(schema_version="9.9"))
    assert manifest is not None
    assert any(d.code == "SCHEMA_VERSION_UNKNOWN" and d.severity == "warning"
               for d in diagnostics)


def test_unknown_contribution_kind_is_preserved_and_unavailable():
    manifest, diagnostics = parse_manifest_mapping(_base(capabilities=[
        {"capability_id": "holo", "kind": "hologram", "description": "future"},
        {"capability_id": "launch", "kind": "command"},
    ]))
    assert manifest is not None
    kinds = {c.capability_id: c for c in manifest.capabilities}
    assert kinds["holo"].kind == "hologram"
    assert kinds["holo"].available is False          # 保留但不可用，不悄悄丢弃
    assert kinds["launch"].available is True
    assert any(d.code == "CAPABILITY_KIND_UNKNOWN" for d in diagnostics)


def test_secrets_and_user_paths_are_rejected():
    _, diagnostics = parse_manifest_mapping(_base(metadata={"api_key": "x"}))
    assert any(d.code == "SECRET_IN_METADATA" for d in diagnostics)
    _, diagnostics = parse_manifest_mapping(_base(description="see /Users/someone/photos"))
    assert any(d.code == "USER_PATH_IN_MANIFEST" for d in diagnostics)


def test_metadata_mapping_is_frozen():
    manifest, _ = parse_manifest_mapping(_base(metadata={"a": 1}))
    with pytest.raises(TypeError):
        manifest.metadata["b"] = 2                     # type: ignore[index]


# ---------------------------------------------------------------------------
# 文件解析：有界、不执行
# ---------------------------------------------------------------------------

def test_manifest_file_roundtrip(tmp_path):
    path = tmp_path / "plugin.json"
    path.write_text(json.dumps(_base()), encoding="utf-8")
    manifest, diagnostics = parse_manifest_file(path)
    assert diagnostics == ()
    assert manifest.plugin_id == "files.format_factory"


def test_manifest_file_rejects_oversized_and_malformed(tmp_path):
    big = tmp_path / "big.json"
    big.write_text("x" * (1024 * 1024 + 1), encoding="utf-8")
    manifest, diagnostics = parse_manifest_file(big)
    assert manifest is None and any(d.code == "MANIFEST_TOO_LARGE" for d in diagnostics)
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    manifest, diagnostics = parse_manifest_file(broken)
    assert manifest is None and any(d.code == "MANIFEST_UNREADABLE" for d in diagnostics)


def test_legacy_python_discovery_never_executes_plugin_code(tmp_path):
    """PL-01 验收：扫描阶段不能执行插件顶层代码（写入即失败）。"""
    folder = tmp_path / "spy_plugin"
    folder.mkdir()
    (folder / "__init__.py").write_text(
        "NAME = 'Spy'\n"
        "VERSION = '0.1.0'\n"
        "AUTHOR = 'x'\n"
        "DESCRIPTION = 'writes at import'\n"
        "API_VERSION = 1\n"
        f"open({str(tmp_path / 'PWNED')!r}, 'w').write('executed')\n",
        encoding="utf-8",
    )
    manifest, diagnostics = legacy_python_manifest(folder)
    assert (tmp_path / "PWNED").exists() is False      # 从未执行
    assert manifest.display_name == "Spy"
    assert manifest.plugin_id == "files.spy_plugin"
    assert manifest.legacy_ids == ("spy_plugin",)
    assert diagnostics == ()


def test_legacy_python_manifest_handles_bad_api_version(tmp_path):
    folder = tmp_path / "weird"
    folder.mkdir()
    (folder / "__init__.py").write_text(
        "NAME = 'Weird'\nAPI_VERSION = 'one'\n", encoding="utf-8")
    manifest, diagnostics = legacy_python_manifest(folder)
    assert manifest is not None
    assert manifest.host_api == (0, 0)                 # 永不与真实宿主区间相交
    assert any(d.code == "LEGACY_API_VERSION_INVALID" for d in diagnostics)


# ---------------------------------------------------------------------------
# 集合级校验：兼容、依赖、循环
# ---------------------------------------------------------------------------

def _m(plugin_id, **kw):
    manifest, diagnostics = parse_manifest_mapping(_base(plugin_id=plugin_id, **kw))
    assert manifest is not None and diagnostics == ()
    return manifest


HOST = dict(host_api=1, topos_version="1.6.0", platform="darwin", arch="x86_64")


def test_duplicate_plugin_and_legacy_id_conflict():
    findings = validate_manifest_set((_m("files.a", legacy_ids=["old_a"]),
                                      _m("files.a")), **HOST)
    assert any(d.code == "PLUGIN_ID_DUPLICATE" for d in findings["files.a"])
    findings = validate_manifest_set((_m("files.a", legacy_ids=["shared"]),
                                      _m("files.b", legacy_ids=["shared"])), **HOST)
    assert any(d.code == "LEGACY_ID_CONFLICT" for d in findings["files.b"])


def test_host_compatibility_rules():
    findings = validate_manifest_set((_m("files.x", host_api=[2, 3]),), **HOST)
    assert any(d.code == "HOST_API_INCOMPATIBLE" for d in findings["files.x"])
    findings = validate_manifest_set((_m("files.x", topos_min_version="2.0"),), **HOST)
    assert any(d.code == "TOPOS_VERSION_INCOMPATIBLE" for d in findings["files.x"])
    findings = validate_manifest_set((_m("files.x", platform={"platforms": ["windows"]}),), **HOST)
    assert any(d.code == "PLATFORM_INCOMPATIBLE" for d in findings["files.x"])


def test_dependencies_missing_optional_and_cycles():
    findings = validate_manifest_set((_m("files.a", dependencies=["files.missing"]),), **HOST)
    assert any(d.code == "DEPENDENCY_MISSING" for d in findings["files.a"])
    findings = validate_manifest_set((_m("files.a", optional_dependencies=["files.no"]),), **HOST)
    assert any(d.code == "OPTIONAL_DEPENDENCY_MISSING" and d.severity == "warning"
               for d in findings["files.a"])
    findings = validate_manifest_set((_m("files.a", dependencies=["files.b"]),
                                      _m("files.b", dependencies=["files.a"])), **HOST)
    assert any(d.code == "DEPENDENCY_CYCLE" for d in findings["files.a"])
    assert any(d.code == "DEPENDENCY_CYCLE" for d in findings["files.b"])


def test_window_must_reference_command_and_provider_provides_valid():
    findings = validate_manifest_set(
        (_m("files.x", capabilities=[{"capability_id": "launch", "kind": "command"}],
            windows=[{"window_id": "w", "command_id": "nope"}]),), **HOST)
    assert any(d.code == "WINDOW_COMMAND_MISSING" for d in findings["files.x"])
    findings = validate_manifest_set(
        (_m("files.x", capabilities=[{"capability_id": "p", "kind": "format_provider"}],
            format_providers=[{"provider_id": "p", "provides": ["teleport"]}]),), **HOST)
    assert any(d.code == "FORMAT_PROVIDES_UNKNOWN" for d in findings["files.x"])


def test_contribution_kinds_are_the_planned_set():
    assert set(CONTRIBUTION_KINDS) == {"command", "window", "background_service",
                                       "format_provider", "inspector", "menu"}
