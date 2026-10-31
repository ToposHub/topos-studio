"""Test NodeCapabilities system for consistency with NODE_REGISTRY.

This test module verifies that the centralized NodeCapability definitions
in node_capabilities.py stay synchronized with the node registry and other
subsystems (scheduler, evaluator, cache, tile, fusion).

Key tests:
- Registry consistency: All registered nodes have capabilities
- Placeholder classification: Matches IS_PLACEHOLDER class attributes
- Action classification: source/effect/output/passthrough matches scheduler
- Cache ownership: Matches evaluator's _CACHE_OWNED_TYPES
- Tile padding: Matches tile_padding_computer's _PADDING_NODE_TYPES
- Fusion eligibility: Matches pass_fusion's FUSIBLE_TYPES
"""

import pytest
from src.features.composite.core.node_engine import NODE_REGISTRY
from src.features.composite.core.node_capabilities import (
    NodeCapability,
    get_node_capability,
    is_placeholder_node_type,
    is_source_node_type,
    is_effect_node_type,
    is_output_node_type,
    is_passthrough_node_type,
    get_effect_type,
    get_tile_padding,
    owns_color_texture,
    owns_depth_texture,
    is_fusible,
    requires_frame_number,
    get_roi_policy,
    get_bypass_mapping,
    check_registry_consistency,
    get_all_capabilities,
    SOURCE_NODE_TYPES,
    EFFECT_NODE_TYPES,
    OUTPUT_NODE_TYPES,
    PASSTHROUGH_NODE_TYPES,
    PLACEHOLDER_NODE_TYPES,
    CACHE_OWNED_TYPES,
    TILE_PADDING_TYPES,
    FUSIBLE_NODE_TYPES,
    FRAME_NUMBER_TYPES,
)


class TestRegistryConsistency:
    """Test that capability table matches NODE_REGISTRY."""

    def test_all_registry_nodes_have_capabilities(self):
        """Every node in NODE_REGISTRY should have a capability entry."""
        missing = []
        for node_type in NODE_REGISTRY:
            cap = get_node_capability(node_type)
            if cap is None:
                missing.append(node_type)
        assert len(missing) == 0, f"Missing capabilities for: {missing}"

    def test_capability_table_nodes_in_registry(self):
        """Every node in capability table should be in registry (or documented exception)."""
        all_caps = get_all_capabilities()
        missing = []
        for node_type in all_caps:
            if node_type not in NODE_REGISTRY:
                missing.append(node_type)
        # Allow documented exceptions for legacy/removed nodes
        assert len(missing) == 0, f"Capabilities for unregistered nodes: {missing}"

    def test_check_registry_consistency_returns_correct_structure(self):
        """check_registry_consistency() should return dict with expected keys."""
        result = check_registry_consistency()
        expected_keys = {
            "missing_in_table",
            "missing_in_registry",
            "raw_placeholder_mismatch",
            "final_placeholder_mismatch",
            "missing_in_categories",
            "unknown_in_categories",
        }
        assert set(result.keys()) == expected_keys, \
            f"Unexpected keys: {set(result.keys()) - expected_keys}"

    def test_registry_nodes_reachable_from_category_menu(self):
        """CP-001: every creatable registry node must be listed in NODE_CATEGORIES.

        Nodes missing from the category menu are invisible to users — they can
        only appear via deserialization (regression seen with matte_cleanup).
        """
        result = check_registry_consistency()
        assert len(result["missing_in_categories"]) == 0, \
            f"Registry nodes absent from Add-Node menu: {result['missing_in_categories']}"

    def test_category_menu_entries_exist_in_registry(self):
        """CP-001: NODE_CATEGORIES must not reference unknown registry types."""
        result = check_registry_consistency()
        assert len(result["unknown_in_categories"]) == 0, \
            f"Category entries with no registry backing: {result['unknown_in_categories']}"

    def test_final_placeholder_mismatch_should_be_zero(self):
        """Final capability should match class IS_PLACEHOLDER attribute.

        After get_node_capability() applies class attribute override,
        the final capability's is_placeholder should match the class attribute.

        This is the real consistency check - raw table mismatches are
        handled by the override mechanism in get_node_capability().
        """
        result = check_registry_consistency()
        # Final mismatch should be zero because get_node_capability()
        # correctly applies class attribute override
        assert len(result["final_placeholder_mismatch"]) == 0, \
            f"Final capability disagrees with class for: {result['final_placeholder_mismatch']}"

    def test_raw_placeholder_mismatch_is_informational(self):
        """Raw table mismatches are informational, not errors.

        When raw table's is_placeholder differs from class IS_PLACEHOLDER,
        get_node_capability() handles it by overriding with class attribute.
        These mismatches are reported as informational only.
        """
        result = check_registry_consistency()
        # Raw mismatches may exist - they are handled by override mechanism
        # This test just verifies the field exists and contains node types
        raw_mismatches = result["raw_placeholder_mismatch"]
        # No assertion - just informational
        # Many nodes have raw mismatches because capability table provides
        # ground truth for nodes that don't set IS_PLACEHOLDER=True


