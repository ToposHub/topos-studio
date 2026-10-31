"""M04.3 · 解释组合扩展域 + 消费者矩阵 + 指纹失效联测。"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

pytestmark = pytest.mark.edit_core

from src.features.edit.core.interpretation import InterpretationOverride
from src.features.edit.core.interpretation_matrix import (
    consumer_support_matrix,
    dimension_status,
    unapplied_dimensions,
)
from src.features.edit.core.render_cache import build_render_cache_fingerprint


class TestOverrideExtension:
    def test_roundtrip_all_fields(self):
        ov = InterpretationOverride(
            fps=24.0, rotation_deg=90, sar=(1, 1), field_order="tt")
        back = InterpretationOverride.from_dict(ov.to_dict())
        assert back == ov
        assert not back.is_empty()

    def test_invalid_values_rejected(self):
        with pytest.raises(ValueError):
            InterpretationOverride(rotation_deg=45).validate()
        with pytest.raises(ValueError):
            InterpretationOverride(sar=(0, 1)).validate()
        with pytest.raises(ValueError):
            InterpretationOverride(field_order="weird").validate()

    def test_malformed_payload_fields_dropped(self):
        ov = InterpretationOverride.from_dict({
            "fps": 25.0, "rotation_deg": 45, "sar": [1],
            "field_order": "xxx"})
        assert ov.fps == 25.0
        assert ov.rotation_deg is None and ov.sar is None
        assert ov.field_order is None


class TestConsumerMatrix:
    def test_fps_applied_others_declared_unapplied(self):
        matrix = consumer_support_matrix()
        assert all(s == "applied"
                   for s in matrix["fps"].values())
        for dim in ("rotation_deg", "sar", "field_order"):
            assert all(s == "declared_unapplied"
                       for s in matrix[dim].values()), dim
        # 未知维度保守值
        assert dimension_status("voodoo", "preview_render") \
            == "declared_unapplied"

    def test_unapplied_dimensions_only_when_set(self):
        assert unapplied_dimensions(None) == []
        assert unapplied_dimensions(InterpretationOverride(fps=25.0)) == []
        assert unapplied_dimensions(
            InterpretationOverride(rotation_deg=90, field_order="tt")) \
            == ["rotation_deg", "field_order"]


class TestFingerprintInvalidation:
    @staticmethod
    def _project(override):
        from src.features.edit.core.timeline_v2 import (
            ClipInstance, EditTrack, Rational, TimelineSequence,
        )
        clip = ClipInstance(
            clip_id="c1", asset_id="a1", track_id="v1",
            timeline_start=0, timeline_duration=24,
            source_start=0, source_duration=24)
        seq = TimelineSequence(
            timeline_id="t1", name="T", fps=Rational(24, 1),
            video_tracks=(EditTrack(
                track_id="v1", track_type="video", index=0,
                clips=(clip,),),),
        )
        table = {"a1": override} if override is not None else {}
        return NS(timelines={"t1": seq}, assets={"a1": NS()},
                  color_stacks={}, interpretation_overrides=table), clip

    def test_override_change_invalidates_fingerprint(self):
        (p1, clip) = self._project(None)
        (p2, clip2) = self._project(InterpretationOverride(fps=24.0))
        (p3, clip3) = self._project(InterpretationOverride(fps=25.0))
        base = build_render_cache_fingerprint(p1, "t1", clip)
        fp_no_override = build_render_cache_fingerprint(p1, "t1", clip)
        assert base == fp_no_override  # 无覆盖工程指纹稳定
        # 加覆盖/改覆盖 → 指纹变化（缓存失效）
        assert build_render_cache_fingerprint(p2, "t1", clip2) != base
        assert build_render_cache_fingerprint(p3, "t1", clip3) \
            != build_render_cache_fingerprint(p2, "t1", clip2)

    def test_new_dimension_change_invalidates(self):
        (p1, clip) = self._project(InterpretationOverride(fps=24.0))
        (p2, clip2) = self._project(
            InterpretationOverride(fps=24.0, rotation_deg=180))
        assert build_render_cache_fingerprint(p2, "t1", clip2) \
            != build_render_cache_fingerprint(p1, "t1", clip)
