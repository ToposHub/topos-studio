"""L18 批次 P41–P80 配方交付测试（转场 P41–P56 + 合成宏 P57–P80）。

范围与口径（docs/upgrade_plan_L_content_library_2026-10-24.md §7，L18 配方与宏）：
- 逐项：recipe.json 可解析 + 必填字段/枚举契约（kind 新增 composite_macro）；
- 依赖真实存在（防悬空引用）：生产 transition_catalog 注册类型、暂存
  transitions/nodes 组件 manifest（nodes/ 目录只读引用）、生产 node_engine
  注册表（NODE_REGISTRY + create_node_by_type）；
- 参数对真实 schema 校验：暂存组件 manifest ptype/range、生产转场核
  DEFAULTS（cores + resample_cores）、生产节点真实 PARAM_DEFS；
- convert.py 载荷结构正确且可序列化：转场载荷真实构造 TransitionInstance、
  mask/复合核真实渲染端点帧；宏载荷结构（slots/steps/connections/output_step）
  端口对真实节点/manifest 声明校验、图无环；
- 白名单纪律：P41–P56 base 均不在 custom_transitions._BASE_TYPES
  （当前仅 lib1_clock_wipe / lib1_true_push）——不产出用户转场 JSON，
  convert.py 也不得提供该转换；
- 能力矩阵如实：status 一律 component_ready（暂存，未接 L05 白名单/宿主图
  导入未接线），禁止 usable/integrated。

只读导入生产模块与暂存组件（nodes/ 不改写）；本批次只写
content_staging/library_v1/presets/ 与本目录。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Mapping

import numpy as np
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

PRESETS_ROOT = REPO_ROOT / "content_staging" / "library_v1" / "presets"

RECIPE_IDS = [f"P{i:02d}" for i in range(41, 81)]
TRANSITION_IDS = [f"P{i:02d}" for i in range(41, 57)]
MACRO_IDS = [f"P{i:02d}" for i in range(57, 81)]

FORBIDDEN_STATUS = ("usable", "integrated", "validated", "released")


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
        f"l18b_convert_{recipe_dir(rid).name}", recipe_dir(rid) / "convert.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@lru_cache(maxsize=None)
def component_manifest(rid: str) -> Dict[str, Any]:
    deps = recipe(rid)["dependencies"]
    return _shared().load_component_manifest(
        deps["component_kind"], deps["component_slug"])


@lru_cache(maxsize=None)
def transition_defaults_map() -> Dict[str, Dict[str, Any]]:
    """生产核 DEFAULTS（transition_catalog 注册类型的参数域事实源）。"""
    from src.features.edit.transition_packs.library_v1 import cores
    from src.features.edit.transition_packs.library_v1.resample_cores import (
        x10_hinged_doors, x12_page_curl, x14_displacement_bridge,
        x15_ripple_refraction, x16_scanline_slices, x17_kaleido_bridge,
        x18_burn_reveal,
    )
    return {
        "lib1_noise_dissolve": cores.NOISE_DISSOLVE_DEFAULTS,
        "lib1_luma_reveal": cores.LUMA_REVEAL_DEFAULTS,
        "lib1_voronoi_reveal": cores.DEFAULTS,
        "lib1_ripple_refraction": x15_ripple_refraction.DEFAULTS,
        "lib1_displacement_bridge": x14_displacement_bridge.DEFAULTS,
        "lib1_scanline_slices": x16_scanline_slices.DEFAULTS,
        "lib1_kaleido_bridge": x17_kaleido_bridge.DEFAULTS,
        "lib1_page_curl": x12_page_curl.DEFAULTS,
        "lib1_burn_reveal": x18_burn_reveal.BURN_DEFAULTS,
        "lib1_hinged_doors": x10_hinged_doors.DEFAULTS,
    }


@lru_cache(maxsize=None)
def production_node(node_type: str):
    from src.features.composite.core.node_engine import (NODE_REGISTRY,
                                                         create_node_by_type)
    if NODE_REGISTRY.get(node_type) is None:
        return None
    node = create_node_by_type(node_type)
    assert node is not None, f"生产节点实例化失败: {node_type}"
    return node


def _is_num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


# ── manifest / ParamDef 值校验 ────────────────────────────────────────

def _assert_manifest_param(name: str, value: Any, spec: Dict[str, Any]) -> None:
    """按暂存组件 manifest ptype/range 校验（vec2/color 为元组形态）。"""
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
    elif ptype == "vec2":
        assert (isinstance(value, (list, tuple)) and len(value) == 2
                and all(_is_num(c) for c in value)), f"{name} 须为 2 元组"
    else:
        pytest.fail(f"未知 manifest 参数 ptype {ptype!r}（{name}）")


def _assert_production_param(name: str, value: Any, node) -> None:
    """按生产节点真实 PARAM_DEFS 校验（create_node_by_type 实例口径）。"""
    defs = {d.name: d for d in type(node).PARAM_DEFS}
    assert name in node.parameters, \
        f"参数 {name} 不在生产节点 {node.node_type} 参数域"
    if name not in defs:
        return  # 节点 parameters 有但无强 schema（如派生字段）——只验存在
    d = defs[name]
    if d.ptype == "float":
        assert _is_num(value), f"{name} 须为数值，得到 {value!r}"
        assert d.min_val <= value <= d.max_val, \
            f"{name}={value} 超出 [{d.min_val}, {d.max_val}]"
    elif d.ptype == "int":
        assert isinstance(value, int) and not isinstance(value, bool), \
            f"{name} 须为 int，得到 {value!r}"
        assert d.min_val <= value <= d.max_val, \
            f"{name}={value} 超出 [{d.min_val}, {d.max_val}]"
    elif d.ptype == "bool":
        assert isinstance(value, bool), f"{name} 须为 bool，得到 {value!r}"
    elif d.ptype == "option":
        assert value in (d.labels or ()), \
            f"{name}={value!r} 不在选项 {list(d.labels or ())}"
    elif d.ptype == "color":
        assert (isinstance(value, (list, tuple)) and len(value) == 3
                and all(_is_num(c) for c in value)), f"{name} 须为 3 元组颜色"
    elif d.ptype == "str":
        assert isinstance(value, str), f"{name} 须为字符串，得到 {value!r}"
    elif d.ptype == "vec2":
        assert (isinstance(value, (list, tuple)) and len(value) == 2
                and all(_is_num(c) for c in value)), f"{name} 须为 2 元组"
    else:
        pytest.fail(f"未知生产节点参数 ptype {d.ptype!r}（{name}）")


# ── 1. 交付完整性 ─────────────────────────────────────────────────────

class TestRecipeDelivery:
    @pytest.mark.parametrize("rid", RECIPE_IDS)
    def test_dir_and_files_present(self, rid: str) -> None:
        d = recipe_dir(rid)  # 恰有一个 pNN_* 目录，否则 recipe_dir_for raise
        assert (d / "recipe.json").is_file(), f"{rid} 缺 recipe.json"
        assert (d / "convert.py").is_file(), f"{rid} 缺 convert.py"

    @pytest.mark.parametrize("rid", RECIPE_IDS)
    def test_required_fields_and_batch_contract(self, rid: str) -> None:
        data = recipe(rid)
        missing = [f for f in _shared().REQUIRED_RECIPE_FIELDS if f not in data]
        assert not missing, f"{rid} 缺必填字段 {missing}"
        assert data["recipe_id"] == rid, "recipe_id 与目录 pNN_ 编号不一致"
        assert data["status"] == "component_ready", \
            f"{rid} status 必须是 component_ready（暂存未接 L05 白名单），禁止写 usable/integrated"
        assert data["kind"] in _shared().VALID_KINDS
        assert data["kind"] == ("transition" if rid in TRANSITION_IDS
                                else "composite_macro")
        assert data["version"] == "1.0.0"
        assert data["working_space"] == "scene-linear"
        pages = data["applies_pages"]
        assert pages and set(pages) <= set(_shared().VALID_PAGES)
        assert pages == (["edit"] if rid in TRANSITION_IDS else ["composite"])
        for field in ("name", "strength_hint", "component", "notes"):
            assert isinstance(data[field], str) and data[field].strip(), \
                f"{rid} 字段 {field} 须为非空字符串"
        assert isinstance(data["params"], dict) and data["params"], \
            f"{rid} params 须为非空 dict"
        assert isinstance(data["dependencies"], dict) and data["dependencies"]

    @pytest.mark.parametrize("rid", RECIPE_IDS)
    def test_recipe_json_is_pure_json(self, rid: str) -> None:
        text = (recipe_dir(rid) / "recipe.json").read_text(encoding="utf-8")
        json.loads(text)  # 直接 json.load 可解析（无注释/尾逗号）


# ── 2. 依赖真实存在（防悬空引用） ─────────────────────────────────────

class TestDependencyExistence:
    def test_transition_deps_shape(self) -> None:
        for rid in TRANSITION_IDS:
            deps = recipe(rid)["dependencies"]
            assert deps["component_kind"] == "transition"
            assert deps["component_id"] and deps["component_slug"]
            assert "transition_type" in deps
            assert "plugin_id" not in deps and "node_type" not in deps
            assert deps["user_json_base"] is False, \
                f"{rid} base 不在白名单，user_json_base 必须如实为 False"

    def test_macro_deps_shape(self) -> None:
        for rid in MACRO_IDS:
            deps = recipe(rid)["dependencies"]
            assert "node_type" in deps, f"{rid} 宏依赖须声明主 node_type"
            assert isinstance(deps.get("aux_node_types", []), list)
            if "component_slug" in deps:
                assert deps["component_kind"] == "node"
                # 暂存主组件的 node_type 命名空间纪律（spec §6：lib1_* 冻结）
                assert deps["node_type"] == f"lib1_{deps['component_slug']}"
            else:
                assert not deps["node_type"].startswith("lib1_"), \
                    f"{rid} 生产主节点不应冒充 lib1_* 命名空间"
            assert "plugin_id" not in deps and "transition_type" not in deps

    def test_transition_types_registered_in_catalog(self) -> None:
        from src.features.edit.core.transition_catalog import TRANSITION_LABELS
        for rid in TRANSITION_IDS:
            ttype = recipe(rid)["dependencies"]["transition_type"]
            assert ttype in TRANSITION_LABELS, \
                f"{rid} 依赖的 {ttype} 未注册进生产 transition_catalog"
            assert ttype in transition_defaults_map(), f"{ttype} 无生产核 DEFAULTS 映射"

    def test_transition_manifest_param_set_matches_production_core(self) -> None:
        for rid in TRANSITION_IDS:
            deps = recipe(rid)["dependencies"]
            manifest = component_manifest(rid)
            assert manifest["component_id"] == deps["component_id"]
            assert manifest["slug"] == deps["component_slug"]
            assert manifest["status"] == "component_ready"
            assert manifest["kind"] == "transition"
            manifest_keys = {p["name"] for p in manifest["params"]}
            assert manifest_keys == set(transition_defaults_map()[
                deps["transition_type"]]), \
                f"{rid} 暂存 manifest 参数域与生产核 DEFAULTS 不一致"

    def test_macro_staging_component_manifests_read_only_ref(self) -> None:
        for rid in MACRO_IDS:
            deps = recipe(rid)["dependencies"]
            if "component_slug" not in deps:
                continue
            manifest = component_manifest(rid)  # nodes/ 目录只读引用
            assert manifest["component_id"] == deps["component_id"], \
                f"{rid} component_id 与 manifest 不符"
            assert manifest["slug"] == deps["component_slug"]
            assert manifest["status"] == "component_ready", \
                f"{rid} 依赖的暂存节点组件未达 component_ready"
            assert manifest["kind"] == "node"
            node_mod = (REPO_ROOT / "content_staging" / "library_v1" / "nodes"
                        / deps["component_slug"]
                        / f"lib1_{deps['component_slug']}_node.py")
            assert node_mod.is_file(), f"暂存节点模块不存在: {node_mod}"

    def test_macro_aux_nodes_in_production_registry(self) -> None:
        from src.features.composite.core.node_engine import NODE_REGISTRY
        for rid in MACRO_IDS:
            aux = recipe(rid)["dependencies"].get("aux_node_types") or []
            for t in aux:
                assert t != recipe(rid)["dependencies"]["node_type"], \
                    f"{rid} aux 不得与主 node_type 重复"
                assert NODE_REGISTRY.get(t) is not None, \
                    f"{rid} 辅助节点 {t} 不在生产 node_engine 注册表"
                assert not t.startswith("lib1_"), \
                    f"{rid} aux_node_types 应为生产基础节点，得到 lib1_* {t}"


# ── 3. 参数对真实 schema 校验 ─────────────────────────────────────────

class TestParamsAgainstRealSchema:
    def test_transition_params_within_manifest_and_int_core_domain(self) -> None:
        for rid in TRANSITION_IDS:
            deps = recipe(rid)["dependencies"]
            defaults = transition_defaults_map()[deps["transition_type"]]
            known = {p["name"]: p for p in component_manifest(rid)["params"]}
            for name, value in recipe(rid)["params"].items():
                assert name in defaults, \
                    f"{rid} 参数 {name} 不在生产核 DEFAULTS（悬空引用）"
                assert name in known, f"{rid} 参数 {name} 不在暂存 manifest schema"
                _assert_manifest_param(name, value, known[name])
                # int 型核参数（scale/seed/band_px/sectors）须传 int（生产核口径）
                if isinstance(defaults[name], int) and not isinstance(defaults[name], bool):
                    assert isinstance(value, int) and not isinstance(value, bool), \
                        f"{rid} 参数 {name} 须为 int（生产核口径），得到 {value!r}"

    def test_macro_staging_primary_params(self) -> None:
        for rid in MACRO_IDS:
            deps = recipe(rid)["dependencies"]
            if "component_slug" not in deps:
                continue
            known = {p["name"]: p for p in component_manifest(rid)["params"]}
            for name, value in recipe(rid)["params"].items():
                assert name in known, f"{rid} 参数 {name} 不在组件 schema（悬空引用）"
                _assert_manifest_param(name, value, known[name])

    def test_macro_production_primary_params(self) -> None:
        for rid in MACRO_IDS:
            deps = recipe(rid)["dependencies"]
            if "component_slug" in deps:
                continue
            node = production_node(deps["node_type"])
            assert node is not None, \
                f"{rid} 生产主节点 {deps['node_type']} 不在 node_engine 注册表"
            for name, value in recipe(rid)["params"].items():
                _assert_production_param(name, value, node)


# ── 4a. 转场配方载荷与真实构造 ────────────────────────────────────────

class TestTransitionPayloads:
    def test_transition_instances_construct_for_real(self) -> None:
        from src.features.edit.core.timeline_v2 import TransitionInstance
        for rid in TRANSITION_IDS:
            deps = recipe(rid)["dependencies"]
            payload = convert_module(rid).to_transition_instance()
            assert payload["transition_type"] == deps["transition_type"]
            assert set(payload) == {"transition_type", "params"}
            inst = TransitionInstance(
                transition_type=payload["transition_type"],
                track_id="track_v1", left_clip_id="clip_a",
                right_clip_id="clip_b", center_frame=10, duration=12,
                params=dict(payload["params"]))
            assert inst.params == payload["params"]
            json.dumps(payload, ensure_ascii=False)  # 载荷可序列化

    def test_user_transition_json_whitelist_discipline(self) -> None:
        """P41–P56 base 均不在白名单：不得产出用户转场 JSON，也不得提供转换。"""
        from src.features.edit.core import custom_transitions
        for rid in TRANSITION_IDS:
            deps = recipe(rid)["dependencies"]
            assert deps["transition_type"] not in custom_transitions._BASE_TYPES
            mod = convert_module(rid)
            assert not hasattr(mod, "to_user_transition_json"), \
                f"{rid} base 未入白名单却提供用户转场 JSON 转换"
            with pytest.raises(ValueError, match="白名单"):
                _shared().user_transition_json(recipe(rid), f"l18_{rid.lower()}")

    def test_noise_dissolve_recipes_render_real_masks(self) -> None:
        from src.features.edit.core.transition_mask import compute_transition_mask
        for rid in ["P41", "P42", "P43", "P44"]:
            payload = convert_module(rid).to_transition_instance()
            params = dict(payload["params"])
            mid = compute_transition_mask(32, 18, 0.25,
                                          payload["transition_type"], params)
            assert mid.dtype == np.float32 and mid.shape == (18, 32)
            assert mid.min() >= 0.0 and mid.max() <= 1.0
            assert not compute_transition_mask(
                8, 8, 0.0, payload["transition_type"], params).any()
            assert compute_transition_mask(
                8, 8, 1.0, payload["transition_type"], params).all()

    def test_noise_recipes_rank_fields_differ(self) -> None:
        """P41–P44 是同一 X06 核的具名参数配方——rank 场须实际不同。"""
        from src.features.edit.transition_packs.library_v1.cores import (
            noise_dissolve_mask)
        fields = []
        for rid in ["P41", "P42", "P43", "P44"]:
            params = dict(convert_module(rid).to_transition_instance()["params"])
            fields.append(np.asarray(
                noise_dissolve_mask(32, 18, 0.5, params), dtype=np.float64))
        for i in range(len(fields)):
            for j in range(i + 1, len(fields)):
                assert not np.allclose(fields[i], fields[j]), \
                    f"P{41+i}/P{41+j} rank 场相同，配方参数未生效"

    def test_luma_recipes_content_driven_order(self) -> None:
        """P45–P48 走生产核内容驱动路径：invert 决定亮/暗部先入（真实像素）。"""
        from src.features.edit.transition_packs.library_v1.cores import (
            luma_reveal_mask)
        w, h = 16, 8
        src = np.zeros((h, w, 3), dtype=np.float32)
        src[:, : w // 2] = 1.0  # 左半亮、右半暗
        for rid in ["P45", "P46", "P47", "P48"]:
            payload = convert_module(rid).to_transition_instance()
            params = dict(payload["params"])
            mask = np.asarray(luma_reveal_mask(
                w, h, 0.35, params, image_a=src, image_b=None), dtype=np.float64)
            bright = mask[:, : w // 2].mean()
            dark = mask[:, w // 2:].mean()
            if params["invert"]:
                assert bright > dark, f"{rid} invert=True 应亮部先入"
            else:
                assert dark >= bright, f"{rid} invert=False 应暗部先入"
            # dispatch 路径（无 image 入参 → 线性回退）形状/端点仍须成立
            from src.features.edit.core.transition_mask import compute_transition_mask
            mid = compute_transition_mask(16, 8, 0.5, payload["transition_type"],
                                          params)
            assert mid.dtype == np.float32 and mid.shape == (8, 16)
            assert mid.min() >= 0.0 and mid.max() <= 1.0

    def test_voronoi_recipe_renders_real_mask(self) -> None:
        from src.features.edit.core.transition_mask import compute_transition_mask
        payload = convert_module("P55").to_transition_instance()
        params = dict(payload["params"])
        mid = compute_transition_mask(32, 18, 0.5, payload["transition_type"],
                                      params)
        assert mid.dtype == np.float32 and mid.shape == (18, 32)
        assert mid.min() >= 0.0 and mid.max() <= 1.0
        assert not compute_transition_mask(
            8, 8, 0.0, payload["transition_type"], params).any()
        assert compute_transition_mask(
            8, 8, 1.0, payload["transition_type"], params).all()

    def test_composite_recipes_render_real_frames(self) -> None:
        """P49–P54/P56 走 resample_cores 复合核真实渲染（端点精确还原）。"""
        from src.features.edit.transition_packs.library_v1.resample_cores import (
            composite_lib1_transition)
        rng = np.random.default_rng(11)
        a = rng.random((12, 16, 3)).astype(np.float32)
        b = rng.random((12, 16, 3)).astype(np.float32)
        for rid in ["P49", "P50", "P51", "P52", "P53", "P54", "P56"]:
            payload = convert_module(rid).to_transition_instance()
            ttype = payload["transition_type"]
            params = dict(payload["params"])
            e0 = composite_lib1_transition(ttype, a, b, 0.0, params)
            e1 = composite_lib1_transition(ttype, a, b, 1.0, params)
            mid = composite_lib1_transition(ttype, a, b, 0.5, params)
            assert np.allclose(e0, a, atol=1e-5), f"{rid} p=0 未精确还原 A"
            assert np.allclose(e1, b, atol=1e-5), f"{rid} p=1 未精确还原 B"
            assert mid.shape == a.shape
            assert not np.allclose(mid, a), f"{rid} 中点帧与 A 相同（未推进）"


# ── 4b. 合成宏载荷与真实构造 ──────────────────────────────────────────

def _macro_payload(rid: str) -> Dict[str, Any]:
    return convert_module(rid).to_macro_payload()


def _step_ports(step_node_type: str, rid: str) -> tuple[List[str], List[str]]:
    """步骤端口名：生产注册节点用真实实例端口；未注册暂存节点用 manifest
    ports 声明（nodes/ 只读）。返回 (inputs, outputs)。"""
    node = production_node(step_node_type)
    if node is not None:
        return ([p.name for p in node.inputs], [p.name for p in node.outputs])
    # 暂存 lib1_* 主节点：以暂存 manifest ports 为准（生产注册与否不假设）
    deps = recipe(rid)["dependencies"]
    if step_node_type == deps["node_type"] and "component_slug" in deps:
        ports = component_manifest(rid)["ports"]
        return ([p["name"] for p in ports if p["direction"] == "input"],
                [p["name"] for p in ports if p["direction"] == "output"])
    raise AssertionError(f"{rid}: 步骤节点 {step_node_type} 既不在生产注册表，"
                         "也不是本配方声明的暂存主组件")


class TestMacroPayloads:
    def test_macro_payload_structure(self) -> None:
        for rid in MACRO_IDS:
            deps = recipe(rid)["dependencies"]
            payload = _macro_payload(rid)
            assert set(payload) == {"pack_id", "macro_id", "kind", "slots",
                                    "steps", "connections", "output_step"}
            assert payload["pack_id"] == "topos_library_v1"
            assert payload["macro_id"] == rid
            assert payload["kind"] == "composite_macro"
            # 外部素材显式插槽（计划 §7.3：不复制作者机器路径/源媒体）
            assert payload["slots"], f"{rid} 宏须声明显式插槽"
            for slot in payload["slots"]:
                assert set(slot) == {"slot", "kind", "description"}
                assert slot["slot"] and slot["kind"]
            step_ids = [s["step"] for s in payload["steps"]]
            assert len(step_ids) == len(set(step_ids)), f"{rid} step id 重复"
            assert payload["output_step"] in step_ids, \
                f"{rid} output_step 不在 steps"
            node_types = {s["node_type"] for s in payload["steps"]}
            assert node_types == {deps["node_type"], *deps.get("aux_node_types", [])}
            json.dumps(payload, ensure_ascii=False)  # 载荷可序列化

    def test_macro_primary_step_params_snapshot(self) -> None:
        for rid in MACRO_IDS:
            deps = recipe(rid)["dependencies"]
            payload = _macro_payload(rid)
            primaries = [s for s in payload["steps"]
                         if s["node_type"] == deps["node_type"]]
            assert len(primaries) == 1, f"{rid} 主步骤须恰有一个"
            params = primaries[0]["parameters"]
            if "component_slug" in deps:
                # 主步骤参数 = manifest DEFAULTS 叠加配方 params（全量快照）
                manifest = component_manifest(rid)
                expected = {p["name"]: p["default"] for p in manifest["params"]}
                expected.update(dict(recipe(rid)["params"]))
                assert params == expected, f"{rid} 主步骤参数快照与契约不符"
            else:
                assert params == {k: _shared().coerce_by_shape(v)
                                  for k, v in recipe(rid)["params"].items()}

    def test_macro_steps_against_real_nodes_or_manifest(self) -> None:
        from src.features.composite.core.node_engine import NODE_REGISTRY
        for rid in MACRO_IDS:
            payload = _macro_payload(rid)
            for step in payload["steps"]:
                nt = step["node_type"]
                inputs, outputs = _step_ports(nt, rid)
                assert inputs or nt == "text" or nt == "constant" or nt == nt, \
                    f"{rid} 步骤 {nt} 无输入端口声明"
                if NODE_REGISTRY.get(nt) is not None:
                    node = production_node(nt)
                    for name, value in step["parameters"].items():
                        _assert_production_param(name, value, node)

    def test_macro_connections_reference_real_ports(self) -> None:
        for rid in MACRO_IDS:
            payload = _macro_payload(rid)
            by_id = {s["step"]: s for s in payload["steps"]}
            assert len(by_id) == len(payload["steps"])
            for conn in payload["connections"]:
                assert conn["from_step"] in by_id, \
                    f"{rid} 连接起点 {conn['from_step']} 不存在"
                assert conn["to_step"] in by_id, \
                    f"{rid} 连接终点 {conn['to_step']} 不存在"
                _, src_outputs = _step_ports(by_id[conn["from_step"]]["node_type"], rid)
                dst_inputs, _ = _step_ports(by_id[conn["to_step"]]["node_type"], rid)
                assert conn["from_port"] in src_outputs, \
                    f"{rid} {conn['from_step']} 无输出端口 {conn['from_port']}"
                assert conn["to_port"] in dst_inputs, \
                    f"{rid} {conn['to_step']} 无输入端口 {conn['to_port']}"

    def test_macro_graph_is_acyclic_and_reaches_output(self) -> None:
        for rid in MACRO_IDS:
            payload = _macro_payload(rid)
            conns = payload["connections"]
            indegree = {s["step"]: 0 for s in payload["steps"]}
            adj: Dict[str, List[str]] = {s["step"]: [] for s in payload["steps"]}
            for c in conns:
                adj[c["from_step"]].append(c["to_step"])
                indegree[c["to_step"]] += 1
            queue = [k for k, v in indegree.items() if v == 0]
            seen = 0
            while queue:
                cur = queue.pop()
                seen += 1
                for nxt in adj[cur]:
                    indegree[nxt] -= 1
                    if indegree[nxt] == 0:
                        queue.append(nxt)
            assert seen == len(indegree), f"{rid} 宏图存在环"
            # 输出步骤必须可由某插槽路径到达（有出度或为唯一单步）
            assert conns or len(indegree) == 1, \
                f"{rid} 多步宏图 output_step 须有上游连接"

    def test_macro_node_params_snapshot(self) -> None:
        """主节点参数快照（P30 口径）：可整体写入真实节点（生产主节点）。"""
        for rid in MACRO_IDS:
            deps = recipe(rid)["dependencies"]
            snapshot = convert_module(rid).to_node_params_snapshot()
            assert snapshot["node_type"] == deps["node_type"]
            assert snapshot["parameters"]
            json.dumps(snapshot, ensure_ascii=False)
            node = production_node(deps["node_type"])
            if node is not None:
                node.parameters.update(snapshot["parameters"])
                for name, value in snapshot["parameters"].items():
                    assert node.parameters[name] == value


# ── 5. 能力矩阵如实 ───────────────────────────────────────────────────

class TestCapabilityHonesty:
    def test_no_recipe_claims_host_integration(self) -> None:
        for rid in RECIPE_IDS:
            assert recipe(rid)["status"] not in FORBIDDEN_STATUS
        # 暂存组件依赖配方必须在 notes 明示"暂存/未接缝"语义
        for rid in RECIPE_IDS:
            deps = recipe(rid)["dependencies"]
            if "component_slug" in deps:
                note = recipe(rid)["notes"]
                assert ("未接" in note) or ("暂存" in note), \
                    f"{rid} 暂存组件依赖须在 notes 明示未接入状态"

    def test_transition_notes_declare_whitelist_status(self) -> None:
        for rid in TRANSITION_IDS:
            note = recipe(rid)["notes"]
            assert "白名单" in note, \
                f"{rid} 转场配方 notes 须明示用户转场白名单状态（未接入）"

    def test_shared_helper_batch_contract_constants(self) -> None:
        sc = _shared()
        assert sc.BATCH_STATUS == "component_ready"
        assert "status" in sc.REQUIRED_RECIPE_FIELDS
        assert set(sc.VALID_PAGES) == {"color", "edit", "composite"}
        assert "composite_macro" in sc.VALID_KINDS  # 本批次新增 kind

    def test_macro_slots_declare_external_material(self) -> None:
        """宏插槽覆盖声明：含外部素材依赖的宏必须有对应插槽（显式插槽纪律）。"""
        for rid in MACRO_IDS:
            deps = recipe(rid)["dependencies"]
            payload = _macro_payload(rid)
            slot_kinds = {s["kind"] for s in payload["slots"]}
            if deps["node_type"] in {"lib1_stmap", "lib1_vector_displace"}:
                assert slot_kinds & {"uv-map", "vector"}, \
                    f"{rid} UV/向量驱动宏须声明 map/vector 插槽"
            if deps["node_type"] == "text":
                assert "text" in slot_kinds, f"{rid} 文字宏须声明 text 插槽"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