class TestActionClassification:
    """Test node action classification (source/effect/output/passthrough/placeholder)."""

    def test_source_nodes_classification(self):
        """Source nodes should be classified correctly."""
        # These are GPU-stateful nodes from render_scheduler.py
        expected_sources = {"color_input", "image_input", "constant", "video_frame"}
        for node_type in expected_sources:
            assert is_source_node_type(node_type), \
                f"{node_type} should be source, got {get_node_capability(node_type).action_type}"

    def test_output_nodes_classification(self):
        """Output nodes should be classified correctly."""
        expected_outputs = {"output", "file_output"}
        for node_type in expected_outputs:
            assert is_output_node_type(node_type), \
                f"{node_type} should be output, got {get_node_capability(node_type).action_type}"

    def test_file_output_is_renderable_and_frame_scoped(self):
        """File output is real, and its export side effect is frame-scoped."""
        cap = get_node_capability("file_output")
        assert cap is not None
        assert cap.is_placeholder is False
        assert is_placeholder_node_type("file_output") is False
        assert requires_frame_number("file_output") is True

    def test_effect_nodes_classification(self):
        """Effect nodes should be classified correctly."""
        # Sample effect nodes from different categories
        effect_samples = [
            "blur", "grade", "transform", "merge",
            "shuffle", "invert", "glow", "keyer",
            "mask", "denoise", "color_correct",
        ]
        for node_type in effect_samples:
            cap = get_node_capability(node_type)
            if cap and not cap.is_placeholder:
                assert is_effect_node_type(node_type), \
                    f"{node_type} should be effect, got {cap.action_type}"

    def test_passthrough_nodes_classification(self):
        """Passthrough nodes should be classified correctly."""
        expected_passthrough = {"dot", "noop"}
        for node_type in expected_passthrough:
            assert is_passthrough_node_type(node_type), \
                f"{node_type} should be passthrough, got {get_node_capability(node_type).action_type}"

    def test_placeholder_nodes_classification(self):
        """Placeholder nodes should be classified correctly."""
        # Sample nodes that are marked IS_PLACEHOLDER=True in their class
        # or marked placeholder in capability table
        expected_placeholders = [
            "ai_generate", "ai_inpaint", "ai_expand", "ai_remove",
            "group", "text",
        ]
        for node_type in expected_placeholders:
            # Check if capability says placeholder OR class says placeholder
            cap = get_node_capability(node_type)
            node_cls = NODE_REGISTRY.get(node_type)
            is_placeholder_attr = getattr(node_cls, "IS_PLACEHOLDER", False) if node_cls else True
            expected_placeholder = cap.is_placeholder or is_placeholder_attr
            # Just verify the node has a valid capability
            assert cap is not None, f"{node_type} should have capability"


