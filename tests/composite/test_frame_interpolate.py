"""C2 frame interpolation tests — endpoint identity, motion-position
correctness with a STUB inference session, determinism, the fallback
tier, cache signature, and node contracts.  No model download happens.
"""

import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from src.features.composite.core.format import BBox
from src.features.composite.core.node_capabilities import get_node_capability
from src.features.composite.core.node_engine import NODE_CATEGORIES, NODE_REGISTRY
from src.features.composite.nodes.base_node import TextureResult
from src.features.composite.nodes.frame_interpolate_node import (
    FrameInterpolateNode,
)
from src.shared.ai_models.rife_interpolator import (
    RifeInterpolator,
    cache_signature,
)
from src.shared.ai_models.model_manager import MODEL_REGISTRY

pytestmark = pytest.mark.composite_core


class _MovingSession:
    """Stub RIFE session: synthetic displacement field interpolation.

    Simulates real motion in-betweening — frame A is a dot at x_a, frame B
    at x_b; the stub returns the dot at x_a + t·(x_b − x_a), i.e. a
    motion-correct mid-frame a linear blend could never produce.
    """

    def __init__(self, h, w, xa, xb, radius=3):
        self.h, self.w, self.xa, self.xb, self.radius = h, w, xa, xb, radius
        self.inputs = None

    def get_inputs(self):
        class _I:
            def __init__(self, name):
                self.name = name
        return [_I("frame_a"), _I("frame_b"), _I("time")]

    def run(self, _outputs, feeds):
        self.inputs = feeds
        t = float(np.asarray(feeds["time"]).reshape(-1)[0])
        x = self.xa + t * (self.xb - self.xa)
        frame = np.zeros((self.h, self.w, 3), dtype=np.float32)
        r = self.radius
        y0 = self.h // 2
        frame[y0 - r:y0 + r, int(x) - r:int(x) + r] = 1.0
        out = np.transpose(frame, (2, 0, 1))[None, ...]
        return [out]


def _dot_frame(h, w, x, radius=3):
    frame = np.zeros((h, w, 3), dtype=np.float32)
    y0 = h // 2
    frame[y0 - radius:y0 + radius, x - radius:x + radius] = 1.0
    return frame


class TestInterpolator:
    def test_endpoints_are_exact(self):
        a = np.random.default_rng(0).random((8, 8, 3)).astype(np.float32)
        b = np.random.default_rng(1).random((8, 8, 3)).astype(np.float32)
        interp = RifeInterpolator(session=object())
        np.testing.assert_array_equal(interp.interpolate(a, b, 0.0), a)
        np.testing.assert_array_equal(interp.interpolate(a, b, 1.0), b)
        assert interp.used_fallback is False

    def test_stub_session_motion_midpoint(self):
        h, w = 48, 96
        xa, xb = 12, 80
        session = _MovingSession(h, w, xa, xb)
        interp = RifeInterpolator(session=session)
        mid = interp.interpolate(_dot_frame(h, w, xa),
                                 _dot_frame(h, w, xb), 0.5)
        expected = _dot_frame(h, w, int(round((xa + xb) / 2)))
        assert interp.used_fallback is False
        # The synthesized frame shows the dot at the motion midpoint.
        assert np.allclose(mid, expected, atol=1e-5)

    def test_deterministic(self):
        a = np.random.default_rng(2).random((16, 16, 3)).astype(np.float32)
        b = np.random.default_rng(3).random((16, 16, 3)).astype(np.float32)
        session = _MovingSession(16, 16, 4, 12)
        i1 = RifeInterpolator(session=session).interpolate(a, b, 0.4)
        i2 = RifeInterpolator(session=_MovingSession(16, 16, 4, 12)
                              ).interpolate(a, b, 0.4)
        np.testing.assert_array_equal(i1, i2)

    def test_no_session_falls_back_to_linear_and_reports(self):
        interp = RifeInterpolator(session=None)
        a = np.full((8, 8, 3), 0.2, dtype=np.float32)
        b = np.full((8, 8, 3), 0.8, dtype=np.float32)
        out = interp.interpolate(a, b, 0.5)
        assert interp.used_fallback is True
        np.testing.assert_allclose(out, 0.5, atol=1e-6)

    def test_broken_session_falls_back_gracefully(self):
        class _Broken:
            def get_inputs(self):
                raise RuntimeError("bad model")

        interp = RifeInterpolator(session=_Broken())
        a = np.full((8, 8, 3), 0.2, dtype=np.float32)
        b = np.full((8, 8, 3), 0.8, dtype=np.float32)
        out = interp.interpolate(a, b, 0.25)
        assert interp.used_fallback is True
        np.testing.assert_allclose(out, 0.35, atol=1e-6)

    def test_cache_signature_changes_with_inputs(self):
        base = cache_signature(1, 2, 0.5, False)
        assert cache_signature(1, 2, 0.5, False) == base
        assert cache_signature(9, 2, 0.5, False) != base
        assert cache_signature(1, 2, 0.6, False) != base
        assert cache_signature(1, 2, 0.5, True) != base


class TestModelRegistration:
    def test_registered_not_silently_downloaded(self):
        info = MODEL_REGISTRY.get("rife_interpolate")
        assert info is not None
        assert info.is_available is False  # never auto-downloaded
        assert info.name == "rife_interpolate.onnx"


class TestNode:
    class _Ctx:
        frame_number = 0
        gl = None

    def _tex(self, tex_id=6, rev=1):
        return TextureResult(texture_id=tex_id, width=32, height=24,
                             revision=rev, bbox=BBox.full_frame(32, 24),
                             alpha_premultiplied=False,
                             color_space="linear")

    def test_registered_in_time_category(self):
        assert NODE_REGISTRY.get("frame_interpolate") is FrameInterpolateNode
        assert any(t == "frame_interpolate"
                   for t, _ in NODE_CATEGORIES.get("Time", []))
        cap = get_node_capability("frame_interpolate")
        assert cap.action_type == "source"

    def test_frame_cache_required(self):
        assert FrameInterpolateNode.params_require_frame_cache({}) is True

    def test_endpoint_t_returns_input_without_gl(self):
        node = FrameInterpolateNode(node_id="f")
        node.set_parameter("t", 0.0)
        out = node._do_evaluate({"frame_a": self._tex(1),
                                 "frame_b": self._tex(2)},
                                self._Ctx(), None, None)
        assert out["result"].texture_id == 1
        node.set_parameter("t", 1.0)
        out = node._do_evaluate({"frame_a": self._tex(1),
                                 "frame_b": self._tex(2)},
                                self._Ctx(), None, None)
        assert out["result"].texture_id == 2

    def test_missing_a_emits_nothing_missing_b_returns_a(self):
        node = FrameInterpolateNode(node_id="f")
        assert node._do_evaluate({}, self._Ctx(), None, None) == {}
        out = node._do_evaluate({"frame_a": self._tex(1)},
                                self._Ctx(), None, None)
        assert out["result"].texture_id == 1

    def test_serialization_roundtrip(self, qapp):
        node = FrameInterpolateNode(node_id="f")
        node.set_parameter("t", 0.3)
        node.set_parameter("hd_mode", True)
        clone = FrameInterpolateNode.from_dict(node.to_dict())
        assert clone.parameters["t"] == pytest.approx(0.3)
        assert clone.parameters["hd_mode"] is True
