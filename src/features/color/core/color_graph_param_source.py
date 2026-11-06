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

from typing import Any, Callable, Mapping

import numpy as np

from src.shared.project.color_graph import (
    ColorGraphData,
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
        default_layer = Layer(str(getattr(node, "label", None)
                                  or node.node_id))
        default_layer.id = node.node_id
        base = default_layer.to_dict(None)
    payload = dict(base)
    params = node.params if isinstance(node.params, Mapping) else {}
    # 结构化 dict 域：基底 + 图命令域覆盖（浅合并；图域键是完整语义键）
    for graph_key, dto_key in _DICT_PARAM_OVERLAYS:
        overlay = params.get(graph_key)
        if isinstance(overlay, Mapping) and overlay:
            merged = dict(payload.get(dto_key) or {})
            merged.update(overlay)
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
                merged_kf[str(track_name)] = {
                    "keyframes": unfreeze_params(
                        list(track["keyframes"]))}
            elif track.get("tombstone"):
                merged_kf[str(track_name)] = {"keyframes": []}
        payload["grade_keyframes"] = merged_kf
    # 曲线 LUT 重建（save/load 语义：控制点持久化，LUT 是缓存）。
    # 输出为原生嵌套 list（与 Layer.to_dict 持久化形态一致；执行器/
    # oracle 侧 np.asarray 同样可用）
    if include_lut_cache and payload.get("curves_data") and not _lut_current(payload):
        from src.features.color.core.layer import build_curves_lut_from_data
        try:
            lut = build_curves_lut_from_data(dict(payload["curves_data"]))
            payload["curves_lut"] = np.asarray(lut).tolist()
        except Exception:  # noqa: BLE001 —— 坏曲线按 identity（缺省）处理
            payload.pop("curves_lut", None)
            payload.pop("curves_data", None)
    return payload


def _lut_current(payload: Mapping[str, Any]) -> bool:
    """payload 已带有效 256×3 LUT 时不必重建。"""
    lut = payload.get("curves_lut")
    try:
        import numpy as np
        arr = np.asarray(lut)
        return arr.shape == (256, 3)
    except Exception:  # noqa: BLE001
        return False


def _iter_corrector_nodes(graph: ColorGraphData):
    """遍历全部 Corrector 节点（含 compound children，plan 展开后按
    成员 node_id 取参——B11 inline 保持成员身份）。"""
    stack = list(graph.nodes)
    while stack:
        node = stack.pop(0)
        if getattr(node, "compound_children", None) is not None:
            stack.extend(node.compound_children.nodes)
        else:
            yield node


def graph_node_layer_provider(
    graph: ColorGraphData,
    frame: int | None = None,
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
    payloads: dict[str, dict[str, Any] | None] = {
        n.node_id: node_layer_payload(n) for n in _iter_corrector_nodes(graph)
    }
    evaluated: dict[str, dict[str, Any] | None] | None = None
    if frame is not None:
        from src.features.color.core.grade_keyframes import (
            GradeKeyframe, GradeKeyframeTrack, evaluate_grade_keyframes,
            evaluate_qualifier_params_tracks,
        )
        evaluated = dict(payloads)
        for n in _iter_corrector_nodes(graph):
            payload = payloads.get(n.node_id)
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
                        points.append(GradeKeyframe(
                            int(entry.get("frame", -1)),
                            entry.get("value"),
                            str(entry.get("interpolation", "linear"))))
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
            evaluated[n.node_id] = merged

    def provide(node_id: str) -> Mapping[str, Any] | None:
        if evaluated is not None:
            return evaluated.get(node_id)
        return payloads.get(node_id)

    return provide


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
