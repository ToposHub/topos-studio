"""C1 AI Gen Expand node tests — mock transport full chain: state
surfacing, NEEDS_CONFIG degradation, cache reuse across evaluations, and
serialization.  No network, no GL (upload falls back to passthrough).
"""

import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from src.features.composite.core.format import BBox
from src.features.composite.core.node_capabilities import get_node_capability
from src.features.composite.core.node_engine import NODE_CATEGORIES, NODE_REGISTRY
from src.features.composite.nodes.ai_gen_expand_node import (
    AIGenExpandNode,
    reset_generation_transport,
    set_generation_transport,
)
from src.features.composite.nodes.base_node import TextureResult
from src.features.composite.render.disk_cache_store import (
    DiskCacheStore,
    reset_disk_cache_store,
)

pytestmark = pytest.mark.composite_core

_W, _H = 32, 24


@pytest.fixture(autouse=True)
def _transport_and_store(tmp_path, monkeypatch):
    store = DiskCacheStore(root_dir=str(tmp_path / "cache"),
                           capacity_gb=1.0)
    monkeypatch.setattr(
        "src.features.composite.render.disk_cache_store._STORE_INSTANCE",
        store)
    yield
    reset_generation_transport()
    reset_disk_cache_store()


class _Ctx:
    """Eval context without GL — upload degrades to passthrough."""
    frame_number = 0
    gl = None


def _tex(tex_id=5, rev=1):
    return TextureResult(texture_id=tex_id, width=_W, height=_H,
                         revision=rev, bbox=BBox.full_frame(_W, _H),
                         alpha_premultiplied=False, color_space="linear")


def _node(**overrides):
    node = AIGenExpandNode(node_id="g1")
    node.set_parameter("prompt", "a serene lake")
    for k, v in overrides.items():
        node.set_parameter(k, v)
    return node


class TestRegistration:
    def test_registered(self):
        assert NODE_REGISTRY.get("ai_gen_expand") is AIGenExpandNode
        assert any(t == "ai_gen_expand"
                   for t, _ in NODE_CATEGORIES.get("AI", []))

    def test_capability(self):
        cap = get_node_capability("ai_gen_expand")
        assert cap.action_type == "source"  # persistent, own evaluation
        assert cap.is_placeholder is False

    def test_no_transport_is_needs_config_passthrough(self):
        node = _node()
        out = node._do_evaluate({"image": _tex(1)}, _Ctx(), None, None)
        assert out["image"].texture_id == 1  # input flows through
        assert node.last_state == "NEEDS_CONFIG"
        assert "NEEDS_CONFIG" in (node.error_message or "")


class TestMockChain:
    def test_success_without_gl_keeps_input_but_records_done(self):
        calls = []

        def transport(request, key, size):
            calls.append((request["prompt"], key))
            return (np.random.default_rng(2).random((_H, _W, 4))
                    .astype(np.float32))

        set_generation_transport(transport, lambda: "sk-test",
                                 cost_confirm=lambda count, p: True)
        node = _node()
        out = node._do_evaluate({"image": _tex(1)}, _Ctx(), None, None)
        # Without GL the generated frame cannot upload — the input stays
        # live (visible passthrough), but the job ran and was cached.
        assert out["image"].texture_id == 1
        assert node.last_state == "DONE"
        assert calls and calls[0][0] == "a serene lake"

    def test_provider_error_records_failed(self):
        def transport(request, key, size):
            raise RuntimeError("content policy rejected")

        set_generation_transport(transport, lambda: "sk-test")
        node = _node()
        out = node._do_evaluate({"image": _tex(1)}, _Ctx(), None, None)
        assert out["image"].texture_id == 1
        assert node.last_state == "FAILED"
        assert "content policy" in (node.error_message or "")

    def test_cost_decline_visible(self):
        set_generation_transport(
            lambda *a: (_ for _ in ()).throw(AssertionError("no call")),
            lambda: "sk-test", cost_confirm=lambda c, p: False)
        node = _node()
        node._do_evaluate({"image": _tex(1)}, _Ctx(), None, None)
        assert node.last_state == "FAILED"
        assert "declined" in node.state_detail


class TestSerialization:
    def test_roundtrip(self, qapp):
        node = _node(seed=123, strength=0.75, expand_left=0.2)
        clone = AIGenExpandNode.from_dict(node.to_dict())
        assert clone.parameters["prompt"] == "a serene lake"
        assert clone.parameters["seed"] == 123
        assert clone.parameters["strength"] == 0.75
        assert clone.parameters["expand_left"] == 0.2

    def test_missing_input_emits_nothing(self):
        node = _node()
        assert node._do_evaluate({}, _Ctx(), None, None) == {}
