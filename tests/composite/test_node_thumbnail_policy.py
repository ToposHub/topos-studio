"""
Tests for node thumbnail throttling policy.

Covers:
- Policy defaults
- Throttle timing (idle vs interaction)
- Max nodes per tick
- Selected-first priority
- Playback suspension
- Cache key skip
- Interaction: only selected node
- Disabled policy
"""

import sys
import os
import time
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from src.features.composite.render.thumbnail_policy import (
    CompositeThumbnailPolicy,
    ThumbnailThrottleState,
    compute_thumbnail_requests,
)


# ── Helpers ─────────────────────────────────────────────────────────────

def _default_state() -> ThumbnailThrottleState:
    """Return a throttle state with last_request_at = 0 (long ago)."""
    return ThumbnailThrottleState(last_request_at=0.0)


CACHE_KEYS_ALL = {
    "sel": "key_sel_v1",
    "vis_a": "key_a_v1",
    "vis_b": "key_b_v1",
    "vis_c": "key_c_v1",
}


# ── Policy defaults ─────────────────────────────────────────────────────

class TestCompositeThumbnailPolicy:
    """Test policy dataclass defaults."""

    def test_default_values(self):
        p = CompositeThumbnailPolicy()
        assert p.enabled is True
        assert p.update_during_playback is False
        assert p.max_updates_per_second_idle == 10
        assert p.max_updates_per_second_interaction == 5
        assert p.max_nodes_per_tick == 4
        assert p.selected_first is True

    def test_custom_values(self):
        p = CompositeThumbnailPolicy(
            enabled=False,
            update_during_playback=True,
            max_updates_per_second_idle=2,
            max_updates_per_second_interaction=1,
            max_nodes_per_tick=1,
            selected_first=False,
        )
        assert p.enabled is False
        assert p.update_during_playback is True
        assert p.max_updates_per_second_idle == 2
        assert p.max_updates_per_second_interaction == 1
        assert p.max_nodes_per_tick == 1
        assert p.selected_first is False


class TestThumbnailThrottleState:
    """Test state dataclass defaults."""

    def test_defaults(self):
        s = ThumbnailThrottleState()
        assert s.last_request_at == 0.0
        assert s.pending_nodes == set()
        assert s.playback_suspended is False
        assert s.last_cache_key == {}


# ── Throttle timing ────────────────────────────────────────────────────

class TestThrottleTiming:
    """Test that throttle intervals are respected for idle and interaction."""

    def test_idle_allows_request(self):
        result = compute_thumbnail_requests(
            CompositeThumbnailPolicy(),
            _default_state(),
            is_interacting=False,
            render_pending=False,
            visible_node_ids=["vis_a"],
            selected_node_id="sel",
            renderer_cache_keys=CACHE_KEYS_ALL,
            now=100.0,
        )
        assert len(result) > 0

    def test_idle_blocks_too_frequent(self):
        now = time.monotonic()
        state = ThumbnailThrottleState(last_request_at=now)
        result = compute_thumbnail_requests(
            CompositeThumbnailPolicy(),
            state,
            is_interacting=False,
            render_pending=False,
            visible_node_ids=["vis_a"],
            selected_node_id="sel",
            renderer_cache_keys=CACHE_KEYS_ALL,
            now=now + 0.01,  # only 10 ms elapsed (idle interval = 100 ms)
        )
        assert result == []

    def test_interaction_allows_after_interval(self):
        result = compute_thumbnail_requests(
            CompositeThumbnailPolicy(),
            _default_state(),
            is_interacting=True,
            render_pending=False,
            visible_node_ids=["vis_a"],
            selected_node_id="sel",
            renderer_cache_keys=CACHE_KEYS_ALL,
            now=100.0,
        )
        assert result == ["sel"]

    def test_interaction_blocks_too_frequent(self):
        now = time.monotonic()
        state = ThumbnailThrottleState(last_request_at=now)
        result = compute_thumbnail_requests(
            CompositeThumbnailPolicy(),
            state,
            is_interacting=True,
            render_pending=False,
            visible_node_ids=["vis_a"],
            selected_node_id="sel",
            renderer_cache_keys=CACHE_KEYS_ALL,
            now=now + 0.01,  # 10 ms (interaction interval = 200 ms)
        )
        assert result == []


# ── Max nodes per tick ─────────────────────────────────────────────────

class TestMaxNodesPerTick:
    """Test that only max_nodes_per_tick nodes are requested per tick."""

    def test_respects_max_nodes(self):
        policy = CompositeThumbnailPolicy(max_nodes_per_tick=2)
        result = compute_thumbnail_requests(
            policy,
            _default_state(),
            is_interacting=False,
            render_pending=False,
            visible_node_ids=["vis_a", "vis_b", "vis_c"],
            selected_node_id="sel",
            renderer_cache_keys=CACHE_KEYS_ALL,
            now=100.0,
        )
        assert len(result) == 2

    def test_all_nodes_when_limit_high(self):
        policy = CompositeThumbnailPolicy(max_nodes_per_tick=10)
        result = compute_thumbnail_requests(
            policy,
            _default_state(),
            is_interacting=False,
            render_pending=False,
            visible_node_ids=["vis_a", "vis_b", "vis_c"],
            selected_node_id="sel",
            renderer_cache_keys=CACHE_KEYS_ALL,
            now=100.0,
        )
        # sel + vis_a + vis_b + vis_c = 4
        assert len(result) == 4


# ── Selected-first priority ────────────────────────────────────────────

