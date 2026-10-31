"""W2-prep HtmlPatch validation (FH-10)."""

from __future__ import annotations

import pytest

from src.features.teaching_animation.render.html_layer import (
    DeclaredNode, HtmlLayerState, HtmlPatch, HtmlPatchError, HtmlPatchErrorCode,
    HtmlPatchOperation, validate_patch,
)


def _state(nodes=None, tokens=(), slots=(), assets=(), html="doc") -> HtmlLayerState:
    return HtmlLayerState(
        revision=3,
        source_hash="hashabc",
        declared_nodes=nodes or {"title": DeclaredNode("title", "content", True, False)},
        declared_tokens=frozenset(tokens),
        declared_slots=frozenset(slots),
        bound_asset_ids=frozenset(assets),
    )


class TestValidPatches:
    def test_replace_text_accepted(self) -> None:
        state = _state()
        patch = HtmlPatch(HtmlPatchOperation.REPLACE_TEXT, "title", "New", 3, "hashabc")
        validate_patch(patch, state)  # no raise

    def test_set_token_accepted(self) -> None:
        state = _state(tokens={"brand"})
        patch = HtmlPatch(HtmlPatchOperation.SET_TOKEN, "brand", "#06c", 3, "hashabc")
        validate_patch(patch, state)

    def test_set_asset_accepted_for_asset_node(self) -> None:
        state = _state(nodes={"logo": DeclaredNode("logo", "content", False, True)}, assets={"logo_png"})
        patch = HtmlPatch(HtmlPatchOperation.SET_ASSET, "logo", "logo_png", 3, "hashabc")
        validate_patch(patch, state)

    def test_transform_node_accepted_for_absolute(self) -> None:
        state = _state(nodes={"badge": DeclaredNode("badge", "absolute_transform", False, False)})
        patch = HtmlPatch(HtmlPatchOperation.TRANSFORM_NODE, "badge", {"x": 10.0, "y": 0.0}, 3, "hashabc")
        validate_patch(patch, state)

    def test_set_slot_attr_accepted(self) -> None:
        state = _state(slots={"card1"})
        patch = HtmlPatch(HtmlPatchOperation.SET_SLOT_ATTR, "card1", "rounded", 3, "hashabc", sub_target="class")
        validate_patch(patch, state)

    def test_set_animation_timing_accepted(self) -> None:
        state = _state()
        patch = HtmlPatch(HtmlPatchOperation.SET_ANIMATION_TIMING, "title", {"duration_ms": 600}, 3, "hashabc")
        validate_patch(patch, state)


class TestRejections:
    def test_stale_revision_rejected(self) -> None:
        state = _state()
        patch = HtmlPatch(HtmlPatchOperation.REPLACE_TEXT, "title", "x", 2, "hashabc")
        with pytest.raises(HtmlPatchError) as exc:
            validate_patch(patch, state)
        assert exc.value.code == HtmlPatchErrorCode.STALE_REVISION

    def test_precondition_mismatch_rejected(self) -> None:
        state = _state()
        patch = HtmlPatch(HtmlPatchOperation.REPLACE_TEXT, "title", "x", 3, "wronghash")
        with pytest.raises(HtmlPatchError) as exc:
            validate_patch(patch, state)
        assert exc.value.code == HtmlPatchErrorCode.PRECONDITION_MISMATCH

    def test_unknown_node_rejected(self) -> None:
        state = _state()
        patch = HtmlPatch(HtmlPatchOperation.REPLACE_TEXT, "ghost", "x", 3, "hashabc")
        with pytest.raises(HtmlPatchError) as exc:
            validate_patch(patch, state)
        assert exc.value.code == HtmlPatchErrorCode.UNKNOWN_NODE

    def test_edit_mode_forbids_transform(self) -> None:
        # content node cannot be transformed
        state = _state()
        patch = HtmlPatch(HtmlPatchOperation.TRANSFORM_NODE, "title", {"x": 1.0}, 3, "hashabc")
        with pytest.raises(HtmlPatchError) as exc:
            validate_patch(patch, state)
        assert exc.value.code == HtmlPatchErrorCode.EDIT_MODE_FORBIDS

    def test_unbound_asset_rejected(self) -> None:
        state = _state(nodes={"logo": DeclaredNode("logo", "content", False, True)}, assets={"a"})
        patch = HtmlPatch(HtmlPatchOperation.SET_ASSET, "logo", "not_bound", 3, "hashabc")
        with pytest.raises(HtmlPatchError) as exc:
            validate_patch(patch, state)
        assert exc.value.code == HtmlPatchErrorCode.ILLEGAL_VALUE

    def test_unknown_token_rejected(self) -> None:
        state = _state(tokens={"brand"})
        patch = HtmlPatch(HtmlPatchOperation.SET_TOKEN, "ghost_token", "#000", 3, "hashabc")
        with pytest.raises(HtmlPatchError) as exc:
            validate_patch(patch, state)
        assert exc.value.code == HtmlPatchErrorCode.UNKNOWN_TOKEN

    def test_illegal_value_types_rejected(self) -> None:
        state = _state()
        patch = HtmlPatch(HtmlPatchOperation.REPLACE_TEXT, "title", 123, 3, "hashabc")
        with pytest.raises(HtmlPatchError) as exc:
            validate_patch(patch, state)
        assert exc.value.code == HtmlPatchErrorCode.ILLEGAL_VALUE


class TestLayerState:
    def test_for_source_hashes_html(self) -> None:
        state = HtmlLayerState.for_source("<div>x</div>")
        assert len(state.source_hash) == 64
        assert state.revision == 1

    def test_with_revision(self) -> None:
        state = HtmlLayerState.for_source("x")
        bumped = state.with_revision(5)
        assert bumped.revision == 5 and state.revision == 1
