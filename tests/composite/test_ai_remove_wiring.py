"""B6: ai_remove node wiring tests (LaMa + mask input).

Covers:
- model mapping includes ai_remove → lama_inpaint
- worker inference path builds the 4-channel LaMa input for ai_remove
- node is no longer a placeholder; ports/params/serialization intact
- scheduler classifies ai_remove as a real effect (not "placeholder")
- cache-hit readback preserves pixels outside the mask
- AICacheManager put/has/get round trip (the disk cache the evaluator reads)

No GL context or ONNX model required — the model-dependent smoke run
lives in test_ai_inference.py (guarded by model availability).
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import numpy as np
import pytest

from src.features.composite.core.node_capabilities import is_placeholder_node_type
from src.features.composite.nodes.ai_remove_node import AIRemoveNode
from src.features.composite.nodes.base_node import TextureResult
from src.features.composite.nodes.placeholder_node import PlaceholderNode
from src.features.composite.render.ai_cache_manager import AICacheManager
from src.features.composite.render.ai_compute_manager import (
    AI_MODEL_MAPPING, AIComputeWorker,
)

pytestmark = pytest.mark.composite_core


class TestAIRemoveModelMapping(unittest.TestCase):

    def test_remove_maps_to_lama(self):
        self.assertEqual(AI_MODEL_MAPPING.get("ai_remove"), "lama_inpaint")

    def test_inpaint_family_shared_model(self):
        self.assertEqual(AI_MODEL_MAPPING["ai_inpaint"],
                         AI_MODEL_MAPPING["ai_remove"])


class _FakeSession:
    """Minimal ORT session stub capturing the assembled input tensor."""

    def __init__(self, output_shape):
        self._out = np.zeros(output_shape, dtype=np.float32)
        self.captured = {}

    def get_outputs(self):
        class Spec:
            name = "output"
        return [Spec()]

    def get_inputs(self):
        class Spec:
            name = "input"
        return [Spec()]

    def run(self, names, feeds):
        self.captured["shape"] = feeds["input"].shape
        self.captured["mask_sum"] = float(feeds["input"][0, 3].sum())
        self.captured["mask_max"] = float(feeds["input"][0, 3].max())
        return [self._out]


class TestAIRemoveInferencePath(unittest.TestCase):
    """The worker's input assembly must treat ai_remove like inpaint."""

    @staticmethod
    def _worker():
        return AIComputeWorker("key", "ai_remove", {}, {},
                               cache_manager=None)

    def test_run_inference_builds_4ch_input(self):
        worker = self._worker()
        h, w = 16, 16
        image = np.zeros((h, w, 3), dtype=np.float32)
        image[:, :, 0] = 0.5
        mask = np.zeros((h, w), dtype=np.float32)
        mask[4:12, 4:12] = 1.0

        session = _FakeSession((1, 3, h, w))
        result = worker._run_inference(image, mask, session)
        self.assertEqual(session.captured["shape"], (1, 4, h, w))
        self.assertAlmostEqual(session.captured["mask_sum"], 64.0, places=3)
        self.assertEqual(result.shape, (h, w, 3))

    def test_run_inference_without_mask_uses_zero_mask(self):
        worker = self._worker()
        h, w = 8, 8
        image = np.full((h, w, 3), 0.25, dtype=np.float32)

        session = _FakeSession((1, 3, h, w))
        worker._run_inference(image, None, session)
        self.assertEqual(session.captured["mask_max"], 0.0)


