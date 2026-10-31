"""C1 cloud generation job tests — request assembly, caching, state
machine, NEEDS_CONFIG degradation and the once-per-batch cost gate.  The
transport is a stub: no network, no model downloads (约束 12).
"""

import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from src.shared.ai_models.generation.image_gen_job import (
    DONE,
    FAILED,
    NEEDS_CONFIG,
    CloudImageGenJob,
    border_expand_mask,
    cache_key,
    expand_request_params,
)
from src.features.composite.render.disk_cache_store import DiskCacheStore

pytestmark = pytest.mark.composite_core


def _image(h=8, w=12, seed=0):
    rng = np.random.default_rng(seed)
    return rng.random((h, w, 4)).astype(np.float32)


class TestBorderMask:
    def test_uniform_border(self):
        mask = border_expand_mask(100, 50, 0.1, 0.2, 0.1, 0.2)
        assert mask.shape == (50, 100)
        assert mask[:, :10].sum() == 10 * 50       # left strip
        assert mask[:, 90:].sum() == 10 * 50       # right strip
        assert mask[:10, :].sum() == 10 * 100      # top strip
        assert mask[40:, :].sum() == 10 * 100      # bottom strip
        assert mask[20:30, 40:60].sum() == 0       # centre untouched

    def test_zero_edges_all_zero(self):
        mask = border_expand_mask(10, 10, 0.0, 0.0, 0.0, 0.0)
        assert mask.sum() == 0

    def test_cache_key_changes_with_inputs(self):
        k1 = cache_key("p", "", "m", 1, 1.0, b"img")
        assert cache_key("p", "", "m", 1, 1.0, b"img") == k1
        assert cache_key("p2", "", "m", 1, 1.0, b"img") != k1
        assert cache_key("p", "", "m", 2, 1.0, b"img") != k1
        assert cache_key("p", "", "m", 1, 1.0, b"img2") != k1
        assert cache_key("p", "", "m", 1, 1.0, b"img", b"mask") != k1


class TestStateMachine:
    def _job(self, tmp_store=None, transport=None, api_key="key",
             cost_confirm=None, image=None):
        return CloudImageGenJob(
            prompt="a cat",
            image=image if image is not None else _image(),
            model="seedream", seed=7,
            cache_store=tmp_store, transport=transport,
            api_key_provider=(lambda: api_key) if api_key is not None
            else None,
            cost_confirm=cost_confirm)

    def test_success_transitions_full_chain(self, tmp_path):
        store = DiskCacheStore(root_dir=str(tmp_path), capacity_gb=1.0)
        calls = []

        def transport(request, key, size):
            calls.append((request["prompt"], key, size))
            return _image(seed=9)

        job = self._job(store, transport)
        state, image = job.run(count=3)
        assert state == DONE
        assert image is not None
        assert job.state.history == ["QUEUED", "UPLOADING", "RUNNING",
                                     "DOWNLOADING", "DONE"]
        assert calls and calls[0][0] == "a cat"
        assert calls[0][1] == "key"

    def test_cache_hit_skips_transport_and_cost_gate(self, tmp_path):
        store = DiskCacheStore(root_dir=str(tmp_path), capacity_gb=1.0)
        transport_calls = []
        gate_calls = []

        def transport(request, key, size):
            transport_calls.append(1)
            return _image(seed=9)

        def gate(count, prompt):
            gate_calls.append(count)
            return True

        job1 = self._job(store, transport, cost_confirm=gate)
        state1, img1 = job1.run(count=3)
        assert state1 == DONE and len(transport_calls) == 1
        assert gate_calls == [3]  # once per batch, not per image

        # Second identical request: cache hit — no transport, no gate.
        job2 = self._job(store, transport, cost_confirm=gate)
        state2, img2 = job2.run(count=3)
        assert state2 == DONE
        assert len(transport_calls) == 1
        assert gate_calls == [3]
        np.testing.assert_allclose(img2, img1, atol=5e-3)

    def test_cache_invalidated_by_input_change(self, tmp_path):
        store = DiskCacheStore(root_dir=str(tmp_path), capacity_gb=1.0)
        transport_calls = []

        def transport(request, key, size):
            transport_calls.append(request["seed"])
            return _image(seed=int(request["seed"]) % 7)

        job1 = self._job(store, transport, image=_image(seed=1))
        assert job1.run()[0] == DONE
        # Different input image → different key → recompute.
        job2 = self._job(store, transport, image=_image(seed=2))
        assert job2.run()[0] == DONE
        assert len(transport_calls) == 2

    def test_needs_config_without_key(self):
        job = self._job(tmp_store=None, transport=lambda *a: _image(),
                        api_key="")
        state, image = job.run()
        assert state == NEEDS_CONFIG
        assert image is None
        assert job.state.state == NEEDS_CONFIG

    def test_cost_decline_aborts_before_transport(self):
        transport_calls = []

        def transport(request, key, size):
            transport_calls.append(1)
            return _image()

        job = self._job(tmp_store=None, transport=transport,
                        cost_confirm=lambda count, p: False)
        state, image = job.run(count=10)
        assert state == FAILED
        assert "declined" in job.state.detail
        assert transport_calls == []

    def test_transport_exception_maps_to_failed(self):
        def transport(request, key, size):
            raise RuntimeError("provider 429")

        job = self._job(tmp_store=None, transport=transport)
        state, image = job.run()
        assert state == FAILED
        assert "429" in job.state.detail


class TestExpandRequest:
    def test_request_assembly(self):
        request = expand_request_params(
            "extend the sky", "no clouds", "seedream", 42, 0.8,
            (0.25, 0.0, 0.25, 0.0), 200, 100)
        assert request["prompt"] == "extend the sky"
        assert request["negative_prompt"] == "no clouds"
        assert request["seed"] == 42
        assert request["strength"] == 0.8
        mask = request["mask"]
        assert mask.shape == (100, 200)
        assert mask[:, :50].sum() == 50 * 100    # left 25%
        assert mask[:, 150:].sum() == 50 * 100   # right 25%
        assert mask.sum() == 2 * 50 * 100        # no top/bottom


class TestKeyGovernance:
    def test_key_provider_controlled_not_stored(self, tmp_path):
        """约束 12: the job never persists or hardcodes the key."""
        store = DiskCacheStore(root_dir=str(tmp_path), capacity_gb=1.0)
        seen_keys = []

        def transport(request, key, size):
            seen_keys.append(key)
            return _image()

        job = CloudImageGenJob(
            prompt="p", image=_image(), cache_store=store,
            transport=transport,
            api_key_provider=lambda: "sk-secret-123")
        job.run()
        assert seen_keys == ["sk-secret-123"]
        # The cache entry carries no key material.
        import json
        meta = store.load_metadata("gen" + job.key)
        blob = json.dumps(meta)
        assert "sk-secret" not in blob
