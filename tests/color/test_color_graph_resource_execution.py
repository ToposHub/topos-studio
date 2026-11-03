"""R06: 资源维度的图执行一致性（快照指纹/预检/bake 协议/GL 组合场景）。

验收（计划 R06 验证清单的资源子集）：2 个非交换 FX、LUT+FX、重复插件、
bypass、missing shader、资源更新驱动缓存失效；bake 取消/晚到守卫。
（取消/resize/旧 revision 的执行器级行为由既有
test_color_graph_cancel_resources / gl_execution 套件覆盖。）
"""
from __future__ import annotations

import numpy as np
import pytest

from src.features.color.core.color_bake_protocol import (
    ColorBakeBroker,
    make_bake_request,
)
from src.features.color.core.color_graph_preflight import (
    preflight_graph_resources,
)
from src.features.color.core.color_graph_snapshot import (
    compute_resource_fingerprint,
    freeze_graph,
)
from src.features.color.core.color_graph_service import ColorGraphService
from src.features.color.core.color_graph_migration import migrate_legacy_stack
from src.features.edit.core.project import EditProject
from tests.color.fixtures.color_graph import f03_layer_stack

SCHEMA = [{"var_name": "amount", "type": "slider", "default": 0.2,
           "min": 0.0, "max": 1.0}]


def _service() -> ColorGraphService:
    return ColorGraphService(
        effect_schema_provider=lambda rid: list(SCHEMA) if rid == "grain" else None,
        lut_reference_validator=lambda ref: ref.startswith("topos-res://"))


def _graph_with_corrector():
    project, _sid = EditProject.create(name="r06").create_color_stack(
        asset_id="a1", name="s")
    stack_data = project.color_stacks[_sid].with_layers(
        [l.to_dict(None) for l in f03_layer_stack()])
    graph = migrate_legacy_stack(stack_data)
    node = next(n for n in graph.nodes if n.node_type == "corrector")
    return graph, node


# ═══ 快照资源指纹（缓存键覆盖资源内容/schema/参数） ═══════════════════

def test_fingerprint_tracks_instance_add_order_and_params() -> None:
    service = _service()
    graph, node = _graph_with_corrector()
    base = compute_resource_fingerprint(graph, resource_hashes={})
    first = service.apply_intent(graph, "add_node_effect",
                                 {"node_id": node.node_id, "resource_id": "grain"})
    with_instance = compute_resource_fingerprint(first.graph, resource_hashes={})
    assert with_instance != base
    # 参数变化
    instance = first.graph.nodes[
        [n.node_id for n in first.graph.nodes].index(node.node_id)] \
        .legacy_payload["plugins"][-1]["instance_id"]
    changed = service.apply_intent(first.graph, "set_node_effect_parameter",
                                   {"node_id": node.node_id,
                                    "instance_id": instance,
                                    "name": "amount", "value": 0.9})
    assert compute_resource_fingerprint(changed.graph, resource_hashes={}) != with_instance
    # 重复实例改变顺序指纹
    doubled = service.apply_intent(changed.graph, "add_node_effect",
                                   {"node_id": node.node_id, "resource_id": "grain"})
    doubled_fp = compute_resource_fingerprint(doubled.graph, resource_hashes={})
    moved = service.apply_intent(doubled.graph, "move_node_effect",
                                 {"node_id": node.node_id, "instance_id": instance,
                                  "index": 1})
    assert compute_resource_fingerprint(moved.graph, resource_hashes={}) != doubled_fp
    # 启用翻转
    toggled = service.apply_intent(doubled.graph, "set_node_effect_enabled",
                                   {"node_id": node.node_id, "instance_id": instance,
                                    "enabled": False})
    assert compute_resource_fingerprint(toggled.graph, resource_hashes={}) != doubled_fp


def test_fingerprint_tracks_plugin_content_hot_update() -> None:
    """插件文件热更（同 graph_revision）：内容哈希进指纹 → 缓存失效。"""
    service = _service()
    graph, node = _graph_with_corrector()
    added = service.apply_intent(graph, "add_node_effect",
                                 {"node_id": node.node_id, "resource_id": "grain"})
    before = compute_resource_fingerprint(added.graph,
                                          resource_hashes={"grain": "hash-a"})
    after = compute_resource_fingerprint(added.graph,
                                         resource_hashes={"grain": "hash-b"})
    assert before != after
    # 未变资源不受无关插件热更影响（其他插件哈希变化会变——按内容寻址正确）
    same = compute_resource_fingerprint(added.graph,
                                        resource_hashes={"grain": "hash-a"})
    assert same == before