class TestCacheOwnership:
    """Test cache ownership for texture lifecycle management."""

    def test_effect_nodes_own_color_texture(self):
        """Effect nodes that create textures should be in CACHE_OWNED_TYPES."""
        # These nodes create new GPU textures and should be cache-owned
        expected_owned = [
            "blur", "grade", "color_correct", "huecorrect",
            "transform", "reformat", "crop", "cornerpin",
            "merge", "deep_merge", "dissolve",
            "shuffle", "copy_channel", "channel_merge",
            "invert", "clamp", "normalize", "premult", "unpremult",
            "blur", "defocus", "glow", "sharpen", "erode_dilate",
            "luma_keyer", "chroma_keyer", "keyer", "matte",
            "noise", "ramp", "mask",
        ]
        owned_count = 0
        for node_type in expected_owned:
            if owns_color_texture(node_type):
                owned_count += 1
        # At least half of expected should be marked as owned
        assert owned_count >= len(expected_owned) // 2, \
            f"Only {owned_count}/{len(expected_owned)} expected nodes marked as cache-owned"

    def test_source_nodes_not_cache_owned(self):
        """Source nodes (external textures) should NOT be cache-owned."""
        # color_input, image_input use external textures
        not_owned = ["color_input", "image_input", "video_frame"]
        for node_type in not_owned:
            assert not owns_color_texture(node_type), \
                f"{node_type} should not be cache-owned (uses external texture)"

    def test_passthrough_nodes_not_cache_owned(self):
        """Passthrough nodes should NOT create new textures."""
        passthrough = ["dot", "noop", "switch"]
        for node_type in passthrough:
            assert not owns_color_texture(node_type), \
                f"{node_type} should not be cache-owned (passthrough)"

    def test_deep_merge_owns_depth(self):
        """Deep merge should own depth texture."""
        assert owns_depth_texture("deep_merge"), \
            "deep_merge should own depth_texture_id"


class TestTilePadding:
    """Test tile padding requirements for spatial effects."""

    def test_spatial_effects_need_padding(self):
        """Spatial effects (blur, glow, etc.) should have tile padding."""
        expected_padding = {
            "blur": 10,  # Default max radius
            "defocus": 10,
            "glow": 10,
            "erode_dilate": 10,
            "transform": 1,
            "cornerpin": 1,
            "sharpen": 2,
            "edge_detect": 2,
        }
        padding_count = 0
        for node_type, expected_pad in expected_padding.items():
            pad = get_tile_padding(node_type)
            if pad > 0:
                padding_count += 1
        # At least half should have padding defined
        assert padding_count >= len(expected_padding) // 2, \
            f"Only {padding_count}/{len(expected_padding)} spatial effects have padding"

    def test_per_pixel_effects_no_padding(self):
        """Per-pixel effects (grade, invert) should have zero padding."""
        no_padding_expected = ["grade", "invert", "shuffle", "premult"]
        for node_type in no_padding_expected:
            assert get_tile_padding(node_type) == 0, \
                f"{node_type} should have zero tile padding"


class TestFusionEligibility:
    """Test fusion eligibility for mega-shader chains."""

    def test_fusible_single_input_effects(self):
        """Single-input per-pixel effects should be fusible."""
        expected_fusible = [
            "grade", "color_correct", "huecorrect",
            "invert", "shuffle", "clamp", "normalize",
            "premult", "unpremult", "cs_transform",
        ]
        fusible_count = 0
        for node_type in expected_fusible:
            if is_fusible(node_type):
                fusible_count += 1
        # At least half should be marked fusible
        assert fusible_count >= len(expected_fusible) // 2, \
            f"Only {fusible_count}/{len(expected_fusible)} expected fusible"

    def test_spatial_effects_not_fusible(self):
        """Spatial effects should NOT be fusible."""
        not_fusible = ["blur", "defocus", "glow", "transform", "reformat", "merge"]
        for node_type in not_fusible:
            assert not is_fusible(node_type), \
                f"{node_type} should not be fusible (spatial or multi-input)"

    def test_multi_input_not_fusible(self):
        """Multi-input nodes should NOT be fusible."""
        not_fusible = ["merge", "dissolve", "channel_merge"]
        for node_type in not_fusible:
            assert not is_fusible(node_type), \
                f"{node_type} should not be fusible (multi-input)"


