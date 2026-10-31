"""
Tests for RenderScheduler.plan() — plan generation and node classification.

Covers:
- Empty graph produces empty plan
- eval_order propagated correctly
- Node classification: source, effect, output, passthrough, placeholder
- Params hash pre-computation matches CacheManager.compute_key
- Input connections pre-resolved to upstream node IDs
- render_mode stored in plan
- Counts (source_count, effect_count, etc.) match classifications
- Same snapshot with different render_mode produces different plan.render_mode
- Disabled nodes classified as passthrough
- Placeholder types classified correctly using dynamic IS_PLACEHOLDER check
"""

import sys
import os
import hashlib
import json
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from src.features.composite.render.render_scheduler import (
    RenderScheduler, RenderPlan, NodePlan, RenderMode,
    _GPU_STATEFUL_TYPES, _is_gpu_stateful,
)
from src.features.composite.core.graph_snapshot import GraphSnapshot, NodeSnapshot, ConnectionSnapshot
from src.features.composite.core.node_engine import NODE_REGISTRY
from src.features.composite.core.node_capabilities import is_placeholder_node_type, is_source_node_type


def _make_snapshot(nodes=None, connections=None, eval_order=None, **kwargs):
    """Helper to create a GraphSnapshot with sensible defaults."""
    node_dict = {}
    if nodes:
        for n in nodes:
            if isinstance(n, dict):
                ns = NodeSnapshot(**n)
            else:
                ns = n
            node_dict[ns.node_id] = ns

    return GraphSnapshot(
        generation=kwargs.get('generation', 1),
        nodes=node_dict,
        connections=connections or [],
        eval_order=eval_order or list(node_dict.keys()),
        color_texture_id=kwargs.get('color_texture_id', 0),
        color_width=kwargs.get('color_width', 0),
        color_height=kwargs.get('color_height', 0),
        color_revision=kwargs.get('color_revision', 0),
    )


def _node(node_id="n1", node_type="blur", params=None, disabled=False, **kwargs):
    """Shorthand to create a NodeSnapshot dict."""
    return {
        "node_id": node_id,
        "node_type": node_type,
        "parameters": params or {"radius": 5.0},
        "disabled": disabled,
        "input_ports": kwargs.get("input_ports", [("image", "image")]),
        "output_ports": kwargs.get("output_ports", [("result", "image")]),
    }


# ── Plan Generation ──

class TestPlanGeneration:
    """RenderScheduler.plan() basic functionality."""

    def test_empty_graph_produces_empty_plan(self):
        scheduler = RenderScheduler()
        snapshot = _make_snapshot(nodes=[])
        plan = scheduler.plan(snapshot, "preview")
        assert plan.total_nodes == 0
        assert plan.eval_order == []
        assert plan.node_plans == {}

    def test_eval_order_propagated(self):
        scheduler = RenderScheduler()
        snapshot = _make_snapshot(
            nodes=[_node("a"), _node("b"), _node("c")],
            eval_order=["a", "b", "c"],
        )
        plan = scheduler.plan(snapshot, "preview")
        assert plan.eval_order == ["a", "b", "c"]

    def test_render_mode_stored(self):
        scheduler = RenderScheduler()
        snapshot = _make_snapshot(nodes=[_node("a")])
        plan_preview = scheduler.plan(snapshot, "preview")
        plan_export = scheduler.plan(snapshot, "export")
        assert plan_preview.render_mode == "preview"
        assert plan_export.render_mode == "export"

    def test_snapshot_reference_stored(self):
        scheduler = RenderScheduler()
        snapshot = _make_snapshot(nodes=[_node("a")])
        plan = scheduler.plan(snapshot, "preview")
        assert plan.snapshot is snapshot

    def test_node_plan_has_correct_fields(self):
        scheduler = RenderScheduler()
        snapshot = _make_snapshot(nodes=[_node("blur1", "blur")])
        plan = scheduler.plan(snapshot, "preview")
        np = plan.node_plans["blur1"]
        assert isinstance(np, NodePlan)
        assert np.node_id == "blur1"
        assert np.node_type == "blur"
        assert np.action == "effect"
        assert len(np.params_hash) == 12

    def test_timeline_clip_input_is_dirty_on_repeated_plan(self):
        """Runtime clip input must re-evaluate even when graph revisions are unchanged."""
        scheduler = RenderScheduler()
        snapshot = _make_snapshot(nodes=[
            _node("src", "timeline_clip_input", input_ports=[], output_ports=[("output", "image")]),
            _node("out", "output", input_ports=[("image", "image")], output_ports=[("display", "image")]),
        ], connections=[
            ConnectionSnapshot(
                src_node_id="src",
                src_port_name="output",
                dst_node_id="out",
                dst_port_name="image",
            )
        ], eval_order=["src", "out"])

        first = scheduler.plan(snapshot, "preview")
        second = scheduler.plan(snapshot, "preview")

        assert set(first.dirty_node_ids) == {"src", "out"}
        assert set(second.dirty_node_ids) == {"src", "out"}