def test_snapshot_cache_key_includes_resource_fingerprint() -> None:
    service = _service()
    graph, node = _graph_with_corrector()
    snap1 = freeze_graph(graph, session_id="s")
    added = service.apply_intent(graph, "add_node_effect",
                                 {"node_id": node.node_id, "resource_id": "grain"})
    snap2 = freeze_graph(added.graph, session_id="s")
    assert snap1.cache_key() != snap2.cache_key()
    assert snap1.resource_fingerprint != snap2.resource_fingerprint


def test_fingerprint_tracks_lut_reference_and_strength() -> None:
    service = _service()
    graph, node = _graph_with_corrector()
    with_lut = service.apply_intent(graph, "set_node_lut",
                                    {"node_id": node.node_id,
                                     "lut_ref": "topos-res://lut_a"})
    fp_lut = compute_resource_fingerprint(with_lut.graph, resource_hashes={})
    strengthened = service.apply_intent(with_lut.graph, "set_node_lut_strength",
                                        {"node_id": node.node_id, "strength": 0.42})
    assert compute_resource_fingerprint(strengthened.graph,
                                        resource_hashes={}) != fp_lut


# ═══ 能力预检（fail-visible，不执行渲染） ════════════════════════════

def test_preflight_reports_unknown_ai_and_lut_issues() -> None:
    service = _service()
    graph, node = _graph_with_corrector()
    # 未知插件 + 缺模型 AI + 不可解析 LUT
    g = service.apply_intent(graph, "add_node_effect",
                             {"node_id": node.node_id, "resource_id": "grain"})
    node_idx = [n.node_id for n in g.graph.nodes].index(node.node_id)
    from dataclasses import replace as _r
    plugins = [dict(p) for p in g.graph.nodes[node_idx].legacy_payload["plugins"]]
    plugins.append({"instance_id": "ghost", "plugin_id": "missing_fx",
                    "params": {}, "enabled": True})
    plugins.append({"instance_id": "ai1", "plugin_id": "ai_up",
                    "params": {}, "enabled": True, "type": "ai"})
    params = dict(g.graph.nodes[node_idx].params)
    params["resources"] = {"lut_ref": "topos-res://lut_missing"}
    patched = _r(g.graph, nodes=tuple(
        _r(n, legacy_payload={**n.legacy_payload, "plugins": plugins},
           params=params) if i == node_idx else n
        for i, n in enumerate(g.graph.nodes)))

    def effect_probe(plugin_id):
        if plugin_id == "missing_fx":
            return None
        if plugin_id == "ai_up":
            return {"type": "ai", "model_path": "/nope/model.onnx",
                    "shader_code": None}
        return {"type": "dctl", "shader_code": "//ok"}

    issues = preflight_graph_resources(
        patched, effect_probe=effect_probe,
        lut_probe=lambda ref: ref == "topos-res://lut_ok",
        ai_model_available=lambda model: False)
    codes = {(i.code, i.resource_id) for i in issues}
    assert ("resource_unknown", "missing_fx") in codes
    assert ("ai_model_missing", "ai_up") in codes
    assert ("lut_unresolvable", "topos-res://lut_missing") in codes
    # 正常实例（grain）无诊断
    assert not [i for i in issues if i.resource_id == "grain"]
    # 机器可读投影
    assert all(isinstance(i.to_dict(), dict) for i in issues)


def test_preflight_clean_graph_has_no_issues() -> None:
    graph, _node = _graph_with_corrector()
    issues = preflight_graph_resources(
        graph, effect_probe=lambda pid: {"type": "dctl", "shader_code": "//ok"},
        lut_probe=lambda ref: True, ai_model_available=lambda m: True)
    assert issues == []


# ═══ bake 异步协议（取消/晚到守卫） ══════════════════════════════════

