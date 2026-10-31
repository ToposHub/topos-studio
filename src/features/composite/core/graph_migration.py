"""
Graph schema migration for the composite engine.

Handles forward migration of composite graph data so that projects saved
with older schema versions can be opened in newer builds without errors.

Migration strategy:
  - Each schema version bump has a dedicated `migrate_vN_to_vN+1()` function.
  - `migrate_graph()` runs the chain from the stored version up to the current version.
  - Unknown / missing version is treated as version 1 (phase 1 format).
  - All migrations are pure-dict transforms — no node instantiation required.

Schema history:
  v1: Phase 1.  Nodes have basic parameters.  BlendNode type is "blend".
  v2: Phase 2.  Format/BBox/Alpha/ColorSpace/Channel/Merge-overhaul.
      - "blend" type aliased to "merge" with new parameters.
      - image_input gains color_space parameter.
      - All nodes gain disabled field default.
      - New node types: premult, unpremult, shuffle, copy_channel,
        cs_transform, reformat, crop.
  v3: Transform pixel-space params + alpha_premultiplied fix.
      - transform pivot_x/y changed from UV (0~1) to pixel space.
      - transform translate_x/y changed from UV (-1~1) to pixel space.
      - Source nodes (image_input, constant, color_input) now correctly
        report alpha_premultiplied=False (straight alpha).
  v4: Crop node parameter rename.
      - crop "reformat" → "affect_format"
      - crop "outside" → "outside_policy"
      - crop outside_policy gains "repeat" option.
  v5: Effect node output port rename.
      - premult, unpremult, clamp output port renamed from "image" to "result".
      - Connections to old "image" port are migrated to "result".
  v6: Per-node keyframe tracks / time_range / node_color.
      - Legacy animation stored as dict values inside node "parameters"
        ({"animated": True, "keyframes": [...]}) is hoisted out to a
        dedicated per-node "keyframe_tracks" dict keyed by param name.
      - Graph-level "keyframe_tracks" (old shared store) is distributed to
        the first node of each referenced type when no per-node store exists.
      - Custom node colors previously only held in runtime header_color are
        now persisted under each node's "node_color".
  v7: Scene3D node split (Phase 7, Nuke route).
      - Scene3D's 67 camera/light/geometry/model/animation params are migrated
        into 4 spawned child nodes (Camera3D/Light3D/Geo3D/Model3D), each
        connected to Scene3D's new scene input ports (camera_in/lights_in/
        geometry_in). Scene3D keeps only canvas/background/gizmo
        params. Model3D is spawned only when the original had a model_path
        with use_imported_model=True.
  v8: Scene3D three scene input ports merged into single `in` port.
  v9: Scene3D scene-environment params moved to global "3D Scene Settings".
      - Scene3D becomes a pure renderer: scene-environment params (cam_*,
        bg_*, light_*, hdri_preset, use_ibl, show_grid, show_axes, output_*,
        msaa, active_camera_id) are moved from each Scene3D node into the
        graph-level viewer_state["default_3d_scene"] dict (the global
        "3D Scene Settings" edited via Properties panel). All Scene3D nodes
        share the same global settings. The first Scene3D node's values are
        used as the global defaults; subsequent Scene3D nodes' scene params
        are dropped (with a warning if they differ). Scene3D keeps only
        width/height/format_name + legacy geometry/material params.
  v10: AE-style layer stacks (AE/Nuke hybrid, plan §3).
      - Adds two optional graph-level dicts: ``layer_stacks`` (stack_id ->
        LayerStack) and ``layers`` (layer_id -> Layer).
      - No existing fields change.  Pre-v10 projects simply have no layer
        stacks; the controller may lazily project one from the Output node's
        upstream chain on first entry to Layers mode (plan §1.2.7 / §12.2).
      - Node base class gains ``supports_layer_root`` /
        ``supports_layer_opacity`` ClassVars (default False); Transform/Text
        opt in.  These are class-level defaults and require no per-node
        serialization change.
  v11: Per-node optional ``user_label`` (B4 canvas rename).
      - Each node may carry a ``user_label`` string; display falls back to
        the auto number when absent.  Purely additive no-op migration.
"""

import copy
from typing import Any, Dict, List

# Current schema version — bump this when adding new fields/nodes
COMPOSITE_SCHEMA_VERSION = 11


# ── Per-node parameter defaults for v2 ──

