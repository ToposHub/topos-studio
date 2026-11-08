"""Read-only model probe: speed undo must restore exact animation keys.

Run from repository root: PYTHONPATH=. .venv/bin/python <this file>
"""
from __future__ import annotations

from dataclasses import replace
import json

from src.features.edit.core.commands import ChangeClipSpeedCommand, CommandStack
from src.features.edit.core.timeline_v2 import ClipInstance, EditTrack, TimelineSequence
from src.shared.media.metadata import Rational


def frames(sequence: TimelineSequence, clip_id: str) -> list[int]:
    clip = sequence.get_clip_by_id(clip_id)
    return [key.frame for key in clip.animated_params["opacity"].keyframes.keyframes]


def main() -> None:
    video = ClipInstance("v", "asset", "v1", 0, 100, 0, 100, linked_clip_id="a")
    video = video.with_keyframe_added("opacity", 1, 0.2)
    audio = replace(video, clip_id="a", track_id="a1", linked_clip_id="v", animated_params={})
    audio = audio.with_keyframe_added("opacity", 40, 0.8)
    timeline = TimelineSequence(
        "speed-undo", "Speed undo", fps=Rational(24, 1),
        video_tracks=(EditTrack("v1", "V1", "video", 0, clips=(video,)),),
        audio_tracks=(EditTrack("a1", "A1", "audio", 0, clips=(audio,)),),
    )
    stack = CommandStack()
    changed = stack.execute(ChangeClipSpeedCommand.create("v", "v1", 2.0), timeline)
    restored = stack.undo(changed)
    result = {phase: {cid: frames(seq, cid) for cid in ("v", "a")}
              for phase, seq in (("before", timeline), ("speed_2x", changed), ("undo", restored))}
    print(json.dumps(result, indent=2))
    assert frames(restored, "v") != frames(timeline, "v"), "Primary rounding defect no longer reproduced"
    assert frames(restored, "a") != frames(timeline, "a"), "Linked undo defect no longer reproduced"


if __name__ == "__main__":
    main()
