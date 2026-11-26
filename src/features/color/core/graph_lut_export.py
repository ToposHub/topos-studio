"""N13: 图 → LUT 导出（pointwise 可表达性预检 + 串行链 CPU 烘焙）。

契约（计划 N13）：

- **预检 fail-closed**：只有「串行 Corrector 链（含 pointwise 域 + LUT）」
  的图可导出为 .cube。分支/混叠/Compound 展开、插件（Glow/NR/AI）、
  窗口/key、邻域效果、非单位 opacity/mask 等一律**显式拒绝并解释**
  （码列表），绝不产出「成功但少效果」的 cube。
- **烘焙**：N³ 网格逐点经串行链 CPU 求值——每 Corrector 依 R05
  `CORRECTOR_PROCESS_ORDER_V1` 中的 pointwise 域（grading → curves；
  LUT 按其顺序位）——输出标准 .cube 文本（DOMAIN [0,1]）。
- 与旧 `lut_exporter`（legacy Layer 域）并存：本模块是图权威侧入口，
  供导出调用点在图模式下使用。
"""
from __future__ import annotations

from dataclasses import replace
from typing import Any, Mapping

import numpy as np

from src.features.color.core.color_graph_param_source import graph_node_layer_provider
from src.shared.project.color_graph import ColorGraphData, NodeType


def graph_pointwise_export_errors(graph: ColorGraphData) -> list[str]:
    """导出预检：可表达性错误列表（空 = 可导出）。

    结构：仅 source → Corrector* → output 的串行链（无分支/混叠/
    compound/多入）；参数域：每 Corrector 只允许 pointwise 基础域 +
    LUT（复用 CPU oracle 的域错误表，另放行 LUT）。
    """
    errors: list[str] = []
    from src.features.color.core.x_grade_domain import (
        X_GRADE_WORKING_KEY, X_GRADE_WORKING_MESSAGE, active_x_grade_node,
    )

    if graph.working_color_context.get(X_GRADE_WORKING_KEY) is False and active_x_grade_node(graph):
        errors.append(X_GRADE_WORKING_MESSAGE)
    chain: list[Any] = []
    current = graph
    # 结构走查：从 source 沿唯一 image 出边下行
    node_by_id = {n.node_id: n for n in current.nodes}
    node = node_by_id.get(graph.source_node_id)
    if node is None or node.node_type != NodeType.SOURCE.value:
        return ["graph_missing_source"]
    while True:
        outs = [e for e in current.edges
                if e.src_node == node.node_id and e.dst_node != node.node_id]
        image_outs = [e for e in outs if e.src_port in ("image", "image_out")]
        if len(outs) > 1 or len(image_outs) > 1:
            errors.append(f"non_serial_branch_at:{node.node_id}")
            break
        if not image_outs:
            break
        nxt = node_by_id.get(image_outs[0].dst_node)
        if nxt is None:
            break
        if nxt.node_type == NodeType.OUTPUT.value:
            # 到达输出：图内不允许 output 之后还有别的消费者
            break
        if nxt.node_type not in (NodeType.CORRECTOR.value,
                                 NodeType.SHARED_CORRECTOR.value):
            errors.append(f"non_corrector_node:{nxt.node_id}"
                          f"({nxt.node_type})")
            break
        chain.append(nxt)
        node = nxt
    # 拒绝：compound / 混叠节点存在（即使不在主链上——旁支也是效果）
    for candidate in graph.nodes:
        if candidate.node_type == NodeType.COMPOUND.value:
            errors.append(f"compound_not_exportable:{candidate.node_id}")
    if not chain:
        errors.append("no_corrector_in_serial_chain")
    # 参数域（每 Corrector）
    from src.features.color.core.color_graph_cpu import (
        production_layer_domain_errors,
    )
    layer_for = graph_node_layer_provider(graph)
    for candidate in chain:
        payload = layer_for(candidate.node_id)
        if payload is None or payload.get("visible") is False:
            continue
        # N05：节点 Color Space/Gamma 变换未在 bake 数学内（v1）——
        # 显式拒绝，不产出少变换的 cube
        color_domain = (candidate.params.get("color")
                        if isinstance(candidate.params, Mapping) else None)
        if isinstance(color_domain, Mapping) and (
                str(color_domain.get("color_space", "Use Timeline"))
                != "Use Timeline"
                or str(color_domain.get("gamma", "Use Timeline"))
                != "Use Timeline"):
            errors.append(f"{candidate.node_id}: color 域非 Timeline"
                          "（Color Space/Gamma 变换不在 pointwise bake）")
        payload = dict(payload)
        lut_ref = payload.pop("active_lut_path", None)
        payload.pop("lut_intensity", None)
        # Authored chroma and X Grade controls are pointwise as well as HS grids.
        if _pointwise_warper(payload) is None and _has_warper(payload):
            errors.append(f"{candidate.node_id}: warper 不在点态表达域")
        else:
            payload.pop("warper_params", None)
            payload.pop("warper_space", None)
            payload.pop("warper_state", None)
        # Check the pointwise backend's effect limits before rendering the grid.
        domain_errors = production_layer_domain_errors(
            payload, backend="pointwise")
        # LUT 由烘焙路径处理（点态）——其余域错误全部保留
        for domain_error in domain_errors:
            errors.append(f"{candidate.node_id}: {domain_error}")
        _ = lut_ref
    # 去重保序
    seen: set[str] = set()
    unique: list[str] = []
    for item in errors:
        if item not in seen:
            seen.add(item)
            unique.append(item)
    return unique