_V2_NODE_DEFAULTS: Dict[str, Dict[str, Any]] = {
    "merge": {
        "blend_mode": "over",
        "opacity": 1.0,
        "format_policy": "from_b",
        "bbox_policy": "union",
    },
    "image_input": {
        "color_space": "auto",
    },
    "constant": {
        "width": 1920,
        "height": 1080,
        "color_r": 0.0,
        "color_g": 0.0,
        "color_b": 0.0,
        "color_a": 1.0,
    },
    "output": {
        "resolution_mode": "auto",
        "width": 1920,
        "height": 1080,
    },
    "blur": {
        "radius": 5.0,
    },
    "grade": {
        "black_point": 0.0,
        "white_point": 1.0,
        "lift": 0.0,
        "gamma": 1.0,
        "gain": 1.0,
        "offset": 0.0,
    },
    "transform": {
        "translate_x": 0.0,
        "translate_y": 0.0,
        "scale_x": 1.0,
        "scale_y": 1.0,
        "rotation": 0.0,
        "pivot_mode": "center",
        "pivot_x": 0.0,
        "pivot_y": 0.0,
        "filter": "linear",
    },
    "reformat": {
        "format_mode": "preset",
        "preset": "HD 1080",
        "width": 1920,
        "height": 1080,
        "resize_mode": "fit",
        "filter": "linear",
        "preserve_bbox": False,
    },
    "crop": {
        "x": 0,
        "y": 0,
        "width": 512,
        "height": 512,
        "affect_format": True,
        "outside_policy": "transparent",
    },
    "shuffle": {
        "r_from": "r",
        "g_from": "g",
        "b_from": "b",
        "a_from": "a",
    },
    "copy_channel": {
        "src_channel": "a",
        "dst_channel": "a",
    },
    "cs_transform": {
        "direction": "srgb_to_linear",
    },
    "invert": {},
    "glow": {
        "threshold": 0.5,
        "radius": 10.0,
        "intensity": 1.0,
    },
    "denoise": {
        "strength": 0.5,
    },
    "mask": {},
    "matte": {},
    "keyer": {},
    "color_input": {},
    "premult": {},
    "unpremult": {},
    "video_frame": {},
    "ai_generate": {},
    "ai_inpaint": {},
    "ai_expand": {},
    "ai_remove": {},
    "file_output": {},
}

# Map old "blend" type to new type name
_TYPE_ALIASES: Dict[str, str] = {
    "blend": "merge",
}


def _iter_nodes(graph_data: dict) -> List[Dict[str, Any]]:
    """Return the graph's node dicts, tolerating corrupt ``nodes`` payloads.

    A missing / null / non-list ``nodes`` key (or non-dict members inside
    it) must never crash a migration — corrupt files are treated as having
    no nodes (AUD-4702).
    """
    nodes = graph_data.get("nodes") if isinstance(graph_data, dict) else None
    if not isinstance(nodes, (list, tuple)):
        return []
    return [n for n in nodes if isinstance(n, dict)]


def _iter_connections(graph_data: dict) -> List[Dict[str, Any]]:
    """Return the graph's connection dicts, tolerating corrupt payloads.

    Same corruption contract as :func:`_iter_nodes` (AUD-4702).
    """
    conns = graph_data.get("connections") if isinstance(graph_data, dict) else None
    if not isinstance(conns, (list, tuple)):
        return []
    return [c for c in conns if isinstance(c, dict)]


def migrate_v1_to_v2(graph_data: dict) -> dict:
    """Migrate a v1 composite graph dict to v2 format.

    Changes applied:
      1. "blend" node type → "merge"
      2. Add missing parameters with safe defaults for every node.
      3. Add "disabled" field if absent.
      4. Add "color_space" to image_input nodes.
    """
    graph_data = copy.deepcopy(graph_data)

    for node_data in _iter_nodes(graph_data):
        # 1. Type alias
        old_type = node_data.get("type", "")
        if old_type in _TYPE_ALIASES:
            node_data["type"] = _TYPE_ALIASES[old_type]

        node_type = node_data.get("type", "")

        # 2. Ensure parameters dict exists
        if "parameters" not in node_data:
            node_data["parameters"] = {}

        # Fill missing parameters from v2 defaults
        defaults = _V2_NODE_DEFAULTS.get(node_type, {})
        for key, default_val in defaults.items():
            if key not in node_data["parameters"]:
                node_data["parameters"][key] = default_val

        # 3. Ensure disabled field
        if "disabled" not in node_data:
            node_data["disabled"] = False

        # 4. image_input: ensure color_space parameter
        if node_type == "image_input":
            if "color_space" not in node_data["parameters"]:
                node_data["parameters"]["color_space"] = "auto"

    # Ensure connections list exists
    if "connections" not in graph_data:
        graph_data["connections"] = []

    return graph_data


