"""FX-M05 regression: video-effects export preflight (never silently drop).

Audit FX-M05: export must never silently degrade. An enabled effect with
no export backend used to vanish from
the deliverable with no warning. The preflight
``preflight_video_effects_export`` scans the timeline and blocks every such
effect so an incomplete deliverable cannot be produced accidentally.

Pure-data (no GL/encode), so it is unit-testable.
"""

from __future__ import annotations

import pytest

from src.features.edit.core.effect_instance import EffectInstance
from src.features.edit.core.timeline_export import (
    ExportPreflightResult,
    preflight_video_effects_export,
)
from src.features.edit.core.timeline_v2 import (
    ClipInstance,
    EditTrack,
    TimelineSequence,
)
from src.shared.media.metadata import Rational

pytestmark = pytest.mark.edit_core


def _seq_with_effects(*effect_types: str) -> TimelineSequence:
    """A one-track timeline whose single clip carries the given effects."""
    effects = tuple(
        EffectInstance(effect_type=t, params={"x": 1.0}, is_enabled=True)
        for t in effect_types
    )
    clip = ClipInstance(
        clip_id="c1", asset_id="a1", track_id="v1",
        timeline_start=0, timeline_duration=10, source_start=0, source_duration=10,
        effects=effects,
    )
    track = EditTrack(track_id="v1", name="V1", track_type="video", index=0, clips=(clip,))
    return TimelineSequence(
        timeline_id="seq", name="M", fps=Rational(24, 1),
        width=8, height=8, video_tracks=(track,),
    )


class TestVideoEffectsPreflight:
    def test_none_sequence_is_ok(self) -> None:
        r = preflight_video_effects_export(None)
        assert r.ok is True
        assert r.degraded == ()

    def test_exportable_effects_pass_clean(self) -> None:
        """Implemented CPU/MPS effects pass with no degraded message."""
        r = preflight_video_effects_export(
            _seq_with_effects(
                "blur", "sharpen", "glow", "erode_dilate", "edge_detect",
                "denoise", "defocus"))
        assert r.ok is True
        assert r.degraded == ()

    def test_gpu_only_effect_is_flagged(self) -> None:
        """Unknown filter with no export backend is explicitly blocked."""
        r = preflight_video_effects_export(_seq_with_effects("missing_filter"))
        assert r.ok is False
        assert len(r.blocking) == 1
        assert "missing_filter" in r.blocking[0]

    def test_only_non_exportable_effects_are_flagged(self) -> None:
        r = preflight_video_effects_export(
            _seq_with_effects("denoise", "edge_detect", "missing_filter")
        )
        assert len(r.blocking) == 1
        assert "edge_detect" not in " ".join(r.blocking)
        assert "denoise" not in " ".join(r.blocking)

    def test_disabled_effect_not_flagged(self) -> None:
        """A disabled effect is skipped at render time, so it is not flagged."""
        eff = EffectInstance(effect_type="missing_filter", params={}, is_enabled=False)
        clip = ClipInstance(
            clip_id="c1", asset_id="a1", track_id="v1",
            timeline_start=0, timeline_duration=10, source_start=0, source_duration=10,
            effects=(eff,),
        )
        track = EditTrack(track_id="v1", name="V1", track_type="video", index=0, clips=(clip,))
        seq = TimelineSequence(
            timeline_id="seq", name="M", fps=Rational(24, 1),
            width=8, height=8, video_tracks=(track,),
        )
        r = preflight_video_effects_export(seq)
        assert r.degraded == ()

    def test_duplicate_effect_type_flagged_once(self) -> None:
        """The same effect_type on multiple clips → one message (deduped)."""
        eff = EffectInstance(effect_type="missing_filter", params={}, is_enabled=True)
        c1 = ClipInstance(clip_id="c1", asset_id="a1", track_id="v1",
                          timeline_start=0, timeline_duration=5, source_start=0, source_duration=5,
                          effects=(eff,))
        c2 = ClipInstance(clip_id="c2", asset_id="a2", track_id="v1",
                          timeline_start=5, timeline_duration=5, source_start=0, source_duration=5,
                          effects=(eff,))
        track = EditTrack(track_id="v1", name="V1", track_type="video", index=0, clips=(c1, c2))
        seq = TimelineSequence(
            timeline_id="seq", name="M", fps=Rational(24, 1),
            width=8, height=8, video_tracks=(track,),
        )
        r = preflight_video_effects_export(seq)
        assert len(r.blocking) == 1

    def test_mixed_chain_flags_only_non_exportable(self) -> None:
        """blur + unsupported filter → only unsupported filter is flagged."""
        r = preflight_video_effects_export(_seq_with_effects("blur", "missing_filter"))
        assert len(r.blocking) == 1
        assert "missing_filter" in r.blocking[0]
