# -*- coding: utf-8 -*-
"""CW-B09:warper 的 LUT 点态表达与图粘贴域测试。

验收:HS 两分量 warper(legacy/任意空间)烘焙进 cube 且数值与 CPU 参考
一致;三分量/CL/CW 显式拒绝(不产出少效果的 cube);图侧 warper 域粘贴
三键齐写/清除。
"""
from __future__ import annotations

import numpy as np
import pytest

from src.features.color.core.graph_lut_export import (
    bake_graph_lut,
    graph_pointwise_export_errors,
)
from src.features.color.core.color_graph_service import ColorGraphService
from src.features.color.core.warper_color import (
    apply_warper_in_space,
    apply_warper_legacy_hsv,
)
from src.shared.project.color_graph import (
    ColorEdgeData,
    ColorGraphData,
    ColorNodeData,
    NodeType,
)

GRID = np.zeros((8, 8, 2), np.float32)
GRID[:, :4, 0] = 0.2    # 低饱和半区加 Δhue
GRID[:, 4:, 1] = -0.3   # 高饱和半区降 Δsat


def _graph(legacy_payload: dict) -> ColorGraphData:
    c1 = ColorNodeData(node_id="c1", node_type=NodeType.CORRECTOR.value,
                       params={}, legacy_payload=legacy_payload)
    source = ColorNodeData(node_id="source", node_type=NodeType.SOURCE.value)
    output = ColorNodeData(node_id="output", node_type=NodeType.OUTPUT.value)

    def edge(src, dst):
        return ColorEdgeData(edge_id=f"{src}->{dst}", src_node=src,
                             src_port="image", dst_node=dst, dst_port="image")
    return ColorGraphData(nodes=(source, c1, output),
                          edges=(edge("source", "c1"), edge("c1", "output")),
                          source_node_id="source", output_node_id="output")


def _parse_cube_center(cube_text: str, size: int = 17) -> np.ndarray:
    from src.shared.color.lut_loader import parse_cube
    loaded = parse_cube(cube_text)
    mid = size // 2
    return np.asarray(loaded.table[mid, mid, mid][:3], dtype=np.float64)


# ---- 可表达:legacy HSV 与 v2 空间真实进 cube ----

@pytest.mark.parametrize("space,apply_fn", [
    (0, apply_warper_legacy_hsv),
    (1, lambda c, g: apply_warper_in_space(c, g, "hsl")),
    (2, lambda c, g: apply_warper_in_space(c, g, "hsp")),
], ids=["legacy_hsv", "hsl", "hsp"])
def test_expressible_warper_bakes_real_effect(space, apply_fn) -> None:
    payload = {"warper_params": GRID.tolist(), "warper_space": space}
    graph = _graph(payload)
    assert graph_pointwise_export_errors(graph) == []
    cube = bake_graph_lut(graph, size=17)
    mid = 8 / 16  # 中心格的线性域值
    center_color = np.array([mid, mid, mid])
    expected = apply_fn(center_color, GRID)
    got = _parse_cube_center(cube)
    # 灰中心:legacy 家族 sat<0.02 旁路 → 恒等;这里只断言 bake 无错且
    # 数值路径可复算(彩色格的强断言见下)
    np.testing.assert_allclose(got, expected, rtol=1e-2, atol=1e-3)


def test_baked_cube_matches_cpu_on_colored_points() -> None:
    graph = _graph({"warper_params": GRID.tolist(), "warper_space": 0})
    cube = bake_graph_lut(graph, size=17)
    from src.shared.color.lut_loader import parse_cube
    loaded = parse_cube(cube)
    axis = np.linspace(0, 1, 17)
    checked = 0
    for b in range(0, 17, 4):
        for g in range(0, 17, 4):
            for r in range(0, 17, 4):
                color = np.array([axis[r], axis[g], axis[b]])
                expected = apply_warper_legacy_hsv(color, GRID)
                sampled = np.asarray(loaded.table[b, g, r][:3], dtype=np.float64)
                np.testing.assert_allclose(sampled, expected, rtol=1e-2, atol=2e-3,
                                           err_msg=f"cube@({r},{g},{b})")
                checked += 1
    assert checked == 125