def migrate_v2_to_v3(graph_data: dict) -> dict:
    """Migrate v2 → v3: Transform pixel-space params + alpha fix.

    Transform changes:
      - New pivot_mode parameter: "center" (default) or "custom".
      - Old UV-space pivot (0.5,0.5 = center) maps to pivot_mode="center".
      - Old non-center UV pivot converts to pixels with pivot_mode="custom".
      - translate_x/y was UV-space (-1~1).  Now pixel-space.
      - Old param names (rotate, scale, anchor_x/y) → new names.
      - y-axis uses REF_H (1080) not REF_W (1920).
    """
    graph_data = copy.deepcopy(graph_data)

    REF_W = 1920
    REF_H = 1080

    for node_data in _iter_nodes(graph_data):
        node_type = node_data.get("type", "")
        if node_type != "transform":
            continue

        params = node_data.get("parameters", {})

        # Handle old v2 default names that didn't match the node's actual params
        if "rotate" in params and "rotation" not in params:
            params["rotation"] = params.pop("rotate")
        if "scale" in params and "scale_x" not in params:
            s = params.pop("scale")
            params["scale_x"] = s
            params.setdefault("scale_y", s)
        if "anchor_x" in params and "pivot_x" not in params:
            params["pivot_x"] = params.pop("anchor_x")
        if "anchor_y" in params and "pivot_y" not in params:
            params["pivot_y"] = params.pop("anchor_y")

        # If pivot_mode is already set (by v1→v2 migration or v2 defaults),
        # respect it — don't re-derive from pivot_x/y values.
        if "pivot_mode" not in params:
            # Legacy v2 data without pivot_mode — derive from UV pivot values
            px = params.get("pivot_x", 0.5)
            py = params.get("pivot_y", 0.5)

            # Old default was (0.5, 0.5) in UV space = image center
            if abs(px - 0.5) < 0.01 and abs(py - 0.5) < 0.01:
                params["pivot_mode"] = "center"
                params["pivot_x"] = 0.0
                params["pivot_y"] = 0.0
            elif 0.0 <= px <= 1.0 and 0.0 <= py <= 1.0:
                # Non-center UV-space pivot — convert to pixels
                params["pivot_mode"] = "custom"
                params["pivot_x"] = px * REF_W
                params["pivot_y"] = py * REF_H
            else:
                # Already in some non-UV space — keep as custom
                params["pivot_mode"] = "custom"
        elif params["pivot_mode"] == "center":
            # Already center — ensure clean pixel-space defaults
            params["pivot_x"] = 0.0
            params["pivot_y"] = 0.0

        # Convert translate from UV-space to pixel-space
        tx = params.get("translate_x", 0.0)
        ty = params.get("translate_y", 0.0)
        if -1.0 <= tx <= 1.0 and -1.0 <= ty <= 1.0 and (tx != 0.0 or ty != 0.0):
            params["translate_x"] = tx * REF_W
            params["translate_y"] = ty * REF_H

    return graph_data


def migrate_v3_to_v4(graph_data: dict) -> dict:
    """Migrate v3 → v4: Crop node parameter rename.

      - crop "reformat"   → "affect_format"
      - crop "outside"    → "outside_policy"
    """
    graph_data = copy.deepcopy(graph_data)

    for node_data in _iter_nodes(graph_data):
        if node_data.get("type") != "crop":
            continue

        params = node_data.get("parameters", {})

        # Rename reformat → affect_format
        if "reformat" in params:
            params["affect_format"] = params.pop("reformat")

        # Rename outside → outside_policy
        if "outside" in params:
            params["outside_policy"] = params.pop("outside")

    return graph_data


def migrate_v4_to_v5(graph_data: dict) -> dict:
    """Migrate v4 → v5: Effect node output port rename.

    Effect nodes (premult, unpremult, clamp) output port renamed from
    "image" to "result" to conform to the global port naming rule:
    - Source/generator nodes: can output "image", "matte", "rgba"
    - Effect nodes: unified output "result"

    Connections referencing the old "image" output port are migrated
    to the new "result" port.
    """
    graph_data = copy.deepcopy(graph_data)

    # Node types with port rename: "image" → "result"
    PORT_RENAME_NODES = {"premult", "unpremult", "clamp"}
    OLD_PORT = "image"
    NEW_PORT = "result"

    # Build a map of node_id -> node_type for quick lookup
    node_types: Dict[str, str] = {}
    for node_data in _iter_nodes(graph_data):
        node_types[node_data.get("id", "")] = node_data.get("type", "")

    # Update connections: rename src_port from "image" to "result"
    # for affected node types
    for conn in _iter_connections(graph_data):
        src_node_id = conn.get("src_node_id", "")
        src_port = conn.get("src_port_name", "")

        # Check if source node is one of the affected types
        src_type = node_types.get(src_node_id, "")
        if src_type in PORT_RENAME_NODES and src_port == OLD_PORT:
            conn["src_port_name"] = NEW_PORT

    return graph_data


