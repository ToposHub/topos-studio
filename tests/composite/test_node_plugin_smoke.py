"""CP-002 节点插件通道冒烟测试。

回归背景：plugins/node_plugins/{posterize,vintage}_plugin.py 自
src.composite → src.features.composite 迁移后 import 一直失效，
官方插件目录实际为零可用插件；且插件声明的 effect marker
（posterize / vintage_tone / film_grain）在求值器分发中无实现，
即使 import 修好也只会透传。

本测试不依赖 GL/Qt：
1. 通过 NodePluginManager._load_plugin_file 加载两个官方插件文件；
2. 断言插件与节点类型注册成功、PARAM_DEFS 可用；
3. 断言三个 effect 的 shader 已注册进 ShaderLibrary；
4. 断言 _dispatch_effect 认识这三个 effect 类型（apply 方法存在）。
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pytest

from src.features.composite.core.node_plugin import NodePluginManager

# 与 NodePluginManager.PLUGIN_DIRS[1] 相同的推导：仓库根/plugins/node_plugins
_REPO_PLUGIN_DIR = (
    Path(__file__).resolve().parents[2] / "plugins" / "node_plugins"
)


@pytest.fixture()
def loaded_manager():
    manager = NodePluginManager()
    posterize_file = _REPO_PLUGIN_DIR / "posterize_plugin.py"
    vintage_file = _REPO_PLUGIN_DIR / "vintage_plugin.py"
    assert posterize_file.exists(), f"missing plugin file: {posterize_file}"
    assert vintage_file.exists(), f"missing plugin file: {vintage_file}"
    assert manager._load_plugin_file(posterize_file), (
        f"posterize plugin failed to load: {manager.errors}")
    assert manager._load_plugin_file(vintage_file), (
        f"vintage plugin failed to load: {manager.errors}")
    return manager


class TestBundledNodePluginsLoad:
    """CP-002: 官方节点插件可加载、可注册。"""

    def test_no_load_errors(self, loaded_manager):
        assert loaded_manager.errors == {}, (
            f"plugin load errors: {loaded_manager.errors}")

    def test_plugins_registered(self, loaded_manager):
        plugins = loaded_manager.plugins
        assert "posterize" in plugins
        assert "vintage_film" in plugins

    def test_plugin_node_types_registered(self, loaded_manager):
        types = loaded_manager.node_types
        assert "posterize" in types
        assert "vintage_tone" in types
        assert "film_grain" in types

    def test_plugin_node_instantiable(self, loaded_manager):
        node = loaded_manager.create_node("posterize")
        assert node is not None
        assert node.node_type == "posterize"
        # PARAM_DEFS 驱动属性面板；缺失会导致节点不可调参
        param_names = {p.name for p in node.PARAM_DEFS}
        assert {"levels", "channel_mode", "dither"} <= param_names

        grain = loaded_manager.create_node("film_grain")
        assert grain is not None
        grain_params = {p.name for p in grain.PARAM_DEFS}
        assert {"grain_size", "grain_intensity", "grain_type",
                "color_grain", "luminance_only"} <= grain_params


class TestPluginEffectShaders:
    """CP-002: 插件 effect 的 GPU 端分发链路存在（无 GL 编译）。"""

    _PLUGIN_EFFECTS = ("posterize", "vintage_tone", "film_grain")

    def test_shaders_registered_in_library(self):
        from src.features.composite.evaluators.shader_library import (
            ShaderLibrary,
        )
        for name in self._PLUGIN_EFFECTS:
            assert name in ShaderLibrary.STANDARD_NAMES, (
                f"{name} missing from STANDARD_NAMES (utility_shaders "
                f"would never receive it)")
            assert name in ShaderLibrary._SHADER_SOURCES, (
                f"{name} missing from _SHADER_SOURCES")

    def test_evaluator_has_apply_methods(self):
        from src.features.composite.evaluators.filter_evaluator import (
            FilterEvaluatorMixin,
        )
        for effect in self._PLUGIN_EFFECTS:
            method = getattr(FilterEvaluatorMixin, f"_apply_{effect}", None)
            assert callable(method), (
                f"FilterEvaluatorMixin._apply_{effect} missing — dispatch "
                f"would fall through to the unknown-effect passthrough")
