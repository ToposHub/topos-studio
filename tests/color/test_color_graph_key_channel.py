"""N02：key 边图域化——key_out 产物 / key_in 消费 / fail-closed。

验收（计划 N02）：梯度 key、窗口、限定器、Outside 反相互补、KeyMixer
组合、Invert Key Input 消费；GL↔CPU 同一组合顺序（qualifier/window →
外部 key 相乘，invert 先取反）；**无依据替代移除**——key 引用无产物/
缺 key 边一律显式拒绝（不 published、不常白、不拿图像 R 通道顶替）。

GL 生产 keyer 的逐像素对拍在 test_m06_keyer_gpu_parity.py；本文件用
CPU 移植作为 matte_provider 注入 GL 执行器，验证**组合与通道**语义。
"""
from __future__ import annotations

from dataclasses import replace as _replace

import numpy as np
import pytest

from src.features.color.core.color_graph_compiler import compile_graph
from src.features.color.core.color_graph_cpu import run_plan_cpu
from src.features.color.core.color_graph_snapshot import freeze_graph
from src.shared.project.color_graph import (
    ColorEdgeData, ColorGraphData, ColorNodeData, NodeType,
)

_W = _H = 32

_QUALIFIER_A = {"enabled": True, "lum_enabled": True, "hue_enabled": False,
                "sat_enabled": False, "lum_low": 0.2, "lum_high": 0.6,
                "lum_soft_low": 0.05, "lum_soft_high": 0.05}
_QUALIFIER_B = {"enabled": True, "lum_enabled": True, "hue_enabled": False,
                "sat_enabled": False, "lum_low": 0.4, "lum_high": 0.8,
                "lum_soft_low": 0.05, "lum_soft_high": 0.05}


def _gradient_source() -> np.ndarray:
    """水平亮度梯度（0→1）+ 恒定色相——qualifier lum 带的可分刺激。"""
    ramp = np.linspace(0.0, 1.0, _W, dtype=np.float32)
    rgb = np.stack([ramp, ramp * 0.8 + 0.1, ramp * 0.5 + 0.2], axis=-1)
    return np.repeat(rgb[None, :, :], _H, axis=0)  # (H, W, 3)


def _node(node_id: str, node_type: NodeType, *, params=None,
          legacy=None) -> ColorNodeData:
    return ColorNodeData(node_id=node_id, node_type=node_type.value,
                         params=params or {},
                         legacy_payload=legacy or {})


def _corrector(node_id: str, qualifier: dict | None) -> ColorNodeData:
    legacy = {"id": node_id, "grading_params": {}}
    if qualifier is not None:
        legacy["qualifier_params"] = dict(qualifier)
    return _node(node_id, NodeType.CORRECTOR, legacy=legacy)


def _graph_with_alpha(nodes, edges, alpha_id):
    return ColorGraphData(
        nodes=tuple(nodes), edges=tuple(edges),
        source_node_id="src", output_node_id="out")


def _alpha_graph(qualifier_by_node: dict[str, dict | None],
                 *, invert_key_in: str | None = None,
                 chain: tuple[str, ...] = ("c1",)):
    """source → c1(→ c2 …) → alpha_output（target=alpha；output 悬空边）。"""
    nodes = [_node("src", NodeType.SOURCE),
            _node("out", NodeType.OUTPUT)]
    edges = [ColorEdgeData("src->c1", "src", "image", chain[0], "image")]
    prev = chain[0]
    for nxt in chain[1:]:
        edges.append(ColorEdgeData(f"{prev}->{nxt}", prev, "image",
                                   nxt, "image"))
        prev = nxt
    for node_id in chain:
        nodes.append(_corrector(node_id, qualifier_by_node.get(node_id)))
    edges.append(ColorEdgeData(f"{prev}->alpha", prev, "key_out",
                               "alpha", "key"))
    nodes.append(_node("alpha", NodeType.ALPHA_OUTPUT))
    edges.append(ColorEdgeData("src->out", "src", "image", "out", "image"))
    graph = _graph_with_alpha(nodes, edges, "alpha")
    if invert_key_in:
        idx = [n.node_id for n in graph.nodes].index(invert_key_in)
        node = graph.nodes[idx]
        patched = _replace(
            node, params={"key_settings": {"invert_key_in": True}})
        graph = _replace(graph, nodes=tuple(
            patched if i == idx else n for i, n in enumerate(graph.nodes)))
    return graph, "alpha"


def _cpu_matte_provider(cache: dict):
    """测试 matte_provider：qualifier/窗口 CPU 移植 → 纹理（宿主语义）。"""

    def provide(layer, input_tex):
        from src.features.color.core.qualifier_cpu import (
            compute_qualifier_matte,
        )
        matte = compute_qualifier_matte(
            _GRADIENT, layer.get("qualifier_params") or {})
        if matte is None:
            return None
        key = id(layer)
        tex = cache.get(key)
        return tex if tex is not None else _upload(matte, cache, key)

    return provide