def migrate_v5_to_v6(graph_data: dict) -> dict:
    """Migrate v5 → v6: per-node keyframe_tracks / time_range / node_color.

    Three transformations:

    1. Legacy animation embedded inside ``node["parameters"][param]`` as a
       dict ``{"animated": ..., "keyframes": [...]}`` is hoisted to a
       per-node ``keyframe_tracks`` store keyed by param name, and the
       original parameter value is restored to its plain scalar default.

    2. The graph-level ``keyframe_tracks`` dict (old shared store, keyed by
       ``node_id.param`` or ``param_name``) is distributed onto the
       corresponding node when that node has no per-node store yet. This
       preserves animation authored before P1.

    3. Node colors are not reconstructable from dict data alone (they lived
       only in runtime header_color), so no color synthesis happens here.
       Custom colors simply start persisting from v6 onward via to_dict.
    """
    graph_data = copy.deepcopy(graph_data)

    # 1 + 2: per-node keyframe tracks
    graph_level_kfs = graph_data.get("keyframe_tracks")
    if not isinstance(graph_level_kfs, dict):
        graph_level_kfs = {}

    for node_data in _iter_nodes(graph_data):
        if not isinstance(node_data, dict):
            continue
        node_id = node_data.get("id", "")
        params = node_data.get("parameters")
        if not isinstance(params, dict):
            params = {}
            node_data["parameters"] = params

        node_tracks = node_data.get("keyframe_tracks")
        if not isinstance(node_tracks, dict):
            node_tracks = {}
            node_data["keyframe_tracks"] = node_tracks

        # 1. Hoist legacy {animated, keyframes} dict values out of parameters
        legacy_keys = []
        for pname, pval in list(params.items()):
            if isinstance(pval, dict) and (
                pval.get("animated") or pval.get("keyframes")
            ):
                kfs = pval.get("keyframes") or []
                if kfs:
                    node_tracks.setdefault(pname, {
                        "param_name": pname,
                        "keyframes": copy.deepcopy(kfs),
                        "min_val": pval.get("min_val", 0.0),
                        "max_val": pval.get("max_val", 1.0),
                    })
                legacy_keys.append(pname)

        # Restore plain scalar defaults for hoisted params so the evaluator
        # sees a normal value rather than a dict.
        for pname in legacy_keys:
            params[pname] = 0.0

        # 2. Distribute graph-level tracks that target this node
        for gkey, gtrack in graph_level_kfs.items():
            if not isinstance(gtrack, dict):
                continue
            # Graph-level keys were either "node_id.param" or bare "param".
            if "." in gkey:
                g_nid, g_pname = gkey.split(".", 1)
            else:
                g_nid, g_pname = node_id, gkey
            if g_nid != node_id:
                continue
            g_kfs = gtrack.get("keyframes") or []
            if g_kfs and g_pname not in node_tracks:
                node_tracks[g_pname] = {
                    "param_name": g_pname,
                    "keyframes": copy.deepcopy(g_kfs),
                    "min_val": gtrack.get("min_val", 0.0),
                    "max_val": gtrack.get("max_val", 1.0),
                }

        # Drop empty tracks dict to keep serialization clean
        if not node_tracks:
            node_data.pop("keyframe_tracks", None)

    # The graph-level store has been distributed; remove it to avoid double
    # counting in detect_composite_mode().
    graph_data.pop("keyframe_tracks", None)

    return graph_data


# ──────────────────────────────────────────────────────────────────────────
# P2: 共享 3D 场景子节点工厂（migration + 实时创建复用）
# ──────────────────────────────────────────────────────────────────────────

# 子节点相对 scene3d 的位置偏移（左侧纵向错开，避免重叠）。
_SCENE3D_CHILD_OFFSET = {
    "camera3d": (-450, -180),
    "light3d":  (-450, -60),
    "geo3d":    (-450, 60),
    "model3d":  (-450, 180),
}

# 子节点 → Scene3D scene 输入端口（v8：全部并入单一 `in` 端口）。
# Scene3D 按 payload 类型自动分派，无需按端口区分源类型。
_SCENE3D_CHILD_PORT = {
    "camera3d": "in",
    "light3d":  "in",
    "geo3d":    "in",
    "model3d":  "in",
}