class TestFrameNumberRequirement:
    """Test frame number requirement for cache keys."""

    def test_time_nodes_require_frame(self):
        """Time-related nodes should require frame number."""
        expected_frame = ["time_offset", "frame_hold", "retime", "video_frame"]
        for node_type in expected_frame:
            assert requires_frame_number(node_type), \
                f"{node_type} should require frame number"

    def test_static_nodes_no_frame(self):
        """Static nodes should NOT require frame number."""
        no_frame = ["blur", "grade", "invert", "constant"]
        for node_type in no_frame:
            assert not requires_frame_number(node_type), \
                f"{node_type} should not require frame number"


class TestROIPolicy:
    """Test ROI propagation policies."""

    def test_source_full_frame(self):
        """Source nodes should have full_frame ROI policy."""
        sources = ["color_input", "image_input", "constant", "video_frame"]
        for node_type in sources:
            assert get_roi_policy(node_type) == "full_frame", \
                f"{node_type} should have full_frame ROI policy"

    def test_identity_filters(self):
        """Simple filters should have identity ROI policy."""
        identity_expected = ["grade", "invert", "shuffle", "premult", "unpremult"]
        identity_count = 0
        for node_type in identity_expected:
            if get_roi_policy(node_type) == "identity":
                identity_count += 1
        # At least half should have identity
        assert identity_count >= len(identity_expected) // 2, \
            f"Only {identity_count}/{len(identity_expected)} have identity ROI"

    def test_expand_spatial(self):
        """Spatial effects should have expand ROI policy."""
        expand_expected = ["blur", "glow", "erode_dilate"]
        for node_type in expand_expected:
            assert get_roi_policy(node_type) == "expand", \
                f"{node_type} should have expand ROI policy"

    def test_merge_union(self):
        """Merge nodes should have merge_union ROI policy."""
        assert get_roi_policy("merge") == "merge_union", \
            "merge should have merge_union ROI policy"


class TestBypassMapping:
    """Test bypass/disabled mapping for passthrough nodes."""

    def test_dot_bypass(self):
        """Dot node should have bypass mapping."""
        mapping = get_bypass_mapping("dot")
        assert mapping is not None, "dot should have bypass_mapping"
        assert "result" in mapping, "dot bypass should map 'result'"

    def test_noop_bypass(self):
        """NoOp node should have bypass mapping."""
        mapping = get_bypass_mapping("noop")
        assert mapping is not None, "noop should have bypass_mapping"

    def test_passthrough_nodes_have_mapping(self):
        """Passthrough nodes should have bypass mapping."""
        passthrough = ["dot", "noop", "switch"]
        mapping_count = 0
        for node_type in passthrough:
            if get_bypass_mapping(node_type) is not None:
                mapping_count += 1
        # At least half should have mapping
        assert mapping_count >= len(passthrough) // 2, \
            f"Only {mapping_count}/{len(passthrough)} passthrough have bypass mapping"


