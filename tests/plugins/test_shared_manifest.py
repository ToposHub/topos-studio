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
    manifest, diagnostics = parse_manifest_mapping(
        _base(metadata={"future_key": {"nested": 1}}, extra_future_field="keep-me"))
    assert diagnostics == ()
    assert manifest.unknown_fields["extra_future_field"] == "keep-me"
    assert manifest.metadata["future_key"] == {"nested": 1}
    # 往返：to_dict 把未知字段原样回填顶层，再解析不丢、不变形（PR-04 复现 1）
    round_tripped, round_diagnostics = parse_manifest_mapping(manifest.to_dict())
    assert round_diagnostics == ()
    assert round_tripped.unknown_fields == {"extra_future_field": "keep-me"}
    assert round_tripped.metadata == manifest.metadata
    assert round_tripped.to_dict() == manifest.to_dict()


def test_capability_unknown_fields_survive_round_trip():
    manifest, _ = parse_manifest_mapping(_base(capabilities=[
        {"capability_id": "launch", "kind": "command", "future_cap_field": {"deep": [1, 2]}},
    ]))
    capability = manifest.capabilities[0]
    assert capability.unknown_fields["future_cap_field"] == {"deep": (1, 2)}   # 冻结形态：列表→元组
    round_tripped, _ = parse_manifest_mapping(manifest.to_dict())             # 序列化回到 JSON 形态
    assert round_tripped.capabilities[0].unknown_fields == capability.unknown_fields
    assert round_tripped.to_dict() == manifest.to_dict()


def test_manifest_is_deeply_immutable():
    """复现 2：修改输入的嵌套 schema 不得改变已解析的 manifest。"""
    nested = {"properties": {"threshold": {"type": "number"}}}
    manifest, _ = parse_manifest_mapping(_base(capabilities=[
        {"capability_id": "launch", "kind": "command", "params_schema": nested,
         "resource_policy": {"max_mb": 512}}]))
    nested["properties"]["threshold"]["type"] = "string"       # 改输入
    capability = manifest.capabilities[0]
    assert capability.params_schema["properties"]["threshold"]["type"] == "number"
    with pytest.raises(TypeError):
        capability.params_schema["properties"]["threshold"]["type"] = "evil"  # type: ignore[index]
    with pytest.raises(TypeError):
        capability.resource_policy["max_mb"] = 1               # type: ignore[index]


def test_future_schema_version_is_preserved_but_blocked():
    """复现 3：未来 schema 声明保留，但阻断级诊断使其不可安装/启用。"""
    manifest, diagnostics = parse_manifest_mapping(_base(schema_version="999.0"))
    assert manifest is not None and manifest.schema_version == "999.0"
    assert any(d.code == "SCHEMA_VERSION_UNSUPPORTED" and d.severity == "error"
               for d in diagnostics)
    # 经由 to_dict 往返后策略一致，不会因为序列化降级
    again, again_diagnostics = parse_manifest_mapping(manifest.to_dict())
    assert any(d.code == "SCHEMA_VERSION_UNSUPPORTED" and d.severity == "error"
               for d in again_diagnostics)


def test_plugin_id_is_strict_full_match():
    for bad_id in ["files.a\n", "files.a ", "files.a/b", "FILES.a"]:
        manifest, diagnostics = parse_manifest_mapping(_base(plugin_id=bad_id))
        assert manifest is None
        assert any(d.code == "PLUGIN_ID_INVALID" for d in diagnostics)


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


@pytest.mark.parametrize("fields,code", [
    ({"windows": 1}, "WINDOWS_NOT_LIST"),
    ({"format_providers": "nope"}, "FORMAT_PROVIDERS_NOT_LIST"),
    ({"legacy_ids": True}, "LEGACY_ID_INVALID"),
    ({"platform": {"arches": 1}}, "PLATFORM_FIELD_INVALID"),   # 损坏平台声明不得默认"无限制"
    ({"author": 42}, "FIELD_TYPE_INVALID"),
    ({"metadata": "not-an-object"}, "METADATA_NOT_OBJECT"),
])
def test_malformed_top_level_fields_reject_manifest(tmp_path, fields, code):
    import json
    from src.shared.plugins.manifest import parse_manifest_file
    path = tmp_path / "topos.plugin.json"
    path.write_text(json.dumps(
        {"plugin_id": "files.bad", "display_name": "Bad", "version": "1.0.0", **fields}),
        encoding="utf-8")
    manifest, diagnostics = parse_manifest_file(path)
    matching = [item for item in diagnostics if item.code == code and item.severity == "error"]
    assert matching and manifest is None
    assert all(item.field for item in matching)                # 诊断必须带字段路径


@pytest.mark.parametrize("fields,code,path", [
    ({"format_providers": [{"provider_id": "p", "priority": "bad-number"}]},
     "PROVIDER_FIELD_INVALID", "format_providers[0].priority"),
    ({"background_services": [{"service_id": "s", "scope": "galaxy"}]},
     "SERVICE_FIELD_INVALID", "background_services[0].scope"),
    ({"capabilities": [{"capability_id": "c", "kind": "command", "cancellable": "yes"}]},
     "CAPABILITY_FIELD_TYPE_INVALID", "capabilities[0].cancellable"),
])
def test_malformed_items_are_dropped_but_manifest_is_kept(tmp_path, fields, code, path):
    """条目级错误：条目被拒、诊断带路径，manifest 其余部分保留可见。"""
    import json
    from src.shared.plugins.manifest import parse_manifest_file
    manifest_path = tmp_path / "topos.plugin.json"
    manifest_path.write_text(json.dumps(
        {"plugin_id": "files.bad", "display_name": "Bad", "version": "1.0.0", **fields}),
        encoding="utf-8")
    manifest, diagnostics = parse_manifest_file(manifest_path)
    assert manifest is not None
    assert any(item.code == code and item.field == path for item in diagnostics)


@pytest.mark.parametrize("window,path_field", [
    ({"window_id": " ", "command_id": "launch"}, "windows[0].window_id"),
    ({"window_id": "w", "command_id": ""}, "windows[0].command_id"),
    ({"window_id": "w", "command_id": "launch", "min_size": [0, 240]}, "windows[0].min_size"),
    ({"window_id": "w", "command_id": "launch", "min_size": [320]}, "windows[0].min_size"),
    ({"window_id": "w", "command_id": "launch", "single_instance": "yes"}, "windows[0].single_instance"),
    ({"window_id": "w", "command_id": "launch", "ownership": "alien"}, "windows[0].ownership"),
])
def test_window_fields_validate_with_paths(tmp_path, window, path_field):
    import json
    from src.shared.plugins.manifest import parse_manifest_file
    path = tmp_path / "topos.plugin.json"
    path.write_text(json.dumps({"plugin_id": "files.bad", "display_name": "Bad",
                                "version": "1.0.0", "windows": [window]}), encoding="utf-8")
    manifest, diagnostics = parse_manifest_file(path)
    assert manifest is not None                                # 窗口本身被拒，manifest 其余保留
    assert any(item.code == "WINDOW_FIELD_INVALID" and item.field == path_field
               for item in diagnostics)