def spawn_3d_scene_children(scene3d_id: str, scene3d_position,
                            scene3d_params: dict, existing_ids,
                            include_model: bool = True,
                            spawn_model_only: bool = False,
                            include_camera: bool = True,
                            include_light: bool = True,
                            include_geo: bool = True):
    """Spawn Scene3D 的子节点 + 连接（纯 dict 变换，供 migration 与实时创建复用）。

    把 scene3d 的相机/灯光/几何/模型参数拆成子节点 dict + 连接 dict。
    migration 和实时节点创建（拖拽模型/菜单建 Scene3D）共用此内核，保证两条
    路径产出的图结构一致（P2：消除"迁移 vs 新建"分歧）。

    Args:
        scene3d_id: Scene3D 节点 id（连接的 dst）。
        scene3d_position: Scene3D 的 [x, y] 位置（子节点据此偏移）。
        scene3d_params: Scene3D 的参数 dict（从中抽取迁移到子节点的参数）。
            实时创建时可传默认参数 dict（缺省键由 from_dict 填默认）。
        existing_ids: 已有 node id 集合（避免冲突）；本函数会向其追加新 id。
        include_model: 是否 spawn Model3D（拖拽模型时 True，菜单建默认 False）。
        spawn_model_only: True 时只 spawn Model3D（拖拽模型到已有 Scene3D 场景时用：
            camera/light/geo 已存在，只补 model3d）。
        include_camera: 是否 spawn 默认 Camera3D。拖拽普通模型时可设 False，使用
            Scene3D 内置 fallback 相机。
        include_light: 是否 spawn 默认 Light3D。拖拽普通模型时可设 False，使用
            Scene3D 内置 fallback 灯光。
        include_geo: 是否 spawn 默认 Geo3D。拖拽模型时设 False，避免默认 primitive
            与导入模型混在一起，造成“导入后显示的是 Geo3D”的误判。

    Returns:
        (child_nodes, child_conns): 子节点 dict 列表 + 连接 dict 列表。
        调用方负责 append 到 graph 的 nodes/connections（migration）或实例化（实时）。
    """
    pos_x, pos_y = scene3d_position[0], scene3d_position[1]

    def _new_id(base: str) -> str:
        i = 0
        while True:
            cand = f"{base}_{i}"
            if cand not in existing_ids:
                existing_ids.add(cand)
                return cand
            i += 1

    # 决定 spawn 哪些子节点类型
    if spawn_model_only:
        child_types = ("model3d",)
    else:
        child_types = ()
        if include_camera:
            child_types = child_types + ("camera3d",)
        if include_light:
            child_types = child_types + ("light3d",)
        if include_geo:
            child_types = child_types + ("geo3d",)
        if include_model:
            child_types = child_types + ("model3d",)

    child_nodes = []
    child_conns = []
    for child_type in child_types:
        param_names = _SCENE3D_PARAM_SPLIT[child_type]
        # Model3D 仅当有模型路径且启用时 spawn（避免空模型节点）
        if child_type == "model3d":
            if not (scene3d_params.get("use_imported_model")
                    and scene3d_params.get("model_path")):
                continue
        child_id = _new_id(f"{scene3d_id}_{child_type}")
        # 子节点参数：从 scene3d 抽取（缺省由 from_dict 填默认）
        child_params = {k: copy.deepcopy(scene3d_params[k])
                        for k in param_names if k in scene3d_params}
        ox, oy = _SCENE3D_CHILD_OFFSET[child_type]
        child_nodes.append({
            "id": child_id,
            "type": child_type,
            "position": [pos_x + ox, pos_y + oy],
            "parameters": child_params,
            "disabled": False,
        })
        child_conns.append({
            "src_node_id": child_id,
            "src_port_name": "display",
            "dst_node_id": scene3d_id,
            "dst_port_name": _SCENE3D_CHILD_PORT[child_type],
        })
    return child_nodes, child_conns


# ── Migration chain ──

# 阶段七 v6→v7：Scene3D 参数 → 子节点映射表。
# 每个 tuple: (子节点 type, 该子节点的目标参数名集合)。
# Scene3D 中这些参数会被迁移到对应子节点并删除，Scene3D 只保留画布/背景/gizmo。
_SCENE3D_PARAM_SPLIT = {
    "camera3d": frozenset({
        "cam_fov", "cam_dist", "cam_orbit_yaw", "cam_orbit_pitch",
        "cam_target_x", "cam_target_y", "cam_target_z",
    }),
    "light3d": frozenset({
        "light_dir_x", "light_dir_y", "light_dir_z", "light_intensity",
    }).union({f"light{i}_{s}" for i in range(4) for s in
              ("dir_x", "dir_y", "dir_z", "color_r", "color_g", "color_b", "intensity")}).union({
        "hdri_path", "use_ibl", "env_intensity",
    }),
    "geo3d": frozenset({
        "primitive", "mesh_scale",
        "instance_enabled", "grid_x", "grid_z", "spacing",
        "object_pos_x", "object_pos_y", "object_pos_z",
        "object_rot_x", "object_rot_y", "object_rot_z",
        "metallic", "roughness",
        "object_color_r", "object_color_g", "object_color_b",
    }),
    "model3d": frozenset({
        "model_path", "use_imported_model",
        "anim_enabled", "anim_time", "anim_speed",
    }),
}

