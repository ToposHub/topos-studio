"""L05 内容库入口测试：用户转场 lib1 base 白名单与真实 JSON 装载、
user_* push 的 base 解析分流、内置 Composite 预设真实加载、
P29 配方→宿主载荷转换。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.features.edit.core import custom_transitions, transition_mask
from src.features.edit.core.transition_catalog import (
    RESAMPLE_TRANSITION_TYPES, resolve_transition_base_type,
)


class TestTransitionWhitelist:
    def test_lib1_bases_whitelisted(self) -> None:
        assert "lib1_clock_wipe" in custom_transitions._BASE_TYPES
        assert "lib1_true_push" in custom_transitions._BASE_TYPES

    def test_user_json_with_lib1_base_loads_and_renders(
            self, tmp_path: Path, monkeypatch) -> None:
        # 真实用户转场 JSON：lib1 base + 定义级默认参数（既有 chdir 模式）
        monkeypatch.chdir(tmp_path)
        user_dir = Path(custom_transitions.user_transitions_dir())
        user_dir.mkdir(parents=True, exist_ok=True)
        (user_dir / "my_clock.json").write_text(json.dumps({
            "id": "my_clock", "label": "My Clock Wipe",
            "base": "lib1_clock_wipe",
            "params": {"start_angle": 90.0, "clockwise": False, "softness": 0.05},
        }), encoding="utf-8")
        custom_transitions.reload_user_transitions()
        assert custom_transitions.is_user_transition_spatial("user_my_clock") is True
        resolved = custom_transitions.resolve_user_transition("user_my_clock")
        assert resolved is not None
        entry, base = resolved
        assert base == "lib1_clock_wipe"
        assert entry.params["start_angle"] == 90.0
        # CPU mask 经 base 解析真实渲染（逆时针 + 90° 起始边）
        mask = transition_mask.compute_transition_mask(16, 8, 0.25, "user_my_clock", {})
        direct = transition_mask.compute_transition_mask(
            16, 8, 0.25, "lib1_clock_wipe",
            {"start_angle": 90.0, "clockwise": False, "softness": 0.05})
        np.testing.assert_array_equal(mask, direct)

    def test_user_push_resolves_to_resample_base(
            self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.chdir(tmp_path)
        user_dir = Path(custom_transitions.user_transitions_dir())
        user_dir.mkdir(parents=True, exist_ok=True)
        (user_dir / "push_right.json").write_text(json.dumps({
            "id": "push_right", "label": "Push Right",
            "base": "lib1_true_push", "params": {"direction": "right"},
        }), encoding="utf-8")
        custom_transitions.reload_user_transitions()
        assert resolve_transition_base_type("user_push_right") \
            in RESAMPLE_TRANSITION_TYPES

    def teardown_method(self) -> None:
        # 恢复生产用户目录扫描状态（无该目录则清空）
        custom_transitions._REGISTRY.clear()


class TestCompositePreset:
    def test_builtin_lib1_preset_loads(self) -> None:
        from src.features.composite.utils.preset_manager import PresetManager
        mgr = PresetManager()
        data = mgr.load_preset("LIB1_Gradient_Tone")
        assert data is not None, "内置预设未找到（presets/LIB1_Gradient_Tone.json）"
        types = {n["type"] for n in data["nodes"]}
        assert types == {"lib1_gradient_map", "lib1_edge_extend"}
        assert len(data["connections"]) == 1
        # 节点可经 registry 真实反序列化
        from src.features.composite.core.node_engine import NODE_REGISTRY
        for node_dict in data["nodes"]:
            node = NODE_REGISTRY[node_dict["type"]].from_dict(node_dict)
            assert node.parameters["amount"] == 0.85 if "amount" in node.parameters \
                else node.parameters["radius"] == 4.0


class TestP29RecipeConversion:
    def test_edit_effect_payload_shape(self) -> None:
        from content_staging.library_v1.presets.p29_quartet_gradient import convert
        params = convert.to_edit_effect_params()
        assert set(params) == {"stop_0", "stop_1", "stop_2", "stop_3",
                               "pos_1", "pos_2", "interp_mode", "amount"}
        assert params["interp_mode"] == "Linear"  # select label 口径
        assert params["stop_0"] == (0.02, 0.02, 0.05)
        assert params["amount"] == 0.85

    def test_color_layer_plugin_payload_shape(self) -> None:
        from content_staging.library_v1.presets.p29_quartet_gradient import convert
        entry = convert.to_color_layer_plugin("inst_p29")
        # layer["plugins"] 实例四字段（layer.py:566 口径）
        assert set(entry) == {"instance_id", "plugin_id", "params", "enabled"}
        assert entry["plugin_id"] == "LIB1_Gradient_Map"
        assert entry["enabled"] is True
        assert entry["params"]["stop_2"] == [0.9, 0.55, 0.25]

    def test_recipe_params_valid_against_real_plugin_schema(self) -> None:
        """配方参数与真实插件 @param schema 对齐（真实 parser 校验）。"""
        from src.features.color.core.plugin_manager import PluginManager
        data = PluginManager()._parse_plugin_file(
            str(REPO_ROOT / "plugins" / "LIB1_Gradient_Map.tglsl"),
            "LIB1_Gradient_Map")
        assert data is not None
        schema = {p["var_name"]: p for p in data["params"]}
        from content_staging.library_v1.presets.p29_quartet_gradient import convert
        for name, value in convert.to_edit_effect_params().items():
            assert name in schema, f"配方参数 {name} 不在插件 schema"
            if schema[name]["type"] == "slider":
                assert schema[name]["min"] <= value <= schema[name]["max"]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
