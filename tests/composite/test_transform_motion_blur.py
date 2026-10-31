"""A5: transform motion blur — sub-frame math, applicability, contracts.

GPU accumulation itself needs a GL context (manual smoke); the closed
form, zero-overhead fall-through and cache/serialization contracts are
covered here.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pytest

from src.features.composite.core.node_engine import NODE_REGISTRY
from src.features.composite.nodes.transform_node import TransformNode

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