class TestSelectedFirstPriority:
    """Test that the selected node is always requested first."""

    def test_selected_node_is_first(self):
        policy = CompositeThumbnailPolicy(max_nodes_per_tick=1)
        result = compute_thumbnail_requests(
            policy,
            _default_state(),
            is_interacting=False,
            render_pending=False,
            visible_node_ids=["vis_a", "vis_b"],
            selected_node_id="sel",
            renderer_cache_keys=CACHE_KEYS_ALL,
            now=100.0,
        )
        assert result == ["sel"]


# ── Playback suspension ────────────────────────────────────────────────

class TestPlaybackSuspension:
    """Test that thumbnail requests are suspended during playback."""

    def test_playback_suspended_skips(self):
        state = ThumbnailThrottleState(playback_suspended=True)
        result = compute_thumbnail_requests(
            CompositeThumbnailPolicy(),
            state,
            is_interacting=False,
            render_pending=False,
            visible_node_ids=["vis_a"],
            selected_node_id="sel",
            renderer_cache_keys=CACHE_KEYS_ALL,
            now=100.0,
        )
        assert result == []

    def test_playback_allowed_when_policy_allows(self):
        state = ThumbnailThrottleState(playback_suspended=True)
        policy = CompositeThumbnailPolicy(update_during_playback=True)
        result = compute_thumbnail_requests(
            policy,
            state,
            is_interacting=False,
            render_pending=False,
            visible_node_ids=["vis_a"],
            selected_node_id="sel",
            renderer_cache_keys=CACHE_KEYS_ALL,
            now=100.0,
        )
        assert len(result) > 0


# ── Cache key skip ─────────────────────────────────────────────────────

class TestCacheKeySkip:
    """Test that nodes with unchanged cache keys are skipped."""

    def test_unchanged_cache_key_skips_all(self):
        state = _default_state()
        state.last_cache_key = dict(CACHE_KEYS_ALL)  # all match
        result = compute_thumbnail_requests(
            CompositeThumbnailPolicy(),
            state,
            is_interacting=False,
            render_pending=False,
            visible_node_ids=["vis_a", "vis_b", "vis_c"],
            selected_node_id="sel",
            renderer_cache_keys=CACHE_KEYS_ALL,
            now=100.0,
        )
        assert result == []

    def test_changed_cache_key_requests_node(self):
        state = _default_state()
        state.last_cache_key = {
            "sel": "key_sel_v0",  # changed
            "vis_a": "key_a_v1",
            "vis_b": "key_b_v1",
            "vis_c": "key_c_v1",
        }
        result = compute_thumbnail_requests(
            CompositeThumbnailPolicy(),
            state,
            is_interacting=False,
            render_pending=False,
            visible_node_ids=["vis_a", "vis_b", "vis_c"],
            selected_node_id="sel",
            renderer_cache_keys=CACHE_KEYS_ALL,
            now=100.0,
        )
        assert result == ["sel"]

    def test_no_old_cache_key_requests_node(self):
        """Nodes that have no recorded old key should be requested."""
        state = _default_state()
        state.last_cache_key = {}  # nothing recorded yet
        result = compute_thumbnail_requests(
            CompositeThumbnailPolicy(),
            state,
            is_interacting=False,
            render_pending=False,
            visible_node_ids=["vis_a"],
            selected_node_id="sel",
            renderer_cache_keys=CACHE_KEYS_ALL,
            now=100.0,
        )
        assert "sel" in result
        assert "vis_a" in result


# ── Disabled policy ────────────────────────────────────────────────────

class TestDisabledPolicy:
    """Test that disabled policy prevents any requests."""

    def test_disabled_skips_all(self):
        policy = CompositeThumbnailPolicy(enabled=False)
        result = compute_thumbnail_requests(
            policy,
            _default_state(),
            is_interacting=False,
            render_pending=False,
            visible_node_ids=["vis_a"],
            selected_node_id="sel",
            renderer_cache_keys=CACHE_KEYS_ALL,
            now=100.0,
        )
        assert result == []


# ── Interaction: only selected ─────────────────────────────────────────

class TestInteractionOnlySelected:
    """Test that during interaction, only the selected node is requested."""

    def test_interaction_only_requests_selected(self):
        policy = CompositeThumbnailPolicy(max_nodes_per_tick=10)
        result = compute_thumbnail_requests(
            policy,
            _default_state(),
            is_interacting=True,
            render_pending=False,
            visible_node_ids=["vis_a", "vis_b", "vis_c"],
            selected_node_id="sel",
            renderer_cache_keys=CACHE_KEYS_ALL,
            now=100.0,
        )
        assert result == ["sel"]

    def test_interaction_no_selected_returns_empty(self):
        result = compute_thumbnail_requests(
            CompositeThumbnailPolicy(),
            _default_state(),
            is_interacting=True,
            render_pending=False,
            visible_node_ids=["vis_a", "vis_b"],
            selected_node_id=None,
            renderer_cache_keys=CACHE_KEYS_ALL,
            now=100.0,
        )
        assert result == []


# ── Render pending priority ────────────────────────────────────────────

class TestRenderPending:
    """Test that thumbnail requests are deferred when a render is pending."""

    def test_render_pending_blocks(self):
        result = compute_thumbnail_requests(
            CompositeThumbnailPolicy(),
            _default_state(),
            is_interacting=False,
            render_pending=True,
            visible_node_ids=["vis_a"],
            selected_node_id="sel",
            renderer_cache_keys=CACHE_KEYS_ALL,
            now=100.0,
        )
        assert result == []

    def test_render_not_pending_allows(self):
        result = compute_thumbnail_requests(
            CompositeThumbnailPolicy(),
            _default_state(),
            is_interacting=False,
            render_pending=False,
            visible_node_ids=["vis_a"],
            selected_node_id="sel",
            renderer_cache_keys=CACHE_KEYS_ALL,
            now=100.0,
        )
        assert len(result) > 0