class TestAIRemoveNodeReal(unittest.TestCase):

    def test_no_longer_placeholder(self):
        self.assertFalse(issubclass(AIRemoveNode, PlaceholderNode))
        self.assertFalse(getattr(AIRemoveNode, "IS_PLACEHOLDER", False))
        self.assertFalse(is_placeholder_node_type("ai_remove"))

    def test_ports_and_defaults(self):
        node = AIRemoveNode(node_id="rm1")
        self.assertEqual([p.name for p in node.inputs], ["image", "mask"])
        self.assertEqual([p.name for p in node.outputs], ["image"])
        self.assertEqual(node.parameters["strength"], 1.0)
        self.assertEqual(node.parameters["use_tiling"], True)
        self.assertEqual(node.parameters["tile_size"], 1024)

    def test_fallback_passthrough_invalid_input(self):
        node = AIRemoveNode(node_id="rm1")
        invalid = TextureResult(texture_id=-1, width=0, height=0)
        self.assertEqual(
            node._do_evaluate({"image": invalid}, ctx=None, fb_manager=None),
            {})

    def test_serialize_round_trip(self):
        node = AIRemoveNode(node_id="rm1")
        node.set_parameter("strength", 0.5)
        node.set_parameter("tile_size", 512)
        node2 = AIRemoveNode.from_dict(node.to_dict())
        self.assertEqual(node2.parameters["strength"], 0.5)
        self.assertEqual(node2.parameters["tile_size"], 512)

    def test_old_project_defaults(self):
        node = AIRemoveNode.from_dict(
            {"id": "rm1", "type": "ai_remove",
             "parameters": {"strength": 0.9}})
        # new params fall back to defaults on old projects
        self.assertEqual(node.parameters["tile_size"], 1024)
        self.assertEqual(node.parameters["use_tiling"], True)


class TestAIRemoveSchedulerAction(unittest.TestCase):

    def test_plan_action_is_effect(self):
        from src.features.composite.core.graph_snapshot import NodeSnapshot
        from src.features.composite.render.render_scheduler import (
            RenderScheduler)
        snap = NodeSnapshot(
            node_id="n1", node_type="ai_remove", parameters={},
            disabled=False, input_ports=[("image", "image"), ("mask", "image")],
            output_ports=[("image", "image")])
        from src.features.composite.core.graph_snapshot import GraphSnapshot
        # AUD-48006 后 GraphSnapshot 为 frozen dataclass，字段经构造器传递
        graph_snap = GraphSnapshot(
            generation=1, nodes={"n1": snap}, eval_order=["n1"])
        plan = RenderScheduler().plan(graph_snap, "preview")
        self.assertEqual(plan.node_plans["n1"].action, "effect")


class TestMaskPreservingReadback(unittest.TestCase):
    """The cache-hit path must guarantee pixels outside the mask."""

    @staticmethod
    def _cls():
        from src.features.composite.evaluators.base_evaluator import (
            BaseEvaluatorMixin)
        return BaseEvaluatorMixin

    def test_read_ai_cache_result_composites_mask(self):
        import cv2
        cls = self._cls()
        h, w = 8, 8
        image = np.full((h, w, 3), 0.2, dtype=np.float32)
        mask = np.zeros((h, w), dtype=np.float32)
        mask[2:6, 2:6] = 1.0

        cached = np.full((h, w, 3), 0.9, dtype=np.float32)
        path = os.path.join(tempfile.mkdtemp(), "k.exr")
        assert cv2.imwrite(path, cached,
                           [cv2.IMWRITE_EXR_COMPRESSION, 1])  # T-P1-71: ZIP 经 libdeflate 间歇 SIGILL

        class FakeCache:
            def get_cache_path(self, key):
                return path

        out = cls._read_ai_cache_result(FakeCache(), "k", image, mask)
        self.assertIsNotNone(out)
        # outside mask → input preserved
        self.assertAlmostEqual(float(out[0, 0, 0]), 0.2, places=5)
        # inside mask → cached result
        self.assertAlmostEqual(float(out[4, 4, 0]), 0.9, places=5)

    def test_missing_cache_file_returns_none(self):
        cls = self._cls()
        image = np.zeros((4, 4, 3), dtype=np.float32)

        class FakeCache:
            def get_cache_path(self, key):
                return None

        self.assertIsNone(cls._read_ai_cache_result(FakeCache(), "k", image, None))