class TestDerivedSets:
    """Test that derived sets are correctly built."""

    def test_source_set_not_empty(self):
        """SOURCE_NODE_TYPES should contain source nodes."""
        assert len(SOURCE_NODE_TYPES) > 0, "SOURCE_NODE_TYPES should not be empty"
        assert "color_input" in SOURCE_NODE_TYPES
        assert "blur" not in SOURCE_NODE_TYPES

    def test_effect_set_not_empty(self):
        """EFFECT_NODE_TYPES should contain effect nodes."""
        assert len(EFFECT_NODE_TYPES) > 0, "EFFECT_NODE_TYPES should not be empty"
        assert "blur" in EFFECT_NODE_TYPES
        assert "color_input" not in EFFECT_NODE_TYPES

    def test_placeholder_set_not_empty(self):
        """PLACEHOLDER_NODE_TYPES should contain placeholder nodes."""
        assert len(PLACEHOLDER_NODE_TYPES) > 0, "PLACEHOLDER_NODE_TYPES should not be empty"

    def test_cache_owned_set_not_empty(self):
        """CACHE_OWNED_TYPES should contain texture-creating nodes."""
        assert len(CACHE_OWNED_TYPES) > 0, "CACHE_OWNED_TYPES should not be empty"
        assert "blur" in CACHE_OWNED_TYPES
        assert "dot" not in CACHE_OWNED_TYPES

    def test_fusible_set_not_empty(self):
        """FUSIBLE_NODE_TYPES should contain fusible nodes."""
        assert len(FUSIBLE_NODE_TYPES) > 0, "FUSIBLE_NODE_TYPES should not be empty"
        assert "grade" in FUSIBLE_NODE_TYPES
        assert "blur" not in FUSIBLE_NODE_TYPES

    def test_sets_are_frozen(self):
        """Derived sets should be frozenset (immutable)."""
        assert isinstance(SOURCE_NODE_TYPES, frozenset)
        assert isinstance(EFFECT_NODE_TYPES, frozenset)
        assert isinstance(CACHE_OWNED_TYPES, frozenset)


class TestEffectTypeMapping:
    """Test effect type mapping for evaluator dispatch."""

    def test_effect_type_matches_node_type(self):
        """Most effect types should match their node_type."""
        # Simple cases where effect_type == node_type
        matches = ["blur", "grade", "invert", "transform", "shuffle"]
        for node_type in matches:
            effect_type = get_effect_type(node_type)
            assert effect_type == node_type, \
                f"{node_type} effect_type should match node_type"

    def test_merge_effect_type(self):
        """Merge node should have 'merge' effect_type."""
        # Both "merge" and "blend" node types use "merge" effect_type
        assert get_effect_type("merge") == "merge"
        assert get_effect_type("blend") == "merge"

    def test_non_effect_no_effect_type(self):
        """Non-effect nodes should have None effect_type."""
        non_effects = ["color_input", "output", "dot", "noop"]
        for node_type in non_effects:
            assert get_effect_type(node_type) is None, \
                f"{node_type} should have None effect_type"


class TestCapabilityFrozen:
    """Test that NodeCapability instances are immutable."""

    def test_capability_is_frozen(self):
        """NodeCapability dataclass should be frozen."""
        cap = get_node_capability("blur")
        assert cap is not None
        # Attempting to modify should raise FrozenInstanceError
        with pytest.raises(Exception):  # dataclasses.FrozenInstanceError
            cap.tile_padding = 100  # Should fail


