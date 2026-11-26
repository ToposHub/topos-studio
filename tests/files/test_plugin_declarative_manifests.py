"""Validate the manifests served by the native Files catalog."""
from __future__ import annotations

from pathlib import Path

from src.features.files.builtin_plugins import BUILTIN_IDS
from src.shared.plugins.manifest import parse_manifest_file, validate_manifest_set

REPO = Path(__file__).resolve().parents[2]
PLUGINS_DIR = REPO / "src" / "features" / "files" / "builtin_manifests"
HOST = dict(host_api=1, topos_version="1.6.0", platform="darwin", arch="x86_64")


def test_all_ten_builtin_manifests_parse_and_validate_cleanly():
    manifests = []
    for plugin_id in BUILTIN_IDS:
        manifest, diagnostics = parse_manifest_file(PLUGINS_DIR / plugin_id / "topos.plugin.json")
        assert manifest is not None, plugin_id
        assert diagnostics == (), (plugin_id, diagnostics)
        assert manifest.plugin_id == f"files.{plugin_id}"
        assert manifest.legacy_ids == ()
        manifests.append(manifest)
    findings = validate_manifest_set(tuple(manifests), **HOST)
    for plugin_id in BUILTIN_IDS:
        blocking = [d for d in findings[f"files.{plugin_id}"] if d.severity == "error"]
        assert blocking == [], (plugin_id, blocking)


def test_launch_commands_declare_windows_and_selection():
    expected = ("format_factory", "image_factory", "shutdown_timer", "decode_benchmark")
    for plugin_id in expected:
        manifest, _ = parse_manifest_file(PLUGINS_DIR / plugin_id / "topos.plugin.json")
        commands = [c for c in manifest.capabilities if c.kind == "command"]
        assert len(commands) == 1, plugin_id
        assert commands[0].selection_mode == "many"
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