_GRADIENT = _gradient_source()
_PROVIDER_CACHE: dict = {}


def _upload(matte: np.ndarray, cache: dict, key) -> "Any":
    import moderngl  # 延迟：仅 GL 用例创建 ctx 后调用

    ctx = _CTX[0]
    data = np.ascontiguousarray(
        np.repeat(matte.astype(np.float16), 4, axis=-1))
    tex = ctx.texture((_W, _H), components=4, dtype="f2", data=data.tobytes())
    cache[key] = tex
    return tex


_CTX: list = []


def _params_of(graph):
    payloads = {n.node_id: dict(n.legacy_payload or {}) for n in graph.nodes}
    params_by_id = {n.node_id: dict(n.params or {}) for n in graph.nodes}

    def provide(key):
        # GL 执行器传 node_id 字符串；run_plan_cpu 传 RenderPass
        node_id = key if isinstance(key, str) else key.node_id
        payload = dict(payloads.get(node_id) or {})
        settings = params_by_id.get(node_id, {}).get("key_settings")
        if settings:
            payload["key_settings"] = dict(settings)
        return payload
    return provide


def _gl_alpha(graph, alpha_id, ctx, executor) -> np.ndarray:
    plan = compile_graph(freeze_graph(graph, session_id="s",
                                      viewer_target=f"viewer:{alpha_id}"),
                         size=(_W, _H))
    src_data = np.ascontiguousarray(np.dstack([
        _GRADIENT, np.ones((_H, _W, 1), np.float32)])).astype(np.float16)
    src_tex = ctx.texture((_W, _H), components=4, dtype="f2",
                          data=src_data.tobytes())
    result = executor.execute_plan(
        plan, src_tex, request_id=f"req-{alpha_id}", source_size=(_W, _H),
        layer_param_provider=_params_of(graph),
        matte_provider=_cpu_matte_provider(_PROVIDER_CACHE))
    assert result.published, result.diagnostic_message
    fbo = ctx.framebuffer(color_attachments=[result.output_texture])
    fbo.use()
    out = np.frombuffer(fbo.read(components=4, dtype="f2"),
                        dtype=np.float16).reshape(_H, _W, 4).astype(np.float32)
    executor.release_revision(f"req-{alpha_id}")
    src_tex.release()
    return out[..., 0]


def _cpu_alpha(graph, alpha_id) -> np.ndarray:
    plan = compile_graph(freeze_graph(graph, session_id="s",
                                      viewer_target=f"viewer:{alpha_id}"),
                         size=(_W, _H))
    execution = run_plan_cpu(plan, _GRADIENT, _params_of(graph))
    assert execution.alpha_output is not None
    return execution.alpha_output[..., 0]


def _qualifier_matte(qualifier: dict) -> np.ndarray:
    from src.features.color.core.qualifier_cpu import compute_qualifier_matte
    return compute_qualifier_matte(_GRADIENT, qualifier)[..., 0]


@pytest.fixture()
def gl():
    moderngl = pytest.importorskip("moderngl")
    ctx = moderngl.create_standalone_context(require=330)
    from src.features.color.core.color_graph_executor import ColorGraphExecutor
    _CTX.clear()
    _CTX.append(ctx)
    _PROVIDER_CACHE.clear()
    executor = ColorGraphExecutor(ctx)
    yield ctx, executor
    try:
        executor.release_pool()
    except Exception:
        pass
    for tex in list(_PROVIDER_CACHE.values()):
        try:
            tex.release()
        except Exception:
            pass
    _PROVIDER_CACHE.clear()
    ctx.release()


# ── 1. corrector.key_out = qualifier matte（GL 上传回读 ≈ CPU 移植） ──


def test_qualifier_key_out_gl_matches_cpu(gl) -> None:
    ctx, executor = gl
    graph, alpha_id = _alpha_graph({"c1": _QUALIFIER_A})
    cpu = _cpu_alpha(graph, alpha_id)
    gpu = _gl_alpha(graph, alpha_id, ctx, executor)
    np.testing.assert_allclose(gpu, cpu, atol=3e-3)
    assert cpu.std() > 0.01  # 梯度上确有区分度


# ── 2. key_in 消费：invert_key_in 取反；相乘组合顺序 ──────────────────


