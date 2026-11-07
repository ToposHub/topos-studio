from types import SimpleNamespace

import numpy as np
import pytest

from src.features.color.core.color_graph_migration import migrate_legacy_stack
from src.features.color.core.color_graph_param_source import graph_node_layer_provider
from src.features.color.core.color_graph_service import ColorGraphService
from src.features.color.core.grade_keyframes import GradeKeyframe, GradeKeyframeTrack
from src.features.color.core.layer import Layer
from tests.color.test_color_graph_migration import _stack_with_layers

SCHEMA = [{"var_name": "amount", "type": "slider", "default": 0.2, "min": 0.0, "max": 1.0},
          {"var_name": "mode", "type": "select", "default": 0, "options": ["a", "b"]}]


def graph_and_service():
    layer = Layer("FX")
    layer.plugins = [{"instance_id": identity, "plugin_id": "example", "type": "dctl",
                      "enabled": True, "params": {"amount": amount, "mode": 0}, "future": "keep"}
                     for identity, amount in (("a", 0.2), ("b", 0.3))]
    graph = migrate_legacy_stack(_stack_with_layers([layer]))
    node = next(n for n in graph.nodes if n.node_type == "corrector")
    return graph, node.node_id, ColorGraphService(effect_schema_provider=lambda _: SCHEMA)


def animated_graph():
    graph, node_id, service = graph_and_service()
    result = service.apply_intent(graph, "batch_edit_keyframes", {"edits": [
        {"action": "set", "node_id": node_id, "param": "fx.a.amount", "frame": frame, "value": value}
        for frame, value in ((0, 0.2), (20, 0.8))]})
    assert result.ok, result.diagnostics
    return result.graph, node_id


def test_effect_keys_are_instance_scoped_and_project_round_trip():
    from src.shared.project.color_graph import ColorGraphData
    from src.app.controllers.color_keyframes_host import build_keyframes_snapshot
    graph, node_id = animated_graph()
    graph = ColorGraphData.from_dict(graph.to_dict())
    payload = graph_node_layer_provider(graph, frame=10)(node_id)
    assert payload["plugins"][0]["params"]["amount"] == pytest.approx(0.5)
    assert payload["plugins"][1]["params"]["amount"] == 0.3
    assert payload["plugins"][0]["future"] == "keep"
    snapshot = build_keyframes_snapshot(context="s", graph=graph, frame_count=21,
        timecode_fps=25, playhead=10, corrector="", effect_schema_provider=lambda _: SCHEMA)
    leaves = [leaf for group in snapshot.tracks[0].children for leaf in group.children]
    assert next(leaf for leaf in leaves if leaf.id.endswith("|fx.a.amount")).markers
    assert not next(leaf for leaf in leaves if leaf.id.endswith("|fx.b.amount")).markers


@pytest.mark.parametrize("param,value,code", [
    ("fx.missing.amount", 0.5, "instance_not_found"),
    ("fx.a.mode", 0.5, "illegal_param_key"),
    ("fx.a.unknown", 0.5, "illegal_param_key"),
    ("fx.a.amount", 2.0, "param_out_of_range"),
])
def test_effect_key_admission_uses_resource_schema(param, value, code):
    graph, node_id, service = graph_and_service()
    result = service.apply_intent(graph, "batch_edit_keyframes", {"edits": [
        {"action": "set", "node_id": node_id, "param": "fx.b.amount", "frame": 5, "value": 0.4},
        {"action": "set", "node_id": node_id, "param": param, "frame": 10, "value": value},
    ]})
    assert not result.ok and result.primary_code == code
    assert result.graph is graph