# Scene3D 保留的参数（v9：纯渲染器，只留画幅/输出元数据）。
# v9 把场景环境参数（cam_*/bg_*/light_*/hdri_preset/use_ibl/show_grid/show_axes/
# output_*/msaa/active_camera_id）移到全局 viewer_state["default_3d_scene"]。
# legacy 几何/材质参数（primitive/object_*/metallic/roughness/object_color_*）
# 不再保留在 Scene3D。新工程必须使用 Geo3D/Model3D 提供几何。
_SCENE3D_KEEP = frozenset({
    "width", "height", "format_name",
})

_SCENE3D_LEGACY_GEOMETRY_PARAMS = frozenset({
    "primitive", "mesh_scale",
    "object_pos_x", "object_pos_y", "object_pos_z",
    "object_rot_x", "object_rot_y", "object_rot_z",
    "metallic", "roughness",
    "object_color_r", "object_color_g", "object_color_b",
})

# v9：Scene3D 移到全局「3D Scene Settings」的场景环境参数键集合。
# migrate_v8_to_v9 把这些键从每个 Scene3D 节点参数迁移到
# viewer_state["default_3d_scene"]（取首个 Scene3D 的值作全局默认）。
_SCENE3D_GLOBAL_PARAMS = frozenset({
    "cam_fov", "cam_dist", "cam_orbit_yaw", "cam_orbit_pitch",
    "cam_target_x", "cam_target_y", "cam_target_z",
    "active_camera_id",
    "bg_opaque", "bg_r", "bg_g", "bg_b",
    "show_grid", "show_axes",
    "grid_cell_size", "grid_opacity", "grid_line_width",
    "grid_minor_color_r", "grid_minor_color_g", "grid_minor_color_b",
    "grid_major_color_r", "grid_major_color_g", "grid_major_color_b",
    "output_show_grid", "output_bg_opaque",
    "msaa",
    "hdri_preset", "use_ibl", "env_intensity",
    "light_dir_x", "light_dir_y", "light_dir_z", "light_intensity",
    "light_color_r", "light_color_g", "light_color_b",
})


def migrate_v6_to_v7(graph_data: dict) -> dict:
    """Migrate v6 → v7: Scene3D 节点拆分（Nuke 路线）。

    阶段七把原本揉在 Scene3D 里的相机/灯光/几何/模型参数拆成 4 个独立子节点：
      - Camera3D / Light3D / Geo3D / Model3D
    每个子节点自动 spawn 并连接到 Scene3D 的对应 scene 输入端口
    (camera_in / lights_in / geometry_in)。

    迁移逻辑（纯 dict 变换，不实例化节点）：
      1. 找每个 scene3d 节点
      2. 从其参数里抽取 cam_*/light_*/object_*/model_*/anim_* 等
      3. spawn 4 个子节点（位置错开布局），把抽取的参数填入
      4. 建立 4 条 scene 连接（子.display → scene3d.{in}_port）
      5. 从 scene3d 参数里删掉已迁移的键（只保留画布/背景/gizmo）

    Model3D 仅当原 scene3d 有 model_path 且 use_imported_model=True 时 spawn。
    """
    graph_data = copy.deepcopy(graph_data)
    existing_ids = {n.get("id") for n in _iter_nodes(graph_data)}

    new_nodes = []
    new_conns = []

    for node_data in _iter_nodes(graph_data):
        if not isinstance(node_data, dict) or node_data.get("type") != "scene3d":
            continue
        scene3d_id = node_data.get("id", "")
        params = node_data.get("parameters", {})
        position = node_data.get("position", [0.0, 0.0])

        # P2：复用共享工厂 spawn 子节点 + 连接（与实时创建路径一致）
        # Model3D 仅当原 scene3d 有 model_path 且 use_imported_model=True 时 spawn
        # （工厂内部判定）。
        has_model = bool(params.get("use_imported_model") and params.get("model_path"))
        child_nodes, child_conns = spawn_3d_scene_children(
            scene3d_id=scene3d_id,
            scene3d_position=position,
            scene3d_params=params,
            existing_ids=existing_ids,
            include_model=has_model,
        )
        new_nodes.extend(child_nodes)
        new_conns.extend(child_conns)

        # 从 scene3d 删除已迁移到子节点的参数（AUD-4701）。
        # 按版本核对：v6→v7 废弃的是 _SCENE3D_PARAM_SPLIT 并集——这些键
        # 的 schema 所有权在本版本移给了 Camera3D/Light3D/Geo3D/Model3D
        # 子节点。场景环境键（bg_*/show_grid/grid_*/output_*/msaa/
        # active_camera_id/light_color_* 等）不是本版本废弃的参数——留在
        # Scene3D 上，由 migrate_v8_to_v9 提升到
        # viewer_state["default_3d_scene"]。此前借用 v9 的 _SCENE3D_KEEP
        # （只留 width/height/format_name）导致 v6 场景环境参数被静默丢弃、
        # v8→v9 的全局设置迁移扑空。
        migrated_keys: set[str] = set()
        for names in _SCENE3D_PARAM_SPLIT.values():
            migrated_keys |= names
        for k in migrated_keys:
            params.pop(k, None)

    if new_nodes:
        nodes = graph_data.get("nodes")
        if not isinstance(nodes, list):
            nodes = []
            graph_data["nodes"] = nodes
        nodes.extend(new_nodes)
    if new_conns:
        conns = graph_data.get("connections")
        if not isinstance(conns, list):
            conns = []
            graph_data["connections"] = conns
        conns.extend(new_conns)

    return graph_data