def test_key_in_inverts_and_multiplies(gl) -> None:
    ctx, executor = gl
    # c1(qualifierA) → c2.key_in（invert）→ alpha：alpha = 1 - qA
    graph, alpha_id = _alpha_graph(
        {"c1": _QUALIFIER_A, "c2": None}, chain=("c1", "c2"),
        invert_key_in="c2")
    edges = list(graph.edges) + [ColorEdgeData(
        "c1k->c2", "c1", "key_out", "c2", "key_in")]
    graph = _replace(graph, edges=tuple(edges))
    expected = 1.0 - _qualifier_matte(_QUALIFIER_A)
    cpu = _cpu_alpha(graph, alpha_id)
    np.testing.assert_allclose(cpu, expected, atol=1e-5)
    gpu = _gl_alpha(graph, alpha_id, ctx, executor)
    np.testing.assert_allclose(gpu, expected, atol=3e-3)

    # 不 invert + c2 自带 qualifierB：alpha = qA * qB（相乘组合）
    graph2, alpha2 = _alpha_graph(
        {"c1": _QUALIFIER_A, "c2": _QUALIFIER_B}, chain=("c1", "c2"))
    edges2 = list(graph2.edges) + [ColorEdgeData(
        "c1k->c2", "c1", "key_out", "c2", "key_in")]
    graph2 = _replace(graph2, edges=tuple(edges2))
    expected2 = (_qualifier_matte(_QUALIFIER_A)
                 * _qualifier_matte(_QUALIFIER_B))
    np.testing.assert_allclose(_cpu_alpha(graph2, alpha2), expected2,
                               atol=1e-5)
    np.testing.assert_allclose(
        _gl_alpha(graph2, alpha2, ctx, executor), expected2, atol=3e-3)


# ── 3. Outside 反相互补 + KeyMixer 组合 ────────────────────────────────


def test_outside_complement_and_key_mixer(gl) -> None:
    ctx, executor = gl
    # source → C(qA)（image 旁路上抛）→ Outside(key_in=C.key_out)
    c1 = _corrector("c1", _QUALIFIER_A)
    outside = _node("outside", NodeType.OUTSIDE,
                    legacy={"id": "outside", "grading_params": {
                        "gain": (-0.5, -0.5, -0.5)}})
    nodes = [_node("src", NodeType.SOURCE), _node("out", NodeType.OUTPUT),
             c1, outside, _node("alpha", NodeType.ALPHA_OUTPUT)]
    edges = [
        ColorEdgeData("e1", "src", "image", "c1", "image"),
        ColorEdgeData("e2", "c1", "image", "outside", "image"),
        ColorEdgeData("ek", "c1", "key_out", "outside", "key_in"),
        ColorEdgeData("ea", "outside", "key_out", "alpha", "key"),
        ColorEdgeData("e-out", "src", "image", "out", "image"),
    ]
    graph = _graph_with_alpha(nodes, edges, "alpha")
    expected = 1.0 - _qualifier_matte(_QUALIFIER_A)
    cpu = _cpu_alpha(graph, "alpha")
    np.testing.assert_allclose(cpu, expected, atol=1e-5)
    np.testing.assert_allclose(_gl_alpha(graph, "alpha", ctx, executor),
                               expected, atol=3e-3)

    # KeyMixer：qA ∩ (1-qA)：outside.key_in = c1.key_out（补集），
    # km 输入 c1.key_out 与 outside.key_out
    km = _node("km", NodeType.KEY_MIXER, params={"mode": "intersect"})
    outside2 = _node("outside2", NodeType.OUTSIDE,
                     legacy={"id": "outside2", "grading_params": {}})
    nodes = [_node("src", NodeType.SOURCE), _node("out", NodeType.OUTPUT),
             c1, outside2, _node("alpha", NodeType.ALPHA_OUTPUT), km]
    edges = [
        ColorEdgeData("e1", "src", "image", "c1", "image"),
        ColorEdgeData("e-out", "src", "image", "out", "image"),
        ColorEdgeData("e2", "c1", "image", "outside2", "image"),
        ColorEdgeData("ek2", "c1", "key_out", "outside2", "key_in"),
        ColorEdgeData("k1", "c1", "key_out", "km", "key"),
        ColorEdgeData("k2", "outside2", "key_out", "km", "key"),
        ColorEdgeData("ka", "km", "key", "alpha", "key"),
    ]
    graph_km = _graph_with_alpha(nodes, edges, "alpha")
    cpu_km = _cpu_alpha(graph_km, "alpha")
    np.testing.assert_allclose(cpu_km, expected * _qualifier_matte(
        _QUALIFIER_A), atol=1e-5)  # qA ∩ (1-qA) = qA(1-qA)
    np.testing.assert_allclose(
        _gl_alpha(graph_km, "alpha", ctx, executor),
        _qualifier_matte(_QUALIFIER_A) * expected, atol=3e-3)


# ── 4. fail-closed：无产物/缺边不再常白 ───────────────────────────────