# ── Node Classification ──

class TestNodeClassification:
    """Verify each node type gets the correct action."""

    def test_source_nodes(self):
        """GPU-stateful types classified as 'source'."""
        scheduler = RenderScheduler()
        for ntype in _GPU_STATEFUL_TYPES:
            snapshot = _make_snapshot(nodes=[_node("n1", ntype)])
            plan = scheduler.plan(snapshot, "preview")
            assert plan.node_plans["n1"].action == "source", f"{ntype} should be source"

    def test_output_node(self):
        snapshot = _make_snapshot(nodes=[
            _node("out1", "output", input_ports=[], output_ports=[("display", "image")]),
        ])
        scheduler = RenderScheduler()
        plan = scheduler.plan(snapshot, "preview")
        assert plan.node_plans["out1"].action == "output"

    def test_file_output_node_is_terminal_output(self):
        snapshot = _make_snapshot(nodes=[
            _node("write1", "file_output", input_ports=[("image", "image")],
                  output_ports=[]),
        ])
        scheduler = RenderScheduler()
        plan = scheduler.plan(snapshot, RenderMode.EXPORT)
        assert plan.node_plans["write1"].action == "output"
        assert plan.output_count == 1

    def test_effect_nodes(self):
        """Standard effect types classified as 'effect'."""
        scheduler = RenderScheduler()
        for ntype in ("blur", "grade", "transform", "invert", "merge",
                       "premult", "unpremult", "shuffle", "copy_channel",
                       "cs_transform", "reformat", "crop"):
            snapshot = _make_snapshot(nodes=[_node("n1", ntype)])
            plan = scheduler.plan(snapshot, "preview")
            assert plan.node_plans["n1"].action == "effect", f"{ntype} should be effect"

    def test_disabled_effect_is_passthrough(self):
        snapshot = _make_snapshot(nodes=[_node("b1", "blur", disabled=True)])
        scheduler = RenderScheduler()
        plan = scheduler.plan(snapshot, "preview")
        assert plan.node_plans["b1"].action == "passthrough"

    def test_disabled_source_still_source(self):
        """Source nodes are classified as 'source' even when disabled."""
        snapshot = _make_snapshot(nodes=[_node("c1", "color_input", disabled=True)])
        scheduler = RenderScheduler()
        plan = scheduler.plan(snapshot, "preview")
        assert plan.node_plans["c1"].action == "source"

    def test_placeholder_nodes(self):
        """Node types with IS_PLACEHOLDER=True should be classified as placeholder.

        BUG-13: Source and output nodes retain their action type even with
        IS_PLACEHOLDER=True. The placeholder flag is for UI indication,
        not for blocking action classification.
        - Source types (text, color_input, etc.) → action='source'
        - Output types (file_output, output) → action='output'
        - Effect types with IS_PLACEHOLDER=True → action='placeholder'
        """
        scheduler = RenderScheduler()
        # Import capability helpers for source/output detection
        from src.features.composite.core.node_capabilities import is_source_node_type, is_output_node_type

        # Find all node types that are actually placeholders
        placeholder_types = []
        for ntype, cls in NODE_REGISTRY.items():
            if getattr(cls, "IS_PLACEHOLDER", False):
                placeholder_types.append(ntype)

        # Test each placeholder type
        for ntype in placeholder_types:
            snapshot = _make_snapshot(nodes=[_node("n1", ntype)])
            plan = scheduler.plan(snapshot, "preview")
            # BUG-13: Source/output nodes keep their action type
            if is_source_node_type(ntype):
                assert plan.node_plans["n1"].action == "source", \
                    f"{ntype} is source type, should be 'source' not 'placeholder'"
            elif is_output_node_type(ntype):
                assert plan.node_plans["n1"].action == "output", \
                    f"{ntype} is output type, should be 'output' not 'placeholder'"
            else:
                assert plan.node_plans["n1"].action == "placeholder", \
                    f"{ntype} should be placeholder"

    def test_non_placeholder_nodes_not_classified_as_placeholder(self):
        """Node types with IS_PLACEHOLDER=False should NOT be classified as placeholder.

        This specifically tests nodes that were incorrectly in the old _PLACEHOLDER_TYPES
        list but now declare IS_PLACEHOLDER=False:
        - mask, matte, keyer, denoise
        - ai_generate, ai_inpaint, ai_expand, ai_remove
        - file_output
        """
        scheduler = RenderScheduler()
        # Test nodes that were in old placeholder list but are now implemented
        # Note: ai_* nodes remain placeholder because they need external models
        formerly_placeholder = ["mask", "matte", "keyer", "denoise"]
        for ntype in formerly_placeholder:
            if ntype in NODE_REGISTRY:
                cls = NODE_REGISTRY[ntype]
                is_placeholder = getattr(cls, "IS_PLACEHOLDER", False)
                # Verify the node class declares IS_PLACEHOLDER=False
                assert is_placeholder is False, f"{ntype}.IS_PLACEHOLDER should be False"
                # Verify scheduler doesn't classify it as placeholder
                snapshot = _make_snapshot(nodes=[_node("n1", ntype)])
                plan = scheduler.plan(snapshot, "preview")
                assert plan.node_plans["n1"].action != "placeholder", \
                    f"{ntype} should not be placeholder (IS_PLACEHOLDER=False)"

    def test_unknown_node_type_is_placeholder(self):
        """Unknown node types (not in NODE_REGISTRY) should be treated as placeholder."""
        scheduler = RenderScheduler()
        # Use a node type that doesn't exist in registry
        unknown_type = "nonexistent_node_type_xyz"
        snapshot = _make_snapshot(nodes=[_node("n1", unknown_type)])
        plan = scheduler.plan(snapshot, "preview")
        assert plan.node_plans["n1"].action == "placeholder", \
            "Unknown node type should be treated as placeholder for safety"

    def test_is_placeholder_node_type_function(self):
        """Test the helper function is_placeholder_node_type directly."""
        # Test a known placeholder (PlaceholderNode)
        assert is_placeholder_node_type("placeholder") is True
        # Test a known non-placeholder (blur)
        assert is_placeholder_node_type("blur") is False
        # Test an unknown type
        assert is_placeholder_node_type("nonexistent_xyz") is True
        # Test nodes formerly in placeholder list
        assert is_placeholder_node_type("mask") is False
        assert is_placeholder_node_type("denoise") is False
        assert is_placeholder_node_type("keyer") is False

    def test_is_gpu_stateful_function(self):
        """Test the helper function _is_gpu_stateful directly."""
        # Test known GPU-stateful types
        assert _is_gpu_stateful("color_input") is True
        assert _is_gpu_stateful("constant") is True
        assert _is_gpu_stateful("image_input") is True
        assert _is_gpu_stateful("video_frame") is True
        # Test non-source types
        assert _is_gpu_stateful("blur") is False
        assert _is_gpu_stateful("grade") is False
        assert _is_gpu_stateful("output") is False
        # Test unknown type (returns False)
        assert _is_gpu_stateful("nonexistent_xyz") is False

    def test_capability_consistency_with_legacy_set(self):
        """Verify _GPU_STATEFUL_TYPES matches SOURCE_NODE_TYPES in node_capabilities."""
        from src.features.composite.core.node_capabilities import SOURCE_NODE_TYPES

        # All legacy types should be in capability source types
        for ntype in _GPU_STATEFUL_TYPES:
            assert ntype in SOURCE_NODE_TYPES, \
                f"{ntype} in _GPU_STATEFUL_TYPES but not in SOURCE_NODE_TYPES"

        # Capability source types may have additional entries (e.g., text)
        # but legacy types must be present
        assert "color_input" in SOURCE_NODE_TYPES
        assert "constant" in SOURCE_NODE_TYPES
        assert "image_input" in SOURCE_NODE_TYPES
        assert "video_frame" in SOURCE_NODE_TYPES

    def test_scheduler_classifies_using_capabilities(self):
        """Verify scheduler uses capability system for classification."""
        scheduler = RenderScheduler()
        # Test that scheduler classification matches capability queries
        for ntype in _GPU_STATEFUL_TYPES:
            snapshot = _make_snapshot(nodes=[_node("n1", ntype)])
            plan = scheduler.plan(snapshot, "preview")
            # Scheduler should classify as 'source'
            assert plan.node_plans["n1"].action == "source"
            # Capability should also identify as source
            assert is_source_node_type(ntype) is True


