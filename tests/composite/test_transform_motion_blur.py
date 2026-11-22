"""A5: transform motion blur — sub-frame math, applicability, contracts.

GPU accumulation itself needs a GL context (manual smoke); the closed
form, zero-overhead fall-through and cache/serialization contracts are
covered here.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pytest

from src.features.composite.core.layer_stack_controller import LayerStackController
from src.features.composite.core.node_engine import NodeGraph
from src.features.composite.evaluators.base_evaluator import BaseEvaluatorMixin
from src.features.composite.nodes.base_node import TextureResult
from src.features.composite.nodes.constant_node import ConstantNode
from src.features.composite.nodes.output_node import MOTION_BLUR_ENABLED_PARAM, OutputNode
from src.features.composite.nodes.transform_node import TransformNode
from src.features.composite.render.cache_manager import CacheManager
from src.features.composite.render.render_scheduler import RenderScheduler
from src.shared.keyframe import Keyframe, KeyframeTrack

pytestmark = pytest.mark.composite_core


class TestSubFrameTimes:

    def _fn(self):
        from src.features.composite.evaluators.base_evaluator import (
            BaseEvaluatorMixin)
        return BaseEvaluatorMixin._motion_blur_sub_frames

    def test_closed_form_full_shutter(self):
        times = self._fn()(1.0, 5, 0.0, 100)
        assert len(times) == 5
        assert times[0] == pytest.approx(99.5)
        assert times[-1] == pytest.approx(100.5)
        # uniform spacing
        diffs = {round(b - a, 9) for a, b in zip(times, times[1:])}
        assert len(diffs) == 1

    def test_zero_shutter_collapses_to_frame(self):
        times = self._fn()(0.0, 8, 0.0, 42)
        assert all(t == 42.0 for t in times)

    def test_offset_shifts_window(self):
        base = self._fn()(0.5, 3, 0.0, 10)
        shifted = self._fn()(0.5, 3, 0.5, 10)
        assert shifted[-1] == pytest.approx(base[-1] + 0.5)


class TestApplicability:

    def _evaluator(self, keyframe_tracks, params):
        from types import SimpleNamespace
        from src.features.composite.evaluators.base_evaluator import (
            BaseEvaluatorMixin)
        ev = SimpleNamespace()
        ns = SimpleNamespace(keyframe_tracks=keyframe_tracks)
        marker = {"_effect_params": params, "_effect_input": None}
        result = BaseEvaluatorMixin._try_transform_motion_blur.__get__(ev)
        applic = BaseEvaluatorMixin._motion_blur_animated_check = None
        # exercise via the real entry with a stubbed env
        ev._snapshot = SimpleNamespace(frame_number=10)
        ev.utility_shaders = {}
        ev._interpolate_parameters = lambda ns_, f: {}
        ev._apply_transform = lambda inp, p, w, h: None
        ev._resource_pool = None
        # direct call returns None for all inapplicable cases
        out = BaseEvaluatorMixin._try_transform_motion_blur(
            SimpleNamespace(**{
                "_MOTION_BLUR_ANIMATED_PARAMS": BaseEvaluatorMixin._MOTION_BLUR_ANIMATED_PARAMS,
                "_motion_blur_sub_frames": BaseEvaluatorMixin._motion_blur_sub_frames.__get__(ev),
                "_try_transform_motion_blur": BaseEvaluatorMixin._try_transform_motion_blur.__get__(ev),
            }), ns, marker) if False else None
        # simpler: bind and call with tracking of fall-through
        bound = BaseEvaluatorMixin._try_transform_motion_blur.__get__(ev)
        return bound, ns, marker

    def test_disabled_flag_falls_through(self):
        bound, ns, marker = self._evaluator({}, {"motion_blur": False})
        assert bound(ns, marker) is None

    def test_no_animated_params_falls_through(self):
        bound, ns, marker = self._evaluator({}, {"motion_blur": True})
        assert bound(ns, marker) is None

    def test_animated_param_with_missing_shader_falls_through(self):
        # keyframed + enabled, but no GL shader available in the stub →
        # the method must degrade to the single-sample path (None)
        tracks = {"translate_x": {"keyframes": []}}
        bound, ns, marker = self._evaluator(tracks, {"motion_blur": True})
        assert bound(ns, marker) is None


class TestNodeContract:

    def test_params_registered_with_defaults(self):
        node = TransformNode(node_id="t")
        assert node.parameters["motion_blur"] is False
        assert node.parameters["shutter"] == 0.5
        assert node.parameters["samples"] == 8
        defs = node.get_param_defs()
        assert defs["samples"].min_val == 2 and defs["samples"].max_val == 16

    def test_blur_off_serialization_round_trip(self):
        node = TransformNode(node_id="t")
        data = node.to_dict()
        node2 = TransformNode.from_dict(data)
        assert node2.parameters["motion_blur"] is False

    def test_param_revision_invalidates_cache(self):
        # changing shutter/samples bumps param revision → downstream caches
        # key off it (global constraint 7)
        node = TransformNode(node_id="t")
        before = node.param_revision
        node.set_parameter("samples", 12)
        assert node.param_revision > before


@pytest.mark.parametrize("render_mode", ["preview", "export"])
def test_layer_motion_blur_master_updates_render_at_the_same_frame(render_mode):
    graph = NodeGraph()
    aux = OutputNode(node_id="aux")
    aux.set_parameter("output_name", "aux")
    aux.set_parameter(MOTION_BLUR_ENABLED_PARAM, False)
    graph.add_node(aux)
    master = OutputNode(node_id="main-output")
    graph.add_node(master)
    controller = LayerStackController(graph, layer_stack_version=2)
    controller.ensure_main_stack(master.id)
    layer_id = controller.create_generated_layer("solid", "Foreground", {})
    transform = controller.layer_transform_node(layer_id)
    transform.set_parameter("motion_blur", True)
    track = KeyframeTrack(param_name="translate_x", min_val=-1000.0, max_val=1000.0)
    track.add_keyframe(Keyframe(0, 0.0))
    track.add_keyframe(Keyframe(20, 24.0))
    transform.set_keyframe_track("translate_x", track)

    effect = TransformNode(node_id="transform-effect")
    effect.set_parameter("motion_blur", True)
    graph.add_node(effect)
    assert graph.connect(graph.get_layer(layer_id).source_node_id, "result", effect.id, "image")
    assert graph.connect(effect.id, "result", transform.id, "image")

    precomp_id = controller.create_precomp_layer()
    precomp = graph.get_node(graph.get_layer(precomp_id).root_node_id)
    nested_stack = precomp.parameters["child_stack_id"]
    source = ConstantNode(node_id="nested-source")
    graph.add_node(source)
    nested_id = controller.add_media_layer("Nested solid", source.id, stack_id=nested_stack,
                                           _force_transform=True)
    nested_transform = controller.layer_transform_node(nested_id)
    nested_transform.set_parameter("motion_blur", True)

    scheduler = RenderScheduler()
    cache = CacheManager(None)
    before = graph.snapshot(frame_number=10)
    scheduler.plan(before, render_mode)
    assert before.nodes[transform.id].parameters["motion_blur"] is True

    graph.set_node_parameter(master.id, MOTION_BLUR_ENABLED_PARAM, False)
    after = graph.snapshot(frame_number=10)
    plan = scheduler.plan(after, render_mode)
    assert transform.id in plan.dirty_node_ids
    assert effect.id not in plan.dirty_node_ids
    assert nested_transform.id not in plan.dirty_node_ids
    assert after.nodes[effect.id].parameters["motion_blur"] is True
    assert after.nodes[nested_transform.id].parameters["motion_blur"] is True
    assert transform.parameters["motion_blur"] is True
    assert controller.layer_transform_node(layer_id).parameters["motion_blur"] is True

    inputs = {"image": TextureResult(texture_id=1, width=320, height=240)}
    evaluator = BaseEvaluatorMixin()
    texture_keys = []
    for snapshot, expected in ((before, True), (after, False)):
        state = snapshot.nodes[transform.id]
        texture_keys.append(cache.compute_key(state, inputs, render_mode=render_mode))
        evaluator._snapshot = snapshot
        marker = evaluator._create_ephemeral_node(state).evaluate(
            inputs, None, None, render_mode=render_mode)
        assert marker["_effect_params"]["motion_blur"] is expected
    assert texture_keys[0] != texture_keys[1]

    graph.set_node_parameter(master.id, MOTION_BLUR_ENABLED_PARAM, True)
    assert graph.snapshot(frame_number=10).nodes[transform.id].parameters["motion_blur"] is True


def test_layer_motion_blur_master_round_trips_and_defaults_on_for_old_graphs():
    graph = NodeGraph()
    out = OutputNode(node_id="out")
    graph.add_node(out)
    controller = LayerStackController(graph)
    layer_id = controller.create_generated_layer("solid", "Legacy layer", {})
    transform = controller.layer_transform_node(layer_id)
    transform.set_parameter("motion_blur", True)
    out.set_parameter(MOTION_BLUR_ENABLED_PARAM, False)
    data = graph.to_dict()
    data["layers"][layer_id].pop("transform_node_id")

    disabled = NodeGraph.from_dict(data)
    assert disabled.get_node(out.id).parameters[MOTION_BLUR_ENABLED_PARAM] is False
    assert disabled.snapshot().nodes[transform.id].parameters["motion_blur"] is False
    assert disabled.get_layer(layer_id).transform_node_id is None

    out_payload = next(item for item in data["nodes"] if item["id"] == out.id)
    out_payload["parameters"].pop(MOTION_BLUR_ENABLED_PARAM)
    legacy = NodeGraph.from_dict(data)
    assert legacy.get_node(out.id).parameters[MOTION_BLUR_ENABLED_PARAM] is True
    assert legacy.snapshot().nodes[transform.id].parameters["motion_blur"] is True
