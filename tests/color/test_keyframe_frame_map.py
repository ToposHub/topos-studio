"""Panel coordinates must sample the same source frames as playback/export."""
from dataclasses import replace

import pytest

from src.features.color.core.keyframe_frame_map import KeyframeFrameMap
from src.features.edit.core.clip_params import SpeedParams, SpeedPoint
from src.features.edit.core.timeline_v2 import ClipInstance


def clip(**changes) -> ClipInstance:
    return replace(ClipInstance(
        clip_id="c", asset_id="a", track_id="v", timeline_start=240,
        timeline_duration=40, source_start=100, source_duration=80), **changes)


@pytest.mark.parametrize("changes", [
    {}, {"speed": 0.5}, {"speed": 2.0},
    {"speed": SpeedParams(reverse=True)}, {"freeze_source_frame": 125},
    {"speed": SpeedParams(speed_points=(SpeedPoint(20, 2.0),))},
    {"speed": SpeedParams(speed_points=(SpeedPoint(20, 2.0, "smooth"),))},
    {"_source_fps_num": 30000, "_source_fps_den": 1001,
     "_timeline_fps_num": 24000, "_timeline_fps_den": 1001},
])
def test_real_clip_mapping_and_first_exact_inverse(changes):
    instance = clip(**changes)
    mapping = KeyframeFrameMap(instance, instance.timeline_duration)
    sampled = [instance.clip_local_to_source_frame(i) for i in range(40)]
    assert [mapping.source_frame(i) for i in range(40)] == sampled
    for source in range(min(sampled) - 1, max(sampled) + 2):
        expected = sampled.index(source) if source in sampled else None
        assert mapping.local_frame(source) == expected
    for frame in range(mapping.panel_total):
        source = mapping.panel_source_frame(frame)
        assert mapping.panel_frame(source) == frame
        local = mapping.panel_to_local(frame)
        assert abs(sampled[local] - source) == min(abs(value - source) for value in sampled)
    assert mapping.source_frame(-1) is None
    assert mapping.source_frame(40) is None


def test_effective_source_offset_preserves_speed_and_reverse():
    for speed in (2.0, SpeedParams(reverse=True)):
        instance = clip(speed=speed)
        mapping = KeyframeFrameMap(instance, 40, effective_source_start=112)
        for local in range(40):
            assert mapping.source_frame(local) == instance.clip_local_to_source_frame(local) + 12


def test_signature_captures_mutable_playback_adapter():
    from types import SimpleNamespace
    instance = SimpleNamespace(source_start=10)
    mapping = KeyframeFrameMap(instance, 40)
    instance.source_start = 20
    assert mapping.signature != KeyframeFrameMap(instance, 40).signature