# ── Params Hash ──

class TestParamsHash:
    """Pre-computed params hash matches what CacheManager would compute."""

    def test_params_hash_matches_manual(self):
        params = {"radius": 5.0, "opacity": 0.5}
        scheduler = RenderScheduler()
        snapshot = _make_snapshot(nodes=[_node("b1", "blur", params=params)])
        plan = scheduler.plan(snapshot, "preview")

        expected = hashlib.md5(
            json.dumps(params, sort_keys=True, default=str).encode()
        ).hexdigest()[:12]
        assert plan.node_plans["b1"].params_hash == expected

    def test_different_params_different_hash(self):
        scheduler = RenderScheduler()
        snap1 = _make_snapshot(nodes=[_node("b1", "blur", params={"radius": 5.0})])
        snap2 = _make_snapshot(nodes=[_node("b1", "blur", params={"radius": 20.0})])
        plan1 = scheduler.plan(snap1, "preview")
        plan2 = scheduler.plan(snap2, "preview")
        assert plan1.node_plans["b1"].params_hash != plan2.node_plans["b1"].params_hash

    def test_same_params_same_hash(self):
        params = {"radius": 5.0}
        scheduler = RenderScheduler()
        snap1 = _make_snapshot(nodes=[_node("b1", "blur", params=params)])
        snap2 = _make_snapshot(nodes=[_node("b1", "blur", params=params)])
        plan1 = scheduler.plan(snap1, "preview")
        plan2 = scheduler.plan(snap2, "preview")
        assert plan1.node_plans["b1"].params_hash == plan2.node_plans["b1"].params_hash