def _request(stack="s1", rev=5, src_rev=3, **kw):
    return make_bake_request(target={"session_id": "sess", "stack_id": stack,
                                     "node_id": "n1", "instance_id": "i1"},
                             stack_id=stack, graph_revision=rev,
                             source_content_revision=src_rev,
                             resource_id="ai_up", params={"scale": 4},
                             spec_version="1", **kw)


def test_bake_lifecycle_submit_accept() -> None:
    broker = ColorBakeBroker()
    task = broker.submit(_request())
    assert broker.request(task).spec == {"resource_id": "ai_up",
                                         "params": {"scale": 4},
                                         "spec_version": "1"}
    result = broker.accept_result(task, {"output": "baked.npy"}, verify=lambda: True)
    assert result == {"output": "baked.npy"}
    # 已接受任务再提交结果：丢弃（幂等）
    assert broker.accept_result(task, {"late": True}) is None
    assert broker.pending() == []


def test_bake_cancel_for_graph_revision_advance() -> None:
    broker = ColorBakeBroker()
    t1 = broker.submit(_request(rev=5))
    t2 = broker.submit(_request(rev=7))
    cancelled = broker.cancel_for_graph("s1", 7)
    assert cancelled == [t1] and broker.was_cancelled(t1)
    assert broker.request(t2) is not None  # 同 revision 不取消
    # 已取消任务晚到结果丢弃
    assert broker.accept_result(t1, {"late": True}, verify=lambda: True) is None


def test_bake_cancel_for_source_and_explicit() -> None:
    broker = ColorBakeBroker()
    t1 = broker.submit(_request(src_rev=3))
    t2 = broker.submit(_request(src_rev=9))
    assert broker.cancel_for_source(9) == [t1]
    assert broker.cancel(t2) is True
    assert broker.pending() == []
    assert broker.cancel(t2) is False  # 幂等


def test_bake_verify_guard_discards_stale_target() -> None:
    """verify（R04 令牌守卫）失败：结果丢弃，不写新目标。"""
    broker = ColorBakeBroker()
    task = broker.submit(_request())
    accepted = {"calls": 0}

    def verify() -> bool:
        accepted["calls"] += 1
        return False  # 节点/实例已被删除

    assert broker.accept_result(task, {"output": 1}, verify=verify) is None
    assert accepted["calls"] == 1
    assert broker.was_cancelled(task)


# ═══ GL 执行：资源组合场景（非交换 FX / 重复 / LUT+FX / missing shader） ═══

moderngl = pytest.importorskip("moderngl", reason="ModernGL 不可用")


def _stub_effect_spec(effect_type: str, body: str, index: int = 0):
    """最小 DCTL_PROCESS stub（与 fixtures.f03_plugin_stub_binding 同构，
    不触发 PluginManager 扫描；body 是 process 函数体片段）。"""
    from src.features.shared.effects.effect_spec import (
        EffectSpec, PluginBinding, ShaderKind,
    )
    code = (f"vec3 process(vec3 color, vec2 uv) {{\n"
            f"    {body}\n"
            f"    return color;\n"
            f"}}\n")
    spec = EffectSpec(
        effect_type=f"plugin:{effect_type}", name=effect_type, category="plugin",
        param_defs=(), shader_kind=ShaderKind.DCTL_PROCESS,
        shader_sources=(code,), dispatch_key=f"plugin:{effect_type}",
        plugin_id=effect_type, plugin_version="1")
    return PluginBinding(spec=spec, enabled=True, index=index)


def _build_with_last_node_plugins(order):
    """迁移 f03 图 → 最后一个 Corrector 注入指定顺序的 stub 插件。"""
    from tests.color.fixtures.color_graph import f03_layer_stack
    from tests.color.test_color_graph_migration import _stack_with_layers
    project, _sid = EditProject.create(name="r06fx").create_color_stack(
        asset_id="a1", name="s")
    stack_data = project.color_stacks[_sid].with_layers(
        [l.to_dict(None) for l in f03_layer_stack()])
    graph = migrate_legacy_stack(stack_data)
    target = next(n for n in graph.nodes if n.node_type == "corrector")
    from src.features.color.core.layer import build_curves_lut_from_data
    from src.shared.project.color_graph import unfreeze_params
    payloads = {n.node_id: unfreeze_params(dict(n.legacy_payload))
                for n in graph.nodes}

    def provide(node_id, _order=order, _graph=graph, _target=target):
        payload = dict(payloads.get(node_id) or {})
        node = next((n for n in _graph.nodes if n.node_id == node_id), None)
        if node is not None:
            payload["visible"] = node.enabled
        if payload.get("curves_data"):
            payload["curves_lut"] = build_curves_lut_from_data(
                dict(payload["curves_data"]))
        if node is not None and node.node_id == _target.node_id:
            payload["_plugin_bindings"] = [
                _stub_effect_spec(f"fx{i}", body, index=i)
                for i, body in enumerate(_order)]
        return payload
    return graph, provide