def migrate_v7_to_v8(graph_data: dict) -> dict:
    """Migrate v7 → v8: Scene3D 三端口合并为单一 `in` 端口。

    v7 的 Scene3D 有三个 scene 输入端口：camera_in / lights_in / geometry_in
    （+ 旧版 model_in）。它们 data_type 都是 scene，无法靠类型区分源类型，
    Scene3D 渲染时已按 payload 类型（CameraPayload/LightPayload/...）分派，
    不依赖端口名。合并成单个 `in` 端口简化连接语义。

    迁移逻辑（纯 dict 变换，不实例化节点）：
      遍历 connections，凡 dst 是 scene3d 且 dst_port_name ∈
      {camera_in, lights_in, geometry_in, model_in} → 改写为 "in"。
    """
    graph_data = copy.deepcopy(graph_data)
    _OLD_PORTS = {"camera_in", "lights_in", "geometry_in", "model_in"}
    # scene3d 节点 id 集合（只对这些节点的连接做端口合并）
    scene3d_ids = {
        n.get("id") for n in _iter_nodes(graph_data)
        if n.get("type") == "scene3d"
    }
    for conn in _iter_connections(graph_data):
        if not isinstance(conn, dict):
            continue
        if (conn.get("dst_node_id") in scene3d_ids
                and conn.get("dst_port_name") in _OLD_PORTS):
            conn["dst_port_name"] = "in"
    return graph_data


def migrate_v8_to_v9(graph_data: dict) -> dict:
    """Migrate v8 → v9: Scene3D 场景环境参数 → 全局「3D Scene Settings」。

    Scene3D 变成纯渲染器：场景环境参数（cam_*/bg_*/light_*/hdri_preset/
    use_ibl/show_grid/show_axes/output_*/msaa/active_camera_id）从每个
    Scene3D 节点移到图级 viewer_state["default_3d_scene"] 字典（即 Properties
    面板「3D Scene Settings」编辑的全局设置）。所有 Scene3D 共享同一份。

    迁移逻辑（纯 dict 变换，不实例化节点）：
      1. 收集所有 scene3d 节点。
      2. 取**第一个** scene3d 的场景环境参数作为全局默认（多 scene3d 时取
         首个；若后续 scene3d 的值与首个不同，记 warning 后丢弃——共享全局
         设置是 v9 的设计决策）。
      3. 写入 graph_data["viewer_state"]["default_3d_scene"]（与已有值合并，
         已有值优先——尊重用户在 v9 前手动编辑的全局设置）。
      4. 从所有 scene3d 节点的 parameters 中删除场景环境 + legacy 几何键。
      5. Scene3D 只保留 width/height/format_name。

    无 scene3d 节点时什么都不做（纯 2D 工程）。
    """
    import logging
    graph_data = copy.deepcopy(graph_data)
    logger = logging.getLogger(__name__)

    scene3d_nodes = [
        n for n in _iter_nodes(graph_data)
        if n.get("type") == "scene3d"
    ]
    if not scene3d_nodes:
        return graph_data  # 纯 2D 工程，无 Scene3D → 跳过

    # 1. 取首个 Scene3D 的场景环境参数作为全局默认。
    first_params = scene3d_nodes[0].get("parameters", {}) or {}
    global_defaults = {}
    for key in _SCENE3D_GLOBAL_PARAMS:
        if key in first_params:
            global_defaults[key] = copy.deepcopy(first_params[key])

    # 2. 多 Scene3D 时检查差异（仅 warning，不阻塞迁移）。
    if len(scene3d_nodes) > 1:
        for nd in scene3d_nodes[1:]:
            np = nd.get("parameters", {}) or {}
            for key in _SCENE3D_GLOBAL_PARAMS:
                if key in np and key in global_defaults:
                    if np[key] != global_defaults[key]:
                        logger.warning(
                            "v9 迁移：Scene3D '%s' 的 %s=%r 与全局默认 %r 不同，"
                            "已丢弃（v9 所有 Scene3D 共享全局设置）。",
                            nd.get("id", "?"), key, np[key],
                            global_defaults[key])
                        break  # 每个节点只报首个差异，避免日志爆炸

    # 3. 写入 viewer_state["default_3d_scene"]（已有值优先）。
    viewer_state = graph_data.setdefault("viewer_state", {})
    if not isinstance(viewer_state, dict):
        viewer_state = {}
        graph_data["viewer_state"] = viewer_state
    existing = viewer_state.get("default_3d_scene")
    if isinstance(existing, dict) and existing:
        # 已有全局设置（用户 v9 前手动编辑过）→ 已有值优先，仅补缺失键
        merged = dict(global_defaults)
        merged.update(existing)
        viewer_state["default_3d_scene"] = merged
    else:
        viewer_state["default_3d_scene"] = global_defaults

    # 4. 从所有 Scene3D 节点删除场景环境 + legacy 几何参数键。
    for nd in scene3d_nodes:
        params = nd.get("parameters")
        if isinstance(params, dict):
            for key in _SCENE3D_GLOBAL_PARAMS | _SCENE3D_LEGACY_GEOMETRY_PARAMS:
                params.pop(key, None)

    return graph_data