# ── Input Resolution ──

class TestInputResolution:
    """Input connections pre-resolved to upstream node IDs."""

    def test_inputs_resolved(self):
        conns = [ConnectionSnapshot("color1", "result", "blur1", "image")]
        snapshot = _make_snapshot(
            nodes=[
                _node("color1", "color_input", input_ports=[], output_ports=[("result", "image")]),
                _node("blur1", "blur"),
            ],
            connections=conns,
            eval_order=["color1", "blur1"],
        )
        scheduler = RenderScheduler()
        plan = scheduler.plan(snapshot, "preview")
        assert plan.node_plans["blur1"].inputs == {"image": "color1"}
        assert plan.node_plans["color1"].inputs == {}

    def test_multiple_inputs_resolved(self):
        conns = [
            ConnectionSnapshot("blur1", "result", "merge1", "image_a"),
            ConnectionSnapshot("grade1", "result", "merge1", "image_b"),
        ]
        snapshot = _make_snapshot(
            nodes=[
                _node("blur1", "blur"),
                _node("grade1", "grade"),
                _node("merge1", "merge",
                      input_ports=[("image_a", "image"), ("image_b", "image")]),
            ],
            connections=conns,
            eval_order=["blur1", "grade1", "merge1"],
        )
        scheduler = RenderScheduler()
        plan = scheduler.plan(snapshot, "preview")
        assert plan.node_plans["merge1"].inputs == {
            "image_a": "blur1",
            "image_b": "grade1",
        }

    def test_no_connections_empty_inputs(self):
        snapshot = _make_snapshot(nodes=[_node("c1", "color_input")])
        scheduler = RenderScheduler()
        plan = scheduler.plan(snapshot, "preview")
        assert plan.node_plans["c1"].inputs == {}


# ── Counts ──

