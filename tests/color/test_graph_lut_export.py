"""N13: 图 LUT 导出（pointwise 可表达性预检 + 串行链烘焙）。

验收（计划 N13）：3D 色立方导出可用且重载语义一致；串行 pointwise+LUT
顺序正确；含 Glow/插件/窗口/分支/Compound 的图**显式拒绝**（不产出
「成功但少效果」的 cube）；重载导出 LUT 对照。
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from src.features.color.core.color_graph_migration import migrate_legacy_stack
from src.features.color.core.color_graph_service import ColorGraphService
from src.features.color.core.graph_lut_export import (
    bake_graph_lut,
    graph_pointwise_export_errors,
)
from src.features.edit.core.project import EditProject
from tests.color.fixtures.color_graph import f03_layer_stack


def _graph():
    project, sid = EditProject.create(name="n13").create_color_stack(
        asset_id="a1", name="s")
    stack = project.color_stacks[sid].with_layers(
        [l.to_dict(None) for l in f03_layer_stack()])
    return migrate_legacy_stack(stack)


def test_clean_pointwise_graph_exports_and_reloads(tmp_path: Path) -> None:
    graph = _graph()
    # f03 含曲线/插件 stub 载荷——干净化：仅保留 grading 域的串行链
    from dataclasses import replace as _r
    service = ColorGraphService()
    errors = graph_pointwise_export_errors(graph)
    if errors:
        pytest.skip(f"f03 载荷含非 pointwise 域（{errors[:1]}）——用最小图验证")
    cube = bake_graph_lut(graph, size=17)
    path = tmp_path / "export.cube"
    path.write_text(cube)
    from src.shared.color.lut_loader import parse_cube
    loaded = parse_cube(cube)
    assert loaded.size == 17 and loaded.format == "cube"
    assert np.allclose(loaded.domain_min, 0.0)
    assert np.allclose(loaded.domain_max, 1.0)


def _minimal_chain_graph():
    """source → C1(gain) → output 的最小 pointwise 图。"""
    from src.shared.project.color_graph import (
        ColorEdgeData, ColorGraphData, ColorNodeData, NodeType,
    )
    c1 = ColorNodeData(node_id="c1", node_type=NodeType.CORRECTOR.value,
                       params={"grade": {"gain": 1.0, "exposure": 0.0}},
                       legacy_payload={})
    source = ColorNodeData(node_id="source", node_type=NodeType.SOURCE.value)
    output = ColorNodeData(node_id="output", node_type=NodeType.OUTPUT.value)

    def edge(src, dst):
        return ColorEdgeData(edge_id=f"{src}->{dst}", src_node=src,
                             src_port="image", dst_node=dst, dst_port="image")
    return ColorGraphData(nodes=(source, c1, output),
                          edges=(edge("source", "c1"), edge("c1", "output")),
                          source_node_id="source", output_node_id="output")


def test_export_matches_pointwise_oracle_per_point() -> None:
    graph = _minimal_chain_graph()
    cube = bake_graph_lut(graph, size=9)
    from src.shared.color.lut_loader import parse_cube, sample_lut_trilinear
    loaded = parse_cube(cube)
    # 网格点精确对照 oracle（gain=1 恒等链 → 表值≈输入网格）
    axis = np.linspace(0, 1, 9)
    pts = np.stack(np.meshgrid(axis, axis, axis, indexing="ij"), -1)[
        ..., ::-1].reshape(-1, 3)  # rgb 网格
    out = sample_lut_trilinear(loaded, pts)
    assert np.allclose(out, pts, atol=2e-3)  # 6 位小数 roundtrip


def test_non_pointwise_graphs_rejected_with_explanation() -> None:
    service = ColorGraphService(effect_schema_provider=lambda rid: [],
                                lut_reference_validator=lambda r: True)
    # 1) 插件实例（Glow/NR 域）→ 拒绝
    graph = _minimal_chain_graph()
    with_fx = service.apply_intent(graph, "add_node_effect",
                                   {"node_id": "c1", "resource_id": "glow"})
    assert with_fx.ok
    errors = graph_pointwise_export_errors(with_fx.graph)
    assert any("glow" in e or "插件" in e for e in errors), errors
    with pytest.raises(ValueError, match="not exportable"):
        bake_graph_lut(with_fx.graph, size=5)
    # 2) 分支结构 → 拒绝
    branched = service.apply_intent(graph, "add_parallel_branch",
                                    {"branch_origin_id": "c1"})
    if branched.ok:
        assert graph_pointwise_export_errors(branched.graph)
    # 3) Compound → 拒绝
    grouped = service.apply_intent(graph, "group_subgraph",
                                   {"member_ids": ["c1"]})
    if grouped.ok:
        errors = graph_pointwise_export_errors(grouped.graph)
        assert any("compound" in e for e in errors), errors


def test_lut_order_is_baked_after_grading(tmp_path: Path) -> None:
    """串行 pointwise+LUT 顺序：grading 先、LUT 后（非交换对照）。"""
    swap = tmp_path / "swap.cube"
    lines = ["LUT_3D_SIZE 2"]
    for b in range(2):
        for g in range(2):
            for r in range(2):
                lines.append(f"{b} {g} {r}")  # R/B 交换
    swap.write_text("\n".join(lines) + "\n")
    # 正式资源引用（图 schema 禁 params 绝对路径）：注册 + 激活 resolver
    import shutil
    from src.shared.project.resource_resolver import (
        ProjectResourceResolver, ResourceEntry, ResourceIndex,
        set_active_project_resolver, sha256_file,
    )
    sha = sha256_file(str(swap))
    entry = ResourceEntry(resource_id=f"lut_{sha[:32]}", kind="lut",
                          archive_path=f"resources/lut/{sha}.cube",
                          size_bytes=swap.stat().st_size, sha256=sha,
                          original_filename="swap.cube", roles=("lut",))
    extracted = tmp_path / "extracted"
    (extracted / "resources" / "lut").mkdir(parents=True)
    shutil.copy(swap, extracted / "resources" / "lut" / f"{sha}.cube")
    set_active_project_resolver(ProjectResourceResolver(
        str(extracted), ResourceIndex(entries={entry.resource_id: entry})))
    service = ColorGraphService(
        effect_schema_provider=lambda rid: [],
        lut_reference_validator=lambda ref: ref.startswith("topos-res://"))
    graph = _minimal_chain_graph()
    lutted = service.apply_intent(graph, "set_node_lut", {
        "node_id": "c1", "lut_ref": entry.uri, "strength": 1.0})
    assert lutted.ok, lutted.diagnostics
    cube = bake_graph_lut(lutted.graph, size=5)
    from src.shared.color.lut_loader import parse_cube, sample_lut_trilinear
    out = sample_lut_trilinear(parse_cube(cube), np.array([[0.8, 0.5, 0.2]]))
    # grading 恒等 → LUT 交换：R↔B（0.8↔0.2）
    assert np.allclose(out, [[0.2, 0.5, 0.8]], atol=2e-3)
    set_active_project_resolver(None)
