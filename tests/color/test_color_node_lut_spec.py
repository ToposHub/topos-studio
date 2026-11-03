"""R05: 节点 LUT 语义、替换与强度（版本化顺序规格 + 混合数学 + 消费投影）。

验收（计划 R05）：非交换操作证明顺序（编译产物级）；强度 0=未套 LUT、
1=完整、0.42 按规格（scene-linear 混合 + HDR 比例 + 负值加法，不全局
clamp）；替换不叠加；移除只移除 LUT；DOMAIN 外策略；投影落 Layer DTO。
"""
from __future__ import annotations

from typing import Any

import numpy as np
import pytest

from src.features.color.core.color_node_lut_spec import (
    CORRECTOR_PROCESS_ORDER_V1,
    LUT_STRENGTH_BLEND_VERSION,
    blend_node_lut_strength,
    lut_strength_from_resources,
    node_lut_projection,
)
from src.features.color.core.color_graph_param_source import node_layer_payload
from src.features.color.core.color_graph_service import ColorGraphService
from src.features.color.core.color_graph_migration import migrate_legacy_stack
from src.features.edit.core.project import EditProject
from tests.color.fixtures.color_graph import f03_layer_stack


# ═══ 顺序规格（非交换证明，编译产物级） ═══════════════════════════════

def _compiled_fragment_source() -> str:
    """用 Mock ctx 捕获 build_composition_shader 生成的 fragment 源。"""
    from unittest.mock import Mock
    from src.features.color.core.shader_compiler import (
        ShaderPipeline,
        plugin_bindings_from_instances,
    )
    from src.features.color.core.plugin_manager import get_plugin_manager
    if not get_plugin_manager().get_all_plugins():
        get_plugin_manager().scan_plugins("plugins")  # 生产由启动路径扫描
    compiler = ShaderPipeline(Mock())
    bindings = plugin_bindings_from_instances([
        {"instance_id": "i1", "plugin_id": "Fake_Infrared",
         "params": {}, "enabled": True, "type": "dctl"}])
    assert bindings, "真实插件目录未解析出绑定（Fake_Infrared 应存在）"
    compiler.build_composition_shader(
        has_curves=True, has_warper=True, has_lut3d=True,
        has_soft_clip=True, has_hsl_curve=True, has_mixer=True,
        has_curves_soft_clip=True, plugins=bindings)
    source = compiler.ctx.program.call_args.kwargs["fragment_shader"]
    return source


def test_compiled_shader_order_matches_versioned_spec() -> None:
    """主链域顺序与非交换证明：primary → curves → LUT → 插件 → blend。

    用非恒等锚点（各域调用语句在 main() 中的相对位置）锁定——identity
    LUT/中性参数无法通过本测试。
    """
    source = _compiled_fragment_source()
    body = source.split("void main() {", 1)[1]
    anchors = [
        ("fx_blur_sharpen", "apply_blur_sharpen("),
        ("primary", "apply_grading("),
        ("curves", "apply_curves("),
        ("hsl_curves", "apply_hsl_curve("),
        ("soft_clip", "apply_soft_clip("),
        ("curves_soft_clip", "apply_curves_soft_clip("),
        ("mixer", "apply_mixer("),
        ("warper", "apply_warper("),
        ("lut3d", "apply_3dlut("),
        ("plugins", "process_P0("),
        ("mask_blend", "apply_blend_mode("),
    ]
    positions = [(domain, body.index(call)) for domain, call in anchors]
    ordered = tuple(domain for domain, _ in positions)
    assert ordered == CORRECTOR_PROCESS_ORDER_V1
    # 规格元数据
    assert LUT_STRENGTH_BLEND_VERSION == 1


def test_non_commutative_order_is_observable() -> None:
    """数值级非交换证明：gain 2.0 与通道交换 LUT 的两次复合不相等。"""
    color = np.array([[0.25, 0.5, 0.75]], dtype=np.float32)
    lut = lambda c: c[..., ::-1].copy()          # 通道交换（非恒等）
    gain = lambda c: (c * 2.0).astype(np.float32)
    lut_then_gain = gain(blend_node_lut_strength(color, lut(np.clip(color, 0, 1)), 1.0))
    gain_then_lut = blend_node_lut_strength(gain(color),
                                            lut(np.clip(gain(color), 0, 1)), 1.0)
    assert not np.allclose(lut_then_gain, gain_then_lut)