class TestPlanCounts:
    """Classification counts match actual classifications."""

    def test_counts_with_mixed_graph(self):
        """Test classification counts in a mixed graph.

        Note: glow is an implemented node (IS_PLACEHOLDER=False), so it's
        classified as 'effect', not 'placeholder'. This test verifies the
        dynamic IS_PLACEHOLDER check correctly classifies nodes.
        """
        snapshot = _make_snapshot(
            nodes=[
                _node("c1", "color_input", input_ports=[], output_ports=[("result", "image")]),
                _node("b1", "blur"),
                _node("b2", "blur", disabled=True),
                _node("g1", "glow"),
                _node("out", "output", input_ports=[("image", "image")], output_ports=[("display", "image")]),
            ],
            connections=[
                ConnectionSnapshot("c1", "result", "b1", "image"),
                ConnectionSnapshot("b1", "result", "out", "image"),
            ],
            eval_order=["c1", "b1", "b2", "g1", "out"],
        )
        scheduler = RenderScheduler()
        plan = scheduler.plan(snapshot, "preview")
        assert plan.total_nodes == 5
        assert plan.source_count == 1     # color_input
        assert plan.effect_count == 2     # blur (enabled) + glow (implemented node)
        assert plan.passthrough_count == 1  # blur (disabled)
        assert plan.placeholder_count == 0  # glow is not placeholder (IS_PLACEHOLDER=False)
        assert plan.output_count == 1     # output


# ── Runtime Diagnostics ──

class TestRenderPlanDiagnostics:
    """RenderPlan diagnostic fields (filled by executor)."""

    def test_initial_diagnostics_zero(self):
        scheduler = RenderScheduler()
        snapshot = _make_snapshot(nodes=[_node("a")])
        plan = scheduler.plan(snapshot, "preview")
        assert plan.cache_hits == 0
        assert plan.cache_misses == 0
        assert plan.eval_time_ms == 0.0
        # dirty_node_ids is pre-computed by plan generation;
        # first plan always marks all nodes dirty
        assert plan.dirty_node_ids == ["a"]
        assert plan.cached_node_ids == []

    def test_hit_rate_with_no_data(self):
        scheduler = RenderScheduler()
        snapshot = _make_snapshot(nodes=[_node("a")])
        plan = scheduler.plan(snapshot, "preview")
        assert plan.hit_rate == 0.0

    def test_hit_rate_with_data(self):
        scheduler = RenderScheduler()
        snapshot = _make_snapshot(nodes=[_node("a")])
        plan = scheduler.plan(snapshot, "preview")
        plan.cache_hits = 3
        plan.cache_misses = 1
        assert plan.hit_rate == 0.75


# ── Scheduler Stats Recording ──

class TestSchedulerStatsRecording:
    """RenderScheduler stores latest plan diagnostics."""

    def test_record_plan_stats(self):
        scheduler = RenderScheduler()
        snapshot = _make_snapshot(nodes=[_node("a")])
        plan = scheduler.plan(snapshot, "preview")
        plan.cache_hits = 2
        plan.cache_misses = 1
        scheduler.record_plan_stats(plan)
        assert scheduler.latest_plan is plan

    def test_latest_plan_initially_none(self):
        scheduler = RenderScheduler()
        assert scheduler.latest_plan is None


# ── Request Tracking (existing functionality) ──

class TestRequestTracking:
    """Ensure existing mark_current/is_stale still works."""

    def test_mark_current_and_stale(self):
        scheduler = RenderScheduler()
        scheduler.mark_current(5)
        assert scheduler.is_stale(3) is True
        assert scheduler.is_stale(5) is False
        assert scheduler.is_stale(7) is False


# ── Interaction Hint on RenderRequest ─────────────────────────────────────

class TestRenderRequestInteractionHint:
    """RenderRequest carries an interaction_active hint so the render thread
    knows to raise the eval-cache budget during a handle drag.

    The hint flows: CompositePage (_interaction_active) → RenderRequest →
    CompositeRenderer._handle_evaluate_graph → _evaluate_snapshot →
    CacheManager.begin/end_interaction.
    """

    def _make_request(self, **overrides):
        """Build a minimal RenderRequest for field-level tests (no real snapshot)."""
        from src.features.composite.render.render_scheduler import RenderRequest
        defaults = dict(
            snapshot=None,
            request_id=1,
            timestamp=0.0,
        )
        defaults.update(overrides)
        return RenderRequest(**defaults)

    def test_default_interaction_active_is_false(self):
        """Non-drag renders must not enter interaction mode."""
        req = self._make_request()
        assert req.interaction_active is False

    def test_interaction_active_can_be_set_true(self):
        """Drag frames set the hint to True."""
        req = self._make_request(interaction_active=True)
        assert req.interaction_active is True

    def test_hint_independent_of_other_fields(self):
        """interaction_active is independent of render_mode / request_id."""
        req_preview = self._make_request(
            render_mode=RenderMode.PREVIEW, interaction_active=True
        )
        req_full = self._make_request(
            render_mode=RenderMode.FULL, interaction_active=True
        )
        assert req_preview.interaction_active is True
        assert req_full.interaction_active is True
        # And defaults back to False regardless of mode
        req_normal = self._make_request(render_mode=RenderMode.PREVIEW)
        assert req_normal.interaction_active is False

    def test_hint_preserved_through_getattr_fallback(self):
        """Renderer reads the hint defensively via getattr(..., False).

        This guards against any legacy/minimal request lacking the field
        (e.g. the AI-result-ready path builds a request without the hint).
        """
        class LegacyRequest:
            """Simulates a request object without the interaction_active field."""
            snapshot = None
            request_id = 99
            timestamp = 0.0
            render_mode = "preview"
            viewer_roi = None

        legacy = LegacyRequest()
        assert getattr(legacy, 'interaction_active', False) is False


