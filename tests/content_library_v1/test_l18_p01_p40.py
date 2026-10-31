"""L18 批次 P01–P40 配方交付测试。

范围与口径（docs/upgrade_plan_L_content_library_2026-10-24.md §7，L18 配方与宏）：
- 逐项：recipe.json 可解析 + 必填字段/枚举契约。P29 为 L05 已交付试点
  （tests/content_library_v1/test_l05_entries.py 深检），本文件只做浅检不重做；
- 依赖真实存在（防悬空引用）：生产插件 plugins/<X>.tglsl、暂存组件
  component_manifest.json、生产 transition_catalog 注册类型、生产节点插件可发现；
- 参数对真实 schema 校验：插件 @param（PluginManager 真实 parser）、暂存组件
  manifest ptype/range、生产转场核 DEFAULTS、posterize 节点真实 ParamDef；
- convert.py 可执行且载荷结构正确：转场载荷真实构造 TransitionInstance、
  mask/resample 生产核真实渲染一次；用户转场 JSON 白名单纪律（白名单外拒绝）；
- 能力矩阵如实：status 一律 component_ready（暂存，未接 L05 白名单），
  禁止把未接入宿主的配方写成 usable。

只读导入生产模块；本批次只写 content_staging/library_v1/presets/ 与本目录。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict

import numpy as np
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

PRESETS_ROOT = REPO_ROOT / "content_staging" / "library_v1" / "presets"

RECIPE_IDS = [f"P{i:02d}" for i in range(1, 41)]
PILOT_ID = "P29"          # L05 试点：浅检；深检归 test_l05_entries.py
BATCH_IDS = [r for r in RECIPE_IDS if r != PILOT_ID]


# ── 缓存加载 ──────────────────────────────────────────────────────────

@lru_cache(maxsize=None)
def _shared():
    from content_staging.library_v1.presets import _shared_convert
    return _shared_convert


@lru_cache(maxsize=None)
def recipe_dir(rid: str) -> Path:
    return _shared().recipe_dir_for(PRESETS_ROOT, rid)


@lru_cache(maxsize=None)
def recipe(rid: str) -> Dict[str, Any]:
    return _shared().load_recipe(recipe_dir(rid) / "recipe.json")


@lru_cache(maxsize=None)
def convert_module(rid: str):
    spec = importlib.util.spec_from_file_location(
        f"l18_convert_{recipe_dir(rid).name}", recipe_dir(rid) / "convert.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@lru_cache(maxsize=None)
def component_manifest(rid: str) -> Dict[str, Any]:
    deps = recipe(rid)["dependencies"]
    return _shared().load_component_manifest(
        deps["component_kind"], deps["component_slug"])


@lru_cache(maxsize=None)
def plugin_schema(plugin_id: str) -> Dict[str, Any]:
    """真实 parser 解析生产插件 @param schema（与 L05 P29 测试同一口径）。"""
    from src.features.color.core.plugin_manager import PluginManager
    data = PluginManager()._parse_plugin_file(
        str(REPO_ROOT / "plugins" / f"{plugin_id}.tglsl"), plugin_id)
    assert data is not None, f"生产插件解析失败: {plugin_id}"
    return {p["var_name"]: p for p in data["params"]}


def _is_num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _assert_param_value(name: str, value: Any, spec: Dict[str, Any]) -> None:
    """按真实参数 spec 校验单个配方参数值（类型 + 范围）。"""
    ptype = spec["ptype"]
    if ptype == "float":
        assert _is_num(value), f"{name} 须为数值，得到 {value!r}"
        lo, hi = spec.get("minimum"), spec.get("maximum")
        if lo is not None:
            assert value >= lo, f"{name}={value} < minimum {lo}"
        if hi is not None:
            assert value <= hi, f"{name}={value} > maximum {hi}"
    elif ptype == "int":
        assert isinstance(value, int) and not isinstance(value, bool), \
            f"{name} 须为 int，得到 {value!r}"
        lo, hi = spec.get("minimum"), spec.get("maximum")
        if lo is not None:
            assert value >= lo, f"{name}={value} < minimum {lo}"
        if hi is not None:
            assert value <= hi, f"{name}={value} > maximum {hi}"
    elif ptype == "bool":
        assert isinstance(value, bool), f"{name} 须为 bool，得到 {value!r}"
    elif ptype == "option":
        assert isinstance(value, str), f"{name} 须为选项字符串，得到 {value!r}"
        assert value in tuple(spec.get("options") or ()), \
            f"{name}={value!r} 不在选项 {list(spec.get('options') or ())}"
    elif ptype == "color":
        assert (isinstance(value, (list, tuple)) and len(value) == 3
                and all(_is_num(c) for c in value)), f"{name} 须为 3 元组颜色"
    else:
        pytest.fail(f"未知参数 ptype {ptype!r}（{name}）")


def _assert_plugin_param(name: str, value: Any, spec: Dict[str, Any]) -> None:
    """按生产插件 @param spec 校验（slider/select/checkbox/color）。"""
    ptype = spec["type"]
    if ptype == "slider":
        assert _is_num(value), f"{name} 须为数值，得到 {value!r}"
        assert spec["min"] <= value <= spec["max"], \
            f"{name}={value} 超出插件 slider [{spec['min']}, {spec['max']}]"
    elif ptype == "select":
        assert isinstance(value, str), f"{name} select 须用 label 字符串，得到 {value!r}"
        assert value in spec["options"], f"{name}={value!r} 不在插件选项 {spec['options']}"
    elif ptype == "checkbox":
        assert isinstance(value, bool), f"{name} checkbox 须为 bool，得到 {value!r}"
    elif ptype == "color":
        assert (isinstance(value, (list, tuple)) and len(value) == 3
                and all(_is_num(c) for c in value)), f"{name} 须为 3 元组颜色"
    else:
        pytest.fail(f"未知插件参数类型 {ptype!r}（{name}）")


# ── 1. 交付完整性 ─────────────────────────────────────────────────────

class TestRecipeDelivery:
    @pytest.mark.parametrize("rid", RECIPE_IDS)
    def test_dir_and_files_present(self, rid: str) -> None:
        d = recipe_dir(rid)  # 恰有一个 pNN_* 目录，否则 recipe_dir_for raise
        assert (d / "recipe.json").is_file(), f"{rid} 缺 recipe.json"
        assert (d / "convert.py").is_file(), f"{rid} 缺 convert.py"

    @pytest.mark.parametrize("rid", BATCH_IDS)
    def test_required_fields_and_batch_contract(self, rid: str) -> None:
        data = recipe(rid)
        missing = [f for f in _shared().REQUIRED_RECIPE_FIELDS if f not in data]
        assert not missing, f"{rid} 缺必填字段 {missing}"
        assert data["recipe_id"] == rid, "recipe_id 与目录 pNN_ 编号不一致"
        assert data["status"] == "component_ready", \
            f"{rid} status 必须是 component_ready（暂存未接 L05 白名单），禁止写 usable/integrated"
        assert data["kind"] in _shared().VALID_KINDS
        assert data["version"] == "1.0.0"
        assert data["working_space"] == "scene-linear"
        pages = data["applies_pages"]
        assert pages and set(pages) <= set(_shared().VALID_PAGES)
        for field in ("name", "strength_hint", "component", "notes"):
            assert isinstance(data[field], str) and data[field].strip(), \
                f"{rid} 字段 {field} 须为非空字符串"
        assert isinstance(data["params"], dict) and data["params"], \
            f"{rid} params 须为非空 dict"
        assert isinstance(data["dependencies"], dict) and data["dependencies"]

    def test_p29_pilot_light_check(self) -> None:
        """P29 已由 L05 交付（test_l05_entries.py 深检），此处只浅检在位。"""
        data = recipe(PILOT_ID)
        assert data["recipe_id"] == PILOT_ID
        assert data["name"] == "四色渐变"
        assert data["dependencies"]["plugin_id"] == "LIB1_Gradient_Map"
        assert data["params"], "P29 params 非空"

    @pytest.mark.parametrize("rid", RECIPE_IDS)
    def test_recipe_json_is_pure_json(self, rid: str) -> None:
        text = (recipe_dir(rid) / "recipe.json").read_text(encoding="utf-8")
        json.loads(text)  # 直接 json.load 可解析（无注释/尾逗号）


# ── 2. 依赖真实存在（防悬空引用） ─────────────────────────────────────

class TestDependencyExistence:
    def test_every_recipe_declares_exactly_one_dep_kind(self) -> None:
        for rid in BATCH_IDS:
            deps = recipe(rid)["dependencies"]
            if "transition_type" in deps:
                # 转场配方：生产 base 与暂存组件身份同声明（同源双引用）
                assert deps["component_kind"] == "transition"
                assert "plugin_id" not in deps and "node_type" not in deps
            else:
                kinds = [k for k in ("plugin_id", "component_slug", "node_type")
                         if k in deps]
                assert len(kinds) == 1, f"{rid} 依赖声明须恰好一种，得到 {kinds}"

    def test_plugin_dependencies_exist_and_versioned(self) -> None:
        import re
        for rid in BATCH_IDS:
            deps = recipe(rid)["dependencies"]
            if "plugin_id" not in deps:
                continue
            pid = deps["plugin_id"]
            path = REPO_ROOT / "plugins" / f"{pid}.tglsl"
            assert path.is_file(), f"{rid} 依赖的生产插件不存在: {path}"
            m = re.search(r'//\s*@version\s+(.+)', path.read_text(encoding="utf-8"))
            assert m, f"{pid} 缺 @version 指令"
            actual = tuple(int(x) for x in m.group(1).strip().split("."))
            minimum = tuple(int(x) for x in deps["min_plugin_version"].split("."))
            assert actual >= minimum, f"{rid} 插件 {pid} 版本 {actual} < 要求 {minimum}"

    def test_component_dependencies_exist(self) -> None:
        for rid in BATCH_IDS:
            deps = recipe(rid)["dependencies"]
            if "component_slug" not in deps:
                continue
            manifest = component_manifest(rid)
            assert manifest["component_id"] == deps["component_id"], \
                f"{rid} component_id 与 manifest 不符"
            assert manifest["slug"] == deps["component_slug"]
            assert manifest["status"] == "component_ready", \
                f"{rid} 依赖的暂存组件未达 component_ready"
            assert manifest["kind"] == deps["component_kind"]

    def test_transition_dependencies_registered_in_catalog(self) -> None:
        from src.features.edit.core.transition_catalog import TRANSITION_LABELS
        for rid in BATCH_IDS:
            deps = recipe(rid)["dependencies"]
            if "transition_type" not in deps:
                continue
            ttype = deps["transition_type"]
            assert ttype in TRANSITION_LABELS, \
                f"{rid} 依赖的 {ttype} 未注册进生产 transition_catalog"
            # 生产核与暂存组件 manifest 必须同名同源（cores 再导出纪律）
            from src.features.edit.transition_packs.library_v1 import cores
            defaults_map = {
                "lib1_clock_wipe": cores.CLOCK_WIPE_DEFAULTS,
                "lib1_venetian_blinds": cores.VENETIAN_DEFAULTS,
                "lib1_checker_reveal": cores.CHECKER_DEFAULTS,
                "lib1_halftone_reveal": cores.HALFTONE_DEFAULTS,
                "lib1_true_push": cores.PUSH_DEFAULTS,
            }
            assert ttype in defaults_map, f"{ttype} 无生产核 DEFAULTS 映射"
            manifest = component_manifest(rid)
            manifest_keys = {p["name"] for p in manifest["params"]}
            assert manifest_keys == set(defaults_map[ttype]), \
                f"{rid} 暂存 manifest 参数域与生产核 DEFAULTS 不一致"

    def test_node_dependency_discoverable(self) -> None:
        deps = recipe("P30")["dependencies"]
        assert deps["node_type"] == "posterize"
        plugin_path = REPO_ROOT / deps["node_plugin"]
        assert plugin_path.is_file(), f"生产节点插件不存在: {plugin_path}"
        from src.features.composite.core.node_plugin import get_plugin_manager
        mgr = get_plugin_manager()
        mgr.discover_plugins()
        assert mgr.get_plugin_for_node("posterize") is not None, \
            "posterize 节点插件不可经 NodePluginManager 发现"


# ── 3. 参数对真实 schema 校验 ─────────────────────────────────────────

class TestParamsAgainstRealSchema:
    def test_plugin_recipe_params(self) -> None:
        for rid in BATCH_IDS:
            deps = recipe(rid)["dependencies"]
            if "plugin_id" not in deps:
                continue
            schema = plugin_schema(deps["plugin_id"])
            for name, value in recipe(rid)["params"].items():
                assert name in schema, f"{rid} 参数 {name} 不在 {deps['plugin_id']} schema"
                _assert_plugin_param(name, value, schema[name])

    def test_component_recipe_params(self) -> None:
        for rid in BATCH_IDS:
            deps = recipe(rid)["dependencies"]
            if "component_slug" not in deps:
                continue
            known = {p["name"]: p for p in component_manifest(rid)["params"]}
            for name, value in recipe(rid)["params"].items():
                assert name in known, f"{rid} 参数 {name} 不在组件 schema（悬空引用）"
                _assert_param_value(name, value, known[name])

    def test_p30_posterize_params_against_real_node(self) -> None:
        from src.features.composite.core.node_plugin import get_plugin_manager
        mgr = get_plugin_manager()
        mgr.discover_plugins()
        node = mgr.create_node("posterize")
        defs = {d.name: d for d in type(node).PARAM_DEFS}
        for name, value in recipe("P30")["params"].items():
            assert name in node.parameters, f"P30 参数 {name} 不在真实 posterize 节点参数"
            if name in defs:
                d = defs[name]
                if d.ptype == "int":
                    assert isinstance(value, int) and not isinstance(value, bool)
                    assert d.min_val <= value <= d.max_val
                elif d.ptype == "option":
                    assert value in d.labels
                elif d.ptype == "bool":
                    assert isinstance(value, bool)
        # 快照可整体写入真实节点（宿主可消费性）
        snapshot = convert_module("P30").to_node_params_snapshot()
        node.parameters.update(snapshot["parameters"])
        assert node.parameters["levels"] == snapshot["parameters"]["levels"]

    def test_transition_recipe_params_within_production_defaults_domain(self) -> None:
        from src.features.edit.transition_packs.library_v1 import cores
        defaults_map = {
            "lib1_clock_wipe": cores.CLOCK_WIPE_DEFAULTS,
            "lib1_venetian_blinds": cores.VENETIAN_DEFAULTS,
            "lib1_checker_reveal": cores.CHECKER_DEFAULTS,
            "lib1_halftone_reveal": cores.HALFTONE_DEFAULTS,
            "lib1_true_push": cores.PUSH_DEFAULTS,
        }
        for rid in BATCH_IDS:
            deps = recipe(rid)["dependencies"]
            if "transition_type" not in deps:
                continue
            defaults = defaults_map[deps["transition_type"]]
            for name, value in recipe(rid)["params"].items():
                assert name in defaults, \
                    f"{rid} 参数 {name} 不在生产核 DEFAULTS（悬空引用）"
                # int 型核参数（count/rows/columns/spacing）须传 int
                if isinstance(defaults[name], int) and not isinstance(defaults[name], bool):
                    assert isinstance(value, int) and not isinstance(value, bool), \
                        f"{rid} 参数 {name} 须为 int（生产核口径），得到 {value!r}"


# ── 4. convert.py 载荷转换 ────────────────────────────────────────────

class TestConvertPayloads:
    def test_plugin_recipes_dual_payload(self) -> None:
        for rid in BATCH_IDS:
            deps = recipe(rid)["dependencies"]
            if "plugin_id" not in deps:
                continue
            mod = convert_module(rid)
            params = mod.to_edit_effect_params()
            assert set(params) == set(recipe(rid)["params"])
            for name, value in params.items():
                spec = plugin_schema(deps["plugin_id"])[name]
                _assert_plugin_param(name, value, spec)
            entry = mod.to_color_layer_plugin(f"inst_{rid}")
            assert set(entry) == {"instance_id", "plugin_id", "params", "enabled"}
            assert entry["plugin_id"] == deps["plugin_id"]
            assert entry["enabled"] is True
            assert entry["params"] == {k: (list(v) if isinstance(v, tuple) else v)
                                       for k, v in params.items()}
            json.dumps(entry, ensure_ascii=False)  # 载荷可序列化（JSON-ready）

    def test_component_filter_recipes_payload(self) -> None:
        for rid in BATCH_IDS:
            deps = recipe(rid)["dependencies"]
            if "component_slug" not in deps or deps["component_kind"] != "filter":
                continue
            payload = convert_module(rid).to_component_payload()
            manifest = component_manifest(rid)
            assert payload["pack_id"] == "topos_library_v1"
            assert payload["component_id"] == manifest["component_id"]
            assert payload["slug"] == manifest["slug"]
            assert payload["kind"] == "filter"
            known = {p["name"]: p for p in manifest["params"]}
            for name, value in payload["params"].items():
                _assert_param_value(name, value, known[name])
            json.dumps(payload, ensure_ascii=False)

    def test_transition_recipes_payload_and_real_instance(self) -> None:
        from src.features.edit.core.timeline_v2 import TransitionInstance
        for rid in BATCH_IDS:
            deps = recipe(rid)["dependencies"]
            if "transition_type" not in deps:
                continue
            payload = convert_module(rid).to_transition_instance()
            assert payload["transition_type"] == deps["transition_type"]
            assert set(payload) == {"transition_type", "params"}
            # 生产模型真实构造（transition_type 合法性由 __post_init__ 校验）
            inst = TransitionInstance(
                transition_type=payload["transition_type"],
                track_id="track_v1", left_clip_id="clip_a", right_clip_id="clip_b",
                center_frame=10, duration=12,
                params=dict(payload["params"]))
            assert inst.params == payload["params"]
            json.dumps(payload, ensure_ascii=False)

    def test_mask_family_recipes_render_real_mask(self) -> None:
        from src.features.edit.core.transition_mask import compute_transition_mask
        mask_ids = ["P33", "P34", "P35", "P36"]
        for rid in mask_ids:
            payload = convert_module(rid).to_transition_instance()
            params = payload["params"]
            mid = compute_transition_mask(32, 18, 0.25,
                                          payload["transition_type"], dict(params))
            assert mid.dtype == np.float32 and mid.shape == (18, 32)
            assert mid.min() >= 0.0 and mid.max() <= 1.0
            # 端点精确（p≤0 全 A / p≥1 全 B）
            assert not compute_transition_mask(
                8, 8, 0.0, payload["transition_type"], dict(params)).any()
            assert compute_transition_mask(
                8, 8, 1.0, payload["transition_type"], dict(params)).all()

    def test_push_recipes_render_real_frames(self) -> None:
        from src.features.edit.transition_packs.library_v1.cores import (
            PUSH_DIRECTIONS, push_frame)
        rng = np.random.default_rng(7)
        # 方幅（试点同口径）：四方向端点/中点全可渲染。
        # （已知生产缺陷另行 xfail 记录：非方幅垂直方向广播失败。）
        a8 = rng.integers(0, 255, size=(8, 8, 3), dtype=np.uint8)
        b8 = rng.integers(0, 255, size=(8, 8, 3), dtype=np.uint8)
        af = rng.random((8, 8, 3))
        bf = rng.random((8, 8, 3))
        for rid in ["P37", "P38", "P39", "P40"]:
            payload = convert_module(rid).to_transition_instance()
            direction = payload["params"]["direction"]
            assert direction in PUSH_DIRECTIONS
            soft = payload["params"]["seam_softness"]
            # 端点还原（X09 生产核契约：uint8 入 → uint8 比特还原；
            # float 入 → float32 输出，数值还原到 float32 精度）
            assert np.array_equal(push_frame(a8, b8, 0.0, direction, soft), a8)
            assert np.array_equal(push_frame(a8, b8, 1.0, direction, soft), b8)
            assert np.allclose(push_frame(af, bf, 0.0, direction, soft), af, atol=1e-6)
            assert np.allclose(push_frame(af, bf, 1.0, direction, soft), bf, atol=1e-6)
            mid = push_frame(a8, b8, 0.25, direction, soft)
            assert mid.shape == a8.shape and mid.dtype == np.uint8
            assert not np.array_equal(mid, a8) and not np.array_equal(mid, b8)

    def test_push_vertical_nonsquare(self) -> None:
        """非方幅垂直推（16:9 素材真实路径）：曾因 fx 形状 (h,1) 对
        (h,w,3) 错位广播抛 ValueError（L18 批次发现并上报，Mac 主集成者
        已修复 cores.py::_sample1 分轴展开为 (h,1,1)）；本测试钉住回归。"""
        from src.features.edit.transition_packs.library_v1.cores import push_frame
        a = np.zeros((8, 12, 3), dtype=np.uint8)
        b = np.full((8, 12, 3), 200, dtype=np.uint8)
        out = push_frame(a, b, 0.25, "up", 0.0)  # 当前 raises ValueError
        assert out.shape == a.shape

    def test_user_transition_json_whitelist_discipline(self) -> None:
        from src.features.edit.core import custom_transitions
        whitelisted = {"P33", "P37", "P38", "P39", "P40"}
        for rid in BATCH_IDS:
            deps = recipe(rid)["dependencies"]
            if "transition_type" not in deps:
                continue
            mod = convert_module(rid)
            if rid in whitelisted:
                uj = mod.to_user_transition_json(f"l18_{recipe_dir(rid).name}")
                assert set(uj) == {"id", "label", "base", "params"}
                assert uj["base"] == deps["transition_type"]
                assert uj["base"] in custom_transitions._BASE_TYPES
                assert uj["label"] == recipe(rid)["name"]
                json.dumps(uj, ensure_ascii=False)
            else:
                # P34–P36：base 不在白名单——共享层直接拒绝，不产出宿主会
                # 拒绝的载荷；配方 convert.py 也不得提供该转换。
                assert not hasattr(mod, "to_user_transition_json"), \
                    f"{rid} base 未入白名单却提供用户转场 JSON 转换"
                with pytest.raises(ValueError, match="白名单"):
                    _shared().user_transition_json(recipe(rid), f"l18_{rid.lower()}")

    def test_user_transition_json_loads_via_production_path(self, tmp_path,
                                                            monkeypatch) -> None:
        """P37 载荷走真实用户转场装载路径（L05 同款 chdir 模式）。"""
        from src.features.edit.core import custom_transitions
        uj = convert_module("P37").to_user_transition_json("l18_push_left")
        monkeypatch.chdir(tmp_path)
        user_dir = Path(custom_transitions.user_transitions_dir())
        user_dir.mkdir(parents=True, exist_ok=True)
        (user_dir / f"{uj['id']}.json").write_text(
            json.dumps(uj, ensure_ascii=False), encoding="utf-8")
        custom_transitions.reload_user_transitions()
        resolved = custom_transitions.resolve_user_transition(
            custom_transitions.user_to_type_id(uj["id"]))
        assert resolved is not None
        entry, base = resolved
        assert base == "lib1_true_push"
        assert entry.params == uj["params"]
        custom_transitions._REGISTRY.clear()  # 恢复生产用户目录扫描状态


# ── 5. 能力矩阵如实 ───────────────────────────────────────────────────

class TestCapabilityHonesty:
    def test_no_recipe_claims_host_integration(self) -> None:
        forbidden = ("usable", "integrated", "validated", "released")
        for rid in BATCH_IDS:
            assert recipe(rid)["status"] not in forbidden
        # 组件载荷类配方必须在 notes 里明示"未接缝/非宿主载荷"语义
        for rid in BATCH_IDS:
            deps = recipe(rid)["dependencies"]
            if "component_slug" in deps and deps["component_kind"] == "filter":
                note = recipe(rid)["notes"]
                assert ("未接缝" in note) or ("暂存" in note), \
                    f"{rid} 暂存组件依赖须在 notes 明示未接入状态"

    def test_shared_helper_batch_contract_constants(self) -> None:
        sc = _shared()
        assert sc.BATCH_STATUS == "component_ready"
        assert "status" in sc.REQUIRED_RECIPE_FIELDS
        assert set(sc.VALID_PAGES) == {"color", "edit", "composite"}


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