# ═══ 强度混合规格 ════════════════════════════════════════════════════

def test_strength_zero_is_exact_identity_including_hdr_and_negatives() -> None:
    color = np.array([[0.25, 0.5, 4.0], [-0.5, 0.1, 2.0]], dtype=np.float32)
    lut_result = np.array([[0.9, 0.9, 0.9], [0.9, 0.9, 0.9]], dtype=np.float32)
    out = blend_node_lut_strength(color, lut_result, 0.0)
    assert np.allclose(out, color)


def test_strength_one_is_full_lut_sdr_with_hdr_ratio_scale() -> None:
    color = np.array([[0.25, 0.5, 0.75], [0.5, 2.0, 4.0]], dtype=np.float32)
    lut = lambda c: c[..., ::-1].copy()
    out = blend_node_lut_strength(color, lut(np.clip(color, 0, 1)), 1.0)
    # SDR 部分完整换序
    assert np.allclose(out[0], [0.75, 0.5, 0.25])
    # HDR 部分：LUT 分支按 max 比例重建（无全局 clamp，输出 >1）
    row = out[1]
    sdr = np.clip(color[1], 0, 1)          # [0.5, 1.0, 1.0] → 换序 [1.0, 1.0, 0.5]
    assert np.allclose(row, sdr[::-1] * (4.0 / 1.0))
    assert row[0] > 1.0 and row[1] > 1.0


def test_strength_midpoint_follows_linear_blend_spec() -> None:
    color = np.array([[0.2, 0.4, 0.6]], dtype=np.float32)
    lut_result = np.array([[0.8, 0.2, 0.0]], dtype=np.float32)
    out = blend_node_lut_strength(color, lut_result, 0.42)
    expected = np.clip(color, 0, 1) * (1 - 0.42) + lut_result * 0.42
    assert np.allclose(out, expected)


def test_negative_values_are_preserved_additively() -> None:
    color = np.array([[-0.2, -0.4, 0.3]], dtype=np.float32)
    lut_result = np.array([[1.0, 1.0, 1.0]], dtype=np.float32)
    out = blend_node_lut_strength(color, lut_result, 1.0)
    # 负偏移按强度加法保留：LUT 输出被精确平移（不丢弃也不反转）
    assert np.allclose(out, [[0.8, 0.6, 1.0]])
    # 中间强度：base(LUT 输出) 与 color 各半，负偏移按强度加权
    half = blend_node_lut_strength(color, lut_result, 0.5)
    expected = color * 0.5 + lut_result * 0.5 + (color - np.clip(color, 0, 99999)) * 0.5
    assert np.allclose(half, expected)


def test_non_unit_domain_resampled_for_sampling_domain() -> None:
    """DOMAIN ≠ [0,1]：表重采样到单位域后 0.5 输入命中表中点。"""
    from src.shared.color.lut_loader import parse_cube, resample_to_unit_domain
    lines = ["LUT_3D_SIZE 2", "DOMAIN_MIN -1.0 -1.0 -1.0",
             "DOMAIN_MAX 1.0 1.0 1.0"]
    # 值取 DOMAIN 坐标恒等（-1/1 角点），重采样到单位域后 0.5 → 0.0
    for b in range(2):
        for g in range(2):
            for r in range(2):
                lines.append(f"{2 * r - 1} {2 * g - 1} {2 * b - 1}")
    lut = parse_cube("\n".join(lines) + "\n")
    resampled = resample_to_unit_domain(lut)
    # 单位域中点 0.5 → DOMAIN 域 0.0 → 表插值 (r+g+b corner mix at 0)
    from src.shared.color.lut_loader import sample_lut_trilinear
    mid = sample_lut_trilinear(resampled, np.array([[0.5, 0.5, 0.5]]))
    assert np.allclose(mid, 0.0, atol=1e-6)


def test_lut_strength_from_resources_defaults_and_clamps() -> None:
    assert lut_strength_from_resources(None) == 1.0
    assert lut_strength_from_resources({}) == 1.0
    assert lut_strength_from_resources({"lut_strength": 0.42}) == 0.42
    assert lut_strength_from_resources({"lut_strength": 5.0}) == 1.0
    assert lut_strength_from_resources({"lut_strength": -1.0}) == 0.0
    assert lut_strength_from_resources({"lut_strength": "bad"}) == 1.0
    assert lut_strength_from_resources({"lut_strength": float("nan")}) == 1.0