def migrate_v9_to_v10(graph_data: dict) -> dict:
    """Migrate v9 → v10: introduce optional AE-style layer stacks.

    This is an additive, no-op migration: pre-v10 graphs have no
    ``layer_stacks`` / ``layers`` keys, and the new schema treats their
    absence as "no layer stacks yet" (the controller projects one on demand
    per plan §1.2.7).  We therefore only normalise the shape: ensure the two
    keys are absent rather than ``null``, and drop malformed entries so a
    corrupt file cannot wedge :meth:`NodeGraph.from_dict`.
    """
    graph_data = copy.deepcopy(graph_data)

    for key in ("layer_stacks", "layers"):
        val = graph_data.get(key)
        if val is None:
            graph_data.pop(key, None)
        elif not isinstance(val, dict):
            # Tolerate corruption: a non-dict value is treated as empty.
            graph_data.pop(key, None)

    return graph_data


def migrate_v10_to_v11(graph_data: dict) -> dict:
    """Migrate v10 → v11: per-node optional ``user_label`` (B4 canvas rename).

    Purely additive: pre-v11 graphs have no ``user_label`` key on nodes and
    the loaded default is ``None`` (display falls back to the auto
    number).  We only normalise the shape — drop non-string / blank values
    so a corrupt file cannot produce a bogus display title.
    """
    graph_data = copy.deepcopy(graph_data)

    for node in _iter_nodes(graph_data):
        ul = node.get("user_label")
        if not (isinstance(ul, str) and ul.strip()):
            node.pop("user_label", None)

    return graph_data


# ── Migration chain ──

_MIGRATIONS = {
    1: migrate_v1_to_v2,
    2: migrate_v2_to_v3,
    3: migrate_v3_to_v4,
    4: migrate_v4_to_v5,
    5: migrate_v5_to_v6,
    6: migrate_v6_to_v7,
    7: migrate_v7_to_v8,
    8: migrate_v8_to_v9,
    9: migrate_v9_to_v10,
    10: migrate_v10_to_v11,
}


def migrate_graph(graph_data: dict) -> dict:
    """Run all needed migrations on a composite graph dict.

    Detects the stored schema version (defaults to 1 if absent) and
    applies migrations sequentially until the data matches
    COMPOSITE_SCHEMA_VERSION.

    Returns the migrated dict (does NOT mutate the input).
    """
    if not graph_data:
        return graph_data

    version = graph_data.get("composite_schema_version", 1)

    # AUD-4702: validate the version field.  A corrupt / non-int marker
    # (e.g. a string) would crash the comparison below or — worse — fall
    # through the chain untouched and still get stamped as current.
    # Per the module contract, an unknown version is treated as v1.
    if isinstance(version, bool) or not isinstance(version, int):
        version = 1
    elif version < 1:
        version = 1

    if version >= COMPOSITE_SCHEMA_VERSION:
        return graph_data  # Already up to date

    data = copy.deepcopy(graph_data)

    migrated = True
    while version < COMPOSITE_SCHEMA_VERSION:
        migration_fn = _MIGRATIONS.get(version)
        if migration_fn is None:
            # No migration registered for this version — the data cannot
            # be brought up to date, so it must NOT be stamped as current
            # (AUD-4702: "未迁移即盖章").
            migrated = False
            break
        data = migration_fn(data)
        version += 1

    if migrated:
        data["composite_schema_version"] = COMPOSITE_SCHEMA_VERSION
    return data
