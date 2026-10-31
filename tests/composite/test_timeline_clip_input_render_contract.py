"""Tests for timeline_clip_input render-side node creation contract.

Verifies that _sync_render_nodes() creates render-side instances for
timeline_clip_input nodes, syncing parameters and handling lifecycle
(create, update, remove) correctly.

The fix replaced the hardcoded tuple ("constant", "image_input", "video_frame")
with is_source_node_type() so that any source node type registered in the
capability table is automatically picked up.
"""
import pytest
from unittest.mock import MagicMock, patch

from src.features.composite.core.graph_snapshot import GraphSnapshot, NodeSnapshot
from src.features.composite.core.eval_context import CompositeEvalContext
from src.features.composite.core.node_capabilities import is_source_node_type
from src.features.composite.core.node_engine import NODE_REGISTRY
from src.features.composite.evaluators.base_evaluator import BaseEvaluatorMixin
from src.features.composite.nodes.base_node import TextureResult
from src.features.composite.nodes.timeline_clip_input_node import TimelineClipInputNode


# ── Helpers ──

def _make_evaluator():
    """Create a minimal BaseEvaluatorMixin with mocked dependencies."""
    evaluator = BaseEvaluatorMixin()
    evaluator._render_nodes = {}
    evaluator._invalidate_cache_for_node = MagicMock()
    return evaluator


def _make_snapshot(nodes):
    """Build a GraphSnapshot with the given node dicts {node_id: node_type}."""
    ns_map = {}
    for nid, ntype in nodes.items():
        ns_map[nid] = NodeSnapshot(
            node_id=nid,
            node_type=ntype,
            parameters={},
            disabled=False,
            input_ports=[],
            output_ports=[("result", "image")],
        )
    return GraphSnapshot(generation=1, nodes=ns_map)


def _make_snapshot_with_params(node_id, node_type, parameters):
    """Build a GraphSnapshot with a single node that has parameters."""
    ns = NodeSnapshot(
        node_id=node_id,
        node_type=node_type,
        parameters=dict(parameters),
        disabled=False,
        input_ports=[],
        output_ports=[("result", "image")],
    )
    return GraphSnapshot(generation=1, nodes={node_id: ns})


# ── Tests ──

