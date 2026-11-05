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

from typing import Any, Mapping

import numpy as np

from src.features.color.core.color_graph_param_source import node_layer_payload
from src.shared.project.color_graph import ColorGraphData, NodeType


def graph_pointwise_export_errors(graph: ColorGraphData) -> list[str]:
    """导出预检：可表达性错误列表（空 = 可导出）。

    结构：仅 source → Corrector* → output 的串行链（无分支/混叠/
    compound/多入）；参数域：每 Corrector 只允许 pointwise 基础域 +
    LUT（复用 CPU oracle 的域错误表，另放行 LUT）。
    """
    errors: list[str] = []
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
    for candidate in chain:
        payload = node_layer_payload(candidate)
        if payload is None:
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
        # N14-B：LUT 导出为点态 bake 语义——HSL/soft_clip/blend 等
        # 逐像素域不在 pointwise 表达域（backend 显式声明，不随 CPU
        # 图路径接线放开）
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


def _apply_pointwise_corrector(color: np.ndarray, payload: Mapping[str, Any],
                               lut_cache: dict) -> np.ndarray:
    """单 Corrector 的 pointwise CPU 求值（R05 v1 顺序：grading → curves
    → … → LUT；本导出仅承载基础域 + LUT，其余已在预检拒绝）。"""
    from src.features.color.core.color_graph_cpu import (
        apply_curves_cpu,
        apply_grading_uniforms_cpu,
    )
    from src.features.color.core.composition_uniforms import (
        composition_uniform_values,
    )
    from src.features.color.core.color_node_lut_spec import (
        blend_node_lut_strength,
    )
    from src.shared.color.lut_loader import (
        load_lut, resample_to_unit_domain, sample_lut_trilinear,
    )
    layer = dict(payload)
    values = composition_uniform_values(layer, width=1, height=1)
    out = apply_grading_uniforms_cpu(color, values)
    curves = layer.get("curves_lut")
    if curves is not None:
        out = apply_curves_cpu(out, curves)
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
                   *, domain: tuple[float, float] = (0.0, 1.0)) -> str:
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
        payload = node_layer_payload(nxt)
        if payload is not None:
            current = _apply_pointwise_corrector(current, payload, lut_cache)
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