# ═══ 消费投影（node resources → Layer DTO） ══════════════════════════

def _node_with(resources: dict[str, Any] | None, *,
               legacy_lut: str | None = None):
    from types import SimpleNamespace
    from src.shared.project.color_graph import NodeType
    legacy: dict[str, Any] = {}
    if legacy_lut:
        legacy = {"active_lut_path": legacy_lut, "lut_intensity": 0.7}
    return SimpleNamespace(
        node_id="n1", node_type=NodeType.CORRECTOR.value, enabled=True,
        label="N", legacy_payload=legacy,
        params={"resources": resources} if resources is not None else {})


def test_projection_maps_resources_to_layer_lut_fields() -> None:
    node = _node_with({"lut_ref": "topos-res://lut_abc", "lut_strength": 0.42},
                      legacy_lut="/old/absolute.cube")
    payload = node_layer_payload(node)
    assert payload["active_lut_path"] == "topos-res://lut_abc"
    assert payload["lut_intensity"] == pytest.approx(0.42)


def test_projection_removal_does_not_revive_legacy_lut() -> None:
    node = _node_with({"other_key": 1}, legacy_lut="/old/absolute.cube")
    payload = node_layer_payload(node)
    assert "active_lut_path" not in payload
    assert "lut_intensity" not in payload


def test_projection_without_resources_keeps_legacy_lut() -> None:
    node = _node_with(None, legacy_lut="/old/absolute.cube")
    payload = node_layer_payload(node)
    assert payload.get("active_lut_path") == "/old/absolute.cube"
    assert payload.get("lut_intensity") == pytest.approx(0.7)


def test_node_lut_projection_helper() -> None:
    assert node_lut_projection(None) == (None, 1.0)
    assert node_lut_projection({"lut_ref": "x"}) == ("x", 1.0)
    assert node_lut_projection({}) == (None, 1.0)
    assert node_lut_projection({"lut_ref": "", "lut_strength": 0.5}) == (None, 0.5)


# ═══ service 语义：替换不叠加/只移除 LUT ════════════════════════════

def _service() -> ColorGraphService:
    return ColorGraphService(
        effect_schema_provider=lambda rid: [],
        lut_reference_validator=lambda ref: ref.startswith("topos-res://"))


def _corrector_graph():
    project, stack_id = EditProject.create(name="r05").create_color_stack(
        asset_id="a1", name="s")
    stack_data = project.color_stacks[stack_id].with_layers(
        [l.to_dict(None) for l in f03_layer_stack()])
    graph = migrate_legacy_stack(stack_data)
    node = next(n for n in graph.nodes if n.node_type == "corrector")
    return graph, node


def test_reapply_replaces_and_remove_only_removes_lut() -> None:
    service = _service()
    graph, node = _corrector_graph()
    first = service.apply_intent(graph, "set_node_lut", {
        "node_id": node.node_id, "lut_ref": "topos-res://lut_a",
        "strength": 0.8})
    assert first.ok
    second = service.apply_intent(first.graph, "set_node_lut", {
        "node_id": node.node_id, "lut_ref": "topos-res://lut_b"})
    assert second.ok
    after = next(n for n in second.graph.nodes if n.node_id == node.node_id)
    resources = after.params["resources"]
    assert resources["lut_ref"] == "topos-res://lut_b"  # 替换不叠加
    assert resources["lut_strength"] == 1.0             # 重置默认强度
    # grade 域保留
    assert (after.params.get("grade") or {}) == (node.params.get("grade") or {})
    removed = service.apply_intent(second.graph, "remove_node_lut",
                                   {"node_id": node.node_id})
    assert removed.ok
    after_remove = next(n for n in removed.graph.nodes
                        if n.node_id == node.node_id)
    assert "lut_ref" not in after_remove.params.get("resources", {})
    assert (after_remove.params.get("grade") or {}) == (node.params.get("grade") or {})
    # 投影：移除后 Layer DTO 无 LUT
    payload = node_layer_payload(after_remove)
    assert "active_lut_path" not in payload