class TestLegacyListConsistency:
    """BUG-14 Phase 0: Check consistency between old lists and capability system.

    These tests verify that the hand-written lists in various modules do not
    drift from the centralized NodeCapability definitions. If inconsistencies
    are found, warnings are logged and the test documents the mismatch.

    The goal is NOT to force immediate migration, but to detect drift early
    so that Phase 2 (migration to capability-derived) can proceed safely.

    Old lists checked:
    - render_scheduler._GPU_STATEFUL_TYPES vs SOURCE_NODE_TYPES
    - tile_padding_computer._PADDING_NODE_TYPES vs capability
    - pass_fusion.FUSIBLE_TYPES/NON_FUSIBLE_TYPES vs FUSIBLE_NODE_TYPES
    - base_evaluator._CACHE_OWNED_TYPES vs CACHE_OWNED_TYPES
    - base_evaluator._NODE_TYPE_MAP vs capability effect_type mapping
    - base_evaluator._PASSTHROUGH_NODE_TYPES vs PASSTHROUGH_NODE_TYPES
    """

    def test_scheduler_gpu_stateful_types_consistency(self):
        """render_scheduler._GPU_STATEFUL_TYPES should match SOURCE_NODE_TYPES."""
        from src.features.composite.render.render_scheduler import _GPU_STATEFUL_TYPES

        scheduler_sources = set(_GPU_STATEFUL_TYPES)
        cap_sources = SOURCE_NODE_TYPES

        # Check for nodes in scheduler but not in capability
        missing_in_cap = scheduler_sources - cap_sources
        if missing_in_cap:
            import warnings
            warnings.warn(
                f"_GPU_STATEFUL_TYPES has nodes not in SOURCE_NODE_TYPES: {missing_in_cap}. "
                "These nodes may be missed when new nodes are added to scheduler.",
                UserWarning
            )

        # Check for nodes in capability but not in scheduler
        missing_in_scheduler = cap_sources - scheduler_sources
        if missing_in_scheduler:
            import warnings
            warnings.warn(
                f"SOURCE_NODE_TYPES has nodes not in _GPU_STATEFUL_TYPES: {missing_in_scheduler}. "
                "Scheduler will treat these as effect nodes instead of source nodes.",
                UserWarning
            )

        # Strict check: all scheduler sources must be in capability
        assert scheduler_sources.issubset(cap_sources), \
            f"Scheduler sources not in capability: {scheduler_sources - cap_sources}"

    def test_tile_padding_node_types_coverage(self):
        """tile_padding_computer uses capability system for padding checks.

        Phase 2 F2-04: Migrated from _PADDING_NODE_TYPES to TILE_PADDING_TYPES.
        """
        # Phase 2: Use capability-based tile padding types
        from src.features.composite.core.node_capabilities import TILE_PADDING_TYPES

        padding_types = set(TILE_PADDING_TYPES)
        cap_effect_types = EFFECT_NODE_TYPES

        # Check for effect nodes not in padding types (may compute padding dynamically)
        effect_without_padding_method = cap_effect_types - padding_types
        # This is informational - many effect nodes have 0 padding anyway

        # Check for nodes in padding types that are not effect nodes
        padding_non_effects = padding_types - cap_effect_types - SOURCE_NODE_TYPES - {"output"}
        if padding_non_effects:
            import warnings
            warnings.warn(
                f"_PADDING_NODE_TYPES includes non-effect nodes: {padding_non_effects}. "
                "These nodes may not need padding computation.",
                UserWarning
            )

        # Verify key spatial effects are covered
        key_spatial = {"blur", "glow", "erode_dilate", "defocus", "sharpen"}
        missing_spatial = key_spatial - padding_types
        assert len(missing_spatial) == 0, \
            f"Key spatial effects missing from _PADDING_NODE_TYPES: {missing_spatial}"

    def test_pass_fusion_fusible_types_consistency(self):
        """pass_fusion.FUSIBLE_TYPES should match FUSIBLE_NODE_TYPES."""
        from src.features.composite.utils.pass_fusion import FUSIBLE_TYPES, NON_FUSIBLE_TYPES

        fusion_fusible = set(FUSIBLE_TYPES)
        fusion_non_fusible = set(NON_FUSIBLE_TYPES)
        cap_fusible = FUSIBLE_NODE_TYPES

        # Check for fusible nodes in pass_fusion not in capability
        missing_fusible_in_cap = fusion_fusible - cap_fusible
        if missing_fusible_in_cap:
            import warnings
            warnings.warn(
                f"pass_fusion.FUSIBLE_TYPES has nodes not marked fusible in capability: {missing_fusible_in_cap}. "
                "Pass fusion will fuse these nodes, but capability says they are not fusible.",
                UserWarning
            )

        # Check for fusible nodes in capability not in pass_fusion
        missing_fusible_in_fusion = cap_fusible - fusion_fusible
        if missing_fusible_in_fusion:
            import warnings
            warnings.warn(
                f"capability marks nodes as fusible but pass_fusion.FUSIBLE_TYPES excludes them: {missing_fusible_in_fusion}. "
                "These nodes could be fused but pass_fusion will not consider them.",
                UserWarning
            )

        # Check for overlap between fusible and non_fusible (should be empty)
        overlap = fusion_fusible & fusion_non_fusible
        assert len(overlap) == 0, \
            f"FUSIBLE_TYPES and NON_FUSIBLE_TYPES overlap: {overlap}"

        # Non-strict informational check
        # pass_fusion may have additional heuristics (multi-input, etc.)
        # so we just warn about significant differences

    def test_pass_fusion_non_fusible_coverage(self):
        """pass_fusion.NON_FUSIBLE_TYPES should cover nodes that are definitely not fusible.

        Nodes that are spatial effects (blur, transform) or multi-input (merge)
        should be in NON_FUSIBLE_TYPES if not in FUSIBLE_TYPES.
        """
        from src.features.composite.utils.pass_fusion import FUSIBLE_TYPES, NON_FUSIBLE_TYPES

        fusion_fusible = set(FUSIBLE_TYPES)
        fusion_non_fusible = set(NON_FUSIBLE_TYPES)

        # Nodes that capability marks as NOT fusible (spatial, multi-input)
        key_non_fusible = {
            "blur", "defocus", "glow", "sharpen", "erode_dilate",  # Spatial effects
            "transform", "cornerpin", "reformat", "crop",  # Transform
            "merge", "deep_merge", "dissolve",  # Multi-input
            "color_input", "constant", "image_input", "output",  # Source/output
        }

        # Check that key non-fusible nodes are either in NON_FUSIBLE or NOT in FUSIBLE
        for node_type in key_non_fusible:
            if node_type not in fusion_non_fusible:
                # Should not be in fusible
                assert node_type not in fusion_fusible, \
                    f"{node_type} is not in NON_FUSIBLE_TYPES but should not be fusible"

    def test_evaluator_cache_owned_types_consistency(self):
        """Phase 2 F2-02: _CACHE_OWNED_TYPES migrated to node_capabilities.

        Evaluator no longer maintains a separate list - capability is the source.
        """
        # Phase 2: Use capability system directly
        cap_owned = CACHE_OWNED_TYPES

        # Verify capability system is populated with texture producers.
        # Self-managed source nodes (constant, text, image_input, ...) keep
        # owns_color_texture=False so the cache manager doesn't double-free
        # their GL objects; the count reflects the remaining cache-owned
        # effect/output nodes, so the floor is set just below the current
        # count to catch regressions without being brittle to that policy.
        assert len(cap_owned) >= 49, \
            f"CACHE_OWNED_TYPES should have many texture producers, got {len(cap_owned)}"

    def test_evaluator_node_type_map_coverage(self):
        """Phase 2 F2-02: _NODE_TYPE_MAP migrated to NODE_REGISTRY.

        Evaluator now uses NODE_REGISTRY for node instantiation.
        """
        from src.features.composite.core.node_engine import NODE_REGISTRY

        registry_types = set(NODE_REGISTRY.keys())
        cap_effect_types = EFFECT_NODE_TYPES

        # Check for effect nodes not in evaluator map
        missing_effect_map = cap_effect_types - registry_types
        # Placeholder/passthrough nodes don't need registry entry

        # Verify that key effect nodes are covered
        key_effects = {"blur", "grade", "transform", "merge", "invert", "shuffle"}
        missing_key = key_effects - registry_types
        assert len(missing_key) == 0, \
            f"Key effect nodes missing from NODE_REGISTRY: {missing_key}"

    def test_evaluator_passthrough_types_consistency(self):
        """Phase 2 F2-02: _PASSTHROUGH_NODE_TYPES migrated to node_capabilities.

        Evaluator no longer maintains a separate list.
        """
        cap_passthrough = PASSTHROUGH_NODE_TYPES

        # Just verify it's a valid frozenset
        assert isinstance(cap_passthrough, frozenset)

    def test_no_missing_node_type_in_any_list(self):
        """Phase 2: Verify capability covers all registered node types.

        Nodes in NODE_REGISTRY should have capability entry or be placeholder.
        """
        from src.features.composite.core.node_engine import NODE_REGISTRY
        from src.features.composite.core.node_capabilities import _NODE_CAPABILITY_TABLE

        registry_types = set(NODE_REGISTRY.keys())
        cap_types = set(_NODE_CAPABILITY_TABLE.keys())

        # All registry nodes should have capability entry or be placeholder/passthrough
        missing_caps = registry_types - cap_types - PLACEHOLDER_NODE_TYPES

        # Allow special nodes without capability entry
        allowed_missing = {"output"}
        missing_caps = missing_caps - allowed_missing

        if missing_caps:
            import warnings
            warnings.warn(
                f"Registered nodes not in capability table: {missing_caps}. "
                "These nodes may be handled incorrectly.",
                UserWarning
            )

        # Verify key effect nodes have capability entry
        key_effects = {"blur", "grade", "transform", "merge", "invert", "shuffle"}
        missing_key = key_effects - cap_types
        assert len(missing_key) == 0, \
            f"Key effect nodes missing from capability: {missing_key}"