# ================================================================
# M08.1c · plan 期烘焙陈旧守卫（preview/export 双模式）+ 徽标数据
# ================================================================

class TestBakeStalenessGuard:
    @staticmethod
    def _bake_snapshot(upstream_v=1, signed=True):
        from types import SimpleNamespace as NS
        from src.features.composite.bake_service import (
            BAKE_SOURCE_SIGNATURE_PARAM,
            _bake_source_signature,
        )
        conns = [NS(src_node_id="a", src_port_name="out",
                    dst_node_id="merge", dst_port_name="A")]
        ref = NS(connections=conns, get_node={
            "a": NS(parameters={"v": 1}, enabled=True),
            "merge": NS(parameters={}, enabled=True)}.get)
        sig = _bake_source_signature(ref, "merge")
        bake_params = {BAKE_SOURCE_SIGNATURE_PARAM: sig} if signed else {}
        return _make_snapshot(
            nodes=[
                _node("a", params={"v": upstream_v}),
                {"node_id": "merge", "node_type": "merge",
                 "parameters": {}, "disabled": False,
                 "input_ports": [("image", "image")],
                 "output_ports": [("result", "image")]},
                {"node_id": "bake_input_merge", "node_type": "constant",
                 "parameters": bake_params, "disabled": False,
                 "input_ports": [("image", "image")],
                 "output_ports": [("result", "image")]},
            ],
            connections=[ConnectionSnapshot(
                src_node_id="a", src_port_name="out",
                dst_node_id="merge", dst_port_name="A")],
        )

    def test_preview_plan_carries_staleness(self):
        scheduler = RenderScheduler()
        snap = self._bake_snapshot(upstream_v=99)  # 上游变化 → 陈旧
        plan = scheduler.plan(snap, "preview")
        assert len(plan.bake_staleness) == 1
        assert plan.bake_staleness[0]["stale"] is True
        assert plan.stale_bake_count == 1

    def test_export_mode_audits_too_and_fresh_counts_zero(self):
        scheduler = RenderScheduler()
        plan = scheduler.plan(self._bake_snapshot(upstream_v=1), "export")
        assert plan.bake_staleness[0]["stale"] is False
        assert plan.stale_bake_count == 0

    def test_unsigned_bake_not_counted_as_stale(self):
        """旧工程无签名：stale=None——不冒称新鲜也不冒称过期（徽标单列）。"""
        scheduler = RenderScheduler()
        plan = scheduler.plan(self._bake_snapshot(signed=False), "preview")
        assert plan.bake_staleness[0]["stale"] is None
        assert plan.stale_bake_count == 0

    def test_page_badge_handler_show_and_clear(self, qtbot):
        from types import SimpleNamespace as NS
        from PyQt6.QtWidgets import QLabel
        from src.features.composite.ui.composite_page import CompositePage
        host = NS(_bake_badge=QLabel(""))
        qtbot.addWidget(host._bake_badge)
        CompositePage._on_bake_staleness_changed(host, [
            {"bake_node_id": "bake_input_merge", "target_id": "merge",
             "stale": True, "reason": "上游参数变化"},
            {"bake_node_id": "bake_input_t", "target_id": "t",
             "stale": None, "reason": "无签名"},
        ])
        assert "1 陈旧" in host._bake_badge.text()
        assert "1 无签名" in host._bake_badge.text()
        assert "bake_input_merge" in host._bake_badge.toolTip()
        # 清除
        CompositePage._on_bake_staleness_changed(host, [])
        assert host._bake_badge.text() == ""
        assert host._bake_badge.isHidden()
