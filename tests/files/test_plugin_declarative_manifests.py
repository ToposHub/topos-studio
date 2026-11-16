"""PL-04/05 回归：内建插件声明式 manifest 与 catalog 优先读取。"""
from __future__ import annotations

import json
from pathlib import Path

from src.app.integrations.file_explorer.plugins import FileExplorerPluginCatalog
from src.shared.plugins.manifest import parse_manifest_file, validate_manifest_set

REPO = Path(__file__).resolve().parents[2]
PLUGINS_DIR = REPO / "external" / "file_explorer" / "plugins"
BUILTIN_IDS = ("format_factory", "image_factory", "shutdown_timer", "decode_benchmark",
               "code_editor", "image_editor", "ai_library_browser", "audio_pro",
               "video_pro", "topos_image")
HOST = dict(host_api=1, topos_version="1.6.0", platform="darwin", arch="x86_64")


def test_all_ten_builtin_manifests_parse_and_validate_cleanly():
    manifests = []
    for plugin_id in BUILTIN_IDS:
        manifest, diagnostics = parse_manifest_file(PLUGINS_DIR / plugin_id / "topos.plugin.json")
        assert manifest is not None, plugin_id
        assert diagnostics == (), (plugin_id, diagnostics)
        assert manifest.plugin_id == f"files.{plugin_id}"
        assert manifest.legacy_ids == (plugin_id,)
        manifests.append(manifest)
    findings = validate_manifest_set(tuple(manifests), **HOST)
    for plugin_id in BUILTIN_IDS:
        blocking = [d for d in findings[f"files.{plugin_id}"] if d.severity == "error"]
        assert blocking == [], (plugin_id, blocking)


def test_launch_commands_declare_windows_and_selection():
    expected = {"format_factory": "path", "image_factory": "none",
                "shutdown_timer": "none", "decode_benchmark": "paths"}
    for plugin_id, kind in expected.items():
        manifest, _ = parse_manifest_file(PLUGINS_DIR / plugin_id / "topos.plugin.json")
        commands = [c for c in manifest.capabilities if c.kind == "command"]
        assert len(commands) == 1, plugin_id
        assert {"path": "single", "paths": "many"}.get(kind, "none") == commands[0].selection_mode
        assert manifest.windows and manifest.windows[0].command_id == commands[0].capability_id


def test_format_providers_declared_separately_from_playback():
    video, _ = parse_manifest_file(PLUGINS_DIR / "video_pro" / "topos.plugin.json")
    provider = video.format_providers[0]
    assert provider.formats == ("topos",)
    assert "playback" not in provider.provides          # 播放复用共享播放器，不新增引擎
    assert set(provider.provides) <= {"probe", "metadata", "thumbnail"}
    topos_image, _ = parse_manifest_file(PLUGINS_DIR / "topos_image" / "topos.plugin.json")
    assert topos_image.format_providers[0].formats == ("toos",)
    assert [c.kind for c in topos_image.capabilities] == ["format_provider"]
    # 无 command 声明的插件不得伪造启动路由
    for plugin_id in ("code_editor", "image_editor", "ai_library_browser", "audio_pro",
                      "video_pro", "topos_image"):
        manifest, _ = parse_manifest_file(PLUGINS_DIR / plugin_id / "topos.plugin.json")
        assert not any(c.kind == "command" for c in manifest.capabilities), plugin_id


def test_real_catalog_prefers_manifest_declarations():
    from types import SimpleNamespace
    loaded = SimpleNamespace(plugins=[{"name": plugin_id} for plugin_id in BUILTIN_IDS],
                             failed_plugins=[])
    catalog = FileExplorerPluginCatalog(PLUGINS_DIR, loaded)
    rows = {row.plugin_id: row for row in catalog.snapshot()}
    assert set(rows) == set(BUILTIN_IDS)
    assert rows["format_factory"].name == "格式工厂"      # manifest display_name
    assert rows["format_factory"].launch_payload_kind == "path"
    assert rows["decode_benchmark"].version == "1.1.0"
    assert rows["video_pro"].category == "视频"
    assert rows["video_pro"].launch_payload_kind == ""    # 只有 format provider
    assert "格式能力随文件使用" in rows["video_pro"].launch_unavailable_reason
    assert rows["topos_image"].launch_payload_kind == ""
    assert not rows["code_editor"].can_launch             # 旧 Web 工具仍走兼容入口


def test_manifest_identity_mismatch_is_ignored_with_diagnostic(tmp_path):
    folder = tmp_path / "tool"
    folder.mkdir()
    (folder / "__init__.py").write_text("NAME = 'Old'\n", encoding="utf-8")
    (folder / "topos.plugin.json").write_text(json.dumps(
        {"plugin_id": "files.other", "display_name": "Imposter", "version": "9.9.9"}),
        encoding="utf-8")
    row = FileExplorerPluginCatalog(tmp_path).snapshot()[0]
    assert row.plugin_id == "tool"
    assert row.name == "Old"                              # 回退到 AST 常量
    assert any("身份" in note for note in row.diagnostics)


def test_unknown_command_kind_never_gets_a_route(tmp_path):
    folder = tmp_path / "future"
    folder.mkdir()
    (folder / "__init__.py").write_text("NAME = 'Future'\n", encoding="utf-8")
    (folder / "topos.plugin.json").write_text(json.dumps(
        {"plugin_id": "files.future", "display_name": "Future", "version": "1.0.0",
         "capabilities": [{"capability_id": "holo", "kind": "hologram"}]}), encoding="utf-8")
    row = FileExplorerPluginCatalog(tmp_path).snapshot()[0]
    assert row.launch_payload_kind == ""
    assert row.launch_unavailable_reason                    # 明确不可启动，不是空白
    assert any("未知贡献类型" in note for note in row.diagnostics)


def test_broken_manifest_falls_back_to_legacy_scan(tmp_path):
    folder = tmp_path / "broken"
    folder.mkdir()
    (folder / "__init__.py").write_text("NAME = 'StillWorks'\nVERSION = '3.1.4'\n", encoding="utf-8")
    (folder / "topos.plugin.json").write_text("{ broken", encoding="utf-8")
    row = FileExplorerPluginCatalog(tmp_path).snapshot()[0]
    assert row.name == "StillWorks"
    assert row.version == "3.1.4"
    assert any("manifest" in note for note in row.diagnostics)
