#!/usr/bin/env python3
"""Reproduce: after blade-splitting a clip, do segments share one decode stream?

Uses the REAL production classes (ClipInstance / TimelineSequence /
TimelineRenderPlan / timeline_ops.split_clip) with the REAL fps metadata of
tests/test_videos footage (all 25fps CFR) on both a 24fps and a 25fps
timeline, and reports:

1. the decode stream key assignment per segment (_decode_stream_key_for_clip)
2. the source-frame ranges each segment actually displays (mapped via
   calculate_source_frame) so overlaps/gaps at split points are visible
3. how many independent decode sources EditGpuDecodeManager would create
   for the same file (= number of distinct stream keys)
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from src.features.edit.core.timeline_v2 import ClipInstance, EditTrack, TimelineSequence
from src.features.edit.core.timeline_ops import split_clip
from src.features.edit.core.timeline_render_plan import (
    TimelineRenderPlan,
    calculate_source_frame,
)
from src.features.edit.core.clip_params import TransformParams, CropParams
from src.shared.media.metadata import Rational


def make_sequence(timeline_fps: float, clip_frames: int, source_frames: int,
                  source_fps: Rational) -> TimelineSequence:
    # fps metadata fields are the same wiring production uses to enable the
    # rational timeline->source mapper (timeline_v2.py:346-361).
    clip = ClipInstance(
        clip_id="clip-1",
        asset_id="asset-1",
        track_id="track-1",
        timeline_start=0,
        timeline_duration=clip_frames,
        source_start=0,
        source_duration=source_frames,
        speed=1.0,
        transform=TransformParams(),
        crop=CropParams(),
        _source_fps_num=source_fps.num,
        _source_fps_den=source_fps.den,
        _timeline_fps_num=int(timeline_fps * 1000),
        _timeline_fps_den=1000,
    )
    track = EditTrack(
        track_id="track-1", name="Video 1", track_type="video", index=0, clips=(clip,)
    )
    return TimelineSequence(
        name="Seq", fps=Rational(int(timeline_fps * 1000), 1000), video_tracks=(track,)
    )


def report(label: str, seq: TimelineSequence) -> None:
    plan = TimelineRenderPlan(seq)
    track = seq.video_tracks[0]
    keys = {}
    print(f"\n=== {label} ===")
    for clip in sorted(track.clips, key=lambda c: c.timeline_start):
        key = plan._decode_stream_key_for_clip(track, clip)
        keys.setdefault(key, []).append(clip.clip_id)
        first = calculate_source_frame(clip, clip.timeline_start)
        last = calculate_source_frame(clip, clip.timeline_end - 1)
        print(
            f"  clip {clip.clip_id[:8]:8} timeline[{clip.timeline_start:3},{clip.timeline_end:3}) "
            f"source[{clip.source_start:3},{clip.source_start + clip.source_duration:3}) "
            f"-> displays src [{first:3}..{last:3}] stream_key={key[:12]}"
        )
    print(f"  => distinct decode stream keys: {len(keys)}  (segments: {len(track.clips)})")
    for key, ids in keys.items():
        print(f"     key {key[:12]}: {len(ids)} segment(s)")


def main() -> int:
    source_fps = Rational(25, 1)  # every video in tests/test_videos
    clip_frames = 240

    # A: timeline fps == source fps (25)
    seq25 = make_sequence(25.0, clip_frames, clip_frames, source_fps)
    for cut in (60, 120, 180):
        target = next(iter(c for c in seq25.video_tracks[0].clips
                           if c.timeline_start < cut < c.timeline_end))
        seq25 = split_clip(seq25, target.clip_id, cut)
    report("timeline 25fps == source 25fps (3 blade cuts)", seq25)

    # B: timeline fps 24 (app default) with 25fps source
    seq24 = make_sequence(24.0, clip_frames, int(clip_frames * 25 / 24), source_fps)
    for cut in (60, 120, 180):
        target = next(iter(c for c in seq24.video_tracks[0].clips
                           if c.timeline_start < cut < c.timeline_end))
        seq24 = split_clip(seq24, target.clip_id, cut)
    report("timeline 24fps (default) / source 25fps (3 blade cuts)", seq24)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