class TestAICacheRoundTrip(unittest.TestCase):

    def test_put_has_get(self):
        cache = AICacheManager(cache_dir=tempfile.mkdtemp())
        img = np.random.rand(8, 8, 3).astype(np.float32)
        key = cache.compute_key("ai_remove", {"strength": 1.0},
                                upstream_key="up", frame_number=3)
        self.assertFalse(cache.has_cache(key))
        cache.put(key, img)
        self.assertTrue(cache.has_cache(key))
        self.assertTrue(cache.get_cache_path(key).endswith(f"{key}.exr"))

    def test_strength_enters_key(self):
        cache = AICacheManager(cache_dir=tempfile.mkdtemp())
        a = cache.compute_key("ai_remove", {"strength": 1.0})
        b = cache.compute_key("ai_remove", {"strength": 0.5})
        self.assertNotEqual(a, b)


class TestMaskNormalization(unittest.TestCase):
    """Regression: the mask readback is RGB (H,W,3); every consumer needs
    a single-channel (H,W) matte — a 3-channel mask used to raise a
    broadcast ValueError in the cache composite and a concat ValueError
    inside the ONNX input builder."""

    def test_run_inference_accepts_3ch_mask(self):
        worker = AIComputeWorker("key", "ai_remove", {}, {}, cache_manager=None)
        h, w = 8, 8
        img = np.zeros((h, w, 3), dtype=np.float32)
        mask = np.ones((h, w, 3), dtype=np.float32)  # wrong ndim on purpose
        session = _FakeSession((1, 3, h, w))
        out = worker._run_inference(img, mask, session)
        self.assertEqual(session.captured["shape"], (1, 4, h, w))
        self.assertAlmostEqual(session.captured["mask_max"], 1.0, places=3)
        self.assertEqual(out.shape, (h, w, 3))

    @staticmethod
    def _cls():
        from src.features.composite.evaluators.base_evaluator import (
            BaseEvaluatorMixin)
        return BaseEvaluatorMixin

    def test_read_ai_cache_result_with_3ch_mask(self):
        import cv2
        cls = self._cls()
        h, w = 8, 8
        image = np.full((h, w, 3), 0.2, dtype=np.float32)
        cached = np.full((h, w, 3), 0.9, dtype=np.float32)
        path = os.path.join(tempfile.mkdtemp(), "k.exr")
        assert cv2.imwrite(path, cached,
                           [cv2.IMWRITE_EXR_COMPRESSION, 1])  # T-P1-71: ZIP 经 libdeflate 间歇 SIGILL

        class FakeCache:
            def get_cache_path(self, key):
                return path

        mask3 = np.ones((h, w, 3), dtype=np.float32)
        out = cls._read_ai_cache_result(FakeCache(), "k", image, mask3)
        self.assertIsNotNone(out)
        # mask=1 → cached wins everywhere
        self.assertTrue(np.allclose(out, cached, atol=1e-3))

    def test_read_ai_cache_result_mask_size_mismatch_returns_none(self):
        cls = self._cls()
        import cv2
        image = np.zeros((8, 8, 3), dtype=np.float32)
        cached = np.zeros((8, 8, 3), dtype=np.float32)
        path = os.path.join(tempfile.mkdtemp(), "k.exr")
        assert cv2.imwrite(path, cached,
                           [cv2.IMWRITE_EXR_COMPRESSION, 1])  # T-P1-71: ZIP 经 libdeflate 间歇 SIGILL

        class FakeCache:
            def get_cache_path(self, key):
                return path

        bad_mask = np.ones((4, 4), dtype=np.float32)
        self.assertIsNone(
            cls._read_ai_cache_result(FakeCache(), "k", image, bad_mask))

    def test_tiling_params_enter_cache_key(self):
        cache = AICacheManager(cache_dir=tempfile.mkdtemp())
        a = cache.compute_key("ai_remove", {"tile_size": 1024})
        b = cache.compute_key("ai_remove", {"tile_size": 512})
        self.assertNotEqual(a, b)
        # generation nodes keep stable keys when inpaint params absent
        c1 = cache.compute_key("ai_generate", {"seed": 1})
        c2 = cache.compute_key("ai_generate", {"seed": 1})
        self.assertEqual(c1, c2)