def test_key_reference_without_producer_rejected(gl) -> None:
    ctx, executor = gl
    # c1 无 qualifier/窗口/外部 key → alpha 引用 c1.key_out 必须拒绝
    graph, alpha_id = _alpha_graph({"c1": None})
    cpu_rejected = False
    try:
        _cpu_alpha(graph, alpha_id)
    except Exception as exc:  # noqa: BLE001 — UnsupportedNode
        assert "key 产物" in str(exc) or "key_output" in str(exc)
        cpu_rejected = True
    assert cpu_rejected

    plan = compile_graph(freeze_graph(graph, session_id="s",
                                      viewer_target=f"viewer:{alpha_id}"),
                         size=(_W, _H))
    src_tex = ctx.texture((_W, _H), components=4, dtype="f2")
    from src.features.color.core.color_graph_executor import ColorGraphExecutor
    result = ColorGraphExecutor(ctx).execute_plan(
        plan, src_tex, request_id="fc", source_size=(_W, _H),
        layer_param_provider=_params_of(graph))
    assert not result.published
    assert result.diagnostic_code == "key_output_missing"
    src_tex.release()


def test_alpha_without_key_edge_rejected(gl) -> None:
    ctx, executor = gl
    # alpha_output 无 key 边（悬空）→ 拒绝（此前返回常白）
    nodes = [_node("src", NodeType.SOURCE), _node("out", NodeType.OUTPUT),
             _node("alpha", NodeType.ALPHA_OUTPUT)]
    edges = [ColorEdgeData("e-out", "src", "image", "out", "image")]
    graph = _graph_with_alpha(nodes, edges, "alpha")
    plan = compile_graph(freeze_graph(graph, session_id="s",
                                      viewer_target="viewer:alpha"),
                         size=(_W, _H))
    src_tex = ctx.texture((_W, _H), components=4, dtype="f2")
    from src.features.color.core.color_graph_executor import ColorGraphExecutor
    result = ColorGraphExecutor(ctx).execute_plan(
        plan, src_tex, request_id="fc2", source_size=(_W, _H))
    assert not result.published
    assert result.diagnostic_code == "key_input_missing"
    src_tex.release()


# ── 5. 窗口 key_out + parallel 声明 key 槽 ────────────────────────────


def test_window_key_out_and_parallel_declared_key(gl) -> None:
    ctx, executor = gl
    circle = {"id": "w1", "type": "circle", "center": [0.5, 0.5],
              "size": [0.5, 0.5], "rotation": 0.0, "feather": 0.0,
              "opacity": 1.0, "visible": True, "invert": False,
              "operation": "add"}
    window_node = ColorNodeData(
        node_id="cw", node_type=NodeType.CORRECTOR.value,
        legacy_payload={"id": "cw", "grading_params": {},
                        "mask_shapes": [circle]})
    nodes = [_node("src", NodeType.SOURCE), _node("out", NodeType.OUTPUT),
             window_node, _node("alpha", NodeType.ALPHA_OUTPUT)]
    edges = [
        ColorEdgeData("e1", "src", "image", "cw", "image"),
        ColorEdgeData("ek", "cw", "key_out", "alpha", "key"),
        ColorEdgeData("e-out", "src", "image", "out", "image"),
    ]
    graph = _graph_with_alpha(nodes, edges, "alpha")

    def window_provider(layer, input_tex):
        from src.features.color.core.layer import MaskShape
        from src.features.color.core.mask_shapes_cpu import combine_masks_cpu
        items = [MaskShape.from_dict(dict(s))
                 for s in layer.get("mask_shapes") or []]
        mask, report = combine_masks_cpu(items, _W, _H)
        assert not report.unsupported
        return _upload(mask[:, :, np.newaxis], _PROVIDER_CACHE, "cw")

    plan = compile_graph(freeze_graph(graph, session_id="s",
                                      viewer_target="viewer:alpha"),
                         size=(_W, _H))
    src_data = np.ascontiguousarray(np.dstack([
        _GRADIENT, np.ones((_H, _W, 1), np.float32)])).astype(np.float16)
    src_tex = ctx.texture((_W, _H), components=4, dtype="f2",
                          data=src_data.tobytes())
    result = executor.execute_plan(
        plan, src_tex, request_id="win", source_size=(_W, _H),
        layer_param_provider=_params_of(graph),
        matte_provider=window_provider)
    assert result.published, result.diagnostic_message
    fbo = ctx.framebuffer(color_attachments=[result.output_texture])
    fbo.use()
    gpu = np.frombuffer(fbo.read(components=4, dtype="f2"),
                        dtype=np.float16).reshape(_H, _W, 4)[..., 0]
    executor.release_revision("win")
    src_tex.release()
    # 窗口中心在界内、远角在界外
    assert gpu[_H // 2, _W // 2] > 0.5
    assert gpu[1, 1] < 0.1
    # CPU 同图：mask_shapes 域 → key 产物一致
    cpu = _cpu_alpha(graph, "alpha")
    np.testing.assert_allclose(gpu.astype(np.float32), cpu, atol=3e-3)
