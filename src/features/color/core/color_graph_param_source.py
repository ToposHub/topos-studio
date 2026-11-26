"""图 Corrector 节点 → 可执行 Layer DTO 的生产参数源（S04/B16.5）。

图内 Corrector 的参数有两处存储（均为权威图的一部分）：

- ``legacy_payload``：迁移/建图时的完整 Layer DTO 快照（grading_params、
  qualifier、曲线控制点、插件、窗口等）；
- ``node.params``：图命令写入的**结构化域**（``grade``/``qualifier``/
  ``opacity``/``blend_mode``/``key_settings``…，B06.2 旧面板手势经
  ``grade_patch`` 写入）。

生产执行取参必须**合并**两者：以 legacy_payload 为基底，node.params 的
结构化域覆盖同名键——只读 legacy_payload 会丢图命令编辑（"已应用"假
状态），只读 node.params 会丢曲线/qualifier/插件等未图域化参数。

曲线遵循 save/load 语义：控制点（``curves_data``）持久化，``curves_lut``
是缓存——从 ``curves_data`` 重建（与 B09 测试 ``_layer_provider`` 同式）。

非 Corrector 节点（mixer/splitter/...）不经过本 provider（执行器只对
corrector/shared_corrector pass 取参）。
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Callable, Iterator, Mapping

import numpy as np
from src.features.color.core.color_node_labels import display_node_label

from src.shared.project.color_graph import (
    ColorGraphData,
    ColorNodeData,
    NodeType,
    unfreeze_params,
)

#: node.params 结构化域 → legacy DTO 键的 dict 域覆盖表
_DICT_PARAM_OVERLAYS = (
    ("grade", "grading_params"),
    ("qualifier", "qualifier_params"),
    ("soft_clip", "soft_clip_params"),
    ("mixer", "mixer_params"),
    ("effects", "effects_params"),
)


def node_layer_payload(node: Any, *,
                       include_lut_cache: bool = True) -> dict[str, Any] | None:
    """单节点 → 合并后的 Layer DTO（非 Corrector 返回 None）。

    图内新建的 Corrector（``op_insert_corrector``/``op_add_layer_branch``
    等）没有 legacy_payload——以生产默认 Layer DTO（``Layer(label)`` 的
    ``to_dict``）为基底，再覆盖 node.params 结构化域。CPU oracle 与 GL
    两侧拿到同一份完整 DTO，避免"缺省值各自解释"。

    :param include_lut_cache: True（执行语义）时从 ``curves_data`` 重建
        ``curves_lut`` 嵌套 list；False（持久化边界语义，
        ``project_legacy_layers``）保留载荷原值——``Layer.from_dict``
        会按控制点重建，载荷守恒按 JSON 形态比较。
    """
    if node.node_type not in (NodeType.CORRECTOR.value,
                              NodeType.SHARED_CORRECTOR.value):
        return None
    if node.legacy_payload:
        base = unfreeze_params(dict(node.legacy_payload))
        base.setdefault("id", node.node_id)
    else:
        from src.features.color.core.layer import Layer
        default_layer = Layer(display_node_label(node.label, node.node_id))
        default_layer.id = node.node_id
        base = default_layer.to_dict(None)
    payload = dict(base)
    if include_lut_cache:
        # A new corrector can have a sparse payload after its first tool edit.
        # Route it through the same production evaluator as a complete Layer.
        payload.setdefault("grading_params", {})
    params = node.params if isinstance(node.params, Mapping) else {}
    identities = (node.node_id, str(payload.get("id") or ""),
                  str(params.get("source_layer_id") or ""))
    payload["name"] = display_node_label(
        node.label if node.label else payload.get("name", ""), *identities)
    # 结构化 dict 域：基底 + 图命令域覆盖（浅合并；图域键是完整语义键）
    for graph_key, dto_key in _DICT_PARAM_OVERLAYS:
        overlay = params.get(graph_key)
        if isinstance(overlay, Mapping) and overlay:
            merged = dict(payload.get(dto_key) or {})
            merged.update(overlay)
            if graph_key == "grade" and "chromogen" in overlay:
                merged["chromogen"] = unfreeze_params(overlay["chromogen"])
            payload[dto_key] = merged
    # 标量域：enabled/opacity/blend_mode（project_legacy_layers 同式）
    payload["visible"] = node.enabled
    if "opacity" in params:
        payload["opacity"] = params["opacity"]
    if "blend_mode" in params:
        payload["blend_mode"] = params["blend_mode"]
    # R05：节点 LUT 资源域 → legacy Layer 消费投影（GL composition 与
    # 持久化同源；lut_ref 支持 topos-res:// 与旧绝对路径）。设置即
    # 覆盖 legacy_payload 的旧 LUT；移除（无 lut_ref 键）不复活旧值。
    from src.features.color.core.color_node_lut_spec import node_lut_projection
    lut_ref, lut_strength = node_lut_projection(params.get("resources"))
    if "resources" in params or lut_ref is not None:
        payload.pop("active_lut_path", None)
        payload.pop("lut_intensity", None)
        if lut_ref is not None:
            payload["active_lut_path"] = lut_ref
            payload["lut_intensity"] = lut_strength
    # N10：节点关键帧域合并（params 逐 param 胜出 legacy 基底；命令与
    # 播放评估/导出读取同域——evaluate_grade_keyframes 消费点不变）。
    # KF-05：显式墓碑（空轨道 + tombstone）同样胜出——图域删除最后
    # 一键后压制 legacy 同名轨道复活（删除语义，不随保存/重开翻转）。
    node_keyframes = params.get("grade_keyframes")
    if isinstance(node_keyframes, Mapping) and node_keyframes:
        merged_kf = dict(payload.get("grade_keyframes") or {})
        for track_name, track in node_keyframes.items():
            if not isinstance(track, Mapping):
                continue
            if track.get("keyframes"):
                merged_kf[str(track_name)] = unfreeze_params(track)
            elif track.get("tombstone"):
                merged_kf[str(track_name)] = {"keyframes": []}
        payload["grade_keyframes"] = merged_kf
    # 曲线 LUT 重建（save/load 语义：控制点持久化，LUT 是缓存）。
    # 输出为原生嵌套 list（与 Layer.to_dict 持久化形态一致；执行器/
    # oracle 侧 np.asarray 同样可用）
    if include_lut_cache and payload.get("curves_data") and not _lut_current(payload):
        from src.features.color.core.curves_soft_clip import normalize_curves_edit
        from src.features.color.core.layer import build_curves_lut_from_data
        try:
            edit = normalize_curves_edit(payload.get("curves_edit"))
            lut = build_curves_lut_from_data(
                dict(payload["curves_data"]), edit["amounts"],
                calibration_version=edit.get("calibration_version", 0),
            )
            payload["curves_lut"] = np.asarray(lut).tolist()
        except Exception:  # noqa: BLE001 —— 坏曲线按 identity（缺省）处理
            payload.pop("curves_lut", None)
            payload.pop("curves_data", None)
    hsl_data = payload.get("hsl_curve_data")
    if include_lut_cache and isinstance(hsl_data, Mapping) and hsl_data:
        from src.features.color.core.curves_calibration import curves_calibration_version
        from src.features.color.core.hsl_curve_calibration import hsl_lut_shape
        from src.features.color.core.layer import build_hsl_curve_lut_from_data

        version = curves_calibration_version(payload.get("curves_edit"))
        expected_shape = hsl_lut_shape(version)
        try:
            cache_shape = np.shape(payload.get("hsl_curve_lut"))
        except (TypeError, ValueError):
            cache_shape = ()
        if cache_shape != expected_shape:
            payload["hsl_curve_lut"] = build_hsl_curve_lut_from_data(
                dict(hsl_data), calibration_version=version,
            ).tolist()
    if include_lut_cache:
        from src.features.color.core.curves_soft_clip import (
            SOFT_CLIP_CALIBRATION_VERSION, normalize_curves_soft_clip,
            soft_clip_calibration_version,
        )

        soft_clip = payload.get("curves_soft_clip")
        if soft_clip_calibration_version(soft_clip) == SOFT_CLIP_CALIBRATION_VERSION:
            payload["curves_soft_clip"] = normalize_curves_soft_clip(soft_clip)
        warper = payload.get("warper_state")
        if isinstance(warper, Mapping):
            from src.features.color.core.warper_state import (
                WarperStateV2, derives_warper_projection, renderable_projection,
            )

            if derives_warper_projection(warper):
                # Raw offsets are a derived cache. A matching render-space
                # number does not establish that the cached grid is current.
                offsets, space = renderable_projection(WarperStateV2.from_dict(warper))
                payload["warper_params"] = offsets.tolist() if offsets is not None else None
                payload["warper_space"] = space
    return payload


def _lut_current(payload: Mapping[str, Any]) -> bool:
    """Reuse a packed LUT already generated from the current snapshot."""
    from src.features.color.core.curves_calibration import (
        curves_calibration_enabled, is_calibrated_curves_lut,
    )
    try:
        arr = np.asarray(payload.get("curves_lut"))
    except (TypeError, ValueError):
        return False
    if curves_calibration_enabled(payload.get("curves_edit")):
        return is_calibrated_curves_lut(arr)
    return arr.shape in ((256, 3), (256, 1, 3))


def _iter_corrector_nodes(graph: ColorGraphData) -> Iterator[ColorNodeData]:
    """Resolve shared defaults before materializing the execution payload."""
    definitions = {definition.definition_id: definition.params
                   for definition in graph.shared_definitions}
    for node in graph.nodes:
        if node.compound_children is not None:
            yield from _iter_corrector_nodes(node.compound_children)
        elif node.node_type == NodeType.SHARED_CORRECTOR.value:
            definition = definitions.get(node.shared_definition_id)
            if definition is not None:
                # Keep the same domain precedence as the graph compiler.
                params = dict(definition)
                params.update({key: value for key, value in node.params.items()
                               if key != "shared_definition_id"})
                node = replace(node, params=params)
            yield node
        elif node.node_type == NodeType.CORRECTOR.value:
            yield node


def graph_node_layer_provider(
    graph: ColorGraphData,
    frame: int | None = None,
    *,
    preview_grade: Mapping[str, Mapping[str, Any]] | None = None,
    rec709_calibration_encoded: bool = False,
    base_grade_signal: Mapping[str, Any] | None = None,
) -> Callable[[str], Mapping[str, Any] | None]:
    """``layer_param_provider`` 工厂：node_id → 合并后的 Layer DTO。

    与 ``ColorGraphExecutor.execute_plan(layer_param_provider=...)`` 直接
    对接；快照图（``ColorGraphSnapshot.graph``）与活动图同构可用。
    compound children 一并编入（plan 按 inline 成员 id 取参）。

    N10：``frame`` 非 None 时对带 ``params.grade_keyframes`` 轨道的节点
    按帧求值（``evaluate_grade_keyframes``——与 legacy 预览/导出同一求
    值函数、同一源帧域）；轨道转换按 graph_revision 缓存避免每帧重建。
    KF-06：未参与求值的节点（恒定/无轨道）仍返回完整合并 base 参数
    （此前返回 None，执行器回退 plan 内嵌 params 会丢 legacy 域）。
    """
    from src.features.color.core.base_grade_runtime import BASE_GRADE_SIGNAL_KEY
    from src.features.color.core.color_node_color_space import node_color_is_identity
    from src.features.color.core.x_grade_domain import X_GRADE_WORKING_KEY, bypass_x_grade

    payloads: dict[str, dict[str, Any] | None] = {}
    x_grade_supported = graph.working_color_context.get(X_GRADE_WORKING_KEY) is not False
    signal = base_grade_signal or graph.working_color_context.get(BASE_GRADE_SIGNAL_KEY)
    for node in _iter_corrector_nodes(graph):
        payload = node_layer_payload(node)
        if payload is not None and not x_grade_supported:
            bypass_x_grade(payload)
        if payload is not None:
            color = node.params.get("color")
            if isinstance(color, Mapping) and not node_color_is_identity(
                color.get("color_space"), color.get("gamma"),
            ):
                payload["_node_input_color"] = color.get("color_space"), color.get("gamma")
            else:
                if rec709_calibration_encoded:
                    payload["_rec709_calibration_encoded"] = True
                if signal:
                    payload[BASE_GRADE_SIGNAL_KEY] = signal
        payloads[node.node_id] = payload
    evaluated: dict[str, dict[str, Any] | None] | None = None
    if frame is not None:
        from src.features.color.core.grade_keyframes import (
            GradeKeyframe, GradeKeyframeTrack, evaluate_grade_keyframes,
            evaluate_mask_tracks_payload, evaluate_qualifier_params_tracks,
        )
        evaluated = dict(payloads)
        for node_id, payload in payloads.items():
            if not payload:
                continue
            kf = payload.get("grade_keyframes")
            if not kf:
                continue
            tracks: dict[str, GradeKeyframeTrack] = {}
            for track_name, track_data in kf.items():
                if not isinstance(track_data, Mapping):
                    continue
                points = []
                for entry in (track_data.get("keyframes") or []):
                    if not isinstance(entry, Mapping):
                        continue
                    try:
                        # R2：经 from_dict（KF-01 decode 单一入口）重建——
                        # 显式键型/动态属性随载荷进入求值域，不再丢失。
                        points.append(GradeKeyframe.from_dict(
                            dict(entry)))
                    except (TypeError, ValueError):
                        continue
                if points:
                    tracks[str(track_name)] = GradeKeyframeTrack(
                        str(track_name), points)
            if not tracks:
                continue
            patched = evaluate_grade_keyframes(
                tracks, int(frame), dict(payload.get("grading_params") or {}))
            merged = dict(payload)
            merged["grading_params"] = patched
            # N10-B：qualifier.* 轨道同帧求值 patch qualifier_params
            # （evaluate_grade_keyframes 白名单只吃 grade 域，互不干扰）。
            if any(str(name).startswith("qualifier.") for name in tracks):
                # 与 layer 级语义一致：字段恒求值（图 payload 基底恒有
                # 默认限定器字段；是否消费由 enabled 决定）
                merged["qualifier_params"] = evaluate_qualifier_params_tracks(
                    tracks, int(frame),
                    dict(payload.get("qualifier_params") or {}))
            # KF-06/窗口写入：mask.<shape_id>.<field> 轨道逐帧补写形状
            # （与导出侧 evaluate_mask_tracks_payload 同函数同基底——
            # 每帧从 base shapes 重求值不累积；matte 生成按补写后的
            # shapes 取哈希，逐帧自然不命中旧缓存）
            if any(str(name).startswith("mask.") for name in tracks):
                patched_shapes = evaluate_mask_tracks_payload(
                    tracks, list(payload.get("mask_shapes") or []),
                    int(frame))
                if patched_shapes is not None:
                    merged["mask_shapes"] = patched_shapes
            evaluated[node_id] = merged
            from src.features.color.core.effect_keyframes import evaluate_effect_tracks
            plugins = evaluate_effect_tracks(tracks, payload.get("plugins") or (), int(frame))
            if plugins is not None:
                merged["plugins"] = plugins

    active = evaluated if evaluated is not None else payloads
    for node_id, patch in (preview_grade or {}).items():
        payload = active.get(node_id)
        if payload is not None:
            grading = {**(payload.get("grading_params") or {}), **patch}
            active[node_id] = {**payload, "grading_params": grading}

    return active.get


def mixer_composite_params(node_params: Mapping[str, Any]) -> tuple[str, float]:
    """Layer Mixer 节点参数 → (blend_mode, opacity)（GL/CPU 同一提取）。

    兼容两处存储：顶层键（B10 构造域/测试）与图命令域
    ``params["mixer"]["blend_mode"/"opacity"]``（``set_node_option`` 的
    领域 schema 键）。缺省 Normal/1.0。
    """
    params = node_params if isinstance(node_params, Mapping) else {}
    nested = params.get("mixer")
    if not isinstance(nested, Mapping):
        nested = {}
    blend = (params.get("blend_mode")
             if params.get("blend_mode") is not None
             else nested.get("blend_mode"))
    opacity = (params.get("opacity")
               if params.get("opacity") is not None
               else nested.get("opacity"))
    try:
        opacity_value = float(opacity)
    except (TypeError, ValueError):
        opacity_value = 1.0
    return (str(blend or "Normal"), opacity_value)
