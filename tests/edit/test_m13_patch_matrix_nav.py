"""M13.1/M13.2 · 源 patch 矩阵/混合值/编辑边界导航回归。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

pytestmark = pytest.mark.edit_core

from src.features.edit.core.source_patch import SourcePatchSpec
from src.features.edit.core.source_patch_matrix import (
    MIXED,
    channel_matrix,
    clip_boundaries,
    nearest_edit_boundary,
    resolve_mixed_values,
)


class TestChannelMatrix:
    def test_passthrough_identity(self):
        m = channel_matrix(SourcePatchSpec(), n_source_channels=2)
        assert m == ((True, False), (False, True))

    def test_mapped_routing_and_mute_row(self):
        # 源 2ch → 目标 3ch：0→0、1→2，目标 1 无映射=静音轨
        spec = SourcePatchSpec(audio_channel_map=((0, 0), (1, 2)))
        m = channel_matrix(spec, 2, n_target_channels=3)
        assert m == ((True, False), (False, False), (False, True))

    def test_out_of_range_ignored(self):
        spec = SourcePatchSpec(audio_channel_map=((5, 0),))
        m = channel_matrix(spec, 2, n_target_channels=1)
        assert m == ((False, False),)  # 越界源不布线


class TestMixedValues:
    def test_uniform(self):
        assert resolve_mixed_values([5, 5, 5]) == 5
        assert resolve_mixed_values(["a", "a"]) == "a"

    def test_mixed(self):
        assert resolve_mixed_values([5, 7, 5]) is MIXED
        assert resolve_mixed_values([True, False]) is MIXED

    def test_empty_is_mixed(self):
        assert resolve_mixed_values([]) is MIXED


class TestEditBoundaryNavigation:
    B = (0, 24, 48, 96, 120)

    def test_next_and_prev(self):
        assert nearest_edit_boundary(self.B, 30, direction="next") == 48
        assert nearest_edit_boundary(self.B, 30, direction="prev") == 24
        assert nearest_edit_boundary(self.B, 48, direction="next") is None or \
            nearest_edit_boundary(self.B, 48, direction="next") == 96
        # 严格大于：48 的 next 是 96
        assert nearest_edit_boundary(self.B, 48, direction="next") == 96
        assert nearest_edit_boundary(self.B, 48, direction="prev") == 24

    def test_include_current(self):
        assert nearest_edit_boundary(
            self.B, 48, direction="next", include_current=True) == 48
        assert nearest_edit_boundary(
            self.B, 48, direction="prev", include_current=True) == 48

    def test_ends_return_none(self):
        assert nearest_edit_boundary(self.B, 200, direction="next") is None
        assert nearest_edit_boundary(self.B, -1, direction="prev") is None

    def test_empty_boundaries(self):
        assert nearest_edit_boundary((), 10) is None

    def test_clip_boundaries_sorted_unique(self):
        from types import SimpleNamespace as NS
        clips = [NS(timeline_start=48), NS(timeline_start=0),
                 NS(timeline_start=48)]
        assert clip_boundaries(clips) == (0, 48)