def _has_warper(payload: Mapping[str, Any]) -> bool:
    return (payload.get("warper_params") is not None
            or payload.get("warper_state") is not None)


def _pointwise_warper(payload: Mapping[str, Any]) -> tuple[np.ndarray, int] | None:
    """可点态表达的 warper → (offsets ndarray, space int);否则 None。"""
    from src.features.color.core.warper_color import (
        RESOLVE_HS709_SPACES, WARPER_CHROMA709_SPACE, WARPER_PACKED_SPACES, WARPER_PREPARED_SPACES,
    )
    from src.features.color.core.warper_state import (
        HS_GRID_NAME,
        WarperStateError,
        WarperStateV2,
        renderable_projection,
    )
    raw_state = payload.get("warper_state")
    has_x_grade = isinstance(raw_state, Mapping) and raw_state.get("x_grade") is not None
    if payload.get("warper_params") is not None and not has_x_grade:
        try:
            offsets = np.asarray(payload["warper_params"], dtype=np.float32)
            space = int(payload.get("warper_space", 0) or 0)
        except (TypeError, ValueError):
            return None
        if space in (0, 1, 2, *WARPER_PREPARED_SPACES):
            components = 3 if space in WARPER_PREPARED_SPACES else 2
            if (offsets.ndim == 3 and offsets.shape[2] == components
                    and offsets.shape[0] >= 2
                    and (offsets.shape[1] == 1 if space in WARPER_PACKED_SPACES else offsets.shape[1] >= 2)
                    and np.isfinite(offsets).all()):
                return offsets, space
        return None
    if isinstance(raw_state, Mapping):
        try:
            state = WarperStateV2.from_dict(raw_state)
            if "x_grade" in state._extra_fields:
                return None
            offsets, space = renderable_projection(state)
        except WarperStateError:
            return None
        if offsets is None and space in RESOLVE_HS709_SPACES:
            # Enabling 709 or creating pins alone is exportable identity.
            offsets = state.grids[HS_GRID_NAME].offsets
        if offsets is None and (space == WARPER_CHROMA709_SPACE or (
                state.x_grade is not None and not state.x_grade.strokes and replace(state, x_grade=None).is_identity())):
            return np.zeros((2, 2, 2), dtype=np.float32), 0
        return (offsets, space) if offsets is not None else None
    return None


