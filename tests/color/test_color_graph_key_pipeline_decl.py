"""N02-B 切片 1：key 管线声明进 plan（编译期派生 + 可解释性 + 一致性锚）。

验证（冻结顺序规格：qualifier → window → bitmap → invert_key_in/
external_key → post:*）：
- 声明从图数据（params 覆盖 legacy_payload）正确派生；
- 无 key 语义节点为空声明（不冒充）；
- to_plan_json 暴露两个字段（可解释性）；
- 执行侧一致性：GL 执行器对声明了 key 管线的 corrector 实际登记
  key_out（key 产物通道存在）——声明与执行不漂移。
"""
from __future__ import annotations

from typing import Any

import pytest

moderngl = pytest.importorskip("moderngl", reason="ModernGL 不可用")

from src.features.color.core.color_graph_compiler import compile_graph
from src.features.color.core.color_graph_executor import ColorGraphExecutor
from src.features.color.core.color_graph_snapshot import freeze_graph
from src.shared.project.color_graph import ColorGraphData, NodeType

from tests.color.test_color_graph_schema import _serial_graph


def _corrector(graph: ColorGraphData) -> Any:
    return next(n for n in graph.nodes if n.node_type == "corrector")


def _with_params(graph: ColorGraphData, params: dict) -> ColorGraphData:
    from dataclasses import replace
    node = _corrector(graph)
    merged = dict(node.params)
    merged.update(params)
    new_node = replace(node, params=merged)
    return replace(graph, nodes=tuple(
        new_node if n.node_id == node.node_id else n for n in graph.nodes))


def test_key_pipeline_declared_from_qualifier_and_postprocess():
    graph = _serial_graph(1)
    graph = _with_params(graph, {"qualifier": {
        "enabled": True, "clean_black": 5.0, "blur": 2.0}})
    plan = compile_graph(freeze_graph(graph, session_id="s"), size=(64, 64))
    corrector_pass = next(p for p in plan.passes if p.op == "corrector")
    assert corrector_pass.key_pipeline == (
        "qualifier", "post:clean_black", "post:blur")
    # 未显式声明 key_source_mode 时按迁移默认 legacy_raw
    assert corrector_pass.key_input_source == "legacy_raw"


def test_key_pipeline_declared_from_window_shapes_and_bitmap():
    from dataclasses import replace
    graph = _serial_graph(1)
    node = _corrector(graph)
    legacy = dict(node.legacy_payload or {})
    legacy["mask_shapes"] = [
        {"type": "circle", "id": "s1", "operation": "add"},
        {"type": "bitmap", "id": "s2", "operation": "add"},
    ]
    new_node = replace(node, legacy_payload=legacy)
    graph = replace(graph, nodes=tuple(
        new_node if n.node_id == node.node_id else n for n in graph.nodes))
    plan = compile_graph(freeze_graph(graph, session_id="s"), size=(64, 64))
    corrector_pass = next(p for p in plan.passes if p.op == "corrector")
    assert corrector_pass.key_pipeline == ("window:shapes", "bitmap")
    assert corrector_pass.key_input_source == ""  # 无 qualifier


def test_key_pipeline_respects_param_overlay_over_legacy():
    from dataclasses import replace
    graph = _serial_graph(1)
    node = _corrector(graph)
    legacy = dict(node.legacy_payload or {})
    legacy["qualifier_params"] = {"enabled": True, "clean_white": 9.0}
    new_node = replace(node, legacy_payload=legacy)
    graph = replace(graph, nodes=tuple(
        new_node if n.node_id == node.node_id else n for n in graph.nodes))
    # 图域覆盖关闭 qualifier（覆盖 legacy 基底）
    graph = _with_params(graph, {"qualifier": {"enabled": False}})
    plan = compile_graph(freeze_graph(graph, session_id="s"), size=(64, 64))
    corrector_pass = next(p for p in plan.passes if p.op == "corrector")
    assert corrector_pass.key_pipeline == ()
    # 反向：legacy 关 + 图域开
    graph2 = _with_params(_serial_graph(1), {"qualifier": {
        "enabled": True, "denoise": 3.0}})
    plan2 = compile_graph(freeze_graph(graph2, session_id="s"), size=(64, 64))
    pass2 = next(p for p in plan2.passes if p.op == "corrector")
    assert pass2.key_pipeline == ("qualifier", "post:denoise")


def test_key_input_source_consumed_from_key_settings():
    graph = _with_params(_serial_graph(1), {
        "qualifier": {"enabled": True},
        "key_settings": {"key_source_mode": "node_input"}})
    plan = compile_graph(freeze_graph(graph, session_id="s"), size=(64, 64))
    corrector_pass = next(p for p in plan.passes if p.op == "corrector")
    assert corrector_pass.key_input_source == "node_input"


