"""AI export subtitle geometry is measured from the final renderer contract."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.app

from src.app.controllers.ai_edit_host_bridge_impl import (  # noqa: E402
    _build_measured_subtitle_quality_cues,
    _merge_brief_export_contract,
    _prepare_brand_quality_contract,
    _resolve_subtitle_safe_area_policy,
)
from src.features.edit.ai_editing.delivery_contracts import (  # noqa: E402
    validate_subtitle_cues,
)
from src.features.edit.core.captions.model import CaptionTrackMetadata  # noqa: E402
from src.features.edit.core.timeline_v2 import (  # noqa: E402
    EditTrack,
    SubtitleClip,
    TimelineSequence,
)


def _sequence(*, width: int, height: int, text: str) -> TimelineSequence:
    clip = SubtitleClip(
        clip_id="subtitle-geometry-1",
        track_id="subtitle-track-1",
        timeline_start=0,
        timeline_duration=48,
        text=text,
    )
    track = EditTrack(
        track_id="subtitle-track-1",
        track_type="subtitle",
        subtitle_clips=(clip,),
        caption_metadata=CaptionTrackMetadata(),
    )
    return TimelineSequence(
        timeline_id="geometry-test",
        width=width,
        height=height,
        subtitle_tracks=(track,),
    )


def test_host_passes_measured_cjk_bbox_to_title_safe_contract(qapp) -> None:
    sequence = _sequence(width=1080, height=1920, text="你好 Hello 世界")
    cues = _build_measured_subtitle_quality_cues(
        sequence,
        (1080, 1920),
        safe_area_profile="title_safe",
        safe_area_inset=0.05,
    )

    assert len(cues) == 1
    cue = cues[0]
    assert cue["safe_area_profile"] == "title_safe"
    assert cue["safe_anchor"] == "bottom_center"
    assert cue["bbox"][2] > 0.0 and cue["bbox"][3] > 0.0
    result = validate_subtitle_cues(
        cues,
        timeline_duration_frames=48,
        fps=24.0,
    )
    assert result.passed
    assert result.safe_area_violations == 0


def test_host_rejects_vertical_social_overlay_safe_area_when_bbox_is_too_low(qapp) -> None:
    sequence = _sequence(width=1080, height=1920, text="竖屏标题 Vertical title")
    cues = _build_measured_subtitle_quality_cues(
        sequence,
        (1080, 1920),
        safe_area_profile="vertical_social",
        safe_area_inset=0.10,
    )

    result = validate_subtitle_cues(
        cues,
        timeline_duration_frames=48,
        fps=24.0,
    )
    assert not result.passed
    assert result.safe_area_violations == 1


def test_safe_area_policy_supports_custom_inset_and_rejects_unknown_profile() -> None:
    assert _resolve_subtitle_safe_area_policy({
        "safe_area_profile": "custom",
        "safe_area_inset": "0.07",
    }) == ("custom", 0.07)
    with pytest.raises(ValueError):
        _resolve_subtitle_safe_area_policy({"safe_area_profile": "unknown"})


def test_host_normalizes_brand_measurements_with_selected_safe_area() -> None:
    elements, requirements = _prepare_brand_quality_contract(
        {
            "brand_requirements": ({"element_id": "logo-main", "kind": "logo"},),
            "brand_elements": ({
                "element_id": "logo-main",
                "kind": "logo",
                "start_frame": 0,
                "end_frame": 48,
                "bbox": [100.0, 100.0, 200.0, 80.0],
            },),
        },
        (1920, 1080),
        safe_area_inset=0.05,
    )

    assert requirements[0]["element_id"] == "logo-main"
    assert elements[0]["safe_area"] == [96.0, 54.0, 1824.0, 1026.0]


def test_host_materializes_brand_measurements_when_export_input_omits_them() -> None:
    clip = type("Clip", (), {
        "clip_id": "clip-logo", "asset_id": "asset-logo", "track_id": "track-logo",
        "timeline_start": 0, "timeline_duration": 48,
        "source_start": 0, "source_duration": 48,
        "speed": type("Speed", (), {"reverse": False})(), "is_enabled": True,
    })()
    track = type("Track", (), {
        "track_id": "track-logo", "is_enabled": True, "is_visible": True,
        "clips": (clip,),
    })()
    sequence = type("Sequence", (), {"video_tracks": (track,)})()
    assets = {"asset-logo": type("Asset", (), {"metadata": {
        "brand_elements": ({
            "element_id": "logo-main", "kind": "logo",
            "bbox": [0.80, 0.05, 0.10, 0.08], "bbox_space": "canvas_normalized",
        },),
    }})()}

    elements, requirements = _prepare_brand_quality_contract(
        {"brand_requirements": ({"element_id": "logo-main", "kind": "logo"},)},
        (1920, 1080),
        safe_area_inset=0.05,
        sequence=sequence,
        assets=assets,
    )

    assert requirements[0]["element_id"] == "logo-main"
    assert elements[0]["measurement_source"] == "asset_annotation"
    assert elements[0]["safe_area"] == [96.0, 54.0, 1824.0, 1026.0]


def test_host_reads_persisted_brand_contract_from_active_editorial_brief() -> None:
    class Scope:
        timeline_id = "timeline-1"
        brief_id = "campaign-brief"

    class Brief:
        brand_requirements = (
            {"element_id": "disclaimer", "kind": "disclaimer"},
        )
        safe_area_profile = "vertical_social"

    class Service:
        active_brief_id = "campaign-brief"

        @staticmethod
        def get_brief(brief_id: str):
            return Brief() if brief_id == "campaign-brief" else None

    merged = _merge_brief_export_contract(Service(), Scope(), {})

    assert merged["brand_requirements"][0]["element_id"] == "disclaimer"
    assert merged["safe_area_profile"] == "vertical_social"