def _apply_pointwise_corrector(color: np.ndarray, payload: Mapping[str, Any],
                               lut_cache: dict) -> np.ndarray:
    """单 Corrector 的 pointwise CPU 求值（R05 v1 顺序：grading → curves
    → … → LUT；本导出仅承载基础域 + LUT，其余已在预检拒绝）。"""
    from src.features.color.core.color_graph_cpu import (
        UnsupportedNode,
        apply_curves_cpu,
        apply_layer_grading_cpu,
    )
    from src.features.color.core.color_node_lut_spec import (
        blend_node_lut_strength,
    )
    from src.shared.color.lut_loader import (
        load_lut, resample_to_unit_domain, sample_lut_trilinear,
    )
    layer = dict(payload)
    encoded = bool(layer.get("_rec709_calibration_encoded"))
    try:
        out, _ = apply_layer_grading_cpu(color, layer, width=1, height=1)
    except UnsupportedNode as exc:
        raise ValueError(str(exc)) from exc
    curves = layer.get("curves_lut")
    if curves is not None:
        out = apply_curves_cpu(out, curves, encoded_input=encoded)
    hsl_curves = layer.get("hsl_curve_lut")
    if hsl_curves is not None:
        from src.features.color.core.advanced_grade_cpu import apply_hsl_curves_image

        out = apply_hsl_curves_image(out, hsl_curves, encoded_input=encoded)
    from src.features.color.core.curves_soft_clip import uses_calibrated_soft_clip

    soft_clip = layer.get("curves_soft_clip")
    if uses_calibrated_soft_clip(soft_clip):
        from src.features.color.core.advanced_grade_cpu import apply_curves_soft_clip_image

        out = apply_curves_soft_clip_image(out, dict(soft_clip), encoded_input=encoded)
    # CW-B09:warper 在 grading/curves 之后、LUT 之前(与 composition
    # 管线位序一致:... → mixer → WARPER → lut3d)
    warper = _pointwise_warper(layer)
    if warper is not None:
        from src.features.color.core.warper_color import (
            WARPER_PREPARED_SPACES,
            apply_warper_in_space,
            apply_warper_legacy_hsv,
        )
        offsets, space = warper
        if space in WARPER_PREPARED_SPACES:
            from src.features.color.core.warper_color import (
                apply_resolve_hs709_texture, prepare_resolve_hs709_texture,
            )

            texture = prepare_resolve_hs709_texture(offsets, space)
            out = apply_resolve_hs709_texture(out, texture, space, encoded_input=encoded).astype(
                np.float64, copy=False)
        elif space == 0:
            out = np.stack([apply_warper_legacy_hsv(pixel, offsets) for pixel in out])
        else:
            space_name = {1: "hsl", 2: "hsp"}[space]
            out = np.stack([apply_warper_in_space(pixel, offsets, space_name) for pixel in out])
    lut_ref = layer.get("active_lut_path")
    if isinstance(lut_ref, str) and lut_ref:
        loaded = lut_cache.get(lut_ref)
        if loaded is None:
            loaded = resample_to_unit_domain(load_lut(lut_ref))
            lut_cache[lut_ref] = loaded
        raw_strength = layer.get("lut_intensity")
        strength = 1.0 if raw_strength is None else float(raw_strength)
        sdr = np.clip(out, 0.0, 1.0)
        sampled = sample_lut_trilinear(loaded, sdr)
        out = blend_node_lut_strength(out, sampled, strength).astype(np.float64)
    return out


def bake_graph_lut(graph: ColorGraphData, size: int = 33,
                   *, domain: tuple[float, float] = (0.0, 1.0),
                   rec709_calibration_encoded: bool = False,
                   base_grade_signal: Mapping[str, Any] | None = None) -> str:
    """串行 pointwise 链 → 标准 .cube 文本（导出入口）。

    Raises:
        ValueError: 预检失败（携带全部错误码——空间/时序/依赖窗口图
        显式拒绝并解释）。
    """
    errors = graph_pointwise_export_errors(graph)
    if errors:
        raise ValueError("graph not exportable as pointwise LUT: "
                         + "; ".join(errors))
    dmin, dmax = float(domain[0]), float(domain[1])
    axis = np.linspace(dmin, dmax, size)
    b_ax, g_ax, r_ax = np.meshgrid(axis, axis, axis, indexing="ij")
    rgb = np.stack([r_ax, g_ax, b_ax], axis=-1).reshape(-1, 3)
    lut_cache: dict = {}
    # 串行链（预检已保证结构）
    node_by_id = {n.node_id: n for n in graph.nodes}
    layer_for = graph_node_layer_provider(
        graph, rec709_calibration_encoded=rec709_calibration_encoded, base_grade_signal=base_grade_signal)
    node = node_by_id[graph.source_node_id]
    current = rgb
    while True:
        image_outs = [e for e in graph.edges
                      if e.src_node == node.node_id
                      and e.dst_node != node.node_id
                      and e.src_port in ("image", "image_out")]
        if not image_outs:
            break
        nxt = node_by_id.get(image_outs[0].dst_node)
        if nxt is None or nxt.node_type == NodeType.OUTPUT.value:
            break
        payload = layer_for(nxt.node_id)
        if payload is not None and payload.get("visible") is not False:
            graded = _apply_pointwise_corrector(current, payload, lut_cache)
            opacity = float(payload.get("opacity", 1.0))
            current = graded if opacity == 1.0 else current + opacity * (graded - current)
        node = nxt
    table = np.clip(current, dmin, dmax).reshape(size, size, size, 3)
    # .cube 行序：for b: for g: for r —— 与 parse_cube 一致
    lines = [f'TITLE "Topos graph export"', f"LUT_3D_SIZE {size}",
             f"DOMAIN_MIN {dmin} {dmin} {dmin}",
             f"DOMAIN_MAX {dmax} {dmax} {dmax}"]
    for b in range(size):
        for g in range(size):
            for r in range(size):
                v = table[b, g, r]
                lines.append(f"{v[0]:.6f} {v[1]:.6f} {v[2]:.6f}")
    return "\n".join(lines) + "\n"
