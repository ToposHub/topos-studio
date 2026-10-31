"""Regression tests for plugin discovery paths."""

from __future__ import annotations

from pathlib import Path

from src.features.color.core.plugin_manager import PluginManager


def test_default_scan_uses_project_plugins_directory() -> None:
    """The default scan must discover plugins from the repository root."""
    manager = PluginManager()
    manager.plugins.clear()

    plugins = manager.scan_plugins()

    assert "Film_Halation" in plugins
    assert Path(plugins["Film_Halation"]["source_uri"]).parent.name == "plugins"


def test_absolute_plugin_directory_is_preserved() -> None:
    """An explicitly supplied absolute directory must not be re-rooted."""
    manager = PluginManager()
    manager.plugins.clear()
    plugin_dir = Path(__file__).resolve().parents[2] / "plugins"

    plugins = manager.scan_plugins(str(plugin_dir))

    assert Path(plugins["Film_Halation"]["source_uri"]).parent == plugin_dir
