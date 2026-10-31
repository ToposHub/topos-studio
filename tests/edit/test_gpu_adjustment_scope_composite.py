"""FX-C02 GPU regression: adjustment scope in the scoped composite.

The GPU composite (_composite_with_adjustment_scope) interleaves
adjustment effect chains at their z-position so an adjustment only sees
the layers below it. The scope contract lives in the pure-data
``_build_scoped_composite_work`` helper, which produces the z-ordered
operation list the renderer walks. These tests exercise THAT list — the
single source of truth for scope — without any GL (the offscreen fixture
is out of P0 scope per audit §7).

If the work list is right, a renderer following it cannot apply an
adjustment to a layer above it, because the adjustment op is emitted
before any upper-layer op.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from src.features.edit.core.edit_gpu_preview_renderer import EditGpuPreviewRenderer

pytestmark = pytest.mark.edit_core


def _adj(track_index: int):
    """A fake adjustment layer with a track_index and an effect chain."""
    layer = MagicMock()
    layer.track_index = track_index
    return (layer, (MagicMock(),))


def _kinds(work) -> list:
    """Strip the work list to just the op kinds (layer/adjustment)."""
    return [item[2] for item in work]


def _adj_positions(work) -> list:
    """Indices of adjustment ops in the work list."""
    return [i for i, item in enumerate(work) if item[2] == 'adjustment']


def _layer_positions(work) -> list:
    return [i for i, item in enumerate(work) if item[2] == 'layer']


class TestScopedWorkListOrder:
    def test_lower_adj_upper_interleave(self) -> None:
        """3-layer fixture: lower(0) / adj(1) / upper(2).

        Op sequence: layer(lower) → adjust(adj) → layer(upper). The
        adjustment runs BEFORE the upper layer is composited, so it can
        only see the lower layer — the FX-C02 scope guarantee."""
        work = EditGpuPreviewRenderer._build_scoped_composite_work(
            track_textures=[(MagicMock(), 1.0), (MagicMock(), 1.0)],
            track_texture_indices=[0, 2],
            adjustment_layers=[_adj(1)],
        )
        assert _kinds(work) == ["layer", "adjustment", "layer"]
        adj_pos = _adj_positions(work)[0]
        assert _layer_positions(work)[0] < adj_pos < _layer_positions(work)[1]

    def test_adjustment_sees_only_layers_below(self) -> None:
        """At the adjustment's position, the layers BEFORE it in the work
        list are exactly the layers below it (z < adj). No upper layer
        appears before the adjustment."""
        work = EditGpuPreviewRenderer._build_scoped_composite_work(
            track_textures=[(MagicMock(), 1.0), (MagicMock(), 1.0), (MagicMock(), 1.0)],
            track_texture_indices=[0, 5, 10],
            adjustment_layers=[_adj(3)],
        )
        adj_pos = _adj_positions(work)[0]
        # z-order of the layers: 0, 5, 10. Adjustment at z=3 sits between 0/5.
        # Layers before the adjustment (z < 3): only z=0.
        before = [item for item in work[:adj_pos] if item[2] == 'layer']
        after = [item for item in work[adj_pos + 1:] if item[2] == 'layer']
        assert len(before) == 1, "only the layer below (z=0) precedes the adjustment"
        assert len(after) == 2, "the two upper layers come after"

    def test_adj_at_z0_with_layer_above(self) -> None:
        """Adjustment at the bottom (z=0) with a layer above (z=2):
        adjustment first (nothing below), then the layer."""
        work = EditGpuPreviewRenderer._build_scoped_composite_work(
            track_textures=[(MagicMock(), 1.0)],
            track_texture_indices=[2],
            adjustment_layers=[_adj(0)],
        )
        assert _kinds(work) == ["adjustment", "layer"]

    def test_two_adjustments_interleave_at_their_z(self) -> None:
        """base(0) / adjA(1) / mid(2) / adjB(3) / top(4).

        adjA runs after base only; adjB runs after base+mid; neither sees top.
        """
        work = EditGpuPreviewRenderer._build_scoped_composite_work(
            track_textures=[(MagicMock(), 1.0), (MagicMock(), 1.0), (MagicMock(), 1.0)],
            track_texture_indices=[0, 2, 4],
            adjustment_layers=[_adj(1), _adj(3)],
        )
        assert _kinds(work) == ["layer", "adjustment", "layer", "adjustment", "layer"]
        adj_a, adj_b = _adj_positions(work)
        # adjA: 1 layer before (base). adjB: 2 layers before (base+mid).
        assert sum(1 for it in work[:adj_a] if it[2] == 'layer') == 1
        assert sum(1 for it in work[:adj_b] if it[2] == 'layer') == 2

    def test_tie_index_layer_before_adjustment(self) -> None:
        """When a layer and an adjustment share a track_index, the layer
        composites first (it is the topmost of 'below' the adjustment)."""
        work = EditGpuPreviewRenderer._build_scoped_composite_work(
            track_textures=[(MagicMock(), 1.0)],
            track_texture_indices=[5],
            adjustment_layers=[_adj(5)],
        )
        assert _kinds(work) == ["layer", "adjustment"]

    def test_empty_inputs_return_empty(self) -> None:
        work = EditGpuPreviewRenderer._build_scoped_composite_work(
            track_textures=[], track_texture_indices=[], adjustment_layers=[],
        )
        assert work == []

    def test_only_layers_no_adjustments(self) -> None:
        """No adjustments → plain bottom-up layer order (fast-path equivalent)."""
        work = EditGpuPreviewRenderer._build_scoped_composite_work(
            track_textures=[(MagicMock(), 1.0), (MagicMock(), 0.5)],
            track_texture_indices=[0, 1],
            adjustment_layers=[],
        )
        assert _kinds(work) == ["layer", "layer"]
