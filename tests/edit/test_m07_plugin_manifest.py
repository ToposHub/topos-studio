"""M07（S09）· 插件版本清单与缺失检查回归。

验收（升级计划 M §M07）：
- 清单遍历真实 ``layer["plugins"]`` 插件实例（此前读 layer 根字段，
  Layer 快照无这些键，清单恒空）；
- 缺失检查用 **Color 插件注册表**（PluginManager），不再误用内置音频
  effect_registry（名空间不交集，检查恒空）；
- 注册表探测失败抛 PluginRegistryUnavailableError / 标 unverified，
  不吞接口错误冒称"无缺失"。
"""

from __future__ import annotations

from types import SimpleNamespace as NS
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.edit_core

from src.features.edit.core.color_versions import (
    PluginRegistryUnavailableError,
    _missing_plugins,
    _plugin_manifest,
)


def _stack(layers):
    return NS(layers=layers)


# ================================================================
# 清单收集：真实 layer.plugins 形态
# ================================================================

class TestPluginManifest:
    def test_reads_layer_plugins_instances(self):
        # layer.py:566 真实形态：{"instance_id","plugin_id","params","enabled"}
        stack = _stack([{"plugins": [
            {"instance_id": "i1", "plugin_id": "vignette",
             "params": {}, "enabled": True},
            {"instance_id": "i2", "plugin_id": "cross_process",
             "params": {}, "enabled": False},
        ]}])
        manifest = _plugin_manifest(stack)
        assert set(manifest) == {"vignette", "cross_process"}

    def test_version_from_registry_when_instance_has_none(self):
        # 实例不带版本 → 从 PluginManager 解析 @version
        fake_registry = {"vignette": {"version": "1.2"}}
        with patch(
            "src.features.color.core.plugin_manager.get_plugin_manager",
            return_value=NS(get_all_plugins=lambda: fake_registry)):
            manifest = _plugin_manifest(_stack([{"plugins": [
                {"plugin_id": "vignette", "enabled": True}]}]))
        assert manifest == {"vignette": "1.2"}

    def test_version_from_instance_when_carried(self):
        # DCTL 等外挂实例自带版本
        stack = _stack([{"plugins": [
            {"plugin_id": "example_plugin", "type": "dctl",
             "enabled": True, "plugin_version": "3.1"}]}])
        assert _plugin_manifest(stack) == {"example_plugin": "3.1"}

    def test_unknown_version_for_uninstalled_plugin(self):
        stack = _stack([{"plugins": [
            {"plugin_id": "example_plugin", "type": "dctl",
             "enabled": True}]}])
        assert _plugin_manifest(stack) == {"example_plugin": "unknown"}

    def test_root_field_fallback_preserved(self):
        # 兼容旧形态/替身：根字段仍可收集
        stack = _stack([{"plugin_id": "legacy", "plugin_version": "0.9"}])
        assert _plugin_manifest(stack) == {"legacy": "0.9"}

    def test_registry_failure_does_not_block_manifest(self):
        with patch(
            "src.features.color.core.plugin_manager.get_plugin_manager",
            side_effect=RuntimeError("boom")):
            manifest = _plugin_manifest(_stack([{"plugins": [
                {"plugin_id": "vignette", "enabled": True}]}]))
        assert manifest == {"vignette": "unknown"}


# ================================================================
# 缺失检查：Color 插件注册表
# ================================================================

class TestMissingPlugins:
    def test_missing_against_color_plugin_registry(self):
        payload = {"plugin_versions": {
            "installed_plugin": "1.0", "missing_plugin": "2.0"}}
        fake_registry = {"installed_plugin": {"version": "1.0"}}
        with patch(
            "src.features.color.core.plugin_manager.get_plugin_manager",
            return_value=NS(get_all_plugins=lambda: fake_registry)):
            assert _missing_plugins(payload) == ["missing_plugin"]

    def test_no_manifest_means_no_missing(self):
        assert _missing_plugins({}) == []
        assert _missing_plugins({"plugin_versions": {}}) == []

    def test_registry_failure_raises_typed_error(self):
        # S09：探测失败不冒称"无缺失"——类型化错误，调用方标 unverified
        payload = {"plugin_versions": {"vignette": "1.0"}}
        with patch(
            "src.features.color.core.plugin_manager.get_plugin_manager",
            side_effect=RuntimeError("registry broken")):
            with pytest.raises(PluginRegistryUnavailableError):
                _missing_plugins(payload)