def test_no_key_semantics_declares_empty():
    plan = compile_graph(freeze_graph(_serial_graph(2), session_id="s"),
                         size=(64, 64))
    for render_pass in plan.passes:
        assert render_pass.key_pipeline == ()
        assert render_pass.key_input_source == ""


def test_plan_json_exposes_key_pipeline():
    graph = _with_params(_serial_graph(1), {"qualifier": {
        "enabled": True, "clean_black": 4.0}})
    plan = compile_graph(freeze_graph(graph, session_id="s"), size=(64, 64))
    data = plan.to_plan_json()
    corrector_json = next(p for p in data["passes"] if p["op"] == "corrector")
    assert corrector_json["key_pipeline"] == [
        "qualifier", "post:clean_black"]
    assert corrector_json["key_input_source"] == "legacy_raw"


def test_executor_registers_key_out_for_declared_pipeline(monkeypatch):
    """执行一致性锚：声明了 qualifier 阶段的 pass，GL 执行必须登记
    key_out 产物（声明与执行不漂移；key 生产仍由宿主 matte_provider
    提供——此处注入与 keyer 同构的 CPU matte）。"""
    import numpy as np
    from src.features.color.core.color_graph_param_source import (
        graph_node_layer_provider,
    )
    import tests.color.test_color_graph_key_channel as key_channel
    from src.shared.project.color_graph import ColorEdgeData, ColorNodeData
    from dataclasses import replace as _replace
    graph = _with_params(_serial_graph(1), {"qualifier": {
        "enabled": True,
        "hue_enabled": True, "hue_center": 0.5, "hue_width": 1.0,
        "hue_soft": 0.1,
        "sat_enabled": True, "sat_low": 0.0, "sat_high": 1.0,
        "sat_soft_low": 0.0, "sat_soft_high": 0.0,
        "lum_enabled": True, "lum_low": 0.0, "lum_high": 1.0,
        "lum_soft_low": 0.0, "lum_soft_high": 0.0,
        "blur": 0.0, "clean_black": 0.0, "clean_white": 0.0,
        "denoise": 0.0, "invert": False}})
    # 加 key 消费者：corrector.key_out → alpha_output.key（key 生产声明
    # 为真 → 发布成功；声明撒谎（无 matte）→ key_output_missing 失败）
    corrector = _corrector(graph)
    alpha = ColorNodeData(node_id="alpha-1",
                          node_type=NodeType.ALPHA_OUTPUT.value,
                          label="Alpha")
    graph = _replace(
        graph,
        nodes=tuple(graph.nodes) + (alpha,),
        edges=tuple(graph.edges) + (ColorEdgeData(
            "k->alpha", corrector.node_id, "key_out",
            alpha.node_id, "key"),),
        alpha_output_node_id=alpha.node_id)
    plan = compile_graph(freeze_graph(graph, session_id="s"),
                         size=(64, 64))
    corrector_pass = next(p for p in plan.passes if p.op == "corrector")
    assert "qualifier" in corrector_pass.key_pipeline

    ctx = moderngl.create_standalone_context(require=330)
    executor = ColorGraphExecutor(ctx)
    # 复用既有 N02 对拍的 CPU keyer 注入（同构宿主 matte_provider 契约）：
    # 注入梯度源与 ctx 后由 _cpu_matte_provider 完成 keyer→纹理。
    monkeypatch.setattr(key_channel, "_GRADIENT", np.tile(
        np.stack([np.linspace(0.05, 0.95, 64, dtype=np.float32),
                  np.full(64, 0.5, dtype=np.float32),
                  1 - np.linspace(0.05, 0.95, 64, dtype=np.float32),
                  np.ones(64, dtype=np.float32)], axis=-1)[None, ...],
        (64, 1, 1)))
    monkeypatch.setattr(key_channel, "_W", 64)
    monkeypatch.setattr(key_channel, "_H", 64)
    monkeypatch.setattr(key_channel, "_CTX", [ctx])
    matte_cache = {}
    matte = key_channel._cpu_matte_provider(matte_cache)
    try:
        ramp = np.linspace(0.05, 0.95, 64, dtype=np.float32)
        source = np.stack([ramp] * 4, axis=-1).reshape(1, 64, 4) \
            if False else np.tile(
                np.stack([ramp, 0.5 - ramp / 2, 1 - ramp,
                          np.ones_like(ramp)], axis=-1)[None, ...],
                (64, 1, 1))
        src_tex = ctx.texture((64, 64), components=4, dtype="f2",
                              data=source.astype(np.float16))
        provider = graph_node_layer_provider(graph)
        result = executor.execute_plan(
            plan, src_tex, request_id="n02b-decl",
            source_size=(64, 64),
            layer_param_provider=provider,
            matte_provider=matte)
        assert result.published, (result.diagnostic_code,
                                  result.diagnostic_message)
        executor.release_revision("n02b-decl")
        src_tex.release()
    finally:
        executor.release_pool()
        for texture in matte_cache.values():
            texture.release()
        ctx.release()