class TestTimelineClipInputRenderContract:
    """Contract tests for timeline_clip_input render-side node creation."""

    def test_is_source_node_type_recognizes_timeline_clip_input(self):
        """Capability system must classify timeline_clip_input as a source type."""
        assert is_source_node_type("timeline_clip_input"), (
            "timeline_clip_input must be registered as action_type='source' "
            "in the capability table"
        )

    def test_node_registry_has_timeline_clip_input(self):
        """NODE_REGISTRY must contain timeline_clip_input."""
        assert "timeline_clip_input" in NODE_REGISTRY, (
            "timeline_clip_input must be registered in NODE_REGISTRY"
        )

    def test_sync_creates_render_node(self):
        """_sync_render_nodes must create a render-side instance for timeline_clip_input."""
        evaluator = _make_evaluator()
        snapshot = _make_snapshot({"tc_1": "timeline_clip_input"})

        evaluator._sync_render_nodes(snapshot)

        assert "tc_1" in evaluator._render_nodes
        node = evaluator._render_nodes["tc_1"]
        assert node.node_type == "timeline_clip_input"

    def test_sync_sets_correct_node_type(self):
        """The created render node must have node_type='timeline_clip_input'."""
        evaluator = _make_evaluator()
        snapshot = _make_snapshot({"my_clip": "timeline_clip_input"})

        evaluator._sync_render_nodes(snapshot)

        node = evaluator._render_nodes["my_clip"]
        assert node.node_type == "timeline_clip_input"

    def test_sync_creates_multiple_source_types(self):
        """All source types should get render nodes, not just timeline_clip_input."""
        evaluator = _make_evaluator()
        snapshot = _make_snapshot({
            "const_1": "constant",
            "img_1": "image_input",
            "vid_1": "video_frame",
            "tc_1": "timeline_clip_input",
        })

        evaluator._sync_render_nodes(snapshot)

        assert "const_1" in evaluator._render_nodes
        assert "img_1" in evaluator._render_nodes
        assert "vid_1" in evaluator._render_nodes
        assert "tc_1" in evaluator._render_nodes

    def test_sync_syncs_parameters(self):
        """Parameters from the snapshot must be synced to the render-side node."""
        evaluator = _make_evaluator()
        snapshot = _make_snapshot_with_params(
            "tc_1", "timeline_clip_input",
            {"timeline_id": "tl_abc", "clip_id": "clip_123", "asset_id": "a_42"},
        )

        evaluator._sync_render_nodes(snapshot)

        node = evaluator._render_nodes["tc_1"]
        assert node.parameters["timeline_id"] == "tl_abc"
        assert node.parameters["clip_id"] == "clip_123"
        assert node.parameters["asset_id"] == "a_42"

    def test_sync_updates_existing_parameters(self):
        """Calling _sync_render_nodes again with changed params must update the node."""
        evaluator = _make_evaluator()

        # First sync
        snap1 = _make_snapshot_with_params(
            "tc_1", "timeline_clip_input",
            {"timeline_id": "tl_old", "clip_id": "clip_old"},
        )
        evaluator._sync_render_nodes(snap1)
        assert evaluator._render_nodes["tc_1"].parameters["timeline_id"] == "tl_old"

        # Second sync with updated params
        snap2 = _make_snapshot_with_params(
            "tc_1", "timeline_clip_input",
            {"timeline_id": "tl_new", "clip_id": "clip_new"},
        )
        evaluator._sync_render_nodes(snap2)
        assert evaluator._render_nodes["tc_1"].parameters["timeline_id"] == "tl_new"
        assert evaluator._render_nodes["tc_1"].parameters["clip_id"] == "clip_new"

    def test_sync_removes_stale_node(self):
        """Nodes no longer in the snapshot must be removed from _render_nodes."""
        evaluator = _make_evaluator()

        # Create node
        snap1 = _make_snapshot({"tc_1": "timeline_clip_input"})
        evaluator._sync_render_nodes(snap1)
        assert "tc_1" in evaluator._render_nodes

        # Remove node from snapshot
        snap2 = _make_snapshot({})
        evaluator._sync_render_nodes(snap2)
        assert "tc_1" not in evaluator._render_nodes

    def test_sync_does_not_duplicate_color_input(self):
        """color_input is handled separately and must not be double-created."""
        evaluator = _make_evaluator()
        snapshot = _make_snapshot({
            "color_1": "color_input",
            "tc_1": "timeline_clip_input",
        })
        # color_input needs set_color_result; AUD-48006 后快照冻结，
        # 字段经 dataclasses.replace 派生新快照
        import dataclasses
        snapshot = dataclasses.replace(
            snapshot,
            color_texture_id=42,
            color_width=1920,
            color_height=1080,
            color_revision=1,
        )

        evaluator._sync_render_nodes(snapshot)

        # color_input should be created via its own special path
        assert "color_1" in evaluator._render_nodes
        assert evaluator._render_nodes["color_1"].node_type == "color_input"
        # timeline_clip_input should also be created
        assert "tc_1" in evaluator._render_nodes
        assert evaluator._render_nodes["tc_1"].node_type == "timeline_clip_input"

    def test_output_node_still_created(self):
        """Output nodes must still be created after the source-node fix."""
        evaluator = _make_evaluator()
        snapshot = _make_snapshot({
            "tc_1": "timeline_clip_input",
            "out_1": "output",
        })

        evaluator._sync_render_nodes(snapshot)

        assert "tc_1" in evaluator._render_nodes
        assert "out_1" in evaluator._render_nodes
        assert evaluator._render_nodes["out_1"].node_type == "output"

    def test_effect_node_not_in_render_nodes(self):
        """Effect nodes (like blur) should NOT be in _render_nodes (they are ephemeral)."""
        evaluator = _make_evaluator()
        snapshot = _make_snapshot({
            "tc_1": "timeline_clip_input",
            "blur_1": "blur",
        })

        evaluator._sync_render_nodes(snapshot)

        assert "tc_1" in evaluator._render_nodes
        assert "blur_1" not in evaluator._render_nodes

    def test_cleanup_called_on_removal(self):
        """cleanup() must be called when a timeline_clip_input node is removed."""
        evaluator = _make_evaluator()

        snap1 = _make_snapshot({"tc_1": "timeline_clip_input"})
        evaluator._sync_render_nodes(snap1)
        node = evaluator._render_nodes["tc_1"]
        node.cleanup = MagicMock()

        snap2 = _make_snapshot({})
        evaluator._sync_render_nodes(snap2)

        node.cleanup.assert_called_once()

    def test_cache_invalidated_on_removal(self):
        """_invalidate_cache_for_node must be called when a node is removed."""
        evaluator = _make_evaluator()

        snap1 = _make_snapshot({"tc_1": "timeline_clip_input"})
        evaluator._sync_render_nodes(snap1)

        snap2 = _make_snapshot({})
        evaluator._sync_render_nodes(snap2)

        evaluator._invalidate_cache_for_node.assert_called_with("tc_1")

    def test_runtime_provider_failure_does_not_return_cached_previous_texture(self):
        """Runtime timeline input must not show a cached texture from a previous clip."""
        node = TimelineClipInputNode("tc_1")
        node.set_cached_texture(TextureResult(texture_id=77, width=16, height=16))

        provider = MagicMock()
        provider.get_timeline_clip_texture.return_value = None
        provider.set_gl_context = MagicMock()
        ctx = CompositeEvalContext(
            gl=MagicMock(),
            runtime_context=MagicMock(),
            timeline_clip_gpu_provider=provider,
        )

        result = node._do_evaluate({}, ctx, fb_manager=MagicMock())

        assert result == {}