def test_v2_state_payload_bakes_like_projection() -> None:
    from src.features.color.core.warper_state import (
        CURRENT_ALGORITHM, WARPER_SCHEMA_VERSION, WarperGrid, WarperStateV2,
    )
    state = WarperStateV2(
        schema_version=WARPER_SCHEMA_VERSION, algorithm_version=CURRENT_ALGORITHM,
        mode="hue_saturation", color_space="hsp", axis_angle=0.0,
        grids={"hs": WarperGrid.identity("hs", 8, 8, ("dh", "ds"))
               .with_offsets(GRID)})
    graph = _graph({"warper_state": state.to_dict()})
    assert graph_pointwise_export_errors(graph) == []
    cube = bake_graph_lut(graph, size=17)
    axis = np.linspace(0, 1, 17)
    from src.shared.color.lut_loader import parse_cube
    loaded = parse_cube(cube)
    for b, g, r in ((16, 8, 0), (8, 0, 16), (4, 12, 12)):
        color = np.array([axis[r], axis[g], axis[b]])
        expected = apply_warper_in_space(color, GRID, "hsp")
        sampled = np.asarray(loaded.table[b, g, r][:3], dtype=np.float64)
        np.testing.assert_allclose(sampled, expected, rtol=1e-2, atol=2e-3)


# ---- 不可表达:显式拒绝 ----

def test_unrepresentable_warper_rejected_not_silently_dropped() -> None:
    from src.features.color.core.warper_state import (
        CURRENT_ALGORITHM, WARPER_SCHEMA_VERSION, WarperGrid, WarperStateV2,
    )
    luma_state = WarperStateV2(
        schema_version=WARPER_SCHEMA_VERSION, algorithm_version=CURRENT_ALGORITHM,
        mode="hue_saturation", color_space="hsv", axis_angle=0.0,
        grids={"hs": WarperGrid.identity("hs", 6, 6, ("dh", "ds", "dl"))})
    graph = _graph({"warper_state": luma_state.to_dict()})
    errors = graph_pointwise_export_errors(graph)
    assert any("warper" in e for e in errors), errors
    with pytest.raises(ValueError, match="warper"):
        bake_graph_lut(graph, size=17)
    # CL 模式同样拒绝
    cl_state = WarperStateV2(
        schema_version=WARPER_SCHEMA_VERSION, algorithm_version=CURRENT_ALGORITHM,
        mode="chroma_luma", color_space="hsv", axis_angle=0.0,
        grids={"grid1": WarperGrid.identity("grid1", 6, 6, ("dc", "dl")),
               "grid2": WarperGrid.identity("grid2", 6, 6, ("dc", "dl"))})
    graph_cl = _graph({"warper_state": cl_state.to_dict()})
    assert any("warper" in e for e in graph_pointwise_export_errors(graph_cl))


def test_bad_component_count_rejected() -> None:
    bad = {"warper_params": np.zeros((6, 6, 3), np.float32).tolist()}
    graph = _graph(bad)
    assert any("warper" in e for e in graph_pointwise_export_errors(graph))


# ---- 图粘贴域 ----

def test_graph_paste_warper_domain_round_trip() -> None:
    service = ColorGraphService()
    target_graph, target_id = _graph({}), "c1"
    payload = {"warper": {"warper_params": GRID.tolist(), "warper_space": 2,
                          "warper_state": None}}
    pasted = service.apply_intent(target_graph, "paste_selected_domains", {
        "node_ids": [target_id], "payload": payload, "domains": ["warper"]})
    assert pasted.ok, pasted.diagnostics
    node = {n.node_id: n for n in pasted.graph.nodes}[target_id]
    assert np.asarray(node.legacy_payload["warper_params"]).shape == (8, 8, 2)
    assert node.legacy_payload["warper_space"] == 2

    # 清除粘贴:空 Mapping(通用循环跳过 None 载荷,清除用 {} 表达)
    cleared = service.apply_intent(pasted.graph, "paste_selected_domains", {
        "node_ids": [target_id], "payload": {"warper": {}}, "domains": ["warper"]})
    assert cleared.ok
    node = {n.node_id: n for n in cleared.graph.nodes}[target_id]
    assert node.legacy_payload["warper_params"] is None
