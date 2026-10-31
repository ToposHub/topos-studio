"""M14.2b · 子帧 crossfade/Stem 语义回归。"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace as NS

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

pytestmark = pytest.mark.edit_core

from src.features.edit.core.audio_crossfade_semantics import (
    crossfade_gain_envelope,
    stem_channel_semantics,
)


class TestCrossfadeEnvelope:
    def test_equal_power_cross_point(self):
        gi, go = crossfade_gain_envelope(
            100, fade_in_samples=50, fade_out_samples=50,
            curve="equal_power")
        # 等功率 ramp：t → sin(t·π/2)；fade-in 中段任意点值匹配公式
        t = 24 / 49
        assert gi[24] == pytest.approx(np.sin(t * np.pi / 2), abs=1e-6)
        # fade 尾端到 1.0
        assert gi[49] == pytest.approx(1.0, abs=1e-6)

    def test_linear_envelope_bounds(self):
        gi, go = crossfade_gain_envelope(
            200, fade_in_samples=100, fade_out_samples=100, curve="linear")
        assert gi[0] == pytest.approx(0.0)
        assert gi[-1] == pytest.approx(1.0)
        assert go[0] == pytest.approx(1.0)
        assert go[-1] == pytest.approx(0.0)

    def test_zero_total_empty(self):
        gi, go = crossfade_gain_envelope(0, 10, 10)
        assert len(gi) == 0 and len(go) == 0

    def test_clamped_to_total(self):
        gi, _ = crossfade_gain_envelope(50, fade_in_samples=200)
        assert len(gi) == 50  # fade 被钳到总长


class TestStemSemantics:
    def test_declaration_shape(self):
        clip = NS(fade_in_samples=480, fade_out_samples=None,
                  audio_sample_offset=12, source_sample_offset=34)
        d = stem_channel_semantics(clip, track_id="A1", is_bus=True)
        assert d["bus_kind"] == "GROUP"
        assert d["fade_samples_in"] == 480
        assert d["fade_samples_out"] is None   # 沿用帧域（如实）
        assert d["sample_offsets"] == {
            "audio_sample_offset": 12, "source_sample_offset": 34}

    def test_regular_track_no_bus(self):
        d = stem_channel_semantics(
            NS(fade_in_samples=None, fade_out_samples=None,
               audio_sample_offset=0, source_sample_offset=0),
            track_id="V1")
        assert d["is_bus"] is False and d["bus_kind"] is None
