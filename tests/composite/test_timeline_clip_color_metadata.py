"""P0-4 regression: the timeline-clip frame provider must tag source frames
with the video's real transfer/primaries (Rec.709/PQ/HLG), not a hardcoded
"srgb".

Previously every source frame returned from TimelineClipFrameProvider was
tagged color_space="srgb", so the Composite IDT decoded Rec.709 video (and
HDR) with the wrong transfer and silently dropped HDR. This test exercises the
detection helper directly with a stub service exposing ClipInfo-style metadata.
"""
import pytest

from src.features.edit.core.timeline_clip_frame_provider import (
    TimelineClipFrameProvider,
)


class _StubClipInfo:
    def __init__(self, color_transfer="", color_primaries="", color_matrix="", color_range=""):
        self.color_transfer = color_transfer
        self.color_primaries = color_primaries
        self.color_matrix = color_matrix
        self.color_range = color_range


class _StubService:
    def __init__(self, clip_info):
        self._info = clip_info

    def get_clip_info(self, clip_id):
        return self._info


def _provider():
    # Construct without the full init (we only test the detection helper).
    return TimelineClipFrameProvider.__new__(TimelineClipFrameProvider)


def test_rec709_video_detected_as_bt709():
    svc = _StubService(_StubClipInfo(color_transfer="bt709", color_primaries="bt709"))
    cs, prim, matrix, rng = _provider()._detect_source_color_space(svc, "clip1")
    assert cs == "bt709"
    assert prim == "bt709"
    assert matrix == "bt709"
    assert rng == "limited"


def test_pq_video_detected_as_hdr_bt2020():
    svc = _StubService(_StubClipInfo(color_transfer="smpte2084", color_primaries="bt2020"))
    cs, prim, matrix, rng = _provider()._detect_source_color_space(svc, "clip1")
    assert cs == "pq"
    # PQ content is BT.2020 gamut even if primaries metadata is absent.
    assert prim == "bt2020"
    assert matrix == "bt2020"
    assert rng == "limited"


def test_hlg_video_detected_as_hdr_bt2020():
    svc = _StubService(_StubClipInfo(color_transfer="arib-std-b67", color_primaries="bt2020"))
    cs, prim, matrix, rng = _provider()._detect_source_color_space(svc, "clip1")
    assert cs == "hlg"
    assert prim == "bt2020"
    assert matrix == "bt2020"
    assert rng == "limited"


def test_unknown_consumer_video_defaults_to_bt709():
    """Consumer video without metadata must default to Rec.709 (the broadcast
    standard), NOT srgb — this is the core P0-4 fix."""
    svc = _StubService(_StubClipInfo(color_transfer="", color_primaries=""))
    cs, prim, matrix, rng = _provider()._detect_source_color_space(svc, "clip1")
    assert cs == "bt709"
    assert prim == "bt709"
    assert matrix == "bt709"
    assert rng == "limited"


def test_missing_clip_info_falls_back_to_bt709():
    svc = _StubService(None)
    cs, prim, matrix, rng = _provider()._detect_source_color_space(svc, "clip1")
    assert cs == "bt709"
    assert prim == "bt709"
    assert matrix == "bt709"
    assert rng == "limited"


def test_wide_gamut_sdr_detected():
    """BT.2020 primaries with an SDR transfer tag the gamut as bt2020."""
    svc = _StubService(_StubClipInfo(color_transfer="bt709", color_primaries="bt2020"))
    cs, prim, matrix, rng = _provider()._detect_source_color_space(svc, "clip1")
    assert cs == "bt709"
    assert prim == "bt2020"