def test_legacy_layer_evaluation_and_save_use_the_original_fx_base():
    from src.features.color.core.image_engine import ImageEngine
    layer = Layer("FX")
    layer.plugins = [{"instance_id": "a", "plugin_id": "example", "params": {"amount": 0.2}}]
    layer.grade_keyframes["fx.a.amount"] = GradeKeyframeTrack("fx.a.amount", [GradeKeyframe(0, 0.2), GradeKeyframe(20, 0.8)])
    assert ImageEngine.apply_grade_keyframes(SimpleNamespace(layers=[layer]), 10, render=False)
    assert layer.plugins[0]["params"]["amount"] == pytest.approx(0.5)
    assert layer.to_dict(None)["plugins"][0]["params"]["amount"] == 0.2
    layer.restore_grade_base("fx.a.amount")
    assert layer.plugins[0]["params"]["amount"] == 0.2
    layer.restore_grade_base()
    assert layer._plugins_base is None


def test_removing_effect_removes_only_its_owned_animation():
    graph, node_id = animated_graph()
    service = ColorGraphService(effect_schema_provider=lambda _: SCHEMA)
    result = service.apply_intent(graph, "remove_node_effect", {"node_id": node_id, "instance_id": "a"})
    assert result.ok
    node = next(n for n in result.graph.nodes if n.node_id == node_id)
    assert [p["instance_id"] for p in node.legacy_payload["plugins"]] == ["b"]
    assert "fx.a.amount" not in node.params.get("grade_keyframes", {})


@pytest.mark.parametrize("domains", [("effects", "keyframes"), ("keyframes", "effects")])
def test_effect_domain_copy_remaps_animation_with_new_instance_ids(domains):
    graph, node_id = animated_graph()
    node = next(n for n in graph.nodes if n.node_id == node_id)
    service = ColorGraphService(effect_schema_provider=lambda _: SCHEMA)
    result = service.apply_intent(graph, "paste_selected_domains", {
        "node_ids": [node_id], "domains": domains,
        "payload": {"effects": node.legacy_payload["plugins"], "keyframes": node.params["grade_keyframes"]}})
    assert result.ok, result.diagnostics
    payload = graph_node_layer_provider(result.graph, frame=10)(node_id)
    identity = payload["plugins"][0]["instance_id"]
    assert identity != "a"
    assert f"fx.{identity}.amount" in payload["grade_keyframes"]
    assert payload["plugins"][0]["params"]["amount"] == pytest.approx(0.5)


def test_keyframes_only_paste_rejects_orphan_effect_tracks():
    graph, node_id = animated_graph()
    node = next(n for n in graph.nodes if n.node_id == node_id)
    tracks = {name.replace("fx.a.", "fx.missing."): value
              for name, value in node.params["grade_keyframes"].items()}
    result = ColorGraphService().apply_intent(graph, "paste_selected_domains", {
        "node_ids": [node_id], "domains": ["keyframes"], "payload": {"keyframes": tracks}})
    assert not result.ok
    assert result.graph is graph


def test_two_instances_render_animated_parameters_on_gl(monkeypatch):
    import moderngl
    from src.features.color.core import plugin_manager, shader_compiler
    from src.features.color.core.color_graph_texture import ColorGraphTextureRenderer
    metadata = {"name": "Example", "params": SCHEMA,
                "shader_code": "vec3 process(vec3 color, vec2 uv) { return color * amount; }"}
    manager = SimpleNamespace(get_plugin=lambda _: metadata)
    monkeypatch.setattr(plugin_manager, "get_plugin_manager", lambda: manager)
    monkeypatch.setattr(shader_compiler, "get_plugin_manager", lambda: manager)
    graph, _node_id = animated_graph()
    ctx = moderngl.create_standalone_context(require=330)
    renderer = ColorGraphTextureRenderer(ctx)
    source = ctx.texture((4, 4), 3, np.full((4, 4, 3), 0.5, "f4").tobytes(), dtype="f4")
    try:
        for frame, amount in ((0, 0.2), (10, 0.5), (20, 0.8)):
            texture = renderer.render(source, graph, frame)
            values = np.frombuffer(texture.read(), dtype="f4").reshape(4, 4, 4)[..., :3]
            np.testing.assert_allclose(values, 0.5 * amount * 0.3, atol=1e-5)
    finally:
        renderer.release()
        source.release()
        ctx.release()