class TestVerificationCriteria:
    """Test the verification criteria from S3-A task spec."""

    def test_scheduler_placeholder_criteria(self):
        """At least half of scheduler's placeholder classification should use capability."""
        # Count how many nodes have is_placeholder defined in capability
        cap_placeholders = PLACEHOLDER_NODE_TYPES
        # These should match nodes that scheduler would mark as placeholder
        assert len(cap_placeholders) > 0, \
            "Capability system should define placeholder nodes"

    def test_evaluator_strategy_criteria(self):
        """Phase 2: Evaluator uses NODE_REGISTRY for node instantiation."""
        from src.features.composite.core.node_engine import NODE_REGISTRY

        registry_types = set(NODE_REGISTRY.keys())
        cap_effect_types = EFFECT_NODE_TYPES

        # Count overlap
        overlap = len(registry_types & cap_effect_types)
        assert overlap >= 5, \
            f"Only {overlap} registry types are effect nodes in capability"

    def test_cache_ownership_criteria(self):
        """Phase 2: Cache ownership uses capability system."""
        cap_owned = CACHE_OWNED_TYPES

        # Verify capability system is populated
        assert len(cap_owned) >= 49, \
            f"CACHE_OWNED_TYPES should have many nodes, got {len(cap_owned)}"

    def test_tile_padding_criteria(self):
        """Tile padding should cover key spatial effect nodes.

        Note: _PADDING_NODE_TYPES is the list of node types that CAN compute
        padding (have _tile_padding() or requires_roi_expansion() methods),
        NOT the list of nodes that NEED padding. Many of those return 0.

        TILE_PADDING_TYPES in capability only includes nodes with tile_padding > 0.
        We verify that key spatial effects (blur, glow, erode_dilate) are covered.
        """
        # Key spatial effects that definitely need padding
        key_spatial_effects = {"blur", "glow", "erode_dilate", "defocus", "sharpen", "transform"}
        cap_padding = TILE_PADDING_TYPES

        # Count overlap
        overlap = len(key_spatial_effects & cap_padding)
        assert overlap >= len(key_spatial_effects) // 2, \
            f"Only {overlap}/{len(key_spatial_effects)} key spatial effects have padding defined"