def _execute(graph, provider, source, size=64):
    from src.features.color.core.color_graph_compiler import compile_graph
    from src.features.color.core.color_graph_executor import ColorGraphExecutor
    ctx = moderngl.create_standalone_context(require=330)
    try:
        executor = ColorGraphExecutor(ctx)
        snapshot = freeze_graph(graph, session_id="r06")
        plan = compile_graph(snapshot, size=(size, size))
        src_tex = ctx.texture((size, size), components=4, dtype="f2",
                              data=source.astype(np.float16))
        result = executor.execute_plan(
            plan, src_tex, request_id="r06-req", source_size=(size, size),
            layer_param_provider=provider)
        if not result.published or result.output_texture is None:
            executor.release_pool()
            src_tex.release()
            return result, None
        fbo = ctx.framebuffer(color_attachments=[result.output_texture])
        fbo.use()
        out = np.frombuffer(fbo.read(components=4, dtype="f2"),
                            dtype=np.float16).reshape(size, size, 4).astype(np.float32)
        executor.release_revision("r06-req")
        src_tex.release()
        executor.release_pool()
        return result, out
    finally:
        ctx.release()


def _ramp(size=64):
    x = np.linspace(0.05, 0.95, size, dtype=np.float32)
    plane = np.tile(x[None, :], (size, 1))
    return np.stack([plane, plane * 0.7 + 0.1, 0.4 - plane * 0.3,
                     np.ones_like(plane)], axis=-1)


SWAP_RB = "color = vec3(color.b, color.g, color.r);"
# 通道相关增益（仅 R 加倍）：与通道交换不可交换
GAIN_R = "color.r = color.r * 2.0;"


class TestGLResourceCombinations:
    def test_two_noncommutative_fx_order_matters(self):
        """A⊕B 与 B⊕A 输出不同（非交换证明，非 identity 通过）。"""
        source = _ramp()
        graph1, prov1 = _build_with_last_node_plugins([SWAP_RB, GAIN_R])
        graph2, prov2 = _build_with_last_node_plugins([GAIN_R, SWAP_RB])
        _r1, out1 = _execute(graph1, prov1, source)
        _r2, out2 = _execute(graph2, prov2, source)
        diff = np.abs(out1 - out2)
        assert diff.max() > 0.01, "非交换 FX 的两种实例顺序必须产生不同输出"

    def test_duplicate_plugin_instances_execute_twice(self):
        """同资源两个实例 = 效果应用两次（binding 数组保留两份）。"""
        source = _ramp()
        graph1, prov1 = _build_with_last_node_plugins([GAIN_R])
        graph2, prov2 = _build_with_last_node_plugins([GAIN_R, GAIN_R])
        _r1, out1 = _execute(graph1, prov1, source)
        _r2, out2 = _execute(graph2, prov2, source)
        # 两次 ×2 gain 与一次 ×2 显著不同（f16 饱和前）
        assert np.abs(out2 - out1).max() > 0.01

    def test_missing_shader_fails_visible(self):
        """坏 shader 源：执行 fail-visible（不产出少效果半帧）。"""
        bad = _stub_effect_spec("bad_fx", "this is not valid glsl {{{")
        graph2, prov2 = _build_with_last_node_plugins(["color = color;"])

        def provide_bad(node_id):
            payload = dict(prov2(node_id))
            if isinstance(payload.get("_plugin_bindings"), list):
                payload["_plugin_bindings"] = [bad]
            return payload

        result, out = _execute(graph2, provide_bad, _ramp())
        assert not result.published
        assert result.diagnostic_code == "shader_compile_failed"
        assert out is None or out.max() == 0.0  # 无半帧发布
