"""L18 配方共享转换 helper（P01–P80 批次；P29 试点保持自有 convert.py）。

真实转换纪律（与 P29 convert.py 同一口径，specs/spec_schema.md §6）：
- recipe.json 是源配方 manifest，不冒充宿主格式；
- 只声明真实存在的依赖：生产插件 ``plugins/<X>.tglsl``、暂存组件
  ``content_staging/library_v1/{filters,transitions}/<slug>/component_manifest.json``、
  生产节点插件（plugins/node_plugins/）、transition_catalog 已注册的 ``lib1_*`` 类型；
- 本模块只做无副作用的载荷构造：不写工程、不碰全局 registry、不加载 GPU；
  版本/内容 hash 由应用时的 plugin_identity（M07.1）与既有命令链负责；
- 暂存组件（status=component_ready，未接 L05 白名单）的载荷是组件参数载荷，
  不是已可应用的宿主载荷——配方 status 字段如实反映这一点。

强转口径：
- 生产插件载荷（edit/color）：按值形态强转（bool→bool、str→select label、
  3 数值列表→color 元组、数值→float），与 P29 完全一致；
- 暂存组件/转场载荷：按 component_manifest.json 的 ptype 权威强转，
  未知参数名直接 raise（转换期诚实失败，不静默丢弃）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Mapping

_DIR = Path(__file__).resolve().parent            # .../presets
LIBRARY_ROOT = _DIR.parent                        # .../library_v1
REPO_ROOT = _DIR.parents[2]                       # 仓库根

PACK_ID = "topos_library_v1"

#: L18 批次 recipe.json 必填字段（P01–P40 除 P29 外的批次契约）
REQUIRED_RECIPE_FIELDS = (
    "recipe_id", "name", "kind", "status", "version", "working_space",
    "applies_pages", "strength_hint", "component", "dependencies",
    "params", "notes",
)

VALID_KINDS = ("filter_look", "transition", "composite_macro")
VALID_PAGES = ("color", "edit", "composite")
BATCH_STATUS = "component_ready"


def load_recipe(path: Path | str) -> Dict[str, Any]:
    """读取源配方 manifest（recipe.json）。"""
    return json.loads(Path(path).read_text(encoding="utf-8"))


# ── 值形态强转（生产插件载荷，P29 口径） ──────────────────────────────

def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def coerce_by_shape(value: Any) -> Any:
    """按 JSON 值形态强转为载荷值：bool/str 原样，3 数值列表→color 元组，
    2 数值列表→vec2 列表，数值保留 int/float（slider float、节点 int 参数
    各得其所）。"""
    if isinstance(value, bool) or isinstance(value, str):
        return value
    if (isinstance(value, (list, tuple)) and len(value) == 3
            and all(_is_number(v) for v in value)):
        return tuple(float(v) for v in value)
    if (isinstance(value, (list, tuple)) and len(value) == 2
            and all(_is_number(v) for v in value)):
        return [float(v) for v in value]
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        return float(value)
    raise TypeError(f"无法按形态强转配方参数值: {value!r}")


def edit_effect_params(data: Mapping[str, Any]) -> Dict[str, Any]:
    """Edit clip 效果载荷：EffectInstance.params（param 名= @param var_name，
    select 用 label 字符串、checkbox 用 bool——catalog ParamDef 口径）。"""
    return {k: coerce_by_shape(v) for k, v in data["params"].items()}


def color_layer_plugin(data: Mapping[str, Any], instance_id: str) -> Dict[str, Any]:
    """Color 层插件实例载荷（layer["plugins"] 列表元素四字段形态，layer.py 口径）。"""
    return {
        "instance_id": str(instance_id),
        "plugin_id": str(data["dependencies"]["plugin_id"]),
        "params": {
            k: (list(v) if isinstance(v, tuple) else v)
            for k, v in edit_effect_params(data).items()
        },
        "enabled": True,
    }


# ── manifest 驱动强转（暂存组件 / 转场载荷） ──────────────────────────

#: manifest kind（单数）→ 暂存区目录名（复数）
COMPONENT_KIND_DIRS = {
    "filter": "filters",
    "transition": "transitions",
    "node": "nodes",
}


def load_component_manifest(component_kind: str, slug: str) -> Dict[str, Any]:
    """读取暂存组件 manifest（filters|transitions|nodes/<slug>/component_manifest.json）。"""
    directory = COMPONENT_KIND_DIRS.get(component_kind)
    if directory is None:
        raise ValueError(f"未知组件 kind {component_kind!r}（{sorted(COMPONENT_KIND_DIRS)}）")
    path = LIBRARY_ROOT / directory / slug / "component_manifest.json"
    return json.loads(path.read_text(encoding="utf-8"))


def coerce_by_manifest(value: Any, pspec: Mapping[str, Any]) -> Any:
    """按 component_manifest.json 的 ptype 权威强转（越界/类型错即 raise）。"""
    ptype = pspec["ptype"]
    if ptype == "int":
        if not _is_number(value):
            raise TypeError(f"参数 {pspec['name']} 须为数值，得到 {value!r}")
        out: Any = int(round(float(value)))
    elif ptype == "float":
        if not _is_number(value):
            raise TypeError(f"参数 {pspec['name']} 须为数值，得到 {value!r}")
        out = float(value)
    elif ptype == "bool":
        if not isinstance(value, bool):
            raise TypeError(f"参数 {pspec['name']} 须为 bool，得到 {value!r}")
        out = value
    elif ptype == "option":
        if not isinstance(value, str) or value not in tuple(pspec.get("options") or ()):
            raise ValueError(
                f"参数 {pspec['name']} 值 {value!r} 不在选项 {list(pspec.get('options') or ())}")
        out = value
    elif ptype == "color":
        if not (isinstance(value, (list, tuple)) and len(value) == 3
                and all(_is_number(v) for v in value)):
            raise TypeError(f"参数 {pspec['name']} 须为 3 元组，得到 {value!r}")
        out = [float(v) for v in value]
    elif ptype == "vec2":
        if not (isinstance(value, (list, tuple)) and len(value) == 2
                and all(_is_number(v) for v in value)):
            raise TypeError(f"参数 {pspec['name']} 须为 2 元组，得到 {value!r}")
        out = [float(v) for v in value]
    else:
        raise TypeError(f"组件参数 ptype {ptype!r} 不受配方载荷支持")
    lo, hi = pspec.get("minimum"), pspec.get("maximum")
    if _is_number(out) and ((lo is not None and out < lo) or (hi is not None and out > hi)):
        raise ValueError(f"参数 {pspec['name']} 值 {out} 超出 [{lo}, {hi}]")
    return out


def component_payload(data: Mapping[str, Any]) -> Dict[str, Any]:
    """暂存组件参数载荷：pack_id + 组件身份 + manifest 强转后的 params。
    未知参数名 raise（转换期诚实失败）。"""
    deps = data["dependencies"]
    comp_kind = str(deps["component_kind"])
    slug = str(deps["component_slug"])
    manifest = load_component_manifest(comp_kind, slug)
    if manifest["component_id"] != deps["component_id"]:
        raise ValueError(
            f"component_id 不匹配：配方 {deps['component_id']!r} vs manifest {manifest['component_id']!r}")
    known = {p["name"]: p for p in manifest["params"]}
    params: Dict[str, Any] = {}
    for key, value in data["params"].items():
        if key not in known:
            raise ValueError(f"配方参数 {key} 不在组件 {slug} schema（防悬空引用）")
        params[key] = coerce_by_manifest(value, known[key])
    return {
        "pack_id": PACK_ID,
        "component_id": manifest["component_id"],
        "slug": slug,
        "kind": manifest["kind"],
        "params": params,
    }


def transition_instance(data: Mapping[str, Any]) -> Dict[str, Any]:
    """Edit 转场实例载荷：TransitionInstance(transition_type, params) 直填形态。
    transition_type 须为 transition_catalog 已注册的 lib1_* 类型（测试另验），
    params 按暂存组件 manifest 强转（生产核 DEFAULTS 同名同域，测试另验）。"""
    payload = component_payload(data)
    return {
        "transition_type": str(data["dependencies"]["transition_type"]),
        "params": payload["params"],
    }


def user_transition_json(data: Mapping[str, Any], user_id: str,
                         label: str | None = None) -> Dict[str, Any]:
    """用户转场 JSON 载荷（custom_transitions 声明格式：id/label/base/params）。

    base 必须在 custom_transitions._BASE_TYPES 白名单（L05 口径）：
    当前仅 lib1_clock_wipe / lib1_true_push；不在白名单的 base 直接 raise，
    不产出宿主拒绝的载荷。需要宿主模块（repo 根在 sys.path）。
    """
    from src.features.edit.core import custom_transitions  # 生产行为事实源

    base = str(data["dependencies"]["transition_type"])
    if base not in custom_transitions._BASE_TYPES:
        raise ValueError(
            f"base {base!r} 不在 custom_transitions._BASE_TYPES 白名单，"
            "不能产出用户转场 JSON（未接入 = 不可声明，防悬空引用）")
    params = component_payload(data)["params"]
    return {
        "id": str(user_id),
        "label": str(label if label is not None else data["name"]),
        "base": base,
        "params": params,
    }


def node_params_snapshot(data: Mapping[str, Any]) -> Dict[str, Any]:
    """Composite 节点参数快照载荷（node_type + parameters 子集）。"""
    deps = data["dependencies"]
    return {
        "node_type": str(deps["node_type"]),
        "parameters": {k: coerce_by_shape(v) for k, v in data["params"].items()},
    }


# ── 合成宏模板载荷（P57–P80；暂存级图模板，非宿主图 JSON） ─────────────

def macro_payload(data: Mapping[str, Any], graph: Mapping[str, Any]) -> Dict[str, Any]:
    """Composite 宏模板载荷（暂存级图模板）。

    spec_schema.md §6：manifest 不冒充宿主 Composite nodes/connections JSON——
    本载荷是**源配方侧图模板**（step 本地 id + 槽位），宿主图导入（节点 ID/
    连接/表达式重映射）是 L05 转换入口的事，本模块只做无副作用构造。

    graph 契约（各宏 convert.py 内声明）：
    - slots: [{"slot", "kind", "description"}]——外部素材显式插槽；
    - steps: [{"step", "node_type", "parameters"?}]，恰有一步 node_type ==
      dependencies["node_type"]（主步骤）；
    - connections: [{"from_step", "from_port", "to_step", "to_port"}]；
    - output_step: 输出步骤 id。
    主步骤参数 = 暂存组件 manifest DEFAULTS 叠加配方 params（manifest 强转）；
    暂存组件未声明时（生产节点为主步骤）= 配方 params 子集（其余应用时取
    生产节点默认）；其余步骤 parameters 按值形态强转透传。
    """
    deps = data["dependencies"]
    node_type = str(deps["node_type"])
    manifest = None
    if "component_slug" in deps:
        manifest = load_component_manifest(
            str(deps["component_kind"]), str(deps["component_slug"]))
        if manifest["component_id"] != deps["component_id"]:
            raise ValueError(
                f"component_id 不匹配：配方 {deps['component_id']!r} vs manifest {manifest['component_id']!r}")
        if manifest["kind"] != deps["component_kind"]:
            raise ValueError(
                f"component_kind 不匹配：配方 {deps['component_kind']!r} vs manifest {manifest['kind']!r}")

    primary_seen = 0
    steps: List[Dict[str, Any]] = []
    for raw in graph["steps"]:
        s_node = str(raw["node_type"])
        if s_node == node_type:
            primary_seen += 1
            if manifest is not None:
                params: Dict[str, Any] = {p["name"]: p["default"]
                                          for p in manifest["params"]}
                params.update(component_payload(data)["params"])
            else:
                params = {k: coerce_by_shape(v)
                          for k, v in data["params"].items()}
        else:
            params = {k: coerce_by_shape(v)
                      for k, v in (raw.get("parameters") or {}).items()}
        steps.append({"step": str(raw["step"]), "node_type": s_node,
                      "parameters": params})
    if primary_seen != 1:
        raise ValueError(
            f"宏图须恰有一个主步骤（node_type={node_type}），得到 {primary_seen}")

    return {
        "pack_id": PACK_ID,
        "macro_id": str(data["recipe_id"]),
        "kind": "composite_macro",
        "slots": [{"slot": str(s["slot"]), "kind": str(s["kind"]),
                   "description": str(s.get("description", ""))}
                  for s in graph["slots"]],
        "steps": steps,
        "connections": [{"from_step": str(c["from_step"]),
                         "from_port": str(c["from_port"]),
                         "to_step": str(c["to_step"]),
                         "to_port": str(c["to_port"])}
                        for c in graph["connections"]],
        "output_step": str(graph["output_step"]),
    }


def recipe_dir_for(preset_root: Path | str, recipe_id: str) -> Path:
    """按 pNN_ 前缀定位配方目录（p29_quartet_gradient 等既有目录兼容）。"""
    prefix = recipe_id.lower() + "_"
    root = Path(preset_root)
    matches = sorted(p for p in root.iterdir()
                     if p.is_dir() and p.name.startswith(prefix))
    if len(matches) != 1:
        raise FileNotFoundError(f"{recipe_id} 应恰有一个 pNN_* 目录，找到 {len(matches)}")
    return matches[0]


def all_recipe_dirs(preset_root: Path | str, ids: List[str]) -> Dict[str, Path]:
    return {rid: recipe_dir_for(Path(preset_root), rid) for rid in ids}
